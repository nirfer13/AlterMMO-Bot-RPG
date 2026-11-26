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
        (25, "dust", "Nieuchwytny pył", "✨")]
    assert items.RESERVED_LOOT_IDS == (21, 23, 24) and not set(items.RESERVED_LOOT_IDS) & set(items.BY_LOOT_ID)
    # Only the Zwój przywołania lives in PETOWNER; it and the key are always listed in !przedmioty.
    assert [item.key for item in items.ITEMS if item.in_petowner] == ["summon_scroll"]
    assert [item.key for item in items.ITEMS if item.always_shown] == ["summon_scroll", "tower_key"]
    assert all(len(item.key) <= 32 and item.hint and item.description and item.label for item in items.ITEMS)
    # No id of an older loot row (0-15) is taken.
    assert min(items.BY_LOOT_ID) == 16 and max(items.BY_LOOT_ID) == 25
    assert items.by_loot_id(17) is items.BY_KEY["tower_key"] and items.by_loot_id("19").key == "bait"
    assert items.by_loot_id(21) is None and items.by_loot_id(13) is None and items.by_loot_id(None) is None


@pytest.mark.parametrize("text, key", [
    ("tower_key", "tower_key"), ("Klucz do wieży", "tower_key"), ("klucz do wiezy", "tower_key"),
    ("KLUCZE DO WIEŻY", "tower_key"), ("zwój przywołania", "summon_scroll"), ("pioro zar-ptaka", "feather"),
    ("Pióro Żar ptaka", "feather"), ("przyneta", "bait"), ("lina", None), ("", None), ("nieuchwytny pył", "dust"),
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


async def test_the_view_lists_the_old_counters_the_scrolls_and_the_keys_and_what_he_has(game_db):
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
        "💎 **Kamienie olśnienia**: 0 - **!oswiecenie**",
        "🪞 **Lustra wizualiów**: 0 - **!transformacja**",
        "📯 **Zwoje przywołania**: 0 - **!przywolaj**",
        "🗝️ **Klucze do wieży**: 0 - **!klucz**",
        "🪶 **Pióra Żar-ptaka**: 1 - **!oswiecenie**"]
    assert "Nieuchwytny" not in embed.description                      # (none left: not listed)


async def test_the_view_lists_the_thefts_and_the_pouch(game_db):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    xpsteal.grant(bot, ala.id, 10000)
    assert await items.view_lines(bot, ala.id) == [                   # a theft alone is something
        "📜 **Zwoje odrodzenia**: 0 - **!odrodzenie**, **!mocneodrodzenie**",
        "🧩 **Fragmenty zwojów**: 0 - 10 fragmentów to pełny zwój (zamienią się przy **!towarzysz**)",
        "💎 **Kamienie olśnienia**: 0 - **!oswiecenie**", "🪞 **Lustra wizualiów**: 0 - **!transformacja**",
        "📯 **Zwoje przywołania**: 0 - **!przywolaj**", "🗝️ **Klucze do wieży**: 0 - **!klucz**",
        items.LINE_THEFTS.format(count=1)]
    await add_owner(bot.db, ala.id, 5, egg_pouch=True, summon_scrolls=2)
    await add_pet(bot.db, 5, pet_lvl=1)
    lines = await items.view_lines(bot, ala.id)
    assert "📯 **Zwoje przywołania**: 2 - **!przywolaj**" in lines and lines[-1] == items.LINE_POUCH
    await bot.db.execute("UPDATE PETOWNER SET BIG_POUCH = TRUE WHERE PLAYER_ID = $1", ala.id)
    assert (await items.view_lines(bot, ala.id))[-1] == items.LINE_BIG_POUCH
    # Without the missions' columns (no summoning scrolls) the line says 0, without the table no item.
    bot._mission_columns = bot._owner_upgrades = False
    bot._player_items = (False, clock.monotonic())
    lines = await items.view_lines(bot, ala.id)
    assert "📯 **Zwoje przywołania**: 0 - **!przywolaj**" in lines and not any("Sakwa" in line for line in lines)


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
