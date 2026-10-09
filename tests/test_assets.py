from __future__ import annotations

from pathlib import Path
from io import BytesIO

import pytest

from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import Asset, Project, Shot, Storyboard, Task, User
from video_workstation.qc import QCResult
from video_workstation.services.assets import register_video_asset, sha256_file, store_uploaded_video
from video_workstation.media import InvalidRange, parse_range_header, resolve_asset_path
from test_api import login, make_client


def seeded(tmp_path):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}", minimum_free_bytes=1))
    db.create_schema()
    with db.session() as session:
        user = User(username="member", password_hash="hash", role="member")
        session.add(user); session.flush()
        project = Project(name="p", created_by_id=user.id)
        board = Storyboard(project=project, created_by_id=user.id, status="approved")
        shot = Shot(storyboard=board, sequence_no=1, title="s", prompt="p", status="approved")
        session.add_all([project, board, shot]); session.flush()
        task = Task(project_id=project.id, shot_id=shot.id, created_by_id=user.id, status="running")
        session.add(task); session.flush()
        ids = task.id, project.id, shot.id
    return db, ids


def test_register_video_asset_is_hashed_and_idempotent(tmp_path):
    db, (task_id, project_id, shot_id) = seeded(tmp_path)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video-bytes")
    qc = QCResult(True, duration_seconds=5, width=1920, height=1080, has_audio=True, video_codec="h264", audio_codec="aac")
    with db.session() as session:
        task = session.get(Task, task_id)
        first = register_video_asset(session, task, path, qc)
        second = register_video_asset(session, task, path, qc)
        assert first.id == second.id
        assert first.project_id == project_id
        assert first.shot_id == shot_id
        assert first.sha256 == sha256_file(path)
        assert first.metadata_json["qc"]["width"] == 1920
        assert session.query(Asset).filter_by(task_id=task_id, kind="video").count() == 1


def test_register_video_asset_requires_real_passed_media(tmp_path):
    db, (task_id, _project_id, _shot_id) = seeded(tmp_path)
    with db.session() as session:
        task = session.get(Task, task_id)
        with pytest.raises(ValueError, match="不存在"):
            register_video_asset(session, task, tmp_path / "missing.mp4", QCResult(True))
        path = tmp_path / "clip.mp4"; path.write_bytes(b"x")
        with pytest.raises(ValueError, match="质检"):
            register_video_asset(session, task, path, QCResult(False, errors=["bad"]))


def test_uploaded_screen_recording_is_bounded_qced_and_managed(tmp_path, monkeypatch):
    db, (_task_id, _project_id, shot_id) = seeded(tmp_path)
    settings = db.settings
    settings.maximum_video_upload_bytes = 16
    monkeypatch.setattr(
        "video_workstation.services.assets.probe_media",
        lambda *_args, **_kwargs: QCResult(True, duration_seconds=5, width=1920, height=1080, has_audio=False, video_codec="h264"),
    )
    with db.session() as session:
        shot = session.get(Shot, shot_id)
        actor = session.get(User, shot.storyboard.project.created_by_id)
        asset = store_uploaded_video(session, actor, shot, "录屏.mp4", BytesIO(b"screen-recording"), settings)
        assert Path(asset.path).is_file()
        assert Path(asset.path).is_relative_to(settings.asset_dir)
        assert asset.metadata_json["source"] == "upload"
        assert asset.metadata_json["qc"]["has_audio"] is False


def test_uploaded_screen_recording_rejects_extension_and_oversize(tmp_path):
    db, (_task_id, _project_id, shot_id) = seeded(tmp_path)
    settings = db.settings
    settings.maximum_video_upload_bytes = 4
    with db.session() as session:
        shot = session.get(Shot, shot_id)
        actor = session.get(User, shot.storyboard.project.created_by_id)
        with pytest.raises(ValueError, match="格式"):
            store_uploaded_video(session, actor, shot, "bad.exe", BytesIO(b"x"), settings)
        with pytest.raises(ValueError, match="过大"):
            store_uploaded_video(session, actor, shot, "big.mp4", BytesIO(b"12345"), settings)
    assert not list(settings.asset_dir.rglob("*.tmp"))


def test_range_parser_supports_prefix_open_and_suffix_ranges():
    assert parse_range_header(None, 100) is None
    assert parse_range_header("bytes=0-9", 100).length == 10
    assert parse_range_header("bytes=90-", 100).end == 99
    assert parse_range_header("bytes=-10", 100).start == 90
    for value in ("bytes=100-110", "bytes=5-4", "bytes=0-1,3-4", "items=0-1"):
        with pytest.raises(InvalidRange):
            parse_range_header(value, 100)


def test_asset_path_must_stay_in_managed_roots(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", database_url=f"sqlite:///{tmp_path / 'app.db'}")
    settings.ensure_directories()
    inside = settings.asset_dir / "p" / "clip.mp4"; inside.parent.mkdir(); inside.write_bytes(b"x")
    asset = Asset(project_id="p", kind="video", path=str(inside))
    assert resolve_asset_path(asset, settings) == inside.resolve()
    outside = tmp_path / "outside.mp4"; outside.write_bytes(b"x")
    asset.path = str(outside)
    with pytest.raises(ValueError, match="越界"):
        resolve_asset_path(asset, settings)


def test_authorized_asset_streaming_and_range(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    project = client.post("/api/projects", json={"name": "预览", "source_script": "环境空镜。"}).json()
    media = app.state.settings.asset_dir / project["id"] / "preview.mp4"
    media.parent.mkdir(parents=True, exist_ok=True); media.write_bytes(b"0123456789")
    with app.state.database.session() as session:
        asset = Asset(project_id=project["id"], shot_id=project["shots"][0]["id"], kind="video", path=str(media), sha256="x")
        session.add(asset); session.flush(); asset_id = asset.id

    partial = client.get(f"/assets/{asset_id}/content", headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"
    assert client.get(f"/assets/{asset_id}/content", headers={"Range": "bytes=20-"}).status_code == 416

    client.post("/logout"); login(client, "outsider")
    assert client.get(f"/assets/{asset_id}/content").status_code == 403
