"""Main application loop for terraglobe.

Ties together connection capture, GeoIP, and the globe renderer into a live,
animated TUI. Designed for Foot but runs in any truecolor terminal.

Controls (press while running):
  q / Ctrl-C   quit
  space / p    pause globe rotation
  + / -        spin faster / slower
  r            reverse spin
  t            toggle trace arcs
  g            toggle graticule
  s            toggle starfield
  h            toggle HUD
  c            clear all traces
  R            force-refresh land + geo cache
"""
from __future__ import annotations

import os
import select
import signal
import sys
import threading
import time

from .conntrack import poll as ct_poll
from .geo import GeoResolver, remote_endpoint
from .globe import (ALT_OFF, ALT_ON, Camera, GlobeConfig, RESET,
                    SHOW, HIDE, Trace, mix, render_frame)
from .land import load_landmask
from .theme import load_palette, load_theme_alpha

try:
    from . import hd as _hd
    HAVE_HD = _hd.HAVE_HD
except Exception:
    _hd = None
    HAVE_HD = False

DEFAULT_COLOR = (150, 255, 180)  # overridden by theme at startup


def flag_emoji(cc: str) -> str:
    """ISO 3166-1 alpha-2 code -> regional-indicator flag emoji (e.g. US -> 🇺🇸)."""
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return ""
    a, b = ord(cc[0]), ord(cc[1])
    if not (65 <= a <= 90 and 65 <= b <= 90):
        return ""
    return chr(0x1F1E6 + (a - 65)) + chr(0x1F1E6 + (b - 65))

POLL_INTERVAL = 1.5     # seconds between connection-table scans
TRACE_HOLD = 3.0        # seconds a trace stays full-bright after last sighting
TRACE_FADE = 2.5        # seconds to fade out after hold
MAX_TRACES = 48
TARGET_FPS = 30

# Cell-height / em-size factor (ascent+descent+leading). Typical monospace.
DEFAULT_PT_SCALE = 1.15
DEFAULT_CELL_ASPECT = 0.55   # cell width / cell height


def _parse_font_pt() -> float:
    """Read the font point size from ~/.config/foot/foot.ini (font=...:size=N)."""
    import re
    try:
        text = open(os.path.expanduser("~/.config/foot/foot.ini")).read()
        m = re.search(r"font\s*=\S*?:size=(\d+(?:\.\d+)?)", text)
        if m:
            return float(m.group(1))
        m = re.search(r"font\s*=\S*?size=(\d+(?:\.\d+)?)", text)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return 9.0


def _query_cell_aspect() -> float | None:
    """Ask the terminal for its cell pixel size (CSI 16 t) and return
    width/height aspect. Returns None if the terminal doesn't answer."""
    px = _query_cell_pixels()
    if px:
        w, h = px
        return w / h if h else None
    return None


def _query_cell_pixels() -> tuple[int, int] | None:
    """CSI 16 t -> (cell_w_px, cell_h_px) device pixels, or None."""
    try:
        fd = sys.stdin.fileno()
    except Exception:
        return None
    try:
        sys.stdout.write("\x1b[16t")
        sys.stdout.flush()
        r, _, _ = select.select([sys.stdin], [], [], 0.15)
        if not r:
            return None
        data = os.read(fd, 64).decode("ascii", "ignore")
        import re
        m = re.search(r"6;(\d+);(\d+)t", data)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


