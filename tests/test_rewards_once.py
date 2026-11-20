"""Owner's rule of 2026-11-20: a decided fight and a paid reward are never repeated.

A Death Tower run and a daily hunt store their cooldown before the fight and
give the attempt back after a technical problem - but only while nothing has
been decided: once a floor of the tower was cleared (its loot is being handed
out) or the monster of a hunt is dead / the hunters have lost, a technical
problem (an error, three disrupted rounds, a cancellation) no longer gives the
attempt back - a second run would hand the same loot out again.  A decided
run or hunt that breaks afterwards is not re-raised either: the player is told
it counts and the owner is pinged.  A lost fight whose loss message cannot be
sent stays an ordinary lost fight (only logged: nothing to settle by hand).

The Twitch treasures (``check_treasure``) are covered in ``test_twitch.py``.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import re

import pytest

from altermmo.core import challenge, guards
from altermmo.core.challenge import Outcome, RoundResult
from altermmo.features import achievements, boss, daily, database, modifiers, pets, tower

ZERO = {"CRIT_PERC": 0.0, "REPLACE_PERC": 0.0, "DEF_PERC": 0.0, "DROP_PERC": 0.0,
        "LOWHP_PERC": 0.0, "SLOW_PERC": 0.0}


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def quiet_lobby(monkeypatch):
    """No second hunter joins: do not wait the real 8 seconds; no busy notices left over."""
    monkeypatch.setattr(daily, "JOIN_TIME", 0.03)
    monkeypatch.setattr(guards, "_last_notice", {})
    daily._gathering.clear()
    daily._panels.clear()


@pytest.fixture
def skills(monkeypatch):
    """No pet bonuses, no database."""
    async def fake_pet_skills(bot, player_id):
        return dict(ZERO)

    monkeypatch.setattr(daily, "pet_skills", fake_pet_skills)


@pytest.fixture
def drops(monkeypatch):
    """The tower's floor loot (boss loot table) as a recorder: a list of (rarity, hunter id)."""
    seen: list[tuple] = []

    async def fake_loot(scene, rarity, hunter, boost):
        seen.append((rarity, hunter.id))
        return ""

    monkeypatch.setattr(boss, "rand_loot", fake_loot)
    monkeypatch.setattr(tower, "_store_record", _nothing)
    return seen


@pytest.fixture
def hunt_loot(monkeypatch):
    """The hunt's rewards without a database: the loot is recorded (hunter ids)."""
    seen: list[int] = []

    async def fake_loot(scene, rarity, hunter, boost):
        seen.append(hunter.id)
        return ""

    monkeypatch.setattr(daily, "rand_loot", fake_loot)
    monkeypatch.setattr(database, "update_ranking_table", _nothing)
    monkeypatch.setattr(achievements, "add_hunts", _nothing)
    return seen


def prompts(channel) -> list[str]:
    """Every numbered fight prompt the bot posted."""
    return [m.content for m in channel.sent if m.content and re.match(r"^(~~)?\d+\. ", m.content)]


async def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.001)


def break_sends(game, broken) -> None:
    """Make every bot message whose text satisfies ``broken(content)`` fail (Discord refuses it)."""
    original = game.channel.send

    async def send(content=None, **kwargs):
        if content and broken(content):
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game.channel.send = send


def owner_pings(game) -> list[str]:
    prefix = f"<@{game.bot.ids.owner}>!"
    return [m.content for m in game.log_channel.messages if m.content and m.content.startswith(prefix)]


async def refused_tower_run(game, player) -> list[str]:
    """``$wieża`` again (as the hub button runs it); returns the refusals, asserts nothing started."""
    refused: list[str] = []

    async def refuse(text):
        refused.append(text)

    assert await tower.start_tower(game.scene(player), refuse=refuse) is False
    return refused


def cut_short(player, floors: int) -> str:
    return tower.MSG_RUN_CUT_SHORT.format(user_id=player.id, floors=floors)


async def _nothing(*args, **kwargs):
    return None


# -- the Death Tower: before the first floor is rewarded --------------------------------------

