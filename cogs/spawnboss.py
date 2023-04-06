import asyncio
import json
import random
import sys
import time
from datetime import datetime, timedelta
from logging import DEBUG

import discord
from discord import Client, channel
from discord.errors import ClientException
from discord.ext import commands, tasks
from discord.message import Message
from discord.user import ClientUser
from numpy import busday_count

from functions.functions_boss import fRandomBossHp

sys.path.insert(1, './functions/')
import functions_boss
import functions_database
import functions_general
import functions_modifiers
import functions_pets
import functions_daily

#Import Globals
from globals.globalvariables import DebugMode

#Respawn time
global respTime
respTime = 0

#Boss alive
global BOSSALIVE
BOSSALIVE = 0

#Shrine alive
global SHRINEALIVE
SHRINEALIVE = 0

#Boss rarity
global BOSSRARITY
BOSSRARITY = 0

#Bot is busy
global BUSY
BUSY = 0

class message(commands.Cog, name="spawnBoss"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Every week ranking check and select the boss slayer."""

        #Choose channel to spawn boss
        global ctx
        if DebugMode is True:
            ctx = await functions_boss.getContext(self, 970571647226642442, 984860815842771024)
        else:
            ctx = await functions_boss.getContext(self, 970684202880204831, 1028328642436136961)

        global BOSSALIVE, BOSSRARITY, respTime, respawnResume
        bossRar, respawnTime, respawnResume = await functions_database.readBossTable(self, ctx)

        #Weekly ranking task create
        self.task2 = self.bot.loop.create_task(self.weekly_ranking(ctx))

        #Chest spawn task create
        self.task3 = self.bot.loop.create_task(self.spawn_modifiers_shrine(ctx))

        #Check if it is necessary to resume boss spawn
        print("Resume?: " + str(respawnResume))
        if respawnResume is True:
            BOSSALIVE = 1
            BOSSRARITY = int(bossRar)
            try:
                respTime = (respawnTime - (datetime.utcnow() + timedelta(hours=2))).total_seconds()
            except:
                respTime = 0
            print("Resp time: " + str(respTime))
            print("Task resuming...")
            self.task = self.bot.loop.create_task(self.spawn_task(ctx))
            print("Task resumed.")

    #define every week task
    # 7 days => 24 hour * 7 days = 168
    async def weekly_ranking(self, ctx):
        while True:
            timestamp = (datetime.utcnow() + timedelta(hours=2))
            if timestamp.strftime("%H:%M UTC %a") == "15:00 UTC Mon":
                print('Weekly ranking summary!')
                winnerID = await functions_database.readSummaryRankingTable(self, ctx)
                print("Winner ID: " + str(winnerID))
                await functions_boss.setBossSlayer(self, ctx, winnerID)
                await functions_database.resetRankingTable(self)
                await ctx.channel.send("<@&985071779787730944>! Ranking za tydzień polowań został zresetowany. Nowa rola <@&983798433590673448> została przydzielona <@" + str(winnerID) + ">! Gratulacje <:GigaChad:970665721321381958>")
                return
            # wait some time before another loop. Don't make it more than 60 sec or it will skip
            await asyncio.sleep(30)

    async def spawn_modifiers_shrine(self, ctx):
        """Spawns a shrine when boss is not alive. Shrine drops different modifiers."""

        print("Spawning shrine task starting...")
        global SHRINEALIVE, BOSSALIVE
        SHRINEALIVE = 0

        while True:
            if DebugMode is False:
                resp_time = random.randint(2700, 7200)
            elif DebugMode is True:
                resp_time = random.randint(30, 55)

            await asyncio.sleep(resp_time)

            if SHRINEALIVE == 0 and (BOSSALIVE == 0 or BOSSALIVE == 1 or BOSSALIVE == 2):
                await functions_modifiers.spawn_modifier_shrine(self, ctx)
                SHRINEALIVE = 1
            elif BOSSALIVE > 2:
                print("Boss spawned. Skip.")
            elif SHRINEALIVE == 1 and (BOSSALIVE == 0 or BOSSALIVE == 1 or BOSSALIVE == 2):
                print("Shrine already spawned. Spawn again.")
                await functions_modifiers.spawn_modifier_shrine(self, ctx)
            else:
                print("Unknow state of shrine.")

    #define Spawn BIG Boss task
    async def spawn_task(self, ctx):
        while True:
            global respTime
            global BOSSALIVE
            global BOSSRARITY
            global SHRINEALIVE
            global respawnResume
            #=== Episode 0
            if BOSSALIVE == 0:
                print("Preparing to channel clear. BOSSALIVE = 0")
                BOSSALIVE = 1
                if DebugMode is False:
                    respTime = random.randint(150,3600)*12
                    #Save Resp to file
                    BOSSRARITY = functions_boss.fBOSSRARITY(respTime)
                    await functions_database.updateBossTable(self, ctx, BOSSRARITY, respTime, True)

                    print("Resp time: " + str(respTime))
                    print("Boss Rarity: " + str(BOSSRARITY))
                    await asyncio.sleep(3600)
                else:
                    respTime = random.randint(15,24)
                    #Save Resp to database
                    BOSSRARITY = functions_boss.fBOSSRARITY(respTime)
                    print("Updating database...")
                    await functions_database.updateBossTable(self, ctx, BOSSRARITY, respTime, True)

                    print("Resp time: " + str(respTime))
                    print("Boss Rarity: " + str(BOSSRARITY))
                    await asyncio.sleep(3)
            #=== Episode 1 - Waiting
            #New Spawn
            if respawnResume is False:
                if BOSSALIVE == 1:
                    BOSSALIVE = 2
                    await functions_general.fClear(self, ctx)
                    print("Channel cleared. BOSSALIVE = 1")
                    async with ctx.typing():
                        await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
                    if DebugMode is False:
                        await asyncio.sleep(respTime)  # time in second
                    else:
                        await asyncio.sleep(respTime)  # time in second

            #Resume Spawn
            else:
                if BOSSALIVE == 1:
                    BOSSALIVE = 2
                    await functions_general.fClear(self, ctx)
                    print("Channel cleared. BOSSALIVE = 1. Resuming.")
                    print("Resume resp time: " + str(respTime))
                    print("Resume boss Rarity: " + str(BOSSRARITY))
                    async with ctx.typing():
                        await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
                    if DebugMode is False:
                        await asyncio.sleep(respTime)  # time in second
                    else:
                        await asyncio.sleep(respTime)  # time in second

            #=== Episode 2 - Before fight
            if BOSSALIVE == 2:
                BOSSALIVE = 3

                #Channel Clear
                print("Channel cleared. BOSSALIVE = 2")
                async with ctx.typing():
                    await ctx.channel.send('Wiatr wzmaga się coraz mocniej, z oddali słychać ryk, a ziemią targają coraz mocniejsze wstrząsy... <:MonkaS:882181709100097587>')
                if DebugMode is False:
                    await asyncio.sleep(random.randint(60,120)*5)  # time in second
                else:
                    await asyncio.sleep(random.randint(3,10))  # time in second

            #=== Episode 3 - Boss respawn
            if BOSSALIVE == 3:
                print("Channel cleared.")
                SHRINEALIVE = 0
                await functions_general.fClear(self, ctx)
                print("Boss appeared.")
                #Send info about boss spawn

                modifiers = await functions_modifiers.load_modifiers(self, ctx)
                BOSSRARITY += modifiers["rarity_boost"]
                if BOSSRARITY > 3:
                    BOSSRARITY = 3

                try:
                    await generalSpawnMessage.delete()
                    print("Message deleted.")
                except:
                    print("No general message to delete.")
                if DebugMode is False:
                    chatChannel = self.bot.get_channel(696932659833733131)
                    generalSpawnMessage = await chatChannel.send("Na kanale <#970684202880204831> pojawił się właśnie potwór! Zabijcie go, żeby zgarnąć nagrody!")
                else:
                    chatChannel = self.bot.get_channel(881090112576962560)
                    generalSpawnMessage = await chatChannel.send("Na kanale <#970684202880204831> pojawił się właśnie potwór! Zabijcie go, żeby zgarnąć nagrody!")
                #Send boss image based on rarity
                global initCommand, is_player_boss, player_boss
                initCommand = "zaatakuj"
                is_player_boss, player_boss = await functions_boss.fBossImage(self, ctx, BOSSRARITY)
                BOSSALIVE = 4
            else:
                await asyncio.sleep(5) #sleep for a while

    #pray to the shrine command
    @commands.command(name="modlitwa", brief="Patron or Creator can pray to the shrine.")
    async def ShrinePray(self, ctx):
        print("Praying...")
        global SHRINEALIVE

        if SHRINEALIVE == 1:
            SHRINEALIVE = 0
            crafter = discord.utils.get(ctx.guild.roles, id=687185998550925312)
            if crafter in ctx.message.author.roles:
                await ctx.message.add_reaction("<:prayge:1063891597760139304>")
                await functions_modifiers.random_modifiers(self, ctx)
            else:
                await ctx.channel.send("Nie umiesz pacierza. Poczekaj na kogoś bardziej wierzącego. <:prayge:1063891597760139304>")
                SHRINEALIVE = 1
        else:
            await ctx.channel.send("Do kogo Ty chcesz się modlić? Przecież tu nic nie ma...")
       

    #create Spawn Boss task command
    @commands.command(name="startSpawnBoss", brief="Starts spawning boss")
    @commands.has_permissions(administrator=True)
    async def startMessage(self, ctx):
        print("Spawning started!")
        global BOSSALIVE
        BOSSALIVE = 0
        self.task = self.bot.loop.create_task(self.spawn_task(ctx))

    # command to stop Spawn Boss task
    @commands.command(pass_context=True, name="stopSpawnBoss", brief="Stops spawning boss")
    @commands.has_permissions(administrator=True)
    async def stopMessage(self, ctx):
        print("Spawning stopped!")
        global BOSSALIVE, respawnResume
        respawnResume = False
        BOSSALIVE = 0
        await functions_database.updateBossTable(self, ctx, 0, 0, False)
        self.task.cancel()

    # command to check Spawn Boss
    @commands.command(pass_context=True, name="checkSpawnBoss", brief="Checking boss spawn time")
    @commands.has_permissions(administrator=True)
    async def checkSpawnMessage(self, ctx):
        await ctx.channel.send("Resp time is " + str(respTime/60/60) + " hours.")

    # command to attack the boss - rarity 0, 1, 2
    @commands.command(pass_context=True, name="zaatakuj", brief="Attacking the boss")
    async def attackMessage(self, ctx):
        global BOSSALIVE, BOSSRARITY, respawnResume

        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:

            if BOSSALIVE == 4: #or str(ctx.message.author.id) == '291836779495948288':
                BOSSALIVE = 5

                if BOSSRARITY in [0,1,2]:
                    BOSSALIVE, bossHunterID = await functions_boss.singleInit(self, ctx,
                                                                              BOSSALIVE, BOSSRARITY)
                    BOSSALIVE = await functions_boss.singleFight(self, ctx, BOSSALIVE,
                                                                 bossHunterID, BOSSRARITY,
                                                                 is_player_boss, player_boss)
                elif BOSSRARITY == 3:
                    BOSSALIVE, playersList = await functions_boss.groupInit(self, ctx,
                                                                            BOSSALIVE, BOSSRARITY)
                    print(BOSSALIVE)
                    BOSSALIVE, playersList = await functions_boss.groupFight(self, ctx,
                                                                             BOSSALIVE, playersList,
                                                                             is_player_boss,
                                                                             player_boss)

            elif BOSSALIVE == 5:
                pass
            else:
                #Boss not Alive
                print("Boss is not alive or attacked!")
                await ctx.channel.send('Nie możesz zaatakować bossa, poczekaj na pojawienie się kolejnego <@' + format(ctx.message.author.id) + '>!')
        else:
            pass #wrong channel


    # ==================================== COMMANDS FOR USERS =======================================================================

    # command to check boss kill record
    @commands.command(name="rekord")
    async def rekord(self, ctx):
        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:
            recordTime, Nick = await functions_database.readRecordTable(self, ctx)
            print ("Record database read.")
            await ctx.channel.send('Poprzedni rekord należy do **' + Nick + '** i wynosi średnio **' + recordTime.lstrip('00:') + ' sekundy na turę walki**.')

    # command to check last boss kill
    @commands.command(pass_context=True, name="kiedy", brief="Check previous boss kill time")
    async def lastKillInfoMessage(self, ctx):
        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:
            fightTime, Nick = await functions_database.readHistoryTable(self, ctx)
            print ("History database read.")
            await ctx.channel.send('Poprzednio boss walczył z **' + Nick + '** i było to **' + fightTime[:16] + ' UTC+1**.')

    @commands.command(name="ranking")
    async def readRankingDatabase(self, ctx):
        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:
            await functions_database.readRankingTable(self, ctx)
            print ("Ranking database read.")

    @commands.command(name="towarzysz", brief="Shows author's pet.")
    async def show_pet(self, ctx):
        await functions_pets.show_pet(self, ctx)

    @commands.command(name="porzucam", brief="Discards author's pet.")
    async def discard_pet(self, ctx):
        print("Discarding author's pet")
        await functions_pets.discard_pet(self, ctx)

    @commands.command(name="polowanie", brief="Try to hunt on a mobs.")
    async def hunting(self, ctx):
        global BOSSALIVE, BUSY, DebugMode
        if (BOSSALIVE in [0,1,2] or DebugMode is True) and BUSY == 0:
            BUSY = 1
            rarity = random.randint(0,100)
            if 0 <= rarity < 70:
                rarity = 0
            elif 70 <= rarity <= 98:
                rarity = 1
            else:
                rarity = 2
            is_player_boss, boss_player = await functions_daily.fBossImage(self, ctx, rarity)
            await functions_daily.hunt_mobs(self, ctx, rarity, is_player_boss, boss_player)
            BUSY = 0
        elif BOSSALIVE == 3:
            await ctx.channel.send("Zaraz pojawi się prawidzwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, to wpisz **$zaatakuj**, żeby stawić mu czoła.")
        elif BOSSALIVE == 4:
            await ctx.channel.send("Teraz pora na walkę z prawdziwym bossem, nie mieszaj się leszczyku <:madge:882184635474386974>")
        elif BOSSALIVE > 4:
            pass
        elif BUSY == 1:
            pass

    # command to flex boss slayer
    @commands.command(pass_context=True, name="flex", brief="Boss slayer flex")
    @commands.cooldown(1, 1800, commands.BucketType.user)
    async def flex(self, ctx):
        my_role = discord.utils.get(ctx.guild.roles, id=983798433590673448)
        print(str(type(ctx.message.author)))
        if my_role in ctx.message.author.roles:
            await functions_boss.flexGif(self, ctx)
            await ctx.channel.send('Potężny <:GigaChad:970665721321381958> <@' + format(ctx.message.author.id) + '> napina swe sprężyste, naoliwione muskuły! Co za widok, robi wrażenie! <:pogu:882182966372106280>')
        else:
            await ctx.channel.send('<:KEKW:936907435921252363> **Miernota** <:2Head:882184634572627978>')

    @flex.error
    async def flexcommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')

    # command to change color
    @commands.command(pass_context=True, name="kolor", brief="Boss slayer color change")
    @commands.cooldown(1, 1800, commands.BucketType.user)
    async def kolor(self, ctx, hexColor):
        my_role = discord.utils.get(ctx.guild.roles, id=983798433590673448)
        print(str(type(ctx.message.author)))
        if my_role in ctx.message.author.roles:
            print("Before function to change color.")
            await functions_boss.changeColor(self, ctx, hexColor)
        else:
            await ctx.channel.send('<:KEKW:936907435921252363> **Kpisz sobie, miernoto?** <:2Head:882184634572627978>')

    @kolor.error
    async def flexcommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')

    # command to change icon
    @commands.command(pass_context=True, name="ikona", brief="Boss slayer icon change")
    @commands.cooldown(1, 1800, commands.BucketType.user)
    async def changeIcon(self, ctx):
        print("Before function to change color.")
        await functions_boss.changeIcon(self, ctx)

    @changeIcon.error
    async def flexcommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')

    # ==================================== COMMANDS FOR DEBUG ======================================

    # command to debug
    @commands.command(pass_context=True, name="time")
    @commands.has_permissions(administrator=True)
    async def checkTime(self, ctx):
        timestamp = (datetime.utcnow() + timedelta(hours=2))
        await ctx.send(str(timestamp))

    # command to debug
    @commands.command(pass_context=True, name="loot")
    @commands.has_permissions(administrator=True)
    async def randLoot(self, ctx, srarity, BossHunter, boost_percent):
        await functions_boss.randLoot(self, ctx, srarity, BossHunter, boost_percent)

    # command to debug
    @commands.command(pass_context=True, name="bossslayer")
    @commands.has_permissions(administrator=True)
    async def bossSlayer(self, ctx, userID):
        await functions_boss.setBossSlayer(self, ctx, userID)

    # command to debug
    @commands.command(pass_context=True, name="remind")
    @commands.has_permissions(administrator=True)
    async def remind(self, ctx):
        Channel = self.bot.get_channel(970684202880204831)
        await Channel.send("Potwór oczekuje na zabicie! Wpisz **$zaatakuj**, aby rozpocząć walkę! @here")

    # command to debug
    @commands.command(pass_context=True, name="rarity")
    @commands.has_permissions(administrator=True)
    async def BOSSRARITY(self, ctx, time):
        await ctx.channel.send(str(functions_boss.fBOSSRARITY(time)))

    # command to debug
    @commands.command(pass_context=True, name="image")
    @commands.has_permissions(administrator=True)
    async def bossImage(self, ctx, rarity):
        await functions_boss.fBossImage(self, ctx, rarity)

    # command to debug
    @commands.command(pass_context=True, name="hp")
    @commands.has_permissions(administrator=True)
    async def bossHp(self, ctx, rarity):
        await ctx.channel.send(str(fRandomBossHp(rarity)))

    # command to debug
    @commands.command(pass_context=True, name="respToFile")
    @commands.has_permissions(administrator=True)
    async def respToFile(self, ctx, respawnTime, BOSSRARITY, respawnStarted):
        functions_boss.fSaveRespawnToFile(respawnTime, BOSSRARITY, respawnStarted)
        await ctx.channel.send("File Saved")

    # command to debug
    @commands.command(pass_context=True, name="ModifiersInit",
                      brief="Creates new file with 0 value of modifiers.")
    @commands.has_permissions(administrator=True)
    async def initModifiers(self, ctx):
        await functions_modifiers.init_modifiers(self, ctx)
        await ctx.channel.send("Modifiers file initialized.")

    # command to debug
    @commands.command(pass_context=True, name="ModifiersLoad",
                      brief="Load modifiers and prints values.")
    @commands.has_permissions(administrator=True)
    async def loadModifiers(self, ctx):
        await functions_modifiers.load_modifiers(self, ctx)
        await ctx.channel.send("Modifiers file loaded.")

    # command to debug
    @commands.command(pass_context=True, name="ModifiersLoadDesc",
                      brief="Load modifiers and prepare string for them.")
    @commands.has_permissions(administrator=True)
    async def loadModifiersDesc(self, ctx):
        await functions_modifiers.load_desc_modifiers(self, ctx)
        await ctx.channel.send("Modifiers string loaded.")

    # command to debug
    @commands.command(pass_context=True, name="ModifiersModify",
                      brief="modifier_name value - set value of the modifier.")
    @commands.has_permissions(administrator=True)
    async def modifyModifiers(self, ctx, modifier_name, value):
        await functions_modifiers.modify_modifiers(self, ctx, modifier_name, value)
        await ctx.channel.send("Modifiers file modified.")

    # command to debug
    @commands.command(pass_context=True, name="ModifiersRandom",
                      brief="Select random modifier.")
    @commands.has_permissions(administrator=True)
    async def randomModifiers(self, ctx):
        await functions_modifiers.random_modifiers(self,)

    # command to debug
    @commands.command(pass_context=True, name="GenerateEgg",
                      brief="Generates egg and print its data.")
    @commands.has_permissions(administrator=True)
    async def generate_egg(self, ctx):
        await functions_pets.generate_pet_egg(self, ctx)

    # command to debug
    @commands.command(pass_context=True, name="spawn")
    @commands.has_permissions(administrator=True)
    async def spawn(self, ctx):
        await functions_boss.fCreateSpawn(self)
        await ctx.channel.send("Spawn created")

    # command to debug
    @commands.command(name="context")
    @commands.has_permissions(administrator=True)
    async def context(self, ctx):
        await functions_boss.getContext(self)

    # ==================================== COMMANDS FOR DATABASE ===================================

    @commands.command(name="updateDatabase")
    @commands.has_permissions(administrator=True)
    async def updateDatabase(self, ctx, BOSSRARITY, respTime, respBool):
        await functions_database.updateBossTable(self, ctx, BOSSRARITY, respTime, respBool)
        await ctx.channel.send("Database updated.")

    @commands.command(name="readDatabase")
    @commands.has_permissions(administrator=True)
    async def readDatabase(self, ctx):
        bossRar, respawnTime, respawnResume = await functions_database.readBossTable(self, ctx)
        print ("Database read.")
        await ctx.channel.send("Czy boss będzie wskrzeszony?: " + str(respawnResume))
        await ctx.channel.send("Boss rarity: " + str(bossRar))
        await ctx.channel.send("Czas wskrzeszenia: " + str(respawnTime))

    @commands.command(name="createAllDatabases")
    @commands.has_permissions(administrator=True)
    async def createAllDatabases(self, ctx):
        await functions_database.createBossTable(self)
        await functions_database.createRecordTable(self)
        await functions_database.createHistoryTable(self)
        await ctx.channel.send("Wszystkie bazy danych utworzone!")

    @commands.command(name="createBossDatabase")
    @commands.has_permissions(administrator=True)
    async def createBossDatabase(self, ctx):
        await functions_database.createBossTable(self)
        await ctx.channel.send("Baza danych utworzona.")

    @commands.command(name="createPetOwnersDatabase",
                      brief="Create table PETOWNER table with one default record.")
    @commands.has_permissions(administrator=True)
    async def create_pet_owners_table(self, ctx):
        await functions_pets.create_pet_owners_table(self)
        await ctx.channel.send("Baza danych PETOWNERS utworzona.")

    @commands.command(name="createPetsDatabase",
                      brief="Create table PETS table with one default record.")
    @commands.has_permissions(administrator=True)
    async def create_pets_table(self, ctx):
        await functions_pets.create_pets_table(self)
        await ctx.channel.send("Baza danych PETS utworzona.")

    @commands.command(name="ReassignPet",
                      brief="Assign pet_id to player_id.")
    @commands.has_permissions(administrator=True)
    async def reassign_pet(self, ctx, pet_id, player_id):
        await functions_pets.reassing_pet(self, pet_id, player_id)
        await ctx.channel.send(f"Pet {pet_id} przypisany do gracza <@{player_id}>.")

    # ====== Record Database Commands to Debug

    @commands.command(name="createRecordDatabase")
    @commands.has_permissions(administrator=True)
    async def createDatabase(self, ctx):
        await functions_database.createRecordTable(self)
        await ctx.channel.send("Baza danych z rekordem utworzona.")

    @commands.command(name="updateRecordDatabase")
    @commands.has_permissions(administrator=True)
    async def updateRecordDatabase(self, ctx, Nick, recordTime_MM_SS_MS):
        await functions_database.updateRecordTable(self, ctx, Nick, recordTime_MM_SS_MS)
        await ctx.channel.send("Baza danych z rekordem zaktualizowana.")

    # ====== History Database Commands to Debug

    @commands.command(name="createHistoryDatabase")
    @commands.has_permissions(administrator=True)
    async def createHistoryDatabase(self, ctx):
        await functions_database.createHistoryTable(self)
        await ctx.channel.send("Baza danych z historią utworzona.")

    @commands.command(name="updateHistoryDatabase")
    @commands.has_permissions(administrator=True)
    async def updateHistoryDatabase(self, ctx, Nick, fightTime):
        await functions_database.updateHistoryTable(self, ctx, Nick, fightTime)
        await ctx.channel.send("Baza danych z historią zaktualizowana.")

    # ====== Ranking Database Commands to Debug

    @commands.command(name="createRankingDatabase")
    @commands.has_permissions(administrator=True)
    async def createRankingDatabase(self, ctx):
        await functions_database.createRankingTable(self)
        await ctx.channel.send("Baza danych z rankingiem utworzona.")

    @commands.command(name="resetRankingDatabase")
    @commands.has_permissions(administrator=True)
    async def resetRankingDatabase(self, ctx):
        await functions_database.resetRankingTable(self)
        await ctx.channel.send("Baza danych z rankingiem zresetowana.")

    @commands.command(name="updateRankingDatabase")
    @commands.has_permissions(administrator=True)
    async def updateRankingDatabase(self, ctx, ID, points):
        print("Starting command...")
        await functions_database.updateRankingTable(self, ctx, ID, points)

def setup(bot):
    bot.add_cog(message(bot))
