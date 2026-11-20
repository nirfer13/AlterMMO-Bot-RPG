"""features/achievements.py: the catalogue of the four mythic achievements, the roles the bot makes,
what the pets stand for, the hand-out to veterans, the start-up and ``$osiagniecia``.

Owner's decisions of 2026-11-20 tested here: only the mythic achievements stay (a mythic boss, a mythic
pet, a mythic pet with Holo, a mythic pet with the rarest seal); only supporters get the roles; the
veteran hand-out is silent with one summary for the owner and waits while something plays on the boss
channel; ``$osiagniecia`` shows what the player has and "X z 4" - never which achievements are missing.
The deletion of the withdrawn achievements' roles is tested in test_achievements.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from pathlib import Path

import discord
import pytest

from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossPhase
from altermmo.features import achievements, patrons
from altermmo.features.achievements import TROPHIES, role_id_of, trophy
from altermmo.settings import Ids

from fakes import FakeMember

PATRON = patrons.PATRON_ROLES[0]
PET_KEYS = ("pet_mythic", "pet_mythic_holo", "pet_mythic_seal")
PET_NAMES = ("Mityczny towarzysz", "Mityczny Holo", "Mityczna Pieczęć Arcyłowcy")
BOT_MADE = [item for item in TROPHIES if item.bot_made]
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


def pings(game) -> int:
    """Messages on the owner's log channel that ping the owner."""
    return game.log_channel.sent_count(f"<@{game.bot.ids.owner}>")


def owner_lines(bot, start: str) -> list[str]:
    return [line for line in bot.owner_log if line.startswith(start)]


@pytest.fixture
def slayer(game):
    """The role of the mythic boss (the game's own role, named as on the server)."""
    role = game.guild.get_role(game.bot.ids.mythic_slayer_role)
    role.name = "Mityczny boss"
    return role


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


async def set_kills(db, player_id: int, kills: int = 0, hunts: int = 0, butchers: int = 0) -> None:
    await db.execute("INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS, HUNTS, BUTCHERS) VALUES ($1, $2, $3, $4)",
                     player_id, kills, hunts, butchers)


# -- the catalogue, pure ----------------------------------------------------------------------

def test_catalogue_has_exactly_the_four_mythic_achievements():
    assert [item.key for item in TROPHIES] == ["mythic_boss", *PET_KEYS]
    assert [item.name for item in TROPHIES] == ["Mityczny boss", *PET_NAMES]
    assert len({item.key for item in TROPHIES}) == len({item.name for item in TROPHIES}) == 4
    assert all(trophy(item.key) is item for item in TROPHIES)
    assert [field.name for field in dataclasses.fields(achievements.Trophy)] == ["key", "name", "ids_attr"]
    with pytest.raises(KeyError):
        trophy("legend")                                     # withdrawn


def test_the_mythic_boss_uses_the_games_role_and_the_bot_makes_the_three_pet_roles():
    assert [item.key for item in BOT_MADE] == list(PET_KEYS)
    assert all(not item.ids_attr for item in BOT_MADE)
    [boss] = [item for item in TROPHIES if not item.bot_made]
    assert (boss.key, boss.ids_attr) == ("mythic_boss", "mythic_slayer_role")
    assert getattr(Ids(), boss.ids_attr) == 1118242273772175401


def test_the_withdrawn_roles_are_the_old_bots_fifteen():
    retired = achievements.RETIRED_ROLES
    assert set(retired) == {
        1125799224966131712, 1125799392989949973, 1125799470509084762,    # boss kills
        1125799891923374121, 1125800644826112043, 1125800721724489758,    # hunts
        1125801055758860329, 1125801161631486033, 1125801216375529563,    # won seasons
        1151427369886826496, 1151427619741511710, 1151427790973964328,    # the tower
        1125819170534211664, 1125818827809226862, 1125818642450370590,    # legendary monster / boss, speed
    }
    assert len(retired) == 15 and len(set(retired.values())) == 15
    assert Ids().mythic_slayer_role not in retired
    assert 983798433590673448 not in retired                  # the season winner's role is no achievement
    assert not set(retired) & set(patrons.SUPPORTER_ROLES)


def test_no_code_uses_a_withdrawn_part_of_the_achievements():
    """The call sites went with the achievements: a left-over call would fail in the middle of a fight."""
    withdrawn = ("unicorn", "legend", "quickness", "tower_sprint", "add_count", "grant_reached",
                 "lower_tier_roles", "chain_of", "_tidy_tiers", "BOSS_KILL_ROLES", "HUNT_ROLES", "BUTCHER_ROLES",
                 "TOWER_ROLES", "UNICORN_ROLE", "LEGEND_ROLE", "QUICKNESS_ROLE", "COUNTERS", "NEW_TABLES",
                 "QUICK_LIMITS")
    assert not [name for name in withdrawn if hasattr(achievements, name)]
    pattern = re.compile(r"\bachievements\.(" + "|".join(withdrawn) + r")\b")
    package = Path(achievements.__file__).resolve().parents[1]
    uses = [f"{path.relative_to(package)}: {match.group(0)}"
            for path in package.rglob("*.py") for match in pattern.finditer(path.read_text(encoding="utf-8"))]
    assert uses == []


def test_role_of_an_achievement_without_the_bot():
    assert all(role_id_of(None, item) is None for item in TROPHIES)


# -- the roles the bot makes --------------------------------------------------------------------

async def test_role_of_an_achievement_with_the_bot(game):
    bot = game.bot
    assert role_id_of(bot, trophy("mythic_boss")) == bot.ids.mythic_slayer_role
    assert achievements.role_exists(bot, trophy("mythic_boss"))
    assert all(role_id_of(bot, item) is None for item in BOT_MADE)   # not made yet
    await achievements.ensure_roles(bot)
    made = {item.key: role_id_of(bot, item) for item in BOT_MADE}
    assert all(made.values()) and len(set(made.values())) == 3
    assert bot.ids.mythic_slayer_role not in made.values()
    assert all(game.guild.get_role(role_id).name == trophy(key).name for key, role_id in made.items())


