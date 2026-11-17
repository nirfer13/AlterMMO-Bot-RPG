"""Pets (companions): eggs, hatching, talents, rerolls, the stable, boss detection.

Port of the legacy ``functions/functions_pets.py``.  Tables (unchanged, shared
with the legacy bot - neither has a primary key):

``PETOWNER``  one row per player: the active pet, four stable slots, the pet
              items (reroll scrolls and their shards, rebirth stones, mirrors)
              and the skill gem (owned by features/skills.py);
``PETS``      one row per pet: name, level 0 (egg) .. 3, talent (``PET_SKILLS``),
              looks and the seven stats used by the fights.

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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, Sequence

import discord

from ..core import io, timing
from ..settings import ASSETS_DIR
from . import modifiers, patrons

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


# -- schema (exactly the production tables) -------------------------------------------

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
           MYTHIC BOOLEAN NOT NULL DEFAULT FALSE
       )""",
)

# -- game rules -----------------------------------------------------------------------

#: Eggs a player without a patron/crafter rank can find (legacy order).
STANDARD_EGGS: tuple[PetType, ...] = (
    PetType.BEAR, PetType.BOAR, PetType.CAT, PetType.RABBIT, PetType.SHEEP, PetType.DOG,
    PetType.EAGLE)
#: Eggs a patron/crafter can find.
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

#: ``$mocneodrodzenie``: 1️⃣ .. 7️⃣ (one per entry of SKILL_POOL) and the confirmation.
SKILL_EMOTES: tuple[str, ...] = tuple(f"{digit}️⃣" for digit in range(1, 8))
ACCEPT_EMOTE = "✅"
CONFIRM_WORD = "$potwierdzam"
CONFIRM_TIMEOUT = 15
MENU_TIMEOUT = 20

# -- texts (verbatim from the legacy bot) ---------------------------------------------

MSG_UNSURE = "*Twój towarzysz spogląda na Ciebie niepewnie.*"
MSG_ALONE_MAD = "Przecież jesteś sam... <:madge:882184635474386974>"
MSG_ALREADY_WAITING = ("Już czekam na Twoje potwierdzenie. Wpisz **$potwierdzam** albo poczekaj "
                       "aż prośba wygaśnie.")
MSG_NO_PET_DATA = "Nie znaleziono danych towarzysza."
MSG_STORED = "Towarzysz został ulokowany w stajni <:peepoBlush:984769061340737586>"
MSG_UNSTORED = "Towarzysz został wyciągnięty ze stajni <:peepoBlush:984769061340737586>"
MSG_SWAPPED = ("Nowy towarzysz został wyciągnięty ze stajni, a stary został w niej schowany "
               "<:peepoBlush:984769061340737586>")
MSG_EMPTY_SLOT = "W stajni nie ma towarzysza na podanym miejscu. Sprawdź stajnie wpisując **$stajnia**."
MSG_TOO_MANY_SKILLS = ("Wybrałeś za dużo statystyk. Można pominąć **maksymalnie 3**! "
                       "<:madge:882184635474386974>")
MSG_ADV_REROLL_MENU = (
    "Wybierz, które umiejętności chcesz pominąć przy przelosowaniu."
    "\nKażda wybrana statystyka to koszt 10 zwojów, maksymalnie 3 do wyboru:"
    f"\n\n{SKILL_EMOTES[0]} - Szansa na krytyczne uderzenie,"
    f"\n{SKILL_EMOTES[1]} - Szansa na zastąpienie ataku,"
    f"\n{SKILL_EMOTES[2]} - Szansa na zablokowanie ataku,"
    f"\n{SKILL_EMOTES[3]} - Rzadszy drop,"
    f"\n{SKILL_EMOTES[4]} - Zmniejszenie życia przeciwnika,"
    f"\n{SKILL_EMOTES[5]} - Spowolnienie przeciwnika,"
    f"\n{SKILL_EMOTES[6]} - Wyczuwanie bossa,"
    f"\n\n*Potwierdź {ACCEPT_EMOTE}.*")
