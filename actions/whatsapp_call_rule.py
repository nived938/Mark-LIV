"""WhatsApp incoming-call automation built around the Advance incoming-call agent.

Detection lives in actions.whatsapp_incoming_agent.py. This module only adds
the optional "answer, speak, and end" rule used by commands such as:
    "If anyone calls me tell them I am not available right now."

Incoming Accept/Decline control is keyboard-only:
    Accept = focus WhatsApp call window -> Tab x3 -> Enter
    Decline = focus WhatsApp call window -> Tab x4 -> Enter
No mouse click is used for those controls.
"""

from __future__ import annotations

import platform
import threading
import time

_DEFAULT_MESSAGE = "Nived is not available right now."

_WATCHER = None
_WATCHER_LOCK = threading.Lock()


def auto_reply_active() -> bool:
    """Return whether the explicit WhatsApp auto-reply rule is currently armed."""
    return _WATCHER is not None


def _log(player, message: str) -> None:
    try:
        player.write_log(message)
    except Exception:
        print(f"[WhatsAppCall] {message}")


def _end_whatsapp_call(player=None) -> tuple[bool, str]:
    """Find and invoke the native WhatsApp End/Hang-up button without mouse."""
    if platform.system() != "Windows":
        return False, "WhatsApp call control requires Windows."

    try:
        from pywinauto import Desktop
    except Exception as exc:
        return False, f"pywinauto unavailable: {exc}"

    end_words = (
        "end", "end call", "hang up", "hangup",
        "disconnect", "leave call",
    )

    try:
        import psutil

        for window in Desktop(backend="uia").windows(
            visible_only=True,
            enabled_only=True,
        ):
            try:
                title = str(window.window_text() or "").lower()
                pid = int(window.element_info.process_id)
                process = psutil.Process(pid)
                process_blob = " ".join(
                    [process.name(), process.exe() or "", " ".join(process.cmdline())]
                ).lower()
                if "whatsapp" not in f"{title} {process_blob}":
                    continue

                for button in window.descendants(control_type="Button"):
                    try:
                        if not button.is_visible() or not button.is_enabled():
                            continue

                        values = [
                            str(button.window_text() or "").strip().lower(),
                            str(button.element_info.name or "").strip().lower(),
                            str(button.element_info.automation_id or "").strip().lower(),
                            str(button.element_info.class_name or "").strip().lower(),
                        ]
                        blob = " ".join(v for v in values if v)
                        if not any(word in blob for word in end_words):
                            continue

                        try:
                            window.set_focus()
                        except Exception:
                            pass

                        try:
                            button.invoke()
                        except Exception:
                            iface = getattr(button, "iface_invoke", None)
                            if iface is None:
                                continue
                            iface.Invoke()

                        name = str(
                            button.window_text()
                            or button.element_info.name
                            or "End"
                        )
                        msg = (
                            f"Invoked WhatsApp End/Hang-up button '{name}' "
                            f"without mouse input."
                        )
                        _log(player, f"[WhatsAppCall] {msg}")
                        return True, msg
                    except Exception:
                        continue
            except Exception:
                continue

        return False, "No accessible WhatsApp End/Hang-up button was found."
    except Exception as exc:
        return False, f"WhatsApp End-call UI scan failed: {exc}"


