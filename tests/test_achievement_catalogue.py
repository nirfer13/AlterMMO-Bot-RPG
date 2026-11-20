"""features/achievements.py, grown in v2: the catalogue, the roles the bot makes, the new counters,
the pet achievements, the hand-out to veterans and ``$osiagniecia``.

Owner's decisions of 2026-11-20 tested here: only supporters get the roles (everybody's deeds are
counted); a supporter gets the highest tier reached, also for what was earned before (veterans,
players who started to support later); a higher tier replaces the lower ones; the veteran hand-out
is silent with one summary for the owner and waits while something plays on the boss channel;
``$osiagniecia`` shows what the player has and "X z Y" - never which achievements are missing.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import discord
import pytest
import pytest_asyncio

from altermmo import schema
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossPhase
from altermmo.features import achievements, patrons
from altermmo.features.achievements import TROPHIES, role_id_of, trophy
from altermmo.settings import Ids

from fakes import FakeMember

PATRON = patrons.PATRON_ROLES[0]
KILL_1, KILL_10, KILL_30 = achievements.BOSS_KILL_ROLES.values()
HUNT_10, HUNT_35, HUNT_100 = achievements.HUNT_ROLES.values()
TOWER_3, TOWER_6, TOWER_10 = achievements.TOWER_ROLES
QUICK = achievements.QUICKNESS_ROLE

BOT_MADE = [item for item in TROPHIES if item.bot_made]
FIXED = [item for item in TROPHIES if item.role_id]
CHAINS = sorted({item.chain for item in TROPHIES if item.chain})
GAME_CHANNELS = ("channel", "announce_channel", "notice_channel")


class DatabaseDown:
    """A database that cannot be reached at all."""

    def _fail(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    transaction = _fail

    async def execute(self, *args, **kwargs):
        self._fail()

    fetch = fetchrow = fetchval = execute


class _Clock:
    """What :mod:`achievements` sees as ``time``: ``monotonic`` is moved by the test (no real waiting)."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    """The clock of the back-off after a refused role (only the achievements module sees it)."""
    fake = _Clock()
    monkeypatch.setattr(achievements, "time", fake)
    return fake


def forbidden() -> discord.Forbidden:
    """What Discord answers when the bot may not make roles."""
    return discord.Forbidden(type("Response", (), {"status": 403, "reason": "Forbidden"})(), "Missing Permissions")


def record_creations(game, monkeypatch) -> list[str]:
    """Names of the roles the bot asks the server to make, refused ones included."""
    asked: list[str] = []
    create = game.guild.create_role

    async def recording(*, name, **kwargs):
        asked.append(name)
        return await create(name=name, **kwargs)

    monkeypatch.setattr(game.guild, "create_role", recording)
    return asked


def refuse_names(game, monkeypatch) -> set[str]:
    """The server refuses to make roles of the names in the returned set (empty it to allow them)."""
    refused: set[str] = set()
    create = game.guild.create_role

    async def picky(*, name, **kwargs):
        if name in refused:
            raise forbidden()
        return await create(name=name, **kwargs)

    monkeypatch.setattr(game.guild, "create_role", picky)
    return refused


def pings(game) -> int:
    """Messages on the owner's log channel that ping the owner."""
    return game.log_channel.sent_count(f"<@{game.bot.ids.owner}>")


def owner_lines(bot, start: str) -> list[str]:
    return [line for line in bot.owner_log if line.startswith(start)]


@pytest_asyncio.fixture
async def game_db(game_db):
    """The test database with the tables that are new in v2 (the bot makes them at start-up)."""
    await schema.create_new_tables(game_db.bot.db)
    return game_db


@pytest.fixture
def old_roles(game):
    """The achievement roles the server had before v2 (the bot never makes these)."""
    return {item.role_id: game.guild.add_role(item.role_id, item.name) for item in FIXED}


def role_ids(member) -> set[int]:
    return {role.id for role in member.roles} - set(patrons.SUPPORTER_ROLES)


def keys_of(bot, member) -> set[str]:
    """Keys of the achievements whose roles the member holds."""
    held = {role.id for role in member.roles}
    return {item.key for item in TROPHIES if role_id_of(bot, item) in held}


def give(game, member, *keys: str) -> None:
    """Hand the member the roles of ``keys`` directly (as a moderator would)."""
    for key in keys:
        role_id = role_id_of(game.bot, trophy(key))
        assert role_id is not None, f"role of {key} not made yet"
        member.roles.append(game.guild.add_role(role_id, trophy(key).name))


def stored(bot) -> dict:
    return bot.store.achievement_roles.load()


def announcements(game) -> int:
    return sum(getattr(game, name).sent_count("zdobywa osiągnięcie") for name in GAME_CHANNELS)


def game_channel_messages(game) -> int:
    return sum(len(getattr(game, name).sent) for name in GAME_CHANNELS)


async def set_kills(db, player_id: int, kills: int = 0, hunts: int = 0, butchers: int = 0) -> None:
    await db.execute("INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS, HUNTS, BUTCHERS) VALUES ($1, $2, $3, $4)",
                     player_id, kills, hunts, butchers)


async def set_counter(db, player_id: int, name: str, value: int) -> None:
    await db.execute("INSERT INTO ACHIEVEMENT_COUNTERS (PLAYER_ID, NAME, VALUE) VALUES ($1, $2, $3)",
                     player_id, name, value)


async def set_tower(db, player_id: int, level: int, time: str | None) -> None:
    await db.execute("INSERT INTO TOWER (PLAYER_ID, TOWER_LEVEL, TIME) VALUES ($1, $2, $3)",
                     player_id, level, time)


async def add_pet(db, pet_id: int, *, skills: int = 1, ultra: bool | None = False, mythic: bool = False,
                  seal: int = 0, holo: bool = False) -> int:
    await db.execute(
        """INSERT INTO PETS (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY, MYTHIC, SEAL, HOLO)
           VALUES ($1, 'Pupil', 1, $2, 'Zwykły', FALSE, $3, $4, $5, $6)""",
        pet_id, skills, ultra, mythic, seal, holo)
    return pet_id


async def set_pets(db, player_id: int, active: int = 0, *stable: int) -> None:
    """The player's active pet and the four places of the stable (0 = empty)."""
    slots = list(stable) + [0] * (4 - len(stable))
    await db.execute(
        """INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED, PET_ID_ALT1, PET_ID_ALT2, PET_ID_ALT3, PET_ID_ALT4)
           VALUES ($1, $2, TRUE, $3, $4, $5, $6)""", player_id, active, *slots)


# -- the catalogue, pure ----------------------------------------------------------------

def test_catalogue_has_39_achievements_with_unique_keys_and_names():
    assert len(TROPHIES) == 39
    keys = [item.key for item in TROPHIES]
    assert len(set(keys)) == len(keys)
    names = [item.name for item in TROPHIES]
    assert len(set(names)) == len(names)                     # the bot makes roles under these names
    assert all(trophy(item.key) is item for item in TROPHIES)


def test_old_achievements_keep_their_roles_and_23_new_ones_are_made_by_the_bot():
    fixed_ids = [item.role_id for item in FIXED]
    assert len(set(fixed_ids)) == len(fixed_ids) == 15
    assert set(fixed_ids) == {*achievements.BOSS_KILL_ROLES.values(), *achievements.HUNT_ROLES.values(),
                              *achievements.BUTCHER_ROLES.values(), *achievements.TOWER_ROLES,
                              achievements.UNICORN_ROLE, achievements.LEGEND_ROLE, QUICK}
    assert len(BOT_MADE) == 23
    assert all(not item.role_id and not item.ids_attr for item in BOT_MADE)
    by_attr = [item for item in TROPHIES if item.ids_attr]
    assert [(item.key, item.ids_attr) for item in by_attr] == [("mythic_boss", "mythic_slayer_role")]
    assert all(hasattr(Ids(), item.ids_attr) for item in by_attr)
    assert not set(fixed_ids) & set(patrons.SUPPORTER_ROLES)


@pytest.mark.parametrize("chain", CHAINS)
def test_tiers_of_a_family_are_listed_lowest_first(chain):
    tiers = [item for item in TROPHIES if item.chain == chain]
    positions = [TROPHIES.index(item) for item in tiers]
    assert positions == list(range(positions[0], positions[0] + len(tiers)))   # side by side
    limits = [item.threshold for item in tiers]
    assert all(limit > 0 for limit in limits)
    if chain == "quick":                                     # a time limit: the higher tier is faster
        assert limits == sorted(limits, reverse=True) and len(set(limits)) == len(limits)
        assert tuple(limits) == achievements.QUICK_LIMITS == (2.0, 1.0, 0.75)
    else:
        assert limits == sorted(limits) and len(set(limits)) == len(limits)
    assert achievements.chain_of(tiers[-1]) == tuple(tiers)


