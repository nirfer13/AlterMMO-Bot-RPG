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

from functions.functions_boss import fRandomBossHp

sys.path.insert(1, './functions/')
import functions_boss
import functions_database
import functions_general
import functions_modifiers

#Import Globals
from globals.globalvariables import DebugMode

#Respawn time
global respTime
respTime = 0

#Boss alive
global bossAlive
bossAlive = 0

#Boss rarity
global bossRarity
bossRarity = 0

class message(commands.Cog, name="spawnBoss"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        #Every week ranking check

        #Choose channel to spawn boss
        global ctx
        if DebugMode == True:
            ctx = await functions_boss.getContext(self, 970571647226642442, 984860815842771024)
        else:
            ctx = await functions_boss.getContext(self, 970684202880204831, 1028328642436136961)

        global bossAlive, bossRarity, respTime, respawnResume
        bossRar, respawnTime, respawnResume = await functions_database.readBossTable(self, ctx)

        #Weekly ranking task create
        self.task2 = self.bot.loop.create_task(self.msg1(ctx))

        #Check if it is necessary to resume boss spawn
        print("Resume?: " + str(respawnResume))
        if respawnResume == True:
            bossAlive = 1
            bossRarity = int(bossRar)
            try:
                respTime = (respawnTime - (datetime.utcnow() + timedelta(hours=1))).total_seconds()
            except:
                respTime = 0
            print("Resp time: " + str(respTime))
            print("Task resuming...")
            self.task = self.bot.loop.create_task(self.spawn_task(ctx))
            print("Task resumed.")

    #define every week task
    # 7 days => 24 hour * 7 days = 168
    async def msg1(self, ctx):
        while True:
            timestamp = (datetime.utcnow() + timedelta(hours=1))
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
    
    #define Spawn BIG Boss task
    async def spawn_task(self, ctx):
        while True:
            global respTime
            global bossAlive
            global bossRarity
            global respawnResume
            #=== Episode 0
            if bossAlive == 0:
                print("Preparing to channel clear. bossAlive = 0")
                bossAlive = 1
                if DebugMode == False:
                    respTime = random.randint(150,3600)*12
                    #Save Resp to file
                    bossRarity = functions_boss.fBossRarity(respTime)
                    await functions_database.updateBossTable(self, ctx, bossRarity, respTime, True)

                    print("Resp time: " + str(respTime))
                    print("Boss Rarity: " + str(bossRarity))
                    await asyncio.sleep(3600)
                else:
                    respTime = random.randint(15,24)
                    #Save Resp to database
                    bossRarity = functions_boss.fBossRarity(respTime)
                    print("Updating database...")
                    await functions_database.updateBossTable(self, ctx, bossRarity, respTime, True)

                    print("Resp time: " + str(respTime))
                    print("Boss Rarity: " + str(bossRarity))
                    await asyncio.sleep(3)
            #=== Episode 1 - Waiting
            #New Spawn
            if respawnResume == False:
                if bossAlive == 1:
                    bossAlive = 2
                    await functions_general.fClear(self, ctx)
                    print("Channel cleared. bossAlive = 1")
                    async with ctx.typing():
                        await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
                    if DebugMode == False:
                        await asyncio.sleep(respTime)  # time in second
                    else:
                        await asyncio.sleep(respTime)  # time in second

            #Resume Spawn
            else:
                if bossAlive == 1:
                    bossAlive = 2
                    await functions_general.fClear(self, ctx)
                    print("Channel cleared. bossAlive = 1. Resuming.")
                    print("Resume resp time: " + str(respTime))
                    print("Resume boss Rarity: " + str(bossRarity))
                    async with ctx.typing():
                        await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
                    if DebugMode == False:
                        await asyncio.sleep(respTime)  # time in second
                    else:
                        await asyncio.sleep(respTime)  # time in second
                   
            #=== Episode 2 - Before fight
            if bossAlive == 2:
                bossAlive = 3

                #Channel Clear
                print("Channel cleared. bossAlive = 2")
                async with ctx.typing():
                    await ctx.channel.send('Wiatr wzmaga się coraz mocniej, z oddali słychać ryk, a ziemią targają coraz mocniejsze wstrząsy... <:MonkaS:882181709100097587>')
                if DebugMode == False:
                    await asyncio.sleep(random.randint(60,120)*5)  # time in second
                else:
                    await asyncio.sleep(random.randint(3,10))  # time in second

            #=== Episode 3 - Boss respawn
            if bossAlive == 3:
                print("Channel cleared.")
                await functions_general.fClear(self, ctx)
                print("Boss appeared.")
                #Send info about boss spawn

                try:
                    await generalSpawnMessage.delete()
                    print("Message deleted.")
                except:
                    print("No general message to delete.")
                if DebugMode == False:
                    chatChannel = self.bot.get_channel(696932659833733131)
                    generalSpawnMessage = await chatChannel.send("Na kanale <#970684202880204831> pojawił się właśnie potwór! Zabijcie go, żeby zgarnąć nagrody!")
                else:
                    chatChannel = self.bot.get_channel(881090112576962560)
                    generalSpawnMessage = await chatChannel.send("Na kanale <#970684202880204831> pojawił się właśnie potwór! Zabijcie go, żeby zgarnąć nagrody!")
                #Send boss image based on rarity
                global initCommand, is_player_boss, player_boss
                initCommand = "zaatakuj"
                is_player_boss, player_boss = await functions_boss.fBossImage(self, ctx, bossRarity)
                bossAlive = 4
            else:
                await asyncio.sleep(5) #sleep for a while
  
    #create Spawn Boss task command
    @commands.command(name="startSpawnBoss", brief="Starts spawning boss")
    @commands.has_permissions(administrator=True)
    async def startMessage(self, ctx):
        print("Spawning started!")
        global bossAlive
        bossAlive = 0
        self.task = self.bot.loop.create_task(self.spawn_task(ctx))

    # command to stop Spawn Boss task
    @commands.command(pass_context=True, name="stopSpawnBoss", brief="Stops spawning boss")
    @commands.has_permissions(administrator=True)
    async def stopMessage(self, ctx):
        print("Spawning stopped!")
        global bossAlive, respawnResume
        respawnResume = False
        bossAlive = 0
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
        global bossAlive, bossRarity, respawnResume

        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:

            if bossAlive == 4: #or str(ctx.message.author.id) == '291836779495948288':
                bossAlive = 5

                if bossRarity in [0,1,2]:
                    bossAlive, bossHunterID = await functions_boss.singleInit(self, ctx, bossAlive, bossRarity)
                    bossAlive = await functions_boss.singleFight(self, ctx, bossAlive, bossHunterID, bossRarity, is_player_boss, player_boss) 
                elif bossRarity == 3:
                    bossAlive, playersList = await functions_boss.groupInit(self, ctx, bossAlive, bossRarity)
                    print(bossAlive)
                    bossAlive, playersList = await functions_boss.groupFight(self, ctx, bossAlive, playersList, is_player_boss, player_boss)

            elif bossAlive == 5:
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
        timestamp = (datetime.utcnow() + timedelta(hours=1))
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
    async def bossRarity(self, ctx, time):
        await ctx.channel.send(str(functions_boss.fBossRarity(time)))

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
    async def respToFile(self, ctx, respawnTime, bossRarity, respawnStarted):
        functions_boss.fSaveRespawnToFile(respawnTime, bossRarity, respawnStarted)
        await ctx.channel.send("File Saved")

    # command to debug
    @commands.command(pass_context=True, name="initModifiers")
    @commands.has_permissions(administrator=True)
    async def initModifiers(self, ctx):
        await functions_modifiers.init_modifiers(self, ctx)
        await ctx.channel.send("Modifiers file initialized.")

    # command to debug
    @commands.command(pass_context=True, name="$m")
    @commands.has_permissions(administrator=True)
    async def initModifiers(self, ctx):
        await functions_modifiers.load_modifiers(self, ctx)
        await ctx.channel.send("Modifiers file loaded.")

    # command to debug
    @commands.command(pass_context=True, name="modifyModifiers")
    @commands.has_permissions(administrator=True)
    async def modifyModifiers(self, ctx, modifier_name, value):
        await functions_modifiers.modify_modifiers(self, ctx, modifier_name, value)
        await ctx.channel.send("Modifiers file modified.")

    # command to debug
    @commands.command(pass_context=True, name="respFromFile")
    @commands.has_permissions(administrator=True)
    async def respFromFile(self, ctx):
        respawnTime, bossRarity, respawnStarted = functions_boss.fReadRespawnFromFile()
        await ctx.channel.send("Read from file - seconds to spawn: " + str(respawnTime) + ". Boss rarity: " + str(bossRarity) + ". Respawn started?: " + str(respawnStarted))

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
    async def updateDatabase(self, ctx, bossRarity, respTime, respBool):
        await functions_database.updateBossTable(self, ctx, bossRarity, respTime, respBool)
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