class _AutoReply:
    def __init__(self, player, speak, message: str):
        self.player = player
        self.speak = speak
        self.message = message or _DEFAULT_MESSAGE

    def on_incoming(self, call) -> None:
        """Called by WhatsAppIncomingAgent from its detector thread."""
        thread = threading.Thread(
            target=self._handle,
            args=(call,),
            daemon=True,
            name="whatsapp-auto-reply",
        )
        thread.start()

    def _handle(self, call) -> None:
        caller = getattr(call, "caller", "someone") or "someone"

        from actions.whatsapp_incoming_agent import get_incoming_agent
        agent = get_incoming_agent()

        ok, error = agent.accept()
        if not ok:
            _log(
                self.player,
                f"[WhatsAppCall] Could not accept incoming call from "
                f"{caller}: {error}",
            )
            return

        _log(
            self.player,
            f"[WhatsAppCall] Accepted incoming call from {caller} using "
            f"keyboard navigation: Tab x3 + Enter.",
        )

        try:
            from core import call_audio
            mirrored, route = call_audio.start()
            if mirrored:
                _log(
                    self.player,
                    f"[WhatsAppCall] Call audio mirror active: {route}",
                )
            else:
                _log(
                    self.player,
                    "[WhatsAppCall] No virtual call-audio route found.",
                )
        except Exception as exc:
            _log(self.player, f"[WhatsAppCall] Audio mirror unavailable: {exc}")

        instruction = (
            "You are speaking to someone on a connected WhatsApp call. "
            "Say exactly this sentence and nothing else: "
            f'"{self.message}". '
            "Do not add a greeting, acknowledgement, explanation, or sign-off."
        )

        try:
            self.speak(instruction)
        except Exception as exc:
            _log(
                self.player,
                f"[WhatsAppCall] Could not start the spoken reply: {exc}",
            )

        time.sleep(4.5)

        ok_end, end_details = _end_whatsapp_call(self.player)
        if not ok_end:
            _log(
                self.player,
                f"[WhatsAppCall] Could not end the call automatically: "
                f"{end_details}",
            )

        try:
            from core import call_audio
            call_audio.stop()
        except Exception:
            pass


def whatsapp_call_rule(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "enable").strip().lower()
    message = " ".join(
        str(params.get("message") or _DEFAULT_MESSAGE).split()
    ).strip()

    global _WATCHER

    from actions.whatsapp_incoming_agent import get_incoming_agent

    if action in {"answer", "accept"}:
        agent = get_incoming_agent()
        if not (agent._thread and agent._thread.is_alive()):
            agent.start()
        ok, error = agent.accept()
        if ok:
            return "Accepted the incoming WhatsApp call with Tab x3 + Enter."
        return f"Could not accept the incoming WhatsApp call: {error}"

    if action in {"decline", "reject"}:
        agent = get_incoming_agent()
        if not (agent._thread and agent._thread.is_alive()):
            agent.start()
        ok, error = agent.decline()
        if ok:
            return "Declined the incoming WhatsApp call with Tab x4 + Enter."
        return f"Could not decline the incoming WhatsApp call: {error}"

    with _WATCHER_LOCK:
        if action in {"disable", "stop", "off"}:
            agent = get_incoming_agent()
            if _WATCHER is not None:
                try:
                    agent.remove_callback(_WATCHER.on_incoming)
                except Exception:
                    pass
                _WATCHER = None
            # The incoming-call agent is shared with whatsapp_advance and the
            # main app, so disabling only removes the auto-answer callback.
            # Detection stays alive for normal accept/decline commands.
            try:
                from core import call_audio
                call_audio.stop()
            except Exception:
                pass
            return "WhatsApp auto-reply disabled."

        if action in {"status", "check"}:
            running = (
                _WATCHER is not None
                and get_incoming_agent()._thread is not None
                and get_incoming_agent()._thread.is_alive()
            )
            if running:
                current = getattr(_WATCHER, "message", "") or message or _DEFAULT_MESSAGE
                return f"WhatsApp auto-reply is active. Message: {current}"
            return "WhatsApp auto-reply is disabled."

        if player is None or speak is None:
            return "WhatsApp auto-reply requires the live JARVIS session."

        auto = _AutoReply(player, speak, message)
        _WATCHER = auto

        agent = get_incoming_agent()
        agent.add_callback(auto.on_incoming)
        agent.start()

        return (
            "WhatsApp auto-reply armed. Incoming calls are detected by the "
            "dedicated WhatsApp incoming agent. Accept uses Tab x3 + Enter."
        )


TOOL = {
    "name": "whatsapp_call_rule",
    "description": (
        "Controls an automatic WhatsApp Desktop incoming-call rule on Windows. "
        "It uses the dedicated whatsapp_incoming_agent with notification, "
        "visual-color, UI Automation and Win32 detection. When enabled, an "
        "incoming call is focused and answered with Tab x3 + Enter, JARVIS "
        "speaks the configured message, then ends the connected call. "
        "For manual control use action answer or decline: Answer = Tab x3 + Enter; "
        "Decline = Tab x4 + Enter. No mouse is used for Accept/Decline."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "enable, disable, status, answer, or decline",
            },
            "message": {
                "type": "STRING",
                "description": "Exact sentence JARVIS should say to the caller",
            },
        },
        "required": [],
    },
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
    "handler": whatsapp_call_rule,
}