async def test_tower_floor_reports_progress_only_when_cleared(game, skills, drops, short_timeouts):
    hunter = game.add_member("Ala")
    progress = {"rewarded": False}
    game.auto_answer(hunter, wrong_on_round=1)
    assert await tower.tower_fight(game.scene(hunter), 1, hunter, progress=progress) is False
    assert progress == {"rewarded": False} and drops == []

    game.channel.send_hooks.clear()
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 1, hunter, progress=progress) is True
    assert progress == {"rewarded": True} and drops == [(0, hunter.id)]

    # Old callers pass no progress: unchanged.
    assert await tower.tower_fight(game.scene(hunter), 1) is True
    assert len(drops) == 2


async def test_tower_cancelled_on_floor_one_before_the_kill_gives_the_attempt_back(game, skills, drops):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, silent_on_round=2)                 # floor 1 never ends on its own
    task = asyncio.ensure_future(tower.explore_tower(game.scene(hunter)))
    await wait_until(lambda: len(prompts(game.channel)) == 2)
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not tower.load_weekly_tower_from_file(bot, hunter.id)
    assert drops == [] and bot.owner_log == []


# -- the Death Tower: after a floor was rewarded ---------------------------------------------

async def test_tower_disruptions_after_a_rewarded_floor_keep_the_cooldown(game, skills, drops, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    real_ask = challenge.ask
    calls = itertools.count(1)

    async def dead_after_floor_one(bot, channel, prompt, **kwargs):
        if next(calls) > 2:                                     # floor 1 has two words
            return RoundResult(Outcome.DISRUPTED)
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", dead_after_floor_one)
    game.auto_answer(hunter)
    assert await tower.explore_tower(game.scene(hunter)) == 1
    assert game.channel.messages[-2].content == tower.MSG_TOWER_INTERRUPTED
    assert game.channel.messages[-1].content == cut_short(hunter, 1)
    assert not game.channel.contains(tower.MSG_ATTEMPT_RETURNED.format(user_id=hunter.id))
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    assert drops == [(0, hunter.id)]
    assert bot.owner_log == [tower.LOG_RUN_CUT_SHORT.format(user_id=hunter.id, floors=1)]
    assert owner_pings(game) == [f"<@{bot.ids.owner}>!   " + bot.owner_log[0]]

    monkeypatch.setattr(challenge, "ask", real_ask)
    assert await refused_tower_run(game, hunter) == [tower.cooldown_text()]
    assert drops == [(0, hunter.id)]


async def test_tower_failure_on_floor_three_names_the_last_cleared_floor(game, skills, drops):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    break_sends(game, lambda content: content.startswith("5. "))   # floors 1 / 2 have 2 / 4 words
    # Not re-raised: the player is told the run counts ("try again" would contradict it).
    assert await tower.explore_tower(game.scene(hunter)) == 2
    assert game.channel.messages[-1].content == cut_short(hunter, 2)
    assert "po 2. piętrze" in game.channel.messages[-1].content
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    assert drops == [(0, hunter.id), (0, hunter.id)]            # one loot for each cleared floor
    assert tower.LOG_RUN_CUT_SHORT.format(user_id=hunter.id, floors=2) in bot.owner_log
    assert len(owner_pings(game)) == 1
    assert await refused_tower_run(game, hunter) == [tower.cooldown_text()]


async def test_tower_cancelled_after_a_rewarded_floor_keeps_the_cooldown(game, skills, drops):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, silent_on_round=4)                 # floor 2 hangs on its second word
    task = asyncio.ensure_future(tower.explore_tower(game.scene(hunter)))
    await wait_until(lambda: len(prompts(game.channel)) == 4)
    assert drops == [(0, hunter.id)]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    assert not game.channel.contains("możesz spróbować ponownie")
    assert await refused_tower_run(game, hunter) == [tower.cooldown_text()]
    assert drops == [(0, hunter.id)]


