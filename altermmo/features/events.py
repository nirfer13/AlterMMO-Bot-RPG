"""Random events of the boss channel (port of the legacy ``functions/functions_events.py``).

Every function plays one event on ``scene.channel`` and is self-contained: it
does not touch ``bot.state`` (the events cog wraps the calls in
``state.running_event`` and summons the ritual boss itself).  The exceptions
are the duel and the ritual, whose sign-up leaves the channel free: they wait
for ``state.busy`` to clear and hold it for the fight / for showing the boss,
and the ritual asks ``bot.boss_cycle.can_summon()``.  An event that
cannot continue - nobody came, its message was deleted, too few players -
ends with its legacy texts; technical errors propagate to the cog.

How players take part (the legacy bot used reactions; they are buttons now,
see :mod:`altermmo.core.ui`):

* sign-ups (invasion, party, ritual, duel, PvP tournament) post the event's
  panel with a "Dołącz" button.  The panel shows the deadline (Discord's live
  "za 10 minut") and the list of everybody who signed up; it is edited as
  people click, but at most once a second.  A second click of the same member
  is told so (``ui.reply``) and counted once;
* volunteer events (dark forest, memory game) and the hazard cups take the
  first valid click; the panel then says who it was and loses its buttons;
* the memory answer is ten emote buttons only the volunteer can press;
* bots never count, and neither do members who may not write on the channel
  (the "dead hunter" ban) - the buttons refuse them;
* "collect for N minutes" events (invasion, party, ritual) end quietly when
  their panel was deleted meanwhile, as before.

The passive events (chest, shrine) get their button from the events cog,
which owns their life cycle; :func:`spawn_chest` only posts the panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterator

import discord

from ..core import challenge, io, timing, ui
from ..settings import ASSETS_DIR
from . import boss, daily, database, modifiers, patrons

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

EVENTS_DIR = ASSETS_DIR / "events"

# -- emoji ---------------------------------------------------------------------------

SWORDS = "⚔️"
SPARKLES = "✨"
TREE = "🌲"
DRINK = "<:Drink:912798939542061086>"
PEPOG = "<:PepoG:790963160528977980>"
RIP = "<:RIP:912797982917816341>"
HAZARD_CUPS = ("<:2Head:882184634572627978>", "<:Nerdge:984770661702578227>")
MEMORY_EMOTES = (
    "<:MonkaChrist:783992337075929098>", "<:peepoBlush:984769061340737586>",
    "<:Susge:973591024322633858>", "<:peepocof:1062666939178160188>",
    "<:HYPERS:1080778935966642276>", "<:Pepega:936907616293093377>",
    "<:Bedge:970576892874854400>", "<:EZ:720710033645633587>",
    "<:gayge:1062110423438078072>", "<:POGGERS:936907543849078844>",
)

# -- waiting times (seconds): (production, debug mode) -------------------------------

INVASION_WAIT = (600, 5)
PARTY_WAIT = (300, 15)
RITUAL_WAIT = (600, 5)
VOLUNTEER_WAIT = (600, 15)      # memory, hazard, hunting, duel
TOURNAMENT_SIGNUP = (180, 10)
MEMORY_STEP_TIMEOUT = 10
TOAST_TIMEOUT = 15
CONFIRM_TIMEOUT = 15

#: A sign-up panel is edited at most once per this many seconds (clicks are gathered meanwhile).
ROSTER_EDIT_GAP = 1.0
#: At most this many mentions are listed on a panel; the rest is a number.
ROSTER_SHOWN = 40

# -- game rules ----------------------------------------------------------------------

#: The legacy code required "more than 2" reactors but counted the bot's own
#: reaction, so in practice two players were enough to start the party.
PARTY_MIN_PLAYERS = 2
PARTY_ROUNDS = 8
HAZARD_STAKE = 5
ASSASSIN_PRICE = 30
ASSASSIN_BETRAYAL_CHANCE = 0.20
RITUAL_FULL_CIRCLE = 12

TOASTS = (
    "Chluśniem, bo uśniem", "Rybka lubi popływać",
    "Pierdykniem, bo odwykniem", "No to po maluchu",
    "Zdrowie wasze w gardło nasze",
    "Wódka to twój wróg, więc lej ją w mordę",
    "Poloneza czas zacząć", "Ciach babkę w piach",
    "Żeby dla Siwcoka nie starczyło",
    "Pijmy, bo szkło nasiąka",
    "Zdrowie pięknych pań i nAvesoma mamy",
    "Oby nam się", "No to po calaku",
    "Żeby nam gęby szuwarami nie zarosły",
    "Uchyl się duszo, bo ulewa idzie",
    "Człowiek nie wielbłąd, pić musi",
    "Pijemy, aż oślepniemy",
    "Robota to głupota, picie to jest życie",
    "Morda się smieje, gdy piwo się leje",
    "Od pieluchy aż po grób, zawsze piwko sobie kup",
    "Za tych, którzy nie mogą",
    "Za tych, co w kosmosie",
    "Nic tak życia nie upiększa, jak jabola dawka większa",
    "No to po szklanie i na grindowanie",
    "Życie jest krótkie, a pić się chce",
    "Pić i jebać, byle z biedy się wygrzebać",
    "Za wyruchanych przez Blizzarda",
    "Za zakola Alterowskie",
    "Żeby Varrakas porzucił anime",
    "Za lambo Szakalakela",
    "Za sugar daddyego Sanczo",
    "Żeby widzowie na Twitchu altermmo_pl dopisali",
    "Za milion za bitcoina",
    "Za zdrowie wszystkich użytkowników tego discorda",
)

# -- texts (legacy wording; "react" became "click") ----------------------------------

CHEST_TITLE = "<:Loot:912797849916436570> Skrzynia ze skarbami! <:Loot:912797849916436570>"
CHEST_DESCR = ('Pojawiła się skrzynia ze skarbami! Jedna osoba może ją otworzyć przyciskiem pod tym postem'
               ' albo komendą **$skrzynia**!'
               '\n\n*Crafterzy i Patroni mogą wydropić dodatkowe, kosmetyczne przedmioty!*')
CHEST_THUMB = 'https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png'

INVASION_TITLE = "<:RIP:912797982917816341> Inwazja na wioskę! <:RIP:912797982917816341>"
INVASION_DESCR = ('Pobliska wioska została zaatakowana przez bezdusznych najeźdźców!\n\n'
                  '**Zaoferujcie im swoją pomoc, klikając „Dołącz” pod tym postem.** Im więcej Was będzie,'
                  ' tym większa szansa, że odpędzicie najeźdźców i odbijecie zrabowane przez nich łupy!'
                  '\n\n*Crafterzy i Patroni mogą wydropić dodatkowe, kosmetyczne przedmioty!*')
INVASION_THUMB = 'https://www.altermmo.pl/wp-content/uploads/altermmo-3-112.png'
MSG_INVASION_LOST = ('Niestety nie udało się obronić wioski... Mężczyźni leżą w kałuży krwi, dzieci'
                     ' zostały rozszarpane na kwałki, a kobiety skończą jako kurtyzany. '
                     'Co z Was za bohaterowie? <:PepeKMS:783992337029267487>')

PARTY_TITLE = "<:Drink:912798939542061086> Zdrowie {name}! <:Drink:912798939542061086>"
PARTY_DESCR = ('Witam <@&985071779787730944>! Tutaj Wasz serwerowy Bard, Stasiek mi na imię.'
               '\nCzy za kimś chodzi **gorzałka**? Może napilibyśmy się za zdrowie jednego'
               ' z patronów lub crafterów?\n\n'
               '**To co? Za zdrowie {name}! Zapraszam do zabawy.**'
               '\n\n*Kliknij „Dołącz” pod postem, jeśli chcesz wziąć udział.*')
MSG_PARTY_START = ("No dobra, to zaczynamy imprezę <:Drink:912798939542061086>! Przygotujcie się, zaraz podam "
                   "toast! Wpiszcie go bez spacji, wielkość liter nie ma znaczenia!")
MSG_PARTY_ROUND = "**KOLEJKA {round}!**"
MSG_PARTY_BRAVO = "Brawo {name}!"
MSG_PARTY_NOBODY = "Pfff... Miernoty. Nie wiecie jak się pije."
MSG_PARTY_WINNER = ("Chopaki.. Hic!... Może jusz starczy?... Hic!... Graulasje <@{winner}>, "
                    "jesteś nje do zdarsia... Hic!\n\n")
PARTY_RANKING_TITLE = 'Ranking pijaków!'
PARTY_RANKING_URL = 'https://www.altermmo.pl/wp-content/uploads/zapierdol.gif'
PARTY_RANKING_FOOTER = 'Jutro kac będzie okrutny...'
MSG_PARTY_ALONE = "No cóż... Pozostało napić się w samotności. Zdrowie {name}! <:Drink:912798939542061086>"

MEMORY_TITLE = "<:PepoG:790963160528977980> Mędrzec potrzebuje pomocy! <:PepoG:790963160528977980>"
MEMORY_DESCR = ('Witajcie, mam na imię Sanczo. Potrzebuję Waszej pomocy...'
                '\n\nZa cholerę nie mogę spamiętać runicznych emotek, a brakuje mi rąk, żeby wertować kartki '
                'w księdze. Pomożecie mi?'
                '\n\n*Kliknij „Pomóż Sanczo” pod postem, jeśli chcesz wziąć udział.*')
MSG_MEMORY_SHOW = ("*Doskonale <@{player}>!* - z entuzjazmem zareagował Sanczo. - *Teraz pokażę Ci 4 runiczne "
                   "emotki, które zaraz będziesz musiał podać w odpowiedniej kolejności. Zapamiętaj je!*")
MSG_MEMORY_ASK = ("*Zaczynam przygotowywać zaklęcie. Klikaj runiczne emotki pod tą wiadomością "
                  "w odpowiedniej kolejności <@{player}>!*")
MSG_MEMORY_GOOD = "*Dobrze!* - Sanczo dopinguje."
MSG_MEMORY_WRONG = "*Całkiem nieźle idz...* - Sanczo nie zdążył dokończyć."
MSG_MEMORY_SLOW = "*Ech, za wolno, poradzę sobie sam...* - rzekł Sanczo i zabrał się do pracy."
MSG_MEMORY_DONE = "*Doskonała robota! Zaklęcie gotowe!* - Twarz Sanczo promienieje z radości."
MSG_MEMORY_NOBODY = "*Ech, poradzę sobie sam...* - rzekł Sanczo i zabrał się do pracy."
MSG_MEMORY_HELPER = "📖 {mention} pomaga Sanczo."

HAZARD_TITLE = "<:tf:882182966606966794> Zaryzykuj! <:tf:882182966606966794>"
HAZARD_DESCR = ('Witaj Wędrowcze, nazywam się Dova. Nie chcesz zgarnąć kilka zwojów?'
                '\n\nWybierz jeden z dwóch kubków. Pod jednym z nich będzie się kryła nagroda.'
                '\n\n*Kliknij jeden z kubków pod postem, jeśli chcesz zagrać. Potrzebujesz 5 zwojów.*')
HAZARD_LABELS = ("Lewy kubek", "Prawy kubek")
MSG_HAZARD_WON = "*Ech, gratuluje* - Dova rzekł zawiedziony."
MSG_HAZARD_LOST = "*Dzięki za zwoje. Kolego* <:2Head:882184634572627978> - Chwilę potem Dova znikł za drzwiami karczmy."
MSG_HAZARD_POOR = "Najpierw musisz mieć czym grać. Potrzebujesz co najmniej 5 zwojów..."
MSG_HAZARD_NOBODY = ("*Ech, na tym Discordzie nie ma pracy dla oszustów z moimi talentami...* "
                     "- rzekł Dova i zniknął w cieniu.")
MSG_HAZARD_PLAYER = "🎲 Kubek wybiera {mention}."

HUNTING_TITLE = "<:MonkaS:882181709100097587> Mroczny las! <a:PeepoRiot:1067317732942565416>"
HUNTING_DESCR = ('Przed Wami złowrogi las... Czy odważycie się do niego wkroczyć?'
                 '\n\n*Kliknij „Wkrocz do lasu”, jeśli chcesz wziąć udział.*')
HUNTING_BUTTON = "Wkrocz do lasu"
MSG_HUNTING_NOBODY = "*Las pozostaje tajemnicą...*"
MSG_FOREST_ENTERED = "🌲 Do lasu wkracza {mention}!"

DUEL_TITLE = "<:MonkaS:882181709100097587> Pojedynek! <a:PeepoRiot:1067317732942565416>"
DUEL_DESCR = ('Przed Wami rozpościera się arena. Czy odważycie się zawalczyć na śmierć i życie?'
              '\n\n*Kliknij „Dołącz” pod postem, jeśli chcesz wziąć udział.*')
DUEL_ROSTER = "Do walki stają ({count}/2)"
MSG_DUEL_CHALLENGE = ("{name} wyzywa {everyone} na pojedynek! Jeśli nikt się nie zgłosi, to {name} "
                      "otrzyma skarb oraz 6 punktów rankingowych!")
MSG_DUEL_NOBODY = "*Arena pozostaje pusta...*"

MSG_ASSASSIN_NO_ACCOUNT = "Nie masz jeszcze peta/konta w systemie."
MSG_ASSASSIN_POOR = "Potrzebujesz minimum 30 zwojów odrodzenia."
MSG_ASSASSIN_CONFIRM = ("Czy jesteś pewien, że chcesz zatrudnić zabójcę za 30 zwojów odrodzenia, "
                        "<@{player}>? <:Hmm:984767035617730620> Kliknij **Potwierdzam** "
                        "albo wpisz **$potwierdzam**.")
MSG_ASSASSIN_NOT_CONFIRMED = "Nie potwierdzono zlecenia zabójstwa."
MSG_ASSASSIN_BETRAYED = ("Zabójca Cię zdradził! Wbija Ci nóż w plecy i zabiera zwoje... "
                         "<:RIP:912797982917816341>")
MSG_ASSASSIN_NO_TARGET = "Zabójca nie znalazł żadnego celu."
MSG_ASSASSIN_DONE = "Zabójca wykonał zlecenie. Ofiarą został <@{target}> <:RIP:912797982917816341>"

TOURNAMENT_TITLE = "<:MonkaS:882181709100097587> Tygodniowy turniej PvP! <a:PeepoRiot:1067317732942565416>"
TOURNAMENT_DESCR = ('Przed Wami rozpościera się arena. Czy odważycie się zawalczyć na śmierć i życie?'
                    '\n\n*Kliknij „Dołącz” pod postem, jeśli chcesz wziąć udział w turnieju.*')
MSG_TOURNAMENT_CANCELLED = "PvP turniej anulowany. Za mało uczestników."
MSG_TOURNAMENT_CLOSED = "**Rejestracja na turniej zakończona!**\nUczestnicy: {players}"
MSG_TOURNAMENT_ROUND = "**Turniej PvP - Runda {round}**"
MSG_TOURNAMENT_BYE = "{mention} szczęśliwie przechodzi do kolejnej rundy!"
MSG_TOURNAMENT_DRAW = "Remis. Losowy zwycięzca: {mention}."
MSG_TOURNAMENT_DUEL_WINNER = "**Zwycięzca:** {mention}"
MSG_TOURNAMENT_WINNER = "🏆 **Zwycięzca turnieju:** {mention}! Gratulacje! 🏆"

RITUAL_TITLE = ("<:MonkaChrist:783992337075929098> Rozpoczął się rytuał przywołania! "
                "<:MonkaS:882181709100097587>")
RITUAL_DESCR = ('Użyczcie swojej energii w rytuale przywołania!\n\n'
                '**Zaoferujcie swoją pomoc, klikając „Dołącz” pod tym postem.** Im więcej Was będzie,'
                ' tym potężniejszą istotę przywołacie!')
MSG_RITUAL_FAILED = 'Rytuał przywołania nie powiódł się... <:Sadge:936907659142111273>'
MSG_RITUAL_OVERTAKEN = ('Rytuał został przerwany - z oddali nadciąga już inny potwór... '
                        '<:MonkaS:882181709100097587>')

# -- panels: buttons, deadlines, rosters ---------------------------------------------

JOIN_LABEL = "Dołącz"
ROSTER_NAME = "Zapisani ({count})"
ROSTER_EMPTY = "Na razie nikogo tu nie ma..."
ROSTER_MORE = "\n...i {count} więcej"
MSG_SIGN_UP_UNTIL = "⏳ Zapisy kończą się {when}."
MSG_SIGN_UP_CLOSED = "⌛ Zapisy zakończone."
MSG_WAITING_UNTIL = "⏳ Czekam na śmiałka - czas mija {when}."
MSG_ALREADY_SIGNED = "Już jesteś na liście <:PepoG:790963160528977980>"
MSG_TOO_LATE = "Ktoś był szybszy <:Sadge:936907659142111273>"
MSG_SIGN_UP_TOO_LATE = "Za późno - zapisy już się zakończyły <:Sadge:936907659142111273>"
# The same for everybody who clicks: one line on the channel is enough (see ui.reply).
ui.GENERAL_NOTICES.update({MSG_TOO_LATE, MSG_SIGN_UP_TOO_LATE})


# -- small helpers -------------------------------------------------------------------

def _wait(bot: "AlterBot", seconds: tuple[int, int]) -> float:
    """Pick the production / debug waiting time and scale it (tests shrink it)."""
    return timing.timeout(seconds[1] if bot.debug else seconds[0])


async def _send_image(scene: "Scene", *parts: str) -> Any:
    """Post a picture from ``assets/events``."""
    return await scene.send(file_path=EVENTS_DIR.joinpath(*parts))


def _embed(title: str, description: str, color: int, thumbnail: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    if thumbnail is not None:
        embed.set_thumbnail(url=thumbnail)
    return embed


def _with_line(embed: discord.Embed, line: str) -> discord.Embed:
    """A copy of ``embed`` with ``line`` added as a paragraph of its description."""
    changed = embed.copy()
    changed.description = f"{embed.description or ''}\n\n{line}"
    return changed


def _until(seconds: float) -> str:
    """"Czekam na śmiałka - czas mija <za 10 minut>" (Discord's live countdown)."""
    return MSG_WAITING_UNTIL.format(when=ui.when(seconds))


def _chest_rarity(rare_from: int) -> int:
    """Loot table 2 when ``randint(0, 100)`` reaches ``rare_from``, else table 1."""
    return 2 if random.randint(0, 100) >= rare_from else 1


async def _volunteer(scene: "Scene", view: ui.ClickView, seconds: float) -> Any:
    """The first server member who clicks a button of ``view``; None after ``seconds``."""
    bot = scene.bot
    deadline = time.monotonic() + seconds
    while True:
        click = await view.next(deadline - time.monotonic())
        if click is None:
            return None
        member = await bot.member(click.member.id)
        if member is not None:
            return member


async def _answer_late_clicks(view: ui.ClickView, text: str, *, chosen: Any = None) -> None:
    """Clicks that were accepted but not used by the flow (somebody was faster) are told so.

    Extra clicks of the ``chosen`` player (a double click, the second cup) are
    left alone: they were acknowledged already and that player was not late.
    """
    chosen_id = getattr(chosen, "id", None)
    for click in view.pending():
        if chosen_id is not None and click.member.id == chosen_id:
            continue
        await ui.reply(click.interaction, text)


async def _panel_deleted(scene: "Scene", message: Any) -> bool:
    """True when Discord says the panel is gone (other trouble counts as "still there")."""
    if message is None:
        return False
    try:
        await io.retry(lambda: scene.channel.fetch_message(message.id), what="fetch_message")
    except discord.NotFound:
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - Discord trouble: carry on with the members heard
        log.warning("Event panel could not be fetched (%s: %s).", type(exc).__name__, exc)
    return False


class _SignUp:
    """A sign-up panel: the event's embed, a "Dołącz" button, the deadline and a live roster.

    Use it as ``async with`` - the button is taken off when the block ends.
    Clicks are queued by the :class:`ui.ClickView`; a member is counted once
    (a second click is told so).  The roster is repainted by a
    small background task, at most once per :data:`ROSTER_EDIT_GAP` seconds.
    """

    def __init__(self, scene: "Scene", embed: discord.Embed, *, emoji: Any,
                 roster_name: str = ROSTER_NAME) -> None:
        self.scene = scene
        self.embed = embed
        self.roster_name = roster_name
        self.members: list[Any] = []
        self.message: Any = None
        self._clicked: set[int] = set()
        self._ends = time.monotonic()
        self._ends_at = datetime.now(timezone.utc)
        self._dirty = asyncio.Event()
        self._painter: asyncio.Task | None = None
        self.view = ui.ClickView([ui.Choice("join", JOIN_LABEL, emoji=emoji, style=discord.ButtonStyle.primary)],
                                 allow=self._allow)

    async def _allow(self, interaction: Any, key: str) -> str | None:
        # Synchronous check-and-add: two quick clicks of one member queue one sign-up.
        if interaction.user.id in self._clicked:
            return MSG_ALREADY_SIGNED
        self._clicked.add(interaction.user.id)
        return None

    def extend(self, seconds: float) -> None:
        """The sign-up ends ``seconds`` from now (shown on the panel)."""
        self._ends = time.monotonic() + seconds
        self._ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        self._dirty.set()

    def render(self, *, closed: bool = False) -> discord.Embed:
        """The panel: event text, deadline (or "closed") and the roster."""
        status = MSG_SIGN_UP_CLOSED if closed else MSG_SIGN_UP_UNTIL.format(when=ui.when(self._ends_at))
        embed = _with_line(self.embed, status)
        embed.clear_fields()
        mentions = [member.mention for member in self.members]
        if not mentions:
            roster = ROSTER_EMPTY
        else:
            roster = ", ".join(mentions[:ROSTER_SHOWN])
            if len(mentions) > ROSTER_SHOWN:
                roster += ROSTER_MORE.format(count=len(mentions) - ROSTER_SHOWN)
        embed.add_field(name=self.roster_name.format(count=len(mentions)), value=roster[:1024], inline=False)
        return embed

    async def open(self, seconds: float) -> Any:
        """Post the panel; the sign-up ends after ``seconds``.  Sending may raise, like :func:`io.send`."""
        self.extend(seconds)
        self._dirty.clear()
        self.message = await self.view.send(self.scene.channel, embed=self.render())
        self._painter = asyncio.ensure_future(self._paint())
        return self.message

    async def _paint(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self.view.refresh(embed=self.render())  # never raises
            await asyncio.sleep(ROSTER_EDIT_GAP)

    async def next_member(self) -> Any:
        """The next member who signs up before the deadline; None when time is up."""
        bot = self.scene.bot
        while True:
            click = await self.view.next(self._ends - time.monotonic())
            if click is None:
                return None
            member = await bot.member(click.member.id)
            if member is None:  # left the server meanwhile
                continue
            self.members.append(member)
            self._dirty.set()
            return member

    async def collect(self) -> list[Any]:
        """Everybody who signs up until the deadline."""
        while await self.next_member() is not None:
            pass
        return list(self.members)

    async def deleted(self) -> bool:
        return await _panel_deleted(self.scene, self.message)

    async def close(self) -> None:
        """Final roster, "closed", buttons off.  Never raises."""
        if self._painter is not None:
            self._painter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._painter
            self._painter = None
        await self.view.close(embed=self.render(closed=True))
        await _answer_late_clicks(self.view, MSG_SIGN_UP_TOO_LATE)

    async def __aenter__(self) -> "_SignUp":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


# -- chest ---------------------------------------------------------------------------

async def spawn_chest(scene: "Scene", *, view: discord.ui.View | None = None) -> Any:
    """Show a treasure chest; one player may open it (``$skrzynia`` or the button in ``view``).

    Returns the panel message (the events cog takes its button off when the chest is gone).
    """
    log.info("Treasure chest spawn.")
    await _send_image(scene, "chest", f"{random.randint(0, 4)}.png")
    extra = {} if view is None else {"view": view}
    return await scene.send(embed=_embed(CHEST_TITLE, CHEST_DESCR, 0xFCBA03, CHEST_THUMB), **extra)


async def open_chest(scene: "Scene") -> None:
    """``scene.author`` opens the chest and gets monster loot."""
    rarity = _chest_rarity(90)
    boost_percent = random.randint(0, 25)
    await daily.rand_loot(scene, rarity, scene.author, boost_percent)


# -- invasion ------------------------------------------------------------------------

async def spawn_invasion(scene: "Scene") -> None:
    """Village invasion: the more players sign up, the better the odds and the loot."""
    bot = scene.bot
    log.info("Invasion spawn.")
    await _send_image(scene, "invasion", f"{random.randint(0, 4)}.png")
    async with _SignUp(scene, _embed(INVASION_TITLE, INVASION_DESCR, 0xFC0303, INVASION_THUMB),
                       emoji=SWORDS) as sign_up:
        await sign_up.open(_wait(bot, INVASION_WAIT))
        users = await sign_up.collect()
        if await sign_up.deleted():
            log.info("Invasion interrupted: its message was deleted.")
            return

    chance = random.randint(0, 100)
    if users and chance <= len(users) * 10 + 10:
        boost_percent = len(users) * 10
        log.info("Invasion repelled by %d players (loot boost %d%%).", len(users), boost_percent)
        for user in users:
            await daily.rand_loot(scene, _chest_rarity(90), user, boost_percent)
    else:
        log.info("Invasion lost (%d defenders).", len(users))
        await _send_image(scene, "invasion", "5.png")
        await scene.send(MSG_INVASION_LOST)


# -- party ---------------------------------------------------------------------------

@contextlib.contextmanager
def _channel_messages(bot: "AlterBot", channel: Any) -> Iterator[asyncio.Queue]:
    """Queue every message players write on ``channel`` while the block runs."""
    queue: asyncio.Queue = asyncio.Queue()
    channel_id = channel.id

    async def on_message(message: Any) -> None:
        if message.author.bot or message.channel.id != channel_id:
            return
        queue.put_nowait(message)

    bot.add_listener(on_message, "on_message")
    try:
        yield queue
    finally:
        bot.remove_listener(on_message, "on_message")


async def _toast_round(scene: "Scene", toast: str) -> Any:
    """Post the toast and return the first player who types it within 15 s (None: nobody).

    Everybody on the channel may try as often as they like; the time limit is
    fixed (the legacy one restarted with every message written anywhere on the
    server).  A round spoiled by connection problems is repeated.
    """
    bot = scene.bot
    prompt = '"**' + " ".join(toast.upper()) + '**"'
    limit = timing.timeout(TOAST_TIMEOUT)
    for attempt in range(1, challenge.MAX_CONSECUTIVE_DISRUPTIONS + 1):
        # (round_running: a typed round - no answers to clicks are posted meanwhile)
        with challenge.round_running(scene.channel), _channel_messages(bot, scene.channel) as queue:
            started = time.monotonic()
            sent = await scene.send(prompt)
            deadline = time.monotonic() + limit
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if message.id > sent.id and str(message.content).lower() == toast.lower():
                    return message.author
        if not bot.net.disrupted_since(started):
            return None
        log.warning("Toast round disrupted (%d/%d in a row).", attempt, challenge.MAX_CONSECUTIVE_DISRUPTIONS)
        await bot.net.wait_connected(timeout=120)
        if attempt < challenge.MAX_CONSECUTIVE_DISRUPTIONS:
            await challenge.notify_disrupted(scene.channel)
    return None


async def spawn_party(scene: "Scene") -> None:
    """Drinking party in honour of a random patron: who types the toasts first?"""
    bot = scene.bot
    channel = scene.channel
    log.info("Party spawn.")

    cheerer = patrons.get_patron(bot)
    if cheerer is None:
        log.warning("Party skipped: nobody holds a patron/crafter role.")
        return
    honoured = cheerer.name

    await scene.send(str(cheerer.display_avatar.url))
    async with _SignUp(scene, _embed(PARTY_TITLE.format(name=honoured), PARTY_DESCR.format(name=honoured),
                                     0xc9700a), emoji=DRINK) as sign_up:
        await sign_up.open(_wait(bot, PARTY_WAIT))
        users = await sign_up.collect()
        if await sign_up.deleted():
            log.info("Party interrupted: its message was deleted.")
            return
    log.info("Party guests: %s", [str(user) for user in users])

    if len(users) < PARTY_MIN_PLAYERS:
        await _send_image(scene, "party", "0.png")
        await scene.send(MSG_PARTY_ALONE.format(name=honoured))
        return

    async with io.safe_typing(channel):
        await timing.pause(2)
        await scene.send(MSG_PARTY_START)
        await timing.pause(5)

    ranking: dict[int, int] = {}
    for round_no in range(1, PARTY_ROUNDS + 1):
        await scene.send(MSG_PARTY_ROUND.format(round=round_no))
        toast = random.choice(TOASTS)
        await timing.pause(8)
        await challenge.countdown(channel)
        drinker = await _toast_round(scene, toast)
        if drinker is None:
            await scene.send(MSG_PARTY_NOBODY)
            continue
        await scene.send(MSG_PARTY_BRAVO.format(name=drinker.name))
        ranking[drinker.id] = ranking.get(drinker.id, 0) + 1

    if not ranking:
        log.info("Party over: nobody managed a single toast.")
        return

    # Most toasts wins; on a tie the one who scored first.
    winner_id = max(ranking, key=ranking.get)
    await _send_image(scene, "party", "1.png")
    ordered = sorted(ranking.items(), key=lambda item: item[1], reverse=True)
    lines = [f"{place}. <@{player_id}>: {toasts}" for place, (player_id, toasts) in enumerate(ordered, 1)]
    embed = discord.Embed(title=PARTY_RANKING_TITLE, url=PARTY_RANKING_URL,
                          description=MSG_PARTY_WINNER.format(winner=winner_id) + '\n'.join(lines),
                          color=0xfcba03)
    embed.set_thumbnail(url=PARTY_RANKING_URL)
    embed.set_footer(text=PARTY_RANKING_FOOTER)
    await scene.send(embed=embed)

    boost_percent = random.randint(0, 50)
    winner = await bot.member(winner_id)
    if winner is None:
        log.info("Party winner %s left the server; no loot.", winner_id)
        return
    await daily.rand_loot(scene, 2, winner, boost_percent)


# -- memory --------------------------------------------------------------------------

async def _memory_failed(scene: "Scene", text: str, *, pause: float = 0) -> bool:
    await scene.send(text)
    if pause:
        await timing.pause(pause)
    await _send_image(scene, "memory", "1.png")
    return False


def rune_key(index: int) -> str:
    """Button key of ``MEMORY_EMOTES[index]`` under the memory question."""
    return f"rune{index}"


async def spawn_memory(scene: "Scene") -> bool:
    """Memory game: the first volunteer repeats four emotes in the right order."""
    bot = scene.bot
    log.info("Memory game spawn.")
    await _send_image(scene, "memory", "0.png")
    embed = _embed(MEMORY_TITLE, MEMORY_DESCR, 0x827b7a)
    seconds = _wait(bot, VOLUNTEER_WAIT)
    async with ui.ClickView([ui.Choice("join", "Pomóż Sanczo", emoji=PEPOG,
                                       style=discord.ButtonStyle.primary)]) as view:
        await view.send(scene.channel, embed=_with_line(embed, _until(seconds)))
        player = await _volunteer(scene, view, seconds)
        await view.close(embed=embed if player is None
                         else _with_line(embed, MSG_MEMORY_HELPER.format(mention=player.mention)))
        await _answer_late_clicks(view, MSG_TOO_LATE, chosen=player)
    if player is None:
        return await _memory_failed(scene, MSG_MEMORY_NOBODY, pause=5)

    pool = list(MEMORY_EMOTES)
    selected: list[str] = []
    for _ in range(4):
        emote = random.choice(pool)
        pool.remove(emote)
        selected.append(emote)
    # The four runes are shown in the message itself and hidden by deleting it.
    shown = await scene.send(MSG_MEMORY_SHOW.format(player=player.id) + "\n\n" + "   ".join(selected))

    await scene.send("... 20...")
    await timing.pause(10)
    await scene.send("... 10...")
    await timing.pause(5)
    await scene.send("... 5...")
    await timing.pause(5)
    if not await io.safe_delete(shown):
        log.warning("Memory game: the message with the emotes could not be deleted.")

    runes = [ui.Choice(rune_key(index), emoji=emote, row=index // 5) for index, emote in enumerate(MEMORY_EMOTES)]
    failure: str | None = None
    async with ui.ClickView(runes, only=[player]) as answer:
        await answer.send(scene.channel, MSG_MEMORY_ASK.format(player=player.id))
        for expected in selected:
            click = await answer.next(timing.timeout(MEMORY_STEP_TIMEOUT))
            if click is None:
                failure = MSG_MEMORY_SLOW
                break
            if MEMORY_EMOTES[int(click.key.removeprefix("rune"))] != expected:
                failure = MSG_MEMORY_WRONG
                break
            # A pressed rune cannot be pressed again by an accidental double click:
            # switched off right here (no await since next() returned it), so a second
            # click already queued or still on its way is skipped by answer.next().
            answer.relabel(click.key, disabled=True)
            await answer.refresh()
            await scene.send(MSG_MEMORY_GOOD)
    if failure is not None:
        return await _memory_failed(scene, failure, pause=5 if failure == MSG_MEMORY_SLOW else 0)

    rarity = _chest_rarity(50)
    boost_percent = random.randint(0, 25)
    await _send_image(scene, "memory", "2.png")
    await scene.send(MSG_MEMORY_DONE)
    await daily.rand_loot(scene, rarity, player, boost_percent)
    return True


# -- hazard --------------------------------------------------------------------------

async def _hazard_bet(bot: "AlterBot", player_id: int, delta: int) -> bool:
    """Settle a bet of somebody who owns at least 5 scrolls; False when they do not."""
    async with bot.db.transaction() as con:
        rows = await con.fetch(
            """UPDATE PETOWNER SET REROLL_SCROLL = REROLL_SCROLL + $2
               WHERE PLAYER_ID = $1 AND REROLL_SCROLL >= $3 RETURNING REROLL_SCROLL""",
            int(player_id), int(delta), HAZARD_STAKE)
    return bool(rows)


def cup_key(index: int) -> str:
    """Button key of ``HAZARD_CUPS[index]``."""
    return f"cup{index}"


async def spawn_hazard(scene: "Scene") -> bool:
    """Shell game: pick one of two cups; 5 reroll scrolls are won or lost."""
    bot = scene.bot
    log.info("Hazard spawn.")
    await _send_image(scene, "hazard", "0.jpeg")
    embed = _embed(HAZARD_TITLE, HAZARD_DESCR, 0x827b7a)
    seconds = _wait(bot, VOLUNTEER_WAIT)
    cups = [ui.Choice(cup_key(index), label, emoji=cup, style=discord.ButtonStyle.primary)
            for index, (cup, label) in enumerate(zip(HAZARD_CUPS, HAZARD_LABELS))]
    player = None
    won = False
    async with ui.ClickView(cups) as view:
        await view.send(scene.channel, embed=_with_line(embed, _until(seconds)))
        lucky_cup = random.choice(list(HAZARD_CUPS))
        deadline = time.monotonic() + seconds
        while True:
            click = await view.next(deadline - time.monotonic())
            if click is None:
                await view.close(embed=embed)
                break
            member = await bot.member(click.member.id)
            if member is None:
                continue
            won = HAZARD_CUPS[int(click.key.removeprefix("cup"))] == lucky_cup
            if not await _hazard_bet(bot, member.id, HAZARD_STAKE if won else -HAZARD_STAKE):
                await ui.reply(click.interaction, MSG_HAZARD_POOR)  # the cups stay for the others
                continue
            player = member
            await view.close(embed=_with_line(embed, MSG_HAZARD_PLAYER.format(mention=player.mention)))
            await _answer_late_clicks(view, MSG_TOO_LATE, chosen=player)
            break

    if player is None:
        await scene.send(MSG_HAZARD_NOBODY)
        return False
    log.info("Hazard: %s %s %d scrolls.", player, "won" if won else "lost", HAZARD_STAKE)
    if not won:
        await scene.send(MSG_HAZARD_LOST)
        return False
    await scene.send(MSG_HAZARD_WON)
    boost_percent = random.randint(20, 55)
    await daily.rand_loot(scene, 2, player, boost_percent)
    return True


# -- hunting -------------------------------------------------------------------------

async def spawn_hunting(scene: "Scene") -> bool:
    """Dark forest: the first volunteer gets a free hunt (others may join it).

    One panel: the forest picture inside the embed and a "Wkrocz do lasu"
    button.  Returns True when a hunt took place (whatever its result), False
    when nobody came.
    """
    bot = scene.bot
    log.info("Hunting event spawn.")
    picture = EVENTS_DIR / "hunting" / "0.png"
    embed = _embed(HUNTING_TITLE, HUNTING_DESCR, 0x003616)
    embed.set_image(url=f"attachment://{picture.name}")
    seconds = _wait(bot, VOLUNTEER_WAIT)
    async with ui.ClickView([ui.Choice("join", HUNTING_BUTTON, emoji=TREE,
                                       style=discord.ButtonStyle.primary)]) as view:
        await view.send(scene.channel, embed=_with_line(embed, _until(seconds)), file_path=picture)
        player = await _volunteer(scene, view, seconds)
        await view.close(embed=embed if player is None
                         else _with_line(embed, MSG_FOREST_ENTERED.format(mention=player.mention)))
        await _answer_late_clicks(view, MSG_TOO_LATE, chosen=player)

    if player is None:
        await scene.send(MSG_HUNTING_NOBODY)
        await _send_image(scene, "hunting", "1.png")
        return False

    rarity = 1 if 0 <= random.randint(0, 100) <= 96 else 2
    is_player_boss, boss_player = await daily.send_mob_image(scene, rarity)
    await daily.hunt_mobs(scene, rarity, is_player_boss, boss_player, player)
    return True


# -- duel ----------------------------------------------------------------------------

async def spawn_duel(scene: "Scene") -> bool:
    """Arena: the first two volunteers fight; a lone challenger wins by walkover.

    Returns True when a duel was fought to the end, False otherwise.
    """
    bot = scene.bot
    log.info("Duel spawn.")
    participants: list[Any] = []
    await _send_image(scene, "pvp", "pvp.png")
    async with _SignUp(scene, _embed(DUEL_TITLE, DUEL_DESCR, 0x360006), emoji=SWORDS,
                       roster_name=DUEL_ROSTER) as sign_up:
        await sign_up.open(_wait(bot, VOLUNTEER_WAIT))
        while len(participants) < 2:
            member = await sign_up.next_member()
            if member is None:
                break
            participants.append(member)
            if len(participants) < 2:
                # A challenger gives the others ten more minutes (a full wait again).
                sign_up.extend(_wait(bot, VOLUNTEER_WAIT))
                everyone = "wszystkich" if bot.debug else f"<@&{bot.ids.hunters_role}>"
                await scene.send(MSG_DUEL_CHALLENGE.format(name=member.name, everyone=everyone))

    if len(participants) < 2:
        if participants:
            challenger = participants[0]
            await database.update_ranking_table(bot, challenger.id, 2, 0)
            await daily.rand_loot(scene, 2, challenger, 0)
        await scene.send(MSG_DUEL_NOBODY)
        await _send_image(scene, "pvp", "0.png")
        return False

    # The sign-up left the channel free, so a hunt or a tower run may be going on:
    # let it finish, then keep the channel for the fight.
    state = bot.state
    while state.busy:
        await asyncio.sleep(2)
    with state.hold_busy("event:DUEL"):
        finished, _winner = await boss.duel_fight(scene, participants)
    return bool(finished)


# -- assassin ------------------------------------------------------------------------

async def _pick_victim(bot: "AlterBot", author_id: int) -> Any:
    """A random member from the top 100 of the ranking (not the client); None when there is none."""
    rows = await bot.db.fetch(
        "SELECT ID FROM RANKING WHERE ID != $1 ORDER BY POINTS DESC LIMIT 100", str(author_id))
    candidates = [row["id"] for row in rows]
    while candidates:
        target_id = random.choice(candidates)
        candidates.remove(target_id)
        victim = await bot.member(target_id)
        if victim is not None and victim.id != author_id:
            return victim
    return None


async def assasinate(scene: "Scene") -> bool:
    """``$zabojca``: for 30 reroll scrolls an assassin kills a random ranked player.

    The client confirms with a button (typing ``$potwierdzam`` works too).
    Returns True only when the order was placed and paid for (also when the
    assassin betrayed the client); the caller gives the cooldown back otherwise.
    """
    bot = scene.bot
    author = scene.author
    if author is None:
        return False

    row = await bot.db.fetchrow("SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = $1", int(author.id))
    if row is None:
        await scene.send(MSG_ASSASSIN_NO_ACCOUNT)
        return False
    if int(row[0] or 0) < ASSASSIN_PRICE:
        await scene.send(MSG_ASSASSIN_POOR)
        return False

    answer = await ui.confirm(bot, scene.channel, author, MSG_ASSASSIN_CONFIRM.format(player=author.id),
                              timeout=timing.timeout(CONFIRM_TIMEOUT))
    if not answer:
        await scene.send(MSG_ASSASSIN_NOT_CONFIRMED)
        return False
    if answer.typed is not None:
        await io.safe_react(answer.typed, RIP)

    # Nobody to kill: say so before any scroll changes hands.
    victim = await _pick_victim(bot, author.id)
    if victim is None:
        await scene.send(MSG_ASSASSIN_NO_TARGET)
        return False

    async with bot.db.transaction() as con:
        paid = await con.fetch(
            """UPDATE PETOWNER SET REROLL_SCROLL = REROLL_SCROLL - $2
               WHERE PLAYER_ID = $1 AND REROLL_SCROLL >= $2 RETURNING REROLL_SCROLL""",
            int(author.id), ASSASSIN_PRICE)
    if not paid:  # the scrolls were spent while the bot waited for the confirmation
        await scene.send(MSG_ASSASSIN_POOR)
        return False
    log.info("Assassin hired by %s (%s) for %d scrolls.", author, author.id, ASSASSIN_PRICE)

    # From here on the order is paid: it counts whatever happens to the messages.
    betrayed = random.random() < ASSASSIN_BETRAYAL_CHANCE
    target = author if betrayed else victim
    text = MSG_ASSASSIN_BETRAYED if betrayed else MSG_ASSASSIN_DONE.format(target=victim.id)
    try:
        if betrayed:
            await scene.send(text)
            await boss.set_dead_assassin(bot, target)
        else:
            await boss.set_dead_assassin(bot, target)
            await scene.send(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - paid orders are never refunded by accident
        log.exception("Assassination of %s (ordered by %s) could not be finished.", target, author)
        await bot.log_to_owner(f"Zlecenie zabójstwa gracza <@{author.id}> (cel: <@{target.id}>) zostało opłacone, "
                               f"ale przerwał je błąd: `{type(exc).__name__}: {exc}`")
    log.info("Assassin: %s -> %s%s.", author, target, " (betrayal)" if betrayed else "")
    return True


# -- weekly PvP tournament -----------------------------------------------------------

async def spawn_pvp_tournament(scene: "Scene") -> Any:
    """Collect players for 3 minutes and run a knock-out PvP tournament.

    Returns the winner, or None when nobody signed up.
    """
    bot = scene.bot
    log.info("PvP tournament spawn.")
    if not bot.debug:
        await scene.send(f"<@&{bot.ids.hunters_role}>")

    await _send_image(scene, "tournament", "0.png")
    async with _SignUp(scene, _embed(TOURNAMENT_TITLE, TOURNAMENT_DESCR, 0x360006), emoji=SWORDS) as sign_up:
        await sign_up.open(_wait(bot, TOURNAMENT_SIGNUP))
        participants = await sign_up.collect()
    log.info("PvP tournament players: %s", [str(member) for member in participants])

    if not participants:
        await scene.send(MSG_TOURNAMENT_CANCELLED)
        return None
    if len(participants) == 1:
        await daily.rand_loot(scene, 2, participants[0], 0)

    random.shuffle(participants)
    await scene.send(MSG_TOURNAMENT_CLOSED.format(players=', '.join(user.mention for user in participants)))

    round_number = 1
    while len(participants) > 1:
        await scene.send(MSG_TOURNAMENT_ROUND.format(round=round_number))
        next_round: list[Any] = []
        if len(participants) % 2 == 1:
            lucky_player = participants.pop()
            next_round.append(lucky_player)
            await scene.send(MSG_TOURNAMENT_BYE.format(mention=lucky_player.mention))

        for i in range(0, len(participants), 2):
            player_1, player_2 = participants[i], participants[i + 1]
            _finished, winner = await boss.duel_fight(scene, [player_1, player_2], False)
            if winner is None:
                winner = random.choice([player_1, player_2])
                await scene.send(MSG_TOURNAMENT_DRAW.format(mention=winner.mention))
            next_round.append(winner)
            await scene.send(MSG_TOURNAMENT_DUEL_WINNER.format(mention=winner.mention))

        participants = next_round
        round_number += 1

    tournament_winner = participants[0]
    await _send_image(scene, "tournament", "1.gif")
    await scene.send(MSG_TOURNAMENT_WINNER.format(mention=tournament_winner.mention))
    log.info("PvP tournament won by %s (%s).", tournament_winner, tournament_winner.id)
    return tournament_winner


# -- ritual --------------------------------------------------------------------------

def ritual_rarity(participants: int) -> int:
    """Boss rarity (0-4) drawn for a ritual of ``participants`` players.

    A normal distribution whose mean grows with the circle (12 players = mythic).
    """
    mu = min(participants / RITUAL_FULL_CIRCLE, 1) * 4
    value = round(random.gauss(mu, 1.0))
    return max(0, min(4, value))


def ritual_duration(bot: "AlterBot") -> float:
    """How long the ritual waits for players (seconds)."""
    return _wait(bot, RITUAL_WAIT)


async def spawn_ritual(scene: "Scene") -> tuple[bool, int, bool, Any]:
    """Summoning ritual: the more players sign up, the mightier the boss.

    Returns ``(success, rarity, is_player_boss, player_boss)``.  On success the
    channel was cleared and the boss picture is already shown; the caller hands
    the boss to the boss cycle at once (nothing may be awaited in between).

    The sign-up leaves the channel free, like the duel.  Before the boss is
    shown the ritual waits for a running hunt / tower run to end and keeps the
    busy flag while it clears the channel and posts the picture.  Should the
    scheduled boss have started to arrive meanwhile, the ritual ends without a
    boss of its own - the scheduled one is never pushed aside.
    """
    bot = scene.bot
    state = bot.state
    log.info("Ritual spawn.")
    await _send_image(scene, "ritual", f"{random.randint(0, 3)}.png")
    async with _SignUp(scene, _embed(RITUAL_TITLE, RITUAL_DESCR, 0xFC0303), emoji=SPARKLES) as sign_up:
        await sign_up.open(ritual_duration(bot))
        users = await sign_up.collect()
        if await sign_up.deleted():
            log.info("Ritual interrupted: its message was deleted.")
            return False, 0, False, None

    if not users:
        await _send_image(scene, "ritual", "4.png")
        await scene.send(MSG_RITUAL_FAILED)
        return False, 0, False, None

    rarity = ritual_rarity(len(users))
    rarity += int((await modifiers.load_modifiers(bot))["rarity_boost"])
    if rarity == 3 or rarity > 4:
        rarity = 4
    while state.busy:  # a hunt or a tower run started during the sign-up: let it finish
        await asyncio.sleep(2)
    if not bot.boss_cycle.can_summon():
        log.info("Ritual of %d players ends without a boss: the scheduled one is arriving (phase %s).",
                 len(users), state.boss_phase.name)
        await scene.send(MSG_RITUAL_OVERTAKEN)
        return False, 0, False, None
    log.info("Ritual of %d players summons a boss of rarity %d.", len(users), rarity)

    with state.hold_busy("event:RITUAL"):
        await io.clear_channel(scene.channel)
        is_player_boss, player_boss = await boss.send_boss_image(scene, rarity)
    if not bot.boss_cycle.can_summon():
        # The scheduled boss began to arrive while the picture was being posted.
        log.info("Ritual boss shown, but the scheduled boss is arriving (phase %s).", state.boss_phase.name)
        await scene.send(MSG_RITUAL_OVERTAKEN)
        return False, 0, False, None
    return True, rarity, is_player_boss, player_boss
