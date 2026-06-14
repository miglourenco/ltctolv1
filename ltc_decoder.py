"""
Software LTC (Linear Timecode) Decoder — SMPTE 12M / BMC
=========================================================
Decodes LTC from raw float32 mono audio samples without any native library.

Biphase Mark Code (BMC) rules
------------------------------
  • Every bit has a transition at its START.
  • A '1' bit has an ADDITIONAL transition at its MIDPOINT.
  • A '0' bit has no mid-bit transition.

Zero-crossing detection maps to intervals:
  short ≈ T/2  →  half-clock-period  (first half of a '1' bit)
  long  ≈ T    →  full-clock-period  (a '0' bit)
  short + short → '1' bit

SMPTE LTC frame layout (80 bits, transmitted LSB-first within each BCD group)
-------------------------------------------------------------------------------
  Bits  0- 3   Frame units BCD
  Bits  4- 7   User bits group 1
  Bits  8- 9   Frame tens BCD (max 2)
  Bit  10      Drop-Frame flag
  Bit  11      Color-Frame flag
  Bits 12-15   User bits group 2
  Bits 16-19   Seconds units BCD
  Bits 20-23   User bits group 3
  Bits 24-26   Seconds tens BCD (max 5)
  Bit  27      BMPC
  Bits 28-31   User bits group 4
  Bits 32-35   Minutes units BCD
  Bits 36-39   User bits group 5
  Bits 40-42   Minutes tens BCD (max 5)
  Bit  43      BGF1
  Bits 44-47   User bits group 6
  Bits 48-51   Hours units BCD
  Bits 52-55   User bits group 7
  Bits 56-57   Hours tens BCD (max 2)
  Bit  58      BGF2
  Bit  59      (reserved)
  Bits 60-63   User bits group 8
  Bits 64-79   Sync word  0011 1111 1111 1101  (= 0x3FFD, LSB-first in shift reg)

Thread safety: NOT thread-safe. Call push_samples() from a single thread only.
"""
from __future__ import annotations

import numpy as np
from collections import deque
from typing import Callable, List, NamedTuple, Optional


# ── Timecode ──────────────────────────────────────────────────────────────────

class Timecode(NamedTuple):
    hours:      int
    minutes:    int
    seconds:    int
    frames:     int
    drop_frame: bool
    fps:        float

    def __str__(self) -> str:
        sep = ";" if self.drop_frame else ":"
        return f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}{sep}{self.frames:02d}"

    def to_frame_number(self) -> int:
        return (self.hours * 3600 + self.minutes * 60 + self.seconds) * round(self.fps) + self.frames

    @staticmethod
    def from_string(s: str, fps: float = 25.0) -> "Timecode":
        drop = ";" in s
        parts = s.replace(";", ":").split(":")
        return Timecode(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), drop, fps)


# ── LTC Decoder ───────────────────────────────────────────────────────────────

# Sync word value when bits 64-79 are pushed into a left-shift register
# (newest bit at LSB, bit-64 ends up at position 15):
#   bits[64..79] = 0,0,1,1,1,1,1,1,1,1,1,1,1,1,0,1
#   → 0*2^15 + 0*2^14 + 1*2^13 + … + 0*2^1 + 1*2^0 = 0x3FFD
_SYNC_FWD = 0x3FFD

# Minimum crossings before attempting frame lock (allows EMA to stabilise)
_CALIB_MIN = 32


