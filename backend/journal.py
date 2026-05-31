"""
journal.py — Bibi's Obsidian journal.

Writes a daily Markdown note of everything Bibi did, so it "never forgets what
it has done throughout the day".  The journal folder is an Obsidian vault: open
`pc-assistant/bibi-journal` as a vault in Obsidian and you'll see one note per
day (YYYY-MM-DD.md) with timestamped entries, plus an index README.

Pure stdlib, thread-safe, never raises into the caller.

Python 3.11+
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "bibi-journal"


def _vault_dir() -> Path:
    configured = os.getenv("BIBI_JOURNAL_DIR", "").strip().strip('"')
    return Path(configured) if configured else _DEFAULT_DIR


def _ensure_vault(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    # An Obsidian vault is just a folder; add a friendly index once.
    readme = vault / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Bibi Journal\n\n"
            "This folder is an **Obsidian vault**. Open it in Obsidian "
            "(Open folder as vault) to browse Bibi's daily activity.\n\n"
            "- One note per day: `YYYY-MM-DD.md`\n"
            "- Each line is a timestamped thing Bibi heard, answered, or did.\n",
            encoding="utf-8",
        )


def _day_note(vault: Path, now: datetime) -> Path:
    return vault / f"{now:%Y-%m-%d}.md"


def log_entry(kind: str, summary: str, detail: str = "") -> None:
    """Append one timestamped entry to today's note.

    kind: short tag e.g. "heard", "answered", "task", "done", "error".
    summary: one-line description.
    detail: optional extra (URL, result, etc.).
    """
    try:
        now = datetime.now()
        vault = _vault_dir()
        with _LOCK:
            _ensure_vault(vault)
            note = _day_note(vault, now)
            if not note.exists():
                note.write_text(
                    f"# {now:%A, %B %d, %Y}\n\n"
                    f"Bibi's activity log for {now:%Y-%m-%d}.\n\n",
                    encoding="utf-8",
                )
            line = f"- **{now:%H:%M:%S}** · `{kind}` — {_clean(summary)}"
            if detail:
                line += f"  \n  ↳ {_clean(detail)}"
            with note.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 — journaling must never break Bibi
        logger.debug("journal write failed: %s", exc)


def log_interaction(transcript: str, kind: str, message: str, tasks: list | None = None) -> None:
    """Log a whole interaction: what was heard, how it was handled, the result,
    and each task that ran."""
    try:
        heard = _clean(transcript)
        if heard:
            log_entry("heard", f'"{heard}"')
        if kind == "question":
            log_entry("answered", _clean(message))
        else:
            for t in (tasks or []):
                title = _clean(str(t.get("title", "")))
                status = t.get("status", "")
                detail = _clean(str(t.get("detail", "")))
                mark = {"done": "✓", "error": "✗", "active": "…"}.get(status, "•")
                log_entry("task", f"{mark} {title}", detail)
            if message:
                log_entry("done", _clean(message))
    except Exception as exc:  # noqa: BLE001
        logger.debug("journal interaction failed: %s", exc)


def _clean(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def vault_path() -> str:
    return str(_vault_dir())
