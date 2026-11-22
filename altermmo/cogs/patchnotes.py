"""Patch notes: posted once after every start that brings an update, and ``$patchnotes``.

The rules live in :mod:`altermmo.features.patchnotes`.  This cog decides *when*:

* once per process, after the bot is ready (a plain task, not ``on_ready``: that fires again
  after every reconnect), the entries of ``config/patchNotes.json`` that were not announced
  yet are posted to the patch-notes channel; the owner gets one note;
* ``$patchnotes`` (administrators; any channel): the status - the highest announced entry and
  where that number comes from, how many entries wait - and it posts what waits, exactly as the
  start-up does;
* ``$patchnotes od <nr>`` (administrators): posts again from that entry on (the owner removed a
  message and wants it back); the remembered number is then the highest one sent.  It never
  jumps over entries that were not announced yet (it refuses and says which ones), and a run
  that stops in the middle never lowers the remembered number.
"""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from ..core import guards, io
from ..features import patchnotes

log = logging.getLogger(__name__)

MSG_USAGE = ("Użycie: **$patchnotes** (stan i wysłanie tego, co czeka) albo **$patchnotes od numer** "
             "(wyślij ponownie od podanego wpisu).")
MSG_WAIT = "Trwa właśnie wysyłanie patch notesów - poczekam, aż się skończy."


def parse_arguments(words: tuple[str, ...]) -> tuple[bool, int | None]:
    """``()`` -> (True, None); ``("od", "3")`` -> (True, 3); anything else -> (False, None)."""
    if not words:
        return True, None
    if len(words) == 2 and words[0].lower() == "od" and words[1].isascii() and words[1].isdigit():
        return True, int(words[1])
    return False, None


class PatchNotesCog(commands.Cog, name="PatchNotes"):
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
        await patchnotes.start_up(self.bot)  # never raises; once per process

    @commands.command(name="patchnotes", brief="Patch notes: status and post what waits; 'od <nr>' posts again.")
    @guards.owner_or_admin()
    async def patch_notes(self, ctx: commands.Context, *words: str) -> None:
        valid, from_id = parse_arguments(words)
        if not valid:
            await io.send(ctx.channel, MSG_USAGE, allowed_mentions=patchnotes.NO_MENTIONS)
            return
        if patchnotes.busy(self.bot):
            await io.send(ctx.channel, MSG_WAIT, allowed_mentions=patchnotes.NO_MENTIONS)
        outcome = await patchnotes.announce(self.bot, from_id=from_id)  # never raises
        await io.send(ctx.channel, patchnotes.command_answer(outcome)[:1990],
                      allowed_mentions=patchnotes.NO_MENTIONS)


async def setup(bot) -> None:
    await bot.add_cog(PatchNotesCog(bot))
