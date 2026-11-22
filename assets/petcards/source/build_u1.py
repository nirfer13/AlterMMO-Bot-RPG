"""Build frames/U1.png and frames/U1-A.apng ("Rubinowa Korona", Ultra Shiny) from the owner's originals.

The owner's U1 (source/U1-rubinowa-korona.png) and its animation A12 (source/A12-puls-rubinow.apng) are
the red/gold frame of source/ultra-shiny.png with a crowned medallion painted over its top-left medallion
and a thin gold line around the whole picture.  Apart from that the bars and corner pieces are pixel for
pixel those of ultra-shiny.png, so they still had the crystal, the arcade and the old geometry that
frame-ultra.png (U0) lost on 2026-11-20 (see build_frame.py).  The owner asked for the same corrections
here, so that U0, U1 and U1-A read as one family:

1. Everything except the crowned medallion is taken from frame-ultra.png itself (crystal and arcade gone,
   enlarged ~5%, bars on the very edge).  Outside the medallion U1 is therefore identical to U0.
2. The thin gold line around the picture is dropped: enlarged with the rest it would lie just outside
   the canvas (U0 has none either).
3. The crowned medallion (disc + crown + ring with the stars) is cut out of U1 and enlarged by the same
   factor.  Placed exactly like the bars it would leave the canvas (the crown lifts in the animation),
   so the whole group is moved just far enough right/down that its left edge and the tip of the crown
   at its highest stay inside the picture.  Behind it the spiked ring of U0's medallion stays where it
   is in U0, exactly as the original U1 shows that ring around its disc.
4. The animation gets the identical correction: every frame is the static U1.png scaled to 512 px, and
   only the moving crown (with its chains and pulsing rubies) is taken from the matching A12 frame and
   moved with the medallion group.  So the still parts are pixel-stable and equal to U1.png scaled down.
   84 frames x 60 ms, endless loop; written as delta frames so the file stays small.

2026-11-21, on the owner's request ("instead of the black behind the crown a ruby colour with shadows",
and: every frame in the style of Darkest Dungeon - heavy black ink, hard-edged shadow shapes, flat
gritty colour fields, no soft gradients or glows):

5. The flat black ink disc behind the crown is repainted as an inked ruby surface: a flat gritty ruby
   field, a darker ruby half-tone band, hard-edged black ink shapes (an ink rim, a jagged crescent of
   shadow hugging one side, slashes cutting in from the edge) and a few lighter worn patches.  It is
   painted once (seeded, so the build stays deterministic) in the coordinates of the original U1 and is
   the same static texture in the picture and in every frame of the animation.
6. The crown (with chains and rubies) keeps an ink outline of KEEP px: dark pixels closer than that to
   any gold/ruby pixel stay as they are, only the ink farther away becomes ruby.  The ring with the
   stars and its dark-red inside, its outline and everything outside the disc are not touched (the
   ring is found in the static U1, where the resting crown closes the gap the ring has in A12).
7. The crown casts a hard-edged, slightly rough shadow (a darker ruby, not black) onto that surface,
   offset down-right: its silhouette (with the ink outline) swept along the offset SHADOW, so the
   shadow is a solid block with straight sides that reaches well past the outline, like a cast
   shadow in an inked drawing (2026-11-21, second pass: at the first offset of 7, 9 px it lay mostly
   under the 5 px outline and showed only as slivers).  Specks of the old thin gold line along the
   disc's edge cast none.  In the animation the shadow follows the crown and lies a little further
   away the higher the crown is lifted.  Only the crown, chains, rubies and this shadow move.

SURFACE (below) picks the treatment; change it and run the script again to switch:
    "B" (used)  the red of U0's own medallion (106, 15, 19) with torn black slashes all round, like that
                medallion, a narrow ink crescent on the right and worn patches on the upper left,
    "A"         dark wine ruby, a heavy black crescent over the upper left, slashes on the right,
    "C"         brighter ruby, a bold black crescent over the whole right side,
    "black"     the flat black ink disc as before 2026-11-21 (the output is byte-identical to that build).

Deterministic.  Needs Pillow and numpy (a build tool, not bot runtime code).

Usage (from anywhere):   python build_u1.py [out_dir [surface]]
    reads   <this folder>/U1-rubinowa-korona.png, A12-puls-rubinow.apng, ultra-shiny.png
            and <this folder>/../frame-ultra.png
    writes  <out_dir>/U1.png and <out_dir>/U1-A.apng   (out_dir defaults to <this folder>/../frames;
            each file is written under a temporary name and then renamed, so readers never see half a file)
    surface overrides SURFACE for one run (for previews), e.g.  python build_u1.py C:/tmp/u1 C
"""
import io
import os
import struct
import sys
import zlib
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "frames"

