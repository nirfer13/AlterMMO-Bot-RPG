"""The fight journal: what happened in every typed fight, round by round.

Why it exists: a player lost a tower floor on a word with a 1.6 s limit and
the owner asked how late her answer was.  The bot could not tell - a round
that times out only knew "nobody answered in time", an answer that arrived a
moment later was mentioned at DEBUG only (off in production), and one that
arrived after the bot had stopped listening was never seen.

What is kept (in memory only, see the bounds below):

* every typed fight - a boss, a duel, a legendary boss fought by a party, a
  hunt, a floor of the tower: :class:`Fight` with its :class:`Round` s (who
  was asked, the word, the limit, what came back and when, by Discord's own
  timestamps, the bot's latency, whether the connection was disturbed);
* **the answer after the time is up**: when a round ends as a timeout the
  journal keeps listening for the first message of the asked player for
  :data:`LATE_WATCH` s (:func:`heard`), or until that player is asked the
  next word, and records by how much it missed - or that nothing came;
* how the fight ended, and - for the owner who reads ``logs/altermmo.log`` -
  one INFO line per lost fight plus one per late answer;
* **when every answer reached Discord and the bot** (:class:`Arrival`,
  owner, 2026-11-26: "Zrób 2 i zapisuj dane z 1 testowo"): the time the
  player's own app put into the message's ``nonce`` when Enter was pressed
  (the official apps use a snowflake of their clock), Discord's timestamp and
  the moment the bot got it.  Nonce -> Discord is the trip from the player's
  device to Discord plus the constant offset of that device's clock, so only
  its excess over the same player's best says something (a lag on the way
  up); nothing decides by it yet - it is collected to be analysed;
* **the archive** (:func:`configure_archive`): every fight, once it ended
  and its late answers are in, is written as one JSON line to
  ``logs/fights/fights-YYYY-MM-DD.jsonl`` by a background thread (a fight
  never waits for the disk), kept :data:`ARCHIVE_KEEP_DAYS` days -
  ``tools/fight_lag_report.py`` reads it.

Rules the code keeps:

* **recording never raises into a fight and never awaits**: every function
  here is synchronous and swallows its own errors (:func:`_never_raises`);
* **no listener, no task per watch**: the bot hands every message to
  :func:`heard` (a dictionary look-up while nothing is watched) and a watch
  ends by a timer of the event loop, so nothing can leak, nothing is taken
  away from the next round or from the commands;
* **bounded**: the last fight of each player plus a ring of :data:`RING`
  fights, at most :data:`MAX_ROUNDS` rounds each, forgotten
  :data:`EXPIRE_AFTER` s after they ended.

The fights call :func:`begin` / :func:`lost` / :func:`won` / :func:`draw` /
:func:`pet_attack` / :func:`saved`; the round itself is recorded by
:func:`altermmo.core.challenge.ask` through :func:`open_round`; the shared
round helpers call :func:`interrupted`.  What players are told about a fight
is in :mod:`altermmo.features.fightreport`.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import json
import logging
import queue
import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Iterable, TypeVar

from ..settings import is_command_text

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

#: How long the journal listens for an answer after a round timed out (seconds).
LATE_WATCH = 20.0
#: A fight is forgotten this long after it ended (or, never ended, after it began).
EXPIRE_AFTER = 30 * 60.0
#: Fights kept besides the last one of each player.
RING = 20
#: Players whose last fight is remembered (the oldest go first).
MAX_PLAYERS = 200
#: Rounds kept per fight (the oldest go first; a duel has at most 40).
MAX_ROUNDS = 80
#: What a player typed / the prompt: only this many characters are kept.
TEXT_LIMIT = 60
PROMPT_LIMIT = 160

#: Days the archive files are kept (the owner analyses a week or more at a time).
ARCHIVE_KEEP_DAYS = 60
#: The archive file of one UTC day: ``fights-2026-11-26.jsonl``.
ARCHIVE_PREFIX, ARCHIVE_SUFFIX = "fights-", ".jsonl"
#: Version of the archive's records (a reader can tell old lines from new ones).
ARCHIVE_VERSION = 1
#: Discord's epoch (2015-01-01 UTC) in milliseconds: a snowflake holds the ms since then.
DISCORD_EPOCH_MS = 1_420_070_400_000
#: A nonce whose time is further than this from Discord's timestamp is not a time (seconds).
NONCE_SANE = 24 * 3600.0

#: Kinds of fights (:attr:`Fight.kind`); ``detail`` is the rarity or the floor.
KIND_BOSS = "boss"
KIND_GROUP = "group"
KIND_DUEL = "duel"
KIND_HUNT = "hunt"
KIND_TOWER = "tower"

#: Who saved a failed round (:attr:`Round.saved`).
SAVED_BY_PET = "pet"
SAVED_BY_SPARE = "spare"   # the party's spare chance against a legendary boss


class Outcome(enum.Enum):
    """How a round ended."""

    CORRECT = "correct"            # the asked player typed the word in time
    WRONG_WORD = "wrong word"      # the asked player typed something else in time
    WRONG_PLAYER = "wrong player"  # somebody else of the fight answered in time
    TIMEOUT = "timeout"            # nobody answered in time
    DISRUPTED = "disrupted"        # connection / event-loop trouble: repeated, not counted
    PET_ATTACK = "pet attack"      # the pet attacked instead: nothing was asked


#: Outcomes that cost the player something (unless the pet saves the round).
FAILED = (Outcome.TIMEOUT, Outcome.WRONG_WORD, Outcome.WRONG_PLAYER)


class LateState(enum.Enum):
    """What is known about an answer after the time of a timed-out round."""

    NONE = "none"                # not looked for (the round did not time out)
    WATCHING = "watching"        # still listening
    FOUND = "found"              # :attr:`Round.late` has it
    NOTHING = "nothing"          # no message within LATE_WATCH
    NEXT_PROMPT = "next prompt"  # the player was asked the next word first
    STOPPED = "stopped"          # the bot shut down while listening


class End(enum.Enum):
    WON = "won"
    LOST = "lost"
    DRAW = "draw"
    INTERRUPTED = "interrupted"  # technical reasons; nobody lost


@dataclass
class Arrival:
    """When a player's message was sent, created and received (see the module's docstring).

    ``nonce_at`` - the clock of the player's device when the message was sent
    (decoded from the nonce the app attached; None when there is none or it
    is not a time: other clients, bots, an app that changed its format).
    ``created_at`` - Discord's timestamp.  ``received_at`` - the bot's clock
    when the message was handled.  The two differences include the offsets of
    the clocks involved: compare each only with others of the same player.
    """

    message_id: int
    created_at: datetime | None
    received_at: datetime
    nonce: str | None = None
    nonce_at: datetime | None = None

    @property
    def upload(self) -> float | None:
        """Seconds from the player's Enter (by the device's clock) to Discord's timestamp."""
        if self.nonce_at is None or self.created_at is None:
            return None
        return (self.created_at - self.nonce_at).total_seconds()

    @property
    def delivery(self) -> float | None:
        """Seconds from Discord's timestamp to the bot handling the message (by the bot's clock)."""
        return None if self.created_at is None else (self.received_at - self.created_at).total_seconds()