PET_FOOTER = "Na zawsze ponosisz odpowiedzialność za to, co oswoiłeś."
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
_SQL_SAVE_PET = """UPDATE PETS SET PET_LVL = $2, PET_SKILLS = $3, VARIANT = $4,
                       CRIT_PERC = $5, REPLACE_PERC = $6, DEF_PERC = $7, DROP_PERC = $8,
                       LOWHP_PERC = $9, SLOW_PERC = $10, DETECTION = $11
                   WHERE PET_ID = $1"""


def _for_update(sql: str, lock: bool) -> str:
    return sql + " FOR UPDATE" if lock else sql


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


def _pet_embed(player_id: int, pet: Any, items: Any) -> discord.Embed:
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

    name, level = pet["pet_name"], _int(pet["pet_lvl"])
    if level == 0:
        title = f'Jajko {name}' if name != DEFAULT_NAME else 'Jajko'
        description = ("Oto Twój towarzysz, <@" + str(player_id) + ">. Opiekuj się nim, a być może "
                       "kiedyś coś z niego wyrośnie...\n" + scroll_desc + add_desc)
    else:
        polish_type = POLISH_TYPES.get(pet["type"], "")
        title = f'{polish_type} {name}' if name != DEFAULT_NAME else f'{polish_type}'
        skill_desc = "📜 **STATYSTYKI:**\n"
        skill_desc += f"Poziom: {level}\n"
        skill_desc += f"Talent: {_talent(_int(pet['pet_skills']))}\n"
        look_name = "Mityczny" if pet["quality"] == "Mythic" else pet["quality"]
        skill_desc += f"Wygląd: {look_name}\n\n"
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


async def show_pet(scene: "Scene", player: Any) -> bool:
    """Show the player's pet and his scrolls; False when he has no pet."""
    bot = scene.bot
    player_id = _pid(player)
    columns = "PET_ID, REROLL_SCROLL, REROLL_SCROLL_SHARD, REBIRTH_STONES, MIRRORS"
    owner = await _owner(bot.db, player_id, columns)
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
        owner = await _owner(bot.db, player_id, columns) or owner

    pet = await _pet(bot.db, pet_id)
    if pet is None:
        log.warning("Player %s owns pet %s which has no row in PETS.", player_id, pet_id)
        await scene.send(MSG_NO_PET_DATA)
        return False

    embed = _pet_embed(player_id, pet, owner)
    image = _pet_image(pet)
    if image is None or not image.is_file():
        log.warning("Pet %s (%s, %s, level %s, variant %s) has no picture (%s); showing it without one.",
                    pet_id, pet["quality"], pet["type"], pet["pet_lvl"], pet["variant"], image)
        await scene.send(embed=embed)
    else:
        embed.set_image(url=f"attachment://{image.name}")
        await scene.send(file_path=image, embed=embed)
    return True


# -- eggs -----------------------------------------------------------------------------

def _roll_egg(player: Any) -> dict[str, Any]:
    """Draw a new egg for ``player`` (patrons and crafters can find premium eggs)."""
    percentage = random.randint(0, 1000)
    mythic = percentage >= MYTHIC_FROM
    if mythic:
        pets_list = MYTHIC_EGGS
    elif patrons.check_if_patron(player):
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
    return {"TYPE": pet.value, "QUALITY": quality, "VARIANT": variant,
            "SHINY": percentage >= SHINY_FROM, "ULTRA_SHINY": percentage >= ULTRA_SHINY_FROM,
            "MYTHIC": mythic}