async def test_ensure_roles_makes_the_missing_roles_without_permissions(game):
    bot = game.bot
    before = len(game.guild.roles)
    made = await achievements.ensure_roles(bot)
    assert made == list(PET_NAMES)
    assert len(game.guild.roles) == before + 3               # the mythic boss's role is not made
    file = stored(bot)
    assert set(file) == set(PET_KEYS)
    for item in BOT_MADE:
        role = game.guild.get_role(file[item.key])
        assert role is not None and role.name == item.name
        asked = role.created_with
        assert asked["permissions"] == discord.Permissions.none()
        assert asked["hoist"] is False and asked["mentionable"] is False
        assert asked["colour"] == discord.Colour(achievements.ROLE_COLOUR)
        assert asked["reason"] == achievements.ROLE_REASON
    assert bot._achievement_roles == file                    # what the bot remembers = the file
    assert bot.owner_log == [achievements.LOG_ROLES_CREATED.format(count=3, names=", ".join(PET_NAMES))]
    assert game_channel_messages(game) == 0

    assert await achievements.ensure_roles(bot) == []        # a second call makes nothing
    assert len(game.guild.roles) == before + 3
    assert stored(bot) == file and len(bot.owner_log) == 1


async def test_each_role_is_saved_as_soon_as_it_is_made(game, monkeypatch):
    """A crash between two roles must not orphan the first one (it would be made twice)."""
    bot = game.bot
    create = game.guild.create_role
    seen: list[dict] = []

    async def watching(*, name, **kwargs):
        seen.append(stored(bot))
        return await create(name=name, **kwargs)

    monkeypatch.setattr(game.guild, "create_role", watching)
    await achievements.ensure_roles(bot)
    assert [set(file) for file in seen] == [set(), {"pet_mythic"}, {"pet_mythic", "pet_mythic_holo"}]


def roles_of_an_earlier_run(game, *, without: str = "") -> dict[str, int]:
    """The 3 roles the bot made in an earlier run, on the server, with their ids saved in the file;
    the role of ``without`` was deleted on the server since (its saved id points nowhere)."""
    saved = {item.key: game.guild.add_role(990_000_000_000_000_100 + index, item.name).id
             for index, item in enumerate(BOT_MADE) if item.key != without}
    if without:
        saved[without] = 990_000_000_000_000_999
    game.bot.store.achievement_roles.save(saved)
    return saved


async def test_role_deleted_on_the_server_is_made_again(game):
    bot = game.bot
    first = roles_of_an_earlier_run(game, without="pet_mythic_holo")
    before = len(game.guild.roles)
    assert achievements.role_exists(bot, trophy("pet_mythic_holo")) is False
    assert await achievements.ensure_roles(bot) == ["Mityczny Holo"]
    again = stored(bot)
    assert again["pet_mythic_holo"] != first["pet_mythic_holo"]
    assert game.guild.get_role(again["pet_mythic_holo"]).name == "Mityczny Holo"
    assert {key: value for key, value in again.items() if key != "pet_mythic_holo"} == {
        key: value for key, value in first.items() if key != "pet_mythic_holo"}
    assert achievements.role_exists(bot, trophy("pet_mythic_holo")) is True
    assert len(game.guild.roles) == before + 1
    assert owner_lines(bot, "Utworzyłem role osiągnięć") == [
        achievements.LOG_ROLES_CREATED.format(count=1, names="Mityczny Holo")]


async def test_role_made_in_this_run_and_deleted_later_is_made_again(game, clock):
    bot = game.bot
    await achievements.ensure_roles(bot)
    first = role_id_of(bot, trophy("pet_mythic"))
    assert achievements.role_exists(bot, trophy("pet_mythic"))           # Discord's cache knows it by now
    clock.now += achievements.ROLE_RETRY_AFTER
    game.guild.roles.remove(game.guild.get_role(first))                    # the owner deletes it by mistake
    assert achievements.role_exists(bot, trophy("pet_mythic")) is False  # $roleosiagniec: "missing"
    assert await achievements.ensure_roles(bot) == ["Mityczny towarzysz"]
    assert role_id_of(bot, trophy("pet_mythic")) != first
    assert game.guild.get_role(role_id_of(bot, trophy("pet_mythic"))).name == "Mityczny towarzysz"


async def test_failure_to_make_roles_is_reported_to_the_owner_and_not_raised(game):
    bot = game.bot
    game.guild.create_role_error = forbidden()
    assert await achievements.ensure_roles(bot) == []
    assert stored(bot) == {}
    assert owner_lines(bot, "Nie udało się utworzyć ról osiągnięć") == [
        achievements.LOG_ROLES_FAILED.format(names=", ".join(PET_NAMES))]
    assert pings(game) == 1                                  # the owner is pinged
    assert game_channel_messages(game) == 0

    game.guild.create_role_error = None                      # the permission was given back
    assert await achievements.ensure_roles(bot) == list(PET_NAMES)


async def test_one_role_that_cannot_be_made_does_not_stop_the_others(game, monkeypatch):
    bot = game.bot
    create = game.guild.create_role

    async def picky(*, name, **kwargs):
        if name == "Mityczny Holo":
            raise RuntimeError("Maximum number of guild roles reached (250)")
        return await create(name=name, **kwargs)

    monkeypatch.setattr(game.guild, "create_role", picky)
    assert await achievements.ensure_roles(bot) == ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]
    assert set(stored(bot)) == {"pet_mythic", "pet_mythic_seal"}
    assert owner_lines(bot, "Nie udało się utworzyć ról osiągnięć") == [
        achievements.LOG_ROLES_FAILED.format(names="Mityczny Holo")]
    monkeypatch.setattr(game.guild, "create_role", create)
    assert await achievements.ensure_roles(bot) == ["Mityczny Holo"]


async def test_roles_file_from_an_earlier_run_is_used_and_read_once(game):
    bot = game.bot
    kept = game.guild.add_role(990_000_000_000_000_100, "Mityczny Holo (renamed by the owner)")
    bot.store.achievement_roles.save({"pet_mythic_holo": kept.id, "pet_mythic": 0, "not_an_achievement": "x"})
    made = await achievements.ensure_roles(bot)
    assert made == ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]     # the owner's renamed role is kept
    assert role_id_of(bot, trophy("pet_mythic_holo")) == kept.id
    assert stored(bot)["pet_mythic_holo"] == kept.id and stored(bot) == bot._achievement_roles
    # The file is read once; what the bot made since is remembered, not read back.
    bot.store.achievement_roles.save({})
    assert role_id_of(bot, trophy("pet_mythic_holo")) == kept.id
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


