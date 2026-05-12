"""Synthetic end-to-end test for the Bibi wake-word flow.

Verifies the full path that desktop_agent.py uses when the wake-word
listener returns a transcript:

    transcript ──► _extract_wake_command ──► _route_intent ──► executor

Without touching the microphone or launching real apps.
"""
import desktop_agent


def _extract(transcript: str) -> str | None:
    return desktop_agent._extract_wake_command(transcript, "bibi")


def test_wake_inline_open_app_routes_to_open_app(monkeypatch):
    seen = {}

    def fake_open_app(app_name: str):
        seen["app_name"] = app_name
        return {"success": True, "message": f"Opened {app_name}", "data": {}}

    monkeypatch.setattr(desktop_agent.executor, "open_app", fake_open_app)

    cmd = _extract("Bibi open Notepad")
    assert cmd == "open Notepad"

    intent = {
        "intent": "open_app",
        "parameters": {"app_name": "Notepad"},
        "raw_transcript": cmd,
    }
    result = desktop_agent._route_intent(intent)
    assert result["success"] is True
    assert "Notepad" in result["message"]
    assert seen["app_name"] == "Notepad"


def test_wake_only_triggers_follow_up_capture():
    cmd = _extract("Hey Bibi")
    assert cmd == "__wake_only__"


def test_no_wake_word_returns_none():
    assert _extract("open notepad please") is None


def test_wake_word_artifact_prefix_is_stripped():
    cmd = _extract("what is the wake word Bibi search the news")
    assert cmd == "search the news"


def test_clarify_intent_routes_safely():
    intent = {
        "intent": "clarify",
        "parameters": {"follow_up": "Sorry, what was that?"},
        "raw_transcript": "mumble",
    }
    result = desktop_agent._route_intent(intent)
    assert result["success"] is True
    assert "Sorry, what was that?" in result["message"]


def test_general_intent_falls_through(monkeypatch):
    captured = {}

    def fake_general(params):
        captured["params"] = params
        return {"success": True, "message": "Handled by general", "data": {}}

    monkeypatch.setattr(desktop_agent.executor, "general", fake_general)

    intent = {
        "intent": "small_talk_unknown",
        "parameters": {},
        "raw_transcript": "tell me a joke",
    }
    result = desktop_agent._route_intent(intent)
    assert result["success"] is True
    assert captured["params"]["raw_transcript"] == "tell me a joke"
