"""
Audio capture using sounddevice (supports ASIO on Windows).

The audio callback runs in a PortAudio real-time thread.
Decoded Timecodes are placed on a queue.Queue for the main thread to consume.
"""
from __future__ import annotations

import queue
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np

from ltc_decoder import LTCDecoder, Timecode


# ── Device enumeration ────────────────────────────────────────────────────────

def list_audio_devices() -> List[Dict[str, Any]]:
    """
    Return all input-capable audio devices.
    Each entry: {"index", "name", "channels", "hostapi", "default_samplerate"}
    """
    try:
        import sounddevice as sd
        result = []
        hostapis = sd.query_hostapis()
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                api_name = hostapis[dev["hostapi"]]["name"] if dev["hostapi"] < len(hostapis) else ""
                result.append({
                    "index":            i,
                    "name":             dev["name"],
                    "channels":         dev["max_input_channels"],
                    "hostapi":          api_name,
                    "default_samplerate": int(dev["default_samplerate"]),
                })
        return result
    except Exception:
        return []


def list_asio_devices() -> List[Dict[str, Any]]:
    """Return only ASIO input devices."""
    return [d for d in list_audio_devices() if "asio" in d["hostapi"].lower()]


def reinit_portaudio() -> None:
    """
    Force PortAudio to re-scan hardware by terminating and re-initialising.
    Call this before list_audio_devices() when the user clicks Refresh,
    so changes made in the driver control panel (e.g. SoundGrid channel count)
    are reflected immediately without restarting the app.
    Safe to call only when no stream is open.
    """
    try:
        import sounddevice as sd
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def get_channel_names(device_index: int, n_channels: int, hostapi: str) -> List[str]:
    """
    Return a human-readable label for each input channel.

    ASIO (Windows): queries PaAsio_GetInputChannelName from the PortAudio ASIO DLL.
    CoreAudio / WDM / MME / all other hosts: returns "Ch 1", "Ch 2", …
    """
    if sys.platform == "win32" and "asio" in hostapi.lower():
        try:
            import ctypes
            import sounddevice as _sd

            _pa = ctypes.CDLL(_sd._libname)
            _pa.PaAsio_GetInputChannelName.restype  = ctypes.c_int
            _pa.PaAsio_GetInputChannelName.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_char_p),
            ]
            names: List[str] = []
            for i in range(n_channels):
                ptr = ctypes.c_char_p()
                err = _pa.PaAsio_GetInputChannelName(device_index, i, ctypes.byref(ptr))
                label = ptr.value.decode("utf-8", errors="replace") if (err == 0 and ptr.value) else f"Ch {i + 1}"
                names.append(f"{i + 1} — {label}")
            return names
        except Exception:
            pass

    return [f"Ch {i + 1}" for i in range(n_channels)]


# ── AudioCapture ──────────────────────────────────────────────────────────────

class AudioCapture:
    """
    Opens a sounddevice InputStream, feeds audio to LTCDecoder,
    and puts decoded Timecodes on tc_queue.

    All public methods must be called from the main thread only.
    The audio callback runs in a PortAudio thread.
    """

    def __init__(self, tc_queue: "queue.Queue[Timecode]") -> None:
        self._tc_queue = tc_queue
        self._decoder: Optional[LTCDecoder] = None
        self._stream = None
        self._device_index: Optional[int] = None
        self._channel: int = 0          # 0-based
        self._sample_rate: int = 48000
        self._block_size: int = 512
        self._running: bool = False
        self._last_callback_time: float = 0.0

    # ── configuration ─────────────────────────────────────────────────────────

    def configure(
        self,
        device_index: int,
        channel: int,
        sample_rate: int = 48000,
        block_size: int = 512,
    ) -> None:
        """
        Set capture parameters. Must be called before start().
        channel: 0-based index into the device's input channels.
        block_size: PortAudio callback buffer size in samples.
        """
        self._device_index = device_index
        self._channel = channel
        self._sample_rate = sample_rate
        self._block_size = block_size

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        if self._device_index is None:
            raise RuntimeError("Call configure() before start()")

        import sounddevice as sd

        self._decoder = LTCDecoder(self._sample_rate)
        self._decoder.on_timecode = self._on_timecode

        for attempt in range(2):
            try:
                dev_info = sd.query_devices(self._device_index)
                n_ch = int(dev_info["max_input_channels"])
                if self._channel >= n_ch:
                    raise ValueError(
                        f"Channel {self._channel + 1} not available — "
                        f"device '{dev_info['name']}' has {n_ch} input channel(s)"
                    )
                self._stream = sd.InputStream(
                    device=self._device_index,
                    channels=n_ch,
                    samplerate=self._sample_rate,
                    blocksize=self._block_size,
                    dtype="float32",
                    callback=self._audio_callback,
                    latency="low",
                )
                self._stream.start()
                try:
                    print(
                        f"[audio] stream open: SR={int(self._stream.samplerate)}  "
                        f"block={self._block_size}  "
                        f"latency={float(self._stream.latency)*1000:.1f}ms"
                    )
                except Exception:
                    pass
                break  # success
            except sd.PortAudioError:
                if attempt == 0:
                    # Driver may have reset (e.g. SoundGrid connecting another app).
                    # Re-initialise PortAudio once and retry.
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
                    continue
                raise  # second attempt also failed — propagate to caller

        self._running = True
        self._last_callback_time = time.monotonic()

        # For ASIO devices the driver controls the sample rate; the rate we
        # requested may differ from what the stream actually opened at.
        # Re-initialise the decoder with the real rate if needed.
        actual_sr = int(self._stream.samplerate)
        if actual_sr != self._sample_rate:
            self._sample_rate = actual_sr
            self._decoder = LTCDecoder(actual_sr)
            self._decoder.on_timecode = self._on_timecode

    def stop(self) -> None:
        self._running = False
        self._last_callback_time = 0.0
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._decoder = None

    # ── status ────────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stream_active(self) -> bool:
        """False if the underlying PortAudio stream has stopped unexpectedly (e.g. driver reset)."""
        if not self._running or self._stream is None:
            return False
        try:
            return bool(self._stream.active)
        except Exception:
            return False

    @property
    def callback_stalled(self) -> bool:
        """True if no audio callback received for 3+ s — detects silent ASIO death on Windows."""
        if not self._running or self._last_callback_time == 0.0:
            return False
        return time.monotonic() - self._last_callback_time > 3.0

    @property
    def actual_sample_rate(self) -> Optional[int]:
        """The sample rate the stream actually opened at (may differ from configured rate for ASIO)."""
        if self._stream is not None:
            return int(self._stream.samplerate)
        return None

    @property
    def signal_present(self) -> bool:
        return self._decoder.signal_present if self._decoder else False

    @property
    def detected_fps(self) -> Optional[float]:
        return self._decoder.detected_fps if self._decoder else None

    @property
    def is_locked(self) -> bool:
        return self._decoder.is_locked if self._decoder else False

    # ── private ───────────────────────────────────────────────────────────────

    def _on_timecode(self, tc: Timecode) -> None:
        try:
            self._tc_queue.put_nowait(tc)
        except queue.Full:
            pass  # discard; main thread is falling behind

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Called from PortAudio real-time thread. Must not block."""
        self._last_callback_time = time.monotonic()
        if not self._running or self._decoder is None:
            return
        try:
            mono = indata[:, self._channel]
            self._decoder.push_samples(mono)
        except Exception:
            pass  # never let an exception propagate into PortAudio
