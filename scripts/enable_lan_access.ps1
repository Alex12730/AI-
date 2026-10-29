[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ruleName = 'AI Video Workstation LAN TCP 8000'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -Wait -PassThru
    exit $elevated.ExitCode
}

$activeConfigurations = @(Get-NetIPConfiguration | Where-Object {
    $_.IPv4Address -and $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up'
})
if ($activeConfigurations.Count -eq 0) { throw '未找到正在联网的 WLAN 或以太网。' }

foreach ($configuration in $activeConfigurations) {
    $profile = Get-NetConnectionProfile -InterfaceIndex $configuration.InterfaceIndex -ErrorAction Stop
    if ($profile.NetworkCategory -eq 'Public') {
        Set-NetConnectionProfile -InterfaceIndex $configuration.InterfaceIndex -NetworkCategory Private
    }
}

Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Description 'Allow authenticated AI video workstation access from the private local subnet only.' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 `
    -Profile Private -RemoteAddress LocalSubnet | Out-Null

Write-Host '局域网访问已启用：只允许专用网络的本地子网访问 TCP 8000。' -ForegroundColor Green
foreach ($configuration in $activeConfigurations) {
    Write-Host ("访问地址：http://{0}:8000" -f $configuration.IPv4Address.IPAddress) -ForegroundColor Cyan
}
Write-Host 'ComfyUI 8188 仍只允许本机访问。' -ForegroundColor DarkGray

