"""features/pets.py + cogs/pets.py: pets against a real PostgreSQL and the fake Discord."""

from __future__ import annotations

import asyncio
import random
import time
import types
from decimal import Decimal

import discord
import pytest
import pytest_asyncio
from discord.ext import commands

from altermmo.core import guards
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import bossdetect, patrons, pets
from altermmo.features.pets import PetType
from altermmo.settings import ASSETS_DIR

from fakes import FakeMember, FakeMessage, FakeRole

PATRON = patrons.PATRON_ROLES[0]
ASK = "albo wpisz **!potwierdzam**."
ALONE = "Niestety jesteś sam jak palec na tym świecie"
PEPOG = "<:PepoG:790963160528977980>"
ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN = pets.SKILL_EMOTES
ACCEPT = pets.ACCEPT_EMOTE

OWNER_DEFAULTS = dict(pet_owned=True, reroll_scroll=0, reroll_scroll_shard=0, rebirth_stones=0, mirrors=0,
                      pet_id_alt1=0, pet_id_alt2=0, skill_gem=0, pet_id_alt3=0, pet_id_alt4=0)
PET_DEFAULTS = dict(pet_name="Towarzysz", pet_lvl=0, pet_skills=0, quality="Standard", shiny=False,
                    type="Cat", variant="0", crit_perc=0, replace_perc=0, def_perc=0, drop_perc=0,
                    lowhp_perc=0, slow_perc=0, init_perc=0, detection=False, ultra_shiny=False, mythic=False)


# -- helpers --------------------------------------------------------------------------

async def _insert(db, table: str, values: dict) -> None:
    marks = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    await db.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({marks})", *values.values())


async def add_owner(db, player_id: int, pet_id: int = 0, **columns) -> None:
    await _insert(db, "PETOWNER", {"player_id": player_id, "pet_id": pet_id, **OWNER_DEFAULTS, **columns})


async def add_pet(db, pet_id: int, **columns) -> None:
    await _insert(db, "PETS", {"pet_id": pet_id, **PET_DEFAULTS, **columns})


def _plain(row) -> dict | None:
    if row is None:
        return None
    return {key: int(value) if isinstance(value, Decimal) else value for key, value in row.items()}


#: The PETOWNER columns of the wandering merchant's goods (new in v2) in a fresh row.
MERCHANT_DEFAULTS = {**{f"pet_id_alt{place}": 0 for place in range(5, 11)}, "stable_places": 4,
                     "egg_pouch": False, "pouch_pet_id": 0,
                     # ... and of the companion missions (2026-11-25): the Duża sakwa, the summoning scrolls
                     "big_pouch": False, "pouch_pet_id2": 0, "summon_scrolls": 0}


async def owner_row(db, player_id: int) -> dict | None:
    return _plain(await db.fetchrow("SELECT * FROM PETOWNER WHERE PLAYER_ID = $1", player_id))


async def pet_row(db, pet_id: int) -> dict | None:
    return _plain(await db.fetchrow("SELECT * FROM PETS WHERE PET_ID = $1", pet_id))


async def count(db, table: str) -> int:
    return await db.fetchval(f"SELECT COUNT(*) FROM {table}")


async def until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the expected thing never happened"
        await asyncio.sleep(0.005)


def rig(monkeypatch, *, choices=(), randint=None):
    """Deterministic dice.  Returns (pools offered to ``choice``, ranges asked from ``randint``)."""
    pools: list[list] = []
    ranges: list[tuple[int, int]] = []
    script = iter(choices)

    def choice(pool):
        pool = list(pool)
        pools.append(pool)
        wanted = next(script, None)
        return wanted if wanted in pool else pool[0]

    def rand(low, high):
        ranges.append((low, high))
        if callable(randint):
            return randint(low, high)
        return high if randint is None else randint

    monkeypatch.setattr(random, "choice", choice)
    monkeypatch.setattr(random, "randint", rand)
    return pools, ranges


def confirms(game, player, text: str = "!potwierdzam") -> None:
    """``player`` types ``text`` whenever the bot asks for a confirmation."""
    game.auto_answer(player, responder=lambda message: text if ASK in message.content else None)


def last_embed(game) -> discord.Embed:
    return next(m for m in reversed(game.channel.messages) if m.embeds).embeds[0]


def patron(game, name: str = "Patron") -> FakeMember:
    return game.add_member(name, roles=[PATRON])


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")


