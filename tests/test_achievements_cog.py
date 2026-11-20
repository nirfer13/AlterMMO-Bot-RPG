"""cogs/achievements.py: ``$osiagniecia``, the two administrator commands and the start-up task.

Owner's decisions of 2026-11-20 tested here: ``$osiagniecia`` is for supporters and shows what the
player has and "X z Y" - never which achievements are missing; somebody who does not support gets
the support links and nothing about achievements; bot answers on the channel are public; the bot
makes the roles of the new achievements itself; the hand-out to veterans after a start is silent
(one summary for the owner) and can never break the start.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from altermmo import bot as bot_module
from altermmo import schema
from altermmo.cogs import achievements as achievements_cog
from altermmo.core import guards
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import achievements, patrons
from altermmo.features.achievements import TROPHIES, role_id_of

from fakes import FakeMessage

PATRON = patrons.PATRON_ROLES[0]
KILL_10 = achievements.BOSS_KILL_ROLES[10]
HUNT_10, HUNT_35 = achievements.HUNT_ROLES[10], achievements.HUNT_ROLES[35]
BOT_MADE = [item for item in TROPHIES if item.bot_made]
FIXED = [item for item in TROPHIES if item.role_id]
GAME_CHANNELS = ("channel", "announce_channel", "notice_channel")
ALIASES = ["achievements", "osiagniecie", "osiągnięcia", "osiągnięcie"]


class DatabaseDown:
    """A database that cannot be reached at all."""

    def _fail(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    transaction = _fail

    async def execute(self, *args, **kwargs):
        self._fail()

    fetch = fetchrow = fetchval = execute


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(guards, "_last_notice", {})


@pytest.fixture
def started(monkeypatch):
    """``achievements.start_up`` only records that it was called (no roles made in the background)."""
    calls: list = []

    async def start_up(bot):
        calls.append(bot)

    monkeypatch.setattr(achievements, "start_up", start_up)
    return calls


@pytest_asyncio.fixture
async def cog(game, started):
    await game.bot.load_extension("altermmo.cogs.achievements")
    for channel in game.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    await game.settle()
    return game.bot.get_cog("Achievements")


@pytest.fixture
def old_roles(game):
    """The achievement roles the server had before v2 (the bot never makes these)."""
    return {item.role_id: game.guild.add_role(item.role_id, item.name) for item in FIXED}


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


def administrator(game, name: str = "Alter"):
    member = game.add_member(name)
    member.guild_permissions = discord.Permissions(administrator=True)
    return member


def supporters_only_text(member=None) -> str:
    """The answer to somebody who does not support; public, so it starts with his mention."""
    links = " • ".join(f"[{label}](<{url}>)" for label, url in patrons.SUPPORT_LINKS)
    text = achievements.MSG_SUPPORTERS_ONLY.format(groups=patrons.SUPPORTERS_LONG, links=links)
    return text if member is None else f"<@{member.id}> {text}"


def answers(channel, since: int = 0) -> list[str]:
    """What the bot posted on ``channel`` (texts of its messages, from ``since``)."""
    return [message.content for message in channel.sent[since:]]


def role_ids(member) -> set[int]:
    return {role.id for role in member.roles} - set(patrons.SUPPORTER_ROLES)


def announcements(game) -> int:
    return sum(getattr(game, name).sent_count("zdobywa osiągnięcie") for name in GAME_CHANNELS)


def start_tasks() -> list[asyncio.Task]:
    """The cog's start-up tasks that are still running or just finished."""
    return [task for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__qualname__", "") == "AchievementsCog._after_start"]


async def set_kills(db, player_id: int, kills: int = 0, hunts: int = 0, butchers: int = 0) -> None:
    await db.execute("INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS, HUNTS, BUTCHERS) VALUES ($1, $2, $3, $4)",
                     player_id, kills, hunts, butchers)


@pytest_asyncio.fixture
async def game_db(game_db):
    """The test database with the tables that are new in v2 (the bot makes them at start-up)."""
    await schema.create_new_tables(game_db.bot.db)
    return game_db


# -- the extension ------------------------------------------------------------------------------

async def test_extension_registers_the_commands_with_their_aliases(cog):
    assert "altermmo.cogs.achievements" in bot_module.EXTENSIONS
    table = {command.name: sorted(command.aliases) for command in cog.get_commands()}
    assert table == {"osiagniecia": ALIASES, "roleosiagniec": [], "nadajosiagniecia": []}

    def admin_only(command) -> bool:
        return any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)

    commands_by_name = {command.name: command for command in cog.get_commands()}
    assert not admin_only(commands_by_name["osiagniecia"])
    assert admin_only(commands_by_name["roleosiagniec"]) and admin_only(commands_by_name["nadajosiagniecia"])


