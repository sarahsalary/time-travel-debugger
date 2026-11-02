@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\ttd.exe" (
    echo TTD is not installed yet. Run scripts\install_windows.ps1 first.
    exit /b 2
)
".venv\Scripts\ttd.exe" incidents "examples\incident-rca.jsonl"
".venv\Scripts\ttd.exe" investigate "examples\incident-rca.jsonl" --error-index 2 --minutes 10 --output "incident-demo.html" --json-output "incident-demo.json"
if errorlevel 1 exit /b %errorlevel%
start "" "incident-demo.html"
endlocal
