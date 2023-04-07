from discord.ext import commands
import discord
import asyncio
import random
import json
import os
import math

from datetime import datetime, timedelta
from PIL import Image
from io import BytesIO

import datetime

#Import Globals
from globals.globalvariables import DebugMode
import functions_database
import functions_modifiers
import functions_pets

class functions_daily(commands.Cog, name="functions_daily"):
    def __init__(self, bot):
        self.bot = bot

    #define Loot
    global randLoot
    async def randLoot(self, ctx, srarity, BossHunter, boost_percent):
        """Randomize loot from boss."""

        rarity = int(srarity)
        boost_percent = int(boost_percent)

        with open("mobLootConfig.json", encoding='utf-8') as jsonFile:
            json_object = json.loads(jsonFile.read())

        if rarity == 0:
            json_object = json_object['loot_details_Normal']
        elif rarity == 1:
            json_object = json_object['loot_details_Rare']
        elif rarity == 2:
            json_object = json_object['loot_details_Epic']
        else:
            json_object = json_object['loot_details_Epic']

        drop_message = ""
        for loot in json_object:
            loot['weight'] = (boost_percent/100 + 1) * loot['weight']
            print(loot['descr'])
            print(loot['weight'])
            if loot['weight'] > 100:
                if loot['id'] == 10:
                    print("Egg dropped")
                    check = await functions_pets.assign_pet(self, ctx, BossHunter)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet assigning.")
                elif loot['id'] == 11:
                    print("Add scroll to user")
                    await functions_pets.assign_scroll(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                elif loot['id'] == 9:
                    print("Add shard to user")
                    await functions_pets.assign_shard(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                else:
                    drop_message += "👉 " + loot['descr'] + "\n"
                loot['weight'] -= 100
                if random.random()*100 <= loot['weight'] and loot['id'] != 10:
                    drop_message += "👉 " + loot['descr'] + " (Bonus)\n"
            elif random.random()*100 <= loot['weight']:
                if loot['id'] == 10:
                    print("Egg dropped")
                    check = await functions_pets.assign_pet(self, ctx, BossHunter)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet assigning.")
                elif loot['id'] == 11:
                    print("Add scroll to user")
                    await functions_pets.assign_scroll(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                elif loot['id'] == 9:
                    print("Add shard to user")
                    await functions_pets.assign_shard(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                else:
                    drop_message += "👉 " + loot['descr'] + "\n"

        title = 'Drop z potwora - ' + str(BossHunter)
        #Embed create
        embed=discord.Embed(title=title,
                            url='https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png',
                            description='Potwór wydropił:\n' + drop_message, color=0xfcdb03)
        embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png')
        embed.set_footer(text='Gratulacje!')
        await ctx.channel.send(embed=embed)

        return drop_message

    #function to send boss image
    global fBossImage
    async def fBossImage(self, ctx, srarity):
        """Create a mob image based on modifiers and rarity."""

        print("Boss spawned. BOSSALIVE = 3")
        rarity = int(srarity)
        if 0 <= rarity <= 1:
            image_number = pow(10,rarity)
            if image_number == 1:
                image_number = 0
            image_name = "mobs/" + str(random.randint(0,6)+image_number) + ".png"
            file=discord.File(image_name)
            boss_player = "mob"
            add_desc = ""
        else:
            image_number = 100
            image_name = "mobs/" + str(100) + ".png"
            file=discord.File(image_name)
            boss_player = "mob"
            add_desc = ""

        #Load modifiers
        modifiers = await functions_modifiers.load_modifiers(self, ctx)
        if modifiers["player_id"] > 0:
            try:
                member = discord.utils.get(ctx.guild.members, id=modifiers["player_id"])
                boss_player = member
                is_player_boss = True
                print(member)
                file = member.avatar_url
                add_desc = "\n\n*Potworem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, "\
                + "to ten gracz zgarnia " + str((rarity+1)) + " zwojów!*"
            except:
                is_player_boss = False
                if 0 <= rarity <= 1:
                    image_number = pow(10,rarity)
                    if image_number == 1:
                        image_number = 0
                    image_name = "mobs/" + str(random.randint(0,6)+image_number) + ".png"
                    file=discord.File(image_name)
                    boss_player = "boss"
                    add_desc = ""
                else:
                    image_number = 100
                    image_name = "mobs/" + str(100) + ".png"
                    file=discord.File(image_name)
                    boss_player = "boss"
                    add_desc = ""
        else:
            is_player_boss = False

        add_desc += await functions_modifiers.load_desc_modifiers(self, ctx)

        print("File with mob picture generated.")

        #title
        if rarity == 0:
            eTitle = f"💀 Zwykły {boss_player}! 💀"
        elif rarity == 1:
            eTitle = f"💀 Rzadki {boss_player}! 💀"
        elif rarity == 2:
            eTitle = f"💀 Wyjątkowy {boss_player}! 💀"
        else:
            eTitle = f"💀 Wyjątkowy {boss_player}! 💀"

        #description
        if rarity == 0:
            eDescr = "Rozpoczynasz codziennie polowanie! Twoją ofiarą będzie zwykły potwór! ⚔️" + add_desc
        elif rarity == 1:
            eDescr = "Rozpoczynasz codziennie polowanie! Twoją ofiarą będzie rzadki potwór! ⚔️" + add_desc
        elif rarity == 2:
            eDescr = "Rozpoczynasz codziennie polowanie! Twoją ofiarą będzie UNIKALNY potwór! ⚔️" + add_desc
        else:
            eDescr = "Rozpoczynasz codziennie polowanie! Twoją ofiarą będzie UNIKALNY potwór! ⚔️" + add_desc

        #color
        if rarity == 0:
            eColor = 0xFFFFFF
        elif rarity == 1:
            eColor = 0x0000FF
        elif rarity == 2:
            eColor = 0xfcba03
        else:
            eColor = 0xfcba03

        #thumb
        if rarity == 0:
            eThumb = 'https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/large-green-square_1f7e9.png'
        elif rarity == 1:
            eThumb = 'https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/large-blue-square_1f7e6.png'
        elif rarity == 2:
            eThumb = 'https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/large-orange-square_1f7ea.png'
        else:
            eThumb = 'https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/large-orange-square_1f7ea.png'

        #image
        embed = discord.Embed(
            title=eTitle,
            description=eDescr,
            color=eColor)
        embed.set_thumbnail(url=eThumb)
        if type(file) == discord.file.File:
            await ctx.channel.send(file=file)
        else:
            await ctx.channel.send(file)
        await ctx.send(embed=embed)

        return is_player_boss, boss_player

    #function to Random BossHP
    global fRandomBossHp
    def fRandomBossHp(BOSSRARITY):
        iBOSSRARITY = int(BOSSRARITY)
        if iBOSSRARITY == 0:
            minHp = 2
            maxHp = 4
        elif iBOSSRARITY == 1:
            minHp = 5
            maxHp = 8
        elif iBOSSRARITY == 2:
            minHp = 9
            maxHp = 12
        else:
            minHp = 9
            maxHp = 12
        bossHP = random.randint(minHp,maxHp)
        return bossHP

    #function to add dead hunter role
    global setDeadHunters
    async def setDeadHunters(self, ctx, userID):
        my_role = discord.utils.get(ctx.guild.roles, id=1091050836303544402)
        members = my_role.members
        print(my_role)
        guild = self.bot.get_guild(686137998177206281)
        user = guild.get_member(int(userID))
        await user.add_roles(my_role)
        print("Dead hunter role granted.")

    #function to carry fight by single player with daily boss
    global hunt_mobs
    async def hunt_mobs(self, ctx, BOSSRARITY, is_player_boss, player_boss):

        async with ctx.typing():
            await ctx.channel.send('Zaatakowałeś potwora <@' + format(ctx.author.id) + '>! <:REEeee:790963160495947856> Wpisz pojawiające się komendy tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Przygotuj się!')

        #Load modifiers
        modifiers = await functions_modifiers.load_modifiers(self, ctx)

        await asyncio.sleep(6)

        #Start time counting
        startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

        #Random the message and requested action
        requestedAction = [("unik", "atak", "paruj", "skok", "bieg", "turlaj", "czaruj", "blok", "skacz", "akcja", "krzyk", "ruch", "posuw", "impet", "zryw"), ("Mob szarżuje na Ciebie! Wpisz **UNIK**", "Mob zawahał się! Teraz! Wpisz **ATAK**", "Mob atakuje, nie masz miejsca na ucieczkę, wpisz **PARUJ**", 
        "Mob próbuje ataku w nogi, wpisz **SKOK**", "Mob szykuje potężny atak o szerokim zasięgu, wpisz **BIEG**", "Mob atakuje w powietrzu, wpisz **TURLAJ**", "Mob rzuca klątwę, wpisz **CZARUJ**", "Mob atakuje, nie masz miejsca na ucieczkę, wpisz **BLOK**","Mob próbuje ataku w nogi, wpisz **SKACZ**","Mob szarżuje na Ciebie, zrób coś, wpisz **AKCJA**", "Nie masz pojęcia co robić, wpisz **KRZYK**", "Musisz zrobić cokolwiek, wpisz **RUCH**", "Mob rzuca głazem w Twoją stronę, wpisz **POSUW**", "Dostrzegasz szansę na uderzenie, wpisz **IMPET**", "Pojawiła się chwila zawachania potwora, wpisz **ZRYW**")]

        bossHP = fRandomBossHp(BOSSRARITY)
        bossHP = int(bossHP * (1+(modifiers["hp_boost_perc"] - modifiers["hp_reduced_perc"])/100))
        print("Wylosowane HP bossa: " + str(bossHP))
        iterator = 0

        #Define check function
        channel = ctx.channel
        def check(ctx):
            def inner(msg):
                return (msg.channel == channel) and (msg.author == ctx.author)
            return inner

        #Start time counting
        startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

        #Start whole fight
        for iterator in range(bossHP): #start boss turn
            iterator += 1

            choosenAction = random.randint(0,len(requestedAction[0])-1)

            try:
                #Send proper action request on chat
                await ctx.channel.send(str(iterator) + '. ' + requestedAction[1][choosenAction])

                #Longer timeout for the first action
                if iterator == 1:
                    cmdTimeout = 7
                else:
                    #Timeout depends on boss rarity
                    print("Mob rarity before timeout calc: " + str(BOSSRARITY))
                    cmdTimeout = 5 - BOSSRARITY
                    cmdTimeout = cmdTimeout * (100 - modifiers["time_reduced_perc"])/100
                msg = await self.bot.wait_for('message', check=check(ctx), timeout=cmdTimeout)
                response = str(msg.content)

                if response.lower() == requestedAction[0][choosenAction]:
                    #Boss killed?
                    if iterator >= bossHP:

                        await ctx.channel.send('Brawo <@' + format(ctx.author.id) + '>! Pokonałeś potwora! <:ok:990161663053422592>')

                        #Time record
                        endTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                        recordTime = endTime - startTime
                        recordTurnTime = recordTime/bossHP
                        await ctx.channel.send('Zabicie potwora zajęło Ci: ' + str(recordTime).lstrip('0:00:') + ' sekundy! Jedna tura zajęła Ci średnio ' + str(recordTurnTime).lstrip('0:00:') + ' sekundy!')
                        previousRecord, Nick = await functions_database.readRecordTable(self, ctx)

                        #Ranking - add points
                        await functions_database.updateRankingTable(self, ctx, ctx.author.id, BOSSRARITY, 0)

                        #Randomize Loot
                        dropLoot = await randLoot(self, ctx, BOSSRARITY, ctx.author, modifiers["drop_boost_perc"])

                        #Send info about loot
                        logChannel = self.bot.get_channel(881090112576962560)
                        await logChannel.send("<@291836779495948288>!   " + ctx.author.name + " otrzymał: \n" + dropLoot)
                    else:
                        print("Good command.")
                else:
                    await ctx.channel.send('Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie jutro! <:RIP:912797982917816341>')
                    logChannel = self.bot.get_channel(881090112576962560)
                    if is_player_boss == False:
                        await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " pomylił się i nie zabił daily moba.")
                    else:
                        await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " pomylił się i nie zabił daily moba. Mobem był " + player_boss.name + ".")
                        await functions_pets.assign_scroll(self, ctx, BOSSRARITY+1, player_boss.id)

                    if modifiers["ban_loser"] > 0:
                        print("Hunter " + str(ctx.author.name) + " is dead.")
                        await setDeadHunters(self, ctx, ctx.author.id)

                    return False

            except asyncio.TimeoutError:
                await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400> Odpocznij i spróbuj jutro! <:RIP:912797982917816341>')
                logChannel = self.bot.get_channel(881090112576962560)
                if is_player_boss == False:
                    await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " nie zdążył wpisać komend i potwór przepadł.")
                else:
                    await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " nie zdążył wpisać komend i potwór przepadł. Potworem był " + player_boss.name + ".")
                    await functions_pets.assign_scroll(self, ctx, BOSSRARITY+1, player_boss.id)

                if modifiers["ban_loser"] > 0:
                    print("Hunter " + str(ctx.author.name) + " is dead.")
                    await setDeadHunters(self, ctx, ctx.author.id)

                return False

    #function to save daily hunter to file
    global save_daily_to_file
    def save_daily_to_file (player_id):
        player_id = str(player_id)

        with open('daily_player_cd.txt', 'r') as r:
            read_lines = r.readlines()
        r.close()
        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        if player_id not in new_list:
            with open('daily_player_cd.txt', 'a') as f:
                f.write(str(player_id) + '\n')

    #function to read daily hunter from file
    global load_daily_from_file
    def load_daily_from_file(player_id):
        player_id = str(player_id)

        with open('daily_player_cd.txt', 'r') as r:
            read_lines = r.readlines()

        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        print(new_list)

        return player_id in new_list

    #function to read daily hunter from file
    global clear_daily_file
    def clear_daily_file():

        open('daily_player_cd.txt', 'w').close()

def setup(bot):
    bot.add_cog(functions_daily(bot))