@pytest.mark.parametrize("broken", ["[1, 2, ", '{"pet_mythic_holo": 99', "[1, 2]", "directory"])
async def test_unreadable_roles_file_means_no_role_is_made(game, broken):
    """Making the roles again would duplicate them and lose the ids of the roles players hold."""
    bot = game.bot
    path = bot.store.achievement_roles.path
    break_file(path, broken)
    damaged = file_state(path)
    patron = game.add_member("Patron", roles=[PATRON])
    before = len(game.guild.roles)
    assert achievements.roles_file_readable(bot) is False
    assert await achievements.ensure_roles(bot) == []        # at start-up
    assert await achievements.ensure_roles(bot) == []        # $roleosiagniec
    for key in ("pet_mythic_holo", "pet_mythic", "pet_mythic_holo"):
        assert await achievements.grant(game.scene(), patron, key) is False
    assert len(game.guild.roles) == before
    assert file_state(path) == damaged                       # the file is never overwritten
    assert bot.owner_log == [achievements.LOG_ROLES_FILE_UNREADABLE.format(path=path)]
    assert pings(game) == 1                                  # once, with a ping
    assert role_ids(patron) == set() and game_channel_messages(game) == 0

    # The owner mends the file: the roles in it are used, only the missing ones are made.
    if path.is_dir():
        path.rmdir()
    holo = game.guild.add_role(990_000_000_000_000_100, "Mityczny Holo (renamed by the owner)")
    bot.store.achievement_roles.save({"pet_mythic_holo": holo.id})
    assert achievements.roles_file_readable(bot) is True
    assert await achievements.grant(game.scene(), patron, "pet_mythic_holo") is True
    assert role_ids(patron) == {holo.id}
    assert await achievements.ensure_roles(bot) == ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]
    assert stored(bot)["pet_mythic_holo"] == holo.id and len(stored(bot)) == 3


async def test_the_mythic_boss_is_given_while_the_roles_file_is_unreadable(game, slayer):
    """Its role is the game's own: the file does not matter for it."""
    break_file(game.bot.store.achievement_roles.path, "[1, 2, ")
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(game.scene(), patron, "mythic_boss") is True
    assert role_ids(patron) == {slayer.id}


@pytest.mark.parametrize("content", [None, "", "  \n"])
async def test_missing_or_empty_roles_file_means_the_roles_are_made(game, content):
    bot = game.bot
    path = bot.store.achievement_roles.path
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    assert achievements.roles_file_readable(bot) is True
    assert await achievements.ensure_roles(bot) == list(PET_NAMES)
    assert set(stored(bot)) == set(PET_KEYS)
    assert owner_lines(bot, "Nie mogę odczytać") == []


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
    assert await achievements.grant(game.scene(), patron, "pet_mythic") is False
    assert len(game.guild.roles) == before and stored(bot) == {} and bot.owner_log == []
    monkeypatch.setattr(game.guild, "unavailable", False)
    # An outage is not a refusal: the next deed makes the role at once (no back-off).
    assert await achievements.grant(game.scene(), patron, "pet_mythic") is True
    assert keys_of(bot, patron) == {"pet_mythic"}


async def test_a_role_of_the_same_name_without_permissions_is_taken_over(game):
    """The saved ids were lost but the roles are there: no duplicates.  Other roles of that name stay alone."""
    bot = game.bot
    ours = game.guild.add_role(990_000_000_000_000_100, "Mityczny Holo")
    mapped = game.guild.add_role(990_000_000_000_000_101, "Mityczna Pieczęć Arcyłowcy")  # renamed, saved
    powerful = game.guild.add_role(990_000_000_000_000_102, "Mityczna Pieczęć Arcyłowcy")
    powerful.permissions = discord.Permissions(manage_messages=True)
    managed = game.guild.add_role(990_000_000_000_000_103, "Mityczna Pieczęć Arcyłowcy")  # an integration's
    managed.managed = True
    bot.store.achievement_roles.save({"pet_mythic": mapped.id})
    patron = game.add_member("Patron", roles=[PATRON])
    before = len(game.guild.roles)

    assert await achievements.ensure_roles(bot) == ["Mityczna Pieczęć Arcyłowcy"]
    assert len(game.guild.roles) == before + 1
    file = stored(bot)
    assert file["pet_mythic_holo"] == ours.id and file["pet_mythic"] == mapped.id
    assert file["pet_mythic_seal"] not in (mapped.id, powerful.id, managed.id)
    assert owner_lines(bot, "Role osiągnięć, które już były na serwerze") == [
        achievements.LOG_ROLES_ADOPTED.format(count=1, names="Mityczny Holo")]
    assert owner_lines(bot, "Utworzyłem role osiągnięć") == [
        achievements.LOG_ROLES_CREATED.format(count=1, names="Mityczna Pieczęć Arcyłowcy")]

    assert await achievements.grant(game.scene(), patron, "pet_mythic_holo") is True
    assert role_ids(patron) == {ours.id}
    assert await achievements.ensure_roles(bot) == [] and len(game.guild.roles) == before + 1


async def test_a_role_just_made_is_used_before_discords_cache_knows_it(game, monkeypatch):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    asked = record_creations(game, monkeypatch)
    known = {role.id for role in game.guild.roles}
    lookup = game.guild.get_role
    # Discord's cache learns about a new role a moment after it is made: here, never.
    monkeypatch.setattr(game.guild, "get_role", lambda role_id: lookup(role_id) if int(role_id) in known else None)

    assert await achievements.grant(scene, patron, "pet_mythic_holo") is True
    holo = role_id_of(bot, trophy("pet_mythic_holo"))
    assert game.guild.get_role(holo) is None and achievements.role_exists(bot, trophy("pet_mythic_holo"))
    assert role_ids(patron) == {holo}
    assert game.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Mityczny Holo!"

    assert await achievements.ensure_roles(bot) == ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]
    assert await achievements.ensure_roles(bot) == []
    assert sorted(asked) == sorted(PET_NAMES)                # every role made exactly once
    assert await achievements.grant(scene, patron, "pet_mythic") is True      # also unseen by the cache
    assert keys_of(bot, patron) == {"pet_mythic_holo", "pet_mythic"}
    assert announcements(game) == 2


# -- after a refusal: the back-off and what the owner is told -------------------------------------------

