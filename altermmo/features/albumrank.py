"""The value of an album's collection and the ranking of the supporters' collections.

Owner's request of 2026-11-22: "W trakcie sprawdzania albumu chciałbym żebyś też pokazywał
wartość punktową kolekcji i miejsce w rankingu. Poza tym stwórz ranking kolekcji. [...] Nie licz
wielu kart tego samego peta na tym samym etapie ewolucji, liczy się najrzadsza karta. Licz
wszystkie karty po 1 na każdą ewolucję peta" - and a moment later: "Ale ci co nie mają wsparcia
nie są liczeni w rankingu. Nie mogą też sprawdzać tego rankingu."

The rules (the lead's design, told to the owner):

* A **slot** is ``(species, stage)``: every member of :class:`pets.PetType` - the mythic Void
  INCLUDED (it is not needed for "Pełny album", but its cards score) - at every stage 0 (the egg)
  .. :data:`pets.MAX_LEVEL` (:func:`score_slots`, computed from those constants).  An entry of a
  species the game does not know or at a stage outside that range scores nothing.  A NULL level
  is the egg, as everywhere in the game (``pets._int``; the album shows such an entry as "Jajko").
* The value of ONE card (one album entry, :func:`card_points`): :data:`CARD_POINTS` for the card
  itself plus a bonus per ornament - ``round(BONUS_SCALE * log2(100 / p))`` where ``p`` is the
  chance of that ornament in percent (:func:`bonus`): the rarer, the more, and twice as rare is
  always :data:`BONUS_SCALE` points more.  Every number of the score is in THE SCORE TABLE below.
* The value of a **collection** (:func:`collection`): per slot only the highest-valued card the
  player has (several cards of one slot: the best one counts, the others add nothing), summed.
  It is a property of the stored cards - whether the ornaments are *shown* (the owner supports)
  does not change it.
* **Who is ranked** (:func:`ranking`): only players who are supporters RIGHT NOW
  (:func:`patrons.check_if_patron` on the guild member) and have at least one scoring entry.  A
  player whose support lapsed or who left the server is not listed and takes no place; his album
  rows stay, and when he supports again he is back with his cards.  Order: points (descending),
  then more scored slots, then the player whose newest COUNTED card is older (who got there
  first), then the player id - deterministic, so every place is a different number.
* **Who may look**: supporters only (:func:`show_ranking`); everybody else gets the same kind of
  "this is for supporters" answer as ``$album`` / ``$osiagniecia``.

Whose support is it, without a Discord request per player?  The bot asks for the member list at
start-up (``intents.members``, discord.py chunks the guild), and every role change arrives as an
event, so the guild's member cache is the truth - :func:`supporters_among` reads it.  While the
cache is NOT complete (``guild.chunked`` is False: the first moments after a start or a
reconnect) an album owner missing from it may still be on the server: only those owners - never
the whole server - are looked up one by one (:data:`LOOKUP_CONCURRENCY` at a time, at most
:data:`LOOKUP_LIMIT` of them).  When that is too many, or a lookup fails for any reason but "not
a member", the ranking is "not available for a moment" (:data:`MSG_UNAVAILABLE`; the album
shows its value without a place) - a ranking must never silently show wrong places.  A "not a
member" answer is remembered for :data:`NOT_MEMBER_TTL` (at most :data:`NOT_MEMBER_LIMIT` of
them), so the next looks do not ask Discord again for every owner who left the server.

Cost: ONE query (the few columns the score needs, every entry of every player; NUMERIC cast so
that 30 000 Decimals need not be decoded) and the score in Python, a card computed once per
distinct look, in a worker thread.  Measured on the local test database with 300 players x 100
entries (``tests/test_albumrank.py``, ``test_the_ranking_is_cheap``, 2026-11-22): about 170 ms for
the whole ranking (~80 ms the query, ~90 ms the score), the event loop held at most ~60-80 ms at
a time; one album of 100 entries ~5 ms.  Every ``$album`` (and the panel's overview page) makes
the ranking anew, so there is NO cache - a sacrifice or a removal is reflected at the next look,
always.  (Before the casts and the thread: ~390 ms, loop held ~200 ms.)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import asyncpg
import discord

from ..core import io, ui
from . import album, patrons, petcards, pets

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# == THE SCORE TABLE ============================================================================
# Every number of the score lives here (the owner may change them).  The chances are written down
# on purpose instead of being read from the roll tables: a change of the drop chances must not
# silently reshuffle the ranking (tests/test_albumrank.py states them literally).

#: Points of a card itself, ornaments or not.
CARD_POINTS = 10
#: ``bonus = round(BONUS_SCALE * log2(100 / p))`` for an ornament of chance ``p`` percent.
BONUS_SCALE = 10
#: The class of the pet, from the egg roll ``randint(0, 1000)`` of pets.py (``SHINY_FROM``,
#: ``ULTRA_SHINY_FROM``, ``MYTHIC_FROM`` = 900 / 980 / 998: 80, 18 and 3 of 1001 results).  An
#: ordinary pet gets nothing; the class is :func:`petcards.tier_of` (mythic > ultra shiny > shiny).
CLASS_PERCENT: dict[str, float] = {"S": 8.0, "U": 1.8, "M": 0.3}
#: The seal tier, from the seal roll of pets.py (``SEAL_ROLL_BELOW``) and the owner's
#: ``assets/petcards/source/pule.json``: Tropiciela 10%, Łowcy 4.8%, Arcyłowcy 0.2%.  A stored seal
#: outside 1..3 gets nothing.
SEAL_PERCENT: dict[int, float] = {1: 10.0, 2: 4.8, 3: 0.2}
#: The foil, from the foil roll of pets.py (``FOIL_ROLL_BELOW``): partial Holo 1%, full Holo 0.1%
#: (both flags set: full Holo, as :func:`petcards.look_of` draws it).
FOIL_PERCENT: dict[int, float] = {petcards.FOIL_PARTIAL: 1.0, petcards.FOIL_FULL: 0.1}
#: The frame: the result the card is DRAWN with (:func:`petcards.frame_of` - a stored id that is
#: unknown or of another class is the base of the class).  A base frame (S0 / U0 / M0) gets
#: nothing, every other result its chance below - the catalogue's chances of 2026-11-21, kept when the
#: drop chances were tripled on 2026-11-22 so the ranking did not move (:func:`frame_points`); a
#: frame added to the catalogue later and missing here scores by its own ``Frame.percent``.
FRAME_PERCENT: dict[str, float] = {
    "S1": 8.0, "S1-A": 0.55, "R1": 4.0, "R1-A": 0.2, "R2": 2.0, "R2-A": 0.15, "R7": 1.0, "R7-A": 0.1,
    "U1": 8.0, "U1-A": 0.55, "R3": 4.0, "R3-A": 0.2, "R4": 2.0, "R4-A": 0.15, "R8": 1.0, "R8-A": 0.1,
    "R5": 5.0, "R5-A": 0.5, "R6": 3.0, "R6-A": 0.3, "R9": 1.0, "R9-A": 0.2,
}
# ================================================================================================


def bonus(percent: float) -> int:
    """The points of an ornament of chance ``percent``: 8% -> 36, 1% -> 66, 0.1% -> 100.

    Nothing for a chance that is not below 100% (or not a positive number).
    """
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return 0
    if not 0 < percent < 100:
        return 0
    return round(BONUS_SCALE * math.log2(100 / percent))


def score_slots() -> frozenset[tuple[str, int]]:
    """Every scoring ``(TYPE, level)``: every species (the mythic ones too) at every stage."""
    return frozenset((kind.value, level) for kind in pets.PetType for level in album.stages())


def _field(row: Any, key: str) -> Any:
    """``row[key]``, None when the row has no such column (rows from before a column existed)."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _slot(row: Any) -> tuple[str, int] | None:
    """The ``(TYPE, level)`` of an entry; None when it has no usable type or level."""
    species = _field(row, "type")
    if not isinstance(species, str):
        return None
    level = _field(row, "pet_lvl")
    try:
        number = 0 if level is None else int(level)
        if number != level and level is not None:  # 2.5 is no stage
            return None
    except (TypeError, ValueError, ArithmeticError):
        return None
    return species, number


