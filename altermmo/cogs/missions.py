"""The companion missions ("Zlecenia"): ``!zlecenia``, ``!przywolaj`` and two commands for the owner.

The game lives in :mod:`altermmo.features.missions`.  ``!zlecenia`` shows the
player's missions as one panel (the board, the progress of the pet that is
away, its result) - like the hub's "Zlecenia" button: on the boss channel
only, following the busy policy of the display commands, also during the
sign-up of the "Trening towarzyszy" event (``stable=True``).  ``!przywolaj``
reads a Zwój przywołania (from a mission, a drop or the merchant - a mythic
boss comes): a gameplay command, silent during a typed fight.  The buttons under the panel are answered by its own view (they die
with it; a click after a restart is answered as stale).

The cog also runs the notifier "missions-watch" (pays the missions that have
ended and tells the channel, only while it is quiet) and, when it is loaded,
looks for a summon interrupted by a crash (the owner is told; nothing is given
back by itself).  For the owner's support (not in the help):
``!ZakonczZlecenie @gracz`` brings the player's pet back now (its mission is
paid at the next look), ``!DajZwojPrzywolania @gracz [n]`` gives scrolls back
by hand (no cap since 2026-11-26; a negative ``n`` takes them, never below 0 -
the same as ``!DajPrzedmiot @gracz zwój przywołania n``).
"""

from __future__ import annotations

import discord
from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import missions, pets

MSG_ENDED = "Zlecenie gracza <@{player_id}> zakończone - towarzysz już wrócił (rozliczy się przy otwarciu tablicy)."
MSG_NOTHING_AWAY = "Gracz <@{player_id}> nie ma towarzysza na zleceniu."
MSG_GIVEN = "Gracz <@{player_id}> ma teraz Zwoje przywołania: {count}."
MSG_NO_COLUMNS = "Zwoje przywołania są wyłączone: baza danych nie ma ich kolumny (SUMMON_SCROLLS)."


class MissionsCog(commands.Cog, name="Zlecenia"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        missions.resolve_summon_marker(self.bot)  # never raises
        self.bot.supervisor.start(missions.WATCH_NAME, lambda: missions.watch(self.bot))

    async def cog_unload(self) -> None:
        self.bot.supervisor.stop(missions.WATCH_NAME)

    @commands.command(name="zlecenia", aliases=["zlecenie", "zl"], brief="Pet missions: board, progress, result.")
    @guards.boss_channel_only()
    @guards.unless_busy(read_only=True, stable=True)
    async def zlecenia(self, ctx: commands.Context) -> None:
        await missions.show_board(Scene.from_ctx(ctx))

    @commands.command(name="przywolaj", aliases=["przywołaj", "przywolanie", "przywołanie"],
                      brief="Use a summoning scroll (mythic boss).")
    @guards.boss_channel_only()
    @guards.not_during_fight()
    @guards.unless_busy()
    async def przywolaj(self, ctx: commands.Context) -> None:
        await missions.use_summon_scroll(Scene.from_ctx(ctx))

    @commands.command(name="ZakonczZlecenie", brief="End the player's companion mission now (support).")
    @guards.owner_or_admin()
    async def end_mission(self, ctx: commands.Context, member: discord.Member) -> None:
        ended = await missions.end_now(self.bot, member)
        text = MSG_ENDED if ended is not None else MSG_NOTHING_AWAY
        await io.send(ctx.channel, text.format(player_id=member.id), allowed_mentions=discord.AllowedMentions.none())
        await io.safe_react(ctx.message, "✅" if ended is not None else "❌")

    @commands.command(name="DajZwojPrzywolania", brief="Give summoning scrolls back to the player (support).")
    @guards.owner_or_admin()
    async def give_summon(self, ctx: commands.Context, member: discord.Member, number: int = 1) -> None:
        if not await pets.mission_columns_available(self.bot, refresh=True):
            await io.send(ctx.channel, MSG_NO_COLUMNS)
            return
        count = await pets.give_summon_scrolls(self.bot, member, number)
        await io.send(ctx.channel, MSG_GIVEN.format(player_id=member.id, count=count),
                      allowed_mentions=discord.AllowedMentions.none())


async def setup(bot) -> None:
    await bot.add_cog(MissionsCog(bot))
