from __future__ import annotations

from io import BytesIO

import pytest

from video_workstation.models import Asset, AuditLog, Project, User
from video_workstation.subtitles import MAX_SUBTITLE_BYTES, parse_srt, store_subtitle_asset
from test_api import login, make_client


VALID = """1\r
00:00:00,000 --> 00:00:01,200\r
第一句\r
\r
2\r
00:00:01,200 --> 00:00:02,500\r
第二句\r
"""


def test_parse_srt_normalizes_bom_crlf_and_rejects_invalid_timelines():
    cues = parse_srt("\ufeff" + VALID)
    assert len(cues) == 2
    assert cues[0].text == "第一句"
    assert cues[-1].end_ms == 2500
    invalid = [
        "",
        "x\n00:00:00,000 --> 00:00:01,000\ntext",
        "1\n00:00:02,000 --> 00:00:01,000\ntext",
        "1\n00:00:00,000 --> 00:00:02,000\na\n\n2\n00:00:01,000 --> 00:00:03,000\nb",
        "1\n00:99:00,000 --> 00:00:01,000\ntext",
        "1\n00:00:00,000 --> 00:00:01,000\n" + "x" * 501,
    ]
    for payload in invalid:
        with pytest.raises(ValueError):
            parse_srt(payload)


def test_store_subtitle_requires_admin_and_records_audit(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    project_data = client.post("/api/projects", json={"name": "字幕", "source_script": "环境空镜。"}).json()
    with app.state.database.session() as session:
        project = session.get(Project, project_data["id"])
        member = session.query(User).filter_by(username="member").one()
        with pytest.raises(Exception, match="管理员"):
            store_subtitle_asset(session, project, member, "captions.srt", VALID.encode(), app.state.settings)
        admin = session.query(User).filter_by(username="admin").one()
        asset = store_subtitle_asset(session, project, admin, "字幕.srt", ("\ufeff" + VALID).encode(), app.state.settings)
        assert asset.metadata_json["cue_count"] == 2
        assert asset.metadata_json["confirmed"] is True
        assert session.query(AuditLog).filter_by(action="asset.subtitle_upload", entity_id=asset.id).count() == 1


def test_subtitle_upload_route_enforces_role_extension_and_size(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    project = client.post("/api/projects", json={"name": "字幕上传", "source_script": "环境空镜。"}).json()
    csrf = client.headers["X-CSRF-Token"]
    denied = client.post(
        f"/projects/{project['id']}/subtitles",
        data={"csrf_token": csrf},
        files={"subtitle": ("captions.srt", VALID.encode("utf-8"), "application/x-subrip")},
    )
    assert denied.status_code == 403

    client.post("/logout")
    login(client, "admin")
    csrf = client.headers["X-CSRF-Token"]
    bad_extension = client.post(
        f"/projects/{project['id']}/subtitles",
        data={"csrf_token": csrf},
        files={"subtitle": ("captions.txt", VALID.encode("utf-8"), "text/plain")},
    )
    assert bad_extension.status_code == 409
    too_large = client.post(
        f"/projects/{project['id']}/subtitles",
        data={"csrf_token": csrf},
        files={"subtitle": ("large.srt", b"x" * (MAX_SUBTITLE_BYTES + 1), "application/x-subrip")},
    )
    assert too_large.status_code == 409
    uploaded = client.post(
        f"/projects/{project['id']}/subtitles",
        data={"csrf_token": csrf},
        files={"subtitle": ("captions.srt", VALID.encode("utf-8"), "application/x-subrip")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    with app.state.database.session() as session:
        assert session.query(Asset).filter_by(project_id=project["id"], kind="subtitle").count() == 1