def class_points(row: Any) -> int:
    return bonus(CLASS_PERCENT[tier]) if (tier := petcards.tier_of(row)) in CLASS_PERCENT else 0


def frame_points(row: Any) -> int:
    """The frame the card is drawn with: nothing for no frame or a base frame."""
    frame = petcards.frame_of(row)
    if frame is None or frame.base:
        return 0
    return bonus(FRAME_PERCENT.get(frame.id, frame.percent))


def seal_points(row: Any) -> int:
    raw = _field(row, "seal")
    try:
        tier = int(raw)
        if tier != raw:  # 2.5, NaN ... is no seal
            return 0
    except (TypeError, ValueError, ArithmeticError):
        return 0
    return bonus(SEAL_PERCENT[tier]) if tier in SEAL_PERCENT else 0


def foil_points(row: Any) -> int:
    foil = petcards.look_of(row).foil
    return bonus(FOIL_PERCENT[foil]) if foil in FOIL_PERCENT else 0


def ornament_points(row: Any) -> int:
    """The card's value without the slot check: :data:`CARD_POINTS` + every ornament's bonus."""
    return CARD_POINTS + class_points(row) + frame_points(row) + seal_points(row) + foil_points(row)


def card_points(row: Any, slots: frozenset[tuple[str, int]] | None = None) -> int:
    """The value of ONE album entry (0 for an entry outside every slot).

    Reads ``type``, ``pet_lvl``, ``mythic``, ``ultra_shiny``, ``shiny``, ``frame``, ``seal``,
    ``holo``, ``holo_partial`` - each optional (rows from before the frames or the partial foil
    have none of them; a NULL counts as nothing).  Never raises.
    """
    slot = _slot(row)
    if slot is None or slot not in (score_slots() if slots is None else slots):
        return 0
    try:
        return ornament_points(row)
    except Exception:  # noqa: BLE001 - a row the game cannot read is worth the card alone
        log.exception("Album entry %s: its ornaments could not be read.", _field(row, "entry_id"))
        return CARD_POINTS


