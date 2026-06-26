"""
Peer discovery for LTCtoLV1 instances on the LAN.

Mirrors the pattern used by zdns_discover.py (which finds Waves LV1 mixers)
but for our own app, so a remote operator's machine can find every
LTCtoLV1 host on the network without typing IPs.

Protocol
--------
- Group:   225.1.1.2 : 13338     (distinct from LV1's 225.1.1.1:13337)
- Payload: a UTF-8 JSON object, one per packet:
    {
      "app":      "LTCtoLV1",
      "version":  "2.0.1",
      "hostname": "LV1-Stage",
      "ips":      ["192.168.1.10", "10.0.0.5"],
      "web_port": 8080,
      "uuid":     "<process uuid, stable for the run>"
    }
- Cadence: hosts beacon every 2 s. Listeners time out entries after 10 s
  of silence — long enough to survive a missed beacon, short enough that
  a host that quits visibly drops off the picker.

Threading: each side owns its own daemon thread; calling stop() joins it.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


MCAST_ADDR = "225.1.1.2"
MCAST_PORT = 13338
BEACON_INTERVAL_S = 2.0
ENTRY_TIMEOUT_S = 10.0
APP_TAG = "LTCtoLV1"


# ─── Data ───────────────────────────────────────────────────────────────


@dataclass
class HostEntry:
    """One LTCtoLV1 instance seen on the LAN."""

    uuid: str
    hostname: str
    version: str
    ips: List[str] = field(default_factory=list)
    web_port: int = 0
    source_ip: str = ""           # IP the announcement packet actually came from
    last_seen: float = 0.0

    @property
    def best_ip(self) -> str:
        """Pick the most-likely-routable IP. The source IP always wins
        (it's the address the host *actually* used to send the beacon,
        so we know packets can come back). Falls back to the first
        announced address."""
        if self.source_ip:
            return self.source_ip
        return self.ips[0] if self.ips else ""

    @property
    def url(self) -> str:
        ip = self.best_ip
        return f"http://{ip}:{self.web_port}/" if ip and self.web_port else ""


# ─── Host: beacon sender ────────────────────────────────────────────────


class Announcer:
    """Periodically broadcasts our presence on the multicast group."""

    def __init__(
        self,
        get_payload: Callable[[], Dict],
        interval_s: float = BEACON_INTERVAL_S,
    ) -> None:
        self._get_payload = get_payload
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="LTCtoLV1-Announcer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        # Build the socket lazily so a missing multicast interface doesn't
        # crash the host app — just silently disables discovery.
        sock = self._open_socket()
        if sock is None:
            print("[announce] failed to open multicast socket")
            return
        first = True
        try:
            while not self._stop.is_set():
                try:
                    payload = self._get_payload()
                    payload.setdefault("app", APP_TAG)
                    data = json.dumps(payload).encode("utf-8")
                    sock.sendto(data, (MCAST_ADDR, MCAST_PORT))
                    if first:
                        print(f"[announce] beaconing on {MCAST_ADDR}:{MCAST_PORT} "
                              f"(host={payload.get('hostname')!r}, "
                              f"ips={payload.get('ips')}, "
                              f"port={payload.get('web_port')})")
                        first = False
                except Exception as exc:  # noqa: BLE001
                    if first:
                        print(f"[announce] beacon send failed: {exc}")
                        first = False
                # wait() returns True if stop was set during the sleep, so
                # we exit immediately on shutdown instead of dragging out.
                if self._stop.wait(self._interval):
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _open_socket(self) -> Optional[socket.socket]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            # Explicit loopback — needed when host + remote run on the
            # same machine. Default is usually ON but some Windows builds
            # ship with it OFF (and quietly so), which makes same-host
            # testing fail silently.
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            except OSError:
                pass
            return s
        except OSError:
            return None


# ─── Remote: beacon listener ────────────────────────────────────────────


class Discoverer:
    """Listens for host beacons and exposes a live, time-pruned list."""

    def __init__(
        self,
        on_change: Optional[Callable[[List[HostEntry]], None]] = None,
    ) -> None:
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._entries: Dict[str, HostEntry] = {}  # keyed by uuid

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="LTCtoLV1-Discoverer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def entries(self) -> List[HostEntry]:
        """Return a snapshot of currently-live hosts, sorted by hostname."""
        now = time.time()
        with self._lock:
            alive = [e for e in self._entries.values()
                     if now - e.last_seen <= ENTRY_TIMEOUT_S]
        alive.sort(key=lambda e: (e.hostname.lower(), e.uuid))
        return alive

    def _run(self) -> None:
        sock = self._open_socket()
        if sock is None:
            print("[discover] failed to open multicast listen socket on "
                  f"{MCAST_ADDR}:{MCAST_PORT} (firewall? port in use?)")
            return
        print(f"[discover] listening on {MCAST_ADDR}:{MCAST_PORT}")
        first_packet = True
        try:
            while not self._stop.is_set():
                try:
                    sock.settimeout(1.0)
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    self._prune_and_notify()
                    continue
                except OSError:
                    break
                if first_packet:
                    print(f"[discover] first packet from {addr[0]}:{addr[1]} "
                          f"({len(data)} bytes)")
                    first_packet = False
                self._handle_packet(data, addr[0])
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _handle_packet(self, data: bytes, source_ip: str) -> None:
        try:
            obj = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(obj, dict) or obj.get("app") != APP_TAG:
            return
        uuid = str(obj.get("uuid") or "")
        if not uuid:
            return
        entry = HostEntry(
            uuid=uuid,
            hostname=str(obj.get("hostname") or "unknown"),
            version=str(obj.get("version") or ""),
            ips=[str(ip) for ip in obj.get("ips") or [] if ip],
            web_port=int(obj.get("web_port") or 0),
            source_ip=source_ip,
            last_seen=time.time(),
        )
        changed = False
        with self._lock:
            prev = self._entries.get(uuid)
            self._entries[uuid] = entry
            if prev is None or _entry_visible_changed(prev, entry):
                changed = True
        if changed:
            self._notify()

    def _prune_and_notify(self) -> None:
        now = time.time()
        removed = False
        with self._lock:
            for uuid in list(self._entries):
                if now - self._entries[uuid].last_seen > ENTRY_TIMEOUT_S:
                    del self._entries[uuid]
                    removed = True
        if removed:
            self._notify()

    def _notify(self) -> None:
        if not self._on_change:
            return
        try:
            self._on_change(self.entries())
        except Exception:
            pass

    def _open_socket(self) -> Optional[socket.socket]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass  # not on Windows; harmless
            s.bind(("", MCAST_PORT))
            # Join the group on every detected NIC + wildcard so we don't
            # miss beacons when the OS picks a different default route.
            for ip in _local_ipv4s_for_join():
                try:
                    mreq = struct.pack(
                        "=4s4s",
                        socket.inet_aton(MCAST_ADDR),
                        socket.inet_aton(ip),
                    )
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError:
                    pass
            try:
                mreq = struct.pack(
                    "=4sl", socket.inet_aton(MCAST_ADDR), socket.INADDR_ANY
                )
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass
            return s
        except OSError:
            return None


# ─── Helpers ────────────────────────────────────────────────────────────


def make_uuid() -> str:
    """Per-process UUID for the host announcement."""
    return _uuid.uuid4().hex


def _entry_visible_changed(a: HostEntry, b: HostEntry) -> bool:
    """Did anything user-visible change? Avoids notifying for trivial
    last_seen updates (which happen every beacon)."""
    return (
        a.hostname != b.hostname
        or a.version != b.version
        or a.web_port != b.web_port
        or a.best_ip != b.best_ip
    )


def _local_ipv4s_for_join() -> List[str]:
    """A small subset of the NICs to join multicast on. Re-uses the same
    enumeration as zdns_discover so behaviour stays consistent."""
    try:
        from zdns_discover import _local_ipv4s
        return _local_ipv4s()
    except Exception:
        return []
