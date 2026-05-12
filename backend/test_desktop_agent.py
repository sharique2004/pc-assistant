import desktop_agent
import uuid


def test_extract_wake_command_with_inline_request():
    command = desktop_agent._extract_wake_command("Bibi open Claude", "bibi")
    assert command == "open Claude"


def test_extract_wake_command_wake_only():
    command = desktop_agent._extract_wake_command("hey bibi", "bibi")
    assert command == "__wake_only__"


def test_extract_wake_command_returns_none_without_wake_word():
    command = desktop_agent._extract_wake_command("open Claude", "bibi")
    assert command is None


def test_extract_wake_command_strips_wake_word_artifact_prefix():
    command = desktop_agent._extract_wake_command(
        "what is the wake word Bibi open Claude and search the best speaker",
        "bibi",
    )
    assert command == "open Claude and search the best speaker"


def test_single_instance_guard_blocks_duplicate_instance():
    mutex_name = f"Local\\BibiDesktopAssistantTest-{uuid.uuid4()}"
    first = desktop_agent._SingleInstanceGuard(mutex_name)
    second = desktop_agent._SingleInstanceGuard(mutex_name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        second.release()
        first.release()
