"""
Autostart-on-login support for LTCtoLV1.

Windows: writes HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.
macOS:   writes ~/Library/LaunchAgents/com.ltctolv1.app.plist.

Both per-user (no admin / sudo needed). The registered command always
includes the ``--start-minimized`` flag so the app comes up in the tray
instead of stealing focus on every login.
"""

from __future__ import annotations

import os
import sys
from typing import Optional


_RUN_KEY_NAME = "LTCtoLV1"
_LAUNCH_AGENT_LABEL = "com.ltctolv1.app"


def is_supported() -> bool:
    return sys.platform in ("win32", "darwin")


def current_exe_path() -> Optional[str]:
    """Path to the running app:
      - Windows frozen: sys.executable (the .exe)
      - macOS frozen:   the .app bundle's outer path (so `open -a` works)
      - dev:            None — we won't register a Python script
    """
    if not getattr(sys, "frozen", False):
        return None
    if sys.platform == "darwin":
        # sys.executable is something like
        # /Applications/LTCtoLV1.app/Contents/MacOS/LTCtoLV1
        # but LaunchAgent should reference the .app bundle so macOS treats
        # it as a proper application launch (icon, dock behaviour, etc).
        exe = sys.executable
        idx = exe.find(".app/")
        if idx != -1:
            return exe[: idx + 4]
        return exe
    return sys.executable


# ─── Windows ────────────────────────────────────────────────────────────


def _windows_command(exe: str, start_minimized: bool) -> str:
    flag = " --start-minimized" if start_minimized else ""
    return f'"{exe}"{flag}'


def _windows_is_enabled(exe: Optional[str]) -> bool:
    if not exe:
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as k:
            val, _ = winreg.QueryValueEx(k, _RUN_KEY_NAME)
        return exe.lower() in val.lower()
    except (ImportError, OSError):
        return False


def _windows_enable(exe: str, start_minimized: bool = True) -> bool:
    try:
        import winreg
        cmd = _windows_command(exe, start_minimized)
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as k:
            winreg.SetValueEx(k, _RUN_KEY_NAME, 0, winreg.REG_SZ, cmd)
        return True
    except (ImportError, OSError):
        return False


def _windows_disable() -> bool:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        ) as k:
            winreg.DeleteValue(k, _RUN_KEY_NAME)
        return True
    except (ImportError, OSError):
        return False


# ─── macOS ──────────────────────────────────────────────────────────────


def _launch_agent_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{_LAUNCH_AGENT_LABEL}.plist")


def _macos_is_enabled(_exe: Optional[str]) -> bool:
    return os.path.isfile(_launch_agent_path())


def _macos_enable(exe: str, start_minimized: bool = True) -> bool:
    """Write a minimal LaunchAgent plist that opens the .app at login."""
    plist_path = _launch_agent_path()
    args_xml = "        <string>--start-minimized</string>\n" if start_minimized else ""
    # `open -a <bundle>` is the canonical way to launch a .app at login;
    # invoking the inner Mach-O directly bypasses Launch Services and breaks
    # the dock / activation policy.
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-a</string>
        <string>{exe}</string>
        <string>--args</string>
{args_xml}    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
    try:
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        with open(plist_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return True
    except OSError:
        return False


def _macos_disable() -> bool:
    path = _launch_agent_path()
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


# ─── Public API ─────────────────────────────────────────────────────────


def is_enabled() -> bool:
    """True if autostart is currently registered AND points at this exe."""
    exe = current_exe_path()
    if sys.platform == "win32":
        return _windows_is_enabled(exe)
    if sys.platform == "darwin":
        return _macos_is_enabled(exe)
    return False


def enable(start_minimized: bool = True) -> bool:
    """Register autostart for the current user. Returns True on success.
    Quietly fails when run from source (no frozen exe to register)."""
    exe = current_exe_path()
    if not exe:
        return False
    if sys.platform == "win32":
        return _windows_enable(exe, start_minimized)
    if sys.platform == "darwin":
        return _macos_enable(exe, start_minimized)
    return False


def disable() -> bool:
    if sys.platform == "win32":
        return _windows_disable()
    if sys.platform == "darwin":
        return _macos_disable()
    return False
