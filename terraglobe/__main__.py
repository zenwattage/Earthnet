"""terraglobe entry point: python3 -m terraglobe [options]"""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="terraglobe",
        description="A floating translucent 3D Earth globe in your terminal "
                    "with live internet-connection trace arcs.",
    )
    p.add_argument("--mmdb", default=None,
                   help="Path to a GeoLite2-City (or compatible) .mmdb file "
                        "for offline GeoIP. If omitted, uses ip-api.com.")
    p.add_argument("--home", default=None,
                   help="Override your home coordinates as LAT,LON "
                        "(otherwise auto-detected from your public IP).")
    p.add_argument("--inches", type=float, default=None,
                   help="Pin the globe to a fixed diameter in inches on screen. "
                        "By default the globe FILLS the window (no dead space, "
                        "max resolution). Size your Foot window small for the "
                        "compact Jarvis-HUD widget look.")
    p.add_argument("--font-pt", type=float, default=None,
                   help="Override the font point size used for inch sizing "
                        "(otherwise read from ~/.config/foot/foot.ini).")
    p.add_argument("--pt-scale", type=float, default=None,
                   help="Cell-height / em-size factor (default 1.15). Tweak "
                        "if the globe measures too big/small on your font.")
    p.add_argument("--no-hd", action="store_true",
                   help="Disable the high-definition sixel renderer and use the "
                        "text renderer instead. (HD is used automatically when "
                        "numpy + Pillow are available.)")
    p.add_argument("--hd-res", type=int, default=None,
                   help="Cap the HD globe image's longest pixel dimension "
                        "(default 960). Lower for higher FPS, raise for more "
                        "detail / a bigger globe.")
    p.add_argument("--hd-colors", type=int, default=None,
                   help="Number of sixel palette colors (default 96). Raise for "
                        "less banding, lower for smaller output.")
    p.add_argument("--hd-ss", type=int, default=None,
                   help="Supersample factor for smooth rotation (default 2: "
                   "render at 2x then downsample). 1 disables it (faster, "
                   "slightly more jitter); 3 is smoother but slower.")
    p.add_argument("--demo", action="store_true",
                   help="Run the animated globe with 12 demo traces from "
                        "Seattle to international destinations (Tokyo, London, "
                        "Sydney, etc). Fully animated, same as live mode.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.demo:
        from .app import App
        from ._render_test import demo_traces, SEATTLE
        args.home = f"{SEATTLE[0]},{SEATTLE[1]}"
        app = App(args)
        with app.lock:
            for i, t in enumerate(demo_traces()):
                app.traces[f"demo_{i}"] = t
        app.run()
        return 0
    from .app import App
    App(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())