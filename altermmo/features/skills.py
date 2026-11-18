"""Skill gems: generation, the gem of a player, using it, daily / weekly cooldowns.

Port of the legacy ``functions/functions_skills.py``.

``SKILLS``            one row per generated gem (no primary key, as in production):
                      id, name, variant (what the gem does) and a "slow" percentage
                      that is only displayed;
``PETOWNER.SKILL_GEM``  the id of the player's ACTIVE gem (0 / NULL = none).  The
                      table belongs to features/pets.py;
``SKILLPOUCH``        new in v2: the other gems of a player, one row per gem
                      (player id, gem id).  A player can collect one gem of
                      every kind; only the active one works (``$skill``).
                      ``$klejnoty`` lists them, ``$wybierzklejnot <nr>`` makes
                      another one active.  The old bot never looks at this
                      table and still sees the active gem in ``PETOWNER``.

A gem is never used up: after a use it "regenerates" until the next daily
(variants 1, 4, 5, 6) or weekly (variants 2, 3) reset.  The cooldown lists are
the legacy files ``skill_daily_player_cd.txt`` / ``skill_weekly_player_cd.txt``
(``bot.store.skill_daily_cd`` / ``skill_weekly_cd``, one id per line).

Owner's rule for players with several gems: ONE gem use a day.  Every use -
also of a weekly gem - puts the player on the daily list, and a player on the
daily list cannot use any gem until the daily reset.  (With a single gem
nothing changes: a daily gem works once a day, a weekly one once a week.)

Differences from the legacy code (apart from the gem pouch described above the
game rules themselves are unchanged):

* every statement is parametrised and a database outage raises
  ``DatabaseUnavailable`` instead of returning False / reading an unbound variable;
* gem ids are allocated under a lock, an empty ``SKILLS`` table no longer
  crashes the generator and ``create_skills_table`` never drops anything;
* a missing ``SKILLS`` row, a NULL ``SKILL_GEM`` or an unknown variant no
  longer crash ``$pokazskill`` / ``$skill``;
* the cooldown is used up exactly when the gem did something: a Discord
  failure while *announcing* an effect no longer leaves the effect in force
  with the gem still ready (it could be repeated for ever);
* the summoning gem refuses - without using anything up - while a boss is on
  the channel or arriving; the boss itself is put on the channel by
  :func:`skill_summon_boss` through ``bot.boss_cycle`` after the busy block;
* the rest listens for reactions from before its message is sent until the end
  and counts only the rest emoji on its own message, never the bot's own click;
* the "tag a traveller" question is a :func:`altermmo.core.challenge.ask`
  round: the bot listens before it asks, only on its channel, and a connection
  problem does not cost the player the weekly use.

The cooldown helpers (``daily_skill_used`` ...) take the bot as their first
argument: the files live in ``bot.store`` now.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, Sequence

import discord

from ..core import challenge, io, timing
from ..core.state import EventType
from ..settings import ASSETS_DIR
from . import modifiers, pets

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)


# -- schema (exactly the production table) --------------------------------------------

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS SKILLS (
           SKILL_ID NUMERIC,
           GEM_NAME VARCHAR(255),
           SKILL_VARIANT NUMERIC,
           SLOW_PERC NUMERIC
       )""",
    # New in v2 (not in the old bot's database): gems a player owns besides the active one.
    """CREATE TABLE IF NOT EXISTS SKILLPOUCH (
           PLAYER_ID NUMERIC,
           SKILL_ID NUMERIC
       )""",
)
#: Tables the old bot's database does not have yet; created at start-up when missing.
NEW_TABLES: tuple[str, ...] = (SCHEMA[1],)

#: Advisory lock that serialises the allocation of ``SKILL_ID`` (the table has
#: no key).  Two-int form, next to the namespaces used by features/pets.py.
_LOCK_SKILL_IDS = 7303


# -- game rules -----------------------------------------------------------------------

#: What a gem does (``SKILLS.SKILL_VARIANT``).
REST, SHINY, SUMMON, FESTIVAL, SLOW, LOOT = 1, 2, 3, 4, 5, 6

#: Gems that regenerate with the weekly reset; all the others with the daily one.
WEEKLY_GEMS = (SHINY, SUMMON)

#: A new gem draws its variant with ``randint(*GENERATED_VARIANTS)``.  Gems 4-6
#: can be shown and used but are never generated - exactly as in the legacy bot.
GENERATED_VARIANTS = (1, 3)
#: ``randint`` range of the displayed "additional slow" of a new gem.
SLOW_PERC_RANGE = (0, 20)

GEM_NAMES: dict[int, str] = {
    REST: "Klejnot Regeneracji",
    SHINY: "Klejnot Wyjątkowości",
    SUMMON: "Klejnot Przywołania",
    FESTIVAL: "Klejnot Błogosławieństwa",
    SLOW: "Klejnot Spowolnienia",
    LOOT: "Klejnot Obfitości",
}
UNKNOWN_GEM_NAME = "Nieznany klejnot"

