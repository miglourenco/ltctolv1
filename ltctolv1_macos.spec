# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for LTCtoLV1 — macOS (.app bundle).
Usage:
    python -m PyInstaller --clean ltctolv1_macos.spec
    (or just run build.sh)

Before running this on your Mac, build the .icns from the iconset:
    iconutil -c icns ltctolv1.iconset
"""
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_all

block_cipher = None

# ── sounddevice: bundle data files + PortAudio dylib ─────────────────────────
_sd_datas = collect_data_files('sounddevice', include_py_files=False)
_sd_bins = collect_dynamic_libs('sounddevice')

# ── numpy: collect everything (avoids missing-import errors on numpy 2.x) ────
_np_datas, _np_bins, _np_hidden = collect_all('numpy')

# ── certifi: CA bundle for SSL in the GitHub update checker ──────────────────
_certifi_datas = collect_data_files('certifi')

# ── optional icon ─────────────────────────────────────────────────────────────
_icns_src = [('ltctolv1.icns', '.')] if os.path.exists('ltctolv1.icns') else []
_icns_path = 'ltctolv1.icns' if os.path.exists('ltctolv1.icns') else None

# ─────────────────────────────────────────────────────────────────────────────

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=_sd_bins + _np_bins,
    datas=_sd_datas + _np_datas + _certifi_datas + _icns_src,
    hiddenimports=_np_hidden + [
        'sounddevice',
        'certifi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'PIL', 'pandas',
        'IPython', 'jupyter', 'notebook',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'wx', 'gtk',
        # Windows-only — not needed on macOS
        'ctypes.wintypes',
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LTCtoLV1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icns_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='LTCtoLV1',
)

app = BUNDLE(
    coll,
    name='LTCtoLV1.app',
    icon=_icns_path,
    bundle_identifier='com.ltctolv1.app',
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'NSHighResolutionCapable': True,
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleName': 'LTCtoLV1',
        'NSMicrophoneUsageDescription': 'LTCtoLV1 needs audio input to read LTC timecode.',
    },
)
