"""Buttons of the random events: forest, chest / shrine, sign-ups, memory, hazard, $zabojca."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import discord
import pytest

from altermmo.cogs import events as events_cog
from altermmo.core import timing, ui
from altermmo.core.state import EventType
from altermmo.features import events, patrons

from fakes import FakeInteraction


@pytest.fixture
def quick(monkeypatch):
    """Waiting times 100x shorter (debug 15 s -> 0.15 s)."""
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.01)


@pytest.fixture
def loot(monkeypatch):
    calls: list[tuple] = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        calls.append((rarity, hunter, boost_percent))
        return ""

    monkeypatch.setattr(events.daily, "rand_loot", rand_loot)
    return calls


@pytest.fixture
def hunt(monkeypatch):
    calls: list = []

    async def send_mob_image(scene, rarity):
        return False, None

    async def hunt_mobs(scene, rarity, is_player_boss, player_boss, player=None):
        calls.append(player)
        return True

    monkeypatch.setattr(events.daily, "send_mob_image", send_mob_image)
    monkeypatch.setattr(events.daily, "hunt_mobs", hunt_mobs)
    return calls


def dead_hunter(game):
    """A member banned by the "dead hunter" role (may not write on the boss channel)."""
    game.channel.silenced_roles.add(game.bot.ids.dead_hunter_role)
    return game.add_member("Duch", roles=[game.bot.ids.dead_hunter_role])


def not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


def live(message) -> bool:
    return ui.is_live(message.id) and message.view is not None and not message.view.is_finished()


# -- dark forest ---------------------------------------------------------------------

async def test_forest_is_one_panel_and_the_first_valid_click_enters(game, hunt):
    ala, ola, ghost = game.add_member("Ala"), game.add_member("Ola"), dead_hunter(game)
    task = asyncio.ensure_future(events.spawn_hunting(game.scene()))
    forest = await game.wait_for_buttons("join")
    embed = forest.embeds[0]
    assert forest.files == ["0.png"] and embed.image.url == "attachment://0.png"    # picture inside the panel
    assert forest.button_labels == [events.HUNTING_BUTTON] and forest.button("join").emoji.name == "🌲"
    assert "Czekam na śmiałka - czas mija <t:" in embed.description and embed.description.endswith(":R>.")
    refused = await game.click(forest, ghost, "join")
    assert refused.whispers == [ui.MSG_CANNOT_PLAY]
    first = await game.click(forest, ala, "join")
    late = await game.click(forest, ola, "join")
    assert await task is True
    assert hunt == [ala]
    assert first.response.deferred and first.whispers == []
    assert late.whispers == [events.MSG_TOO_LATE]            # clicked in the same moment: told privately
    assert forest.view is None and not ui.is_live(forest.id)
    assert events.MSG_FOREST_ENTERED.format(mention=ala.mention) in forest.text
    assert "<t:" not in forest.embeds[0].description and forest.embeds[0].image.url == "attachment://0.png"


async def test_forest_nobody_comes_buttons_go_away(game, hunt, quick):
    assert await events.spawn_hunting(game.scene()) is False
    forest = next(m for m in game.channel.sent if "Mroczny las!" in m.text)
    assert forest.view is None and not ui.is_live(forest.id)
    assert "<t:" not in forest.embeds[0].description
    assert game.channel.contains(events.MSG_HUNTING_NOBODY) and hunt == []


async def test_forest_panel_deleted_in_the_middle(game, hunt, quick):
    async def gone(**kwargs):
        raise not_found()

    def purge(message) -> None:
        if "Mroczny las!" in message.text:
            message.edit = gone
            asyncio.ensure_future(message.delete())

    game.channel.send_hooks.append(purge)
    assert await events.spawn_hunting(game.scene()) is False
    assert game.channel.contains(events.MSG_HUNTING_NOBODY)


# -- chest and shrine (permanent buttons through the cog) ----------------------------

@pytest.fixture
def cog(game):
    cog = events_cog.EventsCog(game.bot)
    cog.register_views()
    return cog


def ctx_of(game, member, content):
    message = game.say(member, content)
    return SimpleNamespace(bot=game.bot, channel=game.channel, author=member, message=message)


async def test_chest_button_opens_it_once(game, cog, loot):
    ala, ola, ghost = game.add_member("Ala"), game.add_member("Ola"), dead_hunter(game)
    assert await cog.run_one_event(forced=EventType.CHEST) is EventType.CHEST
    chest = game.channel.messages[-1]
    assert chest.button_labels == [events_cog.CHEST_BUTTON]
    assert chest.button(events_cog.CHEST_BUTTON_ID).custom_id == "altermmo:events:chest"
    assert chest.view.is_finished()                       # the registered view answers the clicks

    assert (await game.click(chest, ghost, events_cog.CHEST_BUTTON_ID)).whispers == [ui.MSG_CANNOT_PLAY]
    opened = await game.click(chest, ala, events_cog.CHEST_BUTTON_ID)
    assert opened.response.deferred and opened.whispers == []
    assert [call[1] for call in loot] == [ala]
    assert chest.view is None and events_cog.MSG_CHEST_OPENED.format(mention=ala.mention) in chest.text
    assert not game.bot.state.event_alive and game.bot.state.free_for_activity

    too_late = FakeInteraction(game.bot, ola, chest, channel=game.channel)  # a client that did not refresh yet
    await cog.passive_clicked(too_late, EventType.CHEST)
    assert too_late.whispers == [events_cog.MSG_NO_CHEST] and len(loot) == 1
    assert not game.channel.ever_sent(events_cog.MSG_NO_CHEST)           # refusals are private


async def test_chest_command_still_works_and_takes_the_button_away(game, cog, loot):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]
    await cog.open_chest.callback(cog, ctx_of(game, ala, "$skrzynia"))
    assert [call[1] for call in loot] == [ala]
    assert chest.view is None and ala.mention in chest.text
    await cog.open_chest.callback(cog, ctx_of(game, ola, "$skrzynia"))
    assert game.channel.messages[-1].content == events_cog.MSG_NO_CHEST and len(loot) == 1


async def test_chest_button_works_while_a_hunt_runs_like_the_command(game, cog, loot):
    ala = game.add_member("Ala")
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]
    with game.bot.state.hold_busy("hunt"):
        await game.click(chest, ala, events_cog.CHEST_BUTTON_ID)
    assert [call[1] for call in loot] == [ala]


async def test_chest_that_fails_to_open_stays(game, cog, monkeypatch):
    ala = game.add_member("Ala")
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]

    async def broken(scene, rarity, hunter, boost):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(events.daily, "rand_loot", broken)
    interaction = await game.click(chest, ala, events_cog.CHEST_BUTTON_ID)
    assert interaction.whispers == [ui.MSG_FAILED]
    assert game.bot.state.event_alive and game.bot.state.event_type is EventType.CHEST
    assert chest.button_labels == [events_cog.CHEST_BUTTON]


async def test_unclaimed_chest_loses_its_button_when_the_next_event_comes(game, cog, hunt, quick):
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]
    assert chest.button_labels
    await cog.run_one_event(forced=EventType.HUNTING)
    assert chest.view is None and not game.bot.state.event_alive


async def test_old_panel_button_after_a_restart(game, cog, loot):
    """The cog does not know the panel (the bot was restarted): refused, the button goes."""
    ala = game.add_member("Ala")
    old_view = events_cog.ChestView(cog)
    old = await game.channel.send(embed=discord.Embed(title="Skrzynia"), view=old_view)
    old_view.stop()
    interaction = await game.click(old, ala, events_cog.CHEST_BUTTON_ID)
    assert interaction.whispers == [events_cog.MSG_NO_CHEST] and old.view is None and loot == []


async def test_chest_button_elsewhere_is_refused(game, cog, loot):
    ala = game.add_member("Ala")
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]
    interaction = FakeInteraction(game.bot, ala, chest, channel=game.notice_channel)
    await cog.passive_clicked(interaction, EventType.CHEST)
    assert interaction.whispers == [ui.MSG_WRONG_CHANNEL.format(channel_id=game.bot.ids.boss_channel)]
    assert loot == [] and game.bot.state.event_alive


async def test_only_believers_pray_with_the_button(game, cog, monkeypatch):
    monkeypatch.setattr(random, "choice", lambda pool: "points_boost")
    ala = game.add_member("Ala")
    patron = game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    await cog.run_one_event(forced=EventType.SHRINE)
    shrine = game.channel.messages[-1]
    assert shrine.button_labels == [events_cog.SHRINE_BUTTON]

    refused = await game.click(shrine, ala, events_cog.SHRINE_BUTTON_ID)
    assert refused.whispers == [events_cog.MSG_NOT_A_BELIEVER] and shrine.button_labels
    assert game.bot.state.event_alive

    prayed = await game.click(shrine, patron, events_cog.SHRINE_BUTTON_ID)
    assert prayed.response.deferred
    assert (await events.modifiers.load_modifiers(game.bot))["points_boost"] > 0
    assert game.channel.contains("Błogosławieństwo kapliczki!")
    assert shrine.view is None and patron.mention in shrine.text
    assert not game.bot.state.event_alive


async def test_pray_command_still_works_and_takes_the_button_away(game, cog, monkeypatch):
    monkeypatch.setattr(random, "choice", lambda pool: "points_boost")
    patron = game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    ala = game.add_member("Ala")
    await cog.run_one_event(forced=EventType.SHRINE)
    shrine = game.channel.messages[-1]
    await cog.pray.callback(cog, ctx_of(game, ala, "$modlitwa"))
    assert game.channel.messages[-1].content == events_cog.MSG_NOT_A_BELIEVER and shrine.button_labels
    ctx = ctx_of(game, patron, "$modlitwa")
    await cog.pray.callback(cog, ctx)
    assert ctx.message.reactions == [events_cog.PRAYGE]
    assert shrine.view is None and not game.bot.state.event_alive
    await cog.pray.callback(cog, ctx_of(game, patron, "$modlitwa"))
    assert game.channel.messages[-1].content == events_cog.MSG_NO_SHRINE


# -- sign-ups ------------------------------------------------------------------------

async def test_invasion_roster_is_live_throttled_and_counts_everybody_once(game, loot, monkeypatch):
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.06)                  # debug 5 s -> 0.3 s
    monkeypatch.setattr(random, "randint", lambda low, high: low)
    members = [game.add_member(f"Gracz{index}") for index in range(5)]
    ghost = dead_hunter(game)
    task = asyncio.ensure_future(events.spawn_invasion(game.scene()))
    board = await game.wait_for_buttons("join")
    assert board.button_labels == [events.JOIN_LABEL]
    assert "Zapisy kończą się <t:" in board.embeds[0].description and events.ROSTER_EMPTY in board.text
    clicks = [await game.click(board, member, "join") for member in members]
    again = await game.click(board, members[0], "join")
    ghostly = await game.click(board, ghost, "join")
    await asyncio.sleep(0.05)
    assert "Zapisani (5)" in board.text                                 # repainted while open
    await task
    assert all(click.response.deferred for click in clicks)
    assert again.whispers == [events.MSG_ALREADY_SIGNED] and ghostly.whispers == [ui.MSG_CANNOT_PLAY]
    assert [call[1] for call in loot] == members
    painted = [edit for edit in board.edits if "embed" in edit]
    assert 2 <= len(painted) <= 3                                       # never one edit per click
    assert events.MSG_SIGN_UP_CLOSED in board.text and "<t:" not in board.embeds[0].description
    assert all(member.mention in board.text for member in members) and ghost.mention not in board.text
    assert board.view is None and not ui.is_live(board.id)


async def test_long_roster_is_shortened(game):
    sign_up = events._SignUp(game.scene(), discord.Embed(title="x", description="y"), emoji="⚔️")
    sign_up.members = [game.add_member(f"G{index}") for index in range(events.ROSTER_SHOWN + 3)]
    field = sign_up.render().fields[0]
    assert field.name == f"Zapisani ({events.ROSTER_SHOWN + 3})" and field.value.endswith("...i 3 więcej")
    assert len(field.value) <= 1024


async def test_tournament_sign_up_panel(game, loot, quick, monkeypatch):
    async def duel_fight(scene, participants, ban=True):
        return True, participants[0]

    monkeypatch.setattr(events.boss, "duel_fight", duel_fight)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    task = asyncio.ensure_future(events.spawn_pvp_tournament(game.scene()))
    board = await game.wait_for_buttons("join")
    await game.click(board, ala, "join")
    await game.click(board, ola, "join")
    assert await task in (ala, ola)
    assert "Zapisani (2)" in board.text and board.view is None
    assert not game.channel.ever_sent("dołączył do turnieju")           # the roster replaces those messages


async def test_duel_challenger_gives_the_others_more_time(game, loot, monkeypatch):
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.04)                  # debug 15 s -> 0.6 s
    fights = []

    async def duel_fight(scene, participants, ban=True):
        fights.append((list(participants), game.bot.state.busy_owner))
        return True, participants[0]

    monkeypatch.setattr(events.boss, "duel_fight", duel_fight)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    busy_during_sign_up = []
    task = asyncio.ensure_future(events.spawn_duel(game.scene()))
    arena = await game.wait_for_buttons("join")
    await asyncio.sleep(0.4)
    await game.click(arena, ala, "join")
    busy_during_sign_up.append(game.bot.state.busy)
    await asyncio.sleep(0.45)                                            # past the first deadline (0.6 s)
    assert live(arena) and "Do walki stają (1/2)" in arena.text
    await game.click(arena, ola, "join")
    assert await task is True
    assert fights == [([ala, ola], "event:DUEL")] and busy_during_sign_up == [False]
    assert not game.bot.state.busy and arena.view is None


async def test_duel_waits_for_a_running_hunt_before_the_fight(game, loot, monkeypatch):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    order = []

    async def duel_fight(scene, participants, ban=True):
        order.append("fight")
        return True, participants[0]

    async def hunt() -> None:
        with game.bot.state.hold_busy("hunt"):
            await asyncio.sleep(0.3)
            order.append("hunt over")

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *args, **kwargs):
        await real_sleep(min(seconds, 0.05))

    monkeypatch.setattr(events.boss, "duel_fight", duel_fight)
    monkeypatch.setattr(events.asyncio, "sleep", fast_sleep)
    task = asyncio.ensure_future(events.spawn_duel(game.scene()))
    arena = await game.wait_for_buttons("join")
    asyncio.ensure_future(hunt())
    await game.click(arena, ala, "join")
    await game.click(arena, ola, "join")
    assert await task is True
    assert order == ["hunt over", "fight"]


# -- memory and hazard ---------------------------------------------------------------

async def test_memory_runes_are_ten_buttons_only_the_volunteer_may_press(game, loot, monkeypatch):
    monkeypatch.setattr(random, "choice", lambda pool: list(pool)[0])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    task = asyncio.ensure_future(events.spawn_memory(game.scene()))
    board = await game.wait_for_buttons("join")
    assert board.button_labels == ["Pomóż Sanczo"]
    await game.click(board, ala, "join")
    question = await game.wait_for_buttons(events.rune_key(0))
    assert len(question.view.children) == 10
    assert [str(item.emoji) for item in question.view.children] == list(events.MEMORY_EMOTES)
    assert (await game.click(question, ola, events.rune_key(0))).whispers == [
        ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    for index in range(4):
        await game.click(question, ala, events.rune_key(index))
    assert await task is True
    assert question.view is None and board.view is None


async def test_memory_survives_a_double_click_on_a_correct_rune(game, loot, monkeypatch):
    monkeypatch.setattr(random, "choice", lambda pool: list(pool)[0])     # runes 0, 1, 2, 3
    ala = game.add_member("Ala")
    task = asyncio.ensure_future(events.spawn_memory(game.scene()))
    board = await game.wait_for_buttons("join")
    await game.click(board, ala, "join")
    question = await game.wait_for_buttons(events.rune_key(0))
    # Both taps reach the bot before it switched the rune off.
    first, second = await asyncio.gather(game.click(question, ala, events.rune_key(0)),
                                         game.click(question, ala, events.rune_key(0)))
    assert first.response.deferred and second.response.deferred
    assert first.whispers == [] and second.whispers == []
    for index in range(1, 4):
        await game.click(question, ala, events.rune_key(index))
    assert await task is True
    assert game.channel.count("*Dobrze!* - Sanczo dopinguje.") == 4
    assert not game.channel.contains(events.MSG_MEMORY_WRONG) and [call[1] for call in loot] == [ala]


async def test_volunteer_double_click_is_not_told_somebody_was_faster(game, hunt, loot, monkeypatch):
    monkeypatch.setattr(random, "choice", lambda pool: list(pool)[0])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    task = asyncio.ensure_future(events.spawn_memory(game.scene()))
    board = await game.wait_for_buttons("join")
    first, second, late = await asyncio.gather(game.click(board, ala, "join"), game.click(board, ala, "join"),
                                               game.click(board, ola, "join"))
    assert first.whispers == [] and second.whispers == []               # Ala got in: no "Ktoś był szybszy"
    assert first.response.deferred and second.response.deferred
    assert late.whispers == [events.MSG_TOO_LATE]                       # Ola really was too late
    question = await game.wait_for_buttons(events.rune_key(0))
    for index in range(4):
        await game.click(question, ala, events.rune_key(index))
    assert await task is True

    task = asyncio.ensure_future(events.spawn_hunting(game.scene()))
    forest = await game.wait_for_buttons("join")
    first, second = await asyncio.gather(game.click(forest, ola, "join"), game.click(forest, ola, "join"))
    assert await task is True and hunt == [ola]
    assert first.whispers == [] and second.whispers == []


async def test_hazard_second_cup_of_the_player_is_not_told_somebody_was_faster(game_db, loot, monkeypatch):
    game = game_db
    monkeypatch.setattr(random, "choice", lambda pool: list(pool)[1])     # the right cup is lucky
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await game.bot.db.execute(
        "INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED, REROLL_SCROLL) VALUES ($1, 0, FALSE, 9)", ala.id)
    task = asyncio.ensure_future(events.spawn_hazard(game.scene()))
    cups = await game.wait_for_buttons(events.cup_key(0))
    first, second, late = await asyncio.gather(game.click(cups, ala, events.cup_key(1)),
                                               game.click(cups, ala, events.cup_key(0)),
                                               game.click(cups, ola, events.cup_key(0)))
    assert await task is True                                           # the first cup counts, once
    assert first.whispers == [] and second.whispers == []
    assert late.whispers == [events.MSG_TOO_LATE]
    scrolls = await game.bot.db.fetchval("SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = $1", ala.id)
    assert scrolls == 9 + events.HAZARD_STAKE


async def test_hazard_cups_are_two_buttons(game_db, loot, monkeypatch):
    game = game_db
    monkeypatch.setattr(random, "choice", lambda pool: list(pool)[1])     # the right cup is lucky
    ala, ghost = game.add_member("Ala"), dead_hunter(game)
    await game.bot.db.execute(
        "INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED, REROLL_SCROLL) VALUES ($1, 0, FALSE, 9)", ala.id)
    task = asyncio.ensure_future(events.spawn_hazard(game.scene()))
    cups = await game.wait_for_buttons(events.cup_key(0))
    assert cups.button_labels == list(events.HAZARD_LABELS)
    assert (await game.click(cups, ghost, events.cup_key(1))).whispers == [ui.MSG_CANNOT_PLAY]
    await game.click(cups, ala, events.cup_key(1))
    assert await task is True
    assert cups.view is None and [call[1] for call in loot] == [ala]


# -- $zabojca ------------------------------------------------------------------------

async def _assassin_ready(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await game.bot.db.execute(
        "INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED, REROLL_SCROLL) VALUES ($1, 0, FALSE, 30)", ala.id)
    await game.bot.db.execute("INSERT INTO RANKING (ID, NICK, POINTS) VALUES ($1, 'Ola', 10)", str(ola.id))
    return ala, ola


async def test_assassin_is_confirmed_with_a_button(game_db, monkeypatch):
    game = game_db
    monkeypatch.setattr(random, "random", lambda: 0.5)
    ala, ola = await _assassin_ready(game)
    task = asyncio.ensure_future(events.assasinate(game.scene(ala, "$zabojca")))
    question = await game.wait_for_buttons("yes")
    assert "Kliknij **Potwierdzam**" in question.content and question.button_labels == ["Potwierdzam", "Anuluj"]
    assert (await game.click(question, ola, "yes")).whispers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    await game.click(question, ala, "yes")
    assert await task is True
    assert question.view is None
    assert game.channel.contains(f"Ofiarą został <@{ola.id}>")


async def test_assassin_cancelled_with_a_button(game_db):
    game = game_db
    ala, _ola = await _assassin_ready(game)
    task = asyncio.ensure_future(events.assasinate(game.scene(ala, "$zabojca")))
    question = await game.wait_for_buttons("no")
    await game.click(question, ala, "no")
    assert await task is False
    assert game.channel.contains(events.MSG_ASSASSIN_NOT_CONFIRMED) and question.view is None
    value = await game.bot.db.fetchval("SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = $1", ala.id)
    assert int(value) == 30
