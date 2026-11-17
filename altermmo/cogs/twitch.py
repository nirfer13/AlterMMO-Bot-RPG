"""Admin commands for the Twitch integration (roles and treasures from the Twitch bot's tables)."""

from __future__ import annotations

from discord.ext import commands

from ..core import guards
from ..core.scene import Scene
from ..features import twitch


class TwitchCog(commands.Cog, name="Twitch"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="checkTwitch", brief="Check if the user has connected Twitch account.")
    @guards.owner_or_admin()
    async def check_twitch(self, ctx: commands.Context, user_id: str) -> None:
        await twitch.get_user_twitch_name(Scene.from_ctx(ctx), user_id.strip("<@!>"))

    @commands.command(name="getActiveTwitch", brief="Get all users with sufficient watchtime.")
    @guards.owner_or_admin()
    async def assign_roles_watchtime(self, ctx: commands.Context) -> None:
        await twitch.assign_roles_watchtime(self.bot)

    @commands.command(name="getVipModTwitch", brief="Get all users with VIP or Mod Twitch role.")
    @guards.owner_or_admin()
    async def assign_roles_vip_mod(self, ctx: commands.Context) -> None:
        await twitch.assign_roles_vip_mod(self.bot)

    @commands.command(name="getWriterTwitch", brief="Get all users with sufficient messages count.")
    @guards.owner_or_admin()
    async def assign_roles_messages(self, ctx: commands.Context) -> None:
        await twitch.assign_roles_messages(self.bot)

    @commands.command(name="checkTreasure", brief="Get all users who should be rewarded")
    @guards.owner_or_admin()
    async def check_treasure(self, ctx: commands.Context) -> None:
        await twitch.check_treasure(self.bot)


async def setup(bot) -> None:
    await bot.add_cog(TwitchCog(bot))
