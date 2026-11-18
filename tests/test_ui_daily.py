"""Panels of the hunting area: the hunt's join button, live reset times, the gem pouch menu, tower clicks."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from types import SimpleNamespace

import discord
import pytest

from altermmo.cogs import schedule
from altermmo.core import clock, guards, ui
from altermmo.core.scene import Scene
from altermmo.core.state import BossPhase
from altermmo.features import achievements, daily, database, hub, skills, tower

from fakes import FakeInteraction, requested_word

ZERO = {"CRIT_PERC": 0.0, "REPLACE_PERC": 0.0, "DEF_PERC": 0.0, "DROP_PERC": 0.0,
        "LOWHP_PERC": 0.0, "SLOW_PERC": 0.0}
JOIN = "hunt-join"


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    monkeypatch.setattr(guards, "_last_notice", {})
    daily._gathering.clear()
    daily._panels.clear()
    yield
    daily._gathering.clear()
    daily._panels.clear()


@pytest.fixture
def quiet_hunt(monkeypatch):
    """A hunt without database or loot: 2 words to win, no pets, nothing to hand out."""

    async def zero(bot, player_id):
        return dict(ZERO)

    async def nothing(*args, **kwargs):
        return ""

    monkeypatch.setattr(daily, "pet_skills", zero)
    monkeypatch.setattr(daily, "rand_loot", nothing)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)
    monkeypatch.setattr(database, "update_ranking_table", nothing)
    monkeypatch.setattr(achievements, "add_hunts", nothing)


def team_answers(game, players) -> None:
    """Everybody answers the prompts addressed to them (``N. **Name**: ...``)."""
    by_name = {player.name: player for player in players}

    def hook(message):
        match = re.match(r"^\d+\. \*\*(.+?)\*\*: ", message.content)
        word = requested_word(message.content)
        if match is not None and word is not None:
            game.say_later(by_name[match.group(1)], word, 0)

    game.channel.send_hooks.append(hook)


async def until(condition, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the expected thing never happened"
        await asyncio.sleep(0.005)


def roster(message) -> tuple[str, str]:
    (field,) = message.embeds[0].fields
    return field.name, field.value


def whisperer(game, member, channel=None) -> FakeInteraction:
    """A click of ``member`` on some button elsewhere (the hub panel): answers are private."""
    return FakeInteraction(game.bot, member, channel=channel or game.channel)


def gone(message) -> None:
    """The message was purged: every later edit fails like Discord's ``NotFound``."""
    async def edit(**kwargs):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown message")

    message.edit = edit


async def start_gathering(game, starter, rarity: int = 0):
    """Show the monster and start the hunt of ``starter``; returns (the monster's card, the hunt task)."""
    await daily.send_mob_image(game.scene(starter), rarity)
    card = game.last_with_buttons()
    task = asyncio.ensure_future(daily.hunt_mobs(game.scene(starter), rarity, False, None))
    await until(lambda: daily.is_gathering(game.channel))
    return card, task


# -- the hunt: "Dołącz do polowania" -------------------------------------------------------

async def test_button_and_typed_word_fill_the_hunt_and_the_card_shows_the_hunters(game, quiet_hunt, monkeypatch):
    bot = game.bot
    bot.debug = False
    monkeypatch.setattr(daily, "ROSTER_EDIT_GAP", 0.01)
    ala, ola, ela, late = (game.add_member(name) for name in ("Ala", "Ola", "Ela", "Spozniony"))
    dead = game.add_member("Polegly", roles=[bot.ids.dead_hunter_role])
    game.channel.silenced_roles.add(bot.ids.dead_hunter_role)
    team_answers(game, [ala, ola, ela])

    card, task = await start_gathering(game, ala)
    assert card.button_labels == ["Dołącz do polowania"] and card.button(JOIN).emoji.name == "⚔️"
    await until(lambda: card.embeds[0].fields)                          # the list appears with the deadline
    name, value = roster(card)
    assert name == "Łowcy (1/3)" and value.startswith(f"<@{ala.id}>\n⏳ Dołączanie kończy się <t:")

    starter = await game.click(card, ala, JOIN)                         # the starter is already in
    refused = await game.click(card, dead, JOIN)                        # a dead hunter cannot join
    assert refused.whispers == [ui.MSG_CANNOT_PLAY]
    joined = await game.click(card, ola, JOIN)
    assert joined.response.deferred
    await until(lambda: starter.whispers == [daily.MSG_ALREADY_HUNTING])
    await until(lambda: roster(card)[0] == "Łowcy (2/3)")
    again = await game.click(card, ola, JOIN)                           # counted once
    await until(lambda: again.whispers == [daily.MSG_ALREADY_HUNTING])
    assert roster(card)[1].startswith(f"<@{ala.id}>, <@{ola.id}>\n")

    game.say(ela, "$polowanie")                                         # the typed way still works
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> <@{ela.id}> <:ok:990161663053422592>")

    assert card.view is None                                            # the button is gone
    assert roster(card) == ("Łowcy (3/3)", f"<@{ala.id}>, <@{ola.id}>, <@{ela.id}>")
    assert daily.MSG_JOIN_HINT not in card.embeds[0].description and "⏳" not in roster(card)[1]
    assert not ui.is_live(card.id) and not daily.is_gathering(game.channel) and daily._panels == {}
    assert daily.request_join(game.channel, late) is False             # nothing gathers any more


