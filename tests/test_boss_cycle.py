"""The boss life-cycle with stubbed fights: it must always reach a defined state."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import altermmo.features
from altermmo.core import clock, io
from altermmo.core.state import BossInfo, BossPhase
from altermmo.features import boss_cycle, database, items
from altermmo.features.fight_types import FightInterrupted, FightResult
from altermmo.settings import Timings

FAST = Timings(respawn_min=0, respawn_max=0, respawn_multiplier=1, post_fight_pause=0.0,
               pre_spawn_min=0.0, pre_spawn_max=0.0, event_gap_min=0.01, event_gap_max=0.02)
SLOW_RESPAWN = dataclasses.replace(FAST, respawn_min=1000, respawn_max=1000)


class StubBoss:
    """Stands in for altermmo.features.boss."""

    def __init__(self) -> None:
        self.calls: list = []
        self.result = FightResult(won=True)
        self.fight_error: BaseException | None = None
        self.image_errors = 0
        self.fight_gate: asyncio.Event | None = None
        self.init_gate: asyncio.Event | None = None
        self.base_rarity = 1
        #: The special variant the next boss picture shows ('' = an ordinary boss: the old pair).
        self.variant = ""
        #: The extra keywords of every fight ({} = the call as it always was).
        self.special: list[dict] = []
        #: Where the loot of every fight would come from (``items.LOOT_SOURCE``; None: a scheduled boss).
        self.sources: list = []

    def rarity_for_respawn_time(self, seconds, *, debug):
        return self.base_rarity

    async def send_boss_image(self, scene, rarity):
        if self.image_errors:
            self.image_errors -= 1
            raise ConnectionError("discord down")
        self.calls.append(("image", rarity))
        await scene.send(f"BOSS rarity {rarity}")
        return (False, "boss", self.variant) if self.variant else (False, "boss")

    async def single_init(self, scene, rarity):
        self.calls.append(("single_init", rarity))
        if self.init_gate is not None:
            await self.init_gate.wait()
        return scene.author

    async def single_fight(self, scene, hunter, rarity, is_player_boss, player_boss, **special):
        self.calls.append(("single_fight", hunter.name, rarity))
        self.special.append(special)
        self.sources.append(items.LOOT_SOURCE.get())
        if self.fight_gate is not None:
            await self.fight_gate.wait()
        if self.fight_error is not None:
            error, self.fight_error = self.fight_error, None
            raise error
        return self.result

    async def group_init(self, scene, rarity):
        self.calls.append(("group_init", rarity))
        return [scene.author]

    async def group_fight(self, scene, players, is_player_boss, player_boss, **special):
        self.calls.append(("group_fight", len(players)))
        self.special.append(special)
        self.sources.append(items.LOOT_SOURCE.get())
        return self.result

    async def reset_end(self, bot):
        self.calls.append("reset_end")

    async def set_dead_hunters(self, bot, member):
        self.calls.append(("dead", member.name))


@pytest.fixture
def stub(monkeypatch):
    boss = StubBoss()
    modifiers = SimpleNamespace(rarity_boost=0)

    async def load_modifiers(bot):
        return {"rarity_boost": modifiers.rarity_boost}

    async def detect_boss(bot, rarity):
        boss.calls.append(("detect", rarity))

    async def give_summon_scrolls(bot, player_id, number):
        # A Zwój przywołania given back (BossCycle._refund_scroll); ``boss.refund_error`` makes it fail.
        if boss.refund_error is not None:
            raise boss.refund_error
        boss.refunds.append((int(player_id), number))
        return number

    boss.refunds, boss.refund_error = [], None
    modifiers.load_modifiers = load_modifiers
    pets = SimpleNamespace(detect_boss=detect_boss, give_summon_scrolls=give_summon_scrolls)
    for name, module in (("boss", boss), ("modifiers", modifiers), ("pets", pets)):
        monkeypatch.setitem(sys.modules, f"altermmo.features.{name}", module)
        monkeypatch.setattr(altermmo.features, name, module, raising=False)
    boss.modifiers = modifiers
    return boss


@pytest.fixture
def cycle(game_db, stub):
    bot = game_db.bot
    bot.timings = FAST
    bot.supervisor._min_backoff = 0.01
    bot.supervisor._max_backoff = 0.02
    return bot.boss_cycle


async def wait_phase(bot, *phases, timeout=3.0):
    assert await bot.state.wait_boss_phase(*phases, timeout=timeout), \
        f"boss phase stayed {bot.state.boss_phase.name}, wanted {[p.name for p in phases]}"


async def test_full_cycle_brings_a_boss(game_db, cycle, stub):
    bot = game_db.bot
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert game_db.channel.ever_sent(boss_cycle.MSG_SILENCE)
    assert game_db.channel.ever_sent(boss_cycle.MSG_WIND)
    # The channel was wiped right before the boss appeared.
    assert [m.content for m in game_db.channel.messages] == ["Zasady gry", "BOSS rarity 1"]
    assert game_db.announce_channel.contains("właśnie pojawił się potwór")
    assert game_db.announce_channel.messages[-1].content == (
        f"⚔️ Przygotujcie się! Na kanale <#{game_db.bot.ids.boss_channel}> właśnie pojawił się potwór: "
        "🟦 **rzadki boss**. Kto pierwszy stanie do walki, ten zgarnie nagrody!")   # how rare, with its symbol
    assert ("detect", 1) in stub.calls
    row = await database.read_boss_table(bot)
    assert row.respawn_started and row.rarity == 1
    assert not bot.state.busy


async def test_won_fight_resets_and_schedules_the_next_boss(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    await cycle.attack(game_db.scene(player, "!zaatakuj"))
    assert "reset_end" in stub.calls
    stub.calls.clear()
    await wait_phase(bot, BossPhase.VISIBLE)      # the next boss arrived on its own
    assert ("image", 1) in stub.calls


async def test_loser_gets_dead_role_after_the_reset(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    stub.result = FightResult(won=False, losers_to_ban=[player])
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    await cycle.attack(game_db.scene(player))
    assert stub.calls.index("reset_end") < stub.calls.index(("dead", "Ala"))
    assert bot.state.boss_phase is BossPhase.IDLE


@pytest.mark.parametrize("error", [RuntimeError("boom"), ConnectionError("no internet"),
                                   FightInterrupted("lag"), asyncio.TimeoutError()])
async def test_crashed_fight_leaves_the_boss_attackable(game_db, cycle, stub, error):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    stub.fight_error = error
    await cycle.attack(game_db.scene(player))
    assert bot.state.boss_phase is BossPhase.VISIBLE
    assert "reset_end" not in stub.calls
    await asyncio.sleep(0.05)
    assert game_db.channel.contains("walka została przerwana")
    assert any("przerwana" in line for line in bot.owner_log)
    # ...and the second attempt works.
    await cycle.attack(game_db.scene(player))
    assert bot.state.boss_phase is BossPhase.IDLE
    assert "reset_end" in stub.calls


async def test_crashed_fight_can_make_the_boss_escape_by_policy(game_db, cycle, stub):
    bot = game_db.bot
    bot.settings = dataclasses.replace(bot.settings, boss_returns_after_interrupted_fight=False)
    player = game_db.add_member("Ala")
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    stub.fight_error = RuntimeError("boom")
    await cycle.attack(game_db.scene(player))
    assert bot.state.boss_phase is BossPhase.IDLE
    assert "reset_end" in stub.calls


async def test_failing_bookkeeping_cannot_block_the_next_boss(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")

    async def broken_reset(_bot):
        raise ConnectionError("database gone")

    stub.reset_end = broken_reset
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    await cycle.attack(game_db.scene(player))
    assert bot.state.boss_phase is BossPhase.IDLE
    assert any("post-fight reset" in line for line in bot.owner_log)


async def test_only_one_attack_wins_and_others_are_told(game_db, cycle, stub):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    ola = game_db.add_member("Ola")
    stub.init_gate = asyncio.Event()
    stub.fight_gate = asyncio.Event()
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    first = asyncio.ensure_future(cycle.attack(game_db.scene(ala)))
    await asyncio.sleep(0.01)
    assert bot.state.boss_phase is BossPhase.ENGAGED
    await cycle.attack(game_db.scene(ola))            # lobby phase: silent
    assert not game_db.channel.contains("Nie możesz zaatakować bossa")
    stub.init_gate.set()
    await asyncio.sleep(0.01)
    assert bot.state.boss_phase is BossPhase.FIGHTING
    await cycle.attack(game_db.scene(ola))            # fight phase: refused
    assert game_db.channel.contains(f"Nie możesz zaatakować bossa, poczekaj na pojawienie się kolejnego <@{ola.id}>!")
    stub.fight_gate.set()
    await first
    assert [c for c in stub.calls if c[0] == "single_fight"] == [("single_fight", "Ala", 0)]


async def test_no_boss_means_no_attack(game_db, cycle, stub):
    player = game_db.add_member("Ala")
    await cycle.attack(game_db.scene(player))
    assert game_db.channel.contains("Nie możesz zaatakować bossa")
    assert not stub.calls


async def test_legendary_boss_uses_the_group_fight(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    bot.state.boss_rarity = 3
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    await cycle.attack(game_db.scene(player))
    assert ("group_init", 3) in stub.calls and ("group_fight", 1) in stub.calls


async def test_schedule_is_resumed_after_a_restart(game_db, cycle, stub):
    bot = game_db.bot
    await database.update_boss_table(bot, 2, 0, True)   # legacy/previous run left a schedule
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert ("image", 2) in stub.calls
    assert game_db.channel.ever_sent(boss_cycle.MSG_SILENCE)


async def test_cycle_survives_a_crash_while_spawning(game_db, cycle, stub):
    bot = game_db.bot
    stub.image_errors = 2                               # Discord is down for two attempts
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE, timeout=5)
    assert bot.supervisor.status()[boss_cycle.LOOP_NAME]["restarts"] == 2
    assert game_db.channel.sent_count(boss_cycle.MSG_WIND) == 1
    assert not bot.state.busy


async def test_summoned_boss_pauses_the_schedule_and_it_resumes(game_db, cycle, stub):
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    planned = cycle.arrives_at
    assert planned is not None and clock.seconds_until(planned) > 900

    assert await cycle.summon(game_db.scene(player), 4)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 4
    assert not await cycle.summon(game_db.scene(player), 4)     # one boss at a time

    await cycle.attack(game_db.scene(player))
    await wait_phase(bot, BossPhase.WAITING)
    assert cycle.arrives_at == planned
    row = await database.read_boss_table(bot)
    assert row.respawn_started and abs((row.respawn_at - planned).total_seconds()) < 5


async def test_boss_summoned_while_the_schedule_is_being_saved_keeps_the_channel(game_db, cycle, stub,
                                                                                 monkeypatch):
    """A ritual / gem boss summoned during the IDLE phase must not be wiped by the new schedule."""
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    player = game_db.add_member("Ala")
    gate, saving = asyncio.Event(), asyncio.Event()
    real_update = database.update_boss_table

    async def slow_update(bot_, rarity, seconds, started):
        if started and not gate.is_set():
            saving.set()
            await gate.wait()                                 # e.g. the database is slow right now
        return await real_update(bot_, rarity, seconds, started)

    monkeypatch.setattr(database, "update_boss_table", slow_update)
    cycle.start()
    await asyncio.wait_for(saving.wait(), 3)
    assert bot.state.boss_phase is BossPhase.IDLE
    assert await cycle.summon(game_db.scene(player), 4)
    gate.set()
    await asyncio.sleep(0.1)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 4
    planned = cycle.arrives_at
    assert planned is not None

    await cycle.attack(game_db.scene(player))                 # ...and the schedule goes on afterwards
    await wait_phase(bot, BossPhase.WAITING)
    assert cycle.arrives_at == planned
    assert ("single_fight", "Ala", 4) in stub.calls


async def _eventually(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.02)


async def test_unattacked_summoned_boss_leaves_when_the_scheduled_boss_is_due(game_db, cycle, stub):
    """Owner's rule of 2026-11-18: a ritual / gem boss does not hold the scheduled boss up."""
    bot = game_db.bot
    bot.timings = dataclasses.replace(FAST, respawn_min=1, respawn_max=1, summoned_min_stay=0.0)
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    planned = cycle.arrives_at
    assert await cycle.summon(game_db.scene(player), 4)
    assert cycle.summoned_leaves_at() == planned
    assert bot.store.runtime_get("summoned_boss_rarity") == 4

    await _eventually(lambda: game_db.channel.ever_sent("Przywołany potwór rozpływa się w powietrzu"))
    await _eventually(lambda: ("image", 1) in stub.calls)        # the scheduled boss (rarity 1) has arrived
    await wait_phase(bot, BossPhase.VISIBLE)
    assert bot.state.boss_rarity == 1 and cycle.summoned_leaves_at() is None
    assert "reset_end" not in stub.calls                         # nobody fought: modifiers and the dead stay
    assert bot.store.runtime_get("summoned_boss_rarity") is None
    assert not game_db.channel.contains("BOSS rarity 4")         # its picture is gone
    assert ("detect", 1) in stub.calls                           # pets warned about the scheduled boss as usual

    await asyncio.sleep(0.1)                                     # the scheduled boss waits for ever
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 1
    await cycle.attack(game_db.scene(player))
    assert ("single_fight", "Ala", 1) in stub.calls