class DatabaseDown:
    """A database that cannot be reached at all."""

    def _fail(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    transaction = _fail

    async def execute(self, *args, **kwargs):
        self._fail()

    fetch = fetchrow = fetchval = execute


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a !potwierdzam slot was never released"


@pytest.fixture(autouse=True)
def plain_cards(monkeypatch):
    """No card is composed here: every pet is sent as its plain picture.

    The cards of hatched pets (frame, seal, Holo) - which file is sent, under which name -
    are tested in test_pet_cosmetics.py; these tests are about everything else.
    """
    async def card(source, look, cache_dir, *, max_bytes=None):
        return source

    monkeypatch.setattr(pets.petcards, "card", card)


@pytest.fixture
def sacrifices(monkeypatch):
    """Record pet sacrifices instead of drawing a real boss modifier."""
    calls: list = []

    async def random_modifiers(scene, msg=True, sacrefice=False):
        calls.append((scene.author, msg, sacrefice))
        return "points_boost"

    monkeypatch.setattr(pets.modifiers, "random_modifiers", random_modifiers)
    return calls


# -- game rules (no database) ---------------------------------------------------------

def test_pet_types_and_pools_are_the_legacy_ones():
    assert [member.value for member in PetType] == [
        "Bear", "Boar", "Cat", "Rabbit", "Sheep", "Dragon", "Phoenix", "Unicorn", "Snake", "AnimeGirl",
        "Elemental", "Dog", "Guinea", "Skeleton", "Monkey", "Eagle", "Ghost", "Void"]
    assert [t.value for t in pets.STANDARD_EGGS] == ["Bear", "Boar", "Cat", "Rabbit", "Sheep", "Dog", "Eagle"]
    assert [t.value for t in pets.PATRON_EGGS] == [
        "Bear", "Boar", "Cat", "Rabbit", "Sheep", "Dog", "Eagle", "Dragon", "Ghost", "Phoenix", "Unicorn",
        "Snake", "AnimeGirl", "Elemental", "Guinea", "Skeleton", "Monkey"]
    assert [t.value for t in pets.TRANSFORM_TYPES] == [
        "Bear", "Boar", "Cat", "Rabbit", "Sheep", "Dragon", "Phoenix", "Unicorn", "Snake", "AnimeGirl",
        "Elemental", "Monkey", "Dog", "Eagle", "Guinea", "Skeleton", "Ghost"]
    assert pets.SKILL_POOL == ("CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC",
                               "SLOW_PERC", "DETECTION")
    assert pets.SKILL_EMOTES == tuple(f"{n}️⃣" for n in range(1, 8)) and ACCEPT == "✅"
    assert set(pets.POLISH_TYPES) == {member.value for member in PetType}


def test_every_pet_and_egg_picture_exists():
    for pet_type in pets.PATRON_EGGS:
        for number in (0, 1, 10, 11, 100, 101):
            assert (ASSETS_DIR / "pets" / pet_type.value / f"{number}.png").is_file()
    for pet_type in pets.PREMIUM_TYPES:
        for variant in (0, 1):
            assert (ASSETS_DIR / "eggs" / "premium" / pet_type.value / f"{variant}.png").is_file()
    for variant in range(11):
        assert (ASSETS_DIR / "eggs" / "standard" / f"{variant}.png").is_file()
    assert (ASSETS_DIR / "eggs" / "mythic" / "Void" / "0.gif").is_file()
    for number in (0, 10, 100):
        assert (ASSETS_DIR / "pets" / "Void" / f"{number}.gif").is_file()


@pytest.mark.parametrize("roll, shiny, ultra, mythic", [
    (0, False, False, False), (899, False, False, False), (900, True, False, False),
    (979, True, False, False), (980, True, True, False), (997, True, True, False),
    (998, True, True, True), (1000, True, True, True)])
def test_egg_rarity_thresholds(monkeypatch, roll, shiny, ultra, mythic):
    pools, ranges = rig(monkeypatch, randint=lambda low, high: roll if high == 1000 else high)
    egg = pets._roll_egg(FakeMember("Ala"))
    assert (egg["SHINY"], egg["ULTRA_SHINY"], egg["MYTHIC"]) == (shiny, ultra, mythic)
    assert ranges[0] == (0, 1000)
    if mythic:
        assert pools == [[PetType.VOID]] and ranges == [(0, 1000)]
        assert (egg["TYPE"], egg["QUALITY"], egg["VARIANT"]) == ("Void", "Mythic", 0)
    else:
        assert pools == [list(pets.STANDARD_EGGS)] and ranges == [(0, 1000), (0, 10)]
        assert (egg["TYPE"], egg["QUALITY"], egg["VARIANT"]) == ("Bear", "Standard", 10)


def test_only_patrons_find_premium_eggs(monkeypatch):
    supporter = FakeMember("Patron")
    supporter.roles.append(FakeRole(patrons.CRAFTER_ROLE))
    pools, ranges = rig(monkeypatch, choices=[PetType.GHOST], randint=lambda low, high: 0 if high == 1000 else high)
    egg = pets._roll_egg(supporter)
    assert pools == [list(pets.PATRON_EGGS)]
    assert (egg["TYPE"], egg["QUALITY"], egg["VARIANT"]) == ("Ghost", "Premium", 1)
    assert ranges == [(0, 1000), (0, 1)]
    # Somebody without the rank is never offered a premium type; None = no member at all.
    pools.clear()
    assert pets._roll_egg(FakeMember("Ala"))["QUALITY"] == "Standard"
    assert pets._roll_egg(None)["QUALITY"] == "Standard"
    assert pools == [list(pets.STANDARD_EGGS)] * 2


@pytest.mark.parametrize("flags, crit, slow", [
    (dict(mythic=True, ultra_shiny=True, shiny=True), (8, 12), (11, 16)),
    (dict(mythic=False, ultra_shiny=True, shiny=True), (6, 11), (9, 15)),
    (dict(mythic=False, ultra_shiny=False, shiny=True), (5, 8), (8, 12)),
    (dict(mythic=False, ultra_shiny=None, shiny=None), (3, 5), (5, 8)),
])
def test_talent_point_values_depend_on_the_rarity(monkeypatch, flags, crit, slow):
    pools, ranges = rig(monkeypatch, choices=["CRIT_PERC", "SLOW_PERC", "DETECTION"])
    stats = dict.fromkeys(pets.SKILL_POOL, 0)
    stats["DETECTION"] = False
    for _ in range(3):
        pets._roll_skill(stats, pets.SKILL_POOL, flags)
    assert ranges == [crit, slow]                       # detection is learnt, not rolled
    assert stats == {"CRIT_PERC": crit[1], "REPLACE_PERC": 0, "DEF_PERC": 0, "DROP_PERC": 0,
                     "LOWHP_PERC": 0, "SLOW_PERC": slow[1], "DETECTION": True}


def test_known_detection_and_capped_slow_are_never_drawn(monkeypatch):
    pools, _ = rig(monkeypatch)
    plain = dict(mythic=False, ultra_shiny=False, shiny=False)
    stats = dict.fromkeys(pets.SKILL_POOL, 0)
    stats.update(DETECTION=True, SLOW_PERC=80)
    assert pets._roll_skill(stats, pets.SKILL_POOL, plain) == "CRIT_PERC"
    assert pools == [["CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC"]]
    stats["SLOW_PERC"] = 79
    pets._roll_skill(stats, pets.SKILL_POOL, plain)
    assert "SLOW_PERC" in pools[-1] and "DETECTION" not in pools[-1]
    # Nothing left to learn: the legacy loop would have spun for ever.
    assert pets._roll_skill(stats | {"SLOW_PERC": 80}, ["SLOW_PERC", "DETECTION"], plain) is None


# -- schema ---------------------------------------------------------------------------

async def test_schema_is_exactly_the_production_one(game_db):
    db = game_db.bot.db

    async def columns(table: str):
        rows = await db.fetch(
            """SELECT column_name, data_type, character_maximum_length, is_nullable, column_default
               FROM information_schema.columns WHERE table_name = $1 ORDER BY ordinal_position""", table)
        return rows, [(row["column_name"], row["data_type"]) for row in rows]

    rows, shape = await columns("petowner")
    assert shape == [
        ("player_id", "numeric"), ("pet_id", "numeric"), ("pet_owned", "boolean"),
        ("reroll_scroll", "numeric"), ("reroll_scroll_shard", "numeric"), ("rebirth_stones", "numeric"),
        ("mirrors", "numeric"), ("pet_id_alt1", "numeric"), ("pet_id_alt2", "numeric"),
        ("skill_gem", "numeric"), ("pet_id_alt3", "numeric"), ("pet_id_alt4", "numeric"),
        # new in v2 (the wandering merchant of 2026-11-22): stable places 5-10 and the egg pouch
        *((f"pet_id_alt{place}", "numeric") for place in range(5, 11)),
        ("stable_places", "numeric"), ("egg_pouch", "boolean"), ("pouch_pet_id", "numeric"),
        # ... and the companion missions (2026-11-25): the Duża sakwa and the summoning scrolls
        ("big_pouch", "boolean"), ("pouch_pet_id2", "numeric"), ("summon_scrolls", "numeric")]
    merchant = {*(f"pet_id_alt{place}" for place in range(5, 11)), "stable_places", "egg_pouch", "pouch_pet_id"}
    assert merchant == set(pets.UPGRADE_COLUMNS)
    missions = set(pets.MISSION_COLUMNS)
    assert missions == {"big_pouch", "pouch_pet_id2", "summon_scrolls"} and not missions & merchant
    defaults = {row["column_name"]: row["column_default"] for row in rows}
    assert defaults["pet_id_alt3"] == "0" and defaults["pet_id_alt4"] == "0"
    assert all(defaults[f"pet_id_alt{place}"] == "0" for place in range(5, 11)) and defaults["pouch_pet_id"] == "0"
    assert defaults["stable_places"] == "4" and defaults["egg_pouch"] == "false"
    assert defaults["big_pouch"] == "false" and defaults["pouch_pet_id2"] == "0" and defaults["summon_scrolls"] == "0"
    assert all(value is None for name, value in defaults.items()
               if name not in ("pet_id_alt3", "pet_id_alt4", *merchant, *missions))
    assert all(row["is_nullable"] == ("NO" if row["column_name"] in merchant | missions else "YES") for row in rows)

    rows, shape = await columns("pets")
    text = "character varying"
    assert shape == [
        ("pet_id", "numeric"), ("pet_name", text), ("pet_lvl", "numeric"), ("pet_skills", "numeric"),
        ("quality", text), ("shiny", "boolean"), ("type", text), ("variant", text),
        ("crit_perc", "numeric"), ("replace_perc", "numeric"), ("def_perc", "numeric"),
        ("drop_perc", "numeric"), ("lowhp_perc", "numeric"), ("slow_perc", "numeric"),
        ("init_perc", "numeric"), ("detection", "boolean"), ("ultra_shiny", "boolean"), ("mythic", "boolean"),
        ("seal", "numeric"), ("holo", "boolean"),                 # new in v2: the pet cosmetics
        ("frame", text), ("holo_partial", "boolean")]             # ... and the frames of 2026-11-21
    by_name = {row["column_name"]: row for row in rows}
    assert all(by_name[name]["character_maximum_length"] == 255 for name in ("pet_name", "quality", "type", "variant"))
    assert by_name["mythic"]["is_nullable"] == "NO" and by_name["mythic"]["column_default"] == "false"
    assert by_name["seal"]["column_default"] == "0" and by_name["holo"]["column_default"] == "false"
    assert by_name["frame"]["character_maximum_length"] == 16
    assert by_name["frame"]["column_default"] == "''::character varying"
    assert by_name["holo_partial"]["column_default"] == "false"
    assert [name for name, row in by_name.items() if row["is_nullable"] == "NO"] == [
        "mythic", "seal", "holo", "frame", "holo_partial"]

    for table in ("petowner", "pets"):  # neither table has a primary key or any other index
        assert await db.fetchval("SELECT COUNT(*) FROM pg_index WHERE indrelid = $1::regclass", table) == 0


async def test_create_helpers_seed_empty_tables_and_never_destroy(game_db):
    bot, db = game_db.bot, game_db.bot.db
    for _ in range(2):
        await pets.create_pet_owners_table(bot)
        await pets.create_pets_table(bot)
    assert await count(db, "PETOWNER") == 1 and await count(db, "PETS") == 1
    assert (await owner_row(db, 0))["pet_owned"] is True
    default = await pet_row(db, 0)
    assert (default["pet_name"], default["quality"], default["type"], default["variant"]) == (
        "Default", "Standard", "Type", "0")
    assert [default[name] for name in ("crit_perc", "replace_perc", "def_perc", "drop_perc", "lowhp_perc",
                                       "slow_perc", "init_perc")] == [1, 2, 3, 4, 5, 6, 7]
    assert default["mythic"] is False and default["shiny"] is True

    player = game_db.add_member("Ala")
    assert await pets.assign_pet(game_db.scene(), player) is True
    await pets.create_pet_owners_table(bot)
    await pets.create_pets_table(bot)
    assert await count(db, "PETOWNER") == 2 and await count(db, "PETS") == 2      # nothing dropped or re-seeded
    assert (await owner_row(db, player.id))["pet_id"] == 1

    await db.execute("DROP TABLE PETS")
    await pets.create_pets_table(bot)
    assert await count(db, "PETS") == 1 and await count(db, "PETOWNER") == 2


# -- eggs -----------------------------------------------------------------------------

async def test_first_egg_creates_the_owner_row(game_db, monkeypatch):
    db = game_db.bot.db
    rig(monkeypatch, choices=[PetType.SHEEP], randint=lambda low, high: 905 if high == 1000 else 7)
    player = game_db.add_member("O'Neil")
    assert await pets.assign_pet(game_db.scene(), player) is True
    assert await owner_row(db, player.id) == {
        "player_id": player.id, "pet_id": 1, "pet_owned": True, "reroll_scroll": 0, "reroll_scroll_shard": 0,
        "rebirth_stones": 0, "mirrors": 0, "pet_id_alt1": 0, "pet_id_alt2": 0, "skill_gem": 0,
        "pet_id_alt3": 0, "pet_id_alt4": 0, **MERCHANT_DEFAULTS}
    assert await pet_row(db, 1) == {
        "pet_id": 1, "pet_name": "Towarzysz", "pet_lvl": 0, "pet_skills": 0, "quality": "Standard",
        "shiny": True, "type": "Sheep", "variant": "7", "crit_perc": 0, "replace_perc": 0, "def_perc": 0,
        "drop_perc": 0, "lowhp_perc": 0, "slow_perc": 0, "init_perc": 0, "detection": False,
        "ultra_shiny": False, "mythic": False, "seal": 0, "holo": False, "frame": "", "holo_partial": False}
    # A second egg is refused and nothing is generated for it.
    assert await pets.assign_pet(game_db.scene(), player) is False
    assert await count(db, "PETS") == 1 and await count(db, "PETOWNER") == 1
    assert game_db.channel.bot_messages == [game_db.channel.messages[0]]      # an ordinary egg is not announced


async def test_egg_for_a_player_who_only_had_items_keeps_them(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 0, pet_owned=False, reroll_scroll=3, mirrors=1, skill_gem=5, pet_id_alt1=9)
    await add_pet(db, 9)
    assert await pets.assign_pet(game_db.scene(), player) is True
    row = await owner_row(db, player.id)
    assert (row["pet_id"], row["reroll_scroll"], row["mirrors"], row["skill_gem"], row["pet_id_alt1"]) == (10, 3, 1, 5, 9)
    assert await count(db, "PETOWNER") == 1

    nobody = game_db.add_member("Ola")           # a row with NULLs (older than the newer columns)
    await _insert(db, "PETOWNER", {"player_id": nobody.id, "pet_id": None, "pet_owned": None})
    assert await pets.assign_pet(game_db.scene(), nobody) is True
    assert (await owner_row(db, nobody.id))["pet_id"] == 11


async def test_two_eggs_at_once_give_one_pet_and_one_row(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    results = await asyncio.gather(*(pets.assign_pet(game_db.scene(), player) for _ in range(4)))
    assert sorted(results) == [False, False, False, True]
    assert await count(db, "PETOWNER") == 1 and await count(db, "PETS") == 1
    # Different players at once: every egg gets its own PET_ID (the table has no key to enforce it).
    others = [game_db.add_member(f"Gracz{number}") for number in range(5)]
    assert all(await asyncio.gather(*(pets.assign_pet(game_db.scene(), other) for other in others)))
    pet_ids = [(await owner_row(db, other.id))["pet_id"] for other in others]
    assert sorted(pet_ids) == [2, 3, 4, 5, 6] and await count(db, "PETS") == 6


async def test_mythic_egg_is_announced(game_db, monkeypatch):
    rig(monkeypatch, randint=lambda low, high: 1000 if high == 1000 else 0)
    player = game_db.add_member("Ala")
    assert await pets.assign_pet(game_db.scene(), player) is True
    assert game_db.channel.messages[-1].content == (
        f"Łowca <@{player.id}> zdobył mitycznego towarzysza! <:POGGERS:936907543849078844>")
    egg = await pet_row(game_db.bot.db, 1)
    assert (egg["type"], egg["quality"], egg["variant"]) == ("Void", "Mythic", "0")
    assert egg["mythic"] and egg["ultra_shiny"] and egg["shiny"]

    game_db.bot.debug = False                     # production pings the hunters
    other = game_db.add_member("Ola")
    await pets.assign_pet(game_db.scene(), other)
    assert game_db.channel.messages[-1].content == (
        f"<@&985071779787730944>! Łowca <@{other.id}> zdobył mitycznego towarzysza! "
        "<:POGGERS:936907543849078844>")

    # Discord failing on the announcement does not cost the player the egg.
    third = game_db.add_member("Ela")
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("boom"), 1
    assert await pets.assign_pet(game_db.scene(), third) is True
    assert (await owner_row(game_db.bot.db, third.id))["pet_id"] == 3
    assert any("zdobył mitycznego towarzysza" in line for line in game_db.bot.owner_log)


async def test_generate_egg_works_on_an_empty_table_and_counts_up(game_db, monkeypatch):
    db = game_db.bot.db
    pools, _ = rig(monkeypatch, randint=0)
    assert await pets.generate_pet_egg(game_db.scene()) == 1          # the owner is not on the server
    assert pools == [list(pets.STANDARD_EGGS)]
    await add_pet(db, 41)
    game_db.add_member("Alter", roles=[PATRON], member_id=game_db.bot.ids.owner)
    assert await pets.generate_pet_egg(game_db.scene()) == 42
    assert pools[-1] == list(pets.PATRON_EGGS)
    assert await pets.generate_pet_egg(game_db.scene(), game_db.add_member("Ala")) == 43
    assert await count(db, "PETOWNER") == 0                           # generated for nobody


async def test_reassign_pet(game_db):
    bot, db = game_db.bot, game_db.bot.db
    await add_pet(db, 5)
    await add_pet(db, 6)
    assert await pets.reassign_pet(bot, "5", "111") is True            # new player, ids given as text
    assert (await owner_row(db, 111))["pet_id"] == 5
    assert await pets.reassign_pet(bot, 6, 111) is False              # already has a pet
    assert await pets.reassign_pet(bot, 99, 222) is False             # no such pet
    assert await owner_row(db, 222) is None
    await add_owner(db, 333, 0, reroll_scroll=4)
    assert await pets.reassign_pet(bot, 6, 333) is True
    row = await owner_row(db, 333)
    assert (row["pet_id"], row["reroll_scroll"]) == (6, 4) and await count(db, "PETOWNER") == 2


# -- items ----------------------------------------------------------------------------

async def test_items_are_counted_for_new_old_and_absent_players(game_db):
    db, scene = game_db.bot.db, game_db.scene()
    gone = 555_000_000_000_000_001                # not on the server (any more): an id is enough
    assert await pets.assign_scroll(scene, 2, gone) is True
    assert await pets.assign_shard(scene, "3", str(gone)) is True
    assert await pets.assign_rebirth_stone(scene, 1, gone) is True
    assert await pets.assign_mirror(scene, 1, gone) is True
    assert await pets.assign_scroll(scene, 1, gone) is True
    row = await owner_row(db, gone)
    assert (row["reroll_scroll"], row["reroll_scroll_shard"], row["rebirth_stones"], row["mirrors"]) == (3, 3, 1, 1)
    assert (row["pet_id"], row["pet_owned"], row["skill_gem"]) == (0, False, 0)
    assert await count(db, "PETOWNER") == 1

    # Rows older than the columns hold NULLs.
    await _insert(db, "PETOWNER", {"player_id": 7, "pet_id": 3, "pet_owned": True})
    for give in (pets.assign_scroll, pets.assign_shard, pets.assign_rebirth_stone, pets.assign_mirror):
        assert await give(scene, 2, 7) is True
    row = await owner_row(db, 7)
    assert (row["reroll_scroll"], row["reroll_scroll_shard"], row["rebirth_stones"], row["mirrors"]) == (2, 2, 2, 2)
    assert row["pet_id"] == 3


async def test_items_given_at_the_same_moment_are_all_counted(game_db):
    db, scene = game_db.bot.db, game_db.scene()
    await asyncio.gather(*(pets.assign_scroll(scene, 1, 42) for _ in range(6)),
                         *(pets.assign_mirror(scene, 2, 42) for _ in range(3)))
    row = await owner_row(db, 42)
    assert (row["reroll_scroll"], row["mirrors"]) == (6, 6) and await count(db, "PETOWNER") == 1


async def test_new_record_never_duplicates_a_player(game_db):
    bot, db = game_db.bot, game_db.bot.db
    assert await pets.new_record_petowners(bot, 9, 0, False, 0, 0, 0, 0, 12) is True
    assert await owner_row(db, 9) == {
        "player_id": 9, "pet_id": 0, "pet_owned": False, "reroll_scroll": 0, "reroll_scroll_shard": 0,
        "rebirth_stones": 0, "mirrors": 0, "pet_id_alt1": 0, "pet_id_alt2": 0, "skill_gem": 12,
        "pet_id_alt3": 0, "pet_id_alt4": 0, **MERCHANT_DEFAULTS}
    # The row exists already (another flow was faster): merged, nothing lost, nothing overwritten.
    assert await pets.new_record_petowners(bot, 9, 4, True, 1, 2, 3, 4, 99) is False
    row = await owner_row(db, 9)
    assert (row["pet_id"], row["skill_gem"]) == (4, 12)
    assert (row["reroll_scroll"], row["reroll_scroll_shard"], row["rebirth_stones"], row["mirrors"]) == (1, 2, 3, 4)
    results = await asyncio.gather(*(pets.new_record_petowners(bot, 10, 0, False, 1, 0, 0, 0, 0) for _ in range(5)))
    assert sorted(results) == [False] * 4 + [True]
    assert await count(db, "PETOWNER") == 2 and (await owner_row(db, 10))["reroll_scroll"] == 5


# -- level up and fight stats ---------------------------------------------------------

async def test_egg_hatches_and_learns(game_db, monkeypatch):
    db, scene = game_db.bot.db, game_db.scene()
    await add_owner(db, 1, 5)
    await add_pet(db, 5, variant="7", type="Bear")
    pools, ranges = rig(monkeypatch, choices=["DETECTION", "DETECTION"])
    assert await pets.level_up_pet(scene, "1") is True
    row = await pet_row(db, 5)
    assert (row["pet_lvl"], row["pet_skills"], row["variant"]) == (1, 2, "1")       # 7 % 2
    assert row["detection"] is True and row["crit_perc"] == 5
    assert ranges == [(1, 2), (3, 5)]
    assert "DETECTION" in pools[0] and "DETECTION" not in pools[1]                  # not learnt twice

    rig(monkeypatch, choices=["SLOW_PERC", "SLOW_PERC"], randint=lambda low, high: 1 if high == 2 else high)
    assert await pets.level_up_pet(scene, 1) is True
    assert await pets.level_up_pet(scene, 1) is True
    row = await pet_row(db, 5)
    assert (row["pet_lvl"], row["pet_skills"], row["variant"], row["slow_perc"]) == (3, 4, "1", 16)
    assert await pets.level_up_pet(scene, 1) is False                                # level 3 is the top
    assert await pet_row(db, 5) == row


async def test_mythic_pet_always_learns_two_and_slow_is_capped(game_db, monkeypatch):
    db, scene = game_db.bot.db, game_db.scene()
    await add_owner(db, 1, 5)
    await add_pet(db, 5, pet_lvl=1, pet_skills=2, mythic=True, ultra_shiny=True, shiny=True, type="Void",
                  quality="Mythic", slow_perc=80, crit_perc=10)
    pools, ranges = rig(monkeypatch, choices=["SLOW_PERC", "CRIT_PERC"])
    assert await pets.level_up_pet(scene, 1) is True
    row = await pet_row(db, 5)
    assert (row["pet_lvl"], row["pet_skills"], row["slow_perc"]) == (2, 4, 80)
    assert all("SLOW_PERC" not in pool for pool in pools)
    assert ranges == [(8, 12), (8, 12)] and row["crit_perc"] + row["replace_perc"] == 34


async def test_level_up_without_a_usable_pet_is_false(game_db):
    db, scene = game_db.bot.db, game_db.scene()
    assert await pets.level_up_pet(scene, 1) is False                 # no row
    await add_owner(db, 1, 0, pet_id_alt1=5)
    await add_pet(db, 5)
    assert await pets.level_up_pet(scene, 1) is False                 # the pet rests in the stable
    await add_owner(db, 2, 77)
    assert await pets.level_up_pet(scene, 2) is False                 # PETS row is gone (legacy: crash)
    assert (await pet_row(db, 5))["pet_lvl"] == 0


async def test_fight_stats_are_plain_numbers_with_every_legacy_key(game_db):
    bot, db = game_db.bot, game_db.bot.db
    keys = ["CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC", "SLOW_PERC", "INIT_PERC",
            "DETECTION"]
    nothing = dict.fromkeys(keys[:-1], 0) | {"DETECTION": False}
    assert await pets.get_pet_skills(bot, 1) == nothing and list(await pets.get_pet_skills(bot, 1)) == keys

    await add_owner(db, 1, 5, pet_id_alt1=6)
    await add_pet(db, 5, crit_perc=11, replace_perc=7, def_perc=3, drop_perc=9, lowhp_perc=4, slow_perc=21,
                  init_perc=6, detection=True)
    await add_pet(db, 6, crit_perc=99)
    skills = await pets.get_pet_skills(bot, "1")
    assert skills == {"CRIT_PERC": 11, "REPLACE_PERC": 7, "DEF_PERC": 3, "DROP_PERC": 9, "LOWHP_PERC": 4,
                      "SLOW_PERC": 21, "INIT_PERC": 6, "DETECTION": True}
    assert list(skills) == keys and all(type(value) in (int, bool) for value in skills.values())
    assert (await pets.get_pet_skills(bot, FakeMember("Ala", member_id=1)))["CRIT_PERC"] == 11

    await add_owner(db, 2, 0, pet_id_alt1=6)                          # only a stabled pet
    await add_owner(db, 3, 404)                                       # pet without a PETS row
    await _insert(db, "PETOWNER", {"player_id": 4, "pet_id": None})
    await add_owner(db, 8, 9)
    await _insert(db, "PETS", {"pet_id": 9, "detection": None, "crit_perc": None, "slow_perc": Decimal("2.5")})
    for player_id in (2, 3, 4):
        assert await pets.get_pet_skills(bot, player_id) == nothing
    assert await pets.get_pet_skills(bot, 8) == nothing | {"SLOW_PERC": 2.5}


async def test_database_outage_is_an_error_not_an_answer(game):
    game.bot.db = DatabaseDown()
    scene, player = game.scene(), game.add_member("Ala")
    with pytest.raises(DatabaseUnavailable):
        await pets.get_pet_skills(game.bot, player.id)
    for call in (pets.assign_pet(scene, player), pets.level_up_pet(scene, player.id),
                 pets.assign_scroll(scene, 1, player.id), pets.detect_boss(game.bot, 2),
                 pets.show_pet(scene, player), pets.reroll_pet(scene, player)):
        with pytest.raises(DatabaseUnavailable):
            await call
    assert game.channel.bot_messages == [game.channel.messages[0]]    # nobody was told a fairy tale


# -- !towarzysz -----------------------------------------------------------------------

async def test_show_pet_without_a_pet(game_db):
    player = game_db.add_member("Ala")
    assert await pets.show_pet(game_db.scene(player), player) is False
    await add_owner(game_db.bot.db, player.id, 0, reroll_scroll_shard=25)
    assert await pets.show_pet(game_db.scene(player), player) is False
    assert game_db.channel.count(
        f"{ALONE} <@{player.id}> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, "
        "a może i są inne sposoby na zdobycie towarzysza?") == 2
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll_shard"] == 25   # only pet owners convert


async def test_show_egg(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, variant="7")
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    embed = message.embeds[0]
    assert embed.title == "Jajko" and embed.colour.value == 0xfffffc
    assert embed.description == (
        f"Oto Twój towarzysz, <@{player.id}>. Opiekuj się nim, a być może kiedyś coś z niego wyrośnie...\n"
        "\n**💎 PRZEDMIOTY:**\nZwoje odrodzenia: 0\nFragmenty zwojów: 0\nKamienie olśnienia: 0\nLustra wizualiów: 0")
    assert embed.footer.text == "Na zawsze ponosisz odpowiedzialność za to, co oswoiłeś."
    assert message.files == ["7.png"] and embed.image.url == "attachment://7.png"

    await db.execute("UPDATE PETS SET PET_NAME = $1, QUALITY = 'Premium', TYPE = 'Ghost', VARIANT = '1', "
                     "ULTRA_SHINY = TRUE, SHINY = TRUE", "Kacper")
    await pets.show_pet(game_db.scene(player), player)
    embed = last_embed(game_db)
    assert embed.title == "Jajko Kacper" and embed.colour.value == 0xff8d15
    assert embed.description.endswith("\n\n*Absolutnie nadzwyczajny towarzysz.*")
    assert game_db.channel.messages[-1].files == ["1.png"]


async def test_show_grown_pet_with_stats_and_shard_conversion(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, reroll_scroll=2, reroll_scroll_shard=23, rebirth_stones=1)
    await add_pet(db, 5, pet_name="O'Burek", pet_lvl=3, pet_skills=5, quality="Premium", type="Dragon",
                  variant="1", shiny=True, crit_perc=10, replace_perc=7, drop_perc=4, slow_perc=12, detection=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    embed = message.embeds[0]
    assert embed.title == "Smok O'Burek" and embed.colour.value == 0xffdd00
    assert embed.description == (
        f"Oto Twój towarzysz, <@{player.id}>.\n\n"
        "📜 **STATYSTYKI:**\nPoziom: 3\nTalent: Mistrz\n\n"
        "Szansa na zastąpienie ataku: 7 %\nSzansa na krytyczne uderzenie: 10 %\n"
        "Spowolnienie przeciwnika: 12 %\nRzadszy drop o: 4 %\nWyczuwanie bossa: Tak\n"
        "\n**💎 PRZEDMIOTY:**\nZwoje odrodzenia: 4\nFragmenty zwojów: 3\nKamienie olśnienia: 1\nLustra wizualiów: 0"
        "\n\n*Rzadki towarzysz. Miałeś niesamowite szczęście.*")
    assert message.files == ["101.png"] and embed.image.url == "attachment://101.png"
    row = await owner_row(db, player.id)
    assert (row["reroll_scroll"], row["reroll_scroll_shard"]) == (4, 3)             # 23 shards -> 2 scrolls + 3

    await db.execute("UPDATE PETS SET DEF_PERC = 1, LOWHP_PERC = 2, INIT_PERC = 3, PET_LVL = 2, PET_SKILLS = 12, "
                     "PET_NAME = 'Towarzysz', SHINY = FALSE")
    await pets.show_pet(game_db.scene(player), player)
    embed = last_embed(game_db)
    assert embed.title == "Smok" and "Talent: Boski\n" in embed.description
    assert ("Szansa na zablokowanie ataku: 1 %\nZmniejszenie życia przeciwnika: 2 %\n" in embed.description
            and "Szansa na inicjację bossa: 3 %\n" in embed.description)
    assert game_db.channel.messages[-1].files == ["11.png"]


async def test_show_mythic_pet_and_null_items(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await _insert(db, "PETOWNER", {"player_id": player.id, "pet_id": 5, "reroll_scroll": 1})
    await add_pet(db, 5, pet_lvl=2, pet_skills=4, quality="Mythic", type="Void", mythic=True, ultra_shiny=True,
                  shiny=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    embed = last_embed(game_db)
    assert embed.title == "Istota pustki" and embed.colour.value == 0xff4815
    assert "Talent: Ekspert\n\n" in embed.description and "Wygląd" not in embed.description
    assert embed.description.endswith(
        "Zwoje odrodzenia: 1\nFragmenty zwojów: 0\nKamienie olśnienia: 0\nLustra wizualiów: 0"
        "\n\n*Mityczny towarzysz.*")
    assert game_db.channel.messages[-1].files == ["10.gif"]


async def test_show_pet_survives_missing_picture_and_missing_row(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=1, type="Hydra", pet_name="x" * 255)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert message.files == [] and message.embeds[0].image.url is None
    assert len(message.embeds[0].title) == 256 and "Poziom: 1" in message.embeds[0].description
    for broken in (dict(quality="Epic"), dict(variant="x"), dict(pet_lvl=0, quality="Mythic", type="Cat")):
        await db.execute("DELETE FROM PETS")
        await add_pet(db, 5, **{"pet_lvl": 1, **broken})
        assert await pets.show_pet(game_db.scene(player), player) is True
        assert game_db.channel.messages[-1].files == []

    await db.execute("DELETE FROM PETS")
    assert await pets.show_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == "Nie znaleziono danych towarzysza."


# -- !odrodzenie ----------------------------------------------------------------------

async def reroll_setup(game, *, scrolls: int = 2, **pet):
    player = game.add_member("Ala")
    await add_owner(game.bot.db, player.id, 5, reroll_scroll=scrolls)
    await add_pet(game.bot.db, 5, **{**dict(pet_lvl=2, pet_skills=4, crit_perc=20, def_perc=9, detection=True,
                                            init_perc=9, variant="1"), **pet})
    return player


async def test_reroll_spends_a_scroll_and_redistributes_the_talent(game_db, monkeypatch):
    db = game_db.bot.db
    player = await reroll_setup(game_db)
    pools, ranges = rig(monkeypatch, choices=["CRIT_PERC", "DETECTION", "SLOW_PERC", "SLOW_PERC"])
    confirms(game_db, player, "$POTWIERDZAM")
    assert await pets.reroll_pet(game_db.scene(player), player) is True

    assert game_db.channel.contains(
        f"Czy jesteś pewien, że chcesz przelosować statystyki swojego towarzysza <@{player.id}>? "
        "<:Hmm:984767035617730620> Kliknij **Potwierdzam** albo wpisz **!potwierdzam**.")
    row = await pet_row(db, 5)
    assert {name: row[name.lower()] for name in pets.SKILL_POOL} == {
        "CRIT_PERC": 5, "REPLACE_PERC": 0, "DEF_PERC": 0, "DROP_PERC": 0, "LOWHP_PERC": 0, "SLOW_PERC": 16,
        "DETECTION": True}
    assert (row["pet_skills"], row["pet_lvl"], row["variant"], row["init_perc"]) == (4, 2, "1", 9)
    assert ranges == [(3, 5), (5, 8), (5, 8)]
    assert [len(pool) for pool in pools] == [7, 7, 6, 6] and "DETECTION" not in pools[2]
    assert (await owner_row(db, player.id))["reroll_scroll"] == 1
    answer = next(m for m in game_db.channel.messages if m.content == "$POTWIERDZAM")
    assert answer.reactions == [PEPOG]
    assert "Szansa na krytyczne uderzenie: 5 %" in last_embed(game_db).description      # the result is shown


async def test_reroll_needs_a_pet_and_a_scroll(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    assert await pets.reroll_pet(game_db.scene(player), player) is False
    await add_owner(db, player.id, 0, reroll_scroll=5)
    assert await pets.reroll_pet(game_db.scene(player), player) is False
    assert game_db.channel.count(f"{ALONE} <@{player.id}>") == 2
    await db.execute("UPDATE PETOWNER SET PET_ID = 5, REROLL_SCROLL = NULL")
    await add_pet(db, 5, pet_lvl=1, pet_skills=1, crit_perc=4)
    assert await pets.reroll_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == (
        f"Nie masz wystarczająco zwojów odrodzenia <@{player.id}> <:Sadge:936907659142111273> Wpisz "
        "**!towarzysz**, żeby sprawdzić ich ilość. Zwoje możesz zdobyć na polowaniu lub po zabiciu bossów.")
    await db.execute("UPDATE PETOWNER SET REROLL_SCROLL = 9")
    assert await pets.reroll_pet(game_db.scene(player), player, [0, 5]) is False      # 20 scrolls needed
    assert not game_db.channel.contains(ASK)
    await db.execute("DELETE FROM PETS")
    assert await pets.reroll_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == "Nie znaleziono danych towarzysza."


async def test_reroll_times_out_without_changing_anything(game_db, short_timeouts):
    db = game_db.bot.db
    player = await reroll_setup(game_db)
    before = await pet_row(db, 5)
    assert await pets.reroll_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == "*Twój towarzysz spogląda na Ciebie niepewnie.*"
    assert await pet_row(db, 5) == before and (await owner_row(db, player.id))["reroll_scroll"] == 2
    # A late answer is not picked up by the next question.
    game_db.say(player, "!potwierdzam")
    assert await pets.reroll_pet(game_db.scene(player), player) is False
    assert (await owner_row(db, player.id))["reroll_scroll"] == 2


async def test_only_the_player_on_this_channel_can_confirm(game_db, monkeypatch):
    monkeypatch.setattr(pets, "CONFIRM_TIMEOUT", 0.3)
    player = await reroll_setup(game_db)
    other = game_db.add_member("Ola")
    task = asyncio.ensure_future(pets.reroll_pet(game_db.scene(player), player))
    await until(lambda: game_db.channel.contains(ASK))
    game_db.say(other, "!potwierdzam")
    game_db.say(player, "!potwierdzam", channel=game_db.log_channel)
    game_db.say(player, "potwierdzam")
    game_db.say(game_db.bot.fake_user, "!potwierdzam")
    assert await task is False
    assert game_db.channel.messages[-1].content == "*Twój towarzysz spogląda na Ciebie niepewnie.*"
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 2


async def test_answer_typed_before_the_question_arrives_counts(game_db, monkeypatch):
    """The bot listens before it asks: an answer sent while the question is in flight is not lost."""
    player = await reroll_setup(game_db)
    rig(monkeypatch)
    game_db.channel.send_hooks.append(
        lambda message: game_db.say(player, "!potwierdzam") if ASK in message.content else None)
    assert await asyncio.wait_for(pets.reroll_pet(game_db.scene(player), player), 5) is True
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 1


async def test_scrolls_spent_while_the_question_was_open_are_not_spent_twice(game_db, monkeypatch):
    db = game_db.bot.db
    player = await reroll_setup(game_db, scrolls=1)
    before = await pet_row(db, 5)
    task = asyncio.ensure_future(pets.reroll_pet(game_db.scene(player), player))
    await until(lambda: game_db.channel.contains(ASK))
    await db.execute("UPDATE PETOWNER SET REROLL_SCROLL = 0")
    game_db.say(player, "!potwierdzam")
    assert await asyncio.wait_for(task, 5) is False
    assert game_db.channel.messages[-1].content.startswith("Nie masz wystarczająco zwojów odrodzenia")
    assert await pet_row(db, 5) == before and (await owner_row(db, player.id))["reroll_scroll"] == 0


async def test_one_open_question_per_player(game_db, monkeypatch, sacrifices):
    db = game_db.bot.db
    player = await reroll_setup(game_db)
    rig(monkeypatch)
    task = asyncio.ensure_future(pets.reroll_pet(game_db.scene(player), player))
    await until(lambda: game_db.channel.contains(ASK))
    waiting = ("Już czekam na Twoje potwierdzenie. Kliknij **Potwierdzam** (albo wpisz **!potwierdzam**) "
               "albo poczekaj, aż prośba wygaśnie.")
    assert await pets.discard_pet(game_db.scene(player)) is False
    assert await pets.reroll_pet(game_db.scene(player), player) is False
    assert await pets.store_pet(game_db.scene(player), 1) is False
    await db.execute("UPDATE PETOWNER SET PET_ID_ALT2 = 6")
    assert await pets.unstore_pet(game_db.scene(player), 2) is False
    assert game_db.channel.count(waiting) == 4 and game_db.channel.count("Czy jesteś pewien") == 1
    game_db.say(player, "!potwierdzam")
    assert await asyncio.wait_for(task, 5) is True
    # One answer confirmed one thing: the pet was rerolled, not discarded, not moved.
    row = await owner_row(db, player.id)
    assert (row["pet_id"], row["reroll_scroll"], row["pet_id_alt1"]) == (5, 1, 0) and sacrifices == []
    assert await pet_row(db, 5) is not None


async def test_reroll_leaving_stats_out_costs_ten_scrolls_each(game_db, monkeypatch):
    db = game_db.bot.db
    player = await reroll_setup(game_db, scrolls=25, pet_skills=6)
    pools, _ = rig(monkeypatch, choices=["DROP_PERC", "DETECTION", "REPLACE_PERC"])
    confirms(game_db, player)
    assert await pets.reroll_pet(game_db.scene(player), player, [0, 5]) is True
    assert all(pool[:1] != ["CRIT_PERC"] and "CRIT_PERC" not in pool and "SLOW_PERC" not in pool for pool in pools)
    assert pools[0] == ["REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC", "DETECTION"]
    row = await pet_row(db, 5)
    assert (row["crit_perc"], row["slow_perc"], row["drop_perc"], row["detection"]) == (0, 0, 5, True)
    assert row["replace_perc"] == 20                                    # 1 scripted + 3 defaults (first of pool)
    assert (await owner_row(db, player.id))["reroll_scroll"] == 5


# -- !mocneodrodzenie -----------------------------------------------------------------

async def open_menu(game, player):
    task = asyncio.ensure_future(pets.adv_reroll_pet(game.scene(player)))
    menu = await game.wait_for_buttons("pet:skip")
    return task, menu


async def choose(game, menu, member, *indexes: int):
    """``member`` chooses the stats ``indexes`` (of SKILL_POOL) in the menu's list."""
    return await game.click(menu, member, "pet:skip", values=[str(index) for index in indexes])


async def test_advanced_reroll_menu(game_db, monkeypatch):
    db = game_db.bot.db
    player = await reroll_setup(game_db, scrolls=30)
    other = game_db.add_member("Ola")
    pools, _ = rig(monkeypatch)
    confirms(game_db, player)
    task, menu = await open_menu(game_db, player)
    assert menu.content == (
        "Wybierz z listy, które umiejętności chcesz pominąć przy przelosowaniu."
        "\nKażda wybrana statystyka to koszt 10 zwojów, maksymalnie 3 do wyboru."
        "\n\n*Potwierdź przyciskiem **Przelosuj**.*\n\nPominięte: brak - koszt: **1 zwój**.")
    assert menu.reactions == [] and menu.button_labels == ["Przelosuj", "Anuluj"]
    select = menu.button("pet:skip")
    assert [option.label for option in select.options] == [
        "Szansa na krytyczne uderzenie", "Szansa na zastąpienie ataku", "Szansa na zablokowanie ataku",
        "Rzadszy drop", "Zmniejszenie życia przeciwnika", "Spowolnienie przeciwnika", "Wyczuwanie bossa"]
    assert [str(option.emoji) for option in select.options] == [ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN]
    assert (select.min_values, select.max_values) == (0, 3)

    # Strangers cannot use it - told so on the channel, addressed to them; the menu is untouched.
    # A changed mind counts (the list is the whole choice).
    not_yours = [f"Ten przycisk jest dla <@{player.id}>."]
    refused = await choose(game_db, menu, other, 1)
    assert refused.answers == not_yours and refused.public == [f"<@{other.id}> {not_yours[0]}"]
    assert refused.replies[0].message in game_db.channel.messages and "koszt: **1 zwój**" in menu.content
    assert (await game_db.click(menu, other, "pet:skip-accept")).answers == not_yours
    await choose(game_db, menu, player, 6, 0, 3)
    await until(lambda: "koszt: **30 zwojów**" in menu.content)
    await choose(game_db, menu, player, 6, 0)
    await until(lambda: "koszt: **20 zwojów**" in menu.content)
    assert "Pominięte: **Szansa na krytyczne uderzenie**, **Wyczuwanie bossa** - koszt" in menu.content
    assert [option.default for option in menu.button("pet:skip").options] == [True] + [False] * 5 + [True]
    await game_db.click(menu, player, "pet:skip-accept")
    assert await asyncio.wait_for(task, 5) is True
    assert menu.view is None and "koszt: **20 zwojów**" in menu.content            # the list is gone

    assert game_db.channel.contains("Zobaczmy...")
    assert pools and all(pool == ["REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC", "SLOW_PERC"]
                         for pool in pools)
    assert (await owner_row(db, player.id))["reroll_scroll"] == 10            # two stats left out: 20 scrolls
    row = await pet_row(db, 5)
    assert (row["crit_perc"], row["detection"], row["replace_perc"]) == (0, False, 20)


async def test_advanced_reroll_without_a_choice_is_an_ordinary_reroll(game_db, monkeypatch):
    player = await reroll_setup(game_db, scrolls=3)
    rig(monkeypatch)
    confirms(game_db, player)
    task, menu = await open_menu(game_db, player)
    await choose(game_db, menu, player, 2)
    await choose(game_db, menu, player)                                     # ... and taken back
    await game_db.click(menu, player, "pet:skip-accept")
    assert await asyncio.wait_for(task, 5) is True
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 2


async def test_advanced_reroll_allows_three_stats_at_most(game_db):
    """Discord's list stops at three; a forged fourth choice is still refused."""
    player = await reroll_setup(game_db, scrolls=99)
    before = await pet_row(game_db.bot.db, 5)
    task, menu = await open_menu(game_db, player)
    await choose(game_db, menu, player, 0, 1, 2, 3)
    await game_db.click(menu, player, "pet:skip-accept")
    assert await asyncio.wait_for(task, 5) is False
    assert game_db.channel.messages[-1].content == (
        "Wybrałeś za dużo statystyk. Można pominąć **maksymalnie 3**! <:madge:882184635474386974>")
    assert not game_db.channel.contains(ASK) and await pet_row(game_db.bot.db, 5) == before


async def test_advanced_reroll_menu_expires(game_db, short_timeouts):
    player = await reroll_setup(game_db)
    scene = game_db.scene(player)
    assert await pets.adv_reroll_pet(scene) is False
    assert not game_db.channel.contains("Wybierz z listy")                         # the menu is removed
    assert game_db.channel.messages[-1].content == "*Twój towarzysz spogląda na Ciebie niepewnie.*"
    menu = next(m for m in game_db.channel.sent if "Wybierz z listy" in m.content)
    assert menu.deleted and menu.view is None


# -- !transformacja -------------------------------------------------------------------

async def test_transform_changes_the_looks_for_a_mirror(game_db, monkeypatch):
    db = game_db.bot.db
    player = patron(game_db)
    await add_owner(db, player.id, 5, mirrors=2)
    await add_pet(db, 5, pet_lvl=1, pet_skills=2, crit_perc=4, variant="0")
    pools, ranges = rig(monkeypatch, choices=[PetType.DRAGON], randint=1)
    confirms(game_db, player)
    assert await pets.transform_pet(game_db.scene(player), player) is True
    assert game_db.channel.contains(
        f"Czy jesteś pewien, że chcesz zmienić wygląd swojego towarzysza <@{player.id}>? "
        "<:Hmm:984767035617730620> Kliknij **Potwierdzam** albo wpisz **!potwierdzam**.")
    assert pools == [list(pets.TRANSFORM_TYPES)] and ranges == [(0, 1)]
    row = await pet_row(db, 5)
    assert (row["type"], row["quality"], row["variant"], row["crit_perc"], row["pet_skills"]) == (
        "Dragon", "Premium", "1", 4, 2)
    assert (await owner_row(db, player.id))["mirrors"] == 1
    assert last_embed(game_db).title == "Smok" and game_db.channel.messages[-1].files == ["1.png"]

    rig(monkeypatch, choices=[PetType.EAGLE], randint=0)
    assert await pets.transform_pet(game_db.scene(player), player) is True
    row = await pet_row(db, 5)
    assert (row["type"], row["quality"], row["variant"]) == ("Eagle", "Standard", "0")
    assert (await owner_row(db, player.id))["mirrors"] == 0


async def test_transform_refusals(game_db):
    db = game_db.bot.db
    plain, rich = game_db.add_member("Ala"), patron(game_db)
    assert await pets.transform_pet(game_db.scene(plain), plain) is False
    assert game_db.channel.messages[-1].content.startswith(f"{ALONE} <@{plain.id}>")
    await add_owner(db, plain.id, 5, mirrors=3)
    assert await pets.transform_pet(game_db.scene(plain), plain) is False
    assert game_db.channel.messages[-1].content == "Nie znaleziono danych towarzysza."
    await add_pet(db, 5, pet_lvl=1)
    assert await pets.transform_pet(game_db.scene(plain), plain) is False
    assert game_db.channel.messages[-1].content == (
        f"Musisz mieć rangę Patrona, Craftera, subskrybenta Twitch lub wspierającego na YouTube <@{plain.id}>, "
        "żeby przetransformować towarzysza "
        "<:Sadge:936907659142111273> Rangi do zdobycia możesz zobaczyć na tym kanale <#688296443156365354>.")

    await _insert(db, "PETOWNER", {"player_id": rich.id, "pet_id": 6})            # MIRRORS is NULL
    await add_pet(db, 6, pet_lvl=1)
    assert await pets.transform_pet(game_db.scene(rich), rich) is False
    assert game_db.channel.messages[-1].content == (
        f"Nie masz żadnych lustr wizualiów <@{rich.id}> <:Sadge:936907659142111273> Wpisz **!towarzysz**, "
        "żeby sprawdzić ich ilość. Lustra możesz zdobyć na polowaniu lub po zabiciu bossów, jednak są one "
        "bardzo rzadkie.")
    await db.execute("UPDATE PETOWNER SET MIRRORS = 1")
    await db.execute("UPDATE PETS SET MYTHIC = TRUE WHERE PET_ID = 6")
    assert await pets.transform_pet(game_db.scene(rich), rich) is False
    assert game_db.channel.messages[-1].content == f"Mitycznego towarzysza nie można transformować, <@{rich.id}>!"
    assert not game_db.channel.contains(ASK)
    assert (await owner_row(db, rich.id))["mirrors"] == 1


# -- !oswiecenie ----------------------------------------------------------------------

async def test_enlightenment_adds_talent_for_a_stone(game_db, monkeypatch):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, rebirth_stones=1)
    await add_pet(db, 5, pet_lvl=3, pet_skills=6, crit_perc=10, variant="1", type="Dog")
    pools, ranges = rig(monkeypatch, choices=["CRIT_PERC", "DROP_PERC", "DETECTION"])
    confirms(game_db, player)
    assert await pets.enlight_pet(game_db.scene(player), player) is True
    assert game_db.channel.contains(
        f"Czy jesteś pewien, że chcesz zwiększyć talent swojego towarzysza <@{player.id}>? "
        "<:Hmm:984767035617730620> Kliknij **Potwierdzam** albo wpisz **!potwierdzam**.")
    row = await pet_row(db, 5)
    assert (row["pet_skills"], row["pet_lvl"], row["variant"]) == (9, 3, "1")
    assert (row["crit_perc"], row["drop_perc"], row["detection"]) == (15, 5, True)
    assert ranges == [(1, 3), (3, 5), (3, 5)]
    assert (await owner_row(db, player.id))["rebirth_stones"] == 0
    assert "Talent: Boski" in last_embed(game_db).description

    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == (
        f"Nie masz żadnych kamieni olśnienia <@{player.id}> <:Sadge:936907659142111273> Wpisz **!towarzysz**, "
        "żeby sprawdzić ich ilość. Kamienie olśnienia możesz zdobyć na polowaniu lub po zabiciu bossów, "
        "jednak są one bardzo rzadkie.")
    await db.execute("UPDATE PETOWNER SET REBIRTH_STONES = 2")
    assert await pets.enlight_pet(game_db.scene(player), player) is False          # talent 9: the top
    assert game_db.channel.messages[-1].content == (
        f"Twój towarzysz osiągnął już talent **Boski** <@{player.id}> <:prayge:1063891597760139304> Wyżej "
        "sięgają już tylko bogowie.")
    await db.execute("UPDATE PETS SET PET_LVL = 2, PET_SKILLS = 3")
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == (
        f"Niestety Twój towarzysz nie jest godny dostąpić oświecenia <@{player.id}> "
        "<:Sadge:936907659142111273> Wpisz **!towarzysz**, żeby sprawdzić jego poziom.")
    assert (await owner_row(db, player.id))["rebirth_stones"] == 2 and game_db.channel.count(ASK) == 1


async def test_a_typed_change_of_the_pet_says_nothing_during_a_fight(game_db, monkeypatch):
    """(2026-11-26) owner's rule: nothing posts into a running fight - ``_confirmed_change`` (``!oswiecenie``,
    ``!odrodzenie``, ``!transformacja``) checks the fight before its first post; nothing is taken either."""
    from altermmo.core import challenge

    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, rebirth_stones=1, reroll_scroll=3)
    await add_pet(db, 5, pet_lvl=3, pet_skills=6)
    confirms(game_db, player)
    with challenge.round_running(game_db.channel):
        before = len(game_db.channel.sent)
        assert await pets.enlight_pet(game_db.scene(player), player) is False
        assert await pets.reroll_pet(game_db.scene(player), player) is False
        assert len(game_db.channel.sent) == before
    row = await owner_row(db, player.id)
    assert (row["rebirth_stones"], row["reroll_scroll"]) == (1, 3) and (await pet_row(db, 5))["pet_skills"] == 6
    rig(monkeypatch, choices=["CRIT_PERC"] * 9)
    assert await pets.enlight_pet(game_db.scene(player), player) is True         # afterwards as always


async def feathers_of(db, player_id: int) -> int | None:
    value = await db.fetchval("SELECT COUNT FROM PLAYER_ITEMS WHERE PLAYER_ID = $1 AND ITEM = 'feather'", player_id)
    return None if value is None else int(value)


async def test_the_feather_enlightens_a_talent_7_or_8_pet_once_more(game_db, monkeypatch):
    """items_decisions.md §3: talent 7-8 - one Pióro Żar-ptaka + 2 Kamienie olśnienia, +1 talent (one roll);
    mythic pets too; talent 9 is the top.  The question says what it costs."""
    from altermmo.features import items

    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, rebirth_stones=3)
    await add_pet(db, 5, pet_lvl=3, pet_skills=7, crit_perc=10, mythic=True, quality="Mythic", type="Void")
    await items.grant(game_db.bot, player.id, "feather", 2)
    pools, ranges = rig(monkeypatch, choices=["CRIT_PERC"] * 3)
    confirms(game_db, player)
    assert await pets.enlight_pet(game_db.scene(player), player) is True
    assert game_db.channel.contains(
        f"Czy jesteś pewien, że chcesz zwiększyć talent swojego towarzysza <@{player.id}>? To oświecenie kosztuje "
        "🪶 **Pióro Żar-ptaka** i **2 Kamienie olśnienia** - towarzysz zyska jeden talent. "
        "<:Hmm:984767035617730620> Kliknij **Potwierdzam** albo wpisz **!potwierdzam**.")
    row = await pet_row(db, 5)
    assert (row["pet_skills"], row["crit_perc"]) == (8, 22)                    # one point: the mythic 8-12
    assert ranges == [(8, 12)] and len(pools) == 1                             # no 1-3 roll, ONE skill roll
    assert (await owner_row(db, player.id))["rebirth_stones"] == 1 and await feathers_of(db, player.id) == 1
    asked = game_db.channel.count(ASK)

    # Not enough stones (a feather is there): refused before the question, nothing taken.
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == (
        f"Oświecenie z 🪶 **Piórem Żar-ptaka** wymaga też **2 Kamieni olśnienia** <@{player.id}> "
        "<:Sadge:936907659142111273> Wpisz **!przedmioty**, żeby sprawdzić, co nosisz.")
    # No feather (stones enough): the text names both costs.
    await db.execute("UPDATE PETOWNER SET REBIRTH_STONES = 2")
    await items.take(game_db.bot, player.id, "feather")
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == (
        f"Twój towarzysz już dostąpił oświecenia <@{player.id}> <:prayge:1063891597760139304> Wyżej wzniesie go "
        "tylko 🪶 **Pióro Żar-ptaka**: za pióro i **2 Kamienie olśnienia** zyska jeszcze jeden talent. Wpisz "
        "**!przedmioty**, żeby sprawdzić, co nosisz.")
    assert game_db.channel.count(ASK) == asked and (await pet_row(db, 5))["pet_skills"] == 8

    # The last step: talent 9 ("Boski") - and nothing beyond it, whatever he carries.
    await items.grant(game_db.bot, player.id, "feather")
    assert await pets.enlight_pet(game_db.scene(player), player) is True
    assert (await pet_row(db, 5))["pet_skills"] == 9 and "Talent: Boski" in last_embed(game_db).description
    assert (await owner_row(db, player.id))["rebirth_stones"] == 0 and await feathers_of(db, player.id) == 0
    await db.execute("UPDATE PETOWNER SET REBIRTH_STONES = 5")
    await items.grant(game_db.bot, player.id, "feather", 3)
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content.endswith("Wyżej sięgają już tylko bogowie.")
    assert (await owner_row(db, player.id))["rebirth_stones"] == 5 and await feathers_of(db, player.id) == 3

    # Without the item store a talent 7-8 pet has no feather.
    await db.execute("UPDATE PETS SET PET_SKILLS = 7")
    game_db.bot._player_items = (False, items.clock.monotonic())
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content.startswith("Twój towarzysz już dostąpił oświecenia")


async def test_an_ordinary_enlightenment_never_asks_for_a_feather(game_db, monkeypatch):
    from altermmo.features import items

    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, rebirth_stones=1)
    await add_pet(db, 5, pet_lvl=3, pet_skills=6)
    await items.grant(game_db.bot, player.id, "feather")
    rig(monkeypatch, randint=1)
    confirms(game_db, player)
    assert await pets.enlight_pet(game_db.scene(player), player) is True
    assert not game_db.channel.contains("Pióro") and (await pet_row(db, 5))["pet_skills"] == 7
    assert (await owner_row(db, player.id))["rebirth_stones"] == 0 and await feathers_of(db, player.id) == 1


async def test_a_feather_or_stones_gone_after_the_question_change_nothing(game_db, monkeypatch):
    """The locked check sees what is left; and should a conditional take still find nothing, the whole change
    (the other item taken, the talent, the stats) is rolled back."""
    from altermmo.features import items

    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, rebirth_stones=2)
    await add_pet(db, 5, pet_lvl=3, pet_skills=7, crit_perc=10)
    await items.grant(game_db.bot, player.id, "feather")
    rig(monkeypatch, choices=["CRIT_PERC"] * 3)

    # The owner takes the feather back while the question is open.
    game_db.auto_answer(player, responder=lambda message: "!potwierdzam" if ASK in message.content else None)
    real_confirm = pets._confirm

    async def confirm(scene, who, prompt, **kwargs):
        answer = await real_confirm(scene, who, prompt, **kwargs)
        await items.adjust(game_db.bot, player.id, "feather", -1)
        return answer

    monkeypatch.setattr(pets, "_confirm", confirm)
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content.startswith("Twój towarzysz już dostąpił oświecenia")
    assert (await pet_row(db, 5))["pet_skills"] == 7 and (await owner_row(db, player.id))["rebirth_stones"] == 2
    monkeypatch.setattr(pets, "_confirm", real_confirm)

    # The stones vanish between the feather's take and theirs (in the same transaction): all is rolled back.
    await items.grant(game_db.bot, player.id, "feather")
    real_take = items.take_in

    async def take_in(con, player_id, key, number=1):
        left = await real_take(con, player_id, key, number)
        await con.execute("UPDATE PETOWNER SET REBIRTH_STONES = 1 WHERE PLAYER_ID = $1", player_id)
        return left

    monkeypatch.setattr(items, "take_in", take_in)
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content.startswith("Oświecenie z 🪶 **Piórem Żar-ptaka** wymaga też")
    row = await pet_row(db, 5)
    assert (row["pet_skills"], row["crit_perc"]) == (7, 10) and await feathers_of(db, player.id) == 1
    assert (await owner_row(db, player.id))["rebirth_stones"] == 2


async def test_enlightenment_refusals_and_timeout(game_db, short_timeouts):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content.startswith(f"{ALONE} <@{player.id}>")
    await _insert(db, "PETOWNER", {"player_id": player.id, "pet_id": 5})          # REBIRTH_STONES is NULL
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content.startswith("Nie masz żadnych kamieni olśnienia")
    await db.execute("UPDATE PETOWNER SET REBIRTH_STONES = 1")
    assert await pets.enlight_pet(game_db.scene(player), player) is False
    assert game_db.channel.messages[-1].content == "Nie znaleziono danych towarzysza."
    await add_pet(db, 5, pet_lvl=3, pet_skills=4)
    assert await pets.enlight_pet(game_db.scene(player), player) is False          # nobody confirms
    assert game_db.channel.messages[-1].content == "*Twój towarzysz spogląda na Ciebie niepewnie.*"
    assert (await pet_row(db, 5))["pet_skills"] == 4 and (await owner_row(db, player.id))["rebirth_stones"] == 1


# -- !porzucam ------------------------------------------------------------------------

async def test_discard_sacrifices_the_pet(game_db, sacrifices):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, pet_id_alt1=6, reroll_scroll=2)
    await add_pet(db, 5)
    await add_pet(db, 6)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert game_db.channel.contains(
        f"Czy jesteś pewien, że chcesz porzucić swojego towarzysza <@{player.id}>? "
        "<:MonkaS:882181709100097587> Kliknij **Potwierdzam** albo wpisz **!potwierdzam**.")
    row = await owner_row(db, player.id)
    assert (row["pet_id"], row["pet_id_alt1"], row["reroll_scroll"]) == (0, 6, 2)
    assert await pet_row(db, 5) is None and await pet_row(db, 6) is not None
    assert sacrifices == [(player, True, True)]
    answer = next(m for m in game_db.channel.messages if m.content == "!potwierdzam")
    assert answer.reactions == ["<:MonkaS:882181709100097587>"]