SKILL_DESCRIPTIONS: dict[int, str] = {
    REST: "Klejnot regeneracji pozwala na odpoczynek wszystkich podróżników, którzy są chętni. "
          "Zaraz po nabraniu sił mogą ponownie wyruszyć na polowanie.",
    SHINY: "Klejnot wyjątkowości sprawia, że towarzysz wybranego podróżnika zostaje odmieniony "
           "i staje się wyjątkowy.",
    SUMMON: "Klejnot przywołania uwalnia potężnego bossa. Każdy może go zaatakować.",
    FESTIVAL: "Klejnot błogosławieństwa sprawia, że na potwory i bossów spływają efekty magiczne.",
    SLOW: "Klejnot spowolnienia spowalnia nadciągające potwory oraz bossa.",
    LOOT: "Klejnot obfitości sprawia, że następne potwory oraz boss będą oferowały lepsze nagrody.",
}
UNKNOWN_SKILL_DESCRIPTION = "Nieznany klejnot umiejętności."
SKILL_EMBED_COLOR = 0xfc7703
SKILL_EMBED_FOOTER = "Wielcy ludzie nie rodzą się wielkimi, tylko się nimi stają."

#: The rest lasts this long (seconds); the short one is for the debug mode.
REST_SECONDS, REST_SECONDS_DEBUG = 300, 15
REST_IMAGES = 5  # assets/events/rest/0.png .. 4.png
REST_COLOR = 0xFC0303
REST_EMOJI = "<:Bedge:970576892874854400>"
REST_EMOJI_ID = 970576892874854400
FESTIVAL_EMOJI = "<:prayge:1063891597760139304>"
#: The festival prays at the shrine this many times.
FESTIVAL_PRAYERS = 5
#: Seconds to tag the traveller whose pet becomes shiny.
SHINY_TIMEOUT = 30
#: What the slowing / the abundance gem add to the modifiers of the next boss.
SLOW_TIME_BONUS = 40
LOOT_DROP_BONUS, LOOT_POINTS_BONUS = 40, 3
#: The summoned boss: ``randint`` range of its rarity before the shrine boost.
SUMMON_RARITY_RANGE = (2, 4)

MSG_NO_SKILL = ("Niestety nie posiadasz umiejętności <@{player_id}> <:Sadge:936907659142111273> "
                "Klejnoty umiejętności są bardzo rzadkie, więc musisz się bardzo postarać, żeby je zdobyć!")
MSG_NO_SUCH_GEM = "Taki klejnot umiejętności nie istnieje!"
MSG_DAILY_CD = "Twój klejnot umiejętności regeneruje się! Spróbuj jutro! <:Bedge:970576892874854400>"
MSG_WEEKLY_CD = ("Twój klejnot umiejętności regeneruje się! Spróbuj w nadchodzącym tygodniu! "
                 "<:Bedge:970576892874854400>")
MSG_ONE_GEM_A_DAY = ("Dziś użyłeś już klejnotu umiejętności, <@{player_id}>! Jednego dnia działa tylko jeden "
                     "klejnot - spróbuj jutro <:Bedge:970576892874854400>")
MSG_REST_DONE = ("Wszyscy podróżnicy, którzy skorzystali z zaproszenia, mogą ponownie ruszyć na polowanie! "
                 "<:Up:912798893304086558>")
MSG_SHINY_ASK = "Oznacz użytkownika, którego towarzysz ma stać się wyjątkowy!"
MSG_SHINY_TIMEOUT = "Moc kamienia zanikła..."
MSG_NO_SUCH_TRAVELLER = ("Niestety podany podróżnik nie istnieje. "
                         "Moc Twojego kamienia umiejętności została wyczerpana.")
MSG_TRAVELLER_WITHOUT_PET = ("Niestety podany podróżnik nie posiada aktualnie towarzysza "
                             "<:Sadge:936907659142111273> Moc Twojego kamienia umiejętności została wyczerpana.")
MSG_FESTIVAL_DONE = ("Wygląda na to, że bossowie i potwory odczuwają efekty Twojego kamienia umiejętności "
                     "<:MonkaChrist:783992337075929098>")
MSG_SLOW_DONE = "Klejnot sprawia, że przeciwnicy lepią się od błota i poruszają się znacznie wolniej."
MSG_LOOT_DONE = "Klejnot sprawia, że przeciwnicy oferują teraz lepsze nagrody."
MSG_NO_SUCH_NUMBER = ("Nie masz klejnotu o takim numerze, <@{player_id}>. Wpisz **$klejnoty**, żeby zobaczyć "
                      "swoje klejnoty, a potem np. **$wybierzklejnot 2**.")
MSG_ALREADY_ACTIVE = "**{name}** jest już Twoim aktywnym klejnotem, <@{player_id}>."
MSG_GEM_CHOSEN = ("Aktywny klejnot <@{player_id}>: **{name}**. Użyjesz go komendą **$skill**. "
                  "Pozostałe klejnoty czekają w sakwie (**$klejnoty**).")
POUCH_TITLE = "Sakwa z klejnotami umiejętności"
POUCH_FOOTER = "Działa tylko aktywny klejnot, jeden dziennie. Zmiana: $wybierzklejnot numer. Użycie: $skill."
# New in v2 (the legacy bot had no such situations, see the module docstring).
MSG_EVENT_IN_THE_WAY = ("Trwa teraz inne wydarzenie, <@{player_id}>! Klejnot przywołania nie został użyty - "
                        "spróbuj ponownie, gdy się zakończy <:Bedge:970576892874854400>")
MSG_BOSS_IN_THE_WAY = ("Boss już jest na kanale albo właśnie nadciąga, <@{player_id}>! Klejnot przywołania "
                       "nie został użyty - spróbuj ponownie później <:Bedge:970576892874854400>")
