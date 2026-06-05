"""
screen_control.py — Bibi takes over the REAL mouse/keyboard on whatever you're
looking at.

This is instance-independent: it screenshots your actual screen, asks Claude
(vision) where the target is, then moves the real cursor and clicks it with
pyautogui.  So "click the second video" works on the YouTube tab in your normal
Brave — no debug port, no separate window, no matter which app is focused.

Flow:  screenshot → Claude vision returns the target's location (as fractions of
the image) → scale to the logical screen → move + click.

Safety: pyautogui FAILSAFE is on (slam the mouse to a screen corner to abort).
We never click if vision says the target isn't visible.

Python 3.11+
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import perf

logger = logging.getLogger(__name__)

_TMP = Path(os.getenv("AUDIO_TMP_DIR", str(Path(__file__).resolve().parent.parent / "tmp")))
_VISION_MODEL = os.getenv("BIBI_VISION_MODEL", "sonnet").strip() or "sonnet"
_VISION_TIMEOUT = float(os.getenv("BIBI_VISION_TIMEOUT_S", "90"))
_VISION_MAX_W = int(os.getenv("BIBI_VISION_MAX_W", "1280"))  # downscale → faster vision
_VISION_JPEG_Q = int(os.getenv("BIBI_VISION_JPEG_Q", "72"))  # JPEG quality for the vision copy
# Cursor glide time per click. The old hard-coded 0.5s was pure cosmetic delay
# on every single click; 0.15s still moves visibly but feels far snappier.
_MOVE_DURATION = float(os.getenv("BIBI_MOVE_DURATION_S", "0.15"))

_last_shot_path = _TMP / "bibi_screen.png"

try:
    from executor import _resolve_claude_executable as _resolve_claude  # type: ignore
except Exception:  # pragma: no cover
    def _resolve_claude() -> str:
        return shutil.which("claude.exe") or shutil.which("claude") or "claude"


# ── screen capture ────────────────────────────────────────────────────────
def capture(path: Path | None = None) -> tuple[Path, int, int]:
    """Grab the primary screen to a PNG. Returns (path, width_px, height_px)."""
    from PIL import ImageGrab
    img = ImageGrab.grab()
    out = path or _last_shot_path
    _TMP.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out, img.size[0], img.size[1]


def latest_shot() -> bytes | None:
    try:
        return _last_shot_path.read_bytes() if _last_shot_path.exists() else None
    except Exception:
        return None


# ── vision: find a target on screen ──────────────────────────────────────
def _downscaled(image_path: Path) -> Path:
    """Return a width-limited JPEG copy of the screenshot so vision is fast.
    A full-res PNG screenshot is several MB; a downscaled JPEG is ~100-300 KB,
    which the vision model reads and processes much faster. Coordinates stay
    valid because we always work in 0-1 fractions."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        if img.width > _VISION_MAX_W:
            ratio = _VISION_MAX_W / img.width
            img = img.resize((_VISION_MAX_W, int(img.height * ratio)))
        out = image_path.parent / "bibi_vision.jpg"
        img.convert("RGB").save(out, "JPEG", quality=_VISION_JPEG_Q, optimize=True)
        return out
    except Exception:
        pass
    return image_path


def _run_vision(prompt: str, image_path: Path) -> str:
    exe = _resolve_claude()
    image_path = _downscaled(Path(image_path).resolve())
    sysmsg = ("You are a precise screen UI locator for a desktop assistant. "
              "You are given a screenshot path; inspect it and answer ONLY with "
              "the requested output (minified JSON unless asked for prose). Never "
              "refuse; never add extra commentary.")
    # --add-dir lets the CLI read the screenshot (Claude Code sandboxes file
    # reads to the cwd subtree otherwise).
    cmd = [exe, "--print", "--system-prompt", sysmsg,
           "--add-dir", str(image_path.parent), "--model", _VISION_MODEL, "--",
           f"{prompt}\n\nScreenshot file: {image_path}"]
    with perf.timer("vision.claude"):
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_VISION_TIMEOUT,
                             cwd=str(image_path.parent))
    return (out.stdout or "").strip()


def _parse_json(text: str) -> dict | None:
    t = str(text or "")
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.DOTALL)
    if fence:
        t = fence.group(1)
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def locate(target_description: str) -> dict:
    """Return {'found':bool,'x':frac,'y':frac,'label':str} for a described
    on-screen target, using a fresh screenshot."""
    path, w, h = capture()
    prompt = (
        f"Find this on the screen: {target_description}. "
        "Return ONLY minified JSON: "
        '{"found":true|false,"x":<float 0-1 = horizontal fraction of the image>,'
        '"y":<float 0-1 = vertical fraction>,"label":"<what you clicked>"}. '
        "x,y must be the CENTER of the target. If it is not visible, set found=false."
    )
    try:
        data = _parse_json(_run_vision(prompt, path)) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("vision locate failed: %s", exc)
        return {"found": False, "error": str(exc)}
    if not data.get("found"):
        return {"found": False, "label": data.get("label", "")}
    try:
        return {"found": True, "x": float(data["x"]), "y": float(data["y"]),
                "label": str(data.get("label", ""))}
    except Exception:
        return {"found": False}


