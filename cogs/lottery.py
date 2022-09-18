from discord.ext import commands
import random
import json

# Bag for lottery related commends and logic
class Lottery(commands.Cog, name="Lottery"):
    def __init__(self, bot):
        self.bot = bot

    #Choosing random user from users that reacted on given message, command takes ID
    #how to get ID of message: Message id is the last part of message link
    #right click on message copy link
    #f.e https://discord.com/channels/857296258594635816/857296258594635820/858771394191425577
    #858771394191425577 is the ID of the message
    @commands.command(name="lotto")
    
    async def lotto(self, ctx, ID: int):
        with open("ranksConfig.json", encoding='utf-8') as jsonFile:
            jsonObject = json.load(jsonFile)
        users = []
        weights = []
        message = await ctx.channel.fetch_message(ID)
        for reaction in message.reactions:
            async for user in reaction.users():
                guild = message.guild
                if guild.get_member(user.id) is not None:
                     users.append(user)               
        for user in users:				#użytkownicy, którzy zareagowali
            print("=====New User=======")
            print(user)
            ranksOfUser = []
            for role in user.roles:		
                ranksOfUser.append(role.name)           #wszystkie role użytkownika dopisujemy
            #weights.append(1)							#domyslnie szansa x1
            temp_weight = 1.0
            for rank in jsonObject.keys():              #sprawdzanie po skonfigurowanych rankach
                #print(ranksOfUser)
                if rank in ranksOfUser:
                    print(rank)
                    print("===")
                    temp_weight = temp_weight + jsonObject[rank]
            if temp_weight > 1.0:						#odjecie 1 gdy ktos ma bonus
                temp_weight -= 1
            weights.append(temp_weight)
            print(temp_weight)			
			
        print(weights)
        print(users)
        winner = random.choices(users, weights)
        await ctx.channel.send('Loterię wygrał <@' + str(winner[0].id) + '>! Gratulacje!')        

async def setup(bot):
    await bot.add_cog(Lottery(bot))
