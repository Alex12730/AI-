from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from video_workstation.models import ModelProfile, WorkerStatus
from video_workstation.services.models import model_readiness, model_config_fingerprint
from video_workstation.services.system_status import (
    DiskStatus,
    GPUStatus,
    evaluate_alerts,
    parse_nvidia_smi,
    probe_gpu,
)


def test_parse_nvidia_smi_handles_multiple_gpus_and_spaces():
    rows = parse_nvidia_smi(
        "NVIDIA RTX A6000, 552.22, 73, 45056, 49140, 80\n"
        "NVIDIA GeForce GT 730, 475.14, 0, 120, 2048, 41\n"
    )
    assert rows[0] == GPUStatus("NVIDIA RTX A6000", "552.22", 73, 45056, 49140, 80)
    assert rows[1].name == "NVIDIA GeForce GT 730"


def test_probe_gpu_returns_unavailable_for_missing_timeout_and_bad_output():
    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("nvidia-smi", 3)

    def malformed(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="bad,row", stderr="")

    assert probe_gpu(run=missing).status == "unavailable"
    assert probe_gpu(run=timeout).status == "unavailable"
    assert probe_gpu(run=malformed).status == "unavailable"


def test_alert_thresholds_do_not_mark_small_idle_gpu_for_four_gb_reserve():
    small_idle = GPUStatus("GT 730", "x", 0, 100, 2048, 40)
    alerts = evaluate_alerts(
        [small_idle],
        [DiskStatus("data", "D:/data", 500, 400, "ok")],
        has_heavy_work=False,
        queued_count=0,
        healthy_worker_count=0,
        recent_failure_classes=[],
    )
    assert not any(alert.code == "gpu_vram" for alert in alerts)

    a6000 = GPUStatus("RTX A6000", "x", 50, 45500, 49140, 80)
    alerts = evaluate_alerts(
        [a6000],
        [DiskStatus("data", "D:/data", 500, 90, "critical")],
        has_heavy_work=True,
        queued_count=1,
        healthy_worker_count=0,
        recent_failure_classes=["oom", "oom"],
    )
    codes = {alert.code for alert in alerts}
    assert {"gpu_temperature", "gpu_vram", "disk_space", "worker_missing", "repeated_failure"} <= codes


def test_model_readiness_distinguishes_demo_configured_and_admitted():
    demo = ModelProfile(slug="demo", display_name="Demo", adapter_type="demo", enabled=True)
    real = ModelProfile(slug="real", display_name="Real", provider="local", adapter_type="local_command", enabled=True)
    assert model_readiness(demo) == "demo"
    assert model_readiness(real) == "registered"

    real.model_version = "v1"
    real.quantization = "int8"
    real.runtime_config_json = {"command": ["python", "run.py", "{output}"]}
    assert model_readiness(real) == "configured"
    real.validated_presets_json = [{"duration_seconds": 5, "aspect_ratio": "16:9", "config_fingerprint": model_config_fingerprint(real)}]
    assert model_readiness(real) == "admitted"


def test_worker_staleness_boundary_is_two_minutes():
    now = datetime.now(timezone.utc)
    fresh = WorkerStatus(worker_id="fresh", state="idle", last_heartbeat_at=now - timedelta(seconds=119))
    stale = WorkerStatus(worker_id="stale", state="idle", last_heartbeat_at=now - timedelta(seconds=121))
    from video_workstation.services.system_status import worker_is_healthy

    assert worker_is_healthy(fresh, now=now, stale_after_seconds=120)
    assert not worker_is_healthy(stale, now=now, stale_after_seconds=120)
