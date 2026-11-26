"""Player items ("Przedmioty"): the item store, the registry, the drops and the ``!przedmioty`` view.

Owner's decisions (2026-11-26, ``items_decisions.md``):

* **one store** for the new items: the table ``PLAYER_ITEMS`` - one row per
  player and item, a counter that never goes below 0.  Rows are never
  deleted (a used-up item keeps its row at 0).  The Zwój przywołania is the
  one exception: it stays in ``PETOWNER.SUMMON_SCROLLS`` (the companion
  missions put it there first), so for it the registry only reads and writes
  that column through the pets module;
* **no caps anywhere**: a grant is a plain ``COUNT + n`` that makes the row
  when it is missing; a take is a conditional ``UPDATE ... WHERE COUNT >= n
  RETURNING`` that is never retried (``idempotent=False``) - a reply lost
  after the commit must not take a second item;
* **every item is used later, by a command** - nothing acts at the moment it
  drops (loot is granted while the fight's busy flag is held);
* **the loot ids** of the new items are 16-25 (:data:`ITEMS`; 21, 23 and 24
  are reserved and not built).  :func:`grant_drop` is the one dispatcher both
  loot functions use (``boss._grant_loot`` and ``daily._grant_loot``).
  Ids 26 and 27 are Dova's prizes (:data:`DOVA_PRIZES`): they never drop
  from a loot table - only a won game of Dova's cups gives them;
* **where a loot row drops**: two optional keys of a row in
  ``config/lootConfig.json`` / ``config/mobLootConfig.json`` -
  ``"sources": [...]`` (:data:`SOURCES`; a row without it drops everywhere,
  so every older row behaves as it always did) and ``"boost": false`` (the
  weight is not raised by the drop boost).  A row of weight 0 never drops
  (:func:`loot_weight`).  The source of a draw is the context variable
  :data:`LOOT_SOURCE`, set around the encounter of a summoned boss, a duel,
  a walkover, a tournament's lone player and a tower floor's loot - so no
  signature of the loot functions changed.

The view (``!przedmioty``, the hub's "🎒 Przedmioty"): read-only, it works
without a pet, it shows only the player's own items, and it is silent during
a typed fight on the channel (``guards.not_during_fight``; the hub's display
gate refuses the click).  Each line names the command that uses the item.

Where each item is used: the Pióro Żar-ptaka in ``pets.enlight_pet``, the
Przynęta in ``daily.start_hunt(bait=True)``, the Lina sztygara in the mine
(``dungeon``), the Klucz do wieży in ``tower.start_tower(use_key=True)`` -
and the Zwój wskrzeszenia here (:func:`revive`, ``!wskrzes @gracz``); Dova's
Kość and Karta in ``features/dova.py`` (``!kosc``, ``!karta``).  The
Nieuchwytny pył is the one item nobody uses by hand: it undoes its holder's
mistake in a typed fight by itself (:class:`DustGuard`, used by the single
and the legendary boss fight, the hunt and the tower).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, Sequence

import discord

from ..core import clock, fightlog, io, ui

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- the table ---------------------------------------------------------------------------

TABLE = "PLAYER_ITEMS"

SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        PLAYER_ID NUMERIC NOT NULL,
        ITEM      VARCHAR(32) NOT NULL,
        COUNT     INT NOT NULL DEFAULT 0 CHECK (COUNT >= 0),
        PRIMARY KEY (PLAYER_ID, ITEM))""",
)
#: New in v2: made at every start (``schema.create_new_tables``), and by :func:`table_ready` when
#: the database did not answer then.
NEW_TABLES: tuple[str, ...] = SCHEMA
#: The table has no columns that came later (yet).
NEW_COLUMNS: tuple[tuple[str, str, str], ...] = ()

#: Seconds after which a "the table is not there" is looked at again (as ``pets._READY_RECHECK``).
READY_RECHECK = 600.0

_SQL_GRANT = f"""INSERT INTO {TABLE} (PLAYER_ID, ITEM, COUNT) VALUES ($1, $2, $3)
                 ON CONFLICT (PLAYER_ID, ITEM) DO UPDATE SET COUNT = {TABLE}.COUNT + EXCLUDED.COUNT
                 RETURNING COUNT"""
_SQL_TAKE = f"""UPDATE {TABLE} SET COUNT = COUNT - $3
                WHERE PLAYER_ID = $1 AND ITEM = $2 AND COUNT >= $3 RETURNING COUNT"""
#: The owner's take: as many as there are, never below 0.
_SQL_TAKE_UP_TO = f"""UPDATE {TABLE} SET COUNT = GREATEST(COUNT - $3, 0)
                      WHERE PLAYER_ID = $1 AND ITEM = $2 RETURNING COUNT"""
_SQL_COUNT = f"SELECT COUNT FROM {TABLE} WHERE PLAYER_ID = $1 AND ITEM = $2"
_SQL_COUNTS = f"SELECT ITEM, COUNT FROM {TABLE} WHERE PLAYER_ID = $1"

