from discord.ext import commands
from datetime import datetime

# general bag for functions

class functions_general(commands.Cog, name="functions_general"):
    def __init__(self, bot):
        self.bot = bot

    #function to clear
    global fClear
    async def fClear(self, ctx):

        # OLD VERSION
        # channel = ctx.channel
        # count = 0
        # async for _ in channel.history(limit=None):
        #     count += 1
        # if count > 1:
        #     await channel.purge(limit=count-1)
        try:
            channel = ctx.channel

            messages = [msg async for msg in channel.history(limit=1000, oldest_first=True)]
            if len(messages) <= 1:
                return

            messages_to_delete = [
                msg for msg in messages[1:]
                if not msg.pinned
            ]

            print(len(messages_to_delete))
            await channel.purge(limit=None, check=lambda m: m in messages_to_delete)
        except Exception as e:
            print("Problem with deleting messages %s", e)

    global victory_bar
    def victory_bar(a: int, b: int, length: int = 20) -> str:
        """
        Tworzy pasek przewagi między dwiema liczbami.
        a - wartość gracza A
        b - wartość gracza B
        length - całkowita długość paska
        """
        total = a + b
        if total == 0:
            return "⚖️ " + "▬" * length + " ⚖️"  # brak przewagi

        # ile "pól" zajmuje gracz A
        a_len = round((a / total) * length)
        b_len = length - a_len

        bar = "🟥" * a_len + "🟦" * b_len  # np. czerwony vs niebieski
        return f"{bar}"

def setup(bot):
    bot.add_cog(functions_general(bot))
