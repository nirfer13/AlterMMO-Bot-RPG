"""The "Wędrowny kupiec" event (features/merchant.py), the egg pouch and the bigger stable (features/pets.py)."""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter

import pytest

from altermmo.cogs import events as events_cog
from altermmo.core import timing, ui
from altermmo.core.state import EventType
from altermmo.features import daily, merchant, pets, skills, tower

# -- helpers -------------------------------------------------------------------------------

PRICES = {"egg_pouch": 250, "random_egg": 25, "stable_place": 100, "random_gem": 1000, "rest_kit": 10,
          "tower_ticket": 50}


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
    """``cart("rest_kit", ...)``: the merchant brings exactly these wares (prices of the real config)."""
    chosen: list[str] = []

    def draw(config, *, upgrades=True, rng=None):
        by_id = {item[0]: item for item in config.items}
        return [merchant.Offer(merchant.WARES[ware], by_id[ware][1], by_id[ware][2], by_id[ware][4])
                for ware in chosen]

    monkeypatch.setattr(merchant, "draw_stock", draw)

    def choose(*wares: str) -> None:
        chosen[:] = wares

    return choose


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
    config = merchant.load_config()
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


def test_the_cart_holds_different_wares_mostly_one():
    config = merchant.load_config()
    rng = random.Random(7)
    sizes = Counter()
    seen = Counter()
    for _ in range(4000):
        stock = merchant.draw_stock(config, rng=rng)
        ids = [offer.ware.id for offer in stock]
        assert len(ids) == len(set(ids)) and 1 <= len(ids) <= 3
        assert all(offer.price == PRICES[offer.ware.id] and not offer.sold for offer in stock)
        sizes[len(ids)] += 1
        seen.update(ids)
    assert sizes[1] > 0.7 * 4000 and sizes[2] > 0 and sizes[3] > 0
    assert seen["random_egg"] > seen["egg_pouch"] > seen["random_gem"] > 0


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
