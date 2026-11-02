# Incremental upgrade from an extracted v1.1.0 package.
# Preserves .venv, .git, .env, data files and user sessions.
$ErrorActionPreference = "Stop"
$Source = $PSScriptRoot
$Destination = 'D:\Github_Projects\time-travel-debugger'
if (-not (Test-Path $Destination)) {
    throw "Target folder does not exist: $Destination. Run INSTALL_TO_DEFAULT_PATH.ps1 instead."
}
if ((Resolve-Path $Source).Path.TrimEnd('\') -ieq (Resolve-Path $Destination).Path.TrimEnd('\')) {
    Write-Host "Already using the upgraded source folder. No files copied."
    exit 0
}
$Timestamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$Backup = Join-Path $Destination "backups\rca-update-$Timestamp"
$Files = @(
    'src\ttd\rca.py',
    'src\ttd\rca_html.py',
    'src\ttd\cli.py',
    'src\ttd\__init__.py',
    'pyproject.toml',
    'README.md',
    'docs\ROOT_CAUSE_ANALYSIS_FA.md',
    'docs\QUICKSTART_FA.md',
    'examples\incident-rca.jsonl',
    'tests\test_rca.py',
    'examples\incident-report-demo.html',
    'examples\incident-report-demo.json',
    'scripts\run_rca_demo.bat',
    'CHANGELOG.md'
)
# Validate ALL sources before touching the destination.
foreach ($Relative in $Files) {
    if (-not (Test-Path (Join-Path $Source $Relative))) {
        throw "Missing upgrade source file: $Relative. No files modified."
    }
}
foreach ($Relative in $Files) {
    $From = Join-Path $Source $Relative
    $To = Join-Path $Destination $Relative
    if (Test-Path $To) {
        $BackupFile = Join-Path $Backup $Relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $BackupFile) | Out-Null
        Copy-Item -LiteralPath $To -Destination $BackupFile -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $To) | Out-Null
    Copy-Item -LiteralPath $From -Destination $To -Force
    Write-Host "Updated: $Relative"
}
Write-Host "Update copied into $Destination" -ForegroundColor Green
Write-Host "Existing files backed up (if any) under $Backup"
Write-Host "Now reinstall metadata and run tests:"
Write-Host "  cd $Destination"
Write-Host '  .\.venv\Scripts\python.exe -m pip install -e ".[dev]"'
Write-Host '  .\.venv\Scripts\python.exe -m pytest'
