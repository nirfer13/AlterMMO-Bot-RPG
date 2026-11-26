"""Aura towarzysza (features/auras.py, features/pets.py; owner, 2026-11-26, items_decisions.md §8).

The rules without a database first (the config, the draws, the sums, the texts), then the game against a
real PostgreSQL: the roll at hatching, the growth, the fight stats (every hatched pet counts, with or
without a pet at the player's side, no cap but the per-pet maximum), what never touches an aura, the
sacrifice, ``!towarzysz``, ``!stajnia``, ``!ReassignPet`` and the owner's ``!PetLvlUp``.  Every test that
wants an aura writes its own ``auraConfig.json`` (``aura_config`` in conftest.py): the autouse fixture keeps
the chance at 0 (the shipped file has the owner's 1%, which no test but its pin relies on).
"""

from __future__ import annotations

import json
import logging
import random
from decimal import Decimal

import pytest

from altermmo.features import auras, boss, daily, missions, pets
from altermmo.settings import CONFIG_DIR

from test_pet_away import send_away
from test_pets import add_owner, add_pet, admin, cog, confirms, last_embed, owner_row, patron, pet_row, rig, run  # noqa: F401
from test_pets import plain_cards, sacrifices  # noqa: F401 - autouse: plain pictures; the sacrifice's modifier

MROZ, FURIA, SZCZESCIE, OPIEKA = (auras.KINDS[stat] for stat in ("SLOW_PERC", "CRIT_PERC", "DROP_PERC", "DEF_PERC"))


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a !potwierdzam slot was never released"


def draws(monkeypatch, *answers: int, default: int = 0) -> list[int]:
    """Script ``random.randrange``; returns the list of the sizes asked for."""
    asked: list[int] = []
    script = iter(answers)

    def randrange(stop, *args, **kwargs):
        asked.append(stop)
        return next(script, default)

    monkeypatch.setattr(random, "randrange", randrange)
    return asked


# =========================================== the rules ============================================

def test_the_owners_table_is_in_the_code():
    """Start, step and the most ONE pet's aura can reach are code constants (owner's table of 2026-11-26)."""
    assert [(kind.stat, kind.name, kind.emoji, kind.start, kind.step, kind.max) for kind in auras.KINDS.values()] == [
        ("SLOW_PERC", "Aura Mrozu", "❄️", 3, 2, 7), ("CRIT_PERC", "Aura Furii", "🔥", 2, 1, 4),
        ("DROP_PERC", "Aura Szczęścia", "🍀", 3, 2, 7), ("DEF_PERC", "Aura Opieki", "🛡️", 1, 1, 3)]
    assert auras.DEFAULT_WEIGHTS == {"mroz": 35, "furia": 30, "szczescie": 25, "opieka": 10}
    # Roll sizes nobody else uses (the fixtures that fix an egg's cosmetic rolls know 1000 and 10000).
    assert auras.AURA_ROLL not in (pets.COSMETIC_ROLL, pets.FRAME_ROLL, 100) and (auras.GROWTH_ROLL,
                                                                                 auras.GROWTH_HITS) == (2, 1)
    assert set(auras.KINDS) <= set(pets.NO_PET_SKILLS)                         # stats every fight reads


def test_the_shipped_config_gives_an_aura_at_one_hatching_in_a_hundred():
    """§9: the file came with chance 0 (the live bot re-reads it); the last commit set the owner's 100 per
    10000 - 1% - with the owner's weights.  Every other test still starts at 0 (the autouse fixture)."""
    raw = json.loads((CONFIG_DIR / auras.CONFIG_FILE).read_text(encoding="utf-8"))
    assert raw == {"chance_per_10000": 100, "kinds": {"mroz": 35, "furia": 30, "szczescie": 25, "opieka": 10}}
    config = auras.parse(raw)
    assert config.on and config.hits * 100 == auras.AURA_ROLL                    # 1 in 100
    assert config.weights == (("SLOW_PERC", 35), ("CRIT_PERC", 30), ("DROP_PERC", 25), ("DEF_PERC", 10))
    assert auras.load().hits == 0 and not auras.load().on                       # the autouse fixture's file