async def test_nobody_joins_the_card_shows_the_lone_hunter(game, quiet_hunt):
    ala = game.add_member("Ala")
    game.auto_answer(ala)
    card, task = await start_gathering(game, ala)
    assert await task is True
    assert card.view is None and roster(card) == ("Łowcy (1/3)", f"<@{ala.id}>")
    assert game.channel.contains(f"Polowanie się rozpoczyna <@{ala.id}> ")


async def test_a_click_right_after_the_monster_appears_is_not_lost(game, quiet_hunt):
    game.bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    await daily.send_mob_image(game.scene(ala), 1)
    card = game.last_with_buttons()
    await game.click(card, ola, JOIN)                                   # before hunt_mobs listens
    assert await daily.hunt_mobs(game.scene(ala), 1, False, None) is True
    assert game.channel.contains(f"Rozpoczęliście polowanie <@{ala.id}> <@{ola.id}> ")


async def test_three_quick_clicks_edit_the_card_about_once_a_second(game, quiet_hunt):
    game.bot.debug = False
    ala, ola, ela = (game.add_member(name) for name in ("Ala", "Ola", "Ela"))
    team_answers(game, [ala, ola, ela])
    card, task = await start_gathering(game, ala)
    await game.click(card, ola, JOIN)
    await game.click(card, ela, JOIN)                                   # the party is full: the hunt starts
    assert await task is True
    assert 1 <= len(card.edits) <= 2                                    # one repaint at most, then the final list
    assert roster(card)[0] == "Łowcy (3/3)" and card.view is None


async def test_debug_mode_lets_one_tester_fill_the_party_with_the_button(game, quiet_hunt):
    assert game.bot.debug
    ala = game.add_member("Ala")
    game.auto_answer(ala)
    card, task = await start_gathering(game, ala)
    await game.click(card, ala, JOIN)
    await game.click(card, ala, JOIN)
    assert await task is True
    assert game.channel.contains(f"Rozpoczęliście polowanie <@{ala.id}> <@{ala.id}> <@{ala.id}> ")


async def test_the_card_purged_while_gathering_does_not_stop_the_hunt(game, quiet_hunt):
    game.bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    card, task = await start_gathering(game, ala)
    await card.delete()
    gone(card)
    game.say(ola, "$polowanie")
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> ")
    assert not ui.is_live(card.id)


def slow(monkeypatch, name: str, seconds: float) -> None:
    """Every ``ui.<name>`` (``ack`` / ``reply``) takes a round trip of ``seconds`` before it acts."""
    real = getattr(ui, name)

    async def delayed(interaction, *args, **kwargs):
        await asyncio.sleep(seconds)
        return await real(interaction, *args, **kwargs)

    monkeypatch.setattr(ui, name, delayed)


