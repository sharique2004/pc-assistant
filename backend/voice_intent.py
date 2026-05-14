"""
Voice capture, transcription, and intent parsing for the PC Assistant.

This module records microphone audio with simple voice activity detection,
transcribes it locally with Whisper, sends the transcript to a local Ollama
instance running the ``mistral`` model, and returns a structured intent dict.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from collections import deque
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
import cloud_router
import world_model

_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _BACKEND_DIR.parent
_ENV_PATH = _BACKEND_DIR / ".env"
_VENDOR_DIR = _BACKEND_DIR / ".vendor"
_VENV_SITE_PACKAGES = _BACKEND_DIR / ".venv" / "Lib" / "site-packages"
_VENV_SITE_PACKAGES_POSIX = _BACKEND_DIR / ".venv" / "lib"

if _VENDOR_DIR.exists():
    sys.path.insert(0, str(_VENDOR_DIR))

if _VENV_SITE_PACKAGES.exists():
    sys.path.insert(0, str(_VENV_SITE_PACKAGES))
elif _VENV_SITE_PACKAGES_POSIX.exists():
    for candidate in _VENV_SITE_PACKAGES_POSIX.glob("python*/site-packages"):
        if candidate.exists():
            sys.path.insert(0, str(candidate))
            break

load_dotenv(_ENV_PATH)
world_model.initialize()
world_model.warm_world_model_async(force=False)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are an intent classifier for a fully local Windows PC assistant.

Classify the transcript into exactly one of these intents:
- open_app
- create_file
- create_app
- search_pc
- web_search
- system_query
- general
- clarify

Return ONLY a valid JSON object with this exact shape:
{
  "intent": "<one intent name>",
  "parameters": {},
  "confidence": 0.0
}

Rules:
- Do not include markdown, explanations, or code fences.
- Use "clarify" when the request is too ambiguous to act on safely.
- Keep confidence between 0.0 and 1.0.
- Extract the smallest useful parameter set for the selected intent.
- Use "general" for conversational requests, multi-step requests, or anything that
  needs follow-up reasoning or local tool planning.
- Use "general" when the user wants you to remember or recall personal facts.
- Use "create_app" (NOT "create_file") whenever the user asks you to write a
  script, utility, CLI tool, function, program, application, website, or any
  deliverable they will run.  "create_file" is only for plain notes or
  documents - things like "create a file called notes.txt that says hello".

Parameter shapes:
- open_app: {"app_name": "Chrome"}
- create_file: {"file_name": "hello", "file_type": "py", "content": ""}
- create_app: {"description": "a to do list app"}
- search_pc: {"query": "resume I saved last week"}
- web_search: {"query": "how to create an AWS policy generator"}
- system_query: {"query": "what apps are running right now"}
- general: {"raw_text": "<verbatim transcript>"}
- clarify: {"follow_up": "Could you be more specific about what you want to do?"}
""".strip()

_ALLOWED_INTENTS = {
    "open_app",
    "create_file",
    "create_app",
    "search_pc",
    "web_search",
    "system_query",
    "general",
    "clarify",
}
_DEFAULT_CLARIFICATION_PROMPT = "Could you be more specific about what you want to do?"
_DEFAULT_MODEL_CONFIDENCE = 0.85
_DEFAULT_WHISPER_LANGUAGE = "en"
_DEFAULT_WHISPER_PROMPT = (
    "This is a short English voice command for a Windows PC assistant. "
    "Prefer common words and app names such as Chrome, Google, Canvas, Claude, "
    "ChatGPT, Notepad, Spotify, Discord, Edge, Firefox, VS Code, and Explorer."
)
_CHUNK_DURATION_S = 0.1
_PRESPEECH_BUFFER_S = 0.4
_TEMP_FILE_MAX_AGE_S = 3600
_DEFAULT_FALLBACK_SIGNAL = 0.001
_LEGACY_TMP_DIRS = {"c:/temp/pc-assistant", "c:\\temp\\pc-assistant"}
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
_FASTER_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()


class NoSpeechDetectedError(RuntimeError):
    """Raised when microphone capture never crosses the speech threshold."""


def listen_and_parse(trigger: str = "voice") -> dict:
    """
    Record audio, transcribe it with local Whisper, and classify the intent.

    Args:
        trigger (str): Optional label describing how capture was initiated.

    Returns:
        dict: Structured intent payload for the Flask app and frontend.
    """
    normalized_trigger = (trigger or "voice").strip() or "voice"
    try:
        audio_path = _record_audio()
        return _process_audio_path(audio_path=audio_path, trigger=normalized_trigger)
    except NoSpeechDetectedError:
        return {
            "intent": "clarify",
            "parameters": {"follow_up": "I did not hear anything. Please try speaking again."},
            "raw_transcript": "",
            "trigger": normalized_trigger,
            "confidence": 0.0,
            "clarification_prompt": "I did not hear anything. Please try speaking again.",
        }
    except Exception as exc:
        raise RuntimeError(f"Voice intent pipeline failed: {exc}") from exc


def parse_audio_file(audio_path: str, trigger: str = "uploaded_audio") -> dict:
    """
    Transcribe and parse an existing local audio file.

    Args:
        audio_path (str): Path to a locally saved audio file.
        trigger (str): Label describing how the audio was captured.

    Returns:
        dict: Structured intent payload for the Flask app and frontend.
    """
    normalized_trigger = (trigger or "uploaded_audio").strip() or "uploaded_audio"

    try:
        return _process_audio_path(audio_path=audio_path, trigger=normalized_trigger)
    except Exception as exc:
        raise RuntimeError(f"Voice intent pipeline failed: {exc}") from exc


def parse_text_command(transcript: str, trigger: str = "text") -> dict:
    """
    Parse an already-transcribed text command into the standard intent shape.

    Args:
        transcript (str): Natural-language command text.
        trigger (str): Label describing where the text originated.

    Returns:
        dict: Structured intent payload for the assistant pipeline.
    """
    normalized_trigger = (trigger or "text").strip() or "text"
    clean_transcript = re.sub(r"\s+", " ", str(transcript or "").strip())
    if not clean_transcript:
        return {
            "intent": "clarify",
            "parameters": {"follow_up": "I did not catch any words. Please try again."},
            "raw_transcript": "",
            "trigger": normalized_trigger,
            "confidence": 0.0,
            "clarification_prompt": "I did not catch any words. Please try again.",
        }

    try:
        intent_result = _ground_intent_result(_parse_intent(clean_transcript), clean_transcript)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Falling back to clarify after malformed intent payload: %s", exc)
        intent_result = _build_clarify_result(
            transcript=clean_transcript,
            prompt=_DEFAULT_CLARIFICATION_PROMPT,
            confidence=0.0,
        )

    response = {
        "intent": intent_result["intent"],
        "parameters": intent_result.get("parameters", {}),
        "raw_transcript": clean_transcript,
        "trigger": normalized_trigger,
        "confidence": _clamp_confidence(intent_result.get("confidence", 0.0)),
    }
    if response["intent"] == "clarify":
        response["clarification_prompt"] = (
            intent_result.get("clarification_prompt")
            or response["parameters"].get("follow_up")
            or _DEFAULT_CLARIFICATION_PROMPT
        )
    return response


