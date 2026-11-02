@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install_windows.ps1"
if errorlevel 1 exit /b 1
