"""Pets (companions): eggs, hatching, talents, rerolls, the stable, boss detection.

Port of the legacy ``functions/functions_pets.py``.  Tables (unchanged, shared
with the legacy bot - neither has a primary key):

``PETOWNER``  one row per player: the active pet, four stable slots, the pet
              items (reroll scrolls and their shards, rebirth stones, mirrors)
              and the skill gem (owned by features/skills.py) - and, new in v2
              (the wandering merchant, features/merchant.py), stable places
              5-10 with the number of places the player has (``STABLE_PLACES``,
              4 by default) and the egg pouch (``EGG_POUCH``, ``POUCH_PET_ID``):
              an egg that comes while the player has a pet at his side goes
              there instead of being lost; it does not grow until he takes it
              out (``!wyjmijjajko`` or the stable list) - and, new with the
              companion missions (features/missions.py, 2026-11-25), the
              Duża sakwa (``BIG_POUCH``: a second egg, ``POUCH_PET_ID2``) and
              the summoning scrolls (``SUMMON_SCROLLS``), both rewards of a
              mission only (:data:`MISSION_COLUMNS`, :class:`Pouch`);
``PETS``    one row per pet: name, level 0 (egg) .. 3, talent (``PET_SKILLS``),
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
* a player has one pending ``!potwierdzam`` question at a time (one answer
  used to confirm every open question at once, e.g. a reroll *and* a discard),
  and the flag is always released (``!porzucam`` without a pet used to block
  the command until the next restart);
* the bot listens for ``!potwierdzam`` before its question is sent and only on
  the channel where it asked;
* NULL counters, a missing ``PETS`` row or a missing picture are handled
  instead of crashing the command;
* the ``create_*`` helpers never drop anything.

Buttons and menus (the rules behind them are the commands' own):

* every "are you sure?" question has **Potwierdzam / Anuluj** buttons that only
  the asking player can use; typing ``!potwierdzam`` keeps working;
* ``!mocneodrodzenie`` is a multi-choice list (up to three stats to leave out)
  with **Przelosuj / Anuluj** buttons instead of eight reactions;
* the pet shown by ``!towarzysz`` carries a panel (:class:`PetPanel`) for its
  owner: rename in a pop-up window, the stable, and the pet commands.  Showing
  works on every channel; the gameplay buttons, like the commands, only on the
  boss channel, not while the channel is busy and not for dead hunters.  The
  panel acts only on the pet it shows: once another pet is active its buttons
  are refused (typed commands still mean "the active pet");
* the stable (``!stajnia``) has a list that makes a stabled pet the active one,
  exactly as ``!wyciagnijtowarzysza <slot>`` does - but only the pet the entry
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
  owner's ``!DajPieczec`` / ``!DajRamke`` ever changes it: not growing, the
  mirror (the class never changes), a new name, the stable, a new owner or the
  end of the support;
* the one exception to "ONE roll" (owner's request of 2026-11-22): the egg a
  supporter wins from a special boss (:mod:`altermmo.features.bossvariants`).
  When its roll gave only the base look, the whole roll is repeated - the
  rarer the boss, the more repeats are allowed (``rerolls`` of
  :func:`_roll_egg`).  Still supporters only, still no stats;
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

A pet on a companion mission (:mod:`altermmo.features.missions`, owner's
"Model A" of 2026-11-25) stays where it was - at the player's side or in its
stable place - and is only flagged away by its ``PET_MISSIONS`` row until the
row's ``ENDS_AT``.  This module owns that predicate (:func:`sql_not_away`,
:func:`away_until`, :func:`missions_table_ready`): such a pet gives no fight
bonus (:func:`get_pet_skills`), detects no boss, does not grow, cannot be
rerolled, enlightened, transformed, named, sacrificed, moved or given away;
it still counts as the player's (achievements, the album, the stable).  An
egg that comes meanwhile goes to the pouch, as for any pet at his side.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import random
import time
import types
from datetime import datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, NamedTuple, Sequence

import asyncpg
import discord

from ..core import challenge, clock, io, timing, ui, waiting
from ..settings import ASSETS_DIR, COMMAND_PREFIX
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
           PET_ID_ALT4 NUMERIC DEFAULT 0,
           PET_ID_ALT5 NUMERIC NOT NULL DEFAULT 0,
           PET_ID_ALT6 NUMERIC NOT NULL DEFAULT 0,
           PET_ID_ALT7 NUMERIC NOT NULL DEFAULT 0,
           PET_ID_ALT8 NUMERIC NOT NULL DEFAULT 0,
           PET_ID_ALT9 NUMERIC NOT NULL DEFAULT 0,
           PET_ID_ALT10 NUMERIC NOT NULL DEFAULT 0,
           STABLE_PLACES NUMERIC NOT NULL DEFAULT 4,
           EGG_POUCH BOOLEAN NOT NULL DEFAULT FALSE,
           POUCH_PET_ID NUMERIC NOT NULL DEFAULT 0,
           BIG_POUCH BOOLEAN NOT NULL DEFAULT FALSE,
           POUCH_PET_ID2 NUMERIC NOT NULL DEFAULT 0,
           SUMMON_SCROLLS NUMERIC NOT NULL DEFAULT 0
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
    # The wandering merchant's goods (2026-11-22): stable places 5-10, how many places the
    # player has bought (4 = none), the egg pouch and the egg in it (0 = empty).
    *(("petowner", f"pet_id_alt{place}", "NUMERIC NOT NULL DEFAULT 0") for place in range(5, 11)),
    ("petowner", "stable_places", "NUMERIC NOT NULL DEFAULT 4"),
    ("petowner", "egg_pouch", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("petowner", "pouch_pet_id", "NUMERIC NOT NULL DEFAULT 0"),
    # Companion missions (2026-11-25): the big egg pouch (a second egg), and the summoning scrolls.
    ("petowner", "big_pouch", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("petowner", "pouch_pet_id2", "NUMERIC NOT NULL DEFAULT 0"),
    ("petowner", "summon_scrolls", "NUMERIC NOT NULL DEFAULT 0"),
)
#: The columns of ``PETOWNER`` the merchant's stable places and egg pouch need (all there, or
#: those goods are off and the stable keeps its four places: :func:`upgrades_available`).
UPGRADE_COLUMNS: tuple[str, ...] = (*(f"pet_id_alt{place}" for place in range(5, 11)),
                                    "stable_places", "egg_pouch", "pouch_pet_id")
#: The PETOWNER columns of the companion missions: the Duża sakwa and the summoning scrolls
#: (all there, or those rewards are compensated and !przywolaj is off: :func:`mission_columns_available`).
#: Not part of :data:`UPGRADE_COLUMNS`: a failed migration of these must not switch off stable
#: places 5-10 in the code.
MISSION_COLUMNS: tuple[str, ...] = ("big_pouch", "pouch_pet_id2", "summon_scrolls")
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
#: Owner, 2026-11-22: "Zwiększ szanse x3 na każdym elemencie kosmetycznym" - every seal, foil and
#: rarer frame is three times as likely (seal: none 55%, Tropiciela 30%, Łowcy 14.4%, Arcyłowcy
#: 0.6%; foil: none 96.7%, partial Holo 3%, full Holo 0.3%; frames: ``petcards.POOLS``).
#: Both are ``random.randrange(COSMETIC_ROLL)``, independent of each other and of the pet's
#: class; whole numbers only, so the odds are exact.
COSMETIC_ROLL = 1000
#: Seal roll: the first entry whose bound is above the roll gives the tier -
#: 0-5 Arcyłowcy (0.6%), 6-149 Łowcy (14.4%), 150-449 Tropiciela (30%), 450-999 no seal (55%).
SEAL_ROLL_BELOW: tuple[tuple[int, int], ...] = ((6, 3), (150, 2), (450, 1))
#: Foil roll, the same way: 0-2 full Holo (0.3%), 3-32 partial Holo (3%), 33-999 no foil (96.7%).
FOIL_ROLL_BELOW: tuple[tuple[int, int], ...] = ((3, petcards.FOIL_FULL), (33, petcards.FOIL_PARTIAL))
#: Seal tiers are 0 (none) .. MAX_SEAL; their names are ``petcards.SEAL_NAMES``.
MAX_SEAL = 3
#: The third roll, only for an egg with a class (Shiny / Ultra Shiny / mythic):
#: ``random.randrange(FRAME_ROLL)`` in units of 0.01%, so every percent of the owner's pools
#: (``petcards.POOLS``, down to 0.3%) is a whole number of units - no rounding anywhere.
FRAME_ROLL = 10000
#: Results that are worth a line in the owner's log (:data:`MSG_RARE_COSMETICS`) besides every
#: animated frame: the rarest static frame of each pool (R7 / R8 / R9, 3% of their class since the
#: x3 of 2026-11-22).
RARE_FRAME_PERCENT = 3.0
#: The most repeats of the cosmetic roll one egg can get (the egg of a special boss, see
#: :func:`_roll_egg`); whatever a caller asks for is cut down to this.
MAX_COSMETIC_REROLLS = 10


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

#: Stats a talent point can go to; the order is the order of the ``!mocneodrodzenie`` menu.
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

#: Places of the stable every player has; the wandering merchant sells more, up to
#: :data:`STABLE_MAX_PLACES` (``PETOWNER.STABLE_PLACES``; the table has a column per place).
STABLE_BASE_PLACES = 4
STABLE_MAX_PLACES = 10
_STABLE_SLOTS: dict[int, str] = {place: f"PET_ID_ALT{place}" for place in range(1, STABLE_MAX_PLACES + 1)}

#: ``!mocneodrodzenie``: 1️⃣ .. 7️⃣ (one per entry of SKILL_POOL), shown in the list.
SKILL_EMOTES: tuple[str, ...] = tuple(f"{digit}️⃣" for digit in range(1, 8))
#: What the entries of SKILL_POOL are called in the ``!mocneodrodzenie`` list.
SKILL_LABELS: tuple[str, ...] = (
    "Szansa na krytyczne uderzenie", "Szansa na zastąpienie ataku", "Szansa na zablokowanie ataku",
    "Rzadszy drop", "Zmniejszenie życia przeciwnika", "Spowolnienie przeciwnika", "Wyczuwanie bossa")
#: At most this many stats can be left out of a reroll (10 scrolls each).
MAX_SKIPPED = 3
ACCEPT_EMOTE = "✅"
CONFIRM_WORD = "!potwierdzam"
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
CONFIRM_HINT = "Kliknij **Potwierdzam** albo wpisz **!potwierdzam**."
MSG_ALREADY_WAITING = ("Już czekam na Twoje potwierdzenie. Kliknij **Potwierdzam** (albo wpisz "
                       "**!potwierdzam**) albo poczekaj, aż prośba wygaśnie.")
MSG_NO_PET_DATA = "Nie znaleziono danych towarzysza."
MSG_STORED = "Towarzysz został ulokowany w stajni <:peepoBlush:984769061340737586>"
MSG_UNSTORED = "Towarzysz został wyciągnięty ze stajni <:peepoBlush:984769061340737586>"
MSG_SWAPPED = ("Nowy towarzysz został wyciągnięty ze stajni, a stary został w niej schowany "
               "<:peepoBlush:984769061340737586>")
MSG_EMPTY_SLOT = "W stajni nie ma towarzysza na podanym miejscu. Sprawdź stajnie wpisując **!stajnia**."
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
MSG_OTHER_PET = "Ten panel pokazuje innego towarzysza - wpisz **!towarzysz**, żeby zobaczyć aktualnego."
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


def _msg_bad_slot(player_id: int, command: str, places: int = STABLE_BASE_PLACES) -> str:
    if places <= STABLE_BASE_PLACES:
        return ("Każdy gracz ma tylko cztery miejsca na przechowanie slotów - 1, 2, 3 oraz 4 <@"
                + str(player_id) + ">! Wpisz np. **" + COMMAND_PREFIX + command + " 1**.")
    return (f"Twoja stajnia ma {places} miejsc - od 1 do {places} <@{player_id}>! "
            f"Wpisz np. **{COMMAND_PREFIX}{command} 1**.")


def _scrolls(number: int) -> str:
    """``1 zwój`` / ``20 zwojów`` (the costs of a reroll are 1, 10, 20 or 30)."""
    if number == 1:
        return "1 zwój"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f"{number} zwoje"
    return f"{number} zwojów"


def adv_reroll_text(selected: Sequence[int]) -> str:
    """The ``!mocneodrodzenie`` menu with what is chosen so far and what it costs."""
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


def _only_base_look(rolled: Rolled, *, frames: bool = True) -> bool:
    """True when a cosmetic roll gave nothing but the base look of the class.

    No seal, no foil, the base frame.  Without the frame columns
    (``frames=False``) a partial Holo is no ornament either: it has nowhere to
    be stored (see :func:`_roll_egg`), so the egg would look like any other.
    """
    if rolled.seal or rolled.frame or rolled.foil == petcards.FOIL_FULL:
        return False
    return not frames or rolled.foil != petcards.FOIL_PARTIAL


def _rare_frame(ident: str) -> petcards.Frame | None:
    """The frame of result ``ident`` when the owner wants to hear about it (see :data:`RARE_FRAME_PERCENT`)."""
    frame = petcards.FRAMES.get(ident) if ident else None
    if frame is None or frame.base:
        return None
    return frame if frame.animated or frame.percent <= RARE_FRAME_PERCENT else None


def _slot_number(slot: Any) -> int | None:
    """``slot`` (also given as text) as a whole number; None when it is not one."""
    try:
        return int(str(slot).strip())
    except (TypeError, ValueError):
        return None


def _slot_column(slot: Any, places: int = STABLE_BASE_PLACES) -> str | None:
    """Stable column for ``1``..``places`` (also given as text); None for anything else."""
    number = _slot_number(slot)
    if number is None or not 1 <= number <= min(int(places), STABLE_MAX_PLACES):
        return None
    return _STABLE_SLOTS.get(number)


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


_SQL_OWNER_COLUMNS = """SELECT attname::text FROM pg_attribute
                        WHERE attrelid = to_regclass('petowner') AND attname = ANY($1::text[])
                          AND attnum > 0 AND NOT attisdropped"""


async def upgrades_available(bot: "AlterBot", *, refresh: bool = False) -> bool:
    """Whether ``PETOWNER`` has every :data:`UPGRADE_COLUMNS` (stable places 5-10, the egg pouch).

    Added at start-up like the cosmetic columns (``schema.add_new_columns``, all
    or none).  Without them the game works as before the wandering merchant:
    four stable places, no pouch, and the merchant does not offer those goods.
    Remembered on the bot like :func:`cosmetics_available`; ``refresh=True``
    looks again after a "no".
    """
    known = getattr(bot, "_owner_upgrades", None)
    if known is None or (refresh and not known):
        present = {str(row[0]) for row in await bot.db.fetch(_SQL_OWNER_COLUMNS, list(UPGRADE_COLUMNS))}
        known = present.issuperset(UPGRADE_COLUMNS)
        if not known:
            log.warning("PETOWNER has no %s column(s): the stable keeps four places and there is no egg pouch.",
                        " / ".join(column.upper() for column in UPGRADE_COLUMNS if column not in present))
        bot._owner_upgrades = known  # type: ignore[attr-defined]
    return bool(known)


async def mission_columns_available(bot: "AlterBot", *, refresh: bool = False) -> bool:
    """Whether ``PETOWNER`` has every :data:`MISSION_COLUMNS` (the Duża sakwa, the summoning scrolls).

    Added at start-up like :data:`UPGRADE_COLUMNS` (``schema.add_new_columns``,
    a table's missing columns all or none).  Without them the game works as
    before the companion missions: the pouch holds one egg, a mission's big
    pouch and summoning scroll are compensated, and ``!przywolaj`` is off.
    Remembered on the bot like :func:`upgrades_available`; ``refresh=True``
    looks again after a "no".
    """
    known = getattr(bot, "_mission_columns", None)
    if known is None or (refresh and not known):
        present = {str(row[0]) for row in await bot.db.fetch(_SQL_OWNER_COLUMNS, list(MISSION_COLUMNS))}
        known = present.issuperset(MISSION_COLUMNS)
        if not known:
            log.warning("PETOWNER has no %s column(s): the egg pouch holds one egg and the summoning scrolls "
                        "of the companion missions are off.",
                        " / ".join(column.upper() for column in MISSION_COLUMNS if column not in present))
        bot._mission_columns = known  # type: ignore[attr-defined]
    return bool(known)


async def _owner_shape(bot: "AlterBot") -> tuple[bool, bool]:
    """``(upgrades_available, mission_columns_available)``: what :func:`_owner_columns` may read."""
    return await upgrades_available(bot), await mission_columns_available(bot)


def _owner_columns(upgrades: bool, big: bool = False) -> str:
    """The ``PETOWNER`` columns of the active pet, the stable and (with ``upgrades``) the pouch.

    ``big`` (with ``upgrades``; :func:`mission_columns_available`): also the
    Duża sakwa's flag and its second egg.
    """
    places = STABLE_MAX_PLACES if upgrades else STABLE_BASE_PLACES
    columns = ["PET_ID", *(_STABLE_SLOTS[place] for place in range(1, places + 1))]
    if upgrades:
        columns += ["STABLE_PLACES", "EGG_POUCH", "POUCH_PET_ID"]
        if big:
            columns += ["BIG_POUCH", "POUCH_PET_ID2"]
    return ", ".join(columns)


def _places_of(owner: Any) -> int:
    """How many stable places the player of a ``PETOWNER`` row (read by :func:`_owner_columns`) has."""
    try:
        places = _int(owner["stable_places"])
    except (KeyError, IndexError):
        return STABLE_BASE_PLACES
    return max(STABLE_BASE_PLACES, min(places, STABLE_MAX_PLACES))


#: The egg pouch's columns, slot by slot: the Sakwa na jajko's one egg, then the Duża sakwa's second.
_POUCH_SLOTS: tuple[str, ...] = ("POUCH_PET_ID", "POUCH_PET_ID2")


class Pouch(NamedTuple):
    """The egg pouch of a ``PETOWNER`` row (:func:`_pouch`).

    Capacity 0 without ``EGG_POUCH``, 1 with it (the merchant's Sakwa na jajko)
    and 2 with ``EGG_POUCH`` and ``BIG_POUCH`` (the Duża sakwa of the companion
    missions; only when the row was read with those columns).  The slots are
    ``POUCH_PET_ID``, then ``POUCH_PET_ID2``; each is independent (an egg taken
    from slot 1 does not move the one in slot 2).
    """

    #: 0, 1 or 2.
    size: int = 0
    #: ``(column, PET_ID)`` of the filled slots, in slot order.
    eggs: tuple[tuple[str, int], ...] = ()
    #: The first empty slot's column, or None (no pouch, or it is full).
    free_column: str | None = None


def _pouch(owner: Any) -> Pouch:
    """The :class:`Pouch` of a ``PETOWNER`` row (read by :func:`_owner_columns`); no row or columns: no pouch."""
    if owner is None:
        return Pouch()
    try:
        if owner["egg_pouch"] is not True:
            return Pouch()
        try:
            size = 2 if owner["big_pouch"] is True else 1
        except (KeyError, IndexError):
            size = 1
        slots = [(column, max(_int(owner[column.lower()]), 0)) for column in _POUCH_SLOTS[:size]]
    except (KeyError, IndexError):
        return Pouch()
    return Pouch(size, tuple((column, pet_id) for column, pet_id in slots if pet_id > 0),
                 next((column for column, pet_id in slots if pet_id <= 0), None))


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
    """The ``PETS`` row of the player's active pet, or None (also when it is on a mission: see :func:`_present_pet`)."""
    return await q.fetchrow(_SQL_ACTIVE_PET + (" FOR UPDATE OF p" if lock else ""), player_id)


# -- a pet on a companion mission (features/missions.py) -----------------------------------
#
# "Model A" (owner, 2026-11-25): the pet that went stays in its PETOWNER column; its mission row
# says it is away until ENDS_AT.  Away is derived from the time (``ENDS_AT > now()``, the database's
# clock), so it is back on time whatever the bot does.  While away it gives no fight bonus,
# detects no boss, does not train, does not grow, cannot be changed, moved or sacrificed.
#
# The race rule (spec §7.1): a change checks "away" in a statement of its OWN, after it has taken
# its row lock (the send locks the PETOWNER and the PETS row).  Under READ COMMITTED a NOT EXISTS
# folded into a ``... FOR UPDATE`` (or into the UPDATE itself) that waited for the send's lock is
# re-checked only on the locked row, with the statement's OLD snapshot - it would not see the
# ``away`` row the send just committed.  So :func:`sql_not_away` is for read-only queries only.

#: The missions' table (``missions.TABLE``; this module never imports missions at its top).
_MISSIONS_TABLE = "PET_MISSIONS"
#: "The pet ``{pet}`` (an SQL expression) is on a mission right now."
_SQL_AWAY = ("EXISTS (SELECT 1 FROM PET_MISSIONS m WHERE m.PET_ID = {pet} "
             "AND m.STATUS = 'away' AND m.ENDS_AT > now())")
_SQL_AWAY_UNTIL = """SELECT ENDS_AT FROM PET_MISSIONS
                     WHERE PET_ID = $1 AND STATUS = 'away' AND ENDS_AT > now() LIMIT 1"""
_SQL_AWAY_AMONG = """SELECT PET_ID, ENDS_AT FROM PET_MISSIONS
                     WHERE PET_ID = ANY($1::numeric[]) AND STATUS = 'away' AND ENDS_AT > now()"""
_SQL_AWAY_OF = """SELECT PET_ID, ENDS_AT, OFFERS, CHOSEN FROM PET_MISSIONS
                  WHERE PLAYER_ID = $1 AND STATUS = 'away' AND ENDS_AT > now() LIMIT 1"""
#: The active pet unless it is on a mission (``{away}``: :func:`sql_not_away`).  Read-only.
_SQL_PRESENT_PET = """SELECT p.* FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
                      WHERE o.PLAYER_ID = $1 AND o.PET_ID > 0 AND {away} LIMIT 1"""
#: Seconds after which a "the table is not there" is looked at again.
_READY_RECHECK = 600.0

MSG_PET_AWAY = ("🧭 Twój towarzysz **{name}** jest na zleceniu i wróci {when}, <@{player_id}>. Do tego czasu "
                "nie można go zmieniać ani przenosić.")
MSG_SLOT_AWAY = ("🧭 Towarzysz z miejsca {slot} jest na zleceniu i wróci {when}, <@{player_id}> - do tego czasu "
                 "zostaje w stajni.")
#: The stable's line of a pet on a mission (it has no entry in the list then).
STABLE_AWAY = " - 🧭 na zleceniu, wraca {when}"
#: Under the stable while the pet at the player's side is on a mission: every entry would move it - none.
STABLE_SIDE_AWAY = ("🧭 Dopóki towarzysz przy Tobie nie wróci ze zlecenia, nikogo nie wyjmiesz ze stajni "
                    "ani z sakwy.")
#: Under ``!towarzysz`` when the pet shown is on a mission (``mission``: its name).
MSG_SHOWN_AWAY = "\n🧭 **Na zleceniu:** „{mission}” - wraca {when}"
#: The statuses of a move refused because a pet would have to move although it is on a mission:
#: the active pet (:data:`MSG_PET_AWAY`) / the pet of the stable place in question (:data:`MSG_SLOT_AWAY`).
AWAY, SLOT_AWAY = "away", "slot_away"


class PetAway(Exception):
    """A pet on a mission was about to be sacrificed (:func:`_sacrifice_in`: the last line of defence)."""

    def __init__(self, pet_id: int) -> None:
        super().__init__(f"pet {pet_id} is on a mission")
        self.pet_id = pet_id


class Away(NamedTuple):
    """A pet on a mission, as a refusal tells it: its name and when it is back."""

    name: str
    until: datetime


async def missions_table_ready(bot: "AlterBot", *, refresh: bool = False) -> bool:
    """True when ``PET_MISSIONS`` exists (cached on ``bot._pet_missions``; a "no" is looked at again after 10 minutes).

    Without the table nobody can be away, so every away filter is skipped - a
    fight must never run a statement against a missing table (a failed
    statement aborts the whole PostgreSQL transaction).  A "yes" is final;
    ``refresh=True`` looks again at once (``missions.ensure_table`` has just
    made it).
    """
    known = getattr(bot, "_pet_missions", None)
    if known is not None and (known[0] or (not refresh and clock.monotonic() - known[1] < _READY_RECHECK)):
        return bool(known[0])
    ready = bool(await bot.db.fetchval(f"SELECT to_regclass('{_MISSIONS_TABLE.lower()}') IS NOT NULL"))
    if not ready:
        log.info("Table %s is not there (yet): nobody is on a companion mission.", _MISSIONS_TABLE)
    bot._pet_missions = (ready, clock.monotonic())  # type: ignore[attr-defined]
    return ready


def sql_not_away(pet: str, ready: bool) -> str:
    """The SQL condition "the pet ``pet`` (an SQL expression) is not on a mission"; "TRUE" when the table is missing.

    Read-only queries only (spec §7.1): a change checks with :func:`away_until`
    after its own row lock.
    """
    return f"NOT {_SQL_AWAY.format(pet=pet)}" if ready else "TRUE"


async def _away_ends(q: Any, pet_id: Any, ready: bool) -> datetime | None:
    """When the pet ``pet_id`` is back from its mission, or None (not away, no pet, no table).  ONE statement."""
    pet_id = _int(pet_id)
    if not ready or pet_id <= 0:
        return None
    return await q.fetchval(_SQL_AWAY_UNTIL, pet_id)


async def away_until(bot: "AlterBot", q: Any, pet_id: Any) -> datetime | None:
    """When the pet ``pet_id`` comes back from its mission; None when it is not on one.

    ``q``: the connection of the caller's transaction - after the caller's row
    lock this is the fresh statement of the race rule (spec §7.1) - or
    ``bot.db``.  None without the table (:func:`missions_table_ready`).
    """
    return await _away_ends(q, pet_id, await missions_table_ready(bot))


async def _away_among(bot: "AlterBot", q: Any, pet_ids: Sequence[int]) -> dict[int, datetime]:
    """``{PET_ID: when it is back}`` of those of ``pet_ids`` that are on a mission (one statement)."""
    wanted = [pet_id for pet_id in pet_ids if pet_id > 0]
    if not wanted or not await missions_table_ready(bot):
        return {}
    return {_int(row["pet_id"]): row["ends_at"] for row in await q.fetch(_SQL_AWAY_AMONG, wanted)}


async def away_of(bot: "AlterBot", q: Any, player_id: Any) -> tuple[int, datetime, str] | None:
    """The player's pet on a mission as ``(PET_ID, when it is back, the mission's name)``, or None.  For texts.

    The mission is the offer the player chose (``OFFERS[CHOSEN]``); its name
    comes from the missions' config (``missions.mission_name``, imported
    lazily - missions builds on this module), or is its key when that cannot
    be told.
    """
    if not await missions_table_ready(bot):
        return None
    row = await q.fetchrow(_SQL_AWAY_OF, _pid(player_id))
    if row is None:
        return None
    return _int(row["pet_id"]), row["ends_at"], _mission_name(row["offers"], row["chosen"])


def _mission_name(offers: Any, chosen: Any) -> str:
    """The name of the chosen offer of a mission row (its key when the config does not know it).  Never raises."""
    key = ""
    try:
        key = str(json.loads(offers)[int(chosen)]["key"])
        from . import missions  # (lazily: missions builds on this module)

        name = getattr(missions, "mission_name", None)
        return str(name(key)) if callable(name) else key
    except Exception:  # noqa: BLE001 - a text: the key (or nothing) is enough
        return key or "zlecenie"


async def _present_pet(bot: "AlterBot", q: Any, player_id: int) -> Any:
    """The ``PETS`` row of the player's active pet unless it is on a mission, or None.

    Read-only - there is deliberately no ``lock`` (spec §7.1): a change locks
    with :func:`_active_pet` and asks :func:`away_until` afterwards.
    """
    away = sql_not_away("p.PET_ID", await missions_table_ready(bot))
    return await q.fetchrow(_SQL_PRESENT_PET.format(away=away), player_id)


def _called(pet: Any) -> str:
    """How a refusal names the pet: its name, or its species when it was never named (markdown-safe)."""
    if pet is None:
        return DEFAULT_NAME
    name = str(pet["pet_name"] or "").strip()
    if not name or name == DEFAULT_NAME:
        name = POLISH_TYPES.get(str(pet["type"]), "") or DEFAULT_NAME
    return discord.utils.escape_markdown(name[:60])


def _away_message(player_id: int, name: str, ends_at: datetime) -> str:
    """:data:`MSG_PET_AWAY` for the pet ``name`` that is back at ``ends_at``."""
    return MSG_PET_AWAY.format(name=name, when=ui.when(ends_at), player_id=player_id)


async def _away_check(bot: "AlterBot", q: Any, pet_id: int) -> tuple[str, Any] | None:
    """``(AWAY, Away)`` when the pet ``pet_id`` is on a mission, else None - a statement of its own (spec §7.1).

    The ``check`` of :func:`_confirmed_change` asks this after its row lock.
    """
    until = await away_until(bot, q, pet_id)
    if until is None:
        return None
    return AWAY, Away(_called(await _pet(q, pet_id)), until)


#: The pet ``$1`` is back from a mission nobody has paid yet (``ENDS_AT`` passed, ``STATUS`` still away).
_SQL_UNPAID = "SELECT 1 FROM PET_MISSIONS WHERE PET_ID = $1 AND STATUS = 'away' LIMIT 1"
#: :func:`transform_pet` refused while the mission it would confuse is unpaid (its claim failed just now).
RETURNING = "returning"
MSG_PET_RETURNING = ("🧭 Twój towarzysz właśnie wraca ze zlecenia, <@{player_id}>. Zajrzyj do **!zlecenia**, "
                     "a potem spróbuj ponownie.")


async def _claim_due_mission(scene: "Scene", player_id: int) -> None:
    """The player's mission that has ended but was not claimed yet is paid now (``missions.claim_due``,
    imported lazily: missions builds on this module).  Never raises."""
    from . import missions

    await missions.claim_due(scene, player_id)


async def _mission_unpaid(bot: "AlterBot", q: Any, pet_id: int) -> bool:
    """Whether the pet ``pet_id`` is back from a mission that is not paid yet - a statement of its own.

    :func:`transform_pet` waits for that claim: the claim's level compares the
    ``TYPE`` the pet was sent with (:func:`level_mission_pet_in`).
    """
    if not await missions_table_ready(bot):
        return False
    return await q.fetchval(_SQL_UNPAID, pet_id) is not None


async def _move_refusal(bot: "AlterBot", player_id: int, status: str, slot: int = 0) -> str:
    """The text of a move refused with :data:`AWAY` / :data:`SLOT_AWAY` (``slot``: the stable place).

    Read again (the player's one mission row): the lock that decided is gone
    by now.  When the pet came back in the meantime the stable "changed".
    """
    away = await away_of(bot, bot.db, player_id)
    if away is None:
        return MSG_STABLE_CHANGED
    pet_id, ends_at, _mission = away
    if status == SLOT_AWAY:
        return MSG_SLOT_AWAY.format(slot=slot, when=ui.when(ends_at), player_id=player_id)
    return _away_message(player_id, _called(await _pet(bot.db, pet_id)), ends_at)


class MissionPet(NamedTuple):
    """A pet that can go on a companion mission (:func:`mission_pets`)."""

    #: ``"active:<PET_ID>"`` (at the player's side) or ``"<place>:<PET_ID>"`` (a stable place).
    key: str
    #: 0 = at the player's side ("przy Tobie"), 1..10 = the stable place.
    place: int
    #: The pet's whole ``PETS`` row.
    row: Any


#: The prefix of :attr:`MissionPet.key` for the pet at the player's side.
MISSION_ACTIVE = "active"


async def mission_pets(bot: "AlterBot", player_id: Any) -> tuple[list[MissionPet], int]:
    """The player's pets that can go on a mission now, and how many eggs stay at home.

    The pet at his side and those of his stable places - hatched (an egg
    cannot go), with a ``PETS`` row, not on a mission already; never an egg
    of the pouch.  In place order (the side first).  Read without a lock (a
    list to choose from: :func:`mission_pet_in` decides again).
    """
    player_id = _pid(player_id)
    owner = await _owner(bot.db, player_id, _owner_columns(*await _owner_shape(bot)))
    if owner is None:
        return [], 0
    slots = [(0, _int(owner["pet_id"])),
             *((place, _int(owner[_STABLE_SLOTS[place].lower()])) for place in range(1, _places_of(owner) + 1))]
    wanted = [pet_id for _place, pet_id in slots if pet_id > 0]
    if not wanted:
        return [], 0
    away = sql_not_away("p.PET_ID", await missions_table_ready(bot))
    rows: dict[int, Any] = {}
    for row in await bot.db.fetch(f"SELECT p.* FROM PETS p WHERE p.PET_ID = ANY($1::numeric[]) AND {away}", wanted):
        rows.setdefault(_int(row["pet_id"]), row)
    found, eggs = [], 0
    for place, pet_id in slots:
        row = rows.get(pet_id) if pet_id > 0 else None
        if row is None:
            continue
        if _int(row["pet_lvl"]) < 1:
            eggs += 1
            continue
        found.append(MissionPet(f"{MISSION_ACTIVE if place == 0 else place}:{pet_id}", place, row))
    return found, eggs


async def mission_pet_in(con: Any, bot: "AlterBot", player_id: int, key: str) -> tuple[str, Any, int]:
    """The pet ``key`` (:attr:`MissionPet.key`) that is to go on a mission, inside the caller's transaction.

    Locks the player's ``PETOWNER`` row, then the pet's ``PETS`` row, and asks
    "away" afterwards in a statement of its own (spec §7.1).  Returns
    ``(status, the pet's row, place)``: ``"ok"``; ``"changed"`` (the pet is no
    longer where the key says, or the key is not one); ``"no_data"`` (no
    ``PETS`` row); ``"egg"``; ``"away"`` (on a mission already).
    """
    upgrades, big = await _owner_shape(bot)
    owner = await _owner(con, player_id, _owner_columns(upgrades, big), lock=True)
    where, _, wanted = str(key).partition(":")
    pet_id = _slot_number(wanted) or 0
    place = 0 if where == MISSION_ACTIVE else (_slot_number(where) or -1)
    column = "PET_ID" if place == 0 else _slot_column(place, _places_of(owner)) if owner is not None else None
    if owner is None or pet_id <= 0 or column is None or _int(owner[column.lower()]) != pet_id:
        return "changed", None, 0
    pet = await _pet(con, pet_id, lock=True)
    if pet is None:
        return "no_data", None, place
    if _int(pet["pet_lvl"]) < 1:
        return "egg", pet, place
    if await away_until(bot, con, pet_id):
        return AWAY, pet, place
    return _OK, pet, place


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


def _class_look(pet: Any) -> tuple[int, str]:
    """``(embed colour, the sentence under the pet)`` of the pet's class; ``""`` for an ordinary one."""
    if pet["mythic"]:
        return 0xff4815, "*Mityczny towarzysz.*"
    if pet["ultra_shiny"]:
        return 0xff8d15, "*Absolutnie nadzwyczajny towarzysz.*"
    if pet["shiny"]:
        return 0xffdd00, "*Rzadki towarzysz. Miałeś niesamowite szczęście.*"
    return 0xfffffc, ""


def _pet_embed(player_id: int, pet: Any, items: Any, *, cosmetics: bool = True,
               away: tuple[datetime, str] | None = None) -> discord.Embed:
    """The pet's embed.  ``cosmetics``: whether its ornaments are shown (the owner supports).

    ``away``: the pet is on a companion mission - ``(when it is back, the
    mission's name)``, said after the sentence of its class.  The summoning
    scrolls (``items["summon_scrolls"]``, when read) are listed with the items
    while the player has any.
    """
    summons = 0
    with contextlib.suppress(KeyError, IndexError, TypeError):
        summons = _int(items["summon_scrolls"])
    scroll_desc = (f"\n**💎 PRZEDMIOTY:**\nZwoje odrodzenia: {_int(items['reroll_scroll'])}\n" +
                   f"Fragmenty zwojów: {_int(items['reroll_scroll_shard'])}\n"
                   f"Kamienie olśnienia: {_int(items['rebirth_stones'])}" +
                   f"\nLustra wizualiów: {_int(items['mirrors'])}" +
                   (f"\nZwoje przywołania: {summons}" if summons > 0 else ""))

    color, words = _class_look(pet)
    add_desc = f"\n\n{words}" if words else ""
    if away is not None:
        add_desc += "\n" + MSG_SHOWN_AWAY.format(
            mission=discord.utils.escape_markdown(str(away[1])), when=ui.when(away[0]))

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
        # (No "Wygląd: Standard / Premium / Mityczny" line since 2026-11-23 - the owner: "Usuń
        # wzmiankę o wyglądzie standard premium".  QUALITY stays what picks the picture; a mythic
        # pet still says so below, in its title and by its colour.)
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
    """``(status, items, pet)`` of what ``!towarzysz`` shows; status ``alone`` / ``no_data`` / ``_OK``.

    The items include the summoning scrolls when ``PETOWNER`` has their column
    (:func:`mission_columns_available`).
    """
    columns = _SHOW_COLUMNS + (", SUMMON_SCROLLS" if await mission_columns_available(bot) else "")
    owner = await _owner(bot.db, player_id, columns)
    pet_id = _int(owner["pet_id"]) if owner is not None else 0
    if pet_id <= 0:
        return "alone", owner, None
    pet = await _pet(bot.db, pet_id)
    if pet is None:
        log.warning("Player %s owns pet %s which has no row in PETS.", player_id, pet_id)
        return "no_data", owner, None
    return _OK, owner, pet


def _shown_embed(player_id: int, pet: Any, items: Any, *, cosmetics: bool = True,
                 away: tuple[datetime, str] | None = None) -> tuple[discord.Embed, Path | None]:
    """The pet's embed and its source picture (None: no picture; the embed then has no image).

    The picture is the plain illustration; :func:`_shown` turns it into the card.
    ``away``: see :func:`_pet_embed`.
    """
    embed = _pet_embed(player_id, pet, items, cosmetics=cosmetics, away=away)
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


async def _shown_away(bot: "AlterBot", player_id: int, pet: Any) -> tuple[datetime, str] | None:
    """``(when it is back, the mission's name)`` when the shown pet ``pet`` is on a companion mission, else None."""
    away = await away_of(bot, bot.db, player_id)
    if away is None or away[0] != _int(pet["pet_id"]):
        return None
    return away[1], away[2]


async def _shown(bot: "AlterBot", guild: Any, player: Any, pet: Any, items: Any, *,
                 wait: waiting.Waiting | None = None,
                 away: tuple[datetime, str] | None = None) -> tuple[discord.Embed, Path | None]:
    """The pet's embed and the file to send with it (its card, see :func:`_pet_card`).

    ``player``: the pet's owner (a member or a bare id) - a rarer frame, his
    seal and foil are shown only while he is a supporter (:func:`_is_supporter`).
    The embed's image always names the file returned here.  ``wait``: see
    :func:`_pet_card`.  ``away``: see :func:`_pet_embed`.
    """
    # Only a pet (or an egg) with something to show needs the answer (a bare id costs a lookup).
    cosmetics = ornaments_of(pet).any and await _is_supporter(bot, player)
    embed, source = _shown_embed(_pid(player), pet, items, cosmetics=cosmetics, away=away)
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

    away = await _shown_away(bot, player_id, pet)
    async with waiting.notice(scene.channel) as wait:
        embed, image = await _shown(bot, getattr(scene.channel, "guild", None), player, pet, owner,
                                    wait=wait, away=away)
        files = {} if image is None else {"file_path": image}
        if not buttons:
            await scene.send(embed=embed, **files)
            return True
        panel = PetPanel(player, pet, image=image, away=away is not None)
        try:
            await panel.send(scene.channel, embed=embed, **files)
        except BaseException:
            panel.stop()
            raise
    return True


# -- eggs -----------------------------------------------------------------------------

def _roll_egg(player: Any, *, cosmetics: bool = True, frames: bool = True, rerolls: int = 0,
              min_class: int = 0) -> dict[str, Any]:
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

    ``rerolls`` (owner's request of 2026-11-22: the egg of a special boss, see
    :mod:`altermmo.features.bossvariants`): when the supporter's cosmetic roll
    gave only the base look (:func:`_only_base_look`), the WHOLE roll is
    repeated, up to ``rerolls`` times (at most :data:`MAX_COSMETIC_REROLLS`);
    the first result with an ornament is kept, otherwise the last one.  The
    pools and the odds of a single roll are the usual ones, nothing is rolled
    for a non-supporter whatever the boss, and the egg itself (species, class)
    is not drawn again.

    The cosmetic rolls use ``random.randrange`` and come last, so the legacy
    draws (``randint`` / ``choice``) are exactly what they were - and with
    ``rerolls=0`` so is the whole sequence of draws.

    ``min_class`` (the companion missions' Rzadkie / Nadzwyczajne jajko, e.g.
    :data:`SHINY_FROM` / :data:`ULTRA_SHINY_FROM`): a class roll below it - and
    below :data:`MYTHIC_FROM` - is drawn again from ``min_class ..
    MYTHIC_FROM - 1``, so the egg is at least of that class while a mythic egg
    stays exactly as likely as ever (3 in 1001).  Better by class only: the
    cosmetics still follow the supporter rule.  With ``min_class=0`` nothing
    changes, not even the sequence of draws.  ``player`` None (he left the
    server) is no supporter.
    """
    supporter = player is not None and patrons.check_if_patron(player)
    percentage = random.randint(0, 1000)
    if 0 < min_class and percentage < min_class and percentage < MYTHIC_FROM:
        percentage = random.randint(min(int(min_class), MYTHIC_FROM - 1), MYTHIC_FROM - 1)
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
        if supporter:
            allowed, repeats = min(max(int(rerolls), 0), MAX_COSMETIC_REROLLS), 0
            while repeats < allowed and _only_base_look(rolled, frames=frames):
                rolled = _roll_cosmetics(tier, frames=frames)
                repeats += 1
            if repeats:
                log.info("Special boss: the cosmetic roll of a new %s egg was repeated %d time(s) of %d allowed; "
                         "result: seal %s, foil %s, frame %r.", pet.value, repeats, allowed,
                         rolled.seal, rolled.foil, rolled.frame)
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
                      cosmetics: bool, frames: bool = False, *, kept: bool = True) -> None:
    """After a new egg is saved: remember lost columns, tell the owner about rare cosmetics.

    The best seal, full Holo, an animated frame and the rarest static frame of a
    pool (:data:`RARE_FRAME_PERCENT`) are rare enough for the owner to want to
    know - in ONE message per egg; the player sees them on his egg
    (``!towarzysz``), so nothing is announced.  A partial Holo (1%) is not
    reported.  ``kept=False``: the egg was stored only to be sacrificed in the
    same transaction (:func:`place_egg`) - there is no such ``PET_ID`` in
    ``PETS``, so the owner is told nothing; only the columns are remembered.
    Never raises.
    """
    if frames and "FRAME" not in egg:
        bot._pet_frames = False  # type: ignore[attr-defined]  # see _insert_egg: the columns are gone
    if cosmetics and "SEAL" not in egg:
        bot._pet_cosmetics = bot._pet_frames = False  # type: ignore[attr-defined]
        return
    if not kept:
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
    ``!GenerateEgg``).
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


MSG_EGG_TO_POUCH = ("👝 <@{player_id}>, nowe jajko trafia do sakwy, a Twój towarzysz zostaje przy Tobie. W sakwie "
                    "jajko nie rośnie - żeby je wychować, wyjmij je komendą **!wyjmijjajko** albo z listy pod "
                    "**!stajnia**.")
#: The same for the Duża sakwa (two eggs): ``filled`` - how many of its two slots hold an egg now.
MSG_EGG_TO_BIG_POUCH = ("👝 <@{player_id}>, nowe jajko trafia do sakwy ({filled}/2), a Twój towarzysz zostaje przy "
                        "Tobie. W sakwie jajko nie rośnie - żeby je wychować, wyjmij je komendą **!wyjmijjajko** "
                        "albo z listy pod **!stajnia**.")


async def egg_room(bot: "AlterBot", player_id: Any) -> str:
    """Where a new egg of the player would go: ``"active"``, ``"pouch"`` or ``""`` (nowhere).

    The rule of :func:`assign_pet`, read without a lock (a purchase asks this
    before it takes the scrolls; :func:`assign_pet` decides again under the lock).
    """
    player_id = _pid(player_id)
    owner = await _owner(bot.db, player_id, _owner_columns(*await _owner_shape(bot)))
    if owner is None or _int(owner["pet_id"]) <= 0:
        return "active"
    return "pouch" if _pouch(owner).free_column else ""


#: What :func:`assign_pet` returns when the egg went into the egg pouch.  Truthy like ``True``,
#: but the active pet is still the old one: a pet level rolled in the same drop goes to it
#: (owner, 2026-11-23: an egg in the pouch does not block the level).
INTO_POUCH = "pouch"


async def assign_pet(scene: "Scene", player: Any, *, cosmetic_rerolls: int = 0,
                     album_note: list[str] | None = None) -> bool | str:
    """Give the player a new egg; False when there is no room for it.

    Returns ``True`` when the egg became the active pet and :data:`INTO_POUCH`
    when it went into the egg pouch.

    The egg becomes the active pet of a player without one.  A player who has a
    pet at his side keeps it; the egg goes into his egg pouch when he has one
    (bought from the wandering merchant) and it has room - its first free
    slot: the Duża sakwa of the companion missions holds two eggs - and the
    channel is told so.  Otherwise no egg (as always: the drop is lost).  An egg in the
    pouch is not the active pet, so nothing levels it up.

    ``cosmetic_rerolls``: the egg drops from a special boss - its cosmetic roll
    may be repeated that many times (``rerolls`` of :func:`_roll_egg`; only a
    supporter's egg rolls at all).  Where the egg goes does not matter for it.

    A supporter is then told, in ONE more message, what his album says about
    the egg (owner's request of 2026-11-23, ``album.note_new_egg``: which of
    its stages the album has, as what card, and which this companion can still
    complete) - after everything else the egg brings, also for an egg in the
    pouch, and never at the cost of the egg (:func:`_album_note`).  ``album_note``: the egg is
    part of a drop - the note's text goes into this list, for the drop's embed, instead of a
    message of its own (owner, 2026-11-24).
    """
    bot = scene.bot
    player_id = _pid(player)
    cosmetics, frames = await _cosmetic_state(bot)
    upgrades, big = await _owner_shape(bot)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        owner = await _owner(con, player_id, _owner_columns(upgrades, big))
        pouch: Pouch | None = None
        if owner is not None and _int(owner["pet_id"]) > 0:
            pouch = _pouch(owner)
            if pouch.free_column is None:
                log.debug("Player %s already has a pet (and no room in an egg pouch); no egg assigned.",
                          player_id)
                return False
        egg = _roll_egg(player, cosmetics=cosmetics, frames=frames, rerolls=cosmetic_rerolls)
        pet_id = await _insert_egg(con, egg)
        if pouch is not None:
            await con.execute(f"UPDATE PETOWNER SET {pouch.free_column} = $2 WHERE PLAYER_ID = $1",
                              player_id, pet_id)
        elif owner is not None:
            await con.execute("UPDATE PETOWNER SET PET_ID = $2 WHERE PLAYER_ID = $1", player_id, pet_id)
        else:
            await _insert_owner(con, player_id, pet_id, True, 0, 0, 0, 0, 0)
    log.info("Pet %s assigned to player %s%s.", pet_id, player_id,
             f" (into the egg pouch, {pouch.free_column})" if pouch is not None else "")
    if pouch is not None:
        await _say_safely(scene, MSG_EGG_TO_POUCH.format(player_id=player_id) if pouch.size <= 1 else
                          MSG_EGG_TO_BIG_POUCH.format(player_id=player_id, filled=len(pouch.eggs) + 1))
    # A mythic pet (with Holo / the rarest seal) and, for a supporter, his album's note; never raises.
    await after_new_egg(scene, player, player_id, pet_id, egg, cosmetics, frames, announce=True, notes=album_note)
    return INTO_POUCH if pouch is not None else True


async def after_new_egg(scene: "Scene", member: Any, player_id: int, pet_id: int, egg: dict[str, Any],
                        cosmetics: bool, frames: bool, *, announce: bool, notes: list[str] | None = None,
                        album: bool = True, kept: bool = True) -> None:
    """The hooks of an egg that was just stored for the player - after the commit.  Never raises.

    :func:`_egg_stored` (lost columns, the owner's word about rare cosmetics),
    the announcement of a mythic egg (only with ``announce``),
    ``achievements.check_pets`` (announced with ``announce``) and - with
    ``album`` - a supporter's album note (:func:`_album_note`; ``notes``: its
    text goes into this list instead of a message of its own).  ``member``: the
    member, or None / a bare id (he left the server: no supporter, and the
    trophies are checked by id).  ``kept=False``: the egg was stored only to be
    sacrificed in the same transaction (:func:`place_egg`) - only the columns are
    remembered.  Whatever fails here is logged: the egg is the player's already.
    Used by :func:`assign_pet`, :func:`place_egg` and the companion missions.
    """
    player = member if member is not None else player_id
    try:
        await _egg_stored(scene.bot, player_id, pet_id, egg, cosmetics, frames, kept=kept)
        if not kept:
            return
        if egg["MYTHIC"] and announce:
            await _announce_mythic(scene, player_id)
        await achievements.check_pets(scene, player, announce=announce)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - committed: the egg is the player's whatever a hook does
        log.exception("Egg %s of player %s: a hook after the commit failed.", pet_id, player_id)
    if album:
        await _album_note(scene, player, egg, into=notes)  # supporters only; never raises


async def _album_note(scene: "Scene", player: Any, egg: dict[str, Any], *, into: list[str] | None = None) -> None:
    """Tell a supporter what his album says about the egg he was just given.  Never raises.

    The egg is stored and announced before this is called; whatever goes wrong
    here (``album.note_new_egg`` logs its own troubles) costs only the note.
    No random draw is made, so the draws of a drop are what they were.
    """
    try:
        from . import album  # (lazily: the album builds on this module)

        await album.note_new_egg(scene, player, egg, into=into)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the egg is the player's already
        log.exception("Player %s: the album note about the new egg failed.", getattr(player, "id", player))


async def _say_safely(scene: "Scene", text: str) -> None:
    """Post ``text``; a failure is only logged (what it reports has happened already)."""
    try:
        await scene.send(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not post %r: %s: %s", text[:60], type(exc).__name__, exc)


async def give_egg_pouch(bot: "AlterBot", player_id: Any) -> bool:
    """The wandering merchant's egg pouch: True when the player got it, False when he has one already.

    A pouch is for ever and holds one egg; a player has at most one (a
    companion mission can make it the Duża sakwa, :func:`upgrade_pouch_in` - the
    merchant sells only the first pouch).  Needs :func:`upgrades_available` (the
    caller checks it).
    """
    player_id = _pid(player_id)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        tag = await con.execute(
            "UPDATE PETOWNER SET EGG_POUCH = TRUE WHERE PLAYER_ID = $1 AND EGG_POUCH = FALSE", player_id)
    granted = tag != "UPDATE 0"
    if granted:
        log.info("Player %s got the egg pouch.", player_id)
    return granted


async def owned_pet_ids(bot: "AlterBot", player_id: Any) -> list[int]:
    """Every pet of the player: the active one, the stable places and the eggs in the pouch (both slots)."""
    owner = await _owner(bot.db, _pid(player_id), _owner_columns(*await _owner_shape(bot)))
    if owner is None:
        return []
    ids = [_int(owner["pet_id"]),
           *(_int(owner[_STABLE_SLOTS[place].lower()]) for place in range(1, _places_of(owner) + 1)),
           *(pet_id for _column, pet_id in _pouch(owner).eggs)]
    return [pet_id for pet_id in ids if pet_id > 0]


async def pouch_size(bot: "AlterBot", player_id: Any) -> int:
    """How many eggs the player's pouch holds: 0 (none), 1 (Sakwa na jajko) or 2 (Duża sakwa) - for texts."""
    upgrades, big = await _owner_shape(bot)
    if not upgrades:
        return 0
    return _pouch(await _owner(bot.db, _pid(player_id), _owner_columns(upgrades, big))).size


async def has_egg_pouch(bot: "AlterBot", player_id: Any) -> bool:
    """True when the player owns the egg pouch - also the Duża sakwa (False when the columns are missing)."""
    if not await upgrades_available(bot):
        return False
    return bool(await bot.db.fetchval(
        "SELECT BOOL_OR(EGG_POUCH) FROM PETOWNER WHERE PLAYER_ID = $1", _pid(player_id)))


async def stable_places(bot: "AlterBot", player_id: Any) -> int:
    """How many stable places the player has (:data:`STABLE_BASE_PLACES` without a row or the columns)."""
    if not await upgrades_available(bot):
        return STABLE_BASE_PLACES
    places = await bot.db.fetchval("SELECT MAX(STABLE_PLACES) FROM PETOWNER WHERE PLAYER_ID = $1",
                                   _pid(player_id))
    return max(STABLE_BASE_PLACES, min(_int(places) or STABLE_BASE_PLACES, STABLE_MAX_PLACES))


async def add_stable_place(bot: "AlterBot", player_id: Any, limit: int = STABLE_MAX_PLACES) -> int:
    """One more stable place (the wandering merchant); the new number, or 0 at ``limit`` already.

    ``limit`` is cut to :data:`STABLE_MAX_PLACES` (one column per place).
    Needs :func:`upgrades_available` (the caller checks it).
    """
    player_id = _pid(player_id)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        return await add_stable_place_in(con, player_id, limit)


async def add_stable_place_in(con: Any, player_id: int, limit: int = STABLE_MAX_PLACES) -> int:
    """:func:`add_stable_place` inside the caller's transaction (he holds the player's lock).

    The new number of places, or 0 at ``limit`` already or without a
    ``PETOWNER`` row.  Needs :func:`upgrades_available` (the caller checks it).
    """
    limit = min(int(limit), STABLE_MAX_PLACES)
    places = await con.fetchval(
        """UPDATE PETOWNER SET STABLE_PLACES = GREATEST(STABLE_PLACES, $3) + 1
           WHERE PLAYER_ID = $1 AND GREATEST(STABLE_PLACES, $3) < $2 RETURNING STABLE_PLACES""",
        player_id, limit, STABLE_BASE_PLACES)
    if places is None:
        return 0
    log.info("Player %s has %s stable places now.", player_id, _int(places))
    return _int(places)


async def upgrade_pouch_in(con: Any, player_id: int, *, upgrades: bool, big: bool) -> str:
    """The companion missions' "Ulepszenie sakwy", inside the caller's transaction (player's lock held).

    No pouch yet: the Sakwa na jajko (``"pouch"``, needs ``upgrades``); the
    Sakwa already: the Duża sakwa, a second egg (``"big"``, needs ``big`` -
    :func:`mission_columns_available`).  ``""`` when nothing could be given (the
    Duża sakwa already, the columns are missing, or no ``PETOWNER`` row: the
    caller compensates, and that creates the row).
    """
    if not upgrades:
        return ""
    if await con.fetchval("UPDATE PETOWNER SET EGG_POUCH = TRUE WHERE PLAYER_ID = $1 AND EGG_POUCH = FALSE "
                          "RETURNING 1", player_id):
        log.info("Player %s got the egg pouch (a mission).", player_id)
        return "pouch"
    if big and await con.fetchval("UPDATE PETOWNER SET BIG_POUCH = TRUE WHERE PLAYER_ID = $1 AND EGG_POUCH "
                                  "AND NOT BIG_POUCH RETURNING 1", player_id):
        log.info("Player %s got the big egg pouch (a mission).", player_id)
        return "big"
    return ""


# -- the summoning scrolls (the companion missions, the drops: features/missions.py, features/items.py) --
#
# No cap (owner, 2026-11-26: "no caps anywhere"): a player may hold any number of them.  The
# missions' ``summon_cap`` is an ignored key since then (features/missions.py).

async def add_summon_scroll_in(con: Any, player_id: int, number: int = 1) -> int:
    """``number`` more Zwoje przywołania, inside the caller's transaction; the new count.

    The caller holds the player's lock (:func:`_lock_player`); a player without
    a ``PETOWNER`` row gets one (a drop can bring a scroll to somebody who has
    never had a pet).  Needs :func:`mission_columns_available` (the caller
    checks it).
    """
    number = max(int(number), 0)
    sql = """UPDATE PETOWNER SET SUMMON_SCROLLS = SUMMON_SCROLLS + $2
             WHERE PLAYER_ID = $1 RETURNING SUMMON_SCROLLS"""
    count = await con.fetchval(sql, player_id, number)
    if count is None:
        await _insert_owner(con, player_id, 0, False, 0, 0, 0, 0, 0)
        count = await con.fetchval(sql, player_id, number)
    return _int(count)


async def summon_scrolls(bot: "AlterBot", player_id: Any) -> int:
    """How many Zwoje przywołania the player has (0 without a row or the columns)."""
    if not await mission_columns_available(bot):
        return 0
    return _int(await bot.db.fetchval("SELECT MAX(SUMMON_SCROLLS) FROM PETOWNER WHERE PLAYER_ID = $1",
                                      _pid(player_id)))


async def take_summon_scroll(bot: "AlterBot", player_id: Any) -> int | None:
    """Use one Zwój przywołania, atomically: the count left, or None when the player has none.

    Needs :func:`mission_columns_available` (the caller checks it).  Never
    retried (``idempotent=False``): a reply lost after the commit must not take
    a second scroll - it raises ``DatabaseUnavailable`` instead.
    """
    left = await bot.db.fetchval(
        """UPDATE PETOWNER SET SUMMON_SCROLLS = SUMMON_SCROLLS - 1
           WHERE PLAYER_ID = $1 AND SUMMON_SCROLLS > 0 RETURNING SUMMON_SCROLLS""", _pid(player_id),
        idempotent=False)
    return None if left is None else _int(left)


async def return_summon_scroll(bot: "AlterBot", player_id: Any) -> None:
    """Give back the scroll :func:`take_summon_scroll` took (the summon did not happen).

    (There is no cap to mind: the player had it a moment ago.)  Raises when the
    database fails (the caller tells the owner).
    """
    await bot.db.execute("UPDATE PETOWNER SET SUMMON_SCROLLS = SUMMON_SCROLLS + 1 WHERE PLAYER_ID = $1",
                         _pid(player_id))


async def give_summon_scrolls(bot: "AlterBot", player_id: Any, number: int) -> int:
    """Add ``number`` Zwoje przywołania (a drop, the owner's commands); the new count.

    No cap; the player's row is created when he has none.  A negative
    ``number`` (``!DajZwojPrzywolania`` / ``!DajPrzedmiot`` with a minus) takes
    scrolls back, never below 0 (and makes no row).  In one transaction under
    the player's lock, never retried.  Needs :func:`mission_columns_available`
    (the caller checks it).
    """
    player_id, number = _pid(player_id), int(number)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        if number >= 0:
            count = await add_summon_scroll_in(con, player_id, number)
        else:
            count = await con.fetchval(
                """UPDATE PETOWNER SET SUMMON_SCROLLS = GREATEST(SUMMON_SCROLLS + $2, 0)
                   WHERE PLAYER_ID = $1 RETURNING SUMMON_SCROLLS""", player_id, number)
    log.info("Player %s has %s summoning scroll(s) now (%+d).", player_id, _int(count), number)
    return _int(count)


async def owner_counters(bot: "AlterBot", player_id: Any) -> dict[str, int]:
    """The player's item counters of ``PETOWNER`` (``!przedmioty``, features/items.py); zeros without a row.

    Keys: ``reroll_scroll``, ``reroll_scroll_shard``, ``rebirth_stones``,
    ``mirrors`` and - when the column is there (:func:`mission_columns_available`)
    - ``summon_scrolls``.  Read-only: the shards are not turned into scrolls
    here (``!towarzysz`` does that).
    """
    columns = ["REROLL_SCROLL", "REROLL_SCROLL_SHARD", "REBIRTH_STONES", "MIRRORS"]
    if await mission_columns_available(bot):
        columns.append("SUMMON_SCROLLS")
    owner = await _owner(bot.db, _pid(player_id), ", ".join(columns))
    return {column.lower(): (_int(owner[column.lower()]) if owner is not None else 0) for column in columns}


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
    """Set the seal (and the foil, unless ``holo`` is None) of the player's active pet (``!DajPieczec``).

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
    Allowed for a pet on a companion mission too: it is the owner's, and only
    the looks change.
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


#: What ``!DajRamke`` takes for "the base frame of the class" (stored as '').
BASE_FRAME_WORDS = frozenset({"baza", "bazowa", "podstawowa", "base", "brak", "0", "-"})


async def set_frame(bot: "AlterBot", player_id: Any, frame: Any) -> tuple[str, dict[str, Any] | None]:
    """Set the frame result of the player's active pet (``!DajRamke``).

    ``frame``: a result id of the pet's own class (``S1``, ``s1-a``, the base id
    itself) or one of :data:`BASE_FRAME_WORDS` - the base is stored as ''.
    Returns ``(status, data)``: ``no_frame_columns`` (see
    :func:`frames_available`, looked up again), ``alone`` (no active pet),
    ``no_tier`` (an ordinary pet: no frame at all), ``bad_frame`` with
    ``tier`` (the id is not a result of the pet's class: the caller lists
    ``petcards.POOLS[tier]``) or ``_OK`` with ``pet_id`` / ``tier`` / ``frame``
    (the stored id, '' = base).  Allowed for a pet on a companion mission too
    (the owner's command, the looks only).
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
    specimen, not to its owner, so nothing is rolled here.  A pet on a
    companion mission is not reassigned (False, as for every refusal).
    """
    pet_id, player_id = int(pet_id), _pid(player_id)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        if await _pet(con, pet_id, lock=True) is None:
            log.info("Pet %s does not exist; nothing reassigned.", pet_id)
            return False
        if await away_until(bot, con, pet_id):  # (after the pet's lock, a statement of its own)
            log.info("Pet %s is on a mission; not reassigned.", pet_id)
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
    if column not in _ITEM_COLUMNS:
        raise ValueError(f"not an item counter: {column}")
    player_id, number = _pid(player_id), int(number)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        await add_items_in(con, player_id, **{_ITEM_KEYWORDS[column]: number})
    return True


#: The keyword of :func:`add_items_in` for each counter of :data:`_ITEM_COLUMNS`.
_ITEM_KEYWORDS: dict[str, str] = dict(zip(_ITEM_COLUMNS, ("scrolls", "shards", "stones", "mirrors")))


async def add_items_in(con: Any, player_id: int, *, scrolls: int = 0, shards: int = 0, stones: int = 0,
                       mirrors: int = 0) -> None:
    """Add to the player's item counters in ONE statement, inside the caller's transaction.

    The caller holds the player's lock (:func:`_lock_player`); a player without
    a ``PETOWNER`` row gets one with these counters.
    """
    numbers = (int(scrolls), int(shards), int(stones), int(mirrors))
    tag = await con.execute(
        "UPDATE PETOWNER SET " + ", ".join(f"{column} = COALESCE({column}, 0) + ${index}"
                                           for index, column in enumerate(_ITEM_COLUMNS, start=2))
        + " WHERE PLAYER_ID = $1", player_id, *numbers)
    if tag == "UPDATE 0":
        await _insert_owner(con, player_id, 0, False, *numbers, 0)


async def take_scrolls(bot: "AlterBot", player_id: Any, number: int) -> tuple[bool, int]:
    """Spend ``number`` reroll scrolls of the player, atomically (a purchase from the merchant).

    Returns ``(paid, scrolls)``: ``(True, what is left)``, or ``(False, what he
    has)`` when that is not enough - nothing is taken then (0 without a row).
    """
    player_id, number = _pid(player_id), int(number)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        left = await take_scrolls_in(con, player_id, number)
        if left is not None:
            return True, left
        return False, _int(await con.fetchval(
            "SELECT MAX(COALESCE(REROLL_SCROLL, 0)) FROM PETOWNER WHERE PLAYER_ID = $1", player_id))


async def take_scrolls_in(con: Any, player_id: int, number: int) -> int | None:
    """Spend ``number`` reroll scrolls inside the caller's transaction: what is left, or None (not enough)."""
    left = await con.fetchval(
        """UPDATE PETOWNER SET REROLL_SCROLL = REROLL_SCROLL - $2
           WHERE PLAYER_ID = $1 AND REROLL_SCROLL >= $2 RETURNING REROLL_SCROLL""", player_id, int(number))
    return None if left is None else _int(left)


async def return_scrolls(bot: "AlterBot", player_id: Any, number: int) -> bool:
    """Give back scrolls taken by :func:`take_scrolls` (a purchase that could not be completed)."""
    return await _add_item(bot, "REROLL_SCROLL", number, player_id)


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

async def level_up_pet(scene: "Scene", player_id: Any, *, album_note: list[str] | None = None) -> bool:
    """Level up the player's pet (an egg hatches); False without a pet or at the top level.

    ``album_note`` (a drop: ``boss.rand_loot`` / ``daily.rand_loot``; ``player_id`` is then the
    member): a supporter's album note about the pet at its new stage is added to the list for
    the drop's embed (owner, 2026-11-24; ``album.note_level_up``).  Never at the cost of the level.

    A pet on a companion mission does not grow: False (a drop leaves its level
    line out).  The lock comes first and "away" is asked in a statement of its
    own (spec §7.1: a NOT EXISTS inside the ``FOR UPDATE OF p`` that waited for
    the send's lock would not see the send, and would level the pet just sent).
    """
    player = player_id
    player_id = _pid(player_id)
    async with scene.bot.db.transaction() as con:
        pet = await _active_pet(con, player_id, lock=True)
        if pet is None or _int(pet["pet_lvl"]) >= MAX_LEVEL:
            return False
        if await away_until(scene.bot, con, pet["pet_id"]):
            log.info("Pet %s of player %s is on a mission; no level.", pet["pet_id"], player_id)
            return False
        level, add_skill = await _grow_in(con, pet)
    log.info("Pet %s of player %s reached level %s (+%s talent).", pet["pet_id"], player_id, level, add_skill)
    if album_note is not None:
        await _level_note(scene, player, player_id, album_note)
    return True


async def _grow_in(con: Any, pet: Any) -> tuple[int, int]:
    """One level for the (locked, below :data:`MAX_LEVEL`) pet ``pet``: ``(new level, talent added)``.

    The rule of every level-up: 2 talent points for a mythic pet, else 1-2,
    each spent by :func:`_roll_skill`; an egg that hatches keeps one of the two
    looks of a hatched pet.
    """
    level = _int(pet["pet_lvl"])
    variant = _int(pet["variant"])
    if level == 0:  # an egg has one of 11 looks, a hatched pet one of 2
        variant %= 2
    add_skill = 2 if pet["mythic"] else random.randint(1, 2)
    stats = _stats_of(pet)
    for _ in range(add_skill):
        _roll_skill(stats, SKILL_POOL, pet)
    await _save_pet(con, pet, stats, level=level + 1, skills=_int(pet["pet_skills"]) + add_skill, variant=variant)
    return level + 1, add_skill


async def level_mission_pet_in(con: Any, player_id: int, pet_id: Any, *, upgrades: bool, big: bool = False,
                               species: str) -> tuple[Any, int] | None:
    """The companion missions' level for the pet that went: ``(its PETS row after, new level)`` or None.

    Inside the caller's transaction (he holds the player's lock).  The pet must
    still be the player's - at his side or in one of his stable places, never in
    the pouch - hatched and below :data:`MAX_LEVEL`, and of ``species`` (the
    ``TYPE`` it had when it was sent: ``_insert_egg`` reuses the highest
    ``PET_ID``, so a pet sacrificed after its return may have left its id to a
    new one).  Otherwise None: the caller compensates.  The growth is the one
    of :func:`level_up_pet` (:func:`_grow_in`).
    """
    pet_id = _int(pet_id)
    if pet_id <= 0:
        return None
    owner = await _owner(con, player_id, _owner_columns(upgrades, big), lock=True)
    if owner is None:
        return None
    columns = ["pet_id", *(_STABLE_SLOTS[place].lower() for place in range(1, _places_of(owner) + 1))]
    if all(_int(owner[column]) != pet_id for column in columns):
        return None
    pet = await _pet(con, pet_id, lock=True)
    if pet is None or not 1 <= _int(pet["pet_lvl"]) < MAX_LEVEL or str(pet["type"]) != str(species):
        return None
    level, add_skill = await _grow_in(con, pet)
    log.info("Pet %s of player %s reached level %s on a mission (+%s talent).", pet_id, player_id, level, add_skill)
    return await _pet(con, pet_id), level


async def _level_note(scene: "Scene", player: Any, player_id: int, into: list[str]) -> None:
    """The album's note about the pet that has just levelled up, for the drop's embed.  Never raises;
    draws nothing at random (the level's own draws are made already)."""
    try:
        from . import album  # (lazily: the album builds on this module)

        pet = await _active_pet(scene.bot.db, player_id)
        await album.note_level_up(scene, player, pet, into=into)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the level is the player's already
        log.exception("Player %s: the album note about the level failed.", player_id)


async def get_pet_skills(bot: "AlterBot", player_id: Any) -> dict[str, Any]:
    """Stats of the player's active pet for a fight (zeros when he has none).

    Always returns every key of :data:`NO_PET_SKILLS` with plain ``int`` /
    ``float`` values (``DETECTION`` is a bool).  A pet on a companion mission
    is not at his side: zeros (:func:`_present_pet`) - in every fight, the
    hunt, the tower and the drop boost alike.
    """
    skills = dict(NO_PET_SKILLS)
    pet = await _present_pet(bot, bot.db, _pid(player_id))
    if pet is not None:
        for name in skills:
            skills[name] = bool(pet["detection"]) if name == "DETECTION" else _num(pet[name.lower()])
    return skills


# -- changes the player has to confirm ------------------------------------------------

#: Players the bot is waiting for (one ``!potwierdzam`` question at a time).
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
    """Ask ``prompt`` with Potwierdzam / Anuluj buttons; 15 s for a click or a typed ``!potwierdzam``.

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
    """Common flow of ``!odrodzenie``, ``!oswiecenie`` and ``!transformacja``.

    ``check(q, lock)`` says whether the change is possible (``_OK``) or which of
    ``messages`` explains why not.  It runs before the question and once more,
    with the rows locked, in the transaction that applies the change - whatever
    happened during the 15 seconds, nothing is paid for twice or with items
    that are gone.  The status ``other_pet`` (see :func:`_other_pet`) is
    answered with :data:`MSG_OTHER_PET`, and :data:`AWAY` (the pet is on a
    companion mission; ``check`` returns ``(AWAY, Away)``, see
    :func:`_away_check`) with :data:`MSG_PET_AWAY` - shared by every caller.

    Nothing at all happens while a typed fight is on the channel
    (``challenge.fight_on``; owner's rule: nothing may post into a running
    fight) - the typed ``!oswiecenie`` / ``!odrodzenie`` / ``!transformacja``
    then stay silent, as the buttons under the pet do.
    """
    bot = scene.bot
    player_id = _pid(player)
    if challenge.fight_on(getattr(scene, "channel", None)):
        log.info("Pet change of %s asked during a typed fight: nothing posted, nothing changed.", player_id)
        return False
    messages = {"other_pet": MSG_OTHER_PET, **messages}

    def refusal(status: str, data: Any) -> str:
        if status == AWAY and isinstance(data, Away):
            return _away_message(player_id, data.name, data.until)
        return messages[status]

    status, data = await check(bot.db, False)
    if status != _OK:
        await scene.send(refusal(status, data))
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
        await scene.send(refusal(status, data))
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
        if away := await _away_check(scene.bot, q, pet_id):  # (after the owner lock, a statement of its own)
            return away
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
                     "Wpisz **!towarzysz**, żeby sprawdzić ich ilość. Zwoje możesz zdobyć na polowaniu "
                     "lub po zabiciu bossów."),
        })


class SkillMenu(ui.ClickView):
    """The ``!mocneodrodzenie`` menu: a multi-choice list of SKILL_POOL, Przelosuj and Anuluj.

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

    A pet on a companion mission is refused before the menu opens (the menu
    would hold the channel for nothing); :func:`reroll_pet` checks again, locked.
    """
    user = scene.author
    pet = await _active_pet(scene.bot.db, _pid(user))
    if pet is not None and (until := await away_until(scene.bot, scene.bot.db, pet["pet_id"])):
        await scene.send(_away_message(_pid(user), _called(pet), until))
        return False
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

    A companion mission of the pet that has ended but was not paid yet is
    paid first (:func:`_claim_due_mission`): its level reward knows the pet
    by the ``TYPE`` it was sent with - a transform before the claim would turn
    the level into its compensation.  While it stays unpaid (the claim failed)
    the transform is refused (:data:`MSG_PET_RETURNING`).
    """
    player_id = _pid(player)
    mention = "<@" + str(player_id) + ">"
    await _claim_due_mission(scene, player_id)

    async def check(q: Any, lock: bool) -> tuple[str, Any]:
        owner = await _owner(q, player_id, "PET_ID, MIRRORS", lock=lock)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id <= 0:
            return "alone", None
        if _other_pet(pet_id, expected_pet):
            return "other_pet", None
        if away := await _away_check(scene.bot, q, pet_id):  # (after the owner lock, a statement of its own)
            return away
        if await _mission_unpaid(scene.bot, q, pet_id):
            return RETURNING, None
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
            RETURNING: MSG_PET_RETURNING.format(player_id=player_id),
            "mythic": "Mitycznego towarzysza nie można transformować, " + mention + "!",
            "no_rank": ("Musisz mieć rangę Patrona, Craftera, subskrybenta Twitch lub wspierającego na "
                        "YouTube " + mention + ", żeby przetransformować "
                        "towarzysza <:Sadge:936907659142111273> Rangi do zdobycia możesz zobaczyć na tym "
                        "kanale <#688296443156365354>."),
            "poor": ("Nie masz żadnych lustr wizualiów " + mention + " <:Sadge:936907659142111273> "
                     "Wpisz **!towarzysz**, żeby sprawdzić ich ilość. Lustra możesz zdobyć na polowaniu "
                     "lub po zabiciu bossów, jednak są one bardzo rzadkie."),
        })


async def enlight_pet(scene: "Scene", player: Any, *, expected_pet: int | None = None) -> bool:
    """Enlighting pet (1-3 more talent points for a level 3 pet) for a rebirth stone.

    ``expected_pet``: as in :func:`reroll_pet`.
    """
    player_id = _pid(player)
    mention = "<@" + str(player_id) + ">"
    unworthy = ("Niestety Twój towarzysz nie jest godny dostąpić oświecenia " + mention
                + " <:Sadge:936907659142111273> Wpisz **!towarzysz**, żeby sprawdzić jego poziom.")

    async def check(q: Any, lock: bool) -> tuple[str, Any]:
        owner = await _owner(q, player_id, "PET_ID, REBIRTH_STONES", lock=lock)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id <= 0:
            return "alone", None
        if _other_pet(pet_id, expected_pet):
            return "other_pet", None
        if away := await _away_check(scene.bot, q, pet_id):  # (after the owner lock, a statement of its own)
            return away
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
                            + " <:prayge:1063891597760139304> Wpisz **!towarzysz**, żeby sprawdzić jego talent."),
            "poor": ("Nie masz żadnych kamieni olśnienia " + mention + " <:Sadge:936907659142111273> "
                     "Wpisz **!towarzysz**, żeby sprawdzić ich ilość. Kamienie olśnienia możesz zdobyć na "
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

    A pet on a companion mission is not sacrificed: refused before the
    question and again in the transaction, after the owner lock.
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
        if away := await _away_check(bot, bot.db, _int(owner["pet_id"])):  # (a pet on a mission stays)
            await scene.send(_away_message(player_id, away[1].name, away[1].until))
            return False
        ready = await missions_table_ready(bot)
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
        away: tuple[str, Any] | None = None
        try:
            async with bot.db.transaction() as con:
                owner = await _owner(con, player_id, "PET_ID", lock=True)
                pet_id = _int(owner["pet_id"]) if owner is not None else 0
                other = _other_pet(pet_id, expected_pet)
                if pet_id > 0 and not other:
                    # Sent on a mission during the question?  (After the owner lock, a statement of its own.)
                    away = await _away_check(bot, con, pet_id)
                if pet_id > 0 and not other and away is None:
                    # NotSaved (and PetAway, which the check above rules out): everything is rolled back
                    kept = await _sacrifice_in(con, player_id, pet_id, keeps, ready=ready)
                    await con.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", player_id)
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
        if away is not None:
            log.info("Player %s: discard of pet %s refused, it went on a mission.", player_id, pet_id)
            await scene.send(_away_message(player_id, away[1].name, away[1].until))
            return False
        log.info("Player %s discarded pet %s%s.", player_id, pet_id,
                 "" if kept is None else f" (kept in the album as entry {kept})")
        await _sacrificed(scene, player, kept, answer=answer)
    return True


async def _sacrifice_in(con: Any, player_id: int, pet_id: int, keeps: bool, *, ready: bool = False) -> int | None:
    """The sacrifice of one pet inside the caller's transaction: its row leaves ``PETS``.

    ``keeps`` (the owner supports at this moment): the row goes into his album
    first (``album.keep``) and the new ``ENTRY_ID`` is returned; otherwise None
    - the pet is gone, as it always was.  ``album.NotSaved`` passes through:
    the caller's transaction is rolled back and nothing was sacrificed.  The
    caller empties the place the pet stood on (``PETOWNER``) in the same
    transaction.  One path for every sacrifice of the game: ``!porzucam``
    (:func:`discard_pet`) and the mine's egg decisions (:func:`place_egg`).

    ``ready`` (:func:`missions_table_ready`; there is no bot here): a pet on a
    companion mission raises :class:`PetAway` - the last line of defence, both
    callers refuse such a pet before.
    """
    from . import album  # (lazily: the album builds on this module)

    if await _away_ends(con, pet_id, ready):
        raise PetAway(pet_id)
    pet = await _pet(con, pet_id, lock=True) if keeps else None
    kept = await album.keep(con, player_id, pet) if pet is not None else None
    await con.execute("DELETE FROM PETS WHERE PET_ID = $1", pet_id)
    return kept


async def _sacrificed(scene: "Scene", player: Any, kept: int | None, *, answer: ui.Confirmation | None = None) -> None:
    """What follows every committed sacrifice: the random boss modifier, then the album's word.

    ``answer``: the confirmation of ``!porzucam`` (gets the legacy "seen"
    reaction).  The pet IS in the album once the sacrifice is committed,
    whatever the modifier step does - so the player is told and "Pełny album"
    is checked even when that step fails (its error, e.g. Discord down, still
    goes on to the caller).
    """
    from . import album

    try:
        await modifiers.random_modifiers(scene, True, True)
        if answer is not None:
            await _thank(answer, "<:MonkaS:882181709100097587>")
    finally:
        if kept is not None:
            await album.saved(scene, player, kept)  # the answer and the album's achievement; never raises


async def name_pet(scene: "Scene", name: str, *, expected_pet: int | None = None) -> bool:
    """Set name of the author's pet (15% chance that it turns shiny); False without a pet.

    ``expected_pet``: the pet a panel was showing; False (nothing named) when
    another pet is active.

    A pet on a companion mission is not named (a name can make it shiny):
    False with :data:`MSG_PET_AWAY`.  One transaction: the owner's row is
    locked first and "away" is asked in a statement of its own (spec §7.1).
    """
    bot = scene.bot
    player_id = _pid(scene.author)
    name = str(name)[:MAX_NAME_LENGTH]
    away: tuple[str, Any] | None = None
    shiny = False
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, "PET_ID", lock=True)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id > 0 and not _other_pet(pet_id, expected_pet):
            away = await _away_check(bot, con, pet_id)
            if away is None:
                percentage = random.randint(0, 100)
                shiny = percentage >= 85
                await con.execute("UPDATE PETS SET PET_NAME = $2, SHINY = TRUE WHERE PET_ID = $1" if shiny else
                                  "UPDATE PETS SET PET_NAME = $2 WHERE PET_ID = $1", pet_id, name)
    if pet_id <= 0:
        await scene.send(MSG_ALONE_MAD)
        return False
    if _other_pet(pet_id, expected_pet):
        await scene.send(MSG_OTHER_PET)
        return False
    if away is not None:
        await scene.send(_away_message(player_id, away[1].name, away[1].until))
        return False

    if shiny:
        try:
            await scene.send("Towarzysz wygląda na bardzo wdzięcznego i dostrzegasz" +
                             ", że wygląda jakoś inaczej <:Susge:973591024322633858>")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the pet is named and shiny, whatever Discord says
            log.warning("Pet %s turned shiny but the player could not be told: %s: %s",
                        pet_id, type(exc).__name__, exc)
    await io.safe_react(scene.message, "<:peepoBlush:984769061340737586>")
    return True


