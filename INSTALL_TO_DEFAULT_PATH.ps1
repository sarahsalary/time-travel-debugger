$ErrorActionPreference = "Stop"
$Source = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$Destination = 'D:\Github_Projects\time-travel-debugger'
if ($Source.TrimEnd('\') -ieq $Destination.TrimEnd('\')) {
    Write-Host "Project files are already located at $Destination"
    Write-Host 'Next: powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1'
    exit 0
}
if (-not (Test-Path -LiteralPath $Destination)) {
    New-Item -Path $Destination -ItemType Directory -Force | Out-Null
}
# Never overwrite a nonempty project; an existing empty folder is safe.
$Existing = @(Get-ChildItem -LiteralPath $Destination -Force)
if ($Existing.Count -gt 0) {
    throw "The destination is not empty: $Destination. No files were overwritten."
}
Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
}
Write-Host "Complete project copied to $Destination" -ForegroundColor Green
Write-Host "Next: powershell -ExecutionPolicy Bypass -File `"$Destination\scripts\install_windows.ps1`""
