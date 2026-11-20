"""Pet "cards": the picture of a pet with its frame, seal and Holo foil drawn on it.

A pure image module - no Discord, no database.  ``features/pets.py`` asks
:func:`card` for the picture to send and gets back either the pet's own
illustration (nothing to draw, or drawing failed) or a composed file in the
cache folder.

What is drawn (the owner's spec)
--------------------------------
Layers, bottom to top: **illustration -> Holo foil -> frame -> seal**.  The
frame and the seal lie above the foil and are not tinted by it.

* The *frame* follows the pet's class: Shiny = silver fittings
  (``frame-shiny.png``), Ultra Shiny = gold frame (``frame-ultra.png``), an
  ordinary pet has none and a mythic one never gets one - it has its own GIF
  (:func:`look_of`).
* The *seal* (tier 1-3, ``seal-<tier>.png``) sits in the bottom-right corner.
* *Holo* is ``holo.apng``: an independent RGBA layer, 64 frames of 50 ms.  It
  is scaled to the card and alpha-composited over the WHOLE illustration.  The
  owner's previews dim the foil on the cat's silhouette with a hand-made mask;
  such a mask exists for that one picture only, so none is used here and the
  pet itself is tinted by the foil as much as its background.

Overlays are 1024x1024 and are stretched to the size of the picture they go
on - also when that picture is not square (the art of the game is square, so
this is only the sensible thing to do with a foreign file).

What comes out
--------------
* Still picture, no Holo: a **PNG** of the source's size (alpha kept when the
  source has it).  zlib level 6: a 1024x1024 card is encoded in ~0.1 s; level 9
  / ``optimize`` would cost 0.4-0.5 s for a file 3% smaller.
* Holo on a still picture: an **animated GIF**, the 64 foil frames of 50 ms in
  an endless loop, 512 px (:data:`HOLO_SIZE`; a smaller source is not
  enlarged).
* Animated source (the mythic GIFs) with a seal: every source frame with its
  own duration and the source's loop setting, at the source's size, the seal
  drawn on each frame.
* Animated source with Holo: the frames and their durations are the SOURCE's
  (its tempo is kept).  The foil frame for an output frame that starts at time
  ``t`` is picked by time so that the foil is seamless where the GIF loops:
  with ``D`` = length of the source loop and ``k = max(1, round(D / 3200))``::

      foil_index = floor((t * k / D) * 64) % 64

  The foil therefore makes exactly ``k`` turns per source loop and one turn
  takes ``D / k`` instead of exactly 3200 ms - 2520 ms for the 5040 ms loop of
  ``Void/0.gif`` and 3520 ms for the 7040 ms loops of ``Void/10`` / ``Void/100``.
  (The same rule gives foil frame ``i`` for output frame ``i`` of a still
  picture.)

Transparent areas of a picture that becomes a GIF are flattened onto black
(GIF knows no partial transparency, and the foil covers the whole card anyway).

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
* An animated source without Holo keeps **its own colours**: its palette is
  taken over as it is (the mythic GIFs use ~100 colours) and only the slots
  that are left are spent on the seal, so the art outside the seal stays
  within 5 levels of the original (46 dB instead of 40 dB).

File size
---------
Discord refuses uploads above the guild's limit, so every result is at most
``max_bytes``.  GIFs go down a fixed ladder (:data:`LADDER`: more tolerance,
smaller canvas, fewer colours, every second frame dropped with its duration
added to the frame before it) and the first variant that fits is taken; the
log says which.  Still cards are scaled down the same way
(:data:`STILL_SCALES`).  When nothing fits :class:`CardTooBig` is raised.
Measured with the art of the game: a still card 1.3-1.5 MB, Holo on a still
picture 4.8-6.4 MB (first rung), a mythic GIF with a seal 1.1-1.7 MB (about the
size of its source), a mythic GIF with Holo 5.9-7.9 MB (second rung for the two
larger ones under the default 8 MiB).

Cache
-----
Rendering costs 0.15 s (PNG), ~1.1 s (Holo), 1-2 s (mythic with a seal) and
2-5 s (mythic with Holo; every rung of the ladder that is tried costs a whole
encoding), so results are kept on disk:
``card-<Type>-<n>-<what>-<digest>.<png|gif>``, a name that is
safe as a Discord attachment name.  The digest covers everything the picture
depends on - the source (path, size, mtime), the look, ``max_bytes``, the
overlay files used (size, mtime) and :data:`RENDER_VERSION` - so a changed
illustration or overlay simply gets a new file and the old one ages out.
Files are written under a temporary name and renamed, a cache hit touches the
file, and after every new card the least recently used ones are removed while
the folder holds more than :data:`CACHE_MAX_BYTES`.

Memory and time
---------------
The foil file is 20 MB, but decoding all 64 frames takes ~0.15 s, so nothing
of it is kept between renders: frames are decoded one at a time (never 64
RGBA frames at once), and so are the frames of an animated source.  What a
render holds is the finished palette frames (17 MB for a Holo card, 33 MB for
the longest mythic GIF), the copies Pillow's GIF writer makes of them and the
encoded file: the process grows by ~75 MB (Holo card) to ~95 MB (longest
mythic GIF with Holo) while it renders, by ~25 MB for a still card.  One
render runs at a time (:func:`card`).

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
    from PIL import Image, ImageChops
except Exception:  # noqa: BLE001 - also a broken installation (missing DLL ...)
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Folder of the overlays; looked up at every render (tests point it elsewhere).
ASSETS = ASSETS_DIR / "petcards"
#: Names of the seal tiers, as shown to the players.
SEAL_NAMES: dict[int, str] = {1: "Tropiciela", 2: "Łowcy", 3: "Arcyłowcy"}
#: Largest file :func:`render` returns unless the caller knows the guild's limit.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: Part of every cache key: bump it when the pictures this module draws change.
RENDER_VERSION = 1
#: The cache folder is pruned (least recently used first) above this size.
CACHE_MAX_BYTES = 300 * 1024 * 1024
#: An unfinished file older than this was left behind by a crash and is removed.
TEMP_MAX_AGE_S = 3600.0

FRAME_FILES: dict[str, str] = {"shiny": "frame-shiny.png", "ultra": "frame-ultra.png"}
SEAL_FILE = "seal-{tier}.png"
HOLO_FILE = "holo.apng"
#: Duration of a foil frame when the file does not say (the owner's foil: 64 x 50 ms).
HOLO_FRAME_MS = 50
#: Longer side of a Holo card, like the owner's previews; smaller sources are not enlarged.
HOLO_SIZE = 512

#: zlib level of the still cards (see the module docstring for the measurements).
PNG_LEVEL = 6
#: How many composed frames, spread over the loop, the GIF palette is made from.
PALETTE_SAMPLES = 16
#: An animated source keeps its own colours when at least this many palette
#: slots are left for the overlay drawn on it.
OVERLAY_COLOURS_MIN = 48

_TEMP_SUFFIX = ".tmp"
_CARD_NAME = re.compile(r"card-[A-Za-z0-9_-]+-[0-9a-f]{12}\.(?:png|gif)")
_TEMP_NAME = re.compile(r"card-[A-Za-z0-9_.-]+" + re.escape(_TEMP_SUFFIX))


class CardError(Exception):
    """A card could not be made; :func:`card` shows the plain picture instead."""


class CardTooBig(CardError):
    """Not even the smallest variant fits into ``max_bytes``."""


@dataclass(frozen=True)
class Look:
    """What is drawn over a pet's picture."""

    frame: str = ""      # "" (no frame), "shiny" or "ultra"
    seal: int = 0        # 0..3
    holo: bool = False

    @property
    def plain(self) -> bool:
        """True when nothing is drawn over the source picture."""
        return not (self.frame or self.seal or self.holo)


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
#: First rung of an animation without Holo: nothing is traded for size, the
#: source's frames come out as they went in (such files are small anyway).
FAITHFUL = Variant(1.0, 255, 0)
#: The ladder of a still card: only its size can be reduced.
STILL_SCALES: tuple[float, ...] = (1.0, 0.75, 0.5, 0.375, 0.25)


