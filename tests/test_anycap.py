import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from a13n_harness.configuration import RunConfiguration
from a13n_harness.plugin_factories import HarnessPluginFactoryContext
from a13n_service.app import build_app
from a13n_service.app_definitions import discover_apps
from a13n_service.infra.crypto import KeyRing
from a13n_service.infra.errors import ServiceError
from a13n_service.runs.tables import AttemptRow, RunRow
from pydantic import SecretStr
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel

from a13n_anycap import tools
from a13n_anycap.app import KEYS_VARIABLE, load_workspace_keys
from a13n_anycap.credentials import WorkspaceCredential, credential_location, get_workspace_key
from a13n_anycap.policy import WorkspacePolicyRow
from a13n_anycap.tools import APP_ID, AnyCapFactory, AnyCapTools

TASK_ID = "ct_0123456789abcdef"


def test_service_discovers_app_and_paid_tool_requests_approval() -> None:
    app = next(item for item in discover_apps() if item.app_id == "saurlax.anycap")
    assert app.distribution == "a13n-anycap"
    assert app.definition.factory_keys == {APP_ID}
    assert app.definition.frontend is not None
    assert app.definition.database is not None
    assert build_app(role="worker").state is not None
    plugin = AnyCapFactory().create_plugin(HarnessPluginFactoryContext(APP_ID, "anycap", {}, {}))
    agent = Agent(
        TestModel(call_tools=["anycap_submit_video"]),
        output_type=[str, DeferredToolRequests],
        capabilities=plugin.get_capabilities(),
    )
    result = asyncio.run(agent.run("Generate a video")).output
    assert isinstance(result, DeferredToolRequests)
    assert [request.tool_name for request in result.approvals] == ["anycap_submit_video"]


@pytest.fixture
def service_calls(monkeypatch):
    """Use the released Service's Run context contract with isolated storage and HTTP."""
    current = datetime.now(UTC)
    run = SimpleNamespace(
        id="run_a",
        status="running",
        current_attempt_id="rat_a",
        cancel_requested_at=None,
        principal_id="usr_test",
        workspace_id="ws_first",
        organization_id="org_test",
    )
    attempt = SimpleNamespace(run_id="run_a", status="running", lease_expires_at=current + timedelta(seconds=60))
    credentials = {}
    policies = {}
    rows = {
        AttemptRow: {"rat_a": attempt},
        WorkspaceCredential: credentials,
        WorkspacePolicyRow: policies,
    }

    def get_row(table, row_id):
        if table is RunRow:
            return run if row_id == run.id else None
        return rows[table].get(row_id)

    session = SimpleNamespace(get=AsyncMock(side_effect=get_row))
    keys = KeyRing(active_key_id="test", keys={"test": SecretStr(base64.b64encode(bytes(32)).decode())})
    state = SimpleNamespace(
        in_session=False,
        run=run,
        attempt=attempt,
        credentials=credentials,
        policies=policies,
        session=session,
        keys=keys,
    )

    @asynccontextmanager
    async def short_session(_):
        state.in_session = True
        try:
            yield session
        finally:
            state.in_session = False

    monkeypatch.setattr(tools, "short_session", short_session)
    monkeypatch.setattr(tools, "now", AsyncMock(return_value=current))
    monkeypatch.setattr(tools, "authorize_execution", AsyncMock())
    monkeypatch.setenv(KEYS_VARIABLE, '{"ws_first":"test-key","ws_second":"test-key"}')
    factory = AnyCapFactory()
    factory.runtime = SimpleNamespace(storage=object(), access=object(), keys=keys)
    factory.keys = load_workspace_keys()
    plugin = factory.create_plugin(HarnessPluginFactoryContext(APP_ID, "anycap", {}, {}))
    assert isinstance(plugin, AnyCapTools)
    context = SimpleNamespace(
        deps=SimpleNamespace(
            configuration=RunConfiguration(),
            instance=SimpleNamespace(
                identity=SimpleNamespace(issuer="a13n-service", subject="usr_test"),
                agent_instance_id="run_a",
                host_refs={"run_id": "run_a", "attempt_id": "rat_a"},
            ),
        ),
        tool_call_id="call_a",
    )
    return factory, plugin, context, state


