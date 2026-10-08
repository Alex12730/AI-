from __future__ import annotations

import json
from pathlib import Path

from video_workstation.models import Asset, Project, Review, Task, User
from video_workstation.worker import Worker

from test_api import login, make_client


def _approved_demo_task(client, app):
    login(client, "member")
    project = client.post(
        "/api/projects",
        json={"name": "管理闭环", "source_script": "环境空镜。"},
    ).json()
    client.post(f"/api/storyboards/{project['storyboard_id']}/submit")
    client.post("/logout")
    login(client, "admin")
    client.post(f"/api/storyboards/{project['storyboard_id']}/approve")
    task = client.post(
        f"/api/shots/{project['shots'][0]['id']}/enqueue",
        json={
            "model_slug": "demo",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "priority": 1,
            "estimated_temp_bytes": 999999999999,
            "seed": 5,
        },
    ).json()
    return project, task


def test_draft_shot_edit_member_creation_and_project_assignment(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "member")
    project = client.post(
        "/api/projects",
        json={"name": "协作项目", "source_script": "环境空镜。"},
    ).json()
    shot_id = project["shots"][0]["id"]
    edited = client.patch(
        f"/api/shots/{shot_id}",
        json={"title": "片头空镜", "prompt": "清晨城市航拍", "duration_seconds": 6, "aspect_ratio": "9:16"},
    )
    assert edited.status_code == 200
    assert edited.json()["prompt"] == "清晨城市航拍"

    client.post("/logout")
    login(client, "admin")
    created = client.post(
        "/api/admin/users",
        json={"username": "collaborator", "password": "a-strong-local-password"},
    )
    assert created.status_code == 201
    assert client.post(
        f"/api/projects/{project['id']}/members",
        json={"username": "collaborator"},
    ).status_code == 200

    client.post("/logout")
    login(client, "collaborator")
    assert client.get(f"/api/projects/{project['id']}").status_code == 200


def test_admin_can_control_task_and_model_state(tmp_path):
    client, _app = make_client(tmp_path)
    project, task = _approved_demo_task(client, _app)

    assert client.patch(f"/api/tasks/{task['id']}/priority", json={"priority": 0}).json()["priority"] == 0
    assert client.post(f"/api/tasks/{task['id']}/pause").json()["status"] == "paused"
    assert client.post(f"/api/tasks/{task['id']}/resume").json()["status"] == "queued"
    assert client.post(f"/api/tasks/{task['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/tasks/{task['id']}/retry").json()["status"] == "queued"

    toggled = client.patch("/api/models/demo", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is False

    client.post("/logout")
    login(client, "member")
    assert client.patch("/api/models/demo", json={"enabled": True}).status_code == 403
    assert client.get(f"/api/projects/{project['id']}").status_code == 200


def test_acceptance_creates_review_and_traceable_archive_manifest(tmp_path):
    client, app = make_client(tmp_path)
    project, task = _approved_demo_task(client, app)
    with app.state.database.session() as session:
        assert Worker("acceptance-worker").run_once(session) is True

    accepted = client.post(
        f"/api/projects/{project['id']}/accept",
        json={"notes": "样片通过"},
    )
    assert accepted.status_code == 200
    manifest_path = accepted.json()["manifest_path"]
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert manifest["project"]["id"] == project["id"]
    assert manifest["tasks"][0]["model_version"] == "unconfigured"
    assert manifest["tasks"][0]["seed"] == 5

    with app.state.database.session() as session:
        assert session.get(Project, project["id"]).status == "accepted"
        assert session.query(Review).filter_by(project_id=project["id"], status="accepted").count() == 1
        assert session.query(Asset).filter_by(project_id=project["id"], kind="archive_manifest").count() == 1
        assert session.get(Task, task["id"]).status == "succeeded"
