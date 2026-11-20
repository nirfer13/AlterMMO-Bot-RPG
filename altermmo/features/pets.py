"""Pets (companions): eggs, hatching, talents, rerolls, the stable, boss detection.

Port of the legacy ``functions/functions_pets.py``.  Tables (unchanged, shared
with the legacy bot - neither has a primary key):

``PETOWNER``  one row per player: the active pet, four stable slots, the pet
              items (reroll scrolls and their shards, rebirth stones, mirrors)
              and the skill gem (owned by features/skills.py);
``PETS``      one row per pet: name, level 0 (egg) .. 3, talent (``PET_SKILLS``),
              looks and the seven stats used by the fights - and, new in v2,
              the two cosmetics ``SEAL`` (0-3) and ``HOLO`` (see below).

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
  roll for a seal (:data:`SEAL_ROLL_BELOW`) and ONE independent roll for Holo
  (:data:`HOLO_ROLL_BELOW`), whatever the class of the pet.  The result is
  stored with the specimen (``PETS.SEAL`` / ``PETS.HOLO``) and nothing but the
  owner's ``$DajPieczec`` ever changes it: not growing, the mirror, a new name,
  the stable, a new owner or the end of the support;
* what is stored is SHOWN only while the pet's owner is a supporter (owner's
  decision of 2026-11-20): when the support ends the seal and the Holo foil
  disappear from the card and from the statistics, and they are back the
  moment the owner supports again (:func:`_is_supporter`, asked every time
  the pet is shown).  The frame belongs to the pet's class and always stays;
* the two columns are added to the old bot's table at start-up
  (:data:`NEW_COLUMNS`, run by ``schema.add_new_columns``).  When that failed
  the game goes on without cosmetics (:func:`cosmetics_available`): nothing is
  rolled, eggs are stored the old way and every reader takes a row without the
  columns as "no seal, no Holo" (:func:`cosmetics_of`);
* a pet is shown as a card composed by :mod:`altermmo.features.petcards`
  (illustration, Holo foil, frame of its class, seal) and names its seal / Holo
  in words (:func:`_pet_card`) - from the egg on: an egg has its frame, seal
  and Holo like a hatched pet.
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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, Sequence

import asyncpg
import discord

from ..core import io, timing, ui
from ..settings import ASSETS_DIR
from . import bossdetect, modifiers, patrons, petcards

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
           HOLO BOOLEAN NOT NULL DEFAULT FALSE
       )""",
)

#: Columns the old bot's ``PETS`` lacks, as ``(table, column, definition)``; added at every
#: start-up by ``schema.add_new_columns`` (only when missing).  The defaults make every pet
#: that existed before - and every row the old bot still inserts with its own column
#: lists - a pet without a seal and without Holo.
NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("pets", "seal", "NUMERIC NOT NULL DEFAULT 0"),
    ("pets", "holo", "BOOLEAN NOT NULL DEFAULT FALSE"),
)
#: The cosmetic columns of ``PETS`` (all of them are there, or cosmetics are off).
COSMETIC_COLUMNS: tuple[str, ...] = tuple(column for _, column, _ in NEW_COLUMNS)

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
#: gives Holo with a chance of 0.1% (1/1000).  No cosmetic gives stats."  Both rolls are
#: ``random.randrange(COSMETIC_ROLL)``, independent of each other and of the pet's class.
COSMETIC_ROLL = 1000
#: Seal roll: the first entry whose bound is above the roll gives the tier -
#: 0-1 Arcyłowcy (0.2%), 2-49 Łowcy (4.8%), 50-149 Tropiciela (10%), 150-999 no seal (85%).
SEAL_ROLL_BELOW: tuple[tuple[int, int], ...] = ((2, 3), (50, 2), (150, 1))
#: Holo roll: only 0 gives Holo (1/1000).
HOLO_ROLL_BELOW = 1
#: Seal tiers are 0 (none) .. MAX_SEAL; their names are ``petcards.SEAL_NAMES``.
MAX_SEAL = 3

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
#: The owner's log channel (no ping, nothing public): an egg with the best seal and / or Holo.
MSG_RARE_COSMETICS = "Rzadka kosmetyka towarzysza: jajko gracza <@{player_id}> (PET_ID {pet_id}) ma {found}."
MSG_RARE_SEAL = "pieczęć {name}"
MSG_RARE_HOLO = "folię Holo"
PET_FOOTER = "Na zawsze ponosisz odpowiedzialność za to, co oswoiłeś."
#: In the statistics of a pet whose seal / Holo are hidden because its owner does not support now.
MSG_COSMETICS_HIDDEN = "*Ozdoby karty (pieczęć, folia Holo) widać tylko u wspierających.*"
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


