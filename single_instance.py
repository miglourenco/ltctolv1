"""
Single-instance enforcement via a localhost TCP listen socket.

First instance to start wins the lock by binding 127.0.0.1:<port>.
A second instance fails to bind, opens a one-shot connection to the
same port instead, sends a SHOW command, and exits — so the primary
gets a chance to bring its window to the foreground (turning the
double-launch into "raise existing window", matching how Slack,
Notion, etc. behave on Windows).

Cross-platform:
  - Windows uses SO_EXCLUSIVEADDRUSE so two processes really can't
    share the bind, even with weird PORT-reuse flags inherited from
    parent processes.
  - macOS / Linux: default bind semantics are already exclusive for
    a listen socket — no extra flag needed.

The port is fixed (49251 by default) and lives in the IANA dynamic
range. The first-launch CLI flag --allow-multiple skips the check
entirely; useful for developers running host + remote on the same
PC while testing.
"""

from __future__ import annotations

import socket
import sys
import threading
from typing import Callable, Optional


DEFAULT_PORT = 49251
_SIGNAL_SHOW = b"SHOW\n"


class SingleInstance:
    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._listen: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._on_signal: Optional[Callable[[], None]] = None

    # ─── Public API ─────────────────────────────────────────────────────

    def acquire(self) -> bool:
        """Try to become the primary instance. Returns True if we got
        the lock, False if another instance is already running."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform == "win32":
                # SO_EXCLUSIVEADDRUSE is 0xFFFFFFFB (-5 when signed),
                # not exposed as a constant on Python < 3.12. Setting
                # it prevents another process from also binding the
                # same port even with SO_REUSEADDR tricks.
                try:
                    sock.setsockopt(socket.SOL_SOCKET, -5, 1)
                except OSError:
                    pass
            sock.bind(("127.0.0.1", self.port))
            sock.listen(5)
        except OSError:
            return False
        self._listen = sock
        self._thread = threading.Thread(
            target=self._serve, name="SingleInstance", daemon=True
        )
        self._thread.start()
        return True

    def signal_primary(self) -> bool:
        """As a secondary instance, tell the primary to bring its window
        forward. Returns True if the message was delivered."""
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2.0) as s:
                s.sendall(_SIGNAL_SHOW)
            return True
        except OSError:
            return False

    def set_on_signal(self, fn: Callable[[], None]) -> None:
        """Register the callback fired when a secondary instance pings
        us. Runs on the listener thread — the caller is responsible
        for marshalling onto its UI thread (tk: root.after_idle)."""
        self._on_signal = fn

    def release(self) -> None:
        """Drop the lock + stop the listener. Safe to call more than once."""
        self._stop.set()
        try:
            if self._listen:
                self._listen.close()
                self._listen = None
        except OSError:
            pass

    # ─── Listener loop ──────────────────────────────────────────────────

    def _serve(self) -> None:
        sock = self._listen
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                sock.settimeout(0.5)
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(0.5)
                data = conn.recv(64)
                if data.startswith(b"SHOW") and self._on_signal:
                    try:
                        self._on_signal()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[single_instance] on_signal raised: {exc}")
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