async def test_discard_draws_a_real_boss_modifier(game_db, monkeypatch):
    player = game_db.add_member("Ala")
    await add_owner(game_db.bot.db, player.id, 5)
    await add_pet(game_db.bot.db, 5)
    rig(monkeypatch, choices=["points_boost"], randint=3)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert game_db.bot.store.modifiers.load()["points_boost"] == 3
    assert game_db.channel.contains("Poświęcono towarzysza!")


async def test_discard_without_a_pet_does_not_block_the_command(game_db, sacrifices):
    """Legacy: the "waiting for your confirmation" flag was never cleared on this path."""
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    for _ in range(2):
        assert await pets.discard_pet(game_db.scene(player)) is False
    await add_owner(db, player.id, 0, pet_id_alt1=6)
    assert await pets.discard_pet(game_db.scene(player)) is False
    assert game_db.channel.count("Przecież jesteś sam... <:madge:882184635474386974>") == 3
    assert not game_db.channel.contains("Już czekam") and not game_db.channel.contains(ASK)
    assert sacrifices == []


async def test_discard_not_confirmed(game_db, short_timeouts, sacrifices):
    player = game_db.add_member("Ala")
    await add_owner(game_db.bot.db, player.id, 5)
    await add_pet(game_db.bot.db, 5)
    assert await pets.discard_pet(game_db.scene(player)) is False
    assert game_db.channel.messages[-1].content == "*Twój towarzysz oddycha z ulgą...*"
    assert await pet_row(game_db.bot.db, 5) is not None and sacrifices == []


