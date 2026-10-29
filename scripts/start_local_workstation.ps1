[CmdletBinding()]
param([string]$FFmpegDirectory)

$ErrorActionPreference = 'Stop'
$startupMutex = [System.Threading.Mutex]::new($false, 'Local\AIVideoWorkstationStartup')
$startupLock = $false
try {
$startupLock = $startupMutex.WaitOne(120000)
if (-not $startupLock) { throw '另一个工作站启动操作尚未完成，请稍后重试。' }
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
if (-not $env:VIDEO_WORKSTATION_DATA_DIR) {
    $storageRoot = if (Test-Path -LiteralPath 'G:\AI_Workstation\data') { 'G:\AI_Workstation' } else { $projectRoot }
    $env:VIDEO_WORKSTATION_DATA_DIR = Join-Path $storageRoot 'data'
}
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
$env:VIDEO_WORKSTATION_THERMAL_MONITOR = '1'
$logDirectory = Join-Path $dataDirectory 'service-logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
function Stop-OwnedTree([int]$rootProcessId) {
    $allProcesses = @(Get-CimInstance Win32_Process)
    $processIds = @($rootProcessId)
    for ($index = 0; $index -lt $processIds.Count; $index++) {
        $children = @($allProcesses | Where-Object { $_.ParentProcessId -eq $processIds[$index] })
        foreach ($child in $children) { if ($processIds -notcontains $child.ProcessId) { $processIds += $child.ProcessId } }
    }
    [array]::Reverse($processIds)
    foreach ($ownedId in $processIds) { Stop-Process -Id $ownedId -Force -ErrorAction SilentlyContinue }
}
$monitorProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains($pythonPath) -and $_.CommandLine -match 'video_workstation\.cli\s+monitor-temperature'
}
if ($monitorProcesses) {
    $monitorVersion = 0
    try { $monitorVersion = (Get-Content -LiteralPath (Join-Path $dataDirectory 'thermal-status.json') -Raw -Encoding UTF8 | ConvertFrom-Json).monitor_version } catch {}
    if ($monitorVersion -ne 5) {
        foreach ($oldMonitor in $monitorProcesses) { Stop-OwnedTree $oldMonitor.ProcessId }
        Start-Sleep -Seconds 1
        $monitorProcesses = @()
    }
}
if (-not $monitorProcesses) {
    $monitor = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'video_workstation.cli', 'monitor-temperature') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'temperature.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'temperature.stderr.log')
    $monitor.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'temperature.pid')
}
Start-Sleep -Seconds 1
if (-not (Test-Path -LiteralPath (Join-Path $dataDirectory 'thermal-status.json'))) {
    Write-Warning '温度监测尚未产生读数；新重型任务会等待监测恢复。'
}
$wecomSecretPath = Join-Path $dataDirectory 'secrets/wecom-webhook.dpapi'
$wecomConfigured = Test-Path -LiteralPath $wecomSecretPath
if ($wecomConfigured) {
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $normalizedDataDirectory = [System.IO.Path]::GetFullPath($dataDirectory).TrimEnd('\').ToUpperInvariant()
        $directoryHash = $hasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($normalizedDataDirectory))
        $notifierInstanceId = -join ($directoryHash | ForEach-Object { $_.ToString('x2') })
    } finally { $hasher.Dispose() }
    $notifierPattern = 'video_workstation\.cli\s+notify-wecom\s+--instance-id\s+' + [regex]::Escape($notifierInstanceId)
    $notifierProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($pythonPath) -and $_.CommandLine -match $notifierPattern
    })
    if (-not $notifierProcesses) {
        $notifier = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'video_workstation.cli', 'notify-wecom', '--instance-id', $notifierInstanceId) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'wecom.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'wecom.stderr.log')
        $notifier.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'wecom.pid')
    }
}
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
function Assert-NoRunningTask {
    $runningCount = & $pythonPath -m video_workstation.cli active-task-count
    if ($LASTEXITCODE -ne 0 -or [int]$runningCount -gt 0) {
        throw '工作站还有正在运行的镜头，请等待任务完成后再次启动更新后的服务。'
    }
}
$webReady = $false
try {
    $health = Get-LocalHealth
    $webReady = $health.status -eq 'ok' -and $health.thermal_monitor_enabled -eq $true -and $health.runtime_version -eq 5
} catch {}
if (-not $webReady) {
    $webProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($pythonPath) -and $_.CommandLine -match 'uvicorn\s+video_workstation\.main:application'
    })
    if ($webProcesses.Count -gt 0) {
        Assert-NoRunningTask
        foreach ($oldWeb in $webProcesses) { Stop-OwnedTree $oldWeb.ProcessId }
        Start-Sleep -Seconds 1
    }
}
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
if ($workerProcesses) {
    $workerMarker = Join-Path $logDirectory 'worker-runtime.json'
    $workerVerified = $false
    $workerConflict = $false
    if (Test-Path -LiteralPath $workerMarker) {
        try {
            $runtimeInfo = Get-Content -LiteralPath $workerMarker -Raw -Encoding UTF8 | ConvertFrom-Json
            $runtimeProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($runtimeInfo.pid)"
            $workerVerified = $runtimeInfo.thermal_monitor_enabled -eq $true -and $runtimeInfo.runtime_version -eq 5 -and $runtimeProcess.CommandLine -match 'video_workstation\.cli\s+worker'
            if ($workerVerified) {
                $ownedWorkerIds = @($runtimeProcess.ProcessId, $runtimeProcess.ParentProcessId)
                if (@($workerProcesses | Where-Object { $ownedWorkerIds -notcontains $_.ProcessId }).Count -gt 0) {
                    $workerConflict = $true
                }
            }
        } catch {}
    }
    if ($workerConflict) { throw '发现其他 Worker 进程；请检查其数据目录，避免同机重复派发。' }
    if (-not $workerVerified) {
        if (-not $runtimeInfo -or -not $runtimeInfo.pid -or -not (Get-CimInstance Win32_Process -Filter "ProcessId=$($runtimeInfo.pid)")) {
            throw '不能确认旧 Worker 属于当前数据目录；为避免中断其他任务，已停止自动切换。请先排查旧进程。'
        }
        & $pythonPath -m video_workstation.cli stop-idle-worker $runtimeInfo.pid
        if ($LASTEXITCODE -ne 0) { throw '旧 Worker 尚不能安全切换；请等待当前镜头完成后重试。' }
        Start-Sleep -Seconds 1
        $workerProcesses = @()
    }
}
if (-not $workerProcesses) {
    $worker = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'video_workstation.cli', 'worker') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'worker.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'worker.stderr.log')
    $worker.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'worker.pid')
}
Write-Output '工作站：http://127.0.0.1:8000；首次使用在 /setup 建立工作账号。'
Write-Output '模型引擎：http://127.0.0.1:8188；需要单独保持 ComfyUI 运行。'
Write-Output '温度监测：后台每五秒采样；高温或监测失联时暂停派发新重型任务。'
if ($wecomConfigured) {
    Write-Output '企业微信提醒：已配置，后台通知进程已启动。'
} else {
    Write-Output '企业微信提醒：尚未配置；运行 scripts/configure_wecom_alerts.ps1 可启用。'
}
} finally {
    if ($startupLock) { $startupMutex.ReleaseMutex() }
    $startupMutex.Dispose()
}