def _roll_cosmetics() -> tuple[int, bool]:
    """The two cosmetic rolls of one egg: ``(seal tier, holo)``, see :data:`SEAL_ROLL_BELOW`."""
    roll = random.randrange(COSMETIC_ROLL)
    seal = next((tier for below, tier in SEAL_ROLL_BELOW if roll < below), 0)
    holo = random.randrange(COSMETIC_ROLL) < HOLO_ROLL_BELOW
    return seal, holo


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
#: What growing, a reroll and an enlightenment save.  SEAL and HOLO are not in here on purpose
#: (nor in any other UPDATE but :func:`set_cosmetics`): cosmetics are for the pet's whole life.
_SQL_SAVE_PET = """UPDATE PETS SET PET_LVL = $2, PET_SKILLS = $3, VARIANT = $4,
                       CRIT_PERC = $5, REPLACE_PERC = $6, DEF_PERC = $7, DROP_PERC = $8,
                       LOWHP_PERC = $9, SLOW_PERC = $10, DETECTION = $11
                   WHERE PET_ID = $1"""


#: How many of the given columns the table the bot reads as ``PETS`` has.
_SQL_COUNT_COLUMNS = """SELECT COUNT(*) FROM pg_attribute
                        WHERE attrelid = to_regclass('pets') AND attname = ANY($1::text[])
                          AND attnum > 0 AND NOT attisdropped"""


def _for_update(sql: str, lock: bool) -> str:
    return sql + " FOR UPDATE" if lock else sql


async def cosmetics_available(bot: "AlterBot", *, refresh: bool = False) -> bool:
    """Whether ``PETS`` has the cosmetic columns, i.e. whether seals and Holo are in the game.

    Looked up in the database once per run and remembered on the bot, like the
    ``SKILLPOUCH`` check of features/skills.py: the columns are added at
    start-up (``schema.add_new_columns``), and when that could not be done (no
    privilege, the table was locked) the bot plays on without cosmetics until
    its next start.  ``refresh=True`` looks again after a "no" (the owner may
    have added the columns by hand meanwhile).
    """
    known = getattr(bot, "_pet_cosmetics", None)
    if known is None or (refresh and not known):
        found = _int(await bot.db.fetchval(_SQL_COUNT_COLUMNS, list(COSMETIC_COLUMNS)))
        known = found == len(COSMETIC_COLUMNS)
        bot._pet_cosmetics = known  # type: ignore[attr-defined]
        if not known:
            log.warning("PETS has no %s column(s): pet cosmetics are off (nothing is rolled, pets are "
                        "shown without seals and Holo; the frames of shiny pets do not need the columns).",
                        " / ".join(COSMETIC_COLUMNS).upper())
    return known


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


def _pet_embed(player_id: int, pet: Any, items: Any, *, cosmetics: bool = True) -> discord.Embed:
    """The pet's embed.  ``cosmetics``: whether its seal / Holo are shown (the owner supports)."""
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

    # The seal and the Holo in words as well - for an egg too (owner's decision of 2026-11-20:
    # they are visible from the egg on).  They are shown only while the owner supports;
    # otherwise one line says why they are gone.
    seal, holo = cosmetics_of(pet)
    cosmetic_lines = ""
    if cosmetics:
        if seal in petcards.SEAL_NAMES:
            cosmetic_lines += f"Pieczęć: {petcards.SEAL_NAMES[seal]}\n"
        if holo:
            cosmetic_lines += "Folia: Holo\n"
    elif seal or holo:
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
                    cosmetics: bool = True) -> Path | None:
    """The file to send for ``pet`` whose plain picture is ``source``.

    A pet is shown as its card: illustration, Holo foil, the frame of its
    class, seal (:func:`petcards.card`; an ordinary pet without cosmetics, a
    mythic one without them and a card that could not be composed stay the
    plain picture).  An egg is a card like any other (owner's decision of
    2026-11-20: frame, seal and Holo are visible from the egg on).  A pet
    without a picture has no card.
    ``cosmetics=False`` (the owner does not support right now) leaves the seal
    and the foil out; the frame of the pet's class stays.
    A cold Holo card can take seconds - every caller has acknowledged its
    interaction before it gets here.  Never raises for a card problem.
    """
    if source is None:
        return source
    try:
        look = petcards.look_of(pet)
        if not cosmetics:
            look = petcards.Look(frame=look.frame)
        card = await petcards.card(source, look, _card_cache(bot),
                                   max_bytes=_card_max_bytes(guild))
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