async def test_database_failing_after_the_confirmation_leaves_nothing_stuck(game_db, monkeypatch, sacrifices):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5)

    def answer(message):
        if ASK not in message.content:
            return None
        monkeypatch.setattr(db, "transaction", DatabaseDown().transaction)
        return "!potwierdzam"

    game_db.auto_answer(player, responder=answer)
    with pytest.raises(DatabaseUnavailable):
        await pets.discard_pet(game_db.scene(player))
    monkeypatch.undo()
    assert pets._awaiting_confirmation == set() and sacrifices == []
    assert await pet_row(db, 5) is not None and (await owner_row(db, player.id))["pet_id"] == 5
    # ... and the player can simply try again.
    game_db.channel.send_hooks.clear()
    confirms(game_db, player)
    monkeypatch.setattr(pets.modifiers, "random_modifiers", lambda *args: asyncio.sleep(0))
    assert await pets.discard_pet(game_db.scene(player)) is True


async def test_discord_failing_on_the_question_leaves_nothing_stuck(game_db):
    player = await reroll_setup(game_db)
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("boom"), 1
    with pytest.raises(RuntimeError):
        await pets.reroll_pet(game_db.scene(player), player)
    assert pets._awaiting_confirmation == set()
    await game_db.settle()
    # Nobody is left listening (discord.py drops finished listeners on the next message).
    assert all(future.done() for future, _ in game_db.bot._listeners.get("message", []))
    game_db.say(player, "!potwierdzam")
    await game_db.settle()
    assert not game_db.bot._listeners.get("message")
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 2


