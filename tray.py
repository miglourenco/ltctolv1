"""
System tray icon — the "headless" UX surface.

Built on pystray, which spawns its own message loop on a background thread
(run_detached) so tkinter keeps owning the main thread. The menu is rebuilt
on demand whenever pystray re-reads it, so dynamic items like the LAN IP
list stay fresh.

Threading:
  - The pystray callbacks fire on the tray's own thread. Anything that
    touches tk state must be marshalled back via root.after_idle from the
    caller's hooks.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, List, Optional


# Public type aliases for the hooks the caller wires in.
ShowUiHook       = Callable[[], None]
OpenWebHook      = Callable[[], None]
OpenProjectHook  = Callable[[], None]
QuitHook         = Callable[[], None]


class TrayApp:
    """Wraps pystray.Icon + Menu so callers only deal with hooks + start/stop."""

    def __init__(
        self,
        title: str,
        icon_path: str,
        on_show_ui: ShowUiHook,
        on_open_web: OpenWebHook,
        on_open_project: OpenProjectHook,
        on_quit: QuitHook,
        get_lan_ips: Callable[[], List[str]],
        get_web_port: Callable[[], int],
        is_web_enabled: Callable[[], bool],
    ) -> None:
        self._title = title
        self._icon_path = icon_path
        self._on_show_ui = on_show_ui
        self._on_open_web = on_open_web
        self._on_open_project = on_open_project
        self._on_quit = on_quit
        self._get_lan_ips = get_lan_ips
        self._get_web_port = get_web_port
        self._is_web_enabled = is_web_enabled
        self._icon = None  # pystray.Icon | None

    # ─── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> bool:
        """Launch the tray icon on a detached thread. Returns False if the
        pystray import failed or no icon image could be loaded."""
        try:
            import pystray
            from PIL import Image
        except ImportError as exc:
            print(f"[tray] pystray / Pillow not available: {exc}")
            return False

        try:
            image = Image.open(self._icon_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[tray] failed to load icon {self._icon_path}: {exc}")
            return False

        # Menu is a callable so pystray rebuilds it fresh on every popup —
        # important for the live IP list and the changing web-enabled state.
        self._icon = pystray.Icon(
            "LTCtoLV1", image, self._title, menu=pystray.Menu(self._build_menu)
        )
        try:
            self._icon.run_detached()
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[tray] run_detached failed: {exc}")
            self._icon = None
            return False

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def update(self) -> None:
        """Force pystray to re-read the menu (call after web settings change)."""
        if self._icon is None:
            return
        try:
            self._icon.update_menu()
        except Exception:
            pass

    # ─── Menu builder ───────────────────────────────────────────────────

    def _build_menu(self):
        # Late import keeps the hard dependency optional — start() already
        # bailed if pystray is missing.
        import pystray
        items = [
            pystray.MenuItem("Open UI", lambda _i, _it: self._on_show_ui(),
                             default=True),
        ]
        if self._is_web_enabled():
            items.append(
                pystray.MenuItem("Open web remote",
                                 lambda _i, _it: self._on_open_web())
            )
        items.append(
            pystray.MenuItem("Open project…",
                             lambda _i, _it: self._on_open_project())
        )
        items.append(pystray.Menu.SEPARATOR)

        # Non-clickable LAN URL list. pystray renders disabled items as
        # greyed-out so it reads as info-only, not a missing feature.
        port = self._get_web_port()
        ips = self._get_lan_ips()
        web_on = self._is_web_enabled()
        if web_on and ips:
            items.append(_disabled_label("LAN URLs:"))
            for ip in ips:
                items.append(_disabled_label(f"  http://{ip}:{port}/"))
        elif web_on:
            items.append(_disabled_label("Web remote: detecting addresses…"))
        else:
            items.append(_disabled_label("Web remote: disabled"))

        items.append(pystray.Menu.SEPARATOR)
        items.append(
            pystray.MenuItem("Quit", lambda _i, _it: self._on_quit())
        )
        return pystray.Menu(*items)


def _disabled_label(text: str):
    """Non-interactive menu row used for IP listings and section headers."""
    import pystray
    # action=None + enabled=False renders as greyed text, no click.
    return pystray.MenuItem(text, None, enabled=False)


# ─── Icon resolution ────────────────────────────────────────────────────


def default_icon_path() -> Optional[str]:
    """Path to the best tray icon for this platform. Returns None if no icon
    file ships with the build."""
    base = _resource_base()
    if sys.platform == "win32":
        for name in ("ltctolv1.ico", "ltc-lv1-icon.png"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    else:
        # macOS / Linux: prefer the PNG since .icns isn't a PIL-friendly format
        for name in ("ltc-lv1-icon.png", "ltctolv1.ico"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return None


def _resource_base() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))
