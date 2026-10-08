[CmdletBinding()]
param([switch]$Once)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) { throw "缺少 .venv，请先运行 scripts\setup.ps1" }
$env:PYTHONPATH = "src"
$arguments = @("-m", "video_workstation.cli", "worker")
if ($Once) { $arguments += "--once" }
& ".\.venv\Scripts\python.exe" @arguments