# -- the registry ------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One kind of item: what the store, the drops, the view and the owner's command know about it."""

    #: The store's key (``PLAYER_ITEMS.ITEM``) and what ``!DajPrzedmiot`` takes.
    key: str
    #: Its id in the loot tables.
    loot_id: int
    #: Its name (singular), as the drops and the merchant say it.
    name: str
    #: The counter's label in ``!przedmioty`` (plural).
    label: str
    emoji: str
    #: How it is used: the command (markdown), as ``!przedmioty`` says it.
    hint: str
    #: One sentence about it, in the game's voice.
    description: str
    #: Kept in ``PETOWNER.SUMMON_SCROLLS`` (the Zwój przywołania), not in ``PLAYER_ITEMS``.
    in_petowner: bool = False


SUMMON_SCROLL = "summon_scroll"
TOWER_KEY = "tower_key"
FEATHER = "feather"
BAIT = "bait"
REVIVE_SCROLL = "revive_scroll"
ROPE = "rope"
DUST = "dust"
DOVA_DIE = "dova_die"
DOVA_CARD = "dova_card"
#: Dova's prizes (owner, 2026-11-26, ``items_decisions.md`` §12): only a won game of Dova's cups gives
#: them (``features/dova.py``) - no loot table has a row of them and the merchant never sells them.
#: Used with ``!kosc`` / ``!karta``.
DOVA_PRIZES: tuple[str, ...] = (DOVA_DIE, DOVA_CARD)

#: Every item, in the order ``!przedmioty`` lists them.  Loot ids 21, 23 and 24 are reserved
#: (:data:`RESERVED_LOOT_IDS`): such a row stays a text-only drop, as an unknown id always was.
ITEMS: tuple[Item, ...] = (
    Item(SUMMON_SCROLL, 16, "Zwój przywołania", "Zwoje przywołania", "📯", "**!przywolaj**",
         "Odczytany na kanale bossa sprowadza mitycznego bossa, którego zaatakować może każdy.",
         in_petowner=True),
    Item(TOWER_KEY, 17, "Klucz do wieży", "Klucze do wieży", "🗝️", "**!klucz**",
         "Zardzewiały, ale pasuje do Drzwi Wieży Śmierci."),
    Item(FEATHER, 18, "Pióro Żar-ptaka", "Pióra Żar-ptaka", "🪶", "**!oswiecenie**",
         "Wciąż tli się w nim ogień z dalekiej krainy."),
    Item(BAIT, 19, "Przynęta", "Przynęty", "🪤", "**!przyneta**",
         "Pachnie tak, że żaden potwór z puszczy się nie oprze."),
    Item(REVIVE_SCROLL, 20, "Zwój wskrzeszenia", "Zwoje wskrzeszenia", "🕊️", "**!wskrzes @gracz**",
         "Pomięty, ale słowa na nim wciąż mają moc."),
    Item(ROPE, 22, "Lina sztygara", "Liny sztygara", "🪢", "przyda się w **Kopalni Ostatniej Lampy**",
         "Mocna, konopna, pamięta niejedną sztolnię."),
    Item(DUST, 25, "Nieuchwytny pył", "Nieuchwytny pył", "✨", "nosisz go przy sobie - sam wie, kiedy się przydać",
         "Szczypta czegoś, co nie chce dać się złapać."),
    Item(DOVA_DIE, 26, "Kość Dovy", "Kości Dovy", "🎲", "**!kosc**",
         "Kanciasta i podejrzanie lekka - Dova przysięga na brodę swojej babki, że niczym jej nie obciążał."),
    Item(DOVA_CARD, 27, "Karta Dovy", "Karty Dovy", "🃏", "**!karta**",
         "Wytarta od tasowania; obrazek na niej zmienia się, gdy nikt nie patrzy."),
)
BY_KEY: Mapping[str, Item] = {item.key: item for item in ITEMS}
BY_LOOT_ID: Mapping[int, Item] = {item.loot_id: item for item in ITEMS}
#: Loot ids kept for items that are not built (List gończy, Korowaj weselny, Róg Strzyboga).
RESERVED_LOOT_IDS: tuple[int, ...] = (21, 23, 24)
#: Appended to the drop line of a new item (never to a "(Bonus)" copy; it says nothing about what it does).
DROP_HINT = "Twoje przedmioty pokaże **!przedmioty**."


def by_loot_id(loot_id: Any) -> Item | None:
    """The item of a loot row's id, or None (an older id, a reserved one, an unknown one)."""
    try:
        return BY_LOOT_ID.get(int(loot_id))
    except (TypeError, ValueError):
        return None


def _folded(text: str) -> str:
    """``text`` without case, Polish letters and spaces - how the owner may type an item's name."""
    text = unicodedata.normalize("NFKD", str(text).replace("ł", "l").replace("Ł", "L"))
    return "".join(char for char in text.casefold() if char.isalnum())


def find(text: str) -> Item | None:
    """The item ``text`` names: its key or its name (singular or plural), any case, with or without Polish letters."""
    wanted = _folded(text)
    if not wanted:
        return None
    for item in ITEMS:
        if wanted in (_folded(item.key), _folded(item.name), _folded(item.label)):
            return item
    return None


# -- where a loot row drops ----------------------------------------------------------------