async def test_no_name_clashes_with_the_other_extensions(game, monkeypatch, started):
    bot = game.bot
    monkeypatch.setattr(bot.supervisor, "start", lambda *args, **kwargs: None)  # no background loops
    for extension in bot_module.EXTENSIONS:
        await bot.load_extension(extension)                                 # a clash would raise here
    assert len(bot.extensions) == len(bot_module.EXTENSIONS) == 19
    owners: dict[str, list[str]] = {}
    for command in bot.commands:                                         # (top level: what "$name" finds)
        for name in {command.name, *command.aliases}:
            owners.setdefault(name.lower(), []).append(command.qualified_name)
    assert {name: names for name, names in owners.items() if len(names) > 1} == {}
    cog = bot.get_cog("Achievements")
    for name in ("osiagniecia", *ALIASES):
        assert bot.get_command(name).cog is cog, name
    assert bot.get_command("roleosiagniec").cog is cog and bot.get_command("nadajosiagniecia").cog is cog


# -- $osiagniecia ---------------------------------------------------------------------------------

async def test_supporter_sees_what_he_has_and_how_many_of_how_many_never_what_is_missing(game_db, cog,
                                                                                        old_roles):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala", roles=[PATRON, KILL_10])
    await set_kills(bot.db, ala.id, kills=12, hunts=40)                 # a veteran: 35 hunts passed long ago

    await run(game, ala, "$osiagniecia")
    (summary,) = game.channel.bot_messages[1:]                          # (after the pinned rules)
    assert summary.content == "" and summary.view is None
    (embed,) = summary.embeds
    assert embed.title == achievements.MSG_SUMMARY_TITLE
    assert embed.footer.text == achievements.MSG_SUMMARY_FOOTER
    # Boss kills I + II and hunts I + II: a tier counts together with the ones below it.
    assert f"<@{ala.id}>, masz **4 z {len(TROPHIES)}** osiągnięć." in embed.description
    # Shown: the highest tier of each family he has; the hunts tier he had earned is given now.
    assert re.findall(r"🏅 <@&(\d+)>", embed.description) == [str(KILL_10), str(HUNT_35)]
    assert achievements.MSG_SUMMARY_NEW.format(roles=f"<@&{HUNT_35}>") in embed.description
    assert role_ids(ala) == {KILL_10, HUNT_35} and announcements(game) == 0
    # Not a hint of what is missing: no other role and no name of an achievement.
    mentioned = set(re.findall(r"<@&(\d+)>", summary.text))
    assert mentioned == {str(KILL_10), str(HUNT_35)}
    assert not any(item.name in summary.text for item in TROPHIES)

    # Any channel: it only shows something.  Asked again: nothing new any more.
    await run(game, ala, "$osiągnięcia", channel=game.announce_channel)
    (again,) = game.announce_channel.bot_messages
    assert f"masz **4 z {len(TROPHIES)}**" in again.embeds[0].description
    assert "Nowe" not in again.embeds[0].description and role_ids(ala) == {KILL_10, HUNT_35}