def test_the_chance_is_exact_and_cut_to_its_range(caplog):
    assert auras.parse({"chance_per_10000": 100}).hits == 1000                 # 1% of 100000
    assert auras.parse({"chance_per_10000": 12.5}).hits == 125
    assert auras.parse({"chance_per_10000": 10000}).hits == auras.AURA_ROLL     # every hatching
    with caplog.at_level(logging.WARNING, logger="altermmo.features.auras"):
        assert auras.parse({"chance_per_10000": 20000}).hits == auras.AURA_ROLL
        assert auras.parse({"chance_per_10000": -5}).hits == 0
        for broken in ({"chance_per_10000": "100"}, {"chance_per_10000": True}, {"chance_per_10000": None}, [], 7):
            assert auras.parse(broken) == auras.NO_AURAS
    assert "outside 0-10000" in caplog.text and "is not a number" in caplog.text
    assert auras.parse({}) == auras.Config(0, auras.parse({"chance_per_10000": 0}).weights)  # no chance: 0


def test_the_kinds_of_the_config(caplog):
    assert auras.parse({"chance_per_10000": 1, "kinds": {"opieka": 4, "MROZ": 1}}).weights == (
        ("DEF_PERC", 4), ("SLOW_PERC", 1))
    with caplog.at_level(logging.WARNING, logger="altermmo.features.auras"):
        config = auras.parse({"chance_per_10000": 1, "kinds": {"smok": 9, "furia": 2.5, "opieka": -1,
                                                                "szczescie": "3", "mroz": 0}})
        assert config.weights == () and not config.on                          # nothing usable: no aura
        assert auras.parse({"chance_per_10000": 1, "kinds": ["mroz"]}) == auras.NO_AURAS
    assert "unknown aura 'smok'" in caplog.text and "not a whole number" in caplog.text
    assert "kinds must be an object" in caplog.text


