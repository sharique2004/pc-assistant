# ============================================================
#  Stop Bibi  —  closes the Bibi app window and shuts down the
#  backend (:5000). Your everyday Brave and its tabs are NEVER
#  touched, and no stray processes are left behind.
# ============================================================
$ErrorActionPreference = 'SilentlyContinue'

$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }

# 1) Close the Bibi app window(s) FIRST — synchronously, with retry (WM_CLOSE
#    on the window only, so the user's main Brave is unaffected).
if ($py) { & $py (Join-Path $PSScriptRoot 'backend\bibi_stop_window.py') | Out-Null }

# 2) Tell the wake listener to stop (best effort).
try { Invoke-WebRequest -Uri 'http://127.0.0.1:5000/wake/stop' -Method Post -TimeoutSec 3 | Out-Null } catch {}

# 3) Shut the backend down (free :5000).
foreach ($c in (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue)) {
    try { Stop-Process -Id $c.OwningProcess -Force } catch {}
}