# -- the stable -----------------------------------------------------------------------

async def store_pet(scene: "Scene", slot: Any) -> bool:
    """Put the author's pet into stable place 1..N (a pet kept there comes out).

    N is 4, or more for a player who bought places from the wandering merchant.
    Neither the pet at his side nor the one of that place may be on a
    companion mission (the swap would move it): :data:`MSG_PET_AWAY` /
    :data:`MSG_SLOT_AWAY`, asked after the owner lock (spec §7.1).
    """
    bot = scene.bot
    player_id = _pid(scene.author)
    if player_id in _awaiting_confirmation:
        await scene.send(MSG_ALREADY_WAITING)
        return False
    upgrades = await upgrades_available(bot)
    places = STABLE_BASE_PLACES
    refusal = ""
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, _owner_columns(upgrades), lock=True)
        current = _int(owner["pet_id"]) if owner is not None else 0
        if owner is not None:
            places = _places_of(owner)
        column = _slot_column(slot, places)
        if owner is None:
            status = "alone"
        elif current <= 0:
            status = "no_pet"
        elif column is None:
            status = "bad_slot"
        else:
            stored = max(_int(owner[column.lower()]), 0)
            away = await _away_among(bot, con, [current, stored])
            if current in away:
                status = AWAY
                refusal = _away_message(player_id, _called(await _pet(con, current)), away[current])
            elif stored in away:
                status = SLOT_AWAY
                refusal = MSG_SLOT_AWAY.format(slot=_slot_number(slot), when=ui.when(away[stored]),
                                               player_id=player_id)
            else:
                status = _OK
                await con.execute(f"UPDATE PETOWNER SET {column} = $2, PET_ID = $3 WHERE PLAYER_ID = $1",
                                  player_id, current, stored)
    if refusal:
        await scene.send(refusal)
    elif status == "alone":
        await scene.send(_msg_alone(player_id))
    elif status == "no_pet":
        await scene.send("Niestety nie posiadasz aktualnie żadnego peta, <@" + str(player_id)
                         + ">. Możesz sprawdzić swoją stajnie wpisując **!stajnia**.")
    elif status == "bad_slot":
        await scene.send(_msg_bad_slot(player_id, "schowajtowarzysza", places))
    else:
        await scene.send(MSG_STORED)
    return status == _OK


