"""The player items (altermmo/features/items.py, altermmo/cogs/items.py): the store, the registry, the drops,
``!przedmioty`` and the owner's ``!DajPrzedmiot`` - against a real PostgreSQL and the fake Discord."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from discord.ext import commands

from altermmo import schema
from altermmo.core import challenge, clock
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import boss, daily, items, xpsteal
from altermmo.settings import CONFIG_DIR

from fakes import FakeMessage
from test_pets import add_owner, add_pet, count, owner_row


async def run(game, member, content: str, *, channel=None) -> FakeMessage:
    """``member`` types a command; it goes through discord.py's real command machinery."""
    channel = channel or game.channel
    message = FakeMessage(channel, member, content)
    message.edited_at = None
    message._state = None
    channel.messages.append(message)
    await game.bot.invoke(await game.bot.get_context(message))
    await game.settle()
    return message


async def stored(db, player_id: int, key: str) -> int | None:
    value = await db.fetchval("SELECT COUNT FROM PLAYER_ITEMS WHERE PLAYER_ID = $1 AND ITEM = $2", player_id, key)
    return None if value is None else int(value)


@pytest_asyncio.fixture
async def items_cog(game):
    await game.bot.load_extension("altermmo.cogs.items")
    for channel in game.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    return game.bot.get_cog("Przedmioty")


def admin_of(game, name: str = "Admin"):
    admin = game.add_member(name)
    admin.guild_permissions = discord.Permissions(administrator=True)
    return admin


# -- the registry and where a loot row drops --------------------------------------------------

def test_the_registry_holds_the_owners_ids_and_names():
    assert [(item.loot_id, item.key, item.name, item.emoji) for item in items.ITEMS] == [
        (16, "summon_scroll", "Zwój przywołania", "📯"), (17, "tower_key", "Klucz do wieży", "🗝️"),
        (18, "feather", "Pióro Żar-ptaka", "🪶"), (19, "bait", "Przynęta", "🪤"),
        (20, "revive_scroll", "Zwój wskrzeszenia", "🕊️"), (22, "rope", "Lina sztygara", "🪢"),
        (25, "dust", "Nieuchwytny pył", "✨"), (26, "dova_die", "Kość Dovy", "🎲"),
        (27, "dova_card", "Karta Dovy", "🃏")]
    assert items.RESERVED_LOOT_IDS == (21, 23, 24) and not set(items.RESERVED_LOOT_IDS) & set(items.BY_LOOT_ID)
    # Only the Zwój przywołania lives in PETOWNER.
    assert [item.key for item in items.ITEMS if item.in_petowner] == ["summon_scroll"]
    assert all(len(item.key) <= 32 and item.hint and item.description and item.label for item in items.ITEMS)
    # No id of an older loot row (0-15) is taken; 26 and 27 are Dova's prizes (§12).
    assert min(items.BY_LOOT_ID) == 16 and max(items.BY_LOOT_ID) == 27
    assert items.DOVA_PRIZES == ("dova_die", "dova_card")
    assert [items.BY_KEY[key].hint for key in items.DOVA_PRIZES] == ["**!kosc**", "**!karta**"]
    assert items.by_loot_id(17) is items.BY_KEY["tower_key"] and items.by_loot_id("19").key == "bait"
    assert items.by_loot_id(21) is None and items.by_loot_id(13) is None and items.by_loot_id(None) is None


@pytest.mark.parametrize("text, key", [
    ("tower_key", "tower_key"), ("Klucz do wieży", "tower_key"), ("klucz do wiezy", "tower_key"),
    ("KLUCZE DO WIEŻY", "tower_key"), ("zwój przywołania", "summon_scroll"), ("pioro zar-ptaka", "feather"),
    ("Pióro Żar ptaka", "feather"), ("przyneta", "bait"), ("lina", None), ("", None), ("nieuchwytny pył", "dust"),
    ("kość dovy", "dova_die"), ("Kosc Dovy", "dova_die"), ("karta dovy", "dova_card"), ("KARTY DOVY", "dova_card"),
])
def test_the_owner_may_name_an_item_by_its_key_or_its_name(text, key):
    found = items.find(text)
    assert (found.key if found else None) == key