#: Every column :func:`card_points` reads: the memo key of :class:`_Scorer`.
_SCORED_COLUMNS = ("type", "pet_lvl", "mythic", "ultra_shiny", "shiny", "frame", "seal", "holo", "holo_partial")


class _Scorer:
    """``(slot, points)`` of many rows: the slots computed once, a card once per distinct look.

    ``key`` makes the memo key of a row (default: the :data:`_SCORED_COLUMNS` read one by one);
    the ranking passes a faster one for the rows of its own query.  Two rows with the same key
    are worth the same - the key holds every value the score reads.
    """

    def __init__(self, key: Any = None) -> None:
        self.slots = score_slots()
        self._key = key or (lambda row: tuple(_field(row, name) for name in _SCORED_COLUMNS))
        self._memo: dict[tuple, tuple[tuple[str, int] | None, int]] = {}

    def __call__(self, row: Any) -> tuple[tuple[str, int] | None, int]:
        try:
            key = self._key(row)
            known = self._memo.get(key)
        except TypeError:  # an unhashable value: computed without the memo
            key = known = None
        if known is None:
            slot = _slot(row)
            known = (slot, card_points(row, self.slots) if slot in self.slots else 0)
            if key is not None:
                self._memo[key] = known
        return known


# -- one collection ------------------------------------------------------------------------------

#: An ``ADDED_AT`` that is not there sorts as the newest (it never wins a tie).
_LATEST = datetime.max.replace(tzinfo=timezone.utc)


def _moment(row: Any) -> datetime:
    """``ADDED_AT`` as an aware moment (a naive one is UTC) - comparable with every other."""
    added = _field(row, "added_at")
    if not isinstance(added, datetime):
        return _LATEST
    return added.replace(tzinfo=timezone.utc) if added.tzinfo is None else added


def _entry_id(row: Any) -> int:
    try:
        return int(_field(row, "entry_id"))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Collection:
    """The value of one album."""

    #: The sum of the counted cards.
    points: int
    #: The counted card of every scored slot: ``slot -> (points, entry id)``.
    best: dict[tuple[str, int], tuple[int, int]] = field(default_factory=dict)
    #: When the newest COUNTED card came into the album (ties of the ranking).
    newest: datetime = _LATEST

    @property
    def slots(self) -> int:
        """How many slots score (= how many cards count)."""
        return len(self.best)

    def counts(self, row: Any) -> bool:
        """True when this entry is the one counted for its slot."""
        slot = _slot(row)
        return slot in self.best and self.best[slot][1] == _entry_id(row)

    def beaten(self, row: Any, points: int | None = None) -> bool:
        """True when the album holds a card of the entry's slot worth MORE than this entry.

        (Two cards of equal value: neither is "beaten" - one of them counts.)
        """
        slot = _slot(row)
        if slot not in self.best:
            return False
        return self.best[slot][0] > (card_points(row) if points is None else points)


