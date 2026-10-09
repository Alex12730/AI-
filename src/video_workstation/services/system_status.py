from __future__ import annotations

import csv
import io
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Generic, Literal, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import ModelProfile, Task, WorkerStatus
from .models import model_readiness


StatusLevel = Literal["ok", "warning", "critical", "unavailable"]
T = TypeVar("T")


@dataclass(slots=True, frozen=True)
class GPUStatus:
    name: str
    driver_version: str
    utilization_percent: int
    memory_used_mib: int
    memory_total_mib: int
    temperature_c: int

    @property
    def memory_free_mib(self) -> int:
        return max(0, self.memory_total_mib - self.memory_used_mib)

    @property
    def memory_used_percent(self) -> float:
        return 0.0 if self.memory_total_mib <= 0 else self.memory_used_mib / self.memory_total_mib * 100


@dataclass(slots=True, frozen=True)
class DiskStatus:
    name: str
    path: str
    total_gib: float
    free_gib: float
    status: StatusLevel


@dataclass(slots=True, frozen=True)
class Alert:
    code: str
    severity: Literal["warning", "critical"]
    message: str


@dataclass(slots=True)
class ProbeResult(Generic[T]):
    status: StatusLevel
    data: T
    error: str | None = None


@dataclass(slots=True)
class SystemStatus:
    generated_at: datetime
    gpu: ProbeResult[list[GPUStatus]]
    disks: list[DiskStatus]
    workers: list[dict[str, Any]]
    queue: dict[str, int]
    models: list[dict[str, Any]]
    alerts: list[Alert] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "gpu": {"status": self.gpu.status, "data": [asdict(item) for item in self.gpu.data], "error": self.gpu.error},
            "disks": [asdict(item) for item in self.disks],
            "workers": self.workers,
            "queue": self.queue,
            "models": self.models,
            "alerts": [asdict(item) for item in self.alerts],
        }


def parse_nvidia_smi(csv_text: str) -> list[GPUStatus]:
    if len(csv_text.encode("utf-8", errors="ignore")) > 64 * 1024:
        raise ValueError("GPU 输出过大")
    rows: list[GPUStatus] = []
    for raw in csv.reader(io.StringIO(csv_text)):
        if not raw or all(not cell.strip() for cell in raw):
            continue
        if len(raw) != 6:
            raise ValueError("GPU 输出列数不正确")
        name, driver, utilization, used, total, temperature = (cell.strip() for cell in raw)
        rows.append(
            GPUStatus(
                name=name,
                driver_version=driver,
                utilization_percent=int(utilization),
                memory_used_mib=int(used),
                memory_total_mib=int(total),
                temperature_c=int(temperature),
            )
        )
    if not rows:
        raise ValueError("未检测到 GPU")
    return rows


def probe_gpu(*, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> ProbeResult[list[GPUStatus]]:
    argv = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = run(argv, shell=False, check=False, capture_output=True, text=True, timeout=3)
        if completed.returncode != 0:
            return ProbeResult(status="unavailable", data=[], error="nvidia-smi 返回错误")
        return ProbeResult(status="ok", data=parse_nvidia_smi(completed.stdout))
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return ProbeResult(status="unavailable", data=[], error="无法获取 NVIDIA GPU 状态")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def worker_is_healthy(status: WorkerStatus, *, now: datetime, stale_after_seconds: float = 120) -> bool:
    return status.state != "stopped" and now - _aware(status.last_heartbeat_at) <= timedelta(seconds=stale_after_seconds)


def evaluate_alerts(
    gpus: list[GPUStatus],
    disks: list[DiskStatus],
    *,
    has_heavy_work: bool,
    queued_count: int,
    healthy_worker_count: int,
    recent_failure_classes: list[str],
) -> list[Alert]:
    alerts: list[Alert] = []
    for gpu in gpus:
        if gpu.temperature_c >= 80:
            alerts.append(Alert("gpu_temperature", "critical", f"{gpu.name} 温度达到 {gpu.temperature_c}℃"))
        if gpu.memory_used_percent >= 92 or (has_heavy_work and gpu.memory_free_mib < 4096):
            alerts.append(Alert("gpu_vram", "critical", f"{gpu.name} 显存余量不足"))
    for disk in disks:
        if disk.status == "critical":
            alerts.append(Alert("disk_space", "critical", f"{disk.name} 剩余空间不足 100GB"))
    if queued_count > 0 and healthy_worker_count == 0:
        alerts.append(Alert("worker_missing", "critical", "队列存在任务，但没有健康 Worker"))
    if len(recent_failure_classes) >= 2 and recent_failure_classes[0] == recent_failure_classes[1] and recent_failure_classes[0] in {"oom", "infrastructure"}:
        alerts.append(Alert("repeated_failure", "warning", "最近连续出现相同的模型基础设施失败"))
    return alerts


def _disk_status(name: str, path: Path, minimum_free_bytes: int) -> DiskStatus:
    usage = shutil.disk_usage(path)
    gib = 1024**3
    return DiskStatus(
        name=name,
        path=str(path),
        total_gib=round(usage.total / gib, 1),
        free_gib=round(usage.free / gib, 1),
        status="critical" if usage.free < minimum_free_bytes else "ok",
    )


def collect_system_status(session: Session, settings: Settings, *, now: datetime | None = None) -> SystemStatus:
    now = now or datetime.now(timezone.utc)
    gpu = probe_gpu()
    disk_paths: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, path in (("数据盘", settings.data_dir), ("资产盘", settings.asset_dir), ("归档盘", settings.archive_dir)):
        resolved = Path(path).resolve()
        if resolved not in seen:
            seen.add(resolved)
            disk_paths.append((name, resolved))
    disks: list[DiskStatus] = []
    for name, path in disk_paths:
        try:
            disks.append(_disk_status(name, path, settings.minimum_free_bytes))
        except OSError:
            disks.append(DiskStatus(name, str(path), 0, 0, "unavailable"))

    worker_rows = list(session.scalars(select(WorkerStatus).order_by(WorkerStatus.worker_id)))
    workers = [
        {
            "worker_id": row.worker_id,
            "state": row.state,
            "current_task_id": row.current_task_id,
            "last_heartbeat_at": _aware(row.last_heartbeat_at).isoformat(),
            "healthy": worker_is_healthy(row, now=now),
        }
        for row in worker_rows
    ]
    tasks = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(200)))
    queue = {state: sum(task.status == state for task in tasks) for state in ("queued", "running", "succeeded", "terminal_failed")}
    profiles = list(session.scalars(select(ModelProfile).order_by(ModelProfile.display_name)))
    models = [
        {
            "slug": profile.slug,
            "display_name": profile.display_name,
            "enabled": profile.enabled,
            "readiness": model_readiness(profile),
            "validated_presets": len(profile.validated_presets_json),
        }
        for profile in profiles
    ]
    profile_heavy = {profile.id: profile.is_heavy for profile in profiles}
    has_heavy_work = any(
        task.status in {"queued", "running"} and (task.task_type != "generate" or profile_heavy.get(task.model_profile_id, True))
        for task in tasks
    )
    recent_failure_classes = [
        task.error_class or "" for task in tasks if task.status == "terminal_failed"
    ][:2]
    alerts = evaluate_alerts(
        gpu.data,
        disks,
        has_heavy_work=has_heavy_work,
        queued_count=queue["queued"],
        healthy_worker_count=sum(bool(row["healthy"]) for row in workers),
        recent_failure_classes=recent_failure_classes,
    )
    return SystemStatus(now, gpu, disks, workers, queue, models, alerts)
