[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先启动或安装工作台运行环境。' }
if (-not $env:VIDEO_WORKSTATION_DATA_DIR) {
    $storageRoot = if (Test-Path -LiteralPath 'G:\AI_Workstation\data') { 'G:\AI_Workstation' } else { $projectRoot }
    $env:VIDEO_WORKSTATION_DATA_DIR = Join-Path $storageRoot 'data'
}
$env:PYTHONPATH = Join-Path $projectRoot 'src'
Set-Location -LiteralPath $projectRoot

& $pythonPath -m video_workstation.cli disable-wecom
if ($LASTEXITCODE -ne 0) { throw '停用企业微信提醒失败。' }
Write-Host '企业微信温度提醒已停用；后台通知进程会在下一次检查时自动退出。' -ForegroundColor Green

