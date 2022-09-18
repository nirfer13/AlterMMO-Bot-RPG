from discord.ext import commands
import discord
import asyncio


class message(commands.Cog, name="message"):
    def __init__(self, bot):
        self.bot = bot

    async def my_task(self, ctx, messg, time):
        while True:
            await ctx.channel.send(messg)
            await asyncio.sleep(time*3600)  # *3600 time is expected in hours

    # Func to control periodic message, timer is expected to be in hours
    @commands.command(name="periodicMessage", brief="Start periodic message with given time interval which is expected to be in hours")
    @commands.has_any_role("⚡Game Master", "🍕Creative Game Grandmaster")
    async def startMessage(self, ctx, message: str, timer: int):
        self.bot.loop.create_task(self.my_task(ctx, message, timer))

    # command to stop periodic message
    @commands.command(name="stopMessage", brief="stops periodic message")
    async def stopMessage(self, ctx):
        self.bot.loop.stop()


async def setup(bot):
    await bot.add_cog(message(bot))
