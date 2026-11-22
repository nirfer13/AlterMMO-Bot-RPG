"""``$kradnijxp`` - spend a "steal experience" boss drop on a chosen member.

The theft itself is settled by Horus (see :mod:`altermmo.features.xpsteal`); this
cog also runs the loop that announces what each theft brought
(:class:`~altermmo.features.xpsteal.TransferWatch`), supervised like the bot's
other loops: started once from ``cog_load``, cancelled at the unload / shutdown.
"""

from __future__ import annotations

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import xpsteal


class XpStealCog(commands.Cog, name="XpSteal"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.watch = xpsteal.TransferWatch(bot)

    async def cog_load(self) -> None:
        self.bot.supervisor.start(xpsteal.TRANSFERS_LOOP, self.watch.run)

    async def cog_unload(self) -> None:
        self.bot.supervisor.stop(xpsteal.TRANSFERS_LOOP)

    @commands.command(name="kradnijxp", aliases=["kradnij", "ukradnijxp", "kradniexp"],
                      brief="Steal Discord experience from the chosen member (boss drop).")
    @guards.boss_channel_only()
    async def steal_xp(self, ctx: commands.Context, *, target: str | None = None) -> None:
        """Usage: ``$kradnijxp nick`` (or the member's id).  The message disappears at once.

        A mention is understood too, but the texts never suggest it: it notifies the victim."""
        await xpsteal.steal(Scene.from_ctx(ctx), target)

    @steal_xp.error
    async def steal_xp_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # Whatever went wrong, never leave the victim's name on the channel.
        await io.safe_delete(ctx.message)
        await ctx.bot.on_command_error(ctx, error, force=True)

    @commands.command(name="DajKradziez", brief="Give a member an XP theft right (admin).")
    @guards.owner_or_admin()
    async def grant(self, ctx: commands.Context, player_id: int, amount: int = xpsteal.DEFAULT_AMOUNT) -> None:
        if amount <= 0:
            text = xpsteal.MSG_GRANT_BAD_AMOUNT
        elif xpsteal.grant(self.bot, player_id, amount):
            text = xpsteal.MSG_GRANTED.format(player=player_id, amount=xpsteal.number(amount))
        else:
            text = xpsteal.MSG_GRANT_NOT_SAVED.format(player=player_id)
        await io.send(ctx.channel, text)


async def setup(bot) -> None:
    await bot.add_cog(XpStealCog(bot))