def _record_audio() -> str:
    """
    Record microphone input to a temporary WAV file using simple VAD.

    Returns:
        str: Absolute path to the saved WAV file.
    """
    import numpy as np
    import sounddevice as sd
    from scipy.io import wavfile

    requested_sample_rate = _get_env_int("AUDIO_SAMPLE_RATE", 16000)
    max_duration_s = _get_env_float("AUDIO_MAX_DURATION_S", 8.0)
    silence_threshold = _get_env_float("VAD_SILENCE_THRESHOLD", 0.01)
    silence_duration_s = _get_env_float("VAD_SILENCE_DURATION_S", 1.5)
    speech_factor = max(1.5, _get_env_float("VAD_SPEECH_FACTOR", 3.0))
    fallback_signal = max(0.0005, _get_env_float("VAD_FALLBACK_SIGNAL", _DEFAULT_FALLBACK_SIGNAL))
    noise_calibration_s = max(_CHUNK_DURATION_S, _get_env_float("AUDIO_NOISE_CALIBRATION_S", 0.6))
    min_speech_duration_s = max(_CHUNK_DURATION_S, _get_env_float("VAD_MIN_SPEECH_DURATION_S", 0.3))
    input_device = _get_audio_input_device()

    tmp_dir = _get_tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_audio_files(tmp_dir)

    max_chunks = max(1, int(max_duration_s / _CHUNK_DURATION_S))
    silence_chunks_to_stop = max(1, int(silence_duration_s / _CHUNK_DURATION_S))
    prespeech_chunks = max(1, int(_PRESPEECH_BUFFER_S / _CHUNK_DURATION_S))
    noise_calibration_chunks = max(3, int(noise_calibration_s / _CHUNK_DURATION_S))
    min_speech_chunks = max(1, int(min_speech_duration_s / _CHUNK_DURATION_S))

    try:
        device_info = sd.query_devices(device=input_device, kind="input")
    except Exception as exc:
        raise RuntimeError("No usable input microphone was found.") from exc

    sample_rate = _resolve_input_sample_rate(device_info, requested_sample_rate)
    chunk_size = max(1, int(sample_rate * _CHUNK_DURATION_S))

    speech_started = False
    silent_chunks = 0
    frames: list[Any] = []
    all_frames: list[Any] = []
    buffer: deque[Any] = deque(maxlen=prespeech_chunks)
    noise_levels: deque[float] = deque(maxlen=noise_calibration_chunks)
    max_rms = 0.0
    speech_frame_count = 0

    try:
        with sd.InputStream(
            device=input_device,
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        ) as stream:
            for _ in range(max_chunks):
                chunk, overflowed = stream.read(chunk_size)
                if overflowed:
                    logger.warning("Microphone input overflow detected during capture.")

                chunk = np.copy(chunk)
                rms = _compute_normalized_rms(chunk)
                max_rms = max(max_rms, rms)
                all_frames.append(chunk)

                if not speech_started:
                    buffer.append(chunk)
                    noise_levels.append(rms)
                    activation_threshold = _resolve_activation_threshold(
                        noise_levels=noise_levels,
                        configured_threshold=silence_threshold,
                        speech_factor=speech_factor,
                        fallback_signal=fallback_signal,
                    )
                    if rms >= activation_threshold:
                        speech_started = True
                        frames.extend(list(buffer))
                        buffer.clear()
                        speech_frame_count = len(frames)
                    continue

                frames.append(chunk)
                speech_frame_count += 1
                end_threshold = max(fallback_signal * 0.75, silence_threshold * 0.75)
                if rms < end_threshold and speech_frame_count >= min_speech_chunks:
                    silent_chunks += 1
                    if silent_chunks >= silence_chunks_to_stop:
                        break
                else:
                    silent_chunks = 0
    except Exception as exc:
        raise RuntimeError(f"Audio capture failed: {exc}") from exc

    if frames:
        audio = np.concatenate(frames, axis=0)
    elif all_frames:
        audio = np.concatenate(all_frames, axis=0)
        if max_rms < fallback_signal:
            raise NoSpeechDetectedError("No speech was detected from the microphone.")
        logger.info(
            "Falling back to full captured audio because VAD did not trigger; peak RMS was %.5f.",
            max_rms,
        )
    else:
        raise NoSpeechDetectedError("No speech was detected from the microphone.")

    if audio.size == 0:
        raise NoSpeechDetectedError("Captured audio was empty.")

    audio = _normalize_audio(audio)
    wav_path = tmp_dir / f"voice_{int(time.time() * 1000)}.wav"
    wavfile.write(str(wav_path), sample_rate, audio)
    return str(wav_path)


def _transcribe(wav_path: str) -> str:
    """
    Transcribe a local audio file with Whisper.

    Args:
        wav_path (str): Absolute path to a local audio file.

    Returns:
        str: Cleaned transcript text.
    """
    wav_file = Path(wav_path)
    if not wav_file.exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")

    _ensure_ffmpeg_on_path()
    language = _get_whisper_language()
    initial_prompt = _build_whisper_prompt()
    prepared_audio_path = _prepare_audio_for_whisper(wav_file)

    try:
        transcript = _transcribe_with_available_backend(
            prepared_audio_path=prepared_audio_path,
            language=language,
            initial_prompt=initial_prompt,
        )
    except Exception as exc:
        hint = " Ensure ffmpeg is installed and available on PATH." if "ffmpeg" in str(exc).lower() else ""
        raise RuntimeError(f"Whisper transcription failed.{hint}") from exc
    finally:
        if prepared_audio_path != wav_file:
            try:
                prepared_audio_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove prepared audio file: %s", prepared_audio_path)

    transcript = re.sub(r"\s+", " ", transcript)
    if _looks_like_hallucinated_transcript(transcript):
        logger.warning("Discarding likely hallucinated transcript: %s", transcript[:180])
        return ""
    return transcript


def _parse_intent(transcript: str) -> dict:
    """
    Send the transcript to Ollama and parse the returned JSON payload.

    Args:
        transcript (str): Whisper transcript.

    Returns:
        dict: Parsed intent with parameters and confidence.
    """
    clean_transcript = transcript.strip()
    if not clean_transcript:
        return _build_clarify_result("", "I did not catch that. Please try again.", 0.0)

    fast_path_result = _fast_path_intent(clean_transcript)
    if fast_path_result is not None:
        return fast_path_result

    cloud_result = cloud_router.classify_intent(clean_transcript)
    if cloud_result is not None:
        logger.info("Intent classified via hybrid cloud router.")
        return _normalize_intent_result(cloud_result, clean_transcript)

    host = (os.getenv("OLLAMA_HOST", "http://localhost:11434").strip() or "http://localhost:11434").rstrip("/")
    model_name = os.getenv("INTENT_MODEL", "mistral").strip() or "mistral"
    timeout_s = max(30.0, _get_env_float("OLLAMA_INTENT_TIMEOUT_S", 120.0))

    prompt = _build_intent_prompt(clean_transcript)
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }

    try:
        response = requests.post(f"{host}/api/generate", json=payload, timeout=(5, timeout_s))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Ollama at {host}: {exc}") from exc

    raw_content = str(response.json().get("response", "")).strip()
    parsed = _load_ollama_json(raw_content)
    return _normalize_intent_result(parsed, clean_transcript)


