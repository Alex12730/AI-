from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from video_workstation.config import Settings
from video_workstation.cli import stop_idle_worker
from video_workstation.db import Database
from video_workstation.models import ModelProfile, Project, Task, User
from video_workstation.queue import QueueService
from video_workstation.worker import Worker
from video_workstation.services.system_status import GPUStatus, ProbeResult
from video_workstation.thermal import ThermalState, acquire_monitor_lock, load_thermal_state, read_thermal_status, thermal_dispatch_allowed


BASE = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)


def reading(degrees: int | None):
    if degrees is None:
        return ProbeResult(status="unavailable", data=[], error="sensor unavailable")
    return ProbeResult(status="ok", data=[GPUStatus("RTX A6000", "597.16", 90, 1000, 49140, degrees)])


def test_sustained_thresholds_and_single_emergency_sample():
    state = ThermalState()
    for seconds in range(0, 31, 5):
        state.sample(reading(80), BASE + timedelta(seconds=seconds))
        if seconds < 30:
            assert state.level == "normal"
    assert state.level == "warning"
    for seconds in range(35, 66, 5):
        state.sample(reading(85), BASE + timedelta(seconds=seconds))
    assert state.level == "cooldown" and state.locked
    state.sample(reading(90), BASE + timedelta(seconds=70))
    state.sample(reading(90), BASE + timedelta(seconds=80))
    assert state.level == "emergency" and state.locked
    other = ThermalState()
    other.sample(reading(93), BASE)
    assert other.level == "emergency" and other.locked


def test_warning_status_remains_readable(tmp_path):
    state = ThermalState()
    for seconds in range(0, 31, 5):
        state.sample(reading(80), BASE + timedelta(seconds=seconds))
    path = tmp_path / "thermal-status.json"
    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    status = read_thermal_status(path, now=BASE + timedelta(seconds=30))
    assert status["level"] == "warning" and not status["locked"]
    assert "高温" in status["message"]


def test_monitor_recovers_from_corrupted_persisted_shape(tmp_path):
    path = tmp_path / "thermal-status.json"
    for payload in ("[]", '{"samples": {}, "events": []}',
                    '{"samples": [], "events": [], "locked": "false"}',
                    '{"level":"cooldown","locked":false,"recovery_ready":false,"samples":[],"events":[]}'):
        path.write_text(payload, encoding="utf-8")
        state = load_thermal_state(path)
        assert state.level == "unavailable" and state.locked
        state.sample(reading(65), BASE)
        assert state.level == "cooldown" and state.locked


def test_safe_worker_stop_checks_running_tasks_under_sqlite_lock(tmp_path, monkeypatch):
    if os.name != "nt":
        return
    db = Database(Settings(data_dir=tmp_path, session_secret="x" * 32))
    db.create_schema()
    marker = tmp_path / "service-logs" / "worker-runtime.json"
    marker.parent.mkdir()
    marker.write_text('{"pid":1234,"thermal_monitor_enabled":false}', encoding="utf-8")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "powershell.exe":
            return subprocess.CompletedProcess(command, 0, "OWNED\n", "")
        with db.engine.connect() as competing:
            import pytest
            competing.exec_driver_sql("PRAGMA busy_timeout=0")
            with pytest.raises(Exception):
                competing.exec_driver_sql("BEGIN IMMEDIATE")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr("video_workstation.cli.subprocess.run", fake_run)
    import pytest
    with pytest.raises(RuntimeError, match="不能确认"):
        stop_idle_worker(db, 9999)
    assert calls == []
    stop_idle_worker(db, 1234)
    assert calls[1] == ["taskkill", "/T", "/F", "/PID", "1234"]
    with db.session() as session:
        user = User(username="worker-test", password_hash="x", role="admin")
        session.add(user)
        session.flush()
        project = Project(name="Worker stop", created_by_id=user.id)
        session.add(project)
        session.flush()
        session.add(Task(project_id=project.id, created_by_id=user.id, task_type="write_script", status="running"))
    with pytest.raises(RuntimeError, match="正在运行"):
        stop_idle_worker(db, 1234)
    assert len(calls) == 3


def test_transient_heat_and_manual_recovery():
    state = ThermalState()
    state.sample(reading(85), BASE)
    state.sample(reading(79), BASE + timedelta(seconds=5))
    assert state.level == "normal"
    for seconds in range(10, 41, 5):
        state.sample(reading(85), BASE + timedelta(seconds=seconds))
    assert state.locked
    for seconds in range(45, 165, 5):
        state.sample(reading(74), BASE + timedelta(seconds=seconds))
    assert not state.recovery_ready
    state.sample(reading(74), BASE + timedelta(seconds=165))
    assert state.recovery_ready and state.locked
    assert state.resume(BASE + timedelta(seconds=165))
    assert state.level == "normal" and not state.locked


