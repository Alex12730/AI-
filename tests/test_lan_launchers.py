from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8-sig")


def test_lan_scripts_are_windows_powershell_51_safe_utf8():
    for name in ("start_lan_workstation.ps1", "enable_lan_access.ps1", "disable_lan_access.ps1"):
        assert (SCRIPTS / name).read_bytes().startswith(b"\xef\xbb\xbf")


def test_general_launcher_is_loopback_by_default_but_supports_explicit_lan_listen():
    content = text("start_local_workstation.ps1")
    assert "[string]$ListenAddress = '127.0.0.1'" in content
    assert "@('127.0.0.1', '0.0.0.0')" in content
    assert "'--host', $ListenAddress" in content
    assert "listen_address=$ListenAddress" in content
    assert "web-runtime.json" in content
    assert "$webInstanceId" in content
    assert "instance_id" in content
    assert "[int]::TryParse" in content
    assert "foreach ($oldWeb in $webProcesses)" not in content


def test_daily_lan_launcher_explicitly_uses_all_interfaces_for_workstation_only():
    content = text("start_lan_workstation.ps1")
    assert "start_local_workstation.ps1" in content
    assert "-ListenAddress '0.0.0.0'" in content
    assert "-LocalPort 8188" not in content


def test_firewall_enable_rule_is_private_local_subnet_tcp_8000_only():
    content = text("enable_lan_access.ps1")
    assert "New-NetFirewallRule" in content
    assert "-Direction Inbound" in content
    assert "-Action Allow" in content
    assert "-Protocol TCP" in content
    assert "-LocalPort 8000" in content
    assert "-Profile Private" in content
    assert "-RemoteAddress LocalSubnet" in content
    assert "Set-NetConnectionProfile" in content
    assert "Get-NetRoute" in content
    assert "HardwareInterface" in content
    assert "bestCandidates.Count -ne 1" in content
    assert "-LocalPort 8188" not in content
    assert "-RemoteAddress Any" not in content


def test_firewall_disable_only_removes_the_owned_rule():
    enable = text("enable_lan_access.ps1")
    disable = text("disable_lan_access.ps1")
    marker = "AI Video Workstation LAN TCP 8000"
    assert marker in enable and marker in disable
    assert "AI.VideoWorkstation.LAN.TCP.8000" in enable
    assert "AI.VideoWorkstation.LAN.TCP.8000" in disable
    assert "Get-NetFirewallRule -Name $ruleName" in enable
    assert "Get-NetFirewallRule -Name $ruleName" in disable
    assert "New-NetFirewallRule -Name $ruleName" in enable
    assert "Remove-NetFirewallRule" in disable
    assert "-LocalPort 8188" not in disable


def test_launcher_prints_both_local_and_lan_addresses_without_exposing_comfyui():
    content = text("start_local_workstation.ps1")
    assert "局域网入口" in content
    assert "http://127.0.0.1:8000" in content
    assert "http://127.0.0.1:8188" in content