async def _insert_egg(con: Any, egg: dict[str, Any]) -> int:
    """Store a rolled egg under the next free PET_ID; returns that id."""
    await con.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", _LOCK_PET_IDS)
    pet_id = _int(await con.fetchval("SELECT COALESCE(MAX(PET_ID), 0) + 1 FROM PETS"))
    await con.execute(
        """INSERT INTO PETS (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY, MYTHIC,
               TYPE, VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC, LOWHP_PERC, SLOW_PERC,
               INIT_PERC, DETECTION)
           VALUES ($1, $2, 0, 0, $3, $4, $5, $6, $7, $8, 0, 0, 0, 0, 0, 0, 0, FALSE)""",
        pet_id, DEFAULT_NAME, egg["QUALITY"], egg["SHINY"], egg["ULTRA_SHINY"], egg["MYTHIC"],
        egg["TYPE"], str(egg["VARIANT"]))
    log.info("Pet egg %s generated: %s", pet_id, egg)
    return pet_id


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
    egg = _roll_egg(player)
    async with bot.db.transaction() as con:
        pet_id = await _insert_egg(con, egg)
    if egg["MYTHIC"]:
        await _announce_mythic(scene, _pid(player) if player is not None else bot.ids.owner)
    return pet_id


async def assign_pet(scene: "Scene", player: Any) -> bool:
    """Give the player a new egg; False when he already has a pet."""
    bot = scene.bot
    player_id = _pid(player)
    async with bot.db.transaction() as con:
        await _lock_player(con, player_id)
        owner = await _owner(con, player_id, "PET_ID")
        if owner is not None and _int(owner["pet_id"]) > 0:
            log.debug("Player %s already has a pet; no egg assigned.", player_id)
            return False
        egg = _roll_egg(player)
        pet_id = await _insert_egg(con, egg)
        if owner is not None:
            await con.execute("UPDATE PETOWNER SET PET_ID = $2 WHERE PLAYER_ID = $1", player_id, pet_id)
        else:
            await _insert_owner(con, player_id, pet_id, True, 0, 0, 0, 0, 0)
    log.info("Pet %s assigned to player %s.", pet_id, player_id)
    if egg["MYTHIC"]:
        await _announce_mythic(scene, player_id)
    return True


async def reassign_pet(bot: "AlterBot", pet_id: Any, player_id: Any) -> bool:
    """Give an existing pet to a player without an active pet; True when it was done."""
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


async def _confirm(scene: "Scene", player: Any, prompt: str) -> Any:
    """Ask ``prompt`` and wait 15 s for the player's ``$potwierdzam``; the message or None."""
    waiter = asyncio.ensure_future(io.wait_for_message(
        scene.bot, scene.channel, authors=[player],
        check=lambda message: message.content.strip().lower() == CONFIRM_WORD, timeout=None))
    try:
        await asyncio.sleep(0)  # the listener is registered before the question becomes visible
        await scene.send(prompt)
        try:
            return await asyncio.wait_for(waiter, timing.timeout(CONFIRM_TIMEOUT))
        except asyncio.TimeoutError:
            return None
    finally:
        if not waiter.done():
            waiter.cancel()


async def _confirmed_change(scene: "Scene", player: Any, *, prompt: str, messages: dict[str, str],
                            check: Callable[..., Awaitable[tuple[str, Any]]],
                            apply: Callable[[Any, Any], Awaitable[None]]) -> bool:
    """Common flow of ``$odrodzenie``, ``$oswiecenie`` and ``$transformacja``.

    ``check(q, lock)`` says whether the change is possible (``_OK``) or which of
    ``messages`` explains why not.  It runs before the question and once more,
    with the rows locked, in the transaction that applies the change - whatever
    happened during the 15 seconds, nothing is paid for twice or with items
    that are gone.
    """
    bot = scene.bot
    player_id = _pid(player)
    status, _ = await check(bot.db, False)
    if status != _OK:
        await scene.send(messages[status])
        return False
    with _confirmation_slot(player_id) as free:
        if not free:
            await scene.send(MSG_ALREADY_WAITING)
            return False
        answer = await _confirm(scene, player, prompt)
        if answer is None:
            await scene.send(MSG_UNSURE)
            return False
        await io.safe_react(answer, "<:PepoG:790963160528977980>")
        async with bot.db.transaction() as con:
            status, data = await check(con, True)
            if status == _OK:
                await apply(con, data)
    if status != _OK:
        await scene.send(messages[status])
        return False
    await show_pet(scene, player)
    return True


