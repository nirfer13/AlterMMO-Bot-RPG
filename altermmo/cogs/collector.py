"""The rank "Kolekcjoner Pupili" of place 1 of the collection ranking: when it is looked after.

The game lives in :mod:`altermmo.features.collector`.  This cog decides *when*:

* the supervised loop (``collector.run``; started once from ``cog_load`` like the bot's other
  loops, cancelled at the unload / shutdown) synchronises after the start, on request and every
  half an hour as a safety net - one run at a time, waiting while something plays on the boss
  channel or the boss is about to arrive;
* the member events ask for a synchronisation (they only raise a flag): a member whose support
  began or ended (the holder whose support ended gets a minute's grace first: a supporter role
  can blink at a renewal), the rank given or taken by hand, a supporter or the holder who left
  the server.  (``intents.members`` is on: the bot gets these events.)  The album asks by itself
  after an entry was kept or removed (``features/album.py``);
* ``$kolekcjoner`` (administrators): synchronise now and say what happened.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from ..core import guards, io
from ..features import collector

#: The answer names players (who holds the rank): nobody is pinged by it.
QUIET = discord.AllowedMentions.none()


class CollectorCog(commands.Cog, name="Collector"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.supervisor.start(collector.LOOP_NAME, self._loop)

    async def cog_unload(self) -> None:
        self.bot.supervisor.stop(collector.LOOP_NAME)

    async def _loop(self) -> None:
        await collector.run(self.bot)

    # -- what may change the holder ---------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before, after) -> None:
        collector.member_changed(self.bot, before, after)  # never raises

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        collector.member_left(self.bot, member)  # never raises

    # -- administrators ---------------------------------------------------------------

    @commands.command(name="kolekcjoner", brief="Synchronise the rank of the collection ranking's place 1.")
    @guards.owner_or_admin()
    async def sync_now(self, ctx: commands.Context) -> None:
        outcome = await collector.sync_now(self.bot)  # never raises
        await io.send(ctx.channel, collector.describe(outcome)[:1990], allowed_mentions=QUIET)


async def setup(bot) -> None:
    await bot.add_cog(CollectorCog(bot))