async def test_summoned_boss_stays_a_minimal_time_and_while_the_scheduled_one_is_far(game_db, cycle, stub):
    bot = game_db.bot
    bot.timings = dataclasses.replace(FAST, respawn_min=1, respawn_max=1, summoned_min_stay=60.0)
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(player), 4)
    assert clock.seconds_until(cycle.summoned_leaves_at()) > 50  # later than the scheduled boss (1 s away)
    await asyncio.sleep(1.3)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 4
    await cycle.attack(game_db.scene(player))                    # ...and can still be fought
    assert ("single_fight", "Ala", 4) in stub.calls


async def test_summoned_boss_whose_fight_was_interrupted_gets_its_minimal_stay_again(game_db, cycle, stub):
    """A technical problem in the fight must not make a summoned boss vanish at once.

    It was attacked: the boss stays (``boss_returns_after_interrupted_fight``),
    with a fresh minimal stay - never leaving as the "still here" notice appears.
    """
    bot = game_db.bot
    bot.timings = dataclasses.replace(FAST, respawn_min=1, respawn_max=1, summoned_min_stay=1.0)
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(player), 4)
    stub.fight_gate = asyncio.Event()
    stub.fight_error = FightInterrupted("lag")
    fight = asyncio.ensure_future(cycle.attack(game_db.scene(player)))
    await asyncio.sleep(1.3)                     # meanwhile the scheduled boss is due, the minimal stay is over
    stub.fight_gate.set()
    await fight
    await asyncio.sleep(0.3)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 4
    assert not game_db.channel.ever_sent("Przywołany potwór rozpływa się w powietrzu")
    assert game_db.channel.contains("Boss wciąż tu jest")
    assert clock.seconds_until(cycle.summoned_leaves_at()) > 0.3
    await cycle.attack(game_db.scene(player))    # ...and the second try works
    assert stub.calls.count(("single_fight", "Ala", 4)) == 2


