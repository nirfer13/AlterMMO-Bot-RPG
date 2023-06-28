"""Class with all functions used for skills."""

import random
from enum import Enum
from discord.ext import commands

import functions_pets

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsSkills(commands.Cog, name="FunctionsSkills"):
    """Class with all functions used for skillss."""
    def __init__(self, bot):
        self.bot = bot

# ==================================== FUNCTIONS FOR DATABASES =================================

    global create_skills_table
    async def create_skills_table(self):
        '''Droping Skills Table if exists'''

        await self.bot.pg_con.execute("DROP TABLE IF EXISTS SKILLS")
        #Creating table as per requirement
        sql ='''CREATE TABLE SKILLS(
           SKILL_ID NUMERIC,
           GEM_NAME VARCHAR(255),
           SKILL_VARIANT NUMERIC,
           SLOW_PERC NUMERIC
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table SKILLS created successfully.")
        print(f"""INSERT INTO SKILLS 
            (SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC) 
            VALUES ({0},\'{'Default'}\',{0},{0});""")
        await self.bot.pg_con.execute(f"""INSERT INTO SKILLS 
            (SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC) 
            VALUES ({0},\'{'Default'}\',{0},{0});""")
    
    global generate_skill_gem
    async def generate_skill_gem(self, ctx):
        """Generate skill."""

        skill_variant = random.randint(0, 8)
        slow_perc = random.randint(0, 20)

        for retries in range(0,3):
            try:
                db_read = await self.bot.pg_con.fetch("SELECT SKILL_ID FROM SKILLS ORDER BY SKILL_ID DESC LIMIT 1")
                break
            except:
                await ctx.channel.send(f"Błąd bazy danych <:Sadge:936907659142111273>... Próbuję ponownie - {retries}")
        else:
            return False

        print(f"Read from databse {db_read}")

        skill_config = {}
        skill_config["SKILL_ID"] = 1 + int(db_read[0][0])
        skill_config["GEM_NAME"] = "Umiejętność"
        skill_config["SKILL_VARIANT"] = skill_variant
        skill_config["SLOW_PERC"] = slow_perc

        print(skill_config)

        await self.bot.pg_con.execute(f"""INSERT INTO SKILLS 
            (SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC) VALUES 
            ({skill_config["SKILL_ID"]},\'{skill_config["GEM_NAME"]}\',
            {skill_config["SKILL_VARIANT"]},{skill_config["SLOW_PERC"]});""")

        return skill_config["SKILL_ID"]

    global assign_skill
    async def assign_skill(self, ctx, player):
        """Assigning new skill to player in database, but first check if it is possible."""

        player_id = int(player.id)

        sql = f"SELECT SKILL_GEM FROM PETOWNER WHERE PLAYER_ID = {player_id};"
        
        for retries in range(0,3):
            try:
                player_exists = await self.bot.pg_con.fetch(sql)
                break
            except:
                await ctx.channel.send(f"Błąd bazy danych <:Sadge:936907659142111273>... Próbuję ponownie - {retries}")
        else:
            return False

        if player_exists:
            if player_exists[0][0] > 0:
                print("Player exists in PETOWNER database, but it already has skill.")
                return False
            else:
                skill_id = await generate_skill_gem(self, ctx)
                print("Player exists in PETWONER database and he has not skill. Assigning...")
                sql = f"UPDATE PETOWNER SET SKILL_GEM = {skill_id} WHERE PLAYER_ID = {player_id};"
                await self.bot.pg_con.fetch(sql)
                return True
        else:
            print("Player does not exist in PETOWNER database, so creating new record.")
            skill_id = await generate_skill_gem(self, ctx)
            await functions_pets.new_record_petowners(self, player_id, 0, False, 0, 0, 0, 0, skill_id)
            return True

        # Nothing happened.
        return False

def setup(bot):
    """Load the FunctionsSkills cog."""
    bot.add_cog(FunctionsSkills(bot))
