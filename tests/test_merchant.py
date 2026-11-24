"""The "Wędrowny kupiec" event (features/merchant.py), the egg pouch and the bigger stable (features/pets.py)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import Counter

import pytest

from altermmo.cogs import events as events_cog
from altermmo.core import timing, ui
from altermmo.core.state import EventType
from altermmo.features import album, daily, merchant, patrons, pets, skills, tower

from test_album_note import EMPTY_NOTE, force, new_egg, notes, recorded

# -- helpers -------------------------------------------------------------------------------

#: The config the owner ships (``config/merchantConfig.json``).  He retunes it without a restart, so
#: only ONE test reads it - the pin ``test_the_shipped_config_has_every_ware_at_the_owners_price``.
SHIPPED_CONFIG = merchant.CONFIG_FILE
#: The tests' OWN config: every other test here (the rules of the draw, the cart, buying, the refund)
#: runs on these numbers (``own_config``), so a retune of the shipped file never turns a behaviour test
#: red.  (Today they are the owner's numbers, which the pin test says.)
OWN_CONFIG = {
    "stay_seconds": 600,
    "stock_weights": {"1": 80, "2": 15, "3": 5},
    "items": [
        {"id": "random_egg", "name": "Losowe jajko", "price": 25, "price_spread_percent": 30, "weight": 30},
        {"id": "rest_kit", "name": "Zestaw do wypoczynku", "price": 10, "weight": 30},
        {"id": "stable_place", "name": "Powiększenie stajni", "price": 100, "weight": 25, "max_places": 10},
        {"id": "egg_pouch", "name": "Sakwa na jajko", "price": 250, "price_spread_percent": 30, "weight": 10},
        {"id": "tower_ticket", "name": "Wejściówka do wieży", "price": 50, "weight": 3},
        {"id": "random_gem", "name": "Losowy klejnot", "price": 1000, "weight": 2},
    ],
}
PRICES = {"egg_pouch": 250, "random_egg": 25, "stable_place": 100, "random_gem": 1000, "rest_kit": 10,
          "tower_ticket": 50}
#: The wares whose price is drawn at every visit (owner, 2026-11-23: "+ - 30% for egg bag and egg") and
#: what they can cost: 25 less / plus 30 % is 17.5 / 32.5, a half rounded up; 250 less / plus 30 % is 175 / 325.
PRICE_RANGES = {"random_egg": (18, 33), "egg_pouch": (175, 325)}


@pytest.fixture(autouse=True)
def own_config(monkeypatch, tmp_path):
    """The merchant reads the tests' own config (:data:`OWN_CONFIG`), through the real loader."""
    path = tmp_path / "own-merchantConfig.json"
    path.write_text(json.dumps(OWN_CONFIG, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(merchant, "CONFIG_FILE", path)
    return path


@pytest.fixture
def quick(monkeypatch):
    """Waiting times 100x shorter (the merchant stays 15 s in debug mode -> 0.15 s)."""
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.01)


@pytest.fixture
def no_pictures(monkeypatch, tmp_path):
    """The merchant's picture folder is an empty folder of this test (the owner has not supplied any)."""
    folder = tmp_path / "merchant-pictures"
    monkeypatch.setattr(merchant, "PICTURES_DIR", folder)
    return folder


@pytest.fixture
def cart(monkeypatch, no_pictures):
    """``cart("rest_kit", ...)``: the merchant brings exactly these wares at the list prices of the tests'
    config (nothing is drawn here, no price either - see ``visit`` for the real draw)."""
    chosen: list[str] = []

    def draw(config, *, upgrades=True, rng=None):
        by_id = {item[0]: item for item in config.items}
        return [merchant.Offer(merchant.WARES[ware], by_id[ware][1], by_id[ware][2], by_id[ware][4])
                for ware in chosen]

    monkeypatch.setattr(merchant, "draw_stock", draw)

    def choose(*wares: str) -> None:
        chosen[:] = wares

    return choose


def only(*wares: str) -> merchant.Config:
    """The tests' config with nothing but ``wares`` on sale, all of them in every cart."""
    real = merchant.load_config()
    return merchant.Config(real.stay_seconds, {len(wares): 1},
                           tuple(item for item in real.items if item[0] in wares))


@pytest.fixture
def visit(monkeypatch, no_pictures):
    """``visit(seed, "random_egg", ...)``: the merchant brings exactly these wares through the REAL draw
    (``merchant.draw_stock`` with the module's own source of luck, seeded with ``seed``); returns the cart
    that draw gives - its order and its prices - replayed from the same seed."""
    def arrange(seed: int, *wares: str) -> list[merchant.Offer]:
        config = only(*wares)
        monkeypatch.setattr(merchant, "load_config", lambda path=None: config)
        monkeypatch.setattr(merchant, "random", random.Random(seed))
        return merchant.draw_stock(config, rng=random.Random(seed))

    return arrange


async def until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def make_owner(db, player_id: int, *, scrolls: int = 0, pet_id: int = 0, **columns) -> None:
    """A PETOWNER row (``columns``: e.g. ``stable_places=9``, ``egg_pouch=True``, ``pet_id_alt1=3``)."""
    await db.execute("INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED, REROLL_SCROLL, REROLL_SCROLL_SHARD, "
                     "REBIRTH_STONES, MIRRORS, SKILL_GEM, PET_ID_ALT1, PET_ID_ALT2, PET_ID_ALT3, PET_ID_ALT4) "
                     "VALUES ($1, $2, TRUE, $3, 0, 0, 0, 0, 0, 0, 0, 0)", player_id, pet_id, scrolls)
    for column, value in columns.items():
        await db.execute(f"UPDATE PETOWNER SET {column.upper()} = $2 WHERE PLAYER_ID = $1", player_id, value)


async def make_pet(db, pet_id: int, *, level: int = 0, name: str = "Towarzysz") -> None:
    await db.execute(
        """INSERT INTO PETS (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY, MYTHIC, TYPE,
               VARIANT, CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC, LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION)
           VALUES ($1, $2, $3, 0, 'Standard', FALSE, FALSE, FALSE, 'Cat', '1', 0, 0, 0, 0, 0, 0, 0, FALSE)""",
        pet_id, name, level)


async def scrolls_of(db, player_id: int) -> int:
    return int(await db.fetchval("SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = $1", player_id))


async def row(db, player_id: int) -> dict:
    record = await db.fetchrow("SELECT * FROM PETOWNER WHERE PLAYER_ID = $1", player_id)
    return {key: (int(value) if hasattr(value, "as_integer_ratio") and not isinstance(value, bool) else value)
            for key, value in record.items()}


def start(game):
    return asyncio.ensure_future(merchant.spawn_merchant(game.scene()))


# -- config and the draw ---------------------------------------------------------------------

def test_the_shipped_config_has_every_ware_at_the_owners_price():
    """THE pin of ``config/merchantConfig.json``: the one test a retune of that file turns red."""
    config = merchant.load_config(SHIPPED_CONFIG)
    assert SHIPPED_CONFIG.name == "merchantConfig.json" and SHIPPED_CONFIG.parent.name == "config"
    assert {item[0]: item[2] for item in config.items} == PRICES
    assert set(PRICES) == set(merchant.WARES)
    weights = {item[0]: item[3] for item in config.items}
    # often: egg, rest kit, stable place; middling: the pouch; rarely: the tower ticket and the gem
    assert min(weights["random_egg"], weights["rest_kit"], weights["stable_place"]) > weights["egg_pouch"] \
        > max(weights["tower_ticket"], weights["random_gem"]) > 0
    stable = next(item for item in config.items if item[0] == "stable_place")
    assert stable[4]["max_places"] == 10 == pets.STABLE_MAX_PLACES
    assert config.stay_seconds == 600
    # mostly one ware, rarely two or three
    assert set(config.stock_weights) == {1, 2, 3} and config.stock_weights[1] > config.stock_weights[2] + \
        config.stock_weights[3]
    # The egg and the egg pouch cost something else at every visit, by up to 30 %; the rest is fixed.
    spreads = {item[0]: merchant.price_spread(item[4]) for item in config.items}
    assert {ware for ware, spread in spreads.items() if spread} == set(PRICE_RANGES) == {"random_egg", "egg_pouch"}
    assert spreads["random_egg"] == spreads["egg_pouch"] == 30
    assert all(merchant.SPREAD_KEY not in item[4] for item in config.items if item[0] not in PRICE_RANGES)


def test_every_other_test_runs_on_the_tests_own_numbers(own_config):
    """The owner retunes the shipped file without a restart: a behaviour test must not depend on it."""
    assert merchant.CONFIG_FILE == own_config != SHIPPED_CONFIG
    config = merchant.load_config()                                      # (what the merchant reads when he comes)
    assert [(ware, name, price, weight) for ware, name, price, weight, _ in config.items] == [
        (item["id"], item["name"], item["price"], item["weight"]) for item in OWN_CONFIG["items"]]
    assert {item[0]: item[2] for item in config.items} == PRICES
    assert {item[0]: merchant.price_range(item[2], merchant.price_spread(item[4])) for item in config.items
            if merchant.price_spread(item[4])} == PRICE_RANGES
    assert only("rest_kit").items == tuple(item for item in config.items if item[0] == "rest_kit")


def test_a_broken_config_is_refused(tmp_path):
    path = tmp_path / "merchantConfig.json"
    good = json.loads(merchant.CONFIG_FILE.read_text(encoding="utf-8"))
    for broken in ({**good, "items": [*good["items"], {"id": "unicorn", "price": 1}]},
                   {**good, "items": [{**good["items"][0], "price": -5}]},
                   {**good, "stock_weights": {"0": 1}},
                   {key: value for key, value in good.items() if key != "items"}):
        path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(merchant.ConfigError):
            merchant.load_config(path)
    path.write_text("{nie json", encoding="utf-8")
    with pytest.raises(merchant.ConfigError):
        merchant.load_config(path)


@pytest.mark.parametrize("spread", [-1, -0.5, 100, 100.0, 250, 1e9, float("nan"), float("inf"), float("-inf"),
                                    "dużo", "", True, False, [30], {"percent": 30},
                                    10 ** 400, -10 ** 400, "1" + "0" * 400],      # too long for a float
                         ids=lambda spread: repr(spread)[:24])
def test_a_price_spread_that_makes_no_sense_is_refused(tmp_path, spread):
    path = tmp_path / "merchantConfig.json"
    good = json.loads(merchant.CONFIG_FILE.read_text(encoding="utf-8"))
    egg = next(item for item in good["items"] if item["id"] == "random_egg")
    egg["price_spread_percent"] = spread
    path.write_text(json.dumps(good), encoding="utf-8")
    with pytest.raises(merchant.ConfigError, match="random_egg.*price_spread_percent"):
        merchant.load_config(path)
    # ... and a cart never gets a price out of it either (a config built by hand).
    with pytest.raises(merchant.ConfigError):
        merchant.price_spread({"id": "random_egg", "price": 25, "price_spread_percent": spread})


@pytest.mark.parametrize("spread, expected", [(30, 30.0), (12.5, 12.5), ("30", 30.0), (0, 0.0), (None, 0.0),
                                              (99.9, 99.9), (0.5, 0.5)])
def test_a_price_spread_is_a_number_from_nothing_to_just_below_the_whole_price(tmp_path, spread, expected):
    path = tmp_path / "merchantConfig.json"
    good = json.loads(merchant.CONFIG_FILE.read_text(encoding="utf-8"))
    egg = next(item for item in good["items"] if item["id"] == "random_egg")
    egg["price_spread_percent"] = spread
    path.write_text(json.dumps(good), encoding="utf-8")
    config = merchant.load_config(path)
    entry = next(item for item in config.items if item[0] == "random_egg")
    assert merchant.price_spread(entry[4]) == expected and entry[2] == 25      # the list price stays the list price
    assert merchant.price_spread({"id": "rest_kit", "price": 10}) == 0.0       # absent: the fixed price


def test_the_bounds_of_a_drawn_price():
    """From 70 % to 130 % of the list price, the bounds rounded half UP to whole scrolls, never below 1."""
    assert merchant.price_range(25, 30) == (18, 33) == PRICE_RANGES["random_egg"]      # 17.5 and 32.5
    assert merchant.price_range(250, 30) == (175, 325) == PRICE_RANGES["egg_pouch"]    # 175.0 and 325.0
    assert merchant.price_range(25, 30.0) == (18, 33) and merchant.price_range(250, 30.0) == (175, 325)
    assert merchant.price_range(15, 30) == (11, 20)                       # 10.5 and 19.5: both halves go up
    assert merchant.price_range(5, 30) == (4, 7)                          # 3.5 and 6.5
    assert merchant.price_range(10, 30) == (7, 13) and merchant.price_range(100, 30) == (70, 130)
    assert merchant.price_range(1000, 30) == (700, 1300)
    assert merchant.price_range(25, 12.5) == (22, 28)                     # 21.875 and 28.125
    assert merchant.price_range(10, 1.15) == (10, 10)                     # 9.885 and 10.115: nothing to draw
    assert merchant.price_range(1, 30) == (1, 1) and merchant.price_range(2, 30) == (1, 3)
    assert merchant.price_range(1, 99.9) == (1, 2) and merchant.price_range(3, 99) == (1, 6)   # never below 1
    # No spread - and a ware that costs nothing - is the price itself.
    assert merchant.price_range(25, 0) == (25, 25) and merchant.price_range(250, 0.0) == (250, 250)
    assert merchant.price_range(0, 30) == (0, 0)
    # The rule, for every price the merchant could ask and every whole spread.
    for price in range(1, 1100):
        for spread in (1, 5, 10, 30, 50, 75, 99):
            low, high = merchant.price_range(price, spread)
            exact_low, exact_high = price * (100 - spread), price * (100 + spread)      # in hundredths
            assert low == max(1, (exact_low + 50) // 100) and high == max(low, (exact_high + 50) // 100)
            assert 1 <= low <= price <= high


@pytest.mark.parametrize("ware", ["random_egg", "egg_pouch"])
def test_every_price_of_the_range_can_come_and_none_outside_it(ware):
    low, high = PRICE_RANGES[ware]
    config = only(ware)
    rng = random.Random(11)
    drawn = Counter()
    for _ in range(40000):
        (offer,) = merchant.draw_stock(config, rng=rng)
        drawn[offer.price] += 1
    assert sorted(drawn) == list(range(low, high + 1))                   # 16 prices of the egg, 151 of the pouch
    even = 40000 / (high - low + 1)
    assert 0.7 * even < min(drawn.values()) and max(drawn.values()) < 1.3 * even      # each as likely as the other
    # The same through the one-price helper; a fixed price draws nothing at all.
    assert {merchant.draw_price(PRICES[ware], 30, rng) for _ in range(5000)} == set(range(low, high + 1))
    state = rng.getstate()
    assert merchant.draw_price(PRICES[ware], 0, rng) == PRICES[ware] and rng.getstate() == state
    assert merchant.draw_price(1, 30, rng) == 1 and rng.getstate() == state


def test_wares_without_a_spread_cost_what_they_cost_and_the_cart_is_the_one_it_was():
    """The prices are drawn after the wares: which wares come for a seed did not change, and a cart
    without the egg and the pouch draws nothing more than it did before prices could vary."""
    config = merchant.load_config()
    fixed = merchant.Config(config.stay_seconds, config.stock_weights, tuple(
        (ware, name, price, weight, {key: value for key, value in entry.items() if key != merchant.SPREAD_KEY})
        for ware, name, price, weight, entry in config.items))
    varied = 0
    for seed in range(600):
        now, before = random.Random(seed), random.Random(seed)
        stock, old = merchant.draw_stock(config, rng=now), merchant.draw_stock(fixed, rng=before)
        assert [offer.ware.id for offer in stock] == [offer.ware.id for offer in old]
        assert all(offer.price == PRICES[offer.ware.id] for offer in old)
        drawn = [offer for offer in stock if offer.ware.id in PRICE_RANGES]
        assert all(offer.price == PRICES[offer.ware.id] for offer in stock if offer not in drawn)
        if not drawn:
            assert now.getstate() == before.getstate()                     # not one draw more than before
            continue
        varied += 1
        assert now.getstate() != before.getstate()
        # One draw per such ware, after the whole cart was chosen, in the order of the cart.
        expected = [merchant.draw_price(PRICES[offer.ware.id], 30, before) for offer in drawn]
        assert [offer.price for offer in drawn] == expected and now.getstate() == before.getstate()
        assert all(PRICE_RANGES[offer.ware.id][0] <= offer.price <= PRICE_RANGES[offer.ware.id][1] for offer in drawn)
    assert 100 < varied < 500


def test_the_cart_holds_different_wares_mostly_one():
    config = merchant.load_config()
    rng = random.Random(7)
    sizes = Counter()
    seen = Counter()
    prices: dict[str, set] = {ware: set() for ware in PRICES}
    for _ in range(4000):
        stock = merchant.draw_stock(config, rng=rng)
        ids = [offer.ware.id for offer in stock]
        assert len(ids) == len(set(ids)) and 1 <= len(ids) <= 3
        assert not any(offer.sold for offer in stock)
        for offer in stock:
            prices[offer.ware.id].add(offer.price)
        sizes[len(ids)] += 1
        seen.update(ids)
    assert sizes[1] > 0.7 * 4000 and sizes[2] > 0 and sizes[3] > 0
    assert seen["random_egg"] > seen["egg_pouch"] > seen["random_gem"] > 0
    # Only the egg and the pouch change their price from visit to visit.
    assert all(prices[ware] == {PRICES[ware]} for ware in PRICES if ware not in PRICE_RANGES)
    assert prices["random_egg"] == set(range(18, 34)) and len(prices["egg_pouch"]) > 100
    assert min(prices["egg_pouch"]) >= 175 and max(prices["egg_pouch"]) <= 325


def test_without_the_new_columns_neither_the_pouch_nor_stable_places_come():
    config = merchant.load_config()
    rng = random.Random(3)
    for _ in range(500):
        ids = {offer.ware.id for offer in merchant.draw_stock(config, upgrades=False, rng=rng)}
        assert not ids & {"egg_pouch", "stable_place"}
    nothing = merchant.Config(600, {1: 1}, tuple((i, n, p, 0.0, e) for i, n, p, _w, e in config.items))
    assert merchant.draw_stock(nothing, rng=rng) == []


def test_the_merchant_is_a_random_event():
    order = events_cog.EVENT_ORDER
    here = order.index(EventType.MERCHANT)
    assert order[-1] is EventType.RITUAL and EventType["MERCHANT"].value == 12
    assert events_cog.PEAK_WEIGHTS[here] == events_cog.OFF_PEAK_WEIGHTS[here] == 1
    assert events_cog.DEBUG_WEIGHTS[here] == 0 and EventType.MERCHANT not in events_cog.PASSIVE


# -- buying: who is first, who cannot pay ------------------------------------------------------

async def test_two_buyers_at_the_same_moment_only_one_gets_it(game_db, cart):
    game, db = game_db, game_db.bot.db
    cart("rest_kit")
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    for player in (ala, ola):
        await make_owner(db, player.id, scrolls=30)
        daily.save_daily_to_file(game.bot, player.id)                  # both hunted today
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    first, second = await asyncio.gather(game.click(panel, ala, merchant.buy_key(0)),
                                         game.click(panel, ola, merchant.buy_key(0)))
    stock = await asyncio.wait_for(task, 5)                             # sold out: he leaves at once
    assert stock[0].buyer == ala
    await until(lambda: second.answers)
    assert second.answers == [merchant.MSG_ALREADY_SOLD] and first.answers == []
    assert await scrolls_of(db, ala.id) == 20 and await scrolls_of(db, ola.id) == 30
    assert not daily.load_daily_from_file(game.bot, ala.id) and daily.load_daily_from_file(game.bot, ola.id)
    assert game.channel.contains(f"🛒 {ala.mention} kupuje **Zestaw do wypoczynku** za 10 zwojów!")
    assert panel.view is None and merchant.MSG_SOLD_OUT in panel.embeds[0].description
    assert game.channel.messages[-1].content == merchant.MSG_GONE_TEXT          # no picture: words only


async def test_too_few_scrolls_takes_nothing_and_the_ware_stays(game_db, cart):
    game, db = game_db, game_db.bot.db
    cart("rest_kit", "tower_ticket")
    poor, rich = game.add_member("Biedak"), game.add_member("Bogacz")
    await make_owner(db, poor.id, scrolls=9)
    await make_owner(db, rich.id, scrolls=100)
    for player in (poor, rich):
        daily.save_daily_to_file(game.bot, player.id)
        tower.save_weekly_tower_to_file(game.bot, player.id)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, poor, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_POOR.format(name="Zestaw do wypoczynku", price="10 zwojów",
                                                        have="9 zwojów")]
    assert await scrolls_of(db, poor.id) == 9 and daily.load_daily_from_file(game.bot, poor.id)
    # Nothing was sold: the same ware is still there for the next player.
    await game.click(panel, rich, merchant.buy_key(0))
    await game.click(panel, rich, merchant.buy_key(1))
    stock = await asyncio.wait_for(task, 5)
    assert [offer.buyer for offer in stock] == [rich, rich]
    assert await scrolls_of(db, rich.id) == 100 - 10 - 50
    assert not tower.load_weekly_tower_from_file(game.bot, rich.id)            # one more tower run


async def test_a_player_without_a_row_cannot_pay(game_db, cart, quick):
    game = game_db
    cart("random_egg")
    stranger = game.add_member("Nowy")
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    click = await game.click(panel, stranger, merchant.buy_key(0))
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer is None
    assert click.answers == [merchant.MSG_POOR.format(name="Losowe jajko", price="25 zwojów", have="0 zwojów")]
    assert await game.bot.db.fetchval("SELECT COUNT(*) FROM PETS") == 0


async def test_unsold_merchant_leaves_after_his_time(game_db, cart, quick, no_pictures):
    game = game_db
    cart("random_gem")
    no_pictures.mkdir()
    (no_pictures / "0.gif").write_bytes(b"GIF89a")
    (no_pictures / "1.png").write_bytes(b"\x89PNG")
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    assert panel.button_labels == ["Losowy klejnot (1000 zwojów)"]
    assert panel.embeds[0].image.url == "attachment://0.gif"                 # arrival: inside the panel
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer is None and panel.view is None
    assert merchant.MSG_LEAVES in panel.embeds[0].description
    assert panel.files == ["0.gif"] and game.channel.messages[-1].files == ["1.png"]  # leaving: its own post
    assert not game.channel.contains(merchant.MSG_GONE_TEXT)                 # the picture says it


def test_pictures_are_optional(no_pictures):
    assert merchant.picture(merchant.ARRIVAL) is None and merchant.picture(merchant.DEPARTURE) is None
    no_pictures.mkdir()
    (no_pictures / "0.png").write_bytes(b"x")
    (no_pictures / "1.gif").write_bytes(b"")                                 # an empty file is no picture
    assert merchant.picture(merchant.ARRIVAL) == no_pictures / "0.png"
    assert merchant.picture(merchant.DEPARTURE) is None
    (no_pictures / "0.gif").write_bytes(b"x")                                # an animation comes first
    assert merchant.picture(merchant.ARRIVAL) == no_pictures / "0.gif"


# -- the wares ------------------------------------------------------------------------------

async def test_the_stable_grows_up_to_ten_places(game_db, cart):
    game, db = game_db, game_db.bot.db
    cart("stable_place")
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=1000, stable_places=9)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    await game.click(panel, ala, merchant.buy_key(0))
    await asyncio.wait_for(task, 5)
    assert (await row(db, ala.id))["stable_places"] == 10 and await scrolls_of(db, ala.id) == 900
    assert game.channel.contains("Stajnia ma teraz 10 miejsc.")

    # At ten places the next one is refused - no scrolls are taken.
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, ala, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_STABLE_FULL.format(places=10)]
    assert (await row(db, ala.id))["stable_places"] == 10 and await scrolls_of(db, ala.id) == 900
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await pets.add_stable_place(game.bot, ala.id) == 0               # the limit holds below the UI too


