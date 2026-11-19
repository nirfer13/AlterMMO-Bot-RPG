"""Copies of the database: one every night, one at start-up when none is fresh, ``$kopia`` on demand.

The copying itself (and why it cannot delete anything) is described in
:mod:`altermmo.features.backup`.  This cog decides *when*:

* every day at 03:15 (between the daily reset and the Twitch treasure);
* right after the start when the newest copy is older than a day or there is
  none - so the very first start of the bot begins with a copy;
* ``$kopia`` (administrators): now.

Only the newest ``settings.backup_keep`` copies stay (owner: 14).

The automatic copies wait for a quiet moment (no hunt / tower run / event, no
boss fight) - the owner does not want anything to stutter during a fight.
"""

from __future__ import annotations

import asyncio
import logging
import time

from discord.ext import commands

from ..core import guards, io, scheduler, ui
from ..core.state import BossPhase
from ..features import backup

log = logging.getLogger(__name__)

BACKUP_JOB = "database-backup"
BACKUP_HOUR, BACKUP_MINUTE = 3, 15
#: A copy at start-up when the newest one is older than this (seconds).
STALE_AFTER = 24 * 3600.0
#: The scheduled copy is skipped when one was made this recently (e.g. at start-up).
FRESH_ENOUGH = 3600.0
#: How long an automatic copy waits for a quiet moment before it is made anyway.
QUIET_WAIT = 3 * 3600.0
QUIET_POLL = 15.0
#: ``$kopia`` right after a copy would only push an older copy out of the kept ones.
MANUAL_GAP = 600.0

MSG_RUNNING = "Kopia zapasowa właśnie się robi - poczekaj, aż skończy."
MSG_STARTED = "Robię kopię zapasową bazy (baza jest tylko czytana)..."
MSG_JUST_MADE = ("Ostatnia kopia zapasowa jest sprzed {minutes} min - kolejną można zrobić {when}. "
                 "(Zostaje tylko {keep} najnowszych kopii, więc kopie robione jedna po drugiej "
                 "wypychałyby starsze.)")
MSG_FAILED = ("Kopia zapasowa NIE powstała: `{error}`. Baza nie została zmieniona "
              "(kopia tylko ją czyta).")
#: The answer on the channel names only the kind of the error; the details go to the owner's log.
MSG_FAILED_HERE = ("Kopia zapasowa NIE powstała (`{kind}`) - szczegóły są na kanale logów. "
                   "Baza nie została zmieniona (kopia tylko ją czyta).")


def _keep_cancellation(error: BaseException) -> None:
    """Re-raise as a cancellation when the running task is being cancelled.

    A copy interrupted by a restart of the loops or by the shutdown may come
    back as some other error; treating that as "the copy failed" would keep
    the cancelled loop alive.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError() from error


class BackupCog(commands.Cog, name="Backup"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.supervisor.start(BACKUP_JOB, self._loop)

    async def cog_unload(self) -> None:
        self.bot.supervisor.stop(BACKUP_JOB)

    # -- when -------------------------------------------------------------------------

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        await self.copy_at_start()
        job = scheduler.daily(BACKUP_JOB, BACKUP_HOUR, BACKUP_MINUTE, self.scheduled_copy)
        await scheduler.run_job(job, self.bot.store)

    async def copy_at_start(self) -> bool:
        """Make a copy when the newest one is older than a day (or there is none).  Never raises."""
        age = backup.newest_age(self.folder)
        if age is not None and age < STALE_AFTER:
            return False
        log.info("No fresh backup (newest: %s) - making one now.",
                 "none" if age is None else f"{age / 3600:.1f} h old")
        try:
            await self.wait_for_quiet()
            await self.make_copy()
            return True
        except asyncio.CancelledError:
            raise
        except backup.BackupInProgress:
            return False
        except Exception as exc:  # noqa: BLE001 - the nightly job will try again
            _keep_cancellation(exc)
            log.exception("The backup at start-up failed.")
            await self.bot.log_to_owner(MSG_FAILED.format(error=f"{type(exc).__name__}: {exc}"), ping=True)
            return False

    async def scheduled_copy(self) -> None:
        """The nightly copy.  A failure raises: the scheduler retries and tells the owner."""
        age = backup.newest_age(self.folder)
        if age is not None and age < FRESH_ENOUGH:
            log.info("Nightly backup skipped: a copy was made %.0f minutes ago.", age / 60)
            return
        await self.wait_for_quiet()
        try:
            await self.make_copy()
        except backup.BackupInProgress:
            log.info("Nightly backup skipped: a copy is being made right now.")
        except Exception as exc:
            _keep_cancellation(exc)
            raise

    def game_in_action(self) -> bool:
        """True while something plays on the boss channel that a copy should not disturb."""
        state = self.bot.state
        return state.busy or state.boss_phase in (BossPhase.ENGAGED, BossPhase.FIGHTING)

    async def wait_for_quiet(self) -> bool:
        """Wait until nothing plays (at most :data:`QUIET_WAIT`); False when the wait ran out."""
        deadline = time.monotonic() + QUIET_WAIT
        while self.game_in_action():
            if time.monotonic() >= deadline:
                log.warning("Backup: the channel was never quiet for %.0f minutes - copying anyway.",
                            QUIET_WAIT / 60)
                return False
            await asyncio.sleep(QUIET_POLL)
        return True

    # -- how --------------------------------------------------------------------------

    @property
    def folder(self):
        return backup.folder(self.bot.settings)

    async def make_copy(self) -> backup.Backup:
        """Make a copy and tell the owner's log channel about it."""
        bot = self.bot
        made = await backup.create_backup(bot.db, bot.store.data_dir, self.folder, debug=bot.debug,
                                          keep=bot.settings.backup_keep)
        # A copy with far fewer rows than the one before: the owner should look at it today.
        await bot.log_to_owner(backup.describe(made), ping=bool(made.shrunk))
        return made

    @commands.command(name="kopia", aliases=["backup"], brief="Makes a copy of the database now.")
    @guards.owner_or_admin()
    async def copy_now(self, ctx: commands.Context) -> None:
        """Make a copy of the database and of the state files right now."""
        bot = self.bot
        age = backup.newest_age(self.folder)
        if age is not None and age < MANUAL_GAP and bot.settings.backup_keep:
            await io.send(ctx.channel, MSG_JUST_MADE.format(
                minutes=int(age // 60), when=ui.when(MANUAL_GAP - age), keep=bot.settings.backup_keep))
            return
        await io.send(ctx.channel, MSG_STARTED)
        try:
            made = await backup.create_backup(bot.db, bot.store.data_dir, self.folder, debug=bot.debug,
                                              keep=bot.settings.backup_keep)
        except backup.BackupInProgress:
            await io.send(ctx.channel, MSG_RUNNING)
            return
        except Exception as exc:  # noqa: BLE001 - whatever it was, the administrator must hear it
            _keep_cancellation(exc)
            log.exception("$kopia failed.")
            await io.send(ctx.channel, MSG_FAILED_HERE.format(kind=type(exc).__name__))
            await bot.log_to_owner(MSG_FAILED.format(error=f"{type(exc).__name__}: {exc}"))
            return
        await io.send(ctx.channel, backup.describe(made))
        if made.shrunk:
            # Not only whoever typed the command: the next copy will compare with this one
            # and stay silent, so this is the owner's only warning.
            await bot.log_to_owner(backup.describe(made), ping=True)


async def setup(bot) -> None:
    await bot.add_cog(BackupCog(bot))
