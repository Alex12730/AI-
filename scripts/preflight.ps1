[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$failures = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) { $failures.Add("缺少项目虚拟环境 .venv") }
if ([string]::IsNullOrWhiteSpace($env:VIDEO_WORKSTATION_SESSION_SECRET) -or $env:VIDEO_WORKSTATION_SESSION_SECRET.Length -lt 24) { $failures.Add("会话密钥未设置或不足 24 位") }
foreach ($tool in @("ffmpeg", "ffprobe")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { $failures.Add("找不到 $tool") }
}
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { $warnings.Add("当前终端找不到 nvidia-smi；WSL2 内需确认 GPU 透传") }

$dataPath = if ($env:VIDEO_WORKSTATION_DATA_DIR) { $env:VIDEO_WORKSTATION_DATA_DIR } else { Join-Path $projectRoot "data" }
$probePath = $dataPath
while (-not (Test-Path -LiteralPath $probePath)) {
    $parent = Split-Path -Parent $probePath
    if (-not $parent -or $parent -eq $probePath) { break }
    $probePath = $parent
}
if (Test-Path -LiteralPath $probePath) {
    $drive = Get-Item -LiteralPath $probePath
    $root = [System.IO.Path]::GetPathRoot($drive.FullName)
    $disk = Get-PSDrive -Name $root.TrimEnd('\').TrimEnd(':') -ErrorAction SilentlyContinue
    if ($disk -and $disk.Free -lt 100GB) { $failures.Add("数据盘剩余空间低于 100GB") }
}

if ($warnings.Count) { $warnings | ForEach-Object { Write-Warning $_ } }
if ($failures.Count) {
    $failures | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host "预检通过：Python 环境、会话密钥、FFmpeg 与磁盘底线可用。"
