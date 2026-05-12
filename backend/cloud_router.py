"""Optional hybrid cloud decision router for low-risk commands."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(_BACKEND_DIR / ".env")

_DEFAULT_PROVIDER = "gemini"
_DEFAULT_MODEL = "gemini-2.5-flash-lite"

_SAFE_INTENTS = {
    "open_app",
    "web_search",
    "system_query",
    "clarify",
}

_SYSTEM_QUERY_KEYWORDS = (
    "running app",
    "running apps",
    "running process",
    "running processes",
    "cpu",
    "processor",
    "memory",
    "ram",
    "disk",
    "storage",
    "active window",
    "focused window",
    "what is open",
)

_MULTI_STEP_MARKERS = (
    " and then ",
    " then ",
    " after that ",
    " once ",
    " let me know ",
    " tell me ",
    " remind me ",
    " if that works ",
    " when it is ready ",
    " when it's ready ",
)


def get_status() -> dict[str, Any]:
    """Return whether hybrid cloud routing is enabled and usable."""
    mode = _router_mode()
    provider = os.getenv("CLOUD_DECISION_PROVIDER", _DEFAULT_PROVIDER).strip().lower() or _DEFAULT_PROVIDER
    model = os.getenv("CLOUD_DECISION_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    api_key = _get_api_key()
    return {
        "enabled": mode in {"hybrid", "cloud"},
        "mode": mode,
        "provider": provider,
        "model": model,
        "configured": bool(api_key),
    }


def classify_intent(transcript: str) -> dict[str, Any] | None:
    """
    Classify a low-risk transcript with an optional cloud model.

    Returns ``None`` when hybrid mode is disabled, unavailable, or unsafe.
    """
    cleaned = str(transcript or "").strip()
    if not cleaned or not should_use_cloud_router(cleaned):
        return None

    provider = os.getenv("CLOUD_DECISION_PROVIDER", _DEFAULT_PROVIDER).strip().lower() or _DEFAULT_PROVIDER
    if provider != "gemini":
        return None

    api_key = _get_api_key()
    if not api_key:
        return None

    sanitized = _sanitize_transcript(cleaned)
    model = os.getenv("CLOUD_DECISION_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    timeout_s = max(3.0, _get_env_float("CLOUD_DECISION_TIMEOUT_S", 8.0))
    prompt = _build_intent_prompt(sanitized)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {
            "parts": [
                {
                    "text": (
                        "You classify short PC assistant commands into JSON only. "
                        "Never include markdown or explanations."
                    )
                }
            ]
        },
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 160,
        },
    }

    try:
        response = requests.post(
            url,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(3, timeout_s),
        )
        response.raise_for_status()
    except requests.RequestException:
        return None

    text = _extract_gemini_text(response.json())
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, dict):
        return None

    intent = str(parsed.get("intent", "")).strip().lower()
    parameters = parsed.get("parameters", {})
    confidence = parsed.get("confidence", 0.0)
    if intent not in _SAFE_INTENTS:
        return None
    if not isinstance(parameters, dict):
        parameters = {}
    return {"intent": intent, "parameters": parameters, "confidence": confidence}


def should_use_cloud_router(transcript: str) -> bool:
    """Return True when hybrid routing is enabled and the transcript is low-risk."""
    if _router_mode() not in {"hybrid", "cloud"}:
        return False

    text = str(transcript or "").strip()
    if not text:
        return False

    if len(text) > _get_env_int("CLOUD_DECISION_MAX_CHARS", 140):
        return False

    if _looks_sensitive(text):
        return False

    if _looks_multi_step(text):
        return False

    return _looks_cloud_eligible(text)


def _router_mode() -> str:
    return os.getenv("DECISION_ROUTER_MODE", "local").strip().lower() or "local"


def _get_api_key() -> str:
    return (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content", {})
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        return ""
    text_parts = [str(part.get("text", "")) for part in parts if isinstance(part, dict)]
    return "".join(text_parts).strip()


def _build_intent_prompt(transcript: str) -> str:
    return (
        "Classify this short command for a Windows PC assistant.\n\n"
        "Allowed intents:\n"
        "- open_app\n"
        "- web_search\n"
        "- system_query\n"
        "- clarify\n\n"
        "Return only JSON in exactly this shape:\n"
        '{'
        '"intent":"open_app",'
        '"parameters":{},'
        '"confidence":0.0'
        '}\n\n'
        "Rules:\n"
        "- Use web_search for commands like search Google, look up, or open Google and search.\n"
        "- Use open_app for launch/open app requests.\n"
        "- Use system_query for questions about running apps, CPU, memory, disk, or active window.\n"
        "- Use clarify when unclear or when the command looks multi-step.\n\n"
        f'Transcript: "{transcript}"'
    )


def _looks_sensitive(transcript: str) -> bool:
    lowered = str(transcript or "").strip().lower()
    if not lowered:
        return True

    sensitive_terms = (
        "remember that",
        "remember this",
        "about me",
        "do you remember",
        "what do you remember",
        "password",
        "passcode",
        "pin",
        "secret",
        "ssn",
        "social security",
        "bank",
        "routing number",
        "tax",
        "medical",
        "resume",
        "cv",
        "invoice",
        "transcript",
        "grade",
        "desktop",
        "documents",
        "downloads",
        "workspace",
        "i would like to create a file",
        "create a file",
        "make a file",
        "create app",
        "build me an app",
        "search my pc",
        "search my computer",
        "search my files",
        "look in my",
        "folder",
        "directory",
        "path",
        "allowed paths",
    )
    if any(term in lowered for term in sensitive_terms):
        return True

    if re.search(r"[a-z]:[\\/]", lowered):
        return True
    if re.search(r"(?:^|[\s\"'])[\w\-. ]+\.(?:txt|pdf|docx?|xlsx?|pptx?|py|js|ts|json|csv|zip|png|jpg|jpeg|mp4)(?:$|[\s\"'])", lowered):
        return True
    if re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", lowered):
        return True
    if re.search(r"https?://", lowered):
        return True

    return False


def _sanitize_transcript(transcript: str) -> str:
    cleaned = str(transcript or "").strip()
    cleaned = re.sub(r"[A-Za-z]:[\\/][^\s\"']+", "[local_path]", cleaned)
    cleaned = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", cleaned)
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _looks_cloud_eligible(transcript: str) -> bool:
    lowered = str(transcript or "").strip().lower()
    if not lowered:
        return False

    if _looks_web_search_command(lowered):
        return True
    if _looks_open_app_command(lowered):
        return True
    if _looks_system_query(lowered):
        return True

    return False


def _looks_web_search_command(transcript: str) -> bool:
    prefixes = (
        "open google and search ",
        "open google and look up ",
        "open chrome and search ",
        "search on google ",
        "search up on google ",
        "search google ",
        "google search ",
        "look up ",
        "find on google ",
        "google ",
    )
    return any(transcript.startswith(prefix) for prefix in prefixes)


def _looks_open_app_command(transcript: str) -> bool:
    if not re.match(r"^(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?(?:open|launch|start|run)\s+", transcript):
        return False

    risky_terms = (
        "file",
        "folder",
        "document",
        "download",
        "desktop",
        "directory",
        "path",
        "url",
        "website",
        "tab",
        "search",
        "http",
    )
    return not any(term in transcript for term in risky_terms)


def _looks_system_query(transcript: str) -> bool:
    question_starters = ("what", "which", "show", "list", "how much", "how many", "tell me")
    return transcript.startswith(question_starters) and any(keyword in transcript for keyword in _SYSTEM_QUERY_KEYWORDS)


def _looks_multi_step(transcript: str) -> bool:
    lowered = str(transcript or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in _MULTI_STEP_MARKERS)


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
