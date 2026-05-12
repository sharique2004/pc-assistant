"""
app.py — Flask entry point for the PC Assistant backend.

Loads environment from .env, registers all REST endpoints, and starts
the development server.  Routing logic maps the intent returned by
voice_intent.listen_and_parse() to the correct executor function.

Python 3.11+
"""

import os
import logging
import time
from pathlib import Path
import requests as http_client
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

import voice_intent
import executor
import cloud_router
import world_model
from pc_state import get_state
import tts
from flask import send_file

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
world_model.initialize()
world_model.warm_world_model_async(force=False)

# CORS — origins drawn from env so the dev URL (Vite) is never hardcoded.
_raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)
_cors_origins = [origin.strip() for origin in _raw_origins.split(",") if origin.strip()]
CORS(app, origins=_cors_origins)

FLASK_HOST: str = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT: int = int(os.getenv("FLASK_PORT", "5000"))
FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "false").lower() == "true"
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_HEALTH_TIMEOUT_S: float = float(os.getenv("OLLAMA_HEALTH_TIMEOUT_S", "0.5"))
OLLAMA_HEALTH_CACHE_TTL_S: float = float(os.getenv("OLLAMA_HEALTH_CACHE_TTL_S", "5.0"))
_OLLAMA_HEALTH_CACHE: dict = {"checked_at": 0.0, "status": None}

# ---------------------------------------------------------------------------
# Intent → executor routing table
#
# Each key matches the "intent" field returned by listen_and_parse().
# The value is a callable that accepts the "parameters" dict from the intent
# and delegates to the appropriate executor function.
#
# Add new intents here as the project grows — no changes elsewhere required.
# ---------------------------------------------------------------------------

def _route_open_app(params: dict) -> dict:
    """Delegate to executor.open_app using parameters from the intent."""
    app_name: str = params.get("app_name", "")
    if not app_name:
        return {"success": False, "message": "No app_name provided.", "data": {}}
    return executor.open_app(app_name=app_name)


def _route_create_file(params: dict) -> dict:
    """Delegate to executor.create_file using parameters from the intent."""
    file_name: str = params.get("file_name", "")
    file_type: str = params.get("file_type", "txt")
    content: str = params.get("content", "")
    if not file_name:
        return {"success": False, "message": "No file_name provided.", "data": {}}
    return executor.create_file(file_name=file_name, file_type=file_type, content=content)


def _route_create_app(params: dict) -> dict:
    """Delegate to executor.create_app using parameters from the intent."""
    description: str = params.get("description", "")
    if not description:
        return {"success": False, "message": "No description provided.", "data": {}}
    return executor.create_app(description=description)


def _route_search_pc(params: dict) -> dict:
    """Delegate to executor.search_pc using parameters from the intent."""
    query: str = params.get("query", "")
    if not query:
        return {"success": False, "message": "No query provided.", "data": {}}
    return executor.search_pc(query=query)


def _route_web_search(params: dict) -> dict:
    """Delegate to executor.web_search using parameters from the intent."""
    query: str = params.get("query", "")
    if not query:
        return {"success": False, "message": "No query provided.", "data": {}}
    return executor.web_search(query=query)


def _route_system_query(params: dict) -> dict:
    """Delegate to executor.system_query using parameters from the intent."""
    query: str = params.get("query", "")
    return executor.system_query(query=query)


def _route_general(params: dict) -> dict:
    """Delegate to executor.general for unclassified commands."""
    return executor.general(params=params)


def _route_clarify(params: dict) -> dict:
    """
    Handle low-confidence intents that need user clarification.

    Returns a structured response the frontend can display as a prompt.
    """
    return {
        "success": True,
        "message": "Clarification needed.",
        "data": {
            "requires_clarification": True,
            "follow_up": params.get("follow_up", "Could you repeat that more clearly?"),
        },
    }


