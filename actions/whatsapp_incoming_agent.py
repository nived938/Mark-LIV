"""WhatsApp Desktop incoming-call agent.

Detection is intentionally redundant. Mark uses three independent Windows
signals at the same time:
1. Windows toast notifications (caller/name when WhatsApp publishes a toast).
2. Screen/computer vision (looks for the WhatsApp incoming-call button colors).
3. Windows UI Automation / Win32 window inspection.

No WhatsApp Web is used. The three detectors identify the call; control is
performed by focusing the WhatsApp call window and using the keyboard sequence
for Accept/Decline, without mouse input.
"""

from __future__ import annotations

import ctypes
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

try:
    from pywinauto import Desktop
except Exception:
    Desktop = None

try:
    import psutil
except Exception:
    psutil = None

try:
    import pyautogui
except Exception:
    pyautogui = None

try:
    import cv2
    import numpy as np
    import mss
except Exception:
    cv2 = np = mss = None

# Windows notification APIs are optional. The feature still works with the
# screen + UIA/Win32 detectors when these packages are unavailable.
try:
    from winrt.windows.ui.notifications import NotificationKinds
    from winrt.windows.ui.notifications.management import (
        UserNotificationListener,
    )
except Exception:
    NotificationKinds = None
    UserNotificationListener = None


_GENERIC = {
    "whatsapp", "accept", "answer", "decline", "reject", "ignore", "cancel",
    "more", "device settings", "settings", "video call", "voice call",
    "audio call", "start video call", "start voice call", "calling",
    "calling...", "incoming call", "incoming call...", "someone",
}


@dataclass
class IncomingCall:
    caller: str
    window: object
    accept_control: object
    decline_control: object
    detected_at: float
    accept_point: Optional[tuple[int, int]] = None
    decline_point: Optional[tuple[int, int]] = None
    detection_sources: tuple[str, ...] = ()


