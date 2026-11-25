"""Agent tools scoped to the authenticated Service workspace."""

import hashlib
import hmac
import json
from typing import Annotated, Literal

import httpx
from a13n_harness import AbstractHarnessPlugin, AgentContext
from a13n_harness.configuration import HostNotAllowedError
from a13n_harness.plugin_factories import HarnessPluginFactory, HarnessPluginFactoryContext
from a13n_service.infra.db import now, short_session
from a13n_service.infra.errors import ServiceError
from a13n_service.runs.attempts import AuthorityRevoked, authorize_execution
from a13n_service.runs.runtime import Runtime
from a13n_service.runs.tables import AttemptRow, RunRow
from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.exceptions import ToolFailed

from a13n_anycap.client import API_ORIGIN, AnyCapClient, AnyCapError, MediaResult, TaskStatus, error_code, parse_result
from a13n_anycap.credentials import APP_ID, get_workspace_key

type Media = Literal["video", "audio"]
type ModelId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
type TaskHandle = Annotated[str, Field(pattern=r"^ct_[A-Za-z0-9]{1,64}\.[0-9a-f]{32}$")]


class Configuration(BaseModel, extra="forbid"):
    require_generation_approval: bool = True


def signature(key: SecretStr, *parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":")).encode()
    return hmac.digest(key.get_secret_value().encode(), payload, hashlib.sha256).hex()[:32]


def make_task_handle(key: SecretStr, workspace_id: str, task_id: str) -> str:
    return f"{task_id}.{signature(key, 'video-task', workspace_id, task_id)}"


class AnyCapFactory(HarnessPluginFactory):
    configuration_model = Configuration
    display_name = "AnyCap"
    description = "Discover AnyCap models, generate video, and synthesize speech."

    def __init__(self) -> None:
        self.runtime: Runtime | None = None
        self.http: httpx.AsyncClient | None = None
        self.keys: dict[str, SecretStr] = {}

    @classmethod
    def plugin_key(cls) -> str:
        return APP_ID

    def create_plugin(self, context: HarnessPluginFactoryContext) -> AbstractHarnessPlugin:
        config = Configuration.model_validate(context.configuration)
        return AnyCapTools(context.plugin_id, self, config)


