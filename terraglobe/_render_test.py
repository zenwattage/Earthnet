"""Demo: a still frame with 12 international connections from Seattle, WA.

Renders a single frame with HD sixel (if numpy + Pillow are available) or
falls back to the text renderer.  View in Foot:

    foot -e python3 -m terraglobe --demo
"""
from __future__ import annotations

import colorsys
import hashlib
import math
import sys

from .globe import Camera, GlobeConfig, Trace, render_frame
from .land import load_landmask
from .theme import load_palette

SEATTLE = (47.61, -122.33)

# A dozen international destinations spread across the globe
DESTINATIONS = [
    ("Tokyo",        "JP", 35.68, 139.76),
    ("London",       "GB", 51.51,  -0.13),
    ("Sydney",       "AU", -33.87, 151.21),
    ("Sao Paulo",    "BR", -23.55, -46.63),
    ("Singapore",    "SG",  1.35, 103.82),
    ("Mumbai",       "IN", 19.08,  72.88),
    ("Cape Town",    "ZA", -33.92,  18.42),
    ("Moscow",       "RU", 55.76,  37.62),
    ("Dubai",        "AE", 25.20,  55.27),
    ("Buenos Aires", "AR", -34.60, -58.38),
    ("Seoul",        "KR", 37.57, 126.98),
    ("Reykjavik",    "IS", 64.15, -21.94),
]

ASPECT = 0.55


def _trace_color(name: str) -> tuple:
    """Distinct bright color per destination via HSV hash."""
    h = int(hashlib.md5(name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.65, 1.0)
    return (int(r * 255), int(g * 255), int(b * 255))


def _flag_emoji(cc: str) -> str:
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return ""
    a, b = ord(cc[0]), ord(cc[1])
    return chr(0x1F1E6 + (a - 65)) + chr(0x1F1E6 + (b - 65))


def demo_traces():
    """Return 12 Trace objects from Seattle to international destinations.
    Used by --demo to run the full animated globe with fixed traces."""
    traces = []
    for i, (city, cc, lat, lon) in enumerate(DESTINATIONS):
        traces.append(Trace(
            a_lat=SEATTLE[0], a_lon=SEATTLE[1],
            b_lat=lat, b_lon=lon,
            color=_trace_color(city),
            phase=i / 12.0,
            proto="tcp", label=city, city=city, country=cc, cc=cc,
            life=99999.0, alpha=1.0,
        ))
    return traces


def main():
    pal = load_palette()
    pal.proto_tcp = (147, 17, 211)
    cfg = GlobeConfig(land=load_landmask(), graticule=True, stars=False,
                      palette=pal, space_alpha=0.10)

    # Tilt so the screen-center latitude == Seattle
    cam = Camera(spin=0.6, tilt=math.radians(SEATTLE[0]))

    traces = []
    for i, (city, cc, lat, lon) in enumerate(DESTINATIONS):
        traces.append(Trace(
            a_lat=SEATTLE[0], a_lon=SEATTLE[1],
            b_lat=lat, b_lon=lon,
            color=_trace_color(city),
            phase=i / 12.0,
            proto="tcp", label=city, city=city, country=cc, cc=cc,
            life=9999.0, alpha=1.0,
        ))

    # --- HD sixel path (preferred) ---
    try:
        from . import hd as _hd
        if _hd.HAVE_HD:
            cols, rows = 120, 40
            cell_w, cell_h = 8, 17
            iw = min((cols - 2) * cell_w, 480)
            ih = min((rows - 2) * cell_h, 480)
            rgb, mask = _hd.render_hd(iw, ih, cam, traces, cfg,
                                      left=True, ss=1)
            sixel = _hd.sixel_encode(rgb, mask, pal)

            out = sys.stdout.buffer
            out.write(b"\x1b[2J\x1b[H")          # clear
            out.write(b"\x1b[2;2H")              # position for sixel
            out.write(sixel)

            # header
            ac = pal.hud_accent
            dim = "\x1b[38;2;%d;%d;%dm" % pal.hud_dim
            bright = "\x1b[38;2;%d;%d;%dm" % pal.hud_bright
            accent = "\x1b[38;2;%d;%d;%dm" % ac
            header = accent + "\u25c9 " + bright + "TERRAGLOBE" + dim + " \u00b7 " + accent + "DEMO"
            out.write(("\x1b[1;5H" + header).encode())
            out.write(("\x1b[1;%dH" % (cols - 22) + dim + "12 links from Seattle").encode())

            # labels on the right, color-matched to each trace
            label_col = max(cols - 16, 2)
            for i, (city, cc, lat, lon) in enumerate(DESTINATIONS):
                row = 3 + i
                color = _trace_color(city)
                flag = _flag_emoji(cc)
                name = city[:10]
                txt = (flag + " " + name) if flag else name
                fg = "\x1b[38;2;%d;%d;%dm" % color
                out.write(("\x1b[%d;%dH" % (row, label_col)).encode())
                out.write((fg + txt).encode("utf-8"))

            out.write(b"\x1b[0m\n")
            out.flush()
            return
    except Exception as e:
        print("[demo] HD unavailable (%s), using text renderer" % e,
              file=sys.stderr)

    # --- text renderer fallback ---
    cols, rows = 76, 30
    ry = rows - 1
    rx = ry / (2.0 * ASPECT)
    max_rx = (cols - 2) / 2.0
    max_ry = rows - 1
    if rx > max_rx or ry > max_ry:
        s = min(max_rx / rx, max_ry / ry)
        rx *= s
        ry *= s

    frame = render_frame(cols, rows, cam, traces, cfg, rx=rx, ry=ry)
    sys.stdout.write(frame)
    sys.stdout.write("\x1b[0m\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
