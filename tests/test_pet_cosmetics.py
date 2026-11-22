"""Pet cosmetics - the seal, the foil (partial / full Holo) and the frames of the classes' pools:
the rolls, where they are kept, the start-up migration, the cards, the owner's commands.

The renderer (features/petcards.py) is not tested here: wherever a card would be
composed, ``pets.petcards.card`` is replaced by a stand-in that records how it was
asked and hands back a file (the ``cards`` fixture).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import time
import zipfile
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import discord
import pytest
import pytest_asyncio

from altermmo import bot as bot_module
from altermmo import schema
from altermmo.cogs import pets as pets_cog
from altermmo.core import guards, ui
from altermmo.features import achievements, backup, boss, hub, patrons, petcards, pets
from altermmo.features.pets import PetType

from fakes import FakeInteraction, FakeMember, FakeRole
from test_pets import add_owner, add_pet, admin, confirms, count, last_embed, owner_row, patron, pet_row, rig, run
from test_ui_pets import rename, show

MIB = 1024 * 1024
_FRAME_LINES = (",\n           FRAME VARCHAR(16) NOT NULL DEFAULT '',"
                "\n           HOLO_PARTIAL BOOLEAN NOT NULL DEFAULT FALSE")
#: ``PETS`` as the v2 bot of 2026-11-20 made it: with SEAL / HOLO, without the frame columns.
V2_PETS = pets.SCHEMA[1].replace(_FRAME_LINES, "")
#: ``PETS`` as the old bot's database has it: without any cosmetic column.
OLD_PETS = V2_PETS.replace(
    ",\n           SEAL NUMERIC NOT NULL DEFAULT 0,\n           HOLO BOOLEAN NOT NULL DEFAULT FALSE", "")
FULL, PARTIAL, NONE = petcards.FOIL_FULL, petcards.FOIL_PARTIAL, petcards.FOIL_NONE
#: The owner's pools (assets/petcards/source/pule.json) written out once more, in units of 0.01%:
#: the stored result ('' = the base of the class) -> how many of the 10000 frame rolls give it.
OWNERS_POOLS = {
    "S": {"": 5200, "S1": 2400, "S1-A": 165, "R1": 1200, "R1-A": 60, "R2": 600, "R2-A": 45, "R7": 300, "R7-A": 30},
    "U": {"": 5200, "U1": 2400, "U1-A": 165, "R3": 1200, "R3-A": 60, "R4": 600, "R4-A": 45, "R8": 300, "R8-A": 30},
    "M": {"": 7000, "R5": 1500, "R5-A": 150, "R6": 900, "R6-A": 90, "R9": 300, "R9-A": 60},
}
#: How the old bot stores a new egg (functions_pets.py): its own column list, no SEAL / HOLO.
OLD_BOT_INSERT = """INSERT INTO PETS
    (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY, MYTHIC, TYPE, VARIANT,
     CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC, LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION) VALUES
    (77, 'Towarzysz', 0, 0, 'Standard', False, False, False, 'Dog', '3', 0, 0, 0, 0, 0, 0, 0, False);"""


# -- fixtures and helpers ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a $potwierdzam slot was never released"


@pytest.fixture
def cards(monkeypatch, tmp_path):
    """Stands in for the renderer: notes every request and "composes" a file named after the look."""
    calls: list[SimpleNamespace] = []

    async def card(source, look, cache_dir, *, max_bytes=petcards.DEFAULT_MAX_BYTES):
        calls.append(SimpleNamespace(source=source, look=look, cache_dir=cache_dir, max_bytes=max_bytes))
        if look.plain:
            return source                                      # as the real one: nothing to compose
        made = tmp_path / (f"card-{source.stem}-{'framed' if look.frame else 'bare'}-{look.seal}"
                           f"{'-holo.gif' if look.foil else '.png'}")
        made.write_bytes(b"a card")
        return made

    monkeypatch.setattr(pets.petcards, "card", card)
    return calls


@pytest_asyncio.fixture
async def cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.pets")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
        channel.permissions_for = lambda member: member.guild_permissions
    guards._last_notice.clear()
    return game_db.bot.get_cog("Pets")


def dice(monkeypatch, *rolls: int, default: int = 999) -> list[tuple]:
    """Script ``random.randrange`` (the cosmetic rolls); returns the list of what was asked.

    The legacy draws (``random.randint`` / ``random.choice``) do not go through it.
    """
    asked: list[tuple] = []
    script = iter(rolls)

    def randrange(*args):
        asked.append(args)
        return next(script, default)

    monkeypatch.setattr(random, "randrange", randrange)
    return asked


def crafter(game=None, name: str = "Crafter") -> FakeMember:
    if game is not None:
        return game.add_member(name, roles=[patrons.CRAFTER_ROLE])
    member = FakeMember(name)
    member.roles.append(FakeRole(patrons.CRAFTER_ROLE))
    return member


async def old_database(db) -> None:
    """Give the test the old bot's ``PETS`` (call it first: before any statement touched the table)."""
    assert "SEAL" not in OLD_PETS and "HOLO" not in OLD_PETS and "FRAME" not in OLD_PETS and "MYTHIC" in OLD_PETS
    await db.execute("DROP TABLE PETS", idempotent=True)
    await db.execute(OLD_PETS, idempotent=True)


async def v2_database(db) -> None:
    """Give the test the ``PETS`` of 2026-11-20: seals and Holo, but no FRAME / HOLO_PARTIAL."""
    assert "SEAL" in V2_PETS and "HOLO BOOLEAN" in V2_PETS and "FRAME" not in V2_PETS and "HOLO_PARTIAL" not in V2_PETS
    await db.execute("DROP TABLE PETS", idempotent=True)
    await db.execute(V2_PETS, idempotent=True)


async def everything(db, pet_id: int) -> tuple:
    """``(seal, holo, frame, holo_partial)`` exactly as the row stores them."""
    row = await pet_row(db, pet_id)
    return row["seal"], row["holo"], row["frame"], row["holo_partial"]


async def pets_columns(db) -> dict[str, tuple[str, str, str | None]]:
    rows = await db.fetch(
        """SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'pets' ORDER BY ordinal_position""")
    return {row["column_name"]: (row["data_type"], row["is_nullable"], row["column_default"]) for row in rows}


async def cosmetics(db, pet_id: int) -> tuple[int, bool]:
    return pets.cosmetics_of(await pet_row(db, pet_id))


# -- the rolls --------------------------------------------------------------------------

@pytest.mark.parametrize("roll, seal", [(0, 3), (5, 3), (6, 2), (149, 2), (150, 1), (449, 1), (450, 0), (999, 0)])
def test_seal_roll_boundaries(monkeypatch, roll, seal):
    asked = dice(monkeypatch, roll, 500)
    assert pets._roll_cosmetics() == pets.Rolled(seal, NONE, "")
    assert asked == [(1000,), (1000,)]                       # one roll for the seal, one for the foil, no frame


@pytest.mark.parametrize("roll, foil", [(0, FULL), (2, FULL), (3, PARTIAL), (32, PARTIAL), (33, NONE), (999, NONE)])
def test_foil_roll_boundaries(monkeypatch, roll, foil):
    dice(monkeypatch, 500, roll)
    assert pets._roll_cosmetics() == pets.Rolled(0, foil, "")


@pytest.mark.parametrize("tier, roll, frame", [
    ("S", 0, ""), ("S", 5199, ""), ("S", 5200, "S1"), ("S", 7599, "S1"), ("S", 7600, "S1-A"), ("S", 7764, "S1-A"),
    ("S", 7765, "R1"), ("S", 8965, "R1-A"), ("S", 9025, "R2"), ("S", 9625, "R2-A"), ("S", 9670, "R7"),
    ("S", 9969, "R7"), ("S", 9970, "R7-A"), ("S", 9999, "R7-A"),
    ("U", 5199, ""), ("U", 5200, "U1"), ("U", 7600, "U1-A"), ("U", 7765, "R3"), ("U", 9025, "R4"),
    ("U", 9625, "R4-A"), ("U", 9670, "R8"), ("U", 9970, "R8-A"),
    ("M", 6999, ""), ("M", 7000, "R5"), ("M", 8500, "R5-A"), ("M", 8650, "R6"), ("M", 9550, "R6-A"),
    ("M", 9640, "R9"), ("M", 9939, "R9"), ("M", 9940, "R9-A"), ("M", 9999, "R9-A")])
def test_frame_roll_boundaries(monkeypatch, tier, roll, frame):
    asked = dice(monkeypatch, 500, 500, roll)
    assert pets._roll_cosmetics(tier) == pets.Rolled(0, NONE, frame)
    assert asked == [(1000,), (1000,), (10000,)]             # the frame comes last, from its own die
    assert frame == "" or petcards.FRAMES[frame].tier == tier


def test_odds_are_exactly_the_owners_balance(monkeypatch):
    """The whole roll space, every value of each die once: exact counts, nothing left over."""
    seals = Counter(pets._seal_for(roll) for roll in range(pets.COSMETIC_ROLL))
    foils = Counter(pets._foil_for(roll) for roll in range(pets.COSMETIC_ROLL))
    assert seals == {0: 550, 1: 300, 2: 144, 3: 6}           # 55% / 30% / 14.4% / 0.6% (x3, 2026-11-22)
    assert foils == {NONE: 967, PARTIAL: 30, FULL: 3}         # 96.7% / 3% / 0.3% (x3, 2026-11-22)
    assert set(petcards.SEAL_NAMES) == {1, 2, 3} and pets.MAX_SEAL == 3
    assert (pets.COSMETIC_ROLL, pets.SEAL_ROLL_BELOW) == (1000, ((6, 3), (150, 2), (450, 1)))
    assert pets.FOIL_ROLL_BELOW == ((3, FULL), (33, PARTIAL))
    assert pets.FRAME_ROLL == 10000                          # units of 0.01%: no float anywhere

    for tier, owners in OWNERS_POOLS.items():
        frames = Counter(pets._frame_for(tier, roll) for roll in range(pets.FRAME_ROLL))
        assert frames == owners and sum(frames.values()) == pets.FRAME_ROLL
        # ... which is exactly the percent of the catalogue, as a fraction (no rounding).
        for frame in petcards.POOLS[tier]:
            stored = "" if frame.base else frame.id
            assert Fraction(frames[stored], pets.FRAME_ROLL) == Fraction(str(frame.percent)) / 100
        # Exhaustive: every result of the pool can come, and nothing else.
        assert set(frames) == {"" if frame.base else frame.id for frame in petcards.POOLS[tier]}
        assert all(frame == "" or petcards.FRAMES[frame].tier == tier for frame in frames)
    assert set(pets.FRAME_ROLL_BELOW) == set(petcards.POOLS) == {"S", "U", "M"}

    # The same counts through the real roll of an egg (a few hundred dice per class are enough
    # to see that _roll_cosmetics reads the frame die and nothing else; the tables are above).
    for tier in OWNERS_POOLS:
        for roll in range(0, pets.FRAME_ROLL, 37):
            dice(monkeypatch, 999, 999, roll)
            assert pets._roll_cosmetics(tier).frame == pets._frame_for(tier, roll)


def test_a_catalogue_that_is_not_whole_units_is_refused(monkeypatch):
    bad = {"S": (petcards.Frame("S0", "S", "x", "frame-shiny.png", False, 84.005),)}
    monkeypatch.setattr(petcards, "POOLS", bad)
    with pytest.raises(ValueError, match="whole number"):
        pets._frame_table("S")
    short = {"S": (petcards.Frame("S0", "S", "x", "frame-shiny.png", False, 84.0),)}
    monkeypatch.setattr(petcards, "POOLS", short)
    with pytest.raises(ValueError, match="not 100%"):
        pets._frame_table("S")


def test_the_three_rolls_are_independent(monkeypatch):
    results = set()
    for seal_roll in (0, 10, 200, 500):
        for foil_roll in (0, 5, 500):
            for frame_roll in (0, 6000, 9990):
                dice(monkeypatch, seal_roll, foil_roll, frame_roll)
                results.add(pets._roll_cosmetics("S"))
    assert results == {pets.Rolled(seal, foil, frame) for seal in (3, 2, 1, 0) for foil in (FULL, PARTIAL, NONE)
                       for frame in ("", "S1", "R7-A")}


def test_only_supporters_roll_and_only_once(monkeypatch):
    rig(monkeypatch, randint=lambda low, high: 905 if high == 1000 else 0)  # a shiny egg: it could get a frame
    asked = dice(monkeypatch, 0, 0, 9999)
    for nobody in (FakeMember("Ala"), None):
        egg = pets._roll_egg(nobody)
        assert (egg["SEAL"], egg["HOLO"], egg["FRAME"], egg["HOLO_PARTIAL"]) == (0, False, "", False)
        assert egg["SHINY"] is True
    assert asked == []                                       # nothing was rolled for them

    egg = pets._roll_egg(crafter())
    assert (egg["SEAL"], egg["HOLO"], egg["FRAME"], egg["HOLO_PARTIAL"]) == (3, True, "R7-A", False)
    assert asked == [(1000,), (1000,), (10000,)]

    # Several roles do not multiply the tries.
    rich = crafter()
    rich.roles += [FakeRole(role) for role in patrons.PATRON_ROLES]
    asked = dice(monkeypatch, 200, 5, 6000)
    egg = pets._roll_egg(rich)
    assert (egg["SEAL"], egg["HOLO"], egg["FRAME"], egg["HOLO_PARTIAL"]) == (1, False, "S1", True)
    assert asked == [(1000,), (1000,), (10000,)]


