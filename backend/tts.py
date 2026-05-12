"""Local text-to-speech helpers for the PC Assistant."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import pyttsx3

    _HAS_PYTTSX3 = True
except ImportError:
    _HAS_PYTTSX3 = False

_TMP_DIR = Path(__file__).resolve().parent.parent / "tmp"
_TTS_FILE_MAX_AGE_S = 1800


def generate_tts_audio(text: str) -> str:
    """
    Generate a local WAV file containing spoken speech for the provided text.

    Args:
        text (str): Text to speak.

    Returns:
        str: Absolute path to the generated WAV file.
    """
    cleaned_text = " ".join(str(text or "").split()).strip()
    if not cleaned_text:
        raise ValueError("TTS text cannot be empty.")

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_old_tts_files()

    output_path = _TMP_DIR / f"tts_{int(time.time() * 1000)}.wav"

    if _HAS_PYTTSX3:
        try:
            _generate_with_pyttsx3(cleaned_text, output_path)
            if output_path.exists():
                return str(output_path)
        except Exception:
            pass

    _generate_with_powershell(cleaned_text, output_path)
    if not output_path.exists():
        raise FileNotFoundError("TTS engine did not produce an audio file.")
    return str(output_path)


def _generate_with_pyttsx3(text: str, output_path: Path) -> None:
    engine = pyttsx3.init()
    try:
        voice_id = _pick_pyttsx3_voice(engine)
        if voice_id:
            engine.setProperty("voice", voice_id)
        engine.setProperty("rate", int(os.getenv("TTS_RATE_WPM", "175")))
        engine.save_to_file(text, str(output_path))
        engine.runAndWait()
    finally:
        try:
            engine.stop()
        except Exception:
            pass


def _pick_pyttsx3_voice(engine: "pyttsx3.Engine") -> str | None:
    preferred_fragments = ["zira", "hazel", "aria", "jenny", "guy", "david"]
    voices = engine.getProperty("voices") or []
    for fragment in preferred_fragments:
        for voice in voices:
            voice_name = str(getattr(voice, "name", "")).lower()
            if fragment in voice_name:
                return str(getattr(voice, "id", "")) or None
    if voices:
        return str(getattr(voices[0], "id", "")) or None
    return None


def _generate_with_powershell(text: str, output_path: Path) -> None:
    script = """
param(
    [Parameter(Mandatory=$true)][string]$Text,
    [Parameter(Mandatory=$true)][string]$OutputPath
)

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile($OutputPath)
try {
    $synth.Speak($Text)
}
finally {
    $synth.Dispose()
}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = Path(handle.name)

    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Text",
                text,
                "-OutputPath",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup_old_tts_files() -> None:
    cutoff = time.time() - _TTS_FILE_MAX_AGE_S
    for candidate in _TMP_DIR.glob("tts_*.wav"):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink(missing_ok=True)
        except OSError:
            continue
