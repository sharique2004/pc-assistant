"""
profile_store.py — what Bibi knows about YOU.

A small, editable store of personal facts (your name, email, your own profile
URLs, common contacts) so that "open my LinkedIn", "email my manager", etc. use
YOUR real values instead of Bibi guessing/hallucinating a URL.

Backed by a JSON file (pc-assistant/data/bibi_profile.json) you can hand-edit,
or teach Bibi by voice ("remember my LinkedIn is …").  Thread-safe; never
raises into callers.

Python 3.11+
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PATH = Path(os.getenv(
    "BIBI_PROFILE_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "bibi_profile.json"),
))

# Seeded with facts the user has explicitly told Bibi. Edit freely, or teach by
# voice. "sites" maps a spoken site name -> the user's OWN URL on that site.
_DEFAULTS = {
    "name": "",
    "email": "",
    "sites": {},         # spoken site name -> YOUR own URL, e.g. "linkedin": "https://www.linkedin.com/in/you/"
    "contacts": {},      # name -> email, for "email <name>"
    "notes": {},         # freeform key -> value you teach Bibi by voice
}

# Map spoken aliases to canonical profile keys for teaching/lookup.
_KEY_ALIASES = {
    "linkedin": ("sites", "linkedin"),
    "linked in": ("sites", "linkedin"),
    "github": ("sites", "github"),
    "git hub": ("sites", "github"),
    "twitter": ("sites", "twitter"),
    "x": ("sites", "x"),
    "instagram": ("sites", "instagram"),
    "insta": ("sites", "instagram"),
    "portfolio": ("sites", "portfolio"),
    "website": ("sites", "website"),
    "email": ("email", None),
    "name": ("name", None),
}


def _load() -> dict:
    try:
        if _PATH.exists():
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Merge defaults so new keys appear without wiping user edits.
                merged = json.loads(json.dumps(_DEFAULTS))
                merged.update(data)
                for sub in ("sites", "contacts", "notes"):
                    base = dict(_DEFAULTS.get(sub, {}))
                    base.update(data.get(sub, {}) if isinstance(data.get(sub), dict) else {})
                    merged[sub] = base
                return merged
    except Exception as exc:  # noqa: BLE001
        logger.debug("profile load failed: %s", exc)
    return json.loads(json.dumps(_DEFAULTS))


def _save(data: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("profile save failed: %s", exc)


def get_profile() -> dict:
    with _LOCK:
        return _load()


def remember(key: str, value: str) -> str:
    """Teach Bibi a personal fact.  Returns a short spoken confirmation."""
    key = str(key or "").strip().lower()
    value = str(value or "").strip()
    if not key or not value:
        return "I need both what to remember and its value."
    with _LOCK:
        data = _load()
        target = _KEY_ALIASES.get(key)
        if target:
            section, sub = target
            if sub is None:
                data[section] = value
            else:
                data.setdefault(section, {})[sub] = value
        else:
            # Unknown key → store under notes.
            data.setdefault("notes", {})[key] = value
        _save(data)
    return f"Got it — I'll remember your {key}."


def as_context() -> str:
    """Compact one-block summary for the planner prompt."""
    data = get_profile()
    lines = []
    if data.get("name"):
        lines.append(f"name: {data['name']}")
    if data.get("email"):
        lines.append(f"email: {data['email']}")
    for site, url in (data.get("sites") or {}).items():
        if url:
            lines.append(f"my {site}: {url}")
    for person, email in (data.get("contacts") or {}).items():
        if email:
            lines.append(f"contact {person}: {email}")
    for k, v in (data.get("notes") or {}).items():
        if v:
            lines.append(f"{k}: {v}")
    return "; ".join(lines) if lines else "(nothing saved yet)"


def lookup_site(name: str) -> str:
    data = get_profile()
    return str((data.get("sites") or {}).get(str(name or "").strip().lower(), "")).strip()
