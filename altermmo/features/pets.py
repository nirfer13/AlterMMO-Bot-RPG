"""Pets (companions): eggs, hatching, talents, rerolls, the stable, boss detection.

Port of the legacy ``functions/functions_pets.py``.  Tables (unchanged, shared
with the legacy bot - neither has a primary key):

``PETOWNER``  one row per player: the active pet, four stable slots, the pet
              items (reroll scrolls and their shards, rebirth stones, mirrors)
              and the skill gem (owned by features/skills.py);
``PETS``      one row per pet: name, level 0 (egg) .. 3, talent (``PET_SKILLS``),
              looks and the seven stats used by the fights - and, new in v2,
              the cosmetics ``SEAL`` (0-3), ``HOLO`` (full Holo), ``FRAME``
              (the frame result, '' = the base of the class) and
              ``HOLO_PARTIAL`` (partial Holo) - see below.

Differences from the legacy code (the game rules themselves are unchanged):

* every statement is parametrised (a pet named ``O'Neil`` used to break the SQL);
* a database outage raises ``DatabaseUnavailable`` instead of being read as
  "this player has no row" (which created duplicate rows) or returning False;
* counters are changed atomically in SQL and every multi-step change runs in
  one transaction that re-reads the current state: scrolls / stones / mirrors
  can no longer be spent from a value read 15 seconds earlier;
* a player has one pending ``$potwierdzam`` question at a time (one answer
  used to confirm every open question at once, e.g. a reroll *and* a discard),
  and the flag is always released (``$porzucam`` without a pet used to block
  the command until the next restart);
* the bot listens for ``$potwierdzam`` before its question is sent and only on
  the channel where it asked;
* NULL counters, a missing ``PETS`` row or a missing picture are handled
  instead of crashing the command;
* the ``create_*`` helpers never drop anything.

Buttons and menus (the rules behind them are the commands' own):

* every "are you sure?" question has **Potwierdzam / Anuluj** buttons that only
  the asking player can use; typing ``$potwierdzam`` keeps working;
* ``$mocneodrodzenie`` is a multi-choice list (up to three stats to leave out)
  with **Przelosuj / Anuluj** buttons instead of eight reactions;
* the pet shown by ``$towarzysz`` carries a panel (:class:`PetPanel`) for its
  owner: rename in a pop-up window, the stable, and the pet commands.  Showing
  works on every channel; the gameplay buttons, like the commands, only on the
  boss channel, not while the channel is busy and not for dead hunters.  The
  panel acts only on the pet it shows: once another pet is active its buttons
  are refused (typed commands still mean "the active pet");
* the stable (``$stajnia``) has a list that makes a stabled pet the active one,
  exactly as ``$wyciagnijtowarzysza <slot>`` does - but only the pet the entry
  names (a stale list is refused and redrawn).

Cosmetics (new in v2, they give no stats):

* a supporter (patron, crafter, Twitch subscriber, YouTube supporter - whatever
  :func:`patrons.check_if_patron` accepts) gets, for every egg drawn for him, ONE
  roll for a seal (:data:`SEAL_ROLL_BELOW`), ONE independent roll for the foil
  (:data:`FOIL_ROLL_BELOW`: none / partial Holo / full Holo) whatever the class
  of the pet, and - for a Shiny, Ultra Shiny or mythic egg - ONE roll in the
  frame pool of its class (``petcards.POOLS``, :func:`_frame_for`; owner's
  package of 2026-11-21).  Everybody else's egg has no seal, no foil and the
  base frame of its class.  The result is stored with the specimen
  (``PETS.SEAL`` / ``HOLO`` / ``HOLO_PARTIAL`` / ``FRAME``) and nothing but the
  owner's ``$DajPieczec`` / ``$DajRamke`` ever changes it: not growing, the
  mirror (the class never changes), a new name, the stable, a new owner or the
  end of the support;
* what is stored is SHOWN only while the pet's owner is a supporter (owner's
  decision of 2026-11-20): when the support ends the seal, the foil and a
  rarer frame disappear from the card and from the statistics (the card shows
  the base frame of the class, ``petcards.base_look``), and they are back the
  moment the owner supports again (:func:`_is_supporter`, asked every time
  the pet is shown);
* the columns are added to the old bot's table at start-up (:data:`NEW_COLUMNS`,
  run by ``schema.add_new_columns``; the columns of one table are added all or
  none).  The game copes with every state that can result, see
  :func:`cosmetics_available` / :func:`frames_available`: without ``SEAL`` /
  ``HOLO`` nothing is rolled at all; with them but without ``FRAME`` /
  ``HOLO_PARTIAL`` (the v2 table of 2026-11-20 whose new columns could not be
  added) seals and full Holo work exactly as before, only the frame is not
  rolled and a partial Holo result is not kept.  Every reader takes a missing
  column as "nothing" (:func:`cosmetics_of`, :func:`ornaments_of`,
  ``petcards.look_of``);
* a pet is shown as a card composed by :mod:`altermmo.features.petcards`
  (illustration, foil, frame, seal) and names a rarer frame, its seal and its
  foil in words (:func:`_pet_card`) - from the egg on: an egg has its frame,
  seal and foil like a hatched pet.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import random
import time
import types
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, NamedTuple, Sequence

import asyncpg
import discord

from ..core import io, timing, ui, waiting
from ..settings import ASSETS_DIR
from . import achievements, bossdetect, modifiers, patrons, petcards

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)


class PetType(str, Enum):
    """Class with all pets types."""

    BEAR = 'Bear'
    BOAR = 'Boar'
    CAT = 'Cat'
    RABBIT = 'Rabbit'
    SHEEP = 'Sheep'
    DRAGON = 'Dragon'
    PHOENIX = 'Phoenix'
    UNICORN = 'Unicorn'
    SNAKE = 'Snake'
    ANIME_GIRL = 'AnimeGirl'
    ELEMENTAL = 'Elemental'
    DOG = 'Dog'
    GUINEA = 'Guinea'
    SKELETON = 'Skeleton'
    MONKEY = 'Monkey'
    EAGLE = 'Eagle'
    GHOST = 'Ghost'

    VOID = 'Void'


# -- schema (the production tables, plus the two cosmetic columns of PETS) -------------

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS PETOWNER (
           PLAYER_ID NUMERIC,
           PET_ID NUMERIC,
           PET_OWNED BOOLEAN,
           REROLL_SCROLL NUMERIC,
           REROLL_SCROLL_SHARD NUMERIC,
           REBIRTH_STONES NUMERIC,
           MIRRORS NUMERIC,
           PET_ID_ALT1 NUMERIC,
           PET_ID_ALT2 NUMERIC,
           SKILL_GEM NUMERIC,
           PET_ID_ALT3 NUMERIC DEFAULT 0,
           PET_ID_ALT4 NUMERIC DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS PETS (
           PET_ID NUMERIC,
           PET_NAME VARCHAR(255),
           PET_LVL NUMERIC,
           PET_SKILLS NUMERIC,
           QUALITY VARCHAR(255),
           SHINY BOOLEAN,
           TYPE VARCHAR(255),
           VARIANT VARCHAR(255),
           CRIT_PERC NUMERIC,
           REPLACE_PERC NUMERIC,
           DEF_PERC NUMERIC,
           DROP_PERC NUMERIC,
           LOWHP_PERC NUMERIC,
           SLOW_PERC NUMERIC,
           INIT_PERC NUMERIC,
           DETECTION BOOLEAN,
           ULTRA_SHINY BOOLEAN,
           MYTHIC BOOLEAN NOT NULL DEFAULT FALSE,
           SEAL NUMERIC NOT NULL DEFAULT 0,
           HOLO BOOLEAN NOT NULL DEFAULT FALSE,
           FRAME VARCHAR(16) NOT NULL DEFAULT '',
           HOLO_PARTIAL BOOLEAN NOT NULL DEFAULT FALSE
       )""",
)