async def _take_out(bot: "AlterBot", player_id: int, slot: int, expected_pet: int | None) -> str:
    """Swap stable place ``slot`` with the active pet in one locked transaction.

    Status ``alone`` / ``bad_slot`` (not one of the player's places) / ``empty``
    / ``swapped`` / ``taken``, or ``changed`` (nothing done) when
    ``expected_pet`` is given and the place no longer holds it - or, nothing
    done, :data:`SLOT_AWAY` (the place's pet is on a companion mission) /
    :data:`AWAY` (the pet at his side is: the swap would move it), asked after
    the owner lock (spec §7.1; the text: :func:`_move_refusal`).
    """
    upgrades = await upgrades_available(bot)
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, _owner_columns(upgrades), lock=True)
        if owner is None:
            return "alone"
        column = _slot_column(slot, _places_of(owner))
        if column is None:
            return "bad_slot"
        stabled = _int(owner[column.lower()])
        if expected_pet is not None and stabled != expected_pet:
            return "changed"
        if stabled <= 0:
            return "empty"
        current = max(_int(owner["pet_id"]), 0)
        away = await _away_among(bot, con, [stabled, current])
        if stabled in away:
            return SLOT_AWAY
        if current in away:
            return AWAY
        await con.execute(f"UPDATE PETOWNER SET {column} = $2, PET_ID = $3 WHERE PLAYER_ID = $1",
                          player_id, current, stabled)
        return "swapped" if current > 0 else "taken"