async def test_repeated_clicks_of_a_hunter_do_not_push_a_joiner_past_the_deadline(game, quiet_hunt, monkeypatch):
    """Review: each "already hunting" answer was awaited inside the join window."""
    game.bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.4)
    slow(monkeypatch, "reply", 0.15)                                    # every private answer: 150 ms
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    card, task = await start_gathering(game, ala)
    repeats = [game.click_later(card, ala, JOIN) for _ in range(4)]    # the starter clicks again and again
    joined = game.click_later(card, ola, JOIN)                          # Ola clicks at the same moment
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> ")
    assert (await joined).whispers == [] and (await joined).response.deferred
    for click in repeats:
        assert (await click).whispers == [daily.MSG_ALREADY_HUNTING]


async def test_clicks_queued_before_the_hunt_listens_are_refused_without_delaying_it(game, quiet_hunt, monkeypatch):
    """Repeated clicks made before the gathering knows its hunters are refused in the background."""
    game.bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.4)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    await daily.send_mob_image(game.scene(ala), 0)
    card = game.last_with_buttons()
    repeats = [await game.click(card, ala, JOIN) for _ in range(4)]     # queued: nobody hunts yet
    joined = await game.click(card, ola, JOIN)
    slow(monkeypatch, "reply", 0.15)
    assert await daily.hunt_mobs(game.scene(ala), 0, False, None) is True
    assert game.channel.contains(f"Rozpoczęliście polowanie <@{ala.id}> <@{ola.id}> ")
    assert joined.whispers == []
    for click in repeats:
        await until(lambda c=click: c.whispers == [daily.MSG_ALREADY_HUNTING])


async def test_a_click_counts_from_the_moment_it_is_made_not_after_its_acknowledgement(game, quiet_hunt, monkeypatch):
    """Review: the click was queued only after the round trip of its acknowledgement."""
    game.bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.4)
    slow(monkeypatch, "ack", 0.2)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    card, task = await start_gathering(game, ala)
    await asyncio.sleep(0.25)                                           # 0.15 s before the window closes
    click = await game.click(card, ola, JOIN)                           # acknowledged only after it closed
    assert click.response.deferred and click.whispers == []
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> ")


async def test_a_click_left_over_is_told_too_late_only_after_its_acknowledgement(game, quiet_hunt, monkeypatch):
    """A queued click whose acknowledgement is still on its way gets "too late" after it, not instead."""
    game.bot.debug = False
    slow(monkeypatch, "ack", 0.2)
    ala, ola, ela, iza = (game.add_member(name) for name in ("Ala", "Ola", "Ela", "Iza"))
    team_answers(game, [ala, ola, ela])
    card, task = await start_gathering(game, ala)
    clicks = [game.click_later(card, member, JOIN) for member in (ola, ela, iza)]   # one too many
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> <@{ela.id}> ")
    ola_click, ela_click, iza_click = [await click for click in clicks]
    assert ola_click.whispers == [] and ela_click.whispers == []
    assert iza_click.response.deferred and iza_click.whispers == [daily.MSG_JOIN_TOO_LATE]


async def test_a_hunt_without_a_card_still_takes_typed_joins(game, quiet_hunt):
    """Events may call hunt_mobs without the monster's card on this channel: typing keeps working."""
    game.bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    game.say_later(ola, "$polowanie", 0.02)
    assert await daily.hunt_mobs(game.scene(ala), 0, False, None) is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> ")


# -- $polowanie as one function (for the hub panel) ----------------------------------------

async def test_start_hunt_from_a_click_refuses_privately(game, monkeypatch):
    bot = game.bot
    bot.debug = False
    ala = game.add_member("Ala")
    hunts: list = []

    async def fake_hunt(scene):
        hunts.append((scene.author, bot.state.busy_owner))

    monkeypatch.setattr(daily, "daily_hunt", fake_hunt)

    async def start() -> FakeInteraction:
        click = whisperer(game, ala)
        await daily.start_hunt(ui.scene_of(click), refuse=lambda text: ui.reply(click, text))
        return click

    assert (await start()).replies == [] and hunts == [(ala, "hunt")] and not bot.state.busy

    daily.save_daily_to_file(bot, ala.id)
    stamp = int(daily.next_daily_reset().timestamp())
    assert (await start()).whispers == [daily.MSG_ON_COOLDOWN.format(when=f"<t:{stamp}:R>")]

    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    assert (await start()).whispers == [daily.MSG_BOSS_COMING]
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    assert (await start()).whispers == [daily.MSG_BOSS_HERE]
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    assert (await start()).whispers == [daily.MSG_BOSS_FIGHT]
    await bot.state.set_boss_phase(BossPhase.WAITING)

    with bot.state.hold_busy("event:PARTY"):
        assert (await start()).whispers == [ui.MSG_BUSY]
    assert len(hunts) == 1 and game.channel.bot_messages == game.channel.bot_messages[:1]  # nothing public


