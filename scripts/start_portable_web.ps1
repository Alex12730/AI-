[CmdletBinding()]
param(
    [string]$DataDirectory,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '请先运行 install-windows.cmd，或 scripts/setup.ps1 安装运行环境。'
}
& $pythonPath -c 'import sys, uvicorn, fastapi, sqlalchemy, alembic; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 环境不可用，请重新运行 scripts/setup.ps1 查看原因。' }

if (-not $DataDirectory) { $DataDirectory = Join-Path $projectRoot 'data' }
$env:VIDEO_WORKSTATION_DATA_DIR = [System.IO.Path]::GetFullPath($DataDirectory)
New-Item -ItemType Directory -Force -Path $env:VIDEO_WORKSTATION_DATA_DIR | Out-Null
$secretPath = Join-Path $env:VIDEO_WORKSTATION_DATA_DIR 'session-secret.txt'
if (-not (Test-Path -LiteralPath $secretPath)) {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [Convert]::ToBase64String($bytes) | Set-Content -LiteralPath $secretPath -Encoding Ascii -NoNewline
}
$env:VIDEO_WORKSTATION_SESSION_SECRET = (Get-Content -LiteralPath $secretPath -Raw).Trim()
if ($env:VIDEO_WORKSTATION_SESSION_SECRET.Length -lt 24) { throw '本机会话密钥文件无效。' }
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:VIDEO_WORKSTATION_SECURE_COOKIES = '0'
# 此入口只运行 Web；没有 Worker，不会执行排队的生成任务。
# 保留重型任务的温度检查策略；生产启动器负责启动监测和 Worker。
$env:VIDEO_WORKSTATION_THERMAL_MONITOR = '1'
Write-Host "工作台基础模式：http://127.0.0.1:$Port"
Write-Host "首次使用：http://127.0.0.1:$Port/setup （创建你自己的管理员账号）"
Write-Host '可查看界面、编辑项目和分镜；模型生成、自动文案与视频合成需要另行部署运行环境。'
Write-Host '请保持这个窗口打开；按 Ctrl+C 停止。完整生产启动步骤见 README.md。'
& $pythonPath -m uvicorn video_workstation.main:application --factory --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
