"""Time helpers.

The legacy bot faked the Polish timezone with ``utcnow() + 2h`` which is wrong
for half of the year (CET is UTC+1).  Everything in v2 uses real, timezone-aware
datetimes in ``Europe/Warsaw``.

The only place where the legacy convention survives is the database: the
``BOSS.RESPAWN_TIME`` / ``HISTORY.FIGHT_TIME`` columns hold *naive* timestamps
written as ``utcnow() + 2h``.  ``to_legacy`` / ``from_legacy`` convert between
that convention and aware datetimes, so both bot versions can share one database.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
UTC = timezone.utc

_LEGACY_OFFSET = timedelta(hours=2)


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def now() -> datetime:
    """Current time as an aware Europe/Warsaw datetime."""
    return utcnow().astimezone(WARSAW)


def monotonic() -> float:
    """Monotonic seconds; use for measuring durations and timeouts."""
    return time.monotonic()


def to_legacy(moment: datetime) -> datetime:
    """Aware datetime -> naive "UTC+2" timestamp as stored by the legacy bot."""
    if moment.tzinfo is None:
        raise ValueError("to_legacy() needs an aware datetime")
    return (moment.astimezone(UTC) + _LEGACY_OFFSET).replace(tzinfo=None)


def from_legacy(stamp: datetime) -> datetime:
    """Naive legacy "UTC+2" timestamp -> aware UTC datetime."""
    if stamp.tzinfo is not None:
        return stamp.astimezone(UTC)
    return (stamp - _LEGACY_OFFSET).replace(tzinfo=UTC)


def legacy_now() -> datetime:
    """Current time in the legacy naive "UTC+2" convention (DB compatibility only)."""
    return to_legacy(utcnow())


def seconds_until(moment: datetime) -> float:
    """Seconds from now until ``moment`` (negative when it is in the past)."""
    return (moment - utcnow()).total_seconds()


def next_daily(hour: int, minute: int = 0, *, after: datetime | None = None) -> datetime:
    """Next occurrence (strictly after ``after``) of HH:MM Warsaw time."""
    base = (after or now()).astimezone(WARSAW)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= base:
        candidate = (candidate + timedelta(days=1)).replace(hour=hour, minute=minute)
    return _normalize(candidate)


def next_weekly(
    weekday: int, hour: int, minute: int = 0, *, after: datetime | None = None
) -> datetime:
    """Next occurrence (strictly after ``after``) of weekday HH:MM Warsaw time.

    ``weekday`` follows ``datetime.weekday()``: Monday is 0, Sunday is 6.
    """
    base = (after or now()).astimezone(WARSAW)
    days_ahead = (weekday - base.weekday()) % 7
    candidate = (base + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate <= base:
        candidate = (candidate + timedelta(days=7)).replace(hour=hour, minute=minute)
    return _normalize(candidate)


def previous_daily(hour: int, minute: int = 0, *, before: datetime | None = None) -> datetime:
    """Most recent occurrence (at or before ``before``) of HH:MM Warsaw time."""
    base = (before or now()).astimezone(WARSAW)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > base:
        candidate = (candidate - timedelta(days=1)).replace(hour=hour, minute=minute)
    return _normalize(candidate)


def previous_weekly(
    weekday: int, hour: int, minute: int = 0, *, before: datetime | None = None
) -> datetime:
    """Most recent occurrence (at or before ``before``) of weekday HH:MM Warsaw time."""
    base = (before or now()).astimezone(WARSAW)
    days_back = (base.weekday() - weekday) % 7
    candidate = (base - timedelta(days=days_back)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate > base:
        candidate = (candidate - timedelta(days=7)).replace(hour=hour, minute=minute)
    return _normalize(candidate)


def _normalize(moment: datetime) -> datetime:
    """Re-resolve the UTC offset after wall-clock arithmetic (DST safe)."""
    return moment.replace(tzinfo=None).replace(tzinfo=WARSAW)


def is_weekend(moment: datetime | None = None) -> bool:
    """Saturday or Sunday in Warsaw."""
    return (moment or now()).astimezone(WARSAW).weekday() >= 5
