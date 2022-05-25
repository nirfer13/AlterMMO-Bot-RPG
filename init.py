import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import random
import time
import asyncio
import asyncpg
from asyncpg.pool import create_pool

# token and other needed variables will be hidden in .env file
load_dotenv()
description = 'AlterMMO Discord Bot, Development in progres'
intents = discord.Intents.default()
intents.members = True

#commands prefix == #
bot = commands.Bot(
    command_prefix='^',
    description=description,
    intents=intents)

#database
async def create_db_pool():
    bot.pg_con = await asyncpg.create_pool(database="d2am99h8cekkeo", user="hkheidzoebbhxe", password='d545d32cdd85d3018184ff7f82a9129180f577ed9e03e71f9fe05e93d9cd19ee', host='ec2-63-35-156-160.eu-west-1.compute.amazonaws.com')
    print("Connected to database. Pool created.")

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
    #bot.loop.run_until_complete(create_db_pool())
    bot.run(os.environ.get("TOKEN"))


