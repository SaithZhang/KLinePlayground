@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run scripts\setup_galaxy.ps1 first.
    exit /b 1
)
".venv\Scripts\python.exe" main_enhanced.py
exit /b %errorlevel%