@pytest.mark.parametrize("egg_roll, shiny, ultra, mythic, tier", [
    (0, False, False, False, ""), (905, True, False, False, "S"), (985, True, True, False, "U"),
    (1000, True, True, True, "M")])
def test_seal_and_foil_do_not_depend_on_the_class_of_the_pet(monkeypatch, egg_roll, shiny, ultra, mythic, tier):
    for seal_roll, foil_roll, expected in ((0, 0, (3, True, False)), (20, 7, (2, False, True)),
                                           (700, 0, (0, True, False)), (700, 40, (0, False, False))):
        pools, ranges = rig(monkeypatch, randint=lambda low, high: egg_roll if high == 1000 else 0)
        asked = dice(monkeypatch, seal_roll, foil_roll, 9999)
        egg = pets._roll_egg(crafter())
        assert (egg["SHINY"], egg["ULTRA_SHINY"], egg["MYTHIC"]) == (shiny, ultra, mythic)
        assert (egg["SEAL"], egg["HOLO"], egg["HOLO_PARTIAL"]) == expected     # a mythic egg rolls too
        # The frame: only an egg with a class rolls in the pool of its class (9999: its rarest result).
        assert egg["FRAME"] == ("" if not tier else petcards.POOLS[tier][-1].id)
        assert len(asked) == (3 if tier else 2)
        # The legacy draws are what they were: the cosmetic rolls come after them, from another die.
        assert ranges[0] == (0, 1000) and pools == [list(pets.MYTHIC_EGGS if mythic else pets.PATRON_EGGS)]


def test_legacy_draws_are_unchanged_by_the_frames(monkeypatch):
    """The same randint / choice calls, in the same order, as before the cosmetics existed."""
    for egg_roll in (0, 905, 985, 1000):
        pools, ranges = rig(monkeypatch, randint=lambda low, high: egg_roll if high == 1000 else 1)
        dice(monkeypatch)
        pets._roll_egg(crafter())
        plain_pools, plain_ranges = list(pools), list(ranges)
        pools.clear()
        ranges.clear()
        pets._roll_egg(crafter(), cosmetics=False)
        assert (pools, ranges) == (plain_pools, plain_ranges)
        assert ranges[0] == (0, 1000) and len(ranges) == (1 if egg_roll == 1000 else 2)


def test_nothing_is_rolled_without_the_columns(monkeypatch):
    asked = dice(monkeypatch, 0, 0, 0)
    egg = pets._roll_egg(crafter(), cosmetics=False)
    assert asked == []
    assert set(egg) == {"TYPE", "QUALITY", "VARIANT", "SHINY", "ULTRA_SHINY", "MYTHIC"}


@pytest.mark.parametrize("foil_roll, holo", [(0, True), (5, False), (500, False)])
def test_without_the_frame_columns_seal_and_full_holo_work_as_before(monkeypatch, caplog, foil_roll, holo):
    """The v2 table of 2026-11-20: no frame roll, a partial Holo has nowhere to go (it is logged)."""
    rig(monkeypatch, randint=lambda low, high: 985 if high == 1000 else 0)  # an ultra shiny egg
    asked = dice(monkeypatch, 1, foil_roll, 9999)
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        egg = pets._roll_egg(crafter(), frames=False)
    assert asked == [(1000,), (1000,)]                       # the frame die is not cast
    assert (egg["SEAL"], egg["HOLO"]) == (3, holo) and "FRAME" not in egg and "HOLO_PARTIAL" not in egg
    lost = [record for record in caplog.records if "partial Holo of a new" in record.message]
    assert len(lost) == (1 if foil_roll == 5 else 0)


# -- storing the egg --------------------------------------------------------------------

async def test_supporters_egg_is_stored_with_its_cosmetics(game_db, monkeypatch):
    db, bot = game_db.bot.db, game_db.bot
    rig(monkeypatch, randint=0)
    lucky, plain = crafter(game_db), game_db.add_member("Ala")
    asked = dice(monkeypatch, 30, 400)                       # Łowcy, no Holo
    assert await pets.assign_pet(game_db.scene(), lucky) is True
    assert await cosmetics(db, 1) == (2, False)
    assert bot.owner_log == []                               # not rare enough to tell the owner

    assert await pets.assign_pet(game_db.scene(), plain) is True
    assert await cosmetics(db, 2) == (0, False) and len(asked) == 2            # no roll for a non-supporter
    assert await everything(db, 2) == (0, False, "", False)

    # A supporter who already has a pet gets no egg - and no roll.
    assert await pets.assign_pet(game_db.scene(), lucky) is False
    assert len(asked) == 2


async def test_frame_and_partial_holo_are_stored_with_the_egg(game_db, monkeypatch):
    db, bot = game_db.bot.db, game_db.bot
    rig(monkeypatch, randint=lambda low, high: 905 if high == 1000 else 0)    # shiny eggs
    asked = dice(monkeypatch, 500, 5, 8000)                                   # no seal, partial Holo, R1
    lucky, plain = crafter(game_db), game_db.add_member("Ala")
    assert await pets.assign_pet(game_db.scene(), lucky) is True
    assert await everything(db, 1) == (0, False, "R1", True)
    assert bot.owner_log == []                     # a 12% frame and a 3% foil: not rare enough to tell the owner
    assert pets.ornaments_of(await pet_row(db, 1)) == pets.Ornaments("R1", 0, PARTIAL)

    assert await pets.assign_pet(game_db.scene(), plain) is True               # a shiny egg, no supporter
    assert await everything(db, 2) == (0, False, "", False) and len(asked) == 3
    assert petcards.frame_of(await pet_row(db, 2)).id == "S0"                  # the base of its class

    # An ordinary supporter's egg: no frame roll, nothing stored but the default.
    await db.execute("UPDATE PETOWNER SET PET_ID = 0")
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 500, 0, 9999)
    assert await pets.assign_pet(game_db.scene(), lucky) is True
    assert await everything(db, 3) == (0, True, "", False) and len(asked) == 2


@pytest.mark.parametrize("seal_roll, holo_roll, frame_roll, mythic, stored, frame, told, achieved", [
    (1, 500, 999, False, (3, False), "", "pieczęć Arcyłowcy", []),
    (500, 0, 999, False, (0, True), "", "folię Holo", []),
    (0, 0, 999, False, (3, True), "", "pieczęć Arcyłowcy i folię Holo", []),
    (1, 500, 999, True, (3, False), "", "pieczęć Arcyłowcy", ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]),
    (500, 0, 999, True, (0, True), "", "folię Holo", ["Mityczny towarzysz", "Mityczny Holo"]),
    (0, 0, 999, True, (3, True), "", "pieczęć Arcyłowcy i folię Holo",
     ["Mityczny towarzysz", "Mityczny Holo", "Mityczna Pieczęć Arcyłowcy"]),
    # The frames: every animated edition and the 1% static frame of a pool.
    (500, 500, 9999, True, (0, False), "R9-A", "ramkę Oko Otchłani (animowana)", ["Mityczny towarzysz"]),
    (500, 500, 9900, True, (0, False), "R9", "ramkę Oko Otchłani", ["Mityczny towarzysz"]),
    (500, 500, 8500, True, (0, False), "R5-A", "ramkę Astrolabium (animowana)", ["Mityczny towarzysz"]),
    (0, 5, 9980, True, (3, False), "R9-A", "pieczęć Arcyłowcy i ramkę Oko Otchłani (animowana)",
     ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]),
    (0, 0, 9980, True, (3, True), "R9-A", "pieczęć Arcyłowcy, folię Holo i ramkę Oko Otchłani (animowana)",
     ["Mityczny towarzysz", "Mityczny Holo", "Mityczna Pieczęć Arcyłowcy"])])
async def test_owner_hears_about_rare_cosmetics_and_nobody_else(game_db, monkeypatch, caplog, seal_roll, holo_roll,
                                                                frame_roll, mythic, stored, frame, told, achieved):
    db, bot = game_db.bot.db, game_db.bot
    await achievements.ensure_roles(bot)                   # as after a start: the achievement roles exist
    bot.owner_log.clear()
    game_db.log_channel.messages.clear()
    rig(monkeypatch, randint=lambda low, high: 1000 if mythic and high == 1000 else 0)
    dice(monkeypatch, seal_roll, holo_roll, frame_roll)
    lucky = crafter(game_db)
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        assert await pets.assign_pet(game_db.scene(), lucky) is True
    assert await cosmetics(db, 1) == stored and (await pet_row(db, 1))["frame"] == frame
    assert bot.owner_log == [f"Rzadka kosmetyka towarzysza: jajko gracza <@{lucky.id}> (PET_ID 1) ma {told}."]
    assert game_db.log_channel.messages[-1].content == bot.owner_log[0]        # in the log channel, no ping
    assert f"<@{bot.ids.owner}>" not in game_db.log_channel.messages[-1].content
    # The notice itself is never public.  Since the evening of 2026-11-20 only a MYTHIC egg is an
    # achievement (with Holo / the rarest seal one more each), announced after the mythic egg itself;
    # a Holo or a seal on any other egg is no achievement - and nothing else is posted.
    public = [message.content for message in game_db.channel.bot_messages[1:]]
    mythic_news = [text for text in public if "zdobył mitycznego towarzysza" in text]
    assert len(mythic_news) == int(mythic)
    assert [text for text in public if text not in mythic_news] == [
        f"<@{lucky.id}> zdobywa osiągnięcie: {name}!" for name in achieved]
    assert all(told not in message.text for message in game_db.channel.sent)
    assert any("rare cosmetics" in record.message for record in caplog.records)


@pytest.mark.parametrize("egg_roll, foil_roll, frame_roll, frame", [
    (905, 5, 9890, "R7"), (905, 500, 9990, "R7-A"), (985, 500, 9890, "R8"), (985, 500, 8000, "R3"),
    (905, 500, 6000, "S1"), (1000, 5, 999, ""), (1000, 500, 8000, "R5"), (1000, 500, 9000, "R6")])
async def test_which_frames_the_owner_hears_about(game_db, monkeypatch, egg_roll, foil_roll, frame_roll, frame):
    """Every animated edition and the 3% static frame of each pool (R7 / R8 / R9) - no partial Holo."""
    db, bot = game_db.bot.db, game_db.bot
    await achievements.ensure_roles(bot)                   # (a mythic egg is an achievement: its role exists)
    bot.owner_log.clear()
    rig(monkeypatch, randint=lambda low, high: egg_roll if high == 1000 else 0)
    dice(monkeypatch, 500, foil_roll, frame_roll)
    assert await pets.assign_pet(game_db.scene(), crafter(game_db)) is True
    assert (await pet_row(db, 1))["frame"] == frame
    rare = frame in ("R7", "R7-A", "R8")
    assert len(bot.owner_log) == int(rare)
    if rare:
        assert bot.owner_log[0].endswith(f"ma ramkę {petcards.FRAMES[frame].label}.")


async def test_partial_holo_is_not_the_mythic_holo_achievement(game_db, monkeypatch):
    """"Mityczny Holo" keeps meaning FULL Holo (PETS.HOLO): a mythic pet with partial Holo earns only the pet."""
    db, bot = game_db.bot.db, game_db.bot
    await achievements.ensure_roles(bot)
    bot.owner_log.clear()
    rig(monkeypatch, randint=lambda low, high: 1000 if high == 1000 else 0)
    dice(monkeypatch, 500, 3, 999)                                            # partial Holo, base frame
    lucky = crafter(game_db)
    assert await pets.assign_pet(game_db.scene(), lucky) is True
    assert await everything(db, 1) == (0, False, "", True)
    public = [message.content for message in game_db.channel.bot_messages]
    assert f"<@{lucky.id}> zdobywa osiągnięcie: Mityczny towarzysz!" in public
    assert not any("Mityczny Holo" in text for text in public)
    assert await achievements._pet_keys(bot, lucky.id) == {"pet_mythic"}
    assert bot.owner_log == []                                                # partial Holo is not reported
    # The same pet with full Holo would be the achievement (the rule is PETS.HOLO = TRUE).
    await db.execute("UPDATE PETS SET HOLO = TRUE WHERE PET_ID = 1")
    assert await achievements._pet_keys(bot, lucky.id) == {"pet_mythic", "pet_mythic_holo"}


