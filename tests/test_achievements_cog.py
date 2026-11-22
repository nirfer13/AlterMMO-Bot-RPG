"""cogs/achievements.py: ``$osiagniecia``, the two administrator commands and the start-up task.

Owner's decisions of 2026-11-20 tested here: ``$osiagniecia`` is for supporters and shows what the
player has and "X z Y" - never which achievements are missing; somebody who does not support gets
the support links and nothing about achievements; bot answers on the channel are public; the bot
makes the roles of the achievements itself; the hand-out to veterans after a start is silent (one
summary for the owner) and can never break the start.

Owner's decision of 2026-11-20 (evening): only four achievements are left - the mythic boss (the
game's own ``mythic_slayer_role``) and three pet roles the bot makes (a mythic pet, a mythic pet
with Holo, a mythic pet with the rarest seal).  Every other achievement role is deleted from the
server by the bot (at the start and by ``$roleosiagniec``).
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
from test_pets import add_owner, add_pet

PATRON = patrons.PATRON_ROLES[0]
BOT_MADE = [item for item in TROPHIES if item.bot_made]
BOT_MADE_NAMES = ["Mityczny towarzysz", "Mityczny Holo", "Mityczna Pieczęć Arcyłowcy", "Pełny album"]
GAME_CHANNELS = ("channel", "announce_channel", "notice_channel")
ALIASES = ["achievements", "osiagniecie", "osiągnięcia", "osiągnięcie"]
#: Two of the old bot's achievement roles (deleted from the server since the evening of 2026-11-20).
KILL_10, HUNT_10 = 1125799392989949973, 1125799891923374121
#: Roles the bot made earlier on 2026-11-20 for achievements withdrawn the same evening.
EARLIER_MADE = {"rituals_20": (990_000_000_000_000_501, "Kultysta II"),
                "pet_holo": (990_000_000_000_000_502, "Folia Holo")}


def saved_roles(bot) -> dict:
    """The saved role ids as one mapping (the file keeps the later achievements' apart:
    ``achievements.LATER_ROLES_KEY``)."""
    saved = dict(bot.store.achievement_roles.load())
    saved.update(saved.pop(achievements.LATER_ROLES_KEY, {}))
    return saved


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
    """The achievement roles the server had before the owner withdrew them (fixed ids)."""
    return {role_id: game.guild.add_role(role_id, name) for role_id, name in achievements.RETIRED_ROLES.items()}


@pytest.fixture
def earlier_made(game):
    """Roles the bot made earlier that day, remembered in ``data/achievementRoles.json``."""
    roles = {key: game.guild.add_role(role_id, name) for key, (role_id, name) in EARLIER_MADE.items()}
    game.bot.store.achievement_roles.save({key: role.id for key, role in roles.items()})
    return roles


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


def guild_role_ids(game) -> set[int]:
    return {role.id for role in game.guild.roles}


def announcements(game) -> int:
    return sum(getattr(game, name).sent_count("zdobywa osiągnięcie") for name in GAME_CHANNELS)


def made_role(bot, key: str) -> int | None:
    return role_id_of(bot, achievements.trophy(key))


def start_tasks() -> list[asyncio.Task]:
    """The cog's start-up tasks that are still running or just finished."""
    return [task for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__qualname__", "") == "AchievementsCog._after_start"]


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


async def own_pet(db, player_id: int, pet_id: int, **pet) -> None:
    """The player owns one pet (the active one)."""
    await add_owner(db, player_id, pet_id)
    await add_pet(db, pet_id, **pet)


@pytest_asyncio.fixture
async def game_db(game_db):
    """The test database with the tables that are new in v2 (the bot makes them at start-up)."""
    await schema.create_new_tables(game_db.bot.db)
    return game_db


# -- the extension ------------------------------------------------------------------------------

def test_the_catalogue_is_the_four_mythic_achievements_and_the_album():
    assert [item.key for item in TROPHIES] == ["mythic_boss", "pet_mythic", "pet_mythic_holo", "pet_mythic_seal",
                                               "album_full"]
    assert [item.name for item in BOT_MADE] == BOT_MADE_NAMES
    assert [item.ids_attr for item in TROPHIES if not item.bot_made] == ["mythic_slayer_role"]
    assert len(achievements.RETIRED_ROLES) == 15


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
    assert len(bot.extensions) == len(bot_module.EXTENSIONS) == 22                 # (the fight journal: 2026-11-22)
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
    slayer = bot.ids.mythic_slayer_role
    # A veteran: killed a mythic boss, has an old (withdrawn) role still, and a mythic Holo pet
    # from before the roles existed.
    ala = game.add_member("Ala", roles=[PATRON, slayer, KILL_10])
    await own_pet(bot.db, ala.id, 5, mythic=True, holo=True)

    await run(game, ala, "$osiagniecia")
    (summary,) = game.channel.bot_messages[1:]                          # (after the pinned rules)
    assert summary.content == "" and summary.view is None
    (embed,) = summary.embeds
    assert embed.title == achievements.MSG_SUMMARY_TITLE
    assert embed.footer.text == achievements.MSG_SUMMARY_FOOTER
    mythic, holo = made_role(bot, "pet_mythic"), made_role(bot, "pet_mythic_holo")
    assert game.guild.get_role(mythic).name == "Mityczny towarzysz"
    assert game.guild.get_role(holo).name == "Mityczny Holo"
    # The withdrawn role does not count: the mythic boss + the two pet roles given now.
    assert f"<@{ala.id}>, masz **3 z 5** osiągnięć." in embed.description
    assert re.findall(r"🏅 <@&(\d+)>", embed.description) == [str(slayer), str(mythic), str(holo)]
    assert achievements.MSG_SUMMARY_NEW.format(roles=f"<@&{mythic}>, <@&{holo}>") in embed.description
    assert role_ids(ala) == {slayer, KILL_10, mythic, holo} and announcements(game) == 0
    # Not a hint of what is missing: no other role, no name of an achievement, the seal's role not made.
    assert set(re.findall(r"<@&(\d+)>", summary.text)) == {str(slayer), str(mythic), str(holo)}
    assert not any(item.name in summary.text for item in TROPHIES) and "Pieczęć" not in summary.text
    assert made_role(bot, "pet_mythic_seal") is None
    assert KILL_10 in guild_role_ids(game)                              # (deleting is not this command's job)

    # Any channel: it only shows something.  Asked again: nothing new any more.
    await run(game, ala, "$osiągnięcia", channel=game.announce_channel)
    (again,) = game.announce_channel.bot_messages
    assert "masz **3 z 5**" in again.embeds[0].description
    assert "Nowe" not in again.embeds[0].description and role_ids(ala) == {slayer, KILL_10, mythic, holo}


async def test_supporter_with_the_rarest_seal_gets_the_seal_role(game_db, cog):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala", roles=[patrons.TWITCH_SUB_ROLE])
    await add_owner(bot.db, ala.id, 5, pet_id_alt2=6)
    await add_pet(bot.db, 5, mythic=False, seal=3, holo=True)            # not mythic: counts for nothing
    await add_pet(bot.db, 6, mythic=True, seal=3)                        # in the stable: counts
    await run(game, ala, "$osiagniecia")
    (summary,) = game.channel.bot_messages[1:]
    mythic, seal = made_role(bot, "pet_mythic"), made_role(bot, "pet_mythic_seal")
    assert f"<@{ala.id}>, masz **2 z 5** osiągnięć." in summary.embeds[0].description
    assert re.findall(r"🏅 <@&(\d+)>", summary.text) == [str(mythic), str(seal)]
    assert role_ids(ala) == {mythic, seal} and made_role(bot, "pet_mythic_holo") is None


async def test_supporter_with_nothing_yet(game_db, cog):
    game = game_db
    ala = game.add_member("Ala", roles=[patrons.TWITCH_SUB_ROLE])
    await own_pet(game.bot.db, ala.id, 5)                                # an ordinary pet
    await run(game, ala, "$achievements", channel=game.notice_channel)
    (summary,) = game.notice_channel.bot_messages
    assert summary.embeds[0].description == (
        f"<@{ala.id}>, masz **0 z 5** osiągnięć.\n\n{achievements.MSG_SUMMARY_NONE}")
    assert "<@&" not in summary.text and game.bot.store.achievement_roles.load() == {}


async def test_non_supporter_gets_the_links_and_no_achievement_data(game_db, cog, old_roles):
    game = game_db
    bot = game.bot
    ola = game.add_member("Ola", roles=[bot.ids.mythic_slayer_role])
    await own_pet(bot.db, ola.id, 5, mythic=True, holo=True, seal=3)     # would earn all three
    roles_before = guild_role_ids(game)
    await run(game, ola, "$osiagniecia")
    (answer,) = game.channel.bot_messages[1:]
    assert answer.content == supporters_only_text(ola) and answer.embeds == []
    assert answer.content.startswith(f"<@{ola.id}> Osiągnięcia kolekcjonerskie są dla wspierających")
    assert patrons.SUPPORTERS_LONG in answer.content
    assert re.findall(r"\(<(\S+?)>\)", answer.content) == [url for _, url in patrons.SUPPORT_LINKS]
    assert "<@&" not in answer.content and " z " not in answer.content and "masz" not in answer.content
    assert role_ids(ola) == {bot.ids.mythic_slayer_role} and announcements(game) == 0
    assert guild_role_ids(game) == roles_before and bot.store.achievement_roles.load() == {}
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


async def test_osiagniecia_when_the_database_is_down(game, cog, caplog):
    """The roles a supporter holds are enough to answer: the summary is built from them alone."""
    bot = game.bot
    bot.db = DatabaseDown()
    slayer = bot.ids.mythic_slayer_role
    mythic = game.guild.add_role(990_000_000_000_000_300, "Mityczny towarzysz")
    bot.store.achievement_roles.save({"pet_mythic": mythic.id})          # made after an earlier start
    ala = game.add_member("Ala", roles=[PATRON, mythic.id, slayer])
    with caplog.at_level(logging.ERROR, logger="altermmo.features.achievements"):
        await run(game, ala, "$osiagniecia")
    (summary,) = game.channel.bot_messages[1:]                          # (after the pinned rules)
    assert summary.content == "" and summary.view is None
    (embed,) = summary.embeds
    assert embed.title == achievements.MSG_SUMMARY_TITLE
    assert f"<@{ala.id}>, masz **2 z 5** osiągnięć." in embed.description
    assert re.findall(r"🏅 <@&(\d+)>", embed.description) == [str(slayer), str(mythic.id)]  # catalogue order
    assert "Nowe" not in embed.description and role_ids(ala) == {mythic.id, slayer}
    assert not game.channel.ever_sent("Baza danych jest chwilowo niedostępna")
    assert bot.owner_log == [] and announcements(game) == 0
    assert any("showing the roles held" in record.getMessage() for record in caplog.records)


async def test_osiagniecia_when_the_database_is_down_and_nothing_is_held(game, cog):
    game.bot.db = DatabaseDown()
    ala = game.add_member("Ala", roles=[PATRON])
    await run(game, ala, "$osiagniecia")
    (summary,) = game.channel.bot_messages[1:]
    assert summary.embeds[0].description == (
        f"<@{ala.id}>, masz **0 z 5** osiągnięć.\n\n{achievements.MSG_SUMMARY_NONE}")
    assert game.bot.owner_log == []


# -- $roleosiagniec -----------------------------------------------------------------------------------

async def test_roleosiagniec_is_for_administrators_only(game, cog, old_roles, earlier_made):
    roles_before = list(game.guild.roles)
    saved = game.bot.store.achievement_roles.load()
    for command in ("$roleosiagniec", "$nadajosiagniecia"):
        await run(game, game.add_member("Ola"), command)
    assert answers(game.channel) == [] and game.guild.roles == roles_before   # nothing made, nothing deleted
    assert game.bot.owner_log == [] and game.bot.store.achievement_roles.load() == saved


async def test_roleosiagniec_deletes_the_withdrawn_roles_and_makes_the_missing_ones(game, cog, old_roles,
                                                                                  earlier_made, caplog):
    bot = game.bot
    alter = administrator(game)
    slayer = game.guild.get_role(bot.ids.mythic_slayer_role)
    ala = game.add_member("Ala", roles=[PATRON, KILL_10, HUNT_10, slayer.id, EARLIER_MADE["pet_holo"][0]])
    other = game.guild.add_role(990_000_000_000_000_777, "Rzeźnik Potworów")  # not an achievement: stays
    retired = list(achievements.RETIRED_ROLES.values()) + ["Kultysta II", "Folia Holo"]

    with caplog.at_level(logging.INFO, logger="altermmo.features.achievements"):
        await run(game, alter, "$roleosiagniec")
    assert answers(game.channel) == ["\n".join([
        achievements_cog.MSG_ROLES_RETIRED.format(count=17, names=", ".join(retired)),
        achievements_cog.MSG_ROLES_MADE.format(count=4, names=", ".join(BOT_MADE_NAMES))])]
    # Gone from the server and from every member; the mythic boss and other roles stay.
    gone = set(old_roles) | {role_id for role_id, _ in EARLIER_MADE.values()}
    assert not gone & guild_role_ids(game)
    assert all(role.deleted_with == achievements.RETIRE_REASON for role in old_roles.values())
    assert role_ids(ala) == {slayer.id} and {slayer.id, other.id, PATRON} <= guild_role_ids(game)
    # Who held them is in the log before they went.
    held_by = [record.getMessage() for record in caplog.records if "is being deleted" in record.getMessage()]
    assert len(held_by) == 17 and sum(str(ala.id) in line for line in held_by) == 3
    # The three roles are made (no permissions) and remembered; the withdrawn ids are forgotten.
    made = {role.name: role for role in game.guild.roles if role.name in BOT_MADE_NAMES}
    assert sorted(made) == sorted(BOT_MADE_NAMES)
    assert all(role.created_with["permissions"] == discord.Permissions.none() for role in made.values())
    assert saved_roles(bot) == {item.key: made[item.name].id for item in BOT_MADE}
    assert bot.owner_log == [
        achievements.LOG_ROLES_RETIRED.format(count=17, names=", ".join(retired)),
        achievements.LOG_ROLES_CREATED.format(count=4, names=", ".join(BOT_MADE_NAMES))]

    roles_before = list(game.guild.roles)
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == achievements_cog.MSG_ROLES_PRESENT.format(count=4)
    assert game.guild.roles == roles_before and len(answers(game.channel)) == 2 and len(bot.owner_log) == 2


async def test_roleosiagniec_says_which_role_it_could_not_delete(game, cog, old_roles):
    bot = game.bot
    alter = administrator(game)
    stubborn = old_roles[1125818642450370590]                            # "szybkość", above the bot's role
    stubborn.delete_error = forbidden()
    ala = game.add_member("Ala", roles=[stubborn.id])
    deleted = [name for role_id, name in achievements.RETIRED_ROLES.items() if role_id != stubborn.id]

    await run(game, alter, "$roleosiagniec")
    (answer,) = answers(game.channel)
    still_there = achievements_cog.MSG_ROLES_NOT_RETIRED.format(count=1, names=stubborn.name)
    assert answer == "\n".join([
        achievements_cog.MSG_ROLES_RETIRED.format(count=14, names=", ".join(deleted)),   # not among the deleted
        still_there,
        achievements_cog.MSG_ROLES_MADE.format(count=4, names=", ".join(BOT_MADE_NAMES))])
    assert guild_role_ids(game) & set(old_roles) == {stubborn.id} and role_ids(ala) == {stubborn.id}
    assert achievements.LOG_ROLES_NOT_RETIRED.format(names="szybkość") in bot.owner_log

    # Asked again: tried again, still refused - the administrator is told again, the owner's log is not.
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == "\n".join([still_there,
                                                   achievements_cog.MSG_ROLES_PRESENT.format(count=4)])
    assert stubborn.id in guild_role_ids(game)
    assert bot.owner_log.count(achievements.LOG_ROLES_NOT_RETIRED.format(names="szybkość")) == 1

    # Once the bot may delete it: deleted and reported.
    stubborn.delete_error = None
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == "\n".join([
        achievements_cog.MSG_ROLES_RETIRED.format(count=1, names="szybkość"),
        achievements_cog.MSG_ROLES_PRESENT.format(count=4)])
    assert stubborn.id not in guild_role_ids(game) and role_ids(ala) == set()


async def test_roleosiagniec_tells_the_administrator_which_withdrawn_role_is_still_there(game, cog, old_roles):
    stubborn = old_roles[1125818642450370590]                            # "szybkość"
    stubborn.delete_error = forbidden()
    alter = administrator(game)
    await run(game, alter, "$roleosiagniec")
    await run(game, alter, "$roleosiagniec")                             # (the owner's log: told only at first)
    assert all("szybkość" in answer for answer in answers(game.channel))


async def test_roleosiagniec_makes_a_role_again_that_was_deleted_on_the_server(game, cog):
    bot = game.bot
    alter = administrator(game)
    await run(game, alter, "$roleosiagniec")
    lost = game.guild.get_role(bot.store.achievement_roles.load()["pet_mythic_holo"])
    assert lost is not None and lost.name == "Mityczny Holo"

    game.guild.roles.remove(lost)                                        # somebody deletes it on the server
    assert game.guild.get_role(lost.id) is None
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == achievements_cog.MSG_ROLES_MADE.format(count=1, names="Mityczny Holo")
    again = game.guild.get_role(bot.store.achievement_roles.load()["pet_mythic_holo"])
    assert again is not None and again is not lost and again.name == "Mityczny Holo"


def roles_missing_text(missing: list[str]) -> str:
    return achievements_cog.MSG_ROLES_MISSING.format(missing_count=len(missing), missing=", ".join(missing),
                                                     count=len(BOT_MADE) - len(missing))


async def test_roleosiagniec_reports_what_is_missing_when_the_roles_cannot_be_made(game, cog):
    bot = game.bot
    alter = administrator(game)
    names = BOT_MADE_NAMES
    game.guild.create_role_error = forbidden()
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
        if name == "Mityczny Holo":
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "Too many roles")
        return await original(self, name=name, **kwargs)

    game.guild.create_role = picky.__get__(game.guild)
    await run(game, alter, "$roleosiagniec")
    made = [name for name in names if name != "Mityczny Holo"]
    assert answers(game.channel)[-1] == "\n".join([
        achievements_cog.MSG_ROLES_MADE.format(count=3, names=", ".join(made)),
        roles_missing_text(["Mityczny Holo"])])
    assert sorted(saved_roles(bot)) == ["album_full", "pet_mythic", "pet_mythic_seal"]
    assert sorted(tried) == sorted(names)

    # Asked again: the administrator's call tries the missing one at once (no back-off), only that one.
    tried.clear()
    await run(game, alter, "$roleosiagniec")
    assert tried == ["Mityczny Holo"]
    assert answers(game.channel)[-1] == roles_missing_text(["Mityczny Holo"])
    # The owner was told once about each role that could not be made, not at every attempt.
    assert sum(line.startswith(achievements.LOG_ROLES_FAILED.split("{")[0]) for line in bot.owner_log) == 1

    # Once it can be made: only the made line.
    game.guild.create_role = original.__get__(game.guild)
    await run(game, alter, "$roleosiagniec")
    assert answers(game.channel)[-1] == achievements_cog.MSG_ROLES_MADE.format(count=1, names="Mityczny Holo")
    assert len(bot.store.achievement_roles.load()) == 4


