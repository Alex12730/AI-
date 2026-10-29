"""Independent, restart-safe Enterprise WeChat thermal notifications."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import time
from typing import Callable

from .config import Settings
from .thermal import acquire_monitor_lock, read_thermal_status
from .wecom import WeComClient, WeComConfigurationError, WeComDeliveryError, load_wecom_webhook


STATE_FILE = "wecom-notifier-state.json"
STATUS_FILE = "wecom-notifier-status.json"
ALERT_LEVELS = {"warning", "cooldown", "emergency", "recovery_ready", "unavailable"}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
        return parsed.replace(tzinfo=timezone.utc) if parsed and parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def _safe_text(value: str) -> str:
    return "".join(character for character in value if character.isalnum() or character in " ._-（）()")[:80] or "本机工作站"


def _message(level: str, status: dict, workstation_name: str, now: datetime) -> str:
    titles = {
        "warning": "GPU 高温预警",
        "cooldown": "GPU 高温保护：暂停新任务",
        "emergency": "GPU 紧急高温",
        "emergency_repeat": "GPU 紧急高温持续中",
        "recovery_ready": "GPU 温度已恢复",
        "unavailable": "GPU 温度监测失联",
        "monitoring_restored": "GPU 温度监测已恢复",
    }
    actions = {
        "warning": "请检查机箱风道、风扇和室温。",
        "cooldown": "工作台已暂停派发新的重型任务。",
        "emergency": "工作台已锁定后续重型任务，请立即检查主机。",
        "emergency_repeat": "紧急高温仍未恢复，请立即检查主机。",
        "recovery_ready": "温度已满足恢复条件，请在工作台确认继续制作。",
        "unavailable": "工作台无法取得可靠温度，新重型任务已暂停。",
        "monitoring_restored": "温度读数已经恢复。",
    }
    temperature = status.get("temperature_c")
    utilization = status.get("utilization_percent")
    memory = status.get("memory_used_mib")
    values = [
        f"**AI 视频工作台｜{titles[level]}**",
        f"> 主机：{_safe_text(workstation_name)}",
        f"> GPU 温度：{temperature}℃" if type(temperature) is int else "> GPU 温度：未知",
    ]
    if type(utilization) is int:
        values.append(f"> GPU 负载：{utilization}%")
    if type(memory) is int:
        values.append(f"> 显存占用：{memory} MiB")
    values.extend((f"> 时间：{now.astimezone().strftime('%Y-%m-%d %H:%M:%S')}", actions[level]))
    return "\n".join(values)


def _new_state() -> dict:
    return {"schema_version": 1, "last_level": None, "pending": [],
            "last_emergency_sent_at": None, "last_success_at": None,
            "last_error_at": None, "last_error": None}


class ThermalNotifier:
    def __init__(self, data_dir: Path, sender: Callable[[str], None], *, workstation_name: str | None = None):
        self.data_dir = Path(data_dir).resolve()
        self.state_path = self.data_dir / STATE_FILE
        self.status_path = self.data_dir / STATUS_FILE
        self.sender = sender
        self.workstation_name = workstation_name or socket.gethostname()
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (raw.get("schema_version") != 1 or not isinstance(raw.get("pending"), list)
                or raw.get("last_level") not in {None, "normal", "checking", *ALERT_LEVELS}):
                raise ValueError
            for item in raw["pending"]:
                if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
                    or not isinstance(item.get("content"), str) or type(item.get("attempts")) is not int
                    or not isinstance(item.get("next_attempt_at"), str)):
                    raise ValueError
            return raw
        except FileNotFoundError:
            return _new_state()
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return _new_state()

    def _enqueue(self, key: str, level: str, status: dict, now: datetime) -> None:
        if any(item.get("key") == key for item in self.state["pending"]):
            return
        self.state["pending"].append({"key": key, "level": level,
            "content": _message(level, status, self.workstation_name, now),
            "attempts": 0, "next_attempt_at": now.isoformat()})
        self.state["pending"] = self.state["pending"][-20:]

    def _deliver_one(self, now: datetime) -> None:
        if not self.state["pending"]:
            return
        item = self.state["pending"][0]
        next_attempt = _parse_time(item.get("next_attempt_at"))
        if next_attempt is None or now < next_attempt:
            return
        try:
            self.sender(item["content"])
        except WeComDeliveryError:
            item["attempts"] += 1
            delays = (30, 120, 600, 1800)
            item["next_attempt_at"] = (now + timedelta(seconds=delays[min(item["attempts"] - 1, 3)])).isoformat()
            self.state["last_error"] = "企业微信消息发送失败，等待重试"
            self.state["last_error_at"] = now.isoformat()
            return
        self.state["pending"].pop(0)
        self.state["last_success_at"] = now.isoformat()
        self.state["last_error"] = None
        if item["level"] in {"emergency", "emergency_repeat"}:
            self.state["last_emergency_sent_at"] = now.isoformat()

    def tick(self, status: dict, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        level = status.get("level")
        if level not in {"normal", "checking", *ALERT_LEVELS}:
            level = "unavailable"
            status = {**status, "level": level, "temperature_c": None,
                      "utilization_percent": None, "memory_used_mib": None}
        previous = self.state.get("last_level")
        if level != previous:
            if previous == "unavailable" and level in {"normal", "checking"}:
                self._enqueue(f"monitoring_restored:{now.isoformat()}", "monitoring_restored", status, now)
            elif level in ALERT_LEVELS:
                self._enqueue(f"{level}:{now.isoformat()}", level, status, now)
            self.state["last_level"] = level
        if level == "emergency":
            last_sent = _parse_time(self.state.get("last_emergency_sent_at"))
            pending_emergency = any(item.get("level") in {"emergency", "emergency_repeat"}
                                    for item in self.state["pending"])
            if last_sent and (now - last_sent).total_seconds() >= 600 and not pending_emergency:
                self._enqueue(f"emergency_repeat:{now.isoformat()}", "emergency_repeat", status, now)
        self._deliver_one(now)
        _atomic_json(self.state_path, self.state)
        _atomic_json(self.status_path, {"state": "retrying" if self.state["pending"] else "connected",
            "pending_count": len(self.state["pending"]), "last_success_at": self.state.get("last_success_at"),
            "last_error_at": self.state.get("last_error_at"), "last_error": self.state.get("last_error"),
            "observed_level": level, "updated_at": now.isoformat()})


def run_wecom_notifier(settings: Settings, *, interval: float = 5) -> None:
    lock = acquire_monitor_lock(settings.data_dir / "wecom-notifier.lock")
    try:
        webhook = load_wecom_webhook(settings.data_dir)
        if webhook is None:
            _atomic_json(settings.data_dir / STATUS_FILE, {"state": "disabled", "pending_count": 0,
                "updated_at": datetime.now(timezone.utc).isoformat()})
            return
        notifier = ThermalNotifier(settings.data_dir, WeComClient(webhook).send_markdown)
        while True:
            configured = load_wecom_webhook(settings.data_dir)
            if configured is None:
                _atomic_json(settings.data_dir / STATUS_FILE, {"state": "disabled", "pending_count": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat()})
                return
            if configured != webhook:
                webhook = configured
                notifier.sender = WeComClient(webhook).send_markdown
            notifier.tick(read_thermal_status(settings.data_dir / "thermal-status.json"))
            time.sleep(interval)
    except WeComConfigurationError:
        _atomic_json(settings.data_dir / STATUS_FILE, {"state": "configuration_error", "pending_count": 0,
            "updated_at": datetime.now(timezone.utc).isoformat()})
        return
    finally:
        lock.close()
