"""Boss schedule, kill record, fight history and the weekly ranking tables.

Port of the legacy ``functions/functions_database.py``.  Tables (unchanged, so
both bot versions can use the same database):

``BOSS``     (ID NUMERIC, RARITY NUMERIC, RESPAWN_TIME TIMESTAMP, RESPAWN_STARTED BOOLEAN) - one row, ID 0
``RECORD``   (ID NUMERIC, RECORD_TIME VARCHAR(255), NICK VARCHAR(255))                     - one row, ID 0
``HISTORY``  (ID NUMERIC, FIGHT_TIME VARCHAR(255), NICK VARCHAR(255))                      - one row, ID 0
``RANKING``  (ID VARCHAR(255) PRIMARY KEY, NICK VARCHAR(255), POINTS NUMERIC)

Differences from the legacy code:

* every statement is parametrised (a nickname with an apostrophe used to break
  the SQL and with it the whole fight);
* "create table" helpers no longer DROP the table first - they only create what
  is missing; the weekly reset empties ``RANKING`` instead of dropping it;
* ranking points are added with a single atomic upsert;
* a member who left the server no longer crashes the ranking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord

from ..core import clock, io

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

DEFAULT_RECORD = "00:00:20.000"
DEFAULT_NICK = "Nieznajomy"
DEFAULT_FIGHT_TIME = "2020-05-27 15:00:00"

#: Ranking points for killing a boss of a given rarity (legacy values).
POINTS_BY_RARITY = {0: 1, 1: 3, 2: 6, 3: 4, 4: 12}
POINTS_FALLBACK = 4

RANKING_IMAGE = ("https://www.altermmo.pl/wp-content/uploads/alter0000_Ranking_of_monster_slayers."
                 "_Fantasy._Pixel_art._a79d5468-1397-4c68-af75-378d2946240f.png")
SUMMARY_IMAGE = "https://www.altermmo.pl/wp-content/uploads/SwordV2_Transparent-1.png"

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS BOSS (
           ID NUMERIC,
           RARITY NUMERIC,
           RESPAWN_TIME TIMESTAMP,
           RESPAWN_STARTED BOOLEAN
       )""",
    """CREATE TABLE IF NOT EXISTS RECORD (
           ID NUMERIC,
           RECORD_TIME VARCHAR(255),
           NICK VARCHAR(255)
       )""",
    """CREATE TABLE IF NOT EXISTS HISTORY (
           ID NUMERIC,
           FIGHT_TIME VARCHAR(255),
           NICK VARCHAR(255)
       )""",
    """CREATE TABLE IF NOT EXISTS RANKING (
           ID VARCHAR(255) PRIMARY KEY,
           NICK VARCHAR(255),
           POINTS NUMERIC
       )""",
)


@dataclass(frozen=True)
class BossRow:
    """The persisted boss schedule."""

    rarity: int
    #: When the scheduled boss should arrive (aware UTC), None when unknown.
    respawn_at: datetime | None
    #: True when a respawn is scheduled (legacy ``RESPAWN_STARTED``).
    respawn_started: bool


# -- create / reset ---------------------------------------------------------------

async def create_boss_table(bot: "AlterBot") -> None:
    await bot.db.execute(SCHEMA[0], idempotent=True)
    if await bot.db.fetchval("SELECT count(*) FROM BOSS") == 0:
        await bot.db.execute(
            "INSERT INTO BOSS (ID, RARITY, RESPAWN_TIME, RESPAWN_STARTED) VALUES (0, 1, $1, TRUE)",
            clock.legacy_now().replace(microsecond=0),
        )


async def create_record_table(bot: "AlterBot") -> None:
    await bot.db.execute(SCHEMA[1], idempotent=True)
    if await bot.db.fetchval("SELECT count(*) FROM RECORD") == 0:
        await bot.db.execute("INSERT INTO RECORD (ID, RECORD_TIME, NICK) VALUES (0, $1, $2)",
                             DEFAULT_RECORD, DEFAULT_NICK)


async def create_history_table(bot: "AlterBot") -> None:
    await bot.db.execute(SCHEMA[2], idempotent=True)
    if await bot.db.fetchval("SELECT count(*) FROM HISTORY") == 0:
        await bot.db.execute("INSERT INTO HISTORY (ID, FIGHT_TIME, NICK) VALUES (0, $1, $2)",
                             DEFAULT_FIGHT_TIME, DEFAULT_NICK)


async def create_ranking_table(bot: "AlterBot") -> None:
    await bot.db.execute(SCHEMA[3], idempotent=True)