# -- !nazwij --------------------------------------------------------------------------

async def test_naming(game_db, monkeypatch):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    scene = game_db.scene(player, "!nazwij x")
    assert await pets.name_pet(scene, "Burek") is False
    await add_owner(db, player.id, 0)
    assert await pets.name_pet(scene, "Burek") is False
    assert game_db.channel.count("Przecież jesteś sam... <:madge:882184635474386974>") == 2

    await db.execute("UPDATE PETOWNER SET PET_ID = 5")
    await add_pet(db, 5)
    await add_pet(db, 6)
    _, ranges = rig(monkeypatch, randint=84)
    assert await pets.name_pet(scene, "O'Neil; DROP TABLE PETS;--") is True
    row = await pet_row(db, 5)
    assert row["pet_name"] == "O'Neil; DROP TABLE PETS;--" and row["shiny"] is False and ranges == [(0, 100)]
    assert scene.message.reactions == ["<:peepoBlush:984769061340737586>"]
    assert not game_db.channel.contains("wdzięcznego")

    rig(monkeypatch, randint=85)
    assert await pets.name_pet(scene, "Ż" * 300) is True
    row = await pet_row(db, 5)
    assert row["pet_name"] == "Ż" * 255 and row["shiny"] is True
    assert game_db.channel.messages[-1].content == (
        "Towarzysz wygląda na bardzo wdzięcznego i dostrzegasz, że wygląda jakoś inaczej "
        "<:Susge:973591024322633858>")
    assert (await pet_row(db, 6))["pet_name"] == "Towarzysz"


# -- the stable -----------------------------------------------------------------------

async def test_store_and_take_out(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    scene = game_db.scene(player)
    await _insert(db, "PETOWNER", {"player_id": player.id, "pet_id": 5, "pet_id_alt3": 0, "pet_id_alt4": 0})

    async def slots():
        row = await owner_row(db, player.id)
        return [row[name] for name in ("pet_id", "pet_id_alt1", "pet_id_alt2", "pet_id_alt3", "pet_id_alt4")]

    assert await pets.store_pet(scene, "1") is True                         # slot 1 was NULL
    assert await slots() == [0, 5, None, 0, 0]
    assert game_db.channel.messages[-1].content == (
        "Towarzysz został ulokowany w stajni <:peepoBlush:984769061340737586>")
    assert await pets.store_pet(scene, 2) is False
    assert game_db.channel.messages[-1].content == (
        f"Niestety nie posiadasz aktualnie żadnego peta, <@{player.id}>. Możesz sprawdzić swoją stajnie "
        "wpisując **!stajnia**.")

    await db.execute("UPDATE PETOWNER SET PET_ID = 6")
    assert await pets.store_pet(scene, 1) is True                           # swap with the stabled pet
    assert await slots() == [5, 6, None, 0, 0]
    assert await pets.store_pet(scene, 4) is True
    assert await slots() == [0, 6, None, 0, 5]

    assert await pets.unstore_pet(scene, "4") is True
    assert await slots() == [5, 6, None, 0, 0]
    assert game_db.channel.messages[-1].content == (
        "Towarzysz został wyciągnięty ze stajni <:peepoBlush:984769061340737586>")
    assert await pets.unstore_pet(scene, 1) is True
    assert await slots() == [6, 5, None, 0, 0]
    assert game_db.channel.messages[-1].content == (
        "Nowy towarzysz został wyciągnięty ze stajni, a stary został w niej schowany "
        "<:peepoBlush:984769061340737586>")
    for empty in (2, 3):
        assert await pets.unstore_pet(scene, empty) is False
        assert game_db.channel.messages[-1].content == (
            "W stajni nie ma towarzysza na podanym miejscu. Sprawdź stajnie wpisując **!stajnia**.")
    assert await slots() == [6, 5, None, 0, 0]


async def test_stable_slot_must_be_one_to_four(game_db):
    db = game_db.bot.db
    player, nobody = game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, player.id, 5, pet_id_alt1=6)
    before = await owner_row(db, player.id)
    for slot in ("0", "5", "abc", "1.5", "-1"):
        assert await pets.store_pet(game_db.scene(player), slot) is False                # legacy: NameError
        assert game_db.channel.messages[-1].content == (
            "Każdy gracz ma tylko cztery miejsca na przechowanie slotów - 1, 2, 3 oraz 4 "
            f"<@{player.id}>! Wpisz np. **!schowajtowarzysza 1**.")
        assert await pets.unstore_pet(game_db.scene(player), slot) is False
        assert game_db.channel.messages[-1].content == (
            "Każdy gracz ma tylko cztery miejsca na przechowanie slotów - 1, 2, 3 oraz 4 "
            f"<@{player.id}>! Wpisz np. **!wyciagnijtowarzysza 1**.")
    assert await owner_row(db, player.id) == before
    for action in (pets.store_pet, pets.unstore_pet, lambda scene, slot: pets.check_stable(scene)):
        assert await action(game_db.scene(nobody), 1) is False
        assert game_db.channel.messages[-1].content.startswith(f"{ALONE} <@{nobody.id}>")


