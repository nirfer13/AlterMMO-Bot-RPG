"""Pet "cards": the picture of a pet with its frame, seal and Holo foil drawn on it.

A pure image module - no Discord, no database.  ``features/pets.py`` asks
:func:`card` for the picture to send and gets back either the pet's own
illustration (nothing to draw, or drawing failed) or a composed file in the
cache folder.

What is drawn (the owner's spec, package of 2026-11-21)
-------------------------------------------------------
Layers, bottom to top: **illustration -> foil -> ONE frame layer -> seal**.
The frame and the seal lie above the foil and are not tinted by it.

* The *frame* is a RESULT of the pet's class pool (:data:`POOLS`, the
  owner's ``assets/petcards/source/pule.json``): Shiny ``S0`` (silver
  fittings, ``frame-shiny.png``) and four rarer ones, Ultra Shiny ``U0``
  (gold frame, ``frame-ultra.png``) and four rarer ones, Mythic ``M0`` (no
  frame: the pet has its own GIF) and three rarer ones.  Every rarer frame
  but the base has an animated edition (``-A``, an APNG that REPLACES the
  static frame).  An ordinary pet never has a frame.  Which result a row is
  drawn with: :func:`frame_of` (a stored id that is unknown or belongs to
  another class falls back to the base of the class).
* The *seal* (tier 1-3, ``seal-<tier>.png``) sits in the bottom-right corner.
* The *foil* is ``holo.apng``: an independent RGBA layer, 64 frames of 50 ms.
  Full Holo (:data:`FOIL_FULL`) is that layer scaled to the card and
  alpha-composited over the WHOLE illustration.  Partial Holo
  (:data:`FOIL_PARTIAL`) is the same layer seen only through a mask of the
  pet's details, computed from the picture (see "Partial Holo" below).

Static overlays are 1024x1024, animated frames 512x512; both are stretched to
the size of the picture they go on - also when that picture is not square
(the art of the game is square, so this is only the sensible thing to do with
a foreign file).

What comes out
--------------
* Still picture, no foil, static frame or none: a **PNG** of the source's size
  (alpha kept when the source has it).  zlib level 6: a 1024x1024 card is
  encoded in ~0.1 s; level 9 / ``optimize`` would cost 0.4-0.5 s for a file 3%
  smaller.
* Anything that moves is an **animated GIF**: an animated source (the mythic
  GIFs), a foil, an animated frame - one output timeline, every other moving
  layer picks its picture *by time* so that the whole loops seamlessly:

  - animated source: every source frame with its own duration and the
    source's loop setting, at the source's size (the tempo of the art is
    kept).  A layer with ``n`` pictures and a loop of ``L`` ms (the foil:
    64 x 50 = 3200 ms, an animated frame: 84 x 60 = 5040 ms) makes
    ``k = max(1, round(D / L))`` turns per source loop of ``D`` ms; the output
    frame that starts at ``t`` shows its picture::

        index = floor((t * k / D) * n) % n          (layer_index)

    so one turn takes ``D / k`` instead of exactly ``L``: the foil 2520 ms on
    the 5040 ms loop of ``Void/0.gif`` and 3520 ms on the 7040 ms loops of
    ``Void/10`` / ``Void/100``; an animated frame 5040, 7040 and 6040 ms
    (``eggs/mythic/Void/0.gif``) - one turn per loop.
  - still source + animated frame: the frame's own 84 x 60 ms timeline, 512 px
    (:data:`HOLO_SIZE`; a smaller source is not enlarged); a foil on it takes
    ``k = round(5040 / 3200) = 2`` turns (2520 ms each) by the same rule.
  - still source + foil, static frame or none: the foil's 64 x 50 ms, 512 px.

Transparent areas of a picture that becomes a GIF are flattened onto black
(GIF knows no partial transparency).

Partial Holo
------------
The owner's word for it: "it works on selected details of the pet".  His
package has hand-painted samples for two pictures only - an RGBA foil layer
over ``baza/kot.png`` (= ``assets/pets/Cat/0.png`` scaled to 512 px) and over
``baza/pustka.gif`` (byte for byte ``assets/pets/Void/0.gif``): the rim light
of the cat's outline and its chest fur; the glowing cracks, the gem and the
crest of the void egg.  The game has ~120 pictures and no masks, so the mask
is computed from the picture itself (Pillow only, :func:`_detail_mask`):

1. the picture is reduced to 256 px (:data:`PARTIAL_WORK`) and made grey;
2. *detail light* = how much brighter a pixel is than its surroundings
   (grey minus its Gaussian blur of radius 6, negatives cut off) - the rim
   lights, glowing cracks, gems, claws and highlights of the art;
3. multiplied by a soft ellipse around the subject (centre 50% / 55%, radii
   34% / 40% of the picture, fading over the outer 22% of the radius) - the
   pets stand in the middle, the moons and torches of the backgrounds do not;
4. the brightest 2% of the result (a per-picture percentile, so a dark and a
   bright illustration get the same amount; never below level 6) become 255,
   with a linear ramp from 75% of that level up, then a blur of 1 px;
5. a GIF gets ONE mask, from the average of 8 frames spread over its loop:
   it never flickers and a detail that moves fades out instead of jumping.

The foil is drawn through that mask with its alpha ``a`` lifted to
``120 + 0.6 a`` (the real foil has a = 15..255, mean 84, so 129..255; the
hand-made samples reach 206 / 226) so that the details shine clearly even
where the foil itself is faint - with the samples' own strength (about
``80 + a / 2``) the rim of the cat was hard to notice at 512 px.  A clear
pixel of the foil stays clear.  Measured against the hand-made alpha (its
maximum over all frames, at 384 px):

=========  ====================  ====================  ======  =========
picture    mask >16 / >64 (ours) hand >16 / >64        recall  precision
=========  ====================  ====================  ======  =========
cat        7.6% / 3.9%           7.6% / 3.8%           0.41    0.75
void egg   7.1% / 3.9%           5.2% / 2.1%           0.51    0.46
=========  ====================  ====================  ======  =========

(recall: share of the hand-made alpha that our mask also covers; precision:
share of our mask inside the hand-made one widened by 4 px.)  Same amount of
foil, the same places for the cat's outline and the egg's cracks and gem;
what we do not get is the filled chest fur (a large bright area is not
"brighter than its surroundings" inside), the samples' exclusion of the
cat's eyes, and a little of the rock under the cat is tinted too.  Over all
137 pictures of ``assets/pets`` and ``assets/eggs`` the mask covers 6.6-14.9%
of the card (>16, median 9.8%).  A flat picture with no detail light at all
gets a soft glow in the middle instead of nothing (none of the game's
pictures needs it).  The mask costs ~25 ms for a still picture and ~0.5 s
for a mythic GIF (8 frames decoded).

GIF quality - the trade-off chosen
----------------------------------
* **One palette for the whole animation**, no per-frame palettes: with
  per-frame palettes (what Pillow does with RGB frames) areas that do not move
  - the frame, the seal - change colour slightly from frame to frame, i.e.
  they flicker, and the file is ~40% larger.  The palette is an octree
  quantisation of 16 composed frames spread over the loop (the frames in
  between come out 0.1 dB worse at most); of the methods Pillow offers it is
  the one that keeps small saturated details (a pet's eyes, the red of a
  seal) which median cut averages away.
* **No dithering.**  Error diffusion makes every pixel flicker and the file
  30-45% larger; an ordered pattern is stable but visible as a cross-hatch on
  512 px and still +10%.  The price is slight banding in the smoothest parts
  of the rainbow; on painted illustrations it is hard to find.
* **Unchanged pixels are not written again** (Pillow's transparent delta
  frames), and a pixel whose new colour is within ``tolerance`` levels of what
  is already on the screen counts as unchanged.  That is what makes static
  areas truly static, and with tolerance 8 the file shrinks by ~12% at the
  same measured quality.
* An animated source without foil keeps **its own colours**: its palette is
  taken over as it is (the mythic GIFs use ~100 colours) and only the slots
  that are left are spent on the overlays (seal, frame).  An overlay colour
  within 8 levels of one of the source's is not added (Pillow maps colours
  through a cache of 4-level cells, and a frame covers the whole card), so
  the art stays within a few levels of the original (Void/0 with R6 and a
  seal: at most 7 levels, 9 without that rule).

File size
---------
Discord refuses uploads above the guild's limit, so every result is at most
``max_bytes``.  GIFs go down a fixed ladder (:data:`LADDER`: more tolerance,
smaller canvas, fewer colours, every second frame dropped with its duration
added to the frame before it) and the first variant that fits is taken; the
log says which.  Still cards are scaled down the same way
(:data:`STILL_SCALES`).  When nothing fits :class:`CardTooBig` is raised.

Measured with the art of the game (2026-11-21, Pillow 13.0, the owner's PC,
CPU seconds of one cold render; ``max_bytes`` 8 MiB - the bot's real limit,
10 MiB less 5%, gives the same rung everywhere except where noted).  Rung 0
is the first variant tried (FAITHFUL when there is no foil):

=====================================================  ===========  =======  ====  =======
card                                                   output       size     rung  time
=====================================================  ===========  =======  ====  =======
still + static frame + seal (Cat/100, R1)              PNG 1024     1.69 MB  -     0.3 s
still + full Holo (Cat/100, S0) - as before            512, 64 fr   5.91 MB  0     2.9 s
still + animated frame (Cat/100 S1-A, Dragon/100 R6-A) 512, 84 fr   0.4 MB   0     1.1 s
still + animated frame + full Holo (Cat/100, S1-A)     512, 84 fr   8.03 MB  0     4.1 s
... + seal (Dragon/100, U1-A)                          512, 84 fr   6.91 MB  0     4.2 s
mythic GIF + static frame (Void/10, R5)                439, 169 fr  1.69 MB  0     4.6 s
mythic GIF + static frame + seal (Void/0, R9)          441, 121 fr  1.11 MB  0     3.7 s
mythic GIF + animated frame (Void/10, R6-A)            439, 169 fr  2.01 MB  0     5.8 s
mythic egg + animated frame (eggs/mythic/Void/0, R9-A) 448, 145 fr  0.84 MB  0     3.8 s
mythic GIF + animated frame + full Holo + seal         439, 169 fr  8.27 MB  0     6.8 s
  (Void/10, R9-A; Void/0 with R5-A: 7.46 MB, 5.8 s)
mythic GIF + full Holo + seal, no frame (Void/0)       441, 121 fr  7.04 MB  1     9.7 s
  - as before; under the bot's limit rung 0: 8.52 MB, 4.9 s
partial Holo on a still (Cat/0)                        512, 64 fr   0.45 MB  0     2.4 s
partial Holo + animated frame + seal (Cat/0, S1-A)     512, 84 fr   0.84 MB  0     3.7 s
partial Holo on a mythic GIF (Void/0)                  441, 121 fr  1.13 MB  0     4.4 s
partial Holo + animated frame + seal (Void/10, R6-A)   439, 169 fr  1.75 MB  0     6.6 s
=====================================================  ===========  =======  ====  =======

The renderer of 2026-11-20 makes byte-identical full-Holo cards in the same
time on this machine today (2.8 s for Cat/100 instead of the 1.1 s noted
then).  The largest results are close to 8 MiB: on a guild with exactly that
limit a slightly busier picture would take rung 1 (one more encoding, ~+60%
time); Discord's default limit (10 MiB) leaves room.  A static frame on a
mythic GIF costs about as much as a seal (the whole card is redrawn: 1-1.7 MB,
about the size of the source).

Cache
-----
Results are kept on disk: ``card-<Type>-<n>-<what>-<digest>.<png|gif>``, a
name that is safe as a Discord attachment name.  The digest covers everything
the picture depends on - the source (path, size, mtime), the look,
``max_bytes``, every overlay file used (frame, animated frame, seal, foil:
size, mtime) and :data:`RENDER_VERSION` (which also stands for the constants
of the partial-Holo mask) - so a changed illustration or overlay simply gets
a new file and the old one ages out.  Files are written under a temporary
name and renamed, a cache hit touches the file, and after every new card the
least recently used ones are removed while the folder holds more than
:data:`CACHE_MAX_BYTES`.

Memory and time
---------------
The foil file is 20 MB and an animated frame 0.3-2 MB of APNG, but decoding
all their frames takes ~0.15 s, so nothing of them is kept between renders:
their frames are decoded one at a time (never 64 or 84 RGBA frames at once),
and so are the frames of an animated source.  What a render holds is the
finished palette frames (17 MB for a Holo card, 22 MB for 84 frames of an
animated frame, 33 MB for the longest mythic GIF), the copies Pillow's GIF
writer makes of them and the encoded file.  One render runs at a time
(:func:`card`).

Pillow 10.2 or newer is needed for the small files: older versions write the
unchanged pixels of every frame again (a mythic GIF with a seal: 5.4 MB
instead of 1.1 MB).  Without Pillow the module still imports and
:func:`card` returns the plain picture.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from ..settings import ASSETS_DIR

try:  # the bot must start (and show pets plain) on a machine without Pillow
    from PIL import Image, ImageChops, ImageFilter
except Exception:  # noqa: BLE001 - also a broken installation (missing DLL ...)
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Folder of the overlays; looked up at every render (tests point it elsewhere).
ASSETS = ASSETS_DIR / "petcards"
#: Names of the seal tiers, as shown to the players.
SEAL_NAMES: dict[int, str] = {1: "Tropiciela", 2: "Łowcy", 3: "Arcyłowcy"}
#: Largest file :func:`render` returns unless the caller knows the guild's limit.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: Part of every cache key: bump it when the pictures this module draws change.
RENDER_VERSION = 2
#: The cache folder is pruned (least recently used first) above this size.
CACHE_MAX_BYTES = 300 * 1024 * 1024
#: An unfinished file older than this was left behind by a crash and is removed.
TEMP_MAX_AGE_S = 3600.0

SEAL_FILE = "seal-{tier}.png"
HOLO_FILE = "holo.apng"
#: Duration of a foil frame when the file does not say (the owner's foil: 64 x 50 ms).
HOLO_FRAME_MS = 50
#: Duration of an animated frame's picture when the file does not say (84 x 60 ms).
FRAME_ANIMATION_MS = 60
#: Longer side of an animated card made from a still picture (Holo, animated
#: frame), like the owner's previews; smaller sources are not enlarged.
HOLO_SIZE = 512

#: zlib level of the still cards (see the module docstring for the measurements).
PNG_LEVEL = 6
#: How many composed frames, spread over the loop, the GIF palette is made from.
PALETTE_SAMPLES = 16
#: An animated source keeps its own colours when at least this many palette
#: slots are left for the overlay drawn on it.
OVERLAY_COLOURS_MIN = 48
#: ... and an overlay colour this close (levels per channel) to one of the
#: source's is not added to that palette (see _palette).
OWN_COLOUR_GUARD = 8

# The partial-Holo mask (module docstring, "Partial Holo").  Changing any of
# these changes the pictures: bump RENDER_VERSION with them.
PARTIAL_WORK = 256                  # px of the picture the mask is computed on
PARTIAL_DETAIL_RADIUS = 6.0         # "brighter than its surroundings" within this blur radius
PARTIAL_CENTRE = (0.50, 0.55)       # of the picture: where the pet stands
PARTIAL_RADII = (0.34, 0.40)        # of the picture: the ellipse around it
PARTIAL_FADE = 0.22                 # outer part of the radius over which the ellipse fades out
PARTIAL_COVERAGE = 0.02             # the brightest 2% of detail light become full mask
PARTIAL_RAMP = 0.75                 # ... with a linear ramp from 75% of that level
PARTIAL_MIN_LEVEL = 6               # a level below this is never "detail light"
PARTIAL_SOFTEN = 1.0                # blur radius of the finished mask (work px)
PARTIAL_SAMPLES = 8                 # frames of a GIF the mask is computed from (averaged)
PARTIAL_ALPHA = (120, 0.6)          # foil alpha a seen through the mask: 120 + a * 0.6 (at most 255)
PARTIAL_EMPTY = 0.002               # a mask covering less than this is replaced by a soft glow

_TEMP_SUFFIX = ".tmp"
_CARD_NAME = re.compile(r"card-[A-Za-z0-9_-]+-[0-9a-f]{12}\.(?:png|gif)")
_TEMP_NAME = re.compile(r"card-[A-Za-z0-9_.-]+" + re.escape(_TEMP_SUFFIX))


class CardError(Exception):
    """A card could not be made; :func:`card` shows the plain picture instead."""


class CardTooBig(CardError):
    """Not even the smallest variant fits into ``max_bytes``."""


# ----------------------------------------------------------------- catalogue

FOIL_NONE, FOIL_PARTIAL, FOIL_FULL = 0, 1, 2
#: Names of the foils, as shown to the players.
FOIL_NAMES: dict[int, str] = {FOIL_PARTIAL: "Holo częściowe", FOIL_FULL: "Holo pełne"}
#: How the label of an animated edition reads (:func:`frame_label`).
ANIMATED_LABEL = "{name} (animowana)"
#: Suffix of the result id of an animated edition ("S1-A").
ANIMATED_SUFFIX = "-A"


@dataclass(frozen=True)
class Frame:
    """One RESULT of a class's frame pool: a frame, static or its animated edition."""

    id: str              # "S0", "S1", "S1-A", ...
    tier: str            # "S" | "U" | "M"
    name: str            # "Gwiazda Szronu" (Polish, shown to players)
    file: str            # path relative to ASSETS ("" for M0: nothing is drawn)
    animated: bool       # the "-A" editions
    percent: float       # chance inside the tier's pool

    @property
    def base(self) -> bool:
        """True for the base result of the class (S0, U0, M0)."""
        return BASE_FRAME.get(self.tier) == self.id

    @property
    def label(self) -> str:
        """The name as shown to players: "Gwiazda Szronu" or "Gwiazda Szronu (animowana)"."""
        return ANIMATED_LABEL.format(name=self.name) if self.animated else self.name