async def test_start_hunt_from_a_click_joins_a_gathering_hunt(game, quiet_hunt):
    game.bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    card, task = await start_gathering(game, ala)
    for member in (ola, ala):
        click = whisperer(game, member)
        await ui.ack(click)
        assert await daily.start_hunt(ui.scene_of(click), refuse=lambda text, c=click: ui.reply(c, text)) is False
        if member is ala:
            await until(lambda: click.whispers == [daily.MSG_ALREADY_HUNTING])
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> ")


async def test_hub_hunt_click_joins_a_real_gathering_hunt(game, quiet_hunt):
    """The "Polowanie" button of the hub panel, with nothing of the gathering replaced."""
    game.bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala, ola])
    card, task = await start_gathering(game, ala)
    own = whisperer(game, ala)
    assert await hub.hunt_clicked(own) is False                         # the starter: told at once, privately
    assert own.whispers == [daily.MSG_ALREADY_HUNTING]
    click = whisperer(game, ola)
    assert await hub.hunt_clicked(click) is False                       # joined; the hunt is the starter's
    await until(lambda: roster(card)[0] == "Łowcy (2/3)")
    assert click.response.deferred and click.replies == []
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> ")
    assert click.replies == [] and roster(card) == ("Łowcy (2/3)", f"<@{ala.id}>, <@{ola.id}>")


async def test_hub_hunt_click_after_the_gathering_is_refused_privately(game, quiet_hunt):
    game.bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala])
    with game.bot.state.hold_busy("hunt"):                              # as start_hunt holds it
        card, task = await start_gathering(game, ala)
        await until(lambda: not daily.is_gathering(game.channel))      # the hunt has started
        click = whisperer(game, ola)
        assert await hub.hunt_clicked(click) is False
        assert click.whispers == [ui.MSG_BUSY]
        assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> ") and roster(card) == ("Łowcy (1/3)", f"<@{ala.id}>")


@pytest.mark.parametrize("with_card", [True, False])
async def test_hub_hunt_click_right_at_the_deadline_is_told_too_late(game, quiet_hunt, monkeypatch, with_card):
    """A click that reaches the queue when nobody takes requests any more still gets an answer."""
    game.bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    team_answers(game, [ala])
    click = whisperer(game, ola)
    real_next_joiner = daily._next_joiner

    async def next_joiner(*args, **kwargs):
        joiner = await real_next_joiner(*args, **kwargs)
        if joiner is None:                                              # the window has just closed
            assert await hub.hunt_clicked(click) is False
        return joiner

    monkeypatch.setattr(daily, "_next_joiner", next_joiner)
    if with_card:
        card, task = await start_gathering(game, ala)
    else:
        task = asyncio.ensure_future(daily.hunt_mobs(game.scene(ala), 0, False, None))
    assert await task is True
    assert game.channel.contains(f"Brawo <@{ala.id}> ")
    assert click.response.deferred and click.whispers == [daily.MSG_JOIN_TOO_LATE]


async def test_typed_polowanie_answers_on_the_channel_with_the_live_reset_time(game):
    bot = game.bot
    ala = game.add_member("Ala")
    daily.save_daily_to_file(bot, ala.id)
    assert await daily.start_hunt(game.scene(ala)) is False
    content = game.channel.messages[-1].content
    assert content == daily.cooldown_text() and f"<t:{int(daily.next_daily_reset().timestamp())}:R>" in content


# -- the moments of the resets -------------------------------------------------------------

def _warsaw(*args) -> datetime:
    return datetime(*args, tzinfo=clock.WARSAW)