async def unstore_pet(scene: "Scene", slot: Any) -> bool:
    """Take a pet out of stable place 1..N (the active pet takes its place)."""
    player_id = _pid(scene.author)
    number = _slot_number(slot)
    if number is None or not 1 <= number <= STABLE_MAX_PLACES:
        await scene.send(_msg_bad_slot(player_id, "wyciagnijtowarzysza", await stable_places(scene.bot, player_id)))
        return False
    if player_id in _awaiting_confirmation:
        await scene.send(MSG_ALREADY_WAITING)
        return False
    status = await _take_out(scene.bot, player_id, number, None)
    if status == "bad_slot":
        await scene.send(_msg_bad_slot(player_id, "wyciagnijtowarzysza", await stable_places(scene.bot, player_id)))
        return False
    await scene.send(await _unstore_text(scene.bot, player_id, status, number))
    return status in ("swapped", "taken")


def _unstore_message(player_id: int, status: str) -> str:
    """What the channel is told after a :func:`_take_out` (any status but ``changed`` / ``bad_slot`` / away)."""
    return {"alone": _msg_alone(player_id), "empty": MSG_EMPTY_SLOT,
            "swapped": MSG_SWAPPED, "taken": MSG_UNSTORED}[status]


async def _unstore_text(bot: "AlterBot", player_id: int, status: str, slot: int) -> str:
    """:func:`_unstore_message`, and the refusals of a pet on a mission (:func:`_move_refusal`)."""
    if status in (AWAY, SLOT_AWAY):
        return await _move_refusal(bot, player_id, status, slot)
    return _unstore_message(player_id, status)