#: Columns the old bot's ``PETS`` lacks, as ``(table, column, definition)``; added at every
#: start-up by ``schema.add_new_columns`` (only when missing).  The defaults make every pet
#: that existed before - and every row the old bot still inserts with its own column
#: lists - a pet without a seal, without a foil and with the base frame of its class.
#: SEAL / HOLO came on 2026-11-20, FRAME / HOLO_PARTIAL with the frames of 2026-11-21.
NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("pets", "seal", "NUMERIC NOT NULL DEFAULT 0"),
    ("pets", "holo", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("pets", "frame", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("pets", "holo_partial", "BOOLEAN NOT NULL DEFAULT FALSE"),
)
#: The first cosmetic columns of ``PETS`` - the seal and full Holo (both there, or cosmetics
#: are off: :func:`cosmetics_available`).
COSMETIC_COLUMNS: tuple[str, ...] = ("seal", "holo")
#: The columns of the frames and the partial Holo (:func:`frames_available`: these AND the
#: COSMETIC_COLUMNS are there).  Without them the old cosmetics keep working.
FRAME_COLUMNS: tuple[str, ...] = ("frame", "holo_partial")

# -- game rules -----------------------------------------------------------------------

#: Eggs a player who is not a supporter can find (legacy order).
STANDARD_EGGS: tuple[PetType, ...] = (
    PetType.BEAR, PetType.BOAR, PetType.CAT, PetType.RABBIT, PetType.SHEEP, PetType.DOG,
    PetType.EAGLE)
#: Eggs a supporter (patron, crafter, Twitch subscriber, YouTube supporter) can find.
PATRON_EGGS: tuple[PetType, ...] = (
    *STANDARD_EGGS, PetType.DRAGON, PetType.GHOST, PetType.PHOENIX, PetType.UNICORN, PetType.SNAKE,
    PetType.ANIME_GIRL, PetType.ELEMENTAL, PetType.GUINEA, PetType.SKELETON, PetType.MONKEY)
#: The only mythic pet.
MYTHIC_EGGS: tuple[PetType, ...] = (PetType.VOID,)
#: What a mirror can turn a pet into.
TRANSFORM_TYPES: tuple[PetType, ...] = (
    PetType.BEAR, PetType.BOAR, PetType.CAT, PetType.RABBIT, PetType.SHEEP, PetType.DRAGON,
    PetType.PHOENIX, PetType.UNICORN, PetType.SNAKE, PetType.ANIME_GIRL, PetType.ELEMENTAL,
    PetType.MONKEY, PetType.DOG, PetType.EAGLE, PetType.GUINEA, PetType.SKELETON, PetType.GHOST)
PREMIUM_TYPES: frozenset[PetType] = frozenset({
    PetType.DRAGON, PetType.PHOENIX, PetType.UNICORN, PetType.SNAKE, PetType.ANIME_GIRL,
    PetType.ELEMENTAL, PetType.GUINEA, PetType.SKELETON, PetType.MONKEY, PetType.GHOST})

#: Egg roll ``randint(0, 1000)``: at least this much makes the pet special.
SHINY_FROM, ULTRA_SHINY_FROM, MYTHIC_FROM = 900, 980, 998

#: Cosmetics of a supporter's new egg.  The owner's balance, per eligible pet: "First roll,
#: disjoint results: no seal 85%, Tropiciela 10%, Łowcy 4.8%, Arcyłowcy 0.2%.  Second roll
#: gives Holo with a chance of 0.1% (1/1000).  No cosmetic gives stats."  Since 2026-11-21 the
#: second roll has three disjoint results: no foil 98.9%, partial Holo 1%, full Holo 0.1%.
#: Both are ``random.randrange(COSMETIC_ROLL)``, independent of each other and of the pet's
#: class; whole numbers only, so the odds are exact.
COSMETIC_ROLL = 1000
#: Seal roll: the first entry whose bound is above the roll gives the tier -
#: 0-1 Arcyłowcy (0.2%), 2-49 Łowcy (4.8%), 50-149 Tropiciela (10%), 150-999 no seal (85%).
SEAL_ROLL_BELOW: tuple[tuple[int, int], ...] = ((2, 3), (50, 2), (150, 1))
#: Foil roll, the same way: 0 full Holo (0.1% - the very roll that gave Holo before the
#: partial foil existed), 1-10 partial Holo (1%), 11-999 no foil (98.9%).
FOIL_ROLL_BELOW: tuple[tuple[int, int], ...] = ((1, petcards.FOIL_FULL), (11, petcards.FOIL_PARTIAL))
#: Seal tiers are 0 (none) .. MAX_SEAL; their names are ``petcards.SEAL_NAMES``.
MAX_SEAL = 3
#: The third roll, only for an egg with a class (Shiny / Ultra Shiny / mythic):
#: ``random.randrange(FRAME_ROLL)`` in units of 0.01%, so every percent of the owner's pools
#: (``petcards.POOLS``, down to 0.15%) is a whole number of units - no rounding anywhere.
FRAME_ROLL = 10000
#: Results that are worth a line in the owner's log (:data:`MSG_RARE_COSMETICS`) besides every
#: animated frame: the rarest static frame of each pool (R7 / R8 / R9, 1% of their class - as rare
#: as full Holo for a Shiny egg, far rarer for the others).
RARE_FRAME_PERCENT = 1.0


def _frame_table(tier: str) -> tuple[tuple[int, str], ...]:
    """``(bound, result id)`` of a class's pool in :data:`FRAME_ROLL` units, in pool order.

    The first entry whose bound is above the roll is the result.  Computed from
    the percents of ``petcards.POOLS`` as exact fractions; a pool whose percents
    are not whole units or do not cover exactly 100% is a programming error.
    """
    bound = 0
    table = []
    for frame in petcards.POOLS[tier]:
        units = Fraction(str(frame.percent)) * FRAME_ROLL / 100
        if units.denominator != 1 or units <= 0:
            raise ValueError(f"frame {frame.id}: {frame.percent}% is not a whole number of 0.01% units")
        bound += int(units)
        table.append((bound, frame.id))
    if bound != FRAME_ROLL:
        raise ValueError(f"frame pool {tier} covers {bound} of {FRAME_ROLL} units, not 100%")
    return tuple(table)


#: Every class's frame roll, see :func:`_frame_table`.
FRAME_ROLL_BELOW: dict[str, tuple[tuple[int, str], ...]] = {tier: _frame_table(tier) for tier in petcards.POOLS}

#: Stats a talent point can go to; the order is the order of the ``$mocneodrodzenie`` menu.
SKILL_POOL: tuple[str, ...] = (
    "CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC", "SLOW_PERC", "DETECTION")
#: A pet that slows this much cannot get more slow.
SLOW_CAP = 80
#: Points one talent adds, as (mythic, ultra shiny, shiny, ordinary) ``randint`` ranges.
#: INIT_PERC is kept from the legacy table although no pool contains it.
_GAINS: dict[str, tuple[tuple[int, int], ...]] = {
    "INIT_PERC": ((8, 14), (6, 13), (5, 10), (5, 8)),
    "SLOW_PERC": ((11, 16), (9, 15), (8, 12), (5, 8)),
}
_DEFAULT_GAINS: tuple[tuple[int, int], ...] = ((8, 12), (6, 11), (5, 8), (3, 5))

MAX_LEVEL = 3
#: A level 3 pet with at most this talent can be enlightened.
ENLIGHT_MAX_SKILLS = 6
DEFAULT_NAME = "Towarzysz"
MAX_NAME_LENGTH = 255  # PETS.PET_NAME is VARCHAR(255)

#: What :func:`get_pet_skills` returns for a player without a pet (legacy keys and order).
NO_PET_SKILLS: dict[str, Any] = {
    "CRIT_PERC": 0, "REPLACE_PERC": 0, "DEF_PERC": 0, "DROP_PERC": 0,
    "LOWHP_PERC": 0, "SLOW_PERC": 0, "INIT_PERC": 0, "DETECTION": False}

POLISH_TYPES: dict[str, str] = {
    "Bear": "Niedźwiedź", "Cat": "Kot", "Boar": "Dzik", "Dragon": "Smok", "Phoenix": "Feniks",
    "Rabbit": "Królik", "Sheep": "Owca", "Unicorn": "Jednorożec", "Snake": "Wąż",
    "AnimeGirl": "Dziewczynka Anime", "Elemental": "Żywiołak", "Dog": "Pies",
    "Guinea": "Świnka morska", "Skeleton": "Szkielet", "Monkey": "Małpa", "Eagle": "Orzeł",
    "Ghost": "Zjawa", "Void": "Istota pustki",
}
TALENTS: tuple[str, ...] = ("Miernota", "Nowicjusz", "Uczeń", "Przeciętny", "Ekspert", "Mistrz",
                            "Oświecony", "Transcendentny", "Pozaziemski", "Boski")
_RARITY_NAMES: tuple[str, ...] = ("zwykły", "rzadki", "epicki", "legendarny", "mityczny")

_STABLE_SLOTS: dict[int, str] = {1: "PET_ID_ALT1", 2: "PET_ID_ALT2", 3: "PET_ID_ALT3", 4: "PET_ID_ALT4"}

#: ``$mocneodrodzenie``: 1️⃣ .. 7️⃣ (one per entry of SKILL_POOL), shown in the list.
SKILL_EMOTES: tuple[str, ...] = tuple(f"{digit}️⃣" for digit in range(1, 8))
#: What the entries of SKILL_POOL are called in the ``$mocneodrodzenie`` list.
SKILL_LABELS: tuple[str, ...] = (
    "Szansa na krytyczne uderzenie", "Szansa na zastąpienie ataku", "Szansa na zablokowanie ataku",
    "Rzadszy drop", "Zmniejszenie życia przeciwnika", "Spowolnienie przeciwnika", "Wyczuwanie bossa")
#: At most this many stats can be left out of a reroll (10 scrolls each).
MAX_SKIPPED = 3
ACCEPT_EMOTE = "✅"
CONFIRM_WORD = "$potwierdzam"
CONFIRM_TIMEOUT = 15
MENU_TIMEOUT = 20
#: Longest time the advanced reroll menu may stay open altogether (it keeps the channel busy).
MENU_TOTAL_TIMEOUT = 180
#: How long the buttons under a shown pet / the stable stay (seconds without a click).
PANEL_TIMEOUT = 180
#: How long the rename pop-up window may stay open.
RENAME_WINDOW_TIMEOUT = 300
#: Upload limit of a server that does not tell its own (10 MiB, Discord's limit without boosts).
#: The same number as ``boss.DEFAULT_UPLOAD_LIMIT``, repeated here because features/boss.py
#: imports this module (lazily) - importing it back would tie the two modules in a knot.
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024
#: Share of the upload limit a pet card leaves free: the limit counts the whole request
#: (multipart framing, the embed), not the file alone.
CARD_SIZE_MARGIN = 0.05

# -- texts (verbatim from the legacy bot) ---------------------------------------------

MSG_UNSURE = "*Twój towarzysz spogląda na Ciebie niepewnie.*"
MSG_ALONE_MAD = "Przecież jesteś sam... <:madge:882184635474386974>"
#: Ends every "are you sure?" question (the buttons sit under it).
CONFIRM_HINT = "Kliknij **Potwierdzam** albo wpisz **$potwierdzam**."
MSG_ALREADY_WAITING = ("Już czekam na Twoje potwierdzenie. Kliknij **Potwierdzam** (albo wpisz "
                       "**$potwierdzam**) albo poczekaj, aż prośba wygaśnie.")
MSG_NO_PET_DATA = "Nie znaleziono danych towarzysza."
MSG_STORED = "Towarzysz został ulokowany w stajni <:peepoBlush:984769061340737586>"
MSG_UNSTORED = "Towarzysz został wyciągnięty ze stajni <:peepoBlush:984769061340737586>"
MSG_SWAPPED = ("Nowy towarzysz został wyciągnięty ze stajni, a stary został w niej schowany "
               "<:peepoBlush:984769061340737586>")
MSG_EMPTY_SLOT = "W stajni nie ma towarzysza na podanym miejscu. Sprawdź stajnie wpisując **$stajnia**."
MSG_TOO_MANY_SKILLS = ("Wybrałeś za dużo statystyk. Można pominąć **maksymalnie 3**! "
                       "<:madge:882184635474386974>")
MSG_ADV_REROLL_MENU = (
    "Wybierz z listy, które umiejętności chcesz pominąć przy przelosowaniu."
    "\nKażda wybrana statystyka to koszt 10 zwojów, maksymalnie 3 do wyboru."
    "\n\n*Potwierdź przyciskiem **Przelosuj**.*")
MSG_RENAME_DONE = "Twój towarzysz ma teraz na imię **{name}** <:peepoBlush:984769061340737586>"
MSG_RENAME_EMPTY = "Imię towarzysza nie może być puste <:Hmm:984767035617730620>"
MSG_RENAME_COOLDOWN = ("Poczekaj na odnowienie komendy! Imię zmienisz ponownie {when} "
                       "<:Bedge:970576892874854400>")
#: A panel button under a pet that is no longer the active one (another pet came out of the stable).
MSG_OTHER_PET = "Ten panel pokazuje innego towarzysza - wpisz **$towarzysz**, żeby zobaczyć aktualnego."
#: A stable list entry whose slot no longer holds the pet it names.
MSG_STABLE_CHANGED = "Stajnia zmieniła się w międzyczasie - wybierz jeszcze raz."
#: The owner's log channel (no ping, nothing public): an egg with the best seal, full Holo
#: and / or a rare frame (one message per egg, the finds joined by ", " and " i ").
MSG_RARE_COSMETICS = "Rzadka kosmetyka towarzysza: jajko gracza <@{player_id}> (PET_ID {pet_id}) ma {found}."
MSG_RARE_SEAL = "pieczęć {name}"
MSG_RARE_HOLO = "folię Holo"
MSG_RARE_FRAME = "ramkę {name}"
PET_FOOTER = "Na zawsze ponosisz odpowiedzialność za to, co oswoiłeś."
#: The ornaments in the pet's statistics (a base frame is not named: every pet of the class has it).
MSG_FRAME_LINE = "Ramka: {name}"
MSG_SEAL_LINE = "Pieczęć: {name}"
MSG_FOIL_LINE = "Folia: {name}"
#: How the foils are called there (full Holo is "Holo", as it always was).
FOIL_WORDS: dict[int, str] = {petcards.FOIL_FULL: "Holo", petcards.FOIL_PARTIAL: "Holo częściowe"}
#: In the statistics of a pet whose ornaments (a rarer frame, the seal, the foil) are hidden
#: because its owner does not support now - one line, whichever of them the pet has.
MSG_COSMETICS_HIDDEN = "*Ozdoby karty (wyjątkowa ramka, pieczęć, folia Holo) widać tylko u wspierających.*"
RANKING_IMAGE = ("https://www.altermmo.pl/wp-content/uploads/alter0000_Adventurer_with_a_small_dragon_"
                 "pet._Fantasy._Pixel_ar_fac9f513-e338-4727-af61-32b79dbfa7e3.png")
_OK = "ok"


def _msg_alone(player_id: int) -> str:
    return ("Niestety jesteś sam jak palec na tym świecie <@" + str(player_id) + "> "
            "<:Sadge:936907659142111273> Spróbuj zawalczyć z potworami, a może i są inne sposoby "
            "na zdobycie towarzysza?")


def _msg_bad_slot(player_id: int, command: str) -> str:
    return ("Każdy gracz ma tylko cztery miejsca na przechowanie slotów - 1, 2, 3 oraz 4 <@"
            + str(player_id) + ">! Wpisz np. **$" + command + " 1**.")


def _scrolls(number: int) -> str:
    """``1 zwój`` / ``20 zwojów`` (the costs of a reroll are 1, 10, 20 or 30)."""
    if number == 1:
        return "1 zwój"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f"{number} zwoje"
    return f"{number} zwojów"


def adv_reroll_text(selected: Sequence[int]) -> str:
    """The ``$mocneodrodzenie`` menu with what is chosen so far and what it costs."""
    chosen = [SKILL_LABELS[index] for index in sorted(selected) if 0 <= index < len(SKILL_LABELS)]
    cost = 10 * len(chosen) if chosen else 1
    return (MSG_ADV_REROLL_MENU + "\n\nPominięte: " + (", ".join(f"**{name}**" for name in chosen) or "brak")
            + f" - koszt: **{_scrolls(cost)}**.")


# -- small helpers --------------------------------------------------------------------

def _pid(player: Any) -> int:
    """Id of a member (or a bare id given as int/str)."""
    return int(getattr(player, "id", player))


def _int(value: Any) -> int:
    """A NUMERIC (``Decimal``) or NULL from the database as a plain int."""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError, ArithmeticError):
        return 0


def _num(value: Any) -> int | float:
    """A stat from the database as int (or float when it really has a fraction)."""
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _talent(skills: int) -> str:
    return TALENTS[skills] if 0 <= skills < len(TALENTS) else TALENTS[-1]


def _stats_of(pet: Any) -> dict[str, Any]:
    """The stats of a ``PETS`` row a talent point can change."""
    stats: dict[str, Any] = {name: _num(pet[name.lower()]) for name in SKILL_POOL}
    stats["DETECTION"] = bool(pet["detection"])
    return stats


def cosmetics_of(pet: Any) -> tuple[int, bool]:
    """``(seal tier 0-3, holo)`` of a ``PETS`` row.

    A row without the columns (the old bot's table, see :data:`NEW_COLUMNS`), a
    NULL or a tier nobody knows reads as "no seal" / "no Holo".
    """
    try:
        seal = _int(pet["seal"])
    except (KeyError, IndexError, TypeError):
        seal = 0
    try:
        holo = pet["holo"] is True
    except (KeyError, IndexError, TypeError):
        holo = False
    return (seal if 0 < seal <= MAX_SEAL else 0), holo


class Ornaments(NamedTuple):
    """What a pet has beyond the look of its class: a rarer frame, a seal, a foil.

    ``frame`` is the result id of a frame that is NOT the base of the pet's class
    ('' otherwise - the base frame is no ornament: every pet of the class has
    it, also when its owner does not support); ``seal`` 0-3; ``foil`` a
    ``petcards.FOIL_*`` (full Holo wins when both flags are set).
    """

    frame: str = ""
    seal: int = 0
    foil: int = petcards.FOIL_NONE

    @property
    def any(self) -> bool:
        """True when something is there that only a supporter's pet shows."""
        return bool(self.frame or self.seal or self.foil)


def ornaments_of(pet: Any) -> Ornaments:
    """The :class:`Ornaments` of a ``PETS`` row (or an album entry).

    Every column is optional: a row of the old bot's table, of the v2 table of
    2026-11-20 (no ``FRAME`` / ``HOLO_PARTIAL``), a NULL, an unknown frame id
    or one of another class reads as "nothing" (the frame: the base of the
    class, ``petcards.frame_of``).
    """
    seal, holo = cosmetics_of(pet)
    try:
        partial = pet["holo_partial"] is True
    except (KeyError, IndexError, TypeError):
        partial = False
    try:
        frame = petcards.frame_of(pet)
    except Exception:  # noqa: BLE001 - decoration: a row that cannot say has no rarer frame
        frame = None
    foil = petcards.FOIL_FULL if holo else petcards.FOIL_PARTIAL if partial else petcards.FOIL_NONE
    return Ornaments("" if frame is None or frame.base else frame.id, seal, foil)


class Rolled(NamedTuple):
    """The cosmetic rolls of one egg: seal tier, ``petcards.FOIL_*`` and the frame to store
    ('' = the base of the class; always '' for an egg without a class)."""

    seal: int
    foil: int
    frame: str


def _seal_for(roll: int) -> int:
    """The seal tier of a seal roll (``0 .. COSMETIC_ROLL - 1``), see :data:`SEAL_ROLL_BELOW`."""
    return next((tier for below, tier in SEAL_ROLL_BELOW if roll < below), 0)


def _foil_for(roll: int) -> int:
    """The foil of a foil roll (``0 .. COSMETIC_ROLL - 1``), see :data:`FOIL_ROLL_BELOW`."""
    return next((foil for below, foil in FOIL_ROLL_BELOW if roll < below), petcards.FOIL_NONE)


def _frame_for(tier: str, roll: int) -> str:
    """The frame result to store for a frame roll (``0 .. FRAME_ROLL - 1``) of class ``tier``.

    The base result of the class is stored as '' (what every older pet has).
    """
    result = next((ident for below, ident in FRAME_ROLL_BELOW[tier] if roll < below),
                  petcards.BASE_FRAME[tier])
    return "" if result == petcards.BASE_FRAME[tier] else result


def _roll_cosmetics(tier: str = "", *, frames: bool = True) -> Rolled:
    """The cosmetic rolls of one egg of class ``tier`` ('' / "S" / "U" / "M").

    Always in this order: the seal roll, the foil roll (both
    ``randrange(COSMETIC_ROLL)``), then - only for an egg with a class, and only
    when ``frames`` (``PETS`` can keep the frame, see :func:`frames_available`) -
    the frame roll ``randrange(FRAME_ROLL)``.  Each roll is used by exactly one
    result, so the three are independent.
    """
    seal = _seal_for(random.randrange(COSMETIC_ROLL))
    foil = _foil_for(random.randrange(COSMETIC_ROLL))
    frame = _frame_for(tier, random.randrange(FRAME_ROLL)) if frames and tier in FRAME_ROLL_BELOW else ""
    return Rolled(seal, foil, frame)


def _rare_frame(ident: str) -> petcards.Frame | None:
    """The frame of result ``ident`` when the owner wants to hear about it (see :data:`RARE_FRAME_PERCENT`)."""
    frame = petcards.FRAMES.get(ident) if ident else None
    if frame is None or frame.base:
        return None
    return frame if frame.animated or frame.percent <= RARE_FRAME_PERCENT else None


def _slot_column(slot: Any) -> str | None:
    """Stable column for ``1``..``4`` (also given as text); None for anything else."""
    try:
        return _STABLE_SLOTS.get(int(str(slot).strip()))
    except (TypeError, ValueError):
        return None


