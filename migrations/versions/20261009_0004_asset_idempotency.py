"""Make task asset registration idempotent.

Revision ID: 20261009_0004
Revises: 20261009_0003
"""
from alembic import op
import sqlalchemy as sa


revision = "20261009_0004"
down_revision = "20261009_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("assets")}
    if "uq_assets_task_kind" not in indexes:
        op.create_index("uq_assets_task_kind", "assets", ["task_id", "kind"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_assets_task_kind", table_name="assets")
