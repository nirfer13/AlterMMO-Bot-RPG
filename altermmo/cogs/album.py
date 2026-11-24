"""The pet album: ``!album`` (alias ``!kolekcja``) and ``!rankingalbumow`` for supporters.

The game lives in :mod:`altermmo.features.album` (the ranking of the collections:
:mod:`altermmo.features.albumrank`).  Both commands only show something, so they
work on every channel of the server and follow the busy policy of the display
commands (``guards.unless_busy(read_only=True)``), like ``!osiagniecia`` and the
other rankings.  The hub panel's "Album" button runs ``!album``, its "Rankingi"
button shows the ranking of the collections too.  The buttons under the album are
answered by its own view; nothing is registered here (they die with the panel, a
click after a restart is answered as stale).
"""

from __future__ import annotations

from discord.ext import commands

from ..core import guards
from ..core.scene import Scene
from ..features import album, albumrank


class AlbumCog(commands.Cog, name="Album"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="album", aliases=["kolekcja"], brief="Shows the supporter's pet album.")
    @guards.unless_busy(read_only=True)
    async def show(self, ctx: commands.Context) -> None:
        await album.show_album(Scene.from_ctx(ctx))

    @commands.command(name="rankingalbumow", aliases=["rankingalbumów", "rankingkolekcji", "albumy", "ra"],
                      brief="The supporters' most valuable album collections.")
    @guards.unless_busy(read_only=True)
    async def ranking(self, ctx: commands.Context) -> None:
        await albumrank.show_ranking(Scene.from_ctx(ctx))


async def setup(bot) -> None:
    await bot.add_cog(AlbumCog(bot))