async def test_generated_egg_rolls_as_for_the_owner(game_db, monkeypatch):
    db, bot = game_db.bot.db, game_db.bot
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 0, 0)
    assert await pets.generate_pet_egg(game_db.scene()) == 1                   # the owner is not on the server
    assert await cosmetics(db, 1) == (0, False) and asked == []
    game_db.add_member("Alter", roles=[patrons.PATRON_ROLES[0]], member_id=bot.ids.owner)
    assert await pets.generate_pet_egg(game_db.scene()) == 2
    assert await cosmetics(db, 2) == (3, True)
    assert bot.owner_log == [f"Rzadka kosmetyka towarzysza: jajko gracza <@{bot.ids.owner}> (PET_ID 2) ma "
                             "pieczęć Arcyłowcy i folię Holo."]


async def test_the_columns_are_looked_up_once_per_run(game_db, monkeypatch):
    bot = game_db.bot
    lookups = []
    real = bot.db.fetch

    async def fetch(query, *args, **kwargs):
        if "pg_attribute" in query:
            lookups.append(args)
        return await real(query, *args, **kwargs)

    monkeypatch.setattr(bot.db, "fetch", fetch)
    assert await pets.cosmetics_available(bot) is True
    for name in ("Ala", "Ola", "Ela"):
        assert await pets.assign_pet(game_db.scene(), game_db.add_member(name)) is True
    assert await pets.cosmetics_available(bot, refresh=True) is True           # a "yes" is never asked again
    assert await pets.frames_available(bot, refresh=True) is True
    assert lookups == [(["seal", "holo", "frame", "holo_partial"],)]           # one look answers both


@pytest.mark.parametrize("make, cosmetics_on, frames_on", [
    (old_database, False, False), (v2_database, True, False), (None, True, True)])
async def test_what_is_available_follows_the_columns(game_db, make, cosmetics_on, frames_on):
    bot = game_db.bot
    if make is not None:
        await make(bot.db)
    assert await pets.frames_available(bot) is frames_on                       # asked first: it looks for both
    assert await pets.cosmetics_available(bot) is cosmetics_on
    assert await pets._cosmetic_state(bot) == (cosmetics_on, frames_on)
    # Only the frame columns there (somebody added them by hand to the old table): still off - they
    # need the seal / Holo columns beside them (add_new_columns adds a table's columns all or none).
    if make is old_database:
        await bot.db.execute("ALTER TABLE PETS ADD COLUMN FRAME VARCHAR(16) NOT NULL DEFAULT '', "
                             "ADD COLUMN HOLO_PARTIAL BOOLEAN NOT NULL DEFAULT FALSE")
        assert await pets.frames_available(bot, refresh=True) is False
        assert await pets.cosmetics_available(bot, refresh=True) is False


# -- cosmetics are for the pet's whole life ----------------------------------------------

async def test_cosmetics_survive_everything_that_happens_to_a_pet(game_db, monkeypatch, cards):
    db, bot = game_db.bot.db, game_db.bot
    player, heir = patron(game_db), game_db.add_member("Ola")
    await add_owner(db, player.id, 5, reroll_scroll=3, rebirth_stones=1, mirrors=1)
    # A shiny egg with every cosmetic column set (both foils at once only to see that neither
    # flag is ever written over - full Holo wins when it is shown).
    kept = (2, True, "R2-A", True)
    await add_pet(db, 5, variant="7", shiny=True, seal=2, holo=True, frame="R2-A", holo_partial=True)
    rig(monkeypatch, randint=lambda low, high: high)
    asked = dice(monkeypatch, 0, 0, 0)                                         # would change them, if rolled
    confirms(game_db, player)
    scene = game_db.scene(player)

    for level in (1, 2, 3):                                                    # evolution 0 -> 1 -> 2 -> 3
        assert await pets.level_up_pet(scene, player.id) is True
        assert (await pet_row(db, 5))["pet_lvl"] == level and await everything(db, 5) == kept
    await db.execute("UPDATE PETS SET PET_SKILLS = 4 WHERE PET_ID = 5")        # leaves room for an enlightenment

    assert await pets.reroll_pet(scene, player) is True                        # $odrodzenie
    assert await everything(db, 5) == kept
    assert await pets.enlight_pet(scene, player) is True                       # $oswiecenie
    assert await everything(db, 5) == kept
    before = await pet_row(db, 5)
    rig(monkeypatch, choices=[PetType.DRAGON], randint=lambda low, high: high)
    assert await pets.transform_pet(scene, player) is True                     # the mirror
    row = await pet_row(db, 5)
    assert (before["type"], row["type"]) == ("Cat", "Dragon") and await everything(db, 5) == kept
    assert (row["shiny"], row["ultra_shiny"], row["mythic"]) == (True, False, False)   # the class never changes
    assert await pets.name_pet(scene, "Reksio") is True                        # a new name (it is shiny already)
    row = await pet_row(db, 5)
    assert row["pet_name"] == "Reksio" and row["shiny"] is True and await everything(db, 5) == kept

    assert await pets.store_pet(scene, 2) is True                              # into the stable ...
    assert (await owner_row(db, player.id))["pet_id_alt2"] == 5 and await everything(db, 5) == kept
    assert await pets.unstore_pet(scene, 2) is True                            # ... and out again
    assert (await owner_row(db, player.id))["pet_id"] == 5 and await everything(db, 5) == kept

    # A new owner - one without any rank: the specimen keeps what it has (it is only not shown
    # to him, see test_cosmetics_are_shown_only_while_the_owner_supports).
    await db.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", player.id)
    assert await pets.reassign_pet(bot, 5, heir.id) is True
    assert await everything(db, 5) == kept
    cards.clear()
    assert await pets.show_pet(game_db.scene(heir), heir) is True
    assert cards[-1].look == petcards.Look(frame="S0")                         # the base of the class only
    assert await everything(db, 5) == kept
    heir.roles.append(FakeRole(patrons.TWITCH_SUB_ROLE))                       # the heir starts to support
    assert await pets.show_pet(game_db.scene(heir), heir) is True
    assert cards[-1].look == petcards.Look(frame="R2-A", seal=2, foil=FULL)
    assert "Ramka: Miedziane ciernie (animowana)\nPieczęć: Łowcy\nFolia: Holo\n" in last_embed(game_db).description

    # The end of the support changes nothing in the database either: the first owner lost the rank.
    rank = list(player.roles)
    player.roles.clear()
    await db.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", heir.id)
    assert await pets.reassign_pet(bot, 5, player.id) is True
    assert await pets.show_pet(scene, player) is True
    assert await everything(db, 5) == kept
    player.roles.extend(rank)                                                  # ... and it is all back with the rank
    assert await pets.show_pet(scene, player) is True
    assert cards[-1].look == petcards.Look(frame="R2-A", seal=2, foil=FULL)
    assert asked == []                                                         # nothing was ever rolled again


async def test_partial_holo_and_a_frame_survive_growing_and_the_mirror(game_db, monkeypatch, cards):
    """The realistic pair (partial Holo alone) through the same flows; every UPDATE leaves it as it is."""
    db = game_db.bot.db
    player = patron(game_db)
    await add_owner(db, player.id, 6, reroll_scroll=1, mirrors=1)
    await add_pet(db, 6, variant="3", shiny=True, ultra_shiny=True, frame="U1-A", holo_partial=True, seal=1)
    rig(monkeypatch, randint=lambda low, high: high)
    confirms(game_db, player)
    scene = game_db.scene(player)
    assert await pets.level_up_pet(scene, player.id) is True
    assert await pets.reroll_pet(scene, player) is True
    rig(monkeypatch, choices=[PetType.SNAKE], randint=lambda low, high: high)
    assert await pets.transform_pet(scene, player) is True
    assert await everything(db, 6) == (1, False, "U1-A", True)
    assert (await pet_row(db, 6))["type"] == "Snake"
    assert await pets.show_pet(scene, player) is True
    assert cards[-1].look == petcards.Look(frame="U1-A", seal=1, foil=PARTIAL)
    assert "Ramka: Rubinowa Korona (animowana)\nPieczęć: Tropiciela\nFolia: Holo częściowe\n" in (
        last_embed(game_db).description)