async def test_more_stable_places_can_be_used(game_db):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1, stable_places=6)
    await make_pet(db, 1, level=2, name="Mruczek")
    scene = game.scene(ala)
    assert await pets.store_pet(scene, 7) is False                          # only six places
    assert game.channel.contains("Twoja stajnia ma 6 miejsc - od 1 do 6")
    assert await pets.store_pet(scene, 6) is True
    assert (await row(db, ala.id))["pet_id_alt6"] == 1 and (await row(db, ala.id))["pet_id"] == 0
    embed, options = await pets._stable_view(game.bot, ala.id)
    assert "**6.** **Mruczek**" in embed.description and "**7.**" not in embed.description
    assert [option.value for option in options] == ["6:1"]
    assert await pets.unstore_pet(scene, 6) is True and (await row(db, ala.id))["pet_id"] == 1
    # A player with the four places of old gets the old answer.
    ola = game.add_member("Ola")
    await make_owner(db, ola.id, pet_id=0)
    assert await pets.unstore_pet(game.scene(ola), 5) is False
    assert game.channel.contains("Każdy gracz ma tylko cztery miejsca")


async def test_the_pouch_is_sold_once_per_player(game_db, cart):
    game, db = game_db, game_db.bot.db
    cart("egg_pouch")
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=600)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    await game.click(panel, ala, merchant.buy_key(0))
    await asyncio.wait_for(task, 5)
    assert (await row(db, ala.id))["egg_pouch"] is True and await scrolls_of(db, ala.id) == 350

    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, ala, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_HAS_POUCH] and await scrolls_of(db, ala.id) == 350
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_an_egg_in_the_pouch_does_not_grow(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1, egg_pouch=True)
    await make_pet(db, 1, level=1, name="Mruczek")
    scene = game.scene(ala)

    # Before the pouch existed (and still without one) an egg for a player with a pet is lost.
    bob = game.add_member("Bob")
    await make_owner(db, bob.id, pet_id=1)
    assert await pets.egg_room(game.bot, bob.id) == ""
    assert await pets.assign_pet(game.scene(bob), bob) is False

    assert await pets.egg_room(game.bot, ala.id) == "pouch"
    assert await pets.assign_pet(scene, ala) == pets.INTO_POUCH
    egg = (await row(db, ala.id))["pouch_pet_id"]
    assert egg > 1 and (await row(db, ala.id))["pet_id"] == 1
    assert game.channel.contains("nowe jajko trafia do sakwy")
    assert await pets.egg_room(game.bot, ala.id) == ""                       # one egg per pouch
    assert await pets.assign_pet(scene, ala) is False

    # Levels go to the pet at the player's side, never to the egg in the pouch.
    assert await pets.level_up_pet(scene, ala.id) is True
    levels = {int(r["pet_id"]): int(r["pet_lvl"]) for r in await db.fetch("SELECT PET_ID, PET_LVL FROM PETS")}
    assert levels == {1: 2, egg: 0}

    # Taken out: the egg is the active pet, the old one waits in the first free place.
    assert await pets.take_egg_from_pouch(scene) is True
    now = await row(db, ala.id)
    assert (now["pet_id"], now["pet_id_alt1"], now["pouch_pet_id"]) == (egg, 1, 0)
    assert await pets.level_up_pet(scene, ala.id) is True
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = $1", egg)) == 1
    assert await pets.take_egg_from_pouch(scene) is False
    assert game.channel.contains(pets.MSG_POUCH_EMPTY.format(player_id=ala.id))


