# ============================================================
#  Bibi launcher  —  double-click "Start Bibi" on the desktop.
#  Starts the backend (Flask + wake word) and the web UI, waits
#  for them to come up, then opens Bibi in your browser.
#  Runs hidden (no terminal window). Logs go to .\logs\.
# ============================================================
$ErrorActionPreference = 'SilentlyContinue'

$root     = $PSScriptRoot
$backend  = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$logs     = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Port($port) {
    # Flask binds IPv4 (127.0.0.1); Vite binds IPv6 localhost (::1). A default
    # TcpClient opens an IPv4-only socket, so probe each resolved address with a
    # socket of the matching family (covers both stacks), 1s timeout each.
    foreach ($ip in [Net.Dns]::GetHostAddresses('localhost')) {
        try {
            $s = New-Object Net.Sockets.Socket($ip.AddressFamily, [Net.Sockets.SocketType]::Stream, [Net.Sockets.ProtocolType]::Tcp)
            $iar = $s.BeginConnect($ip, $port, $null, $null)
            $ok = $iar.AsyncWaitHandle.WaitOne(1000)
            if ($ok -and $s.Connected) { $s.Close(); return $true }
            $s.Close()
        } catch {}
    }
    return $false
}

# --- 1. Backend (port 5000) -------------------------------------------------
if (-not (Test-Port 5000)) {
    $blog = Join-Path $logs 'backend.log'
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList "/c set PYTHONUTF8=1&& set PYTHONIOENCODING=utf-8&& python app.py > `"$blog`" 2>&1" `
        -WorkingDirectory $backend -WindowStyle Hidden
}

# --- 2. Frontend / web UI (port 5173) --------------------------------------
if (-not (Test-Port 5173)) {
    $flog = Join-Path $logs 'frontend.log'
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList "/c npm run dev > `"$flog`" 2>&1" `
        -WorkingDirectory $frontend -WindowStyle Hidden
}

# --- 3. Wait for the UI to be ready (up to ~45s) ----------------------------
$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    if (Test-Port 5173) { $ready = $true; break }
    Start-Sleep -Seconds 1
}

# Give the dev server a beat to finish its first compile, then open it.
if ($ready) { Start-Sleep -Seconds 2 }
Start-Process 'http://localhost:5173'
