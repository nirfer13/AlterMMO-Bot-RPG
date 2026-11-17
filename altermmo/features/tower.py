"""The weekly Death Tower (``$wieża``): up to ten floors, each a harder typing fight.

Port of the legacy ``functions/functions_tower.py``.

Differences from the legacy code (the game itself is unchanged):

* fight rounds go through :func:`altermmo.core.challenge.ask` (see
  ``features/daily.py``); the legacy "lag compensation" is gone;
* the answering player is recognised by id (the legacy ``msg.author is player``
  identity test only worked by accident of discord.py's member cache);
* the weekly cooldown is still stored before the first floor, but a run that
  ends because of a technical problem gives the attempt back
  (:func:`explore_tower`);
* a cleared floor stays cleared: loot, the tower record and the achievement
  role are guarded one by one, a failure is reported to the owner;
* the record is stored with one statement inside a transaction, the time
  always as ``H:MM:SS.ffffff`` (``str(timedelta)`` dropped the fraction for
  whole seconds and the legacy comparison then crashed);
* ``create_database`` no longer drops the table.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import discord

from ..core import challenge, io, timing
from ..core.challenge import Outcome
from ..settings import ASSETS_DIR
from . import achievements, boss, daily, database, patrons

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS TOWER (
           PLAYER_ID NUMERIC PRIMARY KEY,
           TOWER_LEVEL NUMERIC,
           TIME VARCHAR(255)
       )""",
)

# -- numbers of the game ------------------------------------------------------------

#: Floors of one run.
TOWER_FLOORS = 10
#: Seconds for the first word of a floor; later words get ``8 / floor`` seconds.
FIRST_ROUND_TIME = daily.FIRST_ROUND_TIME
ROUND_TIME_BASE = 8

#: Floor -> rarity of the boss loot table used for its reward (other floors: epic).
_FLOOR_LOOT_RARITY: dict[int, int] = {1: 0, 2: 0, 3: 1, 4: 1, 5: 2, 6: 2, 7: 2, 8: 4, 9: 4, 10: 4}
_FLOOR_LOOT_FALLBACK = 2

#: Floor -> collector's achievement role for patrons and crafters.
_FLOOR_ROLES: dict[int, int] = {
    3: achievements.TOWER_ROLES[0],
    6: achievements.TOWER_ROLES[1],
    10: achievements.TOWER_ROLES[2],
}

# -- texts ---------------------------------------------------------------------------

TOWER_IMAGE = ("https://www.altermmo.pl/wp-content/uploads/alter0000_Huge_mystery_gloomy_tower_with_small_"
               "lights_inside._F_2fe81cd9-bff9-4bbe-af41-8101162ec6b9.png")
TOWER_COLOUR = 0xFF0019

MSG_FIGHT_START = ("Rozpoczyna się walka w Wieży Śmierci, <@{user_id}>! Każde piętro to coraz trudniejszy "
                   "boss do pokonania <:REEeee:790963160495947856>! Wpisz pojawiające się słowa tak szybko, "
                   "jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez spacji! Przygotuj się!")
MSG_FLOOR_CLEARED = "Brawo <@{user_id}> <:ok:990161663053422592> Piętro zaliczone!"
MSG_MISTAKE_LOST = ("Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie w przyszłym tygodniu! "
                    "<:RIP:912797982917816341> Twój rekord został zapisany!")
MSG_TOO_SLOW_LOST = ("Niestety nie zdążyłeś! <:Bedge:970576892874854400>  Spróbuj ponownie w przyszłym "
                     "tygodniu! <:RIP:912797982917816341> Twój rekord został zapisany!")
MSG_NO_RECORD = "Jeszcze nie udało Ci się zeksplorować Wieży Śmierci.\n"

LOG_WEEKLY_RESET = "Zresetowano cooldown na weekly tower."

# New in v2 (the legacy bot had no notion of a run broken by technical problems).
MSG_TOWER_INTERRUPTED = ("Eksploracja Wieży Śmierci została przerwana z powodu problemów technicznych "
                         "<:Sadge:936907659142111273>")
MSG_ATTEMPT_RETURNED = ("<@{user_id}>, z powodu problemów technicznych Twoja wyprawa do Wieży Śmierci się "
                        "nie liczy - możesz spróbować ponownie w tym tygodniu! Zdobyte piętra i łupy "
                        "zostają.")


# -- database ------------------------------------------------------------------------

