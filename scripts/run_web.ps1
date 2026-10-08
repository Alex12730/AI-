[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) { throw "缺少 .venv，请先运行 scripts\setup.ps1" }
if ([string]::IsNullOrWhiteSpace($env:VIDEO_WORKSTATION_SESSION_SECRET) -or $env:VIDEO_WORKSTATION_SESSION_SECRET.Length -lt 24) {
    throw "VIDEO_WORKSTATION_SESSION_SECRET 至少需要 24 个字符"
}
$env:PYTHONPATH = "src"
& ".\.venv\Scripts\python.exe" -m uvicorn video_workstation.main:application --factory --host $HostAddress --port $Port
