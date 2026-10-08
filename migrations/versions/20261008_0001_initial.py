"""Initial workstation schema.

Revision ID: 20261008_0001
Revises:
"""

from alembic import op

from video_workstation.models import Base

revision = "20261008_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