def _process_audio_path(audio_path: str, trigger: str) -> dict:
    """
    Run transcription and intent parsing on an existing audio file path.

    Args:
        audio_path (str): Path to the audio file to process.
        trigger (str): Trigger label to include in the response.

    Returns:
        dict: Structured intent payload for the Flask app and frontend.
    """
    transcript = _transcribe(audio_path)

    if not transcript:
        intent_result = _build_clarify_result(
            transcript="",
            prompt="I did not catch any words. Please try again.",
            confidence=0.0,
        )
    else:
        try:
            intent_result = _parse_intent(transcript)
            intent_result = _ground_intent_result(intent_result, transcript)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Falling back to clarify after malformed Ollama JSON: %s", exc)
            intent_result = _build_clarify_result(
                transcript=transcript,
                prompt=_DEFAULT_CLARIFICATION_PROMPT,
                confidence=0.0,
            )

    response = {
        "intent": intent_result["intent"],
        "parameters": intent_result.get("parameters", {}),
        "raw_transcript": transcript,
        "trigger": trigger,
        "confidence": _clamp_confidence(intent_result.get("confidence", 0.0)),
    }

    clarification_prompt = intent_result.get("clarification_prompt")
    if response["intent"] == "clarify":
        response["clarification_prompt"] = (
            clarification_prompt
            or response["parameters"].get("follow_up")
            or _DEFAULT_CLARIFICATION_PROMPT
        )

    return response


def _build_clarify_result(transcript: str, prompt: str, confidence: float = 0.0) -> dict:
    """
    Build a consistent clarify response.

    Args:
        transcript (str): Original transcript text.
        prompt (str): User-facing follow-up question.
        confidence (float): Confidence score to attach.

    Returns:
        dict: Clarify intent payload.
    """
    follow_up = prompt.strip() or _DEFAULT_CLARIFICATION_PROMPT
    return {
        "intent": "clarify",
        "parameters": {"follow_up": follow_up},
        "confidence": _clamp_confidence(confidence),
        "clarification_prompt": follow_up,
        "raw_transcript": transcript,
    }


def _build_intent_prompt(transcript: str) -> str:
    """
    Build an intent prompt enriched with the current machine context.

    Args:
        transcript (str): Whisper transcript text.

    Returns:
        str: Prompt for the local intent model.
    """
    known_apps = world_model.get_known_app_names(limit=40)
    relevant_memories = world_model.search_memories(transcript, limit=4)
    workspace_dir = os.getenv("WORKSPACE_DIR", "").strip()
    app_bias = ", ".join(known_apps) if known_apps else "Claude, ChatGPT, Chrome, Notepad, VS Code"
    memory_bias = "\n".join(f"- {memory}" for memory in relevant_memories) if relevant_memories else "- None yet"

    return (
        f"{_SYSTEM_PROMPT}\n\n"
        "Local machine context:\n"
        f"- Known installed or discovered apps: {app_bias}\n"
        f"- Primary workspace directory: {workspace_dir or 'Unknown'}\n\n"
        "Relevant long-term memory:\n"
        f"{memory_bias}\n\n"
        f'Transcript: "{transcript}"\n\n'
        "Return only the JSON object."
    )


def _build_whisper_prompt() -> str:
    """
    Build a Whisper initial prompt enriched with local app names.

    Returns:
        str: Prompt bias for local ASR.
    """
    configured_prompt = os.getenv("WHISPER_INITIAL_PROMPT", "").strip()
    if configured_prompt:
        return configured_prompt

    known_apps = _filtered_prompt_app_names(limit=10)
    if not known_apps:
        return _DEFAULT_WHISPER_PROMPT

    return (
        f"{_DEFAULT_WHISPER_PROMPT} "
        "Likely spoken app names on this machine include "
        + ", ".join(known_apps[:10])
        + "."
    )


def _filtered_prompt_app_names(limit: int = 10) -> list[str]:
    excluded_terms = (
        "helper",
        "service",
        "extension",
        "server",
        "runtime",
        "package",
        "proxy",
        "update",
        "host",
        "admin",
        "policy",
        "installer",
        "crash",
        "icloud",
        "onedrive",
        "widget",
        "teamsupdate",
    )
    filtered: list[str] = []
    for name in world_model.get_known_app_names(limit=80):
        cleaned = str(name or "").strip()
        lowered = cleaned.lower()
        if not cleaned or len(cleaned) > 28:
            continue
        if any(term in lowered for term in excluded_terms):
            continue
        if re.search(r"[a-z]+[A-Z][a-z]+[A-Z][A-Za-z]*", cleaned) and " " not in cleaned:
            continue
        if re.search(r"[A-Z].*[A-Z].*[A-Z].*[A-Z]", cleaned) and " " not in cleaned:
            continue
        filtered.append(cleaned)
        if len(filtered) >= limit:
            break
    return filtered