def test_a_row_drops_only_from_its_sources_and_boost_false_keeps_its_weight():
    everywhere = {"id": 1, "descr": "x", "weight": 10}
    boss_only = {**everywhere, "sources": ["boss"]}
    assert all(items.drops_here(everywhere, source) for source in items.SOURCES)
    assert [items.drops_here(boss_only, source) for source in items.SOURCES] == [True, False, False, False, False]
    assert items.drops_here({**everywhere, "sources": "tower"}, "tower")       # (a lone word is read as a list)
    assert items.loot_weight(everywhere, 50, "boss") == 15.0
    assert items.loot_weight(boss_only, 50, "summoned") == 0.0
    assert items.loot_weight({**everywhere, "boost": False}, 50, "boss") == 10
    assert items.loot_weight({**everywhere, "weight": 0}, 50, "boss") == 0.0                # weight 0 never drops
    assert items.loot_weight({**everywhere, "weight": 0, "boost": False}, 0, "boss") == 0.0
    assert items.loot_weight(everywhere, -100, "boss") == 0.0                                # nothing is left of it
    # Exactly the arithmetic the loops always did (draws must not move by a rounding).
    assert items.loot_weight({**everywhere, "weight": 2.5}, 37, "world") == (37 / 100 + 1) * 2.5


def test_the_loot_source_is_set_for_a_block_only():
    assert items.LOOT_SOURCE.get() is None and items.current_source("boss") == "boss"
    with items.loot_source("tower"):
        assert items.current_source("boss") == "tower"
        with items.loot_source("duel"):
            assert items.current_source("world") == "duel"
        assert items.current_source("world") == "tower"
    assert items.LOOT_SOURCE.get() is None
    with pytest.raises(RuntimeError), items.loot_source("summoned"):
        raise RuntimeError("the fight broke")
    assert items.LOOT_SOURCE.get() is None                                     # reset whatever happens


async def test_a_task_started_inside_the_block_keeps_its_source():
    seen = []

    async def drop():
        await asyncio.sleep(0)
        seen.append(items.LOOT_SOURCE.get())

    with items.loot_source("summoned"):
        task = asyncio.ensure_future(drop())
    await task
    assert seen == ["summoned"] and items.LOOT_SOURCE.get() is None


@pytest.mark.parametrize("name, tables, old_ids", [
    ("lootConfig.json", ("Normal", "Rare", "Epic", "Legend", "Mythic"), {0, 1, 2, 10, 12, 13, 14, 15}),
    ("mobLootConfig.json", ("Normal", "Rare", "Epic"), {1, 9, 10, 11, 12, 13, 14}),
])
def test_the_shipped_tables_append_the_new_items_after_every_older_row(name, tables, old_ids):
    """Deploy safety: the running bot re-reads both files, so the new rows come last (every older draw keeps
    its place) and know only the sources and keys of features/items.py."""
    config = json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
    new_ids = set(items.BY_LOOT_ID)
    assert set(config) == {f"loot_details_{table}" for table in tables}
    for key, table in config.items():
        ids = [row["id"] for row in table]
        assert set(ids) <= old_ids | new_ids, key
        assert ids == sorted(ids, key=lambda loot_id: loot_id in new_ids), key    # the new ones at the end
        assert len(set(ids) & new_ids) == len([i for i in ids if i in new_ids]), key   # each item once
        for row in table:
            assert set(row) <= {"id", "descr", "weight", "sources", "boost"}, row
            assert isinstance(row["weight"], (int, float)) and row["weight"] >= 0 and row["descr"], row
            assert set(row.get("sources", ())) <= set(items.SOURCES) and row.get("boost", True) in (True, False)
            if row["id"] in new_ids:
                assert items.by_loot_id(row["id"]).name in row["descr"], row
            assert "kamień przebudzenia" not in row["descr"]                   # the UI's name: Kamień olśnienia
        # A Zwój przywołania never brings the next summoned boss: only a scheduled boss and the world drop it.
        scroll = [row for row in table if row["id"] == 16]
        assert all(row["sources"] == (["boss"] if name == "lootConfig.json" else ["world"]) for row in scroll)


#: items_decisions.md §9: ``loot id: (weight per tier, sources, boost)`` - the tiers are the boss's
#: Normal..Mythic and the monster's Normal..Epic; ``None``: no ``sources`` (it drops everywhere).
FINAL_WEIGHTS = {
    "lootConfig.json": {
        16: ((0, 0, 1, 2, 5), ["boss"], True),
        17: ((0, 1, 2, 3, 5), ["boss", "summoned", "duel"], True),
        18: ((0.03, 0.05, 0.08, 0.1, 0.12), ["boss", "summoned", "duel"], False),
        19: ((2, 3, 4, 4, 6), None, True),
        20: ((0.5, 0.5, 1, 1, 2), None, True),
        22: ((1, 2, 3, 3, 5), None, True),
        25: ((0.05, 0.1, 0.2, 0.3, 0.5), None, True),
    },
    "mobLootConfig.json": {
        16: ((0, 0, 1), ["world"], True),
        17: ((0, 0.3, 2), ["world", "duel"], True),
        18: ((0.02, 0.07, 0.15), None, False),
        19: ((2, 4, 6), None, True),
        20: ((0.2, 1, 3), None, True),
        22: ((1, 3, 6), None, True),
        25: ((0.02, 0.05, 0.3), None, True),
    },
}