async def test_start_spawn_while_the_scheduled_boss_waits_does_not_make_it_leave(game_db, cycle, stub):
    """!startSpawnBoss with spawning already on restarts the loop - the scheduled boss still waits for ever."""
    bot = game_db.bot
    bot.timings = dataclasses.replace(FAST, summoned_min_stay=0.0)
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    stub.calls.clear()
    await cycle.enable()
    await asyncio.sleep(0.3)
    assert cycle.summoned_leaves_at() is None
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 1
    assert not game_db.channel.ever_sent("Przywołany potwór rozpływa się w powietrzu")
    assert ("image", 1) not in stub.calls


@pytest.mark.parametrize("admin", ["reset", "disable"])
async def test_reset_while_the_summoned_boss_leaves_is_not_undone(game_db, cycle, stub, monkeypatch, admin):
    """!ResetBoss / !stopSpawnBoss while an unattacked summoned boss is leaving (Discord is slow)."""
    bot = game_db.bot
    bot.timings = dataclasses.replace(FAST, respawn_min=1, respawn_max=1, summoned_min_stay=0.0)
    player = game_db.add_member("Ala")
    leaving, gate = asyncio.Event(), asyncio.Event()
    real_send_patient = io.send_patient

    async def send_patient(destination, content=None, **kwargs):
        if content == boss_cycle.MSG_SUMMONED_LEFT:
            leaving.set()
            await gate.wait()                    # Discord has problems right now
        return await real_send_patient(destination, content, **kwargs)

    monkeypatch.setattr(io, "send_patient", send_patient)
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(player), 4)
    await asyncio.wait_for(leaving.wait(), 5)
    bot.timings = SLOW_RESPAWN                   # a fresh cycle brings its boss much later
    stub.calls.clear()
    await getattr(cycle, admin)()
    await asyncio.sleep(0.3)
    assert ("image", 1) not in stub.calls       # no boss of the old schedule
    if admin == "reset":
        assert bot.state.boss_phase in (BossPhase.SCHEDULED, BossPhase.WAITING)
        assert clock.seconds_until(cycle.arrives_at) > 900
    else:
        assert bot.state.boss_phase is BossPhase.IDLE
    assert bot.store.runtime_get("summoned_boss_rarity") is None


async def test_start_spawn_while_the_summoned_boss_leaves_does_not_strand_the_boss(game_db, cycle, stub,
                                                                                    monkeypatch):
    """!startSpawnBoss restarts the loop in the middle of the leave: the scheduled boss still comes."""
    bot = game_db.bot
    bot.timings = dataclasses.replace(FAST, respawn_min=1, respawn_max=1, summoned_min_stay=0.0)
    player = game_db.add_member("Ala")
    leaving, gate = asyncio.Event(), asyncio.Event()
    real_send_patient = io.send_patient

    async def send_patient(destination, content=None, **kwargs):
        if content == boss_cycle.MSG_SUMMONED_LEFT and not gate.is_set():
            leaving.set()
            await gate.wait()                    # Discord has problems right now
        return await real_send_patient(destination, content, **kwargs)

    monkeypatch.setattr(io, "send_patient", send_patient)
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(player), 4)
    await asyncio.wait_for(leaving.wait(), 5)
    assert bot.state.boss_phase is BossPhase.ENGAGED          # claimed by the leave, no fight
    stub.calls.clear()
    await cycle.enable()                                      # the admin restarts the loop
    gate.set()
    await _eventually(lambda: ("image", 1) in stub.calls)     # not stuck in ENGAGED for two hours
    await wait_phase(bot, BossPhase.VISIBLE)
    assert bot.state.boss_rarity == 1 and bot.store.runtime_get("summoned_boss_rarity") is None


async def test_summoned_boss_waits_for_ever_while_spawning_is_off(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    await cycle.disable()
    assert await cycle.summon(game_db.scene(player), 4)
    assert cycle.summoned_leaves_at() is None
    await asyncio.sleep(0.1)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 4


async def test_unattacked_boss_leaves_when_a_limit_is_configured(game_db, cycle, stub):
    bot = game_db.bot
    bot.settings = dataclasses.replace(bot.settings, boss_visible_timeout=0.05)
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    bot.timings = dataclasses.replace(FAST, post_fight_pause=30.0)   # keep the next cycle parked
    await wait_phase(bot, BossPhase.SCHEDULED)
    assert game_db.channel.contains("Potwór odchodzi znudzony")
    assert "reset_end" in stub.calls


async def test_disable_and_enable(game_db, cycle, stub):
    bot = game_db.bot
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    await cycle.disable()
    assert bot.state.boss_phase is BossPhase.IDLE
    assert not (await database.read_boss_table(bot)).respawn_started
    await asyncio.sleep(0.05)
    assert bot.state.boss_phase is BossPhase.IDLE       # nothing spawns while disabled
    assert not cycle.enabled
    await cycle.enable()
    await wait_phase(bot, BossPhase.VISIBLE)


async def test_reset_recovers_from_any_state(game_db, cycle, stub):
    bot = game_db.bot
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    bot.state._busy_owner = "stuck"                      # simulate a leaked flag
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    await cycle.reset()
    assert not bot.state.busy
    assert "reset_end" in stub.calls
    await wait_phase(bot, BossPhase.VISIBLE)


def test_final_rarity_rules():
    weekday_noon = datetime(2026, 11, 17, 12, 0, tzinfo=clock.WARSAW)      # Tuesday
    weekday_evening = datetime(2026, 11, 17, 19, 0, tzinfo=clock.WARSAW)
    saturday_noon = datetime(2026, 11, 21, 12, 0, tzinfo=clock.WARSAW)
    # A legendary boss needs a party: off-peak on a weekday it becomes mythic.
    assert boss_cycle.final_rarity(3, 0, weekday_noon, debug=False) == 4
    assert boss_cycle.final_rarity(3, 0, weekday_evening, debug=False) == 3
    assert boss_cycle.final_rarity(3, 0, saturday_noon, debug=False) == 3
    assert boss_cycle.final_rarity(3, 0, weekday_noon, debug=True) == 3
    # Shrine boost, capped at mythic.
    assert boss_cycle.final_rarity(1, 1, weekday_noon, debug=False) == 2
    assert boss_cycle.final_rarity(2, 1, weekday_evening, debug=False) == 3
    assert boss_cycle.final_rarity(2, 5, weekday_evening, debug=False) == 4


async def test_summoned_boss_comes_back_after_a_restart(game_db, cycle, stub):
    """A ritual / gem boss lives only in memory; the bot remembers it across a restart."""
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(player), 4)
    assert bot.store.runtime_get("summoned_boss_rarity") == 4

    # "Restart": a new cycle object reading the same database and data folder.
    bot.supervisor.stop(boss_cycle.LOOP_NAME)
    await bot.state.set_boss_phase(BossPhase.IDLE)
    stub.calls.clear()
    bot.boss_cycle = boss_cycle.BossCycle(bot)
    bot.boss_cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert bot.state.boss_rarity == 4 and ("image", 4) in stub.calls
    planned = bot.boss_cycle.arrives_at

    await bot.boss_cycle.attack(game_db.scene(player))
    assert bot.store.runtime_get("summoned_boss_rarity") is None      # dealt with: not brought back again
    await wait_phase(bot, BossPhase.WAITING)
    assert bot.boss_cycle.arrives_at == planned                       # ...and the scheduled boss still comes


