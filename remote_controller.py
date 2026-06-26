"""
RemoteAppController — HTTP proxy that behaves like AppController.

Lives on the operator's PC. Talks to a host LTCtoLV1 over its REST + SSE
endpoints (the same ones the web remote uses), and re-emits everything on
a local event bus identical to AppController's, so MainWindow can run
unchanged against either controller.

What's NOT exposed in remote mode:
  - The .audio attribute is a stub that reports state inferred from SSE
    events (signal_present, is_locked, detected_fps). stream_active is
    always True so MainWindow's driver-reset path doesn't fire.
  - The .lv1 attribute is a stub with is_connected() — same reasoning.
  - drain_tc_queue() is a no-op; TC arrives via SSE instead of a queue.

Threading
---------
- SSE reader runs on its own daemon thread; auto-reconnects with backoff.
- REST calls are short and synchronous; we tolerate them blocking on the
  caller's thread (usually the tk thread via MainWindow handlers).
- Events are emitted on the SSE thread; MainWindow already marshals back
  to the tk main loop via after_idle, so no extra wrapping needed here.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from models import AppSettings, Cue, CueList
from scene_resolver import CueValidation, validate_all


# Re-use AppController's event names so MainWindow's subscribers don't
# need to know which controller is on the other end of the bus.
from app_controller import (  # noqa: F401  (re-exported for callers)
    EVT_CUES,
    EVT_CUE_FIRED,
    EVT_CUE_SKIPPED,
    EVT_DIRTY,
    EVT_DISCOVERY,
    EVT_LAST_FIRE,
    EVT_LV1_CATALOG,
    EVT_LV1_CURRENT,
    EVT_LV1_STATE,
    EVT_RECENT,
    EVT_RUNNING,
    EVT_SETTINGS,
    EVT_STATUS,
    EVT_TC,
)


# Extra event names specific to remote mode.
EVT_REMOTE_CONNECTED    = "remote_connected"     # {"host": ..., "port": ..., "version": ...}
EVT_REMOTE_DISCONNECTED = "remote_disconnected"  # {"reason": ...}


# ─── Connection-state stubs (mimic real LV1Client / AudioCapture) ───────


class _LV1StateStub:
    """Minimal facade so MainWindow's `self.ctl.lv1.is_connected()` and
    similar property reads work without breaking."""

    def __init__(self, parent: "RemoteAppController") -> None:
        self._parent = parent

    def is_connected(self) -> bool:
        return bool(self._parent._lv1_registered)

    def recall_scene(self, idx: int) -> None:
        # MainWindow only calls this on the local controller's lv1 directly
        # via the LV1Client; for remote we route through the controller
        # method, which is what double-click in the catalog uses.
        self._parent.lv1_recall_scene(int(idx))

    def disconnect(self) -> None:
        self._parent.lv1_disconnect_async()


class _AudioStub:
    """Mirrors the AudioCapture properties MainWindow's poll loop reads."""

    def __init__(self, parent: "RemoteAppController") -> None:
        self._parent = parent

    @property
    def is_locked(self) -> bool:
        return self._parent.signal_state == "LOCKED"

    @property
    def signal_present(self) -> bool:
        return self._parent.signal_state in ("LOCKED", "AUDIO_NOT_LTC")

    @property
    def detected_fps(self) -> Optional[float]:
        return self._parent._fps

    @property
    def stream_active(self) -> bool:
        # Server-side concern; always claim alive so MainWindow doesn't
        # try the driver-reset recovery dance against a remote host.
        return True

    @property
    def callback_stalled(self) -> bool:
        return False

    def stop(self) -> None:
        # Defensive — MainWindow occasionally calls audio.stop() during
        # tear-down. Route the intent through the proper REST endpoint.
        self._parent.stop_capture()


# ─── Remote cue list — mirrors host's cue list from SSE ─────────────────


class _RemoteCueList(CueList):
    """Subclass that rebuilds itself from snapshot dicts pushed via SSE.
    All mutating methods still exist (so MainWindow can call them) but
    they're inert — actual mutations happen via REST and propagate back
    via the SSE cues event."""

    def replace_all(self, cue_dicts: List[Dict[str, Any]]) -> None:
        cues: List[Cue] = []
        for d in cue_dicts:
            c = Cue(
                id=int(d.get("id", 0)),
                label=str(d.get("label", "")),
                timecode=str(d.get("timecode", "00:00:00:00")),
                scene_name=str(d.get("scene_name", "")),
                scene_index=d.get("scene_index"),
                enabled=bool(d.get("enabled", True)),
            )
            c.fired = bool(d.get("fired", False))
            c.scene_status = str(d.get("scene_status", "EMPTY"))
            cues.append(c)
        self.cues = cues


