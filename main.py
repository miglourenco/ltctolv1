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
from tkinter import messagebox

from models import AppSettings
from main_window import MainWindow


def main() -> None:
    settings = AppSettings.load()

    # CLI flags trump persisted settings — useful for autostart shims and
    # for the operator who wants a one-off run in a specific mode.
    forced_mode = None
    if "--host" in sys.argv:
        forced_mode = "host"
    elif "--remote" in sys.argv:
        forced_mode = "remote"
    start_minimized = "--start-minimized" in sys.argv
    allow_multiple = "--allow-multiple" in sys.argv

    # Single-instance enforcement. If another LTCtoLV1 is already running
    # on this PC, signal it to bring its window forward and exit. The
    # --allow-multiple escape hatch is for the dev workflow where the
    # same machine runs a host AND a remote instance side by side.
    instance = None
    if not allow_multiple:
        from single_instance import SingleInstance
        instance = SingleInstance()
        if not instance.acquire():
            print("[instance] LTCtoLV1 is already running — bringing existing window to front and exiting")
            instance.signal_primary()
            return

    # Create the tk root up front. We may need it as the parent for the
    # mode picker / remote host picker BEFORE we have a controller, so it
    # exists but stays hidden until MainWindow takes over.
    root = tk.Tk()
    root.title("LTC → LV1 Snapshot Recall")
    root.minsize(860, 620)
    _setup_root_dpi_and_icon(root)
    root.withdraw()

    # Resolve mode — flag > saved setting > first-launch picker.
    mode = forced_mode or (settings.mode or "")
    if mode not in ("host", "remote"):
        from mode_picker import ModePicker
        print("[mode] no saved mode — opening picker (host / remote)…")
        try:
            root.update_idletasks()
        except tk.TclError:
            _release_instance(instance)
            return
        picker = ModePicker(root)
        root.wait_window(picker)
        mode = picker.result or ""
        if mode not in ("host", "remote"):
            print("[mode] cancelled")
            root.destroy()
            _release_instance(instance)
            return
        settings.mode = mode
        try:
            settings.save()
        except Exception:
            pass
        print(f"[mode] selected: {mode}")
    else:
        print(f"[mode] using: {mode}")

    # Build the appropriate controller. _start_remote may return None if
    # the operator cancelled the host picker; in that case we bail
    # cleanly without leaving an orphan window behind.
    if mode == "remote":
        controller = _start_remote(root, settings)
        if controller is None:
            root.destroy()
            _release_instance(instance)
            return
    else:
        controller = _start_host(settings)

    window = MainWindow(root, controller)

    # Wire the single-instance signal into the window's "show + focus"
    # path so a second launch raises this one instead of starting fresh.
    if instance is not None:
        instance.set_on_signal(
            lambda: root.after_idle(window._show_and_focus)
        )
        controller.add_shutdown_hook(instance.release)

    # Host-only integrations (file association, tray, file-arg open).
    if mode == "host":
        _register_file_assoc()
        if getattr(settings, "tray_enabled", True):
            _start_tray(controller, window)
        file_arg = _first_existing_file_arg(sys.argv[1:])
        if file_arg:
            root.after(100, lambda: window._load_cue_file(file_arg))

    # Reveal the window now that the controller is wired up.
    root.deiconify()

    if start_minimized and mode == "host":
        root.after(0, window._minimize_to_tray)

    root.mainloop()


# ─── Host mode startup ──────────────────────────────────────────────────


def _start_host(settings: AppSettings):
    """Build a local AppController and bring up the host-side services
    (web server, LAN announcer). The controller is returned ready to be
    handed to MainWindow."""
    from app_controller import AppController
    controller = AppController(settings)

    # Optionally start the built-in web remote. Failures here MUST NOT take
    # down the desktop UI — surface via the status bar and carry on.
    if getattr(settings, "web_enabled", False):
        try:
            from web_server import WebServer
            port = int(getattr(settings, "web_port", 8080))
            web = WebServer(controller, host="0.0.0.0", port=port)
            web.start()
            controller.add_shutdown_hook(web.stop)
            controller.set_status(f"Web remote listening on :{port}")
            # Beacon ourselves on the LAN so other LTCtoLV1 instances in
            # remote mode can find us without manual IP entry. Only worth
            # doing when web is up — that's what the remote will connect to.
            controller.start_lan_announcer()
        except Exception as exc:  # noqa: BLE001
            controller.set_status(f"Web remote disabled: {exc}", warn=True)

    return controller