async def reroll_pet(scene: "Scene", player: Any, remove_list: Sequence[int] = ()) -> bool:
    """Reroll pet stats for a scroll; ``remove_list`` (indexes of SKILL_POOL) costs 10 each."""
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
                + "? <:Hmm:984767035617730620> Wpisz **$potwierdzam**."),
        messages={
            "alone": _msg_alone(player_id),
            "no_data": MSG_NO_PET_DATA,
            "poor": ("Nie masz wystarczająco zwojów odrodzenia " + mention + " <:Sadge:936907659142111273> "
                     "Wpisz **$towarzysz**, żeby sprawdzić ich ilość. Zwoje możesz zdobyć na polowaniu "
                     "lub po zabiciu bossów."),
        })


async def adv_reroll_pet(scene: "Scene") -> bool:
    """Advanced reroll: the player first picks (reactions) up to 3 stats to leave out."""
    bot = scene.bot
    user = scene.author
    react_msg = await scene.send(MSG_ADV_REROLL_MENU)
    selected: set[int] = set()
    clicks: asyncio.Queue[tuple[bool, str]] = asyncio.Queue()

    def mine(reaction: Any, member: Any) -> bool:
        """The author's own click on the menu (never the bot's reactions)."""
        return (not getattr(member, "bot", False) and member.id == user.id
                and reaction.message.id == react_msg.id)

    async def on_reaction_add(reaction: Any, member: Any) -> None:
        if mine(reaction, member):
            clicks.put_nowait((True, str(reaction.emoji)))

    async def on_reaction_remove(reaction: Any, member: Any) -> None:
        if mine(reaction, member):
            clicks.put_nowait((False, str(reaction.emoji)))

    async def decorate() -> None:
        for emote in (*SKILL_EMOTES, ACCEPT_EMOTE):
            await io.safe_react(react_msg, emote)

    # Listeners that stay for the whole menu instead of one ``wait_for`` per
    # click: two clicks delivered together would otherwise lose the second one
    # (nobody listens between two ``wait_for`` calls), and taking a reaction
    # back has to count as well.  The bot's own reactions are added in the
    # background, so a player who clicks while the menu fills in is heard too.
    bot.add_listener(on_reaction_add, "on_reaction_add")
    bot.add_listener(on_reaction_remove, "on_reaction_remove")
    decorating = asyncio.ensure_future(decorate())
    try:
        while True:
            try:
                added, emoji = await asyncio.wait_for(clicks.get(), timing.timeout(MENU_TIMEOUT))
            except asyncio.TimeoutError:
                await io.safe_delete(react_msg)
                await scene.send(MSG_UNSURE)
                return False
            if emoji == ACCEPT_EMOTE and added:
                break
            if emoji in SKILL_EMOTES:
                index = SKILL_EMOTES.index(emoji)
                if added:
                    selected.add(index)
                else:
                    selected.discard(index)
    finally:
        bot.remove_listener(on_reaction_add, "on_reaction_add")
        bot.remove_listener(on_reaction_remove, "on_reaction_remove")
        if not decorating.done():
            decorating.cancel()

    await scene.send("Zobaczmy...")
    ignored_skills = sorted(selected)
    if len(ignored_skills) > 3:
        await scene.send(MSG_TOO_MANY_SKILLS)
        return False
    return await reroll_pet(scene, user, ignored_skills)


async def transform_pet(scene: "Scene", player: Any) -> bool:
    """Transform pet appearance for a mirror (patrons and crafters only)."""
    player_id = _pid(player)
    mention = "<@" + str(player_id) + ">"

    async def check(q: Any, lock: bool) -> tuple[str, Any]:
        owner = await _owner(q, player_id, "PET_ID, MIRRORS", lock=lock)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id <= 0:
            return "alone", None
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
                + "? <:Hmm:984767035617730620> Wpisz **$potwierdzam**."),
        messages={
            "alone": _msg_alone(player_id),
            "no_data": MSG_NO_PET_DATA,
            "mythic": "Mitycznego towarzysza nie można transformować, " + mention + "!",
            "no_rank": ("Musisz mieć rangę Craftera lub Patrona " + mention + ", żeby przetransformować "
                        "towarzysza <:Sadge:936907659142111273> Rangi do zdobycia możesz zobaczyć na tym "
                        "kanale <#688296443156365354>."),
            "poor": ("Nie masz żadnych lustr wizualiów " + mention + " <:Sadge:936907659142111273> "
                     "Wpisz **$towarzysz**, żeby sprawdzić ich ilość. Lustra możesz zdobyć na polowaniu "
                     "lub po zabiciu bossów, jednak są one bardzo rzadkie."),
        })


