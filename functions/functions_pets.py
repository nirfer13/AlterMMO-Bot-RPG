"""Class with all functions used for pets."""

import json
from symbol import namedexpr_test
import discord
import random
import asyncio
from enum import Enum
from discord.ext import commands

#Import Globals
from globals.globalvariables import DebugMode

class PetType(str, Enum):
    """Class with all pets types."""

    BEAR = 'Bear'
    BOAR = 'Boar'
    CAT = 'Cat'
    RABBIT = 'Rabbit'
    SHEEP = 'Sheep'
    DRAGON = 'Dragon'
    PHOENIX = 'Phoenix'
    UNICORN = 'Unicorn'
    SNAKE = 'Snake'
    ANIME_GIRL = 'AnimeGirl'

class FunctionsPets(commands.Cog, name="FunctionsPets"):
    """Class with all functions used for pets."""
    def __init__(self, bot):
        self.bot = bot

# ==================================== FUNCTIONS FOR DATABASES =================================

    global create_pet_owners_table
    async def create_pet_owners_table(self):
        "Droping PetOwnersTable if exists"

        await self.bot.pg_con.execute("DROP TABLE IF EXISTS PETOWNER")
        #Creating table as per requirement
        sql ='''CREATE TABLE PETOWNER(
           PLAYER_ID NUMERIC,
           PET_ID NUMERIC,
           PET_OWNED BOOLEAN,
           REROLL_SCROLL NUMERIC,
           REROLL_SCROLL_SHARD NUMERIC
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table created successfully.")
        print(f"""INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED)
             VALUES ({0},{0},{True});""")
        await self.bot.pg_con.execute(f"""INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED)
             VALUES ({0},{0},{True});""")
        print("Data inserted into Database PETOWNER.")

    global create_pets_table
    async def create_pets_table(self):
        '''Droping Pets Table if exists'''

        await self.bot.pg_con.execute("DROP TABLE IF EXISTS PETS")
        #Creating table as per requirement
        sql ='''CREATE TABLE PETS(
           PET_ID NUMERIC,
           PET_NAME VARCHAR(255),
           PET_LVL NUMERIC,
           PET_SKILLS NUMERIC,
           QUALITY VARCHAR(255),
           SHINY BOOLEAN,
           TYPE VARCHAR(255),
           VARIANT VARCHAR(255),
           CRIT_PERC NUMERIC,
           REPLACE_PERC NUMERIC,
           DEF_PERC NUMERIC,
           DROP_PERC NUMERIC,
           LOWHP_PERC NUMERIC,
           SLOW_PERC NUMERIC,
           INIT_PERC NUMERIC,
           DETECTION BOOLEAN
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table PETS created successfully.")
        print(f"""INSERT INTO PETS
               (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY,
              TYPE, VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC,
              LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION) VALUES ({0},\'{'Default'}\',{0},{0},
              \'{'Standard'}\',{True},\'{'Type'}\',\'{0}\',{1},{2},{3},{4},{5},{6},{7},{False});""")
        await self.bot.pg_con.execute(f"""INSERT INTO PETS
               (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY,
              TYPE, VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC,
              LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION) VALUES ({0},\'{'Default'}\',{0},{0},
              \'{'Standard'}\',{True},\'{'Type'}\',\'{0}\',{1},{2},{3},{4},{5},{6},{7},{False});""")

    global show_pet
    async def show_pet(self, ctx, player):
        """Show author's pet and his scrolls."""

        print("Checking if user has a pet...")
        sql = f"SELECT PET_ID, REROLL_SCROLL, REROLL_SCROLL_SHARD, REBIRTH_STONES, MIRRORS FROM PETOWNER WHERE PLAYER_ID = {ctx.author.id};"
        pet_exists = await self.bot.pg_con.fetch(sql)
        print(pet_exists)
        reroll_scroll = pet_exists[0][1]
        reroll_shard = pet_exists[0][2]
        rebirt_stones = pet_exists[0][3]
        mirrors = pet_exists[0][4]

        if pet_exists:
            if pet_exists[0][0] > 0:
                # Convert shards to full scrolls
                if reroll_shard // 10 > 0:
                    reroll_scroll += reroll_shard // 10
                    sql = f"""UPDATE PETOWNER SET REROLL_SCROLL = {reroll_scroll}
                    WHERE PLAYER_ID = {player.id};"""
                    await self.bot.pg_con.fetch(sql)

                    reroll_shard = reroll_shard % 10
                    sql = f"""UPDATE PETOWNER SET REROLL_SCROLL_SHARD = {reroll_shard}
                    WHERE PLAYER_ID = {player.id};"""
                    await self.bot.pg_con.fetch(sql)
                    print(f"Update reroll scrolls {reroll_scroll} and shards {reroll_shard}")

                # Add info about items
                scroll_desc = (f"\n**<:RPGWarrior:995576809666134046> PRZEDMIOTY:**\nZwoje odrodzenia: {reroll_scroll}\n" +
                f"Fragmenty zwojów: {reroll_shard}\nKamienie olśnienia: {rebirt_stones}" +
                f"\nLustra wizualiów: {mirrors}")

                sql = f"""SELECT PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY,
                TYPE, VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC,
                LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION FROM PETS
                WHERE PET_ID = {pet_exists[0][0]};"""
                pet_data = await self.bot.pg_con.fetch(sql)
                print(pet_data)

                #Check if pet is shiny
                if pet_data[0][5]:
                    color = 0xffdd00
                    add_desc = "\n\n*Wygląda na bardzo rzadkie. Miałeś niesamowite szczęście.*"
                else:
                    color = 0xfffffc
                    add_desc = ""

                # Check if pet is level 0
                if pet_data[0][2] == 0:

                    #Check if pet has name
                    if pet_data[0][1] != "Towarzysz":
                        title = f'Jajko {pet_data[0][1]}'
                    else:
                        title = 'Jajko'

                    #Check if pet is premium
                    if pet_data[0][4] == "Standard":
                        path = f"eggs/standard/{pet_data[0][7]}.png"
                    elif pet_data[0][4] == "Premium":
                        path = f"eggs/premium/{pet_data[0][6]}/{pet_data[0][7]}.png"

                    embed = discord.Embed(title=title,
                                        description="Oto Twój towarzysz, <@" + str(player.id) + ">. Opiekuj się nim, a być może kiedyś coś z niego wyrośnie...\n" + scroll_desc + add_desc,
                                        color=color)
                    file = discord.File(path, filename=f"{pet_data[0][7]}.png")
                    embed.set_footer(text = "Na zawsze ponosisz odpowiedzialność za to, co oswoiłeś.")
                    embed.set_image(url=f"attachment://{pet_data[0][7]}.png")
                    await ctx.send(file=file, embed=embed)
                else:
                    #Check if pet has name
                    if pet_data[0][6] == "Bear":
                        polish_type = "Niedźwiedź"
                    elif pet_data[0][6] == "Cat":
                        polish_type = "Kot"
                    elif pet_data[0][6] == "Boar":
                        polish_type = "Dzik"
                    elif pet_data[0][6] == "Dragon":
                        polish_type = "Smok"
                    elif pet_data[0][6] == "Phoenix":
                        polish_type = "Feniks"
                    elif pet_data[0][6] == "Rabbit":
                        polish_type = "Królik"
                    elif pet_data[0][6] == "Sheep":
                        polish_type = "Owca"
                    elif pet_data[0][6] == "Unicorn":
                        polish_type = "Jednorożec"
                    elif pet_data[0][6] == "Snake":
                        polish_type = "Wąż"
                    elif pet_data[0][6] == "AnimeGirl":
                        polish_type = "Dziewczynka Anime"
                    else:
                        polish_type = ""
                    if pet_data[0][1] != "Towarzysz":
                        title = f'{polish_type} {pet_data[0][1]}'
                    else:
                        title = f'{polish_type}'

                    #Check if pet is premium
                    if pet_data[0][4] == "Standard":
                        path = f"eggs/standard/{pet_data[0][7]}.png"
                    elif pet_data[0][4] == "Premium":
                        path = f"eggs/premium/{pet_data[0][6]}/{pet_data[0][7]}.png"

                    image_number = pow(10,(int(pet_data[0][2])-1))
                    if image_number == 1:
                        image_number = 0
                    image_number += int(pet_data[0][7])
                    path = f"pets/{pet_data[0][6]}/{image_number}.png"

                    skill_desc = "<:RPGAddStat:995642835531472956> **STATYSTYKI:**\n"
                    skill_desc += f"Poziom: {pet_data[0][2]}\n"
                    if pet_data[0][3] == 0:
                        talent = "Miernota"
                    elif pet_data[0][3] == 1:
                        talent = "Nowicjusz"
                    elif pet_data[0][3] == 2:
                        talent = "Uczeń"
                    elif pet_data[0][3] == 3:
                        talent = "Przeciętny"
                    elif pet_data[0][3] == 4:
                        talent = "Ekspert"
                    elif pet_data[0][3] == 5:
                        talent = "Mistrz"
                    elif pet_data[0][3] == 6:
                        talent = "Oświecony"
                    elif pet_data[0][3] == 7:
                        talent = "Transcendentny"
                    elif pet_data[0][3] == 8:
                        talent = "Pozaziemski"
                    elif pet_data[0][3] == 9:
                        talent = "Boski"
                    else:
                        talent = "Transcendentny"
                    skill_desc += f"Talent: {talent}\n"
                    skill_desc += f"Wygląd: {pet_data[0][4]}\n\n"
                    if pet_data[0][9] > 0:
                        skill_desc += f"Szansa na zastąpienie ataku: {pet_data[0][9]} %\n"
                    if pet_data[0][8] > 0:
                        skill_desc += f"Szansa na krytyczne uderzenie: {pet_data[0][8]} %\n"
                    if pet_data[0][10] > 0:
                        skill_desc += f"Szansa na zablokowanie ataku: {pet_data[0][10]} %\n"
                    if pet_data[0][12] > 0:
                        skill_desc += f"Zmniejszenie życia przeciwnika: {pet_data[0][12]} %\n"
                    if pet_data[0][13] > 0:
                        skill_desc += f"Spowolnienie przeciwnika: {pet_data[0][13]} %\n"
                    if pet_data[0][11] > 0:
                        skill_desc += f"Rzadszy drop o: {pet_data[0][11]} %\n"
                    if pet_data[0][14] > 0:
                        skill_desc += f"Szansa na inicjację bossa: {pet_data[0][14]} %\n"
                    if pet_data[0][15] is True:
                        skill_desc += f"Wyczuwanie bossa: Tak\n"

                    embed = discord.Embed(title=title,
                                        description="Oto Twój towarzysz, <@" + str(player.id) + ">.\n\n" + skill_desc + scroll_desc + add_desc,
                                        color=color)
                    file = discord.File(path, filename=f"{image_number}.png")
                    embed.set_footer(text = "Na zawsze ponosisz odpowiedzialność za to, co oswoiłeś.")
                    embed.set_image(url=f"attachment://{image_number}.png")
                    await ctx.send(file=file, embed=embed)

            else:
                await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(player.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")
        else:
            await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(player.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")

    global generate_pet_egg
    async def generate_pet_egg(self, ctx, player):
        """Generate first pet as an egg."""

        crafter = discord.utils.get(ctx.guild.roles, id=687185998550925312)
        if crafter in player.roles:
            pets_list = [PetType.BEAR, PetType.BOAR, PetType.CAT,
                         PetType.RABBIT, PetType.SHEEP, PetType.DRAGON,
                         PetType.PHOENIX, PetType.UNICORN, PetType.SNAKE,
                         PetType.ANIME_GIRL]
        else:
            pets_list = [PetType.BEAR, PetType.BOAR, PetType.CAT,
                         PetType.RABBIT, PetType.SHEEP]
            
        print(pets_list)

        pet = random.choice(pets_list)

        percentage = random.randint(0,100)
        shiny = percentage >= 95

        if pet in [PetType.DRAGON, PetType.PHOENIX, PetType.UNICORN, PetType.SNAKE,
                   PetType.ANIME_GIRL]:
            quality = "Premium"
            variant = random.randint(0,1)
        else:
            quality = "Standard"
            variant = random.randint(0,10)

        db_read = await self.bot.pg_con.fetch("SELECT PET_ID FROM PETS ORDER BY PET_ID DESC LIMIT 1")

        print(f"Read from databse {db_read}")

        pet_config = {}
        pet_config["PET_ID"] = 1 + int(db_read[0][0])
        pet_config["PET_NAME"] = "Towarzysz"
        pet_config["PET_LVL"] = 0
        pet_config["PET_SKILLS"] = 0
        pet_config["QUALITY"] = quality
        pet_config["SHINY"] = shiny
        pet_config["TYPE"] = pet
        pet_config["VARIANT"] = variant

        pet_config["CRIT_PERC"] = 0
        pet_config["REPLACE_PERC"] = 0
        pet_config["DEF_PERC"] = 0
        pet_config["DROP_PERC"] = 0
        pet_config["LOWHP_PERC"] = 0
        pet_config["SLOW_PERC"] = 0
        pet_config["INIT_PERC"] = 0
        pet_config["DETECTION"] = False

        print(pet_config)

        await self.bot.pg_con.execute(f"""INSERT INTO PETS
            (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY,
            TYPE, VARIANT,
            CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC,
            LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION) VALUES 
            ({pet_config["PET_ID"]},\'{pet_config["PET_NAME"]}\',
            {pet_config["PET_LVL"]},{pet_config["PET_SKILLS"]},
            \'{pet_config["QUALITY"]}\',{pet_config["SHINY"]},
            \'{pet_config["TYPE"]}\',\'{pet_config["VARIANT"]}\',
            {pet_config["CRIT_PERC"]},{pet_config["REPLACE_PERC"]},
            {pet_config["DEF_PERC"]},{pet_config["DROP_PERC"]},
            {pet_config["LOWHP_PERC"]},{pet_config["SLOW_PERC"]},
            {pet_config["INIT_PERC"]},{pet_config["DETECTION"]});""")

        print(f"Pet egg generated {pet}, shiny: {shiny}.")

        return pet_config["PET_ID"]
    
    global reroll_pet
    async def reroll_pet(self, ctx, player):
        """Reroll pet stats"""

        print("Checking if user has a pet...")
        sql = f"SELECT PET_ID, REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = {player.id};"
        pet_exists = await self.bot.pg_con.fetch(sql)
        pet_id = pet_exists[0][0]
        reroll_scroll = pet_exists[0][1]

        if pet_exists:
            if pet_exists[0][0] > 0:
                print("Pet exists, so we can try to reroll.")
                if reroll_scroll > 0:

                    # Check if reroll pet is confirmed
                    def check_reroll(author):
                        def inner_check(message):
                            if message.author == author: #and message.author not in confirmPlayerList:
                                if message.content.lower() == "$potwierdzam":
                                    print("Reroll accepted: player exists!")
                                    return True
                            else:
                                print("Wrong person or wrong message!")
                                return False
                        return inner_check

                    await ctx.channel.send("Czy jesteś pewien, że chcesz przelosować statystyki swojego towarzysza <@" + str(player.id) + ">? <:Hmm:984767035617730620> Wpisz **$potwierdzam**.")
                    try:
                        confirm_cmd = await self.bot.wait_for('message', timeout=15,
                                                            check=check_reroll(player))
                        await confirm_cmd.add_reaction("<:PepoG:790963160528977980>")

                        sql = f"SELECT PET_SKILLS, SHINY FROM PETS WHERE PET_ID = {pet_id};"
                        pet_data = await self.bot.pg_con.fetch(sql)
                        pet_skills = pet_data[0][0]
                        pet_shiny = pet_data[0][1]
                        pet_crit_perc = 0
                        pet_replace_perc = 0
                        pet_def_perc = 0
                        pet_drop_perc = 0
                        pet_lowhp_perc = 0
                        pet_slow_perc = 0
                        pet_init_perc = 0
                        pet_detection = False

                        skill_list = {"CRIT_PERC": pet_crit_perc, "REPLACE_PERC": pet_replace_perc,
                                    "DEF_PERC": pet_def_perc, "DROP_PERC": pet_drop_perc,
                                    "LOWHP_PERC": pet_lowhp_perc, "SLOW_PERC": pet_slow_perc,
                                    "INIT_PERC": pet_init_perc, "DETECTION": pet_detection}

                        pet_skills = int(pet_skills)
                        for i in range(pet_skills):
                            print(i)
                            while True:
                                skill_name, skill_value = random.choice(list(skill_list.items()))
                                if skill_name == "DETECTION" and pet_detection:
                                    pass
                                else:
                                    if skill_name == "INIT_PERC":
                                        if pet_shiny:
                                            add_skill_value = random.randint(5, 10)
                                        else:
                                            add_skill_value = random.randint(5, 8)
                                    elif skill_name == "SLOW_PERC":
                                        if pet_shiny:
                                            add_skill_value = random.randint(5, 10)
                                        else:
                                            add_skill_value = random.randint(5, 8)
                                    else:
                                        if pet_shiny:
                                            add_skill_value = random.randint(3, 7)
                                        else:
                                            add_skill_value = random.randint(3, 5)

                                    if skill_name == "DETECTION":
                                        skill_list[skill_name] = True
                                    else:
                                        skill_list[skill_name] += add_skill_value

                                    break

                        await self.bot.pg_con.execute(f"""UPDATE PETS SET
                        CRIT_PERC={skill_list["CRIT_PERC"]},
                        REPLACE_PERC={skill_list["REPLACE_PERC"]},
                        DEF_PERC={skill_list["DEF_PERC"]},
                        DROP_PERC={skill_list["DROP_PERC"]},
                        LOWHP_PERC={skill_list["LOWHP_PERC"]},
                        SLOW_PERC={skill_list["SLOW_PERC"]},
                        INIT_PERC={skill_list["INIT_PERC"]},
                        DETECTION={skill_list["DETECTION"]} WHERE PET_ID = {pet_id};""")

                        sql = f"""UPDATE PETOWNER SET REROLL_SCROLL = {reroll_scroll-1}
                        WHERE PLAYER_ID = {player.id};"""
                        await self.bot.pg_con.fetch(sql)

                        await show_pet(self, ctx, player)

                        return True
                    except asyncio.TimeoutError:
                        await ctx.channel.send("*Twój towarzysz spogląda na Ciebie niepewnie.*")
                else:
                    await ctx.channel.send("Nie masz żadnych zwojów odrodzenia <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Wpisz **$towarzysz**, żeby sprawdzić ich ilość. Zwoje możesz zdobyć na polowaniu lub po zabiciu bossów.")
                    return False
            else:
                await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")
                return False
        else:
            await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")
            return False

    global assign_pet
    async def assign_pet(self, ctx, player):
        """Assigning new pet to player in database, but first check if it is possible."""

        player_id = int(player.id)

        sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        player_exists = await self.bot.pg_con.fetch(sql)

        if player_exists:
            if player_exists[0][0] > 0:
                print("Player exists in PETOWNER database, but it already has pet.")
                return False
            else:
                pet_id = await generate_pet_egg(self, ctx, player)
                print("Player exists in PETOWNER database and he has not pet. Assigning...")
                sql = f"UPDATE PETOWNER SET PET_ID = {pet_id} WHERE PLAYER_ID = {player_id};"
                await self.bot.pg_con.fetch(sql)
                return True
        else:
            print("Player does not exist in PETOWNER database, so creating new record.")
            pet_id = await generate_pet_egg(self, ctx, player)
            await new_record_petowners(self, player_id, pet_id, True, 0, 0, 0, 0)
            return True

        # Nothing happened.
        return False

    global store_pet
    async def store_pet(self, ctx, slot):
        """Store a pet to free space in database, but first check if it is possible."""

        player_id = int(ctx.author.id)
        slot = int(slot)

        sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        pet_exists = await self.bot.pg_con.fetch(sql)

        if pet_exists:
            if pet_exists[0][0] > 0:
                print("Player exists in PETOWNER database and it already has pet.")
                if slot == 1:
                    slot_name = "PET_ID_ALT1"
                elif slot == 2:
                    slot_name = "PET_ID_ALT2"
                else:
                    await ctx.channel.send("Każdy gracz ma tylko dwa miejsca na przechowanie slotów - 1 oraz 2, <@" + str(ctx.author.id) + ">! Wpisz np. **$schowajtowarzysza 1**.")

                sql = f"SELECT {slot_name} FROM PETOWNER WHERE PLAYER_ID = {player_id};"
                stored_pet = await self.bot.pg_con.fetch(sql)

                if stored_pet[0][0] > 0:
                    sql = (f"""UPDATE PETOWNER SET {slot_name} = {pet_exists[0][0]},
                    PET_ID = {stored_pet[0][0]} WHERE PLAYER_ID = {player_id};""")
                    await self.bot.pg_con.fetch(sql)
                else:
                    sql = (f"""UPDATE PETOWNER SET {slot_name} = {pet_exists[0][0]},
                    PET_ID = {0} WHERE PLAYER_ID = {player_id};""")
                    await self.bot.pg_con.fetch(sql)

                await ctx.channel.send("Towarzysz został ulokowany w stajni <:peepoBlush:984769061340737586>")

                return True
            else:
                print("Player exists in PETOWNER database and he has not pet. Assigning...")
                await ctx.channel.send("Niestety nie posiadasz aktualnie żadnego peta, <@" + str(ctx.author.id) + ">. Możesz sprawdzić swoją stajnie wpisując **$stajnia**.")
                return False
        else:
            print("Player does not exist in PETOWNER database, so creating new record.")
            await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")
            return False

        # Nothing happened.
        return False
    
    global unstore_pet
    async def unstore_pet(self, ctx, slot):
        """Take a pet from a space in database, but first check if it is possible."""

        player_id = int(ctx.author.id)
        slot = int(slot)

        if slot == 1:
            slot_name = "PET_ID_ALT1"
        elif slot == 2:
            slot_name = "PET_ID_ALT2"
        else:
            await ctx.channel.send("Każdy gracz ma tylko dwa miejsca na przechowanie slotów - 1 oraz 2, <@" + str(ctx.author.id) + ">! Wpisz np. **$wyciagnijtowarzysza 1**.")
            return False

        sql = f"SELECT {slot_name } FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        stored_pet = await self.bot.pg_con.fetch(sql)

        if stored_pet:
            if stored_pet[0][0] > 0:
                print("Pet exists in PETOWNER database.")

                sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {player_id};"
                current_pet = await self.bot.pg_con.fetch(sql)

                if current_pet[0][0] > 0:
                    sql = (f"""UPDATE PETOWNER SET {slot_name} = {current_pet[0][0]},
                    PET_ID = {stored_pet[0][0]} WHERE PLAYER_ID = {player_id};""")
                    await self.bot.pg_con.fetch(sql)
                    await ctx.channel.send("Nowy towarzysz został wyciągnięty ze stajni, a stary został w niej schowany <:peepoBlush:984769061340737586>")
                    return True
                else:
                    sql = (f"""UPDATE PETOWNER SET {slot_name} = {0},
                    PET_ID = {stored_pet[0][0]} WHERE PLAYER_ID = {player_id};""")
                    await self.bot.pg_con.fetch(sql)
                    await ctx.channel.send("Towarzysz został wyciągnięty ze stajni <:peepoBlush:984769061340737586>")
                    return True
            else:
                print("Stored pet does not exist...")
                await ctx.channel.send("W stajni nie ma towarzysza na podanym miejscu. Sprawdź stajnie wpisując **$stajnia**.")
                return False
        else:
            print("Player does not exist in PETOWNER database, so creating new record.")
            await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")
            return False

        # Nothing happened.
        return False
    
    global check_stable
    async def check_stable(self, ctx):
        """Check all pets in stable"""

        player_id = int(ctx.author.id)

        sql = f"SELECT PET_ID, PET_ID_ALT1, PET_ID_ALT2 FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        pets = await self.bot.pg_con.fetch(sql)

        if pets:
            if pets[0][0] > 0:
                # PET 0
                print("Main pet exists in PETOWNER database.")
                sql = f"SELECT PET_NAME, PET_LVL, TYPE FROM PETS WHERE PET_ID = {pets[0][0]};"
                pet0 = await self.bot.pg_con.fetch(sql)

                #Check if pet has name
                if pet0[0][2] == "Bear":
                    polish_type = "Niedźwiedź"
                elif pet0[0][2] == "Cat":
                    polish_type = "Kot"
                elif pet0[0][2] == "Boar":
                    polish_type = "Dzik"
                elif pet0[0][2] == "Dragon":
                    polish_type = "Smok"
                elif pet0[0][2] == "Phoenix":
                    polish_type = "Feniks"
                elif pet0[0][2] == "Rabbit":
                    polish_type = "Królik"
                elif pet0[0][2] == "Sheep":
                    polish_type = "Owca"
                elif pet0[0][2] == "Unicorn":
                    polish_type = "Jednorożec"
                elif pet0[0][2] == "Snake":
                    polish_type = "Wąż"
                elif pet0[0][2] == "AnimeGirl":
                    polish_type = "Dziewczynka Anime"
                else:
                    polish_type = ""

                if pet0[0][1] == 0:
                    polish_type = "Jajko"

                pet1_desc = f"Obecnie posiadany pet: {polish_type} {pet0[0][0]} ({pet0[0][1]} LVL).\n"
            else:
                pet1_desc = "Obecnie posiadany pet: Brak.\n"

            if pets[0][1] > 0:
                # PET 1
                print("Alt pet 1 exists in PETOWNER database.")
                sql = f"SELECT PET_NAME, PET_LVL, TYPE FROM PETS WHERE PET_ID = {pets[0][1]};"
                pet0 = await self.bot.pg_con.fetch(sql)

                #Check if pet has name
                if pet0[0][2] == "Bear":
                    polish_type = "Niedźwiedź"
                elif pet0[0][2] == "Cat":
                    polish_type = "Kot"
                elif pet0[0][2] == "Boar":
                    polish_type = "Dzik"
                elif pet0[0][2] == "Dragon":
                    polish_type = "Smok"
                elif pet0[0][2] == "Phoenix":
                    polish_type = "Feniks"
                elif pet0[0][2] == "Rabbit":
                    polish_type = "Królik"
                elif pet0[0][2] == "Sheep":
                    polish_type = "Owca"
                elif pet0[0][2] == "Unicorn":
                    polish_type = "Jednorożec"
                elif pet0[0][2] == "Snake":
                    polish_type = "Wąż"
                elif pet0[0][2] == "AnimeGirl":
                    polish_type = "Dziewczynka Anime"
                else:
                    polish_type = ""

                if pet0[0][1] == 0:
                    polish_type = "Jajko"

                pet2_desc = f"Pet w stajni 1: {polish_type} {pet0[0][0]} ({pet0[0][1]} LVL).\n"
            else:
                pet2_desc = "Pet w stajni 1: Brak.\n"

            if pets[0][2] > 0:
                # PET 2
                print("Alt pet 2 exists in PETOWNER database.")
                sql = f"SELECT PET_NAME, PET_LVL, TYPE FROM PETS WHERE PET_ID = {pets[0][2]};"
                pet0 = await self.bot.pg_con.fetch(sql)

                #Check if pet has name
                if pet0[0][2] == "Bear":
                    polish_type = "Niedźwiedź"
                elif pet0[0][2] == "Cat":
                    polish_type = "Kot"
                elif pet0[0][2] == "Boar":
                    polish_type = "Dzik"
                elif pet0[0][2] == "Dragon":
                    polish_type = "Smok"
                elif pet0[0][2] == "Phoenix":
                    polish_type = "Feniks"
                elif pet0[0][2] == "Rabbit":
                    polish_type = "Królik"
                elif pet0[0][2] == "Sheep":
                    polish_type = "Owca"
                elif pet0[0][2] == "Unicorn":
                    polish_type = "Jednorożec"
                elif pet0[0][2] == "Snake":
                    polish_type = "Wąż"
                elif pet0[0][2] == "AnimeGirl":
                    polish_type = "Dziewczynka Anime"
                else:
                    polish_type = ""

                if pet0[0][1] == 0:
                    polish_type = "Jajko"

                pet3_desc = f"Pet w stajni 2: {polish_type} {pet0[0][0]} ({pet0[0][1]} LVL).\n"
            else:
                pet3_desc = "Pet w stajni 2: Brak.\n"

            embed = discord.Embed(title="Stajnia",
                                description="Oto Twoja stajnia, <@" + str(ctx.author.id) + "> <:peepoBlush:984769061340737586>\n\n" + pet1_desc + pet2_desc + pet3_desc,
                                color=0x34cceb, )
            embed.set_footer(text = "Możesz schować lub wyciągnąć peta za pomocą komend $schowajtowarzysza oraz $wyciagnijtowarzysza.")
            embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png')
            await ctx.send(embed=embed)

            
        else:
            print("Player does not exist in PETOWNER database, so creating new record.")
            await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")
            return False

        # Nothing happened.
        return False

    global reassign_pet
    async def reassign_pet(self, pet_id, player_id):
        """Ressigning pet to player in database, but first check if it is possible."""

        pet_id = int(pet_id)
        player_id = int(player_id)

        sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        player_exists = await self.bot.pg_con.fetch(sql)

        sql = f"SELECT PET_ID FROM PETS WHERE PET_ID = {pet_id};"
        pet_exists = await self.bot.pg_con.fetch(sql)

        if player_exists and pet_exists:
            if player_exists[0][0] > 0:
                print("Player exists in PETOWNER database, but it already has pet.")
            else:
                print("Player exists in PETOWNER database and he has not pet. Assigning...")
                sql = f"UPDATE PETOWNER SET PET_ID = {pet_id} WHERE PLAYER_ID = {player_id};"
                await self.bot.pg_con.fetch(sql)
        elif not player_exists and pet_exists:
            print("Player does not exist in PETOWNER database, so creating new record.")
            await self.bot.pg_con.execute(f"""INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED,
             REROLL_SCROLL, REROLL_SCROLL_SHARD))
             VALUES ({player_id},{pet_id},{True},{0},{0});""")
        elif not pet_exists:
            print("Pet does not exists in PETOWNER database. Nothing happens.")

    global discard_pet
    async def discard_pet(self, ctx):
        """Discarding pet from the author if it is possible."""

        sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {ctx.author.id};"
        player_exists = await self.bot.pg_con.fetch(sql)

        # Check if discard pet is confirmed
        def check_discard(author):
            def inner_check(message):
                if message.author == author: #and message.author not in confirmPlayerList:
                    if message.content.lower() == "$potwierdzam":
                        print("Discard accepted: player exists!")
                        return True
                else:
                    print("Wrong person or wrong message!")
                    return False
            return inner_check

        if player_exists:
            if player_exists[0][0] > 0:
                print("Player exists in PETOWNER database, it already has pet.")
                await ctx.channel.send("Czy jesteś pewien, że chcesz porzucić swojego towarzysza <@" + str(ctx.author.id) + ">? <:MonkaS:882181709100097587> Wpisz **$potwierdzam**.")
                print("Waiting for confirm command...")
                try:
                    confirm_cmd = await self.bot.wait_for('message', timeout=15,
                                                        check=check_discard(ctx.author))
                    await confirm_cmd.add_reaction("<:MonkaS:882181709100097587>")
                    sql = f"UPDATE PETOWNER SET PET_ID = {0} WHERE PLAYER_ID = {ctx.author.id};"
                    await self.bot.pg_con.fetch(sql)

                except asyncio.TimeoutError:
                    await ctx.channel.send("*Twój towarzysz oddycha z ulgą...*")
            else:
                await ctx.channel.send("Przecież jesteś sam... <:madge:882184635474386974>")
        else:
            await ctx.channel.send("Przecież jesteś sam... <:madge:882184635474386974>")

    global name_pet
    async def name_pet(self, ctx, name):
        "Set name of pet."

        sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {ctx.author.id};"
        player_exists = await self.bot.pg_con.fetch(sql)

        if player_exists:
            if player_exists[0][0] > 0:

                percentage = random.randint(0,100)
                if percentage >= 85:
                    sql = f"""UPDATE PETS SET PET_NAME = \'{name}\', SHINY = \'{True}\'
                        WHERE PET_ID = {player_exists[0][0]};"""
                    await self.bot.pg_con.fetch(sql)
                    await ctx.channel.send("Towarzysz wygląda na bardzo wdzięcznego i dostrzegasz" +
                                           ", że wygląda jakoś inaczej <:Susge:973591024322633858>")

                else:
                    sql = f"""UPDATE PETS SET PET_NAME = \'{name}\'
                        WHERE PET_ID = {player_exists[0][0]};"""
                    await self.bot.pg_con.fetch(sql)

                await ctx.message.add_reaction("<:peepoBlush:984769061340737586>")

            else:
                await ctx.channel.send("Przecież jesteś sam... <:madge:882184635474386974>")
        else:
            await ctx.channel.send("Przecież jesteś sam... <:madge:882184635474386974>")

    global assign_shard
    async def assign_shard(self, ctx, number, player_id):
        """Assigning scroll shard to the player in database."""

        player_id = int(player_id)
        number = int(number)

        sql = f"SELECT REROLL_SCROLL_SHARD FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        scroll_shards = await self.bot.pg_con.fetch(sql)

        if scroll_shards:
            if scroll_shards[0][0] is None:
                print("Player exists and has no shards, we can update dabatase..")
                sql = f"""UPDATE PETOWNER SET REROLL_SCROLL_SHARD = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
            else:
                print("Player exists and has some shards, we can update dabatase..")
                number += scroll_shards[0][0]
                sql = f"""UPDATE PETOWNER SET REROLL_SCROLL_SHARD = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
        else:
            print("Player doest not exists in PETOWNER database, we can update dabatase..")
            await new_record_petowners(self, player_id, 0, False, 0, number, 0, 0)
            return True

        # Nothing happened.
        return False

    global assign_scroll
    async def assign_scroll(self, ctx, number, player_id):
        """Assigning full scroll to the player in database."""

        player_id = int(player_id)
        number = int(number)

        sql = f"SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        scrolls = await self.bot.pg_con.fetch(sql)
        print(scrolls)

        if scrolls:
            if scrolls[0][0] is None:
                print("Player exists and has no shards, we can update dabatase..")
                sql = f"""UPDATE PETOWNER SET REROLL_SCROLL = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
            else:
                print("Player exists and has some shards, we can update dabatase..")
                number += scrolls[0][0]
                sql = f"""UPDATE PETOWNER SET REROLL_SCROLL = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
        else:
            print("Player doest not exists in PETOWNER database, we can update dabatase..")
            await new_record_petowners(self, player_id, 0, False, number, 0, 0, 0)
            return True

        # Nothing happened.
        return False
    
    global assign_rebirth_stone
    async def assign_rebirth_stone(self, ctx, number, player_id):
        """Assigning rebirth stone to the player in database."""

        player_id = int(player_id)
        number = int(number)

        sql = f"SELECT REBIRTH_STONES FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        rebirth_stones = await self.bot.pg_con.fetch(sql)

        if rebirth_stones:
            if rebirth_stones[0][0] is None:
                print("Player exists and has no rebirth stones, we can update dabatase..")
                sql = f"""UPDATE PETOWNER SET REBIRTH_STONES = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
            else:
                print("Player exists and has some rebirth stones, we can update dabatase..")
                number += rebirth_stones[0][0]
                sql = f"""UPDATE PETOWNER SET REBIRTH_STONES = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
        else:
            print("Player doest not exists in PETOWNER database, we can update dabatase..")
            await new_record_petowners(self, player_id, 0, False, 0, 0, number, 0)
            return True

        # Nothing happened.
        return False

    global assign_mirror
    async def assign_mirror(self, ctx, number, player_id):
        """Assigning mirror to the player in database."""

        player_id = int(player_id)
        number = int(number)

        sql = f"SELECT MIRRORS FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        mirrors = await self.bot.pg_con.fetch(sql)

        if mirrors:
            if mirrors[0][0] is None:
                print("Player exists and has no mirrors, we can update dabatase..")
                sql = f"""UPDATE PETOWNER SET MIRRORS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
            else:
                print("Player exists and has some mirrors, we can update dabatase..")
                number += mirrors[0][0]
                sql = f"""UPDATE PETOWNER SET MIRRORS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
                return True
        else:
            print("Player doest not exists in PETOWNER database, we can update dabatase..")
            await new_record_petowners(self, player_id, 0, False, 0, 0, 0, number)
            return True

        # Nothing happened.
        return False
    
    global level_up_pet
    async def level_up_pet(self, ctx, player_id):
        """Level up the pet."""

        player_id = int(player_id)

        sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        player_exists = await self.bot.pg_con.fetch(sql)

        if player_exists:
            if player_exists[0][0] > 0:
                print("Player exists in PETOWNER database, it already has pet.")

                sql = f"""SELECT PET_LVL, PET_SKILLS, SHINY, TYPE, VARIANT, CRIT_PERC, REPLACE_PERC,
                DEF_PERC, DROP_PERC, LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION
                FROM PETS WHERE PET_ID = {player_exists[0][0]};"""
                pet_stats = await self.bot.pg_con.fetch(sql)

                pet_id = player_exists[0][0]
                pet_lvl = pet_stats[0][0]
                pet_skills = pet_stats[0][1]
                pet_shiny = pet_stats[0][2]
                pet_type = pet_stats[0][3]
                pet_variant = int(pet_stats[0][4])
                pet_crit_perc = pet_stats[0][5]
                pet_replace_perc = pet_stats[0][6]
                pet_def_perc = pet_stats[0][7]
                pet_drop_perc = pet_stats[0][8]
                pet_lowhp_perc = pet_stats[0][9]
                pet_slow_perc = pet_stats[0][10]
                pet_init_perc = pet_stats[0][11]
                pet_detection = pet_stats[0][12]

                skill_list = {"CRIT_PERC": pet_crit_perc, "REPLACE_PERC": pet_replace_perc,
                            "DEF_PERC": pet_def_perc, "DROP_PERC": pet_drop_perc,
                            "LOWHP_PERC": pet_lowhp_perc, "SLOW_PERC": pet_slow_perc,
                            "INIT_PERC": pet_init_perc, "DETECTION": pet_detection}

                # Check if pet is possible to level up...
                if pet_lvl < 3:
                    # Check if pet is EGG
                    if pet_lvl == 0:
                        pet_variant = pet_variant % 2

                    pet_lvl += 1

                    add_skill = random.randint(1,2)
                    pet_skills += add_skill

                    for i in range(add_skill):
                        while True:
                            skill_name, skill_value = random.choice(list(skill_list.items()))
                            if skill_name == "DETECTION" and pet_detection:
                                pass
                            else:
                                if skill_name == "INIT_PERC":
                                    if pet_shiny:
                                        add_skill_value = random.randint(5, 10)
                                    else:
                                        add_skill_value = random.randint(5, 8)
                                elif skill_name == "SLOW_PERC":
                                    if pet_shiny:
                                        add_skill_value = random.randint(5, 10)
                                    else:
                                        add_skill_value = random.randint(5, 8)
                                else:
                                    if pet_shiny:
                                        add_skill_value = random.randint(3, 7)
                                    else:
                                        add_skill_value = random.randint(3, 5)

                                if skill_name == "DETECTION":
                                    skill_list[skill_name] = True
                                else:
                                    skill_list[skill_name] += add_skill_value

                                if skill_name == "INIT_PERC":
                                    add_skill_value = random.randint(3, 7)

                                await self.bot.pg_con.execute(f"""UPDATE PETS SET
                                PET_LVL={pet_lvl}, PET_SKILLS={pet_skills},
                                VARIANT={pet_variant},
                                CRIT_PERC={skill_list["CRIT_PERC"]},
                                REPLACE_PERC={skill_list["REPLACE_PERC"]},
                                DEF_PERC={skill_list["DEF_PERC"]},
                                DROP_PERC={skill_list["DROP_PERC"]},
                                LOWHP_PERC={skill_list["LOWHP_PERC"]},
                                SLOW_PERC={skill_list["SLOW_PERC"]},
                                INIT_PERC={skill_list["INIT_PERC"]},
                                DETECTION={skill_list["DETECTION"]} WHERE PET_ID = {pet_id};""")

                                break
                    return True
                else:
                    return False
            else:
                return False
        else:
            return False
        
    global get_pet_skills
    async def get_pet_skills(self, player_id):
        """Get all user pet skills"""

        player_id = int(player_id)

        sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        player_exists = await self.bot.pg_con.fetch(sql)

        skill_list = {"CRIT_PERC": 0, "REPLACE_PERC": 0,
                        "DEF_PERC": 0, "DROP_PERC": 0,
                        "LOWHP_PERC": 0, "SLOW_PERC": 0,
                        "INIT_PERC": 0, "DETECTION": False}
        if player_exists:
            if player_exists[0][0] > 0:
                print("Player exists in PETOWNER database, it already has pet.")

                sql = f"""SELECT PET_LVL, PET_SKILLS, SHINY, TYPE, VARIANT, CRIT_PERC, REPLACE_PERC,
                DEF_PERC, DROP_PERC, LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION
                FROM PETS WHERE PET_ID = {player_exists[0][0]};"""
                pet_stats = await self.bot.pg_con.fetch(sql)

                pet_id = player_exists[0][0]
                pet_lvl = pet_stats[0][0]
                pet_skills = pet_stats[0][1]
                pet_shiny = pet_stats[0][2]
                pet_type = pet_stats[0][3]
                pet_variant = int(pet_stats[0][4])
                pet_crit_perc = pet_stats[0][5]
                pet_replace_perc = pet_stats[0][6]
                pet_def_perc = pet_stats[0][7]
                pet_drop_perc = pet_stats[0][8]
                pet_lowhp_perc = pet_stats[0][9]
                pet_slow_perc = pet_stats[0][10]
                pet_init_perc = pet_stats[0][11]
                pet_detection = pet_stats[0][12]

                skill_list = {"CRIT_PERC": pet_crit_perc, "REPLACE_PERC": pet_replace_perc,
                            "DEF_PERC": pet_def_perc, "DROP_PERC": pet_drop_perc,
                            "LOWHP_PERC": pet_lowhp_perc, "SLOW_PERC": pet_slow_perc,
                            "INIT_PERC": pet_init_perc, "DETECTION": pet_detection}

                return skill_list
            else:
                return skill_list
        else:
            return skill_list
        
    global detect_boss
    async def detect_boss(self, boss_rarity):
        """Send PM to everyone who owns the pet with detection."""
        print("Detecting boss...")

        sql = f"SELECT PET_ID FROM PETS WHERE DETECTION=True;"
        pet_ids = await self.bot.pg_con.fetch(sql)

        if len(pet_ids) > 0:
            print("Length is ok.")
            for pet_id in pet_ids:
                pet_id = int(pet_id[0])
                print("PET_ID: " + str(pet_id))

                sql = f"SELECT PLAYER_ID FROM PETOWNER WHERE PET_ID={pet_id};"
                player_id = await self.bot.pg_con.fetch(sql)

                if len(player_id) > 0:
                    print("Player exists.")
                    player_id = int(player_id[0][0])
                    if player_id > 0:
                        print("Player: " + str(player_id))

                        user = await self.bot.fetch_user(player_id)
                        if boss_rarity == 0:
                            rarity = "zwykły"
                        elif boss_rarity == 1:
                            rarity = "rzadki"
                        elif boss_rarity == 2:
                            rarity = "epicki"
                        elif boss_rarity == 3:
                            rarity = "legendarny"
                        try:
                            await user.send("Nadciąga boss! Będzie " + rarity + ".")
                            if boss_rarity == 3:
                                await user.send("Legendarne bossy mogą pojawić się tylko w weekendy albo wieczorem w tygodniu. W innym wypadku zostaną zamienione na epickie.")
                        except:
                            if DebugMode:
                                chatChannel = self.bot.get_channel(881090112576962560)
                            else:
                                chatChannel = self.bot.get_channel(776379796367212594)
                            print("PM unavailable.")
                            await chatChannel.send("<@" + str(player_id) + "> - Twój towarzysz: *Nadciąga boss! Odblokuj prywatne wiadomości, jeśli chcesz otrzymywać powiadomienia!*")
                else:
                    print("Player not exists.")


async def new_record_petowners(self, player_id: int, pet_id: int, pet_owned: bool,
                                reroll_scroll: int, reroll_scroll_shard: int, rebirth_stones: int,
                                mirrors: int):
    """New record of user in petowners database."""

    sql=f"""INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED,
    REROLL_SCROLL, REROLL_SCROLL_SHARD, REBIRTH_STONES, MIRRORS)
    VALUES ({player_id},{pet_id},{pet_owned},{reroll_scroll},{reroll_scroll_shard},{rebirth_stones},
    {mirrors});"""
    await self.bot.pg_con.fetch(sql)

def setup(bot):
    """Load the FunctionsPets cog."""
    bot.add_cog(FunctionsPets(bot))
