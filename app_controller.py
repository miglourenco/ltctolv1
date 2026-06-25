"""
AppController — single owner of all app state and singletons.

Sits between the UI layers (tkinter MainWindow + Flask web server) and the
domain objects (LV1Client, AudioCapture, CueEngine, CueList, DiscoveryScanner,
AppSettings). Both UI layers go through this controller for every action,
which means:

  - One source of truth for state (no diverging copies between UI and web).
  - Mutations are serialised through a single lock.
  - Event bus broadcasts state changes so any UI can subscribe and react.

Threading model:
  - Mutating methods acquire ``self._lock`` for the duration of the change.
  - The CueEngine is still effectively single-threaded: the audio poll loop
    (driven by MainWindow's tk after-loop) calls on_timecode_tick(); the
    same lock protects engine.load_cue_list() from racing with that.
  - Events are dispatched synchronously to subscribers on whatever thread
    triggered them. Subscribers are responsible for marshalling to their
    own UI thread (tk: root.after_idle; Flask SSE: per-client queue.Queue).
"""

from __future__ import annotations

import os
import queue
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from audio_capture import (
    AudioCapture,
    get_channel_names,
    list_audio_devices,
    reinit_portaudio,
)
from cue_engine import CueEngine
from ltc_decoder import Timecode
from lv1_osc_client import ConnectionState, LV1Client, SceneCatalogSnapshot
from models import RECENT_FILES_MAX, AppSettings, Cue, CueList, _norm
from scene_resolver import CueValidation, validate_all
from zdns_discover import DiscoveryEntry, DiscoveryScanner


# Event names dispatched on the bus. Subscribers receive (name, payload).
EVT_TC            = "tc"             # {"tc": "HH:MM:SS:FF", "fps": float, "signal": "LOCKED"|"AUDIO_NOT_LTC"|"NO_SIGNAL"|None}
EVT_RUNNING       = "running"        # {"running": bool, "recovering": bool, "signal": ...}
EVT_LV1_STATE     = "lv1_state"      # ConnectionState as dict
EVT_LV1_CATALOG   = "lv1_catalog"    # {"scenes": {idx: name}}
EVT_LV1_CURRENT   = "lv1_current"    # {"index": int|None, "name": str|None}
EVT_CUES          = "cues"           # {"cues": [cue_dict, ...]}
EVT_CUE_FIRED     = "cue_fired"      # {"cue_id": int, "scene_index": int, "scene_name": str, "target_tc": str, "fired_tc": str}
EVT_CUE_SKIPPED   = "cue_skipped"    # {"cue_id": int, "reason": str}
EVT_DISCOVERY     = "discovery"      # {"results": [{"host":..., "ip":..., "port":...}], "scanning": bool}
EVT_STATUS        = "status"         # {"text": str, "warn": bool}
EVT_DIRTY         = "dirty"          # {"dirty": bool, "file": str|None}
EVT_SETTINGS      = "settings"       # full settings dict
EVT_LAST_FIRE     = "last_fire"      # {"scene_index":..., "scene_name":..., "target_tc":..., "fired_tc":...} or None
EVT_RECENT        = "recent"         # {"recent_files": [{"path": str, "name": str, "exists": bool}, ...]}

# Minimum gap between two consecutive EVT_TC dispatches to subscribers.
# 100 ms ≈ 10 Hz, plenty for a remote display, and keeps SSE traffic bounded.
_TC_EVENT_MIN_INTERVAL_S = 0.1


