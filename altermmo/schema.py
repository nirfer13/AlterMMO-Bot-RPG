"""Database schema bootstrap.

Every feature module that owns tables exposes ``SCHEMA``: a tuple of
``CREATE TABLE IF NOT EXISTS ...`` statements.  :func:`create_all` runs them
all; it never drops or alters anything, so it is safe on a live database.

Two steps run at every start-up, because the bot took over the old bot's
database, which lacks what is new in v2:

* :func:`create_new_tables` creates the missing new tables (``NEW_TABLES`` of
  the feature modules) - ``CREATE TABLE IF NOT EXISTS`` only;
* :func:`add_new_columns` adds the missing new columns (``NEW_COLUMNS``) to
  tables that exist.  This is the ONE place where the bot alters a table: only
  ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` with a default, never a change
  or a removal of anything that is there - and it gives up quickly instead of
  waiting for a busy table.

Nothing here ever drops a table, a column or a row.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

from .core.db import Database

log = logging.getLogger(__name__)

#: Feature modules that may define ``SCHEMA``.
SCHEMA_MODULES: tuple[str, ...] = (
    "altermmo.features.database",
    "altermmo.features.pets",
    "altermmo.features.skills",
    "altermmo.features.achievements",
    "altermmo.features.album",
    "altermmo.features.tower",
    "altermmo.features.twitch",
    "altermmo.features.missions",
    "altermmo.features.items",
)


def all_statements() -> list[str]:
    statements: list[str] = []
    for module_name in SCHEMA_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            log.debug("Schema module %s is not available.", module_name)
            continue
        statements.extend(getattr(module, "SCHEMA", ()))
    return statements


async def create_all(db: Database) -> int:
    """Create every missing table; returns the number of statements executed."""
    statements = all_statements()
    for statement in statements:
        await db.execute(statement, idempotent=True)
    return len(statements)


async def create_new_tables(db: Database) -> int:
    """Create the tables that are new in v2 (``NEW_TABLES`` of the feature modules).

    Run at every start-up: the old bot's database does not have them.  Only
    ``CREATE TABLE IF NOT EXISTS`` - nothing existing is touched.
    """
    count = 0
    for module_name in SCHEMA_MODULES:
        module = importlib.import_module(module_name)
        for statement in getattr(module, "NEW_TABLES", ()):
            await db.execute(statement, idempotent=True)
            count += 1
    return count


#: How long an ``ALTER TABLE`` of :func:`add_new_columns` may wait for its lock (PostgreSQL's
#: ``lock_timeout``).  Adding a column needs the table for itself for an instant; somebody's
#: long transaction on it must not keep the bot from starting.
NEW_COLUMN_LOCK_TIMEOUT = "3s"


def new_columns() -> list[tuple[str, str, str]]:
    """``(table, column, definition)`` of every column that is new in v2 (``NEW_COLUMNS``)."""
    columns: list[tuple[str, str, str]] = []
    for module_name in SCHEMA_MODULES:
        module = importlib.import_module(module_name)
        columns.extend((table.lower(), column.lower(), definition)
                       for table, column, definition in getattr(module, "NEW_COLUMNS", ()))
    return columns


async def add_new_columns(db: Database) -> list[str]:
    """Add the columns that are new in v2 to the old bot's tables; returns what could not be done.

    Run at every start-up, like :func:`create_new_tables`.  Unlike everything
    else in this module it ALTERS existing tables - in the one harmless way:
    ``ALTER TABLE <table> ADD COLUMN IF NOT EXISTS <column> <type with a default>``.
    Existing rows get the default, nothing that is there is changed or removed.

    * Columns that are there are not touched at all (a second start sends no
      ``ALTER``), a table that does not exist is left to :func:`verify`.
    * The columns of one table are added in one transaction - all or none -
      which waits at most :data:`NEW_COLUMN_LOCK_TIMEOUT` for the table.
    * Nothing is raised for a column that cannot be added (no privilege, the
      table is busy, the database went away): the reason is returned (and
      logged) and the caller decides.  The bot starts in any case; the feature
      that needs the column has to cope without it.

    An empty list means every new column is there.
    """
    wanted = new_columns()
    if not wanted:
        return []
    try:
        rows = await db.fetch(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = ANY($1::text[])""",
            sorted({table for table, _, _ in wanted}))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        log.error("Could not look for the columns that are new in v2 (%s: %s).", type(exc).__name__, exc)
        return [f"{type(exc).__name__}: {exc}"]
    present = {(row["table_name"], row["column_name"]) for row in rows}
    tables = {table for table, _ in present}

    missing: dict[str, list[tuple[str, str]]] = {}
    for table, column, definition in wanted:
        if table in tables and (table, column) not in present:
            missing.setdefault(table, []).append((column, definition))

    problems: list[str] = []
    for table, columns in missing.items():
        names = ", ".join(column for column, _ in columns)
        try:
            async with db.transaction() as con:
                await con.execute("SELECT set_config('lock_timeout', $1, true)", NEW_COLUMN_LOCK_TIMEOUT)
                for column, definition in columns:
                    # Names and definitions are constants of the feature modules, never user input.
                    await con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            log.error("Could not add the new column(s) %s to table %s (%s: %s).",
                      names, table, type(exc).__name__, exc)
            problems.append(f"{table} ({names}): {type(exc).__name__}: {exc}")
        else:
            log.info("Table %s: new column(s) added: %s.", table, names)
    return problems


