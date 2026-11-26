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
    from altermmo.core import challenge, fightlog, guards, timing, ui, waiting

    monkeypatch.setattr(waiting, "NOTICE_DELAY", 24 * 3600.0)
    waiting._notices.clear()
    # The fight journal starts empty: no fight, no late-answer watch of an earlier test.
    fightlog.reset()

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


@pytest.fixture(autouse=True)
def _ordinary_bosses(monkeypatch, tmp_path_factory):
    """No special boss variants unless a test asks for them (features/bossvariants.py).

    The shipped ``config/bossVariants.json`` makes a boss special now and then
    - by chance, which would change the picture and the embed of tests that
    know nothing about it.  Every test starts with a config whose chance is 0
    (nothing is rolled, exactly as before the variants existed); a test of the
    variants points ``bossvariants.CONFIG_DIR`` at its own file
    (``special_bosses`` below) - at the real one only to check the shape of
    that file, never its numbers: those are the owner's to tune.
    """
    from altermmo.features import bossvariants

    config_dir = tmp_path_factory.getbasetemp() / "no-special-bosses"
    if not config_dir.is_dir():
        config_dir.mkdir()
        (config_dir / bossvariants.CONFIG_FILE).write_text('{"chance_percent": 0}', encoding="utf-8")
    monkeypatch.setattr(bossvariants, "CONFIG_DIR", config_dir)


@pytest.fixture(autouse=True)
def _no_auras(monkeypatch, tmp_path_factory):
    """No pet hatches with an aura unless a test asks for one (features/auras.py).

    The shipped ``config/auraConfig.json`` will give an aura now and then once
    the owner turns the chance on - by chance, which would change the notes,
    the stats and the random draws of every test that hatches an egg.  Every
    test starts with a config whose chance is 0 (no draw is made at all); a
    test of the auras writes its own (``aura_config`` below).
    """
    from altermmo.features import auras

    config_dir = tmp_path_factory.getbasetemp() / "no-auras"
    if not config_dir.is_dir():
        config_dir.mkdir()
        (config_dir / auras.CONFIG_FILE).write_text('{"chance_per_10000": 0}', encoding="utf-8")
    monkeypatch.setattr(auras, "CONFIG_DIR", config_dir)


@pytest.fixture
def aura_config(monkeypatch, tmp_path):
    """``aura_config(chance_per_10000=..., kinds=...)`` writes an ``auraConfig.json`` of the test's own.

    Points ``auras.CONFIG_DIR`` at a folder of the test (``kinds`` left out:
    the file has none, so the owner's weights apply) and returns the loaded
    config - the numbers a test relies on are its own, never the shipped file's.
    """
    import json

    from altermmo.features import auras

    folder = tmp_path / "auras"
    folder.mkdir()
    monkeypatch.setattr(auras, "CONFIG_DIR", folder)

    def write(chance_per_10000=10000, **changes):
        (folder / auras.CONFIG_FILE).write_text(
            json.dumps({"chance_per_10000": chance_per_10000, **changes}, ensure_ascii=False), encoding="utf-8")
        return auras.load()

    return write


@pytest.fixture(autouse=True)
def _no_dust_without_a_database(monkeypatch):
    """A bot without a database (the ``game`` fixture) carries no Nieuchwytny pył into a fight.

    Every typed fight reads its fighters' dust once before the countdown
    (``items.reserve_dust``, features/items.py).  Against the ``game``
    fixture's database, which has no address, that read would sit out the
    database layer's retries in every fight - and nobody can hold a dust there
    anyway, so the guard is empty at once.  With a real database (``game_db``)
    the dust is read for real.
    """
    from altermmo.features import items

    real = items.reserve_dust

    async def reserve_dust(bot, fighters):
        if not getattr(getattr(bot, "db", None), "_dsn", ""):
            return items.DustGuard()
        return await real(bot, fighters)

    monkeypatch.setattr(items, "reserve_dust", reserve_dust)


#: The ``bossVariants.json`` of ``special_bosses``: every boss is special, one variant ("mgla"),
#: the first picture of every rarity has it, repeats 1..5 for rarity 0..4.
SPECIAL_BOSSES = {
    "chance_percent": 100,
    "cosmetic_rerolls": {"0": 1, "1": 2, "2": 3, "3": 4, "4": 5},
    "variants": {"mgla": {"name": "Mgła", "line": "🌫️ Tego bossa spowija **Mgła**."}},
    "bosses": {"0": "mgla", "10": "mgla", "100": "mgla", "1000": "mgla", "10000": "mgla"},
}


@pytest.fixture
def special_bosses(monkeypatch, tmp_path):
    """``special_bosses(**changes)`` switches the special bosses on with a config of the test's own.

    Writes :data:`SPECIAL_BOSSES` (with ``changes`` replacing whole keys) into a
    folder of the test, points ``bossvariants.CONFIG_DIR`` there and returns
    the loaded config.  The numbers a test relies on are therefore its own: a
    retune of the shipped ``config/bossVariants.json`` (the chance set to 0,
    fewer repeats) must never turn a behaviour test red.
    """
    import json

    from altermmo.features import bossvariants

    folder = tmp_path / "special-bosses"
    folder.mkdir()
    monkeypatch.setattr(bossvariants, "CONFIG_DIR", folder)

    def write(**changes):
        (folder / bossvariants.CONFIG_FILE).write_text(
            json.dumps({**SPECIAL_BOSSES, **changes}, ensure_ascii=False), encoding="utf-8")
        return bossvariants.load()

    return write


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
        from altermmo.core import fightlog

        await fightlog.stop()  # (as AlterBot.close does: watches and pending answers to "lag")


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