# ── actions on the real screen ────────────────────────────────────────────
def _pg():
    import pyautogui
    pyautogui.FAILSAFE = True
    return pyautogui


def focus_browser(hint: str = "brave") -> bool:
    """Bring the browser window to the FOREGROUND and give it input focus, so a
    real click registers on the page (not just 'activate the window')."""
    try:
        import pygetwindow as gw
        wins = []
        for title in gw.getAllTitles():
            if title and hint.lower() in title.lower():
                wins += gw.getWindowsWithTitle(title)
        if not wins:
            return False
        w = wins[0]
        if getattr(w, "isMinimized", False):
            w.restore()
        w.activate()
        time.sleep(0.4)
        return True
    except Exception:
        return False


def click_xy(fx: float, fy: float, double: bool = False, right: bool = False) -> dict:
    """Move the real cursor to a fractional (0-1) screen position and click."""
    pg = _pg()
    sw, sh = pg.size()
    x = max(1, min(sw - 1, int(float(fx) * sw)))
    y = max(1, min(sh - 1, int(float(fy) * sh)))
    pg.moveTo(x, y, duration=_MOVE_DURATION)
    time.sleep(0.08)
    if right:
        pg.click(button="right")
    else:
        pg.click(clicks=2 if double else 1, interval=0.1)
    return {"ok": True, "x": x, "y": y}


def activate_tab(hint: str) -> bool:
    """Switch the browser to the tab whose label matches `hint` (e.g. 'YouTube'),
    so its page is visible before we act on it. Needed because the Bibi UI is
    itself a tab — without this, Bibi would 'see' the Bibi tab, not the target."""
    if not hint:
        return False
    focus_browser()
    time.sleep(0.3)
    loc = locate(
        f"the browser TAB in the tab strip at the very TOP of the window whose title mentions "
        f"\"{hint}\" (a small tab at the top edge — NOT anything in the page body below)"
    )
    # Only accept a hit that's actually in the tab strip (top ~12% of screen).
    if loc.get("found") and float(loc.get("y", 1)) < 0.12:
        click_xy(loc["x"], loc["y"])
        time.sleep(0.9)
        capture()
        return True
    return False


def click_target(target_description: str, double: bool = False) -> dict:
    """See the screen, find the target, move the real cursor there and click."""
    loc = locate(target_description)
    if not loc.get("found"):
        return {"ok": False, "message": f"I don't see “{target_description}” on your screen right now."}
    res = click_xy(loc["x"], loc["y"], double=double)
    capture()  # refresh the panel preview with the result
    return {"ok": True, "label": loc.get("label", ""), **res}


def type_text(text: str, enter: bool = False) -> dict:
    pg = _pg()
    pg.typewrite(str(text or ""), interval=0.02)
    if enter:
        pg.press("enter")
    time.sleep(0.3)
    capture()
    return {"ok": True}


def press_key(key: str) -> dict:
    pg = _pg()
    pg.press(str(key or "enter"))
    time.sleep(0.2)
    capture()
    return {"ok": True}


def scroll(amount: int = -600) -> dict:
    pg = _pg()
    pg.scroll(int(amount))
    time.sleep(0.2)
    capture()
    return {"ok": True}


def search_here(query: str, attempts: int = 4, wait_between: float = 2.0) -> dict:
    """Find this page's search box (by sight), click it, type the query, Enter.

    Retries because the page may still be loading / the window may not be focused
    yet right after opening a site."""
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "message": "Search for what?"}
    focus_browser()  # make sure the browser has input focus before we click/type
    for i in range(max(1, attempts)):
        loc = locate("the main search box / search input field on this page "
                     "(the box you'd type a query into)")
        if loc.get("found"):
            click_xy(loc["x"], loc["y"])
            time.sleep(0.35)
            pg = _pg()
            pg.hotkey("ctrl", "a")          # clear anything already there
            time.sleep(0.1)
            pg.typewrite(query, interval=0.02)
            time.sleep(0.15)
            pg.press("enter")
            time.sleep(0.5)
            capture()
            return {"ok": True}
        if i < attempts - 1:
            time.sleep(wait_between)          # page probably still loading
    return {"ok": False, "message": "I couldn't find a search box on this page."}


def ordinal(n: int) -> str:
    names = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
             6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth"}
    return names.get(int(n), f"{int(n)}th")


def available() -> bool:
    try:
        import pyautogui  # noqa: F401
        from PIL import ImageGrab  # noqa: F401
        return True
    except Exception:
        return False
