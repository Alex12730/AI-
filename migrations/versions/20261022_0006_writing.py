"""Immutable writing revisions and multiple frozen runs per project."""
from alembic import op
import sqlalchemy as sa

revision = "20261022_0006"
down_revision = "20261022_0005"
branch_labels = None
depends_on = None


def upgrade():
    from video_workstation.models import WritingDocument, WritingRevision, WritingPublication
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    uniques = [u for u in inspector.get_unique_constraints("creation_plans") if u["column_names"] == ["project_id"]]
    if uniques:
        convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
        with op.batch_alter_table("creation_plans", naming_convention=convention) as batch:
            for unique in uniques:
                batch.drop_constraint(unique["name"] or "uq_creation_plans_project_id", type_="unique")
    for idx in sa.inspect(bind).get_indexes("creation_plans"):
        if idx["unique"] and idx["column_names"] == ["project_id"]:
            op.drop_index(idx["name"], table_name="creation_plans")
    if "ix_creation_plans_project_id" not in {i["name"] for i in sa.inspect(bind).get_indexes("creation_plans")}:
        op.create_index("ix_creation_plans_project_id", "creation_plans", ["project_id"])
    for table in (WritingDocument.__table__, WritingRevision.__table__, WritingPublication.__table__):
        table.create(bind, checkfirst=True)


def downgrade():
    # Downgrading cannot restore project uniqueness when several runs exist.
    # Check before dropping any data.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT project_id FROM creation_plans GROUP BY project_id HAVING COUNT(*) > 1 LIMIT 1")).first():
        raise RuntimeError("存在多个制作版本，请恢复迁移前的配套数据库备份")
    for table in ("writing_publications", "writing_revisions", "writing_documents"):
        op.drop_table(table)
    with op.batch_alter_table("creation_plans") as batch:
        batch.create_unique_constraint("uq_creation_plans_project_id", ["project_id"])
