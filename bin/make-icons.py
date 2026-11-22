#!/usr/bin/env python3
"""Draw the app icon and the menu-bar icon.

WHY NOT A MAGNIFYING GLASS

Spotlight owns that shape. A menu-bar item and a dock icon that both look like a
lens are read as Spotlight at a glance, and being mistaken for a system feature is
worse than being unfamiliar: the user stops looking for what the app actually does.

THE SHAPE: TWO LENSES, HANDLES FORMING A V.

An outer lens with a handle going down-right, and inside its glass a second, smaller
lens whose handle breaks out down-left. The two handles meet at the bottom of the
mark and separate, which is what distinguishes this from a single magnifier.

It reads as "search inside what you searched" - which is what the app does: it looks
inside files, inside pictures, inside recordings.

WHY THIS AND NOT THE EARLIER ATTEMPTS

  a single magnifier     indistinguishable from Spotlight at a glance
  three stacked bars     a HAMBURGER MENU; in a menu bar that means "open a menu"
  2x2 grid, one lit      clear at 176px, mush at 22 - outlines dissolve when the
                         44px source is downsampled, and thickening merges them
  a frame with a dot     legible, but said nothing about searching

TWO lenses are distinctive where ONE is not, and the V of handles is a shape no
system icon uses.

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


def _lens(d, cx, cy, r, stroke, handle_to, colour, inner_colour=None):
    """A circle plus a handle from its rim toward `handle_to`."""
    import math
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=colour, width=stroke)

    # The handle starts on the rim, not at the centre: a line drawn from the centre
    # would be hidden behind the glass and leave a stub.
    ang = math.atan2(handle_to[1] - cy, handle_to[0] - cx)
    x0 = cx + math.cos(ang) * (r + stroke * 0.15)
    y0 = cy + math.sin(ang) * (r + stroke * 0.15)
    d.line([(x0, y0), handle_to], fill=colour, width=stroke)


def draw_app_icon(size: int = 1024) -> Image.Image:
    """Dock/Finder icon: two lenses on a rounded ground."""
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

    stroke = int(size * 0.058)
    # Same geometry as the tray icon, so the two read as the same mark.
    _lens(d, 0.44 * size, 0.40 * size, 0.300 * size, stroke,
          (0.92 * size, 0.90 * size), SLAB)
    # The inner lens is accented: at dock size there is room for a focal point,
    # and it is the difference between "a magnifier" and "a magnifier with
    # something in it".
    _lens(d, 0.47 * size, 0.43 * size, 0.100 * size, int(stroke * 0.85),
          (0.10 * size, 0.92 * size), ACCENT)
    return img


def draw_tray_icon(size: int = 44) -> Image.Image:
    """Menu-bar icon: the same two lenses, black + alpha only.

    Drawn at 2x (44) so Retina screens do not upscale a 22px bitmap. The stroke is
    the thinnest that still survives being downsampled to 22 - measured by looking
    at the render, not by arithmetic.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # These numbers were chosen by rendering three spacings at 22px and looking at
    # them side by side. The first attempt put the inner circle 0.03 from the outer
    # ring, which downsampled into one blob; 0.045 keeps them separate.
    stroke = max(int(size * 0.085), 2)
    _lens(d, 0.44 * size, 0.40 * size, 0.300 * size, stroke,
          (0.92 * size, 0.90 * size), (0, 0, 0, 255))
    _lens(d, 0.47 * size, 0.43 * size, 0.100 * size, max(int(stroke * 0.85), 2),
          (0.10 * size, 0.92 * size), (0, 0, 0, 255))
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