def test_the_counted_families_and_their_milestones():
    def thresholds(chain):
        return [int(item.threshold) for item in TROPHIES if item.chain == chain]

    assert thresholds("boss_kills") == [1, 10, 30, 100, 250, 500]
    assert thresholds("hunts") == [10, 35, 100, 250, 500, 1000]
    assert thresholds("butchers") == [1, 3, 10, 25]
    assert thresholds("tower") == [3, 6, 10]
    assert thresholds("tournaments") == [1]
    assert thresholds("duels") == [5, 25]
    assert thresholds("chests") == [10, 50]
    assert thresholds("rituals") == [5, 20]
    for counter in achievements.COUNTERS:                    # every new counter is a family of tiers
        assert counter in CHAINS
        assert all(item.bot_made for item in TROPHIES if item.chain == counter)


def test_role_of_an_achievement_without_the_bot():
    assert role_id_of(None, trophy("boss_kills_30")) == KILL_30
    assert role_id_of(None, trophy("quick_1")) == QUICK
    assert role_id_of(None, trophy("unicorn")) == achievements.UNICORN_ROLE
    assert role_id_of(None, trophy("mythic_boss")) is None   # the bot's settings name it
    assert all(role_id_of(None, item) is None for item in BOT_MADE)


def test_lower_tiers_without_the_bot_know_only_the_old_roles():
    assert achievements.lower_tier_roles(KILL_30) == (KILL_1, KILL_10)
    assert achievements.lower_tier_roles(KILL_1) == ()
    assert achievements.lower_tier_roles(HUNT_100) == (HUNT_10, HUNT_35)
    assert achievements.lower_tier_roles(TOWER_10) == (TOWER_3, TOWER_6)
    assert achievements.lower_tier_roles(QUICK) == ()
    assert achievements.lower_tier_roles(achievements.LEGEND_ROLE) == ()
    assert achievements.lower_tier_roles(424242) == ()


# -- roles of the achievements with the bot ------------------------------------------------

async def test_role_of_an_achievement_with_the_bot(game):
    bot = game.bot
    assert role_id_of(bot, trophy("mythic_boss")) == bot.ids.mythic_slayer_role
    assert role_id_of(bot, trophy("boss_kills_10")) == KILL_10
    assert all(role_id_of(bot, item) is None for item in BOT_MADE)   # not made yet
    await achievements.ensure_roles(bot)
    made = {item.key: role_id_of(bot, item) for item in BOT_MADE}
    assert all(made.values()) and len(set(made.values())) == len(made)
    assert all(game.guild.get_role(role_id).name == trophy(key).name for key, role_id in made.items())


async def test_lower_tiers_with_the_bot_include_the_roles_it_made(game):
    bot = game.bot
    await achievements.ensure_roles(bot)

    def role(key):
        return role_id_of(bot, trophy(key))

    assert achievements.lower_tier_roles(role("boss_kills_500"), bot) == (
        KILL_1, KILL_10, KILL_30, role("boss_kills_100"), role("boss_kills_250"))
    assert achievements.lower_tier_roles(role("boss_kills_100"), bot) == (KILL_1, KILL_10, KILL_30)
    assert achievements.lower_tier_roles(KILL_30, bot) == (KILL_1, KILL_10)
    assert achievements.lower_tier_roles(role("butchers_25"), bot) == tuple(achievements.BUTCHER_ROLES.values())
    assert achievements.lower_tier_roles(role("quick_3"), bot) == (QUICK, role("quick_2"))
    assert achievements.lower_tier_roles(role("quick_2"), bot) == (QUICK,)
    assert achievements.lower_tier_roles(role("duels_25"), bot) == (role("duels_5"),)
    assert achievements.lower_tier_roles(role("chests_50"), bot) == (role("chests_10"),)
    assert achievements.lower_tier_roles(role("rituals_20"), bot) == (role("rituals_5"),)
    for key in ("tournaments_1", "duels_5", "tower_sprint", "pet_holo", "stable_full"):
        assert achievements.lower_tier_roles(role(key), bot) == ()
    assert achievements.lower_tier_roles(bot.ids.mythic_slayer_role, bot) == ()
    # Without the bot the roles it made are unknown: nothing is taken away by mistake.
    assert achievements.lower_tier_roles(role("boss_kills_100")) == ()


async def test_ensure_roles_makes_the_missing_roles_without_permissions(game, old_roles):
    bot = game.bot
    before = len(game.guild.roles)
    made = await achievements.ensure_roles(bot)
    assert made == [item.name for item in BOT_MADE]
    assert len(game.guild.roles) == before + 23              # the old roles are not made again
    file = stored(bot)
    assert set(file) == {item.key for item in BOT_MADE}
    for item in BOT_MADE:
        role = game.guild.get_role(file[item.key])
        assert role is not None and role.name == item.name
        asked = role.created_with
        assert asked["permissions"] == discord.Permissions.none()
        assert asked["hoist"] is False and asked["mentionable"] is False
        assert asked["colour"] == discord.Colour(achievements.ROLE_COLOUR)
        assert asked["reason"] == achievements.ROLE_REASON
    assert bot._achievement_roles == file                    # what the bot remembers = the file
    created = [line for line in bot.owner_log if line.startswith("Utworzyłem role osiągnięć (23)")]
    assert len(created) == 1 and "Pełna stajnia" in created[0]
    assert game_channel_messages(game) == 0

    assert await achievements.ensure_roles(bot) == []        # a second call makes nothing
    assert len(game.guild.roles) == before + 23
    assert stored(bot) == file and len(bot.owner_log) == 1


def roles_of_an_earlier_run(game, *, without: str = "") -> dict[str, int]:
    """The 23 roles the bot made in an earlier run, on the server, with their ids saved in the file;
    the role of ``without`` was deleted on the server since (its saved id points nowhere)."""
    saved = {item.key: game.guild.add_role(990_000_000_000_000_100 + index, item.name).id
             for index, item in enumerate(BOT_MADE) if item.key != without}
    if without:
        saved[without] = 990_000_000_000_000_999
    game.bot.store.achievement_roles.save(saved)
    return saved


async def test_role_deleted_on_the_server_is_made_again(game):
    bot = game.bot
    first = roles_of_an_earlier_run(game, without="pet_holo")
    before = len(game.guild.roles)
    assert achievements.role_exists(bot, trophy("pet_holo")) is False
    assert await achievements.ensure_roles(bot) == ["Folia Holo"]
    again = stored(bot)
    assert again["pet_holo"] != first["pet_holo"] and game.guild.get_role(again["pet_holo"]).name == "Folia Holo"
    assert {key: value for key, value in again.items() if key != "pet_holo"} == {
        key: value for key, value in first.items() if key != "pet_holo"}
    assert role_id_of(bot, trophy("pet_holo")) == again["pet_holo"]
    assert achievements.role_exists(bot, trophy("pet_holo")) is True
    assert len(game.guild.roles) == before + 1
    assert owner_lines(bot, "Utworzyłem role osiągnięć") == [
        achievements.LOG_ROLES_CREATED.format(count=1, names="Folia Holo")]


async def test_role_made_in_this_run_and_deleted_later_is_made_again(game, clock):
    bot = game.bot
    await achievements.ensure_roles(bot)
    first = role_id_of(bot, trophy("pet_holo"))
    assert achievements.role_exists(bot, trophy("pet_holo"))          # Discord's cache knows it by now
    clock.now += achievements.ROLE_RETRY_AFTER
    game.guild.roles.remove(game.guild.get_role(first))               # the owner deletes it by mistake
    assert achievements.role_exists(bot, trophy("pet_holo")) is False  # $roleosiagniec: "missing"
    assert await achievements.ensure_roles(bot) == ["Folia Holo"]
    assert role_id_of(bot, trophy("pet_holo")) != first
    assert game.guild.get_role(role_id_of(bot, trophy("pet_holo"))).name == "Folia Holo"


