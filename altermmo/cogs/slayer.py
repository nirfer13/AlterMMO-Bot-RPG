"""Perks of the boss slayer ("Rzeźnik Potworów"): ``!flex``, ``!kolor``, ``!ikona``.

Cosmetic commands of the weekly ranking winner; they work on every channel of
the server.  Each one has a 30 minute per-user cooldown (the "Poczekaj na
odnowienie komendy!" answer comes from the bot's central error handler).
"""

from __future__ import annotations

from discord.ext import commands

from ..core.scene import Scene
from ..features import boss

COOLDOWN_SECONDS = 1800


class SlayerCog(commands.Cog, name="Slayer"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="flex", brief="Boss slayer flex")
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def flex(self, ctx: commands.Context) -> None:
        """The boss slayer shows off with a random gif."""
        scene = Scene.from_ctx(ctx)
        if not boss.is_boss_slayer(ctx.bot, ctx.author):
            await scene.send(boss.MSG_FLEX_DENIED)
            return
        await boss.flex_gif(scene)
        await scene.send(boss.MSG_FLEX.format(user_id=ctx.author.id))

    @commands.command(name="kolor", brief="Boss slayer color change")
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def colour(self, ctx: commands.Context, hex_color: str | None = None) -> None:
        """``!kolor ff0000`` - the boss slayer changes the colour of the role."""
        scene = Scene.from_ctx(ctx)
        if not boss.is_boss_slayer(ctx.bot, ctx.author):
            await scene.send(boss.MSG_SLAYER_ONLY)
            return
        if not await boss.change_color(scene, hex_color):
            # A typo must not cost the slayer half an hour of waiting.
            ctx.command.reset_cooldown(ctx)

    @commands.command(name="ikona", brief="Boss slayer icon change")
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def icon(self, ctx: commands.Context) -> None:
        """``!ikona`` with a .png attached - the boss slayer changes the icon of the role."""
        scene = Scene.from_ctx(ctx)
        if not boss.is_boss_slayer(ctx.bot, ctx.author):
            await scene.send(boss.MSG_SLAYER_ONLY)
            return
        if not await boss.change_icon(scene):
            ctx.command.reset_cooldown(ctx)


async def setup(bot) -> None:
    await bot.add_cog(SlayerCog(bot))
