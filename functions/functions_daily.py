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

            if loot['weight'] > 100:
                if loot['id'] == 10:
                    check = await functions_pets.assign_pet(self, ctx, BossHunter)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet assigning.")
                elif loot['id'] == 11:
                    await functions_pets.assign_scroll(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                elif loot['id'] == 9:
                    await functions_pets.assign_shard(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                elif loot['id'] == 12:
                    check = await functions_pets.level_up_pet(self, ctx, BossHunter.id)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet leveling.")
                elif loot['id'] == 13:
                    check = await functions_pets.assign_rebirth_stone(self, ctx, 1, BossHunter.id)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during rebirth stone assigning.")
                elif loot['id'] == 14:
                    check = await functions_pets.assign_mirror(self, ctx, 1, BossHunter.id)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during mirror assigning.")
                else:
                    drop_message += "👉 " + loot['descr'] + "\n"
                loot['weight'] -= 100
                if random.random()*100 <= loot['weight'] and loot['id'] != 10:
                    drop_message += "👉 " + loot['descr'] + " (Bonus)\n"
            elif random.random()*100 <= loot['weight']:
                if loot['id'] == 10:
                    check = await functions_pets.assign_pet(self, ctx, BossHunter)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet assigning.")
                elif loot['id'] == 11:
                    await functions_pets.assign_scroll(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                elif loot['id'] == 9:
                    await functions_pets.assign_shard(self, ctx, 1, BossHunter.id)
                    drop_message += "👉 " + loot['descr'] + "\n"
                elif loot['id'] == 12:
                    check = await functions_pets.level_up_pet(self, ctx, BossHunter.id)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet leveling.")
                elif loot['id'] == 13:
                    check = await functions_pets.assign_rebirth_stone(self, ctx, 1, BossHunter.id)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during rebirth stone assigning.")
                elif loot['id'] == 14:
                    check = await functions_pets.assign_mirror(self, ctx, 1, BossHunter.id)
                    if check:
                        drop_message += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during mirror assigning.")
                else:
                    drop_message += "👉 " + loot['descr'] + "\n"

        title = 'Drop - ' + str(BossHunter)
        #Embed create
        embed=discord.Embed(title=title,
                            url='https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png',
                            description='Otrzymałeś:\n' + drop_message, color=0xfcdb03)
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
        add_desc += "\n\n*Inny gracz może dołączyć do polowania wpisując natychmiast $polowanie*."

        print("File with mob picture generated.")

        #title
        if rarity == 0:
            eTitle = f"💀 Zwykły {boss_player}! 💀"
        elif rarity == 1:
            eTitle = f"💀 Rzadki {boss_player}! 💀"
        elif rarity == 2:
            eTitle = f"💀 Unikalny {boss_player}! 💀"
        else:
            eTitle = f"💀 Unikalny {boss_player}! 💀"

        #description
        if rarity == 0:
            eDescr = "Na polowaniu pojawił się zwykły potwór! ⚔️" + add_desc
        elif rarity == 1:
            eDescr = "Na polowaniu pojawił się rzadki potwór! ⚔️" + add_desc
        elif rarity == 2:
            eDescr = "Na polowaniu pojawił się UNIKALNY potwór! ⚔️" + add_desc
        else:
            eDescr = "Na polowaniu pojawił się UNIKALNY potwór! ⚔️" + add_desc

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

        chatChannel = self.bot.get_channel(776379796367212594)
        await chatChannel.send("Na kanale <#970684202880204831> poległeś z ręki potwora <@" + str(userID) + ">, a groziło to banem... <:RIP:912797982917816341> Musisz odpocząć od przygód aż do momentu zabicia następnego bossa.")


    #function to carry fight by single player with daily boss
    global hunt_mobs
    async def hunt_mobs(self, ctx, BOSSRARITY, is_player_boss, player_boss):

        # Cooperative hunting
        #Fight Check Function
        def check(author, player_list):
            def inner_check(message):
                if message.author in player_list and not DebugMode:
                    print("Group fight init error: player already exists!")
                    return False
                else:
                    if message.content.lower() == "$polowanie":
                        return True
                    else:
                        print("Group init error: wrong message!")
                        return False
            return inner_check
        
        player_list = [ctx.author]
        timeout = 5
        while True:
            try:
                another_atk_cmd = await self.bot.wait_for('message',
                                                                timeout=timeout,
                                                                check=check(ctx.author, player_list))
                player_list.append(another_atk_cmd.author)
                print(player_list)
                if len(player_list) >= 3:
                    break
            except asyncio.TimeoutError:
                break

        player_list_string = ""
        if len(player_list) > 0:
            for player in player_list:
                player_list_string = player_list_string + ("<@" + str(player.id) + "> ")
            print(player_list)

        async with ctx.typing():
            if len(player_list) > 1:
                await ctx.channel.send('Rozpoczęliście polowanie ' + player_list_string + '<:REEeee:790963160495947856>! Wpiszcie **słowa przypisane do Was** tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpiszcie słowa bez spacji! Przygotujcie się!')
            else:
                await ctx.channel.send('Polowanie się rozpoczyna ' + player_list_string + '<:REEeee:790963160495947856>! Wpisz pojawiające się słowa tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez spacji! Przygotuj się!')

        #Load modifiers
        modifiers = await functions_modifiers.load_modifiers(self, ctx)

        #Load pet skills
        pet_skills_dict = {}
        for player in player_list:
            pet_skill = await functions_pets.get_pet_skills(self, player.id)
            pet_skills_dict[player.id] = pet_skill

        await asyncio.sleep(9)

        #Start time counting
        startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

        await ctx.channel.send('Uwaga!!! 3...')
        await asyncio.sleep(1)
        await ctx.channel.send('... 2...')
        await asyncio.sleep(1)
        await ctx.channel.send('... 1...')
        await asyncio.sleep(1)

        #Random the message and requested action
        requestedAction = [("unik", "atak", "paruj", "skok", "bieg", "turlaj", "czaruj", "blok", "skacz", "akcja", "krzyk", "ruch", "posuw", "impet", "zryw"), ("Potwór szarżuje na Ciebie! Wpisz **U N I K**", "Potwór zawahał się! Teraz! Wpisz **A T A K**", "Potwór atakuje, nie masz miejsca na ucieczkę, wpisz **P A R U J**", 
        "Potwór próbuje ataku w nogi, wpisz **S K O K**", "Potwór szykuje potężny atak o szerokim zasięgu, wpisz **B I E G**", "Potwór atakuje w powietrzu, wpisz **T U R L A J**", "Potwór rzuca klątwę, wpisz **C Z A R U J**", "Potwór atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K**","Potwór próbuje ataku w nogi, wpisz **S K A C Z**","Potwór szarżuje na Ciebie, zrób coś, wpisz **A K C J A**", "Nie masz pojęcia co robić, wpisz **K R Z Y K**", "Musisz zrobić cokolwiek, wpisz **R U C H**", "Potwór rzuca głazem w Twoją stronę, wpisz **P O S U W**", "Dostrzegasz szansę na uderzenie, wpisz **I M P E T**", "Pojawiła się chwila zawachania potwora, wpisz **Z R Y W**")]

        bossHP = fRandomBossHp(BOSSRARITY)
        bossHP = int(bossHP * (1+(modifiers["hp_boost_perc"] - modifiers["hp_reduced_perc"]
                                  - pet_skills_dict[player_list[0].id]["LOWHP_PERC"])/100))
        print("Wylosowane HP bossa: " + str(bossHP))
        iterator = 0

        #Define check function
        channel = ctx.channel
        def check(ctx, player_list):
            def inner(msg):
                return (msg.channel == channel) and (msg.author in player_list)
            return inner

        #Start time counting
        startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

        #Start whole fight
        while True: #start boss turn
            iterator += 1

            choosenAction = random.randint(0,len(requestedAction[0])-1)
            boss_hunter = random.choice(player_list)

            try:
                if not random.random()*100 < pet_skills_dict[boss_hunter.id]["REPLACE_PERC"]:

                    #Send proper action request on chat
                    await ctx.channel.send(str(iterator) + '. **'  + str(boss_hunter) + "**: " + requestedAction[1][choosenAction])

                    #Longer timeout for the first action
                    if iterator == 1:
                        cmdTimeout = 15
                    else:
                        #Timeout depends on boss rarity
                        print("Mob rarity before timeout calc: " + str(BOSSRARITY))
                        cmdTimeout = 5 - BOSSRARITY
                        cmdTimeout = cmdTimeout * (100 - modifiers["time_reduced_perc"] +
                                    float(pet_skills_dict[boss_hunter.id]["SLOW_PERC"]))/100
                    msg = await self.bot.wait_for('message', check=check(ctx, player_list), timeout=cmdTimeout)
                    response = str(msg.content)
                else:
                    response = requestedAction[0][choosenAction]
                    #Send proper action request on chat
                    await ctx.channel.send('~~' + str(iterator) + '. ' +
                                            requestedAction[1][choosenAction] +
                                            '~~. Twój towarzysz wyprowadza atak!')

                if response.lower() == requestedAction[0][choosenAction] and msg.author == boss_hunter:

                    # Crit from pet
                    if random.random()*100 < pet_skills_dict[boss_hunter.id]["CRIT_PERC"]:
                        iterator += 1

                    #Boss killed?
                    if iterator >= bossHP:

                        await ctx.channel.send('Brawo ' + player_list_string + '<:ok:990161663053422592> Potwór pokonany!')

                        #Time record
                        endTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                        recordTime = endTime - startTime
                        recordTurnTime = recordTime/bossHP
                        await ctx.channel.send('Zabicie potwora zajęło: ' + str(recordTime).lstrip('0:00:') + ' sekundy! Jedna tura zajęła średnio ' + str(recordTurnTime).lstrip('0:00:') + ' sekundy!')
                        previousRecord, Nick = await functions_database.readRecordTable(self, ctx)

                        for hunter in player_list:
                            #Ranking - add points
                            await functions_database.updateRankingTable(self, ctx, hunter.id, BOSSRARITY, 0)

                            #Randomize Loot
                            if len(player_list) > 1:
                                party_boost = 10 * (len(player_list) - 1)
                            else:
                                party_boost = 0
                            drop_boost = modifiers["drop_boost_perc"] + float(pet_skills_dict[hunter.id]["DROP_PERC"]) + party_boost
                            dropLoot = await randLoot(self, ctx, BOSSRARITY, hunter, drop_boost)
                        break
                    else:
                        print("Good command.")
                else:
                    if not random.random()*100 < pet_skills_dict[boss_hunter.id]["DEF_PERC"]:
                        await ctx.channel.send('Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie jutro lub poczekaj na modlitwę Crafterów i Patronów! <:RIP:912797982917816341>\n\n*Możesz również dołączyć do polowania innych graczy.*')
                        logChannel = self.bot.get_channel(881090112576962560)
                        if is_player_boss == False:
                            pass
                            #await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " pomylił się i nie zabił daily moba.")
                        else:
                            pass
                            #await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " pomylił się i nie zabił daily moba. Mobem był " + player_boss.name + ".")
                            await functions_pets.assign_scroll(self, ctx, BOSSRARITY+1, player_boss.id)

                        if modifiers["ban_loser"] > 0:
                            for hunter in player_list:
                                print("Hunter " + str(ctx.author.name) + " is dead.")
                                await setDeadHunters(self, ctx, hunter.id)

                        return False
                    else:
                        await ctx.channel.send('Pomyliłeś się, ale Twój towarzysz Cię chroni!')

            except asyncio.TimeoutError:
                if not random.random()*100 < pet_skills_dict[boss_hunter.id]["DEF_PERC"]:
                    await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400> Odpocznij i spróbuj jutro! <:RIP:912797982917816341>\n\n*Możesz również dołączyć do polowania innych graczy.*')
                    logChannel = self.bot.get_channel(881090112576962560)
                    if is_player_boss == False:
                        pass
                        #await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " nie zdążył wpisać komend i potwór przepadł.")
                    else:
                        #await  logChannel.send("<@291836779495948288>!   " + ctx.author.name + " nie zdążył wpisać komend i potwór przepadł. Potworem był " + player_boss.name + ".")
                        await functions_pets.assign_scroll(self, ctx, BOSSRARITY+1, player_boss.id)

                    if modifiers["ban_loser"] > 0:
                        print("Hunter " + str(ctx.author.name) + " is dead.")
                        await setDeadHunters(self, ctx, ctx.author.id)

                    return False
                else:
                    await ctx.channel.send('Nie zdążyłeś, ale Twój towarzysz Cię chroni!')

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
    async def clear_daily_file(self):

        open('daily_player_cd.txt', 'w').close()
        logChannel = self.bot.get_channel(881090112576962560)
        await logChannel.send("Zresetowano cooldown na daily.")

def setup(bot):
    bot.add_cog(functions_daily(bot))
