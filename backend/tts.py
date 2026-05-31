"""Local text-to-speech helpers for the PC Assistant.

Primary engine is edge-tts (Microsoft neural voices — natural, free, no API
key; needs internet).  Falls back to the offline Windows SAPI voice if edge-tts
is unavailable or the network is down, so Bibi always speaks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import edge_tts  # neural, natural voices

    _HAS_EDGE = True
except ImportError:
    _HAS_EDGE = False

try:
    import pyttsx3

    _HAS_PYTTSX3 = True
except ImportError:
    _HAS_PYTTSX3 = False

_TMP_DIR = Path(__file__).resolve().parent.parent / "tmp"
_TTS_FILE_MAX_AGE_S = 1800

# Default neural voice (warm, natural). Override with BIBI_TTS_VOICE, e.g.
# en-US-JennyNeural, en-US-AriaNeural, en-US-EmmaNeural.
_DEFAULT_NEURAL_VOICE = "en-US-AvaNeural"

# ── make text sound like speech, not a screen reader ──────────────────────
_SPEECH_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_SPEECH_MDLINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_SPEECH_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # emoji & supplemental symbols
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional-indicator (flags)
    "←-⇿"           # arrows (e.g. wttr wind glyphs)
    "⌀-⏿"           # technical symbols
    "]"
)


def _clean_for_speech(text: str) -> str:
    """Strip things that sound terrible when read aloud (markdown, URLs,
    emoji, stray symbols) so Bibi speaks like a person, not a screen reader."""
    t = str(text or "")
    t = _SPEECH_MDLINK_RE.sub(r"\1", t)        # [label](url) -> label
    t = _SPEECH_URL_RE.sub("the link", t)      # bare URL  -> "the link"
    t = _SPEECH_EMOJI_RE.sub("", t)            # drop emoji / dingbats / arrows
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)  # code blocks
    t = re.sub(r"[*_`#>|~]+", " ", t)          # markdown emphasis / headers / tables
    t = t.replace("&", " and ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def generate_tts_audio(text: str) -> str:
    """
    Generate a local audio file of spoken speech for the provided text.

    Returns the absolute path to the audio file (.mp3 for the neural engine,
    .wav for the SAPI fallback).
    """
    cleaned_text = _clean_for_speech(text)
    if not cleaned_text:
        raise ValueError("TTS text cannot be empty.")

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_old_tts_files()

    stamp = int(time.time() * 1000)

    # 1) Neural (edge-tts) — natural voice, primary.
    if _HAS_EDGE:
        mp3_path = _TMP_DIR / f"tts_{stamp}.mp3"
        try:
            _generate_with_edge(cleaned_text, mp3_path)
            if mp3_path.exists() and mp3_path.stat().st_size > 0:
                return str(mp3_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("edge-tts failed, falling back to SAPI: %s", exc)
            try:
                mp3_path.unlink(missing_ok=True)
            except OSError:
                pass

    # 2) Offline fallbacks → WAV.
    wav_path = _TMP_DIR / f"tts_{stamp}.wav"
    if _HAS_PYTTSX3:
        try:
            _generate_with_pyttsx3(cleaned_text, wav_path)
            if wav_path.exists():
                return str(wav_path)
        except Exception:
            pass

    _generate_with_powershell(cleaned_text, wav_path)
    if not wav_path.exists():
        raise FileNotFoundError("TTS engine did not produce an audio file.")
    return str(wav_path)


def _generate_with_edge(text: str, output_path: Path) -> None:
    voice = os.getenv("BIBI_TTS_VOICE", "").strip() or _DEFAULT_NEURAL_VOICE
    # Allow gentle tuning of pace/pitch via env (edge-tts rate/pitch format).
    rate = os.getenv("BIBI_TTS_RATE", "+0%").strip() or "+0%"
    pitch = os.getenv("BIBI_TTS_PITCH", "+0Hz").strip() or "+0Hz"

    async def _run() -> None:
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        await comm.save(str(output_path))

    asyncio.run(_run())


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
    voices = engine.getProperty("voices") or []

    # 1) Explicit pin via env (e.g. BIBI_TTS_VOICE="Microsoft Zira Desktop").
    pinned = os.getenv("BIBI_TTS_VOICE", "").strip().lower()
    if pinned:
        for voice in voices:
            if pinned in str(getattr(voice, "name", "")).lower():
                return str(getattr(voice, "id", "")) or None

    # 2) Prefer a known FEMALE voice by name.
    female_fragments = ["zira", "hazel", "aria", "jenny", "eva", "susan", "linda", "heera"]
    for fragment in female_fragments:
        for voice in voices:
            if fragment in str(getattr(voice, "name", "")).lower():
                return str(getattr(voice, "id", "")) or None

    # 3) Fall back to any voice whose gender metadata says female.
    for voice in voices:
        gender = str(getattr(voice, "gender", "")).lower()
        if "female" in gender:
            return str(getattr(voice, "id", "")) or None

    # 4) Last resort: first available voice (do NOT prefer male names).
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
# Prefer a female voice (Bibi). Try an exact pin, then any female voice.
try {
    $pin = $env:BIBI_TTS_VOICE
    $picked = $false
    if ($pin) {
        foreach ($v in $synth.GetInstalledVoices()) {
            if ($v.VoiceInfo.Name -like "*$pin*") { $synth.SelectVoice($v.VoiceInfo.Name); $picked = $true; break }
        }
    }
    if (-not $picked) {
        $synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)
    }
} catch { }
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
    for pattern in ("tts_*.wav", "tts_*.mp3"):
        for candidate in _TMP_DIR.glob(pattern):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink(missing_ok=True)
            except OSError:
                continue
