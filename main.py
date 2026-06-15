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

from models import AppSettings
from main_window import MainWindow


def main() -> None:
    settings = AppSettings.load()

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

    MainWindow(root, settings)
    root.mainloop()


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
