"""In-memory game state.

Replaces the legacy module-level globals ``BOSSALIVE``, ``BOSSRARITY``, ``BUSY``,
``EVENT_ALIVE``, ``EVENT_TYPE``, ``is_player_boss`` and ``player_boss``.

The legacy flags were set and cleared by hand around long coroutines.  Whenever
one of those coroutines raised (a Discord error, a database hiccup, a member who
left the server) the flag stayed set forever: ``BUSY == 1`` muted nearly every
command and every random event, ``BOSSALIVE == 5`` meant no boss would ever
spawn again.  Here the "busy" flag and the "event running" flag can only be held
through context managers, so they are always released, and the boss phase is
changed through explicit transitions that wake up the spawn loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator

log = logging.getLogger(__name__)


class BossPhase(enum.IntEnum):
    """Life cycle of the big boss (values match the legacy ``BOSSALIVE`` numbers)."""

    IDLE = 0        # nothing scheduled; the spawn loop will schedule a respawn
    SCHEDULED = 1   # respawn time drawn and stored in the database
    WAITING = 2     # channel cleared, "silence" message sent, waiting for the respawn time
    PRE_SPAWN = 3   # "the wind grows stronger" - boss appears in a few minutes
    VISIBLE = 4     # boss is on the channel, waiting for $zaatakuj
    ENGAGED = 5     # somebody attacked; gathering players / choosing the fighter
    FIGHTING = 6    # the fight itself


class EventType(enum.Enum):
    NONE = 0
    SHRINE = 1
    CHEST = 2
    INVASION = 3
    PARTY = 4
    MEMORY = 5
    HUNTING = 6
    HAZARD = 7
    DUEL = 8
    RITUAL = 9
    PVPTOURNAMENT = 10
    TRAINING = 11
    MERCHANT = 12


class Busy(RuntimeError):
    """Raised when an exclusive activity is requested while another one runs."""


@dataclass
class BossInfo:
    """What is currently standing on the boss channel."""

    rarity: int = 0
    is_player_boss: bool = False
    #: ``discord.Member`` when a player/supporter poses as the boss, otherwise None.
    player_boss: Any = None


class GameState:
    """Single source of truth for "what is happening on the boss channel"."""

    def __init__(self) -> None:
        self._boss_phase = BossPhase.IDLE
        self._boss_changed = asyncio.Condition()
        self.boss = BossInfo()

        self._busy_owner: str | None = None
        self._busy_since: float | None = None
        #: The busy owner during whose hold the stable stays usable (see :meth:`stable_stays_open`).
        self._stable_open_for: str | None = None

        self._event_type = EventType.NONE
        self._event_alive = False

    # -- boss phase -------------------------------------------------------------

    @property
    def boss_phase(self) -> BossPhase:
        return self._boss_phase

    @property
    def boss_rarity(self) -> int:
        return self.boss.rarity

    @boss_rarity.setter
    def boss_rarity(self, value: int) -> None:
        self.boss.rarity = int(value)

    async def set_boss_phase(self, phase: BossPhase) -> None:
        """Change the boss phase and wake everybody waiting on it."""
        phase = BossPhase(phase)
        if phase != self._boss_phase:
            log.info("Boss phase: %s -> %s", self._boss_phase.name, phase.name)
        async with self._boss_changed:
            self._boss_phase = phase
            self._boss_changed.notify_all()

    def try_engage_boss(self) -> bool:
        """Atomically claim a visible boss for a fight (VISIBLE -> ENGAGED).

        Returns False when there is no boss to attack or somebody was faster.
        Synchronous on purpose: there is no ``await`` between the check and the
        change, so two ``$zaatakuj`` commands can never both win.
        """
        if self._boss_phase is not BossPhase.VISIBLE:
            return False
        log.info("Boss phase: VISIBLE -> ENGAGED")
        self._boss_phase = BossPhase.ENGAGED
        return True

    async def wait_boss_phase(self, *phases: BossPhase, timeout: float | None = None) -> bool:
        """Wait until the boss is in one of ``phases``; False on timeout."""
        wanted = set(phases)

        async def _wait() -> None:
            async with self._boss_changed:
                await self._boss_changed.wait_for(lambda: self._boss_phase in wanted)

        try:
            await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def sleep_while_boss_phase(self, phase: BossPhase, seconds: float) -> bool:
        """Sleep up to ``seconds`` while the boss stays in ``phase``.

        Returns True when the full time passed with the phase unchanged, False
        as soon as somebody changed the phase (e.g. a boss was summoned).
        """
        if self._boss_phase is not phase:
            return False
        if seconds <= 0:
            return True

        async def _wait() -> None:
            async with self._boss_changed:
                await self._boss_changed.wait_for(lambda: self._boss_phase is not phase)

        try:
            await asyncio.wait_for(_wait(), seconds)
        except asyncio.TimeoutError:
            return True
        return False

    @property
    def boss_blocks_activities(self) -> bool:
        """True from the "wind" announcement until the fight is over.

        Legacy rule ``BOSSALIVE in [0, 1, 2]`` => activities (events, hunting,
        tower) are allowed; from phase 3 upwards the channel belongs to the boss.
        """
        return self._boss_phase >= BossPhase.PRE_SPAWN

    # -- busy flag --------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """True while an exclusive activity (hunt, tower, event, spawn...) runs."""
        return self._busy_owner is not None

    @property
    def busy_owner(self) -> str | None:
        return self._busy_owner

    @property
    def activity_running(self) -> bool:
        """True while a hunt, a tower run or an event plays on the channel.

        Narrower than :attr:`busy`: a skill gem or a pet menu also hold the busy
        flag (for minutes), but they are not a fight.  Owner's rule: the boss,
        the chest and the shrine wait while this is true.
        """
        owner = self._busy_owner
        return owner is not None and (owner in ("hunt", "tower") or owner.startswith("event:"))

    @contextlib.contextmanager
    def hold_busy(self, owner: str) -> Iterator[None]:
        """Mark the channel as busy for the duration of the ``with`` block.

        Raises :class:`Busy` when something else already holds the flag, so
        check ``state.busy`` first when the command should be silently ignored.
        The flag is released even when the block raises or is cancelled.
        """
        if self._busy_owner is not None:
            raise Busy(f"busy with {self._busy_owner!r}, cannot start {owner!r}")
        self._busy_owner = owner
        self._busy_since = time.monotonic()
        log.debug("Busy: acquired by %s", owner)
        try:
            yield
        finally:
            held = time.monotonic() - (self._busy_since or time.monotonic())
            log.debug("Busy: released by %s after %.1fs", owner, held)
            self._busy_owner = None
            self._busy_since = None

    def force_release_busy(self) -> str | None:
        """Admin escape hatch (``$ResetBoss``); returns the previous owner."""
        owner, self._busy_owner, self._busy_since = self._busy_owner, None, None
        return owner

    @property
    def stable_open(self) -> bool:
        """True while the stable may be used although the channel is busy.

        Only during the sign-up of the "Trening towarzyszy" event (owner's
        rule: players choose there which companion they send).  Asked by the
        busy checks of exactly the stable's commands and buttons
        (``guards.unless_busy(stable=True)``, ``ui.gate(stable=True)``);
        everything else stays blocked as during any event.  The exception is
        tied to the busy holder it was made for: should that one lose the
        channel (``$ResetBoss``) and a hunt take it, the stable is blocked
        again at once.
        """
        return self._stable_open_for is not None and self._stable_open_for == self._busy_owner

    @contextlib.contextmanager
    def stable_stays_open(self) -> Iterator[None]:
        """Keep the stable usable for the ``with`` block although the caller holds the busy flag.

        The exception ends with the block, whatever happens in it.  Without a
        busy holder nothing changes (the stable works anyway).
        """
        self._stable_open_for = self._busy_owner
        try:
            yield
        finally:
            self._stable_open_for = None

    async def wait_until_idle(self, *, timeout: float = 1800.0, poll: float = 5.0) -> bool:
        """Wait until no exclusive activity and no event runs; False on timeout.

        Legacy ``wait_until_not_busy``: the boss spawn sequence waits (at most
        30 minutes) for a running event/hunt to finish before touching the channel.
        """
        deadline = time.monotonic() + timeout
        while self.busy or self._event_alive:
            if time.monotonic() >= deadline:
                log.warning("wait_until_idle timed out (busy=%s, event=%s)",
                            self._busy_owner, self._event_type.name)
                return False
            await asyncio.sleep(poll)
        return True

    # -- random events ----------------------------------------------------------

    @property
    def event_alive(self) -> bool:
        return self._event_alive

    @property
    def event_type(self) -> EventType:
        return self._event_type

    def open_event(self, event_type: EventType) -> None:
        """Mark a passive event (shrine, chest) as waiting for a player command."""
        self._event_type = event_type
        self._event_alive = True

    def close_event(self) -> None:
        self._event_type = EventType.NONE
        self._event_alive = False

    def try_claim_event(self, event_type: EventType) -> bool:
        """Atomically take an open passive event (first ``$skrzynia`` wins)."""
        if not self._event_alive or self._event_type is not event_type:
            return False
        self._event_alive = False
        return True

    def reopen_event(self, event_type: EventType) -> None:
        """Give a claimed passive event back (e.g. a non-supporter tried to pray)."""
        if self._event_type is event_type:
            self._event_alive = True

    @contextlib.contextmanager
    def running_event(self, event_type: EventType, *, exclusive: bool = True) -> Iterator[None]:
        """Run an interactive event; flags are cleared when the block ends.

        ``exclusive=True`` also holds the busy flag (invasion, party, memory...).
        """
        self._event_type = event_type
        self._event_alive = True
        try:
            if exclusive:
                with self.hold_busy(f"event:{event_type.name}"):
                    yield
            else:
                yield
        finally:
            self._event_alive = False
            self._event_type = EventType.NONE

    @property
    def free_for_activity(self) -> bool:
        """Legacy rule: ``EVENT_ALIVE == 0 and BUSY == 0 and BOSSALIVE in [0, 1, 2]``.

        A shrine or chest that a player has just claimed (``$modlitwa`` /
        ``$skrzynia`` still handing out the reward) is not "free" yet: its type
        stays set until the command closes the event.
        """
        return (not self._event_alive and self._event_type is EventType.NONE
                and not self.busy and not self.boss_blocks_activities)

    # -- diagnostics ------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        return {
            "boss_phase": self._boss_phase.name,
            "boss_rarity": self.boss.rarity,
            "is_player_boss": self.boss.is_player_boss,
            "busy_owner": self._busy_owner,
            "busy_for_s": round(time.monotonic() - self._busy_since) if self._busy_since else 0,
            "stable_open": self.stable_open,
            "event_type": self._event_type.name,
            "event_alive": self._event_alive,
        }
