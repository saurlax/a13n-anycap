"""Agent tools scoped to the authenticated Service workspace."""

import hashlib
import hmac
import json
import re
from dataclasses import asdict
from typing import Annotated

import httpx
from a13n_harness import AbstractHarnessPlugin, AgentContext
from a13n_harness.configuration import HostNotAllowedError
from a13n_harness.plugin_factories import HarnessPluginFactory, HarnessPluginFactoryContext
from a13n_service.infra.db import now, short_session
from a13n_service.infra.errors import ServiceError
from a13n_service.runs.attempts import AuthorityRevoked, authorize_execution
from a13n_service.runs.runtime import Runtime
from a13n_service.runs.tables import AttemptRow, RunRow
from pydantic import BaseModel, Field, JsonValue, SecretStr, TypeAdapter, ValidationError
from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.exceptions import ToolFailed

from a13n_anycap.client import API_ORIGIN, AnyCapClient, AnyCapError, MediaResult, TaskStatus, error_code, parse_result
from a13n_anycap.credentials import APP_ID, get_workspace_key
from a13n_anycap.executor import execute
from a13n_anycap.operations import OPERATIONS, Feature
from a13n_anycap.policy import Media, ModelId, WorkspacePolicy, get_workspace_policy

type TaskHandle = Annotated[str, Field(pattern=r"^ct_[A-Za-z0-9]{1,64}\.[0-9a-f]{32}$")]
MODEL_ID = TypeAdapter(ModelId)
VIDEO_TASK = re.compile(r"^ct_[A-Za-z0-9]{1,64}$")


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
                    Tool(self.anycap_operations),
                    Tool(self.anycap_read),
                    Tool(self.anycap_execute, requires_approval=self.config.require_generation_approval),
                ],
            ),
        ]

    async def _client(self, ctx: RunContext[AgentContext], media: Media) -> tuple[AnyCapClient, str]:
        workspace_id, settings, key = await self._workspace(ctx, credential=True)
        self._check_policy(settings, media)
        if self.factory.http is None or key is None:
            raise ToolFailed("AnyCap credentials are not configured for this workspace.")
        return AnyCapClient(self.factory.http, key), workspace_id

    async def _workspace(
        self, ctx: RunContext[AgentContext], *, credential: bool
    ) -> tuple[str, WorkspacePolicy, SecretStr | None]:
        runtime = self.factory.runtime
        if runtime is None:
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
                settings = await get_workspace_policy(session, workspace_id)
                key = (
                    await get_workspace_key(session, runtime.keys, run.organization_id, workspace_id)
                    if credential
                    else None
                )
        except AuthorityRevoked:
            raise ToolFailed("The Run is no longer authorized to use AnyCap.") from None
        except ServiceError:
            raise ToolFailed("Service could not verify the Run's authority.") from None
        return workspace_id, settings, (key or self.factory.keys.get(workspace_id)) if credential else None

    @staticmethod
    def _check_policy(policy: WorkspacePolicy, feature: Feature) -> None:
        if not policy.is_enabled(feature):
            raise ToolFailed(f"AnyCap {feature} is disabled for this workspace.")

    async def anycap_operations(self, ctx: RunContext[AgentContext], feature: Feature | None = None) -> dict:
        """Discover enabled AnyCap operations and their required or optional parameters."""
        _, settings, _ = await self._workspace(ctx, credential=False)
        return {
            name: {
                "feature": operation.feature,
                "description": operation.description,
                "tool": "anycap_read" if operation.effect == "read" else "anycap_execute",
                "parameters": [asdict(field) for field in operation.inputs],
            }
            for name, operation in OPERATIONS.items()
            if (feature is None or operation.feature == feature) and settings.is_enabled(operation.feature)
        }

    async def anycap_read(
        self, ctx: RunContext[AgentContext], operation: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        """Call a read-only AnyCap operation listed by anycap_operations."""
        return await self._operation(ctx, operation, arguments, "read")

    async def anycap_execute(
        self, ctx: RunContext[AgentContext], operation: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        """Call an AnyCap operation with possible charges or changes. Discover its inputs with anycap_operations."""
        return await self._operation(ctx, operation, arguments, "write")

    async def _operation(
        self, ctx: RunContext[AgentContext], name: str, arguments: dict[str, JsonValue], effect: str
    ) -> JsonValue:
        operation = OPERATIONS.get(name)
        if operation is None or operation.effect != effect:
            raise ToolFailed("Select an operation listed for this AnyCap tool.")
        workspace_id, settings, key = await self._workspace(ctx, credential=operation.transport != "environment")
        self._check_policy(settings, operation.feature)
        if operation.model and "model" in arguments:
            try:
                MODEL_ID.validate_python(arguments["model"])
            except ValidationError:
                raise ToolFailed("Select a valid AnyCap model ID.") from None
        if name in {"video_task_get", "video_task_wait"}:
            if key is None:
                raise ToolFailed("AnyCap credentials are not configured for this workspace.")
            handle = arguments.get("task_id")
            task_id = handle.split(".", 1)[0] if isinstance(handle, str) else ""
            if not VIDEO_TASK.fullmatch(task_id) or not hmac.compare_digest(
                handle, make_task_handle(key, workspace_id, task_id)
            ):
                raise ToolFailed("Use an AnyCap task handle issued for this workspace and credential.")
            arguments = {**arguments, "task_id": task_id}
        try:
            result = await execute(
                ctx.deps.environment, operation, arguments, workspace_id, key, ctx.deps.configuration.authorize_url
            )
        except HostNotAllowedError:
            raise ToolFailed("The Run's host policy disallows this AnyCap input URL.") from None
        if name == "video_generate" and key is not None and isinstance(result, dict):
            data = result.get("result", result)
            if isinstance(data, dict):
                data = data.get("data", data)
            if (
                isinstance(data, dict)
                and isinstance(task_id := data.get("task_id"), str)
                and VIDEO_TASK.fullmatch(task_id)
            ):
                result["task_handle"] = make_task_handle(key, workspace_id, task_id)
        return result

    def _trace_id(self, client: AnyCapClient, ctx: RunContext[AgentContext], tool: str) -> str:
        if not ctx.tool_call_id:
            raise ToolFailed("AnyCap generation requires a tool-call identity.")
        digest = signature(client.key, tool, ctx.deps.instance.agent_instance_id, self.plugin_id, ctx.tool_call_id)
        return f"trc_{digest}"

    async def anycap_list_models(self, ctx: RunContext[AgentContext], media: Media) -> dict:
        """List available AnyCap models for image, video, music or audio."""
        client, _ = await self._client(ctx, media)
        try:
            return await client.catalog(client.http, media)
        except AnyCapError as error:
            raise ToolFailed(str(error)) from None

    async def anycap_model_schema(
        self,
        ctx: RunContext[AgentContext],
        media: Media,
        model: ModelId,
        operation: Annotated[str, Field(pattern=r"^[a-z][a-z-]{0,40}$")] = "generate",
        mode: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9-]{0,64}$")] = None,
    ) -> dict:
        """Get the exact schema for an AnyCap model, operation and mode."""
        client, _ = await self._client(ctx, media)
        try:
            return await client.read(
                f"/v1/{media}/models/{model}/schema",
                params={"operation": operation, **({"mode": mode} if mode else {})},
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
        client, workspace_id = await self._client(ctx, "video")
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
        client, workspace_id = await self._client(ctx, "video")
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
        client, _ = await self._client(ctx, "audio")
        trace_id = self._trace_id(client, ctx, "generate-audio")
        body = {"model": model, "prompt": prompt, "mode": "text-to-audio"}
        if speaker_id:
            body["speaker_ids"] = [speaker_id]
        try:
            payload = await client.generate("/v1/audio/generate", body, trace_id=trace_id)
            return parse_result(MediaResult, payload, trace_id=trace_id).model_dump(mode="json")
        except AnyCapError as error:
            raise ToolFailed(str(error)) from None