@pytest.mark.parametrize("name, tables", [
    ("lootConfig.json", ("Normal", "Rare", "Epic", "Legend", "Mythic")),
    ("mobLootConfig.json", ("Normal", "Rare", "Epic")),
])
def test_the_new_items_drop_at_the_owners_weights(name, tables):
    """§9, the last commit of the items: the new rows turned on at the owner's weights (they came with
    weight 0 first - the running bot re-reads both files).  A tier whose weight is 0 has no row at all."""
    config = json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
    assert set(FINAL_WEIGHTS[name]) == set(items.BY_LOOT_ID) - DOVA_IDS
    for loot_id, (weights, sources, boost) in FINAL_WEIGHTS[name].items():
        for table, weight in zip(tables, weights, strict=True):
            rows = [row for row in config[f"loot_details_{table}"] if row["id"] == loot_id]
            if not weight:
                assert rows == [], (table, loot_id)
                continue
            [row] = rows
            assert row["weight"] == weight and row.get("sources") == sources, (table, row)
            assert row.get("boost", True) is boost, (table, row)


#: Loot ids of Dova's prizes (§12): no loot table has a row of them.
DOVA_IDS = {items.BY_KEY[key].loot_id for key in items.DOVA_PRIZES}


def test_only_dova_gives_his_prizes():
    """§12: the Kość and the Karta Dovy come from a won game of cups alone - no loot table, no merchant."""
    from altermmo.features import merchant

    assert DOVA_IDS == {26, 27}
    for name in ("lootConfig.json", "mobLootConfig.json"):
        config = json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
        assert not [row for table in config.values() for row in table if row["id"] in DOVA_IDS], name
    assert not [ware for ware in merchant.WARES.values() if ware.item in items.DOVA_PRIZES]
    shipped = merchant.load_config(CONFIG_DIR / "merchantConfig.json")
    assert not [ware for ware, *_ in shipped.items if merchant.WARES[ware].item in items.DOVA_PRIZES]


# -- the table ---------------------------------------------------------------------------

async def test_the_table_is_made_at_start_up_with_its_key_and_check(db):
    assert "altermmo.features.items" in schema.SCHEMA_MODULES
    assert items.NEW_TABLES == items.SCHEMA and items.NEW_COLUMNS == ()
    assert schema.EXPECTED_COLUMNS["player_items"] == {"player_id": "numeric", "item": "character varying",
                                                       "count": "integer"}
    await db.execute("DROP TABLE PLAYER_ITEMS", idempotent=True)
    assert "brak tabeli player_items" in await schema.verify(db)
    await schema.create_new_tables(db)
    await db.execute("INSERT INTO PLAYER_ITEMS (PLAYER_ID, ITEM, COUNT) VALUES (1, 'bait', 2)")
    await schema.create_new_tables(db)                                 # again: nothing breaks, nothing is lost
    assert await schema.verify(db) == [] and await stored(db, 1, "bait") == 2
    with pytest.raises(Exception, match="duplicate key"):              # one row per player and item
        await db.execute("INSERT INTO PLAYER_ITEMS (PLAYER_ID, ITEM, COUNT) VALUES (1, 'bait', 1)")
    with pytest.raises(Exception, match="check constraint"):           # never below 0
        await db.execute("UPDATE PLAYER_ITEMS SET COUNT = -1 WHERE PLAYER_ID = 1")
    assert await db.fetchval("SELECT COUNT FROM PLAYER_ITEMS WHERE PLAYER_ID = 1") == 2


async def test_readiness_is_asked_once_and_a_missing_table_is_made(game_db, monkeypatch):
    bot, db = game_db.bot, game_db.bot.db
    asked: list = []
    real = db.fetchval

    async def fetchval(query, *args, **kwargs):
        if "to_regclass" in query:
            asked.append(query)
        return await real(query, *args, **kwargs)

    monkeypatch.setattr(db, "fetchval", fetchval)
    assert await items.table_ready(bot) is True and await items.table_ready(bot) is True and len(asked) == 1

    # The database did not answer at the start: the first look makes the table.
    await db.execute("DROP TABLE PLAYER_ITEMS")
    bot._player_items = None
    assert await items.table_ready(bot) is True and await count(db, "PLAYER_ITEMS") == 0

    # It cannot be made (no privilege...): "no", remembered for ten minutes, then looked at again.
    await db.execute("DROP TABLE PLAYER_ITEMS")
    bot._player_items = None
    monkeypatch.setattr(items, "SCHEMA", ("CREATE TABLE IF NOT EXISTS nonsense (",))
    assert await items.table_ready(bot) is False
    assert await items.table_ready(bot) is False and await items.counts(bot, 1) == {}
    assert await items.count(bot, 1, "bait") == 0
    monkeypatch.setattr(items, "SCHEMA", items.NEW_TABLES)
    later = clock.monotonic() + items.READY_RECHECK + 1
    monkeypatch.setattr(items.clock, "monotonic", lambda: later)
    assert await items.table_ready(bot) is True


