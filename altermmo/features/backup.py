"""Copies of the database and of the state files ("backup"), on the owner's request.

One copy is one ZIP file in the backup folder (``backups/`` next to ``data/``,
or ``ALTERMMO_BACKUP_DIR``)::

    altermmo-2026-11-19_03-15-00.zip
        manifest.json     when it was made (and whether from the test database of a debug run),
                          rows and columns of every table, the state files
        schema.sql        the CREATE TABLE statements the bot knows (to restore into an empty database)
        db/<table>.csv    every row of the table: CSV with a header, as PostgreSQL's COPY writes it
        data/<file>       the state files (cooldown lists, exp summary, shrine modifiers ...)

The owner's conditions: **absolutely nothing may be deleted while a copy is made**
(nothing in the database, that is) and **the last 14 copies are kept**.

* The database is only read, inside ONE transaction opened as ``READ ONLY``:
  PostgreSQL itself refuses every INSERT / UPDATE / DELETE / TRUNCATE / DROP
  in such a transaction, so not even a bug in this module could change a row.
* Two kinds of statements are sent, nothing else: one SELECT that lists the
  columns, and ``COPY <table> TO STDOUT`` per table.  The table names come
  from the fixed list :data:`TABLES` (the tables the bot works with - the
  database is shared, tables of other applications are not looked at).
* The transaction is REPEATABLE READ: every table is copied as of the same
  moment.  Reading never blocks the game (in PostgreSQL readers and writers do
  not wait for each other), and the files are written on a worker thread.
* The state files are only read - on the event-loop thread, where the bot
  also saves them, so a read can never get in the way of a save.
* A copy is written under a temporary name (``*.part``), checked - ZIP
  checksums, the rows of every table counted again -, flushed to the disk and
  only then given its final name.  An existing copy is never overwritten.
* Only the newest ``keep`` copies stay (owner: 14; ``ALTERMMO_BACKUP_KEEP``,
  0 = all): AFTER a new copy passed its check, the oldest ones beyond that
  number are removed (:func:`prune`).  Only files named exactly as this module
  names them count - a copy the owner put a word in front of (or any other
  file in the folder) is never touched, and neither is the copy just made.
  The only other file ever deleted here is the unfinished ``*.part`` of a
  failed copy.
* Because old copies go away, a copy in which a table is gone, empty or holds
  far fewer rows than in the copy before is reported to the owner with a ping
  (:attr:`Backup.shrunk`): that is the moment to put an older copy aside.
* Restoring is deliberately not a bot command: putting data back means
  overwriting, and that should be a human's decision.

The database address (it holds the password) is never written into a copy.

``python -m altermmo.features.backup`` makes one copy and exits (the bot does
not have to run).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import csv
import functools
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import schema
from ..core import clock

log = logging.getLogger(__name__)

#: Version of the layout described in the module docstring.
FORMAT = 1
PREFIX = "altermmo-"
SUFFIX = ".zip"
PART_SUFFIX = ".part"
MANIFEST = "manifest.json"
SCHEMA_FILE = "schema.sql"
#: The names this module gives its copies: ``altermmo-2026-11-19_03-15-00.zip`` (``..._2.zip``,
#: ``..._3.zip`` for further copies in the same second) - exactly what :func:`_reserve` produces.
#: Nothing else in the folder is a "copy".  (Used with ``fullmatch``.)
_COPY_NAME = re.compile(r"altermmo-(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_([2-9]|[1-9]\d+))?\.zip")

#: Tables the bot empties on purpose (the weekly ranking reset, the Twitch treasure queue):
#: fewer rows there mean nothing.
SHRINK_BY_DESIGN: tuple[str, ...] = ("ranking", "treasure_table")
#: Tables that may become empty in normal play (the last spare gem was taken out of the pouch).
MAY_BECOME_EMPTY: tuple[str, ...] = ("skillpouch",)
#: A table is reported as shrunk when it had at least this many rows in the previous copy ...
SHRINK_MIN_ROWS = 20
#: ... and has fewer than this share of them now.  (A table that is gone, or empty after it
#: held rows, is reported whatever its size.)
SHRINK_SHARE = 0.5

#: The tables the bot works with (its own and the two it shares with the Twitch bot).
TABLES: tuple[str, ...] = tuple(schema.EXPECTED_COLUMNS)

#: Seconds one table may take.
TABLE_TIMEOUT = 300.0
#: A file in the data folder bigger than this is not a state file of the bot; it is left out.
STATE_FILE_LIMIT = 50 * 1024 * 1024

_COLUMNS_QUERY = """SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = ANY($1::text[])
                    ORDER BY table_name, ordinal_position"""

#: Backup folders a copy is being written into right now (one copy at a time per folder).
_in_progress: set[str] = set()


class BackupFailed(RuntimeError):
    """The copy could not be made or did not pass its check; no finished file was left behind."""


class BackupInProgress(RuntimeError):
    """Another copy is being written into the same folder right now."""


@dataclass(frozen=True)
class TableCopy:
    name: str
    rows: int
    columns: tuple[str, ...]


@dataclass(frozen=True)
class Backup:
    """A finished, verified copy."""

    path: Path
    taken_at: datetime
    tables: tuple[TableCopy, ...]
    #: Tables from :data:`TABLES` that the database does not have.
    missing: tuple[str, ...]
    #: Names of the state files in the copy.
    files: tuple[str, ...]
    size: int
    #: Names of the oldest copies removed after this one was finished (see :func:`prune`).
    removed: tuple[str, ...] = ()
    #: Tables with far fewer rows than in the previous copy: ``(table, rows before, rows now)``.
    shrunk: tuple[tuple[str, int, int], ...] = ()

    @property
    def rows(self) -> int:
        return sum(table.rows for table in self.tables)


# -- where the copies live ----------------------------------------------------------------

def folder(settings: Any) -> Path:
    """The backup folder of ``settings`` (default: ``backups`` next to the data folder)."""
    configured = getattr(settings, "backup_dir", None)
    return Path(configured) if configured else Path(settings.data_dir).parent / "backups"


def _copy_order(path: Path) -> tuple[str, int] | None:
    """Sort key of a copy (its time stamp, then its number); None when ``path`` is not named like one."""
    match = _COPY_NAME.fullmatch(path.name)
    return None if match is None else (match.group(1), int(match.group(2) or 1))


def copies(backup_dir: Path) -> list[Path]:
    """The finished copies in ``backup_dir``, oldest first.

    Only regular files named exactly as this module names its copies: a
    renamed copy, an unfinished ``*.part`` or anything else is not listed.
    """
    try:
        found = [path for path in Path(backup_dir).iterdir()
                 if _copy_order(path) is not None and path.is_file() and not path.is_symlink()]
    except OSError:
        return []
    return sorted(found, key=_copy_order)


def prune(backup_dir: Path, keep: int | None, *, protect: Path | None = None) -> list[str]:
    """Remove the oldest copies so that the newest ``keep`` stay; returns the removed names.

    ``keep`` None or 0: nothing is removed.  ``protect`` (the copy just made)
    is never removed, whatever its name sorts like.  Only files listed by
    :func:`copies` can go.  Never raises: a copy that cannot be removed stays.
    """
    if not keep or keep < 1:
        return []
    stored = copies(backup_dir)
    removed: list[str] = []
    for path in stored[:max(0, len(stored) - int(keep))]:
        if protect is not None and path.name == protect.name:
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Backup: the old copy %s could not be removed (%s); it stays.", path.name, exc)
            continue
        removed.append(path.name)
    if removed:
        log.info("Backup: removed the oldest copies (the newest %d stay): %s", keep, ", ".join(removed))
    return removed


def _rows_in(path: Path, debug: bool) -> dict[str, int]:
    """Rows per table according to the manifest of the copy ``path``.

    {} when it cannot be read or comes from the other database (a copy of the
    test database says nothing about the production one): nothing to compare with.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read(MANIFEST))
        if bool(manifest.get("debug", False)) != bool(debug):
            return {}
        return {str(table["name"]): int(table["rows"]) for table in manifest["tables"]}
    except Exception:  # noqa: BLE001 - an unreadable older copy only means: nothing to compare with
        return {}