class AnyCapTools(AbstractHarnessPlugin):
    def __init__(self, plugin_id: str, factory: AnyCapFactory, config: Configuration) -> None:
        self._plugin_id = plugin_id
        self.factory = factory
        self.config = config

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    def get_capabilities(self) -> list[Capability[AgentContext]]:
        return [
            Capability(
                id=self.plugin_id,
                tools=[
                    Tool(self.anycap_list_models),
                    Tool(self.anycap_model_schema),
                    Tool(self.anycap_submit_video, requires_approval=self.config.require_generation_approval),
                    Tool(self.anycap_get_video_task),
                    Tool(self.anycap_generate_audio, requires_approval=self.config.require_generation_approval),
                ],
            ),
        ]

    async def _client(self, ctx: RunContext[AgentContext]) -> tuple[AnyCapClient, str]:
        runtime, http = self.factory.runtime, self.factory.http
        if runtime is None or http is None:
            raise ToolFailed("AnyCap is unavailable in this Service process.")
        instance = ctx.deps.instance
        run_id = instance.host_refs.get("run_id")
        if not run_id or instance.identity.issuer != "a13n-service" or instance.agent_instance_id != run_id:
            raise ToolFailed("AnyCap requires an active Service run.")
        try:
            ctx.deps.configuration.authorize_url(API_ORIGIN)
        except HostNotAllowedError:
            raise ToolFailed("The Run must allow api.anycap.ai to use AnyCap.") from None
        try:
            async with short_session(runtime.storage) as session:
                attempt_id = instance.host_refs.get("attempt_id")
                run = await session.get(RunRow, run_id)
                if (
                    run is None
                    or run.status != "running"
                    or run.current_attempt_id != attempt_id
                    or run.cancel_requested_at is not None
                    or run.principal_id != instance.identity.subject
                ):
                    raise ToolFailed("This Service run is no longer active.")
                attempt = await session.get(AttemptRow, attempt_id)
                if (
                    attempt is None
                    or attempt.run_id != run_id
                    or attempt.status != "running"
                    or attempt.lease_expires_at <= await now(session)
                ):
                    raise ToolFailed("This Service run is no longer active.")
                await authorize_execution(session, runtime.access, run)
                workspace_id = run.workspace_id
                key = await get_workspace_key(session, runtime.keys, run.organization_id, workspace_id)
        except AuthorityRevoked:
            raise ToolFailed("The Run is no longer authorized to use AnyCap.") from None
        except ServiceError:
            raise ToolFailed("Service could not verify the Run's authority.") from None
        if key is None:
            key = self.factory.keys.get(workspace_id)
        if key is None:
            raise ToolFailed("AnyCap credentials are not configured for this workspace.")
        return AnyCapClient(http, key), workspace_id

    def _trace_id(self, client: AnyCapClient, ctx: RunContext[AgentContext], tool: str) -> str:
        if not ctx.tool_call_id:
            raise ToolFailed("AnyCap generation requires a tool-call identity.")
        digest = signature(client.key, tool, ctx.deps.instance.agent_instance_id, self.plugin_id, ctx.tool_call_id)
        return f"trc_{digest}"

    async def anycap_list_models(self, ctx: RunContext[AgentContext], media: Media) -> dict:
        """List current AnyCap models for video or audio generation."""
        client, _ = await self._client(ctx)
        try:
            return await client.read(f"/v1/{media}/models")
        except AnyCapError as error:
            raise ToolFailed(str(error)) from None

    async def anycap_model_schema(self, ctx: RunContext[AgentContext], media: Media, model: ModelId) -> dict:
        """Get the generation schema for an AnyCap model before choosing parameters."""
        client, _ = await self._client(ctx)
        try:
            return await client.read(
                f"/v1/{media}/models/{model}/schema",
                params={"operation": "generate", "mode": f"text-to-{media}"},
            )
        except AnyCapError as error:
            raise ToolFailed(str(error)) from None

    async def anycap_submit_video(
        self,
        ctx: RunContext[AgentContext],
        model: ModelId,
        prompt: Annotated[str, Field(min_length=1, max_length=8000)],
        duration: Annotated[int | None, Field(gt=0)] = None,
        aspect_ratio: Annotated[str | None, Field(max_length=16)] = None,
        resolution: Annotated[str | None, Field(max_length=16)] = None,
        generate_audio: bool | None = None,
    ) -> dict:
        """Submit a paid text-to-video job. Save its handle to check status in a later Run."""
        client, workspace_id = await self._client(ctx)
        trace_id = self._trace_id(client, ctx, "submit-video")
        body = {
            "model": model,
            "prompt": prompt,
            "mode": "text-to-video",
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "generate_audio": generate_audio,
        }
        try:
            payload = await client.generate(
                "/v1/video/tasks", {key: value for key, value in body.items() if value is not None}, trace_id=trace_id
            )
            task = parse_result(TaskStatus, payload, trace_id=trace_id)
        except AnyCapError as error:
            raise ToolFailed(str(error)) from None
        return self._task_result(client.key, workspace_id, task)

    async def anycap_get_video_task(self, ctx: RunContext[AgentContext], task_handle: TaskHandle) -> dict:
        """Get a video's status and available download links using the workspace task handle."""
        client, workspace_id = await self._client(ctx)
        task_id = task_handle.split(".", 1)[0]
        if not hmac.compare_digest(task_handle, make_task_handle(client.key, workspace_id, task_id)):
            raise ToolFailed("Use an AnyCap task handle issued for this workspace and credential.")
        try:
            task = parse_result(TaskStatus, await client.read(f"/v1/video/tasks/{task_id}"))
        except AnyCapError as error:
            raise ToolFailed(str(error)) from None
        if task.task_id != task_id:
            raise ToolFailed("AnyCap returned an unexpected task ID.")
        return self._task_result(client.key, workspace_id, task)

    @staticmethod
    def _task_result(key: SecretStr, workspace_id: str, task: TaskStatus) -> dict:
        return {
            "task_id": task.task_id,
            "task_handle": make_task_handle(key, workspace_id, task.task_id),
            "status": task.status,
            "poll_after_ms": task.poll_after_ms,
            "result": task.result.model_dump(mode="json") if task.result else None,
            "error_code": error_code(task.error),
        }

    async def anycap_generate_audio(
        self,
        ctx: RunContext[AgentContext],
        model: ModelId,
        prompt: Annotated[str, Field(min_length=1, max_length=1000)],
        speaker_id: Annotated[str | None, Field(max_length=128)] = None,
    ) -> dict:
        """Synthesize a short paid speech clip and return download links."""
        client, _ = await self._client(ctx)
        trace_id = self._trace_id(client, ctx, "generate-audio")
        body = {"model": model, "prompt": prompt, "mode": "text-to-audio"}
        if speaker_id:
            body["speaker_ids"] = [speaker_id]
        try:
            payload = await client.generate("/v1/audio/generate", body, trace_id=trace_id)
            return parse_result(MediaResult, payload, trace_id=trace_id).model_dump(mode="json")
        except AnyCapError as error:
            raise ToolFailed(str(error)) from None