async def test_the_pouch_egg_waits_when_the_stable_is_full(game_db):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1, egg_pouch=True, pouch_pet_id=9,
                     pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in (1, 2, 3, 4, 5, 9):
        await make_pet(db, pet_id, level=0 if pet_id == 9 else 1)
    embed, options = await pets._stable_view(game.bot, ala.id)
    assert pets.STABLE_POUCH in embed.description and "pouch:9" in [option.value for option in options]
    assert await pets.take_egg_from_pouch(game.scene(ala)) is False
    assert game.channel.contains(pets.MSG_POUCH_NO_ROOM.format(player_id=ala.id))
    assert (await row(db, ala.id))["pouch_pet_id"] == 9
    # Through the stable list, as soon as there is room.
    await db.execute("UPDATE PETOWNER SET STABLE_PLACES = 5 WHERE PLAYER_ID = $1", ala.id)
    assert await pets._take_from_pouch(game.bot, ala.id, 9) == ("swapped", 5)
    assert (await row(db, ala.id))["pet_id"] == 9 and (await row(db, ala.id))["pet_id_alt5"] == 1


async def test_a_bought_egg_needs_room(game_db, cart, monkeypatch):
    game, db = game_db, game_db.bot.db
    cart("random_egg")
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await make_owner(db, ala.id, scrolls=100, pet_id=1)                     # a pet at her side, no pouch
    await make_pet(db, 1, level=1)
    await make_owner(db, ola.id, scrolls=100)                                # nobody at her side
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, ala, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_NO_EGG_ROOM] and await scrolls_of(db, ala.id) == 100
    await game.click(panel, ola, merchant.buy_key(0))
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer == ola and await scrolls_of(db, ola.id) == 75
    assert int((await row(db, ola.id))["pet_id"]) > 1                         # the egg is at her side


