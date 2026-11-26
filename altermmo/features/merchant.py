"""The "Wędrowny kupiec" event: a merchant sells one to three goods for reroll scrolls.

Owner's request of 2026-11-22 (passed on by the owner's other Claude session):

* the merchant comes like the other random events (the events cog draws him,
  ``!SpawnEvent merchant`` calls him), at most once a day in the draw
  (owner, 2026-11-23; :func:`came_today` - a forced one counts as well);
* he is PASSIVE like the chest (owner, 2026-11-23): the channel is held only
  while his panel is posted, then the event stays open
  (``state.open_event``) and the channel is free - hunts, the tower and the
  stable work.  A purchase waits while a hunt / tower run / event plays on
  the channel (``ui.MSG_BUSY``, as the chest).  Whoever closes the event
  meanwhile (the next event, the boss, the tournament) sends him away
  without a word: he does not post on a channel that belongs to someone else now;
* he has 1 ware (usually) or 2-3 different ones (rarely) - drawn from
  ``config/merchantConfig.json`` (prices, draw weights, how many wares, how
  long he stays, the stable's limit), read every time he comes;
* a ware with ``price_spread_percent`` in the config (owner, 2026-11-23: "make
  prices in merchant store random + - 30% for egg bag and egg") costs something
  else at every visit: its price is drawn ONCE when he comes
  (:func:`price_range`, :func:`draw_stock`) and is the price of that visit - on
  the panel, on the button, at the payment, at a refund and in the logs
  (``Offer.price``).  A visit is not kept over a restart, so there is no
  drawn price to restore;
* every ware sells ONCE, to the first player who can pay for it: the clicks
  of the panel are queued (:class:`ui.ClickView`) and this one coroutine
  handles them one after another, so two players clicking at the same moment
  cannot both buy - the second is told the ware is sold;
* a purchase that cannot be made (too few scrolls, the player does not meet
  the ware's condition) takes nothing and leaves the ware on sale; the player
  gets the reason.  The scrolls are taken atomically before the ware is handed
  over; a ware that turns out not to fit at that moment gives them back;
* when everything is sold he leaves at once; otherwise after his time
  (``stay_seconds``, 15 s in debug mode);
* pictures: ``assets/events/merchant/0.*`` (arrival, inside his panel) and
  ``1.*`` (leaving, posted when he goes) - ``.gif`` first, then ``.png``,
  ``.jpg``, ``.jpeg``, ``.webp``.  Without them he comes and goes with words
  only (:func:`picture`).

The goods are :data:`WARES`: an id (the ``id`` in the config), an emoji, a
description, a check that runs before any scroll is taken and the hand-over
itself.  A new ware is one more :class:`Ware` here plus its entry in the
config file.  A ware that needs something in the database (:attr:`Ware.needs`:
a column, the item store) is not offered while it is missing
(:func:`available_stores`).

Since 2026-11-26 (``items_decisions.md``) most new wares are ITEMS
(:attr:`Ware.item`): the purchase puts them into the player's items
(``features/items.py``) - nothing acts at the counter, so the hand-over works
even before the command that uses the item exists.  The ticket of old
(``tower_ticket``) is the stored Klucz do wieży now; the Zwój przywołania
comes "from under the counter".  The new wares came with weight 0 (the config
is read live by the running bot) and a separate, last commit turned them on at
the owner's weights (items_decisions.md §9).
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Collection, Mapping, Sequence

import discord

from ..core import clock, io, timing, ui
from ..core.scene import Scene
from ..core.state import EventType
from ..settings import CONFIG_DIR
from . import daily, events, items, pets, skills

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot

log = logging.getLogger(__name__)

CONFIG_FILE = CONFIG_DIR / "merchantConfig.json"
#: Where the owner puts the pictures (the convention of the other events: ``assets/events/<event>/<n>.*``).
PICTURES_DIR = events.EVENTS_DIR / "merchant"
ARRIVAL, DEPARTURE = "0", "1"
#: Looked for in this order: an animation first.
PICTURE_SUFFIXES = (".gif", ".png", ".jpg", ".jpeg", ".webp")
#: How long the merchant stays in debug mode (production: ``stay_seconds`` of the config).
STAY_DEBUG = 15
#: How often (seconds) the merchant looks whether his event was closed by somebody else.
WATCH_EVERY = 2.0
#: ``runtime.json``: the day (``YYYY-MM-DD``, local time) the merchant last came (one a day).
DAY_KEY = "merchant_last_day"
#: Config key of a ware: by how many percent its price may differ from ``price``, up and down,
#: from visit to visit (absent or 0: the fixed price).
SPREAD_KEY = "price_spread_percent"
#: A spread must stay below this: at 100 % the price could fall to nothing.
MAX_PRICE_SPREAD = 100.0

COLOUR = 0x8B5A2B
BAG = "🧳"
TITLE = f"{BAG} Wędrowny kupiec! {BAG}"
DESCR = ("Na kanał zawitał wędrowny kupiec. Rozkłada towar na wozie i zachwala go na cały głos.\n\n"
         "**Płacisz zwojami odrodzenia. Każdą rzecz kupiec sprzeda tylko raz - kto pierwszy, ten lepszy!**")
WARE_LINE = "{emoji} **{name}** - {price}\n*{about}*"
SOLD_LINE = "~~{emoji} {name} - {price}~~ - kupuje {mention}"
MSG_STAYS_UNTIL = "⏳ Kupiec odjedzie {when}."
MSG_SOLD_OUT = "🚶 Kupiec sprzedał wszystko i rusza w dalszą drogę."
MSG_LEAVES = "🚶 Kupiec zwija kram i rusza w dalszą drogę."
MSG_GONE_TEXT = "*Skrzypiący wóz kupca znika za zakrętem...*"
BUY_LABEL = "{name} ({price})"
SOLD_LABEL = "Sprzedane"

MSG_BOUGHT = "🛒 {mention} kupuje **{name}** za {price}! {detail}"
MSG_ALREADY_SOLD = "Ktoś był szybszy - to już sprzedane <:Sadge:936907659142111273>"
MSG_POOR = "Za mało zwojów odrodzenia: **{name}** kosztuje {price}, a masz {have}."
MSG_FAILED = ("Coś poszło nie tak przy wydawaniu towaru <:Sadge:936907659142111273> "
              "Administracja dostała już znać.")
LOG_FAILED = ("Wędrowny kupiec: gracz <@{player_id}> zapłacił {price} za **{name}**, ale wydanie towaru przerwał "
              "błąd (`{error}`). Towar uznano za sprzedany - sprawdź w logu, czy gracz go dostał, i ewentualnie "
              "oddaj zwoje albo przyznaj towar ręcznie.")
LOG_NOT_RETURNED = ("Wędrowny kupiec: gracz <@{player_id}> nie mógł dostać **{name}**, ale zwrot {price} nie "
                    "powiódł się (`{error}`). Oddaj mu zwoje ręcznie.")
# The same for everybody who clicks: one line on the channel is enough (see ui.reply).
ui.GENERAL_NOTICES.update({MSG_ALREADY_SOLD})


def scrolls(number: int) -> str:
    """``1 zwój`` / ``25 zwojów`` (the game's wording, ``pets._scrolls``)."""
    return pets._scrolls(int(number))


# -- the goods ---------------------------------------------------------------------------

#: ``(bot, player id, offer) -> None`` when the player may buy, else the reason (nothing is taken).
Check = Callable[["AlterBot", int, "Offer"], Awaitable[str | None]]
#: ``(scene of the buyer, offer) -> (handed over, text)``: the text is the detail of the sale,
#: or the reason why the ware did not fit after all (the scrolls are given back).
Hand = Callable[[Scene, "Offer"], Awaitable[tuple[bool, str]]]


#: What a ware may need in the database to be offered at all (:attr:`Ware.needs`, :func:`draw_stock`'s
#: ``available``, :func:`available_stores`): the ``PETOWNER`` columns of the stable places and the egg
#: pouch (``pets.upgrades_available``), the column of the Zwoje przywołania
#: (``pets.mission_columns_available``), the table ``PLAYER_ITEMS`` (``items.table_ready``).
NEEDS_UPGRADES, NEEDS_MISSIONS, NEEDS_ITEMS = "upgrades", "missions", "items"
NEEDS: frozenset[str] = frozenset({NEEDS_UPGRADES, NEEDS_MISSIONS, NEEDS_ITEMS})


@dataclass(frozen=True)
class Ware:
    """One kind of goods the merchant can bring."""

    id: str
    emoji: str
    about: str
    check: Check
    hand: Hand
    #: What it needs in the database (:data:`NEEDS`); without it the ware never comes.
    needs: frozenset[str] = frozenset()
    #: The item (a key of ``features/items.py``) the ware puts into the player's items - '' for
    #: the wares that act at once.
    item: str = ""


@dataclass
class Offer:
    """One ware on the merchant's cart: its config entry and who bought it."""

    ware: Ware
    name: str
    #: What it costs at THIS visit (a ware with a price spread: drawn once by :func:`draw_stock`);
    #: the panel, the button, the payment, a refund and the logs all read it from here.
    price: int
    options: Mapping[str, Any] = field(default_factory=dict)
    buyer: Any = None

    @property
    def sold(self) -> bool:
        return self.buyer is not None


@dataclass
class Purchase:
    """What became of one click on a ware."""

    sold: bool
    text: str = ""
    failed: bool = False


# egg pouch

MSG_HAS_POUCH = "Masz już sakwę na jajko - druga się nie przyda."
DETAIL_POUCH = ("Od teraz jajko, które zdobędziesz, mając przy sobie towarzysza, trafi do sakwy "
                "(wyjmiesz je komendą **!wyjmijjajko**).")


async def _check_pouch(bot: "AlterBot", player_id: int, offer: Offer) -> str | None:
    return MSG_HAS_POUCH if await pets.has_egg_pouch(bot, player_id) else None


async def _hand_pouch(scene: Scene, offer: Offer) -> tuple[bool, str]:
    if await pets.give_egg_pouch(scene.bot, scene.author.id):
        return True, DETAIL_POUCH
    return False, MSG_HAS_POUCH


# random egg

MSG_NO_EGG_ROOM = ("Nie masz gdzie przyjąć jajka: towarzysz jest przy Tobie, a wolnej sakwy na jajko nie masz. "
                   "Schowaj towarzysza do stajni (**!schowajtowarzysza numer**) i kliknij jeszcze raz.")
DETAIL_EGG = "Jajko jest Twoje - sprawdź je komendą **!towarzysz** albo **!stajnia**."


async def _check_egg(bot: "AlterBot", player_id: int, offer: Offer) -> str | None:
    return None if await pets.egg_room(bot, player_id) else MSG_NO_EGG_ROOM


async def _hand_egg(scene: Scene, offer: Offer) -> tuple[bool, str]:
    # Exactly the egg of a drop (``daily._grant_loot``): rolled, stored, announced the same way.
    if await pets.assign_pet(scene, scene.author):
        return True, DETAIL_EGG
    return False, MSG_NO_EGG_ROOM


# stable place

MSG_STABLE_FULL = "Twoja stajnia ma już {places} miejsc - większa nie będzie."
DETAIL_STABLE = "Stajnia ma teraz {places} miejsc."


def _stable_limit(offer: Offer) -> int:
    try:
        limit = int(offer.options.get("max_places", pets.STABLE_MAX_PLACES))
    except (TypeError, ValueError):
        limit = pets.STABLE_MAX_PLACES
    return max(pets.STABLE_BASE_PLACES, min(limit, pets.STABLE_MAX_PLACES))


async def _check_stable(bot: "AlterBot", player_id: int, offer: Offer) -> str | None:
    places = await pets.stable_places(bot, player_id)
    return MSG_STABLE_FULL.format(places=places) if places >= _stable_limit(offer) else None


async def _hand_stable(scene: Scene, offer: Offer) -> tuple[bool, str]:
    places = await pets.add_stable_place(scene.bot, scene.author.id, _stable_limit(offer))
    if places:
        return True, DETAIL_STABLE.format(places=places)
    return False, MSG_STABLE_FULL.format(places=await pets.stable_places(scene.bot, scene.author.id))


# random skill gem

MSG_ALL_GEMS = "Masz już każdy rodzaj klejnotu, jaki można zdobyć - kupiec nie ma dla Ciebie nic nowego."
DETAIL_GEM = "Nowy klejnot czeka w **!klejnoty**."


async def _check_gem(bot: "AlterBot", player_id: int, offer: Offer) -> str | None:
    return None  # (skills.assign_skill decides under its lock; a refusal gives the scrolls back)


async def _hand_gem(scene: Scene, offer: Offer) -> tuple[bool, str]:
    if await skills.assign_skill(scene, scene.author):
        return True, DETAIL_GEM
    return False, MSG_ALL_GEMS


# rest kit (the hunt's daily cooldown)

MSG_HUNT_READY = "Dziś jeszcze możesz zapolować - zestaw do wypoczynku przyda się dopiero po polowaniu."
DETAIL_REST = "Możesz znowu zapolować (**!polowanie**), gdy kupiec odjedzie."


async def _check_rest(bot: "AlterBot", player_id: int, offer: Offer) -> str | None:
    return None if daily.load_daily_from_file(bot, player_id) else MSG_HUNT_READY


async def _hand_rest(scene: Scene, offer: Offer) -> tuple[bool, str]:
    if await daily.remove_player_daily_file(scene.bot, [scene.author.id]):
        return True, DETAIL_REST
    return False, MSG_HUNT_READY


# an item for the player's items (features/items.py): the Klucz do wieży, the Zwój przywołania and the
# other items of 2026-11-26.  Stored, never used at the purchase (owner: every item is used later, by a
# command - a summon would end the merchant's own visit); no cap, so nothing is refused for having many.
# The hand-over only adds to the store: it works before the command that uses the item exists.

MSG_STORE_OFF = "Ten towar chwilowo nie trafi do Twoich przedmiotów - kupiec zatrzyma go dla kogoś innego."
#: The ticket of old, stored since 2026-11-26 as the Klucz do wieży (``{count}``: the keys he has now).
DETAIL_TOWER = ("🗝️ Klucz trafia do Twoich przedmiotów (masz ich teraz: {count}). Gdy Drzwi Wieży Śmierci będą "
                "dla Ciebie zamknięte, otworzysz je komendą **!klucz**.")
DETAIL_SUMMON = ("📯 Zwój trafia do Twoich przedmiotów (masz ich teraz: {count}) - odczytasz go na kanale bossa "
                 "komendą **!przywolaj**.")
DETAIL_ITEM = "Trafia do Twoich przedmiotów (masz teraz: {count}) - zajrzysz do nich komendą **!przedmioty**."
#: The sale's detail of an item ware (default: :data:`DETAIL_ITEM`).
_ITEM_DETAILS: dict[str, str] = {items.TOWER_KEY: DETAIL_TOWER, items.SUMMON_SCROLL: DETAIL_SUMMON}


async def _check_item(bot: "AlterBot", player_id: int, offer: Offer) -> str | None:
    """Refused only while the item's store is not there (``items.store_ready``): no cap."""
    return None if await items.store_ready(bot, items.BY_KEY[offer.ware.item]) else MSG_STORE_OFF


async def _hand_item(scene: Scene, offer: Offer) -> tuple[bool, str]:
    """One item more (``items.grant``: one statement, never retried - an error counts as sold, the owner checks)."""
    item = items.BY_KEY[offer.ware.item]
    if not await items.store_ready(scene.bot, item):
        return False, MSG_STORE_OFF
    count = await items.grant(scene.bot, scene.author.id, item)
    return True, _ITEM_DETAILS.get(item.key, DETAIL_ITEM).format(count=count)


def _item_ware(ware_id: str, key: str, about: str | None = None, *, needs: str = NEEDS_ITEMS) -> Ware:
    """The ware of an item of features/items.py (its emoji; ``about``: default its description)."""
    item = items.BY_KEY[key]
    text = about if about is not None else f"{item.description} Trafia do Twoich przedmiotów."
    return Ware(ware_id, item.emoji, text, _check_item, _hand_item, needs=frozenset({needs}), item=key)


#: Every ware the merchant can bring, by the ``id`` of ``config/merchantConfig.json``.  (The wares
#: after the gem came on 2026-11-26, first with weight 0 in the config, then turned on there.)
WARES: dict[str, Ware] = {ware.id: ware for ware in (
    Ware("egg_pouch", "👝", "Gdy masz przy sobie towarzysza, nowe jajko trafi do sakwy, zamiast przepaść. "
         "W sakwie jajko nie rośnie - żeby je wychować, wyjmij je.", _check_pouch, _hand_pouch,
         needs=frozenset({NEEDS_UPGRADES})),
    Ware("random_egg", "🥚", "Losowe jajko, takie jak z łupu.", _check_egg, _hand_egg),
    Ware("stable_place", "🐴", "Jedno miejsce więcej w Twojej stajni.", _check_stable, _hand_stable,
         needs=frozenset({NEEDS_UPGRADES})),
    Ware("random_gem", "💎", "Losowy klejnot umiejętności - takiego rodzaju, jakiego jeszcze nie masz.",
         _check_gem, _hand_gem),
    Ware("rest_kit", "🛏️", "Pozwala od razu znowu zapolować.", _check_rest, _hand_rest),
    # (The config id of the "Wejściówka do wieży" of old: since 2026-11-26 the stored Klucz do wieży.)
    _item_ware("tower_ticket", items.TOWER_KEY,
               "Otwiera Drzwi Wieży Śmierci ponownie, gdy w tym tygodniu już tam byłeś. Trafia do Twoich "
               "przedmiotów - użyjesz go, kiedy zechcesz."),
    _item_ware("summon_scroll", items.SUMMON_SCROLL,
               "Spod lady. Kupiec zarzeka się, że takich rzeczy nie sprzedaje - ten jeden „znalazł na "
               "gościńcu”. Trafia do Twoich przedmiotów.", needs=NEEDS_MISSIONS),
    _item_ware("feather", items.FEATHER),
    _item_ware("bait", items.BAIT),
    _item_ware("revive_scroll", items.REVIVE_SCROLL),
    _item_ware("rope", items.ROPE),
    _item_ware("dust", items.DUST),
)}


async def available_stores(bot: "AlterBot") -> frozenset[str]:
    """Which of :data:`NEEDS` the database has now (:func:`draw_stock`'s ``available``).  Never raises.

    A check that fails counts as "not there": its wares stay at home for this
    visit, the others come.
    """
    checks = {NEEDS_UPGRADES: pets.upgrades_available, NEEDS_MISSIONS: pets.mission_columns_available,
              NEEDS_ITEMS: items.table_ready}
    available = set()
    for need, check in checks.items():
        try:
            if await check(bot):
                available.add(need)
        except Exception:  # noqa: BLE001 - those wares stay at home this time
            log.exception("Merchant: cannot tell whether %r is there.", need)
    return frozenset(available)


# -- the config ----------------------------------------------------------------------------

class ConfigError(ValueError):
    """``config/merchantConfig.json`` cannot be used (the event is skipped; the owner is told by the cog)."""


@dataclass(frozen=True)
class Config:
    stay_seconds: float
    #: ``{how many wares: weight}``.
    stock_weights: Mapping[int, float]
    #: ``(id, name, price, weight, the whole entry)`` of every ware in the file, in its order.
    items: tuple[tuple[str, str, int, float, Mapping[str, Any]], ...]


def load_config(path: Path | None = None) -> Config:
    """Read and check the merchant's config; :class:`ConfigError` when it is unusable."""
    path = CONFIG_FILE if path is None else path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stay = float(raw["stay_seconds"])
        stock_weights = {int(count): float(weight) for count, weight in raw["stock_weights"].items()}
        items = []
        for entry in raw["items"]:
            ware_id = str(entry["id"])
            if ware_id not in WARES:
                raise ConfigError(f"unknown ware {ware_id!r} (known: {', '.join(WARES)})")
            price, weight = int(entry["price"]), float(entry.get("weight", 0))
            if price < 0 or weight < 0:
                raise ConfigError(f"ware {ware_id!r}: negative price or weight")
            price_spread(entry)  # (a spread that makes no sense is refused here, not at the draw)
            items.append((ware_id, str(entry.get("name") or ware_id), price, weight, dict(entry)))
    except ConfigError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f"{path.name}: {type(exc).__name__}: {exc}") from exc
    if stay <= 0 or not stock_weights or any(count < 1 or weight < 0 for count, weight in stock_weights.items()) \
            or not any(stock_weights.values()):
        raise ConfigError(f"{path.name}: stay_seconds / stock_weights make no sense")
    if len({item[0] for item in items}) != len(items):
        raise ConfigError(f"{path.name}: a ware is listed twice")
    return Config(stay, stock_weights, tuple(items))


