"""
bibi_desktop.py — Bibi as a real native Windows app.

Starts the Flask backend in-process and opens the UI in a native desktop
window (pywebview + Edge WebView2). No browser tab, no terminal. Microphone
access and audio autoplay are granted up front so the "Bibi" wake word and
Bibi's spoken replies work with zero prompts or clicks.

Run:  python bibi_desktop.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

# Let WebView2 use the real microphone with no permission popup, and allow
# audio to play without a prior user gesture (so Bibi can just talk).
os.environ.setdefault(
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "--use-fake-ui-for-media-stream --autoplay-policy=no-user-gesture-required",
)
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

_HERE = Path(__file__).resolve().parent       # backend/
_ROOT = _HERE.parent                          # pc-assistant/
sys.path.insert(0, str(_HERE))
os.chdir(_HERE)

HOST = os.getenv("FLASK_HOST", "127.0.0.1")
PORT = int(os.getenv("FLASK_PORT", "5000"))
BASE = f"http://{HOST}:{PORT}/"

# Single-instance lock: hold a TCP port for the process lifetime so launching
# Bibi again (double-clicking the desktop button) doesn't open a 2nd window.
# Auto-released when the process exits.
_LOCK_PORT = int(os.getenv("BIBI_LOCK_PORT", "49517"))
_lock_sock = None


def _acquire_single_instance() -> bool:
    global _lock_sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, _LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _lock_sock = sock          # keep a reference so it isn't garbage-collected
    return True


def _port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((HOST, PORT)) == 0


def _run_backend() -> None:
    """Run the existing Flask app in-process (no reloader, no debug)."""
    import app as backend  # triggers the normal Flask setup
    backend.app.run(host=HOST, port=PORT, threaded=True,
                    use_reloader=False, debug=False)


def _ensure_backend() -> bool:
    if _port_open():
        return True
    threading.Thread(target=_run_backend, daemon=True, name="bibi-backend").start()
    for _ in range(80):              # up to ~40s for the first cold import
        if _port_open():
            return True
        time.sleep(0.5)
    return _port_open()


def _start_wake() -> None:
    """Begin listening for the 'Bibi' wake word as soon as the app is up."""
    try:
        import requests
        requests.post(f"{BASE}wake/start", timeout=5)
    except Exception:
        pass


def _focus_existing() -> None:
    """Bring an already-running Bibi window to the foreground."""
    try:
        import pygetwindow as gw
        for win in gw.getWindowsWithTitle("Bibi"):
            try:
                if getattr(win, "isMinimized", False):
                    win.restore()
                win.activate()
                return
            except Exception:
                continue
    except Exception:
        pass


def main() -> None:
    if not _acquire_single_instance():
        print("Bibi is already running — bringing it to the front.", file=sys.stderr)
        _focus_existing()
        return
    if not _ensure_backend():
        print("Bibi backend failed to start — see logs.", file=sys.stderr)
        sys.exit(1)

    threading.Thread(target=_start_wake, daemon=True).start()

    import webview
    window = webview.create_window(
        "Bibi",
        BASE,
        width=1200,
        height=820,
        min_size=(980, 660),
        background_color="#0D0D1C",
    )
    if os.getenv("BIBI_WEBVIEW_SELFTEST"):
        def _on_loaded():
            try:
                window.evaluate_js(
                    "fetch('/tts',{method:'POST',headers:{'Content-Type':'application/json'},"
                    "body:JSON.stringify({text:'webview self test ok'})})"
                )
            except Exception:
                pass
        window.events.loaded += _on_loaded
    webview.start()


if __name__ == "__main__":
    main()
