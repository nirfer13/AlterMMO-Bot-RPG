"""File with loading/saving all modifiers used during boss fight."""

import json
from discord.ext import commands

#Import Globals
from globals.globalvariables import DebugMode

class functions_modifiers(commands.Cog, name="functions_modifiers"):
    """File with loading/saving all modifiers used during boss fight."""
    def __init__(self, bot):
        self.bot = bot

    #function to init modifiers dictionaries
    global init_modifiers
    async def init_modifiers(self, ctx):
        """Creates the file with all modifiers used during boss fight."""

        modifiers ={
        "hp_reduced": 0,
        "hp_boost": 0,
        "rarity_boost": 0,
        "drop_boost": 0,
        "player_id": 0,
        "ban_loser": 0,
        "time_reduced": 0,
        "points_boost": 0 
        }

        with open('bossModifier.json', 'w', encoding="utf-8") as file:
            json.dump(modifiers, file)

    global load_modifiers
    async def load_modifiers(self, ctx):
        with open('bossModifier.json', 'r', encoding="utf-8") as file:
            modifiers = json.load(file)
            
        print(modifiers)

        return modifiers

    global modify_modifiers
    async def modify_modifiers(self, ctx, modifier_name, value):
        """Modifies a modifier in the modifiers dictionary loaded from the file."""

        value = int(value)

        with open('bossModifier.json', 'r', encoding="utf-8") as file:
            modifiers = json.load(file)

        file.close()

        print(modifiers)

        modifiers[modifier_name] = value

        with open('bossModifier.json', 'w', encoding="utf-8") as file:
            json.dump(modifiers, file)

def setup(bot):
    """Load the functions_modifiers cog."""
    bot.add_cog(functions_modifiers(bot))