async def _shown(bot: "AlterBot", guild: Any, player: Any, pet: Any,
                 items: Any) -> tuple[discord.Embed, Path | None]:
    """The pet's embed and the file to send with it (its card, see :func:`_pet_card`).

    ``player``: the pet's owner (a member or a bare id) - his seal and Holo are
    shown only while he is a supporter (:func:`_is_supporter`).
    The embed's image always names the file returned here.
    """
    # Only a pet (or an egg) with something to show needs the answer (a bare id costs a lookup).
    cosmetics = cosmetics_of(pet) != (0, False) and await _is_supporter(bot, player)
    embed, source = _shown_embed(_pid(player), pet, items, cosmetics=cosmetics)
    image = await _pet_card(bot, guild, pet, source, cosmetics=cosmetics)
    if image is not None and image != source:
        embed.set_image(url=f"attachment://{image.name}")
    return embed, image


async def show_pet(scene: "Scene", player: Any, *, buttons: bool = True) -> bool:
    """Show the player's pet and his scrolls; False when he has no pet.

    The pet carries a :class:`PetPanel` for its owner unless ``buttons=False``.
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

    embed, image = await _shown(bot, getattr(scene.channel, "guild", None), player, pet, owner)
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

def _roll_egg(player: Any, *, cosmetics: bool = True) -> dict[str, Any]:
    """Draw a new egg for ``player``.

    Supporters (patrons, crafters, Twitch subscribers, YouTube supporters) can
    find premium eggs and get the two cosmetic rolls (:func:`_roll_cosmetics`):
    once per egg however many of the roles they hold, whatever the class of the
    egg - also a mythic one.  ``cosmetics=False`` (``PETS`` has no columns for
    them, see :func:`cosmetics_available`): nothing is rolled and the egg has no
    ``SEAL`` / ``HOLO`` at all.

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
        egg["SEAL"], egg["HOLO"] = _roll_cosmetics() if supporter else (0, False)
    return egg


_SQL_INSERT_EGG = """INSERT INTO PETS (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY,
                         MYTHIC, TYPE, VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC, LOWHP_PERC,
                         SLOW_PERC, INIT_PERC, DETECTION{columns})
                     VALUES ($1, $2, 0, 0, $3, $4, $5, $6, $7, $8, 0, 0, 0, 0, 0, 0, 0, FALSE{values})"""


async def _insert_egg(con: Any, egg: dict[str, Any]) -> int:
    """Store a rolled egg under the next free PET_ID; returns that id.

    An egg rolled with cosmetics (it has ``SEAL`` / ``HOLO``) is stored with
    them.  Should the columns be missing after all (they were there when the
    bot looked), the egg is stored the old way and the two keys are taken out
    of ``egg``: the player gets his pet, only the cosmetics are lost.
    """
    await con.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", _LOCK_PET_IDS)
    pet_id = _int(await con.fetchval("SELECT COALESCE(MAX(PET_ID), 0) + 1 FROM PETS"))
    args = (pet_id, DEFAULT_NAME, egg["QUALITY"], egg["SHINY"], egg["ULTRA_SHINY"], egg["MYTHIC"],
            egg["TYPE"], str(egg["VARIANT"]))
    if "SEAL" in egg:
        try:
            # A savepoint: a refused INSERT must not spoil the caller's transaction.
            async with con.transaction():
                await con.execute(_SQL_INSERT_EGG.format(columns=", SEAL, HOLO", values=", $9, $10"),
                                  *args, int(egg["SEAL"]), bool(egg.get("HOLO")))
        except asyncpg.UndefinedColumnError:
            seal, holo = egg.pop("SEAL"), egg.pop("HOLO", False)
            log.info("PETS has no SEAL / HOLO columns: the cosmetics of pet egg %s are lost "
                     "(seal %s, holo %s).", pet_id, seal, holo)
    if "SEAL" not in egg:
        await con.execute(_SQL_INSERT_EGG.format(columns="", values=""), *args)
    log.info("Pet egg %s generated: %s", pet_id, egg)
    return pet_id


