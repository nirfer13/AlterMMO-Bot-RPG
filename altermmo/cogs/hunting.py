"""The daily hunt: ``$polowanie`` and the admin commands of its cooldown.

The game itself lives in :mod:`altermmo.features.daily`; the body of
``$polowanie`` is :func:`altermmo.features.daily.start_hunt`, so a button can
run exactly the same thing.  Other players join a hunt by typing
``$polowanie`` in its first seconds or with the "Dołącz do polowania" button
under the monster.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import daily

log = logging.getLogger(__name__)

# The texts live in features/daily.py now (kept here under their old names).
MSG_BOSS_COMING = daily.MSG_BOSS_COMING
MSG_BOSS_HERE = daily.MSG_BOSS_HERE
MSG_ON_COOLDOWN = daily.MSG_ON_COOLDOWN


def not_joining_a_hunt():
    """Stay silent for a ``$polowanie`` typed while a hunt is gathering hunters.

    In that moment the word joins the running hunt (``daily.hunt_mobs`` listens
    for it).  The same message also reaches this command; without the check
    the player who just joined would be told that the bot is busy.  Must stand
    above ``guards.unless_busy()``.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if daily.is_gathering(ctx.channel):
            raise guards.SilentCheckFailure("a hunt is gathering hunters")
        return True

    return commands.check(predicate)


class HuntingCog(commands.Cog, name="Hunting"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="polowanie", aliases=['pol', 'p'], brief="Try to hunt on a mobs.")
    @guards.boss_channel_only()
    @not_joining_a_hunt()
    @guards.unless_busy()
    async def hunting(self, ctx: commands.Context) -> None:
        """Hunt a monster; once a day (a prayer at the shrine may reset the cooldown)."""
        await daily.start_hunt(Scene.from_ctx(ctx))

    # -- admin --------------------------------------------------------------------

    @commands.command(name="ClearDaily")
    @guards.owner_or_admin()
    async def clear_cd_daily(self, ctx: commands.Context) -> None:
        """Let everybody hunt again."""
        await daily.clear_daily_file(self.bot)
        await io.send(ctx.channel, "Zresetowano daily cd.")

    @commands.command(name="ClearDailyAlter")
    @guards.owner_or_admin()
    async def clear_cd_daily_alter(self, ctx: commands.Context) -> None:
        """Let the administrator who typed it hunt again."""
        await daily.remove_player_daily_file(self.bot, [str(ctx.author.id)])
        await io.send(ctx.channel, "Zresetowano daily cd Altera.")


async def setup(bot) -> None:
    await bot.add_cog(HuntingCog(bot))
