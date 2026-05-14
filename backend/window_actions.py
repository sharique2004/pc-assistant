"""Local window automation helpers for the desktop assistant."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

try:
    import pygetwindow as gw
except Exception:  # noqa: BLE001
    gw = None

try:
    from pywinauto import Desktop, clipboard
    from pywinauto.keyboard import send_keys

    _HAS_PYWINAUTO = True
except Exception:  # noqa: BLE001
    Desktop = None
    clipboard = None
    send_keys = None
    _HAS_PYWINAUTO = False

try:
    import win32clipboard
    import win32con

    _HAS_WIN32_CLIPBOARD = True
except Exception:  # noqa: BLE001
    win32clipboard = None
    win32con = None
    _HAS_WIN32_CLIPBOARD = False

_WINDOW_TIMEOUT_S = 12.0
_WINDOW_RETRY_DELAY_S = 0.35
_CURRENT_WINDOW_ALIASES = {
    "",
    "current",
    "current app",
    "current window",
    "current tab",
    "this app",
    "this window",
    "this tab",
    "active app",
    "active window",
    "active tab",
    "it",
}
_SAFE_KEY_ALIASES = {
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "tab": "{TAB}",
    "escape": "{ESC}",
    "esc": "{ESC}",
    "space": " ",
    "backspace": "{BACKSPACE}",
    "delete": "{DELETE}",
    "page down": "{PGDN}",
    "pagedown": "{PGDN}",
    "page up": "{PGUP}",
    "pageup": "{PGUP}",
    "home": "{HOME}",
    "end": "{END}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "ctrl l": "^l",
    "control l": "^l",
    "ctrl f": "^f",
    "control f": "^f",
    "ctrl k": "^k",
    "control k": "^k",
    "ctrl a": "^a",
    "control a": "^a",
    "ctrl w": "^w",
    "control w": "^w",
    "ctrl tab": "^{TAB}",
    "control tab": "^{TAB}",
    "shift tab": "+{TAB}",
    "alt left": "%{LEFT}",
    "alt right": "%{RIGHT}",
    "alt tab": "%{TAB}",
}

_WINDOW_ALIASES = {
    "claude": "Claude",
    "anthropic claude": "Claude",
    "chatgpt": "ChatGPT",
    "chat gpt": "ChatGPT",
    "chrome": "Chrome",
    "google chrome": "Chrome",
    "edge": "Edge",
    "microsoft edge": "Edge",
    "firefox": "Firefox",
    "brave": "Brave",
    "prism": "Prism Launcher",
    "prism launcher": "Prism Launcher",
    "minecraft": "Prism Launcher",
}

_BROWSER_ALIASES = {
    "chrome",
    "google chrome",
    "edge",
    "microsoft edge",
    "firefox",
    "brave",
}

_CHAT_APP_ALIASES = {
    "claude",
    "anthropic claude",
    "chatgpt",
    "chat gpt",
    "whatsapp",
    "whatsapp desktop",
}

_MESSAGE_APP_PROFILES: dict[str, dict[str, Any]] = {
    "whatsapp": {
        "window_hint": "WhatsApp",
        "search_shortcut": "^f",
        "confirm_search_with_enter": True,
        "search_pause_s": 0.2,
        "post_search_pause_s": 0.65,
        "submit_message": True,
        "verify_recipient": True,
    },
    "whatsapp desktop": {
        "window_hint": "WhatsApp",
        "search_shortcut": "^f",
        "confirm_search_with_enter": True,
        "search_pause_s": 0.2,
        "post_search_pause_s": 0.65,
        "submit_message": True,
        "verify_recipient": True,
    },
}


@dataclass
class WindowMatch:
    """Small wrapper for a candidate desktop window."""

    title: str
    handle: Any
    score: float


def get_active_window_title() -> str:
    """Return the current foreground window title when available."""
    if gw is None:
        return ""
    try:
        title = gw.getActiveWindowTitle()
    except Exception:  # noqa: BLE001
        return ""
    return str(title or "").strip()


def focus_window(window_hint: str, timeout_s: float = _WINDOW_TIMEOUT_S) -> dict[str, Any]:
    """
    Focus a top-level desktop window whose title matches the supplied hint.

    Args:
        window_hint (str): Spoken or typed app/window name.
        timeout_s (float): Maximum time to wait for the window to appear.

    Returns:
        dict[str, Any]: Structured result containing the focused window title.
    """
    resolved_hint = _resolve_runtime_window_hint(window_hint)
    if not resolved_hint:
        return {
            "success": False,
            "message": "I could not determine which window to focus.",
            "data": {"window_hint": window_hint},
            "requires_confirmation": False,
        }

    match = _wait_for_window(resolved_hint, timeout_s=timeout_s)
    if match is None:
        return {
            "success": False,
            "message": f"I could not find a window for {resolved_hint}.",
            "data": {"window_hint": resolved_hint, "requested_window_hint": window_hint},
            "requires_confirmation": False,
        }

    try:
        _activate_window(match.handle, match.title)
        return {
            "success": True,
            "message": f"Focused {match.title}.",
            "data": {
                "window_title": match.title,
                "window_hint": resolved_hint,
                "requested_window_hint": window_hint,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "message": f"I found {match.title} but could not focus it: {exc}",
            "data": {
                "window_title": match.title,
                "window_hint": resolved_hint,
                "requested_window_hint": window_hint,
            },
            "requires_confirmation": False,
        }


def type_text(
    window_hint: str,
    text: str,
    *,
    submit: bool = True,
    select_all: bool = False,
    navigate_search: bool = False,
    timeout_s: float = _WINDOW_TIMEOUT_S,
) -> dict[str, Any]:
    """
    Focus a window and paste text into it using local keyboard automation.

    Args:
        window_hint (str): Title hint for the destination window.
        text (str): Text to paste into the window.
        submit (bool): Press Enter after typing when True.
        select_all (bool): Send Ctrl+A first to replace the current field.
        navigate_search (bool): Send Ctrl+L first, useful for browsers.
        timeout_s (float): Maximum wait time for the window to appear.

    Returns:
        dict[str, Any]: Structured automation result.
    """
    if not _HAS_PYWINAUTO:
        return _missing_automation_dependency_result()

    clean_text = " ".join(str(text or "").split()).strip()
    if not clean_text:
        return {
            "success": False,
            "message": "I need text to type into the target window.",
            "data": {"window_hint": window_hint},
            "requires_confirmation": False,
        }

    focus_result = focus_window(window_hint, timeout_s=timeout_s)
    if not focus_result.get("success"):
        return focus_result

    try:
        if navigate_search:
            send_keys("^l")
            time.sleep(0.15)
        elif select_all:
            send_keys("^a")
            time.sleep(0.08)

        _paste_text(clean_text)
        if submit:
            time.sleep(0.08)
            send_keys("{ENTER}")

        return {
            "success": True,
            "message": f"Typed into {focus_result['data'].get('window_title', window_hint)}.",
            "data": {
                "window_hint": window_hint,
                "window_title": focus_result["data"].get("window_title"),
                "typed_text": clean_text,
                "submitted": submit,
                "navigate_search": navigate_search,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Window typing failed")
        return {
            "success": False,
            "message": f"I could not type into {window_hint}: {exc}",
            "data": {
                "window_hint": window_hint,
                "window_title": focus_result["data"].get("window_title"),
            },
            "requires_confirmation": False,
        }


def click_button(window_hint: str, button_text: str, timeout_s: float = _WINDOW_TIMEOUT_S) -> dict[str, Any]:
    """
    Click a visible button inside a desktop window using UI Automation.

    Args:
        window_hint (str): Title hint for the destination window.
        button_text (str): Visible button label to click.
        timeout_s (float): Maximum wait time for the window to appear.

    Returns:
        dict[str, Any]: Structured automation result.
    """
    if not _HAS_PYWINAUTO:
        return _missing_automation_dependency_result()

    match = _wait_for_window(window_hint, timeout_s=timeout_s)
    if match is None:
        return {
            "success": False,
            "message": f"I could not find a window for {window_hint}.",
            "data": {"window_hint": window_hint, "button_text": button_text},
            "requires_confirmation": False,
        }

    try:
        _activate_window(match.handle, match.title)
        ui_window = _find_ui_window(match.title)
        if ui_window is None:
            raise RuntimeError("UI Automation could not connect to the target window.")

        target_button = _find_button(ui_window, button_text)
        if target_button is None:
            raise RuntimeError(f"I could not find a '{button_text}' button.")

        target_button.click_input()
        return {
            "success": True,
            "message": f"Clicked {button_text} in {match.title}.",
            "data": {
                "window_hint": window_hint,
                "window_title": match.title,
                "button_text": button_text,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Button click failed")
        return {
            "success": False,
            "message": f"I could not click {button_text} in {match.title}: {exc}",
            "data": {
                "window_hint": window_hint,
                "window_title": match.title,
                "button_text": button_text,
            },
            "requires_confirmation": False,
        }


def press_keys(window_hint: str, keys: str, timeout_s: float = _WINDOW_TIMEOUT_S) -> dict[str, Any]:
    """
    Focus a window and send a safe keyboard shortcut or key press.

    Args:
        window_hint (str): Title hint for the destination window.
        keys (str): Human-readable shortcut like "ctrl l" or "enter".
        timeout_s (float): Maximum wait time for the window to appear.

    Returns:
        dict[str, Any]: Structured automation result.
    """
    if not _HAS_PYWINAUTO or send_keys is None:
        return _missing_automation_dependency_result()

    key_sequence = _normalize_key_sequence(keys)
    if not key_sequence:
        return {
            "success": False,
            "message": f"I cannot safely press '{keys}' yet.",
            "data": {"window_hint": window_hint, "requested_keys": keys},
            "requires_confirmation": False,
        }

    focus_result = focus_window(window_hint, timeout_s=timeout_s)
    if not focus_result.get("success"):
        return focus_result

    try:
        send_keys(key_sequence)
        return {
            "success": True,
            "message": f"Pressed {keys} in {focus_result['data'].get('window_title', window_hint)}.",
            "data": {
                "window_hint": focus_result["data"].get("window_hint", window_hint),
                "window_title": focus_result["data"].get("window_title"),
                "requested_keys": keys,
                "key_sequence": key_sequence,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Window shortcut failed")
        return {
            "success": False,
            "message": f"I could not press {keys} in {window_hint}: {exc}",
            "data": {
                "window_hint": focus_result["data"].get("window_hint", window_hint),
                "window_title": focus_result["data"].get("window_title"),
                "requested_keys": keys,
            },
            "requires_confirmation": False,
        }


def app_search(app_name: str, query: str, timeout_s: float = _WINDOW_TIMEOUT_S) -> dict[str, Any]:
    """
    Send a query into a known application after it has been opened.

    Args:
        app_name (str): Target application name.
        query (str): Text to submit in that application's UI.
        timeout_s (float): Maximum wait time for the app window.

    Returns:
        dict[str, Any]: Structured automation result.
    """
    normalized_app = _normalize_text(app_name)
    if normalized_app in _BROWSER_ALIASES:
        return type_text(
            app_name,
            query,
            submit=True,
            navigate_search=True,
            timeout_s=timeout_s,
        )

    if normalized_app in _CHAT_APP_ALIASES:
        return type_text(
            app_name,
            query,
            submit=True,
            select_all=False,
            timeout_s=timeout_s,
        )

    return type_text(
        app_name,
        query,
        submit=True,
        select_all=False,
        timeout_s=timeout_s,
    )


def app_send_message(
    app_name: str,
    contact_name: str,
    message: str,
    timeout_s: float = _WINDOW_TIMEOUT_S,
) -> dict[str, Any]:
    """
    Open a chat-style app window, select a contact, and send a message.

    Args:
        app_name (str): Target chat application.
        contact_name (str): Person or chat to search for.
        message (str): Message body to send.
        timeout_s (float): Maximum wait time for the window.

    Returns:
        dict[str, Any]: Structured automation result.
    """
    if not _HAS_PYWINAUTO:
        return _missing_automation_dependency_result()

    profile = _resolve_message_profile(app_name)
    if profile is None:
        return {
            "success": False,
            "message": f"I do not have a messaging automation profile for {app_name} yet.",
            "data": {"app_name": app_name},
            "requires_confirmation": False,
        }

    clean_contact = " ".join(str(contact_name or "").split()).strip()
    clean_message = " ".join(str(message or "").split()).strip()
    if not clean_contact or not clean_message:
        return {
            "success": False,
            "message": "I need both a contact name and a message.",
            "data": {"app_name": app_name, "contact_name": contact_name},
            "requires_confirmation": False,
        }

    focus_result = focus_window(str(profile.get("window_hint") or app_name), timeout_s=timeout_s)
    if not focus_result.get("success"):
        return focus_result

    window_title = focus_result["data"].get("window_title", app_name)

    try:
        search_shortcut = str(profile.get("search_shortcut") or "")
        if search_shortcut:
            send_keys(search_shortcut)
            time.sleep(float(profile.get("search_pause_s", 0.18)))

        _paste_text(clean_contact)
        if profile.get("confirm_search_with_enter", True):
            time.sleep(0.08)
            send_keys("{ENTER}")
        time.sleep(float(profile.get("post_search_pause_s", 0.3)))

        # ---- Recipient verification gate ------------------------------------
        # Read the chat header from the focused window's UI tree and compare
        # it to the intended contact name.  If the header does not look like
        # the intended contact (or we can't read it at all), STOP - return
        # requires_confirmation: True with details rather than typing into
        # whatever chat is open.  This prevents wrong-recipient sends when
        # WhatsApp search returns nothing or the user has another chat focused.
        verify_recipient = profile.get("verify_recipient", False)
        chat_header: str | None = None
        verification_state = "skipped"
        if verify_recipient:
            chat_header = _read_chat_header_text(window_title)
            verification_state = _classify_recipient_match(chat_header, clean_contact)

            if verification_state != "match":
                # Escape the search field so WhatsApp is left in a clean state.
                try:
                    send_keys("{ESC}")
                except Exception:  # noqa: BLE001
                    pass
                detected = chat_header if chat_header else "(could not read the chat header)"
                if verification_state == "unreadable":
                    detail = (
                        f"I could not confirm that the open chat in {app_name} is {clean_contact}. "
                        "Confirm to send the message anyway, or cancel and try again."
                    )
                else:
                    detail = (
                        f"The open chat in {app_name} looks like \"{detected}\", "
                        f"not {clean_contact}. Confirm only if you really want to send there."
                    )
                return {
                    "success": False,
                    "message": detail,
                    "data": {
                        "app_name": app_name,
                        "contact_name": clean_contact,
                        "window_title": window_title,
                        "detected_chat": detected,
                        "verification": verification_state,
                        "message_preview": clean_message,
                        "requires_recipient_confirmation": True,
                    },
                    "requires_confirmation": True,
                }
        # ----------------------------------------------------------------------

        _paste_text(clean_message)
        if profile.get("submit_message", True):
            time.sleep(0.08)
            send_keys("{ENTER}")

        return {
            "success": True,
            "message": f"Sent your message to {clean_contact} in {window_title}.",
            "data": {
                "app_name": app_name,
                "contact_name": clean_contact,
                "window_title": window_title,
                "detected_chat": chat_header,
                "verification": verification_state,
                "message": clean_message,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Chat message automation failed")
        return {
            "success": False,
            "message": f"I could not send the message in {app_name}: {exc}",
            "data": {
                "app_name": app_name,
                "contact_name": clean_contact,
                "window_title": window_title,
            },
            "requires_confirmation": False,
        }


def send_open_chat_message(app_name: str, message: str, timeout_s: float = _WINDOW_TIMEOUT_S) -> dict[str, Any]:
    """
    Type and send a message body into whatever chat is currently open in a
    messaging app, without running search.  Used as the /confirm path after
    the user has approved sending to a chat that didn't match the originally
    requested contact.
    """
    if not _HAS_PYWINAUTO:
        return _missing_automation_dependency_result()

    profile = _resolve_message_profile(app_name)
    window_hint = str((profile or {}).get("window_hint") or app_name)
    focus_result = focus_window(window_hint, timeout_s=timeout_s)
    if not focus_result.get("success"):
        return focus_result

    clean_message = " ".join(str(message or "").split()).strip()
    if not clean_message:
        return {
            "success": False,
            "message": "No message body was supplied to send.",
            "data": {"app_name": app_name},
            "requires_confirmation": False,
        }

    try:
        _paste_text(clean_message)
        if (profile or {}).get("submit_message", True):
            time.sleep(0.08)
            send_keys("{ENTER}")
        return {
            "success": True,
            "message": f"Sent your message in {focus_result['data'].get('window_title', app_name)}.",
            "data": {
                "app_name": app_name,
                "window_title": focus_result["data"].get("window_title"),
                "message": clean_message,
            },
            "requires_confirmation": False,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Confirmed open-chat send failed")
        return {
            "success": False,
            "message": f"I could not deliver the confirmed message in {app_name}: {exc}",
            "data": {
                "app_name": app_name,
                "window_title": focus_result["data"].get("window_title"),
            },
            "requires_confirmation": False,
        }


def _read_chat_header_text(window_title: str, timeout_s: float = 1.6) -> str | None:
    """
    Best-effort read of the currently open chat's header text inside a
    messaging app, by walking the focused window's UIA tree.

    Returns the most likely "this is the contact name" string, or None if
    pywinauto is unavailable, the window cannot be located, or no plausible
    header was found.

    Used as a recipient safety check before app_send_message types the body.
    """
    if not _HAS_PYWINAUTO or Desktop is None:
        return None

    ui_window = _find_ui_window(window_title)
    if ui_window is None:
        return None

    deadline = time.time() + max(0.4, timeout_s)
    candidates: list[str] = []

    while time.time() < deadline:
        candidates = []
        try:
            descendants = ui_window.descendants(control_type="Text") or []
        except Exception:  # noqa: BLE001
            descendants = []

        for element in descendants:
            try:
                text = str(element.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not text or len(text) > 80:
                continue
            normalized = _normalize_text(text)
            if not normalized:
                continue
            # Discard noise: app chrome, generic labels, system strings.
            if normalized in _CHAT_HEADER_NOISE:
                continue
            if any(noise in normalized for noise in _CHAT_HEADER_NOISE_SUBSTRINGS):
                continue
            candidates.append(text)
            # First plausible match is almost always the open chat title
            # because pywinauto traverses the tree top-down.
            return text

        time.sleep(0.18)

    return candidates[0] if candidates else None


def _classify_recipient_match(chat_header: str | None, intended_contact: str) -> str:
    """
    Decide whether the chat header looks like the intended recipient.

    Returns one of:
        "match"      - header tokens overlap meaningfully with intended_contact.
        "mismatch"   - header was read but does not look like the contact.
        "unreadable" - header could not be read (None / empty / app chrome).
    """
    normalized_intended = _normalize_text(intended_contact)
    if not normalized_intended:
        return "unreadable"
    if not chat_header:
        return "unreadable"

    normalized_header = _normalize_text(chat_header)
    if not normalized_header or normalized_header in _CHAT_HEADER_NOISE:
        return "unreadable"

    if normalized_intended == normalized_header:
        return "match"

    intended_tokens = {token for token in normalized_intended.split() if len(token) >= 2}
    header_tokens = {token for token in normalized_header.split() if len(token) >= 2}
    if intended_tokens and intended_tokens.issubset(header_tokens):
        return "match"
    if header_tokens and header_tokens.issubset(intended_tokens):
        return "match"

    # Compact contains (handles "alex" vs "alex.jones" or "alexj")
    compact_intended = _compact_text(normalized_intended)
    compact_header = _compact_text(normalized_header)
    if compact_intended and compact_header:
        if compact_intended in compact_header or compact_header in compact_intended:
            return "match"

    return "mismatch"


# Strings that frequently appear as Text elements in WhatsApp / Discord chrome
# and do not represent a contact name. Anything here is treated as "unreadable".
_CHAT_HEADER_NOISE: set[str] = {
    "",
    "whatsapp",
    "discord",
    "chats",
    "calls",
    "status",
    "search",
    "search or start a new chat",
    "type a message",
    "online",
    "typing",
    "menu",
    "settings",
    "profile",
    "new chat",
}
_CHAT_HEADER_NOISE_SUBSTRINGS: tuple[str, ...] = (
    "search or start",
    "type a message",
    "click here for",
    "last seen",
)


def _missing_automation_dependency_result() -> dict[str, Any]:
    return {
        "success": False,
        "message": "Window automation is unavailable because pywinauto is not installed.",
        "data": {},
        "requires_confirmation": False,
    }


def _resolve_message_profile(app_name: str) -> dict[str, Any] | None:
    normalized_name = _normalize_text(app_name)
    if not normalized_name:
        return None
    return _MESSAGE_APP_PROFILES.get(normalized_name)


def _resolve_runtime_window_hint(window_hint: str) -> str:
    normalized_hint = _normalize_text(window_hint)
    if normalized_hint in _CURRENT_WINDOW_ALIASES:
        return get_active_window_title()
    return _normalize_window_hint(window_hint)


def _wait_for_window(window_hint: str, timeout_s: float = _WINDOW_TIMEOUT_S) -> WindowMatch | None:
    deadline = time.time() + max(1.0, timeout_s)
    while time.time() < deadline:
        match = _find_best_window(window_hint)
        if match is not None:
            return match
        time.sleep(_WINDOW_RETRY_DELAY_S)
    return None


def _find_best_window(window_hint: str) -> WindowMatch | None:
    if gw is None:
        return None

    candidates = []
    normalized_hint = _normalize_window_hint(window_hint)
    compact_hint = _compact_text(normalized_hint)
    if not normalized_hint:
        return None

    try:
        windows = gw.getAllWindows()
    except Exception:  # noqa: BLE001
        return None

    for window in windows:
        title = str(getattr(window, "title", "") or "").strip()
        if not title:
            continue
        normalized_title = _normalize_text(title)
        if not normalized_title:
            continue

        score = 0.0
        if normalized_title == normalized_hint:
            score += 2.5
        if normalized_hint in normalized_title:
            score += 1.2
        if compact_hint and compact_hint in _compact_text(normalized_title):
            score += 0.9
        score += _token_overlap_score(normalized_hint, normalized_title)
        if score > 0.8:
            candidates.append(WindowMatch(title=title, handle=window, score=score))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def _activate_window(window_handle: Any, title: str = "") -> None:
    activation_error: Exception | None = None
    if hasattr(window_handle, "isMinimized") and window_handle.isMinimized:
        window_handle.restore()
        time.sleep(0.15)
    if hasattr(window_handle, "activate"):
        try:
            window_handle.activate()
            time.sleep(0.2)
            return
        except Exception as exc:  # noqa: BLE001
            activation_error = exc

    ui_window = _find_ui_window(title) if title else None
    if ui_window is not None:
        try:
            ui_window.set_focus()
            time.sleep(0.15)
            return
        except Exception as exc:  # noqa: BLE001
            activation_error = activation_error or exc

    if activation_error is not None:
        error_text = str(activation_error).lower()
        if "operation completed successfully" in error_text:
            return
        raise activation_error


def _find_ui_window(title: str) -> Any | None:
    if not _HAS_PYWINAUTO:
        return None

    normalized_title = _normalize_text(title)
    windows = Desktop(backend="uia").windows()
    best_window = None
    best_score = 0.0

    for window in windows:
        window_title = str(window.window_text() or "").strip()
        normalized_candidate = _normalize_text(window_title)
        if not normalized_candidate:
            continue
        score = 0.0
        if normalized_candidate == normalized_title:
            score += 2.0
        if normalized_title in normalized_candidate:
            score += 1.0
        score += _token_overlap_score(normalized_title, normalized_candidate)
        if score > best_score:
            best_score = score
            best_window = window

    return best_window if best_score >= 0.8 else None


def _find_button(ui_window: Any, button_text: str) -> Any | None:
    normalized_button_text = _normalize_text(button_text)
    best_button = None
    best_score = 0.0

    for button in ui_window.descendants(control_type="Button"):
        candidate_text = str(button.window_text() or "").strip()
        normalized_candidate = _normalize_text(candidate_text)
        if not normalized_candidate:
            continue
        score = 0.0
        if normalized_candidate == normalized_button_text:
            score += 2.0
        if normalized_button_text in normalized_candidate:
            score += 1.0
        score += _token_overlap_score(normalized_button_text, normalized_candidate)
        if score > best_score:
            best_score = score
            best_button = button

    return best_button if best_score >= 0.8 else None


def _paste_text(text: str) -> None:
    if send_keys is None:
        raise RuntimeError("pywinauto clipboard helpers are unavailable.")

    if _HAS_WIN32_CLIPBOARD:
        previous_text = _get_clipboard_text()
        had_previous_text = previous_text is not None
        _set_clipboard_text(text)
        send_keys("^v")
        time.sleep(0.08)
        if had_previous_text and previous_text is not None:
            _set_clipboard_text(previous_text)
        return

    if clipboard is None:
        raise RuntimeError("Windows clipboard support is unavailable.")

    previous_text = None
    had_previous_text = False
    try:
        previous_text = clipboard.GetData()
        had_previous_text = True
    except Exception:
        previous_text = None

    if hasattr(clipboard, "SetClipboardText"):
        clipboard.SetClipboardText(text)
    elif hasattr(clipboard, "SetData"):
        clipboard.SetData(text)
    else:
        raise RuntimeError("No supported clipboard writer is available.")

    send_keys("^v")
    time.sleep(0.08)

    if had_previous_text and previous_text is not None:
        if hasattr(clipboard, "SetClipboardText"):
            clipboard.SetClipboardText(previous_text)
        elif hasattr(clipboard, "SetData"):
            clipboard.SetData(previous_text)


def _get_clipboard_text() -> str | None:
    if not _HAS_WIN32_CLIPBOARD:
        return None
    try:
        win32clipboard.OpenClipboard()
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def _set_clipboard_text(text: str) -> None:
    if not _HAS_WIN32_CLIPBOARD:
        raise RuntimeError("Windows clipboard support is unavailable.")
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(str(text), win32con.CF_UNICODETEXT)
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def _normalize_window_hint(value: str) -> str:
    normalized = _normalize_text(value)
    return _WINDOW_ALIASES.get(normalized, value).strip()


def _normalize_key_sequence(keys: str) -> str:
    normalized = _normalize_text(keys)
    return _SAFE_KEY_ALIASES.get(normalized, "")


def _normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _compact_text(value: str) -> str:
    return _normalize_text(value).replace(" ", "")


def _token_overlap_score(left: str, right: str) -> float:
    left_tokens = set(_normalize_text(left).split())
    right_tokens = set(_normalize_text(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return overlap * 0.25


__all__ = [
    "app_search",
    "app_send_message",
    "click_button",
    "focus_window",
    "get_active_window_title",
    "press_keys",
    "type_text",
]
