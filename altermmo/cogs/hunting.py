"""The daily hunt: ``!polowanie``, ``!przyneta`` and the admin commands of its cooldown.

The game itself lives in :mod:`altermmo.features.daily`; the body of
``!polowanie`` is :func:`altermmo.features.daily.start_hunt`, so a button can
run exactly the same thing.  Other players join a hunt by typing
``!polowanie`` in its first seconds or with the "Dołącz do polowania" button
under the monster.  ``!przyneta`` (owner, 2026-11-26) starts the same hunt
with a Przynęta: its monster comes one step rarer.
"""

from __future__ import annotations

import logging

import discord
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
    """Stay silent for a ``!polowanie`` typed while a hunt is gathering hunters.

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


def no_bait_while_gathering():
    """``!przyneta`` while a hunt gathers hunters: refused with :data:`daily.MSG_BAIT_GATHERING`, nothing used.

    Must run before ``guards.unless_busy()`` (the gathering hunt holds the
    busy flag: the player would read the busy notice instead of why) - so
    both are in one ``guards.in_order`` check.  The
    refusal shares the busy notice's limit per channel, so a burst of
    ``!przyneta`` cannot fill the channel while the hunters gather.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if not daily.is_gathering(ctx.channel):
            return True
        if guards.busy_notice_due(ctx.channel.id):
            try:
                await io.send(ctx.channel, daily.MSG_BAIT_GATHERING.format(user_id=ctx.author.id),
                              allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                       users=[ctx.author]), attempts=1)
            except Exception:  # noqa: BLE001 - a refusal is never worth an error
                log.info("The bait's refusal during a gathering hunt could not be posted.")
        raise guards.SilentCheckFailure("a hunt is gathering hunters")

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

    @commands.command(name="przyneta", aliases=["przynęta"], brief="Start the daily hunt with a bait.")
    @guards.in_order(guards.boss_channel_only(), guards.not_during_fight(), no_bait_while_gathering(),
                     guards.unless_busy())
    async def bait(self, ctx: commands.Context) -> None:
        """The daily hunt with a Przynęta: the monster comes one step rarer (``daily.start_hunt(bait=True)``)."""
        await daily.start_hunt(Scene.from_ctx(ctx), bait=True)

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