def _shrunk(previous: dict[str, int], tables: Sequence[TableCopy]) -> list[tuple[str, int, int]]:
    """Tables that lost their rows since the previous copy: ``(table, rows before, rows now)``.

    Reported: a table that held rows and is gone from the database or empty
    now, and a bigger table that lost more than half of its rows.  Not
    reported: what the bot empties by design (:data:`SHRINK_BY_DESIGN`).
    """
    now = {table.name: table.rows for table in tables}
    found = []
    for name, before in previous.items():
        if name in SHRINK_BY_DESIGN or before <= 0:
            continue
        rows = now.get(name)
        if rows is None:
            found.append((name, before, 0))  # the table is not in the database any more
        elif rows == 0 and name not in MAY_BECOME_EMPTY:
            found.append((name, before, 0))
        elif before >= SHRINK_MIN_ROWS and rows < before * SHRINK_SHARE:
            found.append((name, before, rows))
    return found


def newest_age(backup_dir: Path) -> float | None:
    """Seconds since the newest copy was made; None when there is no copy at all."""
    newest: float | None = None
    for path in copies(backup_dir):
        with contextlib.suppress(OSError):
            modified = path.stat().st_mtime
            newest = modified if newest is None else max(newest, modified)
    return None if newest is None else max(0.0, time.time() - newest)


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / 1024 / 1024:.1f} MB"
    return f"{max(1, round(count / 1024))} KB"