async def create_database(scene: "Scene | AlterBot") -> None:
    """Create the TOWER table when it is missing (existing records are kept).

    Takes a scene like the legacy function took a ``ctx``; a bare bot is
    accepted too because the legacy "create all databases" command called it
    without one.
    """
    bot = getattr(scene, "bot", scene)
    for statement in SCHEMA:
        await bot.db.execute(statement, idempotent=True)
    log.info("Table TOWER is in place.")


def _record_text(record_time: Any) -> str:
    """A floor time as it is stored: ``H:MM:SS.ffffff``."""
    if isinstance(record_time, timedelta):
        return database.format_turn_time(record_time)
    return str(record_time).strip()


def _as_timedelta(record_time: Any) -> timedelta | None:
    if isinstance(record_time, timedelta):
        return record_time
    return database.parse_turn_time(str(record_time))


async def new_record_tower(bot: "AlterBot", player_id: int, level: int, record_time: Any) -> bool:
    """Insert a player's first tower record; False when he already has one (nothing changes)."""
    status = await bot.db.execute(
        """INSERT INTO TOWER (PLAYER_ID, TOWER_LEVEL, TIME) VALUES ($1, $2, $3)
           ON CONFLICT (PLAYER_ID) DO NOTHING""",
        int(player_id), int(level), _record_text(record_time), idempotent=True)
    return status.endswith(" 1")


async def _store_record(bot: "AlterBot", player_id: int, level: int, record_time: Any) -> bool:
    """Keep the player's best floor (and the best time on it); True when the record changed."""
    player_id, level = int(player_id), int(level)
    text = _record_text(record_time)
    new_time = _as_timedelta(record_time)
    async with bot.db.transaction() as con:
        row = await con.fetchrow(
            "SELECT TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = $1 FOR UPDATE", player_id)
        if row is None:
            await con.execute(
                """INSERT INTO TOWER (PLAYER_ID, TOWER_LEVEL, TIME) VALUES ($1, $2, $3)
                   ON CONFLICT (PLAYER_ID) DO UPDATE
                       SET TOWER_LEVEL = EXCLUDED.TOWER_LEVEL, TIME = EXCLUDED.TIME
                       WHERE COALESCE(TOWER.TOWER_LEVEL, 0) < EXCLUDED.TOWER_LEVEL""",
                player_id, level, text)
            return True
        stored_level = int(row["tower_level"] or 0)
        if stored_level > level:
            return False
        if stored_level == level:
            stored_time = database.parse_turn_time(row["time"])
            # An unreadable stored time counts as beaten, like an unreadable boss record.
            if stored_time is not None and (new_time is None or new_time >= stored_time):
                return False
        await con.execute("UPDATE TOWER SET TOWER_LEVEL = $2, TIME = $3 WHERE PLAYER_ID = $1",
                          player_id, level, text)
        return True


async def _grant_floor_role(scene: "Scene", player: Any, level: int) -> bool:
    """Floors 3, 6 and 10 give patrons and crafters a collector's role."""
    role_id = _FLOOR_ROLES.get(int(level))
    if role_id is None or not patrons.check_if_patron(player):
        return False
    return bool(await achievements.set_achiev_role(scene, player, role_id))


async def update_tower_level(scene: "Scene", player: Any, level: int, record_time: Any) -> bool:
    """Store a cleared floor: the tower record and, for patrons, the achievement role.

    ``record_time`` is the time the floor took (a ``timedelta``, or text in the
    stored ``H:MM:SS.ffffff`` format).  Returns True, like the legacy function.
    """
    improved = await _store_record(scene.bot, player.id, level, record_time)
    log.info("Tower: %s (%s) cleared floor %s in %s (record %s).", player, player.id, level,
             _record_text(record_time), "updated" if improved else "kept")
    await _grant_floor_role(scene, player, level)
    return True


# -- a floor ---------------------------------------------------------------------------

async def tower_image(scene: "Scene", level: int) -> None:
    """Show the picture and the announcement of a floor."""
    level = int(level)
    if level == 1:
        description = ("Rozpoczynasz eksplorację Wieży Śmierci. Strzeż się!"
                       "\n\n*Crafterzy i Patroni mogą zdobyć kolekcjonerskie osiągnięcia!*")
    else:
        description = ("Przechodzisz na kolejne piętro. Powodzenia!"
                       "\n\n*Crafterzy i Patroni mogą zdobyć kolekcjonerskie osiągnięcia!*")
    embed = discord.Embed(
        title=f"<:AlterUp:846344753124999169> {level} piętro Wieży Śmierci! <:Loot:912797849916436570>",
        description=description, color=0xF00511)

    picture = ASSETS_DIR / "tower" / f"{level}.png"
    if picture.is_file():
        await scene.send(file_path=picture)
    else:
        log.warning("Tower picture %s is missing; showing the floor without it.", picture)
    await scene.send(embed=embed)