def price_spread(entry: Mapping[str, Any]) -> float:
    """``price_spread_percent`` of a ware's config entry (0.0: none, the price is fixed).

    :class:`ConfigError` when it is no number, negative, or :data:`MAX_PRICE_SPREAD` and more.
    """
    raw = entry.get(SPREAD_KEY, 0)
    try:
        if isinstance(raw, bool):
            raise TypeError("true / false is no number")
        spread = float(0 if raw is None else raw)
    except (TypeError, ValueError, OverflowError) as exc:  # (OverflowError: a whole number of hundreds of digits)
        raise ConfigError(f"ware {entry.get('id')!r}: {SPREAD_KEY} is no number ({_shown(raw)})") from exc
    if not 0 <= spread < MAX_PRICE_SPREAD:  # (also "nan" and "inf", which compare false)
        raise ConfigError(f"ware {entry.get('id')!r}: {SPREAD_KEY} must be 0 or more and below "
                          f"{MAX_PRICE_SPREAD:g} (it is {_shown(raw)})")
    return spread


def _shown(value: Any) -> str:
    """A config value for an error text: its ``repr``, cut short (the text goes to the owner's log)."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - e.g. a whole number too long to be printed at all
        text = f"<{type(value).__name__}>"
    return text if len(text) <= 60 else text[:57] + "..."


def _half_up(value: Decimal) -> int:
    """``value`` as a whole number, a half rounded UP (Python's ``round`` would make 32.5 the even 32)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def price_range(price: int, spread_percent: float) -> tuple[int, int]:
    """The lowest and the highest price of a ware listed at ``price`` with ``spread_percent``.

    ``price`` less and plus that many percent, each bound rounded half up to whole scrolls
    (decimal arithmetic on the numbers as written: 30 % of 25 is 7.5, so 17.5 -> 18 and
    32.5 -> 33) and never below 1: 25 with 30 % is 18..33, 250 with 30 % is 175..325.  No
    spread - or a ware that costs nothing - is ``(price, price)``.
    """
    price = int(price)
    if price <= 0 or spread_percent <= 0:
        return price, price
    part = Decimal(price) * Decimal(str(spread_percent)) / Decimal(100)
    low = max(1, _half_up(Decimal(price) - part))
    return low, max(low, _half_up(Decimal(price) + part))


def draw_price(price: int, spread_percent: float, rng: Any = random) -> int:
    """The price of one visit: any whole number of :func:`price_range`, each as likely as the other.

    One ``rng.randint`` - and none at all for a ware whose price is fixed.
    """
    low, high = price_range(price, spread_percent)
    return low if low == high else rng.randint(low, high)


def draw_stock(config: Config, *, available: Collection[str] | None = None, upgrades: bool = True,
               rng: random.Random | None = None) -> list[Offer]:
    """The merchant's cart: how many wares (``stock_weights``), then that many DIFFERENT ones by weight.

    Wares with weight 0 never come; neither does a ware whose :attr:`Ware.needs`
    the database does not have - ``available`` (:func:`available_stores`;
    None: everything is there).  ``upgrades=False`` (the older way to say it)
    takes the ``PETOWNER`` columns of the stable place and the egg pouch out
    of ``available``.  Fewer wares when fewer can come; an empty list when
    none can.  (Which wares are left out changes nothing in the draw of the
    others: they are out of the pool before the first die.)

    The price of a ware with a spread (``price_spread_percent``) is drawn here,
    once for the visit (:func:`draw_price`).  The prices are drawn AFTER the
    wares, in the order of the cart: which wares come for a given seed is what
    it was before prices could vary, and a cart without such a ware draws
    nothing more.
    """
    rng = rng or random
    have = set(NEEDS if available is None else available)
    if not upgrades:
        have.discard(NEEDS_UPGRADES)
    pool = [(item, item[3]) for item in config.items if item[3] > 0 and WARES[item[0]].needs <= have]
    counts = sorted(config.stock_weights)
    count = rng.choices(counts, weights=[config.stock_weights[c] for c in counts])[0]
    chosen: list[Offer] = []
    while pool and len(chosen) < count:
        index = rng.choices(range(len(pool)), weights=[weight for _, weight in pool])[0]
        (ware_id, name, price, _weight, entry), _ = pool.pop(index)
        chosen.append(Offer(WARES[ware_id], name, price, entry))
    for offer in chosen:
        offer.price = draw_price(offer.price, price_spread(offer.options), rng)
    return chosen


# -- buying --------------------------------------------------------------------------------

async def buy(scene: Scene, offer: Offer) -> Purchase:
    """``scene.author`` buys ``offer``: check, pay, hand over.  The caller makes sure it is not sold yet.

    Nothing is taken when the check refuses or the scrolls are too few.  The
    scrolls are taken before the hand-over; a ware that does not fit after all
    gives them back.  A hand-over that raises counts as SOLD (owner's rule: what
    is to happen once happens once - the reward may have been granted already);
    the owner is pinged to check it.
    """
    bot = scene.bot
    player = scene.author
    reason = await offer.ware.check(bot, int(player.id), offer)
    if reason is not None:
        return Purchase(False, reason)
    paid, have = await pets.take_scrolls(bot, player.id, offer.price)
    if not paid:
        return Purchase(False, MSG_POOR.format(name=offer.name, price=scrolls(offer.price), have=scrolls(have)))
    try:
        handed, text = await offer.ware.hand(scene, offer)
    except Exception as exc:  # noqa: BLE001 - paid: the ware counts as sold, the owner checks
        log.exception("Merchant: %s paid %s for %s, but the hand-over failed.", player.id, offer.price, offer.ware.id)
        await bot.log_to_owner(LOG_FAILED.format(player_id=player.id, price=scrolls(offer.price), name=offer.name,
                                                 error=type(exc).__name__), ping=True)
        return Purchase(True, MSG_FAILED, failed=True)
    if not handed:
        try:
            await pets.return_scrolls(bot, player.id, offer.price)
        except Exception as exc:  # noqa: BLE001 - the player must not lose the scrolls unnoticed
            log.exception("Merchant: %s scrolls of %s could not be given back.", offer.price, player.id)
            await bot.log_to_owner(LOG_NOT_RETURNED.format(player_id=player.id, price=scrolls(offer.price),
                                                           name=offer.name, error=type(exc).__name__), ping=True)
        log.info("Merchant: %s could not get %s after paying; %s scrolls given back.", player.id, offer.ware.id,
                 offer.price)
        return Purchase(False, text)
    log.info("Merchant: %s (%s) bought %s for %s scrolls.", player, player.id, offer.ware.id, offer.price)
    return Purchase(True, text)


# -- the pictures --------------------------------------------------------------------------

def picture(which: str) -> Path | None:
    """``assets/events/merchant/<which>.<gif|png|jpg|jpeg|webp>`` (first found), or None.

    Looked up every time: a picture the owner adds works without a restart.
    """
    for suffix in PICTURE_SUFFIXES:
        path = PICTURES_DIR / f"{which}{suffix}"
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


# -- the event -----------------------------------------------------------------------------

def buy_key(index: int) -> str:
    """Button key of the ``index``-th ware of the cart."""
    return f"buy{index}"


def _panel(stock: Sequence[Offer], *, ends_at: datetime, status: str | None = None,
           picture_name: str | None = None) -> discord.Embed:
    lines = []
    for offer in stock:
        values = {"emoji": offer.ware.emoji, "name": offer.name, "price": scrolls(offer.price),
                  "about": offer.ware.about}
        if offer.sold:
            lines.append(SOLD_LINE.format(mention=offer.buyer.mention, **values))
        else:
            lines.append(WARE_LINE.format(**values))
    text = status if status is not None else MSG_STAYS_UNTIL.format(when=ui.when(ends_at))
    embed = discord.Embed(title=TITLE, description=f"{DESCR}\n\n" + "\n\n".join(lines) + f"\n\n{text}",
                          color=COLOUR)
    if picture_name is not None:
        embed.set_image(url=f"attachment://{picture_name}")
    return embed


def came_today(bot: "AlterBot") -> bool:
    """True when the merchant has already come today (the events cog leaves him out of the draw)."""
    try:
        return bot.store.runtime_get(DAY_KEY) == clock.now().date().isoformat()
    except Exception:  # noqa: BLE001 - a broken note must not stop the events
        log.exception("Cannot read when the merchant came last.")
        return False


def _remember_today(bot: "AlterBot") -> None:
    try:
        bot.store.runtime_set(DAY_KEY, clock.now().date().isoformat())
    except Exception:  # noqa: BLE001 - at worst he comes twice that day
        log.exception("Cannot note that the merchant came today.")


def still_here(bot: "AlterBot") -> bool:
    """True while the merchant's event is open (nobody - the next event, the boss... - closed it)."""
    state = bot.state
    return state.event_alive and state.event_type is EventType.MERCHANT


async def spawn_merchant(scene: Scene) -> list[Offer]:
    """The merchant comes, sells what he can and leaves; returns his cart (``Offer.buyer`` set when sold).

    Passive like the chest: the channel is held only while his panel is
    posted; then his event stays open and this coroutine sells until he is
    sold out, his time is up or somebody else closes the event (then he
    leaves without a word).  An empty cart (every ware switched off in the
    config) means he does not come at all.  :class:`ConfigError` propagates
    (the cog tells the owner).
    """
    bot = scene.bot
    state = bot.state
    config = load_config()
    stock = draw_stock(config, available=await available_stores(bot))
    if not stock:
        log.warning("Merchant skipped: no ware can come (check %s).", CONFIG_FILE.name)
        return []
    log.info("Merchant spawn: %s.", ", ".join(f"{offer.ware.id} for {offer.price}" for offer in stock))
    seconds = timing.timeout(STAY_DEBUG if bot.debug else config.stay_seconds)
    ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    arrival = picture(ARRIVAL)
    shown = None if arrival is None else arrival.name
    choices = [ui.Choice(buy_key(index), BUY_LABEL.format(name=offer.name, price=scrolls(offer.price))[:80],
                         emoji=offer.ware.emoji, style=discord.ButtonStyle.success)
               for index, offer in enumerate(stock)]

    async def not_now(interaction: Any, key: str) -> str | None:
        # As the chest: no purchase while a hunt / tower run / event plays on the channel.
        return ui.MSG_BUSY if state.activity_running else None

    sent_away = False
    async with ui.ClickView(choices, allow=not_now) as view:
        with state.hold_busy("event-spawn:MERCHANT"):
            await io.clear_channel(scene.channel)
            extra = {} if arrival is None else {"file_path": arrival}
            await view.send(scene.channel, embed=_panel(stock, ends_at=ends_at, picture_name=shown), **extra)
        state.open_event(EventType.MERCHANT)
        _remember_today(bot)
        try:
            deadline = time.monotonic() + seconds
            while not all(offer.sold for offer in stock):
                if not still_here(bot):
                    sent_away = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                click = await view.next(min(remaining, WATCH_EVERY))
                if click is None:
                    continue
                if not still_here(bot):
                    await ui.reply(click.interaction, ui.MSG_STALE)
                    sent_away = True
                    break
                offer = stock[int(click.key.removeprefix("buy"))]
                if offer.sold:
                    await ui.reply(click.interaction, MSG_ALREADY_SOLD)
                    continue
                member = await bot.member(click.member.id)
                if member is None:  # left the server meanwhile
                    continue
                buyer = Scene(bot=bot, channel=scene.channel, author=member)
                try:
                    purchase = await buy(buyer, offer)
                except Exception:  # noqa: BLE001 - e.g. the database is gone: nothing was taken, he stays
                    log.exception("Merchant: the purchase of %s by %s failed before anything was taken.",
                                  offer.ware.id, member.id)
                    await ui.reply(click.interaction, ui.MSG_FAILED)
                    continue
                if not purchase.sold:
                    await ui.reply(click.interaction, purchase.text)
                    continue
                offer.buyer = member
                button = view.button(click.key)
                button.disabled, button.label, button.style = True, SOLD_LABEL, discord.ButtonStyle.secondary
                await view.refresh(embed=_panel(stock, ends_at=ends_at, picture_name=shown))
                if purchase.failed:
                    await ui.reply(click.interaction, purchase.text)
                else:
                    await scene.send(MSG_BOUGHT.format(mention=member.mention, name=offer.name,
                                                       price=scrolls(offer.price), detail=purchase.text))
        finally:
            if still_here(bot):
                state.close_event()
        late = view.pending()
        if sent_away:
            # The channel belongs to whoever closed the event: no words, only the buttons go.
            await view.close()
        else:
            status = MSG_SOLD_OUT if all(offer.sold for offer in stock) else MSG_LEAVES
            await view.close(embed=_panel(stock, ends_at=ends_at, status=status, picture_name=shown))
        for click in late:
            await ui.reply(click.interaction, MSG_ALREADY_SOLD if stock[int(click.key[3:])].sold
                           else ui.MSG_STALE)

    sold = [offer.ware.id for offer in stock if offer.sold]
    log.info("Merchant leaves (sold: %s)%s.", ", ".join(sold) or "nothing", " - sent away" if sent_away else "")
    if sent_away:
        return stock
    departure = picture(DEPARTURE)
    if departure is not None:
        await scene.send(file_path=departure)
    else:
        await scene.send(MSG_GONE_TEXT)
    return stock
