"""Database schema bootstrap.

Every feature module that owns tables exposes ``SCHEMA``: a tuple of
``CREATE TABLE IF NOT EXISTS ...`` statements.  :func:`create_all` runs them
all; it never drops or alters anything, so it is safe on a live database.
"""

from __future__ import annotations

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
    "altermmo.features.tower",
    "altermmo.features.twitch",
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


#: Structure of the production database (read from its information_schema on
#: 2026-11-17).  Used by :func:`verify` to warn at start-up when the live
#: database does not look like the code expects, instead of failing in the
#: middle of a fight.  Tables owned by the Twitch bot are listed as well.
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "boss": {"id": "numeric", "rarity": "numeric", "respawn_time": "timestamp without time zone", "respawn_started": "boolean"},
    "record": {"id": "numeric", "record_time": "character varying", "nick": "character varying"},
    "history": {"id": "numeric", "fight_time": "character varying", "nick": "character varying"},
    "ranking": {"id": "character varying", "nick": "character varying", "points": "numeric"},
    "petowner": {"player_id": "numeric", "pet_id": "numeric", "pet_owned": "boolean", "reroll_scroll": "numeric", "reroll_scroll_shard": "numeric", "rebirth_stones": "numeric", "mirrors": "numeric", "pet_id_alt1": "numeric", "pet_id_alt2": "numeric", "skill_gem": "numeric", "pet_id_alt3": "numeric", "pet_id_alt4": "numeric"},
    "pets": {"pet_id": "numeric", "pet_name": "character varying", "pet_lvl": "numeric", "pet_skills": "numeric", "quality": "character varying", "shiny": "boolean", "type": "character varying", "variant": "character varying", "crit_perc": "numeric", "replace_perc": "numeric", "def_perc": "numeric", "drop_perc": "numeric", "lowhp_perc": "numeric", "slow_perc": "numeric", "init_perc": "numeric", "detection": "boolean", "ultra_shiny": "boolean", "mythic": "boolean"},
    "skills": {"skill_id": "numeric", "gem_name": "character varying", "skill_variant": "numeric", "slow_perc": "numeric"},
    "skillpouch": {"player_id": "numeric", "skill_id": "numeric"},  # new in v2, see create_new_tables
    "achievements": {"player_id": "numeric", "boss_kills": "numeric", "hunts": "numeric", "butchers": "numeric"},
    "tower": {"player_id": "numeric", "tower_level": "numeric", "time": "character varying"},
    "chatters_information": {"chtr_id": "integer", "chtr_name": "text", "chtr_time": "integer", "chtr_comments": "integer", "chtr_isvip": "boolean", "chtr_issub": "boolean", "chtr_ismod": "boolean", "chtr_isbroadcaster": "boolean", "discord_id": "text", "watchstreak": "integer"},
    "treasure_table": {"chtr_id": "integer", "chtr_name": "text", "windatetime": "text"},
}


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
                problems.append(f"{table}: brak kolumny {column}")
            elif found != expected_type:
                problems.append(f"{table}.{column}: typ {found} zamiast {expected_type}")
    return problems
