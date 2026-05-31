"""
screen_agent.py — Bibi looks at your screen, understands it, and figures out
how to get things done from what it sees.

Two capabilities:
  • describe()    — "what's on my screen / read this" → a spoken description.
  • run_goal()    — an OBSERVE → REASON → ACT → RE-OBSERVE loop (computer-use
                    style): each step Bibi screenshots the screen, decides the
                    single best next action toward the goal, narrates it, and
                    performs it with the real mouse/keyboard — then looks again.

It reuses screen_control for capture, the Claude vision call, and the real
input primitives.

Python 3.11+
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Any, Callable

import screen_control as sc

logger = logging.getLogger(__name__)

_MAX_STEPS = int(os.getenv("BIBI_SCREEN_MAX_STEPS", "8"))
# Goals that need CONTENT GENERATION (solve/write code, answer) — the blind
# click loop is useless for these; we read → generate → paste instead.
_GENERATIVE_RE = re.compile(
    r"\b(solve|write|complete|finish|implement|code|answer|fill in|compose|draft)\b",
    re.IGNORECASE,
)


def _run_claude_text(prompt: str, system: str, model: str = "sonnet", timeout: float = 120) -> str:
    exe = sc._resolve_claude()
    cmd = [exe, "--print", "--system-prompt", system, "--model", model, "--", prompt]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (out.stdout or "").strip()


def _strip_fences(text: str) -> str:
    m = re.search(r"```[a-zA-Z0-9+]*\s*\n(.*?)```", str(text or ""), flags=re.DOTALL)
    return (m.group(1) if m else str(text or "")).strip()


def solve_on_screen(goal: str, on_step: Callable[[str, dict], None] | None = None) -> dict:
    """Read the problem/question on screen, GENERATE the solution with Claude,
    and paste it into the editor/field — reliable for coding & answers, where a
    blind click loop fails."""
    try:
        path, _, _ = sc.capture()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"I couldn't capture your screen: {exc}"}

    if on_step:
        on_step("Reading the problem on your screen…", {})
    read = sc._run_vision(
        "Read the main task/problem/question on this screen. Return ONLY JSON: "
        '{"problem":"<the full problem/question statement text>",'
        '"stub":"<any starter code or function signature shown, copied verbatim, or empty>",'
        '"kind":"code"|"text",'
        '"language":"<python|javascript|... or empty>",'
        '"where":"<short description of WHERE to put the answer, e.g. the code editor on the right>"}',
        path,
    )
    d = sc._parse_json(read) or {}
    problem = str(d.get("problem", "")).strip()
    if not problem:
        return {"ok": False, "message": "I couldn't read a clear problem or question on your screen."}

    kind = str(d.get("kind", "code")).lower()
    lang = str(d.get("language", "") or "python")
    stub = str(d.get("stub", ""))
    where = str(d.get("where", "")) or ("the code editor" if kind == "code" else "the answer field")

    if on_step:
        on_step("Working out the solution…", {})
    if kind == "code":
        system = ("You are an expert competitive programmer. Output ONLY the complete, correct "
                  "solution code — no markdown fences, no explanation. Match the given starter "
                  "signature/class EXACTLY so it can replace it directly.")
        prompt = f"Problem:\n{problem}\n\nStarter code to match exactly:\n{stub}\n\nWrite the complete {lang} solution code."
    else:
        system = ("You are a helpful assistant. Output ONLY the answer text to place into the "
                  "field — no preamble, no markdown, no quotes.")
        prompt = f"Task: {goal}\n\nWhat's on screen:\n{problem}"

    try:
        solution = _strip_fences(_run_claude_text(prompt, system))
    except Exception as exc:  # noqa: BLE001
        logger.warning("solution generation failed: %s", exc)
        return {"ok": False, "message": "I read the problem but couldn't work out a solution."}
    if not solution:
        return {"ok": False, "message": "I read the problem but came up empty on a solution."}

    # Paste it (avoids code-editor auto-indent mangling).
    try:
        import pyperclip
        pyperclip.copy(solution)
        have_clip = True
    except Exception:
        have_clip = False

    if on_step:
        on_step(f"Putting the solution into {where}…", {})
    click = sc.click_target(where)
    if not click.get("ok"):
        return {"ok": False,
                "message": ("I worked out the solution and copied it to your clipboard, but couldn't "
                            "find where to put it — click your editor and press Ctrl+V.")}
    time.sleep(0.4)
    pg = sc._pg()
    pg.hotkey("ctrl", "a")
    time.sleep(0.15)
    if have_clip:
        pg.hotkey("ctrl", "v")
    else:
        pg.typewrite(solution, interval=0.005)
    time.sleep(0.4)
    sc.capture()
    return {"ok": True, "done": True,
            "message": "I worked out the solution and pasted it in. Review it, then run or submit when you're happy."}


def describe() -> str:
    """Spoken description of what's currently on screen."""
    try:
        path, _, _ = sc.capture()
        prompt = (
            "Describe what is on this screen for the user, in 2-4 short spoken "
            "sentences: which app/website, the main thing shown, and what they "
            "could do next. Plain conversational text only — no markdown, no JSON."
        )
        text = sc._run_vision(prompt, path).strip()
        return text or "I'm looking but I can't make out the screen right now."
    except Exception as exc:  # noqa: BLE001
        logger.warning("describe failed: %s", exc)
        return "I couldn't read your screen just now."


