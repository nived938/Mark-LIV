import os
import time
from pathlib import Path
from urllib.parse import quote

try:
    import pyautogui
except Exception:
    pyautogui = None

try:
    import pyperclip
except Exception:
    pyperclip = None

try:
    from pywinauto import Desktop
except Exception:
    Desktop = None


def _clean_phone(phone):
    return "".join(c for c in (phone or "") if c.isdigit())


def _open_desktop():
    try:
        os.startfile("whatsapp:")
        return True
    except Exception:
        return False


def _find_whatsapp_window(timeout=12.0):
    if not Desktop:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for win in Desktop(backend="uia").windows():
                try:
                    title = (win.window_text() or "").strip().lower()
                    cls = (getattr(win, "class_name", lambda: "")() or "").lower()
                    if "whatsapp" in title or "whatsapp" in cls:
                        return win
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)
    return None


def _focus_whatsapp(timeout=10.0):
    win = _find_whatsapp_window(timeout)
    if not win:
        return None
    try:
        win.restore()
    except Exception:
        pass
    try:
        win.set_focus()
    except Exception:
        pass
    time.sleep(0.3)
    return win


def _get_edits(win):
    try:
        return win.descendants(control_type="Edit")
    except Exception:
        return []


def _set_edit_text(control, text):
    try:
        control.click_input()
        time.sleep(0.15)
        control.set_edit_text(text)
        return True
    except Exception:
        return False


def _click_search_and_find(contact):
    win = _focus_whatsapp(8)
    if not win:
        return False, "WhatsApp desktop window did not appear."

    for _ in range(8):
        try:
            edits = _get_edits(win)
            search = None
            for edit in edits:
                try:
                    text = (edit.window_text() or "").lower()
                    aid = (getattr(edit, "automation_id", lambda: "")() or "").lower()
                    if "search" in text or "search" in aid:
                        search = edit
                        break
                except Exception:
                    pass
            if search is None and edits:
                search = edits[0]
            if search and _set_edit_text(search, contact):
                time.sleep(1.5)
                if pyautogui:
                    pyautogui.press("down")
                    pyautogui.press("enter")
                time.sleep(1.8)
                return True, ""
        except Exception:
            pass
        time.sleep(0.6)

    if pyautogui:
        try:
            win.set_focus()
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.5)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.write(contact, interval=0.04)
            time.sleep(1.5)
            pyautogui.press("down")
            pyautogui.press("enter")
            time.sleep(1.8)
            return True, ""
        except Exception as e:
            return False, str(e)
    return False, "Could not access the WhatsApp search box."


def _focus_message_box(win):
    """Focus the actual chat composer, not the search field."""
    if not pyautogui:
        return False

    # First try UI Automation controls whose accessible name identifies the composer.
    try:
        edits = _get_edits(win)
        for edit in reversed(edits):
            try:
                text = (edit.window_text() or "").lower()
                aid = (getattr(edit, "automation_id", lambda: "")() or "").lower()
                combined = text + " " + aid
                if any(x in combined for x in ("type a message", "message", "compose", "write a message")):
                    edit.click_input()
                    time.sleep(0.2)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # The current chat composer is at the bottom of the WhatsApp window.
    # Clicking its center avoids accidentally typing into the contact search box.
    try:
        rect = win.rectangle()
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width > 500 and height > 400:
            x = rect.left + int(width * 0.70)
            y = rect.bottom - max(65, int(height * 0.075))
            pyautogui.click(x, y)
            time.sleep(0.3)
            return True
    except Exception:
        pass

    return False


def _type_and_send_message(win, message):
    """Use clipboard paste because it works reliably with WhatsApp's contenteditable composer."""
    if not pyautogui:
        return False, "pyautogui is not installed."

    if not _focus_message_box(win):
        return False, "Could not focus the WhatsApp message box."

    try:
        if pyperclip:
            old_clipboard = None
            try:
                old_clipboard = pyperclip.paste()
            except Exception:
                pass
            pyperclip.copy(message)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.25)
            pyautogui.press("enter")
            time.sleep(1.2)
            if old_clipboard is not None:
                try:
                    pyperclip.copy(old_clipboard)
                except Exception:
                    pass
            return True, ""

        pyautogui.write(message, interval=0.03)
        pyautogui.press("enter")
        time.sleep(1.2)
        return True, ""
    except Exception as e:
        return False, str(e)