def _roll_skill(stats: dict[str, Any], pool: Sequence[str], pet: Any) -> str | None:
    """Spend one talent point on a random stat of ``pool``; returns the stat.

    Detection can be learnt once and slow stops at :data:`SLOW_CAP`.  The legacy
    code drew again when it hit such a stat; drawing from what is still possible
    gives the same odds without a loop that could spin for ever.
    """
    eligible = [name for name in pool
                if not (name == "DETECTION" and stats["DETECTION"])
                and not (name == "SLOW_PERC" and stats["SLOW_PERC"] >= SLOW_CAP)]
    if not eligible:
        return None
    name = random.choice(eligible)
    if name == "DETECTION":
        stats[name] = True
        return name
    mythic, ultra, shiny, ordinary = _GAINS.get(name, _DEFAULT_GAINS)
    low, high = mythic if pet["mythic"] else ultra if pet["ultra_shiny"] else shiny if pet["shiny"] else ordinary
    stats[name] += random.randint(low, high)
    return name


# -- database helpers -----------------------------------------------------------------

#: Namespaces of the advisory locks (two-int form, so they cannot meet a lock
#: another module takes on a bare player id).
_LOCK_OWNER, _LOCK_PET_IDS = 7301, 7302

_SQL_ACTIVE_PET = """SELECT p.* FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
                     WHERE o.PLAYER_ID = $1 AND o.PET_ID > 0 LIMIT 1"""
#: What growing, a reroll and an enlightenment save.  SEAL, HOLO, HOLO_PARTIAL and FRAME are not
#: in here on purpose (nor in any other UPDATE but :func:`set_cosmetics` / :func:`set_frame`):
#: cosmetics are for the pet's whole life.
_SQL_SAVE_PET = """UPDATE PETS SET PET_LVL = $2, PET_SKILLS = $3, VARIANT = $4,
                       CRIT_PERC = $5, REPLACE_PERC = $6, DEF_PERC = $7, DROP_PERC = $8,
                       LOWHP_PERC = $9, SLOW_PERC = $10, DETECTION = $11
                   WHERE PET_ID = $1"""


#: Which of the given columns the table the bot reads as ``PETS`` has.
_SQL_PRESENT_COLUMNS = """SELECT attname::text FROM pg_attribute
                          WHERE attrelid = to_regclass('pets') AND attname = ANY($1::text[])
                            AND attnum > 0 AND NOT attisdropped"""


def _for_update(sql: str, lock: bool) -> str:
    return sql + " FOR UPDATE" if lock else sql


async def _look_for_columns(bot: "AlterBot") -> None:
    """Find out which cosmetic columns ``PETS`` has; remembers both answers on the bot."""
    wanted = [*COSMETIC_COLUMNS, *FRAME_COLUMNS]
    present = {str(row[0]) for row in await bot.db.fetch(_SQL_PRESENT_COLUMNS, wanted)}
    cosmetics = present.issuperset(COSMETIC_COLUMNS)
    frames = cosmetics and present.issuperset(FRAME_COLUMNS)
    bot._pet_cosmetics, bot._pet_frames = cosmetics, frames  # type: ignore[attr-defined]
    if not cosmetics:
        log.warning("PETS has no %s column(s): pet cosmetics are off (nothing is rolled, pets are shown "
                    "without seals, foils and rarer frames; the base frames of the classes do not need the "
                    "columns).", " / ".join(column.upper() for column in COSMETIC_COLUMNS if column not in present))
    elif not frames:
        log.warning("PETS has no %s column(s): seals and full Holo work, but no frame is rolled and a "
                    "partial Holo is not kept (every pet shows the base frame of its class).",
                    " / ".join(column.upper() for column in FRAME_COLUMNS if column not in present))


async def cosmetics_available(bot: "AlterBot", *, refresh: bool = False) -> bool:
    """Whether ``PETS`` has the first cosmetic columns (``SEAL`` / ``HOLO``): seals and Holo.

    Looked up in the database once per run and remembered on the bot, like the
    ``SKILLPOUCH`` check of features/skills.py: the columns are added at
    start-up (``schema.add_new_columns``), and when that could not be done (no
    privilege, the table was locked) the bot plays on without cosmetics until
    its next start.  ``refresh=True`` looks again after a "no" (the owner may
    have added the columns by hand meanwhile).  One look answers this and
    :func:`frames_available` together.
    """
    known = getattr(bot, "_pet_cosmetics", None)
    if known is None or (refresh and not known):
        await _look_for_columns(bot)
        known = bot._pet_cosmetics  # type: ignore[attr-defined]
    return bool(known)


async def frames_available(bot: "AlterBot", *, refresh: bool = False) -> bool:
    """Whether ``PETS`` has ALL the cosmetic columns, the frame and partial Holo ones too.

    False on the v2 table of 2026-11-20 whose ``FRAME`` / ``HOLO_PARTIAL`` could
    not be added at start-up (``schema.add_new_columns`` adds a table's columns
    all or none, so only the new two can be missing beside working seals and
    Holo): the game then works as on that day - the seal roll and the full Holo
    of the foil roll are kept, the frame is not rolled, a partial Holo result
    is not kept, and every pet shows the base frame of its class.  Remembered
    like :func:`cosmetics_available`.
    """
    known = getattr(bot, "_pet_frames", None)
    if known is None or (refresh and not known):
        await _look_for_columns(bot)
        known = bot._pet_frames  # type: ignore[attr-defined]
    return bool(known)


async def _lock_player(con: Any, player_id: int) -> None:
    """Serialise changes of one player's ``PETOWNER`` row (the table has no unique key)."""
    await con.execute("SELECT pg_advisory_xact_lock($1::int4, $2::int4)",
                      _LOCK_OWNER, player_id % 2_000_000_000)


async def _owner(q: Any, player_id: int, columns: str, *, lock: bool = False) -> Any:
    """``columns`` of the player's ``PETOWNER`` row (None when there is no row)."""
    return await q.fetchrow(
        _for_update(f"SELECT {columns} FROM PETOWNER WHERE PLAYER_ID = $1", lock), player_id)


async def _pet(q: Any, pet_id: int, *, lock: bool = False) -> Any:
    return await q.fetchrow(_for_update("SELECT * FROM PETS WHERE PET_ID = $1", lock), pet_id)


async def _active_pet(q: Any, player_id: int, *, lock: bool = False) -> Any:
    """The ``PETS`` row of the player's active pet, or None."""
    return await q.fetchrow(_SQL_ACTIVE_PET + (" FOR UPDATE OF p" if lock else ""), player_id)


async def _save_pet(con: Any, pet: Any, stats: dict[str, Any], *, level: int | None = None,
                    skills: int | None = None, variant: int | None = None) -> None:
    await con.execute(
        _SQL_SAVE_PET, pet["pet_id"],
        _int(pet["pet_lvl"]) if level is None else level,
        _int(pet["pet_skills"]) if skills is None else skills,
        pet["variant"] if variant is None else str(variant),
        *(stats[name] for name in SKILL_POOL))


async def _insert_owner(con: Any, player_id: int, pet_id: int, pet_owned: bool, reroll_scroll: int,
                        reroll_scroll_shard: int, rebirth_stones: int, mirrors: int, skill_id: int) -> None:
    await con.execute(
        """INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED, REROLL_SCROLL, REROLL_SCROLL_SHARD,
               REBIRTH_STONES, MIRRORS, SKILL_GEM, PET_ID_ALT1, PET_ID_ALT2, PET_ID_ALT3, PET_ID_ALT4)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 0, 0, 0)""",
        player_id, pet_id, pet_owned, reroll_scroll, reroll_scroll_shard, rebirth_stones, mirrors, skill_id)


async def new_record_petowners(bot: "AlterBot", player_id: int, pet_id: int, pet_owned: bool,
                               reroll_scroll: int, reroll_scroll_shard: int, rebirth_stones: int,
                               mirrors: int, skill_id: int) -> bool:
    """New record of user in petowners database; True when a row was created.

    The table has no unique key, so two flows creating "the first row" of the
    same player used to leave duplicates.  When the player already has a row
    nothing is inserted: the counters are added to it and the pet / skill gem
    fill the empty places (False is returned).
    """
    player_id = _pid(player_id)
    values = (int(pet_id), int(reroll_scroll), int(reroll_scroll_shard), int(rebirth_stones),
              int(mirrors), int(skill_id))
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        if await _owner(con, player_id, "PLAYER_ID") is None:
            await _insert_owner(con, player_id, values[0], bool(pet_owned), *values[1:])
            return True
        await con.execute(
            """UPDATE PETOWNER SET
                   PET_ID = CASE WHEN COALESCE(PET_ID, 0) <= 0 THEN $2::numeric ELSE PET_ID END,
                   REROLL_SCROLL = COALESCE(REROLL_SCROLL, 0) + $3::numeric,
                   REROLL_SCROLL_SHARD = COALESCE(REROLL_SCROLL_SHARD, 0) + $4::numeric,
                   REBIRTH_STONES = COALESCE(REBIRTH_STONES, 0) + $5::numeric,
                   MIRRORS = COALESCE(MIRRORS, 0) + $6::numeric,
                   SKILL_GEM = CASE WHEN COALESCE(SKILL_GEM, 0) <= 0 THEN $7::numeric ELSE SKILL_GEM END
               WHERE PLAYER_ID = $1""", player_id, *values)
    log.warning("PETOWNER row of player %s already existed; merged instead of adding a duplicate.", player_id)
    return False


async def create_pet_owners_table(bot: "AlterBot") -> None:
    """Create ``PETOWNER`` when it is missing; the legacy default row only into an empty table."""
    await bot.db.execute(SCHEMA[0], idempotent=True)
    async with bot.db.transaction() as con:
        await con.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", _LOCK_OWNER)
        if await con.fetchval("SELECT 1 FROM PETOWNER LIMIT 1") is None:
            await con.execute("INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED) VALUES ($1, $2, $3)",
                              0, 0, True)
            log.info("Table PETOWNER seeded with the default row.")


async def create_pets_table(bot: "AlterBot") -> None:
    """Create ``PETS`` when it is missing; the legacy default row only into an empty table."""
    await bot.db.execute(SCHEMA[1], idempotent=True)
    async with bot.db.transaction() as con:
        await con.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", _LOCK_PET_IDS)
        if await con.fetchval("SELECT 1 FROM PETS LIMIT 1") is None:
            await con.execute(
                """INSERT INTO PETS (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY,
                       TYPE, VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC, LOWHP_PERC,
                       SLOW_PERC, INIT_PERC, DETECTION)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)""",
                0, "Default", 0, 0, "Standard", True, True, "Type", "0", 1, 2, 3, 4, 5, 6, 7, False)
            log.info("Table PETS seeded with the default row.")


# -- showing a pet --------------------------------------------------------------------

def _pet_image(pet: Any) -> Path | None:
    """Picture of an egg / a pet, or None when the row does not describe one."""
    quality, pet_type, variant = pet["quality"], str(pet["type"]), str(pet["variant"])
    level = _int(pet["pet_lvl"])
    if level == 0:
        if quality == "Standard":
            return ASSETS_DIR / "eggs" / "standard" / f"{variant}.png"
        if quality == "Premium":
            return ASSETS_DIR / "eggs" / "premium" / pet_type / f"{variant}.png"
        if quality == "Mythic":
            return ASSETS_DIR / "eggs" / "mythic" / pet_type / f"{variant}.gif"
        return None
    try:
        number = (0 if level == 1 else 10 ** (level - 1)) + int(variant)
    except ValueError:
        return None
    if quality in ("Standard", "Premium"):
        return ASSETS_DIR / "pets" / pet_type / f"{number}.png"
    if quality == "Mythic":
        return ASSETS_DIR / "pets" / pet_type / f"{number}.gif"
    return None


def ornament_lines(pet: Any) -> list[str]:
    """The pet's ornaments in words: ``Ramka: …`` (a rarer frame only), ``Pieczęć: …``, ``Folia: …``."""
    frame, seal, foil = ornaments_of(pet)
    lines = []
    if frame:
        lines.append(MSG_FRAME_LINE.format(name=petcards.FRAMES[frame].label))
    if seal in petcards.SEAL_NAMES:
        lines.append(MSG_SEAL_LINE.format(name=petcards.SEAL_NAMES[seal]))
    if foil in FOIL_WORDS:
        lines.append(MSG_FOIL_LINE.format(name=FOIL_WORDS[foil]))
    return lines


def _pet_embed(player_id: int, pet: Any, items: Any, *, cosmetics: bool = True) -> discord.Embed:
    """The pet's embed.  ``cosmetics``: whether its ornaments are shown (the owner supports)."""
    scroll_desc = (f"\n**💎 PRZEDMIOTY:**\nZwoje odrodzenia: {_int(items['reroll_scroll'])}\n" +
                   f"Fragmenty zwojów: {_int(items['reroll_scroll_shard'])}\n"
                   f"Kamienie olśnienia: {_int(items['rebirth_stones'])}" +
                   f"\nLustra wizualiów: {_int(items['mirrors'])}")

    if pet["mythic"]:
        color = 0xff4815
        add_desc = "\n\n*Mityczny towarzysz.*"
    elif pet["ultra_shiny"]:
        color = 0xff8d15
        add_desc = "\n\n*Absolutnie nadzwyczajny towarzysz.*"
    elif pet["shiny"]:
        color = 0xffdd00
        add_desc = "\n\n*Rzadki towarzysz. Miałeś niesamowite szczęście.*"
    else:
        color = 0xfffffc
        add_desc = ""

    # A rarer frame, the seal and the foil in words as well - for an egg too (owner's decision
    # of 2026-11-20: they are visible from the egg on).  They are shown only while the owner
    # supports; otherwise one line says why they are gone.
    cosmetic_lines = ""
    if cosmetics:
        cosmetic_lines = "".join(line + "\n" for line in ornament_lines(pet))
    elif ornaments_of(pet).any:
        cosmetic_lines = MSG_COSMETICS_HIDDEN + "\n"

    name, level = pet["pet_name"], _int(pet["pet_lvl"])
    if level == 0:
        title = f'Jajko {name}' if name != DEFAULT_NAME else 'Jajko'
        description = ("Oto Twój towarzysz, <@" + str(player_id) + ">. Opiekuj się nim, a być może "
                       "kiedyś coś z niego wyrośnie...\n" + ("\n" + cosmetic_lines if cosmetic_lines else "")
                       + scroll_desc + add_desc)
    else:
        polish_type = POLISH_TYPES.get(pet["type"], "")
        title = f'{polish_type} {name}' if name != DEFAULT_NAME else f'{polish_type}'
        skill_desc = "📜 **STATYSTYKI:**\n"
        skill_desc += f"Poziom: {level}\n"
        skill_desc += f"Talent: {_talent(_int(pet['pet_skills']))}\n"
        look_name = "Mityczny" if pet["quality"] == "Mythic" else pet["quality"]
        skill_desc += f"Wygląd: {look_name}\n"
        skill_desc += cosmetic_lines + "\n"
        for column, label in (("replace_perc", "Szansa na zastąpienie ataku"),
                              ("crit_perc", "Szansa na krytyczne uderzenie"),
                              ("def_perc", "Szansa na zablokowanie ataku"),
                              ("lowhp_perc", "Zmniejszenie życia przeciwnika"),
                              ("slow_perc", "Spowolnienie przeciwnika"),
                              ("drop_perc", "Rzadszy drop o"),
                              ("init_perc", "Szansa na inicjację bossa")):
            value = _num(pet[column])
            if value > 0:
                skill_desc += f"{label}: {value} %\n"
        if pet["detection"] is True:
            skill_desc += "Wyczuwanie bossa: Tak\n"
        description = ("Oto Twój towarzysz, <@" + str(player_id) + ">.\n\n" + skill_desc
                       + scroll_desc + add_desc)

    embed = discord.Embed(title=title[:256], description=description, color=color)
    embed.set_footer(text=PET_FOOTER)
    return embed


