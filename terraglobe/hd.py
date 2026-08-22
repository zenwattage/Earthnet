"""High-definition sixel rendering path for terraglobe.

When numpy + Pillow are available, the globe and trace arcs are rasterized to a
real pixel image (anti-aliased, far higher resolution than text cells) and
blitted to the terminal with sixel -- which Foot supports. The Jarvis HUD
chrome stays as crisp text in the margins around the sixel image.

Self-contained: no img2sixel dependency (it ships a buggy build on some
systems). Pillow handles color quantization; the sixel serialization is ours.
"""
from __future__ import annotations

import math

try:
    import numpy as np
    from PIL import Image
    HAVE_HD = True
except Exception:
    HAVE_HD = False


# ---------------------------------------------------------------------------
# Land mask -> numpy (cached on the LandMask object)
# ---------------------------------------------------------------------------

def _land_array(land):
    arr = getattr(land, "_np", None)
    if arr is None:
        arr = np.frombuffer(bytes(land.data), dtype=np.uint8).astype(np.float32) / 255.0
        land._np = arr
    return arr


# ---------------------------------------------------------------------------
# Vectorized globe renderer
# ---------------------------------------------------------------------------

def render_hd(width: int, height: int, camera, traces, cfg, left: bool = False,
              ss: int = 2):
    """Render a TRON/Jarvis-style wireframe globe to (rgb, mask).

    Only glowing lines are drawn: the sphere silhouette, the lat/long
    graticule, and the coastline outlines (landmasses as outlines, not solid
    fill). Everything else is transparent (mask False) so the terminal
    background shows through. Trace arcs are composited on top. If ``left``,
    the globe hugs the left edge.

    ``ss`` supersamples the wireframe (rendered at ss×ss then box-downsampled)
    for sub-pixel anti-aliasing, so lines glide smoothly as the globe rotates
    instead of jittering pixel-to-pixel."""
    pal = cfg.palette
    Wf = max(8, width)
    Hf = max(8, height)
    W = Wf * ss
    H = Hf * ss
    ARCH_MAX = 0.57
    margin = 8 * ss
    R_h = (H - 2 * margin) / (2.0 + ARCH_MAX)
    R_w = (W - 2 * margin) / 2.0
    R = min(R_h, R_w)
    top_pad = ARCH_MAX * R + margin
    cy = top_pad + R
    cx = min(R + 6.0 * ss, W - R - 2.0 * ss) if left else W / 2.0
    inv_R = 1.0 / R

    gridc = np.array(pal.grid, dtype=np.float32)

    camera.update_matrices()
    m = camera._inv
    m0, m1, m2, m3, m4, m5, m6, m7, m8 = m

    xs = (np.arange(W, dtype=np.float32) + 0.5 - cx) * inv_R
    ys = (np.arange(H, dtype=np.float32) + 0.5 - cy) * inv_R
    NX = np.broadcast_to(xs, (H, W))
    NY = np.broadcast_to(-ys[:, None], (H, W))  # screen-up = north
    r2 = NX * NX + NY * NY
    inside = r2 <= 1.0
    z = np.sqrt(np.maximum(0.0, 1.0 - r2))

    def geo2d(nx, ny, nz):
        gy = np.clip(m3 * nx + m4 * ny + m5 * nz, -1.0, 1.0)
        gz = m6 * nx + m7 * ny + m8 * nz
        gx = m0 * nx + m1 * ny + m2 * nz
        return np.degrees(np.arcsin(gy)), np.degrees(np.arctan2(gz, gx))

    nlat, nlon = geo2d(NX, NY, z)
    flat, flon = geo2d(NX, NY, -z)

    land = cfg.land
    larr = _land_array(land)
    nla = land.n_lat
    nlo = land.n_lon

    def coverage2d(lat, lon):
        li = np.clip(((90.0 - lat) / 180.0 * nla).astype(np.int32), 0, nla - 1)
        lo = np.clip(((lon + 180.0) / 360.0 * nlo).astype(np.int32), 0, nlo - 1)
        return np.where(inside, larr[li * nlo + lo], 0.0)

    cn = coverage2d(nlat, nlon)
    cf = coverage2d(flat, flon)

    # coastline intensity via coverage gradient (Sobel-ish) -> anti-aliased.
    # Blur the coverage first so the coastline edge is smooth and doesn't
    # jitter as the quantized landmask shifts beneath the rotating globe.
    rim_band = r2 < 0.97
    def blur3(c):
        k = c.copy()
        k[1:-1, 1:-1] = (c[:-2, :-2] + c[:-2, 1:-1] + c[:-2, 2:] +
                         c[1:-1, :-2] + c[1:-1, 1:-1] + c[1:-1, 2:] +
                         c[2:, :-2] + c[2:, 1:-1] + c[2:, 2:]) / 9.0
        return k
    cn_b = blur3(cn)
    cf_b = blur3(cf)
    def grad_int(c):
        gx = np.zeros_like(c); gy = np.zeros_like(c)
        gx[:, 1:] = c[:, 1:] - c[:, :-1]
        gy[1:, :] = c[1:, :] - c[:-1, :]
        g = np.sqrt(gx * gx + gy * gy)
        return np.clip(g / 0.28, 0.0, 1.0) * inside * rim_band
    coast_n_int = grad_int(cn_b)
    coast_f_int = grad_int(cf_b)

    # graticule intensity (AA): falloff from the nearest gridline
    if cfg.graticule:
        def gdist(v, step):
            return np.abs(((v + step / 2.0) % step) - step / 2.0)
        dg = np.minimum(gdist(nlon, cfg.grid_step_lon), gdist(nlat, cfg.grid_step_lat))
        grat_int = np.clip(1.0 - dg / 0.35, 0.0, 1.0) * inside * 0.22
    else:
        grat_int = np.zeros_like(r2)

    # sphere silhouette: 1px outline (stable, binary)
    rim = np.zeros_like(inside)
    rim[1:] |= inside[1:] & ~inside[:-1]
    rim[:-1] |= inside[:-1] & ~inside[1:]
    rim[:, 1:] |= inside[:, 1:] & ~inside[:, :-1]
    rim[:, :-1] |= inside[:, :-1] & ~inside[:, 1:]
    rim_int = rim.astype(np.float32)

    # near-side (bright) and far-side (dim) wireframe intensities; near occludes far
    near_int = np.maximum.reduce([
        rim_int,
        np.where(z > 0, grat_int, 0.0),
        np.where(z > 0, coast_n_int, 0.0),
    ])
    far_int = np.maximum(np.where(z <= 0, grat_int, 0.0),
                         np.where(z <= 0, coast_f_int, 0.0))
    far_int = np.where(near_int > 0.10, 0.0, far_int)

    gridc_far = gridc * 0.30
    rgb = np.where(near_int[..., None] > 0.10,
                   gridc * near_int[..., None],
                   gridc_far * far_int[..., None])
    mask = (near_int > 0.10) | (far_int > 0.10)

    # ---- traces (glow buffer composited over the wireframe) ----
    glow = np.zeros((H, W), dtype=np.float32)
    tcolr = np.zeros((H, W, 3), dtype=np.float32)
    for tr in traces:
        if tr.alpha <= 0.02:
            continue
        _rasterize_trace(tr, camera, R, W, H, cx, cy, pal, glow, tcolr)
    nz = glow > 0.01
    tcolr[nz] = tcolr[nz] / glow[nz][:, None]
    tmask = glow > 0.2
    if tmask.any():
        bright = (0.4 + 0.6 * np.clip(glow, 0.0, 1.0))[..., None]
        rgb[tmask] = tcolr[tmask] * bright[tmask]
        mask |= tmask

    # box-downsample the supersampled render to the final resolution
    if ss > 1:
        rgb = rgb.reshape(Hf, ss, Wf, ss, 3).mean(axis=(1, 3))
        cov = mask.reshape(Hf, ss, Wf, ss).astype(np.float32).mean(axis=(1, 3))
        mask = cov > 0.30

    return np.clip(rgb, 0, 255).astype(np.uint8), mask


