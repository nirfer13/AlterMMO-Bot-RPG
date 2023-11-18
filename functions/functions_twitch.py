"""File to sum up the experience from drops."""

import json
import asyncio
import discord
from discord.ext import commands
import asyncio

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsTwitch(commands.Cog, name="FunctionsTwitch"):
    """Twitch class, which is used to integrate Twitch accounts with Discord."""
    def __init__(self, bot):
        self.bot = bot

    global create_ttv_dc_database
    async def create_ttv_dc_database(self):
        """Create discord_twitch table if does not exist."""

        table_creation = """CREATE TABLE IF NOT EXISTS discord_twitch (
        DISCORD_ID  SERIAL PRIMARY KEY,
        TWITCH_NAME TEXT NOT NULL
        ); """
        await self.bot.pg_con.execute(table_creation)
        print("Table discord_twitch created successfully.")

    global assign_roles_watchtime
    async def assign_roles_watchtime(self):
        """Get list of users who have enough watchtime to receive new role."""

        #Database Reading
        db_ranking_twitch = await self.bot.pg_con.fetch("SELECT discord_id, chtr_time, chtr_issub" +
                                        " FROM  chatters_information WHERE chtr_time > 45720 " +
                                        " AND discord_id IS NOT NULL")
        # 180 000

        for user_id, time, is_sub in db_ranking_twitch:
            print(f"Users: {user_id} - time: {time/60/60} - is_sub: {is_sub}")
            if user_id:
                # Get full user object
                user = await self.bot.fetch_user(user_id)
                # Get discord server object
                guild = self.bot.get_guild(686137998177206281)

                role_id, roles_to_remove = get_twitch_viewer_role(time, is_sub)

                # Check if user belongs to server and should receive a role
                if guild.get_member(user.id) is not None and role_id > 0:

                    # Get specified role object
                    my_role = discord.utils.get(guild.roles, id=role_id)
                    members = my_role.members

                    # Check if user already has role
                    if user not in members:
                        print(type(user))
                        user = guild.get_member(user.id)
                        print(type(user))
                        await user.add_roles(my_role)

                        # Remove previously added roles
                        for remove_role_id in roles_to_remove:
                            role_to_remove = discord.utils.get(guild.roles, id=remove_role_id)
                            remove_role_members = role_to_remove.members
                            if user in remove_role_members:
                                await user.remove_roles(role_to_remove)

                        # Information about role acquired
                        if DebugMode:
                            chat_channel = self.bot.get_channel(881090112576962560)
                        else:
                            chat_channel = self.bot.get_channel(776379796367212594)
                        await chat_channel.send(f"<@{user.id}> zdobywa rolę {my_role} za " +
                                                "śledzenie streamów na Twitchu!")

    global assign_roles_messages
    async def assign_roles_messages(self):
        """Get list of users who have enough messages to receive new role."""

        #Database Reading
        db_ranking_twitch = await self.bot.pg_con.fetch("SELECT discord_id, chtr_comments, " +
                                "chtr_issub FROM  chatters_information WHERE chtr_time > 45720 " +
                                " AND discord_id IS NOT NULL")
        # 180 000

        for user_id, messages, is_sub in db_ranking_twitch:
            print(f"Users: {user_id} - messages: {messages} - is_sub: {is_sub}")
            if user_id:
                # Get full user object
                user = await self.bot.fetch_user(user_id)
                # Get discord server object
                guild = self.bot.get_guild(686137998177206281)

                role_id, roles_to_remove = get_twitch_messager_role(messages, is_sub)

                # Check if user belongs to server and should receive a role
                if guild.get_member(user.id) is not None and role_id > 0:

                    # Get specified role object
                    my_role = discord.utils.get(guild.roles, id=role_id)
                    members = my_role.members

                    # Check if user already has role
                    if user not in members:
                        print(type(user))
                        user = guild.get_member(user.id)
                        print(type(user))
                        await user.add_roles(my_role)

                        # Remove previously added roles
                        for remove_role_id in roles_to_remove:
                            role_to_remove = discord.utils.get(guild.roles, id=remove_role_id)
                            remove_role_members = role_to_remove.members
                            if user in remove_role_members:
                                await user.remove_roles(role_to_remove)

                        # Information about role acquired
                        if DebugMode:
                            chat_channel = self.bot.get_channel(881090112576962560)
                        else:
                            chat_channel = self.bot.get_channel(776379796367212594)
                        await chat_channel.send(f"<@{user.id}> zdobywa rolę {my_role} za " +
                                                "pisanie na Twitchu!")

