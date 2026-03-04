# Realtime Desktop Shortcut Creator

$WshShell = New-Object -ComObject WScript.Shell
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Realtime.lnk"
$RealtimeRoot = Split-Path -Parent $PSScriptRoot
$RunBat = Join-Path $RealtimeRoot "scripts\run.bat"
$IconPath = Join-Path $RealtimeRoot "assets\realtime.ico"

$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $RunBat
$Shortcut.WorkingDirectory = $RealtimeRoot
$Shortcut.Description = "Realtime - Voice Transcription"
$Shortcut.WindowStyle = 1

if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}

$Shortcut.Save()

Write-Host ""
Write-Host "  Realtime shortcut created on Desktop!" -ForegroundColor Cyan
Write-Host "  Location: $ShortcutPath" -ForegroundColor Gray
Write-Host ""