# -- the egg pouch (sold by the wandering merchant) --------------------------------------

MSG_POUCH_NONE = ("Nie masz sakwy na jajko, <@{player_id}>. Czasem sprzedaje ją wędrowny kupiec, a czasem "
                  "przynosi ją towarzysz ze zlecenia <:Hmm:984767035617730620>")
MSG_POUCH_EMPTY = "Twoja sakwa na jajko jest pusta, <@{player_id}>."
MSG_POUCH_NO_ROOM = ("Nie ma gdzie odłożyć towarzysza, który jest teraz przy Tobie, <@{player_id}> - stajnia "
                     "jest pełna. Zwolnij w niej miejsce i spróbuj jeszcze raz.")
MSG_POUCH_TAKEN = ("🥚 Jajko z sakwy jest teraz przy Tobie, <@{player_id}>, i może rosnąć! "
                   "<:peepoBlush:984769061340737586>")
MSG_POUCH_SWAPPED = ("🥚 Jajko z sakwy jest teraz przy Tobie, <@{player_id}>, a dotychczasowy towarzysz "
                     "odpoczywa w stajni na miejscu {slot}. <:peepoBlush:984769061340737586>")
#: Added to MSG_POUCH_TAKEN / MSG_POUCH_SWAPPED when the Duża sakwa still holds its other egg.
MSG_POUCH_ONE_LEFT = " W sakwie czeka jeszcze jedno jajko."


