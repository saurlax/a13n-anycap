"""Service App registration and process resources."""

import os
from contextlib import AsyncExitStack
from typing import Annotated

import httpx
from a13n_service.app_definitions import AppDefinition
from a13n_service.runs.runtime import Runtime
from pydantic import Field, SecretStr, StringConstraints, TypeAdapter

from a13n_anycap.tools import APP_ID, AnyCapFactory

type WorkspaceId = Annotated[str, StringConstraints(pattern=r"^ws_[a-zA-Z0-9]+$")]
type ApiKey = Annotated[SecretStr, Field(min_length=1, max_length=1024)]
WORKSPACE_KEYS = TypeAdapter(dict[WorkspaceId, ApiKey])
KEYS_VARIABLE = "A13N_ANYCAP_WORKSPACE_KEYS"


def load_workspace_keys() -> dict[str, SecretStr]:
    try:
        keys = WORKSPACE_KEYS.validate_json(os.environ.get(KEYS_VARIABLE, "{}"))
        for key in keys.values():
            value = key.get_secret_value()
            if not value.isascii() or not value.isprintable() or any(char.isspace() for char in value):
                raise ValueError
    except ValueError:
        raise ValueError(f"{KEYS_VARIABLE} must be a JSON object mapping workspace IDs to API keys.") from None
    return keys


async def startup(runtime: Runtime, stack: AsyncExitStack) -> None:
    keys = load_workspace_keys()
    factory = runtime.plugins.require(APP_ID)
    if not isinstance(factory, AnyCapFactory):
        raise RuntimeError("Unexpected AnyCap factory")
    http = await stack.enter_async_context(httpx.AsyncClient(timeout=55, follow_redirects=False))
    factory.runtime = runtime
    factory.http = http
    factory.keys = keys


def definition() -> AppDefinition:
    return AppDefinition(
        app_id=APP_ID,
        name="AnyCap",
        factory_keys=frozenset({APP_ID}),
        startup=startup,
    )
