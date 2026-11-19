"""PostgreSQL access layer.

Why this wrapper exists (all of these were real failure modes of the legacy bot):

* the pool was created once at start-up; if the database was unreachable at
  that moment ``bot.pg_con`` never existed and every later call crashed;
* queries had no timeout, so a half-open TCP connection after an internet drop
  made ``await`` hang forever in the middle of a fight;
* SQL was built with ``str.format`` - a nickname containing an apostrophe broke
  the statement (and anything typed by a user was an SQL injection vector).

Every query here is parametrised (``$1, $2 ...``), has a timeout, runs on a
connection that was verified to be alive, and is retried when it is safe to do so.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, AsyncIterator, Sequence

import asyncpg

log = logging.getLogger(__name__)


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached (or a write could not be confirmed)."""


#: Errors that mean "the connection is broken", as opposed to "the SQL is wrong".
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresConnectionError,
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.AdminShutdownError,
    asyncpg.exceptions.CrashShutdownError,
    asyncio.TimeoutError,
    TimeoutError,
    OSError,
)


class Database:
    """Small, resilient facade over an :class:`asyncpg.Pool`."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        command_timeout: float = 20.0,
        acquire_timeout: float = 10.0,
        ping_after: float = 20.0,
        attempts: int = 3,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._acquire_timeout = acquire_timeout
        self._ping_after = ping_after
        self._attempts = attempts
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._last_ok = 0.0

    # -- lifecycle --------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        """Create the pool; raises when the database is unreachable."""
        async with self._pool_lock:
            if self._pool is not None:
                return
            if not self._dsn:
                raise DatabaseUnavailable("No database URL configured (DATABASE_URL).")
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                command_timeout=self._command_timeout,
                timeout=self._acquire_timeout,
                # Drop idle connections quickly: after an internet outage the old
                # sockets are half-open and would swallow queries until a timeout.
                max_inactive_connection_lifetime=60.0,
                server_settings={"application_name": "altermmo-bot"},
            )
            self._last_ok = time.monotonic()
            log.info("Connected to database. Pool created.")

    async def connect_with_retry(self, *, max_delay: float = 60.0) -> None:
        """Keep trying to create the pool until it works (used at start-up)."""
        delay = 2.0
        while True:
            try:
                await self.connect()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("Database unreachable (%s: %s); retrying in %.0fs.",
                          type(exc).__name__, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    async def close(self) -> None:
        async with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pool.close(), timeout=10)

    # -- public query API -------------------------------------------------------

    async def fetch(self, query: str, *args: Any, timeout: float | None = None) -> list[asyncpg.Record]:
        """Run a SELECT and return all rows.  Safe to retry, so it is retried."""
        return await self._call("fetch", query, args, timeout, idempotent=True)

    async def fetchrow(self, query: str, *args: Any, timeout: float | None = None) -> asyncpg.Record | None:
        """Run a SELECT and return the first row (or None)."""
        return await self._call("fetchrow", query, args, timeout, idempotent=True)

    async def fetchval(self, query: str, *args: Any, timeout: float | None = None,
                       idempotent: bool = True) -> Any:
        """Run a SELECT and return the first column of the first row (or None).

        Pass ``idempotent=False`` for a writing statement with ``RETURNING``
        (e.g. ``SET x = x + 1 RETURNING x``): it is then retried only when it
        provably never reached the server, like :meth:`execute`.
        """
        return await self._call("fetchval", query, args, timeout, idempotent=idempotent)

    async def execute(self, query: str, *args: Any, timeout: float | None = None,
                      idempotent: bool = False) -> str:
        """Run a data-modifying statement; returns the command tag (e.g. ``UPDATE 1``).

        A write is only retried when it provably never reached the server
        (the connection could not be obtained).  Pass ``idempotent=True`` for
        statements that are harmless to repeat (``SET x = 5``, ``DELETE WHERE``,
        ``CREATE TABLE IF NOT EXISTS``) to have them retried on any connection error.
        """
        return await self._call("execute", query, args, timeout, idempotent=idempotent)

    async def executemany(self, query: str, args_list: Sequence[Sequence[Any]], *,
                          timeout: float | None = None) -> None:
        """Run one statement for many argument tuples inside a single transaction."""
        async with self.transaction() as con:
            await con.executemany(query, args_list, timeout=timeout or self._command_timeout)

    @contextlib.asynccontextmanager
    async def transaction(self, *, readonly: bool = False,
                          isolation: str | None = None) -> AsyncIterator[asyncpg.Connection]:
        """``async with db.transaction() as con:`` - several statements, all or nothing.

        Use ``con.fetch / con.fetchrow / con.fetchval / con.execute`` inside.
        Nothing is retried automatically; a connection failure raises
        :class:`DatabaseUnavailable` and the transaction is rolled back.

        ``readonly=True``: PostgreSQL refuses every change inside the
        transaction (used by the backup).  ``isolation="repeatable_read"``:
        all statements see the database as of one moment.
        """
        con = await self._acquire_with_retry()
        broken = False
        cancelled = False
        try:
            async with con.transaction(readonly=readonly, isolation=isolation):
                try:
                    yield con
                except asyncio.CancelledError:
                    cancelled = True
                    if readonly:
                        # Nothing to roll back - and the connection may be in the middle of a
                        # COPY, where the ROLLBACK would wait for the whole command timeout
                        # (a shutdown would hang that long).  Dropping the connection ends
                        # the transaction on the server at once.
                        con.terminate()
                    raise
            self._last_ok = time.monotonic()
        except Exception as exc:
            if cancelled:
                # The block was cancelled and the ROLLBACK failed on top of it (asyncpg cannot
                # roll back in the middle of a COPY).  The cancellation is what happened: a
                # swallowed one would leave the cancelled task running.  The connection goes.
                broken = True
                raise asyncio.CancelledError() from None
            if isinstance(exc, _CONNECTION_ERRORS):
                broken = True
                raise DatabaseUnavailable(f"Database connection failed: {exc!r}") from exc
            broken = con.is_closed()
            raise
        finally:
            await self._release(con, broken)

    async def ping(self) -> bool:
        """True when a trivial query succeeds."""
        try:
            return await self.fetchval("SELECT 1", timeout=5) == 1
        except Exception:  # noqa: BLE001
            return False

    # -- internals --------------------------------------------------------------

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            try:
                await self.connect()
            except _CONNECTION_ERRORS as exc:
                raise DatabaseUnavailable(f"Cannot connect to database: {exc!r}") from exc
        assert self._pool is not None
        return self._pool

    async def _acquire(self) -> asyncpg.Connection:
        """Get a connection that is known to be alive."""
        pool = await self._ensure_pool()
        con = await pool.acquire(timeout=self._acquire_timeout)
        if time.monotonic() - self._last_ok > self._ping_after:
            try:
                await con.execute("SELECT 1", timeout=5)
                self._last_ok = time.monotonic()
            except BaseException:
                await self._release(con, broken=True)
                raise
        return con

    async def _acquire_with_retry(self) -> asyncpg.Connection:
        last: BaseException | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._acquire()
            except DatabaseUnavailable as exc:
                last = exc
            except _CONNECTION_ERRORS as exc:
                last = exc
            log.warning("Database connection attempt %d/%d failed: %r", attempt, self._attempts, last)
            if attempt < self._attempts:
                await asyncio.sleep(min(0.5 * 2 ** (attempt - 1), 4.0))
        raise DatabaseUnavailable(f"Database unreachable after {self._attempts} attempts: {last!r}") from last

    async def _release(self, con: asyncpg.Connection, broken: bool) -> None:
        pool = self._pool
        try:
            if broken and not con.is_closed():
                con.terminate()
            if pool is not None:
                await asyncio.shield(pool.release(con, timeout=5))
        except Exception:  # noqa: BLE001 - releasing must never mask the real error
            log.debug("Ignoring error while releasing a connection.", exc_info=True)

    async def _call(self, method: str, query: str, args: Sequence[Any],
                    timeout: float | None, *, idempotent: bool) -> Any:
        last: BaseException | None = None
        for attempt in range(1, self._attempts + 1):
            con = await self._acquire_with_retry()
            broken = False
            try:
                result = await getattr(con, method)(
                    query, *args, timeout=timeout or self._command_timeout
                )
                self._last_ok = time.monotonic()
                return result
            except _CONNECTION_ERRORS as exc:
                broken = True
                last = exc
                if not idempotent:
                    raise DatabaseUnavailable(
                        f"Database connection failed during a write: {exc!r}"
                    ) from exc
                log.warning("Query failed on a broken connection (attempt %d/%d): %r",
                            attempt, self._attempts, exc)
            except Exception:
                broken = con.is_closed()
                raise
            finally:
                await self._release(con, broken)
            if attempt < self._attempts:
                await asyncio.sleep(min(0.5 * 2 ** (attempt - 1), 4.0))
        raise DatabaseUnavailable(f"Query failed after {self._attempts} attempts: {last!r}") from last