async def test_stable_view(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, pet_id_alt1=6, pet_id_alt2=404, pet_id_alt3=None)
    await add_pet(db, 5, pet_name="O'Burek", pet_lvl=3, type="Dragon")
    await add_pet(db, 6, pet_name="Towarzysz", pet_lvl=0, type="Cat")
    assert await pets.check_stable(game_db.scene(player)) is True
    embed = last_embed(game_db)
    assert embed.title == "Stajnia" and embed.colour.value == 0x34cceb
    assert embed.description == (
        f"Oto Twoja stajnia, <@{player.id}> <:peepoBlush:984769061340737586>\n\n"
        "🐾 **Przy Tobie**\n"
        "**O'Burek** · Smok · poziom 3\n\n"
        "🐴 **W stajni**\n"
        "**1.** **Towarzysz** · jajko\n"
        "**2.** *brak danych w tabeli PETS*\n"
        "**3.** *wolne miejsce*\n"
        "**4.** *wolne miejsce*")
    assert embed.footer.text == ("Możesz schować lub wyciągnąć peta za pomocą komend !schowajtowarzysza "
                                 "oraz !wyciagnijtowarzysza.")
    assert embed.thumbnail.url == "https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png"


# -- ranking --------------------------------------------------------------------------

async def test_pet_ranking(game_db):
    bot, db = game_db.bot, game_db.bot.db
    await pets.create_pets_table(bot)                                   # the "Default" row is not a pet
    await add_pet(db, 1, pet_name="O'Neil the Magnificent Dragon", pet_lvl=3, pet_skills=9, quality="Premium",
                  type="Dragon", shiny=True, ultra_shiny=True)
    await add_pet(db, 2, pet_name="Pustka", pet_lvl=3, pet_skills=6, quality="Mythic", type="Void", shiny=True,
                  ultra_shiny=True, mythic=True, slow_perc=30)
    await add_pet(db, 3, pet_name="Mruczek", pet_lvl=2, pet_skills=6, slow_perc=5)
    await add_pet(db, 4, pet_lvl=0, type="Hydra", shiny=None, ultra_shiny=None)
    for pet_id in range(10, 20):
        await add_pet(db, pet_id, pet_skills=0, slow_perc=0)
    await pets.pet_ranking(game_db.scene())
    embed = last_embed(game_db)
    lines = embed.description.splitlines()
    assert len(lines) == 20                                  # ten pets, two short lines each
    assert lines[:8] == [
        "🥇 **O'Neil the Magnifice** · Smok · poziom 3",
        "┗ Talent: **Boski** · 🌟 Ultra shiny · 💎 Premium",
        "🥈 **Pustka** · Istota pustki · poziom 3",
        "┗ Talent: **Oświecony** · 🔮 Mityczny",
        "🥉 **Mruczek** · Kot · poziom 2",
        "┗ Talent: **Oświecony**",                           # nothing special: nothing listed
        "**4.** **Towarzysz** · jajko",                      # level 0, a type the bot has no name for
        "┗ Talent: **Miernota**",
    ]
    assert lines[-2].startswith("**10.** ") and "Nie" not in embed.description and "Tak" not in embed.description
    assert "Default" not in embed.description
    assert embed.title == "Najbardziej utalentowani towarzysze!" and embed.colour.value == 0xFF0000
    assert embed.footer.text == "Gratulacje dla posiadaczy!"
    assert embed.url == embed.thumbnail.url == (
        "https://www.altermmo.pl/wp-content/uploads/alter0000_Adventurer_with_a_small_dragon_pet._Fantasy."
        "_Pixel_ar_fac9f513-e338-4727-af61-32b79dbfa7e3.png")

    await db.execute("DELETE FROM PETS")
    await pets.pet_ranking(game_db.scene())                              # an empty ranking is still an answer
    assert last_embed(game_db).description == pets.MSG_NO_PETS_YET


def _ranked(**columns) -> dict:
    """A row of the ranking's query (lower-case columns)."""
    return {"pet_name": "Towarzysz", "pet_lvl": 0, "pet_skills": 5, "quality": "Standard", "shiny": False,
            "type": "Cat", "ultra_shiny": False, "mythic": False, **columns}


@pytest.mark.parametrize("species", [kind.value for kind in pets.PetType])
def test_an_egg_in_the_pet_ranking_does_not_tell_its_species(species):
    """Owner, 2026-11-23: an egg's species is not known before it hatches - in the whole game, so in the
    ranking of the most talented pets as well.  The egg keeps its name, its talent and its badges."""
    polish = pets.POLISH_TYPES[species]
    for level in (0, None):                                              # (NULL is the egg, as everywhere)
        egg = pets._ranking_entry(4, _ranked(type=species, pet_lvl=level))
        assert egg == "**4.** **Towarzysz** · jajko\n┗ Talent: **Mistrz**"
        assert not any(name in egg for name in pets.POLISH_TYPES.values()) and species not in egg
    named = pets._ranking_entry(1, _ranked(type=species, pet_name="Iskra", shiny=True, quality="Premium"))
    assert named == "🥇 **Iskra** · jajko\n┗ Talent: **Mistrz** · ✨ Shiny · 💎 Premium"
    # Once it hatched the species is told, as before.
    for level in range(1, pets.MAX_LEVEL + 1):
        grown = pets._ranking_entry(2, _ranked(type=species, pet_lvl=level))
        assert grown == f"🥈 **Towarzysz** · {polish} · poziom {level}\n┗ Talent: **Mistrz**"
    # The other lists of pets - the stable, its menu and the egg pouch - never named an egg's species;
    # the title of ``!towarzysz`` is "Jajko" (tests of the pet's embed).
    egg = _ranked(type=species, pet_name="Iskra")
    assert pets._pet_brief(egg) == "Iskra · jajko" and pets._pet_line(egg) == "**Iskra** · jajko"
    assert pets._pet_brief({**egg, "pet_lvl": 2}) == f"Iskra · {polish} · poziom 2"


async def test_the_pet_ranking_names_the_species_of_hatched_pets_only(game_db):
    bot, db = game_db.bot, game_db.bot.db
    await add_pet(db, 1, pet_name="Iskra", pet_lvl=0, pet_skills=9, quality="Premium", type="Dragon", shiny=True)
    await add_pet(db, 2, pet_lvl=0, pet_skills=8, quality="Mythic", type="Void", shiny=True, ultra_shiny=True,
                  mythic=True)
    await add_pet(db, 3, pet_lvl=None, pet_skills=7, type="Dog")
    await add_pet(db, 4, pet_name="Burek", pet_lvl=1, pet_skills=6, type="Dog")
    await pets.pet_ranking(game_db.scene())
    text = last_embed(game_db).description
    assert text.splitlines() == [
        "🥇 **Iskra** · jajko",
        "┗ Talent: **Boski** · ✨ Shiny · 💎 Premium",
        "🥈 **Towarzysz** · jajko",
        "┗ Talent: **Pozaziemski** · 🔮 Mityczny",
        "🥉 **Towarzysz** · jajko",
        "┗ Talent: **Transcendentny**",
        "**4.** **Burek** · Pies · poziom 1",
        "┗ Talent: **Oświecony**",
    ]
    assert "Smok" not in text and "Istota pustki" not in text and text.count("Pies") == 1


# -- boss detection -------------------------------------------------------------------

async def detection_setup(game):
    db = game.bot.db
    members = [game.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    ala, ola, ela, ula = members
    await add_owner(db, ala.id, 1)
    await add_owner(db, ola.id, 2)
    await add_owner(db, ela.id, 3)                      # no detection
    await add_owner(db, ula.id, 0, pet_id_alt1=4)       # the detecting pet is in the stable
    await add_owner(db, 777, 5)                         # left the server
    await add_owner(db, 0, 6)                           # the legacy placeholder row
    for pet_id, detection in ((1, True), (2, True), (3, False), (4, True), (5, True), (6, True)):
        await add_pet(db, pet_id, pet_lvl=1, detection=detection)
    return members


@pytest.mark.parametrize("rarity, word", [(0, "zwykły"), (1, "rzadki"), (2, "epicki"), (3, "legendarny"),
                                         (4, "mityczny"), (7, "mityczny"), (-1, "zwykły")])
async def test_detecting_pets_warn_their_owners(game_db, rarity, word):
    ala, ola, ela, ula = await detection_setup(game_db)
    await pets.detect_boss(game_db.bot, rarity)
    assert ala.dms == ola.dms == [bossdetect.dm_warning(game_db.bot, f"Nadciąga boss! Będzie {word}.")]
    assert ela.dms == [] and ula.dms == []
    assert game_db.notice_channel.messages == [] and game_db.log_channel.messages == []


async def test_unreachable_player_does_not_stop_the_others(game_db):
    ala, ola, ela, ula = await detection_setup(game_db)

    async def closed(content=None, **kwargs):
        raise forbidden()

    ala.send = closed
    await pets.detect_boss(game_db.bot, 2)                              # must not raise
    assert ola.dms == [bossdetect.dm_warning(game_db.bot, "Nadciąga boss! Będzie epicki.")]
    notice = (f"<@{ala.id}> - Twój towarzysz: *Nadciąga boss! Odblokuj prywatne wiadomości, jeśli chcesz "
              "otrzymywać powiadomienia!* Nie chcesz ich wcale? Kliknij przycisk poniżej.")
    assert [m.content for m in game_db.log_channel.messages] == [notice]         # debug mode: the log channel
    assert game_db.notice_channel.messages == []

    game_db.bot.debug = False
    game_db.notice_channel.fail_with, game_db.notice_channel.fail_sends = RuntimeError("boom"), 1
    await pets.detect_boss(game_db.bot, 2)                              # even the notice failing is survived
    await pets.detect_boss(game_db.bot, 2)
    assert [m.content for m in game_db.notice_channel.messages] == [notice]
    assert len(ola.dms) == 3


async def test_discord_outage_during_detection_is_not_blamed_on_the_player(game_db):
    ala, ola, ela, ula = await detection_setup(game_db)

    async def outage(content=None, **kwargs):
        raise ConnectionError("network down")

    ala.send = outage
    await pets.detect_boss(game_db.bot, 1)
    assert ola.dms == [bossdetect.dm_warning(game_db.bot, "Nadciąga boss! Będzie rzadki.")]
    assert game_db.log_channel.messages == [] and game_db.notice_channel.messages == []


# -- the cog --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.pets")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
        channel.permissions_for = lambda member: member.guild_permissions
    guards._last_notice.clear()
    return game_db.bot.get_cog("Pets")


async def run(game, member, content: str, *, channel=None) -> FakeMessage:
    """``member`` types a command; it goes through discord.py's real command machinery."""
    channel = channel or game.channel
    message = FakeMessage(channel, member, content)
    message.edited_at = None
    message._state = None
    channel.messages.append(message)
    await game.bot.invoke(await game.bot.get_context(message))
    await game.settle()
    return message


def admin(game, name: str = "Alter") -> FakeMember:
    member = game.add_member(name)
    member.guild_permissions = discord.Permissions(administrator=True)
    return member


async def test_cog_registers_the_legacy_commands(cog):
    assert type(cog).__name__ == "PetsCog"
    table = {command.name: sorted(command.aliases) for command in cog.get_commands()}
    assert table == {
        "towarzysz": ["t"], "porzucam": [], "odrodzenie": ["o", "odr"],
        "mocneodrodzenie": ["mo", "mocneodr", "odr2", "odrodzenie2"], "oswiecenie": ["oświecenie"],
        "transformacja": ["transform"], "schowajtowarzysza": ["schowaj", "st"],
        "wyciagnijtowarzysza": ["wt", "wyciagnij", "wyciągnij", "wyciągnijtowarzysza"],
        "stajnia": ["s"], "rankingtowarzyszy": ["rt", "towarzysze"], "nazwij": ["n"],
        "wyczuwanie": ["powiadomienia", "powiadomieniaboss"], "wyjmijjajko": ["wj", "wyjmij"],
        "PetLvlUp": [], "AddScroll": [], "AddShard": [], "GenerateEgg": [],
        "createPetOwnersDatabase": [], "createPetsDatabase": [], "ReassignPet": [], "DajPieczec": [],
        "DajRamke": ["DajRamkę"]}
    cooldown = cog.name_pet._buckets._cooldown
    assert (cooldown.rate, cooldown.per, cog.name_pet._buckets.type) == (1, 23 * 3600, commands.BucketType.user)
    assert all(command._buckets._cooldown is None for command in cog.get_commands() if command.name != "nazwij")


async def test_display_commands_work_anywhere_gameplay_only_on_the_boss_channel(game_db, cog):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, reroll_scroll=3)
    await add_pet(db, 5, pet_lvl=1, pet_skills=1, crit_perc=4)
    elsewhere = game_db.announce_channel
    for command in ("!towarzysz", "!t", "!stajnia", "!s", "!rankingtowarzyszy", "!rt", "!towarzysze"):
        await run(game_db, player, command, channel=elsewhere)
    assert [m.embeds[0].title for m in elsewhere.messages if m.embeds] == [
        "Kot", "Kot", "Stajnia", "Stajnia", *["Najbardziej utalentowani towarzysze!"] * 3]

    for command in ("!porzucam", "!odrodzenie", "!o", "!mocneodrodzenie", "!oswiecenie", "!transformacja",
                    "!schowajtowarzysza 1", "!wyciagnijtowarzysza 1", "!nazwij Burek"):
        await run(game_db, player, command, channel=elsewhere)
    assert len(elsewhere.bot_messages) == 7 and game_db.channel.bot_messages == [game_db.channel.messages[0]]
    row = await owner_row(db, player.id)
    assert (row["pet_id"], row["pet_id_alt1"]) == (5, 0) and (await pet_row(db, 5))["pet_name"] == "Towarzysz"

    await run(game_db, player, "!schowaj 2")                             # ... and on the boss channel they work
    await run(game_db, player, "!wt 2")
    assert game_db.channel.contains("ulokowany w stajni") and game_db.channel.contains("wyciągnięty ze stajni")