def _looks_like_hallucinated_transcript(transcript: str) -> bool:
    cleaned = re.sub(r"\s+", " ", str(transcript or "").strip())
    if not cleaned:
        return False

    tokens = re.findall(r"[A-Za-z0-9]+", cleaned)
    if len(tokens) < 8:
        return False

    lowered_tokens = [token.lower() for token in tokens]
    counts = Counter(lowered_tokens)
    most_common_token, most_common_count = counts.most_common(1)[0]

    if most_common_count >= 8 and most_common_count / max(len(lowered_tokens), 1) >= 0.45:
        return True

    repeated_run = 1
    longest_run = 1
    for current, nxt in zip(lowered_tokens, lowered_tokens[1:]):
        if current == nxt:
            repeated_run += 1
            longest_run = max(longest_run, repeated_run)
        else:
            repeated_run = 1

    if longest_run >= 5:
        return True

    if len(cleaned) > 120 and len(counts) <= max(4, len(lowered_tokens) // 8):
        return True

    return False


def _ground_intent_result(intent_result: dict[str, Any], transcript: str) -> dict[str, Any]:
    """
    Ground parsed intents against the local machine model before execution.

    Args:
        intent_result (dict[str, Any]): Parsed intent output.
        transcript (str): Original transcript.

    Returns:
        dict[str, Any]: Potentially corrected intent payload.
    """
    intent_name = str(intent_result.get("intent", "")).strip().lower()
    parameters = dict(intent_result.get("parameters", {}))

    if intent_name == "open_app":
        requested_name = _stringify_param(parameters.get("app_name")) or _extract_app_name(transcript)
        if requested_name:
            resolved_app = world_model.resolve_app(requested_name, refresh_if_needed=False)
            if resolved_app:
                parameters["app_name"] = str(resolved_app.get("display_name") or requested_name)
            else:
                suggestions = world_model.suggest_apps(requested_name, limit=3)
                if suggestions and intent_result.get("confidence", 0.0) < 0.65:
                    follow_up = "Did you mean " + ", ".join(
                        str(item.get("display_name")) for item in suggestions if item.get("display_name")
                    ) + "?"
                    return _build_clarify_result(transcript, follow_up, intent_result.get("confidence", 0.0))

    if intent_name == "search_pc":
        query = _stringify_param(parameters.get("query")) or transcript.strip()
        web_query = _extract_web_search_query(query) or _extract_web_search_query(transcript)
        if web_query:
            intent_result["intent"] = "web_search"
            parameters = {"query": web_query}
        else:
            parameters["query"] = query

    if intent_name == "general":
        raw_text = _stringify_param(parameters.get("raw_text")) or transcript.strip()
        web_query = _extract_web_search_query(raw_text) or _extract_web_search_query(transcript)
        if web_query:
            intent_result["intent"] = "web_search"
            parameters = {"query": web_query}

    if intent_name == "system_query":
        query = _stringify_param(parameters.get("query")) or transcript.strip()
        if not _extract_system_query(query) and not _extract_system_query(transcript):
            intent_result["intent"] = "general"
            parameters = {"raw_text": transcript.strip()}
        else:
            parameters["query"] = query

    intent_result["parameters"] = parameters
    return intent_result


def _normalize_intent_result(parsed: Any, transcript: str) -> dict:
    """
    Validate and normalize the Ollama response into the expected schema.

    Args:
        parsed (Any): Decoded JSON from Ollama.
        transcript (str): Original transcript for fallback parameter filling.

    Returns:
        dict: Intent payload safe for the rest of the app.
    """
    if not isinstance(parsed, dict):
        raise ValueError("Ollama response must decode to a JSON object.")

    raw_intent = str(parsed.get("intent", "")).strip().lower()
    parameters = parsed.get("parameters", {})
    raw_confidence = parsed.get("confidence")
    confidence = (
        _clamp_confidence(raw_confidence)
        if raw_confidence is not None
        else _DEFAULT_MODEL_CONFIDENCE
    )

    if not isinstance(parameters, dict):
        parameters = {}

    if raw_intent not in _ALLOWED_INTENTS:
        return _build_clarify_result(transcript, _DEFAULT_CLARIFICATION_PROMPT, confidence)

    normalized = {
        "intent": raw_intent,
        "parameters": _fill_parameters(raw_intent, parameters, transcript),
        "confidence": confidence,
    }

    if normalized["intent"] == "clarify":
        follow_up = normalized["parameters"].get("follow_up") or _DEFAULT_CLARIFICATION_PROMPT
        return _build_clarify_result(transcript, follow_up, confidence)

    if normalized["confidence"] < _get_env_float("CLARIFY_THRESHOLD", 0.5):
        return _build_clarify_result(transcript, _DEFAULT_CLARIFICATION_PROMPT, normalized["confidence"])

    if _is_ambiguous(normalized["intent"], normalized["parameters"]):
        return _build_clarify_result(transcript, _DEFAULT_CLARIFICATION_PROMPT, normalized["confidence"])

    return normalized


def _fill_parameters(intent: str, parameters: dict[str, Any], transcript: str) -> dict[str, Any]:
    """
    Backfill missing parameters from the transcript when possible.

    Args:
        intent (str): Selected intent.
        parameters (dict[str, Any]): Parameters returned by Ollama.
        transcript (str): Original transcript.

    Returns:
        dict[str, Any]: Normalized parameter dictionary.
    """
    params = {str(key): value for key, value in parameters.items()}
    clean_transcript = transcript.strip()

    if intent == "open_app":
        app_name = _stringify_param(params.get("app_name"))
        if not app_name:
            app_name = _extract_app_name(clean_transcript)
        return {"app_name": app_name}

    if intent == "create_file":
        file_name = _stringify_param(params.get("file_name"))
        file_type = _stringify_param(params.get("file_type")).lstrip(".")
        content_value = params.get("content")
        content = content_value if isinstance(content_value, str) else _stringify_param(content_value)

        inferred_name, inferred_type = _extract_file_details(clean_transcript)
        if not file_name and inferred_name:
            file_name = inferred_name
        if not file_type and inferred_type:
            file_type = inferred_type
        if not content:
            content = _extract_file_content(clean_transcript)

        if file_name and "." in file_name and not file_type:
            path_name = Path(file_name)
            if path_name.suffix:
                file_type = path_name.suffix.lstrip(".")
                file_name = path_name.stem

        return {
            "file_name": file_name,
            "file_type": file_type or "txt",
            "content": content,
        }

    if intent == "create_app":
        description = _stringify_param(params.get("description"))
        if not description:
            description = _extract_app_description(clean_transcript)
        return {"description": description}

    if intent == "search_pc":
        query = _stringify_param(params.get("query")) or clean_transcript
        return {"query": query}

    if intent == "web_search":
        query = _stringify_param(params.get("query")) or _extract_web_search_query(clean_transcript) or clean_transcript
        return {"query": query}

    if intent == "system_query":
        query = _stringify_param(params.get("query")) or clean_transcript
        return {"query": query}

    if intent == "general":
        raw_text = _stringify_param(params.get("raw_text")) or clean_transcript
        return {"raw_text": raw_text}

    if intent == "clarify":
        follow_up = _stringify_param(params.get("follow_up")) or _DEFAULT_CLARIFICATION_PROMPT
        return {"follow_up": follow_up}

    return params


def _is_ambiguous(intent: str, parameters: dict[str, Any]) -> bool:
    """
    Check whether an intent is missing the minimum parameters needed to act.

    Args:
        intent (str): Selected intent.
        parameters (dict[str, Any]): Normalized parameters.

    Returns:
        bool: True when the request should fall back to clarify.
    """
    if intent == "open_app":
        return not str(parameters.get("app_name", "")).strip()
    if intent == "create_file":
        return not str(parameters.get("file_name", "")).strip()
    if intent == "create_app":
        return not str(parameters.get("description", "")).strip()
    if intent in {"search_pc", "web_search", "system_query"}:
        return not str(parameters.get("query", "")).strip()
    return False


def _fast_path_intent(transcript: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"\s+", " ", str(transcript or "").strip()).strip()
    if not cleaned:
        return None

    web_query = _extract_web_search_query(cleaned)
    if web_query:
        return {
            "intent": "web_search",
            "parameters": {"query": web_query},
            "confidence": 0.99,
        }

    if _is_claude_task_request(cleaned):
        return {
            "intent": "general",
            "parameters": {"raw_text": cleaned},
            "confidence": 0.98,
        }

    if _is_codex_task_request(cleaned):
        return {
            "intent": "general",
            "parameters": {"raw_text": cleaned},
            "confidence": 0.98,
        }

    if _is_app_search_request(cleaned):
        return {
            "intent": "general",
            "parameters": {"raw_text": cleaned},
            "confidence": 0.98,
        }

    if _is_app_message_request(cleaned):
        return {
            "intent": "general",
            "parameters": {"raw_text": cleaned},
            "confidence": 0.98,
        }

    if _is_window_interaction_request(cleaned):
        return {
            "intent": "general",
            "parameters": {"raw_text": cleaned},
            "confidence": 0.98,
        }

    if _is_launch_minecraft_request(cleaned):
        return {
            "intent": "general",
            "parameters": {"raw_text": cleaned},
            "confidence": 0.98,
        }

    open_app_name = _extract_fast_open_app_name(cleaned)
    if open_app_name:
        return {
            "intent": "open_app",
            "parameters": {"app_name": open_app_name},
            "confidence": 0.97,
        }

    create_file_params = _extract_fast_create_file_params(cleaned)
    if create_file_params is not None:
        return {
            "intent": "create_file",
            "parameters": create_file_params,
            "confidence": 0.97,
        }

    create_app_description = _extract_fast_create_app_description(cleaned)
    if create_app_description:
        return {
            "intent": "create_app",
            "parameters": {"description": create_app_description},
            "confidence": 0.96,
        }

    local_search_query = _extract_local_search_query(cleaned)
    if local_search_query:
        return {
            "intent": "search_pc",
            "parameters": {"query": local_search_query},
            "confidence": 0.96,
        }

    system_query = _extract_system_query(cleaned)
    if system_query:
        return {
            "intent": "system_query",
            "parameters": {"query": system_query},
            "confidence": 0.96,
        }

    if _is_memory_general_request(cleaned):
        return {
            "intent": "general",
            "parameters": {"raw_text": cleaned},
            "confidence": 0.95,
        }

    return None


def _extract_web_search_query(transcript: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(transcript or "").strip()).strip(" .?!")
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


def _extract_fast_open_app_name(transcript: str) -> str:
    if _looks_multi_part_command(transcript):
        return ""

    app_name = _extract_app_name(transcript)
    normalized_app_name = _normalize_spoken_text(app_name)
    if not normalized_app_name:
        return ""

    blocked_terms = {
        "file",
        "folder",
        "document",
        "documents",
        "downloads",
        "desktop",
        "directory",
        "path",
        "website",
        "url",
        "browser search",
        "google search",
    }
    if normalized_app_name in blocked_terms:
        return ""

    if world_model.resolve_app(app_name, refresh_if_needed=False):
        return app_name

    if len(normalized_app_name.split()) <= 4 and len(normalized_app_name) <= 32:
        return app_name

    return ""


def _extract_fast_create_file_params(transcript: str) -> dict[str, Any] | None:
    lowered = transcript.lower()
    if not lowered.startswith(("create ", "make ", "generate ")):
        return None
    if " file" not in lowered and not re.search(r"\.[A-Za-z0-9]{1,6}\b", transcript):
        return None
    if " app" in lowered or " application" in lowered:
        return None

    file_name, file_type = _extract_file_details(transcript)
    if not file_name:
        return None

    return {
        "file_name": file_name,
        "file_type": file_type or "txt",
        "content": _extract_file_content(transcript),
    }


def _extract_fast_create_app_description(transcript: str) -> str:
    lowered = transcript.lower()
    # Verbs that introduce a "build me X" intent. "Write" is here because
    # phrasings like "write a CLI tool that ..." or "write a python script
    # that ..." are conceptually create_app requests, not file-write requests.
    if not lowered.startswith(("build ", "create ", "make ", "generate ", "write ", "code ")):
        return ""
    # "create a file" / "make a file" remain create_file requests.
    if " file" in lowered:
        return ""
    # Nouns that signal "a project/deliverable, not chat". Includes "script"
    # so "write a python script that ..." is treated as create_app and reaches
    # the Claude / Codex router instead of the general planner.
    if not any(token in lowered for token in (
        " app", " application", " website", " site", " tool", " program",
        " script", " utility", " cli ", " cli tool", " command line",
        " command-line", " function", " regex",
    )):
        return ""
    description = _extract_app_description(transcript)
    return description if len(description) >= 6 else ""


def _extract_local_search_query(transcript: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(transcript or "").strip()).strip(" .?!")
    lowered = cleaned.lower()
    if not cleaned or _extract_web_search_query(cleaned):
        return ""

    patterns = (
        r"^(?:please\s+)?search\s+(?:my\s+)?(?:pc|computer|files?)\s+for\s+(.+)$",
        r"^(?:please\s+)?look\s+for\s+(.+?)\s+(?:on|in)\s+my\s+(?:pc|computer|files?|desktop|documents|downloads)$",
        r"^(?:please\s+)?find\s+(.+?)\s+(?:on|in)\s+my\s+(?:pc|computer|files?|desktop|documents|downloads)$",
        r"^(?:please\s+)?locate\s+(.+)$",
        r"^(?:please\s+)?find\s+my\s+(.+)$",
    )

    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            query = match.group(1).strip(" .?!")
            query = re.sub(r"^(?:the\s+)", "", query, flags=re.IGNORECASE)
            return query

    if lowered.startswith("find ") and any(marker in lowered for marker in (" file", " folder", " document", " resume", " pdf", " notes")):
        return cleaned[5:].strip(" .?!")

    return ""


def _extract_system_query(transcript: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(transcript or "").strip()).strip(" .?!")
    lowered = cleaned.lower()
    if not cleaned:
        return ""

    question_starters = ("what", "which", "show", "list", "how much", "how many", "tell me")
    keywords = (
        "running app",
        "running apps",
        "running process",
        "running processes",
        "apps are running",
        "processes are running",
        "what apps",
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
    if lowered.startswith(question_starters) and any(keyword in lowered for keyword in keywords):
        return cleaned
    return ""


def _is_memory_general_request(transcript: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(transcript or "").strip()).lower()
    return lowered.startswith((
        "remember that ",
        "remember this ",
        "what do you remember",
        "do you remember",
        "what do you know about me",
    ))


def _is_codex_task_request(transcript: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(transcript or "").strip().lower()).strip(" .?!")
    patterns = (
        r"^(?:please\s+)?(?:use|ask|tell|run)\s+codex\s+(?:to\s+)?\S+",
        r"^(?:please\s+)?(?:have|let)\s+codex\s+\S+",
        r"^codex\s+\S+",
    )
    return any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)


def _is_claude_task_request(transcript: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(transcript or "").strip().lower()).strip(" .?!")
    patterns = (
        r"^(?:please\s+)?(?:use|ask|tell|run)\s+claude(?:\s+code)?\s+(?:to\s+)?\S+",
        r"^(?:please\s+)?(?:have|let)\s+claude(?:\s+code)?\s+\S+",
        r"^claude(?:\s+code)?\s+\S+",
    )
    return any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)