def collection(rows: Iterable[Any], scorer: _Scorer | None = None) -> Collection:
    """The value of an album from its entries: per slot the best card, summed.

    Of two cards of equal value the one that came first counts (older ``ADDED_AT``, then the
    smaller entry id) - so "the newest counted card" of a tie-break is the moment the player
    reached that value.
    """
    score = scorer or _Scorer()
    best: dict[tuple[str, int], tuple[int, Any]] = {}
    for row in rows:
        slot, points = score(row)
        if points <= 0:
            continue
        held = best.get(slot)  # type: ignore[arg-type]
        # (The moment is read only for a tie: most rows are decided by their points.)
        if held is None or points > held[0] or (points == held[0] and _first(row, held[1])):
            best[slot] = (points, row)  # type: ignore[index]
    return Collection(points=sum(points for points, _ in best.values()),
                      best={slot: (points, _entry_id(row)) for slot, (points, row) in best.items()},
                      newest=max((_moment(row) for _, row in best.values()), default=_LATEST))


def _first(row: Any, other: Any) -> bool:
    """True when ``row`` came into the album before ``other`` (then: the smaller entry id)."""
    return (_moment(row), _entry_id(row)) < (_moment(other), _entry_id(other))


# -- the ranking ---------------------------------------------------------------------------------

#: Only what the score needs, every entry of every player.  An album made before the frame / the
#: partial foil existed (``schema.add_new_columns`` could not add them yet) is read without the
#: one(s) it lacks - and only those (:func:`_all_rows`).
#: The NUMERIC columns are cast: decoding 30 000 ``Decimal``\ s took more than half of the whole
#: read.  ``PLAYER_ID`` as text keeps every digit (``int`` per player, :func:`_player_id`); the level
#: and the seal as float8 keep a fraction or NaN visible, so the score reads them as before
#: (2.5 is no stage / no seal).
_SQL_ALL = (f"SELECT PLAYER_ID::text AS PLAYER_ID, ENTRY_ID, ADDED_AT, TYPE, PET_LVL::float8 AS PET_LVL, "
            f"SHINY, ULTRA_SHINY, MYTHIC, SEAL::float8 AS SEAL, HOLO{{later}} FROM {album.TABLE}")


def _query_key(row: Any) -> tuple:
    """The memo key of a row of :data:`_SQL_ALL`: every value after ``PLAYER_ID, ENTRY_ID,
    ADDED_AT`` - exactly the columns the score reads (one query: every row has the same shape)."""
    return tuple(row.values())[3:]


def _player_id(raw: Any) -> int | None:
    """``PLAYER_ID`` (text, Decimal or int) as an int; None for something that is no id."""
    try:
        return int(Decimal(str(raw)))
    except (TypeError, ValueError, ArithmeticError):
        return None


def collections(rows: Iterable[Any]) -> dict[int, Collection]:
    """``player id -> Collection`` of every player who has a scoring entry (rows of :data:`_SQL_ALL`)."""
    per_player: dict[Any, list[Any]] = {}
    for row in rows:
        per_player.setdefault(row["player_id"], []).append(row)
    scorer = _Scorer(_query_key)
    values: dict[int, Collection] = {}
    for raw_id, entries in per_player.items():
        player_id = _player_id(raw_id)
        if player_id is None:
            continue
        value = collection(entries, scorer)
        if value.points > 0:
            values[player_id] = value
    return values

#: While the member cache is incomplete: how many album owners missing from it are looked up at
#: most (more: "not available for a moment"), and how many at a time.
LOOKUP_LIMIT = 50
LOOKUP_CONCURRENCY = 4


@dataclass(frozen=True)
class Standing:
    """One ranked player."""

    place: int
    player_id: int
    member: Any
    value: Collection

    @property
    def points(self) -> int:
        return self.value.points

    @property
    def cards(self) -> int:
        return self.value.slots


