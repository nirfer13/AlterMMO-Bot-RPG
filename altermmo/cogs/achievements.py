"""Achievements: ``$osiagniecia`` for supporters, and the roles the bot looks after itself.

The game lives in :mod:`altermmo.features.achievements`.  This cog decides *when*:

* once after every start the roles of the new achievements are made when they
  are missing and every supporter gets what their data stands for (veterans
  get the tiers they passed long ago) - quietly, with one note for the owner;
* ``$osiagniecia`` (supporters; any channel, it only shows something): what
  the player has and how many of how many - never which ones are missing.
  The panel's "Osiągnięcia" button runs the same thing;
* ``$roleosiagniec`` and ``$nadajosiagniecia`` (administrators): do the two
  start-up steps now.
"""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import achievements

log = logging.getLogger(__name__)

MSG_ROLES_MADE = "Utworzone role osiągnięć ({count}): {names}."
MSG_ROLES_PRESENT = "Wszystkie role osiągnięć już istnieją ({count})."
MSG_ROLES_MISSING = ("Nie udało się utworzyć ról osiągnięć ({missing_count}): {missing}. Sprawdź, czy bot ma "
                     "uprawnienie \"Zarządzanie rolami\" i czy serwer nie ma już 250 ról, a potem spróbuj "
                     "ponownie. Istniejące role: {count}.")
MSG_SYNC_STARTED = "Sprawdzam, komu należą się zaległe osiągnięcia..."
MSG_SYNC_DONE = "Zaległe osiągnięcia: nadano {roles} ról {players} wspierającym (lista jest na kanale logów)."
MSG_SYNC_NOTHING = "Zaległe osiągnięcia: nikomu nic nie brakowało."
MSG_SYNC_FAILED = "Nie udało się nadać zaległych osiągnięć (`{kind}`) - szczegóły są w logu bota."


class AchievementsCog(commands.Cog, name="Achievements"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._start: asyncio.Task | None = None

    async def cog_load(self) -> None:
        # A plain task, not a supervised one: this runs once per start and ends.
        self._start = asyncio.ensure_future(self._after_start())

    async def cog_unload(self) -> None:
        if self._start is not None and not self._start.done():
            self._start.cancel()

    async def _after_start(self) -> None:
        await self.bot.wait_until_ready()
        await achievements.start_up(self.bot)  # never raises

    # -- players ----------------------------------------------------------------------

    @commands.command(name="osiagniecia", aliases=["osiągnięcia", "osiagniecie", "osiągnięcie", "achievements"],
                      brief="Shows the supporter's achievements.")
    @guards.unless_busy(read_only=True)
    async def show(self, ctx: commands.Context) -> None:
        await achievements.show_achievements(Scene.from_ctx(ctx))

    # -- administrators ---------------------------------------------------------------

    @commands.command(name="roleosiagniec", brief="Create the missing achievement roles.")
    @guards.owner_or_admin()
    async def make_roles(self, ctx: commands.Context) -> None:
        made = await achievements.ensure_roles(self.bot)  # (everything, also what failed before)
        own = [item for item in achievements.TROPHIES if item.bot_made]
        missing = [item.name for item in own if not achievements.role_exists(self.bot, item)]
        lines = []
        if made:
            lines.append(MSG_ROLES_MADE.format(count=len(made), names=", ".join(made)))
        if missing:
            lines.append(MSG_ROLES_MISSING.format(missing_count=len(missing), missing=", ".join(missing),
                                                  count=len(own) - len(missing)))
        elif not made:
            lines.append(MSG_ROLES_PRESENT.format(count=len(own)))
        await io.send(ctx.channel, "\n".join(lines))

    @commands.command(name="nadajosiagniecia", brief="Give supporters the achievements they have earned.")
    @guards.owner_or_admin()
    async def give_out(self, ctx: commands.Context) -> None:
        await io.send(ctx.channel, MSG_SYNC_STARTED)
        try:
            await achievements.ensure_roles(self.bot)
            players, roles = await achievements.sync_veterans(self.bot)
        except Exception as exc:  # noqa: BLE001 - told to the administrator, the details are logged
            log.exception("$nadajosiagniecia failed.")
            await io.send(ctx.channel, MSG_SYNC_FAILED.format(kind=type(exc).__name__))
            return
        await io.send(ctx.channel, MSG_SYNC_DONE.format(roles=roles, players=players) if roles
                      else MSG_SYNC_NOTHING)


async def setup(bot) -> None:
    await bot.add_cog(AchievementsCog(bot))