async def reset_ranking_table(bot: "AlterBot") -> None:
    """Start a new season: remove every ranking entry (the table itself stays)."""
    await bot.db.execute(SCHEMA[3], idempotent=True)
    await bot.db.execute("DELETE FROM RANKING", idempotent=True)
    log.info("Ranking table emptied.")


# -- boss schedule ----------------------------------------------------------------

async def update_boss_table(bot: "AlterBot", rarity: int, respawn_seconds: float, resume: bool) -> None:
    """Persist the boss schedule: rarity and "arrives ``respawn_seconds`` from now"."""
    respawn_at = clock.utcnow() + timedelta(seconds=int(respawn_seconds))
    stamp = clock.to_legacy(respawn_at).replace(microsecond=0)
    status = await bot.db.execute(
        "UPDATE BOSS SET RARITY = $1, RESPAWN_TIME = $2, RESPAWN_STARTED = $3 WHERE ID = 0",
        int(rarity), stamp, bool(resume), idempotent=True,
    )
    if status.endswith(" 0"):
        await bot.db.execute(
            "INSERT INTO BOSS (ID, RARITY, RESPAWN_TIME, RESPAWN_STARTED) VALUES (0, $1, $2, $3)",
            int(rarity), stamp, bool(resume),
        )


async def read_boss_table(bot: "AlterBot") -> BossRow | None:
    """The persisted boss schedule, or None when the table is empty."""
    row = await bot.db.fetchrow(
        "SELECT RARITY, RESPAWN_TIME, RESPAWN_STARTED FROM BOSS ORDER BY ID LIMIT 1")
    if row is None:
        return None
    respawn_at = clock.from_legacy(row["respawn_time"]) if row["respawn_time"] is not None else None
    return BossRow(
        rarity=int(row["rarity"] or 0),
        respawn_at=respawn_at,
        respawn_started=bool(row["respawn_started"]),
    )


# -- record -----------------------------------------------------------------------

def format_turn_time(turn_time: timedelta) -> str:
    """``timedelta`` -> ``H:MM:SS.ffffff`` (always with microseconds, unlike ``str``)."""
    total = max(turn_time.total_seconds(), 0.0)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{seconds:09.6f}"


def parse_turn_time(text: str) -> timedelta | None:
    """Parse a stored record (``0:00:01.234567`` / ``00:00:20.000`` / ``0:00:02``)."""
    try:
        hours, minutes, seconds = str(text).strip().split(":")
        return timedelta(hours=int(hours), minutes=int(minutes), seconds=float(seconds))
    except (ValueError, AttributeError):
        return None


def is_new_record(previous: str, turn_time: timedelta) -> bool:
    """True when ``turn_time`` beats the stored record (unreadable record = beaten)."""
    stored = parse_turn_time(previous)
    return stored is None or turn_time < stored


def display_record(record_time: str) -> str:
    """Record as shown by ``$rekord``: seconds only, e.g. ``15.121325``."""
    parsed = parse_turn_time(record_time)
    if parsed is None:
        return str(record_time)
    shown = str(record_time).strip().lstrip("0:")
    return shown if shown and not shown.startswith(".") else f"0{shown}"


async def update_record_table(bot: "AlterBot", nick: str, record_time: timedelta | str) -> None:
    text = format_turn_time(record_time) if isinstance(record_time, timedelta) else str(record_time).strip()
    status = await bot.db.execute(
        "UPDATE RECORD SET RECORD_TIME = $1, NICK = $2 WHERE ID = 0", text, str(nick), idempotent=True)
    if status.endswith(" 0"):
        await bot.db.execute("INSERT INTO RECORD (ID, RECORD_TIME, NICK) VALUES (0, $1, $2)", text, str(nick))


async def read_record_table(bot: "AlterBot") -> tuple[str, str]:
    """``(record_time, nick)``; defaults when the table is empty."""
    row = await bot.db.fetchrow("SELECT RECORD_TIME, NICK FROM RECORD ORDER BY ID LIMIT 1")
    if row is None:
        return DEFAULT_RECORD, DEFAULT_NICK
    return str(row["record_time"]), str(row["nick"])


# -- history ----------------------------------------------------------------------

