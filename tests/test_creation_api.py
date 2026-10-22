import re

from sqlalchemy import select

from test_api import make_client, login
from video_workstation.models import CreationPlan, Project, Task, User


def token(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def create(client, *, recipe="story", script="清晨的森林"):
    page = client.get("/create")
    return client.post("/create", data={"csrf_token": token(page), "name": "新手作品",
        "script": script, "recipe": recipe, "target_seconds": 10, "aspect_ratio": "16:9"}, follow_redirects=False)


def test_guided_pages_and_honest_production_gate(tmp_path):
    client, app = make_client(tmp_path)
    assert client.get("/create", follow_redirects=False).status_code == 303
    login(client, "member")
    page = client.get("/create")
    assert page.status_code == 200
    assert "故事短片" in page.text and "模板" in page.text
    created = create(client)
    assert created.status_code == 303
    url = created.headers["location"]
    detail = client.get(url)
    assert "确认分镜" in detail.text and "验证" in detail.text
    assert 'AI 生成 · 等待模型验证' in detail.text
    assert '可选：使用已有视频替代 AI 生成' in detail.text
    assert '<strong>需要真实素材</strong>' not in detail.text
    blocked = client.post(url + "/start", data={"csrf_token": token(detail)})
    assert blocked.status_code == 409
    assert "验证" in blocked.text
    with app.state.database.session() as session:
        assert session.scalar(select(Task.id)) is None
        assert session.scalar(select(CreationPlan)).storyboard.status == "draft"


def test_login_lands_on_beginner_creation(tmp_path):
    client, _ = make_client(tmp_path)
    page = client.get("/login")
    response = client.post("/login", data={"csrf_token": token(page), "username": "member",
        "password": "a-strong-local-password"}, follow_redirects=False)
    assert response.headers["location"] == "/create"


def test_guided_creation_and_confirmation_require_csrf_and_owner(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    assert client.post("/create", data={"name": "x", "script": "y", "recipe": "story",
        "target_seconds": 10, "aspect_ratio": "16:9", "csrf_token": "wrong"}).status_code == 403
    url = create(client).headers["location"]
    client.post("/logout")
    login(client, "outsider")
    assert client.get(url).status_code == 403
    assert client.post(url + "/start", data={"csrf_token": token(client.get("/create"))}).status_code == 403
    assert client.post(url + "/pause", data={"csrf_token": token(client.get("/create"))}).status_code == 403


def test_real_tutorial_requests_material_and_escapes_script(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    url = create(client, recipe="product", script="<script>alert(1)</script>演示软件按钮").headers["location"]
    page = client.get(url)
    assert "真实视频" in page.text
    assert "&lt;script&gt;" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert client.post(url + "/start", data={"csrf_token": token(page)}).status_code == 409


def test_validation_error_preserves_form_and_no_partial_project(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    page = client.get("/create")
    response = client.post("/create", data={"csrf_token": token(page), "name": "保留此名字",
        "script": "森林", "recipe": "story", "target_seconds": 31, "aspect_ratio": "16:9"})
    assert response.status_code == 422
    assert "保留此名字" in response.text
    assert "5 秒" in response.text
    with app.state.database.session() as session:
        assert session.scalar(select(Project.id)) is None


def test_edit_shot_is_available_before_confirmation(tmp_path):
    client, app = make_client(tmp_path)
    login(client, "member")
    url = create(client).headers["location"]
    with app.state.database.session() as session:
        plan = session.scalar(select(CreationPlan))
        shot_id = plan.storyboard.shots[0].id
    page = client.get(url)
    response = client.post(f"{url}/shots/{shot_id}/edit", data={"csrf_token": token(page),
        "title": "我的标题", "prompt": "安静的湖面"}, follow_redirects=False)
    assert response.status_code == 303
    assert "安静的湖面" in client.get(url).text


def test_real_video_to_finished_delivery_with_existing_worker(tmp_path):
    import io
    import shutil
    import subprocess
    import pytest
    from video_workstation.worker import Worker
    from video_workstation.qc import probe_media
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg runtime unavailable")
    source = tmp_path / "真实录屏.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=blue:s=320x180:r=24:d=5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        check=True, capture_output=True, timeout=60)
    client, app = make_client(tmp_path)
    login(client, "member")
    url = create(client, recipe="product", script="展示真实软件").headers["location"]
    page = client.get(url)
    with app.state.database.session() as session:
        plan = session.scalar(select(CreationPlan))
        shots = [s.id for s in plan.storyboard.shots]
        plan_id = plan.id
    for shot_id in shots:
        uploaded = client.post(f"{url}/shots/{shot_id}/video", data={"csrf_token": token(page)},
            files={"video": ("真实录屏.mp4", io.BytesIO(source.read_bytes()), "video/mp4")}, follow_redirects=False)
        assert uploaded.status_code == 303
    confirmed = client.post(url + "/start", data={"csrf_token": token(page)}, follow_redirects=False)
    assert confirmed.status_code == 303
    with app.state.database.session() as session:
        worker = Worker("test-real-compose", settings=app.state.settings)
        assert worker.run_once(session) is True
        worker.run_once(session)  # Reconcile completion to preview_ready.
        plan = session.get(CreationPlan, plan_id)
        assert plan.status == "preview_ready"
        tasks = list(session.scalars(select(Task)))
        assert len(tasks) == 1 and tasks[0].task_type == "compose_project"
        qc = probe_media(tasks[0].result_json["output_path"], expected_duration=10)
        assert qc.passed
        assert (qc.width, qc.height) == (1920, 1080)
    preview = client.get(url)
    assert "保存视频" in preview.text
    # The personal confirmation does not grant member the legacy admin compose permission.
    with app.state.database.session() as session:
        project_id = session.get(CreationPlan, plan_id).project_id
    assert client.post(f"/api/projects/{project_id}/compose", json={"asset_ids": ["x"], "aspect_ratio": "16:9"}).status_code == 403


def test_draft_binding_cannot_overwrite_confirmed_snapshot(tmp_path):
    from video_workstation.creation_web import lock_creation
    from video_workstation.services.creation import start_creation
    from test_creation import qualified
    client, app = make_client(tmp_path)
    login(client, "member")
    create(client)
    with app.state.database.session() as stale:
        plan = stale.scalar(select(CreationPlan))
        plan_id = plan.id
        with app.state.database.session() as fresh:
            qualified(fresh, "wan-2.2")
            current = fresh.get(CreationPlan, plan_id)
            start_creation(fresh, fresh.get(User, current.created_by_id), current, app.state.settings)
        lock_creation(stale, plan)
        assert plan.status == "running"
        assert len(plan.snapshot_json["nodes"]) == 2
