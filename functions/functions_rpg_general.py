
import discord
from discord.ext import commands
from enum import Enum

from discord.ui import Button, View
import json

from discord.utils import sane_wait_for

#Import Globals
from globals.globalvariables import DebugMode

#Character Classes
global Class
class Class(Enum):
    Warrior = 0
    Mage = 1
    Rogue = 2
    Cleric = 3

global bot

class functions_rpg_general(commands.Cog, name="functions_rpg_general"):
    def __init__(self, bot):
        self.bot = bot

    #============================ USERS FUNCTIONS ==============================

    #CREATE NEW CHARACTER
    #newtogeneral(Update general character info) -> calcStats (calculate additional stats) -> Update Stats (Update stats in database)
    global createCharacter
    async def createCharacter(self, ctx, playerID):
        print("Creating character.")
        global cmdMessage
        cmdMessage = ctx.message
        
        global self_var, ctx_var
        ctx_var = ctx
        self_var = self
        #CHECK IF USER EXISTS IN DATABASE
        print("Checking if user exists...")
        sql=("SELECT ID, NICK FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(playerID)))
        check = await self.bot.pg_con.fetch(sql)
        #User Exists
        if check:
            embed=discord.Embed(title='Bohater istnieje!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Stworzyłeś już bohatera wcześniej <@" + str(ctx.author.id) + ">! Możesz podglądnąć swój profil wpisując **#profil**. Być może w przyszłości pojawi się możliwość zmiany klasy.", color=0x00C1C7)
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

            #Select class buttons
            global Class
            Class = Class.Cleric
            class MyView_SelectClass(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout = 120)
                @discord.ui.button(label="Wojownik", style = discord.ButtonStyle.green, emoji = warriorEmoji)
                async def verify1(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global self_var, ctx_var
                    playerClass = Class.Warrior
                    await botMessage.delete()
                    print("Message deleted")
                    await newtoRpgGeneral(self_var, ctx_var, ctx_var.author.id, playerClass)
                    await interaction.response.edit_message(view=None)
                @discord.ui.button(label="Mag", style = discord.ButtonStyle.green, emoji = mageEmoji)
                async def verify2(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global self_var, ctx_var
                    playerClass = Class.Mage
                    await botMessage.delete()
                    print("Message deleted")
                    await newtoRpgGeneral(self_var, ctx_var, ctx_var.author.id, playerClass)
                    await interaction.response.edit_message(view=None)
                @discord.ui.button(label="Łotrzyk", style = discord.ButtonStyle.green, emoji = rogueEmoji)
                async def verify3(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global self_var, ctx_var
                    playerClass = Class.Rogue
                    await botMessage.delete()
                    print("Message deleted")
                    await newtoRpgGeneral(self_var, ctx_var, ctx_var.author.id, playerClass)
                    await interaction.response.edit_message(view=None)
                @discord.ui.button(label="Kleryk", style = discord.ButtonStyle.green, emoji = clericEmoji)
                async def verify4(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global self_var, ctx_var
                    playerClass = Class.Cleric
                    await botMessage.delete()
                    print("Message deleted")
                    await newtoRpgGeneral(self_var, ctx_var, ctx_var.author.id, playerClass)
                    await interaction.response.edit_message(view=None)
                @discord.ui.button(label="Anuluj", style = discord.ButtonStyle.danger)
                async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global self_var, ctx_var
                    await cmdMessage.delete()
                    await botMessage.delete()
                    await interaction.response.edit_message(view=None)

                #Timeout
                async def on_timeout(self) -> None:
                    try:
                        await botMessage.delete()
                        embed=discord.Embed(title='Spróbuj później!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Daj znać, gdy się zastanowisz <@" + str(playerID) + "> i po prostu spróbuj później.", color=0x00C1C7)
                        embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
                        await ctx.channel.send(embed=embed)
                    except:
                        pass
                    
                #Check function
                async def interaction_check (self, interaction: discord.Interaction) -> bool:
                    if interaction.user == ctx_var.author:
                        print("The same user pressed button.")
                        return True
                    else:
                        print("Other user pressed button.")
                        return False

            view = MyView_SelectClass()
            botMessage = await ctx.channel.send(embed=embed, view=view)

    #CHECK PROFILE OF THE CHARACTER BY CTX
    global checkGeneralProfile
    async def checkGeneralProfile(self, ctx):
        print("Checking profile...")
        #CHECK IF USER EXISTS IN DATABASE
        print("Checking if user exists...")
        print(ctx.author.id)
        global playerID
        playerID = ctx.author.id
        sql=("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(ctx.author.id)))
        check = await self.bot.pg_con.fetch(sql)
        global self_var, ctx_var
        ctx_var = ctx
        self_var = self
        print(check)
        if check:
            print("User exists!")
            check = check[0]
            print(check[2])
            if check[2] == Class.Warrior.name:
                url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"
            elif check[2] == Class.Mage.name:
                url1="https://www.altermmo.pl/wp-content/uploads/Icon21.png"
            elif check[2] == Class.Rogue.name:
                url1="https://www.altermmo.pl/wp-content/uploads/Icon39.png"
            elif check[2] == Class.Cleric.name:
                url1="https://www.altermmo.pl/wp-content/uploads/Icon20.png"
            else:
                url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"

            user = self.bot.get_user(ctx.author.id)

            print("Before hero stats reading...")
            MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP = await readHeroStatsTable(self, ctx, playerID)
            print("Hero stats read.")

            EXP = check[3]
            LVL = check[4]
            STR = check[5]
            AGI = check[6]
            INTEL =  check[7]
            STAM = check[8]
            REMPOINTS = check[9]

            #Load experience table from file
            print("Reading exp constants from the file...")
            with open("expConfig.json", encoding='utf-8') as jsonFile:
                jsonObject = json.loads(jsonFile.read())
            print("Constants from file loaded.")

            #Get required exp based on lvl
            requiredExp = jsonObject['exp_required'][LVL+1]['exp']
            print("Exp required to next lvl: " + str(requiredExp))


            print("Preparing Description...")
            desc1 = "**POZIOM:** " + str(LVL)
            desc2 = "\n**DOŚWIADCZENIE:** " + str(EXP) + "/" + str(requiredExp)

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
            desc16 = "\nUNIK: " + str(round(DODGE * 100, 2)) + "%"
            desc17 = "\nSZANSA NA KRYT.: "+ str(round(CRIT * 100,2)) + "%"
            desc18 = "\nODBIJANIE OBR.: " + str(round(RFLCT * 100,2)) + "%"
            desc19 = "\nREFLEKS: " + str(round(RFLX,2)) + " sekundy"
            desc20 = "\nSZANSA NA DODATKOWY DROP: " + str(round(ADDROP * 100,2)) + "%"

            descript = desc1 + desc2 + desc3 + desc4 + desc5 + desc6 + desc7 + desc8 + desc9 + desc10 + desc11 + desc12 + desc13 + desc14 + desc15 + desc16 + desc17 + desc18 + desc19 + desc20

            embed=discord.Embed(title='Bohater ' + str(user.name), url=url1, description=descript, color=0x00C1C7)
            embed.set_thumbnail(url=url1)


            #Check if lvl up possible then show buttons
            class MyView_StartLvlUp(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout = 30)
                @discord.ui.button(label="Awansuj", style = discord.ButtonStyle.green, emoji = "<:Up:912798893304086558>")
                async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
                    await interaction.response.edit_message(view=MyView_ProgresLvlUp())
                async def interaction_check (self, interaction: discord.Interaction) -> bool:
                    if interaction.user == ctx_var.author:
                        print("The same user pressed button.")
                        return True
                    else:
                        print("Other user pressed button.")
                        return False

            #Global stats to increase after level up
            global addStr, addAgi, addInt, addStm, addRest
            addRest = REMPOINTS
            addStr = 0
            addAgi = 0
            addInt = 0
            addStm = 0

            #Stats to lvl up handling
            class MyView_ProgresLvlUp(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout = 60)
                @discord.ui.button(label="SIŁA", style = discord.ButtonStyle.green, emoji = "➕")
                async def verify1(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global addRest
                    if addRest > 0:
                        global addStr
                        addRest -= 1
                        addStr += 1
                        button.label = str(addStr) + " SIŁA"
                        print("Rem. points, str: " + str(addRest) + " " + str(addStr))
                        if addRest == 0:
                            print("Rest points 0")
                            await interaction.response.edit_message(view=MyView_FinishLvlUp())
                        else:
                            await interaction.response.edit_message(view=self)
                    else:
                        pass
                @discord.ui.button(label="ZRECZNOŚĆ", style = discord.ButtonStyle.green, emoji = "➕")
                async def verify2(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global addRest
                    if addRest > 0:
                        global addAgi
                        addRest -= 1
                        addAgi += 1
                        button.label = str(addAgi) + " ZRĘCZNOŚĆ"
                        print("Rem. points, agi: " + str(addRest) + " " + str(addAgi))
                        if addRest == 0:
                            print("Rest points 0")
                            await interaction.response.edit_message(view=MyView_FinishLvlUp())
                        else:
                            await interaction.response.edit_message(view=self)
                    else:
                        pass
                @discord.ui.button(label="INTELIGENCJA", style = discord.ButtonStyle.green, emoji = "➕")
                async def verify3(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global addRest
                    if addRest > 0:
                        global addInt
                        addRest -= 1
                        addInt += 1
                        button.label = str(addInt) + " INTELIGENCJA"
                        print("Rem. points, int: " + str(addRest) + " " + str(addInt))
                        if addRest == 0:
                            print("Rest points 0")
                            await interaction.response.edit_message(view=MyView_FinishLvlUp())
                        else:
                            await interaction.response.edit_message(view=self)
                    else:
                        pass
                @discord.ui.button(label="WYTRZYMAŁOŚĆ", style = discord.ButtonStyle.green, emoji = "➕")
                async def verify4(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global addRest
                    if addRest > 0:
                        global addStm
                        addRest -= 1
                        addStm += 1
                        button.label = str(addStm) + " WYTRZYMAŁOŚĆ"
                        print("Rem. points, stm: " + str(addRest) + " " + str(addStm))
                        if addRest == 0:
                            print("Rest points 0")
                            await interaction.response.edit_message(view=MyView_FinishLvlUp())
                        else:
                            await interaction.response.edit_message(view=self)
                    else:
                        pass
                @discord.ui.button(label="Anuluj", style = discord.ButtonStyle.danger)
                async def verify5(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global addStr, addAgi, addInt, addStm, addRest
                    addRest = REMPOINTS
                    addStr = 0
                    addAgi = 0
                    addInt = 0
                    addStm = 0
                    await interaction.response.edit_message(view=MyView_StartLvlUp())
                async def interaction_check (self, interaction: discord.Interaction) -> bool:
                    if interaction.user == ctx_var.author:
                        print("The same user pressed button.")
                        return True
                    else:
                        print("Other user pressed button.")
                        return False
            
            #Confirmation stats
            class MyView_FinishLvlUp(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout = 60)
                @discord.ui.button(label="Akceptuj", style = discord.ButtonStyle.green, emoji = "🟢")
                async def verify4(self, interaction: discord.Interaction, button: discord.ui.Button):
                    STR = check[5] + addStr
                    AGI = check[6] + addAgi
                    INTEL =  check[7] + addInt
                    STAM = check[8] + addStm
                    REMPOINTS = addRest
                    print(STR)
                    print(AGI)
                    print(INTEL)
                    print(STAM)
                    print(REMPOINTS)
                    global self_var, ctx_var
                    ctx = await self_var.bot.get_context(botMessage) 
                    global playerID
                    print(playerID)
                    await interaction.response.edit_message(view=None)
                    
                    print('UPDATE RPG_GENERAL SET STR = {}, AGI = {}, INTEL = {}, STAM = {}, REMPOINTS = {} WHERE ID = \'{}\''.format(STR, AGI, INTEL, STAM, REMPOINTS, str(playerID)))
                    await self_var.bot.pg_con.execute('UPDATE RPG_GENERAL SET STR = {}, AGI = {}, INTEL = {}, STAM = {}, REMPOINTS = {} WHERE ID = \'{}\''.format(STR, AGI, INTEL, STAM, REMPOINTS, str(playerID)))
                    print("Database updated.")
                    print(ctx)
                    await calcStats(self_var, ctx, playerID, False)
                    await botMessage.delete()
                    await checkGeneralProfile(self_var, ctx_var)
                @discord.ui.button(label="Anuluj", style = discord.ButtonStyle.danger)
                async def verify5(self, interaction: discord.Interaction, button: discord.ui.Button):
                    global addStr, addAgi, addInt, addStm, addRest
                    addRest = REMPOINTS
                    addStr = 0
                    addAgi = 0
                    addInt = 0
                    addStm = 0
                    await interaction.response.edit_message(view=MyView_StartLvlUp())
                async def interaction_check (self, interaction: discord.Interaction) -> bool:
                    if interaction.user == ctx_var.author:
                        print("The same user pressed button.")
                        return True
                    else:
                        print("Other user pressed button.")
                        return False
  
            #Lvl up available only when REMPOINTS > 0
            if REMPOINTS > 0:        
                view = MyView_StartLvlUp()
            else:
                view = None

            botMessage = await ctx.channel.send(embed=embed, view=view)
            
        else:
            embed=discord.Embed(title='Bohater nie istnieje!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Nie stworzyłeś jeszcze swojego bohatera. Możesz to zrobić wpisując komendę **#start**!", color=0x00C1C7)
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
            botMessage = await ctx.channel.send(embed=embed)

        print("Profile presented.")

    #REGEN HERO HP/MP
    global regenHPMP
    async def regenHPMP(self, ctx):
        playerID = ctx.author.id
        print("Before hero stats reading...")
        MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP = await readHeroStatsTable(self, ctx, playerID)
        print("Hero stats read.")
        print("Before hero healing...")
        await updateHPMPHeroStats(self, ctx, playerID, MaxHP, MaxMP)
        print("Hero healed.")

    #============================ AUXILIARY FUNCTIONS ==============================

    #CALCULATE ADDITIONAL STATS
    global calcStats
    async def calcStats(self, ctx, ID, new: bool):
        sql=("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(ID)))
        check = await self.bot.pg_con.fetch(sql)
        if check:
            print("User exists!")
            check = check[0]
            print(check[2])
            #Parse to variables
            playerID = check[0]
            CLASS = str(check[2])
            print("Class read from database: " + CLASS)
            EXP = check[3]
            LVL = check[4]
            print("Class: " + str(check[2]) + ", EXP: " + str(check[3]) + ", LVL: " + str(check[4]) + ", STR: " + str(check[5])+ ",AGI: " + str(check[6])+ ", INT: " + str(check[7])+ ", STAM: " + str(check[8]))
            
            STR = check[5]
            AGI = check[6]
            INT = check[7]
            STAM = check[8]
            
            print("Reading calc constants from the file...")
            #Stat point/lvl, HP/Lvl, MP/Lvl | ATK/1STR, HP/1STR | ATK/1INT, CRIT/1INT, MP/1INT | ATK/1AGI, DODGE/1AGI, CRIT/1AGI
            with open("heroStatConfig.json", encoding='utf-8') as jsonFile:
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


            #Read database with additional stats
            print("ENUM CLASS CHECK================")
            print(CLASS)
            print(Class.Rogue)
            print(Class.Rogue.name)
            #Check EQ with stats
            if CLASS == Class.Warrior.name:
                print("Stats calculated for Warrior")
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT_Alt*INT + 0
                PATK = Scale_ATK_STR*STR + Scale_ATK_AGI_Alt*AGI + 0
                MATK = Scale_ATK_INT_Alt*INT
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI_Alt*AGI + 0
                CRIT = Scale_CRIT_AGI_Alt*AGI + Scale_CRIT_INT_Alt*INT + 0
                RFLCT = 0 #TODO From EQ
                RFLX = 0 #TODO From EQ
                ADDROP = 0 #TODO From EQ
            elif CLASS == Class.Rogue.name:
                print("Stats calculated for Rogue")
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR_Alt*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT_Alt*INT + 0
                PATK = Scale_ATK_STR_Alt*STR + Scale_ATK_AGI*AGI + 0
                MATK = Scale_ATK_INT_Alt*INT
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI*AGI + 0
                CRIT = Scale_CRIT_AGI*AGI + Scale_CRIT_INT_Alt*INT + 0
                RFLCT = 0 #TODO From EQ
                RFLX = 0 #TODO From EQ
                ADDROP = 0 #TODO From EQ
            elif CLASS == Class.Mage.name:
                print("Stats calculated for Mage")
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR_Alt*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT*INT + 0
                PATK = Scale_ATK_STR_Alt*STR + Scale_ATK_AGI_Alt*AGI + 0
                MATK = Scale_ATK_INT*INT
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI_Alt*AGI + 0
                CRIT = Scale_CRIT_AGI_Alt*AGI + Scale_CRIT_INT*INT + 0
                RFLCT = 0 #TODO From EQ
                RFLX = 0 #TODO From EQ
                ADDROP = 0 #TODO From EQ
            elif CLASS == Class.Cleric.name:
                print("Stats calculated for Cleric")
                HP = Scale_HP_Lvl*LVL + Scale_HP_STR_Alt*STR + Scale_HP_STAM*STAM + 0
                MP = Scale_MP_Lvl*LVL + Scale_MP_INT*INT + 0
                PATK = Scale_ATK_STR_Alt*STR + Scale_ATK_AGI_Alt*AGI + 0
                MATK = Scale_ATK_INT*INT
                PDEF = 0 #TODO From EQ
                MDEF = 0 #TODO From EQ
                DODGE = Scale_DODGE_AGI_Alt*AGI + 0
                CRIT = Scale_CRIT_AGI_Alt*AGI + Scale_CRIT_INT*INT + 0
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

        if new:
            await insertHeroStats(self, ctx, playerID, CLASS, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP)
        else:
            await updateHeroStats(self, ctx, playerID, CLASS, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP)

    #SPAWN MOB
    global spawnMob
    async def spawnMob(self, ctx, mobLvl: int):
        print("Trying to spawn mob...")
        if mobLvl > 10 and mobLvl > 0:
            await ctx.channel.send("Wybrałeś zły poziom potwora! Wybierz poziom od 1 do 10!")
        else:
            
            class Mob:
                #Constructor
                def __init__(self, mobLvl):
                    self.mobLvl = mobLvl

                    print("Reading calc constants from the file...")
                    #HP/Lvl, MP/Lvl, PATK/LVL, MATK/LVL, PDEF/LVL, MDEF/LVL, DODGE/LVLDIFF, CRIT/LVLDIFF, RFLCT/1
                    with open("mobStatConfig.json", encoding='utf-8') as jsonFile:
                        jsonObject = json.loads(jsonFile.read())
                    print("Constants from file loaded.")

                    #General
                    Scale_HP_Lvl = jsonObject['General'][0]['weight']
                    print("HP/lvl = " + str(Scale_HP_Lvl))
                    Scale_MP_Lvl = jsonObject['General'][1]['weight']
                    print("MP/lvl = " + str(Scale_MP_Lvl))
                    Scale_PATK_Lvl = jsonObject['General'][2]['weight']
                    print("PATK/lvl = " + str(Scale_PATK_Lvl))
                    Scale_MATK_Lvl = jsonObject['General'][3]['weight']
                    print("MATK/lvl = " + str(Scale_MATK_Lvl))
                    Scale_PDEF_Lvl = jsonObject['General'][4]['weight']
                    print("PDEF/lvl = " + str(Scale_PDEF_Lvl))
                    Scale_MDEF_Lvl = jsonObject['General'][5]['weight']
                    print("MDEF/lvl = " + str(Scale_MDEF_Lvl))
                    Scale_DODGE_Lvldiff = jsonObject['General'][6]['weight']
                    print("DODGE/lvldiff = " + str(Scale_DODGE_Lvldiff))
                    Scale_CRIT_Lvldiff = jsonObject['General'][7]['weight']
                    print("CRIT/lvldiff = " + str(Scale_CRIT_Lvldiff))
                    Scale_RFLCT = jsonObject['General'][8]['weight']
                    print("RFLCT/lvldiff = " + str(Scale_RFLCT))
                    Scale_MAXPDEF = jsonObject['General'][9]['weight']
                    print("MAXPDEF = " + str(Scale_MAXPDEF))

                    self.HP = Scale_HP_Lvl * mobLvl
                    self.MP = Scale_MP_Lvl * mobLvl
                    self.PATK = Scale_PATK_Lvl * mobLvl
                    self.MATK = Scale_MATK_Lvl * mobLvl
                    self.PDEF = Scale_PDEF_Lvl * mobLvl
                    self.MDEF = Scale_MDEF_Lvl * mobLvl
                    self.DODGE = 0
                    self.CRIT = 0
                    self.RFLCT = 0
                    self.REDUC = self.PDEF / Scale_MAXPDEF #Value in %/100

                    self.ActHP = self.HP
                    self.ActMP = self.MP
                    print("Mob constructor end.")

                def getDamage(self, PATK: int, MATK: int):
                    print("Mob is getting dmg...")
                    self.ActHP -= PATK * (1 - self.REDUC)
                    print("HP left: " + str(self.ActHP))

                    #check if mob killed
                    if self.ActHP <= 0:
                        print("Mob is dead!")
                        return True
                    else:
                        print("Mob still alive!")
                        return False

            Monster = Mob(mobLvl)
            return Monster

    #SPAWN PLAYER
    global spawnPlayer
    async def spawnPlayer(self, ctx, playerID):
        print("Trying to spawn player...")
        #CHECK IF USER EXISTS IN DATABASE
        print("Checking if user exists...")
        sql=("SELECT ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(playerID)))
        check = await self.bot.pg_con.fetch(sql)
        if check:
            print("User exists!")
            check = check[0]
            print("Before hero stats reading...")
            MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP = await readHeroStatsTable(self, ctx, playerID)
            print("Hero stats read.")
            
            class Player:
                #Constructor
                def __init__(self, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP):

                    print("Reading calc constants from the file...")
                    with open("heroStatConfig.json", encoding='utf-8') as jsonFile:
                        jsonObject = json.loads(jsonFile.read())
                    print("Constants from file loaded.")
                    #General
                    Scale_MAXPDEF = jsonObject['General'][4]['weight']

                    self.HP = MaxHP
                    self.MP = MaxMP
                    self.PATK = PATK
                    self.MATK = MATK
                    self.PDEF = PDEF
                    self.MDEF = MDEF
                    self.DODGE = DODGE
                    self.CRIT = CRIT
                    self.RFLCT = RFLCT
                    self.REDUCP = self.PDEF / Scale_MAXPDEF #Value in %/100
                    self.REDUCM = self.MDEF / Scale_MAXPDEF #Value in %/100

                    self.ActHP = ActHP
                    self.ActMP = ActMP
                    print("Player constructor end.")

                def getDamage(self, PATK: int, MATK: int):
                    print("Player is getting dmg...")
                    self.ActHP -= PATK * (1 - self.REDUCP)
                    self.ActHP -= MATK * (1 - self.REDUCM)
                    print("HP left: " + str(self.ActHP))

                    #check if player killed
                    if self.ActHP <= 0:
                        print("Player is dead!")
                        return True
                    else:
                        print("Player still alive!")
                        return False

            Player = Player(MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP)
            Player.getDamage(10, 10)
            return Player

        else:
            print("Player does not exists!")


            


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
        print('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS) VALUES (\'{}\',\'{}\',\'{}\',{},{},{},{},{},{},{});'.format(str(playerID), user.name, str(playerClass.name), 0, 1, 5, 5, 5, 5, 0))
        await self.bot.pg_con.execute('INSERT INTO RPG_GENERAL (ID, NICK, CURRENT_CLASS, EXPERIENCE, LEVEL, STR, AGI, INTEL, STAM, REMPOINTS) VALUES (\'{}\',\'{}\',\'{}\',{},{},{},{},{},{},{});'.format(str(playerID), user.name, str(playerClass.name), 0, 1, 5, 5, 5, 5, 0))
        print("Database updated with general stats.")
        print("Data inserted in General RPG Database.")
        if playerClass == Class.Warrior:
            url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"
        elif playerClass == Class.Mage:
            url1="https://www.altermmo.pl/wp-content/uploads/Icon21.png"
        elif playerClass == Class.Rogue:
            url1="https://www.altermmo.pl/wp-content/uploads/Icon39.png"
        elif playerClass == Class.Cleric:
            url1="https://www.altermmo.pl/wp-content/uploads/Icon20.png"
        else:
            url1="https://www.altermmo.pl/wp-content/uploads/Icon17.png"
        embed=discord.Embed(title='Powodzenia!', url=url1, description="Świetnie! Stworzyłeś swojego  <@" + str(playerID) + ">, którym możesz zatopić się w świat fantasy AlterMMO. Przyjemnych przygód!\n\nMożesz sprawdzić profil swojej postaci wpisując **#profil**", color=0x00C1C7)
        embed.set_thumbnail(url=url1)

        botResponseMsg = await ctx.channel.send(embed=embed)

        await calcStats(self, ctx, playerID, True)

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
           DODGE REAL,
           CRIT REAL,
           RFLCT REAL,
           RFLX REAL,
           ADDROP REAL
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

    global updateStatsRPGGeneral
    async def updateStatsRPGGeneral(self, ctx, playerID, STR, AGI, INT, STAM, REMPOINTS):
        print("Trying to update Main Hero Stats in RPG General...")
        print('UPDATE RPG_GENERAL SET STR = {}, AGI = {}, INTEL = {}, STAM = {}, REMPOINTS = {} WHERE ID = \'{}\''.format(STR, AGI, INT, STAM, REMPOINTS, str(playerID)))
        await self.bot.pg_con.execute('UPDATE RPG_GENERAL SET STR = {}, AGI = {}, INTEL = {}, STAM = {}, REMPOINTS = {} WHERE ID = \'{}\''.format(STR, AGI, INT, STAM, REMPOINTS, str(playerID)))
        print("Data updated in Main Hero Stats in RPG General Database.")

    global updateExpHeroStats
    async def updateExpHeroStats(self, ctx, playerID, acqEXP):
        print("Trying to update Exp Hero Stats...")
        dbRpgRead = await self.bot.pg_con.fetch("SELECT NICK, EXPERIENCE, LEVEL, REMPOINTS FROM RPG_GENERAL WHERE ID = \'{}\';".format(str(playerID)))
        EXP = dbRpgRead[0][1]
        LVL = dbRpgRead[0][2]
        REMPOINTS = dbRpgRead[0][3]
        acqEXP = int(acqEXP)

        #Load experience table from file
        print("Reading exp constants from the file...")
        with open("expConfig.json", encoding='utf-8') as jsonFile:
            jsonObject = json.loads(jsonFile.read())
        print("Constants from file loaded.")

        #Get required exp based on lvl
        requiredExp = jsonObject['exp_required'][LVL+1]['exp']
        print("Exp required to next lvl: " + str(requiredExp))

        
        if EXP + acqEXP >= requiredExp:
            print("Lvl up.")
            LVL = LVL+1
            EXP = EXP+acqEXP - requiredExp
            REMPOINTS = REMPOINTS + 5
            print('UPDATE RPG_GENERAL SET LEVEL = {}, EXPERIENCE = {}, REMPOINTS = {} WHERE ID = \'{}\''.format(LVL, EXP, REMPOINTS, str(playerID)))
            await self.bot.pg_con.execute('UPDATE RPG_GENERAL SET LEVEL = {}, EXPERIENCE = {}, REMPOINTS = {} WHERE ID = \'{}\''.format(LVL, EXP, REMPOINTS, str(playerID)))
            
            embed=discord.Embed(title='Awans!', url='https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png', description="Gratulacje! Awansowałeś na kolejny poziom <@" + str(playerID) +">!", color=0x00C1C7)
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png')
            embed.set_footer(text='Poziom ' + str(LVL) + '!')
            botMessage = await ctx.channel.send(embed=embed)

        else:
            print("Not lvl up.")
            EXP = EXP + acqEXP
            print('UPDATE RPG_GENERAL SET EXPERIENCE = {} WHERE ID = \'{}\''.format(EXP, str(playerID)))
            await self.bot.pg_con.execute('UPDATE RPG_GENERAL SET EXPERIENCE = {} WHERE ID = \'{}\''.format(EXP, str(playerID)))


        print("Data updated in Exp Hero Stats Database.")

    global readHeroStatsTable
    async def readHeroStatsTable(self, ctx, playerID):
        #Database Reading
        print("Trying to read Hero Stats Database...")
        dbRpgStatsRead = await self.bot.pg_con.fetch("SELECT ID, CURRENT_CLASS, MaxHP, ActHP, MaxMP, ActMP, PATK, MATK, PDEF, MDEF, DODGE, CRIT, RFLCT, RFLX, ADDROP FROM RPG_HERO_STATS WHERE ID = \'{}\';".format(str(playerID)))

        if dbRpgStatsRead:
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
        else:
            embed=discord.Embed(title='Bohater nie istnieje!', url='https://www.altermmo.pl/wp-content/uploads/Icon47.png', description="Nie stworzyłeś jeszcze swojego bohatera. Możesz to zrobić wpisując komendę **#start**!", color=0x00C1C7)
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Icon47.png')
            botMessage = await ctx.channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(functions_rpg_general(bot))
