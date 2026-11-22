#!/usr/bin/env python3
"""Draw the app icon and the menu-bar icon.

WHY NOT A MAGNIFYING GLASS

Spotlight owns that shape. A menu-bar item and a dock icon that both look like a
lens are read as Spotlight at a glance, and being mistaken for a system feature is
worse than being unfamiliar: the user stops looking for what the app actually does.

THE SHAPE: A FRAME WITH A DOT INSIDE.

A rounded square outline containing a filled dot. It reads as "something is in
here, and this marks it" - the idea of a search without the shape of one.

THREE SHAPES WERE TRIED AND REJECTED BY RENDERING THEM AT 22 PIXELS, which is the
only size the menu bar ever uses:

  a magnifying glass      reads as Spotlight - the one thing this must not be
                          confused with
  three stacked bars      a HAMBURGER MENU. In a menu bar that means "open a menu
                          here", which is actively misleading
  a 2x2 grid, one lit     clear at 176px, mush at 22. Outlines at 3px in a 44px
                          icon become 1.5px after downsampling and the cells
                          dissolve; thickened, they merge into four blobs

Two elements survive 22px. Four do not. That is the whole constraint, and it is
only visible by looking at the render rather than at the arithmetic.

    app icon      rounded square, warm ground, three pale slabs, one accent dot
    tray icon     same three slabs in BLACK + ALPHA ONLY, because macOS menu-bar
                  template images are masked by the system: any colour is ignored
                  and only the alpha survives. A coloured icon here renders as a
                  grey blob, which is what shipped before and why it looked wrong.

Run from the repository root:
    ./.venv/bin/python bin/make-icons.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "src-tauri" / "icons"

# The accent, used in the app icon only. A warm orange avoids the blue that almost
# every search product uses, and is distinct from Spotlight's grey.
ACCENT = (232, 122, 58, 255)
GROUND_TOP = (38, 32, 44, 255)
GROUND_BOTTOM = (22, 18, 28, 255)
SLAB = (236, 233, 245, 255)


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw_app_icon(size: int = 1024) -> Image.Image:
    """The dock/Finder icon: ground, frame, dot."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(size - 1, 1)
        gd.line([(0, y), (size, y)],
                fill=tuple(int(GROUND_TOP[i] * (1 - t) + GROUND_BOTTOM[i] * t)
                           for i in range(3)) + (255,))
    img.paste(grad, (0, 0), rounded_mask(size, int(size * 0.225)))

    d = ImageDraw.Draw(img)
    margin = int(size * 0.26)
    stroke = int(size * 0.075)
    d.rounded_rectangle([margin, margin, size - margin, size - margin],
                        radius=int(size * 0.10), outline=SLAB, width=stroke)
    r = int(size * 0.115)
    c = size // 2
    d.ellipse([c - r, c - r, c + r, c + r], fill=ACCENT)
    return img


def draw_tray_icon(size: int = 44) -> Image.Image:
    """The menu-bar icon: black + alpha only, because macOS masks it.

    Drawn at 2x (44) so Retina screens do not upscale a 22px bitmap. The stroke is
    a little over one pixel at 22px, which is the thinnest that stays solid after
    downsampling.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    margin = int(size * 0.16)
    stroke = max(int(size * 0.105), 2)
    d.rounded_rectangle([margin, margin, size - margin, size - margin],
                        radius=int(size * 0.13), outline=(0, 0, 0, 255), width=stroke)
    r = max(int(size * 0.155), 2)
    c = size // 2
    d.ellipse([c - r, c - r, c + r, c + r], fill=(0, 0, 0, 255))
    return img


def main() -> int:
    ICONS.mkdir(parents=True, exist_ok=True)

    app = draw_app_icon(1024)
    app.save(ICONS / "icon.png")
    print(f"  wrote {ICONS/'icon.png'} ({app.size[0]}x{app.size[1]})")

    tray = draw_tray_icon(44)
    tray.save(ICONS / "tray.png")
    print(f"  wrote {ICONS/'tray.png'} ({tray.size[0]}x{tray.size[1]}, black + alpha)")

    # .icns is what the bundle actually uses. iconutil needs an .iconset folder
    # containing every size, and refuses anything else.
    iconset = ICONS / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for px in (16, 32, 128, 256, 512):
        app.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
        app.resize((px * 2, px * 2), Image.LANCZOS).save(iconset / f"icon_{px}x{px}@2x.png")
    out = ICONS / "icon.icns"
    r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  wrote {out}")
    else:
        print(f"  iconutil failed: {r.stderr.strip()[:120]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
