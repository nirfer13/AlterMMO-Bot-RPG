"""``!pomoc`` - the list of player commands (also behind the "Pomoc" button of the hub panel).

The help is one embed: a short introduction (also: what the buttons do) and
one field per part of the game.  Discord limits a field to 1024 characters
and a whole embed to 6000; ``tests/test_ui_hub.py`` checks both, and that
every player command of the bot is mentioned here.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from ..core import io
from ..features import patrons

HELP_TITLE = "<:PepoG:790963160528977980> Komendy dostępne w grze <:PepoG:790963160528977980>"
HELP_COLOUR = 0x42F34C

#: (An f-string only for the shared wording of the supporter groups - see ``features/patrons.py``.)
HELP_INTRO = f"""Respawn bossa może trwać od kilkunastu minut do kilkunastu godzin, więc warto zaglądać co jakiś czas na Discorda. Im dłużej boss się respi, tym potężniejszy będzie i tym lepszy drop zaoferuje.

Jeśli brakuje Wam graczy, to warto zawołać rolę <@&985071779787730944>. Do roli tej można przypisać się przyciskiem **Powiadomienia** na **Panelu gry** (drugie kliknięcie ją zdejmuje) albo na kanale <#719989249956380793>.

Wspierający ({patrons.SUPPORTERS_LONG}) mogą zostać bossami. Jest to losowe. Jednak jeśli taki boss pokona gracza, to użytkownik Discorda, który owym bossem był, zgarnia dodatkowe doświadczenie.

🎮 **Przyciski:** przypięty na kanale bossa **Panel gry** rusza na polowanie i do Wieży, wysyła towarzysza na zlecenie oraz pokazuje towarzysza, stajnię, klejnoty, rankingi (wspierającym także ranking kolekcji) i album. Do wydarzeń dołączasz przyciskami pod ich postami (**Dołącz**, **Wkrocz do lasu**, **Otwórz skrzynię**, **Pomódl się**...), a pytania potwierdzasz przyciskiem **Potwierdzam** albo wpisując **!potwierdzam**. Same walki toczą się na klawiaturze: wpisuj podane słowa, zanim minie czas!

⛏️ **Kopalnia Ostatniej Lampy:** schodzi do niej tylko ten, kto pierwszy kliknie **Wejdź do kopalni**. Łup trzeba zabrać, zanim kopalnię zamknie następne wydarzenie albo boss - inaczej przepada.

Gra toczy się na kanale bossa; komendy, które tylko coś pokazują (rankingi, towarzysz, klejnoty...), działają na każdym kanale serwera.

Więcej informacji w wiadomości https://discord.com/channels/686137998177206281/1093905013480378519/1093905013480378519."""

