[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonPath = if ($pythonCommand) { $pythonCommand.Source } else { $null }
if (-not $pythonPath) {
    $bundled = "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $bundled) { $pythonPath = (Get-Item -LiteralPath $bundled).FullName }
}
if (-not $pythonPath) { throw "未找到 Python 3.11+。请先在 WSL2 或 Windows 安装 Python。" }

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    & $pythonPath -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "创建 .venv 失败" }
}
& ".\.venv\Scripts\python.exe" -m pip install "setuptools>=75" wheel
if ($LASTEXITCODE -ne 0) { throw "安装构建工具失败" }
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]" --no-build-isolation
if ($LASTEXITCODE -ne 0) { throw "安装项目依赖失败" }
Write-Host "项目虚拟环境已准备。下一步设置 VIDEO_WORKSTATION_DATA_DIR 和 VIDEO_WORKSTATION_SESSION_SECRET。"