_ACTION_PROMPT = """You are Bibi, controlling a real Windows PC by LOOKING at the screen and moving the real mouse and keyboard. You decide ONE next action at a time, then you'll see the result and decide again.

GOAL: {goal}

What you've done so far: {history}

Look carefully at the attached screenshot and decide the single best NEXT action toward the goal. Coordinates are FRACTIONS of the image (0.0-1.0): x from left, y from top, at the CENTER of the thing to click.

Return ONLY one minified JSON object:
{{"observation":"<one short line: the relevant thing you see now>",
  "say":"<one short friendly line to speak to the user about what you're doing>",
  "action":"click"|"double_click"|"right_click"|"type"|"press"|"scroll"|"wait"|"done"|"give_up",
  "x":<fraction or null>, "y":<fraction or null>,
  "text":"<text to type, for action=type>",
  "key":"<single key for action=press, e.g. enter, tab, escape>",
  "direction":"down"|"up"}}

Rules:
- One concrete action per step. Prefer clicking a clearly-visible control.
- For action=type, first make sure the right field is focused (click it on a previous step).
- Use action=done when the GOAL is clearly accomplished on screen.
- Use action=give_up if you're stuck, the screen is wrong, or it would be unsafe/irreversible (payments, deleting).
- NEVER take destructive/irreversible actions (purchase, delete, send money) without it being the explicit goal — prefer give_up and let the user finish.
"""


def run_goal(goal: str, on_step: Callable[[str, dict], None] | None = None,
             max_steps: int | None = None) -> dict:
    """Observe→reason→act loop. on_step(say, action_dict) is called each step so
    the caller can narrate/spk progress."""
    goal = " ".join(str(goal or "").split()).strip()
    if not goal:
        return {"ok": False, "message": "What would you like me to do on screen?"}
    # Generative goals (solve/write/answer/code) → read+generate+paste, NOT a
    # blind click loop (which just clicks around and never writes anything).
    if _GENERATIVE_RE.search(goal):
        return solve_on_screen(goal, on_step=on_step)
    steps_cap = max_steps or _MAX_STEPS
    history: list[str] = []
    last: dict[str, Any] = {}

    for i in range(steps_cap):
        try:
            path, _, _ = sc.capture()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"I couldn't capture your screen: {exc}"}

        prompt = _ACTION_PROMPT.replace("{goal}", goal).replace(
            "{history}", "; ".join(history[-6:]) or "(nothing yet)")
        try:
            raw = sc._run_vision(prompt, path)
            act = sc._parse_json(raw) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("screen step vision failed: %s", exc)
            return {"ok": False, "message": "I had trouble reading the screen."}

        last = act
        action = str(act.get("action", "")).strip().lower()
        say = str(act.get("say") or act.get("observation") or "").strip()
        if on_step and say:
            on_step(say, act)

        if action in ("done", "give_up", ""):
            break

        try:
            if action in ("click", "double_click", "right_click"):
                fx, fy = act.get("x"), act.get("y")
                if fx is None or fy is None:
                    history.append(f"step {i+1}: no coords for {action}")
                    continue
                sc.click_xy(fx, fy, double=(action == "double_click"), right=(action == "right_click"))
            elif action == "type":
                sc.type_text(act.get("text", ""))
            elif action == "press":
                sc.press_key(act.get("key", "enter"))
            elif action == "scroll":
                sc.scroll(-700 if str(act.get("direction", "down")) != "up" else 700)
            elif action == "wait":
                time.sleep(1.2)
            history.append(f"step {i+1}: {action} — {act.get('observation','')[:60]}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("screen action '%s' failed: %s", action, exc)
            history.append(f"step {i+1}: {action} failed")
        time.sleep(0.9)  # let the UI settle before the next observation

    sc.capture()  # refresh panel preview
    final_action = str(last.get("action", "")).strip().lower()
    if final_action == "give_up":
        return {"ok": False, "done": False,
                "message": str(last.get("say") or "I got stuck — can you take it from here?")}
    return {"ok": True, "done": final_action == "done",
            "message": str(last.get("say") or "Done with what I could see.")}