def nonce_time(nonce: Any, created_at: datetime | None) -> datetime | None:
    """The moment a snowflake nonce stands for, if it is one close to ``created_at``.  Never raises."""
    try:
        if isinstance(nonce, bool) or nonce is None:
            return None
        text = str(nonce).strip()
        if not text.isdigit() or len(text) > 20:
            return None
        value = int(text)
        if value <= 0:
            return None
        moment = datetime.fromtimestamp(((value >> 22) + DISCORD_EPOCH_MS) / 1000, tz=timezone.utc)
        if created_at is not None and abs((created_at - moment).total_seconds()) > NONCE_SANE:
            return None
        return moment
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def arrival_of(message: Any, received_at: datetime | None = None) -> Arrival | None:
    """The :class:`Arrival` of ``message``, now.  Never raises (None when it cannot be told)."""
    try:
        received = received_at or datetime.now(timezone.utc)
        created = getattr(message, "created_at", None)
        created = created if isinstance(created, datetime) else None
        raw = getattr(message, "nonce", None)
        nonce = None if raw is None else clip(raw, 40)
        return Arrival(message_id=int(getattr(message, "id", 0) or 0), created_at=created,
                       received_at=received, nonce=nonce, nonce_at=nonce_time(raw, created))
    except Exception:  # noqa: BLE001 - a by-product, see _never_raises
        log.exception("Fight journal: reading the arrival of a message failed.")
        return None


@dataclass
class Late:
    """The first message of the asked player after the time of a round was up."""

    #: Seconds after the prompt at which Discord created it.
    after: float
    text: str
    #: True when it arrived while the round was still listening (inside the grace window).
    in_grace: bool = False
    arrival: Arrival | None = None


@dataclass
class Round:
    number: int
    asked_id: int | None
    asked_name: str
    word: str
    prompt: str = ""
    #: The time limit of the round in seconds.
    limit: float = 0.0
    #: None while the round is running.
    outcome: Outcome | None = None
    #: Reaction time by Discord's clock (prompt created -> answer created); None without an answer.
    elapsed: float | None = None
    typed: str | None = None
    typed_by_id: int | None = None
    typed_by_name: str = ""
    #: The bot's gateway latency when the prompt was sent (seconds).
    latency: float = 0.0
    #: How long the bot listened after the limit (seconds).
    grace: float = 0.0
    #: How long sending the prompt took (seconds).
    send_time: float = 0.0
    #: The gateway connection dropped during the round.
    disturbed: bool = False
    #: The event loop was late by this much when the listening ended (seconds).
    stall: float = 0.0
    #: :data:`SAVED_BY_PET` / :data:`SAVED_BY_SPARE` when the failed round cost nothing.
    saved: str | None = None
    late: Late | None = None
    late_state: LateState = LateState.NONE
    #: How long the journal listened for a late answer after the round (seconds).
    watched: float = 0.0
    #: Nothing was heard while listening, but the gateway connection dropped meanwhile:
    #: an answer written then would not have reached the bot.
    watch_disturbed: bool = False
    #: The typed word was the one of this earlier round (it arrived after that round's time).
    stale_of: int | None = None
    prompt_id: int = 0
    prompt_at: datetime | None = None
    #: The bot's clock when sending the prompt returned, minus Discord's timestamp of it (seconds).
    prompt_lag: float | None = None
    #: The arrival of the answer given in time (the late one is in :attr:`late`).
    arrival: Arrival | None = None

    @property
    def over(self) -> float | None:
        """By how many seconds the late answer missed the limit (None without one)."""
        return None if self.late is None else self.late.after - self.limit


@dataclass
class Fight:
    kind: str
    channel_id: int
    #: ``(player id, name)`` in the order the fight knows them.
    fighters: list[tuple[int, str]]
    detail: Any = None
    serial: int = 0
    started: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    rounds: list[Round] = field(default_factory=list)
    #: None while the fight runs (or when it broke without saying how it ended).
    end: End | None = None
    ended: float | None = None
    #: Ids of the players who lost it (a duel: the loser; otherwise everybody).
    losers: tuple[int, ...] = ()
    winners: tuple[int, ...] = ()
    #: The round that lost the fight (a duel: the loser's last failed round), if any.
    decisive: Round | None = None
    reason: str = ""
    #: A complaint about this fight was answered (:mod:`altermmo.features.fightreport`).
    answered: bool = False
    owner_told: bool = False
    #: Handed to the archive (:func:`_archive`); at most once.
    archived: bool = False

    def has(self, player_id: int) -> bool:
        return any(fighter_id == player_id for fighter_id, _ in self.fighters)

    def name_of(self, player_id: int | None) -> str:
        return next((name for fighter_id, name in self.fighters if fighter_id == player_id), str(player_id))

    @property
    def party(self) -> bool:
        return len(self.fighters) > 1

    def rounds_of(self, player_id: int) -> list[Round]:
        """The rounds in which ``player_id`` was asked a word."""
        return [entry for entry in self.rounds
                if entry.asked_id == player_id and entry.outcome not in (None, Outcome.PET_ATTACK)]

    def disrupted_rounds(self) -> list[Round]:
        return [entry for entry in self.rounds if entry.outcome is Outcome.DISRUPTED]


# -- state --------------------------------------------------------------------------------

#: channel id -> the fight running there.
_open: dict[int, Fight] = {}
#: player id -> the last fight he took part in.
_last: dict[int, Fight] = {}
_recent: deque[Fight] = deque(maxlen=RING)
_serial = 0


