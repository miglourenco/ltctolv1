# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for LTCtoLV1 (Windows .exe).
Usage: pyinstaller ltctolv1.spec
       (or just run build.bat)
"""
import os
from PyInstaller.utils.hooks import (
    collect_data_files, collect_dynamic_libs, collect_all
)

block_cipher = None

# ── sounddevice: bundle data files + both PortAudio DLLs ─────────────────────
# (libportaudio64bit.dll  and  libportaudio64bit-asio.dll)
_sd_datas = collect_data_files('sounddevice', include_py_files=False)
_sd_bins = collect_dynamic_libs('sounddevice')

# ── numpy: collect everything (avoids missing-import errors on numpy 2.x) ────
_np_datas, _np_bins, _np_hidden = collect_all('numpy')

# ── certifi: CA bundle for SSL in the GitHub update checker ──────────────────
_certifi_datas = collect_data_files('certifi')

# ── optional icon ─────────────────────────────────────────────────────────────
_ico_src = [('ltctolv1.ico', '.')] if os.path.exists('ltctolv1.ico') else []
_ico_path = 'ltctolv1.ico' if os.path.exists('ltctolv1.ico') else None

# ── web remote: bundle the entire web/ tree (index.html + static/*) ──────────
# WebServer reads from sys._MEIPASS/web at runtime when frozen.
_web_datas = []
if os.path.isdir('web'):
    for root, _dirs, files in os.walk('web'):
        for f in files:
            src = os.path.join(root, f)
            dest = os.path.dirname(src)  # preserve relative directory layout
            _web_datas.append((src, dest))

# ─────────────────────────────────────────────────────────────────────────────

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=_sd_bins + _np_bins,
    datas=_sd_datas + _np_datas + _certifi_datas + _ico_src + _web_datas,
    hiddenimports=_np_hidden + [
        'sounddevice',
        'certifi',
        'ctypes',
        'ctypes.wintypes',
        'flask',
        'werkzeug',
        'werkzeug.serving',
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'PIL', 'pandas',
        'IPython', 'jupyter', 'notebook',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'wx', 'gtk',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LTCtoLV1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-packed PyInstaller binaries are one of the strongest single
    # triggers for SmartScreen / antivirus false positives — packers
    # are heuristically associated with malware self-extraction. The
    # resulting .exe is ~30-40 MB larger but downloads cleanly and
    # runs without "this file is dangerous" prompts on most setups.
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ico_path,
    # Embed Windows VS_VERSION_INFO so Explorer / SmartScreen can show
    # CompanyName + ProductName instead of "Unknown publisher". Helps
    # the file build reputation more quickly across downloads.
    version='version_info.txt',
)