#: The sources a loot row's ``"sources"`` may name.
SOURCE_BOSS = "boss"            # a scheduled boss (and the admin's !loot)
SOURCE_SUMMONED = "summoned"    # any summoned boss: the scroll, the gem, the ritual
SOURCE_DUEL = "duel"            # the duel event, the tournament, the walkover, a tournament's lone player
SOURCE_TOWER = "tower"          # a floor of the Death Tower
SOURCE_WORLD = "world"          # hunts, Mroczny las, the chest, the invasion, the party, memory, hazard,
#                                 the training's winner, Twitch
SOURCES: tuple[str, ...] = (SOURCE_BOSS, SOURCE_SUMMONED, SOURCE_DUEL, SOURCE_TOWER, SOURCE_WORLD)

#: Where the loot being drawn now comes from; None: the loot function's own default
#: (``boss.rand_loot``: :data:`SOURCE_BOSS`, ``daily.rand_loot``: :data:`SOURCE_WORLD`).
#: A context variable, so that no signature of the loot functions (and none of the tests'
#: fakes of them) had to change; set it with :func:`loot_source`.
LOOT_SOURCE: contextvars.ContextVar[str | None] = contextvars.ContextVar("altermmo_loot_source", default=None)


@contextlib.contextmanager
def loot_source(source: str) -> Iterator[None]:
    """Loot drawn inside the block comes from ``source`` (:data:`SOURCES`); reset afterwards, whatever happens."""
    token = LOOT_SOURCE.set(source)
    try:
        yield
    finally:
        LOOT_SOURCE.reset(token)


def current_source(default: str) -> str:
    """The source of the loot drawn now (:data:`LOOT_SOURCE`), ``default`` when none is set."""
    return LOOT_SOURCE.get() or default


def drops_here(loot: Mapping[str, Any], source: str) -> bool:
    """Whether a loot row may drop from ``source``: a row without ``"sources"`` drops everywhere."""
    sources = loot.get("sources")
    if sources is None:
        return True
    if isinstance(sources, str):
        sources = (sources,)
    return source in sources


def loot_weight(loot: Mapping[str, Any], boost: int, source: str) -> float:
    """The weight a loot row is drawn with (percent; 0 = it is not drawn at all, not even a die is rolled).

    0 when the row does not drop from ``source`` or its weight is not above 0 -
    ``random() * 100 <= 0`` used to be true when ``random()`` returned exactly
    0.0, and the new rows came with weight 0 before they were turned on.
    Otherwise the weight raised by ``boost`` percent, exactly as the loops
    always computed it - unless the row says ``"boost": false``.
    """
    if not drops_here(loot, source):
        return 0.0
    weight = loot["weight"]
    if not weight > 0:
        return 0.0
    if loot.get("boost", True) is False:
        return weight
    weight = (boost / 100 + 1) * weight
    return weight if weight > 0 else 0.0


# -- the store ---------------------------------------------------------------------------

def _pets():
    """The pets feature (imported on first use, like in ``features/boss.py``)."""
    from . import pets
    return pets


def _pid(player: Any) -> int:
    return int(getattr(player, "id", player))


def _int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError, ArithmeticError):
        return 0