async def test_a_summoned_boss_keeps_its_origin_across_a_restart_and_its_loot_is_a_summoned_ones(game_db, cycle,
                                                                                                 stub):
    """(Items, 2026-11-26) ``BossInfo.origin`` and its own key in runtime.json (the rarity stays a plain int)."""
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(player), 4, origin="scroll")
    assert bot.state.boss.origin == "scroll"
    assert bot.store.runtime_get("summoned_boss_rarity") == 4
    info = bot.store.runtime_get("summoned_boss_info")
    assert info["origin"] == "scroll" and abs((datetime.fromisoformat(info["at"]) - clock.utcnow()).total_seconds()) < 5

    # "Restart" as in test_summoned_boss_comes_back_after_a_restart: the origin comes back with the boss.
    bot.supervisor.stop(boss_cycle.LOOP_NAME)
    await bot.state.set_boss_phase(BossPhase.IDLE)
    bot.boss_cycle = boss_cycle.BossCycle(bot)
    bot.boss_cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert bot.state.boss_rarity == 4 and bot.state.boss.origin == "scroll"
    await bot.boss_cycle.attack(game_db.scene(player))
    assert stub.sources == ["summoned"] and items.LOOT_SOURCE.get() is None
    assert bot.store.runtime_get("summoned_boss_rarity") is None and bot.store.runtime_get("summoned_boss_info") is None
    await wait_phase(bot, BossPhase.WAITING)

    # A gem boss (and any summon that names no origin) is summoned as well; an unknown origin is none.
    assert await bot.boss_cycle.summon(game_db.scene(player), 2, origin="nonsense")
    assert bot.state.boss.origin == "" and bot.store.runtime_get("summoned_boss_info")["origin"] == ""
    await bot.boss_cycle.attack(game_db.scene(player))
    assert stub.sources == ["summoned", "summoned"]


async def test_a_scheduled_boss_draws_the_loot_of_a_scheduled_boss(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert bot.state.boss.origin == "" and bot.store.runtime_get("summoned_boss_info") is None
    await cycle.attack(game_db.scene(player))
    assert stub.sources == [None]                              # (boss.rand_loot's default: "boss")


async def test_the_summoning_paths_name_their_origin():
    """The scroll, the gem and the ritual say how their boss came (BossCycle.summon's ``origin``)."""
    import inspect

    from altermmo.cogs import events as events_cog
    from altermmo.features import missions, skills

    assert boss_cycle.SUMMON_ORIGINS == ("", "scroll", "gem", "ritual")
    assert inspect.signature(boss_cycle.BossCycle.summon).parameters["origin"].default == ""
    assert 'summon(scene, SUMMON_RARITY, origin="scroll")' in inspect.getsource(missions._read_scroll)
    assert 'summon(scene, rarity, origin="gem")' in inspect.getsource(skills)
    assert 'origin="ritual"' in inspect.getsource(events_cog)


async def test_switching_spawning_on_does_not_wipe_a_summoned_boss(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    await cycle.disable()
    assert await cycle.summon(game_db.scene(player), 2)               # e.g. a skill gem while spawning is off
    await cycle.enable()
    await asyncio.sleep(0.1)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss_rarity == 2
    await cycle.attack(game_db.scene(player))
    assert ("single_fight", "Ala", 2) in stub.calls


async def test_gem_boss_never_barges_into_an_activity_or_a_sign_up(game_db, cycle, stub):
    from altermmo.core.state import EventType

    bot = game_db.bot
    state = bot.state
    player = game_db.add_member("Ala")
    with state.hold_busy("hunt"):
        assert not await cycle.summon(game_db.scene(player), 4)
    with state.running_event(EventType.RITUAL, exclusive=False):      # the open sign-up of a ritual
        assert not await cycle.summon(game_db.scene(player), 4)
        assert state.event_alive and state.event_type is EventType.RITUAL   # ...which is left alone
    assert stub.calls == [] and state.boss_phase is BossPhase.IDLE

    # (Owner, 2026-11-26) an open passive event is waited for, not closed: the merchant's cart, the
    # mine, an unclaimed chest, the shrine - nothing is posted, the event stays open.
    for event in (EventType.MERCHANT, EventType.DUNGEON, EventType.CHEST, EventType.SHRINE):
        state.open_event(event)
        assert not await cycle.summon(game_db.scene(player), 4)
        assert state.event_alive and state.event_type is event
    assert stub.calls == [] and state.boss_phase is BossPhase.IDLE
    state.close_event()

    busy_while_posting = []
    real_image = stub.send_boss_image

    async def send_boss_image(scene, rarity):
        busy_while_posting.append(state.busy_owner)
        return await real_image(scene, rarity)

    stub.send_boss_image = send_boss_image
    assert await cycle.summon(game_db.scene(player), 4)
    assert busy_while_posting == ["boss-summon"] and not state.busy and not state.event_alive
    assert state.boss_phase is BossPhase.VISIBLE


# -- the boss of a Zwój przywołania (items_decisions.md §1, 2026-11-26) ----------------------------

async def restart(bot) -> None:
    """"Restart" as in test_summoned_boss_comes_back_after_a_restart: a new cycle on the same data folder."""
    bot.supervisor.stop(boss_cycle.LOOP_NAME)
    await bot.state.set_boss_phase(BossPhase.IDLE)
    bot.boss_cycle = boss_cycle.BossCycle(bot)
    bot.boss_cycle.start()


def note(bot) -> dict | None:
    return bot.store.runtime_get("summoned_boss_info")


def backdate(bot, minutes: float) -> None:
    """The scroll boss's stay began ``minutes`` ago (in runtime.json and in memory)."""
    info = note(bot)
    began = clock.utcnow() - timedelta(minutes=minutes)
    info["at"] = began.isoformat()
    bot.store.runtime_set("summoned_boss_info", info)
    bot.boss_cycle._summoned_at = began


async def test_an_unattacked_scroll_boss_leaves_after_its_half_hour_and_the_scroll_is_lost(game_db, cycle, stub):
    bot = game_db.bot
    bot.timings = dataclasses.replace(SLOW_RESPAWN, scroll_boss_stay=0.3)
    ala = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    planned = cycle.arrives_at
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    assert note(bot) == {"origin": "scroll", "at": note(bot)["at"], "by": ala.id, "claimed": False}
    notice = game_db.announce_channel.messages[-1]
    assert notice.content == boss_cycle.NOTICE_SCROLL.format(name="Ala", channel=bot.ids.boss_channel)
    assert clock.seconds_until(cycle.summoned_leaves_at()) < 1 < clock.seconds_until(planned)   # not the schedule's

    await _eventually(lambda: game_db.channel.ever_sent(boss_cycle.MSG_SCROLL_BOSS_LEFT))
    await wait_phase(bot, BossPhase.WAITING)                     # (the quiet hour was over: the schedule goes on)
    assert cycle.arrives_at == planned and bot.state.boss.origin == "" and bot.state.boss_rarity == 1
    assert notice.deleted and note(bot) is None and bot.store.runtime_get("summoned_boss_rarity") is None
    assert stub.refunds == [] and "reset_end" not in stub.calls  # the scroll is lost; nobody fought
    assert not game_db.channel.ever_sent(boss_cycle.MSG_SUMMONED_LEFT)
    assert not game_db.channel.contains("BOSS rarity 4")


async def test_a_scroll_boss_leaving_in_the_quiet_hour_leaves_the_silence_to_come(game_db, cycle, stub):
    """It leaves at its own time: back to SCHEDULED while the quiet hour lasts (its "silence" still to come)."""
    bot = game_db.bot
    bot.timings = dataclasses.replace(SLOW_RESPAWN, post_fight_pause=60.0, scroll_boss_stay=0.3)
    ala = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.SCHEDULED)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    await _eventually(lambda: game_db.channel.ever_sent(boss_cycle.MSG_SCROLL_BOSS_LEFT))
    await wait_phase(bot, BossPhase.SCHEDULED)
    await asyncio.sleep(0.1)
    assert bot.state.boss_phase is BossPhase.SCHEDULED and not game_db.channel.ever_sent(boss_cycle.MSG_SILENCE)


async def test_where_the_schedule_stands_after_a_scroll_boss(game_db, cycle, stub):
    now = clock.utcnow()
    cycle.arrives_at, cycle.quiet_until = now + timedelta(hours=1), now + timedelta(minutes=5)
    assert cycle._phase_after_scroll_boss() is BossPhase.SCHEDULED
    cycle.quiet_until = now - timedelta(minutes=5)
    assert cycle._phase_after_scroll_boss() is BossPhase.WAITING
    cycle.quiet_until = None
    assert cycle._phase_after_scroll_boss() is BossPhase.WAITING
    cycle.arrives_at = None                                      # no schedule at all: a new one is drawn
    assert cycle._phase_after_scroll_boss() is BossPhase.IDLE


async def test_the_scroll_boss_stays_half_an_hour_whatever_the_schedule_says(game_db, cycle, stub):
    bot = game_db.bot
    bot.timings = dataclasses.replace(FAST, respawn_min=1, respawn_max=1)
    ala = game_db.add_member("Ala")
    assert bot.timings.scroll_boss_stay == 1800.0
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)                     # the scheduled boss is due in a second
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    assert abs(clock.seconds_until(cycle.summoned_leaves_at()) - 1800) < 5
    await asyncio.sleep(1.3)                                     # the scheduled boss waits
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss.origin == "scroll"
    assert ("image", 1) not in stub.calls