# ─── Remote mode startup ────────────────────────────────────────────────


def _start_remote(root: tk.Tk, settings: AppSettings):
    """Show the host picker, build a RemoteAppController, and connect.
    Returns the controller on success, or None if the user cancelled."""
    from remote_controller import RemoteAppController
    from remote_picker import RemotePicker

    while True:
        print("[remote] opening host picker — scanning LAN for LTCtoLV1 hosts…")
        # Pump pending tk events so the picker actually gets drawn
        # immediately (important on Windows where a fresh tk root that
        # was withdrawn doesn't service its event queue otherwise).
        try:
            root.update_idletasks()
        except tk.TclError:
            return None
        picker = RemotePicker(
            root,
            default_host=settings.remote_host or "",
            default_port=int(settings.remote_port or 8080),
        )
        root.wait_window(picker)
        if picker.result is None:
            print("[remote] picker cancelled — exiting")
            return None
        host, port = picker.result
        print(f"[remote] connecting to {host}:{port}…")

        controller = RemoteAppController(settings, host, port)
        ok, err = controller.start()
        if ok:
            print(f"[remote] connected to {host}:{port}")
            return controller
        print(f"[remote] connect failed: {err}")
        # Connection failed — surface and let the user pick again or quit.
        messagebox.showerror(
            "Connect failed",
            f"Could not reach {host}:{port}\n\n{err or 'Unknown error'}",
            parent=root,
        )


# ─── Common helpers ─────────────────────────────────────────────────────


def _register_file_assoc() -> None:
    """Best-effort: bind the .ltcv1 extension to this exe (frozen, Windows)."""
    try:
        import file_assoc
        if file_assoc.is_supported() and not file_assoc.is_registered():
            file_assoc.register()
    except Exception:
        pass


def _first_existing_file_arg(args) -> str:
    """Return the first argv entry that points at an existing file, or ''."""
    for a in args:
        if a and not a.startswith("-") and os.path.isfile(a):
            return a
    return ""


def _release_instance(instance) -> None:
    """Best-effort release of the single-instance lock for the aborted
    startup paths (mode picker cancelled, remote host picker cancelled)."""
    if instance is None:
        return
    try:
        instance.release()
    except Exception:
        pass


def _start_tray(controller, window) -> None:
    """Wire up the system tray. Tray callbacks fire on the pystray thread
    and marshal back to tk's main loop via root.after_idle."""
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
        window._tray = tray
        controller.add_shutdown_hook(tray.stop)


def _setup_root_dpi_and_icon(root: tk.Tk) -> None:
    """Apply Windows DPI awareness + load the app icon. Extracted so the
    mode picker / remote picker that pop before MainWindow inherit the
    same look-and-feel."""
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        root.iconbitmap(_resource("ltctolv1.ico"))
    except Exception:
        pass
    if sys.platform == "win32":
        _force_windows_icon(root, _resource("ltctolv1.ico"))


def _force_windows_icon(root, ico_path: str) -> None:
    """Win32: explicitly attach a multi-resolution ICO to the window so the
    taskbar uses it. Safe no-op on failure."""
    try:
        import ctypes
        from ctypes import wintypes

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.restype = ctypes.c_void_p

        if not os.path.isfile(ico_path):
            return
        hwnd = root.winfo_id()

        sm_cx = user32.GetSystemMetrics(49)  # SM_CXSMICON
        sm_cy = user32.GetSystemMetrics(50)  # SM_CYSMICON
        h_small = user32.LoadImageW(
            None, ico_path, IMAGE_ICON, sm_cx, sm_cy, LR_LOADFROMFILE
        )
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