async def test_a_ware_that_does_not_fit_after_all_gives_the_scrolls_back(game_db, cart, monkeypatch):
    game, db = game_db, game_db.bot.db
    cart("random_gem")
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=1500)

    async def every_gem_already(scene, player, variant=None):
        return False

    monkeypatch.setattr(skills, "assign_skill", every_gem_already)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, ala, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_ALL_GEMS] and await scrolls_of(db, ala.id) == 1500
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_real_gem_is_bought(game_db, cart):
    game, db = game_db, game_db.bot.db
    cart("random_gem")
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=1000)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    await game.click(panel, ala, merchant.buy_key(0))
    await asyncio.wait_for(task, 5)
    assert await scrolls_of(db, ala.id) == 0 and int((await row(db, ala.id))["skill_gem"]) > 0


async def test_rest_kit_and_tower_ticket_need_a_used_attempt(game_db, cart):
    game, db = game_db, game_db.bot.db
    cart("rest_kit", "tower_ticket")
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=100)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    rest = await game.click(panel, ala, merchant.buy_key(0))
    ticket = await game.click(panel, ala, merchant.buy_key(1))
    await until(lambda: rest.answers and ticket.answers)
    assert rest.answers == [merchant.MSG_HUNT_READY] and ticket.answers == [merchant.MSG_TOWER_READY]
    assert await scrolls_of(db, ala.id) == 100
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_broken_hand_over_counts_as_sold_and_the_owner_is_told(game_db, cart, monkeypatch):
    game, db = game_db, game_db.bot.db
    cart("rest_kit")
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=10)
    daily.save_daily_to_file(game.bot, ala.id)

    async def broken(bot, ids):
        raise OSError("disk full")

    monkeypatch.setattr(daily, "remove_player_daily_file", broken)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    click = await game.click(panel, ala, merchant.buy_key(0))
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer == ala and await scrolls_of(db, ala.id) == 0       # never sold twice
    assert click.answers == [merchant.MSG_FAILED]
    assert len(game.bot.owner_log) == 1 and "disk full" not in game.bot.owner_log[0] \
        and "OSError" in game.bot.owner_log[0]