_SHOW_COLUMNS = "PET_ID, REROLL_SCROLL, REROLL_SCROLL_SHARD, REBIRTH_STONES, MIRRORS"


async def _load_shown_pet(bot: "AlterBot", player_id: int) -> tuple[str, Any, Any]:
    """``(status, items, pet)`` of what ``$towarzysz`` shows; status ``alone`` / ``no_data`` / ``_OK``."""
    owner = await _owner(bot.db, player_id, _SHOW_COLUMNS)
    pet_id = _int(owner["pet_id"]) if owner is not None else 0
    if pet_id <= 0:
        return "alone", owner, None
    pet = await _pet(bot.db, pet_id)
    if pet is None:
        log.warning("Player %s owns pet %s which has no row in PETS.", player_id, pet_id)
        return "no_data", owner, None
    return _OK, owner, pet


def _shown_embed(player_id: int, pet: Any, items: Any, *,
                 cosmetics: bool = True) -> tuple[discord.Embed, Path | None]:
    """The pet's embed and its source picture (None: no picture; the embed then has no image).

    The picture is the plain illustration; :func:`_shown` turns it into the card.
    """
    embed = _pet_embed(player_id, pet, items, cosmetics=cosmetics)
    image = _pet_image(pet)
    if image is None or not image.is_file():
        log.warning("Pet %s (%s, %s, level %s, variant %s) has no picture (%s); showing it without one.",
                    pet["pet_id"], pet["quality"], pet["type"], pet["pet_lvl"], pet["variant"], image)
        return embed, None
    embed.set_image(url=f"attachment://{image.name}")
    return embed, image


def _card_cache(bot: "AlterBot") -> Path:
    """Where the composed cards are kept: ``<data dir>/cache/petcards``.

    (A sub-folder: the copies of features/backup.py take only the files lying
    directly in the data folder, so the cache never ends up in a copy.)
    """
    return bot.store.data_dir / "cache" / "petcards"


def _card_max_bytes(guild: Any) -> int:
    """The biggest card that can be sent to ``guild``: its upload limit less :data:`CARD_SIZE_MARGIN`."""
    limit = getattr(guild, "filesize_limit", None) or DEFAULT_UPLOAD_LIMIT
    return int(limit * (1 - CARD_SIZE_MARGIN))


async def _is_supporter(bot: "AlterBot", player: Any) -> bool:
    """Whether the pet's owner supports the server right now (decides if the cosmetics are shown).

    ``player`` is a member (his current roles are asked) or a bare id (the
    member is looked up; somebody who left the server supports nothing).  A
    lookup that fails hides the cosmetics for this one showing - it never
    stops the pet from being shown.
    """
    if getattr(player, "roles", None) is not None:
        return patrons.check_if_patron(player)
    try:
        member = await bot.member(_pid(player))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - decoration: show the pet without it
        log.warning("Cannot tell whether %s is a supporter (%s: %s); cosmetics hidden.",
                    player, type(exc).__name__, exc)
        return False
    return patrons.check_if_patron(member)


async def _pet_card(bot: "AlterBot", guild: Any, pet: Any, source: Path | None, *,
                    cosmetics: bool = True, wait: waiting.Waiting | None = None) -> Path | None:
    """The file to send for ``pet`` whose plain picture is ``source``.

    A pet is shown as its card: illustration, foil, its frame, seal
    (:func:`petcards.card`; an ordinary pet without cosmetics, a mythic one
    without them and a card that could not be composed stay the plain
    picture).  An egg is a card like any other (owner's decision of
    2026-11-20: frame, seal and Holo are visible from the egg on).  A pet
    without a picture has no card.
    ``cosmetics=False`` (the owner does not support right now) shows the base
    look of the class (``petcards.base_look``): the seal, the foil and a rarer
    frame are left out, the base frame of the class stays.
    A cold Holo / animated card can take seconds - every caller has
    acknowledged its interaction before it gets here, and when it is slow the
    player sees "🐾 Nadchodzi towarzysz..." (``wait``: the caller's
    :func:`waiting.notice` around drawing and sending the card; None: no
    notice).  Never raises for a card problem.
    """
    if source is None:
        return source
    try:
        look = petcards.look_of(pet) if cosmetics else petcards.base_look(pet)
        drawing = petcards.card(source, look, _card_cache(bot), max_bytes=_card_max_bytes(guild))
        card = await (drawing if wait is None else wait.run(drawing))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the card is decoration, the pet is shown in any case
        log.exception("Pet %s: the card of %s could not be composed; showing the plain picture.",
                      pet["pet_id"], source)
        return source
    if card is None or not Path(card).is_file():
        log.warning("Pet %s: the card %s of %s is not a file; showing the plain picture.",
                    pet["pet_id"], card, source)
        return source
    return Path(card)


async def _shown(bot: "AlterBot", guild: Any, player: Any, pet: Any, items: Any, *,
                 wait: waiting.Waiting | None = None) -> tuple[discord.Embed, Path | None]:
    """The pet's embed and the file to send with it (its card, see :func:`_pet_card`).

    ``player``: the pet's owner (a member or a bare id) - a rarer frame, his
    seal and foil are shown only while he is a supporter (:func:`_is_supporter`).
    The embed's image always names the file returned here.  ``wait``: see
    :func:`_pet_card`.
    """
    # Only a pet (or an egg) with something to show needs the answer (a bare id costs a lookup).
    cosmetics = ornaments_of(pet).any and await _is_supporter(bot, player)
    embed, source = _shown_embed(_pid(player), pet, items, cosmetics=cosmetics)
    image = await _pet_card(bot, guild, pet, source, cosmetics=cosmetics, wait=wait)
    if image is not None and image != source:
        embed.set_image(url=f"attachment://{image.name}")
    return embed, image


async def show_pet(scene: "Scene", player: Any, *, buttons: bool = True) -> bool:
    """Show the player's pet and his scrolls; False when he has no pet.

    The pet carries a :class:`PetPanel` for its owner unless ``buttons=False``.
    While a slow card is drawn the channel sees "🐾 Nadchodzi towarzysz..."
    (:mod:`altermmo.core.waiting`), taken away once the pet is posted.
    """
    bot = scene.bot
    player_id = _pid(player)
    owner = await _owner(bot.db, player_id, _SHOW_COLUMNS)
    pet_id = _int(owner["pet_id"]) if owner is not None else 0
    if pet_id <= 0:
        await scene.send(_msg_alone(player_id))
        return False

    # Convert shards to full scrolls (ten for one), in one atomic statement.
    if _int(owner["reroll_scroll_shard"]) >= 10:
        await bot.db.execute(
            """UPDATE PETOWNER SET
                   REROLL_SCROLL = COALESCE(REROLL_SCROLL, 0) + FLOOR(REROLL_SCROLL_SHARD / 10),
                   REROLL_SCROLL_SHARD = MOD(REROLL_SCROLL_SHARD, 10)
               WHERE PLAYER_ID = $1 AND REROLL_SCROLL_SHARD >= 10""", player_id, idempotent=True)

    status, owner, pet = await _load_shown_pet(bot, player_id)
    if status == "alone":  # discarded / stabled in the meantime
        await scene.send(_msg_alone(player_id))
        return False
    if status != _OK:
        await scene.send(MSG_NO_PET_DATA)
        return False

    async with waiting.notice(scene.channel) as wait:
        embed, image = await _shown(bot, getattr(scene.channel, "guild", None), player, pet, owner,
                                    wait=wait)
        files = {} if image is None else {"file_path": image}
        if not buttons:
            await scene.send(embed=embed, **files)
            return True
        panel = PetPanel(player, pet, image=image)
        try:
            await panel.send(scene.channel, embed=embed, **files)
        except BaseException:
            panel.stop()
            raise
    return True


# -- eggs -----------------------------------------------------------------------------

def _roll_egg(player: Any, *, cosmetics: bool = True, frames: bool = True) -> dict[str, Any]:
    """Draw a new egg for ``player``.

    Supporters (patrons, crafters, Twitch subscribers, YouTube supporters) can
    find premium eggs and get the cosmetic rolls (:func:`_roll_cosmetics`):
    once per egg however many of the roles they hold - the seal and the foil
    whatever the class of the egg, also a mythic one, and the frame of its
    class (an ordinary egg has none).  Everybody else's egg is stored with no
    seal, no foil and ``FRAME = ''`` (the base of its class) - nothing is rolled.

    ``cosmetics=False`` (``PETS`` has no columns for them, see
    :func:`cosmetics_available`): nothing is rolled and the egg has no ``SEAL``
    / ``HOLO`` / ``FRAME`` / ``HOLO_PARTIAL`` at all.  ``frames=False`` (only
    the frame columns are missing, :func:`frames_available`): seal and foil are
    rolled as always, the frame is not, and the egg has only ``SEAL`` /
    ``HOLO`` - a partial Holo result has nowhere to go and is lost (logged).

    The cosmetic rolls use ``random.randrange`` and come last, so the legacy
    draws (``randint`` / ``choice``) are exactly what they were.
    """
    supporter = patrons.check_if_patron(player)
    percentage = random.randint(0, 1000)
    mythic = percentage >= MYTHIC_FROM
    if mythic:
        pets_list = MYTHIC_EGGS
    elif supporter:
        pets_list = PATRON_EGGS
    else:
        pets_list = STANDARD_EGGS
    pet = PetType(random.choice(pets_list))

    if pet in MYTHIC_EGGS:
        quality, variant = "Mythic", 0
    elif pet in PREMIUM_TYPES:
        quality, variant = "Premium", random.randint(0, 1)
    else:
        quality, variant = "Standard", random.randint(0, 10)
    egg = {"TYPE": pet.value, "QUALITY": quality, "VARIANT": variant,
           "SHINY": percentage >= SHINY_FROM, "ULTRA_SHINY": percentage >= ULTRA_SHINY_FROM,
           "MYTHIC": mythic}
    if cosmetics:
        tier = petcards.tier_of({"mythic": mythic, "ultra_shiny": egg["ULTRA_SHINY"], "shiny": egg["SHINY"]})
        rolled = _roll_cosmetics(tier, frames=frames) if supporter else Rolled(0, petcards.FOIL_NONE, "")
        egg["SEAL"], egg["HOLO"] = rolled.seal, rolled.foil == petcards.FOIL_FULL
        if frames:
            egg["FRAME"], egg["HOLO_PARTIAL"] = rolled.frame, rolled.foil == petcards.FOIL_PARTIAL
        elif rolled.foil == petcards.FOIL_PARTIAL:
            log.info("PETS has no HOLO_PARTIAL column: the partial Holo of a new %s egg is lost.", pet.value)
    return egg


_SQL_INSERT_EGG = """INSERT INTO PETS (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY,
                         MYTHIC, TYPE, VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC, LOWHP_PERC,
                         SLOW_PERC, INIT_PERC, DETECTION{columns})
                     VALUES ($1, $2, 0, 0, $3, $4, $5, $6, $7, $8, 0, 0, 0, 0, 0, 0, 0, FALSE{values})"""
#: The cosmetic keys of a rolled egg, as the groups of columns that come (and go) together:
#: the frame / partial Holo, then the seal / full Holo.  ``_insert_egg`` tries the richest first.
_EGG_COSMETICS: tuple[tuple[str, ...], ...] = (("FRAME", "HOLO_PARTIAL"), ("SEAL", "HOLO"))


def _egg_value(egg: dict[str, Any], key: str) -> Any:
    """The value an egg's cosmetic key is stored with (its column's type)."""
    if key == "SEAL":
        return int(egg.get(key, 0))
    if key == "FRAME":
        return str(egg.get(key, "") or "")
    return bool(egg.get(key, False))


async def _insert_egg(con: Any, egg: dict[str, Any]) -> int:
    """Store a rolled egg under the next free PET_ID; returns that id.

    An egg rolled with cosmetics is stored with every cosmetic key it has
    (``SEAL`` / ``HOLO``, and ``FRAME`` / ``HOLO_PARTIAL`` when the frames are
    on).  Should columns be missing after all (they were there when the bot
    looked), the egg is stored with what the table can keep - without the
    frame columns, then the old way - and the lost keys are taken out of
    ``egg``: the player gets his pet, only those cosmetics are lost.
    """
    await con.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", _LOCK_PET_IDS)
    pet_id = _int(await con.fetchval("SELECT COALESCE(MAX(PET_ID), 0) + 1 FROM PETS"))
    args = (pet_id, DEFAULT_NAME, egg["QUALITY"], egg["SHINY"], egg["ULTRA_SHINY"], egg["MYTHIC"],
            egg["TYPE"], str(egg["VARIANT"]))
    for index, group in enumerate(_EGG_COSMETICS):
        if group[0] not in egg:
            continue
        keys = [key for later in _EGG_COSMETICS[index:] for key in later if later[0] in egg]
        columns = "".join(f", {key}" for key in keys)
        values = "".join(f", ${number}" for number in range(len(args) + 1, len(args) + len(keys) + 1))
        try:
            # A savepoint: a refused INSERT must not spoil the caller's transaction.
            async with con.transaction():
                await con.execute(_SQL_INSERT_EGG.format(columns=columns, values=values),
                                  *args, *(_egg_value(egg, key) for key in keys))
            break
        except asyncpg.UndefinedColumnError:
            lost = {key: egg.pop(key, None) for key in group}
            log.info("PETS has no %s columns: these cosmetics of pet egg %s are lost (%s).",
                     " / ".join(group), pet_id, lost)
    else:
        await con.execute(_SQL_INSERT_EGG.format(columns="", values=""), *args)
    log.info("Pet egg %s generated: %s", pet_id, egg)
    return pet_id


def _rare_finds(egg: dict[str, Any]) -> list[str]:
    """What of a stored egg the owner wants to hear about: the best seal, full Holo, a rare frame."""
    found = []
    seal = int(egg.get("SEAL", 0) or 0)
    if seal >= MAX_SEAL:
        found.append(MSG_RARE_SEAL.format(name=petcards.SEAL_NAMES.get(seal, seal)))
    if egg.get("HOLO"):
        found.append(MSG_RARE_HOLO)
    frame = _rare_frame(str(egg.get("FRAME", "") or ""))
    if frame is not None:
        found.append(MSG_RARE_FRAME.format(name=frame.label))
    return found