async def update_history_table(bot: "AlterBot", nick: str, fight_time: datetime | str | None = None) -> None:
    """Remember who fought the boss last and when (Polish local time)."""
    if fight_time is None:
        fight_time = clock.now()
    if isinstance(fight_time, datetime):
        if fight_time.tzinfo is not None:
            fight_time = fight_time.astimezone(clock.WARSAW).replace(tzinfo=None)
        text = fight_time.isoformat(sep=" ", timespec="microseconds")
    else:
        text = str(fight_time)
    status = await bot.db.execute(
        "UPDATE HISTORY SET FIGHT_TIME = $1, NICK = $2 WHERE ID = 0", text, str(nick), idempotent=True)
    if status.endswith(" 0"):
        await bot.db.execute("INSERT INTO HISTORY (ID, FIGHT_TIME, NICK) VALUES (0, $1, $2)", text, str(nick))


async def read_history_table(bot: "AlterBot") -> tuple[str, str]:
    """``(fight_time, nick)``; defaults when the table is empty."""
    row = await bot.db.fetchrow("SELECT FIGHT_TIME, NICK FROM HISTORY ORDER BY ID LIMIT 1")
    if row is None:
        return DEFAULT_FIGHT_TIME, DEFAULT_NICK
    return str(row["fight_time"]), str(row["nick"])


# -- ranking ----------------------------------------------------------------------

def ranking_points(boss_rarity: int, modifier: int = 0) -> int:
    return POINTS_BY_RARITY.get(int(boss_rarity), POINTS_FALLBACK) + int(modifier)


async def update_ranking_table(bot: "AlterBot", user_id: int | str, boss_rarity: int, modifier: int = 0) -> int:
    """Add points for a boss of ``boss_rarity`` (+ ``modifier``); returns the new total."""
    points = ranking_points(boss_rarity, modifier)
    member = await bot.member(user_id)
    nick = member.name if member is not None else str(user_id)
    total = await bot.db.fetchval(
        """INSERT INTO RANKING (ID, NICK, POINTS) VALUES ($1, $2, $3)
           ON CONFLICT (ID) DO UPDATE SET POINTS = RANKING.POINTS + EXCLUDED.POINTS
           RETURNING POINTS""",
        str(user_id), nick, points,
        idempotent=False,  # additive: repeating it after a lost reply would add the points twice
    )
    log.info("Ranking: %s (%s) %+d -> %s", nick, user_id, points, total)
    return int(total)


async def _ranking_lines(bot: "AlterBot", separator: str) -> tuple[str, list]:
    rows = await bot.db.fetch("SELECT ID, NICK, POINTS FROM RANKING ORDER BY POINTS DESC LIMIT 10")
    lines = []
    place = 1
    for row in rows:
        member = await bot.member(row["id"])
        if member is None:
            continue  # left the server - not shown, as before
        lines.append(f"{place}. **{member.name}**{separator}{int(row['points'])} pkt.")
        place += 1
    return "\n".join(lines) + ("\n" if lines else ""), rows


#: Last line of ``$ranking``; ``{when}`` is :func:`altermmo.core.ui.when` of the season end.
MSG_SEASON_ENDS = "\n⏳ Sezon kończy się {when}."


async def read_ranking_table(scene: "Scene") -> None:
    """``$ranking``: post the current top 10 and when the season ends (Discord's live countdown)."""
    from ..core import ui
    from .daily import next_season_end  # (daily imports this module)

    description, _ = await _ranking_lines(scene.bot, " - ")
    description += MSG_SEASON_ENDS.format(when=ui.when(next_season_end()))
    embed = discord.Embed(title="Ranking łowców potworów!", url=RANKING_IMAGE,
                          description=description, color=0xFF0000)
    embed.set_thumbnail(url=RANKING_IMAGE)
    embed.set_footer(text="Sezon kończy się w każdy poniedziałek o 15!")
    await scene.send(embed=embed)


async def read_summary_ranking_table(scene: "Scene") -> int:
    """End of season: post the final top 10 and return the winner's id.

    When the ranking is empty the owner is the default boss slayer (legacy rule).
    """
    description, rows = await _ranking_lines(scene.bot, " ")
    embed = discord.Embed(title="Ranking łowców potworów na koniec sezonu!", url=SUMMARY_IMAGE,
                          description=description, color=0xFF0000)
    embed.set_thumbnail(url=SUMMARY_IMAGE)
    embed.set_footer(text="Ranga Rzeźnika Potworów została przydzielona!")
    await io.send(scene.channel, embed=embed)
    try:
        return int(rows[0]["id"])
    except (IndexError, ValueError, TypeError):
        log.warning("Unable to select the boss slayer from the ranking; using the owner.")
        return scene.bot.ids.owner