async def test_after_a_refusal_grant_does_not_ask_again_until_the_back_off_is_over(game, monkeypatch, clock):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    asked = record_creations(game, monkeypatch)
    game.guild.create_role_error = forbidden()

    assert await achievements.grant(scene, patron, "pet_mythic_holo") is False
    assert asked == ["Mityczny Holo"]                        # only its own role, not the other two
    for key in ("pet_mythic_holo", "pet_mythic", "pet_mythic_seal"):   # the next deeds do not ask the server
        assert await achievements.grant(scene, patron, key) is False
    clock.now += achievements.ROLE_RETRY_AFTER - 1
    assert await achievements.grant(scene, patron, "pet_mythic_holo") is False
    assert asked == ["Mityczny Holo"]
    clock.now += 2                                           # the hour is over: asked again
    assert await achievements.grant(scene, patron, "pet_mythic_holo") is False
    assert asked == ["Mityczny Holo"] * 2

    game.guild.create_role_error = None                      # the permission was given back ...
    assert await achievements.grant(scene, patron, "pet_mythic_holo") is False   # ... a new hour runs
    assert asked == ["Mityczny Holo"] * 2
    clock.now += achievements.ROLE_RETRY_AFTER + 1
    assert await achievements.grant(scene, patron, "pet_mythic_holo") is True
    assert asked == ["Mityczny Holo"] * 3
    assert keys_of(bot, patron) == {"pet_mythic_holo"}
    assert stored(bot) == {"pet_mythic_holo": role_id_of(bot, trophy("pet_mythic_holo"))}
    assert len(owner_lines(bot, "Nie udało się utworzyć ról osiągnięć")) == 1


async def test_a_forced_ensure_roles_ignores_the_back_off_and_its_success_clears_it(game, clock):
    bot = game.bot
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    holo = game.guild.add_role(990_000_000_000_000_100, "Mityczny Holo (from an earlier run)")
    bot.store.achievement_roles.save({"pet_mythic_holo": holo.id})
    game.guild.create_role_error = forbidden()
    assert await achievements.grant(scene, patron, "pet_mythic") is False    # refused: an hour of quiet
    game.guild.create_role_error = None
    clock.now += 60
    assert await achievements.grant(scene, patron, "pet_mythic") is False    # still within the hour

    assert len(await achievements.ensure_roles(bot)) == 2                    # $roleosiagniec does not wait
    assert await achievements.grant(scene, patron, "pet_mythic") is True
    game.guild.roles.remove(holo)                                            # deleted on the server
    assert await achievements.grant(scene, patron, "pet_mythic_holo") is True   # made again at once
    assert role_id_of(bot, trophy("pet_mythic_holo")) != holo.id
    assert keys_of(bot, patron) == {"pet_mythic", "pet_mythic_holo"}


async def test_owner_is_told_once_per_role_that_cannot_be_made_not_at_every_deed(game, monkeypatch, clock):
    bot = game.bot
    scene = game.scene()
    patrons_ = [game.add_member(f"Patron {number}", roles=[PATRON]) for number in range(3)]
    asked = record_creations(game, monkeypatch)
    game.guild.create_role_error = forbidden()

    for _ in range(5):
        for patron in patrons_:
            clock.now += achievements.ROLE_RETRY_AFTER + 1   # every deed may ask the server again
            assert await achievements.grant(scene, patron, "pet_mythic_seal") is False
    assert asked == ["Mityczna Pieczęć Arcyłowcy"] * 15
    assert owner_lines(bot, "Nie udało się utworzyć ról osiągnięć") == [
        achievements.LOG_ROLES_FAILED.format(names="Mityczna Pieczęć Arcyłowcy")]

    for _ in range(3):
        clock.now += achievements.ROLE_RETRY_AFTER + 1
        assert await achievements.grant(scene, patrons_[0], "pet_mythic") is False
    assert owner_lines(bot, "Nie udało się utworzyć ról osiągnięć") == [
        achievements.LOG_ROLES_FAILED.format(names="Mityczna Pieczęć Arcyłowcy"),
        achievements.LOG_ROLES_FAILED.format(names="Mityczny towarzysz")]
    assert pings(game) == 2 and len(bot.owner_log) == 2
    assert all(role_ids(patron) == set() for patron in patrons_) and announcements(game) == 0


# -- grant ------------------------------------------------------------------------------------------

async def test_grant_is_for_supporters_only(game, slayer):
    scene = game.scene()
    plain = game.add_member("Zwykły")
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(scene, plain, "mythic_boss") is False
    assert await achievements.grant(scene, plain, "pet_mythic_holo") is False
    assert role_ids(plain) == set() and announcements(game) == 0
    assert stored(game.bot) == {}                            # no role made for somebody who gets nothing

    assert await achievements.grant(scene, patron, "mythic_boss") is True
    assert role_ids(patron) == {slayer.id}
    assert game.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Mityczny boss!"


@pytest.mark.parametrize("role_id", [patrons.CRAFTER_ROLE, patrons.TWITCH_SUB_ROLE, *patrons.YOUTUBE_ROLES])
async def test_every_kind_of_supporter_collects(game, role_id):
    supporter = game.add_member("Widz", roles=[role_id])
    assert await achievements.grant(game.scene(), supporter, "pet_mythic") is True
    assert keys_of(game.bot, supporter) == {"pet_mythic"}


async def test_grant_makes_its_own_missing_role_first_and_only_that_one(game):
    bot = game.bot
    patron = game.add_member("Patron", roles=[patrons.CRAFTER_ROLE])
    before = len(game.guild.roles)
    assert await achievements.grant(game.scene(), patron, "pet_mythic_seal") is True
    assert keys_of(bot, patron) == {"pet_mythic_seal"}
    assert game.channel.messages[-1].content == (
        f"<@{patron.id}> zdobywa osiągnięcie: Mityczna Pieczęć Arcyłowcy!")
    assert len(game.guild.roles) == before + 1               # not the other two (that is the start-up's job)
    assert stored(bot) == {"pet_mythic_seal": role_id_of(bot, trophy("pet_mythic_seal"))}
    assert bot.owner_log == [achievements.LOG_ROLES_CREATED.format(count=1, names="Mityczna Pieczęć Arcyłowcy")]
    assert achievements.role_exists(bot, trophy("pet_mythic")) is False


async def test_grant_makes_a_role_deleted_on_the_server_again(game):
    bot = game.bot
    first = roles_of_an_earlier_run(game, without="pet_mythic")
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(game.scene(), patron, "pet_mythic") is True
    again = role_id_of(bot, trophy("pet_mythic"))
    assert again != first["pet_mythic"] and game.guild.get_role(again).name == "Mityczny towarzysz"
    assert role_ids(patron) == {again} and stored(bot)["pet_mythic"] == again