def test_only_the_admin_statements_write_cosmetics():
    """Every UPDATE / INSERT of PETS in the bot's code: SEAL, HOLO, HOLO_PARTIAL and FRAME appear only in
    the owner's ``set_cosmetics`` (two forms: with and without the frame columns) and ``set_frame``.

    (The INSERT of a new egg adds them through its ``{columns}`` placeholder.)
    """
    cosmetic = re.compile(r"\b(SEAL|HOLO|HOLO_PARTIAL|FRAME)\b")
    pattern = re.compile(r'(UPDATE\s+PETS\s+SET|INSERT\s+INTO\s+PETS\b)[^"]*')
    writers = []
    statements = 0
    for path in sorted(Path(pets.__file__).parents[1].rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            statements += 1
            if cosmetic.search(match.group(0)):
                writers.append((path.name, match.group(1).split()[0]))
    assert statements >= 9 and writers == [("pets.py", "UPDATE")] * 3
    owners = [len([m for m in pattern.finditer(inspect.getsource(function)) if cosmetic.search(m.group(0))])
              for function in (pets.set_cosmetics, pets.set_frame)]
    assert owners == [2, 1]                                                    # all three are the owner's
    assert "{columns}" in pets._SQL_INSERT_EGG and not cosmetic.search(pets._SQL_INSERT_EGG)
    assert not cosmetic.search(pets._SQL_SAVE_PET.upper())


# -- reading rows with and without the columns --------------------------------------------

def test_rows_without_the_columns_read_as_no_cosmetics():
    assert pets.cosmetics_of({"pet_id": 1}) == (0, False)                      # the old bot's table
    assert pets.cosmetics_of({"seal": None, "holo": None}) == (0, False)
    assert pets.cosmetics_of({"seal": Decimal("2"), "holo": True}) == (2, True)
    assert pets.cosmetics_of({"seal": 3, "holo": False}) == (3, False)
    assert pets.cosmetics_of({"seal": 7, "holo": True}) == (0, True)            # a tier nobody knows
    assert pets.cosmetics_of({"seal": -1}) == (0, False)
    assert pets.cosmetics_of({"seal": "x", "holo": "tak"}) == (0, False)


def test_ornaments_read_every_shape_of_a_row():
    o = pets.Ornaments
    assert pets.ornaments_of({"pet_id": 1}) == o() and not o().any            # the old bot's table
    assert pets.ornaments_of({"seal": 2, "holo": False}) == o("", 2, NONE)     # the table of 2026-11-20
    assert pets.ornaments_of({"shiny": True, "frame": "S1-A", "holo_partial": True}) == o("S1-A", 0, PARTIAL)
    assert pets.ornaments_of({"shiny": True, "frame": " s1-a "}) == o("S1-A")  # as petcards.frame_of reads it
    assert pets.ornaments_of({"shiny": True, "frame": "S0"}) == o()            # the base frame is no ornament
    assert pets.ornaments_of({"shiny": True, "frame": ""}) == o()
    assert pets.ornaments_of({"shiny": True, "frame": "U1"}) == o()            # another class's frame: the base
    assert pets.ornaments_of({"shiny": True, "frame": "R99"}) == o()
    assert pets.ornaments_of({"shiny": True, "frame": None, "holo_partial": None}) == o()
    assert pets.ornaments_of({"frame": "S1"}) == o()                           # an ordinary pet has no frame
    assert pets.ornaments_of({"mythic": True, "frame": "R9-A"}) == o("R9-A")
    assert pets.ornaments_of({"mythic": True, "frame": "M0"}) == o()
    assert pets.ornaments_of({"holo": True, "holo_partial": True}) == o("", 0, FULL)   # full wins
    assert pets.ornaments_of({"holo_partial": "tak"}) == o()                   # only a real TRUE counts
    assert o("S1").any and o("", 1).any and o("", 0, PARTIAL).any


ITEMS = dict(reroll_scroll=0, reroll_scroll_shard=0, rebirth_stones=0, mirrors=0)


def grown_row(**columns) -> dict:
    return {"pet_id": 5, "pet_name": "Burek", "pet_lvl": 2, "pet_skills": 3, "quality": "Standard",
            "shiny": False, "type": "Cat", "variant": "1", "crit_perc": 4, "replace_perc": 0, "def_perc": 0,
            "drop_perc": 0, "lowhp_perc": 0, "slow_perc": 0, "init_perc": 0, "detection": False,
            "ultra_shiny": False, "mythic": False, **columns}


@pytest.mark.parametrize("columns, lines", [
    (dict(seal=1, holo=False), "Pieczęć: Tropiciela\n"),
    (dict(seal=2, holo=False), "Pieczęć: Łowcy\n"),
    (dict(seal=3, holo=True), "Pieczęć: Arcyłowcy\nFolia: Holo\n"),
    (dict(seal=0, holo=True), "Folia: Holo\n"),
    (dict(seal=0, holo=False), ""),
    (dict(), ""),                                                              # a row of the old table
    (dict(seal=None, holo=None), ""),
    (dict(holo_partial=True), "Folia: Holo częściowe\n"),
    (dict(holo=True, holo_partial=True), "Folia: Holo\n"),                     # full wins
    (dict(shiny=True, frame="S1"), "Ramka: Gwiazda Szronu\n"),
    (dict(shiny=True, frame="S1-A"), "Ramka: Gwiazda Szronu (animowana)\n"),
    (dict(shiny=True, frame="S0"), ""),                                        # the base frame is not named
    (dict(shiny=True, frame=""), ""),
    (dict(shiny=True, frame="R8"), ""),                                        # not of its class: the base
    (dict(frame="R7"), ""),                                                    # an ordinary pet: no frame
    (dict(shiny=True, ultra_shiny=True, frame="R4-A", seal=3, holo_partial=True),
     "Ramka: Rozbite zwierciadło (animowana)\nPieczęć: Arcyłowcy\nFolia: Holo częściowe\n"),
    (dict(mythic=True, frame="R9", seal=1, holo=True), "Ramka: Oko Otchłani\nPieczęć: Tropiciela\nFolia: Holo\n")])
def test_embed_names_the_cosmetics_of_a_hatched_pet(columns, lines):
    embed = pets._pet_embed(1, grown_row(**columns), ITEMS)
    assert embed.description.split("\n\n**💎")[0].split("\n\n*")[0] == (
        "Oto Twój towarzysz, <@1>.\n\n📜 **STATYSTYKI:**\nPoziom: 2\nTalent: Przeciętny\nWygląd: Standard\n"
        + lines + "\nSzansa na krytyczne uderzenie: 4 %")
    if not any(key in columns for key in ("shiny", "mythic")):
        assert embed.description == (
            "Oto Twój towarzysz, <@1>.\n\n📜 **STATYSTYKI:**\nPoziom: 2\nTalent: Przeciętny\nWygląd: Standard\n"
            + lines + "\nSzansa na krytyczne uderzenie: 4 %\n"
            "\n**💎 PRZEDMIOTY:**\nZwoje odrodzenia: 0\nFragmenty zwojów: 0\nKamienie olśnienia: 0\nLustra wizualiów: 0")


@pytest.mark.parametrize("columns, hidden", [
    (dict(shiny=True, frame="S1"), True), (dict(shiny=True, frame="S1-A"), True), (dict(holo_partial=True), True),
    (dict(seal=1), True), (dict(holo=True), True), (dict(shiny=True, frame="S0"), False),
    (dict(shiny=True), False), (dict(frame="S1"), False), (dict(), False)])
def test_one_line_says_what_is_hidden_from_a_non_supporter(columns, hidden):
    embed = pets._pet_embed(1, grown_row(**columns), ITEMS, cosmetics=False)
    assert (pets.MSG_COSMETICS_HIDDEN in embed.description) is hidden
    assert embed.description.count(pets.MSG_COSMETICS_HIDDEN) <= 1
    assert "Ramka:" not in embed.description and "Folia:" not in embed.description
    assert "Pieczęć:" not in embed.description
    assert "ramka" in pets.MSG_COSMETICS_HIDDEN and "pieczęć" in pets.MSG_COSMETICS_HIDDEN


def test_egg_names_its_cosmetics_too():
    """Owner's decision: the seal and the Holo are visible from the egg on."""
    plain = pets._pet_embed(1, grown_row(pet_lvl=0), ITEMS)
    rich = pets._pet_embed(1, grown_row(pet_lvl=0, seal=3, holo=True), ITEMS)
    assert rich.title == plain.title == "Jajko Burek"
    assert "Pieczęć" not in plain.description and "Folia" not in plain.description
    assert "wyrośnie...\n\n**💎 PRZEDMIOTY:**" in plain.description            # an egg without them: as it was
    assert "wyrośnie...\n\nPieczęć: Arcyłowcy\nFolia: Holo\n\n**💎 PRZEDMIOTY:**" in rich.description
    assert rich.description.replace("\nPieczęć: Arcyłowcy\nFolia: Holo\n", "") == plain.description
    # The owner does not support right now: one line says why they are gone.
    hidden = pets._pet_embed(1, grown_row(pet_lvl=0, seal=3, holo=True), ITEMS, cosmetics=False)
    assert "Pieczęć:" not in hidden.description and "Folia: Holo" not in hidden.description
    assert f"wyrośnie...\n\n{pets.MSG_COSMETICS_HIDDEN}\n\n**💎 PRZEDMIOTY:**" in hidden.description


# -- the card that is sent --------------------------------------------------------------

async def test_hatched_pet_is_shown_as_its_card(game_db, cards):
    db, bot = game_db.bot.db, game_db.bot
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, variant="1", type="Cat", seal=2, holo=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert message.files == ["card-11-bare-2-holo.gif"]                        # the card, not the illustration
    assert message.embeds[0].image.url == "attachment://card-11-bare-2-holo.gif"
    assert message.view.image.name == "card-11-bare-2-holo.gif"                # the panel remembers what it sent
    assert "Wygląd: Standard\nPieczęć: Łowcy\nFolia: Holo\n\n" in message.embeds[0].description

    [asked] = cards
    assert asked.source == pets.ASSETS_DIR / "pets" / "Cat" / "11.png"         # the plain illustration
    assert asked.look == petcards.look_of(await pet_row(db, 5))
    assert (asked.look.seal, asked.look.foil, asked.look.plain) == (2, petcards.FOIL_FULL, False)
    assert asked.cache_dir == bot.store.data_dir / "cache" / "petcards"
    assert asked.max_bytes == int(10 * MIB * 0.95)                             # 10 MiB less 5%

    # Without the panel (buttons=False) the same file goes out.
    assert await pets.show_pet(game_db.scene(player), player, buttons=False) is True
    assert game_db.channel.messages[-1].files == ["card-11-bare-2-holo.gif"]
    assert game_db.channel.messages[-1].view is None


async def test_real_renderer_and_the_pet_code_fit_together(game_db):
    """No stand-in here: a still card (a frame and a seal) composed by features/petcards.py itself."""
    pytest.importorskip("PIL")
    db, bot = game_db.bot.db, game_db.bot
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, variant="1", type="Cat", shiny=True, seal=2)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    sent = message.view.image
    assert sent.parent == bot.store.data_dir / "cache" / "petcards" and sent.is_file()
    assert re.fullmatch(r"card-Cat-11-[A-Za-z0-9_-]+\.png", sent.name)          # a name Discord takes as it is
    assert message.files == [sent.name] and message.embeds[0].image.url == f"attachment://{sent.name}"
    assert sent.read_bytes() != (pets.ASSETS_DIR / "pets" / "Cat" / "11.png").read_bytes()
    # The same pet again: the same file out of the cache; the panel has nothing new to send.
    assert await message.view.redraw(bot) is True and "attachments" not in message.edits[-1]
    # An egg and an ordinary pet without cosmetics never get into the cache.
    await db.execute("UPDATE PETS SET SHINY = FALSE, SEAL = 0")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["11.png"]
    assert [path.name for path in sent.parent.iterdir()] == [sent.name]


async def test_card_may_be_as_big_as_the_servers_upload_limit_less_a_margin(game_db, cards):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=1, shiny=True)
    game_db.guild.filesize_limit = 25 * MIB                                    # a boosted server
    await pets.show_pet(game_db.scene(player), player)
    assert cards[-1].max_bytes == int(25 * MIB * 0.95)
    game_db.guild.filesize_limit = 0                                           # unknown: Discord's 10 MiB
    await pets.show_pet(game_db.scene(player), player)
    assert cards[-1].max_bytes == int(10 * MIB * 0.95)
    assert pets._card_max_bytes(None) == int(10 * MIB * 0.95)
    assert pets.DEFAULT_UPLOAD_LIMIT == boss.DEFAULT_UPLOAD_LIMIT              # the repeated constant


@pytest.mark.parametrize("flags, framed", [
    (dict(), False), (dict(shiny=True), True), (dict(shiny=True, ultra_shiny=True), True),
    (dict(shiny=True, ultra_shiny=True, mythic=True, quality="Mythic", type="Void", variant="0"), False)])
async def test_frame_follows_the_class_of_the_pet(game_db, cards, flags, framed):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, **{"pet_lvl": 1, "variant": "1", **flags})
    assert await pets.show_pet(game_db.scene(player), player) is True
    message, look = game_db.channel.messages[-1], cards[-1].look
    assert bool(look.frame) is framed and look.plain is not framed             # mythic: its own GIF, no frame
    source = cards[-1].source.name
    assert message.files == (["card-1-framed-0.png"] if framed else [source])
    assert message.embeds[0].image.url == f"attachment://{message.files[0]}"


async def test_egg_is_a_card_like_a_hatched_pet(game_db, cards):
    """Frame, seal and Holo are visible from the egg on (owner's decision)."""
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, variant="7", shiny=True, ultra_shiny=True, seal=3, holo=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert cards[-1].source == pets.ASSETS_DIR / "eggs" / "standard" / "7.png"
    assert cards[-1].look == petcards.Look(frame="U0", seal=3, foil=petcards.FOIL_FULL)   # the frame of its class too
    assert message.files == ["card-7-framed-3-holo.gif"]
    assert message.embeds[0].image.url == "attachment://card-7-framed-3-holo.gif"
    assert message.embeds[0].title == "Jajko"
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in message.embeds[0].description

    # Hatching changes the illustration, not the look.
    assert await pets.level_up_pet(game_db.scene(player), player.id) is True
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert cards[-1].look == petcards.Look(frame="U0", seal=3, foil=petcards.FOIL_FULL)
    assert cards[-1].source == pets.ASSETS_DIR / "pets" / "Cat" / "1.png"

    # A shiny egg of somebody who does not support: the frame alone; a mythic egg: its own GIF, no frame.
    stranger = game_db.add_member("Ola")
    await add_owner(db, stranger.id, 6)
    await add_pet(db, 6, variant="3", shiny=True, seal=1)
    assert await pets.show_pet(game_db.scene(stranger), stranger) is True
    assert cards[-1].look == petcards.Look(frame="S0")
    assert game_db.channel.messages[-1].files == ["card-3-framed-0.png"]
    await db.execute("UPDATE PETS SET QUALITY = 'Mythic', TYPE = 'Void', VARIANT = '0', MYTHIC = TRUE, "
                     "ULTRA_SHINY = TRUE WHERE PET_ID = 6")
    assert await pets.show_pet(game_db.scene(stranger), stranger) is True
    assert cards[-1].look.plain and game_db.channel.messages[-1].files == ["0.gif"]


async def test_ordinary_egg_is_shown_exactly_as_before(game_db, cards):
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, variant="7")                                          # no class, no seal, no Holo
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert message.files == ["7.png"] and message.embeds[0].image.url == "attachment://7.png"
    assert [call.look.plain for call in cards] == [True]                       # nothing is composed
    assert message.embeds[0].title == "Jajko"
    assert "Pieczęć" not in message.embeds[0].description and "Folia" not in message.embeds[0].description


async def test_eggs_cosmetics_are_hidden_while_the_owner_does_not_support(game_db, cards):
    db = game_db.bot.db
    player = game_db.add_member("Ala")                                         # no supporter role
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, variant="7", seal=2, holo=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert message.files == ["7.png"] and cards[-1].look.plain                 # the plain egg
    assert pets.MSG_COSMETICS_HIDDEN in message.embeds[0].description
    player.roles.append(FakeRole(patrons.PATRON_ROLES[2]))                     # supports: there they are
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["card-7-bare-2-holo.gif"]
    assert "Pieczęć: Łowcy\nFolia: Holo\n" in last_embed(game_db).description


async def test_plain_picture_is_sent_when_there_is_no_card(game_db, monkeypatch, tmp_path):
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=3, variant="1", type="Dog", shiny=True, seal=1)

    async def source_again(source, look, cache_dir, *, max_bytes):            # how the renderer reports a failure
        return source

    async def broken(source, look, cache_dir, *, max_bytes):                  # it promised never to raise; if it does
        raise RuntimeError("no Pillow today")

    async def nowhere(source, look, cache_dir, *, max_bytes):                 # a card that is not on the disk
        return tmp_path / "missing.png"

    async def nothing(source, look, cache_dir, *, max_bytes):
        return None

    for renderer in (source_again, broken, nowhere, nothing):
        monkeypatch.setattr(pets.petcards, "card", renderer)
        assert await pets.show_pet(game_db.scene(player), player) is True
        message = game_db.channel.messages[-1]
        assert message.files == ["101.png"] and message.embeds[0].image.url == "attachment://101.png"
        assert "Pieczęć: Tropiciela\n" in message.embeds[0].description       # the words stay

    # A pet without a picture has nothing to compose a card from.
    calls = []
    monkeypatch.setattr(pets.petcards, "card", lambda *args, **kwargs: calls.append(args))
    await db.execute("UPDATE PETS SET TYPE = 'Hydra'")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == [] and calls == []


