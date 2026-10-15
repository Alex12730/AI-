[CmdletBinding()]
param([string]$FFmpegDirectory)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先准备仓库的 .venv 环境。' }
if ($FFmpegDirectory) {
    $mediaDirectory = (Resolve-Path -LiteralPath $FFmpegDirectory).Path
    $env:PATH = "$mediaDirectory;$env:PATH"
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    throw '需要 FFmpeg 和 FFprobe，请传入 -FFmpegDirectory。'
}
if (-not $env:VIDEO_WORKSTATION_DATA_DIR) { $env:VIDEO_WORKSTATION_DATA_DIR = Join-Path $projectRoot 'data' }
$dataDirectory = $env:VIDEO_WORKSTATION_DATA_DIR
New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
$secretPath = Join-Path $dataDirectory 'session-secret.txt'
if (-not $env:VIDEO_WORKSTATION_SESSION_SECRET) {
    if (-not (Test-Path -LiteralPath $secretPath)) {
        $randomBytes = New-Object byte[] 48
        $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($randomBytes) } finally { $generator.Dispose() }
        [Convert]::ToBase64String($randomBytes) | Set-Content -LiteralPath $secretPath -Encoding ascii
    }
    $env:VIDEO_WORKSTATION_SESSION_SECRET = (Get-Content -LiteralPath $secretPath -Raw).Trim()
}
if ($env:VIDEO_WORKSTATION_SESSION_SECRET.Length -lt 24) { throw '工作站会话密钥太短。' }
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$logDirectory = Join-Path $dataDirectory 'service-logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
function Get-LocalHealth {
    $healthRequest = [System.Net.WebRequest]::Create('http://127.0.0.1:8000/health')
    $healthRequest.Proxy = $null
    $healthRequest.Timeout = 2000
    $healthRequest.ReadWriteTimeout = 2000
    $healthResponse = $healthRequest.GetResponse()
    try {
        $reader = New-Object System.IO.StreamReader($healthResponse.GetResponseStream())
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
    } finally { $healthResponse.Dispose() }
}
$webReady = $false
try {
    $health = Get-LocalHealth
    $webReady = $health.status -eq 'ok'
} catch {}
if (-not $webReady) {
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { throw '8000 端口已被其他服务占用。' }
    $web = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'video_workstation.main:application', '--factory', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'web.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'web.stderr.log')
    $web.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'web.pid')
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            $health = Get-LocalHealth
            if ($health.status -eq 'ok') { $webReady = $true; break }
        } catch {}
    }
    if (-not $webReady) { throw '工作站启动失败，请查看 data/service-logs/web.stderr.log。' }
}
$workerProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains($pythonPath) -and $_.CommandLine -match 'video_workstation\.cli\s+worker'
}
if (-not $workerProcesses) {
    $worker = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'video_workstation.cli', 'worker') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'worker.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'worker.stderr.log')
    $worker.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'worker.pid')
}
Write-Output '工作站：http://127.0.0.1:8000；首次使用在 /setup 创建管理员。'
Write-Output '模型引擎：http://127.0.0.1:8188；需要单独保持 ComfyUI 运行。'
