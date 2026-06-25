"""
Windows file association for the .ltcv1 cue list extension.

Writes per-user registry entries under HKCU\\Software\\Classes so double-
clicking a .ltcv1 file opens it in LTCtoLV1. Per-user means no UAC prompt;
the binding only applies to the current Windows user, which is the right
trade-off for a portable / non-MSI-installed .exe.

macOS handles the association declaratively via Info.plist's
CFBundleDocumentTypes (set in ltctolv1_macos.spec), so this module is a
no-op on non-Windows platforms.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from models import CUE_FILE_DESCRIPTION, CUE_FILE_EXTENSION, WINDOWS_PROGID


def is_supported() -> bool:
    """File association is only implemented for Windows so far."""
    return sys.platform == "win32"


def current_exe_path() -> Optional[str]:
    """Path to the running .exe when frozen, else None (registering python.exe
    against a dev script is messy and we'd rather skip it than do it wrong)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def is_registered() -> bool:
    """True if our ProgID is bound to CUE_FILE_EXTENSION for the current user
    AND the open-command still points at our current .exe path."""
    if not is_supported():
        return False
    exe = current_exe_path()
    if not exe:
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        # Check the extension → ProgID link.
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{CUE_FILE_EXTENSION}") as k:
            progid, _ = winreg.QueryValueEx(k, "")
        if progid != WINDOWS_PROGID:
            return False
        # Check the ProgID's open command still points at this exe.
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{WINDOWS_PROGID}\shell\open\command") as k:
            cmd, _ = winreg.QueryValueEx(k, "")
        return _normalise(exe) in _normalise(cmd)
    except OSError:
        return False


def register(exe_path: Optional[str] = None) -> bool:
    """Register the file association. Returns True on success. Per-user, so no
    admin elevation required. If ``exe_path`` is None, uses the current frozen
    executable (fails for dev/source runs)."""
    if not is_supported():
        return False
    exe = exe_path or current_exe_path()
    if not exe or not os.path.isfile(exe):
        return False
    try:
        import winreg
    except ImportError:
        return False

    open_command = f'"{exe}" "%1"'

    try:
        # 1) .ltcv1 → ProgID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{CUE_FILE_EXTENSION}") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, WINDOWS_PROGID)
            # Content type — helps the "Open with…" dialog group the file sanely
            winreg.SetValueEx(k, "Content Type", 0, winreg.REG_SZ, "application/json")
            winreg.SetValueEx(k, "PerceivedType", 0, winreg.REG_SZ, "text")

        # 2) ProgID friendly name
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{WINDOWS_PROGID}") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, CUE_FILE_DESCRIPTION)
            # FriendlyTypeName is what Explorer shows in the "Type" column.
            winreg.SetValueEx(k, "FriendlyTypeName", 0, winreg.REG_SZ, CUE_FILE_DESCRIPTION)

        # 3) ProgID icon — share the app's exe icon (resource index 0).
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{WINDOWS_PROGID}\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}",0')

        # 4) ProgID open command — the actual double-click action.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{WINDOWS_PROGID}\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, open_command)
    except OSError:
        return False

    # Best-effort: nudge Explorer to refresh its icon cache so the new file
    # type icon shows up without a logout. Failure is harmless.
    _notify_shell_assoc_changed()
    return True


def unregister() -> bool:
    """Remove our registry entries. Returns True if anything was deleted."""
    if not is_supported():
        return False
    try:
        import winreg
    except ImportError:
        return False
    removed_any = False
    for key in [
        rf"Software\Classes\{WINDOWS_PROGID}\shell\open\command",
        rf"Software\Classes\{WINDOWS_PROGID}\shell\open",
        rf"Software\Classes\{WINDOWS_PROGID}\shell",
        rf"Software\Classes\{WINDOWS_PROGID}\DefaultIcon",
        rf"Software\Classes\{WINDOWS_PROGID}",
        rf"Software\Classes\{CUE_FILE_EXTENSION}",
    ]:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            removed_any = True
        except OSError:
            pass
    if removed_any:
        _notify_shell_assoc_changed()
    return removed_any


def _normalise(path: str) -> str:
    """Strip quotes / case-fold so a path comparison survives different
    capitalisation (Windows is case-insensitive for filenames)."""
    return path.replace('"', "").strip().lower()


def _notify_shell_assoc_changed() -> None:
    """Fire SHChangeNotify(SHCNE_ASSOCCHANGED) so Explorer reloads its icon
    cache and the new file-type binding takes effect immediately."""
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
        )
    except Exception:
        pass
