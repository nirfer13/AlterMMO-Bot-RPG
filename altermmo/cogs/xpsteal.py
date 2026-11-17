"""``$kradnijxp`` - spend a "steal experience" boss drop on a chosen member."""

from __future__ import annotations

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import xpsteal


class XpStealCog(commands.Cog, name="XpSteal"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="kradnijxp", aliases=["kradnij", "ukradnijxp", "kradniexp"],
                      brief="Steal Discord experience from the chosen member (boss drop).")
    @guards.boss_channel_only()
    async def steal_xp(self, ctx: commands.Context, *, target: str | None = None) -> None:
        """Usage: ``$kradnijxp nick`` (or a mention).  The message disappears at once."""
        await xpsteal.steal(Scene.from_ctx(ctx), target)

    @steal_xp.error
    async def steal_xp_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # Whatever went wrong, never leave the victim's name on the channel.
        await io.safe_delete(ctx.message)
        await ctx.bot.on_command_error(ctx, error, force=True)

    @commands.command(name="DajKradziez", brief="Give a member an XP theft right (admin).")
    @guards.owner_or_admin()
    async def grant(self, ctx: commands.Context, player_id: int, amount: int = xpsteal.DEFAULT_AMOUNT) -> None:
        xpsteal.grant(self.bot, player_id, amount)
        await io.send(ctx.channel,
                      f"<@{player_id}> może teraz ukraść {amount} doświadczenia (**$kradnijxp nick**).")


async def setup(bot) -> None:
    await bot.add_cog(XpStealCog(bot))
