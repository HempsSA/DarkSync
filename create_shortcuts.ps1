$ErrorActionPreference = "Stop"

# Find Python
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "[X] Python not found in PATH." -ForegroundColor Red
    exit 1
}

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")

Write-Host ""
Write-Host "  Python:    $python"
Write-Host "  Project:   $projectDir"
Write-Host "  Desktop:   $desktop"
Write-Host ""

# Create shortcut for DarkSync 2.0
$ws = New-Object -ComObject WScript.Shell
$sc1 = $ws.CreateShortcut("$desktop\DarkSync 2.0.lnk")
$sc1.TargetPath = $python
$sc1.Arguments = "`"$projectDir\DarkSync 2.0.py`""
$sc1.WorkingDirectory = $projectDir
$sc1.IconLocation = "$projectDir\icon_main.ico,0"
$sc1.Description = "DarkSync 2.0 Multi-Job Edition"
$sc1.Save()
Write-Host "  [OK] DarkSync 2.0.lnk"

# Create shortcut for DarkSync Desktop
$sc2 = $ws.CreateShortcut("$desktop\DarkSync Desktop.lnk")
$sc2.TargetPath = $python
$sc2.Arguments = "`"$projectDir\darksync_desktop.py`""
$sc2.WorkingDirectory = $projectDir
$sc2.IconLocation = "$projectDir\icon_desktop.ico,0"
$sc2.Description = "DarkSync Desktop Edition"
$sc2.Save()
Write-Host "  [OK] DarkSync Desktop.lnk"

Write-Host ""
Write-Host "  Done! Shortcuts created on your Desktop."
Write-Host ""
