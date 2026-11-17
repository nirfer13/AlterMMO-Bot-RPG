"""The Death Tower: ``$wieża``, its rankings and the admin commands of its cooldown.

``$rankingwiezy`` and ``$rekordwiezy`` only display something and work on every
channel of the server; the run itself works on the boss channel only.  The game
lives in :mod:`altermmo.features.tower`.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..core.state import BossPhase
from ..features import tower

log = logging.getLogger(__name__)

MSG_BOSS_COMING = ("Zaraz pojawi się prawidzwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, "
                   "to wpisz **$zaatakuj**, żeby stawić mu czoła.")
MSG_BOSS_HERE = ("Nie możesz ruszyć na eksplorację Wieży Śmierci, na Twojej drodze stoi boss "
                 "<:MonkaS:882181709100097587> Wpusz **$zaatakuj**, żeby stawić mu czoła.")
MSG_ON_COOLDOWN = "Drzwi Wieży Śmierci ani drgną. Sprawdź w następnym tygodniu <:Bedge:970576892874854400>"


class TowerCog(commands.Cog, name="Tower"):
    def __init__(self, bot) -> None:
        self.bot = bot

    # -- gameplay (boss channel only) ---------------------------------------------

    @commands.command(name="wieża", aliases=['wieza'], brief="Try to explor the death tower.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def explore_tower(self, ctx: commands.Context) -> None:
        """Climb the Death Tower; once a week."""
        bot = ctx.bot
        state = bot.state
        if state.busy:  # somebody took the channel between the guard and here
            await guards.notify_busy(ctx)
            return
        phase = state.boss_phase
        on_cd = tower.load_weekly_tower_from_file(bot, ctx.author.id)
        # No await between the checks above and hold_busy: two players cannot both start.
        if (not state.boss_blocks_activities or bot.debug) and not on_cd:
            with state.hold_busy("tower"):
                await tower.explore_tower(Scene.from_ctx(ctx))
        elif phase is BossPhase.PRE_SPAWN:
            await io.send(ctx.channel, MSG_BOSS_COMING)
        elif phase is BossPhase.VISIBLE:
            await io.send(ctx.channel, MSG_BOSS_HERE)
        elif phase > BossPhase.VISIBLE:
            return  # a boss fight is on: stay silent, as before
        elif on_cd:
            await io.send(ctx.channel, MSG_ON_COOLDOWN)

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