def test_workspace_key_overrides_environment_and_is_bound_to_its_workspace(service_calls) -> None:
    factory, plugin, context, state = service_calls

    async def verify() -> None:
        async with httpx.AsyncClient() as http:
            factory.http = http
            encrypted = state.keys.protect(
                b"workspace-key", credential_location("org_test", "ws_first")
            ).model_dump_json()
            state.credentials["ws_first"] = WorkspaceCredential(workspace_id="ws_first", encrypted_key=encrypted)
            client, _ = await plugin._client(context, "video")
            assert client.key.get_secret_value() == "workspace-key"

            state.credentials["ws_second"] = WorkspaceCredential(workspace_id="ws_second", encrypted_key=encrypted)
            with pytest.raises(ServiceError):
                await get_workspace_key(state.session, state.keys, "org_test", "ws_second")

            del state.credentials["ws_first"]
            client, _ = await plugin._client(context, "video")
            assert client.key == factory.keys["ws_first"]

    asyncio.run(verify())


def test_workspace_policy_ignores_legacy_model_filters_and_blocks_disabled_features(service_calls) -> None:
    factory, plugin, context, state = service_calls
    state.policies["ws_first"] = WorkspacePolicyRow(
        workspace_id="ws_first",
        policy={"video": {"models": ["first"]}, "audio": {"enabled": False}},
    )
    calls = []

    def catalog(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert "Authorization" not in request.headers
        models = [
            {
                "model": name,
                "display_name": name,
                "status": "active",
                "operations": [{"operation": "generate", "modes": [{"mode": mode}]}],
            }
            for name, mode in [("first", "text-to-video"), ("second", "text-to-video"), ("edit-only", "edit-video")]
        ]
        return httpx.Response(200, json={"status": "success", "models": models})

    async def verify() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(catalog)) as http:
            factory.http = http
            result = await plugin.anycap_list_models(context, "video")
            assert [model["model"] for model in result["models"]] == ["first", "second", "edit-only"]
            with pytest.raises(ToolFailed, match="audio is disabled"):
                await plugin.anycap_generate_audio(context, "any-model", "Example")
            state.policies["ws_first"].policy = {"video": {"enabled": False, "models": []}}
            with pytest.raises(ToolFailed, match="video is disabled"):
                await plugin.anycap_submit_video(context, "first", "Example")
            assert calls == ["/v1/video/models"]

    asyncio.run(verify())


def test_full_operations_follow_workspace_policy_and_keep_video_handles_scoped(service_calls, monkeypatch) -> None:
    factory, plugin, context, state = service_calls
    context.deps.environment = object()
    executor = AsyncMock(return_value={"data": {"task_id": TASK_ID, "status": "queued"}})
    monkeypatch.setattr(tools, "execute", executor)

    async def verify() -> None:
        entries = await plugin.anycap_operations(context)
        assert {
            "audio_read",
            "video_read",
            "image_generate",
            "music_generate",
            "page_deploy",
            "web_search",
            "social_profile",
            "drive_upload",
            "snapshot_restore",
            "draw_start",
        } <= entries.keys()
        assert entries["drive_delete"]["tool"] == "anycap_execute"
        with pytest.raises(ToolFailed, match="Select an operation"):
            await plugin.anycap_read(context, "drive_delete", {})
        state.policies["ws_first"] = WorkspacePolicyRow(
            workspace_id="ws_first",
            policy={"audio_read": {"enabled": False}, "video_read": {"models": ["allowed"]}},
        )
        assert "audio_read" not in await plugin.anycap_operations(context)
        with pytest.raises(ToolFailed, match="audio_read is disabled"):
            await plugin.anycap_execute(context, "audio_read", {"url": "https://example.com/clip.wav"})
        with pytest.raises(ToolFailed, match="valid AnyCap model ID"):
            await plugin.anycap_execute(context, "video_read", {"url": "https://example.com/clip.mp4", "model": ""})
        with pytest.raises(ToolFailed, match="issued for this workspace"):
            await plugin.anycap_read(context, "video_task_get", {"task_id": TASK_ID})
        executor.assert_not_awaited()
        await plugin.anycap_execute(context, "video_read", {"url": "https://example.com/clip.mp4"})
        assert executor.await_args.args[2] == {"url": "https://example.com/clip.mp4"}
        submitted = await plugin.anycap_execute(context, "video_generate", {"model": "allowed", "prompt": "Test"})
        assert submitted["task_handle"] == tools.make_task_handle(factory.keys["ws_first"], "ws_first", TASK_ID)
        await plugin.anycap_read(context, "video_task_get", {"task_id": submitted["task_handle"]})
        assert executor.await_args.args[2] == {"task_id": TASK_ID}

    asyncio.run(verify())