async def test_supporter_with_nothing_yet(game_db, cog):
    game = game_db
    ala = game.add_member("Ala", roles=[patrons.TWITCH_SUB_ROLE])
    await run(game, ala, "$achievements", channel=game.notice_channel)
    (summary,) = game.notice_channel.bot_messages
    assert summary.embeds[0].description == (
        f"<@{ala.id}>, masz **0 z {len(TROPHIES)}** osiągnięć.\n\n{achievements.MSG_SUMMARY_NONE}")
    assert "<@&" not in summary.text


async def test_non_supporter_gets_the_links_and_no_achievement_data(game_db, cog, old_roles):
    game = game_db
    bot = game.bot
    ola = game.add_member("Ola")
    await set_kills(bot.db, ola.id, kills=12, hunts=40)                 # counted, but no roles
    await run(game, ola, "$osiagniecia")
    (answer,) = game.channel.bot_messages[1:]
    assert answer.content == supporters_only_text(ola) and answer.embeds == []
    assert answer.content.startswith(f"<@{ola.id}> Osiągnięcia kolekcjonerskie są dla wspierających")
    assert patrons.SUPPORTERS_LONG in answer.content
    assert re.findall(r"\(<(\S+?)>\)", answer.content) == [url for _, url in patrons.SUPPORT_LINKS]
    assert "<@&" not in answer.content and " z " not in answer.content and "masz" not in answer.content
    assert role_ids(ola) == set() and announcements(game) == 0
    assert bot.owner_log == []


async def test_every_alias_answers_on_any_channel(game, cog):
    ola = game.add_member("Ola")
    for name in ("osiagniecia", *ALIASES):
        for channel in (game.channel, game.announce_channel, game.notice_channel):
            before = len(channel.sent)
            await run(game, ola, f"${name}", channel=channel)
            assert answers(channel, before) == [supporters_only_text(ola)], (name, channel.name)


async def test_osiagniecia_while_busy_follows_the_busy_policy(game, cog, monkeypatch):
    bot = game.bot
    ola = game.add_member("Ala")
    with bot.state.hold_busy("event:TEST"):
        await run(game, ola, "$osiagniecia")
        await run(game, ola, "$osiagniecia")                             # one notice per 20 s on the channel
        assert answers(game.channel) == [guards.BUSY_NOTICE]

        monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="silent"))
        await run(game, ola, "$osiagniecia", channel=game.announce_channel)
        assert answers(game.announce_channel) == []

        # A display-only command: free under "read_only_free" (like $stajnia).
        monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="read_only_free"))
        await run(game, ola, "$osiagniecia")
        assert answers(game.channel) == [guards.BUSY_NOTICE, supporters_only_text(ola)]

    monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="notify"))
    await run(game, ola, "$osiagniecia")
    assert answers(game.channel)[-1] == supporters_only_text(ola)


async def test_osiagniecia_when_the_database_is_down(game, cog, old_roles, caplog):
    """The roles a supporter holds are enough to answer: the summary is built from them alone."""
    game.bot.db = DatabaseDown()
    ala = game.add_member("Ala", roles=[PATRON, KILL_10, HUNT_35])
    with caplog.at_level(logging.ERROR, logger="altermmo.features.achievements"):
        await run(game, ala, "$osiagniecia")
    (summary,) = game.channel.bot_messages[1:]                          # (after the pinned rules)
    assert summary.content == "" and summary.view is None
    (embed,) = summary.embeds
    assert embed.title == achievements.MSG_SUMMARY_TITLE
    # Boss kills I + II and hunts I + II: a tier counts together with the ones below it.
    assert f"<@{ala.id}>, masz **4 z {len(TROPHIES)}** osiągnięć." in embed.description and len(TROPHIES) == 39
    assert re.findall(r"🏅 <@&(\d+)>", embed.description) == [str(KILL_10), str(HUNT_35)]
    assert "Nowe" not in embed.description and role_ids(ala) == {KILL_10, HUNT_35}
    assert not game.channel.ever_sent("Baza danych jest chwilowo niedostępna")
    assert game.bot.owner_log == [] and announcements(game) == 0
    assert any("showing the roles held" in record.getMessage() for record in caplog.records)


