import json
import os
from unittest.mock import MagicMock, patch

import cloud_router
import voice_intent


def test_should_use_cloud_router_for_low_risk_search():
    with patch.dict(os.environ, {"DECISION_ROUTER_MODE": "hybrid"}, clear=False):
        assert cloud_router.should_use_cloud_router("search on google how to build a minecraft server") is True


def test_should_not_use_cloud_router_for_sensitive_local_request():
    with patch.dict(os.environ, {"DECISION_ROUTER_MODE": "hybrid"}, clear=False):
        assert cloud_router.should_use_cloud_router("search my pc for resume.pdf") is False


def test_should_not_use_cloud_router_for_general_chitchat():
    with patch.dict(os.environ, {"DECISION_ROUTER_MODE": "hybrid"}, clear=False):
        assert cloud_router.should_use_cloud_router("who are you") is False


def test_should_not_use_cloud_router_for_multi_step_request():
    with patch.dict(os.environ, {"DECISION_ROUTER_MODE": "hybrid"}, clear=False):
        assert cloud_router.should_use_cloud_router("open claude and then tell me when it is ready") is False


def test_classify_intent_with_gemini_response():
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps({
                                "intent": "web_search",
                                "parameters": {"query": "how to create an aws policy generator"},
                                "confidence": 0.95,
                            })
                        }
                    ]
                }
            }
        ]
    }

    with patch.dict(
        os.environ,
        {
            "DECISION_ROUTER_MODE": "hybrid",
            "GEMINI_API_KEY": "test-key",
            "CLOUD_DECISION_MODEL": "gemini-2.5-flash-lite",
        },
        clear=False,
    ):
        with patch("cloud_router.requests.post", return_value=mock_response) as mock_post:
            result = cloud_router.classify_intent("search on google how to create an aws policy generator")

    assert result is not None
    assert result["intent"] == "web_search"
    assert result["parameters"]["query"] == "how to create an aws policy generator"
    mock_post.assert_called_once()


def test_parse_intent_handles_polite_open_request_locally():
    with patch("voice_intent.cloud_router.classify_intent", return_value={
        "intent": "open_app",
        "parameters": {"app_name": "Spotify"},
        "confidence": 0.91,
    }) as mock_classify:
        result = voice_intent._parse_intent("Could you open Spotify please")

    assert result["intent"] == "open_app"
    assert result["parameters"]["app_name"] == "Spotify"
    mock_classify.assert_not_called()


def test_fast_path_open_app_avoids_llm():
    with patch("voice_intent.cloud_router.classify_intent") as mock_cloud:
        with patch("voice_intent.world_model.resolve_app", return_value={"display_name": "Claude", "path": "C:/fake/Claude.exe"}):
            result = voice_intent._parse_intent("Open Claude")

    assert result["intent"] == "open_app"
    assert result["parameters"]["app_name"] == "Claude"
    mock_cloud.assert_not_called()


def test_fast_path_system_query_avoids_llm():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("What apps are running right now?")

    assert result["intent"] == "system_query"
    assert "running" in result["parameters"]["query"].lower()
    mock_cloud.assert_not_called()


def test_grounding_rejects_non_system_question_as_system_query():
    result = voice_intent._ground_intent_result(
        {
            "intent": "system_query",
            "parameters": {"query": "what is two plus two"},
            "confidence": 0.9,
        },
        "what is two plus two",
    )

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "what is two plus two"


def test_grounding_keeps_real_system_query():
    result = voice_intent._ground_intent_result(
        {
            "intent": "system_query",
            "parameters": {"query": "what apps are running right now"},
            "confidence": 0.9,
        },
        "what apps are running right now",
    )

    assert result["intent"] == "system_query"
    assert "running" in result["parameters"]["query"]


def test_fast_path_create_file_extracts_parameters():
    with patch("voice_intent.cloud_router.classify_intent") as mock_cloud:
        result = voice_intent._parse_intent("Create a Python file called hello.py with content print hello")

    assert result["intent"] == "create_file"
    assert result["parameters"]["file_name"] == "hello"
    assert result["parameters"]["file_type"] == "py"
    assert "print hello" in result["parameters"]["content"]
    mock_cloud.assert_not_called()


def test_fast_path_launch_minecraft_stays_general():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("launch minecraft")

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "launch minecraft"
    mock_cloud.assert_not_called()


def test_fast_path_open_and_search_stays_general():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("open claude and search what is the best speaker")

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "open claude and search what is the best speaker"
    mock_cloud.assert_not_called()


def test_fast_path_open_and_search_up_stays_general():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("open claude and search up the best speaker")

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "open claude and search up the best speaker"
    mock_cloud.assert_not_called()


def test_fast_path_open_and_message_stays_general():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("open whatsapp search John and write I am on my way")

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "open whatsapp search John and write I am on my way"
    mock_cloud.assert_not_called()


def test_fast_path_open_and_type_stays_general():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("open claude and type compare bose and sony")

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "open claude and type compare bose and sony"
    mock_cloud.assert_not_called()


def test_fast_path_codex_task_stays_general():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("use codex to build a notes app")

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "use codex to build a notes app"
    mock_cloud.assert_not_called()


def test_fast_path_current_window_interaction_stays_general():
    with patch("voice_intent.cloud_router.classify_intent", return_value=None) as mock_cloud:
        result = voice_intent._parse_intent("click launch in current window")

    assert result["intent"] == "general"
    assert result["parameters"]["raw_text"] == "click launch in current window"
    mock_cloud.assert_not_called()
