$ErrorActionPreference = "Stop"
$Project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $Project
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw "Python 3.10+ was not found. Install Python and rerun."
    }
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}
& ".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ is required'"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ is required." }
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& ".venv\Scripts\python.exe" -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Project installation failed." }
& ".venv\Scripts\python.exe" -m pytest
if ($LASTEXITCODE -ne 0) { throw "Tests failed. Do not publish until fixed." }
& ".venv\Scripts\rootrewind.exe" --help
if ($LASTEXITCODE -ne 0) { throw "CLI smoke test failed." }
Write-Host "Installation and tests complete in $Project" -ForegroundColor Green