def _is_launch_minecraft_request(transcript: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(transcript or "").strip().lower()).strip(" .?!")
    return lowered in {
        "launch minecraft",
        "open minecraft",
        "start minecraft",
        "run minecraft",
    }


def _is_app_search_request(transcript: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(transcript or "").strip().lower()).strip(" .?!")
    patterns = (
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+.+?\s+and\s+(?:search(?:\s+up)?|look\s+up|google|ask(?:\s+(?:about|me))?|find(?:\s+(?:out|me))?|tell\s+me\s+about)\s+.+$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+.+?\s+and\s+search\s+for\s+.+$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+.+?\s+and\s+look\s+for\s+.+$",
    )
    return any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)


def _is_app_message_request(transcript: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(transcript or "").strip().lower()).strip(" .?!")
    patterns = (
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+.+?\s+(?:and\s+)?search(?:\s+up)?\s+(?:for\s+)?.+?\s+and\s+(?:write|send|message|say|tell|text)\s+.+$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+.+?\s+(?:and\s+)?message\s+.+?\s+(?:that\s+|saying\s+|with\s+the\s+message\s+)?.+$",
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+.+?\s+(?:and\s+)?(?:tell|text)\s+(?!me\b)[a-z][a-z0-9 .'-]{0,40}\s+(?:that\s+|saying\s+)?.+$",
        r"^(?:please\s+)?send\s+.+?\s+(?:a\s+)?message\s+on\s+.+?\s+(?:that\s+|saying\s+)?.+$",
        r"^(?:please\s+)?message\s+.+?\s+on\s+.+?\s+(?:that\s+|saying\s+)?.+$",
    )
    return any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)


