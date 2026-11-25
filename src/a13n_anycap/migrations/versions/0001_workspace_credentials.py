"""Create the workspace credential table."""

import sqlalchemy as sa
from a13n_service.app_database import migration_name
from alembic import op

revision = "0001_workspace_credentials"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    local_name = "workspace_credentials"
    table = migration_name(local_name)
    op.create_table(
        table,
        sa.Column("workspace_id", sa.String(length=72), nullable=False),
        sa.Column("encrypted_key", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f(f"{table}__fk_workspace_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id", name=op.f(f"{table}__pk")),
    )


def downgrade() -> None:
    op.drop_table(migration_name("workspace_credentials"))
