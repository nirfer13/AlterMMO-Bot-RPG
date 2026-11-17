"""Database wrapper against a real PostgreSQL test server."""

from __future__ import annotations

import asyncio

import pytest

from altermmo.core.db import Database, DatabaseUnavailable


async def test_parametrised_queries_survive_apostrophes(db):
    await db.execute("CREATE TABLE t_names (id VARCHAR(255) PRIMARY KEY, nick VARCHAR(255), points NUMERIC)")
    await db.execute("INSERT INTO t_names (id, nick, points) VALUES ($1, $2, $3)", "1", "O'Neil'); DROP TABLE t_names;--", 3)
    row = await db.fetchrow("SELECT nick, points FROM t_names WHERE id = $1", "1")
    assert row["nick"] == "O'Neil'); DROP TABLE t_names;--"
    assert int(row["points"]) == 3
    assert await db.fetchval("SELECT count(*) FROM t_names") == 1


async def test_reads_recover_after_connections_are_killed(db):
    assert await db.fetchval("SELECT 1") == 1
    # Simulate what an internet outage does: every pooled connection dies.
    await db.fetch("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                   "WHERE application_name = 'altermmo-bot' AND pid <> pg_backend_pid()")
    try:
        await db.fetchval("SELECT pg_terminate_backend(pg_backend_pid())")
    except Exception:  # noqa: BLE001 - killing our own backend may or may not surface an error
        pass
    assert await db.fetchval("SELECT 41 + 1") == 42
    assert await db.ping()


async def test_writes_work_after_connections_are_killed(db):
    await db.execute("CREATE TABLE t_counter (n INT)")
    await db.execute("INSERT INTO t_counter VALUES (0)")
    db._last_ok = 0.0  # force the liveness check, as after a long idle period
    await db.fetch("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                   "WHERE application_name = 'altermmo-bot' AND pid <> pg_backend_pid()")
    db._last_ok = 0.0
    await db.execute("UPDATE t_counter SET n = n + 1")
    assert await db.fetchval("SELECT n FROM t_counter") == 1


async def test_transaction_rolls_back_on_error(db):
    await db.execute("CREATE TABLE t_tx (n INT)")
    with pytest.raises(ZeroDivisionError):
        async with db.transaction() as con:
            await con.execute("INSERT INTO t_tx VALUES (1)")
            raise ZeroDivisionError
    assert await db.fetchval("SELECT count(*) FROM t_tx") == 0
    async with db.transaction() as con:
        await con.execute("INSERT INTO t_tx VALUES (2)")
    assert await db.fetchval("SELECT count(*) FROM t_tx") == 1


async def test_sql_errors_are_not_masked_as_outages(db):
    import asyncpg

    with pytest.raises(asyncpg.PostgresError):
        await db.fetch("SELECT * FROM table_that_does_not_exist")
    assert await db.fetchval("SELECT 1") == 1


async def test_unreachable_server_raises_database_unavailable_quickly():
    database = Database("postgresql://tester@127.0.0.1:1/nope", attempts=2, acquire_timeout=1)
    started = asyncio.get_running_loop().time()
    with pytest.raises(DatabaseUnavailable):
        await database.fetchval("SELECT 1")
    assert asyncio.get_running_loop().time() - started < 15
    await database.close()


async def test_query_timeout_does_not_hang_forever(db):
    with pytest.raises(DatabaseUnavailable):
        await db.fetchval("SELECT pg_sleep(5)", timeout=0.3)
    assert await db.fetchval("SELECT 1") == 1