async def test_through_the_events_cog(game_db, cart, quick):
    game = game_db
    cart("rest_kit")
    cog = events_cog.EventsCog(game.bot)
    run = asyncio.ensure_future(cog.run_one_event(forced=EventType.MERCHANT))
    await game.wait_for_buttons(merchant.buy_key(0))
    state = game.bot.state
    # Passive like the chest (owner, 2026-11-23): his event is open, the channel is free.
    await until(lambda: state.event_alive)
    assert not state.busy and state.event_type is EventType.MERCHANT
    assert await asyncio.wait_for(run, 5) is EventType.MERCHANT
    assert not state.busy and not state.event_alive and state.event_type is EventType.NONE
    assert game.channel.contains(merchant.MSG_GONE_TEXT)


async def test_a_purchase_waits_while_a_hunt_plays(game_db, cart):
    game, db = game_db, game_db.bot.db
    cart("rest_kit")
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=10)
    daily.save_daily_to_file(game.bot, ala.id)
    state = game.bot.state
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    await until(lambda: state.event_alive)
    with state.hold_busy("hunt"):                                             # a hunt starts meanwhile
        refused = await game.click(panel, ala, merchant.buy_key(0))
        await until(lambda: refused.answers)
    assert refused.answers == [ui.MSG_BUSY] and await scrolls_of(db, ala.id) == 10
    await game.click(panel, ala, merchant.buy_key(0))                        # the hunt is over
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer == ala and await scrolls_of(db, ala.id) == 0


