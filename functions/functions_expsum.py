"""File to sum up the experience from drops."""

import json
import asyncio
import discord
from discord.ext import commands
import asyncio

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsExpsum(commands.Cog, name="FunctionsExpsum"):
    """Exp summary class, which is used to sum up the experience from drops."""
    def __init__(self, bot):
        self.bot = bot

    global init_file_exp
    def init_file_exp(self):
        """Reset the file with experience."""

        modifiers ={
        "1234212213": 0
        }

        with open('expSummary.json', 'w', encoding="utf-8") as file:
            json.dump(modifiers, file)

    global add_file_exp
    def add_file_exp(self, player_id, experience):
        """Check if an ID exists, then add exp."""

        with open('expSummary.json', 'r', encoding="utf-8") as file:
            players = json.load(file)

        player_id = str(player_id)
        file.close()

        if player_id in players:
            players[player_id] += experience
        else:
            players[player_id] = experience

        with open('expSummary.json', 'w', encoding="utf-8") as file:
            json.dump(players, file)

    global show_file_exp
    async def show_file_exp(self, ctx):
        """Show the table with an exp summary."""

        with open('expSummary.json', 'r', encoding="utf-8") as file:
            players = json.load(file)

        text = []
        for key, value in players.items():
            text.append(f"<@{key}>: {value}")

        exp_table = '\n'.join(text)
        await ctx.send(exp_table)

def setup(bot):
    """Load the FunctionsExpsum cog."""
    bot.add_cog(FunctionsExpsum(bot))
