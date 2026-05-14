import os
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json

# Ensure env vars are set BEFORE importing executor so it doesn't crash on allowed paths missing
os.environ["WORKSPACE_DIR"] = "C:/test_workspace"
os.environ["ALLOWED_PATHS"] = "C:/test_allowed_1,C:/test_allowed_2"

import executor
import pc_state

@pytest.fixture
def mock_subprocess_popen():
    with patch("subprocess.Popen") as mock_popen:
        yield mock_popen

@pytest.fixture
def mock_startfile():
    with patch("os.startfile") as mock_startfile:
        yield mock_startfile

@pytest.fixture
def mock_requests_post():
    with patch("requests.post") as mock_post:
        yield mock_post


def test_resolve_app_path():
    # Chrome may resolve to either a literal chrome.exe path or to a
    # shell:AppsFolder\Chrome AUMID URI (Start Menu source).  Both are valid
    # launchable targets, so accept either.
    chrome_path = executor.resolve_app_path("chrome")
    lowered = chrome_path.lower()
    assert "chrome.exe" in lowered or "shell:appsfolder\\chrome" in lowered

    # Test lookup failure (should raise FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        executor.resolve_app_path("nonexistentapp123")


def test_resolve_app_path_alias_chat_gpt():
    # Force the legacy alias path: stub out world_model so we exercise the
    # _resolve_alias_path fallback that the static _APP_ALIAS_PATHS table feeds.
    with patch("executor.world_model.resolve_app", return_value=None):
        with patch("executor.os.path.exists", side_effect=lambda path: str(path).lower().endswith("chatgpt.exe")):
            path = executor.resolve_app_path("chat gpt")
            assert "chatgpt.exe" in path.lower()


def test_resolve_app_path_fuzzy_match():
    with patch("executor.world_model.resolve_app", return_value=None):
        with patch("executor._resolve_alias_path", return_value=None):
            with patch("executor._discover_launchable_apps", return_value={
                "claude": "C:/fake/Claude.exe",
                "chatgpt": "C:/fake/chatgpt.exe",
            }):
                path = executor.resolve_app_path("clod")
                assert path == "C:/fake/Claude.exe"


def test_open_app_success(mock_startfile):
    app_record = {"display_name": "Chrome", "path": "C:/fake_path/chrome.exe", "source": "alias"}
    with patch("executor.window_actions.focus_window", return_value={"success": False, "data": {}}):
        with patch("executor.world_model.resolve_app", return_value=app_record):
            with patch("executor.world_model.suggest_apps", return_value=[]):
                with patch("executor.world_model.remember_app_alias") as mock_remember_alias:
                    with patch("executor.resolve_app_path", return_value="C:/fake_path/chrome.exe"):
                        with patch("executor.os.path.exists", return_value=True):
                            with patch("executor._verify_app_launch", return_value=True):
                                result = executor.open_app("chrome")
                                assert result["success"] is True
                                assert result["data"]["exe_path"] == "C:/fake_path/chrome.exe"
                                assert result["data"]["resolved_app_name"].lower() == "chrome"
                                mock_startfile.assert_called_once_with("C:/fake_path/chrome.exe")
                                mock_remember_alias.assert_called_once()


def test_open_app_retries_when_world_model_path_is_stale(mock_startfile):
    stale_record = {"display_name": "claude", "path": "C:/stale/claude.exe", "source": "alias", "score": 1.0}
    live_suggestion = {
        "display_name": "claude",
        "path": "C:/live/claude.exe",
        "source": "running_process",
        "score": 1.8,
    }

    def _fake_startfile(path):
        if str(path) == "C:/stale/claude.exe":
            raise FileNotFoundError("stale")
        return None

    with patch("executor.window_actions.focus_window", return_value={"success": False, "data": {}}):
        with patch("executor.world_model.resolve_app", return_value=stale_record):
            with patch("executor.resolve_app_path", return_value="C:/stale/claude.exe"):
                with patch("executor.world_model.suggest_apps", return_value=[live_suggestion]):
                    with patch("executor.os.path.exists", side_effect=lambda path: str(path) == "C:/live/claude.exe"):
                        with patch("executor._verify_app_launch", return_value=True):
                            mock_startfile.side_effect = _fake_startfile
                            result = executor.open_app("Claude")

    assert result["success"] is True
    assert result["data"]["exe_path"] == "C:/live/claude.exe"
    assert mock_startfile.call_count == 1


