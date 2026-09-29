"""Pixel-art lighthouse logo: a 24x24 grid, one char per pixel, rendered as merged SVG rects or a PNG.

Usage:
  python3 assets/make_logo.py assets/logo.svg                              # classic, transparent
  python3 assets/make_logo.py assets/logo-icon.svg --disc                  # classic, on a dark disc
  python3 assets/make_logo.py assets/logo-brutal.svg --brutal              # neobrutalist: flat colours, black outline, hard shadow
  python3 assets/make_logo.py assets/logo-brutal-icon.svg --brutal --tile  # neobrutalist, on a bordered tile
  python3 assets/make_logo.py assets/logo-neo.svg --vector                 # neobrutalist, smooth vector (not pixelated)
  python3 assets/make_logo.py assets/logo-neo-icon.svg --vector --tile     # smooth vector, on a rounded lime tile
A .png output path writes a transparent PNG instead, scaled with --scale N (default 10):
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

# Neobrutalist palette: a few flat, loud colours and black ink, matching the landing page.
INK = "#111111"
BRUTAL = {
    "b": "#ffd23f",                  # beams: yellow, drawn as light (no outline or shadow)
    "R": INK,                        # lantern frame and base (roof and gallery: see brutal_colour)
    "l": "#ffd23f",                  # lamp
    "L": "#fff7cc",                  # lamp core
    "s": "#ffffff", "S": "#ffffff",  # white stripes
    "m": "#4ade80", "M": "#4ade80",  # green stripes, flat (no shading)
    "d": INK,                        # door
}
TILE = "#d9f99d"                     # lime tile behind the --tile icon
GREEN = "#4ade80"


def brutal_colour(x, y, c):
    """Brutal colour per pixel: 'R' is roof (rows 2-4) and gallery (row 9) in green, else ink."""
    if c == "R" and (y <= 4 or y == 9):
        return GREEN
    return BRUTAL[c]

# Smooth neobrutalist lighthouse (--vector): flat fills, 4px black outlines, and a hard shadow made
# with an SVG filter (the shape's alpha, flooded black and offset), so it has no blur.
VECTOR_LIGHTHOUSE = """<g stroke="#111111" stroke-width="4" stroke-linejoin="round" stroke-linecap="round">
  <path d="M54 40 L12 26 V56 Z" fill="#ffd23f"/>
  <path d="M74 40 L116 26 V56 Z" fill="#ffd23f"/>
  <g clip-path="url(#tower)" stroke="none">
    <rect x="30" y="50" width="68" height="60" fill="#ffffff"/>
    <rect x="30" y="68" width="68" height="12" fill="#4ade80"/>
    <rect x="30" y="92" width="68" height="14" fill="#4ade80"/>
  </g>
  <path d="M46 56 H82 L90 104 H38 Z" fill="none"/>
  <path d="M57 104 V92 a7 7 0 0 1 14 0 V104 Z" fill="#111111"/>
  <rect x="40" y="48" width="48" height="10" rx="2" fill="#4ade80"/>
  <rect x="50" y="30" width="28" height="18" rx="2" fill="#ffd23f"/>
  <circle cx="64" cy="39" r="6" fill="#fff7cc" stroke="none"/>
  <path d="M46 31 L64 14 L82 31 Z" fill="#4ade80"/>
  <rect x="28" y="104" width="72" height="10" rx="3" fill="#111111"/>
