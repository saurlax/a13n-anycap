"""Execute fixed AnyCap operations with workspace-local files and credentials."""

import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from a13n_harness.environment.commands import ArgvCommand, CommandEnvironment, CommandRequest
from a13n_harness.environment.files import FileOperator, FileTreeRequest
from a13n_harness.environment.models import EnvironmentError
from a13n_harness.environment.providers import BoundEnvironment
from a13n_harness.environment.retention import EnvironmentOutputPolicy
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import JsonValue, SecretStr
from pydantic_ai.exceptions import ToolFailed

from a13n_anycap.client import API_ORIGIN
from a13n_anycap.operations import Input, Operation

MAX_BYTES = 256 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
MAX_FILES = 1024
CLI_SECONDS = 150
FILE_NAME = re.compile(r"[^\\/:*?\"<>|\x00]+")
KEY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,80}")


def http_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return url.scheme in ("http", "https") and bool(url.hostname) and url.username is None and url.password is None
    except ValueError:
        return False


def bounded_text(value: JsonValue) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 16_384 and "\x00" not in value


def validate_arguments(
    operation: Operation, arguments: dict[str, JsonValue], authorize_url: Callable[[str], None]
) -> None:
    fields = {field.name: field for field in operation.inputs}
    if set(arguments).difference(fields):
        raise ToolFailed("Use the parameter names listed for this AnyCap operation.")
    for field in fields.values():
        if field.name not in arguments:
            if field.required:
                raise ToolFailed(f"Missing AnyCap parameter {field.name}.")
            continue
        value = arguments[field.name]
        match field.kind:
            case "bool":
                valid = type(value) is bool
            case "int":
                valid = type(value) is int and -(2**31) <= value < 2**31
            case "params":
                valid = isinstance(value, dict) and len(value) <= 64 and all(KEY_NAME.fullmatch(key) for key in value)
            case "media_refs":
                valid = (
                    isinstance(value, list)
                    and 0 < len(value) <= 10
                    and all(
                        (isinstance(item, str) and bounded_text(item) and http_url(item))
                        or (
                            isinstance(item, dict)
                            and set(item) == {"workspace_path"}
                            and bounded_text(item["workspace_path"])
                        )
                        for item in value
                    )
                )
            case "texts" | "files" | "trees" | "urls":
                valid = isinstance(value, list) and 0 < len(value) <= 64 and all(map(bounded_text, value))
            case _:
                valid = bounded_text(value)
        if not valid:
            raise ToolFailed(f"Invalid AnyCap parameter {field.name}.")
        if field.kind in {"url", "urls"}:
            for url in value if isinstance(value, list) else [value]:
                if not isinstance(url, str) or not http_url(url):
                    raise ToolFailed(f"Provide an HTTP(S) URL for {field.name}.")
                authorize_url(url)
    if "timeout" in arguments and not re.fullmatch(r"[1-9][0-9]{0,2}s", str(arguments["timeout"])):
        raise ToolFailed("Set timeout to a number of seconds, such as 90s.")
    if "timeout" in arguments and int(str(arguments["timeout"])[:-1]) > CLI_SECONDS - 5:
        raise ToolFailed("Set an AnyCap timeout of at most 145 seconds.")
    if operation.command == ("snapshot", "restore"):
        url = urlsplit(str(arguments["url"]))
        if url.scheme != "https" or url.hostname not in {"drive.anycap.cloud", "drive.anycap.ai"}:
            raise ToolFailed("Use an HTTPS AnyCap Drive snapshot share URL.")
    if operation.command == ("annotate",) and (target := arguments.get("target")) and isinstance(target, str):
        if http_url(target):
            authorize_url(target)


def safe_leaf(name: str) -> str:
    if name in {"", ".", ".."} or not FILE_NAME.fullmatch(name):
        raise ToolFailed("This workspace file name cannot be staged.")
    return name


