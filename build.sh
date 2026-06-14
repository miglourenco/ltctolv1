#!/usr/bin/env bash
# Build LTCtoLV1 for macOS.
# Run from the project root:  bash build.sh
set -e

echo "=== [1/4] Installing / upgrading dependencies ==="
python3 -m pip install --upgrade sounddevice numpy pyinstaller Pillow

echo ""
echo "=== [2/4] Generating icons ==="
python3 make_icons.py
if [ -d "ltctolv1.iconset" ]; then
    iconutil -c icns ltctolv1.iconset
    echo "Created ltctolv1.icns"
fi

echo ""
echo "=== [3/4] Building LTCtoLV1.app ==="
python3 -m PyInstaller --clean -y ltctolv1_macos.spec

echo ""
echo "=== [4/4] Creating LTCtoLV1.dmg ==="
python3 make_dmg_bg.py 2>/dev/null || true   # background is optional
rm -f dist/LTCtoLV1.dmg

# create-dmg is from homebrew: brew install create-dmg
DMG_ARGS=(
    --volname "LTCtoLV1"
    --window-pos 200 120
    --window-size 600 400
    --icon-size 100
    --icon "LTCtoLV1.app" 150 200
    --hide-extension "LTCtoLV1.app"
    --app-drop-link 450 200
)
if [ -f "dmg_background.png" ]; then
    DMG_ARGS+=(--background "dmg_background.png")
fi

create-dmg "${DMG_ARGS[@]}" "dist/LTCtoLV1.dmg" "dist/LTCtoLV1.app"

echo ""
echo "=== Done! ==="
echo "App bundle : dist/LTCtoLV1.app"
echo "Installer  : dist/LTCtoLV1.dmg"
