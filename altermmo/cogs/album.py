"""The pet album: ``$album`` (alias ``$kolekcja``) for supporters.

The game lives in :mod:`altermmo.features.album`.  ``$album`` only shows
something, so it works on every channel of the server and follows the busy
policy of the display commands (``guards.unless_busy(read_only=True)``), like
``$osiagniecia``.  The hub panel's "Album" button runs the same thing.  The
buttons under the album are answered by its own view; nothing is registered
here (they die with the panel, a click after a restart is answered as stale).
"""

from __future__ import annotations

from discord.ext import commands

from ..core import guards
from ..core.scene import Scene
from ..features import album


class AlbumCog(commands.Cog, name="Album"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="album", aliases=["kolekcja"], brief="Shows the supporter's pet album.")
    @guards.unless_busy(read_only=True)
    async def show(self, ctx: commands.Context) -> None:
        await album.show_album(Scene.from_ctx(ctx))


async def setup(bot) -> None:
    await bot.add_cog(AlbumCog(bot))