# Step 3 of build_frame.py: x' = (x - LEFT) * SCALE + MARGIN (the same for y with TOP), 1024 px units.
LEFT, TOP, MARGIN = 25.0, 35.0, 2.0
SCALE = (1024 - 2 * MARGIN) / (996.0 - LEFT)
# How far inside the canvas the medallion group must stay: its left edge / the crown's highest tip.
KEEP_IN_X, KEEP_IN_Y = 3.0, 6.0

WIN = 380                     # the medallion lies in the top-left WIN x WIN pixels of the original
BAND_X, BAND_Y = 24, 25       # the thin gold line around U1 covers these outer columns / rows
DISK = (124.97, 137.47, 111.5)  # the black ink disc: centre x, centre y, radius (fitted to its edge)
RING_FROM_ROW = 185           # below this row the group is only the disc; lower the ring sticks out
CLASP = (15, 205, BAND_X, 224)  # the left clasp of the ring reaches into the gold line's band (x0, y0, x1, y1)
FRAME_MS = 60

# -- the surface behind the crown (2026-11-21) ------------------------------------------------------
# Which treatment: "A", "B", "C" (see LOOKS) or "black" (the flat black ink disc, as before 2026-11-21).
SURFACE = "B"
if len(sys.argv) > 2:
    SURFACE = sys.argv[2]
# All sizes in pixels of the original U1 (1024 px); the animation (512 px) uses half of them.
INK_MAX = 48                  # brighter than this (max of R, G, B) = gold / ruby / chain, darker = ink
KEEP = 5.0                    # ink closer than this to the crown or the ring stays: their outline
RING_TOP = 145                # above this row the gold is the crown, below it the ring (its top lies just below)
OPEN_ABOVE = 132              # ink above this row is the open disc around the crown (the ring's inside starts lower)
GOLD_G, GOLD_CLOSE = 40, 4    # the ring's gold: green above GOLD_G; cracks up to 2 * GOLD_CLOSE px are closed
RIM = 3.5                     # the ink rim along the edge of the disc
SHADOW = (16.0, 14.0)         # the crown's shadow: offset right / down of the crown at rest
SHADOW_GROW = (0.20, 0.30)    # ... and how much further per pixel the crown is lifted
SHADOW_STEP = 0.5             # the shadow is the crown's silhouette swept along the offset in these steps
SPECK_EDGE = 6.0              # gold lying only this close to the disc's edge casts no shadow (old line specks)
SHADOW_SOFT = 1.6             # the shadow's edge: blur before the hard cut (gives the rough edge its swing)
SHADOW_ROUGH = 0.45           # how far the grain of the surface roughens that edge
# Colours of the ruby surface: base field, lighter worn patches; "mid" darkens the base for the
# half-tone band, "shade" for the crown's shadow.  Angles in degrees on the screen: 0 right, 90 down,
# 180 left, 270 up.  crescent = (from, to, widest) of the ink shadow hugging the rim; slashes = ink
# strokes cutting in from the edge (count, from angle, to angle, length range, width range); lights =
# worn patches (count, from angle, to angle).
LOOKS = {
    "A": dict(seed=101, base=(80, 13, 21), light=(104, 22, 29), mid=0.50, shade=0.30,
              crescent=(150, 290, 26), slashes=(6, -70, 50, (26, 50), (9, 15)), lights=(3, -50, 30),
              hatches=2),
    "B": dict(seed=202, base=(106, 15, 19), light=(128, 24, 27), mid=0.52, shade=0.36,
              crescent=(-70, 40, 12), slashes=(10, 0, 360, (24, 52), (8, 14)), lights=(3, 165, 255),
              hatches=2),
    "C": dict(seed=303, base=(134, 22, 31), light=(158, 35, 42), mid=0.50, shade=0.30,
              crescent=(-110, 80, 36), slashes=(4, 150, 280, (20, 40), (8, 12)), lights=(4, 160, 260),
              hatches=2),
}


# -- small helpers --------------------------------------------------------------------------------
def dilate(m, n):
    for _ in range(n):
        g = m.copy()
        g[1:] |= m[:-1]; g[:-1] |= m[1:]; g[:, 1:] |= m[:, :-1]; g[:, :-1] |= m[:, 1:]
        m = g
    return m