def test_the_half_hour_is_a_timing_of_its_own():
    from altermmo import settings

    assert settings.Timings().scroll_boss_stay == 1800.0 and settings.Timings().summoned_min_stay == 600.0
    assert 0 < settings._DEBUG_TIMINGS.scroll_boss_stay < 1800.0              # debug mode shortens it too


async def test_a_scroll_boss_is_refused_while_spawning_is_off(game_db, cycle, stub):
    """Only the running loop sends it away after its half hour; the gem keeps today's rule."""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await cycle.disable()
    assert not await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    assert stub.calls == [] and bot.state.boss_phase is BossPhase.IDLE and note(bot) is None
    assert await cycle.summon(game_db.scene(ala), 4, origin="gem")


async def test_a_restart_never_makes_a_scroll_boss_stay_longer(game_db, cycle, stub):
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN                                   # (the stay: 30 min, at least 10 more after a restart)
    ala = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    # max(when it came, now - 30 min + 10 min): what is left of the half hour, but at least 10 minutes.
    for minutes_ago, left in ((15, 15), (25, 10), (40, 10), (0, 30)):
        backdate(bot, minutes_ago)
        await restart(bot)
        await wait_phase(bot, BossPhase.VISIBLE)
        assert bot.state.boss.origin == "scroll" and bot.state.boss_rarity == 4
        assert abs(clock.seconds_until(bot.boss_cycle.summoned_leaves_at()) - left * 60) < 5, minutes_ago
        info = note(bot)
        assert info["by"] == ala.id and info["claimed"] is False
        assert abs(clock.seconds_until(datetime.fromisoformat(info["at"])) + (30 - left) * 60) < 5
        # A fresh notice, naming the reader (the scene of a restart has no author: the note says who).
        assert game_db.announce_channel.messages[-1].content == boss_cycle.NOTICE_SCROLL.format(
            name="Ala", channel=bot.ids.boss_channel)
    assert stub.refunds == []


async def test_an_interrupted_fight_gives_a_fresh_half_hour_and_the_boss_counts_as_claimed(game_db, cycle, stub):
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    backdate(bot, 25)
    stub.fight_error = FightInterrupted("lag")
    await cycle.attack(game_db.scene(ola))
    assert bot.state.boss_phase is BossPhase.VISIBLE
    assert abs(clock.seconds_until(cycle.summoned_leaves_at()) - 1800) < 5
    info = note(bot)
    assert info["claimed"] is True and abs(clock.seconds_until(datetime.fromisoformat(info["at"]))) < 5
    await cycle.reset()                                          # claimed once: never refunded
    assert stub.refunds == []
    await cycle.disable()


@pytest.mark.parametrize("admin, why", [("reset", boss_cycle.WHY_RESET), ("disable", boss_cycle.WHY_STOPPED)])
async def test_an_unclaimed_scroll_boss_gives_its_scroll_back_on_reset_and_stop(game_db, cycle, stub, admin, why):
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    ala = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    notice = game_db.announce_channel.messages[-1]
    await getattr(cycle, admin)()
    assert stub.refunds == [(ala.id, 1)]
    assert boss_cycle.LOG_SCROLL_REFUNDED.format(player_id=ala.id, why=why) in bot.owner_log
    assert game_db.channel.ever_sent(boss_cycle.MSG_SCROLL_REFUNDED.format(player_id=ala.id))
    assert note(bot) is None and notice.deleted
    await getattr(cycle, admin)()                                # once: the note is gone
    assert stub.refunds == [(ala.id, 1)]
    await cycle.disable()


@pytest.mark.parametrize("origin", ["gem", "ritual", ""])
async def test_other_summoned_bosses_give_nothing_back(game_db, cycle, stub, origin):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    assert await cycle.summon(game_db.scene(ala), 4, origin=origin, show_image=origin != "ritual")
    await cycle.reset()
    assert stub.refunds == []
    await cycle.disable()


async def test_a_scroll_boss_being_fought_gives_nothing_back(game_db, cycle, stub):
    """!ResetBoss during the fight: no refund (and the fight's loot is the fight's business)."""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    for phase in (BossPhase.ENGAGED, BossPhase.FIGHTING):
        await bot.state.set_boss_phase(phase)
        assert cycle._owed_refund() is None
    await cycle.reset()
    assert stub.refunds == []
    await cycle.disable()


async def test_a_refund_that_fails_is_told_to_the_owner(game_db, cycle, stub):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    stub.refund_error = RuntimeError("test: no database")
    with bot.state.hold_busy("hunt"):                            # (a hunt runs: the player is not told now)
        await cycle.disable()
    assert bot.owner_log == [boss_cycle.LOG_SCROLL_NOT_REFUNDED.format(player_id=ala.id, why=boss_cycle.WHY_STOPPED,
                                                                       error="RuntimeError")]
    assert game_db.log_channel.messages[-1].content.startswith(f"<@{bot.ids.owner}>!")
    assert not game_db.channel.ever_sent("wraca cały i zdrowy")


