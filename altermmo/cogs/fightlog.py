"""The fight journal on Discord: the answer to a "lag" complaint and ``!walka``.

* every message goes to :func:`altermmo.features.fightreport.on_message`: a
  player who has just lost a typed fight on this channel and writes "lag"
  (in any of its forms) gets the fight's log and an honest explanation, once
  per fight.  The listener is separate from the commands and from the fights'
  own listeners, decides without waiting and never raises;
* ``!walka <gracz>`` (administrators; any channel): the last recorded fight of
  a member, in the same format - to check a fight without the log file.

The journal itself (:mod:`altermmo.core.fightlog`) is fed by the fights and
by ``AlterBot.on_message``; it works without this cog.
"""

from __future__ import annotations

from discord.ext import commands

from ..core import guards
from ..core.scene import Scene
from ..features import fightreport


class FightLogCog(commands.Cog, name="FightLog"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message) -> None:
        fightreport.on_message(self.bot, message)  # never raises; an answer is sent in the background

    @commands.command(name="walka", brief="The last recorded fight of a member: its log and why it was lost.")
    @guards.owner_or_admin()
    async def last_fight(self, ctx: commands.Context, *, who: str | None = None) -> None:
        """Usage: ``!walka gracz`` (a mention, an id or a name)."""
        await fightreport.show(Scene.from_ctx(ctx), who)


async def setup(bot) -> None:
    await bot.add_cog(FightLogCog(bot))
