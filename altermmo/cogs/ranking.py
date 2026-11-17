"""Boss record, last fight and the weekly ranking (``$rekord``, ``$kiedy``, ``$ranking``)."""

from __future__ import annotations

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import database


class RankingCog(commands.Cog, name="Ranking"):
    def __init__(self, bot) -> None:
        self.bot = bot

    # Display-only commands: any channel of the server (owner's decision; the old
    # bot answered them on the boss channel only).

    @commands.command(name="rekord")
    @guards.unless_busy(read_only=True)
    async def record(self, ctx: commands.Context) -> None:
        """Who killed a boss fastest (average time per fight turn)."""
        record_time, nick = await database.read_record_table(self.bot)
        await io.send(ctx.channel, f"Poprzedni rekord należy do **{nick}** i wynosi średnio "
                                   f"**{database.display_record(record_time)} sekundy na turę walki**.")

    @commands.command(name="kiedy", brief="Check previous boss kill time")
    @guards.unless_busy(read_only=True)
    async def last_fight(self, ctx: commands.Context) -> None:
        """Who fought the boss last and when."""
        fight_time, nick = await database.read_history_table(self.bot)
        await io.send(ctx.channel, f"Poprzednio boss walczył z **{nick}** i było to "
                                   f"**{fight_time[:16]}** czasu polskiego.")

    @commands.command(name="ranking", aliases=["r"])
    @guards.unless_busy(read_only=True)
    async def ranking(self, ctx: commands.Context) -> None:
        """This season's top 10 monster hunters."""
        await database.read_ranking_table(Scene.from_ctx(ctx))


async def setup(bot) -> None:
    await bot.add_cog(RankingCog(bot))