async def test_the_player_hears_of_his_refund_only_while_the_channel_is_quiet(game_db, cycle, stub):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    with bot.state.hold_busy("hunt"):
        await cycle.disable()
    assert stub.refunds == [(ala.id, 1)] and bot.owner_log == [
        boss_cycle.LOG_SCROLL_REFUNDED.format(player_id=ala.id, why=boss_cycle.WHY_STOPPED)]
    assert not game_db.channel.ever_sent(boss_cycle.MSG_SCROLL_REFUNDED.format(player_id=ala.id))


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("why", ["discord down", "busy"])
async def test_a_scroll_boss_that_cannot_come_back_after_a_restart(game_db, cycle, stub, claimed, why):
    """A failed restore (or a channel taken at the restore): the scroll goes back - unless it was claimed."""
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    ala = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    if claimed:
        bot.boss_cycle._amend_summoned(claimed=True)
    if why == "discord down":
        stub.image_errors = 1
    else:
        bot.state._busy_owner = "hunt"
    await restart(bot)
    await _eventually(lambda: note(bot) is None)
    await asyncio.sleep(0.05)
    assert bot.store.runtime_get("summoned_boss_rarity") is None
    assert stub.refunds == ([] if claimed else [(ala.id, 1)])
    if not claimed:
        assert boss_cycle.LOG_SCROLL_REFUNDED.format(player_id=ala.id, why=boss_cycle.WHY_NOT_BACK) in bot.owner_log
    bot.state._busy_owner = None
    await bot.boss_cycle.disable()


async def test_spawning_off_at_the_start_gives_the_scroll_back(game_db, cycle, stub):
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    ala = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    bot.store.runtime_set("boss_spawning_enabled", False)        # (switched off in the file: no !stopSpawnBoss)
    await restart(bot)
    await _eventually(lambda: stub.refunds)
    assert stub.refunds == [(ala.id, 1)] and note(bot) is None
    assert boss_cycle.LOG_SCROLL_REFUNDED.format(player_id=ala.id, why=boss_cycle.WHY_SPAWNING_OFF) in bot.owner_log

    # A gem boss waits for !startSpawnBoss, as it always did.
    bot.store.runtime_set("summoned_boss_rarity", 4)
    bot.store.runtime_set("summoned_boss_info", {"origin": "gem", "at": clock.utcnow().isoformat(), "by": ala.id,
                                                 "claimed": False})
    await restart(bot)
    await asyncio.sleep(0.2)
    assert stub.refunds == [(ala.id, 1)] and note(bot)["origin"] == "gem"
    bot.supervisor.stop(boss_cycle.LOOP_NAME)


async def test_a_scroll_owed_since_the_start_is_paid_once(game_db, cycle, stub):
    """``runtime.json`` was too old to trust (AlterBot._review_saved_state): the cycle pays the scroll back."""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    bot.store.runtime_set(boss_cycle.KEY_REFUND_DUE, {"by": ala.id, "why": boss_cycle.WHY_STALE})
    cycle.start()
    await _eventually(lambda: stub.refunds)
    assert stub.refunds == [(ala.id, 1)] and bot.store.runtime_get(boss_cycle.KEY_REFUND_DUE) is None
    assert boss_cycle.LOG_SCROLL_REFUNDED.format(player_id=ala.id, why=boss_cycle.WHY_STALE) in bot.owner_log
    await cycle._pay_refund_due()
    assert stub.refunds == [(ala.id, 1)]


def test_which_boss_owes_its_scroll():
    assert boss_cycle.scroll_refund_owed({"origin": "scroll", "by": 7, "claimed": False}) == {"by": 7}
    for info in ({"origin": "scroll", "by": 7, "claimed": True}, {"origin": "gem", "by": 7}, {"origin": "scroll"},
                 {"origin": "scroll", "by": "nonsense"}, {"origin": "scroll", "by": 0}, None, 4):
        assert boss_cycle.scroll_refund_owed(info) is None, info


async def test_every_summoned_boss_is_announced_on_the_general_chat(game_db, cycle, stub, monkeypatch):
    """After ``BossInfo`` is set, on every path, without a mention, the name escaped; it goes with its boss."""
    bot = game_db.bot
    chat = game_db.announce_channel
    channel = bot.ids.boss_channel
    mentions = []
    real_send = io.send

    async def send(destination, content=None, **kwargs):
        if destination is chat:
            mentions.append(kwargs.get("allowed_mentions"))
        return await real_send(destination, content, **kwargs)

    monkeypatch.setattr(io, "send", send)
    star = game_db.add_member("*Ala_*")
    for kwargs, expected in (
            ({"origin": "scroll"}, boss_cycle.NOTICE_SCROLL.format(name=r"\*Ala\_\*", channel=channel)),
            ({"origin": "gem"}, boss_cycle.NOTICE_GEM.format(channel=channel, emoji="🟥", rarity="mityczny")),
            ({"origin": "ritual", "show_image": False},
             boss_cycle.NOTICE_RITUAL.format(channel=channel, emoji="🟥", rarity="mityczny")),
            ({}, boss_cycle.spawn_notice(channel, 4))):
        assert await cycle.summon(game_db.scene(star), 4, **kwargs)
        notice = chat.messages[-1]
        assert notice.content == expected and boss_cycle.is_spawn_notice(notice.content, channel)
        assert mentions[-1] is not None and mentions[-1].users is False and mentions[-1].everyone is False
        await cycle.attack(game_db.scene(star))                 # fought: the notice goes with it
        assert notice.deleted and chat.messages == []
    gem = boss_cycle.summon_notice(channel, 2, "gem")
    assert "🟪 **epicki boss**" in gem and boss_cycle.summon_notice(channel, 2, "nonsense") == \
        boss_cycle.spawn_notice(channel, 2)
    assert boss_cycle.summon_notice(channel, 4, "scroll", "") == boss_cycle.NOTICE_SCROLL.format(
        name="Ktoś", channel=channel)


# -- special bosses (features/bossvariants.py): the variant goes with the boss into its fight ------

