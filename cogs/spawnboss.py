from discord.errors import ClientException
from discord.ext import commands
from datetime import datetime

import discord
import asyncio
import random
import time
import datetime
import json

from discord.user import ClientUser
from discord import Client

import sys
sys.path.insert(1, './functions/')
import functions_general

class message(commands.Cog, name="spawnBoss"):
    def __init__(self, bot):
        self.bot = bot
    
    #define Spawn BIG Boss task
    async def spawn_task(self, ctx):
        while True:
            global bossAlive
            if bossAlive == 0:
               print("Preparing to channel clear. bossAlive = 0")
               bossAlive = 1
               await asyncio.sleep(60)
            if bossAlive == 1:
               bossAlive = 2
               await functions_general.fClear(self, ctx)
               print("Channel cleared. bossAlive = 1")
               await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
               #await asyncio.sleep(random.randint(30,31))  # time in second
               await asyncio.sleep(random.randint(150,3600)*24)  # time in second
            if bossAlive == 2:
               bossAlive = 3
               await functions_general.fClear(self, ctx)
               print("Channel cleared. bossAlive = 2")
               await ctx.channel.send('Wiatr wzmaga się coraz mocniej, z oddali słychać ryk, a ziemią targają coraz mocniejsze wstrząsy...')
               await asyncio.sleep(random.randint(3,10)*60)  # time in second
            if bossAlive == 3:
                bossAlive = 4
                await functions_general.fClear(self, ctx)
                print("Channel cleared.")
                print("Boss spawned. bossAlive = 3")
                imageName = "mobs/" + str(random.randint(1,4)) + ".gif"
                embed = discord.Embed(
                    title="💀 Boss! 💀",
                    description=f"Pojawił się potężny potwór! Zabij go natychmiast, żeby zgarnąć nagrody! Wpisz **#atak**, żeby rozpocząć walkę! ⚔️",
                    color=0xFFA500)
                await ctx.channel.send(file=discord.File(imageName))
                await ctx.send(embed=embed)
            else:
                await asyncio.sleep(30) #sleep for a while
    
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
        global bossAlive
        bossAlive = 0
        self.task.cancel()

    # command to attack the boss
    @commands.command(pass_context=True, name="atak", brief="Attacking the boss")
    async def attackMessage(self, ctx):
        global bossAlive
        if bossAlive == 4 or str(ctx.message.author.id) == '291836779495948288':
            author = discord.User.id
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
            

            #Random BossHP and go with for loop
            bossHP = random.randint(4,8) #random the number of turns
            print("Wylosowane HP bossa: " + str(bossHP))
            iterator = 0

            #define check function
            channel = ctx.channel
            def check(m):
                return m.channel == channel


            #define Loot
            def randLoot():
                with open("lootConfig.json", encoding='utf-8') as jsonFile:
                    jsonObject = json.loads(jsonFile.read())
                
                lootDescrList = []
                lootWeightList = []
                for loot in jsonObject['loot_details']:
                    lootDescrList.append(loot['descr'])
                    lootWeightList.append(loot['weight'])

                #print(lootDescrList)
                #print(lootWeightList)

                global dropLoot
                dropLoot = random.choices(lootDescrList, lootWeightList)
                
                #Embed create   
                embed=discord.Embed(title='Boss Drop', url='https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png', description='Boss wydropił:\n👉 ' + str(dropLoot[0]), color=0xfcdb03)
                embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png')
                embed.set_footer(text='Gratulacje!')
                return embed


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
                        cmdTimeout = 3
                    msg = await self.bot.wait_for('message', check=check, timeout=cmdTimeout)
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
                                chann = self.bot.get_channel(881090112576962560)
                                #print(str(chann.name))
                                await chann.send("<@291836779495948288>!   " + ctx.message.author.name + " otrzymał: 3000 expa za rekord")
                                with open('recordTime.txt', 'w') as f:
                                  f.write(str(recordTime) + "\n")
                                  f.write(str(format(ctx.message.author.name)))                          
                            
                            #Randomize Loot
                            await ctx.send(embed=randLoot())
                            #Send info about loot
                            chann = self.bot.get_channel(881090112576962560)
                            #print(str(chann.name))
                            await chann.send("<@291836779495948288>!   " + ctx.message.author.name + " otrzymał: " + str(dropLoot[0]))
                            
                        else:
                            await ctx.channel.send('Brawo! <:pepeOK:783992337406623754>')
                    else:
                        await ctx.channel.send('Pomyliłeś się! <:PepeHands:783992337377918986> Boss pojawi się później! <:RIP:912797982917816341>')
                        bossAlive = 0
                        break
                except asyncio.TimeoutError:
                    await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400> Boss pojawi się później! <:RIP:912797982917816341>')
                    bossAlive = 0
                    break
        else:
            print("Boss is not alive!")
            await ctx.channel.send('Boss nie żyje, poczekaj na jego respawn <@' + format(ctx.message.author.id) + '>!')


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
        await ctx.channel.send('Poprzednio boss walczył z **' + lastKillLines[1] + '** i było to **' + lastKillLines[0].rstrip('\n') + '**.')


                     

def setup(bot):
    bot.add_cog(message(bot))