async def test_tower_cancelled_while_the_first_loot_is_handed_out_keeps_the_cooldown(game, skills,
                                                                                      monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    monkeypatch.setattr(tower, "_store_record", _nothing)
    handed_out: list[int] = []
    in_loot = asyncio.Event()

    async def slow_loot(scene, rarity, player, boost):
        handed_out.append(player.id)
        in_loot.set()
        await asyncio.get_running_loop().create_future()        # the bot stops right here

    monkeypatch.setattr(boss, "rand_loot", slow_loot)
    game.auto_answer(hunter)
    task = asyncio.ensure_future(tower.explore_tower(game.scene(hunter)))
    await asyncio.wait_for(in_loot.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handed_out == [hunter.id]
    assert tower.load_weekly_tower_from_file(bot, hunter.id)    # the loot may be out: the run counts
    assert await refused_tower_run(game, hunter) == [tower.cooldown_text()]
    assert handed_out == [hunter.id]


# -- the Death Tower: a lost floor stays lost ----------------------------------------------

async def test_tower_floor_lost_whose_message_cannot_be_sent_ends_the_run_normally(game, skills, drops,
                                                                                   caplog):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, wrong_on_round=3)                  # clears floor 1, falls on floor 2
    break_sends(game, lambda content: content == tower.MSG_MISTAKE_LOST)
    assert await tower.explore_tower(game.scene(hunter)) == 1
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    assert not game.channel.contains("problemów technicznych")  # neither given back nor "cut short"
    assert drops == [(0, hunter.id)]
    # Only logged: after a lost floor the owner has nothing to hand out by hand.
    assert bot.owner_log == [] and owner_pings(game) == []
    assert any(r.name == "altermmo.features.daily" and r.getMessage() == "Could not announce the lost fight."
               and r.exc_info and isinstance(r.exc_info[1], RuntimeError) for r in caplog.records)


async def test_tower_floor_one_lost_whose_message_cannot_be_sent_is_no_technical_problem(game, skills, drops):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, wrong_on_round=1)
    break_sends(game, lambda content: content == tower.MSG_MISTAKE_LOST)
    assert await tower.explore_tower(game.scene(hunter)) == 0
    assert tower.load_weekly_tower_from_file(bot, hunter.id)    # an ordinary lost run counts
    assert not game.channel.contains("problemów technicznych") and drops == []


# -- the daily hunt ------------------------------------------------------------------------

async def test_hunt_reports_progress_only_when_decided(game, skills, hunt_loot, monkeypatch, short_timeouts):
    hunter = game.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)

    game.auto_answer(hunter)
    won = {"decided": False}
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None, progress=won) is True
    assert won == {"decided": True} and hunt_loot == [hunter.id]

    game.channel.send_hooks.clear()
    game.auto_answer(hunter, silent_on_round=2)
    lost = {"decided": False}
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None, progress=lost) is False
    assert lost == {"decided": True}

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(challenge, "ask", dead_line)
        broken = {"decided": False}
        assert await daily.hunt_mobs(game.scene(hunter), 0, False, None, progress=broken) is None
    assert broken == {"decided": False}

    # Old callers (events) pass no progress: unchanged.
    game.channel.send_hooks.clear()
    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(), 0, False, None, hunter) is True
    assert hunt_loot == [hunter.id, hunter.id]


