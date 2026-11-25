"""Workspace credentials protected by the Service key ring."""

from typing import Annotated, Literal

from a13n_service.app_database import AppDatabase
from a13n_service.infra.crypto import Envelope, KeyRing, SecretLocation
from a13n_service.tenancy.tables import WorkspaceRow
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

APP_ID = "saurlax.anycap"
DATABASE = AppDatabase(APP_ID, migrations="a13n_anycap:migrations")
DATABASE.reference(WorkspaceRow.__table__)


def validate_api_key(key: SecretStr) -> SecretStr:
    value = key.get_secret_value()
    if not value.isascii() or not value.isprintable() or any(char.isspace() for char in value):
        raise ValueError("Enter an AnyCap API key without whitespace.")
    return key


type ApiKey = Annotated[SecretStr, Field(min_length=1, max_length=1024), AfterValidator(validate_api_key)]
type CredentialSource = Literal["workspace", "environment", "none"]


class Base(DeclarativeBase):
    metadata = DATABASE.metadata


class WorkspaceCredential(Base):
    __tablename__ = DATABASE.name("workspace_credentials")

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    encrypted_key: Mapped[str]


class CredentialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: ApiKey


class CredentialStatus(BaseModel):
    source: CredentialSource


def credential_location(organization_id: str, workspace_id: str) -> SecretLocation:
    return SecretLocation(organization_id, WorkspaceCredential.__tablename__, "encrypted_key", workspace_id)


async def get_workspace_key(
    session: AsyncSession, keys: KeyRing, organization_id: str, workspace_id: str
) -> SecretStr | None:
    row = await session.get(WorkspaceCredential, workspace_id)
    if row is None:
        return None
    envelope = Envelope.model_validate_json(row.encrypted_key)
    return SecretStr(keys.reveal(envelope, credential_location(organization_id, workspace_id)).decode())
