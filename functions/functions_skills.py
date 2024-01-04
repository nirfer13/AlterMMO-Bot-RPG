"""Class with all functions used for skills."""

import random
import discord
import asyncio
from enum import Enum
from discord.ext import commands

import functions_pets
import functions_daily
import functions_modifiers

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

        skill_variant = random.randint(1, 3)
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

        if skill_variant == 1:
            skill_config["GEM_NAME"] = "Klejnot Regeneracji"
        elif skill_variant == 2:
            skill_config["GEM_NAME"] = "Klejnot Wyjątkowości"
        elif skill_variant == 3:
            skill_config["GEM_NAME"] = "Klejnot Przywołania"
        elif skill_variant == 4:
            skill_config["GEM_NAME"] = "Klejnot Błogosławieństwa"
        elif skill_variant == 5:
            skill_config["GEM_NAME"] = "Klejnot Spowolnienia"
        elif skill_variant == 6:
            skill_config["GEM_NAME"] = "Klejnot Obfitości"
        else:
            skill_config["GEM_NAME"] = "Nieznany klejnot"

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

    global show_skill
    async def show_skill(self, ctx):
        """Show current skill."""

        player_id = int(ctx.author.id)

        sql = f"SELECT SKILL_GEM FROM petowner WHERE PLAYER_ID = {player_id};"
        gem_exists = await self.bot.pg_con.fetch(sql)

        if gem_exists:
            skill_id = gem_exists[0][0]


            if skill_id > -1:
                skill_data = await get_skill_data(self, ctx, skill_id)

                skill_name = skill_data[1]
                skill_variant = int(skill_data[2])
                skill_slow_perc = int(skill_data[3])

                print(skill_name)
                print(skill_variant)
                print(skill_slow_perc)

                if skill_variant == 0:
                    await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")
                    return False
                elif skill_variant == 1:
                    skill_desc = f"Klejnot regeneracji pozwala na odpoczynek wszystkich podróżników, którzy są chętni. Zaraz po nabraniu sił mogą ponownie wyruszyć na polowanie.\n\nDodatkowe spowolnienie: {skill_slow_perc} %.\n\nMożesz użyć umiejętności przez komendę **$skill**."
                elif skill_variant == 2:
                    skill_desc = f"Klejnot wyjątkowości sprawia, że towarzysz wybranego podróżnika zostaje odmieniony i staje się wyjątkowy.\n\nDodatkowe spowolnienie: {skill_slow_perc} %.\n\nMożesz użyć umiejętności przez komendę **$skill**."
                elif skill_variant == 3:
                    skill_desc = f"Klejnot przywołania uwalnia potężnego bossa. Każdy może go zaatakować.\n\nDodatkowe spowolnienie: {skill_slow_perc} %.\n\nMożesz użyć umiejętności przez komendę **$skill**."
                elif skill_variant == 4:
                    skill_desc = f"Klejnot błogosławieństwa sprawia, że na potwory i bossów spływają efekty magiczne.\n\nDodatkowe spowolnienie: {skill_slow_perc} %.\n\nMożesz użyć umiejętności przez komendę **$skill**."
                elif skill_variant == 5:
                    skill_desc = f"Klejnot spowolnienia spowalnia nadciągające potwory oraz bossa.\n\nDodatkowe spowolnienie: {skill_slow_perc} %.\n\nMożesz użyć umiejętności przez komendę **$skill**."
                elif skill_variant == 6:
                    skill_desc = f"Klejnot obfitości sprawia, że następne potwory oraz boss będą oferowały lepsze nagrody.\n\nDodatkowe spowolnienie: {skill_slow_perc} %.\n\nMożesz użyć umiejętności przez komendę **$skill**."
                else:
                    skill_desc = "Nieznany klejnot umiejętności."

                title = skill_name
                path = f"skill_gems/{skill_variant}.png"
                color = 0xfc7703
                embed = discord.Embed(title=title,
                                    description="Oto Twój klejnot umiejętności, <@" + str(player_id) + ">.\n\n" + skill_desc,
                                    color=color)
                file = discord.File(path, filename=f"{skill_variant}.png")
                embed.set_footer(text = "Wielcy ludzie nie rodzą się wielkimi, tylko się nimi stają.")
                embed.set_image(url=f"attachment://{skill_variant}.png")
                await ctx.send(file=file, embed=embed)

            else:
                await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")
        else:
            await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")

    global use_skill
    async def use_skill(self, ctx, player):
        """Use skill based on the type of the skill."""

        player_id = int(player.id)

        sql = f"SELECT SKILL_GEM FROM petowner WHERE PLAYER_ID = {player_id};"
        gem_exists = await self.bot.pg_con.fetch(sql)

        if gem_exists:
            skill_id = gem_exists[0][0]


            if skill_id > -1:
                skill_data = await get_skill_data(self, ctx, skill_id)

                skill_name = skill_data[1]
                skill_variant = int(skill_data[2])
                skill_slow_perc = int(skill_data[3])

                print(skill_name)
                print(skill_variant)
                print(skill_slow_perc)

                if skill_variant == 0:
                    await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")
                elif skill_variant == 1:
                    await skill_rest(self, ctx)
                    daily_skill_used(ctx.author.id)
                    return 1
                elif skill_variant == 2:
                    await skill_make_shiny(self, ctx)
                    weekly_skill_used(ctx.author.id)
                    return 2
                elif skill_variant == 3:
                    if await skill_spawn_boss(self, ctx):
                        weekly_skill_used(ctx.author.id)
                        return 3
                elif skill_variant == 4:
                    await skill_shrine_festival(self, ctx)
                    daily_skill_used(ctx.author.id)
                    return 4
                elif skill_variant == 5:
                    await skill_slow(self, ctx)
                    daily_skill_used(ctx.author.id)
                    return 5
                elif skill_variant == 6:
                    await skill_increase_loot(self, ctx)
                    daily_skill_used(ctx.author.id)
                    return 6
                
                return 0
            else:
                await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")
                return 0
        else:
            await ctx.channel.send("Niestety nie posiadasz umiejętności <@" + str(player_id) + "> <:Sadge:936907659142111273> Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")
            return 0

    global skill_rest
    async def skill_rest(self, ctx):
        """Skill which organizes meal. Everyone who use reaction can go hunting again."""

        if not load_daily_skill(ctx.author.id):
            print("Meal spawn.")

            e_title = f"<:peepofat:1062666941405331507> {ctx.author.name} zaprasza na odpoczynek! <:peepofat:1062666941405331507>"

            e_descr = ('Wspaniały dzień! Wszyscy bohaterowie zostali zaproszeni na ucztę!\n\n'
            '**Zostaw reakcję, żeby wypocząć i móc ponownie wyruszyć na polowanie. Odpoczynek potrwa maksymalnie 5 minut.**')

            e_color = 0xFC0303

            image_name = "events/rest/" + str(random.randint(0,4)) + ".png"
            file=discord.File(image_name)

            embed = discord.Embed(
                title=e_title,
                description=e_descr,
                color=e_color)

            await ctx.send(file=file)
            msg = await ctx.send(embed=embed)

            await msg.add_reaction("<:Bedge:970576892874854400>")

            if DebugMode:
                await asyncio.sleep(15)
            else:
                await asyncio.sleep(300)

            users = []
            message = await ctx.channel.fetch_message(msg.id)
            for reaction in message.reactions:
                async for user in reaction.users():
                    guild = message.guild
                    if guild.get_member(user.id) is not None:
                        users.append(str(user.id))
                break
            print(users)

            await functions_daily.remove_player_daily_file(self, users)
            await ctx.send("Wszyscy podróżnicy, którzy skorzystali z zaproszenia, mogą ponownie ruszyć na polowanie! <:Up:912798893304086558>")

            return True
        else:
            await ctx.send("Twój klejnot umiejętności regeneruje się! Spróbuj jutro! <:Bedge:970576892874854400>")
            return False

    global skill_make_shiny
    async def skill_make_shiny(self, ctx):
        """Skill that is used to make a pet shiny."""

        if not load_weekly_skill(ctx.author.id):

            # Check if reroll pet is confirmed
            def check_user(author):
                def inner_check(message):
                    if message.author == author:
                        return True
                    else:
                        print("Wrong person or wrong message!")
                        return False
                return inner_check

            try:
                await ctx.channel.send("Oznacz użytkownika, którego towarzysz ma stać się wyjątkowy!")
                user_to_shiny = await self.bot.wait_for('message', timeout=30, check=check_user(ctx.author))
                user_string = user_to_shiny.content.strip()
                user_id = int(user_string[2:-1])

            except asyncio.TimeoutError:
                await ctx.channel.send("Moc kamienia zanikła...")
                return False
            except ValueError:
                await ctx.channel.send("Niestety podany podróżnik nie istnieje. Moc Twojego kamienia umiejętności została wyczerpana.")
                return False

            # Check if user exists
            try:
                user = self.bot.get_user(user_id)
                print(user)
            except:
                await ctx.channel.send("Niestety podany podróżnik nie istnieje. Moc Twojego kamienia umiejętności została wyczerpana.")
                return False

            if user:
                sql = f"SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = {user_id};"
                pet_exists = await self.bot.pg_con.fetch(sql)

                if pet_exists:

                    if pet_exists[0][0] > 0:

                        sql = f"""UPDATE PETS SET SHINY = \'{True}\'
                                WHERE PET_ID = {pet_exists[0][0]};"""
                        await self.bot.pg_con.fetch(sql)
                        await ctx.channel.send(f"Twój Towarzysz <@{user_id}> wygląda na bardzo wdzięcznego i dostrzegasz" +
                                                f", że wygląda jakoś inaczej <:Susge:973591024322633858> To sprawka <@{ctx.author.id}>.")

                    else:
                        await ctx.channel.send("Niestety podany podróżnik nie posiada aktualnie towarzysza <:Sadge:936907659142111273> Moc Twojego kamienia umiejętności została wyczerpana.")
                else:
                    await ctx.channel.send("Niestety podany podróżnik nie posiada aktualnie towarzysza <:Sadge:936907659142111273> Moc Twojego kamienia umiejętności została wyczerpana.")
            else:
                await ctx.channel.send("Niestety podany podróżnik nie istnieje. Moc Twojego kamienia umiejętności została wyczerpana.")
                return False
        else:
            await ctx.send("Twój klejnot umiejętności regeneruje się! Spróbuj w nadchodzącym tygodniu! <:Bedge:970576892874854400>")
            return False

    global skill_spawn_boss
    async def skill_spawn_boss(self, ctx):
        """Skill that is used to summon a boss."""

        if not load_weekly_skill(ctx.author.id):
          return True
        else:
            await ctx.send("Twój klejnot umiejętności regeneruje się! Spróbuj w nadchodzącym tygodniu! <:Bedge:970576892874854400>")
            return False
        
    global skill_shrine_festival
    async def skill_shrine_festival(self, ctx):
        """Skill that is used to pray to shrine 5x."""

        if not load_daily_skill(ctx.author.id):
            await ctx.message.add_reaction("<:prayge:1063891597760139304>")
            for i in range(5):
                await functions_modifiers.random_modifiers(self, ctx, False)

            await ctx.send("Wygląda na to, że bossowie i potwory odczuwają efekty Twojego kamienia umiejętności <:MonkaChrist:783992337075929098>")
            return True
        else:
            await ctx.send("Twój klejnot umiejętności regeneruje się! Spróbuj jutro! <:Bedge:970576892874854400>")
            return False

    global skill_slow
    async def skill_slow(self, ctx):
        """Skill that is used to add slow during next fight."""

        if not load_daily_skill(ctx.author.id):

            await functions_modifiers.modify_modifiers(self, ctx, "time_increased_perc", 40)

            await ctx.send("Klejnot sprawia, że przeciwnicy lepią się od błota i poruszają się znacznie wolniej.")
            return True
        else:
            await ctx.send("Twój klejnot umiejętności regeneruje się! Spróbuj jutro! <:Bedge:970576892874854400>")
            return False

    global skill_increase_loot
    async def skill_increase_loot(self, ctx):
        """Skill that is used to add additional loot during next fight."""

        if not load_daily_skill(ctx.author.id):

            await functions_modifiers.modify_modifiers(self, ctx, "drop_boost_perc", 40)
            await functions_modifiers.modify_modifiers(self, ctx, "points_boost", 3)

            await ctx.send("Klejnot sprawia, że przeciwnicy oferują teraz lepsze nagrody.")
            return True
        else:
            await ctx.send("Twój klejnot umiejętności regeneruje się! Spróbuj jutro! <:Bedge:970576892874854400>")
            return False


    # DAILY
    #function to save daily skills user to file
    global daily_skill_used
    def daily_skill_used (player_id):
        player_id = str(player_id)

        with open('skill_daily_player_cd.txt', 'r') as r:
            read_lines = r.readlines()
        r.close()
        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        if player_id not in new_list:
            with open('skill_daily_player_cd.txt', 'a') as f:
                f.write(str(player_id) + '\n')

    #function to read daily skill user from file
    global load_daily_skill
    def load_daily_skill(player_id):
        player_id = str(player_id)

        with open('skill_daily_player_cd.txt', 'r') as r:
            read_lines = r.readlines()

        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        print(new_list)

        return player_id in new_list

    #function to clear all daily skill users from file
    global clear_daily_skill
    async def clear_daily_skill(self):

        open('skill_daily_player_cd.txt', 'w').close()
        logChannel = self.bot.get_channel(881090112576962560)
        await logChannel.send("Zresetowano cooldown na daily skille.")

    # WEEKLY
    #function to save weekly skills user to file
    global weekly_skill_used
    def weekly_skill_used (player_id):
        player_id = str(player_id)

        with open('skill_weekly_player_cd.txt', 'r') as r:
            read_lines = r.readlines()
        r.close()
        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        if player_id not in new_list:
            with open('skill_weekly_player_cd.txt', 'a') as f:
                f.write(str(player_id) + '\n')

    #function to read weekly skill user from file
    global load_weekly_skill
    def load_weekly_skill(player_id):
        player_id = str(player_id)

        with open('skill_weekly_player_cd.txt', 'r') as r:
            read_lines = r.readlines()

        new_list = []
        for line in read_lines:
            new_list.append(line.strip())

        print(new_list)

        return player_id in new_list

    #function to clear all weekly skill users from file
    global clear_weekly_skill
    async def clear_weekly_skill(self):

        open('skill_weekly_player_cd.txt', 'w').close()
        logChannel = self.bot.get_channel(881090112576962560)
        await logChannel.send("Zresetowano cooldown na weekly skille.")


def setup(bot):
    """Load the FunctionsSkills cog."""
    bot.add_cog(FunctionsSkills(bot))
