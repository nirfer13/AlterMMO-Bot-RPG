"""features/skills.py + cogs/skills.py: skill gems against a real PostgreSQL and the fake Discord."""

from __future__ import annotations

import asyncio
import random
import sys
import time
import types
from decimal import Decimal

import discord
import pytest
import pytest_asyncio

from altermmo.core import challenge, guards, timing, ui
from altermmo.core.challenge import Outcome, RoundResult
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossPhase
from altermmo.features import daily as real_daily
from altermmo.features import modifiers, skills

from fakes import FakeMember, FakeMessage

NO_SKILL = "Niestety nie posiadasz umiejętności <@{}> <:Sadge:936907659142111273> Klejnoty umiejętności są " \
           "bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!"
REST_DONE = "Wszyscy podróżnicy, którzy skorzystali z zaproszenia, mogą ponownie ruszyć na polowanie! " \
            "<:Up:912798893304086558>"
ASK = "Oznacz użytkownika, którego towarzysz ma stać się wyjątkowy!"
NO_TRAVELLER = "Niestety podany podróżnik nie istnieje. Moc Twojego kamienia umiejętności została wyczerpana."
NO_PET = "Niestety podany podróżnik nie posiada aktualnie towarzysza <:Sadge:936907659142111273> " \
         "Moc Twojego kamienia umiejętności została wyczerpana."
BEDGE = "<:Bedge:970576892874854400>"
INVITATION = "zaprasza na odpoczynek!"

OWNER_DEFAULTS = dict(pet_owned=True, reroll_scroll=0, reroll_scroll_shard=0, rebirth_stones=0, mirrors=0,
                      pet_id_alt1=0, pet_id_alt2=0, skill_gem=0, pet_id_alt3=0, pet_id_alt4=0)
PET_DEFAULTS = dict(pet_name="Towarzysz", pet_lvl=1, pet_skills=0, quality="Standard", shiny=False,
                    type="Cat", variant="0", crit_perc=0, replace_perc=0, def_perc=0, drop_perc=0,
                    lowhp_perc=0, slow_perc=0, init_perc=0, detection=False, ultra_shiny=False, mythic=False)


# -- helpers --------------------------------------------------------------------------

def one_gem(player_id: int) -> str:
    """The refusal of a gem while a player with SEVERAL gems is on the daily list ("one gem a day";
    a player with one gem gets ``skills.daily_cd_text()`` from ``!skill`` - that gem regenerates):
    the countdown to the next daily reset (02:30 / Monday 15:00)."""
    stamp = int(real_daily.next_daily_reset().timestamp())
    return (f"Dziś użyłeś już klejnotu umiejętności, <@{player_id}>! Jednego dnia działa tylko jeden "
            f"klejnot - spróbuj ponownie <t:{stamp}:R> {BEDGE}")


def weekly_cd() -> str:
    """The weekly "regenerating" text: the countdown to the end of the season (Monday 15:00)."""
    stamp = int(real_daily.next_season_end().timestamp())
    return f"Twój klejnot umiejętności regeneruje się aż do nowego tygodnia! Będzie gotowy <t:{stamp}:R> {BEDGE}"


async def _insert(db, table: str, values: dict) -> None:
    marks = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    await db.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({marks})", *values.values())


async def add_owner(db, player_id: int, pet_id: int = 0, **columns) -> None:
    await _insert(db, "PETOWNER", {"player_id": player_id, "pet_id": pet_id, **OWNER_DEFAULTS, **columns})


async def add_pet(db, pet_id: int, **columns) -> None:
    await _insert(db, "PETS", {"pet_id": pet_id, **PET_DEFAULTS, **columns})


async def add_gem(db, skill_id: int, variant: int, *, name: str | None = None, slow: int | None = 7) -> None:
    await _insert(db, "SKILLS", {"skill_id": skill_id, "gem_name": name or skills.gem_name(variant),
                                 "skill_variant": variant, "slow_perc": slow})


async def with_gem(game, variant: int, name: str = "Ala", *, skill_id: int = 5) -> FakeMember:
    """A player on the server who owns a gem of ``variant``."""
    player = game.add_member(name)
    await add_gem(game.bot.db, skill_id, variant)
    await add_owner(game.bot.db, player.id, skill_gem=skill_id)
    return player


def _plain(row) -> dict | None:
    if row is None:
        return None
    return {key: int(value) if isinstance(value, Decimal) else value for key, value in row.items()}


async def owner_rows(db, player_id: int) -> list[dict]:
    return [_plain(row) for row in await db.fetch("SELECT * FROM PETOWNER WHERE PLAYER_ID = $1", player_id)]


async def gems(db) -> list[tuple]:
    rows = await db.fetch("SELECT * FROM SKILLS ORDER BY SKILL_ID")
    return [(int(r["skill_id"]), r["gem_name"], int(r["skill_variant"]), int(r["slow_perc"])) for r in rows]


async def until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the expected thing never happened"
        await asyncio.sleep(0.005)


def rig(monkeypatch, table: dict | None = None) -> list[tuple[int, int]]:
    """Deterministic ``random.randint``: ``table[(low, high)]`` or ``low``.  Returns the ranges asked."""
    asked: list[tuple[int, int]] = []

    def randint(low, high):
        asked.append((low, high))
        return (table or {}).get((low, high), low)

    monkeypatch.setattr(random, "randint", randint)
    return asked


def texts(game) -> list[str]:
    """What the bot wrote on the boss channel (without the permanent first message)."""
    return [m.text for m in game.channel.bot_messages[1:]]


def modifier_file(game) -> dict:
    return {key: value for key, value in game.bot.store.modifiers.load().items() if value}


def rest_clicks(game, monkeypatch, *clickers, during=None) -> list:
    """While the rest lasts (instead of its five minutes) ``clickers`` click "Odpocznij" one by one.

    ``during(invitation)`` runs after the clicks.  Returns the interactions (filled during the rest).
    """
    answers: list = []

    async def pause(seconds):
        invitation = game.last_with_buttons()
        assert invitation is not None and INVITATION in invitation.embeds[0].title
        for member in clickers:
            answers.append(await game.click(invitation, member, "rest"))
        if during is not None:
            await during(invitation)

    monkeypatch.setattr(timing, "pause", pause)
    return answers


def tags(game, member, text: str, *, delay: float = 0.0) -> None:
    """``member`` answers the "tag a traveller" question with ``text``."""
    game.auto_answer(member, delay=delay, responder=lambda message: text if message.content == ASK else None)


def reaction_listeners(game) -> list:
    events = game.bot.extra_events
    return [*events.get("on_reaction_add", []), *events.get("on_reaction_remove", [])]


