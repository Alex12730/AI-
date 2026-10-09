[CmdletBinding()]
param([switch]$StartNow)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_scheduled.ps1"
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $runner)) {
    throw "缺少计划任务运行脚本: $runner"
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

foreach ($mode in @("web", "worker")) {
    $suffix = if ($mode -eq "web") { "Web" } else { "Worker" }
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Mode $mode" `
        -WorkingDirectory $projectRoot
    Register-ScheduledTask `
        -TaskName "AI-Video-Workstation-$suffix" `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "AI 视频自动化工作站本地 $suffix 服务" `
        -Force | Out-Null
}

if ($StartNow) {
    Start-ScheduledTask -TaskName "AI-Video-Workstation-Web"
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
            if ($health.status -eq "ok") {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) {
        throw "Web 服务未能启动，请检查 data\web-service.stderr.log"
    }
    Start-ScheduledTask -TaskName "AI-Video-Workstation-Worker"
}

Get-ScheduledTask -TaskName "AI-Video-Workstation-*" |
    Select-Object TaskName, State |
    Sort-Object TaskName