def describe(backup: Backup) -> str:
    """One Polish line about a finished copy (for the owner)."""
    backup_dir = backup.path.parent
    stored = copies(backup_dir)
    total = 0
    for path in stored:
        with contextlib.suppress(OSError):
            total += path.stat().st_size
    text = (f"💾 Kopia zapasowa gotowa: `{backup.path.name}` - tabel: {len(backup.tables)}, "
            f"wierszy: {backup.rows}, plików stanu: {len(backup.files)}, rozmiar: {_size(backup.size)}. "
            f"Baza była tylko czytana. W folderze `{backup_dir.name}` jest teraz kopii: {len(stored)} "
            f"(razem {_size(total)}).")
    if backup.removed:
        text += f" Usunięto najstarsze kopie: {len(backup.removed)} (zostają najnowsze)."
    if backup.missing:
        text += " Tych tabel nie ma w bazie: " + ", ".join(backup.missing) + "."
    if backup.shrunk:
        text += (" ⚠️ UWAGA: w porównaniu z poprzednią kopią zniknęły dane z tabel: "
                 + ", ".join(f"{name} (było {before}, jest {now})" for name, before, now in backup.shrunk)
                 + ". Jeśli to nie było zamierzone, przenieś starszą kopię do innego folderu albo dopisz słowo "
                 "na początku nazwy pliku (np. `zachowaj-altermmo-...`) - najstarsze kopie są z czasem usuwane.")
    return text


# -- making a copy ------------------------------------------------------------------------

class _Worker:
    """Runs all the file work of one copy on ONE thread, off the event loop.

    ``zipfile`` objects must not be used from two threads at once; with a
    single worker thread the jobs simply queue up - also the clean-up after a
    cancelled copy waits for the write that was still running.
    """

    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="altermmo-backup")

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(function, *args, **kwargs))

    def close(self) -> None:
        self._pool.shutdown(wait=False)


