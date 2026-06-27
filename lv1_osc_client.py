"""
Waves LV1 OSC-over-TCP client.

Connects to an LV1 mixer, runs the MyFOH-style /handshake registration,
auto-responds to /ping with /pong, maintains a live mirror of the scene
catalog (/Notify/SceneList + /Notify/Scene/Name), tracks the current scene
index (/Notify/CurSceneIndex), and exposes recall_scene() to drive snapshot
recall.

Threading model:
  - A single background reader thread reads from the socket and dispatches
    /Notify/... messages by updating state under a lock and firing
    callbacks. Callbacks run on the reader thread; the caller is responsible
    for marshalling to the UI thread if needed.
  - send() is safe to call from any thread; writes are short and synchronous.
"""

from __future__ import annotations

import logging
import random
import socket
import string
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from lv1_osc import (
    HEADER_LEN,
    LV1_HEADER,
    OscArg,
    OscMessage,
    bool_arg,
    decode_packet,
    encode_message,
    frame_batch,
    frame_message,
    int_arg,
    int_value,
    str_arg,
    str_value,
    try_extract_frame,
)


log = logging.getLogger(__name__)


# --- Public state objects ---------------------------------------------------


@dataclass
class SceneCatalogSnapshot:
    """An immutable snapshot of the LV1's scene list."""

    scenes: Dict[int, str]
    received_at: float


@dataclass
class ConnectionState:
    connected: bool
    registered: bool
    last_error: Optional[str]
    host: Optional[str]
    port: Optional[int]


# --- The client -------------------------------------------------------------


