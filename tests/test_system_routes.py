from __future__ import annotations

from test_api import login, make_client


def test_system_routes_are_admin_only_and_render_status(tmp_path):
    client, _app = make_client(tmp_path)
    assert client.get("/system", follow_redirects=False).status_code == 303

    login(client, "member")
    assert client.get("/system").status_code == 403
    assert client.get("/api/system/status").status_code == 403

    client.post("/logout")
    login(client, "admin")
    response = client.get("/api/system/status")
    assert response.status_code == 200
    payload = response.json()
    assert {"gpu", "disks", "workers", "queue", "models", "alerts"} <= payload.keys()
    page = client.get("/system")
    assert page.status_code == 200
    assert "运行状态" in page.text
    assert "GPU" in page.text
    assert "Worker" in page.text


def test_models_page_distinguishes_enablement_from_readiness(tmp_path):
    client, _app = make_client(tmp_path)
    login(client, "admin")
    page = client.get("/models")
    assert page.status_code == 200
    assert "生产准备度" in page.text
    assert "演示" in page.text
    assert "已登记" in page.text


def test_sidebar_uses_cached_summary_without_hardware_probe(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "admin")
    page = client.get("/")
    assert "状态未检查" in page.text
    client.get("/api/system/status")
    assert app.state.system_status_summary["label"] in client.get("/").text
