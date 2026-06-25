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

# ── web remote: bundle the entire web/ tree (index.html + static/*) ──────────
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
    datas=_sd_datas + _np_datas + _certifi_datas + _icns_src + _web_datas,
    hiddenimports=_np_hidden + [
        'sounddevice',
        'certifi',
        'flask',
        'werkzeug',
        'werkzeug.serving',
        'pystray',
        'pystray._darwin',
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
        # Declare ownership of the .ltcv1 file type so the OS sends
        # double-click + open-with events back to this .app. macOS picks
        # this up automatically the first time the .app is launched.
        'CFBundleDocumentTypes': [
            {
                'CFBundleTypeName': 'LTCtoLV1 cue list',
                'CFBundleTypeRole': 'Editor',
                'LSHandlerRank': 'Owner',
                'CFBundleTypeExtensions': ['ltcv1'],
                'CFBundleTypeIconFile': 'ltctolv1.icns',
                'LSItemContentTypes': ['com.ltctolv1.cuelist'],
            },
        ],
        # Export the UTI so Spotlight / other apps can identify the file type.
        'UTExportedTypeDeclarations': [
            {
                'UTTypeIdentifier': 'com.ltctolv1.cuelist',
                'UTTypeDescription': 'LTCtoLV1 cue list',
                'UTTypeConformsTo': ['public.json'],
                'UTTypeTagSpecification': {
                    'public.filename-extension': ['ltcv1'],
                    'public.mime-type': ['application/json'],
                },
                'UTTypeIconFile': 'ltctolv1.icns',
            },
        ],
    },
)
