"""
CueEngine — frame-accurate cue matching and LV1 snapshot recall.

All methods must be called from the main thread only (same thread that polls
the timecode queue and owns the LV1Client + UI).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from ltc_decoder import Timecode
from lv1_osc_client import LV1Client
from models import Cue, CueList
from scene_resolver import resolve


class CueEngine:
    """
    Receives Timecode objects one at a time, matches them against a CueList,
    and recalls the matching LV1 scene when a match is found.

    Fired cues are marked so they only trigger once per playback pass. When
    TC jumps backwards by more than 1 second, fired flags reset for any
    cue at or after the new TC position so they can fire again.

    Scene resolution is decoupled from firing: resolve_against_catalog() is
    called by the owner whenever the LV1 scene catalog changes. The engine
    never sends OSC to a cue with status == "MISSING" or "EMPTY".
    """

    def __init__(
        self,
        lv1_client: LV1Client,
        tolerance_frames: int = 1,
        dry_run: bool = False,
    ) -> None:
        self._lv1 = lv1_client
        self.tolerance_frames = tolerance_frames
        self.dry_run = dry_run
        self._cue_list: CueList = CueList()
        self._fps: float = 25.0
        self._last_frame: Optional[int] = None
        self._cue_frames: Dict[int, int] = {}  # cue_id → pre-computed frame number

        # Optional callbacks (called from main thread)
        self.on_cue_fired: Optional[Callable[[Cue], None]] = None
        self.on_cue_skipped: Optional[Callable[[Cue, str], None]] = None
        self.on_send_error: Optional[Callable[[str], None]] = None

    # ---- configuration -----------------------------------------------------

    def load_cue_list(self, cue_list: CueList) -> None:
        self._cue_list = cue_list
        self._cue_list.reset_fired_flags()
        self._last_frame = None
        self._recompute_cue_frames()

    def set_fps(self, fps: float) -> None:
        if fps != self._fps:
            self._fps = fps
            self._recompute_cue_frames()

    def reset(self) -> None:
        """Reset all fired flags (call on stop/rewind)."""
        self._cue_list.reset_fired_flags()
        self._last_frame = None

    def _recompute_cue_frames(self) -> None:
        """Pre-compute absolute frame numbers for every cue at the current FPS."""
        self._cue_frames = {
            c.id: c.timecode_as_frames(self._fps) for c in self._cue_list.cues
        }

    # ---- scene resolution --------------------------------------------------

    def resolve_against_catalog(self, catalog: Dict[int, str]) -> None:
        """Refresh each cue's scene_index + scene_status against a live catalog.

        Call this whenever the LV1's /Notify/SceneList changes. After this,
        cues whose scene_status is "OK" or "RECOVERED" will fire as normal;
        those with "MISSING" or "EMPTY" will be skipped (and on_cue_skipped
        called)."""
        for cue in self._cue_list.cues:
            r = resolve(cue.scene_name, cue.scene_index, catalog)
            cue.scene_status = r.status
            if r.index is not None:
                cue.scene_index = r.index

    # ---- main processing entry point ---------------------------------------

    def on_timecode(self, tc: Timecode) -> List[Cue]:
        """Process one incoming Timecode. Returns the list of cues fired this
        call (may be empty). Must be called from the main thread."""
        fps = tc.fps if tc.fps else self._fps
        current_frame = tc.to_frame_number()

        if fps != self._fps:
            self._fps = fps
            self._recompute_cue_frames()

        self._handle_backwards_jump(current_frame, fps)
        self._last_frame = current_frame

        fired: List[Cue] = []
        tol = self.tolerance_frames
        for cue in self._cue_list.cues:
            if not cue.enabled or cue.fired:
                continue
            cue_frame = self._cue_frames.get(cue.id, -1)
            if cue_frame < 0:
                continue
            if abs(current_frame - cue_frame) <= tol:
                if self._fire(cue):
                    fired.append(cue)

        return fired

    # ---- internal ----------------------------------------------------------

    def _handle_backwards_jump(self, current_frame: int, fps: float) -> None:
        """If TC jumped backwards by more than 1 second, reset fired flags
        for all cues whose timecode is >= the new position."""
        if self._last_frame is None:
            return
        jump = self._last_frame - current_frame
        if jump > round(fps):
            for cue in self._cue_list.cues:
                cue_frame = cue.timecode_as_frames(fps)
                if cue_frame >= current_frame:
                    cue.fired = False

    def _fire(self, cue: Cue) -> bool:
        """Try to send the recall. Returns True if the cue was actually fired
        (or counted as fired in dry-run); False if it was skipped."""
        cue.fired = True  # mark even on skip so we don't retry every frame

        # Refuse to send a recall for an unresolved cue.
        if cue.scene_status in ("MISSING", "EMPTY") or cue.scene_index is None:
            if self.on_cue_skipped:
                reason = (
                    "no matching LV1 scene"
                    if cue.scene_status == "MISSING"
                    else "scene index unknown"
                )
                self.on_cue_skipped(cue, reason)
            return False

        if self.dry_run:
            if self.on_cue_fired:
                self.on_cue_fired(cue)
            return True

        if not self._lv1.is_connected():
            if self.on_cue_skipped:
                self.on_cue_skipped(cue, "LV1 not connected")
            return False

        try:
            self._lv1.recall_scene(cue.scene_index)
        except Exception as exc:  # noqa: BLE001
            if self.on_send_error:
                self.on_send_error(str(exc))
            return False

        if self.on_cue_fired:
            self.on_cue_fired(cue)
        return True
