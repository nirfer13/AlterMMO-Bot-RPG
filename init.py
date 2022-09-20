import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import asyncpg
from asyncpg.pool import create_pool
import asyncio


from globals.globalvariables import DebugMode



# token and other needed variables will be hidden in .env file
load_dotenv()
description = 'AlterMMO Discord Bot, Development in progres'
intents = discord.Intents.all()
#intents.members = True

#commands prefix == #
bot = commands.Bot(
    command_prefix='^',
    description=description,
    intents=intents)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send('Poczekaj na odnowienie komendy!')

#database
async def create_db_pool():
    #Establishing the connection
    if DebugMode == False:
        bot.pg_con = await asyncpg.create_pool(os.environ.get("DATABASE_URL"))
    else:
        bot.pg_con = await asyncpg.create_pool(os.environ.get("HEROKU_POSTGRESQL_BRONZE_URL"))    

    print("Connected to database. Pool created.")

async def load_extensions():
    for file in os.listdir("./cogs"):
        if file.endswith(".py"):
            extension = file[:-3]
            try:
                await bot.load_extension(f"cogs.{extension}")
                print(f"Loaded extension '{extension}'")
            except Exception as e:
                exception = f"{type(e).__name__}: {e}"
                print(f"Failed to load extension {extension}\n{exception}")


async def main():
    await load_extensions()
    await bot.start(os.environ.get("TOKEN"))

if __name__ == '__main__':
    try:
        asyncio.get_event_loop().run_until_complete(create_db_pool())
    except:
        print("Database unreachable.")
    asyncio.get_event_loop().run_until_complete(main())


#loads cogs as extentions to bot