def erode(m, n):
    return ~dilate(~m, n)


def flood(m, seeds):
    """Pixels of m 4-connected to the seeds."""
    h, w = m.shape
    seen = np.zeros_like(m)
    q = deque(seeds)
    while q:
        y, x = q.popleft()
        if not (0 <= y < h and 0 <= x < w) or seen[y, x] or not m[y, x]:
            continue
        seen[y, x] = True
        q.extend(((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)))
    return seen


def border(m):
    h, w = m.shape
    return [(0, x) for x in range(w)] + [(h - 1, x) for x in range(w)] + \
           [(y, 0) for y in range(h)] + [(y, w - 1) for y in range(h)]


def fill_holes(m):
    return ~flood(~m, border(m))


def soft(mask, radius):
    im = Image.fromarray(mask.astype(np.uint8) * 255, "L").filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(im).astype(np.float64) / 255.0


def to_u8(a):
    return np.clip(a + 0.5, 0, 255).astype(np.uint8)


def premultiplied_to_straight(pm):
    a = pm[..., 3:4] / 255.0
    rgb = np.where(a > 0, pm[..., :3] / np.maximum(a, 1e-9), 0.0)
    return np.concatenate([rgb, pm[..., 3:4]], -1)


def place_group(rgba, unit, dx, dy, size):
    """Enlarge a layer like build_frame.py does, moved by (dx, dy) 1024-px units; unit = 1 (1024) or 0.5 (512).

    Returns premultiplied RGBA floats."""
    coeffs = (1 / SCALE, 0, unit * (LEFT - (MARGIN + dx) / SCALE),
              0, 1 / SCALE, unit * (TOP - (MARGIN + dy) / SCALE))
    im = Image.fromarray(to_u8(rgba), "RGBA").convert("RGBa")
    out = im.transform((size, size), Image.Transform.AFFINE, coeffs, resample=Image.Resampling.BICUBIC)
    return np.asarray(out).astype(np.float64)


def over(top_pm, base):
    """Premultiplied layer over a straight RGBA picture -> straight RGBA (floats)."""
    ta = top_pm[..., 3:4] / 255.0
    ba = base[..., 3:4] / 255.0
    out_a = ta + ba * (1 - ta)
    out_pm = top_pm[..., :3] + base[..., :3] * ba * (1 - ta)
    rgb = np.where(out_a > 0, out_pm / np.maximum(out_a, 1e-9), 0.0)
    return np.concatenate([rgb, out_a * 255], -1)


def load(path):
    return np.asarray(Image.open(path).convert("RGBA")).astype(np.float64)


def gauss(a, sigma):
    """Separable Gaussian blur of a float array (edges repeated)."""
    if sigma <= 0:
        return a
    r = int(np.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    for axis in range(a.ndim):
        p = np.pad(a, [(r, r) if ax == axis else (0, 0) for ax in range(a.ndim)], mode="edge")
        out = np.zeros_like(a)
        for i, kv in enumerate(k):
            sl = [slice(None)] * a.ndim
            sl[axis] = slice(i, i + a.shape[axis])
            out += kv * p[tuple(sl)]
        a = out
    return a


def shift(a, dx, dy):
    """Move a float picture by (dx, dy) pixels (bilinear); what comes in from outside is 0."""
    im = Image.fromarray(a.astype(np.float32), "F")
    out = im.transform(im.size, Image.Transform.AFFINE, (1, 0, -dx, 0, 1, -dy),
                       resample=Image.Resampling.BILINEAR)
    return np.asarray(out).astype(np.float64)


def distance_to(mask, rmax):
    """Distance of every pixel to the nearest True pixel of mask, up to rmax (farther: rmax + 1)."""
    h, w = mask.shape
    far = rmax + 1.0
    d = np.full((h, w), far)
    r = int(np.ceil(rmax))
    pad = np.pad(mask, r)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            dd = float(np.hypot(dx, dy))
            if dd <= rmax:
                np.minimum(d, np.where(pad[r + dy:r + dy + h, r + dx:r + dx + w], dd, far), out=d)
    return d


def centres(n, s):
    """Pixel centres of an n-pixel axis at scale s (1 = 1024 px, 0.5 = 512 px) in 1024-px coordinates."""
    return (np.arange(n) + 0.5) / s - 0.5


def disk_at(s):
    w = int(WIN * s)
    c = centres(w, s)
    return np.hypot(c[None, :] - DISK[0], c[:, None] - DISK[1]) <= DISK[2]


# -- APNG writer: delta frames, every frame kept (Pillow merges identical frames) --------------------
def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _idat(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    b, pos, out = buf.getvalue(), 8, b""
    while pos < len(b):
        n = struct.unpack(">I", b[pos:pos + 4])[0]
        if b[pos + 4:pos + 8] == b"IDAT":
            out += b[pos + 8:pos + 8 + n]
        pos += 12 + n
    return out


def write_apng(path, frames, ms):
    h, w = frames[0].shape[:2]
    out = [b"\x89PNG\r\n\x1a\n",
           _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)),
           _chunk(b"acTL", struct.pack(">II", len(frames), 0))]
    seq = 0
    for k, fr in enumerate(frames):
        if k == 0:
            x0, y0, x1, y1 = 0, 0, w, h
        else:
            ys, xs = np.nonzero((fr != frames[k - 1]).any(-1))
            if len(ys):
                x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            else:                                   # nothing changes: rewrite one pixel as it is
                x0, y0, x1, y1 = 0, 0, 1, 1
        # dispose: none, blend: source - the region replaces what was there, transparency included
        out.append(_chunk(b"fcTL", struct.pack(">IIIIIHHBB", seq, x1 - x0, y1 - y0, x0, y0, ms, 1000, 0, 0)))
        seq += 1
        data = _idat(Image.fromarray(fr[y0:y1, x0:x1], "RGBA"))
        if k == 0:
            out.append(_chunk(b"IDAT", data))
        else:
            out.append(_chunk(b"fdAT", struct.pack(">I", seq) + data))
            seq += 1
    out.append(_chunk(b"IEND", b""))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(b"".join(out))
    os.replace(tmp, path)


