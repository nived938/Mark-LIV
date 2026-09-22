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

import ctypes
import ctypes
import platform
import threading
import time

from PIL import Image, ImageChops
import pyautogui
import mss


_DEFAULT_MESSAGE = "Nived is not available right now."
_POLL_SECONDS = 0.65
_VISION_COOLDOWN = 1.0
_MIN_CHANGE = 0.7
_CONFIDENCE = 0.70
_DPI_AWARENESS_CONTEXT_PER_MONITOR_V2 = -4


def _log(player, message: str) -> None:
    try:
        player.write_log(message)
    except Exception:
        print(f"[WhatsAppCall] {message}")


def _make_dpi_aware() -> None:
    """Make the watcher thread use the same physical coordinate space as mss."""
    if platform.system() != "Windows":
        return
    try:
        user32 = ctypes.windll.user32
        fn = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if fn is not None:
            fn(ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_V2))
            return
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _click_screen(x: int, y: int) -> None:
    """Move to a visually detected target before clicking it."""
    pyautogui.moveTo(int(x), int(y), duration=0.08)
    time.sleep(0.06)
    pyautogui.click(int(x), int(y))


def _capture_screen() -> tuple[bytes, float, float, int, int, Image.Image]:
    """Capture the complete Windows virtual desktop, including all monitors."""
    with mss.mss() as sct:
        desktop = sct.monitors[0]  # [0] is the bounding rectangle of all monitors.
        shot = sct.grab(desktop)

    full_w, full_h = shot.width, shot.height
    origin_x, origin_y = int(desktop["left"]), int(desktop["top"])

    # Keep buttons and text readable while avoiding an unnecessarily huge image.
    max_w, max_h = 1920, 1080
    scale = min(1.0, max_w / max(full_w, 1), max_h / max(full_h, 1))

    if scale < 1.0:
        img = Image.frombytes("RGB", (full_w, full_h), shot.rgb)
        img = img.resize(
            (max(1, round(full_w * scale)), max(1, round(full_h * scale))),
            Image.Resampling.LANCZOS,
        )
    else:
        img = Image.frombytes("RGB", (full_w, full_h), shot.rgb)

    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=False)

    # sx/sy map model coordinates back to real Windows virtual-desktop pixels.
    return (
        buf.getvalue(),
        full_w / img.width,
        full_h / img.height,
        origin_x,
        origin_y,
        img,
    )


def _changed(previous: Image.Image | None, current: Image.Image) -> bool:
    """Return True when enough of the desktop changed to justify a Gemini vision call."""
    if previous is None:
        return True

    a = previous.resize((64, 36), Image.Resampling.BILINEAR).convert("L")
    b = current.resize((64, 36), Image.Resampling.BILINEAR).convert("L")
    diff = ImageChops.difference(a, b)

    # A small incoming-call popup occupies enough of the desktop to cross this
    # threshold, while ordinary cursor motion and tiny UI animation normally do
    # not. The detector still sees the complete desktop when it does run.
    mean = sum(diff.getdata()) / (64 * 36)
    return mean >= _MIN_CHANGE


