"""Class with all functions used for pets."""

import json
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
    async def show_pet(self, ctx):
        """Show author's pet and his scrolls."""

        print("Checking if user has a pet...")
        sql = f"SELECT PET_ID, REROLL_SCROLL, REROLL_SCROLL_SHARD FROM PETOWNER WHERE PLAYER_ID = {ctx.author.id};"
        pet_exists = await self.bot.pg_con.fetch(sql)
        print(pet_exists)
        reroll_scroll = pet_exists[0][1]
        reroll_shard = pet_exists[0][2]

        if pet_exists:
            if pet_exists[0][0] > 0:
                # Convert shards to full scrolls
                if reroll_shard // 10 > 0:
                    reroll_scroll += reroll_shard // 10
                    sql = f"""UPDATE PETOWNER SET REROLL_SCROLL = {reroll_scroll}
                    WHERE PLAYER_ID = {ctx.author.id};"""
                    await self.bot.pg_con.fetch(sql)

                    reroll_shard = reroll_shard % 10
                    sql = f"""UPDATE PETOWNER SET REROLL_SCROLL_SHARD = {reroll_shard}
                    WHERE PLAYER_ID = {ctx.author.id};"""
                    await self.bot.pg_con.fetch(sql)
                    print(f"Update reroll scrolls {reroll_scroll} and shards {reroll_shard}")

                # Add info about scrolls
                scroll_desc = f"""\n\nZwoje odrodzenia: {reroll_scroll}
                Fragmenty zwojów: {reroll_shard}"""

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
                                        description="Oto Twój towarzysz, <@" + str(ctx.author.id) + ">. Opiekuj się nim, a być może kiedyś coś z niego wyrośnie..." + scroll_desc + add_desc,
                                        color=color)
                    file = discord.File(path, filename=f"{pet_data[0][7]}.png")
                    embed.set_footer(text = "Na zawsze ponosisz odpowiedzialność za to, co oswoiłeś.")
                    embed.set_image(url=f"attachment://{pet_data[0][7]}.png")
                    await ctx.send(file=file, embed=embed)
            else:
                await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")
        else:
            await ctx.channel.send("Niestety jesteś sam jak palec na tym świecie <@" + str(ctx.author.id) + "> <:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby na zdobycie towarzysza?")

    global generate_pet_egg
    async def generate_pet_egg(self, ctx, player):
        """Generate first pet as an egg."""

        crafter = discord.utils.get(ctx.guild.roles, id=687185998550925312)
        if crafter in player.roles:
            pets_list = [PetType.BEAR, PetType.BOAR, PetType.CAT,
                         PetType.RABBIT, PetType.SHEEP, PetType.DRAGON,
                         PetType.PHOENIX, PetType.UNICORN]
        else:
            pets_list = [PetType.BEAR, PetType.BOAR, PetType.CAT,
                         PetType.RABBIT, PetType.SHEEP]
            
        print(pets_list)

        pet = random.choice(pets_list)

        percentage = random.randint(0,100)
        shiny = percentage >= 95

        if pet in [PetType.DRAGON, PetType.PHOENIX, PetType.UNICORN]:
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

        print(pet_config)

        await self.bot.pg_con.execute(f"""INSERT INTO PETS
            (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY,
            TYPE, VARIANT) VALUES ({pet_config["PET_ID"]},\'{pet_config["PET_NAME"]}\',
            {pet_config["PET_LVL"]},{pet_config["PET_SKILLS"]},
            \'{pet_config["QUALITY"]}\',{pet_config["SHINY"]},
            \'{pet_config["TYPE"]}\',\'{pet_config["VARIANT"]}\');""")

        print(f"Pet egg generated {pet}, shiny: {shiny}.")

        return pet_config["PET_ID"]

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
            await new_record_petowners(self, player_id, pet_id, True, 0, 0)
            return True

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
            await new_record_petowners(self, player_id, 0, False, 0, number)
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
            await new_record_petowners(self, player_id, 0, False, number, 0)
            return True

        # Nothing happened.
        return False
 
async def new_record_petowners(self, player_id: int, pet_id: int, pet_owned: bool,
                                reroll_scroll: int, reroll_scroll_shard: int):
    """New record of user in petowners database."""

    sql=f"""INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED,
    REROLL_SCROLL, REROLL_SCROLL_SHARD)
    VALUES ({player_id},{pet_id},{pet_owned},{reroll_scroll},{reroll_scroll_shard});"""
    await self.bot.pg_con.fetch(sql)

def setup(bot):
    """Load the FunctionsPets cog."""
    bot.add_cog(FunctionsPets(bot))
