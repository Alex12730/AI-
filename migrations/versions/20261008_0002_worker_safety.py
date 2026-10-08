"""Worker lease token and model execution snapshot.

Revision ID: 20261008_0002
Revises: 20261008_0001
"""
from alembic import op
import sqlalchemy as sa

revision = "20261008_0002"
down_revision = "20261008_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("tasks")}
    columns = [
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("model_version_snapshot", sa.String(120), nullable=False, server_default=""),
        sa.Column("quantization_snapshot", sa.String(80), nullable=False, server_default=""),
        sa.Column("model_config_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("runtime_config_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("estimated_temp_bytes", sa.Integer(), nullable=False, server_default="0"),
    ]
    for column in columns:
        if column.name not in existing:
            op.add_column("tasks", column)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("tasks")}
    if "ix_tasks_lease_token" not in indexes:
        op.create_index("ix_tasks_lease_token", "tasks", ["lease_token"])


def downgrade() -> None:
    op.drop_index("ix_tasks_lease_token", table_name="tasks")
    for name in ["estimated_temp_bytes", "runtime_config_snapshot", "model_config_fingerprint", "quantization_snapshot", "model_version_snapshot", "lease_token"]:
        op.drop_column("tasks", name)
