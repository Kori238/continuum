"""Generate the PWA icons. One-off: run it, commit the PNGs, delete nothing else.

Written against zlib+struct rather than Pillow on purpose — the icon is the ◈
brand mark, which is pure geometry, so pulling an imaging library into a
personal assistant just to draw two diamonds would be silly.

Supersamples 4x and box-downsamples for antialiasing. The diamond sits well
inside the maskable safe zone (the middle 80%), so one image serves both the
"any" and "maskable" purposes and Android can crop it to any device shape
without clipping the mark.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "continuum" / "app" / "static"

BG_IN = (0x14, 0x38, 0x3F)     # radial centre, matches the app's backdrop
BG_OUT = (0x0A, 0x15, 0x19)    # --bg
ACCENT = (0x5B, 0x8C, 0xFF)    # --accent
GLOW = (0x35, 0xC4, 0x8A)      # --local, a hint of teal in the inner mark

SS = 4                          # supersampling factor


def _diamond(px: float, py: float, cx: float, cy: float, r: float) -> float:
    """Manhattan distance from centre, normalised so 1.0 is the diamond edge."""
    return (abs(px - cx) + abs(py - cy)) / r


def render(size: int) -> bytes:
    n = size * SS
    cx = cy = (n - 1) / 2.0
    outer_r = n * 0.34
    ring_w = n * 0.055
    inner_r = n * 0.155
    max_d = math.hypot(cx, cy)

    rows = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            # background: radial fade, same shape as the CSS backdrop
            t = min(math.hypot(x - cx, y - cy) / max_d, 1.0)
            t = t ** 0.85
            r = int(BG_IN[0] + (BG_OUT[0] - BG_IN[0]) * t)
            g = int(BG_IN[1] + (BG_OUT[1] - BG_IN[1]) * t)
            b = int(BG_IN[2] + (BG_OUT[2] - BG_IN[2]) * t)

            d = _diamond(x, y, cx, cy, outer_r)
            # outer ring
            if 1.0 - ring_w / outer_r <= d <= 1.0:
                r, g, b = ACCENT
            # inner solid diamond
            elif _diamond(x, y, cx, cy, inner_r) <= 1.0:
                r, g, b = GLOW
            row += bytes((r, g, b))
        rows.append(row)

    # box-downsample SSxSS -> 1
    out = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            rs = gs = bs = 0
            for dy in range(SS):
                src = rows[y * SS + dy]
                for dx in range(SS):
                    i = (x * SS + dx) * 3
                    rs += src[i]; gs += src[i + 1]; bs += src[i + 2]
            k = SS * SS
            row += bytes((rs // k, gs // k, bs // k))
        out.append(bytes(row))
    return _png(size, size, out)


def _png(w: int, h: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + r for r in rows)          # filter type 0 per scanline

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


if __name__ == "__main__":
    for size in (192, 512):
        path = OUT / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"wrote {path} ({path.stat().st_size} bytes)")