def test_video_submit_and_cross_run_read_without_cross_workspace_access(service_calls) -> None:
    factory, plugin, context, state = service_calls
    requests = []

    def anycap(request):
        assert not state.in_session
        requests.append(request)
        assert request.url.host == "api.anycap.ai"
        assert request.headers["Authorization"] == "Bearer test-key"
        task = {"task_id": TASK_ID}
        if request.method == "POST":
            assert json.loads(request.content) == {
                "model": "test-video",
                "prompt": "A sunrise",
                "mode": "text-to-video",
                "generate_audio": False,
            }
            task.update(status="queued", poll_after_ms=2000)
            return httpx.Response(202, json={"status": "success", "data": task})
        assert request.url.path == f"/v1/video/tasks/{TASK_ID}"
        task.update(
            status="succeeded",
            result={
                "model": "test-video",
                "credit_cost": 2,
                "outputs": [{"type": "video", "url": "https://media.example/clip.mp4", "mime_type": "video/mp4"}],
            },
        )
        return httpx.Response(200, json={"status": "success", "data": task})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(anycap)) as factory.http:
            queued = await plugin.anycap_submit_video(context, "test-video", "A sunrise", generate_audio=False)
            assert queued["poll_after_ms"] == 2000
            assert queued["task_id"] == TASK_ID
            context.deps.instance.agent_instance_id = "run_b"
            context.deps.instance.host_refs["run_id"] = "run_b"
            state.run.id = "run_b"
            # The current attempt belongs to the new Run, just as a real Service claim would.
            state.attempt.run_id = "run_b"
            completed = await plugin.anycap_get_video_task(context, queued["task_handle"])
            assert completed["result"]["outputs"][0]["url"] == "https://media.example/clip.mp4"
            state.run.workspace_id = "ws_second"
            with pytest.raises(ToolFailed, match="issued for this workspace"):
                await plugin.anycap_get_video_task(context, queued["task_handle"])

    asyncio.run(check())
    assert [request.method for request in requests] == ["POST", "GET"]


def test_ambiguous_audio_submit_is_not_retried_or_leaked(service_calls) -> None:
    factory, plugin, context, state = service_calls
    requests = []

    def anycap(request):
        assert not state.in_session
        requests.append(request)
        assert request.url.path == "/v1/audio/generate"
        assert json.loads(request.content) == {
            "model": "test-audio",
            "prompt": "Hello",
            "mode": "text-to-audio",
            "speaker_ids": ["voice"],
        }
        raise httpx.ReadTimeout("Bearer test-key")

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(anycap)) as factory.http:
            with pytest.raises(ToolFailed, match="outcome is unknown") as error:
                await plugin.anycap_generate_audio(context, "test-audio", "Hello", "voice")
        assert "test-key" not in str(error.value)
        assert requests[0].headers["X-Trace-ID"] in str(error.value)

    asyncio.run(check())
    assert len(requests) == 1


def test_unapproved_host_or_cancelled_run_never_sends_http(service_calls) -> None:
    factory, plugin, context, state = service_calls

    def unexpected_http(_):
        pytest.fail("The plugin sent a request before establishing its authority")

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_http)) as factory.http:
            context.deps.configuration = RunConfiguration(allowed_hosts={"example.com"})
            with pytest.raises(ToolFailed, match="must allow api.anycap.ai"):
                await plugin.anycap_list_models(context, "audio")
            context.deps.configuration = RunConfiguration()
            state.run.cancel_requested_at = datetime.now(UTC)
            with pytest.raises(ToolFailed, match="no longer active"):
                await plugin.anycap_list_models(context, "audio")

    asyncio.run(check())


def test_invalid_startup_config_does_not_emit_secret(monkeypatch) -> None:
    monkeypatch.setenv(KEYS_VARIABLE, '{"ws_test":"test secret"}')
    with pytest.raises(ValueError, match=KEYS_VARIABLE) as error:
        load_workspace_keys()
    assert "test secret" not in str(error.value)