def get_twitch_viewer_role(watchtime: int, is_sub: bool):
    """Select proper viewer role to assign based on watchtime of the user."""

    # Podgladacz (1044570191239057538) - 125h - 50h (chtr_issub)
    # Ogladacz (1044570461511622716) - 250h - 100h (chtr_issub)
    # Widz (1044570672313147442) - 375 - 150 (chtr_issub)
    # Pasjonata (1044570838365638667) - 500 - 200 (chtr_issub)
    # Fanboy (1175454109705441410) - 750 - 300 (chtr_issub)

    watchtime_h = watchtime / 60 / 60
    if is_sub:
        if watchtime_h >= 50 and watchtime_h < 100:
            role_id = 1044570191239057538
        elif watchtime_h >= 100 and watchtime_h < 150:
            role_id = 1044570461511622716
        elif watchtime_h >= 150 and watchtime_h < 200:
            role_id = 1044570672313147442
        elif watchtime_h >= 200 and watchtime_h < 300:
            role_id = 1044570838365638667
        elif watchtime_h >= 300:
            role_id = 1175454109705441410
        else:
            role_id = 0
    else:
        if watchtime_h >= 125 and watchtime_h < 250:
            role_id = 1044570191239057538
        elif watchtime_h >= 250 and watchtime_h < 375:
            role_id = 1044570461511622716
        elif watchtime_h >= 375 and watchtime_h < 500:
            role_id = 1044570672313147442
        elif watchtime_h >= 500 and watchtime_h < 750:
            role_id = 1044570838365638667
        elif watchtime_h >= 750:
            role_id = 1175454109705441410
        else:
            role_id = 0

    roles_to_remove = [1044570191239057538,
                    1044570461511622716,
                    1044570672313147442,
                    1044570838365638667,
                    1175454109705441410]

    if role_id in roles_to_remove:
        roles_to_remove.remove(role_id)

    return role_id, roles_to_remove

def get_twitch_messager_role(messages: int, is_sub: bool):
    """Select proper messager role to assign based on messages of the user."""

    # Analfabeta (1175474239890010134) - 300 - 200 (chtr_issub)
    # Niepisaty (1175474471637893221) - 600 - 350 (chtr_issub)
    # Czatownik (1175474738789875854) - 1250 - 625 (chtr_issub)
    # Skryba (1175474928162721833) - 2500 - 1250 (chtr_issub)
    # Spamer (1175475198854709330) - 5000 - 2500 (chtr_issub)

    if is_sub:
        if messages >= 200 and messages < 250:
            role_id = 1175474239890010134
        elif messages >= 280 and messages < 625:
            role_id = 1175474471637893221
        elif messages >= 625 and messages < 1250:
            role_id = 1175474738789875854
        elif messages >= 1250 and messages < 2500:
            role_id = 1175474928162721833
        elif messages >= 2500:
            role_id = 1175475198854709330
        else:
            role_id = 0
    else:
        if messages >= 300 and messages < 600:
            role_id = 1175474239890010134
        elif messages >= 600 and messages < 1250:
            role_id = 1175474471637893221
        elif messages >= 1250 and messages < 2500:
            role_id = 1175474738789875854
        elif messages >= 2500 and messages < 5000:
            role_id = 1175474928162721833
        elif messages >= 5000:
            role_id = 1175475198854709330
        else:
            role_id = 0

    roles_to_remove = [1175474239890010134,
                    1175474471637893221,
                    1175474738789875854,
                    1175474928162721833,
                    1175475198854709330]

    if role_id in roles_to_remove:
        roles_to_remove.remove(role_id)

    return role_id, roles_to_remove

def setup(bot):

    """Load the FunctionsTwitchcog."""
    bot.add_cog(FunctionsTwitch(bot))