def _and(parts: Sequence[str]) -> str:
    """``a``, ``a i b``, ``a, b i c``."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " i " + parts[-1]


async def _egg_stored(bot: "AlterBot", player_id: int, pet_id: int, egg: dict[str, Any],
                      cosmetics: bool, frames: bool = False) -> None:
    """After a new egg is saved: remember lost columns, tell the owner about rare cosmetics.

    The best seal, full Holo, an animated frame and the rarest static frame of a
    pool (:data:`RARE_FRAME_PERCENT`) are rare enough for the owner to want to
    know - in ONE message per egg; the player sees them on his egg
    (``$towarzysz``), so nothing is announced.  A partial Holo (1%) is not
    reported.  Never raises.
    """
    if frames and "FRAME" not in egg:
        bot._pet_frames = False  # type: ignore[attr-defined]  # see _insert_egg: the columns are gone
    if cosmetics and "SEAL" not in egg:
        bot._pet_cosmetics = bot._pet_frames = False  # type: ignore[attr-defined]
        return
    found = _rare_finds(egg)
    if not found:
        return
    log.info("Pet egg %s of player %s has rare cosmetics: seal %s, holo %s, frame %r.", pet_id, player_id,
             egg.get("SEAL"), egg.get("HOLO"), egg.get("FRAME", ""))
    await bot.log_to_owner(MSG_RARE_COSMETICS.format(player_id=player_id, pet_id=pet_id, found=_and(found)))


async def _cosmetic_state(bot: "AlterBot") -> tuple[bool, bool]:
    """``(cosmetics_available, frames_available)`` - what a new egg can be stored with."""
    cosmetics = await cosmetics_available(bot)
    return cosmetics, cosmetics and await frames_available(bot)


async def _announce_mythic(scene: "Scene", player_id: int) -> None:
    """Tell everybody about a mythic egg; the egg stays even when Discord fails."""
    bot = scene.bot
    text = f"Łowca <@{player_id}> zdobył mitycznego towarzysza! <:POGGERS:936907543849078844>"
    if not bot.debug:
        text = f"<@&{bot.ids.hunters_role}>! " + text
    try:
        await scene.send(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Mythic pet of %s could not be announced: %s: %s", player_id, type(exc).__name__, exc)
        await bot.log_to_owner(f"Nie udało się ogłosić na kanale: {text}")


async def generate_pet_egg(scene: "Scene", player: Any = None) -> int:
    """Generate first pet as an egg (for nobody yet); returns its PET_ID.

    Without ``player`` the egg is drawn as for the owner of the server (legacy
    ``$GenerateEgg``).
    """
    bot = scene.bot
    if player is None:
        player = await bot.member(bot.ids.owner)
    player_id = _pid(player) if player is not None else bot.ids.owner
    cosmetics, frames = await _cosmetic_state(bot)
    egg = _roll_egg(player, cosmetics=cosmetics, frames=frames)
    async with bot.db.transaction() as con:
        pet_id = await _insert_egg(con, egg)
    await _egg_stored(bot, player_id, pet_id, egg, cosmetics, frames)
    if egg["MYTHIC"]:
        await _announce_mythic(scene, player_id)
    return pet_id


async def assign_pet(scene: "Scene", player: Any) -> bool:
    """Give the player a new egg; False when he already has a pet."""
    bot = scene.bot
    player_id = _pid(player)
    cosmetics, frames = await _cosmetic_state(bot)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        owner = await _owner(con, player_id, "PET_ID")
        if owner is not None and _int(owner["pet_id"]) > 0:
            log.debug("Player %s already has a pet; no egg assigned.", player_id)
            return False
        egg = _roll_egg(player, cosmetics=cosmetics, frames=frames)
        pet_id = await _insert_egg(con, egg)
        if owner is not None:
            await con.execute("UPDATE PETOWNER SET PET_ID = $2 WHERE PLAYER_ID = $1", player_id, pet_id)
        else:
            await _insert_owner(con, player_id, pet_id, True, 0, 0, 0, 0, 0)
    log.info("Pet %s assigned to player %s.", pet_id, player_id)
    await _egg_stored(bot, player_id, pet_id, egg, cosmetics, frames)
    if egg["MYTHIC"]:
        await _announce_mythic(scene, player_id)
    await achievements.check_pets(scene, player)  # a mythic pet (with Holo / the rarest seal); never raises
    return True


#: The row lock every change of a pet takes (as ``_active_pet(lock=True)``), but naming its
#: columns: right after the columns were added by hand a prepared "SELECT p.*" of this
#: connection would be refused once, in the middle of the transaction.
_SQL_LOCK_ACTIVE_PET = """SELECT p.PET_ID, p.MYTHIC, p.ULTRA_SHINY, p.SHINY
                          FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
                          WHERE o.PLAYER_ID = $1 AND o.PET_ID > 0 LIMIT 1 FOR UPDATE OF p"""


def _foil_value(holo: Any) -> int | None:
    """``holo`` of :func:`set_cosmetics` as a ``petcards.FOIL_*`` (None: leave the foil as it is)."""
    if holo is None:
        return None
    if holo is True or holo is False:
        return petcards.FOIL_FULL if holo else petcards.FOIL_NONE
    try:
        foil = int(holo)
    except (TypeError, ValueError):
        raise ValueError(holo) from None
    if foil not in (petcards.FOIL_NONE, petcards.FOIL_PARTIAL, petcards.FOIL_FULL):
        raise ValueError(holo)
    return foil


async def set_cosmetics(bot: "AlterBot", player_id: Any, seal: Any,
                        holo: bool | int | None = None) -> tuple[str, dict[str, Any] | None]:
    """Set the seal (and the foil, unless ``holo`` is None) of the player's active pet (``$DajPieczec``).

    The only way cosmetics change after the egg was drawn (with :func:`set_frame`):
    for the owner, to look at the cards and to put a mistake right.  ``holo``:
    True / False as always (full Holo / no foil) or a ``petcards.FOIL_*``; the
    foil is one of the three, so setting one clears the other flag.  Returns
    ``(status, what the pet has now)``: ``bad_seal`` (not 0..:data:`MAX_SEAL`),
    ``bad_foil``, ``no_columns`` (cosmetics are off, see
    :func:`cosmetics_available` - looked up again), ``no_frame_columns`` (a
    partial Holo was asked for and ``PETS`` cannot keep it, see
    :func:`frames_available`), ``alone`` (no active pet) or ``_OK`` with
    ``pet_id`` / ``seal`` / ``holo`` (full Holo) / ``foil`` (``FOIL_*``).
    """
    player_id = _pid(player_id)
    try:
        seal = int(seal)
    except (TypeError, ValueError):
        return "bad_seal", None
    if not 0 <= seal <= MAX_SEAL:
        return "bad_seal", None
    try:
        foil = _foil_value(holo)
    except ValueError:
        return "bad_foil", None
    if not await cosmetics_available(bot, refresh=True):
        return "no_columns", None
    frames = await frames_available(bot, refresh=True)
    if foil == petcards.FOIL_PARTIAL and not frames:
        return "no_frame_columns", None
    full = None if foil is None else foil == petcards.FOIL_FULL
    partial = None if foil is None else foil == petcards.FOIL_PARTIAL
    async with bot.db.transaction() as con:
        pet = await con.fetchrow(_SQL_LOCK_ACTIVE_PET, player_id)
        if pet is None:
            return "alone", None
        if frames:
            row = await con.fetchrow(
                """UPDATE PETS SET SEAL = $2, HOLO = COALESCE($3::boolean, HOLO),
                       HOLO_PARTIAL = COALESCE($4::boolean, HOLO_PARTIAL) WHERE PET_ID = $1
                   RETURNING PET_ID, SEAL, HOLO, HOLO_PARTIAL""", pet["pet_id"], seal, full, partial)
        else:  # the table of 2026-11-20: seals and full Holo only
            row = await con.fetchrow(
                """UPDATE PETS SET SEAL = $2, HOLO = COALESCE($3::boolean, HOLO) WHERE PET_ID = $1
                   RETURNING PET_ID, SEAL, HOLO""", pet["pet_id"], seal, full)
    now = {"pet_id": _int(row["pet_id"]), "seal": _int(row["seal"]), "holo": row["holo"] is True}
    now["foil"] = ornaments_of(row).foil
    log.info("Cosmetics of pet %s (player %s) set by an administrator: %s", now["pet_id"], player_id, now)
    return _OK, now


#: What ``$DajRamke`` takes for "the base frame of the class" (stored as '').
BASE_FRAME_WORDS = frozenset({"baza", "bazowa", "podstawowa", "base", "brak", "0", "-"})


async def set_frame(bot: "AlterBot", player_id: Any, frame: Any) -> tuple[str, dict[str, Any] | None]:
    """Set the frame result of the player's active pet (``$DajRamke``).

    ``frame``: a result id of the pet's own class (``S1``, ``s1-a``, the base id
    itself) or one of :data:`BASE_FRAME_WORDS` - the base is stored as ''.
    Returns ``(status, data)``: ``no_frame_columns`` (see
    :func:`frames_available`, looked up again), ``alone`` (no active pet),
    ``no_tier`` (an ordinary pet: no frame at all), ``bad_frame`` with
    ``tier`` (the id is not a result of the pet's class: the caller lists
    ``petcards.POOLS[tier]``) or ``_OK`` with ``pet_id`` / ``tier`` / ``frame``
    (the stored id, '' = base).
    """
    player_id = _pid(player_id)
    wanted = str(frame or "").strip()
    if not await frames_available(bot, refresh=True):
        return "no_frame_columns", None
    async with bot.db.transaction() as con:
        pet = await con.fetchrow(_SQL_LOCK_ACTIVE_PET, player_id)
        if pet is None:
            return "alone", None
        tier = petcards.tier_of(pet)
        if not tier:
            return "no_tier", {"pet_id": _int(pet["pet_id"]), "tier": tier}
        if wanted.lower() in BASE_FRAME_WORDS:
            stored = ""
        else:
            found = petcards.FRAMES.get(wanted.upper())
            if found is None or found.tier != tier:
                return "bad_frame", {"pet_id": _int(pet["pet_id"]), "tier": tier}
            stored = "" if found.base else found.id
        row = await con.fetchrow("UPDATE PETS SET FRAME = $2 WHERE PET_ID = $1 RETURNING PET_ID, FRAME",
                                 pet["pet_id"], stored)
    now = {"pet_id": _int(row["pet_id"]), "tier": tier, "frame": str(row["frame"] or "")}
    log.info("Frame of pet %s (player %s) set by an administrator: %s", now["pet_id"], player_id, now)
    return _OK, now


async def reassign_pet(bot: "AlterBot", pet_id: Any, player_id: Any) -> bool:
    """Give an existing pet to a player without an active pet; True when it was done.

    The pet comes as it is - with its frame, seal and foil: they belong to the
    specimen, not to its owner, so nothing is rolled here.
    """
    pet_id, player_id = int(pet_id), _pid(player_id)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        if await _pet(con, pet_id) is None:
            log.info("Pet %s does not exist; nothing reassigned.", pet_id)
            return False
        owner = await _owner(con, player_id, "PET_ID")
        if owner is None:
            await _insert_owner(con, player_id, pet_id, True, 0, 0, 0, 0, 0)
        elif _int(owner["pet_id"]) > 0:
            log.info("Player %s already has a pet; pet %s not reassigned.", player_id, pet_id)
            return False
        else:
            await con.execute("UPDATE PETOWNER SET PET_ID = $2 WHERE PLAYER_ID = $1", player_id, pet_id)
    log.info("Pet %s reassigned to player %s.", pet_id, player_id)
    return True


# -- pet items ------------------------------------------------------------------------

_ITEM_COLUMNS: tuple[str, ...] = ("REROLL_SCROLL", "REROLL_SCROLL_SHARD", "REBIRTH_STONES", "MIRRORS")


async def _add_item(bot: "AlterBot", column: str, number: Any, player_id: Any) -> bool:
    """Add ``number`` to one item counter of the player (creating his row when needed)."""
    player_id, number = _pid(player_id), int(number)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        tag = await con.execute(
            f"UPDATE PETOWNER SET {column} = COALESCE({column}, 0) + $2 WHERE PLAYER_ID = $1",
            player_id, number)
        if tag == "UPDATE 0":
            await _insert_owner(con, player_id, 0, False,
                                *(number if name == column else 0 for name in _ITEM_COLUMNS), 0)
    return True


async def assign_shard(scene: "Scene", number: Any, player_id: Any) -> bool:
    """Assigning scroll shard to the player in database."""
    return await _add_item(scene.bot, "REROLL_SCROLL_SHARD", number, player_id)


async def assign_scroll(scene: "Scene", number: Any, player_id: Any) -> bool:
    """Assigning full scroll to the player in database."""
    return await _add_item(scene.bot, "REROLL_SCROLL", number, player_id)


async def assign_rebirth_stone(scene: "Scene", number: Any, player_id: Any) -> bool:
    """Assigning rebirth stone to the player in database."""
    return await _add_item(scene.bot, "REBIRTH_STONES", number, player_id)


async def assign_mirror(scene: "Scene", number: Any, player_id: Any) -> bool:
    """Assigning mirror to the player in database."""
    return await _add_item(scene.bot, "MIRRORS", number, player_id)


# -- growing --------------------------------------------------------------------------

async def level_up_pet(scene: "Scene", player_id: Any) -> bool:
    """Level up the player's pet (an egg hatches); False without a pet or at the top level."""
    player_id = _pid(player_id)
    async with scene.bot.db.transaction() as con:
        pet = await _active_pet(con, player_id, lock=True)
        if pet is None:
            return False
        level = _int(pet["pet_lvl"])
        if level >= MAX_LEVEL:
            return False
        variant = _int(pet["variant"])
        if level == 0:  # an egg has one of 11 looks, a hatched pet one of 2
            variant %= 2
        add_skill = 2 if pet["mythic"] else random.randint(1, 2)
        stats = _stats_of(pet)
        for _ in range(add_skill):
            _roll_skill(stats, SKILL_POOL, pet)
        await _save_pet(con, pet, stats, level=level + 1,
                        skills=_int(pet["pet_skills"]) + add_skill, variant=variant)
    log.info("Pet %s of player %s reached level %s (+%s talent).", pet["pet_id"], player_id, level + 1, add_skill)
    return True


async def get_pet_skills(bot: "AlterBot", player_id: Any) -> dict[str, Any]:
    """Stats of the player's active pet for a fight (zeros when he has none).

    Always returns every key of :data:`NO_PET_SKILLS` with plain ``int`` /
    ``float`` values (``DETECTION`` is a bool).
    """
    skills = dict(NO_PET_SKILLS)
    pet = await _active_pet(bot.db, _pid(player_id))
    if pet is not None:
        for name in skills:
            skills[name] = bool(pet["detection"]) if name == "DETECTION" else _num(pet[name.lower()])
    return skills


# -- changes the player has to confirm ------------------------------------------------

#: Players the bot is waiting for (one ``$potwierdzam`` question at a time).
_awaiting_confirmation: set[int] = set()


@contextlib.contextmanager
def _confirmation_slot(player_id: int) -> Iterator[bool]:
    """Yield True when the player has no open question; released whatever happens."""
    if player_id in _awaiting_confirmation:
        yield False
        return
    _awaiting_confirmation.add(player_id)
    try:
        yield True
    finally:
        _awaiting_confirmation.discard(player_id)


async def _confirm(scene: "Scene", player: Any, prompt: str, **send_kwargs: Any) -> ui.Confirmation:
    """Ask ``prompt`` with Potwierdzam / Anuluj buttons; 15 s for a click or a typed ``$potwierdzam``.

    The bot listens for the typed answer before the question becomes visible
    (:func:`ui.confirm`); only ``player`` can answer, only on this channel.
    The result is falsy when the player cancelled or did not answer.
    ``send_kwargs`` (e.g. ``allowed_mentions=``) go with the question.
    """
    return await ui.confirm(scene.bot, scene.channel, player, prompt,
                            timeout=timing.timeout(CONFIRM_TIMEOUT), typed=CONFIRM_WORD, **send_kwargs)


