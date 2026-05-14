"""
executor.py — Windows execution layer.

PLACEHOLDER — implement this module (assigned to the Firebase Studio / Gemini agent).

═══════════════════════════════════════════════════════════════
OVERVIEW
═══════════════════════════════════════════════════════════════
This module is the "hands" of the PC Assistant.  It translates parsed
intents into real Windows OS actions:
  - Launching applications            (open_app)
  - Creating files on disk            (create_file)
  - Generating code via Ollama        (create_app)
  - Searching the filesystem          (search_pc)
  - Answering PC state questions      (system_query)
  - Handling anything else            (general)
  - Executing deferred confirmations  (confirm_operation)

app.py calls these functions through INTENT_ROUTER and never catches their
return values directly — every function MUST return a dict, never raise.

═══════════════════════════════════════════════════════════════
SAFETY RULES  (enforce these in every function)
═══════════════════════════════════════════════════════════════
1. NEVER delete files.
2. ALL file-system writes must be within a path listed in ALLOWED_PATHS.
3. Operations that are irreversible or write outside the workspace must
   return requires_confirmation=True and queue via _queue_operation().
   The frontend will call POST /confirm before the action runs.

ALLOWED_PATHS validation helper — implement at module level:

    from pathlib import Path

    def _is_path_allowed(target_path: str) -> bool:
        \"\"\"Return True if target_path is within one of the ALLOWED_PATHS dirs.\"\"\"
        raw   = os.getenv("ALLOWED_PATHS", "")
        roots = [Path(p.strip()).resolve() for p in raw.split(",") if p.strip()]
        try:
            target = Path(target_path).resolve()
            return any(target.is_relative_to(r) for r in roots)
        except Exception:
            return False

═══════════════════════════════════════════════════════════════
DEFERRED CONFIRMATION PATTERN
═══════════════════════════════════════════════════════════════
Use a module-level dict to hold operations pending user confirmation.
The frontend receives an operation_id and calls POST /confirm, which
routes to confirm_operation() below.

    import uuid

    _PENDING_OPS: dict = {}   # { operation_id: {"fn": callable, "kwargs": dict} }

    def _queue_operation(fn: callable, kwargs: dict, description: str) -> dict:
        \"\"\"
        Store fn + kwargs and return a requires_confirmation response.
        The frontend will display description and POST /confirm if approved.
        \"\"\"
        op_id = str(uuid.uuid4())
        _PENDING_OPS[op_id] = {"fn": fn, "kwargs": kwargs}
        return {
            "success": False,
            "message": "This action requires your confirmation.",
            "data": {
                "requires_confirmation": True,
                "operation_id":          op_id,
                "description":           description,
            },
        }

═══════════════════════════════════════════════════════════════
HOW TO CALL OLLAMA FOR CODE GENERATION  (used by create_app)
═══════════════════════════════════════════════════════════════
    import requests, os

    host  = os.getenv("OLLAMA_HOST",    "http://localhost:11434")
    model = os.getenv("CODEGEN_MODEL",  "qwen2.5-coder:14b")

    payload = {
        "model":  model,
        "prompt": f"Write a complete application: {description}\\n\\n"
                  "Return only the file contents in this JSON format:\\n"
                  '[{"filename": "main.py", "content": "..."}]',
        "stream": False,
    }
    response = requests.post(f"{host}/api/generate", json=payload, timeout=120)
    response.raise_for_status()
    raw  = response.json()["response"]
    # Parse raw as JSON list of {"filename": str, "content": str}
    # Write each file to WORKSPACE_DIR / <project_slug> /

═══════════════════════════════════════════════════════════════
HOW TO LAUNCH WINDOWS APPLICATIONS  (used by open_app)
═══════════════════════════════════════════════════════════════
    import subprocess, os

    # Option 1 — os.startfile (simplest, uses Windows shell association)
    os.startfile(exe_path)

    # Option 2 — subprocess.Popen (better for CLI apps or when you need PID)
    subprocess.Popen([exe_path], shell=False)

    # Use resolve_app_path(app_name) first to get exe_path.

═══════════════════════════════════════════════════════════════
HOW TO SEARCH FILES  (used by search_pc)
═══════════════════════════════════════════════════════════════
    # Option A — Everything CLI (fast, requires Everything to be installed)
    import subprocess
    result = subprocess.run(
        ["es.exe", query, "-n", "20"],
        capture_output=True, text=True, timeout=10
    )
    files = [l.strip() for l in result.stdout.splitlines() if l.strip()]

    # Option B — pathlib recursive scan (slower, always available)
    from pathlib import Path
    allowed = os.getenv("ALLOWED_PATHS", "").split(",")
    files   = []
    for root in allowed:
        for p in Path(root.strip()).rglob(f"*{query}*"):
            if p.is_file():
                files.append(str(p))

═══════════════════════════════════════════════════════════════
REQUIRED ENVIRONMENT VARIABLES
═══════════════════════════════════════════════════════════════
    OLLAMA_HOST     — http://localhost:11434
    CODEGEN_MODEL   — qwen2.5-coder:14b
    ALLOWED_PATHS   — comma-separated writable directories
    WORKSPACE_DIR   — default directory for new files / projects
    VSCODE_PATH     — path to Code.exe for opening generated projects
    APP_SEARCH_PATHS— semicolon-separated extra dirs to search for .exe files

DEPENDENCIES (all in requirements.txt):
    psutil, pygetwindow, requests, python-dotenv
"""

import os
import uuid
import subprocess
import json
import difflib
import re
import shutil
import threading
import time
import urllib.parse
import webbrowser
import requests
from datetime import datetime
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
import pc_state
import world_model
import window_actions

load_dotenv()
world_model.initialize()
world_model.warm_world_model_async(force=False)

_WORKSPACE_DIR: str = os.getenv("WORKSPACE_DIR", "C:/pc-assistant-workspace")
_PENDING_OPS: dict = {}
_PENDING_OPS_LOCK = threading.Lock()
_PENDING_OP_TTL_S = float(os.getenv("PENDING_OP_TTL_S", "900"))
_PENDING_OP_MAX = int(os.getenv("PENDING_OP_MAX", "100"))
_APP_DISCOVERY_CACHE: dict = {"timestamp": 0.0, "candidates": {}}
_APP_DISCOVERY_TTL_S = 120.0
_DEFAULT_PLANNER_MAX_STEPS = 3
_DEFAULT_PLANNER_TIMEOUT_S = 25.0
_DEFAULT_CODEX_TASK_TIMEOUT_S = 900.0
_CODEX_COMPLEX_APP_MARKERS = {
    "auth",
    "authentication",
    "backend",
    "database",
    "dashboard",
    "full stack",
    "full-stack",
    "multi page",
    "multi-page",
    "next.js",
    "react",
    "sqlite",
    "typescript",
    "vite",
}
_APP_ALIAS_PATHS: dict[str, list[str]] = {
    "chrome": [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ],
    "google chrome": [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ],
    "notepad": ["C:/Windows/System32/notepad.exe"],
    "calculator": ["C:/Windows/System32/calc.exe"],
    "calc": ["C:/Windows/System32/calc.exe"],
    "explorer": ["C:/Windows/explorer.exe"],
    "file explorer": ["C:/Windows/explorer.exe"],
    "terminal": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/wt.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/windowsterminal.exe"),
    ],
    "windows terminal": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/wt.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/windowsterminal.exe"),
    ],
    "vscode": [os.path.expandvars(r"%LOCALAPPDATA%/Programs/Microsoft VS Code/Code.exe")],
    "vs code": [os.path.expandvars(r"%LOCALAPPDATA%/Programs/Microsoft VS Code/Code.exe")],
    "visual studio code": [os.path.expandvars(r"%LOCALAPPDATA%/Programs/Microsoft VS Code/Code.exe")],
    "spotify": [os.path.expandvars(r"%APPDATA%/Spotify/Spotify.exe")],
    "discord": [os.path.expandvars(r"%LOCALAPPDATA%/Discord/Update.exe")],
    "steam": ["C:/Program Files (x86)/Steam/Steam.exe"],
    "vlc": ["C:/Program Files/VideoLAN/VLC/vlc.exe"],
    "firefox": ["C:/Program Files/Mozilla Firefox/firefox.exe"],
    "edge": ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"],
    "microsoft edge": ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"],
    "chatgpt": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/chatgpt.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/chatgpt.exe"),
    ],
    "chat gpt": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/chatgpt.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/chatgpt.exe"),
    ],
    "chat g p t": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/chatgpt.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0/chatgpt.exe"),
    ],
    "claude": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/claude.exe"),
        "C:/Program Files/WindowsApps/Claude_*/app/Claude.exe",
    ],
    "clod": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/claude.exe"),
        "C:/Program Files/WindowsApps/Claude_*/app/Claude.exe",
    ],
    "cloud": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/claude.exe"),
        "C:/Program Files/WindowsApps/Claude_*/app/Claude.exe",
    ],
    "clawed": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/claude.exe"),
        "C:/Program Files/WindowsApps/Claude_*/app/Claude.exe",
    ],
    "anthropic claude": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/claude.exe"),
        "C:/Program Files/WindowsApps/Claude_*/app/Claude.exe",
    ],
    "prism launcher": [
        os.path.expandvars(r"%APPDATA%/Microsoft/Windows/Start Menu/Programs/Prism Launcher.lnk"),
        os.path.expandvars(r"%PROGRAMDATA%/Microsoft/Windows/Start Menu/Programs/Prism Launcher.lnk"),
        os.path.expandvars(r"%LOCALAPPDATA%/Programs/PrismLauncher/Prismlauncher.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Programs/PrismLauncher/prismlauncher.exe"),
    ],
    "prism": [
        os.path.expandvars(r"%APPDATA%/Microsoft/Windows/Start Menu/Programs/Prism Launcher.lnk"),
        os.path.expandvars(r"%PROGRAMDATA%/Microsoft/Windows/Start Menu/Programs/Prism Launcher.lnk"),
        os.path.expandvars(r"%LOCALAPPDATA%/Programs/PrismLauncher/Prismlauncher.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Programs/PrismLauncher/prismlauncher.exe"),
    ],
    "minecraft": [
        os.path.expandvars(r"%APPDATA%/Microsoft/Windows/Start Menu/Programs/Prism Launcher.lnk"),
        os.path.expandvars(r"%PROGRAMDATA%/Microsoft/Windows/Start Menu/Programs/Prism Launcher.lnk"),
        os.path.expandvars(r"%LOCALAPPDATA%/Programs/PrismLauncher/Prismlauncher.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%/Programs/PrismLauncher/prismlauncher.exe"),
    ],
    "whatsapp": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/WhatsApp.exe"),
        os.path.expandvars(r"%APPDATA%/Microsoft/Windows/Start Menu/Programs/WhatsApp.lnk"),
        os.path.expandvars(r"%PROGRAMDATA%/Microsoft/Windows/Start Menu/Programs/WhatsApp.lnk"),
        os.path.expandvars(r"%LOCALAPPDATA%/WhatsApp\WhatsApp.exe"),
    ],
    "whatsapp desktop": [
        os.path.expandvars(r"%LOCALAPPDATA%/Microsoft/WindowsApps/WhatsApp.exe"),
        os.path.expandvars(r"%APPDATA%/Microsoft/Windows/Start Menu/Programs/WhatsApp.lnk"),
        os.path.expandvars(r"%PROGRAMDATA%/Microsoft/Windows/Start Menu/Programs/WhatsApp.lnk"),
        os.path.expandvars(r"%LOCALAPPDATA%/WhatsApp\WhatsApp.exe"),
    ],
}

