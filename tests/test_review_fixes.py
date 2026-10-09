from __future__ import annotations

from pathlib import Path

import pytest

from video_workstation.adapters.base import AdapterResult
from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import Asset, AuditLog, ModelProfile, Project, Shot, Storyboard, Task, User, WorkerStatus
from video_workstation.qc import QCResult
from video_workstation.services.postproduction import create_compose_task
from video_workstation.services.models import ModelNotAdmitted, model_config_fingerprint, validate_model_for_shot
from video_workstation.worker import Worker


def seeded_database(tmp_path):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}", minimum_free_bytes=1))
    db.create_schema()
    with db.session() as session:
        user = User(username="member", password_hash="hash", role="member")
        profile = ModelProfile(slug="real", display_name="Real", adapter_type="local_command", enabled=True, validated_presets_json=[{"duration_seconds": 5, "aspect_ratio": "16:9"}])
        session.add_all([user, profile]); session.flush()
        project = Project(name="p", created_by_id=user.id, status="approved")
        board = Storyboard(project=project, created_by_id=user.id, status="approved")
        shot = Shot(storyboard=board, sequence_no=1, title="s", prompt="p", scene_type="broll", duration_seconds=5, aspect_ratio="16:9", status="approved")
        session.add_all([project, board, shot]); session.flush()
        task = Task(project_id=project.id, shot_id=shot.id, model_profile_id=profile.id, created_by_id=user.id, status="queued", priority=1, payload_json={"prompt":"p", "output_path":str(tmp_path / "invalid.mp4"), "duration_seconds":5, "aspect_ratio":"16:9", "seed":1, "minimum_free_bytes":1}, estimated_temp_bytes=1, model_config_fingerprint=model_config_fingerprint(profile))
        session.add(task)
    return db, task.id


def test_worker_commits_claim_before_execution_and_invalid_media_fails_quality(tmp_path):
    db, task_id = seeded_database(tmp_path)
    class ProbeAdapter:
        def execute(self, request):
            with db.session() as other:
                observed = other.get(Task, task_id)
                assert observed.status == "running"
                assert observed.attempts == 1
                other.add(AuditLog(action="concurrent.write", entity_type="task", entity_id=task_id))
            request.output_path.write_bytes(b"not a video")
            return AdapterResult(success=True, output_path=request.output_path)
    with db.session() as session:
        assert Worker("worker", adapter_factory=lambda _p: ProbeAdapter(), lease_seconds=2, heartbeat_seconds=.1).run_once(session)
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task.status == "terminal_failed"
        assert task.error_class == "quality"
        assert session.query(AuditLog).filter_by(action="concurrent.write").count() == 1
        worker = session.query(WorkerStatus).filter_by(worker_id="worker").one()
        assert worker.state == "idle"
        assert worker.current_task_id is None


def test_product_ui_rejects_generative_profiles():
    shot = Shot(scene_type="product_ui", sequence_no=1, title="ui", prompt="ui", duration_seconds=5, aspect_ratio="16:9")
    h3 = ModelProfile(slug="minimax-h3-fl2va", display_name="H3", adapter_type="minimax_h3", enabled=True)
    with pytest.raises(ModelNotAdmitted, match="真实录屏"):
        validate_model_for_shot(shot, h3)


def test_worker_dispatches_compose_task_and_registers_composite(tmp_path, monkeypatch):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}", minimum_free_bytes=1))
    db.create_schema()
    source = db.settings.asset_dir / ("a" * 32) / "source.mp4"
    with db.session() as session:
        admin = User(username="admin", password_hash="hash", role="admin")
        video2x = ModelProfile(
            slug="video2x", display_name="Video2X", adapter_type="local_command", enabled=True,
            model_version="local-1", quantization="realesrgan",
            runtime_config_json={"command": ["video2x", "{input}", "{output}", "{scale}"]},
        )
        session.add_all([admin, video2x]); session.flush()
        video2x.validated_presets_json = [{"scale": 2, "config_fingerprint": model_config_fingerprint(video2x)}]
        project = Project(id="a" * 32, name="compose", created_by_id=admin.id)
        board = Storyboard(project=project, created_by_id=admin.id, status="approved")
        shot = Shot(storyboard=board, sequence_no=1, title="s", prompt="p", status="approved")
        session.add_all([project, board, shot]); session.flush()
        source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(b"source")
        asset = Asset(project_id=project.id, shot_id=shot.id, kind="video", path=str(source), sha256="x", metadata_json={"qc": {"passed": True, "duration_seconds": 1}})
        session.add(asset); session.flush()
        task = create_compose_task(session, admin, project, asset_ids=[asset.id], subtitle_asset_id=None, aspect_ratio="16:9", priority=1, settings=db.settings, upscale=True)
        task_id = task.id

    def fake_execute(request):
        request.output_path.write_bytes(b"composite")
        return AdapterResult(True, request.output_path, metadata={"expected_duration": 1})

    monkeypatch.setattr("video_workstation.worker.execute_compose", fake_execute)
    monkeypatch.setattr("video_workstation.worker.probe_media", lambda *_args, **_kwargs: QCResult(True, duration_seconds=1, width=1920, height=1080, has_audio=True, video_codec="h264", audio_codec="aac"))
    with db.session() as session:
        assert Worker("post-worker", settings=db.settings).run_once(session) is True
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task.status == "succeeded"
        composite = session.query(Asset).filter_by(task_id=task_id, kind="composite").one()
        assert Path(composite.path).read_bytes() == b"composite"
        followup = session.query(Task).filter_by(task_type="upscale", status="queued").one()
        assert followup.payload_json["source_asset_id"] == composite.id