def _send_message_desktop(contact, message):
    if not pyautogui:
        return False, "pyautogui is not installed."
    if not _open_desktop():
        return False, "Could not launch the installed WhatsApp desktop app."

    win = _focus_whatsapp(12)
    if not win:
        return False, "WhatsApp opened, but its desktop window was not detected."

    ok, error = _click_search_and_find(contact)
    if not ok:
        return False, error

    win = _focus_whatsapp(5)
    if not win:
        return False, "WhatsApp chat opened, but its window disappeared."

    ok, error = _type_and_send_message(win, message)
    if not ok:
        return False, error
    return True, ""


def _send_by_phone(phone, message):
    if not pyautogui:
        return False, "pyautogui is not installed."
    number = _clean_phone(phone)
    if not number:
        return False, "Invalid phone number."
    try:
        uri = "whatsapp://send?phone=" + number + "&text=" + quote(message)
        os.startfile(uri)
        time.sleep(3)
        pyautogui.press("enter")
        time.sleep(0.7)
        return True, ""
    except Exception as e:
        return False, str(e)


def _click_call_button(kind):
    win = _focus_whatsapp(8)
    if not win:
        return False, "WhatsApp window was not found."
    wanted = {"video", "video call", "videocall", "start video call"} if kind == "video" else {"voice call", "audio call", "start voice call", "start audio call"}
    try:
        for control in win.descendants():
            try:
                name = (control.window_text() or "").strip().lower()
                aid = (getattr(control, "automation_id", lambda: "")() or "").strip().lower()
                if name in wanted or aid in wanted:
                    try:
                        control.invoke()
                        time.sleep(2)
                        return True, ""
                    except Exception:
                        try:
                            control.click_input()
                            time.sleep(2)
                            return True, ""
                        except Exception:
                            pass
            except Exception:
                continue
    except Exception:
        pass
    return False, f"WhatsApp exposed no exact {kind} call control to Windows UI Automation."