async def _take_from_pouch(bot: "AlterBot", player_id: int, expected_pet: int | None) -> tuple[str, int]:
    """Make an egg in the pouch the active pet, in one locked transaction.

    ``expected_pet``: the egg the stable list named (in either slot of the Duża
    sakwa); None (``!wyjmijjajko``): the egg of the first filled slot.  The pet
    at the player's side goes to his first free stable place.  Returns
    ``(status, place)``: ``none`` (no pouch, or the columns are missing),
    ``empty`` (no egg in it), ``no_room`` (a pet at his side and the stable is
    full), ``changed`` (``expected_pet`` is in no slot of the pouch), ``taken``
    (no pet was at his side) or ``swapped`` with the place the old pet went to
    - or :data:`AWAY` (nothing done): the pet at his side is on a companion
    mission and would have to go to the stable (asked after the owner lock).
    """
    upgrades, big = await _owner_shape(bot)
    if not upgrades:
        return "none", 0
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, _owner_columns(True, big), lock=True)
        pouch = _pouch(owner)
        if not pouch.size:
            return "none", 0
        if expected_pet is not None:
            slot = next(((column, egg) for column, egg in pouch.eggs if egg == expected_pet), None)
            if slot is None:
                return "changed", 0
        elif pouch.eggs:
            slot = pouch.eggs[0]
        else:
            return "empty", 0
        column_of_egg, egg = slot
        current = max(_int(owner["pet_id"]), 0)
        if current > 0 and await away_until(bot, con, current):
            return AWAY, 0
        if current <= 0:
            await con.execute(f"UPDATE PETOWNER SET PET_ID = $2, {column_of_egg} = 0 WHERE PLAYER_ID = $1",
                              player_id, egg)
            return "taken", 0
        for place in range(1, _places_of(owner) + 1):
            column = _STABLE_SLOTS[place]
            if _int(owner[column.lower()]) <= 0:
                await con.execute(f"UPDATE PETOWNER SET {column} = $2, PET_ID = $3, {column_of_egg} = 0 "
                                  "WHERE PLAYER_ID = $1", player_id, current, egg)
                return "swapped", place
        return "no_room", 0


def _pouch_message(player_id: int, status: str, place: int, *, left: int = 0) -> str:
    """What the channel is told after :func:`_take_from_pouch` (any status but ``changed``).

    ``left``: eggs still in the pouch after one was taken (the Duża sakwa holds two).
    """
    text = {"none": MSG_POUCH_NONE, "empty": MSG_POUCH_EMPTY, "no_room": MSG_POUCH_NO_ROOM,
            "taken": MSG_POUCH_TAKEN, "swapped": MSG_POUCH_SWAPPED}[status].format(player_id=player_id, slot=place)
    if left > 0 and status in ("taken", "swapped"):
        text += MSG_POUCH_ONE_LEFT
    return text


async def _eggs_in_pouch(bot: "AlterBot", player_id: int) -> int:
    """How many eggs wait in the player's pouch now (read without a lock, for a text)."""
    upgrades, big = await _owner_shape(bot)
    if not upgrades:
        return 0
    return len(_pouch(await _owner(bot.db, player_id, _owner_columns(upgrades, big))).eggs)


async def take_egg_from_pouch(scene: "Scene") -> bool:
    """``!wyjmijjajko``: an egg in the author's pouch becomes his active pet (and can grow).

    The Duża sakwa gives the egg of its first filled slot.
    """
    player_id = _pid(scene.author)
    if player_id in _awaiting_confirmation:
        await scene.send(MSG_ALREADY_WAITING)
        return False
    status, place = await _take_from_pouch(scene.bot, player_id, None)
    await scene.send(await _pouch_text(scene.bot, player_id, status, place))
    return status in ("taken", "swapped")


async def _pouch_text(bot: "AlterBot", player_id: int, status: str, place: int) -> str:
    """:func:`_pouch_message` after :func:`_take_from_pouch` (any status but ``changed``), with the eggs left.

    :data:`AWAY`: the pet at his side is on a mission (:func:`_move_refusal`).
    """
    if status == AWAY:
        return await _move_refusal(bot, player_id, status)
    left = 0
    if status in ("taken", "swapped"):
        log.info("Player %s took the egg out of the pouch (%s, place %s).", player_id, status, place)
        left = await _eggs_in_pouch(bot, player_id)
    return _pouch_message(player_id, status, place, left=left)


