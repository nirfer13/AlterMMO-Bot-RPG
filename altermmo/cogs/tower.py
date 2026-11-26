"""The Death Tower: ``!wieża``, ``!klucz``, its rankings and the admin commands of its cooldown.

``!rankingwiezy`` and ``!rekordwiezy`` only display something and work on every
channel of the server; the run itself works on the boss channel only.  The game
lives in :mod:`altermmo.features.tower`; the body of ``!wieża`` is
:func:`altermmo.features.tower.start_tower`, so a button can run exactly the
same thing.  ``!klucz`` (skróty ``!uzyjklucza``, ``!klucze``) is the same run
opened with a Klucz do wieży (``use_key=True``), like the "Użyj klucza" button;
a new typed command, so it says nothing during a typed fight.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import tower

log = logging.getLogger(__name__)

# The texts live in features/tower.py now (kept here under their old names).
MSG_BOSS_COMING = tower.MSG_BOSS_COMING
MSG_BOSS_HERE = tower.MSG_BOSS_HERE
MSG_ON_COOLDOWN = tower.MSG_ON_COOLDOWN


class TowerCog(commands.Cog, name="Tower"):
    def __init__(self, bot) -> None:
        self.bot = bot

    # -- gameplay (boss channel only) ---------------------------------------------

    @commands.command(name="wieża", aliases=['wieza'], brief="Try to explor the death tower.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def explore_tower(self, ctx: commands.Context) -> None:
        """Climb the Death Tower; once a week."""
        await tower.start_tower(Scene.from_ctx(ctx))

    @commands.command(name="klucz", aliases=["uzyjklucza", "klucze"],
                      brief="Use a tower key: one more run this week.")
    @guards.boss_channel_only()
    @guards.not_during_fight()
    @guards.unless_busy()
    async def use_key(self, ctx: commands.Context) -> None:
        """A Klucz do wieży opens the Death Tower again (the "Użyj klucza" button does the same)."""
        await tower.start_tower(Scene.from_ctx(ctx), use_key=True)

    # -- display only (any channel; never blocked, exactly as in the old bot) ------

    @commands.command(name="rankingwiezy", aliases=['wieze', 'rankingwieza', 'rankingwieży',
                                                    'rankingwieża', 'rw'],
                      brief="Shows death tower ranking.")
    async def tower_ranking(self, ctx: commands.Context) -> None:
        await tower.tower_ranking(Scene.from_ctx(ctx))

    @commands.command(name="rekordwiezy", aliases=["rekordwieży", "rekordwieża", "rekordwieza"],
                      brief="Shows personal death tower record.")
    async def check_personal_tower(self, ctx: commands.Context) -> None:
        await tower.check_personal_tower(Scene.from_ctx(ctx))

    # -- admin --------------------------------------------------------------------

    @commands.command(name="ClearTowerCD")
    @guards.owner_or_admin()
    async def clear_cd(self, ctx: commands.Context) -> None:
        """Let everybody enter the tower again."""
        await tower.clear_weekly_tower_file(self.bot)
        await io.send(ctx.channel, "Zresetowano tower cd.")

    @commands.command(name="createTowerDatabase", brief="Create empty table TOWER.")
    @guards.owner_or_admin()
    async def create_tower_database(self, ctx: commands.Context) -> None:
        """Create the TOWER table when it is missing (existing records are kept)."""
        await tower.create_database(Scene.from_ctx(ctx))
        await io.send(ctx.channel, "Baza danych TOWER utworzona.")


async def setup(bot) -> None:
    await bot.add_cog(TowerCog(bot))