@pytest.mark.parametrize("now, daily_reset, season_end", [
    (_warsaw(2026, 11, 18, 12, 0), _warsaw(2026, 11, 19, 2, 30), _warsaw(2026, 11, 23, 15, 0)),   # Wednesday
    (_warsaw(2026, 11, 19, 1, 0), _warsaw(2026, 11, 19, 2, 30), _warsaw(2026, 11, 23, 15, 0)),    # before 02:30
    (_warsaw(2026, 11, 23, 14, 0), _warsaw(2026, 11, 23, 15, 0), _warsaw(2026, 11, 23, 15, 0)),   # Monday 14:00
    (_warsaw(2026, 11, 23, 15, 0), _warsaw(2026, 11, 24, 2, 30), _warsaw(2026, 11, 30, 15, 0)),   # at the end
    (_warsaw(2026, 3, 28, 12, 0), _warsaw(2026, 3, 29, 2, 30), _warsaw(2026, 3, 30, 15, 0)),      # summer time
])
def test_next_resets(now, daily_reset, season_end):
    assert daily.next_daily_reset(now) == daily_reset
    assert daily.next_season_end(now) == season_end


async def test_reset_moments_are_the_ones_of_the_schedule(monkeypatch):
    """The texts promise what cogs/schedule.py really does."""
    seen: dict = {}

    def weekly(name, weekday, hour, minute, run, **kwargs):
        seen[name] = (weekday, hour, minute)
        return SimpleNamespace(name=name)

    def every_day(name, hour, minute, run, **kwargs):
        seen[name] = (hour, minute)
        return SimpleNamespace(name=name)

    monkeypatch.setattr(schedule.scheduler, "weekly", weekly)
    monkeypatch.setattr(schedule.scheduler, "daily", every_day)
    cog = schedule.ScheduleCog(SimpleNamespace(supervisor=SimpleNamespace(start=lambda *args: None)))
    await cog.cog_load()
    assert seen[schedule.WEEKLY_JOB] == daily.SEASON_END_AT
    assert seen[schedule.DAILY_JOB] == daily.DAILY_RESET_AT


# -- the tower -----------------------------------------------------------------------------

async def test_start_tower_from_a_click_and_from_the_command(game, monkeypatch):
    bot = game.bot
    bot.debug = False
    ala = game.add_member("Ala")
    runs: list = []

    async def fake_run(scene):
        runs.append((scene.author, bot.state.busy_owner))
        return 0

    monkeypatch.setattr(tower, "explore_tower", fake_run)

    async def start() -> FakeInteraction:
        click = whisperer(game, ala)
        await tower.start_tower(ui.scene_of(click), refuse=lambda text: ui.reply(click, text))
        return click

    assert (await start()).replies == [] and runs == [(ala, "tower")] and not bot.state.busy
    tower.save_weekly_tower_to_file(bot, ala.id)
    monday = f"<t:{int(daily.next_season_end().timestamp())}:R>"
    assert (await start()).whispers == [tower.MSG_ON_COOLDOWN.format(when=monday)]
    await bot.state.set_boss_phase(BossPhase.ENGAGED)
    assert (await start()).whispers == [tower.MSG_BOSS_FIGHT]
    await bot.state.set_boss_phase(BossPhase.WAITING)
    with bot.state.hold_busy("hunt"):
        assert (await start()).whispers == [ui.MSG_BUSY]
    assert len(game.channel.bot_messages) == 1                         # nothing public so far

    assert await tower.start_tower(game.scene(ala)) is False             # the typed command answers publicly
    assert game.channel.messages[-1].content == tower.cooldown_text()
    assert monday in game.channel.messages[-1].content and len(runs) == 1


# -- the gem pouch: a menu instead of $wybierzklejnot --------------------------------------

OWNER_DEFAULTS = dict(pet_owned=True, reroll_scroll=0, reroll_scroll_shard=0, rebirth_stones=0, mirrors=0,
                      pet_id_alt1=0, pet_id_alt2=0, skill_gem=0, pet_id_alt3=0, pet_id_alt4=0)
MENU = "skills:choose"


async def _insert(db, table: str, values: dict) -> None:
    marks = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    await db.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({marks})", *values.values())


async def collector(game, name: str = "Ala", gems=((5, skills.REST), (8, skills.SUMMON), (9, skills.SLOW))):
    """A player with the first gem active and the others in the pouch."""
    db = game.bot.db
    player = game.add_member(name)
    for skill_id, variant in gems:
        await _insert(db, "SKILLS", {"skill_id": skill_id, "gem_name": skills.gem_name(variant),
                                     "skill_variant": variant, "slow_perc": 7})
    await _insert(db, "PETOWNER", {"player_id": player.id, "pet_id": 0, **OWNER_DEFAULTS,
                                   "skill_gem": gems[0][0]})
    for skill_id, _ in gems[1:]:
        await db.execute("INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID) VALUES ($1, $2)", player.id, skill_id)
    return player