@dataclass(frozen=True)
class Ranking:
    """The ranked supporters, best first (places 1 .. N)."""

    standings: tuple[Standing, ...]

    @property
    def total(self) -> int:
        return len(self.standings)

    def of(self, player_id: int) -> Standing | None:
        return next((item for item in self.standings if item.player_id == int(player_id)), None)


def _later_choices() -> list[tuple[str, ...]]:
    """The later columns to read, most first: all of them, then every set without one, ... none.

    An album made before :data:`album.LATER_COLUMNS` may lack ONE of them only (one could be
    added at start-up, the other not): the ranking must then still read the other one - the
    album's overview scores the viewer's own rows (``SELECT *``) with every column the table has,
    and the value beside his place would otherwise disagree with the place.
    """
    names = tuple(column for column, _ in album.LATER_COLUMNS)
    return [choice for size in range(len(names), -1, -1) for choice in itertools.combinations(names, size)]


async def _all_rows(db: Any) -> list[Any]:
    """Every entry of every player with exactly the later columns the table has (:func:`_later_choices`).

    A column that is not there is a planning error of the database (nothing is read), so trying
    the next set costs one round trip; with two later columns at most four.
    """
    choices = _later_choices()
    for index, choice in enumerate(choices):
        try:
            return await album._fetch(db, "fetch", _SQL_ALL.format(later="".join(f", {name}" for name in choice)))
        except asyncpg.UndefinedColumnError as exc:
            if index + 1 == len(choices):
                raise
            log.warning("PET_ALBUM lacks a later column (%s): the ranking tries without %s.",
                        exc, ", ".join(choice) or "it")
    raise AssertionError("unreachable")  # pragma: no cover - the last choice reads no later column


#: "Is not a member" answers of :func:`supporters_among` (``bot._album_not_members``: player id ->
#: when it expires, ``_clock`` seconds) are believed this long: while the guild's member list is
#: incomplete, every look at an album or the ranking would otherwise ask Discord again for every
#: album owner who left the server (up to :data:`LOOKUP_LIMIT` requests, each a 404).
NOT_MEMBER_TTL = 600.0
#: ... and at most this many of them are remembered (the oldest one goes first).
NOT_MEMBER_LIMIT = 1000


def _clock() -> float:
    return time.monotonic()


def _not_members(bot: Any) -> dict[int, float]:
    memory = getattr(bot, "_album_not_members", None)
    if not isinstance(memory, dict):
        memory = {}
        bot._album_not_members = memory
    return memory


def _known_not_member(bot: Any, player_id: int) -> bool:
    """True while an earlier lookup said ``player_id`` is not on the server (and that is still fresh)."""
    memory = _not_members(bot)
    expires = memory.get(player_id)
    if expires is None:
        return False
    if expires <= _clock():
        del memory[player_id]
        return False
    return True


def _remember_not_member(bot: Any, player_id: int) -> None:
    memory = _not_members(bot)
    memory.pop(player_id, None)  # (re-inserted: the newest at the end)
    memory[player_id] = _clock() + NOT_MEMBER_TTL
    while len(memory) > NOT_MEMBER_LIMIT:
        del memory[next(iter(memory))]


def _is_not_member(exc: BaseException) -> bool:
    """Discord's answer for a user who is not on the server (left it): HTTP 404."""
    return isinstance(exc, discord.NotFound)