class LTCDecoder:
    """
    Stateful software LTC decoder.

    Usage::

        decoder = LTCDecoder(sample_rate=48000)
        decoder.on_timecode = my_callback   # called with a Timecode

        # in audio callback (one thread only):
        decoder.push_samples(mono_float32_array)
    """

    def __init__(self, sample_rate: int = 48000) -> None:
        self.sample_rate = sample_rate
        self.on_timecode: Optional[Callable[[Timecode], None]] = None
        self._reset_state()

    # ── public API ────────────────────────────────────────────────────────────

    def push_samples(self, samples: np.ndarray) -> List[Timecode]:
        """
        Feed a 1-D float32 mono array.
        Returns any Timecodes decoded during this call (usually 0 or 1).
        Also invokes self.on_timecode for each decoded frame.

        Implementation: numpy bulk zero-crossing detection — only the crossing
        events (~4 000/s at 48 kHz LTC 25 fps) are processed in Python,
        instead of iterating all 48 000 samples/s individually.
        """
        results: List[Timecode] = []
        n = len(samples)
        if n == 0:
            return results

        # Sign array for this block (+1 / -1, no zeros)
        signs = np.sign(samples).astype(np.int8)
        signs[signs == 0] = 1

        # Prepend the carry-over sign from the previous block so crossings
        # at position 0 of this block are detected correctly.
        prev_s = np.int8(1 if self._prev_sign >= 0 else -1)
        extended = np.empty(n + 1, dtype=np.int8)
        extended[0] = prev_s
        extended[1:] = signs

        # Indices in `extended` where a sign change occurs.
        # extended[xi] → extended[xi+1] is a crossing; extended[xi+1] == signs[xi].
        xing_indices = np.flatnonzero(np.diff(extended))

        self._pos += n

        if len(xing_indices) == 0:
            self._samples_since_xing += n
            self._no_xing_count += n
            if self._no_xing_count > self.sample_rate // 2:
                self._signal_present = False
                if self._calibrated:
                    self._calibrated = False
                    self._calib_buf.clear()
                    self._bits.clear()
                    self._sync_reg = 0
                    self._pending_short = False
            self._prev_sign = int(signs[-1])
            return results

        # At least one crossing — signal is present
        self._signal_present = True
        self._no_xing_count = 0

        # Interval counting matches the original per-sample logic:
        #   • First crossing at extended-index xi:
        #       interval = _samples_since_xing + xi + 1
        #       (xi samples elapsed in this block before the crossing sample, +1 for it)
        #   • Subsequent crossing at xi_curr (previous at xi_prev):
        #       interval = xi_curr - xi_prev
        prev_xi = -1
        for xi in xing_indices:
            if prev_xi < 0:
                interval = self._samples_since_xing + int(xi) + 1
            else:
                interval = int(xi) - prev_xi
            prev_xi = int(xi)

            tc = self._on_crossing(interval)
            if tc is not None:
                results.append(tc)
                if self.on_timecode:
                    self.on_timecode(tc)

        # Accumulate samples after the last crossing for the next block
        self._samples_since_xing = n - prev_xi - 1
        self._prev_sign = int(signs[-1])
        return results

    def reset(self) -> None:
        """Clear decoder state (call when restarting capture)."""
        self._reset_state()

    @property
    def is_locked(self) -> bool:
        """True once the half-period EMA has stabilised."""
        return self._calibrated

    @property
    def detected_fps(self) -> Optional[float]:
        """Best-guess FPS from observed bit period, or None if not yet locked."""
        if not self._calibrated or self._half_period is None:
            return None
        return self._fps_from_half_period(self._half_period)

    @property
    def signal_present(self) -> bool:
        """True if a zero-crossing was seen within the last ~500 ms."""
        return self._signal_present

    # ── internal state ────────────────────────────────────────────────────────

    def _reset_state(self) -> None:
        # Zero-crossing tracking (persists across push_samples calls)
        self._prev_sign: int = 0              # +1 or -1
        self._samples_since_xing: int = 0    # interval counter
        self._pos: int = 0                   # global sample position

        # EMA calibration
        self._half_period: Optional[float] = None
        self._calib_buf: List[int] = []
        self._calibrated: bool = False

        # BMC state machine
        self._pending_short: bool = False    # waiting for 2nd half of a '1' bit

        # 80-bit frame accumulator (deque, oldest = index 0)
        self._bits: deque = deque(maxlen=80)

        # 16-bit sync-word shift register (newest bit at LSB)
        self._sync_reg: int = 0

        # Signal-presence tracking
        self._signal_present: bool = False
        self._no_xing_count: int = 0

        # Stale-calibration detection: counts crossings that all look like gaps
        # (interval >> half_period).  If too many pile up the calibration is wrong.
        self._consecutive_gaps: int = 0

    # ── sample-level processing ───────────────────────────────────────────────

    def _step(self, s: float) -> Optional[Timecode]:
        self._pos += 1
        self._samples_since_xing += 1

        sign = 1 if s >= 0.0 else -1

        if self._prev_sign != 0 and sign != self._prev_sign:
            interval = self._samples_since_xing
            self._samples_since_xing = 0
            self._signal_present = True
            self._no_xing_count = 0
            self._prev_sign = sign
            return self._on_crossing(interval)

        # Track signal absence (~500 ms)
        self._no_xing_count += 1
        if self._no_xing_count > self.sample_rate // 2:
            self._signal_present = False
            # Re-calibrate when signal returns; also flush stale bit state so
            # the new calibration starts from a clean shift register.
            if self._calibrated:
                self._calibrated = False
                self._calib_buf.clear()
                self._bits.clear()
                self._sync_reg = 0
                self._pending_short = False

        self._prev_sign = sign
        return None

    # ── crossing-level processing ─────────────────────────────────────────────

    def _on_crossing(self, interval: int) -> Optional[Timecode]:
        if interval < 2:        # ignore glitches
            return None

        if not self._calibrated:
            self._calibrate(interval)
            return None

        assert self._half_period is not None
        threshold = self._half_period * 1.6

        # Gap detection: interval >> full-bit period means the signal paused
        # briefly (LTC stopped/started in < 500 ms, or a buffer dropout).
        # A legitimate full-bit interval is 2 × half_period; anything beyond
        # 3 × half_period is a gap. Flush bit state but keep calibration —
        # the decoder re-aligns within one LTC frame (~40 ms at 25 fps).
        if interval > self._half_period * 3:
            self._pending_short = False
            self._bits.clear()
            self._sync_reg = 0
            # Count consecutive gap-only crossings.  If every crossing looks
            # like a gap the calibration is stale (e.g. locked on noise floor
            # before LTC started).  Force a fresh calibration.
            self._consecutive_gaps += 1
            if self._consecutive_gaps > 32:
                self._calibrated = False
                self._calib_buf.clear()
                self._consecutive_gaps = 0
            return None

        if interval <= threshold:
            # Short: half-clock pulse
            if self._pending_short:
                # Two shorts → '1' bit
                self._pending_short = False
                # Slow EMA (1 %) — prevents half_period drifting on noise
                self._half_period = self._half_period * 0.99 + interval * 0.01
                return self._push_bit(1)
            else:
                self._pending_short = True
        else:
            # Long: full-clock pulse → '0' bit
            if self._pending_short:
                # Orphaned short means bit alignment was lost (one crossing
                # was missed or spurious).  Flush the shift register so the
                # next complete 80-bit window will be a clean frame boundary.
                # Recovery happens within one LTC frame (~40 ms at 25 fps).
                self._pending_short = False
                self._bits.clear()
                self._sync_reg = 0
                # Still emit the '0' bit so we stay in phase going forward
                self._half_period = self._half_period * 0.99 + (interval * 0.5) * 0.01
                return self._push_bit(0)
            self._half_period = self._half_period * 0.99 + (interval * 0.5) * 0.01
            return self._push_bit(0)

        return None

    # ── calibration ───────────────────────────────────────────────────────────

    def _calibrate(self, interval: int) -> None:
        # Reject silence/dropout gaps (max legit interval at 24 fps/44.1 kHz ≈ 23 samples).
        if interval > 200:
            return

        self._calib_buf.append(interval)
        if len(self._calib_buf) >= _CALIB_MIN:
            intervals = sorted(self._calib_buf)

            # Robust minimum: 5th-percentile (skips 1-2 glitch outliers at the low end).
            idx_min = max(0, len(intervals) // 20)
            min_iv  = intervals[idx_min]
            max_iv  = intervals[-1]

            if max_iv > min_iv * 1.4:
                # Bimodal: short (T/2) and long (T) intervals both present.
                # Collect the short cluster using min_iv * 1.4 as the cut.
                shorts = [x for x in intervals if x < min_iv * 1.4]
                self._half_period = float(sum(shorts)) / len(shorts)
            else:
                # Unimodal: only full-bit (T) intervals visible (few '1' bits
                # in the calibration window).  Half-period = T / 2.
                mean_iv = float(sum(intervals)) / len(intervals)
                self._half_period = mean_iv / 2.0

            # Sanity check: minimum plausible half_period is ~4.6 samples
            # (60 fps at 44.1 kHz).  Smaller values mean the buffer was filled
            # with noise crossings from a silent channel — reject and retry.
            if self._half_period < 4.0:
                self._half_period = None
                self._calib_buf.clear()
                return

            self._calibrated = True
            self._calib_buf.clear()
            self._pending_short = False
            self._consecutive_gaps = 0

    # ── bit-level processing ──────────────────────────────────────────────────

    def _push_bit(self, bit: int) -> Optional[Timecode]:
        self._consecutive_gaps = 0   # valid bit received — calibration is working
        # Update 16-bit sync register: shift left, insert new bit at LSB
        self._sync_reg = ((self._sync_reg << 1) | bit) & 0xFFFF

        # Accumulate bits in deque (oldest = index 0, newest = index 79)
        self._bits.append(bit)

        # Attempt frame decode when sync word is detected and buffer is full
        if self._sync_reg == _SYNC_FWD and len(self._bits) == 80:
            return self._decode_frame()

        return None

    # ── SMPTE frame decode ────────────────────────────────────────────────────

    def _decode_frame(self) -> Optional[Timecode]:
        b = list(self._bits)   # b[0] = bit-0 (frame-units LSB), b[79] = sync MSB

        def bcd(start: int, count: int) -> int:
            """Read `count` bits starting at `start`, LSB first."""
            return sum(b[start + i] << i for i in range(count))

        try:
            frame_u  = bcd(0,  4)
            frame_t  = bcd(8,  2)
            drop_frm = bool(b[10])
            sec_u    = bcd(16, 4)
            sec_t    = bcd(24, 3)
            min_u    = bcd(32, 4)
            min_t    = bcd(40, 3)
            hr_u     = bcd(48, 4)
            hr_t     = bcd(56, 2)

            frames  = frame_u + frame_t * 10
            seconds = sec_u   + sec_t   * 10
            minutes = min_u   + min_t   * 10
            hours   = hr_u    + hr_t    * 10

            # Sanity check (rejects corrupt frames)
            if frames > 29 or seconds > 59 or minutes > 59 or hours > 23:
                return None

            fps = self._fps_from_half_period(self._half_period)  # type: ignore[arg-type]
            return Timecode(hours, minutes, seconds, frames, drop_frm, fps)

        except Exception:
            return None

    # ── FPS estimation ────────────────────────────────────────────────────────

    def _fps_from_half_period(self, half_period: float) -> float:
        # bit_period = 2 × half_period samples
        # frame_samples = 80 × bit_period
        # fps = sample_rate / frame_samples
        if half_period <= 0:
            return 25.0
        fps_raw = self.sample_rate / (80.0 * 2.0 * half_period)
        # Snap to nearest known frame rate.
        # High rates (50/60) use doubled-rate LTC: the signal runs at 2×
        # the base rate but frame numbers still encode 0–24/0–29.
        # Reliable detection at 50 fps+ requires a 96 kHz sample rate.
        known_rates = (24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)
        nearest = min(known_rates, key=lambda x: abs(fps_raw - x))
        if abs(fps_raw - nearest) < 1.0:
            return nearest

        # fps_raw doesn't match any standard rate — the configured sample_rate
        # may differ from the actual hardware rate (common with ASIO drivers
        # that report a static default before initialisation).
        # Try common sample rates; if one gives a clean standard fps, adopt it.
        for test_sr in (48000, 44100, 96000, 88200):
            if test_sr == self.sample_rate:
                continue
            test_fps = test_sr / (80.0 * 2.0 * half_period)
            test_nearest = min(known_rates, key=lambda x: abs(test_fps - x))
            if abs(test_fps - test_nearest) < 1.0:
                self.sample_rate = test_sr  # self-correct for ASIO rate mismatch
                return test_nearest

        return round(fps_raw, 2)
