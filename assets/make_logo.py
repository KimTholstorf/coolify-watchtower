"""Pixel-art lighthouse logo: 24x24 grid, one char per pixel, rendered as merged SVG rects.

Usage: python3 assets/make_logo.py assets/logo.svg && python3 assets/make_logo.py assets/logo-icon.svg --disc
A .png output path writes a transparent PNG instead, scaled with --scale N (default 10, i.e. 240x240):
       python3 assets/make_logo.py assets/logo-icon.png --disc --scale 10
"""
import struct
import sys
import zlib

GRID = """
........................
........................
...........RR...........
..........RRRR..........
bbb......RRRRRR......bbb
bbbbbb...RllllR...bbbbbb
bbbbbbbbbRlLLlRbbbbbbbbb
bbbbbbbbbRlLLlRbbbbbbbbb
bbbbbb...RllllR...bbbbbb
bbb....RRRRRRRRRR....bbb
.........sssssS.........
.........sssssS.........
.........mmmmmM.........
........mmmmmmmM........
........sssssssS........
........sssssssS........
........mmmmmmmM........
.......mmmmmmmmmM.......
.......ssssddsssS.......
.......ssssddsssS.......
......mmmmmddmmmmM......
......mmmmmddmmmmM......
....RRRRRRRRRRRRRRRR....
........................
"""

PALETTE = {
    "b": "#bbf7d0",  # light beam
    "R": "#14532d",  # roof, lantern frame, gallery, base
    "l": "#d9f99d",  # lamp
    "L": "#f7fee7",  # lamp core
    "s": "#4ade80",  # light stripe
    "S": "#22c55e",  # light stripe, shaded side
    "m": "#16a34a",  # dark stripe
    "M": "#15803d",  # dark stripe, shaded side
    "d": "#052e16",  # door
}

rows = [r for r in GRID.strip("\n").split("\n")]
assert len(rows) == 24 and all(len(r) == 24 for r in rows), [len(r) for r in rows]

# --disc: dark pixel disc behind the lighthouse (icon/avatar variant).
DISC = "--disc" in sys.argv
if DISC:
    PALETTE.update({"R": "#052e16", "d": "#022c22", "o": "#14532d"})
    rows = [
        "".join(
            "o" if c == "." and (x - 11.5) ** 2 + (y - 11.5) ** 2 <= 11.8 ** 2 else c
            for x, c in enumerate(row)
        )
        for y, row in enumerate(rows)
    ]
    # Beams stop at the disc edge.
    rows = [
        "".join(
            "." if c == "b" and (x - 11.5) ** 2 + (y - 11.5) ** 2 > 11.8 ** 2 else c
            for x, c in enumerate(row)
        )
        for y, row in enumerate(rows)
    ]

out = sys.argv[1]
if out.endswith(".png"):
    scale = int(sys.argv[sys.argv.index("--scale") + 1]) if "--scale" in sys.argv else 10
    raw = b""
    for row in rows:
        line = b"".join(
            (bytes.fromhex(PALETTE[c][1:]) + b"\xff" if c != "." else b"\x00\x00\x00\x00") * scale for c in row
        )
        raw += (b"\x00" + line) * scale  # filter byte 0 per scanline

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    size = 24 * scale
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    open(out, "wb").write(png)
    print(f"{size}x{size} png")
    sys.exit()

rects = []
for y, row in enumerate(rows):
    x = 0
    while x < 24:
        c = row[x]
        if c == ".":
            x += 1
            continue
        start = x
        while x < 24 and row[x] == c:
            x += 1
        rects.append(f'<rect x="{start}" y="{y}" width="{x - start}" height="1" fill="{PALETTE[c]}"/>')

svg = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" shape-rendering="crispEdges" role="img">'
    "<title>coolify-watchtower</title>" + "".join(rects) + "</svg>\n"
)
open(sys.argv[1], "w").write(svg)
print(f"{len(rects)} rects")
