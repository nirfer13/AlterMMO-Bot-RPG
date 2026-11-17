"""Wall-clock jobs: no double runs, catch-up after downtime, retry after failure."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from altermmo.core import clock, scheduler


class Memory:
    def __init__(self):
        self.data = {}

    def runtime_get(self, key, default=None):
        return self.data.get(key, default)

    def runtime_set(self, key, value):
        self.data[key] = value


def _weekly(run=None, **kwargs):
    async def noop():
        return None

    return scheduler.weekly("ranking", 0, 15, 0, run or noop, **kwargs)


def test_nothing_is_due_on_first_start():
    assert scheduler.pending_occurrence(_weekly(), None, clock.now()) is None


def test_occurrence_missed_during_downtime_is_caught_up():
    job = _weekly()
    last_run = datetime(2026, 11, 16, 14, 0, tzinfo=clock.WARSAW)   # Monday 14:00
    now = datetime(2026, 11, 16, 17, 30, tzinfo=clock.WARSAW)       # bot back at 17:30
    assert scheduler.pending_occurrence(job, last_run, now) == datetime(2026, 11, 16, 15, 0, tzinfo=clock.WARSAW)


def test_occurrence_never_runs_twice():
    job = _weekly()
    last_run = datetime(2026, 11, 16, 15, 0, 5, tzinfo=clock.WARSAW)
    for hours in (0, 1, 24, 100):
        now = last_run + timedelta(hours=hours, seconds=1)
        assert scheduler.pending_occurrence(job, last_run, now) is None
    assert scheduler.pending_occurrence(job, last_run, last_run + timedelta(days=7)) is not None


def test_stale_occurrence_is_skipped_when_catch_up_window_passed():
    job = _weekly(catch_up_within=timedelta(hours=2))
    last_run = datetime(2026, 11, 14, 12, 0, tzinfo=clock.WARSAW)
    assert scheduler.pending_occurrence(job, last_run, datetime(2026, 11, 16, 16, 0, tzinfo=clock.WARSAW)) is not None
    assert scheduler.pending_occurrence(job, last_run, datetime(2026, 11, 16, 18, 0, tzinfo=clock.WARSAW)) is None


async def test_run_job_retries_until_success_and_persists(monkeypatch):
    memory = Memory()
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("discord down")

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(scheduler.asyncio, "sleep", fast_sleep)
    job = scheduler.Job(
        name="t",
        previous=lambda now: now - timedelta(seconds=1),      # always "just became due"
        following=lambda now: now + timedelta(hours=1),
        run=flaky,
    )
    memory.runtime_set(job.key, (clock.now() - timedelta(hours=1)).isoformat())
    task = asyncio.ensure_future(scheduler.run_job(job, memory))
    for _ in range(200):
        if len(calls) >= 2:
            break
        await real_sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2
    assert datetime.fromisoformat(memory.data[job.key]) > clock.now() - timedelta(minutes=1)


async def test_run_job_first_start_only_records_marker(monkeypatch):
    memory = Memory()
    ran = []

    async def run():
        ran.append(1)

    job = _weekly(run)
    task = asyncio.ensure_future(scheduler.run_job(job, memory))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ran == []
    assert job.key in memory.data
