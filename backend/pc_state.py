"""
pc_state.py — Host machine state module.

PLACEHOLDER — implement this module (assigned to the Firebase Studio / Gemini agent).

═══════════════════════════════════════════════════════════════
OVERVIEW
═══════════════════════════════════════════════════════════════
This module provides a single public function, get_state(), that returns a
live snapshot of the Windows host machine.  It is called by the
GET /system-state endpoint in app.py and by executor.system_query().

═══════════════════════════════════════════════════════════════
HOW TO USE PSUTIL  (CPU, memory, disk, running processes)
═══════════════════════════════════════════════════════════════
    import psutil

    # CPU — interval=1 blocks for 1 second to get a meaningful measurement.
    cpu_percent = psutil.cpu_percent(interval=1)

    # Memory
    mem = psutil.virtual_memory()
    memory = {
        "total_gb":  round(mem.total   / 1024**3, 1),
        "used_gb":   round(mem.used    / 1024**3, 1),
        "percent":   mem.percent,
    }

    # Disk — use DISK_PATH env var (default "C:/")
    disk_path = os.getenv("DISK_PATH", "C:/")
    d = psutil.disk_usage(disk_path)
    disk = {
        "total_gb": round(d.total / 1024**3, 1),
        "used_gb":  round(d.used  / 1024**3, 1),
        "percent":  d.percent,
    }

    # Running processes — deduplicate by name, filter noise
    _SYSTEM_PROC_BLOCKLIST = {
        "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
        "services.exe", "lsass.exe", "svchost.exe", "dwm.exe",
        "conhost.exe", "fontdrvhost.exe", "spoolsv.exe",
    }
    running_apps = sorted({
        p.info["name"].lower()
        for p in psutil.process_iter(["name"])
        if p.info["name"] and p.info["name"].lower() not in _SYSTEM_PROC_BLOCKLIST
    })

═══════════════════════════════════════════════════════════════
HOW TO GET THE ACTIVE WINDOW  (pygetwindow)
═══════════════════════════════════════════════════════════════
    import pygetwindow as gw

    title = gw.getActiveWindowTitle()
    # Returns None if no window is focused; handle gracefully:
    active_window = title if title else "Unknown"

═══════════════════════════════════════════════════════════════
HOW TO GET RECENT FILES  (pathlib)
═══════════════════════════════════════════════════════════════
    from pathlib import Path
    import os

    raw_dirs = os.getenv(
        "RECENT_FILES_DIRS",
        f"{Path.home()}/Desktop,{Path.home()}/Documents,{Path.home()}/Downloads"
    )
    dirs  = [d.strip() for d in raw_dirs.split(",") if d.strip()]
    limit = int(os.getenv("RECENT_FILES_LIMIT", "10"))

    # Collect all files with their mtime, sort newest-first, take limit.
    all_files = []
    for dir_path in dirs:
        try:
            for p in Path(dir_path).iterdir():
                if p.is_file():
                    all_files.append((p.stat().st_mtime, p.name))
        except (PermissionError, FileNotFoundError):
            continue  # skip unreadable directories

    all_files.sort(key=lambda x: x[0], reverse=True)
    recent_files = [name for _, name in all_files[:limit]]

═══════════════════════════════════════════════════════════════
REQUIRED ENVIRONMENT VARIABLES
═══════════════════════════════════════════════════════════════
    DISK_PATH          — drive/path for disk usage report (default: C:/)
    RECENT_FILES_DIRS  — comma-separated dirs to scan for recent files
    RECENT_FILES_LIMIT — max recent files to return (default: 10)

DEPENDENCIES (all in requirements.txt):
    psutil>=5.9.8, pygetwindow>=0.0.9
"""

import os
import datetime
from dotenv import load_dotenv

load_dotenv()

# Read once at module level; used in get_state() and _get_disk().
_DISK_PATH: str = os.getenv("DISK_PATH", "C:/")

# Implementers: call datetime.datetime.utcnow().isoformat() inside get_state()
# to populate the "timestamp" field.  This reference keeps the import live.
_DATETIME_MODULE = datetime.datetime


# ═══════════════════════════════════════════════════════════════
# Public interface
# ═══════════════════════════════════════════════════════════════

def get_state() -> dict:
    """
    Return a live snapshot of the host machine's current state.
    """
    return {
        "active_window": _get_active_window(),
        "running_apps":  _get_running_apps(),
        "recent_files":  _get_recent_files(),
        "cpu_percent":   _get_cpu(),
        "memory":        _get_memory(),
        "disk":          _get_disk(),
        "timestamp":     _DATETIME_MODULE.utcnow().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
# Private helper stubs
# ═══════════════════════════════════════════════════════════════

def _get_cpu() -> float:
    """
    Return current CPU utilisation as a float 0–100.
    """
    import psutil
    return psutil.cpu_percent(interval=1)


def _get_memory() -> dict:
    """
    Return RAM usage information.
    """
    import psutil
    mem = psutil.virtual_memory()
    return {
        "total_gb": round(mem.total / 1024**3, 1),
        "used_gb":  round(mem.used  / 1024**3, 1),
        "percent":  mem.percent,
    }


def _get_disk() -> dict:
    """
    Return disk usage for the drive specified by DISK_PATH env var.
    """
    import psutil
    d = psutil.disk_usage(_DISK_PATH)
    return {
        "total_gb": round(d.total / 1024**3, 1),
        "used_gb":  round(d.used  / 1024**3, 1),
        "percent":  d.percent,
    }


def _get_active_window() -> str:
    """
    Return the title of the currently focused foreground window.
    """
    try:
        import pygetwindow as gw
        title = gw.getActiveWindowTitle()
        return title if title else "Unknown"
    except Exception:
        return "Unknown"


def _get_running_apps() -> list:
    """
    Return a sorted, deduplicated list of running user-process names.
    """
    import psutil
    blocklist = {
        "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
        "services.exe", "lsass.exe", "svchost.exe", "dwm.exe",
        "conhost.exe", "fontdrvhost.exe", "spoolsv.exe",
    }
    try:
        names = {
            p.info["name"].lower()
            for p in psutil.process_iter(["name"])
            if p.info["name"]
               and p.info["name"].lower() not in blocklist
               and p.info["name"].lower().endswith(".exe")
        }
        return sorted(names)
    except Exception:
        return []


def _get_recent_files() -> list:
    """
    Return names of the most recently modified files across RECENT_FILES_DIRS.
    """
    from pathlib import Path
    import os

    raw_dirs = os.getenv(
        "RECENT_FILES_DIRS",
        f"{Path.home()}/Desktop,{Path.home()}/Documents,{Path.home()}/Downloads"
    )
    dirs  = [d.strip() for d in raw_dirs.split(",") if d.strip()]
    limit = int(os.getenv("RECENT_FILES_LIMIT", "10"))

    all_files = []
    for dir_path in dirs:
        try:
            for p in Path(dir_path).iterdir():
                if p.is_file():
                    all_files.append((p.stat().st_mtime, p.name))
        except (PermissionError, FileNotFoundError):
            continue  # skip unreadable directories

    all_files.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in all_files[:limit]]