async def table_ready(bot: "AlterBot", *, refresh: bool = False) -> bool:
    """True when ``PLAYER_ITEMS`` exists (cached on ``bot._player_items``; a "no" is looked at again after 10 minutes).

    Like ``pets.missions_table_ready``: a statement against a missing table
    would fail (and abort a whole PostgreSQL transaction), so every reader and
    writer asks this first - and a drop of a new item is then left out, not
    reported to the owner after every fight.  A "no" also tries to make the
    table (``CREATE TABLE IF NOT EXISTS``: the database did not answer at the
    start, when ``schema.create_new_tables`` makes it).  A "yes" is final;
    ``refresh=True`` looks again at once.
    """
    known = getattr(bot, "_player_items", None)
    if known is not None and (known[0] or (not refresh and clock.monotonic() - known[1] < READY_RECHECK)):
        return bool(known[0])
    ready = bool(await bot.db.fetchval(f"SELECT to_regclass('{TABLE.lower()}') IS NOT NULL"))
    if not ready:
        try:
            for statement in SCHEMA:
                await bot.db.execute(statement, idempotent=True)
            ready = bool(await bot.db.fetchval(f"SELECT to_regclass('{TABLE.lower()}') IS NOT NULL"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the new items are off until it works
            log.error("Table %s is missing and could not be made (%s: %s): the new items are off.",
                      TABLE, type(exc).__name__, exc)
        else:
            log.warning("Table %s was missing (not made at start-up); it is made now.", TABLE)
    bot._player_items = (ready, clock.monotonic())  # type: ignore[attr-defined]
    return ready


async def store_ready(bot: "AlterBot", item: Item) -> bool:
    """Whether ``item`` can be stored now: its column (``pets.mission_columns_available``) or :func:`table_ready`."""
    if item.in_petowner:
        return await _pets().mission_columns_available(bot)
    return await table_ready(bot)


def _item(key: str | Item) -> Item:
    return key if isinstance(key, Item) else BY_KEY[key]


async def count(bot: "AlterBot", player_id: Any, key: str | Item) -> int:
    """How many of ``key`` the player has (0 without a row, or while the store is not there)."""
    item = _item(key)
    if item.in_petowner:
        return await _pets().summon_scrolls(bot, player_id)
    if not await table_ready(bot):
        return 0
    return _int(await bot.db.fetchval(_SQL_COUNT, _pid(player_id), item.key))


async def counts(bot: "AlterBot", player_id: Any) -> dict[str, int]:
    """``{key: count}`` of the player's items in ``PLAYER_ITEMS`` (not the Zwój przywołania); {} without the table."""
    if not await table_ready(bot):
        return {}
    rows = await bot.db.fetch(_SQL_COUNTS, _pid(player_id))
    return {str(row["item"]): _int(row["count"]) for row in rows if str(row["item"]) in BY_KEY}


async def count_in(q: Any, player_id: int, key: str | Item, *, lock: bool = False) -> int:
    """How many of ``key`` the player has, read through ``q`` (a transaction's connection); 0 without a row.

    ``lock=True`` locks his row (``FOR UPDATE``) until the transaction ends, so a
    conditional take later in it finds what was counted.  Only for
    ``PLAYER_ITEMS`` items; the caller asked :func:`table_ready`.
    """
    item = _item(key)
    if item.in_petowner:
        raise ValueError(f"{item.key} lives in PETOWNER: use pets.summon_scrolls")
    return _int(await q.fetchval(_SQL_COUNT + (" FOR UPDATE" if lock else ""), int(player_id), item.key))


async def grant_in(con: Any, player_id: int, key: str | Item, number: int = 1) -> int:
    """Add ``number`` items inside the caller's transaction (the row is made when missing); the new count.

    Only for ``PLAYER_ITEMS`` items; the caller asked :func:`table_ready`.
    """
    item = _item(key)
    if item.in_petowner:
        raise ValueError(f"{item.key} lives in PETOWNER: use pets.add_summon_scroll_in")
    return _int(await con.fetchval(_SQL_GRANT, int(player_id), item.key, max(int(number), 0)))


async def take_in(con: Any, player_id: int, key: str | Item, number: int = 1) -> int | None:
    """Use ``number`` items inside the caller's transaction: the count left, or None when he has fewer.

    Nothing is taken when he has fewer than ``number``.  Only for ``PLAYER_ITEMS``
    items; the caller asked :func:`table_ready`.
    """
    item = _item(key)
    if item.in_petowner:
        raise ValueError(f"{item.key} lives in PETOWNER: use pets.take_summon_scroll")
    left = await con.fetchval(_SQL_TAKE, int(player_id), item.key, max(int(number), 1))
    return None if left is None else _int(left)


async def grant(bot: "AlterBot", player_id: Any, key: str | Item, number: int = 1) -> int:
    """Give the player ``number`` items (no cap: the row is made when missing); the new count.

    Never retried (``idempotent=False``): a reply lost after the commit must
    not give a second item - it raises ``DatabaseUnavailable`` instead.  The
    caller asked :func:`store_ready`.
    """
    item = _item(key)
    player_id, number = _pid(player_id), max(int(number), 0)
    if item.in_petowner:
        return await _pets().give_summon_scrolls(bot, player_id, number)
    return _int(await bot.db.fetchval(_SQL_GRANT, player_id, item.key, number, idempotent=False))


async def take(bot: "AlterBot", player_id: Any, key: str | Item, number: int = 1) -> int | None:
    """Use ``number`` items, atomically: the count left, or None when the player has fewer (nothing taken).

    Never retried (``idempotent=False``): a reply lost after the commit must
    not take a second item - it raises ``DatabaseUnavailable`` instead (the
    caller tells the owner).  The caller asked :func:`table_ready`; the Zwój
    przywołania has its own (``pets.take_summon_scroll``).
    """
    item = _item(key)
    if item.in_petowner:
        raise ValueError(f"{item.key} lives in PETOWNER: use pets.take_summon_scroll")
    left = await bot.db.fetchval(_SQL_TAKE, _pid(player_id), item.key, max(int(number), 1), idempotent=False)
    return None if left is None else _int(left)


async def adjust(bot: "AlterBot", player_id: Any, key: str | Item, delta: int) -> int:
    """The owner's ``!DajPrzedmiot``: add ``delta`` items, or take ``-delta`` (never below 0); the new count.

    The caller asked :func:`store_ready`.
    """
    item = _item(key)
    player_id, delta = _pid(player_id), int(delta)
    if item.in_petowner:
        return await _pets().give_summon_scrolls(bot, player_id, delta)
    if delta >= 0:
        return await grant(bot, player_id, item, delta)
    left = await bot.db.fetchval(_SQL_TAKE_UP_TO, player_id, item.key, -delta, idempotent=False)
    return _int(left)


async def grant_drop(bot: "AlterBot", hunter: Any, loot_id: Any) -> bool:
    """One new item from a loot table (ids 16-25, :data:`ITEMS`) for ``hunter``: whether it was given.

    False (the drop line is left out) only when the store is not there (the
    table is missing, or ``SUMMON_SCROLLS`` for the Zwój przywołania) - a
    database error raises, and the drop then tells the owner to give it by
    hand.  No cap: an item always fits.
    """
    item = by_loot_id(loot_id)
    if item is None:
        raise ValueError(f"loot id {loot_id!r} is no item of features/items.py")
    if not await store_ready(bot, item):
        log.warning("Loot %s (%s) for %s left out: its store is not there.", item.loot_id, item.key,
                    getattr(hunter, "id", hunter))
        return False
    left = await grant(bot, hunter, item)
    log.info("Player %s got the item %s from a drop (has %s now).", getattr(hunter, "id", hunter), item.key, left)
    return True


# -- the Nieuchwytny pył in a fight ----------------------------------------------------------
#
# Owner, 2026-11-26 (``items_decisions.md`` §7): in the single boss fight (a summoned boss's
# too), the legendary group fight, the hunt (also in the Mroczny las) and the tower (once per
# run) a mistake or a missed word of a dust holder costs nothing - "nic się nie dzieje": the
# round counts for nothing and the fight goes on.  Not in the duel, the tournament, training,
# events, spell races or sign-ups.  The fight tries everything else first (the pet's attack
# instead of the round, the pet's protection, the party's spare chance) - the rare dust is
# spent only when nothing else saved the player.

#: What the fight posts instead of the lost fight (``{name}``: the holder, escaped - nobody is pinged).
MSG_DUST_MISTAKE = ("Pomyliłeś się, ale **Nieuchwytny pył** gracza **{name}** rozwiewa się w powietrzu - nic się "
                    "nie stało! ✨")
MSG_DUST_TOO_SLOW = ("Nie zdążyłeś, ale **Nieuchwytny pył** gracza **{name}** rozwiewa się w powietrzu - nic się "
                     "nie stało! ✨")
#: To the owner (with a ping): the dust saved its holder, but taking it from the store failed.
LOG_DUST_UNSURE = ("Nieuchwytny pył uratował gracza <@{player_id}> w walce, ale baza danych nie odpowiedziała przy "
                   "jego zabieraniu (`{error}`) - nie wiadomo, czy zniknął z sakwy. Sprawdź PLAYER_ITEMS i w razie "
                   "potrzeby zabierz go: `!DajPrzedmiot @gracz nieuchwytny pył -1`")

#: Who of the fighters carries a dust - the one read before a fight (read-only; ``$1``: their ids).
_SQL_DUST_HOLDERS = (f"SELECT PLAYER_ID FROM {TABLE} WHERE ITEM = '{DUST}' AND COUNT > 0 "
                     "AND PLAYER_ID = ANY($1::numeric[])")


class DustGuard:
    """The Nieuchwytny pył of one fight (of one whole tower run), kept in memory.

    Nothing may wait for the database inside a round - it would stutter the
    fight (owner's rule).  So the fighters who carry a dust are read once,
    before the countdown (:func:`reserve_dust`), and a round only asks this
    guard: :meth:`take` is synchronous.  Each holder's dust works at most once
    per guard - five dusts in the pocket are not five lives in one fight; the
    tower makes one guard for the whole run.

    What :meth:`take` used is only remembered (:attr:`used`): it leaves the
    store after the fight is decided (:func:`settle_dust`).  A fight that is
    interrupted, cancelled or crashes before that takes nothing - in the
    player's favour, as "Nikt nie ponosi konsekwencji" says.
    """

    def __init__(self, holders: Iterable[Any] = ()) -> None:
        #: Ids of the fighters whose dust can still undo a mistake in this fight.
        self.left: set[int] = {_pid(holder) for holder in holders}
        #: Ids of the holders whose dust undid a mistake, in order (still in the store until settled).
        self.used: list[int] = []

    def take(self, player: Any) -> bool:
        """Use ``player``'s dust now: True when he had one left for this fight (it never works twice in it)."""
        try:
            player_id = _pid(player)
        except (TypeError, ValueError):
            return False
        if player_id not in self.left:
            return False
        self.left.discard(player_id)
        self.used.append(player_id)
        return True


async def reserve_dust(bot: "AlterBot", fighters: Iterable[Any]) -> DustGuard:
    """The :class:`DustGuard` of a fight about to start: who of ``fighters`` carries a dust.  Never raises.

    One read-only query, before the countdown - never inside a round.
    Nothing is taken here (:func:`settle_dust` does it, after the decision).
    A store that is not there, or a database that does not answer, gives an
    empty guard and a log line: the fight goes on as it would without the item.
    """
    ids: list[int] = []
    try:
        ids = sorted({_pid(fighter) for fighter in fighters})
        if not ids or not await table_ready(bot):
            return DustGuard()
        rows = await bot.db.fetch(_SQL_DUST_HOLDERS, ids)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the fight must not wait for (or fail on) an item
        log.warning("Dust: the holders among %s could not be read (%s: %s); no dust works in this fight.",
                    ids, type(exc).__name__, exc)
        return DustGuard()
    guard = DustGuard(row["player_id"] for row in rows)
    if guard.left:
        log.info("Dust: %s carry a Nieuchwytny pył into the fight.", sorted(guard.left))
    return guard


async def settle_dust(bot: "AlterBot", guard: DustGuard | None) -> None:
    """After the fight is decided: one dust less for every holder it saved (:attr:`DustGuard.used`).

    Never raises, and no database call at all when no dust was used.  Each
    is a conditional take, never retried (:func:`take`).  A holder who has none
    left by now (the owner's ``!DajPrzedmiot``) loses nothing - only a log
    line.  A database that did not answer: taken or not is unknown - the
    owner is pinged and checks.  The guard forgets what it settled, so a
    second call takes nothing more.
    """
    if guard is None or not guard.used:
        return
    used, guard.used = guard.used, []
    for player_id in used:
        try:
            left = await take(bot, player_id, DUST)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - taken or not: unknown, the owner checks
            log.exception("Dust: the Nieuchwytny pył of %s could not be taken after the fight.", player_id)
            with contextlib.suppress(Exception):
                await bot.log_to_owner(LOG_DUST_UNSURE.format(player_id=player_id, error=type(exc).__name__),
                                       ping=True)
            continue
        if left is None:
            log.warning("Dust: %s was saved by a Nieuchwytny pył but has none left to take.", player_id)
        else:
            log.info("Dust: a Nieuchwytny pył of %s was used up in a fight (%s left).", player_id, left)


def dust_culprit(result: Any, asked: Any, fighters: Sequence[Any]) -> Any:
    """Whose mistake a failed round was - whose dust may undo it.

    In a party fight (the hunt, the legendary boss) any fighter's message ends
    the round: when somebody else than ``asked`` typed (the journal's "wrong
    player"), the mistake is his.  Otherwise (a wrong word, silence) it is the
    asked player's.  The fighter object is returned (for his name), the
    message's author when he is not among ``fighters``.
    """
    author = getattr(result, "author", None)
    author_id = getattr(author, "id", None)
    if author is None or author_id == getattr(asked, "id", asked):
        return asked
    return next((fighter for fighter in fighters if getattr(fighter, "id", None) == author_id), author)


def dust_line(holder: Any, *, timed_out: bool) -> str:
    """What the fight posts when ``holder``'s dust undid the round (his name escaped; no mention)."""
    name = discord.utils.escape_mentions(discord.utils.escape_markdown(str(holder)))
    return (MSG_DUST_TOO_SLOW if timed_out else MSG_DUST_MISTAKE).format(name=name)


async def dust_saves(scene: "Scene", guard: DustGuard | None, holder: Any, *, timed_out: bool) -> bool:
    """A failed round of a fight: ``holder``'s Nieuchwytny pył undoes it when he has one left (True).

    Called by the fights after the pet's protection (and the party's spare
    chance) did not save the round.  Everything but the one message is
    synchronous, as with the pet's protection: the guard was read before the
    countdown, the journal notes the save (:data:`fightlog.SAVED_BY_DUST`),
    and the message goes through the fight's own channel - no extra notice,
    nobody pinged.  The caller then adds one to the life of the boss / the
    monster / the floor (the round counts for nothing) and asks the next word.
    False: no dust left for him in this fight (or no guard) - the fight is
    lost as before.
    """
    if guard is None or not guard.take(holder):
        return False
    fightlog.saved(scene.channel, fightlog.SAVED_BY_DUST)
    log.info("Dust: the Nieuchwytny pył of %s (%s) undid a %s.", holder, getattr(holder, "id", holder),
             "missed word" if timed_out else "mistake")
    await scene.send(dust_line(holder, timed_out=timed_out), allowed_mentions=discord.AllowedMentions.none())
    return True


# -- !przedmioty ---------------------------------------------------------------------------

VIEW_TITLE = "🎒 Przedmioty"
VIEW_COLOUR = 0xC08A3E
MSG_VIEW_HEAD = "Oto, co nosisz w sakwie, <@{player_id}>:"
MSG_EMPTY = ("Twoja sakwa świeci pustkami, <@{player_id}>. Ruszaj na **!polowanie** - potwory same się "
             "nie okradną!")
#: The old counters of ``PETOWNER`` (shown when above 0): ``(column, emoji, label, how it is used)``.
OLD_COUNTERS: tuple[tuple[str, str, str, str], ...] = (
    ("reroll_scroll", "📜", "Zwoje odrodzenia", "**!odrodzenie**, **!mocneodrodzenie**"),
    ("reroll_scroll_shard", "🧩", "Fragmenty zwojów",
     "10 fragmentów to pełny zwój (zamienią się przy **!towarzysz**)"),
    ("rebirth_stones", "💎", "Kamienie olśnienia", "**!oswiecenie**"),
    ("mirrors", "🪞", "Lustra wizualiów", "**!transformacja**"),
)
LINE = "{emoji} **{label}**: {count} - {hint}"
LINE_THEFTS = "🎯 **Kradzieże doświadczenia**: {count} - **!kradnijxp nick**"
LINE_POUCH = "👝 **Sakwa na jajko** - jajko, które się nie mieści, trafia do niej (**!wyjmijjajko**)"
LINE_BIG_POUCH = "🎒 **Duża sakwa** - mieści dwa jajka (**!wyjmijjajko**)"


async def view_lines(bot: "AlterBot", player_id: Any) -> list[str]:
    """The lines of ``!przedmioty`` for the player; [] when he has nothing at all (the empty view).

    Only what he has (owner, 2026-11-26: "Nie pokazuj zwojów i kluczy wieży,
    jeśli ktoś nie ma. Tak samo z innymi przedmiotami."): every counter and
    item with at least one, the XP thefts and the pouch when he has them.  A
    store that is not there counts as none.
    """
    from . import xpsteal

    pets = _pets()
    player_id = _pid(player_id)
    owner = await pets.owner_counters(bot, player_id)
    stored = await counts(bot, player_id)
    thefts = len(xpsteal.pending(bot, player_id))
    pouch = await pets.pouch_size(bot, player_id)
    lines = [LINE.format(emoji=emoji, label=label, count=_int(owner.get(column)), hint=hint)
             for column, emoji, label, hint in OLD_COUNTERS if _int(owner.get(column)) > 0]
    for item in ITEMS:
        number = _int(owner.get("summon_scrolls")) if item.in_petowner else stored.get(item.key, 0)
        if number > 0:
            lines.append(LINE.format(emoji=item.emoji, label=item.label, count=number, hint=item.hint))
    if thefts > 0:
        lines.append(LINE_THEFTS.format(count=thefts))
    if pouch > 0:
        lines.append(LINE_BIG_POUCH if pouch > 1 else LINE_POUCH)
    return lines


async def show_items(scene: "Scene", player: Any = None) -> bool:
    """``!przedmioty`` / the hub's "Przedmioty": the player's items on the channel; False when he has none.

    Read-only and public (owner: nothing "only for you"); the embed mentions
    only the player himself.  Works without a pet.
    """
    player_id = _pid(scene.author if player is None else player)
    lines = await view_lines(scene.bot, player_id)
    mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=player_id)])
    if not lines:
        await scene.send(MSG_EMPTY.format(player_id=player_id), allowed_mentions=mentions)
        return False
    embed = discord.Embed(title=VIEW_TITLE, description=MSG_VIEW_HEAD.format(player_id=player_id) + "\n\n"
                          + "\n".join(lines), color=VIEW_COLOUR)
    await scene.send(embed=embed, allowed_mentions=mentions)
    return True


