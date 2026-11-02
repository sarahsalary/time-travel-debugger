@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\ttd.exe" (
  echo Install first: powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
  exit /b 1
)
.venv\Scripts\ttd.exe trace examples\demo.jsonl --at 2024-10-26T14:30:01Z
.venv\Scripts\ttd.exe anomalies examples\demo.jsonl
.venv\Scripts\ttd.exe report examples\demo.jsonl --output demo-report.html
endlocal