async def test_commands_are_blocked_with_a_notice_while_the_bot_is_busy(game_db, cog):
    player = game_db.add_member("Ala")
    await add_owner(game_db.bot.db, player.id, 5)
    await add_pet(game_db.bot.db, 5)
    with game_db.bot.state.hold_busy("event:TEST"):
        for command in ("!towarzysz", "!stajnia", "!rt", "!porzucam", "!odr", "!mo", "!oświecenie", "!transform",
                        "!st 1", "!wyciągnij 1", "!n Burek"):
            await run(game_db, player, command)
    assert [m.content for m in game_db.channel.bot_messages[1:]] == [guards.BUSY_NOTICE]
    assert (await pet_row(game_db.bot.db, 5))["pet_name"] == "Towarzysz"


async def test_commands_run_the_confirmed_flows(game_db, cog, monkeypatch, sacrifices):
    db = game_db.bot.db
    player = patron(game_db)
    await add_owner(db, player.id, 5, reroll_scroll=1, rebirth_stones=1, mirrors=1)
    await add_pet(db, 5, pet_lvl=3, pet_skills=3, crit_perc=4)
    rig(monkeypatch, choices=[PetType.DOG], randint=1)
    confirms(game_db, player)
    await run(game_db, player, "!odr")
    await run(game_db, player, "!oswiecenie")
    await run(game_db, player, "!transformacja")
    row = await owner_row(db, player.id)
    assert (row["reroll_scroll"], row["rebirth_stones"], row["mirrors"]) == (0, 0, 0)
    assert (await pet_row(db, 5))["pet_skills"] == 4
    await run(game_db, player, "!porzucam")
    assert await pet_row(db, 5) is None and len(sacrifices) == 1


async def test_advanced_reroll_command_holds_and_releases_the_channel(game_db, cog, monkeypatch):
    player = await reroll_setup(game_db, scrolls=10)
    player.guild_permissions = discord.Permissions(send_messages=True)  # may play (see the cog fixture)
    other = game_db.add_member("Ola")
    rig(monkeypatch)
    confirms(game_db, player)
    task = asyncio.ensure_future(run(game_db, player, "!mo"))
    menu = await game_db.wait_for_buttons("pet:skip")
    assert game_db.bot.state.busy and game_db.bot.state.busy_owner == "adv-reroll"
    await run(game_db, other, "!towarzysz")                              # blocked meanwhile
    assert game_db.channel.contains(guards.BUSY_NOTICE)
    await choose(game_db, menu, player, 6)
    await game_db.click(menu, player, "pet:skip-accept")
    await asyncio.wait_for(task, 5)
    assert not game_db.bot.state.busy
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 0

    # Discord failing in the middle: the channel is free again and the owner hears about it.
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("boom"), 1
    await run(game_db, player, "!mocneodrodzenie")
    assert not game_db.bot.state.busy
    assert any("mocneodrodzenie" in line and "RuntimeError" in line for line in game_db.bot.owner_log)


async def test_stable_commands_explain_a_missing_slot(game_db, cog):
    player = game_db.add_member("Ala")
    await run(game_db, player, "!schowajtowarzysza")
    assert game_db.channel.messages[-1].content == (
        "Po spacji podaj numer miejsca w stajni, do którego chcesz schować towarzysza (1-4) np. "
        "**!schowajtowarzysza 1**.")
    await run(game_db, player, "!wyciagnijtowarzysza")
    assert game_db.channel.messages[-1].content == (
        "Po spacji podaj numer miejsca w stajni, z którego chcesz wyjąć towarzysza (1-4) np. "
        "**!wyciagnijtowarzysza 1**.")
    await run(game_db, player, "!st abc")
    assert game_db.channel.messages[-1].content.startswith(ALONE)


async def test_naming_cooldown_counts_only_successful_namings(game_db, cog, monkeypatch):
    db = game_db.bot.db
    player, other = game_db.add_member("Ala"), game_db.add_member("Ola")
    rig(monkeypatch, randint=0)
    await run(game_db, player, "!nazwij")                                # no name: usage hint, no cooldown
    assert game_db.channel.messages[-1].content.startswith("Niepoprawne użycie komendy.")
    await run(game_db, player, "!nazwij Burek")                          # no pet: no cooldown either
    assert game_db.channel.messages[-1].content == "Przecież jesteś sam... <:madge:882184635474386974>"

    await add_owner(db, player.id, 5)
    await add_pet(db, 5)
    await run(game_db, player, '!n "Sir O\'Burek"')
    assert (await pet_row(db, 5))["pet_name"] == "Sir O'Burek"
    await run(game_db, player, "!nazwij Reksio")
    assert game_db.channel.messages[-1].content.startswith(
        "Poczekaj na odnowienie komendy! Imię zmienisz ponownie <t:")
    stamp = int(game_db.channel.messages[-1].content.split("<t:")[1].split(":")[0])
    assert abs(stamp - (time.time() + 23 * 3600)) < 60                   # Discord's live "in 23 hours"
    assert (await pet_row(db, 5))["pet_name"] == "Sir O'Burek"
    # A refused command (wrong channel, busy) neither uses nor clears the running cooldown.
    await run(game_db, player, "!nazwij Reksio", channel=game_db.announce_channel)
    await run(game_db, player, "!nazwij Reksio")
    assert game_db.channel.messages[-1].content.startswith("Poczekaj na odnowienie komendy!")

    await add_owner(db, other.id, 6)                                     # the cooldown is per player
    await add_pet(db, 6)
    await run(game_db, other, "!nazwij Reksio")
    assert (await pet_row(db, 6))["pet_name"] == "Reksio"


async def test_admin_commands(game_db, cog, monkeypatch):
    db = game_db.bot.db
    boss, player = admin(game_db), game_db.add_member("Ala")
    rig(monkeypatch, randint=0)
    for command in (f"!AddScroll 3 {player.id}", f"!AddShard 2 {player.id}", "!GenerateEgg",
                    f"!ReassignPet 1 {player.id}", f"!PetLvlUp {player.id}", "!createPetsDatabase",
                    "!createPetOwnersDatabase", f"!DajPieczec {player.id} 3 tak", f"!DajRamke {player.id} S1-A"):
        await run(game_db, player, command)                              # not an administrator: ignored
    assert await count(db, "PETOWNER") == 0 and await count(db, "PETS") == 0
    assert game_db.channel.bot_messages == [game_db.channel.messages[0]]

    await run(game_db, boss, "!createPetOwnersDatabase")
    assert game_db.channel.messages[-1].content == "Baza danych PETOWNERS utworzona."
    await run(game_db, boss, "!createPetsDatabase")
    assert game_db.channel.messages[-1].content == "Baza danych PETS utworzona."
    assert await count(db, "PETOWNER") == 1 and await count(db, "PETS") == 1

    message = await run(game_db, boss, f"!AddScroll 3 {player.id}")
    await run(game_db, boss, f"!AddShard 2 {player.id}")
    row = await owner_row(db, player.id)
    assert (row["reroll_scroll"], row["reroll_scroll_shard"]) == (3, 2) and message.reactions == ["✅"]
    await run(game_db, boss, "!GenerateEgg")
    assert game_db.channel.messages[-1].content == "Wygenerowano jajko (PET_ID 1)."
    assert (await run(game_db, boss, f"!PetLvlUp {player.id}")).reactions == ["❌"]         # no pet yet
    await run(game_db, boss, f"!ReassignPet 1 {player.id}")
    assert game_db.channel.messages[-1].content == f"Pet 1 przypisany do gracza <@{player.id}>."
    await run(game_db, boss, f"!ReassignPet 1 {player.id}")
    assert game_db.channel.messages[-1].content.startswith("Pet 1 nie został przypisany")
    assert (await run(game_db, boss, f"!PetLvlUp {player.id}")).reactions == ["✅"]
    assert (await pet_row(db, 1))["pet_lvl"] == 1
    await run(game_db, boss, "!AddScroll dużo")                          # bad arguments: a usage hint
    assert game_db.channel.messages[-1].content.startswith("Niepoprawne użycie komendy.")
    assert "AddColumn" not in game_db.bot.all_commands                   # the raw ALTER TABLE command is gone


# -- the companion missions' foundations: the Duża sakwa and the cores (features/missions.py) ----

def test_the_pouch_of_a_row():
    small = {"egg_pouch": True, "pouch_pet_id": Decimal(0)}
    big = {"egg_pouch": True, "big_pouch": True, "pouch_pet_id": 7, "pouch_pet_id2": 0}
    assert pets._pouch(None) == pets._pouch({"pet_id": 1}) == pets.Pouch(0, (), None)   # no row / no columns
    assert pets._pouch({"egg_pouch": False, "pouch_pet_id": 0}) == pets.Pouch()
    assert pets._pouch(small) == pets.Pouch(1, (), "POUCH_PET_ID")
    assert pets._pouch(small | {"pouch_pet_id": Decimal(7)}) == pets.Pouch(1, (("POUCH_PET_ID", 7),), None)
    # Read without the mission columns, a pouch is the merchant's one-egg pouch.
    assert pets._pouch({"egg_pouch": True, "pouch_pet_id": 7}).size == 1
    assert pets._pouch(big) == pets.Pouch(2, (("POUCH_PET_ID", 7),), "POUCH_PET_ID2")
    assert pets._pouch(big | {"pouch_pet_id": 0, "pouch_pet_id2": 8}) == pets.Pouch(
        2, (("POUCH_PET_ID2", 8),), "POUCH_PET_ID")                              # each slot on its own
    assert pets._pouch(big | {"pouch_pet_id2": 8}) == pets.Pouch(2, (("POUCH_PET_ID", 7), ("POUCH_PET_ID2", 8)), None)
    assert pets._pouch(big | {"egg_pouch": False}) == pets.Pouch()               # the big flag alone is no pouch
    assert pets._pouch(small | {"big_pouch": False, "pouch_pet_id2": 9}).eggs == ()   # slot 2 of a small one


def test_the_owner_columns_with_the_big_pouch():
    assert "BIG_POUCH" not in pets._owner_columns(True) and "BIG_POUCH" not in pets._owner_columns(False, True)
    assert pets._owner_columns(True, True).endswith("STABLE_PLACES, EGG_POUCH, POUCH_PET_ID, BIG_POUCH, POUCH_PET_ID2")
    assert all("SUMMON_SCROLLS" not in pets._owner_columns(upgrades, big)
               for upgrades in (False, True) for big in (False, True))


async def test_the_mission_columns_are_looked_up_once_and_again_after_a_no(game_db):
    from altermmo import schema

    bot, db = game_db.bot, game_db.bot.db
    assert await pets.mission_columns_available(bot) is True
    for column in pets.MISSION_COLUMNS:
        await db.execute(f"ALTER TABLE PETOWNER DROP COLUMN {column}", idempotent=True)
    assert await pets.mission_columns_available(bot, refresh=True) is True     # a "yes" is never asked again
    bot._mission_columns = None                                                # (a new run)
    assert await pets.mission_columns_available(bot) is False
    assert await pets.upgrades_available(bot) is True                          # the merchant's goods stay on
    assert await schema.add_new_columns(db) == []                              # what the next start does
    assert await pets.mission_columns_available(bot) is False                  # a "no" is remembered ...
    assert await pets.mission_columns_available(bot, refresh=True) is True     # ... until looked at again


async def test_without_the_mission_columns_the_pouch_holds_one_egg_as_before(game_db, monkeypatch):
    game, bot, db = game_db, game_db.bot, game_db.bot.db
    for column in pets.MISSION_COLUMNS:
        await db.execute(f"ALTER TABLE PETOWNER DROP COLUMN {column}", idempotent=True)
    rig(monkeypatch, randint=0)
    ala = game.add_member("Ala")
    await add_owner(db, ala.id, 1, egg_pouch=True)
    await add_pet(db, 1, pet_lvl=1)
    assert await pets.mission_columns_available(bot) is False and await pets.pouch_size(bot, ala.id) == 1
    assert await pets.egg_room(bot, ala.id) == "pouch"
    assert await pets.assign_pet(game.scene(ala), ala) == pets.INTO_POUCH
    assert game.channel.messages[-1].content == pets.MSG_EGG_TO_POUCH.format(player_id=ala.id)
    assert await pets.egg_room(bot, ala.id) == "" and await pets.assign_pet(game.scene(ala), ala) is False
    async with db.transaction() as con:
        assert await pets.upgrade_pouch_in(con, ala.id, upgrades=True, big=False) == ""   # compensated
    embed, options = await pets._stable_view(bot, ala.id)
    assert pets.STABLE_POUCH in embed.description and [option.value for option in options] == ["pouch:2"]
    assert await pets.owned_pet_ids(bot, ala.id) == [1, 2] and await pets.summon_scrolls(bot, ala.id) == 0
    assert await pets.take_egg_from_pouch(game.scene(ala)) is True
    assert game.channel.messages[-1].content == pets.MSG_POUCH_SWAPPED.format(player_id=ala.id, slot=1)


