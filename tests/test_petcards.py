"""Pet cards (features/petcards.py): frame, seal and Holo foil drawn on a pet's picture.

No database and no Discord here.  Sources are small generated pictures; the
frame and seal overlays are the real ones (copied into a temporary assets
folder), the 20 MB foil is replaced by a tiny synthetic APNG whose frames say
their own number, and so are the animated frames (their pictures write their
number in binary into a strip at the top) - except in the few tests that
decode the real files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import threading
from decimal import Decimal
from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")
from PIL import ImageChops, ImageDraw, ImageFilter  # noqa: E402

from altermmo.features import petcards  # noqa: E402
from altermmo.features.petcards import FOIL_FULL, FOIL_NONE, FOIL_PARTIAL, CardTooBig, Look  # noqa: E402

REAL_ASSETS = petcards.ASSETS
ATTACHMENT_NAME = re.compile(r"[A-Za-z0-9_-]+\.(png|gif)")


# -------------------------------------------------------------------- helpers

def foil_colour(number: int) -> tuple[int, int, int]:
    """The colour of synthetic foil frame ``number``: neighbours differ by 60 levels."""
    return 20 + 60 * (number % 4), 20 + 60 * (number // 4 % 4), 20 + 60 * (number // 16)


def foil_number(colour: tuple[int, ...]) -> int:
    red, green, blue = (round((channel - 20) / 60) for channel in colour[:3])
    return red + 4 * green + 16 * blue


def make_foil(path: Path, frames: int = 64, size: int = 24) -> None:
    """A foil whose left half is opaque (frame number as colour) and right half clear."""
    pictures = []
    for number in range(frames):
        picture = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        picture.paste((*foil_colour(number), 255), (0, 0, size // 2, size))
        pictures.append(picture)
    pictures[0].save(path, format="PNG", save_all=True, append_images=pictures[1:], duration=50, loop=0,
                     disposal=0, blend=0)


BITS = 7   # 84 pictures need 7 bits


def make_animated_frame(path: Path, size: int, frames: int = 84, duration: int = 60) -> None:
    """An "animated frame" whose picture ``n`` writes ``n`` in binary into a strip at the top
    (white cell = 1, black = 0, opaque); everything else is clear."""
    cell = size // 8
    pictures = []
    for number in range(frames):
        picture = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        for bit in range(BITS):
            colour = (255, 255, 255, 255) if number >> bit & 1 else (0, 0, 0, 255)
            picture.paste(colour, (bit * cell, 0, (bit + 1) * cell, cell))
        pictures.append(picture)
    path.parent.mkdir(parents=True, exist_ok=True)
    pictures[0].save(path, format="PNG", save_all=True, append_images=pictures[1:], duration=duration, loop=0,
                     disposal=0, blend=0)


def frame_number(picture, size: int) -> int:
    """Which picture of a synthetic animated frame is shown on ``picture``."""
    cell = size // 8
    rgb = picture.convert("RGB")
    return sum(1 << bit for bit in range(BITS)
               if sum(rgb.getpixel((bit * cell + cell // 2, cell // 2))) > 384)


def make_png(path: Path, size: tuple[int, int] = (96, 96), mode: str = "RGB") -> Path:
    """A picture with gradients in it (so that scaling and palettes have something to do)."""
    ramp = Image.linear_gradient("L").resize(size)
    picture = Image.merge("RGB", (ramp, Image.radial_gradient("L").resize(size), ramp.rotate(180)))
    if mode == "RGBA":
        picture.putalpha(Image.radial_gradient("L").resize(size).point(lambda level: 0 if level > 150 else 255))
    elif mode == "P":
        picture = picture.quantize(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    picture.save(path)
    return path


def detail_picture(size: int = 96) -> Image.Image:
    """A dark picture with "detail light": a bright ring in the middle and a bright
    square in a corner (far from where a pet stands)."""
    picture = Image.new("RGB", (size, size), (30, 30, 45))
    draw = ImageDraw.Draw(picture)
    centre, radius = size // 2, size * 3 // 16
    draw.ellipse((centre - radius, centre + 4 - radius, centre + radius, centre + 4 + radius),
                 outline=(235, 235, 245), width=2)
    draw.rectangle((2, 2, 10, 10), fill=(250, 250, 250))
    return picture


def make_detail_png(path: Path, size: int = 96) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    detail_picture(size).save(path)
    return path


GIF_DURATIONS = [40, 50, 1200, 700, 900, 1100, 1050]   # 5040 ms, like assets/pets/Void/0.gif


def gif_frames(count: int, size: int = 48) -> list:
    """Frames of few, clearly different colours: a block wandering over a two-coloured ground."""
    frames = []
    for number in range(count):
        frame = Image.new("RGB", (size, size), (40, 10, 90))
        frame.paste((10, 120, 60), (0, size // 2, size, size))
        left = 2 + number * 4
        frame.paste((230, 200, 30 + 25 * number), (left, 6, left + 10, 16))
        frames.append(frame)
    return frames


def make_gif(path: Path, durations: list[int] = GIF_DURATIONS, loop: int | None = 0) -> list:
    """An optimised GIF (partial frames, "unchanged" pixels) and the frames it must show."""
    frames = gif_frames(len(durations))
    options = {} if loop is None else {"loop": loop}
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, **options)
    return frames


def read_frames(path: Path) -> tuple[list, list[int], dict]:
    """Every frame of an animation as a viewer shows it, the durations and the file's info."""
    with Image.open(path) as opened:
        info = dict(opened.info)
        frames, durations = [], []
        for number in range(getattr(opened, "n_frames", 1)):
            opened.seek(number)
            frames.append(opened.convert("RGB"))
            durations.append(opened.info.get("duration"))
    return frames, durations, info


def differs(first, second, box=None) -> bool:
    """True when the two pictures differ somewhere (inside ``box``)."""
    difference = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
    return (difference.crop(box) if box else difference).getbbox() is not None


def changed(first, second, threshold: int = 24):
    """L picture: 255 where the two pictures differ by more than ``threshold`` levels."""
    red, green, blue = ImageChops.difference(first.convert("RGB"), second.convert("RGB")).split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
        lambda level: 255 if level > threshold else 0)


def share(mask) -> float:
    """Share of the pixels of an L picture that are set."""
    return mask.histogram()[255] / (mask.width * mask.height)


def overlay(name: str, size: tuple[int, int], folder: Path = REAL_ASSETS):
    with Image.open(folder / name) as opened:
        return opened.convert("RGBA").resize(size, Image.Resampling.LANCZOS)