# -- the ruby surface ---------------------------------------------------------------------------
def paint_ruby(look):
    """The inked ruby surface of the disc, WIN x WIN RGB floats in the coordinates of the original U1,
    and the grain (0..1) that also roughens the crown's shadow.  Drawn 4x larger and averaged down, so
    every ink shape has a hard but clean edge."""
    rng = np.random.default_rng(look["seed"])
    cx, cy, r = DISK
    ss = 4
    n = WIN * ss
    th = np.arange(0.0, 360.0, 0.5)
    rad = np.radians(th)

    def new():
        im = Image.new("L", (n, n), 0)
        return im, ImageDraw.Draw(im)

    def poly(draw, pts, fill=255):
        draw.polygon([(float(x) * ss, float(y) * ss) for x, y in pts], fill=fill)

    def down(im):
        return np.asarray(im.resize((WIN, WIN), Image.Resampling.BOX)).astype(np.float64) / 255.0

    def bump(a0, a1, width):
        """0 outside the arc a0..a1, rising to width in its middle (a crescent's profile)."""
        t = ((th - a0) % 360) / (a1 - a0)
        return np.where(t < 1, width * np.sin(np.pi * np.clip(t, 0, 1)) ** 0.7, 0.0)

    def spikes(depth, spacing, half):
        """Sharp triangular flicks along the edge: brush strokes pulled inwards."""
        w = np.zeros_like(th)
        c = rng.uniform(0, spacing[1])
        while c < 360:
            h, d = rng.uniform(*half), rng.uniform(0.3, 1.0)
            dist = np.abs(((th - c + 180) % 360) - 180)
            w = np.maximum(w, np.clip(1 - dist / h, 0, None) * d * depth[int(c * 2) % len(th)])
            c += rng.uniform(*spacing)
        return w

    def edge(width):
        return [(cx + (r - wv) * np.cos(a), cy + (r - wv) * np.sin(a)) for wv, a in zip(width, rad)]

    def outer(extra=4.0):
        return [(cx + (r + extra) * np.cos(a), cy + (r + extra) * np.sin(a)) for a in rad]

    def at(angle, radius):
        a = np.radians(angle)
        return np.array([cx + radius * np.cos(a), cy + radius * np.sin(a)])

    crescent = bump(*look["crescent"])
    wobble = gauss(rng.normal(0, 1, len(th)), 6) * 6
    ink_w = RIM + crescent + spikes(5 + 0.6 * crescent, (8, 17), (2.2, 4.5)) + np.clip(wobble, -0.8, 0.8)
    mid_w = ink_w + 4 + 0.5 * crescent + spikes(4 + 0.5 * crescent, (6, 12), (1.5, 3.2))

    ink, d_ink = new()
    poly(d_ink, outer())
    poly(d_ink, edge(ink_w), 0)
    mid, d_mid = new()
    poly(d_mid, outer())
    poly(d_mid, edge(mid_w), 0)
    light, d_light = new()

    # slashes of ink cut in from the edge, with torn (sawtooth) sides
    count, a0, a1, length, width = look["slashes"]
    for i in range(count):
        a = a0 + (a1 - a0) * (i + rng.uniform(0.15, 0.85)) / count
        p0 = at(a, r + 4)
        turn = np.radians(rng.uniform(-24, 24))
        inward = -np.array([np.cos(np.radians(a)), np.sin(np.radians(a))])
        d = np.array([inward[0] * np.cos(turn) - inward[1] * np.sin(turn),
                      inward[0] * np.sin(turn) + inward[1] * np.cos(turn)])
        nrm = np.array([-d[1], d[0]])
        ln, wd = rng.uniform(*length), rng.uniform(*width)
        bend = rng.uniform(-0.25, 0.25) * wd           # the stroke curves a little, like a brush flick
        left, right = [], []
        for t in (0.0, 0.25, 0.5, 0.72):
            half = wd / 2 * (1 - t) ** 0.9
            mid_pt = p0 + d * ln * t + nrm * bend * 4 * t * (1 - t)
            left += [mid_pt + nrm * half,
                     mid_pt + d * ln * 0.09 + nrm * half * rng.uniform(0.25, 0.6)]
            right += [mid_pt - nrm * half,
                      mid_pt + d * ln * 0.12 - nrm * half * rng.uniform(0.3, 0.65)]
        poly(d_ink, left + [p0 + d * ln + nrm * bend * 0.5] + right[::-1])
        # a half-tone stroke beside every other one (the ink's dry edge)
        if i % 2 == 0:
            q0 = p0 + nrm * wd * rng.choice([-0.9, 0.9])
            l2 = ln * rng.uniform(0.55, 0.85)
            poly(d_mid, [q0 + nrm * wd * 0.45, q0 + d * l2 + nrm * bend, q0 - nrm * wd * 0.45])

    # lighter worn patches where the light falls, each with a few light scratches
    count, a0, a1 = look["lights"]
    for i in range(count):
        a = a0 + (a1 - a0) * (i + rng.uniform(0.2, 0.8)) / count
        c = at(a, r * rng.uniform(0.66, 0.84))
        tang = np.radians(a + 90)
        pts = []
        for j in range(7):
            b = tang + j * 2 * np.pi / 7 + rng.uniform(-0.2, 0.2)
            rr = rng.uniform(6, 12) * (2.0 if j in (0, 4) else 1.0)
            pts.append(c + rr * np.array([np.cos(b), np.sin(b)]))
        poly(d_light, pts)
        for _ in range(3):
            s0 = c + rng.uniform(-12, 12, 2)
            b = tang + rng.uniform(-0.4, 0.4)
            dv = np.array([np.cos(b), np.sin(b)])
            nv = np.array([-dv[1], dv[0]])
            ls = rng.uniform(14, 26)
            poly(d_light, [s0 - dv * ls / 2 + nv * 1.3, s0 + dv * ls / 2, s0 - dv * ls / 2 - nv * 1.3])

    # hatching: short parallel half-tone strokes where the shadow band meets the field
    ca, cb, _ = look["crescent"]
    for i in range(look["hatches"]):
        a = ca + (cb - ca) * rng.uniform(0.15, 0.85)
        j = int(round((a % 360) * 2)) % len(th)
        base = at(a, r - mid_w[j] - 2)
        b = np.radians(a + 180 + rng.uniform(-35, 35))
        dv = np.array([np.cos(b), np.sin(b)])
        nv = np.array([-dv[1], dv[0]])
        for m in range(4):
            s0 = base + nv * (m - 1.5) * 5.0
            ls = rng.uniform(12, 20)
            poly(d_mid, [s0 + nv * 1.4, s0 + dv * ls, s0 - nv * 1.4])

    ink, mid, light = down(ink), down(mid), down(light)

    # flat colour fields, then the grit of the frame's painted red
    base = np.array(look["base"], np.float64)
    tex = np.broadcast_to(base, (WIN, WIN, 3)).copy()
    tex = tex * (1 - light[..., None]) + np.array(look["light"], np.float64) * light[..., None]
    tex = tex * (1 - mid[..., None] * (1 - look["mid"]))
    grain = gauss(rng.normal(0, 1, (WIN, WIN)), 0.7)
    grain /= grain.std()
    pits = gauss(rng.normal(0, 1, (WIN, WIN)), 1.0)
    pits /= pits.std()
    mottle = gauss(rng.normal(0, 1, (WIN, WIN)), 8)
    mottle /= mottle.std()
    tex *= (1 + 0.07 * grain + 0.05 * mottle)[..., None]
    tex *= np.where(pits > 2.0, 0.6, np.where(pits < -2.2, 1.18, 1.0))[..., None]
    tex = tex * (1 - ink[..., None]) + np.array([4.0, 2.0, 3.0]) * ink[..., None]
    rough = np.clip(gauss(rng.normal(0, 1, (WIN, WIN)), 1.4) * 3.0 + 0.5, 0, 1)
    return np.clip(tex, 0, 255), rough