def random_tower_hp(level: int) -> int:
    """Number of correct words needed on a floor (before the pet's bonus)."""
    return 2 * int(level)


def floor_loot_rarity(level: int) -> int:
    """Rarity of the boss loot table that rewards a floor."""
    return _FLOOR_LOOT_RARITY.get(int(level), _FLOOR_LOOT_FALLBACK)


async def tower_fight(scene: "Scene", tower_level: int, player: Any = None) -> bool | None:
    """One floor of the tower: ``player`` (default: ``scene.author``) types the words.

    Returns True when the floor was cleared, False when the player lost and
    None when the fight was interrupted by repeated connection problems (the
    player is told, nothing is lost).  Technical failures before the last word
    propagate; afterwards the floor is cleared whatever happens.
    """
    bot = scene.bot
    channel = scene.channel
    tower_level = int(tower_level)
    hunter = player if player is not None else scene.author
    if hunter is None:
        raise ValueError("tower_fight needs a player: pass one or use a scene with an author")

    await scene.send(MSG_FIGHT_START.format(user_id=hunter.id))
    pet = await daily.pet_skills(bot, hunter.id)
    await timing.pause(8)
    await challenge.countdown(channel)

    floor_hp = daily.scaled_hp(random_tower_hp(tower_level), -pet["LOWHP_PERC"])
    round_time = float(ROUND_TIME_BASE / max(tower_level, 1)) * (100 + pet["SLOW_PERC"]) / 100
    log.info("Tower: %s (%s) on floor %s, hp %s, %.2f s per word.", hunter, hunter.id, tower_level,
             floor_hp, round_time)

    started = time.monotonic()
    turn = 0
    while True:
        turn += 1
        word, prompt = daily.MOB_ACTIONS[random.randint(0, len(daily.MOB_ACTIONS) - 1)]

        if not daily.roll(pet["REPLACE_PERC"]):
            limit = FIRST_ROUND_TIME if turn == 1 else round_time
            result = await daily.fight_round(bot, channel,
                                             str(turn) + ". **" + str(hunter) + "**: " + prompt,
                                             players=[hunter], limit=limit)
            if result is None:
                log.warning("Tower fight interrupted: too many disrupted rounds in a row.")
                await io.send_patient(channel, MSG_TOWER_INTERRUPTED, give_up_after=120)
                return None
            timed_out = result.outcome is Outcome.TIMEOUT
            correct = result.is_correct(word, by=hunter)
        else:
            await scene.send("~~" + str(turn) + ". " + str(hunter) + ": " + prompt + "~~ "
                             + daily.MSG_PET_ATTACKS)
            timed_out = False
            correct = True

        if correct:
            if daily.roll(pet["CRIT_PERC"]):
                turn += 1
            if turn >= floor_hp:
                break
            continue

        if daily.roll(pet["DEF_PERC"]):
            await scene.send(daily.MSG_TOO_SLOW_PROTECTED if timed_out else daily.MSG_MISTAKE_PROTECTED)
            continue

        await scene.send(MSG_TOO_SLOW_LOST if timed_out else MSG_MISTAKE_LOST)
        log.info("Tower: %s lost on floor %s (%s) in turn %s.", hunter, tower_level,
                 "timeout" if timed_out else "mistake", turn)
        return False

    # -- the floor is cleared: from here on nothing may turn it into a failure --
    fight_time = timedelta(seconds=time.monotonic() - started)
    context = "po zaliczonym piętrze Wieży Śmierci"
    await daily.guarded(bot, "ogłoszenie zwycięstwa",
                        scene.send(MSG_FLOOR_CLEARED.format(user_id=hunter.id)), context=context)
    await daily.guarded(bot, "czas walki", scene.send(daily.fight_time_message(fight_time, floor_hp)),
                        context=context)
    await daily.guarded(bot, f"łup <@{hunter.id}> za piętro {tower_level}",
                        boss.rand_loot(scene, floor_loot_rarity(tower_level), hunter, pet["DROP_PERC"]),
                        context=context)
    await daily.guarded(bot, f"rekord wieży <@{hunter.id}> (piętro {tower_level})",
                        _store_record(bot, hunter.id, tower_level, fight_time), context=context)
    await daily.guarded(bot, f"osiągnięcie wieży <@{hunter.id}> (piętro {tower_level})",
                        _grant_floor_role(scene, hunter, tower_level), context=context)
    return True


