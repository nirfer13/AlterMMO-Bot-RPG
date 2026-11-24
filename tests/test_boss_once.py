"""A boss is fought once (owner's rule of 2026-11-20): the real boss cycle with real fights.

Once a fight is decided - the boss is dead or the hunters have lost - neither an
error while the result is told or the rewards are handed out, nor the time
limit of the encounter, nor a restart of the bot may bring the same boss back
(``BossCycle.mark_decided``).  A fight that breaks *before* it is decided still
leaves the boss attackable (``boss_returns_after_interrupted_fight``).  The reset
after a decided fight (shrine modifiers, the dead, the bans) is owed until
``finish`` runs: a restart in between makes it up (``_finish_after_restart``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import re
import time
from types import SimpleNamespace

import discord
import pytest

from altermmo.core import challenge
from altermmo.core.challenge import Outcome, RoundResult
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossInfo, BossPhase
from altermmo.features import boss, boss_cycle, database, expsum, modifiers
from altermmo.features.fight_types import FightResult

from fakes import requested_word

EXP_ONLY = [{"id": 0, "descr": "250 doświadczenia na Discordzie", "weight": 100}]
SUMMONED = boss_cycle._KEY_SUMMONED
PENDING = boss_cycle._KEY_PENDING_RESET
INTERRUPTED = "walka została przerwana"
LOST_LINE = "Ala pomylił się i nie zabił bossa."


@pytest.fixture(autouse=True)
def plain_fights(monkeypatch, tmp_path):
    """Predictable fights: the loot is 250 experience, no pet helps, the lone attacker fights at once.

    The lobby is not under test here (``tests/test_boss.py`` covers it); the
    fight, its rewards and the boss cycle are the real ones.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    tables = {f"loot_details_{name}": EXP_ONLY for name in ("Normal", "Rare", "Epic", "Legend", "Mythic")}
    (config_dir / "lootConfig.json").write_text(json.dumps(tables, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(boss, "CONFIG_DIR", config_dir)

    async def no_pet(bot, player_id):
        return {key: 0.0 for key in boss._PET_SKILL_KEYS}

    async def alone(scene, rarity):
        return scene.author

    async def picture(scene, rarity):
        await scene.send(f"BOSS rarity {rarity}")
        return False, None

    monkeypatch.setattr(boss, "_pet_skills", no_pet)
    monkeypatch.setattr(boss, "single_init", alone)
    monkeypatch.setattr(boss, "send_boss_image", picture)


# -- helpers --------------------------------------------------------------------------------

async def settle() -> None:
    """Let what the cycle started in the background (notices, acknowledgements) run."""
    for _ in range(50):
        await asyncio.sleep(0)


async def eventually(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


async def scheduled_boss(game, monkeypatch, rarity: int = 1, *, hp: int = 2) -> None:
    """The scheduled boss stands on the channel; its row in BOSS still says "respawn started"."""
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: hp)
    await database.update_boss_table(game.bot, rarity, 0, True)
    game.bot.state.boss = BossInfo(rarity=rarity)
    await game.bot.state.set_boss_phase(BossPhase.VISIBLE)


async def summoned_boss(game, monkeypatch, rarity: int = 1, *, hp: int = 2, scheduled: int = 2):
    """A ritual / skill gem boss on the channel while the scheduled boss (``scheduled``) waits.

    Returns when the scheduled boss is due.
    """
    bot = game.bot
    cycle = bot.boss_cycle
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: hp)
    await database.update_boss_table(bot, scheduled, 5000, True)
    planned = (await database.read_boss_table(bot)).respawn_at
    cycle.base_rarity = scheduled
    cycle.arrives_at = planned
    await bot.state.set_boss_phase(BossPhase.WAITING)
    assert await cycle.summon(game.scene(), rarity, show_image=False)
    assert bot.store.runtime_get(SUMMONED) == rarity
    return planned


def not_attackable(member) -> str:
    return boss_cycle.MSG_NOT_ATTACKABLE.format(user_id=member.id)


async def assert_boss_is_gone(game, *members, interrupted_before: int = 0) -> None:
    """Nobody can attack the boss any more - neither with !zaatakuj nor with a click.

    ``interrupted_before``: "the fight was interrupted" notices of earlier encounters.
    """
    bot = game.bot
    await settle()
    assert bot.state.boss_phase not in (BossPhase.VISIBLE, BossPhase.ENGAGED, BossPhase.FIGHTING)
    assert game.channel.sent_count(INTERRUPTED) == interrupted_before     # no "the boss is still here"
    assert boss_cycle.ui.register(bot, boss_cycle.AttackView())
    old = await boss_cycle.with_attack_button(lambda view: game.channel.send("stary boss", view=view))
    for member in members:
        await bot.boss_cycle.attack(game.scene(member, "!zaatakuj"))
        assert game.channel.messages[-1].content == not_attackable(member)
        click = await game.click(old, member, "Zaatakuj")
        assert click.public == [not_attackable(member)]
    assert bot.state.boss_phase not in (BossPhase.VISIBLE, BossPhase.ENGAGED, BossPhase.FIGHTING)


def pinged(game, text: str) -> bool:
    """The owner got ``text`` with a ping in the log channel."""
    return any(m.content == f"<@{game.bot.ids.owner}>!   {text}" for m in game.log_channel.messages)


def broke(error: str) -> str:
    return boss_cycle.MSG_DECIDED_FIGHT_BROKE.format(error=error)


def broke_reports(bot) -> list[str]:
    head = boss_cycle.MSG_DECIDED_FIGHT_BROKE.split("`")[0]
    return [line for line in bot.owner_log if line.startswith(head)]


async def ranking(bot) -> dict[str, int]:
    return {row["id"]: int(row["points"]) for row in await bot.db.fetch("SELECT ID, POINTS FROM RANKING")}


def team_answers(game, players, *, wrong_rounds=()) -> None:
    """Every party member answers the prompts addressed to them (``N. **Name**: ...``)."""
    by_name = {player.name: player for player in players}
    counter = itertools.count(1)

    def hook(message):
        match = re.match(r"^\d+\. \*\*(.+?)\*\*: ", message.content)
        word = requested_word(message.content)
        if match is None or word is None:
            return
        addressed = by_name[match.group(1)]
        game.say_later(addressed, "zlaodpowiedz" if next(counter) in wrong_rounds else word, 0)

    game.channel.send_hooks.append(hook)


def legendary_party(game, monkeypatch, names=("Ala", "Ola", "Ela", "Ula")):
    """Four players; the legendary lobby hands them to the fight at once."""
    players = [game.add_member(name) for name in names]

    async def party(scene, rarity):
        return players

    monkeypatch.setattr(boss, "group_init", party)
    return players


def refuse_text(game, text: str) -> None:
    """Discord refuses exactly the message ``text`` on the boss channel (every attempt)."""
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        if content == text:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
        return await real_send(content, **kwargs)

    game.channel.send = send


async def die_during_the_rewards(game, monkeypatch, hunter) -> None:
    """The process dies while the loot is handed out: the attack task is cancelled right there."""
    reached = asyncio.Event()

    async def loot_of_a_dying_process(scene, rarity, hunter_, boost):
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(boss, "rand_loot", loot_of_a_dying_process)
    game.auto_answer(hunter)
    attack = asyncio.ensure_future(game.bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj")))
    await asyncio.wait_for(reached.wait(), 5)
    attack.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attack
    await settle()
    assert not game.channel.ever_sent(INTERRUPTED)             # a decided boss is not "still here"


async def new_process(game) -> boss_cycle.BossCycle:
    """A restart: nothing is going on in memory; the data folder and the database stay."""
    bot = game.bot
    bot.state.boss = BossInfo()
    await bot.state.set_boss_phase(BossPhase.IDLE)
    bot.boss_cycle = boss_cycle.BossCycle(bot)
    return bot.boss_cycle


# -- (a) a won fight that breaks during the rewards ----------------------------------------------

@pytest.mark.parametrize("breaks", ["patron check", "drop report"])
async def test_won_fight_that_breaks_during_the_rewards_is_not_fought_again(game_db, monkeypatch, breaks):
    game = game_db
    bot = game.bot
    hunter, other = game.add_member("Ala"), game.add_member("Ola")
    revived = game.add_member("Ela", roles=[bot.ids.dead_hunter_role])
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    if breaks == "patron check":
        # The step after the loot that is not guarded: is a mythic slayer a patron?
        def check_if_patron(member):
            raise AttributeError("roles went missing")

        monkeypatch.setattr(boss, "patrons", SimpleNamespace(check_if_patron=check_if_patron))
        rarity, error = 4, "AttributeError: roles went missing"
    else:
        real_log = bot.log_to_owner

        async def log_to_owner(text, *, ping=False):
            if text.startswith(hunter.name + " otrzymał: \n"):
                raise RuntimeError("log channel gone")
            await real_log(text, ping=ping)

        monkeypatch.setattr(bot, "log_to_owner", log_to_owner)
        rarity, error = 1, "RuntimeError: log channel gone"
    await scheduled_boss(game, monkeypatch, rarity)
    game.auto_answer(hunter)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert bot.state.boss_phase is BossPhase.IDLE              # finish ran: the next boss is scheduled
    assert broke(error) in bot.owner_log and pinged(game, broke(error))
    assert len(broke_reports(bot)) == 1
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS      # reset_end ran
    assert revived.roles == []
    assert not (await database.read_boss_table(bot)).respawn_started
    points = await ranking(bot)
    assert list(points) == [str(hunter.id)]
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}

    await assert_boss_is_gone(game, hunter, other)
    # ...and nothing was handed out a second time.
    assert await ranking(bot) == points
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}
    assert game.channel.sent_count("Boss Drop - Ala") == 1
    assert game.channel.sent_count("Pokonałeś bossa") == 1