async def supporters_among(bot: "AlterBot", player_ids: Iterable[int], *,
                           viewer: Any = None) -> dict[int, Any] | None:
    """``player id -> member`` of those who support the game now; None when it cannot be known.

    Read from the guild's member cache (complete once the guild is chunked; role changes arrive
    as events).  ``viewer`` - the member who asks - is taken as he is.  A player missing from a
    complete cache left the server.  While the cache is incomplete, the missing ones are looked
    up one by one (only they, bounded); too many of them, or a lookup that fails for another
    reason than "not a member", and the answer is None (see the module docstring).  A "not a
    member" answer is believed for :data:`NOT_MEMBER_TTL` seconds; a player who joins meanwhile
    is in the member cache (the join is an event) and counts at once all the same.
    """
    guild = bot.get_guild(bot.ids.guild)
    if guild is None or getattr(guild, "unavailable", False):
        return None
    complete = bool(getattr(guild, "chunked", True))
    found: dict[int, Any] = {}
    missing: list[int] = []
    for player_id in dict.fromkeys(int(item) for item in player_ids):
        member = viewer if viewer is not None and int(viewer.id) == player_id else guild.get_member(player_id)
        if member is not None:
            if patrons.check_if_patron(member):
                found[player_id] = member
        elif not complete and not _known_not_member(bot, player_id):
            missing.append(player_id)
    if not missing:
        return found
    if len(missing) > LOOKUP_LIMIT:
        log.info("Album ranking: the member list is incomplete and %d album owners are not in it; "
                 "not available now.", len(missing))
        return None
    gate = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def look_up(player_id: int) -> Any:
        async with gate:
            try:
                return await io.retry(lambda: guild.fetch_member(player_id), attempts=2,
                                      what="fetch_member")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - sorted out below
                return exc

    answers = await asyncio.gather(*(look_up(player_id) for player_id in missing))
    for player_id, answer in zip(missing, answers):
        if isinstance(answer, BaseException):
            if _is_not_member(answer):
                _remember_not_member(bot, player_id)  # (not asked again for a while)
                continue  # left the server: not ranked
            log.warning("Album ranking: member %s could not be looked up (%s: %s); not available now.",
                        player_id, type(answer).__name__, answer)
            return None
        if patrons.check_if_patron(answer):
            found[player_id] = answer
    return found


def _order(item: tuple[int, Collection]) -> tuple:
    player_id, value = item
    return -value.points, -value.slots, value.newest, player_id


async def ranking(bot: "AlterBot", *, viewer: Any = None) -> Ranking | None:
    """The ranking of the supporters' collections; None when it is not available for a moment.

    ``viewer`` (the member who asks) counts as he is - he is a supporter whenever he may look.
    Raises :class:`DatabaseUnavailable` like every read.
    """
    rows = await _all_rows(bot.db)
    # (In a worker thread: a big album table is ~0.1 s of Python, and the event loop - a typed
    # fight on another channel - must not wait for it.)
    values = await asyncio.to_thread(collections, rows)
    members = await supporters_among(bot, values, viewer=viewer)
    if members is None:
        return None
    ranked = sorted(((player_id, value) for player_id, value in values.items() if player_id in members),
                    key=_order)
    return Ranking(tuple(Standing(place, player_id, members[player_id], value)
                         for place, (player_id, value) in enumerate(ranked, start=1)))


@dataclass(frozen=True)
class Overview:
    """What the album's overview says about the value: the points and the place (None: none /
    not known now - ``available`` tells which)."""

    points: int
    place: int | None = None
    total: int = 0
    available: bool = True


async def overview_of(bot: "AlterBot", viewer: Any, rows: Sequence[Any]) -> Overview:
    """The value of the viewer's album (from ``rows``, his entries) and his place.  Never raises:
    a ranking that cannot be made costs only the place line."""
    points = collection(rows).points
    if points <= 0:
        return Overview(0)
    try:
        board = await ranking(bot, viewer=viewer)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the album is shown without its place
        log.warning("Album of %s: the ranking could not be made (%s: %s).", getattr(viewer, "id", viewer),
                    type(exc).__name__, exc)
        board = None
    if board is None:
        return Overview(points, available=False)
    mine = board.of(viewer.id)
    if mine is None:
        return Overview(points, None, board.total)
    # The value beside the place is the one the place was given for (one source: a card kept or
    # removed between the two reads cannot make them disagree).
    return Overview(mine.points, mine.place, board.total)


# -- texts ---------------------------------------------------------------------------------------

MSG_SUPPORTERS_ONLY = ("Ranking kolekcji jest dla wspierających ({groups}) <:PepoG:790963160528977980>\n"
                       "Wesprzyj AlterMMO: {links}")