def _log_activity(message: str):
    log_path = Path("activity.log")
    timestamp = datetime.now().isoformat()
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def _get_env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    try:
        return int(raw_value) if raw_value else int(default)
    except ValueError:
        return int(default)


def _get_env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    try:
        return float(raw_value) if raw_value else float(default)
    except ValueError:
        return float(default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _slugify(value: str, fallback: str = "generated-task", max_length: int = 42) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").lower()).strip("-")
    if not slug:
        slug = fallback
    return slug[:max_length].strip("-") or fallback


def _extract_codex_task(raw_text: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    patterns = [
        r"^(?:please\s+)?(?:use|ask|tell|run)\s+codex\s+(?:to\s+)?(?P<task>.+)$",
        r"^(?:please\s+)?(?:have|let)\s+codex\s+(?P<task>.+)$",
        r"^codex\s+(?P<task>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            task = match.group("task").strip(" .?!")
            return task or None
    return None


def _extract_claude_task(raw_text: str) -> str | None:
    """
    Detect explicit hand-offs to Claude Code, e.g.:
        "use claude to build me a budget app"
        "have claude create a script that renames files"
        "ask claude code to set up a Flask backend"
    """
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    patterns = [
        r"^(?:please\s+)?(?:use|ask|tell|run)\s+claude(?:\s+code)?\s+(?:to\s+)?(?P<task>.+)$",
        r"^(?:please\s+)?(?:have|let)\s+claude(?:\s+code)?\s+(?P<task>.+)$",
        r"^claude(?:\s+code)?\s+(?P<task>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            task = match.group("task").strip(" .?!")
            return task or None
    return None


def _provider_override() -> str:
    """Return user-pinned provider preference from env, or empty string."""
    return (
        os.getenv("APP_BUILDER", "")
        or os.getenv("CODEGEN_PROVIDER", "")
        or ""
    ).strip().lower()


def _codex_cli_available() -> bool:
    try:
        _resolve_codex_executable()
        return True
    except FileNotFoundError:
        return False


def _claude_cli_available() -> bool:
    try:
        _resolve_claude_executable()
        return True
    except FileNotFoundError:
        return False


# Phrases that strongly suggest "small focused script / one-shot CLI" work,
# which Codex tends to handle faster than Claude. Tokens are matched as
# substrings of " {description} " (with bookend spaces) so we don't false-fire
# on substrings inside larger words.
_CODEX_LEAN_MARKERS: tuple[str, ...] = (
    " script ", " scripts ",
    " python script", " bash script", " powershell script", " shell script",
    " one-liner ", " one liner ",
    " cli ", " cli tool", " command line ", " command-line ",
    " utility ", " utilities ",
    " function ",
    " regex ", " snippet ",
)


def _choose_app_provider(description: str) -> str:
    """
    Decide which back-end should generate code for `create_app(description)`.

    Return one of: "claude", "codex", "ollama".

    Rules, in order:
      1. Explicit env override (APP_BUILDER / CODEGEN_PROVIDER) wins.
      2. Explicit "use claude ..." / "use codex ..." in the description wins.
      3. Script-style phrasing routes to Codex when Codex is installed.
      4. Everything else (any "create / build me an application that ...")
         routes to Claude when Claude is installed.
      5. Fall back to the other CLI if the preferred one is missing.
      6. Local Ollama is the last resort - only when neither CLI is available.
    """
    override = _provider_override()
    if override in {"claude", "claude-code"}:
        return "claude"
    if override in {"codex", "codex-cli"}:
        return "codex"
    if override in {"ollama", "local-ollama"}:
        return "ollama"

    claude_available = _claude_cli_available()
    codex_available = _codex_cli_available()
    lowered = f" {str(description or '').lower()} "

    # Explicit hand-off mentions in the description.
    if "claude code" in lowered or re.search(r"\bclaude\b", lowered):
        if claude_available:
            return "claude"
        # Asked for Claude but it's not installed - fall through to next-best.
    if re.search(r"\bcodex\b", lowered):
        if codex_available:
            return "codex"
        # Asked for Codex but it's not installed - fall through to next-best.

    # Script-leaning phrasing prefers Codex.
    if any(marker in lowered for marker in _CODEX_LEAN_MARKERS):
        if codex_available:
            return "codex"
        if claude_available:
            return "claude"
        return "ollama"

    # Default: Claude handles "applications" / "apps" / "websites" / anything
    # that requires multi-file reasoning.  When Claude isn't installed, Codex
    # is the next-best general-purpose option; Ollama is only a fallback when
    # no real CLI is plumbed in.
    if claude_available:
        return "claude"
    if codex_available:
        return "codex"
    return "ollama"


def _should_use_codex_for_app(description: str) -> bool:
    """Backward-compatible wrapper around the unified router."""
    return _choose_app_provider(description) == "codex"


def _should_use_claude_for_app(description: str) -> bool:
    """Backward-compatible wrapper around the unified router."""
    return _choose_app_provider(description) == "claude"


def _resolve_codex_executable() -> str:
    configured = os.getenv("CODEX_CLI_PATH", "").strip().strip('"')
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            return str(configured_path)
        found_configured = shutil.which(configured)
        if found_configured:
            return found_configured

    for candidate in ("codex.cmd", "codex.exe", "codex"):
        found = shutil.which(candidate)
        if found and not found.lower().endswith(".ps1"):
            return found

    raise FileNotFoundError(
        "Could not find codex.cmd or codex.exe on PATH. Install or log in to Codex CLI first."
    )


def _resolve_claude_executable() -> str:
    """
    Locate the Claude Code CLI.

    Prefers the real `claude.exe` over `claude.cmd`: the .cmd wrapper invokes
    cmd.exe, which on Windows can swallow stdin and mangle multi-line argv
    when run from `subprocess.run(...)`. If only the .cmd is found, we try
    one more step and look inside the npm install layout where the .cmd
    points at the real exe.
    """
    configured = os.getenv("CLAUDE_CLI_PATH", "").strip().strip('"')
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            return str(configured_path)
        found_configured = shutil.which(configured)
        if found_configured:
            return found_configured

    # Prefer the real exe so we bypass cmd.exe.
    for candidate in ("claude.exe", "claude.cmd", "claude"):
        found = shutil.which(candidate)
        if not found or found.lower().endswith(".ps1"):
            continue
        if found.lower().endswith(".cmd"):
            # Resolve the npm shim to the actual claude.exe next to it.
            exe_path = (
                Path(found).resolve().parent
                / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            )
            if exe_path.exists():
                return str(exe_path)
        return found

    raise FileNotFoundError(
        "Could not find the Claude Code CLI on PATH. Install it from "
        "https://docs.claude.com/claude-code/install before using Claude routing."
    )


def _tail_text(value: str, limit: int = 4000) -> str:
    text = str(value or "")
    return text[-limit:] if len(text) > limit else text


def _build_claude_prompt(task: str, target_dir: Path, context: str = "") -> str:
    context_block = f"\nExtra context:\n{context.strip()}\n" if str(context or "").strip() else ""
    return (
        "You are running inside a non-interactive Claude Code session that the "
        "PC Assistant has spawned in a fresh, empty project directory.\n\n"
        f"Working directory: {target_dir}\n\n"
        "Task: " + task.strip() + "\n\n"
        "Constraints:\n"
        "- Create whatever files are needed to satisfy the task. Use Write/Edit tools.\n"
        "- Stay inside the working directory; do not touch anything outside it.\n"
        "- Do not run servers or long-lived processes; this is a one-shot generation.\n"
        "- Prefer clear, runnable code over commentary. Include a short README.md with "
        "  setup and run instructions if the project has more than one file.\n"
        f"{context_block}"
        "When you are done, end with one sentence summarizing what you built."
    )


def _build_codex_prompt(task: str, target_dir: Path, context: str = "") -> str:
    context_block = f"\nExtra context:\n{context.strip()}\n" if str(context or "").strip() else ""
    return (
        "You are working as a local coding agent launched by the PC Assistant.\n"
        f"Target workspace: {target_dir}\n"
        "Complete the user's task inside this target workspace. "
        "Create or edit files directly as needed, keep the implementation runnable, "
        "and run lightweight verification when possible. Do not modify files outside "
        "the target workspace unless the task explicitly requires it.\n"
        f"{context_block}\n"
        f"User task: {task.strip()}\n"
    )


def _extract_open_and_search_command(raw_text: str) -> tuple[str, str] | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    # Action verbs that mean "type this into the app's input box":
    # search, look up, google, ask, ask about, find out, tell me about,
    # find me / find, look for. Verbs that take a follow-up object
    # (write/type/paste) are handled by _extract_open_and_type_command instead.
    patterns = [
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+and\s+(?:search(?:\s+up)?|look\s+up|google|ask(?:\s+(?:about|me))?|find(?:\s+(?:out|me))?|tell\s+me\s+about)\s+(?P<query>.+)$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+and\s+search\s+for\s+(?P<query>.+)$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+and\s+look\s+for\s+(?P<query>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            app_name = match.group("app").strip(" .?!")
            query = _clean_search_phrase(match.group("query"))
            if app_name and query:
                return app_name, query
    return None


def _extract_open_and_message_command(raw_text: str) -> tuple[str, str, str] | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    # Recognized phrasings:
    #   "open WhatsApp and search Alex and write hi"
    #   "open WhatsApp and message Alex hi"
    #   "open WhatsApp and tell Alex I am running late"
    #   "open WhatsApp and text Alex hi"
    #   "send Alex a message on WhatsApp saying hi"
    #   "message Alex on WhatsApp that I am running late"
    patterns = [
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+(?:and\s+)?search(?:\s+up)?\s+(?:for\s+)?(?P<contact>.+?)\s+and\s+(?:write|send|message|say|tell|text)\s+(?P<message>.+)$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+(?:and\s+)?message\s+(?P<contact>.+?)\s+(?:that\s+|saying\s+|with\s+the\s+message\s+)?(?P<message>.+)$",
        # "tell ME about ..." / "tell ME how ..." are search queries, not
        # messages - the negative lookahead defers those to the search regex.
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+(?:and\s+)?(?:tell|text)\s+(?!me\b)(?P<contact>[a-z][a-z0-9 .'-]{0,40}?)\s+(?:that\s+|saying\s+)?(?P<message>.+)$",
        r"^(?:please\s+)?send\s+(?P<contact>.+?)\s+(?:a\s+)?message\s+on\s+(?P<app>.+?)\s+(?:that\s+|saying\s+)?(?P<message>.+)$",
        r"^(?:please\s+)?message\s+(?P<contact>.+?)\s+on\s+(?P<app>.+?)\s+(?:that\s+|saying\s+)?(?P<message>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue

        app_name = match.group("app").strip(" .?!")
        contact_name = match.group("contact").strip(" .?!")
        message = match.group("message").strip(" .?!")
        if app_name and contact_name and message:
            return app_name, contact_name, message

    return None


def _clean_search_phrase(value: str) -> str:
    cleaned = " ".join(str(value or "").split()).strip(" .?!")
    cleaned = re.sub(r"^(?:up\s+)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:for\s+)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^(?:what(?:'s| is)\s+the\s+wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:the\s+wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" .?!")


def _is_launch_minecraft_command(raw_text: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(raw_text or "").strip().lower()).strip(" .?!")
    return lowered in {
        "launch minecraft",
        "open minecraft",
        "start minecraft",
        "run minecraft",
    }


def _extract_open_and_type_command(raw_text: str) -> tuple[str, str, str] | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    patterns = [
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+and\s+(?P<verb>type|write|paste|enter)\s+(?P<text>.+)$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<app>.+?)\s+and\s+(?P<verb>search(?:\s+up)?(?:\s+for)?)\s+(?P<text>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        app_name = match.group("app").strip(" .?!")
        verb = match.group("verb").strip().lower()
        text = match.group("text").strip(" .?!")
        if "search" in verb:
            text = _clean_search_phrase(text)
        if app_name and text:
            return app_name, verb, text
    return None


def _extract_window_text_command(raw_text: str) -> tuple[str, str, str] | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    patterns = [
        r"^(?P<verb>type|write|paste|enter|search(?:\s+up)?(?:\s+for)?)\s+(?P<text>.+?)\s+(?:in|into|on)\s+(?P<window>.+)$",
        r"^(?:in|into|on)\s+(?P<window>.+?)\s+(?P<verb>type|write|paste|enter|search(?:\s+up)?(?:\s+for)?)\s+(?P<text>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        window_hint = match.group("window").strip(" .?!")
        verb = match.group("verb").strip().lower()
        text = match.group("text").strip(" .?!")
        if "search" in verb:
            text = _clean_search_phrase(text)
        if window_hint and text:
            return window_hint, verb, text
    return None


def _extract_window_click_command(raw_text: str) -> tuple[str, str] | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    patterns = [
        r"^(?:click)\s+(?:the\s+)?(?P<button>.+?)(?:\s+button)?\s+(?:in|on)\s+(?P<window>.+)$",
        r"^(?:in|on)\s+(?P<window>.+?)\s+(?:click)\s+(?:the\s+)?(?P<button>.+?)(?:\s+button)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        window_hint = match.group("window").strip(" .?!")
        button_text = match.group("button").strip(" .?!")
        if window_hint and button_text:
            return window_hint, button_text
    return None


def _extract_window_shortcut_command(raw_text: str) -> tuple[str, str] | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    patterns = [
        r"^(?:press|hit|use)\s+(?P<keys>.+?)\s+(?:in|on)\s+(?P<window>.+)$",
        r"^(?:in|on)\s+(?P<window>.+?)\s+(?:press|hit|use)\s+(?P<keys>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        window_hint = match.group("window").strip(" .?!")
        keys = match.group("keys").strip(" .?!")
        if window_hint and keys:
            return window_hint, keys
    return None


def automate_window_text(
    window_hint: str,
    text: str,
    *,
    submit: bool = False,
    navigate_search: bool = False,
    select_all: bool = False,
) -> dict:
    try:
        result = window_actions.type_text(
            window_hint,
            text,
            submit=submit,
            navigate_search=navigate_search,
            select_all=select_all,
        )
        result.setdefault("data", {})
        result["data"].setdefault("window_hint", window_hint)
        result["data"].setdefault("typed_text", text)
        result["data"].setdefault("submitted", submit)
        result["data"].setdefault("navigate_search", navigate_search)
        return result
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def automate_open_and_type(
    app_name: str,
    text: str,
    *,
    submit: bool = False,
    navigate_search: bool = False,
    select_all: bool = False,
) -> dict:
    try:
        open_result = open_app(app_name)
        if not open_result.get("success"):
            return open_result

        automation_result = automate_window_text(
            app_name,
            text,
            submit=submit,
            navigate_search=navigate_search,
            select_all=select_all,
        )
        combined_success = bool(open_result.get("success")) and bool(automation_result.get("success"))
        resolved_name = open_result.get("data", {}).get("resolved_app_name") or app_name
        if combined_success:
            action = "searched" if navigate_search else "typed into"
            user_message = f"Opened {resolved_name} and {action} it."
        else:
            user_message = (
                f"I opened {resolved_name}, but I could not finish the in-app action. "
                f"{automation_result.get('message', '')}"
            ).strip()

        return {
            "success": combined_success,
            "message": user_message,
            "data": {
                "app": resolved_name,
                "text": text,
                "open_result": open_result.get("data", {}),
                "automation_result": automation_result.get("data", {}),
                "submitted": submit,
                "navigate_search": navigate_search,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def automate_click_button(window_hint: str, button_text: str) -> dict:
    try:
        return window_actions.click_button(window_hint, button_text)
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def automate_press_keys(window_hint: str, keys: str) -> dict:
    try:
        return window_actions.press_keys(window_hint, keys)
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def automate_app_search(app_name: str, query: str) -> dict:
    try:
        open_result = open_app(app_name)
        if not open_result.get("success"):
            return open_result

        automation_result = window_actions.app_search(app_name, query)
        combined_success = bool(open_result.get("success")) and bool(automation_result.get("success"))
        resolved_name = (
            open_result.get("data", {}).get("resolved_app_name")
            or app_name
        )
        if combined_success:
            message = f"Opened {resolved_name} and submitted your search."
        else:
            message = (
                f"I opened {resolved_name}, but I could not finish the in-app search. "
                f"{automation_result.get('message', '')}"
            ).strip()

        data = {
            "app": resolved_name,
            "query": query,
            "open_result": open_result.get("data", {}),
            "automation_result": automation_result.get("data", {}),
        }
        return {
            "success": combined_success,
            "message": message,
            "data": data,
            "requires_confirmation": False,
        }
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def automate_app_message(app_name: str, contact_name: str, message: str) -> dict:
    try:
        open_result = open_app(app_name)
        if not open_result.get("success"):
            return open_result

        automation_result = window_actions.app_send_message(app_name, contact_name, message)
        resolved_name = open_result.get("data", {}).get("resolved_app_name") or app_name
        automation_data = automation_result.get("data") or {}

        # ---- Recipient verification gate ------------------------------------
        # window_actions.app_send_message refuses to type the body when it
        # cannot confirm the open chat is the intended contact. Two flavours:
        #
        # 1) "mismatch" - search opened the wrong chat. We know which chat is
        #    actually open; queue a confirmation that, when /confirm hits,
        #    types the message body into the currently-focused chat (the one
        #    the user can see and consciously approve).
        #
        # 2) "unreadable" - we couldn't read the chat header at all. Refuse
        #    outright; do NOT register a confirm path. Forcing the user to
        #    retry avoids sending blind.
        # ---------------------------------------------------------------------
        verification = str(automation_data.get("verification") or "")
        if automation_data.get("requires_recipient_confirmation") and verification == "mismatch":
            detected_chat = str(automation_data.get("detected_chat") or "")
            return _queue_operation(
                fn=_force_send_open_chat_message,
                kwargs={
                    "app_name": app_name,
                    "intended_contact": contact_name,
                    "detected_chat": detected_chat,
                    "message": message,
                    "resolved_name": resolved_name,
                },
                description=(
                    f"WhatsApp opened a chat with \"{detected_chat}\" instead of {contact_name}. "
                    "Confirm to send anyway, or cancel."
                ),
            )

        if automation_data.get("requires_recipient_confirmation") and verification == "unreadable":
            return {
                "success": False,
                "message": (
                    f"I opened {resolved_name} but could not confirm which chat is selected, "
                    "so I did not send the message. Open the chat yourself and try again."
                ),
                "data": {
                    "app": resolved_name,
                    "contact_name": contact_name,
                    "message_text": message,
                    "open_result": open_result.get("data", {}),
                    "automation_result": automation_data,
                },
                "requires_confirmation": False,
            }

        combined_success = bool(open_result.get("success")) and bool(automation_result.get("success"))
        if combined_success:
            user_message = f"Opened {resolved_name} and sent your message to {contact_name}."
        else:
            user_message = (
                f"I opened {resolved_name}, but I could not finish sending the message. "
                f"{automation_result.get('message', '')}"
            ).strip()

        return {
            "success": combined_success,
            "message": user_message,
            "data": {
                "app": resolved_name,
                "contact_name": contact_name,
                "message_text": message,
                "open_result": open_result.get("data", {}),
                "automation_result": automation_data,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def _force_send_open_chat_message(
    app_name: str,
    intended_contact: str,
    detected_chat: str,
    message: str,
    resolved_name: str,
) -> dict:
    """
    Called by /confirm after the user has been told the open chat is not the
    intended contact and explicitly approved sending to it anyway.

    Re-focuses the messaging app and types the message body into the chat
    that is currently selected.  Does NOT re-run the search step - the user
    is consenting to the chat as it stood at /command time.
    """
    try:
        result = window_actions.send_open_chat_message(app_name, message)
        if result.get("success"):
            return {
                "success": True,
                "message": (
                    f"Sent your message in {resolved_name} to the currently open chat "
                    f"({detected_chat or 'unknown'}), as confirmed."
                ),
                "data": {
                    "app": resolved_name,
                    "intended_contact": intended_contact,
                    "detected_chat": detected_chat,
                    "message_text": message,
                    "automation_result": result.get("data", {}),
                },
                "requires_confirmation": False,
            }
        return {
            "success": False,
            "message": result.get("message") or f"Could not deliver the message in {resolved_name}.",
            "data": {
                "app": resolved_name,
                "intended_contact": intended_contact,
                "detected_chat": detected_chat,
                "automation_result": result.get("data", {}),
            },
            "requires_confirmation": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def launch_minecraft() -> dict:
    try:
        open_result = open_app("Prism Launcher")
        if not open_result.get("success"):
            return open_result

        click_result = window_actions.click_button("Prism Launcher", "Launch")
        if click_result.get("success"):
            return {
                "success": True,
                "message": "Opened Prism Launcher and clicked Launch.",
                "data": {
                    "launcher": "Prism Launcher",
                    "open_result": open_result.get("data", {}),
                    "automation_result": click_result.get("data", {}),
                },
                "requires_confirmation": False,
            }

        return {
            "success": False,
            "message": (
                "I opened Prism Launcher, but I could not click the Launch button. "
                + str(click_result.get("message", ""))
            ).strip(),
            "data": {
                "launcher": "Prism Launcher",
                "open_result": open_result.get("data", {}),
                "automation_result": click_result.get("data", {}),
            },
            "requires_confirmation": False,
        }
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}


def _handle_direct_general_shortcut(raw_text: str) -> dict[str, Any] | None:
    claude_task = _extract_claude_task(raw_text)
    if claude_task is not None:
        return run_claude_task(claude_task)

    codex_task = _extract_codex_task(raw_text)
    if codex_task is not None:
        return run_codex_task(codex_task)

    open_and_message = _extract_open_and_message_command(raw_text)
    if open_and_message is not None:
        app_name, contact_name, message = open_and_message
        return automate_app_message(app_name, contact_name, message)

    open_and_search = _extract_open_and_search_command(raw_text)
    if open_and_search is not None:
        app_name, query = open_and_search
        return automate_app_search(app_name, query)

    open_and_type = _extract_open_and_type_command(raw_text)
    if open_and_type is not None:
        app_name, verb, text = open_and_type
        return automate_open_and_type(
            app_name,
            text,
            submit=verb == "enter",
            navigate_search="search" in verb,
        )

    window_text = _extract_window_text_command(raw_text)
    if window_text is not None:
        window_hint, verb, text = window_text
        return automate_window_text(
            window_hint,
            text,
            submit=verb == "enter",
            navigate_search="search" in verb,
        )

    window_click = _extract_window_click_command(raw_text)
    if window_click is not None:
        window_hint, button_text = window_click
        return automate_click_button(window_hint, button_text)

    window_shortcut = _extract_window_shortcut_command(raw_text)
    if window_shortcut is not None:
        window_hint, keys = window_shortcut
        return automate_press_keys(window_hint, keys)

    if _is_launch_minecraft_command(raw_text):
        return launch_minecraft()

    return None


def _iter_launch_candidates(app_name: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    def add_candidate(display_name: str, path: str, source: str, score: float = 1.0) -> None:
        raw_path = str(path or "").strip()
        if not raw_path:
            return
        # Preserve backslashes in shell URIs (shell:AppsFolder\<AUMID>) and other
        # protocol launchers - ShellExecute rejects forward-slash forms.
        if raw_path.lower().startswith(("shell:", "ms-")):
            normalized_path = raw_path
        else:
            normalized_path = raw_path.replace("\\", "/")
        lowered_path = normalized_path.lower()
        if lowered_path in seen_paths:
            return
        seen_paths.add(lowered_path)
        candidates.append({
            "display_name": display_name or app_name,
            "path": normalized_path,
            "source": source,
            "score": score,
        })

    app_record = world_model.resolve_app(app_name, refresh_if_needed=True)
    if app_record and app_record.get("path"):
        add_candidate(
            str(app_record.get("display_name") or app_name),
            str(app_record.get("path")),
            str(app_record.get("source") or "world_model"),
            float(app_record.get("score", 1.0) or 1.0),
        )

    try:
        resolved_path = resolve_app_path(app_name)
    except FileNotFoundError:
        resolved_path = ""
    if resolved_path:
        add_candidate(_friendly_app_name(resolved_path, app_name), resolved_path, "resolved_path", 0.95)

    # Only accept suggestions that (a) clear a score floor AND (b) have a real
    # name overlap with the user's query. Without (b), unrelated apps like
    # AppVShNotify can slip past for "Spotify" purely on raw fuzzy ratio.
    suggestion_floor = float(os.getenv("APP_SUGGEST_SCORE_FLOOR", "0.72"))
    suggestions = world_model.suggest_apps(app_name, limit=5)
    for suggestion in suggestions:
        suggestion_path = str(suggestion.get("path") or "").strip()
        if not suggestion_path:
            continue
        suggestion_score = float(suggestion.get("score", 0.0) or 0.0)
        if suggestion_score < suggestion_floor:
            continue
        if not _alias_matches_resolution(
            app_name,
            str(suggestion.get("display_name") or ""),
            suggestion_path,
        ):
            continue
        add_candidate(
            str(suggestion.get("display_name") or app_name),
            suggestion_path,
            str(suggestion.get("source") or "suggestion"),
            suggestion_score,
        )

    return candidates


def _is_launch_uri(path: str) -> bool:
    """Return True if `path` is a shell URI (AUMID, ms-* protocol, etc.)."""
    return str(path or "").lower().startswith(("shell:", "ms-"))


def _alias_matches_resolution(spoken_name: str, display_name: str, exe_path: str) -> bool:
    """
    Heuristic: only persist a learned alias if what we actually launched has a
    real name overlap with what the user said.  Prevents "Open WhatsApp" from
    teaching the world model that "WhatsApp -> unsecapp.exe" when the resolver
    misfires once.
    """
    spoken_compact = _compact_app_name(spoken_name)
    if not spoken_compact:
        return False

    candidates = [
        _compact_app_name(display_name),
        _compact_app_name(Path(exe_path).stem) if exe_path and not _is_launch_uri(exe_path) else "",
        # For shell:AppsFolder\<vendor>.<AppName>_<hash>!Entry, pull <AppName>.
        _compact_app_name(re.split(r"[\\!]", str(exe_path))[-1]) if _is_launch_uri(exe_path) else "",
        _compact_app_name(re.split(r"[\\!.]", str(exe_path))[-2]) if _is_launch_uri(exe_path) and "." in str(exe_path) else "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if spoken_compact in candidate or candidate in spoken_compact:
            return True
        # Allow short typos / pronunciations: ratio >= 0.7 against either side.
        ratio = difflib.SequenceMatcher(None, spoken_compact, candidate).ratio()
        if ratio >= 0.7:
            return True
    return False


def _launchable_target_exists(path: str) -> bool:
    """
    Treat shell URIs and protocol launchers as always-present, since
    os.path.exists() can only answer for filesystem paths.
    """
    if not path:
        return False
    if _is_launch_uri(path):
        return True
    return os.path.exists(path)


def _choose_launch_candidate(app_name: str) -> dict[str, Any] | None:
    candidates = _iter_launch_candidates(app_name)
    if not candidates:
        return None

    scored_candidates: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        path = str(candidate.get("path") or "")
        source = str(candidate.get("source") or "")
        score = float(candidate.get("score", 0.0) or 0.0)
        if _launchable_target_exists(path):
            score += 0.45
        if source == "running_process":
            score += 0.35
        if source == "resolved_path":
            score += 0.2
        if source == "start_app":
            score += 0.25
        scored_candidates.append((score, candidate))

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    return scored_candidates[0][1]


def _launch_app_candidate(app_name: str) -> tuple[str, str]:
    last_error: Exception | None = None
    candidates = _iter_launch_candidates(app_name)
    if not candidates:
        raise FileNotFoundError(f"Could not locate {app_name}")

    ordered_candidates: list[dict[str, Any]] = []
    preferred_candidate = _choose_launch_candidate(app_name)
    if preferred_candidate is not None:
        ordered_candidates.append(preferred_candidate)
    for candidate in candidates:
        if candidate not in ordered_candidates:
            ordered_candidates.append(candidate)

    for candidate in ordered_candidates:
        candidate_path = str(candidate.get("path") or "").strip()
        if not candidate_path:
            continue
        try:
            os.startfile(candidate_path)
            return candidate_path, str(candidate.get("display_name") or app_name)
        except FileNotFoundError as exc:
            last_error = exc
            continue
        except OSError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise FileNotFoundError(str(last_error)) from last_error
    raise FileNotFoundError(f"Could not locate {app_name}")


def _verify_app_launch(exe_path: str, resolved_name: str, timeout_s: float = 4.0) -> bool:
    """
    Best-effort verification that a launched application actually appeared.

    For filesystem paths we look for a running process whose stem matches the
    executable's stem.  For shell URIs (AUMIDs), the resolved process name
    rarely matches the AUMID — we fall back to a contains-check against the
    user-facing resolved_name (e.g. "WhatsApp" matches "WhatsApp.Root.exe").
    """
    deadline = time.time() + timeout_s

    if _is_launch_uri(exe_path):
        normalized_target = _normalize_app_name(resolved_name)
        target_compact = _compact_app_name(resolved_name)
        if not normalized_target:
            return False
        while time.time() < deadline:
            try:
                for name, _ in _iter_running_app_candidates():
                    candidate_compact = _compact_app_name(name)
                    if candidate_compact and target_compact and (
                        candidate_compact == target_compact
                        or candidate_compact.startswith(target_compact)
                        or target_compact in candidate_compact
                    ):
                        return True
            except Exception:
                break
            time.sleep(0.35)
        return False

    normalized_target = _normalize_app_name(Path(exe_path).stem or resolved_name)
    while time.time() < deadline:
        try:
            if any(
                _normalize_app_name(name) == normalized_target
                for name, _ in _iter_running_app_candidates()
            ):
                return True
        except Exception:
            break
        time.sleep(0.35)

    return False


def _format_app_suggestions(suggestions: list[dict]) -> str:
    names = [item.get("display_name", "") for item in suggestions if item.get("display_name")]
    names = [name for index, name in enumerate(names) if name and name not in names[:index]]
    if not names:
        return ""
    preview = ", ".join(names[:3])
    return f" Did you mean {preview}?"


def _summarize_state_for_user(state: dict, query: str) -> tuple[str, dict]:
    lowered_query = str(query or "").lower()
    if "running" in lowered_query or "apps" in lowered_query or "process" in lowered_query:
        running_apps = state.get("running_apps", [])
        preview = ", ".join(str(app).replace(".exe", "") for app in running_apps[:6])
        message = (
            f"I found {len(running_apps)} running apps."
            + (f" Right now I can see {preview}." if preview else "")
        )
        return message, {"running_apps": running_apps}

    if "memory" in lowered_query or "ram" in lowered_query:
        memory = state.get("memory", {})
        return (
            "Memory usage is "
            f"{memory.get('used_gb', 0):.1f} of {memory.get('total_gb', 0):.1f} GB "
            f"({memory.get('percent', 0):.1f}%).",
            {"memory": memory},
        )

    if "cpu" in lowered_query or "processor" in lowered_query:
        cpu_percent = float(state.get("cpu_percent", 0.0))
        return f"CPU usage is currently {cpu_percent:.1f}%.", {"cpu_percent": cpu_percent}

    if "disk" in lowered_query or "storage" in lowered_query:
        disk = state.get("disk", {})
        return (
            "Disk usage on "
            f"{disk.get('path', 'C:/')} is {disk.get('used_gb', 0):.0f} of "
            f"{disk.get('total_gb', 0):.0f} GB ({disk.get('percent', 0):.1f}%).",
            {"disk": disk},
        )

    if "window" in lowered_query or "active" in lowered_query or "focused" in lowered_query:
        active_window = state.get("active_window", "Unknown")
        return f"The active window is {active_window}.", {"active_window": active_window}

    return "Here is the current system state.", state


def _trim_state_for_planner(state: dict) -> dict[str, Any]:
    running_apps = list(state.get("running_apps", []))
    return {
        "active_window": state.get("active_window"),
        "cpu_percent": state.get("cpu_percent"),
        "memory": state.get("memory"),
        "disk": state.get("disk"),
        "running_apps": running_apps[:12],
    }


def _extract_web_search_query(raw_text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip()).strip(" .?!")
    if not cleaned:
        return ""

    cleaned = re.sub(r"^(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?", "", cleaned, flags=re.IGNORECASE)
    lowered = cleaned.lower()
    prefixes = (
        "open google and search ",
        "open google and look up ",
        "open chrome and search ",
        "search up on google ",
        "search on google ",
        "search and google ",
        "search in google ",
        "search google for ",
        "search google ",
        "google search for ",
        "google search ",
        "search the web for ",
        "search the web ",
        "search online for ",
        "search online ",
        "look up on google ",
        "look up ",
        "find on google ",
        "google ",
    )

    for prefix in prefixes:
        if lowered.startswith(prefix):
            query = cleaned[len(prefix):].strip()
            return re.sub(r"^(?:for\s+)", "", query, flags=re.IGNORECASE).strip(" .?!")

    return ""


def _get_relevant_memories(query: str, limit: int = 5) -> list[str]:
    try:
        return world_model.search_memories(query, limit=limit)
    except Exception:
        return []


def _detect_memory_fact(raw_text: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", str(raw_text or "").strip())
    if not cleaned:
        return None

    patterns = [
        r"^(?:please\s+)?remember(?:\s+that|\s+this)?\s+(?P<fact>.+)$",
        r"^(?:for future reference[:,]?\s*)remember(?:\s+that)?\s+(?P<fact>.+)$",
        r"^(?:can you|could you|please)\s+remember(?:\s+that)?\s+(?P<fact>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            fact = re.sub(r"[.?!]+$", "", match.group("fact")).strip()
            return fact or None
    return None


def _is_memory_recall_query(raw_text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(raw_text or "").strip().lower())
    if not normalized:
        return False

    patterns = (
        "what do you remember about me",
        "what do you know about me",
        "what have you remembered about me",
        "tell me what you remember about me",
        "do you remember anything about me",
    )
    return any(pattern in normalized for pattern in patterns)


def _parse_json_response(raw_response: str) -> dict[str, Any] | None:
    text = str(raw_response or "").strip()
    if not text:
        return None
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _call_ollama_generate(host: str, model: str, prompt: str, timeout_s: float = 60.0, expect_json: bool = False) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    if expect_json:
        payload["format"] = "json"

    response = requests.post(f"{host}/api/generate", json=payload, timeout=timeout_s)
    response.raise_for_status()
    return str(response.json().get("response", "")).strip()


def _summarize_tool_observation(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result, dict) else {}
    summary: dict[str, Any] = {
        "success": bool(result.get("success")) if isinstance(result, dict) else False,
        "message": str(result.get("message", "")) if isinstance(result, dict) else "",
        "requires_confirmation": bool(data.get("requires_confirmation")) if isinstance(data, dict) else False,
    }

    if isinstance(data, dict):
        if data.get("resolved_app_name"):
            summary["resolved_app_name"] = data.get("resolved_app_name")
        if data.get("file_path"):
            summary["file_path"] = data.get("file_path")
        if data.get("project_dir"):
            summary["project_dir"] = data.get("project_dir")
        if data.get("count") is not None:
            summary["count"] = data.get("count")
        if data.get("results"):
            summary["results"] = list(data.get("results", []))[:5]
        if data.get("suggestions"):
            summary["suggestions"] = data.get("suggestions")
        if data.get("window_title"):
            summary["window_title"] = data.get("window_title")
        if data.get("button_text"):
            summary["button_text"] = data.get("button_text")
        if data.get("typed_text"):
            summary["typed_text"] = data.get("typed_text")
        if data.get("requested_keys"):
            summary["requested_keys"] = data.get("requested_keys")

    return summary


def _build_planner_prompt(raw_text: str, history: list[dict[str, Any]], step: int, max_steps: int) -> str:
    try:
        state = pc_state.get_state()
    except Exception:
        state = {}

    workspace = os.getenv("WORKSPACE_DIR", _WORKSPACE_DIR)
    recent_apps = world_model.get_known_app_names(limit=24)
    memories = _get_relevant_memories(raw_text, limit=5)
    summarized_state = _trim_state_for_planner(state)
    history_json = json.dumps(history, ensure_ascii=True, indent=2) if history else "[]"
    memories_text = json.dumps(memories, ensure_ascii=True)
    apps_text = ", ".join(recent_apps) if recent_apps else "Unknown"

    return (
        "You are a fully local Windows assistant planner. "
        "Think in terms of grounded local actions, but return only JSON.\n\n"
        f"Current step: {step} of {max_steps}\n"
        f"User request: {raw_text}\n"
        f"Workspace directory: {workspace}\n"
        f"Known apps on this computer: {apps_text}\n"
        f"Relevant long-term memory facts: {memories_text}\n"
        f"Current machine snapshot: {json.dumps(summarized_state, ensure_ascii=True)}\n"
        f"Completed tool history: {history_json}\n\n"
        "Available tools:\n"
        '- open_app: {"app_name": "Claude"}\n'
        '- focus_window: {"window_hint": "current window"}\n'
        '- create_file: {"file_name": "notes", "file_type": "txt", "content": "buy milk"}\n'
        '- create_app: {"description": "a to do list app"}\n'
        '- search_pc: {"query": "resume"}\n'
        '- web_search: {"query": "how to create an AWS policy generator"}\n'
        '- system_query: {"query": "what apps are running right now"}\n'
        '- app_search: {"app_name": "Claude", "query": "what is the best speaker"}\n'
        '- app_send_message: {"app_name": "WhatsApp", "contact_name": "Alex", "message": "I am on my way"}\n'
        '- type_in_window: {"window_hint": "current window", "text": "hello there", "submit": false, "navigate_search": false, "select_all": false}\n'
        '- click_button: {"window_hint": "Prism Launcher", "button_text": "Launch"}\n'
        '- press_keys: {"window_hint": "current window", "keys": "ctrl l"}\n'
        '- codex_task: {"task": "write a python script that renames files", "project_name": "rename script"}\n'
        '- claude_task: {"task": "build a React budget tracker with charts", "project_name": "budget tracker"}\n'
        '- launch_minecraft: {}\n'
        '- remember_fact: {"fact": "I usually work in C:/Users/Sharique Khatri"}\n\n'
        "Rules:\n"
        "- Use tools when needed to complete the task, not just to talk about it.\n"
        "- You may use multiple tool steps across turns.\n"
        "- If a tool already succeeded, do not repeat it unless the observation shows it failed.\n"
        "- Use app_search when the user wants you to open an app and then type or submit a query inside it.\n"
        "- Use app_send_message when the user wants you to open a messaging app, find a contact, and send a message.\n"
        "- Use type_in_window to type into the current or named window when the app is already open.\n"
        "- Use click_button to click visible UI buttons in the current or named window.\n"
        "- Use press_keys for safe shortcuts like ctrl l, ctrl f, enter, tab, or escape.\n"
        "- Use codex_task for fast script-style coding work and CLI utilities, or when the user explicitly says \"use Codex\".\n"
        "- Use claude_task for multi-file projects, UI/frontend work, full-stack apps, or anything needing thoughtful architecture, or when the user says \"use Claude\".\n"
        "- Use launch_minecraft when the user asks to launch Minecraft through Prism Launcher.\n"
        "- When the user refers to the current app/window/tab, use window_hint as \"current window\".\n"
        "- Treat known apps and system state as environment context, not as personal memory facts.\n"
        "- When the user asks what you remember about them, only use the long-term memory facts.\n"
        "- When the task is complete, respond naturally and briefly.\n"
        "- When more detail is required, ask a concise clarification.\n\n"
        "Return exactly one JSON object in one of these shapes:\n"
        '{"type":"tool","tool":"open_app","arguments":{"app_name":"Claude"},"reason":"why this tool helps"}\n'
        '{"type":"tool","tool":"app_search","arguments":{"app_name":"Claude","query":"what is the best speaker"},"reason":"why this tool helps"}\n'
        '{"type":"tool","tool":"app_send_message","arguments":{"app_name":"WhatsApp","contact_name":"Alex","message":"I am on my way"},"reason":"why this tool helps"}\n'
        '{"type":"tool","tool":"type_in_window","arguments":{"window_hint":"current window","text":"compare bose and sony","submit":false},"reason":"why this tool helps"}\n'
        '{"type":"tool","tool":"codex_task","arguments":{"task":"write a python script that renames files in a folder","project_name":"rename script"},"reason":"why a focused script belongs on Codex"}\n'
        '{"type":"tool","tool":"claude_task","arguments":{"task":"build a React budget tracker with charts","project_name":"budget tracker"},"reason":"why a UI app belongs on Claude Code"}\n'
        '{"type":"final","response":"I opened Claude and it is ready.","confidence":0.92}\n'
        '{"type":"clarify","response":"Which file name would you like me to use?","confidence":0.41}\n'
    )


def _execute_planner_tool(tool_name: str, arguments: dict[str, Any], raw_text: str) -> dict[str, Any]:
    tool = str(tool_name or "").strip().lower()
    args = arguments if isinstance(arguments, dict) else {}

    if tool == "open_app":
        return open_app(str(args.get("app_name") or raw_text).strip())
    if tool == "focus_window":
        return window_actions.focus_window(str(args.get("window_hint") or "current window").strip())
    if tool == "create_file":
        return create_file(
            file_name=str(args.get("file_name") or "note").strip(),
            file_type=str(args.get("file_type") or "txt").strip(),
            content=str(args.get("content") or ""),
        )
    if tool == "create_app":
        return create_app(str(args.get("description") or raw_text).strip())
    if tool == "search_pc":
        return search_pc(str(args.get("query") or raw_text).strip())
    if tool == "web_search":
        return web_search(str(args.get("query") or raw_text).strip())
    if tool == "system_query":
        return system_query(str(args.get("query") or raw_text).strip())
    if tool == "app_search":
        return automate_app_search(
            str(args.get("app_name") or "").strip(),
            str(args.get("query") or raw_text).strip(),
        )
    if tool == "app_send_message":
        return automate_app_message(
            str(args.get("app_name") or "").strip(),
            str(args.get("contact_name") or "").strip(),
            str(args.get("message") or raw_text).strip(),
        )
    if tool == "type_in_window":
        return automate_window_text(
            str(args.get("window_hint") or "current window").strip(),
            str(args.get("text") or raw_text).strip(),
            submit=_as_bool(args.get("submit"), default=False),
            navigate_search=_as_bool(args.get("navigate_search"), default=False),
            select_all=_as_bool(args.get("select_all"), default=False),
        )
    if tool == "click_button":
        return automate_click_button(
            str(args.get("window_hint") or "current window").strip(),
            str(args.get("button_text") or "").strip(),
        )
    if tool == "press_keys":
        return automate_press_keys(
            str(args.get("window_hint") or "current window").strip(),
            str(args.get("keys") or "").strip(),
        )
    if tool == "codex_task":
        return run_codex_task(
            str(args.get("task") or raw_text).strip(),
            project_name=str(args.get("project_name") or "").strip(),
            context=str(args.get("context") or "").strip(),
        )
    if tool == "claude_task":
        return run_claude_task(
            str(args.get("task") or raw_text).strip(),
            project_name=str(args.get("project_name") or "").strip(),
            context=str(args.get("context") or "").strip(),
        )
    if tool == "launch_minecraft":
        return launch_minecraft()
    if tool == "remember_fact":
        fact = str(args.get("fact") or "").strip()
        if not fact:
            return {
                "success": False,
                "message": "No fact was provided to remember.",
                "data": {},
                "requires_confirmation": False,
            }
        world_model.store_memory(fact, importance=1.15)
        return {
            "success": True,
            "message": f"I'll remember that {fact}.",
            "data": {"stored_fact": fact},
            "requires_confirmation": False,
        }

    return {
        "success": False,
        "message": f"Unsupported tool: {tool_name}",
        "data": {},
        "requires_confirmation": False,
    }


def _build_general_prompt(raw_text: str) -> str:
    try:
        state = pc_state.get_state()
    except Exception:
        state = {}
    summary, trimmed_state = _summarize_state_for_user(state, raw_text)
    workspace = os.getenv("WORKSPACE_DIR", _WORKSPACE_DIR)
    recent_apps = world_model.get_known_app_names(limit=10)
    memories = _get_relevant_memories(raw_text, limit=4)
    return (
        "You are a local Windows computer assistant. "
        "Respond naturally and briefly, like a polished personal assistant. "
        "Stay grounded in the provided machine context.\n\n"
        f"User request: {raw_text}\n"
        f"Workspace: {workspace}\n"
        f"Known apps: {', '.join(recent_apps) if recent_apps else 'Unknown'}\n"
        f"Relevant memory: {json.dumps(memories, ensure_ascii=True)}\n"
        f"System snapshot summary: {summary}\n"
        f"Structured system snapshot: {json.dumps(trimmed_state, ensure_ascii=True)}\n"
    )

def open_app(app_name: str) -> dict:
    try:
        focus_result = window_actions.focus_window(app_name, timeout_s=0.8)
        if focus_result.get("success"):
            focused_title = str(focus_result.get("data", {}).get("window_title") or app_name)
            return {
                "success": True,
                "message": f"Focused {focused_title}.",
                "data": {
                    "exe_path": "",
                    "resolved_app_name": focused_title,
                    "requested_app_name": app_name,
                    "launch_verified": True,
                    "focused_existing_window": True,
                },
                "requires_confirmation": False,
            }

        exe_path, resolved_name = _launch_app_candidate(app_name)
        _log_activity(f"Launched application: {exe_path}")
        remembered_candidate = _choose_launch_candidate(app_name)
        if remembered_candidate is not None and _alias_matches_resolution(
            app_name,
            remembered_candidate.get("display_name") or resolved_name,
            remembered_candidate.get("path") or exe_path,
        ):
            world_model.remember_app_alias(
                app_name,
                {
                    "display_name": remembered_candidate.get("display_name") or resolved_name,
                    "path": remembered_candidate.get("path") or exe_path,
                    "source": remembered_candidate.get("source") or "alias",
                },
            )
        launch_verified = _verify_app_launch(exe_path, resolved_name)
        return {
            "success": True,
            "message": (
                f"Opened {resolved_name}."
                if launch_verified
                else f"Launched {resolved_name}. It may still be starting."
            ),
            "data": {
                "exe_path": exe_path,
                "resolved_app_name": resolved_name,
                "requested_app_name": app_name,
                "launch_verified": launch_verified,
            },
            "requires_confirmation": False,
        }
    except FileNotFoundError:
        suggestions = world_model.suggest_apps(app_name, limit=5)
        return {
            "success": False,
            "message": f"Could not find {app_name}.{_format_app_suggestions(suggestions)}".strip(),
            "data": {"suggestions": suggestions, "requested_app_name": app_name},
            "requires_confirmation": False,
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}

def create_file(file_name: str, file_type: str, content: str = "") -> dict:
    try:
        if "." in file_name:
            target = Path(_WORKSPACE_DIR) / file_name
        else:
            target = Path(_WORKSPACE_DIR) / f"{file_name}.{file_type}"
        
        target_path = str(target.resolve())
        if not _is_path_allowed(target_path):
            return {"success": False, "message": "Path is outside allowed directories.", "data": {}, "requires_confirmation": False}
        
        def do_create(path, data):
            import os
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            world_model.index_file_path(path, source="query_scan")
            _log_activity(f"Created file: {path}")
            try:
                os.startfile(path)
                _log_activity(f"Launched file: {path}")
            except Exception:
                pass
            return {
                "success": True,
                "message": f"Created {Path(path).name} in your workspace.",
                "data": {"file_path": path},
                "requires_confirmation": False,
            }

        return _queue_operation(
            fn=do_create,
            kwargs={"path": target_path, "data": content},
            description=f"Create {target.name} in {os.path.dirname(target_path)}"
        )
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def _run_claude_interactive(task_text: str, target: Path, extra_context: str) -> dict:
    """
    Launch Claude Code in a NEW visible terminal window with the prompt
    pre-loaded.  The Flask backend returns immediately; the user watches
    Claude work in Windows Terminal and can intervene any time.

    No stdout capture, no completion callback - this is fire-and-watch.

    Also opens VS Code at the target dir alongside the terminal so files
    appear live as Claude writes them.
    """
    try:
        claude_executable = _resolve_claude_executable()
    except FileNotFoundError as exc:
        return {
            "success": False,
            "message": str(exc),
            "data": {"project_dir": str(target), "mode": "interactive"},
            "requires_confirmation": False,
        }

    prompt = _build_claude_prompt(task_text, target, extra_context)
    model = os.getenv("CLAUDE_TASK_MODEL", "").strip()

    command = [
        claude_executable,
        "--permission-mode", "acceptEdits",
    ]
    if model:
        command.extend(["--model", model])
    # `--` separator: keep our prompt from being eaten as another --model value
    # if a future flag accepts variadic args (this bit us with --add-dir).
    command.append("--")
    command.append(prompt)

    # Open VS Code alongside the terminal so the project files appear as
    # Claude writes them.  Fire-and-forget; if VS Code isn't configured we
    # silently skip it - the terminal is the primary surface.
    try:
        vscode_path = os.getenv("VSCODE_PATH", "")
        if vscode_path and os.path.exists(vscode_path):
            subprocess.Popen([vscode_path, str(target)], shell=False)
    except Exception:  # noqa: BLE001
        pass

    # CREATE_NEW_CONSOLE spawns the child with its own console window.
    # On Windows 11 with Windows Terminal as the default conhost, this opens
    # in WT (matching the screenshot the user shared); on older Windows it
    # falls back to a plain conhost window.
    create_new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

    try:
        subprocess.Popen(
            command,
            cwd=str(target),
            creationflags=create_new_console,
            close_fds=False,
        )
    except FileNotFoundError as exc:
        return {
            "success": False,
            "message": f"Could not launch Claude Code: {exc}",
            "data": {"project_dir": str(target), "mode": "interactive"},
            "requires_confirmation": False,
        }

    _log_activity(f"Claude interactive launched target={target}")
    return {
        "success": True,
        "message": (
            f"Opened Claude Code in a new terminal window inside {target.name}. "
            "Watch it build your app there - you can intervene any time."
        ),
        "data": {
            "task": task_text,
            "project_dir": str(target),
            "tool": "claude_code",
            "mode": "interactive",
        },
        "requires_confirmation": False,
    }


def _run_claude_headless(task_text: str, target: Path, extra_context: str) -> dict:
    """
    Legacy headless path: run claude --print, capture stdout, open VS Code on
    success.  Selectable via CLAUDE_TASK_MODE=headless when the caller wants
    a captured summary instead of an interactive session.
    """
    claude_executable = _resolve_claude_executable()
    prompt = _build_claude_prompt(task_text, target, extra_context)
    timeout_s = max(60.0, _get_env_float("CLAUDE_TASK_TIMEOUT_S", 600.0))
    model = os.getenv("CLAUDE_TASK_MODEL", "").strip()

    command = [
        claude_executable,
        "--print",
        "--permission-mode", "acceptEdits",
        "--add-dir", str(target),
    ]
    if model:
        command.extend(["--model", model])
    command.append("--")
    command.append(prompt)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(target),
    )

    stdout_tail = _tail_text(completed.stdout)
    stderr_tail = _tail_text(completed.stderr)
    success = completed.returncode == 0
    last_message = stdout_tail.strip() if success else ""

    if success:
        message = last_message or f"Claude finished the task in {target}."
        try:
            vscode_path = os.getenv("VSCODE_PATH", "")
            if vscode_path and os.path.exists(vscode_path):
                subprocess.Popen([vscode_path, str(target)], shell=False)
            else:
                os.startfile(str(target))
        except Exception:  # noqa: BLE001
            pass
    else:
        detail = stderr_tail or stdout_tail or f"Claude exited with code {completed.returncode}."
        message = f"Claude could not finish that task. {detail}"

    _log_activity(f"Claude task returncode={completed.returncode} target={target}")
    return {
        "success": success,
        "message": message,
        "data": {
            "task": task_text,
            "project_dir": str(target),
            "returncode": completed.returncode,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "last_message": last_message,
            "tool": "claude_code",
            "mode": "headless",
        },
        "requires_confirmation": False,
    }


def run_claude_task(task: str, project_name: str = "", context: str = "") -> dict:
    """
    Hand a coding task off to Claude Code in non-interactive (--print) mode.

    Creates a fresh project directory under WORKSPACE_DIR, invokes Claude with
    permission to read/write inside that directory, and returns the final
    message Claude emits when it finishes.

    Wrapped in _queue_operation because Claude has network-side compute cost,
    so the user must explicitly confirm via POST /confirm before it runs.
    """
    try:
        clean_task = re.sub(r"\s+", " ", str(task or "").strip())
        if not clean_task:
            return {
                "success": False,
                "message": "I need a task before I can hand it to Claude.",
                "data": {},
                "requires_confirmation": False,
            }

        slug_source = project_name or clean_task
        slug = _slugify(slug_source, fallback="claude-task")
        workspace = Path(os.getenv("WORKSPACE_DIR", _WORKSPACE_DIR))
        target_dir = workspace / slug

        def do_run_claude(task_text: str, target_path: str, extra_context: str):
            target = Path(target_path)
            target.mkdir(parents=True, exist_ok=True)

            mode = os.getenv("CLAUDE_TASK_MODE", "interactive").strip().lower()
            if mode == "interactive":
                return _run_claude_interactive(task_text, target, extra_context)
            return _run_claude_headless(task_text, target, extra_context)

        return _queue_operation(
            fn=do_run_claude,
            kwargs={
                "task_text": clean_task,
                "target_path": str(target_dir.resolve()),
                "extra_context": context,
            },
            description=f"Run Claude Code locally for: {clean_task[:90]}",
        )
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def run_codex_task(task: str, project_name: str = "", context: str = "") -> dict:
    try:
        clean_task = re.sub(r"\s+", " ", str(task or "").strip())
        if not clean_task:
            return {
                "success": False,
                "message": "I need a task before I can hand it to Codex.",
                "data": {},
                "requires_confirmation": False,
            }

        slug_source = project_name or clean_task
        slug = _slugify(slug_source, fallback="codex-task")
        workspace = Path(os.getenv("WORKSPACE_DIR", _WORKSPACE_DIR))
        target_dir = workspace / slug
        last_message_path = target_dir / ".codex-last-message.txt"

        def do_run_codex(task_text: str, target_path: str, last_message_file: str, extra_context: str):
            target = Path(target_path)
            target.mkdir(parents=True, exist_ok=True)

            codex_executable = _resolve_codex_executable()
            prompt = _build_codex_prompt(task_text, target, extra_context)
            timeout_s = max(60.0, _get_env_float("CODEX_TASK_TIMEOUT_S", _DEFAULT_CODEX_TASK_TIMEOUT_S))
            command = [
                codex_executable,
                "exec",
                "--cd",
                str(target),
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "--skip-git-repo-check",
                "-o",
                str(last_message_file),
                prompt,
            ]

            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )

            last_message = ""
            try:
                last_message = Path(last_message_file).read_text(encoding="utf-8").strip()
            except OSError:
                last_message = ""

            stdout_tail = _tail_text(completed.stdout)
            stderr_tail = _tail_text(completed.stderr)
            success = completed.returncode == 0
            if success:
                message = last_message or f"Codex finished the task in {target}."
                try:
                    os.startfile(str(target))
                except Exception:
                    pass
            else:
                detail = stderr_tail or stdout_tail or f"Codex exited with code {completed.returncode}."
                message = f"Codex could not finish that task. {detail}"

            _log_activity(f"Codex task returncode={completed.returncode} target={target}")
            return {
                "success": success,
                "message": message,
                "data": {
                    "task": task_text,
                    "project_dir": str(target),
                    "returncode": completed.returncode,
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                    "last_message": last_message,
                },
                "requires_confirmation": False,
            }

        return _queue_operation(
            fn=do_run_codex,
            kwargs={
                "task_text": clean_task,
                "target_path": str(target_dir.resolve()),
                "last_message_file": str(last_message_path.resolve()),
                "extra_context": context,
            },
            description=f"Run Codex locally for: {clean_task[:90]}",
        )
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def create_app(description: str) -> dict:
    try:
        clean_description = re.sub(r"\s+", " ", str(description or "").strip())

        # Unified routing decision - see _choose_app_provider for the rules.
        # Claude Code is the default for "create an application that ...";
        # Codex picks up script-style work; Ollama is a last-resort fallback
        # only when neither CLI is installed.
        provider = _choose_app_provider(description)
        if provider == "claude":
            task = (
                "Create a complete local application for this request: "
                f"{clean_description}. Include setup and run instructions in a README."
            )
            return run_claude_task(task, project_name=clean_description)

        if provider == "codex":
            task = (
                "Create a complete local application for this request: "
                f"{clean_description}. Include setup/run instructions in the project."
            )
            return run_codex_task(task, project_name=clean_description)

        slug = _slugify(description, fallback="generated-app", max_length=30)
        if not slug:
            slug = "generated-app"
            
        def do_create_app(slug_name, desc):
            host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            model = os.getenv("CODEGEN_MODEL", "qwen2.5-coder:14b")
            prompt = (
                f"Write a complete application: {desc}\n\n"
                "Return only the file contents in this JSON format:\n"
                '[{"filename": "main.py", "content": "..."}]'
            )
            payload = {"model": model, "prompt": prompt, "stream": False}
            try:
                resp = requests.post(f"{host}/api/generate", json=payload, timeout=120)
                resp.raise_for_status()
                raw = resp.json().get("response", "")
                
                raw = raw.strip()
                if raw.startswith("```json"): raw = raw[7:]
                elif raw.startswith("```"): raw = raw[3:]
                if raw.endswith("```"): raw = raw[:-3]
                raw = raw.strip()

                files = json.loads(raw)
                project_dir = Path(_WORKSPACE_DIR) / slug_name
                project_dir.mkdir(parents=True, exist_ok=True)
                
                created = []
                project_root = project_dir.resolve()
                for f in files:
                    filename = f.get("filename")
                    content = f.get("content", "")
                    if not filename:
                        continue
                    target_path = (project_dir / filename).resolve()
                    if not target_path.is_relative_to(project_root):
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(target_path, "w", encoding="utf-8") as file_:
                        file_.write(content)
                    world_model.index_file_path(target_path, source="query_scan")
                    created.append(str(target_path))

                if not created:
                    return {
                        "success": False,
                        "message": "The code model did not return any valid project files.",
                        "data": {"project_dir": str(project_dir), "files_created": []},
                        "requires_confirmation": False,
                    }
                
                _log_activity(f"Created app project: {project_dir} with files: {created}")
                
                vscode_path = os.getenv("VSCODE_PATH")
                if vscode_path and os.path.exists(vscode_path):
                    subprocess.Popen([vscode_path, str(project_dir)], shell=False)
                    _log_activity(f"Launched VS Code at: {project_dir}")
                else:
                    os.startfile(str(project_dir))
                    _log_activity(f"Opened explorer at: {project_dir}")
                    
                return {
                    "success": True,
                    "message": f"Created project '{slug_name}' with {len(created)} files.",
                    "data": {"project_dir": str(project_dir), "files_created": created},
                    "requires_confirmation": False
                }
            except Exception as ex:
                return {"success": False, "message": str(ex), "data": {}, "requires_confirmation": False}

        return _queue_operation(
            fn=do_create_app,
            kwargs={"slug_name": slug, "desc": description},
            description=f"Generate app: {description[:60]}"
        )
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def search_pc(query: str) -> dict:
    try:
        web_query = _extract_web_search_query(query)
        if web_query:
            return web_search(web_query)

        matches = world_model.search_files(query, limit=20)
        files = [item["path"] for item in matches]
        best_match = matches[0]["name"] if matches else None
        return {
            "success": True,
            "message": (
                f"I found {len(files)} matches for '{query}'."
                + (f" The best match looks like {best_match}." if best_match else "")
            ),
            "data": {"results": files, "count": len(files), "matches": matches},
            "requires_confirmation": False
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def web_search(query: str) -> dict:
    try:
        clean_query = _extract_web_search_query(query) or str(query or "").strip()
        clean_query = re.sub(r"\s+", " ", clean_query).strip(" .?!")
        clean_query = _clean_search_phrase(clean_query)
        if not clean_query:
            return {
                "success": False,
                "message": "I need a search query before I can search the web.",
                "data": {},
                "requires_confirmation": False,
            }

        provider = (os.getenv("WEB_SEARCH_PROVIDER", "google").strip().lower() or "google")
        encoded_query = urllib.parse.quote_plus(clean_query)
        if provider == "duckduckgo":
            url = f"https://duckduckgo.com/?q={encoded_query}"
        elif provider == "bing":
            url = f"https://www.bing.com/search?q={encoded_query}"
        else:
            url = f"https://www.google.com/search?q={encoded_query}"

        opened = False
        try:
            opened = bool(webbrowser.open(url, new=2))
        except Exception:
            opened = False

        if not opened:
            os.startfile(url)

        _log_activity(f"Opened web search: {url}")
        return {
            "success": True,
            "message": f"Searching the web for {clean_query}.",
            "data": {
                "query": clean_query,
                "url": url,
                "provider": provider,
            },
            "requires_confirmation": False,
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def system_query(query: str) -> dict:
    try:
        state = pc_state.get_state()
        message, filtered_state = _summarize_state_for_user(state, query)
        return {"success": True, "message": message, "data": filtered_state, "requires_confirmation": False}
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def general(params: dict) -> dict:
    try:
        raw_text = str(params.get("raw_transcript", params.get("raw_text", "")) or "").strip()
        if not raw_text:
            return {
                "success": False,
                "message": "I did not receive any text to work with.",
                "data": {},
                "requires_confirmation": False,
            }

        shortcut_result = _handle_direct_general_shortcut(raw_text)
        if shortcut_result is not None:
            return shortcut_result

        remembered_fact = _detect_memory_fact(raw_text)
        if remembered_fact:
            world_model.store_memory(remembered_fact, importance=1.2)
            return {
                "success": True,
                "message": f"I'll remember that {remembered_fact}.",
                "data": {
                    "stored_fact": remembered_fact,
                    "raw_text": raw_text,
                    "style": "assistant_reply",
                },
                "requires_confirmation": False,
            }

        if _is_memory_recall_query(raw_text):
            memories = _get_relevant_memories(raw_text, limit=5)
            if memories:
                if len(memories) == 1:
                    message = f"I remember that {memories[0]}."
                else:
                    memory_preview = "; ".join(memories[:4])
                    message = f"Here is what I remember about you: {memory_preview}."
            else:
                message = "I do not have any personal memories saved yet. You can tell me to remember something important."
            return {
                "success": True,
                "message": message,
                "data": {
                    "raw_text": raw_text,
                    "style": "assistant_reply",
                    "memories": memories if memories else [],
                },
                "requires_confirmation": False,
            }

        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        planner_model = os.getenv("PLANNER_MODEL", "").strip() or os.getenv("INTENT_MODEL", "mistral").strip() or "mistral"
        general_model = os.getenv("GENERAL_MODEL", "").strip() or planner_model
        planner_timeout_s = max(8.0, _get_env_float("GENERAL_PLANNER_TIMEOUT_S", _DEFAULT_PLANNER_TIMEOUT_S))
        planner_max_steps = max(1, _get_env_int("GENERAL_PLANNER_MAX_STEPS", _DEFAULT_PLANNER_MAX_STEPS))
        history: list[dict[str, Any]] = []
        last_tool_result: dict[str, Any] | None = None

        for step in range(1, planner_max_steps + 1):
            planner_prompt = _build_planner_prompt(raw_text, history, step, planner_max_steps)
            planner_raw = _call_ollama_generate(
                host=host,
                model=planner_model,
                prompt=planner_prompt,
                timeout_s=planner_timeout_s,
                expect_json=True,
            )
            planner_decision = _parse_json_response(planner_raw)

            if planner_decision is None:
                answer = planner_raw.strip()
                if answer:
                    return {
                        "success": True,
                        "message": answer,
                        "data": {"raw_text": raw_text, "style": "assistant_reply", "planner_steps": history},
                        "requires_confirmation": False,
                    }
                break

            decision_type = str(planner_decision.get("type", "final")).strip().lower()
            if decision_type == "final":
                answer = str(planner_decision.get("response", "")).strip()
                if not answer and last_tool_result is not None:
                    answer = str(last_tool_result.get("message", "")).strip()
                if not answer:
                    answer = "I finished that request."
                return {
                    "success": True,
                    "message": answer,
                    "data": {"raw_text": raw_text, "style": "assistant_reply", "planner_steps": history},
                    "requires_confirmation": False,
                }

            if decision_type == "clarify":
                follow_up = str(planner_decision.get("response", "")).strip() or "Could you clarify what you want me to do?"
                return {
                    "success": True,
                    "message": "Clarification needed.",
                    "data": {
                        "requires_clarification": True,
                        "follow_up": follow_up,
                        "planner_steps": history,
                    },
                    "requires_confirmation": False,
                }

            if decision_type != "tool":
                break

            tool_name = str(planner_decision.get("tool", "")).strip()
            tool_arguments = planner_decision.get("arguments", {})
            tool_result = _execute_planner_tool(tool_name, tool_arguments, raw_text)
            last_tool_result = tool_result

            history.append(
                {
                    "step": step,
                    "tool": tool_name,
                    "arguments": tool_arguments if isinstance(tool_arguments, dict) else {},
                    "reason": str(planner_decision.get("reason", "")).strip(),
                    "observation": _summarize_tool_observation(tool_result),
                }
            )

            if tool_result.get("data", {}).get("requires_confirmation") or tool_result.get("requires_confirmation"):
                tool_result.setdefault("data", {})
                tool_result["data"]["planner_steps"] = history
                return tool_result

        if last_tool_result is not None:
            last_tool_result.setdefault("data", {})
            last_tool_result["data"]["planner_steps"] = history
            return last_tool_result

        answer = _call_ollama_generate(
            host=host,
            model=general_model,
            prompt=_build_general_prompt(raw_text),
            timeout_s=planner_timeout_s,
            expect_json=False,
        ).strip()

        return {
            "success": True,
            "message": answer or "I could not finish that request yet.",
            "data": {"raw_text": raw_text, "style": "assistant_reply", "planner_steps": history},
            "requires_confirmation": False,
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": {}, "requires_confirmation": False}


def confirm_operation(operation_id: str) -> dict:
    with _PENDING_OPS_LOCK:
        _prune_pending_ops_locked()
        op = _PENDING_OPS.pop(operation_id, None)
    if op is None:
        return {"success": False, "message": "Unknown or expired operation_id.", "data": {}, "requires_confirmation": False}
    try:
        return op["fn"](**op["kwargs"])
    except Exception as exc:
        return {"success": False, "message": str(exc), "data": {}, "requires_confirmation": False}

def resolve_app_path(app_name: str) -> str:
    app_record = world_model.resolve_app(app_name, refresh_if_needed=True)
    if app_record and app_record.get("path"):
        return str(app_record["path"])

    query = _normalize_app_name(app_name)
    if not query:
        raise FileNotFoundError("No application name was provided.")

    direct_path = _resolve_alias_path(query)
    if direct_path:
        return direct_path

    candidates = _discover_launchable_apps()
    exact_keys = {
        query,
        _compact_app_name(query),
        query.replace(" ", "-"),
        query.replace(" ", "_"),
    }
    for key in exact_keys:
        if key in candidates:
            return candidates[key]

    best_match = _find_best_app_match(query, candidates)
    if best_match is not None:
        return best_match

    raise FileNotFoundError(f"Could not locate {app_name}")


def _is_path_allowed(target_path: str) -> bool:
    raw = os.getenv("ALLOWED_PATHS", "")
    roots = [Path(p.strip()).resolve() for p in raw.split(",") if p.strip()]
    try:
        target = Path(target_path).resolve()
        return any(target.is_relative_to(r) for r in roots)
    except Exception:
        return False


def _queue_operation(fn, kwargs: dict, description: str) -> dict:
    op_id = str(uuid.uuid4())
    with _PENDING_OPS_LOCK:
        _prune_pending_ops_locked()
        _PENDING_OPS[op_id] = {"fn": fn, "kwargs": kwargs, "created_at": time.time()}
    return {
        "success": False,
        "message": "This action requires your confirmation.",
        "data": {
            "requires_confirmation": True,
            "operation_id":          op_id,
            "description":           description,
        }
    }


def _prune_pending_ops_locked() -> None:
    now = time.time()
    expired_ids = [
        op_id
        for op_id, op in _PENDING_OPS.items()
        if now - float(op.get("created_at") or now) > _PENDING_OP_TTL_S
    ]
    for op_id in expired_ids:
        _PENDING_OPS.pop(op_id, None)

    overflow = len(_PENDING_OPS) - _PENDING_OP_MAX
    if overflow <= 0:
        return

    oldest = sorted(
        _PENDING_OPS,
        key=lambda op_id: float(_PENDING_OPS[op_id].get("created_at") or 0.0),
    )
    for op_id in oldest[:overflow]:
        _PENDING_OPS.pop(op_id, None)


def _normalize_app_name(value: str) -> str:
    """
    Normalize spoken or typed app names into a comparison-friendly key.
    """
    lowered = str(value or "").strip().lower()
    lowered = re.sub(r"\.exe$", "", lowered)
    lowered = re.sub(r"^(?:please\s+)?(?:can you\s+)?(?:could you\s+)?", "", lowered)
    lowered = re.sub(r"^(?:open|launch|start|run)\s+", "", lowered)
    lowered = re.sub(
        r"\s+(?:on|in)\s+my\s+(?:pc|computer|laptop|desktop)$",
        "",
        lowered,
    )
    lowered = re.sub(r"\s+(?:for me|please)$", "", lowered)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(part for part in lowered.split() if part)


def _compact_app_name(value: str) -> str:
    """
    Remove spaces from a normalized app name for fuzzy matching.
    """
    return _normalize_app_name(value).replace(" ", "")


def _friendly_app_name(exe_path: str, fallback_name: str) -> str:
    """
    Turn an executable path into a readable app name for user-facing messages.
    """
    # Shell URIs (e.g. shell:AppsFolder\<AUMID>) cannot be parsed by Path; the
    # AUMID is a non-friendly identifier anyway, so use the caller's name.
    if _is_launch_uri(exe_path):
        cleaned_fallback = _normalize_app_name(fallback_name)
        return cleaned_fallback.title() if cleaned_fallback else "Application"
    stem = Path(exe_path).stem.replace("-", " ").replace("_", " ").strip()
    if stem:
        return stem.title()
    cleaned_fallback = _normalize_app_name(fallback_name)
    return cleaned_fallback.title() if cleaned_fallback else "Application"


def _resolve_alias_path(query: str) -> str | None:
    """
    Resolve a normalized app query through known alias paths and glob patterns.
    """
    from glob import glob

    alias_candidates = {
        query,
        _compact_app_name(query),
    }

    for alias, raw_paths in _APP_ALIAS_PATHS.items():
        normalized_alias = _normalize_app_name(alias)
        alias_variants = {normalized_alias, _compact_app_name(normalized_alias)}
        if alias_candidates.isdisjoint(alias_variants):
            continue

        for raw_path in raw_paths:
            expanded = os.path.expandvars(raw_path)
            if "*" in expanded:
                for matched_path in glob(expanded):
                    if os.path.exists(matched_path):
                        return matched_path.replace("\\", "/")
                continue

            if os.path.exists(expanded):
                return expanded.replace("\\", "/")

    return None


def _discover_launchable_apps(refresh: bool = False) -> dict[str, str]:
    """
    Collect launchable application candidates from aliases, running processes,
    and configured search roots. Results are cached briefly for responsiveness.
    """
    now = datetime.now().timestamp()
    if not refresh and _APP_DISCOVERY_CACHE["candidates"] and now - _APP_DISCOVERY_CACHE["timestamp"] < _APP_DISCOVERY_TTL_S:
        return _APP_DISCOVERY_CACHE["candidates"]

    candidates: dict[str, str] = {}

    for alias in _APP_ALIAS_PATHS:
        resolved = _resolve_alias_path(_normalize_app_name(alias))
        if resolved:
            _register_app_candidate(candidates, alias, resolved)

    for process_name, process_path in _iter_running_app_candidates():
        _register_app_candidate(candidates, process_name, process_path)

    for root in _iter_app_search_roots():
        try:
            for current_root, _, files in os.walk(root):
                for file_name in files:
                    if not file_name.lower().endswith(".exe"):
                        continue
                    full_path = os.path.join(current_root, file_name).replace("\\", "/")
                    _register_app_candidate(candidates, Path(file_name).stem, full_path)
        except OSError:
            continue

    _APP_DISCOVERY_CACHE["timestamp"] = now
    _APP_DISCOVERY_CACHE["candidates"] = candidates
    return candidates


def _iter_app_search_roots() -> list[str]:
    """
    Return configured and sensible default application search roots.
    """
    configured_roots = os.getenv(
        "APP_SEARCH_PATHS",
        "C:/Program Files;C:/Program Files (x86);%LOCALAPPDATA%/Programs",
    ).split(";")
    default_roots = [
        r"%LOCALAPPDATA%/Microsoft/WindowsApps",
        r"%PROGRAMFILES%",
        r"%PROGRAMFILES(X86)%",
    ]

    roots: list[str] = []
    seen: set[str] = set()
    for raw_root in [*configured_roots, *default_roots]:
        expanded = os.path.expandvars(raw_root).strip().strip('"')
        if not expanded:
            continue
        normalized = expanded.replace("\\", "/").lower()
        if normalized in seen or not os.path.exists(expanded):
            continue
        seen.add(normalized)
        roots.append(expanded)
    return roots


def _iter_running_app_candidates() -> list[tuple[str, str]]:
    """
    Yield names and executable paths for currently running user processes.
    """
    try:
        import psutil
    except Exception:
        return []

    candidates: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for process in psutil.process_iter(["name", "exe"]):
        try:
            process_name = process.info.get("name") or ""
            process_path = process.info.get("exe") or ""
        except (psutil.Error, OSError, AttributeError):
            continue

        if not process_name or not process_path or not process_path.lower().endswith(".exe"):
            continue

        normalized_path = process_path.replace("\\", "/")
        if normalized_path.lower() in seen_paths:
            continue

        seen_paths.add(normalized_path.lower())
        candidates.append((Path(process_name).stem, normalized_path))
    return candidates


def _register_app_candidate(candidates: dict[str, str], raw_name: str, path: str) -> None:
    """
    Register multiple lookup keys for one executable path.
    """
    normalized_name = _normalize_app_name(raw_name)
    normalized_path_name = _normalize_app_name(Path(path).stem)
    keys = {
        normalized_name,
        normalized_path_name,
        _compact_app_name(normalized_name),
        _compact_app_name(normalized_path_name),
    }

    for key in keys:
        if key and key not in candidates:
            candidates[key] = path


def _find_best_app_match(query: str, candidates: dict[str, str]) -> str | None:
    """
    Find the closest executable match for a normalized app query.
    """
    compact_query = _compact_app_name(query)
    if not compact_query:
        return None

    query_soundex = _soundex(compact_query)
    best_path: str | None = None
    best_score = 0.0

    for candidate_key, candidate_path in candidates.items():
        if len(candidate_key) <= 2:
            continue

        score = difflib.SequenceMatcher(None, compact_query, candidate_key).ratio()
        if candidate_key.startswith(compact_query) or compact_query.startswith(candidate_key):
            score += 0.08
        if query_soundex and query_soundex == _soundex(candidate_key):
            score += 0.25
        if abs(len(compact_query) - len(candidate_key)) >= 4:
            score -= 0.12

        if score > best_score:
            best_score = score
            best_path = candidate_path

    if best_score >= 0.72:
        return best_path

    return None


def _soundex(value: str) -> str:
    """
    Compute a compact Soundex code for fuzzy spoken app-name matching.
    """
    cleaned = re.sub(r"[^a-z]", "", str(value or "").lower())
    if not cleaned:
        return ""

    first_letter = cleaned[0].upper()
    mapping = {
        **{letter: "1" for letter in "bfpv"},
        **{letter: "2" for letter in "cgjkqsxz"},
        **{letter: "3" for letter in "dt"},
        "l": "4",
        **{letter: "5" for letter in "mn"},
        "r": "6",
    }

    encoded_digits: list[str] = []
    previous_digit = mapping.get(cleaned[0], "")
    for letter in cleaned[1:]:
        digit = mapping.get(letter, "")
        if digit and digit != previous_digit:
            encoded_digits.append(digit)
        previous_digit = digit

    return (first_letter + "".join(encoded_digits) + "000")[:4]
