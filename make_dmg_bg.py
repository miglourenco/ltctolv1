"""
Generate the DMG installer background (600 x 400 px).
Icons in create-dmg are placed at (150, 200) and (450, 200) — 100 px size.
Logo lives in the top ~165 px; the lower ~235 px is the drag zone.
Run once:  python3 make_dmg_bg.py
"""
from PIL import Image, ImageDraw, ImageFont
import os

W, H = 600, 400

CYAN   = (0,   200, 230)
ORANGE = (255, 130, 30)
WHITE  = (230, 230, 230)
DIM    = (100, 100, 100)

img  = Image.new("RGBA", (W, H), (0, 0, 0, 255))
draw = ImageDraw.Draw(img)

# ── logo in top band (scaled to 165 px tall) ──────────────────────────────────
LOGO_H  = 165
logo_raw = Image.open(os.path.join(os.path.dirname(__file__), "logo.png")).convert("RGBA")
logo_w   = int(logo_raw.width * LOGO_H / logo_raw.height)
logo     = logo_raw.resize((logo_w, LOGO_H), Image.LANCZOS)
lx = (W - logo_w) // 2
img.paste(logo, (lx, 0), logo)

# Fade the logo bottom edge into black
fade_start = LOGO_H - 40
for y in range(fade_start, LOGO_H + 10):
    t     = (y - fade_start) / 50
    alpha = int(min(t, 1.0) * 255)
    draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))

draw = ImageDraw.Draw(img)   # refresh after paste

# ── thin divider ──────────────────────────────────────────────────────────────
DIV_Y = LOGO_H + 4
draw.rectangle([50, DIV_Y, W - 50, DIV_Y], fill=(35, 35, 35, 255))

# ── fonts ─────────────────────────────────────────────────────────────────────
try:
    font_label = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 13)
    font_hint  = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 11)
except Exception:
    font_label = font_hint = ImageFont.load_default()

# ── labels under the icon drop zones (icons centred at y=200) ────────────────
LABEL_Y = 295   # below 100-px icons whose centres are at y=200

lbl1 = "LTCtoMIDI"
b1   = draw.textbbox((0, 0), lbl1, font=font_label)
draw.text((150 - (b1[2] - b1[0]) // 2, LABEL_Y), lbl1,
          font=font_label, fill=CYAN + (255,))

lbl2 = "Applications"
b2   = draw.textbbox((0, 0), lbl2, font=font_label)
draw.text((450 - (b2[2] - b2[0]) // 2, LABEL_Y), lbl2,
          font=font_label, fill=ORANGE + (255,))

# ── cyan → orange gradient arrow ─────────────────────────────────────────────
AX1, AX2, AY = 218, 372, 207
for i in range(AX2 - AX1 - 18):
    t = i / (AX2 - AX1 - 18)
    r = int(CYAN[0] + (ORANGE[0] - CYAN[0]) * t)
    g = int(CYAN[1] + (ORANGE[1] - CYAN[1]) * t)
    b = int(CYAN[2] + (ORANGE[2] - CYAN[2]) * t)
    draw.line([(AX1 + i, AY - 2), (AX1 + i, AY + 2)], fill=(r, g, b, 255))

# arrowhead
draw.polygon([(AX2, AY), (AX2 - 16, AY - 9), (AX2 - 16, AY + 9)],
             fill=ORANGE + (255,))

# ── hint ─────────────────────────────────────────────────────────────────────
hint = "drag to install"
bh   = draw.textbbox((0, 0), hint, font=font_hint)
draw.text(((W - (bh[2] - bh[0])) // 2, H - 18),
          hint, font=font_hint, fill=(50, 50, 50, 255))

# ── save ─────────────────────────────────────────────────────────────────────
out = os.path.join(os.path.dirname(__file__), "dmg_background.png")
img.convert("RGB").save(out, "PNG")
print(f"Saved {W}×{H} → {out}")
