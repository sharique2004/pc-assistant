"""
Smoke test the live pipeline by POSTing typed commands to /command.

Run while the Flask backend is up on port 5000:

    python pipeline_smoke.py

The filename intentionally does not start with `test_` so pytest does not
collect it.  This is a live-system check that requires Ollama and the
backend to be running - it would fail in CI.
"""
from __future__ import annotations

import json
import sys
import requests

BASE = "http://127.0.0.1:5000"


def post(text: str) -> dict:
    resp = requests.post(
        f"{BASE}/command",
        json={"trigger": "typed_text", "text": text},
        timeout=180,
    )
    return resp.json()


def show(label: str, response: dict) -> None:
    intent = (response.get("intent") or {}).get("intent")
    parameters = (response.get("intent") or {}).get("parameters")
    result = response.get("result") or {}
    success = result.get("success")
    message = result.get("message")
    requires_confirmation = (result.get("data") or {}).get("requires_confirmation") or result.get("requires_confirmation")
    operation_id = (result.get("data") or {}).get("operation_id")

    print(f"\n=== {label}")
    print(f"  intent={intent}  params={parameters}")
    print(f"  success={success}  requires_confirmation={bool(requires_confirmation)}")
    if operation_id:
        print(f"  operation_id={operation_id}")
    if isinstance(message, str):
        first_line = message.splitlines()[0] if message else ""
        print(f"  message={first_line[:160]}")


SCENARIOS = [
    # 1. App-open scenarios across source types
    "Open Notepad",
    "Open WhatsApp",
    "Open ChatGPT",
    "Open Chrome",
    "Open Spotify",

    # 2. App + action (chained)
    "Open ChatGPT and ask how to make pasta",
    "Open ChatGPT and search up the best web framework",
    "Open ChatGPT and tell me about quantum computing",
    "Open Claude and ask what the best speaker is",

    # 3. Coding hand-off routing
    "Use claude to build me a small React budget tracker",
    "Use codex to write a Python script that renames files",
    "Create a React dashboard app for sales data",
    "Create a command line tool that converts JSON to CSV",

    # 4. Utility intents
    "What apps are running right now",
    "How much memory am I using",
    "What is the active window",
    "Find my resume",
    "Search the web for the cheapest noise cancelling headphones",

    # 5. Memory + general
    "Remember that my favorite editor is VS Code",
    "What do you remember about me",
]


def main() -> int:
    failures = 0
    for phrase in SCENARIOS:
        try:
            response = post(phrase)
        except requests.RequestException as exc:
            print(f"\n=== {phrase}\n  REQUEST ERROR: {exc}")
            failures += 1
            continue
        show(phrase, response)

        # Anything that came back with success=False AND not requires_confirmation
        # is a regression candidate.
        result = response.get("result") or {}
        if result.get("success") is False and not (
            (result.get("data") or {}).get("requires_confirmation")
            or result.get("requires_confirmation")
        ):
            failures += 1
    print(f"\n--- summary: {failures} regressions out of {len(SCENARIOS)} scenarios")
    return failures


if __name__ == "__main__":
    sys.exit(main())