def _pool(tier: str, base: tuple[str, str, str, float],
          rarer: tuple[tuple[str, str, float, float], ...]) -> tuple[Frame, ...]:
    """A class's results: its base and, per rarer frame, the static and the animated edition."""
    ident, name, file, percent = base
    results = [Frame(ident, tier, name, file, False, float(percent))]
    for ident, name, static, animated in rarer:
        results.append(Frame(ident, tier, name, f"frames/{ident}.png", False, float(static)))
        results.append(Frame(ident + ANIMATED_SUFFIX, tier, name, f"frames/{ident}{ANIMATED_SUFFIX}.apng",
                             True, float(animated)))
    return tuple(results)


#: Every class's results in the order of the owner's ``pule.json``; the
#: percents (every rarer result x3 since 2026-11-22, the base takes the rest) of a class sum to 100 (tests check that, and that every file exists).
POOLS: dict[str, tuple[Frame, ...]] = {
    "S": _pool("S", ("S0", "Srebrne okucia", "frame-shiny.png", 52), (
        ("S1", "Gwiazda Szronu", 24, 1.65),
        ("R1", "Księżycowa stal", 12, 0.6),
        ("R2", "Miedziane ciernie", 6, 0.45),
        ("R7", "Przeszywana skóra", 3, 0.3),
    )),
    "U": _pool("U", ("U0", "Złoty relikwiarz", "frame-ultra.png", 52), (
        ("U1", "Rubinowa Korona", 24, 1.65),
        ("R3", "Złote wrota", 12, 0.6),
        ("R4", "Rozbite zwierciadło", 6, 0.45),
        ("R8", "Bursztynowy skarabeusz", 3, 0.3),
    )),
    "M": _pool("M", ("M0", "Oryginalna ilustracja", "", 70), (
        ("R5", "Astrolabium", 15, 1.5),
        ("R6", "Kręgosłup Pustki", 9, 0.9),
        ("R9", "Oko Otchłani", 3, 0.6),
    )),
}
#: Every result id -> its Frame.
FRAMES: dict[str, Frame] = {frame.id: frame for pool in POOLS.values() for frame in pool}
#: The result a pet of the class is drawn with when nothing else is stored.
BASE_FRAME: dict[str, str] = {"S": "S0", "U": "U0", "M": "M0"}