def test_a_broken_file_means_no_aura_and_never_raises(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(auras, "CONFIG_DIR", tmp_path)
    with caplog.at_level(logging.WARNING, logger="altermmo.features.auras"):
        assert auras.load() == auras.NO_AURAS                                   # missing
        (tmp_path / auras.CONFIG_FILE).write_text("{chance_per_10000: 100", encoding="utf-8")
        assert auras.load() == auras.NO_AURAS                                   # not JSON
        (tmp_path / auras.CONFIG_FILE).write_bytes(b"\xff\xfe\x00")
        assert auras.load() == auras.NO_AURAS                                   # not UTF-8
        monkeypatch.setattr(auras, "parse", lambda raw: 1 / 0)
        (tmp_path / auras.CONFIG_FILE).write_text("{}", encoding="utf-8")
        assert auras.load() == auras.NO_AURAS                                   # whatever goes wrong inside
    assert "is missing" in caplog.text and "cannot be read" in caplog.text and "could not be understood" in caplog.text
    # (a BOM of an editor is no error)
    monkeypatch.undo()
    monkeypatch.setattr(auras, "CONFIG_DIR", tmp_path)
    (tmp_path / auras.CONFIG_FILE).write_text('﻿{"chance_per_10000": 50}', encoding="utf-8")
    assert auras.load().hits == 500


def test_no_draw_at_all_while_the_auras_are_off(monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("nothing may be drawn")

    monkeypatch.setattr(random, "randrange", never)
    assert auras.roll(auras.NO_AURAS) is None
    assert auras.roll(auras.Config(0, (("SLOW_PERC", 35),))) is None           # chance 0
    assert auras.roll(auras.Config(5000, ())) is None                           # no kind can come
    assert auras.grow("SLOW_PERC", 7) is None and auras.grow("CRIT_PERC", 4) is None   # at the pet's maximum
    assert auras.grow("NIC", 1) is None


def test_the_hatching_roll_and_the_kind(monkeypatch):
    config = auras.parse({"chance_per_10000": 100})                             # 1%: 1000 of 100000
    asked = draws(monkeypatch, 1000)
    assert auras.roll(config) is None and asked == [auras.AURA_ROLL]            # 1000 is not below 1000
    # Below the chance: the kind by the owner's weights 35 / 30 / 25 / 10 (whole numbers: exact).
    for pick, expected in ((0, MROZ), (34, MROZ), (35, FURIA), (64, FURIA), (65, SZCZESCIE), (89, SZCZESCIE),
                           (90, OPIEKA), (99, OPIEKA)):
        asked = draws(monkeypatch, 999, pick)
        assert auras.roll(config) == (expected.stat, expected.start) and asked == [auras.AURA_ROLL, 100]


def test_the_growth_is_one_step_with_half_a_chance_up_to_the_pets_maximum(monkeypatch):
    asked = draws(monkeypatch, 0)
    assert auras.grow("SLOW_PERC", 3) == 5 and asked == [2]
    draws(monkeypatch, 1)
    assert auras.grow("SLOW_PERC", 3) is None                                    # the other half: no step
    draws(monkeypatch, 0)
    assert auras.grow("SLOW_PERC", 6) == 7                                       # never above the maximum
    draws(monkeypatch, 0)
    assert auras.grow("DEF_PERC", 2) == 3
    asked = draws(monkeypatch, 0)
    assert auras.grow("DEF_PERC", 3) is None and asked == []                    # at the maximum: no draw


def test_a_pets_aura_never_exceeds_its_kinds_maximum():
    assert auras.aura_of({"aura": "SLOW_PERC", "aura_perc": Decimal(5)}) == (MROZ, 5)
    assert auras.aura_of({"aura": "SLOW_PERC", "aura_perc": 50}) == (MROZ, 7)    # an owner's hand edit
    assert auras.aura_of({"aura": "CRIT_PERC", "aura_perc": 9}) == (FURIA, 4)
    for nothing in ({"aura": "", "aura_perc": 0}, {"aura": "SLOW_PERC", "aura_perc": 0},
                    {"aura": "SLOW_PERC", "aura_perc": -3}, {"aura": "REPLACE_PERC", "aura_perc": 5},
                    {"aura": None, "aura_perc": None}, {"pet_id": 5}, None):
        assert auras.aura_of(nothing) is None


def test_the_auras_of_all_pets_add_up_without_a_cap():
    """Owner, 2026-11-26: "Tylko jak już to limity na towarzysza" - no limit on the sum."""
    rows = [{"pet_id": 1, "pet_lvl": 3, "aura": "SLOW_PERC", "aura_perc": 7},
            {"pet_id": 2, "pet_lvl": 1, "aura": "SLOW_PERC", "aura_perc": 7},
            {"pet_id": 3, "pet_lvl": 2, "aura": "SLOW_PERC", "aura_perc": 7},
            {"pet_id": 3, "pet_lvl": 2, "aura": "SLOW_PERC", "aura_perc": 7},  # the same pet twice (no key)
            {"pet_id": 4, "pet_lvl": 2, "aura": "DEF_PERC", "aura_perc": 30},  # one pet: at most its maximum
            {"pet_id": 5, "pet_lvl": 0, "aura": "CRIT_PERC", "aura_perc": 2},  # an egg has none
            {"pet_id": 6, "pet_lvl": 1, "aura": "", "aura_perc": 0}]
    assert auras.totals(rows) == {"SLOW_PERC": 21, "DEF_PERC": 3}
    assert auras.totals([]) == {}


def test_the_texts():
    hatched = auras.Change(auras.HATCHED, "SLOW_PERC", 3)
    assert auras.note(hatched) == ("🌟 Z jajka wykluł się towarzysz otoczony **Aurą Mrozu**! Aura pomaga Ci zawsze "
                                   "- nawet gdy towarzysz odpoczywa w stajni.")
    assert auras.note(auras.Change(auras.GREW, "DROP_PERC", 5)) == (
        "🌟 **Aura Szczęścia** Twojego towarzysza przybrała na sile - teraz to +5 %!")
    assert auras.note(auras.Change(auras.GREW, "NIC", 5)) == ""
    pet = {"pet_lvl": 2, "aura": "CRIT_PERC", "aura_perc": 3}
    assert auras.shown_line(pet) == ("🌟 Aura Furii: +3 % do szansy na krytyczne uderzenie - działa zawsze, także "
                                     "gdy towarzysz jest w stajni.")
    assert auras.stable_mark(pet) == " · 🌟 Aura Furii"
    assert auras.sacrifice_warning(pet) == ("Uwaga: ten towarzysz ma **Aurę Furii** - porzucając go, stracisz ją "
                                            "na zawsze.")
    assert auras.stable_header({"DEF_PERC": 1, "SLOW_PERC": 9}) == (
        "🌟 **Aury Twoich towarzyszy:** ❄️ Spowolnienie przeciwnika +9 % · 🛡️ Szansa na zablokowanie ataku +1 %")
    assert "maks" not in auras.stable_header({"SLOW_PERC": 40})                 # no cap, so no "(maks.)"
    nothing = {"pet_lvl": 2, "aura": "", "aura_perc": 0}
    assert auras.shown_line(nothing) == auras.stable_mark(nothing) == auras.sacrifice_warning(nothing) == ""
    assert auras.stable_header({}) == ""


# ===================================== hatching and growing =======================================

async def egg_at_side(game, name: str = "Ala", pet_id: int = 5, **pet):
    player = game.add_member(name)
    await add_owner(game.bot.db, player.id, pet_id)
    await add_pet(game.bot.db, pet_id, **pet)
    return player


async def test_an_egg_hatches_with_an_aura_and_the_drop_says_so(game_db, aura_config, monkeypatch):
    db = game_db.bot.db
    aura_config(10000, kinds={"mroz": 1})
    player = await egg_at_side(game_db)
    pools, ranges = rig(monkeypatch, randint=1)
    asked = draws(monkeypatch)
    notes: list[str] = []
    assert await pets.level_up_pet(game_db.scene(player), player, album_note=notes) is True
    row = await pet_row(db, 5)
    assert (row["pet_lvl"], row["aura"], row["aura_perc"]) == (1, "SLOW_PERC", 3)
    # The level's own draws come first (one talent point: randint, choice), then the aura's two.
    assert ranges == [(1, 2), (3, 5)] and len(pools) == 1 and asked == [auras.AURA_ROLL, 1]
    assert notes == [auras.MSG_HATCHED.format(aura="Aurą Mrozu")]               # for everybody, not a supporter only
    assert (await pets.get_pet_skills(game_db.bot, player.id))["SLOW_PERC"] == 3


async def test_no_aura_and_no_draw_while_the_chance_is_0(game_db, monkeypatch):
    db = game_db.bot.db
    player = await egg_at_side(game_db)
    asked = draws(monkeypatch)
    notes: list[str] = []
    assert await pets.level_up_pet(game_db.scene(player), player, album_note=notes) is True
    assert asked == [] and notes == []                                           # the draws of a level stay as they were
    row = await pet_row(db, 5)
    assert (row["pet_lvl"], row["aura"], row["aura_perc"]) == (1, "", 0)


async def test_a_pet_that_had_hatched_already_never_gets_an_aura(game_db, aura_config, monkeypatch):
    aura_config(10000)
    player = await egg_at_side(game_db, pet_lvl=1)
    asked = draws(monkeypatch)
    assert await pets.level_up_pet(game_db.scene(player), player.id) is True
    assert asked == [] and (await pet_row(game_db.bot.db, 5))["aura"] == ""


async def test_a_broken_config_costs_no_level(game_db, aura_config, monkeypatch):
    """The config is read BEFORE the level's transaction and never raises: the egg hatches without an aura."""
    aura_config(10000)
    monkeypatch.setattr(auras, "parse", lambda raw: 1 / 0)
    player = await egg_at_side(game_db)
    assert await pets.level_up_pet(game_db.scene(player), player.id) is True
    row = await pet_row(game_db.bot.db, 5)
    assert (row["pet_lvl"], row["aura"]) == (1, "")


async def test_the_aura_grows_at_a_later_level_up_to_the_pets_maximum(game_db, aura_config, monkeypatch):
    db = game_db.bot.db
    aura_config(10000)                                                           # (no roll after the hatching)
    player = await egg_at_side(game_db, pet_lvl=1, pet_skills=1, aura="SLOW_PERC", aura_perc=5)
    rig(monkeypatch, randint=1)
    asked = draws(monkeypatch, 0)
    notes: list[str] = []
    assert await pets.level_up_pet(game_db.scene(player), player, album_note=notes) is True
    assert asked == [auras.GROWTH_ROLL]
    row = await pet_row(db, 5)
    assert (row["pet_lvl"], row["aura"], row["aura_perc"]) == (2, "SLOW_PERC", 7)
    assert notes == ["🌟 **Aura Mrozu** Twojego towarzysza przybrała na sile - teraz to +7 %!"]
    # Level 2 -> 3 at the maximum: nothing is drawn, nothing is said.
    asked = draws(monkeypatch, 0)
    aura_note: list[str] = []
    assert await pets.level_up_pet(game_db.scene(player), player, aura_note=aura_note) is True
    assert asked == [] and aura_note == [] and (await pet_row(db, 5))["aura_perc"] == 7


async def test_the_other_half_of_the_growth_roll_leaves_the_aura_as_it_is(game_db, monkeypatch):
    player = await egg_at_side(game_db, pet_lvl=2, pet_skills=2, aura="DEF_PERC", aura_perc=1)
    asked = draws(monkeypatch, 1)
    notes: list[str] = []
    assert await pets.level_up_pet(game_db.scene(player), player.id, aura_note=notes) is True
    assert asked == [auras.GROWTH_ROLL] and notes == []
    row = await pet_row(game_db.bot.db, 5)
    assert (row["pet_lvl"], row["aura_perc"]) == (3, 1)


async def test_a_mission_level_grows_the_aura_of_a_stabled_pet(game_db, monkeypatch):
    """The only way a stabled pet grows: its aura can grow too, and the mission's result says so."""
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 0, pet_id_alt2=6)
    await add_pet(db, 6, pet_lvl=1, pet_skills=1, type="Dog", aura="DROP_PERC", aura_perc=3)
    draws(monkeypatch, 0)
    grown: list[auras.Change] = []
    async with db.transaction() as con:
        done = await pets.level_mission_pet_in(con, player.id, 6, upgrades=True, big=True, species="Dog",
                                               aura_out=grown)
    assert done is not None and done[1] == 2
    assert grown == [auras.Change(auras.GREW, "DROP_PERC", 5)] and (await pet_row(db, 6))["aura_perc"] == 5
    line = missions.result_line({"kind": "level", "name": "Burek", "new": 2, "aura": "DROP_PERC", "aura_perc": 5})
    assert line == ("⬆️ **Burek** awansuje na poziom 2!\n"
                    "🌟 **Aura Szczęścia** Twojego towarzysza przybrała na sile - teraz to +5 %!")
    assert missions.result_line({"kind": "level", "name": "Burek", "new": 2}) == "⬆️ **Burek** awansuje na poziom 2!"


async def test_the_owners_pet_level_command_says_the_aura(game_db, aura_config, monkeypatch, cog):
    aura_config(10000, kinds={"opieka": 1})
    boss_admin = admin(game_db)
    player = await egg_at_side(game_db)
    message = await run(game_db, boss_admin, f"!PetLvlUp {player.id}")
    assert message.reactions == ["✅"]
    assert game_db.channel.messages[-1].content == auras.MSG_HATCHED.format(aura="Aurą Opieki")
    assert (await pet_row(game_db.bot.db, 5))["aura"] == "DEF_PERC"


# ========================================== the fights ===========================================

async def test_every_hatched_pet_adds_its_aura_to_the_fight_stats(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, pet_id_alt1=6, pet_id_alt2=7, pet_id_alt3=8, pet_id_alt4=9)
    await add_pet(db, 5, pet_lvl=3, crit_perc=11, slow_perc=20, def_perc=99, aura="SLOW_PERC", aura_perc=3)
    await add_pet(db, 6, pet_lvl=2, aura="SLOW_PERC", aura_perc=7)
    await add_pet(db, 7, pet_lvl=1, aura="DEF_PERC", aura_perc=3)
    await add_pet(db, 8, pet_lvl=0, aura="CRIT_PERC", aura_perc=4)              # an egg gives nothing
    await add_pet(db, 9, pet_lvl=1, aura="DROP_PERC", aura_perc=50)             # (a hand edit: at most 7)
    await add_pet(db, 9, pet_lvl=1, aura="DROP_PERC", aura_perc=50)             # the same pet twice
    skills = await pets.get_pet_skills(game_db.bot, player.id)
    assert skills == {"CRIT_PERC": 11, "REPLACE_PERC": 0, "DEF_PERC": 102, "DROP_PERC": 7, "LOWHP_PERC": 0,
                      "SLOW_PERC": 30, "INIT_PERC": 0, "DETECTION": False}      # own + auras, no other limit
    assert list(skills) == list(pets.NO_PET_SKILLS)
    assert (await boss._pet_skills(game_db.bot, player.id))["SLOW_PERC"] == 30.0  # every fight reads these
    assert (await daily.pet_skills(game_db.bot, player.id))["DEF_PERC"] == 102.0
    assert await pets.aura_bonus(game_db.bot, player.id) == {"SLOW_PERC": 10, "DROP_PERC": 7, "DEF_PERC": 3}


async def test_the_auras_help_without_a_pet_at_the_side_or_with_it_on_a_mission(game_db):
    db = game_db.bot.db
    nobody, away = game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, nobody.id, 0, pet_id_alt4=6)
    await add_pet(db, 6, pet_lvl=2, aura="CRIT_PERC", aura_perc=2)
    assert await pets.get_pet_skills(game_db.bot, nobody.id) == dict(pets.NO_PET_SKILLS) | {"CRIT_PERC": 2}
    await add_owner(db, away.id, 7, pet_id_alt1=8)
    await add_pet(db, 7, pet_lvl=2, crit_perc=11, aura="DROP_PERC", aura_perc=3)
    await add_pet(db, 8, pet_lvl=1)
    await send_away(db, away.id, 7)
    # The pet at her side is on a mission: its own stats are not there, its aura "działa zawsze".
    assert await pets.get_pet_skills(game_db.bot, away.id) == dict(pets.NO_PET_SKILLS) | {"DROP_PERC": 3}
    egg_owner = game_db.add_member("Ela")                                        # an egg at the side
    await add_owner(db, egg_owner.id, 9, pet_id_alt1=10)
    await add_pet(db, 9, pet_lvl=0)
    await add_pet(db, 10, pet_lvl=3, aura="SLOW_PERC", aura_perc=7)
    assert (await pets.get_pet_skills(game_db.bot, egg_owner.id))["SLOW_PERC"] == 7


