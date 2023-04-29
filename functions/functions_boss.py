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
import functions_expsum

class functions_boss(commands.Cog, name="functions_boss"):
    def __init__(self, bot):
        self.bot = bot

    #define Loot
    global randLoot
    async def randLoot(self, ctx, srarity, BossHunter, boost_percent):
        """Randomize loot from boss."""

        rarity = int(srarity)
        boost_percent = int(boost_percent)

        with open("lootConfig.json", encoding='utf-8') as jsonFile:
            jsonObject = json.loads(jsonFile.read())

        if rarity == 0:
            jsonObject = jsonObject['loot_details_Normal']
        elif rarity == 1:
            jsonObject = jsonObject['loot_details_Rare']
        elif rarity == 2:
            jsonObject = jsonObject['loot_details_Epic']
        elif rarity == 3:
            jsonObject = jsonObject['loot_details_Legend']
        else:
            jsonObject = jsonObject['loot_details_Legend']

        dropMessage = ""
        for loot in jsonObject:
            loot['weight'] = (boost_percent/100 + 1) * loot['weight']

            # Bonus drop
            if loot['weight'] > 100:

                # Exp dropped
                if loot['id'] == 0:
                    dropMessage += "👉 " + loot['descr'] + "\n"
                    exp = [int(s) for s in loot['descr'].split() if s.isdigit()]
                    functions_expsum.add_file_exp(self, BossHunter.id, exp[0])
                # Pet dropped
                elif loot['id'] == 10:
                    check = await functions_pets.assign_pet(self, ctx, BossHunter)
                    if check:
                        dropMessage += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet assigning.")
                # Other drop
                else:
                    dropMessage += "👉 " + loot['descr'] + "\n"

                loot['weight'] -= 100

                # Normal drop after bonus remove
                if random.random()*100 <= loot['weight']:
                    # Exp dropped
                    if loot['id'] == 0:
                        dropMessage += "👉 " + loot['descr'] + "\n"
                        exp = [int(s) for s in loot['descr'].split() if s.isdigit()]
                        functions_expsum.add_file_exp(self, BossHunter.id, exp[0])
                    # Egg dropped
                    elif loot['id'] == 10:
                        pass
                    # Other drop
                    else:
                        dropMessage += "👉 " + loot['descr'] + " (Bonus)\n"

            # Normal drop
            elif random.random()*100 <= loot['weight']:

                # Exp dropped
                if loot['id'] == 0:
                    dropMessage += "👉 " + loot['descr'] + "\n"
                    exp = [int(s) for s in loot['descr'].split() if s.isdigit()]
                    functions_expsum.add_file_exp(self, BossHunter.id, exp[0])
                # Egg dropped
                elif loot['id'] == 10:
                    print("Egg dropped")
                    check = await functions_pets.assign_pet(self, ctx, BossHunter)
                    if check:
                        print("PET ASSIGNED")
                        dropMessage += "👉 " + loot['descr'] + "\n"
                    else:
                        print("Failed to check database during pet assigning.")
                # Other drop
                else:
                    dropMessage += "👉 " + loot['descr'] + "\n"

        title = 'Boss Drop - ' + str(BossHunter)
        #Embed create
        embed=discord.Embed(title=title,
                            url='https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png',
                            description='Boss wydropił:\n' + dropMessage, color=0xfcdb03)
        embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png')
        embed.set_footer(text='Gratulacje!')
        await ctx.channel.send(embed=embed)

        return dropMessage

    #function to send boss image
    global fBossImage
    async def fBossImage(self, ctx, srarity):
        """Create a boss image based on modifiers and random crafter."""

        print("Boss spawned. BOSSALIVE = 3")
        rarity = int(srarity)

        #image name
        percentage = random.randint(0,100)
        is_player_boss = percentage >= 50
        print("Boss player? " + str(is_player_boss) + " " + str(percentage))

        if is_player_boss:
            my_role = discord.utils.get(ctx.guild.roles, id=687185998550925312)
            members = my_role.members
            boss_player = random.choice(members)
            print(boss_player)
            file = boss_player.avatar_url
            add_desc = "\n\n*Bossem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, "\
            + "to ten gracz zgarnia " + str((rarity+1)*500) + " doświadczenia!*"
        else:
            imageNumber = pow(10,rarity)
            if imageNumber == 1:
                imageNumber = 0
            imageName = "boss/" + str(random.randint(0,7)+imageNumber) + ".gif"
            file=discord.File(imageName)
            boss_player = "boss"
            add_desc = ""

        #Load modifiers
        modifiers = await functions_modifiers.load_modifiers(self, ctx)
        if modifiers["player_id"] > 0:
            try:
                member = discord.utils.get(ctx.guild.members, id=modifiers["player_id"])
                boss_player = member
                print(member)
                file = member.avatar_url
                add_desc = "\n\n*Bossem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, "\
                + "to ten gracz zgarnia " + str((rarity+1)*500) + " doświadczenia!*"
            except:
                imageNumber = pow(10,rarity)
                if imageNumber == 1:
                    imageNumber = 0
                imageName = "boss/" + str(random.randint(0,7)+imageNumber) + ".gif"
                file=discord.File(imageName)
                boss_player = "boss"
                add_desc = ""

        add_desc += await functions_modifiers.load_desc_modifiers(self, ctx)

        print("File with boss picture generated.")

        #title
        if rarity == 0:
            eTitle = f"💀 Zwykły {boss_player}! 💀"
        elif rarity == 1:
            eTitle = f"💀 Rzadki {boss_player}! 💀"
        elif rarity == 2:
            eTitle = f"💀 Epicki {boss_player}! 💀"
        elif rarity == 3:
            eTitle = f"💀 Legendarny {boss_player}! 💀"
        else:
            eTitle = f"💀 Legendarny {boss_player}! 💀"

        #description
        if rarity == 0:
            eDescr = "Pojawił się zwykły boss! Zabij go natychmiast, żeby zgarnąć nagrody! Wpisz **$zaatakuj**, żeby rozpocząć walkę! ⚔️" + add_desc
        elif rarity == 1:
            eDescr = "Pojawił się rzadki boss! Zabij go natychmiast, żeby zgarnąć nagrody! Wpisz **$zaatakuj**, żeby rozpocząć walkę! ⚔️" + add_desc
        elif rarity == 2:
            eDescr = "Pojawił się epicki boss! Zabij go natychmiast, żeby zgarnąć nagrody! Wpisz **$zaatakuj**, żeby rozpocząć walkę! ⚔️" + add_desc
        elif rarity == 3:
            eDescr = "Pojawił się LEGENDARNY boss! Nie dasz rady sam, będziesz potrzebował kompanów spośród <@&985071779787730944>! Wpisz **$zaatakuj**, żeby rozpocząć walkę! ⚔️" + add_desc
        else:
            eDescr = "Pojawił się LEGENDARNY boss! Nie dasz rady sam, będziesz potrzebował kompanów spośród <@&985071779787730944>! Wpisz **$zaatakuj**, żeby rozpocząć walkę! ⚔️" + add_desc

        #color
        if rarity == 0:
            eColor = 0xFFFFFF
        elif rarity == 1:
            eColor = 0x0000FF
        elif rarity == 2:
            eColor = 0x800080
        elif rarity == 3:
            eColor = 0x7F5A00
        else:
            eColor = 0x7F5A00

        #thumb
        if rarity == 0:
            eThumb = 'https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/large-green-square_1f7e9.png'
        elif rarity == 1:
            eThumb = 'https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/large-blue-square_1f7e6.png'
        elif rarity == 2:
            eThumb = 'https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/large-purple-square_1f7ea.png'
        elif rarity == 3:
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

    #function to set boss rarity
    global fBOSSRARITY
    def fBOSSRARITY(time):
        iTime = int(time)
        print("Time inside function: " + str(iTime))
        if DebugMode == False:
            if iTime >= 0 and iTime < 14400: #0-4h
                BOSSRARITY = 0
            elif iTime >= 14400 and iTime < 28800: #4-8h
                BOSSRARITY = 1
            elif iTime >= 28800 and iTime < 39600: #8-11h
                BOSSRARITY = 2
            elif iTime >= 39600 and iTime <= 45000: #12h
                BOSSRARITY = 3
            else:
                BOSSRARITY = 0
        else:
            if iTime >= 0 and iTime < 12:
                BOSSRARITY = 0
            elif iTime >= 10 and iTime < 17:
                BOSSRARITY = 1
            elif iTime >= 17 and iTime <= 20:
                BOSSRARITY = 2
            elif iTime >= 20 and iTime <= 25:
                BOSSRARITY = 3
            else:
                BOSSRARITY = 0
        return BOSSRARITY

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
        elif iBOSSRARITY == 3:
            minHp = 15
            maxHp = 20
        else:
            minHp = 15
            maxHp = 20
        bossHP = random.randint(minHp,maxHp)
        return bossHP

    #function to save respawn time to file
    global fSaveRespawnToFile
    def fSaveRespawnToFile (respawnTime, BOSSRARITY, respStarted):
        intRespawnTime = int(respawnTime)
        Time = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
        + datetime.timedelta(seconds=intRespawnTime)
        print("Respawn timestamp saved to file: " + str(Time))
        with open('respawnTimeInfo.txt', 'w') as f:
            f.write(str(Time) + '\n')
            f.write(str(BOSSRARITY) + '\n')
            f.write(str(respStarted))

    #function to read respawn time from file
    global fReadRespawnFromFile
    def fReadRespawnFromFile ():
        with open('respawnTimeInfo.txt', 'r') as r:
            readLines = r.readlines()
        #line 0
        spawnTimestamp = datetime.datetime.strptime(readLines[0].rstrip('\n'), "%Y-%m-%d %H:%M:%S.%f")
        print(str(spawnTimestamp))
        secondsToSpawn = spawnTimestamp - (datetime.datetime.utcnow() + datetime.timedelta(hours=2))
        #line 1
        BOSSRARITY = readLines[1].rstrip('\n')
        #line 2
        respStarted = readLines[2]

        return secondsToSpawn.total_seconds(), BOSSRARITY, respStarted

    #function to get context
    global getContext
    async def getContext(self, channelID, messageID):
        channel = self.bot.get_channel(channelID)
        msg = await channel.fetch_message(messageID)
        ctx = await self.bot.get_context(msg)
        return ctx

    #SINGLE
    #function to single init fight
    global singleInit
    async def singleInit(self, ctx, BOSSALIVE, BOSSRARITY):
        global respawnResume
        await ctx.message.add_reaction("⚔️")
        async with ctx.typing():
            await ctx.channel.send('Jesteś sam?... 120... *Wpisz **$zaatakuj**, jeśli chcesz zawalczyć o bossa!*')
        respawnResume = False
        preFight = False
        mainUser = ctx.author

        #Check Function
        def check(author):
            def inner_check(message):
                if message.author == author:
                    print("Single init error: same author!")
                    return False
                else:
                    if message.content.lower() == "$zaatakuj":
                        return True
                    else:
                        print("Single init error: wrong message!")
                        return False
            return inner_check

        maxWait = 8
        i = range(maxWait)
        for x in i:
            try:
                if DebugMode is True:
                    timeout=1
                else:
                    timeout=15
                anotherAtkCmd = await self.bot.wait_for('message', timeout=timeout, check=check(ctx.author))

                preFight = True
                async with ctx.typing():
                    await asyncio.sleep(2)
                await ctx.channel.send('"**SPOKÓJ!!!**" - *słyszyscie głos w swojej głowie.* "Zachowajcie resztki honoru i wystawcie do walki najsilniejszego z Was."')
                initCmd = random.choice(["Konstantynopolitańczykówna", "degrengolada", "Antropomorfizacja", "Zjawiskowy", "Opsomaniak", "Egzegeza", "Chasydyzm", "Eksplikacja", "Apoteoza", "Buńczuczny","Konstantynopolitańczykówna", "Degrengolada", "Prokrastynacja", "Wszeteczeństwo", "Melepeta", "Imponderabilia", "Inwariant", "Tromtadracja", "Transcendencja", "Lumpenproletariat", "Dezynwoltura", "Eudajmonizm", "Interlokutor", "Indyferentny", "Promiskuityzm", "Transcendencja", "Tromtadracja", "Eudajmonizm", "Historiozofia", "Partykularny", "Manicheizm", "Retroseksualizm", "Autowaloryzacja", "Aseptyczny", "Tanatologia", "Deratyzacja", "Kontestacja", "Falerystyka", "Tromtadracja", "Dezynwoltura", "Eudajmonizm", "Interlokutor", "Indyferentny", "Efemeryczny"])
                await asyncio.sleep(6)
                async with ctx.typing():
                    await ctx.channel.send('"Pierwszy, który PŁYNNIE wypowie zaklęcie, które zaraz zdradzę, będzie godzien walki ze mną!"')
                await asyncio.sleep(8)
                async with ctx.typing():
                    await ctx.channel.send('"Zaklęcie to **' + " ".join(initCmd.upper()) + '**"')
                break
            except asyncio.TimeoutError:
                if x == maxWait-1:
                    pass
                else:
                    await ctx.channel.send("... " + str(15*maxWait -(x+1)*15) + "...")

        if preFight == True:
            Try = 0
            try:
                print("Prefight True")
                while True:
                    spellCmd = await self.bot.wait_for('message', timeout=15)
                    response = str(spellCmd.content)
                    if  response.lower() == initCmd.lower():
                        bossHunterID = spellCmd.author
                        #await Cmd.add_reaction("⚔️")
                        BOSSALIVE = 6
                        return BOSSALIVE, spellCmd.author
                    else:
                        Try+=1
            except asyncio.TimeoutError:
                async with ctx.typing():
                    await ctx.channel.send('"Pfff... Miernoty. Nikt z Was nie jest godzien."')
                logChannel = self.bot.get_channel(881090112576962560)
                await  logChannel.send("Cała grupa nie zdążyła wpisac hasła.")

                #Reset at the end of the fight.
                await resetEnd(self, ctx)
                BOSSALIVE = 0

                return BOSSALIVE, 0

        elif preFight == False:
            print("Prefight False")
            bossHunterID = ctx.author
            print("Boss hunter name: " + bossHunterID.name)
            BOSSALIVE = 6
            return BOSSALIVE, bossHunterID

    #function to carry fight by single player
    global singleFight
    async def singleFight(self, ctx, BOSSALIVE ,bossHunterID, BOSSRARITY, is_player_boss, player_boss):
        if BOSSALIVE == 6:
            async with ctx.typing():
                await ctx.channel.send('Zaatakowałeś bossa <@' + format(bossHunterID.id) + '>! <:REEeee:790963160495947856> Wpisz pojawiające się słowa tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez spacji! Przygotuj się!')

            #Load modifiers
            modifiers = await functions_modifiers.load_modifiers(self, ctx)

            await asyncio.sleep(6)

            #Start time counting
            startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
            #Save resp time and nickname
            await functions_database.updateHistoryTable(self, ctx, bossHunterID.name, startTime)

            await ctx.channel.send('Uwaga!!! 3...')
            await asyncio.sleep(1)
            await ctx.channel.send('... 2...')
            await asyncio.sleep(1)
            await ctx.channel.send('... 1...')
            await asyncio.sleep(1)

            #Random the message and requested action
            requestedAction = [("unik", "atak", "paruj", "skok", "bieg", "turlaj", "czaruj", "blok", "skacz", "akcja", "krzyk", "ruch", "posuw", "impet", "zryw"), ("Boss szarżuje na Ciebie! Wpisz **U N I K**", "Boss zawahał się! Teraz! Wpisz **A T A K**", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **P A R U J**", 
            "Boss próbuje ataku w nogi, wpisz **S K O K**", "Boss szykuje potężny atak o szerokim zasięgu, wpisz **B I E G**", "Boss atakuje w powietrzu, wpisz **T U R L A J**", "Boss rzuca klątwę, wpisz **C Z A R U J**", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K**","Boss próbuje ataku w nogi, wpisz **S K A C Z**","Boss szarżuje na Ciebie, zrób coś, wpisz **A K C J A**", "Nie masz pojęcia co robić, wpisz **K R Z Y K**", "Musisz zrobić cokolwiek, wpisz **R U C H**", "Boss rzuca głazem w Twoją stronę, wpisz **P O S U W**", "Dostrzegasz szansę na uderzenie, wpisz **I M P E T**", "Pojawiła się chwila zawachania potwora, wpisz **Z R Y W**")]

            bossHP = fRandomBossHp(BOSSRARITY)
            bossHP = int(bossHP * (1+(modifiers["hp_boost_perc"] - modifiers["hp_reduced_perc"])/100))
            print("Wylosowane HP bossa: " + str(bossHP))
            iterator = 0

            #Define check function
            channel = ctx.channel
            def check(ctx):
                def inner(msg):
                    return (msg.channel == channel) and (msg.author == bossHunterID)
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
                        print("Boss rarity before timeout calc: " + str(BOSSRARITY))
                        cmdTimeout = 5 - BOSSRARITY
                        cmdTimeout = cmdTimeout * (100 - modifiers["time_reduced_perc"])/100
                    msg = await self.bot.wait_for('message', check=check(ctx), timeout=cmdTimeout)
                    response = str(msg.content)

                    if response.lower() == requestedAction[0][choosenAction]:
                        #Boss killed?
                        if iterator >= bossHP:

                            await ctx.channel.send('Brawo <@' + format(bossHunterID.id) + '>! Pokonałeś bossa! <:POGGIES:790963160491753502><:POGGIES:790963160491753502><:POGGIES:790963160491753502>')

                            #Time record
                            endTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                            recordTime = endTime - startTime
                            recordTurnTime = recordTime/bossHP
                            await ctx.channel.send('Zabicie bossa zajęło Ci: ' + str(recordTime).lstrip('0:00:') + ' sekundy! Jedna tura zajęła Ci średnio ' + str(recordTurnTime).lstrip('0:00:') + ' sekundy!')
                            previousRecord, Nick = await functions_database.readRecordTable(self, ctx)

                            if datetime.datetime.strptime(previousRecord, "%H:%M:%S.%f") > datetime.datetime.strptime(str(recordTurnTime), "%H:%M:%S.%f"):
                                await ctx.channel.send('Pobiłeś rekord i zgarniasz dodatkowe 1500 doświadczenia na discordzie!')
                                logChannel = self.bot.get_channel(881090112576962560)
                                await logChannel.send("<@291836779495948288>!   " + bossHunterID.name + " otrzymał: 1500 expa za rekord")
                                await functions_database.updateRecordTable(self, ctx, bossHunterID.name, recordTurnTime)

                            #Ranking - add points
                            await functions_database.updateRankingTable(self, ctx, bossHunterID.id, BOSSRARITY, modifiers["points_boost"])

                            #Randomize Loot
                            dropLoot = await randLoot(self, ctx, BOSSRARITY, bossHunterID, modifiers["drop_boost_perc"])

                            #Send info about loot
                            logChannel = self.bot.get_channel(881090112576962560)
                            await logChannel.send("<@291836779495948288>!   " + bossHunterID.name + " otrzymał: \n" + dropLoot)

                            #Reset at the end of the fight.
                            await resetEnd(self, ctx)
                            BOSSALIVE = 0

                            return BOSSALIVE

                        else:
                            print("Good command.")
                    else:
                        await ctx.channel.send('Pomyliłeś się! <:PepeHands:783992337377918986> Boss pojawi się później! <:RIP:912797982917816341>')
                        logChannel = self.bot.get_channel(881090112576962560)
                        if is_player_boss == False:
                            await  logChannel.send("<@291836779495948288>!   " + bossHunterID.name + " pomylił się i nie zabił bossa.")
                        else:
                            await  logChannel.send("<@291836779495948288>!   " + bossHunterID.name + " pomylił się i nie zabił bossa. Bossem był " + player_boss.name + " i należy mu się " + str((BOSSRARITY+1)*500) + " expa.")

                        #Reset at the end of the fight.
                        await resetEnd(self, ctx)
                        BOSSALIVE = 0

                        if modifiers["ban_loser"] > 0:
                            print("Hunter " + str(bossHunterID.name) + " is dead.")
                            await setDeadHunters(self, ctx, bossHunterID.id)

                        return BOSSALIVE

                except asyncio.TimeoutError:
                    await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400> Boss pojawi się później! <:RIP:912797982917816341>')
                    logChannel = self.bot.get_channel(881090112576962560)
                    if is_player_boss == False:
                        await  logChannel.send("<@291836779495948288>!   " + bossHunterID.name + " nie zdążył wpisać komend i boss uciekł.")
                    else:
                        await  logChannel.send("<@291836779495948288>!   " + bossHunterID.name + " nie zdążył wpisać komend i boss uciekł. Bossem był " + player_boss.name + " i należy mu się " + str((BOSSRARITY+1)*500) + " expa.")

                    #Reset at the end of the fight.
                    await resetEnd(self, ctx)
                    BOSSALIVE = 0

                    if modifiers["ban_loser"] > 0:
                        print("Hunter " + str(bossHunterID.name) + " is dead.")
                        await setDeadHunters(self, ctx, bossHunterID.id)

                    return BOSSALIVE
        else:
            return 0

    #GROUP
    #function to group init fight
    global groupInit
    async def groupInit(self, ctx, BOSSALIVE, BOSSRARITY):
        global respawnResume
        await ctx.message.add_reaction("⚔️")
        async with ctx.typing():
            await ctx.channel.send('**Znajdź więcej chętnych do walki. Chyba nie myślisz, że sam dasz radę?...** - *słyszysz głos w swojej głowie*... 30...')

        playersNum = 1
        playersList = [ctx.author]
        respawnResume = False
        preFight = False
        mainUser = ctx.author

        #Initialization Check Function
        def check(author, playersList):
            def inner_check(message):
                if message.author in playersList and not DebugMode:
                    print("Group fight init error: player already exists!")
                    return False
                else:
                    if message.content.lower() == "$zaatakuj":
                        return True
                    else:
                        print("Group init error: wrong message!")
                        return False
            return inner_check

        #Fight Check Function
        def checkFight(author, playersList, confirmPlayerList):
            def inner_check(message):
                if message.author in playersList: #and message.author not in confirmPlayerList:
                    if message.content.lower() == "$tak":
                        print("Group fight: player exists!")
                        return True
                else:
                    print("Wrong person or wrong message!")
                    return False
            return inner_check

        maxWait = 6
        timeout = 300
        y=0
        while BOSSALIVE == 5:
            try:
                #Wait for attack command
                anotherAtkCmd = await self.bot.wait_for('message',
                                                        timeout=timeout,
                                                        check=check(ctx.author, playersList))
                playersList.append(anotherAtkCmd.author)

                print(playersList)
                playerListString = " "
                for player in playersList:
                    playerListString = playerListString + ("<@" + str(player.id) + "> ")

                #Found all player
                if len(playersList) >= 4:
                    print("Group fight begins!")

                    async with ctx.typing():
                        await ctx.channel.send('Jesteście gotowi do ataku? Wpiszcie **$tak**.' + playerListString + "... 60...")
                    confirmPlayerList = []

                    while BOSSALIVE == 5:
                        try:
                            #Wait for confirm command
                            print("Waiting for confirm command...")
                            confirmCmd = await self.bot.wait_for('message', timeout=60,
                                                                check=checkFight(ctx.author,
                                                                                 playersList,
                                                                                 confirmPlayerList))
                            await confirmCmd.add_reaction("⚔️")
                            confirmPlayerList.append(confirmCmd.author)

                            #4 players confirmed
                            if len(confirmPlayerList) == 4:
                                BOSSALIVE = 6

                        except asyncio.TimeoutError:
                            #someone did not confirm, return to waiting for players
                            playersList = confirmPlayerList
                            playerListString = " "
                            if len(playersList) > 0:
                                for player in playersList:
                                    playerListString = playerListString + ("<@" + str(player.id) + "> ")
                                print(playersList)
                            y=0
                            await ctx.channel.send('Zbierzcie drużynę przed atakiem...' + playerListString + "Jeśli ktoś chce dołączyć do drużyny, to powinien wpisać **$zaatakuj**.")
                            break
                else:
                    if len(playersList) == 1:
                        await ctx.channel.send("**Jesteś sam! Zbierz więcej łowców!**" + playerListString + "...**  - *słyszycie głos w swojej głowie*... " + str(math.trunc((timeout*maxWait)/60 - (((y+1)*timeout)/60))) + "...")
                    else:
                        await ctx.channel.send("**Nieźle, jest już Was " + str(len(playersList)) + ". Zbierzcie więcej łowców!" + playerListString + "...**  - *słyszycie głos w swojej głowie*... " + str(math.trunc((timeout*maxWait)/60 - (((y+1)*timeout)/60))) + "...")

            except asyncio.TimeoutError:
                y += 1
                if y == 1:
                    await ctx.channel.send("**Tchórzycie, co? Pff...** - *Wpisz $zaatakuj, jeśli chcesz dołączyć do walki!* " + str(math.trunc((timeout*maxWait)/60 - (((y+1)*timeout)/60))) + "...")
                elif y == maxWait - 2:
                    await ctx.channel.send("**A myślałem, że czas na rozrywkę...** - *Wpisz $zaatakuj, jeśli chcesz dołączyć do walki! Ostatnia szansa <@&985071779787730944>!* " + str(math.trunc((timeout*maxWait)/60 - (((y+1)*timeout)/60))) + "...")
                elif y == maxWait - 1:
                    #Reset in the end of the fight
                    await resetEnd(self, ctx)
                    BOSSALIVE = 0
                    await ctx.channel.send("**Miernoty... Wiedziałem, że nie jesteście godni walki ze mną.**  - *słyszycie głos w swojej głowie.*")
                else:
                    await ctx.channel.send("... " + str(math.trunc((timeout*maxWait)/60 - (((y+1)*timeout)/60))) + "...")

        for player in playersList:
            print("Boss hunter: " + str(player.name))

        print("List printed")
        return BOSSALIVE, playersList

    #function to carry fight by single player
    global groupFight
    async def groupFight(self, ctx, BOSSALIVE , playersList, is_player_boss, player_boss):
        if BOSSALIVE == 6:
            BOSSRARITY = 3

            #Load modifiers
            modifiers = await functions_modifiers.load_modifiers(self, ctx)

            playerListString = " "
            for player in playersList:
                playerListString = playerListString + ("<@" + str(player.id) + "> ")

            async with ctx.typing():
                await ctx.channel.send('Zaatakowaliście bossa' + playerListString + '! <:REEeee:790963160495947856> Wpiszcie **słowa przypisane do Was** tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpiszcie słowa bez spacji! Przygotujcie się!')
            await asyncio.sleep(17)
            #Start time counting
            startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
            #Save resp time and nickname
            await functions_database.updateHistoryTable(self, ctx, "Party " + str(playersList[0].name), startTime)

            await ctx.channel.send('Uwaga!!! 3...')
            await asyncio.sleep(1)
            await ctx.channel.send('... 2...')
            await asyncio.sleep(1)
            await ctx.channel.send('... 1...')
            await asyncio.sleep(1)

            #Random the message and requested action
            requestedAction = [("unik", "atak", "paruj", "skok", "bieg", "turlaj", "czaruj", "blok", "skacz", "akcja", "krzyk", "ruch", "posuw", "impet", "zryw"), ("Boss szarżuje na Ciebie! Wpisz **U N I K**", "Boss zawahał się! Teraz! Wpisz **A T A K**", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **P A R U J**", 
            "Boss próbuje ataku w nogi, wpisz **S K O K**", "Boss szykuje potężny atak o szerokim zasięgu, wpisz **B I E G**", "Boss atakuje w powietrzu, wpisz **T U R L A J**", "Boss rzuca klątwę, wpisz **C Z A R U J**", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K**","Boss próbuje ataku w nogi, wpisz **S K A C Z**","Boss szarżuje na Ciebie, zrób coś, wpisz **A K C J A**", "Nie masz pojęcia co robić, wpisz **K R Z Y K**", "Musisz zrobić cokolwiek, wpisz **R U C H**", "Boss rzuca głazem w Twoją stronę, wpisz **P O S U W**", "Dostrzegasz szansę na uderzenie, wpisz **I M P E T**", "Pojawiła się chwila zawachania potwora, wpisz **Z R Y W**")]

            bossHP = fRandomBossHp(BOSSRARITY)
            bossHP = int(bossHP * (1+(modifiers["hp_boost_perc"] - modifiers["hp_reduced_perc"])/100))
            print("Wylosowane HP bossa: " + str(bossHP))
            iterator = 0
            chances = 1

            #Define check function
            channel = ctx.channel
            def check(ctx, playersList):
                def inner(msg):
                    return (msg.channel == channel) and (msg.author in playersList)
                return inner

            #Start time counting
            startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

            #Start whole fight
            for iterator in range(bossHP): #start boss turn
                iterator += 1

                choosenAction = random.randint(0,len(requestedAction[0])-1)
                bossHunterID = random.choice(playersList)

                try:
                    #Send proper action request on chat
                    await ctx.channel.send(str(iterator) + '. **' + str(bossHunterID) + "**: " + requestedAction[1][choosenAction])

                    #Longer timeout for the first action
                    if iterator == 1:
                        cmdTimeout = 7
                    else:
                        #Timeout depends on boss rarity
                        print("Boss rarity before timeout calc: " + str(BOSSRARITY))
                        cmdTimeout = 4
                        cmdTimeout = cmdTimeout * (100 - modifiers["time_reduced_perc"])/100
                    msg = await self.bot.wait_for('message', check=check(ctx, playersList), timeout=cmdTimeout)
                    response = str(msg.content)

                    if response.lower() == requestedAction[0][choosenAction] and msg.author == bossHunterID:
                        #Boss killed?
                        if iterator >= bossHP:

                            await ctx.channel.send('Wow! Świetnie' + playerListString + '! Pokonaliście legendarnego bossa! <:POGGIES:790963160491753502><:POGGIES:790963160491753502><:POGGIES:790963160491753502>')

                            #Time record
                            endTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                            recordTime = endTime - startTime
                            recordTurnTime = recordTime/bossHP
                            await ctx.channel.send('Zabicie bossa zajęło Wam: ' + str(recordTime).lstrip('0:00:') + ' sekundy! Jedna tura zajęła Wam średnio ' + str(recordTurnTime).lstrip('0:00:') + ' sekundy!')
                            previousRecord, Nick = await functions_database.readRecordTable(self, ctx)

                            if datetime.datetime.strptime(previousRecord, "%H:%M:%S.%f") > datetime.datetime.strptime(str(recordTurnTime), "%H:%M:%S.%f"):
                                await ctx.channel.send('Pobiliście rekord i zgarniacie dodatkowe 1500 doświadczenia na discordzie!')
                                logChannel = self.bot.get_channel(881090112576962560)
                                await logChannel.send("<@291836779495948288>!   " + playerListString + " otrzymali: 1500 expa za rekord")
                                await functions_database.updateRecordTable(self, ctx, "Party " + str(playersList[0].name), recordTurnTime)

                            for hunter in playersList:

                                #Ranking - add points
                                await functions_database.updateRankingTable(self, ctx,
                                                                            hunter.id, BOSSRARITY, modifiers["points_boost"])

                                #Randomize Loot
                                dropLoot = await randLoot(self, ctx, BOSSRARITY, hunter, modifiers["drop_boost_perc"])

                                #Send info about loot
                                logChannel = self.bot.get_channel(881090112576962560)
                                await logChannel.send("<@291836779495948288>!   " + str(hunter) +
                                                     " otrzymał: " + dropLoot)

                            #Reset in the end of the fight
                            await resetEnd(self, ctx)
                            BOSSALIVE = 0

                            return BOSSALIVE, playersList
                        else:
                            print("Good command.")
                    else:
                        if chances > 0:
                            chances -= 1
                        else:
                            await ctx.channel.send('Pomyliłeś się! <:PepeHands:783992337377918986> Boss pojawi się później! <:RIP:912797982917816341>')
                            logChannel = self.bot.get_channel(881090112576962560)

                            if is_player_boss == False:
                                await  logChannel.send("<@291836779495948288>!   " + "Party " + str(playersList[0].name) + " pomyliło się i nie zabili bossa.")
                            else:
                                await  logChannel.send("<@291836779495948288>!   " + "Party " + str(playersList[0].name) + " pomyliło się i nie zabili bossa. Bossem był " + player_boss.name + " i należy mu się " + str((BOSSRARITY+1)*500) + " expa.")

                            #Reset in the end of the fight
                            await resetEnd(self, ctx)
                            BOSSALIVE = 0

                            if modifiers["ban_loser"] > 0:
                                for hunter in playersList:
                                    print("Hunter " + str(hunter.name) + " is dead.")
                                    await setDeadHunters(self, ctx, hunter.id)

                            return BOSSALIVE, playersList

                except asyncio.TimeoutError:
                    if chances > 0:
                        chances -= 1

                    else:
                        await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400> Boss pojawi się później! <:RIP:912797982917816341>')
                        logChannel = self.bot.get_channel(881090112576962560)
                        if is_player_boss == False:
                            await  logChannel.send("<@291836779495948288>!   " + "Party " + str(playersList[0].name) + " pomyliło się i nie zabili bossa.")
                        else:
                            await  logChannel.send("<@291836779495948288>!   " + "Party " + str(playersList[0].name) + " pomyliło się i nie zabili bossa. Bossem był " + player_boss.name + " i należy mu się " + str((BOSSRARITY+1)*500) + " expa.")

                        #Reset in the end of the fight
                        await resetEnd(self, ctx)
                        BOSSALIVE = 0

                        if modifiers["ban_loser"] > 0:
                            for hunter in playersList:
                                print("Hunter " + str(hunter.name) + " is dead.")
                                await setDeadHunters(self, ctx, hunter.id)

                        return BOSSALIVE, playersList
        else:
            return BOSSALIVE, playersList

    #function to add Boss Slayer role
    global setBossSlayer
    async def setBossSlayer(self, ctx, userID):
        my_role = discord.utils.get(ctx.guild.roles, id=983798433590673448)
        members = my_role.members
        print(my_role)
        if members:
            print(members)
            for member in members:
                 await member.remove_roles(my_role)
        print("Boss slayer role removed.")
        guild = self.bot.get_guild(686137998177206281)
        user = guild.get_member(int(userID))
        await user.add_roles(my_role)
        print("Boss slayer role granted.")

    #function to remove dead hunter role
    global resetDeadHunters
    async def resetDeadHunters(self, ctx):
        my_role = discord.utils.get(ctx.guild.roles, id=1091050836303544402)
        members = my_role.members
        print(my_role)
        if members:
            print(members)
            for member in members:
                 await member.remove_roles(my_role)
        print("Dead Hunter role removed.")

    #function to add Boss Slayer role
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

    #function to add random gif for Boss Slayer
    global flexGif
    async def flexGif(self, ctx):
        print("Random flex gif selecting.")
        gif = "flexgifs/" + random.choice(os.listdir("flexgifs/"))
        await ctx.channel.send(file=discord.File(gif))

    #function to change color of Boss Slayer Role
    global changeColor
    async def changeColor(self, ctx, hexColor):
        print("Changing color.")
        bossslayer_role = discord.utils.get(ctx.guild.roles, id=983798433590673448)
        print("Role taken.")
        print(bossslayer_role)
        hexColor = "0x" + hexColor
        print("Hex Color: " + hexColor)
        an_integer = int(hexColor, 16)
        print(an_integer)
        await bossslayer_role.edit(colour=an_integer, reason="Zmiana przez użytkownika.")

    #function to change icon of Boss Slayer Role
    global changeIcon
    async def changeIcon(self, ctx):
        print("Changing icon.")
        bossslayer_role = discord.utils.get(ctx.guild.roles, id=983798433590673448)
        print("Role taken.")
        print(bossslayer_role)
        print("1.")
        if ctx.message.attachments:
            if ctx.message.attachments[0].content_type == "image/png":
                image = await ctx.message.attachments[0].read()
                print("Image opened.")
                print(type(image))
                await ctx.bossslayer_role.edit(display_icon=image, reason="Zmiana przez użytkownika.")
                await ctx.send("Ikona została zmieniona Wielki Wojowniku. <:peepoglad:895774173887094874>")
            else:
                await ctx.send("<@" + str(ctx.author.id) + ">, zły format ikony. Poprawny format to .png. <:madge:882184635474386974>")
        else:
            await ctx.send("Dodaj ikonę w wiadomości z komendą - format .png <:madge:882184635474386974>")

    global resetEnd
    async def resetEnd(self, ctx):
        await functions_modifiers.init_modifiers(self, ctx)
        await functions_database.updateBossTable(self, ctx, 0, 0, False)
        await resetDeadHunters(self, ctx)
        print("Reset after boss... Modifiers, database and dead hunters reset.")

def setup(bot):
    bot.add_cog(functions_boss(bot))
