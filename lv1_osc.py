"""
Minimal OSC encoder / decoder + Waves LV1 TCP framing.

Why a custom impl instead of python-osc?
  python-osc only speaks OSC-over-UDP and doesn't know about the
  Waves LV1's proprietary 4-byte-length + 8-byte-header TCP framing.
  We need that framing to talk to the LV1 at all, and we only need a
  handful of OSC types in practice (int32, float, double, string, bool,
  int64, blob) — a few hundred lines covers it.

Wire format (TCP):
    [ 4-byte length BE ]  [ 8-byte header ]  [ OSC payload ]
  where length = size of the OSC payload only (NOT including the header).
  The default header is 00 00 00 02 00 00 00 00 — observed in every
  live MyFOH / MyMon capture.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, List, Tuple, Union


# --- OSC encoding -----------------------------------------------------------

OscArgValue = Union[int, float, str, bool, bytes, None]


@dataclass
class OscArg:
    """One OSC argument. `type` is a single-char OSC type tag.

    Supported tags:
      i  int32      (value: int)
      h  int64      (value: int)
      f  float32    (value: float)
      d  float64    (value: float)
      s  string     (value: str)
      b  blob       (value: bytes)
      T  true       (value: ignored)
      F  false      (value: ignored)
      N  null       (value: ignored)
    """

    type: str
    value: OscArgValue = None


@dataclass
class OscMessage:
    address: str
    args: List[OscArg] = field(default_factory=list)


def _pad4(b: bytes) -> bytes:
    """OSC pads strings/blobs to a 4-byte boundary with zeros."""
    pad = (4 - (len(b) % 4)) % 4
    return b + (b"\x00" * pad)


def _encode_string(s: str) -> bytes:
    return _pad4(s.encode("utf-8") + b"\x00")


def _encode_blob(b: bytes) -> bytes:
    return _pad4(struct.pack(">I", len(b)) + b)


def encode_message(address: str, args: List[OscArg]) -> bytes:
    """Encode one OSC message to bytes (no TCP framing)."""
    out = _encode_string(address)
    tags = b"," + "".join(a.type for a in args).encode("ascii")
    out += _encode_string(tags.decode("ascii"))
    for a in args:
        t = a.type
        v = a.value
        if t == "i":
            out += struct.pack(">i", int(v))
        elif t == "h":
            out += struct.pack(">q", int(v))
        elif t == "f":
            out += struct.pack(">f", float(v))
        elif t == "d":
            out += struct.pack(">d", float(v))
        elif t == "s":
            out += _encode_string(str(v))
        elif t == "b":
            out += _encode_blob(bytes(v))  # type: ignore[arg-type]
        elif t in ("T", "F", "N"):
            pass  # no payload
        else:
            raise ValueError(f"Unsupported OSC type tag: {t!r}")
    return out


# --- OSC decoding -----------------------------------------------------------


def _decode_string(buf: bytes, offset: int) -> Tuple[str, int]:
    end = buf.find(b"\x00", offset)
    if end < 0:
        raise ValueError("OSC string not null-terminated")
    s = buf[offset:end].decode("utf-8", errors="replace")
    # Skip past the null + any padding to the next 4-byte boundary
    next_off = end + 1
    while next_off % 4 != 0:
        next_off += 1
    return s, next_off


def _decode_blob(buf: bytes, offset: int) -> Tuple[bytes, int]:
    (n,) = struct.unpack(">I", buf[offset : offset + 4])
    offset += 4
    blob = buf[offset : offset + n]
    offset += n
    while offset % 4 != 0:
        offset += 1
    return blob, offset


def decode_packet(packet: bytes) -> OscMessage:
    """Decode one OSC message from raw bytes (no TCP framing)."""
    address, offset = _decode_string(packet, 0)
    if offset >= len(packet):
        return OscMessage(address=address, args=[])
    tags, offset = _decode_string(packet, offset)
    if not tags.startswith(","):
        return OscMessage(address=address, args=[])
    args: List[OscArg] = []
    for tag in tags[1:]:
        if tag == "i":
            (v,) = struct.unpack(">i", packet[offset : offset + 4])
            offset += 4
            args.append(OscArg("i", v))
        elif tag == "h":
            (v,) = struct.unpack(">q", packet[offset : offset + 8])
            offset += 8
            args.append(OscArg("h", v))
        elif tag == "f":
            (v,) = struct.unpack(">f", packet[offset : offset + 4])
            offset += 4
            args.append(OscArg("f", v))
        elif tag == "d":
            (v,) = struct.unpack(">d", packet[offset : offset + 8])
            offset += 8
            args.append(OscArg("d", v))
        elif tag == "s":
            s, offset = _decode_string(packet, offset)
            args.append(OscArg("s", s))
        elif tag == "b":
            blob, offset = _decode_blob(packet, offset)
            args.append(OscArg("b", blob))
        elif tag == "T":
            args.append(OscArg("T", True))
        elif tag == "F":
            args.append(OscArg("F", False))
        elif tag == "N":
            args.append(OscArg("N", None))
        else:
            # Unknown tag — stop parsing gracefully
            break
    return OscMessage(address=address, args=args)


# --- Convenience constructors ------------------------------------------------


def int_arg(n: int) -> OscArg:
    return OscArg("i", int(n))


def str_arg(s: str) -> OscArg:
    return OscArg("s", str(s))


def bool_arg(b: bool) -> OscArg:
    return OscArg("T" if b else "F")


def int_value(arg: OscArg) -> int | None:
    if arg.type in ("i", "h") and isinstance(arg.value, int):
        return arg.value
    return None


def str_value(arg: OscArg) -> str | None:
    if arg.type == "s" and isinstance(arg.value, str):
        return arg.value
    return None


# --- TCP framing -------------------------------------------------------------

LV1_HEADER = bytes([0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00])
HEADER_LEN = 8


def frame_message(payload: bytes, header: bytes = LV1_HEADER) -> bytes:
    """Wrap a single OSC payload in the LV1's TCP framing."""
    return struct.pack(">I", len(payload)) + header + payload


def frame_batch(payloads: List[bytes], header: bytes = LV1_HEADER) -> bytes:
    """Wrap multiple OSC payloads as one TCP write (LV1 requires this for
    /handshake + /device_name and similar batched messages)."""
    out = b""
    for p in payloads:
        out += struct.pack(">I", len(p)) + header + p
    return out


def try_extract_frame(rx_buf: bytes) -> Tuple[bytes | None, bytes | None, bytes]:
    """Try to extract one complete OSC frame from a TCP rx buffer.

    Returns (header, payload, remaining_buf). header/payload are None if
    not enough bytes yet. On size-out-of-range we drop one byte and retry
    on the next call (resync).
    """
    if len(rx_buf) < 4 + HEADER_LEN:
        return None, None, rx_buf
    (size,) = struct.unpack(">I", rx_buf[:4])
    if size == 0 or size > 16 * 1024 * 1024:
        # Bad size — drop one byte to attempt resync.
        return None, None, rx_buf[1:]
    total = 4 + HEADER_LEN + size
    if len(rx_buf) < total:
        return None, None, rx_buf
    header = rx_buf[4 : 4 + HEADER_LEN]
    payload = rx_buf[4 + HEADER_LEN : total]
    return header, payload, rx_buf[total:]
