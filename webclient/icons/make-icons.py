#!/usr/bin/env python3
"""Generates the PWA install icons for this cockpit's chassis theme.

Deterministic (no fonts, no randomness, no external assets) so a downstream
fork can rebrand by editing the colors/glyph below and re-running this —
that's the point of committing it instead of just the PNGs.

Draws a patch-jack glyph (this UI's own visual language: the deck is a
"patchbay") on the instrument theme's own chassis colors, read here as
literal hex values copied from the `:root` block in index.html rather than
parsed from it, since duplicating four color constants is simpler and more
robust than a CSS parser for a one-off generator.

Usage: python3 make-icons.py [--out DIR]
"""

import argparse
import os

from PIL import Image, ImageDraw

# Colors, copied from index.html's :root (the "instrument" theme's chassis).
ROOM = "#0d0b09"       # dark chassis field -- icon background
METAL_HI = "#a89c88"   # bezel ring
AMBER = "#ff9e3d"      # jack barrel / accent
INK = "#241f1a"        # jack bore (the "hole")

SIZES = {
    "icon-192.png": (192, 0.0),
    "icon-512.png": (512, 0.0),
    "icon-512-maskable.png": (512, 0.16),  # maskable safe-zone padding
    "apple-touch-icon-180.png": (180, 0.0),
}


def draw_jack(size: int, padding: float) -> Image.Image:
    """A single 1/4" patch-jack glyph, centered, scaled to `size` with
    `padding` (fraction of size, each side) kept clear for maskable icons."""
    img = Image.new("RGBA", (size, size), ROOM)
    draw = ImageDraw.Draw(img)

    cx = cy = size / 2
    content_r = size / 2 * (1 - 2 * padding)

    bezel_r = content_r
    barrel_r = content_r * 0.72
    bore_r = content_r * 0.34
    tip_r = content_r * 0.14

    def circle(r, fill, outline=None, width=0):
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r], fill=fill, outline=outline, width=width
        )

    circle(bezel_r, fill=None, outline=METAL_HI, width=max(1, round(size * 0.02)))
    circle(barrel_r, fill=AMBER)
    circle(bore_r, fill=INK)
    circle(tip_r, fill=METAL_HI)

    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    for name, (size, padding) in SIZES.items():
        img = draw_jack(size, padding)
        path = os.path.join(args.out, name)
        img.save(path)
        print(f"wrote {path} ({size}x{size})")


if __name__ == "__main__":
    main()