async def test_grant_twice_changes_nothing(game, slayer):
    scene = game.scene()
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(scene, patron, "pet_mythic") is True
    assert await achievements.grant(scene, patron, "pet_mythic") is False     # already holds it
    patron.roles.append(slayer)                                              # given at the kill
    assert await achievements.grant(scene, patron, "mythic_boss") is False
    assert keys_of(game.bot, patron) == {"pet_mythic", "mythic_boss"} and announcements(game) == 1


async def test_grant_without_announcement_sends_nothing(game):
    patron = game.add_member("Patron", roles=[PATRON])
    assert await achievements.grant(game.scene(), patron, "pet_mythic_holo", announce=False) is True
    assert keys_of(game.bot, patron) == {"pet_mythic_holo"}
    assert game_channel_messages(game) == 0


async def test_grant_to_somebody_who_is_not_here(game, slayer):
    scene = game.scene()
    gone = FakeMember("Były Patron")                         # a stale object of somebody who left
    gone.roles.append(game.guild.add_role(PATRON))
    assert await achievements.grant(scene, gone, "mythic_boss") is False
    assert await achievements.grant(scene, 555, "mythic_boss") is False
    assert await achievements.grant(scene, None, "mythic_boss") is False
    assert role_ids(gone) == set() and announcements(game) == 0

    patron = game.add_member("Ala", roles=[PATRON])
    assert await achievements.grant(scene, patron.id, "mythic_boss") is True     # a bare id of a member
    assert role_ids(patron) == {slayer.id}


# -- the counters give no role -----------------------------------------------------------------------

@pytest.mark.parametrize("add, start", [
    (achievements.add_boss_kill, (0, 0, 0)), (achievements.add_boss_kill, (29, 0, 0)),
    (achievements.add_hunts, (0, 9, 0)), (achievements.add_hunts, (0, 99, 0)),
    (achievements.add_butcher, (0, 0, 0)), (achievements.add_butcher, (0, 0, 9)),
])
async def test_counted_deeds_give_no_role_and_make_none(game_db, slayer, add, start):
    """The old milestones (1 / 10 / 30 kills, 10 / 35 / 100 hunts, 1 / 3 / 10 seasons) are statistics now."""
    patron = game_db.add_member("Patron", roles=[PATRON])
    await set_kills(game_db.bot.db, patron.id, *start)
    before = len(game_db.guild.roles)
    assert await add(game_db.scene(), patron) is True
    assert role_ids(patron) == set() and len(game_db.guild.roles) == before
    assert stored(game_db.bot) == {} and announcements(game_db) == 0 and game_db.bot.owner_log == []


# -- pets ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pet, keys", [
    ({"mythic": True}, {"pet_mythic"}),
    ({"mythic": True, "holo": True}, {"pet_mythic", "pet_mythic_holo"}),
    ({"mythic": True, "seal": 3}, {"pet_mythic", "pet_mythic_seal"}),
    ({"mythic": True, "seal": 2}, {"pet_mythic"}),            # not the rarest seal
    ({"mythic": True, "seal": 1, "holo": True}, {"pet_mythic", "pet_mythic_holo"}),
    ({"mythic": True, "seal": 3, "holo": True}, set(PET_KEYS)),
    ({"holo": True}, set()),                                  # Holo / the rarest seal alone: not mythic
    ({"seal": 3}, set()),
    ({"seal": 3, "holo": True, "ultra": True, "skills": 10}, set()),
    ({"ultra": None, "skills": 9}, set()),                    # ULTRA_SHINY may be NULL in old rows
])
async def test_what_a_pet_stands_for(game_db, pet, keys):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await set_pets(db, player.id, await add_pet(db, 7001, **pet))
    assert await achievements._pet_keys(game_db.bot, player.id) == keys


async def test_pets_in_the_stable_count(game_db):
    bot = game_db.bot
    db = bot.db
    await add_pet(db, 7001)
    await add_pet(db, 7002, mythic=True, holo=True)
    await add_pet(db, 7003, mythic=True, seal=3)
    await add_pet(db, 7004, holo=True, seal=3)
    await add_pet(db, 7099, mythic=True, holo=True, seal=3)   # somebody else's pet
    player = game_db.add_member("Ala")
    await set_pets(db, player.id, 7001, 0, 7004, 7002)        # active + 2 in the stable
    assert await achievements._pet_keys(bot, player.id) == {"pet_mythic", "pet_mythic_holo"}
    other = game_db.add_member("Ola")
    await set_pets(db, other.id, 0, 0, 0, 0, 7003)            # only in the last place of the stable
    assert await achievements._pet_keys(bot, other.id) == {"pet_mythic", "pet_mythic_seal"}


async def test_no_pets_stand_for_nothing(game_db):
    bot = game_db.bot
    assert await achievements._pet_keys(bot, 424242) == set()                  # no row at all
    await set_pets(bot.db, 434343, 0)                                           # a row, but no pet
    assert await achievements._pet_keys(bot, 434343) == set()
    await set_pets(bot.db, 444444, 7777)                                        # a pet that is not there
    assert await achievements._pet_keys(bot, 444444) == set()


async def test_check_pets_gives_supporters_what_their_pets_stand_for(game_db):
    bot = game_db.bot
    db = bot.db
    patron = game_db.add_member("Patron", roles=[PATRON])
    plain = game_db.add_member("Zwykły")
    await add_pet(db, 7001, mythic=True, seal=3)
    await add_pet(db, 7002, mythic=True, holo=True)
    for player in (patron, plain):
        await set_pets(db, player.id, 7001, 7002)

    assert await achievements.check_pets(game_db.scene(), plain) == []
    assert role_ids(plain) == set() and announcements(game_db) == 0
    assert stored(bot) == {}                                                    # nothing made for him

    given = await achievements.check_pets(game_db.scene(), patron.id)
    assert [item.key for item in given] == list(PET_KEYS)                       # in the catalogue's order
    assert keys_of(bot, patron) == set(PET_KEYS)
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 3
    assert await achievements.check_pets(game_db.scene(), patron) == []          # nothing new
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 3