async def test_the_next_event_sends_him_away_without_a_word(game_db, cart):
    game = game_db
    cart("rest_kit")
    state = game.bot.state
    task = start(game)
    await game.wait_for_buttons(merchant.buy_key(0))
    await until(lambda: state.event_alive)
    state.close_event()                                                       # the next event / the boss
    stock = await asyncio.wait_for(task, 5)
    assert not stock[0].sold
    assert not game.channel.contains(merchant.MSG_GONE_TEXT) and not game.channel.contains("zwija kram")
    assert not state.busy and not state.event_alive


async def test_once_a_day_in_the_draw(game_db, cart, quick, monkeypatch):
    game = game_db
    cart("rest_kit")
    cog = events_cog.EventsCog(game.bot)
    assert cog.not_again_today() == ()
    await asyncio.wait_for(merchant.spawn_merchant(game.scene()), 5)
    assert merchant.came_today(game.bot) and cog.not_again_today() == (EventType.MERCHANT,)
    drawn = []
    monkeypatch.setattr(events_cog, "pick_event", lambda now, *, debug, exclude=(): drawn.append(exclude))
    await cog.run_one_event()
    assert EventType.MERCHANT in drawn[0]
    game.bot.store.runtime_set(merchant.DAY_KEY, "2000-01-01")               # he came on another day
    assert not merchant.came_today(game.bot) and cog.not_again_today() == ()


async def test_an_egg_into_the_pouch_does_not_block_the_level_of_the_same_drop(game_db, monkeypatch):
    """Owner, 2026-11-23: only a new ACTIVE egg stops the pet level of its drop."""
    game, db = game_db, game_db.bot.db
    monkeypatch.setattr(daily, "_loot_table", lambda rarity: [
        {"id": daily.LOOT_EGG, "descr": "Jajko", "weight": 100},
        {"id": daily.LOOT_PET_LEVEL, "descr": "Poziom", "weight": 100}])
    monkeypatch.setattr(random, "random", lambda: 0.0)
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1, egg_pouch=True)
    await make_pet(db, 1, level=1)
    assert await daily.rand_loot(game.scene(ala), 1, ala, 0) == "👉 Jajko\n👉 Poziom\n"
    assert (await row(db, ala.id))["pouch_pet_id"] > 1
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 1")) == 2

    ola = game.add_member("Ola")                                              # nobody at her side
    assert await daily.rand_loot(game.scene(ola), 1, ola, 0) == "👉 Jajko\n"   # the old rule stays


# -- a price drawn for the visit: shown, asked, paid, given back ------------------------------

@pytest.mark.parametrize("ware, name, seed", [("random_egg", "Losowe jajko", 1), ("random_egg", "Losowe jajko", 2),
                                              ("egg_pouch", "Sakwa na jajko", 1), ("egg_pouch", "Sakwa na jajko", 2)])
