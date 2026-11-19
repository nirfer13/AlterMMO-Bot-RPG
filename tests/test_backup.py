"""Copies of the database (features/backup.py): complete, restorable - and they never change anything."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import zipfile
from datetime import datetime
from types import SimpleNamespace

import asyncpg
import discord
import pytest

from altermmo.cogs import backup as backup_cog
from altermmo.core import clock
from altermmo.core.state import BossPhase
from altermmo.features import backup

NASTY = "O'Neil, \"Zły\" Łowca;\nw drugiej linii \\. żółć 💀"


@pytest.fixture
def folders(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "expSummary.json").write_text('{"1": 2500, "2": -2500}', encoding="utf-8")
    (data / "daily_player_cd.txt").write_text("1\n2\n", encoding="utf-8")
    (data / "runtime.json.abc.tmp").write_text("half written", encoding="utf-8")
    return SimpleNamespace(data=data, backups=tmp_path / "backups")


async def fill(db) -> None:
    await db.execute("INSERT INTO RANKING (ID, NICK, POINTS) VALUES ($1, $2, $3)", "1", NASTY, 7)
    await db.execute("INSERT INTO RANKING (ID, NICK, POINTS) VALUES ($1, $2, $3)", "2", "", 0)
    await db.execute("INSERT INTO RANKING (ID, NICK, POINTS) VALUES ($1, $2, $3)", "3", None, None)
    await db.execute("INSERT INTO BOSS (ID, RARITY, RESPAWN_TIME, RESPAWN_STARTED) VALUES (0, 2, $1, TRUE)",
                     datetime(2026, 11, 19, 3, 15, 0))
    await db.execute("INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID) VALUES (291836779495948288, 5)")


async def fingerprint(db) -> dict[str, tuple[int, str | None]]:
    """Rows and a checksum of the content of every table the bot works with."""
    result = {}
    present = {row["table_name"] for row in await db.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    for table in backup.TABLES:
        if table in present:
            row = await db.fetchrow(
                f'SELECT count(*) AS rows, md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) AS sum '
                f'FROM "{table}" t')
            result[table] = (row["rows"], row["sum"])
    return result


def read_csv(archive: zipfile.ZipFile, table: str) -> list[list[str]]:
    with archive.open(f"db/{table}.csv") as raw:
        return list(csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline="")))


# -- what a copy holds -------------------------------------------------------------------------

async def test_copy_holds_every_table_and_the_state_files(db, folders):
    await fill(db)
    made = await backup.create_backup(db, folders.data, folders.backups)
    assert made.path.parent == folders.backups and made.path.name.startswith("altermmo-")
    assert made.path.suffix == ".zip" and made.size == made.path.stat().st_size > 0
    assert {table.name for table in made.tables} | set(made.missing) == set(backup.TABLES)
    rows = {table.name: table.rows for table in made.tables}
    assert rows["ranking"] == 3 and rows["boss"] == 1 and rows["skillpouch"] == 1 and rows["pets"] == 0
    assert made.rows == sum(rows.values())
    assert made.files == ("daily_player_cd.txt", "expSummary.json")          # no half-written *.tmp

    with zipfile.ZipFile(made.path) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == 1 and manifest["files"] == list(made.files)
        assert manifest["debug"] is False
        assert {entry["name"]: entry["rows"] for entry in manifest["tables"]} == rows
        ranking = read_csv(archive, "ranking")
        assert ranking[0] == ["id", "nick", "points"]
        assert sorted(ranking[1:]) == [["1", NASTY, "7"], ["2", "", "0"], ["3", "", ""]]
        assert read_csv(archive, "pets")[0][:2] == ["pet_id", "pet_name"]    # an empty table: the header only
        assert archive.read("data/expSummary.json") == b'{"1": 2500, "2": -2500}'
        assert "CREATE TABLE IF NOT EXISTS RANKING" in archive.read("schema.sql").decode("utf-8")
        for name in archive.namelist():                                      # the password never leaves .env
            assert b"postgresql://" not in archive.read(name)
    assert [path.name for path in folders.backups.iterdir()] == [made.path.name]   # no *.part left


async def test_missing_tables_are_named_not_fatal(db, folders):
    await db.execute("DROP TABLE SKILLPOUCH")                                 # (the throw-away test database)
    made = await backup.create_backup(db, folders.data, folders.backups)
    assert "skillpouch" in made.missing and "skillpouch" not in {table.name for table in made.tables}
    assert "Tych tabel nie ma w bazie: " in backup.describe(made) and "skillpouch" in backup.describe(made)


async def test_copy_can_be_put_back(db, folders):
    """The CSV files are what PostgreSQL itself reads back: a restore gives the same rows."""
    await fill(db)
    made = await backup.create_backup(db, folders.data, folders.backups)
    with zipfile.ZipFile(made.path) as archive:
        for table in ("ranking", "boss", "skillpouch"):
            content = archive.read(f"db/{table}.csv")
            await db.execute(f'CREATE TABLE restored_{table} (LIKE "{table}")')
            async with db.transaction() as con:
                await con.copy_to_table(f"restored_{table}", source=io.BytesIO(content), format="csv", header=True)
            for one, other in ((table, f"restored_{table}"), (f"restored_{table}", table)):
                assert await db.fetch(f'SELECT * FROM "{one}" EXCEPT SELECT * FROM "{other}"') == []
    assert await db.fetchval("SELECT nick FROM restored_ranking WHERE id = '1'") == NASTY
    assert await db.fetchval("SELECT nick FROM restored_ranking WHERE id = '2'") == ""
    assert await db.fetchval("SELECT nick FROM restored_ranking WHERE id = '3'") is None   # NULL stays NULL


# -- the owner's condition: nothing is changed, nothing is deleted -------------------------------

async def test_database_is_identical_after_a_copy(db, folders):
    await fill(db)
    before = await fingerprint(db)
    await backup.create_backup(db, folders.data, folders.backups)
    await backup.create_backup(db, folders.data, folders.backups)
    assert await fingerprint(db) == before and before["ranking"][0] == 3


async def test_copy_only_reads_inside_a_read_only_transaction(db, folders, monkeypatch):
    """Everything the copy sends to the database: one SELECT of column names and COPY ... TO per table."""
    await fill(db)
    opened: list[dict] = []
    sent: list[tuple] = []
    real_transaction = db.transaction

    class ReadingOnly:
        """Lets through only what a copy may do; anything else fails the test."""

        def __init__(self, con):
            self._con = con

        async def fetch(self, query, *args, **kwargs):
            sent.append(("fetch", " ".join(query.split())))
            return await self._con.fetch(query, *args, **kwargs)

        async def copy_from_table(self, table, **kwargs):
            sent.append(("copy_from_table", table))
            return await self._con.copy_from_table(table, **kwargs)

        def __getattr__(self, name):
            raise AssertionError(f"the backup called connection.{name}")

    @contextlib.asynccontextmanager
    async def transaction(**kwargs):
        opened.append(kwargs)
        async with real_transaction(**kwargs) as con:
            assert await con.fetchval("SHOW transaction_read_only") == "on"
            assert await con.fetchval("SHOW transaction_isolation") == "repeatable read"
            yield ReadingOnly(con)

    monkeypatch.setattr(db, "transaction", transaction)
    made = await backup.create_backup(db, folders.data, folders.backups)
    assert opened == [{"readonly": True, "isolation": "repeatable_read"}]
    assert sent[0][0] == "fetch" and sent[0][1].startswith("SELECT table_name, column_name FROM information_schema")
    assert sent[1:] == [("copy_from_table", table.name) for table in made.tables] and len(sent) > 5


@pytest.mark.parametrize("statement", [
    "DELETE FROM RANKING",
    "TRUNCATE RANKING",
    "DROP TABLE RANKING",
    "UPDATE RANKING SET POINTS = 0",
    "INSERT INTO RANKING (ID, NICK, POINTS) VALUES ('9', 'x', 1)",
    "ALTER TABLE RANKING ADD COLUMN x INT",
])
async def test_the_database_refuses_changes_in_the_transaction_the_copy_uses(db, statement):
    await fill(db)
    before = await fingerprint(db)
    with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
        async with db.transaction(readonly=True, isolation="repeatable_read") as con:
            await con.execute(statement)
    assert await fingerprint(db) == before


async def test_players_keep_playing_while_a_copy_is_made(db, folders, monkeypatch):
    """The copy is one snapshot; a write made meanwhile is neither blocked nor half-copied."""
    await fill(db)
    wrote = []
    real_copy = asyncpg.Connection.copy_from_table

    async def copy_from_table(self, table, **kwargs):
        if table == "boss" and not wrote:                 # the first table: a player scores meanwhile
            wrote.append(await asyncio.wait_for(
                db.execute("INSERT INTO RANKING (ID, NICK, POINTS) VALUES ('77', 'w trakcie', 1)"), 5))
        return await real_copy(self, table, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "copy_from_table", copy_from_table)
    made = await backup.create_backup(db, folders.data, folders.backups)
    assert wrote == ["INSERT 0 1"]                                            # the game was not blocked
    assert await db.fetchval("SELECT count(*) FROM RANKING") == 4             # ...and its row is there
    assert {table.name: table.rows for table in made.tables}["ranking"] == 3  # the copy: the moment it began


# -- the files -----------------------------------------------------------------------------------

async def test_existing_copies_are_never_overwritten_or_removed(db, folders, monkeypatch):
    moment = datetime(2026, 11, 19, 3, 15, 0, tzinfo=clock.WARSAW)
    monkeypatch.setattr(clock, "now", lambda: moment)
    folders.backups.mkdir()
    other = folders.backups / "notatki.txt"
    other.write_text("moje", encoding="utf-8")
    first = await backup.create_backup(db, folders.data, folders.backups)
    content = first.path.read_bytes()
    await fill(db)
    second = await backup.create_backup(db, folders.data, folders.backups)   # the very same second
    third = await backup.create_backup(db, folders.data, folders.backups)
    assert first.path.name == "altermmo-2026-11-19_03-15-00.zip"
    assert second.path.name == "altermmo-2026-11-19_03-15-00_2.zip"
    assert third.path.name == "altermmo-2026-11-19_03-15-00_3.zip"
    assert first.path.read_bytes() == content and other.read_text(encoding="utf-8") == "moje"
    assert backup.copies(folders.backups) == [first.path, second.path, third.path]
    assert (folders.data / "expSummary.json").read_text(encoding="utf-8") == '{"1": 2500, "2": -2500}'
    assert (folders.data / "runtime.json.abc.tmp").exists()                  # state files are only read


async def test_failed_check_leaves_no_file_that_looks_like_a_copy(db, folders, monkeypatch):
    await fill(db)
    before = await fingerprint(db)
    earlier = await backup.create_backup(db, folders.data, folders.backups)

    def verify(part, manifest):
        raise backup.BackupFailed("rows do not match")

    monkeypatch.setattr(backup, "_verify", verify)
    with pytest.raises(backup.BackupFailed):
        await backup.create_backup(db, folders.data, folders.backups)
    assert [path.name for path in folders.backups.iterdir()] == [earlier.path.name]   # the old copy stays
    assert await fingerprint(db) == before


async def test_check_notices_a_table_that_lost_rows_on_the_way(db, folders, monkeypatch):
    await fill(db)
    monkeypatch.setattr(backup, "_rows_of", lambda status: int(status.split()[-1]) + 1)
    with pytest.raises(backup.BackupFailed, match="rows in the copy"):
        await backup.create_backup(db, folders.data, folders.backups)
    assert backup.copies(folders.backups) == []


async def test_cancelled_copy_stays_cancelled_and_leaves_nothing(db, folders, monkeypatch):
    """A restart of the loops / the shutdown in the middle of a table: the task really ends."""
    await db.execute("INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID) SELECT g, g FROM generate_series(1, 400000) g")
    before = await fingerprint(db)
    writing = asyncio.Event()
    real_run = backup._Worker.run

    async def run(self, function, *args, **kwargs):
        if getattr(function, "__name__", "") == "write":
            writing.set()
            await asyncio.sleep(0.01)                                         # the table takes a while
        return await real_run(self, function, *args, **kwargs)

    monkeypatch.setattr(backup._Worker, "run", run)
    task = asyncio.ensure_future(backup.create_backup(db, folders.data, folders.backups))
    await asyncio.wait_for(writing.wait(), 20)
    cancelled_at = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    assert task.cancelled()
    assert asyncio.get_running_loop().time() - cancelled_at < 5              # a shutdown does not hang on it
    assert list(folders.backups.iterdir()) == []                              # no copy, no *.part
    monkeypatch.setattr(backup._Worker, "run", real_run)
    assert await db.fetchval("SELECT 41 + 1") == 42 and await fingerprint(db) == before
    async with db.transaction() as con:                                       # the pool is not left read-only
        assert await con.fetchval("SHOW transaction_read_only") == "off"
    made = await backup.create_backup(db, folders.data, folders.backups)      # ...and the next copy works
    assert {table.name: table.rows for table in made.tables}["skillpouch"] == 400000


async def test_full_disk_is_reported_as_that_not_as_a_database_outage(db, folders, monkeypatch):
    import time

    await fill(db)
    real_run = backup._Worker.run

    async def run(self, function, *args, **kwargs):
        if getattr(function, "__name__", "") == "write":
            raise OSError(28, "No space left on device")
        return await real_run(self, function, *args, **kwargs)

    monkeypatch.setattr(backup._Worker, "run", run)
    started = time.monotonic()
    with pytest.raises(backup.BackupFailed, match="No space left on device"):
        await backup.create_backup(db, folders.data, folders.backups)
    assert time.monotonic() - started < 10                                    # no 20 s wait for a stuck connection
    assert list(folders.backups.iterdir()) == []
    assert await db.fetchval("SELECT count(*) FROM RANKING") == 3


async def test_part_file_of_somebody_else_is_not_removed(db, folders, monkeypatch):
    """Two processes choosing the same name: the second one fails and touches nothing."""
    folders.backups.mkdir()
    final = folders.backups / "altermmo-2026-11-19_03-15-00.zip"
    part = folders.backups / (final.name + ".part")
    part.write_bytes(b"the other process is writing here")
    monkeypatch.setattr(backup, "_reserve", lambda backup_dir, taken_at: (final, part))
    with pytest.raises(FileExistsError):
        await backup.create_backup(db, folders.data, folders.backups)
    assert part.read_bytes() == b"the other process is writing here" and not final.exists()


async def test_copy_cancelled_while_its_file_is_being_created_leaves_nothing(db, folders, monkeypatch):
    creating = asyncio.Event()
    loop = asyncio.get_running_loop()
    real_open = backup._open_new

    def open_new(part, opened):
        import time

        loop.call_soon_threadsafe(creating.set)
        time.sleep(0.2)                                                      # the cancellation arrives meanwhile
        return real_open(part, opened)

    monkeypatch.setattr(backup, "_open_new", open_new)
    task = asyncio.ensure_future(backup.create_backup(db, folders.data, folders.backups))
    await asyncio.wait_for(creating.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert list(folders.backups.iterdir()) == []                              # the file it did create is gone


async def test_state_files_are_read_on_the_event_loop_thread(db, folders, monkeypatch):
    """...where the bot saves them: a read can never make a save fail (Windows)."""
    import threading

    threads = []
    real = backup._read_state_files

    def read_state_files(data_dir):
        threads.append(threading.current_thread())
        return real(data_dir)

    monkeypatch.setattr(backup, "_read_state_files", read_state_files)
    made = await backup.create_backup(db, folders.data, folders.backups)
    assert threads == [threading.main_thread()] and made.files == ("daily_player_cd.txt", "expSummary.json")


async def test_unreachable_database_makes_no_copy(folders):
    from altermmo.core.db import Database

    with pytest.raises(Exception):
        await backup.create_backup(Database(""), folders.data, folders.backups)
    assert backup.copies(folders.backups) == [] and not list(folders.backups.glob("*.part"))


async def test_one_copy_at_a_time(db, folders):
    first = asyncio.ensure_future(backup.create_backup(db, folders.data, folders.backups))
    await asyncio.sleep(0)
    with pytest.raises(backup.BackupInProgress):
        await backup.create_backup(db, folders.data, folders.backups)
    await first
    await backup.create_backup(db, folders.data, folders.backups)             # free again afterwards


def test_folder_and_age(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path / "data", backup_dir=None)
    assert backup.folder(settings) == tmp_path / "backups"
    settings.backup_dir = tmp_path / "elsewhere"
    assert backup.folder(settings) == tmp_path / "elsewhere"
    assert backup.newest_age(tmp_path / "nothing-here") is None and backup.copies(tmp_path / "nothing-here") == []
    (tmp_path / "altermmo-2026-11-19_03-15-00.zip").write_bytes(b"x")
    (tmp_path / "altermmo-2026-11-19_03-15-00.zip.part").write_bytes(b"x")    # unfinished: not a copy
    assert [path.name for path in backup.copies(tmp_path)] == ["altermmo-2026-11-19_03-15-00.zip"]
    assert 0 <= backup.newest_age(tmp_path) < 60


# -- the cog: when copies are made ---------------------------------------------------------------

@pytest.fixture
def cog(game_db):
    return backup_cog.BackupCog(game_db.bot)


def ctx_of(game, member):
    return SimpleNamespace(bot=game.bot, channel=game.channel, author=member, message=game.say(member, "$kopia"))


async def test_kopia_command_makes_a_copy_and_says_so(game_db, cog):
    game = game_db
    alter = game.add_member("Alter")
    alter.guild_permissions = discord.Permissions(administrator=True)
    await cog.copy_now.callback(cog, ctx_of(game, alter))
    made = backup.copies(cog.folder)
    assert len(made) == 1 and cog.folder == game.bot.store.data_dir.parent / "backups"
    answer = game.channel.messages[-1].content
    assert answer.startswith("💾 Kopia zapasowa gotowa: `" + made[0].name) and "Baza była tylko czytana" in answer
    with zipfile.ZipFile(made[0]) as archive:                                 # (the test game runs in debug mode)
        assert json.loads(archive.read("manifest.json"))["debug"] is game.bot.debug is True
    assert "jest teraz kopii: 1" in answer
    assert game.channel.contains(backup_cog.MSG_STARTED)


async def test_kopia_is_for_administrators(game_db, cog):
    from discord.ext import commands

    player = game_db.add_member("Ala")
    ctx = ctx_of(game_db, player)
    ctx.guild = game_db.bot.fake_guild
    ctx.permissions = discord.Permissions.none()
    with pytest.raises(commands.MissingPermissions):
        for check in cog.copy_now.checks:
            await discord.utils.maybe_coroutine(check, ctx)
    assert backup.copies(cog.folder) == []


async def test_kopia_reports_a_failure_without_details_on_the_channel(game_db, cog, monkeypatch):
    game = game_db
    alter = game.add_member("Alter")

    async def broken(*args, **kwargs):
        raise OSError("disk full at 10.0.0.5")

    monkeypatch.setattr(backup, "create_backup", broken)
    await cog.copy_now.callback(cog, ctx_of(game, alter))
    answer = game.channel.messages[-1].content
    assert answer == backup_cog.MSG_FAILED_HERE.format(kind="OSError") and "10.0.0.5" not in answer
    assert any("disk full" in line for line in game.bot.owner_log)


async def test_copy_at_start_only_when_none_is_fresh(game_db, cog):
    assert await cog.copy_at_start() is True                                  # no copy at all: make one
    assert len(backup.copies(cog.folder)) == 1
    assert any("Kopia zapasowa gotowa" in line for line in game_db.bot.owner_log)
    assert await cog.copy_at_start() is False                                 # a fresh one exists
    assert len(backup.copies(cog.folder)) == 1


async def test_copy_at_start_never_raises(game_db, cog, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr(backup, "create_backup", broken)
    assert await cog.copy_at_start() is False
    assert any("NIE powstała" in line and "no database" in line for line in game_db.bot.owner_log)


async def test_nightly_copy_skips_when_one_was_just_made(game_db, cog, monkeypatch):
    await cog.scheduled_copy()
    assert len(backup.copies(cog.folder)) == 1
    await cog.scheduled_copy()                                                # e.g. right after the start-up copy
    assert len(backup.copies(cog.folder)) == 1
    monkeypatch.setattr(backup_cog, "FRESH_ENOUGH", 0.0)
    await cog.scheduled_copy()
    assert len(backup.copies(cog.folder)) == 2


async def test_error_during_a_cancellation_does_not_keep_the_loop_alive(game_db, cog, monkeypatch):
    """A copy that comes back with some error while its task is being cancelled: the task ends."""
    started = asyncio.Event()

    async def interrupted(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise RuntimeError("cannot switch to state 15; another operation is in progress") from None

    monkeypatch.setattr(backup, "create_backup", interrupted)
    for copy in (cog.copy_at_start, cog.scheduled_copy):
        started.clear()
        task = asyncio.ensure_future(copy())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert not any("NIE powstała" in line for line in game_db.bot.owner_log)   # no false alarm for the owner


async def test_nightly_copy_failure_reaches_the_scheduler(game_db, cog, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr(backup, "create_backup", broken)
    with pytest.raises(RuntimeError):
        await cog.scheduled_copy()                                            # the scheduler retries and reports


async def test_automatic_copy_waits_for_a_quiet_channel(game_db, cog, monkeypatch):
    game = game_db
    monkeypatch.setattr(backup_cog, "QUIET_POLL", 0.01)
    with game.bot.state.hold_busy("hunt"):
        waiting = asyncio.ensure_future(cog.scheduled_copy())
        await asyncio.sleep(0.1)
        assert not waiting.done() and backup.copies(cog.folder) == []         # not in the middle of a hunt
    await game.bot.state.set_boss_phase(BossPhase.FIGHTING)
    await asyncio.sleep(0.1)
    assert not waiting.done() and backup.copies(cog.folder) == []             # ...nor of a boss fight
    await game.bot.state.set_boss_phase(BossPhase.IDLE)
    await asyncio.wait_for(waiting, 10)
    assert len(backup.copies(cog.folder)) == 1


async def test_automatic_copy_does_not_wait_for_ever(game_db, cog, monkeypatch):
    monkeypatch.setattr(backup_cog, "QUIET_POLL", 0.01)
    monkeypatch.setattr(backup_cog, "QUIET_WAIT", 0.05)
    with game_db.bot.state.hold_busy("stuck"):
        assert await cog.wait_for_quiet() is False
        await asyncio.wait_for(cog.scheduled_copy(), 10)                      # a stuck flag must not stop the copies
    assert len(backup.copies(cog.folder)) == 1


async def test_the_bot_loads_the_backup_cog_and_starts_the_nightly_job(game_db):
    from altermmo.bot import EXTENSIONS

    assert "altermmo.cogs.backup" in EXTENSIONS
    cog = backup_cog.BackupCog(game_db.bot)
    await cog.cog_load()
    assert game_db.bot.supervisor.is_running(backup_cog.BACKUP_JOB)
    await cog.cog_unload()
    assert backup_cog.BACKUP_JOB not in game_db.bot.supervisor.names()