def test_stale_and_bad_monitor_file_block_dispatch(tmp_path):
    settings = Settings(data_dir=tmp_path, session_secret="x" * 32, thermal_monitor_enabled=True)
    assert not thermal_dispatch_allowed(settings, now=BASE)
    path = tmp_path / "thermal-status.json"
    path.write_text("invalid", encoding="utf-8")
    assert read_thermal_status(path, now=BASE)["level"] == "unavailable"
    assert not thermal_dispatch_allowed(settings, now=BASE)
    state = ThermalState()
    state.sample(reading(65), BASE)
    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    assert thermal_dispatch_allowed(settings, now=BASE + timedelta(seconds=14))
    assert read_thermal_status(path, now=BASE + timedelta(seconds=16))["level"] == "unavailable"
    assert not thermal_dispatch_allowed(settings, now=BASE + timedelta(seconds=16))
    malformed = state.to_dict()
    malformed.pop("locked")
    path.write_text(json.dumps(malformed), encoding="utf-8")
    assert not thermal_dispatch_allowed(settings, now=BASE + timedelta(seconds=5))
    malformed["locked"] = False
    malformed["level"] = "cooldown"
    path.write_text(json.dumps(malformed), encoding="utf-8")
    assert not thermal_dispatch_allowed(settings, now=BASE + timedelta(seconds=5))


def test_second_monitor_cannot_acquire_same_data_directory(tmp_path):
    handle = acquire_monitor_lock(tmp_path / "monitor.lock")
    try:
        import pytest
        with pytest.raises(RuntimeError, match="已经运行"):
            acquire_monitor_lock(tmp_path / "monitor.lock")
    finally:
        handle.close()


def test_unavailable_reading_never_looks_like_zero_or_clears_lock():
    state = ThermalState()
    for seconds in range(0, 31, 5):
        state.sample(reading(85), BASE + timedelta(seconds=seconds))
    state.sample(reading(None), BASE + timedelta(seconds=35))
    assert state.locked and state.level == "unavailable"
    assert state.temperature_c is None
    assert not state.resume(BASE + timedelta(seconds=36))


def test_single_failed_sensor_sample_has_fifteen_second_grace():
    state = ThermalState()
    state.sample(reading(60), BASE)
    state.sample(reading(None), BASE + timedelta(seconds=5))
    assert state.level == "checking" and not state.locked and state.temperature_c is None
    state.sample(reading(None), BASE + timedelta(seconds=15))
    assert state.level == "unavailable" and state.locked


def test_heat_lock_skips_heavy_task_but_allows_local_writing(tmp_path):
    db = Database(Settings(data_dir=tmp_path, session_secret="x" * 32))
    db.create_schema()
    with db.session() as session:
        user = User(username="writer", password_hash="x", role="admin")
        profile = ModelProfile(slug="heavy", display_name="Heavy", adapter_type="local_command", is_heavy=True)
        session.add_all([user, profile])
        session.flush()
        project = Project(name="Work", created_by_id=user.id)
        session.add(project)
        session.flush()
        session.add_all([
            Task(project_id=project.id, created_by_id=user.id, model_profile_id=profile.id,
                 task_type="generate", priority=0),
            Task(project_id=project.id, created_by_id=user.id, task_type="write_script", priority=2),
        ])
    with db.session() as session:
        task = QueueService(session).claim_next("worker", allow_heavy=False)
        assert task is not None and task.task_type == "write_script"


def test_worker_does_not_claim_heavy_task_without_fresh_monitor(tmp_path):
    settings = Settings(data_dir=tmp_path, session_secret="x" * 32,
                        minimum_free_bytes=1, thermal_monitor_enabled=True)
    db = Database(settings)
    db.create_schema()
    with db.session() as session:
        user = User(username="maker", password_hash="x", role="admin")
        profile = ModelProfile(slug="video", display_name="Video", adapter_type="demo", is_heavy=True)
        session.add_all([user, profile])
        session.flush()
        project = Project(name="Video", created_by_id=user.id)
        session.add(project)
        session.flush()
        task = Task(project_id=project.id, created_by_id=user.id, model_profile_id=profile.id,
                    task_type="generate", priority=0)
        session.add(task)
        session.flush()
        task_id = task.id
    with db.session() as session:
        assert Worker("worker", settings=settings).run_once(session) is False
        assert session.get(Task, task_id).status == "queued"


def test_thermal_status_endpoint_requires_login_and_resume_ready(tmp_path):
    from test_api import login, make_client
    client, app = make_client(tmp_path)
    app.state.settings.thermal_monitor_enabled = True
    assert client.get("/api/thermal/status").status_code == 401
    login(client, "admin")
    status = client.get("/api/thermal/status").json()
    assert status["level"] == "unavailable" and status["locked"]
    assert client.post("/api/thermal/resume").status_code == 409
    state = ThermalState()
    for seconds in range(0, 31, 5):
        state.sample(reading(85), BASE + timedelta(seconds=seconds))
    for seconds in range(35, 156, 5):
        state.sample(reading(70), BASE + timedelta(seconds=seconds))
    assert state.recovery_ready
    now = datetime.now(timezone.utc)
    state.last_sample_at = state.last_good_at = now
    state.recovery_since = now - timedelta(seconds=120)
    (tmp_path / "thermal-status.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")
    assert client.post("/api/thermal/resume").status_code == 200
    assert (tmp_path / "thermal-resume-request.json").is_file()
