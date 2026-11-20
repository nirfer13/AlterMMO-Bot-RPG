"""Boss fights: the boss picture, lobbies, single / group / duel fights, loot and roles.

Port of the legacy ``functions/functions_boss.py``.  The boss *life-cycle*
(when a boss comes, what happens after a fight) lives in
:mod:`altermmo.features.boss_cycle`; this module is what the cycle calls.

Differences from the legacy code (the gameplay itself is unchanged):

* fights never touch the boss phase and never run the post-fight reset: they
  return a :class:`~altermmo.features.fight_types.FightResult` and the cycle
  does the rest, so a fight that raises can no longer leave ``BOSSALIVE`` stuck;
* every "type the word" round goes through :func:`altermmo.core.challenge.ask`
  (listening starts before the prompt, the player's reaction time is judged by
  Discord's timestamps, a round broken by a connection problem is repeated
  instead of punishing the player).  The legacy "lag compensation" is gone;
* lobbies queue the players' messages for their whole duration, so a
  ``$zaatakuj`` / ``$tak`` typed while the bot is sending something is not lost;
* buttons next to the typed commands: "Zaatakuj" under the boss picture (see
  :mod:`altermmo.features.boss_cycle`) and in the single lobby, "Dołącz" /
  "Gotowy" in the legendary lobby.  Clicks go into the same queue as the typed
  messages, so both obey the same rules and timing.  The legendary lobby is
  one panel (roster, buttons, time left) edited in place instead of a series
  of messages; it is posted anew only when somebody must be pinged.  The
  fight rounds themselves stay typed - typing in time is the game;
* once the boss is dead the fight is won, whatever happens afterwards: each
  piece of bookkeeping (record, ranking, loot, roles) is guarded on its own and
  a failure is reported to the owner's log channel;
* members who left the server, missing roles and missing channels are handled
  instead of crashing with ``None.attribute``.

``functions_boss.fSaveRespawnToFile`` / ``fReadRespawnFromFile`` (the unused
``respawnTimeInfo.txt`` predecessor of the ``BOSS`` table) and ``getContext``
(replaced by ``bot.boss_scene()``) are not ported.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, Sequence

import discord

from ..core import challenge, io, timing, ui
from ..core.challenge import Outcome
from ..settings import ASSETS_DIR, CONFIG_DIR
from . import achievements, boss_cycle, database, expsum, fight_types, general, modifiers, patrons, xpsteal

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- numbers of the game ------------------------------------------------------------

#: Boss life by rarity: (min, max) of ``random.randint``; unknown rarities use the last one.
BOSS_HP_RANGES: dict[int, tuple[int, int]] = {0: (4, 6), 1: (6, 9), 2: (9, 12), 3: (15, 20)}
BOSS_HP_FALLBACK = (15, 20)

#: Experience for beating the kill record.
RECORD_EXP = 1500
#: Seconds for the very first word of a boss fight.
FIRST_ROUND_TIME = 7
#: Seconds per word against a mythic boss / in a group fight (before modifiers).
MYTHIC_ROUND_TIME = 1.5
GROUP_ROUND_TIME = 4
GROUP_RARITY = 3
GROUP_SIZE = 4
#: Mistakes the whole party may make before the boss wins.
GROUP_SPARE_CHANCES = 1

#: Single lobby: 4 steps of 15 s - one minute (1 s steps in debug mode) for another player to object.
LOBBY_STEPS = 4
LOBBY_STEP = 15
LOBBY_STEP_DEBUG = 1
#: Hard limit for typing the spell, counted from the moment it is shown.
SPELL_TIME = 15

#: Group lobby: steps of 300 s; after the fifth silent one the boss leaves.
GROUP_LOBBY_STEP = 300
GROUP_LOBBY_STEPS = 6
GROUP_CONFIRM_TIME = 60
#: A lobby panel is edited at most once per this many seconds (clicks in between are gathered).
PANEL_EDIT_INTERVAL = 1.0

DUEL_HP = 3000
DUEL_ROUND_TIME = 5
DUEL_MAX_TURNS = 40
DUEL_SPELL_TIME = 30
DUEL_SPELL_TRIES = 10
DUEL_RARITY = 2
DUEL_LOSER_POINTS = -12

#: Used when the guild does not tell its upload limit (10 MiB, Discord's lowest).
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024

LOOT_EXP = 0
LOOT_EGG = 10
LOOT_PET_LEVEL = 12
LOOT_REBIRTH_STONE = 13
LOOT_MIRROR = 14
LOOT_SKILL_GEM = 15
#: Appended to the drop line of a new skill gem (a player can collect several).
SKILL_GEM_HINT = "Działa tylko aktywny klejnot - zmienisz go komendą **$wybierzklejnot numer**."

_LOOT_TABLES: dict[int, str] = {
    0: "loot_details_Normal",
    1: "loot_details_Rare",
    2: "loot_details_Epic",
    3: "loot_details_Legend",
    4: "loot_details_Mythic",
}
_LOOT_TABLE_FALLBACK = "loot_details_Legend"

_PET_SKILL_KEYS = ("CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC", "SLOW_PERC")

# -- words ---------------------------------------------------------------------------

#: The spell that decides who fights the boss / who starts a duel.
SPELL_WORDS: tuple[str, ...] = (
    "Zabawowy", "Spolegliwy", "Słonina", "Serownik", "Onegdaj", "Monidło", "Koafiura", "Blurb",
    "Prodiż", "Wszeteczeństwo", "Frymuśny", "Aha", "Kombinatoryka", "Rzęsiście", "Ancymonek",
    "Konkurencja", "Eksploracja", "Przepocony", "Śródziemie", "Ofiarny", "Zdelegalizowany",
    "Zakochany", "Zakłopotany", "Zdematerializowany", "Smoczy", "Zauroczony", "Zawistny",
    "Zaistniały", "Zorganizowany", "Demineralizacja",
)

#: (word to type, prompt) for normal, rare, epic and legendary bosses.
BOSS_ACTIONS: tuple[tuple[str, str], ...] = (
    ("unik", "Boss szarżuje na Ciebie! Wpisz **U N I K**"),
    ("atak", "Boss zawahał się! Teraz! Wpisz **A T A K**"),
    ("paruj", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **P A R U J**"),
    ("skok", "Boss próbuje ataku w nogi, wpisz **S K O K**"),
    ("bieg", "Boss szykuje potężny atak o szerokim zasięgu, wpisz **B I E G**"),
    ("turlaj", "Boss atakuje w powietrzu, wpisz **T U R L A J**"),
    ("czaruj", "Boss rzuca klątwę, wpisz **C Z A R U J**"),
    ("blok", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K**"),
    ("skacz", "Boss próbuje ataku w nogi, wpisz **S K A C Z**"),
    ("akcja", "Boss szarżuje na Ciebie, zrób coś, wpisz **A K C J A**"),
    ("krzyk", "Nie masz pojęcia co robić, wpisz **K R Z Y K**"),
    ("ruch", "Musisz zrobić cokolwiek, wpisz **R U C H**"),
    ("posuw", "Boss rzuca głazem w Twoją stronę, wpisz **P O S U W**"),
    ("impet", "Dostrzegasz szansę na uderzenie, wpisz **I M P E T**"),
    ("zryw", "Pojawiła się chwila zawachania potwora, wpisz **Z R Y W**"),
)

#: (word to type, prompt) for mythic bosses.
MYTHIC_ACTIONS: tuple[tuple[str, str], ...] = (
    ("zmykaj", "Boss szarżuje na Ciebie! Wpisz **Z M Y K A J**"),
    ("agresja", "Boss zawahał się! Teraz! Wpisz **A G R E S J A**"),
    ("parada", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **P A R A D A**"),
    ("uskok", "Boss próbuje ataku w nogi, wpisz **U S K O K**"),
    ("sprint", "Boss szykuje potężny atak o szerokim zasięgu, wpisz **S P R I N T**"),
    ("kulanie", "Boss atakuje w powietrzu, wpisz **K U L A N I E**"),
    ("zaklęcie", "Boss rzuca klątwę, wpisz **Z A K L Ę C I E**"),
    ("blokada", "Boss atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K A D A**"),
    ("panika", "Nie masz pojęcia co robić, wpisz **P A N I K A**"),
    ("obrót", "Musisz zrobić cokolwiek, wpisz **O B R Ó T**"),
)

#: (word to type, prompt) for duels between two players.
DUEL_ACTIONS: tuple[tuple[str, str], ...] = (
    ("zmykaj", "Przeciwnik szarżuje na Ciebie! Wpisz **Z M Y K A J**"),
    ("agresja", "Przeciwnik zawahał się! Teraz! Wpisz **A G R E S J A**"),
    ("parada", "Przeciwnik atakuje, nie masz miejsca na ucieczkę, wpisz **P A R A D A**"),
    ("uskok", "Przeciwnik próbuje ataku w nogi, wpisz **U S K O K**"),
    ("sprint", "Przeciwnik szykuje potężny atak o szerokim zasięgu, wpisz **S P R I N T**"),
    ("kulanie", "Przeciwnik atakuje w powietrzu, wpisz **K U L A N I E**"),
    ("zaklęcie", "Przeciwnik rzuca klątwę, wpisz **Z A K L Ę C I E**"),
    ("blokada", "Przeciwnik atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K A D A**"),
    ("panika", "Nie masz pojęcia co robić, wpisz **P A N I K A**"),
    ("obrót", "Musisz zrobić cokolwiek, wpisz **O B R Ó T**"),
    ("kontra", "Wróg odsłania gardę! Wpisz **K O N T R A**"),
    ("barykada", "Wróg próbuje Cię przycisnąć! Wpisz **B A R Y K A D A**"),
    ("skok", "Na ziemi pojawia się pułapka! Wpisz **S K O K**"),
    ("cios", "Masz okazję uderzyć! Wpisz **C I O S**"),
    ("czar", "Twoja różdżka drży w dłoni – wpisz **C Z A R**"),
    ("podstęp", "Wróg daje się nabrać na Twój ruch – wpisz **P O D S T Ę P**"),
    ("unik", "Strzały lecą prosto w Ciebie – wpisz **U N I K**"),
    ("okrzyk", "Dodaj sobie odwagi! Wpisz **O K R Z Y K**"),
    ("leczenie", "Czujesz słabnięcie – wpisz **L E C Z E N I E**"),
    ("zamach", "Masz szansę na potężny zamach – wpisz **Z A M A C H**"),
)

# -- texts ---------------------------------------------------------------------------

LOOT_IMAGE = "https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png"

_THUMB_BASE = "https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/"
_BOSS_THUMBS: dict[int, str] = {
    0: _THUMB_BASE + "large-green-square_1f7e9.png",
    1: _THUMB_BASE + "large-blue-square_1f7e6.png",
    2: _THUMB_BASE + "large-purple-square_1f7ea.png",
}
_BOSS_THUMB_FALLBACK = _THUMB_BASE + "large-orange-square_1f7ea.png"
_BOSS_COLOURS: dict[int, int] = {0: 0xFFFFFF, 1: 0x0000FF, 2: 0x800080}
_BOSS_COLOUR_FALLBACK = 0x7F5A00
_BOSS_ADJECTIVES: dict[int, str] = {0: "Zwykły", 1: "Rzadki", 2: "Epicki", 3: "Legendarny", 4: "Mityczny"}
_BOSS_KINDS: dict[int, str] = {0: "zwykły", 1: "rzadki", 2: "epicki", 4: "MITYCZNY"}

_ATTACK_HINT = "Kliknij **Zaatakuj** albo wpisz **$zaatakuj**, żeby rozpocząć walkę! ⚔️"

MSG_SINGLE_LOBBY = ("Jesteś sam?... *Inny gracz może kliknąć **Zaatakuj** albo wpisać **$zaatakuj**, "
                    "jeśli chce zawalczyć o bossa!* Zgłoszenia kończą się {deadline}.")
MSG_SINGLE_LOBBY_ALONE = "Jesteś sam?... *Nikt inny nie stanął do walki - <@{user_id}> walczy sam!*"
MSG_SINGLE_LOBBY_RIVAL = "Jesteś sam?... *Nie! <@{user_id}> też chce zawalczyć o bossa!*"
MSG_ALREADY_FIGHTING = "Już stoisz do walki z tym bossem - czekamy, czy ktoś jeszcze się zgłosi ⚔️"
MSG_CALM_DOWN = ('"**SPOKÓJ!!!**" - *słyszyscie głos w swojej głowie.* "Zachowajcie resztki honoru '
                 'i wystawcie do walki najsilniejszego z Was."')
MSG_SPELL_COMING = '"Pierwszy, który PŁYNNIE wypowie zaklęcie, które zaraz zdradzę, będzie godzien walki ze mną!"'
MSG_NOBODY_WORTHY = '"Pfff... Miernoty. Nikt z Was nie jest godzien."'
LOG_SPELL_TIMEOUT = "Cała grupa nie zdążyła wpisac hasła."

MSG_SINGLE_START = ("Zaatakowałeś bossa <@{user_id}>! <:REEeee:790963160495947856> Wpisz pojawiające się "
                    "słowa tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez "
                    "spacji! Przygotuj się!")
MSG_SINGLE_WON = ("Brawo <@{user_id}>! Pokonałeś bossa! <:POGGIES:790963160491753502>"
                  "<:POGGIES:790963160491753502><:POGGIES:790963160491753502>")
MSG_SINGLE_RECORD = "Pobiłeś rekord i zgarniasz dodatkowe 1500 doświadczenia na discordzie!"
MSG_MISTAKE_LOST = ("Pomyliłeś się! <:PepeHands:783992337377918986> Boss pojawi się później! "
                    "<:RIP:912797982917816341>")
MSG_TOO_SLOW_LOST = ("Niestety nie zdążyłeś! <:Bedge:970576892874854400> Boss pojawi się później! "
                     "<:RIP:912797982917816341>")
MSG_MISTAKE_PROTECTED = "Pomyliłeś się, ale Twój towarzysz Cię chroni!"
MSG_TOO_SLOW_PROTECTED = "Nie zdążyłeś, ale Twój towarzysz Cię chroni!"

MSG_GROUP_LOBBY = ("**Znajdź więcej chętnych do walki. Chyba nie myślisz, że sam dasz radę?...** - "
                   "*słyszysz głos w swojej głowie*... 30...")
MSG_GROUP_START = ("Zaatakowaliście bossa{mentions}! <:REEeee:790963160495947856> Wpiszcie **słowa "
                   "przypisane do Was** tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! "
                   "Wpiszcie słowa bez spacji! Przygotujcie się!")
MSG_GROUP_WON = ("Wow! Świetnie{mentions}! Pokonaliście legendarnego bossa! <:POGGIES:790963160491753502>"
                 "<:POGGIES:790963160491753502><:POGGIES:790963160491753502>")
MSG_GROUP_RECORD = "Pobiliście rekord i zgarniacie dodatkowe 1500 doświadczenia na discordzie!"
MSG_GROUP_GAVE_UP = ("**Miernoty... Wiedziałem, że nie jesteście godni walki ze mną.**  - "
                     "*słyszycie głos w swojej głowie.*")

# The legendary lobby panel: the boss's voice, what to do and until when, the party.
GROUP_PANEL_TITLE = "⚔️ Drużyna na legendarnego bossa"
VOICE_START = ("**Znajdź więcej chętnych do walki. Chyba nie myślisz, że sam dasz radę?...** - "
               "*słyszysz głos w swojej głowie*...")
VOICE_COWARDS = "**Tchórzycie, co? Pff...** - *słyszycie głos w swojej głowie*..."
VOICE_ALONE = "**Jesteś sam! Zbierz więcej łowców!** - *słyszysz głos w swojej głowie*..."
VOICE_MORE = ("**Nieźle, jest już Was {count}. Zbierzcie więcej łowców!** - "
              "*słyszycie głos w swojej głowie*...")
VOICE_LAST_CHANCE = "**A myślałem, że czas na rozrywkę...** - *słyszycie głos w swojej głowie*..."
VOICE_READY = "**Jesteście gotowi do ataku?** - *słyszycie głos w swojej głowie*..."
VOICE_REGROUP = "**Zbierzcie drużynę przed atakiem...** - *słyszycie głos w swojej głowie*..."
VOICE_SET = "**Wreszcie ktoś godny walki ze mną!** - *słyszycie głos w swojej głowie*..."
GROUP_HOW_TO_JOIN = ("Kliknij **Dołącz** albo wpisz **$zaatakuj**, jeśli chcesz dołączyć do walki! "
                     "Potrzeba {size} łowców. Jeśli drużyna się nie zbierze, boss odejdzie {deadline}.")
GROUP_HOW_TO_CONFIRM = ("Kliknijcie **Gotowy** albo wpiszcie **$tak**. Czas na potwierdzenie mija "
                        "{deadline} - kto nie zdąży, wypada z drużyny.")
GROUP_ROSTER = "Drużyna ({count}/{size})"
GROUP_ROSTER_EMPTY = "*nikogo jeszcze nie ma*"
#: Posted with a new panel, so that the mentioned players / role get a notification.
MSG_GROUP_READY_PING = "Jesteście gotowi do ataku?{mentions}"
MSG_GROUP_REGROUP_PING = "Zbierzcie drużynę przed atakiem...{mentions}"
MSG_GROUP_LAST_CHANCE = "Ostatnia szansa, <@&{role_id}>! Kliknij **Dołącz** albo wpisz **$zaatakuj**."
MSG_GROUP_SET = "Do walki z legendarnym bossem stają:{mentions}"
MSG_GROUP_NOT_GATHERED = "Drużyna się nie zebrała."
# Answers to clicks in the legendary lobby.
MSG_ALREADY_IN_PARTY = "Już jesteś w drużynie! ⚔️"
MSG_PARTY_FULL = "Drużyna jest już w komplecie - poczekaj, czy wszyscy potwierdzą gotowość."
MSG_NOT_IN_PARTY = "Ten przycisk jest dla łowców z drużyny."
# The same for everybody who clicks: one line on the channel is enough (see ui.reply).
ui.GENERAL_NOTICES.update({MSG_PARTY_FULL, MSG_NOT_IN_PARTY})
MSG_ALREADY_READY = "Już potwierdziłeś gotowość ✅"

JOIN_LABEL = "Dołącz"
READY_LABEL = "Gotowy"

MSG_DUEL_START = ("Rozpoczyna się walka pomiędzy <@{first}> oraz <@{second}>! "
                  "<:REEeee:790963160495947856>")
MSG_DUEL_SPELL_COMING = ("Pierwszy, który PŁYNNIE wypowie zaklęcie, które zaraz się pojawi, "
                         "będzie rozpoczynał pojedynek!")
MSG_DUEL_RANDOM_START = "Ten, który atakuje pierwszy, będzie wybrany losowo."
LOG_DUEL_SPELL_TIMEOUT = "Timeout podczas rozpoczynania pojedynku."
MSG_DUEL_DRAW = "Walka trwa zbyt długo. Remis!"
MSG_DUEL_INTERRUPTED = ("Pojedynek został przerwany z powodu problemów technicznych "
                        "<:Sadge:936907659142111273> Remis!")
MSG_DUEL_MISTAKE = "Pomyliłeś się! <:PepeHands:783992337377918986> Nie zadajesz obrażeń!"
MSG_DUEL_MISTAKE_WEAK = "Pomyliłeś się, ale Twój towarzysz i tak wyprowadza słaby atak!"
MSG_DUEL_NOT_YOUR_TURN = ("Nie Twoja kolej! <:PepeHands:783992337377918986> "
                          "Przeciwnik zadaje maksymalne obrażenia!")
MSG_DUEL_NOT_YOUR_TURN_PROTECTED = "Nie Twoja kolej, ale Twój towarzysz Cię broni!"
MSG_DUEL_TOO_SLOW = "Niestety nie zdążyłeś! <:Bedge:970576892874854400> Nie zadajesz obrażeń!"
MSG_DUEL_TOO_SLOW_WEAK = "Nie zdążyłeś, ale Twój towarzysz i tak wyprowadza słaby atak!"

MSG_PET_ATTACKS = "Twój towarzysz wyprowadza atak!"

MSG_DEAD = ("Na kanale <#{channel_id}> poległeś z ręki {killer} <@{user_id}>, a groziło to banem... "
            "<:RIP:912797982917816341> Musisz odpocząć od przygód aż do momentu zabicia następnego bossa.")

MSG_FLEX = ("Potężny <:GigaChad:970665721321381958> <@{user_id}> napina swe sprężyste, naoliwione muskuły! "
            "Co za widok, robi wrażenie! <:pogu:882182966372106280>")
MSG_FLEX_DENIED = "<:KEKW:936907435921252363> **Miernota** <:2Head:882184634572627978>"
MSG_SLAYER_ONLY = "<:KEKW:936907435921252363> **Kpisz sobie, miernoto?** <:2Head:882184634572627978>"
MSG_BAD_COLOUR = "Podaj kolor w formacie HEX, np. **$kolor ff0000** <:madge:882184635474386974>"
MSG_COLOUR_FAILED = "Nie udało się zmienić koloru roli <:Sadge:936907659142111273>"
MSG_ICON_CHANGED = "Ikona została zmieniona Wielki Wojowniku. <:peepoglad:895774173887094874>"
MSG_ICON_BAD_FORMAT = "<@{user_id}>, zły format ikony. Poprawny format to .png. <:madge:882184635474386974>"
MSG_ICON_MISSING = "Dodaj ikonę w wiadomości z komendą - format .png <:madge:882184635474386974>"
MSG_ICON_DOWNLOAD_FAILED = "Nie udało się pobrać ikony, spróbuj ponownie <:Sadge:936907659142111273>"
MSG_ICONS_UNAVAILABLE = ("Serwer nie może teraz używać ikon ról (potrzebny jest odpowiedni poziom "
                         "ulepszeń serwera) <:Sadge:936907659142111273>")
MSG_ICON_TOO_BIG = ("Ta ikona jest za duża - Discord przyjmuje ikony ról do 256 KB. "
                    "Zmniejsz obrazek i spróbuj ponownie <:madge:882184635474386974>")
MSG_ICON_REJECTED = ("Discord nie przyjął tego obrazka (ikona roli: plik .png do 256 KB) "
                     "<:Sadge:936907659142111273>")
MSG_ICON_FORBIDDEN = ("Nie mam uprawnień, żeby zmienić tę rolę <:Sadge:936907659142111273> "
                      "Dałem znać administratorowi.")
MSG_ICON_NOT_SENT = ("Nie udało się teraz zmienić ikony (problem z połączeniem), spróbuj ponownie "
                     "za chwilę <:Sadge:936907659142111273>")
#: Discord's size limit for a role icon (bytes).
ROLE_ICON_LIMIT = 256 * 1024
#: Discord error code: "this server needs more boosts to do that".
_NEEDS_BOOSTS = 50101

_HEX_COLOUR = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{1,6})$")


# -- small helpers -------------------------------------------------------------------

def _pets():
    """The pets feature (imported on first use: it is a sibling that may import us)."""
    from . import pets
    return pets


def _skills():
    """The skill gems feature (imported on first use, like :func:`_pets`)."""
    from . import skills
    return skills


def _id_of(member_or_id: Any) -> int | None:
    """Id of a member (or a bare id); None when there is nobody."""
    if member_or_id is None:
        return None
    try:
        return int(getattr(member_or_id, "id", member_or_id))
    except (TypeError, ValueError):
        return None


def _mentions(players: Sequence[Any]) -> str:
    """Legacy ``playerListString``: `` <@1> <@2> `` (leading space, one after every mention)."""
    return " " + "".join(f"<@{player.id}> " for player in players)


def _num(value: Any) -> str:
    """A number as players should read it: ``3000.0`` -> ``3000``, ``Decimal("7.50")`` -> ``7.5``."""
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _first_number(text: str) -> int | None:
    """The first whole number in a loot description (``"250 doświadczenia ..."`` -> 250)."""
    numbers = [int(word) for word in str(text).split() if word.isdigit()]
    return numbers[0] if numbers else None


def _scaled_hp(base: int, percent: float) -> int:
    """``base`` changed by ``percent`` %, cut down to a whole number, never below 1.

    Exact decimal maths: the legacy float expression lost a point now and then
    (``20 * 1.15`` is not always 23 in binary floating point).
    """
    scaled = Decimal(int(base)) * (Decimal(100) + Decimal(str(percent))) / Decimal(100)
    return max(1, int(scaled))


def _avatar_url(member: Any) -> str | None:
    avatar = getattr(member, "display_avatar", None)
    url = getattr(avatar, "url", None)
    return str(url) if url else None


async def _pet_skills(bot: "AlterBot", player_id: int) -> dict[str, float]:
    """The player's pet bonuses as plain floats (the database hands out ``Decimal``)."""
    raw = await _pets().get_pet_skills(bot, player_id)
    if not isinstance(raw, dict):
        raw = {}
    return {key: float(raw.get(key) or 0) for key in _PET_SKILL_KEYS}


