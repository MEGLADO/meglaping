"""Build the app icon from the logo svg.

The logo is a 12x12 grid of solid squares, so the icon is written directly as an ICO
of uncompressed BGRA bitmaps. Nearest-neighbour keeps the pixels hard at every size.

    python tools/make_icon.py
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)

GRID = 12
BACKGROUND = (0x0D, 0x0D, 0x0F)
ACCENT = (0xB0, 0x4A, 0x4F)

_RECT = re.compile(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)" fill="(#[0-9a-fA-F]{6})"')


def read_pixels(svg_path: Path) -> set[tuple[int, int]]:
    """The accent squares of the mark, as (x, y) cells on the 12x12 grid."""
    svg = svg_path.read_text(encoding="utf-8")
    cells = set()
    for x, y, w, h, fill in _RECT.findall(svg):
        if fill.lower() != "#b04a4f":
            continue  # the full-bleed background rect
        for dx in range(int(float(w))):
            for dy in range(int(float(h))):
                cells.add((int(float(x)) + dx, int(float(y)) + dy))
    return cells


def render(cells: set[tuple[int, int]], size: int, rounded: bool = True) -> bytes:
    """BGRA rows, bottom-up, as an ICO bitmap wants them."""
    radius = size // 8 if rounded else 0
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            # Nearest-neighbour from the 12x12 grid keeps every square hard-edged.
            on = (x * GRID // size, y * GRID // size) in cells
            r, g, b = ACCENT if on else BACKGROUND
            alpha = 0 if radius and _outside_corner(x, y, size, radius) else 255
            row += bytes((b, g, r, alpha))
        rows.append(bytes(row))
    return b"".join(rows)


def _outside_corner(x: int, y: int, size: int, radius: int) -> bool:
    """True for pixels beyond the rounded corner, matching rx on the tile svg.

    Clamps to the inner rectangle and measures from there, so each pixel is tested
    against its own nearest corner. Testing every corner erases the whole band.
    """
    cx = min(max(x, radius), size - 1 - radius)
    cy = min(max(y, radius), size - 1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 > radius * radius


def build_ico(cells: set[tuple[int, int]], out: Path) -> None:
    images = []
    for size in SIZES:
        pixels = render(cells, size)
        # BITMAPINFOHEADER: height is doubled because the AND mask follows the colours.
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
        mask_stride = ((size + 31) // 32) * 4
        images.append(header + pixels + b"\x00" * (mask_stride * size))

    offset = 6 + 16 * len(images)
    directory = b""
    for size, blob in zip(SIZES, images):
        directory += struct.pack(
            "<BBBBHHII", 0 if size == 256 else size, 0 if size == 256 else size,
            0, 0, 1, 32, len(blob), offset,
        )
        offset += len(blob)
    out.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + directory + b"".join(images))


def build_mark(cells: set[tuple[int, int]], out: Path) -> None:
    """The logo on transparency, for readmes and light backgrounds."""
    squares = "\n".join(
        f'  <rect x="{x}" y="{y}" width="1" height="1" fill="#b04a4f"/>'
        for x, y in sorted(cells, key=lambda c: (c[1], c[0]))
    )
    out.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12" width="512" height="512"\n'
        '     shape-rendering="crispEdges">\n'
        "  <title>meglaping</title>\n"
        f"{squares}\n"
        "</svg>\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    cells = read_pixels(ASSETS / "meglaping-tile.svg")
    assert cells, "no accent squares found in the tile svg"
    build_mark(cells, ASSETS / "meglaping-mark.svg")
    build_ico(cells, ASSETS / "meglaping.ico")
    ico = ASSETS / "meglaping.ico"
    print(f"{len(cells)} squares -> {ico.name} ({ico.stat().st_size:,} bytes, sizes {SIZES})")
