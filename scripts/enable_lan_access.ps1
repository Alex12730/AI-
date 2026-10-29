[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ruleName = 'AI.VideoWorkstation.LAN.TCP.8000'
$ruleDisplayName = 'AI Video Workstation LAN TCP 8000'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -Wait -PassThru
    exit $elevated.ExitCode
}

$routeCandidates = foreach ($route in @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop)) {
    $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
    $ipInterface = Get-NetIPInterface -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
    if ($adapter -and $adapter.Status -eq 'Up' -and $adapter.HardwareInterface -and $ipInterface) {
        [pscustomobject]@{InterfaceIndex=$route.InterfaceIndex; TotalMetric=([int]$route.RouteMetric + [int]$ipInterface.InterfaceMetric)}
    }
}
$physicalCandidates = @($routeCandidates | Sort-Object TotalMetric | Group-Object InterfaceIndex | ForEach-Object { $_.Group[0] })
if ($physicalCandidates.Count -eq 0) { throw '未找到正在联网的物理 WLAN 或以太网。' }
$bestMetric = ($physicalCandidates | Measure-Object TotalMetric -Minimum).Minimum
$bestCandidates = @($physicalCandidates | Where-Object { $_.TotalMetric -eq $bestMetric })
if ($bestCandidates.Count -ne 1) { throw '检测到多个同优先级网络，请断开不使用的网卡后重试。' }
$configuration = Get-NetIPConfiguration -InterfaceIndex $bestCandidates[0].InterfaceIndex -ErrorAction Stop
$profile = Get-NetConnectionProfile -InterfaceIndex $configuration.InterfaceIndex -ErrorAction Stop
if ($profile.NetworkCategory -eq 'Public') {
    Set-NetConnectionProfile -InterfaceIndex $configuration.InterfaceIndex -NetworkCategory Private
}

Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name $ruleName -DisplayName $ruleDisplayName -Description 'Allow authenticated AI video workstation access from the private local subnet only.' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 `
    -Profile Private -RemoteAddress LocalSubnet | Out-Null

Write-Host '局域网访问已启用：只允许专用网络的本地子网访问 TCP 8000。' -ForegroundColor Green
Write-Host ("访问地址：http://{0}:8000" -f $configuration.IPv4Address.IPAddress) -ForegroundColor Cyan
Write-Host 'ComfyUI 8188 仍只允许本机访问。' -ForegroundColor DarkGray

