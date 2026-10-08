from __future__ import annotations

from fastapi.testclient import TestClient

from video_workstation.config import Settings
from video_workstation.main import create_app
from video_workstation.models import Task, User
from video_workstation.security import PasswordService


def make_client(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        session_secret="test-only-secret-that-is-long-enough",
        minimum_free_bytes=1,
    )
    app = create_app(settings)
    with app.state.database.session() as session:
        password_hash = PasswordService().hash("a-strong-local-password")
        session.add_all(
            [
                User(username="admin", password_hash=password_hash, role="admin"),
                User(username="member", password_hash=password_hash, role="member"),
                User(username="outsider", password_hash=password_hash, role="member"),
            ]
        )
    return TestClient(app), app


def login(client: TestClient, username: str):
    login_page = client.get("/login")
    token = login_page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    response = client.post(
        "/login",
        data={"username": username, "password": "a-strong-local-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    client.headers["X-CSRF-Token"] = response.headers["X-CSRF-Token"]


def test_anonymous_user_is_redirected_to_login(tmp_path):
    client, _app = make_client(tmp_path)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/health").json()["status"] == "ok"


def test_project_approval_and_demo_enqueue_flow(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")

    created = client.post(
        "/api/projects",
        json={"name": "新品营销片", "source_script": "真实界面录屏。环境空镜。人物展示产品。"},
    )
    assert created.status_code == 201
    project = created.json()
    assert len(project["shots"]) == 3

    submitted = client.post(f"/api/storyboards/{project['storyboard_id']}/submit")
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "pending_approval"
    assert client.post(f"/api/storyboards/{project['storyboard_id']}/approve").status_code == 403

    client.post("/logout")
    login(client, "admin")
    approved = client.post(f"/api/storyboards/{project['storyboard_id']}/approve")
    assert approved.status_code == 200

    client.post("/logout")
    login(client, "member")
    broll_shot = next(shot for shot in project["shots"] if shot["scene_type"] == "broll")
    enqueued = client.post(
        f"/api/shots/{broll_shot['id']}/enqueue",
        json={
            "model_slug": "demo",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "priority": 1,
            "estimated_temp_bytes": 1,
            "seed": 123,
        },
    )
    assert enqueued.status_code == 201
    assert enqueued.json()["status"] == "queued"

    with app.state.database.session() as session:
        assert session.query(User).filter_by(username="member").one().role == "member"


def test_unverified_h3_preset_and_project_escape_are_blocked(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "member")
    created = client.post(
        "/api/projects",
        json={"name": "人物短片", "source_script": "人物表演并说一句对白。"},
    ).json()
    client.post(f"/api/storyboards/{created['storyboard_id']}/submit")
    client.post("/logout")
    login(client, "admin")
    client.post(f"/api/storyboards/{created['storyboard_id']}/approve")
    client.post("/logout")
    login(client, "member")

    blocked = client.post(
        f"/api/shots/{created['shots'][0]['id']}/enqueue",
        json={
            "model_slug": "minimax-h3-fl2va",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "priority": 1,
            "estimated_temp_bytes": 1,
            "seed": 7,
        },
    )
    assert blocked.status_code == 409
    assert "未通过" in blocked.json()["detail"]

    client.post("/logout")
    login(client, "outsider")
    assert client.get(f"/api/projects/{created['id']}").status_code == 403


def test_admin_model_page_and_member_dashboard_permissions(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "member")
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "制作工作台" in dashboard.text
    assert client.get("/models").status_code == 403

    client.post("/logout")
    login(client, "admin")
    models = client.get("/models")
    assert models.status_code == 200
    assert "MiniMax H3" in models.text
    assert "未验证" in models.text


def test_cross_origin_or_missing_csrf_is_rejected(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "member")
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/projects", json={"name": "x", "source_script": "y"}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.post(
        "/api/projects",
        json={"name": "x", "source_script": "y"},
        headers={"Origin": "http://evil.test"},
    ).status_code == 403


def test_enqueue_uses_approved_shot_values_and_rejects_tampering(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    project = client.post(
        "/api/projects",
        json={"name": "20 秒竖屏片", "source_script": "环境空镜。"},
    ).json()
    shot_id = project["shots"][0]["id"]
    assert client.patch(
        f"/api/shots/{shot_id}",
        json={
            "title": "20 秒竖屏空镜",
            "prompt": "城市夜景",
            "duration_seconds": 20,
            "aspect_ratio": "9:16",
        },
    ).status_code == 200
    client.post(f"/api/storyboards/{project['storyboard_id']}/submit")
    client.post("/logout")
    login(client, "admin")
    client.post(f"/api/storyboards/{project['storyboard_id']}/approve")

    tampered = client.post(
        f"/api/shots/{shot_id}/enqueue",
        json={
            "model_slug": "demo",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "priority": 1,
            "estimated_temp_bytes": 1,
            "seed": 321,
        },
    )
    assert tampered.status_code == 409
    assert "已审批镜头" in tampered.json()["detail"]

    enqueued = client.post(
        f"/api/shots/{shot_id}/enqueue",
        json={
            "model_slug": "demo",
            "duration_seconds": 20,
            "aspect_ratio": "9:16",
            "priority": 1,
            "estimated_temp_bytes": 1,
            "seed": 321,
        },
    )
    assert enqueued.status_code == 201
    with app.state.database.session() as session:
        tasks = session.query(Task).filter_by(shot_id=shot_id).all()
        assert len(tasks) == 1
        assert tasks[0].payload_json["duration_seconds"] == 20
        assert tasks[0].payload_json["aspect_ratio"] == "9:16"