async def enlight_pet(scene: "Scene", player: Any) -> bool:
    """Enlighting pet (1-3 more talent points for a level 3 pet) for a rebirth stone."""
    player_id = _pid(player)
    mention = "<@" + str(player_id) + ">"
    unworthy = ("Niestety Twój towarzysz nie jest godny dostąpić oświecenia " + mention
                + " <:Sadge:936907659142111273> Wpisz **$towarzysz**, żeby sprawdzić jego poziom.")

    async def check(q: Any, lock: bool) -> tuple[str, Any]:
        owner = await _owner(q, player_id, "PET_ID, REBIRTH_STONES", lock=lock)
        pet_id = _int(owner["pet_id"]) if owner is not None else 0
        if pet_id <= 0:
            return "alone", None
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
                + "? <:Hmm:984767035617730620> Wpisz **$potwierdzam**."),
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


async def discard_pet(scene: "Scene") -> bool:
    """Discarding pet from the author if it is possible (a sacrifice: a random boss modifier)."""
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
        answer = await _confirm(
            scene, player,
            "Czy jesteś pewien, że chcesz porzucić swojego towarzysza <@" + str(player_id)
            + ">? <:MonkaS:882181709100097587> Wpisz **$potwierdzam**.")
        if answer is None:
            await scene.send("*Twój towarzysz oddycha z ulgą...*")
            return False
        # The pet that is active *now* goes, together with its row in PETS.
        async with bot.db.transaction() as con:
            owner = await _owner(con, player_id, "PET_ID", lock=True)
            pet_id = _int(owner["pet_id"]) if owner is not None else 0
            if pet_id > 0:
                await con.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", player_id)
                await con.execute("DELETE FROM PETS WHERE PET_ID = $1", pet_id)
        if pet_id <= 0:
            await scene.send(MSG_ALONE_MAD)
            return False
        log.info("Player %s discarded pet %s.", player_id, pet_id)
        await modifiers.random_modifiers(scene, True, True)
        await io.safe_react(answer, "<:MonkaS:882181709100097587>")
    return True


async def name_pet(scene: "Scene", name: str) -> bool:
    """Set name of the author's pet (15% chance that it turns shiny); False without a pet."""
    bot = scene.bot
    player_id = _pid(scene.author)
    name = str(name)[:MAX_NAME_LENGTH]
    owner = await _owner(bot.db, player_id, "PET_ID")
    pet_id = _int(owner["pet_id"]) if owner is not None else 0
    if pet_id <= 0:
        await scene.send(MSG_ALONE_MAD)
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


async def unstore_pet(scene: "Scene", slot: Any) -> bool:
    """Take a pet out of stable slot 1-4 (the active pet takes its place)."""
    bot = scene.bot
    player_id = _pid(scene.author)
    column = _slot_column(slot)
    if column is None:
        await scene.send(_msg_bad_slot(player_id, "wyciagnijtowarzysza"))
        return False
    if player_id in _awaiting_confirmation:
        await scene.send(MSG_ALREADY_WAITING)
        return False
    async with bot.db.transaction() as con:
        owner = await _owner(con, player_id, _STABLE_COLUMNS, lock=True)
        if owner is None:
            status = "alone"
        elif _int(owner[column.lower()]) <= 0:
            status = "empty"
        else:
            current = max(_int(owner["pet_id"]), 0)
            status = "swapped" if current > 0 else "taken"
            await con.execute(f"UPDATE PETOWNER SET {column} = $2, PET_ID = $3 WHERE PLAYER_ID = $1",
                              player_id, current, _int(owner[column.lower()]))
    await scene.send({"alone": _msg_alone(player_id), "empty": MSG_EMPTY_SLOT,
                      "swapped": MSG_SWAPPED, "taken": MSG_UNSTORED}[status])
    return status in ("swapped", "taken")


