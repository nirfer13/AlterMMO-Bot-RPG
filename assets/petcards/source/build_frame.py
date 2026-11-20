"""Build assets/petcards/frame-ultra.png from the owner's ultra-shiny.png (his requests of 2026-11-20).

1. The top-centre crystal goes: where it covered the top bar, the bar is filled in
   with a clean stretch of the same bar from beside it.
2. Everything that hung under the top bar between the two corner pieces goes (the
   arcade with its pointed feet, and the body of the crystal).  It is found by flood
   fill, so the corner pieces cannot be touched; the bar gets a plain black outline
   underneath, like the other three bars.
3. The frame is enlarged by about 5% and shifted so that its bars lie on the very
   edge of the picture, and the few pixels left between a bar and the border are
   closed with the outline's black.

Usage: build_frame.py <ultra-shiny.png> <frame-ultra.png>
"""
import math
import sys
from collections import deque

import numpy as np
from PIL import Image

src, out = sys.argv[1], sys.argv[2]
im = Image.open(src).convert("RGBA")
a = np.asarray(im).copy()
SIZE = a.shape[0]
assert a.shape[:2] == (1024, 1024)

# -- 1. the bar under the crystal ---------------------------------------------------------
GAP = (462, 590)          # columns where the crystal sits on the bar
DONOR_SHIFT = -128        # the bar is taken from this many columns to the side (a clean stretch)
BAR_ROWS = 76             # rows that can hold the bar
alpha = a[..., 3].astype(int)


def bar_top(x: int) -> int:
    return int(np.flatnonzero(alpha[:BAR_ROWS + 30, x] > 128)[0])


left_top = np.median([bar_top(x) for x in range(GAP[0] - 12, GAP[0] - 2)])
right_top = np.median([bar_top(x) for x in range(GAP[1] + 3, GAP[1] + 13)])
patched = a.copy()
for x in range(GAP[0], GAP[1] + 1):
    donor = a[:BAR_ROWS + 20, x + DONOR_SHIFT]
    want = left_top + (right_top - left_top) * (x - GAP[0]) / (GAP[1] - GAP[0])
    dy = int(round(want - bar_top(x + DONOR_SHIFT)))
    column = np.zeros_like(donor)
    if dy >= 0:
        column[dy:] = donor[:len(donor) - dy]
    else:
        column[:dy] = donor[-dy:]
    patched[:BAR_ROWS, x] = column[:BAR_ROWS]
# What is left of the crystal under BAR_ROWS is removed with the arcade below.
a = patched
alpha = a[..., 3].astype(int)

# -- 2. the arcade ------------------------------------------------------------------------
X0, X1 = 252, 794         # between the medallion and the arch of the right corner piece
OUTLINE_FROM = 65         # first row of the bar's new lower outline (its gold ends above)
FILLET = 18               # the cut bends down into the corner piece's arc over this many columns


def cut_row(x: int) -> int:
    """Last row of the bar's outline at column x: about 69, with the slight wobble of a drawn line."""
    row = 69 + 1.2 * math.sin(x / 37.0) + 0.8 * math.sin(x / 11.0 + 1.0)
    if x > X1 - FILLET:                                   # a rounded inner corner instead of a step
        t = (x - (X1 - FILLET)) / FILLET
        row += 13 * t * t
    return int(round(row))


solid = alpha > 24
for x in range(X0, X1 + 1):
    # the cut under the bar - without gaps where it steps down by more than a row
    low, high = sorted((cut_row(x), cut_row(min(x + 1, X1))))
    solid[low + 1:high + 2, x] = False
for x in (X0, X1):
    solid[cut_row(x) + 1:270, x] = False                  # its two ends, down past the feet
seen = np.zeros_like(solid)
queue = deque()
for y, x in ((175, 303), (175, 753), (150, 524), (200, 524), (110, 420), (110, 630), (100, 270)):
    ys, xs = np.nonzero(solid[y - 12:y + 13, x - 12:x + 13])
    if len(ys):
        queue.append((y - 12 + int(ys[0]), x - 12 + int(xs[0])))
while queue:
    y, x = queue.popleft()
    if not (0 <= y < SIZE and 0 <= x < SIZE) or seen[y, x] or not solid[y, x]:
        continue
    seen[y, x] = True
    queue.extend(((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)))
ys, xs = np.nonzero(seen)
print("removed under the bar:", int(seen.sum()), "px; x", xs.min(), "-", xs.max(), " y", ys.min(), "-", ys.max())
assert ys.min() > 60 and ys.max() < 260 and xs.min() > X0 and xs.max() < X1, "the fill leaked into a corner piece"

grow = seen.copy()                                         # with the faint anti-aliased rim around it
for _ in range(3):
    g = grow.copy()
    g[1:] |= grow[:-1]; g[:-1] |= grow[1:]; g[:, 1:] |= grow[:, :-1]; g[:, :-1] |= grow[:, 1:]
    grow = g
erase = seen | (grow & (alpha <= 24))
x_from, x_to = int(xs.min()), int(xs.max())
for x in range(x_from, x_to + 1):
    erase[:cut_row(x) + 1, x] = False                      # never above the cut
a[erase] = 0
for x in range(x_from, x_to + 1):                          # the bar's own lower outline
    cut = cut_row(x)
    a[OUTLINE_FROM:cut + 1, x] = (0, 0, 0, 255)
    a[cut + 1, x] = (0, 0, 0, 120)

# -- 3. to the edge of the picture --------------------------------------------------------
LEFT, RIGHT, TOP, BOTTOM = 25.0, 996.0, 35.0, 1006.0       # outer edges of the bars in the drawing
MARGIN = 2.0
scale = (SIZE - 2 * MARGIN) / (RIGHT - LEFT)
assert abs(scale - (SIZE - 2 * MARGIN) / (BOTTOM - TOP)) < 1e-6
coeffs = (1 / scale, 0, LEFT - MARGIN / scale, 0, 1 / scale, TOP - MARGIN / scale)
res = Image.fromarray(a, "RGBA").convert("RGBa").transform(
    im.size, Image.AFFINE, coeffs, resample=Image.BICUBIC).convert("RGBA")
r = np.asarray(res).copy()
# Close the slivers between a bar and the border (the drawn outline is not perfectly straight).
CLOSE = 9
opaque = r[..., 3] > 128
for x in range(SIZE):
    col = np.flatnonzero(opaque[:, x])
    if len(col):
        if 0 < col[0] <= CLOSE:
            r[:col[0], x] = (0, 0, 0, 255)
        if 0 < SIZE - 1 - col[-1] <= CLOSE:
            r[col[-1] + 1:, x] = (0, 0, 0, 255)
for y in range(SIZE):
    row = np.flatnonzero(opaque[y])
    if len(row):
        if 0 < row[0] <= CLOSE:
            r[y, :row[0]] = (0, 0, 0, 255)
        if 0 < SIZE - 1 - row[-1] <= CLOSE:
            r[y, row[-1] + 1:] = (0, 0, 0, 255)
Image.fromarray(r, "RGBA").save(out, optimize=True)
print("scale", round(scale, 4), "->", out)