async def create_backup(db: Any, data_dir: Path, backup_dir: Path, *,
                        tables: Sequence[str] = TABLES, debug: bool = False,
                        keep: int | None = None) -> Backup:
    """Make one copy of ``tables`` and of the files in ``data_dir``; returns it once it is verified.

    ``debug``: the copy comes from a debug run (the test database); noted in the manifest.
    ``keep``: once the new copy is finished, only the newest ``keep`` copies stay
    (:func:`prune`); None / 0 = nothing is removed.

    Raises :class:`BackupInProgress` when a copy into ``backup_dir`` is already
    being made, and whatever went wrong otherwise (database unreachable, disk
    full, :class:`BackupFailed` from the check) - then no finished file exists.
    """
    backup_dir = Path(backup_dir)
    key = os.path.abspath(backup_dir)
    if key in _in_progress:
        raise BackupInProgress(str(backup_dir))
    _in_progress.add(key)
    worker = _Worker()
    try:
        return await _create(db, Path(data_dir), backup_dir, tuple(tables), worker, bool(debug), keep)
    finally:
        _in_progress.discard(key)
        worker.close()


def _open_new(part: Path, opened: list[zipfile.ZipFile]) -> zipfile.ZipFile:
    """Create the ``*.part`` archive; ``opened`` gets it even when the caller was cancelled meanwhile."""
    # "x": the file must not exist yet - nothing is ever written over.
    opened.append(zipfile.ZipFile(part, "x", zipfile.ZIP_DEFLATED))
    return opened[-1]


def _discard_opened(opened: list[zipfile.ZipFile], part: Path) -> None:
    """Clean up after a failed copy: only a ``*.part`` this very call created is removed."""
    _discard(opened[0] if opened else None, part if opened else None)