async def test_time_limit_during_the_rewards_ends_the_encounter_for_good(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter, other = game.add_member("Ala"), game.add_member("Ola")
    bot.settings = dataclasses.replace(bot.settings, boss_fight_time_limit=1.5)
    looted = []

    async def endless_loot(scene, rarity, hunter_, boost):
        looted.append(hunter_.name)
        await asyncio.Event().wait()                           # Discord hangs while the drop is posted

    monkeypatch.setattr(boss, "rand_loot", endless_loot)
    await scheduled_boss(game, monkeypatch, 1)
    game.auto_answer(hunter)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert looted == ["Ala"]
    assert bot.state.boss_phase is BossPhase.IDLE
    assert broke("TimeoutError: ") in bot.owner_log and pinged(game, broke("TimeoutError: "))
    points = await ranking(bot)
    assert list(points) == [str(hunter.id)]                    # the points came before the loot

    await assert_boss_is_gone(game, hunter, other)
    assert looted == ["Ala"] and await ranking(bot) == points
    assert game.channel.sent_count("Pokonałeś bossa") == 1


async def test_won_group_fight_that_breaks_during_the_rewards_is_not_fought_again(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    players = legendary_party(game, monkeypatch)
    real_log = bot.log_to_owner

    async def log_to_owner(text, *, ping=False):
        if text.startswith("Ala otrzymał: "):
            raise RuntimeError("log channel gone")
        await real_log(text, ping=ping)

    monkeypatch.setattr(bot, "log_to_owner", log_to_owner)
    await scheduled_boss(game, monkeypatch, 3, hp=3)
    team_answers(game, players)

    await bot.boss_cycle.attack(game.scene(players[0], "!zaatakuj"))

    assert bot.state.boss_phase is BossPhase.IDLE
    assert pinged(game, broke("RuntimeError: log channel gone"))
    paid = bot.store.exp_summary.load()
    assert paid == {str(players[0].id): 250}                   # the others are the owner's to pay by hand
    await assert_boss_is_gone(game, *players)
    assert bot.store.exp_summary.load() == paid
    assert game.channel.sent_count("Pokonaliście legendarnego bossa") == 1


# -- (b) a lost fight whose loss message cannot be sent ------------------------------------------

@pytest.mark.parametrize("how", ["mistake", "too slow"])
async def test_lost_fight_whose_loss_message_fails_stays_lost(game_db, monkeypatch, short_timeouts, how):
    game = game_db
    bot = game.bot
    dead_role = bot.role(bot.ids.dead_hunter_role)
    hunter, other = game.add_member("Ala"), game.add_member("Ola")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    await scheduled_boss(game, monkeypatch, 0, hp=5)
    lost_text = boss.MSG_MISTAKE_LOST if how == "mistake" else boss.MSG_TOO_SLOW_LOST
    refuse_text(game, lost_text)
    if how == "mistake":
        game.auto_answer(hunter, wrong_on_round=2)
    else:
        game.auto_answer(hunter, silent_on_round=2)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert bot.state.boss_phase is BossPhase.IDLE
    assert not game.channel.ever_sent(lost_text)              # Discord refused it...
    assert dead_role.members == [hunter]                       # ...the fight is lost all the same
    assert game.notice_channel.contains(f"poległeś z ręki potwora <@{hunter.id}>")
    expected = LOST_LINE if how == "mistake" else "Ala nie zdążył wpisać komend i boss uciekł."
    assert expected in bot.owner_log
    assert broke_reports(bot) == []                            # nothing broke: the fight simply ended
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    await assert_boss_is_gone(game, hunter, other)
    assert dead_role.members == [hunter]
    assert await ranking(bot) == {}


async def test_lost_group_fight_whose_loss_message_fails_stays_lost(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    dead_role = bot.role(bot.ids.dead_hunter_role)
    players = legendary_party(game, monkeypatch)
    bystander = game.add_member("Iza")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    await scheduled_boss(game, monkeypatch, 3, hp=9)
    refuse_text(game, boss.MSG_MISTAKE_LOST)
    team_answers(game, players, wrong_rounds={1, 2})          # the spare chance, then the loss

    await bot.boss_cycle.attack(game.scene(players[0], "!zaatakuj"))

    assert bot.state.boss_phase is BossPhase.IDLE
    assert not game.channel.ever_sent(boss.MSG_MISTAKE_LOST)
    assert set(dead_role.members) == set(players)
    assert "Party Ala pomyliło się i nie zabili bossa." in bot.owner_log
    assert broke_reports(bot) == []
    await assert_boss_is_gone(game, bystander, *players)
    assert set(dead_role.members) == set(players)


async def test_lost_fight_that_breaks_after_the_decision_still_bans_and_ends(game_db, monkeypatch):
    """The player boss cannot be paid (the disk is full): the lost fight is not turned into "interrupted"."""
    game = game_db
    bot = game.bot
    dead_role = bot.role(bot.ids.dead_hunter_role)
    hunter, patron = game.add_member("Ala"), game.add_member("Patron")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(expsum, "add_file_exp", disk_full)
    await scheduled_boss(game, monkeypatch, 1, hp=5)
    bot.state.boss = BossInfo(rarity=1, is_player_boss=True, player_boss=patron)
    game.auto_answer(hunter, wrong_on_round=2)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert bot.state.boss_phase is BossPhase.IDLE
    assert pinged(game, broke("OSError: disk full"))
    assert dead_role.members == [hunter]                       # finish got the decided result
    assert game.channel.sent_count(boss.MSG_MISTAKE_LOST) == 1
    await assert_boss_is_gone(game, hunter, patron)


# -- (c) restart safety: the durable state is cleared when the fight is decided ----------------

def snapshot_at_the_victory(game, seen: dict) -> None:
    """Record the durable boss state the moment the victory is announced (before any reward)."""
    bot = game.bot
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        if content and "Pokonałeś bossa" in content and "row" not in seen:
            seen["row"] = await database.read_boss_table(bot)
            seen["summoned"] = bot.store.runtime_get(SUMMONED)
            seen["ranking"] = await ranking(bot)
            seen["paid"] = bot.store.exp_summary.load()
        return await real_send(content, **kwargs)

    game.channel.send = send


async def test_scheduled_boss_row_is_cleared_before_the_first_reward(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await scheduled_boss(game, monkeypatch, 2)
    assert (await database.read_boss_table(bot)).respawn_started
    seen: dict = {}
    snapshot_at_the_victory(game, seen)
    game.auto_answer(hunter)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    row = seen["row"]
    assert (row.rarity, row.respawn_started) == (0, False)
    assert seen["ranking"] == {} and seen["paid"] == {}        # it really was before the rewards
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}
    assert bot.state.boss_phase is BossPhase.IDLE


async def test_summoned_boss_note_is_cleared_but_the_paused_schedule_stays(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    planned = await summoned_boss(game, monkeypatch, 1, scheduled=2)
    seen: dict = {}
    snapshot_at_the_victory(game, seen)
    game.auto_answer(hunter)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert seen["summoned"] is None and seen["paid"] == {}
    row = seen["row"]
    assert (row.rarity, row.respawn_started, row.respawn_at) == (2, True, planned)   # that boss still comes
    assert bot.state.boss_phase is BossPhase.SCHEDULED
    assert bot.boss_cycle.arrives_at == planned
    assert bot.store.runtime_get(SUMMONED) is None


@pytest.mark.parametrize("kind", ["scheduled", "summoned"])
async def test_restart_during_the_rewards_does_not_bring_the_boss_back(game_db, monkeypatch, kind):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    planned = None
    if kind == "scheduled":
        await scheduled_boss(game, monkeypatch, 1)
    else:
        planned = await summoned_boss(game, monkeypatch, 1, scheduled=2)

    await die_during_the_rewards(game, monkeypatch, hunter)
    fresh = await new_process(game)
    await fresh._restore()

    if kind == "scheduled":
        assert bot.state.boss_phase is BossPhase.IDLE          # a new boss is drawn, this one is gone
    else:
        assert bot.state.boss_phase is BossPhase.SCHEDULED     # the paused schedule goes on...
        assert fresh.arrives_at == planned and bot.state.boss_rarity == 2
        assert fresh._resume_phase is None                     # ...and no summoned boss stands there
    assert game.channel.sent_count("BOSS rarity 1") == 0
    assert bot.store.runtime_get(SUMMONED) is None
    await assert_boss_is_gone(game, hunter)


async def test_restart_before_the_decision_still_brings_the_summoned_boss_back(game_db, monkeypatch):
    """The other side of the rule: an undecided fight of a summoned boss is not lost by a restart."""
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await summoned_boss(game, monkeypatch, 1, scheduled=2)
    prompt = asyncio.Event()
    game.channel.send_hooks.append(lambda message: prompt.set() if message.content.startswith("1. ") else None)
    attack = asyncio.ensure_future(bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj")))
    await asyncio.wait_for(prompt.wait(), 5)                   # the hunter has not answered yet
    attack.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attack
    assert bot.store.runtime_get(SUMMONED) == 1

    fresh = await new_process(game)
    await fresh._restore()
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 1
    assert game.channel.sent_count("BOSS rarity 1") == 1


async def test_restart_during_the_rewards_does_not_carry_the_modifiers_over(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    dead = game.add_member("Ela", roles=[bot.ids.dead_hunter_role])
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 50)
    await scheduled_boss(game, monkeypatch, 1)

    await die_during_the_rewards(game, monkeypatch, hunter)
    # The process died before finish(): the reset is owed - and remembered on disk.
    assert (await modifiers.load_modifiers(bot))["drop_boost_perc"] == 50
    assert dead.roles != []
    assert bot.store.runtime_get(PENDING) == {"losers": []}

    bot.timings = dataclasses.replace(bot.timings, respawn_min=1000, respawn_max=1000)
    fresh = await new_process(game)
    fresh.start()
    assert await bot.state.wait_boss_phase(BossPhase.WAITING, timeout=5)   # the next boss is on its way
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    assert dead.roles == []
    assert bot.store.runtime_get(PENDING) is None


def snapshot_pending_at(game, text: str, seen: dict) -> None:
    """Record the pending-reset marker the moment the boss channel gets ``text``."""
    bot = game.bot
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        if content and text in content and "pending" not in seen:
            seen["pending"] = bot.store.runtime_get(PENDING)
        return await real_send(content, **kwargs)

    game.channel.send = send


@pytest.mark.parametrize("won", [True, False], ids=["won", "lost"])
async def test_pending_reset_is_noted_at_the_decision_and_cleared_by_finish(game_db, monkeypatch, won):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    await scheduled_boss(game, monkeypatch, 1, hp=5)
    assert bot.store.runtime_get(PENDING) is None
    seen: dict = {}
    snapshot_pending_at(game, "Pokonałeś bossa" if won else boss.MSG_MISTAKE_LOST, seen)
    if won:
        game.auto_answer(hunter)
    else:
        game.auto_answer(hunter, wrong_on_round=2)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    # Written before the result was told; the losers to ban are part of it.
    assert seen["pending"] == {"losers": [] if won else [hunter.id]}
    assert bot.state.boss_phase is BossPhase.IDLE
    assert bot.store.runtime_get(PENDING) is None              # finish ran the reset
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS

    # A restart now has nothing to make up for.
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 30)
    fresh = await new_process(game)
    await fresh._restore()
    assert (await modifiers.load_modifiers(bot))["drop_boost_perc"] == 30


async def test_restart_between_a_lost_fight_and_its_reset_does_the_reset_once(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    dead_role = bot.role(bot.ids.dead_hunter_role)
    hunter = game.add_member("Ala")
    fallen = game.add_member("Ela", roles=[bot.ids.dead_hunter_role])      # died to the previous boss
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 50)
    await scheduled_boss(game, monkeypatch, 1, hp=5)
    reached = asyncio.Event()
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        if content == boss.MSG_MISTAKE_LOST:
            reached.set()
            await asyncio.Event().wait()                       # the process dies while telling the loss
        return await real_send(content, **kwargs)

    game.channel.send = send
    game.auto_answer(hunter, wrong_on_round=2)
    attack = asyncio.ensure_future(bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj")))
    await asyncio.wait_for(reached.wait(), 5)
    attack.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attack
    await settle()
    assert not game.channel.ever_sent(INTERRUPTED)
    # Decided, but the reset never ran: the modifiers are on, Ela is dead, Ala is not banned yet.
    assert bot.store.runtime_get(PENDING) == {"losers": [hunter.id]}
    assert (await modifiers.load_modifiers(bot))["drop_boost_perc"] == 50
    assert dead_role.members == [fallen]

    fresh = await new_process(game)
    await fresh._restore()

    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    assert dead_role.members == [hunter]                       # Ela revived, the loser banned
    assert game.notice_channel.sent_count(f"poległeś z ręki potwora <@{hunter.id}>") == 1
    assert bot.store.runtime_get(PENDING) is None
    assert bot.state.boss_phase is BossPhase.IDLE              # the boss is gone: a new one is drawn
    assert game.channel.sent_count("BOSS rarity 1") == 0

    # A second restart: the reset was made up for once - nothing happens again.
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 30)
    again = await new_process(game)
    await again._restore()
    assert (await modifiers.load_modifiers(bot))["drop_boost_perc"] == 30
    assert dead_role.members == [hunter]
    assert game.notice_channel.sent_count(f"poległeś z ręki potwora <@{hunter.id}>") == 1


async def test_pending_reset_that_fails_after_a_restart_is_tried_again(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 50)
    bot.store.runtime_set(PENDING, {"losers": []})
    real_init = modifiers.init_modifiers

    async def database_away(bot_):
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(modifiers, "init_modifiers", database_away)
    fresh = await new_process(game)
    await fresh._restore()                                     # never raises; the owner is told
    assert any("modifiers after a restart" in line for line in bot.owner_log)
    assert bot.store.runtime_get(PENDING) == {"losers": []}    # still owed

    monkeypatch.setattr(modifiers, "init_modifiers", real_init)
    again = await new_process(game)
    await again._restore()
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    assert bot.store.runtime_get(PENDING) is None


async def test_pending_reset_is_made_up_after_a_restart_while_spawning_is_off(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    fallen = game.add_member("Ela", roles=[bot.ids.dead_hunter_role])
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 50)
    bot.store.runtime_set(PENDING, {"losers": []})
    bot.store.runtime_set(boss_cycle._KEY_ENABLED, False)
    fresh = await new_process(game)
    fresh.start()
    await eventually(lambda: bot.store.runtime_get(PENDING) is None, timeout=0.3)
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    assert fallen.roles == []


async def test_restart_without_a_pending_reset_changes_nothing(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    fallen = game.add_member("Ela", roles=[bot.ids.dead_hunter_role])
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 50)
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    await database.update_boss_table(bot, 2, 5000, True)       # the next boss is on its way
    before = await modifiers.load_modifiers(bot)
    assert bot.store.runtime_get(PENDING) is None

    fresh = await new_process(game)
    await fresh._restore()

    assert await modifiers.load_modifiers(bot) == before       # the shrine's gifts are for the coming boss
    assert fallen.roles == [bot.role(bot.ids.dead_hunter_role)]
    assert bot.state.boss_phase is BossPhase.SCHEDULED and bot.state.boss_rarity == 2


# -- (d) a fight interrupted before it is decided: the boss stays (old policy) -------------------

async def test_fight_interrupted_in_the_rounds_leaves_the_boss_attackable(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    await scheduled_boss(game, monkeypatch, 1)
    real_ask = challenge.ask
    line_is_dead = [True]

    async def ask(bot_, channel, prompt, **kwargs):
        if line_is_dead[0]:
            return RoundResult(Outcome.DISRUPTED)
        return await real_ask(bot_, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", ask)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert bot.state.boss_phase is BossPhase.VISIBLE
    await eventually(lambda: game.channel.contains(boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS))
    assert any("Walka z bossem przerwana" in line and "FightInterrupted" in line for line in bot.owner_log)
    assert broke_reports(bot) == []
    assert (await database.read_boss_table(bot)).respawn_started          # a restart would bring it back
    assert (await modifiers.load_modifiers(bot))["ban_loser"] == 1        # nothing was reset

    line_is_dead[0] = False
    game.auto_answer(hunter)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))          # the second try is fought
    assert bot.state.boss_phase is BossPhase.IDLE
    assert list(await ranking(bot)) == [str(hunter.id)]


async def test_time_limit_in_the_lobby_leaves_the_boss_attackable(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    real_limit = bot.settings.boss_fight_time_limit
    bot.settings = dataclasses.replace(bot.settings, boss_fight_time_limit=0.2)
    await summoned_boss(game, monkeypatch, 1)

    async def lobby_that_never_ends(scene, rarity):
        await asyncio.Event().wait()

    monkeypatch.setattr(boss, "single_init", lobby_that_never_ends)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert bot.state.boss_phase is BossPhase.VISIBLE
    assert bot.store.runtime_get(SUMMONED) == 1                # still remembered for a restart
    await eventually(lambda: game.channel.contains(boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS))
    assert broke_reports(bot) == []

    async def alone(scene, rarity):
        return scene.author

    monkeypatch.setattr(boss, "single_init", alone)
    bot.settings = dataclasses.replace(bot.settings, boss_fight_time_limit=real_limit)
    game.auto_answer(hunter)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))
    assert bot.state.boss_phase is BossPhase.SCHEDULED         # fought; the paused schedule goes on
    assert bot.store.runtime_get(SUMMONED) is None


@pytest.mark.parametrize("where", ["lobby", "round"])
async def test_cancelled_undecided_fight_leaves_the_boss_attackable(game_db, monkeypatch, where):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await scheduled_boss(game, monkeypatch, 1)
    reached = asyncio.Event()
    if where == "lobby":
        async def waiting_lobby(scene, rarity):
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(boss, "single_init", waiting_lobby)
    else:
        game.channel.send_hooks.append(lambda message: reached.set() if message.content.startswith("1. ") else None)
    attack = asyncio.ensure_future(bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj")))
    await asyncio.wait_for(reached.wait(), 5)
    attack.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attack

    assert bot.state.boss_phase is BossPhase.VISIBLE
    await eventually(lambda: game.channel.contains(boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS))
    assert any("Walka z bossem przerwana" in line and "cancelled" in line for line in bot.owner_log)
    assert (await database.read_boss_table(bot)).respawn_started

    async def alone(scene, rarity):
        return scene.author

    monkeypatch.setattr(boss, "single_init", alone)
    game.auto_answer(hunter)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))
    assert bot.state.boss_phase is BossPhase.IDLE
    assert list(await ranking(bot)) == [str(hunter.id)]


# -- (e) mark_decided outside an encounter ------------------------------------------------------

@pytest.mark.parametrize("phase", [BossPhase.IDLE, BossPhase.SCHEDULED, BossPhase.WAITING,
                                   BossPhase.PRE_SPAWN, BossPhase.VISIBLE], ids=lambda phase: phase.name)
async def test_mark_decided_outside_an_encounter_does_nothing(game_db, monkeypatch, phase):
    game = game_db
    bot = game.bot
    cycle = bot.boss_cycle
    loser = game.add_member("Ala")
    await database.update_boss_table(bot, 2, 500, True)
    before = await database.read_boss_table(bot)
    bot.store.runtime_set(SUMMONED, 3)
    writes = []

    async def database_gone(*args, **kwargs):
        writes.append(args)
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(database, "update_boss_table", database_gone)
    await bot.state.set_boss_phase(phase)

    await cycle.mark_decided(FightResult(won=False, losers_to_ban=[loser]))
    await boss._decided(bot, FightResult(won=True))           # the way the fights call it

    assert cycle._decided is None and writes == []
    assert bot.store.runtime_get(SUMMONED) == 3
    assert await database.read_boss_table(bot) == before
    assert bot.state.boss_phase is phase


@pytest.mark.parametrize("phase", [BossPhase.ENGAGED, BossPhase.FIGHTING], ids=lambda phase: phase.name)
async def test_mark_decided_never_raises_when_the_database_and_the_disk_fail(game_db, monkeypatch, phase):
    game = game_db
    bot = game.bot
    cycle = bot.boss_cycle
    await database.update_boss_table(bot, 2, 500, True)
    writes = []

    async def database_gone(*args, **kwargs):
        writes.append(args[1:])
        raise DatabaseUnavailable("database gone")

    def disk_gone(key, value):
        raise OSError("disk gone")

    monkeypatch.setattr(database, "update_boss_table", database_gone)
    bot.store.runtime_set(SUMMONED, 3)
    loser = game.add_member("Ala")
    monkeypatch.setattr(bot.store, "runtime_set", disk_gone)
    await bot.state.set_boss_phase(phase)
    # The encounter attack() is running (mark_decided ignores any other fight).
    cycle._encounters += 1
    cycle._active_encounter = cycle._encounters
    result = FightResult(won=False, losers_to_ban=[loser])

    await cycle.mark_decided(result)                           # logged, not raised

    assert writes == [(0, 0, False)]
    assert cycle._decided_result(cycle._encounters) == result  # the encounter still counts as decided
    assert any("boss table after a decided fight" in line and "DatabaseUnavailable" in line
               for line in bot.owner_log)


async def test_failed_write_at_the_decision_does_not_spoil_the_fight(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await scheduled_boss(game, monkeypatch, 1)
    real_update = database.update_boss_table
    failed = []

    async def flaky_update(bot_, rarity, seconds, resume):
        if not failed:
            failed.append((rarity, seconds, resume))
            raise DatabaseUnavailable("database gone")
        return await real_update(bot_, rarity, seconds, resume)

    monkeypatch.setattr(database, "update_boss_table", flaky_update)
    game.auto_answer(hunter)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert failed == [(0, 0, False)]                           # the write at the decision failed...
    assert bot.state.boss_phase is BossPhase.IDLE              # ...the fight went on and ended as usual
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}
    assert not (await database.read_boss_table(bot)).respawn_started     # reset_end wrote it afterwards
    assert broke_reports(bot) == []


# -- (f) a decision belongs to its encounter ----------------------------------------------------

async def test_decision_of_an_earlier_encounter_is_not_reused(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    cycle = bot.boss_cycle
    hunter = game.add_member("Ala")
    seen_at_claim = []

    async def alone(scene, rarity):
        seen_at_claim.append(cycle._decided)
        return scene.author

    monkeypatch.setattr(boss, "single_init", alone)
    real_log = bot.log_to_owner
    drop_report_fails = [True]

    async def log_to_owner(text, *, ping=False):
        if drop_report_fails[0] and text.startswith("Ala otrzymał: \n"):
            drop_report_fails[0] = False
            raise RuntimeError("log channel gone")
        await real_log(text, ping=ping)

    monkeypatch.setattr(bot, "log_to_owner", log_to_owner)
    game.auto_answer(hunter)

    # Encounter 1: won, then broken during the rewards - this boss is gone.
    await scheduled_boss(game, monkeypatch, 1)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))
    assert bot.state.boss_phase is BossPhase.IDLE and len(broke_reports(bot)) == 1

    # The next boss; encounter 2 is interrupted before it is decided: it stays.
    await scheduled_boss(game, monkeypatch, 1)
    real_ask = challenge.ask

    async def dead_line(bot_, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", dead_line)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))
    assert bot.state.boss_phase is BossPhase.VISIBLE
    await eventually(lambda: game.channel.contains(boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS))
    assert len(broke_reports(bot)) == 1                        # not "decided" because encounter 1 was

    # Encounter 3: the boss of encounter 2 is fought normally.
    monkeypatch.setattr(challenge, "ask", real_ask)
    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))
    assert bot.state.boss_phase is BossPhase.IDLE
    assert seen_at_claim == [None, None, None]                 # every encounter starts undecided
    assert bot.store.exp_summary.load() == {str(hunter.id): 500}
    assert len(broke_reports(bot)) == 1