class WorkspaceFiles:
    def __init__(self, root: Path, files: FileOperator) -> None:
        self.root = root
        self.files = files
        self.bytes = 0
        self.count = 0

    async def _copy_in(self, source: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self.count += 1
        if self.count > MAX_FILES:
            raise ToolFailed("This AnyCap input has too many files.")
        with target.open("xb") as stream:
            async for chunk in self.files.read_bytes_stream(source):
                self.bytes += len(chunk)
                if self.bytes > MAX_BYTES:
                    raise ToolFailed("This AnyCap input exceeds the transfer limit.")
                stream.write(chunk)

    async def stage(self, path: str, *, tree: bool = False, target: Path | None = None) -> str:
        meta = await self.files.stat(path)
        if target is None:
            destination = Path(tempfile.mkdtemp(prefix="input-", dir=self.root))
            target = destination / safe_leaf(PurePosixPath(meta.path).name or "workspace")
        if meta.kind == "file":
            await self._copy_in(meta.path, target)
            return str(target)
        if not tree or meta.kind != "directory":
            raise ToolFailed("Provide a regular workspace file or an allowed directory.")
        entries = await self.files.list_tree(
            FileTreeRequest(
                root=meta.path,
                max_depth=16,
                max_entries=MAX_FILES,
                max_directory_calls=512,
                include_hidden=True,
                max_bytes=256 * 1024,
            )
        )
        if entries.truncated:
            raise ToolFailed("This workspace directory exceeds AnyCap's transfer limits.")
        target.mkdir()
        for entry in entries.entries:
            relative = PurePosixPath(entry.path).relative_to(PurePosixPath(meta.path))
            output = target.joinpath(*(safe_leaf(part) for part in relative.parts))
            if entry.kind == "directory":
                output.mkdir(parents=True, exist_ok=True)
            elif entry.kind == "file":
                await self._copy_in(entry.path, output)
            else:
                raise ToolFailed("AnyCap does not transfer workspace symlinks or special files.")
        return str(target)

    async def publish(self, source: Path, path: str, *, replace: bool = False) -> None:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BYTES:
            raise ToolFailed("The AnyCap output is missing or exceeds the transfer limit.")

        async def chunks() -> AsyncIterator[bytes]:
            with source.open("rb") as stream:
                while data := stream.read(65536):
                    yield data

        await self.files.write_bytes_stream(path, chunks(), mode="upsert" if replace else "create")

    async def publish_directory(self, source: Path, path: str) -> None:
        if not source.is_dir() or source.is_symlink():
            raise ToolFailed("AnyCap did not restore a directory.")
        entries = []
        size = 0
        for entry in source.rglob("*"):
            if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                raise ToolFailed("AnyCap returned an unsupported archive member.")
            if entry.is_file():
                size += entry.stat().st_size
            entries.append(entry)
            if size > MAX_BYTES or len(entries) > MAX_FILES:
                raise ToolFailed("The AnyCap output exceeds the transfer limit.")
        await self.files.mkdir(path, parents=True, exist_ok=False)
        for entry in sorted(entries):
            destination = f"{path.rstrip('/')}/{entry.relative_to(source).as_posix()}"
            if entry.is_dir():
                await self.files.mkdir(destination, parents=True, exist_ok=True)
            else:
                await self.publish(entry, destination)


async def generation_parameter(
    value: JsonValue, staging: WorkspaceFiles, authorize_url: Callable[[str], None], depth: int = 0
) -> JsonValue:
    if depth > 8:
        raise ToolFailed("AnyCap generation parameter nesting exceeds the limit.")
    if isinstance(value, dict):
        if set(value) == {"workspace_path"}:
            path = value["workspace_path"]
            if not isinstance(path, str) or not bounded_text(path):
                raise ToolFailed("Provide a valid workspace_path.")
            return await staging.stage(path)
        return {
            name: await generation_parameter(item, staging, authorize_url, depth + 1) for name, item in value.items()
        }
    if isinstance(value, list):
        return [await generation_parameter(item, staging, authorize_url, depth + 1) for item in value]
    if isinstance(value, str):
        if http_url(value):
            authorize_url(value)
        elif (
            value.startswith((".", "~", "@"))
            or any(char in value for char in ("\\", "/", "\x00"))
            or re.match(r"^[A-Za-z]:", value)
        ):
            raise ToolFailed("Pass local generation inputs as {workspace_path: ...}.")
    return value


def encode_flag(field: Input, value: JsonValue) -> list[str]:
    name = "--" + field.name.replace("_", "-")
    if isinstance(value, bool):
        return [f"{name}={str(value).lower()}"]
    if isinstance(value, list):
        return [f"{name}={item}" for item in value]
    return [f"{name}={value}"]


async def stage_value(field: Input, value: JsonValue, staging: WorkspaceFiles) -> JsonValue:
    if field.kind in {"file", "tree"} and isinstance(value, str):
        return await staging.stage(value, tree=field.kind == "tree")
    if field.kind in {"files", "trees"} and isinstance(value, list):
        return [await staging.stage(str(path), tree=field.kind == "trees") for path in value]
    return value


def cli_binary() -> str:
    candidate = os.getenv("ANYCAP_CLI_PATH") or shutil.which("anycap")
    if not candidate or not Path(candidate).is_file():
        raise ToolFailed("Install the AnyCap 0.6.6 native executable on the Service workers.")
    if os.name == "nt" and Path(candidate).suffix.lower() != ".exe":
        raise ToolFailed("Set ANYCAP_CLI_PATH to the native AnyCap executable.")
    return str(Path(candidate).resolve())


def process_env(root: Path, key: SecretStr) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in (
            "PATH",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        )
        if name in os.environ
    }
    return {
        **env,
        "ANYCAP_API_KEY": key.get_secret_value(),
        "ANYCAP_API_ENDPOINT": API_ORIGIN,
        "ANYCAP_CONFIG_DIR": str(root),
        "ANYCAP_NO_UPDATE": "1",
        "ANYCAP_NO_KEYRING": "1",
    }


