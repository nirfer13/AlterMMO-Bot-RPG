from discord.ext import commands
import discord
import json

#Import Globals
from globals.globalvariables import DebugMode

class functions_rpg_general(commands.Cog, name="functions_rpg_general"):
    def __init__(self, bot):
        self.bot = bot

    #CREATE NEW CHARACTER
    #newtogeneral(Update general character info) -> calcStats (calculate additional stats) -> Update Stats (Update stats in database)
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
        sql=("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(ctx.author.id)))
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

            print("Before hero stats reading...")
            MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP = await readHeroStatsTable(self, ctx, ctx.author.id)
            print("Hero stats read.")

            EXP = check[3]
            LVL = check[4]
            STR = check[5]
            AGI = check[6]
            INTEL =  check[7]
            STAM = check[8]
            REMPOINTS = check[9]

            print("Preparing Description...")
            desc1 = "**POZIOM:** " + str(LVL)
            desc2 = "\n**DOŚWIADCZENIE:** " + str(EXP) + "/5000"

            desc3 = "\n\n<:RPGHP:995641654616805396> **HP:** " + str(ActHP) + "/" + str(MaxHP)
            desc4 = "\n<:RPGMana:995641689899286568> **MANA:** " + str(ActMP) + "/" + str(MaxMP)

            desc5 = "\n\n<:RPGStats:995642897787523072> **STATYSTYKI**"
            desc6 = "\nSIŁA: " + str(STR)
            desc7 = "\nZRĘCZNOŚĆ: " + str(AGI)
            desc8 = "\nINTELIGENCJA: " + str(INTEL)
            desc9 = "\nWYTRZYMAŁOŚĆ: " + str(STAM)
            desc10 = "\nPKT DO PRZYDZIELENIA: " + str(REMPOINTS)

            desc11 = "\n\n<:RPGAddStat:995642835531472956> **DODATKOWE STATYSTYKI**"
            desc12 = "\nFIZ. ATAK: " + str(PATK)
            desc13 = "\nMAG. ATAK: " + str(MATK)
            desc14 = "\nFIZ. OBRONA: " + str(PDEF)
            desc15 = "\nMAG. OBRONA: " + str(MDEF)
            desc16 = "\nUNIK: " + str(DODGE * 100) + "%"
            desc17 = "\nSZANSA NA KRYT.: "+ str(CRIT * 100) + "%"
            desc18 = "\nODBIJANIE OBR.: " + str(RFLCT*100) + "%"
            desc19 = "\nREFLEKS: " + str(RFLX) + " sekundy"
            desc20 = "\nSZANSA NA DODATKOWY DROP: " + str(ADDROP*100) + "%"

            descript = desc1 + desc2 + desc3 + desc4 + desc5 + desc6 + desc7 + desc8 + desc9 + desc10 + desc11 + desc12 + desc13 + desc14 + desc15 + desc16 + desc17 + desc18 + desc19 + desc20

            embed=discord.Embed(title='Bohater ' + str(user.name), url=url1, description=descript, color=0x00C1C7)
            embed.set_thumbnail(url=url1)
            await ctx.channel.send(embed=embed)
        else:
            embed=discord.Embed(title='Bohater nie istnieje!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Nie stworzyłeś jeszcze swojego bohatera. Możesz to zrobić wpisując komendę **#start**!", color=0x00C1C7)
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
            botMessage = await ctx.channel.send(embed=embed)

        print("Profile presented.")

    #CALCULATE ADDITIONAL STATS
    global calcStats
    async def calcStats(self, ctx, ID):
        sql=("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(ID)))
        check = await self.bot.pg_con.fetch(sql)
        if check:
            print("User exists!")
            check = check[0]
            print(check[2])
            #Parse to variables
            playerID = check[0]
            CLASS = str(check[2])
            EXP = check[3]
            LVL = check[4]
            print("Class: " + str(check[2]) + ", EXP: " + str(check[3]) + ", LVL: " + str(check[4]) + ", STR: " + str(check[5])+ ",AGI: " + str(check[6])+ ", INT: " + str(check[7])+ ", STAM: " + str(check[8]))
            
            STR = check[5]
            AGI = check[6]
            INT = check[7]
            STAM = check[8]
            
            print("Reading calc constants from the file...")
            #Stat point/lvl, HP/Lvl, MP/Lvl | ATK/1STR, HP/1STR | ATK/1INT, CRIT/1INT, MP/1INT | ATK/1AGI, DODGE/1AGI, CRIT/1AGI
            with open("HeroStatConfig.json", encoding='utf-8') as jsonFile:
                jsonObject = json.loads(jsonFile.read())
            print("Constants from file loaded.")

            #General
            Scale_StatPoint_Lvl = jsonObject['General'][0]['weight']
            print("Stat point/lvl = " + str(Scale_StatPoint_Lvl))
            Scale_HP_Lvl = jsonObject['General'][1]['weight']
            print("HP/lvl = " + str(Scale_HP_Lvl))
            Scale_MP_Lvl = jsonObject['General'][2]['weight']
            print("MP/lvl = " + str(Scale_MP_Lvl))
            Scale_HP_STAM = jsonObject['General'][3]['weight']
            print("HP/STAM = " + str(Scale_HP_STAM))

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

            #RPG_HERO_STATS (ID, STR, AGI, INTEL, STAM, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP)
            if str(CLASS) == "Wojownik":
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT_Alt*INT + 0
                PATK = Scale_ATK_STR*STR + Scale_ATK_AGI_Alt*AGI + 0
                MATK = Scale_ATK_INT_Alt*INT
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI_Alt*AGI + 0
                CRIT = Scale_CRIT_AGI_Alt*AGI + 0
                RFLCT = 0 #TODO From EQ
                RFLX = 0 #TODO From EQ
                ADDROP = 0 #TODO From EQ
            elif str(CLASS) == "Łotrzyk":
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR_Alt*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT_Alt*INT + 0
                PATK = Scale_ATK_STR_Alt*STR + Scale_ATK_AGI*AGI + 0
                MATK = Scale_ATK_INT_Alt*INT
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI*AGI + 0
                CRIT = Scale_CRIT_AGI*AGI + 0
                RFLCT = 0 #TODO From EQ
                RFLX = 0 #TODO From EQ
                ADDROP = 0 #TODO From EQ
            elif str(CLASS) == "Mag":
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR_Alt*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT*INT + 0
                PATK = Scale_ATK_STR_Alt*STR + Scale_ATK_AGI_Alt*AGI + 0
                MATK = Scale_ATK_INT*INT
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI_Alt*AGI + 0
                CRIT = Scale_CRIT_AGI_Alt*AGI + 0
                RFLCT = 0 #TODO From EQ
                RFLX = 0 #TODO From EQ
                ADDROP = 0 #TODO From EQ
            elif str(CLASS) == "Kapłan":
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR_Alt*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT*INT + 0
                PATK = Scale_ATK_STR_Alt*STR + Scale_ATK_AGI_Alt*AGI + 0
                MATK = Scale_ATK_INT*INT
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

        MaxHP = HP
        ActHP = MaxHP
        MaxMP = MP
        ActMP = MaxMP

        await insertHeroStats(self, ctx, playerID, CLASS, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP)



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
        print('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS) VALUES (\'{}\',\'{}\',\'{}\',{},{},{},{},{},{},{});'.format(str(playerID), user.name, str(playerClass), 0, 1, 5, 5, 5, 5, 0))
        await self.bot.pg_con.execute('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS) VALUES (\'{}\',\'{}\',\'{}\',{},{},{},{},{},{},{});'.format(str(playerID), user.name, str(playerClass), 0, 1, 5, 5, 5, 5, 0))
        print("Database updated with general stats.")
        print("Data inserted in General RPG Database.")
        if playerClass == "Wojownik":
                url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"
        elif playerClass == "Mag":
            url1="https://www.altermmo.pl/wp-content/uploads/Icon21.png"
        elif playerClass == "Łotrzyk":
            url1="https://www.altermmo.pl/wp-content/uploads/Icon39.png"
        elif playerClass == "Kleryk":
            url1="https://www.altermmo.pl/wp-content/uploads/Icon20.png"
        else:
            url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"
        embed=discord.Embed(title='Powodzenia!', url=url1, description="Świetnie! Stworzyłeś swojego bohatera, którym możesz zatopić się w świat fantasy AlterMMO. Przyjemnych przygód!", color=0x00C1C7)
        embed.set_thumbnail(url=url1)
        botResponseMsg = await ctx.channel.send(embed=embed)

        await calcStats(self, ctx, playerID)

    global readRpgGeneral
    async def readRpgGeneral(self, ctx):
        #Database Reading
        print("Trying to read RPG General Database...")
        dbRpgRead = await self.bot.pg_con.fetch("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS, EQONIDS, EQOFFIDS FROM RPG_GENERAL ORDER BY LEVEL DESC LIMIT 10")
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
                print("STR: " + str(Person[5]))
                print("AGI: " + str(Person[6]))
                print("INTEL: " + str(Person[7]))
                print("STAM: " + str(Person[8]))
                print("REMAINING POINTS: " + str(Person[9]))
                print("EQIDON: " + str(Person[10]))
                print("EQIDOFF: " + str(Person[11]))
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
        sql ='''CREATE TABLE RPG_HERO_STATS (
           ID VARCHAR(255) PRIMARY KEY,
           CURRENT_CLASS VARCHAR(255),
           MaxHP INT,
           ActHP INT,
           MaxMP INT,
           ActMP INT,
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

    global insertHeroStats
    async def insertHeroStats(self, ctx, playerID, playerClass, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP):
        #Database Update
        print("Trying to insert Hero Stats...")
        print('INSERT INTO RPG_HERO_STATS (ID, CURRENT_CLASS, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP) VALUES ({},\'{}\',{},{},{},{},{},{},{},{},{},{},{},{},{});'.format(str(playerID), str(playerClass), MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP))
        await self.bot.pg_con.execute('INSERT INTO RPG_HERO_STATS (ID, CURRENT_CLASS, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP) VALUES ({},\'{}\',{},{},{},{},{},{},{},{},{},{},{},{},{});'.format(str(playerID), str(playerClass), MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP))
        print("Data inserted in Hero Stats Database.")

    global updateHeroStats
    async def updateHeroStats(self, ctx, playerID, playerClass, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP):
        #Database Update
        print("Trying to update Hero Stats...")
        print('UPDATE RPG_HERO_STATS SET CURRENT_CLASS = \'{}\', MaxHP = {}, ActHP = {}, MaxMP = {}, ActMP = {}, PATK = {}, MATK = {}, PDEF = {}, MDEF = {}, DODGE = {}, CRIT = {}, RFLCT = {}, RFLX = {}, ADDROP = {} WHERE ID = \'{}\''.format(str(playerID), str(playerClass), MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP, str(playerID)))
        await self.bot.pg_con.execute('UPDATE RPG_HERO_STATS SET CURRENT_CLASS = \'{}\', MaxHP = {}, ActHP = {}, MaxMP = {}, ActMP = {}, PATK = {}, MATK = {}, PDEF = {}, MDEF = {}, DODGE = {}, CRIT = {}, RFLCT = {}, RFLX = {}, ADDROP = {} WHERE ID = \'{}\''.format(str(playerClass), MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP, str(playerID)))
        print("Data updated in Hero Stats Database.")

    global updateHPMPHeroStats
    async def updateHPMPHeroStats(self, ctx, playerID, ActHP, ActMP):
        print("Trying to update HP/MP Hero Stats...")
        print('UPDATE RPG_HERO_STATS SET ActHP = {}, ActMP = {} WHERE ID = \'{}\''.format(ActHP, ActMP, str(playerID)))
        await self.bot.pg_con.execute('UPDATE RPG_HERO_STATS SET ActHP = {}, ActMP = {} WHERE ID = \'{}\''.format(ActHP, ActMP, str(playerID)))
        print("Data updated in HP/MP Hero Stats Database.")

    global readHeroStatsTable
    async def readHeroStatsTable(self, ctx, playerID):
        #Database Reading
        print("Trying to read Hero Stats Database...")
        dbRpgStatsRead = await self.bot.pg_con.fetch("SELECT ID, CURRENT_CLASS, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP FROM RPG_HERO_STATS WHERE ID = \'{}\';".format(str(playerID)))
        
        print("ID: " + dbRpgStatsRead[0][0])
        ID = dbRpgStatsRead[0][0]
        print("Class: " + str(dbRpgStatsRead[0][1]))
        Class = dbRpgStatsRead[0][1]
        print("MaxHP: " + str(dbRpgStatsRead[0][2]))
        MaxHP = dbRpgStatsRead[0][2]
        print("ActHP: " + str(dbRpgStatsRead[0][3]))
        ActHP = dbRpgStatsRead[0][3]
        print("MaxMP: " + str(dbRpgStatsRead[0][4]))
        MaxMP = dbRpgStatsRead[0][4]
        print("ActMP: " + str(dbRpgStatsRead[0][5]))
        ActMP = dbRpgStatsRead[0][5]
        print("PATK: " + str(dbRpgStatsRead[0][6]))
        PATK = dbRpgStatsRead[0][6]
        print("MATK: " + str(dbRpgStatsRead[0][7]))
        MATK = dbRpgStatsRead[0][7]
        print("PDEF: " + str(dbRpgStatsRead[0][8]))
        PDEF = dbRpgStatsRead[0][8]
        print("MDEF: " + str(dbRpgStatsRead[0][9]))
        MDEF = dbRpgStatsRead[0][9]
        print("DODGE: " + str(dbRpgStatsRead[0][10]))
        DODGE = dbRpgStatsRead[0][10]
        print("CRIT: " + str(dbRpgStatsRead[0][11]))
        CRIT = dbRpgStatsRead[0][11]
        print("RFLCT: " + str(dbRpgStatsRead[0][12]))
        RFLCT = dbRpgStatsRead[0][12]
        print("RFLX: " + str(dbRpgStatsRead[0][13]))
        RFLX = dbRpgStatsRead[0][13]
        print("ADDROP: " + str(dbRpgStatsRead[0][14]))
        ADDROP = dbRpgStatsRead[0][14]

        return MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP





def setup(bot):
    bot.add_cog(functions_rpg_general(bot))