async def active_gem(game, player) -> int:
    return int(await game.bot.db.fetchval("SELECT SKILL_GEM FROM PETOWNER WHERE PLAYER_ID = $1", player.id))


async def test_pouch_menu_makes_another_gem_active(game_db):
    ala = await collector(game_db)
    ola = game_db.add_member("Ola")
    assert await skills.show_skills(game_db.scene(ala)) is True
    pouch = game_db.channel.messages[-1]
    menu = pouch.button(MENU)
    assert [(o.label, o.value, o.description, o.default) for o in menu.options] == [
        ("1. Klejnot Regeneracji", "1", "aktywny", True),
        ("2. Klejnot Przywołania", "2", None, False),
        ("3. Klejnot Spowolnienia", "3", None, False)]
    assert menu.placeholder == skills.POUCH_PLACEHOLDER and "w menu pod listą" in pouch.embeds[0].description

    other = await game_db.click(pouch, ola, MENU, values=["2"])            # not her list
    assert other.whispers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    already = await game_db.click(pouch, ala, MENU, values=["1"])
    assert already.whispers == [skills.MSG_ALREADY_ACTIVE.format(player_id=ala.id, name="Klejnot Regeneracji")]

    chosen = await game_db.click(pouch, ala, MENU, values=["2"])
    assert chosen.whispers == [skills.MSG_GEM_CHOSEN.format(player_id=ala.id, name="Klejnot Przywołania")]
    assert await active_gem(game_db, ala) == 8
    assert "**2. Klejnot Przywołania** ✅ aktywny" in pouch.embeds[0].description        # the list is repainted
    assert [o.default for o in pouch.button(MENU).options] == [False, True, False]
    assert pouch.view is not None and len(game_db.channel.bot_messages) == 2             # nothing public

    assert await skills.choose_skill(game_db.scene(ala), "3") is True                   # typing still works
    assert await active_gem(game_db, ala) == 9


async def test_pouch_menu_acknowledges_the_choice_before_the_database_work(game_db, monkeypatch):
    """Review: two transactions (with retries) ran before the 3-second answer."""
    ala = await collector(game_db)
    await skills.show_skills(game_db.scene(ala))
    pouch = game_db.channel.messages[-1]
    clicks: list = []
    seen: list = []
    real_gate, real_choose, real_load = ui.gate, skills._choose, skills._load_pouch

    async def gate(interaction, **kwargs):
        clicks.append(interaction)
        return await real_gate(interaction, **kwargs)

    def watched(name, real):
        async def run(*args, **kwargs):
            seen.append((name, clicks[-1].response.is_done()))
            return await real(*args, **kwargs)
        return run

    monkeypatch.setattr(ui, "gate", gate)
    monkeypatch.setattr(skills, "_choose", watched("_choose", real_choose))
    monkeypatch.setattr(skills, "_load_pouch", watched("_load_pouch", real_load))
    chosen = await game_db.click(pouch, ala, MENU, values=["2"])
    assert seen == [("_choose", True), ("_load_pouch", True)]          # answered before the database
    assert chosen.response.deferred
    assert chosen.whispers == [skills.MSG_GEM_CHOSEN.format(player_id=ala.id, name="Klejnot Przywołania")]
    assert "**2. Klejnot Przywołania** ✅ aktywny" in pouch.embeds[0].description        # still repainted
    assert [o.default for o in pouch.button(MENU).options] == [False, True, False]
    assert await active_gem(game_db, ala) == 8


