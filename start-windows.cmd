@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_portable_web.ps1" %*
set "result=%errorlevel%"
pause
exit /b %result%