async def test_check_pets_gives_nothing_for_holo_or_the_rarest_seal_of_an_ordinary_pet(game_db):
    bot = game_db.bot
    patron = game_db.add_member("Patron", roles=[PATRON])
    await set_pets(bot.db, patron.id, await add_pet(bot.db, 7001, holo=True, seal=3, ultra=True))
    before = len(game_db.guild.roles)
    assert await achievements.check_pets(game_db.scene(), patron) == []
    assert role_ids(patron) == set() and len(game_db.guild.roles) == before and stored(bot) == {}
    assert announcements(game_db) == 0


async def test_check_pets_quietly_and_never_raising(game_db):
    db = game_db.bot.db
    patron = game_db.add_member("Patron", roles=[PATRON])
    await set_pets(db, patron.id, await add_pet(db, 7001, mythic=True))
    given = await achievements.check_pets(game_db.scene(), patron, announce=False)
    assert [item.key for item in given] == ["pet_mythic"] and game_channel_messages(game_db) == 0
    assert await achievements.check_pets(game_db.scene(), None) == []
    assert await achievements.check_pets(game_db.scene(), 555) == []           # left the server

    game_db.bot.db = DatabaseDown()
    other = game_db.add_member("Ola", roles=[PATRON])
    assert await achievements.check_pets(game_db.scene(), other) == []
    assert role_ids(other) == set()


async def test_check_pets_never_gives_the_mythic_boss(game_db, slayer):
    """A killed mythic boss leaves no trace in the data: only the kill gives its role."""
    patron = game_db.add_member("Patron", roles=[PATRON])
    await set_pets(game_db.bot.db, patron.id, await add_pet(game_db.bot.db, 7001, mythic=True))
    await achievements.check_pets(game_db.scene(), patron)
    assert slayer not in patron.roles


# -- what the data says ---------------------------------------------------------------------------------

async def test_deserved_is_what_the_pets_stand_for(game_db):
    bot = game_db.bot
    await set_kills(bot.db, 8001, kills=500, hunts=1000, butchers=25)           # counters: no achievement
    assert await achievements.deserved(bot, 8001) == set()
    await set_pets(bot.db, 8001, await add_pet(bot.db, 7001, mythic=True, holo=True))
    assert await achievements.deserved(bot, 8001) == {"pet_mythic", "pet_mythic_holo"}


async def test_sync_gives_what_the_data_says_quietly(game_db, slayer):
    bot = game_db.bot
    db = bot.db
    veteran = game_db.add_member("Weteran", roles=[PATRON])
    await set_pets(db, veteran.id, await add_pet(db, 7001, mythic=True, seal=3), await add_pet(db, 7002, holo=True))
    given = await achievements.sync(game_db.scene(), veteran)
    assert [item.key for item in given] == ["pet_mythic", "pet_mythic_seal"]
    assert keys_of(bot, veteran) == {"pet_mythic", "pet_mythic_seal"}
    assert game_channel_messages(game_db) == 0               # silent by default
    assert await achievements.sync(game_db.scene(), veteran) == []


async def test_sync_keeps_a_role_the_data_does_not_show(game_db, slayer):
    """A role given by hand (or at the kill) is never taken back."""
    bot = game_db.bot
    await achievements.ensure_roles(bot)
    patron = game_db.add_member("Patron", roles=[PATRON])
    give(game_db, patron, "pet_mythic_holo", "mythic_boss")
    assert await achievements.sync(game_db.scene(), patron) == []
    assert keys_of(bot, patron) == {"pet_mythic_holo", "mythic_boss"}


async def test_sync_can_announce(game_db):
    patron = game_db.add_member("Patron", roles=[PATRON])
    await set_pets(game_db.bot.db, patron.id, await add_pet(game_db.bot.db, 7001, mythic=True))
    given = await achievements.sync(game_db.scene(), patron, announce=True)
    assert [item.key for item in given] == ["pet_mythic"]
    assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Mityczny towarzysz!"


async def test_sync_gives_nothing_to_somebody_who_does_not_support(game_db):
    plain = game_db.add_member("Zwykły")
    await set_pets(game_db.bot.db, plain.id, await add_pet(game_db.bot.db, 7001, mythic=True, holo=True, seal=3))
    assert await achievements.sync(game_db.scene(), plain) == []
    assert await achievements.sync(game_db.scene(), None) == []
    assert role_ids(plain) == set() and stored(game_db.bot) == {}


# -- the hand-out to veterans ------------------------------------------------------------------------

async def test_sync_veterans_only_supporters_on_the_server_and_one_note_for_the_owner(game_db):
    bot = game_db.bot
    db = bot.db
    full = await add_pet(db, 7001, mythic=True, holo=True)
    sealed = await add_pet(db, 7002, mythic=True, seal=3)
    ala = game_db.add_member("Ala", roles=[PATRON])
    ola = game_db.add_member("Ola", roles=[patrons.YOUTUBE_ROLES[0]])
    plain = game_db.add_member("Zwykły")
    killer = game_db.add_member("Ela", roles=[PATRON])                         # only counters, no pets
    await set_pets(db, ala.id, full)
    await set_pets(db, ola.id, 0, sealed)
    await set_pets(db, plain.id, full)
    await set_pets(db, 555, full)                                              # a supporter who left the server
    await set_kills(db, killer.id, kills=500, hunts=1000, butchers=25)

    assert await achievements.sync_veterans(bot) == (2, 4)
    assert keys_of(bot, ala) == {"pet_mythic", "pet_mythic_holo"}
    assert keys_of(bot, ola) == {"pet_mythic", "pet_mythic_seal"}
    assert role_ids(plain) == set() and role_ids(killer) == set()
    assert game_channel_messages(game_db) == 0               # nothing on the game channels
    notes = owner_lines(bot, "Zaległe osiągnięcia")
    assert notes == ["\n".join([achievements.LOG_VETERANS.format(roles=4, players=2),
                                f"<@{ala.id}>: Mityczny towarzysz, Mityczny Holo",
                                f"<@{ola.id}>: Mityczny towarzysz, Mityczna Pieczęć Arcyłowcy"])]
    assert pings(game_db) == 0

    assert await achievements.sync_veterans(bot) == (0, 0)   # a second start: nothing new, no note
    assert len(owner_lines(bot, "Zaległe osiągnięcia")) == 1


async def test_sync_veterans_without_anything_to_give_says_nothing(game_db):
    assert await achievements.sync_veterans(game_db.bot) == (0, 0)
    assert game_db.bot.owner_log == [] and game_channel_messages(game_db) == 0