async def test_panel_gets_a_new_card_when_the_look_changes(game_db, monkeypatch, cards):
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=2, variant="1", seal=1)
    panel = await show(game_db, player)
    assert panel.files == ["card-11-bare-1.png"] and panel.embeds[0].image.url == "attachment://card-11-bare-1.png"

    rig(monkeypatch, randint=0)                                                # the name does not make it shiny
    await rename(game_db, panel, player, "Reksio")
    assert "attachments" not in panel.edits[-1]                                # the same card: not sent again
    assert panel.embeds[0].title == "Kot Reksio"
    assert panel.embeds[0].image.url == "attachment://card-11-bare-1.png"

    rig(monkeypatch, randint=100)                                              # this one does: a frame appears
    await rename(game_db, panel, player, "Azor")
    assert (await pet_row(db, 5))["shiny"] is True
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["card-11-framed-1.png"]
    assert panel.embeds[0].image.url == "attachment://card-11-framed-1.png"
    assert panel.view.image.name == "card-11-framed-1.png"

    await db.execute("UPDATE PETS SET PET_LVL = 3")                            # it grew: another illustration
    assert await panel.view.redraw(game_db.bot) is True
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["card-101-framed-1.png"]
    assert cards[-1].max_bytes == int(10 * MIB * 0.95) and cards[-1].look.seal == 1


# -- shown only while the owner supports (owner's decision of 2026-11-20) -----------------

SUPPORT_LOST_LINE = "*Ozdoby karty (wyjątkowa ramka, pieczęć, folia Holo) widać tylko u wspierających.*"


@pytest.mark.parametrize("flags, frame, base", [
    (dict(shiny=True), "S1-A", "S0"), (dict(shiny=True), "R7", "S0"),
    (dict(shiny=True, ultra_shiny=True), "U1-A", "U0"), (dict(shiny=True, ultra_shiny=True), "R8", "U0"),
    (dict(shiny=True, ultra_shiny=True, mythic=True, quality="Mythic", type="Void", variant="0"), "R9-A", "")])
async def test_a_rarer_frame_is_shown_only_while_the_owner_supports(game_db, cards, flags, frame, base):
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, **{"pet_lvl": 1, "variant": "1", **flags, "frame": frame, "holo_partial": True})
    scene = game_db.scene(player)
    assert await pets.show_pet(scene, player) is True
    assert cards[-1].look == petcards.Look(frame=frame, foil=PARTIAL)
    description = last_embed(game_db).description
    assert f"Ramka: {petcards.FRAMES[frame].label}\n" in description and "Folia: Holo częściowe\n" in description
    assert SUPPORT_LOST_LINE not in description

    player.roles.clear()                                                       # the support ended
    assert await pets.show_pet(scene, player) is True
    assert cards[-1].look == petcards.Look(frame=base) == petcards.base_look(await pet_row(db, 5))
    description = last_embed(game_db).description
    assert "Ramka:" not in description and "Folia:" not in description
    assert description.count(SUPPORT_LOST_LINE) == 1                           # one line says why
    assert await everything(db, 5) == (0, False, frame, True)                  # nothing was taken away

    # A rarer frame alone is enough to ask whether the owner supports (and to hide it if not).
    await db.execute("UPDATE PETS SET HOLO_PARTIAL = FALSE")
    assert await pets.show_pet(scene, player) is True
    assert cards[-1].look == petcards.Look(frame=base) and SUPPORT_LOST_LINE in last_embed(game_db).description
    player.roles.append(FakeRole(patrons.CRAFTER_ROLE))
    assert await pets.show_pet(scene, player) is True
    assert cards[-1].look == petcards.Look(frame=frame)


@pytest.mark.parametrize("role", [patrons.PATRON_ROLES[0], patrons.CRAFTER_ROLE, patrons.TWITCH_SUB_ROLE,
                                  *patrons.YOUTUBE_ROLES])
async def test_cosmetics_are_shown_only_while_the_owner_supports(game_db, cards, role):
    """Losing the rank hides the seal and the Holo (card and words); getting it back shows them again."""
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[role])
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, variant="1", type="Cat", shiny=True, seal=3, holo=True)
    scene = game_db.scene(player)

    assert await pets.show_pet(scene, player) is True                          # supports: everything is there
    shown = game_db.channel.messages[-1]
    assert shown.files == ["card-11-framed-3-holo.gif"]
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in shown.embeds[0].description
    assert SUPPORT_LOST_LINE not in shown.embeds[0].description

    player.roles.clear()                                                       # the support ended
    assert await pets.show_pet(scene, player) is True
    hidden = game_db.channel.messages[-1]
    assert cards[-1].look == petcards.Look(frame="S0")                         # the frame is the pet's class: it stays
    assert hidden.files == ["card-11-framed-0.png"]
    assert hidden.embeds[0].image.url == "attachment://card-11-framed-0.png"
    description = hidden.embeds[0].description
    assert "Pieczęć" not in description.replace(SUPPORT_LOST_LINE, "") and "Folia: Holo" not in description
    assert f"Wygląd: Standard\n{SUPPORT_LOST_LINE}\n\n" in description         # one line says why
    assert await cosmetics(db, 5) == (3, True)                                 # nothing was taken away

    player.roles.append(FakeRole(role))                                        # supports again: it is all back
    assert await pets.show_pet(scene, player) is True
    assert game_db.channel.messages[-1].files == ["card-11-framed-3-holo.gif"]
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in last_embed(game_db).description


async def test_pet_without_cosmetics_of_a_non_supporter_looks_as_always(game_db, cards):
    """Nothing to hide: no line about supporters, and an ordinary pet stays the plain picture."""
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=1, variant="1", type="Cat", ultra_shiny=True, shiny=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert cards[-1].look == petcards.Look(frame="U0")                         # frames are for everybody
    assert "wspierających" not in last_embed(game_db).description
    await db.execute("UPDATE PETS SET SHINY = FALSE, ULTRA_SHINY = FALSE, SEAL = 2")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["1.png"] and cards[-1].look.plain   # hidden seal: plain picture
    # The same goes for an egg: hidden for him, with the line that says why.
    await db.execute("UPDATE PETS SET PET_LVL = 0, HOLO = TRUE")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert cards[-1].look.plain and pets.MSG_COSMETICS_HIDDEN in last_embed(game_db).description


async def test_panel_follows_the_support_when_it_is_redrawn(game_db, monkeypatch, cards):
    """The panel asks again at every redraw: a rename after the rank was lost shows the bare card."""
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=2, variant="1", seal=1, holo=True)
    panel = await show(game_db, player)
    assert panel.files == ["card-11-bare-1-holo.gif"]

    player.roles.clear()
    rig(monkeypatch, randint=0)                                                # the name does not make it shiny
    await rename(game_db, panel, player, "Reksio")
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["11.png"]   # the plain illustration
    assert panel.embeds[0].image.url == "attachment://11.png" and panel.view.image.name == "11.png"
    assert SUPPORT_LOST_LINE in panel.embeds[0].description

    player.roles.append(FakeRole(patrons.TWITCH_SUB_ROLE))
    assert await panel.view.redraw(game_db.bot) is True
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["card-11-bare-1-holo.gif"]
    assert "Pieczęć: Tropiciela\nFolia: Holo\n" in panel.embeds[0].description


async def test_owner_given_as_an_id_is_looked_up(game_db, monkeypatch, cards):
    """A bare id (no roles to read): the member is looked up; a stranger or a failed lookup hides the cosmetics."""
    db, bot = game_db.bot.db, game_db.bot
    supporter, plain = crafter(game_db, "Ala"), game_db.add_member("Ola")
    for owner_id, pet_id in ((supporter.id, 5), (plain.id, 6), (424242, 7)):
        await add_owner(db, owner_id, pet_id)
        await add_pet(db, pet_id, pet_lvl=1, variant="1", seal=2)
    assert await pets._is_supporter(bot, supporter.id) is True
    assert await pets._is_supporter(bot, plain.id) is False
    assert await pets._is_supporter(bot, 424242) is False                      # left the server

    scene = game_db.scene(plain)
    assert await pets.show_pet(scene, supporter.id, buttons=False) is True
    assert cards[-1].look.seal == 2 and "Pieczęć: Łowcy\n" in last_embed(game_db).description
    assert await pets.show_pet(scene, 424242, buttons=False) is True
    assert cards[-1].look.plain and SUPPORT_LOST_LINE in last_embed(game_db).description

    async def broken(user_id):
        raise RuntimeError("Discord is down")

    monkeypatch.setattr(bot, "member", broken)
    assert await pets._is_supporter(bot, supporter.id) is False                # decoration: never an error
    assert await pets.show_pet(scene, supporter.id, buttons=False) is True     # the pet is shown in any case
    assert cards[-1].look.plain

    # A pet with nothing to show costs no lookup at all.
    await db.execute("UPDATE PETS SET SEAL = 0")
    lookups = []
    monkeypatch.setattr(bot, "member", lambda user_id: lookups.append(user_id))
    assert await pets.show_pet(scene, supporter.id, buttons=False) is True and lookups == []


async def test_click_is_acknowledged_before_the_card_is_composed(game_db, monkeypatch, cards):
    """A cold Holo card can take seconds; Discord gives a click three."""
    db = game_db.bot.db
    player = patron(game_db)
    await add_owner(db, player.id, 5, mirrors=1)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=1, variant="1", holo=True)
    events: list[str] = []
    real_card, real_ack = pets.petcards.card, ui.ack

    async def card(*args, **kwargs):
        events.append("card")
        return await real_card(*args, **kwargs)

    async def ack(interaction):
        events.append("ack")
        return await real_ack(interaction)

    monkeypatch.setattr(pets.petcards, "card", card)
    monkeypatch.setattr(ui, "ack", ack)

    # The "Towarzysz" button of the pinned panel.
    clicked = FakeInteraction(game_db.bot, player, channel=game_db.channel)
    assert await hub.show_display(clicked, hub.show_pet) is True
    panel = game_db.channel.messages[-1]
    assert events == ["ack", "card"] and clicked.response.deferred
    assert panel.files == ["card-1-bare-0-holo.gif"]

    # A gameplay button under the pet: the flow shows the pet anew.
    events.clear()
    rig(monkeypatch, choices=[PetType.DOG], randint=0)
    confirms(game_db, player)
    await game_db.click(panel, player, "pet:transform")
    assert events[0] == "ack" and events[-1] == "card"
    assert game_db.channel.messages[-1].files == ["card-0-bare-0-holo.gif"]

    # The rename window: the panel is redrawn after the submitted window was acknowledged.
    events.clear()
    panel = game_db.channel.messages[-1]
    rig(monkeypatch, randint=100)
    await rename(game_db, panel, player, "Azor")
    assert events == ["ack", "card"]


# -- the start-up migration -------------------------------------------------------------

async def test_migration_adds_the_columns_to_the_old_table(db, monkeypatch):
    await old_database(db)
    await add_pet(db, 5, pet_lvl=2, shiny=True)
    await add_pet(db, 6)
    before = await pets_columns(db)
    assert "seal" not in before and "holo" not in before and "frame" not in before
    # Only the cosmetic columns: every other table (ACHIEVEMENT_COUNTERS too) is made by create_all.
    assert await schema.verify(db) == ["pets: brak kolumny seal", "pets: brak kolumny holo",
                                       "pets: brak kolumny frame", "pets: brak kolumny holo_partial"]

    assert await schema.add_new_columns(db) == []
    columns = await pets_columns(db)
    assert list(columns) == [*before, "seal", "holo", "frame", "holo_partial"]  # appended, nothing else touched
    assert {name: columns[name] for name in before} == before
    assert columns["seal"] == ("numeric", "NO", "0") and columns["holo"] == ("boolean", "NO", "false")
    assert columns["frame"] == ("character varying", "NO", "''::character varying")
    assert columns["holo_partial"] == ("boolean", "NO", "false")
    for pet_id in (5, 6):                                                      # the pets that were there
        assert await everything(db, pet_id) == (0, False, "", False)
    assert (await pet_row(db, 5))["shiny"] is True and await count(db, "PETS") == 2
    assert await schema.verify(db) == []

    # Every later start: nothing is altered any more (not even a lock on the table is asked for).
    transactions = []
    real = db.transaction
    monkeypatch.setattr(db, "transaction", lambda *args, **kwargs: transactions.append(1) or real(*args, **kwargs))
    assert await schema.add_new_columns(db) == [] and await schema.add_new_columns(db) == []
    assert transactions == [] and await pets_columns(db) == columns

    # The old bot's INSERT (its own column list) still works; its pet has no cosmetics.
    await db.execute(OLD_BOT_INSERT)
    assert await everything(db, 77) == (0, False, "", False)