#: Structure of the production database (read from its information_schema on
#: 2026-11-17, plus what v2 adds to it).  Used by :func:`verify` to warn at start-up when the live
#: database does not look like the code expects, instead of failing in the
#: middle of a fight.  Tables owned by the Twitch bot are listed as well.
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "boss": {"id": "numeric", "rarity": "numeric", "respawn_time": "timestamp without time zone", "respawn_started": "boolean"},
    "record": {"id": "numeric", "record_time": "character varying", "nick": "character varying"},
    "history": {"id": "numeric", "fight_time": "character varying", "nick": "character varying"},
    "ranking": {"id": "character varying", "nick": "character varying", "points": "numeric"},
    "petowner": {"player_id": "numeric", "pet_id": "numeric", "pet_owned": "boolean", "reroll_scroll": "numeric", "reroll_scroll_shard": "numeric", "rebirth_stones": "numeric", "mirrors": "numeric", "pet_id_alt1": "numeric", "pet_id_alt2": "numeric", "skill_gem": "numeric", "pet_id_alt3": "numeric", "pet_id_alt4": "numeric", **{f"pet_id_alt{place}": "numeric" for place in range(5, 11)}, "stable_places": "numeric", "egg_pouch": "boolean", "pouch_pet_id": "numeric", "big_pouch": "boolean", "pouch_pet_id2": "numeric", "summon_scrolls": "numeric"},  # pet_id_alt5 .. pouch_pet_id: new in v2 (the wandering merchant), big_pouch .. summon_scrolls: the companion missions, see add_new_columns
    "pets": {"pet_id": "numeric", "pet_name": "character varying", "pet_lvl": "numeric", "pet_skills": "numeric", "quality": "character varying", "shiny": "boolean", "type": "character varying", "variant": "character varying", "crit_perc": "numeric", "replace_perc": "numeric", "def_perc": "numeric", "drop_perc": "numeric", "lowhp_perc": "numeric", "slow_perc": "numeric", "init_perc": "numeric", "detection": "boolean", "ultra_shiny": "boolean", "mythic": "boolean", "seal": "numeric", "holo": "boolean", "frame": "character varying", "holo_partial": "boolean", "aura": "character varying", "aura_perc": "numeric", "mission_perc": "numeric"},  # seal .. mission_perc: new in v2, see add_new_columns
    "skills": {"skill_id": "numeric", "gem_name": "character varying", "skill_variant": "numeric", "slow_perc": "numeric"},
    "skillpouch": {"player_id": "numeric", "skill_id": "numeric"},  # new in v2, see create_new_tables
    "achievements": {"player_id": "numeric", "boss_kills": "numeric", "hunts": "numeric", "butchers": "numeric"},
    # new in v2 (features/album.py, see create_new_tables): the album's own columns + the snapshot of a PETS row
    "pet_album": {"entry_id": "bigint", "player_id": "numeric", "added_at": "timestamp with time zone", "pet_id": "numeric", "pet_name": "character varying", "pet_lvl": "numeric", "pet_skills": "numeric", "quality": "character varying", "shiny": "boolean", "type": "character varying", "variant": "character varying", "crit_perc": "numeric", "replace_perc": "numeric", "def_perc": "numeric", "drop_perc": "numeric", "lowhp_perc": "numeric", "slow_perc": "numeric", "init_perc": "numeric", "detection": "boolean", "ultra_shiny": "boolean", "mythic": "boolean", "seal": "numeric", "holo": "boolean", "frame": "character varying", "holo_partial": "boolean", "aura": "character varying", "aura_perc": "numeric", "mission_perc": "numeric"},
    # new in v2 (features/missions.py, see create_new_tables): the companion missions, one row per board
    "pet_missions": {"mission_id": "bigint", "player_id": "numeric", "period_start": "timestamp with time zone",
                     "rolled_at": "timestamp with time zone", "offers": "text", "rerolls": "integer", "status": "text",
                     "chosen": "integer", "pet_id": "numeric", "pet_place": "integer", "pet_snapshot": "text",
                     "chance": "integer", "roll": "integer", "outcome": "text",
                     "sent_at": "timestamp with time zone", "ends_at": "timestamp with time zone",
                     "paid_at": "timestamp with time zone", "result": "text", "announced_at": "timestamp with time zone",
                     "seen_at": "timestamp with time zone"},
    # new in v2 (features/items.py, see create_new_tables): the player items, one row per player and item
    "player_items": {"player_id": "numeric", "item": "character varying", "count": "integer"},
    "tower": {"player_id": "numeric", "tower_level": "numeric", "time": "character varying"},
    "chatters_information": {"chtr_id": "integer", "chtr_name": "text", "chtr_time": "integer", "chtr_comments": "integer", "chtr_isvip": "boolean", "chtr_issub": "boolean", "chtr_ismod": "boolean", "chtr_isbroadcaster": "boolean", "discord_id": "text", "watchstreak": "integer"},
    "treasure_table": {"chtr_id": "integer", "chtr_name": "text", "windatetime": "text"},
}


def missing_column(table: str, column: str) -> str:
    """How :func:`verify` words a column that is not there."""
    return f"{table}: brak kolumny {column}"


async def verify(db: Database) -> list[str]:
    """Compare the live database with :data:`EXPECTED_COLUMNS`.

    Returns human-readable problems (missing tables/columns, changed types);
    an empty list means everything the bot needs is there.  Extra tables and
    columns are fine - the database is shared with other applications.
    """
    rows = await db.fetch(
        """SELECT table_name, column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = ANY($1::text[])""",
        list(EXPECTED_COLUMNS),
    )
    actual: dict[str, dict[str, str]] = {}
    for row in rows:
        actual.setdefault(row["table_name"], {})[row["column_name"]] = row["data_type"]

    problems: list[str] = []
    for table, columns in EXPECTED_COLUMNS.items():
        if table not in actual:
            problems.append(f"brak tabeli {table}")
            continue
        for column, expected_type in columns.items():
            found = actual[table].get(column)
            if found is None:
                problems.append(missing_column(table, column))
            elif found != expected_type:
                problems.append(f"{table}.{column}: typ {found} zamiast {expected_type}")
    return problems
