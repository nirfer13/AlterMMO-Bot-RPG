"""File to sum up the experience from drops."""

import json
import discord
from discord.ext import commands
import asyncio
import random
import asyncpg

import functions_daily
import functions_boss

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
                                        " FROM  chatters_information WHERE chtr_time > 445000 " +
                                        " AND discord_id IS NOT NULL")
        # 450 000

        user_dict = {}
        for user_id, time, is_sub in db_ranking_twitch:
            print(f"Users: {user_id} - time: {time/60/60} - is_sub: {is_sub}")
            if user_id:

                # Check if there is another, same user_id with more messages
                if user_id in user_dict:
                    if time < user_dict[user_id]:
                        continue

                # Add user to dictionary
                user_dict[user_id] = time

                # Get full user object
                user = await self.bot.fetch_user(user_id)
                # Get discord server object
                guild = self.bot.get_guild(686137998177206281)

                role_id, roles_to_remove = get_twitch_viewer_role(time)

                # Check if user belongs to server and should receive a role
                if guild.get_member(user.id) is not None and role_id > 0:

                    # Get specified role object
                    my_role = discord.utils.get(guild.roles, id=role_id)
                    members = my_role.members

                    # Check if user already has role
                    if user not in members:

                         # Check if user has better role
                        role1 = discord.utils.get(guild.roles, id=1044570191239057538)
                        role2 = discord.utils.get(guild.roles, id=1044570461511622716)
                        role3 = discord.utils.get(guild.roles, id=1044570672313147442)
                        role4 = discord.utils.get(guild.roles, id=1044570838365638667)
                        role5 = discord.utils.get(guild.roles, id=1175454109705441410)

                        roles = [role1, role2, role3, role4, role5]

                        # Role to assign index
                        new_index = roles.index(my_role)

                        # Current role index
                        role_index = 0
                        current_role_index = -1
                        for role in roles:
                            if user in role.members:
                                current_role_index = role_index
                            role_index += 1

                        if new_index > current_role_index:

                            user = guild.get_member(user.id)
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
                                "chtr_isvip, chtr_ismod FROM chatters_information WHERE chtr_comments > 299 " +
                                " AND discord_id IS NOT NULL")
        # 180 000

        user_dict = {}
        for user_id, messages, is_vip, is_mod in db_ranking_twitch:

            if user_id:

                # Check if there is another, same user_id with more messages
                if user_id in user_dict:
                    if messages < user_dict[user_id]:
                        continue

                print(f"Users: {user_id} - messages: {messages} - is_vip: {is_vip} - is_mod: {is_mod}")
                # Add user to dictionary
                user_dict[user_id] = messages

                # Get full user object
                user = await self.bot.fetch_user(user_id)
                # Get discord server object
                guild = self.bot.get_guild(686137998177206281)

                role_id, roles_to_remove = get_twitch_messager_role(messages)
                # Check if user belongs to server and should receive a role
                if guild.get_member(user.id) is not None and role_id > 0:

                    # Get specified role object
                    my_role = discord.utils.get(guild.roles, id=role_id)
                    members = my_role.members

                    # Check if user already has role
                    if user not in members:

                        user = guild.get_member(user.id)
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
                        
    global assign_roles_vip_mod
    async def assign_roles_vip_mod(self):
        """Get list of users who have vip or mod role on Twitch."""

        #Database Reading
        db_ranking_twitch = await self.bot.pg_con.fetch("SELECT discord_id, chtr_isvip, chtr_ismod" +
                                        " FROM  chatters_information WHERE (chtr_isvip = TRUE OR chtr_ismod = TRUE) " +
                                        " AND discord_id IS NOT NULL")

        user_dict = {}
        for user_id, is_vip, is_mod in db_ranking_twitch:
            print(f"Users: {user_id} - is_vip: {is_vip} - is_mod: {is_mod}")
            if user_id:

                # Add user to dictionary
                user_dict[user_id] = is_vip

                # Get full user object
                user = await self.bot.fetch_user(user_id)
                # Get discord server object
                guild = self.bot.get_guild(686137998177206281)

                if is_vip:
                    role_id = 964845150213906442
                elif is_mod:
                    role_id = 969683014709825627

                # Check if user belongs to server and should receive a role
                if guild.get_member(user.id) is not None and role_id > 0:

                    # Get specified role object
                    my_role = discord.utils.get(guild.roles, id=role_id)
                    members = my_role.members

                    # Check if user already has role
                    if user not in members:

                        user = guild.get_member(user.id)
                        await user.add_roles(my_role)

                        # Information about role acquired
                        if DebugMode:
                            chat_channel = self.bot.get_channel(881090112576962560)
                        else:
                            chat_channel = self.bot.get_channel(776379796367212594)

                        if is_vip:
                            await chat_channel.send(f"<@{user.id}> zdobywa rolę {my_role} za " +
                                                    "VIPa na Twitchu!")
                        elif is_mod:
                            await chat_channel.send(f"<@{user.id}> zdobywa rolę {my_role} za " +
                                                    "moderatora na Twitchu!")

    global check_treasure
    async def check_treasure(self):
        """Get list of users who should receive tresure from twitch"""


        db_treasure_twitch = await self.bot.pg_con.fetch("SELECT chtr_name" +
                                " FROM treasure_table ORDER BY chtr_id ASC")

        await self.bot.pg_con.fetch("DELETE FROM treasure_table")

        print(db_treasure_twitch)
        for user in db_treasure_twitch:

            db_discord_id = await self.bot.pg_con.fetch("SELECT discord_id" +
                                f" FROM chatters_information WHERE chtr_name=\'{user[0]}\'")

            if DebugMode is True:
                chat_channel = self.bot.get_channel(970571647226642442)
                ctx = await functions_boss.getContext(self, 970571647226642442, 1125837611299254383)
            else:
                chat_channel = self.bot.get_channel(970684202880204831)
                ctx = await functions_boss.getContext(self, 970684202880204831, 1028328642436136961)


            print(db_discord_id[0][0])
            user = await self.bot.fetch_user(db_discord_id[0][0])
            boost_percent = random.randint(0,25)
            await functions_daily.randLoot(self, ctx, 2, user, boost_percent)

def get_twitch_viewer_role(watchtime: int):
    """Select proper viewer role to assign based on watchtime of the user."""

    # Podgladacz (1044570191239057538) - 125
    # Ogladacz (1044570461511622716) - 250
    # Widz (1044570672313147442) - 375
    # Pasjonata (1044570838365638667) - 500
    # Fanboy (1175454109705441410) - 750

    watchtime_h = watchtime / 60 / 60

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

def get_twitch_messager_role(messages: int):
    """Select proper messager role to assign based on messages of the user."""

    # Analfabeta (1175474239890010134) - 300 - 200 (chtr_issub)
    # Niepisaty (1175474471637893221) - 600 - 350 (chtr_issub)
    # Czatownik (1175474738789875854) - 1250 - 625 (chtr_issub)
    # Skryba (1175474928162721833) - 2500 - 1250 (chtr_issub)
    # Wieszcz (1175475198854709330) - 5000 - 2500 (chtr_issub)

    if messages >= 1000 and messages < 3600:
        role_id = 1175474239890010134
    elif messages >= 3600 and messages < 7500:
        role_id = 1175474471637893221
    elif messages >= 7500 and messages < 15000:
        role_id = 1175474738789875854
    elif messages >= 15000 and messages < 30000:
        role_id = 1175474928162721833
    elif messages >= 30000:
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