async def test_sync_veterans_without_the_server_does_nothing(game_db, monkeypatch):
    patron = game_db.add_member("Ala", roles=[PATRON])
    await set_pets(game_db.bot.db, patron.id, await add_pet(game_db.bot.db, 7001, mythic=True))
    monkeypatch.setattr(game_db.bot, "get_guild", lambda guild_id: None)
    assert await achievements.sync_veterans(game_db.bot) == (0, 0)
    assert role_ids(patron) == set() and game_db.bot.owner_log == []


async def test_sync_veterans_splits_a_long_summary_without_cutting_a_line(game_db):
    bot = game_db.bot
    await add_pet(bot.db, 7001, mythic=True, holo=True, seal=3)
    veterans = [game_db.add_member(f"Weteran {number}", roles=[PATRON]) for number in range(60)]
    for veteran in veterans:
        await set_pets(bot.db, veteran.id, 7001)
    await achievements.ensure_roles(bot)                     # (as at the start; its note is not the summary)
    bot.owner_log.clear()
    game_db.log_channel.sent.clear()
    assert await achievements.sync_veterans(bot) == (60, 180)

    notes = list(bot.owner_log)
    assert len(notes) >= 3                                   # ~5000 characters in all
    assert notes[0].startswith("Zaległe osiągnięcia: nadałem 180 ról 60 wspierającym.\n")
    assert all(len(note) <= achievements.LOG_CHUNK < 1990 for note in notes)
    assert [message.content for message in game_db.log_channel.sent] == notes   # nothing cut on the way
    lines = [line for note in notes for line in note.split("\n")][1:]
    assert sorted(lines) == sorted(f"<@{veteran.id}>: {', '.join(PET_NAMES)}" for veteran in veterans)
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


@pytest.mark.parametrize("activity", ["busy", "engaged", "fight"])
async def test_sync_veterans_waits_while_something_plays_on_the_boss_channel(game_db, monkeypatch, activity):
    bot = game_db.bot
    state = bot.state
    patron = game_db.add_member("Ala", roles=[PATRON])
    await set_pets(bot.db, patron.id, await add_pet(bot.db, 7001, mythic=True))
    await achievements.ensure_roles(bot)
    hunt = state.hold_busy("hunt")
    if activity == "busy":
        hunt.__enter__()
    else:
        await state.set_boss_phase(BossPhase.ENGAGED if activity == "engaged" else BossPhase.FIGHTING)

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
    assert keys_of(bot, patron) == {"pet_mythic"}
    assert game_channel_messages(game_db) == 0


async def test_one_players_failure_does_not_stop_the_others(game_db, monkeypatch):
    bot = game_db.bot
    broken = game_db.add_member("Ala", roles=[PATRON])
    fine = game_db.add_member("Ola", roles=[PATRON])
    pet = await add_pet(bot.db, 7001, mythic=True)
    for player in (broken, fine):
        await set_pets(bot.db, player.id, pet)
    deserved = achievements.deserved

    async def flaky(bot_, player_id):
        if player_id == broken.id:
            raise RuntimeError("test: this one's data cannot be read")
        return await deserved(bot_, player_id)

    monkeypatch.setattr(achievements, "deserved", flaky)
    assert await achievements.sync_veterans(bot) == (1, 1)
    assert keys_of(bot, fine) == {"pet_mythic"} and role_ids(broken) == set()
    notes = owner_lines(bot, "Zaległe osiągnięcia")
    assert len(notes) == 1 and f"<@{fine.id}>" in notes[0] and str(broken.id) not in notes[0]


# -- the start-up ------------------------------------------------------------------------------------

async def test_start_up_retires_then_makes_then_hands_out(game, monkeypatch):
    calls: list[str] = []

    def recording(name, result):
        async def step(bot):
            calls.append(name)
            return result
        return step

    monkeypatch.setattr(achievements, "retire_roles", recording("retire_roles", []))
    monkeypatch.setattr(achievements, "ensure_roles", recording("ensure_roles", []))
    monkeypatch.setattr(achievements, "sync_veterans", recording("sync_veterans", (0, 0)))
    await achievements.start_up(game.bot)
    assert calls == ["retire_roles", "ensure_roles", "sync_veterans"]


async def test_start_up_on_the_server_of_today(game_db, monkeypatch):
    """The withdrawn roles go first - they make room for the new ones on a full server (250 roles)."""
    bot = game_db.bot
    guild = game_db.guild
    for role_id, name in achievements.RETIRED_ROLES.items():
        guild.add_role(role_id, name)
    holo = guild.add_role(990_000_000_000_000_100, "Folia Holo")                # made earlier that day
    towarzysz = guild.add_role(990_000_000_000_000_101, "Mityczny towarzysz")   # made earlier that day
    bot.store.achievement_roles.save({"pet_holo": holo.id, "pet_mythic": towarzysz.id})
    patron = game_db.add_member("Ala", roles=[PATRON])
    patron.roles.append(holo)
    await set_pets(bot.db, patron.id, await add_pet(bot.db, 7001, mythic=True, holo=True))
    limit = len(guild.roles)                                 # the server is full
    create = guild.create_role

    async def full_server(**kwargs):
        if len(guild.roles) >= limit:
            raise discord.HTTPException(type("Response", (), {"status": 400, "reason": "Bad Request"})(),
                                        "Maximum number of guild roles reached (250)")
        return await create(**kwargs)

    monkeypatch.setattr(guild, "create_role", full_server)
    await achievements.start_up(bot)

    assert not {role.id for role in guild.roles} & {*achievements.RETIRED_ROLES, holo.id}
    assert set(stored(bot)) == set(PET_KEYS) and stored(bot)["pet_mythic"] == towarzysz.id
    assert all(achievements.role_exists(bot, item) for item in TROPHIES)
    assert keys_of(bot, patron) == {"pet_mythic", "pet_mythic_holo"}
    assert holo not in patron.roles
    assert owner_lines(bot, "Usunąłem z serwera role wycofanych osiągnięć (16)")
    assert owner_lines(bot, "Utworzyłem role osiągnięć (2)")
    assert owner_lines(bot, "Zaległe osiągnięcia: nadałem 2 ról 1 wspierającym.")
    assert owner_lines(bot, "Nie udało się") == []
    assert game_channel_messages(game_db) == 0


@pytest.mark.parametrize("step", ["retire_roles", "ensure_roles", "sync_veterans"])
async def test_start_up_never_raises(game, monkeypatch, step):
    async def broken(bot):
        raise RuntimeError("test")

    monkeypatch.setattr(achievements, step, broken)
    await achievements.start_up(game.bot)                    # logged, not raised