def whatsapp_advance(parameters=None, **_ctx):
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()
    contact = str(params.get("contact") or "")
    phone = str(params.get("phone") or "")
    message = str(params.get("message") or "")
    confirmation = str(params.get("confirmation") or "")

    # Incoming-call controls use the dedicated redundant detector. The agent
    # focuses the WhatsApp call surface and uses keyboard-only navigation:
    # Accept = Tab x3 + Enter, Decline = Tab x4 + Enter. Never use the mouse.

    if action in ("accept_incoming", "answer_incoming"):
        from actions.whatsapp_incoming_agent import get_incoming_agent
        agent = get_incoming_agent()
        if not (agent._thread and agent._thread.is_alive()):
            agent.start()
        caller = agent.pending.caller if agent.pending else (contact or "the caller")
        ok, error = agent.accept()
        if not ok:
            return f"Could not accept the incoming WhatsApp call from {caller}: {error}"
        if message:
            time.sleep(1.0)
            sent, send_error = _send_message_desktop(caller, message)
            if not sent:
                return f"Accepted the WhatsApp call from {caller}, but I could not send the message: {send_error}"
            return f"Accepted the WhatsApp call from {caller} and sent the message."
        return f"Accepted the WhatsApp call from {caller}."

    if action in ("decline_incoming", "reject_incoming"):
        from actions.whatsapp_incoming_agent import get_incoming_agent
        agent = get_incoming_agent()
        caller = agent.pending.caller if agent.pending else (contact or "the caller")
        ok, error = agent.decline()
        if not ok:
            return f"Could not decline the incoming WhatsApp call from {caller}: {error}"
        if message:
            sent, send_error = _send_message_desktop(caller, message)
            if not sent:
                return f"Declined the WhatsApp call from {caller}, but I could not send the follow-up message: {send_error}"
            return f"Declined the WhatsApp call from {caller} and sent the follow-up message."
        return f"Declined the WhatsApp call from {caller}."

    if action in ("call_and_message", "message_then_call"):
        target = contact or phone
        if not target:
            return "A WhatsApp contact name or phone number is required."
        if not message:
            return "The message to send is required."
        # Sending the message before starting the call is the reliable desktop
        # flow: once the native call window takes focus, the chat composer may
        # no longer be reachable. The call still follows immediately.
        if phone:
            sent, send_error = _send_by_phone(phone, message)
        else:
            sent, send_error = _send_message_desktop(contact, message)
        if not sent:
            return f"Could not send the message to {target}: {send_error}"
        if not _open_desktop():
            return f"Sent the message to {target}, but could not open WhatsApp for the call."
        win = _focus_whatsapp(10)
        if not win:
            return f"Sent the message to {target}, but the WhatsApp window was not detected for the call."
        if contact:
            ok, error = _click_search_and_find(contact)
            if not ok:
                return f"Sent the message to {target}, but could not open the chat for the call: {error}"
        kind = "voice"
        ok, error = _click_call_button(kind)
        if not ok:
            return f"Sent the message to {target}, but could not trigger the WhatsApp call: {error}"
        return f"Sent the message to {target} and started the WhatsApp voice call."

    if action in ("open", "open_whatsapp"):
        return "Opened the WhatsApp desktop app." if _open_desktop() else "Could not open the WhatsApp desktop app."

    if action in ("prepare_message", "message", "send", "send_message", "send_confirmed", "confirm_and_send"):
        if not contact and not phone:
            return "A WhatsApp contact name or international phone number is required."
        if not message:
            return "The message text is required."
        if phone:
            ok, error = _send_by_phone(phone, message)
        else:
            ok, error = _send_message_desktop(contact, message)
        if not ok:
            return f"WhatsApp send failed: {error}"
        return f"Sent the WhatsApp message to {contact or phone}."

    if action in ("call", "voice_call", "video_call"):
        target = contact or phone
        if not target:
            return "A WhatsApp contact name or phone number is required."
        if not _open_desktop():
            return "Could not open the WhatsApp desktop app."
        win = _focus_whatsapp(10)
        if not win:
            return "WhatsApp opened, but its Windows app window was not detected."
        if contact:
            ok, error = _click_search_and_find(contact)
            if not ok:
                return f"Could not open the WhatsApp chat for {target}: {error}"
        else:
            try:
                os.startfile("whatsapp://send?phone=" + _clean_phone(phone))
                time.sleep(3)
            except Exception as e:
                return f"Could not open the WhatsApp contact: {e}"
        kind = "video" if action == "video_call" else "voice"
        ok, error = _click_call_button(kind)
        if ok:
            return f"Triggered the WhatsApp {kind} call control for {target}."
        return f"Opened WhatsApp to {target}, but I could not trigger the {kind} call control. {error}"

    return "Unknown action. Use open_whatsapp, message, send, call, or video_call."


TOOL = {
    "name": "whatsapp_advance",
    "description": (
        "WINDOWS WHATSAPP DESKTOP ONLY. This is the default WhatsApp tool for untagged commands "
        "about WhatsApp on this PC. Use it for 'message achan in whatsapp', 'call achan in "
        "whatsapp', and similar commands unless the user explicitly says @phone/Android phone. "
        "Never use phone_advance for those normal PC WhatsApp commands. Never use WhatsApp Web. "
        "For messages, find the contact, open the chat, focus the actual message composer, "
        "paste the message, and press Enter automatically. Do not ask for confirmation. "
        "Do not report success unless the message input was focused and Enter was pressed. "
        "Calls and video calls should use Windows UI Automation automatically. Incoming calls are detected in the native Windows WhatsApp call dialog; accept_incoming and decline_incoming control the pending call. For decline_incoming, include message for a follow-up text. For accept_incoming, include message to send after answering. call_and_message sends the requested text and then starts the voice call because the native call window can take focus."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "open_whatsapp, message, send, call, video_call, accept_incoming, decline_incoming, or call_and_message"},
            "contact": {"type": "STRING", "description": "WhatsApp contact name"},
            "phone": {"type": "STRING", "description": "International phone number"},
            "message": {"type": "STRING", "description": "Message text"},
            "confirmation": {"type": "STRING", "description": "Legacy field, not required"},
        },
        "required": ["action"],
    },
    "handler": whatsapp_advance,
}