async def test_osiagniecia_when_the_database_is_down_and_nothing_is_held(game, cog):
    game.bot.db = DatabaseDown()
    ala = game.add_member("Ala", roles=[PATRON])
    await run(game, ala, "$osiagniecia")
    (summary,) = game.channel.bot_messages[1:]
    assert summary.embeds[0].description == (
        f"<@{ala.id}>, masz **0 z {len(TROPHIES)}** osiągnięć.\n\n{achievements.MSG_SUMMARY_NONE}")
    assert game.bot.owner_log == []


# -- $roleosiagniec -----------------------------------------------------------------------------------

async def test_roleosiagniec_is_for_administrators_only(game, cog):
    roles_before = list(game.guild.roles)
    for command in ("$roleosiagniec", "$nadajosiagniecia"):
        await run(game, game.add_member("Ola"), command)
    assert answers(game.channel) == [] and game.guild.roles == roles_before
    assert game.bot.owner_log == [] and game.bot.store.achievement_roles.load() == {}


async def test_roleosiagniec_makes_the_missing_roles_then_reports_that_all_exist(game, cog):
    bot = game.bot
    alter = administrator(game)
    names = [item.name for item in BOT_MADE]
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel) == [achievements_cog.MSG_ROLES_MADE.format(count=23, names=", ".join(names))]
    made = {role.name: role for role in game.guild.roles if role.name in names}
    assert sorted(made) == sorted(names) and len(names) == 23
    assert all(role.created_with["permissions"] == discord.Permissions.none() for role in made.values())
    remembered = bot.store.achievement_roles.load()
    assert remembered == {item.key: made[item.name].id for item in BOT_MADE}
    assert any(line.startswith("Utworzyłem role osiągnięć (23)") for line in bot.owner_log)

    roles_before = list(game.guild.roles)
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == achievements_cog.MSG_ROLES_PRESENT.format(count=23)
    assert game.guild.roles == roles_before and len(answers(game.channel)) == 2


async def test_roleosiagniec_makes_a_role_again_that_was_deleted_on_the_server(game, cog):
    bot = game.bot
    alter = administrator(game)
    await run(game, alter, "$roleosiagniec")
    lost = game.guild.get_role(bot.store.achievement_roles.load()["rituals_20"])
    assert lost is not None and lost.name == "Kultysta II"

    game.guild.roles.remove(lost)                                        # somebody deletes it on the server
    assert game.guild.get_role(lost.id) is None
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == achievements_cog.MSG_ROLES_MADE.format(count=1, names="Kultysta II")
    again = game.guild.get_role(bot.store.achievement_roles.load()["rituals_20"])
    assert again is not None and again is not lost and again.name == "Kultysta II"


def roles_missing_text(missing: list[str]) -> str:
    return achievements_cog.MSG_ROLES_MISSING.format(missing_count=len(missing), missing=", ".join(missing),
                                                     count=len(BOT_MADE) - len(missing))