async def test_migration_adds_the_frame_columns_to_the_v2_table(db):
    """The table of 2026-11-20 (seals and Holo there) gets the two new columns; what is stored stays."""
    await v2_database(db)
    await add_pet(db, 5, pet_lvl=2, shiny=True, seal=3, holo=True)
    before = await pets_columns(db)
    assert "seal" in before and "frame" not in before
    assert await schema.verify(db) == ["pets: brak kolumny frame", "pets: brak kolumny holo_partial"]
    assert await schema.add_new_columns(db) == []
    columns = await pets_columns(db)
    assert list(columns) == [*before, "frame", "holo_partial"] and {name: columns[name] for name in before} == before
    assert await everything(db, 5) == (3, True, "", False)                     # the base frame of its class
    assert petcards.frame_of(await pet_row(db, 5)).id == "S0"
    assert await schema.verify(db) == []


async def test_migration_is_what_the_feature_modules_declare(db):
    assert schema.new_columns() == [("pets", "seal", "NUMERIC NOT NULL DEFAULT 0"),
                                    ("pets", "holo", "BOOLEAN NOT NULL DEFAULT FALSE"),
                                    ("pets", "frame", "VARCHAR(16) NOT NULL DEFAULT ''"),
                                    ("pets", "holo_partial", "BOOLEAN NOT NULL DEFAULT FALSE"),
                                    ("pet_album", "frame", "VARCHAR(16) NOT NULL DEFAULT ''"),
                                    ("pet_album", "holo_partial", "BOOLEAN NOT NULL DEFAULT FALSE")]
    assert pets.COSMETIC_COLUMNS == ("seal", "holo") and pets.FRAME_COLUMNS == ("frame", "holo_partial")
    assert [column for _, column, _ in pets.NEW_COLUMNS] == [*pets.COSMETIC_COLUMNS, *pets.FRAME_COLUMNS]
    assert schema.EXPECTED_COLUMNS["pets"]["seal"] == "numeric"
    assert schema.EXPECTED_COLUMNS["pets"]["holo"] == "boolean"
    assert schema.EXPECTED_COLUMNS["pets"]["frame"] == "character varying"
    assert schema.EXPECTED_COLUMNS["pets"]["holo_partial"] == "boolean"
    # The fresh table of pets.SCHEMA has every column NEW_COLUMNS would add, with the same definition.
    for _, column, definition in pets.NEW_COLUMNS:
        assert f"{column.upper()} {definition}" in pets.SCHEMA[1]
    assert await schema.add_new_columns(db) == []                              # a fresh database: nothing to add
    await db.execute("DROP TABLE PETS", idempotent=True)                       # no table at all: verify's business
    assert await schema.add_new_columns(db) == []
    assert "brak tabeli pets" in await schema.verify(db)


async def test_migration_gives_up_on_a_busy_table_instead_of_hanging(db, monkeypatch):
    await old_database(db)
    await add_pet(db, 5)
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    async with db.transaction() as somebody:                                   # a long transaction of somebody else
        await somebody.execute("LOCK TABLE PETS IN ACCESS SHARE MODE")
        started = time.monotonic()
        problems = await schema.add_new_columns(db)
        assert time.monotonic() - started < 5
    assert len(problems) == 1
    assert problems[0].startswith("pets (seal, holo, frame, holo_partial): LockNotAvailableError")
    assert "seal" not in await pets_columns(db) and await count(db, "PETS") == 1
    assert await schema.add_new_columns(db) == []                              # the next start: the table is free
    assert await cosmetics(db, 5) == (0, False)


async def test_migration_adds_all_columns_of_a_table_or_none(db, monkeypatch):
    await old_database(db)
    monkeypatch.setattr(pets, "NEW_COLUMNS", (*pets.NEW_COLUMNS[:1], ("pets", "holo", "NO SUCH TYPE")))
    problems = await schema.add_new_columns(db)
    assert len(problems) == 1 and "pets (seal, holo)" in problems[0]
    assert "seal" not in await pets_columns(db)                                # rolled back with the failed one


async def test_migration_reports_an_unreachable_database_instead_of_raising():
    class Down:
        async def fetch(self, *args, **kwargs):
            raise RuntimeError("database is down")

    assert await schema.add_new_columns(Down()) == ["RuntimeError: database is down"]


async def test_start_up_adds_the_columns_or_tells_the_owner_once(game_db, monkeypatch):
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    await old_database(db)

    async with db.transaction() as somebody:
        await somebody.execute("LOCK TABLE PETS IN ACCESS SHARE MODE")
        await bot.setup_hook()                                                 # starts all the same
    [note] = bot._startup_notes                                                # one note, saying what it means
    problem = note.removeprefix("Nie udało się dodać do bazy danych kolumn nowych w v2: ").split(". Kosmetyka")[0]
    assert problem.startswith("pets (seal, holo, frame, holo_partial): LockNotAvailableError")
    assert note == bot_module.MSG_NEW_COLUMNS_FAILED.format(problems=problem)
    assert "Kosmetyka towarzyszy (pieczęcie, folia Holo) jest WYŁĄCZONA" in note
    assert "NOWE RAMKI" not in note and "album" not in note                    # the worst case says all of it
    assert "Baza danych różni się" not in note                                 # not the alarm for a broken database
    assert await pets.cosmetics_available(bot) is False

    await bot.on_ready()
    told = game_db.log_channel.messages[-1].content
    assert told.startswith(f"<@{bot.ids.owner}>!   Uwagi po starcie bota:\n- Nie udało się dodać") and note in told
    posted = len(game_db.log_channel.messages)
    await bot.on_ready()                                                       # a reconnect: not told again
    assert len(game_db.log_channel.messages) == posted and bot._startup_notes == []

    # The next start finds the table free: the columns are added, nothing to report.
    await bot.setup_hook()
    assert bot._startup_notes == [] and await schema.verify(db) == []
    assert (await pets_columns(db))["holo"] == ("boolean", "NO", "false")


async def test_start_up_note_when_only_the_frame_columns_are_missing(game_db, monkeypatch):
    """The PETS of 2026-11-20 (seals, Holo) whose FRAME / HOLO_PARTIAL cannot be added: only those are off."""
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    await v2_database(db)

    async with db.transaction() as somebody:
        await somebody.execute("LOCK TABLE PETS IN ACCESS SHARE MODE")
        await bot.setup_hook()
    [note] = bot._startup_notes
    problem = note.removeprefix("Nie udało się dodać do bazy danych kolumn nowych w v2: ").split(". Pieczęcie")[0]
    assert problem.startswith("pets (frame, holo_partial): LockNotAvailableError")
    assert note == bot_module.MSG_FRAME_COLUMNS_FAILED.format(problems=problem)
    assert "Pieczęcie i pełna folia Holo działają normalnie" in note and "NOWE RAMKI" in note
    assert "Kosmetyka towarzyszy (pieczęcie, folia Holo) jest WYŁĄCZONA" not in note
    assert "Baza danych różni się" not in note
    assert await pets.cosmetics_available(bot) is True and await pets.frames_available(bot) is False


async def test_start_up_note_when_only_the_album_lacks_its_columns(game_db, monkeypatch):
    """PETS is complete, the album made before the frames cannot get them: keeping such a pet is refused."""
    from altermmo.features import album

    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    await db.execute(f"DROP TABLE IF EXISTS {album.TABLE}", idempotent=True)
    await db.execute(
        f"""CREATE TABLE {album.TABLE} (ENTRY_ID BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            PLAYER_ID NUMERIC NOT NULL, ADDED_AT TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            {', '.join(f'{column} {definition}' for column, definition in album.BASE_COLUMNS)})""",
        idempotent=True)

    async with db.transaction() as somebody:
        await somebody.execute(f"LOCK TABLE {album.TABLE} IN ACCESS SHARE MODE")
        await bot.setup_hook()
    [note] = bot._startup_notes
    problem = note.removeprefix("Nie udało się dodać do bazy danych kolumn nowych w v2: ").split(". Kosmetyka")[0]
    assert problem.startswith("pet_album (frame, holo_partial): LockNotAvailableError")
    assert note == bot_module.MSG_ALBUM_COLUMNS_FAILED.format(problems=problem)
    assert "Kosmetyka towarzyszy działa normalnie" in note and "porzucenie jest ODRZUCANE" in note
    assert "WYŁĄCZONA" not in note and "Baza danych różni się" not in note
    assert await pets.cosmetics_available(bot) is True and await pets.frames_available(bot) is True

    bot._startup_notes = []                                                    # (told once connected)
    await bot.setup_hook()                                                     # the next start: added, nothing to say
    assert bot._startup_notes == [] and await schema.verify(db) == []


def test_start_up_note_says_only_what_is_really_missing():
    note = bot_module.new_columns_note
    missing = schema.missing_column
    every = [missing(table, column) for table, column, _ in schema.new_columns()]
    album_only = [missing("pet_album", "frame"), missing("pet_album", "holo_partial")]
    assert note(["x"], every) == bot_module.MSG_NEW_COLUMNS_FAILED.format(problems="x")   # the worst case wins
    assert note(["x", "y"], [missing("pets", "frame"), *album_only]) == (
        bot_module.MSG_FRAME_COLUMNS_FAILED.format(problems="x; y"))
    assert note(["x"], album_only) == bot_module.MSG_ALBUM_COLUMNS_FAILED.format(problems="x")
    assert note(["x"], []) is None                                                       # all there after all
    assert note(["x"], ["tower: brak kolumny time"]) is None                             # not a new column


async def test_owner_is_told_at_start_up_when_cards_cannot_be_composed(game_db, monkeypatch):
    """A machine without Pillow shows every pet plain: that must not go unnoticed."""
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    assert petcards.pillow_available() is True
    await bot.setup_hook()
    assert bot._startup_notes == []                                            # Pillow is there: nothing to say

    monkeypatch.setattr(petcards, "Image", None)
    assert petcards.pillow_available() is False
    await bot.setup_hook()
    assert bot._startup_notes == [bot_module.MSG_NO_PILLOW]
    assert "pip install -r requirements.txt" in bot_module.MSG_NO_PILLOW and "\\\\" not in bot_module.MSG_NO_PILLOW
    await bot.on_ready()
    assert "Brak biblioteki Pillow" in game_db.log_channel.messages[-1].content


async def test_owner_is_told_when_the_database_was_down_at_start_up(game_db, monkeypatch):
    """No connection at start-up: nothing was verified and no column added - the owner hears why."""
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))

    async def refused(self):
        raise ConnectionRefusedError("the database is restarting")

    monkeypatch.setattr(type(db), "connect", refused)
    await bot.setup_hook()                                                     # starts all the same
    assert bot._startup_notes == [bot_module.MSG_DATABASE_DOWN_AT_START.format(error="ConnectionRefusedError")]
    assert_database_down_note_is_truthful(bot._startup_notes[0])
    await bot.on_ready()
    assert "Baza danych nie odpowiadała przy starcie bota (ConnectionRefusedError)" in (
        game_db.log_channel.messages[-1].content)


def assert_database_down_note_is_truthful(note: str) -> None:
    """Nothing was checked, so the note may not say WHAT is off - only what each missing piece means.

    Production has had SEAL / HOLO since 2026-11-20: the likely case is that only the frame
    columns or the album are missing, and then the seals and the full Holo DO work.
    """
    assert "nie sprawdził jej struktury" in note and "mógł nie dodać tabel ani kolumn nowych w v2" in note
    assert "Jeśli czegoś z tego brakuje" in note                              # conditional, not "is off"
    assert "kosmetyka towarzyszy (pieczęcie, folia Holo) jest wyłączona do" not in note   # the old, flat claim
    assert "bez kolumn SEAL / HOLO tabeli PETS cała kosmetyka towarzyszy" in note
    assert "bez samych kolumn FRAME / HOLO_PARTIAL pieczęcie i pełna folia Holo działają" in note
    assert "nowe ramki i częściowa folia Holo nie są losowane ani zapisywane" in note
    # keep() makes a missing PET_ALBUM at the first sacrifice; until then the album reads as empty.
    assert "bez tabeli PET_ALBUM album nie działa" not in note and "nie mogą porzucić towarzysza" not in note
    assert ("tabelę PET_ALBUM bot utworzy sam przy pierwszym porzuceniu towarzysza przez wspierającego "
            "(do tego czasu album jest pusty)") in note
    assert "porzucenie towarzysza z rzadką ramką albo częściową folią Holo" in note
    assert "Reszta gry działa normalnie" in note


async def test_database_down_note_when_verify_fails_after_the_columns_could_not_be_added(game_db, monkeypatch):
    """The real upgrade (only FRAME / HOLO_PARTIAL missing), the ALTER fails and then the check fails too:
    one note, the one that says nothing was checked - and it does not claim the seals are off."""
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    await v2_database(db)

    async def gone(db):
        raise ConnectionResetError("the database went away")

    monkeypatch.setattr(schema, "verify", gone)
    async with db.transaction() as somebody:
        await somebody.execute("LOCK TABLE PETS IN ACCESS SHARE MODE")
        await bot.setup_hook()
    assert bot._startup_notes == [bot_module.MSG_DATABASE_DOWN_AT_START.format(error="ConnectionResetError")]
    assert_database_down_note_is_truthful(bot._startup_notes[0])
    assert await pets.cosmetics_available(bot) is True and await pets.frames_available(bot) is False