STABLE_ACTIVE = "🐾 **Przy Tobie**"
STABLE_PLACES = "🐴 **W stajni**"
STABLE_POUCH = "👝 **W sakwie**"
#: The heading of the Duża sakwa (a mission's reward): ``filled`` of its two slots hold an egg.
STABLE_BIG_POUCH = "🎒 **W Dużej sakwie** ({filled}/2)"
STABLE_NOBODY = "*brak towarzysza*"
STABLE_FREE = "*wolne miejsce*"
STABLE_POUCH_EMPTY = "*pusta*"
STABLE_NO_DATA = "*brak danych w tabeli PETS*"
#: The value of the stable list's entry for the egg in the pouch (``pouch:<pet id>``).
POUCH_OPTION = "pouch"


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
    """The stable embed and the pets that can be taken out (None: the player has no row).

    The places the player has (4, or more bought from the wandering merchant)
    and, when he owns one, the egg pouch.  A pet on a companion mission says
    so on its line (:data:`STABLE_AWAY`) and has no entry in the list; while
    it is the pet at his side the list has no entry at all (each would swap
    it out - refused) and the stable says why (:data:`STABLE_SIDE_AWAY`).
    """
    owner = await _owner(bot.db, player_id, _owner_columns(*await _owner_shape(bot)))
    if owner is None:
        return None
    away = await away_of(bot, bot.db, player_id)
    away_pet, away_line = (away[0], STABLE_AWAY.format(when=ui.when(away[1]))) if away is not None else (0, "")

    places = _places_of(owner)
    pet_ids = [_int(owner["pet_id"]), *(_int(owner[_STABLE_SLOTS[place].lower()]) for place in range(1, places + 1))]
    side_away = pet_ids[0] > 0 and pet_ids[0] == away_pet
    pouch = _pouch(owner)
    in_pouch = [pet_id for _column, pet_id in pouch.eggs]
    wanted = [pet_id for pet_id in (*pet_ids, *in_pouch) if pet_id > 0]
    pets_by_id: dict[int, Any] = {}
    if wanted:
        rows = await bot.db.fetch(
            "SELECT PET_ID, PET_NAME, PET_LVL, TYPE FROM PETS WHERE PET_ID = ANY($1::numeric[])", wanted)
        for row in rows:
            pets_by_id.setdefault(_int(row["pet_id"]), row)

    # Short blocks instead of "label: value" sentences: the pet at the player's side, the
    # places of the stable (an empty place says so in italics) and the egg pouch.
    active = STABLE_NOBODY
    lines = []
    options: list[ui.Option] = []
    for index, pet_id in enumerate(pet_ids):
        if pet_id <= 0:
            line = STABLE_NOBODY if index == 0 else STABLE_FREE
        else:
            pet = pets_by_id.get(pet_id)
            if pet is None:
                line = STABLE_NO_DATA
            elif pet_id == away_pet:  # on a mission: it stays where it is
                line = _pet_line(pet) + away_line
            else:
                line = _pet_line(pet)
                if index > 0 and not side_away:  # "slot:pet": a stale list cannot take out another pet
                    options.append(ui.Option(f"{index}:{pet_id}", _pet_brief(pet)[:100],
                                             f"Miejsce w stajni {index}", emoji="🐾"))
        if index == 0:
            active = line
        else:
            lines.append(f"**{index}.** {line}")
    pouch_block = ""
    if pouch.size:
        pouch_lines = []
        for egg_id in in_pouch:  # one line and one entry per egg (the Duża sakwa holds two)
            egg = pets_by_id.get(egg_id)
            pouch_lines.append(STABLE_NO_DATA if egg is None else _pet_line(egg))
            if egg is not None and not side_away:
                options.append(ui.Option(f"{POUCH_OPTION}:{egg_id}", _pet_brief(egg)[:100],
                                         "Sakwa na jajko" if pouch.size <= 1 else "Duża sakwa", emoji="👝"))
        heading = STABLE_POUCH if pouch.size <= 1 else STABLE_BIG_POUCH.format(filled=len(in_pouch))
        pouch_block = f"\n\n{heading}\n" + ("\n".join(pouch_lines) or STABLE_POUCH_EMPTY)

    embed = discord.Embed(
        title="Stajnia",
        description=("Oto Twoja stajnia, <@" + str(player_id) + "> <:peepoBlush:984769061340737586>\n\n"
                     + f"{STABLE_ACTIVE}\n{active}\n\n{STABLE_PLACES}\n" + "\n".join(lines) + pouch_block
                     + (f"\n\n{STABLE_SIDE_AWAY}" if side_away else "")),
        color=0x34cceb,
    )
    footer = "Możesz schować lub wyciągnąć peta za pomocą komend !schowajtowarzysza oraz !wyciagnijtowarzysza."
    if pouch.size:
        footer += " Jajko z sakwy wyjmiesz komendą !wyjmijjajko."
    embed.set_footer(text=footer)
    embed.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png")
    return embed, options


