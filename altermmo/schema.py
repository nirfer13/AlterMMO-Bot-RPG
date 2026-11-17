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