def decode_result(stdout: bytes, *, succeeded: bool) -> JsonValue:
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        if succeeded and not stdout.strip():
            return {"status": "success"}
        raise ToolFailed("AnyCap did not return a usable response.") from None
    if not succeeded or (isinstance(data, dict) and data.get("status") == "error"):
        raise ToolFailed("AnyCap rejected the operation. Check the inputs and workspace credential.") from None
    return data


async def run_cli(binary: str, argv: list[str], cwd: Path, env: dict[str, str]) -> JsonValue:
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_OUTPUT + 1,
        )
    except OSError:
        raise ToolFailed("The AnyCap executable could not be started.") from None
    assert process.stdout is not None
    try:
        async with asyncio.timeout(CLI_SECONDS):
            try:
                await process.stdout.readexactly(MAX_OUTPUT + 1)
            except asyncio.IncompleteReadError as end:
                return decode_result(end.partial, succeeded=await process.wait() == 0)
            raise ToolFailed("The AnyCap response exceeded the size limit.")
    except TimeoutError:
        raise ToolFailed("AnyCap timed out. Check remote state before repeating changes or paid requests.") from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()


async def call_mcp(binary: str, tool: str, arguments: dict, cwd: Path, env: dict[str, str]) -> JsonValue:
    with open(os.devnull, "w") as discard:
        try:
            async with (
                asyncio.timeout(CLI_SECONDS),
                stdio_client(
                    StdioServerParameters(command=binary, args=["mcp", "--allow-root", str(cwd)], env=env, cwd=cwd),
                    errlog=discard,
                ) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                if not result.is_error:
                    data = result.structured_content
                    if data is None:
                        data = [item.model_dump(mode="json", by_alias=True) for item in result.content]
                    if len(json.dumps(data).encode()) <= MAX_OUTPUT:
                        return data
        except (OSError, ExceptionGroup, TimeoutError):
            raise ToolFailed("AnyCap MCP could not complete the operation.") from None
    raise ToolFailed("AnyCap MCP rejected the operation or returned too much data.")


def sanitize(value: JsonValue, root: Path, key_value: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(key_value, "[redacted]").replace(str(root), ".")
    if isinstance(value, list):
        return [sanitize(item, root, key_value) for item in value]
    if isinstance(value, dict):
        return {str(sanitize(name, root, key_value)): sanitize(item, root, key_value) for name, item in value.items()}
    return value


async def run_in_environment(
    environment: BoundEnvironment, operation: Operation, arguments: dict[str, JsonValue], workspace_id: str
) -> JsonValue:
    argv = list(operation.command)
    positionals = [str(arguments[field.name]) for field in operation.arguments if field.name in arguments]
    for field in operation.flags:
        if field.name in arguments:
            argv.extend(encode_flag(field, arguments[field.name]))
    if operation.command in (("draw",), ("annotate",)):
        argv.extend(("--no-wait", "--bind=127.0.0.1"))
    if operation.output:
        argv.append(f"--output={arguments['output_path']}")
    if positionals:
        argv.extend(("--", *positionals))
    request = CommandRequest(
        command=ArgvCommand(executable="anycap", arguments=tuple(argv)),
        environment=CommandEnvironment(
            set={
                "ANYCAP_NO_UPDATE": "1",
                "ANYCAP_NO_KEYRING": "1",
                "ANYCAP_CONFIG_DIR": f".anycap-a13n-collaboration/{workspace_id}",
            },
            unset=("ANYCAP_API_KEY", "ANYCAP_API_ENDPOINT"),
        ),
        output_policy=EnvironmentOutputPolicy(
            max_inline_bytes=MAX_OUTPUT, max_output_bytes=MAX_OUTPUT, overflow="fail"
        ),
    )
    try:
        async with asyncio.timeout(CLI_SECONDS):
            result = await environment.shell.exec(request)
    except (EnvironmentError, TimeoutError):
        raise ToolFailed(
            "AnyCap could not run in the Agent Environment. Verify that its native CLI is installed."
        ) from None
    return decode_result(result.output.stdout.inline or b"", succeeded=result.status.exit_code == 0)


async def execute(
    environment: BoundEnvironment,
    operation: Operation,
    arguments: dict[str, JsonValue],
    workspace_id: str,
    key: SecretStr | None,
    authorize_url: Callable[[str], None],
) -> JsonValue:
    validate_arguments(operation, arguments, authorize_url)
    if operation.transport == "environment":
        return await run_in_environment(environment, operation, arguments, workspace_id)
    if key is None:
        raise ToolFailed("AnyCap credentials are not configured for this workspace.")
    binary = cli_binary()
    with tempfile.TemporaryDirectory(prefix="anycap-a13n-") as directory:
        root = Path(directory)
        work = root / "work"
        work.mkdir()
        staging = WorkspaceFiles(work, environment.files)
        env = process_env(root / "config", key)
        try:
            if operation.transport == "mcp":
                prepared = dict(arguments)
                for field in operation.flags:
                    if field.kind == "media_refs" and field.name in prepared:
                        prepared[field.name] = await generation_parameter(prepared[field.name], staging, authorize_url)
                data = await call_mcp(binary, operation.command[0], prepared, work, env)
            else:
                data = await run_staged_cli(binary, operation, arguments, staging, env, authorize_url)
        except (EnvironmentError, OSError, ValueError, RecursionError):
            raise ToolFailed("The AnyCap workspace file transfer or command preparation failed.") from None
        return sanitize(data, root, key.get_secret_value())


async def run_staged_cli(
    binary: str,
    operation: Operation,
    arguments: dict[str, JsonValue],
    staging: WorkspaceFiles,
    env: dict[str, str],
    authorize_url: Callable[[str], None],
) -> JsonValue:
    argv = list(operation.command)
    project = arguments.get("project_dir")
    config_path = f"{project.rstrip('/')}/anycap.toml" if isinstance(project, str) else None
    config = staging.root / "anycap.toml"
    if config_path:
        try:
            await staging.stage(config_path, target=config)
        except EnvironmentError as error:
            if error.code not in {"environment_path_not_found", "not_found", "environment_not_found"}:
                raise
    for field in operation.flags:
        if field.name not in arguments:
            continue
        value = arguments[field.name]
        if field.kind == "params" and isinstance(value, dict):
            for name, item in value.items():
                entry = await generation_parameter(item, staging, authorize_url)
                encoded = entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False, allow_nan=False)
                argv.append(f"--param={name}={encoded}")
        else:
            argv.extend(encode_flag(field, await stage_value(field, value, staging)))
    if operation.command == ("video", "generate") and "no_wait" not in arguments and "output_path" not in arguments:
        argv.append("--no-wait")
    elif operation.command in {("video", "generate"), ("video", "tasks", "wait")} and "timeout" not in arguments:
        argv.append("--timeout=120s")
    output = arguments.get("output_path")
    local_output = None
    if isinstance(output, str):
        local_output = staging.root / "outputs" / safe_leaf(PurePosixPath(output).name)
        local_output.parent.mkdir()
        flag = "target" if operation.output == "directory" else "output"
        argv.append(f"--{flag}={local_output}")
    positionals = [
        str(await stage_value(field, arguments[field.name], staging))
        for field in operation.arguments
        if field.name in arguments
    ]
    if positionals:
        argv.extend(("--", *positionals))
    data = await run_cli(binary, argv, staging.root, env)
    published = []
    if local_output is not None and isinstance(output, str):
        if operation.output == "directory" and local_output.exists():
            await staging.publish_directory(local_output, output)
            published.append(output)
        else:
            files = sorted(local_output.parent.iterdir())
            if len(files) > MAX_FILES or sum(file.stat().st_size for file in files) > MAX_BYTES:
                raise ToolFailed("The AnyCap output exceeds the transfer limit.")
            for file in files:
                destination = (
                    output if file == local_output else str(PurePosixPath(output).with_name(safe_leaf(file.name)))
                )
                await staging.publish(file, destination)
                published.append(destination)
    if operation.output in {"file", "directory"} and not published:
        raise ToolFailed("AnyCap did not produce the requested workspace output.")
    if config_path and operation.effect == "write" and config.is_file():
        await staging.publish(config, config_path, replace=True)
        published.append(config_path)
    return {"result": data, "workspace_files": published} if published else data
