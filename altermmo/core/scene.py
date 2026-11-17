"""``Scene`` - where a piece of gameplay happens.

Legacy game functions took a ``ctx`` (``commands.Context``).  Background loops
have no command context, so the old bot fetched one specific pinned message at
start-up and built a fake context from it (if that message was ever deleted the
bot could not start its loops).

A ``Scene`` is the explicit replacement: the bot, the channel the action plays
on, and - only when a player triggered it - the player and their message.
Commands build it with :meth:`Scene.from_ctx`, background loops with
``bot.boss_scene()``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import discord

from . import io

if TYPE_CHECKING:  # pragma: no cover
    from discord.ext import commands

    from ..bot import AlterBot


@dataclass(frozen=True)
class Scene:
    bot: "AlterBot"
    channel: discord.TextChannel
    #: The player who started this (None for background flows such as events).
    author: discord.Member | None = None
    #: The message that started this (None for background flows).
    message: discord.Message | None = None

    @classmethod
    def from_ctx(cls, ctx: "commands.Context") -> "Scene":
        return cls(bot=ctx.bot, channel=ctx.channel, author=ctx.author, message=ctx.message)

    @property
    def guild(self) -> discord.Guild:
        """The channel's guild (falls back to the bot's main guild)."""
        guild = getattr(self.channel, "guild", None)
        return guild if guild is not None else self.bot.guild

    def with_author(self, author: discord.Member | None,
                    message: discord.Message | None = None) -> "Scene":
        """Same place, different player (e.g. the member who answered an event)."""
        return replace(self, author=author, message=message)

    async def send(self, content: str | None = None, **kwargs: Any) -> discord.Message:
        """Send to the scene's channel with retries (see :func:`altermmo.core.io.send`)."""
        return await io.send(self.channel, content, **kwargs)