class WhatsAppIncomingAgent:
    """Redundant incoming-call detector/controller for WhatsApp Desktop."""

    def __init__(
        self,
        on_incoming: Optional[Callable[[IncomingCall], None]] = None,
        poll_seconds: float = 0.5,
    ):
        self.on_incoming = on_incoming
        self.poll_seconds = max(0.25, float(poll_seconds))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._pending: Optional[IncomingCall] = None
        self._last_signature = ""
        self._last_seen_at = 0.0
        self._notification_seen: set[str] = set()
        self._notification_ready_logged = False
        self._visual_last_log = 0.0
        self._visual_present = False
        self._event_cooldown_until = 0.0
        self._last_visual_signature = None

    @property
    def pending(self) -> Optional[IncomingCall]:
        with self._lock:
            return self._pending

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="WhatsAppIncomingAgent",
            daemon=True,
        )
        self._thread.start()
        print(
            "[WhatsAppAgent] Incoming-call monitor started "
            "(notification + UIA; visual controls require notification confirmation)."
        )

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    @staticmethod
    def _safe_text(control) -> str:
        try:
            return str(control.window_text() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _safe_id(control) -> str:
        try:
            return str(getattr(control, "automation_id", lambda: "")() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _safe_class(control) -> str:
        try:
            return str(getattr(control, "class_name", lambda: "")() or "").strip()
        except Exception:
            return ""

    def _is_whatsapp_process(self, control) -> bool:
        if psutil is None:
            return False
        try:
            pid = int(control.process_id())
            p = psutil.Process(pid)
            blob = " ".join(
                [p.name(), p.exe() or "", " ".join(p.cmdline())]
            ).lower()
            return "whatsapp" in blob
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Detector 1: Windows notifications
    # ------------------------------------------------------------------

    @staticmethod
    def _notification_text(notification) -> str:
        parts = []
        try:
            app = notification.app_info
            parts.append(str(app.display_info.display_name or ""))
        except Exception:
            pass
        try:
            binding = notification.notification.visual.get_binding(
                "ToastGeneric"
            )
            for item in binding.get_text_elements():
                value = str(item.text or "").strip()
                if value:
                    parts.append(value)
        except Exception:
            pass
        try:
            # Some WinRT projections expose the XML instead.
            xml = str(notification.notification.content.get_xml())
            parts.append(xml)
        except Exception:
            pass
        return " ".join(x for x in parts if x).strip()

    def _poll_notifications(self):
        if UserNotificationListener is None or NotificationKinds is None:
            if not self._notification_ready_logged:
                print(
                    "[WhatsAppAgent] Notification detector unavailable "
                    "(optional WinRT notification packages not installed)."
                )
                self._notification_ready_logged = True
            return None

        try:
            listener = UserNotificationListener.current
            # Access can be denied on Windows privacy settings. Requesting it
            # here is safe; Windows decides whether the desktop app may read it.
            try:
                status = listener.request_access_async().get()
                if not self._notification_ready_logged:
                    print(f"[WhatsAppAgent] Notification access: {status}")
                    self._notification_ready_logged = True
            except Exception:
                if not self._notification_ready_logged:
                    print(
                        "[WhatsAppAgent] Notification access could not be requested; "
                        "continuing with screen + UIA detectors."
                    )
                    self._notification_ready_logged = True

            notes = listener.get_notifications_async(
                NotificationKinds.TOAST
            ).get()
            newest = None
            for note in notes:
                text = self._notification_text(note)
                low = self._norm(text)
                if "whatsapp" not in low:
                    continue
                if not any(
                    word in low
                    for word in ("incoming call", "calling", "voice call", "video call")
                ):
                    continue

                key = low[-500:]
                if key in self._notification_seen:
                    continue
                self._notification_seen.add(key)
                if len(self._notification_seen) > 100:
                    self._notification_seen = set(list(self._notification_seen)[-50:])

                caller = self._caller_from_text(text)
                newest = (caller, text)
            return newest
        except Exception as exc:
            if not self._notification_ready_logged:
                print(f"[WhatsAppAgent] Notification detector error: {exc}")
                self._notification_ready_logged = True
        return None

    # ------------------------------------------------------------------
    # Detector 2: screen/computer vision
    # ------------------------------------------------------------------

    def _visual_call_controls(self):
        """Find the typical red/green WhatsApp call buttons on the screen.

        This deliberately does not assume a fixed resolution. It searches the
        whole virtual desktop for saturated red/green circular-ish regions,
        then returns their centers. UIA is preferred when it exposes controls.
        """
        if mss is None or cv2 is None or np is None:
            return None
        try:
            with mss.mss() as shot:
                monitors = shot.monitors[1:]
                if not monitors:
                    return None

                candidates = []
                for mon in monitors:
                    img = np.array(shot.grab(mon))
                    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

                    # WhatsApp incoming controls are normally highly saturated
                    # green (accept) and red (decline).
                    green = cv2.inRange(
                        hsv, np.array([35, 90, 70]), np.array([95, 255, 255])
                    )
                    red1 = cv2.inRange(
                        hsv, np.array([0, 100, 70]), np.array([12, 255, 255])
                    )
                    red2 = cv2.inRange(
                        hsv, np.array([165, 100, 70]), np.array([179, 255, 255])
                    )
                    red = cv2.bitwise_or(red1, red2)

                    for kind, mask in (("accept", green), ("decline", red)):
                        contours, _ = cv2.findContours(
                            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                        )
                        for contour in contours:
                            area = cv2.contourArea(contour)
                            if area < 250 or area > 100000:
                                continue
                            x, y, w, h = cv2.boundingRect(contour)
                            if min(w, h) < 12 or max(w, h) > 500:
                                continue
                            ratio = w / max(1, h)
                            if ratio < 0.45 or ratio > 2.2:
                                continue
                            cx = mon["left"] + x + w // 2
                            cy = mon["top"] + y + h // 2
                            # Prefer compact button-sized regions.
                            score = abs(w - h) + abs(w - 70) * 0.15
                            candidates.append((score, kind, cx, cy, w, h))

                accepts = [x for x in candidates if x[1] == "accept"]
                declines = [x for x in candidates if x[1] == "decline"]
                if not accepts or not declines:
                    self._visual_present = False
                    return None

                # The two controls should be reasonably close together.
                best = None
                for a in accepts:
                    for d in declines:
                        distance = ((a[2] - d[2]) ** 2 + (a[3] - d[3]) ** 2) ** 0.5
                        if distance > 900:
                            continue
                        pair_score = a[0] + d[0] + distance * 0.05
                        if best is None or pair_score < best[0]:
                            best = (pair_score, a, d)

                if not best:
                    self._visual_present = False
                    return None

                _, a, d = best
                self._visual_present = True
                visual_signature = (round(a[2] / 40), round(a[3] / 40), round(d[2] / 40), round(d[3] / 40))
                if visual_signature == self._last_visual_signature:
                    return None
                self._last_visual_signature = visual_signature
                # Visual matching is intentionally silent here. A color pair
                # on the desktop is only a supporting signal and is not itself
                # considered an incoming call. Actual events are logged after
                # notification/UIA corroboration in _find_incoming().
                return (a[2], a[3]), (d[2], d[3])
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Detector 3: UI Automation + Win32
    # ------------------------------------------------------------------

    def _find_whatsapp_windows(self):
        if Desktop is None:
            return []
        try:
            return Desktop(backend="uia").windows(visible_only=False)
        except Exception:
            return []

    def _looks_like_whatsapp(self, window) -> bool:
        title = self._norm(self._safe_text(window))
        aid = self._norm(self._safe_id(window))
        cls = self._norm(self._safe_class(window))
        return (
            "whatsapp" in title
            or "whatsapp" in aid
            or "whatsapp" in cls
            or self._is_whatsapp_process(window)
        )

    def _find_buttons(self, window):
        accept = decline = None
        try:
            controls = window.descendants(control_type="Button")
        except Exception:
            controls = []

        for control in controls:
            name = self._norm(self._safe_text(control))
            aid = self._norm(self._safe_id(control))
            if accept is None and (
                name in {"accept", "answer"}
                or "accept" in name
                or "answer" in name
                or "accept" in aid
                or "answer" in aid
            ):
                accept = control
            if decline is None and (
                name in {"decline", "reject", "ignore"}
                or "decline" in name
                or "reject" in name
                or "ignore" in name
                or any(x in aid for x in ("decline", "reject", "ignore"))
            ):
                decline = control
        return accept, decline

    def _win32_whatsapp_windows(self):
        if psutil is None:
            return []

        user32 = ctypes.windll.user32
        results = []

        enum_proc_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        @enum_proc_type
        def callback(hwnd, _lparam):
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pid_value = int(pid.value)
                if not pid_value:
                    return True
                proc = psutil.Process(pid_value)
                blob = " ".join(
                    [proc.name(), proc.exe() or "", " ".join(proc.cmdline())]
                ).lower()
                if "whatsapp" not in blob:
                    return True

                length = user32.GetWindowTextLengthW(hwnd)
                title_buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title_buf, length + 1)

                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)

                visible = bool(user32.IsWindowVisible(hwnd))
                results.append(
                    {
                        "hwnd": hwnd,
                        "pid": pid_value,
                        "title": title_buf.value,
                        "class": class_buf.value,
                        "visible": visible,
                    }
                )
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows.argtypes = [enum_proc_type, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            callback_ref = callback
            user32.EnumWindows(callback_ref, 0)
        except Exception:
            return []
        return results

    def _best_whatsapp_chat_caller(self) -> str:
        # The native popup can be a WebView/non-client surface with no caller
        # text. WhatsApp normally activates the caller's chat, so use a short
        # visible chat-header/button label as the fallback.
        found = []
        for window in self._find_whatsapp_windows():
            if not self._looks_like_whatsapp(window):
                continue
            try:
                for control in window.descendants():
                    text = self._safe_text(control).strip()
                    low = self._norm(text)
                    if not text or low in _GENERIC or len(text) > 60:
                        continue
                    if any(x in low for x in ("search", "type a message", "chat list", "whatsapp business", "web content")):
                        continue
                    if any(x in low for x in ("voice call", "video call", "missed call", "no answer")):
                        continue
                    found.append(text)
            except Exception:
                continue
        if not found:
            return ""
        return min(found, key=len)

    def _extract_caller(self, window) -> str:
        texts = []
        try:
            title = self._safe_text(window)
            if title:
                texts.append(title)
        except Exception:
            pass

        try:
            for control in window.descendants():
                text = self._safe_text(control)
                if text:
                    texts.append(text)
        except Exception:
            pass

        return self._caller_from_text(" | ".join(texts)) or "someone"

    def _caller_from_text(self, raw: str) -> str:
        if not raw:
            return ""
        texts = [re.sub(r"\s+", " ", x).strip() for x in str(raw).split("|")]
        patterns = (
            r"(?:incoming\s+call\s+from|call\s+from|incoming\s+call|calling)\s*[:\-]?\s*(.+)$",
            r"^(.+?)\s+(?:is\s+)?calling$",
        )
        for clean in texts:
            for pattern in patterns:
                m = re.search(pattern, clean, flags=re.IGNORECASE)
                if m:
                    candidate = m.group(1).strip(" -:|.")
                    if candidate and self._norm(candidate) not in _GENERIC:
                        return candidate

        for clean in texts:
            low = self._norm(clean.strip(" -:|."))
            if not clean or low in _GENERIC:
                continue
            if len(clean) > 80 or clean.isdigit():
                continue
            if any(
                word in low
                for word in (
                    "incoming", "call", "device", "microphone", "camera",
                    "settings", "whatsapp", "calling",
                )
            ):
                continue
            return clean
        return ""

    def _find_incoming(self):
        if time.time() < self._event_cooldown_until:
            return None
        sources = []
        caller = ""

        # Detector 1.
        notification = self._poll_notifications()
        if notification:
            caller = notification[0]
            sources.append("notification")

        # Detector 3a: UI Automation.
        for window in self._find_whatsapp_windows():
            accept, decline = self._find_buttons(window)
            if accept is None or decline is None:
                continue
            if not self._looks_like_whatsapp(window):
                continue
            detected_caller = self._extract_caller(window)
            if self._norm(detected_caller) not in {"someone", "non client input sink window"}:
                caller = caller or detected_caller
            sources.append("uia")
            return IncomingCall(
                caller=caller or "someone",
                window=window,
                accept_control=accept,
                decline_control=decline,
                detected_at=time.time(),
                detection_sources=tuple(dict.fromkeys(sources)),
            )

        # Detector 3b: Win32 confirms WhatsApp is creating native windows.
        win32 = self._win32_whatsapp_windows()
        if win32:
            sources.append("win32")

        # Detector 2: visual controls. This is intentionally independent of
        # WhatsApp's accessibility tree.
        points = self._visual_call_controls()
        if points and ("notification" in sources or "win32" in sources):
            accept_point, decline_point = points
            sources.append("vision")
            notification_text = notification[1] if notification else ""
            return IncomingCall(
                caller=caller or self._caller_from_text(notification_text) or "unknown caller",
                window=None,
                accept_control=None,
                decline_control=None,
                detected_at=time.time(),
                accept_point=accept_point,
                decline_point=decline_point,
                detection_sources=tuple(dict.fromkeys(sources)),
            )

        return None

    @staticmethod
    def _click(control) -> tuple[bool, str]:
        if control is None:
            return False, "No UI Automation control is available."
        try:
            control.invoke()
            return True, ""
        except Exception:
            try:
                control.click_input()
                return True, ""
            except Exception as exc:
                return False, str(exc)

    def _focus_whatsapp_call_window(self, call: Optional[IncomingCall]) -> bool:
        """Focus the actual WhatsApp call surface before keyboard navigation."""
        if platform.system() != "Windows":
            return False

        try:
            user32 = ctypes.windll.user32
            target_hwnd = 0

            if call is not None and call.window is not None:
                try:
                    target_hwnd = int(call.window.handle)
                except Exception:
                    target_hwnd = 0

            foreground = int(user32.GetForegroundWindow() or 0)
            if not target_hwnd and foreground:
                for item in self._win32_whatsapp_windows():
                    if int(item.get("hwnd") or 0) == foreground:
                        target_hwnd = foreground
                        break

            if not target_hwnd:
                visible = [w for w in self._win32_whatsapp_windows() if w.get("visible")]

                def area(item):
                    try:
                        rect = wintypes.RECT()
                        user32.GetWindowRect(item["hwnd"], ctypes.byref(rect))
                        return max(1, int(rect.right - rect.left)) * max(
                            1, int(rect.bottom - rect.top)
                        )
                    except Exception:
                        return 10**18

                if visible:
                    target_hwnd = int(min(visible, key=area).get("hwnd") or 0)

            if not target_hwnd:
                return False

            try:
                user32.ShowWindow(target_hwnd, 9)
            except Exception:
                pass

            try:
                user32.SetForegroundWindow(target_hwnd)
            except Exception:
                pass

            if call is not None and call.window is not None:
                try:
                    call.window.set_focus()
                except Exception:
                    pass

            time.sleep(0.20)
            return int(user32.GetForegroundWindow() or 0) == target_hwnd
        except Exception:
            return False

    def _keyboard_select(self, action: str, call: Optional[IncomingCall]) -> tuple[bool, str]:
        """Focus WhatsApp, then Tab N times and press Enter. No mouse is used."""
        if action not in {"accept", "decline"}:
            return False, f"Unsupported incoming-call action: {action}"

        if not pyautogui:
            return False, "pyautogui is not installed."

        if not self._focus_whatsapp_call_window(call):
            return False, "Could not focus the WhatsApp call window."

        tabs = 3 if action == "accept" else 4
        try:
            for _ in range(tabs):
                pyautogui.press("tab")
                time.sleep(0.12)
            pyautogui.press("enter")
            label = "Accept" if action == "accept" else "Decline"
            return True, f"Focused WhatsApp call window and used {label}: Tab x{tabs} + Enter."
        except Exception as exc:
            return False, f"Keyboard control failed: {exc}"

    def _fresh_or_pending(self):
        with self._lock:
            pending = self._pending
        fresh = self._find_incoming()
        return fresh or pending

    def accept(self) -> tuple[bool, str]:
        call = self._fresh_or_pending()
        if not call:
            return False, "There is no pending WhatsApp incoming call."

        ok, error = self._keyboard_select("accept", call)

        if ok:
            with self._lock:
                self._pending = None
                self._last_signature = ""
            self._event_cooldown_until = time.time() + 5.0
        return ok, error

    def decline(self) -> tuple[bool, str]:
        call = self._fresh_or_pending()
        if not call:
            return False, "There is no pending WhatsApp incoming call."

        ok, error = self._keyboard_select("decline", call)

        if ok:
            with self._lock:
                self._pending = None
                self._last_signature = ""
            self._event_cooldown_until = time.time() + 5.0
        return ok, error

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                call = self._find_incoming()
                if call is None:
                    with self._lock:
                        if self._pending and time.time() - self._last_seen_at > 120:
                            self._pending = None
                            self._last_signature = ""
                    continue

                signature = self._norm(call.caller)
                with self._lock:
                    already_pending = self._pending is not None
                    same_recent = (
                        signature == self._last_signature
                        and time.time() - self._last_seen_at < 5
                    )
                    if already_pending or same_recent:
                        continue
                    self._pending = call
                    self._last_signature = signature
                    self._last_seen_at = time.time()

                print(
                    f"[WhatsAppAgent] Incoming call detected from {call.caller} "
                    f"via {', '.join(call.detection_sources) or 'unknown'}."
                )
                if self.on_incoming:
                    try:
                        self.on_incoming(call)
                    except Exception as exc:
                        print(f"[WhatsAppAgent] Event callback failed: {exc}")
            except Exception as exc:
                print(f"[WhatsAppAgent] Detection error: {exc}")


_AGENT: Optional[WhatsAppIncomingAgent] = None


def get_incoming_agent() -> WhatsAppIncomingAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = WhatsAppIncomingAgent()
    return _AGENT


def start_incoming_call_agent(
    on_incoming: Callable[[IncomingCall], None],
) -> WhatsAppIncomingAgent:
    agent = get_incoming_agent()
    agent.on_incoming = on_incoming
    agent.start()
    return agent