@pytest.fixture(scope="module")
def tiny_foil(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("foil") / "holo.apng"
    make_foil(path)
    return path


@pytest.fixture(scope="module")
def tiny_frames(tmp_path_factory) -> Path:
    """Synthetic animated frames: S1-A for 96 px still cards, R5-A for 48 px GIFs."""
    folder = tmp_path_factory.mktemp("frames")
    make_animated_frame(folder / "S1-A.apng", 96)
    make_animated_frame(folder / "R5-A.apng", 48)
    return folder


@pytest.fixture
def assets(tmp_path, monkeypatch, tiny_foil, tiny_frames) -> Path:
    """A private assets folder: the real frames and seals, the synthetic foil and animated frames."""
    folder = tmp_path / "assets"
    (folder / "frames").mkdir(parents=True)
    for name in ("frame-shiny.png", "frame-ultra.png", "seal-1.png", "seal-2.png", "seal-3.png",
                 "frames/S1.png", "frames/R5.png"):
        shutil.copy2(REAL_ASSETS / name, folder / name)
    shutil.copy2(tiny_foil, folder / "holo.apng")
    for name in ("S1-A.apng", "R5-A.apng"):
        shutil.copy2(tiny_frames / name, folder / "frames" / name)
    monkeypatch.setattr(petcards, "ASSETS", folder)
    # The frames of the synthetic foil are 64 unrelated colours (the real foil is
    # one rainbow that moves): the palette has to see every one of them.
    monkeypatch.setattr(petcards, "PALETTE_SAMPLES", 64)
    return folder


@pytest.fixture
def cache(tmp_path) -> Path:
    return tmp_path / "cache" / "petcards"


# ------------------------------------------------------------------ catalogue

SPEC_TABLE = {   # spec 1, the owner's dla-bota.md / pule.json: id -> (tier, name, percent, file)
    "S0": ("S", "Srebrne okucia", 52, "frame-shiny.png"),
    "S1": ("S", "Gwiazda Szronu", 24, "frames/S1.png"), "S1-A": ("S", "Gwiazda Szronu", 1.65, "frames/S1-A.apng"),
    "R1": ("S", "Księżycowa stal", 12, "frames/R1.png"), "R1-A": ("S", "Księżycowa stal", 0.6, "frames/R1-A.apng"),
    "R2": ("S", "Miedziane ciernie", 6, "frames/R2.png"), "R2-A": ("S", "Miedziane ciernie", 0.45, "frames/R2-A.apng"),
    "R7": ("S", "Przeszywana skóra", 3, "frames/R7.png"), "R7-A": ("S", "Przeszywana skóra", 0.3, "frames/R7-A.apng"),
    "U0": ("U", "Złoty relikwiarz", 52, "frame-ultra.png"),
    "U1": ("U", "Rubinowa Korona", 24, "frames/U1.png"), "U1-A": ("U", "Rubinowa Korona", 1.65, "frames/U1-A.apng"),
    "R3": ("U", "Złote wrota", 12, "frames/R3.png"), "R3-A": ("U", "Złote wrota", 0.6, "frames/R3-A.apng"),
    "R4": ("U", "Rozbite zwierciadło", 6, "frames/R4.png"),
    "R4-A": ("U", "Rozbite zwierciadło", 0.45, "frames/R4-A.apng"),
    "R8": ("U", "Bursztynowy skarabeusz", 3, "frames/R8.png"),
    "R8-A": ("U", "Bursztynowy skarabeusz", 0.3, "frames/R8-A.apng"),
    "M0": ("M", "Oryginalna ilustracja", 70, ""),
    "R5": ("M", "Astrolabium", 15, "frames/R5.png"), "R5-A": ("M", "Astrolabium", 1.5, "frames/R5-A.apng"),
    "R6": ("M", "Kręgosłup Pustki", 9, "frames/R6.png"), "R6-A": ("M", "Kręgosłup Pustki", 0.9, "frames/R6-A.apng"),
    "R9": ("M", "Oko Otchłani", 3, "frames/R9.png"), "R9-A": ("M", "Oko Otchłani", 0.6, "frames/R9-A.apng"),
}


def test_catalogue_is_the_spec_table():
    assert set(petcards.FRAMES) == set(SPEC_TABLE)
    for ident, (tier, name, percent, file) in SPEC_TABLE.items():
        frame = petcards.FRAMES[ident]
        assert (frame.id, frame.tier, frame.name, frame.percent, frame.file) == (ident, tier, name, percent, file)
        assert frame.animated == ident.endswith("-A")
        assert frame in petcards.POOLS[tier]
    assert petcards.BASE_FRAME == {"S": "S0", "U": "U0", "M": "M0"}
    assert set(petcards.POOLS) == {"S", "U", "M"}
    for tier, pool in petcards.POOLS.items():
        assert math.isclose(sum(frame.percent for frame in pool), 100.0), tier
        assert pool[0].id == petcards.BASE_FRAME[tier] and pool[0].base and not any(f.base for f in pool[1:])
        assert len({frame.id for frame in pool}) == len(pool)
    assert (petcards.FOIL_NONE, petcards.FOIL_PARTIAL, petcards.FOIL_FULL) == (0, 1, 2)


def test_catalogue_agrees_with_the_owners_pools():
    """``source/pule.json`` of the owner's package: same results, order, names and percents."""
    owner = json.loads((REAL_ASSETS / "source" / "pule.json").read_text(encoding="utf-8"))
    names = {frame["id"]: frame["name"] for frame in owner["frames"]}
    for tier, results in owner["framePools"].items():
        pool = petcards.POOLS[tier]
        assert [result["id"] for result in results] == [frame.id for frame in pool]
        for result, frame in zip(results, pool):
            assert frame.percent == result["percent"] and frame.name == names[result["frame"]]
            assert frame.animated == (result["animation"] is not None)
    assert owner["rendering"]["order"] == ["pet", "foil", "frame", "seal"]


def test_every_frame_file_exists_in_its_documented_format():
    for frame in petcards.FRAMES.values():
        if not frame.file:
            assert frame.id == "M0"
            continue
        path = REAL_ASSETS / frame.file
        with Image.open(path) as opened:
            assert opened.format == "PNG" and opened.mode == "RGBA", frame.id
            if frame.animated:
                # A whole transparent frame, 512 px, 84 pictures of 60 ms in an endless loop.
                assert (opened.size, opened.n_frames, opened.info.get("loop")) == ((512, 512), 84, 0), frame.id
                assert opened.info.get("duration") == 60, frame.id
                assert opened.getchannel("A").getextrema()[0] == 0, "transparent where the pet shows"
            else:
                assert opened.size == (1024, 1024) and getattr(opened, "n_frames", 1) == 1, frame.id
                assert opened.getchannel("A").getextrema() == (0, 255), frame.id


def test_an_animated_frame_plays_60_ms_per_picture():
    with Image.open(REAL_ASSETS / "frames" / "R1-A.apng") as opened:
        durations = []
        for number in range(opened.n_frames):
            opened.seek(number)
            durations.append(opened.info.get("duration"))
    assert durations == [60] * 84


# ---------------------------------------------------------------------- the look

def test_tier_follows_the_class_priority():
    assert petcards.tier_of({"mythic": False, "ultra_shiny": False, "shiny": False}) == ""
    assert petcards.tier_of({"shiny": True}) == "S"
    assert petcards.tier_of({"ultra_shiny": True, "shiny": True}) == "U"
    assert petcards.tier_of({"mythic": True, "ultra_shiny": True, "shiny": True}) == "M"
    assert petcards.tier_of({}) == "" and petcards.tier_of(None) == ""


def test_look_of_reads_frame_seal_and_foil():
    assert petcards.look_of({"mythic": False, "ultra_shiny": False, "shiny": False}) == Look()
    assert petcards.look_of({"shiny": True}) == Look(frame="S0")                 # the base of the class
    assert petcards.look_of({"ultra_shiny": True, "shiny": True}) == Look(frame="U0")
    assert petcards.look_of({"shiny": True, "frame": "S1-A", "seal": 2, "holo_partial": True}) == Look(
        frame="S1-A", seal=2, foil=FOIL_PARTIAL)
    assert petcards.look_of({"ultra_shiny": True, "frame": "R8", "holo": True}) == Look(frame="R8", foil=FOIL_FULL)
    # A mythic pet: its own GIF (M0 draws nothing) or one of its own frames.
    assert petcards.look_of({"mythic": True, "ultra_shiny": True, "shiny": True}) == Look()
    assert petcards.look_of({"mythic": True, "frame": "M0"}) == Look()
    mythic = petcards.look_of({"mythic": True, "frame": "R9-A", "seal": 2, "holo": True})
    assert mythic == Look(frame="R9-A", seal=2, foil=FOIL_FULL) and not mythic.plain
    # Full Holo wins over partial (both set should not happen).
    assert petcards.look_of({"holo": True, "holo_partial": True}).foil == FOIL_FULL
    assert petcards.look_of({"holo": False, "holo_partial": True}).foil == FOIL_PARTIAL


def test_look_of_tolerates_old_rows_and_garbage():
    assert petcards.look_of({}) == Look() and Look().plain
    assert petcards.look_of(None) == Look()                                      # no row at all
    assert petcards.look_of({"shiny": None, "ultra_shiny": None, "mythic": None, "seal": None, "holo": None,
                             "holo_partial": None, "frame": None}) == Look()
    assert petcards.look_of({"shiny": True}) == Look(frame="S0")                 # no frame / foil columns yet
    assert petcards.look_of({"seal": Decimal("2"), "holo": True}) == Look(seal=2, foil=FOIL_FULL)
    assert petcards.look_of({"seal": Decimal("3.0")}).seal == 3
    assert petcards.look_of({"seal": 7}).seal == 3 and petcards.look_of({"seal": -1}).seal == 0
    assert petcards.look_of({"seal": Decimal("NaN")}).seal == 0
    assert petcards.look_of({"seal": "what"}).seal == 0

    class Liar:
        def __bool__(self):
            raise ValueError("no")

    weird = {"shiny": True, "frame": ["S1"], "holo": Liar(), "holo_partial": Liar(), "mythic": Liar()}
    assert petcards.look_of(weird) == Look(frame="S0")


@pytest.mark.parametrize("stored, wanted", [
    ("", "S0"), (None, "S0"), ("S1", "S1"), ("S1-A", "S1-A"), (" r7-a ", "R7-A"),
    ("U1", "S0"), ("R5", "S0"), ("M0", "S0"),          # results of another class
    ("S9", "S0"), ("gold", "S0"), (5, "S0"), (b"S1", "S0"),
])
def test_frame_of_falls_back_to_the_base_of_the_class(stored, wanted):
    assert petcards.frame_of({"shiny": True, "frame": stored}) is petcards.FRAMES[wanted]


def test_frame_of_an_ordinary_or_mythic_pet():
    assert petcards.frame_of({"frame": "S1"}) is None                            # an ordinary pet: never a frame
    assert petcards.look_of({"frame": "S1", "seal": 1}) == Look(seal=1)
    assert petcards.frame_of({"mythic": True}) is petcards.FRAMES["M0"]
    assert petcards.frame_of({"mythic": True, "shiny": True, "frame": "S1"}) is petcards.FRAMES["M0"]
    assert petcards.frame_of({"mythic": True, "frame": "R6"}) is petcards.FRAMES["R6"]
    assert petcards.frame_of({"ultra_shiny": True, "frame": "R6-A"}) is petcards.FRAMES["U0"]


def test_frame_label():
    assert petcards.frame_label({"shiny": True}) == ""                           # the base says nothing
    assert petcards.frame_label({"ultra_shiny": True, "frame": "U0"}) == ""
    assert petcards.frame_label({"mythic": True}) == ""
    assert petcards.frame_label({"frame": "S1"}) == ""                           # ordinary pet
    assert petcards.frame_label({"shiny": True, "frame": "S1"}) == "Gwiazda Szronu"
    assert petcards.frame_label({"shiny": True, "frame": "S1-A"}) == "Gwiazda Szronu (animowana)"
    assert petcards.frame_label({"ultra_shiny": True, "frame": "R8-A"}) == "Bursztynowy skarabeusz (animowana)"
    assert petcards.frame_label({"mythic": True, "frame": "R9"}) == "Oko Otchłani"
    assert petcards.frame_label({"shiny": True, "frame": "R9"}) == ""            # not of its class: the base
    assert petcards.FRAMES["R6-A"].label == "Kręgosłup Pustki (animowana)"


def test_base_look_shows_the_class_and_nothing_else():
    rich = {"ultra_shiny": True, "shiny": True, "frame": "R4-A", "seal": 3, "holo": True, "holo_partial": True}
    assert petcards.base_look(rich) == Look(frame="U0")
    assert petcards.base_look({"shiny": True, "frame": "S1", "seal": 1}) == Look(frame="S0")
    assert petcards.base_look({"mythic": True, "frame": "R9-A", "seal": 2, "holo": True}) == Look()
    assert petcards.base_look({"seal": 2, "holo": True}) == Look() and petcards.base_look(None) == Look()


def test_look_normalises_and_knows_when_it_is_plain():
    assert Look().plain and Look(frame="M0").plain and Look(frame="M0") == Look()
    assert Look(frame=None) == Look()
    assert not Look(frame="S0").plain and not Look(seal=1).plain and not Look(foil=FOIL_PARTIAL).plain
    assert Look(foil=True) == Look(foil=FOIL_FULL) and Look(foil=False) == Look(foil=FOIL_NONE)
    assert Look(frame="S1-A").frame == "S1-A"
    assert petcards.SEAL_NAMES == {1: "Tropiciela", 2: "Łowcy", 3: "Arcyłowcy"}
    assert petcards.DEFAULT_MAX_BYTES == 8 * 1024 * 1024


# ------------------------------------------------------------------ still cards

def test_plain_look_returns_the_source_and_creates_nothing(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    before = source.read_bytes()
    assert petcards.render(source, Look(), cache) is source
    assert petcards.render(source, Look(frame="M0"), cache) is source
    assert not cache.exists() and source.read_bytes() == before


def test_frame_is_drawn_where_it_is_opaque_and_nowhere_else(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png", (128, 128))
    before = source.read_bytes()
    card = petcards.render(source, Look(frame="U0"), cache)
    assert card.parent == cache and card.suffix == ".png" and source.read_bytes() == before
    with Image.open(card) as opened:
        assert (opened.format, opened.size, opened.mode) == ("PNG", (128, 128), "RGB")
        made = opened.convert("RGB")
    frame, original = overlay("frame-ultra.png", (128, 128)), Image.open(source).convert("RGB")
    alpha = frame.getchannel("A")
    opaque, clear = alpha.point(lambda level: 255 if level == 255 else 0), alpha.point(
        lambda level: 255 if level == 0 else 0)
    assert opaque.getbbox() and clear.getbbox()
    black = Image.new("RGB", (128, 128))
    # Under the opaque parts of the frame: the frame.  Where it is clear: the pet, untouched.
    assert not differs(Image.composite(made, black, opaque), Image.composite(frame.convert("RGB"), black, opaque))
    assert not differs(Image.composite(made, black, clear), Image.composite(original, black, clear))
    assert differs(made, original)


@pytest.mark.parametrize("ident", sorted(ident for ident, frame in petcards.FRAMES.items()
                                         if frame.file and not frame.animated))
def test_every_static_frame_renders_as_an_overlay(tmp_path, cache, ident):
    """The real files: each static frame is a plain overlay of the card, as the base frames always were."""
    source = make_png(tmp_path / "pets" / "Dragon" / "100.png", (64, 64))
    card = petcards.render(source, Look(frame=ident, seal=1), cache)
    with Image.open(card) as opened:
        assert (opened.format, opened.size) == ("PNG", (64, 64))
        made = opened.convert("RGB")
    top = overlay(petcards.FRAMES[ident].file, (64, 64))
    top.alpha_composite(overlay("seal-1.png", (64, 64)))
    opaque = top.getchannel("A").point(lambda level: 255 if level == 255 else 0)
    black = Image.new("RGB", (64, 64))
    assert share(opaque) > 0.02, "the frame is drawn"
    assert not differs(Image.composite(made, black, opaque), Image.composite(top.convert("RGB"), black, opaque))
    assert ident in card.name.replace("_", "-")


def test_seal_sits_in_the_bottom_right_corner_above_the_frame(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Dog" / "10.png", (128, 128))
    original = Image.open(source).convert("RGB")
    cards = {tier: Image.open(petcards.render(source, Look(seal=tier), cache)).convert("RGB") for tier in (1, 2, 3)}
    for tier, made in cards.items():
        box = overlay(f"seal-{tier}.png", (128, 128)).getchannel("A").point(
            lambda level: 255 if level > 8 else 0).getbbox()
        assert box[0] > 64 and box[1] > 40, "the seal is in the right half, towards the bottom"
        assert differs(made, original, box) and not differs(made, original, (0, 0, 64, 40))
    assert differs(cards[1], cards[2]) and differs(cards[2], cards[3])
    # Seal above frame: with both, the seal's opaque pixels still show the seal.
    both = Image.open(petcards.render(source, Look(frame="U0", seal=3), cache)).convert("RGB")
    seal = overlay("seal-3.png", (128, 128))
    opaque = seal.getchannel("A").point(lambda level: 255 if level == 255 else 0)
    black = Image.new("RGB", (128, 128))
    assert not differs(Image.composite(both, black, opaque), Image.composite(seal.convert("RGB"), black, opaque))
    assert differs(both, cards[3], (0, 0, 40, 40)), "and the frame is there too"


@pytest.mark.parametrize("mode", ["RGBA", "P"])
def test_other_source_modes_and_shapes(tmp_path, cache, assets, mode):
    source = make_png(tmp_path / "pets" / "Bear" / "1.png", (120, 80), mode)
    with Image.open(petcards.render(source, Look(frame="S0", seal=1), cache)) as made:
        # Not square: the overlays are stretched to the picture, its size is kept.
        assert made.size == (120, 80)
        assert made.mode == ("RGBA" if mode == "RGBA" else "RGB")
        if mode == "RGBA":   # transparent where neither the source nor an overlay has anything
            assert made.getchannel("A").getextrema() == (0, 255)


def test_still_card_is_scaled_down_to_fit(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png", (256, 256))
    full = petcards.render(source, Look(frame="U0"), cache)
    budget = full.stat().st_size // 2
    small = petcards.render(source, Look(frame="U0"), cache, max_bytes=budget)
    assert small != full and small.stat().st_size <= budget
    with Image.open(small) as made:
        assert made.format == "PNG" and made.width < 256
    with pytest.raises(CardTooBig):
        petcards.render(source, Look(frame="U0"), cache, max_bytes=50)


# ------------------------------------------------------------------- Holo cards

def test_holo_card_is_a_64_frame_50_ms_endless_gif(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    card = petcards.render(source, Look(foil=FOIL_FULL), cache)
    assert card.suffix == ".gif"
    frames, durations, info = read_frames(card)
    assert len(frames) == 64 and durations == [50] * 64 and info["loop"] == 0
    assert frames[0].size == (96, 96), "a source smaller than HOLO_SIZE is not enlarged"
    original = Image.open(source).convert("RGB")
    for number, frame in enumerate(frames):
        # Left half: foil frame number i on output frame i; right half (clear foil): the pet.
        assert foil_number(frame.getpixel((20, 48))) == number
        assert max(abs(a - b) for a, b in zip(frame.getpixel((80, 30)), original.getpixel((80, 30)))) <= 12


def test_holo_lies_under_frame_and_seal_which_do_not_flicker(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    frames, _, _ = read_frames(petcards.render(source, Look(frame="U0", seal=3, foil=FOIL_FULL), cache))
    assert len(frames) == 64
    top = overlay("frame-ultra.png", (96, 96))
    top.alpha_composite(overlay("seal-3.png", (96, 96)))
    opaque = top.getchannel("A").point(lambda level: 255 if level == 255 else 0)
    black = Image.new("RGB", (96, 96))
    first = Image.composite(frames[0], black, opaque)
    # Close to the overlay's own colours (one palette for everything) ...
    wanted = Image.composite(top.convert("RGB"), black, opaque)
    assert max(high for _, high in ImageChops.difference(first, wanted).getextrema()) <= 24
    # ... and bit-identical in every frame although the foil changes right beside them.
    assert all(not differs(Image.composite(frame, black, opaque), first) for frame in frames)
    assert differs(frames[0], frames[1])


def test_holo_scales_a_big_source_down_to_512(tmp_path, cache, assets, monkeypatch):
    monkeypatch.setattr(petcards, "HOLO_SIZE", 64)   # stands for 512 with a 1024 px source
    source = make_png(tmp_path / "pets" / "Cat" / "0.png", (128, 96))
    with Image.open(petcards.render(source, Look(foil=FOIL_FULL), cache)) as made:
        assert made.size == (64, 48) and made.n_frames == 64
    with Image.open(petcards.render(source, Look(frame="S1-A"), cache)) as made:
        assert made.size == (64, 48), "an animated frame on a still picture: the same size"


def test_real_foil_on_a_small_canvas(tmp_path, cache):
    """The real 20 MB foil: 64 frames of 50 ms, RGBA, usable frame by frame."""
    source = make_png(tmp_path / "pets" / "Cat" / "0.png", (64, 64))
    card = petcards.render(source, Look(foil=FOIL_FULL, seal=2), cache)
    frames, durations, info = read_frames(card)
    assert len(frames) == 64 and durations == [50] * 64 and info["loop"] == 0 and frames[0].size == (64, 64)
    assert differs(frames[0], Image.open(source)) and differs(frames[0], frames[32])
    assert card.stat().st_size < 400_000


# ------------------------------------------------------------ animated frames

def test_animated_frame_on_a_still_plays_its_own_84_x_60_ms(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    card = petcards.render(source, Look(frame="S1-A"), cache)
    assert card.suffix == ".gif"
    frames, durations, info = read_frames(card)
    assert len(frames) == 84 and durations == [60] * 84 and info["loop"] == 0
    assert frames[0].size == (96, 96)
    assert [frame_number(frame, 96) for frame in frames] == list(range(84))
    # The animated frame REPLACES the static one: below its strip the pet only,
    # bit-identical in every frame and close to the source.
    below = (0, 12, 96, 96)
    original = Image.open(source).convert("RGB")
    assert all(not differs(frame, frames[0], below) for frame in frames)
    assert max(high for _, high in ImageChops.difference(frames[0], original).crop(below).getextrema()) <= 24


def test_animated_frame_with_foil_and_seal_on_a_still(tmp_path, cache, assets, monkeypatch):
    monkeypatch.setattr(petcards, "PALETTE_SAMPLES", 84)   # every one of the unrelated foil colours
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    frames, durations, info = read_frames(petcards.render(source, Look(frame="S1-A", seal=3, foil=FOIL_FULL),
                                                          cache))
    # The frame's timeline (84 x 60 = 5040 ms); the foil (64 x 50 = 3200 ms)
    # makes k = round(5040 / 3200) = 2 turns, its picture chosen by time.
    assert len(frames) == 84 and durations == [60] * 84 and info["loop"] == 0
    wanted = [number * 60 * 2 * 64 // 5040 % 64 for number in range(84)]
    assert wanted[:3] == [0, 1, 3] and wanted[42] == 0 and wanted[83] == 62
    assert [foil_number(frame.getpixel((20, 48))) for frame in frames] == wanted
    assert [frame_number(frame, 96) for frame in frames] == list(range(84)), "the frame lies above the foil"
    seal_box = overlay("seal-3.png", (96, 96)).getchannel("A").point(lambda level: 255 if level == 255 else 0)
    black = Image.new("RGB", (96, 96))
    first = Image.composite(frames[0], black, seal_box)
    assert all(not differs(Image.composite(frame, black, seal_box), first) for frame in frames), "seal on top"


def test_mythic_gif_with_an_animated_frame_keeps_its_tempo(tmp_path, cache, assets):
    source = tmp_path / "pets" / "Void" / "0.gif"
    shown = make_gif(source)
    frames, durations, info = read_frames(petcards.render(source, Look(frame="R5-A"), cache))
    assert durations == GIF_DURATIONS and info["loop"] == 0 and frames[0].size == (48, 48)
    # D = 5040 ms, L = 5040 ms -> k = 1: picture floor(t * 84 / 5040) for the frame starting at t.
    starts = [sum(GIF_DURATIONS[:number]) for number in range(len(GIF_DURATIONS))]
    assert [frame_number(frame, 48) for frame in frames] == [start * 84 // 5040 for start in starts] == [
        0, 0, 1, 21, 33, 48, 66]
    below = Image.new("L", (48, 48), 255)
    below.paste(0, (0, 0, 48, 6))
    black = Image.new("RGB", (48, 48))
    for made, wanted in zip(frames, shown):   # outside the strip: exactly the source's frame (its own colours)
        assert not differs(Image.composite(made, black, below), Image.composite(wanted, black, below))


def test_long_mythic_loop_makes_the_frame_turn_twice(tmp_path, cache, assets):
    source = tmp_path / "pets" / "Void" / "10.gif"
    make_gif(source, [1440] * 7)                  # D = 10080 ms -> k = round(10080 / 5040) = 2
    frames, durations, _ = read_frames(petcards.render(source, Look(frame="R5-A", seal=1, foil=FOIL_FULL), cache))
    assert durations == [1440] * 7
    assert [frame_number(frame, 48) for frame in frames] == [0, 24, 48, 72, 12, 36, 60]
    # The foil: k = round(10080 / 3200) = 3 turns.
    assert [foil_number(frame.getpixel((6, 40))) for frame in frames] == [
        number * 1440 * 3 * 64 // 10080 % 64 for number in range(7)]


def test_mythic_gif_with_a_static_frame(tmp_path, cache, assets):
    source = tmp_path / "pets" / "Void" / "100.gif"
    shown = make_gif(source)
    frames, durations, _ = read_frames(petcards.render(source, Look(frame="R5", seal=2), cache))
    assert durations == GIF_DURATIONS
    top = overlay("frames/R5.png", (48, 48))
    top.alpha_composite(overlay("seal-2.png", (48, 48)))
    opaque = top.getchannel("A").point(lambda level: 255 if level == 255 else 0)
    clear = top.getchannel("A").point(lambda level: 255 if level == 0 else 0)
    black = Image.new("RGB", (48, 48))
    assert share(opaque) > 0.01 and share(clear) > 0.2
    first = Image.composite(frames[0], black, opaque)
    assert all(not differs(Image.composite(frame, black, opaque), first) for frame in frames)
    for made, wanted in zip(frames, shown):
        assert not differs(Image.composite(made, black, clear), Image.composite(wanted, black, clear))


def test_a_real_animated_frame(tmp_path, cache):
    """The real R1-A (84 x 60 ms, 512 px) on a small still picture."""
    source = make_png(tmp_path / "pets" / "Cat" / "1.png", (64, 64))
    card = petcards.render(source, Look(frame="R1-A"), cache)
    frames, durations, info = read_frames(card)
    assert sum(durations) == 5040 and info["loop"] == 0 and frames[0].size == (64, 64)
    # Pictures that come out the same at 64 px are merged by the GIF writer (their times added).
    assert 40 <= len(frames) <= 84 and all(duration % 60 == 0 for duration in durations)
    assert any(differs(frames[0], frame) for frame in frames[1:])


# ---------------------------------------------------------------- partial Holo

def test_partial_mask_finds_the_details_near_the_middle():
    picture = detail_picture(256)
    mask = petcards._detail_mask(picture)
    assert mask.mode == "L" and mask.size == (256, 256)
    assert mask.tobytes() == petcards._detail_mask(picture).tobytes(), "deterministic"
    assert mask.crop((74, 126, 88, 140)).getextrema()[1] > 128, "the ring's left side"
    assert mask.crop((120, 124, 136, 140)).getextrema()[1] == 0, "inside the ring: nothing stands out"
    assert mask.crop((0, 0, 40, 40)).getextrema()[1] == 0, "a bright corner is not the pet"
    covered = sum(mask.histogram()[16:]) / (256 * 256)
    assert 0.005 < covered < 0.15


def test_partial_mask_of_a_flat_picture_is_a_soft_glow():
    mask = petcards._detail_mask(Image.new("RGB", (64, 64), (90, 90, 90)))
    low, high = mask.getextrema()
    assert low == 0 and 60 < high < 128 and mask.getpixel((32, 35)) == high


def test_partial_holo_tints_only_the_details(tmp_path, cache, assets):
    source = make_detail_png(tmp_path / "pets" / "Cat" / "0.png")
    original = Image.open(source).convert("RGB")
    card = petcards.render(source, Look(foil=FOIL_PARTIAL), cache)
    frames, durations, info = read_frames(card)
    assert len(frames) == 64 and durations == [50] * 64 and info["loop"] == 0
    touched = Image.new("L", (96, 96), 0)
    for frame in frames:
        touched = ImageChops.lighter(touched, changed(frame, original))
    assert 0.005 < share(touched) < 0.15, "a part of the card, not all of it"
    assert touched.getpixel((48 - 18, 52)) == 255, "the ring (left half: the synthetic foil is opaque there)"
    assert touched.crop((0, 0, 14, 14)).getbbox() is None, "the bright corner stays as it is"
    assert touched.crop((60, 0, 96, 96)).getbbox() is None, "where the foil is clear nothing changes"
    # Full Holo covers the whole left half; no foil is the plain picture.
    full, _, _ = read_frames(petcards.render(source, Look(foil=FOIL_FULL), cache))
    assert share(changed(full[0], original)) > 0.4
    assert all(differs(part, whole) for part, whole in zip(frames, full))
    assert petcards.render(source, Look(), cache) is source
    # Deterministic: the same card from another cache, byte for byte.
    assert petcards.render(source, Look(foil=FOIL_PARTIAL), tmp_path / "other").read_bytes() == card.read_bytes()


def test_partial_holo_on_a_gif_uses_one_mask_for_every_frame(tmp_path, cache, assets):
    source = tmp_path / "pets" / "Void" / "0.gif"
    source.parent.mkdir(parents=True)
    pictures = []
    for number in range(len(GIF_DURATIONS)):   # a ring that stays, a block that wanders
        picture = detail_picture(48)
        picture.paste((200, 60, 60), (2 + number * 4, 40, 8 + number * 4, 46))
        pictures.append(picture)
    pictures[0].save(source, save_all=True, append_images=pictures[1:], duration=GIF_DURATIONS, loop=0)
    with Image.open(source) as opened:
        mask = petcards._partial_mask(opened, True)
        assert mask.tobytes() == petcards._partial_mask(opened, True).tobytes()
    allowed = mask.resize((48, 48)).point(lambda level: 255 if level else 0).filter(ImageFilter.MaxFilter(5))
    frames, durations, _ = read_frames(petcards.render(source, Look(frame="R5-A", foil=FOIL_PARTIAL), cache))
    assert durations == GIF_DURATIONS
    shown, _, _ = read_frames(source)
    strip = Image.new("L", (48, 48), 255)
    strip.paste(0, (0, 0, 48, 6))
    tinted = 0
    for made, wanted in zip(frames, shown):
        difference = ImageChops.multiply(changed(made, wanted), strip)
        assert ImageChops.subtract(difference, allowed).getbbox() is None, "only inside the one mask"
        tinted += difference.getbbox() is not None
    assert tinted >= len(frames) // 2


def test_foil_index_rule():
    # A still picture: 64 frames of 50 ms -> frame i gets foil frame i.
    assert [petcards.foil_index(number * 50, 3200, 3200, 64) for number in range(64)] == list(range(64))
    # The loops of the existing mythic art: 5040 ms -> 2 turns, 7040 ms -> 2 turns.
    assert petcards.foil_index(2520, 5040, 3200, 64) == 0 and petcards.foil_index(2519, 5040, 3200, 64) == 63
    assert petcards.foil_index(3520, 7040, 3200, 64) == 0 and petcards.foil_index(1760, 7040, 3200, 64) == 32
    # A short loop still gets one whole turn, a long one several: seamless at the loop point.
    assert petcards.foil_index(599, 600, 3200, 64) == 63
    assert petcards.foil_index(9999, 10000, 3200, 64) == 63 and petcards.foil_index(5000, 10000, 3200, 64) == 32
    assert petcards.foil_index(10, 0, 3200, 64) == 0
    # An animated frame (84 x 60 ms) on the mythic loops: one turn each.
    assert petcards.layer_index(3520, 7040, 5040, 84) == 42 and petcards.layer_index(6039, 6040, 5040, 84) == 83


def test_frame_plan_merges_durations():
    assert petcards._plan([40, 50, 60, 70, 80], 1) == [(0, 0, 40), (1, 40, 50), (2, 90, 60), (3, 150, 70), (4, 220, 80)]
    assert petcards._plan([40, 50, 60, 70, 80], 2) == [(0, 0, 90), (2, 90, 130), (4, 220, 80)]
    assert petcards._plan([40, 50, 60, 70, 80], 4) == [(0, 0, 220), (4, 220, 80)]


# ------------------------------------------------------- animated (mythic) source

def test_test_gif_really_has_partial_frames(tmp_path):
    """The source the tests below use is the hard kind: later frames are small patches."""
    shown = make_gif(tmp_path / "Void" / "0.gif")
    with Image.open(tmp_path / "Void" / "0.gif") as opened:
        opened.seek(1)
        assert opened.tile[0][1] != (0, 0, 48, 48)
    frames, durations, _ = read_frames(tmp_path / "Void" / "0.gif")
    assert durations == GIF_DURATIONS and all(not differs(a, b) for a, b in zip(frames, shown))


def test_mythic_with_seal_keeps_frames_durations_and_colours(tmp_path, cache, assets):
    source = tmp_path / "pets" / "Void" / "0.gif"
    shown = make_gif(source)
    before = source.read_bytes()
    card = petcards.render(source, Look(seal=3), cache)
    assert card.suffix == ".gif" and source.read_bytes() == before
    frames, durations, info = read_frames(card)
    assert durations == GIF_DURATIONS and info["loop"] == 0 and frames[0].size == (48, 48)
    seal_box = overlay("seal-3.png", (48, 48)).getchannel("A").getbbox()
    outside = Image.new("L", (48, 48), 255)
    outside.paste(0, seal_box)
    black = Image.new("RGB", (48, 48))
    for made, wanted in zip(frames, shown):
        # Outside the seal every frame is exactly the source's (fully composed) frame.
        assert not differs(Image.composite(made, black, outside), Image.composite(wanted, black, outside))
        assert differs(made, wanted, seal_box)


def test_mythic_without_loop_stays_without_loop(tmp_path, cache, assets):
    source = tmp_path / "pets" / "Void" / "10.gif"
    make_gif(source, [100, 200, 300], loop=None)
    _, durations, info = read_frames(petcards.render(source, Look(seal=1), cache))
    assert durations == [100, 200, 300] and "loop" not in info


def test_mythic_with_holo_keeps_its_tempo_and_places_the_foil_by_time(tmp_path, cache, assets):
    source = tmp_path / "pets" / "Void" / "0.gif"
    shown = make_gif(source)
    frames, durations, info = read_frames(petcards.render(source, Look(foil=FOIL_FULL), cache))
    assert durations == GIF_DURATIONS and info["loop"] == 0 and frames[0].size == (48, 48)
    # D = 5040 ms -> k = round(5040 / 3200) = 2 turns of the foil, 2520 ms each;
    # foil_index = floor((t * k / D) * 64) % 64 for the frame starting at t.
    starts = [sum(GIF_DURATIONS[:number]) for number in range(len(GIF_DURATIONS))]
    wanted = [int(start * 2 / 5040 * 64) % 64 for start in starts]
    assert wanted == [0, 1, 2, 32, 50, 9, 37]
    assert [foil_number(frame.getpixel((6, 40))) for frame in frames] == wanted
    for made, original in zip(frames, shown):   # the clear half of the foil shows the source frame
        assert max(high for _, high in ImageChops.difference(made, original).crop((30, 0, 48, 48)).getextrema()) <= 12


# -------------------------------------------------------------- GIF encoding

def test_gif_is_written_without_loss_and_with_one_palette(assets):
    """What the encoder writes is what was quantised: the delta frames lose nothing."""
    pictures = gif_frames(6, 40)
    colours = sorted({colour for picture in pictures for _, colour in picture.getcolors()})
    frames = petcards._quantised(iter(pictures), colours, 0)
    data = petcards._gif(frames, [50, 60, 70, 80, 90, 100], 0, colours)
    target = assets / "check.gif"
    target.write_bytes(data)
    decoded, durations, info = read_frames(target)
    assert durations == [50, 60, 70, 80, 90, 100] and info["loop"] == 0
    assert all(not differs(made, wanted) for made, wanted in zip(decoded, pictures))
    # One global colour table: no frame carries a local one (bit 7 of the image descriptor flags).
    assert local_tables(data) == 0


def local_tables(data: bytes) -> int:
    """How many frames of a GIF bring their own colour table (walks the block structure)."""
    position = 13 + (3 * (2 << (data[10] & 7)) if data[10] & 0x80 else 0)
    count = 0
    while data[position] != 0x3B:
        if data[position] == 0x21:
            position += 2
        else:
            flags = data[position + 9]
            count += flags >> 7
            position += 10 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0) + 1
        while data[position]:
            position += data[position] + 1
        position += 1
    return count


def test_tolerance_keeps_near_pixels_and_never_drifts():
    base = Image.new("RGB", (8, 8), (100, 100, 100))
    pictures = [base.copy() for _ in range(6)]
    for number, picture in enumerate(pictures):
        picture.paste((100 + 3 * number, 100, 100), (0, 0, 4, 8))      # creeps by 3 levels a frame
        picture.paste((30 * number, 0, 0), (4, 0, 8, 4))               # jumps by 30
    colours = sorted({colour for picture in pictures for _, colour in picture.getcolors()})
    exact = petcards._quantised(iter(pictures), colours, 0)
    loose = petcards._quantised(iter(pictures), colours, 8)
    for number, picture in enumerate(pictures):
        # Pillow tells palette colours apart down to 4 levels: "exact" means within 3.
        assert max(high for _, high in ImageChops.difference(exact[number].convert("RGB"), picture).getextrema()) <= 3
        shown = loose[number].convert("RGB")
        # Never further than the tolerance from the truth, however long the creep lasts.
        assert max(high for _, high in ImageChops.difference(shown, picture).getextrema()) <= 8
        assert shown.getpixel((6, 2)) == (30 * number, 0, 0)
    assert loose[1].tobytes()[:4] == loose[0].tobytes()[:4] and loose[5].tobytes()[:4] != loose[0].tobytes()[:4]


# ------------------------------------------------------------------ size ladder

def test_small_budget_takes_a_smaller_variant(tmp_path, cache, assets, caplog):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    full = petcards.render(source, Look(frame="U0", foil=FOIL_FULL), cache)
    budget = full.stat().st_size * 2 // 3
    with caplog.at_level(logging.INFO, logger=petcards.__name__):
        small = petcards.render(source, Look(frame="U0", foil=FOIL_FULL), cache, max_bytes=budget)
    assert small != full and small.stat().st_size <= budget
    frames, durations, info = read_frames(small)
    assert frames[0].width < 96 and info["loop"] == 0
    assert sum(durations) == 3200, "the loop keeps its length also when frames are merged"
    assert any("rendered" in record.message and "colours" in record.message for record in caplog.records)


def test_small_budget_with_an_animated_frame(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    look = Look(frame="S1-A", foil=FOIL_FULL)
    full = petcards.render(source, look, cache)
    budget = full.stat().st_size // 3
    small = petcards.render(source, look, cache, max_bytes=budget)
    assert small != full and small.stat().st_size <= budget
    frames, durations, info = read_frames(small)
    assert frames[0].width < 96 and info["loop"] == 0
    assert sum(durations) == 5040, "the frame's loop keeps its length"


def test_ladder_ends_with_merged_frames(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    sizes = {}
    for variant in petcards.LADDER:
        with Image.open(source) as opened:
            layers = petcards._Moving(foil=petcards._Layer(assets / "holo.apng"))
            try:
                size = petcards._fit((96, 96), round(96 * variant.scale))
                data = petcards._animation(opened, False, layers, Look(foil=FOIL_FULL), size, variant, [50] * 64,
                                           0, None)
            finally:
                layers.close()
        sizes[variant] = len(data)
    ordered = [sizes[variant] for variant in petcards.LADDER]
    assert ordered[-1] < ordered[0] / 4 and min(ordered) == ordered[-1]
    assert petcards.LADDER[0].scale == 1 and petcards.LADDER[0].step == 1
    assert [variant.scale for variant in petcards.LADDER] == sorted(
        (variant.scale for variant in petcards.LADDER), reverse=True)


def test_impossible_budget_raises_in_render_and_falls_back_in_card(tmp_path, cache, assets, caplog):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    with pytest.raises(CardTooBig):
        petcards.render(source, Look(foil=FOIL_FULL), cache, max_bytes=200)
    with pytest.raises(CardTooBig, match="tried before"):   # the ladder is not walked twice
        petcards.render(source, Look(foil=FOIL_FULL), cache, max_bytes=200)
    with caplog.at_level(logging.WARNING, logger=petcards.__name__):
        assert asyncio.run(petcards.card(source, Look(foil=FOIL_FULL), cache, max_bytes=200)) is source
    assert any("CardTooBig" in record.message for record in caplog.records)
    assert not cache.exists() or not list(cache.iterdir()), "nothing too big is ever written"


# ----------------------------------------------------------------------- cache

def test_cache_hit_returns_the_same_file_without_rendering(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    first = petcards.render(source, Look(frame="S0"), cache)
    content = first.read_bytes()
    os.utime(first, (1_000_000_000, 1_000_000_000))

    def boom(*args, **kwargs):
        raise AssertionError("rendered again")

    monkeypatch.setattr(petcards, "_still", boom)
    monkeypatch.setattr(petcards, "_store", boom)
    assert petcards.render(source, Look(frame="S0"), cache) == first
    assert first.read_bytes() == content
    assert first.stat().st_mtime > 1_000_000_000, "a hit marks the card as recently used"
    assert [path.name for path in cache.iterdir()] == [first.name]


def later(path: Path, seconds: int = 5) -> None:
    """Give ``path`` a newer modification time (as if the file had been replaced)."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + seconds * 1_000_000_000))


def test_cache_key_follows_everything_the_picture_depends_on(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    look = Look(frame="S0", seal=2)

    def name(look=look, max_bytes=petcards.DEFAULT_MAX_BYTES, source=source) -> str:
        return petcards._stem(source, look, max_bytes)

    base = name()
    assert name() == base and base.startswith("card-Cat-0-S0-seal2-")
    assert name(look=Look(frame="U0", seal=2)) != base and name(look=Look(frame="S0", seal=3)) != base
    assert name(look=Look(frame="S1", seal=2)) != base and name(look=Look(frame="S1-A", seal=2)) != name(
        look=Look(frame="S1", seal=2))
    assert len({name(look=Look(frame="S0", seal=2, foil=foil)) for foil in (FOIL_NONE, FOIL_PARTIAL, FOIL_FULL)}) == 3
    assert name(look=Look(foil=FOIL_PARTIAL)).startswith("card-Cat-0-holopart-")
    assert name(look=Look(frame="S1-A")).startswith("card-Cat-0-S1_A-")
    assert name(max_bytes=1_000_000) != base
    assert name(source=make_png(tmp_path / "pets" / "Dog" / "0.png")) != base

    later(source)                                                                    # the picture was replaced
    touched = name()
    assert touched != base
    later(assets / "seal-2.png")                                                     # a new seal graphic
    resealed = name()
    assert resealed != touched
    later(assets / "seal-3.png", 9)                                                  # not on this card
    later(assets / "holo.apng", 9)
    later(assets / "frames" / "S1-A.apng", 9)
    assert name() == resealed
    monkeypatch.setattr(petcards, "RENDER_VERSION", petcards.RENDER_VERSION + 1)
    assert name() != resealed


@pytest.mark.parametrize("look, changed_file, other_file", [
    (Look(frame="S1-A"), "frames/S1-A.apng", "frames/R5-A.apng"),
    (Look(frame="S1"), "frames/S1.png", "frames/R5.png"),
    (Look(foil=FOIL_PARTIAL), "holo.apng", "frames/S1-A.apng"),
    (Look(frame="R5-A", seal=1, foil=FOIL_FULL), "frames/R5-A.apng", "frames/S1-A.apng"),
])
def test_cache_key_follows_the_overlays_of_the_look(tmp_path, assets, look, changed_file, other_file):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    before = petcards._stem(source, look, petcards.DEFAULT_MAX_BYTES)
    later(assets / other_file)
    assert petcards._stem(source, look, petcards.DEFAULT_MAX_BYTES) == before
    later(assets / changed_file, 9)
    assert petcards._stem(source, look, petcards.DEFAULT_MAX_BYTES) != before


def test_changed_source_gets_a_new_card(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    first = petcards.render(source, Look(seal=1), cache)
    Image.new("RGB", (96, 96), (200, 30, 30)).save(source)
    later(source)
    second = petcards.render(source, Look(seal=1), cache)
    assert second != first and first.exists()
    assert Image.open(second).convert("RGB").getpixel((5, 5)) == (200, 30, 30)


@pytest.mark.parametrize("folder, stem", [("Żółw ognisty", "1 (kopia)"), ("a b/c", "x.y.z"), ("", "§"),
                                          ("Void", "100"), ("x" * 80, "y" * 80)])
def test_card_names_are_safe_attachment_names(tmp_path, cache, assets, folder, stem):
    source = make_png(tmp_path / "pets" / folder / f"{stem}.png", (32, 32))
    for look in (Look(frame="U0", seal=3), Look(seal=1, foil=FOIL_FULL), Look(frame="S1-A", foil=FOIL_PARTIAL)):
        card = petcards.render(source, look, cache)
        assert ATTACHMENT_NAME.fullmatch(card.name) and len(card.name) < 100, card.name
        assert card.name.startswith("card-") and petcards._CARD_NAME.fullmatch(card.name)


def test_store_is_atomic(tmp_path, monkeypatch):
    folder = tmp_path / "deep" / "cache"
    path = petcards._store(folder, "card-a-0123456789ab.png", b"whole")
    assert path.read_bytes() == b"whole" and [entry.name for entry in folder.iterdir()] == [path.name]

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(petcards.os, "replace", crash)
    with pytest.raises(OSError):
        petcards._store(folder, "card-b-0123456789ab.png", b"never seen")
    with pytest.raises(OSError):
        petcards._store(folder, path.name, b"half")
    # The failed writes left neither a half file nor a temporary one; the old card is intact.
    assert [entry.name for entry in folder.iterdir()] == [path.name] and path.read_bytes() == b"whole"


def test_prune_removes_least_recently_used_cards_only(tmp_path, monkeypatch):
    folder = tmp_path / "cache"
    folder.mkdir()
    now = 2_000_000_000

    def put(name: str, size: int, age: float) -> Path:
        path = folder / name
        path.write_bytes(b"x" * size)
        os.utime(path, (now - age, now - age))
        return path

    monkeypatch.setattr(petcards.time, "time", lambda: float(now))
    monkeypatch.setattr(petcards, "CACHE_MAX_BYTES", 250)
    oldest = put("card-Cat-0-seal1-000000000001.png", 100, 500)
    old = put("card-Cat-0-seal2-000000000002.gif", 100, 400)
    recent = put("card-Cat-0-seal3-000000000003.png", 100, 300)
    fresh = put("card-Cat-0-holo-000000000004.gif", 100, 0)
    foreign = put("notes.txt", 1000, 900)
    lookalike = put("card-Cat-0-seal1-zzzz.png", 1000, 900)
    stale = put("card-Cat-0-holo-000000000005.gif.123-abcdef01.tmp", 50, petcards.TEMP_MAX_AGE_S + 10)
    young = put("card-Cat-0-holo-000000000006.gif.123-abcdef02.tmp", 50, 5)

    removed = petcards._prune(folder, keep=oldest)
    # 400 bytes of cards, limit 250: the two least recently used go - but never the one just made.
    assert {path.name for path in removed} == {old.name, recent.name, stale.name}
    assert oldest.exists() and fresh.exists() and foreign.exists() and lookalike.exists() and young.exists()
    assert petcards._prune(folder, keep=oldest) == []
    assert petcards._prune(tmp_path / "missing") == []


def test_render_prunes_but_keeps_the_new_card(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    first = petcards.render(source, Look(seal=1), cache)
    os.utime(first, (1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(petcards, "CACHE_MAX_BYTES", 1)
    second = petcards.render(source, Look(seal=2), cache)
    assert second.exists() and not first.exists()


# ------------------------------------------------------------------------ card()

async def test_card_renders_off_the_loop_and_hits_the_cache(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    assert await petcards.card(source, Look(), cache) is source and not cache.exists()
    threads = []
    real = petcards.render
    monkeypatch.setattr(petcards, "render", lambda *args, **kwargs: (
        threads.append(threading.current_thread()), real(*args, **kwargs))[1])
    made = await petcards.card(source, Look(frame="U0", seal=3), cache)
    assert made.parent == cache and made.suffix == ".png"
    assert threads and threads[0] is not threading.current_thread()
    assert await petcards.card(source, Look(frame="U0", seal=3), cache) == made
    assert len(threads) == 1, "the second call was a cache hit"


async def test_cards_are_rendered_one_at_a_time_and_only_once(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    running, most, stored = 0, 0, []
    real_still, real_store = petcards._still, petcards._store
    guard = threading.Lock()

    def still(*args, **kwargs):
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        try:
            threading.Event().wait(0.05)
            return real_still(*args, **kwargs)
        finally:
            with guard:
                running -= 1

    monkeypatch.setattr(petcards, "_still", still)
    monkeypatch.setattr(petcards, "_store", lambda *args: (stored.append(args[1]), real_store(*args))[1])
    looks = [Look(seal=1), Look(seal=2), Look(seal=1), Look(seal=3), Look(seal=1)]
    cards = await asyncio.gather(*(petcards.card(source, look, cache) for look in looks))
    assert most == 1 and len(stored) == 3 and len(set(stored)) == 3
    assert cards[0] == cards[2] == cards[4] and len(set(cards)) == 3


async def test_cache_hit_does_not_wait_behind_a_render(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    ready = await petcards.card(source, Look(seal=1), cache)
    started, release = threading.Event(), threading.Event()
    real = petcards._still

    def slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(petcards, "_still", slow)
    rendering = asyncio.create_task(petcards.card(source, Look(seal=2), cache))
    try:
        await asyncio.to_thread(started.wait, 10)
        assert await asyncio.wait_for(petcards.card(source, Look(seal=1), cache), 2) == ready
        assert not rendering.done()
    finally:
        release.set()
    assert (await rendering).suffix == ".png"


async def test_card_falls_back_to_the_source(tmp_path, cache, assets, caplog):
    broken = tmp_path / "pets" / "Cat" / "0.png"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"this is not a picture")
    good = make_png(tmp_path / "pets" / "Cat" / "1.png")
    missing = tmp_path / "pets" / "Cat" / "404.png"
    (assets / "seal-2.png").unlink()
    with caplog.at_level(logging.WARNING, logger=petcards.__name__):
        assert await petcards.card(broken, Look(frame="U0"), cache) is broken
        assert await petcards.card(broken, Look(foil=FOIL_FULL), cache) is broken
        assert await petcards.card(missing, Look(seal=1), cache) is missing
        assert await petcards.card(good, Look(seal=2), cache) is good               # overlay missing
        assert await petcards.card(good, Look(frame="gold"), cache) is good         # no such frame
        assert await petcards.card(good, Look(frame="R1"), cache) is good           # its file is not there
        assert await petcards.card(good, Look(seal=9), cache) is good
        assert await petcards.card(good, Look(foil=7), cache) is good               # no such foil
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 8
    assert (await petcards.card(good, Look(seal=1), cache)).parent == cache, "other cards still work"
    with pytest.raises(Exception):
        petcards.render(good, Look(seal=2), cache)
    (assets / "holo.apng").unlink()
    assert await petcards.card(good, Look(foil=FOIL_FULL), cache) is good
    assert await petcards.card(good, Look(foil=FOIL_PARTIAL), cache) is good


async def test_card_falls_back_when_the_cache_cannot_be_written(tmp_path, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    blocked = tmp_path / "file-not-folder"
    blocked.write_text("in the way")
    assert await petcards.card(source, Look(seal=1), blocked / "cache") is source


async def test_without_pillow_pets_are_shown_plain_with_one_warning(tmp_path, cache, assets, monkeypatch, caplog):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    ready = petcards.render(source, Look(seal=1), cache)
    monkeypatch.setattr(petcards, "Image", None)
    monkeypatch.setattr(petcards, "ImageChops", None)
    monkeypatch.setattr(petcards, "ImageFilter", None)
    monkeypatch.setattr(petcards, "_pillow_warned", False)
    assert petcards.look_of({"shiny": True, "seal": 1, "frame": "S1-A"}) == Look(frame="S1-A", seal=1)
    assert petcards.base_look({"shiny": True, "frame": "S1-A"}) == Look(frame="S0")
    with caplog.at_level(logging.WARNING, logger=petcards.__name__):
        assert await petcards.card(source, Look(seal=2), cache) is source
        assert await petcards.card(source, Look(foil=FOIL_FULL), cache) is source
        assert await petcards.card(source, Look(frame="S1-A", foil=FOIL_PARTIAL), cache) is source
        assert await petcards.card(source, Look(seal=1), cache) == ready, "a finished card needs no Pillow"
    assert len([record for record in caplog.records if "Pillow" in record.message]) == 1
    with pytest.raises(petcards.CardError):
        petcards.render(source, Look(seal=2), cache)


async def test_cancellation_gets_through(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    started, release = threading.Event(), threading.Event()
    real = petcards._still

    def slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(petcards, "_still", slow)
    task = asyncio.create_task(petcards.card(source, Look(seal=1), cache))
    await asyncio.to_thread(started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    monkeypatch.setattr(petcards, "_still", real)
    # The abandoned render finishes on its thread; the next card waits for it and works.
    assert (await asyncio.wait_for(petcards.card(source, Look(seal=2), cache), 10)).exists()