async def check_stable(scene: "Scene") -> bool:
    """Show the author's active pet, the stable places and the egg pouch; False when he has no row.

    On a channel the stable carries a list (:class:`StablePanel`) that makes a
    stabled pet the active one - only for the author, only where and when
    ``!wyciagnijtowarzysza`` would work.
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


# -- an egg the player places himself (the mine, features/dungeon.py) -------------------

async def store_in_free_place(bot: "AlterBot", player_id: Any) -> tuple[str, int, Any]:
    """Put the pet at the player's side into his FIRST FREE stable place, in one locked transaction.

    ``!schowajtowarzysza <slot>`` (:func:`store_pet`) names a place and swaps
    with whoever stands there; the mine's "Stajnia" button only ever moves the
    active pet out of the way, so it must not bring another pet back.  Returns
    ``(status, place, the pet's PETS row)``: ``alone`` (no row at all),
    ``no_pet`` (nobody at his side), ``full`` (no free place), :data:`AWAY`
    (the pet at his side is on a companion mission: it is not moved; asked
    after the owner lock) or ``_OK``.
    """
    player_id = _pid(player_id)
    upgrades = await upgrades_available(bot)
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, _owner_columns(upgrades), lock=True)
        if owner is None:
            return "alone", 0, None
        current = _int(owner["pet_id"])
        if current <= 0:
            return "no_pet", 0, None
        if await away_until(bot, con, current):
            return AWAY, 0, await _pet(con, current)
        for place in range(1, _places_of(owner) + 1):
            column = _STABLE_SLOTS[place]
            if _int(owner[column.lower()]) <= 0:
                await con.execute(f"UPDATE PETOWNER SET {column} = $2, PET_ID = 0 WHERE PLAYER_ID = $1",
                                  player_id, current)
                pet = await _pet(con, current)
                log.info("Player %s put pet %s into the free stable place %s.", player_id, current, place)
                return _OK, place, pet
        return "full", 0, None


class EggPlaces(NamedTuple):
    """Where a new egg of a player could go right now (read without a lock; :func:`place_egg` decides again)."""

    #: Nobody at his side: the egg can become his companion.
    active: bool
    #: He owns the egg pouch and it has a free slot (the Duża sakwa holds two eggs).
    pouch: bool
    #: How many stable places he has.
    places: int
    #: The numbers of his free stable places.
    free: tuple[int, ...]
    #: His occupied stable places as ``(place, PET_ID, the pet's row or None without data)`` - without
    #: the places whose pet is on a companion mission (:attr:`away`): a swap must never offer one.
    occupied: tuple[tuple[int, int, Any], ...]
    #: The pet at his side (``PET_ID`` / ``PET_NAME`` / ``PET_LVL`` / ``TYPE``), or None - also when it
    #: is on a mission (:attr:`active_away`), so the texts can name it.
    active_pet: Any = None
    #: The pet at his side is on a companion mission (it gets no level, it cannot be moved).
    active_away: bool = False
    #: The stable places whose pet is on a mission: neither free nor in :attr:`occupied`.
    away: frozenset[int] = frozenset()

    @property
    def room(self) -> str:
        """The answer of :func:`egg_room`: ``"active"``, ``"pouch"`` or ``""``."""
        return "active" if self.active else "pouch" if self.pouch else ""


async def egg_places(bot: "AlterBot", player_id: Any) -> EggPlaces:
    """The player's places for a new egg: at his side, the pouch, every stable place (see :class:`EggPlaces`)."""
    player_id = _pid(player_id)
    owner = await _owner(bot.db, player_id, _owner_columns(*await _owner_shape(bot)))
    if owner is None:
        return EggPlaces(True, False, STABLE_BASE_PLACES, tuple(range(1, STABLE_BASE_PLACES + 1)), ())
    places = _places_of(owner)
    stabled = {place: _int(owner[_STABLE_SLOTS[place].lower()]) for place in range(1, places + 1)}
    active = _int(owner["pet_id"])
    wanted = [pet_id for pet_id in (active, *stabled.values()) if pet_id > 0]
    rows: dict[int, Any] = {}
    if wanted:
        for row in await bot.db.fetch(
                "SELECT PET_ID, PET_NAME, PET_LVL, TYPE FROM PETS WHERE PET_ID = ANY($1::numeric[])", wanted):
            rows.setdefault(_int(row["pet_id"]), row)
    away = await _away_among(bot, bot.db, wanted)
    return EggPlaces(
        active <= 0, _pouch(owner).free_column is not None, places,
        tuple(place for place, pet_id in stabled.items() if pet_id <= 0),
        tuple((place, pet_id, rows.get(pet_id)) for place, pet_id in stabled.items()
              if pet_id > 0 and pet_id not in away),
        rows.get(active) if active > 0 else None,
        active > 0 and active in away,
        frozenset(place for place, pet_id in stabled.items() if pet_id > 0 and pet_id in away))


def roll_egg(player: Any, *, cosmetics: bool = True, frames: bool = True, rerolls: int = 0,
             min_class: int = 0) -> dict[str, Any]:
    """A new egg for ``player``, rolled exactly as :func:`assign_pet` rolls one - but not stored yet.

    The egg of the mine: the game's normal egg (a supporter's may be of a
    premium species and gets the usual cosmetic roll, anybody else's is of the
    standard pool; a mythic one is possible for everybody).  It is stored when
    the player has said where it goes (:func:`place_egg`).  The result holds
    plain numbers, texts and booleans, so it can wait in a JSON file.

    The companion missions pass ``rerolls`` (a supporter's cosmetic roll may be
    repeated, as for a special boss's egg) and ``min_class`` (the egg is at
    least of that class; mythic as likely as ever) - see :func:`_roll_egg`.
    ``player`` may be None (he left the server: no supporter).
    """
    return _roll_egg(player, cosmetics=cosmetics, frames=frames, rerolls=rerolls, min_class=min_class)


def egg_row(egg: dict[str, Any]) -> dict[str, Any]:
    """A rolled egg as the ``PETS`` row it will become (lower-case keys), for the code that shows a pet."""
    return {"pet_id": 0, "pet_name": DEFAULT_NAME, "pet_lvl": 0, "pet_skills": 0,
            "quality": egg["QUALITY"], "type": egg["TYPE"], "variant": str(egg["VARIANT"]),
            "shiny": bool(egg["SHINY"]), "ultra_shiny": bool(egg["ULTRA_SHINY"]), "mythic": bool(egg["MYTHIC"]),
            "seal": int(egg.get("SEAL", 0) or 0), "holo": bool(egg.get("HOLO", False)),
            "frame": str(egg.get("FRAME", "") or ""), "holo_partial": bool(egg.get("HOLO_PARTIAL", False))}


async def egg_shown(bot: "AlterBot", guild: Any, player: Any, egg: dict[str, Any]) -> tuple[list[str], Path | None]:
    """How the game shows a rolled egg to ``player``: ``(lines of words, the picture or None)``.

    Exactly what ``!towarzysz`` shows of an egg (:func:`_pet_embed`,
    :func:`_pet_card`): never its species; the sentence of its class; its
    ornaments in words and on the card only while he supports (one line says
    so when they are hidden).  The picture is the egg's card, or None when the
    egg has no picture.  Never raises for a card problem.
    """
    row = egg_row(egg)
    ornaments = ornaments_of(row).any
    cosmetics = ornaments and await _is_supporter(bot, player)
    lines = [words] if (words := _class_look(row)[1]) else []
    if cosmetics:
        lines += ornament_lines(row)
    elif ornaments:
        lines.append(MSG_COSMETICS_HIDDEN)
    source = _pet_image(row)
    if source is None or not source.is_file():
        log.warning("A rolled egg (%s, variant %s) has no picture (%s); showing it in words only.",
                    row["quality"], row["variant"], source)
        return lines, None
    return lines, await _pet_card(bot, guild, row, source, cosmetics=cosmetics)


#: ``where`` of :func:`place_egg`: by the game's normal rule (at the player's side, else the
#: free pouch); the first free stable place; the stable place of a pet that is sacrificed for
#: it; nowhere - the egg itself is sacrificed; and, for an egg nobody decided about, wherever
#: there is room without a sacrifice (side, pouch, a free stable place - in that order).
EGG_TO_ROOM, EGG_TO_STABLE, EGG_SWAP, EGG_SACRIFICE, EGG_ANYWHERE = "room", "stable", "swap", "sacrifice", "anywhere"
#: ``EggPlaced.status``: where the egg is now ...
EGG_ACTIVE, EGG_IN_POUCH, EGG_IN_STABLE, EGG_SWAPPED, EGG_SACRIFICED = "active", "pouch", "stable", "swapped", "sacrificed"
#: ... or why nothing happened: the place is not what the player was shown (choose again) /
#: there is no room anywhere.
EGG_CHANGED, EGG_NO_ROOM = "changed", "no_room"


class EggPlaced(NamedTuple):
    """What :func:`place_egg` did."""

    status: str
    #: ``PET_ID`` of the stored egg (0: it was not stored - sacrificed, or nothing happened).
    pet_id: int = 0
    #: The stable place it took (``EGG_IN_STABLE`` / ``EGG_SWAPPED``).
    place: int = 0
    #: Album entry of what was sacrificed (a supporter's pet / egg); None: nothing was kept.
    kept: int | None = None
    #: ``PET_ID`` of the pet that was sacrificed for the egg (``EGG_SWAPPED``).
    gone: int = 0

    @property
    def done(self) -> bool:
        """True when the egg's fate is settled (stored or sacrificed)."""
        return self.status not in (EGG_CHANGED, EGG_NO_ROOM)


async def place_egg(scene: "Scene", player: Any, egg: dict[str, Any], where: str, *, place: Any = None,
                    expected_pet: int | None = None, announce: bool = True) -> EggPlaced:
    """Store a rolled egg (:func:`roll_egg`) where the player said - or sacrifice it.

    ONE transaction under the player's lock, with the state read again in it:

    * :data:`EGG_TO_ROOM` - the rule of :func:`assign_pet`: his companion when
      nobody is at his side, else into his empty egg pouch;
    * :data:`EGG_TO_STABLE` - his first free stable place;
    * :data:`EGG_SWAP` - stable place ``place``, when it still holds
      ``expected_pet``: that pet is sacrificed (:func:`_sacrifice_in`, the
      path of ``!porzucam``: a supporter's pet goes into his album, anybody
      else's is gone) and the egg takes its place - both or neither;
    * :data:`EGG_SACRIFICE` - the egg itself is sacrificed by the same rule: a
      supporter's egg becomes an album entry (stored and sacrificed in the
      same transaction), anybody else's is simply gone;
    * :data:`EGG_ANYWHERE` - at his side, else the pouch, else a free stable
      place; nothing is ever sacrificed for it.

    (The pouch: its first free slot - the Duża sakwa has two.  The work inside
    the transaction is :func:`place_egg_in`, which the companion missions run
    in their own claim transaction; the hooks below are :func:`after_new_egg`.)

    When the place is not what the caller saw (a pet moved meanwhile, the pouch
    filled) nothing happens and the status is :data:`EGG_CHANGED` - or
    :data:`EGG_NO_ROOM` for :data:`EGG_ANYWHERE`: the caller still holds the
    egg.  ``album.NotSaved`` passes through with nothing changed.

    After the commit the hooks of a new egg run (:func:`_egg_stored`, the
    announcement of a mythic egg, ``achievements.check_pets``) and, when
    something was sacrificed, the
    hooks of a sacrifice (:func:`_sacrificed`: the random boss modifier, the
    album's word and achievement).  A hook that fails is logged - what was
    committed is reported in any case.  ``announce=False`` keeps the hooks
    quiet (an egg placed while the channel belongs to somebody else).
    Whether the player supports is asked at this moment (``player``: a
    member, or a bare id that is looked up).
    """
    bot = scene.bot
    player_id = _pid(player)
    member = player if getattr(player, "roles", None) is not None else None
    if member is None and where in (EGG_SWAP, EGG_SACRIFICE):
        member = await bot.member(player_id)
    keeps = member is not None and patrons.check_if_patron(member)
    upgrades, big = await _owner_shape(bot)
    cosmetics, frames = await _cosmetic_state(bot)
    ready = await missions_table_ready(bot)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        result = await place_egg_in(con, player_id, egg, where, upgrades=upgrades, big=big, place=place,
                                    expected_pet=expected_pet, keeps=keeps, ready=ready)
    if not result.done:
        return result
    status = result.status
    # (A sacrificed egg is not reported as a stored one: its PET_ID is not in PETS - only the columns
    # it was stored with for that moment are remembered.)
    if result.pet_id or status == EGG_SACRIFICED:
        await after_new_egg(scene, member, player_id, result.pet_id, egg, cosmetics, frames, announce=announce,
                            album=False, kept=status != EGG_SACRIFICED)
    if status in (EGG_SWAPPED, EGG_SACRIFICED) and member is not None:
        try:
            await _sacrificed(scene.with_author(member), member, result.kept)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - committed: the caller must hear what happened to the egg
            log.exception("Egg of player %s (%s): a hook after the commit failed.", player_id, status)
    return result


async def place_egg_in(con: Any, player_id: int, egg: dict[str, Any], where: str, *, upgrades: bool,
                       big: bool = False, place: Any = None, expected_pet: int | None = None,
                       keeps: bool = False, ready: bool = False) -> EggPlaced:
    """The core of :func:`place_egg`, inside the caller's transaction (he holds the player's lock).

    Reads the player's row again (``FOR UPDATE``), stores the egg where
    ``where`` says (or sacrifices it / the stabled ``expected_pet``; ``keeps``:
    the player supports, so what is sacrificed goes into his album) and
    returns what happened.  :data:`EGG_TO_ROOM` / :data:`EGG_ANYWHERE` fill the
    player's side, then the pouch's first free slot (the Duża sakwa has two),
    then - only :data:`EGG_ANYWHERE` - a free stable place.  ``upgrades`` /
    ``big``: :func:`upgrades_available` / :func:`mission_columns_available`.
    ``ready`` (:func:`missions_table_ready`): a stabled pet on a companion
    mission is never sacrificed for the egg - :data:`EGG_SWAP` answers
    :data:`EGG_CHANGED` (asked after the owner lock, spec §7.1).
    Runs no hooks (:func:`after_new_egg` does, after the commit);
    ``album.NotSaved`` passes through.
    """
    kept, gone = None, 0
    owner = await _owner(con, player_id, _owner_columns(upgrades, big), lock=True)
    if where == EGG_SACRIFICE:
        pet_id = 0
        if keeps:  # the album keeps a PETS row: the egg is one for the length of this transaction
            pet_id = await _insert_egg(con, egg)
            kept = await _sacrifice_in(con, player_id, pet_id, True)
        status, number = EGG_SACRIFICED, 0
    else:
        active = _int(owner["pet_id"]) if owner is not None else 0
        free_pouch = _pouch(owner).free_column
        places = _places_of(owner) if owner is not None else STABLE_BASE_PLACES
        free = [number for number in range(1, places + 1)
                if owner is None or _int(owner[_STABLE_SLOTS[number].lower()]) <= 0]
        column, number = None, 0
        if where in (EGG_TO_ROOM, EGG_ANYWHERE) and active <= 0:
            status, column = EGG_ACTIVE, "PET_ID"
        elif where in (EGG_TO_ROOM, EGG_ANYWHERE) and free_pouch is not None:
            status, column = EGG_IN_POUCH, free_pouch
        elif where in (EGG_TO_STABLE, EGG_ANYWHERE) and free:
            status, number = EGG_IN_STABLE, free[0]
            column = _STABLE_SLOTS[number]
        elif where == EGG_SWAP:
            number = _slot_number(place) or 0
            column = _slot_column(number, places) if owner is not None else None
            wanted = _int(expected_pet)
            if column is None or wanted <= 0 or _int(owner[column.lower()]) != wanted:
                return EggPlaced(EGG_CHANGED)
            if await _away_ends(con, wanted, ready):  # (sent on a mission since the player chose it)
                log.info("Egg of player %s: pet %s is on a mission, not sacrificed for it.", player_id, wanted)
                return EggPlaced(EGG_CHANGED)
            status, gone = EGG_SWAPPED, wanted
            kept = await _sacrifice_in(con, player_id, wanted, keeps, ready=ready)  # NotSaved: nothing happened
        else:
            return EggPlaced(EGG_NO_ROOM if where == EGG_ANYWHERE else EGG_CHANGED)
        pet_id = await _insert_egg(con, egg)
        if owner is None:  # (PET_OWNED as assign_pet / _add_item write it for a first row)
            await _insert_owner(con, player_id, 0, status == EGG_ACTIVE, 0, 0, 0, 0, 0)
        await con.execute(f"UPDATE PETOWNER SET {column} = $2 WHERE PLAYER_ID = $1", player_id, pet_id)
    log.info("Egg of player %s: %s (pet %s, place %s%s%s).", player_id, status, pet_id or "-", number or "-",
             f", pet {gone} sacrificed for it" if gone else "",
             "" if kept is None else f", kept in the album as entry {kept}")
    return EggPlaced(status, 0 if status == EGG_SACRIFICED else pet_id, number, kept, gone)


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
    """The list under ``!stajnia``: choose a stabled pet (or the egg in the pouch) to make it the active one.

    Same effect and the same guards as ``!wyciagnijtowarzysza <slot>``: boss
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
    def _parse(value: Any) -> tuple[int, int] | None:
        """``"3:7"`` -> (place 3, pet 7), ``"pouch:9"`` -> (0, pet 9 in the egg pouch); None for anything else.

        Whether the place is one of the player's is checked under the lock (:func:`_take_out`).
        """
        slot, _, pet_id = str(value).partition(":")
        try:
            pet = int(pet_id)
        except ValueError:
            return None
        if pet <= 0:
            return None
        if slot == POUCH_OPTION:
            return 0, pet
        number = _slot_number(slot)
        return (number, pet) if number is not None and 1 <= number <= STABLE_MAX_PLACES else None

    async def _chosen(self, interaction: Any) -> None:
        # (stable=True: also during the sign-up of the "Trening towarzyszy" event, as the command.)
        if not await ui.gate(interaction, stable=True):
            return
        values = (getattr(interaction, "data", None) or {}).get("values") or list(self.select.values)
        await ui.ack(interaction)
        if not values:
            return
        scene = ui.scene_of(interaction)
        player_id = _pid(interaction.user)
        chosen = self._parse(values[0])
        if player_id in _awaiting_confirmation:
            await scene.send(MSG_ALREADY_WAITING)  # as !wyciagnijtowarzysza
        else:
            # The pet the entry names must still be in its slot / the pouch (checked under the lock).
            place = 0
            if chosen is None:
                status = "changed"
            elif chosen[0] == 0:
                status, place = await _take_from_pouch(scene.bot, player_id, chosen[1])
            else:
                status = await _take_out(scene.bot, player_id, *chosen)
            if status in ("changed", "bad_slot"):
                log.info("Stable list of %s: %r no longer matches the stable.", player_id, values[0])
                await ui.reply(interaction, MSG_STABLE_CHANGED)
            elif chosen is not None and chosen[0] == 0:
                await scene.send(await _pouch_text(scene.bot, player_id, status, place))
            else:
                await scene.send(await _unstore_text(scene.bot, player_id, status, chosen[0] if chosen else 0))
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
    """Buttons under the pet shown by ``!towarzysz``; only its owner can use them.

    ``Zmień imię`` opens a pop-up window (``!nazwij`` with its 23-hour
    cooldown), ``Stajnia`` shows the stable (display only, any channel), the
    rest start exactly the flows of ``!odrodzenie``, ``!mocneodrodzenie``,
    ``!oswiecenie``, ``!transformacja`` and ``!porzucam``.  Gameplay buttons
    are refused outside the boss channel, while the channel is
    busy and for dead hunters.  A gameplay button takes the panel's buttons
    away: the flow shows the pet anew when it changed it.  Buttons that make
    no sense for this pet (rerolling an egg, enlightening a young pet,
    transforming a mythic one or without the rank) are not shown.

    The panel acts only on the pet it shows (:attr:`pet_id`): once another pet
    is active (taken out of the stable meanwhile) a click takes the buttons off
    and is answered with :data:`MSG_OTHER_PET`; the flows re-check
    it after their question too.  ``image``: the file sent with the panel (the
    pet's card, see :func:`_pet_card`).  ``away``: the pet is on a companion
    mission - nothing can change it, so only ``Stajnia`` is shown (a click
    that races a send is refused by :meth:`_play` and the flows themselves).
    """

    def __init__(self, player: Any, pet: Any, *, image: Path | None = None,
                 timeout: float | None = None, away: bool = False) -> None:
        super().__init__(owner=player, timeout=PANEL_TIMEOUT if timeout is None else timeout)
        self.player = player
        self.pet_id = _int(pet["pet_id"])
        self.image = image
        self.pet_name = str(pet["pet_name"] or "")
        self.away = away
        level, skills = _int(pet["pet_lvl"]), _int(pet["pet_skills"])
        hidden = set()
        if away:
            hidden |= {self.rename, self.reroll, self.adv_reroll, self.enlight, self.transform, self.discard}
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
                             unless_busy=_display_gate_busy(interaction.client), stable=True):
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

    async def _not_away(self, interaction: Any) -> bool:
        """True unless this panel's pet is on a companion mission (it went after the panel was shown).

        Otherwise the click is answered with :data:`MSG_PET_AWAY` and the
        panel's buttons are taken off.
        """
        bot = interaction.client
        until = await away_until(bot, bot.db, self.pet_id)
        if until is None:
            return True
        log.info("Pet panel of %s: pet %s is on a mission; click refused.", self.owner_id, self.pet_id)
        await ui.reply(interaction, _away_message(_pid(interaction.user), _called(await _pet(bot.db, self.pet_id)),
                                                  until))
        await self.finish()
        return False

    async def _play(self, interaction: Any, flow: Callable[["Scene"], Awaitable[bool]], *,
                    hold: str | None = None) -> None:
        """Run a gameplay flow for the click, with the guards of its command."""
        if not await ui.gate(interaction) or not await self.shows_active_pet(interaction):
            return
        if not await self._not_away(interaction):
            return
        log.info("Pet panel of %s: %s.", self.owner_id, (interaction.data or {}).get("custom_id"))
        scene = ui.scene_of(interaction)
        if hold is None:
            await ui.ack(interaction)
            await self.finish()
            await flow(scene)
            return
        # Like ``!mocneodrodzenie``: the flow holds the channel.  The database read
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
        embed, image = await _shown(bot, guild, self.player, pet, items, wait=wait,
                                    away=await _shown_away(bot, _pid(self.player), pet))
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
    """The ``!nazwij`` cooldown of ``member`` (shared by the command and the button); None without one."""
    command = bot.get_command("nazwij") if hasattr(bot, "get_command") else None
    buckets = getattr(command, "_buckets", None)
    if buckets is None or not getattr(buckets, "valid", False):
        return None
    try:
        return buckets.get_bucket(types.SimpleNamespace(author=member, guild=getattr(member, "guild", None)))
    except Exception:  # noqa: BLE001 - a cooldown that cannot be read is no reason to fail the click
        log.exception("Could not read the !nazwij cooldown of %s.", getattr(member, "id", member))
        return None


async def rename_from_panel(panel: PetPanel, interaction: Any) -> bool:
    """``Zmień imię``: ask for the name in a pop-up window, then name the pet like ``!nazwij``.

    The guards and the 23-hour cooldown of the command apply (the cooldown is
    the command's own, so a name given with a button counts for ``!nazwij``
    and the other way round; a refused naming does not use it up).  Only the
    pet the panel shows is named (see :meth:`PetPanel.shows_active_pet`),
    checked when the button is clicked and again when the window is sent.

    The cooldown is charged on the bucket fetched again right before it, with
    nothing awaited in between: discord.py drops a never-used bucket from its
    cache whenever anybody's ``!nazwij`` looks at the cooldowns, so the bucket
    seen at the click may be an orphan by the time the window is sent.
    """
    bot = interaction.client
    if not await ui.gate(interaction) or not await panel.shows_active_pet(interaction):
        return False
    if not await panel._not_away(interaction):  # (a name can make it shiny: not while on a mission)
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
    if not await panel.shows_active_pet(submitted) or not await panel._not_away(submitted):
        return False  # (the window may have been open for minutes)
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
    An egg is "jajko" without its species: an egg does not tell its species before it hatches
    (owner, 2026-11-23 - the rule of the album, meant for the whole game; the stable's
    :func:`_pet_brief` and ``!towarzysz`` never named it).  Its name, talent and badges stay.
    """
    name = discord.utils.escape_markdown(str(pet["pet_name"] or "")[:20])
    level = _int(pet["pet_lvl"])
    about = ["jajko"] if level == 0 else [POLISH_TYPES.get(pet["type"], ""), f"poziom {level}"]
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
    # (A pet on a companion mission detects nothing: it is not at its owner's side.)
    away = sql_not_away("p.PET_ID", await missions_table_ready(bot))
    rows = await bot.db.fetch(
        f"""SELECT DISTINCT o.PLAYER_ID FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
            WHERE p.DETECTION = TRUE AND o.PET_ID > 0 AND o.PLAYER_ID > 0 AND {away}""")
    rarity = _RARITY_NAMES[min(max(_int(boss_rarity), 0), len(_RARITY_NAMES) - 1)]
    text = bossdetect.dm_warning(bot, MSG_BOSS_WARNING.format(rarity=rarity))
    log.info("Boss detection: warning %d player(s) about a boss (%s).", len(rows), rarity)
    for row in rows:
        player_id = _int(row["player_id"])
        if bossdetect.is_off(bot, player_id):
            continue  # switched off by the player (button under the hint / !wyczuwanie)
        try:
            await _warn_about_boss(bot, player_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Boss detection: could not warn player %s: %s: %s",
                        player_id, type(exc).__name__, exc)
