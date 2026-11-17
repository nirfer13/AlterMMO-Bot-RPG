"""Administrator-only maintenance and debug commands.

The legacy "create database" commands DROPPED the live table before creating
it again.  Here they only create what is missing, so running one by accident
cannot destroy player data.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from .. import schema
from ..core import clock, guards, io
from ..core.scene import Scene
from ..features import database, expsum

log = logging.getLogger(__name__)


def _as_bool(text: str) -> bool:
    return str(text).strip().lower() in {"1", "true", "t", "yes", "tak"}


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        permissions = getattr(ctx.author, "guild_permissions", None)
        if permissions is None or not permissions.administrator:
            raise guards.SilentCheckFailure("administrators only")
        return True

    # -- experience summary -------------------------------------------------------

    @commands.command(name="Exp")
    async def exp_summary(self, ctx: commands.Context) -> None:
        """Show the experience to hand out, then reset the list."""
        await expsum.show_file_exp(Scene.from_ctx(ctx))
        expsum.init_file_exp(self.bot)

    # -- small debug helpers ------------------------------------------------------

    @commands.command(name="time")
    async def check_time(self, ctx: commands.Context) -> None:
        await io.send(ctx.channel, str(clock.now()))

    @commands.command(name="getlastmsg")
    async def last_message(self, ctx: commands.Context) -> None:
        message = await io.last_message(ctx.channel)
        if message is not None:
            await io.send(ctx.channel, str(message.content) or "(pusta wiadomość)")
            await io.send(ctx.channel, str(message.author))

    @commands.command(name="SendPM", brief="Sends private message to the owner.")
    async def send_pm(self, ctx: commands.Context) -> None:
        user = await self.bot.fetch_user(self.bot.ids.owner)
        await user.send("Hello there!")

    # -- database -----------------------------------------------------------------

    @commands.command(name="createAllDatabases")
    async def create_all(self, ctx: commands.Context) -> None:
        """Create every missing table (existing tables and data are left alone)."""
        count = await schema.create_all(self.bot.db)
        await database.create_boss_table(self.bot)
        await database.create_record_table(self.bot)
        await database.create_history_table(self.bot)
        await io.send(ctx.channel, f"Wszystkie bazy danych utworzone! ({count} tabel sprawdzonych)")

    @commands.command(name="updateDatabase")
    async def update_boss(self, ctx: commands.Context, rarity: int, resp_time: int, resp_bool: str) -> None:
        await database.update_boss_table(self.bot, rarity, resp_time, _as_bool(resp_bool))
        await io.send(ctx.channel, "Database updated.")

    @commands.command(name="readDatabase")
    async def read_boss(self, ctx: commands.Context) -> None:
        row = await database.read_boss_table(self.bot)
        if row is None:
            await io.send(ctx.channel, "Tabela BOSS jest pusta.")
            return
        respawn = row.respawn_at.astimezone(clock.WARSAW).strftime("%Y-%m-%d %H:%M:%S") \
            if row.respawn_at else "-"
        await io.send(ctx.channel, f"Czy boss będzie wskrzeszony?: {row.respawn_started}")
        await io.send(ctx.channel, f"Boss rarity: {row.rarity}")
        await io.send(ctx.channel, f"Czas wskrzeszenia: {respawn}")

    @commands.command(name="createBossDatabase")
    async def create_boss(self, ctx: commands.Context) -> None:
        await database.create_boss_table(self.bot)
        await io.send(ctx.channel, "Baza danych utworzona.")

    @commands.command(name="createRecordDatabase")
    async def create_record(self, ctx: commands.Context) -> None:
        await database.create_record_table(self.bot)
        await io.send(ctx.channel, "Baza danych z rekordem utworzona.")

    @commands.command(name="updateRecordDatabase")
    async def update_record(self, ctx: commands.Context, nick: str, record_time: str) -> None:
        if database.parse_turn_time(record_time) is None:
            await io.send(ctx.channel, "Podaj czas w formacie G:MM:SS.ssssss, np. 0:00:02.500000")
            return
        await database.update_record_table(self.bot, nick, record_time)
        await io.send(ctx.channel, "Baza danych z rekordem zaktualizowana.")

    @commands.command(name="createHistoryDatabase")
    async def create_history(self, ctx: commands.Context) -> None:
        await database.create_history_table(self.bot)
        await io.send(ctx.channel, "Baza danych z historią utworzona.")

    @commands.command(name="updateHistoryDatabase")
    async def update_history(self, ctx: commands.Context, nick: str, *, fight_time: str) -> None:
        await database.update_history_table(self.bot, nick, fight_time)
        await io.send(ctx.channel, "Baza danych z historią zaktualizowana.")

    @commands.command(name="createRankingDatabase")
    async def create_ranking(self, ctx: commands.Context) -> None:
        await database.create_ranking_table(self.bot)
        await io.send(ctx.channel, "Baza danych z rankingiem utworzona.")

    @commands.command(name="resetRankingDatabase")
    async def reset_ranking(self, ctx: commands.Context) -> None:
        await database.reset_ranking_table(self.bot)
        await io.send(ctx.channel, "Baza danych z rankingiem zresetowana.")

    @commands.command(name="updateRankingDatabase")
    async def update_ranking(self, ctx: commands.Context, user_id: int, rarity: int, modifier: int = 0) -> None:
        """Add ranking points as if ``user_id`` killed a boss of ``rarity`` (+ ``modifier``)."""
        total = await database.update_ranking_table(self.bot, user_id, rarity, modifier)
        await io.send(ctx.channel, f"Ranking zaktualizowany: <@{user_id}> ma teraz {total} pkt.")


async def setup(bot) -> None:
    await bot.add_cog(AdminCog(bot))
