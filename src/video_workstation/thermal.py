"""Independent GPU temperature sampling and conservative dispatch protection.

The monitor is a separate process. Its atomic status file is the single source
for the web UI and the worker. No external notifications or process killing.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RUNTIME_VERSION, Settings
from .services.system_status import ProbeResult, GPUStatus, probe_gpu


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _date(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
        return parsed.replace(tzinfo=timezone.utc) if parsed and parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def _seconds(now: datetime, since: datetime | None) -> float:
    return (now - since).total_seconds() if since else 0


@dataclass
class ThermalState:
    level: str = "unavailable"
    locked: bool = True
    recovery_ready: bool = False
    temperature_c: int | None = None
    utilization_percent: int | None = None
    memory_used_mib: int | None = None
    last_sample_at: datetime | None = None
    last_good_at: datetime | None = None
    warning_since: datetime | None = None
    cooldown_since: datetime | None = None
    emergency_since: datetime | None = None
    recovery_since: datetime | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    daily_max_c: int | None = None
    daily_max_date: str | None = None

    def sample(self, result: ProbeResult[list[GPUStatus]], now: datetime) -> None:
        first_valid_sample = self.last_good_at is None and not self.events
        gap = _seconds(now, self.last_sample_at)
        self.last_sample_at = now
        gpu = result.data[0] if result.status == "ok" and result.data else None
        if gpu is None:
            self.temperature_c = None
            self.utilization_percent = None
            self.memory_used_mib = None
            self.warning_since = self.cooldown_since = self.emergency_since = self.recovery_since = None
            self.recovery_ready = False
            if self.locked or self.last_good_at is None or _seconds(now, self.last_good_at) >= 15:
                self._level("unavailable", now)
                self.locked = True
            else:
                self._level("checking", now)
            self._record(now, None)
            return
        if gap > 15:
            self.warning_since = self.cooldown_since = self.emergency_since = self.recovery_since = None
        self.last_good_at = now
        if first_valid_sample:
            self.locked = False
        t = gpu.temperature_c
        self.temperature_c = t
        self.utilization_percent = gpu.utilization_percent
        self.memory_used_mib = gpu.memory_used_mib
        today = now.date().isoformat()
        if self.daily_max_date != today:
            self.daily_max_date, self.daily_max_c = today, t
        else:
            self.daily_max_c = max(self.daily_max_c or t, t)
        self.warning_since = (self.warning_since or now) if t >= 80 else None
        self.cooldown_since = (self.cooldown_since or now) if t >= 85 else None
        self.emergency_since = (self.emergency_since or now) if t >= 90 else None
        self.recovery_since = (self.recovery_since or now) if t < 75 else None
        self.recovery_ready = bool(self.locked and self.recovery_since and _seconds(now, self.recovery_since) >= 120)
        if t >= 93 or (self.emergency_since and _seconds(now, self.emergency_since) >= 10):
            self.locked = True
            self._level("emergency", now)
        elif self.cooldown_since and _seconds(now, self.cooldown_since) >= 30:
            self.locked = True
            self._level("cooldown", now)
        elif self.locked:
            self._level("recovery_ready" if self.recovery_ready else "cooldown", now)
        elif self.warning_since and _seconds(now, self.warning_since) >= 30:
            self._level("warning", now)
        else:
            self._level("normal", now)
        self._record(now, t)

    def resume(self, now: datetime) -> bool:
        if not self.locked or not self.recovery_ready or self.temperature_c is None:
            return False
        if self.last_good_at is None or _seconds(now, self.last_good_at) > 15:
            return False
        self.locked = False
        self.recovery_ready = False
        self._level("normal", now)
        return True

    def _level(self, level: str, now: datetime) -> None:
        if self.level != level:
            self.events.append({"at": now.isoformat(), "level": level, "temperature_c": self.temperature_c})
            self.events = self.events[-100:]
        self.level = level

    def _record(self, now: datetime, value: int | None) -> None:
        self.samples.append({"at": now.isoformat(), "temperature_c": value})
        self.samples = self.samples[-720:]

    def to_dict(self) -> dict[str, Any]:
        return {name: _iso(getattr(self, name)) for name in (
            "last_sample_at", "last_good_at", "warning_since", "cooldown_since", "emergency_since", "recovery_since"
        )} | {"monitor_version": RUNTIME_VERSION, "level": self.level, "locked": self.locked, "recovery_ready": self.recovery_ready,
             "temperature_c": self.temperature_c, "utilization_percent": self.utilization_percent,
             "memory_used_mib": self.memory_used_mib, "samples": self.samples, "events": self.events,
             "daily_max_c": self.daily_max_c, "daily_max_date": self.daily_max_date}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ThermalState":
        if not isinstance(raw, dict) or not isinstance(raw.get("samples", []), list) or not isinstance(raw.get("events", []), list):
            raise ValueError("Invalid thermal state shape")
        if (any(not isinstance(item, dict) for item in raw.get("samples", []))
            or any(not isinstance(item, dict) for item in raw.get("events", []))
            or any(type(raw.get(name)) is not bool for name in ("locked", "recovery_ready"))
            or any(raw.get(name) is not None and type(raw[name]) is not int for name in
                   ("temperature_c", "utilization_percent", "memory_used_mib", "daily_max_c"))
            or raw.get("level") not in {"normal", "checking", "warning", "cooldown", "emergency", "recovery_ready", "unavailable"}):
            raise ValueError("Invalid thermal state fields")
        level, locked, ready = raw["level"], raw["locked"], raw["recovery_ready"]
        if ((level in {"normal", "checking", "warning"} and locked)
            or (level in {"cooldown", "emergency", "recovery_ready", "unavailable"} and not locked)
            or (level == "recovery_ready") != ready):
            raise ValueError("Inconsistent thermal protection state")
        state = cls()
        for name in ("last_sample_at", "last_good_at", "warning_since", "cooldown_since", "emergency_since", "recovery_since"):
            setattr(state, name, _date(raw.get(name)))
        for name in ("level", "locked", "recovery_ready", "temperature_c", "utilization_percent",
                     "memory_used_mib", "samples", "events", "daily_max_c", "daily_max_date"):
            if name in raw:
                setattr(state, name, raw[name])
        return state


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def read_thermal_status(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    unavailable = {"level": "unavailable", "locked": True, "recovery_ready": False,
                   "temperature_c": None, "utilization_percent": None, "memory_used_mib": None,
                   "last_sample_at": None, "message": "温度监测未运行或读数已失联；新重型任务已暂停"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return unavailable
        sampled = _date(raw.get("last_sample_at"))
        good = _date(raw.get("last_good_at"))
        if sampled is None or good is None or abs(_seconds(now, sampled)) >= 15 or abs(_seconds(now, good)) >= 15:
            return unavailable | {"samples": raw.get("samples", []), "events": raw.get("events", [])}
        level = raw.get("level")
        locked = raw.get("locked")
        temperature = raw.get("temperature_c")
        if (level not in {"normal", "checking", "warning", "cooldown", "emergency", "recovery_ready"}
            or type(locked) is not bool or type(raw.get("recovery_ready")) is not bool
            or not isinstance(raw.get("samples"), list) or not isinstance(raw.get("events"), list)
            or (level in {"normal", "warning", "checking"} and locked)
            or (level in {"cooldown", "emergency", "recovery_ready"} and not locked)
            or (level == "checking" and temperature is not None)
            or (level != "checking" and type(temperature) is not int)
            or (level == "recovery_ready") != raw["recovery_ready"]):
            return unavailable
        raw["message"] = {
            "normal": "温度正常", "checking": "温度读数暂不可用，正在重试",
            "warning": "GPU 持续高温，请检查散热",
            "cooldown": "降温保护中；新重型任务已暂停",
            "emergency": "紧急高温；新任务已暂停，当前任务需人工处理",
            "recovery_ready": "温度已恢复，请手动继续制作",
        }[raw["level"]]
        return raw
    except (OSError, UnicodeError, ValueError, TypeError):
        return unavailable


def thermal_dispatch_allowed(settings: Settings, *, now: datetime | None = None) -> bool:
    if not settings.thermal_monitor_enabled:
        return True
    return not read_thermal_status(settings.data_dir / "thermal-status.json", now=now)["locked"]


def request_thermal_resume(settings: Settings, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    status = read_thermal_status(settings.data_dir / "thermal-status.json", now=now)
    if not settings.thermal_monitor_enabled or not status.get("recovery_ready"):
        return False
    _write_atomic(settings.data_dir / "thermal-resume-request.json", {"requested_at": now.isoformat()})
    return True


def acquire_monitor_lock(path: Path):
    """Keep a file lock for the monitor lifetime; a crashed process releases it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        raise RuntimeError("温度监测进程已经运行") from None


def load_thermal_state(path: Path) -> ThermalState:
    try:
        return ThermalState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ThermalState()
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        # A damaged persisted lock must never silently become an unlocked first sample.
        return ThermalState(events=[{"at": datetime.now(timezone.utc).isoformat(),
                                     "level": "unavailable", "temperature_c": None}])


def run_thermal_monitor(settings: Settings, *, interval: float = 5) -> None:
    monitor_lock = acquire_monitor_lock(settings.data_dir / "thermal-monitor.lock")
    path = settings.data_dir / "thermal-status.json"
    state = load_thermal_state(path)
    try:
        while True:
            now = datetime.now(timezone.utc)
            state.sample(probe_gpu(), now)
            request_path = settings.data_dir / "thermal-resume-request.json"
            if request_path.exists():
                try:
                    requested = _date(json.loads(request_path.read_text(encoding="utf-8")).get("requested_at"))
                    if requested and 0 <= _seconds(now, requested) <= 15:
                        state.resume(now)
                except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
                    pass
                request_path.unlink(missing_ok=True)
            _write_atomic(path, state.to_dict())
            time.sleep(interval)
    finally:
        monitor_lock.close()