class LV1Client:
    DEFAULT_DEVICE_NAME = "LTCtoLV1"

    def __init__(
        self,
        device_name: Optional[str] = None,
        uuid: Optional[str] = None,
        auto_reconnect: bool = True,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 15.0,
    ) -> None:
        self.device_name = device_name or self.DEFAULT_DEVICE_NAME
        self.uuid = uuid or _random_uuid()
        self.auto_reconnect = auto_reconnect
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s

        # Connection target
        self._host: Optional[str] = None
        self._port: Optional[int] = None

        # Socket + reader thread
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rx_buf = b""
        self._send_lock = threading.Lock()  # serialise sends across threads
        self._state_lock = threading.Lock()  # protect scene catalog / current

        # Mirrored state
        self._scenes: Dict[int, str] = {}
        self._current_scene: Optional[int] = None
        self._connected = False
        self._registered = False
        self._last_error: Optional[str] = None
        self._handshake_acked = threading.Event()

        # Callbacks (called from the reader thread — caller marshals to UI)
        self.on_connection_change: Optional[Callable[[ConnectionState], None]] = None
        self.on_catalog_change: Optional[Callable[[SceneCatalogSnapshot], None]] = None
        self.on_current_scene_change: Optional[Callable[[Optional[int]], None]] = None
        self.on_log: Optional[Callable[[str, str], None]] = None  # (level, msg)

    # --- Lifecycle ----------------------------------------------------------

    def connect(self, host: str, port: int) -> None:
        """Start connecting (returns immediately). Use connection_state()
        or the on_connection_change callback to know when it's up.

        Idempotent: if we're already connected to the exact same target,
        we re-emit the current state and return. This matters for the
        web/remote-mode flow where the operator clicks Connect on an
        already-connected target — without this check the call would
        tear down a healthy LV1 session and reopen it, which on Windows
        adds a multi-second freeze waiting on the reader's join."""
        with self._state_lock:
            same_target = (
                self._connected
                and self._host == host
                and self._port == port
            )
        if same_target:
            # Re-broadcast the current state so any UI that just issued
            # the connect (and is sitting at "● connecting…") snaps back
            # to ONLINE without having to wait for the next genuine
            # state transition.
            self._emit_connection_state()
            return
        self.disconnect()
        self._host = host
        self._port = port
        self._stop.clear()
        self._handshake_acked.clear()
        self._reader = threading.Thread(
            target=self._run_reader,
            name=f"LV1Reader[{host}:{port}]",
            daemon=True,
        )
        self._reader.start()

    def disconnect(self) -> None:
        """Stop the reader thread and close the socket. Waits up to 2 s
        for the reader to exit cleanly so any in-flight callbacks settle
        before the caller assumes we're fully torn down. Use fast_close()
        instead during app shutdown."""
        self._stop.set()
        self._close_socket()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=2.0)
        self._reader = None
        with self._state_lock:
            self._connected = False
            self._registered = False
        self._emit_connection_state()

    def fast_close(self) -> None:
        """Stop the reader + close the socket, but don't wait for the
        reader thread to exit. Intended for app shutdown only — the
        reader is a daemon thread that will die with the process, and
        the up-to-2-second join() in disconnect() feels like a freeze
        on a clean app close even though no real work is happening."""
        self.auto_reconnect = False
        self._stop.set()
        self._close_socket()

    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected and self._registered

    def connection_state(self) -> ConnectionState:
        with self._state_lock:
            return ConnectionState(
                connected=self._connected,
                registered=self._registered,
                last_error=self._last_error,
                host=self._host,
                port=self._port,
            )

    # --- Public actions -----------------------------------------------------

    def recall_scene(self, index: int) -> None:
        """Recall a scene by 0-based index. Fire-and-forget."""
        self._send_message("/Set/CurSceneIndex", [int_arg(int(index))])

    def request_scene_list(self) -> None:
        """Ask the LV1 to re-broadcast the scene catalog. The LV1 normally
        sends this on connect anyway; this is for forced refresh."""
        # No dedicated /Get/SceneList endpoint exists; the catalog arrives
        # via /Notify/SceneList during the handshake. A re-handshake is
        # the only way to force it. For now, just no-op — most callers
        # rely on the auto-push during handshake.
        pass

    # --- State queries ------------------------------------------------------

    def scene_catalog(self) -> Dict[int, str]:
        """Current scene catalog: {0: 'Scene Teste A', 1: 'Scene Teste B', ...}"""
        with self._state_lock:
            return dict(self._scenes)

    def current_scene(self) -> Optional[int]:
        with self._state_lock:
            return self._current_scene

    # --- Reader thread ------------------------------------------------------

    def _run_reader(self) -> None:
        backoff = self.reconnect_min_s
        last_error_logged: Optional[str] = None
        while not self._stop.is_set():
            try:
                self._connect_socket()
                backoff = self.reconnect_min_s
                last_error_logged = None  # back online — next failure is news again
                self._handshake()
                self._read_loop()
            except Exception as exc:  # noqa: BLE001
                # Don't surface "socket was closed because we asked to stop"
                # as a connection failure. Socket-gone errors after _stop
                # is set are just shutdown noise.
                if not self._stop.is_set():
                    msg = str(exc)
                    self._set_error(msg)
                    # Auto-reconnect re-enters this loop every few seconds
                    # whenever the LV1 is unreachable. We log the FIRST
                    # failure so the operator notices, then stay quiet on
                    # identical retries — the LV1 STATUS panel already
                    # shows "Disconnected — ..." so the operator can see
                    # the persistent state without it bouncing in/out of
                    # the status bar.
                    if msg != last_error_logged:
                        self._log("warn", f"LV1 connection failed: {exc}")
                        last_error_logged = msg
            finally:
                self._close_socket()
                with self._state_lock:
                    self._connected = False
                    self._registered = False
                self._emit_connection_state()
            if not self.auto_reconnect or self._stop.is_set():
                break
            # Backoff with jitter so a fleet of clients doesn't thunder-hammer
            sleep_for = backoff * (0.9 + 0.2 * random.random())
            self._stop.wait(sleep_for)
            backoff = min(backoff * 2, self.reconnect_max_s)

    def _connect_socket(self) -> None:
        host = self._host
        port = self._port
        if not host or not port:
            raise RuntimeError("No host/port set")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)  # connect deadline
        s.connect((host, port))
        s.settimeout(None)  # blocking reads from here
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s
        self._rx_buf = b""
        with self._state_lock:
            self._connected = True
            self._registered = False
            self._last_error = None
        self._emit_connection_state()
        self._log("info", f"TCP connected to {host}:{port}")

    def _close_socket(self) -> None:
        s = self._sock
        self._sock = None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _handshake(self) -> None:
        """MyFOH-style /handshake + /device_name batched as one TCP write.

        Runs inline on the reader thread: sends the batch, then does its own
        bounded recv() loop until the /handshake ACK arrives (the regular
        _read_loop() only starts AFTER this returns, so we can't wait on
        _handshake_acked alone — nobody would be reading the socket)."""
        payloads = [
            encode_message(
                "/handshake",
                [int_arg(1), int_arg(-1), int_arg(1)],
            ),
            encode_message(
                "/device_name",
                [str_arg(self.device_name), str_arg(self.uuid)],
            ),
        ]
        sock = self._sock
        if sock is None:
            raise RuntimeError("Socket gone before handshake")
        with self._send_lock:
            sock.sendall(frame_batch(payloads))

        # Drain incoming frames with a 3 s overall deadline. Anything that
        # arrives (catalog, notifies, current scene) gets dispatched normally.
        deadline = time.monotonic() + 3.0
        while not self._handshake_acked.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("No /handshake ACK after 3 s")
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
            except socket.timeout:
                raise RuntimeError("No /handshake ACK after 3 s")
            except OSError as exc:
                raise RuntimeError(f"recv during handshake failed: {exc}")
            finally:
                sock.settimeout(None)
            if not chunk:
                raise RuntimeError("LV1 closed the connection during handshake")
            self._rx_buf += chunk
            self._drain()

        with self._state_lock:
            self._registered = True
        self._emit_connection_state()
        self._log("info", f"Registered as device '{self.device_name}'")

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                chunk = sock.recv(65536)
            except OSError as exc:
                # Windows-only race: when close() lands before the
                # in-flight recv() wakes up via shutdown(), recv raises
                # WSAENOTSOCK (10038). Same goes for WSAENOTCONN (10057)
                # if the peer closed the half-duplex first. These are
                # teardown noise, not connection problems — return so the
                # outer reader exits cleanly without logging a cryptic
                # Win32 error to the status bar. Auto-reconnect (if on)
                # will pick the LV1 back up if the peer is still alive.
                win = getattr(exc, "winerror", None)
                if win in (10038, 10057):
                    return
                raise RuntimeError(f"recv failed: {exc}")
            if not chunk:
                raise RuntimeError("LV1 closed the connection")
            self._rx_buf += chunk
            self._drain()

    def _drain(self) -> None:
        while True:
            header, payload, remaining = try_extract_frame(self._rx_buf)
            self._rx_buf = remaining
            if header is None or payload is None:
                return
            try:
                msg = decode_packet(payload)
            except Exception as exc:  # noqa: BLE001
                self._log("warn", f"decode failed: {exc}")
                continue
            self._dispatch(msg)

    # --- Message dispatch ---------------------------------------------------

    def _dispatch(self, msg: OscMessage) -> None:
        addr = msg.address

        # 1. Auto-pong to keep the link alive (the LV1 drops us after ~5 s
        #    of unanswered pings).
        if addr == "/ping":
            try:
                self._send_raw(encode_message("/pong", msg.args))
            except Exception:
                pass
            return

        # 2. Handshake ACK
        if addr == "/handshake":
            v = int_value(msg.args[0]) if msg.args else None
            if v == 1:
                self._handshake_acked.set()
            return

        # 3. Scene catalog
        if addr == "/Notify/SceneList":
            self._handle_scene_list(msg)
            return

        # 4. A single scene being renamed (broadcast for the CURRENT scene only)
        if addr == "/Notify/Scene/Name":
            self._handle_scene_name(msg)
            return

        # 5. Current scene changed
        if addr == "/Notify/CurSceneIndex":
            self._handle_current_scene(msg)
            return

        # Anything else — ignore. We only care about scenes for this app.

    def _handle_scene_list(self, msg: OscMessage) -> None:
        """,iisis...  [count, idx0, name0, idx1, name1, ...]"""
        args = msg.args
        if not args:
            return
        count = int_value(args[0])
        if count is None:
            return
        scenes: Dict[int, str] = {}
        i = 1
        while i + 1 < len(args):
            idx = int_value(args[i])
            name = str_value(args[i + 1])
            if idx is not None and name is not None:
                scenes[idx] = name
            i += 2
        with self._state_lock:
            self._scenes = scenes
        self._log("info", f"Scene catalog updated ({len(scenes)} scenes)")
        snap = SceneCatalogSnapshot(scenes=dict(scenes), received_at=time.time())
        cb = self.on_catalog_change
        if cb:
            try:
                cb(snap)
            except Exception:
                pass

    def _handle_scene_name(self, msg: OscMessage) -> None:
        """,s  "new name" — broadcast for the CURRENT scene on rename/recall.

        IMPORTANT: we DON'T modify the catalog from this message. The LV1
        emits it BEFORE /Notify/CurSceneIndex updates, so writing to
        self._scenes[self._current_scene] would corrupt the old slot.
        Every recall + every rename is followed by a full /Notify/SceneList
        re-broadcast, which is the only authoritative source for the
        catalog. This handler is kept as a hook in case we ever want to
        surface "current scene was renamed" UX, but it must not touch state.
        """
        return

    def _handle_current_scene(self, msg: OscMessage) -> None:
        if not msg.args:
            return
        idx = int_value(msg.args[0])
        if idx is None:
            return
        with self._state_lock:
            self._current_scene = idx
        cb = self.on_current_scene_change
        if cb:
            try:
                cb(idx)
            except Exception:
                pass

    # --- Send helpers -------------------------------------------------------

    def _send_message(self, address: str, args: List[OscArg]) -> None:
        self._send_raw(encode_message(address, args))

    def _send_raw(self, payload: bytes) -> None:
        with self._send_lock:
            sock = self._sock
            if sock is None:
                return
            try:
                sock.sendall(frame_message(payload))
            except OSError as exc:
                self._log("warn", f"send failed: {exc}")

    # --- Misc helpers -------------------------------------------------------

    def _set_error(self, msg: str) -> None:
        with self._state_lock:
            self._last_error = msg

    def _emit_connection_state(self) -> None:
        cb = self.on_connection_change
        if cb:
            try:
                cb(self.connection_state())
            except Exception:
                pass

    def _log(self, level: str, msg: str) -> None:
        cb = self.on_log
        if cb:
            try:
                cb(level, msg)
            except Exception:
                pass
        else:
            log.log(_LEVELS.get(level, logging.INFO), msg)


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _random_uuid() -> str:
    """8-4-4-4-12 uppercase hex, like '1AC2D917-4918-4EAE-BE3C-375E290240F3'."""
    parts = [
        "".join(random.choices(string.hexdigits.upper()[:16], k=n))
        for n in (8, 4, 4, 4, 12)
    ]
    return "-".join(parts)
