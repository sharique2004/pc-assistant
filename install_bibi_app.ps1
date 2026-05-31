# Installs "Bibi" as a real Windows app: Start-Menu + Desktop shortcuts to the
# packaged Bibi.exe (native Electron app, orb icon embedded). Safe to re-run.
$root = $PSScriptRoot
$exe  = Join-Path $root 'Bibi\Bibi.exe'

if (-not (Test-Path $exe)) { Write-Output "Bibi.exe not found at $exe"; exit 1 }

$ws = New-Object -ComObject WScript.Shell
$links = @(
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Bibi.lnk'),   # Start Menu (searchable, pinnable)
    (Join-Path ([Environment]::GetFolderPath('Desktop'))  'Bibi.lnk')    # Desktop
)
foreach ($lnk in $links) {
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath       = $exe
    $s.WorkingDirectory = (Split-Path $exe)
    $s.IconLocation     = "$exe,0"
    $s.Description       = 'Bibi - your voice assistant'
    $s.Save()
    Write-Output "created: $lnk"
}

# Retire the old, confusing shortcuts (Brave launcher / python webview).
foreach ($old in @('Start Bibi.lnk')) {
    foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
        $p = Join-Path $dir $old
        if (Test-Path $p) { Remove-Item $p -Force; Write-Output "removed old: $p" }
    }
}
Write-Output "Done. 'Bibi' is now in your Start Menu and on your Desktop with the orb icon."
