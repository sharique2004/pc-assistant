"""
wake_service.py — always-on local wake-word listener for the web UI.

A background thread continuously samples the default microphone, runs a local
Whisper (faster-whisper, tiny) transcription on each short chunk, and looks for
the wake word ("Bibi" by default).  When it hears it, the trailing command is
executed through the SAME local pipeline the /command endpoint uses
(voice_intent + executor) — no cloud, no extra round-trips.

Detected events are pushed to a thread-safe ring buffer that the web UI polls
via GET /wake/status, so the orb can light up and show the heard command and
result in real time.

This is a headless, GUI-free distillation of the logic in desktop_agent.py so
it can run inside the Flask process without dragging in Tkinter / winsound /
Windows-mutex code.

Python 3.11+
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from scipy.io import wavfile

import executor
import voice_intent
import orchestrator as _orch

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parent

_WAKE_ARTIFACT_PATTERNS = (
    r"^(?:what(?:'s| is)\s+the\s+wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
    r"^(?:the\s+wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
    r"^(?:wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
)


# ─────────────────────────────────────────────────────────────────────────────
# Wake-phrase parsing (ported verbatim from desktop_agent so behaviour matches)
# ─────────────────────────────────────────────────────────────────────────────
def _boost_audio(audio: "np.ndarray", target_peak: float = 0.6, max_gain: float = 12.0) -> "np.ndarray":
    """Amplify soft int16 audio toward a healthy peak so quiet speech is
    transcribed reliably (without forcing the user to speak loudly).  Pure
    silence (peak ~0) is left alone."""
    try:
        peak = float(np.max(np.abs(audio.astype(np.float32))))
        if peak < 1.0:
            return audio
        gain = min(max_gain, (target_peak * 32767.0) / peak)
        if gain <= 1.15:
            return audio
        boosted = np.clip(audio.astype(np.float32) * gain, -32768, 32767)
        return boosted.astype(np.int16)
    except Exception:
        return audio


def _normalize_token(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _strip_wake_artifacts(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    for pattern in _WAKE_ARTIFACT_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,.!?")


def extract_wake_command(transcript: str, wake_word: str) -> str | None:
    """Return the command after the wake word, "__wake_only__" if the wake word
    was heard alone, or None if the wake word was not present."""
    cleaned = re.sub(r"\s+", " ", str(transcript or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    normalized_wake = _normalize_token(wake_word)
    normalized_text = _normalize_token(cleaned)
    if not normalized_text:
        return None

    # Tight set — only close phonetic matches to "Bibi". Loose ones like
    # "baby"/"bb"/"vivi"/"bebe" caused random triggers on normal speech.
    wake_variants = {
        normalized_wake,
        normalized_wake.replace(" ", ""),
        "bibi",
        "beebee",
        "bee bee",
        "bibby", "bibbi", "biby", "bibee", "bibie",
    }

    lowered = cleaned.lower()
    for variant in wake_variants:
        if not variant:
            continue
        variant_pattern = re.escape(variant).replace(r"\ ", r"\s+")
        match = re.search(rf"\b{variant_pattern}\b", lowered)
        if not match:
            continue

        remaining = (cleaned[: match.start()] + " " + cleaned[match.end():]).strip(" ,.!?")
        remaining = re.sub(r"^(?:hey|okay|ok)(?:\s+|$)", "", remaining, flags=re.IGNORECASE)
        remaining = re.sub(r"(?:\s+|^)(?:hey|okay|ok)$", "", remaining, flags=re.IGNORECASE)
        remaining = _strip_wake_artifacts(remaining).strip(" ,.!?")
        return remaining if remaining else "__wake_only__"

    return None


def _route_intent(intent: dict[str, Any]) -> dict[str, Any]:
    intent_name = str(intent.get("intent", "general")).strip().lower() or "general"
    params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}

    if intent_name == "open_app":
        return executor.open_app(str(params.get("app_name", "")).strip())
    if intent_name == "create_file":
        return executor.create_file(
            file_name=str(params.get("file_name", "")).strip(),
            file_type=str(params.get("file_type", "txt")).strip(),
            content=str(params.get("content", "")),
        )
    if intent_name == "create_app":
        return executor.create_app(str(params.get("description", "")).strip())
    if intent_name == "search_pc":
        return executor.search_pc(str(params.get("query", "")).strip())
    if intent_name == "web_search":
        return executor.web_search(str(params.get("query", "")).strip())
    if intent_name == "system_query":
        return executor.system_query(str(params.get("query", "")).strip())
    if intent_name == "clarify":
        prompt = str(
            params.get("follow_up")
            or intent.get("clarification_prompt")
            or "Could you repeat that?"
        ).strip()
        return {
            "success": True,
            "message": prompt,
            "data": {"requires_clarification": True},
            "requires_confirmation": False,
        }

    general_params = dict(params)
    general_params.setdefault("raw_transcript", intent.get("raw_transcript", ""))
    return executor.general(general_params)


# ─────────────────────────────────────────────────────────────────────────────
# Wake-word listener
# ─────────────────────────────────────────────────────────────────────────────
class WakeWordListener:
    """Continuously samples the microphone and listens for a wake phrase."""

    def __init__(self) -> None:
        self.sample_rate = int(os.getenv("WAKE_SAMPLE_RATE", "16000"))
        self.chunk_duration_s = float(os.getenv("WAKE_CHUNK_DURATION_S", "1.5"))
        self.signal_threshold = float(os.getenv("WAKE_SIGNAL_THRESHOLD", "0.0022"))
        self.cooldown_s = float(os.getenv("WAKE_COOLDOWN_S", "0.6"))
        self.wake_word = (os.getenv("WAKE_WORD", "Bibi").strip() or "Bibi").lower()
        self.tmp_dir = Path(os.getenv("AUDIO_TMP_DIR", str(_BACKEND_DIR.parent / "tmp")))
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._model = None
        self._model_lock = threading.Lock()

    def poll(self) -> str | None:
        audio = self._record_chunk()
        if audio is None:
            return None

        # Gate on the LOUDEST 0.25s sub-window, not the 1.5s average. A short,
        # soft "Bibi" barely moves the full-window RMS (it's mostly silence),
        # which is why the user had to shout. The windowed-max captures the
        # word itself. A false positive just costs one cheap transcribe (Whisper
        # rejects non-speech), so we can afford to be sensitive.
        level = self._speech_level(audio)
        if level > 0.0015:
            logger.debug("WAKE chunk level=%.4f (threshold=%.4f)", level, self.signal_threshold)
        if level < self.signal_threshold:
            return None

        # Boost soft/quiet speech before transcription so the user doesn't have
        # to speak loudly. Whisper's VAD still rejects pure noise.
        audio = _boost_audio(audio)

        wav_path = self.tmp_dir / f"wake_{int(time.time() * 1000)}.wav"
        wavfile.write(str(wav_path), self.sample_rate, audio)
        try:
            transcript = self._transcribe(wav_path)
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass

        cmd = extract_wake_command(transcript, self.wake_word) if transcript else None
        if transcript:
            logger.info("WAKE transcript=%r -> command=%r", transcript, cmd)
        if not transcript:
            return None
        return cmd

    def _speech_level(self, audio: "np.ndarray") -> float:
        """Loudest 0.25s sub-window RMS (normalized 0..1).  Far more sensitive
        to a short, soft wake word than the full-window average."""
        try:
            flat = audio.astype(np.float32).flatten()
            if flat.size == 0:
                return 0.0
            win = max(1, int(0.25 * self.sample_rate))
            if flat.size >= win:
                n = flat.size // win
                windows = flat[: n * win].reshape(n, win)
                rms = np.sqrt(np.mean(windows * windows, axis=1))
                return float(np.max(rms) / 32768.0)
            return float(np.sqrt(np.mean(flat * flat)) / 32768.0)
        except Exception:
            return 1.0  # on error, don't block — let transcription decide

    def _record_chunk(self) -> np.ndarray | None:
        frames = int(self.sample_rate * self.chunk_duration_s)
        try:
            audio = sd.rec(frames, samplerate=self.sample_rate, channels=1, dtype="int16")
            sd.wait()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Wake-word recording failed: %s", exc)
            return None

        if audio is None or getattr(audio, "size", 0) == 0:
            return None
        return np.copy(audio).reshape(-1, 1)

    def _transcribe(self, wav_path: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except Exception:
            try:
                return str(voice_intent._transcribe(str(wav_path))).strip()  # type: ignore[attr-defined]
            except Exception:
                return ""

        with self._model_lock:
            if self._model is None:
                self._model = self._build_model(WhisperModel)
            model = self._model

        segments, _ = model.transcribe(
            str(wav_path),
            language=os.getenv("WAKE_WORD_LANGUAGE", "en"),
            task="transcribe",
            beam_size=1,
            temperature=0.0,
            vad_filter=True,
            without_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=f"{self.wake_word}. Hey {self.wake_word}. {self.wake_word} open Claude.",
        )
        text = " ".join(str(seg.text).strip() for seg in segments if str(seg.text).strip())
        return re.sub(r"\s+", " ", text).strip()

    def _build_model(self, whisper_model_class: Any) -> Any:
        model_name = os.getenv("WAKE_WORD_MODEL", "tiny").strip() or "tiny"
        device = _detect_device()
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info("Loading wake-word Whisper model '%s' on %s", model_name, device)
        return whisper_model_class(model_name, device=device, compute_type=compute_type)


def _detect_device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


# ─────────────────────────────────────────────────────────────────────────────
# Service — owns the background thread + event log the web UI polls
# ─────────────────────────────────────────────────────────────────────────────
class WakeService:
    """Singleton-ish controller wrapping the listener in a daemon thread."""

    def __init__(self) -> None:
        self._listener: WakeWordListener | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._events_lock = threading.Lock()
        self._seq = 0
        self._status_text = "Wake listening is off."
        self._ignore_audio_until = 0.0
        self._last_error = ""

    # ---- public API -------------------------------------------------------
    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running.is_set():
                return self.status()
            try:
                if self._listener is None:
                    self._listener = WakeWordListener()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                return self.status()

            self._running.set()
            self._status_text = f'Listening for "{self._listener.wake_word.title()}"…'
            self._last_error = ""
            self._thread = threading.Thread(target=self._loop, daemon=True, name="wake-listener")
            self._thread.start()
            logger.info("Wake service started.")
            _orch.orchestrator.set_wake_state(True, self._status_text)
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._running.clear()
            self._status_text = "Wake listening is off."
            logger.info("Wake service stopped.")
            _orch.orchestrator.set_wake_state(False, self._status_text)
            return self.status()

    def simulate(self, transcript: str) -> dict[str, Any]:
        """Inject a transcript as if the wake word + command were just heard.

        Dev/test affordance so the web UI loop can be exercised without a live
        microphone.  Gated behind WAKE_DEBUG in app.py.  Runs on a worker thread
        so the HTTP request returns immediately and the UI sees events appear
        via polling, exactly like a real utterance.
        """
        text = str(transcript or "").strip()
        if not text:
            return self.status()
        command = extract_wake_command(text, self._wake_word_value()) or text
        _orch.orchestrator.signal_wake("Wake word heard.")
        if command == "__wake_only__":
            _orch.orchestrator.speak("Yes? What can I do?")
        else:
            _orch.orchestrator.run_utterance(command, source="wake")
        return self.status()

    def _wake_word_value(self) -> str:
        if self._listener:
            return self._listener.wake_word
        return (os.getenv("WAKE_WORD", "Bibi").strip() or "Bibi").lower()

    def status(self) -> dict[str, Any]:
        wake_word = self._listener.wake_word if self._listener else os.getenv("WAKE_WORD", "Bibi")
        with self._events_lock:
            events = list(self._events[-12:])
        return {
            "listening": self._running.is_set(),
            "wake_word": str(wake_word).title(),
            "status": self._status_text,
            "seq": self._seq,
            "events": events,
            "error": self._last_error,
        }

    # ---- internals --------------------------------------------------------
    def _push_event(self, kind: str, **fields: Any) -> None:
        with self._events_lock:
            self._seq += 1
            self._events.append({"id": self._seq, "kind": kind, "ts": time.time(), **fields})
            if len(self._events) > 60:
                self._events = self._events[-60:]

    def _loop(self) -> None:
        listener = self._listener
        assert listener is not None
        while self._running.is_set():
            if time.time() < self._ignore_audio_until:
                time.sleep(0.15)
                continue
            try:
                command = listener.poll()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Wake poll error: %s", exc)
                self._last_error = str(exc)
                time.sleep(listener.cooldown_s)
                continue

            if command is None:
                time.sleep(listener.cooldown_s)
                continue

            # Don't let our own actions / TTS re-trigger the mic immediately.
            self._ignore_audio_until = time.time() + 2.0

            if command == "__wake_only__":
                self._status_text = "Listening for your command…"
                _orch.orchestrator.signal_wake("Listening… speak your command")
                self._handle_followup(listener)
            else:
                # Inline command ("bibi open youtube"): heard + working in one go.
                self._status_text = f"Heard: {command}"
                _orch.orchestrator.signal_wake("Listening…")
                _orch.orchestrator.signal_processing(f"Got it — working on “{command}”…")
                _orch.orchestrator.run_utterance(command, source="wake")

            if self._listener:
                self._status_text = f'Listening for "{self._listener.wake_word.title()}"…'
            self._ignore_audio_until = time.time() + 2.0
            time.sleep(listener.cooldown_s)

    def _handle_followup(self, listener: WakeWordListener) -> None:
        """Wake word heard alone — capture the next utterance and run it.

        Transcription ONLY (local Whisper); Bibi's Claude brain does the
        reasoning, so this path never touches Ollama.
        """
        wav_path = ""
        try:
            wav_path = voice_intent._record_audio()
            transcript = voice_intent._transcribe(wav_path).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("follow-up capture failed: %s", exc)
            transcript = ""
        finally:
            if wav_path:
                try:
                    from pathlib import Path as _P
                    _P(wav_path).unlink(missing_ok=True)
                except OSError:
                    pass
        if transcript:
            # Recording stopped (silence) → "got it, working" sound, then run.
            _orch.orchestrator.signal_processing(f"Got it — working on “{transcript}”…")
            _orch.orchestrator.run_utterance(transcript, source="wake")
        else:
            _orch.orchestrator.speak("I didn't catch that. Try again after the beep.")


# Module-level singleton used by app.py
service = WakeService()
