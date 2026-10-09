[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("web", "worker")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "缺少 .venv，请先运行 scripts\setup.ps1"
}

$dataDir = Join-Path $projectRoot "data"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$env:PYTHONPATH = "src"
$env:VIDEO_WORKSTATION_DATA_DIR = $dataDir

if ($Mode -eq "web") {
    $secretPath = Join-Path $dataDir ".session-secret"
    if (-not (Test-Path -LiteralPath $secretPath)) {
        $secretBytes = [byte[]]::new(32)
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $generator.GetBytes($secretBytes)
        } finally {
            $generator.Dispose()
        }
        [Convert]::ToBase64String($secretBytes) | Set-Content -LiteralPath $secretPath -Encoding Ascii -NoNewline
    }
    $env:VIDEO_WORKSTATION_SESSION_SECRET = (Get-Content -LiteralPath $secretPath -Raw).Trim()
    $webProcess = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "video_workstation.main:application", "--factory", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dataDir "web-service.stdout.log") `
        -RedirectStandardError (Join-Path $dataDir "web-service.stderr.log")
    $webProcess.WaitForExit()
    exit $webProcess.ExitCode
}

$ready = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $ready) {
    throw "Web 服务未能在规定时间内启动，Worker 不会运行"
}

$workerProcess = Start-Process -FilePath $python `
    -ArgumentList @("-m", "video_workstation.cli", "worker") `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $dataDir "worker-service.stdout.log") `
    -RedirectStandardError (Join-Path $dataDir "worker-service.stderr.log")
$workerProcess.WaitForExit()
exit $workerProcess.ExitCode