def _roll(chance_percent: float) -> bool:
    """True with ``chance_percent`` % probability (the legacy ``random()*100 < x`` test)."""
    return random.random() * 100 < chance_percent


async def _decided(bot: "AlterBot", result: "fight_types.FightResult") -> None:
    """The fight is decided: the boss life-cycle remembers it before anything else happens.

    A boss is fought once.  After this call an error, the time limit of the
    encounter or a restart of the bot can no longer bring this boss back
    (:meth:`BossCycle.mark_decided`).  Never raises.
    """
    cycle = getattr(bot, "boss_cycle", None)
    if cycle is not None:
        await cycle.mark_decided(result)


async def _say_lost(scene: "Scene", text: str) -> None:
    """Tell the channel the fight is lost; a message that cannot be sent changes nothing."""
    try:
        await scene.send(text)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the fight stays lost (it used to become "interrupted": a second go)
        log.exception("Could not announce the lost boss fight.")


async def _guarded(bot: "AlterBot", what: str, awaitable: Awaitable[Any], default: Any = None) -> Any:
    """Await one piece of bookkeeping after a won fight.

    The fight is already won: a failing step is logged and reported to the
    owner (who can hand the reward out by hand) and the rest continues.
    """
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Step after a won fight failed: %s", what)
        await bot.log_to_owner(f"Błąd po wygranej walce ({what}): `{type(exc).__name__}: {exc}`. "
                               "Sprawdź, czy nagrody nie trzeba przyznać ręcznie.", ping=True)
        return default


