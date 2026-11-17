"""Dramatic pauses and waiting times of the gameplay, in one adjustable place.

Game flows are full of ``sleep(6)`` ("let the players read this") and
``timeout=600`` ("wait ten minutes for volunteers").  Routing them through
:func:`pause` and :func:`timeout` lets the test-suite skip the pauses and shrink
the waits, so whole fights and events can be simulated in milliseconds.

Production code must never change the scales.
"""

from __future__ import annotations

import asyncio

#: Multiplier for :func:`pause` (tests set it to 0).
PAUSE_SCALE = 1.0
#: Multiplier for :func:`timeout` (tests may shrink it).
TIMEOUT_SCALE = 1.0


async def pause(seconds: float) -> None:
    """Sleep for a gameplay pause (``await pause(6)`` instead of ``asyncio.sleep(6)``)."""
    await asyncio.sleep(max(0.0, seconds * PAUSE_SCALE))


def timeout(seconds: float) -> float:
    """A waiting time for ``wait_for`` / ``challenge.ask`` (scaled in tests)."""
    return seconds * TIMEOUT_SCALE
