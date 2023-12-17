"""Class with all functions used for skills."""

import random
import discord
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

        skill_variant = random.randint(1, 6)
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
                pass
        else:
            pass

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
    
    global get_skill_data
    async def get_skill_data(self, ctx, skill_id):
        """Get all data about the skill with given skill_id."""

        for retries in range(0,3):
            try:
                db_read = await self.bot.pg_con.fetch("SELECT SKILL_ID, " +
                f"GEM_NAME, SKILL_VARIANT, SLOW_PERC FROM SKILLS WHERE SKILL_ID = {skill_id}")
                break
            except:
                await ctx.channel.send(f"Błąd bazy danych <:Sadge:936907659142111273>... Próbuję ponownie - {retries}")
        else:
            return False

        if db_read:
            print(db_read)
            skill_data = db_read[0]
            return skill_data
        else:
            await ctx.channel.send("Taki klejnot umiejętności nie istnieje!")

    global use_skill
    async def use_skill(self, ctx, player):
        """Use skill based on the type of the skill."""

        player_id = int(player.id)

        sql = f"SELECT SKILL_GEM FROM petowner WHERE PLAYER_ID = {player_id};"
        gem_exists = await self.bot.pg_con.fetch(sql)

        if gem_exists:
            skill_id = gem_exists[0][0]


            if skill_id > -1:
                await ctx.channel.send(f"Posiadasz kamień ID: {skill_id}")

                skill_data = await get_skill_data(self, ctx, skill_id)

                skill_name = skill_data[0]
                skill_variant = skill_data[1]
                skill_slow_perc = skill_data[2]
        
                #TODO Add functions related to skill_variant.
            else:
                await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")
        else:
            await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")

    global skill_rest
    async def skill_rest(self, ctx):
        """Skill which organizes meal. Everyone who use reaction can go hunting again."""

        print("Meal spawn.")

        e_title = f"<:peepofat:1062666941405331507> Wielka uczta zorganizowana przez {ctx.author.name}! <:peepofat:1062666941405331507>"

        e_descr = ('Wspaniały dzień! Wszyscy bohaterowie zostali zaproszeni na ucztę!\n\n'
        '**Zostaw reakcję, żeby wypocząć i móc ponownie wyruszyć na polowanie.**')

        e_color = 0xFC0303

        image_name = "events/rest/" + str(random.randint(0,4)) + ".png"
        file=discord.File(image_name)

        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        await ctx.send(file=file)
        msg = await ctx.send(embed=embed)

        users_list = []
        #Define check function
        def check(reaction, user):
            users_list.append(user)
            return msg.channel == ctx.channel and str(reaction.emoji) == "<:Bedge:970576892874854400>" and user.id != 971322848616525874 and user.id != 859729615123251200 and msg.id == reaction.message.id and user not in users_list

        await msg.add_reaction("<:Bedge:970576892874854400>")

        if DebugMode:
            timeout = 15
        else:
            timeout = 600



def setup(bot):
    """Load the FunctionsSkills cog."""
    bot.add_cog(FunctionsSkills(bot))
