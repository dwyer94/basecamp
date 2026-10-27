"""Draw Basecamp's home-screen icons (R-72). Standard library only, no third-party art.

Writes ``basecamp/web/icon-192.png``, ``icon-512.png`` and ``apple-touch-icon.png`` (180):
a tent under a mountain on a blue square, the same picture as ``icon.svg``. The shapes fill
the middle 80%, so Android's round "maskable" crop keeps them.

    python tools/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "basecamp" / "web"
BG = (35, 87, 217)  # --accent
MOUNTAIN = (124, 160, 240)
SNOW = (255, 255, 255)
TENT = (255, 255, 255)
DOOR = (35, 87, 217)
SAMPLES = 4  # per axis, for smooth edges

# Shapes in a 0..1 square, drawn in order (later ones on top). Same as icon.svg.
SHAPES: list[tuple[tuple[int, int, int], list[tuple[float, float]]]] = [
    (MOUNTAIN, [(0.14, 0.78), (0.44, 0.26), (0.74, 0.78)]),
    (SNOW, [(0.36, 0.40), (0.44, 0.26), (0.52, 0.40), (0.47, 0.37), (0.44, 0.41),
            (0.41, 0.37)]),
    (TENT, [(0.40, 0.78), (0.64, 0.42), (0.88, 0.78)]),
    (DOOR, [(0.58, 0.78), (0.64, 0.60), (0.70, 0.78)]),
]


def inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    hit = False
    j = len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def color_at(x: float, y: float) -> tuple[int, int, int]:
    color = BG
    for fill, poly in SHAPES:
        if inside(x, y, poly):
            color = fill
    return color


def render(size: int) -> bytes:
    rows = []
    n = SAMPLES * SAMPLES
    for py in range(size):
        row = bytearray([0])  # PNG filter: none
        for px in range(size):
            r = g = b = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    c = color_at((px + (sx + 0.5) / SAMPLES) / size,
                                 (py + (sy + 0.5) / SAMPLES) / size)
                    r, g, b = r + c[0], g + c[1], b + c[2]
            row += bytes((r // n, g // n, b // n))
        rows.append(bytes(row))
    return png(size, b"".join(rows))


def png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512),
                       ("apple-touch-icon.png", 180)):
        (WEB / name).write_bytes(render(size))
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
