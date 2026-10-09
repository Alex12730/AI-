from __future__ import annotations

from sqlalchemy import inspect, text

from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import (
    Asset,
    AuditLog,
    ModelProfile,
    Project,
    Review,
    Shot,
    Storyboard,
    Task,
    User,
    WorkerStatus,
)


EXPECTED_TABLES = {
    "users",
    "projects",
    "project_members",
    "storyboards",
    "shots",
    "tasks",
    "assets",
    "reviews",
    "model_profiles",
    "audit_logs",
    "worker_statuses",
}


def test_sqlite_bootstrap_enables_wal_and_foreign_keys(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}")
    database = Database(settings)
    database.create_schema()

    assert EXPECTED_TABLES <= set(inspect(database.engine).get_table_names())
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_all_core_entities_can_be_persisted(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}")
    database = Database(settings)
    database.create_schema()

    with database.session() as session:
        admin = User(username="admin", password_hash="hash", role="admin")
        member = User(username="member", password_hash="hash", role="member")
        profile = ModelProfile(
            slug="demo",
            display_name="Demo 本地适配器",
            adapter_type="demo",
            enabled=True,
        )
        session.add_all([admin, member, profile])
        session.flush()

        project = Project(name="产品营销片", source_script="展示真实界面", created_by_id=member.id)
        project.members.append(member)
        session.add(project)
        session.flush()

        storyboard = Storyboard(project_id=project.id, created_by_id=member.id, status="approved")
        session.add(storyboard)
        session.flush()

        shot = Shot(
            storyboard_id=storyboard.id,
            sequence_no=1,
            title="界面录屏",
            prompt="真实录屏，不生成界面",
            scene_type="product_ui",
            duration_seconds=5,
            aspect_ratio="16:9",
        )
        session.add(shot)
        session.flush()

        task = Task(
            project_id=project.id,
            shot_id=shot.id,
            model_profile_id=profile.id,
            created_by_id=member.id,
            task_type="generate",
            status="queued",
            priority=1,
            payload_json={"prompt": "本地生成"},
        )
        session.add(task)
        session.flush()

        asset = Asset(project_id=project.id, shot_id=shot.id, task_id=task.id, kind="video", path="p/a.mp4")
        review = Review(project_id=project.id, shot_id=shot.id, reviewer_id=admin.id, status="approved")
        audit = AuditLog(actor_id=admin.id, action="storyboard.approve", entity_type="storyboard", entity_id=storyboard.id)
        session.add_all([asset, review, audit])

    with database.session() as session:
        assert session.query(User).count() == 2
        assert session.query(Project).count() == 1
        assert session.query(Storyboard).count() == 1
        assert session.query(Shot).count() == 1
        assert session.query(Task).count() == 1
        assert session.query(Asset).count() == 1
        assert session.query(Review).count() == 1
        assert session.query(ModelProfile).count() == 1
        assert session.query(AuditLog).count() == 1


def test_alembic_migrates_the_configured_non_default_database(tmp_path):
    custom = tmp_path / "nested" / "custom.db"
    database = Database(Settings(data_dir=tmp_path / "nested", database_url=f"sqlite:///{custom}"))
    database.migrate()
    assert custom.exists()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261009_0004"


def test_worker_status_is_unique_and_task_reference_is_cleared(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}")
    database = Database(settings)
    database.create_schema()

    with database.session() as session:
        user = User(username="member", password_hash="hash", role="member")
        session.add(user)
        session.flush()
        project = Project(name="片子", created_by_id=user.id)
        session.add(project)
        session.flush()
        task = Task(project_id=project.id, created_by_id=user.id, status="running")
        session.add(task)
        session.flush()
        status = WorkerStatus(worker_id="gpu-worker-1", state="running", current_task_id=task.id)
        session.add(status)
        session.flush()
        status_id = status.id
        session.delete(task)

    with database.session() as session:
        saved = session.get(WorkerStatus, status_id)
        assert saved.worker_id == "gpu-worker-1"
        assert saved.state == "running"
        assert saved.current_task_id is None