# -- $wieża ----------------------------------------------------------------------------

async def _give_back_attempt(scene: "Scene", player: Any) -> None:
    """Take the player off the weekly cooldown list and tell him (never raises)."""
    daily.release_cooldown(scene.bot.store.weekly_tower_cd, player.id)
    log.info("Tower run of %s (%s) given back after a technical problem.", player, player.id)
    await io.send_patient(scene.channel, MSG_ATTEMPT_RETURNED.format(user_id=player.id), give_up_after=60)


async def explore_tower(scene: "Scene") -> int:
    """The whole ``$wieża`` run of ``scene.author``; returns the number of floors cleared.

    The caller has already checked the cooldown and holds the busy flag.  The
    weekly cooldown is stored before the first floor (as in the legacy bot);
    when the run ends because of a technical problem the attempt is given back.
    """
    bot = scene.bot
    player = scene.author
    if player is None:
        raise ValueError("explore_tower needs a scene with an author")
    save_weekly_tower_to_file(bot, player.id)
    cleared = 0
    try:
        for level in range(1, TOWER_FLOORS + 1):
            await tower_image(scene, level)
            result = await tower_fight(scene, level, player)
            if result is None:
                await _give_back_attempt(scene, player)
                return cleared
            if not result:
                break
            cleared = level
    except asyncio.CancelledError:
        daily.release_cooldown(bot.store.weekly_tower_cd, player.id)  # e.g. the bot is shutting down
        raise
    except Exception:
        log.exception("Tower run of %s (%s) failed on floor %s; giving the attempt back.",
                      player, player.id, cleared + 1)
        await _give_back_attempt(scene, player)
        raise
    log.info("Tower: %s (%s) finished the run with %s floors cleared.", player, player.id, cleared)
    return cleared


# -- rankings --------------------------------------------------------------------------

def _tower_embed(title: str, description: str, footer: str) -> discord.Embed:
    embed = discord.Embed(title=title, url=TOWER_IMAGE, description=description, color=TOWER_COLOUR)
    embed.set_thumbnail(url=TOWER_IMAGE)
    embed.set_footer(text=footer)
    return embed


async def tower_ranking(scene: "Scene") -> None:
    """Post the ten best explorers of the tower (highest floor first, then the shortest time)."""
    bot = scene.bot
    rows = await bot.db.fetch(
        "SELECT PLAYER_ID, TOWER_LEVEL, TIME FROM TOWER ORDER BY TOWER_LEVEL DESC, TIME LIMIT 10")
    ranking = ""
    for place, row in enumerate(rows, start=1):
        player_id = int(row["player_id"])
        member = await bot.member(player_id)
        who = member.name if member is not None else f"<@{player_id}>"  # left the server
        ranking += (f"{place}. **{who}** - Poziom Wieży Śmierci: {int(row['tower_level'] or 0)} "
                    f"- Czas: {row['time']}.\n")
    await scene.send(embed=_tower_embed("Eksploratorzy Wieży Śmierci!", ranking,
                                        "Gratulacje dla nieustraszonych!"))


async def check_personal_tower(scene: "Scene") -> None:
    """Post the tower record of ``scene.author``."""
    player = scene.author
    row = await scene.bot.db.fetchrow(
        "SELECT TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = $1", int(player.id))
    if row is not None:
        text = (f"<@{player.id}> udało Ci się ukończyć Wieżę Śmierci na poziomie "
                f"**{int(row['tower_level'] or 0)}** w czasie **{row['time']}**.\n")
    else:
        text = MSG_NO_RECORD
    await scene.send(embed=_tower_embed("Personalny rekord Wieży Śmierci!", text, "Gratulacje!"))


# -- the weekly cooldown list ----------------------------------------------------------

def save_weekly_tower_to_file(bot: "AlterBot", player_id: int | str) -> None:
    """Remember that the player used this week's tower run (no duplicates)."""
    if not bot.store.weekly_tower_cd.contains(player_id):
        bot.store.weekly_tower_cd.add(player_id)


def load_weekly_tower_from_file(bot: "AlterBot", player_id: int | str) -> bool:
    """True when the player already entered the tower this week."""
    return bot.store.weekly_tower_cd.contains(player_id)


async def clear_weekly_tower_file(bot: "AlterBot") -> None:
    """Everybody may enter the tower again (weekly reset)."""
    bot.store.weekly_tower_cd.clear()
    log.info("Weekly tower cooldown reset.")
    await bot.log_to_owner(LOG_WEEKLY_RESET)