async def _thank(answer: ui.Confirmation, emoji: str) -> None:
    """The legacy "seen" reaction: under a typed answer, or under the question after a click."""
    await io.safe_react(answer.typed if answer.typed is not None else answer.message, emoji)


def _other_pet(active_pet_id: int, expected_pet: int | None) -> bool:
    """True when a button under ``expected_pet`` would act on another (active) pet.

    ``expected_pet`` None: a typed command, which always means the active pet.
    Without an active pet the flows answer "you are alone" themselves.
    """
    return expected_pet is not None and active_pet_id > 0 and active_pet_id != expected_pet


async def _confirmed_change(scene: "Scene", player: Any, *, prompt: str, messages: dict[str, str],
                            check: Callable[..., Awaitable[tuple[str, Any]]],
                            apply: Callable[[Any, Any], Awaitable[None]]) -> bool:
    """Common flow of ``$odrodzenie``, ``$oswiecenie`` and ``$transformacja``.

    ``check(q, lock)`` says whether the change is possible (``_OK``) or which of
    ``messages`` explains why not.  It runs before the question and once more,
    with the rows locked, in the transaction that applies the change - whatever
    happened during the 15 seconds, nothing is paid for twice or with items
    that are gone.  The status ``other_pet`` (see :func:`_other_pet`) is
    answered with :data:`MSG_OTHER_PET`.
    """
    bot = scene.bot
    player_id = _pid(player)
    messages = {"other_pet": MSG_OTHER_PET, **messages}
    status, _ = await check(bot.db, False)
    if status != _OK:
        await scene.send(messages[status])
        return False
    with _confirmation_slot(player_id) as free:
        if not free:
            await scene.send(MSG_ALREADY_WAITING)
            return False
        answer = await _confirm(scene, player, prompt)
        if not answer:
            await scene.send(MSG_UNSURE)
            return False
        await _thank(answer, "<:PepoG:790963160528977980>")
        async with bot.db.transaction() as con:
            status, data = await check(con, True)
            if status == _OK:
                await apply(con, data)
    if status != _OK:
        await scene.send(messages[status])
        return False
    await show_pet(scene, player)
    return True


async def reroll_pet(scene: "Scene", player: Any, remove_list: Sequence[int] = (), *,
                     expected_pet: int | None = None) -> bool:
    """Reroll pet stats for a scroll; ``remove_list`` (indexes of SKILL_POOL) costs 10 each.

    ``expected_pet``: the pet a panel button was clicked under; nothing happens
    when another pet is active (also checked after the question).
    """
    player_id = _pid(player)
    removed = {int(index) for index in remove_list if 0 <= int(index) < len(SKILL_POOL)}
    cost = 10 * len(removed) if removed else 1
    pool = [name for index, name in enumerate(SKILL_POOL) if index not in removed]
    mention = "<@" + str(player_id) + ">"

    async def check(q: Any, lock: bool) -> tuple[str, Any]:
        owner = await _owner(q, player_id, "PET_ID, REROLL_SCROLL", lock=lock)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id <= 0:
            return "alone", None
        if _other_pet(pet_id, expected_pet):
            return "other_pet", None
        if _int(owner["reroll_scroll"]) < cost:
            return "poor", None
        pet = await _pet(q, pet_id, lock=lock)
        return (_OK, pet) if pet is not None else ("no_data", None)

    async def apply(con: Any, pet: Any) -> None:
        stats: dict[str, Any] = dict.fromkeys(SKILL_POOL, 0)
        stats["DETECTION"] = False
        for _ in range(_int(pet["pet_skills"])):
            if _roll_skill(stats, pool, pet) is None:
                break
        await _save_pet(con, pet, stats)
        await con.execute("UPDATE PETOWNER SET REROLL_SCROLL = REROLL_SCROLL - $2 WHERE PLAYER_ID = $1",
                          player_id, cost)
        log.info("Pet %s of player %s rerolled for %s scroll(s): %s", pet["pet_id"], player_id, cost, stats)

    return await _confirmed_change(
        scene, player, check=check, apply=apply,
        prompt=("Czy jesteś pewien, że chcesz przelosować statystyki swojego towarzysza " + mention
                + "? <:Hmm:984767035617730620> " + CONFIRM_HINT),
        messages={
            "alone": _msg_alone(player_id),
            "no_data": MSG_NO_PET_DATA,
            "poor": ("Nie masz wystarczająco zwojów odrodzenia " + mention + " <:Sadge:936907659142111273> "
                     "Wpisz **$towarzysz**, żeby sprawdzić ich ilość. Zwoje możesz zdobyć na polowaniu "
                     "lub po zabiciu bossów."),
        })


class SkillMenu(ui.ClickView):
    """The ``$mocneodrodzenie`` menu: a multi-choice list of SKILL_POOL, Przelosuj and Anuluj.

    Only ``player`` can use it (dead hunters neither).  A change of the list
    arrives as a click with the key :attr:`SKIP` whose interaction carries the
    chosen values; the list shows the current choice as its defaults.
    """

    SKIP = "pet:skip"

    def __init__(self, player: Any) -> None:
        super().__init__([ui.Choice("pet:skip-accept", "Przelosuj", emoji="🎲",
                                    style=discord.ButtonStyle.success, row=1),
                          ui.Choice("pet:skip-cancel", "Anuluj", emoji="✖️", row=1)], only=[player])
        self.select: discord.ui.Select = discord.ui.Select(
            custom_id=ui.FLOW_PREFIX + self.SKIP, placeholder="Umiejętności do pominięcia (0-3)…",
            min_values=0, max_values=MAX_SKIPPED, options=self._options(()), row=0)
        self.select.callback = functools.partial(self._clicked, self.SKIP)  # type: ignore[method-assign]
        self.add_item(self.select)

    @staticmethod
    def _options(selected: Sequence[int]) -> list[discord.SelectOption]:
        return [discord.SelectOption(label=label, value=str(index), emoji=SKILL_EMOTES[index],
                                     default=index in selected)
                for index, label in enumerate(SKILL_LABELS)]

    def show(self, selected: Sequence[int]) -> None:
        """Make the list show ``selected`` (call :meth:`refresh` afterwards)."""
        self.select.options = self._options(selected)

    @staticmethod
    def chosen(click: ui.Click) -> list[int]:
        """The stats a list change chose (indexes of SKILL_POOL, unknown values dropped)."""
        values = (getattr(click.interaction, "data", None) or {}).get("values") or []
        chosen: set[int] = set()
        for value in values:
            with contextlib.suppress(TypeError, ValueError):
                if 0 <= int(value) < len(SKILL_POOL):
                    chosen.add(int(value))
        return sorted(chosen)


async def adv_reroll_pet(scene: "Scene", *, expected_pet: int | None = None) -> bool:
    """Advanced reroll: the player first picks (a list) up to 3 stats to leave out.

    Every change of the list restarts the 20 s; the menu as a whole is limited
    too (it keeps the channel busy, so endless clicking must not hold it for
    ever).  Then the ordinary reroll question follows (:func:`reroll_pet`,
    which also gets ``expected_pet``).
    """
    user = scene.author
    selected: list[int] = []
    menu_deadline = time.monotonic() + timing.timeout(MENU_TOTAL_TIMEOUT)
    async with SkillMenu(user) as menu:
        menu_msg = await menu.send(scene.channel, adv_reroll_text(selected))
        while True:
            wait = min(timing.timeout(MENU_TIMEOUT), menu_deadline - time.monotonic())
            click = await menu.next(wait)
            if click is None or click.key == "pet:skip-cancel":
                await menu.close()
                await io.safe_delete(menu_msg)
                await scene.send(MSG_UNSURE)
                return False
            if click.key == "pet:skip-accept":
                break
            selected = SkillMenu.chosen(click)
            menu.show(selected)
            await menu.refresh(content=adv_reroll_text(selected))
        await menu.close(content=adv_reroll_text(selected))
        log.info("Advanced reroll of %s: leaving out %s.", _pid(user), [SKILL_POOL[i] for i in selected])

    await scene.send("Zobaczmy...")
    if len(selected) > MAX_SKIPPED:
        await scene.send(MSG_TOO_MANY_SKILLS)
        return False
    return await reroll_pet(scene, user, selected, expected_pet=expected_pet)


async def transform_pet(scene: "Scene", player: Any, *, expected_pet: int | None = None) -> bool:
    """Transform pet appearance for a mirror (supporters only: patrons, crafters,
    Twitch subscribers, YouTube supporters).

    Only the looks change (``TYPE`` / ``QUALITY`` / ``VARIANT``): the class
    (``SHINY`` / ``ULTRA_SHINY``), and with it the frame, the seal and the foil
    belong to the specimen and stay.  ``expected_pet``: as in :func:`reroll_pet`.
    """
    player_id = _pid(player)
    mention = "<@" + str(player_id) + ">"

    async def check(q: Any, lock: bool) -> tuple[str, Any]:
        owner = await _owner(q, player_id, "PET_ID, MIRRORS", lock=lock)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id <= 0:
            return "alone", None
        if _other_pet(pet_id, expected_pet):
            return "other_pet", None
        pet = await _pet(q, pet_id, lock=lock)
        if pet is None:
            return "no_data", None
        if pet["mythic"]:
            return "mythic", None
        if not patrons.check_if_patron(player):
            return "no_rank", None
        if _int(owner["mirrors"]) <= 0:
            return "poor", None
        return _OK, pet

    async def apply(con: Any, pet: Any) -> None:
        pet_type = PetType(random.choice(TRANSFORM_TYPES))
        quality = "Premium" if pet_type in PREMIUM_TYPES else "Standard"
        variant = random.randint(0, 1)
        await con.execute("UPDATE PETS SET VARIANT = $2, QUALITY = $3, TYPE = $4 WHERE PET_ID = $1",
                          pet["pet_id"], str(variant), quality, pet_type.value)
        await con.execute("UPDATE PETOWNER SET MIRRORS = MIRRORS - 1 WHERE PLAYER_ID = $1", player_id)
        log.info("Pet %s of player %s transformed into %s/%s.", pet["pet_id"], player_id,
                 pet_type.value, variant)

    return await _confirmed_change(
        scene, player, check=check, apply=apply,
        prompt=("Czy jesteś pewien, że chcesz zmienić wygląd swojego towarzysza " + mention
                + "? <:Hmm:984767035617730620> " + CONFIRM_HINT),
        messages={
            "alone": _msg_alone(player_id),
            "no_data": MSG_NO_PET_DATA,
            "mythic": "Mitycznego towarzysza nie można transformować, " + mention + "!",
            "no_rank": ("Musisz mieć rangę Patrona, Craftera, subskrybenta Twitch lub wspierającego na "
                        "YouTube " + mention + ", żeby przetransformować "
                        "towarzysza <:Sadge:936907659142111273> Rangi do zdobycia możesz zobaczyć na tym "
                        "kanale <#688296443156365354>."),
            "poor": ("Nie masz żadnych lustr wizualiów " + mention + " <:Sadge:936907659142111273> "
                     "Wpisz **$towarzysz**, żeby sprawdzić ich ilość. Lustra możesz zdobyć na polowaniu "
                     "lub po zabiciu bossów, jednak są one bardzo rzadkie."),
        })


async def enlight_pet(scene: "Scene", player: Any, *, expected_pet: int | None = None) -> bool:
    """Enlighting pet (1-3 more talent points for a level 3 pet) for a rebirth stone.

    ``expected_pet``: as in :func:`reroll_pet`.
    """
    player_id = _pid(player)
    mention = "<@" + str(player_id) + ">"
    unworthy = ("Niestety Twój towarzysz nie jest godny dostąpić oświecenia " + mention
                + " <:Sadge:936907659142111273> Wpisz **$towarzysz**, żeby sprawdzić jego poziom.")

    async def check(q: Any, lock: bool) -> tuple[str, Any]:
        owner = await _owner(q, player_id, "PET_ID, REBIRTH_STONES", lock=lock)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id <= 0:
            return "alone", None
        if _other_pet(pet_id, expected_pet):
            return "other_pet", None
        if _int(owner["rebirth_stones"]) <= 0:
            return "poor", None
        pet = await _pet(q, pet_id, lock=lock)
        if pet is None:
            return "no_data", None
        level, skills = _int(pet["pet_lvl"]), _int(pet["pet_skills"])
        if skills <= ENLIGHT_MAX_SKILLS and level == MAX_LEVEL:
            return _OK, pet
        if level < MAX_LEVEL:
            return "unworthy", None
        return ("enlightened" if skills > ENLIGHT_MAX_SKILLS else "unworthy"), None

    async def apply(con: Any, pet: Any) -> None:
        add_skill = random.randint(1, 3)
        stats = _stats_of(pet)
        for _ in range(add_skill):
            _roll_skill(stats, SKILL_POOL, pet)
        await _save_pet(con, pet, stats, skills=_int(pet["pet_skills"]) + add_skill)
        await con.execute("UPDATE PETOWNER SET REBIRTH_STONES = REBIRTH_STONES - 1 WHERE PLAYER_ID = $1",
                          player_id)
        log.info("Pet %s of player %s enlightened (+%s talent).", pet["pet_id"], player_id, add_skill)

    return await _confirmed_change(
        scene, player, check=check, apply=apply,
        prompt=("Czy jesteś pewien, że chcesz zwiększyć talent swojego towarzysza " + mention
                + "? <:Hmm:984767035617730620> " + CONFIRM_HINT),
        messages={
            "alone": _msg_alone(player_id),
            "no_data": MSG_NO_PET_DATA,
            "unworthy": unworthy,
            "enlightened": ("Twój towarzysz już dostąpił oświecenia " + mention
                            + " <:prayge:1063891597760139304> Wpisz **$towarzysz**, żeby sprawdzić jego talent."),
            "poor": ("Nie masz żadnych kamieni olśnienia " + mention + " <:Sadge:936907659142111273> "
                     "Wpisz **$towarzysz**, żeby sprawdzić ich ilość. Kamienie olśnienia możesz zdobyć na "
                     "polowaniu lub po zabiciu bossów, jednak są one bardzo rzadkie."),
        })


