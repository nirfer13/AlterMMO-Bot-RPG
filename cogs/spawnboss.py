from logging import DEBUG
from discord import channel
from discord.errors import ClientException
from discord.ext import commands
from datetime import datetime, timedelta

import discord
import asyncio
import random
import time
import datetime
import json
from discord.message import Message

from discord.user import ClientUser
from discord import Client

import sys
from functions.functions_boss import fRandomBossHp
sys.path.insert(1, './functions/')
import functions_general
import functions_boss

#Debug Mode?
global DebugMode
DebugMode = False

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
        global ctx
        ctx = await functions_boss.getContext(self, 970571647226642442, 972797895340339272)
        global bossAlive, bossRarity, respTime, respawnResume
        respawnTime, bossRar, strRespawnResume = functions_boss.fReadRespawnFromFile()
        respawnResume = (strRespawnResume == "True")
        print("Resume?: " + str(respawnResume))
        if respawnResume == True:
            bossAlive = 1
            bossRarity = int(bossRar)
            respTime = int(respawnTime)
            print("Task resuming...")
            self.task = self.bot.loop.create_task(self.spawn_task(ctx))
            print("Task resumed.")
    
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
                    await asyncio.sleep(300)
               else:
                    await asyncio.sleep(10)
            #=== Episode 1 - Waiting
            #New Spawn
            if respawnResume == False:
                if bossAlive == 1:
                   bossAlive = 2
                   await functions_general.fClear(self, ctx)
                   print("Channel cleared. bossAlive = 1")
                   await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
                   if DebugMode == False:
                        respTime = random.randint(150,3600)*24
                        await asyncio.sleep(respTime)  # time in second
                   else:
                        respTime = random.randint(15,24)
                        await asyncio.sleep(respTime)  # time in second
                   #Save Resp to file
                   bossRarity = functions_boss.fBossRarity(respTime)
                   functions_boss.fSaveRespawnToFile(respTime, bossRarity, True)

                   
                   print("Resp time: " + str(respTime))
                   print("Boss Rarity: " + str(bossRarity))
            #Resume Spawn
            else:
                if bossAlive == 1:
                   bossAlive = 2
                   await functions_general.fClear(self, ctx)
                   print("Channel cleared. bossAlive = 1. Resuming.")
                   await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
                   if DebugMode == False:
                        await asyncio.sleep(respTime)  # time in second
                   else:
                        await asyncio.sleep(respTime)  # time in second
                   print("Resume resp time: " + str(respTime))
                   print("Resume boss Rarity: " + str(bossRarity))
               
            #=== Episode 2 - Before fight
            if bossAlive == 2:
               bossAlive = 3

               #Channel Clear
               await functions_general.fClear(self, ctx)
               print("Channel cleared. bossAlive = 2")
               
               await ctx.channel.send('Wiatr wzmaga się coraz mocniej, z oddali słychać ryk, a ziemią targają coraz mocniejsze wstrząsy... <:MonkaS:882181709100097587>')
               if DebugMode == False:
                    await asyncio.sleep(random.randint(3,10)*60)  # time in second
               else:
                    await asyncio.sleep(random.randint(3,10))  # time in second
            #=== Episode 3 - Boss respawn
            if bossAlive == 3:
                bossAlive = 4
                await functions_general.fClear(self, ctx)
                print("Channel cleared.")
                print("Boss appeared.")
                #Send boss image based on rarity
                await functions_boss.fBossImage(self, ctx, bossRarity)
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
        functions_boss.fSaveRespawnToFile(0, 0, False)
        self.task.cancel()

    # command to check Spawn Boss
    @commands.command(pass_context=True, name="checkSpawnBoss", brief="Checking boss spawn time")
    @commands.has_permissions(administrator=True)
    async def checkSpawnMessage(self, ctx):
        await ctx.channel.send("Resp time is " + str(respTime/60/60) + " hours.")

    # command to attack the boss
    @commands.command(pass_context=True, name="zaatakuj", brief="Attacking the boss")
    async def attackMessage(self, ctx):
        global bossAlive, bossRarity, respawnResume
        if bossAlive == 4 or str(ctx.message.author.id) == '291836779495948288':
            bossAlive = 5
            respawnResume = False
            author = discord.User.id

            #save user ID to not kill steal
            bossHunterID = ctx.message.author.id
            await ctx.message.add_reaction("⚔️")
            await ctx.channel.send('Zaatakowałeś bossa <@' + format(ctx.message.author.id) + '>! <:REEeee:790963160495947856> Wpisz pojawiające się komendy tak szybko, jak to możliwe!')

            #Start time counting
            startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
            #Save resp time and nickname
            chann = self.bot.get_channel(881090112576962560)
            with open('lastKillInfo.txt', 'w') as f:
                f.write(str(startTime) + "\n")
                f.write(str(format(ctx.message.author.name)))

            #Random the message and requested action
            requestedAction = [("unik", "atak", "paruj", "skok", "biegnij", "turlaj"), ("Boss szarżuje na Ciebie! Wpisz **UNIK**!!!", "Boss zawahał się! Teraz! Wpisz **ATAK**!!!", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **PARUJ**!!!", 
            "Boss próbuje ataku w nogi, wpisz **SKOK**!!!", "Boss szykuje potężny atak o szerokim zasięgu, wpisz **BIEGNIJ**!!!", "Boss atakuje w powietrzu, wpisz **TURLAJ**!!!")]
            
            bossHP = fRandomBossHp(bossRarity)
            print("Wylosowane HP bossa: " + str(bossHP))
            iterator = 0



            #Define check function
            channel = ctx.channel
            def check(ctx):
                def inner(msg):
                    return (msg.channel == channel) and (msg.author == ctx.author)
                return inner


            #Start whole fight
            for iterator in range(bossHP): #start boss turn
                iterator += 1

                choosenAction = random.randint(0,len(requestedAction[0])-1)
                #print("Wylosowany numer akcji: " + str(choosenAction))
                #print("Trzeba wpisac: " + requestedAction[0][choosenAction])

                try:
                    #Send proper action request on chat
                    await ctx.channel.send(str(iterator) + '. ' + requestedAction[1][choosenAction])

                    #Longer timeout for the first action
                    if iterator == 1:
                        cmdTimeout = 7
                    else:
                        #Timeout depends on boss rarity
                        cmdTimeout = 5 - bossRarity
                    msg = await self.bot.wait_for('message', check=check(ctx), timeout=cmdTimeout)
                    response = str(msg.content)

                    if response.lower() == requestedAction[0][choosenAction]:
                        #Boss killed?
                        if iterator >= bossHP:
                            bossAlive = 0
                            await ctx.channel.send('Brawo <@' + format(ctx.message.author.id) + '>! Pokonałeś bossa! <:POGGIES:790963160491753502><:POGGIES:790963160491753502><:POGGIES:790963160491753502>')
                            
                            #Time record
                            endTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                            recordTime = endTime - startTime
                            await ctx.channel.send('Zabicie bossa zajęło Ci: ' + str(recordTime).lstrip('0:00:') + ' sekundy!')
                            with open('recordTime.txt', 'r') as r:
                                previousRecord = r.readlines()
                                
                            if datetime.datetime.strptime(previousRecord[0].rstrip('\n'), "%H:%M:%S.%f") > datetime.datetime.strptime(str(recordTime), "%H:%M:%S.%f"):
                                await ctx.channel.send('Pobiłeś rekord i zgarniasz dodatkowe 3000 doświadczenia na discordzie!')
                                #print(str(chann.name))
                                logChannel = self.bot.get_channel(881090112576962560)
                                await logChannel.send("<@291836779495948288>!   " + ctx.message.author.name + " otrzymał: 3000 expa za rekord")
                                functions_boss.fSaveRespawnToFile(0, 0, False)
                                with open('recordTime.txt', 'w') as f:
                                    f.write(str(recordTime) + "\n")
                                    f.write(str(format(ctx.message.author.name)))                          
                            
                            #Randomize Loot
                            emb, dropLoot = functions_boss.randLoot(bossRarity)
                            await ctx.send(embed=emb) 
                            #Send info about loot
                            logChannel = self.bot.get_channel(881090112576962560)
                            await logChannel.send("<@291836779495948288>!   " + ctx.message.author.name + " otrzymał: " + str(dropLoot[0]))
                            
                        else:
                            await ctx.channel.send('Brawo! <:pepeOK:783992337406623754>')
                    else:
                        await ctx.channel.send('Pomyliłeś się! <:PepeHands:783992337377918986> Boss pojawi się później! <:RIP:912797982917816341>')
                        logChannel = self.bot.get_channel(881090112576962560)
                        await  logChannel.send("<@291836779495948288>!   " + ctx.message.author.name + " pomylił się i nie zabił bossa.")
                        functions_boss.fSaveRespawnToFile(0, 0, False)
                        bossAlive = 0
                        break
                        
                except asyncio.TimeoutError:
                    await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400> Boss pojawi się później! <:RIP:912797982917816341>')
                    logChannel = self.bot.get_channel(881090112576962560)
                    await  logChannel.send("<@291836779495948288>!   " + ctx.message.author.name + " nie zdążył wpisać komend i boss uciekł.")
                    functions_boss.fSaveRespawnToFile(0, 0, False)
                    bossAlive = 0
                    break
        else:
            print("Boss is not alive or attacked!")
            await ctx.channel.send('Nie możesz zaatakować bossa, poczekaj na pojawienie się kolejnego <@' + format(ctx.message.author.id) + '>!')


    # ==================================== COMMANDS FOR USERS =======================================================================

    # command to check boss kill record
    @commands.command(pass_context=True, name="rekord", brief="Check previous boss kill record")
    async def recordMessage(self, ctx):
        with open('recordTime.txt', 'r') as f:
            recordLines = f.readlines()
        await ctx.channel.send('Poprzedni rekord należy do **' + recordLines[1] + '** i wynosi **' + recordLines[0].rstrip('\n').lstrip('0:00:') + ' sekundy**.')

    # command to check last boss kill
    @commands.command(pass_context=True, name="kiedy", brief="Check previous boss kill time")
    async def lastKillInfoMessage(self, ctx):
        with open('lastKillInfo.txt', 'r') as f:
            lastKillLines = f.readlines()
        await ctx.channel.send('Poprzednio boss walczył z **' + lastKillLines[1] + '** i było to **' + lastKillLines[0].rstrip('\n') + '(UTC+2)**.')

    # ==================================== COMMANDS FOR DEBUG =======================================================================

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

                     

def setup(bot):
    bot.add_cog(message(bot))