# -- the Zwój wskrzeszenia (!wskrzes @gracz) --------------------------------------------------

MSG_REVIVED = ("🕊️ <@{reviver}> odczytuje nad <@{target}> **Zwój wskrzeszenia**! Poległy łowca kicha, otrzepuje się "
               "z kurzu i wraca do gry.")
MSG_NOT_DEAD = "<@{target}> żyje i ma się dobrze, <@{reviver}> - szkoda zwoju."
MSG_REVIVE_WHOM = "Wskaż poległego łowcę, <@{reviver}>: **!wskrzes @gracz**"
MSG_NO_REVIVE_SCROLL = ("Nie masz żadnego **Zwoju wskrzeszenia**, <@{reviver}>. Zwykle wypada z potworów i bossów - "
                        "albo kupisz go u Wędrownego kupca.")
#: Somebody else is reading a scroll over the same hunter right now (one scroll per fallen hunter).
MSG_REVIVE_TAKEN = ("Nad tym łowcą ktoś już odczytuje **Zwój wskrzeszenia**, <@{reviver}> - Twój zostaje w "
                    "sakwie.")
MSG_REVIVE_FAILED = ("Słowa zwoju rozwiały się bez echa, <@{reviver}> <:Sadge:936907659142111273> Zwój wskrzeszenia "
                     "wrócił do Twojej sakwy, a administracja dostała już znać.")