@contextlib.contextmanager
def _listening(bot: "AlterBot", channel: Any) -> Iterator[asyncio.Queue]:
    """Queue every player message written on ``channel`` while the block runs.

    Lobbies ask several questions in a row; with a plain ``wait_for`` after each
    prompt a ``$zaatakuj`` / ``$tak`` typed while the bot was still sending (or
    adding a reaction) was lost.  Bots and other channels never get in.
    """
    queue: asyncio.Queue = asyncio.Queue()
    channel_id = channel.id

    async def on_message(message: discord.Message) -> None:
        if message.author.bot or message.channel.id != channel_id:
            return
        queue.put_nowait(message)

    bot.add_listener(on_message, "on_message")
    try:
        yield queue
    finally:
        bot.remove_listener(on_message, "on_message")


async def _next_message(queue: asyncio.Queue, accept: Callable[[Any], bool], timeout: float) -> Any:
    """The first queued message that ``accept`` likes within ``timeout`` s, else None."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message = await asyncio.wait_for(queue.get(), remaining)
        except asyncio.TimeoutError:
            return None
        if accept(message):
            return message


def _forget_queued(queue: asyncio.Queue) -> None:
    """Drop what was written before a question was asked."""
    while not queue.empty():
        queue.get_nowait()


def _says(message: Any, text: str) -> bool:
    return str(message.content).strip().lower() == text.lower()


@dataclass(frozen=True)
class _Knock:
    """A click on a lobby button, queued among the typed messages of the lobby."""

    key: str
    member: Any
    interaction: Any


def _who(item: Any) -> Any:
    """The player behind a queued lobby item (a typed message or a click)."""
    return item.member if isinstance(item, _Knock) else item.author


def _is(item: Any, command: str, key: str) -> bool:
    """True when ``item`` is the typed ``command`` or a click on the button ``key``."""
    if isinstance(item, _Knock):
        return item.key == key
    return _says(item, command)


async def _knock(inbox: asyncio.Queue, interaction: Any, key: str) -> str:
    """Queue an accepted click for the lobby and acknowledge it (the panel shows the result)."""
    inbox.put_nowait(_Knock(key, interaction.user, interaction))
    await ui.ack(interaction)
    return ui.DROP


@contextlib.contextmanager
def _lobby_door(bot: "AlterBot", door: Callable[[Any], Awaitable[None]]) -> Iterator[None]:
    """While the block runs, a click on the boss picture's "Zaatakuj" button goes to ``door``.

    (Like a typed ``$zaatakuj`` goes to the running lobby.)
    """
    cycle = getattr(bot, "boss_cycle", None)
    if cycle is not None:
        cycle.lobby_door = door
    try:
        yield
    finally:
        if cycle is not None and cycle.lobby_door is door:
            cycle.lobby_door = None


async def _answer_door(allow: Callable[[Any, str], Awaitable[str | None]], interaction: Any, key: str) -> None:
    """Handle a click that came through the lobby door like a click on the lobby's own ``key`` button."""
    reason = await allow(interaction, key)
    if reason is None:
        await ui.ack(interaction)
    elif reason != ui.DROP:
        await ui.reply(interaction, reason)


async def _ask(bot: "AlterBot", channel: Any, prompt: str, *, players: Sequence[Any],
               limit: float) -> challenge.RoundResult | None:
    """One "type the word" round that never blames the players for our lag.

    A disrupted round is announced and asked again; None after
    ``challenge.MAX_CONSECUTIVE_DISRUPTIONS`` disrupted rounds in a row.
    """
    for attempt in range(1, challenge.MAX_CONSECUTIVE_DISRUPTIONS + 1):
        result = await challenge.ask(bot, channel, prompt, players=players,
                                     timeout=timing.timeout(max(float(limit), 0.0)))
        if result.outcome is not Outcome.DISRUPTED:
            return result
        log.warning("Fight round disrupted (%d/%d in a row).", attempt, challenge.MAX_CONSECUTIVE_DISRUPTIONS)
        if attempt < challenge.MAX_CONSECUTIVE_DISRUPTIONS:
            await challenge.notify_disrupted(channel)
    return None


async def _boss_round(bot: "AlterBot", channel: Any, prompt: str, *, players: Sequence[Any],
                      limit: float) -> challenge.RoundResult:
    """:func:`_ask` for boss fights: repeated disruptions interrupt the fight."""
    result = await _ask(bot, channel, prompt, players=players, limit=limit)
    if result is None:
        raise fight_types.FightInterrupted(
            f"{challenge.MAX_CONSECUTIVE_DISRUPTIONS} fight rounds in a row were disrupted")
    return result


# -- loot ----------------------------------------------------------------------------

def _loot_table(rarity: int) -> list[dict[str, Any]]:
    config = json.loads((CONFIG_DIR / "lootConfig.json").read_text(encoding="utf-8"))
    return config[_LOOT_TABLES.get(int(rarity), _LOOT_TABLE_FALLBACK)]