async def test_roleosiagniec_reports_what_is_missing_when_the_roles_cannot_be_made(game, cog):
    bot = game.bot
    alter = administrator(game)
    names = [item.name for item in BOT_MADE]
    game.guild.create_role_error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                                      "Missing Permissions")
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel) == [roles_missing_text(names)]
    assert answers(game.channel)[0].endswith("Istniejące role: 0.")
    assert bot.store.achievement_roles.load() == {}
    assert bot.owner_log == [achievements.LOG_ROLES_FAILED.format(names=", ".join(names))]

    # Only some can be made: both lines - what was made now and what is still missing.
    game.guild.create_role_error = None
    original = type(game.guild).create_role
    tried: list[str] = []

    async def picky(self, *, name, **kwargs):
        tried.append(name)
        if name == "Kultysta II":
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "Too many roles")
        return await original(self, name=name, **kwargs)

    game.guild.create_role = picky.__get__(game.guild)
    await run(game, alter, "$roleosiagniec")
    made = [name for name in names if name != "Kultysta II"]
    assert answers(game.channel)[-1] == "\n".join([
        achievements_cog.MSG_ROLES_MADE.format(count=22, names=", ".join(made)),
        roles_missing_text(["Kultysta II"])])
    assert sorted(bot.store.achievement_roles.load()) == sorted(item.key for item in BOT_MADE
                                                                if item.name != "Kultysta II")
    assert sorted(tried) == sorted(names)

    # Asked again: the administrator's call tries the missing one at once (no back-off), only that one.
    tried.clear()
    await run(game, alter, "$roleosiagniec")
    assert tried == ["Kultysta II"]
    assert answers(game.channel)[-1] == roles_missing_text(["Kultysta II"])
    # The owner was told once about each role that could not be made, not at every attempt.
    assert sum(line.startswith(achievements.LOG_ROLES_FAILED.split("{")[0]) for line in bot.owner_log) == 1

    # Once it can be made: only the made line.
    game.guild.create_role = original.__get__(game.guild)
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == achievements_cog.MSG_ROLES_MADE.format(count=1, names="Kultysta II")
    assert len(bot.store.achievement_roles.load()) == 23


async def test_roleosiagniec_does_not_claim_that_all_roles_exist_when_some_are_missing(game, cog):
    game.guild.create_role_error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                                      "Missing Permissions")
    await run(game, administrator(game), "$roleosiagniec")
    (answer,) = answers(game.channel)
    assert answer == roles_missing_text([item.name for item in BOT_MADE])
    assert achievements_cog.MSG_ROLES_PRESENT.split("(")[0] not in answer
    assert achievements_cog.MSG_ROLES_MADE.split("(")[0] not in answer


# -- $nadajosiagniecia ----------------------------------------------------------------------------

async def test_nadajosiagniecia_with_nothing_to_give(game_db, cog, old_roles):
    game = game_db
    game.add_member("Ala", roles=[PATRON])                               # a supporter without deeds
    await run(game, administrator(game), "$nadajosiagniecia")
    assert answers(game.channel) == [achievements_cog.MSG_SYNC_STARTED, achievements_cog.MSG_SYNC_NOTHING]
    assert len(game.bot.store.achievement_roles.load()) == 23            # the roles are made first
    assert not any(line.startswith("Zaległe osiągnięcia") for line in game.bot.owner_log)


async def test_nadajosiagniecia_gives_supporters_what_they_earned_quietly(game_db, cog, old_roles):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala", roles=[PATRON, HUNT_10])
    ola = game.add_member("Ola")                                         # does not support
    await set_kills(bot.db, ala.id, hunts=40)
    await set_kills(bot.db, ola.id, hunts=40)
    await bot.db.execute("INSERT INTO ACHIEVEMENT_COUNTERS (PLAYER_ID, NAME, VALUE) VALUES ($1, 'tournaments', 1)",
                         ala.id)                                         # won before the role existed
    assert role_ids(ala) == {HUNT_10}

    await run(game, administrator(game), "$nadajosiagniecia")
    assert answers(game.channel) == [achievements_cog.MSG_SYNC_STARTED,
                                     achievements_cog.MSG_SYNC_DONE.format(roles=2, players=1)]
    tournament = role_id_of(bot, achievements.trophy("tournaments_1"))
    assert role_ids(ala) == {HUNT_35, tournament} and role_ids(ola) == set()
    assert announcements(game) == 0                                       # silent on the game channels
    (summary,) = [line for line in bot.owner_log if line.startswith("Zaległe osiągnięcia")]
    assert summary.startswith(achievements.LOG_VETERANS.format(roles=2, players=1, lines="")) and \
        f"<@{ala.id}>" in summary and f"<@{ola.id}>" not in summary

    await run(game, administrator(game, "Admin2"), "$nadajosiagniecia")  # again: nothing left
    assert answers(game.channel)[-1] == achievements_cog.MSG_SYNC_NOTHING