async def test_the_price_of_the_visit_is_the_one_shown_asked_and_paid(game_db, visit, caplog, ware, name, seed):
    game, db = game_db, game_db.bot.db
    (expected,) = visit(seed, ware)
    price, (low, high) = expected.price, PRICE_RANGES[ware]
    assert low <= price <= high and price != PRICES[ware]                    # (seeds that do not hit the list price)
    cost = merchant.scrolls(price)
    poor, rich = game.add_member("Biedak"), game.add_member("Bogacz")
    await make_owner(db, poor.id, scrolls=price - 1)
    await make_owner(db, rich.id, scrolls=price + 7)
    with caplog.at_level(logging.INFO, logger="altermmo.features.merchant"):
        task = start(game)
        panel = await game.wait_for_buttons(merchant.buy_key(0))
        # Shown: on the button and in the panel.
        assert panel.button_labels == [f"{name} ({cost})"]
        assert f"**{name}** - {cost}\n" in panel.embeds[0].description
        assert merchant.scrolls(PRICES[ware]) not in panel.embeds[0].description
        # Asked: one scroll short is too few - and nothing is taken.
        refused = await game.click(panel, poor, merchant.buy_key(0))
        await until(lambda: refused.answers)
        assert refused.answers == [merchant.MSG_POOR.format(name=name, price=cost,
                                                            have=merchant.scrolls(price - 1))]
        assert await scrolls_of(db, poor.id) == price - 1
        # Paid: exactly that many scrolls.
        await game.click(panel, rich, merchant.buy_key(0))
        stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer == rich and stock[0].price == price
    assert await scrolls_of(db, rich.id) == 7 and await scrolls_of(db, poor.id) == price - 1
    assert game.channel.contains(f"🛒 {rich.mention} kupuje **{name}** za {cost}!")
    assert f"~~{stock[0].ware.emoji} {name} - {cost}~~ - kupuje {rich.mention}" in panel.embeds[0].description
    logged = [record.getMessage() for record in caplog.records]
    assert f"Merchant spawn: {ware} for {price}." in logged
    assert any(line.endswith(f"bought {ware} for {price} scrolls.") for line in logged)


async def test_the_price_stays_the_same_for_the_whole_visit(game_db, visit):
    """Drawn once when he comes: a refusal, another sale and the panel drawn anew do not change it."""
    game, db = game_db, game_db.bot.db
    expected = visit(5, "random_egg", "rest_kit")
    egg = next(index for index, offer in enumerate(expected) if offer.ware.id == "random_egg")
    kit = 1 - egg
    price = expected[egg].price
    assert expected[kit].price == 10 and 18 <= price <= 33 and price != 25
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=10 + price, pet_id=1)                # a pet at her side, no pouch: no room
    await make_pet(db, 1, level=1)
    daily.save_daily_to_file(game.bot, ala.id)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(egg))
    line = f"**Losowe jajko** - {merchant.scrolls(price)}\n"
    assert line in panel.embeds[0].description
    refused = await game.click(panel, ala, merchant.buy_key(egg))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_NO_EGG_ROOM] and await scrolls_of(db, ala.id) == 10 + price
    await game.click(panel, ala, merchant.buy_key(kit))                      # the other ware: the panel is redrawn
    await until(lambda: "kupuje" in panel.embeds[0].description)
    assert line in panel.embeds[0].description and await scrolls_of(db, ala.id) == price
    assert f"Losowe jajko ({merchant.scrolls(price)})" in panel.button_labels
    await db.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", ala.id)   # now there is room
    await game.click(panel, ala, merchant.buy_key(egg))
    stock = await asyncio.wait_for(task, 5)
    assert [offer.price for offer in stock] == [offer.price for offer in expected]
    assert all(offer.buyer == ala for offer in stock) and await scrolls_of(db, ala.id) == 0


async def test_a_refund_gives_back_exactly_what_was_paid(game_db, visit, monkeypatch):
    game, db = game_db, game_db.bot.db
    (expected,) = visit(3, "random_egg")
    price = expected.price
    assert 18 <= price <= 33 and price != 25
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=40)
    moved: list[tuple[str, int]] = []
    take, give_back = pets.take_scrolls, pets.return_scrolls

    async def taken(bot, player_id, number):
        moved.append(("taken", number))
        return await take(bot, player_id, number)

    async def returned(bot, player_id, number):
        moved.append(("returned", number))
        return await give_back(bot, player_id, number)

    async def no_room_after_all(scene, player):
        return False

    monkeypatch.setattr(pets, "take_scrolls", taken)
    monkeypatch.setattr(pets, "return_scrolls", returned)
    monkeypatch.setattr(pets, "assign_pet", no_room_after_all)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, ala, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_NO_EGG_ROOM]
    assert moved == [("taken", price), ("returned", price)] and await scrolls_of(db, ala.id) == 40
    assert panel.button_labels == [f"Losowe jajko ({merchant.scrolls(price)})"]   # still on sale, at the same price
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("seed", [1, 3])
async def test_a_hand_over_that_failed_tells_the_owner_the_price_that_was_paid(game_db, visit, monkeypatch, seed):
    """The owner gives the scrolls back by hand from this line: it must name the price of the visit."""
    game, db = game_db, game_db.bot.db
    (expected,) = visit(seed, "random_egg")
    price = expected.price
    assert 18 <= price <= 33 and price != 25
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=40)

    async def broken(scene, player):
        raise RuntimeError("test: the egg could not be stored")

    monkeypatch.setattr(pets, "assign_pet", broken)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    click = await game.click(panel, ala, merchant.buy_key(0))
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer == ala and await scrolls_of(db, ala.id) == 40 - price    # sold, never twice
    assert click.answers == [merchant.MSG_FAILED]
    assert game.bot.owner_log == [merchant.LOG_FAILED.format(
        player_id=ala.id, price=merchant.scrolls(price), name="Losowe jajko", error="RuntimeError")]
    assert f"zapłacił {merchant.scrolls(price)} za" in game.bot.owner_log[0]
    assert merchant.scrolls(25) not in game.bot.owner_log[0]                 # not the list price


