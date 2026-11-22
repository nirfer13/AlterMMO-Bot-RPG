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

from altermmo import bot as _bot_module  # noqa: E402
from altermmo.core.db import Database  # noqa: E402

import fakes  # noqa: E402

# Like the real bot (``AlterBot.setup_hook``): every module is imported before anything
# runs.  Several modules import each other lazily, and some tests put a stand-in for a
# whole module into ``sys.modules`` - a module first imported in such a test would be
# built on the stand-in and fail (or, worse, pass) depending on which tests ran before.
_not_imported = _bot_module.import_all_modules()
assert not _not_imported, f"modules that cannot be imported: {_not_imported}"

#: Every pytest process gets its own database (``<name>_<pid>``), dropped when
#: the session ends: the ``db`` fixture recreates the whole schema before each
#: test, so two runs sharing one database would wipe each other's tables.
TEST_DATABASE_URL = os.environ.get(
    "ALTERMMO_TEST_DATABASE_URL", "postgresql://tester@127.0.0.1:55432/altermmo_test"
) + f"_{os.getpid()}"

#: Set ALTERMMO_REQUIRE_TEST_DB=1 to fail (instead of skip) the database tests
#: when the test server does not answer, so a green run cannot hide them.
REQUIRE_TEST_DB = os.environ.get("ALTERMMO_REQUIRE_TEST_DB", "") not in ("", "0")

_database_created = False


@pytest.fixture(autouse=True)
def _fast_rounds(monkeypatch):
    """Keep simulated gameplay fast.

    * no dramatic pauses (``timing.pause`` returns immediately),
    * a short post-timeout listening window in fight rounds,
    * no "Nadchodzi towarzysz..." notice (core/waiting.py) while a card is drawn,
      however slow the machine: its delay is out of reach unless a test sets it.

    Waiting times (``timing.timeout``) stay real; a test that exercises a
    "nobody answered" path shrinks them with the ``short_timeouts`` fixture.
    """
    from altermmo.core import challenge, guards, timing, ui, waiting

    monkeypatch.setattr(waiting, "NOTICE_DELAY", 24 * 3600.0)
    waiting._notices.clear()

    monkeypatch.setattr(challenge, "MIN_GRACE", 0.05)
    # Every click is answered in tests, unless a test switches these filters of ui.reply on
    # to test them:
    # * the "same notice moments ago" filter (NOTICE_GAP) is off;
    # * a fight counts as "on" only while one of its rounds is running (no lingering
    #   15 s after the last round, during which notices are swallowed);
    # * the busy notice limiter (one per 20 s per channel, shared with the typed
    #   commands) starts empty - a second busy notice within one test is still swallowed.
    monkeypatch.setattr(ui, "NOTICE_GAP", 0.0)
    ui._recent_notices.clear()
    monkeypatch.setattr(challenge, "FIGHT_LINGER", 0.0)
    challenge._rounds_running.clear()
    challenge._fight_seen.clear()
    guards._last_notice.clear()
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
    global _database_created
    if _database_created:
        return True
    admin = None
    for _attempt in range(3):  # a busy server may need a moment
        try:
            admin = await asyncpg.connect(f"{base}/postgres", timeout=10)
            break
        except Exception:  # noqa: BLE001
            admin = None
    if admin is None:
        return False
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    _database_created = True
    return True


def pytest_sessionfinish(session, exitstatus):
    """Drop this process's throw-away database."""
    if not _database_created:
        return
    import asyncio

    import asyncpg

    base, _, name = TEST_DATABASE_URL.rpartition("/")

    async def drop() -> None:
        admin = await asyncpg.connect(f"{base}/postgres", timeout=10)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()

    try:
        asyncio.run(drop())
    except Exception as exc:  # noqa: BLE001 - leaving a test database behind is harmless
        print(f"\n(could not drop test database {name}: {exc})")


@pytest_asyncio.fixture
async def db():
    if not await _ensure_test_database():
        if REQUIRE_TEST_DB:
            pytest.fail("PostgreSQL test server is not reachable (see tests/conftest.py).")
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