def _is_window_interaction_request(transcript: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(transcript or "").strip().lower()).strip(" .?!")
    patterns = (
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+.+?\s+and\s+(?:type|write|paste|enter)\s+.+$",
        r"^(?:please\s+)?(?:type|write|paste|enter|search(?:\s+up)?(?:\s+for)?)\s+.+?\s+(?:in|into|on)\s+.+$",
        r"^(?:please\s+)?(?:in|into|on)\s+.+?\s+(?:type|write|paste|enter|search(?:\s+up)?(?:\s+for)?)\s+.+$",
        r"^(?:please\s+)?(?:click|press|hit)\s+.+?\s+(?:in|on)\s+.+$",
        r"^(?:please\s+)?(?:in|on)\s+.+?\s+(?:click|press|hit)\s+.+$",
    )
    return any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)


def _looks_multi_part_command(transcript: str) -> bool:
    lowered = str(transcript or "").strip().lower()
    if not lowered:
        return False
    markers = (
        " and then ",
        " after that ",
        " then ",
        " once ",
        " let me know ",
        " tell me ",
        " when it is ready ",
        " when it's ready ",
        " if that works ",
    )
    return any(marker in lowered for marker in markers)


def _normalize_spoken_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _transcribe_with_available_backend(
    prepared_audio_path: Path,
    language: str | None,
    initial_prompt: str,
) -> str:
    backend = _get_asr_backend()
    if backend in {"auto", "faster-whisper"} and _faster_whisper_available():
        try:
            return _transcribe_with_faster_whisper(
                prepared_audio_path=prepared_audio_path,
                language=language,
                initial_prompt=initial_prompt,
            )
        except Exception as exc:
            if backend == "faster-whisper":
                raise
            logger.warning("Faster-Whisper failed, falling back to openai-whisper: %s", exc)

    return _transcribe_with_openai_whisper(
        prepared_audio_path=prepared_audio_path,
        language=language,
        initial_prompt=initial_prompt,
    )


def _get_asr_backend() -> str:
    raw_backend = os.getenv("ASR_BACKEND", "").strip().lower() or os.getenv("WHISPER_BACKEND", "").strip().lower()
    if raw_backend in {"faster-whisper", "openai-whisper", "whisper", "auto"}:
        return "openai-whisper" if raw_backend == "whisper" else raw_backend
    return "auto"


def _faster_whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def _transcribe_with_faster_whisper(
    prepared_audio_path: Path,
    language: str | None,
    initial_prompt: str,
) -> str:
    model = _get_faster_whisper_model()
    beam_size = max(1, _get_env_int("FASTER_WHISPER_BEAM_SIZE", 1))
    segments, _ = model.transcribe(
        str(prepared_audio_path),
        language=language,
        task="transcribe",
        condition_on_previous_text=False,
        initial_prompt=initial_prompt,
        temperature=0.0,
        beam_size=beam_size,
        vad_filter=True,
        without_timestamps=True,
    )
    transcript = " ".join(str(segment.text).strip() for segment in segments if str(segment.text).strip())
    return transcript.strip()


def _get_faster_whisper_model() -> Any:
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "small").strip() or "small"
    preferred_device = _detect_whisper_device()
    preferred_compute_type = (
        os.getenv("FASTER_WHISPER_COMPUTE_TYPE", "").strip()
        or ("float16" if preferred_device == "cuda" else "int8")
    )
    cache_key = (model_name, preferred_device, preferred_compute_type)

    with _MODEL_LOCK:
        if cache_key in _FASTER_MODEL_CACHE:
            return _FASTER_MODEL_CACHE[cache_key]

        compute_type_candidates = [preferred_compute_type]
        if preferred_compute_type != "int8":
            compute_type_candidates.append("int8")
        if preferred_compute_type != "float32":
            compute_type_candidates.append("float32")

        last_error: Exception | None = None
        for compute_type in compute_type_candidates:
            try:
                model = WhisperModel(model_name, device=preferred_device, compute_type=compute_type)
                _FASTER_MODEL_CACHE[(model_name, preferred_device, compute_type)] = model
                return model
            except Exception as exc:
                last_error = exc
                continue

    raise RuntimeError(f"Failed to load Faster-Whisper model '{model_name}'.") from last_error


def _transcribe_with_openai_whisper(
    prepared_audio_path: Path,
    language: str | None,
    initial_prompt: str,
) -> str:
    model = _get_whisper_model()
    device = str(getattr(model, "device", "cpu"))
    use_fp16 = device.startswith("cuda")
    audio_input = _load_prepared_audio(prepared_audio_path)
    result = model.transcribe(
        audio_input,
        fp16=use_fp16,
        language=language,
        task="transcribe",
        temperature=0,
        verbose=False,
        condition_on_previous_text=False,
        initial_prompt=initial_prompt,
        no_speech_threshold=0.45,
        logprob_threshold=-0.9,
        compression_ratio_threshold=2.2,
    )
    return str(result.get("text", "")).strip()


def _get_whisper_model() -> Any:
    """
    Lazily load and cache the configured Whisper model.

    Returns:
        Any: Loaded Whisper model instance.
    """
    import whisper

    model_name = os.getenv("WHISPER_MODEL", "small").strip() or "small"
    preferred_device = _detect_whisper_device()
    cache_key = (model_name, preferred_device)

    with _MODEL_LOCK:
        if cache_key in _MODEL_CACHE:
            return _MODEL_CACHE[cache_key]

        try:
            model = whisper.load_model(model_name, device=preferred_device)
        except Exception as exc:
            if preferred_device == "cuda":
                logger.warning("Failed to load Whisper on CUDA, falling back to CPU: %s", exc)
                model = whisper.load_model(model_name, device="cpu")
                _MODEL_CACHE[(model_name, "cpu")] = model
                return model
            raise RuntimeError(f"Failed to load Whisper model '{model_name}'.") from exc

        _MODEL_CACHE[cache_key] = model
        return model


