"""``$pomoc`` - the list of player commands."""

from __future__ import annotations

import discord
from discord.ext import commands

from ..core import io

HELP_TITLE = "<:PepoG:790963160528977980> Komendy dostępne w grze <:PepoG:790963160528977980>"
HELP_TEXT = """Respawn bossa może trwać od kilkunastu minut do kilkunastu godzin, wiec warto zaglądać co jakiś czas na Discorda. Im dłużej boss się respi, tym potężniejszy będzie i tym lepszy drop zaoferuje.

Jeśli brakuje Wam graczy, to warto zawołać rolę <@&985071779787730944>. Do roli tej można przypisać się na kanale <#719989249956380793>.

<@&687185998550925312> oraz Patroni mogą zostać bossami. Jest to losowe. Jednak jeśli taki boss pokona gracza, to użytkownik Discorda, który owym bossem był, zgarnia dodatkowe doświadczenie.

Dostępne komendy (dla wszystkich):
- **$polowanie - Polujesz na zwykłe stwory, możliwe raz dziennie lub po odpowiedniej modlitwie w kapliczce**. Komenda, od której warto zacząć przygodę. Skrót $p.
- **$wieża - Wyruszasz na eksplorację wieży śmierci. Im wyżej, tym trudniej**. Dostępna raz w tygodniu.                               

- $zaatakuj - Atakuje bossa, gdy ten czeka na tym kanale.
- $kiedy - Podaje kto i kiedy ostatnio walczył z bossem.
- $rekord - Podaje kto najszybciej i w jakim czasie pokonał bossa.
- $ranking - Podaje sezonowy (tygodniowy) ranking łowców potworów. Skrót $r.

- $towarzysz - Sprawdź swojego towarzysza. Skrót $t.
- $porzucam - Porzucasz obecnego towarzysza.
- $nazwij "Nowe imię" - Nazywasz swojego towarzysza.
- $schowajtowarzysza numer_slota - Chowa towarzysza do stajni i do podanego slota (1-4). Skrót $st.
- $wyciagnijtowarzysza numer_slota - Wyciąga towarzysza ze stajni z podanego slota (1-4). Skrót $wt.
- $stajnia - Sprawdza stajnie z towarzyszami. Skrót $s.
- $odrodzenie - Przelosowuje wszystkie statystyki aktualnie wybranego towarzysza,
- $mocneodrodzenie - Bardziej zaawansowany sposób przelosowania statystyk.
- $oswiecenie - Zwiększa talent towarzysza.
- $rankingtowarzyszy - Lista najbardziej utalentowanych towarzyszy. Skrót $rt.
- $zabojca - Wynajmuje zabójcę na jednego z graczy w rankingu.
- $kradnijxp nick - Kradnie doświadczenie wybranemu graczowi (nagroda z bossa).
                                        
- $pokażumiejętność - Prezentuje klejnot umiejetności, który aktualnie posiadasz. Skrót $pu.
- $użyjumiejętność - Powoduje użycie klejnotu umiejętności. Skrót $uu.

- $rankingwieży - Sprawdź ranking najlepszych eksploratorów Wieży Śmierci. Skrót $rw.
- $rekordwieży - Sprawdź osobisty rekord w eksploracji Wieży Śmierci.

Komendy dla <@&983798433590673448>:
- $flex - Pokaż innym jakim jesteś koxem,
- $kolor "kolor w kodowaniu hexadecymalnym" - Zmienia kolor rangi <@&983798433590673448> np. $kolor "00FFFF".

Komendy dla <@&687185998550925312> i patronów:
- $modlitwa - Gdy pojawi się kapliczka, to za sprawą modlitwy można wpłynąć na statystyki kolejnego bossa.
- $transformacja - Zmienia wygląd towarzysza.

Więcej informacji w wiadomości https://discord.com/channels/686137998177206281/1093905013480378519/1093905013480378519."""


class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="pomoc", aliases=["komendy"], brief="Send all available commands.")
    @commands.cooldown(1, 600, commands.BucketType.user)
    async def help(self, ctx: commands.Context) -> None:
        embed = discord.Embed(title=HELP_TITLE, description=HELP_TEXT, color=0x42F34C)
        await io.send(ctx.channel, embed=embed)

    @help.error
    async def help_cooldown(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            minutes = round(error.retry_after / 60, 2)
            await io.send(ctx.channel, f"Poczekaj na odnowienie komendy! Zostało {minutes} minut "
                                       "<:Bedge:970576892874854400>.")
        else:
            await ctx.bot.on_command_error(ctx, error, force=True)


async def setup(bot) -> None:
    await bot.add_cog(HelpCog(bot))