async def _grant_loot(scene: "Scene", hunter: Any, loot: dict[str, Any]) -> bool:
    """Hand one loot item to ``hunter``; False when there was nothing to give.

    (A pet that cannot level up any more, a player without room for another
    egg...).  Items without a mechanic of their own are text only - the owner
    hands them out from the log channel.
    """
    bot = scene.bot
    loot_id = loot["id"]
    if loot_id == LOOT_EXP:
        experience = _first_number(loot["descr"])
        if experience is None:
            log.warning("Loot description without an amount of experience: %r", loot["descr"])
            return False
        expsum.add_file_exp(bot, hunter.id, experience)
        return True
    if loot_id == LOOT_EGG:
        return bool(await _pets().assign_pet(scene, hunter))
    if loot_id == LOOT_PET_LEVEL:
        return bool(await _pets().level_up_pet(scene, hunter.id))
    if loot_id == LOOT_REBIRTH_STONE:
        return bool(await _pets().assign_rebirth_stone(scene, 1, hunter.id))
    if loot_id == LOOT_MIRROR:
        return bool(await _pets().assign_mirror(scene, 1, hunter.id))
    if loot_id == LOOT_SKILL_GEM:
        return bool(await _skills().assign_skill(scene, hunter))
    if loot_id == xpsteal.LOOT_ID:
        before = len(xpsteal.pending(bot, hunter.id))
        xpsteal.grant(bot, hunter.id, xpsteal.amount_from_description(loot["descr"]))
        return len(xpsteal.pending(bot, hunter.id)) > before
    return True