def _foil_value(foil: Any) -> int:
    """``foil`` as a FOIL_* number; a bool is the old Holo flag (True = full Holo)."""
    if foil is True:
        return FOIL_FULL
    if foil is False or foil is None:
        return FOIL_NONE
    return foil


@dataclass(frozen=True)
class Look:
    """What is drawn over a pet's picture."""

    frame: str = ""      # "" (nothing drawn) or a result id from FRAMES ("M0" is normalised to "")
    seal: int = 0        # 0..3
    foil: int = FOIL_NONE

    def __post_init__(self) -> None:
        # A result that draws nothing (M0) is the same look as no frame at all:
        # one cache entry, ``plain`` when nothing else is set.
        known = FRAMES.get(self.frame) if isinstance(self.frame, str) else None
        if not self.frame or (known is not None and not known.file):
            object.__setattr__(self, "frame", "")
        object.__setattr__(self, "foil", _foil_value(self.foil))

    @property
    def plain(self) -> bool:
        """True when nothing is drawn over the source picture."""
        return not (self.frame or self.seal or self.foil)


# ------------------------------------------------------------------- the look

def _field(pet: Any, key: str) -> Any:
    """``pet[key]``, None when the row has no such column (or there is no row)."""
    try:
        return pet[key]
    except (KeyError, IndexError, TypeError):
        return None


