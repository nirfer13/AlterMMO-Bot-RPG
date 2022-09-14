﻿from discord.ext import commands
import discord
import json

#Import Globals
from globals.globalvariables import DebugMode

class functions_rpg_general(commands.Cog, name="functions_rpg_general"):
    def __init__(self, bot):
        self.bot = bot

    #CREATE NEW CHARACTER
    global createCharacter
    async def createCharacter(self, ctx, playerID):
        print("Creating character.")
        #CHECK IF USER EXISTS IN DATABASE
        print("Checking if user exists...")
        sql=("SELECT ID, NICK FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(playerID)))
        check = await self.bot.pg_con.fetch(sql)
        #User Exists
        if check:
            embed=discord.Embed(title='Bohater istnieje!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Stworzyłeś już bohatera wcześniej. Możesz podglądnąć swój profil wpisując **#profil**. Być może w przyszłości pojawi się możliwość zmiany klasy.", color=0x00C1C7)
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
            botMessage = await ctx.channel.send(embed=embed)
        else:
            warriorEmoji = "<:RPGWarrior:995576809666134046>"
            mageEmoji = "<:RPGMage:995577415462047765>"
            rogueEmoji = "<:RPGRogue:995577306745679942>"
            clericEmoji = "<:RPGCleric:995577107642073098>"

            #Embed create
            desc1 = "Chcesz zostać bohaterem, jakiego ten Discord nie widział <@" + str(playerID) + ">? <:GigaChad:970665721321381958>\n\nPo stworzeniu swojego profilu będziesz mógł zdobywać doświadczenie, wykonywać zadania czy zbierać i ulepszać ekwipunek, aby pokonywać coraz trudniejsze potwory. <:Up:912798893304086558>\n\n**Pierwszym krokiem jest wybranie klasy:**\n\n"
            desc2 = warriorEmoji + " **Wojownik** - To ten, który przyjmie na klatę najwięcej obrażeń i wciąż będzie szedł w Twoją stronę, a gdy już dojdzie...\n"
            desc3 = mageEmoji + " **Mag** - Woli trzymać się z tyłu, bo tam w spokoju może przygotować potężne zaklęcia, które spopielą lub zamrożą przeciwników.\n"
            desc4 = rogueEmoji + " **Łotrzyk** - Cichy zabójca, który nie przepada za wrogimi toporami czy kulami ognia i dlatego po prostu ich zwinnie unika.\n"
            desc5 = clericEmoji + " **Kleryk** - Chciałby wszystkim pomagać przez leczenie i rzucanie zaklęć wspierających, jednak świat jest jaki jest i dlatego postanowił się dostosować.\n"
            desc6 = "\n**Wybierz reakcję odpowiadającą danej klasie!**"
            embed=discord.Embed(title='Tworzenie postaci', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description=desc1 + desc2 + desc3 + desc4 + desc5 + desc6, color=0x00C1C7)
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
            embed.set_footer(text='Powodzenia!')
            botMessage = await ctx.channel.send(embed=embed)

            await botMessage.add_reaction(warriorEmoji)
            await botMessage.add_reaction(mageEmoji)
            await botMessage.add_reaction(rogueEmoji)
            await botMessage.add_reaction(clericEmoji)
            await botMessage.add_reaction("🔴")

            def check(reaction, user):
                print(str(reaction.emoji))
                return user == ctx.author and str(reaction.emoji) in [warriorEmoji, mageEmoji, rogueEmoji, clericEmoji, "🔴"]

            print("Waiting for reaction")
            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=90, check=check)
                print(str(type(reaction)))
            except:
                await botMessage.delete()
                embed=discord.Embed(title='Spróbuj później!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Daj znać, gdy się zastanowisz i po prostu spróbuj później.", color=0x00C1C7)
                embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
                botMessage = await ctx.channel.send(embed=embed)
        
            x = str(reaction.emoji)
            if str(reaction.emoji) == warriorEmoji:
                playerClass = "Wojownik"
            elif str(reaction.emoji) == mageEmoji:
                playerClass = "Mag"
            elif str(reaction.emoji) == rogueEmoji:
                playerClass = "Łotrzyk"
            elif str(reaction.emoji) == clericEmoji:
                playerClass = "Kleryk"
            else:
                embed=discord.Embed(title='Spróbuj później!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Daj znać, gdy się zastanowisz i po prostu spróbuj później.", color=0x00C1C7)
                embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
                botResponseMsg = await ctx.channel.send(embed=embed)

            await botMessage.delete()
            await newtoRpgGeneral(self, ctx, ctx.author.id, playerClass)

    #CHECK PROFILE OF THE CHARACTER
    global checkGeneralProfile
    async def checkGeneralProfile(self, ctx):
        print("Checking profile...")
        #CHECK IF USER EXISTS IN DATABASE
        print("Checking if user exists...")
        print(ctx.author.id)
        sql=("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(ctx.author.id)))
        check = await self.bot.pg_con.fetch(sql)
        print(check)
        if check:
            print("User exists!")
            check = check[0]
            print(check[2])
            if check[2] == "Wojownik":
                url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"
            elif check[2] == "Mag":
                url1="https://www.altermmo.pl/wp-content/uploads/Icon21.png"
            elif check[2] == "Łotrzyk":
                url1="https://www.altermmo.pl/wp-content/uploads/Icon39.png"
            elif check[2] == "Kleryk":
                url1="https://www.altermmo.pl/wp-content/uploads/Icon20.png"
            else:
                url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"

            user = self.bot.get_user(ctx.author.id)

            print("Preparing Description...")
            desc1 = "**POZIOM:** " + str(check[4])
            desc2 = "\n**DOŚWIADCZENIE:** " + str(check[3]) + "/5000" #TODO

            #TODO
            desc3 = "\n\n<:RPGHP:995641654616805396> **HP:** " + "10/10"
            desc4 = "\n<:RPGMana:995641689899286568> **MANA:** " + "10/15"

            desc45 = "\n\n<:RPGStats:995642897787523072> **STATYSTYKI**"
            desc5 = "\nSIŁA: " + "10"
            desc6 = "\nZRĘCZNOŚĆ: " + "5"
            desc7 = "\nINTELIGENCJA: " + "5"
            desc8 = "\nWYTRZYMAŁOŚĆ: " + "10"

            desc89 = "\n\n<:RPGAddStat:995642835531472956> **DODATKOWE STATYSTYKI**"
            desc9 = "\nATAK: " + "10"
            desc10 = "\nUNIK: " + "10 %"
            desc11 = "\nSZANSA NA KRYT.: " + "36 %"
            desc12 = "\nODBIJANIE OBR.: " + "5 %"
            desc13 = "\nREFLEKS: " + "0.5 sekundy"
            desc14 = "\nSZANSA NA DODATKOWY DROP: " + "15 %"

            descript = desc1 + desc2 + desc3 + desc4 + desc45 + desc5 + desc6 + desc7 + desc8 + desc89 + desc9 + desc10 + desc11 + desc12 + desc13 +desc14

            embed=discord.Embed(title='Bohater ' + str(user.name), url=url1, description=descript, color=0x00C1C7)
            embed.set_thumbnail(url=url1)
            await ctx.channel.send(embed=embed)
        else:
            embed=discord.Embed(title='Bohater nie istnieje!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Nie stworzyłeś jeszcze swojego bohatera. Możesz to zrobić wpisując komendę **#start**!", color=0x00C1C7)
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
            botMessage = await ctx.channel.send(embed=embed)

    #CALCULATE ADDITIONAL STATS
    global calcStats
    async def calcStats(self, ctx, ID):
        sql=("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(ID)))
        check = await self.bot.pg_con.fetch(sql)
        if check:
            print("User exists!")
            check = check[0]
            print(check[2])
            #Parse to variables
            CLASS = str(check[2])
            EXP = check[3]
            LVL = check[4]
            print("Class: " + str(check[2]) + ", EXP: " + str(check[3]) + ", LVL: " + str(check[4]))
            print("Reading calc constants from the file...")
            #Stat point/lvl, HP/Lvl, MP/Lvl | ATK/1STR, HP/1STR | ATK/1INT, CRIT/1INT, MP/1INT | ATK/1AGI, DODGE/1AGI, CRIT/1AGI
            with open("HeroStatConfig.json", encoding='utf-8') as jsonFile:
                jsonObject = json.loads(jsonFile.read())
            #TODO From file to variables and calculate

            #General
            Scale_StatPoint_Lvl = jsonObject['General'][0]['weight']
            print("Stat point/lvl = " + str(Scale_StatPoint_Lvl))
            Scale_HP_Lvl = jsonObject['General'][1]['weight']
            print("HP/lvl = " + str(Scale_HP_Lvl))
            Scale_MP_Lvl = jsonObject['General'][2]['weight']
            print("MP/lvl = " + str(Scale_MP_Lvl))

            #Strength
            Scale_ATK_STR = jsonObject['Strength'][0]['weight']
            print("ATK/STR = " + str(Scale_ATK_STR))
            Scale_HP_STR = jsonObject['Strength'][1]['weight']
            print("HP/STR = " + str(Scale_HP_STR))

            #Intelligence
            Scale_ATK_INT = jsonObject['Intelligence'][0]['weight']
            print("ATK/INT = " + str(Scale_ATK_INT))
            Scale_CRIT_INT = jsonObject['Intelligence'][1]['weight']
            print("CRIT/INT = " + str(Scale_CRIT_INT))
            Scale_MP_INT = jsonObject['Intelligence'][2]['weight']
            print("MP/INT = " + str(Scale_MP_INT))

            #Agility
            Scale_ATK_AGI = jsonObject['Agility'][0]['weight']
            print("ATK/AGI = " + str(Scale_ATK_AGI))
            Scale_DODGE_AGI = jsonObject['Agility'][1]['weight']
            print("DODGE/AGI = " + str(Scale_DODGE_AGI))
            Scale_CRIT_AGI = jsonObject['Agility'][2]['weight']
            print("CRIT/AGI = " + str(Scale_CRIT_AGI))

            #Strength_Alt
            Scale_ATK_STR_Alt = jsonObject['Strength_Alt'][0]['weight']
            print("ATK/STR_Alt = " + str(Scale_ATK_STR_Alt))
            Scale_HP_STR_Alt = jsonObject['Strength_Alt'][1]['weight']
            print("HP/STR_Alt = " + str(Scale_HP_STR_Alt))

            #Intelligence_Alt
            Scale_ATK_INT_Alt = jsonObject['Intelligence_Alt'][0]['weight']
            print("ATK/INT_Alt = " + str(Scale_ATK_INT_Alt))
            Scale_CRIT_INT_Alt = jsonObject['Intelligence_Alt'][1]['weight']
            print("CRIT/INT_Alt = " + str(Scale_CRIT_INT_Alt))
            Scale_MP_INT_Alt = jsonObject['Intelligence_Alt'][2]['weight']
            print("MP/INT_Alt = " + str(Scale_MP_INT_Alt))

            #Agility_Alt
            Scale_ATK_AGI_Alt = jsonObject['Agility_Alt'][0]['weight']
            print("ATK/AGI_Alt = " + str(Scale_ATK_AGI_Alt))
            Scale_DODGE_AGI_Alt = jsonObject['Agility_Alt'][1]['weight']
            print("DODGE/AGI_Alt = " + str(Scale_DODGE_AGI_Alt))
            Scale_CRIT_AGI_Alt = jsonObject['Agility_Alt'][2]['weight']
            print("CRIT/AG_AltI = " + str(Scale_CRIT_AGI_Alt))


            #Read database with additional stts
            #Check EQ with stats

            #RPG_HERO_STATS (ID, STR, AGI, INTEL, STAM, HP, MP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP)
            if str(CLASS) == "Wojownik":
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR*STR + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT_Alt*INT + 0
                PATK = Scale_ATK_STR*STR + Scale_ATK_AGI_Alt + 0
                MATK = Scale_ATK_INT_Alt
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI_Alt*AGI + 0
                CRIT = Scale_CRIT_AGI_Alt*AGI + 0
                RFLCT = 0 #TODO From EQ
                RFLX = 0 #TODO From EQ
                ADDROP = 0 #TODO From EQ
            else:
                print("Error! Unknown class.")

        else:
            print("Hero does not exists. Error!")

    #============================ RPG GENERAL DATABASE ==============================

    #generate RPG Database
    global createRpgGeneralTable
    async def createRpgGeneralTable(self, ctx):
        #Droping General RPG table if already exists.
        print("Trying to create RPG Database.")
        await self.bot.pg_con.execute("DROP TABLE IF EXISTS RPG_GENERAL")
        print("RPG Database dropped.")
        #Creating table as per requirement
        sql ='''CREATE TABLE RPG_GENERAL (
           ID VARCHAR(255) PRIMARY KEY,
           NICK VARCHAR(255),
           CURRENT_CLASS VARCHAR(255),
           EXPERIENCE INT,
           LEVEL INT,
           STR INT,
           AGI INT,
           INTEL INT,
           STAM INT,
           REMPOINTS INT,
           EQONIDS INT[],
           EQOFFIDS INT[]
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table RPG_GENERAL created successfully.")
        #print('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL) VALUES ({},\'{}\',\'{}\',{},{});'.format("291836779495948288","Andrzej", "Warrior", "50", "2"))
        #await self.bot.pg_con.execute('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL) VALUES ({},\'{}\',\'{}\',{},{});'.format("291836779495948288","Andrzej", "Wojownik", "50", "2"))
        #print("Data inserted into RPG_GENERAL Database.")

    global newtoRpgGeneral
    async def newtoRpgGeneral(self, ctx, playerID, playerClass):
        #Database Update
        user = self.bot.get_user(playerID)
        print("Trying to update Database...")
        print('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL) VALUES ({},\'{}\',\'{}\',{},{});'.format(str(playerID), user.name, str(playerClass), 0, 1))
        await self.bot.pg_con.execute('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL) VALUES (\'{}\',\'{}\',\'{}\',{},{});'.format(str(playerID), user.name, str(playerClass), 0, 1))
        #TODO Default Additional Stats Based on Class
        print("Data inserted in General RPG Database.")

    global readRpgGeneral
    async def readRpgGeneral(self, ctx):
        #Database Reading
        print("Trying to read RPG General Database...")
        dbRpgRead = await self.bot.pg_con.fetch("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL FROM RPG_GENERAL ORDER BY LEVEL DESC LIMIT 10")
        x = 1
        rankingString = ""
        for Person in dbRpgRead:
            user = self.bot.get_user(int(Person[0]))
            if user:
                rankingString += str(x) + ". **" + user.name + "** - Klasa: " + str(Person[2]) + " ("+ str(Person[4]) + " poziom)\n"
                print("ID: " + Person[0])
                print("Class: " + str(Person[2]))
                print("Nick: " + str(Person[1]))
                print("Level: " + str(Person[4]))
                print("Exp: " + str(Person[3]))
                x+=1

        #Embed create   
        emb=discord.Embed(title='Ranking bohaterów!', url='https://www.altermmo.pl/wp-content/uploads/Icon24.png', description=rankingString, color=0x00C1C7)
        emb.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon24.png')
        await ctx.send(embed=emb)

    #============================ HERO STATS DATABASE ==============================
    #generate Hero Stats Database
    global createHeroStatsTable
    async def createHeroStatsTable(self, ctx):
        #Droping General RPG table if already exists.
        print("Trying to create Hero Stats Database.")
        await self.bot.pg_con.execute("DROP TABLE IF EXISTS RPG_HERO_STATS")
        print("RPG Hero Stats dropped.")
        #Creating table as per requirement
        sql ='''CREATE TABLE RPG_HER_STATS (
           ID VARCHAR(255) PRIMARY KEY,
           CURRENT_CLASS VARCHAR(255),
           HP INT,
           MP INT,
           PATK INT,
           MATK INT,
           PDEF INT,
           MDEF INT,
           DODGE INT,
           CRIT INT,
           RFLCT INT,
           RFLX INT,
           ADDROP INT
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table RPG_HERO_STATS created successfully.")
        #print('INSERT INTO RPG_HERO_STATS (ID, STR, AGI, INTEL, STAM, HP, MP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP) VALUES ({},/'{}/',{},{},{},{},{},{},{},{},{},{},{},{},{},{});'.format("291836779495948288", "Wojownik","5", "5", "5", "5"))
        #await self.bot.pg_con.execute('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL) VALUES ({},\'{}\',\'{}\',{},{});'.format("291836779495948288","Andrzej", "Wojownik", "50", "2"))
        #print("Data inserted into RPG_HERO_STATS Database.")




def setup(bot):
    bot.add_cog(functions_rpg_general(bot))