async def _egg_stored(bot: "AlterBot", player_id: int, pet_id: int, egg: dict[str, Any],
                      cosmetics: bool) -> None:
    """After a new egg is saved: remember lost columns, tell the owner about rare cosmetics.

    The best seal and Holo are rare enough for the owner to want to know; the
    player sees them on his egg (``$towarzysz``), so nothing is announced.  Never raises.
    """
    if cosmetics and "SEAL" not in egg:
        bot._pet_cosmetics = False  # type: ignore[attr-defined]  # see _insert_egg: the columns are gone
        return
    seal, holo = int(egg.get("SEAL", 0)), bool(egg.get("HOLO", False))
    found = []
    if seal >= MAX_SEAL:
        found.append(MSG_RARE_SEAL.format(name=petcards.SEAL_NAMES.get(seal, seal)))
    if holo:
        found.append(MSG_RARE_HOLO)
    if not found:
        return
    log.info("Pet egg %s of player %s has rare cosmetics: seal %s, holo %s.", pet_id, player_id, seal, holo)
    await bot.log_to_owner(MSG_RARE_COSMETICS.format(player_id=player_id, pet_id=pet_id,
                                                     found=" i ".join(found)))


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
    cosmetics = await cosmetics_available(bot)
    egg = _roll_egg(player, cosmetics=cosmetics)
    async with bot.db.transaction() as con:
        pet_id = await _insert_egg(con, egg)
    await _egg_stored(bot, player_id, pet_id, egg, cosmetics)
    if egg["MYTHIC"]:
        await _announce_mythic(scene, player_id)
    return pet_id


async def assign_pet(scene: "Scene", player: Any) -> bool:
    """Give the player a new egg; False when he already has a pet."""
    bot = scene.bot
    player_id = _pid(player)
    cosmetics = await cosmetics_available(bot)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        owner = await _owner(con, player_id, "PET_ID")
        if owner is not None and _int(owner["pet_id"]) > 0:
            log.debug("Player %s already has a pet; no egg assigned.", player_id)
            return False
        egg = _roll_egg(player, cosmetics=cosmetics)
        pet_id = await _insert_egg(con, egg)
        if owner is not None:
            await con.execute("UPDATE PETOWNER SET PET_ID = $2 WHERE PLAYER_ID = $1", player_id, pet_id)
        else:
            await _insert_owner(con, player_id, pet_id, True, 0, 0, 0, 0, 0)
    log.info("Pet %s assigned to player %s.", pet_id, player_id)
    await _egg_stored(bot, player_id, pet_id, egg, cosmetics)
    if egg["MYTHIC"]:
        await _announce_mythic(scene, player_id)
    return True


async def set_cosmetics(bot: "AlterBot", player_id: Any, seal: Any,
                        holo: bool | None = None) -> tuple[str, dict[str, Any] | None]:
    """Set the seal (and Holo, unless ``holo`` is None) of the player's active pet (``$DajPieczec``).

    The only way cosmetics change after the egg was drawn: for the owner, to
    look at the cards and to put a mistake right.  Returns ``(status, what the
    pet has now)``: ``bad_seal`` (not 0..:data:`MAX_SEAL`), ``no_columns``
    (cosmetics are off, see :func:`cosmetics_available` - looked up again),
    ``alone`` (no active pet) or ``_OK`` with ``pet_id`` / ``seal`` / ``holo``.
    """
    player_id = _pid(player_id)
    try:
        seal = int(seal)
    except (TypeError, ValueError):
        return "bad_seal", None
    if not 0 <= seal <= MAX_SEAL:
        return "bad_seal", None
    if not await cosmetics_available(bot, refresh=True):
        return "no_columns", None
    async with bot.db.transaction() as con:
        # The row lock every change of a pet takes (as _active_pet(lock=True)), but naming its
        # column: right after the columns were added by hand a prepared "SELECT p.*" of this
        # connection would be refused once, in the middle of the transaction.
        pet_id = await con.fetchval(
            """SELECT p.PET_ID FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
               WHERE o.PLAYER_ID = $1 AND o.PET_ID > 0 LIMIT 1 FOR UPDATE OF p""", player_id)
        if pet_id is None:
            return "alone", None
        row = await con.fetchrow(
            """UPDATE PETS SET SEAL = $2, HOLO = COALESCE($3::boolean, HOLO) WHERE PET_ID = $1
               RETURNING PET_ID, SEAL, HOLO""", pet_id, seal, holo)
    now = {"pet_id": _int(row["pet_id"]), "seal": _int(row["seal"]), "holo": row["holo"] is True}
    log.info("Cosmetics of pet %s (player %s) set by an administrator: %s", now["pet_id"], player_id, now)
    return _OK, now