def open_ink(sub, s, grp, disk, inside):
    """Distance to the nearest gold and the open ink of the disc: ink farther than KEEP from any gold
    that is reached from the part of the disc above OPEN_ABOVE without coming closer than that to gold
    (and without entering `inside`, the ring's inside)."""
    w = sub.shape[0]
    colored = (sub[..., :3].max(-1) > INK_MAX) & grp
    p = np.pad(colored, 1).astype(np.int16)                 # lone specks (left over from the old gold
    count = sum(p[1 + dy:1 + dy + w, 1 + dx:1 + dx + w]     # line at the disc's edge) are not gold
                for dy in (-1, 0, 1) for dx in (-1, 0, 1))
    colored &= count >= 3
    k = KEEP * s
    d_all = distance_to(colored, k + 1.5)
    rows = centres(w, s)[:, None]
    far = disk & (d_all >= k + 0.5) & ~inside
    seeds = list(zip(*np.nonzero(far & (rows < OPEN_ABOVE))))
    return colored, d_all, far, flood(far, seeds)


def ring_inside(u1, grp):
    """The ring with its dark-red inside and stars, in the static U1 where the crown rests on the ring
    and closes it: what the gold encloses below RING_TOP.  Gold = green above GOLD_G (the red specks of
    the inside have none), its dark cracks closed first.  (In A12 the ring has a gap under the lifted
    crown, so this is taken from U1 and used for every frame.)"""
    gold = (u1[:WIN, :WIN, 1] > GOLD_G) & grp
    gold = erode(dilate(gold, GOLD_CLOSE), GOLD_CLOSE) & grp
    rows = centres(WIN, 1.0)[:, None]
    return fill_holes(gold) & (rows >= RING_TOP)


