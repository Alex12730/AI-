from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")


def test_configuration_launcher_uses_hidden_prompt_and_never_accepts_webhook_argument():
    content = script("configure_wecom_alerts.ps1")
    assert "-m video_workstation.cli configure-wecom" in content
    assert "Webhook" not in content.split("param(", 1)[1].split(")", 1)[0]
    assert "qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" not in content


def test_disable_launcher_removes_encrypted_configuration_through_cli():
    content = script("disable_wecom_alerts.ps1")
    assert "-m video_workstation.cli disable-wecom" in content
    assert "qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" not in content


def test_workstation_starts_notifier_only_when_encrypted_secret_exists():
    content = script("start_local_workstation.ps1")
    assert "secrets/wecom-webhook.dpapi" in content.replace("\\", "/")
    assert "video_workstation.cli', 'notify-wecom'" in content
    assert "-WindowStyle Hidden" in content
    assert "wecom.stdout.log" in content
    assert "wecom.stderr.log" in content


def test_workstation_detects_owned_notifier_without_putting_webhook_on_command_line():
    content = script("start_local_workstation.ps1")
    assert "video_workstation\\.cli\\s+notify-wecom" in content
    assert "CommandLine.Contains($pythonPath)" in content
    assert "qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" not in content