@dataclass(eq=False)
class _Watch:
    """Listening for the answer after the time of ``entry`` (see :func:`heard`)."""

    fight: Fight
    entry: Round
    keys: tuple[tuple[int, int], ...]
    started: float
    handle: asyncio.TimerHandle | None = None
    #: The bot's connection monitor (``bot.net``), asked at the end whether the link held.
    net: Any = None


#: ``(channel id, player id)`` -> the watch waiting for that player's next message.
_watches: dict[tuple[int, int], _Watch] = {}
#: Background tasks started through :func:`spawn` (cancelled by :func:`stop`).
_tasks: set[asyncio.Task] = set()


def _never_raises(default: Any = None) -> Callable[[F], F]:
    """The journal is a by-product: whatever goes wrong in it must not reach a fight."""

    def decorate(function: F) -> F:
        @functools.wraps(function)
        def safe(*args: Any, **kwargs: Any) -> Any:
            try:
                return function(*args, **kwargs)
            except Exception:  # noqa: BLE001 - see the docstring
                log.exception("Fight journal: %s failed (the fight is not affected).", function.__name__)
                return default
        return safe  # type: ignore[return-value]

    return decorate


def _ident(player: Any) -> tuple[int, str]:
    """``(id, name)`` of a member (or of a bare id)."""
    player_id = int(getattr(player, "id", player))
    return player_id, (str(player) if hasattr(player, "id") else str(player_id))