class DatabaseDown:
    """A database that cannot be reached at all."""

    def _fail(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    transaction = _fail

    async def execute(self, *args, **kwargs):
        self._fail()

    fetch = fetchrow = fetchval = execute


@pytest.fixture
def daily(monkeypatch):
    """Stand-in for features/daily.py (owned by another feature, imported lazily)."""
    import altermmo.features as features

    module = types.ModuleType("altermmo.features.daily")
    module.removed = []
    module.cleared = 0

    async def remove_player_daily_file(bot, player_id_list):
        module.removed.append(list(player_id_list))
        bot.store.daily_cd.remove(player_id_list)

    async def clear_daily_file(bot):
        module.cleared += 1
        bot.store.daily_cd.clear()

    module.remove_player_daily_file = remove_player_daily_file
    module.clear_daily_file = clear_daily_file
    module.next_daily_reset = real_daily.next_daily_reset      # the reset times are the real ones
    module.next_season_end = real_daily.next_season_end
    monkeypatch.setitem(sys.modules, "altermmo.features.daily", module)
    monkeypatch.setattr(features, "daily", module, raising=False)
    return module


@pytest.fixture
def prayers(monkeypatch):
    """Record the prayers of the festival gem instead of drawing real modifiers."""
    calls: list = []

    async def random_modifiers(scene, msg=True, sacrefice=False):
        calls.append((scene.author, msg, sacrefice))
        return "points_boost"

    monkeypatch.setattr(modifiers, "random_modifiers", random_modifiers)
    return calls


@pytest.fixture
def summons(game, monkeypatch):
    """Record ``bot.boss_cycle.summon`` calls; ``summons.result`` is what it answers."""
    box = types.SimpleNamespace(calls=[], result=True, error=None)

    async def summon(scene, rarity, **kwargs):
        box.calls.append((scene.author, rarity))
        if box.error is not None:
            raise box.error
        return box.result

    monkeypatch.setattr(game.bot.boss_cycle, "summon", summon)
    return box


# -- the table ------------------------------------------------------------------------

async def test_schema_is_the_production_table(db):
    rows = await db.fetch(
        """SELECT column_name, data_type, character_maximum_length FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'skills' ORDER BY ordinal_position""")
    assert [tuple(row) for row in rows] == [
        ("skill_id", "numeric", None), ("gem_name", "character varying", 255),
        ("skill_variant", "numeric", None), ("slow_perc", "numeric", None)]
    assert await db.fetchval(
        "SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_name = 'skills'") == 0


async def test_create_skills_table_creates_seeds_and_never_destroys(game_db):
    db = game_db.bot.db
    await db.execute("DROP TABLE SKILLS")                                # the legacy helper started with this
    await skills.create_skills_table(game_db.bot)
    await skills.create_skills_table(game_db.bot)
    assert await gems(db) == [(0, "Default", 0, 0)]

    await add_gem(db, 1, 2)
    await skills.create_skills_table(game_db.bot)
    assert await gems(db) == [(0, "Default", 0, 0), (1, "Klejnot Wyjątkowości", 2, 7)]

    await db.execute("DELETE FROM SKILLS WHERE SKILL_ID = 0")
    await skills.create_skills_table(game_db.bot)                        # not empty: no default row is added
    assert await gems(db) == [(1, "Klejnot Wyjątkowości", 2, 7)]


# -- generating gems ------------------------------------------------------------------

async def test_generate_skill_gem_works_in_an_empty_table_and_counts_up(game_db, monkeypatch):
    asked = rig(monkeypatch, {(1, 3): 3, (0, 20): 20})
    scene = game_db.scene()
    assert await skills.generate_skill_gem(scene) == 1                   # legacy: IndexError on an empty table
    assert asked == [(1, 3), (0, 20)]                                    # legacy ranges, legacy order
    rig(monkeypatch, {(1, 3): 1, (0, 20): 0})
    assert await skills.generate_skill_gem(scene) == 2
    rig(monkeypatch, {(1, 3): 2, (0, 20): 13})
    assert await skills.generate_skill_gem(scene) == 3
    assert await gems(game_db.bot.db) == [
        (1, "Klejnot Przywołania", 3, 20), (2, "Klejnot Regeneracji", 1, 0), (3, "Klejnot Wyjątkowości", 2, 13)]


async def test_generate_skill_gem_follows_the_default_row(game_db):
    await skills.create_skills_table(game_db.bot)
    assert await skills.generate_skill_gem(game_db.scene()) == 1


async def test_gems_generated_at_the_same_time_get_different_ids(game_db):
    scene = game_db.scene()
    ids = await asyncio.gather(*(skills.generate_skill_gem(scene) for _ in range(6)))
    assert sorted(ids) == [1, 2, 3, 4, 5, 6]


def test_gem_names():
    assert [skills.gem_name(variant) for variant in range(1, 7)] == [
        "Klejnot Regeneracji", "Klejnot Wyjątkowości", "Klejnot Przywołania", "Klejnot Błogosławieństwa",
        "Klejnot Spowolnienia", "Klejnot Obfitości"]
    assert skills.gem_name(0) == skills.gem_name(7) == "Nieznany klejnot"


# -- assigning a gem (boss loot) ------------------------------------------------------

async def test_assign_skill_creates_the_row_of_a_new_player(game_db, monkeypatch):
    rig(monkeypatch, {(1, 3): 2, (0, 20): 4})
    player = game_db.add_member("O'Neil")                                # an apostrophe broke legacy SQL elsewhere
    assert await skills.assign_skill(game_db.scene(), player) is True
    assert await owner_rows(game_db.bot.db, player.id) == [
        dict(player_id=player.id, pet_id=0, pet_owned=False, reroll_scroll=0, reroll_scroll_shard=0,
             rebirth_stones=0, mirrors=0, pet_id_alt1=0, pet_id_alt2=0, skill_gem=1, pet_id_alt3=0, pet_id_alt4=0,
             # the wandering merchant's columns (new in v2), as in a fresh row
             **{f"pet_id_alt{place}": 0 for place in range(5, 11)}, stable_places=4, egg_pouch=False,
             pouch_pet_id=0)]
    assert await gems(game_db.bot.db) == [(1, "Klejnot Wyjątkowości", 2, 4)]
    assert texts(game_db) == []                                          # the loot code announces the gem


@pytest.mark.parametrize("stored", [0, None, -1])
async def test_assign_skill_fills_the_row_of_a_player_without_a_gem(game_db, stored):
    player = game_db.add_member("Ala")
    await add_owner(game_db.bot.db, player.id, 9, reroll_scroll=3, skill_gem=stored)   # NULL: legacy TypeError
    assert await skills.assign_skill(game_db.scene(), player) is True
    (row,) = await owner_rows(game_db.bot.db, player.id)
    assert (row["skill_gem"], row["pet_id"], row["reroll_scroll"]) == (1, 9, 3)


async def pouch(db, player_id: int) -> list[int]:
    rows = await db.fetch("SELECT SKILL_ID FROM SKILLPOUCH WHERE PLAYER_ID = $1 ORDER BY SKILL_ID", player_id)
    return [int(row["skill_id"]) for row in rows]


async def test_next_gems_are_of_other_kinds_and_go_to_the_pouch(game_db):
    db = game_db.bot.db
    player = await with_gem(game_db, skills.REST)
    assert await skills.assign_skill(game_db.scene(), player) is True
    assert await skills.assign_skill(game_db.scene(), player) is True
    assert (await owner_rows(db, player.id))[0]["skill_gem"] == 5        # the first gem stays active
    assert await pouch(db, player.id) == [6, 7]
    assert sorted(gem[2] for gem in await gems(db)) == [skills.REST, skills.SHINY, skills.SUMMON]

    # One gem of every kind that can drop: there is nothing more to collect.
    assert await skills.assign_skill(game_db.scene(), player) is False
    assert len(await gems(db)) == 3 and await pouch(db, player.id) == [6, 7]   # no orphan gem was generated


@pytest.mark.parametrize("drawn", [1, 2, 3])
async def test_a_drawn_kind_the_player_already_has_is_replaced(game_db, monkeypatch, drawn):
    db = game_db.bot.db
    player = await with_gem(game_db, skills.SHINY)
    rig(monkeypatch, {(1, 3): drawn})
    monkeypatch.setattr(random, "choice", lambda kinds: list(kinds)[-1])
    assert await skills.assign_skill(game_db.scene(), player) is True
    new_kind = (await gems(db))[-1][2]
    assert new_kind == (drawn if drawn != skills.SHINY else skills.SUMMON)


async def test_forced_kind_for_the_admin_command(game_db):
    db = game_db.bot.db
    player = await with_gem(game_db, skills.REST)
    assert await skills.assign_skill(game_db.scene(), player, skills.LOOT) is True
    assert await skills.assign_skill(game_db.scene(), player, skills.LOOT) is False
    assert await skills.assign_skill(game_db.scene(), player, skills.REST) is False
    assert [gem[2] for gem in await gems(db)] == [skills.REST, skills.LOOT]


async def test_assign_skill_to_a_player_who_left_the_server(game_db):
    gone = FakeMember("Były gracz")                                      # not in the guild any more
    assert await skills.assign_skill(game_db.scene(), gone) is True
    assert (await owner_rows(game_db.bot.db, gone.id))[0]["skill_gem"] == 1
    assert await skills.assign_skill(game_db.scene(), gone.id) is True   # a bare id works as well
    assert await pouch(game_db.bot.db, gone.id) == [2]


async def test_assign_skill_with_duplicated_owner_rows(game_db):
    """PETOWNER has no unique key: production may hold two rows of one player."""
    db = game_db.bot.db
    player, other = game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, player.id)
    await add_owner(db, player.id, skill_gem=None)
    assert await skills.assign_skill(game_db.scene(), player) is True
    assert [row["skill_gem"] for row in await owner_rows(db, player.id)] == [1, 1]

    await add_owner(db, other.id)
    await add_gem(db, 40, skills.SLOW)
    await add_owner(db, other.id, skill_gem=40)
    assert await skills.assign_skill(game_db.scene(), other) is True     # one of the rows holds a gem:
    assert sorted(row["skill_gem"] for row in await owner_rows(db, other.id)) == [0, 40]   # it stays active
    assert await pouch(db, other.id) == [41]