async def check_stable(scene: "Scene") -> bool:
    """Show the author's active pet and the four stable slots; False when he has no row."""
    bot = scene.bot
    player_id = _pid(scene.author)
    owner = await _owner(bot.db, player_id, _STABLE_COLUMNS)
    if owner is None:
        await scene.send(_msg_alone(player_id))
        return False

    pet_ids = [_int(value) for value in owner]
    wanted = [pet_id for pet_id in pet_ids if pet_id > 0]
    pets_by_id: dict[int, Any] = {}
    if wanted:
        rows = await bot.db.fetch(
            "SELECT PET_ID, PET_NAME, PET_LVL, TYPE FROM PETS WHERE PET_ID = ANY($1::numeric[])", wanted)
        for row in rows:
            pets_by_id.setdefault(_int(row["pet_id"]), row)

    descriptions = []
    for index, pet_id in enumerate(pet_ids):
        label = "Obecnie posiadany pet" if index == 0 else f"Pet w stajni {index}"
        if pet_id <= 0:
            descriptions.append(f"{label}: Brak.\n")
            continue
        pet = pets_by_id.get(pet_id)
        if pet is None:
            descriptions.append(f"{label}: Brak danych w tabeli PETS.\n")
            continue
        pet_lvl = _int(pet["pet_lvl"])
        polish_type = "Jajko" if pet_lvl == 0 else POLISH_TYPES.get(pet["type"], "")
        descriptions.append(f"{label}: {polish_type} {pet['pet_name']} ({pet_lvl} LVL).\n")

    embed = discord.Embed(
        title="Stajnia",
        description=("Oto Twoja stajnia, <@" + str(player_id) + "> <:peepoBlush:984769061340737586>\n\n"
                     + "".join(descriptions)),
        color=0x34cceb,
    )
    embed.set_footer(text="Możesz schować lub wyciągnąć peta za pomocą komend $schowajtowarzysza "
                          "oraz $wyciagnijtowarzysza.")
    embed.set_thumbnail(url="https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png")
    await scene.send(embed=embed)
    return True


# -- ranking and boss detection -------------------------------------------------------

async def pet_ranking(scene: "Scene") -> None:
    """Show the ten most talented pets."""
    rows = await scene.bot.db.fetch(
        """SELECT PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, TYPE, ULTRA_SHINY, MYTHIC FROM PETS
           WHERE PET_ID > 0
           ORDER BY PET_SKILLS DESC NULLS LAST, SLOW_PERC DESC NULLS LAST, PET_ID LIMIT 10""")

    def yes_no(flag: Any) -> str:
        return "Tak" if flag is True else "Nie"

    ranking_string = ""
    for place, pet in enumerate(rows, start=1):
        ranking_string += (
            f"{place}. **{str(pet['pet_name'] or '')[:20]}** ({_int(pet['pet_lvl'])} lvl) - "
            f"Talent: {_talent(_int(pet['pet_skills']))} - Wygląd: {pet['quality']} - "
            f"Shiny: {yes_no(pet['shiny'])} - Ultra shiny: {yes_no(pet['ultra_shiny'])} - "
            f"Mityczny: {yes_no(pet['mythic'])} - Typ: {POLISH_TYPES.get(pet['type'], '')}.\n")

    emb = discord.Embed(title='Najbardziej utalentowani towarzysze!', url=RANKING_IMAGE,
                        description=ranking_string, color=0xFF0000)
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
        await io.send(channel, "<@" + str(player_id) + "> - Twój towarzysz: *Nadciąga boss! Odblokuj "
                               "prywatne wiadomości, jeśli chcesz otrzymywać powiadomienia!*", attempts=1)


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
        try:
            await _warn_about_boss(bot, player_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Boss detection: could not warn player %s: %s: %s",
                        player_id, type(exc).__name__, exc)