def repaint(img, s, grp, disk, inside, tex, rough, look, lift):
    """Turn the ink of the disc into the ruby surface (with the crown's shadow) in a picture at scale s
    (1 = the original U1, 0.5 = a frame of A12).  lift = how far the crown is raised, 1024-px units.
    Returns the window WIN*s x WIN*s of the repainted picture, where the surface shows (0..1) and
    the shadow (0..1).  Only the open ink (see open_ink) becomes surface: the outlines of the crown,
    chains and ring stay, and so does the dark-red inside of the ring."""
    w = int(WIN * s)
    sub = img[:w, :w]
    colored, d_all, _, reached = open_ink(sub, s, grp, disk, inside)
    k = KEEP * s
    c = centres(w, s)
    crown = colored & (c[:, None] < RING_TOP)
    # What casts the shadow: the crown with its chains, not the specks of the old thin gold line that
    # survive along the disc's edge (in A12 at 512 px more of them than in U1 at 1024 px).
    edge = np.hypot(c[None, :] - DISK[0], c[:, None] - DISK[1]) > DISK[2] - SPECK_EDGE
    caster = flood(crown, list(zip(*np.nonzero(crown & ~edge))))
    d_caster = distance_to(caster, k + 1.5)
    reached = dilate(reached, 1) & ~inside
    surface = np.clip(d_all - k + 0.5, 0, 1) * disk * reached
    shape = np.clip(k + 0.5 - d_caster, 0, 1)
    ox, oy = s * (SHADOW[0] + SHADOW_GROW[0] * lift), s * (SHADOW[1] + SHADOW_GROW[1] * lift)
    steps = max(1, int(np.ceil(np.hypot(ox, oy) / (SHADOW_STEP * s))))
    sh = shape.copy()
    for i in range(1, steps + 1):        # swept, not just moved: a solid block with straight sides
        np.maximum(sh, shift(shape, ox * i / steps, oy * i / steps), out=sh)
    sh = gauss(sh, SHADOW_SOFT * s)
    sh = np.clip((sh + SHADOW_ROUGH * (rough - 0.5) - 0.5) * 4 + 0.5, 0, 1) * surface
    lit = tex * (1 - sh * (1 - look["shade"]))[..., None]
    out = sub.copy()
    out[..., :3] = sub[..., :3] * (1 - surface[..., None]) + lit * surface[..., None]
    # The originals' ink is 250..255 opaque, in A12 varying from frame to frame: the surface is solid.
    out[..., 3] = sub[..., 3] * (1 - surface) + 255 * surface
    return out, surface, sh