def _detect_whisper_device() -> str:
    """
    Detect whether Whisper should run on CUDA or CPU.

    Returns:
        str: ``cuda`` when available, otherwise ``cpu``.
    """
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _load_ollama_json(raw_content: str) -> dict[str, Any]:
    """
    Extract a JSON object from Ollama output that may include extra wrapping.

    Args:
        raw_content (str): Raw text returned by Ollama.

    Returns:
        dict[str, Any]: Parsed JSON object.
    """
    candidate = raw_content.strip()
    if not candidate:
        raise ValueError("Ollama returned an empty response.")

    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)
    candidate = candidate.strip().strip("`").strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("Ollama did not return a JSON object.")
    return parsed


def _extract_app_name(transcript: str) -> str:
    """
    Pull an application name out of a spoken launch command.

    Args:
        transcript (str): Original transcript text.

    Returns:
        str: Best-effort application name.
    """
    patterns = [
        r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open|launch|start|run)\s+(?P<name>.+?)(?:\s+(?:for me|please))?$",
        r"^(?:i want to\s+)?(?:open|launch|start)\s+(?P<name>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, transcript, flags=re.IGNORECASE)
        if match:
            app_name = match.group("name").strip(" .,!?")
            app_name = re.sub(
                r"\s+(?:on|in)\s+my\s+(?:pc|computer|laptop|desktop)$",
                "",
                app_name,
                flags=re.IGNORECASE,
            )
            app_name = re.sub(r"\s+(?:for me|please)$", "", app_name, flags=re.IGNORECASE)
            return app_name.strip(" .,!?")
    return ""


def _extract_file_details(transcript: str) -> tuple[str, str]:
    """
    Infer a target file name and type from a transcript.

    Args:
        transcript (str): Original transcript text.

    Returns:
        tuple[str, str]: ``(file_name, file_type)``.
    """
    quoted_name = re.search(r'["\'](?P<name>[^"\']+\.[A-Za-z0-9]+)["\']', transcript)
    if quoted_name:
        path_name = Path(quoted_name.group("name"))
        return path_name.stem, path_name.suffix.lstrip(".")

    named_file = re.search(
        r"(?:called|named)\s+(?P<name>[A-Za-z0-9_.-]+)",
        transcript,
        flags=re.IGNORECASE,
    )
    if named_file:
        path_name = Path(named_file.group("name"))
        file_type = path_name.suffix.lstrip(".")
        return path_name.stem if path_name.suffix else path_name.name, file_type

    plain_file = re.search(
        r"(?:create|make)\s+(?:a\s+|an\s+)?(?:(?P<kind>python|text|markdown|json|html|javascript)\s+)?file(?:\s+called|\s+named)?\s+(?P<name>[A-Za-z0-9_.-]+)",
        transcript,
        flags=re.IGNORECASE,
    )
    if plain_file:
        raw_name = plain_file.group("name")
        raw_kind = (plain_file.group("kind") or "").lower()
        path_name = Path(raw_name)
        explicit_type = path_name.suffix.lstrip(".")
        inferred_type = explicit_type or _map_file_kind(raw_kind)
        return path_name.stem if path_name.suffix else path_name.name, inferred_type

    type_only = re.search(
        r"(?:create|make)\s+(?:a\s+|an\s+)?(?P<kind>python|text|markdown|json|html|javascript)\s+file",
        transcript,
        flags=re.IGNORECASE,
    )
    if type_only:
        return "", _map_file_kind(type_only.group("kind").lower())

    return "", ""


def _extract_file_content(transcript: str) -> str:
    """
    Extract optional initial file content from natural language.

    Args:
        transcript (str): Original transcript text.

    Returns:
        str: Best-effort file content text.
    """
    match = re.search(
        r"(?:with content|containing|that says|saying)\s+(.+)$",
        transcript,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _extract_app_description(transcript: str) -> str:
    """
    Extract a project description from a create-app style transcript.

    Args:
        transcript (str): Original transcript text.

    Returns:
        str: Best-effort project description.
    """
    cleaned = re.sub(
        r"^(?:please\s+)?(?:can you\s+)?(?:build|create|make|generate)\s+(?:me\s+)?",
        "",
        transcript,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" .!?")


def _map_file_kind(kind: str) -> str:
    """
    Map spoken file kinds to extensions.

    Args:
        kind (str): Spoken file type label.

    Returns:
        str: File extension without a leading dot.
    """
    mapping = {
        "python": "py",
        "text": "txt",
        "markdown": "md",
        "json": "json",
        "html": "html",
        "javascript": "js",
    }
    return mapping.get(kind.lower(), "") if kind else ""


def _compute_normalized_rms(chunk: Any) -> float:
    """
    Compute a 0.0-1.0 RMS loudness score for an ``int16`` audio chunk.

    Args:
        chunk (Any): NumPy audio array.

    Returns:
        float: Normalized RMS amplitude.
    """
    import numpy as np

    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)) / 32768.0)


def _cleanup_old_audio_files(tmp_dir: Path) -> None:
    """
    Remove stale temporary WAV files from the project temp directory.

    Args:
        tmp_dir (Path): Directory holding captured WAV files.
    """
    cutoff = time.time() - _TEMP_FILE_MAX_AGE_S
    for wav_file in tmp_dir.glob("*.wav"):
        try:
            if wav_file.stat().st_mtime < cutoff:
                wav_file.unlink(missing_ok=True)
        except OSError:
            logger.debug("Skipping cleanup for temp file: %s", wav_file)


def _resolve_activation_threshold(
    noise_levels: deque[float],
    configured_threshold: float,
    speech_factor: float,
    fallback_signal: float,
) -> float:
    """
    Compute a VAD trigger threshold from ambient noise and configured minimums.

    Args:
        noise_levels (deque[float]): Recent background RMS readings.
        configured_threshold (float): User-configured minimum trigger threshold.
        speech_factor (float): Multiplier applied to the ambient noise floor.
        fallback_signal (float): Absolute floor that still counts as usable speech.

    Returns:
        float: Threshold above which capture is treated as speech.
    """
    if not noise_levels:
        return max(configured_threshold, fallback_signal)

    ordered_levels = sorted(noise_levels)
    median_noise = ordered_levels[len(ordered_levels) // 2]
    return max(configured_threshold, median_noise * speech_factor, fallback_signal)


def _normalize_audio(audio: Any) -> Any:
    """
    Boost quiet recordings to a healthier level for Whisper without clipping.

    Args:
        audio (Any): NumPy int16 audio array.

    Returns:
        Any: Normalized audio array.
    """
    import numpy as np

    if getattr(audio, "size", 0) == 0:
        return audio

    peak = float(np.max(np.abs(audio.astype(np.int32))))
    if peak <= 0:
        return audio

    target_peak = 12000.0
    gain = min(8.0, target_peak / peak)
    if gain <= 1.1:
        return audio

    boosted = np.clip(audio.astype(np.float32) * gain, -32768, 32767)
    return boosted.astype(np.int16)


def _resolve_input_sample_rate(device_info: Any, requested_sample_rate: int) -> int:
    """
    Prefer the microphone's native sample rate when it differs materially.

    Args:
        device_info (Any): sounddevice device metadata.
        requested_sample_rate (int): Configured sample rate.

    Returns:
        int: Sample rate to use for the input stream.
    """
    try:
        default_sample_rate = int(round(float(device_info.get("default_samplerate", 0))))
    except (AttributeError, TypeError, ValueError):
        default_sample_rate = 0

    if default_sample_rate and abs(default_sample_rate - requested_sample_rate) >= 2000:
        return default_sample_rate
    return requested_sample_rate


def _ensure_ffmpeg_on_path() -> None:
    """
    Add a common local FFmpeg install directory to PATH when needed.
    """
    if shutil.which("ffmpeg"):
        return

    local_appdata = Path(os.getenv("LOCALAPPDATA", ""))
    candidate_dirs: list[Path] = []
    winget_root = local_appdata / "Microsoft" / "WinGet" / "Packages"
    if winget_root.exists():
        candidate_dirs.extend(path.parent for path in winget_root.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))

    for directory in candidate_dirs:
        ffmpeg_exe = directory / "ffmpeg.exe"
        if ffmpeg_exe.exists():
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            return


