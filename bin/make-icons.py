#!/usr/bin/env python3
"""Draw the app icon and the menu-bar icon.

WHY NOT A MAGNIFYING GLASS

Spotlight owns that shape. A menu-bar item and a dock icon that both look like a
lens are read as Spotlight at a glance, and being mistaken for a system feature is
worse than being unfamiliar: the user stops looking for what the app actually does.

THE SHAPE: TWO CONCENTRIC LENSES, HANDLES FORMING A V.

Drawn by the project owner in assets/mark.svg: an outer ring at r=70 and an inner
one at r=58, concentric, with two tapered filled handles at 90 and 45 degrees. The
handles separate downward into a V, which is what distinguishes this from a single
magnifier.

The SVG's geometry lives in assets/mark.svg with the colour left as {{INK}}, so the
same drawing produces a light mark for the dark app icon and a black one for the
menu bar. The original file carried 8 KB of C2PA provenance metadata that renders
nothing; it is stripped rather than shipped in every build.

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


def _render_mark(ink: str, px: int, weight: int = 8) -> Image.Image:
    """Rasterise assets/mark.svg at `px`, in `ink`, on a transparent ground.

    Done through headless Chrome because it is the only SVG renderer on a stock
    macOS machine that handles transforms and arcs correctly. Drawsvg and cairosvg
    are not dependencies of this project and are not worth adding for one file.
    """
    import subprocess
    import tempfile

    tpl = (ROOT / "assets" / "mark.svg").read_text(encoding="utf-8")
    svg = tpl.replace("{{INK}}", ink).replace('stroke-width="8"',
                                               f'stroke-width="{weight}"')
    work = Path(tempfile.mkdtemp())
    (work / "mark.svg").write_text(svg, encoding="utf-8")
    (work / "page.html").write_text(
        f'<!doctype html><html><body style="margin:0;background:transparent">'
        f'<img src="mark.svg" style="display:block;width:{px}px;height:{px}px">'
        f"</body></html>", encoding="utf-8")

    chrome = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    if not chrome.exists():
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        raise SystemExit("  no Chrome or Edge to rasterise the SVG")

    out = work / "out.png"
    subprocess.run([str(chrome), "--headless", "--disable-gpu", "--hide-scrollbars",
                    f"--window-size={px},{px}",
                    "--default-background-color=00000000",
                    f"--screenshot={out}", f"file://{work / 'page.html'}"],
                   capture_output=True)
    return Image.open(out).convert("RGBA").resize((px, px), Image.LANCZOS)


def draw_app_icon(size: int = 1024) -> Image.Image:
    """Dock/Finder icon: the owner's mark in pale ink on a rounded ground."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(size - 1, 1)
        gd.line([(0, y), (size, y)],
                fill=tuple(int(GROUND_TOP[i] * (1 - t) + GROUND_BOTTOM[i] * t)
                           for i in range(3)) + (255,))
    img.paste(grad, (0, 0), rounded_mask(size, int(size * 0.225)))

    # FIT BY THE INK'S ACTUAL BOUNDS, not by its viewBox.
    #
    # The SVG's handles are rotated paths that extend BELOW y=256: the drawing is
    # not centred in its own 256x256 box. Trusting the box put the mark small and
    # up-left, with the V clipped at the bottom edge.
    #
    # Rendering large, measuring the alpha bounding box, and scaling that box into
    # the icon centres it whatever the source file does.
    canvas = int(size * 0.74)          # the mark occupies 74% of the tile
    big = _render_mark("#ECE9F5", size)
    box = big.getbbox()
    if box:
        big = big.crop(box)
    # Keep the aspect ratio: a lens with a V handle is taller than it is wide.
    w, h = big.size
    scale = canvas / max(w, h)
    big = big.resize((max(int(w * scale), 1), max(int(h * scale), 1)), Image.LANCZOS)
    img.paste(big, ((size - big.width) // 2, (size - big.height) // 2), big)
    return img


def draw_tray_icon(size: int = 44) -> Image.Image:
    """Menu-bar icon: the same mark, black on transparent.

    macOS masks menu-bar template images to a single colour, so any hue is
    discarded - black plus alpha is the only thing that survives. Rendered at twice
    the target size and downsampled, because a 22px raster of a 70px ring aliases.
    """
    # Same fitting as the app icon, for the same reason.
    # A HEAVIER STROKE FOR THE MENU BAR.
    #
    # The source is stroke-width 8 in a 256 box, which is 0.69px once the mark is
    # 22px tall - the double ring collapses into one faint line. 14 lands at about
    # 1.5px, which is what the surrounding system icons use. Judged by rendering at
    # the real size.
    big = _render_mark("#000000", size * 2, weight=14)
    box = big.getbbox()
    if box:
        big = big.crop(box)
    target = int(size * 0.92)
    w, h = big.size
    scale = target / max(w, h)
    big = big.resize((max(int(w * scale), 1), max(int(h * scale), 1)), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(big, ((size - big.width) // 2, (size - big.height) // 2), big)
    return out


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
