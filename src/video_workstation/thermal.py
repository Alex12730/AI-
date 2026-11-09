"""Independent GPU temperature sampling, history, and dispatch protection.

The monitor is a separate process. Its atomic status file is the single source
for the web UI and worker, while task adapters cooperatively cancel their own
work when the shared lock activates.
"""
from __future__ import annotations

import json
import os
import sys
import time
import csv
from io import BytesIO
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import RUNTIME_VERSION, Settings
from .services.system_status import ProbeResult, GPUStatus, probe_gpu


CHINA_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")


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
    cooling_started_at: datetime | None = None
    first_at_or_below_75_at: datetime | None = None
    cooling_peak_c: int | None = None
    cooling_cycles: list[dict[str, Any]] = field(default_factory=list)

    def sample(self, result: ProbeResult[list[GPUStatus]], now: datetime, *, auto_resume: bool = False) -> None:
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
        self.recovery_since = (self.recovery_since or now) if self.locked and t <= 75 else None
        if t >= 85 and self.cooling_started_at is None:
            self.cooling_started_at = now
            self.first_at_or_below_75_at = None
            self.cooling_peak_c = t
        if self.cooling_started_at is not None:
            self.cooling_peak_c = max(self.cooling_peak_c or t, t)
            if t <= 75 and self.first_at_or_below_75_at is None:
                self.first_at_or_below_75_at = now
        self.recovery_ready = bool(self.locked and self.recovery_since and _seconds(now, self.recovery_since) >= 120)
        if t >= 93 or (self.emergency_since and _seconds(now, self.emergency_since) >= 10):
            self.locked = True
            self._level("emergency", now)
        elif t >= 85:
            self.locked = True
            self._level("cooldown", now)
        elif self.locked:
            self._level("recovery_ready" if self.recovery_ready else "cooldown", now)
        elif self.warning_since and _seconds(now, self.warning_since) >= 30:
            self._level("warning", now)
        else:
            self._level("normal", now)
        if auto_resume and self.recovery_ready:
            self.resume(now)
        self._record(now, t)

    def resume(self, now: datetime) -> bool:
        if not self.locked or not self.recovery_ready or self.temperature_c is None:
            return False
        if self.last_good_at is None or _seconds(now, self.last_good_at) > 15:
            return False
        if self.cooling_started_at is not None:
            seconds_to_75 = (_seconds(self.first_at_or_below_75_at, self.cooling_started_at)
                             if self.first_at_or_below_75_at else None)
            self.cooling_cycles.append({
                "started_at": self.cooling_started_at.isoformat(),
                "first_at_or_below_75_at": _iso(self.first_at_or_below_75_at),
                "recovered_at": now.isoformat(),
                "seconds_to_75": round(seconds_to_75, 3) if seconds_to_75 is not None else None,
                "total_pause_seconds": round(_seconds(now, self.cooling_started_at), 3),
                "peak_temperature_c": self.cooling_peak_c,
            })
            self.cooling_cycles = self.cooling_cycles[-100:]
        self.cooling_started_at = None
        self.first_at_or_below_75_at = None
        self.cooling_peak_c = None
        self.recovery_since = None
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
        self.samples.append({"at": now.isoformat(), "temperature_c": value,
                             "utilization_percent": self.utilization_percent,
                             "memory_used_mib": self.memory_used_mib})
        self.samples = self.samples[-720:]

    def to_dict(self) -> dict[str, Any]:
        result = {name: _iso(getattr(self, name)) for name in (
            "last_sample_at", "last_good_at", "warning_since", "cooldown_since", "emergency_since", "recovery_since",
            "cooling_started_at", "first_at_or_below_75_at"
        )} | {"monitor_version": RUNTIME_VERSION, "level": self.level, "locked": self.locked, "recovery_ready": self.recovery_ready,
             "temperature_c": self.temperature_c, "utilization_percent": self.utilization_percent,
             "memory_used_mib": self.memory_used_mib, "samples": self.samples, "events": self.events,
             "daily_max_c": self.daily_max_c, "daily_max_date": self.daily_max_date,
             "cooling_peak_c": self.cooling_peak_c, "cooling_cycles": self.cooling_cycles}
        sample_at = self.last_sample_at or datetime.now(timezone.utc)
        result["cooldown_elapsed_seconds"] = (round(_seconds(sample_at, self.cooling_started_at))
                                                if self.cooling_started_at else None)
        result["seconds_to_75"] = (round(_seconds(self.first_at_or_below_75_at, self.cooling_started_at))
                                    if self.first_at_or_below_75_at and self.cooling_started_at else None)
        result["stable_cooling_seconds"] = (min(120, round(_seconds(sample_at, self.recovery_since)))
                                             if self.locked and self.recovery_since else 0)
        result["resume_in_seconds"] = max(0, 120 - result["stable_cooling_seconds"]) if self.locked else 0
        result["last_cooling_cycle"] = self.cooling_cycles[-1] if self.cooling_cycles else None
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ThermalState":
        if (not isinstance(raw, dict) or not isinstance(raw.get("samples", []), list)
                or not isinstance(raw.get("events", []), list)
                or not isinstance(raw.get("cooling_cycles", []), list)):
            raise ValueError("Invalid thermal state shape")
        if (any(not isinstance(item, dict) for item in raw.get("samples", []))
            or any(not isinstance(item, dict) for item in raw.get("events", []))
            or any(type(raw.get(name)) is not bool for name in ("locked", "recovery_ready"))
            or any(raw.get(name) is not None and type(raw[name]) is not int for name in
                   ("temperature_c", "utilization_percent", "memory_used_mib", "daily_max_c", "cooling_peak_c"))
            or raw.get("level") not in {"normal", "checking", "warning", "cooldown", "emergency", "recovery_ready", "unavailable"}):
            raise ValueError("Invalid thermal state fields")
        level, locked, ready = raw["level"], raw["locked"], raw["recovery_ready"]
        if ((level in {"normal", "checking", "warning"} and locked)
            or (level in {"cooldown", "emergency", "recovery_ready", "unavailable"} and not locked)
            or (level == "recovery_ready") != ready):
            raise ValueError("Inconsistent thermal protection state")
        state = cls()
        for name in ("last_sample_at", "last_good_at", "warning_since", "cooldown_since", "emergency_since", "recovery_since",
                     "cooling_started_at", "first_at_or_below_75_at"):
            setattr(state, name, _date(raw.get(name)))
        for name in ("level", "locked", "recovery_ready", "temperature_c", "utilization_percent",
                     "memory_used_mib", "samples", "events", "daily_max_c", "daily_max_date",
                     "cooling_peak_c", "cooling_cycles"):
            if name in raw:
                setattr(state, name, raw[name])
        return state


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        for attempt in range(10):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                # Windows can briefly deny replacing a file while another
                # process (web UI, notifier, antivirus) has it open.
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        temp.unlink(missing_ok=True)