def _rasterize_trace(tr, camera, R, W, H, cx, cy, pal, glow, tcolr):
    a = tr.alpha
    steps = 80
    t = np.linspace(0.0, 1.0, steps + 1, dtype=np.float32)
    av = np.array(tr.a_vec(), dtype=np.float32)
    bv = np.array(tr.b_vec(), dtype=np.float32)
    dot = float(np.clip(np.dot(av, bv), -1.0, 1.0))
    omega = math.acos(dot)
    if omega < 1e-6:
        pts = np.broadcast_to(av, (steps + 1, 3)).copy()
    elif omega > math.pi - 1e-4:
        # antipode: slerp is undefined; use a perpendicular great circle
        u = np.cross(av, np.array([1.0, 0.0, 0.0], dtype=np.float32))
        if float(np.linalg.norm(u)) < 1e-6:
            u = np.cross(av, np.array([0.0, 1.0, 0.0], dtype=np.float32))
        u = (u / np.linalg.norm(u)).astype(np.float32)
        th = (t * math.pi).astype(np.float32)
        pts = av[None, :] * np.cos(th)[:, None] + u[None, :] * np.sin(th)[:, None]
    else:
        s = math.sin(omega)
        w0 = (np.sin((1 - t) * omega) / s)[:, None] * av
        w1 = (np.sin(t * omega) / s)[:, None] * bv
        pts = w0 + w1
    lift = 1.0 + np.sin(np.pi * t) * tr.arch_height()
    pts = pts * lift[:, None]
    # world transform
    mf = camera._fwd
    def mv(m, p):
        return np.stack([m[0]*p[:,0]+m[1]*p[:,1]+m[2]*p[:,2],
                         m[3]*p[:,0]+m[4]*p[:,1]+m[5]*p[:,2],
                         m[6]*p[:,0]+m[7]*p[:,1]+m[8]*p[:,2]], axis=1)
    w = mv(mf, pts)
    px = (cx + R * w[:, 0]).astype(np.float32)
    py = (cy - R * w[:, 1]).astype(np.float32)
    depth = w[:, 2]
    head = tr.phase % 1.0
    d = np.abs(t - head)
    d = np.minimum(d, 1.0 - d)
    # narrow bright moving pulse (traffic) along a dimmer body -- crisp, no wide streak
    head_pulse = np.maximum(0.0, 1.0 - d * 10.0)
    base = 0.55 * a
    gv = np.minimum(1.0, base + head_pulse * 0.45)
    behind = depth < 0
    # Occlude arc points behind the globe's disk silhouette (no backside show-through)
    proj2 = w[:, 0] ** 2 + w[:, 1] ** 2
    occluded = behind & (proj2 < 1.0)
    gv = np.where(occluded, 0.0, gv)
    gv = np.where(behind & ~occluded, gv * 0.4, gv)
    # pure trace color (laser-like); only behind-limb arcs are dimmed
    color = np.array(tr.color, dtype=np.float32)
    far = np.array(pal.trace_far, dtype=np.float32)
    col = np.broadcast_to(color, (steps + 1, 3)).copy()
    col = np.where((behind & ~occluded)[:, None], col * 0.4, col)

    # --- dense polyline interpolation so the line is continuous (no breakup) ---
    dxs = np.diff(px); dys = np.diff(py)
    segs = (np.hypot(dxs, dys).astype(np.int32) + 1).clip(1, 256)
    cum = np.concatenate(([0], np.cumsum(segs)))
    total = int(cum[-1])
    if total > 0:
        idx = np.arange(total, dtype=np.int64)
        seg_id = np.clip(np.searchsorted(cum, idx, side='right') - 1, 0, len(px) - 2)
        fr = ((idx - cum[seg_id]).astype(np.float32)) / segs[seg_id].astype(np.float32)
        PXP = (px[seg_id] + fr * dxs[seg_id]).astype(np.int32)
        PYP = (py[seg_id] + fr * dys[seg_id]).astype(np.int32)
        GVP = gv[seg_id] + fr * (gv[seg_id + 1] - gv[seg_id])
        COLP = col[seg_id] + fr[:, None] * (col[seg_id + 1] - col[seg_id])
        ok0 = GVP > 0.02
        # crisp brush: centre + 4 edge pixels only (no diagonal corners -> no
        # fuzzy white outline); keeps the line thin and laser-like
        for dy, dx, wmul in ((0, 0, 1.0), (-1, 0, 0.3), (1, 0, 0.3),
                             (0, -1, 0.3), (0, 1, 0.3)):
            yy = PYP + dy; xx = PXP + dx
            wgt = GVP * wmul
            ok = ok0 & (xx >= 0) & (xx < W) & (yy >= 0) & (yy < H)
            if not ok.any():
                continue
            np.add.at(glow, (yy[ok], xx[ok]), wgt[ok])
            np.add.at(tcolr, (yy[ok], xx[ok], 0), COLP[ok, 0] * wgt[ok])
            np.add.at(tcolr, (yy[ok], xx[ok], 1), COLP[ok, 1] * wgt[ok])
            np.add.at(tcolr, (yy[ok], xx[ok], 2), COLP[ok, 2] * wgt[ok])
    # NOTE: normalization is done once in render_hd after all traces
    # accumulate (not here) so per-trace calls don't re-divide.