#: ``(field name, field value)``; mentions render only in values, never in names.
HELP_FIELDS: tuple[tuple[str, str], ...] = (
    ("⚔️ Przygoda", """- **!polowanie - Polujesz na zwykłe stwory, możliwe raz dziennie lub po odpowiedniej modlitwie w kapliczce**. Komenda, od której warto zacząć przygodę. Inni dołączają przyciskiem **Dołącz do polowania** albo wpisując od razu !polowanie. Skróty !p, !pol.
- **!wieża - Wyruszasz na eksplorację Wieży Śmierci. Im wyżej, tym trudniej**. Raz w tygodniu - albo z kluczem (!klucz).
- !zaatakuj - Atakuje bossa, gdy ten czeka na tym kanale (albo przycisk **Zaatakuj** pod bossem). Na legendarnego bossa zbiera się drużyna: **Dołącz** albo !zaatakuj, a gotowość potwierdza się przyciskiem **Gotowy** albo wpisując !tak.
- !skrzynia - Otwiera skrzynię, gdy pojawi się na kanale (albo przycisk **Otwórz skrzynię**).
- Wędrowny kupiec - kupujesz za zwoje przyciskiem pod jego postem; kto pierwszy, ten lepszy.
- !zabojca - Wynajmuje zabójcę na jednego z graczy w rankingu. Skrót !asasyn.
- !kradnijxp nick - Kradnie doświadczenie wybranemu graczowi (nagroda z bossa)."""),
    ("🏆 Rankingi i rekordy", """- !kiedy - Podaje kto i kiedy ostatnio walczył z bossem.
- !rekord - Podaje kto najszybciej i w jakim czasie pokonał bossa.
- !ranking - Podaje sezonowy (tygodniowy) ranking łowców potworów. Skrót !r.
- !rankingwieży - Ranking najlepszych eksploratorów Wieży Śmierci. Skrót !rw.
- !rekordwieży - Twój osobisty rekord w eksploracji Wieży Śmierci.
- !rankingtowarzyszy - Lista najbardziej utalentowanych towarzyszy. Skrót !rt."""),
    ("🐾 Towarzysz", """- !towarzysz - Sprawdź swojego towarzysza; pod nim znajdziesz przyciski akcji. Skrót !t.
- !nazwij Nowe imię - Nazywasz swojego towarzysza (raz na 23 godziny). Skrót !n.
- !stajnia - Sprawdza stajnię z towarzyszami; z listy pod nią wyciągniesz towarzysza. Skrót !s.
- !schowajtowarzysza numer_slota - Chowa towarzysza do podanego slota stajni. Skrót !st.
- !wyciagnijtowarzysza numer_slota - Wyciąga towarzysza ze stajni z podanego slota. Skrót !wt.
- !wyjmijjajko - Wyjmuje jajko z sakwy na jajko, żeby mogło rosnąć (towarzysz przy Tobie idzie do stajni). Skrót !wj.
- !odrodzenie - Przelosowuje wszystkie statystyki towarzysza. Skrót !o.
- !mocneodrodzenie - Przelosowanie z pominięciem wybranych statystyk (wybierasz je z listy). Skrót !mo.
- !oswiecenie - Zwiększa talent towarzysza.
- !porzucam - Porzucasz obecnego towarzysza.
- !wyczuwanie - Wyłącza albo włącza powiadomienia towarzysza o nadciągającym bossie."""),
    ("💎 Klejnoty umiejętności", """- !klejnoty - Pokazuje zebrane klejnoty umiejętności (działa tylko aktywny, jeden dziennie); aktywny wybierzesz z menu pod listą. Skrót !sakwa.
- !wybierzklejnot numer - Zmienia aktywny klejnot na wybrany z listy !klejnoty. Skrót !ws.
- !pokazskill - Prezentuje aktywny klejnot umiejętności. Skrót !pu.
- !skill - Używa aktywnego klejnotu umiejętności. Skrót !uu."""),
    ("🎒 Przedmioty", """- !przedmioty - Wszystko, co nosisz w sakwie. Skrót !eq.
- !przywolaj - Zwój przywołania: przybywa mityczny boss (czeka pół godziny).
- !przyneta - Polowanie z Przynętą: groźniejszy potwór.
- !wskrzes @gracz - Wskrzesza poległego łowcę.
- !kosc, !karta - Kość albo Karta Dovy: nowa rzadkość albo nowe perki bossa."""),
    ("👑 Rzeźnik Potworów", """Komendy dla <@&983798433590673448>:
- !flex - Pokaż innym jakim jesteś koxem.
- !kolor kolor_hex - Zmienia kolor rangi <@&983798433590673448>, np. !kolor 00FFFF.
- !ikona - Zmienia ikonę rangi: wyślij komendę z dołączonym obrazkiem .png."""),
    ("🧭 Zlecenia", """- **!zlecenia - Raz dziennie wysyłasz towarzysza (tego przy Tobie albo ze stajni) na zlecenie z tablicy; szansę widzisz przed wysłaniem**. Pokazuje też postęp i łup. Skrót !zl."""),
    # Before 2026-11-20: "🙏 Patroni" (crafters and patrons only).
    ("🙏 Wspierający", f"""Komendy tylko dla wspierających ({patrons.SUPPORTERS_LONG}):
- !modlitwa - Gdy pojawi się kapliczka, modlitwą (albo przyciskiem **Pomódl się**) można wpłynąć na statystyki kolejnego bossa.
- !transformacja - Zmienia wygląd towarzysza (zużywa lustro wizualiów).
- !osiagniecia - Pokazuje Twoje kolekcjonerskie osiągnięcia i ile ich już masz.
- !album - Album Twoich poświęconych towarzyszy (każdy trafia tam po **!porzucam**): przeglądaj go i kompletuj kolekcję. Skrót !kolekcja.
- !rankingalbumow - Ranking najcenniejszych kolekcji wspierających. Pierwsze miejsce nosi rangę **Kolekcjoner Pupili** - dodatkowa szansa w giveawayach. Skróty !rankingkolekcji, !ra.
Wszystkie benefity wspierających i sposoby wsparcia znajdziesz w przypiętym **Panelu gry**."""),
)

#: The whole help as one text (kept for code and tests that read it as before).
HELP_TEXT = HELP_INTRO + "".join(f"\n\n**{name}**\n{value}" for name, value in HELP_FIELDS)


def help_embed() -> discord.Embed:
    """The help as an embed (``!pomoc`` and the hub panel's "Pomoc" button)."""
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