#: To the owner (with a ping).
LOG_REVIVE_FAILED = ("Zwój wskrzeszenia: nie udało się zdjąć roli poległego łowcy graczowi <@{target}> (odczytał go "
                     "<@{reviver}>). Zwój {refund}. Sprawdź rolę i uprawnienia bota.")
LOG_REVIVE_REFUNDED = "wrócił do gracza"
LOG_REVIVE_NOT_REFUNDED = ("NIE wrócił (`{error}`) - oddaj go: `!DajPrzedmiot @gracz zwój wskrzeszenia 1`")
LOG_REVIVE_UNSURE = ("Nie wiadomo, czy Zwój wskrzeszenia gracza <@{reviver}> został zabrany - baza danych nie "
                     "odpowiedziała (`{error}`), nikt nie został wskrzeszony. Sprawdź PLAYER_ITEMS i w razie potrzeby "
                     "oddaj zwój: `!DajPrzedmiot @gracz zwój wskrzeszenia 1`")

#: Ids of the fallen hunters a scroll is being read over right now (in memory: one scroll per hunter).
_reviving: set[int] = set()


def _is_dead(bot: "AlterBot", member: Any) -> bool:
    """Whether ``member`` has the dead-hunter role (whatever gave it: a lost fight, a duel, an assassin)."""
    role_id = bot.ids.dead_hunter_role
    return any(getattr(role, "id", None) == role_id for role in getattr(member, "roles", None) or ())