# ------------------------------------------------------------------- the look

def _field(pet: Any, key: str) -> Any:
    """``pet[key]``, None when the row has no such column (or there is no row)."""
    try:
        return pet[key]
    except (KeyError, IndexError, TypeError):
        return None


def pillow_available() -> bool:
    """Whether Pillow could be imported, i.e. whether cards can be composed on this machine."""
    return Image is not None


def look_of(pet: Mapping) -> Look:
    """The look of a ``PETS`` row (asyncpg Record or dict, lower-case keys).

    Reads ``mythic``, ``ultra_shiny``, ``shiny``, ``seal`` and ``holo``.  A
    missing key or NULL means not shiny / no seal / no Holo (rows from before
    the cosmetics have neither column).  The seal arrives as Decimal or int and
    is clamped to 0..3.  Priority MYTHIC > ULTRA_SHINY > SHINY > ordinary, and
    a mythic pet never gets a frame: it has its own GIF.
    """
    if _field(pet, "mythic"):
        frame = ""
    elif _field(pet, "ultra_shiny"):
        frame = "ultra"
    elif _field(pet, "shiny"):
        frame = "shiny"
    else:
        frame = ""
    try:
        seal = int(_field(pet, "seal"))
    except (TypeError, ValueError, ArithmeticError):  # NULL, text, Decimal NaN / Infinity
        seal = 0
    return Look(frame=frame, seal=min(max(seal, 0), max(SEAL_NAMES)), holo=bool(_field(pet, "holo")))