async def reassign_pet(bot: "AlterBot", pet_id: Any, player_id: Any) -> bool:
    """Give an existing pet to a player without an active pet; True when it was done.

    The pet comes as it is - with its seal and Holo: they belong to the
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


async def _confirm(scene: "Scene", player: Any, prompt: str) -> ui.Confirmation:
    """Ask ``prompt`` with Potwierdzam / Anuluj buttons; 15 s for a click or a typed ``$potwierdzam``.

    The bot listens for the typed answer before the question becomes visible
    (:func:`ui.confirm`); only ``player`` can answer, only on this channel.
    The result is falsy when the player cancelled or did not answer.
    """
    return await ui.confirm(scene.bot, scene.channel, player, prompt,
                            timeout=timing.timeout(CONFIRM_TIMEOUT), typed=CONFIRM_WORD)


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

    Only the looks change (``TYPE`` / ``QUALITY`` / ``VARIANT``): the seal and
    Holo belong to the specimen and stay.  ``expected_pet``: as in :func:`reroll_pet`.
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
    """
    bot = scene.bot
    player = scene.author
    player_id = _pid(player)
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
            + ">? <:MonkaS:882181709100097587> " + CONFIRM_HINT)
        if not answer:
            await scene.send("*Twój towarzysz oddycha z ulgą...*")
            return False
        # The pet that is active *now* goes, together with its row in PETS.
        async with bot.db.transaction() as con:
            owner = await _owner(con, player_id, "PET_ID", lock=True)
            pet_id = _int(owner["pet_id"]) if owner is not None else 0
            other = _other_pet(pet_id, expected_pet)
            if pet_id > 0 and not other:
                await con.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", player_id)
                await con.execute("DELETE FROM PETS WHERE PET_ID = $1", pet_id)
        if other:
            log.info("Player %s: discard of pet %s refused, pet %s is active now.", player_id,
                     expected_pet, pet_id)
            await scene.send(MSG_OTHER_PET)
            return False
        if pet_id <= 0:
            await scene.send(MSG_ALONE_MAD)
            return False
        log.info("Player %s discarded pet %s.", player_id, pet_id)
        await modifiers.random_modifiers(scene, True, True)
        await _thank(answer, "<:MonkaS:882181709100097587>")
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
        acknowledged its interaction - a new card may take a moment.
        """
        player_id = _pid(self.player)
        status, items, pet = await _load_shown_pet(bot, player_id)
        if status != _OK:
            return False
        if _int(pet["pet_id"]) != self.pet_id:
            await self.finish()
            return False
        self.pet_name = str(pet["pet_name"] or "")
        guild = getattr(self.message, "guild", None) or getattr(self.player, "guild", None)
        embed, image = await _shown(bot, guild, self.player, pet, items)
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


async def _warn_about_boss(bot: "AlterBot", player_id: int, text: str) -> None:
    member = await bot.member(player_id)
    if member is None:
        log.info("Boss detection: player %s left the server; skipped.", player_id)
        return
    try:
        await io.send(member, text, attempts=2)
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
    """
    rows = await bot.db.fetch(
        """SELECT DISTINCT o.PLAYER_ID FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
           WHERE p.DETECTION = TRUE AND o.PET_ID > 0 AND o.PLAYER_ID > 0""")
    rarity = _RARITY_NAMES[min(max(_int(boss_rarity), 0), len(_RARITY_NAMES) - 1)]
    text = "Nadciąga boss! Będzie " + rarity + "."
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
