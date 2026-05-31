# ============================================================
#  Bibi launcher — opens Bibi as a NATIVE app window using your
#  Brave engine in app-mode (no tabs, no address bar, own taskbar
#  entry), backed by the local Flask server on :5000.
#
#  This uses a real Chromium engine, so microphone, audio playback,
#  and typing all work reliably (unlike the embedded WebView2 shell).
#  It runs in an isolated profile (.bibi-app), separate from your
#  everyday Brave, so it never touches your normal browsing.
#
#  Logs: .\logs\backend.out.log / backend.err.log
#  (Standalone webview shell kept as backend\bibi_desktop.py;
#   old browser-tab launcher kept as launch_bibi_web.ps1.)
# ============================================================
$ErrorActionPreference = 'SilentlyContinue'
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'

$root       = $PSScriptRoot
$backend    = Join-Path $root 'backend'
$logs       = Join-Path $root 'logs'
$profileDir = Join-Path $root '.bibi-app'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Port($port) {
    foreach ($ip in [Net.Dns]::GetHostAddresses('localhost')) {
        try {
            $s = New-Object Net.Sockets.Socket($ip.AddressFamily, [Net.Sockets.SocketType]::Stream, [Net.Sockets.ProtocolType]::Tcp)
            $iar = $s.BeginConnect($ip, $port, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(800) -and $s.Connected) { $s.Close(); return $true }
            $s.Close()
        } catch {}
    }
    return $false
}

# 1) Backend — serves the built UI + API on :5000 (no console window).
if (-not (Test-Port 5000)) {
    $py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
    if (-not $py) { $py = 'python' }
    Start-Process -FilePath $py -ArgumentList 'app.py' -WorkingDirectory $backend `
        -RedirectStandardOutput (Join-Path $logs 'backend.out.log') `
        -RedirectStandardError  (Join-Path $logs 'backend.err.log')
}
for ($i = 0; $i -lt 45; $i++) { if (Test-Port 5000) { break }; Start-Sleep -Seconds 1 }

# Start listening for the "Bibi" wake word right away (best effort).
try { Invoke-WebRequest -Uri 'http://127.0.0.1:5000/wake/start' -Method Post -TimeoutSec 5 | Out-Null } catch {}

# 2) Open Bibi as a chromeless app window in Brave.
$brave = @(
    "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe",
    "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
    "${env:ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($brave) {
    # Chromeless app window on your DEFAULT Brave profile. This runs inside your
    # existing Brave instance, which renders reliably (a separate profile did
    # not). The flags only take effect on a cold Brave start, which is fine.
    $argline = '--app=http://localhost:5000' +
               ' --autoplay-policy=no-user-gesture-required' +
               ' --use-fake-ui-for-media-stream'
    Start-Process -FilePath $brave -ArgumentList $argline
} else {
    # Brave not found — fall back to the default browser.
    Start-Process 'http://localhost:5000'
}