class AppController:
    """One instance per process. Owns all domain objects and emits events."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._lock = threading.RLock()

        # ── domain singletons ────────────────────────────────────────────
        self.tc_queue: "queue.Queue[Timecode]" = queue.Queue(maxsize=200)
        self.audio = AudioCapture(self.tc_queue)
        try:
            hostname = socket.gethostname() or "unknown"
        except Exception:
            hostname = "unknown"
        self.lv1 = LV1Client(device_name=f"LTC - {hostname}")
        self.cue_list: CueList = CueList()
        self.engine = CueEngine(
            self.lv1,
            tolerance_frames=settings.tolerance_frames,
            dry_run=settings.dry_run,
        )
        self.scanner = DiscoveryScanner(timeout_s=5.0)

        # Wire engine callbacks (called from whatever thread runs the engine,
        # which is the polling thread = tk main thread).
        self.engine.on_cue_fired = self._on_engine_cue_fired
        self.engine.on_cue_skipped = self._on_engine_cue_skipped
        self.engine.on_send_error = self._on_engine_send_error

        # Wire LV1 callbacks. These fire from the LV1 reader thread.
        self.lv1.on_connection_change = self._on_lv1_connection
        self.lv1.on_catalog_change = self._on_lv1_catalog
        self.lv1.on_current_scene_change = self._on_lv1_current_scene
        self.lv1.on_log = self._on_lv1_log

        # ── live state (protected by self._lock) ─────────────────────────
        self.running: bool = False
        self.recovering: bool = False
        self.current_tc: Optional[Timecode] = None
        self.current_file: Optional[str] = None
        self.dirty: bool = False
        # "LOCKED" | "AUDIO_NOT_LTC" | "NO_SIGNAL" | None
        self.signal_state: Optional[str] = None
        self.scene_catalog: Dict[int, str] = {}
        self.lv1_current_scene: Optional[int] = None
        self.lv1_state: Optional[ConnectionState] = None
        self.discovered: List[DiscoveryEntry] = []
        self.discovery_running: bool = False
        self.last_fire: Optional[Dict[str, Any]] = None
        self.last_status: Optional[Dict[str, Any]] = None

        # Audio-device cache (populated on demand by list_audio_devices_cached).
        self._audio_devices: List[Dict[str, Any]] = []

        # Event bus
        self._subscribers: List[Callable[[str, Dict[str, Any]], None]] = []
        self._sub_lock = threading.Lock()
        self._last_tc_event_time: float = 0.0

        # Shutdown hooks. Anything registered here is called (in registration
        # order) by shutdown() during a graceful app exit. Used so MainWindow
        # can tear down the WebServer without having to import it directly.
        self._shutdown_hooks: List[Callable[[], None]] = []
        self._is_shutting_down: bool = False

    # ════════════════════════════════════════════════════════════════════
    # Event bus
    # ════════════════════════════════════════════════════════════════════

    def subscribe(self, fn: Callable[[str, Dict[str, Any]], None]) -> Callable[[], None]:
        """Add an event subscriber. Returns an unsubscribe function."""
        with self._sub_lock:
            self._subscribers.append(fn)

        def _unsub() -> None:
            with self._sub_lock:
                try:
                    self._subscribers.remove(fn)
                except ValueError:
                    pass

        return _unsub

    def _emit(self, name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        payload = payload or {}
        with self._sub_lock:
            subs = list(self._subscribers)
        for s in subs:
            try:
                s(name, payload)
            except Exception as exc:  # noqa: BLE001
                # Never let a misbehaving subscriber take down the emitter.
                print(f"[controller] subscriber raised: {exc}")

    # ════════════════════════════════════════════════════════════════════
    # State snapshot — single source of truth for "what does the UI show?"
    # ════════════════════════════════════════════════════════════════════

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            lv1s = self.lv1_state or self.lv1.connection_state()
            return {
                "version": _version_string(),
                "running": self.running,
                "recovering": self.recovering,
                "signal": self.signal_state,
                "current_tc": str(self.current_tc) if self.current_tc else None,
                "fps": self.audio.detected_fps,
                "lv1": {
                    "connected": bool(lv1s.connected),
                    "registered": bool(lv1s.registered),
                    "host": lv1s.host,
                    "port": lv1s.port,
                    "last_error": lv1s.last_error,
                },
                "lv1_current_scene": self.lv1_current_scene,
                "lv1_current_scene_name": (
                    self.scene_catalog.get(self.lv1_current_scene)
                    if self.lv1_current_scene is not None else None
                ),
                "scenes": [
                    {"index": i, "name": self.scene_catalog[i]}
                    for i in sorted(self.scene_catalog)
                ],
                "cues": [self.cue_to_dict(c) for c in self.cue_list.cues],
                "current_file": self.current_file,
                "dirty": self.dirty,
                "last_fire": self.last_fire,
                "last_status": self.last_status,
                "recent_files": self._recent_files_payload(),
                "discovery": {
                    "scanning": self.discovery_running,
                    "results": [
                        {
                            "host": r.host or "",
                            "ip": (r.addresses[0] if r.addresses else ""),
                            "port": r.port or 0,
                        }
                        for r in self.discovered
                    ],
                },
                "settings": _settings_dict(self.settings),
            }

    @staticmethod
    def cue_to_dict(c: Cue) -> Dict[str, Any]:
        return {
            "id": c.id,
            "label": c.label,
            "timecode": c.timecode,
            "scene_name": c.scene_name,
            "scene_index": c.scene_index,
            "enabled": c.enabled,
            "fired": c.fired,
            "scene_status": c.scene_status,
        }

    # ════════════════════════════════════════════════════════════════════
    # Audio device enumeration
    # ════════════════════════════════════════════════════════════════════

    def refresh_audio_devices(self) -> List[Dict[str, Any]]:
        reinit_portaudio()
        with self._lock:
            self._audio_devices = list_audio_devices()
            return list(self._audio_devices)

    def audio_devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            if not self._audio_devices:
                self._audio_devices = list_audio_devices()
            return list(self._audio_devices)

    def channel_names(self, device_index: int) -> List[str]:
        devs = self.audio_devices()
        for d in devs:
            if d["index"] == device_index:
                n_ch = int(d.get("channels", 1))
                names = get_channel_names(device_index, n_ch, d.get("hostapi", ""))
                if not names:
                    names = [f"Ch {i + 1}" for i in range(n_ch)]
                return names
        return []

    # ════════════════════════════════════════════════════════════════════
    # Audio capture lifecycle
    # ════════════════════════════════════════════════════════════════════

    def start_capture(
        self,
        device_index: int,
        channel_zero_based: int,
        sample_rate: int,
        block_size: int,
        device_label: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Configure and start the audio stream. Returns (ok, error_msg)."""
        with self._lock:
            if self.running:
                return True, None
            try:
                self.audio.configure(device_index, channel_zero_based, sample_rate, block_size)
                self.audio.start()
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
            self.running = True
            self.recovering = False
            self.signal_state = None
            # Persist the choices into settings (for next session).
            if device_label:
                self.settings.audio_device = device_label
            self.settings.audio_channel = channel_zero_based + 1
            self.settings.sample_rate = sample_rate
            self.settings.block_size = block_size
        self._emit(EVT_RUNNING, {
            "running": True,
            "recovering": False,
            "signal": None,
        })
        return True, None

    def stop_capture(self) -> None:
        with self._lock:
            if not self.running:
                return
            try:
                self.audio.stop()
            except Exception:
                pass
            self.running = False
            self.recovering = False
            self.signal_state = None
            self.current_tc = None
        self._emit(EVT_RUNNING, {
            "running": False,
            "recovering": False,
            "signal": None,
        })

    def set_recovering(self, recovering: bool) -> None:
        with self._lock:
            self.recovering = recovering
            if recovering:
                self.signal_state = None
        self._emit(EVT_RUNNING, {
            "running": self.running,
            "recovering": self.recovering,
            "signal": self.signal_state,
        })

    # ════════════════════════════════════════════════════════════════════
    # TC processing (called from the polling loop, ~25 Hz)
    # ════════════════════════════════════════════════════════════════════

    def drain_tc_queue(self) -> Optional[Timecode]:
        """Pull every queued Timecode through the engine. Returns the last
        TC, or None if nothing was processed. Safe to call only from the
        same thread every time (the engine is not multi-threaded)."""
        latest: Optional[Timecode] = None
        try:
            while True:
                tc = self.tc_queue.get_nowait()
                with self._lock:
                    self.engine.on_timecode(tc)
                    self.engine.set_fps(tc.fps)
                latest = tc
        except queue.Empty:
            pass
        if latest is not None:
            with self._lock:
                self.current_tc = latest
            self._maybe_emit_tc()
        return latest

    def update_signal_state(self, new_state: Optional[str]) -> bool:
        """Update the LTC signal state. Returns True if it changed."""
        with self._lock:
            if new_state == self.signal_state:
                return False
            self.signal_state = new_state
        self._maybe_emit_tc()
        return True

    def _maybe_emit_tc(self) -> None:
        """Throttle EVT_TC to ~10 Hz to keep SSE traffic bounded."""
        now = time.monotonic()
        if now - self._last_tc_event_time < _TC_EVENT_MIN_INTERVAL_S:
            return
        self._last_tc_event_time = now
        with self._lock:
            payload = {
                "tc": str(self.current_tc) if self.current_tc else None,
                "fps": self.audio.detected_fps,
                "signal": self.signal_state,
            }
        self._emit(EVT_TC, payload)

    # ════════════════════════════════════════════════════════════════════
    # LV1 connect / disconnect / recall
    # ════════════════════════════════════════════════════════════════════

    def lv1_connect(self, host: str, port: int) -> None:
        with self._lock:
            self.lv1.auto_reconnect = True
            self.lv1.connect(host, port)
            self.settings.lv1_host = host
            self.settings.lv1_port = port

    def lv1_disconnect_async(self) -> None:
        """Disconnect on a worker thread — disconnect() joins the reader
        thread, which can block for up to 2 s."""
        self.lv1.auto_reconnect = False
        threading.Thread(
            target=self.lv1.disconnect,
            name="LV1DisconnectWorker",
            daemon=True,
        ).start()

    def lv1_recall_scene(self, index: int) -> Tuple[bool, Optional[str]]:
        """Returns (ok, err). Refuses if not connected."""
        if not self.lv1.is_connected():
            return False, "LV1 not connected"
        try:
            self.lv1.recall_scene(int(index))
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, None

    def resolve_target(self, manual_host: str, manual_port: int) -> Optional[Tuple[str, int]]:
        """Resolve the connection target the same way the tk UI does:
        manual host+port wins, else look up port in discovery cache."""
        if manual_host and manual_port > 0:
            return manual_host, manual_port
        if manual_host:
            with self._lock:
                for r in self.discovered:
                    if manual_host in r.addresses and r.port:
                        return manual_host, r.port
            return None
        # Fall back to first discovery result (UI passes its own selection
        # explicitly when it has one — this branch covers "any LV1").
        with self._lock:
            for r in self.discovered:
                ip = r.addresses[0] if r.addresses else ""
                if ip and r.port:
                    return ip, r.port
        return None

    # ── LV1 callbacks (from reader thread) ──────────────────────────────

    def _on_lv1_connection(self, state: ConnectionState) -> None:
        with self._lock:
            self.lv1_state = state
        self._emit(EVT_LV1_STATE, {
            "connected": bool(state.connected),
            "registered": bool(state.registered),
            "host": state.host,
            "port": state.port,
            "last_error": state.last_error,
        })

    def _on_lv1_catalog(self, snap: SceneCatalogSnapshot) -> None:
        with self._lock:
            self.scene_catalog = dict(snap.scenes)
            self.engine.resolve_against_catalog(self.scene_catalog)
        self._emit(EVT_LV1_CATALOG, {
            "scenes": [
                {"index": i, "name": self.scene_catalog[i]}
                for i in sorted(self.scene_catalog)
            ],
        })
        # Cue statuses changed too — push the new cue snapshot.
        self._emit_cues()

    def _on_lv1_current_scene(self, idx: Optional[int]) -> None:
        with self._lock:
            self.lv1_current_scene = idx
            name = self.scene_catalog.get(idx) if idx is not None else None
        self._emit(EVT_LV1_CURRENT, {"index": idx, "name": name})

    def _on_lv1_log(self, level: str, msg: str) -> None:
        # Forward warn/error to the status bar; info just goes to stdout.
        if level in ("warn", "warning", "error"):
            self.set_status(msg, warn=True)
        else:
            print(f"[lv1] {msg}")

    # ════════════════════════════════════════════════════════════════════
    # Engine callbacks
    # ════════════════════════════════════════════════════════════════════

    def _on_engine_cue_fired(self, cue: Cue) -> None:
        with self._lock:
            cur = str(self.current_tc) if self.current_tc else "??:??:??:??"
            self.last_fire = {
                "cue_id": cue.id,
                "scene_index": cue.scene_index,
                "scene_name": cue.scene_name or cue.label,
                "target_tc": cue.timecode,
                "fired_tc": cur,
            }
        self._emit(EVT_CUE_FIRED, dict(self.last_fire))
        self._emit(EVT_LAST_FIRE, dict(self.last_fire))
        # The cue's `.fired` flag changed → push cue snapshot for badges.
        self._emit_cues()

    def _on_engine_cue_skipped(self, cue: Cue, reason: str) -> None:
        self._emit(EVT_CUE_SKIPPED, {"cue_id": cue.id, "reason": reason})
        self.set_status(f"Skipped '{cue.label}': {reason}", warn=True)

    def _on_engine_send_error(self, msg: str) -> None:
        self.set_status(f"LV1 send error: {msg}", warn=True)

    # ════════════════════════════════════════════════════════════════════
    # Cue list operations (mutating — all hold the lock + emit EVT_CUES)
    # ════════════════════════════════════════════════════════════════════

    def add_cue(
        self,
        label: str,
        timecode: str,
        scene_name: str = "",
        scene_index: Optional[int] = None,
        enabled: bool = True,
    ) -> Cue:
        with self._lock:
            cue = self.cue_list.add(
                label=label,
                timecode=timecode,
                scene_name=scene_name,
                scene_index=scene_index,
            )
            cue.enabled = enabled
            self.engine.load_cue_list(self.cue_list)
            if self.scene_catalog:
                self.engine.resolve_against_catalog(self.scene_catalog)
            self._mark_dirty_locked()
        self._emit_cues()
        return cue

    def update_cue(self, cue_id: int, **fields: Any) -> bool:
        with self._lock:
            ok = self.cue_list.replace(cue_id, **fields)
            if not ok:
                return False
            self.engine.load_cue_list(self.cue_list)
            if self.scene_catalog:
                self.engine.resolve_against_catalog(self.scene_catalog)
            self._mark_dirty_locked()
        self._emit_cues()
        return True

    def remove_cue(self, cue_id: int) -> bool:
        with self._lock:
            ok = self.cue_list.remove(cue_id)
            if not ok:
                return False
            self.engine.load_cue_list(self.cue_list)
            self._mark_dirty_locked()
        self._emit_cues()
        return True

    def move_cue_up(self, cue_id: int) -> bool:
        with self._lock:
            if not self.cue_list.move_up(cue_id):
                return False
            self.engine.load_cue_list(self.cue_list)
            self._mark_dirty_locked()
        self._emit_cues()
        return True

    def move_cue_down(self, cue_id: int) -> bool:
        with self._lock:
            if not self.cue_list.move_down(cue_id):
                return False
            self.engine.load_cue_list(self.cue_list)
            self._mark_dirty_locked()
        self._emit_cues()
        return True

    def toggle_cue_enabled(self, cue_id: int) -> Optional[bool]:
        """Returns the new enabled value, or None if cue not found."""
        with self._lock:
            cue = self.cue_list.by_id(cue_id)
            if cue is None:
                return None
            cue.enabled = not cue.enabled
            new_val = cue.enabled
            self._mark_dirty_locked()
        self._emit_cues()
        return new_val

    def tap_at_current_tc(self) -> Optional[Cue]:
        """Add a cue at the current TC. Returns the new cue or None."""
        with self._lock:
            if self.current_tc is None:
                return None
            tc_str = str(self.current_tc)
            new = self.cue_list.add(
                label=f"Cue {len(self.cue_list) + 1}",
                timecode=tc_str,
            )
            self.engine.load_cue_list(self.cue_list)
            if self.scene_catalog:
                self.engine.resolve_against_catalog(self.scene_catalog)
            self._mark_dirty_locked()
        self._emit_cues()
        return new

    def assign_scene_to_cue(self, cue_id: int, scene_index: int) -> bool:
        """Drag-equivalent: assign a scene catalog entry to a cue."""
        with self._lock:
            cue = self.cue_list.by_id(cue_id)
            if cue is None:
                return False
            scene_name = self.scene_catalog.get(scene_index, "")
            self.cue_list.replace(
                cue_id,
                scene_name=scene_name,
                scene_index=scene_index,
            )
            self.engine.load_cue_list(self.cue_list)
            if self.scene_catalog:
                self.engine.resolve_against_catalog(self.scene_catalog)
            self._mark_dirty_locked()
        self._emit_cues()
        self.set_status(f"Cue '{cue.label}' → scene [{scene_index}] {scene_name}")
        return True

    def test_fire_cue(self, cue_id: int) -> Tuple[bool, Optional[str]]:
        """Manual recall via the UI 'Test' button. Returns (ok, err)."""
        with self._lock:
            cue = self.cue_list.by_id(cue_id)
            if cue is None:
                return False, "cue not found"
            if cue.scene_index is None or cue.scene_status in ("MISSING", "EMPTY"):
                return False, f"Cue '{cue.label}' has no resolved LV1 scene."
            dry = self.engine.dry_run
            idx = cue.scene_index
            name = cue.scene_name
            label = cue.label
        if dry:
            self._on_lv1_log("info", f"[dry-run] would recall scene {idx}")
            return True, None
        if not self.lv1.is_connected():
            return False, "Connect to the LV1 first."
        try:
            self.lv1.recall_scene(idx)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        self.set_status(f"Test fire: scene {idx} → {name or label}")
        return True, None

    def reset_fired(self) -> None:
        with self._lock:
            self.engine.reset()
        self._emit_cues()

    def revalidate(self) -> List[CueValidation]:
        """Re-resolve every cue against the current catalog. Returns the
        validation results (caller can surface warnings to the UI)."""
        with self._lock:
            if not self.scene_catalog:
                return []
            self.engine.resolve_against_catalog(self.scene_catalog)
            results = validate_all(self.cue_list.cues, self.scene_catalog)
        self._emit_cues()
        return results

    def validate(self) -> List[CueValidation]:
        with self._lock:
            return validate_all(self.cue_list.cues, self.scene_catalog)

    # ════════════════════════════════════════════════════════════════════
    # File ops
    # ════════════════════════════════════════════════════════════════════

    def new_cue_list(self) -> None:
        with self._lock:
            self.cue_list = CueList()
            self.current_file = None
            self.engine.load_cue_list(self.cue_list)
            self._clear_dirty_locked()
        self._emit_cues()

    def load_cue_file(self, path: str) -> Tuple[bool, Optional[str], bool]:
        """Returns (ok, err, was_migrated_from_midi)."""
        try:
            was_midi = CueList.was_migrated_from_midi(path)
            cl = CueList.load(path)
        except Exception as exc:  # noqa: BLE001
            # If a recent-files entry is the one that failed, prune it so the
            # menu doesn't keep offering dead links.
            self._prune_recent(path)
            return False, str(exc), False
        with self._lock:
            self.cue_list = cl
            self.current_file = path
            self.settings.last_cue_file = path
            self.engine.load_cue_list(self.cue_list)
            if self.scene_catalog:
                self.engine.resolve_against_catalog(self.scene_catalog)
            self._clear_dirty_locked()
        self.add_recent(path)
        self._emit_cues()
        return True, None, was_midi

    def save_cue_file(self, path: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """Save to ``path``, or to current_file if None. Returns (ok, err)."""
        with self._lock:
            target = path or self.current_file
            if not target:
                return False, "No file path"
            try:
                self.cue_list.save(target)
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
            self.current_file = target
            self.settings.last_cue_file = target
            self._clear_dirty_locked()
        self.add_recent(target)
        self.set_status(f"Saved: {os.path.basename(target)}")
        return True, None

    # ────────────────────────────────────────────────────────────────────
    # Recent files
    # ────────────────────────────────────────────────────────────────────

    def add_recent(self, path: str) -> None:
        """Push ``path`` to the front of the recent-files list, deduping
        case-insensitively on Windows. Truncated to RECENT_FILES_MAX."""
        if not path:
            return
        norm_key = _norm(path)
        with self._lock:
            cur = list(self.settings.recent_files or [])
            cur = [p for p in cur if _norm(p) != norm_key]
            cur.insert(0, path)
            cur = cur[:RECENT_FILES_MAX]
            self.settings.recent_files = cur
        self._emit(EVT_RECENT, {"recent_files": self._recent_files_payload()})

    def clear_recent(self) -> None:
        with self._lock:
            self.settings.recent_files = []
        self._emit(EVT_RECENT, {"recent_files": []})

    def _prune_recent(self, path: str) -> None:
        """Remove a single entry from the recent list (used after a load
        fails — the file likely moved or was deleted)."""
        norm_key = _norm(path)
        with self._lock:
            cur = [p for p in (self.settings.recent_files or []) if _norm(p) != norm_key]
            if cur == self.settings.recent_files:
                return
            self.settings.recent_files = cur
        self._emit(EVT_RECENT, {"recent_files": self._recent_files_payload()})

    def _recent_files_payload(self) -> List[Dict[str, Any]]:
        """Snapshot-friendly view of the recent list — adds ``name`` (basename)
        and ``exists`` so the UI can show stale entries differently."""
        out: List[Dict[str, Any]] = []
        for p in (self.settings.recent_files or []):
            try:
                exists = bool(p) and os.path.isfile(p)
            except OSError:
                exists = False
            out.append({"path": p, "name": os.path.basename(p) or p, "exists": exists})
        return out

    def load_cue_list_from_data(self, data: List[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        """Replace the current cue list from a raw JSON-decoded list. Used by
        the web /api/cues/upload endpoint."""
        try:
            cl = CueList()
            cl.cues = [Cue.from_dict(d) for d in data]
            cl._next_id = max((c.id for c in cl.cues), default=0) + 1
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        with self._lock:
            self.cue_list = cl
            self.current_file = None
            self.engine.load_cue_list(self.cue_list)
            if self.scene_catalog:
                self.engine.resolve_against_catalog(self.scene_catalog)
            self._mark_dirty_locked()
        self._emit_cues()
        return True, None

    # ════════════════════════════════════════════════════════════════════
    # Discovery
    # ════════════════════════════════════════════════════════════════════

    def start_discovery(self) -> bool:
        """Kick off an LV1 LAN scan. Returns False if one is already running."""
        if self.scanner.is_running:
            return False
        with self._lock:
            self.discovery_running = True
        self.set_status("Scanning for LV1s…")
        self._emit(EVT_DISCOVERY, {"scanning": True, "results": []})
        self.scanner.start(on_complete=self._on_discovery_done)
        return True

    def _on_discovery_done(self, results: List[DiscoveryEntry]) -> None:
        with self._lock:
            self.discovered = results
            self.discovery_running = False
        out = [
            {
                "host": r.host or "",
                "ip": (r.addresses[0] if r.addresses else ""),
                "port": r.port or 0,
            }
            for r in results
        ]
        self._emit(EVT_DISCOVERY, {"scanning": False, "results": out})
        self.set_status(f"Discovery: {len(results)} LV1{'s' if len(results) != 1 else ''} found")

    # ════════════════════════════════════════════════════════════════════
    # Settings
    # ════════════════════════════════════════════════════════════════════

    def set_tolerance(self, frames: int) -> None:
        with self._lock:
            self.engine.tolerance_frames = frames
            self.settings.tolerance_frames = frames
        self._emit(EVT_SETTINGS, _settings_dict(self.settings))

    def set_dry_run(self, on: bool) -> None:
        with self._lock:
            self.engine.dry_run = on
            self.settings.dry_run = on
        self._emit(EVT_SETTINGS, _settings_dict(self.settings))
        self.set_status("Dry-run ON" if on else "Dry-run OFF")

    def update_settings(self, **kw: Any) -> None:
        """Bulk-update settings fields (anything in AppSettings.__dataclass_fields__)."""
        with self._lock:
            fields = type(self.settings).__dataclass_fields__
            for k, v in kw.items():
                if k in fields:
                    setattr(self.settings, k, v)
        # Side-effects for engine-affecting fields
        if "tolerance_frames" in kw:
            with self._lock:
                self.engine.tolerance_frames = int(self.settings.tolerance_frames)
        if "dry_run" in kw:
            with self._lock:
                self.engine.dry_run = bool(self.settings.dry_run)
        self._emit(EVT_SETTINGS, _settings_dict(self.settings))

    def save_settings(self) -> None:
        with self._lock:
            try:
                self.settings.save()
            except Exception:
                pass

    # ════════════════════════════════════════════════════════════════════
    # Shutdown
    # ════════════════════════════════════════════════════════════════════

    def add_shutdown_hook(self, fn: Callable[[], None]) -> None:
        """Register a callable to be invoked by shutdown(). Used to plug in
        the WebServer's stop() so it gets a graceful close."""
        self._shutdown_hooks.append(fn)

    def shutdown(self) -> None:
        """Wind down the controller: stop audio + LV1 + every shutdown hook
        (web server, etc). Safe to call more than once."""
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        # Stop hooks first so they unsubscribe before we tear down state.
        for hook in self._shutdown_hooks:
            try:
                hook()
            except Exception:
                pass
        try:
            self.stop_capture()
        except Exception:
            pass
        try:
            self.lv1.disconnect()
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════════
    # Status + dirty
    # ════════════════════════════════════════════════════════════════════

    def set_status(self, text: str, warn: bool = False) -> None:
        with self._lock:
            self.last_status = {"text": text, "warn": bool(warn)}
        self._emit(EVT_STATUS, dict(self.last_status))

    def _mark_dirty_locked(self) -> None:
        if not self.dirty:
            self.dirty = True
            self._emit(EVT_DIRTY, {"dirty": True, "file": self.current_file})

    def _clear_dirty_locked(self) -> None:
        if self.dirty:
            self.dirty = False
        # Always emit so the file label refreshes on Save / Open / New
        self._emit(EVT_DIRTY, {"dirty": False, "file": self.current_file})

    def mark_dirty(self) -> None:
        with self._lock:
            self._mark_dirty_locked()

    # ════════════════════════════════════════════════════════════════════
    # Convenience: emit cue snapshot
    # ════════════════════════════════════════════════════════════════════

    def _emit_cues(self) -> None:
        with self._lock:
            payload = {"cues": [self.cue_to_dict(c) for c in self.cue_list.cues]}
        self._emit(EVT_CUES, payload)


# ───── Helpers ──────────────────────────────────────────────────────────


def _settings_dict(s: AppSettings) -> Dict[str, Any]:
    from dataclasses import asdict
    return asdict(s)


def _version_string() -> str:
    # Imported lazily to avoid a circular import (main_window holds _VERSION).
    try:
        from main_window import _VERSION
        return _VERSION
    except Exception:
        return "0.0.0"
