"""The Zwój wskrzeszenia (items_decisions.md §5): ``!wskrzes @gracz`` takes the dead-hunter role away for one
scroll - altermmo/features/items.py (``revive``) and altermmo/cogs/items.py, against a real PostgreSQL and the
fake Discord."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from altermmo.core import challenge, guards, ui
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import items

from fakes import FakeChannel
from test_daily import make_ctx, run_checks
from test_items import stored


@pytest_asyncio.fixture
async def cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.items")
    return game_db.bot.get_cog("Przedmioty")


@pytest.fixture(autouse=True)
def nobody_is_being_revived():
    items._reviving.clear()
    yield
    assert not items._reviving, "a revival guard was never released"


def dead(game, member) -> None:
    """``member`` fell (the role every way of dying gives) and cannot write on the boss channel any more."""
    role_id = game.bot.ids.dead_hunter_role
    game.channel.silenced_roles.add(role_id)
    member.roles.append(game.guild.add_role(role_id))


def is_dead(game, member) -> bool:
    return any(role.id == game.bot.ids.dead_hunter_role for role in member.roles)


def mentions_spy(game) -> list:
    """The ``allowed_mentions`` of every message the bot sends to the boss channel from now on."""
    seen: list = []
    send = game.channel.send

    async def spy(content=None, **kwargs):
        seen.append(kwargs.get("allowed_mentions"))
        return await send(content, **kwargs)

    game.channel.send = spy
    return seen


async def test_a_scroll_revives_a_fallen_hunter_whatever_killed_him(game_db):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await items.grant(bot, ala.id, "revive_scroll", 2)
    dead(game_db, ola)
    allowed = mentions_spy(game_db)
    assert await items.revive(game_db.scene(ala), ola) is True
    assert not is_dead(game_db, ola) and await stored(bot.db, ala.id, "revive_scroll") == 1
    assert game_db.channel.messages[-1].content == (
        f"🕊️ <@{ala.id}> odczytuje nad <@{ola.id}> **Zwój wskrzeszenia**! Poległy łowca kicha, otrzepuje się "
        "z kurzu i wraca do gry.")
    # Owner rule: a player text mentions only the acting player - <@Ola> shows her name, without a ping.
    assert [user.id for user in allowed[-1].users] == [ala.id] and allowed[-1].everyone is False
    assert bot.owner_log == []
    # Back among the living: a second scroll would be wasted - it is not taken, and he is not pinged for it.
    assert await items.revive(game_db.scene(ala), ola) is False
    assert game_db.channel.messages[-1].content == f"<@{ola.id}> żyje i ma się dobrze, <@{ala.id}> - szkoda zwoju."
    assert [user.id for user in allowed[-1].users] == [ala.id]
    assert await stored(bot.db, ala.id, "revive_scroll") == 1


async def test_without_a_scroll_or_a_target_nothing_happens(game_db):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    dead(game_db, ola)
    assert await items.revive(game_db.scene(ala), ola) is False
    assert game_db.channel.messages[-1].content == (
        f"Nie masz żadnego **Zwoju wskrzeszenia**, <@{ala.id}>. Zwykle wypada z potworów i bossów - albo kupisz "
        "go u Wędrownego kupca.")
    assert is_dead(game_db, ola) and await stored(bot.db, ala.id, "revive_scroll") is None
    assert await items.revive(game_db.scene(ala), None) is False
    assert game_db.channel.messages[-1].content == f"Wskaż poległego łowcę, <@{ala.id}>: **!wskrzes @gracz**"
    # Without the item store nobody has a scroll.
    await items.grant(bot, ala.id, "revive_scroll")
    bot._player_items = (False, items.clock.monotonic())
    assert await items.revive(game_db.scene(ala), ola) is False
    assert game_db.channel.messages[-1].content.startswith("Nie masz żadnego **Zwoju wskrzeszenia**")
    assert is_dead(game_db, ola)


async def test_nobody_revives_himself(game_db):
    """A fallen hunter cannot write on the boss channel; one that gets a word through anyway changes nothing."""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await items.grant(bot, ala.id, "revive_scroll")
    dead(game_db, ala)
    before = len(game_db.channel.sent)
    assert await items.revive(game_db.scene(ala), ala) is False
    assert is_dead(game_db, ala) and len(game_db.channel.sent) == before
    assert await stored(bot.db, ala.id, "revive_scroll") == 1


async def test_two_scrolls_are_never_spent_on_one_hunter(game_db, monkeypatch):
    bot = game_db.bot
    ala, ela, ola = game_db.add_member("Ala"), game_db.add_member("Ela"), game_db.add_member("Ola")
    for reader in (ala, ela):
        await items.grant(bot, reader.id, "revive_scroll")
    dead(game_db, ola)
    gate = asyncio.Event()
    real_take = items.take

    async def slow_take(*args, **kwargs):
        await gate.wait()
        return await real_take(*args, **kwargs)

    monkeypatch.setattr(items, "take", slow_take)
    first = asyncio.ensure_future(items.revive(game_db.scene(ala), ola))
    await asyncio.sleep(0.01)
    assert await items.revive(game_db.scene(ela), ola) is False                 # while Ala reads hers
    assert game_db.channel.messages[-1].content == (
        f"Nad tym łowcą ktoś już odczytuje **Zwój wskrzeszenia**, <@{ela.id}> - Twój zostaje w sakwie.")
    gate.set()
    assert await first is True and not is_dead(game_db, ola)
    assert await stored(bot.db, ala.id, "revive_scroll") == 0 and await stored(bot.db, ela.id, "revive_scroll") == 1


async def test_a_hunter_revived_meanwhile_gives_the_scroll_back(game_db, monkeypatch):
    """The boss's reset (or anything else) brought him back while the scroll was being taken."""
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await items.grant(bot, ala.id, "revive_scroll")
    dead(game_db, ola)
    real_take = items.take

    async def take_and_reset(*args, **kwargs):
        left = await real_take(*args, **kwargs)
        ola.roles.clear()
        return left

    monkeypatch.setattr(items, "take", take_and_reset)
    assert await items.revive(game_db.scene(ala), ola) is False
    assert game_db.channel.messages[-1].content == f"<@{ola.id}> żyje i ma się dobrze, <@{ala.id}> - szkoda zwoju."
    assert await stored(bot.db, ala.id, "revive_scroll") == 1 and bot.owner_log == []


