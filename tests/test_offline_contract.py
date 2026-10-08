from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from video_workstation.config import Settings
from video_workstation.main import create_app
from video_workstation.models import ModelProfile, Task, User
from video_workstation.offline import OfflinePolicyError, validate_offline_profile
from video_workstation.security import PasswordService
from video_workstation.worker import Worker


def test_profile_policy_allows_loopback_but_rejects_cloud_endpoints_and_secrets():
    local = ModelProfile(
        slug="local",
        display_name="Local",
        adapter_type="local_command",
        provider="local",
        runtime_config_json={"endpoint": "http://127.0.0.1:8188", "command": ["python", "run.py"]},
    )
    validate_offline_profile(local)

    cloud = ModelProfile(
        slug="cloud",
        display_name="Cloud",
        adapter_type="local_command",
        provider="local",
        runtime_config_json={"endpoint": "https://api.example.com/generate"},
    )
    with pytest.raises(OfflinePolicyError, match="loopback"):
        validate_offline_profile(cloud)

    secret = ModelProfile(
        slug="secret",
        display_name="Secret",
        adapter_type="local_command",
        provider="local",
        runtime_config_json={"api_key": "must-not-be-here"},
    )
    with pytest.raises(OfflinePolicyError, match="敏感字段"):
        validate_offline_profile(secret)


def test_demo_production_flow_completes_with_outbound_socket_blocked(tmp_path, monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("运行期不得建立外部网络连接")

    monkeypatch.setattr(socket, "create_connection", blocked)
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        session_secret="offline-contract-secret-2026",
        minimum_free_bytes=1,
    )
    app = create_app(settings)
    with app.state.database.session() as session:
        password_hash = PasswordService().hash("offline-local-password")
        session.add_all(
            [
                User(username="admin", password_hash=password_hash, role="admin"),
                User(username="member", password_hash=password_hash, role="member"),
            ]
        )

    client = TestClient(app)
    client.post("/login", data={"username": "member", "password": "offline-local-password"})
    project = client.post("/api/projects", json={"name": "离线片", "source_script": "环境空镜。"}).json()
    client.post(f"/api/storyboards/{project['storyboard_id']}/submit")
    client.post("/logout")
    client.post("/login", data={"username": "admin", "password": "offline-local-password"})
    client.post(f"/api/storyboards/{project['storyboard_id']}/approve")
    task_response = client.post(
        f"/api/shots/{project['shots'][0]['id']}/enqueue",
        json={
            "model_slug": "demo",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "priority": 1,
            "estimated_temp_bytes": 1,
            "seed": 1,
        },
    )
    assert task_response.status_code == 201

    with app.state.database.session() as session:
        assert Worker("offline-test-worker").run_once(session) is True

    with app.state.database.session() as session:
        task = session.get(Task, task_response.json()["id"])
        assert task.status == "succeeded"
        assert task.result_json["demo"] is True
