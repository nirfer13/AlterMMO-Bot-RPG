"""Gateway connection health tracking.

While the gateway is disconnected Discord does not deliver ``MESSAGE_CREATE``
events, and messages typed during the outage are never replayed.  A fight that
waits for a player's answer would therefore punish the player for the bot's own
connection drop.  ``ConnectionMonitor`` lets game flows ask "was the link broken
while I was waiting?" and wait for the link to come back.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class ConnectionMonitor:
    """Tracks gateway connects/disconnects via discord.py events."""

    def __init__(self) -> None:
        self._connected = asyncio.Event()
        self._last_disconnect: float | None = None
        self._last_connect: float | None = None
        self._disconnects = 0

    # -- wiring -----------------------------------------------------------------

    def attach(self, bot) -> None:
        """Register the listeners on a ``discord.Client``."""
        bot.add_listener(self._on_up, "on_connect")
        bot.add_listener(self._on_up, "on_resumed")
        bot.add_listener(self._on_up, "on_ready")
        bot.add_listener(self._on_down, "on_disconnect")

    async def _on_up(self) -> None:
        self.mark_connected()

    async def _on_down(self) -> None:
        self.mark_disconnected()

    # -- state changes (public for tests) ---------------------------------------

    def mark_connected(self) -> None:
        if not self._connected.is_set():
            outage = None
            if self._last_disconnect is not None:
                outage = time.monotonic() - self._last_disconnect
            if outage is not None:
                log.warning("Gateway connection restored after %.1fs.", outage)
            else:
                log.info("Gateway connected.")
        self._last_connect = time.monotonic()
        self._connected.set()

    def mark_disconnected(self) -> None:
        if self._connected.is_set():
            self._disconnects += 1
            log.warning("Gateway connection lost (disconnect #%d).", self._disconnects)
        self._last_disconnect = time.monotonic()
        self._connected.clear()

    # -- queries ----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def disconnect_count(self) -> int:
        return self._disconnects

    def disrupted_since(self, since: float) -> bool:
        """True when the link is down now or dropped/reconnected after ``since``.

        ``since`` is a ``time.monotonic()`` timestamp.
        """
        if not self._connected.is_set():
            return True
        if self._last_disconnect is not None and self._last_disconnect >= since:
            return True
        return False

    async def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait until the gateway is connected; False when ``timeout`` elapsed."""
        if self._connected.is_set():
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True
