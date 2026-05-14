"""
Persistent local world model for the PC Assistant.

This module keeps a small SQLite-backed understanding of the user's machine:
- launchable applications and aliases
- recently seen files
- lightweight refresh metadata

It lets the assistant ground speech against the actual computer instead of
guessing from a handful of hardcoded names every time.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
import uuid
from typing import Any, Iterable

from dotenv import load_dotenv

try:
    import psutil
except Exception:  # pragma: no cover - optional at import time
    psutil = None

try:
    import winreg
except Exception:  # pragma: no cover - Windows only
    winreg = None

_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BACKEND_DIR.parent
load_dotenv(_BACKEND_DIR / ".env")

_DATA_DIR = _BACKEND_DIR / "data"
_DB_PATH = _DATA_DIR / "world_model.sqlite3"
_LOCK = threading.Lock()
_WARM_THREAD: threading.Thread | None = None

_APP_REFRESH_TTL_S = 900.0
_FILE_REFRESH_TTL_S = 420.0
_SEARCH_APP_LIMIT = 900
_SEARCH_FILE_LIMIT = 1600
_FILE_SEARCH_TIMEOUT_S = 2.5
_FILE_SEARCH_MAX_VISITED = 4500

_APP_SOURCE_PRIORITY = {
    "alias": 1.0,
    "running_process": 0.96,
    "start_app": 0.95,         # Get-StartApps - covers Microsoft Store apps with AUMIDs
    "registry": 0.94,
    "windows_apps": 0.9,
    "start_menu": 0.88,
    "search_root": 0.78,
}

_FILE_SOURCE_PRIORITY = {
    "query_scan": 1.0,
    "everything": 0.98,
    "recent_index": 0.86,
}

_NOISY_APP_NAMES = {
    "python",
    "python3",
    "pythonw",
    "conhost",
    "cmd",
    "powershell",
    "pwsh",
    "git",
    "esbuild",
    "node",
}

_SKIP_DIR_NAMES = {
    "$recycle.bin",
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "cache",
    "logs",
    "node_modules",
    "temp",
    "tmp",
}


def initialize() -> None:
    """Create the SQLite database and schema if needed."""
    with _LOCK:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS apps (
                    path TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    compact_name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_apps_normalized_name ON apps(normalized_name);
                CREATE INDEX IF NOT EXISTS idx_apps_compact_name ON apps(compact_name);

                CREATE TABLE IF NOT EXISTS aliases (
                    alias TEXT PRIMARY KEY,
                    normalized_alias TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_aliases_normalized_alias ON aliases(normalized_alias);

                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    parent TEXT NOT NULL,
                    modified_at REAL NOT NULL,
                    size INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    indexed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_files_normalized_name ON files(normalized_name);
                CREATE INDEX IF NOT EXISTS idx_files_modified_at ON files(modified_at);

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_memory (
                    id TEXT PRIMARY KEY,
                    fact TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    importance REAL DEFAULT 1.0
                );
                """
            )
            conn.commit()
        finally:
            conn.close()


def warm_world_model_async(force: bool = False) -> None:
    """Warm the local app and file indexes in a background thread."""
    initialize()

    global _WARM_THREAD
    with _LOCK:
        if _WARM_THREAD and _WARM_THREAD.is_alive():
            return
        _WARM_THREAD = threading.Thread(
            target=refresh_world_model,
            kwargs={"force": force},
            name="pc-assistant-world-model",
            daemon=True,
        )
        _WARM_THREAD.start()


def refresh_world_model(force: bool = False) -> dict[str, int]:
    """Refresh both the app catalog and the lightweight file index."""
    app_count = refresh_app_catalog(force=force)
    file_count = seed_recent_files(force=force)
    return {"apps": app_count, "files": file_count}


def get_status() -> dict[str, Any]:
    """Return health and size information about the local world model."""
    initialize()
    with _connect() as conn:
        app_count = conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
        alias_count = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return {
        "db_path": str(_DB_PATH),
        "exists": _DB_PATH.exists(),
        "apps_indexed": int(app_count),
        "aliases_indexed": int(alias_count),
        "files_indexed": int(file_count),
        "last_app_refresh": _read_meta("last_app_refresh"),
        "last_file_refresh": _read_meta("last_file_refresh"),
    }


