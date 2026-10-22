"""Persist guided personal production plans without changing legacy projects."""
from alembic import op
import sqlalchemy as sa

revision = "20261022_0005"
down_revision = "20261009_0004"
branch_labels = None
depends_on = None


def upgrade():
    # Initial migration uses current metadata for brand-new installations.
    if sa.inspect(op.get_bind()).has_table("creation_plans"):
        return
    op.create_table("creation_plans",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("storyboard_id", sa.String(32), sa.ForeignKey("storyboards.id"), nullable=False, unique=True),
        sa.Column("created_by_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipe", sa.String(30), nullable=False),
        sa.Column("target_seconds", sa.Integer(), nullable=False),
        sa.Column("aspect_ratio", sa.String(10), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_creation_plans_status", "creation_plans", ["status"])


def downgrade():
    op.drop_table("creation_plans")
