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

Write-Host '请粘贴企业微信群机器人的 Webhook 地址，然后按回车。' -ForegroundColor Cyan
Write-Host '地址不会显示、不会作为命令行参数传递，也不会写入 Git。' -ForegroundColor DarkGray
& $pythonPath -m video_workstation.cli configure-wecom
if ($LASTEXITCODE -ne 0) { throw '企业微信提醒配置失败，请核对地址后重试。' }

Write-Host '企业微信测试消息已发送，配置已使用当前 Windows 用户加密保存。' -ForegroundColor Green
Write-Host '下次启动工作台时，温度提醒进程会自动运行。' -ForegroundColor Green