MSG_INTERRUPTED = ("Problemy z połączeniem przerwały użycie klejnotu <:Sadge:936907659142111273> "
                   "Moc Twojego kamienia umiejętności nie została wyczerpana - spróbuj ponownie za chwilę.")

#: A mention as Discord writes it into a message: ``<@id>`` (``<@!id>`` in older clients).
_MENTION = re.compile(r"<@!?(\d{1,20})>")


class SkillInterrupted(Exception):
    """Connection problems stopped a gem in the middle; its cooldown must not be used up."""


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


def _affected(command_tag: str) -> int:
    """Number of rows from a command tag such as ``UPDATE 1``."""
    try:
        return int(str(command_tag).split()[-1])
    except (ValueError, IndexError):
        return 0


def gem_name(skill_variant: int) -> str:
    """The name a gem of this variant gets when it is generated."""
    return GEM_NAMES.get(int(skill_variant), UNKNOWN_GEM_NAME)


async def _announce(scene: "Scene", text: str) -> None:
    """Tell the channel about an effect that is already in force.

    The effect cannot be taken back, so a failed send must not abort the
    caller before it uses up the cooldown: the failure goes to the log and to
    the owner instead.
    """
    try:
        await scene.send(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Skill effect applied but could not be announced: %s: %s", type(exc).__name__, exc)
        await scene.bot.log_to_owner("Nie udało się ogłosić efektu klejnotu umiejętności na kanale "
                                     f"({type(exc).__name__}). Efekt działa: {text}")


async def _apply_effects(bot: "AlterBot", skill: str,
                         steps: Sequence[Callable[[], Awaitable[Any]]]) -> None:
    """Apply the parts of one gem effect, one after another.

    A failure before anything was applied propagates: nothing happened and the
    cooldown stays unused.  Once a part is in force the remaining ones are
    still attempted and their failures only reported - the gem did work, so
    the caller goes on and uses up the cooldown (otherwise a broken file write
    would let the player stack the first parts again and again).
    """
    applied = 0
    for number, step in enumerate(steps, start=1):
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if applied == 0:
                raise
            log.exception("%s: part %d/%d of the effect failed.", skill, number, len(steps))
            await bot.log_to_owner(f"Klejnot umiejętności ({skill}): część efektu {number}/{len(steps)} "
                                   f"nie zadziałała: `{type(exc).__name__}: {exc}`")
        else:
            applied += 1


async def _send_picture(scene: "Scene", picture: Path) -> None:
    """Post a decorative picture; neither a missing file nor a failed upload stops the flow."""
    if not picture.is_file():
        log.warning("Picture %s is missing; going on without it.", picture)
        return
    try:
        await scene.send(file_path=picture)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - decoration only
        log.warning("Could not post the picture %s: %s: %s", picture, type(exc).__name__, exc)


# ==================================== FUNCTIONS FOR DATABASES =================================

async def create_skills_table(bot: "AlterBot") -> None:
    """Create ``SKILLS`` when it is missing; the legacy default row only into an empty table."""
    await bot.db.execute(SCHEMA[0], idempotent=True)
    async with bot.db.transaction() as con:
        await con.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", _LOCK_SKILL_IDS)
        if await con.fetchval("SELECT 1 FROM SKILLS LIMIT 1") is None:
            await con.execute(
                "INSERT INTO SKILLS (SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC) VALUES ($1, $2, $3, $4)",
                0, "Default", 0, 0)
            log.info("Table SKILLS seeded with the default row.")


async def _insert_gem(con: Any, variant: int | None = None) -> int:
    """Draw a new gem and store it on ``con`` (inside a transaction); returns its id.

    ``variant`` fixes what the gem does; by default it is drawn.
    """
    skill_variant = random.randint(*GENERATED_VARIANTS) if variant is None else int(variant)
    slow_perc = random.randint(*SLOW_PERC_RANGE)
    await con.execute("SELECT pg_advisory_xact_lock($1::int4, 0)", _LOCK_SKILL_IDS)
    skill_id = _int(await con.fetchval("SELECT COALESCE(MAX(SKILL_ID), 0) + 1 FROM SKILLS"))
    await con.execute(
        "INSERT INTO SKILLS (SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC) VALUES ($1, $2, $3, $4)",
        skill_id, gem_name(skill_variant), skill_variant, slow_perc)
    log.info("Skill gem %s generated: %s (variant %s, slow %s %%).",
             skill_id, gem_name(skill_variant), skill_variant, slow_perc)
    return skill_id


async def generate_skill_gem(scene: "Scene") -> int:
    """Generate skill: a new row of ``SKILLS``; returns its ``SKILL_ID``."""
    async with scene.bot.db.transaction() as con:
        return await _insert_gem(con)


async def _gem_of(q: Any, player_id: int) -> int | None:
    """The id of the player's ACTIVE gem: 0 when there is none, None without a ``PETOWNER`` row.

    The table has no unique key; should a player have more rows, the one that
    holds a gem counts.
    """
    rows = await q.fetch("SELECT SKILL_GEM FROM PETOWNER WHERE PLAYER_ID = $1", player_id)
    if not rows:
        return None
    return max(_int(row["skill_gem"]) for row in rows)


async def _ensure_pouch(bot: "AlterBot") -> None:
    """Create ``SKILLPOUCH`` when it is still missing (checked once per run).

    The bot creates it at start-up; this covers a first start during which the
    database could not be reached.
    """
    if getattr(bot, "_skill_pouch_ready", False):
        return
    if not await bot.db.fetchval("SELECT to_regclass('skillpouch') IS NOT NULL"):
        await bot.db.execute(SCHEMA[1], idempotent=True)
        log.info("Table SKILLPOUCH created.")
    bot._skill_pouch_ready = True


async def _owned_gems(con: Any, player_id: int) -> tuple[int, list[int]] | None:
    """``(active gem id, ids in the pouch)`` of a player; None without a ``PETOWNER`` row.

    Locks the player's ``PETOWNER`` rows, so everything that changes the gems
    of one player (a new gem, choosing the active one) runs one after another.
    """
    rows = await con.fetch("SELECT SKILL_GEM FROM PETOWNER WHERE PLAYER_ID = $1 FOR UPDATE", player_id)
    if not rows:
        return None
    active = max(max(_int(row["skill_gem"]) for row in rows), 0)
    stored = await con.fetch("SELECT SKILL_ID FROM SKILLPOUCH WHERE PLAYER_ID = $1 ORDER BY SKILL_ID", player_id)
    pouch: list[int] = []
    for row in stored:
        skill_id = _int(row["skill_id"])
        if skill_id > 0 and skill_id != active and skill_id not in pouch:
            pouch.append(skill_id)
    return active, pouch


async def _gem_rows(q: Any, skill_ids: Sequence[int]) -> dict[int, Any]:
    """``SKILLS`` rows of the given gems by id (gems without a row are left out)."""
    if not skill_ids:
        return {}
    rows = await q.fetch(
        """SELECT SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC FROM SKILLS
           WHERE SKILL_ID = ANY($1::numeric[])""", [int(skill_id) for skill_id in skill_ids])
    return {_int(row["skill_id"]): row for row in rows}


async def assign_skill(scene: "Scene", player: Any, variant: int | None = None) -> bool:
    """Assigning new skill to player in database, but first check if it is possible.

    A player collects one gem of every kind.  The new gem is of a kind the
    player does not have yet (drawn as before; ``variant`` forces a kind).  The
    first gem becomes the active one, the next ones go to the pouch
    (``$klejnoty`` / ``$wybierzklejnot``).  True when the player got a new gem,
    False when they already have every kind that can drop (or the forced one).
    """
    bot = scene.bot
    player_id = _pid(player)
    await _ensure_pouch(bot)

    for attempt in (1, 2):
        async with bot.db.transaction() as con:
            owned = await _owned_gems(con, player_id)
            if owned is not None:
                active, pouch = owned
                rows = await _gem_rows(con, [active, *pouch])
                have = {_int(row["skill_variant"]) for row in rows.values()} - {0}
                if variant is not None:
                    missing = [int(variant)] if int(variant) not in have else []
                else:
                    missing = [kind for kind in range(GENERATED_VARIANTS[0], GENERATED_VARIANTS[1] + 1)
                               if kind not in have]
                if not missing:
                    log.info("Player %s already has every skill gem that can drop.", player_id)
                    return False
                drawn = random.randint(*GENERATED_VARIANTS) if variant is None else int(variant)
                skill_id = await _insert_gem(con, drawn if drawn in missing else random.choice(missing))
                if active > 0 and active in rows:
                    await con.execute("INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID) VALUES ($1, $2)",
                                      player_id, skill_id)
                    log.info("Skill gem %s added to the pouch of player %s.", skill_id, player_id)
                else:
                    await con.execute("UPDATE PETOWNER SET SKILL_GEM = $2 WHERE PLAYER_ID = $1",
                                      player_id, skill_id)
                    log.info("Skill gem %s assigned to player %s (active).", skill_id, player_id)
                return True
        if attempt == 1:
            # No PETOWNER row yet: create an empty one (a parallel flow that was
            # faster is fine - the row is merged, never duplicated) and try again.
            await pets.new_record_petowners(bot, player_id, 0, False, 0, 0, 0, 0, 0)
    log.error("Could not create the PETOWNER row of player %s; no skill gem assigned.", player_id)
    return False


async def get_skill_data(scene: "Scene", skill_id: int) -> Any:
    """Get all data about the skill with given skill_id.

    Returns the row (``SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC``) or, after
    telling the channel, None when there is no such gem.
    """
    skill_data = await scene.bot.db.fetchrow(
        "SELECT SKILL_ID, GEM_NAME, SKILL_VARIANT, SLOW_PERC FROM SKILLS WHERE SKILL_ID = $1", int(skill_id))
    if skill_data is None:
        log.warning("Skill gem %s does not exist in SKILLS.", skill_id)
        await scene.send(MSG_NO_SUCH_GEM)
    return skill_data


async def _player_gem(scene: "Scene", player_id: int) -> tuple[str, int, int] | None:
    """``(name, variant, slow %)`` of the player's gem; None - after saying why - without one."""
    skill_id = await _gem_of(scene.bot.db, player_id)
    if skill_id is None or skill_id <= 0:
        await scene.send(MSG_NO_SKILL.format(player_id=player_id))
        return None
    skill_data = await get_skill_data(scene, skill_id)
    if skill_data is None:
        return None
    skill_variant = _int(skill_data["skill_variant"])
    if skill_variant == 0:
        await scene.send(MSG_NO_SKILL.format(player_id=player_id))
        return None
    skill_name = skill_data["gem_name"]
    return (UNKNOWN_GEM_NAME if skill_name is None else str(skill_name),
            skill_variant, _int(skill_data["slow_perc"]))


# ==================================== THE POUCH ================================================

async def _numbered_gems(q: Any, active: int, pouch: Sequence[int]) -> list[tuple[int, Any]]:
    """The player's gems as ``(gem id, SKILLS row)`` in the order of their numbers.

    The number of a gem is its place by gem id, so it does not change when
    another gem becomes active.  Gems without a usable ``SKILLS`` row are left out.
    """
    rows = await _gem_rows(q, [skill_id for skill_id in (active, *pouch) if skill_id > 0])
    return [(skill_id, rows[skill_id]) for skill_id in sorted(rows)
            if _int(rows[skill_id]["skill_variant"]) != 0]


def _gem_label(row: Any) -> str:
    name = row["gem_name"]
    return UNKNOWN_GEM_NAME if name is None else str(name)


async def show_skills(scene: "Scene") -> bool:
    """``$klejnoty``: every gem of the author with its number; False when they have none."""
    bot = scene.bot
    player_id = int(scene.author.id)
    await _ensure_pouch(bot)
    async with bot.db.transaction() as con:
        owned = await _owned_gems(con, player_id)
        gems = await _numbered_gems(con, *owned) if owned is not None else []
    if not gems:
        await scene.send(MSG_NO_SKILL.format(player_id=player_id))
        return False
    active = owned[0]

    lines = []
    for number, (skill_id, row) in enumerate(gems, start=1):
        cooldown = _cooldown_note(bot, player_id, _int(row["skill_variant"]))
        if skill_id == active:
            lines.append(f"**{number}. {_gem_label(row)}** ✅ aktywny{cooldown}")
        else:
            lines.append(f"{number}. {_gem_label(row)}{cooldown}")
    description = (f"Oto Twoje klejnoty umiejętności, <@{player_id}>.\n\n" + "\n".join(lines)
                   + "\n\nZmień aktywny klejnot komendą **$wybierzklejnot numer**, "
                     "a szczegóły aktywnego zobaczysz przez **$pokazskill**.")
    embed = discord.Embed(title=POUCH_TITLE, description=description, color=SKILL_EMBED_COLOR)
    embed.set_footer(text=POUCH_FOOTER)
    await scene.send(embed=embed)
    return True


def _cooldown_note(bot: "AlterBot", player_id: int, skill_variant: int) -> str:
    """" (regeneruje się ...)" when a gem of this kind cannot be used right now."""
    if skill_variant in WEEKLY_GEMS and load_weekly_skill(bot, player_id):
        return " *(regeneruje się do końca tygodnia)*"
    return " *(regeneruje się do jutra)*" if load_daily_skill(bot, player_id) else ""


async def choose_skill(scene: "Scene", number: Any) -> bool:
    """``$wybierzklejnot <nr>``: make gem ``number`` of ``$klejnoty`` the active one.

    The gem that was active goes to the pouch.  True when the active gem changed.
    """
    bot = scene.bot
    player_id = int(scene.author.id)
    try:
        wanted = int(str(number).strip())
    except (TypeError, ValueError):
        wanted = 0

    status, name = "none", ""
    await _ensure_pouch(bot)
    async with bot.db.transaction() as con:
        owned = await _owned_gems(con, player_id)
        gems = await _numbered_gems(con, *owned) if owned is not None else []
        if gems:
            active = owned[0]
            if not 1 <= wanted <= len(gems):
                status = "bad_number"
            else:
                skill_id, row = gems[wanted - 1]
                name = _gem_label(row)
                if skill_id == active:
                    status = "already"
                else:
                    await con.execute("DELETE FROM SKILLPOUCH WHERE PLAYER_ID = $1 AND SKILL_ID = $2",
                                      player_id, skill_id)
                    if active > 0:
                        await con.execute(
                            """INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID)
                               SELECT $1::numeric, $2::numeric WHERE NOT EXISTS
                                   (SELECT 1 FROM SKILLPOUCH WHERE PLAYER_ID = $1 AND SKILL_ID = $2)""",
                            player_id, active)
                    await con.execute("UPDATE PETOWNER SET SKILL_GEM = $2 WHERE PLAYER_ID = $1",
                                      player_id, skill_id)
                    status = "chosen"
                    log.info("Player %s made the skill gem %s active (was %s).", player_id, skill_id, active)

    if status == "none":
        await scene.send(MSG_NO_SKILL.format(player_id=player_id))
    elif status == "bad_number":
        await scene.send(MSG_NO_SUCH_NUMBER.format(player_id=player_id))
    elif status == "already":
        await scene.send(MSG_ALREADY_ACTIVE.format(player_id=player_id, name=name))
    else:
        await scene.send(MSG_GEM_CHOSEN.format(player_id=player_id, name=name))
    return status == "chosen"


# ==================================== SHOWING AND USING ========================================

async def show_skill(scene: "Scene") -> bool:
    """Show current skill of the scene's author; False when they have none."""
    player_id = int(scene.author.id)
    gem = await _player_gem(scene, player_id)
    if gem is None:
        return False
    skill_name, skill_variant, skill_slow_perc = gem

    if skill_variant in SKILL_DESCRIPTIONS:
        skill_desc = (SKILL_DESCRIPTIONS[skill_variant]
                      + f"\n\nDodatkowe spowolnienie: {skill_slow_perc} %."
                      + "\n\nMożesz użyć umiejętności przez komendę **$skill**.")
    else:
        skill_desc = UNKNOWN_SKILL_DESCRIPTION

    embed = discord.Embed(title=skill_name,
                          description="Oto Twój klejnot umiejętności, <@" + str(player_id) + ">.\n\n" + skill_desc,
                          color=SKILL_EMBED_COLOR)
    embed.set_footer(text=SKILL_EMBED_FOOTER)
    path = ASSETS_DIR / "skill_gems" / f"{skill_variant}.png"
    if path.is_file():
        embed.set_image(url=f"attachment://{skill_variant}.png")
        await scene.send(file_path=path, embed=embed)
    else:
        log.warning("Skill gem picture %s is missing; showing the gem without it.", path)
        await scene.send(embed=embed)
    return True


async def use_skill(scene: "Scene", player: Any) -> int:
    """Use skill based on the type of the skill.

    Returns the variant of the gem that was handled, 0 when nothing was used
    (no gem, the summoning gem on cooldown or blocked by a boss, a use
    interrupted by connection problems).  For :data:`SUMMON` (3) the caller
    must finish with :func:`skill_summon_boss` once the busy flag is released.
    """
    bot = scene.bot
    player_id = _pid(player)
    if scene.author is None or scene.author.id != player_id:
        scene = scene.with_author(player, scene.message)  # the gem works for its owner

    gem = await _player_gem(scene, player_id)
    if gem is None:
        return 0
    skill_name, skill_variant, _ = gem
    log.info("Player %s uses the skill gem %r (variant %s).", player_id, skill_name, skill_variant)

    # Cooldowns, in one place: a weekly gem once a week, and one gem use a day
    # whichever gem it is (owner's rule for players who collect several gems).
    weekly = skill_variant in WEEKLY_GEMS
    if weekly and load_weekly_skill(bot, player_id):
        await scene.send(MSG_WEEKLY_CD)
        return 0
    if load_daily_skill(bot, player_id):
        await scene.send(MSG_ONE_GEM_A_DAY.format(player_id=player_id) if weekly else MSG_DAILY_CD)
        return 0

    if skill_variant == REST:
        await skill_rest(scene)
        daily_skill_used(bot, player_id)
        return REST
    if skill_variant == SHINY:
        try:
            await skill_make_shiny(scene)
        except SkillInterrupted:
            return 0
        weekly_skill_used(bot, player_id)
        daily_skill_used(bot, player_id)
        return SHINY
    if skill_variant == SUMMON:
        if await skill_spawn_boss(scene):
            weekly_skill_used(bot, player_id)
            daily_skill_used(bot, player_id)
            return SUMMON
        return 0
    if skill_variant == FESTIVAL:
        await skill_shrine_festival(scene)
        daily_skill_used(bot, player_id)
        return FESTIVAL
    if skill_variant == SLOW:
        await skill_slow(scene)
        daily_skill_used(bot, player_id)
        return SLOW
    if skill_variant == LOOT:
        await skill_increase_loot(scene)
        daily_skill_used(bot, player_id)
        return LOOT

    log.warning("Player %s has a skill gem of the unknown variant %s.", player_id, skill_variant)
    return 0


# ==================================== THE GEMS =================================================

def _is_rest_emoji(emoji: Any) -> bool:
    return getattr(emoji, "id", None) == REST_EMOJI_ID or str(emoji) == REST_EMOJI


@contextlib.contextmanager
def _rest_clicks(bot: "AlterBot") -> Iterator[list[tuple[int, Any, bool]]]:
    """Record every click on the rest emoji as ``(message id, user, added?)`` during the block.

    Listeners that stay for the whole rest: a click is never lost between two
    waits, taking a reaction back counts as well, and - because they are
    registered before the invitation is sent - neither is the fastest player.
    """
    clicks: list[tuple[int, Any, bool]] = []

    async def on_reaction_add(reaction: Any, user: Any) -> None:
        if _is_rest_emoji(reaction.emoji):
            clicks.append((reaction.message.id, user, True))

    async def on_reaction_remove(reaction: Any, user: Any) -> None:
        if _is_rest_emoji(reaction.emoji):
            clicks.append((reaction.message.id, user, False))

    with io.reaction_listener(bot, on_reaction_add, on_reaction_remove):
        yield clicks


async def _rest_reactors(scene: "Scene", message: Any) -> list[int] | None:
    """Ids of the people who have the rest emoji on ``message`` right now, read from Discord.

    None when Discord cannot be asked: the caller then uses the clicks it heard.
    Reading the final list (as the legacy bot did) also covers clicks made
    while the connection was down.
    """
    try:
        fresh = await io.retry(lambda: scene.channel.fetch_message(message.id), what="fetch_message")
        ids: list[int] = []
        for reaction in fresh.reactions:
            if not _is_rest_emoji(reaction.emoji):
                continue
            async for user in reaction.users():
                if not getattr(user, "bot", False):
                    ids.append(user.id)
        return ids
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the clicks heard live are good enough
        log.info("Rest reactions could not be read from Discord (%s: %s); using the ones heard live.",
                 type(exc).__name__, exc)
        return None


async def skill_rest(scene: "Scene") -> bool:
    """Skill which organizes meal. Everyone who use reaction can go hunting again."""
    bot = scene.bot
    author = scene.author

    if load_daily_skill(bot, author.id):
        await scene.send(MSG_DAILY_CD)
        return False

    e_title = f"<:peepofat:1062666941405331507> {author.name} zaprasza na odpoczynek! <:peepofat:1062666941405331507>"

    e_descr = ('Wspaniały dzień! Wszyscy bohaterowie zostali zaproszeni na ucztę!\n\n'
               '**Zostaw reakcję, żeby wypocząć i móc ponownie wyruszyć na polowanie. '
               'Odpoczynek potrwa maksymalnie 5 minut.**')

    embed = discord.Embed(title=e_title, description=e_descr, color=REST_COLOR)

    with _rest_clicks(bot) as clicks:
        await _send_picture(scene, ASSETS_DIR / "events" / "rest" / f"{random.randint(0, REST_IMAGES - 1)}.png")
        msg = await scene.send(embed=embed)
        if not await io.safe_react(msg, REST_EMOJI):
            log.warning("Could not add the rest reaction; players have to pick the emoji themselves.")
        log.info("Rest of %s started.", author)
        await timing.pause(REST_SECONDS_DEBUG if bot.debug else REST_SECONDS)

    # Who has the reaction on the invitation at the end (clicks taken back do not count).
    resting: dict[int, None] = {}
    for message_id, user, added in clicks:
        if message_id != msg.id or getattr(user, "bot", False):
            continue
        if added:
            resting[user.id] = None
        else:
            resting.pop(user.id, None)
    fetched = await _rest_reactors(scene, msg)
    if fetched is not None:
        resting.update(dict.fromkeys(fetched))  # clicks made while the connection was down
    guild = scene.guild
    users = [str(user_id) for user_id in resting if guild.get_member(user_id) is not None]
    log.info("Rest of %s is over: %d traveller(s) rested.", author, len(users))

    if users:
        from . import daily  # lazy: daily -> modifiers / pets, and it may not be loaded yet
        await daily.remove_player_daily_file(bot, users)
    await _announce(scene, MSG_REST_DONE)
    return True


async def _ask_for_traveller(scene: "Scene") -> challenge.RoundResult:
    """Ask the author to tag somebody; lag never counts as "no answer".

    Raises :class:`SkillInterrupted` after
    ``challenge.MAX_CONSECUTIVE_DISRUPTIONS`` disrupted rounds in a row.
    """
    for attempt in range(1, challenge.MAX_CONSECUTIVE_DISRUPTIONS + 1):
        result = await challenge.ask(scene.bot, scene.channel, MSG_SHINY_ASK, players=[scene.author],
                                     timeout=timing.timeout(SHINY_TIMEOUT))
        if result.outcome is not challenge.Outcome.DISRUPTED:
            return result
        log.warning("Skill question disrupted (%d/%d in a row).", attempt, challenge.MAX_CONSECUTIVE_DISRUPTIONS)
        if attempt < challenge.MAX_CONSECUTIVE_DISRUPTIONS:
            await challenge.notify_disrupted(scene.channel)
    await io.send_patient(scene.channel, MSG_INTERRUPTED, give_up_after=120)
    raise SkillInterrupted(f"{challenge.MAX_CONSECUTIVE_DISRUPTIONS} questions in a row were disrupted")


async def skill_make_shiny(scene: "Scene") -> bool:
    """Skill that is used to make a pet shiny; True when a pet became shiny.

    Raises :class:`SkillInterrupted` when connection problems made it
    impossible to ask whose pet it should be.
    """
    bot = scene.bot
    author = scene.author

    if load_weekly_skill(bot, author.id):
        await scene.send(MSG_WEEKLY_CD)
        return False

    result = await _ask_for_traveller(scene)
    if not result.answered:
        await scene.send(MSG_SHINY_TIMEOUT)
        return False

    match = _MENTION.fullmatch(str(result.message.content).strip())
    if match is None:
        await scene.send(MSG_NO_SUCH_TRAVELLER)
        return False
    user_id = int(match.group(1))

    # Check if user exists (somebody who left the server has no companion to change)
    if await bot.member(user_id) is None:
        await scene.send(MSG_NO_SUCH_TRAVELLER)
        return False

    changed = await bot.db.execute(
        """UPDATE PETS SET SHINY = TRUE
           WHERE PET_ID IN (SELECT PET_ID FROM PETOWNER WHERE PLAYER_ID = $1 AND PET_ID > 0)""",
        user_id, idempotent=True)
    if _affected(changed) == 0:
        await scene.send(MSG_TRAVELLER_WITHOUT_PET)
        return False

    log.info("Skill gem of %s made the pet of player %s shiny.", author.id, user_id)
    await _announce(scene, f"Twój Towarzysz <@{user_id}> wygląda na bardzo wdzięcznego i dostrzegasz" +
                           f", że wygląda jakoś inaczej <:Susge:973591024322633858> To sprawka <@{author.id}>.")
    return True


async def skill_spawn_boss(scene: "Scene") -> bool:
    """Skill that is used to summon a boss: may the author's gem summon one now?

    Nothing is used up and nothing appears here; after True the caller marks
    the weekly use and - outside the busy block - calls :func:`skill_summon_boss`.
    """
    bot = scene.bot

    if load_weekly_skill(bot, scene.author.id):
        await scene.send(MSG_WEEKLY_CD)
        return False
    if not bot.boss_cycle.can_summon():
        await scene.send(MSG_BOSS_IN_THE_WAY.format(player_id=scene.author.id))
        return False
    state = bot.state
    if state.event_alive and state.event_type in (EventType.DUEL, EventType.RITUAL):
        # Their sign-up leaves the channel free; a boss now would wipe it.
        await scene.send(MSG_EVENT_IN_THE_WAY.format(player_id=scene.author.id))
        return False
    return True


async def skill_summon_boss(scene: "Scene") -> bool:
    """Put the boss of the summoning gem on the channel (legacy part of ``$uzyjskilla``).

    Call it after :func:`use_skill` returned :data:`SUMMON` and the busy flag
    was released.  Should the boss not appear after all (the scheduled one
    started to arrive at this very moment, Discord failed), the author gets
    the weekly use of the gem back.
    """
    bot = scene.bot

    rarity = random.randint(*SUMMON_RARITY_RANGE)
    # Load all modifiers from file
    loaded = await modifiers.load_modifiers(bot)
    rarity += _int(loaded.get("rarity_boost", 0))
    if rarity == 3 or rarity > 4:
        rarity = 4

    summoned = False
    try:
        summoned = bool(await bot.boss_cycle.summon(scene, rarity))
    finally:
        if not summoned and scene.author is not None:
            bot.store.skill_weekly_cd.remove([scene.author.id])
            bot.store.skill_daily_cd.remove([scene.author.id])  # ...and today's one use
            log.info("Boss of the summoning gem did not appear; player %s keeps the weekly use.",
                     scene.author.id)
    if not summoned:
        await scene.send(MSG_BOSS_IN_THE_WAY.format(player_id=getattr(scene.author, "id", 0)))
        return False
    log.info("Boss of rarity %s summoned by the skill gem of %s.", rarity, getattr(scene.author, "id", None))
    return True


async def skill_shrine_festival(scene: "Scene") -> bool:
    """Skill that is used to pray to shrine 5x."""
    bot = scene.bot

    if load_daily_skill(bot, scene.author.id):
        await scene.send(MSG_DAILY_CD)
        return False

    await io.safe_react(scene.message, FESTIVAL_EMOJI)
    await _apply_effects(bot, "skill_shrine_festival",
                         [lambda: modifiers.random_modifiers(scene, False)] * FESTIVAL_PRAYERS)
    await _announce(scene, MSG_FESTIVAL_DONE)
    return True


async def skill_slow(scene: "Scene") -> bool:
    """Skill that is used to add slow during next fight."""
    bot = scene.bot

    if load_daily_skill(bot, scene.author.id):
        await scene.send(MSG_DAILY_CD)
        return False

    await modifiers.modify_modifiers(bot, "time_increased_perc", SLOW_TIME_BONUS)
    await _announce(scene, MSG_SLOW_DONE)
    return True


async def skill_increase_loot(scene: "Scene") -> bool:
    """Skill that is used to add additional loot during next fight."""
    bot = scene.bot

    if load_daily_skill(bot, scene.author.id):
        await scene.send(MSG_DAILY_CD)
        return False

    await _apply_effects(bot, "skill_increase_loot", [
        lambda: modifiers.modify_modifiers(bot, "drop_boost_perc", LOOT_DROP_BONUS),
        lambda: modifiers.modify_modifiers(bot, "points_boost", LOOT_POINTS_BONUS),
    ])
    await _announce(scene, MSG_LOOT_DONE)
    return True


# ==================================== COOLDOWNS ================================================

# DAILY
def daily_skill_used(bot: "AlterBot", player_id: int | str) -> None:
    """Save the player to the list of today's skill users (once)."""
    if not bot.store.skill_daily_cd.contains(player_id):
        bot.store.skill_daily_cd.add(player_id)


def load_daily_skill(bot: "AlterBot", player_id: int | str) -> bool:
    """True when the player's daily gem was already used today."""
    return bot.store.skill_daily_cd.contains(player_id)


async def clear_daily_skill(bot: "AlterBot") -> None:
    """Clear all daily skill users (the daily reset)."""
    bot.store.skill_daily_cd.clear()
    log.info("Daily skill cooldowns cleared.")
    await bot.log_to_owner("Zresetowano cooldown na daily skille.")


# WEEKLY
def weekly_skill_used(bot: "AlterBot", player_id: int | str) -> None:
    """Save the player to the list of this week's skill users (once)."""
    if not bot.store.skill_weekly_cd.contains(player_id):
        bot.store.skill_weekly_cd.add(player_id)


def load_weekly_skill(bot: "AlterBot", player_id: int | str) -> bool:
    """True when the player's weekly gem was already used this week."""
    return bot.store.skill_weekly_cd.contains(player_id)


async def clear_weekly_skill(bot: "AlterBot") -> None:
    """Clear all weekly skill users (the weekly reset)."""
    bot.store.skill_weekly_cd.clear()
    log.info("Weekly skill cooldowns cleared.")
    await bot.log_to_owner("Zresetowano cooldown na weekly skille.")
