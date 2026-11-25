"""Workspace-wide access rules for the AnyCap tools."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from a13n_anycap.credentials import DATABASE, Base
from a13n_anycap.operations import Feature

type Media = Literal["image", "video", "music", "audio"]
type ModelId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


class FeaturePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True


class WorkspacePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image: FeaturePolicy = FeaturePolicy()
    video: FeaturePolicy = FeaturePolicy()
    music: FeaturePolicy = FeaturePolicy()
    audio: FeaturePolicy = FeaturePolicy()
    image_read: FeaturePolicy = FeaturePolicy()
    video_read: FeaturePolicy = FeaturePolicy()
    audio_read: FeaturePolicy = FeaturePolicy()
    web: FeaturePolicy = FeaturePolicy()
    social: FeaturePolicy = FeaturePolicy()
    drive: FeaturePolicy = FeaturePolicy()
    page: FeaturePolicy = FeaturePolicy()
    snapshot: FeaturePolicy = FeaturePolicy()
    collaboration: FeaturePolicy = FeaturePolicy()
    account: FeaturePolicy = FeaturePolicy()

    def is_enabled(self, feature: Feature) -> bool:
        return getattr(self, feature).enabled


class WorkspacePolicyRow(Base):
    __tablename__ = DATABASE.name("workspace_policy")

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    policy: Mapped[dict] = mapped_column(JSONB)


async def get_workspace_policy(session: AsyncSession, workspace_id: str) -> WorkspacePolicy:
    row = await session.get(WorkspacePolicyRow, workspace_id)
    if row is None:
        return WorkspacePolicy()
    # Earlier previews stored a model list next to each feature toggle.
    policy = {
        name: {key: value for key, value in settings.items() if key != "models"}
        if isinstance(settings, dict)
        else settings
        for name, settings in row.policy.items()
    }
    return WorkspacePolicy.model_validate(policy)