async def test_a_feature_module_that_cannot_be_imported_is_not_called_a_database_outage(game_db, monkeypatch):
    """``altermmo.features.album`` broken: the owner hears that the code is broken and what really is
    off in the database (here: the frame columns of PETS were never added) - never "the database did
    not answer" (the import error used to escape into setup_hook's database except)."""
    import sys

    import altermmo.features as features_package

    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    await v2_database(db)                                                      # FRAME / HOLO_PARTIAL missing
    monkeypatch.setitem(sys.modules, "altermmo.features.album", None)          # import -> ModuleNotFoundError
    monkeypatch.delattr(features_package, "album", raising=False)

    await bot.setup_hook()
    notes = bot._startup_notes
    assert not any(note.startswith("Baza danych nie odpowiadała") for note in notes)
    [columns] = [note for note in notes if note.startswith("Nie udało się dodać do bazy danych kolumn")]
    problem = columns.removeprefix("Nie udało się dodać do bazy danych kolumn nowych w v2: ").split(". Pieczęcie")[0]
    assert problem.startswith("ModuleNotFoundError")
    assert columns == bot_module.MSG_FRAME_COLUMNS_FAILED.format(problems=problem)   # what is really off
    [broken] = [note for note in notes if note.startswith("Nie udało się wczytać części kodu bota")]
    assert "altermmo.features.album" in broken
    assert not any(note.startswith("Baza danych różni się") for note in notes)  # the frame columns: explained above
    assert len(notes) == 2


def test_start_up_note_copes_with_a_feature_module_that_cannot_be_imported(monkeypatch):
    import sys

    import altermmo.features as features_package

    missing = schema.missing_column
    every = schema.new_columns()
    assert bot_module._importable_new_columns() == every                       # everything importable: the same
    monkeypatch.setitem(sys.modules, "altermmo.features.album", None)
    with pytest.raises(ImportError):
        schema.new_columns()                                                   # (what used to escape)
    assert bot_module._importable_new_columns() == [column for column in every if column[0] != "pet_album"]
    monkeypatch.delattr(features_package, "album", raising=False)
    album_only = [missing("pet_album", "frame"), missing("pet_album", "holo_partial")]
    assert bot_module.new_columns_note(["x"], [missing("pets", "frame")]) == (
        bot_module.MSG_FRAME_COLUMNS_FAILED.format(problems="x"))
    assert bot_module.new_columns_note(["x"], album_only) is None              # the album's own note says it
    monkeypatch.setitem(sys.modules, "altermmo.features.pets", None)
    monkeypatch.delattr(features_package, "pets", raising=False)
    assert bot_module.new_columns_note(["x"], [missing("pets", "frame")]) is None   # no pet works: module note


# -- the game without the columns (the migration failed) ----------------------------------

async def test_game_goes_on_without_cosmetics_when_the_columns_are_missing(game_db, monkeypatch, cards, cog, caplog):
    db, bot = game_db.bot.db, game_db.bot
    await old_database(db)
    boss_admin, player = admin(game_db), crafter(game_db)
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 0, 0)                                            # the best seal and Holo, if rolled

    with caplog.at_level(logging.WARNING, logger="altermmo.features.pets"):
        assert await pets.assign_pet(game_db.scene(), player) is True          # the egg is given all the same
    assert asked == [] and bot.owner_log == []                                 # nothing rolled, nothing reported
    assert sum("pet cosmetics are off" in record.message for record in caplog.records) == 1
    row = await pet_row(db, 1)
    assert "seal" not in row and "holo" not in row and row["pet_lvl"] == 0
    assert await pets.generate_pet_egg(game_db.scene(), player) == 2 and asked == []
    assert await pets.cosmetics_available(bot) is False

    # Pets are shown as before: an egg, then the hatched pet - a card without a seal or Holo.
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["0.png"] and [call.look.plain for call in cards] == [True]
    assert await pets.level_up_pet(game_db.scene(), player.id) is True
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert (cards[-1].look.seal, cards[-1].look.foil, cards[-1].look.plain) == (0, petcards.FOIL_NONE, True)
    assert message.files == ["0.png"] and "Pieczęć" not in message.embeds[0].description
    assert "Wygląd: Standard\n\n" in message.embeds[0].description

    # The rest of the pet game does not notice.
    assert (await pets.get_pet_skills(bot, player.id))["CRIT_PERC"] >= 0
    await pets.pet_ranking(game_db.scene(player))
    assert game_db.channel.messages[-1].embeds[0].title == "Najbardziej utalentowani towarzysze!"
    assert await pets.check_stable(game_db.scene(player)) is True
    assert await pets.store_pet(game_db.scene(player), 1) is True
    assert await pets.unstore_pet(game_db.scene(player), 1) is True
    assert await pets.name_pet(game_db.scene(player), "Burek") is True

    # The owner's command says why it cannot work ...
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 tak")
    assert game_db.channel.messages[-1].content == pets_cog.MSG_SEAL_NO_COLUMNS
    assert await pets.set_cosmetics(bot, player.id, 3, True) == ("no_columns", None)
    # ... and works as soon as the columns are there - without a restart.
    assert await schema.add_new_columns(db) == []
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 tak")
    assert game_db.channel.messages[-1].content == pets_cog.MSG_SEAL_SET.format(
        player_id=player.id, pet_id=1, seal="Arcyłowcy", holo="tak")
    assert await pets.cosmetics_available(bot) is True and await cosmetics(db, 1) == (3, True)


async def test_egg_is_stored_even_when_the_columns_vanished_after_the_bot_looked(game_db, monkeypatch, caplog):
    db, bot = game_db.bot.db, game_db.bot
    await old_database(db)
    bot._pet_cosmetics = True                                                  # they were there at start-up
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 0, 0)
    player = crafter(game_db)
    await add_owner(db, player.id, 0, reroll_scroll=4)
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        assert await pets.assign_pet(game_db.scene(), player) is True
    assert len(asked) == 2                                                     # rolled, but there is nowhere to keep it
    owner = await owner_row(db, player.id)
    assert (owner["pet_id"], owner["reroll_scroll"]) == (1, 4)                 # the whole transaction went through
    assert (await pet_row(db, 1))["pet_name"] == "Towarzysz" and await count(db, "PETS") == 1
    assert any("PETS has no SEAL / HOLO columns: these cosmetics of pet egg 1 are lost "
               "({'SEAL': 3, 'HOLO': True})" in record.message for record in caplog.records)
    assert bot.owner_log == []                                                 # nothing rare was kept
    assert await pets.cosmetics_available(bot) is False                        # the bot knows now

    asked.clear()
    assert await pets.generate_pet_egg(game_db.scene(), player) == 2 and asked == []


async def test_egg_is_stored_when_only_the_frame_columns_vanished(game_db, monkeypatch, caplog):
    """Both were there when the bot looked; FRAME / HOLO_PARTIAL are gone: the seal and Holo are kept."""
    db, bot = game_db.bot.db, game_db.bot
    await v2_database(db)
    bot._pet_cosmetics = bot._pet_frames = True
    rig(monkeypatch, randint=lambda low, high: 905 if high == 1000 else 0)     # a shiny egg
    asked = dice(monkeypatch, 0, 0, 9999)                                      # Arcyłowcy, Holo, R7-A
    player = crafter(game_db)
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        assert await pets.assign_pet(game_db.scene(), player) is True
    assert len(asked) == 3                                                     # rolled, the frame has nowhere to go
    row = await pet_row(db, 1)
    assert (row["seal"], row["holo"]) == (3, True) and "frame" not in row
    assert any("PETS has no FRAME / HOLO_PARTIAL columns: these cosmetics of pet egg 1 are lost "
               "({'FRAME': 'R7-A', 'HOLO_PARTIAL': False})" in record.message for record in caplog.records)
    # The owner hears about what was kept - not about the lost frame.
    assert bot.owner_log == [f"Rzadka kosmetyka towarzysza: jajko gracza <@{player.id}> (PET_ID 1) ma "
                             "pieczęć Arcyłowcy i folię Holo."]
    assert await pets.cosmetics_available(bot) is True and await pets.frames_available(bot) is False

    asked.clear()                                                              # the next egg: no frame roll
    assert await pets.generate_pet_egg(game_db.scene(), player) == 2 and asked == [(1000,), (1000,)]


async def test_game_goes_on_with_the_old_cosmetics_when_only_the_frame_columns_are_missing(
        game_db, monkeypatch, cards, cog):
    """The v2 table of 2026-11-20 (the frame columns could not be added): seals and full Holo as on that day."""
    db, bot = game_db.bot.db, game_db.bot
    await v2_database(db)
    boss_admin, player = admin(game_db), crafter(game_db)
    rig(monkeypatch, randint=lambda low, high: 985 if high == 1000 else 0)     # an ultra shiny egg
    asked = dice(monkeypatch, 30, 0, 9999)
    assert await pets.assign_pet(game_db.scene(), player) is True
    assert asked == [(1000,), (1000,)] and await cosmetics(db, 1) == (2, True)
    assert (await pets._cosmetic_state(bot)) == (True, False)

    assert await pets.show_pet(game_db.scene(player), player) is True
    assert cards[-1].look == petcards.Look(frame="U0", seal=2, foil=FULL)      # the base frame of the class
    assert "Pieczęć: Łowcy\nFolia: Holo\n" in last_embed(game_db).description and "Ramka" not in (
        last_embed(game_db).description)

    def said() -> str:
        return game_db.channel.messages[-1].content

    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 tak")           # the old command works ...
    assert await cosmetics(db, 1) == (3, True) and "folia Holo - **tak**" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 częściowa")     # ... a partial Holo cannot be kept
    assert said() == pets_cog.MSG_FRAME_NO_COLUMNS and await cosmetics(db, 1) == (3, True)
    await run(game_db, boss_admin, f"$DajRamke {player.id} U1")
    assert said() == pets_cog.MSG_FRAME_NO_COLUMNS
    assert await pets.set_frame(bot, player.id, "U1") == ("no_frame_columns", None)

    # The columns are added (by hand, or at the next start): the commands work without a restart.
    assert await schema.add_new_columns(db) == []
    await run(game_db, boss_admin, f"$DajRamke {player.id} U1")
    assert (await pet_row(db, 1))["frame"] == "U1" and await pets.frames_available(bot) is True
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 częściowa")
    assert await everything(db, 1) == (3, False, "U1", True)


async def test_only_a_missing_column_is_forgiven(game_db, monkeypatch):
    """Any other refusal of the INSERT is an error as before - and nothing of the egg is kept."""
    db = game_db.bot.db
    await db.execute("ALTER TABLE PETS ADD CONSTRAINT no_big_seal CHECK (SEAL < 3)")
    rig(monkeypatch, randint=0)
    dice(monkeypatch, 0, 500)
    player = crafter(game_db)
    with pytest.raises(asyncpg.CheckViolationError):
        await pets.assign_pet(game_db.scene(), player)
    assert await count(db, "PETS") == 0 and await owner_row(db, player.id) is None


# -- $DajPieczec ------------------------------------------------------------------------

async def test_admin_sets_the_cosmetics_of_the_active_pet(game_db, cog, cards):
    db = game_db.bot.db
    boss_admin, player, lonely = admin(game_db), game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, player.id, 5, pet_id_alt1=6)
    await add_pet(db, 5, pet_lvl=1, variant="1")
    await add_pet(db, 6, pet_lvl=1)                                            # in the stable: never touched
    await add_owner(db, lonely.id, 0)

    def said() -> str:
        return game_db.channel.messages[-1].content

    await run(game_db, player, f"$DajPieczec {player.id} 3 tak")               # not an administrator: nothing
    assert await cosmetics(db, 5) == (0, False)

    set_text = (f"Towarzysz gracza <@{player.id}> (PET_ID 5) ma teraz: pieczęć - **Łowcy**, folia Holo - "
                "**nie**. Widać to na karcie towarzysza (**$towarzysz**).")
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 2")
    assert await cosmetics(db, 5) == (2, False)
    # The player does not support: it is stored, and the administrator is told why nobody sees it.
    assert said() == set_text + pets_cog.MSG_SEAL_NOT_SHOWN and "nie ma teraz rangi wspierającego" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 0")               # nothing to hide: no warning
    assert said().endswith("(**$towarzysz**).") and await cosmetics(db, 5) == (0, False)
    player.roles.append(FakeRole(patrons.YOUTUBE_ROLES[1]))                    # a supporter from now on
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 2")
    assert said() == set_text and await cosmetics(db, 5) == (2, False)
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 TAK")
    assert await cosmetics(db, 5) == (3, True) and "pieczęć - **Arcyłowcy**, folia Holo - **tak**" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 1")               # Holo left out: unchanged
    assert await cosmetics(db, 5) == (1, True) and "pieczęć - **Tropiciela**, folia Holo - **tak**" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 0 nie")
    assert await cosmetics(db, 5) == (0, False) and "pieczęć - **brak**, folia Holo - **nie**" in said()
    assert await cosmetics(db, 6) == (0, False)

    # What it set is what the player sees.
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 tak")
    await pets.show_pet(game_db.scene(player), player)
    assert game_db.channel.messages[-1].files == ["card-1-bare-3-holo.gif"]
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in last_embed(game_db).description

    # Polite refusals; nothing changes.
    for tier in ("4", "-1"):
        await run(game_db, boss_admin, f"$DajPieczec {player.id} {tier}")
        assert said() == "Pieczęć: 0-3 (0 = brak, 1 = Tropiciela, 2 = Łowcy, 3 = Arcyłowcy)."
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 2 może")
    assert said() == (f"Folia Holo: **tak** (pełna), **częściowa** albo **nie** (pominięta - bez zmian), np. "
                      f"**$DajPieczec {player.id} 3 tak**.")
    for nobody in (lonely.id, 12345):                                          # no active pet / no row at all
        await run(game_db, boss_admin, f"$DajPieczec {nobody} 2")
        assert said() == f"<@{nobody}> nie ma przy sobie towarzysza - nie ma komu dać pieczęci."
    await run(game_db, boss_admin, "$DajPieczec")                              # bad arguments: a usage hint
    assert said().startswith("Niepoprawne użycie komendy.")
    await run(game_db, boss_admin, f"$DajPieczec {player.id} dużo")
    assert said().startswith("Niepoprawne użycie komendy.")
    assert await cosmetics(db, 5) == (3, True)