async def test_pouch_menu_follows_the_gameplay_rules(game_db):
    bot = game_db.bot
    ala = await collector(game_db)
    elsewhere = game_db.announce_channel
    assert await skills.show_skills(Scene(bot=bot, channel=elsewhere, author=ala)) is True   # shown anywhere
    pouch = elsewhere.messages[-1]
    refused = await game_db.click(pouch, ala, MENU, values=["2"])       # chosen: boss channel only
    assert refused.whispers == [ui.MSG_WRONG_CHANNEL.format(channel_id=bot.ids.boss_channel)]

    await skills.show_skills(game_db.scene(ala))
    pouch = game_db.channel.messages[-1]
    with bot.state.hold_busy("hunt"):
        assert (await game_db.click(pouch, ala, MENU, values=["2"])).whispers == [ui.MSG_BUSY]
    ala.roles.append(game_db.guild.add_role(bot.ids.dead_hunter_role))
    game_db.channel.silenced_roles.add(bot.ids.dead_hunter_role)
    assert (await game_db.click(pouch, ala, MENU, values=["2"])).whispers == [ui.MSG_CANNOT_PLAY]
    assert await active_gem(game_db, ala) == 5


async def test_pouch_with_one_gem_or_in_private_has_no_menu_and_the_menu_expires(game_db, monkeypatch):
    lone = await collector(game_db, "Ola", gems=((3, skills.SLOW),))
    assert await skills.show_skills(game_db.scene(lone)) is True
    assert game_db.channel.messages[-1].view is None
    assert "$wybierzklejnot numer" in game_db.channel.messages[-1].embeds[0].description

    ala = await collector(game_db)
    click = FakeInteraction(game_db.bot, ala, channel=game_db.channel)
    assert await skills.show_skills(ui.whisper_scene(click)) is True                 # privately (hub panel)
    assert click.replies[0].ephemeral and click.replies[0].view is None

    monkeypatch.setattr(skills, "POUCH_MENU_SECONDS", 0.05)
    await skills.show_skills(game_db.scene(ala))
    pouch = game_db.channel.messages[-1]
    assert pouch.view is not None
    await until(lambda: pouch.view is None)                                          # unused: taken away
    assert not ui.is_live(pouch.id)


async def test_pouch_and_cooldown_texts_show_the_real_reset(game_db):
    bot = game_db.bot
    ala = await collector(game_db)
    skills.daily_skill_used(bot, ala.id)
    skills.weekly_skill_used(bot, ala.id)
    await skills.show_skills(game_db.scene(ala))
    text = game_db.channel.messages[-1].embeds[0].description
    tomorrow = f"<t:{int(daily.next_daily_reset().timestamp())}:R>"
    monday = f"<t:{int(daily.next_season_end().timestamp())}:R>"
    assert f"**1. Klejnot Regeneracji** ✅ aktywny *(regeneruje się, gotowy {tomorrow})*" in text
    assert f"2. Klejnot Przywołania *(regeneruje się, gotowy {monday})*" in text
    descriptions = [option.description for option in game_db.channel.messages[-1].button(MENU).options]
    assert descriptions == ["aktywny", "regeneruje się", "regeneruje się"]
    assert skills.daily_cd_text() == skills.MSG_DAILY_CD.format(when=tomorrow)
    assert skills.weekly_cd_text() == skills.MSG_WEEKLY_CD.format(when=monday)


# -- the rest's sign-up panel --------------------------------------------------------------

async def test_rest_roster_is_repainted_while_the_rest_lasts(game_db, monkeypatch):
    bot = game_db.bot
    monkeypatch.setattr(skills, "ROSTER_EDIT_GAP", 0.01)
    host = await collector(game_db, "Ala", gems=((5, skills.REST),))
    ola, ela = game_db.add_member("Ola"), game_db.add_member("Ela")
    for tired in (ola, ela):
        bot.store.daily_cd.add(tired.id)
    seen: list = []

    async def pause(seconds):
        invitation = game_db.last_with_buttons()
        assert invitation.button_labels == [skills.REST_BUTTON]
        assert invitation.embeds[0].fields[0].value == skills.REST_ROSTER_EMPTY
        await game_db.click(invitation, ola, "rest")
        await until(lambda: invitation.embeds[0].fields[0].name == "Odpoczywają (1)")
        seen.append(invitation.embeds[0].fields[0].value)

    monkeypatch.setattr(skills.timing, "pause", pause)
    assert await skills.use_skill(game_db.scene(host, "$skill"), host) == skills.REST
    assert seen == [f"<@{ola.id}>"]
    assert bot.store.daily_cd.all() == [str(ela.id)]
    assert game_db.channel.ever_sent(skills.MSG_REST_DONE)