async def test_a_failed_aura_read_never_stops_a_fight(game_db, monkeypatch, caplog):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, crit_perc=11, aura="CRIT_PERC", aura_perc=2)

    async def broken(bot, *, refresh=False):
        raise RuntimeError("the database hiccupped")

    monkeypatch.setattr(pets, "aura_columns_available", broken)
    with caplog.at_level(logging.WARNING, logger="altermmo.features.pets"):
        assert (await pets.get_pet_skills(game_db.bot, player.id))["CRIT_PERC"] == 11   # own stats only
    assert "the auras could not be read" in caplog.text


# ================================ what never touches an aura ======================================

async def test_reroll_enlightenment_transformation_and_a_name_leave_the_aura_alone(game_db, monkeypatch):
    db = game_db.bot.db
    player = patron(game_db)
    await add_owner(db, player.id, 5, reroll_scroll=5, rebirth_stones=2, mirrors=1)
    await add_pet(db, 5, pet_lvl=3, pet_skills=3, crit_perc=12, aura="SLOW_PERC", aura_perc=5)
    rig(monkeypatch, randint=1)
    confirms(game_db, player)
    for change in (pets.reroll_pet(game_db.scene(player), player),
                   pets.enlight_pet(game_db.scene(player), player),
                   pets.transform_pet(game_db.scene(player), player),
                   pets.name_pet(game_db.scene(player), "Szron")):
        assert await change is True
        row = await pet_row(db, 5)
        assert (row["aura"], row["aura_perc"]) == ("SLOW_PERC", 5)
    assert (await pet_row(db, 5))["pet_name"] == "Szron"


