"""Generate the app icon (icon.ico) with no third-party dependencies.

Pillow is not a runtime dependency of this project and it would be silly to add
one just to draw a square, so the PNGs are encoded by hand with zlib and packed
into an ICO container. Windows accepts PNG-compressed ICO entries.

Run this only when changing the artwork:

    python make_icon.py
"""

import os
import struct
import zlib

# Matches the app and viewer accent.
ORANGE = (255, 69, 0)
ORANGE_DEEP = (214, 54, 0)
WHITE = (255, 255, 255)

SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4          # supersampling factor, for smooth edges at small sizes


def _rounded(x, y, w, h, radius):
    """Inside a rounded rectangle occupying the whole canvas?"""
    if x < radius and y < radius:
        return (x - radius) ** 2 + (y - radius) ** 2 <= radius ** 2
    if x > w - radius and y < radius:
        return (x - (w - radius)) ** 2 + (y - radius) ** 2 <= radius ** 2
    if x < radius and y > h - radius:
        return (x - radius) ** 2 + (y - (h - radius)) ** 2 <= radius ** 2
    if x > w - radius and y > h - radius:
        return (x - (w - radius)) ** 2 + (y - (h - radius)) ** 2 <= radius ** 2
    return True


def _glyph(px, py):
    """The white mark: an arrow descending into an open tray.

    Coordinates are 0..1 so the shape scales cleanly to any icon size.
    """
    # Arrow shaft
    if 0.44 <= px <= 0.56 and 0.20 <= py <= 0.52:
        return True
    # Arrow head: a triangle narrowing to a point at py = 0.70
    if 0.50 <= py <= 0.70:
        half = 0.20 * (0.70 - py) / 0.20
        if abs(px - 0.50) <= half:
            return True
    # Tray: base plus two short uprights, open at the top
    if 0.76 <= py <= 0.84 and 0.24 <= px <= 0.76:
        return True
    if 0.62 <= py <= 0.84 and (0.24 <= px <= 0.32 or 0.68 <= px <= 0.76):
        return True
    return False


def render(size):
    """Return RGBA bytes for one icon size."""
    radius = size * 0.22
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = a = 0
            hits = 0
            # Supersample this pixel.
            for sy in range(SS):
                for sx in range(SS):
                    fx = x + (sx + 0.5) / SS
                    fy = y + (sy + 0.5) / SS
                    if not _rounded(fx, fy, size, size, radius):
                        continue
                    hits += 1
                    if _glyph(fx / size, fy / size):
                        cr, cg, cb = WHITE
                    else:
                        # Subtle vertical gradient so it isn't a flat slab.
                        t = fy / size
                        cr = int(ORANGE[0] + (ORANGE_DEEP[0] - ORANGE[0]) * t)
                        cg = int(ORANGE[1] + (ORANGE_DEEP[1] - ORANGE[1]) * t)
                        cb = int(ORANGE[2] + (ORANGE_DEEP[2] - ORANGE[2]) * t)
                    r += cr
                    g += cg
                    b += cb
            total = SS * SS
            if hits:
                row += bytes((r // hits, g // hits, b // hits, (255 * hits) // total))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return rows


def png_bytes(size, rows):
    """Minimal RGBA PNG encoder."""
    raw = b"".join(b"\x00" + row for row in rows)   # filter type 0 per scanline

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def build(path):
    images = []
    for size in SIZES:
        images.append((size, png_bytes(size, render(size))))

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256 in the ICO format
            0 if size >= 256 else size,
            0, 0, 1, 32, len(data), offset,
        )
        blobs += data
        offset += len(data)

    with open(path, "wb") as f:
        f.write(header + entries + blobs)
    return path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    build(out)
    print(f"wrote {out} ({os.path.getsize(out)} bytes, sizes: "
          f"{', '.join(str(s) for s in SIZES)})")
