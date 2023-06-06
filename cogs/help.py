from discord.ext import commands
import discord
from datetime import datetime, timezone, timedelta
import textwrap

# Bag for help command

class CheckCommands(commands.Cog, name="CheckCommands"):
    def __init__(self, bot):
        self.bot = bot

    # Check bot commands.
    @commands.command(name="pomoc", alias=["komendy"], brief="Send all available commands.")
    @commands.cooldown(1, 600, commands.BucketType.user)
    async def help(self, ctx):
        embed = discord.Embed(
            title="<:PepoG:790963160528977980> Komendy dostępne w grze <:PepoG:790963160528977980>",
            description=textwrap.dedent("""Respawn bossa może trwać od kilkunastu minut do kilkunastu godzin, wiec warto zaglądać co jakiś czas na Discorda. Im dłużej boss się respi, tym potężniejszy będzie i tym lepszy drop zaoferuje.

Jeśli brakuje Wam graczy, to warto zawołać rolę <@&985071779787730944>. Do roli tej można przypisać się na kanale <#719989249956380793>.

<@&687185998550925312> oraz Patroni mogą zostać bossami. Jest to losowe. Jednak jeśli taki boss pokona gracza, to użytkownik Discorda, który owym bossem był, zgarnia dodatkowe doświadczenie.

Dostępne komendy (dla wszystkich):
- **$polowanie - Polujesz na zwykłe stwory, możliwe raz dziennie lub po odpowiedniej modlitwie w kapliczce**. Komenda, od której warto zacząć przygodę.

- $zaatakuj - Atakuje bossa, gdy ten czeka na tym kanale,
- $kiedy - Podaje kto i kiedy ostatnio walczył z bossem,
- $rekord - Podaje kto najszybciej i w jakim czasie pokonał bossa,
- $ranking - Podaje ranking łowców potworów,
- $towarzysz - Sprawdź swojego towarzysza,
- $porzucam - Porzucasz obecnego towarzysza.
- $nazwij "Nowe imię" - Nazywasz swojego towarzysza.
- $schowajtowarzysza numer_slota - Chowa towarzysza do stajni i do podanego slota (1 lub 2).
- $wyciagnijtowarzysza numer_slota - Wyciąga towarzysza ze stajni z podanego slota (1 lub 2).
- $stajnia - Sprawdza stajnie z towarzyszami,
- $odrodzenie - Przelosowuje wszystkie statystyki aktualnie wybranego towarzysza,
- $oswiecenie - Zwiększa talent towarzysza,
- $transformacja - Zmienia wygląd towarzysza,
- $rankingtowarzyszy - Lista najbardziej utalentowanych towarzyszy.

Komendy dla <@&983798433590673448>:
- $flex - Pokaż innym jakim jesteś koxem,
- $kolor "kolor w kodowaniu hexadecymalnym" - Zmienia kolor rangi <@&983798433590673448> np. $kolor "00FFFF".

Komendy dla <@&687185998550925312> i patronów:
- $modlitwa - Gdy pojawi się kapliczka, to za sprawą modlitwy można wpłynąć na statystyki kolejnego bossa.

Więcej informacji w wiadomości https://discord.com/channels/686137998177206281/1093905013480378519/1093905013480378519."""),
            color=0x42F34C)
        await ctx.send(embed=embed)

    @help.error
    async def helpcommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało '
                           + str(round(error.retry_after/60, 2)) +
                           ' minut <:Bedge:970576892874854400>.')

def setup(bot):
    bot.add_cog(CheckCommands(bot))
