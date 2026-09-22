"""Visual WhatsApp incoming-call auto-reply action.

The rule is armed by a normal JARVIS command such as:
    "If anyone calls me, tell them I am not available right now."

While armed, a daemon thread watches the Windows desktop. It uses Gemini vision
to identify a WhatsApp incoming-call surface, the Answer button and the End
button. It only clicks when the vision result explicitly says the call is an
incoming WhatsApp call with usable coordinates and reasonable confidence.

The rule is process-local: it stays active until disabled or JARVIS exits.
"""
from __future__ import annotations

import io
import json
import platform
import re
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops
import pyautogui
import pygetwindow


_DEFAULT_MESSAGE = "Nived is not available right now."
_POLL_SECONDS = 0.75
_VISION_COOLDOWN = 1.75
_MIN_CHANGE = 2.5
_CONFIDENCE = 0.78


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


_CONFIG = _base_dir() / "config" / "api_keys.json"


def _log(player, message: str) -> None:
    try:
        player.write_log(message)
    except Exception:
        print(f"[WhatsAppCall] {message}")


def _has_whatsapp_window() -> bool:
    try:
        for win in pygetwindow.getAllWindows():
            title = str(getattr(win, "title", "") or "").lower()
            if "whatsapp" in title and int(getattr(win, "width", 0) or 0) > 150:
                return True
    except Exception:
        # pygetwindow can be incomplete on some Windows desktop states. The
        # visual path is still safe because it independently requires "WhatsApp"
        # in the model's classification.
        return True
    return False


def _capture_screen() -> tuple[bytes, float, float, Image.Image]:
    screen = pyautogui.screenshot().convert("RGB")
    full_w, full_h = screen.size

    max_w, max_h = 1280, 720
    scale = min(1.0, max_w / max(full_w, 1), max_h / max(full_h, 1))
    if scale < 1.0:
        img = screen.resize(
            (max(1, round(full_w * scale)), max(1, round(full_h * scale))),
            Image.Resampling.LANCZOS,
        )
    else:
        img = screen

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=False)
    return buf.getvalue(), full_w / img.width, full_h / img.height, img


def _changed(previous: Image.Image | None, current: Image.Image) -> bool:
    if previous is None:
        return True
    a = previous.resize((64, 36), Image.Resampling.BILINEAR).convert("L")
    b = current.resize((64, 36), Image.Resampling.BILINEAR).convert("L")
    diff = ImageChops.difference(a, b)
    # Mean pixel difference. Call notifications and their buttons change enough
    # to exceed this, while tiny cursor motion generally does not.
    mean = sum(diff.getdata()) / (64 * 36)
    return mean >= _MIN_CHANGE


def _vision(image_bytes: bytes) -> dict | None:
    from google import genai  # noqa: F401
    from google.genai import types as gtypes
    from core import gemini

    prompt = """
Inspect this desktop screenshot visually.

You are looking ONLY for an actual WhatsApp Desktop incoming voice or video
call. Ignore normal WhatsApp chats, notifications, banners, contact previews,
and outgoing calls.

Return ONLY JSON with this schema:
{
  "is_whatsapp": true|false,
  "state": "incoming"|"connected"|"none",
  "confidence": 0.0,
  "answer_x": integer|null,
  "answer_y": integer|null,
  "end_x": integer|null,
  "end_y": integer|null
}

Rules:
- "incoming" means a call is actively ringing and an Answer/Accept control is
  visibly available.
- "connected" means the WhatsApp call is already connected and an End/Hang up
  control is visibly available.
- "none" means neither condition is clearly present.
- Coordinates are pixel coordinates in THIS screenshot, measured from the
  top-left corner.
- Only provide answer_x/answer_y for a clearly visible Answer/Accept button.
- Only provide end_x/end_y for a clearly visible End/Hang up button.
- If anything is uncertain, use null coordinates and state "none".
- Do not click anything. Do not describe the screenshot. JSON only.
""".strip()

    try:
        result = gemini.as_json(
            [
                gtypes.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt,
            ],
            tier=gemini.FAST,
            timeout_ms=10_000,
            default=None,
        )
        return result if isinstance(result, dict) else None
    except Exception as exc:
        print(f"[WhatsAppCall] Vision error: {exc}")
        return None


def _coord(value) -> int | None:
    try:
        n = int(float(value))
        return n if n >= 0 else None
    except Exception:
        return None


