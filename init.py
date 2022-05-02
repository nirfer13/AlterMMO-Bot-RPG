import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import random
import time
import asyncio

# token and other needed variables will be hidden in .env file
load_dotenv()
description = 'AlterMMO Discord Bot, Development in progres'
intents = discord.Intents.default()
intents.members = True

#commands prefix == #
bot = commands.Bot(
    command_prefix='#',
    description=description,
    intents=intents)


#loads cogs as extentions to bot
if __name__ == '__main__':
    for file in os.listdir("./cogs"):
        if file.endswith(".py"):
            extension = file[:-3]
            try:
                bot.load_extension(f"cogs.{extension}")
                print(f"Loaded extension '{extension}'")
            except Exception as e:
                exception = f"{type(e).__name__}: {e}"
                print(f"Failed to load extension {extension}\n{exception}")
    bot.run(os.environ.get("TOKEN"))
