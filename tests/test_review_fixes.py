"""Regression tests for the defects found by the v2 review (network drops, switch-over)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from altermmo.bot import LEGACY_STATE_FILES, Stores
from altermmo.core import io
from altermmo.core.supervisor import Supervisor
from altermmo.features import events

SWORDS = "⚔️"


def raw_click(game, message, emoji, member) -> None:
    """A click as the gateway reports it for a message that is NOT in the message cache."""
    payload = SimpleNamespace(message_id=message.id, user_id=member.id, emoji=emoji,
                              member=member, guild_id=game.guild.id)
    game.bot.dispatch("raw_reaction_add", payload)


# -- reactions after a re-login (message cache emptied) ------------------------------

async def test_reaction_listener_hears_raw_clicks_on_uncached_messages(game):
    ala = game.add_member("Ala")
    heard = []

    async def on_add(reaction, user) -> None:
        heard.append((reaction.message.id, str(reaction.emoji), user.id))

    message = SimpleNamespace(id=4242)
    with io.reaction_listener(game.bot, on_add):
        raw_click(game, message, SWORDS, ala)
        await asyncio.sleep(0.05)
    assert heard == [(4242, SWORDS, ala.id)]

    # Outside the block nothing listens any more.
    raw_click(game, message, SWORDS, ala)
    await asyncio.sleep(0.05)
    assert len(heard) == 1
    assert not game.bot.extra_events.get("on_raw_reaction_add")
    assert not game.bot.extra_events.get("on_reaction_add")


async def test_reaction_listener_ignores_raw_copy_of_a_cached_message(game, monkeypatch):
    """discord.py sends both events for a cached message: the click must count once."""
    ala = game.add_member("Ala")
    heard = []

    async def on_add(reaction, user) -> None:
        heard.append(user.id)

    message = SimpleNamespace(id=77)
    monkeypatch.setattr(type(game.bot), "cached_messages", property(lambda self: [message]),
                        raising=False)
    with io.reaction_listener(game.bot, on_add):
        raw_click(game, message, SWORDS, ala)
        game.bot.dispatch("reaction_add", SimpleNamespace(emoji=SWORDS, message=message), ala)
        await asyncio.sleep(0.05)
    assert heard == [ala.id]


async def test_duel_volunteers_are_heard_through_raw_events(game, monkeypatch):
    """After a longer outage the arena message is not cached; volunteers must still get in."""
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    fights = []

    async def duel_fight(scene, participants, ban=True):
        fights.append((list(participants), game.bot.state.busy_owner))
        return True, participants[0]

    monkeypatch.setattr(events.boss, "duel_fight", duel_fight)
    busy_during_sign_up = []

    def hook(message) -> None:
        if "Pojedynek!" in message.text:
            busy_during_sign_up.append(game.bot.state.busy)
            raw_click(game, message, SWORDS, ala)
            raw_click(game, message, SWORDS, ola)

    game.channel.send_hooks.append(hook)
    assert await events.spawn_duel(game.scene()) is True
    # The sign-up leaves the channel free (hunts and the tower work, as in the old
    # bot); only the fight itself holds the busy flag, and it is released afterwards.
    assert busy_during_sign_up == [False]
    assert fights == [([ala, ola], "event:DUEL")]
    assert not game.bot.state.busy


async def test_duel_waits_for_a_running_hunt_before_the_fight(game, monkeypatch):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    order = []

    async def duel_fight(scene, participants, ban=True):
        order.append("fight")
        return True, participants[0]

    monkeypatch.setattr(events.boss, "duel_fight", duel_fight)

    async def hunt() -> None:
        with game.bot.state.hold_busy("hunt"):
            await asyncio.sleep(0.3)
            order.append("hunt over")

    def hook(message) -> None:
        if "Pojedynek!" in message.text:
            asyncio.ensure_future(hunt())
            raw_click(game, message, SWORDS, ala)
            raw_click(game, message, SWORDS, ola)

    game.channel.send_hooks.append(hook)
    monkeypatch.setattr(events.asyncio, "sleep", _fast_sleep(asyncio.sleep))
    assert await events.spawn_duel(game.scene()) is True
    assert order == ["hunt over", "fight"]


def _fast_sleep(real_sleep):
    async def sleep(seconds, *args, **kwargs):
        await real_sleep(min(seconds, 0.05))
    return sleep


# -- Discord calls cannot hang for minutes -------------------------------------------

async def test_retry_limits_every_attempt():
    attempts = []

    async def never_answers():
        attempts.append(1)
        await asyncio.sleep(3600)

    with pytest.raises(asyncio.TimeoutError):
        await io.retry(never_answers, attempts=2, first_delay=0.01, attempt_timeout=0.05)
    assert len(attempts) == 2


# -- the owner hears about a loop that keeps crashing --------------------------------

async def test_supervisor_reports_the_first_crash():
    supervisor = Supervisor(min_backoff=0.01, max_backoff=0.01)
    reports = []
    reported = asyncio.Event()

    async def on_crash(name, error, crashes) -> None:
        reports.append((name, error, crashes))
        reported.set()

    async def broken() -> None:
        raise RuntimeError("boom")

    supervisor.on_crash = on_crash
    supervisor.start("boss-cycle", broken)
    try:
        await asyncio.wait_for(reported.wait(), 2)
        await asyncio.sleep(0.1)  # several more restarts: still one report (then every tenth)
    finally:
        await supervisor.stop_all()
    assert reports[0] == ("boss-cycle", "RuntimeError: boom", 1)
    assert all(crashes % 10 == 1 for _name, _error, crashes in reports)


# -- a claimed shrine / chest is not a free channel ----------------------------------

async def test_channel_is_not_free_while_a_chest_is_being_looted(game):
    from altermmo.core.state import EventType

    state = game.bot.state
    state.open_event(EventType.CHEST)
    assert state.try_claim_event(EventType.CHEST)      # $skrzynia is handing out the loot
    assert not state.free_for_activity                 # no new event may wipe the channel now
    state.close_event()
    assert state.free_for_activity


# -- switch-over: job markers and the data folder ------------------------------------

def test_job_markers_of_a_bot_that_was_not_running_are_dropped(game):
    import os
    import time

    bot = game.bot
    bot.store.runtime.save({"job_last_run:weekly-ranking": "2026-11-02T15:00:05+01:00",
                            "job_last_run:daily-reset": "2026-11-08T02:30:01+01:00",
                            "boss_spawning_enabled": True})

    bot._review_saved_state()                          # written a moment ago: trusted
    assert "job_last_run:weekly-ranking" in bot.store.runtime.load()
    assert bot._startup_notes == []

    three_days_ago = time.time() - 3 * 86400
    os.utime(bot.store.runtime.path, (three_days_ago, three_days_ago))
    bot._review_saved_state()
    # A season end "missed" while the old bot was in charge must not run days later.
    assert bot.store.runtime.load() == {"boss_spawning_enabled": True}
    assert len(bot._startup_notes) == 1 and "NIE zostaną nadrobione" in bot._startup_notes[0]


def test_owner_is_told_when_spawning_is_switched_off(game):
    bot = game.bot
    bot.store.runtime_set("boss_spawning_enabled", False)
    bot._review_saved_state()
    assert any("$startSpawnBoss" in note for note in bot._startup_notes)


def test_debug_runs_use_their_own_data_folder(monkeypatch, tmp_path):
    from altermmo import settings

    for name in ("ALTERMMO_DATA_DIR", "ALTERMMO_LEGACY_DIR"):
        monkeypatch.delenv(name, raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")

    monkeypatch.setenv("ALTERMMO_DEBUG", "1")
    debug = settings.load_settings(env_file=empty_env)
    assert debug.data_dir.name == "data-debug" and debug.legacy_dir is None

    monkeypatch.setenv("ALTERMMO_DEBUG", "0")
    assert settings.load_settings(env_file=empty_env).data_dir.name == "data"


# -- switch-over: state files of the old bot -----------------------------------------

def test_state_files_are_taken_over_from_the_old_folder(tmp_path):
    legacy, own = tmp_path / "old-bot", tmp_path / "v2"
    legacy.mkdir()
    own.mkdir()
    (legacy / "daily_player_cd.txt").write_text("111\n222\n", encoding="utf-8")
    (legacy / "expSummary.json").write_text('{"111": 2500}', encoding="utf-8")
    store = Stores(tmp_path / "v2" / "data")

    assert sorted(store.migrate_legacy([own, legacy])) == ["daily_player_cd.txt", "expSummary.json"]
    assert store.daily_cd.all() == ["111", "222"]
    assert store.exp_summary.load() == {"111": 2500}
    # The old bot's files are only read.
    assert (legacy / "daily_player_cd.txt").read_text(encoding="utf-8") == "111\n222\n"

    # A second start never overwrites what v2 has written meanwhile.
    store.daily_cd.add(333)
    (legacy / "daily_player_cd.txt").write_text("999\n", encoding="utf-8")
    assert store.migrate_legacy([own, legacy]) == []
    assert store.daily_cd.all() == ["111", "222", "333"]
    assert set(LEGACY_STATE_FILES) >= {"bossModifier.json", "weekly_tower_cd.txt",
                                       "skill_daily_player_cd.txt", "skill_weekly_player_cd.txt"}
