"""
Discover Waves LV1 servers on the LAN via the custom "/zDNS" announcement
on multicast 225.1.1.1:13337. Standard mDNS / Bonjour does NOT work — only
this proprietary announcement does.

Each /zDNS packet is OSC-formatted and carries the service type, instance
UUID, hostname, listening port, and every IPv4 + IPv6 address on every NIC
of the LV1 host.

We rank the advertised IPv4s so callers can pick the address most likely
to actually route (192.168.x / 10.x > Docker/WSL 172.x > APIPA 169.254.x).
"""

from __future__ import annotations

import re
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from lv1_osc import decode_packet, int_value, str_value


MCAST_ADDR = "225.1.1.1"
MCAST_PORT = 13337


@dataclass
class DiscoveryEntry:
    service: str
    uuid: Optional[str]
    host: Optional[str]
    port: Optional[int]
    addresses: List[str] = field(default_factory=list)  # IPv4, ranked best-first
    source: str = ""  # IP the packet actually came from


# --- IP ranking --------------------------------------------------------------


def _rank_ip(ip: str) -> int:
    if re.match(r"^127\.", ip):
        return -100
    if re.match(r"^169\.254\.", ip):
        return -50
    if re.match(r"^172\.(1[6-9]|2[0-9]|3[01])\.", ip):
        return 30  # Docker / WSL / Hyper-V
    if re.match(r"^192\.168\.56\.", ip):
        return 20  # VirtualBox host-only
    if re.match(r"^192\.168\.", ip):
        return 100  # typical home/studio LAN
    if re.match(r"^10\.", ip):
        return 90  # corporate LAN
    return 40


def _ipv4_like(s: str) -> bool:
    return bool(re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", s))


def _parse_zdns(buf: bytes) -> Optional[dict]:
    try:
        msg = decode_packet(buf)
    except Exception:
        return None
    if msg.address != "/zDNS" or not msg.args:
        return None
    args = msg.args
    if len(args) < 2 or args[0].type != "s":
        return None

    service = str_value(args[0]) or ""
    uuid = str_value(args[1]) if len(args) > 1 else None

    host: Optional[str] = None
    port: Optional[int] = None
    ipv4s: List[str] = []

    for a in args[2:]:
        v = str_value(a)
        if v is not None:
            if _ipv4_like(v):
                ipv4s.append(v)
            elif host is None and v:
                host = v
        else:
            n = int_value(a)
            if n is not None and port is None and 1024 < n < 65536:
                port = n

    return {
        "service": service,
        "uuid": uuid,
        "host": host,
        "port": port,
        "ipv4s": ipv4s,
    }


# --- One-shot discover -------------------------------------------------------


def discover(
    timeout_s: float = 5.0,
    filter_service: str = "_waveslv113._tcp",
    on_found: Optional[Callable[[DiscoveryEntry], None]] = None,
) -> List[DiscoveryEntry]:
    """Block for `timeout_s` collecting /zDNS announcements. Returns the
    de-duplicated list of LV1 servers found. `on_found` (if given) is
    called from the listener thread for each new entry."""
    found: dict[str, DiscoveryEntry] = {}
    stop = threading.Event()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            # SO_REUSEPORT not available on Windows — that's fine.
            pass
        sock.bind(("", MCAST_PORT))

        # Join the multicast group on every non-loopback IPv4 interface so we
        # catch the announcement regardless of which NIC the LV1 lives on.
        joined_any = False
        for ip in _local_ipv4s():
            try:
                mreq = struct.pack(
                    "=4s4s",
                    socket.inet_aton(MCAST_ADDR),
                    socket.inet_aton(ip),
                )
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined_any = True
            except OSError:
                pass
        if not joined_any:
            try:
                mreq = struct.pack(
                    "=4sl", socket.inet_aton(MCAST_ADDR), socket.INADDR_ANY
                )
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass

        sock.settimeout(0.5)
        deadline = _now() + timeout_s
        while not stop.is_set() and _now() < deadline:
            try:
                buf, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            z = _parse_zdns(buf)
            if not z or z["service"] != filter_service:
                continue
            ranked = sorted(z["ipv4s"], key=_rank_ip, reverse=True)
            key = f"{z['service']}|{z['host']}|{z['port']}"
            if key in found:
                continue
            entry = DiscoveryEntry(
                service=z["service"],
                uuid=z["uuid"],
                host=z["host"],
                port=z["port"],
                addresses=ranked,
                source=addr[0],
            )
            found[key] = entry
            if on_found:
                try:
                    on_found(entry)
                except Exception:
                    pass
    finally:
        try:
            sock.close()
        except Exception:
            pass

    return list(found.values())


def _now() -> float:
    import time

    return time.monotonic()


def _local_ipv4s() -> List[str]:
    """Best-effort enumeration of non-loopback IPv4 addresses on this host."""
    ips: set[str] = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    # Some systems (esp. Windows) don't expose all NIC IPs via gethostname;
    # also probe via a connected socket trick.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


# --- Background scanner (UI-friendly) ---------------------------------------


class DiscoveryScanner:
    """Run discovery on a background thread without blocking the UI."""

    def __init__(self, timeout_s: float = 5.0) -> None:
        self.timeout_s = timeout_s
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._results: List[DiscoveryEntry] = []
        self._running = False

    def start(self, on_complete: Optional[Callable[[List[DiscoveryEntry]], None]] = None) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True

        def _run() -> None:
            try:
                results = discover(timeout_s=self.timeout_s)
            except Exception:
                results = []
            with self._lock:
                self._results = results
                self._running = False
            if on_complete:
                try:
                    on_complete(results)
                except Exception:
                    pass

        t = threading.Thread(target=_run, name="LV1Discovery", daemon=True)
        self._thread = t
        t.start()
        return True

    @property
    def results(self) -> List[DiscoveryEntry]:
        with self._lock:
            return list(self._results)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running
