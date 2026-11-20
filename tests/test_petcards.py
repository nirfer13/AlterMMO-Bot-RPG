"""Pet cards (features/petcards.py): frame, seal and Holo foil drawn on a pet's picture.

No database and no Discord here.  Sources are small generated pictures; the
frame and seal overlays are the real ones (copied into a temporary assets
folder), the 20 MB foil is replaced by a tiny synthetic APNG whose frames say
their own number - except in the one test that decodes the real foil.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
from decimal import Decimal
from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")
from PIL import ImageChops  # noqa: E402

from altermmo.features import petcards  # noqa: E402
from altermmo.features.petcards import CardTooBig, Look  # noqa: E402

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


def overlay(name: str, size: tuple[int, int]):
    with Image.open(REAL_ASSETS / name) as opened:
        return opened.convert("RGBA").resize(size, Image.Resampling.LANCZOS)


@pytest.fixture(scope="module")
def tiny_foil(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("foil") / "holo.apng"
    make_foil(path)
    return path


@pytest.fixture
def assets(tmp_path, monkeypatch, tiny_foil) -> Path:
    """A private assets folder: the real frames and seals, the synthetic foil."""
    folder = tmp_path / "assets"
    folder.mkdir()
    for name in ("frame-shiny.png", "frame-ultra.png", "seal-1.png", "seal-2.png", "seal-3.png"):
        shutil.copy2(REAL_ASSETS / name, folder / name)
    shutil.copy2(tiny_foil, folder / "holo.apng")
    monkeypatch.setattr(petcards, "ASSETS", folder)
    # The frames of the synthetic foil are 64 unrelated colours (the real foil is
    # one rainbow that moves): the palette has to see every one of them.
    monkeypatch.setattr(petcards, "PALETTE_SAMPLES", 64)
    return folder


@pytest.fixture
def cache(tmp_path) -> Path:
    return tmp_path / "cache" / "petcards"


# ---------------------------------------------------------------------- look_of

def test_look_of_follows_the_class_priority():
    assert petcards.look_of({"mythic": False, "ultra_shiny": False, "shiny": False}) == Look()
    assert petcards.look_of({"mythic": False, "ultra_shiny": False, "shiny": True}).frame == "shiny"
    assert petcards.look_of({"mythic": False, "ultra_shiny": True, "shiny": True}).frame == "ultra"
    # A mythic pet has its own GIF and never a frame - whatever else is set.
    assert petcards.look_of({"mythic": True, "ultra_shiny": True, "shiny": True}).frame == ""
    mythic = petcards.look_of({"mythic": True, "ultra_shiny": True, "shiny": True, "seal": 2, "holo": True})
    assert mythic == Look(frame="", seal=2, holo=True) and not mythic.plain


def test_look_of_tolerates_old_rows():
    assert petcards.look_of({}) == Look() and Look().plain
    assert petcards.look_of({"shiny": None, "ultra_shiny": None, "mythic": None, "seal": None, "holo": None}) == Look()
    assert petcards.look_of({"shiny": True}) == Look(frame="shiny")           # no cosmetics columns yet
    assert petcards.look_of({"seal": Decimal("2"), "holo": True}) == Look(seal=2, holo=True)
    assert petcards.look_of({"seal": Decimal("3.0")}).seal == 3
    assert petcards.look_of({"seal": 7}).seal == 3 and petcards.look_of({"seal": -1}).seal == 0
    assert petcards.look_of({"seal": Decimal("NaN")}).seal == 0
    assert petcards.look_of({"seal": "what"}).seal == 0
    assert petcards.look_of(None) == Look()                                    # no row at all


def test_plain_means_nothing_to_draw():
    assert Look().plain
    assert not Look(frame="shiny").plain and not Look(seal=1).plain and not Look(holo=True).plain
    assert petcards.SEAL_NAMES == {1: "Tropiciela", 2: "Łowcy", 3: "Arcyłowcy"}
    assert petcards.DEFAULT_MAX_BYTES == 8 * 1024 * 1024


# ------------------------------------------------------------------ still cards

def test_plain_look_returns_the_source_and_creates_nothing(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    before = source.read_bytes()
    assert petcards.render(source, Look(), cache) is source
    assert not cache.exists() and source.read_bytes() == before


def test_frame_is_drawn_where_it_is_opaque_and_nowhere_else(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png", (128, 128))
    before = source.read_bytes()
    card = petcards.render(source, Look(frame="ultra"), cache)
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
    both = Image.open(petcards.render(source, Look(frame="ultra", seal=3), cache)).convert("RGB")
    seal = overlay("seal-3.png", (128, 128))
    opaque = seal.getchannel("A").point(lambda level: 255 if level == 255 else 0)
    black = Image.new("RGB", (128, 128))
    assert not differs(Image.composite(both, black, opaque), Image.composite(seal.convert("RGB"), black, opaque))
    assert differs(both, cards[3], (0, 0, 40, 40)), "and the frame is there too"


@pytest.mark.parametrize("mode", ["RGBA", "P"])
def test_other_source_modes_and_shapes(tmp_path, cache, assets, mode):
    source = make_png(tmp_path / "pets" / "Bear" / "1.png", (120, 80), mode)
    with Image.open(petcards.render(source, Look(frame="shiny", seal=1), cache)) as made:
        # Not square: the overlays are stretched to the picture, its size is kept.
        assert made.size == (120, 80)
        assert made.mode == ("RGBA" if mode == "RGBA" else "RGB")
        if mode == "RGBA":   # transparent where neither the source nor an overlay has anything
            assert made.getchannel("A").getextrema() == (0, 255)


def test_still_card_is_scaled_down_to_fit(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png", (256, 256))
    full = petcards.render(source, Look(frame="ultra"), cache)
    budget = full.stat().st_size // 2
    small = petcards.render(source, Look(frame="ultra"), cache, max_bytes=budget)
    assert small != full and small.stat().st_size <= budget
    with Image.open(small) as made:
        assert made.format == "PNG" and made.width < 256
    with pytest.raises(CardTooBig):
        petcards.render(source, Look(frame="ultra"), cache, max_bytes=50)


# ------------------------------------------------------------------- Holo cards

def test_holo_card_is_a_64_frame_50_ms_endless_gif(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    card = petcards.render(source, Look(holo=True), cache)
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
    frames, _, _ = read_frames(petcards.render(source, Look(frame="ultra", seal=3, holo=True), cache))
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
    with Image.open(petcards.render(source, Look(holo=True), cache)) as made:
        assert made.size == (64, 48) and made.n_frames == 64


def test_real_foil_on_a_small_canvas(tmp_path, cache):
    """The real 20 MB foil: 64 frames of 50 ms, RGBA, usable frame by frame."""
    source = make_png(tmp_path / "pets" / "Cat" / "0.png", (64, 64))
    card = petcards.render(source, Look(holo=True, seal=2), cache)
    frames, durations, info = read_frames(card)
    assert len(frames) == 64 and durations == [50] * 64 and info["loop"] == 0 and frames[0].size == (64, 64)
    assert differs(frames[0], Image.open(source)) and differs(frames[0], frames[32])
    assert card.stat().st_size < 400_000


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
    frames, durations, info = read_frames(petcards.render(source, Look(holo=True), cache))
    assert durations == GIF_DURATIONS and info["loop"] == 0 and frames[0].size == (48, 48)
    # D = 5040 ms -> k = round(5040 / 3200) = 2 turns of the foil, 2520 ms each;
    # foil_index = floor((t * k / D) * 64) % 64 for the frame starting at t.
    starts = [sum(GIF_DURATIONS[:number]) for number in range(len(GIF_DURATIONS))]
    wanted = [int(start * 2 / 5040 * 64) % 64 for start in starts]
    assert wanted == [0, 1, 2, 32, 50, 9, 37]
    assert [foil_number(frame.getpixel((6, 40))) for frame in frames] == wanted
    for made, original in zip(frames, shown):   # the clear half of the foil shows the source frame
        assert max(high for _, high in ImageChops.difference(made, original).crop((30, 0, 48, 48)).getextrema()) <= 12


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


def test_frame_plan_merges_durations():
    assert petcards._plan([40, 50, 60, 70, 80], 1) == [(0, 0, 40), (1, 40, 50), (2, 90, 60), (3, 150, 70), (4, 220, 80)]
    assert petcards._plan([40, 50, 60, 70, 80], 2) == [(0, 0, 90), (2, 90, 130), (4, 220, 80)]
    assert petcards._plan([40, 50, 60, 70, 80], 4) == [(0, 0, 220), (4, 220, 80)]


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
    full = petcards.render(source, Look(frame="ultra", holo=True), cache)
    budget = full.stat().st_size * 2 // 3
    with caplog.at_level(logging.INFO, logger=petcards.__name__):
        small = petcards.render(source, Look(frame="ultra", holo=True), cache, max_bytes=budget)
    assert small != full and small.stat().st_size <= budget
    frames, durations, info = read_frames(small)
    assert frames[0].width < 96 and info["loop"] == 0
    assert sum(durations) == 3200, "the loop keeps its length also when frames are merged"
    assert any("rendered" in record.message and "colours" in record.message for record in caplog.records)


def test_ladder_ends_with_merged_frames(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    sizes = {}
    for variant in petcards.LADDER:
        with Image.open(source) as opened:
            foil = petcards._Foil(assets / "holo.apng")
            try:
                size = petcards._fit((96, 96), round(96 * variant.scale))
                data = petcards._animation(opened, False, foil, Look(holo=True), size, variant, [50] * 64, 0, None)
            finally:
                foil.close()
        sizes[variant] = len(data)
    ordered = [sizes[variant] for variant in petcards.LADDER]
    assert ordered[-1] < ordered[0] / 4 and min(ordered) == ordered[-1]
    assert petcards.LADDER[0].scale == 1 and petcards.LADDER[0].step == 1
    assert [variant.scale for variant in petcards.LADDER] == sorted(
        (variant.scale for variant in petcards.LADDER), reverse=True)


def test_impossible_budget_raises_in_render_and_falls_back_in_card(tmp_path, cache, assets, caplog):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    with pytest.raises(CardTooBig):
        petcards.render(source, Look(holo=True), cache, max_bytes=200)
    with pytest.raises(CardTooBig, match="tried before"):   # the ladder is not walked twice
        petcards.render(source, Look(holo=True), cache, max_bytes=200)
    with caplog.at_level(logging.WARNING, logger=petcards.__name__):
        assert asyncio.run(petcards.card(source, Look(holo=True), cache, max_bytes=200)) is source
    assert any("CardTooBig" in record.message for record in caplog.records)
    assert not cache.exists() or not list(cache.iterdir()), "nothing too big is ever written"


# ----------------------------------------------------------------------- cache

def test_cache_hit_returns_the_same_file_without_rendering(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    first = petcards.render(source, Look(frame="shiny"), cache)
    content = first.read_bytes()
    os.utime(first, (1_000_000_000, 1_000_000_000))

    def boom(*args, **kwargs):
        raise AssertionError("rendered again")

    monkeypatch.setattr(petcards, "_still", boom)
    monkeypatch.setattr(petcards, "_store", boom)
    assert petcards.render(source, Look(frame="shiny"), cache) == first
    assert first.read_bytes() == content
    assert first.stat().st_mtime > 1_000_000_000, "a hit marks the card as recently used"
    assert [path.name for path in cache.iterdir()] == [first.name]


def test_cache_key_follows_everything_the_picture_depends_on(tmp_path, cache, assets, monkeypatch):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    look = Look(frame="shiny", seal=2)

    def name(look=look, max_bytes=petcards.DEFAULT_MAX_BYTES, source=source) -> str:
        return petcards._stem(source, look, max_bytes)

    base = name()
    assert name() == base and base.startswith("card-Cat-0-shiny-seal2-")
    assert name(look=Look(frame="ultra", seal=2)) != base and name(look=Look(frame="shiny", seal=3)) != base
    assert name(look=Look(frame="shiny", seal=2, holo=True)) != base
    assert name(max_bytes=1_000_000) != base
    assert name(source=make_png(tmp_path / "pets" / "Dog" / "0.png")) != base

    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))     # the picture was replaced
    touched = name()
    assert touched != base
    seal = assets / "seal-2.png"
    stat = seal.stat()
    os.utime(seal, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))       # a new seal graphic
    resealed = name()
    assert resealed != touched
    other = assets / "seal-3.png"                                                    # not on this card
    os.utime(other, ns=(stat.st_atime_ns, stat.st_mtime_ns + 9_000_000_000))
    os.utime(assets / "holo.apng", ns=(stat.st_atime_ns, stat.st_mtime_ns + 9_000_000_000))
    assert name() == resealed
    monkeypatch.setattr(petcards, "RENDER_VERSION", petcards.RENDER_VERSION + 1)
    assert name() != resealed


def test_changed_source_gets_a_new_card(tmp_path, cache, assets):
    source = make_png(tmp_path / "pets" / "Cat" / "0.png")
    first = petcards.render(source, Look(seal=1), cache)
    Image.new("RGB", (96, 96), (200, 30, 30)).save(source)
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = petcards.render(source, Look(seal=1), cache)
    assert second != first and first.exists()
    assert Image.open(second).convert("RGB").getpixel((5, 5)) == (200, 30, 30)


@pytest.mark.parametrize("folder, stem", [("Żółw ognisty", "1 (kopia)"), ("a b/c", "x.y.z"), ("", "§"),
                                          ("Void", "100"), ("x" * 80, "y" * 80)])
def test_card_names_are_safe_attachment_names(tmp_path, cache, assets, folder, stem):
    source = make_png(tmp_path / "pets" / folder / f"{stem}.png", (32, 32))
    for look in (Look(frame="ultra", seal=3), Look(seal=1, holo=True)):
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
    made = await petcards.card(source, Look(frame="ultra", seal=3), cache)
    assert made.parent == cache and made.suffix == ".png"
    assert threads and threads[0] is not threading.current_thread()
    assert await petcards.card(source, Look(frame="ultra", seal=3), cache) == made
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
        assert await petcards.card(broken, Look(frame="ultra"), cache) is broken
        assert await petcards.card(broken, Look(holo=True), cache) is broken
        assert await petcards.card(missing, Look(seal=1), cache) is missing
        assert await petcards.card(good, Look(seal=2), cache) is good               # overlay missing
        assert await petcards.card(good, Look(frame="gold"), cache) is good         # no such frame
        assert await petcards.card(good, Look(seal=9), cache) is good
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 6
    assert (await petcards.card(good, Look(seal=1), cache)).parent == cache, "other cards still work"
    with pytest.raises(Exception):
        petcards.render(good, Look(seal=2), cache)
    (assets / "holo.apng").unlink()
    assert await petcards.card(good, Look(holo=True), cache) is good


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
    monkeypatch.setattr(petcards, "_pillow_warned", False)
    assert petcards.look_of({"shiny": True, "seal": 1}) == Look(frame="shiny", seal=1)
    with caplog.at_level(logging.WARNING, logger=petcards.__name__):
        assert await petcards.card(source, Look(seal=2), cache) is source
        assert await petcards.card(source, Look(holo=True), cache) is source
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