def clip(text: Any, limit: int = TEXT_LIMIT) -> str:
    """``text`` on one line, at most ``limit`` characters."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _prune(now: float | None = None) -> None:
    """Forget what is older than :data:`EXPIRE_AFTER`."""
    now = time.monotonic() if now is None else now

    def expired(fight: Fight) -> bool:
        return now - (fight.ended if fight.ended is not None else fight.started) > EXPIRE_AFTER

    if any(expired(fight) for fight in _recent):
        kept = [fight for fight in _recent if not expired(fight)]
        _recent.clear()
        _recent.extend(kept)
    for player_id in [player_id for player_id, fight in _last.items() if expired(fight)]:
        del _last[player_id]
    for channel_id in [channel_id for channel_id, fight in _open.items() if expired(fight)]:
        del _open[channel_id]


# -- the fight ------------------------------------------------------------------------------

@_never_raises()
def begin(channel: Any, kind: str, fighters: Iterable[Any], *, detail: Any = None) -> Fight | None:
    """A typed fight starts on ``channel`` now; its rounds are recorded until it says how it ended.

    A fight still open there (it raised instead of ending) is left as it is:
    without a result.
    """
    global _serial
    channel_id = int(channel.id)
    _prune()
    unended = _open.pop(channel_id, None)
    if unended is not None:
        _archive(unended)  # (as it is: without an end)
    _serial += 1
    fight = Fight(kind=kind, channel_id=channel_id, fighters=[_ident(fighter) for fighter in fighters],
                  detail=detail, serial=_serial)
    _open[channel_id] = fight
    _recent.append(fight)
    for player_id, _ in fight.fighters:
        _last.pop(player_id, None)  # (re-inserted: the dictionary keeps the oldest first)
        _last[player_id] = fight
    while len(_last) > MAX_PLAYERS:
        del _last[next(iter(_last))]
    return fight


def open_fight(channel: Any) -> Fight | None:
    """The fight being recorded on ``channel`` (None when there is none)."""
    channel_id = getattr(channel, "id", channel)
    return _open.get(int(channel_id)) if channel_id is not None else None


def _close(channel: Any, end: End, *, losers: Iterable[int] = (), winners: Iterable[int] = (),
           reason: str = "") -> Fight | None:
    fight = _open.pop(int(channel.id), None)
    if fight is None:
        return None
    fight.end = end
    fight.ended = time.monotonic()
    fight.losers = tuple(losers)
    fight.winners = tuple(winners)
    fight.reason = reason
    return fight


def _last_asked(fight: Fight) -> Round | None:
    """The newest round in which somebody was asked a word."""
    return next((entry for entry in reversed(fight.rounds) if entry.outcome is not Outcome.PET_ATTACK), None)


@_never_raises()
def lost(channel: Any, *, timed_out: bool | None = None) -> Fight | None:
    """The fight on ``channel`` is lost by all its fighters (its last asked round decided it).

    ``timed_out`` is what the fight itself says about that round; it is only
    compared with the journal (a difference is logged, the journal is kept).
    """
    fight = _close(channel, End.LOST)
    if fight is None:
        return None
    fight.losers = tuple(player_id for player_id, _ in fight.fighters)
    entry = _last_asked(fight)
    if entry is not None and entry.outcome in FAILED:
        fight.decisive = entry
        fight.reason = entry.outcome.value
        if timed_out is not None and timed_out != (entry.outcome is Outcome.TIMEOUT):
            log.warning("Fight journal: the fight says timed_out=%s, the journal has %r for round %s.",
                        timed_out, entry.outcome.value, entry.number)
    else:
        fight.reason = "timeout" if timed_out else "mistake" if timed_out is not None else ""
    log.info("%s", lost_line(fight))
    _settle(fight)
    return fight


@_never_raises()
def won(channel: Any, *, winner: Any = None, loser: Any = None) -> Fight | None:
    """The fight on ``channel`` is won - by everybody, or (a duel) by ``winner`` against ``loser``."""
    fight = _close(channel, End.WON)
    if fight is None:
        return None
    if loser is None:
        fight.winners = tuple(player_id for player_id, _ in fight.fighters)
        _settle(fight)
        return fight
    loser_id = _ident(loser)[0]
    fight.losers = (loser_id,)
    fight.winners = (_ident(winner)[0],) if winner is not None else ()
    # A duel is decided by the damage of many rounds; the loser's last failed one is the
    # nearest thing to "the round that lost it".
    fight.decisive = next((entry for entry in reversed(fight.rounds_of(loser_id))
                           if entry.outcome in (Outcome.TIMEOUT, Outcome.WRONG_WORD)), None)
    fight.reason = "duel"
    log.info("%s", lost_line(fight))
    _settle(fight)
    return fight


@_never_raises()
def draw(channel: Any) -> Fight | None:
    """The fight on ``channel`` ended without a winner or a loser."""
    fight = _close(channel, End.DRAW)
    if fight is not None:
        _settle(fight)
    return fight


@_never_raises()
def interrupted(channel: Any, reason: str = "") -> Fight | None:
    """The fight on ``channel`` was given up for technical reasons: nobody lost it."""
    fight = _close(channel, End.INTERRUPTED, reason=reason)
    if fight is not None:
        log.info("Fight interrupted: %s | %s | %s", kind_text(fight), fighters_text(fight),
                 reason or "technical reasons")
        _settle(fight)
    return fight


@_never_raises()
def pet_attack(channel: Any, turn: int | None, asked: Any, word: str = "") -> None:
    """Round ``turn`` was not asked: the pet of ``asked`` attacked instead."""
    fight = open_fight(channel)
    if fight is None:
        return
    asked_id, asked_name = _ident(asked) if asked is not None else (None, "")
    _append(fight, Round(number=int(turn) if turn is not None else len(fight.rounds) + 1,
                         asked_id=asked_id, asked_name=asked_name, word=str(word or ""),
                         outcome=Outcome.PET_ATTACK))


@_never_raises()
def saved(channel: Any, by: str = SAVED_BY_PET) -> None:
    """The failed round just played on ``channel`` cost nothing (the pet protected, a spare chance)."""
    fight = open_fight(channel)
    entry = _last_asked(fight) if fight is not None else None
    if entry is not None and entry.outcome in FAILED:
        entry.saved = by


def _append(fight: Fight, entry: Round) -> None:
    fight.rounds.append(entry)
    if len(fight.rounds) > MAX_ROUNDS:
        del fight.rounds[0]


# -- one round (called by challenge.ask) ----------------------------------------------------

class _NoRound:
    """What :func:`open_round` returns when nothing is recorded: every call does nothing."""

    def sent(self, prompt: Any, *, latency: float = 0.0, grace: float = 0.0, send_time: float = 0.0) -> None:
        pass

    def late_in_grace(self, message: Any, elapsed: float) -> None:
        pass

    def silent(self, net: Any = None) -> None:
        pass

    def answered(self, message: Any, elapsed: float, *, net: Any = None, since: float | None = None) -> None:
        pass

    def disrupted(self, *, gateway: bool = False, stall: float = 0.0) -> None:
        pass

    def timed_out(self, *, stall: float = 0.0) -> None:
        pass


NO_ROUND = _NoRound()


class RoundEntry(_NoRound):
    """The round being played; :func:`altermmo.core.challenge.ask` tells it what happens."""

    def __init__(self, fight: Fight, entry: Round, channel_id: int, players: tuple[int, ...]) -> None:
        self.fight = fight
        self.entry = entry
        self.channel_id = channel_id
        #: Whose message after the time is looked for: the asked player (else everybody asked).
        self.targets = (entry.asked_id,) if entry.asked_id is not None else players

    @_never_raises()
    def sent(self, prompt: Any, *, latency: float = 0.0, grace: float = 0.0, send_time: float = 0.0) -> None:
        """The prompt is on Discord.  Whoever is asked now is no longer waited for by an older round."""
        entry = self.entry
        entry.prompt_id = int(prompt.id)
        entry.prompt_at = prompt.created_at
        entry.latency, entry.grace, entry.send_time = float(latency), float(grace), float(send_time)
        if isinstance(prompt.created_at, datetime):
            entry.prompt_lag = (datetime.now(timezone.utc) - prompt.created_at).total_seconds()
        for player_id in self.targets:
            watch = _watches.get((self.channel_id, player_id))
            if watch is not None:
                _end_watch(watch, LateState.NEXT_PROMPT)

    @_never_raises()
    def late_in_grace(self, message: Any, elapsed: float) -> None:
        """A message arrived while the round still listened, but it was created after the limit."""
        entry = self.entry
        author_id = int(message.author.id)
        if author_id in self.targets and entry.late is None:
            entry.late = Late(after=float(elapsed), text=clip(message.content), in_grace=True,
                              arrival=arrival_of(message))
            entry.late_state = LateState.FOUND

    @_never_raises()
    def silent(self, net: Any = None) -> None:
        """The round stopped listening without an answer: from now on :func:`heard` listens.

        ``net``: the bot's connection monitor - a watch that ends with nothing
        notes whether the gateway connection dropped while it listened.
        """
        if self.entry.late is None and self.entry.prompt_at is not None:
            _start_watch(self.fight, self.entry, self.channel_id, self.targets, net)

    @_never_raises()
    def answered(self, message: Any, elapsed: float, *, net: Any = None, since: float | None = None) -> None:
        """A fighter answered in time (the right word or not).

        ``net`` / ``since``: the connection monitor and the start of the round -
        an answered round is not checked for a dropped connection by the fight,
        the journal notes it all the same.
        """
        entry = self.entry
        _drop_watch(entry)
        entry.late, entry.late_state = None, LateState.NONE
        author_id, author_name = _ident(message.author)
        text = str(message.content or "").strip().lower()
        entry.elapsed = float(elapsed)
        entry.typed = clip(message.content)
        entry.typed_by_id, entry.typed_by_name = author_id, author_name
        entry.arrival = arrival_of(message)
        entry.disturbed = bool(net is not None and since is not None and net.disrupted_since(since))
        if entry.asked_id is not None and author_id != entry.asked_id:
            entry.outcome = Outcome.WRONG_PLAYER
        elif text == entry.word:
            entry.outcome = Outcome.CORRECT
        else:
            entry.outcome = Outcome.WRONG_WORD
            self._answer_of_the_previous_round(message, text)

    def _answer_of_the_previous_round(self, message: Any, text: str) -> None:
        """A wrong word that is the word of the player's previous, timed-out round was that round's answer.

        It arrived after the next prompt, so the bot took it for the answer
        to that one.  The journal says so: the earlier round gets its late
        answer, this one a note (:attr:`Round.stale_of`).
        """
        entry = self.entry
        mine = [other for other in self.fight.rounds_of(entry.asked_id) if other is not entry]  # type: ignore[arg-type]
        previous = mine[-1] if mine else None
        if (previous is None or previous.outcome is not Outcome.TIMEOUT or previous.late is not None
                or previous.late_state is not LateState.NEXT_PROMPT or previous.prompt_at is None
                or text != previous.word):
            return
        previous.late = Late(after=(message.created_at - previous.prompt_at).total_seconds(),
                             text=clip(message.content), arrival=arrival_of(message))
        previous.late_state = LateState.FOUND
        entry.stale_of = previous.number
        log.info("%s", late_line(self.fight, previous))

    @_never_raises()
    def disrupted(self, *, gateway: bool = False, stall: float = 0.0) -> None:
        """The round does not count: the connection dropped or the event loop stalled."""
        entry = self.entry
        _drop_watch(entry)
        entry.late, entry.late_state = None, LateState.NONE
        entry.outcome = Outcome.DISRUPTED
        entry.disturbed = bool(gateway)
        entry.stall = max(float(stall), 0.0)

    @_never_raises()
    def timed_out(self, *, stall: float = 0.0) -> None:
        """Nobody answered in time (and it was not the bot's connection)."""
        entry = self.entry
        entry.outcome = Outcome.TIMEOUT
        entry.elapsed = None
        entry.stall = max(float(stall), 0.0)
        if entry.late is not None and entry.late.in_grace:
            log.info("%s", late_line(self.fight, entry))  # (it used to be a DEBUG line of the round)


@_never_raises(NO_ROUND)
def open_round(channel_id: int, *, turn: int | None, asked: Any, word: str | None, prompt: str,
               limit: float, players: Iterable[int] | None) -> _NoRound:
    """Start recording a round of the fight open on the channel.

    :data:`NO_ROUND` when there is nothing to record: no fight is open there,
    or the question has no expected ``word`` (it is not a fight round).
    """
    if word is None:
        return NO_ROUND
    fight = _open.get(int(channel_id))
    if fight is None:
        return NO_ROUND
    asked_id, asked_name = _ident(asked) if asked is not None else (None, "")
    entry = Round(number=int(turn) if turn is not None else len(fight.rounds) + 1,
                  asked_id=asked_id, asked_name=asked_name, word=str(word).strip().lower(),
                  prompt=clip(prompt, PROMPT_LIMIT), limit=float(limit))
    _append(fight, entry)
    return RoundEntry(fight, entry, int(channel_id), tuple(int(player) for player in players or ()))


# -- the answer after the time is up --------------------------------------------------------

def _start_watch(fight: Fight, entry: Round, channel_id: int, players: tuple[int, ...],
                 net: Any = None) -> None:
    if not players:
        return  # (a round anybody may answer: there is nobody to wait for)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop, no timer: a watch must never outlive its bound
    keys = tuple((channel_id, player_id) for player_id in players)
    for key in keys:
        older = _watches.get(key)
        if older is not None:
            _end_watch(older, LateState.NEXT_PROMPT)
    watch = _Watch(fight, entry, keys, time.monotonic(), net=net)
    watch.handle = loop.call_later(LATE_WATCH, _expire, watch)
    for key in keys:
        _watches[key] = watch
    entry.late_state = LateState.WATCHING


def _forget_watch(watch: _Watch) -> None:
    for key in watch.keys:
        if _watches.get(key) is watch:
            del _watches[key]
    if watch.handle is not None:
        watch.handle.cancel()
        watch.handle = None


def _watch_of(entry: Round) -> _Watch | None:
    return next((watch for watch in _watches.values() if watch.entry is entry), None)


def _drop_watch(entry: Round) -> None:
    """Stop listening for ``entry`` without a word (the round turned out not to be a timeout)."""
    if entry.late_state is LateState.WATCHING:
        watch = _watch_of(entry)
        if watch is not None:
            _forget_watch(watch)


def _end_watch(watch: _Watch, state: LateState, late: Late | None = None) -> None:
    _forget_watch(watch)
    entry = watch.entry
    entry.watched = time.monotonic() - watch.started
    entry.late = late
    entry.late_state = state
    if late is None and watch.net is not None:
        entry.watch_disturbed = bool(watch.net.disrupted_since(watch.started))
    if state is not LateState.STOPPED:
        log.info("%s", late_line(watch.fight, entry))
    _settle(watch.fight)  # (an ended fight waited for this answer to be archived)


def _expire(watch: _Watch) -> None:
    """The timer of a watch: nothing came within :data:`LATE_WATCH`."""
    try:
        watch.handle = None
        if any(_watches.get(key) is watch for key in watch.keys):
            _end_watch(watch, LateState.NOTHING)
    except Exception:  # noqa: BLE001 - a timer callback has nobody to raise to
        log.exception("Fight journal: closing a late-answer watch failed.")


def heard(message: Any) -> None:
    """Every message the bot receives passes here: is it the answer somebody is still owed?

    The first message the asked player writes on the fight's channel after
    the prompt ends the watch of that round.  Commands and complaints about
    lag are not answers and are skipped.  Nothing is consumed: the message
    goes on to the commands and to every listener as before.  Never raises.
    """
    if not _watches:
        return
    try:
        author = message.author
        if getattr(author, "bot", False):
            return
        watch = _watches.get((message.channel.id, author.id))
        if watch is None or message.id <= watch.entry.prompt_id or watch.entry.prompt_at is None:
            return
        text = str(message.content or "")
        if is_command_text(text) or is_lag_complaint(text):
            return
        after = (message.created_at - watch.entry.prompt_at).total_seconds()
        _end_watch(watch, LateState.FOUND, Late(after=after, text=clip(text), arrival=arrival_of(message)))
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("Fight journal: looking at a message failed.")


# -- look-ups -------------------------------------------------------------------------------

def last_fight(player_id: int) -> Fight | None:
    """The last fight ``player_id`` took part in (None when it is forgotten)."""
    try:
        _prune()
        return _last.get(int(player_id))
    except Exception:  # noqa: BLE001
        log.exception("Fight journal: look-up failed.")
        return None


def find_fight(who: str) -> tuple[Fight, int] | None:
    """``(fight, player id)`` for an id, a mention or a name of a fighter; the newest fight wins."""
    try:
        _prune()
        wanted = str(who or "").strip()
        match = re.fullmatch(r"<@!?(\d{5,20})>|(\d{5,20})", wanted)
        if match is not None:
            player_id = int(match.group(1) or match.group(2))
            fight = _last.get(player_id)
            return (fight, player_id) if fight is not None else None
        wanted = wanted.lstrip("@").casefold()
        found = [(fight, player_id) for fight in _last.values() for player_id, name in fight.fighters
                 if name.casefold() == wanted and _last.get(player_id) is fight]
        return max(found, key=lambda pair: pair[0].serial) if found else None
    except Exception:  # noqa: BLE001
        log.exception("Fight journal: look-up failed.")
        return None


def recent() -> list[Fight]:
    """The fights of the ring, oldest first."""
    return list(_recent)


# -- the lines for logs/altermmo.log ---------------------------------------------------------

def kind_text(fight: Fight) -> str:
    if fight.kind == KIND_TOWER:
        return f"tower floor {fight.detail}"
    if fight.kind == KIND_GROUP:
        return "group boss"
    if fight.kind == KIND_DUEL:
        return "duel"
    return f"{fight.kind} (rarity {fight.detail})" if fight.detail is not None else fight.kind


def fighters_text(fight: Fight) -> str:
    return ", ".join(f"{name} ({player_id})" for player_id, name in fight.fighters)


def _round_text(fight: Fight, entry: Round) -> str:
    whom = f" for {entry.asked_name}" if fight.party and entry.asked_name else ""
    return f"round {entry.number}{whom}, word {entry.word!r}, limit {entry.limit:.2f} s"


def _late_text(entry: Round) -> str:
    late = entry.late
    if late is not None:
        return (f"first message {late.after:.2f} s after the prompt, {late.after - entry.limit:.2f} s "
                f"over the limit: {late.text!r}")
    if entry.late_state is LateState.WATCHING:
        return f"no answer in time (listening {LATE_WATCH:.0f} s more for a late one)"
    dropped = " (the connection DROPPED while listening)" if entry.watch_disturbed else ""
    if entry.late_state is LateState.NOTHING:
        return f"no answer, none within {entry.watched:.0f} s after the round either{dropped}"
    if entry.late_state is LateState.NEXT_PROMPT:
        return f"no answer, none before the next prompt {entry.watched:.1f} s later either{dropped}"
    return "no answer in time"


def _outcome_text(entry: Round) -> str:
    if entry.outcome is Outcome.TIMEOUT:
        return _late_text(entry)
    if entry.outcome is Outcome.WRONG_WORD:
        stale = f" (the word of round {entry.stale_of}, late)" if entry.stale_of is not None else ""
        return f"wrong word {entry.typed!r} after {entry.elapsed or 0.0:.2f} s{stale}"
    if entry.outcome is Outcome.WRONG_PLAYER:
        return (f"answered by {entry.typed_by_name} instead of {entry.asked_name} after "
                f"{entry.elapsed or 0.0:.2f} s: {entry.typed!r}")
    return entry.outcome.value if entry.outcome is not None else "unfinished"


def _health_text(fight: Fight, entry: Round) -> str:
    stall = f", event loop late by {entry.stall:.2f} s" if entry.stall >= 0.05 else ""
    return (f"latency {entry.latency * 1000:.0f} ms, grace {entry.grace:.2f} s, connection "
            f"{'DISTURBED' if entry.disturbed else 'steady'}{stall}, "
            f"{len(fight.disrupted_rounds())} disrupted round(s)")


def arrivals_of(fight: Fight, player_id: int | None) -> list[Arrival]:
    """Every recorded arrival of ``player_id``'s messages in the fight (answers in time and late ones)."""
    found: list[Arrival] = []
    for entry in fight.rounds:
        if entry.arrival is not None and entry.typed_by_id == player_id:
            found.append(entry.arrival)
        if entry.late is not None and entry.late.arrival is not None and entry.asked_id == player_id:
            found.append(entry.late.arrival)
    return found


def answer_arrival(entry: Round) -> Arrival | None:
    """The arrival of what decided ``entry``: the answer in time, else the late one."""
    if entry.arrival is not None:
        return entry.arrival
    return entry.late.arrival if entry.late is not None else None


def arrival_text(fight: Fight, entry: Round) -> str:
    """The times of the answer of ``entry`` (" | ..." for a log line; '' when it had no answer).

    "send +0.31 s (+0.12 s over this fight's best of 7)" - nonce -> Discord;
    only the excess means something (the device's clock offset is in both).
    A message without a nonce adds nothing to the line (the archive has it).
    """
    arrival = answer_arrival(entry)
    if arrival is None or arrival.nonce is None:
        return ""
    parts = []
    upload = arrival.upload
    if upload is None:
        parts.append(f"no client send time (nonce {arrival.nonce!r} is not a time)")
    else:
        player_id = entry.typed_by_id if entry.arrival is arrival else entry.asked_id
        uploads = [item.upload for item in arrivals_of(fight, player_id) if item.upload is not None]
        best = min(uploads) if uploads else upload
        parts.append(f"client send {upload:+.2f} s ({upload - best:+.2f} s over this fight's best of "
                     f"{len(uploads)})")
    if arrival.delivery is not None:
        parts.append(f"Discord->bot {arrival.delivery:.2f} s")
    return " | " + ", ".join(parts)


def lost_line(fight: Fight) -> str:
    """The one INFO line of a lost fight (the owner analyses fights from the log file)."""
    entry = fight.decisive
    if fight.kind == KIND_DUEL:
        loser = fight.losers[0] if fight.losers else None
        mine = fight.rounds_of(loser) if loser is not None else []
        tally = (f"{len(mine)} round(s) asked, "
                 f"{sum(1 for item in mine if item.outcome is Outcome.TIMEOUT)} without an answer in time, "
                 f"{sum(1 for item in mine if item.outcome is Outcome.WRONG_WORD)} with a wrong word")
        who = f"{fight.name_of(loser)} ({loser}) lost to {', '.join(fight.name_of(w) for w in fight.winners)}"
        if entry is None:
            return f"Fight lost: duel | {who} | {tally}; no failed round"
        return (f"Fight lost: duel | {who} | {tally}; last failed {_round_text(fight, entry)}: "
                f"{_outcome_text(entry)} | {_health_text(fight, entry)}{arrival_text(fight, entry)}")
    if entry is None:
        return (f"Fight lost: {kind_text(fight)} | {fighters_text(fight)} | no round recorded "
                f"({fight.reason or 'unknown reason'})")
    return (f"Fight lost: {kind_text(fight)} | {fighters_text(fight)} | {_round_text(fight, entry)}: "
            f"{_outcome_text(entry)} | {_health_text(fight, entry)}{arrival_text(fight, entry)}")


def late_line(fight: Fight, entry: Round) -> str:
    """The INFO line written when the watch of a timed-out round ends."""
    return (f"Late answer: {kind_text(fight)} | {entry.asked_name or fighters_text(fight)} "
            f"({entry.asked_id}) | {_round_text(fight, entry)}: {_late_text(entry)}{arrival_text(fight, entry)}")


def summary_line(fight: Fight) -> str:
    """The INFO line of an archived fight: how it ended and how its answers travelled."""
    asked = [entry for entry in fight.rounds if entry.outcome not in (None, Outcome.PET_ATTACK)]
    arrivals = [(entry, answer_arrival(entry)) for entry in asked]
    answered = [(entry, arrival) for entry, arrival in arrivals if arrival is not None]
    timed = [(entry, arrival) for entry, arrival in answered if arrival.upload is not None]
    end = fight.end.value if fight.end is not None else "never ended"
    text = (f"Fight archived: {kind_text(fight)} | {fighters_text(fight)} | {end}, {len(asked)} round(s), "
            f"client send time in {len(timed)}/{len(answered)} answer(s)")
    worst: tuple[float, Round] | None = None
    for entry, arrival in timed:
        player_id = entry.typed_by_id if entry.arrival is arrival else entry.asked_id
        best = min(item.upload for item in arrivals_of(fight, player_id) if item.upload is not None)
        excess = arrival.upload - best  # type: ignore[operator]
        if worst is None or excess > worst[0]:
            worst = (excess, entry)
    if worst is not None:
        text += f", slowest send {worst[0]:+.2f} s over the player's best (round {worst[1].number})"
    deliveries = [arrival.delivery for _, arrival in answered if arrival.delivery is not None]
    if deliveries:
        text += f", Discord->bot at most {max(deliveries):.2f} s"
    return text


# -- "lag" ----------------------------------------------------------------------------------

#: A complaint is a short remark, not a story that happens to contain the word.
COMPLAINT_MAX_LENGTH = 120
_WORDS = re.compile(r"[a-z]+")
_STRETCHED = re.compile(r"(.)\1+")
#: The word "lag" in the forms players use, after :func:`_fold` (no diacritics, no doubled
#: letters): the Polish noun in its cases ("lag", "lagi", "laga", "lagu", "lagów", "lagiem"...),
#: the verb with its prefixes ("laguje", "lagowało", "zlagowało", "przylagowało", "lagnęło"...),
#: and the English forms ("lags", "laggy", "lagging", "lagged").  Words that merely contain
#: the letters ("flaga", "plaga", "blaga", "szlag", "lagier", "laguna") do not match.
_LAG = re.compile(
    r"lag(?:i|a|u|ow|om|ami|ach|iem|s|y|ing|ed)?"
    r"|(?:z|za|przy|prze|wy|po|s)?lag(?:uje|uja|ujesz|ujemy|owac|owal[aoyi]?|owalem|owalam"
    r"|nal|nela|nelo|owanie|owany|owana|owane|ujacy|ujaca|ujace)")


def _fold(text: str) -> str:
    """Lower case, Polish letters to their base letters, stretched letters to one ("laaag" -> "lag")."""
    text = unicodedata.normalize("NFD", text.lower().replace("ł", "l"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _STRETCHED.sub(r"\1", text)


def is_lag_complaint(text: Any) -> bool:
    """True when ``text`` is a short message that complains about lag.

    "lag", "lagi", "ale lag", "LAG!!!", "laaag", "lagg", "co za lagi", "przez
    laga", "zlagowało mnie" - any form of the word "lag" as a word of its own,
    in a message of at most :data:`COMPLAINT_MAX_LENGTH` characters that is
    not a command.  Near-synonyms ("ping", "zacięło", "zwiecha") are left out
    on purpose: they are said about many other things.
    """
    text = str(text or "").strip()
    if not text or len(text) > COMPLAINT_MAX_LENGTH or is_command_text(text):
        return False
    folded = _fold(text)
    if "lag" not in folded:  # (every message of the server passes here: the common case ends early)
        return False
    return any(_LAG.fullmatch(word) for word in _WORDS.findall(folded))


# -- the archive ------------------------------------------------------------------------------

def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _seconds(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def arrival_record(arrival: Arrival | None) -> dict[str, Any] | None:
    if arrival is None:
        return None
    return {"message_id": arrival.message_id, "created_at": _iso(arrival.created_at),
            "received_at": _iso(arrival.received_at), "nonce": arrival.nonce,
            "nonce_at": _iso(arrival.nonce_at), "upload": _seconds(arrival.upload),
            "delivery": _seconds(arrival.delivery)}


def round_record(entry: Round) -> dict[str, Any]:
    late = entry.late
    return {
        "number": entry.number, "asked_id": entry.asked_id, "asked_name": entry.asked_name,
        "word": entry.word, "limit": _seconds(entry.limit),
        "outcome": entry.outcome.value if entry.outcome is not None else None,
        "elapsed": _seconds(entry.elapsed), "typed": entry.typed, "typed_by_id": entry.typed_by_id,
        "saved": entry.saved, "stale_of": entry.stale_of,
        "prompt_id": entry.prompt_id or None, "prompt_at": _iso(entry.prompt_at),
        "prompt_lag": _seconds(entry.prompt_lag), "send_time": _seconds(entry.send_time),
        "latency": _seconds(entry.latency), "grace": _seconds(entry.grace),
        "disturbed": entry.disturbed, "stall": _seconds(entry.stall),
        "arrival": arrival_record(entry.arrival),
        "late": None if late is None else {
            "after": _seconds(late.after), "over": _seconds(late.after - entry.limit), "text": late.text,
            "in_grace": late.in_grace, "arrival": arrival_record(late.arrival)},
        "late_state": entry.late_state.value, "watched": _seconds(entry.watched),
        "watch_disturbed": entry.watch_disturbed,
    }


def fight_record(fight: Fight) -> dict[str, Any]:
    """One line of the archive: the whole fight, round by round (plain JSON values)."""
    duration = None if fight.ended is None else fight.ended - fight.started
    detail = fight.detail if isinstance(fight.detail, (int, float, str, type(None))) else str(fight.detail)
    return {
        "v": ARCHIVE_VERSION, "serial": fight.serial, "kind": fight.kind, "detail": detail,
        "channel_id": fight.channel_id, "started_at": _iso(fight.started_at),
        "duration": _seconds(duration), "end": fight.end.value if fight.end is not None else None,
        "reason": fight.reason,
        "fighters": [{"id": player_id, "name": name} for player_id, name in fight.fighters],
        "winners": list(fight.winners), "losers": list(fight.losers),
        "decisive_round": fight.decisive.number if fight.decisive is not None else None,
        "rounds": [round_record(entry) for entry in fight.rounds],
    }


class _Writer:
    """The archive's thread: appends the lines it is handed to the file of their day.

    The event loop only puts a ready line on a queue (never blocks); a full
    disk or a locked file costs the line and one ERROR, never a fight.
    """

    def __init__(self, directory: Path, keep_days: int) -> None:
        self.directory = directory
        self.keep_days = keep_days
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.pruned_on: str | None = None
        self.thread = threading.Thread(target=self._run, name="fight-archive", daemon=True)
        self.thread.start()

    def put(self, day: str, line: str) -> None:
        self.queue.put((day, line))

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until every line handed in so far is written (False on timeout)."""
        done = threading.Event()
        self.queue.put(done)
        return done.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        self.queue.put(None)
        self.thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            day, line = item
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with open(self.directory / f"{ARCHIVE_PREFIX}{day}{ARCHIVE_SUFFIX}", "a", encoding="utf-8",
                          newline="\n") as file:
                    file.write(line + "\n")
            except Exception:  # noqa: BLE001 - the archive is a by-product
                log.exception("Fight archive: writing a fight to %s failed.", self.directory)
            if self.pruned_on != day:
                self.pruned_on = day
                self.prune(day)

    def prune(self, today: str) -> None:
        """Delete the day files older than :attr:`keep_days` (by the date in their names)."""
        try:
            limit = (datetime.fromisoformat(today) - timedelta(days=self.keep_days)).date().isoformat()
            for path in self.directory.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}"):
                day = path.name[len(ARCHIVE_PREFIX):-len(ARCHIVE_SUFFIX)]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and day < limit:
                    path.unlink()
                    log.info("Fight archive: %s deleted (older than %s days).", path.name, self.keep_days)
        except Exception:  # noqa: BLE001
            log.exception("Fight archive: deleting old files failed.")


_writer: _Writer | None = None


def configure_archive(directory: Path | None, *, keep_days: int = ARCHIVE_KEEP_DAYS) -> None:
    """Archive every fight from now on into ``directory`` (None: stop archiving).  Never raises."""
    global _writer
    try:
        if _writer is not None:
            if directory is not None and Path(directory) == _writer.directory:
                _writer.keep_days = keep_days
                return
            _writer.close()
            _writer = None
        if directory is not None:
            _writer = _Writer(Path(directory), keep_days)
            log.info("Fight archive: every fight is written to %s (kept %s days).", directory, keep_days)
    except Exception:  # noqa: BLE001
        log.exception("Fight archive: could not be set up; fights are kept in memory only.")


def archive_directory() -> Path | None:
    return None if _writer is None else _writer.directory


def flush_archive(timeout: float = 5.0) -> bool:
    """Block until the archive has written everything handed to it (tests, shutdown)."""
    return True if _writer is None else _writer.flush(timeout)


def _settle(fight: Fight) -> None:
    """Archive ``fight`` if it has ended and no late answer of it is still awaited.  Never raises."""
    try:
        if fight.end is None or fight.archived:
            return
        if any(watch.fight is fight for watch in _watches.values()):
            return  # (the watch's end settles it again)
        _archive(fight)
    except Exception:  # noqa: BLE001
        log.exception("Fight archive: settling a fight failed (the fight is not affected).")


def _archive(fight: Fight) -> None:
    """Hand ``fight`` to the archive (once).  Never raises, never waits for the disk."""
    try:
        if _writer is None or fight.archived:
            return
        fight.archived = True
        line = json.dumps(fight_record(fight), ensure_ascii=False, separators=(",", ":"))
        _writer.put(fight.started_at.astimezone(timezone.utc).date().isoformat(), line)
        log.info("%s", summary_line(fight))
    except Exception:  # noqa: BLE001
        log.exception("Fight archive: archiving a fight failed (the fight is not affected).")


# -- background work that must not leak ------------------------------------------------------

def spawn(coroutine: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task | None:
    """Run ``coroutine`` in the background; :func:`stop` cancels it at shutdown.  Never raises."""
    try:
        task = asyncio.get_running_loop().create_task(coroutine, name=name)
    except Exception:  # noqa: BLE001 - e.g. no running loop
        coroutine.close()
        log.exception("Fight journal: could not start a background task.")
        return None
    _tasks.add(task)
    task.add_done_callback(_task_done)
    return task


def _task_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.error("Fight journal: a background task failed.", exc_info=task.exception())


def pending() -> tuple[int, int]:
    """``(watches, background tasks)`` that are still alive - for diagnostics and tests."""
    return len({id(watch) for watch in _watches.values()}), len(_tasks)


def _stop_watches() -> None:
    for watch in {id(watch): watch for watch in _watches.values()}.values():
        try:
            _end_watch(watch, LateState.STOPPED)
        except Exception:  # noqa: BLE001
            log.exception("Fight journal: stopping a watch failed.")
    _watches.clear()


async def stop() -> None:
    """Shutdown: end every watch and cancel every background task.  Never raises.

    The fights still running are archived as they are (without an end) and
    the archive writes out what it holds.
    """
    _stop_watches()
    for fight in list(_open.values()):
        _archive(fight)
    tasks = [task for task in _tasks if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    if _writer is not None:
        try:
            if not await asyncio.to_thread(_writer.flush, 5.0):
                log.warning("Fight archive: not everything was written before the shutdown.")
        except Exception:  # noqa: BLE001
            log.exception("Fight archive: flushing at shutdown failed.")


def reset() -> None:
    """Forget everything (tests; no event loop needed).  The archive is switched off."""
    global _serial
    configure_archive(None)
    _stop_watches()
    for task in list(_tasks):
        try:
            task.cancel()
        except Exception:  # noqa: BLE001 - a task of a loop that is closed already
            pass
    _tasks.clear()
    _open.clear()
    _last.clear()
    _recent.clear()
    _serial = 0