def _flag(pet: Any, key: str) -> bool:
    """``pet[key]`` as a truth value; a missing column, NULL or a value that cannot say is False."""
    try:
        return bool(_field(pet, key))
    except Exception:  # noqa: BLE001 - an object whose __bool__ fails is not a "yes"
        return False


def pillow_available() -> bool:
    """Whether Pillow could be imported, i.e. whether cards can be composed on this machine."""
    return Image is not None


def tier_of(pet: Mapping) -> str:
    """The class of a ``PETS`` row: "M" / "U" / "S", or "" for an ordinary pet.

    Priority MYTHIC > ULTRA_SHINY > SHINY; a missing key or NULL is False.
    """
    if _flag(pet, "mythic"):
        return "M"
    if _flag(pet, "ultra_shiny"):
        return "U"
    if _flag(pet, "shiny"):
        return "S"
    return ""


def frame_of(pet: Mapping) -> Frame | None:
    """The frame result this row is drawn with; None for an ordinary pet.

    The stored ``frame`` id when it is a result of the pet's own class,
    otherwise - no column yet, ``''``, NULL, an unknown id, an id of another
    class, garbage - the base of the class.
    """
    tier = tier_of(pet)
    if not tier:
        return None
    stored = _field(pet, "frame")
    if isinstance(stored, str):
        found = FRAMES.get(stored.strip().upper())
        if found is not None and found.tier == tier:
            return found
    return FRAMES[BASE_FRAME[tier]]


def frame_label(pet: Mapping) -> str:
    """The frame's name for the pet's description: "" for the base of the class (or no
    frame), else e.g. "Gwiazda Szronu" or "Gwiazda Szronu (animowana)"."""
    frame = frame_of(pet)
    return "" if frame is None or frame.base else frame.label


def look_of(pet: Mapping) -> Look:
    """The full look of a ``PETS`` row (asyncpg Record or dict, lower-case keys).

    Reads ``mythic``, ``ultra_shiny``, ``shiny``, ``frame``, ``seal``,
    ``holo`` and ``holo_partial``, every one optional: rows from before the
    cosmetics or the frames have none of them.  The frame is :func:`frame_of`;
    the seal arrives as Decimal or int and is clamped to 0..3; ``holo`` is full
    Holo and wins over ``holo_partial``.  Garbage never raises - it counts as
    nothing.  Whether all of it is *shown* (the owner supports the game) is
    the caller's decision: see :func:`base_look`.
    """
    frame = frame_of(pet)
    try:
        seal = int(_field(pet, "seal"))
    except (TypeError, ValueError, ArithmeticError):  # NULL, text, Decimal NaN / Infinity
        seal = 0
    if _flag(pet, "holo"):
        foil = FOIL_FULL
    elif _flag(pet, "holo_partial"):
        foil = FOIL_PARTIAL
    else:
        foil = FOIL_NONE
    return Look(frame=frame.id if frame is not None else "", seal=min(max(seal, 0), max(SEAL_NAMES)),
                foil=foil)


def base_look(pet: Mapping) -> Look:
    """What the pet of a player who does not support the game shows: the base frame of
    its class and nothing else (seal, foil and a rarer frame stay stored, unseen)."""
    tier = tier_of(pet)
    return Look(frame=BASE_FRAME[tier] if tier else "")


def _frame(look: Look) -> Frame | None:
    """The Frame of ``look`` (None for no frame); CardError for an unknown id."""
    if not look.frame:
        return None
    frame = FRAMES.get(look.frame) if isinstance(look.frame, str) else None
    if frame is None:
        raise CardError(f"unknown frame {look.frame!r}")
    return frame


def _overlays(look: Look) -> list[Path]:
    """The overlay files this look needs, in drawing order (the foil first)."""
    frame = _frame(look)
    if look.seal and look.seal not in SEAL_NAMES:
        raise CardError(f"unknown seal tier {look.seal!r}")
    if look.foil not in (FOIL_NONE, FOIL_PARTIAL, FOIL_FULL):
        raise CardError(f"unknown foil {look.foil!r}")
    files = [HOLO_FILE] if look.foil else []
    if frame is not None:
        files.append(frame.file)
    if look.seal:
        files.append(SEAL_FILE.format(tier=look.seal))
    return [ASSETS / name for name in files]


# ---------------------------------------------------------------------- cache

