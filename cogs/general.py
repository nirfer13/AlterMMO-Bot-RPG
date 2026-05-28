from discord.ext import commands
import discord
from datetime import datetime, timezone, timedelta

import datetime
import sys
import time
sys.path.insert(1, './functions/')
import functions_general

# general bag for commands that does not fit anywhere else

class general(commands.Cog, name="general"):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ping", brief="Check if bot is alive")
    async def ping(self, ctx):
        ws_latency = round(self.bot.latency * 1000)

        start = time.perf_counter()
        message = await ctx.send("Sprawdzam...")
        end = time.perf_counter()

        api_latency = round((end - start) * 1000)

        embed = discord.Embed(
            title="🏓 Pong!",
            description=(
                f"Gateway: {ws_latency} ms\n"
                f"API (send message): {api_latency} ms"
            ),
            color=0x42F34C
        )

        await message.edit(content=None, embed=embed)
		
	# saving history channel to .txt format, defualt file will be name channelHistory and only 100 messages will be saved
    # both parameters can be changed in commands params
    # simple formmating userName | channelName | message
    @commands.command(brief="Saves messages from current channel to txt, default amount of messages == 100")
    @commands.has_permissions(administrator=True)
    async def saveToTxt(self, ctx, limit: int = 100):
        print("1")
        text_channel_list = []
        print("2")
        guild = ctx.guild
        limit = int(limit)
        print("3")
        messages = [message async for message in ctx.channel.history(limit=limit)]
        messages.reverse()
        now = datetime.datetime.utcnow() # current date and time
        print("6")
        file_name = now.strftime("%m_%d_%Y_%H_%M_%S")
        print("7")
        with open(file_name + ".txt", "a+", encoding="utf-8") as f:
            for message in messages:
                splittedString = message.content.split("\n", 1)
                first = "<h2><strong>"+ splittedString[0]+"</strong></h2>\n"
                second = ""
                try:
                    second = splittedString[1]
                except:
                    pass

                print(
                    f"{first + second} \n\n",
                    file=f)
        print("8")
        print("last: ", limit, " were saved to file with name: " + file_name)

    # command to clear channel
    @commands.command(name="clear", pass_context = True, brief="Clear channel messages")
    @commands.has_permissions(administrator=True)
    async def clear(self, ctx):
        await functions_general.fClear(self, ctx)
    
    # Just debug action, printing to console when ready and logged in.
    @commands.Cog.listener()
    async def on_ready(self):
        print('ready')
        print('Logged in as:  ', self.bot.user)
        print('ID:  ', self.bot.user.id)

def setup(bot):
    bot.add_cog(general(bot))