class App:
    def __init__(self, args):
        self.args = args
        print("[app] loading theme...", flush=True)
        self.palette = load_palette()
        print(f"[app] theme: space #{self.palette.space[0]:02X}"
              f"{self.palette.space[1]:02X}{self.palette.space[2]:02X}  "
              f"accent #{self.palette.hud_accent[0]:02X}"
              f"{self.palette.hud_accent[1]:02X}{self.palette.hud_accent[2]:02X}",
              flush=True)
        # Theme translucency: let the globe rim blend into whatever alpha the
        # terminal/theme background calls for.
        self.theme_alpha = load_theme_alpha()
        space_alpha = max(0.10, 1.0 - self.theme_alpha)
        print(f"[app] theme background-alpha: {self.theme_alpha:.2f} "
              f"(globe rim blend {space_alpha:.2f})", flush=True)
        print("[app] loading land mask...", flush=True)
        # stars off by default: the starfield conflicted with the spinning
        # globe and trace lines (smearing/streaking). Press 's' to re-enable.
        self.cfg = GlobeConfig(land=load_landmask(), graticule=True,
                               stars=False, palette=self.palette,
                               space_alpha=space_alpha)
        # Physical sizing: globe diameter in inches, derived from the font pt
        # size so it is correct at any display scale.
        self.inches = getattr(args, "inches", None) or None
        pt = getattr(args, "font_pt", None) or _parse_font_pt()
        self.pt_scale = getattr(args, "pt_scale", None) or DEFAULT_PT_SCALE
        self.cell_h_in = pt / 72.0 * self.pt_scale
        self.cell_aspect = DEFAULT_CELL_ASPECT  # refined from CSI 16 at runtime
        # HD (sixel) path
        self.hd = HAVE_HD and not getattr(args, "no_hd", False)
        self.hd_res = int(getattr(args, "hd_res", None) or 960)  # cap longest px dim
        self.hd_colors = int(getattr(args, "hd_colors", None) or 96)
        self.hd_ss = int(getattr(args, "hd_ss", None) or 2)  # 2=supersample (smoother); 1=fast (jittery)
        self.cell_w_px = 8
        self.cell_h_px = 17  # refined from CSI 16 at runtime
        print(f"[app] render: {'HD sixel' if self.hd else 'text'}"
              f"{'' if self.hd else ''}  globe {'%g\"' % self.inches if self.inches else 'fill window'}"
              f"  font {pt}pt  cell {self.cell_h_in:.3f}in tall", flush=True)
        self.geo = GeoResolver(args.mmdb)
        self.home = self._resolve_home()
        # Tilt the globe so the screen-center latitude == your home latitude.
        import math as _math
        home_lat = max(-89.0, min(89.0, float(self.home[0])))
        self.camera = Camera(spin=0.0, tilt=_math.radians(home_lat))
        print(f"[app] globe centered on latitude {home_lat:.1f}\u00b0", flush=True)
        self.spin_speed = 0.18      # rad/s
        self.trace_speed = 0.35     # cycles/s along the arc
        self.traces: dict[str, Trace] = {}
        self.lock = threading.Lock()
        self.paused = False
        self.show_traces = True
        self.show_hud = True
        self.show_labels = True
        self.running = True
        self.backend = "none"
        self.flow_count = 0
        self.fps = 0.0
        self._fps_acc = 0.0
        self._fps_n = 0
        self._last_fps_t = time.perf_counter()
        self.cols, self.rows = 80, 24
        self._stop = threading.Event()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)

    def _resolve_home(self):
        if self.args.home:
            lat, lon = (float(x) for x in self.args.home.split(","))
            print(f"[app] home (from --home): {lat}, {lon}", flush=True)
            return (lat, lon)
        h = self.geo.detect_home()
        if h is None:
            print("[app] home detection failed; defaulting to 0,0. "
                  "Use --home LAT,LON.", flush=True)
            return (0.0, 0.0)
        return h

    # ---- capture thread ----
    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                flows, backend = ct_poll()
                self.backend = backend
                self.flow_count = len(flows)
                remotes: dict[str, str] = {}  # ip -> proto
                for f in flows:
                    r = remote_endpoint(f.src, f.dst)
                    if r is None:
                        continue
                    remotes[r] = f.proto
                if remotes:
                    resolved = self.geo.resolve_many(list(remotes.keys()))
                    with self.lock:
                        seen = set()
                        for ip, proto in remotes.items():
                            g = resolved.get(ip)
                            if not g:
                                continue
                            # 0,0 means the locator couldn't place it; skip
                            if g.get("lat") == 0 and g.get("lon") == 0:
                                continue
                            seen.add(ip)
                            self._upsert_trace(ip, proto, g)
                        # traces not seen begin fading (handled in main loop)
            except Exception as e:
                print(f"[app] poll error: {e}", flush=True)
            self._stop.wait(POLL_INTERVAL)

    def _trace_color(self, ip: str) -> tuple:
        """Distinct bright color per destination IP via HSV hash.
        Each endpoint gets its own hue so traces don't blur together."""
        import colorsys, hashlib
        h = int(hashlib.md5(ip.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        r, g, b = colorsys.hsv_to_rgb(h, 0.65, 1.0)
        return (int(r * 255), int(g * 255), int(b * 255))

    def _upsert_trace(self, ip: str, proto: str, g: dict):
        t = self.traces.get(ip)
        color = self._trace_color(ip)
        city = g.get("city", "") or ""
        country = g.get("country", "") or ""
        cc = (g.get("cc", "") or "").upper()
        label = city or country
        if t:
            t.life = TRACE_HOLD + TRACE_FADE
            t.alpha = 1.0
            t.proto = proto
            t.color = color
            t.label = label
            t.city = city
            t.country = country
            t.cc = cc
        else:
            if len(self.traces) >= MAX_TRACES:
                # drop the one with the least remaining life
                worst = min(self.traces.values(), key=lambda x: x.life)
                for k, v in self.traces.items():
                    if v is worst:
                        del self.traces[k]
                        break
            self.traces[ip] = Trace(
                a_lat=self.home[0], a_lon=self.home[1],
                b_lat=g["lat"], b_lon=g["lon"],
                color=color, life=TRACE_HOLD + TRACE_FADE,
                phase=(hash(ip) & 0xFFFF) / 65535.0,
                proto=proto, label=label, city=city, country=country, cc=cc,
            )

    # ---- rendering ----
    def _size(self):
        try:
            sz = os.get_terminal_size(sys.stdout.fileno())
            return sz.columns, sz.lines
        except Exception:
            return 80, 24

    def _globe_radii(self, cols: int, rows: int):
        """Compute the globe radii in sub-pixels. With ``inches`` set, the disk
        is a fixed physical size; otherwise it fills the window (no dead space).
        Returns (rx, ry)."""
        if self.inches:
            cell_w_in = self.cell_h_in * self.cell_aspect
            rx = self.inches / (2.0 * cell_w_in)
            ry = self.inches / self.cell_h_in
        else:
            # fill the window with a 1-cell margin, keeping a true circle
            ry = rows - 1                      # vertical sub-px radius
            rx = ry / (2.0 * self.cell_aspect)  # circle aspect correction
        # clamp to fit
        max_rx = (cols - 2) / 2.0
        max_ry = rows - 1
        if rx > max_rx or ry > max_ry:
            s = min(max_rx / rx if rx else 1.0, max_ry / ry if ry else 1.0)
            rx *= s
            ry *= s
        return rx, ry

    def _hud(self, cols, rows, traces, rx, ry):
        if not self.show_hud:
            return ""
        pal = self.palette
        ac = pal.hud_accent
        dim = "\x1b[38;2;%d;%d;%dm" % pal.hud_dim
        bright = "\x1b[38;2;%d;%d;%dm" % pal.hud_bright
        accent = "\x1b[38;2;%d;%d;%dm" % ac
        bg = mix(pal.space, ac, 0.12)
        bg_sgr = "\x1b[48;2;%d;%d;%dm" % bg
        parts = []

        def at(r, c, s):
            return f"\x1b[{r};{c}H{s}"

        def clampc(c):
            return 1 if c < 1 else cols if c > cols else c
        def clampr(r):
            return 1 if r < 1 else rows if r > rows else r

        # corner brackets (sci-fi frame)
        parts.append(at(1, 1, f"{accent}┌{dim}─"))
        parts.append(at(2, 1, f"{accent}│"))
        parts.append(at(1, cols - 2, f"{dim}─{accent}┐"))
        parts.append(at(2, cols, f"{accent}│"))
        parts.append(at(rows, 1, f"{accent}└{dim}─"))
        parts.append(at(rows - 1, 1, f"{accent}│"))
        parts.append(at(rows, cols - 1, f"{dim}─{accent}┘"))
        parts.append(at(rows - 1, cols, f"{accent}│"))

        # top bar
        parts.append(at(1, 5, f"{accent}◉ {bright}TERRAGLOBE{dim} · {accent}LIVE LINK MATRIX"))
        stat = (f"{dim}flows {bright}{self.flow_count}{dim}  "
                f"links {bright}{len(traces)}{dim}  "
                f"{bright}{self.fps:4.1f}{dim}fps")
        parts.append(at(1, clampc(cols - 2 - len(stat)), stat))

        # compass ticks around the globe
        cx = cols / 2.0
        cy = rows / 2.0
        rx_c = rx
        ry_c = ry / 2.0
        for label, dx, dy in (("N", 0, -1), ("E", 1, 0), ("S", 0, 1), ("W", -1, 0)):
            lx = clampc(int(cx + dx * (rx_c + 1)))
            ly = clampr(int(cy + dy * (ry_c + 1)))
            parts.append(at(ly, lx, f"{accent}{label}"))

        # telemetry panel hidden by default (header still shows link count);
        # set self.show_panel = True to bring back the side list.
        if getattr(self, "show_panel", False):
            pw = min(26, max(10, cols - 6))
            pc = clampc(cols - pw - 1)
            pr = 3
            max_lines = max(0, min(rows - pr - 3, 12))
            ordered = sorted(traces, key=lambda t: -t.life)[:max_lines]
            header = f"{accent}ACTIVE LINKS{dim} {len(traces):>3}"
            parts.append(at(pr, pc, bg_sgr + accent + header.ljust(pw) + "\x1b[49m"))
            for i, t in enumerate(ordered):
                r = pr + 1 + i
                if r > rows - 2:
                    break
                dot = "\x1b[38;2;%d;%d;%dm●" % t.color
                proto = (t.proto or "ip").upper()[:3]
                lbl = (t.label or "?")[: pw - 8]
                line = f"{dot}{dim}→{bright}{lbl:<{pw - 8}}{dim}{proto:>3}"
                parts.append(at(r, pc, bg_sgr + line[:pw] + "\x1b[49m"))

        # bottom bar
        parts.append(at(rows, 5, f"{dim}origin {bright}{self.home[0]:.2f},{self.home[1]:.2f}"
                                  f"{dim}  src {bright}{self.backend}"))
        ctrl = (f"{dim}q quit · space pause · +/- spin · t traces · "
                f"g grid · s stars · c clear")
        parts.append(at(rows, clampc(cols - 1 - len(ctrl)), ctrl))

        if self.paused:
            parts.append(at(2, 5, f"\x1b[38;2;255;200;80m⏸ PAUSED{RESET}"))
        return "".join(parts) + RESET

    def _frame_hd(self, cols, rows, traces):
        """Build one HD frame as bytes: an opaque sixel globe blitted at the
        left, filling the window height, with Jarvis HUD text only in the top
        and bottom margins (no panel, no text over the image). The sixel
        self-overwrites each frame; the screen is cleared only on resize so
        there's no black flash or ghosting while the globe spins."""
        pal = self.palette
        ac = pal.hud_accent
        dim = ("\x1b[38;2;%d;%d;%dm" % pal.hud_dim).encode()
        bright = ("\x1b[38;2;%d;%d;%dm" % pal.hud_bright).encode()
        accent = ("\x1b[38;2;%d;%d;%dm" % ac).encode()
        RESETb = RESET.encode()

        gr_cols = max(8, cols - 2)
        gr_rows = max(6, rows - 2)
        cap = self.hd_res
        iw = min(gr_cols * self.cell_w_px, cap)
        ih = min(gr_rows * self.cell_h_px, cap)
        iw = max(8, iw); ih = max(8, ih)

        rgb, mask = _hd.render_hd(iw, ih, self.camera, traces, self.cfg,
                                  left=True, ss=self.hd_ss)
        sixel = _hd.sixel_encode(rgb, mask, self.palette)
        # cells the sixel occupies (to erase the previous frame's sixel)
        sw = (iw + self.cell_w_px - 1) // self.cell_w_px
        sh = (ih + self.cell_h_px - 1) // self.cell_h_px
        blank = b" " * sw

        o = []
        o.append(b"\x1b[?2026h")                       # synced update: begin
        # full clear only on resize (handles margin text outside the sixel)
        last = getattr(self, "_hd_last", None)
        if last != (cols, rows, iw, ih):
            o.append(b"\x1b[2J\x1b[H")
            self._hd_last = (cols, rows, iw, ih)
        # erase the previous sixel every frame by overwriting its cells with
        # spaces -- Foot doesn't erase an old sixel when a new one is blitted,
        # so without this the grid/trace/coastline lines streak as it spins.
        for r in range(2, 2 + sh):
            o.append(("\x1b[%d;2H" % r).encode()); o.append(blank)
        # sixel globe at row 2, col 2 (left-justified, fills height)
        o.append(b"\x1b[2;2H")
        o.append(sixel)

        def at(r, c, b):
            o.append(("\x1b[%d;%dH" % (r, c)).encode()); o.append(b)

        # corner brackets
        at(1, 1, accent + "\u250c".encode() + dim + "\u2500".encode())
        at(2, 1, accent + "\u2502".encode())
        at(1, cols - 2, dim + "\u2500".encode() + accent + "\u2510".encode())
        at(2, cols, accent + "\u2502".encode())
        at(rows, 1, accent + "\u2514".encode() + dim + "\u2500".encode())
        at(rows - 1, 1, accent + "\u2502".encode())
        at(rows, cols - 1, dim + "\u2500".encode() + accent + "\u2518".encode())
        at(rows - 1, cols, accent + "\u2502".encode())

        # header (no side panel)
        at(1, 5, accent + "\u25c9 ".encode() + bright + b"TERRAGLOBE" + dim +
           " \u00b7 ".encode() + accent + b"HD LINK MATRIX")
        stat = (dim + ("flows %d  links %d  %.1ffps" % (self.flow_count, len(traces), self.fps)).encode())
        at(1, max(2, cols - 2 - len(stat.decode('ascii', 'replace'))), stat)

        # footer
        at(rows, 5, dim + ("origin %.2f,%.2f  src %s" % (self.home[0], self.home[1], self.backend)).encode())
        ctrl = "q quit \u00b7 space pause \u00b7 +/- spin \u00b7 t traces \u00b7 g grid \u00b7 c clear"
        at(rows, max(2, cols - 1 - len(ctrl)), dim + ctrl.encode())

        # destination labels: stationary on the right, color-matched to each trace
        if getattr(self, "show_labels", True):
            label_w = 14
            label_col = max(cols - label_w - 2, 2)
            max_labels = min(rows - 4, 12)
            n_lab = 0
            for t in sorted(traces, key=lambda x: -x.life):
                if t.life <= TRACE_FADE or n_lab >= max_labels:
                    break
                lr = 3 + n_lab
                if lr > rows - 2:
                    break
                flag = flag_emoji(t.cc)
                name = (t.city or t.country or "?")[:10]
                txt = (flag + " " + name) if flag else name
                fg = "\x1b[38;2;%d;%d;%dm" % t.color
                at(lr, label_col, (fg + txt).encode("utf-8"))
                n_lab += 1

        if self.paused:
            at(2, 5, ("\x1b[38;2;255;200;80m\u23f8 PAUSED").encode() + RESETb)
        o.append(b"\x1b[?2026l")                       # synced update: end
        o.append(RESETb)
        return b"".join(o)

    def run(self):
        # enter alt screen, hide cursor, cbreak
        out = sys.stdout
        old_term = None
        try:
            fd = sys.stdin.fileno()
            import termios, tty
            old_term = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            pass
        out.write(ALT_ON + HIDE)
        out.flush()
        # refine cell geometry from the real terminal (CSI 16 t) if available
        cpx = _query_cell_pixels()
        if cpx:
            self.cell_w_px, self.cell_h_px = cpx
            asp = cpx[0] / cpx[1]
            if 0.3 < asp < 0.8:
                self.cell_aspect = asp
        self.poll_thread.start()
        last = time.perf_counter()
        try:
            while self.running:
                now = time.perf_counter()
                dt = min(0.1, now - last)
                last = now
                cols, rows = self._size()
                if (cols, rows) != (self.cols, self.rows):
                    self.cols, self.rows = cols, rows

                # advance state
                if not self.paused:
                    self.camera.spin += dt * self.spin_speed
                with self.lock:
                    live = []
                    for ip, t in list(self.traces.items()):
                        t.life -= dt
                        if t.life <= 0:
                            del self.traces[ip]
                            continue
                        t.phase = (t.phase + dt * self.trace_speed) % 1.0
                        # fade: full while life > FADE, then ramp to 0
                        if t.life > TRACE_FADE:
                            t.alpha = 1.0
                        else:
                            t.alpha = max(0.0, t.life / TRACE_FADE)
                        live.append(t)
                    traces = live if self.show_traces else []

                if self.hd:
                    out.buffer.write(self._frame_hd(cols, rows, live))
                else:
                    rx, ry = self._globe_radii(cols, rows)
                    frame = render_frame(cols, rows, self.camera, traces,
                                         self.cfg, rx=rx, ry=ry)
                    hud = self._hud(cols, rows, live, rx, ry)
                    out.write(frame + "\n" + hud)
                out.flush()

                # fps counter
                self._fps_acc += dt
                self._fps_n += 1
                if now - self._last_fps_t > 0.5:
                    self.fps = self._fps_n / self._fps_acc if self._fps_acc else 0
                    self._fps_acc = 0.0
                    self._fps_n = 0
                    self._last_fps_t = now

                # input + frame pacing
                self._handle_input()
                elapsed = time.perf_counter() - now
                wait = max(0.0, 1.0 / TARGET_FPS - elapsed)
                if wait > 0:
                    select.select([sys.stdin], [], [], wait)
        finally:
            self._stop.set()
            out.write(RESET + SHOW + ALT_OFF)
            out.flush()
            if old_term is not None:
                try:
                    import termios
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, old_term)
                except Exception:
                    pass

    def _handle_input(self):
        import termios  # noqa
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0)
        except Exception:
            return
        if not r:
            return
        try:
            data = os.read(sys.stdin.fileno(), 64)
        except Exception:
            return
        if not data:
            return
        for ch in data:
            c = chr(ch) if 32 <= ch < 127 else ""
            if c == "q" or ch == 0x03:        # q or Ctrl-C
                self.running = False
            elif c == " " or c == "p":
                self.paused = not self.paused
            elif c == "+":
                self.spin_speed = min(2.0, self.spin_speed + 0.05)
            elif c == "-":
                self.spin_speed = max(-2.0, self.spin_speed - 0.05)
            elif c == "r":
                self.spin_speed = -self.spin_speed
            elif c == "t":
                self.show_traces = not self.show_traces
            elif c == "g":
                self.cfg.graticule = not self.cfg.graticule
            elif c == "s":
                self.cfg.stars = not self.cfg.stars
            elif c == "h":
                self.show_hud = not self.show_hud
            elif c == "l":
                self.show_labels = not self.show_labels
            elif c == "c":
                with self.lock:
                    self.traces.clear()