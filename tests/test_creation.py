from types import SimpleNamespace

import pytest
from sqlalchemy import select

from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import Asset, CreationPlan, ModelProfile, Task, User
from video_workstation.queue import QueueService
from video_workstation.services.creation import (
    RECIPES, advance_creations, create_guided_project, recommend_shot,
    start_creation, set_creation_paused,
)
from video_workstation.services.models import model_config_fingerprint, seed_model_profiles
from video_workstation.services.projects import PermissionDenied


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, minimum_free_bytes=1)
    db = Database(settings)
    db.create_schema()
    with db.session() as session:
        user = User(username="owner", password_hash="unused", role="member")
        other = User(username="other", password_hash="unused", role="member")
        session.add_all([user, other])
        seed_model_profiles(session)
    return db, settings, user.id, other.id


def qualified(session, slug):
    profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == slug))
    profile.enabled = True
    profile.model_version = "local-test"
    profile.quantization = "test"
    profile.runtime_config_json = {"command": ["python", "local.py", "{output_path}"]}
    profile.validated_presets_json = [{"duration_seconds": 5, "aspect_ratio": "16:9",
        "config_fingerprint": model_config_fingerprint(profile)}]
    return profile


def new_plan(session, owner_id, recipe="story", seconds=10):
    return create_guided_project(session, session.get(User, owner_id), name="测试作品",
        script="森林里的晨雾。阳光穿过树梢。", recipe=recipe,
        target_seconds=seconds, aspect_ratio="16:9")


def test_six_workflows_and_target_duration(setup):
    db, _, owner, _ = setup
    assert set(RECIPES) == {"product", "knowledge", "talking", "acting", "story", "advert"}
    with db.session() as session:
        for recipe in RECIPES:
            plan = new_plan(session, owner, recipe, 30)
            shots = plan.storyboard.shots
            assert len(shots) == 6
            assert sum(shot.duration_seconds for shot in shots) == 30
        with pytest.raises(ValueError, match="5 秒"):
            new_plan(session, owner, seconds=31)


def test_router_never_uses_demo_and_explains_missing_validation(setup):
    db, _, owner, _ = setup
    with db.session() as session:
        plan = new_plan(session, owner)
        profiles = list(session.scalars(select(ModelProfile)))
        decision = recommend_shot(plan.storyboard.shots[0], profiles)
        assert decision["model_slug"] is None
        assert "验证" in decision["reason"]
        qualified(session, "wan-2.2")
        assert recommend_shot(plan.storyboard.shots[0], profiles)["model_slug"] == "wan-2.2"
        plan.storyboard.shots[0].scene_type = "product_ui"
        assert recommend_shot(plan.storyboard.shots[0], profiles)["source"] == "material"
        plan.storyboard.shots[0].scene_type = "character"
        assert recommend_shot(plan.storyboard.shots[0], profiles)["model_slug"] is None
        qualified(session, "ltx-2.3")
        assert recommend_shot(plan.storyboard.shots[0], profiles)["model_slug"] == "ltx-2.3"


def test_confirmation_atomic_owner_only_and_idempotent(setup):
    db, settings, owner, outsider = setup
    with db.session() as session:
        plan = new_plan(session, owner)
        with pytest.raises(PermissionDenied):
            start_creation(session, session.get(User, outsider), plan, settings)
        with pytest.raises(ValueError, match="验证"):
            start_creation(session, session.get(User, owner), plan, settings)
        assert plan.storyboard.status == "draft"
        assert session.scalar(select(Task.id)) is None
        qualified(session, "wan-2.2")
        start_creation(session, session.get(User, owner), plan, settings)
        first = plan.snapshot_json
        start_creation(session, session.get(User, owner), plan, settings)
        assert plan.snapshot_json == first
        assert plan.storyboard.approved_by_id == owner
        assert plan.storyboard.status == "approved"
        assert len(list(session.scalars(select(Task)))) == 1


def finish_video(session, task, settings):
    path = settings.asset_dir / f"{task.id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    from video_workstation.services.assets import sha256_file
    asset = Asset(project_id=task.project_id, shot_id=task.shot_id, task_id=task.id,
        kind="video", path=str(path), sha256=sha256_file(path),
        metadata_json={"qc": {"passed": True, "duration_seconds": 5}})
    session.add(asset)
    task.status = "succeeded"
    session.flush()
    return asset