def read_thermal_status(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    unavailable = {"level": "unavailable", "locked": True, "recovery_ready": False,
                   "temperature_c": None, "utilization_percent": None, "memory_used_mib": None,
                   "last_sample_at": None, "message": "温度监测未运行或读数已失联；全部任务已暂停"}
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
            "cooldown": "降温保护中；全部任务已暂停",
            "emergency": "紧急高温；全部任务已暂停",
            "recovery_ready": "温度已恢复，请手动继续制作",
        }[raw["level"]]
        history_dir = path.parent / "thermal-history"
        raw["history_days"] = sorted(
            item.stem.removeprefix("temperature-") for item in history_dir.glob("temperature-*.csv")
            if item.stem.removeprefix("temperature-").count("-") == 2
        )[-31:]
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


def _append_csv(path: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def append_temperature_history(data_dir: Path, state: ThermalState, now: datetime) -> None:
    local_now = now.astimezone(CHINA_TIME)
    _append_csv(
        data_dir / "thermal-history" / f"temperature-{local_now.date().isoformat()}.csv",
        ["sampled_at", "temperature_c", "utilization_percent", "memory_used_mib", "level", "locked"],
        {"sampled_at": local_now.isoformat(), "temperature_c": state.temperature_c,
         "utilization_percent": state.utilization_percent, "memory_used_mib": state.memory_used_mib,
         "level": state.level, "locked": int(state.locked)},
    )


def append_cooling_cycle(data_dir: Path, cycle: dict[str, Any]) -> None:
    _append_csv(
        data_dir / "thermal-history" / "cooling-cycles.csv",
        ["started_at", "first_at_or_below_75_at", "recovered_at", "seconds_to_75",
         "total_pause_seconds", "peak_temperature_c"],
        cycle,
    )


def export_thermal_xlsx(data_dir: Path, day: str) -> bytes:
    try:
        selected = datetime.strptime(day, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("导出日期格式必须为 YYYY-MM-DD") from exc
    source = data_dir / "thermal-history" / f"temperature-{selected.isoformat()}.csv"
    if not source.is_file():
        raise FileNotFoundError("所选日期没有温度记录")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    samples = workbook.active
    samples.title = "温度采样"
    sample_headers = ["采样时间（北京时间）", "GPU温度（℃）", "GPU利用率（%）", "显存占用（MiB）", "状态", "已暂停"]
    samples.append(sample_headers)
    with source.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            samples.append([row.get("sampled_at"), _number(row.get("temperature_c")),
                            _number(row.get("utilization_percent")), _number(row.get("memory_used_mib")),
                            row.get("level"), "是" if row.get("locked") == "1" else "否"])
    cycles = workbook.create_sheet("降温周期")
    cycle_headers = ["85℃保护开始", "首次达到75℃", "自动恢复", "降到75℃耗时（秒）", "总暂停时长（秒）", "峰值温度（℃）"]
    cycles.append(cycle_headers)
    cycle_path = data_dir / "thermal-history" / "cooling-cycles.csv"
    if cycle_path.is_file():
        with cycle_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if _local_day(row.get("started_at")) != selected.isoformat():
                    continue
                cycles.append([_local_timestamp(row.get("started_at")), _local_timestamp(row.get("first_at_or_below_75_at")),
                               _local_timestamp(row.get("recovered_at")), _number(row.get("seconds_to_75")),
                               _number(row.get("total_pause_seconds")), _number(row.get("peak_temperature_c"))])
    for sheet in (samples, cycles):
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="17324D")
        for column, width in zip("ABCDEF", (28, 16, 18, 20, 18, 14)):
            sheet.column_dimensions[column].width = width
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _number(value: str | None):
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def _local_timestamp(value: str | None) -> str | None:
    parsed = _date(value)
    return parsed.astimezone(CHINA_TIME).isoformat() if parsed else None


def _local_day(value: str | None) -> str | None:
    converted = _local_timestamp(value)
    return converted[:10] if converted else None


def run_thermal_monitor(settings: Settings, *, interval: float = 5) -> None:
    monitor_lock = acquire_monitor_lock(settings.data_dir / "thermal-monitor.lock")
    path = settings.data_dir / "thermal-status.json"
    state = load_thermal_state(path)
    try:
        while True:
            now = datetime.now(timezone.utc)
            previous_cycle = state.cooling_cycles[-1].get("recovered_at") if state.cooling_cycles else None
            state.sample(probe_gpu(), now, auto_resume=settings.thermal_auto_resume)
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
            append_temperature_history(settings.data_dir, state, now)
            if state.cooling_cycles and state.cooling_cycles[-1].get("recovered_at") != previous_cycle:
                append_cooling_cycle(settings.data_dir, state.cooling_cycles[-1])
            time.sleep(interval)
    finally:
        monitor_lock.close()
