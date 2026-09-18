@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0kline-control.ps1" stop
exit /b %errorlevel%