async def test_failure_to_make_roles_is_reported_to_the_owner_and_not_raised(game):
    bot = game.bot
    game.guild.create_role_error = discord.Forbidden(
        type("Response", (), {"status": 403, "reason": "Forbidden"})(), "Missing Permissions")
    assert await achievements.ensure_roles(bot) == []
    assert stored(bot) == {}
    failed = [line for line in bot.owner_log if line.startswith("Nie udało się utworzyć ról osiągnięć")]
    assert len(failed) == 1 and "Folia Holo" in failed[0] and "$roleosiagniec" in failed[0]
    assert game.log_channel.contains(f"<@{bot.ids.owner}>")  # the owner is pinged
    assert game_channel_messages(game) == 0

    game.guild.create_role_error = None                      # the permission was given back
    assert len(await achievements.ensure_roles(bot)) == 23


async def test_one_role_that_cannot_be_made_does_not_stop_the_others(game, monkeypatch):
    bot = game.bot
    create = game.guild.create_role

    async def picky(*, name, **kwargs):
        if name == "Kultysta II":
            raise RuntimeError("Maximum number of guild roles reached (250)")
        return await create(name=name, **kwargs)

    monkeypatch.setattr(game.guild, "create_role", picky)
    made = await achievements.ensure_roles(bot)
    assert len(made) == 22 and "Kultysta II" not in made
    assert "rituals_20" not in stored(bot) and len(stored(bot)) == 22
    assert any(line.startswith("Nie udało się utworzyć ról osiągnięć: Kultysta II.") for line in bot.owner_log)
    monkeypatch.setattr(game.guild, "create_role", create)
    assert await achievements.ensure_roles(bot) == ["Kultysta II"]


async def test_roles_file_from_an_earlier_run_is_used_and_read_once(game):
    bot = game.bot
    kept = game.guild.add_role(990_000_000_000_000_100, "Folia Holo (renamed by the owner)")
    bot.store.achievement_roles.save({"pet_holo": kept.id, "duels_5": 0, "not_an_achievement": "x"})
    made = await achievements.ensure_roles(bot)
    assert "Folia Holo" not in made and len(made) == 22      # the owner's renamed role is kept
    assert role_id_of(bot, trophy("pet_holo")) == kept.id
    assert stored(bot)["pet_holo"] == kept.id and stored(bot) == bot._achievement_roles
    # The file is read once; what the bot made since is remembered, not read back.
    bot.store.achievement_roles.save({})
    assert role_id_of(bot, trophy("pet_holo")) == kept.id
    assert await achievements.ensure_roles(bot) == []


