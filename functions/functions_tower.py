"""Class with all functions used for weekly tower."""

from turtle import update
from discord.ext import commands
import discord
import random
import asyncio

from datetime import datetime

import datetime

import functions_patrons
import functions_achievements
import functions_pets
import functions_boss

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsTower(commands.Cog, name="FunctionsTower"):
    """Class with all functions used for weekly tower."""
    def __init__(self, bot):
        self.bot = bot

    global create_database
    async def create_database(self, ctx):

        await self.bot.pg_con.execute("DROP TABLE IF EXISTS TOWER")
        #Creating table as per requirement
        sql ='''CREATE TABLE TOWER (
           PLAYER_ID NUMERIC PRIMARY KEY,
           TOWER_LEVEL NUMERIC,
           TIME VARCHAR(255)
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table TOWER created successfully (empty).")

    global tower_image
    async def tower_image(self, ctx, level: int):
        """Function to show death tower level."""

        e_title = f"<:AlterUp:846344753124999169> {str(level)} piętro Wieży Śmierci! <:Loot:912797849916436570>"

        image_name = "tower/" + str(level) + ".png"
        file=discord.File(image_name)

        if level == 1:
            e_descr = ('Rozpoczynasz eksplorację Wieży Śmierci. Strzeż się!'
            '\n\n*Crafterzy i Patroni mogą zdobyć kolekcjonerskie osiągnięcia!*')
        else:
            e_descr = ('Przechodzisz na kolejne piętro. Powodzenia!'
            '\n\n*Crafterzy i Patroni mogą zdobyć kolekcjonerskie osiągnięcia!*')

        e_color = 0xf00511

        #image
        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        await ctx.channel.send(file=file)
        await ctx.send(embed=embed)

    #function to Random Tower Level Hp
    global random_tower_hp
    def random_tower_hp(level):
        level = int(level)
        tower_hp = 2 * level
        return tower_hp

    global tower_fight
    async def tower_fight(self, ctx, tower_level, player=None):

        timeout = 8
        player = ctx.author

        await ctx.channel.send('Rozpoczyna się walka w Wieży Śmierci, <@' + str(player.id) + '>! Każde piętro to coraz trudniejszy boss do pokonania <:REEeee:790963160495947856>! Wpisz pojawiające się słowa tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez spacji! Przygotuj się!')

        #Load pet skills
        pet_skills_dict = {}
        for retries in range(0,3):
            try:
                pet_skill = await functions_pets.get_pet_skills(self, player.id)
                pet_skills_dict[player.id] = pet_skill
                break
            except:
                await ctx.channel.send(f"Błąd bazy danych <:Sadge:936907659142111273>... Próbuję ponownie - {retries}")
        else:
            return False

        await asyncio.sleep(8)

        #Start time counting
        startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

        await ctx.channel.send('Uwaga!!! 3...')
        await asyncio.sleep(1)
        await ctx.channel.send('... 2...')
        await asyncio.sleep(1)
        await ctx.channel.send('... 1...')
        await asyncio.sleep(1)

        #Random the message and requested action
        requestedAction = [("unik", "atak", "paruj", "skok", "bieg", "turlaj", "czaruj", "blok", "skacz", "akcja", "krzyk", "ruch", "posuw", "impet", "zryw"), ("Potwór szarżuje na Ciebie! Wpisz **U N I K**", "Potwór zawahał się! Teraz! Wpisz **A T A K**", "Potwór atakuje, nie masz miejsca na ucieczkę, wpisz **P A R U J**", 
        "Potwór próbuje ataku w nogi, wpisz **S K O K**", "Potwór szykuje potężny atak o szerokim zasięgu, wpisz **B I E G**", "Potwór atakuje w powietrzu, wpisz **T U R L A J**", "Potwór rzuca klątwę, wpisz **C Z A R U J**", "Potwór atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K**","Potwór próbuje ataku w nogi, wpisz **S K A C Z**","Potwór szarżuje na Ciebie, zrób coś, wpisz **A K C J A**", "Nie masz pojęcia co robić, wpisz **K R Z Y K**", "Musisz zrobić cokolwiek, wpisz **R U C H**", "Potwór rzuca głazem w Twoją stronę, wpisz **P O S U W**", "Dostrzegasz szansę na uderzenie, wpisz **I M P E T**", "Pojawiła się chwila zawachania potwora, wpisz **Z R Y W**")]

        bossHP = random_tower_hp(tower_level)
        bossHP = int(bossHP * (1 - (pet_skills_dict[player.id]["LOWHP_PERC"]/100)))
        print("Wylosowane HP bossa: " + str(bossHP))
        iterator = 0

        #Define check function
        channel = ctx.channel
        def check(ctx):
            def inner(msg):
                return (msg.channel == channel) and (msg.author is player)
            return inner

        #Start time counting
        startTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

        #Start whole fight
        while True: #start boss turn
            iterator += 1

            choosenAction = random.randint(0,len(requestedAction[0])-1)
            boss_hunter = player

            try:
                if not random.random()*100 < pet_skills_dict[boss_hunter.id]["REPLACE_PERC"]:

                    #Send proper action request on chat
                    await ctx.channel.send(str(iterator) + ". **"  + str(boss_hunter) + "**: " + requestedAction[1][choosenAction])

                    #Longer timeout for the first action
                    if iterator == 1:
                        cmdTimeout = 15
                    else:
                        #Timeout depends on boss rarity
                        print("Tower level before timeout calc: " + str(tower_level))
                        cmdTimeout = float(8/tower_level)
                        cmdTimeout = cmdTimeout * (100 + float(pet_skills_dict[boss_hunter.id]["SLOW_PERC"]))/100
                        print(cmdTimeout)
                    msg = await self.bot.wait_for('message', check=check(ctx), timeout=cmdTimeout)
                    response = str(msg.content)
                else:
                    response = requestedAction[0][choosenAction]
                    
                    #Send proper action request on chat
                    msg = await ctx.channel.send("~~" + str(iterator) + ". " +
                                                str(boss_hunter) + ": " +
                                            requestedAction[1][choosenAction] +
                                            "~~ Twój towarzysz wyprowadza atak!")
                    msg.author = boss_hunter

                if response.lower() == requestedAction[0][choosenAction] and msg.author == boss_hunter:

                    # Crit from pet
                    if random.random()*100 < pet_skills_dict[boss_hunter.id]["CRIT_PERC"]:
                        iterator += 1

                    #Boss killed?
                    if iterator >= bossHP:

                        await ctx.channel.send('Brawo <@' + str(player.id) + '> <:ok:990161663053422592> Piętro zaliczone!')

                        #Time record
                        endTime = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                        recordTime = endTime - startTime
                        recordTurnTime = recordTime/bossHP
                        await ctx.channel.send('Zabicie potwora zajęło: ' + str(recordTime).lstrip('0:00:') + ' sekundy! Jedna tura zajęła średnio ' + str(recordTurnTime).lstrip('0:00:') + ' sekundy!')

                        #Randomize Loot
                        drop_boost = float(pet_skills_dict[boss_hunter.id]["DROP_PERC"])
                        if tower_level in [1,2]:
                            rarity = 0
                        elif tower_level in [3,4]:
                            rarity = 1
                        elif tower_level in [5,6,7]:
                            rarity = 2
                        elif tower_level in [8,9,10]:
                            rarity = 4
                        else:
                            rarity = 2
                        dropLoot = await functions_boss.randLoot(self, ctx, rarity, boss_hunter, drop_boost)
                        await update_tower_level(self, ctx, boss_hunter, tower_level, recordTime)
                        break
                    else:
                        print("Good command.")
                else:
                    if not random.random()*100 < pet_skills_dict[boss_hunter.id]["DEF_PERC"]:
                        await ctx.channel.send('Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie w przyszłym tygodniu! <:RIP:912797982917816341> Twój rekord został zapisany!')
                        logChannel = self.bot.get_channel(881090112576962560)

                        return False
                    else:
                        await ctx.channel.send('Pomyliłeś się, ale Twój towarzysz Cię chroni!')

            except asyncio.TimeoutError:
                if not random.random()*100 < pet_skills_dict[boss_hunter.id]["DEF_PERC"]:
                    await ctx.channel.send('Niestety nie zdążyłeś! <:Bedge:970576892874854400>  Spróbuj ponownie w przyszłym tygodniu! <:RIP:912797982917816341> Twój rekord został zapisany!')
                    logChannel = self.bot.get_channel(881090112576962560)

                    return False
                else:
                    await ctx.channel.send('Nie zdążyłeś, ale Twój towarzysz Cię chroni!')

        return True

    global update_tower_level
    async def update_tower_level(self, ctx, player: discord.member, level: int,
                                record_time):

        player_id = int(player.id)

        sql = f"SELECT TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = {player_id};"

        string_record = str(record_time).lstrip(' ')

        print(string_record)

        for retries in range(0,3):
            try:
                tower_level = await self.bot.pg_con.fetch(sql)
                break
            except:
                tower_level = None
        else:
            pass

        if tower_level:
            print(f"Tower level: {tower_level}")
            if tower_level[0][0] < level:
                print("Player exists and has finished death tower, we can update dabatase..")
                sql = f"""UPDATE TOWER SET TOWER_LEVEL = {level}, TIME = \'{string_record}\'
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
            elif tower_level[0][0] == level and datetime.datetime.strptime(tower_level[0][1], "%H:%M:%S.%f") > datetime.datetime.strptime(str(record_time), "%H:%M:%S.%f"):
                print("Player exists and has finished death tower, but player did it faster, so we can update dabatase..")
                sql = f"""UPDATE TOWER SET TOWER_LEVEL = {level}, TIME = \'{string_record}\'
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
        else:
            print("Player doest not exists in TOWER database, we can insert dabatase..")
            await new_record_tower(self, player_id, level, string_record)

        if level == 3 and functions_patrons.check_if_patron(self, ctx, player):
            await functions_achievements.set_achiev_role(self, ctx, player, 1151427369886826496)
        elif level == 6 and functions_patrons.check_if_patron(self, ctx, player):
            await functions_achievements.set_achiev_role(self, ctx, player, 1151427619741511710)
        elif level == 10 and functions_patrons.check_if_patron(self, ctx, player):
            await functions_achievements.set_achiev_role(self, ctx, player, 1151427790973964328)

        return True
    
    #Check tower ranking
    global tower_ranking
    async def tower_ranking(self, ctx):
        #Database Reading
        db_ranking_tower = await self.bot.pg_con.fetch("SELECT PLAYER_ID, TOWER_LEVEL, TIME FROM TOWER ORDER BY TOWER_LEVEL DESC, TIME LIMIT 10")

        x = 1
        ranking_string = ""
        for person in db_ranking_tower:
            user = self.bot.get_user(int(person[0]))
            ranking_string += f"{x}. **{user.name}** - Poziom Wieży Śmierci: {person[1]} - Czas: {person[2]}.\n"
            x+=1

        #Embed create
        emb=discord.Embed(title='Eksploratorzy Wieży Śmierci!', url='https://www.altermmo.pl/wp-content/uploads/alter0000_Death_tower_grim_dark_very_high._Fantasy_realistic._54471f3c-8193-47c9-aade-d1b38a460922-1.png', description=ranking_string, color=0xff0019)
        emb.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/alter0000_Death_tower_grim_dark_very_high._Fantasy_realistic._54471f3c-8193-47c9-aade-d1b38a460922-1.png')
        emb.set_footer(text='Gratulacje dla nieustraszonych!')
        await ctx.send(embed=emb)

    #Check tower ranking
    global check_personal_tower
    async def check_personal_tower(self, ctx):
        #Database Reading
        db_ranking_tower = await self.bot.pg_con.fetch(f"SELECT PLAYER_ID, TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = {ctx.author.id}")

        if db_ranking_tower:
            user = self.bot.get_user(int(db_ranking_tower[0][0]))
            ranking_string = f"<@{user.id}> udało Ci się ukończyć Wieżę Śmierci na poziomie **{db_ranking_tower[0][1]}** w czasie **{db_ranking_tower[0][2]}**.\n"

            #Embed create
            emb=discord.Embed(title='Personalny rekord Wieży Śmierci!', url='https://www.altermmo.pl/wp-content/uploads/alter0000_Death_tower_grim_dark_very_high._Fantasy_realistic._54471f3c-8193-47c9-aade-d1b38a460922-1.png', description=ranking_string, color=0xff0019)
            emb.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/alter0000_Death_tower_grim_dark_very_high._Fantasy_realistic._54471f3c-8193-47c9-aade-d1b38a460922-1.png')
            emb.set_footer(text='Gratulacje!')
            await ctx.send(embed=emb)
        else:
            ranking_string = "Jeszcze nie udało Ci się zeksplorować Wieży Śmierci.\n"

            #Embed create
            emb=discord.Embed(title='Personalny rekord Wieży Śmierci!', url='https://www.altermmo.pl/wp-content/uploads/alter0000_Death_tower_grim_dark_very_high._Fantasy_realistic._54471f3c-8193-47c9-aade-d1b38a460922-1.png', description=ranking_string, color=0xff0019)
            emb.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/alter0000_Death_tower_grim_dark_very_high._Fantasy_realistic._54471f3c-8193-47c9-aade-d1b38a460922-1.png')
            emb.set_footer(text='Gratulacje!')
            await ctx.send(embed=emb)
    
    #function to save weekly tower to file
    global save_weekly_tower_to_file
    def save_weekly_tower_to_file (player_id):
        player_id = str(player_id)

        with open('weekly_tower_cd.txt', 'r') as r:
            read_lines = r.readlines()
        r.close()
        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        if player_id not in new_list:
            with open('weekly_tower_cd.txt', 'a') as f:
                f.write(str(player_id) + '\n')

    #function to read weekly tower from file
    global load_weekly_tower_from_file
    def load_weekly_tower_from_file(player_id):
        player_id = str(player_id)

        with open('weekly_tower_cd.txt', 'r') as r:
            read_lines = r.readlines()

        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        return player_id in new_list

    #function to read daily hunter from file
    global clear_weekly_tower_file
    async def clear_weekly_tower_file(self):

        open('weekly_tower_cd.txt', 'w').close()
        logChannel = self.bot.get_channel(881090112576962560)
        await logChannel.send("Zresetowano cooldown na weekly tower.")


async def new_record_tower(self, player_id: int, level: int, record_time):
    """New record of user in tower database."""

    string_record = str(record_time).lstrip(' ')

    sql=f"""INSERT INTO TOWER (PLAYER_ID, TOWER_LEVEL, TIME)
    VALUES ({player_id},{level},\'{string_record}\');"""

    print(sql)

    for retries in range(0,3):
        try:
            await self.bot.pg_con.fetch(sql)
            break
        except:
            pass
    else:
        return False

def setup(bot):
    """Load the FunctionsTower cog."""
    bot.add_cog(FunctionsTower(bot))