def test_open_app_strips_pc_filler(mock_startfile):
    app_record = {
        "display_name": "Canvas",
        "path": "C:/Users/Sharique Khatri/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/Canvas.lnk",
        "source": "alias",
    }
    # Stub suggest_apps so the real SQLite (which may contain Canvas-like shell
    # AUMID entries) does not override the mocked alias record.
    with patch("executor.world_model.resolve_app", return_value=app_record):
        with patch("executor.world_model.suggest_apps", return_value=[]):
            with patch("executor._verify_app_launch", return_value=True):
                result = executor.open_app("canvas on my pc")

    assert result["success"] is True
    assert result["data"]["resolved_app_name"] == "Canvas"
    mock_startfile.assert_called_once_with(app_record["path"])


def test_create_file(mock_startfile, tmp_path):
    # Temporarily override WORKSPACE_DIR and ALLOWED_PATHS
    executor._WORKSPACE_DIR = str(tmp_path / "workspace")
    allowed = str(tmp_path / "workspace")
    os.environ["ALLOWED_PATHS"] = allowed
    
    # Mock _is_path_allowed to always return True for this test
    with patch("executor._is_path_allowed", return_value=True):
        result = executor.create_file("testfile", "txt", "hello world")
        assert result["success"] is False # because it returns confirmation required
        assert result["data"]["requires_confirmation"] is True
        op_id = result["data"]["operation_id"]
        
        # Confirm it
        confirm_result = executor.confirm_operation(op_id)
        assert confirm_result["success"] is True
        file_path = confirm_result["data"]["file_path"]
        assert "testfile.txt" in file_path
        
        # Ensure file was actually created in tmp_path
        assert os.path.exists(file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            assert f.read() == "hello world"
            
        mock_startfile.assert_called_once_with(file_path)


def test_create_app(mock_requests_post, mock_subprocess_popen, mock_startfile, tmp_path):
    executor._WORKSPACE_DIR = str(tmp_path / "workspace")

    # Mock the Ollama API response
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "response": '[{"filename": "main.py", "content": "print(\'test\')"}]'
    }
    mock_requests_post.return_value = mock_response

    # Pin the router to the local Ollama path so this test exercises that
    # branch even when claude.cmd / codex.cmd happen to be installed on the
    # host running the suite.
    with patch.dict(os.environ, {"APP_BUILDER": "ollama"}):
        result = executor.create_app("A simple test app")
        assert result["data"]["requires_confirmation"] is True
        op_id = result["data"]["operation_id"]

        with patch.dict(os.environ, {"VSCODE_PATH": "C:/fake_code.exe", "OLLAMA_HOST": "dummy"}):
            with patch("os.path.exists", return_value=True):
                confirm_result = executor.confirm_operation(op_id)

                assert confirm_result["success"] is True
                assert len(confirm_result["data"]["files_created"]) == 1
                project_dir = confirm_result["data"]["project_dir"]
                assert "simple-test-app" in project_dir
                mock_subprocess_popen.assert_called_once()


def test_create_app_rejects_path_traversal_from_model(mock_requests_post, tmp_path):
    executor._WORKSPACE_DIR = str(tmp_path / "workspace")

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "response": '[{"filename": "../escape.py", "content": "print(\\"bad\\")"}]'
    }
    mock_requests_post.return_value = mock_response

    with patch.dict(os.environ, {"APP_BUILDER": "ollama"}):
        result = executor.create_app("A simple test app")
        confirm_result = executor.confirm_operation(result["data"]["operation_id"])

    assert confirm_result["success"] is False
    assert "valid project files" in confirm_result["message"]
    assert not (tmp_path / "workspace" / "escape.py").exists()


def test_search_pc(tmp_path):
    d1 = tmp_path / "dir1"
    d1.mkdir()
    (d1 / "test_resume.pdf").write_text("dummy")
    (d1 / "other.txt").write_text("dummy")
    
    os.environ["ALLOWED_PATHS"] = str(d1)
    
    result = executor.search_pc("resume")
    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert "test_resume.pdf" in result["data"]["results"][0]


def test_web_search_opens_browser():
    with patch("executor.webbrowser.open", return_value=True) as mock_open:
        result = executor.web_search("search on google how to create aws policy generator")

    assert result["success"] is True
    assert result["data"]["query"] == "how to create aws policy generator"
    assert "google.com/search" in result["data"]["url"]
    mock_open.assert_called_once()