def _rasterize_stars(W, H, cx, cy, R, inside, pal, col):
    import hashlib
    n = int(W * H * 0.0010)
    star = np.array(pal.star, dtype=np.float32)
    for i in range(n):
        h = hashlib.md5(f"{W}x{H}:{i}".encode()).digest()
        px = (h[0] << 8 | h[1]) % W
        py = (h[2] << 8 | h[3]) % H
        if not inside[py, px]:
            col[py, px] = star


# ---------------------------------------------------------------------------
# Sixel encoder (self-contained; PIL does quantization)
# ---------------------------------------------------------------------------

DCS = b"\x1bP"
ST = b"\x1b\\"
SIXEL_CHARS = b"?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\^_`abcdefghijklmnopqrstuvwxyz{|}~"

_FIXED_PAL_CACHE: dict = {}


def _fixed_pal_image(pal) -> "Image.Image":
    """A fixed palette that stably covers every color the renderer can produce
    (grid/trace/trace-far colors at a brightness sweep, plus behind-limb blends).
    Cached per palette so it is built once and reused across frames -- this is
    what stops adaptive-quantization color flicker as the globe spins."""
    key = (tuple(pal.grid), tuple(pal.trace), tuple(pal.trace_far),
           tuple(pal.trace_hot), tuple(pal.proto_tcp), tuple(pal.proto_udp),
           tuple(pal.proto_icmp))
    cached = _FIXED_PAL_CACHE.get(key)
    if cached is not None:
        return cached
    colors = set()
    base = [pal.grid, pal.trace, pal.trace_far, pal.trace_hot,
            pal.proto_tcp, pal.proto_udp, pal.proto_icmp]
    far = tuple(pal.trace_far)
    steps = 8
    for c in base:
        ct = tuple(int(v) for v in c)
        for i in range(steps + 1):
            b = i / steps
            colors.add(tuple(int(v * b) for v in ct))
            # behind-limb blend: col*0.5 + far*0.5, then brightness
            colors.add(tuple(int((ct[j] * 0.5 + far[j] * 0.5) * b) for j in range(3)))
    # per-destination trace colors: 8-hue sweep so distinct trace colors
    # quantize well in the fixed sixel palette (no flicker)
    import colorsys as _cs
    for hi in range(8):
        h = hi / 8.0
        r, g, b = _cs.hsv_to_rgb(h, 0.65, 1.0)
        ct = (int(r * 255), int(g * 255), int(b * 255))
        for i in range(steps + 1):
            br = i / steps
            colors.add(tuple(int(v * br) for v in ct))
            colors.add(tuple(int(v * 0.4 * br) for v in ct))
    # PIL 'P' mode supports max 256 palette entries
    sorted_colors = sorted(colors)
    if len(sorted_colors) > 256:
        sorted_colors = sorted_colors[:256]
    flat = [v for c in sorted_colors for v in c]
    pimg = Image.new("P", (len(sorted_colors), 1))
    pimg.putpalette(flat)
    _FIXED_PAL_CACHE[key] = pimg
    return pimg