async def test_hunt_cancelled_before_the_kill_gives_the_attempt_back(game, skills, hunt_loot, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 3)
    game.auto_answer(hunter, silent_on_round=3)                 # the last word never comes
    task = asyncio.ensure_future(daily.daily_hunt(game.scene(hunter)))
    await wait_until(lambda: len(prompts(game.channel)) == 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not daily.load_daily_from_file(bot, hunter.id) and hunt_loot == []


async def test_hunt_cancelled_during_the_reward_hand_out_keeps_the_daily_cooldown(game, skills, hunt_loot,
                                                                                  monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)
    handed_out: list[int] = []
    in_loot = asyncio.Event()

    async def slow_loot(scene, rarity, player, boost):
        handed_out.append(player.id)
        in_loot.set()
        await asyncio.get_running_loop().create_future()        # the bot stops right here

    monkeypatch.setattr(daily, "rand_loot", slow_loot)
    game.auto_answer(hunter)
    task = asyncio.ensure_future(daily.daily_hunt(game.scene(hunter)))
    await asyncio.wait_for(in_loot.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert game.channel.contains(daily.MSG_WON.format(mentions=f"<@{hunter.id}> "))
    assert handed_out == [hunter.id]
    assert daily.load_daily_from_file(bot, hunter.id)           # the monster is dead: the hunt counts
    assert not game.channel.contains("możesz zapolować ponownie")


async def test_hunt_error_after_the_kill_keeps_the_daily_cooldown(game, skills, hunt_loot, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)

    def broken_clock(*args, **kwargs):
        raise RuntimeError("clock broke")

    monkeypatch.setattr(daily, "fight_time_message", broken_clock)   # fails outside the guarded steps
    game.auto_answer(hunter)
    # Not re-raised ("something went wrong, try again" would be wrong): the hunt counts as not won.
    assert await daily.daily_hunt(game.scene(hunter)) is False
    assert daily.load_daily_from_file(bot, hunter.id)
    assert not game.channel.contains("możesz zapolować ponownie")
    assert game.channel.messages[-1].content == daily.MSG_HUNT_CUT_SHORT.format(user_id=hunter.id)
    assert hunt_loot == []                                      # the error came before the loot
    report = daily.LOG_HUNT_CUT_SHORT.format(user_id=hunter.id, error="RuntimeError")
    assert bot.owner_log == [report]
    assert owner_pings(game) == [f"<@{bot.ids.owner}>!   " + report]

    # The cooldown holds: a second $polowanie today is refused (no second monster).
    refused: list[str] = []

    async def refuse(text):
        refused.append(text)

    shown = len(game.channel.sent)
    assert await daily.start_hunt(game.scene(hunter), refuse=refuse) is False
    assert refused == [daily.cooldown_text()] and len(game.channel.sent) == shown   # no monster


async def test_hunt_error_before_the_kill_gives_the_attempt_back(game, skills, hunt_loot, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 3)
    game.auto_answer(hunter)
    break_sends(game, lambda content: content.startswith("3. "))     # the last word
    with pytest.raises(RuntimeError):
        await daily.daily_hunt(game.scene(hunter))
    assert not daily.load_daily_from_file(bot, hunter.id)
    assert game.channel.messages[-1].content == daily.MSG_ATTEMPT_RETURNED.format(user_id=hunter.id)
    assert hunt_loot == []


async def test_lost_hunt_whose_message_cannot_be_sent_stays_lost(game_db, monkeypatch, caplog):
    bot = game_db.bot
    monkeypatch.setattr(random, "randint", lambda low, high: low)    # a normal monster, 3 hp
    hunter, patron = game_db.add_member("Ala"), game_db.add_member("Patron")
    await modifiers.modify_modifiers(bot, "player_id", patron.id)    # the monster is a player
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    game_db.auto_answer(hunter, wrong_on_round=2)
    break_sends(game_db, lambda content: content == daily.MSG_MISTAKE_LOST)

    assert await daily.daily_hunt(game_db.scene(hunter)) is False
    assert daily.load_daily_from_file(bot, hunter.id)                # no give-back
    assert not game_db.channel.contains("możesz zapolować ponownie")
    # The player who posed as the monster still gets his scroll, the loser is still banned.
    row = await bot.db.fetchrow("SELECT reroll_scroll FROM PETOWNER WHERE PLAYER_ID = $1", patron.id)
    assert row is not None and row["reroll_scroll"] == 1
    assert bot.role(bot.ids.dead_hunter_role) in hunter.roles
    # The unsent message is only logged (daily.say_lost): nothing for the owner to settle by hand.
    assert bot.owner_log == [] and owner_pings(game_db) == []
    assert any(r.getMessage() == "Could not announce the lost fight." and r.exc_info
               and isinstance(r.exc_info[1], RuntimeError) for r in caplog.records)


async def test_failing_dead_hunter_ban_is_reported_not_raised(game, skills, monkeypatch):
    bot = game.bot
    hunter, patron = game.add_member("Ala"), game.add_member("Patron")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    scrolls: list[tuple] = []

    async def record_scrolls(scene, number, player_id):
        scrolls.append((number, player_id))

    async def no_ban(bot, member_or_id):
        raise RuntimeError("Missing Permissions")

    monkeypatch.setattr(pets, "assign_scroll", record_scrolls)
    monkeypatch.setattr(boss, "set_dead_hunters", no_ban)
    game.auto_answer(hunter, wrong_on_round=1)
    progress = {"decided": False}
    assert await daily.hunt_mobs(game.scene(hunter), 1, True, patron, progress=progress) is False
    assert progress == {"decided": True}
    assert game.channel.messages[-1].content == daily.MSG_MISTAKE_LOST
    assert scrolls == [(2, patron.id)]
    assert any(f"po przegranym polowaniu (rola martwego łowcy <@{hunter.id}>)" in line
               and "RuntimeError" in line for line in bot.owner_log)