INTENT_ROUTER: dict = {
    "open_app": _route_open_app,
    "create_file": _route_create_file,
    "create_app": _route_create_app,
    "search_pc": _route_search_pc,
    "web_search": _route_web_search,
    "system_query": _route_system_query,
    "general": _route_general,
    "clarify": _route_clarify,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(code: str, detail: str, status: int = 400):
    """
    Build a standardised error response envelope.

    Args:
        code:   Snake_case error identifier consumed by the frontend.
        detail: Human-readable explanation.
        status: HTTP status code.

    Returns:
        Flask Response with JSON body and the supplied status code.
    """
    logger.warning("error=%s  detail=%s", code, detail)
    return jsonify({"success": False, "error": code, "detail": detail}), status


def _get_ollama_status() -> dict:
    """
    Return cached Ollama reachability so UI polling cannot stall the demo.
    """
    now = time.time()
    cached_status = _OLLAMA_HEALTH_CACHE.get("status")
    checked_at = float(_OLLAMA_HEALTH_CACHE.get("checked_at") or 0.0)
    if cached_status and now - checked_at < OLLAMA_HEALTH_CACHE_TTL_S:
        return dict(cached_status)

    ollama_status: dict = {"reachable": False, "host": OLLAMA_HOST, "error": None}
    try:
        resp = http_client.get(OLLAMA_HOST, timeout=OLLAMA_HEALTH_TIMEOUT_S)
        ollama_status["reachable"] = resp.status_code == 200
    except http_client.exceptions.ConnectionError:
        ollama_status["error"] = "Connection refused - is Ollama running?"
    except http_client.exceptions.Timeout:
        ollama_status["error"] = f"Timed out after {OLLAMA_HEALTH_TIMEOUT_S:g} s."
    except Exception as exc:  # noqa: BLE001
        ollama_status["error"] = str(exc)

    _OLLAMA_HEALTH_CACHE["checked_at"] = now
    _OLLAMA_HEALTH_CACHE["status"] = dict(ollama_status)
    return ollama_status


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """
    Liveness probe.  Reports Flask status and whether Ollama is reachable.

    Pings OLLAMA_HOST (from .env) with a short cached timeout.  The overall HTTP
    status is always 200 — the ``ollama.reachable`` field tells the frontend
    whether local inference is available.

    Returns:
        200 JSON:
            {
                "status":  "ok",
                "service": "pc-assistant-backend",
                "ollama": {
                    "reachable": bool,
                    "host":      str,
                    "error":     str | null
                }
            }
    """
    return jsonify({
        "status":  "ok",
        "service": "pc-assistant-backend",
        "ollama":  _get_ollama_status(),
        "cloud_router": cloud_router.get_status(),
        "world_model": world_model.get_status(),
    }), 200


@app.route("/command", methods=["POST"])
def command():
    """
    Main voice-command pipeline endpoint.

    1. Validates the JSON body.
    2. Calls voice_intent.listen_and_parse(trigger) to capture audio and
       extract a structured intent dict.
    3. Looks up the correct handler in INTENT_ROUTER.
    4. Calls the handler and returns the combined result.

    Request body (JSON):
        {
            "trigger": str  — e.g. "hold_to_speak" or "keyboard"
        }

    Returns:
        200 JSON:
            {
                "success": true,
                "intent":  { "intent": str, "parameters": dict, "raw_transcript": str },
                "result":  { "success": bool, "message": str, "data": dict }
            }
        400 JSON: {"success": false, "error": str, "detail": str}
        500 JSON: {"success": false, "error": str, "detail": str}
    """
    body = request.get_json(silent=True) if request.is_json else None
    uploaded_audio = request.files.get("audio")
    if body is None and uploaded_audio is None:
        return _error("invalid_request", "Send JSON or multipart form data with an audio file.", 400)

    text_command = ""
    if body is not None:
        text_command = str(
            body.get("text")
            or body.get("command")
            or body.get("transcript")
            or ""
        ).strip()

    if text_command:
        trigger = str(body.get("trigger", "typed_text") or "typed_text").strip()
    elif uploaded_audio is not None:
        trigger = request.form.get("trigger", "tap_to_speak").strip()
    else:
        trigger = body.get("trigger", "").strip()

    if not trigger:
        return _error("missing_field", "'trigger' is a required non-empty string.", 400)

    # --- Step 1: Voice capture + intent parsing ---
    try:
        if text_command:
            intent = voice_intent.parse_text_command(transcript=text_command, trigger=trigger)
        elif uploaded_audio is not None and uploaded_audio.filename:
            audio_path = _save_uploaded_audio(uploaded_audio.filename, uploaded_audio.read())
            try:
                intent = voice_intent.parse_audio_file(audio_path=audio_path, trigger=trigger)
            finally:
                try:
                    Path(audio_path).unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove temp uploaded audio file: %s", audio_path)
        else:
            intent = voice_intent.listen_and_parse(trigger=trigger)
        logger.info("intent parsed: %s", intent.get("intent"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("listen_and_parse failed")
        return _error("intent_parse_failed", str(exc), 500)

    # --- Step 2: Validate intent shape ---
    intent_name: str = intent.get("intent", "general")
    parameters: dict = intent.get("parameters", {})

    # --- Step 3: Route to executor ---
    handler = INTENT_ROUTER.get(intent_name, _route_general)
    try:
        result: dict = handler(parameters)
        logger.info("executor result: success=%s", result.get("success"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("executor handler failed for intent '%s'", intent_name)
        return _error("executor_failed", str(exc), 500)

    return jsonify({"success": True, "intent": intent, "result": result}), 200


def _save_uploaded_audio(filename: str, data: bytes) -> str:
    """
    Persist uploaded audio to the project tmp directory for local processing.

    Args:
        filename (str): Original uploaded filename.
        data (bytes): Raw uploaded file bytes.

    Returns:
        str: Absolute path to the saved temporary audio file.
    """
    tmp_dir = Path(__file__).resolve().parent.parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    safe_suffix = Path(filename).suffix.lower()
    if safe_suffix not in {".wav", ".webm", ".ogg", ".mp3", ".m4a", ".mp4", ".aac", ".flac"}:
        safe_suffix = ".webm"

    temp_path = tmp_dir / f"browser_audio_{int(time.time() * 1000)}{safe_suffix}"
    temp_path.write_bytes(data)
    return str(temp_path)


@app.route("/system-state", methods=["GET"])
def system_state():
    """
    Return a live snapshot of the host machine's state.

    Calls pc_state.get_state() and returns the resulting dict verbatim.

    Returns:
        200 JSON: { ...fields from pc_state.get_state()... }
        500 JSON: {"success": false, "error": "state_fetch_failed", "detail": str}
    """
    try:
        state: dict = get_state()
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_state failed")
        return _error("state_fetch_failed", str(exc), 500)

    return jsonify(state), 200

@app.route("/tts", methods=["POST"])
def tts_endpoint():
    """
    Generate local TTS audio from text.
    Request body (JSON): { "text": str }
    Returns: The audio file as attachment.
    """
    body = request.get_json(silent=True)
    if not body or "text" not in body:
        return _error("missing_field", "'text' is required.", 400)
    text = body["text"].strip()
    if not text:
        return _error("missing_field", "text cannot be empty.", 400)
        
    try:
        audio_path = tts.generate_tts_audio(text)
        if not os.path.exists(audio_path):
            raise FileNotFoundError("TTS engine did not output a file.")
        return send_file(audio_path, mimetype="audio/wav")
    except Exception as exc:
        logger.exception("tts generation failed")
        return _error("tts_failed", str(exc), 500)


@app.route("/confirm", methods=["POST"])
def confirm():
    """
    Secondary confirmation step for operations that set requires_confirmation=True
    (e.g. destructive file operations).

    Request body (JSON):
        {
            "operation_id": str  — opaque ID returned by the executor in a
                                   previous /command response
        }

    Returns:
        200 JSON: {"success": true, "result": { ... }}
        400 JSON: {"success": false, "error": str, "detail": str}
        500 JSON: {"success": false, "error": str, "detail": str}
    """
    body = request.get_json(silent=True)
    if body is None:
        return _error("invalid_json", "Request body must be valid JSON.", 400)

    operation_id: str = body.get("operation_id", "").strip()
    if not operation_id:
        return _error("missing_field", "'operation_id' is required.", 400)

    try:
        result: dict = executor.confirm_operation(operation_id=operation_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("confirm_operation failed")
        return _error("confirm_failed", str(exc), 500)

    return jsonify({"success": True, "result": result}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting PC Assistant backend on %s:%s", FLASK_HOST, FLASK_PORT)
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, threaded=True)