def sixel_encode(rgb: "np.ndarray", mask: "np.ndarray", pal) -> bytes:
    """Encode an (H, W, 3) uint8 RGB array to sixel bytes. ``mask`` (H, W) bool
    selects opaque pixels; unmasked pixels are transparent (P2=1, not drawn).
    ``pal`` is the theme Palette, used to build a fixed quantization palette so
    colors stay stable across frames (no flicker)."""
    pimg = _fixed_pal_image(pal)
    img = Image.fromarray(rgb, "RGB").quantize(palette=pimg, dither=Image.Dither.NONE)
    pal_flat = img.getpalette()
    K = len(pal_flat) // 3
    pal_rgb = [(pal_flat[i*3], pal_flat[i*3+1], pal_flat[i*3+2]) for i in range(K)]
    indexed = np.asarray(img)        # (H, W) int palette indices

    H, W = indexed.shape
    bands = (H + 5) // 6
    pad = bands * 6 - H
    if pad:
        indexed = np.pad(indexed, ((0, pad), (0, 0)), mode="edge")
        mask = np.pad(mask, ((0, pad), (0, 0)), mode="edge")
        H = bands * 6
    indexed = indexed.reshape(bands, 6, W)
    maskb = mask.reshape(bands, 6, W)

    out = bytearray()
    out += DCS + b"0;1;0q"   # P2=1: don't pre-fill background; only masked px drawn
    out += b'"1;1;%d;%d' % (W, H)
    for i, (r, g, b) in enumerate(pal_rgb):
        out += b"#%d;2;%d;%d;%d" % (i, r * 100 // 255, g * 100 // 255, b * 100 // 255)

    for band in range(bands):
        block = indexed[band]                      # (6, W)
        mb = maskb[band]                           # (6, W)
        if not mb.any():
            out += b"-"
            continue
        ucs = np.unique(block[mb])                 # only colors in masked pixels
        if ucs.size == 0:
            out += b"-"
            continue
        masks = ((block[:, :, None] == ucs[None, None, :]) & mb[:, :, None]).astype(np.uint8)
        packed = (masks[0] | (masks[1] << 1) | (masks[2] << 2) |
                  (masks[3] << 3) | (masks[4] << 4) | (masks[5] << 5))
        for i, c in enumerate(ucs.tolist()):
            col = packed[:, i]
            if not col.any():
                continue
            out += b"#%d" % c
            _rle_row(col, out)
            out += b"$"
        out += b"-"
    out += ST
    return bytes(out)


def _rle_row(chars: "np.ndarray", out: bytearray) -> None:
    """Run-length encode a 1-D array of 6-bit sixel values into ``out``."""
    if chars.size == 0:
        return
    a = chars
    diff = np.diff(a)
    idx = np.nonzero(diff != 0)[0] + 1
    starts = np.concatenate(([0], idx))
    ends = np.concatenate((idx, [a.size]))
    vals = a[starts]
    lens = ends - starts
    for v, ln in zip(vals.tolist(), lens.tolist()):
        ch = SIXEL_CHARS[v:v+1]
        if ln == 1:
            out += ch
        elif ln <= 3:
            out += ch * ln
        else:
            remaining = ln
            while remaining > 0:
                chunk = remaining if remaining <= 255 else 255
                out += b"!%d" % chunk
                out += ch
                remaining -= chunk