def _ascii(text: str, fallback: str) -> str:
    """``text`` reduced to what an attachment name may safely hold."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:24] or fallback


def _stem(source: Path, look: Look, max_bytes: int) -> str:
    """File name (without extension) of the card: readable prefix + digest of all inputs.

    Raises ``OSError`` when the source or an overlay does not exist - before
    anything is rendered.
    """
    digest = hashlib.sha256()

    def add(*parts: object) -> None:
        digest.update(("\x1f".join(str(part) for part in parts) + "\x1e").encode("utf-8"))

    stat = source.stat()
    add(RENDER_VERSION, source.resolve(), stat.st_size, stat.st_mtime_ns,
        look.frame, look.seal, int(look.foil), int(max_bytes))
    for overlay in _overlays(look):
        stat = overlay.stat()
        add(overlay.relative_to(ASSETS).as_posix(), stat.st_size, stat.st_mtime_ns)
    foil = {FOIL_PARTIAL: "holopart", FOIL_FULL: "holo"}.get(look.foil, "")
    what = [_ascii(look.frame, "") if look.frame else "", f"seal{look.seal}" if look.seal else "", foil]
    label = "-".join([_ascii(source.parent.name, "pet"), _ascii(source.stem, "0"), *filter(None, what)])
    return f"card-{label}-{digest.hexdigest()[:12]}"


def _cached(cache_dir: Path, stem: str) -> Path | None:
    """The finished card called ``stem`` (touched: it was just used), or None."""
    for extension in ("gif", "png"):
        path = cache_dir / f"{stem}.{extension}"
        if path.is_file():
            try:
                os.utime(path)
            except OSError:  # read-only folder: the card is still good
                pass
            return path
    return None


def _store(cache_dir: Path, name: str, data: bytes) -> Path:
    """Write ``data`` as ``cache_dir/name`` so that the name only ever holds a whole file."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / name
    temp = cache_dir / f"{name}.{os.getpid()}-{uuid.uuid4().hex[:8]}{_TEMP_SUFFIX}"
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, target)
        except PermissionError:
            # Windows: another process made the same card and is reading it
            # right now.  Its copy is as good as ours.
            if not target.is_file():
                raise
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def _prune(cache_dir: Path, keep: Path | None = None) -> list[Path]:
    """Remove stale unfinished files, then the least recently used cards above the limit.

    Only files named as this module names them are counted or removed - never
    ``keep`` (the card just made) and never a foreign file in the folder.
    Returns what was removed.
    """
    removed: list[Path] = []
    cards: list[tuple[float, int, Path]] = []
    now = time.time()
    try:
        entries = list(os.scandir(cache_dir))
    except OSError:
        return removed
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            path = Path(entry.path)
            if _TEMP_NAME.fullmatch(entry.name):
                if now - stat.st_mtime > TEMP_MAX_AGE_S:
                    path.unlink()
                    removed.append(path)
            elif _CARD_NAME.fullmatch(entry.name):
                cards.append((stat.st_mtime, stat.st_size, path))
        except OSError:  # gone meanwhile, or in use: the next round gets it
            continue
    total = sum(size for _, size, _ in cards)
    for _, size, path in sorted(cards, key=lambda card: (card[0], card[2].name)):
        if total <= CACHE_MAX_BYTES:
            break
        if keep is not None and path.name == keep.name:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path)
    return removed


# ------------------------------------------------------------------- drawing

def _fit(size: tuple[int, int], longest: int) -> tuple[int, int]:
    """``size`` scaled so that its longer side is at most ``longest`` (never enlarged)."""
    width, height = size
    side = max(width, height)
    if side <= longest:
        return size
    return max(1, round(width * longest / side)), max(1, round(height * longest / side))