MSG_TITLE = "Najcenniejsze kolekcje towarzyszy!"
MSG_LINE = "{place} **{name}** · **{points} pkt** · {cards}"
MSG_OWN_LINE = "Twoje miejsce: **{place}.** z {total} · **{points} pkt** · {cards}"
MSG_OWN_NONE = "Twojej kolekcji jeszcze tu nie ma - pierwszy wpis w albumie (**$porzucam**) da Ci miejsce."
MSG_NOBODY = "Nikt jeszcze nie ma kolekcji w albumie - bądź pierwszy! <:peepoBlush:984769061340737586>"
#: How it works, without naming any ornament (owner's spoiler rule).
MSG_HOW = "Liczy się najrzadsza karta każdego gatunku na każdym etapie - im rzadsza, tym więcej punktów."
MSG_UNAVAILABLE = ("Ranking kolekcji jest chwilowo niedostępny - spróbuj ponownie za chwilę "
                   "<:Sadge:936907659142111273>")
#: The album's overview (features/album.py).
MSG_VALUE = "Wartość kolekcji: **{points} pkt**"
MSG_PLACE = "Miejsce w rankingu: **{place}.** (z {total})"
MSG_PLACE_UNKNOWN = "Miejsce w rankingu: chwilowo nieznane"
#: The entry's page (features/album.py).
MSG_CARD_VALUE = "Wartość karty: **{points} pkt**"
MSG_NOT_COUNTED = "*Ta karta się nie liczy: w albumie masz rzadszą kartę tego gatunku na tym samym etapie.*"
#: How many players the ranking lists.
TOP = 10


def points_text(points: int) -> str:
    """``987`` / ``1 234`` / ``12 345``: thousands apart by a no-break space (Polish style)."""
    return f"{int(points):,}".replace(",", chr(0xA0))


def cards_word(number: int) -> str:
    """``1 karta`` / ``3 karty`` / ``5 kart`` / ``22 karty``."""
    if number == 1:
        return "1 karta"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f"{number} karty"
    return f"{number} kart"


def supporters_only_text(member: Any) -> str:
    links = " • ".join(f"[{label}](<{url}>)" for label, url in patrons.SUPPORT_LINKS)
    text = MSG_SUPPORTERS_ONLY.format(groups=patrons.SUPPORTERS_LONG, links=links)
    return f"<@{member.id}> {text}" if member is not None else text


def _name(member: Any) -> str:
    """A ranked player as the other rankings show him: his name, escaped (cut to 32 characters)."""
    return discord.utils.escape_markdown(str(getattr(member, "name", "") or "?")[:32])


def ranking_embed(board: Ranking, viewer: Any) -> discord.Embed:
    """The top :data:`TOP`, and the viewer's own line when he is below it (or not ranked yet).

    Under them one line about the rank place 1 carries (:mod:`altermmo.features.collector`).
    """
    from . import collector  # (lazily: the collector builds on this module)

    lines = [MSG_LINE.format(place=ui.place(item.place), name=_name(item.member),
                             points=points_text(item.points), cards=cards_word(item.cards))
             for item in board.standings[:TOP]]
    if not lines:
        lines = [MSG_NOBODY]
    mine = board.of(viewer.id) if viewer is not None else None
    if mine is not None and mine.place > TOP:
        lines += ["", MSG_OWN_LINE.format(place=mine.place, total=board.total, points=points_text(mine.points),
                                          cards=cards_word(mine.cards))]
    elif mine is None and board.standings:
        lines += ["", MSG_OWN_NONE]
    embed = discord.Embed(title=MSG_TITLE, url=pets.RANKING_IMAGE, description="\n".join(lines),
                          color=0xFF0000)
    embed.set_thumbnail(url=pets.RANKING_IMAGE)
    # What place 1 carries (owner, 2026-11-23): the rank of features/collector.py.
    embed.add_field(name=collector.MSG_RANKING_NAME, value=collector.MSG_RANKING_LINE, inline=False)
    embed.set_footer(text=MSG_HOW)
    return embed


#: Nobody is ever pinged by the ranking (names are plain text, but a name is the player's text).
QUIET = discord.AllowedMentions.none()


async def show_ranking(scene: "Scene") -> bool:
    """``$rankingalbumow`` / the hub's "Rankingi": supporters only.  False when not shown."""
    member = scene.author
    if member is None or not patrons.check_if_patron(member):
        await scene.send(supporters_only_text(member))
        return False
    board = await ranking(scene.bot, viewer=member)
    if board is None:
        await scene.send(MSG_UNAVAILABLE)
        return False
    await scene.send(embed=ranking_embed(board, member), allowed_mentions=QUIET)
    return True