async def test_two_gems_for_the_same_new_player_at_once(game_db):
    player = game_db.add_member("Ala")
    scene = game_db.scene()
    results = await asyncio.gather(skills.assign_skill(scene, player), skills.assign_skill(scene, player))
    assert results == [True, True]
    rows = await owner_rows(game_db.bot.db, player.id)
    made = await gems(game_db.bot.db)
    assert len(rows) == 1 and len(made) == 2                             # no duplicate row, no orphan gem
    assert made[0][2] != made[1][2]                                      # two different kinds
    assert sorted([rows[0]["skill_gem"], *await pouch(game_db.bot.db, player.id)]) == [1, 2]


async def test_database_outage_is_an_error_not_a_business_answer(game):
    game.bot.db = DatabaseDown()
    player = game.add_member("Ala")
    scene = game.scene(player)
    for call in (skills.assign_skill(scene, player), skills.generate_skill_gem(scene),
                 skills.get_skill_data(scene, 1), skills.show_skill(scene), skills.use_skill(scene, player),
                 skills.create_skills_table(game.bot)):
        with pytest.raises(DatabaseUnavailable):
            await call
    assert texts(game) == []                                             # no "Błąd bazy danych... Próbuję ponownie"


async def test_get_skill_data(game_db):
    await add_gem(game_db.bot.db, 3, skills.SUMMON, name="Klejnot O'Neila", slow=11)
    row = await skills.get_skill_data(game_db.scene(), 3)
    assert (int(row[0]), row[1], int(row[2]), int(row[3])) == (3, "Klejnot O'Neila", 3, 11)
    assert row["gem_name"] == "Klejnot O'Neila"
    assert await skills.get_skill_data(game_db.scene(), 4) is None
    assert texts(game_db) == ["Taki klejnot umiejętności nie istnieje!"]


# -- !pokazskill ----------------------------------------------------------------------

DESCRIPTIONS = {
    1: "Klejnot regeneracji pozwala na odpoczynek wszystkich podróżników, którzy są chętni. Zaraz po nabraniu sił "
       "mogą ponownie wyruszyć na polowanie.",
    2: "Klejnot wyjątkowości sprawia, że towarzysz wybranego podróżnika zostaje odmieniony i staje się wyjątkowy.",
    3: "Klejnot przywołania uwalnia potężnego bossa. Każdy może go zaatakować.",
    4: "Klejnot błogosławieństwa sprawia, że na potwory i bossów spływają efekty magiczne.",
    5: "Klejnot spowolnienia spowalnia nadciągające potwory oraz bossa.",
    6: "Klejnot obfitości sprawia, że następne potwory oraz boss będą oferowały lepsze nagrody.",
}


@pytest.mark.parametrize("variant", [1, 2, 3, 4, 5, 6])
async def test_show_skill(game_db, variant):
    player = await with_gem(game_db, variant)
    assert await skills.show_skill(game_db.scene(player)) is True
    (message,) = game_db.channel.bot_messages[1:]
    embed = message.embeds[0]
    assert embed.title == skills.gem_name(variant)
    assert embed.description == (
        f"Oto Twój klejnot umiejętności, <@{player.id}>.\n\n{DESCRIPTIONS[variant]}"
        "\n\nDodatkowe spowolnienie: 7 %.\n\nMożesz użyć umiejętności przez komendę **!skill**.")
    assert embed.footer.text == "Wielcy ludzie nie rodzą się wielkimi, tylko się nimi stają."
    assert embed.colour.value == 0xfc7703
    assert embed.image.url == f"attachment://{variant}.png" and message.files == [f"{variant}.png"]


async def test_show_skill_without_a_gem(game_db):
    db = game_db.bot.db
    await skills.create_skills_table(game_db.bot)                        # the legacy "Default" gem 0 / variant 0
    nobody, zero, null, negative = (game_db.add_member(name) for name in ("Ala", "Ola", "Ela", "Iza"))
    await add_owner(db, zero.id, 3)
    await add_owner(db, null.id, 3, skill_gem=None)                      # legacy: TypeError (None > -1)
    await add_owner(db, negative.id, 3, skill_gem=-1)
    for player in (nobody, zero, null, negative):
        assert await skills.show_skill(game_db.scene(player)) is False
    assert texts(game_db) == [NO_SKILL.format(p.id) for p in (nobody, zero, null, negative)]