def _vision(image_bytes: bytes) -> dict | None:
    from google.genai import types as gtypes
    from core import gemini

    prompt = r"""
You are a visual UI detector for a desktop automation program.

Inspect the ENTIRE Windows desktop screenshot and identify an active WhatsApp
Desktop incoming voice/video call or an already-connected WhatsApp call.

The call can be the main WhatsApp window, a separate call window, floating
popup, or notification-style call card. Do not require the word WhatsApp to be
visible if the visual layout is clearly a WhatsApp call.

Ignore chats, messages, settings, contact cards, ordinary Windows notifications,
and outgoing calls.

Return ONLY JSON:
{
  "state": "incoming" | "connected" | "none",
  "confidence": 0.0,

  "answer_box": [x1,y1,x2,y2] | null,
  "decline_box": [x1,y1,x2,y2] | null,
  "end_box": [x1,y1,x2,y2] | null,

  "answer_x": integer | null,
  "answer_y": integer | null,
  "decline_x": integer | null,
  "decline_y": integer | null,
  "end_x": integer | null,
  "end_y": integer | null
}

Coordinates are PIXELS IN THIS EXACT SCREENSHOT. (0,0) is the screenshot's
top-left corner.

For an incoming call:
- answer_box is the complete green Answer/Accept button.
- decline_box is the complete red Decline/Reject button.
- answer_x/y and decline_x/y are the centers of those boxes.

For a connected call:
- end_box is the complete red End/Hang Up button.
- end_x/y is its center.

Never guess coordinates. Use null when a button is not clearly visible.
Do not return markdown or explanations.
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


def _box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = [int(float(v)) for v in value]
    except Exception:
        return None
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _center(box):
    if not box:
        return None
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


def _valid_box(box, width: int, height: int):
    if not box:
        return None
    x1, y1, x2, y2 = box
    if x1 >= width or y1 >= height or x2 <= 0 or y2 <= 0:
        return None
    return (
        max(0, min(width - 1, x1)),
        max(0, min(height - 1, y1)),
        max(1, min(width, x2)),
        max(1, min(height, y2)),
    )


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
        try:
            from core import call_audio
            call_audio.stop()
        except Exception:
            pass
        return "WhatsApp auto-reply disabled."

    def _run(self) -> None:
        try:
            if platform.system() != "Windows":
                _log(self.player, "[WhatsAppCall] Windows desktop automation is required.")
                return

            _make_dpi_aware()
            last_vision = 0.0
            previous_image = None

            while not self._stop.wait(_POLL_SECONDS):
                try:
                    image_bytes, sx, sy, origin_x, origin_y, image = _capture_screen()
                except Exception as exc:
                    _log(self.player, f"[WhatsAppCall] Screen capture failed: {exc}")
                    continue

                now = time.monotonic()
                changed = _changed(previous_image, image)
                previous_image = image

                # This is intentionally visual-only. There is no window-title
                # gate, WhatsApp-process check, OCR shortcut, or fixed coordinate.
                # Gemini receives the real desktop image whenever the desktop
                # changes enough to warrant a visual inspection.
                if not changed:
                    continue
                if now - last_vision < _VISION_COOLDOWN:
                    continue

                result = _vision(image_bytes)
                last_vision = now

                if not result:
                    _log(self.player, "[WhatsAppCall] Visual scan returned no result.")
                    continue

                _log(
                    self.player,
                    "[WhatsAppCall] Vision scan: "
                    f"state={result.get('state')} confidence={result.get('confidence')}"
                )

                state = str(result.get("state") or "none").strip().lower()
                try:
                    confidence = float(result.get("confidence") or 0.0)
                except Exception:
                    confidence = 0.0

                if confidence < _CONFIDENCE:
                    state = "none"

                answer_box = _valid_box(_box(result.get("answer_box")), image.width, image.height)
                decline_box = _valid_box(_box(result.get("decline_box")), image.width, image.height)
                end_box = _valid_box(_box(result.get("end_box")), image.width, image.height)

                answer_x = _coord(result.get("answer_x"))
                answer_y = _coord(result.get("answer_y"))
                decline_x = _coord(result.get("decline_x"))
                decline_y = _coord(result.get("decline_y"))
                end_x = _coord(result.get("end_x"))
                end_y = _coord(result.get("end_y"))

                answer_center = _center(answer_box) or (
                    (answer_x, answer_y)
                    if answer_x is not None and answer_y is not None
                    else None
                )
                decline_center = _center(decline_box) or (
                    (decline_x, decline_y)
                    if decline_x is not None and decline_y is not None
                    else None
                )
                end_center = _center(end_box) or (
                    (end_x, end_y)
                    if end_x is not None and end_y is not None
                    else None
                )

                if state == "incoming" and not self._owned_call:
                    if answer_center is None:
                        _log(self.player, "[WhatsAppCall] Incoming-call visuals detected, but Answer was not clear enough.")
                        continue

                    click_x = origin_x + round(answer_center[0] * sx)
                    click_y = origin_y + round(answer_center[1] * sy)
                    _log(
                        self.player,
                        f"[WhatsAppCall] Visual Answer target: ({click_x},{click_y}). Answering."
                    )
                    _click_screen(click_x, click_y)
                    self._owned_call = True
                    self._message_sent = False
                    time.sleep(1.0)
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
                                "[WhatsAppCall] No virtual audio route found. "
                                "The caller will only hear JARVIS when WhatsApp's microphone "
                                "is routed to JARVIS's audio output."
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

                    # The speech path is allowed to finish before we ask vision
                    # where the current End button is.
                    time.sleep(4.5)

                    try:
                        image_bytes2, sx2, sy2, origin_x2, origin_y2, image2 = _capture_screen()
                        result2 = _vision(image_bytes2) or {}
                    except Exception as exc:
                        _log(self.player, f"[WhatsAppCall] Could not re-check the connected call: {exc}")
                        result2 = {}

                    state2 = str(result2.get("state") or "none").strip().lower()
                    end_box2 = _valid_box(_box(result2.get("end_box")), image2.width, image2.height)
                    end_center2 = _center(end_box2)

                    ex = _coord(result2.get("end_x"))
                    ey = _coord(result2.get("end_y"))
                    if end_center2 is None and ex is not None and ey is not None:
                        end_center2 = (ex, ey)

                    if (
                        state2 == "connected"
                        and end_center2 is not None
                    ):
                        _log(self.player, "[WhatsAppCall] Visual detector found End Call. Ending the call.")
                        click_x2 = origin_x2 + round(end_center2[0] * sx2)
                        click_y2 = origin_y2 + round(end_center2[1] * sy2)
                        _click_screen(click_x2, click_y2)
                    elif state2 == "connected":
                        _log(self.player, "[WhatsAppCall] Call is connected, but End Call was not visually clear.")
                    else:
                        _log(self.player, "[WhatsAppCall] Call is no longer connected.")

                    try:
                        from core import call_audio
                        call_audio.stop()
                    except Exception:
                        pass

                    self._owned_call = False
                    self._message_sent = False
                    continue

                if self._owned_call and state == "none":
                    # The other party may have ended the call. Never click a
                    # coordinate after the connected call UI disappears.
                    self._owned_call = False
                    self._message_sent = False
                    try:
                        from core import call_audio
                        call_audio.stop()
                    except Exception:
                        pass

        except Exception as exc:
            _log(self.player, f"[WhatsAppCall] Watcher stopped unexpectedly: {exc}")


_WATCHER: WhatsAppCallWatcher | None = None
_WATCHER_LOCK = threading.Lock()


def _one_shot_call_action(action: str, player=None) -> str:
    """Visually find and click Answer or Decline on the current incoming call."""
    if platform.system() != "Windows":
        return "WhatsApp call control requires Windows."

    _make_dpi_aware()

    try:
        image_bytes, sx, sy, origin_x, origin_y, image = _capture_screen()
        result = _vision(image_bytes) or {}
    except Exception as exc:
        return f"Could not inspect the WhatsApp call visually: {exc}"

    state = str(result.get("state") or "none").strip().lower()
    try:
        confidence = float(result.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0

    if state != "incoming" or confidence < _CONFIDENCE:
        _log(player, f"[WhatsAppCall] Visual {action} rejected: state={state} confidence={confidence}")
        return "No active incoming WhatsApp call was visually confirmed."

    box_key = "answer_box" if action == "answer" else "decline_box"
    x_key = "answer_x" if action == "answer" else "decline_x"
    y_key = "answer_y" if action == "answer" else "decline_y"

    box = _valid_box(_box(result.get(box_key)), image.width, image.height)
    center = _center(box)

    if center is None:
        x = _coord(result.get(x_key))
        y = _coord(result.get(y_key))
        center = (x, y) if x is not None and y is not None else None

    if center is None:
        return f"Visual detector found the incoming call but not the {action} button."

    click_x = origin_x + round(center[0] * sx)
    click_y = origin_y + round(center[1] * sy)

    _log(player, f"[WhatsAppCall] Visual {action} target: ({click_x},{click_y})")
    _click_screen(click_x, click_y)
    return f"Visually located and clicked WhatsApp {action}."



def whatsapp_call_rule(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "enable").strip().lower()
    message = " ".join(str(params.get("message") or _DEFAULT_MESSAGE).split()).strip()

    global _WATCHER

    if action in {"answer", "accept"}:
        return _one_shot_call_action("answer", player=player)

    if action in {"decline", "reject"}:
        return _one_shot_call_action("decline", player=player)

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
                "description": "enable (default), disable, status, answer, or decline",
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