def _overlays(look: Look) -> list[Path]:
    """The overlay files this look needs, in drawing order (the foil first)."""
    if look.frame and look.frame not in FRAME_FILES:
        raise CardError(f"unknown frame {look.frame!r}")
    if look.seal and look.seal not in SEAL_NAMES:
        raise CardError(f"unknown seal tier {look.seal!r}")
    files = [HOLO_FILE] if look.holo else []
    if look.frame:
        files.append(FRAME_FILES[look.frame])
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
        look.frame, look.seal, int(look.holo), int(max_bytes))
    for overlay in _overlays(look):
        stat = overlay.stat()
        add(overlay.name, stat.st_size, stat.st_mtime_ns)
    what = [look.frame, f"seal{look.seal}" if look.seal else "", "holo" if look.holo else ""]
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
    """Frame and seal as one RGBA layer of ``size`` (None when the look has neither)."""
    layer = None
    for path in _overlays(look):
        if path.name == HOLO_FILE:
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


class _Foil:
    """The Holo foil, decoded one frame at a time (see "Memory and time" above)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._image = Image.open(path)
        self._at = 0
        self._last: tuple[int, tuple[int, int], Any] | None = None
        self.count = int(getattr(self._image, "n_frames", 1))
        self.frame_ms = int(self._image.info.get("duration") or HOLO_FRAME_MS)
        self.loop_ms = self.count * self.frame_ms

    def frame(self, index: int, size: tuple[int, int]) -> Image.Image:
        """Foil frame ``index`` as RGBA of ``size``."""
        if self._last is not None and self._last[:2] == (index, size):
            return self._last[2]
        if index < self._at:
            # Pillow cannot rewind an APNG from the middle ("image file is
            # truncated"); opening it again costs nothing.
            self._image.close()
            self._image = Image.open(self._path)
        self._image.seek(index)
        self._at = index
        picture = _resized(self._image.convert("RGBA"), size)
        self._last = (index, size, picture)
        return picture

    def close(self) -> None:
        self._image.close()


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


def foil_index(start_ms: int, loop_ms: int, foil_loop_ms: int, foil_frames: int) -> int:
    """Which foil frame lies on an output frame that starts at ``start_ms``.

    ``loop_ms`` is the length of the whole animation (D), ``foil_loop_ms`` the
    foil's own (3200): the foil makes ``k = max(1, round(D / 3200))`` whole
    turns per loop, see the module docstring.
    """
    if loop_ms <= 0 or foil_frames <= 0:
        return 0
    turns = max(1, round(loop_ms / foil_loop_ms)) if foil_loop_ms > 0 else 1
    return (start_ms * turns * foil_frames // loop_ms) % foil_frames


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
    spent on ``region``, the part of the card the overlay changes.
    """
    if own is not None and len(own) <= colours - OVERLAY_COLOURS_MIN:
        extra = _octree(_strip([sample.crop(region) for sample in samples]), colours - len(own)) if region else []
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