def _resized(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """``image`` stretched to ``size``: Lanczos when shrinking, bicubic (no ringing) when enlarging."""
    if image.size == size:
        return image
    shrinking = size[0] * size[1] < image.width * image.height
    return image.resize(size, Image.Resampling.LANCZOS if shrinking else Image.Resampling.BICUBIC)


def _top(look: Look, size: tuple[int, int]) -> Image.Image | None:
    """The still overlays above the foil - a static frame and the seal - as one RGBA
    layer of ``size`` (None when there is neither).  An animated frame is not part
    of it: it is drawn frame by frame below this layer."""
    layer = None
    for path in _overlays(look):
        if path.name == HOLO_FILE or path.suffix == ".apng":
            continue
        with Image.open(path) as overlay:
            piece = _resized(overlay.convert("RGBA"), size)
        if layer is None:
            layer = piece
        else:
            layer.alpha_composite(piece)
    return layer


def _on_black(picture: Image.Image) -> Image.Image:
    """RGBA ``picture`` made opaque (transparent areas become black); used for GIF frames."""
    if picture.getextrema()[3][0] == 255:
        return picture
    canvas = Image.new("RGBA", picture.size, (0, 0, 0, 255))
    canvas.alpha_composite(picture)
    return canvas


class _Layer:
    """An animated overlay (the foil, an animated frame), decoded one frame at a time
    (see "Memory and time" above).

    ``mask`` (an L picture of any size) makes it the partial Holo: the layer's
    alpha is lifted (:data:`PARTIAL_ALPHA`) and multiplied by the mask.
    """

    def __init__(self, path: Path, frame_ms: int = HOLO_FRAME_MS, mask: Image.Image | None = None) -> None:
        self._path = path
        self._image = Image.open(path)
        self._at = 0
        self._last: tuple[int, tuple[int, int], Any] | None = None
        self._mask = mask
        self._masks: dict[tuple[int, int], Image.Image] = {}
        self.count = int(getattr(self._image, "n_frames", 1))
        self.frame_ms = int(self._image.info.get("duration") or frame_ms)
        self.loop_ms = self.count * self.frame_ms

    def _mask_for(self, size: tuple[int, int]) -> Image.Image:
        mask = self._masks.get(size)
        if mask is None:
            self._masks.clear()   # one rung of the ladder at a time
            mask = self._masks[size] = self._mask.resize(size, Image.Resampling.BICUBIC)
        return mask

    def frame(self, index: int, size: tuple[int, int]) -> Image.Image:
        """Picture ``index`` as RGBA of ``size``."""
        if self._last is not None and self._last[:2] == (index, size):
            return self._last[2]
        if index < self._at:
            # Pillow cannot rewind an APNG from the middle ("image file is
            # truncated"); opening it again costs nothing.
            self._image.close()
            self._image = Image.open(self._path)
            self._at = 0
        self._image.seek(index)
        self._at = index
        picture = _resized(self._image.convert("RGBA"), size)
        if self._mask is not None:
            if picture is self._image:   # never write into the decoder's own picture
                picture = picture.copy()
            lift, gain = PARTIAL_ALPHA   # a clear pixel of the layer stays clear
            alpha = picture.getchannel("A").point(
                [min(255, round(lift + level * gain)) if level else 0 for level in range(256)])
            picture.putalpha(ImageChops.multiply(alpha, self._mask_for(size)))
        self._last = (index, size, picture)
        return picture

    def close(self) -> None:
        self._image.close()


#: The old name of the foil layer.
_Foil = _Layer


def _centre_weight(size: tuple[int, int]) -> Image.Image:
    """A soft ellipse around the place where the pet stands: 255 inside, 0 outside."""
    width, height = size
    # radial_gradient: 256x256, the value is the distance from the centre in
    # units of 128 * sqrt(2) / 255 - i.e. 179 at a distance of 128 px.
    edge = 179
    ellipse = Image.radial_gradient("L").resize(
        (max(1, round(2 * PARTIAL_RADII[0] * width * edge / 128)),
         max(1, round(2 * PARTIAL_RADII[1] * height * edge / 128))), Image.Resampling.BILINEAR)
    weight = Image.new("L", size, 255)
    weight.paste(ellipse, (round(PARTIAL_CENTRE[0] * width - ellipse.width / 2),
                           round(PARTIAL_CENTRE[1] * height - ellipse.height / 2)))
    inner = edge * (1 - PARTIAL_FADE)
    return weight.point([255 if level <= inner else
                         max(0, round(255 * (edge - level) / (edge - inner))) if level < edge else 0
                         for level in range(256)])


def _detail_mask(picture: Image.Image) -> Image.Image:
    """The partial-Holo mask of an RGB ``picture`` at :data:`PARTIAL_WORK` px (L, 0..255).

    Detail light (brighter than the surroundings) near the middle, the
    brightest :data:`PARTIAL_COVERAGE` of it; see the module docstring.
    """
    small = picture.convert("RGB")
    small.thumbnail((PARTIAL_WORK, PARTIAL_WORK), Image.Resampling.LANCZOS)
    grey = small.convert("L")
    light = ImageChops.subtract(grey, grey.filter(ImageFilter.GaussianBlur(PARTIAL_DETAIL_RADIUS)))
    weight = _centre_weight(grey.size)
    light = ImageChops.multiply(light, weight)
    counts = light.histogram()
    total, above, level = sum(counts), 0, 255
    while level > 1 and above + counts[level] <= PARTIAL_COVERAGE * total:
        above += counts[level]
        level -= 1
    high = max(level, PARTIAL_MIN_LEVEL)
    low = high * PARTIAL_RAMP
    mask = light.point([0 if value <= low else 255 if value >= high else round(255 * (value - low) / (high - low))
                        for value in range(256)])
    mask = mask.filter(ImageFilter.GaussianBlur(PARTIAL_SOFTEN))
    covered = sum(mask.histogram()[16:]) / max(1, total)
    if covered < PARTIAL_EMPTY:
        # Nothing stands out (a flat picture): a soft glow in the middle
        # rather than a foil nobody can see.
        mask = weight.point([level * 2 // 5 for level in range(256)])
    return mask


def _partial_mask(source: Image.Image, moving: bool) -> Image.Image:
    """The partial-Holo mask of ``source``: of the picture, or of the average of
    :data:`PARTIAL_SAMPLES` frames spread over a GIF's loop (one mask for every frame)."""
    if not moving:
        return _detail_mask(_on_black(source.convert("RGBA")).convert("RGB"))
    count = int(source.n_frames)
    picks = sorted({number * count // PARTIAL_SAMPLES for number in range(PARTIAL_SAMPLES)})
    average = None
    for taken, index in enumerate(picks):
        source.seek(index)
        picture = _on_black(source.convert("RGBA")).convert("RGB")
        picture.thumbnail((PARTIAL_WORK, PARTIAL_WORK), Image.Resampling.LANCZOS)
        average = picture if average is None else Image.blend(average, picture, 1 / (taken + 1))
    source.seek(0)
    return _detail_mask(average)


def _shown_ms(duration: Any) -> int:
    """How long a viewer shows a GIF frame: browsers play delays of <= 10 ms as 100 ms."""
    duration = int(duration or 0)
    return 100 if duration <= 10 else duration


def _plan(durations: list[int], step: int) -> list[tuple[int, int, int]]:
    """``(source frame, start ms, duration ms)`` of every output frame.

    With ``step`` N only every N-th frame is kept; it stays on the screen for
    its own time plus that of the dropped frames after it, so the loop keeps
    its length.
    """
    plan = []
    start = 0
    for index in range(0, len(durations), step):
        duration = sum(durations[index:index + step])
        plan.append((index, start, duration))
        start += duration
    return plan


def layer_index(start_ms: int, loop_ms: int, layer_loop_ms: int, layer_frames: int) -> int:
    """Which picture of a moving layer lies on an output frame that starts at ``start_ms``.

    ``loop_ms`` is the length of the whole animation (D), ``layer_loop_ms`` the
    layer's own (L: 3200 for the foil, 5040 for an animated frame): the layer
    makes ``k = max(1, round(D / L))`` whole turns per loop, see the module
    docstring.
    """
    if loop_ms <= 0 or layer_frames <= 0:
        return 0
    turns = max(1, round(loop_ms / layer_loop_ms)) if layer_loop_ms > 0 else 1
    return (start_ms * turns * layer_frames // loop_ms) % layer_frames


#: The rule was written for the foil first.
foil_index = layer_index


def _strip(pictures: list[Image.Image]) -> Image.Image:
    """The pictures side by side - one image to make a palette from."""
    width = sum(picture.width for picture in pictures)
    strip = Image.new("RGB", (width, max(picture.height for picture in pictures)))
    left = 0
    for picture in pictures:
        strip.paste(picture, (left, 0))
        left += picture.width
    return strip


def _octree(picture: Image.Image, colours: int) -> list[tuple[int, int, int]]:
    """At most ``colours`` colours that represent ``picture`` (RGB)."""
    if colours < 1:
        return []
    reduced = picture.quantize(colors=colours, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    flat = reduced.getpalette() or []
    used = sorted(index for _, index in reduced.getcolors(256) or [])
    return [tuple(flat[3 * index:3 * index + 3]) for index in used if 3 * index + 2 < len(flat)]


def _palette(samples: list[Image.Image], colours: int, own: list[tuple[int, int, int]] | None,
             region: tuple[int, int, int, int] | None) -> list[tuple[int, int, int]]:
    """The colours of the whole animation, at most ``colours`` of them.

    ``own`` are the colours of an animated source that is drawn unscaled and
    without foil: they are taken over exactly and only the remaining slots are
    spent on ``region``, the part of the card the overlay changes.  An overlay
    colour within :data:`OWN_COLOUR_GUARD` levels of one of the source's is
    left out: Pillow maps colours through a cache of 4-level cells, and such a
    neighbour takes over pixels of the art (a frame covers the whole card, so
    this is not rare).
    """
    if own is not None and len(own) <= colours - OVERLAY_COLOURS_MIN:
        extra = _octree(_strip([sample.crop(region) for sample in samples]), colours - len(own)) if region else []
        extra = [colour for colour in extra
                 if not any(max(abs(colour[0] - red), abs(colour[1] - green), abs(colour[2] - blue))
                            <= OWN_COLOUR_GUARD for red, green, blue in own)]
        return list(dict.fromkeys([*own, *extra]))[:colours]
    return list(dict.fromkeys(_octree(_strip(samples), colours)))


def _quantised(pictures: Iterable[Image.Image], colours: list[tuple[int, int, int]],
               tolerance: int) -> list[Image.Image]:
    """RGB ``pictures`` as palette images that share ``colours``.

    A pixel whose true colour is within ``tolerance`` levels (per channel) of
    the colour already shown at that place keeps the old palette index: static
    areas then stay bit-identical from frame to frame and the encoder writes
    them as "unchanged".  The error never accumulates - it is measured against
    what is on the screen, not against the previous true frame.
    """
    palette = Image.new("P", (1, 1))
    palette.putpalette([channel for colour in colours for channel in colour])
    near = [255] * (tolerance + 1) + [0] * (255 - tolerance)
    frames: list[Any] = []
    shown = None
    for picture in pictures:
        frame = picture.quantize(palette=palette, dither=Image.Dither.NONE)
        if shown is not None and tolerance > 0:
            red, green, blue = ImageChops.difference(picture, shown).split()
            unchanged = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(near)
            frame.paste(frames[-1], mask=unchanged)
        frame.info = {}  # nothing of the source (duration, loop, comment ...) may leak into the file
        shown = frame.convert("RGB")
        frames.append(frame)
    return frames


def _gif(frames: list[Image.Image], durations: list[int], loop: int | None,
         colours: list[tuple[int, int, int]]) -> bytes:
    """The animation as GIF bytes: one global palette, unchanged pixels left out.

    The colour table is padded to the next power of two that leaves at least
    one free slot; its last slot is the "transparent = keep what is shown"
    index Pillow's delta frames need.  ``loop`` None writes no loop extension
    (play once), 0 loops for ever.
    """
    slots = 2
    while slots < len(colours) + 1:
        slots *= 2
    table = bytes(channel for colour in colours for channel in colour).ljust(3 * slots, b"\0")
    options: dict[str, Any] = {} if loop is None else {"loop": loop}
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=durations,
                   disposal=1, optimize=True, interlace=False, palette=table, transparency=slots - 1,
                   **options)
    return buffer.getvalue()


def _survey(source: Image.Image, colours: bool) -> tuple[list[int], list[tuple[int, int, int]] | None]:
    """One pass over an animated source: the duration of every frame and, when
    ``colours`` is set, every colour it uses (None when a palette cannot hold them)."""
    durations: list[int] = []
    seen: set[tuple[int, int, int]] | None = set() if colours else None
    for index in range(source.n_frames):
        source.seek(index)
        durations.append(int(source.info.get("duration") or 0))
        if seen is not None:
            found = _on_black(source.convert("RGBA")).convert("RGB").getcolors(256)
            seen = None if found is None else seen | {colour for _, colour in found}
            if seen is not None and len(seen) > 256:
                seen = None
    return durations, None if seen is None else sorted(seen)


@dataclass
class _Moving:
    """The animated overlays of one card: the foil and / or an animated frame."""

    foil: _Layer | None = None
    frame: _Layer | None = None

    def close(self) -> None:
        for layer in (self.foil, self.frame):
            if layer is not None:
                layer.close()


def _animation(source: Image.Image, moving: bool, layers: _Moving, look: Look, size: tuple[int, int],
               variant: Variant, durations: list[int], loop: int | None,
               own: list[tuple[int, int, int]] | None) -> bytes:
    """One rung of the ladder: the animated card of ``size`` as GIF bytes."""
    plan = _plan(durations, variant.step)
    # The moving layers are placed by the times a viewer really plays (see _shown_ms).
    shown = [_shown_ms(duration) for duration in durations]
    starts = [start for _, start, _ in _plan(shown, variant.step)]
    length = sum(shown)
    top = _top(look, size)
    still = None if moving else _on_black(_resized(source.convert("RGBA"), size))

    def composed(wanted: set[int] | None = None) -> Iterator[Image.Image]:
        """The finished frames in order (only those numbered in ``wanted``), as RGB."""
        for number, (index, _, _) in enumerate(plan):
            if wanted is not None and number not in wanted:
                continue
            if moving:
                # seek() composes the frames in between itself (partial frames,
                # disposal), so after it convert() gives what a viewer shows.
                source.seek(index)
                picture = _on_black(_resized(source.convert("RGBA"), size))
            else:
                picture = still.copy()
            for layer in (layers.foil, layers.frame):   # foil below the frame
                if layer is not None:
                    picture.alpha_composite(
                        layer.frame(layer_index(starts[number], length, layer.loop_ms, layer.count), size))
            if top is not None:
                picture.alpha_composite(top)
            yield picture.convert("RGB")

    picks = {round(pick * len(plan) / PALETTE_SAMPLES) for pick in range(PALETTE_SAMPLES)}
    if layers.frame is not None:
        region = (0, 0, *size)   # an animated frame may change any part of the card
    else:
        region = top.getchannel("A").getbbox() if top is not None else None
    colours = _palette(list(composed(picks)), variant.colours, own, region)
    frames = _quantised(composed(), colours, variant.tolerance)
    return _gif(frames, [duration for _, _, duration in plan], loop, colours)


def _animated(source: Image.Image, look: Look, max_bytes: int) -> tuple[bytes, str]:
    """A card that moves (foil, animated frame and / or an animated source): GIF bytes
    and how they were made."""
    moving = bool(getattr(source, "is_animated", False))
    frame = _frame(look)
    layers = _Moving()
    try:
        if look.foil:
            mask = _partial_mask(source, moving) if look.foil == FOIL_PARTIAL else None
            layers.foil = _Layer(ASSETS / HOLO_FILE, HOLO_FRAME_MS, mask)
        if frame is not None and frame.animated:
            layers.frame = _Layer(ASSETS / frame.file, FRAME_ANIMATION_MS)
        if moving:
            loop = source.info.get("loop")
            durations, own = _survey(source, colours=not look.foil)
        else:
            # A still picture plays the timeline of the animated frame if it
            # has one, else the foil's.
            timeline = layers.frame or layers.foil
            loop, own = 0, None
            durations = [timeline.frame_ms] * timeline.count  # type: ignore[union-attr]
        full = source.size if moving and not look.foil else _fit(source.size, HOLO_SIZE)
        tried = []
        for variant in (LADDER if look.foil else (FAITHFUL, *LADDER)):
            size = full if variant.scale == 1 else _fit(full, max(1, round(max(full) * variant.scale)))
            data = _animation(source, moving, layers, look, size, variant, durations, loop,
                              own if size == source.size else None)
            made = f"{size[0]}x{size[1]}, {variant}"
            if len(data) <= max_bytes:
                return data, made
            tried.append(f"{made}: {len(data)} bytes")
            log.debug("Pet card too big as %s: %d bytes > %d.", made, len(data), max_bytes)
        raise CardTooBig(f"no variant fits into {max_bytes} bytes ({'; '.join(tried[-2:])})")
    finally:
        layers.close()


def _still(source: Image.Image, look: Look, max_bytes: int) -> tuple[bytes, str]:
    """A card that does not move: PNG bytes and how they were made."""
    transparent = source.mode in ("RGBA", "LA", "PA") or "transparency" in source.info
    picture = source.convert("RGBA")
    top = _top(look, picture.size)
    if top is not None:
        picture.alpha_composite(top)
    if not transparent:
        picture = picture.convert("RGB")
    size = 0
    for scale in STILL_SCALES:
        scaled = picture if scale == 1 else _resized(
            picture, _fit(picture.size, max(1, round(max(picture.size) * scale))))
        buffer = io.BytesIO()
        scaled.save(buffer, "PNG", compress_level=PNG_LEVEL)
        size = buffer.tell()
        if size <= max_bytes:
            return buffer.getvalue(), f"{scaled.width}x{scaled.height}"
    raise CardTooBig(f"no variant fits into {max_bytes} bytes (the smallest has {size})")


@dataclass(frozen=True)
class Variant:
    """One rung of the size ladder of the animated cards."""

    scale: float         # of the full canvas
    colours: int         # palette size; one slot more is used for "pixel unchanged"
    tolerance: int       # a pixel within this many levels of the shown one is not redrawn
    step: int = 1        # keep every step-th frame (the dropped ones give it their time)

    def __str__(self) -> str:
        text = f"{self.scale:g}x, {self.colours} colours, tolerance {self.tolerance}"
        return text if self.step == 1 else f"{text}, every {self.step}. frame"


#: Tried in this order, the first result that fits wins.  Each rung is roughly
#: 15-45% smaller than the one before; the last one is ~1-2% of the first.
LADDER: tuple[Variant, ...] = (
    Variant(1.0, 255, 8),
    Variant(1.0, 255, 12),        # 16 already shows as grain in smooth areas
    Variant(0.875, 255, 12),      # 512 -> 448
    Variant(0.75, 255, 12),       # 384
    Variant(0.625, 255, 12),      # 320
    Variant(0.625, 127, 12),
    Variant(0.625, 127, 12, 2),
    Variant(0.5, 127, 12, 2),     # 256
    Variant(0.375, 63, 12, 2),    # 192
    Variant(0.25, 63, 12, 4),     # 128
)
#: First rung of an animation without foil: nothing is traded for size, the
#: source's frames come out as they went in (such files are small anyway).
FAITHFUL = Variant(1.0, 255, 0)
#: The ladder of a still card: only its size can be reduced.
STILL_SCALES: tuple[float, ...] = (1.0, 0.75, 0.5, 0.375, 0.25)


# --------------------------------------------------------------------- public

#: Cards that fit into no variant: remembered so that the whole ladder is not
#: walked again every time such a pet is shown (until the bot restarts).
_too_big: set[str] = set()


def _moves(look: Look) -> bool:
    """Whether ``look`` alone makes the card an animation (a foil or an animated frame)."""
    if look.foil:
        return True
    frame = _frame(look)
    return frame is not None and frame.animated


def render(source: Path, look: Look, cache_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Path:
    """The picture to send for ``source`` with ``look``.  Blocking - see :func:`card`.

    Returns ``source`` itself when ``look.plain``, otherwise the composed card
    inside ``cache_dir`` (created on demand).  The result is cached on disk: a
    second call with the same inputs returns the existing file without
    rendering.  Raises when the card cannot be made (:class:`CardError`,
    ``OSError`` for a missing file, whatever Pillow raises for a broken one).
    """
    if look.plain:
        return source
    picture, folder = Path(source), Path(cache_dir)
    stem = _stem(picture, look, max_bytes)
    hit = _cached(folder, stem)
    if hit is not None:
        return hit
    if Image is None:
        raise CardError("Pillow is not installed")
    if stem in _too_big:
        raise CardTooBig(f"no variant fits into {max_bytes} bytes (tried before)")
    started = time.perf_counter()
    try:
        with Image.open(picture) as opened:
            if _moves(look) or getattr(opened, "is_animated", False):
                (data, made), extension = _animated(opened, look, max_bytes), "gif"
            else:
                (data, made), extension = _still(opened, look, max_bytes), "png"
    except CardTooBig:
        _too_big.add(stem)
        raise
    path = _store(folder, f"{stem}.{extension}", data)
    log.info("Pet card %s rendered in %.2f s: %s, %d bytes.", path.name, time.perf_counter() - started,
             made, len(data))
    _prune(folder, keep=path)
    return path


#: Held by the worker thread that renders.  A cancelled :func:`card` leaves its
#: thread running; this lock keeps the next render from starting beside it.
_rendering = threading.Lock()
#: Per event loop, the queue of cards waiting to be rendered (an asyncio lock
#: belongs to one loop, and pytest makes a new loop for every test).  Waiting
#: here costs no thread, unlike waiting for ``_rendering``.
_queues: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_pillow_warned = False


def _render_alone(source: Path, look: Look, cache_dir: Path, max_bytes: int) -> Path:
    """:func:`render` on the worker thread, never beside another render."""
    with _rendering:
        return render(source, look, cache_dir, max_bytes=max_bytes)


async def card(source: Path, look: Look, cache_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Path:
    """:func:`render` for the bot: off the event loop, one render at a time, never failing.

    A card that is already in the cache is returned at once (a few ``stat``
    calls), without waiting behind a render in progress.  Otherwise the card
    is rendered in a worker thread, at most one at a time.  Whatever goes
    wrong - an overlay is missing, the source is broken, Pillow is not
    installed, the disk is full, the result is too big - is logged as a
    warning and ``source`` is returned: the pet is shown plain rather than not
    at all.  Only cancellation gets through.
    """
    global _pillow_warned
    if look.plain:
        return source
    try:
        hit = _cached(Path(cache_dir), _stem(Path(source), look, max_bytes))
        if hit is not None:
            return hit
        if Image is None:
            if not _pillow_warned:
                _pillow_warned = True
                log.warning("Pillow is not installed: pets are shown without frames, seals and Holo "
                            "(pip install -r requirements.txt).")
            return source
        loop = asyncio.get_running_loop()
        try:
            queue = _queues.get(loop)
            if queue is None:
                queue = _queues[loop] = asyncio.Lock()
        except TypeError:  # a loop that cannot be weakly referenced: _rendering alone keeps order
            queue = asyncio.Lock()
        async with queue:
            return await asyncio.to_thread(_render_alone, source, look, cache_dir, max_bytes)
    except Exception as exc:  # noqa: BLE001 - CancelledError is a BaseException and passes
        log.warning("Pet card of %s (%s) could not be made, the plain picture is shown: %s: %s",
                    source, look, type(exc).__name__, exc)
        return source