</g>"""


def vector_svg(tile):
    drop = 6 if tile else 5
    defs = (f'<defs><filter id="hard" x="-10%" y="-10%" width="130%" height="130%">'
            f'<feFlood flood-color="#111111"/><feComposite in2="SourceAlpha" operator="in"/>'
            f'<feOffset dx="{drop}" dy="{drop}"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>'
            f'</filter><clipPath id="tower"><path d="M46 56 H82 L90 104 H38 Z"/></clipPath></defs>')
    if tile:
        body = ('<rect x="4" y="4" width="112" height="112" rx="18" fill="#d9f99d" stroke="#111111" stroke-width="4" '
                'filter="url(#hard)"/><g transform="translate(12 10) scale(0.78)"><g filter="url(#hard)">'
                + VECTOR_LIGHTHOUSE + '</g></g>')
    else:
        body = '<g filter="url(#hard)">' + VECTOR_LIGHTHOUSE + '</g>'
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" role="img"><title>coolify-watchtower</title>'
            + defs + body + '</svg>\n')


if "--vector" in sys.argv:
    if sys.argv[1].endswith(".png"):
        sys.exit("--vector writes SVG only; rasterise it with e.g. `qlmanage -t -s 512 -o . file.svg` on macOS")
    open(sys.argv[1], "w").write(vector_svg("--tile" in sys.argv))
    print("vector svg")
    sys.exit()

rows = [r for r in GRID.strip("\n").split("\n")]
assert len(rows) == 24 and all(len(r) == 24 for r in rows), [len(r) for r in rows]
args = sys.argv[1:]


def classic():
    """The original logo: 24x24, optionally (--disc) on a dark pixel disc."""
    palette, grid = dict(PALETTE), rows
    if "--disc" in args:
        palette.update({"R": "#052e16", "d": "#022c22", "o": "#14532d"})
        inside = lambda x, y: (x - 11.5) ** 2 + (y - 11.5) ** 2 <= 11.8 ** 2
        # Fill the disc behind the art; beams stop at the disc edge.
        grid = ["".join("o" if c == "." and inside(x, y) else ("." if c == "b" and not inside(x, y) else c)
                        for x, c in enumerate(row)) for y, row in enumerate(grid)]
    return 24, {(x, y): palette[c] for y, row in enumerate(grid) for x, c in enumerate(row) if c != "."}


def brutal():
    """Neobrutalist logo: flat colours, a 1px black outline around the silhouette and a hard shadow.
    With --tile it sits on a bordered lime tile with its own shadow, like the landing page's cards."""
    tile = "--tile" in args
    size, off, drop = (32, 3, 2) if tile else (29, 1, 2)
    art = {(x + off, y + off): brutal_colour(x, y, c) for y, row in enumerate(rows) for x, c in enumerate(row) if c != "."}
    beams = {(x + off, y + off) for y, row in enumerate(rows) for x, c in enumerate(row) if c == "b"}
    solid = set(art) - beams  # the lighthouse itself; beams are light and get no outline or shadow
    outline = {(x + dx, y + dy) for (x, y) in solid for dx in (-1, 0, 1) for dy in (-1, 0, 1)} - solid
    px = {}
    if tile:
        hi = size - 1 - drop  # tile covers 0..hi, its shadow sits drop pixels down and right
        for y in range(hi + 1):
            for x in range(hi + 1):
                px[(x + drop, y + drop)] = INK
        for y in range(hi + 1):
            for x in range(hi + 1):
                px[(x, y)] = INK if x in (0, hi) or y in (0, hi) else TILE
        drop = 1  # a smaller shadow for the lighthouse inside the tile
    for (x, y) in solid | outline:
        px[(x + drop, y + drop)] = INK  # hard shadow of the outlined silhouette
    for p in outline:
        px[p] = INK
    px.update(art)
    return size, {p: c for p, c in px.items() if 0 <= p[0] < size and 0 <= p[1] < size}


RETRO = {
    "title": "#b69cff",  # purple title bar
    "sky": "#a5f3fc",    # pale cyan sky
    "sea": "#60a5fa",    # blue sea
    "button": "#ffffff",
}


