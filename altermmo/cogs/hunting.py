"""The daily hunt: ``$polowanie`` and the admin commands of its cooldown.

The game itself lives in :mod:`altermmo.features.daily`.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..core.state import BossPhase
from ..features import daily

log = logging.getLogger(__name__)

MSG_BOSS_COMING = ("Zaraz pojawi się prawidzwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, "
                   "to wpisz **$zaatakuj**, żeby stawić mu czoła.")
MSG_BOSS_HERE = ("Teraz pora na walkę z prawdziwym bossem, nie mieszaj się leszczyku "
                 "<:madge:882184635474386974>")
MSG_ON_COOLDOWN = ("Zregeneruj się i spróbuj zapolować jutro <:Bedge:970576892874854400> Niektóre modlitwy "
                   "przy kapliczkach również są w stanie zregenerować Twoje siły, więc bądź czujny!")


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
        bot = ctx.bot
        state = bot.state
        if state.busy:  # somebody took the channel between the guard and here
            await guards.notify_busy(ctx)
            return
        phase = state.boss_phase
        on_cd = daily.load_daily_from_file(bot, ctx.author.id)
        # No await between the checks above and hold_busy: two players cannot both start.
        if (not state.boss_blocks_activities or bot.debug) and not on_cd:
            with state.hold_busy("hunt"):
                await daily.daily_hunt(Scene.from_ctx(ctx))
        elif phase is BossPhase.PRE_SPAWN:
            await io.send(ctx.channel, MSG_BOSS_COMING)
        elif phase is BossPhase.VISIBLE:
            await io.send(ctx.channel, MSG_BOSS_HERE)
        elif phase > BossPhase.VISIBLE:
            return  # a boss fight is on: stay silent, as before
        elif on_cd:
            await io.send(ctx.channel, MSG_ON_COOLDOWN)

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
