# Creates "Start Bibi" and "Stop Bibi" shortcuts on the Desktop.
# Safe to re-run any time (it overwrites the existing shortcuts).
$root    = $PSScriptRoot
$desktop = [Environment]::GetFolderPath('Desktop')
$ws      = New-Object -ComObject WScript.Shell

# --- Start Bibi ---
$s = $ws.CreateShortcut((Join-Path $desktop 'Start Bibi.lnk'))
$s.TargetPath       = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
$s.Arguments        = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\launch_bibi.ps1`""
$s.WorkingDirectory = $root
$s.IconLocation     = "$root\assets\bibi.ico"
$s.Description       = 'Start Bibi voice assistant (backend + web UI)'
$s.WindowStyle      = 7   # minimized, so no console flashes
$s.Save()

# --- Stop Bibi ---
$s = $ws.CreateShortcut((Join-Path $desktop 'Stop Bibi.lnk'))
$s.TargetPath       = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
$s.Arguments        = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\stop_bibi.ps1`""
$s.WorkingDirectory = $root
$s.IconLocation     = "$root\assets\bibi_stop.ico"
$s.Description       = 'Stop Bibi (frees ports 5000 & 5173; leaves your browser alone)'
$s.WindowStyle      = 7
$s.Save()

Write-Output "Shortcuts created on Desktop: $desktop"
