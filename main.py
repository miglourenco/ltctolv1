"""
LTC → LV1 Snapshot Recall (OSC)
Entry point.
"""
import os
import sys


def _resource(name: str) -> str:
    """Return absolute path to a bundled resource (works both frozen and from source)."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS          # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)

# Must be set BEFORE sounddevice is imported anywhere.
# Tells sounddevice to load the ASIO-enabled PortAudio DLL (Windows only).
# On macOS/Linux this env var has no effect; don't set it to avoid spurious warnings.
if sys.platform == "win32":
    os.environ.setdefault("SD_ENABLE_ASIO", "1")
    # Set App User Model ID so Windows shows our icon in the taskbar instead of
    # the generic Python/tkinter feather.
    try:
        from ctypes import windll
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.ltctolv1.app")
    except Exception:
        pass

import tkinter as tk

from app_controller import AppController
from models import AppSettings
from main_window import MainWindow


def main() -> None:
    settings = AppSettings.load()
    controller = AppController(settings)

    # --start-minimized is set by the autostart shim. Brings the app up in
    # the tray with no visible window so it doesn't steal focus on login.
    start_minimized = "--start-minimized" in sys.argv

    # Optionally start the built-in web remote. Failures here MUST NOT take
    # down the desktop UI — surface via the status bar and carry on.
    if getattr(settings, "web_enabled", False):
        try:
            from web_server import WebServer
            port = int(getattr(settings, "web_port", 8080))
            web = WebServer(controller, host="0.0.0.0", port=port)
            web.start()
            # Register graceful shutdown so the SSE streams + werkzeug server
            # exit cleanly when the user closes the app.
            controller.add_shutdown_hook(web.stop)
            controller.set_status(f"Web remote listening on :{port}")
        except Exception as exc:  # noqa: BLE001
            controller.set_status(f"Web remote disabled: {exc}", warn=True)

    root = tk.Tk()
    root.title("LTC → LV1 Snapshot Recall")
    root.minsize(860, 620)

    try:
        # High-DPI awareness on Windows
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    try:
        root.iconbitmap(_resource("ltctolv1.ico"))
    except Exception:
        pass

    # On Windows, force the taskbar icon via Win32 WM_SETICON. tkinter's
    # iconbitmap only sets the title-bar icon reliably; the taskbar falls
    # back to the .exe's embedded icon, which Windows aggressively caches —
    # explicitly pushing the ICO to ICON_SMALL + ICON_BIG bypasses all that.
    if sys.platform == "win32":
        _force_windows_icon(root, _resource("ltctolv1.ico"))

    window = MainWindow(root, controller)

    # Register .ltcv1 file association on first frozen launch (Windows only).
    # Best-effort: silent on failure, since this is a UX nicety, not a hard
    # requirement. macOS handles association declaratively via Info.plist.
    try:
        import file_assoc
        if file_assoc.is_supported() and not file_assoc.is_registered():
            file_assoc.register()
    except Exception:
        pass

    # System tray. The icon stays up for the lifetime of the app so the
    # operator can always send the UI to the tray (and bring it back).
    # Failures are non-fatal: the desktop UI still works without a tray.
    if getattr(settings, "tray_enabled", True):
        _start_tray(controller, window)

    # If the OS invoked us with a file path (double-click on a .ltcv1 file or
    # drag-and-drop onto the .exe), open it now that the window is up.
    file_arg = _first_existing_file_arg(sys.argv[1:])
    if file_arg:
        # Defer slightly so the window finishes laying out before we touch it.
        root.after(100, lambda: window._load_cue_file(file_arg))

    # --start-minimized → withdraw the window before the user sees it.
    if start_minimized:
        root.after(0, window._minimize_to_tray)

    root.mainloop()


def _first_existing_file_arg(args) -> str:
    """Return the first argv entry that points at an existing file, or ''."""
    for a in args:
        if a and not a.startswith("-") and os.path.isfile(a):
            return a
    return ""


def _start_tray(controller, window) -> None:
    """Wire up the system tray with hooks that marshal back to the tk main
    thread (pystray callbacks fire on its own message-loop thread)."""
    try:
        from tray import TrayApp, default_icon_path
    except Exception as exc:  # noqa: BLE001
        print(f"[tray] not available: {exc}")
        return

    icon_path = default_icon_path()
    if not icon_path:
        print("[tray] no icon file found")
        return

    root = window.root

    def _show_ui():
        root.after_idle(window._show_and_focus)

    def _open_web():
        root.after_idle(window._open_web_remote)

    def _open_project():
        # Bring the UI up first so the OS file picker has a parent — picking
        # against a hidden window misbehaves on macOS in particular.
        def _do():
            window._show_and_focus()
            window._open_list()
        root.after_idle(_do)

    def _quit():
        root.after_idle(window._quit_from_tray)

    def _get_ips():
        try:
            from zdns_discover import _local_ipv4s, _rank_ip
            return sorted(set(_local_ipv4s()), key=_rank_ip, reverse=True)
        except Exception:
            return []

    def _get_port():
        return int(getattr(controller.settings, "web_port", 8080))

    def _web_on():
        return bool(getattr(controller.settings, "web_enabled", False))

    tray = TrayApp(
        title="LTC to LV1",
        icon_path=icon_path,
        on_show_ui=_show_ui,
        on_open_web=_open_web,
        on_open_project=_open_project,
        on_quit=_quit,
        get_lan_ips=_get_ips,
        get_web_port=_get_port,
        is_web_enabled=_web_on,
    )
    if tray.start():
        window._tray = tray  # so the MainWindow can update_menu after changes
        controller.add_shutdown_hook(tray.stop)


def _force_windows_icon(root, ico_path: str) -> None:
    """Win32: explicitly attach a multi-resolution ICO to the window so the
    taskbar uses it. Safe no-op on failure."""
    try:
        import ctypes
        from ctypes import wintypes

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.restype = ctypes.c_void_p

        if not os.path.isfile(ico_path):
            return
        hwnd = root.winfo_id()

        # Small (title bar): use system small-icon metric (typically 16x16)
        sm_cx = user32.GetSystemMetrics(49)  # SM_CXSMICON
        sm_cy = user32.GetSystemMetrics(50)  # SM_CYSMICON
        h_small = user32.LoadImageW(
            None, ico_path, IMAGE_ICON, sm_cx, sm_cy, LR_LOADFROMFILE
        )
        # Big (taskbar / Alt-Tab): use system icon metric (typically 32x32)
        bg_cx = user32.GetSystemMetrics(11)  # SM_CXICON
        bg_cy = user32.GetSystemMetrics(12)  # SM_CYICON
        h_big = user32.LoadImageW(
            None, ico_path, IMAGE_ICON, bg_cx, bg_cy, LR_LOADFROMFILE
        )

        if h_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
        if h_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
    except Exception:
        pass


if __name__ == "__main__":
    main()
