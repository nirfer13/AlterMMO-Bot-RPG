"""Class with all functions used for patrons."""

import random
from discord.ext import commands
import discord

import functions_patrons

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsAchievements(commands.Cog, name="FunctionsAchievements"):
    """Class with all functions used for achievements."""
    def __init__(self, bot):
        self.bot = bot

    global create_database
    async def create_database(self, ctx):

        await self.bot.pg_con.execute("DROP TABLE IF EXISTS ACHIEVEMENTS")
        #Creating table as per requirement
        sql ='''CREATE TABLE ACHIEVEMENTS (
           PLAYER_ID NUMERIC PRIMARY KEY,
           BOSS_KILLS NUMERIC,
           HUNTS NUMERIC,
           BUTCHERS NUMERIC
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table ACHIEVEMENTS created successfully (empty).")

    global add_boss_kill
    async def add_boss_kill(self, ctx, player: discord.member):

        player_id = int(player.id)
        number = 1

        sql = f"SELECT BOSS_KILLS FROM ACHIEVEMENTS WHERE PLAYER_ID = {player_id};"

        for retries in range(0,3):
            try:
                boss_kills = await self.bot.pg_con.fetch(sql)
                break
            except:
                boss_kills = None
        else:
            pass

        if boss_kills:
            if boss_kills[0][0] is None:
                print("Player exists and has boss kills, we can update dabatase..")
                sql = f"""UPDATE ACHIEVEMENTS SET BOSS_KILLS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
            else:
                print("Player exists and has some boss kills, we can update dabatase..")
                number += boss_kills[0][0]
                sql = f"""UPDATE ACHIEVEMENTS SET BOSS_KILLS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
        else:
            print("Player doest not exists in ACHIEVEMENTS database, we can update dabatase..")
            await new_record_achievements(self, player_id, 1, 0, 0)

        if number == 1 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125799224966131712)
        elif number == 10 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125799392989949973)
        elif number == 30 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125799470509084762)

        return True

    global add_butcher
    async def add_butcher(self, ctx, player: discord.member):

        player_id = int(player.id)
        number = 1

        sql = f"SELECT BUTCHERS FROM ACHIEVEMENTS WHERE PLAYER_ID = {player_id};"

        for retries in range(0,3):
            try:
                butchers = await self.bot.pg_con.fetch(sql)
                break
            except:
                butchers = None
        else:
            pass

        if butchers:
            if butchers[0][0] is None:
                print("Player exists and has butchers, we can update dabatase..")
                sql = f"""UPDATE ACHIEVEMENTS SET BUTCHERS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
            else:
                print("Player exists and has some butchers, we can update dabatase..")
                number += butchers[0][0]
                sql = f"""UPDATE ACHIEVEMENTS SET BUTCHERS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
        else:
            print("Player doest not exists in ACHIEVEMENTS database, we can update dabatase..")
            await new_record_achievements(self, player_id, 0, 0, 1)

        if number == 1 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125801055758860329)
        elif number == 3 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125801161631486033)
        elif number == 10 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125801216375529563)

        return True

    global add_hunts
    async def add_hunts(self, ctx, player: discord.member):

        player_id = int(player.id)
        number = 1

        sql = f"SELECT HUNTS FROM ACHIEVEMENTS WHERE PLAYER_ID = {player_id};"

        for retries in range(0,3):
            try:
                hunts = await self.bot.pg_con.fetch(sql)
                break
            except:
                hunts = None
        else:
            pass

        if hunts:
            if hunts[0][0] is None:
                print("Player exists and has hybts, we can update dabatase..")
                sql = f"""UPDATE ACHIEVEMENTS SET HUNTS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
            else:
                print("Player exists and has some hunts, we can update dabatase..")
                number += hunts[0][0]
                sql = f"""UPDATE ACHIEVEMENTS SET HUNTS = {number}
                WHERE PLAYER_ID = {player_id};"""
                await self.bot.pg_con.fetch(sql)
        else:
            print("Player doest not exists in ACHIEVEMENTS database, we can update dabatase..")
            await new_record_achievements(self, player_id, 0, 1, 0)

        if number == 10 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125799891923374121)
        elif number == 35 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125800644826112043)
        elif number == 100 and functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125800721724489758)

        return True

    global unicorn
    async def unicorn(self, ctx, player: discord.member):

        if functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125819170534211664)

        return True

    global legend
    async def legend(self, ctx, player: discord.member):

        if functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125818827809226862)

        return True

    global quickness
    async def quickness(self, ctx, player: discord.member):

        if functions_patrons.check_if_patron(self, ctx, player):
            await set_achiev_role(self, ctx, player, 1125818642450370590)

        return True


    #function to add achievement role
    global set_achiev_role
    async def set_achiev_role(self, ctx, player: discord.member, role_id: int):
        """Assign role to the member.
        """

        my_role = discord.utils.get(ctx.guild.roles, id=role_id)
        members = my_role.members

        roles_to_remove = []
        # Boss killing roles
        if role_id == 1125799392989949973:
            roles_to_remove = [1125799224966131712]
        elif role_id == 1125799470509084762:
            roles_to_remove = [1125799392989949973, 1125799224966131712]\

        # Hunting roles
        elif role_id == 1125800644826112043:
            roles_to_remove = [1125799891923374121]
        elif role_id == 1125800721724489758:
            roles_to_remove = [1125799891923374121, 1125800644826112043]

        # Butcher roles
        elif role_id == 1125801161631486033:
            roles_to_remove = [1125801055758860329]
        elif role_id == 1125801216375529563:
            roles_to_remove = [1125801055758860329, 1125801161631486033]

        # Death tower roles
        elif role_id == 1151427619741511710:
            roles_to_remove = [1151427369886826496]
        elif role_id == 1151427790973964328:
            roles_to_remove = [1151427369886826496, 1151427619741511710]

        guild = self.bot.get_guild(686137998177206281)
        user = guild.get_member(int(player.id))
        if user not in members:
            await user.add_roles(my_role)
            await ctx.channel.send(f"<@{player.id}> zdobywa osiągnięcie: {my_role}!")

            for remove_role_id in roles_to_remove:
                role_to_remove = discord.utils.get(ctx.guild.roles, id=remove_role_id)
                remove_role_members = role_to_remove.members
                if user in remove_role_members:
                    await user.remove_roles(role_to_remove)

async def new_record_achievements(self, player_id: int, boss_kills: int, hunts: bool,
                                butchers: int):
    """New record of user in achievements database."""

    sql=f"""INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS, HUNTS,
    BUTCHERS)
    VALUES ({player_id},{boss_kills},{hunts},{butchers});"""

    for retries in range(0,3):
        try:
            await self.bot.pg_con.fetch(sql)
            break
        except:
            pass
    else:
        return False

def setup(bot):
    """Load the FunctionsAchievements cog."""
    bot.add_cog(FunctionsAchievements(bot))
