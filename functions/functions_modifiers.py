"""File with loading/saving all modifiers used during boss fight."""

import json
import discord
import random
from discord.ext import commands

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsModifiers(commands.Cog, name="FunctionsModifiers"):
    """File with loading/saving all modifiers used during boss fight."""
    def __init__(self, bot):
        self.bot = bot

    global spawn_modifier_shrine
    async def spawn_modifier_shrine(self, ctx):
        """Function to spawn shrine modifier."""

        print("Boss is dead, so shrine is spawned.")

        e_title = "🕯️ Kapliczka zmian! 🕯️"

        e_descr = 'Oto kapliczka zmian, która wpłynie na następnego bossa! '\
        'Jeśli chcesz wpłynąć na następnego bossa, to pomódl się wpisując **$modlitwa**.\n\n'\
        '*Tylko Crafterzy i Patroni znają odpowiednią modlitwę, ale każdy może skorzystać'\
        ' z błogosławieństw rzuconych na potwora.*'

        e_color = 0x609AF7

        e_thumb = 'https://www.altermmo.pl/wp-content/uploads/Prayge.png'

        image_name = "shrines/" + str(random.randint(0,3)) + ".png"
        file=discord.File(image_name)

        #image
        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)
        embed.set_thumbnail(url=e_thumb)

        await ctx.channel.send(file=file)
        await ctx.send(embed=embed)

    global random_modifiers
    async def random_modifiers(self, ctx):
        """Random modifiers and save it to the file."""

        modifiers ={
        "hp_reduced_perc": 0,
        "hp_boost_perc": 0,
        "drop_boost_perc": 0,
        "time_reduced_perc": 0,
        "rarity_boost": 0,
        "ban_loser": 0,
        "points_boost": 0,
        "player_id": 0
        }

        modifier = random.choice(list(modifiers.keys()))

        if modifier == "hp_reduced_perc":
            random_value = random.randint(5, 15)
            desc = "Modlitwa w kapliczce sprawiła, że "\
            "następny boss **będzie miał o " + str(random_value) + "% mniej życia!**"
        elif modifier == "hp_boost_perc":
            random_value = random.randint(5, 15)
            desc = "Modlitwa w kapliczce sprawiła, że "\
            "następny boss **będzie miał o " + str(random_value) + "% więcej życia!**"
        elif modifier == "rarity_boost":
            random_value = 1
            desc = "Modlitwa w kapliczce sprawiła, że "\
            "następny boss **będzie o " + str(random_value) + " poziom rzadszy!**"
        elif modifier == "drop_boost_perc":
            random_value = random.randint(5, 25)
            desc = "Modlitwa w kapliczce sprawiła, że "\
            "następny boss **będzie miał o " + str(random_value) + "% rzadsze przedmioty!**"
        elif modifier == "player_id":
            loaded_modifiers = await load_modifiers(self, ctx)
            if loaded_modifiers["player_id"] == 0:
                random_value = ctx.author.id
                desc = "Modlitwa w kapliczce sprawiła, że "\
                "**zostaniesz następny bossem <@" + str(ctx.author.id) + ">!**"
            else:
                random_value = loaded_modifiers["player_id"]
                desc = "Modlitwa nie odnosi żadnego skutku..."
        elif modifier == "ban_loser":
            random_value = 1
            desc = "Modlitwa w kapliczce sprawiła, że "\
            "**następny gracz, który przegra z bossem zostanie zbanowany do następnego bossa!**"
        elif modifier == "time_reduced_perc":
            random_value = random.randint(5, 25)
            desc = "Modlitwa w kapliczce sprawiła, że "\
            "podczas walki z następnym bossem **będzie o " + str(random_value) + "% mniej czasu na reakcję!**"
        elif modifier == "points_boost":
            random_value = random.randint(1, 5)
            desc = "Modlitwa w kapliczce sprawiła, że "\
            "za wygraną z kolejnym bossem **będzie o " + str(random_value) + " więcej punktów!**"

        await modify_modifiers(self, ctx, modifier, random_value)

        title = 'Błogosławieństwo kapliczki!'
        #Embed create
        embed=discord.Embed(title=title,
                            url='https://www.altermmo.pl/wp-content/uploads/Prayge.png',
                            description=desc, color=0x609AF7)
        embed.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/Prayge.png')
        embed.set_footer(text='Powodzenia!')
        await ctx.channel.send(embed=embed)


    #function to init modifiers dictionaries
    global init_modifiers
    async def init_modifiers(self, ctx):
        """Creates the file with all modifiers (set to 0) used during boss fight."""

        modifiers ={
        "hp_reduced_perc": 0,
        "hp_boost_perc": 0,
        "drop_boost_perc": 0,
        "time_reduced_perc": 0,
        "rarity_boost": 0,
        "ban_loser": 0,
        "points_boost": 0,
        "player_id": 0
        }

        with open('bossModifier.json', 'w', encoding="utf-8") as file:
            json.dump(modifiers, file)

    global load_modifiers
    async def load_modifiers(self, ctx):
        """Load modifiers from file and return them. 
        Should be called in the beginning of the fight."""

        with open('bossModifier.json', 'r', encoding="utf-8") as file:
            modifiers = json.load(file)

        print(modifiers)

        return modifiers
    
    global load_desc_modifiers
    async def load_desc_modifiers(self, ctx):
        """Load modifiers from file and return them as description."""

        with open('bossModifier.json', 'r', encoding="utf-8") as file:
            modifiers = json.load(file)

        print(modifiers)

        modifiers_desc = ""
        init = False
        for key, value in modifiers.items():
            if value > 0 and init is False:
                init = True
                modifiers_desc += "\n\nDodatkowe statystyki:"
            if value > 0:
                if key == "hp_reduced_perc":
                    modifiers_desc+= f"\n🔺 Życie jest zmniejszone o {value} %"
                elif key == "hp_boost_perc":
                    modifiers_desc+= f"\n🔻 Życie jest zwiększone o {value} %"
                elif key == "drop_boost_perc":
                    modifiers_desc+= f"\n🔺 Drop zwiększony o {value} %"
                elif key == "time_reduced_perc":
                    modifiers_desc+= f"\n🔻 Czas na reakcję zmniejszony o {value} %"
                elif key == "rarity_boost":
                    modifiers_desc+= f"\n🔺 Rzadkość zwiększona o {value} poziom"
                elif key == "points_boost":
                    modifiers_desc+= f"\n🔺 Punkty za wygraną zwiększone o {value}"
                elif key == "player_id":
                    modifiers_desc+= "\n🔺 Bossem będzie gracz <@" + str(value) + ">"
                elif key == "ban_loser":
                    modifiers_desc+= "\n🔻 Przegrany zostanie zbanowany"

        print(modifiers_desc)

        return modifiers_desc

    global modify_modifiers
    async def modify_modifiers(self, ctx, modifier_name, value):
        """Modifies a modifier in the modifiers dictionary loaded from the file."""

        value = int(value)

        with open('bossModifier.json', 'r', encoding="utf-8") as file:
            modifiers = json.load(file)

        file.close()

        modifiers[modifier_name] += value
        print(modifiers)

        with open('bossModifier.json', 'w', encoding="utf-8") as file:
            json.dump(modifiers, file)

def setup(bot):
    """Load the functions_modifiers cog."""
    bot.add_cog(FunctionsModifiers(bot))
