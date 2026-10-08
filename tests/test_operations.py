from __future__ import annotations

import json
from pathlib import Path

from video_workstation.models import Asset, ModelProfile, Project, Review, Task, User
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
        json={"title": "片头空镜", "prompt": "清晨城市航拍", "duration_seconds": 10, "aspect_ratio": "9:16"},
    )
    assert edited.status_code == 200
    assert edited.json()["prompt"] == "清晨城市航拍"
    assert edited.json()["duration_seconds"] == 10

    invalid = client.patch(
        f"/api/shots/{shot_id}",
        json={"title": "错误档位", "prompt": "不应保存", "duration_seconds": 6, "aspect_ratio": "9:16"},
    )
    assert invalid.status_code == 409
    unchanged = client.get(f"/api/projects/{project['id']}").json()["shots"][0]
    assert unchanged["title"] == "片头空镜"
    assert unchanged["duration_seconds"] == 10

    assert client.post(f"/api/storyboards/{project['storyboard_id']}/submit").status_code == 200
    locked = client.patch(
        f"/api/shots/{shot_id}",
        json={"title": "审批后修改", "prompt": "不应保存", "duration_seconds": 15, "aspect_ratio": "16:9"},
    )
    assert locked.status_code == 409

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


def test_project_page_edits_withdraws_and_matches_saved_shot_presets(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    project = client.post(
        "/api/projects",
        json={"name": "可选档位", "source_script": "环境空镜。"},
    ).json()
    shot_id = project["shots"][0]["id"]
    csrf_token = client.headers["X-CSRF-Token"]

    draft_page = client.get(f"/projects/{project['id']}")
    assert draft_page.status_code == 200
    assert 'name="duration_seconds"' in draft_page.text
    assert 'name="aspect_ratio"' in draft_page.text
    assert "保存镜头设置" in draft_page.text

    saved = client.post(
        f"/shots/{shot_id}/settings",
        data={"duration_seconds": "20", "aspect_ratio": "9:16", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/projects/{project['id']}"

    assert client.post(
        f"/storyboards/{project['storyboard_id']}/submit",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    ).status_code == 303
    pending_page = client.get(f"/projects/{project['id']}")
    assert "退回修改" in pending_page.text
    assert 'name="duration_seconds"' not in pending_page.text

    withdrawn = client.post(
        f"/storyboards/{project['storyboard_id']}/withdraw",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert withdrawn.status_code == 303
    assert 'name="duration_seconds"' in client.get(f"/projects/{project['id']}").text

    client.post(
        f"/storyboards/{project['storyboard_id']}/submit",
        data={"csrf_token": csrf_token},
    )
    client.post("/logout")
    login(client, "admin")
    client.post(f"/api/storyboards/{project['storyboard_id']}/approve")
    approved_page = client.get(f"/projects/{project['id']}")
    assert 'name="model_slug"' in approved_page.text
    assert f'name="csrf_token" value="{client.headers["X-CSRF-Token"]}"' in approved_page.text
    assert '<option value="demo">Demo 本地适配器（演示）</option>' in approved_page.text
    assert 'name="duration_seconds" value="20"' in approved_page.text
    assert 'name="aspect_ratio" value="9:16"' in approved_page.text

    with app.state.database.session() as session:
        session.query(ModelProfile).filter_by(slug="demo").one().enabled = False
    no_match_page = client.get(f"/projects/{project['id']}")
    assert "暂无已验证模型" in no_match_page.text
    assert 'name="model_slug"' not in no_match_page.text


def test_draft_page_bulk_updates_all_shots_then_allows_single_override(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "member")
    project = client.post(
        "/api/projects",
        json={"name": "批量镜头设置", "source_script": "环境空镜。人物表演。产品界面。"},
    ).json()
    storyboard_id = project["storyboard_id"]
    csrf_token = client.headers["X-CSRF-Token"]

    draft_page = client.get(f"/projects/{project['id']}")
    assert draft_page.status_code == 200
    assert "统一设置全部镜头" in draft_page.text
    assert f'action="/storyboards/{storyboard_id}/shot-settings"' in draft_page.text
    assert "应用后仍可单独修改某个镜头。" in draft_page.text

    bulk_saved = client.post(
        f"/storyboards/{storyboard_id}/shot-settings",
        data={"duration_seconds": "15", "aspect_ratio": "9:16", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert bulk_saved.status_code == 303
    assert bulk_saved.headers["location"] == f"/projects/{project['id']}"
    bulk_project = client.get(f"/api/projects/{project['id']}").json()
    assert [(shot["duration_seconds"], shot["aspect_ratio"]) for shot in bulk_project["shots"]] == [
        (15, "9:16"),
        (15, "9:16"),
        (15, "9:16"),
    ]

    first_shot_id = bulk_project["shots"][0]["id"]
    single_saved = client.post(
        f"/shots/{first_shot_id}/settings",
        data={"duration_seconds": "5", "aspect_ratio": "16:9", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert single_saved.status_code == 303
    overridden = client.get(f"/api/projects/{project['id']}").json()
    assert [(shot["duration_seconds"], shot["aspect_ratio"]) for shot in overridden["shots"]] == [
        (5, "16:9"),
        (15, "9:16"),
        (15, "9:16"),
    ]


def test_bulk_shot_settings_route_rejects_csrf_invalid_values_and_outsider(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "member")
    project = client.post(
        "/api/projects",
        json={"name": "批量设置权限", "source_script": "环境空镜。人物表演。"},
    ).json()
    route = f"/storyboards/{project['storyboard_id']}/shot-settings"
    member_csrf = client.headers["X-CSRF-Token"]

    invalid_csrf = client.post(
        route,
        data={"duration_seconds": "15", "aspect_ratio": "9:16", "csrf_token": "wrong-token"},
        headers={"X-CSRF-Token": ""},
    )
    assert invalid_csrf.status_code == 403

    invalid_values = client.post(
        route,
        data={"duration_seconds": "6", "aspect_ratio": "9:16", "csrf_token": member_csrf},
    )
    assert invalid_values.status_code == 409
    unchanged = client.get(f"/api/projects/{project['id']}").json()
    assert [(shot["duration_seconds"], shot["aspect_ratio"]) for shot in unchanged["shots"]] == [
        (5, "16:9"),
        (5, "16:9"),
    ]

    client.post("/logout")
    login(client, "outsider")
    outsider_csrf = client.headers["X-CSRF-Token"]
    forbidden = client.post(
        route,
        data={"duration_seconds": "15", "aspect_ratio": "9:16", "csrf_token": outsider_csrf},
    )
    assert forbidden.status_code == 403

    client.post("/logout")
    login(client, "member")
    still_unchanged = client.get(f"/api/projects/{project['id']}").json()
    assert [(shot["duration_seconds"], shot["aspect_ratio"]) for shot in still_unchanged["shots"]] == [
        (5, "16:9"),
        (5, "16:9"),
    ]


def test_bulk_shot_settings_form_is_hidden_after_submission(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "member")
    project = client.post(
        "/api/projects",
        json={"name": "审批锁定批量设置", "source_script": "环境空镜。人物表演。"},
    ).json()
    csrf_token = client.headers["X-CSRF-Token"]
    bulk_action = f'/storyboards/{project["storyboard_id"]}/shot-settings'

    assert bulk_action in client.get(f"/projects/{project['id']}").text
    submitted = client.post(
        f"/storyboards/{project['storyboard_id']}/submit",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert submitted.status_code == 303

    pending_page = client.get(f"/projects/{project['id']}")
    assert bulk_action not in pending_page.text
    assert "5 秒 · 16:9" in pending_page.text
