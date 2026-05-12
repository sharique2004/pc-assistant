from unittest.mock import patch

import app


def test_command_accepts_typed_text():
    client = app.app.test_client()
    parsed_intent = {
        "intent": "general",
        "parameters": {"raw_text": "who are you"},
        "raw_transcript": "who are you",
        "trigger": "typed_text",
        "confidence": 0.95,
    }

    with patch("app.voice_intent.parse_text_command", return_value=parsed_intent) as mock_parse:
        with patch("app.executor.general") as mock_general:
            mock_general.return_value = {
                "success": True,
                "message": "I am your local assistant.",
                "data": {},
                "requires_confirmation": False,
            }

            response = client.post(
                "/command",
                json={"trigger": "typed_text", "text": "who are you"},
            )

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["intent"]["raw_transcript"] == "who are you"
    assert data["result"]["message"] == "I am your local assistant."
    mock_parse.assert_called_once_with(transcript="who are you", trigger="typed_text")
    mock_general.assert_called_once_with(params={"raw_text": "who are you"})
