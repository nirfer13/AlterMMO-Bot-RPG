"""File with all events."""

import json
import discord
import random
import asyncio
from discord.ext import commands

import functions_daily

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsEvents(commands.Cog, name="FunctionsEvents"):

    def __init__(self, bot):
        self.bot = bot

    global spawn_chest
    async def spawn_chest(self, ctx):
        """Function to spawn chest."""

        print("Treasure chest spawn.")

        e_title = "<:Loot:912797849916436570> Skrzynia ze skarbami! <:Loot:912797849916436570>"

        e_descr = ('Pojawiła się skrzynia ze skarbami! Jedna osoba może ją otworzyć komendą'
        ' **$skrzynia**!'
        '\n\n*Crafterzy i Patroni mogą wydropić dodatkowe, kosmetyczne przedmioty!*')

        e_color = 0xFCBA03

        e_thumb = 'https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png'

        image_name = "events/chest/" + str(random.randint(0,4)) + ".png"
        file=discord.File(image_name)

        #image
        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)
        embed.set_thumbnail(url=e_thumb)

        await ctx.channel.send(file=file)
        await ctx.send(embed=embed)

    global open_chest
    async def open_chest(self, ctx):
        """Function to open chest."""

        rarity = random.randint(0,100)
        if rarity >= 90:
            rarity = 2
        else:
            rarity = 1
        boost_percent = random.randint(0,25)
        await functions_daily.randLoot(self, ctx, rarity, ctx.author, boost_percent)

    global spawn_invasion
    async def spawn_invasion(self, ctx):
        """Function to spawn invasion."""

        print("Invasion spawn.")

        e_title = "<:RIP:912797982917816341> Inwazja na wioskę! <:RIP:912797982917816341>"

        e_descr = ('Pobliska wioska została zaatakowana przez bezdusznych najeźdźców!\n\n'
        '**Zaoferujcie im swoją pomoc zostawiając reakcję pod tym postem.** Im więcej Was będzie,'
        'tym większa szansa, że odpędzicie najeźdćów i odbijecie zrabowane przez nich łupy!'
        '\n\n*Crafterzy i Patroni mogą wydropić dodatkowe, kosmetyczne przedmioty!*')

        e_color = 0xFC0303

        e_thumb = 'https://www.altermmo.pl/wp-content/uploads/altermmo-3-112.png'

        image_name = "events/invasion/" + str(random.randint(0,4)) + ".png"
        file=discord.File(image_name)

        #image
        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)
        embed.set_thumbnail(url=e_thumb)

        await ctx.channel.send(file=file)
        msg = await ctx.send(embed=embed)

        await msg.add_reaction("⚔️")
        if DebugMode:
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(600)

        users = []
        message = await ctx.channel.fetch_message(msg.id)
        for reaction in message.reactions:
            async for user in reaction.users():
                guild = message.guild
                if guild.get_member(user.id) is not None and user.id != 859729615123251200 and user.id != 971322848616525874 and user not in users:
                    users.append(user)

        chance = random.randint(0,100)

        if chance <= len(users)*10 + 10:
            for user in users:
                rarity = random.randint(0,100)
                if rarity >= 90:
                    rarity = 2
                else:
                    rarity = 1
                boost_percent = len(users) * 10
                print(boost_percent)
                await functions_daily.randLoot(self, ctx, rarity, user, boost_percent)
        else:
            image_name = "events/invasion/5.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            await ctx.send('Niestety nie udało się obronić wioski... Mężczyźni leżą w kałuży krwi, dzieci'
                     ' zostały rozszarpane na kwałki, a kobiety skończą jako kurtyzany. '
                     'Co z Was za bohaterowie? <:PepeKMS:783992337029267487>')

def setup(bot):
    """Load the FunctionsEvents cog."""
    bot.add_cog(FunctionsEvents(bot))