def _prepare_audio_for_whisper(audio_path: Path) -> Path:
    """
    Convert incoming audio to a normalized mono WAV for Whisper.

    Args:
        audio_path (Path): Original audio file path.

    Returns:
        Path: Temporary WAV file ready for transcription.
    """
    ffmpeg_command = _get_ffmpeg_command()
    tmp_dir = _get_tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    prepared_path = tmp_dir / f"prepared_{audio_path.stem}_{int(time.time() * 1000)}.wav"
    target_sample_rate = _get_env_int("AUDIO_SAMPLE_RATE", 16000)

    try:
        subprocess.run(
            [
                ffmpeg_command,
                "-y",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                str(target_sample_rate),
                "-c:a",
                "pcm_s16le",
                str(prepared_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg or add it to PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(f"Audio conversion failed: {stderr or exc}") from exc

    _normalize_wav_file(prepared_path)
    return prepared_path


def _normalize_wav_file(wav_path: Path) -> None:
    """
    Normalize a converted WAV file in place to help Whisper with quiet clips.

    Args:
        wav_path (Path): Path to a mono WAV file.
    """
    from scipy.io import wavfile

    try:
        sample_rate, audio = wavfile.read(str(wav_path))
    except Exception as exc:
        raise RuntimeError(f"Prepared audio could not be read: {wav_path}") from exc

    if getattr(audio, "ndim", 1) > 1:
        audio = audio[:, 0]

    normalized_audio = _normalize_audio(audio)
    wavfile.write(str(wav_path), sample_rate, normalized_audio)


def _load_prepared_audio(wav_path: Path) -> Any:
    """
    Load a prepared mono WAV file into float32 PCM samples for Whisper.

    Args:
        wav_path (Path): Path to a prepared WAV file.

    Returns:
        Any: NumPy float32 audio array in the -1.0 to 1.0 range.
    """
    import numpy as np
    from scipy.io import wavfile

    try:
        sample_rate, audio = wavfile.read(str(wav_path))
    except Exception as exc:
        raise RuntimeError(f"Prepared audio could not be loaded for Whisper: {wav_path}") from exc

    if sample_rate != _get_env_int("AUDIO_SAMPLE_RATE", 16000):
        raise RuntimeError(f"Prepared audio sample rate was unexpected: {sample_rate}")

    if getattr(audio, "ndim", 1) > 1:
        audio = audio[:, 0]

    if audio.dtype == np.int16:
        return audio.astype(np.float32) / 32768.0
    if audio.dtype == np.int32:
        return audio.astype(np.float32) / 2147483648.0
    if audio.dtype == np.uint8:
        return (audio.astype(np.float32) - 128.0) / 128.0
    return audio.astype(np.float32)


def _get_ffmpeg_command() -> str:
    """
    Resolve the FFmpeg executable path after PATH bootstrapping.

    Returns:
        str: Executable path or command name.
    """
    _ensure_ffmpeg_on_path()
    ffmpeg_command = shutil.which("ffmpeg")
    if ffmpeg_command:
        return ffmpeg_command
    raise RuntimeError("FFmpeg was not found. Install FFmpeg or add it to PATH.")


def _get_tmp_dir() -> Path:
    """
    Resolve the directory used for temporary voice recordings.

    Returns:
        Path: Project-local temp directory.
    """
    raw_tmp_dir = os.getenv("AUDIO_TMP_DIR", "").strip()
    normalized_raw = raw_tmp_dir.replace("\\", "/").lower()
    if raw_tmp_dir and normalized_raw not in _LEGACY_TMP_DIRS:
        return Path(raw_tmp_dir).expanduser()
    return _PROJECT_DIR / "tmp"


def _get_audio_input_device() -> int | str | None:
    """
    Resolve the optional microphone device from the environment.

    Returns:
        int | str | None: Numeric device index, device name, or None.
    """
    raw_value = os.getenv("AUDIO_INPUT_DEVICE", "").strip()
    if not raw_value:
        return None
    if raw_value.isdigit():
        return int(raw_value)
    return raw_value


def _get_whisper_language() -> str | None:
    """
    Resolve the transcription language, defaulting to English for PC commands.

    Returns:
        str | None: Language code, or None to let Whisper auto-detect.
    """
    raw_value = os.getenv("WHISPER_LANGUAGE", "").strip().lower()
    if not raw_value:
        return _DEFAULT_WHISPER_LANGUAGE
    if raw_value in {"auto", "detect"}:
        return None
    return raw_value


def _stringify_param(value: Any) -> str:
    """
    Convert a model parameter value to a trimmed string.

    Args:
        value (Any): Raw parameter value.

    Returns:
        str: Trimmed string or an empty string for null-like values.
    """
    if value is None:
        return ""
    return str(value).strip()


def _get_env_int(name: str, default: int) -> int:
    """
    Read an integer environment variable with a safe fallback.

    Args:
        name (str): Environment variable name.
        default (int): Fallback value.

    Returns:
        int: Parsed integer value.
    """
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid integer for %s=%r. Using default %s.", name, raw_value, default)
        return default


def _get_env_float(name: str, default: float) -> float:
    """
    Read a float environment variable with a safe fallback.

    Args:
        name (str): Environment variable name.
        default (float): Fallback value.

    Returns:
        float: Parsed float value.
    """
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("Invalid float for %s=%r. Using default %s.", name, raw_value, default)
        return default


def _clamp_confidence(value: Any) -> float:
    """
    Clamp a confidence-like value into the 0.0-1.0 range.

    Args:
        value (Any): Any numeric-ish value.

    Returns:
        float: Normalized confidence score.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, numeric))


__all__ = ["listen_and_parse", "parse_audio_file", "parse_text_command"]
