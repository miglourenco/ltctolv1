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

# ─────────────────────────────────────────────────────────────────────────────

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=_sd_bins + _np_bins,
    datas=_sd_datas + _np_datas + _certifi_datas + _ico_src,
    hiddenimports=_np_hidden + [
        'sounddevice',
        'certifi',
        'ctypes',
        'ctypes.wintypes',
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
    upx=True,
    # Don't compress PortAudio DLLs — UPX breaks some audio DLLs
    upx_exclude=[
        'libportaudio64bit.dll',
        'libportaudio64bit-asio.dll',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ico_path,
)
