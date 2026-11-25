"""Service App registration and process resources."""

import os
from contextlib import AsyncExitStack
from typing import Annotated

import httpx
from a13n_service.app_definitions import AppDefinition
from a13n_service.app_plugins import Frontend
from a13n_service.infra.audit import record
from a13n_service.infra.db import short_session
from a13n_service.runs.runtime import Runtime
from a13n_service.tenancy.access import administering_workspace
from a13n_service.tenancy.app_plugins import app_router
from a13n_service.tenancy.requests import Actor, CurrentRuntime, WorkspaceId
from pydantic import SecretStr, StringConstraints, TypeAdapter
from sqlalchemy.dialects.postgresql import insert

from a13n_anycap.credentials import (
    APP_ID,
    DATABASE,
    ApiKey,
    CredentialStatus,
    CredentialUpdate,
    WorkspaceCredential,
    credential_location,
)
from a13n_anycap.policy import WorkspacePolicy, WorkspacePolicyRow, get_workspace_policy
from a13n_anycap.tools import AnyCapFactory

type WorkspaceKeyId = Annotated[str, StringConstraints(pattern=r"^ws_[a-zA-Z0-9]+$")]
WORKSPACE_KEYS = TypeAdapter(dict[WorkspaceKeyId, ApiKey])
KEYS_VARIABLE = "ANYCAP_WORKSPACE_KEYS"
router = app_router(APP_ID)


def load_workspace_keys() -> dict[str, SecretStr]:
    try:
        return WORKSPACE_KEYS.validate_json(os.environ.get(KEYS_VARIABLE, "{}"))
    except ValueError:
        raise ValueError(f"{KEYS_VARIABLE} must be a JSON object mapping workspace IDs to API keys.") from None


def fallback_status(runtime: Runtime, workspace_id: str) -> CredentialStatus:
    factory = runtime.plugins.require(APP_ID)
    assert isinstance(factory, AnyCapFactory)
    return CredentialStatus(source="environment" if workspace_id in factory.keys else "none")


@router.get("/credential")
async def credential_status(runtime: CurrentRuntime, workspace_id: WorkspaceId) -> CredentialStatus:
    async with short_session(runtime.storage) as session:
        if await session.get(WorkspaceCredential, workspace_id) is not None:
            return CredentialStatus(source="workspace")
    return fallback_status(runtime, workspace_id)


@router.put("/credential")
async def save_credential(
    body: CredentialUpdate, runtime: CurrentRuntime, actor: Actor, workspace_id: WorkspaceId
) -> CredentialStatus:
    action = "anycap.credential.save"
    async with administering_workspace(runtime.storage, runtime.access, actor, workspace_id, action=action) as (
        session,
        scope,
    ):
        envelope = runtime.keys.protect(
            body.api_key.get_secret_value().encode(), credential_location(scope.organization_id, workspace_id)
        ).model_dump_json()
        stmt = insert(WorkspaceCredential).values(workspace_id=workspace_id, encrypted_key=envelope)
        await session.execute(
            stmt.on_conflict_do_update(index_elements=["workspace_id"], set_={"encrypted_key": envelope})
        )
        record(
            session, scope, actor_id=actor.id, action=action, target_kind="anycap_credential", target_id=workspace_id
        )
    return CredentialStatus(source="workspace")


@router.delete("/credential")
async def delete_credential(runtime: CurrentRuntime, actor: Actor, workspace_id: WorkspaceId) -> CredentialStatus:
    action = "anycap.credential.delete"
    async with administering_workspace(runtime.storage, runtime.access, actor, workspace_id, action=action) as (
        session,
        scope,
    ):
        if (row := await session.get(WorkspaceCredential, workspace_id)) is not None:
            await session.delete(row)
            record(
                session,
                scope,
                actor_id=actor.id,
                action=action,
                target_kind="anycap_credential",
                target_id=workspace_id,
            )
    return fallback_status(runtime, workspace_id)


@router.get("/policy")
async def workspace_policy(runtime: CurrentRuntime, workspace_id: WorkspaceId) -> WorkspacePolicy:
    async with short_session(runtime.storage) as session:
        return await get_workspace_policy(session, workspace_id)


@router.put("/policy")
async def save_policy(
    body: WorkspacePolicy, runtime: CurrentRuntime, actor: Actor, workspace_id: WorkspaceId
) -> WorkspacePolicy:
    action = "anycap.policy.save"
    async with administering_workspace(runtime.storage, runtime.access, actor, workspace_id, action=action) as (
        session,
        scope,
    ):
        policy = body.model_dump(mode="json")
        stmt = insert(WorkspacePolicyRow).values(workspace_id=workspace_id, policy=policy)
        await session.execute(stmt.on_conflict_do_update(index_elements=["workspace_id"], set_={"policy": policy}))
        record(session, scope, actor_id=actor.id, action=action, target_kind="anycap_policy", target_id=workspace_id)
    return body


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
        routers=(router,),
        frontend=Frontend("a13n_anycap", "static", styles=("style.css",)),
        database=DATABASE,
        startup=startup,
    )