@pytest.mark.parametrize("seed", [1, 3])
async def test_a_refund_that_failed_tells_the_owner_the_price_that_was_paid(game_db, visit, monkeypatch, seed):
    game, db = game_db, game_db.bot.db
    (expected,) = visit(seed, "random_egg")
    price = expected.price
    assert 18 <= price <= 33 and price != 25
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=40)

    async def no_room_after_all(scene, player):
        return False

    async def lost(bot, player_id, number):
        raise OSError("test: the database went away")

    monkeypatch.setattr(pets, "assign_pet", no_room_after_all)
    monkeypatch.setattr(pets, "return_scrolls", lost)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, ala, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_NO_EGG_ROOM] and await scrolls_of(db, ala.id) == 40 - price
    assert game.bot.owner_log == [merchant.LOG_NOT_RETURNED.format(
        player_id=ala.id, price=merchant.scrolls(price), name="Losowe jajko", error="OSError")]
    assert f"zwrot {merchant.scrolls(price)} nie" in game.bot.owner_log[0]
    assert merchant.scrolls(25) not in game.bot.owner_log[0]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_every_visit_draws_its_own_price(game_db, visit, quick):
    game = game_db
    seen = set()
    for seed in range(6):
        (expected,) = visit(seed, "random_egg")
        task = start(game)
        panel = await game.wait_for_buttons(merchant.buy_key(0))
        assert panel.button_labels == [f"Losowe jajko ({merchant.scrolls(expected.price)})"]
        seen.add(expected.price)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(seen) > 2 and all(18 <= price <= 33 for price in seen)


# -- the album note of a bought egg (features/album.py, tests/test_album_note.py) -------------------

async def test_an_egg_bought_by_a_supporter_comes_with_the_album_note_once(game_db, cart, monkeypatch):
    """The merchant's egg is the egg of a drop (``pets.assign_pet``): a supporter gets the album's note
    with it - exactly one, before the line that says who bought what; nobody else gets anything new."""
    game, db = game_db, game_db.bot.db
    cart("random_egg")
    force(monkeypatch, new_egg("Cat"))
    patron = patrons.PATRON_ROLES[0]
    ala, ola = game.add_member("Ala", roles=[patron]), game.add_member("Ola")
    for player in (ala, ola):
        await make_owner(db, player.id, scrolls=25)
    sent = recorded(game, monkeypatch)                                        # everything posted from now on

    task = start(game)                                                        # a supporter buys
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    await game.click(panel, ala, merchant.buy_key(0))
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer == ala and await scrolls_of(db, ala.id) == 0
    (note,) = notes(sent)
    assert note.content == EMPTY_NOTE.format(player_id=ala.id) == album.egg_note(ala.id, new_egg("Cat"), [])
    texts = [message.content or "" for message, _ in sent]
    (bought,) = [text for text in texts if text.startswith(f"🛒 {ala.mention} kupuje **Losowe jajko** za 25 zwojów!")]
    assert texts.index(note.content) < texts.index(bought)                   # the egg's note, then the sale

    task = start(game)                                                        # somebody who does not support
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    await game.click(panel, ola, merchant.buy_key(0))
    stock = await asyncio.wait_for(task, 5)
    assert stock[0].buyer == ola and int((await row(db, ola.id))["pet_id"]) > 0
    assert any((message.content or "").startswith(f"🛒 {ola.mention} kupuje **Losowe jajko**") for message, _ in sent)
    assert notes(sent) == [note]                                            # nothing new for her


async def test_no_album_note_without_a_new_egg(game_db, cart, monkeypatch):
    """No egg, no note: a supporter who has no room for the egg (nothing is taken) and one who buys the
    egg pouch."""
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))
    patron = patrons.PATRON_ROLES[0]
    ala = game.add_member("Ala", roles=[patron])
    await make_owner(db, ala.id, scrolls=300, pet_id=1)                       # a pet at her side, no pouch yet
    await make_pet(db, 1, level=1)
    cart("random_egg", "egg_pouch")
    sent = recorded(game, monkeypatch)
    task = start(game)
    panel = await game.wait_for_buttons(merchant.buy_key(0))
    refused = await game.click(panel, ala, merchant.buy_key(0))
    await until(lambda: refused.answers)
    assert refused.answers == [merchant.MSG_NO_EGG_ROOM] and await scrolls_of(db, ala.id) == 300
    assert notes(sent) == []
    await game.click(panel, ala, merchant.buy_key(1))                        # the pouch: a ware, not an egg
    await until(lambda: game.channel.contains(f"🛒 {ala.mention} kupuje **Sakwa na jajko** za 250 zwojów!"))
    assert (await row(db, ala.id))["egg_pouch"] is True and await scrolls_of(db, ala.id) == 50
    assert notes(sent) == []
    await game.click(panel, ala, merchant.buy_key(0))                        # now the egg has a place: the pouch
    stock = await asyncio.wait_for(task, 5)
    assert all(offer.buyer == ala for offer in stock) and await scrolls_of(db, ala.id) == 25
    assert (await row(db, ala.id))["pouch_pet_id"] > 1
    (note,) = notes(sent)                                                     # ... and with the egg its note
    assert note.content == EMPTY_NOTE.format(player_id=ala.id)


# -- the production table before the start-up migration --------------------------------------

async def test_the_old_petowner_table_works_until_the_columns_are_added(game_db, cart):
    from altermmo import schema

    game, db, bot = game_db, game_db.bot.db, game_db.bot
    for column in pets.UPGRADE_COLUMNS:                                       # the table as production has it
        await db.execute(f"ALTER TABLE PETOWNER DROP COLUMN {column}", idempotent=True)
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=500, pet_id=1)
    await make_pet(db, 1, level=1)
    assert await pets.upgrades_available(bot) is False
    assert await pets.stable_places(bot, ala.id) == 4 and await pets.has_egg_pouch(bot, ala.id) is False
    assert await pets.egg_room(bot, ala.id) == "" and await pets.assign_pet(game.scene(ala), ala) is False
    assert await pets.store_pet(game.scene(ala), 4) is True and await pets.unstore_pet(game.scene(ala), 4) is True
    embed, _options = await pets._stable_view(bot, ala.id)
    assert "**4.**" in embed.description and pets.STABLE_POUCH not in embed.description
    assert await pets.owned_pet_ids(bot, ala.id) == [1]
    assert await pets._take_from_pouch(bot, ala.id, None) == ("none", 0)
    assert "petowner: brak kolumny stable_places" in await schema.verify(db)

    assert await schema.add_new_columns(db) == []                             # what the next start does
    assert await pets.upgrades_available(bot, refresh=True) is True
    row_now = await row(db, ala.id)
    assert (row_now["pet_id"], row_now["reroll_scroll"], row_now["stable_places"], row_now["egg_pouch"]) == \
        (1, 500, 4, False)
    assert not [problem for problem in await schema.verify(db) if problem.startswith("petowner")]