def refresh_app_catalog(force: bool = False) -> int:
    """Discover launchable apps across common Windows sources and index them."""
    initialize()
    last_refresh = float(_read_meta("last_app_refresh") or 0.0)
    if not force and time.time() - last_refresh < _APP_REFRESH_TTL_S:
        with _connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0])

    discovered: dict[str, dict[str, str]] = {}
    for display_name, path, source in _iter_discovered_apps():
        raw_path = str(path or "")
        if raw_path.lower().startswith(("shell:", "ms-")):
            # AUMID / protocol URIs require literal backslashes - never slashify.
            normalized_path = raw_path
        else:
            normalized_path = str(Path(raw_path)).replace("\\", "/")
        existing = discovered.get(normalized_path)
        if existing and _APP_SOURCE_PRIORITY.get(existing["source"], 0.5) >= _APP_SOURCE_PRIORITY.get(source, 0.5):
            continue
        discovered[normalized_path] = {
            "display_name": display_name,
            "source": source,
        }

    now = time.time()
    rows = [
        (
            path,
            record["display_name"],
            _normalize_text(record["display_name"]),
            _compact_text(record["display_name"]),
            record["source"],
            now,
        )
        for path, record in discovered.items()
    ]

    with _connect() as conn:
        conn.execute("DELETE FROM apps")
        conn.executemany(
            """
            INSERT INTO apps (
                path, display_name, normalized_name, compact_name, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    _write_meta("last_app_refresh", str(now))
    return len(rows)


def seed_recent_files(force: bool = False) -> int:
    """Index a lightweight set of recent files from configured user folders."""
    initialize()
    last_refresh = float(_read_meta("last_file_refresh") or 0.0)
    if not force and time.time() - last_refresh < _FILE_REFRESH_TTL_S:
        with _connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    indexed = 0
    recent_files: list[Path] = []
    for root in _iter_recent_roots():
        recent_files.extend(_collect_recent_files(root, per_root_limit=220))

    seen_paths: set[str] = set()
    for file_path in sorted(recent_files, key=_mtime_or_zero, reverse=True):
        normalized_path = str(file_path.resolve()).replace("\\", "/")
        if normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        if index_file_path(file_path, source="recent_index"):
            indexed += 1

    _write_meta("last_file_refresh", str(time.time()))
    return indexed


def get_known_app_names(limit: int = 80) -> list[str]:
    """Return app names from the local index for prompt biasing and UI hints."""
    initialize()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT display_name, source
            FROM apps
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (max(1, limit * 2),),
        ).fetchall()

    unique_names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row["display_name"]).strip()
        key = _normalize_text(name)
        if not name or key in seen:
            continue
        seen.add(key)
        unique_names.append(name)
        if len(unique_names) >= limit:
            break
    return unique_names


def resolve_app(query: str, refresh_if_needed: bool = True) -> dict[str, Any] | None:
    """Resolve a spoken app query to the best known local application record."""
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return None

    alias_match = _lookup_alias(normalized_query)
    if alias_match:
        return alias_match

    candidates = _load_app_candidates()
    best = _score_best_app_match(normalized_query, candidates)
    if best or not refresh_if_needed:
        return best

    refresh_app_catalog(force=True)
    candidates = _load_app_candidates()
    return _score_best_app_match(normalized_query, candidates)


def suggest_apps(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return the top likely app matches for a query."""
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    ranked = _rank_app_candidates(normalized_query, _load_app_candidates())
    return ranked[: max(1, limit)]


def remember_app_alias(alias: str, app_record: dict[str, Any]) -> None:
    """Persist a successful spoken alias so future launches become effortless."""
    normalized_alias = _normalize_text(alias)
    if not normalized_alias or not app_record or not app_record.get("path"):
        return

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO aliases (alias, normalized_alias, display_name, path, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(normalized_alias) DO UPDATE SET
                alias=excluded.alias,
                display_name=excluded.display_name,
                path=excluded.path,
                created_at=excluded.created_at
            """,
            (
                alias.strip(),
                normalized_alias,
                str(app_record.get("display_name") or alias).strip(),
                str(app_record["path"]),
                time.time(),
            ),
        )
        conn.commit()


def index_file_path(file_path: str | Path, source: str = "query_scan") -> bool:
    """Insert or update one file in the local file index."""
    try:
        path = Path(file_path).resolve()
        if not path.is_file():
            return False
        stat = path.stat()
    except OSError:
        return False

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO files (path, name, stem, normalized_name, parent, modified_at, size, source, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name=excluded.name,
                stem=excluded.stem,
                normalized_name=excluded.normalized_name,
                parent=excluded.parent,
                modified_at=excluded.modified_at,
                size=excluded.size,
                source=excluded.source,
                indexed_at=excluded.indexed_at
            """,
            (
                str(path).replace("\\", "/"),
                path.name,
                path.stem,
                _normalize_text(path.name),
                str(path.parent).replace("\\", "/"),
                float(stat.st_mtime),
                int(stat.st_size),
                source,
                time.time(),
            ),
        )
        conn.commit()
    return True


def search_files(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search indexed files first, then fall back to local scanning when needed."""
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    seed_recent_files(force=False)
    candidates = [
        candidate
        for candidate in _query_indexed_files(normalized_query, limit=max(limit * 4, 40))
        if _is_in_allowed_roots(Path(str(candidate["path"])))
    ]

    if len(candidates) < limit:
        for found_path in _iter_query_file_matches(normalized_query, limit=max(limit * 5, 40)):
            if index_file_path(found_path, source="query_scan"):
                candidates.append(_build_file_record(Path(found_path), source="query_scan"))

    ranked = _rank_file_candidates(normalized_query, candidates)
    deduped: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for candidate in ranked:
        if not _is_in_allowed_roots(Path(str(candidate["path"]))):
            continue
        path = str(candidate["path"]).lower()
        if path in seen_paths:
            continue
        seen_paths.add(path)
        deduped.append(candidate)
        if len(deduped) >= limit:
            break
    return deduped


def _connect() -> sqlite3.Connection:
    initialize()
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _read_meta(key: str) -> str | None:
    initialize()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _write_meta(key: str, value: str) -> None:
    initialize()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()

def store_memory(fact: str, importance: float = 1.0) -> None:
    """Store a persistent piece of knowledge about the user context."""
    initialize()
    cleaned_fact = re.sub(r"\s+", " ", str(fact or "").strip()).strip(" .")
    if not cleaned_fact:
        return

    now = time.time()
    with _connect() as conn:
        existing = conn.execute(
            """
            SELECT id, importance
            FROM user_memory
            WHERE LOWER(fact) = LOWER(?)
            """,
            (cleaned_fact,),
        ).fetchone()

        if existing is not None:
            conn.execute(
                """
                UPDATE user_memory
                SET fact = ?, last_accessed_at = ?, importance = ?
                WHERE id = ?
                """,
                (
                    cleaned_fact,
                    now,
                    max(float(existing["importance"] or 0.0), float(importance)),
                    str(existing["id"]),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO user_memory (id, fact, created_at, last_accessed_at, importance)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), cleaned_fact, now, now, importance),
            )
        conn.commit()


def search_memories(query: str, limit: int = 5) -> list[str]:
    """Retrieve the most relevant stored memories for a query."""
    initialize()
    now = time.time()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, fact, importance, last_accessed_at
            FROM user_memory
            ORDER BY importance DESC, last_accessed_at DESC
            LIMIT ?
            """,
            (max(limit * 8, 24),),
        ).fetchall()

    normalized_query = _normalize_text(query)
    compact_query = _compact_text(query)
    query_tokens = _memory_tokens(normalized_query)
    ranked: list[tuple[float, str, str]] = []

    for row in rows:
        row_id = str(row["id"])
        fact = str(row["fact"])
        normalized_fact = _normalize_text(fact)
        compact_fact = _compact_text(fact)
        fact_tokens = _memory_tokens(normalized_fact)
        score = float(row["importance"] or 1.0) * 0.45

        if not normalized_query:
            score += 0.2
        else:
            if normalized_fact == normalized_query:
                score += 2.0
            elif normalized_query in normalized_fact:
                score += 1.1
            elif compact_query and compact_query in compact_fact:
                score += 1.0

            if query_tokens and fact_tokens:
                overlap = len(query_tokens & fact_tokens)
                score += overlap * 0.35

            score += difflib.SequenceMatcher(None, compact_query, compact_fact).ratio() * 0.55

        recency_hours = max(0.0, (now - float(row["last_accessed_at"] or now)) / 3600.0)
        score += max(0.0, 0.12 - min(recency_hours / 300.0, 0.12))
        ranked.append((score, row_id, fact))

    ranked.sort(key=lambda item: item[0], reverse=True)

    selected = ranked[: max(1, limit)]
    if selected:
        with _connect() as conn:
            conn.executemany(
                "UPDATE user_memory SET last_accessed_at = ? WHERE id = ?",
                [(now, row_id) for _, row_id, _ in selected],
            )
            conn.commit()

    return [fact for _, _, fact in selected]


def _iter_discovered_apps() -> Iterable[tuple[str, str, str]]:
    seen_paths: set[str] = set()
    for iterator in (
        _iter_start_apps_aumid(),
        _iter_registry_apps(),
        _iter_windows_apps(),
        _iter_start_menu_apps(),
        _iter_running_apps(),
        _iter_search_root_apps(),
    ):
        for display_name, path, source in iterator:
            raw_path = str(path or "")
            if raw_path.lower().startswith("shell:"):
                # AUMID launch URI - keep verbatim, do not run through Path.resolve().
                resolved_path = raw_path
            else:
                try:
                    resolved_path = str(Path(raw_path).resolve()).replace("\\", "/")
                except OSError:
                    resolved_path = raw_path.replace("\\", "/")

            if resolved_path.lower() in seen_paths or _should_skip_app(display_name, resolved_path):
                continue

            seen_paths.add(resolved_path.lower())
            yield display_name, resolved_path, source


def _iter_start_apps_aumid() -> Iterable[tuple[str, str, str]]:
    """
    Enumerate Start Menu apps via PowerShell's Get-StartApps and yield them as
    launchable candidates using the shell:AppsFolder\\<AUMID> URI scheme.

    This is the only reliable way to discover Microsoft Store / UWP / MSIX
    apps (Spotify, WhatsApp, Notepad, etc.) without admin permissions on
    C:\\Program Files\\WindowsApps.
    """
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return

    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command",
                "Get-StartApps | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return

    stdout = (completed.stdout or "").strip()
    if not stdout:
        return

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return

    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("Name") or "").strip()
        app_id = str(entry.get("AppID") or "").strip()
        if not name or not app_id:
            continue
        # AppID examples:
        #   "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"      (Store / MSIX)
        #   "Chrome"                                           (legacy shortcut)
        #   "{6D809377-6AF0-444B-8957-A3773F02200E}\\Foo.lnk"  (filesystem)
        # All three forms are launchable via shell:AppsFolder\<AppID>.
        yield name, f"shell:AppsFolder\\{app_id}", "start_app"


