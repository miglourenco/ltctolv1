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

    MainWindow(root, settings)
    root.mainloop()


if __name__ == "__main__":
    main()