async def discard_pet(scene: "Scene", *, expected_pet: int | None = None) -> bool:
    """Discarding pet from the author if it is possible (a sacrifice: a random boss modifier).

    ``expected_pet``: the pet a panel button was clicked under; when another
    pet is active (before or after the question) nothing is discarded.

    A supporter's pet is kept in his album (:mod:`altermmo.features.album`,
    owner's decision of 2026-11-21): the question and the answer say so, and
    the album's row is written in the SAME transaction that deletes the pet -
    it is in ``PETS`` or in the album, never both, never neither.  When the
    album cannot be written nothing is sacrificed (the player and the owner
    are told).  Whether he supports is asked again at the sacrifice itself.
    Once the sacrifice is committed the player is told the pet was kept and
    "Pełny album" is checked even when the boss modifier step fails (its error
    still reaches the caller).
    Everybody else's sacrifice is exactly what it always was.
    """
    from . import album  # (lazily: the album builds on this module)

    bot = scene.bot
    player = scene.author
    player_id = _pid(player)
    kept: int | None = None
    with _confirmation_slot(player_id) as free:
        if not free:
            await scene.send(MSG_ALREADY_WAITING)
            return False
        owner = await _owner(bot.db, player_id, "PET_ID")
        if owner is None or _int(owner["pet_id"]) <= 0:
            await scene.send(MSG_ALONE_MAD)
            return False
        if _other_pet(_int(owner["pet_id"]), expected_pet):
            await scene.send(MSG_OTHER_PET)
            return False
        answer = await _confirm(
            scene, player,
            "Czy jesteś pewien, że chcesz porzucić swojego towarzysza <@" + str(player_id)
            + ">? <:MonkaS:882181709100097587> "
            + (album.MSG_DISCARD_KEPT if patrons.check_if_patron(player) else "") + CONFIRM_HINT)
        if not answer:
            await scene.send("*Twój towarzysz oddycha z ulgą...*")
            return False
        keeps = patrons.check_if_patron(player)  # the status at the moment of the sacrifice
        # The pet that is active *now* goes, together with its row in PETS (into the album).
        try:
            async with bot.db.transaction() as con:
                owner = await _owner(con, player_id, "PET_ID", lock=True)
                pet_id = _int(owner["pet_id"]) if owner is not None else 0
                other = _other_pet(pet_id, expected_pet)
                if pet_id > 0 and not other:
                    pet = await _pet(con, pet_id, lock=True) if keeps else None
                    if pet is not None:
                        kept = await album.keep(con, player_id, pet)  # NotSaved: everything is rolled back
                    await con.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", player_id)
                    await con.execute("DELETE FROM PETS WHERE PET_ID = $1", pet_id)
        except album.NotSaved as exc:
            log.error("Player %s: pet %s not sacrificed, the album could not be written (%s).",
                      player_id, pet_id, exc)
            await bot.log_to_owner(album.LOG_NOT_SAVED.format(pet_id=pet_id, player_id=player_id,
                                                              error=str(exc)[:300]))
            await scene.send(album.MSG_NOT_SAVED.format(player_id=player_id))
            return False
        if other:
            log.info("Player %s: discard of pet %s refused, pet %s is active now.", player_id,
                     expected_pet, pet_id)
            await scene.send(MSG_OTHER_PET)
            return False
        if pet_id <= 0:
            await scene.send(MSG_ALONE_MAD)
            return False
        log.info("Player %s discarded pet %s%s.", player_id, pet_id,
                 "" if kept is None else f" (kept in the album as entry {kept})")
        try:
            await modifiers.random_modifiers(scene, True, True)
            await _thank(answer, "<:MonkaS:882181709100097587>")
        finally:
            # Committed: the pet IS in the album, whatever the modifier step does (its error,
            # e.g. Discord down, still goes on to the caller).
            if kept is not None:
                await album.saved(scene, player)  # the answer and the album's achievement; never raises
    return True


async def name_pet(scene: "Scene", name: str, *, expected_pet: int | None = None) -> bool:
    """Set name of the author's pet (15% chance that it turns shiny); False without a pet.

    ``expected_pet``: the pet a panel was showing; False (nothing named) when
    another pet is active.
    """
    bot = scene.bot
    player_id = _pid(scene.author)
    name = str(name)[:MAX_NAME_LENGTH]
    owner = await _owner(bot.db, player_id, "PET_ID")
    pet_id = _int(owner["pet_id"]) if owner is not None else 0
    if pet_id <= 0:
        await scene.send(MSG_ALONE_MAD)
        return False
    if _other_pet(pet_id, expected_pet):
        await scene.send(MSG_OTHER_PET)
        return False

    percentage = random.randint(0, 100)
    if percentage >= 85:
        await bot.db.execute("UPDATE PETS SET PET_NAME = $2, SHINY = TRUE WHERE PET_ID = $1",
                             pet_id, name, idempotent=True)
        try:
            await scene.send("Towarzysz wygląda na bardzo wdzięcznego i dostrzegasz" +
                             ", że wygląda jakoś inaczej <:Susge:973591024322633858>")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the pet is named and shiny, whatever Discord says
            log.warning("Pet %s turned shiny but the player could not be told: %s: %s",
                        pet_id, type(exc).__name__, exc)
    else:
        await bot.db.execute("UPDATE PETS SET PET_NAME = $2 WHERE PET_ID = $1",
                             pet_id, name, idempotent=True)
    await io.safe_react(scene.message, "<:peepoBlush:984769061340737586>")
    return True


# -- the stable -----------------------------------------------------------------------

_STABLE_COLUMNS = "PET_ID, PET_ID_ALT1, PET_ID_ALT2, PET_ID_ALT3, PET_ID_ALT4"


async def store_pet(scene: "Scene", slot: Any) -> bool:
    """Put the author's pet into stable slot 1-4 (a pet kept there comes out)."""
    bot = scene.bot
    player_id = _pid(scene.author)
    if player_id in _awaiting_confirmation:
        await scene.send(MSG_ALREADY_WAITING)
        return False
    column = _slot_column(slot)
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, _STABLE_COLUMNS, lock=True)
        current = _int(owner["pet_id"]) if owner is not None else 0
        if owner is None:
            status = "alone"
        elif current <= 0:
            status = "no_pet"
        elif column is None:
            status = "bad_slot"
        else:
            status = _OK
            stored = max(_int(owner[column.lower()]), 0)
            await con.execute(f"UPDATE PETOWNER SET {column} = $2, PET_ID = $3 WHERE PLAYER_ID = $1",
                              player_id, current, stored)
    if status == "alone":
        await scene.send(_msg_alone(player_id))
    elif status == "no_pet":
        await scene.send("Niestety nie posiadasz aktualnie żadnego peta, <@" + str(player_id)
                         + ">. Możesz sprawdzić swoją stajnie wpisując **$stajnia**.")
    elif status == "bad_slot":
        await scene.send(_msg_bad_slot(player_id, "schowajtowarzysza"))
    else:
        await scene.send(MSG_STORED)
    return status == _OK


async def _take_out(bot: "AlterBot", player_id: int, column: str, expected_pet: int | None) -> str:
    """Swap stable ``column`` with the active pet in one locked transaction.

    Status ``alone`` / ``empty`` / ``swapped`` / ``taken``, or ``changed``
    (nothing done) when ``expected_pet`` is given and the slot no longer holds it.
    """
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, _STABLE_COLUMNS, lock=True)
        if owner is None:
            return "alone"
        stabled = _int(owner[column.lower()])
        if expected_pet is not None and stabled != expected_pet:
            return "changed"
        if stabled <= 0:
            return "empty"
        current = max(_int(owner["pet_id"]), 0)
        await con.execute(f"UPDATE PETOWNER SET {column} = $2, PET_ID = $3 WHERE PLAYER_ID = $1",
                          player_id, current, stabled)
        return "swapped" if current > 0 else "taken"


async def unstore_pet(scene: "Scene", slot: Any) -> bool:
    """Take a pet out of stable slot 1-4 (the active pet takes its place)."""
    player_id = _pid(scene.author)
    column = _slot_column(slot)
    if column is None:
        await scene.send(_msg_bad_slot(player_id, "wyciagnijtowarzysza"))
        return False
    if player_id in _awaiting_confirmation:
        await scene.send(MSG_ALREADY_WAITING)
        return False
    status = await _take_out(scene.bot, player_id, column, None)
    await scene.send(_unstore_message(player_id, status))
    return status in ("swapped", "taken")


def _unstore_message(player_id: int, status: str) -> str:
    """What the channel is told after a :func:`_take_out` (any status but ``changed``)."""
    return {"alone": _msg_alone(player_id), "empty": MSG_EMPTY_SLOT,
            "swapped": MSG_SWAPPED, "taken": MSG_UNSTORED}[status]


STABLE_ACTIVE = "🐾 **Przy Tobie**"
STABLE_PLACES = "🐴 **W stajni**"
STABLE_NOBODY = "*brak towarzysza*"
STABLE_FREE = "*wolne miejsce*"
STABLE_NO_DATA = "*brak danych w tabeli PETS*"


def _pet_brief(pet: Any) -> str:
    """``Mruczek · Kot · poziom 2`` / ``Towarzysz · jajko`` - plain text (a menu shows no markdown)."""
    level = _int(pet["pet_lvl"])
    name = str(pet["pet_name"] or "")[:60]
    if level == 0:
        return f"{name} · jajko"
    return " · ".join(part for part in (name, POLISH_TYPES.get(pet["type"], ""), f"poziom {level}") if part)


def _pet_line(pet: Any) -> str:
    """:func:`_pet_brief` for an embed: the name in bold (and safe from markdown in it)."""
    name, _, rest = _pet_brief(pet).partition(" · ")
    return f"**{discord.utils.escape_markdown(name)}** · {rest}"


async def _stable_view(bot: "AlterBot", player_id: int) -> tuple[discord.Embed, list[ui.Option]] | None:
    """The stable embed and the stabled pets that can be taken out (None: the player has no row)."""
    owner = await _owner(bot.db, player_id, _STABLE_COLUMNS)
    if owner is None:
        return None

    pet_ids = [_int(value) for value in owner]
    wanted = [pet_id for pet_id in pet_ids if pet_id > 0]
    pets_by_id: dict[int, Any] = {}
    if wanted:
        rows = await bot.db.fetch(
            "SELECT PET_ID, PET_NAME, PET_LVL, TYPE FROM PETS WHERE PET_ID = ANY($1::numeric[])", wanted)
        for row in rows:
            pets_by_id.setdefault(_int(row["pet_id"]), row)

    # Two short blocks instead of five "label: value" sentences: the pet at the player's
    # side, then the four places of the stable (an empty place says so in italics).
    active = STABLE_NOBODY
    places = []
    options: list[ui.Option] = []
    for index, pet_id in enumerate(pet_ids):
        if pet_id <= 0:
            line = STABLE_NOBODY if index == 0 else STABLE_FREE
        else:
            pet = pets_by_id.get(pet_id)
            if pet is None:
                line = STABLE_NO_DATA
            else:
                line = _pet_line(pet)
                if index > 0:  # "slot:pet": a stale list cannot take out another pet than it names
                    options.append(ui.Option(f"{index}:{pet_id}", _pet_brief(pet)[:100],
                                             f"Miejsce w stajni {index}", emoji="🐾"))
        if index == 0:
            active = line
        else:
            places.append(f"**{index}.** {line}")

    embed = discord.Embed(
        title="Stajnia",
        description=("Oto Twoja stajnia, <@" + str(player_id) + "> <:peepoBlush:984769061340737586>\n\n"
                     + f"{STABLE_ACTIVE}\n{active}\n\n{STABLE_PLACES}\n" + "\n".join(places)),
        color=0x34cceb,
    )
    embed.set_footer(text="Możesz schować lub wyciągnąć peta za pomocą komend $schowajtowarzysza "
                          "oraz $wyciagnijtowarzysza.")
    embed.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png")
    return embed, options


async def check_stable(scene: "Scene") -> bool:
    """Show the author's active pet and the four stable slots; False when he has no row.

    On a channel the stable carries a list (:class:`StablePanel`) that makes a
    stabled pet the active one - only for the author, only where and when
    ``$wyciagnijtowarzysza`` would work.
    """
    bot = scene.bot
    player_id = _pid(scene.author)
    view = await _stable_view(bot, player_id)
    if view is None:
        await scene.send(_msg_alone(player_id))
        return False
    embed, options = view
    if not options:
        await scene.send(embed=embed)
        return True
    panel = StablePanel(scene.author, options)
    try:
        await panel.send(scene.channel, embed=embed)
    except BaseException:
        panel.stop()
        raise
    return True


# -- panels (buttons under a shown pet and under the stable) ---------------------------

def _display_gate_busy(bot: "AlterBot") -> bool:
    """Whether display-only buttons are blocked while busy (as ``unless_busy(read_only=True)``)."""
    return getattr(getattr(bot, "settings", None), "busy_policy", "notify") != "read_only_free"


async def _edit_panel(message: Any, **kwargs: Any) -> bool:
    """Edit a panel message; False when it is gone (purged).  Never raises."""
    if message is None:
        return False
    try:
        await message.edit(**kwargs)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - cosmetic; usually the message was purged meanwhile
        return False


class StablePanel(ui.PanelView):
    """The list under ``$stajnia``: choose a stabled pet to make it the active one.

    Same effect and the same guards as ``$wyciagnijtowarzysza <slot>``: boss
    channel only, not while busy, not for dead hunters.  The stable is redrawn
    after every change.
    """

    def __init__(self, owner: Any, options: Sequence[ui.Option], *, timeout: float | None = None) -> None:
        super().__init__(owner=owner, timeout=PANEL_TIMEOUT if timeout is None else timeout)
        self.select: discord.ui.Select = discord.ui.Select(
            custom_id=ui.FLOW_PREFIX + "pet:stable-take", placeholder="Wyciągnij towarzysza ze stajni…",
            min_values=1, max_values=1, options=self._options(options))
        self.select.callback = self._chosen  # type: ignore[method-assign]
        self.add_item(self.select)

    @staticmethod
    def _options(options: Sequence[ui.Option]) -> list[discord.SelectOption]:
        return [discord.SelectOption(label=option.label[:100], value=option.value,
                                     description=(option.description or None) and option.description[:100],
                                     emoji=option.emoji) for option in options[:25]]

    @staticmethod
    def _parse(value: Any) -> tuple[str, int] | None:
        """``"3:7"`` -> (stable column of slot 3, pet 7); None for anything else."""
        slot, _, pet_id = str(value).partition(":")
        column = _slot_column(slot)
        try:
            pet = int(pet_id)
        except ValueError:
            return None
        return (column, pet) if column is not None and pet > 0 else None

    async def _chosen(self, interaction: Any) -> None:
        if not await ui.gate(interaction):
            return
        values = (getattr(interaction, "data", None) or {}).get("values") or list(self.select.values)
        await ui.ack(interaction)
        if not values:
            return
        scene = ui.scene_of(interaction)
        player_id = _pid(interaction.user)
        chosen = self._parse(values[0])
        if player_id in _awaiting_confirmation:
            await scene.send(MSG_ALREADY_WAITING)  # as $wyciagnijtowarzysza
        else:
            # The pet the entry names must still be in its slot (checked under the lock).
            status = "changed" if chosen is None else await _take_out(scene.bot, player_id, *chosen)
            if status == "changed":
                log.info("Stable list of %s: %r no longer matches the stable.", player_id, values[0])
                await ui.reply(interaction, MSG_STABLE_CHANGED)
            else:
                await scene.send(_unstore_message(player_id, status))
        # Redrawn in every case: the list must not keep showing the entry as chosen.
        view = await _stable_view(scene.bot, player_id)
        if view is None:
            await self.finish()
            return
        embed, options = view
        if not options:
            await self.finish(embed=embed)
            return
        self.select.options = self._options(options)
        await _edit_panel(self.message, embed=embed, view=self)