async def test_a_sacrifice_warns_and_loses_the_aura(game_db, sacrifices):
    """The question says what is lost; the album keeps the copy (only a picture: it helps nobody)."""
    from altermmo.features import album

    db = game_db.bot.db
    player = patron(game_db)
    await add_owner(db, player.id, 5, pet_id_alt1=6)
    await add_pet(db, 5, pet_lvl=2, aura="DEF_PERC", aura_perc=2, mission_perc=6)
    await add_pet(db, 6, pet_lvl=1, aura="DEF_PERC", aura_perc=1)
    assert (await pets.get_pet_skills(game_db.bot, player.id))["DEF_PERC"] == 3
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert game_db.channel.contains(
        f"Czy jesteś pewien, że chcesz porzucić swojego towarzysza <@{player.id}>? <:MonkaS:882181709100097587> "
        "Uwaga: ten towarzysz ma **Aurę Opieki** - porzucając go, stracisz ją na zawsze. "
        + album.MSG_DISCARD_KEPT)
    assert await pet_row(db, 5) is None
    kept = await db.fetchrow(f"SELECT AURA, AURA_PERC, MISSION_PERC FROM {album.TABLE} WHERE PET_ID = 5")
    assert (kept["aura"], kept["aura_perc"], kept["mission_perc"]) == ("DEF_PERC", 2, 6)   # copied, inert
    assert (await pets.get_pet_skills(game_db.bot, player.id))["DEF_PERC"] == 1