def crown_lifts(frames, disk):
    """How far the crown is raised in every A12 frame (512 px), found by matching its gold to frame 0
    (only the gold inside the disc and above the ring: the crown)."""
    g0 = frames[0][..., 1].astype(np.float64)
    w = disk.shape[0]
    crown = (g0[:w, :w] > 100) & disk & (centres(w, 0.5)[:, None] < RING_TOP - 2 * KEEP)
    ys, xs = np.nonzero(crown)
    lifts = []
    for fr in frames:
        im = Image.fromarray(fr[..., 1].astype(np.float32), "F")
        best, best_s = None, 0.0
        for s in np.arange(0, 14.01, 0.1):
            moved = np.asarray(im.transform(im.size, Image.Transform.AFFINE, (1, 0, 0, 0, 1, -s),
                                            resample=Image.Resampling.BILINEAR))
            err = float(((moved[ys, xs] - g0[ys, xs]) ** 2).sum())
            if best is None or err < best - 1e-6:
                best, best_s = err, float(s)
        lifts.append(round(best_s, 1))
    return lifts


def save_png(img, path):
    tmp = path.with_name(path.name + ".tmp")
    Image.fromarray(img, "RGBA").save(tmp, "PNG", optimize=True)
    os.replace(tmp, path)


def main():
    if SURFACE not in LOOKS and SURFACE != "black":
        sys.exit(f"SURFACE must be one of {sorted(LOOKS)} or 'black', not {SURFACE!r}")
    painting = SURFACE != "black"

    # -- 1. the medallion group in the original -----------------------------------------------------
    us = load(HERE / "ultra-shiny.png")
    u1 = load(HERE / "U1-rubinowa-korona.png")
    u0 = load(HERE.parent / "frame-ultra.png")
    assert us.shape == u1.shape == u0.shape == (1024, 1024, 4)

    changed = np.abs(u1 - us).max(-1)[:WIN, :WIN] > 24          # what U1 painted over ultra-shiny.png
    yy, xx = np.mgrid[:WIN, :WIN]
    band = (xx < BAND_X) | (yy < BAND_Y)
    disk = np.hypot(xx - DISK[0], yy - DISK[1]) <= DISK[2]
    g = (changed & ~band) | disk
    g = fill_holes(erode(dilate(g, 3), 3) | disk)
    g = flood(g & ~(band & ~disk), [(int(DISK[1]), int(DISK[0]))])   # one piece: the disc and what hangs on it
    g = disk | (g & (yy >= RING_FROM_ROW))                     # above that only the disc (no stray bits of the old ring)
    group = np.zeros((1024, 1024), bool)
    group[:WIN, :WIN] = g
    group[CLASP[1]:CLASP[3], CLASP[0]:CLASP[2]] = True
    group_soft = soft(group, 0.7)

    # -- 2. the crown's motion in the animation -----------------------------------------------------
    apng = Image.open(HERE / "A12-puls-rubinow.apng")
    n_frames = apng.n_frames
    frames = []
    for i in range(n_frames):
        apng.seek(i)
        assert int(apng.info.get("duration", FRAME_MS)) == FRAME_MS
        frames.append(np.asarray(apng.convert("RGBA")))
    assert frames[0].shape == (512, 512, 4) and n_frames == 84
    first = frames[0].astype(np.int16)
    moving = np.zeros((512, 512), bool)
    for fr in frames[1:]:
        moving |= np.abs(fr.astype(np.int16) - first).max(-1) > 6
    mys, mxs = np.nonzero(moving)

    # -- 3. where the group goes --------------------------------------------------------------------
    gys, gxs = np.nonzero(group)
    gx0 = min(int(gxs.min()), 2 * int(mxs.min()))
    gy0 = min(int(gys.min()), 2 * int(mys.min()))               # the crown at its highest
    DX = max(0.0, KEEP_IN_X - ((gx0 - LEFT) * SCALE + MARGIN))
    DY = max(0.0, KEEP_IN_Y - ((gy0 - TOP) * SCALE + MARGIN))
    print(f"medallion group moved by ({DX:.2f}, {DY:.2f}) px against the bars")

    # -- 4. the static frame ------------------------------------------------------------------------
    layer = u1.copy()
    if painting:
        look = LOOKS[SURFACE]
        tex, rough = paint_ruby(look)
        inside = ring_inside(u1, group[:WIN, :WIN])
        layer[:WIN, :WIN] = repaint(u1, 1.0, group[:WIN, :WIN], disk, inside, tex, rough, look, 0.0)[0]
        print(f"surface {SURFACE}: painted")
    layer[..., 3] *= group_soft
    static = to_u8(over(place_group(layer, 1.0, DX, DY, 1024), u0))
    OUT.mkdir(parents=True, exist_ok=True)
    save_png(static, OUT / "U1.png")

    # -- 5. the animation ---------------------------------------------------------------------------
    small = Image.fromarray(static, "RGBA").convert("RGBa").resize((512, 512), Image.Resampling.LANCZOS)
    still = to_u8(premultiplied_to_straight(np.asarray(small).astype(np.float64))).astype(np.float64)
    group512 = np.asarray(Image.fromarray(to_u8(group_soft * 255), "L").resize((512, 512), Image.Resampling.BOX))
    group512 = group512.astype(np.float64) / 255.0
    # Only pixels that ever move come from the animation, everything else is the still picture, so the
    # still parts are exactly U1.png scaled down.  Inside the disc the frame's own pixels are used (the
    # disc moves with the crown); above the disc only the crown itself, recognised by differing from the
    # resting first frame (what lies behind it there is the old position of the gold line and spikes).
    inner = moving * group512
    w2 = WIN // 2
    mods = [None] * n_frames
    if painting:
        # The same surface, scaled to the 512-px frames; the crown, its outline and its shadow are
        # worked out per frame.  Where any of that changes between frames the frame's pixels are used,
        # feathered out over a few pixels of plain surface (identical in all frames) into the still picture.
        tex512 = np.asarray(Image.fromarray(to_u8(tex), "RGB").resize((w2, w2), Image.Resampling.LANCZOS))
        tex512 = tex512.astype(np.float64)
        rough512 = np.asarray(Image.fromarray(to_u8(rough * 255), "L").resize((w2, w2), Image.Resampling.LANCZOS))
        rough512 = rough512.astype(np.float64) / 255.0
        grp2 = group512[:w2, :w2] > 0.5
        disk2 = disk_at(0.5)
        inside2 = np.asarray(Image.fromarray(inside.astype(np.uint8) * 255, "L").resize((w2, w2), Image.Resampling.BOX)) > 0
        lifts = crown_lifts(frames, disk2)
        print("crown lift per frame (512 px):", " ".join(f"{v:g}" for v in lifts))
        always = np.ones((w2, w2))
        ever = np.zeros((w2, w2))
        vary = np.zeros((w2, w2), bool)
        for i, fr in enumerate(frames):
            m, surf, _ = repaint(fr.astype(np.float64), 0.5, grp2, disk2, inside2, tex512, rough512, look,
                                 2 * lifts[i])
            mods[i] = m
            always = np.minimum(always, surf)
            ever = np.maximum(ever, surf)
            if i:
                vary |= (m != mods[0]).any(-1)
        # From the frames: every pixel of the surface that changes (crown passing, shadow moving).
        # Pixels that are never surface (crown, outlines, the ring) keep the rule above.
        own = vary & (ever > 0)
        plain = always > 0.999                      # surface in every frame, never shadowed or covered
        own = own | (dilate(own, 2) & plain)
        feather = np.clip(gauss(dilate(own, 2).astype(np.float64), 1.5) * 1.5, 0, 1) * plain
        own = np.maximum(own, feather)
        inner[:w2, :w2] = np.maximum(inner[:w2, :w2], own * group512[:w2, :w2])
    result = []
    for i, fr in enumerate(frames):
        f = fr.astype(np.float64)
        lifted = np.clip((np.abs(f - first).max(-1) - 10) / 50, 0, 1) * (1 - group512) * moving
        weight = np.clip(inner + lifted, 0, 1)
        if mods[i] is not None:
            f[:w2, :w2] = mods[i]
        f[..., 3] *= weight
        result.append(to_u8(over(place_group(f, 0.5, DX, DY, 512), still)))
    write_apng(OUT / "U1-A.apng", result, FRAME_MS)
    print("written:", OUT / "U1.png", OUT / "U1-A.apng")


if __name__ == "__main__":
    main()