def _iter_registry_apps() -> Iterable[tuple[str, str, str]]:
    if winreg is None:
        return []

    hives = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
    key_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
    ]

    for hive in hives:
        for key_path in key_paths:
            try:
                root_key = winreg.OpenKey(hive, key_path)
            except OSError:
                continue

            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root_key, index)
                except OSError:
                    break
                index += 1

                try:
                    subkey = winreg.OpenKey(root_key, subkey_name)
                    raw_path = winreg.QueryValue(subkey, None)
                except OSError:
                    continue

                expanded_path = os.path.expandvars(str(raw_path)).strip()
                if expanded_path and Path(expanded_path).exists():
                    yield Path(subkey_name).stem, expanded_path, "registry"


def _iter_windows_apps() -> Iterable[tuple[str, str, str]]:
    root = Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps"))
    if not root.exists():
        return []
    for candidate in root.glob("*.exe"):
        yield candidate.stem, str(candidate), "windows_apps"


def _iter_start_menu_apps() -> Iterable[tuple[str, str, str]]:
    roots = [
        Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")),
        Path(os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs")),
    ]
    extensions = {".lnk", ".url", ".appref-ms", ".exe"}
    for root in roots:
        if not root.exists():
            continue
        for current_root, dir_names, file_names in os.walk(root):
            dir_names[:] = [name for name in dir_names if name.lower() not in _SKIP_DIR_NAMES]
            for file_name in file_names:
                candidate = Path(current_root) / file_name
                if candidate.suffix.lower() not in extensions:
                    continue
                yield candidate.stem, str(candidate), "start_menu"


def _iter_running_apps() -> Iterable[tuple[str, str, str]]:
    if psutil is None:
        return []

    for process in psutil.process_iter(["name", "exe"]):
        try:
            name = str(process.info.get("name") or "").strip()
            path = str(process.info.get("exe") or "").strip()
        except (OSError, psutil.Error, ValueError):
            continue

        if not name or not path:
            continue
        yield Path(name).stem, path, "running_process"


def _iter_search_root_apps() -> Iterable[tuple[str, str, str]]:
    count = 0
    for root in _iter_app_search_roots():
        for current_root, dir_names, file_names in os.walk(root):
            dir_names[:] = [name for name in dir_names if name.lower() not in _SKIP_DIR_NAMES]
            for file_name in file_names:
                if count >= _SEARCH_APP_LIMIT:
                    return
                if not file_name.lower().endswith(".exe"):
                    continue
                full_path = str(Path(current_root) / file_name)
                yield Path(file_name).stem, full_path, "search_root"
                count += 1


def _iter_app_search_roots() -> list[Path]:
    configured = os.getenv(
        "APP_SEARCH_PATHS",
        r"%LOCALAPPDATA%/Programs;C:/Program Files;C:/Program Files (x86)",
    ).split(";")
    defaults = [
        r"%LOCALAPPDATA%\Microsoft\WindowsApps",
        r"%LOCALAPPDATA%\Programs",
        r"%PROGRAMFILES%",
        r"%PROGRAMFILES(X86)%",
    ]

    roots: list[Path] = []
    seen: set[str] = set()
    for raw_path in [*configured, *defaults]:
        expanded = os.path.expandvars(raw_path).strip().strip('"')
        if not expanded:
            continue
        candidate = Path(expanded)
        normalized = str(candidate).replace("\\", "/").lower()
        if normalized in seen or not candidate.exists():
            continue
        seen.add(normalized)
        roots.append(candidate)
    return roots


def _lookup_alias(normalized_query: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT alias, display_name, path
            FROM aliases
            WHERE normalized_alias = ?
            """,
            (normalized_query,),
        ).fetchone()
    if row is None:
        return None
    return {
        "display_name": str(row["display_name"]),
        "path": str(row["path"]),
        "source": "alias",
        "score": 1.0,
        "alias": str(row["alias"]),
    }


def _load_app_candidates() -> list[dict[str, Any]]:
    initialize()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT display_name, path, normalized_name, compact_name, source, updated_at
            FROM apps
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _score_best_app_match(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = _rank_app_candidates(query, candidates)
    if not ranked:
        return None
    return ranked[0] if ranked[0]["score"] >= 0.71 else None


def _rank_app_candidates(query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact_query = _compact_text(query)
    query_soundex = _soundex(compact_query)
    ranked: list[dict[str, Any]] = []

    for candidate in candidates:
        normalized_name = str(candidate.get("normalized_name") or "")
        compact_name = str(candidate.get("compact_name") or "")
        score = difflib.SequenceMatcher(None, compact_query, compact_name).ratio()

        if normalized_name == query or compact_name == compact_query:
            score += 0.5
        elif normalized_name.startswith(query) or compact_name.startswith(compact_query):
            score += 0.2
        elif query in normalized_name:
            score += 0.12

        if query_soundex and query_soundex == _soundex(compact_name):
            score += 0.18

        score += _APP_SOURCE_PRIORITY.get(str(candidate.get("source")), 0.55) * 0.12
        ranked.append(
            {
                "display_name": str(candidate["display_name"]),
                "path": str(candidate["path"]),
                "source": str(candidate["source"]),
                "score": round(score, 4),
            }
        )

    ranked.sort(key=lambda item: (item["score"], item["display_name"]), reverse=True)
    return ranked


def _query_indexed_files(query: str, limit: int) -> list[dict[str, Any]]:
    like_value = f"%{query}%"
    compact_value = f"%{_compact_text(query)}%"
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT path, name, stem, parent, modified_at, size, source
            FROM files
            WHERE normalized_name LIKE ? OR REPLACE(normalized_name, ' ', '') LIKE ?
            ORDER BY modified_at DESC
            LIMIT ?
            """,
            (like_value, compact_value, max(1, limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def _iter_query_file_matches(query: str, limit: int) -> Iterable[Path]:
    everything = shutil.which("es.exe")
    yielded = 0

    if everything:
        try:
            result = subprocess.run(
                [everything, query, "-n", str(limit)],
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            for line in result.stdout.splitlines():
                if yielded >= limit:
                    return
                candidate = Path(line.strip())
                if candidate.is_file() and _is_in_allowed_roots(candidate):
                    yielded += 1
                    yield candidate
            if yielded >= limit:
                return
        except Exception:
            pass

    compact_query = _compact_text(query)
    deadline = time.monotonic() + float(os.getenv("FILE_SEARCH_TIMEOUT_S", _FILE_SEARCH_TIMEOUT_S))
    max_visited = int(os.getenv("FILE_SEARCH_MAX_VISITED", str(_FILE_SEARCH_MAX_VISITED)))
    visited_files = 0

    for root in _iter_file_search_roots():
        for current_root, dir_names, file_names in os.walk(root):
            if time.monotonic() >= deadline or visited_files >= max_visited:
                return
            dir_names[:] = [name for name in dir_names if name.lower() not in _SKIP_DIR_NAMES]
            for file_name in file_names:
                visited_files += 1
                if yielded >= min(limit, _SEARCH_FILE_LIMIT):
                    return
                if time.monotonic() >= deadline or visited_files >= max_visited:
                    return
                candidate = Path(current_root) / file_name
                normalized_name = _normalize_text(file_name)
                if query in normalized_name or compact_query in _compact_text(file_name):
                    yielded += 1
                    yield candidate


def _rank_file_candidates(query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact_query = _compact_text(query)
    ranked: list[dict[str, Any]] = []
    now = time.time()

    for candidate in candidates:
        name = str(candidate.get("name") or Path(str(candidate["path"])).name)
        normalized_name = _normalize_text(name)
        compact_name = _compact_text(name)
        score = difflib.SequenceMatcher(None, compact_query, compact_name).ratio()

        if normalized_name == query:
            score += 0.45
        elif query in normalized_name or compact_query in compact_name:
            score += 0.18

        modified_at = float(candidate.get("modified_at") or 0.0)
        age_hours = max(0.0, (now - modified_at) / 3600.0)
        recency_bonus = max(0.0, 0.15 - min(age_hours / 300.0, 0.15))
        source_bonus = _FILE_SOURCE_PRIORITY.get(str(candidate.get("source")), 0.5) * 0.08

        ranked.append(
            {
                "path": str(candidate["path"]),
                "name": name,
                "parent": str(candidate.get("parent") or Path(str(candidate["path"])).parent),
                "modified_at": modified_at,
                "size": int(candidate.get("size") or 0),
                "source": str(candidate.get("source") or "query_scan"),
                "score": round(score + recency_bonus + source_bonus, 4),
            }
        )

    ranked.sort(key=lambda item: (item["score"], item["modified_at"]), reverse=True)
    return ranked


def _iter_allowed_roots() -> list[Path]:
    raw_paths = os.getenv("ALLOWED_PATHS", "").split(",")
    roots: list[Path] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        expanded = os.path.expandvars(raw_path).strip().strip('"')
        if not expanded:
            continue
        candidate = Path(expanded).resolve()
        normalized = str(candidate).replace("\\", "/").lower()
        if normalized in seen or not candidate.exists():
            continue
        seen.add(normalized)
        roots.append(candidate)
    return roots


def _iter_file_search_roots() -> list[Path]:
    """Return allowed scan roots ordered from specific/demo-relevant to broad."""
    allowed_roots = _iter_allowed_roots()
    roots: list[Path] = []
    seen: set[str] = set()

    def add_root(candidate: Path) -> None:
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        normalized = os.path.normcase(os.path.realpath(str(resolved)))
        if normalized in seen or not resolved.is_dir():
            return
        if not _is_path_under_roots(resolved, allowed_roots):
            return
        seen.add(normalized)
        roots.append(resolved)

    add_root(_PROJECT_ROOT)
    add_root(Path.cwd())

    for root in sorted(allowed_roots, key=lambda item: len(item.parts), reverse=True):
        add_root(root)

    return roots


def _iter_recent_roots() -> list[Path]:
    raw_value = os.getenv("RECENT_FILES_DIRS", "").strip()
    if not raw_value:
        return _iter_allowed_roots()

    roots: list[Path] = []
    seen: set[str] = set()
    for raw_path in raw_value.split(","):
        expanded = os.path.expandvars(raw_path).strip().strip('"')
        if not expanded:
            continue
        candidate = Path(expanded)
        normalized = str(candidate).replace("\\", "/").lower()
        if normalized in seen or not candidate.exists():
            continue
        seen.add(normalized)
        roots.append(candidate)
    return roots or _iter_allowed_roots()


def _collect_recent_files(root: Path, per_root_limit: int) -> list[Path]:
    files: list[Path] = []
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name.lower() not in _SKIP_DIR_NAMES]
        for file_name in file_names:
            candidate = Path(current_root) / file_name
            if candidate.suffix.lower() in {".tmp", ".log", ".cache"}:
                continue
            files.append(candidate)
            if len(files) >= per_root_limit:
                return sorted(files, key=_mtime_or_zero, reverse=True)
    return sorted(files, key=_mtime_or_zero, reverse=True)


def _build_file_record(path: Path, source: str) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        stat = None
    return {
        "path": str(path.resolve()).replace("\\", "/"),
        "name": path.name,
        "stem": path.stem,
        "parent": str(path.parent.resolve()).replace("\\", "/"),
        "modified_at": float(stat.st_mtime) if stat else 0.0,
        "size": int(stat.st_size) if stat else 0,
        "source": source,
    }


def _is_in_allowed_roots(path: Path) -> bool:
    return _is_path_under_roots(path, _iter_allowed_roots())


def _is_path_under_roots(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = os.path.normcase(os.path.realpath(str(path)))
    except OSError:
        return False

    for root in roots:
        try:
            normalized_root = os.path.normcase(os.path.realpath(str(root)))
            if os.path.commonpath([resolved, normalized_root]) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _should_skip_app(display_name: str, path: str) -> bool:
    name = _normalize_text(display_name)
    if not name or name in _NOISY_APP_NAMES:
        return True
    lowered_path = path.replace("\\", "/").lower()
    return any(segment in lowered_path for segment in ("/node_modules/", "/git/", "/python", "/sdk/"))


def _normalize_text(value: str) -> str:
    cleaned = re.sub(r"\.exe$", "", str(value or "").strip().lower())
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return " ".join(part for part in cleaned.split() if part)


def _compact_text(value: str) -> str:
    return _normalize_text(value).replace(" ", "")


def _memory_tokens(value: str) -> set[str]:
    return {token for token in _normalize_text(value).split() if len(token) >= 3}


def _soundex(value: str) -> str:
    cleaned = re.sub(r"[^a-z]", "", str(value or "").lower())
    if not cleaned:
        return ""

    mapping = {
        **{letter: "1" for letter in "bfpv"},
        **{letter: "2" for letter in "cgjkqsxz"},
        **{letter: "3" for letter in "dt"},
        "l": "4",
        **{letter: "5" for letter in "mn"},
        "r": "6",
    }

    first_letter = cleaned[0].upper()
    digits: list[str] = []
    previous = mapping.get(cleaned[0], "")
    for letter in cleaned[1:]:
        current = mapping.get(letter, "")
        if current and current != previous:
            digits.append(current)
        previous = current
    return (first_letter + "".join(digits) + "000")[:4]


def _mtime_or_zero(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


__all__ = [
    "get_known_app_names",
    "get_status",
    "index_file_path",
    "initialize",
    "refresh_app_catalog",
    "refresh_world_model",
    "remember_app_alias",
    "resolve_app",
    "search_files",
    "search_memories",
    "seed_recent_files",
    "store_memory",
    "suggest_apps",
    "warm_world_model_async",
]