class WhatsAppCallWatcher:
    def __init__(self, player, speak):
        self.player = player
        self.speak = speak
        self.message = _DEFAULT_MESSAGE
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._owned_call = False
        self._message_sent = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, message: str = _DEFAULT_MESSAGE) -> str:
        with self._lock:
            self.message = " ".join(str(message or "").split()).strip() or _DEFAULT_MESSAGE
            if self.running:
                return f"Already monitoring WhatsApp calls. Message: {self.message}"
            self._stop.clear()
            self._owned_call = False
            self._message_sent = False
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="whatsapp-call-watcher",
            )
            self._thread.start()
        return f"WhatsApp auto-reply armed. Message: {self.message}"

    def stop(self) -> str:
        self._stop.set()
        return "WhatsApp auto-reply disabled."

    def _run(self) -> None:
        try:
            if platform.system() != "Windows":
                _log(self.player, "[WhatsAppCall] Windows desktop automation is required.")
                return

            previous_image = None
            last_vision = 0.0
            last_state = "none"

            while not self._stop.wait(_POLL_SECONDS):
                if not _has_whatsapp_window():
                    if not self._owned_call:
                        last_state = "none"
                    continue

                try:
                    image_bytes, sx, sy, image = _capture_screen()
                except Exception as exc:
                    _log(self.player, f"[WhatsAppCall] Screen capture failed: {exc}")
                    continue

                now = time.monotonic()
                changed = _changed(previous_image, image)
                previous_image = image

                if not changed and now - last_vision < _VISION_COOLDOWN:
                    continue
                if now - last_vision < _VISION_COOLDOWN:
                    continue

                result = _vision(image_bytes)
                last_vision = now
                if not result or not result.get("is_whatsapp"):
                    continue

                state = str(result.get("state") or "none").lower()
                confidence = float(result.get("confidence") or 0.0)
                if confidence < _CONFIDENCE:
                    state = "none"

                answer_x = _coord(result.get("answer_x"))
                answer_y = _coord(result.get("answer_y"))
                end_x = _coord(result.get("end_x"))
                end_y = _coord(result.get("end_y"))

                if state == "incoming" and not self._owned_call:
                    if answer_x is None or answer_y is None:
                        continue

                    _log(self.player, "[WhatsAppCall] Incoming call identified visually. Answering.")
                    pyautogui.click(round(answer_x * sx), round(answer_y * sy))
                    self._owned_call = True
                    self._message_sent = False
                    time.sleep(1.25)
                    continue

                if self._owned_call and state == "connected" and not self._message_sent:
                    self._message_sent = True

                    try:
                        from core import call_audio
                        mirrored, route = call_audio.start()
                        if mirrored:
                            _log(self.player, f"[WhatsAppCall] Call audio mirror active: {route}")
                        else:
                            _log(
                                self.player,
                                "[WhatsAppCall] No dedicated call-audio route found; "
                                "JARVIS will use the normal speaker path.",
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
                        _log(self.player, f"[WhatsAppCall] Could not start the spoken reply: {exc}")

                    # The sentence is short. Give the audio path time to finish
                    # before attempting to hang up.
                    time.sleep(4.0)

                    # Refresh the call UI once, because the End button can move
                    # after the call transitions from ringing to connected.
                    try:
                        image_bytes2, sx2, sy2, image2 = _capture_screen()
                        result2 = _vision(image_bytes2) or {}
                    except Exception:
                        result2 = {}

                    if (
                        str(result2.get("state") or "").lower() == "connected"
                        and bool(result2.get("is_whatsapp"))
                    ):
                        ex = _coord(result2.get("end_x"))
                        ey = _coord(result2.get("end_y"))
                        if ex is not None and ey is not None:
                            _log(self.player, "[WhatsAppCall] Reply finished. Ending the call.")
                            pyautogui.click(round(ex * sx2), round(ey * sy2))
                        else:
                            _log(self.player, "[WhatsAppCall] Reply finished, but no End button was visible.")
                    else:
                        _log(self.player, "[WhatsAppCall] Call already ended.")

                    try:
                        from core import call_audio
                        call_audio.stop()
                    except Exception:
                        pass

                    self._owned_call = False
                    self._message_sent = False
                    last_state = "none"
                    continue

                if self._owned_call and state == "none":
                    # The other side may have ended the call. Do not click anything
                    # once the connected UI disappears.
                    self._owned_call = False
                    self._message_sent = False
                    try:
                        from core import call_audio
                        call_audio.stop()
                    except Exception:
                        pass

                last_state = state

        except Exception as exc:
            _log(self.player, f"[WhatsAppCall] Watcher stopped unexpectedly: {exc}")


_WATCHER: WhatsAppCallWatcher | None = None
_WATCHER_LOCK = threading.Lock()


def whatsapp_call_rule(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "enable").strip().lower()
    message = " ".join(str(params.get("message") or _DEFAULT_MESSAGE).split()).strip()

    global _WATCHER

    with _WATCHER_LOCK:
        if action in {"disable", "stop", "off"}:
            if _WATCHER is None:
                return "WhatsApp auto-reply is already disabled."
            result = _WATCHER.stop()
            _WATCHER = None
            return result

        if action in {"status", "check"}:
            if _WATCHER is not None and _WATCHER.running:
                return f"WhatsApp auto-reply is active. Message: {_WATCHER.message}"
            return "WhatsApp auto-reply is disabled."

        if player is None or speak is None:
            return "WhatsApp auto-reply requires the live JARVIS session."

        _WATCHER = WhatsAppCallWatcher(player, speak)
        return _WATCHER.start(message)


TOOL = {
    "name": "whatsapp_call_rule",
    "description": (
        "Controls a background WhatsApp incoming-call auto-reply rule on Windows. "
        "Use it when the user says things like 'If anyone calls me, tell them I "
        "am not available right now', 'automatically answer WhatsApp calls and "
        "tell them I am busy', or 'stop automatic WhatsApp call answering'. "
        "While enabled, JARVIS visually inspects the desktop for a real incoming "
        "WhatsApp voice/video call, identifies the Answer button, answers it, "
        "speaks the configured message, then identifies and clicks the End call "
        "button. It is process-local and remains active until disabled or JARVIS "
        "exits. Do not use this for normal WhatsApp messages."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "enable (default), disable, or status",
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
