"""Create the workspace access policy table."""

import sqlalchemy as sa
from a13n_service.app_database import migration_name
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_workspace_policy"
down_revision = "0001_workspace_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = migration_name("workspace_policy")
    op.create_table(
        table,
        sa.Column("workspace_id", sa.String(length=72), nullable=False),
        sa.Column("policy", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f(f"{table}__fk_workspace_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id", name=op.f(f"{table}__pk")),
    )


def downgrade() -> None:
    op.drop_table(migration_name("workspace_policy"))
