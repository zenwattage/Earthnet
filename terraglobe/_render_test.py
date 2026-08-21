"""Render a still frame to a file for verifying the globe renderer without a
TTY. Run: python3 -m terraglobe._render_test > frame.ans ; then view in a
truecolor terminal, or pipe through `less -R`."""
from __future__ import annotations

import sys

from .globe import Camera, GlobeConfig, Trace, render_frame
from .land import load_landmask
from .theme import load_palette

# demo geometry: fill a compact window (the Jarvis-HUD widget look)
ASPECT = 0.55


def main():
    cols, rows = 76, 30
    cfg = GlobeConfig(land=load_landmask(), graticule=True, stars=True,
                      palette=load_palette())
    cam = Camera(spin=0.6, tilt=0.42)

    # fill the window with a true-circle globe
    ry = rows - 1
    rx = ry / (2.0 * ASPECT)
    max_rx = (cols - 2) / 2.0
    max_ry = rows - 1
    if rx > max_rx or ry > max_ry:
        s = min(max_rx / rx, max_ry / ry)
        rx *= s
        ry *= s

    traces = [
        Trace(51.5, -0.1, 35.68, 139.76, phase=0.2, proto="tcp"),   # Tokyo
        Trace(51.5, -0.1, 37.77, -122.4, phase=0.55, proto="tcp"),  # SF
        Trace(51.5, -0.1, -33.86, 151.2, phase=0.8, proto="udp"),   # Sydney
        Trace(51.5, -0.1, -23.55, -46.63, phase=0.35, proto="tcp"), # Sao Paulo
        Trace(51.5, -0.1, 1.35, 103.8, phase=0.1, proto="tcp"),     # Singapore
    ]

    frame = render_frame(cols, rows, cam, traces, cfg, rx=rx, ry=ry)
    sys.stdout.write(frame)
    sys.stdout.write("\x1b[0m\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()