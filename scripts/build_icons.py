#!/usr/bin/env python3
"""Regenerate app/static/ icons from design/icon-source.png.

    python scripts/build_icons.py

The source PNG is the design itself, so every output here is a high-quality
downscale of it rather than a redrawn approximation. Re-run this if the
source art changes; the generated files are committed, so the running app
never needs Pillow (it's a dev-only dependency, see requirements-dev.txt).

The one non-obvious bit is the crop. The source is an app-store style icon:
the glyph deliberately sits in a lot of padding, which is correct at
home-screen sizes but leaves it about 38% of the frame — roughly 6px of
artwork in a 16px browser tab, which resolves to an unreadable blob. So the
small sizes are cropped in around the glyph, and only the large ones keep
the full padded frame. The crop is measured from the art rather than
hardcoded, so it still does the right thing if the design is replaced.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "design" / "icon-source.png"
OUT = ROOT / "app" / "static"

# Sizes small enough that the source's padding hurts legibility.
CROPPED_PNG_SIZES = {"favicon-16.png": 16, "favicon-32.png": 32}

# Sizes where the padding is the point — iOS home screen and PWA/dashboard
# icons are masked and shrunk by the OS, and a tight crop would look wrong.
FULL_PNG_SIZES = {
    "apple-touch-icon.png": 180,
    "icon-192.png": 192,
    "icon-512.png": 512,
}

# Multi-resolution .ico for the bare /favicon.ico browsers request on their
# own. Built from the cropped art for the same reason as the small PNGs.
ICO_SIZES = [(16, 16), (32, 32), (48, 48)]

# Fraction of the cropped frame the glyph should span. 0.8 was picked by
# rendering 0.58/0.70/0.80 side by side at 16px: below ~0.7 the broom's
# bristles blur into its head.
GLYPH_COVERAGE = 0.8


def glyph_crop(image: Image.Image) -> Image.Image:
    """A square crop centred on the artwork's light-on-dark glyph.

    Returns the image unchanged if no glyph is detectable, so a source that
    doesn't match this assumption degrades to the old behaviour instead of
    producing garbage.
    """
    # Threshold on luminance, not per channel: the background here is a
    # saturated blue whose blue channel alone is 232, so a per-channel
    # threshold matches the whole canvas.
    mask = image.convert("L").point(lambda v: 255 if v > 200 else 0)
    box = mask.getbbox()
    if box is None:
        print("warning: no glyph detected, using the full frame", file=sys.stderr)
        return image

    left, top, right, bottom = box
    centre_x, centre_y = (left + right) // 2, (top + bottom) // 2
    side = int(max(right - left, bottom - top) / GLYPH_COVERAGE)

    # Keep the crop inside the canvas without letting it drift off-centre
    # enough to clip the glyph.
    side = min(side, image.width, image.height)
    half = side // 2
    centre_x = min(max(centre_x, half), image.width - half)
    centre_y = min(max(centre_y, half), image.height - half)

    print(f"glyph {right - left}x{bottom - top} at ({centre_x},{centre_y}) -> {side}x{side} crop")
    return image.crop((centre_x - half, centre_y - half, centre_x + half, centre_y + half))


def main() -> int:
    if not SOURCE.exists():
        print(f"missing source image: {SOURCE}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    source = Image.open(SOURCE).convert("RGBA")
    if source.width != source.height:
        print(f"warning: source is {source.width}x{source.height}, not square", file=sys.stderr)

    cropped = glyph_crop(source)

    for name, size in FULL_PNG_SIZES.items():
        source.resize((size, size), Image.LANCZOS).save(OUT / name, "PNG", optimize=True)
        print(f"wrote {name} ({size}x{size}, full frame)")

    for name, size in CROPPED_PNG_SIZES.items():
        cropped.resize((size, size), Image.LANCZOS).save(OUT / name, "PNG", optimize=True)
        print(f"wrote {name} ({size}x{size}, cropped)")

    cropped.save(OUT / "favicon.ico", "ICO", sizes=ICO_SIZES)
    print(f"wrote favicon.ico ({', '.join(f'{w}x{h}' for w, h in ICO_SIZES)}, cropped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
