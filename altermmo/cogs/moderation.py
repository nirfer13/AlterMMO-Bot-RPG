"""Server moderation helpers."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..core import io

log = logging.getLogger(__name__)


class ModerationCog(commands.Cog, name="Moderation"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="massBan", brief="ban all users that joined in last {given} minutes")
    @commands.has_permissions(ban_members=True, administrator=True)
    async def mass_ban(self, ctx: commands.Context, timer: int, *, reason: str = "Not specified") -> None:
        """Ban everybody who joined the server in the last ``timer`` minutes (raid defence)."""
        now = discord.utils.utcnow()
        banned = 0
        failed = 0
        for member in list(ctx.guild.members):
            if member.bot or member.joined_at is None:
                continue
            minutes = (now - member.joined_at).total_seconds() / 60.0
            if minutes > timer:
                continue
            try:
                await member.ban(reason=f"massBan: {reason}")
                banned += 1
                log.warning("massBan: banned %s (joined %.1f minutes ago).", member, minutes)
            except discord.HTTPException as exc:
                failed += 1
                log.warning("massBan: could not ban %s: %s", member, exc)
        await io.send(ctx.channel, f"Zbanowano {banned} użytkowników (nieudane próby: {failed}).")


async def setup(bot) -> None:
    await bot.add_cog(ModerationCog(bot))
