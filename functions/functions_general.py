from discord.ext import commands
import discord
import asyncio
from datetime import datetime

# general bag for functions

class functions_general(commands.Cog, name="functions_general"):
    def __init__(self, bot):
        self.bot = bot

    #function to clear
    global fClear
    async def fClear(self, ctx):
        channel = ctx.channel
        count = 0
        async for _ in channel.history(limit=None):
            count += 1
        await channel.purge(limit=count-1)


def setup(bot):
    bot.add_cog(functions_general(bot))
