"""``$pomoc`` - the list of player commands (also behind the "Pomoc" button of the hub panel).

The help is one embed: a short introduction (also: what the buttons do) and
one field per part of the game.  Discord limits a field to 1024 characters
and a whole embed to 6000; ``tests/test_ui_hub.py`` checks both, and that
every player command of the bot is mentioned here.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from ..core import io

HELP_TITLE = "<:PepoG:790963160528977980> Komendy dostępne w grze <:PepoG:790963160528977980>"
HELP_COLOUR = 0x42F34C

HELP_INTRO = """Respawn bossa może trwać od kilkunastu minut do kilkunastu godzin, więc warto zaglądać co jakiś czas na Discorda. Im dłużej boss się respi, tym potężniejszy będzie i tym lepszy drop zaoferuje.

Jeśli brakuje Wam graczy, to warto zawołać rolę <@&985071779787730944>. Do roli tej można przypisać się na kanale <#719989249956380793>.

<@&687185998550925312> oraz Patroni mogą zostać bossami. Jest to losowe. Jednak jeśli taki boss pokona gracza, to użytkownik Discorda, który owym bossem był, zgarnia dodatkowe doświadczenie.

🎮 **Przyciski:** przypięty na kanale bossa panel **Centrum gry** rusza na polowanie i do Wieży oraz pokazuje (tylko Tobie) towarzysza, stajnię, klejnoty i rankingi. Do wydarzeń dołączasz przyciskami pod ich postami (**Dołącz**, **Wkrocz do lasu**, **Otwórz skrzynię**, **Pomódl się**...), a pytania potwierdzasz przyciskiem **Potwierdzam** albo - jak dotąd - wpisując **$potwierdzam**. Wszystkie komendy poniżej działają nadal. Same walki toczą się na klawiaturze: wpisuj podane słowa, zanim minie czas!

Gra toczy się na kanale bossa; komendy, które tylko coś pokazują (rankingi, towarzysz, klejnoty...), działają na każdym kanale serwera.

Więcej informacji w wiadomości https://discord.com/channels/686137998177206281/1093905013480378519/1093905013480378519."""

#: ``(field name, field value)``; mentions render only in values, never in names.
HELP_FIELDS: tuple[tuple[str, str], ...] = (
    ("⚔️ Przygoda", """- **$polowanie - Polujesz na zwykłe stwory, możliwe raz dziennie lub po odpowiedniej modlitwie w kapliczce**. Komenda, od której warto zacząć przygodę. Inni dołączają przyciskiem **Dołącz do polowania** albo wpisując od razu $polowanie. Skróty $p, $pol.
- **$wieża - Wyruszasz na eksplorację Wieży Śmierci. Im wyżej, tym trudniej**. Dostępna raz w tygodniu.
- $zaatakuj - Atakuje bossa, gdy ten czeka na tym kanale (albo przycisk **Zaatakuj** pod bossem). Na legendarnego bossa zbiera się drużyna: **Dołącz** albo $zaatakuj, a gotowość potwierdza się przyciskiem **Gotowy** albo wpisując $tak.
- $skrzynia - Otwiera skrzynię, gdy pojawi się na kanale (albo przycisk **Otwórz skrzynię**).
- $zabojca - Wynajmuje zabójcę na jednego z graczy w rankingu. Skrót $asasyn.
- $kradnijxp nick - Kradnie doświadczenie wybranemu graczowi (nagroda z bossa)."""),
    ("🏆 Rankingi i rekordy", """- $kiedy - Podaje kto i kiedy ostatnio walczył z bossem.
- $rekord - Podaje kto najszybciej i w jakim czasie pokonał bossa.
- $ranking - Podaje sezonowy (tygodniowy) ranking łowców potworów. Skrót $r.
- $rankingwieży - Ranking najlepszych eksploratorów Wieży Śmierci. Skrót $rw.
- $rekordwieży - Twój osobisty rekord w eksploracji Wieży Śmierci.
- $rankingtowarzyszy - Lista najbardziej utalentowanych towarzyszy. Skrót $rt."""),
    ("🐾 Towarzysz", """- $towarzysz - Sprawdź swojego towarzysza; pod nim znajdziesz przyciski akcji. Skrót $t.
- $nazwij Nowe imię - Nazywasz swojego towarzysza (raz na 23 godziny). Skrót $n.
- $stajnia - Sprawdza stajnię z towarzyszami; z listy pod nią wyciągniesz towarzysza. Skrót $s.
- $schowajtowarzysza numer_slota - Chowa towarzysza do podanego slota stajni (1-4). Skrót $st.
- $wyciagnijtowarzysza numer_slota - Wyciąga towarzysza ze stajni z podanego slota (1-4). Skrót $wt.
- $odrodzenie - Przelosowuje wszystkie statystyki towarzysza. Skrót $o.
- $mocneodrodzenie - Przelosowanie z pominięciem wybranych statystyk (wybierasz je z listy). Skrót $mo.
- $oswiecenie - Zwiększa talent towarzysza.
- $porzucam - Porzucasz obecnego towarzysza.
- $wyczuwanie - Wyłącza albo włącza powiadomienia towarzysza o nadciągającym bossie."""),
    ("💎 Klejnoty umiejętności", """- $klejnoty - Pokazuje zebrane klejnoty umiejętności (działa tylko aktywny, jeden dziennie); aktywny wybierzesz z menu pod listą. Skrót $sakwa.
- $wybierzklejnot numer - Zmienia aktywny klejnot na wybrany z listy $klejnoty. Skrót $ws.
- $pokazskill - Prezentuje aktywny klejnot umiejętności. Skrót $pu.
- $skill - Używa aktywnego klejnotu umiejętności. Skrót $uu."""),
    ("👑 Rzeźnik Potworów", """Komendy dla <@&983798433590673448>:
- $flex - Pokaż innym jakim jesteś koxem.
- $kolor kolor_hex - Zmienia kolor rangi <@&983798433590673448>, np. $kolor 00FFFF.
- $ikona - Zmienia ikonę rangi: wyślij komendę z dołączonym obrazkiem .png."""),
    ("🙏 Patroni", """Komendy dla <@&687185998550925312> i patronów:
- $modlitwa - Gdy pojawi się kapliczka, modlitwą (albo przyciskiem **Pomódl się**) można wpłynąć na statystyki kolejnego bossa.
- $transformacja - Zmienia wygląd towarzysza."""),
)

#: The whole help as one text (kept for code and tests that read it as before).
HELP_TEXT = HELP_INTRO + "".join(f"\n\n**{name}**\n{value}" for name, value in HELP_FIELDS)


def help_embed() -> discord.Embed:
    """The help as an embed (``$pomoc`` and the hub panel's "Pomoc" button)."""
    embed = discord.Embed(title=HELP_TITLE, description=HELP_INTRO, color=HELP_COLOUR)
    for name, value in HELP_FIELDS:
        embed.add_field(name=name, value=value, inline=False)
    return embed


class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot) -> None:
        self.bot = bot

    # A cooldown hit is answered by the bot's central error handler (Discord's live countdown).
    @commands.command(name="pomoc", aliases=["komendy"], brief="Send all available commands.")
    @commands.cooldown(1, 600, commands.BucketType.user)
    async def help(self, ctx: commands.Context) -> None:
        await io.send(ctx.channel, embed=help_embed())


async def setup(bot) -> None:
    await bot.add_cog(HelpCog(bot))