# ─── The controller ─────────────────────────────────────────────────────


class RemoteAppController:
    """Drop-in replacement for AppController, backed by HTTP/SSE."""

    # MainWindow probes this to decide whether to hide local-only widgets.
    is_remote: bool = True

    def __init__(self, settings: AppSettings, host: str, port: int) -> None:
        self.settings = settings
        self._host = host
        self._port = port
        self._base = f"http://{host}:{port}"

        self._lock = threading.RLock()

        # ── Mirrored state (populated from /api/state + SSE) ─────────────
        self.running: bool = False
        self.recovering: bool = False
        self.current_tc: Optional[Any] = None     # we store the string only
        self.signal_state: Optional[str] = None
        self._fps: Optional[float] = None
        self.scene_catalog: Dict[int, str] = {}
        self.lv1_current_scene: Optional[int] = None
        self._lv1_registered: bool = False
        self._lv1_connected: bool = False
        self._lv1_host: Optional[str] = None
        self._lv1_port: Optional[int] = None
        self._lv1_last_error: Optional[str] = None
        self.discovered: List[Any] = []          # opaque entries (host owns ranking)
        self.discovery_running: bool = False
        self.last_fire: Optional[Dict[str, Any]] = None
        self.last_status: Optional[Dict[str, Any]] = None
        self.cue_list: _RemoteCueList = _RemoteCueList()
        self.current_file: Optional[str] = None
        self.dirty: bool = False
        self._version: Optional[str] = None

        # ── MainWindow-facing facades ────────────────────────────────────
        self.lv1 = _LV1StateStub(self)
        self.audio = _AudioStub(self)

        # No engine / scanner / queue exist on the client side — but
        # provide attribute placeholders in case some code path probes
        # them defensively.
        self.engine = None
        self.scanner = None
        self.tc_queue: "queue.Queue[Any]" = queue.Queue(maxsize=1)

        # ── Event bus + shutdown plumbing ────────────────────────────────
        self._subscribers: List[Callable[[str, Dict[str, Any]], None]] = []
        self._sub_lock = threading.Lock()
        self._shutdown_hooks: List[Callable[[], None]] = []
        self._is_shutting_down: bool = False

        # SSE reader thread state
        self._sse_thread: Optional[threading.Thread] = None
        self._sse_stop = threading.Event()
        self._sse_resp = None
        self._sse_connected = False
        self._cached_audio_devices: List[Dict[str, Any]] = []

    # ════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ════════════════════════════════════════════════════════════════════

    def start(self) -> Tuple[bool, Optional[str]]:
        """Pull initial /api/state then spawn the SSE reader. Returns
        (ok, error_msg). Failure here means the host is unreachable."""
        try:
            snap = self._get("/api/state", timeout=4.0)
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't reach host: {exc}"
        self._apply_snapshot(snap)
        # Persist target so the next launch can reconnect automatically.
        self.settings.remote_host = self._host
        self.settings.remote_port = self._port
        try:
            self.settings.save()
        except Exception:
            pass
        self._sse_thread = threading.Thread(
            target=self._sse_loop, name="RemoteSSE", daemon=True
        )
        self._sse_thread.start()
        self._emit(EVT_REMOTE_CONNECTED, {
            "host": self._host, "port": self._port, "version": self._version,
        })
        return True, None

    def shutdown(self) -> None:
        """Best-effort fast teardown: stop the SSE reader, close the
        long-lived HTTP response, and briefly wait for the reader thread
        to exit. The reader is a daemon thread so even if it overruns the
        join, the process exit will reap it — but a short join lets us
        flush any in-flight subscriber callbacks cleanly."""
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        for hook in self._shutdown_hooks:
            try:
                hook()
            except Exception:
                pass
        self._sse_stop.set()
        try:
            if self._sse_resp is not None:
                self._sse_resp.close()
                self._sse_resp = None
        except Exception:
            pass
        if self._sse_thread and self._sse_thread.is_alive():
            self._sse_thread.join(timeout=0.5)

    def add_shutdown_hook(self, fn: Callable[[], None]) -> None:
        self._shutdown_hooks.append(fn)

    def save_settings(self) -> None:
        try:
            self.settings.save()
        except Exception:
            pass

    def start_lan_announcer(self) -> None:
        # Only the host advertises itself — a remote control wouldn't make
        # sense as a discovery target. Present as a no-op so callers don't
        # have to special-case the controller type.
        pass

    # ════════════════════════════════════════════════════════════════════
    # Event bus (identical signature to AppController.subscribe)
    # ════════════════════════════════════════════════════════════════════

    def subscribe(self, fn: Callable[[str, Dict[str, Any]], None]) -> Callable[[], None]:
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
                print(f"[remote] subscriber raised: {exc}")

    # ════════════════════════════════════════════════════════════════════
    # Compatibility shims — MainWindow accesses these directly
    # ════════════════════════════════════════════════════════════════════

    @property
    def lv1_state(self):
        """Mirror AppController.lv1_state which is a ConnectionState
        dataclass populated by the LV1 reader thread. MainWindow probes
        .connected, .registered, .host, .port and .last_error on it."""
        from lv1_osc_client import ConnectionState
        return ConnectionState(
            connected=self._lv1_connected,
            registered=self._lv1_registered,
            last_error=self._lv1_last_error,
            host=self._lv1_host,
            port=self._lv1_port,
        )

    # ════════════════════════════════════════════════════════════════════
    # Snapshot (used by MainWindow's tray repaint path)
    # ════════════════════════════════════════════════════════════════════

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": self._version,
                "running": self.running,
                "recovering": self.recovering,
                "signal": self.signal_state,
                "current_tc": self.current_tc,
                "fps": self._fps,
                "lv1": {
                    "connected": self._lv1_connected,
                    "registered": self._lv1_registered,
                    "host": self._lv1_host,
                    "port": self._lv1_port,
                    "last_error": self._lv1_last_error,
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
                "cues": [self._cue_dict(c) for c in self.cue_list.cues],
                "current_file": self.current_file,
                "dirty": self.dirty,
                "last_fire": self.last_fire,
                "last_status": self.last_status,
                "discovery": {"scanning": self.discovery_running, "results": []},
                "settings": {},
            }

    # Kept for parity with AppController.cue_to_dict; web_server uses it on
    # the host side but might call on the controller through duck-typed code.
    @staticmethod
    def cue_to_dict(c: Cue) -> Dict[str, Any]:
        return RemoteAppController._cue_dict(c)

    @staticmethod
    def _cue_dict(c: Cue) -> Dict[str, Any]:
        return {
            "id": c.id, "label": c.label, "timecode": c.timecode,
            "scene_name": c.scene_name, "scene_index": c.scene_index,
            "enabled": c.enabled, "fired": c.fired,
            "scene_status": c.scene_status,
        }

    # ════════════════════════════════════════════════════════════════════
    # Audio device introspection (proxied)
    # ════════════════════════════════════════════════════════════════════

    def refresh_audio_devices(self) -> List[Dict[str, Any]]:
        try:
            r = self._post("/api/audio/refresh", {})
            self._cached_audio_devices = list(r.get("devices") or [])
        except Exception:
            self._cached_audio_devices = []
        return list(self._cached_audio_devices)

    def audio_devices(self) -> List[Dict[str, Any]]:
        if not self._cached_audio_devices:
            try:
                r = self._get("/api/audio/devices")
                self._cached_audio_devices = list(r.get("devices") or [])
            except Exception:
                pass
        return list(self._cached_audio_devices)

    def channel_names(self, device_index: int) -> List[str]:
        try:
            r = self._get(f"/api/audio/channels?device_index={int(device_index)}")
            return list(r.get("channels") or [])
        except Exception:
            return []

    # ════════════════════════════════════════════════════════════════════
    # Capture lifecycle (proxied)
    # ════════════════════════════════════════════════════════════════════

    def start_capture(self, device_index: int, channel_zero_based: int,
                      sample_rate: int, block_size: int,
                      device_label: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        try:
            r = self._post("/api/run/start", {
                "device_index": int(device_index),
                "channel": int(channel_zero_based) + 1,
                "sample_rate": int(sample_rate),
                "block_size": int(block_size),
                "device_label": device_label,
            })
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return bool(r.get("ok")), r.get("error")

    def stop_capture(self) -> None:
        try:
            self._post("/api/run/stop", {})
        except Exception:
            pass

    def set_recovering(self, _recovering: bool) -> None:
        # Server owns recovery decisions; this only existed locally to
        # mark the UI during driver resets, which can't happen remotely.
        pass

    def drain_tc_queue(self):
        # TC arrives via SSE EVT_TC, not via a local queue.
        return None

    def update_signal_state(self, _new_state: Optional[str]) -> bool:
        # Server emits these from its own poll loop; the local one is inert.
        return False

    # ════════════════════════════════════════════════════════════════════
    # LV1 control (proxied)
    # ════════════════════════════════════════════════════════════════════

    def lv1_connect(self, host: str, port: int) -> None:
        try:
            self._post("/api/lv1/connect", {"host": host, "port": int(port)})
        except Exception:
            pass

    def lv1_disconnect_async(self) -> None:
        try:
            self._post("/api/lv1/disconnect", {})
        except Exception:
            pass

    def lv1_recall_scene(self, index: int) -> Tuple[bool, Optional[str]]:
        try:
            r = self._post("/api/lv1/recall", {"index": int(index)})
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return bool(r.get("ok")), r.get("error")

    def resolve_target(self, manual_host: str, manual_port: int):
        # In remote mode we don't run discovery locally; the host's
        # /api/cues/state.discovery list is what we'd consult, but it's
        # opaque to the proxy. Fall back to whatever the user typed.
        if manual_host and manual_port > 0:
            return manual_host, manual_port
        return None

    def start_discovery(self) -> bool:
        try:
            r = self._post("/api/lv1/discover", {})
            return bool(r.get("ok"))
        except Exception:
            return False

    # ════════════════════════════════════════════════════════════════════
    # Cue list mutations (proxied — state updates come back via EVT_CUES)
    # ════════════════════════════════════════════════════════════════════

    def add_cue(self, label: str, timecode: str, scene_name: str = "",
                scene_index: Optional[int] = None, enabled: bool = True) -> Optional[Cue]:
        try:
            r = self._post("/api/cues", {
                "label": label, "timecode": timecode,
                "scene_name": scene_name, "scene_index": scene_index,
                "enabled": enabled,
            })
            cue_id = int(r.get("cue_id") or 0)
            if cue_id:
                return Cue(id=cue_id, label=label, timecode=timecode,
                           scene_name=scene_name, scene_index=scene_index,
                           enabled=enabled)
        except Exception:
            pass
        return None

    def update_cue(self, cue_id: int, **fields: Any) -> bool:
        try:
            r = self._patch(f"/api/cues/{int(cue_id)}", fields)
            return bool(r.get("ok"))
        except Exception:
            return False

    def remove_cue(self, cue_id: int) -> bool:
        try:
            r = self._delete(f"/api/cues/{int(cue_id)}")
            return bool(r.get("ok"))
        except Exception:
            return False

    def move_cue_up(self, cue_id: int) -> bool:
        return self._move(cue_id, "up")

    def move_cue_down(self, cue_id: int) -> bool:
        return self._move(cue_id, "down")

    def _move(self, cue_id: int, direction: str) -> bool:
        try:
            r = self._post(f"/api/cues/{int(cue_id)}/move", {"direction": direction})
            return bool(r.get("ok"))
        except Exception:
            return False

    def toggle_cue_enabled(self, cue_id: int) -> Optional[bool]:
        try:
            r = self._post(f"/api/cues/{int(cue_id)}/toggle", {})
            return r.get("enabled") if r.get("ok") else None
        except Exception:
            return None

    def tap_at_current_tc(self) -> Optional[Cue]:
        try:
            r = self._post("/api/cues/tap", {})
            cue_id = int(r.get("cue_id") or 0)
            if cue_id:
                # The full cue will land via EVT_CUES; return a thin stub
                # so MainWindow can select the row by id immediately.
                return Cue(id=cue_id, label="", timecode="",
                           scene_name="", scene_index=None)
        except Exception:
            pass
        return None

    def assign_scene_to_cue(self, cue_id: int, scene_index: int) -> bool:
        try:
            r = self._post(f"/api/cues/{int(cue_id)}/assign",
                           {"scene_index": int(scene_index)})
            return bool(r.get("ok"))
        except Exception:
            return False

    def test_fire_cue(self, cue_id: int) -> Tuple[bool, Optional[str]]:
        try:
            r = self._post(f"/api/cues/{int(cue_id)}/fire", {})
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return bool(r.get("ok")), r.get("error")

    def reset_fired(self) -> None:
        try:
            self._post("/api/cues/reset", {})
        except Exception:
            pass

    def revalidate(self) -> List[CueValidation]:
        try:
            self._post("/api/cues/revalidate", {})
        except Exception:
            pass
        # Issues come back in the response but we don't actually use them in
        # remote mode (MainWindow's revalidate path also shows local warnings
        # which would have to be reconstructed from the catalog snapshot).
        with self._lock:
            return validate_all(self.cue_list.cues, self.scene_catalog)

    def validate(self) -> List[CueValidation]:
        with self._lock:
            return validate_all(self.cue_list.cues, self.scene_catalog)

    # ════════════════════════════════════════════════════════════════════
    # File ops (proxied)
    # ════════════════════════════════════════════════════════════════════

    def new_cue_list(self) -> None:
        try:
            self._post("/api/cues/new", {})
        except Exception:
            pass

    def load_cue_file(self, path: str) -> Tuple[bool, Optional[str], bool]:
        """In remote mode `path` is a file on the OPERATOR'S local disk.
        Read it locally and upload to the host."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), False
        try:
            r = self._post("/api/cues/upload", data)
            return bool(r.get("ok")), r.get("error"), False
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), False

    def save_cue_file(self, path: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """Mirror the web's Save semantics:

        - `path is None` or `path == self.current_file` → plain "Save",
          host re-saves at its current_file with no body. This is the
          right behaviour when the operator clicked File ▸ Save on a
          file that the host already owns.
        - Otherwise `path` is taken as a *name only* (basename) and the
          host writes it into its default projects folder. This is what
          File ▸ Save as… should pass in remote mode — full filesystem
          paths from the operator's machine are meaningless on the host.
        """
        body: Dict[str, Any] = {}
        if path and path != self.current_file:
            name = os.path.basename(path).strip()
            if name:
                body["name"] = name
        try:
            r = self._post("/api/cues/save", body)
            return bool(r.get("ok")), r.get("error")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def load_cue_list_from_data(self, data: List[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        try:
            r = self._post("/api/cues/upload", data)
            return bool(r.get("ok")), r.get("error")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ────────────────────────────────────────────────────────────────────
    # Recent files (proxied — entries refer to paths on the HOST)
    # ────────────────────────────────────────────────────────────────────

    def add_recent(self, _path: str) -> None:
        # Host adds entries automatically on its own load/save; nothing
        # to do here.
        pass

    def clear_recent(self) -> None:
        try:
            self._request("/api/cues/recent", method="DELETE")
        except Exception:
            pass

    def _prune_recent(self, _path: str) -> None:
        # Host-side concern. Pruning happens implicitly when load fails.
        pass

    def _recent_files_payload(self) -> List[Dict[str, Any]]:
        try:
            r = self._get("/api/cues/recent")
            return list(r.get("recent_files") or [])
        except Exception:
            return []

    # ════════════════════════════════════════════════════════════════════
    # Settings + status (proxied)
    # ════════════════════════════════════════════════════════════════════

    def set_tolerance(self, frames: int) -> None:
        try:
            self._patch("/api/settings", {"tolerance_frames": int(frames)})
        except Exception:
            pass

    def set_dry_run(self, on: bool) -> None:
        try:
            self._patch("/api/settings", {"dry_run": bool(on)})
        except Exception:
            pass

    def update_settings(self, **kw: Any) -> None:
        try:
            self._patch("/api/settings", kw)
        except Exception:
            pass

    def set_status(self, text: str, warn: bool = False) -> None:
        # Status bar mirrors host's status — emitting locally lets the
        # operator see the message right away without a round-trip.
        self.last_status = {"text": text, "warn": bool(warn)}
        self._emit(EVT_STATUS, dict(self.last_status))

    def mark_dirty(self) -> None:
        # Host owns the dirty flag; this is best-effort feedback.
        pass

    # ════════════════════════════════════════════════════════════════════
    # SSE reader — pulls live events from the host
    # ════════════════════════════════════════════════════════════════════

    def _sse_loop(self) -> None:
        backoff = 1.0
        while not self._sse_stop.is_set():
            try:
                self._sse_session()
                backoff = 1.0  # successful run — reset
            except Exception as exc:  # noqa: BLE001
                if self._sse_stop.is_set():
                    break
                self._sse_connected = False
                self._emit(EVT_REMOTE_DISCONNECTED, {"reason": str(exc)})
                self._sse_stop.wait(backoff)
                backoff = min(backoff * 2.0, 15.0)

    def _sse_session(self) -> None:
        req = urllib.request.Request(
            f"{self._base}/api/events",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
        )
        # No read timeout — SSE is a long-lived stream. We rely on keep-
        # alive comments (": ping" every 15 s server-side) to detect a
        # silent peer; socket-level errors break us out into the backoff.
        self._sse_resp = urllib.request.urlopen(req, timeout=None)
        self._sse_connected = True
        self._emit(EVT_REMOTE_CONNECTED, {
            "host": self._host, "port": self._port, "version": self._version,
        })
        try:
            self._parse_sse(self._sse_resp)
        finally:
            try:
                self._sse_resp.close()
            except Exception:
                pass
            self._sse_resp = None

    def _parse_sse(self, resp) -> None:
        """Minimal text/event-stream parser. Each event is one or more
        `field: value` lines terminated by a blank line."""
        evt_name = "message"
        data_lines: List[str] = []
        for raw in resp:
            if self._sse_stop.is_set():
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                # Dispatch the accumulated event.
                if data_lines:
                    payload_str = "\n".join(data_lines)
                    try:
                        payload = json.loads(payload_str)
                    except json.JSONDecodeError:
                        payload = {}
                    self._handle_event(evt_name, payload)
                evt_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue  # comment / keep-alive
            if line.startswith("event:"):
                evt_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            # Other SSE fields (id, retry) are not used.

    # ════════════════════════════════════════════════════════════════════
    # State updates from SSE events
    # ════════════════════════════════════════════════════════════════════

    def _handle_event(self, name: str, payload: Dict[str, Any]) -> None:
        if name == "snapshot":
            self._apply_snapshot(payload)
            return
        if name == EVT_TC:
            with self._lock:
                self.current_tc = payload.get("tc")
                self._fps = payload.get("fps")
                self.signal_state = payload.get("signal")
            self._emit(EVT_TC, payload)
            return
        if name == EVT_RUNNING:
            with self._lock:
                self.running = bool(payload.get("running"))
                self.recovering = bool(payload.get("recovering"))
                if "signal" in payload:
                    self.signal_state = payload.get("signal")
            self._emit(EVT_RUNNING, payload)
            return
        if name == EVT_LV1_STATE:
            with self._lock:
                self._lv1_connected = bool(payload.get("connected"))
                self._lv1_registered = bool(payload.get("registered"))
                self._lv1_host = payload.get("host")
                self._lv1_port = payload.get("port")
                self._lv1_last_error = payload.get("last_error")
            self._emit(EVT_LV1_STATE, payload)
            return
        if name == EVT_LV1_CATALOG:
            with self._lock:
                self.scene_catalog = {
                    int(s["index"]): str(s["name"])
                    for s in payload.get("scenes") or []
                }
            self._emit(EVT_LV1_CATALOG, payload)
            return
        if name == EVT_LV1_CURRENT:
            with self._lock:
                self.lv1_current_scene = payload.get("index")
            self._emit(EVT_LV1_CURRENT, payload)
            return
        if name == EVT_CUES:
            with self._lock:
                self.cue_list.replace_all(payload.get("cues") or [])
            self._emit(EVT_CUES, payload)
            return
        if name == EVT_CUE_FIRED:
            with self._lock:
                self.last_fire = dict(payload)
            self._emit(EVT_CUE_FIRED, payload)
            return
        if name == EVT_LAST_FIRE:
            with self._lock:
                self.last_fire = dict(payload)
            self._emit(EVT_LAST_FIRE, payload)
            return
        if name == EVT_DIRTY:
            with self._lock:
                self.dirty = bool(payload.get("dirty"))
                self.current_file = payload.get("file")
            self._emit(EVT_DIRTY, payload)
            return
        if name == EVT_DISCOVERY:
            with self._lock:
                self.discovery_running = bool(payload.get("scanning"))
                self.discovered = self._build_discovery_entries(payload.get("results") or [])
            self._emit(EVT_DISCOVERY, payload)
            return
        if name == EVT_STATUS:
            with self._lock:
                self.last_status = dict(payload)
            self._emit(EVT_STATUS, payload)
            return
        if name == EVT_RECENT:
            self._emit(EVT_RECENT, payload)
            return
        if name == EVT_SETTINGS:
            # We don't override our LOCAL settings with the host's — the
            # remote operator may want different UI prefs. Just re-emit.
            self._emit(EVT_SETTINGS, payload)
            return
        # Unknown events: forward verbatim, harmless.
        self._emit(name, payload)

    @staticmethod
    def _build_discovery_entries(rows: List[Dict[str, Any]]):
        """Convert the simplified snapshot/event form ([{host, ip, port}, ...])
        back into DiscoveryEntry-like objects so MainWindow's _resolve_target
        can iterate .addresses / .host / .port the same way as in host mode."""
        from zdns_discover import DiscoveryEntry
        entries: List[DiscoveryEntry] = []
        for r in rows:
            ip = str(r.get("ip") or "")
            entries.append(DiscoveryEntry(
                service="",
                uuid=None,
                host=str(r.get("host") or ""),
                port=int(r.get("port") or 0),
                addresses=[ip] if ip else [],
                source=ip,
            ))
        return entries

    def _apply_snapshot(self, snap: Dict[str, Any]) -> None:
        """Hydrate every mirrored field from a /api/state response or the
        initial SSE 'snapshot' event."""
        with self._lock:
            self.running = bool(snap.get("running"))
            self.recovering = bool(snap.get("recovering"))
            self.current_tc = snap.get("current_tc")
            self.signal_state = snap.get("signal")
            self._fps = snap.get("fps")
            self._version = snap.get("version")
            lv1 = snap.get("lv1") or {}
            self._lv1_connected = bool(lv1.get("connected"))
            self._lv1_registered = bool(lv1.get("registered"))
            self._lv1_host = lv1.get("host")
            self._lv1_port = lv1.get("port")
            self._lv1_last_error = lv1.get("last_error")
            self.scene_catalog = {
                int(s["index"]): str(s["name"])
                for s in snap.get("scenes") or []
            }
            self.lv1_current_scene = snap.get("lv1_current_scene")
            self.cue_list.replace_all(snap.get("cues") or [])
            self.current_file = snap.get("current_file")
            self.dirty = bool(snap.get("dirty"))
            self.last_fire = snap.get("last_fire")
            self.last_status = snap.get("last_status")
            disc = snap.get("discovery") or {}
            self.discovery_running = bool(disc.get("scanning"))
            self.discovered = self._build_discovery_entries(disc.get("results") or [])
        # Fire individual events so subscribers see initial state painted.
        self._emit(EVT_CUES, {"cues": snap.get("cues") or []})
        self._emit(EVT_LV1_CATALOG, {"scenes": snap.get("scenes") or []})
        self._emit(EVT_LV1_STATE, snap.get("lv1") or {})
        self._emit(EVT_RUNNING, {
            "running": self.running, "recovering": self.recovering,
            "signal": self.signal_state,
        })
        if self.last_fire:
            self._emit(EVT_LAST_FIRE, self.last_fire)

    # ════════════════════════════════════════════════════════════════════
    # HTTP plumbing
    # ════════════════════════════════════════════════════════════════════

    def _get(self, path: str, timeout: float = 5.0) -> Dict[str, Any]:
        return self._request(path, method="GET", timeout=timeout)

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 5.0) -> Dict[str, Any]:
        return self._request(path, method="POST", body=body, timeout=timeout)

    def _patch(self, path: str, body: Dict[str, Any],
               timeout: float = 5.0) -> Dict[str, Any]:
        return self._request(path, method="PATCH", body=body, timeout=timeout)

    def _delete(self, path: str, timeout: float = 5.0) -> Dict[str, Any]:
        return self._request(path, method="DELETE", timeout=timeout)

    def _request(self, path: str, method: str = "GET",
                 body: Optional[Any] = None,
                 timeout: float = 5.0) -> Dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self._base}{path}", data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
                err = json.loads(body_text).get("error") or body_text
            except Exception:
                err = exc.reason or str(exc)
            raise RuntimeError(f"{exc.code} {err}")
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc.reason))
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