def _animation(source: Image.Image, moving: bool, foil: _Foil | None, look: Look, size: tuple[int, int],
               variant: Variant, durations: list[int], loop: int | None,
               own: list[tuple[int, int, int]] | None) -> bytes:
    """One rung of the ladder: the animated card of ``size`` as GIF bytes."""
    plan = _plan(durations, variant.step)
    # The foil is placed by the times a viewer really plays (see _shown_ms).
    shown = [_shown_ms(duration) for duration in durations]
    starts = [start for _, start, _ in _plan(shown, variant.step)]
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
            if foil is not None:
                picture.alpha_composite(
                    foil.frame(foil_index(starts[number], sum(shown), foil.loop_ms, foil.count), size))
            if top is not None:
                picture.alpha_composite(top)
            yield picture.convert("RGB")

    picks = {round(pick * len(plan) / PALETTE_SAMPLES) for pick in range(PALETTE_SAMPLES)}
    region = top.getchannel("A").getbbox() if top is not None else None
    colours = _palette(list(composed(picks)), variant.colours, own, region)
    frames = _quantised(composed(), colours, variant.tolerance)
    return _gif(frames, [duration for _, _, duration in plan], loop, colours)


def _animated(source: Image.Image, look: Look, max_bytes: int) -> tuple[bytes, str]:
    """A card that moves (Holo and / or an animated source): GIF bytes and how they were made."""
    moving = bool(getattr(source, "is_animated", False))
    foil = _Foil(ASSETS / HOLO_FILE) if look.holo else None
    try:
        if moving:
            loop = source.info.get("loop")
            durations, own = _survey(source, colours=not look.holo)
        else:
            loop, own = 0, None
            durations = [foil.frame_ms] * foil.count  # type: ignore[union-attr]
        full = _fit(source.size, HOLO_SIZE) if look.holo else source.size
        tried = []
        for variant in (LADDER if look.holo else (FAITHFUL, *LADDER)):
            size = full if variant.scale == 1 else _fit(full, max(1, round(max(full) * variant.scale)))
            data = _animation(source, moving, foil, look, size, variant, durations, loop,
                              own if size == source.size else None)
            made = f"{size[0]}x{size[1]}, {variant}"
            if len(data) <= max_bytes:
                return data, made
            tried.append(f"{made}: {len(data)} bytes")
            log.debug("Pet card too big as %s: %d bytes > %d.", made, len(data), max_bytes)
        raise CardTooBig(f"no variant fits into {max_bytes} bytes ({'; '.join(tried[-2:])})")
    finally:
        if foil is not None:
            foil.close()


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


# --------------------------------------------------------------------- public

#: Cards that fit into no variant: remembered so that the whole ladder is not
#: walked again every time such a pet is shown (until the bot restarts).
_too_big: set[str] = set()


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
            if look.holo or getattr(opened, "is_animated", False):
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