async def _say_revive(scene: "Scene", text: str, *people: Any) -> None:
    """Post a line of the scroll; only ``people`` are pinged.  Never raises (what happened, happened)."""
    try:
        await scene.send(text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                        users=list(people)))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Revive: a line could not be posted.")


async def revive(scene: "Scene", target: Any) -> bool:
    """``!wskrzes @gracz``: ``scene.author`` reads a Zwój wskrzeszenia over ``target``; True when he is back.

    Owner, 2026-11-26 (``items_decisions.md`` §5): it takes the dead-hunter
    role away, whatever gave it (a hunt or boss lost under "ban_loser", a lost
    duel, an assassin).  The order: the role is checked, the scroll taken
    conditionally (never retried), the role checked again (the boss's reset
    may have revived him meanwhile: the scroll comes back) and removed.  A
    role that cannot be removed gives the scroll back and pings the owner; a
    database that did not answer the take gives nothing back on a guess (the
    owner checks).  One scroll per fallen hunter: while one is read over him,
    another reader is refused (:data:`_reviving`, in memory).  A dead hunter
    cannot write on the channel, so nobody revives himself (a dead reader that
    gets through anyway is ignored).  Only the reader is pinged, also by the
    line that brings the hunter back (owner rule: a player text mentions only
    the acting player; ``<@target>`` still shows the revived hunter's name).
    """
    bot, reviver = scene.bot, scene.author
    reviver_id = _pid(reviver)
    if target is None:
        await _say_revive(scene, MSG_REVIVE_WHOM.format(reviver=reviver_id), reviver)
        return False
    if _is_dead(bot, reviver):
        log.info("Revive: %s is a fallen hunter himself; nothing done.", reviver_id)
        return False
    target_id = _pid(target)
    if not _is_dead(bot, target):
        await _say_revive(scene, MSG_NOT_DEAD.format(target=target_id, reviver=reviver_id), reviver)
        return False
    if target_id in _reviving:
        await _say_revive(scene, MSG_REVIVE_TAKEN.format(reviver=reviver_id), reviver)
        return False
    _reviving.add(target_id)
    try:
        return await _revive(scene, target)
    finally:
        _reviving.discard(target_id)


