"""
Standalone CLI to verify the LV1 OSC core works.

Usage:
    python test_lv1_connection.py                 # discover + connect to first LV1, show scenes
    python test_lv1_connection.py 192.168.1.73    # connect to specific IP (auto-discover port)
    python test_lv1_connection.py 192.168.1.73 58762
    python test_lv1_connection.py --recall 2      # connect to first LV1 and recall scene 2

This does NOT touch the UI, audio capture or LTC decoder. It exercises only:
  - zdns_discover (multicast UDP)
  - lv1_osc_client (TCP framing, /handshake, ping/pong, scene tracking)
  - lv1_osc (encode/decode)
"""

from __future__ import annotations

import sys
import time

from lv1_osc_client import ConnectionState, LV1Client, SceneCatalogSnapshot
from zdns_discover import DiscoveryEntry, discover


def main() -> int:
    args = sys.argv[1:]
    recall_idx: int | None = None
    if "--recall" in args:
        i = args.index("--recall")
        try:
            recall_idx = int(args[i + 1])
        except (IndexError, ValueError):
            print("--recall requires an integer index")
            return 2
        del args[i : i + 2]

    host = args[0] if args else None
    port = int(args[1]) if len(args) > 1 else None

    # If port not given, discover.
    if not port:
        print(f"Discovering LV1s on the LAN (5 s)…  filter: host={host or '*'}")
        results = discover(timeout_s=5.0)
        if host:
            results = [r for r in results if host in r.addresses]
        if not results:
            print("No LV1 found. Check that the Waves Remote service is running.")
            return 1
        for r in results:
            print(f"  • {r.host!r:30s}  {r.addresses[0]}:{r.port}  (UUID {r.uuid})")
        chosen = results[0]
        host = chosen.addresses[0]
        port = chosen.port
        print(f"\nConnecting to {chosen.host or '?'} @ {host}:{port}\n")

    client = LV1Client(device_name="LTCtoLV1-test")

    def on_log(level: str, msg: str) -> None:
        print(f"  [{level:5s}] {msg}")

    def on_conn(state: ConnectionState) -> None:
        flag = "REG" if state.registered else ("CON" if state.connected else "OFF")
        err = f"  err={state.last_error}" if state.last_error else ""
        print(f"  [conn ] {flag}  {state.host}:{state.port}{err}")

    def on_catalog(snap: SceneCatalogSnapshot) -> None:
        print(f"  [scene] catalog received ({len(snap.scenes)} scenes):")
        for idx in sorted(snap.scenes):
            print(f"           [{idx:3d}] {snap.scenes[idx]}")

    def on_current(idx: int | None) -> None:
        if idx is None:
            print("  [curr ] (no current scene)")
            return
        name = client.scene_catalog().get(idx, "<unknown>")
        print(f"  [curr ] scene {idx}: {name!r}")

    client.on_log = on_log
    client.on_connection_change = on_conn
    client.on_catalog_change = on_catalog
    client.on_current_scene_change = on_current

    client.connect(host, port)

    # Wait up to 5 s for the registration to complete.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if client.is_connected():
            break
        time.sleep(0.1)

    if not client.is_connected():
        print("\n✗ Failed to connect/register within 5 s.")
        client.disconnect()
        return 1

    # Give it 2 s to receive the scene catalog (sent during handshake).
    time.sleep(2.0)

    cat = client.scene_catalog()
    print(f"\n✓ Connected. Scene catalog has {len(cat)} entries.")
    print(f"  Current scene: {client.current_scene()}")

    if recall_idx is not None:
        if recall_idx not in cat:
            print(f"\n⚠ Scene {recall_idx} is not in the catalog (have: {sorted(cat)}).")
        else:
            print(f"\n→ Recalling scene {recall_idx}: {cat[recall_idx]!r}")
            client.recall_scene(recall_idx)
            time.sleep(1.0)

    print("\nListening for 10 s for live events (Ctrl-C to exit early)…")
    try:
        time.sleep(10.0)
    except KeyboardInterrupt:
        pass

    client.disconnect()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
