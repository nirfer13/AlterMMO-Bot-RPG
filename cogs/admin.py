from discord.ext import commands
import discord
import asyncio
from datetime import datetime

# Class that holds commands related to administration of server

class admin(commands.Cog, name="admin"):
    def __init__(self, bot):
        self.bot = bot

    # mass ban all users that joined server in last {timer} minutes
    @commands.command(name="massBan", brief = "ban all users that joined in last {given} minutes")
    @commands.has_permissions(ban_members=True)
    @commands.has_permissions(administrator=True)
    async def massBan(self, ctx, timer: int, reason="Not specified"):
        now = datetime.utcnow()
        for member in ctx.guild.members:
            diffTime = (now - member.joined_at).total_seconds() / 60.0
            if diffTime <= timer:
                print(member, " will be banned")
                await member.ban()


def setup(bot):
    bot.add_cog(admin(bot))