async def test_the_big_pouch_holds_two_eggs(game_db, monkeypatch):
    game, bot, db = game_db, game_db.bot, game_db.bot.db
    rig(monkeypatch, randint=0)
    ala = game.add_member("Ala")
    await add_owner(db, ala.id, 1, egg_pouch=True, big_pouch=True)
    await add_pet(db, 1, pet_lvl=1, pet_name="Mruczek")
    scene = game.scene(ala)
    assert await pets.pouch_size(bot, ala.id) == 2 and await pets.egg_room(bot, ala.id) == "pouch"

    # Drop eggs fill slot 1, then slot 2 - the pet at her side stays.
    assert await pets.assign_pet(scene, ala) == pets.INTO_POUCH
    assert game.channel.messages[-1].content == pets.MSG_EGG_TO_BIG_POUCH.format(player_id=ala.id, filled=1)
    assert (await pets.egg_places(bot, ala.id)).pouch is True
    assert await pets.assign_pet(scene, ala) == pets.INTO_POUCH
    assert "nowe jajko trafia do sakwy (2/2)" in game.channel.messages[-1].content
    row = await owner_row(db, ala.id)
    assert (row["pet_id"], row["pouch_pet_id"], row["pouch_pet_id2"]) == (1, 2, 3)
    assert await pets.egg_room(bot, ala.id) == "" and (await pets.egg_places(bot, ala.id)).pouch is False
    assert await pets.assign_pet(scene, ala) is False                          # full: the drop is lost
    assert await pets.owned_pet_ids(bot, ala.id) == [1, 2, 3]

    # The stable shows both eggs, each with an entry of its own.
    embed, options = await pets._stable_view(bot, ala.id)
    assert pets.STABLE_BIG_POUCH.format(filled=2) in embed.description and pets.STABLE_POUCH not in embed.description
    assert [option.value for option in options] == ["pouch:2", "pouch:3"]

    # The stable list takes the egg of slot 2 by its id; slot 1 keeps its egg.
    assert await pets._take_from_pouch(bot, ala.id, 3) == ("swapped", 1)
    row = await owner_row(db, ala.id)
    assert (row["pet_id"], row["pet_id_alt1"], row["pouch_pet_id"], row["pouch_pet_id2"]) == (3, 1, 2, 0)
    assert await pets._take_from_pouch(bot, ala.id, 3) == ("changed", 0)
    embed, _options = await pets._stable_view(bot, ala.id)
    assert pets.STABLE_BIG_POUCH.format(filled=1) in embed.description

    # A new egg fills the free slot 2 again; !wyjmijjajko takes slot 1 first and says what is left.
    assert await pets.assign_pet(scene, ala) == pets.INTO_POUCH
    assert (await owner_row(db, ala.id))["pouch_pet_id2"] == 4
    assert await pets.take_egg_from_pouch(scene) is True
    assert game.channel.messages[-1].content == (pets.MSG_POUCH_SWAPPED.format(player_id=ala.id, slot=2)
                                                 + " W sakwie czeka jeszcze jedno jajko.")
    row = await owner_row(db, ala.id)
    assert (row["pet_id"], row["pet_id_alt2"], row["pouch_pet_id"], row["pouch_pet_id2"]) == (2, 3, 0, 4)
    assert await pets.take_egg_from_pouch(scene) is True
    assert game.channel.messages[-1].content == pets.MSG_POUCH_SWAPPED.format(player_id=ala.id, slot=3)
    assert await pets.take_egg_from_pouch(scene) is False
    assert game.channel.messages[-1].content == pets.MSG_POUCH_EMPTY.format(player_id=ala.id)


async def test_an_egg_placed_anywhere_goes_to_the_side_then_both_pouch_slots_then_the_stable(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    rig(monkeypatch, randint=0)
    ala = game.add_member("Ala")
    await add_owner(db, ala.id, 0, egg_pouch=True, big_pouch=True, pet_id_alt1=50, pet_id_alt2=51, pet_id_alt3=52)
    placed = [await pets.place_egg(game.scene(ala), ala, pets.roll_egg(ala), pets.EGG_ANYWHERE) for _ in range(5)]
    assert [egg.status for egg in placed] == [pets.EGG_ACTIVE, pets.EGG_IN_POUCH, pets.EGG_IN_POUCH,
                                              pets.EGG_IN_STABLE, pets.EGG_NO_ROOM]
    assert placed[3].place == 4 and placed[4].pet_id == 0
    row = await owner_row(db, ala.id)
    assert (row["pet_id"], row["pouch_pet_id"], row["pouch_pet_id2"], row["pet_id_alt4"]) == tuple(
        egg.pet_id for egg in placed[:4])


async def test_the_hooks_of_a_new_egg_never_raise(game_db, monkeypatch):
    game = game_db
    rig(monkeypatch, randint=lambda low, high: 1000 if high == 1000 else 0)  # a mythic egg
    ala = game.add_member("Ala")
    egg = pets.roll_egg(ala, cosmetics=False)
    scene = game.scene(ala)
    before = len(game.channel.messages)
    await pets.after_new_egg(scene, ala, ala.id, 1, egg, False, False, announce=False)
    assert len(game.channel.messages) == before                                # a quiet channel: no word
    await pets.after_new_egg(scene, None, ala.id, 1, egg, False, False, announce=True)   # (he left the server)
    assert game.channel.messages[-1].content == (
        f"Łowca <@{ala.id}> zdobył mitycznego towarzysza! <:POGGERS:936907543849078844>")

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    noted: list = []

    async def note(scene, player, egg, *, into=None):
        noted.append(into)

    monkeypatch.setattr(pets.achievements, "check_pets", broken)
    monkeypatch.setattr(pets, "_album_note", note)
    await pets.after_new_egg(scene, ala, ala.id, 1, egg, False, False, announce=False, notes=["drop"])
    assert noted == [["drop"]]                                                 # the note comes all the same
    await pets.after_new_egg(scene, ala, ala.id, 1, egg, False, False, announce=False, album=False)
    assert noted == [["drop"]]


async def test_the_counters_of_a_mission_change_inside_the_callers_transaction(game_db):
    db = game_db.bot.db
    await add_owner(db, 1, 0, reroll_scroll=10, reroll_scroll_shard=None, rebirth_stones=1)
    async with db.transaction() as con:
        await pets._lock_player(con, 1)
        await pets.add_items_in(con, 1, scrolls=2, shards=3, stones=1, mirrors=4)
        await pets.add_items_in(con, 2, scrolls=1, mirrors=1)                  # no row yet: created
        assert await pets.take_scrolls_in(con, 1, 13) is None                  # 12 are not enough: nothing taken
        assert await pets.take_scrolls_in(con, 1, 5) == 7
        assert await pets.take_scrolls_in(con, 3, 1) is None                   # no row
        assert [await pets.add_stable_place_in(con, 1, 6) for _ in range(3)] == [5, 6, 0]
        assert await pets.add_stable_place_in(con, 3) == 0
    row = await owner_row(db, 1)
    assert (row["reroll_scroll"], row["reroll_scroll_shard"], row["rebirth_stones"], row["mirrors"],
            row["stable_places"]) == (7, 3, 2, 4, 6)
    row = await owner_row(db, 2)
    assert (row["pet_id"], row["pet_owned"], row["reroll_scroll"], row["reroll_scroll_shard"], row["rebirth_stones"],
            row["mirrors"]) == (0, False, 1, 0, 0, 1)
    with pytest.raises(RuntimeError):
        async with db.transaction() as con:
            await pets.add_items_in(con, 1, scrolls=100)
            raise RuntimeError("the claim failed later")
    assert (await owner_row(db, 1))["reroll_scroll"] == 7                     # rolled back with the rest
    # The old one-counter writers and the merchant's purchase are wrappers of the cores now.
    assert await pets.assign_mirror(game_db.scene(), 2, 1) is True
    assert await pets.take_scrolls(game_db.bot, 1, 8) == (False, 7)
    assert await pets.take_scrolls(game_db.bot, 1, 7) == (True, 0)
    assert (await owner_row(db, 1))["mirrors"] == 6
    with pytest.raises(ValueError):
        await pets._add_item(game_db.bot, "PET_ID", 1, 1)


async def test_the_pouch_step_of_a_mission(game_db):
    bot, db = game_db.bot, game_db.bot.db
    await add_owner(db, 1, 0)

    async def step(player_id: int, *, upgrades: bool = True, big: bool = True) -> str:
        async with db.transaction() as con:
            await pets._lock_player(con, player_id)
            return await pets.upgrade_pouch_in(con, player_id, upgrades=upgrades, big=big)

    assert await step(1, upgrades=False) == ""                                # the columns are missing
    assert await step(1) == "pouch" and await pets.pouch_size(bot, 1) == 1
    assert await step(1, big=False) == ""                                     # no Duża sakwa without its columns
    assert await step(1) == "big" and await pets.pouch_size(bot, 1) == 2
    assert await step(1) == ""                                                # the Duża sakwa already
    assert await step(2) == "" and await owner_row(db, 2) is None            # no row: the caller compensates
    row = await owner_row(db, 1)
    assert (row["egg_pouch"], row["big_pouch"]) == (True, True) and await pets.has_egg_pouch(bot, 1) is True


async def test_summoning_scrolls(game_db):
    bot, db = game_db.bot, game_db.bot.db
    await add_owner(db, 1, 0)

    async def add(player_id: int, number: int = 1) -> int:
        async with db.transaction() as con:
            await pets._lock_player(con, player_id)
            return await pets.add_summon_scroll_in(con, player_id, number)

    assert [await add(1) for _ in range(4)] == [1, 2, 3, 4]                   # no cap (owner, 2026-11-26)
    assert await add(2, 2) == 2 and (await owner_row(db, 2))["summon_scrolls"] == 2   # no row: it is made
    assert await pets.summon_scrolls(bot, 1) == 4
    assert await pets.take_summon_scroll(bot, 1) == 3
    await pets.return_summon_scroll(bot, 1)
    assert await pets.summon_scrolls(bot, 1) == 4
    assert [await pets.take_summon_scroll(bot, 1) for _ in range(5)] == [3, 2, 1, 0, None]
    assert await pets.give_summon_scrolls(bot, 1, 25) == 25                  # the owner's command: no cap either
    assert await pets.give_summon_scrolls(bot, 1, -30) == 0                  # a minus takes them, never below 0
    assert await pets.give_summon_scrolls(bot, 9, 2) == 2                    # a player without a row gets one
    assert await pets.give_summon_scrolls(bot, 8, -1) == 0                   # ... but not for a minus
    assert (await owner_row(db, 9))["summon_scrolls"] == 2 and await count(db, "PETOWNER") == 3
    bot._mission_columns = False
    assert await pets.summon_scrolls(bot, 1) == 0                             # without the columns: none


async def test_the_level_of_a_mission_goes_to_the_pet_that_went(game_db, monkeypatch):
    db = game_db.bot.db
    await add_owner(db, 1, 5, pet_id_alt2=6, pet_id_alt3=8, egg_pouch=True, pouch_pet_id=7)
    await add_pet(db, 5, pet_lvl=1, pet_skills=1, type="Bear")
    await add_pet(db, 6, pet_lvl=2, pet_skills=3, type="Cat", variant="1", crit_perc=4)
    await add_pet(db, 7, pet_lvl=0, type="Dog")                                # the egg in the pouch
    await add_pet(db, 8, pet_lvl=3, type="Cat")                                # at the top already
    await add_pet(db, 9, pet_lvl=1, type="Cat")                                # nobody's
    await add_owner(db, 2, 10)
    await add_pet(db, 10, pet_lvl=0, type="Cat")                               # an egg at the side
    rig(monkeypatch, choices=["CRIT_PERC", "CRIT_PERC"], randint=lambda low, high: 2 if high == 2 else low)

    async def level(player_id: int, pet_id: int, species: str = "Cat"):
        async with db.transaction() as con:
            await pets._lock_player(con, player_id)
            return await pets.level_mission_pet_in(con, player_id, pet_id, upgrades=True, big=True, species=species)

    after, new_level = await level(1, 6)                                       # sent from the stable
    assert new_level == 3 and (pets._int(after["pet_lvl"]), pets._int(after["pet_skills"])) == (3, 5)
    assert (pets._int(after["crit_perc"]), after["variant"]) == (10, "1")
    assert (await level(1, 5, "Bear"))[1] == 2                                 # sent from her side
    assert await level(1, 6) is None                                           # level 3 is the top
    assert await level(1, 8) is None
    assert await level(1, 7, "Dog") is None                                    # the pouch never grows
    assert await level(1, 9) is None                                           # not hers
    assert await level(1, 5, "Cat") is None                                    # another species: a reused PET_ID
    assert await level(2, 10) is None                                          # an egg
    assert await level(3, 5, "Bear") is None and await level(1, 0) is None
    assert (await pet_row(db, 5))["pet_lvl"] == 2 and (await pet_row(db, 9))["pet_lvl"] == 1


@pytest.mark.parametrize("roll, min_class, flags", [
    (0, 0, (False, False, False)), (0, 900, (True, False, False)), (899, 900, (True, False, False)),
    (950, 900, (True, False, False)), (985, 900, (True, True, False)), (0, 980, (True, True, False)),
    (979, 980, (True, True, False)), (998, 900, (True, True, True)), (1000, 980, (True, True, True))])
def test_min_class_draws_a_class_below_it_again(monkeypatch, roll, min_class, flags):
    pools, ranges = rig(monkeypatch, randint=lambda low, high: roll if high == 1000 else low)
    egg = pets.roll_egg(FakeMember("Ala"), cosmetics=False, min_class=min_class)
    assert (egg["SHINY"], egg["ULTRA_SHINY"], egg["MYTHIC"]) == flags
    redrawn = 0 < min_class and roll < min_class
    assert ranges[:2] == [(0, 1000), (min_class, pets.MYTHIC_FROM - 1)] if redrawn else (
        (min_class, pets.MYTHIC_FROM - 1) not in ranges)


def test_min_class_zero_draws_exactly_what_the_game_always_drew():
    member = FakeMember("Ala")
    for seed in range(200):
        random.seed(seed)
        expected = pets._roll_egg(member)
        state = random.getstate()
        random.seed(seed)
        assert pets.roll_egg(member, min_class=0) == expected and random.getstate() == state


@pytest.mark.parametrize("min_class, percent", [
    (0, {"ordinary": 89.91, "shiny": 7.99, "ultra": 1.80, "mythic": 0.30}),
    (900, {"shiny": 81.39, "ultra": 18.31, "mythic": 0.30}),
    (980, {"ultra": 99.70, "mythic": 0.30})])
def test_min_class_odds_by_enumeration(monkeypatch, min_class, percent):
    """Every class roll, and every second roll it can lead to: the exact odds of a mission egg."""
    from collections import Counter
    from fractions import Fraction

    script: list[int] = []
    asked: list[tuple[int, int]] = []

    def randint(low, high):
        asked.append((low, high))
        return script.pop(0) if script else low

    monkeypatch.setattr(random, "randint", randint)
    monkeypatch.setattr(random, "choice", lambda pool: list(pool)[0])

    def draw(*rolls: int) -> str:
        script[:], asked[:] = list(rolls), []
        egg = pets._roll_egg(None, cosmetics=False, min_class=min_class)
        return "mythic" if egg["MYTHIC"] else "ultra" if egg["ULTRA_SHINY"] else "shiny" if egg["SHINY"] else "ordinary"

    odds: Counter = Counter()
    redrawn = 0
    for first in range(1001):
        kind = draw(first)
        if 0 < min_class and first < min_class:
            assert asked[1] == (min_class, pets.MYTHIC_FROM - 1)           # again, from min_class, never mythic
            redrawn += 1
        else:
            odds[kind] += Fraction(1, 1001)
    second = range(min_class, pets.MYTHIC_FROM)
    for roll in second if redrawn else ():
        odds[draw(0, roll)] += Fraction(redrawn, 1001 * len(second))
    assert sum(odds.values()) == 1 and odds["mythic"] == Fraction(3, 1001)    # mythic never rises
    assert {kind: round(float(share) * 100, 2) for kind, share in odds.items()} == percent
