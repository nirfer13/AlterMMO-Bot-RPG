"""The boss life-cycle with stubbed fights: it must always reach a defined state."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

import altermmo.features
from altermmo.core import clock
from altermmo.core.state import BossPhase
from altermmo.features import boss_cycle, database
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

    def rarity_for_respawn_time(self, seconds, *, debug):
        return self.base_rarity

    async def send_boss_image(self, scene, rarity):
        if self.image_errors:
            self.image_errors -= 1
            raise ConnectionError("discord down")
        self.calls.append(("image", rarity))
        await scene.send(f"BOSS rarity {rarity}")
        return False, "boss"

    async def single_init(self, scene, rarity):
        self.calls.append(("single_init", rarity))
        if self.init_gate is not None:
            await self.init_gate.wait()
        return scene.author

    async def single_fight(self, scene, hunter, rarity, is_player_boss, player_boss):
        self.calls.append(("single_fight", hunter.name, rarity))
        if self.fight_gate is not None:
            await self.fight_gate.wait()
        if self.fight_error is not None:
            error, self.fight_error = self.fight_error, None
            raise error
        return self.result

    async def group_init(self, scene, rarity):
        self.calls.append(("group_init", rarity))
        return [scene.author]

    async def group_fight(self, scene, players, is_player_boss, player_boss):
        self.calls.append(("group_fight", len(players)))
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

    modifiers.load_modifiers = load_modifiers
    pets = SimpleNamespace(detect_boss=detect_boss)
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
    assert game_db.announce_channel.contains("pojawił się właśnie potwór")
    assert ("detect", 1) in stub.calls
    row = await database.read_boss_table(bot)
    assert row.respawn_started and row.rarity == 1
    assert not bot.state.busy


async def test_won_fight_resets_and_schedules_the_next_boss(game_db, cycle, stub):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    cycle.start()
    await wait_phase(bot, BossPhase.VISIBLE)
    await cycle.attack(game_db.scene(player, "$zaatakuj"))
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
