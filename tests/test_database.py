"""features/database.py against a real PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

from altermmo.core import clock
from altermmo.features import daily, database


async def test_boss_schedule_roundtrip_and_legacy_format(game_db):
    bot = game_db.bot
    assert await database.read_boss_table(bot) is None
    await database.update_boss_table(bot, 2, 3600, True)
    row = await database.read_boss_table(bot)
    assert row.rarity == 2 and row.respawn_started is True
    assert abs((row.respawn_at - clock.utcnow()).total_seconds() - 3600) < 5
    # Stored exactly like the legacy bot did: naive "UTC+2" timestamp.
    raw = await bot.db.fetchval("SELECT RESPAWN_TIME FROM BOSS WHERE ID = 0")
    assert abs((raw - clock.legacy_now()).total_seconds() - 3600) < 5
    await database.update_boss_table(bot, 0, 0, False)
    row = await database.read_boss_table(bot)
    assert row.rarity == 0 and row.respawn_started is False
    assert await bot.db.fetchval("SELECT count(*) FROM BOSS") == 1


async def test_record_and_history_survive_apostrophes(game_db):
    bot = game_db.bot
    assert await database.read_record_table(bot) == (database.DEFAULT_RECORD, database.DEFAULT_NICK)
    await database.update_record_table(bot, "O'Neil", timedelta(seconds=1.5))
    record, nick = await database.read_record_table(bot)
    assert nick == "O'Neil" and record == "0:00:01.500000"
    await database.update_history_table(bot, "Party d'Artagnan")
    fight_time, nick = await database.read_history_table(bot)
    assert nick == "Party d'Artagnan"
    assert fight_time[:10] == clock.now().strftime("%Y-%m-%d")


def test_record_comparison_handles_every_stored_format():
    assert database.is_new_record("00:00:20.000", timedelta(seconds=3))
    assert not database.is_new_record("0:00:01.500000", timedelta(seconds=3))
    # str(timedelta) drops the fraction for whole seconds - used to crash strptime.
    assert database.is_new_record("0:00:02", timedelta(seconds=1.9))
    assert database.is_new_record("garbage", timedelta(seconds=99))
    assert database.format_turn_time(timedelta(seconds=2)) == "0:00:02.000000"
    assert database.display_record("0:00:15.121325") == "15.121325"
    assert database.display_record("00:00:20.000") == "20.000"
    assert database.display_record("0:00:00.900000") == "0.900000"


async def test_ranking_points_upsert_and_listing(game_db):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    ola = game_db.add_member("O'la")
    assert await database.update_ranking_table(bot, ala.id, 0, 0) == 1
    assert await database.update_ranking_table(bot, ala.id, 4, 2) == 15
    assert await database.update_ranking_table(bot, ola.id, 2, -12) == -6
    assert await database.update_ranking_table(bot, 123456789, 1, 0) == 3   # left the server
    await database.read_ranking_table(game_db.scene())
    embed = game_db.channel.messages[-1].embeds[0]
    assert embed.title == "Ranking łowców potworów!"
    season_end = f"<t:{int(daily.next_season_end().timestamp())}:R>"     # Discord's live countdown
    assert embed.description == (f"1. **Ala** - 15 pkt.\n2. **O'la** - -6 pkt.\n"
                                 f"\n⏳ Sezon kończy się {season_end}.")


async def test_season_summary_returns_winner_and_reset_keeps_table(game_db):
    bot = game_db.bot
    assert await database.read_summary_ranking_table(game_db.scene()) == bot.ids.owner
    ala = game_db.add_member("Ala")
    await database.update_ranking_table(bot, ala.id, 4, 0)
    assert await database.read_summary_ranking_table(game_db.scene()) == ala.id
    await database.reset_ranking_table(bot)
    assert await bot.db.fetchval("SELECT count(*) FROM RANKING") == 0
    assert await database.update_ranking_table(bot, ala.id, 0, 0) == 1


async def test_create_helpers_never_destroy_data(game_db):
    bot = game_db.bot
    await database.update_record_table(bot, "Ala", timedelta(seconds=1))
    await database.create_record_table(bot)
    await database.create_boss_table(bot)
    await database.create_history_table(bot)
    await database.create_ranking_table(bot)
    assert (await database.read_record_table(bot))[1] == "Ala"
    assert await bot.db.fetchval("SELECT count(*) FROM BOSS") == 1