async def test_roleosiagniec_does_not_claim_that_all_roles_exist_when_some_are_missing(game, cog):
    game.guild.create_role_error = forbidden()
    await run(game, administrator(game), "$roleosiagniec")
    (answer,) = answers(game.channel)
    assert answer == roles_missing_text(BOT_MADE_NAMES)
    assert achievements_cog.MSG_ROLES_PRESENT.split("(")[0] not in answer
    assert achievements_cog.MSG_ROLES_MADE.split("(")[0] not in answer
    assert achievements_cog.MSG_ROLES_RETIRED.split("(")[0] not in answer


# -- $nadajosiagniecia ----------------------------------------------------------------------------

async def test_nadajosiagniecia_with_nothing_to_give(game_db, cog):
    game = game_db
    ala = game.add_member("Ala", roles=[PATRON])                         # a supporter with an ordinary pet
    await own_pet(game.bot.db, ala.id, 5, holo=True, seal=3)
    await run(game, administrator(game), "$nadajosiagniecia")
    assert answers(game.channel) == [achievements_cog.MSG_SYNC_STARTED, achievements_cog.MSG_SYNC_NOTHING]
    assert len(game.bot.store.achievement_roles.load()) == 4             # the roles are made first
    assert role_ids(ala) == set()
    assert not any(line.startswith("Zaległe osiągnięcia") for line in game.bot.owner_log)


