"""Clicks nobody listens to any more.

Flow buttons (:class:`altermmo.core.ui.ClickView` and friends) die with the
coroutine that posted them: when an event is over, or when the bot was
restarted while a message with buttons was on the channel.  discord.py simply
ignores a click on such a button and Discord then tells the player "this
interaction failed".  This cog answers those clicks ("this button no longer
works") and takes the dead buttons off the message.

Permanent buttons (fixed ``custom_id``, registered with ``ui.register``) are
not touched: their registered view answers them.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..core import ui

log = logging.getLogger(__name__)


class UiCog(commands.Cog, name="Ui"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        if not custom_id.startswith(ui.FLOW_PREFIX):
            return
        message = interaction.message
        if message is not None and ui.is_live(message.id):
            return  # a running flow answers this one
        log.info("Stale button %s clicked by %s.", custom_id, getattr(interaction.user, "id", "?"))
        await ui.handle_stale(interaction)


async def setup(bot) -> None:
    await bot.add_cog(UiCog(bot))
