import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pydantic_ai.exceptions import ToolFailed

from a13n_anycap import executor
from a13n_anycap.operations import OPERATIONS


def test_executor_accepts_only_declared_parameters_and_snapshot_origin() -> None:
    with pytest.raises(ToolFailed, match="parameter names"):
        executor.validate_arguments(
            OPERATIONS["image_generate"], {"prompt": "x", "endpoint": "https://other.example"}, print
        )
    with pytest.raises(ToolFailed, match="snapshot share URL"):
        executor.validate_arguments(
            OPERATIONS["snapshot_restore"],
            {"url": "https://other.example/s/test", "output_path": "/workspace/out"},
            print,
        )


def test_media_staging_uses_workspace_file_api_and_fixed_native_cli_flags(monkeypatch, tmp_path) -> None:
    async def contents(path):
        assert path == "/workspace/input.png"
        yield b"workspace-content"

    files = SimpleNamespace(
        stat=AsyncMock(return_value=SimpleNamespace(kind="file", path="/workspace/input.png")),
        read_bytes_stream=contents,
    )
    command = AsyncMock(return_value={"outputs": [{"url": "https://anycap.example/asset"}]})
    monkeypatch.setattr(executor, "run_cli", command)
    staging = executor.WorkspaceFiles(tmp_path, files)

    async def verify() -> None:
        operation = OPERATIONS["image_generate"]
        args = {
            "model": "sample",
            "prompt": "--endpoint=other",
            "param": {"images": [{"workspace_path": "/workspace/input.png"}]},
        }
        result = await executor.run_staged_cli("anycap", operation, args, staging, {}, print)
        assert result == command.return_value
        argv = command.await_args.args[1]
        assert argv[:2] == ["image", "generate"]
        assert "--prompt=--endpoint=other" in argv
        assert not any(value.startswith("--endpoint") for value in argv)
        assert (
            tmp_path / next(path.name for path in tmp_path.iterdir()) / "input.png"
        ).read_bytes() == b"workspace-content"
        with pytest.raises(ToolFailed, match="workspace_path"):
            await executor.generation_parameter("../host-secret", staging, print)

    asyncio.run(verify())


def test_credentials_are_scoped_to_child_process_without_inheriting_other_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("A13N_DATABASE__URL", "private-database")
    monkeypatch.setenv("ANYCAP_API_KEY", "unrelated-key")
    result = executor.process_env(tmp_path / "config", SecretStr("workspace-key"))
    assert result["ANYCAP_API_KEY"] == "workspace-key"
    assert result["ANYCAP_CONFIG_DIR"] == str(tmp_path / "config")
    assert result["ANYCAP_NO_UPDATE"] == result["ANYCAP_NO_KEYRING"] == "1"
    assert "A13N_DATABASE__URL" not in result