# -- grant, take, adjust -----------------------------------------------------------------

async def test_grant_and_take_have_no_cap_and_never_go_below_zero(game_db):
    bot, db = game_db.bot, game_db.bot.db
    assert await items.grant(bot, 7, "tower_key") == 1                # the row is made
    assert await items.grant(bot, 7, "tower_key", 40) == 41           # no cap anywhere
    assert await items.count(bot, 7, "tower_key") == 41 and await items.count(bot, 7, "bait") == 0
    assert await items.take(bot, 7, "tower_key", 40) == 1
    assert await items.take(bot, 7, "tower_key", 2) is None           # fewer than asked: nothing taken
    assert await items.take(bot, 7, "tower_key") == 0
    assert await items.take(bot, 7, "tower_key") is None and await stored(db, 7, "tower_key") == 0
    assert await items.take(bot, 8, "bait") is None                   # no row at all
    assert await items.counts(bot, 7) == {"tower_key": 0}
    # The owner's adjustment: a minus takes, never below 0.
    assert await items.adjust(bot, 7, "rope", 3) == 3 and await items.adjust(bot, 7, "rope", -5) == 0
    assert await items.adjust(bot, 9, "rope", -2) == 0 and await stored(db, 9, "rope") is None
    # Inside a caller's transaction (the later uses of the items: the feather in !oswiecenie...).
    async with db.transaction() as con:
        assert await items.grant_in(con, 7, "feather", 2) == 2
        assert await items.count_in(con, 7, "feather", lock=True) == 2 and await items.count_in(con, 8, "feather") == 0
        assert await items.take_in(con, 7, "feather") == 1 and await items.take_in(con, 7, "feather", 5) is None
    assert await items.counts(bot, 7) == {"tower_key": 0, "rope": 0, "feather": 1}
    with pytest.raises(ValueError):
        await items.take(bot, 7, "summon_scroll")                     # it has its own (pets.take_summon_scroll)


async def test_a_take_is_never_retried(game_db, monkeypatch):
    """A reply lost after the commit must not take a second item: the take raises instead."""
    bot = game_db.bot
    await items.grant(bot, 7, "bait", 2)
    seen = []
    real = bot.db.fetchval

    async def fetchval(query, *args, **kwargs):
        seen.append((query.split()[0], kwargs.get("idempotent", True)))
        return await real(query, *args, **kwargs)

    monkeypatch.setattr(bot.db, "fetchval", fetchval)
    await items.take(bot, 7, "bait")
    await items.grant(bot, 7, "bait")
    await items.adjust(bot, 7, "bait", -1)
    assert seen == [("UPDATE", False), ("INSERT", False), ("UPDATE", False)]


async def test_the_summoning_scroll_is_kept_in_petowner(game_db):
    bot, db = game_db.bot, game_db.bot.db
    assert await items.grant(bot, 5, "summon_scroll", 2) == 2          # a player without a row gets one
    assert (await owner_row(db, 5))["summon_scrolls"] == 2 and await stored(db, 5, "summon_scroll") is None
    assert await items.count(bot, 5, "summon_scroll") == 2
    assert await items.adjust(bot, 5, "summon_scroll", -9) == 0
    assert await items.adjust(bot, 6, "summon_scroll", -1) == 0 and await owner_row(db, 6) is None
    assert await items.store_ready(bot, items.BY_KEY["summon_scroll"]) is True
    bot._mission_columns = False                                       # SUMMON_SCROLLS missing
    assert await items.store_ready(bot, items.BY_KEY["summon_scroll"]) is False
    assert await items.store_ready(bot, items.BY_KEY["bait"]) is True


# -- a drop ------------------------------------------------------------------------------

async def test_a_drop_of_a_new_item_goes_to_the_store(game_db):
    bot, db = game_db.bot, game_db.bot.db
    ala = game_db.add_member("Ala")
    assert await items.grant_drop(bot, ala, 19) is True and await items.grant_drop(bot, ala, 19) is True
    assert await stored(db, ala.id, "bait") == 2
    assert await items.grant_drop(bot, ala, 16) is True                # no pet, no row: the row is made
    assert (await owner_row(db, ala.id))["summon_scrolls"] == 1
    with pytest.raises(ValueError):
        await items.grant_drop(bot, ala, 21)                           # reserved: no item (callers never ask)


