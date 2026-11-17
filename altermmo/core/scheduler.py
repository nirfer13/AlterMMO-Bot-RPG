"""Wall-clock jobs (weekly ranking reset, daily cooldown reset, tournament...).

The legacy bot polled ``strftime("%H:%M") == "15:00"`` every 35 seconds.  If the
bot (or the internet) happened to be down in that one minute the job was simply
skipped - e.g. cooldowns were never cleared and "hunting stopped working" - and
after a reconnect duplicated loops ran the weekly reset twice.

A :class:`Job` knows its schedule as real datetimes, remembers on disk when it
last ran, catches up after downtime and can never run twice for one occurrence.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Protocol

from . import clock

log = logging.getLogger(__name__)


class _Memory(Protocol):
    def runtime_get(self, key: str, default=None): ...
    def runtime_set(self, key: str, value) -> None: ...


@dataclass
class Job:
    """One recurring job.

    ``previous(now)`` / ``following(now)`` return the latest occurrence at or
    before ``now`` and the first one after it (aware datetimes).

    ``catch_up_within`` - how late a missed occurrence may still be executed
    after downtime.  ``None`` = always catch up (resets), ``timedelta(0)`` =
    never (a tournament that was missed is not held at 3 a.m.).
    """

    name: str
    previous: Callable[[datetime], datetime]
    following: Callable[[datetime], datetime]
    run: Callable[[], Awaitable[None]]
    catch_up_within: timedelta | None = None

    @property
    def key(self) -> str:
        return f"job_last_run:{self.name}"


def _load_last_run(memory: _Memory, job: Job) -> datetime | None:
    raw = memory.runtime_get(job.key)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        log.warning("Ignoring unreadable last-run marker for job %s: %r", job.name, raw)
        return None


def _save_last_run(memory: _Memory, job: Job, moment: datetime) -> None:
    try:
        memory.runtime_set(job.key, moment.isoformat())
    except OSError:
        log.exception("Could not persist the last-run marker of job %s.", job.name)


def pending_occurrence(job: Job, last_run: datetime | None, now: datetime) -> datetime | None:
    """The occurrence that should run right now, or None when nothing is due."""
    if last_run is None:
        return None
    due = job.previous(now)
    if due <= last_run:
        return None
    if job.catch_up_within is not None and now - due > job.catch_up_within:
        return None
    return due


async def run_job(job: Job, memory: _Memory, *, max_sleep: float = 300.0) -> None:
    """Run ``job`` forever (meant to be started under the supervisor).

    A failing run is logged and retried on the next tick-and marked as done only
    after it succeeded, so an outage at the scheduled minute delays the job
    instead of cancelling it.
    """
    last_run = _load_last_run(memory, job)
    if last_run is None:
        # First start ever: assume the previous occurrence was handled (by the
        # legacy bot) and only run future ones.
        last_run = clock.now()
        _save_last_run(memory, job, last_run)

    failures = 0
    while True:
        now = clock.now()
        due = pending_occurrence(job, last_run, now)
        if due is not None:
            log.info("Running scheduled job %s (occurrence %s).", job.name, due.isoformat())
            try:
                await job.run()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                failures += 1
                delay = min(60.0 * failures, 900.0)
                log.exception("Scheduled job %s failed (attempt %d); retrying in %.0fs.",
                              job.name, failures, delay)
                await asyncio.sleep(delay)
                continue
            failures = 0
            last_run = clock.now()
            _save_last_run(memory, job, last_run)
            log.info("Scheduled job %s finished.", job.name)
            continue

        if due is None and last_run < job.previous(now):
            # A missed occurrence that is too old to catch up: skip it for good.
            log.warning("Scheduled job %s missed its occurrence at %s; skipping.",
                        job.name, job.previous(now).isoformat())
            last_run = now
            _save_last_run(memory, job, last_run)

        wait = (job.following(now) - now).total_seconds()
        # Sleep in slices: robust against clock changes and DST.
        await asyncio.sleep(max(0.5, min(wait, max_sleep)))


def weekly(name: str, weekday: int, hour: int, minute: int, run: Callable[[], Awaitable[None]],
           *, catch_up_within: timedelta | None = None) -> Job:
    """Job running every ``weekday`` (Monday=0) at HH:MM Warsaw time."""
    return Job(
        name=name,
        previous=lambda now: clock.previous_weekly(weekday, hour, minute, before=now),
        following=lambda now: clock.next_weekly(weekday, hour, minute, after=now),
        run=run,
        catch_up_within=catch_up_within,
    )


def daily(name: str, hour: int, minute: int, run: Callable[[], Awaitable[None]],
          *, catch_up_within: timedelta | None = None) -> Job:
    """Job running every day at HH:MM Warsaw time."""
    return Job(
        name=name,
        previous=lambda now: clock.previous_daily(hour, minute, before=now),
        following=lambda now: clock.next_daily(hour, minute, after=now),
        run=run,
        catch_up_within=catch_up_within,
    )