async def test_show_skill_survives_broken_gem_data(game_db):
    db = game_db.bot.db
    lost, unknown, default, nameless = (game_db.add_member(name) for name in ("Ala", "Ola", "Ela", "Iza"))
    await add_owner(db, lost.id, skill_gem=77)                           # no such row in SKILLS
    await add_gem(db, 8, 9, name="Klejnot Tajemnic")
    await add_owner(db, unknown.id, skill_gem=8)
    await add_gem(db, 9, 0, name="Default")
    await add_owner(db, default.id, skill_gem=9)
    await _insert(db, "SKILLS", {"skill_id": 10, "gem_name": None, "skill_variant": 1, "slow_perc": None})
    await add_owner(db, nameless.id, skill_gem=10)

    assert await skills.show_skill(game_db.scene(lost)) is False         # legacy: TypeError on None[1]
    assert texts(game_db) == ["Taki klejnot umiejętności nie istnieje!"]

    assert await skills.show_skill(game_db.scene(unknown)) is True       # legacy: FileNotFoundError (9.png)
    message = game_db.channel.bot_messages[-1]
    assert message.embeds[0].title == "Klejnot Tajemnic" and message.files == []
    assert message.embeds[0].description == (
        f"Oto Twój klejnot umiejętności, <@{unknown.id}>.\n\nNieznany klejnot umiejętności.")

    assert await skills.show_skill(game_db.scene(default)) is False
    assert game_db.channel.bot_messages[-1].content == NO_SKILL.format(default.id)

    assert await skills.show_skill(game_db.scene(nameless)) is True      # NULLs: legacy int(None)
    embed = game_db.channel.bot_messages[-1].embeds[0]
    assert embed.title == "Nieznany klejnot" and "Dodatkowe spowolnienie: 0 %." in embed.description


# -- !skill: nothing to use -----------------------------------------------------------

async def test_use_skill_without_a_gem(game_db):
    player, other = game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(game_db.bot.db, other.id, skill_gem=77)
    assert await skills.use_skill(game_db.scene(player), player) == 0
    assert await skills.use_skill(game_db.scene(other), other) == 0
    assert texts(game_db) == [NO_SKILL.format(player.id), "Taki klejnot umiejętności nie istnieje!"]
    assert game_db.bot.store.skill_daily_cd.all() == game_db.bot.store.skill_weekly_cd.all() == []


async def test_use_skill_with_an_unknown_variant_does_nothing(game_db):
    player = await with_gem(game_db, 9)
    assert await skills.use_skill(game_db.scene(player), player) == 0
    assert texts(game_db) == []


# -- gem 1: the rest ------------------------------------------------------------------

async def test_rest_lets_the_guests_hunt_again(game_db, daily, monkeypatch):
    bot = game_db.bot
    asked = rig(monkeypatch, {(0, 4): 3})
    host = await with_gem(game_db, skills.REST)
    ola, ela, iza, olek = (game_db.add_member(name) for name in ("Ola", "Ela", "Iza", "Olek"))
    dead = game_db.add_member("Polegly", roles=[bot.ids.dead_hunter_role])
    game_db.channel.silenced_roles.add(bot.ids.dead_hunter_role)
    gone = FakeMember("Były gracz")
    for tired in (ola, ela, iza, olek, dead, gone):
        bot.store.daily_cd.add(tired.id)
    shown: list = []

    async def look(invitation) -> None:
        shown.append(invitation.embeds[0])

    answers = rest_clicks(game_db, monkeypatch, bot.fake_user, ola, ela, ola, dead, gone, during=look)
    assert await skills.use_skill(game_db.scene(host, "!skill"), host) == 1

    picture, invitation, already, cannot, done = game_db.channel.bot_messages[1:]
    assert picture.files == ["3.png"] and (0, 4) in asked
    embed = invitation.embeds[0]
    assert embed.title == ("<:peepofat:1062666941405331507> Ala zaprasza na odpoczynek! "
                           "<:peepofat:1062666941405331507>")
    assert embed.description == (
        "Wspaniały dzień! Wszyscy bohaterowie zostali zaproszeni na ucztę!\n\n**Kliknij „Odpocznij”, żeby "
        "wypocząć i móc ponownie wyruszyć na polowanie. Odpoczynek potrwa maksymalnie 5 minut.**"
        "\n\n⌛ Odpoczynek dobiegł końca.")
    assert [(field.name, field.value) for field in embed.fields] == [
        ("Odpoczywają (3)", f"<@{ola.id}>, <@{ela.id}>, <@{gone.id}>")]
    assert embed.colour.value == 0xFC0303 and invitation.reactions == []    # no reaction any more
    assert invitation.view is None and done.content == REST_DONE              # the button is gone

    # While the rest lasted: the deadline was on the panel; who clicked heard what.
    assert "⏳ Odpoczynek kończy się <t:" in shown[0].description
    bot_click, ola_1, ela_1, ola_2, dead_click, gone_click = answers
    assert bot_click.replies == [] and not bot_click.response.is_done()      # the bot's own click: ignored
    assert ola_1.response.deferred and ola_1.replies == [] and ela_1.replies == []
    assert ola_2.answers == [skills.MSG_ALREADY_RESTING]                      # counted once
    assert dead_click.answers == [ui.MSG_CANNOT_PLAY]                         # dead hunters rest no more
    assert gone_click.response.deferred and gone_click.replies == []
    # Both refusals are messages on the channel (under the invitation), each naming its clicker.
    assert already.content == f"<@{ola.id}> {skills.MSG_ALREADY_RESTING}" and already is ola_2.replies[0].message
    assert cannot.content == f"<@{dead.id}> {ui.MSG_CANNOT_PLAY}" and cannot is dead_click.replies[0].message

    assert daily.removed == [[str(ola.id), str(ela.id)]]                      # left the server: not counted
    assert bot.store.daily_cd.all() == [str(iza.id), str(olek.id), str(dead.id), str(gone.id)]
    assert bot.store.skill_daily_cd.all() == [str(host.id)]
    assert reaction_listeners(game_db) == []

    # The gem regenerates until the daily reset (one gem a day).
    assert await skills.use_skill(game_db.scene(host, "!skill"), host) == 0
    assert game_db.channel.bot_messages[-1].content == skills.daily_cd_text() and game_db.channel.count(INVITATION) == 1
    await skills.clear_daily_skill(bot)
    assert await skills.skill_rest(game_db.scene(host)) is True