async def test_set_cosmetics_statuses(game_db):
    db, bot = game_db.bot.db, game_db.bot
    await add_owner(db, 1, 5)
    await add_pet(db, 5, seal=1, holo=True)                                    # an egg can be given a seal too
    assert await pets.set_cosmetics(bot, 1, 2) == ("ok", {"pet_id": 5, "seal": 2, "holo": True, "foil": FULL})
    assert await pets.set_cosmetics(bot, "1", "3", False) == (
        "ok", {"pet_id": 5, "seal": 3, "holo": False, "foil": NONE})
    # The foil is one of three: setting one clears the other flag.
    assert await pets.set_cosmetics(bot, 1, 3, PARTIAL) == ("ok", {"pet_id": 5, "seal": 3, "holo": False,
                                                                   "foil": PARTIAL})
    assert await everything(db, 5) == (3, False, "", True)
    assert await pets.set_cosmetics(bot, 1, 3, None) == ("ok", {"pet_id": 5, "seal": 3, "holo": False,
                                                                "foil": PARTIAL})    # left out: unchanged
    assert (await pets.set_cosmetics(bot, 1, 3, True))[1]["foil"] == FULL
    assert await everything(db, 5) == (3, True, "", False)
    assert (await pets.set_cosmetics(bot, 1, 3, PARTIAL))[1]["foil"] == PARTIAL
    assert (await pets.set_cosmetics(bot, 1, 0, NONE))[1] == {"pet_id": 5, "seal": 0, "holo": False, "foil": NONE}
    assert await everything(db, 5) == (0, False, "", False)
    for bad in (4, -1, "x", None):
        assert await pets.set_cosmetics(bot, 1, bad) == ("bad_seal", None)
    for bad in (3, -1, "x"):
        assert await pets.set_cosmetics(bot, 1, 1, bad) == ("bad_foil", None)
    assert await pets.set_cosmetics(bot, 2, 1) == ("alone", None)
    await db.execute("DELETE FROM PETS")                                       # the owner row points nowhere
    assert await pets.set_cosmetics(bot, 1, 1) == ("alone", None)


# -- $DajRamke --------------------------------------------------------------------------

async def test_admin_sets_the_frame_of_the_active_pet(game_db, cog, cards):
    db = game_db.bot.db
    boss_admin, player, lonely = admin(game_db), game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, player.id, 5, pet_id_alt1=6)
    await add_pet(db, 5, pet_lvl=1, variant="1", shiny=True)
    await add_pet(db, 6, pet_lvl=1, shiny=True)                                # in the stable: never touched
    await add_owner(db, lonely.id, 0)

    def said() -> str:
        return game_db.channel.messages[-1].content

    async def frame() -> str:
        return (await pet_row(db, 5))["frame"]

    await run(game_db, player, f"$DajRamke {player.id} S1-A")                  # not an administrator: nothing
    assert await frame() == ""

    await run(game_db, boss_admin, f"$DajRamke {player.id} s1-a")              # any case
    assert await frame() == "S1-A"
    # He does not support: stored, and the administrator is told why nobody sees it.
    assert said() == (f"Towarzysz gracza <@{player.id}> (PET_ID 5) ma teraz ramkę **Gwiazda Szronu (animowana)**. "
                      "Widać to na karcie towarzysza (**$towarzysz**)." + pets_cog.MSG_FRAME_NOT_SHOWN)
    player.roles.append(FakeRole(patrons.PATRON_ROLES[1]))
    await run(game_db, boss_admin, f"$DajRamke {player.id} R7")
    assert await frame() == "R7" and said().endswith("ramkę **Przeszywana skóra**. Widać to na karcie towarzysza "
                                                     "(**$towarzysz**).")
    await pets.show_pet(game_db.scene(player), player)                         # what it set is what he sees
    assert cards[-1].look == petcards.Look(frame="R7") and "Ramka: Przeszywana skóra\n" in last_embed(
        game_db).description

    # Back to the base of the class - by a word or by its id; stored as ''.
    for word in ("baza", "S0", "podstawowa"):
        await run(game_db, boss_admin, f"$DajRamke {player.id} R2")
        await run(game_db, boss_admin, f"$DajRamke {player.id} {word}")
        assert await frame() == "" and "ramkę **Srebrne okucia (podstawowa)**" in said()
    player.roles.clear()
    await run(game_db, boss_admin, f"$DajRamke {player.id} baza")              # the base is seen by everybody
    assert not said().endswith(pets_cog.MSG_FRAME_NOT_SHOWN)

    # A frame of another class, or nonsense: refused, with the pool of this pet's class.
    for wrong in ("U1", "R9-A", "X1", "S1-AA"):
        await run(game_db, boss_admin, f"$DajRamke {player.id} {wrong}")
        assert said() == (f"Ramki **{wrong}** nie ma w puli tego towarzysza (klasa: Shiny). Możliwe: "
                          "**S0** (Srebrne okucia (podstawowa)), **S1** (Gwiazda Szronu), "
                          "**S1-A** (Gwiazda Szronu (animowana)), **R1** (Księżycowa stal), "
                          "**R1-A** (Księżycowa stal (animowana)), **R2** (Miedziane ciernie), "
                          "**R2-A** (Miedziane ciernie (animowana)), **R7** (Przeszywana skóra), "
                          "**R7-A** (Przeszywana skóra (animowana)) - albo **baza** (podstawowa ramka klasy), "
                          f"np. **$DajRamke {player.id} R7-A**.")
        assert await frame() == ""
    assert (await pet_row(db, 6))["frame"] == ""

    # An ordinary pet has no frame; nobody to give it to.
    await db.execute("UPDATE PETS SET SHINY = FALSE WHERE PET_ID = 5")
    await run(game_db, boss_admin, f"$DajRamke {player.id} S1")
    assert said() == (f"Towarzysz gracza <@{player.id}> (PET_ID 5) jest zwykły - ramki mają tylko "
                      "towarzysze Shiny, Ultra Shiny i mityczni.")
    for nobody in (lonely.id, 12345):
        await run(game_db, boss_admin, f"$DajRamke {nobody} S1")
        assert said() == f"<@{nobody}> nie ma przy sobie towarzysza - nie ma komu dać ramki."
    await run(game_db, boss_admin, "$DajRamke")                                # bad arguments: a usage hint
    assert said().startswith("Niepoprawne użycie komendy.")
    assert await frame() == ""


@pytest.mark.parametrize("looks, label", [
    ({"shiny": True}, "Shiny"),
    ({"shiny": True, "ultra_shiny": True}, "Ultra Shiny"),
    ({"shiny": True, "ultra_shiny": True, "mythic": True, "quality": "Mythic", "type": "Void"}, "Mityczny"),
])
async def test_a_wrong_frame_names_the_class_as_a_label(game_db, cog, looks, label):
    """"klasa" is feminine: the class is named as its label after a colon, never "klasa mityczny"."""
    db = game_db.bot.db
    boss_admin, player = admin(game_db), game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=1, **looks)
    await run(game_db, boss_admin, f"$DajRamke {player.id} X1")
    said = game_db.channel.messages[-1].content
    assert said.startswith(f"Ramki **X1** nie ma w puli tego towarzysza (klasa: {label}). Możliwe: "), said
    assert "klasa mityczn" not in said.lower()


async def test_set_frame_statuses(game_db):
    db, bot = game_db.bot.db, game_db.bot
    await add_owner(db, 1, 5)
    await add_pet(db, 5, shiny=True, ultra_shiny=True, mythic=True, quality="Mythic", type="Void")
    assert await pets.set_frame(bot, 1, "R9-A") == ("ok", {"pet_id": 5, "tier": "M", "frame": "R9-A"})
    assert await pets.set_frame(bot, "1", " r5 ") == ("ok", {"pet_id": 5, "tier": "M", "frame": "R5"})
    assert await pets.set_frame(bot, 1, "M0") == ("ok", {"pet_id": 5, "tier": "M", "frame": ""})
    assert await pets.set_frame(bot, 1, "R1") == ("bad_frame", {"pet_id": 5, "tier": "M"})
    assert await pets.set_frame(bot, 1, None) == ("bad_frame", {"pet_id": 5, "tier": "M"})
    await db.execute("UPDATE PETS SET MYTHIC = FALSE")
    assert await pets.set_frame(bot, 1, "U1-A") == ("ok", {"pet_id": 5, "tier": "U", "frame": "U1-A"})
    await db.execute("UPDATE PETS SET ULTRA_SHINY = FALSE, SHINY = FALSE")
    assert await pets.set_frame(bot, 1, "S1") == ("no_tier", {"pet_id": 5, "tier": ""})
    assert (await pet_row(db, 5))["frame"] == "U1-A"                           # refused: nothing written
    assert await pets.set_frame(bot, 2, "S1") == ("alone", None)
    assert all(len(frame.id) <= 16 for frame in petcards.FRAMES.values())     # FRAME is VARCHAR(16)


async def test_admin_sets_a_partial_holo(game_db, cog, cards):
    db = game_db.bot.db
    boss_admin, player = admin(game_db), crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=1, variant="1", holo=True)

    def said() -> str:
        return game_db.channel.messages[-1].content

    for word, stored, answer in (("częściowa", (1, False, "", True), "częściowa"), ("tak", (1, True, "", False), "tak"),
                                 ("czesciowe", (1, False, "", True), "częściowa"), ("pełna", (1, True, "", False), "tak"),
                                 ("brak", (1, False, "", False), "nie")):
        await run(game_db, boss_admin, f"$DajPieczec {player.id} 1 {word}")
        assert await everything(db, 5) == stored
        assert f"pieczęć - **Tropiciela**, folia Holo - **{answer}**" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 0 częściowa")
    await pets.show_pet(game_db.scene(player), player)
    assert cards[-1].look == petcards.Look(foil=PARTIAL) and "Folia: Holo częściowe\n" in last_embed(
        game_db).description


# -- copies of the database -------------------------------------------------------------

async def test_backup_copies_the_table_with_and_without_the_columns(db, tmp_path):
    """The copy lists the columns the table really has, so both shapes are copied as they are."""
    for folder in ("data", "new", "old"):
        (tmp_path / folder).mkdir()
    await add_pet(db, 5, seal=2, holo=True, shiny=True, frame="S1-A", holo_partial=True)
    made = await backup.create_backup(db, tmp_path / "data", tmp_path / "new", tables=("pets",))
    with zipfile.ZipFile(made.path) as archive:
        header, row = archive.read("db/pets.csv").decode("utf-8").splitlines()
        assert header.split(",")[-5:] == ["mythic", "seal", "holo", "frame", "holo_partial"]
        assert row.split(",")[-4:] == ["2", "t", "S1-A", "t"]
        assert "SEAL NUMERIC NOT NULL DEFAULT 0" in archive.read("schema.sql").decode("utf-8")
        assert "FRAME VARCHAR(16) NOT NULL DEFAULT ''" in archive.read("schema.sql").decode("utf-8")
    assert made.tables[0].columns[-4:] == ("seal", "holo", "frame", "holo_partial")

    await old_database(db)
    await add_pet(db, 6)
    made = await backup.create_backup(db, tmp_path / "data", tmp_path / "old", tables=("pets",))
    with zipfile.ZipFile(made.path) as archive:
        header = archive.read("db/pets.csv").decode("utf-8").splitlines()[0]
        assert header.split(",")[-1] == "mythic" and "seal" not in header
    assert made.tables[0].rows == 1 and "seal" not in made.tables[0].columns
