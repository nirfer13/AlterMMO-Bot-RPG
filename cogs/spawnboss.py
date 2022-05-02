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

class message(commands.Cog, name="spawnBoss"):
    def __init__(self, bot):
        self.bot = bot
        bossAlive = False        
    
    #define Spawn Boss task
    async def spawn_task(self, ctx):
        while True:
            global bossAlive
            if bossAlive == False:
                await asyncio.sleep(random.randint(1,7))  # *60 time is expected in minutes
                bossAlive = True
                print("Boss spawned.")
                imageName = "mobs/" + str(random.randint(1,4)) + ".gif"
                embed = discord.Embed(
                    title="💀 Boss! 💀",
                    description=f"Pojawił się potężny potwór! Zabij go natychmiast, żeby zgarnąć nagrody! Wpisz **#atak**, żeby rozpocząć walkę! ⚔️",
                    color=0xFFA500)
                await ctx.channel.send(file=discord.File(imageName))
                await ctx.send(embed=embed)
            else:
                await asyncio.sleep(5) #sleep for a while
                
                
            
    #create Spawn Boss task command
    @commands.command(name="startSpawnBoss", brief="Starts spawning boss")
    @commands.has_permissions(administrator=True)
    async def startMessage(self, ctx):
        print("Spawning started!")
        global bossAlive
        bossAlive = False
        self.task = self.bot.loop.create_task(self.spawn_task(ctx))
        

    # command to stop periodic message
    @commands.command(pass_context=True, name="stopSpawnBoss", brief="Stops spawning boss")
    @commands.has_permissions(administrator=True)
    async def stopMessage(self, ctx):
        print("Spawning stopped!")
        global bossAlive
        bossAlive = False
        self.task.cancel()

    # command to kill the boss
    """@commands.command(pass_context=True, name="killBoss", brief="Kill the boss")
    @commands.has_permissions(administrator=True)
    async def killMessage(self, ctx):
        global bossAlive
        if bossAlive == True:
            print("Boss killed!")
            bossAlive = False
        else:
            print("Boss is not alive!")"""

    #define Private Info about Event

    @commands.command(pass_context=True, name="infoCheck", brief="Checking info command")
    @commands.has_permissions(administrator=True)
    async def infoMessage(self, ctx):
        funcInfo("Test123")


    # command to attack the boss
    @commands.command(pass_context=True, name="atak", brief="Attacking the boss")
    async def attackMessage(self, ctx):
        global bossAlive
        if bossAlive == True:
            author = discord.User.id
            await ctx.message.add_reaction("⚔️")
            await ctx.channel.send('Zaatakowałeś bossa <@' + format(ctx.message.author.id) + '>! <:REEeee:790963160495947856> Wpisz pojawiające się komendy tak szybko, jak to możliwe!')

            #Start time counting
            startTime = datetime.datetime.utcnow()

            #Random the message and requested action
            requestedAction = [("unik", "atak", "paruj", "skok", "biegnij"), ("Boss szarżuje na Ciebie! Wpisz **UNIK**!!!", "Boss zawahał się! Teraz! Wpisz **ATAK**!!!", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **PARUJ**!!!", 
            "Boss próbuje ataku w nogi, wpisz **SKOK**!!!", "Boss szykuje potężny atak o szerokim zasięgu, wpisz **BIEGNIJ**!!!")]
            

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
                            await ctx.channel.send('Brawo <@' + format(ctx.message.author.id) + '>! Pokonałeś bossa! <:POGGIES:790963160491753502><:POGGIES:790963160491753502><:POGGIES:790963160491753502>')
                            
                            #Time record
                            endTime = datetime.datetime.utcnow()
                            recordTime = endTime - startTime
                            await ctx.channel.send('Zabicie bossa zajęło Ci: ' + str(recordTime).lstrip('0:00:') + ' sekundy!')
                            with open('recordTime.txt', 'r') as r:
                                previousRecord = r.readlines()
                                
                            if datetime.datetime.strptime(previousRecord[0].rstrip('\n'), "%H:%M:%S.%f") > datetime.datetime.strptime(str(recordTime), "%H:%M:%S.%f"):
                                await ctx.channel.send('Pobiłeś rekord i zgarniasz dodatkowe 3000 doświadczenia na discordzie!')
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
                        bossAlive = False
                        break
                except asyncio.TimeoutError:
                    await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400> Boss pojawi się później! <:RIP:912797982917816341>')
                    bossAlive = False
                    break
        else:
            print("Boss is not alive!")
            await ctx.channel.send('Boss nie żyje, poczekaj na jego spawn <@' + format(ctx.message.author.id) + '>!')

    # command to check boss kill record
    @commands.command(pass_context=True, name="rekord", brief="Check previous boss kill record")
    async def recordMessage(self, ctx):
        with open('recordTime.txt', 'r') as f:
            recordLines = f.readlines()
        await ctx.channel.send('Poprzedni rekord należy do **' + recordLines[1] + '** i wynosi **' + recordLines[0].rstrip('\n').lstrip('0:00:') + ' sekundy**.')
                     

def setup(bot):
    bot.add_cog(message(bot))