async def test_interrupted_encounter_then_a_decided_one_ends_with_the_new_result(game_db, monkeypatch):
    """Encounter 1 is interrupted (undecided), encounter 2 is lost and breaks: its own result counts."""
    game = game_db
    bot = game.bot
    dead_role = bot.role(bot.ids.dead_hunter_role)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    await scheduled_boss(game, monkeypatch, 1, hp=5)
    real_ask = challenge.ask

    async def dead_line(bot_, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", dead_line)
    await bot.boss_cycle.attack(game.scene(ala, "!zaatakuj"))
    assert bot.state.boss_phase is BossPhase.VISIBLE
    await eventually(lambda: game.channel.contains(boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS))

    monkeypatch.setattr(challenge, "ask", real_ask)

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(expsum, "add_file_exp", disk_full)
    bot.state.boss = BossInfo(rarity=1, is_player_boss=True, player_boss=ala)
    game.auto_answer(ola, wrong_on_round=1)
    await bot.boss_cycle.attack(game.scene(ola, "!zaatakuj"))

    assert pinged(game, broke("OSError: disk full"))
    assert dead_role.members == [ola]                          # the loser of encounter 2 - nobody else
    await assert_boss_is_gone(game, ala, ola, interrupted_before=1)


async def test_decision_of_a_fight_from_before_reset_boss_is_ignored(game_db, monkeypatch):
    """!ResetBoss while a fight sits in its lobby: when that fight is decided later, it is not the
    encounter of the cycle any more - it neither writes the boss table nor owes a reset."""
    game = game_db
    bot = game.bot
    cycle = bot.boss_cycle
    hunter = game.add_member("Ala")
    bot.store.runtime_set(boss_cycle._KEY_ENABLED, False)       # !ResetBoss starts no new schedule
    await scheduled_boss(game, monkeypatch, 1)
    in_lobby, go = asyncio.Event(), asyncio.Event()

    async def slow_lobby(scene, rarity):
        in_lobby.set()
        await go.wait()
        return scene.author

    monkeypatch.setattr(boss, "single_init", slow_lobby)
    real_update = database.update_boss_table
    writes: list[tuple] = []

    async def spy_update(bot_, rarity, seconds, resume):
        writes.append((rarity, seconds, resume))
        return await real_update(bot_, rarity, seconds, resume)

    monkeypatch.setattr(database, "update_boss_table", spy_update)
    seen: dict = {}
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        if content and "Pokonałeś bossa" in content and "decided" not in seen:
            seen["decided"] = cycle._decided
            seen["phase"] = bot.state.boss_phase
            seen["pending"] = bot.store.runtime_get(PENDING)
            seen["writes"] = list(writes)
        return await real_send(content, **kwargs)

    game.channel.send = send
    attack = asyncio.ensure_future(cycle.attack(game.scene(hunter, "!zaatakuj")))
    await asyncio.wait_for(in_lobby.wait(), 5)
    await cycle.reset()
    assert bot.state.boss_phase is BossPhase.IDLE
    writes.clear()

    game.auto_answer(hunter)
    go.set()
    await asyncio.wait_for(attack, 5)

    # The stale fight set FIGHTING itself after its lobby: only the encounter tells it apart.
    assert seen["phase"] is BossPhase.FIGHTING
    assert seen["decided"] is None                             # not this cycle's encounter
    assert seen["pending"] is None and seen["writes"] == []
    assert broke_reports(bot) == []


async def test_decision_of_a_fight_from_before_reset_boss_does_not_decide_the_next_encounter(game_db,
                                                                                                monkeypatch):
    game = game_db
    bot = game.bot
    cycle = bot.boss_cycle
    stale, current = game.add_member("Ala"), game.add_member("Ola")
    bot.store.runtime_set(boss_cycle._KEY_ENABLED, False)       # !ResetBoss starts no new schedule
    await scheduled_boss(game, monkeypatch, 1)
    in_lobby = {stale.id: asyncio.Event(), current.id: asyncio.Event()}
    go = asyncio.Event()

    async def lobby(scene, rarity):
        in_lobby[scene.author.id].set()
        if scene.author.id == current.id:
            await asyncio.Event().wait()                       # the new encounter stays in its lobby
        await go.wait()
        return scene.author

    monkeypatch.setattr(boss, "single_init", lobby)
    seen: dict = {}
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        if content and "Pokonałeś bossa" in content and "decided" not in seen:
            seen["decided"] = cycle._decided
            seen["summoned"] = bot.store.runtime_get(SUMMONED)
            seen["pending"] = bot.store.runtime_get(PENDING)
        return await real_send(content, **kwargs)

    game.channel.send = send
    first = asyncio.ensure_future(cycle.attack(game.scene(stale, "!zaatakuj")))
    second = None
    try:
        await asyncio.wait_for(in_lobby[stale.id].wait(), 5)
        await cycle.reset()                                    # !ResetBoss
        assert await cycle.summon(game.scene(), 1, show_image=False)   # a ritual boss appears...
        second = asyncio.ensure_future(cycle.attack(game.scene(current, "!zaatakuj")))
        await asyncio.wait_for(in_lobby[current.id].wait(), 5)       # ...and Ola engages it

        game.auto_answer(stale)
        go.set()                                               # the fight from before the reset goes on
        await asyncio.wait_for(eventually(lambda: "decided" in seen), 5)

        assert seen["decided"] is None                         # Ola's encounter is not decided by Ala's fight
        assert seen["summoned"] == 1                           # the ritual boss is still remembered
        assert seen["pending"] is None
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


async def test_hanging_write_at_the_decision_does_not_hold_the_fight(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await scheduled_boss(game, monkeypatch, 1)
    monkeypatch.setattr(boss_cycle, "DECIDED_WRITE_TIMEOUT", 0.05)
    real_update = database.update_boss_table
    hung: list[str] = []

    async def hanging_update(bot_, rarity, seconds, resume):
        if not hung:
            hung.append("waiting")
            try:
                await asyncio.Event().wait()                   # the database does not answer
            finally:
                hung.append("given up")
        return await real_update(bot_, rarity, seconds, resume)

    monkeypatch.setattr(database, "update_boss_table", hanging_update)
    game.auto_answer(hunter)

    await asyncio.wait_for(bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj")), 5)

    assert hung == ["waiting", "given up"]                     # bounded by DECIDED_WRITE_TIMEOUT
    assert any("boss table after a decided fight" in line and "TimeoutError" in line for line in bot.owner_log)
    assert game.channel.sent_count("Pokonałeś bossa") == 1     # the fight ended as usual...
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}
    assert bot.state.boss_phase is BossPhase.IDLE
    assert not (await database.read_boss_table(bot)).respawn_started     # ...and finish wrote the row
    assert bot.store.runtime_get(PENDING) is None
    assert broke_reports(bot) == []
    await assert_boss_is_gone(game, hunter)


async def test_fight_time_does_not_include_the_write_at_the_decision(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    hunter = game.add_member("Ala")
    await scheduled_boss(game, monkeypatch, 1, hp=2)
    later = [0.0]
    monkeypatch.setattr(boss, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + later[0],
                                                      time=time.time))
    real_update = database.update_boss_table
    decision_writes: list[tuple] = []

    async def slow_update(bot_, rarity, seconds, resume):
        if not decision_writes:
            decision_writes.append((rarity, seconds, resume))
            later[0] += 100.0                                  # the write takes 100 s on the fight's clock
        return await real_update(bot_, rarity, seconds, resume)

    monkeypatch.setattr(database, "update_boss_table", slow_update)
    game.auto_answer(hunter)

    await bot.boss_cycle.attack(game.scene(hunter, "!zaatakuj"))

    assert decision_writes == [(0, 0, False)]
    [shown] = [m.content for m in game.channel.sent if m.content and m.content.startswith("Zabicie bossa")]
    seconds = float(re.match(r"Zabicie bossa zajęło Ci: (\d+\.\d\d) sekundy!", shown).group(1))
    assert seconds < 100                                       # the 100 s of the write are not counted
    assert bot.state.boss_phase is BossPhase.IDLE


def test_interrupted_policy_is_still_the_default():
    """The tests above of an undecided fight rely on it: the boss stays after a technical problem."""
    from altermmo.settings import Settings
    assert {f.name: f.default for f in dataclasses.fields(Settings)}["boss_returns_after_interrupted_fight"] is True
