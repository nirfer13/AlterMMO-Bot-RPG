"""Core building blocks: state, storage, supervisor, clock, io."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
import pytest

from altermmo.core import clock, io
from altermmo.core.state import BossPhase, Busy, EventType, GameState
from altermmo.core.storage import IdListFile, JsonFile, migrate_legacy_file
from altermmo.core.supervisor import Supervisor


# -- state --------------------------------------------------------------------------

def test_busy_flag_is_released_when_the_activity_crashes():
    state = GameState()
    with pytest.raises(ValueError):
        with state.hold_busy("hunt"):
            assert state.busy
            raise ValueError("boom")
    assert not state.busy


def test_busy_flag_cannot_be_taken_twice():
    state = GameState()
    with state.hold_busy("hunt"):
        with pytest.raises(Busy):
            with state.hold_busy("tower"):
                pass
        assert state.busy_owner == "hunt"
    assert not state.busy


def test_running_event_always_clears_flags():
    state = GameState()
    with pytest.raises(RuntimeError):
        with state.running_event(EventType.INVASION):
            assert state.event_alive and state.busy
            assert state.event_type is EventType.INVASION
            raise RuntimeError("discord down")
    assert not state.event_alive
    assert not state.busy
    assert state.event_type is EventType.NONE


def test_passive_event_can_be_claimed_once():
    state = GameState()
    state.open_event(EventType.CHEST)
    assert not state.try_claim_event(EventType.SHRINE)
    assert state.try_claim_event(EventType.CHEST)
    assert not state.try_claim_event(EventType.CHEST)
    state.reopen_event(EventType.CHEST)
    assert state.try_claim_event(EventType.CHEST)


async def test_only_one_attacker_engages_the_boss():
    state = GameState()
    assert not state.try_engage_boss()
    await state.set_boss_phase(BossPhase.VISIBLE)
    assert state.try_engage_boss()
    assert not state.try_engage_boss()
    assert state.boss_phase is BossPhase.ENGAGED


async def test_wait_boss_phase_wakes_up_on_change():
    state = GameState()
    await state.set_boss_phase(BossPhase.FIGHTING)
    waiter = asyncio.ensure_future(state.wait_boss_phase(BossPhase.IDLE, timeout=1))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    await state.set_boss_phase(BossPhase.IDLE)
    assert await waiter is True
    assert await state.wait_boss_phase(BossPhase.VISIBLE, timeout=0.01) is False


def test_free_for_activity_matches_legacy_rule():
    state = GameState()
    assert state.free_for_activity
    state._boss_phase = BossPhase.WAITING
    assert state.free_for_activity
    state._boss_phase = BossPhase.PRE_SPAWN
    assert not state.free_for_activity
    state._boss_phase = BossPhase.IDLE
    with state.hold_busy("x"):
        assert not state.free_for_activity


# -- storage ------------------------------------------------------------------------

def test_id_list_file_roundtrip(tmp_path):
    cooldowns = IdListFile(tmp_path / "data" / "daily_player_cd.txt")
    assert not cooldowns.contains(1)
    cooldowns.add(1)
    cooldowns.add(2)
    cooldowns.add(1)
    assert cooldowns.contains("1") and cooldowns.count(1) == 2
    assert cooldowns.remove([1], all_occurrences=False) == 1
    assert cooldowns.count(1) == 1
    assert cooldowns.remove([1, 2]) == 2
    assert cooldowns.all() == []
    cooldowns.add(5)
    cooldowns.clear()
    assert cooldowns.all() == []


def test_id_list_file_reads_legacy_format(tmp_path):
    path = tmp_path / "daily_player_cd.txt"
    path.write_text("562743965598613515\n\n291836779495948288\n", encoding="utf-8")
    cooldowns = IdListFile(path)
    assert cooldowns.contains(562743965598613515)
    assert cooldowns.all() == ["562743965598613515", "291836779495948288"]


def test_json_file_survives_missing_empty_and_corrupt_files(tmp_path):
    store = JsonFile(tmp_path / "x.json", lambda: {"a": 1})
    assert store.load() == {"a": 1}
    store.path.write_text("", encoding="utf-8")
    assert store.load() == {"a": 1}
    store.path.write_text("{broken", encoding="utf-8")
    assert store.load() == {"a": 1}
    store.save({"zażółć": 2})
    assert store.load() == {"zażółć": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_migrate_legacy_file_never_overwrites(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "expSummary.json").write_text('{"1": 5}', encoding="utf-8")
    data = tmp_path / "data"
    migrate_legacy_file("expSummary.json", data, [legacy])
    assert (data / "expSummary.json").read_text(encoding="utf-8") == '{"1": 5}'
    (legacy / "expSummary.json").write_text('{"1": 99}', encoding="utf-8")
    migrate_legacy_file("expSummary.json", data, [legacy])
    assert (data / "expSummary.json").read_text(encoding="utf-8") == '{"1": 5}'


# -- supervisor ---------------------------------------------------------------------

async def test_supervisor_restarts_a_crashing_loop():
    supervisor = Supervisor(min_backoff=0.01, max_backoff=0.02)
    runs: list[int] = []

    async def flaky():
        runs.append(len(runs))
        if len(runs) < 3:
            raise RuntimeError("network down")
        await asyncio.sleep(3600)

    supervisor.start("loop", flaky)
    for _ in range(100):
        if len(runs) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(runs) == 3
    assert supervisor.is_running("loop")
    assert supervisor.status()["loop"]["restarts"] == 2
    await supervisor.stop_all()
    assert not supervisor.is_running("loop")


async def test_supervisor_start_replaces_a_running_task():
    supervisor = Supervisor(min_backoff=0.01)
    started: list[str] = []

    async def loop_a():
        started.append("a")
        await asyncio.sleep(3600)

    supervisor.start("loop", loop_a)
    await asyncio.sleep(0.01)
    supervisor.start("loop", loop_a)
    await asyncio.sleep(0.01)
    assert started == ["a", "a"]
    assert len(supervisor.names()) == 1
    await supervisor.stop_all()


# -- clock --------------------------------------------------------------------------

def test_legacy_timestamp_roundtrip():
    moment = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    legacy = clock.to_legacy(moment)
    assert legacy == datetime(2026, 11, 17, 12, 0)
    assert clock.from_legacy(legacy) == moment


def test_weekly_and_daily_schedule_use_warsaw_wall_clock():
    winter = datetime(2026, 11, 17, 11, 0, tzinfo=clock.WARSAW)  # Tuesday
    assert clock.next_weekly(0, 15, after=winter) == datetime(2026, 11, 23, 15, 0, tzinfo=clock.WARSAW)
    assert clock.previous_weekly(0, 15, before=winter) == datetime(2026, 11, 16, 15, 0, tzinfo=clock.WARSAW)
    assert clock.next_daily(2, 30, after=winter) == datetime(2026, 11, 18, 2, 30, tzinfo=clock.WARSAW)
    assert clock.previous_daily(2, 30, before=winter) == datetime(2026, 11, 17, 2, 30, tzinfo=clock.WARSAW)
    summer = datetime(2026, 7, 6, 15, 0, tzinfo=clock.WARSAW)  # Monday 15:00 sharp
    assert clock.next_weekly(0, 15, after=summer) == datetime(2026, 7, 13, 15, 0, tzinfo=clock.WARSAW)
    assert clock.previous_weekly(0, 15, before=summer) == summer
    # 15:00 in Warsaw is 14:00 UTC in winter and 13:00 UTC in summer.
    assert clock.next_weekly(0, 15, after=winter).astimezone(timezone.utc).hour == 14
    assert clock.next_weekly(0, 15, after=summer).astimezone(timezone.utc).hour == 13


# -- io -----------------------------------------------------------------------------

async def test_send_retries_transient_failures(game):
    game.channel.fail_with = aiohttp.ClientConnectionError("no internet")
    game.channel.fail_sends = 2
    message = await io.send(game.channel, "hej", attempts=3)
    assert message.content == "hej"


async def test_send_gives_up_after_all_attempts(game):
    game.channel.fail_with = aiohttp.ClientConnectionError("no internet")
    game.channel.fail_sends = 5
    with pytest.raises(aiohttp.ClientConnectionError):
        await io.send(game.channel, "hej", attempts=2)


async def test_send_patient_never_raises(game):
    game.channel.fail_with = aiohttp.ClientConnectionError("no internet")
    game.channel.fail_sends = 3
    assert (await io.send_patient(game.channel, "hej")).content == "hej"
    game.channel.fail_with = ValueError("permanent")
    game.channel.fail_sends = 1
    assert await io.send_patient(game.channel, "hej") is None


async def test_clear_channel_keeps_first_message_and_pins(game):
    player = game.add_member("Ala")
    kept = game.say(player, "przypięte")
    kept.pinned = True
    for index in range(5):
        game.say(player, f"spam {index}")
    deleted = await io.clear_channel(game.channel)
    assert deleted == 5
    assert [m.content for m in game.channel.messages] == ["Zasady gry", "przypięte"]


async def test_wait_for_message_is_scoped(game):
    player = game.add_member("Ala")
    other = game.add_member("Ola")
    game.say_later(other, "nie ja", 0.01)
    game.say_later(player, "zly kanal", 0.02, channel=game.log_channel)
    game.say_later(player, "ja", 0.03)
    message = await io.wait_for_message(game.bot, game.channel, authors=[player], timeout=1)
    assert message.content == "ja"
    with pytest.raises(asyncio.TimeoutError):
        await io.wait_for_message(game.bot, game.channel, authors=[player], timeout=0.05)


async def test_resolve_member_returns_none_for_people_who_left(game):
    player = game.add_member("Ala")
    assert await io.resolve_member(game.guild, player.id) is player
    assert await io.resolve_member(game.guild, 123) is None
    assert await io.resolve_member(game.guild, "abc") is None