async def test_rest_repeated_clicks_post_one_notice_per_player(game_db, daily, monkeypatch):
    """With the real repeat filter of ui.reply the channel gets one "already resting" per player."""
    monkeypatch.setattr(ui, "NOTICE_GAP", 10.0)
    host, ola, ela = await with_gem(game_db, skills.REST), game_db.add_member("Ola"), game_db.add_member("Ela")
    for tired in (ola, ela):
        game_db.bot.store.daily_cd.add(tired.id)
    answers = rest_clicks(game_db, monkeypatch, ola, ola, ola, ela, ela)
    assert await skills.skill_rest(game_db.scene(host)) is True
    assert [click.answers for click in answers] == [[], [skills.MSG_ALREADY_RESTING], [], [],
                                                    [skills.MSG_ALREADY_RESTING]]
    assert all(click.response.is_done() for click in answers)          # every click acknowledged
    assert texts(game_db)[2:] == [f"<@{ola.id}> {skills.MSG_ALREADY_RESTING}",
                                  f"<@{ela.id}> {skills.MSG_ALREADY_RESTING}", REST_DONE]
    assert daily.removed == [[str(ola.id), str(ela.id)]]


async def test_rest_nobody_came(game_db, daily):
    host = await with_gem(game_db, skills.REST)
    game_db.bot.store.daily_cd.add(host.id)
    assert await skills.skill_rest(game_db.scene(host)) is True
    assert daily.removed == [] and game_db.bot.store.daily_cd.all() == [str(host.id)]
    assert texts(game_db)[-1] == REST_DONE
    assert game_db.bot.store.skill_daily_cd.all() == []                  # use_skill marks the use, not the gem


async def test_rest_with_the_real_daily_module(game_db, monkeypatch):
    """No stand-in: the rest calls features/daily.py as it is on disk."""
    bot = game_db.bot
    host, ola, ela = await with_gem(game_db, skills.REST), game_db.add_member("Ola"), game_db.add_member("Ela")
    for tired in (host, ola, ela):
        bot.store.daily_cd.add(tired.id)
    rest_clicks(game_db, monkeypatch, ola)
    assert await skills.use_skill(game_db.scene(host, "!skill"), host) == 1
    assert bot.store.daily_cd.all() == [str(host.id), str(ela.id)]
    assert bot.store.skill_daily_cd.all() == [str(host.id)] and texts(game_db)[-1] == REST_DONE


@pytest.mark.parametrize("debug, seconds", [(True, 15), (False, 300)])
async def test_rest_lasts_five_minutes(game_db, daily, monkeypatch, debug, seconds):
    pauses: list[float] = []

    async def pause(length):
        pauses.append(length)

    monkeypatch.setattr(timing, "pause", pause)
    game_db.bot.debug = debug
    host = await with_gem(game_db, skills.REST)
    await skills.skill_rest(game_db.scene(host))
    assert pauses == [seconds]


async def test_rest_without_its_picture_or_its_panel_still_happens(game_db, daily, monkeypatch):
    """No picture; the invitation is purged in the middle of the rest: whoever clicked still rests."""
    monkeypatch.setattr(skills, "REST_IMAGES", 99)
    rig(monkeypatch, {(0, 98): 98})                                      # assets/events/rest/98.png is not there
    host, ola = await with_gem(game_db, skills.REST), game_db.add_member("Ola")
    game_db.bot.store.daily_cd.add(ola.id)

    async def purge(invitation) -> None:
        await invitation.delete()

        async def gone(**kwargs):
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "unknown message")

        invitation.edit = gone

    rest_clicks(game_db, monkeypatch, ola, during=purge)
    assert await skills.skill_rest(game_db.scene(host)) is True
    assert [m.files for m in game_db.channel.bot_messages[1:]] == [[]]   # the invitation is gone, "done" is here
    assert texts(game_db) == [REST_DONE]
    assert game_db.bot.store.daily_cd.all() == []


async def test_rest_that_cannot_be_announced_uses_nothing_up(game_db, daily):
    host = await with_gem(game_db, skills.REST)
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("discord broke"), 2
    with pytest.raises(RuntimeError):
        await skills.use_skill(game_db.scene(host), host)
    assert game_db.bot.store.skill_daily_cd.all() == [] and reaction_listeners(game_db) == []


async def test_gem_works_for_its_owner_whoever_started_the_scene(game_db, daily):
    host, admin = await with_gem(game_db, skills.REST), game_db.add_member("Alter")
    assert await skills.use_skill(game_db.scene(admin), host) == 1
    assert game_db.channel.contains("Ala zaprasza na odpoczynek!")
    assert game_db.bot.store.skill_daily_cd.all() == [str(host.id)]


# -- gem 2: a shiny pet ---------------------------------------------------------------

async def shiny_setup(game, *, pet: bool = True):
    """The gem's owner and a traveller (with a pet when ``pet``)."""
    owner = await with_gem(game, skills.SHINY)
    target = game.add_member("Ola")
    if pet:
        await add_owner(game.bot.db, target.id, 21)
        await add_pet(game.bot.db, 21)
        await add_pet(game.bot.db, 22)                                   # somebody else's pet stays as it is
    return owner, target


async def is_shiny(game, pet_id: int = 21) -> bool:
    return await game.bot.db.fetchval("SELECT SHINY FROM PETS WHERE PET_ID = $1", pet_id)


@pytest.mark.parametrize("mention", ["<@{}>", "<@!{}>", "  <@{}>  "])
async def test_shiny_gem_changes_the_tagged_travellers_pet(game_db, mention):
    owner, target = await shiny_setup(game_db)
    stranger = game_db.add_member("Jan")
    tags(game_db, stranger, f"<@{stranger.id}>")                         # only the owner of the gem is heard
    tags(game_db, owner, mention.format(target.id), delay=0.02)
    assert await skills.use_skill(game_db.scene(owner, "!skill"), owner) == 2
    assert texts(game_db) == [ASK, f"Twój Towarzysz <@{target.id}> wygląda na bardzo wdzięcznego i dostrzegasz, "
                                   "że wygląda jakoś inaczej <:Susge:973591024322633858> "
                                   f"To sprawka <@{owner.id}>."]
    assert await is_shiny(game_db) is True and await is_shiny(game_db, 22) is False
    assert game_db.bot.store.skill_weekly_cd.all() == [str(owner.id)]
    assert game_db.bot.store.skill_daily_cd.all() == [str(owner.id)]     # a weekly gem is that day's one use

    assert await skills.use_skill(game_db.scene(owner, "!skill"), owner) == 0
    assert texts(game_db)[-1] == weekly_cd() and game_db.channel.count(ASK) == 1


async def test_shiny_gem_answer_is_heard_only_on_the_bots_channel(game_db, short_timeouts):
    owner, target = await shiny_setup(game_db)
    game_db.channel.send_hooks.append(
        lambda message: message.content == ASK and game_db.say_later(
            owner, f"<@{target.id}>", 0, channel=game_db.announce_channel))
    assert await skills.skill_make_shiny(game_db.scene(owner)) is False
    assert texts(game_db) == [ASK, "Moc kamienia zanikła..."] and await is_shiny(game_db) is False