async def test_a_drop_is_left_out_only_while_its_store_is_missing(game_db, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    bot._player_items = (False, clock.monotonic())
    assert await items.grant_drop(bot, ala, 17) is False
    bot._mission_columns = False
    assert await items.grant_drop(bot, ala, 16) is False
    bot._player_items, bot._mission_columns = None, None
    assert await items.grant_drop(bot, ala, 17) is True

    async def broken(*args, **kwargs):
        raise DatabaseUnavailable("gone")

    monkeypatch.setattr(bot.db, "fetchval", broken)
    with pytest.raises(DatabaseUnavailable):                           # (the drop tells the owner to give it)
        await items.grant_drop(bot, ala, 17)


async def test_both_loot_tables_grant_the_new_items_for_real(game_db, monkeypatch, tmp_path):
    """The real dispatchers of boss.py and daily.py, the real store: the line, the hint, the row."""
    bot, db = game_db.bot, game_db.bot.db
    ala = game_db.add_member("Ala")
    folder = tmp_path / "config"
    folder.mkdir()
    rows = [{"id": 17, "descr": "🗝️ **Klucz do wieży**!", "weight": 100},
            {"id": 21, "descr": "List gończy (zarezerwowany)", "weight": 100}]
    for name, tables in (("lootConfig.json", ("Normal", "Rare", "Epic", "Legend", "Mythic")),
                         ("mobLootConfig.json", ("Normal", "Rare", "Epic"))):
        (folder / name).write_text(json.dumps({f"loot_details_{t}": rows for t in tables}), encoding="utf-8")
    monkeypatch.setattr(boss, "CONFIG_DIR", folder)
    monkeypatch.setattr(daily, "CONFIG_DIR", folder)
    monkeypatch.setattr(boss.random, "random", lambda: 0.5)
    hint = items.DROP_HINT
    expected = f"👉 🗝️ **Klucz do wieży**!\n{hint}\n👉 List gończy (zarezerwowany)\n"
    assert await boss.rand_loot(game_db.scene(ala), 0, ala, 0) == expected
    assert await daily.rand_loot(game_db.scene(ala), 0, ala, 0) == expected
    assert await stored(db, ala.id, "tower_key") == 2


# -- the Nieuchwytny pył in a fight (the fights themselves: test_boss, test_daily, test_tower) ---------

def test_the_dust_guard_lets_each_holder_off_once_and_nobody_else():
    guard = items.DustGuard([7, SimpleNamespace(id=8)])
    assert guard.left == {7, 8} and guard.used == []
    assert guard.take(SimpleNamespace(id=9)) is False                  # no dust in his pocket
    assert guard.take(SimpleNamespace(id=7)) is True
    assert guard.take(7) is False                                       # five dusts are not five lives
    assert guard.take(None) is False and guard.take(8) is True
    assert guard.left == set() and guard.used == [7, 8]
    assert items.DustGuard().take(7) is False


def test_whose_mistake_a_failed_round_was():
    ala, ola = SimpleNamespace(id=1, name="Ala"), SimpleNamespace(id=2, name="Ola")
    twin = SimpleNamespace(id=2, name="Ola (another object)")

    def answered_by(member):
        return challenge.RoundResult(challenge.Outcome.ANSWERED, None, SimpleNamespace(author=member, content="zle"))

    assert items.dust_culprit(challenge.RoundResult(challenge.Outcome.TIMEOUT), ala, [ala, ola]) is ala
    assert items.dust_culprit(answered_by(ala), ala, [ala, ola]) is ala          # his own wrong word
    assert items.dust_culprit(answered_by(twin), ala, [ala, ola]) is ola         # somebody else typed
    stranger = SimpleNamespace(id=3, name="Obcy")
    assert items.dust_culprit(answered_by(stranger), ala, [ala, ola]) is stranger


def test_the_dust_line_escapes_the_name_and_pings_nobody():
    holder = type("Member", (), {"id": 1, "__str__": lambda self: "*Ala*_@everyone"})()
    line = items.dust_line(holder, timed_out=False)
    assert line.startswith("Pomyliłeś się, ale **Nieuchwytny pył** gracza **\\*Ala\\*\\_@​everyone**")
    assert line.endswith(" rozwiewa się w powietrzu - nic się nie stało! ✨") and "<@" not in line
    assert items.dust_line(holder, timed_out=True).startswith("Nie zdążyłeś, ale **Nieuchwytny pył**")


async def test_the_dust_is_read_once_before_a_fight_and_taken_once_after_it(game_db, monkeypatch):
    bot, db = game_db.bot, game_db.bot.db
    await items.grant(bot, 1, "dust", 2)
    await items.grant(bot, 2, "dust", 1)
    await items.grant(bot, 3, "dust", 1)
    await items.adjust(bot, 3, "dust", -1)                              # a row at 0: no dust
    await items.grant(bot, 4, "bait", 5)                                # another item: no dust
    calls: list[tuple[str, str, bool]] = []
    for method in ("fetch", "fetchval", "fetchrow", "execute"):
        real = getattr(db, method)

        async def spy(query, *args, _real=real, _method=method, **kwargs):
            calls.append((_method, query.split()[0], kwargs.get("idempotent", True)))
            return await _real(query, *args, **kwargs)

        monkeypatch.setattr(db, method, spy)
    guard = await items.reserve_dust(bot, [SimpleNamespace(id=1), 2, 3, 4, 5, 1])
    assert guard.left == {1, 2} and guard.used == []
    assert [call for call in calls if call[1] != "SELECT" or call[0] != "fetchval"] == [("fetch", "SELECT", True)]
    calls.clear()
    await items.settle_dust(bot, guard)                                 # nothing used: not a single statement
    assert calls == []
    assert guard.take(1) and guard.take(2)
    await items.adjust(bot, 2, "dust", -1)                              # (he spent it meanwhile: nothing to take)
    calls.clear()
    await items.settle_dust(bot, guard)
    assert calls == [("fetchval", "UPDATE", False), ("fetchval", "UPDATE", False)]   # conditional, never retried
    assert (await stored(db, 1, "dust"), await stored(db, 2, "dust")) == (1, 0)
    await items.settle_dust(bot, guard)                                 # settled once: never twice
    assert await stored(db, 1, "dust") == 1 and guard.used == [] and bot.owner_log == []
    assert (await items.reserve_dust(bot, [])).left == set()


async def test_a_dust_the_database_cannot_read_or_take_never_stops_a_fight(game_db, monkeypatch):
    bot = game_db.bot
    await items.grant(bot, 1, "dust")
    bot._player_items = (False, clock.monotonic())                      # the store is not there
    assert (await items.reserve_dust(bot, [1])).left == set()
    bot._player_items = None
    real_fetch = bot.db.fetch

    async def broken(*args, **kwargs):
        raise DatabaseUnavailable("gone")

    monkeypatch.setattr(bot.db, "fetch", broken)
    assert (await items.reserve_dust(bot, [1])).left == set()          # an empty guard, no exception
    monkeypatch.setattr(bot.db, "fetch", real_fetch)
    guard = await items.reserve_dust(bot, [1])
    assert guard.take(1)
    monkeypatch.setattr(bot.db, "fetchval", broken)
    await items.settle_dust(bot, guard)                                 # taken or not: unknown - the owner checks
    assert bot.owner_log == [items.LOG_DUST_UNSURE.format(player_id=1, error="DatabaseUnavailable")]
    assert game_db.log_channel.messages[-1].content.startswith(f"<@{bot.ids.owner}>!")   # with a ping


# -- !przedmioty --------------------------------------------------------------------------

async def test_the_view_of_a_player_with_nothing(game_db):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    assert await items.view_lines(bot, ala.id) == []
    assert await items.show_items(game_db.scene(ala)) is False
    message = game_db.channel.messages[-1]
    assert message.content == (f"Twoja sakwa świeci pustkami, <@{ala.id}>. Ruszaj na **!polowanie** - potwory "
                               "same się nie okradną!")
    await add_owner(bot.db, ala.id, 0)                                 # a row of zeros is nothing either
    await items.grant(bot, ala.id, "bait", 0)
    assert await items.view_lines(bot, ala.id) == []


async def test_the_view_lists_only_what_he_has(game_db):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await add_owner(bot.db, ala.id, 0, reroll_scroll=5, reroll_scroll_shard=3)   # no pet at all
    await items.grant(bot, ala.id, "feather")
    await items.grant(bot, ala.id, "dust", 0)                          # (a used-up item keeps its row at 0)
    assert await items.show_items(game_db.scene(ala)) is True
    embed = game_db.channel.messages[-1].embeds[0]
    assert embed.title == "🎒 Przedmioty"
    assert embed.description.split("\n") == [
        f"Oto, co nosisz w sakwie, <@{ala.id}>:", "",
        "📜 **Zwoje odrodzenia**: 5 - **!odrodzenie**, **!mocneodrodzenie**",
        "🧩 **Fragmenty zwojów**: 3 - 10 fragmentów to pełny zwój (zamienią się przy **!towarzysz**)",
        "🪶 **Pióra Żar-ptaka**: 1 - **!oswiecenie**"]
    # Owner: nothing he does not have is listed (no "0" lines, the scrolls and keys included).
    assert not any(word in embed.description for word in ("Nieuchwytny", "Kamienie", "Lustra", "przywołania",
                                                          "Klucze"))


async def test_the_view_lists_the_thefts_and_the_pouch(game_db):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    xpsteal.grant(bot, ala.id, 10000)
    assert await items.view_lines(bot, ala.id) == [items.LINE_THEFTS.format(count=1)]   # a theft alone is something
    await add_owner(bot.db, ala.id, 5, egg_pouch=True, summon_scrolls=2)
    await add_pet(bot.db, 5, pet_lvl=1)
    lines = await items.view_lines(bot, ala.id)
    assert "📯 **Zwoje przywołania**: 2 - **!przywolaj**" in lines and lines[-1] == items.LINE_POUCH
    await bot.db.execute("UPDATE PETOWNER SET BIG_POUCH = TRUE WHERE PLAYER_ID = $1", ala.id)
    assert (await items.view_lines(bot, ala.id))[-1] == items.LINE_BIG_POUCH
    # Without the missions' columns (no summoning scrolls) there is no scroll line, without the table no item.
    bot._mission_columns = bot._owner_upgrades = False
    bot._player_items = (False, clock.monotonic())
    lines = await items.view_lines(bot, ala.id)
    assert not any("przywołania" in line or "Sakwa" in line for line in lines)


async def test_przedmioty_is_a_display_command_silent_during_a_fight(game_db, items_cog):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await items.grant(bot, ala.id, "rope", 2)
    command = bot.get_command("przedmioty")
    assert set(command.aliases) == {"ekwipunek", "eq", "plecak", "ekw"} and command.cog_name == "Przedmioty"
    for word in ("!przedmioty", "!eq", "!plecak"):
        await run(game_db, ala, word, channel=game_db.announce_channel)          # any channel of the server
        assert "🪢 **Liny sztygara**: 2" in game_db.announce_channel.messages[-1].embeds[0].description
    # A typed fight on the channel: not a word between its prompts (and no busy notice either).
    bob = game_db.add_member("Bob")
    with challenge.round_running(game_db.channel):
        before = len(game_db.channel.sent)
        await run(game_db, ala, "!przedmioty")
        with bot.state.hold_busy("hunt"):
            await run(game_db, ala, "!ekw")
        assert len(game_db.channel.sent) == before
    await run(game_db, bob, "!przedmioty")                             # afterwards: answered again
    assert game_db.channel.messages[-1].content.startswith("Twoja sakwa świeci pustkami, <@")
    # The busy policy of the display commands (the default: a notice while an event runs).
    with bot.state.hold_busy("event:CHEST"):
        await run(game_db, ala, "!przedmioty")
    assert game_db.channel.messages[-1].content == "Trwa teraz inne wydarzenie – spróbuj ponownie za chwilę " \
                                                    "<:Bedge:970576892874854400>"


# -- !DajPrzedmiot -------------------------------------------------------------------------

async def test_the_owner_gives_and_takes_items(game_db, items_cog):
    """(The member converter needs a real Discord member: the command's body is called as in test_missions.)"""
    bot, db = game_db.bot, game_db.bot.db
    admin, ala = admin_of(game_db), game_db.add_member("Ala")
    ctx = SimpleNamespace(channel=game_db.channel, author=admin, message=game_db.scene(admin).message)

    async def give(what: str) -> str:
        await items_cog.give_item.callback(items_cog, ctx, ala, what=what)
        return game_db.channel.messages[-1].content

    assert await give("klucz do wieży 3") == f"Gracz <@{ala.id}> ma teraz 🗝️ **Klucze do wieży**: 3."
    assert await stored(db, ala.id, "tower_key") == 3
    assert await give("tower_key -5") == f"Gracz <@{ala.id}> ma teraz 🗝️ **Klucze do wieży**: 0."   # never below 0
    await give("Pióro Żar-ptaka")                                                  # n left out: one
    await give("zwój przywołania 2")                                               # PETOWNER (the row is made)
    assert await stored(db, ala.id, "feather") == 1 and (await owner_row(db, ala.id))["summon_scrolls"] == 2
    assert bot.owner_log[-1] == (f"🎒 !DajPrzedmiot: {admin} - **Zwój przywołania** +2 dla gracza {ala} "
                                 f"(<@{ala.id}>), ma teraz: 2.")
    assert len([line for line in bot.owner_log if line.startswith("🎒 !DajPrzedmiot")]) == 4
    unknown = await give("miecz 2")                                                # no such item
    assert unknown.startswith("Nie znam przedmiotu **miecz**.") and "**tower_key** (Klucz do wieży)" in unknown
    bot._player_items = (False, clock.monotonic())                                 # the table is missing
    assert "chwilowo niedostępne" in await give("przynęta 9") and await stored(db, ala.id, "bait") is None
    assert len([line for line in bot.owner_log if line.startswith("🎒 !DajPrzedmiot")]) == 4

    # Typed by somebody who is not an administrator: nothing at all.
    bot._player_items = None
    before = len(game_db.channel.sent)
    await run(game_db, ala, f"!DajPrzedmiot {ala.id} przynęta 9")
    assert game_db.channel.sent[before:] == [] and await stored(db, ala.id, "bait") is None


def test_the_owners_command_reads_the_number_at_the_end():
    from altermmo.cogs import items as cog

    assert cog.parse_request("klucz do wieży 2") == ("klucz do wieży", 2)
    assert cog.parse_request("bait -3") == ("bait", -3) and cog.parse_request("bait +3") == ("bait", 3)
    assert cog.parse_request("Pióro Żar-ptaka") == ("Pióro Żar-ptaka", 1) and cog.parse_request("") == ("", 1)


def test_the_owners_command_is_admin_only_and_not_in_the_help():
    from altermmo.cogs import help as help_cog
    from altermmo.cogs import items as cog

    command = cog.ItemsCog.give_item
    assert isinstance(command, commands.Command)
    assert any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)
    assert "DajPrzedmiot" not in help_cog.HELP_TEXT and "!przedmioty" in help_cog.HELP_TEXT