async def test_nadajosiagniecia_gives_supporters_what_they_earned_quietly(game_db, cog):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala", roles=[PATRON])
    ola = game.add_member("Ola")                                         # does not support
    await own_pet(bot.db, ala.id, 5, mythic=True, seal=3)                # from before the roles existed
    await own_pet(bot.db, ola.id, 6, mythic=True, seal=3)

    await run(game, administrator(game), "$nadajosiagniecia")
    assert answers(game.channel) == [achievements_cog.MSG_SYNC_STARTED,
                                     achievements_cog.MSG_SYNC_DONE.format(roles=2, players=1)]
    assert role_ids(ala) == {made_role(bot, "pet_mythic"), made_role(bot, "pet_mythic_seal")}
    assert role_ids(ola) == set()
    assert announcements(game) == 0                                       # silent on the game channels
    (summary,) = [line for line in bot.owner_log if line.startswith("Zaległe osiągnięcia")]
    assert summary == (achievements.LOG_VETERANS.format(roles=2, players=1)
                       + f"\n<@{ala.id}>: Mityczny towarzysz, Mityczna Pieczęć Arcyłowcy")

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


async def test_start_up_deletes_the_withdrawn_roles_makes_the_new_and_hands_out_quietly(game_db, old_roles,
                                                                                       earlier_made):
    game = game_db
    bot = game.bot
    slayer = bot.ids.mythic_slayer_role
    ala = game.add_member("Ala", roles=[PATRON, KILL_10, slayer])
    ola = game.add_member("Ola", roles=[HUNT_10])                        # does not support
    await own_pet(bot.db, ala.id, 5, mythic=True, holo=True)
    await own_pet(bot.db, ola.id, 6, mythic=True, holo=True)

    await bot.load_extension("altermmo.cogs.achievements")               # the real start_up
    (task,) = start_tasks()
    await asyncio.wait_for(task, 2)
    assert task.exception() is None
    assert not (set(old_roles) | {role_id for role_id, _ in EARLIER_MADE.values()}) & guild_role_ids(game)
    assert sorted(saved_roles(bot)) == ["album_full", "pet_mythic", "pet_mythic_holo", "pet_mythic_seal"]
    assert role_ids(ala) == {slayer, made_role(bot, "pet_mythic"), made_role(bot, "pet_mythic_holo")}
    assert role_ids(ola) == set() and announcements(game) == 0
    assert [line.split(" (")[0] for line in bot.owner_log] == [
        "Usunąłem z serwera role wycofanych osiągnięć", "Utworzyłem role osiągnięć",
        "Zaległe osiągnięcia: nadałem 2 ról 1 wspierającym.\n<@" + str(ala.id) + ">: Mityczny towarzysz, Mityczny Holo"]


@pytest.mark.parametrize("trouble", ["retire", "roles", "hand-out", "database"])
async def test_trouble_inside_start_up_never_escapes_the_task(game, old_roles, monkeypatch, caplog, trouble):
    bot = game.bot
    supporter = game.add_member("Ala", roles=[PATRON])

    async def broken(bot_):
        raise RuntimeError("test")

    if trouble == "retire":
        monkeypatch.setattr(achievements, "retire_roles", broken)
    elif trouble == "roles":
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
    # What came before the trouble was done.
    assert (set(old_roles) <= guild_role_ids(game)) is (trouble == "retire")
    assert len(bot.store.achievement_roles.load()) == (0 if trouble in ("retire", "roles") else 4)
    # The game goes on: the commands work.
    game.channel.type = discord.ChannelType.text
    ola = game.add_member("Ola")
    await run(game, ola, "$osiagniecia")
    assert answers(game.channel)[-1] == supporters_only_text(ola)