async def test_no_warning_for_a_pet_without_an_aura(game_db, sacrifices):
    player = game_db.add_member("Ala")
    await add_owner(game_db.bot.db, player.id, 5)
    await add_pet(game_db.bot.db, 5, pet_lvl=2)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert not game_db.channel.contains("Uwaga: ten towarzysz ma")


async def test_a_reassigned_pet_moves_with_its_aura(game_db):
    """``!ReassignPet``: the pet - and its aura - is the new owner's only; the old one has it no more."""
    db = game_db.bot.db
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, ala.id, 0, pet_id_alt2=6)
    await add_pet(db, 6, pet_lvl=2, aura="SLOW_PERC", aura_perc=5)
    assert (await pets.get_pet_skills(game_db.bot, ala.id))["SLOW_PERC"] == 5
    assert await pets.reassign_pet(game_db.bot, 6, ola.id) is True
    assert (await owner_row(db, ala.id))["pet_id_alt2"] == 0 and (await owner_row(db, ola.id))["pet_id"] == 6
    assert (await pets.get_pet_skills(game_db.bot, ala.id))["SLOW_PERC"] == 0
    assert (await pets.get_pet_skills(game_db.bot, ola.id))["SLOW_PERC"] == 5
    assert (await pet_row(db, 6))["aura_perc"] == 5