async def test_nadajosiagniecia_when_it_fails(game, cog, caplog):
    game.bot.db = DatabaseDown()
    game.add_member("Ala", roles=[PATRON])
    with caplog.at_level(logging.ERROR, logger="altermmo.cogs.achievements"):
        await run(game, administrator(game), "$nadajosiagniecia")
    assert answers(game.channel) == [achievements_cog.MSG_SYNC_STARTED,
                                     achievements_cog.MSG_SYNC_FAILED.format(kind="DatabaseUnavailable")]
    assert any("$nadajosiagniecia failed" in record.getMessage() for record in caplog.records)


# -- the start-up task --------------------------------------------------------------------------------

@pytest.fixture
def not_ready(game, monkeypatch):
    """The bot is not connected yet: ``wait_until_ready`` waits until the event is set."""
    ready = asyncio.Event()

    async def wait_until_ready():
        await ready.wait()

    monkeypatch.setattr(game.bot, "wait_until_ready", wait_until_ready)
    return ready


async def test_start_up_runs_once_after_the_bot_is_ready(game, started, not_ready):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.achievements")
    (task,) = start_tasks()
    for _ in range(3):
        await game.settle()
    assert started == [] and not task.done()                             # not before the bot is ready

    not_ready.set()
    await asyncio.wait_for(task, 2)
    assert started == [bot] and task.exception() is None
    await game.settle()
    assert started == [bot] and start_tasks() == []                      # once, and the task is over
    await bot.unload_extension("altermmo.cogs.achievements")             # a finished task: nothing to cancel
    assert started == [bot]


async def test_unloading_the_cog_cancels_the_waiting_start(game, started, not_ready):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.achievements")
    (task,) = start_tasks()
    await bot.unload_extension("altermmo.cogs.achievements")
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    not_ready.set()
    await game.settle()
    assert started == [] and start_tasks() == []


@pytest.mark.parametrize("trouble", ["roles", "hand-out", "database"])
async def test_trouble_inside_start_up_never_escapes_the_task(game, monkeypatch, caplog, trouble):
    bot = game.bot
    supporter = game.add_member("Ala", roles=[PATRON])

    async def broken(bot_):
        raise RuntimeError("test")

    if trouble == "roles":
        monkeypatch.setattr(achievements, "ensure_roles", broken)
    elif trouble == "hand-out":
        monkeypatch.setattr(achievements, "sync_veterans", broken)
    else:
        bot.db = DatabaseDown()                                          # the bot starts while the database is down
    unhandled: list = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop_, context: unhandled.append(context))
    try:
        with caplog.at_level(logging.ERROR, logger="altermmo.features.achievements"):
            await bot.load_extension("altermmo.cogs.achievements")
            (task,) = start_tasks()
            await asyncio.wait_for(task, 2)
    finally:
        loop.set_exception_handler(previous)
    assert not task.cancelled() and task.exception() is None and unhandled == []
    assert any(record.getMessage() == "Achievements at start-up failed." for record in caplog.records)
    assert announcements(game) == 0 and supporter.roles == [game.guild.get_role(PATRON)]
    if trouble != "roles":                                                # the roles were made before the trouble
        assert len(bot.store.achievement_roles.load()) == 23
    # The game goes on: the commands work.
    game.channel.type = discord.ChannelType.text
    ola = game.add_member("Ola")
    await run(game, ola, "$osiagniecia")
    assert answers(game.channel)[-1] == supporters_only_text(ola)