class PetPanel(ui.PanelView):
    """Buttons under the pet shown by ``$towarzysz``; only its owner can use them.

    ``Zmień imię`` opens a pop-up window (``$nazwij`` with its 23-hour
    cooldown), ``Stajnia`` shows the stable (display only, any channel), the
    rest start exactly the flows of ``$odrodzenie``, ``$mocneodrodzenie``,
    ``$oswiecenie``, ``$transformacja`` and ``$porzucam``.  Gameplay buttons
    are refused outside the boss channel, while the channel is
    busy and for dead hunters.  A gameplay button takes the panel's buttons
    away: the flow shows the pet anew when it changed it.  Buttons that make
    no sense for this pet (rerolling an egg, enlightening a young pet,
    transforming a mythic one or without the rank) are not shown.

    The panel acts only on the pet it shows (:attr:`pet_id`): once another pet
    is active (taken out of the stable meanwhile) a click takes the buttons off
    and is answered with :data:`MSG_OTHER_PET`; the flows re-check
    it after their question too.  ``image``: the file sent with the panel (the
    pet's card, see :func:`_pet_card`).
    """

    def __init__(self, player: Any, pet: Any, *, image: Path | None = None,
                 timeout: float | None = None) -> None:
        super().__init__(owner=player, timeout=PANEL_TIMEOUT if timeout is None else timeout)
        self.player = player
        self.pet_id = _int(pet["pet_id"])
        self.image = image
        self.pet_name = str(pet["pet_name"] or "")
        level, skills = _int(pet["pet_lvl"]), _int(pet["pet_skills"])
        hidden = set()
        if level < 1:
            hidden |= {self.reroll, self.adv_reroll}
        if not (level == MAX_LEVEL and skills <= ENLIGHT_MAX_SKILLS):
            hidden.add(self.enlight)
        if pet["mythic"] or not patrons.check_if_patron(player):
            hidden.add(self.transform)
        for item in hidden:
            self.remove_item(item)

    # -- display only ---------------------------------------------------------------

    @discord.ui.button(label="Zmień imię", emoji="✏️", style=discord.ButtonStyle.secondary, row=0,
                       custom_id=ui.FLOW_PREFIX + "pet:rename")
    async def rename(self, interaction: Any, button: discord.ui.Button) -> None:
        await rename_from_panel(self, interaction)

    @discord.ui.button(label="Stajnia", emoji="🐴", style=discord.ButtonStyle.secondary, row=0,
                       custom_id=ui.FLOW_PREFIX + "pet:stable")
    async def stable(self, interaction: Any, button: discord.ui.Button) -> None:
        if not await ui.gate(interaction, boss_channel=False, players_only=False,
                             unless_busy=_display_gate_busy(interaction.client)):
            return
        await ui.ack(interaction)
        await check_stable(ui.scene_of(interaction))

    # -- gameplay -------------------------------------------------------------------

    @discord.ui.button(label="Odrodzenie", emoji="🎲", style=discord.ButtonStyle.primary, row=1,
                       custom_id=ui.FLOW_PREFIX + "pet:reroll")
    async def reroll(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._play(interaction, lambda scene: reroll_pet(scene, scene.author, expected_pet=self.pet_id))

    @discord.ui.button(label="Mocne odrodzenie", emoji="🔮", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=ui.FLOW_PREFIX + "pet:adv-reroll")
    async def adv_reroll(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._play(interaction, lambda scene: adv_reroll_pet(scene, expected_pet=self.pet_id),
                         hold="adv-reroll")

    @discord.ui.button(label="Oświecenie", emoji="✨", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=ui.FLOW_PREFIX + "pet:enlight")
    async def enlight(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._play(interaction, lambda scene: enlight_pet(scene, scene.author, expected_pet=self.pet_id))

    @discord.ui.button(label="Transformacja", emoji="🪞", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=ui.FLOW_PREFIX + "pet:transform")
    async def transform(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._play(interaction, lambda scene: transform_pet(scene, scene.author, expected_pet=self.pet_id))

    @discord.ui.button(label="Porzuć", emoji="💔", style=discord.ButtonStyle.danger, row=2,
                       custom_id=ui.FLOW_PREFIX + "pet:discard")
    async def discard(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._play(interaction, lambda scene: discard_pet(scene, expected_pet=self.pet_id))

    async def shows_active_pet(self, interaction: Any) -> bool:
        """True when the click may act on the active pet: it is this panel's pet (or there is none).

        Otherwise the click is answered with :data:`MSG_OTHER_PET`
        and the panel's buttons are taken off.
        """
        player_id = _pid(interaction.user)
        owner = await _owner(interaction.client.db, player_id, "PET_ID")
        active = _int(owner["pet_id"]) if owner is not None else 0
        if not _other_pet(active, self.pet_id):
            return True
        log.info("Pet panel of %s shows pet %s but pet %s is active; click refused.",
                 player_id, self.pet_id, active)
        await ui.reply(interaction, MSG_OTHER_PET)
        await self.finish()
        return False

    async def _play(self, interaction: Any, flow: Callable[["Scene"], Awaitable[bool]], *,
                    hold: str | None = None) -> None:
        """Run a gameplay flow for the click, with the guards of its command."""
        if not await ui.gate(interaction) or not await self.shows_active_pet(interaction):
            return
        log.info("Pet panel of %s: %s.", self.owner_id, (interaction.data or {}).get("custom_id"))
        scene = ui.scene_of(interaction)
        if hold is None:
            await ui.ack(interaction)
            await self.finish()
            await flow(scene)
            return
        # Like ``$mocneodrodzenie``: the flow holds the channel.  The database read
        # above may have let somebody take it, so the gate looks again; nothing is
        # awaited between its (passing) check and taking the flag.
        if not await ui.gate(interaction):
            return
        with interaction.client.state.hold_busy(hold):
            await ui.ack(interaction)
            await self.finish()
            await flow(scene)

    async def redraw(self, bot: "AlterBot") -> bool:
        """Show this panel's pet as it is now (after a rename).  Never raises on Discord.

        When another pet is active the buttons are taken off instead (the
        message carries this pet's picture).  A changed picture is sent along
        with the embed: the pet grew meanwhile, or its card is another one now
        (the new name made it shiny, so it has a frame).  The caller has
        acknowledged its interaction - a new card may take a moment (and the
        channel sees "🐾 Nadchodzi towarzysz..." while it does, see
        :mod:`altermmo.core.waiting`).
        """
        player_id = _pid(self.player)
        status, items, pet = await _load_shown_pet(bot, player_id)
        if status != _OK:
            return False
        if _int(pet["pet_id"]) != self.pet_id:
            await self.finish()
            return False
        self.pet_name = str(pet["pet_name"] or "")
        async with waiting.notice(getattr(self.message, "channel", None)) as wait:
            return await self._redraw(bot, pet, items, wait)

    async def _redraw(self, bot: "AlterBot", pet: Any, items: Any, wait: waiting.Waiting) -> bool:
        """:meth:`redraw` once the pet is read: draw its card, edit the message."""
        guild = getattr(self.message, "guild", None) or getattr(self.player, "guild", None)
        embed, image = await _shown(bot, guild, self.player, pet, items, wait=wait)
        if image == self.image:
            return await _edit_panel(self.message, embed=embed)
        try:
            attachments = [] if image is None else [discord.File(str(image))]
        except OSError as exc:  # the picture vanished since it was checked
            log.warning("Pet %s: picture %s could not be read (%s); panel retired.", self.pet_id, image, exc)
            await self.finish()
            return False
        if not await _edit_panel(self.message, embed=embed, attachments=attachments):
            return False
        self.image = image
        return True


def _rename_bucket(bot: "AlterBot", member: Any) -> Any:
    """The ``$nazwij`` cooldown of ``member`` (shared by the command and the button); None without one."""
    command = bot.get_command("nazwij") if hasattr(bot, "get_command") else None
    buckets = getattr(command, "_buckets", None)
    if buckets is None or not getattr(buckets, "valid", False):
        return None
    try:
        return buckets.get_bucket(types.SimpleNamespace(author=member, guild=getattr(member, "guild", None)))
    except Exception:  # noqa: BLE001 - a cooldown that cannot be read is no reason to fail the click
        log.exception("Could not read the $nazwij cooldown of %s.", getattr(member, "id", member))
        return None


async def rename_from_panel(panel: PetPanel, interaction: Any) -> bool:
    """``Zmień imię``: ask for the name in a pop-up window, then name the pet like ``$nazwij``.

    The guards and the 23-hour cooldown of the command apply (the cooldown is
    the command's own, so a name given with a button counts for ``$nazwij``
    and the other way round; a refused naming does not use it up).  Only the
    pet the panel shows is named (see :meth:`PetPanel.shows_active_pet`),
    checked when the button is clicked and again when the window is sent.

    The cooldown is charged on the bucket fetched again right before it, with
    nothing awaited in between: discord.py drops a never-used bucket from its
    cache whenever anybody's ``$nazwij`` looks at the cooldowns, so the bucket
    seen at the click may be an orphan by the time the window is sent.
    """
    bot = interaction.client
    if not await ui.gate(interaction) or not await panel.shows_active_pet(interaction):
        return False
    bucket = _rename_bucket(bot, interaction.user)
    if bucket is not None:
        retry_after = bucket.get_retry_after()  # a preview only: the window is not opened in vain
        if retry_after > 0:
            await ui.reply(interaction, MSG_RENAME_COOLDOWN.format(when=ui.when(retry_after)))
            return False
    default = panel.pet_name if panel.pet_name and panel.pet_name != DEFAULT_NAME else None
    window = await ui.ask_text(interaction, "Nowe imię towarzysza", "Imię", max_length=MAX_NAME_LENGTH,
                               default=default[:MAX_NAME_LENGTH] if default else None,
                               timeout=timing.timeout(RENAME_WINDOW_TIMEOUT))
    if window is None:
        return False
    submitted, name = window.interaction, str(window.value or "").strip()
    if not name:
        await ui.reply(submitted, MSG_RENAME_EMPTY)
        return False
    if not await panel.shows_active_pet(submitted):  # the window may have been open for minutes
        return False
    if bot.state.busy:
        await ui.reply(submitted, ui.MSG_BUSY)
        return False
    bucket = _rename_bucket(bot, submitted.user)  # no await between this and the charge below
    if bucket is not None:
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await ui.reply(submitted, MSG_RENAME_COOLDOWN.format(when=ui.when(retry_after)))
            return False
    await ui.ack(submitted)
    done = False
    try:
        done = await name_pet(ui.scene_of(submitted), name, expected_pet=panel.pet_id)
    finally:
        if not done and bucket is not None:
            bucket.reset()  # nothing was named: the 23 hours do not start
    if done:
        await ui.reply(submitted, MSG_RENAME_DONE.format(name=discord.utils.escape_markdown(name[:100])))
        await panel.redraw(bot)
    return done


# -- ranking and boss detection -------------------------------------------------------

#: What makes a pet special, as shown in the ranking - only what the pet really has.
BADGE_SHINY, BADGE_ULTRA_SHINY = "✨ Shiny", "🌟 Ultra shiny"
BADGE_MYTHIC, BADGE_PREMIUM = "🔮 Mityczny", "💎 Premium"
MSG_NO_PETS_YET = "Nikt jeszcze nie ma towarzysza."


def _ranking_entry(place: int, pet: Any) -> str:
    """Two lines of the pet ranking: who it is, then its talent and what makes it special.

    (The old single line listed every property with "Tak" / "Nie" and was hard to read.)
    """
    name = discord.utils.escape_markdown(str(pet["pet_name"] or "")[:20])
    level = _int(pet["pet_lvl"])
    about = [POLISH_TYPES.get(pet["type"], ""), "jajko" if level == 0 else f"poziom {level}"]
    badges = []
    # Only the highest badge: mythic already says (ultra) shiny, ultra shiny says shiny.
    if pet["mythic"] is True or pet["quality"] == "Mythic":
        badges.append(BADGE_MYTHIC)
    else:
        if pet["ultra_shiny"] is True:
            badges.append(BADGE_ULTRA_SHINY)
        elif pet["shiny"] is True:
            badges.append(BADGE_SHINY)
        if pet["quality"] == "Premium":
            badges.append(BADGE_PREMIUM)
    first = " · ".join([f"{ui.place(place)} **{name}**", *(part for part in about if part)])
    second = " · ".join([f"Talent: **{_talent(_int(pet['pet_skills']))}**", *badges])
    return f"{first}\n┗ {second}"


async def pet_ranking(scene: "Scene") -> None:
    """Show the ten most talented pets."""
    rows = await scene.bot.db.fetch(
        """SELECT PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, TYPE, ULTRA_SHINY, MYTHIC FROM PETS
           WHERE PET_ID > 0
           ORDER BY PET_SKILLS DESC NULLS LAST, SLOW_PERC DESC NULLS LAST, PET_ID LIMIT 10""")

    ranking_string = "\n".join(_ranking_entry(place, pet) for place, pet in enumerate(rows, start=1))

    emb = discord.Embed(title='Najbardziej utalentowani towarzysze!', url=RANKING_IMAGE,
                        description=ranking_string or MSG_NO_PETS_YET, color=0xFF0000)
    emb.set_thumbnail(url=RANKING_IMAGE)
    emb.set_footer(text='Gratulacje dla posiadaczy!')
    await scene.send(embed=emb)


#: First line of the private "a boss is coming" warning (players know it word for word).
MSG_BOSS_WARNING = "Nadciąga boss! Będzie {rarity}."


async def _warn_about_boss(bot: "AlterBot", player_id: int, text: str) -> None:
    member = await bot.member(player_id)
    if member is None:
        log.info("Boss detection: player %s left the server; skipped.", player_id)
        return
    try:
        # With the button that switches these warnings off (features/bossdetect.py).
        await bossdetect.send_warning(member, text)
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - closed DMs must not stop the boss
        log.info("Boss detection: cannot DM player %s (%s: %s).", player_id, type(exc).__name__, exc)
        if io.is_transient(exc):
            return  # Discord is having a bad moment, the player's settings are fine
    channel = bot.get_channel(bot.ids.log_channel if bot.debug else bot.ids.notice_channel)
    if channel is not None:
        # The hint has a button that switches these warnings off for the player.
        await bossdetect.send_hint(bot, channel, player_id)


async def detect_boss(bot: "AlterBot", boss_rarity: int) -> None:
    """Send PM to everyone whose active pet can detect bosses.

    One player who cannot be reached never stops the others (or the boss).
    The warning's first line is :data:`MSG_BOSS_WARNING`; under it one line and a
    button tell how to switch the warnings off (``bossdetect.dm_warning``).
    """
    rows = await bot.db.fetch(
        """SELECT DISTINCT o.PLAYER_ID FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
           WHERE p.DETECTION = TRUE AND o.PET_ID > 0 AND o.PLAYER_ID > 0""")
    rarity = _RARITY_NAMES[min(max(_int(boss_rarity), 0), len(_RARITY_NAMES) - 1)]
    text = bossdetect.dm_warning(bot, MSG_BOSS_WARNING.format(rarity=rarity))
    log.info("Boss detection: warning %d player(s) about a boss (%s).", len(rows), rarity)
    for row in rows:
        player_id = _int(row["player_id"])
        if bossdetect.is_off(bot, player_id):
            continue  # switched off by the player (button under the hint / $wyczuwanie)
        try:
            await _warn_about_boss(bot, player_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Boss detection: could not warn player %s: %s: %s",
                        player_id, type(exc).__name__, exc)