def test_search_pc_redirects_google_search():
    with patch("executor.web_search") as mock_web_search:
        mock_web_search.return_value = {
            "success": True,
            "message": "Searching the web for how to create a towel.",
            "data": {"query": "how to create a towel"},
            "requires_confirmation": False,
        }
        result = executor.search_pc("search and google how to create a towel")

    assert result["success"] is True
    mock_web_search.assert_called_once_with("how to create a towel")


def test_web_search_strips_wake_word_artifact_prefix():
    with patch("executor.webbrowser.open", return_value=True) as mock_open:
        result = executor.web_search("what is the wake wordthe best speaker")

    assert result["success"] is True
    assert result["data"]["query"] == "the best speaker"
    mock_open.assert_called_once()


def test_web_search_extracts_open_google_phrase():
    with patch("executor.webbrowser.open", return_value=True) as mock_open:
        result = executor.web_search("open google and search how to create a minecraft server")

    assert result["success"] is True
    assert result["data"]["query"] == "how to create a minecraft server"
    mock_open.assert_called_once()


def test_system_query():
    with patch("pc_state.get_state") as mock_state:
        mock_state.return_value = {
            "active_window": "VS Code",
            "cpu_percent": 45.0,
            "memory": {"percent": 60.0}
        }
        
        # Test full state fetch
        result = executor.system_query("what is the state?")
        assert result["data"]["cpu_percent"] == 45.0
        
        # Test filtered state fetch
        result2 = executor.system_query("how much cpu?")
        assert "cpu_percent" in result2["data"]
        assert "active_window" not in result2["data"]


