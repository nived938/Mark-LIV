"""Audio mirror for routing JARVIS speech into a WhatsApp call.

The normal JARVIS speaker path stays untouched. When a WhatsApp auto-reply is
active, this module can duplicate JARVIS PCM to a dedicated output endpoint
such as VB-CABLE Input or VoiceMeeter Input. WhatsApp must use the matching
virtual microphone endpoint so the caller receives the speech.

No virtual audio driver is installed by this module. When no dedicated output
endpoint is available, mirroring remains disabled and JARVIS continues to use
its normal speaker.
"""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import sounddevice as sd


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


_CONFIG = _base_dir() / "config" / "api_keys.json"
_SAMPLE_RATE = 24000
_CHANNELS = 1
_DTYPE = "int16"


def _config() -> dict:
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _configured_device() -> str:
    value = _config().get("whatsapp_call_audio_device")
    return str(value or "").strip()


def _find_output_device(preferred: str = ""):
    """Return a sounddevice output endpoint, preferring an exact configured name."""
    devices = list(sd.query_devices())

    if preferred:
        low = preferred.lower()
        for idx, dev in enumerate(devices):
            name = str(dev.get("name") or "")
            if dev.get("max_output_channels", 0) > 0 and (
                name.lower() == low or low in name.lower()
            ):
                return idx, name

    # Prefer a virtual/call-routing endpoint. Physical speakers are deliberately
    # not auto-selected because duplicating JARVIS onto the normal speakers would
    # produce doubled audio.
    candidates = (
        "cable input",
        "vb-audio",
        "voicemeeter input",
        "voicemeeter aux input",
        "virtual audio",
        "line 1",
    )
    for token in candidates:
        for idx, dev in enumerate(devices):
            name = str(dev.get("name") or "")
            if dev.get("max_output_channels", 0) > 0 and token in name.lower():
                return idx, name

    return None, ""


class _Mirror:
    def __init__(self):
        self._lock = threading.Lock()
        self._enabled = False
        self._stop = threading.Event()
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=80)
        self._thread: threading.Thread | None = None
        self._stream = None
        self._device_name = ""

    def start(self, device: str = "") -> tuple[bool, str]:
        with self._lock:
            if self._enabled:
                return True, self._device_name or "active"

            idx, name = _find_output_device(device or _configured_device())
            if idx is None:
                return False, "No dedicated WhatsApp call audio output was found."

            self._stop.clear()
            self._enabled = True
            self._device_name = name
            self._thread = threading.Thread(
                target=self._worker,
                args=(idx, name),
                daemon=True,
                name="whatsapp-call-audio",
            )
            self._thread.start()
            return True, name

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            self._stop.set()
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            stream = self._stream
            self._stream = None
            self._device_name = ""

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def push(self, pcm: bytes) -> None:
        with self._lock:
            enabled = self._enabled
        if not enabled or not pcm:
            return
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            # Dropping the oldest mirror block is preferable to ever blocking
            # JARVIS's real speaker path.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(pcm)
            except queue.Full:
                pass

    def _worker(self, idx: int, name: str) -> None:
        stream = None
        try:
            stream = sd.RawOutputStream(
                samplerate=_SAMPLE_RATE,
                channels=_CHANNELS,
                dtype=_DTYPE,
                blocksize=1024,
                device=idx,
            )
            stream.start()
            with self._lock:
                self._stream = stream
            print(f"[CallAudio] Mirroring JARVIS speech to {name}")

            while not self._stop.is_set():
                try:
                    pcm = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    stream.write(pcm)
                except Exception as exc:
                    print(f"[CallAudio] Output failed: {exc}")
                    break
        except Exception as exc:
            print(f"[CallAudio] Could not open {name}: {exc}")
        finally:
            with self._lock:
                if self._stream is stream:
                    self._stream = None
                self._enabled = False
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass


_mirror = _Mirror()


def start(device: str = "") -> tuple[bool, str]:
    return _mirror.start(device)


def stop() -> None:
    _mirror.stop()


def push(pcm: bytes) -> None:
    _mirror.push(pcm)