async def test_scheduled_special_boss_keeps_its_variant_until_the_fight(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    stub.variant = "krwawy-ksiezyc"
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert (bot.state.boss.rarity, bot.state.boss.variant) == (1, "krwawy-ksiezyc")
    assert bot.state.snapshot()["boss_variant"] == "krwawy-ksiezyc"
    stub.variant = ""                                             # (whatever is drawn later is another boss)
    await cycle.attack(game_db.scene(player, "!zaatakuj"))
    assert ("single_fight", "Ala", 1) in stub.calls
    assert stub.special == [{"variant": "krwawy-ksiezyc"}]
    assert bot.state.boss.variant == ""                           # gone with the boss


async def test_ordinary_boss_is_fought_by_the_old_call(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert bot.state.boss.variant == ""
    await cycle.attack(game_db.scene(player, "!zaatakuj"))
    assert stub.special == [{}]                                   # no keyword at all


async def test_special_boss_keeps_its_variant_through_an_interrupted_fight(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    bot.state.boss = BossInfo(rarity=1, variant="smocza-wylinka")
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    stub.fight_error = FightInterrupted("connection lost")
    await cycle.attack(game_db.scene(player))
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss.variant == "smocza-wylinka"
    await cycle.attack(game_db.scene(player))                     # the same boss, the same promise
    assert stub.special == [{"variant": "smocza-wylinka"}] * 2


async def test_summoned_special_boss_keeps_its_variant(game_db, cycle, stub):
    """A skill gem boss: the picture posted by the cycle decides, also for a legendary party."""
    bot = game_db.bot
    player = game_db.add_member("Ala")
    stub.variant = "widmowe-okowy"
    assert await cycle.summon(game_db.scene(player), 3)
    assert (bot.state.boss.rarity, bot.state.boss.variant) == (3, "widmowe-okowy")
    await cycle.attack(game_db.scene(player))
    assert ("group_fight", 1) in stub.calls and stub.special == [{"variant": "widmowe-okowy"}]

    # The next one is ordinary: nothing of the previous boss is left.
    stub.variant = ""
    assert await cycle.summon(game_db.scene(player), 2)
    assert bot.state.boss.variant == ""
    await cycle.attack(game_db.scene(player))
    assert stub.special[-1] == {}


async def test_ritual_boss_brings_the_variant_it_was_shown_as(game_db, cycle, stub):
    """``show_image=False``: the ritual showed the boss itself and says what it showed."""
    bot = game_db.bot
    player = game_db.add_member("Ala")
    assert await cycle.summon(game_db.scene(player), 4, show_image=False, variant="krwawy-ksiezyc")
    assert not [call for call in stub.calls if call[0] == "image"]
    assert (bot.state.boss.rarity, bot.state.boss.variant) == (4, "krwawy-ksiezyc")
    await cycle.attack(game_db.scene(player))
    assert ("single_fight", "Ala", 4) in stub.calls and stub.special == [{"variant": "krwawy-ksiezyc"}]

    assert await cycle.summon(game_db.scene(player), 4, show_image=False)   # an ordinary ritual boss
    assert bot.state.boss.variant == ""
    assert await cycle.summon(game_db.scene(player), 4, show_image=False) is False                 # (one at a time)


@pytest.mark.parametrize("not_a_text", [None, 7, ("krwawy-ksiezyc",)])
async def test_ritual_boss_with_a_variant_that_is_no_text_is_ordinary(game_db, cycle, stub, not_a_text):
    """Whatever a caller hands over as the variant, BossInfo holds a text - '' = an ordinary boss."""
    bot = game_db.bot
    player = game_db.add_member("Ala")
    assert await cycle.summon(game_db.scene(player), 4, show_image=False, variant=not_a_text)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss.variant == ""
    await cycle.attack(game_db.scene(player))
    assert ("single_fight", "Ala", 4) in stub.calls and stub.special == [{}]


@pytest.mark.parametrize("before, after", [("krwawy-ksiezyc", ""), ("", "widmowe-okowy"),
                                           ("krwawy-ksiezyc", "smocza-wylinka")])
async def test_restart_rolls_the_variant_anew_with_the_picture(game_db, cycle, stub, before, after):
    """The variant lives only in memory: after a restart it is what the picture shown AGAIN says.

    Never a leftover of the previous picture (a framed boss without its bonus, or a bonus
    behind an ordinary picture).
    """
    bot = game_db.bot
    bot.timings = SLOW_RESPAWN
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.WAITING)
    stub.variant = before
    assert await cycle.summon(game_db.scene(player), 4)
    assert bot.state.boss.variant == before

    # "Restart" as in test_summoned_boss_comes_back_after_a_restart (the same bot.state: a stale
    # variant would show).
    bot.supervisor.stop(boss_cycle.LOOP_NAME)
    await bot.state.set_boss_phase(BossPhase.IDLE)
    stub.calls.clear()
    stub.variant = after
    bot.boss_cycle = boss_cycle.BossCycle(bot)
    bot.boss_cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert ("image", 4) in stub.calls and bot.state.boss.rarity == 4
    assert bot.state.boss.variant == after
    await bot.boss_cycle.attack(game_db.scene(player))
    assert stub.special == ([{"variant": after}] if after else [{}])


async def test_switching_spawning_on_keeps_the_variant_of_a_summoned_boss(game_db, cycle, stub):
    """!startSpawnBoss restarts the loop, not the boss: its picture stays, so does its variant."""
    bot = game_db.bot
    player = game_db.add_member("Ala")
    await cycle.disable()
    stub.variant = "widmowe-okowy"
    assert await cycle.summon(game_db.scene(player), 2)
    stub.variant = ""                                             # (a picture posted now would be ordinary)
    stub.calls.clear()
    await cycle.enable()
    await asyncio.sleep(0.1)
    assert bot.state.boss_phase is BossPhase.VISIBLE
    assert (bot.state.boss.rarity, bot.state.boss.variant) == (2, "widmowe-okowy")
    assert not [call for call in stub.calls if call[0] == "image"]
    await cycle.attack(game_db.scene(player))
    assert ("single_fight", "Ala", 2) in stub.calls and stub.special == [{"variant": "widmowe-okowy"}]


async def test_start_spawn_keeps_the_variant_of_the_scheduled_boss(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    stub.variant = "smocza-wylinka"
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    stub.variant = ""
    stub.calls.clear()
    await cycle.enable()
    await asyncio.sleep(0.3)
    assert bot.state.boss_phase is BossPhase.VISIBLE
    assert (bot.state.boss.rarity, bot.state.boss.variant) == (1, "smocza-wylinka")
    assert not [call for call in stub.calls if call[0] == "image"]
    await cycle.attack(game_db.scene(player))
    assert stub.special == [{"variant": "smocza-wylinka"}]


# -- after a start of the bot: what the previous run left behind goes (owner's request) ----------

async def test_spawn_notice_names_the_boss_that_really_appears(game_db, cycle, stub):
    """The shrine made the boss rarer: the notice shows the final rarity, like the picture."""
    bot = game_db.bot
    stub.modifiers.rarity_boost = 1                              # base rarity 1 -> an epic boss
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    assert game_db.channel.messages[-1].content == "BOSS rarity 2"
    notice = game_db.announce_channel.messages[-1].content
    assert "🟪 **epicki boss**" in notice and "rzadki" not in notice
    assert boss_cycle.is_spawn_notice(notice, bot.ids.boss_channel)


OLD_NOTICE = "Na kanale <#{channel}> pojawił się właśnie potwór! Zabijcie go, żeby zgarnąć nagrody!"


def test_spawn_notices_are_recognised_in_both_wordings():
    channel = 970684202880204831
    assert boss_cycle.is_spawn_notice(boss_cycle.spawn_notice(channel), channel)
    assert boss_cycle.is_spawn_notice(OLD_NOTICE.format(channel=channel), channel)      # the old bot's text
    assert not boss_cycle.is_spawn_notice(boss_cycle.spawn_notice(123), channel)        # another channel
    assert not boss_cycle.is_spawn_notice("Na kanale pojawił się potwór", channel)      # no channel link
    assert not boss_cycle.is_spawn_notice(f"Zapraszam na <#{channel}>!", channel)
    assert not boss_cycle.is_spawn_notice(None, channel)


def test_spawn_notice_names_the_rarity_with_its_symbol():
    """Owner's request: the notice says how rare the boss is and shows the symbol of that rarity."""
    channel = 970684202880204831
    expected = {0: "🟩 **zwykły boss**", 1: "🟦 **rzadki boss**", 2: "🟪 **epicki boss**",
                3: "🟧 **legendarny boss**", 4: "🟥 **mityczny boss**"}
    for rarity, words in expected.items():
        notice = boss_cycle.spawn_notice(channel, rarity)
        assert words in notice and f"<#{channel}>" in notice
        assert boss_cycle.is_spawn_notice(notice, channel)          # still found (and removed) later
        assert boss_cycle.rarity_emoji(rarity) == words.split()[0]
    # A legendary boss needs a party; every other one goes to whoever attacks first.
    assert boss_cycle.spawn_notice(channel, 3).endswith("Sam nikt mu nie podoła - zbierzcie drużynę!")
    assert all(boss_cycle.spawn_notice(channel, rarity).endswith("ten zgarnie nagrody!") for rarity in (0, 1, 2, 4))
    # No rarity (or one nobody knows): the notice as it was.
    assert boss_cycle.spawn_notice(channel) == boss_cycle.spawn_notice(channel, 99) == (
        f"⚔️ Przygotujcie się! Na kanale <#{channel}> właśnie pojawił się potwór. "
        "Kto pierwszy stanie do walki, ten zgarnie nagrody!")
    assert boss_cycle.rarity_emoji(99) == "🟧"                      # shown as legendary, like the boss embed
    assert len(set(boss_cycle.RARITY_EMOJI.values())) == 5 and set(boss_cycle.RARITY_EMOJI) == set(boss_cycle.RARITY_NAMES)


async def test_start_clears_the_boss_channel_but_keeps_pins_and_the_oldest_post(game_db, cycle, stub):
    game = game_db
    ala = game.add_member("Ala")
    rules = game.channel.messages[0]                                # "Zasady gry"
    panel = await game.channel.send("Panel gry")
    await panel.pin()
    await game.channel.send("BOSS rarity 2")                        # a picture whose boss is not attackable
    game.say(ala, "!zaatakuj")
    await game.channel.send("1. Boss szarżuje na Ciebie! Wpisz **U N I K**")   # half a fight
    await cycle.tidy_after_start()
    assert game.channel.messages == [rules, panel]
    assert not game.bot.state.busy                                  # the hold for the purge is released


async def test_start_does_not_clear_under_something_that_already_runs(game_db, cycle, stub):
    game = game_db
    await game.channel.send("Potwór!")
    with game.bot.state.hold_busy("hunt"):
        await cycle.tidy_after_start()
    assert game.channel.contains("Potwór!")


async def test_tidy_happens_once_per_start_not_at_every_restart_of_the_loop(game_db, cycle, stub, monkeypatch):
    game = game_db
    calls = []
    real = cycle.tidy_after_start

    async def tidy():
        calls.append(game.bot.state.boss_phase.name)
        await real()

    monkeypatch.setattr(cycle, "tidy_after_start", tidy)
    game.bot.timings = SLOW_RESPAWN
    cycle.start()
    await wait_phase(game.bot, BossPhase.SCHEDULED, BossPhase.WAITING)
    assert calls == ["IDLE"]                                         # before the schedule is touched
    await cycle.enable()                                             # !startSpawnBoss restarts the loop
    await asyncio.sleep(0.05)
    await cycle.reset()                                              # !ResetBoss as well
    await asyncio.sleep(0.05)
    assert calls == ["IDLE"]                                         # the loop restarted twice: no second tidy


async def test_tidy_never_raises(game_db, cycle, stub, monkeypatch):
    game = game_db

    async def broken(channel, **kwargs):
        raise RuntimeError("discord is down")

    monkeypatch.setattr(io, "clear_channel", broken)
    monkeypatch.setattr(game.bot, "text_channel", lambda channel_id: (_ for _ in ()).throw(RuntimeError("no cache")))
    await cycle.tidy_after_start()                                   # logged, not raised
    assert not game.bot.state.busy


async def test_start_removes_left_over_boss_notices_from_the_general_chat(game_db, cycle, stub):
    game = game_db
    bot = game.bot
    chat = game.announce_channel
    ala = game.add_member("Ala")
    boss_channel = bot.ids.boss_channel
    old = await chat.send(OLD_NOTICE.format(channel=boss_channel))               # left by the old bot
    new = await chat.send(boss_cycle.spawn_notice(boss_channel))                 # left by a previous run
    other = await chat.send("Ranking za tydzień polowań został zresetowany.")    # the bot's, but not a notice
    quote = game.say(ala, OLD_NOTICE.format(channel=boss_channel), channel=chat)  # a player quoting it
    pinned = await chat.send(boss_cycle.spawn_notice(boss_channel))
    await pinned.pin()
    await cycle.tidy_after_start()
    assert old.deleted and new.deleted
    assert chat.messages == [other, quote, pinned]                               # nothing else is touched


async def test_notice_on_the_general_chat_goes_when_the_boss_is_gone(game_db, cycle, stub):
    game = game_db
    bot = game.bot
    player = game.add_member("Ala")
    bot.timings = dataclasses.replace(FAST, respawn_min=1000, respawn_max=1000, post_fight_pause=0.0)
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    cycle.base_rarity = 1
    await cycle._phase_pre_spawn()                                   # the boss appears and is announced
    assert bot.state.boss_phase is BossPhase.VISIBLE
    notice = game.announce_channel.messages[-1]
    assert boss_cycle.is_spawn_notice(notice.content, bot.ids.boss_channel)
    assert bot.store.runtime_get(boss_cycle._KEY_SPAWN_NOTICE) == {
        "channel": game.announce_channel.id, "message": notice.id}

    stub.fight_error = FightInterrupted("lag")                       # an interrupted fight: the boss stays...
    await cycle.attack(game.scene(player))
    assert bot.state.boss_phase is BossPhase.VISIBLE and not notice.deleted      # ...and so does its notice

    await cycle.attack(game.scene(player))                           # fought: the boss is gone
    assert notice.deleted and game.announce_channel.messages == []
    assert bot.store.runtime_get(boss_cycle._KEY_SPAWN_NOTICE) is None


async def test_notice_is_found_again_after_a_restart(game_db, cycle, stub):
    """The process restarted while a boss stood there: the remembered notice is still removed."""
    game = game_db
    bot = game.bot
    notice = await game.announce_channel.send(boss_cycle.spawn_notice(bot.ids.boss_channel))
    bot.store.runtime_set(boss_cycle._KEY_SPAWN_NOTICE,
                          {"channel": game.announce_channel.id, "message": notice.id})
    fresh = boss_cycle.BossCycle(bot)                                # a new process knows no message object
    await fresh.remove_spawn_notice()
    assert notice.deleted and bot.store.runtime_get(boss_cycle._KEY_SPAWN_NOTICE) is None
    await fresh.remove_spawn_notice()                                # nothing left: no error
    bot.store.runtime_set(boss_cycle._KEY_SPAWN_NOTICE, {"channel": 1, "message": 2})   # an unknown channel
    await fresh.remove_spawn_notice()
    assert bot.store.runtime_get(boss_cycle._KEY_SPAWN_NOTICE) is None


@pytest.mark.parametrize("command", ["reset", "disable"])
async def test_reset_and_stop_take_the_notice_away(game_db, cycle, stub, command):
    game = game_db
    bot = game.bot
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    await cycle._phase_pre_spawn()
    notice = game.announce_channel.messages[-1]
    await getattr(cycle, command)()
    assert notice.deleted
    await cycle.disable()                                            # (stop the loop a reset restarted)


async def test_start_brings_the_pinned_panel_up_to_date(game_db, cycle, stub, monkeypatch):
    from altermmo.features import hub

    refreshed = []

    async def refresh_panel(bot):
        refreshed.append(bot)
        return True

    monkeypatch.setattr(hub, "refresh_panel", refresh_panel)
    await cycle.tidy_after_start()
    assert refreshed == [game_db.bot]
