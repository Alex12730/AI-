from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import ModelProfile, Project, Shot, Storyboard, Task, User
from video_workstation.queue import QueueService


@pytest.fixture()
def database(tmp_path):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}"))
    db.create_schema()
    return db


def seed(session):
    user = User(username="member", password_hash="hash", role="member")
    profile = ModelProfile(slug="demo", display_name="Demo", adapter_type="demo", enabled=True)
    session.add_all([user, profile])
    session.flush()
    project = Project(name="片子", created_by_id=user.id, status="approved")
    storyboard = Storyboard(project=project, created_by_id=user.id, status="approved")
    shot = Shot(
        storyboard=storyboard,
        sequence_no=1,
        title="镜头",
        prompt="本地生成",
        scene_type="broll",
        duration_seconds=5,
        aspect_ratio="16:9",
        status="approved",
    )
    session.add_all([project, storyboard, shot])
    session.flush()
    return user, project, shot, profile


def add_task(session, user, project, shot, profile, *, priority=2, created_at=None):
    task = Task(
        project_id=project.id,
        shot_id=shot.id,
        model_profile_id=profile.id,
        created_by_id=user.id,
        task_type="generate",
        status="queued",
        priority=priority,
        created_at=created_at or datetime.now(timezone.utc),
        payload_json={"prompt": shot.prompt},
    )
    session.add(task)
    session.flush()
    return task


def test_claim_uses_priority_aging_and_allows_only_one_running_task(database):
    now = datetime.now(timezone.utc)
    with database.session() as session:
        user, project, shot, profile = seed(session)
        old_p2 = add_task(session, user, project, shot, profile, priority=2, created_at=now - timedelta(hours=2, minutes=1))
        add_task(session, user, project, shot, profile, priority=1, created_at=now)

    with database.session() as session:
        claimed = QueueService(session).claim_next("worker-a", now=now, lease_seconds=60)
        assert claimed.id == old_p2.id
        assert claimed.status == "running"
        assert claimed.attempts == 1

    with database.session() as session:
        assert QueueService(session).claim_next("worker-b", now=now, lease_seconds=60) is None


def test_expired_lease_is_recovered_without_duplicate_execution(database):
    now = datetime.now(timezone.utc)
    with database.session() as session:
        user, project, shot, profile = seed(session)
        task = add_task(session, user, project, shot, profile)
        task.status = "running"
        task.attempts = 1
        task.leased_by = "dead-worker"
        task.lease_expires_at = now - timedelta(seconds=1)

    with database.session() as session:
        recovered = QueueService(session).recover_expired(now=now)
        assert recovered == 1
        task = session.get(Task, task.id)
        assert task.status == "queued"
        assert task.leased_by is None


def test_infrastructure_failure_retries_once_but_quality_failure_does_not(database):
    now = datetime.now(timezone.utc)
    with database.session() as session:
        user, project, shot, profile = seed(session)
        infra = add_task(session, user, project, shot, profile)

    with database.session() as session:
        service = QueueService(session)
        task = service.claim_next("worker", now=now)
        service.fail(task, error_class="infrastructure", message="模型进程退出")
        assert task.status == "queued"

    with database.session() as session:
        service = QueueService(session)
        task = service.claim_next("worker", now=now + timedelta(minutes=1))
        service.fail(task, error_class="infrastructure", message="再次退出")
        assert task.id == infra.id
        assert task.status == "terminal_failed"

    with database.session() as session:
        user = session.query(User).filter_by(username="member").one()
        project = session.query(Project).one()
        shot = session.query(Shot).one()
        profile = session.query(ModelProfile).one()
        quality = add_task(session, user, project, shot, profile)

    with database.session() as session:
        service = QueueService(session)
        task = service.claim_next("worker", now=now + timedelta(minutes=2))
        service.fail(task, error_class="quality", message="肢体畸变")
        assert task.id == quality.id
        assert task.status == "terminal_failed"


def test_queued_task_can_be_cancelled(database):
    with database.session() as session:
        user, project, shot, profile = seed(session)
        task = add_task(session, user, project, shot, profile)
        QueueService(session).cancel(task)
        assert task.status == "cancelled"


def test_stale_worker_token_cannot_overwrite_reclaimed_task(database):
    now = datetime.now(timezone.utc)
    with database.session() as session:
        user, project, shot, profile = seed(session)
        add_task(session, user, project, shot, profile)
    with database.session() as session:
        first = QueueService(session).claim_next("worker-a", now=now, lease_seconds=1)
        stale_token = first.lease_token
    with database.session() as session:
        QueueService(session).recover_expired(now=now + timedelta(seconds=2))
        second = QueueService(session).claim_next("worker-b", now=now + timedelta(seconds=2))
        assert second.lease_token != stale_token
        with pytest.raises(ValueError, match="租约"):
            QueueService(session).succeed(second, {}, lease_token=stale_token)
