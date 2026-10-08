[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $bundled = "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $bundled) { $python = Get-Item -LiteralPath $bundled }
}
if (-not $python) { throw "未找到 Python 3.11+。请先在 WSL2 或 Windows 安装 Python。" }

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    & $python.Source -m venv .venv
}
& ".\.venv\Scripts\python.exe" -m pip install "setuptools>=75" wheel
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]" --no-build-isolation
Write-Host "项目虚拟环境已准备。下一步设置 VIDEO_WORKSTATION_DATA_DIR 和 VIDEO_WORKSTATION_SESSION_SECRET。"
