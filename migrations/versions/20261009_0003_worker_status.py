"""Persist worker runtime status.

Revision ID: 20261009_0003
Revises: 20261008_0002
"""
from alembic import op
import sqlalchemy as sa


revision = "20261009_0003"
down_revision = "20261008_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "worker_statuses" not in inspector.get_table_names():
        op.create_table(
            "worker_statuses",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("worker_id", sa.String(120), nullable=False),
            sa.Column("state", sa.String(20), nullable=False, server_default="starting"),
            sa.Column("current_task_id", sa.String(32), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("worker_statuses")}
    if "ix_worker_statuses_worker_id" not in indexes:
        op.create_index("ix_worker_statuses_worker_id", "worker_statuses", ["worker_id"], unique=True)
    if "ix_worker_statuses_state" not in indexes:
        op.create_index("ix_worker_statuses_state", "worker_statuses", ["state"])
    if "ix_worker_statuses_current_task_id" not in indexes:
        op.create_index("ix_worker_statuses_current_task_id", "worker_statuses", ["current_task_id"])
    if "ix_worker_statuses_last_heartbeat_at" not in indexes:
        op.create_index("ix_worker_statuses_last_heartbeat_at", "worker_statuses", ["last_heartbeat_at"])


def downgrade() -> None:
    op.drop_table("worker_statuses")
