@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0kline-control.ps1" restart
exit /b %errorlevel%
