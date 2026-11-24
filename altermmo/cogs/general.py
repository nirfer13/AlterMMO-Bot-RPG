"""Commands that do not fit anywhere else: ``!ping``, ``!clear``, ``!saveToTxt``."""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

from ..core import clock, guards, io

log = logging.getLogger(__name__)


class GeneralCog(commands.Cog, name="General"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="ping", brief="Check if bot is alive")
    async def ping(self, ctx: commands.Context) -> None:
        latency = self.bot.latency
        ws_latency = round(latency * 1000) if latency == latency else -1  # NaN before the first heartbeat

        start = time.perf_counter()
        message = await io.send(ctx.channel, "Sprawdzam...")
        api_latency = round((time.perf_counter() - start) * 1000)

        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Gateway: {ws_latency} ms\nAPI (send message): {api_latency} ms",
            color=0x42F34C,
        )
        await message.edit(content=None, embed=embed)

    @commands.command(name="saveToTxt",
                      brief="Saves messages from current channel to txt, default amount of messages == 100")
    @guards.owner_or_admin()
    async def save_to_txt(self, ctx: commands.Context, limit: int = 100) -> None:
        """Dump the last ``limit`` messages of this channel into data/exports/<timestamp>.txt.

        The first line of every message becomes an ``<h2>`` heading (the format
        used for pasting news into the website).
        """
        messages = [message async for message in ctx.channel.history(limit=limit)]
        messages.reverse()
        exports = self.bot.settings.data_dir / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        path = exports / (clock.now().strftime("%m_%d_%Y_%H_%M_%S") + ".txt")
        with path.open("a+", encoding="utf-8") as handle:
            for message in messages:
                first, _, rest = message.content.partition("\n")
                handle.write(f"<h2><strong>{first}</strong></h2>\n{rest} \n\n\n")
        log.info("Saved the last %d messages of #%s to %s", len(messages), ctx.channel, path)
        await io.send(ctx.channel, f"Zapisano {len(messages)} wiadomości do pliku `{path.name}`.")

    @commands.command(name="clear", brief="Clear channel messages")
    @guards.owner_or_admin()
    async def clear(self, ctx: commands.Context) -> None:
        await io.clear_channel(ctx.channel)


async def setup(bot) -> None:
    await bot.add_cog(GeneralCog(bot))
