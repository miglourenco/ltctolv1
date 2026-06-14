"""
Convert the app icon PNG to .ico (Windows) and an iconset folder (macOS).

Source: ltc-lv1-icon.png  →  ltctolv1.ico  +  ltctolv1.iconset/

Usage:
    python make_icons.py

On macOS, after running this script, also run:
    iconutil -c icns ltctolv1.iconset
"""
import os
import sys
from pathlib import Path
from PIL import Image

SRC = "ltc-lv1-icon.png"

img = Image.open(SRC).convert("RGBA")

# ── Center-crop to square ─────────────────────────────────────────────────────
w, h = img.size
side = min(w, h)
left = (w - side) // 2
top = (h - side) // 2
img = img.crop((left, top, left + side, top + side))

# ── Windows .ico ──────────────────────────────────────────────────────────────
ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
frames = [img.resize(s, Image.LANCZOS) for s in ico_sizes]
frames[0].save(
    "ltctolv1.ico",
    format="ICO",
    sizes=ico_sizes,
    append_images=frames[1:],
)
print("Created ltctolv1.ico")

# ── macOS iconset (run  iconutil -c icns ltctolv1.iconset  on a Mac) ──────────
iconset = Path("ltctolv1.iconset")
iconset.mkdir(exist_ok=True)

mac_sizes = {
    "icon_16x16.png":       16,
    "icon_16x16@2x.png":    32,
    "icon_32x32.png":       32,
    "icon_32x32@2x.png":    64,
    "icon_128x128.png":    128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png":    256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png":    512,
    "icon_512x512@2x.png": 1024,
}

for fname, size in mac_sizes.items():
    img.resize((size, size), Image.LANCZOS).save(iconset / fname)

print("Created ltctolv1.iconset/")

if sys.platform == "darwin":
    ret = os.system("iconutil -c icns ltctolv1.iconset")
    if ret == 0:
        print("Created ltctolv1.icns")
    else:
        print("iconutil failed — run manually: iconutil -c icns ltctolv1.iconset")
else:
    print("On macOS run: iconutil -c icns ltctolv1.iconset")
