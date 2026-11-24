"""The pinned "Panel gry": ``!panel`` (administrators) and its permanent buttons.

``!panel`` posts the panel on the boss channel (the channel it is typed on,
when that is a boss channel - production or test) and pins it; typing it
again replaces the previous panel.  The buttons are registered in
``cog_load``, so panels posted before a restart keep working.  Everything
the panel does lives in :mod:`altermmo.features.hub`.
"""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from ..core import guards, io
from ..features import hub, shortcuts

log = logging.getLogger(__name__)


class HubCog(commands.Cog, name="Hub"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        hub.register(self.bot)
        shortcuts.register(self.bot)  # "Pokaż towarzysza" / "Album" under drops and sacrifices

    @commands.command(name="panel", aliases=["centrum"], brief="Post and pin the 'Panel gry'.")
    @guards.owner_or_admin()
    async def post_panel(self, ctx: commands.Context) -> None:
        bot = self.bot
        channel = ctx.channel if bot.is_boss_channel(ctx.channel) else bot.boss_channel
        try:
            _, pinned = await hub.post_panel(bot, channel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the administrator is told, the old panel stays
            log.exception("Could not post the hub panel.")
            await _say(ctx, f"{hub.MSG_NOT_POSTED} (`{type(exc).__name__}`)")
            return
        text = hub.MSG_POSTED if pinned else hub.MSG_POSTED_NOT_PINNED
        if channel.id != ctx.channel.id or not pinned:
            await _say(ctx, text.format(channel_id=channel.id))
        else:
            await io.safe_react(ctx.message, "✅")


async def _say(ctx: commands.Context, text: str) -> None:
    try:
        await io.send(ctx.channel, text)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - an answer to the administrator is never worth an error
        log.debug("Could not answer !panel.", exc_info=True)


async def setup(bot) -> None:
    await bot.add_cog(HubCog(bot))