def break_file(path, how: str) -> None:
    """Make the saved role ids unreadable: corrupt text, or a directory in the file's place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if how == "directory":
        path.mkdir()                                         # reading it raises an OSError
    else:
        path.write_text(how, encoding="utf-8")


def file_state(path) -> str:
    return "<directory>" if path.is_dir() else path.read_text(encoding="utf-8")


@pytest.mark.parametrize("broken", ["[1, 2, ", '{"pet_holo": 99', "[1, 2]", "directory"])
async def test_unreadable_roles_file_means_no_role_is_made(game, broken):
    """Making the roles again would duplicate all 23 and lose the ids of the roles players hold."""
    bot = game.bot
    path = bot.store.achievement_roles.path
    break_file(path, broken)
    damaged = file_state(path)
    patron = game.add_member("Patron", roles=[PATRON])
    before = len(game.guild.roles)
    assert achievements.roles_file_readable(bot) is False
    assert await achievements.ensure_roles(bot) == []        # at start-up
    assert await achievements.ensure_roles(bot) == []        # $roleosiagniec
    for key in ("pet_holo", "duels_5", "stable_full", "pet_holo"):
        assert await achievements.grant(game.scene(), patron, key) is False
    assert len(game.guild.roles) == before
    assert file_state(path) == damaged                       # the file is never overwritten
    told = owner_lines(bot, "Nie mogę odczytać pliku z rolami osiągnięć")
    assert told == [achievements.LOG_ROLES_FILE_UNREADABLE.format(path=path)]
    assert len(bot.owner_log) == 1 and pings(game) == 1      # once, with a ping
    assert role_ids(patron) == set() and game_channel_messages(game) == 0

    # The owner mends the file: the roles in it are used, only the missing ones are made.
    if path.is_dir():
        path.rmdir()
    holo = game.guild.add_role(990_000_000_000_000_100, "Folia Holo (renamed by the owner)")
    bot.store.achievement_roles.save({"pet_holo": holo.id})
    assert achievements.roles_file_readable(bot) is True
    assert await achievements.grant(game.scene(), patron, "pet_holo") is True
    assert role_ids(patron) == {holo.id}
    made = await achievements.ensure_roles(bot)
    assert len(made) == 22 and "Folia Holo" not in made
    assert stored(bot)["pet_holo"] == holo.id and len(stored(bot)) == 23


@pytest.mark.parametrize("content", [None, "", "  \n"])
async def test_missing_or_empty_roles_file_means_the_roles_are_made(game, content):
    bot = game.bot
    path = bot.store.achievement_roles.path
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    assert achievements.roles_file_readable(bot) is True
    assert len(await achievements.ensure_roles(bot)) == 23
    assert set(stored(bot)) == {item.key for item in BOT_MADE}
    assert owner_lines(bot, "Nie mogę odczytać") == []


async def test_unreadable_roles_file_gives_no_lower_tier_to_a_holder_of_a_higher_one(game, old_roles):
    bot = game.bot
    higher = game.guild.add_role(990_000_000_000_000_500, "Pogromca bossów IV")   # made in an earlier run
    veteran = game.add_member("Weteran", roles=[PATRON])
    veteran.roles.append(higher)
    break_file(bot.store.achievement_roles.path, '{"boss_kills_100": 990000000000000500, ')
    assert await achievements.grant_reached(game.scene(), veteran, "boss_kills", 101) is None
    assert role_ids(veteran) == {higher.id}
    assert announcements(game) == 0


async def test_ensure_roles_without_the_server_does_nothing(game, monkeypatch):
    monkeypatch.setattr(game.bot, "get_guild", lambda guild_id: None)
    assert await achievements.ensure_roles(game.bot) == []
    assert stored(game.bot) == {} and game.bot.owner_log == []


async def test_ensure_roles_while_the_server_is_unavailable_does_nothing_and_does_not_wait(game, monkeypatch,
                                                                                          clock):
    bot = game.bot
    patron = game.add_member("Patron", roles=[PATRON])
    before = len(game.guild.roles)
    monkeypatch.setattr(game.guild, "unavailable", True, raising=False)   # a Discord outage
    assert await achievements.ensure_roles(bot) == []
    assert await achievements.grant(game.scene(), patron, "pet_holo") is False
    assert len(game.guild.roles) == before and stored(bot) == {} and bot.owner_log == []
    monkeypatch.setattr(game.guild, "unavailable", False)
    # An outage is not a refusal: the next deed makes the role at once (no back-off).
    assert await achievements.grant(game.scene(), patron, "pet_holo") is True
    assert keys_of(bot, patron) == {"pet_holo"}


async def test_a_role_of_the_same_name_without_permissions_is_taken_over(game):
    """The saved ids were lost but the roles are there: no duplicates.  Other roles of that name stay alone."""
    bot = game.bot
    ours = game.guild.add_role(990_000_000_000_000_100, "Folia Holo")
    powerful = game.guild.add_role(990_000_000_000_000_101, "Pełna stajnia")
    powerful.permissions = discord.Permissions(manage_messages=True)
    managed = game.guild.add_role(990_000_000_000_000_102, "Ultra Shiny")   # e.g. an integration's role
    managed.managed = True
    mapped = game.guild.add_role(990_000_000_000_000_103, "Boski talent")   # the owner renamed another one
    bot.store.achievement_roles.save({"pet_mythic": mapped.id})
    patron = game.add_member("Patron", roles=[PATRON])
    before = len(game.guild.roles)

    made = await achievements.ensure_roles(bot)
    assert "Folia Holo" not in made and "Mityczny towarzysz" not in made and len(made) == 21
    assert {"Pełna stajnia", "Ultra Shiny", "Boski talent"} <= set(made)
    assert len(game.guild.roles) == before + 21
    file = stored(bot)
    assert file["pet_holo"] == ours.id and file["pet_mythic"] == mapped.id and len(file) == 23
    for key, other in (("stable_full", powerful), ("pet_ultra", managed), ("pet_divine", mapped)):
        assert file[key] != other.id and game.guild.get_role(file[key]).name == other.name
    assert owner_lines(bot, "Role osiągnięć, które już były na serwerze") == [
        achievements.LOG_ROLES_ADOPTED.format(count=1, names="Folia Holo")]
    assert len(owner_lines(bot, "Utworzyłem role osiągnięć (21)")) == 1

    assert await achievements.grant(game.scene(), patron, "pet_holo") is True
    assert role_ids(patron) == {ours.id}
    assert await achievements.ensure_roles(bot) == [] and len(game.guild.roles) == before + 21


async def test_a_role_just_made_is_used_before_discords_cache_knows_it(game, monkeypatch):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    asked = record_creations(game, monkeypatch)
    known = {role.id for role in game.guild.roles}
    lookup = game.guild.get_role
    # Discord's cache learns about a new role a moment after it is made: here, never.
    monkeypatch.setattr(game.guild, "get_role", lambda role_id: lookup(role_id) if int(role_id) in known else None)

    assert await achievements.grant(scene, patron, "pet_holo") is True
    holo = role_id_of(bot, trophy("pet_holo"))
    assert game.guild.get_role(holo) is None and achievements.role_exists(bot, trophy("pet_holo"))
    assert role_ids(patron) == {holo}
    assert game.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Folia Holo!"

    made = await achievements.ensure_roles(bot)               # start-up / $roleosiagniec
    assert len(made) == 22 and "Folia Holo" not in made
    assert await achievements.ensure_roles(bot) == []
    assert sorted(asked) == sorted(item.name for item in BOT_MADE)   # every role made exactly once

    assert await achievements.grant(scene, patron, "duels_5") is True
    assert await achievements.grant(scene, patron, "duels_25") is True   # replaces duels_5, also unseen
    assert keys_of(bot, patron) == {"pet_holo", "duels_25"}
    assert announcements(game) == 3


# -- after a refusal: the back-off and what the owner is told ------------------------------------

async def test_after_a_refusal_grant_does_not_ask_again_until_the_back_off_is_over(game, monkeypatch, clock):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    asked = record_creations(game, monkeypatch)
    game.guild.create_role_error = forbidden()

    assert await achievements.grant(scene, patron, "pet_holo") is False
    assert asked == ["Folia Holo"]                           # only its own role, not the other 22
    for key in ("pet_holo", "duels_5", "stable_full"):       # the next deeds do not ask the server
        assert await achievements.grant(scene, patron, key) is False
    clock.now += achievements.ROLE_RETRY_AFTER - 1
    assert await achievements.grant(scene, patron, "pet_holo") is False
    assert asked == ["Folia Holo"]
    clock.now += 2                                           # the hour is over: asked again
    assert await achievements.grant(scene, patron, "pet_holo") is False
    assert asked == ["Folia Holo"] * 2

    game.guild.create_role_error = None                      # the permission was given back ...
    assert await achievements.grant(scene, patron, "pet_holo") is False   # ... a new hour runs since the refusal
    assert asked == ["Folia Holo"] * 2
    clock.now += achievements.ROLE_RETRY_AFTER + 1
    assert await achievements.grant(scene, patron, "pet_holo") is True
    assert asked == ["Folia Holo"] * 3
    assert keys_of(bot, patron) == {"pet_holo"} and stored(bot) == {"pet_holo": role_id_of(bot, trophy("pet_holo"))}
    assert len(owner_lines(bot, "Nie udało się utworzyć ról osiągnięć")) == 1


async def test_a_forced_ensure_roles_ignores_the_back_off_and_its_success_clears_it(game, clock):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    holo = game.guild.add_role(990_000_000_000_000_100, "Folia Holo (from an earlier run)")
    bot.store.achievement_roles.save({"pet_holo": holo.id})
    game.guild.create_role_error = forbidden()
    assert await achievements.grant(scene, patron, "duels_5") is False    # refused: an hour of quiet
    game.guild.create_role_error = None
    clock.now += 60
    assert await achievements.grant(scene, patron, "duels_5") is False    # still within the hour

    assert len(await achievements.ensure_roles(bot)) == 22               # $roleosiagniec does not wait
    assert await achievements.grant(scene, patron, "duels_5") is True
    game.guild.roles.remove(holo)                                         # deleted on the server
    assert await achievements.grant(scene, patron, "pet_holo") is True   # made again at once: no back-off
    assert role_id_of(bot, trophy("pet_holo")) != holo.id
    assert keys_of(bot, patron) == {"duels_5", "pet_holo"}


async def test_owner_is_told_once_per_role_that_cannot_be_made_not_at_every_deed(game_db, monkeypatch, clock):
    bot = game_db.bot
    scene = game_db.scene()
    patron = game_db.add_member("Patron", roles=[PATRON])
    asked = record_creations(game_db, monkeypatch)
    game_db.guild.create_role_error = forbidden()

    assert await achievements.add_count(scene, patron, "chests", 10) == 10
    for _ in range(15):
        clock.now += achievements.ROLE_RETRY_AFTER + 1     # every deed may ask the server again
        await achievements.add_count(scene, patron, "chests")
    assert asked == ["Łowca skarbów I"] * 16
    assert owner_lines(bot, "Nie udało się utworzyć ról osiągnięć") == [
        achievements.LOG_ROLES_FAILED.format(names="Łowca skarbów I")]

    clock.now += achievements.ROLE_RETRY_AFTER + 1
    assert await achievements.add_count(scene, patron, "duels", 5) == 5
    for _ in range(4):
        clock.now += achievements.ROLE_RETRY_AFTER + 1
        await achievements.add_count(scene, patron, "duels")
    assert asked.count("Pojedynkowicz I") == 5
    assert owner_lines(bot, "Nie udało się utworzyć ról osiągnięć") == [
        achievements.LOG_ROLES_FAILED.format(names="Łowca skarbów I"),
        achievements.LOG_ROLES_FAILED.format(names="Pojedynkowicz I")]
    assert pings(game_db) == 2 and len(bot.owner_log) == 2
    assert role_ids(patron) == set() and announcements(game_db) == 0
    assert await achievements.add_count(scene, patron, "chests") == 26     # still counted


# -- grant ------------------------------------------------------------------------------------

async def test_grant_is_for_supporters_only(game, old_roles):
    scene = game.scene()
    plain = game.add_member("Zwykły")
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(scene, plain, "legend") is False
    assert await achievements.grant(scene, plain, "pet_holo") is False
    assert role_ids(plain) == set() and announcements(game) == 0
    assert stored(game.bot) == {}                            # no role made for somebody who gets nothing

    assert await achievements.grant(scene, patron, "legend") is True
    assert role_ids(patron) == {achievements.LEGEND_ROLE}
    assert game.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Legendarny boss!"


async def test_grant_makes_its_own_missing_role_first_and_only_that_one(game):
    bot = game.bot
    patron = game.add_member("Patron", roles=[patrons.CRAFTER_ROLE])
    before = len(game.guild.roles)
    assert await achievements.grant(game.scene(), patron, "pet_holo") is True
    assert keys_of(bot, patron) == {"pet_holo"}
    assert game.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Folia Holo!"
    assert len(game.guild.roles) == before + 1               # not the other 22 (that is the start-up's job)
    assert stored(bot) == {"pet_holo": role_id_of(bot, trophy("pet_holo"))}
    assert owner_lines(bot, "Utworzyłem role osiągnięć") == [
        achievements.LOG_ROLES_CREATED.format(count=1, names="Folia Holo")]

    assert await achievements.grant(game.scene(), patron, "duels_5") is True
    assert keys_of(bot, patron) == {"pet_holo", "duels_5"}
    assert len(game.guild.roles) == before + 2 and set(stored(bot)) == {"pet_holo", "duels_5"}
    assert achievements.role_exists(bot, trophy("duels_25")) is False


async def test_grant_makes_a_role_deleted_on_the_server_again(game):
    bot = game.bot
    first = roles_of_an_earlier_run(game, without="duels_5")
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(game.scene(), patron, "duels_5") is True
    again = role_id_of(bot, trophy("duels_5"))
    assert again != first["duels_5"] and game.guild.get_role(again).name == "Pojedynkowicz I"
    assert role_ids(patron) == {again} and stored(bot)["duels_5"] == again


async def test_grant_twice_and_lower_tier_change_nothing(game, old_roles):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(scene, patron, "boss_kills_30") is True
    assert await achievements.grant(scene, patron, "boss_kills_30") is False     # already holds it
    for lower in ("boss_kills_1", "boss_kills_10"):
        assert await achievements.grant(scene, patron, lower) is False           # holds a higher tier
    assert role_ids(patron) == {KILL_30} and announcements(game) == 1
    assert stored(bot) == {}                                                     # nothing needed making

    assert await achievements.grant(scene, patron, "boss_kills_100") is True     # a bot-made higher tier
    assert keys_of(bot, patron) == {"boss_kills_100"}                            # the old tier taken away
    assert await achievements.grant(scene, patron, "boss_kills_30") is False
    assert keys_of(bot, patron) == {"boss_kills_100"}


async def test_grant_without_announcement_sends_nothing(game, old_roles):
    patron = game.add_member("Patron", roles=[PATRON])
    patron.roles.append(old_roles[KILL_1])
    assert await achievements.grant(game.scene(), patron, "boss_kills_10", announce=False) is True
    assert role_ids(patron) == {KILL_10}                     # the lower tier still goes away
    assert game_channel_messages(game) == 0


async def test_grant_to_somebody_who_is_not_here(game, old_roles):
    scene = game.scene()
    gone = FakeMember("Były Patron")                         # a stale object of somebody who left
    gone.roles.append(game.guild.add_role(PATRON))
    assert await achievements.grant(scene, gone, "legend") is False
    assert await achievements.grant(scene, 555, "legend") is False
    assert await achievements.grant(scene, None, "legend") is False
    assert role_ids(gone) == set()
    assert announcements(game) == 0

    patron = game.add_member("Ala", roles=[PATRON])
    assert await achievements.grant(scene, patron.id, "legend") is True          # a bare id of a member
    assert role_ids(patron) == {achievements.LEGEND_ROLE}


# -- the new counters ---------------------------------------------------------------------------

async def counter_value(db, player_id: int, name: str) -> int | None:
    value = await db.fetchval(
        "SELECT VALUE FROM ACHIEVEMENT_COUNTERS WHERE PLAYER_ID = $1 AND NAME = $2", player_id, name)
    return None if value is None else int(value)


async def test_new_counters_count_for_everybody(game_db):
    db = game_db.bot.db
    scene = game_db.scene()
    plain = game_db.add_member("Zwykły")
    for expected in range(1, 31):
        assert await achievements.add_count(scene, plain, "duels") == expected
    assert await achievements.add_count(scene, plain, "chests", 7) == 7
    assert await achievements.add_count(scene, plain.id, "rituals") == 1
    assert await counter_value(db, plain.id, "duels") == 30
    assert await counter_value(db, plain.id, "chests") == 7
    assert await counter_value(db, plain.id, "rituals") == 1
    assert await counter_value(db, plain.id, "tournaments") is None
    assert role_ids(plain) == set() and announcements(game_db) == 0


@pytest.mark.parametrize("counter", achievements.COUNTERS)
async def test_new_counter_tiers_for_a_supporter(game_db, counter):
    bot = game_db.bot
    scene = game_db.scene()
    patron = game_db.add_member("Patron", roles=[patrons.TWITCH_SUB_ROLE])
    tiers = [item for item in TROPHIES if item.chain == counter]
    total = 0
    for tier in tiers:
        missing = int(tier.threshold) - total - 1
        if missing:
            total = await achievements.add_count(scene, patron, counter, missing)
        assert tier.key not in keys_of(bot, patron)
        total = await achievements.add_count(scene, patron, counter)
        assert total == int(tier.threshold)
        assert keys_of(bot, patron) == {tier.key}            # the higher tier replaces the lower one
        assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: {tier.name}!"
    await achievements.add_count(scene, patron, counter)
    assert keys_of(bot, patron) == {tiers[-1].key}
    assert announcements(game_db) == len(tiers)
    assert await counter_value(bot.db, patron.id, counter) == total + 1


async def test_counter_passed_in_one_go_gives_the_highest_tier(game_db):
    bot = game_db.bot
    patron = game_db.add_member("Patron", roles=[PATRON])
    assert await achievements.add_count(game_db.scene(), patron, "chests", 60) == 60
    assert keys_of(bot, patron) == {"chests_50"}
    assert announcements(game_db) == 1


async def test_add_count_without_announcement_gives_the_role_quietly(game_db):
    """E.g. the ritual counts in the background: the role comes, the channel stays quiet."""
    bot = game_db.bot
    patron = game_db.add_member("Patron", roles=[PATRON])
    assert await achievements.add_count(game_db.scene(), patron, "rituals", 5, announce=False) == 5
    assert keys_of(bot, patron) == {"rituals_5"}
    assert await achievements.add_count(game_db.scene(), patron, "rituals", 15, announce=False) == 20
    assert keys_of(bot, patron) == {"rituals_20"}            # the lower tier still goes away
    assert game_channel_messages(game_db) == 0


async def test_grant_reached_falls_back_to_the_highest_tier_that_can_be_given(game_db, monkeypatch, clock):
    bot = game_db.bot
    scene = game_db.scene()
    refused = refuse_names(game_db, monkeypatch)
    refused.add("Kultysta II")
    assert len(await achievements.ensure_roles(bot)) == 22   # start-up: "Kultysta II" refused
    patron = game_db.add_member("Patron", roles=[PATRON])
    other = game_db.add_member("Ola", roles=[PATRON])

    assert await achievements.add_count(scene, patron, "rituals", 25) == 25
    assert keys_of(bot, patron) == {"rituals_5"}             # not left with nothing
    assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Kultysta I!"
    assert await achievements.grant_reached(scene, other, "rituals", 20) == trophy("rituals_5")
    assert await achievements.add_count(scene, patron, "rituals") == 26
    assert keys_of(bot, patron) == {"rituals_5"} and announcements(game_db) == 2
    assert len(owner_lines(bot, "Nie udało się utworzyć ról osiągnięć")) == 1

    refused.clear()                                          # the permission was given back
    clock.now += achievements.ROLE_RETRY_AFTER + 1
    assert await achievements.add_count(scene, patron, "rituals") == 27
    assert keys_of(bot, patron) == {"rituals_20"}            # the upgrade, rituals_5 taken away
    assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Kultysta II!"
    assert announcements(game_db) == 3


async def test_unknown_counter_or_nobody_is_not_counted(game_db):
    db = game_db.bot.db
    scene = game_db.scene()
    patron = game_db.add_member("Patron", roles=[PATRON])
    assert await achievements.add_count(scene, patron, "boss_kills") == 0
    assert await achievements.add_count(scene, patron, "secrets") == 0
    assert await achievements.add_count(scene, None, "duels") == 0
    assert await db.fetchval("SELECT count(*) FROM ACHIEVEMENT_COUNTERS") == 0
    assert role_ids(patron) == set()


async def test_add_count_never_raises_on_a_database_error(game):
    game.bot.db = DatabaseDown()
    patron = game.add_member("Patron", roles=[PATRON])
    for counter in achievements.COUNTERS:
        assert await achievements.add_count(game.scene(), patron, counter, 100) == 0
    assert role_ids(patron) == set() and announcements(game) == 0


async def test_add_count_with_a_broken_table_does_not_raise(game_db):
    await game_db.bot.db.execute("DROP TABLE ACHIEVEMENT_COUNTERS")
    patron = game_db.add_member("Patron", roles=[PATRON])
    assert await achievements.add_count(game_db.scene(), patron, "duels", 5) == 0
    assert role_ids(patron) == set()
    assert await game_db.bot.db.fetchval("SELECT 1") == 1    # the pool is still usable


# -- speed in a boss fight and the tower sprint ---------------------------------------------

@pytest.mark.parametrize("seconds, expected", [
    (5.0, None),
    (2.0, None),
    (1.99, "quick_1"),
    (1.0, "quick_1"),
    (0.99, "quick_2"),
    (0.75, "quick_2"),
    (0.74, "quick_3"),
    (0.1, "quick_3"),
    (None, "quick_1"),                                       # how the old bot called it: "under 2 s"
])
async def test_quickness_tiers_at_the_boundaries(game, old_roles, seconds, expected):
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.quickness(game.scene(), patron, seconds) is True
    assert keys_of(game.bot, patron) == ({expected} if expected else set())
    assert announcements(game) == (1 if expected else 0)


async def test_faster_tier_replaces_the_slower_and_a_slower_fight_takes_nothing(game, old_roles):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    await achievements.quickness(scene, patron, 1.5)
    assert role_ids(patron) == {QUICK}
    await achievements.quickness(scene, patron, 0.9)
    assert keys_of(bot, patron) == {"quick_2"}
    await achievements.quickness(scene, patron, 0.5)
    assert keys_of(bot, patron) == {"quick_3"}
    for slower in (1.5, 0.9, None):
        await achievements.quickness(scene, patron, slower)
    assert keys_of(bot, patron) == {"quick_3"} and announcements(game) == 3


async def test_quickness_of_a_player_who_does_not_support(game, old_roles):
    plain = game.add_member("Zwykły")
    assert await achievements.quickness(game.scene(), plain, 0.5) is True
    assert role_ids(plain) == set() and announcements(game) == 0


@pytest.mark.parametrize("level, seconds, expected", [
    (6, 19.99, True),
    (6, 20.0, False),
    (6, 0.0, True),
    (10, 5.0, True),
    (5, 1.0, False),
    (3, 19.0, False),
    (7, -1.0, False),                                        # a broken clock is not a sprint
])
async def test_tower_sprint_boundaries(game, level, seconds, expected):
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.tower_sprint(game.scene(), patron, level, seconds) is expected
    assert keys_of(game.bot, patron) == ({"tower_sprint"} if expected else set())


async def test_tower_sprint_only_for_supporters_and_once(game):
    scene = game.scene()
    plain = game.add_member("Zwykły")
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.tower_sprint(scene, plain, 8, 3.0) is False
    assert await achievements.tower_sprint(scene, patron, 8, 3.0) is True
    assert await achievements.tower_sprint(scene, patron, 9, 3.0) is False
    assert role_ids(plain) == set() and announcements(game) == 1


# -- pets ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("pet, key", [
    ({"mythic": True}, "pet_mythic"),
    ({"ultra": True}, "pet_ultra"),
    ({"skills": 9}, "pet_divine"),
    ({"skills": 10}, "pet_divine"),
    ({"seal": 3}, "pet_seal"),
    ({"holo": True}, "pet_holo"),
])
async def test_each_pet_achievement(game_db, pet, key):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await set_pets(db, player.id, await add_pet(db, 7001, **pet))
    assert await achievements._pet_keys(game_db.bot, player.id) == {key}


async def test_ordinary_pets_stand_for_nothing(game_db):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_pet(db, 7001, skills=8, seal=2, ultra=None)    # ULTRA_SHINY may be NULL in old rows
    await add_pet(db, 7002, seal=1)
    await set_pets(db, player.id, 7001, 7002)
    assert await achievements._pet_keys(game_db.bot, player.id) == set()
    assert await achievements._pet_keys(game_db.bot, 424242) == set()          # no pets at all
    await set_pets(db, 434343, 0)                                               # a row, but no pet
    assert await achievements._pet_keys(game_db.bot, 434343) == set()


async def test_pets_in_the_stable_count_and_a_full_stable(game_db):
    bot = game_db.bot
    db = bot.db
    player = game_db.add_member("Ala")
    await add_pet(db, 7001)
    await add_pet(db, 7002, mythic=True)
    await add_pet(db, 7003, holo=True, seal=3)
    await add_pet(db, 7004)
    await add_pet(db, 7005, ultra=True)
    await add_pet(db, 7099, skills=9)                        # somebody else's pet
    await set_pets(db, player.id, 7001, 0, 7002, 7003)       # active + 2 in the stable, 2 places free
    assert await achievements._pet_keys(bot, player.id) == {"pet_mythic", "pet_holo", "pet_seal"}

    other = game_db.add_member("Ola")
    await set_pets(db, other.id, 7001, 7002, 7003, 7004, 7005)
    assert await achievements._pet_keys(bot, other.id) == {
        "stable_full", "pet_mythic", "pet_holo", "pet_seal", "pet_ultra"}


async def test_check_pets_gives_supporters_what_their_pets_stand_for(game_db):
    bot = game_db.bot
    db = bot.db
    patron = game_db.add_member("Patron", roles=[PATRON])
    plain = game_db.add_member("Zwykły")
    await add_pet(db, 7001, mythic=True, skills=9)
    await add_pet(db, 7002, holo=True)
    for player in (patron, plain):
        await set_pets(db, player.id, 7001, 7002)

    assert await achievements.check_pets(game_db.scene(), plain) == []
    assert role_ids(plain) == set() and announcements(game_db) == 0

    given = await achievements.check_pets(game_db.scene(), patron.id)
    assert sorted(item.key for item in given) == ["pet_divine", "pet_holo", "pet_mythic"]
    assert keys_of(bot, patron) == {"pet_divine", "pet_holo", "pet_mythic"}
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 3
    assert await achievements.check_pets(game_db.scene(), patron) == []          # nothing new
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 3


async def test_check_pets_quietly_and_never_raising(game_db):
    db = game_db.bot.db
    patron = game_db.add_member("Patron", roles=[PATRON])
    await set_pets(db, patron.id, await add_pet(db, 7001, ultra=True))
    given = await achievements.check_pets(game_db.scene(), patron, announce=False)
    assert [item.key for item in given] == ["pet_ultra"] and game_channel_messages(game_db) == 0
    assert await achievements.check_pets(game_db.scene(), None) == []

    game_db.bot.db = DatabaseDown()
    other = game_db.add_member("Ola", roles=[PATRON])
    assert await achievements.check_pets(game_db.scene(), other) == []
    assert role_ids(other) == set()


# -- what the data says ----------------------------------------------------------------------------

async def test_deserved_reads_every_source(game_db):
    bot = game_db.bot
    db = bot.db
    player_id = 8001
    await set_kills(db, player_id, kills=120, hunts=36, butchers=3)
    await set_counter(db, player_id, "duels", 25)
    await set_counter(db, player_id, "chests", 9)
    await set_counter(db, player_id, "tournaments", 1)
    await set_counter(db, player_id, "secret_handshakes", 999)                  # not a counter we know
    await set_tower(db, player_id, 6, "0:00:19.500000")
    await set_pets(db, player_id, await add_pet(db, 7001, holo=True))
    assert await achievements.deserved(bot, player_id) == {
        "boss_kills_1", "boss_kills_10", "boss_kills_30", "boss_kills_100",
        "hunts_10", "hunts_35",
        "butchers_1", "butchers_3",
        "duels_5", "duels_25", "tournaments_1",
        "tower_3", "tower_6", "tower_sprint",
        "pet_holo",
    }
    assert await achievements.deserved(bot, 424242) == set()


async def test_deserved_with_null_counters(game_db):
    db = game_db.bot.db
    await db.execute("INSERT INTO ACHIEVEMENTS (PLAYER_ID, HUNTS) VALUES (8002, 10)")
    assert await achievements.deserved(game_db.bot, 8002) == {"hunts_10"}


@pytest.mark.parametrize("level, time, sprint", [
    (6, "0:00:19.999999", True),
    (10, "0:00:05", True),
    (6, "0:00:20.000000", False),
    (6, "0:01:10.000000", False),
    (6, "1:00:01.000000", False),
    (5, "0:00:03.000000", False),                            # fast, but a low floor
    (8, None, False),                                        # no time stored
    (8, "", False),
    (8, "szybko", False),                                    # an unreadable time
    (8, "0:xx:10", False),
])
async def test_deserved_tower_record_time(game_db, level, time, sprint):
    await set_tower(game_db.bot.db, 8003, level, time)
    keys = await achievements.deserved(game_db.bot, 8003)
    assert ("tower_sprint" in keys) is sprint
    tiers = {"tower_3"} | ({"tower_6"} if level >= 6 else set()) | ({"tower_10"} if level >= 10 else set())
    assert keys - {"tower_sprint"} == tiers                  # the floors count whatever the time


async def test_sync_gives_the_highest_tier_of_every_family_quietly(game_db, old_roles):
    bot = game_db.bot
    db = bot.db
    veteran = game_db.add_member("Weteran", roles=[PATRON])
    veteran.roles += [old_roles[KILL_1], old_roles[KILL_10], old_roles[HUNT_10], old_roles[TOWER_3]]
    await set_kills(db, veteran.id, kills=260, hunts=40, butchers=0)
    await set_counter(db, veteran.id, "rituals", 6)
    await set_tower(db, veteran.id, 10, "0:00:42.000000")
    await set_pets(db, veteran.id, await add_pet(db, 7001, seal=3))

    given = await achievements.sync(game_db.scene(), veteran)
    assert [item.key for item in given] == ["boss_kills_250", "hunts_35", "tower_10", "pet_seal", "rituals_5"]
    assert keys_of(bot, veteran) == {"boss_kills_250", "hunts_35", "tower_10", "pet_seal", "rituals_5"}
    assert game_channel_messages(game_db) == 0               # silent by default
    assert await achievements.sync(game_db.scene(), veteran) == []


async def test_sync_keeps_a_higher_tier_the_data_does_not_show(game_db, old_roles):
    """A role given by hand (or by a counter of the old bot) is never taken back."""
    bot = game_db.bot
    patron = game_db.add_member("Patron", roles=[PATRON])
    patron.roles.append(old_roles[KILL_30])
    await set_kills(game_db.bot.db, patron.id, kills=12)
    assert await achievements.sync(game_db.scene(), patron) == []
    assert role_ids(patron) == {KILL_30}


async def test_sync_can_announce(game_db, old_roles):
    patron = game_db.add_member("Patron", roles=[PATRON])
    await set_kills(game_db.bot.db, patron.id, kills=31)
    given = await achievements.sync(game_db.scene(), patron, announce=True)
    assert [item.key for item in given] == ["boss_kills_30"]
    assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Pogromca bossów III!"


async def test_sync_gives_nothing_to_somebody_who_does_not_support(game_db, old_roles):
    plain = game_db.add_member("Zwykły")
    await set_kills(game_db.bot.db, plain.id, kills=500, hunts=1000, butchers=25)
    assert await achievements.sync(game_db.scene(), plain) == []
    assert await achievements.sync(game_db.scene(), None) == []
    assert role_ids(plain) == set() and stored(game_db.bot) == {}


async def test_sync_falls_back_to_a_lower_tier_and_upgrades_later(game_db, monkeypatch, clock):
    bot = game_db.bot
    refused = refuse_names(game_db, monkeypatch)
    refused.add("Kultysta II")
    await achievements.ensure_roles(bot)
    veteran = game_db.add_member("Weteran", roles=[PATRON])
    await set_counter(bot.db, veteran.id, "rituals", 25)
    assert [item.key for item in await achievements.sync(game_db.scene(), veteran)] == ["rituals_5"]
    assert keys_of(bot, veteran) == {"rituals_5"}
    assert await achievements.sync(game_db.scene(), veteran) == []

    refused.clear()
    clock.now += achievements.ROLE_RETRY_AFTER + 1
    assert [item.key for item in await achievements.sync(game_db.scene(), veteran)] == ["rituals_20"]
    assert keys_of(bot, veteran) == {"rituals_20"}
    assert game_channel_messages(game_db) == 0


async def test_sync_takes_away_the_lower_of_two_tiers_held(game_db, old_roles):
    """The old bot gave the tower roles floor by floor, also to holders of a higher one."""
    bot = game_db.bot
    await achievements.ensure_roles(bot)
    doubled = game_db.add_member("Ala", roles=[PATRON])
    doubled.roles += [old_roles[TOWER_6], old_roles[TOWER_10]]
    tripled = game_db.add_member("Ela", roles=[PATRON])
    tripled.roles += [old_roles[KILL_1], old_roles[KILL_10], old_roles[KILL_30]]
    await set_kills(bot.db, tripled.id, kills=35)            # what the data says changes nothing here
    quick = game_db.add_member("Ola", roles=[PATRON])
    give(game_db, quick, "quick_1", "quick_3")               # a bot-made tier above an old one
    single = game_db.add_member("Ula", roles=[PATRON])
    single.roles += [old_roles[TOWER_6], old_roles[HUNT_35]]
    plain = game_db.add_member("Zwykły")                     # not a supporter: his roles are not touched
    plain.roles += [old_roles[TOWER_6], old_roles[TOWER_10]]

    for member in (doubled, tripled, quick, single, plain):
        assert await achievements.sync(game_db.scene(), member) == []
    assert role_ids(doubled) == {TOWER_10}
    assert role_ids(tripled) == {KILL_30}
    assert keys_of(bot, quick) == {"quick_3"}
    assert role_ids(single) == {TOWER_6, HUNT_35}            # one tier of a family: nothing removed
    assert role_ids(plain) == {TOWER_6, TOWER_10}
    assert game_channel_messages(game_db) == 0


# -- the hand-out to veterans ---------------------------------------------------------------------

async def test_sync_veterans_only_supporters_on_the_server_and_one_note_for_the_owner(game_db, old_roles):
    bot = game_db.bot
    db = bot.db
    ala = game_db.add_member("Ala", roles=[PATRON])
    ola = game_db.add_member("Ola", roles=[patrons.YOUTUBE_ROLES[0]])
    plain = game_db.add_member("Zwykły")
    await set_kills(db, ala.id, kills=12)
    await set_tower(db, ola.id, 6, "0:00:12.500000")
    await set_kills(db, plain.id, kills=50)
    await set_kills(db, 555, kills=50)                       # a supporter who left the server
    await set_counter(db, 556, "duels", 30)

    assert await achievements.sync_veterans(bot) == (2, 3)
    assert role_ids(ala) == {KILL_10}
    assert keys_of(bot, ola) == {"tower_6", "tower_sprint"}
    assert role_ids(plain) == set()
    assert game_channel_messages(game_db) == 0               # nothing on the game channels
    notes = [line for line in bot.owner_log if line.startswith("Zaległe osiągnięcia")]
    assert len(notes) == 1
    assert notes[0].startswith("Zaległe osiągnięcia: nadałem 3 ról 2 wspierającym.")
    assert f"<@{ala.id}>: Pogromca bossów II" in notes[0]
    assert f"<@{ola.id}>: Wieża Śmierci II, Sprinter Wieży" in notes[0]
    assert str(plain.id) not in notes[0] and "<@555>" not in notes[0]

    assert await achievements.sync_veterans(bot) == (0, 0)   # a second start: nothing new, no note
    assert len([line for line in bot.owner_log if line.startswith("Zaległe osiągnięcia")]) == 1


async def test_sync_veterans_without_anything_to_give_says_nothing(game_db):
    assert await achievements.sync_veterans(game_db.bot) == (0, 0)
    assert game_db.bot.owner_log == [] and game_channel_messages(game_db) == 0


async def test_sync_veterans_splits_a_long_summary_without_cutting_a_line(game_db, old_roles):
    bot = game_db.bot
    veterans = [game_db.add_member(f"Weteran {number}", roles=[PATRON]) for number in range(60)]
    for veteran in veterans:
        await set_kills(bot.db, veteran.id, kills=12, hunts=40, butchers=3)
    assert await achievements.sync_veterans(bot) == (60, 180)

    notes = list(bot.owner_log)
    assert len(notes) >= 3                                   # ~4600 characters in all
    assert notes[0].startswith("Zaległe osiągnięcia: nadałem 180 ról 60 wspierającym.\n")
    assert all(len(note) <= achievements.LOG_CHUNK < 1990 for note in notes)
    assert [message.content for message in game_db.log_channel.sent] == notes   # nothing cut on the way
    lines = [line for note in notes for line in note.split("\n")][1:]
    assert sorted(lines) == sorted(
        f"<@{veteran.id}>: Pogromca bossów II, Myśliwy II, Rzeźnik potworów II" for veteran in veterans)
    assert game_channel_messages(game_db) == 0


class _QuietClock:
    """What :mod:`achievements` sees as ``asyncio``: everything real except ``sleep``, which
    records the pause and only yields (no real waiting)."""

    def __init__(self, on_pause):
        self.pauses: list[float] = []
        self._on_pause = on_pause

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, result=None):
        self.pauses.append(delay)
        await self._on_pause(delay)
        await asyncio.sleep(0)
        return result


@pytest.mark.parametrize("activity", ["busy", "fight"])
async def test_sync_veterans_waits_while_something_plays_on_the_boss_channel(game_db, old_roles, monkeypatch,
                                                                             activity):
    bot = game_db.bot
    state = bot.state
    patron = game_db.add_member("Ala", roles=[PATRON])
    await set_kills(bot.db, patron.id, kills=12)
    monkeypatch.setattr(achievements, "QUIET_POLL", 15.0)
    hunt = state.hold_busy("hunt")
    if activity == "busy":
        hunt.__enter__()
    else:
        await state.set_boss_phase(BossPhase.FIGHTING)

    async def on_pause(delay):
        if delay != achievements.QUIET_POLL:
            return
        assert role_ids(patron) == set()                     # nothing handed out during the action
        if clock.pauses.count(achievements.QUIET_POLL) == 3:  # ... until it is over
            if activity == "busy":
                hunt.__exit__(None, None, None)
            else:
                await state.set_boss_phase(BossPhase.IDLE)

    clock = _QuietClock(on_pause)
    monkeypatch.setattr(achievements, "asyncio", clock)
    assert await achievements.sync_veterans(bot) == (1, 1)
    assert clock.pauses.count(achievements.QUIET_POLL) == 3
    assert role_ids(patron) == {KILL_10}
    assert game_channel_messages(game_db) == 0


async def test_one_players_failure_does_not_stop_the_others(game_db, old_roles, monkeypatch):
    bot = game_db.bot
    broken = game_db.add_member("Ala", roles=[PATRON])
    fine = game_db.add_member("Ola", roles=[PATRON])
    for player in (broken, fine):
        await set_kills(bot.db, player.id, kills=1)
    deserved = achievements.deserved

    async def flaky(bot_, player_id):
        if player_id == broken.id:
            raise RuntimeError("test: this one's data cannot be read")
        return await deserved(bot_, player_id)

    monkeypatch.setattr(achievements, "deserved", flaky)
    assert await achievements.sync_veterans(bot) == (1, 1)
    assert role_ids(fine) == {KILL_1} and role_ids(broken) == set()
    notes = [line for line in bot.owner_log if line.startswith("Zaległe osiągnięcia")]
    assert len(notes) == 1 and f"<@{fine.id}>" in notes[0] and str(broken.id) not in notes[0]


async def test_start_up_makes_the_roles_then_hands_out_and_never_raises(game_db, old_roles, monkeypatch):
    bot = game_db.bot
    patron = game_db.add_member("Ala", roles=[PATRON])
    await set_counter(bot.db, patron.id, "tournaments", 2)
    await achievements.start_up(bot)
    assert len(stored(bot)) == 23
    assert keys_of(bot, patron) == {"tournaments_1"}
    assert game_channel_messages(game_db) == 0

    async def broken(bot_):
        raise RuntimeError("test")

    monkeypatch.setattr(achievements, "sync_veterans", broken)
    await achievements.start_up(bot)                         # logged, not raised


# -- $osiagniecia ---------------------------------------------------------------------------------

async def test_held_counts_a_tier_together_with_the_tiers_below(game):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    assert achievements.held(bot, member) == ([], 0)

    give(game, member, "boss_kills_30")
    shown, count = achievements.held(bot, member)
    assert [item.key for item in shown] == ["boss_kills_30"] and count == 3

    give(game, member, "boss_kills_1", "unicorn", "quick_3", "duels_5")   # a stray lower tier too
    shown, count = achievements.held(bot, member)
    assert [item.key for item in shown] == ["boss_kills_30", "quick_3", "unicorn", "duels_5"]
    assert count == 3 + 3 + 1 + 1

    member.roles.append(game.guild.get_role(bot.ids.mythic_slayer_role))
    assert achievements.held(bot, member)[1] == 9


async def test_held_with_achievements_given_a_moment_ago(game):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    give(game, member, "boss_kills_10")
    shown, count = achievements.held(bot, member, [trophy("boss_kills_250"), trophy("legend")])
    assert [item.key for item in shown] == ["boss_kills_250", "legend"]
    assert count == 5 + 1


async def test_summary_shows_what_is_held_and_how_many_of_how_many(game, old_roles):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    give(game, member, "boss_kills_30", "quick_2", "unicorn")
    embed = achievements.summary_embed(bot, member)
    text = embed.description
    assert embed.title == achievements.MSG_SUMMARY_TITLE
    assert f"<@{member.id}>, masz **6 z 39** osiągnięć." in text
    for key in ("boss_kills_30", "quick_2", "unicorn"):
        assert f"<@&{role_id_of(bot, trophy(key))}>" in text
    assert "Nowe" not in text
    assert embed.footer.text == achievements.MSG_SUMMARY_FOOTER


async def test_summary_never_reveals_what_is_missing(game, old_roles):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    give(game, member, "hunts_35", "tower_sprint", "pet_holo")
    new = [trophy("pet_holo")]
    embed = achievements.summary_embed(bot, member, new)
    text = "\n".join([embed.title or "", embed.description or "", embed.footer.text or "",
                      *(f"{f.name} {f.value}" for f in embed.fields)])
    assert "**4 z 39**" in text
    assert f"🎉 **Nowe:** <@&{role_id_of(bot, trophy('pet_holo'))}>" in text
    counted = {"hunts_10", "hunts_35", "tower_sprint", "pet_holo"}
    lacking = [item for item in TROPHIES if item.key not in counted]
    assert len(lacking) == 35
    for item in lacking:
        assert item.name not in text, item.key
        assert str(role_id_of(bot, item)) not in text, item.key


async def test_summary_of_somebody_with_nothing_yet(game):
    member = game.add_member("Ala", roles=[PATRON])
    embed = achievements.summary_embed(game.bot, member)
    assert "**0 z 39**" in embed.description
    assert achievements.MSG_SUMMARY_NONE in embed.description
    assert "<@&" not in embed.description


async def test_show_achievements_is_for_supporters_only(game_db, old_roles):
    plain = game_db.add_member("Zwykły")
    await set_kills(game_db.bot.db, plain.id, kills=50, hunts=200)
    assert await achievements.show_achievements(game_db.scene(plain, "$osiagniecia")) is False
    answer = game_db.channel.bot_messages[-1]
    # Public, like every answer of the bot, so it says whom it answers.
    assert answer.content.startswith(f"<@{plain.id}> Osiągnięcia kolekcjonerskie są dla wspierających (")
    assert patrons.SUPPORTERS_LONG in answer.content
    assert answer.embeds == [] and " z 39" not in answer.text
    assert role_ids(plain) == set() and stored(game_db.bot) == {}

    assert await achievements.show_achievements(game_db.scene()) is False      # nobody asked
    assert game_db.channel.bot_messages[-1].content.startswith("Osiągnięcia kolekcjonerskie")


async def test_show_achievements_without_the_database_shows_the_roles_held(game, old_roles):
    bot = game.bot
    bot.db = DatabaseDown()
    patron = game.add_member("Ala", roles=[PATRON])
    patron.roles += [old_roles[KILL_10], old_roles[achievements.UNICORN_ROLE]]
    assert await achievements.show_achievements(game.scene(patron, "$osiagniecia")) is True
    answer = game.channel.bot_messages[-1]
    assert len(answer.embeds) == 1
    assert f"<@{patron.id}>, masz **3 z 39** osiągnięć." in answer.text
    assert f"<@&{KILL_10}>" in answer.text and f"<@&{achievements.UNICORN_ROLE}>" in answer.text
    assert "Nowe" not in answer.text
    assert role_ids(patron) == {KILL_10, achievements.UNICORN_ROLE}


async def test_show_achievements_hands_out_quietly_and_shows_the_summary(game_db, old_roles):
    bot = game_db.bot
    patron = game_db.add_member("Ala", roles=[PATRON])
    patron.roles += [old_roles[KILL_1], old_roles[achievements.UNICORN_ROLE]]
    await set_kills(bot.db, patron.id, kills=12)
    await set_pets(bot.db, patron.id, await add_pet(bot.db, 7001, holo=True))

    assert await achievements.show_achievements(game_db.scene(patron, "$osiagniecia")) is True
    assert keys_of(bot, patron) == {"boss_kills_10", "unicorn", "pet_holo"}
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 0    # no announcement, just the summary
    answer = game_db.channel.bot_messages[-1]
    assert len(answer.embeds) == 1                           # a public message on the channel
    text = answer.text
    assert f"<@{patron.id}>, masz **4 z 39** osiągnięć." in text
    new_line = next(line for line in text.splitlines() if line.startswith("🎉 **Nowe:**"))
    assert f"<@&{KILL_10}>" in new_line and f"<@&{role_id_of(bot, trophy('pet_holo'))}>" in new_line
    assert f"<@&{achievements.UNICORN_ROLE}>" not in new_line
    assert f"<@&{KILL_1}>" not in text

    assert await achievements.show_achievements(game_db.scene(patron, "$osiagniecia")) is True
    assert "Nowe" not in game_db.channel.bot_messages[-1].text      # nothing new the second time
    assert "**4 z 39**" in game_db.channel.bot_messages[-1].text


async def test_values_from_the_database_are_numbers_not_decimals(game_db):
    """NUMERIC columns come back as Decimal; the tiers must still be found."""
    db = game_db.bot.db
    await db.execute("INSERT INTO ACHIEVEMENT_COUNTERS (PLAYER_ID, NAME, VALUE) VALUES (8004, 'chests', $1)",
                     Decimal("10"))
    assert "chests_10" in await achievements.deserved(game_db.bot, 8004)
