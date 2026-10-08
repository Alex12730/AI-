from __future__ import annotations

from fastapi.testclient import TestClient

from video_workstation.config import Settings
from video_workstation.main import create_app
from video_workstation.models import AuditLog, User
from video_workstation.security import PasswordService


def make_empty_client(tmp_path, client_host: str = "127.0.0.1"):
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        session_secret="test-only-secret-that-is-long-enough",
        minimum_free_bytes=1,
    )
    app = create_app(settings)
    return TestClient(app, client=(client_host, 50000)), app


def csrf_from(response) -> str:
    return response.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def test_local_setup_creates_first_admin_and_signs_in(tmp_path):
    client, app = make_empty_client(tmp_path)

    setup_page = client.get("/setup")
    assert setup_page.status_code == 200
    assert "创建首个管理员" in setup_page.text
    token = csrf_from(setup_page)

    created = client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "a-new-local-password",
            "password_confirm": "a-new-local-password",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/"
    assert client.get("/").status_code == 200

    with app.state.database.session() as session:
        admin = session.query(User).filter_by(username="admin").one()
        assert admin.role == "admin"
        assert PasswordService().verify(admin.password_hash, "a-new-local-password")
        assert session.query(AuditLog).filter_by(action="user.bootstrap_admin", actor_id=admin.id).count() == 1


def test_setup_rejects_invalid_password_confirmation(tmp_path):
    client, app = make_empty_client(tmp_path)
    token = csrf_from(client.get("/setup"))

    mismatch = client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "a-new-local-password",
            "password_confirm": "different-local-password",
            "csrf_token": token,
        },
    )
    assert mismatch.status_code == 400
    assert "两次输入的密码不一致" in mismatch.text

    token = csrf_from(client.get("/setup"))
    too_short = client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "short",
            "password_confirm": "short",
            "csrf_token": token,
        },
    )
    assert too_short.status_code == 400
    assert "密码至少 12 个字符" in too_short.text

    with app.state.database.session() as session:
        assert session.query(User).count() == 0


def test_setup_is_disabled_after_admin_exists(tmp_path):
    client, _app = make_empty_client(tmp_path)
    token = csrf_from(client.get("/setup"))
    assert client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "a-new-local-password",
            "password_confirm": "a-new-local-password",
            "csrf_token": token,
        },
        follow_redirects=False,
    ).status_code == 303

    second_visit = client.get("/setup", follow_redirects=False)
    assert second_visit.status_code == 303
    assert second_visit.headers["location"] == "/login"


def test_setup_is_rejected_for_non_loopback_clients(tmp_path):
    client, app = make_empty_client(tmp_path, client_host="192.0.2.10")

    response = client.get("/setup")
    assert response.status_code == 403
    assert "仅允许在工作站本机初始化" in response.json()["detail"]

    with app.state.database.session() as session:
        assert session.query(User).count() == 0


def test_login_page_links_to_setup_only_before_admin_exists(tmp_path):
    client, app = make_empty_client(tmp_path)
    assert 'href="/setup"' in client.get("/login").text

    with app.state.database.session() as session:
        session.add(
            User(
                username="admin",
                password_hash=PasswordService().hash("a-new-local-password"),
                role="admin",
            )
        )

    assert 'href="/setup"' not in client.get("/login").text