# -- the patch notes ----------------------------------------------------------------------------

def test_the_patch_notes_entry_teases_the_items_and_names_no_effect():
    """Entry 20 (items_decisions.md §10): spoiler-free, no numbers, no item's name or effect, no aura's name -
    and every command, button and name it does give is the real one (the notes are final once posted).

    The bot posts an unsent entry at its next start: the entry came in the same commit that turned the
    drops, the wares and the auras on (§9), never before - so what it promises is switched on, checked
    below in the shipped files.  Dova's teaser came with Dova's prizes (§12), in the same commit."""
    from altermmo.cogs import items as cog
    from altermmo.features import auras, hub, merchant, pets
    from altermmo.features import patchnotes
    from altermmo.settings import Timings

    # (Found by its title: its number may change when the lead merges, and every later update adds an entry.)
    [entry] = [note for note in patchnotes.load_notes() if note.title == "Pełniejsze sakwy"]
    assert entry.date == "2026-11-26" and len(entry.changes) == 6
    text = " ".join(entry.changes)
    low = text.lower()
    assert patchnotes.spoiler_in(entry) is None                              # no percentage, no forbidden word
    assert not any(char.isdigit() for char in text)                           # no chances, prices or values
    assert "$" not in text and "lobby" not in low
    # What the players find out themselves: which items there are, what they and the auras do.
    for item in items.ITEMS:
        assert item.name.lower() not in low, item.name
    for kind in auras.KINDS.values():
        for word in (kind.name, kind.instrumental, kind.accusative, kind.stat_words, kind.stat_label):
            assert word.lower() not in low, word
    # The command, its short form, the button and the panel are the real ones.
    names = {command.name: command for command in cog.ItemsCog.__cog_commands__}
    assert "przedmioty" in names and "eq" in names["przedmioty"].aliases
    assert "**!przedmioty** (skrót **!eq**)" in text and "**Przedmioty** w **Panelu gry**" in text
    assert hub.PANEL_TITLE.endswith("Panel gry")
    view = hub.HubView()
    try:
        assert any(getattr(item, "custom_id", None) == hub.ITEMS_ID and item.label == "Przedmioty"
                   for item in view.children)
    finally:
        view.stop()
    # The new stat by its shown name, the scroll boss's half an hour.
    assert f"**{pets.MSG_MISSION_PERC_LABEL}**" in text
    assert "pół godziny" in text and Timings().scroll_boss_stay == 30 * 60
    # "więcej różnych przedmiotów ... więcej towarów", "z jajka wykluwa się ... aurą": all of it is on.
    assert "więcej różnych przedmiotów" in text and "więcej towarów" in text and "**aurą**" in text
    for name in ("lootConfig.json", "mobLootConfig.json"):
        config = json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
        rows = [row for table in config.values() for row in table if row["id"] in items.BY_LOOT_ID]
        assert rows and all(row["weight"] > 0 for row in rows), name
    shipped = merchant.load_config(CONFIG_DIR / "merchantConfig.json")
    wares = {ware: weight for ware, _name, _price, weight, _entry in shipped.items}
    assert all(wares[ware] > 0 for ware in ("summon_scroll", "feather", "bait", "revive_scroll", "rope", "dust"))
    assert auras.parse(json.loads((CONFIG_DIR / auras.CONFIG_FILE).read_text(encoding="utf-8"))).on
    # Dova's teaser (§12): it names him and his cups, never a prize, a command or what they do.
    from altermmo.features import dova, events
    assert entry.changes[-1] == ("Dova od kubków przywiózł z dalekich stron coś wyjątkowego - kto go ogra, ten może "
                                 "się zdziwić.")
    assert "Dova" in events.HAZARD_DESCR and "kubek" in " ".join(events.HAZARD_LABELS).lower()   # him, his cups
    assert all(word not in low for word in ("kość", "kosc", "karta", "karty", "!kosc", "!karta", "rzadkoś", "perk"))
    assert dova.DIE_CHANCE > 0 and dova.CARD_CHANCE > 0                  # what it promises is switched on
