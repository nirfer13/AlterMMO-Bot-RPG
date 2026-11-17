"""Supervised background tasks.

The legacy bot started its loops with ``bot.loop.create_task`` and never looked
at them again: one unhandled exception (a Discord 5xx, a dropped database
connection, a ``None`` channel during an outage) killed the boss or event loop
silently and for good.  Here every loop runs under a supervisor that logs the
traceback and restarts it with exponential backoff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

TaskFactory = Callable[[], Awaitable[None]]


@dataclass
class _Entry:
    name: str
    factory: TaskFactory
    task: asyncio.Task | None = None
    restarts: int = 0
    last_error: str | None = None
    started_at: float = field(default_factory=time.monotonic)


class Supervisor:
    """Keeps named background coroutines alive."""

    def __init__(self, *, min_backoff: float = 5.0, max_backoff: float = 300.0,
                 healthy_after: float = 600.0) -> None:
        self._entries: dict[str, _Entry] = {}
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._healthy_after = healthy_after
        #: ``await on_crash(name, error_text, crashes_in_a_row)`` - set by the bot
        #: so the owner hears about a loop that keeps failing (first crash and
        #: then every tenth), instead of finding out from a silent channel.
        self.on_crash: Callable[[str, str, int], Awaitable[None]] | None = None

    def start(self, name: str, factory: TaskFactory) -> None:
        """Start (or restart) the supervised task ``name``.

        ``factory`` is called every time the task has to be (re)started and must
        return a fresh coroutine.
        """
        self.stop(name)
        entry = _Entry(name=name, factory=factory)
        self._entries[name] = entry
        entry.task = asyncio.create_task(self._run(entry), name=f"supervised:{name}")

    def stop(self, name: str) -> bool:
        """Cancel the supervised task; returns True when something was running."""
        entry = self._entries.pop(name, None)
        if entry is None or entry.task is None:
            return False
        entry.task.cancel()
        return True

    def restart(self, name: str) -> bool:
        """Restart a known task with its original factory."""
        entry = self._entries.get(name)
        if entry is None:
            return False
        self.start(name, entry.factory)
        return True

    def is_running(self, name: str) -> bool:
        entry = self._entries.get(name)
        return bool(entry and entry.task and not entry.task.done())

    def names(self) -> list[str]:
        return list(self._entries)

    def status(self) -> dict[str, dict[str, object]]:
        """Snapshot used by the admin diagnostics command."""
        report: dict[str, dict[str, object]] = {}
        for name, entry in self._entries.items():
            report[name] = {
                "running": bool(entry.task and not entry.task.done()),
                "restarts": entry.restarts,
                "last_error": entry.last_error,
                "uptime_s": round(time.monotonic() - entry.started_at),
            }
        return report

    async def stop_all(self) -> None:
        tasks = [e.task for e in self._entries.values() if e.task is not None]
        self._entries.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, entry: _Entry) -> None:
        delay = self._min_backoff
        crashes_in_a_row = 0
        while True:
            entry.started_at = time.monotonic()
            crashed = False
            try:
                await entry.factory()
                log.warning("Background task %r returned; restarting it.", entry.name)
                entry.last_error = "returned"
            except asyncio.CancelledError:
                log.info("Background task %r cancelled.", entry.name)
                raise
            except Exception as exc:  # noqa: BLE001 - the whole point is to survive anything
                crashed = True
                entry.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Background task %r crashed; restarting in %.0fs.", entry.name, delay)

            entry.restarts += 1
            if time.monotonic() - entry.started_at >= self._healthy_after:
                delay = self._min_backoff
                crashes_in_a_row = 0
            if crashed:
                crashes_in_a_row += 1
                if self.on_crash is not None and crashes_in_a_row % 10 == 1:
                    try:
                        await self.on_crash(entry.name, entry.last_error or "", crashes_in_a_row)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - reporting must never stop the restart
                        log.exception("Could not report the crash of %r.", entry.name)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_backoff)
