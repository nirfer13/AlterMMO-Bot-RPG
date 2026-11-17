"""Shared pytest fixtures.

* ``game``  - a :class:`tests.fakes.Game`: the real bot class wired to fake
  Discord objects, with file state in a temp directory.
* ``db``    - a real PostgreSQL test database with the bot's schema, emptied
  before every test.  Skipped when no test server is reachable.  The server is
  taken from ``ALTERMMO_TEST_DATABASE_URL`` (default: local port 55432).
  NEVER point this at a production database: every table is dropped.
* ``game_db`` - ``game`` whose ``bot.db`` is the test database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from altermmo.core.db import Database  # noqa: E402

import fakes  # noqa: E402

TEST_DATABASE_URL = os.environ.get(
    "ALTERMMO_TEST_DATABASE_URL", "postgresql://tester@127.0.0.1:55432/altermmo_test"
)


@pytest.fixture(autouse=True)
def _fast_rounds(monkeypatch):
    """Keep simulated gameplay fast.

    * no dramatic pauses (``timing.pause`` returns immediately),
    * a short post-timeout listening window in fight rounds.

    Waiting times (``timing.timeout``) stay real; a test that exercises a
    "nobody answered" path shrinks them with the ``short_timeouts`` fixture.
    """
    from altermmo.core import challenge, timing

    monkeypatch.setattr(challenge, "MIN_GRACE", 0.05)
    monkeypatch.setattr(timing, "PAUSE_SCALE", 0.0)


@pytest.fixture
def short_timeouts(monkeypatch):
    """Scale every ``timing.timeout(...)`` down 1000x (600 s -> 0.6 s)."""
    from altermmo.core import timing

    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.001)


@pytest_asyncio.fixture
async def game(tmp_path):
    bundle = await fakes.make_game(tmp_path)
    try:
        yield bundle
    finally:
        await bundle.cleanup()
        await bundle.bot.supervisor.stop_all()


async def _ensure_test_database() -> bool:
    """Create the throw-away database when the test server is up."""
    import asyncpg

    base, _, name = TEST_DATABASE_URL.rpartition("/")
    if "test" not in name:
        raise RuntimeError(f"Refusing to run tests against database {name!r} (name must contain 'test').")
    try:
        admin = await asyncpg.connect(f"{base}/postgres", timeout=3)
    except Exception:  # noqa: BLE001
        return False
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return True


@pytest_asyncio.fixture
async def db():
    if not await _ensure_test_database():
        pytest.skip("PostgreSQL test server is not reachable (see tests/conftest.py).")
    database = Database(TEST_DATABASE_URL, max_size=3)
    await database.connect()
    try:
        await database.execute("DROP SCHEMA public CASCADE", idempotent=True)
        await database.execute("CREATE SCHEMA public", idempotent=True)
        try:
            from altermmo import schema
        except ImportError:
            schema = None
        if schema is not None:
            await schema.create_all(database)
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def game_db(game, db):
    game.bot.db = db
    return game
