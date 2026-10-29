from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from video_workstation.wecom import WeComDeliveryError
from video_workstation.wecom_notifier import ThermalNotifier


NOW = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)


def thermal(level="normal", temperature=70, *, utilization=90, memory=18000):
    return {
        "level": level,
        "temperature_c": temperature,
        "utilization_percent": utilization,
        "memory_used_mib": memory,
        "last_sample_at": NOW.isoformat(),
        "locked": level in {"cooldown", "emergency", "recovery_ready", "unavailable"},
    }


class Sender:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    def __call__(self, content):
        self.messages.append(content)
        if self.failures:
            self.failures -= 1
            raise WeComDeliveryError("企业微信消息发送失败")


def test_state_transitions_send_once_and_never_repeat_five_second_samples(tmp_path):
    sender = Sender()
    notifier = ThermalNotifier(tmp_path, sender, workstation_name="视频主机")
    notifier.tick(thermal(), NOW)
    notifier.tick(thermal("warning", 81), NOW + timedelta(seconds=30))
    notifier.tick(thermal("warning", 82), NOW + timedelta(seconds=35))
    notifier.tick(thermal("cooldown", 86), NOW + timedelta(seconds=65))
    notifier.tick(thermal("recovery_ready", 73), NOW + timedelta(minutes=5))
    assert len(sender.messages) == 3
    assert "高温预警" in sender.messages[0] and "81℃" in sender.messages[0]
    assert "暂停新任务" in sender.messages[1] and "86℃" in sender.messages[1]
    assert "温度已恢复" in sender.messages[2] and "73℃" in sender.messages[2]
    assert all("视频主机" in message for message in sender.messages)


def test_emergency_repeats_no_more_than_once_per_ten_minutes(tmp_path):
    sender = Sender()
    notifier = ThermalNotifier(tmp_path, sender)
    notifier.tick(thermal(), NOW)
    notifier.tick(thermal("emergency", 93), NOW + timedelta(seconds=5))
    notifier.tick(thermal("emergency", 92), NOW + timedelta(minutes=9, seconds=59))
    notifier.tick(thermal("emergency", 91), NOW + timedelta(minutes=10, seconds=5))
    assert len(sender.messages) == 2
    assert all("紧急高温" in message for message in sender.messages)
    assert "持续" in sender.messages[1]


def test_unavailable_and_monitoring_restored_are_each_sent_once(tmp_path):
    sender = Sender()
    notifier = ThermalNotifier(tmp_path, sender)
    notifier.tick(thermal(), NOW)
    notifier.tick(thermal("unavailable", None, utilization=None, memory=None), NOW + timedelta(seconds=20))
    notifier.tick(thermal("unavailable", None, utilization=None, memory=None), NOW + timedelta(seconds=25))
    notifier.tick(thermal(), NOW + timedelta(seconds=30))
    assert len(sender.messages) == 2
    assert "监测失联" in sender.messages[0]
    assert "监测已恢复" in sender.messages[1]


def test_restart_does_not_resend_the_current_state(tmp_path):
    first = Sender()
    notifier = ThermalNotifier(tmp_path, first)
    notifier.tick(thermal(), NOW)
    notifier.tick(thermal("warning", 81), NOW + timedelta(seconds=30))
    assert len(first.messages) == 1

    after_restart = Sender()
    ThermalNotifier(tmp_path, after_restart).tick(
        thermal("warning", 82), NOW + timedelta(seconds=35)
    )
    assert after_restart.messages == []


def test_failed_delivery_is_persisted_and_retried_after_backoff(tmp_path):
    failing = Sender(failures=1)
    notifier = ThermalNotifier(tmp_path, failing)
    notifier.tick(thermal(), NOW)
    notifier.tick(thermal("warning", 81), NOW + timedelta(seconds=30))
    state = json.loads((tmp_path / "wecom-notifier-state.json").read_text(encoding="utf-8"))
    assert len(state["pending"]) == 1
    assert state["pending"][0]["attempts"] == 1
    assert "key=" not in json.dumps(state)

    recovered = Sender()
    restarted = ThermalNotifier(tmp_path, recovered)
    restarted.tick(thermal("warning", 82), NOW + timedelta(seconds=59))
    assert recovered.messages == []
    restarted.tick(thermal("warning", 82), NOW + timedelta(seconds=60))
    assert len(recovered.messages) == 1
    state = json.loads((tmp_path / "wecom-notifier-state.json").read_text(encoding="utf-8"))
    assert state["pending"] == []


def test_first_start_reports_existing_danger_but_not_normal_state(tmp_path):
    normal_sender = Sender()
    ThermalNotifier(tmp_path / "normal", normal_sender).tick(thermal(), NOW)
    assert normal_sender.messages == []

    danger_sender = Sender()
    ThermalNotifier(tmp_path / "danger", danger_sender).tick(thermal("emergency", 93), NOW)
    assert len(danger_sender.messages) == 1
    assert "紧急高温" in danger_sender.messages[0]


def test_public_status_contains_no_message_body_or_secret(tmp_path):
    sender = Sender()
    ThermalNotifier(tmp_path, sender).tick(thermal("warning", 81), NOW)
    status = json.loads((tmp_path / "wecom-notifier-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "connected"
    assert status["pending_count"] == 0
    serialized = json.dumps(status, ensure_ascii=False)
    assert "key=" not in serialized
    assert "高温预警" not in serialized


def test_new_emergency_bypasses_an_ordinary_alert_waiting_for_retry(tmp_path):
    sender = Sender(failures=1)
    notifier = ThermalNotifier(tmp_path, sender)
    notifier.tick(thermal(), NOW)
    notifier.tick(thermal("warning", 81), NOW + timedelta(seconds=30))
    sender.failures = 0

    notifier.tick(thermal("emergency", 93), NOW + timedelta(seconds=31))

    assert "紧急高温" in sender.messages[-1]
    state = json.loads((tmp_path / "wecom-notifier-state.json").read_text(encoding="utf-8"))
    assert [item["level"] for item in state["pending"]] == ["warning"]


def test_outage_coalesces_repeated_levels_without_discarding_emergency(tmp_path):
    sender = Sender(failures=100)
    notifier = ThermalNotifier(tmp_path, sender)
    notifier.tick(thermal(), NOW)
    levels = ["warning", "cooldown", "emergency", "unavailable", "recovery_ready"]
    for index in range(40):
        level = levels[index % len(levels)]
        notifier.tick(thermal(level, 93 if level == "emergency" else 81),
                      NOW + timedelta(minutes=index + 1))

    state = json.loads((tmp_path / "wecom-notifier-state.json").read_text(encoding="utf-8"))
    pending_levels = [item["level"] for item in state["pending"]]
    assert "emergency" in pending_levels
    assert len(pending_levels) <= 7
    assert len(pending_levels) == len(set(pending_levels))


def test_outbox_is_persisted_before_network_delivery(tmp_path):
    observed = []

    def inspect_during_send(_content):
        state = json.loads((tmp_path / "wecom-notifier-state.json").read_text(encoding="utf-8"))
        observed.append([item["level"] for item in state["pending"]])

    notifier = ThermalNotifier(tmp_path, inspect_during_send)
    notifier.tick(thermal("emergency", 93), NOW)
    assert observed == [["emergency"]]