async def test_shiny_gem_fades_when_nobody_is_tagged(game_db, short_timeouts):
    owner, _ = await shiny_setup(game_db)
    assert await skills.use_skill(game_db.scene(owner), owner) == 2
    assert texts(game_db) == [ASK, "Moc kamienia zanikła..."]
    assert game_db.bot.store.skill_weekly_cd.all() == [str(owner.id)]    # legacy: the week is gone
    assert await is_shiny(game_db) is False


@pytest.mark.parametrize("answer", ["Ola", "<@Ola>", "<@12345678901234567890123>", "<@{gone}>", "{target}",
                                    "<@{target}> poproszę"])
async def test_shiny_gem_wasted_on_somebody_who_does_not_exist(game_db, answer):
    owner, target = await shiny_setup(game_db)
    gone = FakeMember("Były gracz")
    await add_owner(game_db.bot.db, gone.id, 22)                         # left the server, the pet is still stored
    tags(game_db, owner, answer.format(target=target.id, gone=gone.id))
    assert await skills.use_skill(game_db.scene(owner), owner) == 2
    assert texts(game_db) == [ASK, NO_TRAVELLER]
    assert game_db.bot.store.skill_weekly_cd.all() == [str(owner.id)]
    assert await is_shiny(game_db) is False and await is_shiny(game_db, 22) is False


@pytest.mark.parametrize("pet_id", [None, 0, 99])
async def test_shiny_gem_wasted_on_a_traveller_without_a_pet(game_db, pet_id):
    owner, target = await shiny_setup(game_db, pet=False)
    if pet_id is not None:
        await add_owner(game_db.bot.db, target.id, pet_id)               # 99: the PETS row does not exist
    tags(game_db, owner, f"<@{target.id}>")
    assert await skills.use_skill(game_db.scene(owner), owner) == 2
    assert texts(game_db) == [ASK, NO_PET]
    assert game_db.bot.store.skill_weekly_cd.all() == [str(owner.id)]


async def test_shiny_gem_survives_lag(game_db, monkeypatch):
    owner, target = await shiny_setup(game_db)
    real_ask, rounds = challenge.ask, []

    async def ask(bot, channel, prompt, **kwargs):
        rounds.append(prompt)
        if len(rounds) == 1:
            return RoundResult(Outcome.DISRUPTED)
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", ask)
    tags(game_db, owner, f"<@{target.id}>")
    assert await skills.use_skill(game_db.scene(owner), owner) == 2
    assert game_db.channel.count(challenge.MSG_DISRUPTED) == 1 and await is_shiny(game_db) is True


