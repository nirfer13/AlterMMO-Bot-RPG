"""Class with all functions used for patrons."""

import random
from discord.ext import commands
import discord

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsPatrons(commands.Cog, name="FunctionsPatrons"):
    """Class with all functions used for patrons."""
    def __init__(self, bot):
        self.bot = bot

    global get_patron
    def get_patron(self, ctx):
        """Function to get random member in patron/crafter roles.
        """

        crafter = discord.utils.get(ctx.guild.roles, id=687185998550925312)
        patron1 = discord.utils.get(ctx.guild.roles, id=1113402734280970331)
        patron2 = discord.utils.get(ctx.guild.roles, id=1113402836705890355)
        patron3 = discord.utils.get(ctx.guild.roles, id=1113403087734980608)
        patron4 = discord.utils.get(ctx.guild.roles, id=1113403223508779068)
        members = crafter.members + patron1.members + patron2.members +\
        patron3.members + patron4.members
        print("Members granted")
        return random.choice(members)

    global check_if_patron
    def check_if_patron(self, ctx, player: discord.member):
        """Check if player is a patron or crafter."""

        crafter = discord.utils.get(ctx.guild.roles, id=687185998550925312)
        patron1 = discord.utils.get(ctx.guild.roles, id=1113402734280970331)
        patron2 = discord.utils.get(ctx.guild.roles, id=1113402836705890355)
        patron3 = discord.utils.get(ctx.guild.roles, id=1113403087734980608)
        patron4 = discord.utils.get(ctx.guild.roles, id=1113403223508779068)
        if crafter in player.roles or patron1 in player.roles or patron2 in player.roles or\
        patron3 in player.roles or patron4 in player.roles:
            return True
        else:
            return False

def setup(bot):
    """Load the FunctionsPatrons cog."""
    bot.add_cog(FunctionsPatrons(bot))