def retro():
    """Retro + neobrutalist logo: the brutal pixel lighthouse inside a little Windows-98-style window
    (purple title bar with minimise/maximise/close buttons) with a black frame and a hard shadow.
    Cyan sky, blue sea, and a dithered line where they meet. 40x40.
    With --notitle: just the framed scene, no title bar. 34x34."""
    title = "--notitle" not in args
    if title:
        size, win, bar, ox, oy = 40, 37, 10, 7, 11  # bar: title bar rows 1..bar-1, separator on row bar
    else:
        size, win, bar, ox, oy = 34, 31, 0, 4, 5
    drop = 2                                         # window covers 0..win, shadow drop pixels down/right
    horizon = oy + 22                                # the lighthouse base row sits on the waterline
    px = {}
    for y in range(win + 1):
        for x in range(win + 1):
            px[(x + drop, y + drop)] = INK                          # window shadow
    for y in range(win + 1):
        for x in range(win + 1):
            if x in (0, win) or y in (0, win) or (title and y == bar):
                c = INK                                              # frame and title bar separator
            elif y < bar:
                c = RETRO["title"]
            elif y > horizon:
                c = RETRO["sea"]
            elif y == horizon:
                c = RETRO["sea"] if x % 2 else RETRO["sky"]          # dithered horizon
            else:
                c = RETRO["sky"]
            px[(x, y)] = c
    # Title bar buttons, Windows 98 style: minimise, maximise, close (7x7, ink border, white face, 5x5 glyph).
    glyphs = {
        "min": [".....", ".....", ".....", ".###.", "....."],
        "max": [".....", ".###.", ".#.#.", ".###.", "....."],
        "close": [".....", ".#.#.", "..#..", ".#.#.", "....."],
    }
    for bx, name in ((14, "min"), (22, "max"), (30, "close")) if title else ():
        for y in range(7):
            for x in range(7):
                edge = x in (0, 6) or y in (0, 6)
                px[(bx + x, 2 + y)] = INK if edge or glyphs[name][y - 1][x - 1] == "#" else RETRO["button"]
    # The lighthouse, standing in the sea, its base on the waterline.
    art = {(x + ox, y + oy): brutal_colour(x, y, c) for y, row in enumerate(rows) for x, c in enumerate(row) if c != "."}
    beams = {(x + ox, y + oy) for y, row in enumerate(rows) for x, c in enumerate(row) if c == "b"}
    solid = set(art) - beams
    outline = {(x + dx, y + dy) for (x, y) in solid for dx in (-1, 0, 1) for dy in (-1, 0, 1)} - solid
    inside = lambda p: 1 <= p[0] <= win - 1 and bar < p[1] <= win - 1  # keep drawing inside the window body
    for (x, y) in solid | outline:
        if inside((x + 1, y + 1)):
            px[(x + 1, y + 1)] = INK                                 # small hard shadow on the sky
    for p in outline:
        if inside(p):
            px[p] = INK
    px.update({p: c for p, c in art.items() if inside(p)})
    return size, px


if "--retro" in args:
    size, pixels = retro()
else:
    size, pixels = brutal() if "--brutal" in args else classic()
out = args[0]

if out.endswith(".png"):
    scale = int(args[args.index("--scale") + 1]) if "--scale" in args else 10
    raw = b""
    for y in range(size):
        line = b"".join((bytes.fromhex(pixels[(x, y)][1:]) + b"\xff" if (x, y) in pixels else b"\x00" * 4) * scale
                        for x in range(size))
        raw += (b"\x00" + line) * scale  # filter byte 0 per scanline

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    side = size * scale
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    open(out, "wb").write(png)
    print(f"{side}x{side} png")
    sys.exit()

rects = []
for y in range(size):
    x = 0
    while x < size:
        c = pixels.get((x, y))
        if c is None:
            x += 1
            continue
        start = x
        while x < size and pixels.get((x, y)) == c:
            x += 1
        rects.append(f'<rect x="{start}" y="{y}" width="{x - start}" height="1" fill="{c}"/>')

svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges" role="img">'
    "<title>coolify-watchtower</title>" + "".join(rects) + "</svg>\n"
)
open(out, "w").write(svg)
print(f"{len(rects)} rects")