async def test_shiny_gem_is_not_used_up_by_connection_problems(game_db, monkeypatch):
    owner, _ = await shiny_setup(game_db)

    async def ask(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", ask)
    assert await skills.use_skill(game_db.scene(owner), owner) == 0
    assert texts(game_db) == [*[challenge.MSG_DISRUPTED] * (challenge.MAX_CONSECUTIVE_DISRUPTIONS - 1),
                              skills.MSG_INTERRUPTED]
    assert game_db.bot.store.skill_weekly_cd.all() == [] and await is_shiny(game_db) is False
    with pytest.raises(skills.SkillInterrupted):
        await skills.skill_make_shiny(game_db.scene(owner))


async def test_shiny_gem_is_used_up_even_when_the_good_news_cannot_be_sent(game_db):
    owner, target = await shiny_setup(game_db)

    def answer(message: FakeMessage) -> None:
        if message.content == ASK:
            game_db.say_later(owner, f"<@{target.id}>", 0)
            game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("discord broke"), 1

    game_db.channel.send_hooks.append(answer)
    assert await skills.use_skill(game_db.scene(owner), owner) == 2
    assert await is_shiny(game_db) is True
    assert game_db.bot.store.skill_weekly_cd.all() == [str(owner.id)]
    assert any("Nie udało się ogłosić efektu klejnotu" in line for line in game_db.bot.owner_log)


# -- gem 3: summoning a boss ----------------------------------------------------------

async def test_summoning_gem_asks_for_a_boss(game_db):
    player = await with_gem(game_db, skills.SUMMON)
    assert await skills.use_skill(game_db.scene(player), player) == 3
    assert texts(game_db) == [] and game_db.bot.store.skill_weekly_cd.all() == [str(player.id)]

    assert await skills.use_skill(game_db.scene(player), player) == 0    # once a week
    assert texts(game_db) == [weekly_cd()]


@pytest.mark.parametrize("phase", [BossPhase.PRE_SPAWN, BossPhase.VISIBLE, BossPhase.ENGAGED, BossPhase.FIGHTING])
async def test_summoning_gem_keeps_its_power_while_a_boss_is_around(game_db, phase):
    player = await with_gem(game_db, skills.SUMMON)
    await game_db.bot.state.set_boss_phase(phase)
    assert await skills.use_skill(game_db.scene(player), player) == 0
    assert texts(game_db) == [skills.MSG_BOSS_IN_THE_WAY.format(player_id=player.id)]
    assert game_db.bot.store.skill_weekly_cd.all() == []
    assert game_db.bot.state.boss_phase is phase                         # legacy forced BOSSALIVE = 4


@pytest.mark.parametrize("roll, boost, rarity", [(2, 0, 2), (3, 0, 4), (4, 0, 4), (2, 1, 4), (3, 1, 4), (4, 1, 4),
                                                 (2, 2, 4), (2, 7, 4)])
async def test_summoned_boss_rarity(game, summons, monkeypatch, roll, boost, rarity):
    asked = rig(monkeypatch, {(2, 4): roll})
    player = game.add_member("Ala")
    await modifiers.modify_modifiers(game.bot, "rarity_boost", boost)
    skills.weekly_skill_used(game.bot, player.id)
    assert await skills.skill_summon_boss(game.scene(player)) is True
    assert summons.calls == [(player, rarity)] and asked == [(2, 4)]
    assert game.bot.store.skill_weekly_cd.all() == [str(player.id)]
    assert texts(game) == []


async def test_summoning_gem_is_given_back_when_the_boss_does_not_come(game, summons):
    player = game.add_member("Ala")
    skills.weekly_skill_used(game.bot, player.id)
    summons.result = False                                               # the scheduled boss was faster
    assert await skills.skill_summon_boss(game.scene(player)) is False
    assert game.bot.store.skill_weekly_cd.all() == []
    assert texts(game) == [skills.MSG_BOSS_IN_THE_WAY.format(player_id=player.id)]

    skills.weekly_skill_used(game.bot, player.id)
    summons.error = ConnectionError("discord down")
    with pytest.raises(ConnectionError):
        await skills.skill_summon_boss(game.scene(player))
    assert game.bot.store.skill_weekly_cd.all() == []


# -- gems 4, 5, 6: blessings for the next boss ----------------------------------------

async def test_festival_gem_prays_five_times(game_db, prayers):
    player = await with_gem(game_db, skills.FESTIVAL)
    scene = game_db.scene(player, "!skill")
    assert await skills.use_skill(scene, player) == 4
    assert prayers == [(player, False, False)] * 5
    assert scene.message.reactions == ["<:prayge:1063891597760139304>"]
    assert texts(game_db) == ["Wygląda na to, że bossowie i potwory odczuwają efekty Twojego kamienia "
                              "umiejętności <:MonkaChrist:783992337075929098>"]
    assert game_db.bot.store.skill_daily_cd.all() == [str(player.id)]

    assert await skills.use_skill(game_db.scene(player), player) == 0
    assert texts(game_db)[-1] == skills.daily_cd_text() and len(prayers) == 5


async def test_festival_gem_draws_real_modifiers(game_db, daily, monkeypatch):
    player = await with_gem(game_db, skills.FESTIVAL)
    monkeypatch.setattr(random, "choice", lambda pool: "points_boost")
    rig(monkeypatch, {(1, 5): 4})
    await skills.use_skill(game_db.scene(player), player)
    assert modifier_file(game_db) == {"points_boost": 20}
    assert game_db.channel.count("Błogosławieństwo kapliczki!") == 0     # five silent prayers, one message


async def test_festival_gem_half_done_still_counts_as_used(game_db, monkeypatch):
    """A failure after the first prayer must not leave the gem ready to stack more."""
    player = await with_gem(game_db, skills.FESTIVAL)
    done: list[int] = []

    async def random_modifiers(scene, msg=True, sacrefice=False):
        done.append(len(done) + 1)
        if len(done) == 3:
            raise OSError("disk full")

    monkeypatch.setattr(modifiers, "random_modifiers", random_modifiers)
    assert await skills.use_skill(game_db.scene(player), player) == 4
    assert done == [1, 2, 3, 4, 5]
    assert game_db.bot.store.skill_daily_cd.all() == [str(player.id)]
    assert any("część efektu 3/5" in line for line in game_db.bot.owner_log)


async def test_festival_gem_that_did_nothing_is_not_used_up(game_db, monkeypatch):
    player = await with_gem(game_db, skills.FESTIVAL)

    async def random_modifiers(scene, msg=True, sacrefice=False):
        raise OSError("disk full")

    monkeypatch.setattr(modifiers, "random_modifiers", random_modifiers)
    with pytest.raises(OSError):
        await skills.use_skill(game_db.scene(player), player)
    assert game_db.bot.store.skill_daily_cd.all() == [] and texts(game_db) == []


async def test_slow_gem(game_db):
    player = await with_gem(game_db, skills.SLOW)
    assert await skills.use_skill(game_db.scene(player), player) == 5
    assert modifier_file(game_db) == {"time_increased_perc": 40}
    assert texts(game_db) == ["Klejnot sprawia, że przeciwnicy lepią się od błota i poruszają się znacznie wolniej."]
    assert game_db.bot.store.skill_daily_cd.all() == [str(player.id)]

    assert await skills.use_skill(game_db.scene(player), player) == 0
    assert texts(game_db)[-1] == skills.daily_cd_text() and modifier_file(game_db) == {"time_increased_perc": 40}
    assert game_db.bot.store.skill_weekly_cd.all() == []


async def test_abundance_gem(game_db):
    player = await with_gem(game_db, skills.LOOT)
    await modifiers.modify_modifiers(game_db.bot, "points_boost", 2)     # blessings add up
    assert await skills.use_skill(game_db.scene(player), player) == 6
    assert modifier_file(game_db) == {"drop_boost_perc": 40, "points_boost": 5}
    assert texts(game_db) == ["Klejnot sprawia, że przeciwnicy oferują teraz lepsze nagrody."]
    assert game_db.bot.store.skill_daily_cd.all() == [str(player.id)]
    assert await skills.skill_increase_loot(game_db.scene(player)) is False
    assert texts(game_db)[-1] == one_gem(player.id)


async def test_effect_that_cannot_be_announced_is_not_free_to_repeat(game_db):
    """Legacy: the exception skipped the cooldown, so +40 % could be stacked on every Discord hiccup."""
    player = await with_gem(game_db, skills.SLOW)
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("discord broke"), 1
    assert await skills.use_skill(game_db.scene(player), player) == 5
    assert modifier_file(game_db) == {"time_increased_perc": 40}
    assert game_db.bot.store.skill_daily_cd.all() == [str(player.id)]
    assert any("Efekt działa: Klejnot sprawia" in line for line in game_db.bot.owner_log)

    await skills.use_skill(game_db.scene(player), player)
    assert texts(game_db) == [skills.daily_cd_text()] and modifier_file(game_db) == {"time_increased_perc": 40}


# -- cooldown lists -------------------------------------------------------------------

async def test_cooldown_lists_keep_the_legacy_files(game):
    bot = game.bot
    assert not skills.load_daily_skill(bot, 11) and not skills.load_weekly_skill(bot, 11)
    skills.daily_skill_used(bot, 11)
    skills.daily_skill_used(bot, "11")
    skills.daily_skill_used(bot, 12)
    skills.weekly_skill_used(bot, 13)
    skills.weekly_skill_used(bot, 13)
    assert (bot.store.data_dir / "skill_daily_player_cd.txt").read_text() == "11\n12\n"
    assert (bot.store.data_dir / "skill_weekly_player_cd.txt").read_text() == "13\n"
    assert skills.load_daily_skill(bot, 11) and skills.load_daily_skill(bot, "12")
    assert not skills.load_daily_skill(bot, 13) and skills.load_weekly_skill(bot, 13)
    assert not skills.load_weekly_skill(bot, 11)

    await skills.clear_daily_skill(bot)
    assert bot.store.skill_daily_cd.all() == [] and bot.store.skill_weekly_cd.all() == ["13"]
    await skills.clear_weekly_skill(bot)
    assert bot.store.skill_weekly_cd.all() == []
    assert [m.content for m in game.log_channel.messages] == [
        "Zresetowano cooldown na daily skille.", "Zresetowano cooldown na weekly skille."]


async def test_clearing_cooldowns_does_not_need_the_log_channel(game):
    skills.daily_skill_used(game.bot, 11)
    skills.weekly_skill_used(game.bot, 11)
    del game.bot.fake_channels[game.bot.ids.log_channel]                 # legacy: None.send -> the reset died
    await skills.clear_daily_skill(game.bot)
    await skills.clear_weekly_skill(game.bot)
    assert not skills.load_daily_skill(game.bot, 11) and not skills.load_weekly_skill(game.bot, 11)


# -- the cog --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.skills")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
        channel.permissions_for = lambda member: member.guild_permissions
    guards._last_notice.clear()
    return game_db.bot.get_cog("Skills")


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
    assert type(cog).__name__ == "SkillsCog"
    table = {command.name: sorted(command.aliases) for command in cog.get_commands()}
    assert table == {
        "pokazskill": sorted(['pokazumiejetnosc', 'pokażumiejętność', 'pu', 'pokazskilla', 'ps']),
        "uzyjskilla": sorted(['użyjumiejetnosc', 'użyjumiejętność', 'us', 'uzyj', 'uzyjskill', 'uu', 'skill']),
        "klejnoty": sorted(["sakwa", "skille", "umiejetnosci", "umiejętności"]),
        "wybierzklejnot": sorted(["wybierzskill", "wybierzumiejetnosc", "wybierzumiejętność", "ws"]),
        "DajKlejnot": [],
        "ModifiersLoad": [], "GenerateSkill": [], "createSkillsDatabase": [], "skill_rest": []}
    assert all(command._buckets._cooldown is None for command in cog.get_commands())


async def test_showing_works_anywhere_using_only_on_the_boss_channel(game_db, cog):
    player = await with_gem(game_db, skills.SLOW)
    elsewhere = game_db.announce_channel
    for command in ("!pokazskill", "!pokazumiejetnosc", "!pokażumiejętność", "!pu", "!pokazskilla", "!ps"):
        await run(game_db, player, command, channel=elsewhere)
    assert [m.embeds[0].title for m in elsewhere.bot_messages] == ["Klejnot Spowolnienia"] * 6

    for command in ("!uzyjskilla", "!skill", "!us", "!uu"):
        await run(game_db, player, command, channel=elsewhere)
    assert len(elsewhere.bot_messages) == 6 and modifier_file(game_db) == {}
    assert game_db.bot.store.skill_daily_cd.all() == []

    await run(game_db, player, "!skill")                                 # ... and on the boss channel it works
    assert modifier_file(game_db) == {"time_increased_perc": 40} and not game_db.bot.state.busy
    for command in ("!uzyjskilla", "!użyjumiejetnosc", "!użyjumiejętność", "!us", "!uzyj", "!uzyjskill", "!uu"):
        await run(game_db, player, command)
    assert texts(game_db)[1:] == [skills.daily_cd_text()] * 7


async def test_commands_are_blocked_with_a_notice_while_the_bot_is_busy(game_db, cog):
    """Legacy ``!uzyjskilla`` died with UnboundLocalError here."""
    player, boss = await with_gem(game_db, skills.SLOW), admin(game_db)
    with game_db.bot.state.hold_busy("event:TEST"):
        for command in ("!pokazskill", "!skill", "!uzyjskilla"):
            await run(game_db, player, command)
        await run(game_db, boss, "!skill_rest")
        assert game_db.bot.state.busy_owner == "event:TEST"              # nobody took the flag over
    assert texts(game_db) == [guards.BUSY_NOTICE]
    assert modifier_file(game_db) == {} and game_db.bot.store.skill_daily_cd.all() == []


async def test_using_a_gem_holds_the_channel_and_always_lets_go(game_db, cog, daily, monkeypatch):
    host, other = await with_gem(game_db, skills.REST), game_db.add_member("Ola")
    gate = asyncio.Event()

    async def pause(seconds):
        await gate.wait()

    monkeypatch.setattr(timing, "pause", pause)
    task = asyncio.ensure_future(run(game_db, host, "!skill"))
    await until(lambda: game_db.channel.contains(INVITATION))
    assert game_db.bot.state.busy and game_db.bot.state.busy_owner == "skill"
    await run(game_db, other, "!pokazskill")                             # blocked meanwhile
    assert game_db.channel.contains(guards.BUSY_NOTICE)
    gate.set()
    await task
    assert not game_db.bot.state.busy and game_db.channel.contains(REST_DONE)

    # A crash in the middle (legacy: BUSY stayed 1 until the restart).
    game_db.bot.store.skill_daily_cd.clear()
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("discord broke"), 2
    await run(game_db, host, "!skill")
    assert not game_db.bot.state.busy and reaction_listeners(game_db) == []
    assert game_db.bot.store.skill_daily_cd.all() == []
    assert any("RuntimeError" in line for line in game_db.bot.owner_log)


async def test_database_outage_during_a_command(game_db, cog):
    player = game_db.add_member("Ala")
    game_db.bot.db = DatabaseDown()
    await run(game_db, player, "!skill")
    await run(game_db, player, "!pokazskill")
    assert not game_db.bot.state.busy
    assert texts(game_db) == ["Baza danych jest chwilowo niedostępna. Spróbuj ponownie za moment "
                              "<:Sadge:936907659142111273>"] * 2


async def test_summoning_gem_puts_a_boss_on_the_channel(game_db, cog, monkeypatch):
    rig(monkeypatch, {(2, 4): 3})                                        # 3 is never summoned: it becomes 4
    player, second = await with_gem(game_db, skills.SUMMON), game_db.add_member("Ola")
    await add_owner(game_db.bot.db, second.id, skill_gem=5)
    await run(game_db, player, "!skill")
    state = game_db.bot.state
    assert state.boss_phase is BossPhase.VISIBLE and state.boss_rarity == 4 and not state.busy
    assert game_db.channel.contains("Pojawił się")                       # the boss embed
    assert game_db.bot.store.skill_weekly_cd.all() == [str(player.id)]

    await run(game_db, second, "!skill")                                 # legacy: wiped the channel, new boss
    assert texts(game_db)[-1] == skills.MSG_BOSS_IN_THE_WAY.format(player_id=second.id)
    assert state.boss_phase is BossPhase.VISIBLE and game_db.bot.store.skill_weekly_cd.all() == [str(player.id)]
    await run(game_db, player, "!skill")
    assert texts(game_db)[-1] == weekly_cd()


async def test_summoning_gem_is_given_back_when_summoning_fails(game_db, cog, summons):
    player = await with_gem(game_db, skills.SUMMON)
    summons.error = ConnectionError("discord down")
    await run(game_db, player, "!skill")
    assert len(summons.calls) == 1 and not game_db.bot.state.busy
    assert game_db.bot.store.skill_weekly_cd.all() == []


async def test_admin_commands(game_db, cog, daily):
    db = game_db.bot.db
    player, boss = game_db.add_member("Ala"), admin(game_db)
    await db.execute("DROP TABLE SKILLS")
    for command in ("!ModifiersLoad", "!GenerateSkill", "!createSkillsDatabase", "!skill_rest"):
        await run(game_db, player, command)                              # not for players
    assert texts(game_db) == []
    assert await db.fetchval("SELECT to_regclass('public.skills')") is None

    await run(game_db, boss, "!createSkillsDatabase")
    await run(game_db, boss, "!createSkillsDatabase")
    await run(game_db, boss, "!GenerateSkill")
    await run(game_db, boss, "!ModifiersLoad")
    assert texts(game_db) == ["Baza danych SKILLS utworzona.", "Baza danych SKILLS utworzona.",
                              "Wygenerowano klejnot umiejętności (SKILL_ID 1).", "Modifiers file loaded."]
    assert [gem[0] for gem in await gems(db)] == [0, 1]

    await run(game_db, boss, "!skill_rest", channel=game_db.announce_channel)   # admin commands work anywhere
    assert [m.text for m in game_db.announce_channel.bot_messages][-1] == REST_DONE
    assert game_db.announce_channel.count(INVITATION) == 1 and not game_db.bot.state.busy
    assert game_db.bot.store.skill_daily_cd.all() == []                  # legacy: the debug command marks nothing