async def _revive(scene: "Scene", target: Any) -> bool:
    """The scroll and the role of :func:`revive` (under its guard)."""
    bot, reviver = scene.bot, scene.author
    reviver_id, target_id = _pid(reviver), _pid(target)
    try:
        ready = await table_ready(bot)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - nothing was taken
        log.exception("Revive: the item store could not be asked for the scroll of %s.", reviver_id)
        await _say_revive(scene, ui.MSG_FAILED, reviver)
        return False
    try:
        left = await take(bot, reviver_id, REVIVE_SCROLL) if ready else None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - taken or not: unknown, the owner checks
        log.exception("Revive: the scroll of %s could not be taken.", reviver_id)
        await bot.log_to_owner(LOG_REVIVE_UNSURE.format(reviver=reviver_id, error=type(exc).__name__), ping=True)
        await _say_revive(scene, ui.MSG_FAILED, reviver)
        return False
    if left is None:
        await _say_revive(scene, MSG_NO_REVIVE_SCROLL.format(reviver=reviver_id), reviver)
        return False
    role = bot.role(bot.ids.dead_hunter_role)
    if not _is_dead(bot, target):  # (revived meanwhile, e.g. the boss's reset: the scroll was not needed)
        await _give_back(bot, reviver_id, target_id, told_owner=False)
        await _say_revive(scene, MSG_NOT_DEAD.format(target=target_id, reviver=reviver_id), reviver)
        return False
    if role is None or not await io.remove_role(target, role, reason="AlterMMO RPG: Zwój wskrzeszenia"):
        await _give_back(bot, reviver_id, target_id, told_owner=True)
        await _say_revive(scene, MSG_REVIVE_FAILED.format(reviver=reviver_id), reviver)
        return False
    log.info("Revive: %s (%s) revived %s (%s); %s scroll(s) left.", reviver, reviver_id, target, target_id, left)
    # Only the reader is pinged (owner rule: a player text mentions only the acting player); <@target>
    # still shows the revived hunter's name.
    await _say_revive(scene, MSG_REVIVED.format(reviver=reviver_id, target=target_id), reviver)
    return True


async def _give_back(bot: "AlterBot", reviver_id: int, target_id: int, *, told_owner: bool) -> None:
    """The scroll of a revival that did not happen goes back.  Never raises.

    ``told_owner``: the role could not be removed - the owner is pinged with
    the outcome of the refund; otherwise only a refund that failed reaches him.
    """
    error = ""
    try:
        await grant(bot, reviver_id, REVIVE_SCROLL)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Revive: the scroll of %s could not be given back.", reviver_id)
        error = type(exc).__name__
    if told_owner or error:
        refund = LOG_REVIVE_NOT_REFUNDED.format(error=error) if error else LOG_REVIVE_REFUNDED
        with contextlib.suppress(Exception):
            await bot.log_to_owner(LOG_REVIVE_FAILED.format(target=target_id, reviver=reviver_id, refund=refund),
                                   ping=True)