async def _drop(scene: "Scene", hunter: Any, loot: dict[str, Any], *, bonus: bool) -> str:
    """Grant one drop and return its line(s) for the drop message ("" when nothing dropped)."""
    bot = scene.bot
    if bonus and loot["id"] == LOOT_EGG:
        return ""  # a second egg never drops (legacy rule)
    try:
        granted = await _grant_loot(scene, hunter, loot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one broken item must not cost the player the others
        log.exception("Could not grant loot %s to %s (%s).", loot.get("id"), hunter, hunter.id)
        await bot.log_to_owner(
            f"Nie udało się przyznać łupu graczowi {hunter} (<@{hunter.id}>): {loot.get('descr')} "
            f"(`{type(exc).__name__}: {exc}`). Przyznaj ręcznie.", ping=True)
        return ""
    if not granted:
        log.info("Loot %s rolled for %s but there was nothing to grant.", loot["id"], hunter.id)
        return ""
    line = "👉 " + loot["descr"] + (" (Bonus)\n" if bonus else "\n")
    if loot["id"] == xpsteal.LOOT_ID and not bonus:
        line += xpsteal.COMMAND_HINT + "\n"
    if loot["id"] == LOOT_SKILL_GEM and not bonus:
        line += SKILL_GEM_HINT + "\n"
    return line


async def rand_loot(scene: "Scene", rarity: int, hunter: Any, boost_percent: float) -> str:
    """Draw the loot of a boss of ``rarity`` for ``hunter``, grant it and post the drop embed.

    ``boost_percent`` raises every weight; a weight above 100 % is a certain
    drop plus a chance for a second, "(Bonus)" one.  Returns the drop message
    (one ``👉 ...`` line per item) for the owner's log.
    """
    bot = scene.bot
    boost = int(boost_percent)
    drop_message = ""
    thefts_before = len(xpsteal.pending(bot, hunter.id))
    for loot in _loot_table(int(rarity)):
        weight = (boost / 100 + 1) * loot["weight"]
        if weight > 100:
            drop_message += await _drop(scene, hunter, loot, bonus=False)
            if random.random() * 100 <= weight - 100:
                drop_message += await _drop(scene, hunter, loot, bonus=True)
        elif random.random() * 100 <= weight:
            drop_message += await _drop(scene, hunter, loot, bonus=False)

    embed = discord.Embed(title="Boss Drop - " + str(hunter), url=LOOT_IMAGE,
                          description="Boss wydropił:\n" + drop_message, color=0xFCDB03)
    embed.set_thumbnail(url=LOOT_IMAGE)
    embed.set_footer(text="Gratulacje!")
    try:
        await scene.send(embed=embed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the loot is already granted; keep the caller's log line
        log.warning("Loot of %s granted but the drop embed could not be posted: %s: %s",
                    hunter, type(exc).__name__, exc)
        await bot.log_to_owner(f"Nie udało się pokazać łupu gracza {hunter} na kanale "
                               f"(`{type(exc).__name__}`); łup został przyznany.")
    if len(xpsteal.pending(bot, hunter.id)) > thefts_before:
        await xpsteal.remind(scene, hunter)  # how to use the theft: ping + private message
    log.info("Loot for %s (rarity %s, boost %s%%): %r", hunter, rarity, boost, drop_message)
    return drop_message


# -- the boss appears ----------------------------------------------------------------

def rarity_for_respawn_time(seconds: float, *, debug: bool) -> int:
    """Boss rarity that belongs to a respawn delay (the longer the wait, the rarer)."""
    respawn = int(seconds)
    if not debug:
        if 0 <= respawn < 14400:        # 0-4 h
            return 0
        if 14400 <= respawn < 28800:    # 4-8 h
            return 1
        if 28800 <= respawn < 39600:    # 8-11 h
            return 2
        if 39600 <= respawn <= 45000:   # 11-12.5 h
            return 3
        return 0
    if 0 <= respawn < 12:
        return 0
    if 10 <= respawn < 17:
        return 1
    if 17 <= respawn <= 20:
        return 2
    if 20 <= respawn <= 25:
        return 3
    return 0


def random_boss_hp(rarity: int) -> int:
    """Number of correct words needed to kill a boss of ``rarity`` (before modifiers)."""
    low, high = BOSS_HP_RANGES.get(int(rarity), BOSS_HP_FALLBACK)
    return random.randint(low, high)


def _boss_reward(rarity: int) -> int:
    """Experience for the player who posed as a boss that was not beaten."""
    return (int(rarity) + 1) * 500


def _boss_picture(rarity: int):
    """A random picture of a boss of ``rarity`` (``assets/boss/<n>.gif``)."""
    first = pow(10, int(rarity))
    if first == 1:
        first = 0
    return ASSETS_DIR / "boss" / f"{random.randint(0, 7) + first}.gif"


async def send_boss_image(scene: "Scene", rarity: int) -> tuple[bool, Any]:
    """Show the boss: its picture (or the avatar of the player posing as it) and the embed.

    The embed carries the permanent "Zaatakuj" button; the boss cycle takes it
    off when the boss is engaged or leaves.  Returns ``(is_player_boss,
    player_boss)``; ``player_boss`` is the member who earns experience when the
    boss is not beaten, or None.
    """
    bot = scene.bot
    rarity = int(rarity)

    percentage = random.randint(0, 100)
    player_boss = patrons.get_patron(bot) if percentage >= 50 else None

    # The shrine may have chosen who the next boss is.
    forced_id = (await modifiers.load_modifiers(bot))["player_id"]
    if forced_id > 0:
        player_boss = await bot.member(forced_id)
        if player_boss is None:
            log.info("The player chosen as the boss (%s) left the server; a monster comes instead.", forced_id)

    is_player_boss = player_boss is not None
    add_desc = ""
    if is_player_boss:
        add_desc = ("\n\n*Bossem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, "
                    "to ten gracz zgarnia " + str(_boss_reward(rarity)) + " doświadczenia!*")
    picture = None if is_player_boss else _boss_picture(rarity)
    add_desc += await modifiers.load_desc_modifiers(bot)

    name = player_boss.name if is_player_boss else "boss"
    # The rarity is in the title in words and as its symbol (owner's request of 2026-11-20).
    symbol = boss_cycle.rarity_emoji(rarity)
    title = f"💀 {symbol} {_BOSS_ADJECTIVES.get(rarity, 'Legendarny')} {name}! {symbol} 💀"
    if rarity in _BOSS_KINDS:
        description = (f"Pojawił się {_BOSS_KINDS[rarity]} boss! Zabij go natychmiast, żeby zgarnąć nagrody! "
                       + _ATTACK_HINT + add_desc)
    else:
        description = ("Pojawił się LEGENDARNY boss! Nie dasz rady sam, będziesz potrzebował kompanów "
                       f"spośród <@&{bot.ids.hunters_role}>! " + _ATTACK_HINT + add_desc)
    embed = discord.Embed(title=title, description=description,
                          color=_BOSS_COLOURS.get(rarity, _BOSS_COLOUR_FALLBACK))
    embed.set_thumbnail(url=_BOSS_THUMBS.get(rarity, _BOSS_THUMB_FALLBACK))

    if is_player_boss:
        avatar = _avatar_url(player_boss)
        if avatar is not None:
            await scene.send(avatar)
    elif picture.is_file():
        await scene.send(file_path=picture)
    else:
        log.warning("Boss picture %s is missing; showing the boss without it.", picture)
    message = await boss_cycle.with_attack_button(lambda view: scene.send(embed=embed, view=view))
    cycle = getattr(bot, "boss_cycle", None)
    if cycle is not None:
        cycle.note_attack_button(message)
    log.info("Boss shown: rarity %s, player boss: %s.", rarity, player_boss)
    return is_player_boss, player_boss


# -- single fight --------------------------------------------------------------------

async def single_init(scene: "Scene", rarity: int) -> Any:
    """Decide who fights the boss alone.

    One minute for another player to object - with ``$zaatakuj`` or the
    "Zaatakuj" button under the lobby message (whose deadline Discord counts
    down); when somebody does, the first one to type the spell fights.
    Returns the fighter, or None when nobody typed the spell in time (the boss
    leaves).
    """
    bot = scene.bot
    channel = scene.channel
    attacker = scene.author
    step = LOBBY_STEP_DEBUG if bot.debug else LOBBY_STEP
    window = timing.timeout(step) * LOBBY_STEPS

    def objects(item: Any) -> bool:
        return _who(item).id != attacker.id and _is(item, "$zaatakuj", "object")

    with _listening(bot, channel) as inbox:
        async def allow(interaction: Any, key: str) -> str | None:
            if interaction.user.id == attacker.id:
                return MSG_ALREADY_FIGHTING
            return await _knock(inbox, interaction, key)

        async def door(interaction: Any) -> None:
            await _answer_door(allow, interaction, "object")

        async with ui.ClickView([ui.Choice("object", boss_cycle.ATTACK_LABEL, emoji=boss_cycle.ATTACK_EMOJI,
                                           style=discord.ButtonStyle.primary)], allow=allow) as view:
            with _lobby_door(bot, door):
                await view.send(channel, MSG_SINGLE_LOBBY.format(deadline=ui.when(window)))
                rival = await _next_message(inbox, objects, window)
            if rival is None:
                await view.close(content=MSG_SINGLE_LOBBY_ALONE.format(user_id=attacker.id))
                log.info("Boss fight: %s fights alone, nobody objected.", attacker)
                return attacker
            await view.close(content=MSG_SINGLE_LOBBY_RIVAL.format(user_id=_who(rival).id))

        log.info("Boss fight: %s also wants the boss; the spell decides.", _who(rival))
        async with io.safe_typing(channel):
            await timing.pause(2)
        await scene.send(MSG_CALM_DOWN)
        spell = random.choice(SPELL_WORDS)
        await timing.pause(6)
        await scene.send(MSG_SPELL_COMING)
        await timing.pause(8)

        _forget_queued(inbox)
        await scene.send('"Zaklęcie to **' + " ".join(spell.upper()) + '**"')
        with challenge.round_running(channel):  # a typed contest: no answers to clicks meanwhile
            winner = await _next_message(inbox, lambda message: _says(message, spell),
                                         timing.timeout(SPELL_TIME))

    if winner is None:
        await scene.send(MSG_NOBODY_WORTHY)
        await bot.log_to_owner(LOG_SPELL_TIMEOUT)
        log.info("Boss fight: nobody typed the spell in time.")
        return None
    log.info("Boss fight: %s typed the spell first.", winner.author)
    return winner.author


def _payout_note(fighters: Sequence[Any], rarity: int, is_player_boss: bool, player_boss: Any) -> tuple[str, Any]:
    """Owner-log sentence about the player boss after a lost fight, and who gets paid.

    A player who is the boss and fights "himself" gets nothing: losing on
    purpose must not be a way to farm experience.
    """
    if not is_player_boss or getattr(player_boss, "id", None) is None:
        return "", None
    if any(fighter.id == player_boss.id for fighter in fighters):
        return (" Bossem był " + player_boss.name + ", ale sam brał udział w walce, "
                "więc nagroda mu się nie należy."), None
    return (" Bossem był " + player_boss.name + " i należy mu się "
            + str(_boss_reward(rarity)) + " expa."), player_boss


async def _report_lost_fight(bot: "AlterBot", line: str, fighters: Sequence[Any], rarity: int,
                             is_player_boss: bool, player_boss: Any) -> None:
    note, paid = _payout_note(fighters, rarity, is_player_boss, player_boss)
    await bot.log_to_owner(line + note, ping=True)
    if paid is not None:
        expsum.add_file_exp(bot, paid.id, _boss_reward(rarity))


async def _settle_record(scene: "Scene", fighters: Sequence[Any], nick: str, turn_time: timedelta,
                         announcement: str, owner_line: str) -> None:
    """Store a new kill record and report the 1500 experience for it to the owner.

    Exactly as in the old bot the record reward is NOT written to the exp
    summary (``$Exp``): the owner hands it out from the pinged log line, so
    adding it there as well would pay it twice.
    """
    bot = scene.bot
    previous, _ = await database.read_record_table(bot)
    if not database.is_new_record(previous, turn_time):
        return
    await bot.log_to_owner(owner_line, ping=True)
    await _guarded(bot, "zapis rekordu", database.update_record_table(bot, nick, turn_time))
    await scene.send(announcement)


async def single_fight(scene: "Scene", hunter: Any, rarity: int, is_player_boss: bool,
                       player_boss: Any) -> fight_types.FightResult:
    """``hunter`` fights the boss alone: type every requested word in time.

    One mistake or one missed word loses the fight (unless the pet protects).
    A technical failure before the kill propagates; after the kill the fight
    is won whatever happens.
    """
    bot = scene.bot
    channel = scene.channel
    rarity = int(rarity)

    await scene.send(MSG_SINGLE_START.format(user_id=hunter.id))
    mods = await modifiers.load_modifiers(bot)
    pet = await _pet_skills(bot, hunter.id)
    await timing.pause(6)
    await database.update_history_table(bot, hunter.name)
    await challenge.countdown(channel)

    # The mythic boss keeps its own words: the "changed commands" of the shrine are short
    # and would make it the easiest fight (owner's decision).
    actions = MYTHIC_ACTIONS if rarity == 4 else modifiers.fight_actions(mods, BOSS_ACTIONS, "Boss")
    boss_hp = _scaled_hp(random_boss_hp(rarity),
                         mods["hp_boost_perc"] - mods["hp_reduced_perc"] - pet["LOWHP_PERC"])
    base_time = MYTHIC_ROUND_TIME if rarity == 4 else 5 - rarity
    round_time = base_time * (100 - mods["time_reduced_perc"] + mods["time_increased_perc"]
                              + pet["SLOW_PERC"]) / 100
    banned = [hunter] if mods["ban_loser"] > 0 else []
    log.info("Single boss fight: %s (%s) vs rarity %s, hp %s, %.2f s per word.",
             hunter, hunter.id, rarity, boss_hp, round_time)

    started = time.monotonic()
    turn = 0
    while True:
        turn += 1
        word, prompt = actions[random.randint(0, len(actions) - 1)]

        if not _roll(pet["REPLACE_PERC"]):
            limit = FIRST_ROUND_TIME if turn == 1 else round_time
            result = await _boss_round(bot, channel, str(turn) + ". " + prompt, players=[hunter], limit=limit)
            timed_out = result.outcome is Outcome.TIMEOUT
            correct = result.is_correct(word)
        else:
            await scene.send("~~" + str(turn) + ". " + prompt + "~~. " + MSG_PET_ATTACKS)
            timed_out = False
            correct = True

        if correct:
            if _roll(pet["CRIT_PERC"]):
                turn += 1
            if turn >= boss_hp:
                break
            continue

        if _roll(pet["DEF_PERC"]):
            await scene.send(MSG_TOO_SLOW_PROTECTED if timed_out else MSG_MISTAKE_PROTECTED)
            continue

        lost = fight_types.FightResult(won=False, losers_to_ban=banned)
        await _decided(bot, lost)
        if timed_out:
            await _say_lost(scene, MSG_TOO_SLOW_LOST)
            line = hunter.name + " nie zdążył wpisać komend i boss uciekł."
        else:
            await _say_lost(scene, MSG_MISTAKE_LOST)
            line = hunter.name + " pomylił się i nie zabił bossa."
        await _report_lost_fight(bot, line, [hunter], rarity, is_player_boss, player_boss)
        log.info("Single boss fight lost by %s (%s) in turn %s.", hunter, "timeout" if timed_out else "mistake", turn)
        return lost

    # -- the boss is dead: from here on nothing may turn the victory into a failure --
    fight_time = timedelta(seconds=time.monotonic() - started)  # (measured before anything is awaited)
    turn_time = fight_time / boss_hp
    await _decided(bot, fight_types.FightResult(won=True))
    log.info("Single boss fight won by %s (%s) in %.2f s.", hunter, hunter.id, fight_time.total_seconds())

    await _guarded(bot, "ogłoszenie zwycięstwa", scene.send(MSG_SINGLE_WON.format(user_id=hunter.id)))
    await _guarded(bot, "czas walki", scene.send(
        f"Zabicie bossa zajęło Ci: {round(fight_time.total_seconds(), 2):.2f} sekundy! "
        f"Jedna tura zajęła Ci średnio {round(turn_time.total_seconds(), 2):.2f} sekundy!"))
    await _guarded(bot, "rekord", _settle_record(
        scene, [hunter], hunter.name, turn_time, MSG_SINGLE_RECORD,
        hunter.name + " otrzymał: 1500 expa za rekord"))
    await _guarded(bot, "punkty rankingowe",
                   database.update_ranking_table(bot, hunter.id, rarity, mods["points_boost"]))
    drop = await _guarded(bot, "łup", rand_loot(scene, rarity, hunter,
                                                mods["drop_boost_perc"] + pet["DROP_PERC"]), default="")
    await bot.log_to_owner(hunter.name + " otrzymał: \n" + drop, ping=True)
    if rarity == 4 and patrons.check_if_patron(hunter):
        await _guarded(bot, "rola pogromcy mitycznego bossa", set_mythic_slayer(scene, hunter.id))
    await _guarded(bot, "licznik zabitych bossów", achievements.add_boss_kill(scene, hunter))
    return fight_types.FightResult(won=True)


# -- duel ----------------------------------------------------------------------------

async def duel_fight(scene: "Scene", participants: Sequence[Any], ban: bool = True) -> tuple[bool, Any]:
    """Two players fight each other, turn by turn, until one runs out of life.

    Returns ``(True, winner)``, or ``(False, None)`` when the duel ended in a
    draw (40 turns without a winner, or repeated connection problems).  With
    ``ban`` the loser gets the "dead hunter" role until the next boss dies.
    """
    bot = scene.bot
    channel = scene.channel
    duelers = list(participants)[:2]
    first, second = duelers
    ids = {first.id, second.id}

    await scene.send(MSG_DUEL_START.format(first=first.id, second=second.id))
    pets = [await _pet_skills(bot, first.id), await _pet_skills(bot, second.id)]
    # Each fighter's life is lowered by the opponent's pet.
    hp: list[float] = [max(1, min(DUEL_HP, DUEL_HP * (100 - pets[1 - index]["LOWHP_PERC"]) / 100))
                       for index in (0, 1)]

    for index, (square, colour) in enumerate((("🟥", 0xFF0000), ("🟦", 0x0000FF))):
        pet = pets[index]
        description = (
            f"❤️ HP: {_num(hp[index])}\n\n"
            f"🔄 Szansa na zastąpienie ataku: {_num(pet['REPLACE_PERC'])} %\n"
            f"💥 Szansa na krytyczne uderzenie: {_num(pet['CRIT_PERC'])} %\n"
            f"🛡️ Szansa na zablokowanie ataku: {_num(pet['DEF_PERC'])} %\n"
            f"⚔️ Zmniejszenie życia przeciwnika: {_num(pet['LOWHP_PERC'])} %\n"
            f"🐢 Spowolnienie przeciwnika: {_num(pet['SLOW_PERC'])} %"
        )
        embed = discord.Embed(title=square + " " + duelers[index].name, description=description, color=colour)
        avatar = _avatar_url(duelers[index])
        if avatar is not None:
            embed.set_thumbnail(url=avatar)
        await scene.send(embed=embed)

    # Who starts: the first one to type the spell.
    spell = random.choice(SPELL_WORDS)
    await timing.pause(6)
    await scene.send(MSG_DUEL_SPELL_COMING)
    await challenge.countdown(channel)
    with _listening(bot, channel) as inbox:
        await scene.send('"Zaklęcie to **' + " ".join(spell.upper()) + '**"')
        tries = 0
        while True:
            message = await _next_message(inbox, lambda m: m.author.id in ids,
                                          timing.timeout(DUEL_SPELL_TIME))
            if message is None:
                await scene.send('"' + MSG_DUEL_RANDOM_START + '"')
                starter = random.choice([0, 1])
                await bot.log_to_owner(LOG_DUEL_SPELL_TIMEOUT)
                break
            if _says(message, spell):
                starter = 0 if message.author.id == first.id else 1
                break
            tries += 1
            if tries > DUEL_SPELL_TRIES:
                await scene.send(MSG_DUEL_RANDOM_START)
                starter = random.choice([0, 1])
                break

    await scene.send("Zaczyna **" + duelers[starter].name + "**.")
    await timing.pause(3)
    await challenge.countdown(channel)
    log.info("Duel: %s vs %s, %s starts.", first, second, duelers[starter])

    turn = 0
    while True:
        turn += 1
        if turn > DUEL_MAX_TURNS:
            await scene.send(MSG_DUEL_DRAW)
            log.info("Duel %s vs %s: draw after %d turns.", first, second, DUEL_MAX_TURNS)
            return False, None

        word, prompt = DUEL_ACTIONS[random.randint(0, len(DUEL_ACTIONS) - 1)]
        me = (starter + turn - 1) % 2
        foe = 1 - me
        fighter = duelers[me]
        pet = pets[me]

        if not _roll(pet["REPLACE_PERC"]):
            limit = DUEL_ROUND_TIME * (100 + pet["SLOW_PERC"]) / 100
            result = await _ask(bot, channel, str(turn) + ". **" + str(fighter.name) + "**: " + prompt,
                                players=duelers, limit=limit)
            if result is None:
                await io.send_patient(channel, MSG_DUEL_INTERRUPTED, give_up_after=120)
                log.warning("Duel %s vs %s interrupted by connection problems.", first, second)
                return False, None
            if result.outcome is Outcome.TIMEOUT:
                # Legacy: "weak attack" of the pet after a timeout was (limit - limit) / 3.
                damage = 0.0
                if not _roll(pet["DEF_PERC"]):
                    await scene.send(MSG_DUEL_TOO_SLOW)
                else:
                    await scene.send(MSG_DUEL_TOO_SLOW_WEAK)
            elif result.author.id != fighter.id:
                if not _roll(pet["DEF_PERC"]):
                    damage = limit
                    await scene.send(MSG_DUEL_NOT_YOUR_TURN)
                else:
                    damage = limit / 2
                    await scene.send(MSG_DUEL_NOT_YOUR_TURN_PROTECTED)
            elif result.is_correct(word):
                damage = limit - result.elapsed
                if _roll(pet["CRIT_PERC"]):
                    damage *= 2
            elif not _roll(pet["DEF_PERC"]):
                damage = 0.0
                await scene.send(MSG_DUEL_MISTAKE)
            else:
                damage = (limit - result.elapsed) / 3
                await scene.send(MSG_DUEL_MISTAKE_WEAK)
        else:
            limit = DUEL_ROUND_TIME
            sent_at = time.monotonic()
            await scene.send("~~" + str(turn) + ". " + str(fighter.name) + ":" + prompt + "~~ " + MSG_PET_ATTACKS)
            damage = limit - (time.monotonic() - sent_at)
            if _roll(pet["CRIT_PERC"]):
                damage *= 2

        damage = max(damage, 0.0)
        reduction = (100 - int(pets[foe]["DEF_PERC"])) / 100
        dealt = round(damage * 100 * reduction)
        reduced = round(damage * 100 - dealt)
        hp[foe] -= dealt

        if hp[foe] > 0:
            bar = general.victory_bar(hp[0], hp[1])
            await scene.send(f"{fighter.name} zadał {dealt} obrażeń (zredukowane o {reduced}).\n\n"
                             f"{first} 🟥 HP: {_num(hp[0])}\n{second} 🟦 HP: {_num(hp[1])}\n\n{bar}")
            continue

        loser = duelers[foe]
        log.info("Duel: %s beat %s in turn %d.", fighter, loser, turn)
        await _guarded(bot, "ogłoszenie wyniku pojedynku",
                       scene.send("**" + str(fighter) + "** wygrał walkę z **" + str(loser) + "**."))
        await _guarded(bot, "punkty zwycięzcy pojedynku",
                       database.update_ranking_table(bot, fighter.id, DUEL_RARITY, 0))
        await _guarded(bot, "łup zwycięzcy pojedynku", rand_loot(scene, DUEL_RARITY, fighter, pet["DROP_PERC"]))
        await _guarded(bot, "punkty przegranego pojedynku",
                       database.update_ranking_table(bot, loser.id, DUEL_RARITY, DUEL_LOSER_POINTS))
        if ban:
            await set_dead_players(bot, loser)
        return True, fighter


# -- group fight ---------------------------------------------------------------------

class _GroupLobby:
    """The lobby of a legendary boss: one panel with the party, the buttons and the time left.

    The rules and the timing are those of the typed protocol (and typing keeps
    working): players join with ``$zaatakuj`` / "Dołącz"; a silent step of
    :data:`GROUP_LOBBY_STEP` s counts towards the boss leaving (a join starts
    the step anew); when four are there, each has :data:`GROUP_CONFIRM_TIME` s
    (anew after every confirmation) to confirm with ``$tak`` / "Gotowy";
    those who do not are dropped and the gathering goes on.

    The panel is edited in place - at most once per :data:`PANEL_EDIT_INTERVAL`
    s, clicks in between are gathered into one edit - and posted anew only to
    ping somebody (the ready check, the last call to the hunters, a regroup).
    A panel deleted meanwhile is posted again at the next change (with the same
    text, but without pinging anybody again).
    """

    GATHER = "gather"
    CONFIRM = "confirm"
    DONE = "done"

    def __init__(self, scene: "Scene") -> None:
        self.scene = scene
        self.bot = scene.bot
        self.channel = scene.channel
        self.players: list[Any] = [scene.author]
        self.confirmed: list[Any] = []
        self.stage = self.GATHER
        self.voice = VOICE_START
        #: Wall-clock time (``time.time()``) shown with ``ui.when``.
        self.deadline = time.time()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.view: ui.ClickView | None = None
        self.content: str | None = None
        #: (button key, member id) of clicks queued but not processed yet.
        self._queued: set[tuple[str, int]] = set()
        self._dirty = asyncio.Event()
        self._painter: asyncio.Future | None = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    # -- the flow -------------------------------------------------------------------

    async def run(self) -> list[Any] | None:
        bot = self.bot
        step = timing.timeout(GROUP_LOBBY_STEP)
        confirm_time = timing.timeout(GROUP_CONFIRM_TIME)
        waited = 0
        # The door opens before the first Discord round trip: a click on the boss
        # picture in the meantime already joins the party.
        with _listening(bot, self.channel) as inbox, _lobby_door(bot, self._door):
            self.inbox = inbox
            try:
                await io.safe_react(self.scene.message, "⚔️")
                self._set_deadline(step * (GROUP_LOBBY_STEPS - 1 - waited))
                await self._post(None)
                while True:
                    self._set_deadline(step * (GROUP_LOBBY_STEPS - 1 - waited))
                    joined = await _next_message(inbox, self._joins, step)
                    if joined is None:
                        waited += 1
                        if waited == 1:
                            self.voice = VOICE_COWARDS
                            self._touch()
                        elif waited == GROUP_LOBBY_STEPS - 2:
                            self.voice = VOICE_LAST_CHANCE
                            await self._post(MSG_GROUP_LAST_CHANCE.format(role_id=bot.ids.hunters_role))
                        elif waited == GROUP_LOBBY_STEPS - 1:
                            self.voice = MSG_GROUP_GAVE_UP
                            await self._close(MSG_GROUP_NOT_GATHERED)
                            await self.scene.send(MSG_GROUP_GAVE_UP)
                            log.info("Group boss fight: the party did not gather.")
                            return None
                        continue

                    self.players.append(_who(joined))
                    if len(self.players) < GROUP_SIZE:
                        count = len(self.players)
                        self.voice = VOICE_ALONE if count == 1 else VOICE_MORE.format(count=count)
                        self._touch()
                        continue

                    # Four players: everybody has to confirm.
                    self.stage = self.CONFIRM
                    self.confirmed = []
                    self.voice = VOICE_READY
                    self._set_deadline(confirm_time)
                    await self._post(MSG_GROUP_READY_PING.format(mentions=_mentions(self.players)))
                    while len(self.confirmed) < GROUP_SIZE:
                        self._set_deadline(confirm_time)
                        confirmation = await _next_message(inbox, self._confirms, confirm_time)
                        if confirmation is None:
                            break
                        if not isinstance(confirmation, _Knock):
                            await io.safe_react(confirmation, "⚔️")
                        self.confirmed.append(_who(confirmation))
                        self._touch()
                    if len(self.confirmed) == GROUP_SIZE:
                        log.info("Group boss fight: party %s.", ", ".join(str(p) for p in self.players))
                        self.voice = VOICE_SET
                        await self._close(MSG_GROUP_SET.format(mentions=_mentions(self.players)))
                        return self.players

                    # Somebody did not confirm: keep those who did and go on gathering.
                    self.players = list(self.confirmed)
                    self.confirmed = []
                    waited = 0
                    self.stage = self.GATHER
                    self.voice = VOICE_REGROUP
                    self._set_deadline(step * (GROUP_LOBBY_STEPS - 1 - waited))
                    await self._post(MSG_GROUP_REGROUP_PING.format(mentions=_mentions(self.players)))
            finally:
                await self._stop_painter()
                if self.view is not None:
                    await self.view.close()  # an error: at least no buttons stay behind

    def _set_deadline(self, seconds: float) -> None:
        self.deadline = time.time() + max(float(seconds), 0.0)

    # -- typed messages and clicks ----------------------------------------------------

    def _taken(self, item: Any) -> None:
        if isinstance(item, _Knock):
            self._queued.discard((item.key, item.member.id))

    def _joins(self, item: Any) -> bool:
        self._taken(item)
        return (_is(item, "$zaatakuj", "join")
                and all(_who(item).id != player.id for player in self.players))

    def _confirms(self, item: Any) -> bool:
        self._taken(item)
        member_id = _who(item).id
        return (_is(item, "$tak", "ready")
                and any(member_id == player.id for player in self.players)
                and all(member_id != player.id for player in self.confirmed))

    async def _allow(self, interaction: Any, key: str) -> str | None:
        """Who may click what - answered with ui.reply; an accepted click is queued like a typed command."""
        member_id = interaction.user.id
        in_party = any(member_id == player.id for player in self.players)
        if key == "join":
            if self.stage != self.GATHER:
                return MSG_PARTY_FULL
            if in_party or (key, member_id) in self._queued:
                return MSG_ALREADY_IN_PARTY
        elif key == "ready":
            if self.stage != self.CONFIRM or not in_party:
                return MSG_NOT_IN_PARTY
            if any(member_id == player.id for player in self.confirmed) or (key, member_id) in self._queued:
                return MSG_ALREADY_READY
        else:
            return ui.MSG_STALE
        self._queued.add((key, member_id))
        return await _knock(self.inbox, interaction, key)

    async def _door(self, interaction: Any) -> None:
        """A click on the boss picture's "Zaatakuj" during the lobby: like "Dołącz"."""
        await _answer_door(self._allow, interaction, "join")

    # -- the panel ------------------------------------------------------------------------

    def _choices(self) -> list[ui.Choice]:
        if self.stage == self.CONFIRM:
            return [ui.Choice("ready", READY_LABEL, emoji="✅", style=discord.ButtonStyle.success)]
        return [ui.Choice("join", JOIN_LABEL, emoji=boss_cycle.ATTACK_EMOJI,
                          style=discord.ButtonStyle.primary)]

    def _embed(self) -> discord.Embed:
        lines = [self.voice]
        deadline = ui.when(max(self.deadline - time.time(), 0.0))
        if self.stage == self.GATHER:
            lines.append(GROUP_HOW_TO_JOIN.format(size=GROUP_SIZE, deadline=deadline))
        elif self.stage == self.CONFIRM:
            lines.append(GROUP_HOW_TO_CONFIRM.format(deadline=deadline))
        confirmed = {player.id for player in self.confirmed}
        roster = []
        for player in self.players:
            if self.stage == self.GATHER:
                mark = "⚔️"
            elif self.stage == self.CONFIRM:
                mark = "✅" if player.id in confirmed else "⏳"
            else:
                mark = "✅"
            roster.append(f"{mark} <@{player.id}>")
        embed = discord.Embed(title=GROUP_PANEL_TITLE, description="\n\n".join(lines),
                              color=_BOSS_COLOUR_FALLBACK)
        embed.add_field(name=GROUP_ROSTER.format(count=len(self.players), size=GROUP_SIZE),
                        value="\n".join(roster) or GROUP_ROSTER_EMPTY, inline=False)
        return embed

    async def _post(self, content: str | None, *, ping: bool = True) -> None:
        """Post the panel anew (``content`` pings whoever it mentions); the old one goes away.

        ``ping=False``: the same text, but nobody is notified (a deleted panel
        that is only put back - its ping was already heard).
        Raises like :func:`io.send` - the encounter treats it as a technical problem.
        """
        extra: dict[str, Any] = {} if ping else {"allowed_mentions": discord.AllowedMentions.none()}
        async with self._lock:
            old = self.view
            view = ui.ClickView(self._choices(), allow=self._allow)
            await view.send(self.channel, content, embed=self._embed(), **extra)
            self.view = view
            self.content = content
            self._last_edit = time.monotonic()
        if old is not None:
            await old.close()
            await io.safe_delete(old.message)

    def _touch(self) -> None:
        """The panel shows something old: edit it soon (see :meth:`_paint`)."""
        self._dirty.set()
        if self._painter is None or self._painter.done():
            self._painter = asyncio.ensure_future(self._paint())

    async def _paint(self) -> None:
        """Edit the panel whenever it is out of date, at most once per ``PANEL_EDIT_INTERVAL``."""
        while True:
            await self._dirty.wait()
            delay = self._last_edit + PANEL_EDIT_INTERVAL - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._dirty.clear()
            await self._refresh()

    async def _refresh(self) -> None:
        """One edit of the panel.  Never raises; a deleted panel is posted again."""
        async with self._lock:
            view = self.view
            if view is None or view.closed or view.message is None:
                return
            try:
                await view.message.edit(embed=self._embed(), view=view)
                self._last_edit = time.monotonic()
                return
            except asyncio.CancelledError:
                raise
            except discord.NotFound:
                pass
            except Exception as exc:  # noqa: BLE001 - cosmetic: the next change edits it again
                log.info("Could not update the legendary lobby panel (%s: %s).", type(exc).__name__, exc)
                return
        log.info("The legendary lobby panel was deleted; posting it again (without pinging anybody).")
        try:
            await self._post(self.content, ping=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - typing $zaatakuj / $tak still works
            log.warning("Could not post the legendary lobby panel again (%s: %s).", type(exc).__name__, exc)

    async def _stop_painter(self) -> None:
        painter, self._painter = self._painter, None
        if painter is not None and not painter.done():
            painter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await painter

    async def _close(self, content: str) -> None:
        """The lobby is over: the panel shows the outcome, its buttons go away."""
        await self._stop_painter()
        self.stage = self.DONE
        async with self._lock:
            if self.view is not None:
                await self.view.close(content=content, embed=self._embed())


async def group_init(scene: "Scene", rarity: int) -> list[Any] | None:
    """Gather a party of four for a legendary boss (see :class:`_GroupLobby`).

    Players join with ``$zaatakuj`` or the "Dołącz" button; when four are
    there, every one of them has 60 s to confirm with ``$tak`` or "Gotowy"
    (those who do not are dropped and the gathering goes on).  Returns the
    four players in the order they joined, or None when the party did not
    gather in time (the boss leaves).
    """
    return await _GroupLobby(scene).run()


async def group_fight(scene: "Scene", players: Sequence[Any], is_player_boss: bool,
                      player_boss: Any) -> fight_types.FightResult:
    """The party fights a legendary boss: each word is addressed to one random member.

    Any message of a party member ends the round; a wrong word, a word typed
    by somebody else than the addressed player, or silence is a mistake.  The
    party may make one mistake; the second one loses the fight.
    """
    bot = scene.bot
    channel = scene.channel
    rarity = GROUP_RARITY
    players = list(players)
    party = "Party " + str(players[0].name)
    mentions = _mentions(players)

    mods = await modifiers.load_modifiers(bot)
    pets = {player.id: await _pet_skills(bot, player.id) for player in players}

    await scene.send(MSG_GROUP_START.format(mentions=mentions))
    await timing.pause(17)
    await database.update_history_table(bot, party)

    boss_hp = _scaled_hp(random_boss_hp(rarity),
                         mods["hp_boost_perc"] - mods["hp_reduced_perc"]
                         - sum(pets[player.id]["LOWHP_PERC"] for player in players))
    chances = GROUP_SPARE_CHANCES
    banned = list(players) if mods["ban_loser"] > 0 else []
    actions = modifiers.fight_actions(mods, BOSS_ACTIONS, "Boss")
    log.info("Group boss fight: %s, hp %s.", party, boss_hp)
    await challenge.countdown(channel)

    started = time.monotonic()
    turn = 0
    while True:
        turn += 1
        word, prompt = actions[random.randint(0, len(actions) - 1)]
        hunter = random.choice(players)
        pet = pets[hunter.id]

        if not _roll(pet["REPLACE_PERC"]):
            if turn == 1:
                limit = FIRST_ROUND_TIME
            else:
                limit = GROUP_ROUND_TIME * (100 - mods["time_reduced_perc"] + mods["time_increased_perc"]
                                            + pet["SLOW_PERC"]) / 100
            result = await _boss_round(bot, channel, str(turn) + ". **" + str(hunter) + "**: " + prompt,
                                       players=players, limit=limit)
            timed_out = result.outcome is Outcome.TIMEOUT
            correct = result.is_correct(word, by=hunter)
        else:
            await scene.send("~~" + str(turn) + ". " + str(hunter) + ":" + prompt + "~~ " + MSG_PET_ATTACKS)
            timed_out = False
            correct = True

        if correct:
            if _roll(pet["CRIT_PERC"]):
                turn += 1
            if turn >= boss_hp:
                break
            continue

        if _roll(pet["DEF_PERC"]):
            await scene.send(MSG_TOO_SLOW_PROTECTED if timed_out else MSG_MISTAKE_PROTECTED)
            continue
        if chances > 0:
            chances -= 1  # the party's spare chance is used up silently
            continue

        lost = fight_types.FightResult(won=False, losers_to_ban=banned)
        await _decided(bot, lost)
        await _say_lost(scene, MSG_TOO_SLOW_LOST if timed_out else MSG_MISTAKE_LOST)
        await _report_lost_fight(bot, party + " pomyliło się i nie zabili bossa.", players, rarity,
                                 is_player_boss, player_boss)
        log.info("Group boss fight lost by %s in turn %s.", party, turn)
        return lost

    # -- the boss is dead: from here on nothing may turn the victory into a failure --
    fight_time = timedelta(seconds=time.monotonic() - started)  # (measured before anything is awaited)
    turn_time = fight_time / boss_hp
    await _decided(bot, fight_types.FightResult(won=True))
    log.info("Group boss fight won by %s in %.2f s.", party, fight_time.total_seconds())

    await _guarded(bot, "ogłoszenie zwycięstwa", scene.send(MSG_GROUP_WON.format(mentions=mentions)))
    await _guarded(bot, "czas walki", scene.send(
        f"Zabicie bossa zajęło Wam: {round(fight_time.total_seconds(), 2):.2f} sekundy! "
        f"Jedna tura zajęła Wam średnio {round(turn_time.total_seconds(), 2):.2f} sekundy!"))
    await _guarded(bot, "rekord", _settle_record(
        scene, players, party, turn_time, MSG_GROUP_RECORD, mentions + " otrzymali: 1500 expa za rekord"))
    for hunter in players:
        await _guarded(bot, f"punkty rankingowe gracza {hunter}",
                       database.update_ranking_table(bot, hunter.id, rarity, mods["points_boost"]))
        drop = await _guarded(bot, f"łup gracza {hunter}", rand_loot(
            scene, rarity, hunter, mods["drop_boost_perc"] + pets[hunter.id]["DROP_PERC"]), default="")
        await bot.log_to_owner(str(hunter) + " otrzymał: " + drop, ping=True)
    return fight_types.FightResult(won=True)


# -- roles ---------------------------------------------------------------------------

def is_boss_slayer(bot: "AlterBot", member: Any) -> bool:
    """True when ``member`` holds the boss slayer ("Rzeźnik Potworów") role."""
    return any(role.id == bot.ids.boss_slayer_role for role in getattr(member, "roles", None) or [])


async def set_boss_slayer(bot: "AlterBot", user_id: int | str) -> bool:
    """Hand the boss slayer role over to the season winner.

    The previous holders lose it.  When the winner left the server nobody gets
    the role and the owner is told.  Returns True when the winner has the role.
    """
    role = bot.role(bot.ids.boss_slayer_role)
    if role is None:
        log.error("The boss slayer role does not exist on the server.")
        await bot.log_to_owner(f"Rola Rzeźnika Potworów nie istnieje na serwerze - "
                               f"nie przyznano jej graczowi <@{user_id}>.", ping=True)
        return False
    winner = await bot.member(user_id)
    for member in list(role.members):
        if winner is None or member.id != winner.id:
            await io.remove_role(member, role, reason="AlterMMO RPG: koniec sezonu")
    if winner is None:
        log.warning("Season winner %s left the server; nobody gets the boss slayer role.", user_id)
        await bot.log_to_owner(f"Zwycięzca sezonu (<@{user_id}>) opuścił serwer - "
                               "nikt nie otrzymał roli Rzeźnika Potworów.", ping=True)
        return False
    if not await io.add_role(winner, role, reason="AlterMMO RPG: zwycięzca sezonu"):
        await bot.log_to_owner(f"Nie udało się nadać roli Rzeźnika Potworów graczowi <@{winner.id}>.", ping=True)
        return False
    log.info("Boss slayer role granted to %s (%s).", winner, winner.id)
    return True


async def reset_dead_hunters(bot: "AlterBot") -> None:
    """Bring the fallen back: take the "dead hunter" role from everybody who has it."""
    role = bot.role(bot.ids.dead_hunter_role)
    if role is None:
        log.warning("The dead hunter role does not exist on the server; nothing to reset.")
        return
    stuck = []
    for member in list(role.members):
        if not await io.remove_role(member, role, reason="AlterMMO RPG: boss pokonany"):
            stuck.append(member)
    if stuck:
        await bot.log_to_owner("Nie udało się zdjąć roli poległego łowcy: "
                               + ", ".join(f"<@{member.id}>" for member in stuck))
    log.info("Dead hunter role removed from everybody.")


async def _set_dead(bot: "AlterBot", member_or_id: Any, killer: str) -> bool:
    """Give the "dead hunter" role and post the notice; never raises."""
    user_id = _id_of(member_or_id)
    if user_id is None:
        return False
    member = await bot.member(user_id)
    if member is None:
        log.info("Dead hunter role not granted: %s left the server.", user_id)
        return False
    role = bot.role(bot.ids.dead_hunter_role)
    if not await io.add_role(member, role, reason="AlterMMO RPG: poległy łowca"):
        await bot.log_to_owner(f"Nie udało się nadać roli poległego łowcy graczowi <@{user_id}>.")
        return False
    log.info("Dead hunter role granted to %s (%s).", member, user_id)
    try:
        notice = bot.get_channel(bot.ids.notice_channel)
        if notice is None:
            log.warning("Notice channel %s is not available; no death notice for %s.",
                        bot.ids.notice_channel, user_id)
        else:
            await io.send(notice, MSG_DEAD.format(channel_id=bot.ids.boss_channel, killer=killer,
                                                  user_id=user_id))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the notice is cosmetic, the role is what matters
        log.warning("Could not post the death notice for %s: %s: %s", user_id, type(exc).__name__, exc)
    return True


async def set_dead_hunters(bot: "AlterBot", member_or_id: Any) -> bool:
    """The player lost to a boss while the "ban the loser" modifier was on."""
    return await _set_dead(bot, member_or_id, "potwora")


async def set_dead_players(bot: "AlterBot", member_or_id: Any) -> bool:
    """The player lost a duel."""
    return await _set_dead(bot, member_or_id, "drugiego gracza")


async def set_dead_assassin(bot: "AlterBot", member_or_id: Any) -> bool:
    """The player was killed by an assassin."""
    return await _set_dead(bot, member_or_id, "zabójcy")


async def set_mythic_slayer(scene: "Scene", user_id: int | str) -> None:
    """Give the mythic slayer role and tell the hunters how rare the deed is."""
    bot = scene.bot
    role = bot.role(bot.ids.mythic_slayer_role)
    earlier = len(role.members) if role is not None else 0
    member = await bot.member(user_id)
    if role is None or member is None or not await io.add_role(
            member, role, reason="AlterMMO RPG: zabicie mitycznego bossa"):
        await bot.log_to_owner(f"Nie udało się nadać roli pogromcy mitycznego bossa graczowi <@{user_id}>.")
    else:
        log.info("Mythic slayer role granted to %s (%s).", member, user_id)
    announcement = (f"<@&{bot.ids.hunters_role}>! Łowca <@{user_id}> zabił mitycznego bossa! "
                    "<:POGGERS:936907543849078844> ")
    if earlier > 0:
        announcement += f"Do tej pory udało się to tylko {earlier} łowcom!"
    else:
        announcement += "Jest to pierwszy łowca, który tego dokonał!"
    await scene.send(announcement)


# -- boss slayer perks ---------------------------------------------------------------

async def flex_gif(scene: "Scene") -> bool:
    """Post a random "flex" gif that fits the server's upload limit; False when none could be sent."""
    folder = ASSETS_DIR / "flexgifs"
    limit = getattr(scene.guild, "filesize_limit", None) or DEFAULT_UPLOAD_LIMIT
    try:
        candidates = sorted(path for path in folder.iterdir()
                            if path.is_file() and path.stat().st_size <= limit)
    except OSError as exc:
        log.warning("Cannot list the flex gifs in %s: %s", folder, exc)
        candidates = []
    if not candidates:
        log.warning("No flex gif below the upload limit of %s bytes in %s.", limit, folder)
        return False
    gif = random.choice(candidates)
    try:
        await scene.send(file_path=gif)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the gif is decoration
        log.warning("Could not post the flex gif %s: %s: %s", gif.name, type(exc).__name__, exc)
        return False
    return True


async def change_color(scene: "Scene", hex_color: str | None) -> bool:
    """Change the colour of the boss slayer role (``ff0000``, ``#ff0000``); True when changed."""
    bot = scene.bot
    match = _HEX_COLOUR.match(str(hex_color or "").strip())
    if match is None:
        await scene.send(MSG_BAD_COLOUR)
        return False
    role = bot.role(bot.ids.boss_slayer_role)
    if role is None:
        log.error("The boss slayer role does not exist on the server.")
        await scene.send(MSG_COLOUR_FAILED)
        return False
    colour = discord.Colour(int(match.group(1), 16))
    try:
        await io.retry(lambda: role.edit(colour=colour, reason="Zmiana przez użytkownika."),
                       what="role colour change")
    except discord.HTTPException as exc:
        log.warning("Could not change the boss slayer colour: %s: %s", type(exc).__name__, exc)
        await scene.send(MSG_COLOUR_FAILED)
        return False
    log.info("Boss slayer colour changed to #%06x by %s.", colour.value, scene.author)
    return True


async def change_icon(scene: "Scene") -> bool:
    """Set the boss slayer role icon from the .png attached to the command; True when changed."""
    bot = scene.bot
    attachments = getattr(scene.message, "attachments", None) or []
    if not attachments:
        await scene.send(MSG_ICON_MISSING)
        return False
    attachment = attachments[0]
    content_type = str(getattr(attachment, "content_type", None) or "").split(";")[0].strip().lower()
    if content_type != "image/png":
        await scene.send(MSG_ICON_BAD_FORMAT.format(user_id=scene.author.id))
        return False
    role = bot.role(bot.ids.boss_slayer_role)
    if role is None:
        log.error("The boss slayer role does not exist on the server.")
        await scene.send(MSG_ICONS_UNAVAILABLE)
        return False
    if int(getattr(attachment, "size", 0) or 0) > ROLE_ICON_LIMIT:
        await scene.send(MSG_ICON_TOO_BIG)
        return False
    try:
        image = await io.retry(lambda: attachment.read(), what="icon download")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - Discord said no, or the network gave up after the retries
        log.warning("Could not download the role icon: %s: %s", type(exc).__name__, exc)
        await scene.send(MSG_ICON_DOWNLOAD_FAILED)
        return False
    if len(image) > ROLE_ICON_LIMIT:
        await scene.send(MSG_ICON_TOO_BIG)
        return False
    try:
        await io.retry(lambda: role.edit(display_icon=image, reason="Zmiana przez użytkownika."),
                       what="role icon change")
    except asyncio.CancelledError:
        raise
    except ValueError:  # discord.py: the bytes are not a supported image
        await scene.send(MSG_ICON_BAD_FORMAT.format(user_id=scene.author.id))
        return False
    except discord.HTTPException as exc:
        log.warning("Could not change the boss slayer icon: %s: %s", type(exc).__name__, exc)
        # Too few boosts for role icons (Discord's code 50101, or the server simply has no
        # ROLE_ICONS feature) comes first: it is not the bot's and not the picture's fault.
        features = getattr(getattr(role, "guild", None), "features", None)
        if getattr(exc, "code", 0) == _NEEDS_BOOSTS or (features is not None and "ROLE_ICONS" not in features):
            await scene.send(MSG_ICONS_UNAVAILABLE)
        elif isinstance(exc, discord.Forbidden):  # no "Manage Roles", or the bot's role is below this one
            await scene.send(MSG_ICON_FORBIDDEN)
            await bot.log_to_owner("Nie mogę zmienić ikony roli Rzeźnika Potworów ($ikona): brak uprawnienia "
                                   "\"Zarządzanie rolami\" albo rola bota jest poniżej tej roli.", ping=True)
        else:
            await scene.send(MSG_ICON_REJECTED)
        return False
    except Exception as exc:  # noqa: BLE001 - the network gave up after the retries
        log.warning("Could not change the boss slayer icon: %s: %s", type(exc).__name__, exc)
        await scene.send(MSG_ICON_NOT_SENT)
        return False
    await scene.send(MSG_ICON_CHANGED)
    log.info("Boss slayer icon changed by %s.", scene.author)
    return True


# -- after the fight -----------------------------------------------------------------

async def reset_end(bot: "AlterBot") -> None:
    """Clean up after a boss: zero the modifiers, clear the schedule, revive the dead.

    All three steps always run; the first failure is raised afterwards so the
    boss cycle can report it.
    """
    steps: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
        ("modifiers", lambda: modifiers.init_modifiers(bot)),
        ("boss table", lambda: database.update_boss_table(bot, 0, 0, False)),
        ("dead hunters", lambda: reset_dead_hunters(bot)),
    )
    first_error: Exception | None = None
    for name, step in steps:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Reset after the boss: step %r failed.", name)
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error
    log.info("Reset after boss: modifiers, database and dead hunters reset.")