def test_general(mock_requests_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": "I am a helpful assistant."}
    mock_requests_post.return_value = mock_response
    
    result = executor.general({"raw_transcript": "Who are you?"})
    assert result["success"] is True
    assert result["message"] == "I am a helpful assistant."
    mock_requests_post.assert_called_once()


def test_general_remember_fact():
    with patch("executor.world_model.store_memory") as mock_store_memory:
        result = executor.general({"raw_transcript": "remember that my favorite editor is VS Code"})
        assert result["success"] is True
        assert "remember" in result["message"].lower()
        mock_store_memory.assert_called_once_with("my favorite editor is VS Code", importance=1.2)


def test_general_planner_executes_tool_then_final():
    with patch("executor._call_ollama_generate") as mock_generate:
        mock_generate.side_effect = [
            json.dumps({
                "type": "tool",
                "tool": "open_app",
                "arguments": {"app_name": "Claude"},
                "reason": "Open the requested application first."
            }),
            json.dumps({
                "type": "final",
                "response": "Claude is open and ready.",
                "confidence": 0.94
            }),
        ]

        with patch("executor.open_app") as mock_open_app:
            mock_open_app.return_value = {
                "success": True,
                "message": "Opened Claude.",
                "data": {"resolved_app_name": "Claude"},
                "requires_confirmation": False,
            }
            result = executor.general({"raw_transcript": "open claude and let me know when it is ready"})

    assert result["success"] is True
    assert result["message"] == "Claude is open and ready."
    assert result["data"]["planner_steps"][0]["tool"] == "open_app"
    mock_open_app.assert_called_once_with("Claude")


def test_automate_app_search_success():
    with patch("executor.open_app") as mock_open_app:
        mock_open_app.return_value = {
            "success": True,
            "message": "Opened Claude.",
            "data": {"resolved_app_name": "Claude"},
            "requires_confirmation": False,
        }
        with patch("executor.window_actions.app_search") as mock_app_search:
            mock_app_search.return_value = {
                "success": True,
                "message": "Typed into Claude.",
                "data": {"window_title": "Claude"},
                "requires_confirmation": False,
            }
            result = executor.automate_app_search("Claude", "what is the best speaker")

    assert result["success"] is True
    assert "submitted your search" in result["message"].lower()
    mock_open_app.assert_called_once_with("Claude")
    mock_app_search.assert_called_once_with("Claude", "what is the best speaker")


def test_general_shortcut_open_and_search():
    with patch("executor.automate_app_search") as mock_automate:
        mock_automate.return_value = {
            "success": True,
            "message": "Opened Claude and submitted your search.",
            "data": {},
            "requires_confirmation": False,
        }
        result = executor.general({"raw_transcript": "open claude and search what is the best speaker"})

    assert result["success"] is True
    mock_automate.assert_called_once_with("claude", "what is the best speaker")


def test_general_shortcut_open_and_search_up():
    with patch("executor.automate_app_search") as mock_automate:
        mock_automate.return_value = {
            "success": True,
            "message": "Opened Claude and submitted your search.",
            "data": {},
            "requires_confirmation": False,
        }
        result = executor.general({"raw_transcript": "open claude and search up the best speaker"})

    assert result["success"] is True
    mock_automate.assert_called_once_with("claude", "the best speaker")


def test_general_shortcut_open_and_type():
    with patch("executor.automate_open_and_type") as mock_automate:
        mock_automate.return_value = {
            "success": True,
            "message": "Opened Claude and typed into it.",
            "data": {},
            "requires_confirmation": False,
        }
        result = executor.general({"raw_transcript": "open claude and type compare bose and sony"})

    assert result["success"] is True
    mock_automate.assert_called_once_with("claude", "compare bose and sony", submit=False, navigate_search=False)


def test_general_shortcut_type_in_current_window():
    with patch("executor.automate_window_text") as mock_automate:
        mock_automate.return_value = {
            "success": True,
            "message": "Typed into Claude.",
            "data": {"window_title": "Claude"},
            "requires_confirmation": False,
        }
        result = executor.general({"raw_transcript": "type compare bose and sony in current window"})

    assert result["success"] is True
    mock_automate.assert_called_once_with(
        "current window",
        "compare bose and sony",
        submit=False,
        navigate_search=False,
    )


def test_general_shortcut_click_button_in_current_window():
    with patch("executor.automate_click_button") as mock_click:
        mock_click.return_value = {
            "success": True,
            "message": "Clicked Launch in Prism Launcher.",
            "data": {"window_title": "Prism Launcher"},
            "requires_confirmation": False,
        }
        result = executor.general({"raw_transcript": "click launch in current window"})

    assert result["success"] is True
    mock_click.assert_called_once_with("current window", "launch")


def test_general_shortcut_press_keys_in_current_window():
    with patch("executor.automate_press_keys") as mock_press:
        mock_press.return_value = {
            "success": True,
            "message": "Pressed ctrl l in Claude.",
            "data": {"window_title": "Claude"},
            "requires_confirmation": False,
        }
        result = executor.general({"raw_transcript": "press ctrl l in current window"})

    assert result["success"] is True
    mock_press.assert_called_once_with("current window", "ctrl l")


def test_general_shortcut_launch_minecraft():
    with patch("executor.launch_minecraft") as mock_launch_minecraft:
        mock_launch_minecraft.return_value = {
            "success": True,
            "message": "Opened Prism Launcher and clicked Launch.",
            "data": {},
            "requires_confirmation": False,
        }
        result = executor.general({"raw_transcript": "launch minecraft"})

    assert result["success"] is True
    mock_launch_minecraft.assert_called_once()


def test_automate_app_message_success():
    with patch("executor.open_app") as mock_open_app:
        mock_open_app.return_value = {
            "success": True,
            "message": "Opened WhatsApp.",
            "data": {"resolved_app_name": "WhatsApp"},
            "requires_confirmation": False,
        }
        with patch("executor.window_actions.app_send_message") as mock_send_message:
            mock_send_message.return_value = {
                "success": True,
                "message": "Sent your message to John.",
                "data": {"contact_name": "John"},
                "requires_confirmation": False,
            }
            result = executor.automate_app_message("WhatsApp", "John", "I am on my way")

    assert result["success"] is True
    assert "sent your message" in result["message"].lower()
    mock_open_app.assert_called_once_with("WhatsApp")
    mock_send_message.assert_called_once_with("WhatsApp", "John", "I am on my way")


def test_general_shortcut_open_and_message():
    with patch("executor.automate_app_message") as mock_app_message:
        mock_app_message.return_value = {
            "success": True,
            "message": "Opened WhatsApp and sent your message to John.",
            "data": {},
            "requires_confirmation": False,
        }
        result = executor.general({
            "raw_transcript": "open whatsapp search John and write I am on my way"
        })

    assert result["success"] is True
    mock_app_message.assert_called_once_with("whatsapp", "John", "I am on my way")


def test_general_shortcut_codex_task():
    with patch("executor.run_codex_task") as mock_codex_task:
        mock_codex_task.return_value = {
            "success": False,
            "message": "This action requires your confirmation.",
            "data": {"requires_confirmation": True, "operation_id": "op"},
            "requires_confirmation": True,
        }
        result = executor.general({"raw_transcript": "use codex to build a notes app"})

    assert result["data"]["requires_confirmation"] is True
    mock_codex_task.assert_called_once_with("build a notes app")


def test_create_app_routes_react_dashboard_to_claude():
    # React / dashboard / frontend markers trip the Claude routing path.
    with patch("executor.run_claude_task") as mock_claude_task:
        mock_claude_task.return_value = {
            "success": False,
            "message": "This action requires your confirmation.",
            "data": {"requires_confirmation": True, "operation_id": "op"},
            "requires_confirmation": True,
        }
        result = executor.create_app("a React dashboard app with authentication and a database")

    assert result["data"]["requires_confirmation"] is True
    mock_claude_task.assert_called_once()
    assert "React dashboard" in mock_claude_task.call_args.args[0]


def test_create_app_uses_codex_for_complex_script_requests():
    # Script-style phrasing should route to Codex when Codex is installed.
    description = (
        "a python script that scans a folder full of log files, parses out "
        "timestamps and error codes, and produces a CSV summary"
    )
    with patch("executor._claude_cli_available", return_value=True):
        with patch("executor._codex_cli_available", return_value=True):
            with patch("executor.run_codex_task") as mock_codex_task:
                mock_codex_task.return_value = {
                    "success": False,
                    "message": "This action requires your confirmation.",
                    "data": {"requires_confirmation": True, "operation_id": "op"},
                    "requires_confirmation": True,
                }
                result = executor.create_app(description)

    assert result["data"]["requires_confirmation"] is True
    mock_codex_task.assert_called_once()


def test_create_app_generic_description_defaults_to_claude():
    # The original gap reported in code review: generic "create an application
    # that tracks X" was falling through to local Ollama instead of Claude.
    # With Claude installed, plain "applications" must go to Claude.
    with patch("executor._claude_cli_available", return_value=True):
        with patch("executor._codex_cli_available", return_value=True):
            with patch("executor.run_claude_task") as mock_claude_task:
                mock_claude_task.return_value = {
                    "success": False,
                    "message": "This action requires your confirmation.",
                    "data": {"requires_confirmation": True, "operation_id": "op"},
                    "requires_confirmation": True,
                }
                result = executor.create_app("an application that tracks my daily workouts")

    assert result["data"]["requires_confirmation"] is True
    mock_claude_task.assert_called_once()


def test_create_app_falls_back_to_codex_when_claude_unavailable():
    # If Claude CLI is missing but Codex is installed, generic app requests
    # should land on Codex - not collapse to local Ollama.
    with patch("executor._claude_cli_available", return_value=False):
        with patch("executor._codex_cli_available", return_value=True):
            with patch("executor.run_codex_task") as mock_codex_task:
                mock_codex_task.return_value = {
                    "success": False,
                    "message": "This action requires your confirmation.",
                    "data": {"requires_confirmation": True, "operation_id": "op"},
                    "requires_confirmation": True,
                }
                result = executor.create_app("an application that tracks my daily workouts")

    assert result["data"]["requires_confirmation"] is True
    mock_codex_task.assert_called_once()


def test_create_app_uses_ollama_only_when_no_cli_installed(mock_requests_post, tmp_path):
    # Last-resort path: with both CLIs missing, the local Ollama codegen
    # branch still runs.
    executor._WORKSPACE_DIR = str(tmp_path / "workspace")
    mock_requests_post.return_value.json.return_value = {
        "response": '[{"filename": "main.py", "content": "print(\'hello\')"}]'
    }
    with patch("executor._claude_cli_available", return_value=False):
        with patch("executor._codex_cli_available", return_value=False):
            result = executor.create_app("an application that tracks my daily workouts")

    # Falls into the Ollama _queue_operation path (no Claude/Codex run).
    assert result["data"]["requires_confirmation"] is True
    assert "operation_id" in result["data"]


def test_automate_app_message_blocks_send_when_recipient_unverifiable():
    # Recipient safety gate: app_send_message returns
    # requires_recipient_confirmation when it cannot read the chat header.
    # The executor wrapper must NOT auto-send - it returns a hard refusal
    # and does NOT queue a confirmation path for the unreadable case.
    fake_open = {
        "success": True,
        "data": {"resolved_app_name": "WhatsApp"},
    }
    fake_send = {
        "success": False,
        "message": "Could not confirm recipient.",
        "data": {
            "requires_recipient_confirmation": True,
            "verification": "unreadable",
            "detected_chat": "",
        },
        "requires_confirmation": True,
    }
    with patch("executor.open_app", return_value=fake_open):
        with patch("executor.window_actions.app_send_message", return_value=fake_send):
            result = executor.automate_app_message("WhatsApp", "Alex", "hello")

    assert result["success"] is False
    assert result["requires_confirmation"] is False
    assert "did not send" in result["message"].lower()


def test_automate_app_message_queues_confirm_when_recipient_mismatched():
    # Recipient safety gate: when the open chat doesn't match the intended
    # contact, executor queues a force-send operation that requires user
    # /confirm before delivering.
    fake_open = {
        "success": True,
        "data": {"resolved_app_name": "WhatsApp"},
    }
    fake_send = {
        "success": False,
        "message": "Mismatch.",
        "data": {
            "requires_recipient_confirmation": True,
            "verification": "mismatch",
            "detected_chat": "Bob Smith",
        },
        "requires_confirmation": True,
    }
    with patch("executor.open_app", return_value=fake_open):
        with patch("executor.window_actions.app_send_message", return_value=fake_send):
            result = executor.automate_app_message("WhatsApp", "Alex", "hello")

    assert result["data"]["requires_confirmation"] is True
    assert "operation_id" in result["data"]
    assert "bob smith" in result["data"].get("description", "").lower()


def test_run_codex_task_queues_and_executes(tmp_path):
    executor._WORKSPACE_DIR = str(tmp_path / "workspace")
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = "codex output"
    completed.stderr = ""

    with patch("executor._resolve_codex_executable", return_value="codex.cmd"):
        with patch("executor.subprocess.run", return_value=completed) as mock_run:
            with patch("executor.os.startfile") as mock_startfile:
                result = executor.run_codex_task("build a calculator app", project_name="calculator")
                assert result["data"]["requires_confirmation"] is True
                confirm_result = executor.confirm_operation(result["data"]["operation_id"])

    assert confirm_result["success"] is True
    assert "calculator" in confirm_result["data"]["project_dir"]
    command = mock_run.call_args.args[0]
    assert command[:2] == ["codex.cmd", "exec"]
    assert "--ask-for-approval" in command
    assert "never" in command
    mock_startfile.assert_called_once()


def test_run_claude_task_passes_prompt_positionally(tmp_path):
    # Claude Code on Windows refuses --print invocations when the prompt is
    # piped through stdin via a .cmd shim ("Input must be provided either
    # through stdin or as a prompt argument when using --print").  The fix
    # is to append the built prompt as the final positional argument; this
    # test pins that calling convention.
    executor._WORKSPACE_DIR = str(tmp_path / "workspace")
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = "claude output"
    completed.stderr = ""

    with patch.dict(os.environ, {"VSCODE_PATH": ""}):
        with patch("executor._resolve_claude_executable", return_value="claude.exe"):
            with patch("executor.subprocess.run", return_value=completed) as mock_run:
                with patch("executor.os.startfile") as mock_startfile:
                    result = executor.run_claude_task("build a workout tracker", project_name="workout tracker")
                    assert result["data"]["requires_confirmation"] is True
                    confirm_result = executor.confirm_operation(result["data"]["operation_id"])

    assert confirm_result["success"] is True
    assert "workout-tracker" in confirm_result["data"]["project_dir"]
    command = mock_run.call_args.args[0]
    kwargs = mock_run.call_args.kwargs
    assert command[0] == "claude.exe"
    assert "--print" in command
    # Prompt is the last positional argument and contains the task verbatim.
    assert "build a workout tracker" in command[-1]
    # The `--` separator must precede the prompt so that Claude's argv parser
    # does not consume the prompt as a second value to --add-dir.
    assert command[-2] == "--"
    # We deliberately stopped piping the prompt through stdin.
    assert "input" not in kwargs
    mock_startfile.assert_called_once()