# -- $osiagniecia ------------------------------------------------------------------------------------

async def test_held_lists_the_achievements_in_the_catalogues_order(game, slayer):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    assert achievements.held(bot, member) == []
    give(game, member, "pet_mythic_seal", "pet_mythic")
    assert achievements.held(bot, member) == [trophy("pet_mythic"), trophy("pet_mythic_seal")]
    member.roles.append(slayer)
    assert [item.key for item in achievements.held(bot, member)] == ["mythic_boss", "pet_mythic", "pet_mythic_seal"]


async def test_held_with_achievements_given_a_moment_ago(game):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    give(game, member, "pet_mythic")
    assert achievements.held(bot, member, [trophy("pet_mythic_holo")]) == [
        trophy("pet_mythic"), trophy("pet_mythic_holo")]


async def test_held_does_not_count_the_withdrawn_roles(game):
    bot = game.bot
    member = game.add_member("Ala", roles=[PATRON, *achievements.RETIRED_ROLES])
    assert achievements.held(bot, member) == []
    assert "**0 z 4**" in achievements.summary_embed(bot, member).description


async def test_summary_shows_what_is_held_and_how_many_of_how_many(game, slayer):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    member.roles.append(slayer)
    give(game, member, "pet_mythic_holo")
    embed = achievements.summary_embed(bot, member)
    text = embed.description
    assert embed.title == achievements.MSG_SUMMARY_TITLE
    assert f"<@{member.id}>, masz **2 z 4** osiągnięć." in text
    assert f"🏅 <@&{slayer.id}>" in text and f"🏅 <@&{role_id_of(bot, trophy('pet_mythic_holo'))}>" in text
    assert "Nowe" not in text
    assert embed.footer.text == achievements.MSG_SUMMARY_FOOTER


async def test_summary_never_reveals_what_is_missing(game, slayer):
    bot = game.bot
    await achievements.ensure_roles(bot)
    member = game.add_member("Ala", roles=[PATRON])
    give(game, member, "pet_mythic")
    new = [trophy("pet_mythic")]
    embed = achievements.summary_embed(bot, member, new)
    text = "\n".join([embed.title or "", embed.description or "", embed.footer.text or "",
                      *(f"{f.name} {f.value}" for f in embed.fields)])
    assert "**1 z 4**" in text
    assert f"🎉 **Nowe:** <@&{role_id_of(bot, trophy('pet_mythic'))}>" in text
    lacking = [item for item in TROPHIES if item.key != "pet_mythic"]
    assert len(lacking) == 3
    for item in lacking:
        assert item.name not in text, item.key
        assert str(role_id_of(bot, item)) not in text, item.key


async def test_summary_of_somebody_with_nothing_yet(game):
    member = game.add_member("Ala", roles=[PATRON])
    embed = achievements.summary_embed(game.bot, member)
    assert "**0 z 4**" in embed.description
    assert achievements.MSG_SUMMARY_NONE in embed.description
    assert "<@&" not in embed.description


async def test_show_achievements_is_for_supporters_only(game_db):
    plain = game_db.add_member("Zwykły")
    await set_pets(game_db.bot.db, plain.id, await add_pet(game_db.bot.db, 7001, mythic=True))
    assert await achievements.show_achievements(game_db.scene(plain, "$osiagniecia")) is False
    answer = game_db.channel.bot_messages[-1]
    # Public, like every answer of the bot, so it says whom it answers.
    assert answer.content.startswith(f"<@{plain.id}> Osiągnięcia kolekcjonerskie są dla wspierających (")
    assert patrons.SUPPORTERS_LONG in answer.content
    assert all(url in answer.content for _, url in patrons.SUPPORT_LINKS)
    assert answer.embeds == [] and " z 4" not in answer.text
    assert role_ids(plain) == set() and stored(game_db.bot) == {}

    assert await achievements.show_achievements(game_db.scene()) is False      # nobody asked
    assert game_db.channel.bot_messages[-1].content.startswith("Osiągnięcia kolekcjonerskie")


async def test_show_achievements_without_the_database_shows_the_roles_held(game, slayer):
    bot = game.bot
    bot.db = DatabaseDown()
    await achievements.ensure_roles(bot)
    patron = game.add_member("Ala", roles=[PATRON])
    patron.roles.append(slayer)
    give(game, patron, "pet_mythic_seal")
    assert await achievements.show_achievements(game.scene(patron, "$osiagniecia")) is True
    answer = game.channel.bot_messages[-1]
    assert len(answer.embeds) == 1
    assert f"<@{patron.id}>, masz **2 z 4** osiągnięć." in answer.text
    assert f"<@&{slayer.id}>" in answer.text
    assert f"<@&{role_id_of(bot, trophy('pet_mythic_seal'))}>" in answer.text
    assert "Nowe" not in answer.text


async def test_show_achievements_hands_out_quietly_and_shows_the_summary(game_db, slayer):
    bot = game_db.bot
    patron = game_db.add_member("Ala", roles=[PATRON])
    patron.roles.append(slayer)
    await set_pets(bot.db, patron.id, await add_pet(bot.db, 7001, mythic=True, holo=True))

    assert await achievements.show_achievements(game_db.scene(patron, "$osiagniecia")) is True
    assert keys_of(bot, patron) == {"mythic_boss", "pet_mythic", "pet_mythic_holo"}
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 0    # no announcement, just the summary
    answer = game_db.channel.bot_messages[-1]
    assert len(answer.embeds) == 1                           # a public message on the channel
    text = answer.text
    assert f"<@{patron.id}>, masz **3 z 4** osiągnięć." in text
    new_line = next(line for line in text.splitlines() if line.startswith("🎉 **Nowe:**"))
    assert f"<@&{role_id_of(bot, trophy('pet_mythic'))}>" in new_line
    assert f"<@&{role_id_of(bot, trophy('pet_mythic_holo'))}>" in new_line
    assert f"<@&{slayer.id}>" not in new_line
    assert "Mityczna Pieczęć Arcyłowcy" not in text and "pet_mythic_seal" not in text

    assert await achievements.show_achievements(game_db.scene(patron, "$osiagniecia")) is True
    assert "Nowe" not in game_db.channel.bot_messages[-1].text      # nothing new the second time
    assert "**3 z 4**" in game_db.channel.bot_messages[-1].text