async def _create(db: Any, data_dir: Path, backup_dir: Path, tables: tuple[str, ...],
                  worker: _Worker, debug: bool, keep: int | None) -> Backup:
    taken_at = clock.now()
    final, part = await worker.run(_reserve, backup_dir, taken_at)
    earlier = await worker.run(copies, backup_dir)
    previous = await worker.run(_rows_in, earlier[-1], debug) if earlier else {}
    # Read right here, on the event-loop thread: the bot saves these files on this thread
    # (core/storage.py), so a read can never overlap a save.  On Windows a file that is
    # open for reading cannot be replaced - a read on the worker thread could make a save fail.
    state_files = _read_state_files(data_dir)
    #: Filled on the worker thread by the job that creates the file: a cancellation that
    #: arrives while it runs cannot lose the fact that the file is ours.
    opened: list[zipfile.ZipFile] = []
    finished = False
    try:
        archive = await worker.run(_open_new, part, opened)
        copied, missing = await _copy_tables(db, archive, tables, worker)
        files = await worker.run(_add_state_files, archive, state_files)
        manifest = {
            "format": FORMAT,
            "taken_at": taken_at.isoformat(),
            "debug": debug,
            "tables": [{"name": table.name, "rows": table.rows, "columns": list(table.columns)}
                       for table in copied],
            "missing_tables": list(missing),
            "files": list(files),
        }
        await worker.run(archive.writestr, SCHEMA_FILE, _schema_text())
        await worker.run(archive.writestr, MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        await worker.run(archive.close)
        await worker.run(_flush_to_disk, part)
        await worker.run(_verify, part, manifest)
        size = await worker.run(_publish, part, final)
        finished = True
    finally:
        if not finished:
            # Runs after whatever the worker thread was still doing (one thread, in order).
            with contextlib.suppress(Exception):
                await asyncio.shield(worker.run(_discard_opened, opened, part))
    log.info("Backup written: %s (%d tables, %d rows, %d state files, %d bytes).",
             final, len(copied), sum(table.rows for table in copied), len(files), size)
    shrunk = _shrunk(previous, copied)
    if shrunk:
        log.warning("Backup: far fewer rows than in the previous copy: %s", shrunk)
    # Only now, with the new copy finished and checked, may the oldest ones go.
    removed = await asyncio.shield(worker.run(prune, backup_dir, keep, protect=final))
    return Backup(path=final, taken_at=taken_at, tables=tuple(copied), missing=tuple(missing),
                  files=tuple(files), size=size, removed=tuple(removed), shrunk=tuple(shrunk))


async def _copy_tables(db: Any, archive: zipfile.ZipFile, tables: tuple[str, ...],
                       worker: _Worker) -> tuple[list[TableCopy], list[str]]:
    """Write ``db/<table>.csv`` for every table that exists.  The database is only read."""
    copied: list[TableCopy] = []
    missing: list[str] = []
    # READ ONLY: the database refuses any change inside this transaction.
    # REPEATABLE READ: all tables as of one moment.
    async with db.transaction(readonly=True, isolation="repeatable_read") as con:
        columns: dict[str, list[str]] = {}
        for row in await con.fetch(_COLUMNS_QUERY, list(tables)):
            columns.setdefault(row["table_name"], []).append(row["column_name"])
        for table in tables:
            if table not in columns:
                missing.append(table)
                continue
            stream = await worker.run(archive.open, f"db/{table}.csv", "w", force_zip64=True)
            # A problem with the FILE (disk full ...) must not surface inside the COPY: the
            # database connection would be stuck half-way and the error reported as a
            # database outage.  It is remembered, the rest of the table is let through
            # unwritten, and the copy fails afterwards with the real reason.
            trouble: list[BaseException] = []
            try:
                async def sink(data: bytes, stream: Any = stream, trouble: list = trouble) -> None:
                    if trouble:
                        return
                    try:
                        await worker.run(stream.write, data)
                    except Exception as exc:  # noqa: BLE001
                        trouble.append(exc)

                status = await con.copy_from_table(table, schema_name="public", output=sink, format="csv",
                                                   header=True, timeout=TABLE_TIMEOUT)
            finally:
                try:
                    await worker.run(stream.close)
                except Exception as exc:  # noqa: BLE001
                    trouble.append(exc)
            if trouble:
                raise BackupFailed(f"cannot write the copy ({table}): {trouble[0]!r}") from trouble[0]
            copied.append(TableCopy(name=table, rows=_rows_of(status), columns=tuple(columns[table])))
    return copied, missing


def _rows_of(status: str) -> int:
    """``"COPY 42"`` -> 42."""
    try:
        return int(str(status).split()[-1])
    except (IndexError, ValueError) as exc:
        raise BackupFailed(f"unexpected answer of the database to COPY: {status!r}") from exc


def _schema_text() -> str:
    statements = [statement.strip().rstrip(";") for statement in schema.all_statements()]
    return ("-- CREATE TABLE statements of the AlterMMO bot (for restoring into an empty database).\n"
            "-- A database changed by hand later may differ: the column names of the copied\n"
            "-- tables are in manifest.json and in the first line of every CSV file.\n\n"
            + ";\n\n".join(statements) + ";\n")


def _reserve(backup_dir: Path, taken_at: datetime) -> tuple[Path, Path]:
    """Create the folder and choose a name nobody uses: ``(final, part)``."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = taken_at.strftime("%Y-%m-%d_%H-%M-%S")
    number = 1
    while True:
        name = f"{PREFIX}{stamp}" + ("" if number == 1 else f"_{number}") + SUFFIX
        final = backup_dir / name
        part = backup_dir / (name + PART_SUFFIX)
        if not final.exists() and not part.exists():
            return final, part
        number += 1


def _read_state_files(data_dir: Path) -> list[tuple[str, bytes]]:
    """Name and content of every file in ``data_dir`` (they are only read).

    Call it on the event-loop thread, see :func:`_create`.
    """
    found: list[tuple[str, bytes]] = []
    try:
        paths = sorted(path for path in data_dir.iterdir() if path.is_file())
    except OSError:
        log.warning("Backup: the data folder %s cannot be listed; the copy holds the database only.", data_dir)
        return found
    for path in paths:
        if path.name.endswith(".tmp"):
            continue  # a state file in the middle of being written (see core/storage.py)
        try:
            if path.stat().st_size > STATE_FILE_LIMIT:
                log.warning("Backup: %s is not a state file of the bot (too big); it is not in the copy.", path)
                continue
            found.append((path.name, path.read_bytes()))
        except OSError:
            log.warning("Backup: state file %s cannot be read; it is not in the copy.", path)
    return found


def _add_state_files(archive: zipfile.ZipFile, state_files: list[tuple[str, bytes]]) -> list[str]:
    """Put the state files read by :func:`_read_state_files` into the archive."""
    for name, content in state_files:
        archive.writestr(f"data/{name}", content)
    return [name for name, _ in state_files]


def _flush_to_disk(part: Path) -> None:
    """Make sure the written copy is on the disk, not only in the system's cache.

    Without it a power cut right after the rename could leave a damaged file
    under the name of a finished copy.
    """
    with open(part, "r+b") as handle:  # (Windows flushes only a handle opened for writing)
        os.fsync(handle.fileno())


def _verify(part: Path, manifest: dict[str, Any]) -> None:
    """Read the written archive back: checksums, the manifest, the rows of every table."""
    csv.field_size_limit(2 ** 31 - 1)  # long text values must not fail the check
    with zipfile.ZipFile(part) as archive:
        broken = archive.testzip()
        if broken is not None:
            raise BackupFailed(f"the copy is damaged (checksum of {broken})")
        if json.loads(archive.read(MANIFEST)) != manifest:
            raise BackupFailed("the manifest in the copy differs from what was written")
        for table in manifest["tables"]:
            with archive.open(f"db/{table['name']}.csv") as raw:
                reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
                header = next(reader, None)
                rows = sum(1 for _ in reader)
            if header != table["columns"]:
                raise BackupFailed(f"table {table['name']}: columns {header} instead of {table['columns']}")
            if rows != table["rows"]:
                raise BackupFailed(f"table {table['name']}: {rows} rows in the copy, "
                                   f"{table['rows']} were sent by the database")
        for name in manifest["files"]:
            archive.getinfo(f"data/{name}")
        archive.getinfo(SCHEMA_FILE)


def _publish(part: Path, final: Path) -> int:
    """Give the checked copy its final name (never over an existing file); returns its size."""
    if final.exists():
        raise BackupFailed(f"{final.name} already exists - it is not overwritten")
    os.rename(part, final)
    return final.stat().st_size


def _discard(archive: zipfile.ZipFile | None, part: Path | None) -> None:
    """Remove the unfinished ``*.part`` of a failed copy (and nothing else).

    ``part`` is None when this copy did not create the file (e.g. it already existed).
    """
    if archive is not None:
        with contextlib.suppress(Exception):
            archive.close()
        handle = getattr(archive, "fp", None)
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()
    if part is not None and part.name.endswith(SUFFIX + PART_SUFFIX):
        with contextlib.suppress(OSError):
            part.unlink()


# -- python -m altermmo.features.backup ---------------------------------------------------

def main() -> int:
    """Make one copy of the database from ``.env`` and exit (0 = made and verified)."""
    from ..core.db import Database
    from ..settings import load_settings

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr] - emoji on an old console
    settings = load_settings()
    if not settings.database_url:
        print("Brak adresu bazy danych w pliku .env - kopia nie powstała.")
        return 2

    async def run() -> Backup:
        db = Database(settings.database_url, min_size=1, max_size=1)
        await db.connect()
        try:
            return await create_backup(db, settings.data_dir, folder(settings), debug=settings.debug,
                                       keep=settings.backup_keep)
        finally:
            await db.close()

    try:
        backup = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - say it plainly, whatever it was
        print(f"Kopia NIE powstała: {type(exc).__name__}: {exc}")
        return 1
    if settings.debug:
        print("UWAGA: tryb debug (ALTERMMO_DEBUG w pliku .env) - to jest kopia bazy TESTOWEJ, "
              "nie produkcyjnej.")
    else:
        print("To jest kopia bazy produkcyjnej (DATABASE_URL z pliku .env).")
    print(describe(backup))
    print(f"Plik: {backup.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