async def test_a_role_that_cannot_be_removed_gives_the_scroll_back_and_tells_the_owner(game_db, monkeypatch):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await items.grant(bot, ala.id, "revive_scroll")
    dead(game_db, ola)

    async def forbidden(*roles, reason=None):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    monkeypatch.setattr(ola, "remove_roles", forbidden)
    assert await items.revive(game_db.scene(ala), ola) is False
    assert is_dead(game_db, ola) and await stored(bot.db, ala.id, "revive_scroll") == 1
    assert game_db.channel.messages[-1].content == (
        f"Słowa zwoju rozwiały się bez echa, <@{ala.id}> <:Sadge:936907659142111273> Zwój wskrzeszenia wrócił do "
        "Twojej sakwy, a administracja dostała już znać.")
    assert bot.owner_log == [items.LOG_REVIVE_FAILED.format(target=ola.id, reviver=ala.id,
                                                            refund=items.LOG_REVIVE_REFUNDED)]


async def test_a_refund_that_fails_is_for_the_owner(game_db, monkeypatch):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await items.grant(bot, ala.id, "revive_scroll")
    dead(game_db, ola)

    async def forbidden(*roles, reason=None):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    async def no_grant(*args, **kwargs):
        raise DatabaseUnavailable("down")

    monkeypatch.setattr(ola, "remove_roles", forbidden)
    monkeypatch.setattr(items, "grant", no_grant)
    assert await items.revive(game_db.scene(ala), ola) is False
    assert bot.owner_log == [items.LOG_REVIVE_FAILED.format(
        target=ola.id, reviver=ala.id, refund=items.LOG_REVIVE_NOT_REFUNDED.format(error="DatabaseUnavailable"))]


async def test_a_take_the_database_may_or_may_not_have_done_gives_nothing_back(game_db, monkeypatch):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await items.grant(bot, ala.id, "revive_scroll")
    dead(game_db, ola)

    async def lost_reply(*args, **kwargs):
        raise DatabaseUnavailable("the reply was lost")

    monkeypatch.setattr(items, "take", lost_reply)
    assert await items.revive(game_db.scene(ala), ola) is False
    assert is_dead(game_db, ola) and game_db.channel.messages[-1].content == ui.MSG_FAILED
    assert bot.owner_log == [items.LOG_REVIVE_UNSURE.format(reviver=ala.id, error="DatabaseUnavailable")]
    assert await stored(bot.db, ala.id, "revive_scroll") == 1


async def test_the_command_is_a_gameplay_command(game_db, cog):
    bot = game_db.bot
    command = bot.get_command("wskrzes")
    assert command.cog_name == "Przedmioty" and set(command.aliases) == {"wskrzesz", "wskrzeszenie"}
    assert bot.get_command("wskrzeszenie") is command and len(command.checks) == 1     # (guards.in_order)
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await items.grant(bot, ala.id, "revive_scroll")
    dead(game_db, ola)
    # Only on the boss channel; silent during a typed fight; the busy notice while the channel is busy.
    elsewhere = FakeChannel(555, game_db.guild, bot.fake_user, name="inny")
    with bot.state.hold_busy("hunt"):
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, "!wskrzes", channel=elsewhere))
    assert elsewhere.sent == []                                                   # (no busy notice over there)
    with challenge.round_running(game_db.channel):
        before = len(game_db.channel.sent)
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, "!wskrzes"))
        assert len(game_db.channel.sent) == before
    with bot.state.hold_busy("event:PARTY"):
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, "!wskrzes"))
    assert game_db.channel.messages[-1].content == guards.BUSY_NOTICE
    ctx = make_ctx(game_db, ala, "!wskrzes")
    await run_checks(command, ctx)
    await command.callback(cog, ctx, ola)
    assert not is_dead(game_db, ola) and await stored(bot.db, ala.id, "revive_scroll") == 0


def test_the_help_and_the_items_view_name_the_command():
    from altermmo.cogs import help as help_cog

    assert "!wskrzes @gracz" in help_cog.HELP_TEXT and items.BY_KEY["revive_scroll"].hint == "**!wskrzes @gracz**"