# ============================================ shown ==============================================

async def test_the_pet_shows_its_aura_and_its_resourcefulness(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, pet_skills=3, crit_perc=4, mission_perc=6, aura="SLOW_PERC", aura_perc=3)
    assert await pets.show_pet(game_db.scene(player), player) is True
    text = last_embed(game_db).description
    assert ("Szansa na krytyczne uderzenie: 4 %\nZaradność w zleceniach: 6 %\n"
            "🌟 Aura Mrozu: +3 % do spowolnienia przeciwnika - działa zawsze, także gdy towarzysz jest w stajni.\n"
            in text)
    await db.execute("UPDATE PETS SET AURA = '', AURA_PERC = 0, MISSION_PERC = 0")
    assert await pets.show_pet(game_db.scene(player), player) is True
    text = last_embed(game_db).description
    assert "🌟" not in text and "Zaradność" not in text


async def test_the_stable_sums_the_auras_and_marks_each_aura_pet(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5, pet_id_alt1=6, pet_id_alt3=7)
    await add_pet(db, 5, pet_name="Szron", pet_lvl=2, type="Dog", aura="SLOW_PERC", aura_perc=3)
    await add_pet(db, 6, pet_name="Iskra", pet_lvl=3, aura="SLOW_PERC", aura_perc=7)
    await add_pet(db, 7, pet_name="Mruczek", pet_lvl=1, aura="DROP_PERC", aura_perc=3)
    embed, _options = await pets._stable_view(game_db.bot, player.id)
    text = embed.description
    assert text.startswith(f"Oto Twoja stajnia, <@{player.id}> <:peepoBlush:984769061340737586>\n\n"
                           "🌟 **Aury Twoich towarzyszy:** ❄️ Spowolnienie przeciwnika +10 % · 🍀 Rzadszy drop +3 %\n\n"
                           f"{pets.STABLE_ACTIVE}\n**Szron** · Pies · poziom 2 · 🌟 Aura Mrozu\n")
    assert "**1.** **Iskra** · Kot · poziom 3 · 🌟 Aura Mrozu" in text
    assert "**3.** **Mruczek** · Kot · poziom 1 · 🌟 Aura Szczęścia" in text
    await db.execute("UPDATE PETS SET AURA = '', AURA_PERC = 0")
    embed, _options = await pets._stable_view(game_db.bot, player.id)
    assert "🌟" not in embed.description


def test_the_mines_level_line_shows_the_auras_note():
    from altermmo.features import dungeon

    note = auras.MSG_HATCHED.format(aura="Aurą Mrozu")
    granted = {"level": {"name": "Iskra", "old": 0, "new": 1, "notes": [note]}}
    assert dungeon.granted_lines(granted) == [dungeon.MSG_GOT_LEVEL.format(name="Iskra", old=0, new=1), note]
    stored_before = {"level": {"name": "Iskra", "old": 1, "new": 2}}            # a record of the older code
    assert dungeon.granted_lines(stored_before) == [dungeon.MSG_GOT_LEVEL.format(name="Iskra", old=1, new=2)]
