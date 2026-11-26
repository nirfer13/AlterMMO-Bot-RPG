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
  loot functions use (``boss._grant_loot`` and ``daily._grant_loot``);
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
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Mapping

import discord

from ..core import clock

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
    #: Shown in ``!przedmioty`` also when the player has none.
    always_shown: bool = False


SUMMON_SCROLL = "summon_scroll"
TOWER_KEY = "tower_key"
FEATHER = "feather"
BAIT = "bait"
REVIVE_SCROLL = "revive_scroll"
ROPE = "rope"
DUST = "dust"

#: Every item, in the order ``!przedmioty`` lists them.  Loot ids 21, 23 and 24 are reserved
#: (:data:`RESERVED_LOOT_IDS`): such a row stays a text-only drop, as an unknown id always was.
ITEMS: tuple[Item, ...] = (
    Item(SUMMON_SCROLL, 16, "Zwój przywołania", "Zwoje przywołania", "📯", "**!przywolaj**",
         "Odczytany na kanale bossa sprowadza mitycznego bossa, którego zaatakować może każdy.",
         in_petowner=True, always_shown=True),
    Item(TOWER_KEY, 17, "Klucz do wieży", "Klucze do wieży", "🗝️", "**!klucz**",
         "Zardzewiały, ale pasuje do Drzwi Wieży Śmierci.", always_shown=True),
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
    0.0, and the new rows ship with weight 0.  Otherwise the weight raised by
    ``boost`` percent, exactly as the loops always computed it - unless the row
    says ``"boost": false``.
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


# -- !przedmioty ---------------------------------------------------------------------------

VIEW_TITLE = "🎒 Przedmioty"
VIEW_COLOUR = 0xC08A3E
MSG_VIEW_HEAD = "Oto, co nosisz w sakwie, <@{player_id}>:"
MSG_EMPTY = ("Twoja sakwa świeci pustkami, <@{player_id}>. Ruszaj na **!polowanie** - potwory same się "
             "nie okradną!")
#: The old counters of ``PETOWNER`` (always shown): ``(column, emoji, label, how it is used)``.
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

    The old counters, the Zwoje przywołania and the Klucze do wieży are always
    listed; the XP thefts and the pouch when he has them; every other item
    when he has at least one.  A store that is not there counts as none.
    """
    from . import xpsteal

    pets = _pets()
    player_id = _pid(player_id)
    owner = await pets.owner_counters(bot, player_id)
    stored = await counts(bot, player_id)
    thefts = len(xpsteal.pending(bot, player_id))
    pouch = await pets.pouch_size(bot, player_id)
    lines = [LINE.format(emoji=emoji, label=label, count=_int(owner.get(column)), hint=hint)
             for column, emoji, label, hint in OLD_COUNTERS]
    numbers = [_int(owner.get(column)) for column, *_ in OLD_COUNTERS]
    for item in ITEMS:
        number = _int(owner.get("summon_scrolls")) if item.in_petowner else stored.get(item.key, 0)
        numbers.append(number)
        if item.always_shown or number > 0:
            lines.append(LINE.format(emoji=item.emoji, label=item.label, count=number, hint=item.hint))
    if thefts > 0:
        lines.append(LINE_THEFTS.format(count=thefts))
    if pouch > 0:
        lines.append(LINE_BIG_POUCH if pouch > 1 else LINE_POUCH)
    if not any(number > 0 for number in numbers) and thefts <= 0 and pouch <= 0:
        return []
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
