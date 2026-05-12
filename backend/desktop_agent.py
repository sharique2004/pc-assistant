"""Persistent local desktop assistant widget with wake-word listening."""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
import winsound
from ctypes import WinDLL, c_void_p, get_last_error, wintypes
from pathlib import Path
from tkinter import ttk
from typing import Any

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from scipy.io import wavfile

import executor
import tts
import voice_intent

_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(_BACKEND_DIR / ".env")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_MUTEX_ALREADY_EXISTS = 183
_kernel32 = WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_WAKE_ARTIFACT_PATTERNS = (
    r"^(?:what(?:'s| is)\s+the\s+wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
    r"^(?:the\s+wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
    r"^(?:wake\s+word(?:\s+is)?)(?:\s+|(?=[a-z]))",
)


class _SingleInstanceGuard:
    """Keep only one Bibi desktop widget running per user session."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: int | None = None

    def acquire(self) -> bool:
        self.handle = int(
            _kernel32.CreateMutexW(
                c_void_p(),
                wintypes.BOOL(False),
                wintypes.LPCWSTR(self.name),
            )
        )
        if not self.handle:
            raise OSError("Failed to create the Bibi instance mutex.")
        return get_last_error() != _MUTEX_ALREADY_EXISTS

    def release(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = None


class WakeWordListener:
    """Continuously samples the microphone and listens for a wake phrase."""

    def __init__(self) -> None:
        self.sample_rate = int(os.getenv("WAKE_SAMPLE_RATE", "16000"))
        self.chunk_duration_s = float(os.getenv("WAKE_CHUNK_DURATION_S", "1.2"))
        self.signal_threshold = float(os.getenv("WAKE_SIGNAL_THRESHOLD", "0.008"))
        self.cooldown_s = float(os.getenv("WAKE_COOLDOWN_S", "1.0"))
        self.wake_word = (os.getenv("WAKE_WORD", "Bibi").strip() or "Bibi").lower()
        self.tmp_dir = Path(os.getenv("AUDIO_TMP_DIR", str(_BACKEND_DIR.parent / "tmp")))
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._model = None
        self._model_lock = threading.Lock()

    def poll(self) -> str | None:
        """Return a command string when the wake word is detected."""
        audio = self._record_chunk()
        if audio is None:
            return None

        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)) / 32768.0)
        if rms < self.signal_threshold:
            return None

        wav_path = self.tmp_dir / f"wake_{int(time.time() * 1000)}.wav"
        wavfile.write(str(wav_path), self.sample_rate, audio)
        try:
            transcript = self._transcribe(wav_path)
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not transcript:
            return None

        return _extract_wake_command(transcript, self.wake_word)

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
        text = " ".join(str(segment.text).strip() for segment in segments if str(segment.text).strip())
        return re.sub(r"\s+", " ", text).strip()

    def _build_model(self, whisper_model_class: Any) -> Any:
        model_name = os.getenv("WAKE_WORD_MODEL", "tiny").strip() or "tiny"
        device = _detect_device()
        compute_type = "float16" if device == "cuda" else "int8"
        return whisper_model_class(model_name, device=device, compute_type=compute_type)


class DesktopAssistantApp:
    """Floating Tk desktop widget for the always-on assistant."""

    def __init__(self) -> None:
        self.instance_guard = _SingleInstanceGuard("Local\\BibiDesktopAssistant")
        if not self.instance_guard.acquire():
            raise SystemExit("Bibi is already running.")
        self.root = tk.Tk()
        self.root.title("Bibi")
        self.root.geometry("408x212+24+24")
        self.root.minsize(360, 190)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#0d1117")
        self.root.protocol("WM_DELETE_WINDOW", self._shutdown)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.wake_listener = WakeWordListener()
        self.running = True
        self.wake_enabled = tk.BooleanVar(value=True)
        self.hearing_button_var = tk.StringVar(value="Stop Hearing")
        self.status_var = tk.StringVar(value="Wake listening is on.")
        self.last_heard_var = tk.StringVar(value='Say "Bibi" to wake me.')
        self.last_result_var = tk.StringVar(value="Ready for app launches, web search, and multi-step actions.")
        self._busy_lock = threading.Lock()
        self._ignore_audio_until = 0.0
        self._current_worker: threading.Thread | None = None

        self._build_ui()
        self._sync_wake_controls()
        self.root.after(120, self._process_events)
        self._wake_thread = threading.Thread(target=self._wake_loop, daemon=True, name="wake-listener")
        self._wake_thread.start()

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Bibi.TFrame", background="#0d1117")
        style.configure("Bibi.TLabel", background="#0d1117", foreground="#f3f6fb")
        style.configure("Muted.TLabel", background="#0d1117", foreground="#8d9bb2")
        style.configure("Bibi.TCheckbutton", background="#0d1117", foreground="#d9e1ee")
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10))

        frame = ttk.Frame(self.root, padding=16, style="Bibi.TFrame")
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Bibi", style="Bibi.TLabel", font=("Segoe UI Semibold", 17)).pack(anchor="w")
        ttk.Label(
            frame,
            text="Private local assistant",
            style="Muted.TLabel",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(frame, textvariable=self.status_var, style="Bibi.TLabel", wraplength=320).pack(anchor="w")
        ttk.Label(frame, textvariable=self.last_heard_var, style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, textvariable=self.last_result_var, style="Muted.TLabel", wraplength=320).pack(anchor="w", pady=(8, 0))

        controls = ttk.Frame(frame, style="Bibi.TFrame")
        controls.pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(
            controls,
            text="Wake word on",
            variable=self.wake_enabled,
            style="Bibi.TCheckbutton",
            command=self._sync_wake_controls,
        ).pack(side="left")
        ttk.Button(
            controls,
            textvariable=self.hearing_button_var,
            command=self._toggle_wake_listening,
            style="Accent.TButton",
        ).pack(side="right")
        ttk.Button(controls, text="Listen now", command=self._listen_now, style="Accent.TButton").pack(side="right", padx=(0, 8))

    def run(self) -> None:
        self.root.mainloop()

    def _wake_loop(self) -> None:
        while self.running:
            if not self.wake_enabled.get():
                time.sleep(0.25)
                continue
            if time.time() < self._ignore_audio_until:
                time.sleep(0.15)
                continue
            if self._busy_lock.locked():
                time.sleep(0.15)
                continue

            command = self.wake_listener.poll()
            if command is None:
                time.sleep(self.wake_listener.cooldown_s)
                continue

            if command == "__wake_only__":
                self.events.put(("status", "Wake word heard. Listening for your command..."))
                self.events.put(("heard", "Wake word detected"))
                self._start_worker(self._capture_follow_up_command)
            else:
                self.events.put(("status", "Wake word heard. Executing inline command..."))
                self.events.put(("heard", command))
                self._start_worker(self._execute_transcript_command, command)

            time.sleep(self.wake_listener.cooldown_s)

    def _listen_now(self) -> None:
        if self._busy_lock.locked():
            return
        self.events.put(("status", "Listening now..."))
        self._start_worker(self._capture_follow_up_command)

    def _toggle_wake_listening(self) -> None:
        self.wake_enabled.set(not self.wake_enabled.get())
        self._sync_wake_controls()

    def _sync_wake_controls(self) -> None:
        wake_is_enabled = bool(self.wake_enabled.get())
        self.hearing_button_var.set("Stop Hearing" if wake_is_enabled else "Resume Hearing")
        if not self._busy_lock.locked():
            self.status_var.set("Wake listening is on." if wake_is_enabled else "Wake listening is paused.")
        if not wake_is_enabled:
            self._ignore_audio_until = time.time() + 0.4

    def _start_worker(self, fn: Any, *args: Any) -> None:
        worker = threading.Thread(target=self._run_worker, args=(fn, *args), daemon=True)
        self._current_worker = worker
        worker.start()

    def _run_worker(self, fn: Any, *args: Any) -> None:
        if not self._busy_lock.acquire(blocking=False):
            return
        try:
            fn(*args)
        finally:
            self._busy_lock.release()
            self.events.put(("status", "Wake listening is on." if self.wake_enabled.get() else "Wake listening is paused."))

    def _capture_follow_up_command(self) -> None:
        try:
            intent = voice_intent.listen_and_parse(trigger="wake_word")
        except Exception as exc:  # noqa: BLE001
            self.events.put(("result", f"Voice capture failed: {exc}"))
            self._speak_async("I had trouble listening just then.")
            return
        self._handle_intent(intent)

    def _execute_transcript_command(self, transcript: str) -> None:
        intent = voice_intent.parse_text_command(transcript, trigger="wake_word_inline")
        self._handle_intent(intent)

    def _handle_intent(self, intent: dict[str, Any]) -> None:
        raw_transcript = str(intent.get("raw_transcript", "")).strip()
        if raw_transcript:
            self.events.put(("heard", raw_transcript))

        result = _route_intent(intent)
        message = str(result.get("message", "Done.")).strip() or "Done."
        self.events.put(("result", message))
        self._speak_async(message)

    def _speak_async(self, text: str) -> None:
        clean_text = " ".join(str(text or "").split()).strip()
        if not clean_text:
            return
        self._ignore_audio_until = time.time() + 2.0
        threading.Thread(target=self._play_tts, args=(clean_text,), daemon=True).start()

    def _play_tts(self, text: str) -> None:
        try:
            audio_path = tts.generate_tts_audio(text)
            winsound.PlaySound(audio_path, winsound.SND_FILENAME)
        except Exception as exc:  # noqa: BLE001
            logger.debug("TTS playback failed: %s", exc)
        finally:
            self._ignore_audio_until = time.time() + 1.0

    def _process_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "status":
                self.status_var.set(str(payload))
            elif event == "heard":
                self.last_heard_var.set(f"Heard: {payload}")
            elif event == "result":
                self.last_result_var.set(f"Result: {payload}")

        if self.running:
            self.root.after(120, self._process_events)

    def _shutdown(self) -> None:
        self.running = False
        self.instance_guard.release()
        self.root.destroy()


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
        prompt = str(params.get("follow_up") or intent.get("clarification_prompt") or "Could you repeat that?").strip()
        return {
            "success": True,
            "message": prompt,
            "data": {"requires_clarification": True},
            "requires_confirmation": False,
        }

    general_params = dict(params)
    general_params.setdefault("raw_transcript", intent.get("raw_transcript", ""))
    return executor.general(general_params)


def _extract_wake_command(transcript: str, wake_word: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", str(transcript or "").strip()).strip(" .?!")
    if not cleaned:
        return None

    normalized_wake = _normalize_token(wake_word)
    normalized_text = _normalize_token(cleaned)
    if not normalized_text:
        return None

    wake_variants = {
        normalized_wake,
        normalized_wake.replace(" ", ""),
        "beebee",
        "bee bee",
        "b b",
    }

    lowered = cleaned.lower()
    for variant in wake_variants:
        variant_pattern = re.escape(variant.replace(" ", r"\s+"))
        match = re.search(rf"\b{variant_pattern}\b", lowered)
        if not match:
            continue

        remaining = (cleaned[: match.start()] + " " + cleaned[match.end():]).strip(" ,.!?")
        remaining = re.sub(r"^(?:hey|okay|ok)(?:\s+|$)", "", remaining, flags=re.IGNORECASE)
        remaining = re.sub(r"(?:\s+|^)(?:hey|okay|ok)$", "", remaining, flags=re.IGNORECASE)
        remaining = _strip_wake_artifacts(remaining).strip(" ,.!?")
        return remaining if remaining else "__wake_only__"

    return None


def _strip_wake_artifacts(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    for pattern in _WAKE_ARTIFACT_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,.!?")


def _normalize_token(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _detect_device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


if __name__ == "__main__":
    DesktopAssistantApp().run()