def test_marketing_preempts_after_current_shot_and_resumes_normal_after_failure(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        actor = session.get(User, owner)
        normal = new_plan(session, owner)
        start_creation(session, actor, normal, settings)
        normal_first = session.scalar(select(Task).where(Task.project_id == normal.project_id))
        normal_first.status = "running"
        first_marketing = new_plan(session, owner)
        start_creation(session, actor, first_marketing, settings, marketing_priority=True)
        second_marketing = new_plan(session, owner)
        start_creation(session, actor, second_marketing, settings, marketing_priority=True)
        assert session.scalar(select(Task.id).where(Task.project_id == second_marketing.project_id)) is None
        first_marketing_task = session.scalar(select(Task).where(Task.project_id == first_marketing.project_id))
        assert first_marketing_task.priority == -1 and normal_first.priority == 1
        advance_creations(session, settings)
        assert normal.snapshot_json["preempted_by"] == first_marketing.id
        assert normal_first.status == "running"
        finish_video(session, normal_first, settings)
        advance_creations(session, settings)
        assert len(list(session.scalars(select(Task).where(Task.project_id == normal.project_id)))) == 1
        finish_video(session, first_marketing_task, settings)
        advance_creations(session, settings)
        marketing_second_shot = session.scalar(select(Task).where(Task.project_id == first_marketing.project_id,
                                                                   Task.id != first_marketing_task.id))
        assert marketing_second_shot.priority == -1
        finish_video(session, marketing_second_shot, settings)
        advance_creations(session, settings)
        compose = session.scalar(select(Task).where(Task.project_id == first_marketing.project_id,
                                                    Task.task_type == "compose_project"))
        assert compose.priority == -1
        compose.status = "succeeded"
        session.add(Asset(project_id=compose.project_id, task_id=compose.id, kind="composite",
                          path="not-read-in-this-state-test", metadata_json={"qc": {"passed": True}}))
        session.flush()
        advance_creations(session, settings)
        assert first_marketing.status == "preview_ready"
        second_task = session.scalar(select(Task).where(Task.project_id == second_marketing.project_id))
        assert second_task is not None and second_task.priority == -1
        assert normal.snapshot_json["preempted_by"] == second_marketing.id
        second_task.status = "terminal_failed"
        advance_creations(session, settings)
        assert second_marketing.status == "failed"
        assert "preempted_by" not in normal.snapshot_json
        assert len(list(session.scalars(select(Task).where(Task.project_id == normal.project_id)))) == 2


def test_normal_videos_finish_in_submission_order(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        actor = session.get(User, owner)
        first = new_plan(session, owner)
        start_creation(session, actor, first, settings)
        second = new_plan(session, owner)
        start_creation(session, actor, second, settings)
        assert session.scalar(select(Task.id).where(Task.project_id == second.project_id)) is None
        first_task = session.scalar(select(Task).where(Task.project_id == first.project_id))
        finish_video(session, first_task, settings)
        advance_creations(session, settings)
        assert session.scalar(select(Task.id).where(Task.project_id == second.project_id)) is None
        first_second_task = session.scalar(select(Task).where(Task.project_id == first.project_id,
                                                               Task.id != first_task.id))
        assert first_second_task is not None
        finish_video(session, first_second_task, settings)
        advance_creations(session, settings)
        compose = session.scalar(select(Task).where(Task.project_id == first.project_id,
                                                    Task.task_type == "compose_project"))
        assert compose is not None
        assert session.scalar(select(Task.id).where(Task.project_id == second.project_id)) is None
        compose.status = "terminal_failed"
        advance_creations(session, settings)
        assert first.status == "failed"
        assert session.scalar(select(Task.id).where(Task.project_id == second.project_id)) is not None


def test_pausing_queued_shot_prevents_claim_until_user_resumes(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        actor = session.get(User, owner)
        plan = new_plan(session, owner)
        start_creation(session, actor, plan, settings, marketing_priority=True)
        task = session.scalar(select(Task).where(Task.project_id == plan.project_id))
        set_creation_paused(session, actor, plan, True)
        assert plan.status == "paused" and task.status == "paused"
        assert QueueService(session).claim_next("worker") is None
        set_creation_paused(session, actor, plan, False)
        assert plan.status == "running" and task.status == "queued"
        assert QueueService(session).claim_next("worker").id == task.id


@pytest.mark.parametrize("marketing", [False, True])
def test_confirmation_order_wins_over_draft_creation_order(setup, marketing):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        actor = session.get(User, owner)
        older_draft = new_plan(session, owner)
        newer_draft = new_plan(session, owner)
        start_creation(session, actor, newer_draft, settings, marketing_priority=marketing)
        start_creation(session, actor, older_draft, settings, marketing_priority=marketing)
        assert session.scalar(select(Task.id).where(Task.project_id == newer_draft.project_id)) is not None
        assert session.scalar(select(Task.id).where(Task.project_id == older_draft.project_id)) is None
        advance_creations(session, settings)
        assert session.scalar(select(Task.id).where(Task.project_id == older_draft.project_id)) is None


def test_scheduler_does_not_override_manual_task_pause(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        actor = session.get(User, owner)
        plan = new_plan(session, owner)
        start_creation(session, actor, plan, settings)
        task = session.scalar(select(Task).where(Task.project_id == plan.project_id))
        QueueService(session).pause(task)
        advance_creations(session, settings)
        assert task.status == "paused"
        assert QueueService(session).claim_next("worker") is None
        QueueService(session).resume(task)
        advance_creations(session, settings)
        assert task.status == "queued"


@pytest.mark.parametrize("marketing", [False, True])
def test_manually_paused_shot_yields_queue_and_plan_resume_restores_it(setup, marketing):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        actor = session.get(User, owner)
        first = new_plan(session, owner)
        start_creation(session, actor, first, settings, marketing_priority=marketing)
        second = new_plan(session, owner)
        start_creation(session, actor, second, settings, marketing_priority=marketing)
        first_task = session.scalar(select(Task).where(Task.project_id == first.project_id))
        QueueService(session).pause(first_task)
        advance_creations(session, settings)
        assert first.status == "paused" and first.snapshot_json["manual_task_pause_id"] == first_task.id
        assert session.scalar(select(Task.id).where(Task.project_id == second.project_id)) is not None
        set_creation_paused(session, actor, first, False)
        assert first.status == "running" and first_task.status == "queued"
        assert "manual_task_pause_id" not in first.snapshot_json


def test_sequential_generation_pause_restart_and_explicit_composition(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        plan = new_plan(session, owner)
        start_creation(session, session.get(User, owner), plan, settings)
        plan_id = plan.id
        first = session.scalar(select(Task))
        set_creation_paused(session, session.get(User, owner), plan, True)
        a1 = finish_video(session, first, settings)
        advance_creations(session, settings)
        assert plan.status == "paused"
        assert len(list(session.scalars(select(Task)))) == 1
        set_creation_paused(session, session.get(User, owner), plan, False)
        advance_creations(session, settings)
        second = session.scalar(select(Task).where(Task.id != first.id))
        a2 = finish_video(session, second, settings)
        asset_ids = [a1.id, a2.id]
    # New session imitates a restarted Worker. No duplicate generation or composition.
    with db.session() as session:
        advance_creations(session, settings)
        advance_creations(session, settings)
        tasks = list(session.scalars(select(Task)))
        assert len(tasks) == 3
        compose = next(t for t in tasks if t.task_type == "compose_project")
        assert compose.payload_json["asset_ids"] == asset_ids
        assert compose.payload_json["expected_duration"] == 10
        compose.status = "succeeded"
        session.add(Asset(project_id=compose.project_id, task_id=compose.id,
            kind="composite", path="not-read-in-this-state-test", metadata_json={"qc": {"passed": True}}))
        session.flush()
        advance_creations(session, settings)
        assert session.get(CreationPlan, plan_id).status == "preview_ready"


def test_failure_does_not_start_next_shot(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        plan = new_plan(session, owner)
        start_creation(session, session.get(User, owner), plan, settings)
        task = session.scalar(select(Task))
        task.status = "terminal_failed"
        task.error_message = "显存不足"
        advance_creations(session, settings)
        assert plan.status == "failed"
        assert "显存不足" in plan.error_message
        assert len(list(session.scalars(select(Task)))) == 1


def test_missing_or_changed_material_blocks_without_crashing_scheduler(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        plan = new_plan(session, owner)
        start_creation(session, session.get(User, owner), plan, settings)
        task = session.scalar(select(Task))
        asset = finish_video(session, task, settings)
        from pathlib import Path
        Path(asset.path).write_bytes(b"changed")
        advance_creations(session, settings)
        assert plan.status == "blocked"
        assert "改变" in plan.error_message
        assert len(list(session.scalars(select(Task)))) == 1


def test_unavailable_disk_blocks_scheduler_instead_of_crashing(setup, monkeypatch):
    from video_workstation.storage import InsufficientStorage, StorageGuard
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        plan = new_plan(session, owner)
        start_creation(session, session.get(User, owner), plan, settings)
        finish_video(session, session.scalar(select(Task)), settings)
        def no_space(*args, **kwargs):
            raise InsufficientStorage("磁盘空间不足")
        monkeypatch.setattr(StorageGuard, "ensure_capacity", no_space)
        advance_creations(session, settings)
        assert plan.status == "blocked"
        assert "磁盘" in plan.error_message


def test_corrupt_snapshot_blocks_one_plan_without_stopping_others(setup):
    db, settings, owner, _ = setup
    with db.session() as session:
        qualified(session, "wan-2.2")
        corrupt = new_plan(session, owner)
        start_creation(session, session.get(User, owner), corrupt, settings)
        corrupt.snapshot_json = {"bindings": {}}
        healthy = new_plan(session, owner)
        start_creation(session, session.get(User, owner), healthy, settings)
        advance_creations(session, settings)
        assert corrupt.status == "blocked"
        finish_video(session, session.get(Task, healthy.snapshot_json["nodes"][0]["task_id"]), settings)
        advance_creations(session, settings)
        assert healthy.status == "running"
        assert healthy.snapshot_json["nodes"][1]["task_id"]
