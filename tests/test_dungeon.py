"""The "Kopalnia Ostatniej Lampy" event (features/dungeon.py) and the egg a player places himself (features/pets.py)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from types import SimpleNamespace

import discord
import pytest

import altermmo.features
from altermmo.cogs import events as events_cog
from altermmo.cogs import help as help_cog
from altermmo.cogs.ui import UiCog
from altermmo.core import timing, ui
from altermmo.core.state import BossPhase, EventType
from altermmo.features import boss_cycle, dungeon, expsum, modifiers, patchnotes, patrons, pets, shortcuts
from altermmo.features import events as events_feature

from fakes import FakeInteraction
from test_boss_cycle import FAST, StubBoss
from test_merchant import make_owner, make_pet, row, scrolls_of, until
from test_pets import rig

PATRON = patrons.PATRON_ROLES[0]
#: The config the bot ships with.  ONE test reads it (it pins the package's table, the texts and the
#: owner's eggs); every other test plays on a copy of :data:`TABLE` of its own (the ``own_table``
#: fixture), so a change of the balance in ``config/dungeonConfig.json`` fails that one test - and
#: nothing else - until :data:`TABLE` is changed with it on purpose.
SHIPPED = dungeon.CONFIG_FILE

#: The package's table (``Opis wydarzenia.md``): name, chance of the descent into the chamber, XP, scrolls -
#: and the eggs of the owner's later message (chamber 4: 1, then one more per chamber), the level in the treasury.
TABLE = (
    ("Lampownia", Fraction(1), 20, 0, 0, False),
    ("Rozjazd wagoników", Fraction(9, 10), 40, 1, 0, False),
    ("Most nad Czarną Wodą", Fraction(4, 5), 70, 1, 0, False),
    ("Zatopiona pompownia", Fraction(2, 3), 110, 2, 1, False),
    ("Błękitna żyła", Fraction(5, 8), 180, 2, 2, False),
    ("Szyb wiszących klatek", Fraction(1, 2), 300, 3, 3, False),
    ("Wrota sztygara", Fraction(2, 5), 550, 4, 4, False),
    ("Skarbiec Ostatniej Lampy", Fraction(1, 4), 1000, 16, 5, True),
)
LOST_ALL = "traci wszystko z tej wyprawy"


# -- helpers -------------------------------------------------------------------------------

def asks(sent) -> bool:
    """A message with buttons that asks something (not the "Album" shortcut under a kept sacrifice)."""
    return sent.view is not None and not any(isinstance(item, shortcuts.AlbumButton) for item in sent.view.children)


class Luck:
    """Stands in for ``random`` inside the mine: every descent succeeds unless a result is queued."""

    def __init__(self) -> None:
        self.results: list[int] = []
        self.calls: list[int] = []

    def randrange(self, stop: int) -> int:
        self.calls.append(stop)
        return self.results.pop(0) if self.results else 0


@pytest.fixture(autouse=True)
def quick_mine(monkeypatch):
    """No waiting between two clicks, before the closing summary, or before an unfinished closing goes on."""
    monkeypatch.setattr(dungeon, "CLICK_GUARD", 0.0)
    monkeypatch.setattr(dungeon, "SUMMARY_DELAY", 0.0)
    monkeypatch.setattr(dungeon, "ORPHAN_WAIT", 0.01)
    monkeypatch.setattr(dungeon, "RETRY_EVERY", 0.0)
    monkeypatch.setattr(dungeon, "SAVE_RETRY", 0.0)


@pytest.fixture(autouse=True)
def own_table(monkeypatch, tmp_path):
    """The mine of the flow tests is read from a config written here from :data:`TABLE` (see :data:`SHIPPED`)."""
    path = tmp_path / "the-tests-own-table" / "dungeonConfig.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"chambers": [
        {"name": name, "chance": f"{chance.numerator}/{chance.denominator}", "xp": xp, "scrolls": scrolls,
         "eggs": eggs, "pet_level": int(level), "text": f"Opis komory {number}."}
        for number, (name, chance, xp, scrolls, eggs, level) in enumerate(TABLE, start=1)]}), encoding="utf-8")
    monkeypatch.setattr(dungeon, "CONFIG_FILE", path)
    return path


@pytest.fixture(autouse=True)
def ordinary_eggs(monkeypatch):
    """No test depends on the real dice of an egg: unless a test rigs them itself (``test_pets.rig``), every
    egg is an ordinary one (no class, so no mythic announcement) without a seal or a foil (so no note to the
    owner about rare cosmetics).  Only the rolls of ``pets._roll_egg`` / ``pets._roll_cosmetics`` are fixed."""
    randint, randrange = random.randint, random.randrange

    def plain_randint(low, high):
        return 0 if (low, high) == (0, 1000) else randint(low, high)              # the class roll: ordinary

    def plain_randrange(stop, *args, **kwargs):
        if not args and not kwargs and stop in (pets.COSMETIC_ROLL, pets.FRAME_ROLL):
            return stop - 1                                                        # no seal, no foil, the base frame
        return randrange(stop, *args, **kwargs)

    monkeypatch.setattr(random, "randint", plain_randint)
    monkeypatch.setattr(random, "randrange", plain_randrange)


@pytest.fixture
def luck(monkeypatch):
    stand_in = Luck()
    monkeypatch.setattr(dungeon, "random", stand_in)
    return stand_in


@pytest.fixture
def no_modifier(monkeypatch):
    """The boss modifier of a sacrifice is only noted (who sacrificed), not drawn."""
    seen: list[int] = []

    async def noted(scene, msg=True, sacrefice=False):
        assert msg is True and sacrefice is True
        seen.append(scene.author.id)
        return "noted"

    monkeypatch.setattr(modifiers, "random_modifiers", noted)
    return seen


async def open_mine(game) -> dungeon.Mine:
    """A new appearance of the mine (the one before, if any, is closed by it)."""
    return await dungeon.spawn_dungeon(game.scene())


async def enter(game, mine, player):
    """``player`` takes the entrance; returns the message of his expedition."""
    await game.click(mine.announcement, player, "enter")
    return mine.expeditions[player.id].message


async def dispatch(game, message, member, key: str) -> FakeInteraction:
    """A click on the mine's button ``key`` under ``message``, the way it reaches the bot in production.

    Also for a button the message no longer shows (a client that has not
    redrawn it yet, a second click on the page before): discord.py's own view
    store decides whether a view still answers that button of that message
    (``ViewStore.dispatch_view``), and the two listeners that see every
    interaction run as well (``cogs/ui.py``: a click nobody listens to;
    ``cogs/events.py``: an orphan click of the mine).
    """
    bot = game.bot
    custom_id = dungeon.KEY_PREFIX + key
    interaction = FakeInteraction(bot, member, message, channel=game.channel,
                                  data={"custom_id": custom_id, "component_type": 2})
    store = bot._connection._view_store
    before = set(store._ViewStore__tasks)
    store.dispatch_view(2, custom_id, interaction)
    await asyncio.gather(*(set(store._ViewStore__tasks) - before), UiCog(bot).on_interaction(interaction),
                         events_cog.EventsCog(bot).on_interaction(interaction))
    return interaction


async def late_enter(game, mine, player) -> FakeInteraction:
    """A click on "Wejdź do kopalni" from a client that still shows the button (through the dispatcher)."""
    return await dispatch(game, mine.announcement, player, "enter")


async def descend(game, message, player, chamber: int) -> None:
    """Go down (successfully - use it with ``luck``) until the expedition stands in ``chamber``."""
    for _ in range(chamber - 1):
        await game.click(message, player, "deeper")


async def go_down(game, player, chamber: int):
    """A new mine; ``player`` enters and goes down to ``chamber``.  Returns ``(mine, message)``."""
    mine = await open_mine(game)
    message = await enter(game, mine, player)
    await descend(game, message, player, chamber)
    return mine, message


async def leave(game, player, chamber: int):
    """A new mine; ``player`` goes down to ``chamber`` and takes the loot.  Returns ``(mine, message)``."""
    mine, message = await go_down(game, player, chamber)
    await game.click(message, player, "claim" if chamber == mine.config.last else "leave")
    return mine, message


def record_of(game, player) -> dict:
    """What ``data/dungeon.json`` holds for the player."""
    return game.bot.store.dungeon.load()["mine"]["expeditions"][str(player.id)]


def xp_of(game, player) -> int:
    return int(game.bot.store.exp_summary.load().get(str(player.id), 0))


async def pets_count(db) -> int:
    return int(await db.fetchval("SELECT COUNT(*) FROM PETS"))


async def album_of(db, player) -> list:
    return await db.fetch("SELECT * FROM PET_ALBUM WHERE PLAYER_ID = $1 ORDER BY ENTRY_ID", player.id)


def fates(game, player) -> list[str]:
    return [entry["fate"] for entry in record_of(game, player)["eggs"]]


async def restart(game) -> None:
    """What the next start of the bot does with the mine's file (the memory of this run is gone)."""
    bot = game.bot
    bot._mine, bot._mine_jobs, bot._mine_restart_done = None, [], False
    bot._mine_leftover, bot._mine_leftover_told = None, False
    bot.boss_cycle._tidied = True                                           # (the start-up clearing is done)
    await dungeon.close_after_restart(bot)


def summaries(game) -> list:
    """Every closing summary the bot ever posted (also those purged with the channel since)."""
    return [message for message in game.channel.sent
            if message.embeds and message.embeds[0].title == dungeon.SUMMARY_TITLE]


def places(*, active=False, pouch=False, free=(), occupied=(), pet=None, total=4) -> pets.EggPlaces:
    return pets.EggPlaces(active, pouch, total, tuple(free), tuple(occupied), pet)


def bad_request() -> discord.HTTPException:
    """The error Discord answers with when it refuses a message (HTTP 400: too long, a broken attachment ...)."""
    return discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"),  # type: ignore[arg-type]
                                 {"code": 50035, "message": "Invalid Form Body"})


def disk_full(value):
    raise OSError("disk full")


@pytest.fixture
def boss(monkeypatch, game_db):
    """The boss cycle with a boss that only shows its picture (the fights are not the mine's business)."""
    stand_in = StubBoss()
    monkeypatch.setitem(sys.modules, "altermmo.features.boss", stand_in)
    monkeypatch.setattr(altermmo.features, "boss", stand_in, raising=False)
    monkeypatch.setattr(boss_cycle, "IDLE_POLL", 0.01)
    game_db.bot.timings = FAST
    return stand_in


async def close_by(game, how: str) -> None:
    """The mine is closed by the next event, by the boss, by somebody who only closed the event, or by a restart."""
    bot = game.bot
    if how == "event":
        assert await events_cog.EventsCog(bot).run_one_event(forced=EventType.CHEST) is EventType.CHEST
    elif how == "boss":
        # What every phase of the boss cycle does when it needs the channel (the phases themselves:
        # the tests of "the boss never waits for the mine" below), then the channel is wiped.
        assert await bot.boss_cycle._wait_for_the_channel() is True
        await game.channel.purge(limit=None, check=lambda m: m.content != "Zasady gry")
    elif how == "unannounced":
        bot.state.close_event()
    else:
        await restart(game)
    await dungeon.tick(bot)


# -- the config, the chances, the words -------------------------------------------------------

def test_the_shipped_config_is_the_packages_table_with_more_eggs_in_the_deeper_rows():
    config = dungeon.load_config(SHIPPED)
    assert config.last == 8
    assert [(c.name, c.chance, c.xp, c.scrolls, c.eggs, c.pet_level) for c in config.chambers] == list(TABLE)
    assert [c.number for c in config.chambers] == list(range(1, 9)) and all(c.text for c in config.chambers)
    # The owner: an egg again from chamber 4, one more in every deeper row.
    assert [c.eggs for c in config.chambers] == [0, 0, 0, 1, 2, 3, 4, 5]
    assert config.chamber(4).text.endswith("Ktoś ułożył tu jajo na posłaniu ze szmat.")
    assert "Większość haków jest pusta." in config.chamber(1).text
    assert dungeon.page_too_long(config) == ""                                 # its texts fit every page
    # One entrance per appearance: there is no entry time to configure.
    assert "entry_seconds" not in json.loads(SHIPPED.read_text(encoding="utf-8"))
    # The flow tests play on their own copy of these numbers (``own_table``), not on this file.
    assert dungeon.CONFIG_FILE != SHIPPED and dungeon.load_config().chambers != config.chambers
    assert [(c.name, c.chance, c.xp, c.scrolls, c.eggs, c.pet_level) for c in dungeon.load_config().chambers] == list(TABLE)


def test_the_chances_are_exact_and_the_whole_way_is_one_and_a_half_percent():
    config = dungeon.load_config()
    assert config.chamber(4).chance == Fraction(2, 3) != Fraction(66, 100)        # two of three, not 66%
    assert config.chamber(5).chance == Fraction(5, 8)                             # 62.5%
    assert [config.reach(number) for number in range(1, 9)] == [
        Fraction(1), Fraction(9, 10), Fraction(72, 100), Fraction(48, 100), Fraction(30, 100),
        Fraction(15, 100), Fraction(6, 100), Fraction(15, 1000)]
    assert dungeon.chance_text(config.reach(8), approximate=True) == "1,5%"


def test_the_expected_number_of_eggs_of_every_plan():
    """Whoever always stops at chamber k brings 0.48 / 0.60 / 0.45 / 0.24 / 0.075 egg per expedition:
    the best plan gives 0.6 egg, the deep rows are the jackpot.  A change of the table changes this on purpose."""
    config = dungeon.load_config()
    assert [config.expected_eggs(number) for number in range(1, 9)] == [
        0, 0, 0, Fraction(48, 100), Fraction(60, 100), Fraction(45, 100), Fraction(24, 100), Fraction(75, 1000)]
    assert max(config.expected_eggs(number) for number in range(1, 9)) == Fraction(6, 10)


def test_a_broken_config_is_refused(tmp_path, own_table):
    path = tmp_path / "dungeonConfig.json"
    good = json.loads(own_table.read_text(encoding="utf-8"))
    chambers = good["chambers"]

    def with_chamber(index: int, **changes) -> dict:
        return {**good, "chambers": [{**c, **changes} if i == index else c for i, c in enumerate(chambers)]}

    for broken in ({key: value for key, value in good.items() if key != "chambers"},
                   {**good, "chambers": chambers[:1]},                       # one chamber is no descent
                   {**good, "chambers": [*chambers, chambers[-1]]},          # nine: there are eight pictures
                   {**good, "chambers": "osiem"},
                   {**good, "chambers": [*chambers[:-1], "skarbiec"]},
                   with_chamber(0, chance="90%"),                            # entering must be safe
                   with_chamber(3, chance="0%"),
                   with_chamber(3, chance="150%"),
                   with_chamber(3, chance="2/0"),
                   with_chamber(3, chance="często"),
                   with_chamber(3, chance=True),
                   with_chamber(3, xp=-5),
                   with_chamber(3, scrolls=1.5),
                   with_chamber(3, eggs=99),
                   with_chamber(7, pet_level=2),
                   with_chamber(2, name="  "),
                   with_chamber(2, name="N" * (dungeon.NAME_MAX + 1)),       # Discord would refuse the pages
                   with_chamber(5, text="T" * (dungeon.TEXT_MAX + 1))):
        path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(dungeon.ConfigError):
            dungeon.load_config(path)
    path.write_text("{nie json", encoding="utf-8")
    with pytest.raises(dungeon.ConfigError):
        dungeon.load_config(path)
    with pytest.raises(dungeon.ConfigError):
        dungeon.load_config(tmp_path / "nie-ma.json")
    # Every way of writing a chance is the same exact number; a key nobody knows is ignored.
    path.write_text(json.dumps({**with_chamber(3, chance="66,6%"), "entry_seconds": 600}), encoding="utf-8")
    assert dungeon.load_config(path).chamber(4).chance == Fraction(666, 1000)
    path.write_text(json.dumps(with_chamber(3, chance=40)), encoding="utf-8")
    assert dungeon.load_config(path).chamber(4).chance == Fraction(2, 5)
    # The longest names and texts the check lets through still give pages Discord takes - every one of them.
    longest = {**good, "chambers": [{**c, "name": "N" * dungeon.NAME_MAX, "text": "T" * dungeon.TEXT_MAX,
                                     "xp": 1_000_000, "scrolls": 100_000, "eggs": dungeon.MAX_EGGS}
                                    for c in chambers]}
    path.write_text(json.dumps(longest), encoding="utf-8")
    assert dungeon.page_too_long(dungeon.load_config(path)) == ""
    # ... and a page that would not fit is named (the check behind the two limits).
    roomy = dungeon.load_config(path)
    bloated = dungeon.Config(tuple(dataclasses.replace(c, text="T" * 5000) for c in roomy.chambers))
    assert dungeon.page_too_long(bloated) == "the page of chamber 1"


def test_polish_counts_and_chances():
    assert [dungeon.eggs_text(n) for n in (1, 2, 3, 4, 5, 10, 12, 22, 25)] == [
        "1 jajo", "2 jaja", "3 jaja", "4 jaja", "5 jaj", "10 jaj", "12 jaj", "22 jaja", "25 jaj"]
    assert [dungeon.chance_text(c) for c in (Fraction(1), Fraction(9, 10), Fraction(2, 3), Fraction(5, 8))] == [
        "100%", "90%", "2 na 3", "62,5%"]
    assert dungeon.chance_text(Fraction(1, 7), approximate=True) == "ok. 14,29%"
    config = dungeon.load_config()
    assert [c.loot for c in config.chambers] == [
        "20 XP", "40 XP + 1 zwój", "70 XP + 1 zwój", "110 XP + 2 zwoje + 1 jajo", "180 XP + 2 zwoje + 2 jaja",
        "300 XP + 3 zwoje + 3 jaja", "550 XP + 4 zwoje + 4 jaja",
        "1000 XP + 16 zwojów + 5 jaj + 1 poziom aktywnego peta"]


def test_the_mine_is_a_random_event_with_the_merchants_weight():
    order = events_cog.EVENT_ORDER
    here, merchant = order.index(EventType.DUNGEON), order.index(EventType.MERCHANT)
    assert order[-1] is EventType.RITUAL and EventType["DUNGEON"].value == 13
    assert events_cog.PEAK_WEIGHTS[here] == events_cog.PEAK_WEIGHTS[merchant] == 1
    assert events_cog.OFF_PEAK_WEIGHTS[here] == events_cog.OFF_PEAK_WEIGHTS[merchant] == 1
    assert events_cog.DEBUG_WEIGHTS[here] == 0
    # Passive: the next event closes it, the boss does not count it as an activity in its way.
    assert EventType.DUNGEON in events_cog.CLOSED_BY_NEXT and EventType.DUNGEON not in events_cog.PASSIVE
    assert EventType.DUNGEON in boss_cycle._PASSIVE_EVENTS


# -- the texts: the table, the warning, Discord's limits ----------------------------------------

def test_the_announcement_has_the_open_table_the_real_eggs_and_the_warning():
    config = dungeon.load_config()
    embed = dungeon.announcement_embed(config, latest=dungeon.LATEST.format(when="<t:1:R>"), picture_name="00.jpg")
    text = embed.description
    assert embed.title == dungeon.TITLE and embed.image.url == "attachment://00.jpg"
    assert text.startswith("Od trzydziestu lat nad szybem wisiała zgaszona lampa.")       # the package, verbatim
    for line in ("**1. Lampownia** · 100% · 20 XP",
                 "**4. Zatopiona pompownia** · 2 na 3 · 110 XP + 2 zwoje + 1 jajo",
                 "**5. Błękitna żyła** · 62,5% · 180 XP + 2 zwoje + 2 jaja",
                 "**7. Wrota sztygara** · 40% · 550 XP + 4 zwoje + 4 jaja",
                 "**8. Skarbiec Ostatniej Lampy** · 25% · 1000 XP + 16 zwojów + 5 jaj + 1 poziom aktywnego peta",
                 "**Od wejścia do skarbca · 1,5%.**"):
        assert line in text
    assert LOST_ALL in text and "następne wydarzenie albo boss - najpóźniej <t:1:R>" in text
    assert text.endswith(dungeon.MSG_ONE_ENTRANCE) and "kto pierwszy kliknie" in text
    # What the owner's rules replaced is gone: no "as much time as you need", no "one egg at most",
    # no "standard" egg, nothing about everybody playing an expedition of his own.
    for gone in ("tyle czasu, ile potrzebujesz", "najwyżej jedno", "standardow", "Każdy gracz", "lobby"):
        assert gone not in text
    assert len(text) <= 4096 and len(embed) <= 6000
    # Without a known closing time only the rule is stated.
    assert "najpóźniej" not in dungeon.announcement_embed(config).description
    # Once somebody went down the post names him; after the closing it says so.
    taken = dungeon.announcement_embed(config, went_down="<@5>").description
    assert taken.endswith(dungeon.MSG_WENT_DOWN.format(mention="<@5>")) and dungeon.MSG_ONE_ENTRANCE not in taken
    over = dungeon.announcement_embed(config, went_down="<@5>", closed=True,
                                      latest=dungeon.LATEST.format(when="<t:1:R>")).description
    assert over.endswith(dungeon.MSG_HAD_GONE_DOWN.format(mention="<@5>") + "\n" + dungeon.MSG_ANNOUNCE_CLOSED)
    # A closed mine warns of nothing any more: no rule in the present tense, no moment that has passed.
    assert "najpóźniej" not in over and LOST_ALL not in over and "schodzi <@5>" not in over
    assert "**4. Zatopiona pompownia** · 2 na 3" in over                       # (the table stays)
    unused = dungeon.announcement_embed(config, closed=True).description
    assert unused.endswith(dungeon.MSG_ANNOUNCE_CLOSED) and dungeon.MSG_ONE_ENTRANCE not in unused
    assert LOST_ALL not in unused
    assert len(dungeon.announcement_embed(config, went_down="<@" + "9" * 20 + ">", closed=True,
                                          latest=dungeon.LATEST.format(when="<t:1700000000:R>"))) <= 6000


def test_every_panel_version_fits_discord_and_warns_about_the_closing():
    config = dungeon.load_config()
    assert dungeon.page_too_long(config) == ""
    latest = dungeon.LATEST.format(when="<t:1700000000:R>")
    long_pet = {"pet_id": 5, "pet_name": "Ż" * 255, "pet_lvl": 2, "type": "Cat"}
    crowded = places(pet=long_pet, total=10, occupied=tuple((n, n, long_pet) for n in range(1, 11)))
    roomy = places(active=True, pouch=True, free=range(1, 11), total=10)
    for number in range(1, config.last + 1):
        for state in (crowded, roomy, None):
            for notice in ("", dungeon.MSG_STABLED.format(name="Ż" * 60)):
                embed = dungeon.chamber_embed(config, 10 ** 18, number, state, latest=latest, notice=notice,
                                              picture_name="03-pet.jpg")
                assert LOST_ALL in embed.description and "najpóźniej <t:1700000000:R>" in embed.description
                assert len(embed.description) <= 4096 and len(embed.title) <= 256 and len(embed) <= 6000
                assert "lobby" not in embed.description.lower() and "standardow" not in embed.description
        table = dungeon.table_embed(config, 10 ** 18, number, latest=latest)
        assert LOST_ALL in table.description and "**4. Zatopiona pompownia** · 2 na 3" in table.description
        assert len(table.description) <= 4096 and len(table) <= 6000
    # Every chamber's page says where an egg could go right now (the package: "stan miejsca na jajo") ...
    for number in range(1, config.last + 1):
        assert "🥚 **Jaja:**" in dungeon.chamber_embed(config, 1, number, roomy).description
        assert dungeon.ROOM_ACTIVE in dungeon.chamber_embed(config, 1, number, roomy).description
        assert dungeon.MSG_NO_ROOM in dungeon.chamber_embed(config, 1, number, crowded).description
    # ... unless no egg can be reached from there (a table without eggs in that row and below).
    shallow = dungeon.Config(tuple(dataclasses.replace(c, eggs=0 if c.number != 4 else 1) for c in config.chambers))
    assert "🥚" in dungeon.chamber_embed(shallow, 1, 4, roomy).description
    assert "🥚" not in dungeon.chamber_embed(shallow, 1, 5, roomy).description
    # ... and the level before and in the treasury, with the pet's name and the change.
    assert "🐾" not in dungeon.chamber_embed(config, 1, 6, roomy).description
    for number in (7, 8):
        assert "poziom 2 → 3" in dungeon.chamber_embed(config, 1, number, crowded).description
        assert dungeon.MSG_LEVEL_NONE in dungeon.chamber_embed(config, 1, number, roomy).description
    assert "szansa zejścia **25%**" in dungeon.chamber_embed(config, 1, 7, roomy).description
    assert dungeon.MSG_TREASURY in dungeon.chamber_embed(config, 1, 8, roomy).description
    # After leaving: the worst case of eggs, each with its fate, and the egg warning while one is undecided.
    eggs = [{"egg": {}, "fate": pets.EGG_SWAPPED, "place": 10, "pet": "**" + "Ż" * 120 + "** · Kot · poziom 3"}
            for _ in range(dungeon.MAX_EGGS - 1)] + [{"egg": {}, "fate": dungeon.UNDECIDED}]
    record = {"state": dungeon.EGGS, "chamber": 8, "eggs": eggs,
              "granted": {"xp": 1000, "scrolls": 16, "level": {"name": "Ż" * 120, "old": 2, "new": 3},
                          "failed": ["5 jaj", "16 zwojów odrodzenia"]}}
    result = dungeon.result_embed(config, 10 ** 18, record, latest=latest, notice=dungeon.MSG_EGG_FAILED,
                                  egg_lines=["*Absolutnie nadzwyczajny towarzysz.*", "Ramka: " + "x" * 60])
    assert "wynieść z poziomu 8" in result.description and "Jajo 10 z 10" in result.description
    assert "jaja bez decyzji trafią tam, gdzie jest wolne miejsce" in result.description
    assert "najpóźniej <t:1700000000:R>" in result.description
    assert len(result.description) <= 4096 and len(result) <= 6000
    for state in (dungeon.LOST, dungeon.CLOSED):
        ended = dungeon.ended_embed(config, 10 ** 18, {"state": state, "chamber": 7})
        assert len(ended.description) <= 4096 and "550 XP + 4 zwoje + 4 jaja" in ended.description
    assert dungeon.MSG_FAILED_DESCENT in dungeon.ended_embed(config, 1, {"state": dungeon.LOST, "chamber": 1}).description


def test_the_summary_is_one_message_whatever_it_has_to_say():
    assert dungeon.summary_text([], {}) == ""
    assert dungeon.summary_text([1], {}) == dungeon.SUMMARY_LOST.format(players="<@1>")
    text = dungeon.summary_text([1, 2], {3: ["jajo 1 z 2 trafiło do sakwy", "jajo 2 z 2 przepadło"]})
    assert "stracili cały łup z wyprawy:** <@1>, <@2>." in text
    assert "• <@3>: jajo 1 z 2 trafiło do sakwy; jajo 2 z 2 przepadło" in text
    crowd = dungeon.summary_text(list(range(10 ** 18, 10 ** 18 + 30)),
                                 {10 ** 18 + n: ["jajo 1 z 5 " + dungeon.FATE_TEXT[dungeon.EGG_LOST]] * 5
                                  for n in range(300)})
    assert len(crowd) <= 4096 and "…i jeszcze" in crowd


def test_the_patch_notes_entry_is_for_players_and_spoiler_free():
    notes = patchnotes.load_notes()
    # (Found by its title: its number changes when the lead merges, and every later update adds an entry.)
    [entry] = [note for note in notes if note.title == "Kopalnia Ostatniej Lampy"]
    assert entry.date == "2026-11-23"
    text = " ".join(entry.changes)
    assert patchnotes.spoiler_in_text(text) is None                          # no percentage, no forbidden word
    assert not any(char.isdigit() for char in text)                           # no numbers of rewards or chances
    assert "traci wszystko" in text and "kto pierwszy" in text and "lobby" not in text.lower()


def test_the_converted_pictures_are_there_and_small():
    from PIL import Image

    names = ["00.jpg", "00-pet.jpg", *(f"{n:02d}.jpg" for n in range(1, 9)), "03-pet.jpg"]
    for name in names:
        path = dungeon.PICTURES_DIR / name
        assert path.is_file() and path.stat().st_size < 600_000, name          # (the package's PNGs: ~2 MB each)
        with Image.open(path) as image:
            assert image.format == "JPEG" and image.size == (1024, 1024)
    assert not list(dungeon.PICTURES_DIR.glob("*.png"))
    assert dungeon.picture(0).name == "00.jpg" and dungeon.picture(3).name == "03.jpg"
    assert dungeon.picture(3, with_pet=True).name == "03-pet.jpg"
    assert dungeon.picture(5, with_pet=True).name == "05.jpg"                 # no "-pet" picture: the plain one


# -- the one entrance ----------------------------------------------------------------------------

async def test_the_first_player_who_clicks_goes_down_safely(game_db, luck):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    state = game.bot.state
    # Passive like the merchant: the event is open, the channel is free.
    assert state.event_alive and state.event_type is EventType.DUNGEON and not state.busy
    announcement = mine.announcement
    assert announcement.button_labels == [dungeon.ENTER_LABEL] and announcement.files == ["00.jpg"]
    assert LOST_ALL in announcement.embeds[0].description and "<@&" not in announcement.text  # nobody is pinged
    assert dungeon.MSG_ONE_ENTRANCE in announcement.embeds[0].description

    message = await enter(game, mine, ala)
    assert luck.calls == []                                                   # entering never rolls
    assert message.button_labels == [dungeon.LEAVE_LABEL, dungeon.DEEPER_LABEL, dungeon.STABLE_LABEL,
                                     dungeon.TABLE_LABEL]
    embed = message.embeds[0]
    assert embed.title == "⛏️ 1/8 · Lampownia" and ala.mention in embed.description
    assert "*Opis komory 1.*" in embed.description                             # the chamber's text of the config
    assert "**Powrót teraz:** 20 XP" in embed.description
    # The state of her places for an egg is on the page from the first chamber on (she has nothing yet).
    assert "🥚 **Jaja:**" in embed.description and dungeon.ROOM_ACTIVE in embed.description
    assert "**Następna komora (Rozjazd wagoników):** 40 XP + 1 zwój · szansa zejścia **90%**" in embed.description
    assert LOST_ALL in embed.description and message.files == ["01.jpg"]
    assert record_of(game, ala) == {"state": "chamber", "chamber": 1, "eggs": [], "granted": None}
    assert xp_of(game, ala) == 0 and await pets_count(db) == 0               # nothing is given for entering
    # The entrance is used up like a chest: its button is gone and the post says who went down.
    assert announcement.view is None and announcement.files == ["00.jpg"]      # (she walks alone)
    assert LOST_ALL in announcement.embeds[0].description                       # the warning stays while it is open
    assert announcement.embeds[0].description.endswith(dungeon.MSG_WENT_DOWN.format(mention=ala.mention))
    assert dungeon.is_open(game.bot) and state.event_type is EventType.DUNGEON  # the expedition goes on


async def test_one_expedition_per_appearance_whoever_clicks_afterwards(game_db, luck, monkeypatch):
    game = game_db
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola"), game.add_member("Ela")
    mine = await open_mine(game)
    message = await enter(game, mine, ala)
    # The button itself is gone from the announcement - but the bot still listens to it while the mine is
    # open: a client that still shows the button must be told "taken", not "this event is over".
    assert mine.announcement.view is None and ui.is_live(mine.announcement.id)
    # A second player and the same player again click it (through discord.py's dispatcher): nothing starts.
    second, again = await late_enter(game, mine, ola), await late_enter(game, mine, ala)
    assert second.answers == [dungeon.MSG_TAKEN] and again.answers == [dungeon.MSG_TAKEN]
    assert second.public[0].startswith(ola.mention) and not second.whispers       # an ordinary public message
    assert list(mine.expeditions) == [ala.id] and mine.expeditions[ala.id].message is message
    assert record_of(game, ala)["chamber"] == 1 and luck.calls == []
    # One line for everybody: twenty late clicks do not post twenty notices.
    monkeypatch.setattr(ui, "NOTICE_GAP", 10.0)
    assert dungeon.MSG_TAKEN in ui.GENERAL_NOTICES
    crowd = [await late_enter(game, mine, player) for player in (ola, ela, ala, ola)]
    assert sum(len(click.replies) for click in crowd) == 1 and all(click.response.is_done() for click in crowd)
    # Also when the expedition is over there is no second one in this appearance: the mine is closed,
    # nobody listens to its entrance any more and the click is answered as every click on an event that is over.
    monkeypatch.setattr(ui, "NOTICE_GAP", 0.0)
    await game.click(message, ala, "leave")
    assert mine.closed and not ui.is_live(mine.announcement.id)
    assert (await late_enter(game, mine, ola)).answers == [ui.MSG_STALE]
    assert list(mine.expeditions) == [ala.id] and xp_of(game, ala) == 20 and xp_of(game, ola) == 0
    # The next appearance has an entrance of its own.
    other = await open_mine(game)
    assert await enter(game, other, ola) is not None and list(other.expeditions) == [ola.id]


async def test_two_first_clicks_at_the_same_moment_let_one_player_in(game_db):
    game = game_db
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    mine = await open_mine(game)
    first, second = await asyncio.gather(game.click(mine.announcement, ala, "enter"),
                                         game.click(mine.announcement, ola, "enter"))
    assert list(mine.expeditions) == [ala.id] and second.answers == [dungeon.MSG_TAKEN] and first.replies == []
    assert sum(1 for m in game.channel.messages if m.embeds and m.embeds[0].title == "⛏️ 1/8 · Lampownia") == 1
    assert ala.mention in mine.announcement.embeds[0].description
    assert ola.mention not in mine.announcement.embeds[0].description
    # The same player with two devices enters once as well.
    mine = await open_mine(game)
    await asyncio.gather(game.click(mine.announcement, ola, "enter"), game.click(mine.announcement, ola, "enter"))
    assert list(mine.expeditions) == [ola.id]
    assert sum(1 for m in game.channel.messages if m.embeds and m.embeds[0].title == "⛏️ 1/8 · Lampownia") == 1


async def test_an_expedition_that_cannot_be_posted_leaves_the_entrance_free(game_db):
    game = game_db
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    mine = await open_mine(game)
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("Discord is down"), 1
    failed = await game.click(mine.announcement, ala, "enter")
    assert failed.answers == [ui.MSG_FAILED] and mine.expeditions == {}
    assert mine.announcement.button_labels == [dungeon.ENTER_LABEL] and dungeon.is_open(game.bot)
    assert game.bot.store.dungeon.load()["mine"]["expeditions"] == {}
    assert await enter(game, mine, ola) is not None                           # nothing happened: the next click enters


async def test_nobody_enters_and_the_mine_closes_with_the_next_event(game_db):
    game = game_db
    cog = events_cog.EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON
    mine = dungeon.current(game.bot)
    await dungeon.tick(game.bot)
    assert dungeon.is_open(game.bot)                                          # it waits, like an unopened chest
    assert await cog.run_one_event(forced=EventType.CHEST) is EventType.CHEST
    await dungeon.tick(game.bot)
    assert mine.closed and summaries(game) == [] and game.bot.owner_log == []


async def test_the_mine_closes_once_its_one_expedition_is_over(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    state = game.bot.state
    mine, message = await go_down(game, ala, 3)
    assert dungeon.is_open(game.bot)
    await game.click(message, ala, "leave")                                    # taken: nothing is left to do
    assert mine.closed and not state.event_alive and state.event_type is EventType.NONE and state.free_for_activity
    assert mine.announcement.embeds[0].description.endswith(
        dungeon.MSG_HAD_GONE_DOWN.format(mention=ala.mention) + "\n" + dungeon.MSG_ANNOUNCE_CLOSED)
    assert summaries(game) == []                                              # nothing to report: no message
    # A failed descent ends it as well.
    mine, message = await go_down(game, ala, 2)
    luck.results = [4]
    await game.click(message, ala, "deeper")
    assert mine.closed and not state.event_alive and summaries(game) == []
    # With eggs to decide it stays open until the last of them is decided.
    mine, message = await leave(game, ala, 5)
    assert dungeon.is_open(game.bot) and not mine.closed
    await game.click(message, ala, "egg-take")
    assert dungeon.is_open(game.bot)
    await game.click(message, ala, "egg-stable")
    assert mine.closed and not state.event_alive and summaries(game) == []


# -- the descent ---------------------------------------------------------------------------------

async def test_exactly_one_draw_per_descent_with_the_exact_chance(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    message = await enter(game, mine, ala)
    # One draw per click, over the denominator of the chamber's chance; the highest winning result wins.
    for chamber, (stop, last_win) in enumerate([(10, 8), (5, 3), (3, 1), (8, 4), (2, 0), (5, 1), (4, 0)], start=2):
        luck.results = [last_win]
        before = len(luck.calls)
        await game.click(message, ala, "deeper")
        assert luck.calls[before:] == [stop] and mine.expeditions[ala.id].chamber == chamber
        assert record_of(game, ala)["chamber"] == chamber
    assert message.embeds[0].title == "⛏️ 8/8 · Skarbiec Ostatniej Lampy"
    assert message.button_labels == [dungeon.CLAIM_LABEL, dungeon.STABLE_LABEL, dungeon.TABLE_LABEL]
    assert message.files == ["08.jpg"] and dungeon.MSG_TREASURY in message.embeds[0].description
    assert xp_of(game, ala) == 0                                               # nothing until she takes it


@pytest.mark.parametrize("chamber,first_loss", [(2, 9), (3, 4), (4, 2), (5, 5), (6, 1), (7, 2), (8, 1)])
async def test_the_first_losing_result_of_every_chamber_ends_the_expedition(game_db, luck, chamber, first_loss):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=7, pet_id=1)
    await make_pet(db, 1, level=2, name="Iskra")
    mine, message = await go_down(game, ala, chamber - 1)
    luck.results = [first_loss]
    await game.click(message, ala, "deeper")
    assert record_of(game, ala)["state"] == "lost" and message.view is None
    text = message.embeds[0].description
    assert message.embeds[0].title == dungeon.LOST_TITLE and dungeon.MSG_FAILED_DESCENT in text
    assert f"({TABLE[chamber - 1][0]})" in text and mine.config.chamber(chamber - 1).loot in text
    # Only the loot of this expedition is gone: what she had before is untouched.
    assert xp_of(game, ala) == 0 and await scrolls_of(db, ala.id) == 7
    assert (await row(db, ala.id))["pet_id"] == 1 and await pets_count(db) == 1
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 1")) == 2
    # The buttons are gone; a click from a client that still shows them rolls nothing.
    late = await dispatch(game, message, ala, "deeper")
    assert late.answers == [ui.MSG_STALE] and len(luck.calls) == chamber - 1
    assert record_of(game, ala)["state"] == "lost" and xp_of(game, ala) == 0


async def test_a_descent_makes_one_random_draw_and_nothing_else(game_db, monkeypatch):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    message = await enter(game, mine, ala)
    draws: list[str] = []
    for name in ("randint", "choice", "choices", "random", "uniform", "shuffle", "sample"):
        real = getattr(random, name)

        def counted(*args, _real=real, _name=name, **kwargs):
            draws.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(random, name, counted)
    monkeypatch.setattr(random, "randrange", lambda stop: draws.append(f"randrange({stop})") or 0)
    await game.click(message, ala, "table")
    await game.click(message, ala, "back")
    await game.click(message, ala, "stable")
    assert draws == []                                                        # looking around never rolls
    await game.click(message, ala, "deeper")
    assert draws == ["randrange(10)"] and mine.expeditions[ala.id].chamber == 2


async def test_double_and_concurrent_clicks_descend_once(game_db, luck, monkeypatch):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    message = await enter(game, mine, ala)
    await asyncio.gather(*(game.click(message, ala, "deeper") for _ in range(3)))
    assert luck.calls == [10] and mine.expeditions[ala.id].chamber == 2
    # A second click that arrives right after the new page was shown was made on the old one.
    monkeypatch.setattr(dungeon, "CLICK_GUARD", 30.0)
    echo = await game.click(message, ala, "deeper")
    assert luck.calls == [10] and mine.expeditions[ala.id].chamber == 2 and echo.response.deferred
    assert echo.replies == []                                                 # acknowledged, nothing posted
    mine.expeditions[ala.id].shown_at -= 60                                    # she has read the page meanwhile
    await game.click(message, ala, "deeper")
    assert luck.calls == [10, 5] and mine.expeditions[ala.id].chamber == 3


async def test_a_click_on_a_button_the_page_no_longer_has_is_acknowledged(game_db, luck, monkeypatch):
    """discord.py drops a click whose button the view has no more: the player must not read "interaction failed"."""
    game = game_db
    ala = game.add_member("Ala")
    monkeypatch.setattr(dungeon, "ORPHAN_WAIT", 0.01)
    mine, message = await go_down(game, ala, 2)
    await game.click(message, ala, "table")                                    # the page has only "back" now

    def click_on(custom_id: str, target=message) -> FakeInteraction:
        return FakeInteraction(game.bot, ala, target, data={"custom_id": custom_id, "component_type": 2})

    orphan = click_on(dungeon.KEY_PREFIX + "leave")                             # pressed on the page before
    await dungeon.answer_orphan(orphan)
    assert orphan.response.deferred and orphan.replies == [] and xp_of(game, ala) == 0 and luck.calls == [10]
    # A click its view answers is left alone; so is every button that is not the mine's.
    answered = await game.click(message, ala, "back")
    await dungeon.answer_orphan(answered)
    foreign = click_on(ui.FLOW_PREFIX + "yes")
    await dungeon.answer_orphan(foreign)
    assert not foreign.response.is_done()
    cog = events_cog.EventsCog(game.bot)
    late = click_on(dungeon.KEY_PREFIX + "claim")
    await cog.on_interaction(late)
    assert late.response.deferred
    # Under a message nobody listens to any more (the mine is closed) cogs/ui.py answers ("this no longer works").
    game.bot.state.close_event()
    await dungeon.tick(game.bot)
    dead = click_on(dungeon.KEY_PREFIX + "enter", mine.announcement)
    await dungeon.answer_orphan(dead)
    assert not dead.response.is_done()


async def test_the_table_button_shows_the_table_and_never_rolls(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 3)
    calls = list(luck.calls)
    click = await game.click(message, ala, "table")
    assert click.replies == []                                                # a page of the same message
    page = message.embeds[0]
    assert page.title == "⛏️ 3/8 · Most nad Czarną Wodą · Tabela nagród"
    assert "**6. Szyb wiszących klatek** · 50% · 300 XP + 3 zwoje + 3 jaja" in page.description
    assert LOST_ALL in page.description and message.button_labels == [dungeon.BACK_LABEL]
    await game.click(message, ala, "back")
    assert message.embeds[0].title == "⛏️ 3/8 · Most nad Czarną Wodą" and dungeon.DEEPER_LABEL in message.button_labels
    assert luck.calls == calls and record_of(game, ala)["chamber"] == 3
    assert message.files == ["03.jpg"]                                        # (no pet at her side: the plain bridge)


async def test_the_stable_button_moves_the_pet_and_refreshes_the_offer(game_db, luck):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4)
    for pet_id in (1, 2, 3, 4):
        await make_pet(db, pet_id, level=1, name="Iskra" if pet_id == 1 else f"Pet{pet_id}")
    mine, message = await go_down(game, ala, 3)
    assert message.files == ["03-pet.jpg"]                                    # a hatched pet walks with her
    assert mine.announcement.files == ["00-pet.jpg"]                           # ... also on the entrance picture
    assert mine.announcement.embeds[0].image.url == "attachment://00-pet.jpg"
    assert dungeon.ROOM_STABLE.format(count=1) in message.embeds[0].description
    calls = list(luck.calls)
    await game.click(message, ala, "stable")
    now = await row(db, ala.id)
    assert (now["pet_id"], now["pet_id_alt4"]) == (0, 1)                       # the first free place
    text = message.embeds[0].description
    assert "Iskra odpoczywa w stajni. Masz teraz miejsce na jajo." in text
    assert dungeon.ROOM_ACTIVE in text and "wolne miejsca w stajni" not in text
    assert message.files == ["03.jpg"] and luck.calls == calls                 # never rolls
    await game.click(message, ala, "stable")
    assert dungeon.MSG_STABLE_NOBODY in message.embeds[0].description
    # A full stable: the pet stays, and the page says so.
    ola = game.add_member("Ola")
    await make_owner(db, ola.id, pet_id=5, pet_id_alt1=6, pet_id_alt2=7, pet_id_alt3=8, pet_id_alt4=9)
    for pet_id in range(5, 10):
        await make_pet(db, pet_id, level=1)
    mine, other = await go_down(game, ola, 1)
    await game.click(other, ola, "stable")
    assert dungeon.MSG_STABLE_FULL in other.embeds[0].description and (await row(db, ola.id))["pet_id"] == 5
    assert record_of(game, ola)["chamber"] == 1 and luck.calls == calls


async def test_clicks_of_somebody_else_are_refused(game_db, luck):
    game = game_db
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    mine = await open_mine(game)
    message = await enter(game, mine, ala)
    for key in ("deeper", "leave", "stable", "table"):
        foreign = await game.click(message, ola, key)
        assert foreign.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    assert luck.calls == [] and record_of(game, ala)["chamber"] == 1 and xp_of(game, ola) == 0


async def test_clicks_wait_while_a_hunt_plays(game_db, luck):
    game = game_db
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    mine = await open_mine(game)
    with game.bot.state.hold_busy("hunt"):                                     # a hunt is on: nobody goes down
        entering = await game.click(mine.announcement, ola, "enter")
    assert entering.answers == [ui.MSG_BUSY] and mine.expeditions == {}
    assert mine.announcement.button_labels == [dungeon.ENTER_LABEL]            # the entrance waits for later
    message = await enter(game, mine, ala)
    with game.bot.state.hold_busy("tower"):
        refused = await game.click(message, ala, "deeper")
    assert luck.calls == [] and refused.response.is_done()                     # (one busy notice per 20 s)
    await game.click(message, ala, "deeper")                                    # the run is over
    assert luck.calls == [10]


# -- taking the loot -------------------------------------------------------------------------------

@pytest.mark.parametrize("chamber", range(1, 9))
async def test_every_row_pays_exactly_what_the_table_says(game_db, luck, chamber):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=3, pet_id=1)
    await make_pet(db, 1, level=1, name="Iskra")
    _name, _chance, xp, scrolls, eggs, level = TABLE[chamber - 1]
    mine, message = await leave(game, ala, chamber)
    assert xp_of(game, ala) == xp and await scrolls_of(db, ala.id) == 3 + scrolls
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 1")) == (2 if level else 1)
    record = record_of(game, ala)
    assert len(record["eggs"]) == eggs and all(entry["fate"] == "" for entry in record["eggs"])
    assert record["state"] == ("eggs" if eggs else "done") and record["chamber"] == chamber
    assert record["granted"] == {"xp": xp, "scrolls": scrolls, "failed": [],
                                 "level": {"name": "Iskra", "old": 1, "new": 2} if level else None}
    assert await pets_count(db) == 1                                           # an egg is stored when it is placed
    text = message.embeds[0].description
    assert message.embeds[0].title == f"⛏️ Powrót z łupem · {chamber}/8 · {TABLE[chamber - 1][0]}"
    assert f"wynieść z poziomu {chamber}.”" in text and f"✅ {xp} XP" in text
    assert ("zwoj" in text or "zwój" in text) is bool(scrolls)
    assert ("✅ awans: Iskra · poziom 1 → 2" in text) is level
    if eggs:
        assert f"Jajo 1 z {eggs}" in text and text.count("czeka na Twoją decyzję") == eggs
        assert "jaja bez decyzji trafią tam, gdzie jest wolne miejsce" in text and message.view is not None
        assert not mine.closed
    else:
        assert "🥚" not in text and message.view is None and mine.closed
    assert len(luck.calls) == chamber - 1


async def test_double_and_concurrent_clicks_pay_once(game_db, luck):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=0)
    mine, message = await go_down(game, ala, 5)
    await asyncio.gather(*(game.click(message, ala, "leave") for _ in range(3)))
    assert xp_of(game, ala) == 180 and await scrolls_of(db, ala.id) == 2
    assert len(record_of(game, ala)["eggs"]) == 2
    # After the end the old buttons are gone; a click from a client that still shows "Wracam z łupem"
    # is only acknowledged (through the dispatcher) and pays nothing.
    assert dungeon.LEAVE_LABEL not in message.button_labels and dungeon.DEEPER_LABEL not in message.button_labels
    late = await dispatch(game, message, ala, "leave")
    assert late.response.is_done() and late.replies == []
    assert xp_of(game, ala) == 180 and await scrolls_of(db, ala.id) == 2 and len(luck.calls) == 4
    assert len(record_of(game, ala)["eggs"]) == 2


async def test_the_treasury_level_goes_to_the_pet_at_the_players_side(game_db, luck):
    game, db = game_db, game_db.bot.db

    async def claim(name: str, **owner):
        player = game.add_member(name)
        await make_owner(db, player.id, **owner)
        _mine, message = await go_down(game, player, 7)
        before = message.embeds[0].description                                 # the page before the last descent
        await game.click(message, player, "deeper")
        inside = message.embeds[0].description
        await game.click(message, player, "claim")
        assert len(record_of(game, player)["eggs"]) == 5
        return player, before, inside, message.embeds[0].description

    await make_pet(db, 1, level=2, name="Iskra")
    await make_pet(db, 2, level=pets.MAX_LEVEL, name="Stary")
    await make_pet(db, 3, level=0)
    await make_pet(db, 4, level=1, name="Stajenny")

    ala, before, inside, after = await claim("Ala", pet_id=1)
    assert "Iskra · poziom 2 → 3" in before and "Iskra · poziom 2 → 3" in inside
    assert "✅ awans: Iskra · poziom 2 → 3" in after
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 1")) == 3

    _, before, inside, after = await claim("Ola", pet_id=2)                     # at its last level
    assert dungeon.MSG_LEVEL_MAX.format(name="Stary") in before and dungeon.MSG_LEVEL_MAX.format(name="Stary") in inside
    assert dungeon.MSG_GOT_NO_LEVEL_MAX in after and "✅ awans" not in after
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 2")) == pets.MAX_LEVEL

    ela, before, inside, after = await claim("Ela", pet_id=3)                   # an egg at her side hatches
    assert "wykluje się · poziom 0 → 1" in before and "poziom 0 → 1" in after
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 3")) == 1

    ula, before, inside, after = await claim("Ula", pet_id=0, pet_id_alt1=4)    # nobody at her side
    assert dungeon.MSG_LEVEL_NONE in before and dungeon.MSG_LEVEL_NONE in inside
    assert dungeon.MSG_GOT_NO_LEVEL_NONE in after
    assert int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 4")) == 1   # a stabled pet gets nothing
    for player in (ala, ela, ula):
        assert xp_of(game, player) == 1000 and await scrolls_of(db, player.id) == 16


async def test_a_reward_that_fails_is_reported_and_never_paid_twice(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=1)
    mine, message = await go_down(game, ala, 3)

    async def database_down(scene, number, player_id):
        raise OSError("connection lost")

    working = pets.assign_scroll
    monkeypatch.setattr(pets, "assign_scroll", database_down)
    await game.click(message, ala, "leave")
    record = record_of(game, ala)
    assert record["state"] == "done" and record["granted"] == {
        "xp": 70, "scrolls": 0, "level": None, "failed": ["1 zwój odrodzenia"]}
    assert xp_of(game, ala) == 70 and await scrolls_of(db, ala.id) == 1
    text = message.embeds[0].description
    assert "✅ 70 XP" in text and "Nie udało się przyznać: 1 zwój odrodzenia" in text and message.view is None
    log_ = game.bot.owner_log
    assert len(log_) == 1 and f"<@{ala.id}>" in log_[0] and "1 zwój odrodzenia" in log_[0] and "komory 3" in log_[0]
    assert "connection lost" not in log_[0] and "OSError" in log_[0]
    # No second chance to be paid, although the database works again: not by the button the player may
    # still see, not by the watch, not by two restarts of the bot.
    monkeypatch.setattr(pets, "assign_scroll", working)
    assert (await dispatch(game, message, ala, "leave")).answers == [ui.MSG_STALE]
    await dungeon.tick(game.bot)
    for _ in range(2):
        await restart(game)
        await dungeon.tick(game.bot)
    assert xp_of(game, ala) == 70 and await scrolls_of(db, ala.id) == 1 and len(game.bot.owner_log) == 1
    assert record_of(game, ala)["granted"]["failed"] == ["1 zwój odrodzenia"] and mine.settled


async def test_experience_that_cannot_be_written_is_reported(game_db, luck, monkeypatch):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    message = await enter(game, mine, ala)

    monkeypatch.setattr(game.bot.store.exp_summary, "save", disk_full)
    assert expsum.add_file_exp(game.bot, ala.id, 5) is False
    await game.click(message, ala, "leave")
    assert record_of(game, ala)["granted"]["failed"] == ["20 XP"]
    assert "Nie udało się przyznać: 20 XP" in message.embeds[0].description and len(game.bot.owner_log) == 1


# -- the eggs: the game's normal eggs, shown, placed one after the other --------------------------------

async def test_a_supporters_egg_can_be_premium_and_nobody_elses_can(game_db, luck, monkeypatch):
    """The eggs of the mine are rolled exactly as ``pets.assign_pet`` rolls one for that player."""
    game = game_db
    supporter, plain = game.add_member("Ala", roles=[PATRON]), game.add_member("Ola")
    pools, _ranges = rig(monkeypatch, choices=[pets.PetType.GHOST] * 2,
                         randint=lambda low, high: 0 if high == 1000 else high)
    await leave(game, supporter, 5)
    assert pools == [list(pets.PATRON_EGGS)] * 2                              # two eggs, the supporters' pool
    eggs = [entry["egg"] for entry in record_of(game, supporter)["eggs"]]
    assert [(egg["TYPE"], egg["QUALITY"], egg["VARIANT"]) for egg in eggs] == [("Ghost", "Premium", 1)] * 2
    assert all("SEAL" in egg for egg in eggs)                                 # ... with the usual cosmetic roll
    pools.clear()
    await leave(game, plain, 5)
    assert pools == [list(pets.STANDARD_EGGS)] * 2                             # never offered a premium species
    eggs = [entry["egg"] for entry in record_of(game, plain)["eggs"]]
    assert {egg["QUALITY"] for egg in eggs} == {"Standard"} and all(egg["SEAL"] == 0 for egg in eggs)
    assert json.loads(json.dumps(eggs)) == eggs                               # an egg can wait in the file


async def test_a_mythic_egg_is_announced_when_it_is_placed(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    rig(monkeypatch, randint=lambda low, high: 1000 if high == 1000 else 0)    # the roll of a mythic egg
    mine, message = await leave(game, ala, 4)
    assert record_of(game, ala)["eggs"][0]["egg"]["TYPE"] == "Void"
    assert "*Mityczny towarzysz.*" in message.embeds[0].description
    assert not game.channel.contains("zdobył mitycznego towarzysza")            # not hers yet
    await game.click(message, ala, "egg-take")
    assert game.channel.contains(f"Łowca <@{ala.id}> zdobył mitycznego towarzysza!")
    assert await db.fetchval("SELECT MYTHIC FROM PETS WHERE PET_ID = $1", (await row(db, ala.id))["pet_id"]) is True


def test_the_choices_offered_for_every_state_of_the_places():
    def labels(**state) -> list[str]:
        return [label for _key, label, _style, _emoji in dungeon.egg_buttons(places(**state))]

    pet = (1, 5, None)
    take, pouch, stable = dungeon.EGG_ACTIVE_LABEL, dungeon.EGG_POUCH_LABEL, dungeon.EGG_STABLE_LABEL
    swap, sacrifice = dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL
    full = dict(occupied=[pet] * 4)
    for active in (False, True):
        for has_pouch in (False, True):
            for stable_free in (False, True):
                state = dict(active=active, pouch=has_pouch, **(dict(free=[2]) if stable_free else full))
                expected = ([take] if active else [pouch] if has_pouch else []) + [stable if stable_free else swap]
                assert labels(**state) == [*expected, sacrifice], state
    assert labels(active=True, free=[1, 2, 3, 4]) == [take, stable, sacrifice]  # a player with nothing at all
    assert [key for key, *_ in dungeon.egg_buttons(None)] == ["egg-refresh"]    # the stable could not be read


async def test_the_places_are_read_from_the_players_row(game_db):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    assert await pets.egg_places(bot, 77) == pets.EggPlaces(True, False, 4, (1, 2, 3, 4), ())
    await make_owner(db, 5, pet_id=1, egg_pouch=True, stable_places=6, pet_id_alt2=2, pet_id_alt6=3)
    await make_pet(db, 1, level=2, name="Iskra")
    await make_pet(db, 2, level=1, name="Bury")
    found = await pets.egg_places(bot, 5)
    assert (found.active, found.pouch, found.places, found.free, found.room) == (False, True, 6, (1, 3, 4, 5), "pouch")
    assert [(place, pet_id, None if pet is None else pet["pet_name"]) for place, pet_id, pet in found.occupied] == [
        (2, 2, "Bury"), (6, 3, None)]                                          # (pet 3 has no row in PETS)
    assert found.active_pet["pet_name"] == "Iskra" and await pets.egg_room(bot, 5) == found.room
    await db.execute("UPDATE PETOWNER SET POUCH_PET_ID = 9 WHERE PLAYER_ID = 5")
    assert (await pets.egg_places(bot, 5)).room == ""


@pytest.mark.parametrize("supporter", [False, True])
async def test_choice_a_the_egg_is_taken_by_the_games_normal_rule(game_db, luck, no_modifier, supporter):
    game, db = game_db, game_db.bot.db
    roles = [PATRON] if supporter else []
    ala = game.add_member("Ala", roles=roles)                                  # nobody at her side
    mine, message = await leave(game, ala, 4)
    assert message.button_labels == [dungeon.EGG_ACTIVE_LABEL, dungeon.EGG_STABLE_LABEL, dungeon.EGG_SACRIFICE_LABEL]
    assert message.files[0].endswith((".png", ".gif"))                         # the egg, as the game shows one
    rolled = record_of(game, ala)["eggs"][0]["egg"]
    await game.click(message, ala, "egg-take")
    owner = await row(db, ala.id)
    assert owner["pet_id"] > 0 and fates(game, ala) == [pets.EGG_ACTIVE] and record_of(game, ala)["state"] == "done"
    egg = await db.fetchrow("SELECT * FROM PETS WHERE PET_ID = $1", owner["pet_id"])
    assert (egg["type"], egg["quality"], egg["variant"]) == (rolled["TYPE"], rolled["QUALITY"], str(rolled["VARIANT"]))
    assert int(egg["pet_lvl"]) == 0 and await pets_count(db) == 1               # exactly the egg that was rolled
    assert "1. jajo jest teraz Twoim towarzyszem" in message.embeds[0].description and message.view is None
    assert no_modifier == [] and await album_of(db, ala) == [] and mine.closed

    # With a pet at her side the same button is the pouch - when she has one and it is empty.
    ola = game.add_member("Ola", roles=roles)
    await make_owner(db, ola.id, pet_id=50, egg_pouch=True, pet_id_alt1=51, pet_id_alt2=52, pet_id_alt3=53,
                     pet_id_alt4=54)
    for pet_id in range(50, 55):
        await make_pet(db, pet_id, level=1)
    _mine, other = await leave(game, ola, 5)
    assert other.button_labels == [dungeon.EGG_POUCH_LABEL, dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]
    await game.click(other, ola, "egg-take")
    owner = await row(db, ola.id)
    assert owner["pouch_pet_id"] > 54 and owner["pet_id"] == 50 and fates(game, ola) == [pets.EGG_IN_POUCH, ""]
    # The pouch holds one egg: the second egg is offered neither it nor a free stable place.
    assert "Jajo 2 z 2" in other.embeds[0].description and "1. jajo trafiło do sakwy" in other.embeds[0].description
    assert other.button_labels == [dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]


@pytest.mark.parametrize("supporter", [False, True])
async def test_choice_b_eggs_go_into_free_stable_places_one_after_the_other(game_db, luck, no_modifier, supporter):
    """Several eggs are decided one by one; the choices are computed anew for each of them."""
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON] if supporter else [])
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt4=4)     # place 3 is free
    for pet_id in (1, 2, 3, 4):
        await make_pet(db, pet_id, level=1, name=f"Pet{pet_id}")
    _mine, message = await leave(game, ala, 5)                                  # two eggs
    assert "Jajo 1 z 2" in message.embeds[0].description
    assert message.button_labels == [dungeon.EGG_STABLE_LABEL, dungeon.EGG_SACRIFICE_LABEL]
    await game.click(message, ala, "egg-stable")
    owner = await row(db, ala.id)
    first = owner["pet_id_alt3"]
    assert first > 4 and owner["pet_id"] == 1 and fates(game, ala) == [pets.EGG_IN_STABLE, ""]
    assert record_of(game, ala)["eggs"][0]["place"] == 3 and record_of(game, ala)["state"] == "eggs"
    # The first egg took the last free place: the second is offered the swap instead.
    text = message.embeds[0].description
    assert "Jajo 2 z 2" in text and "1. jajo trafiło do stajni (miejsce 3)" in text
    assert message.button_labels == [dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]
    assert await pets_count(db) == 5 and no_modifier == []


@pytest.mark.parametrize("supporter", [False, True])
async def test_choice_c_the_swap_sacrifices_the_stabled_pet_once_and_the_egg_takes_its_place(
        game_db, luck, no_modifier, supporter):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON] if supporter else [])
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in range(1, 6):
        await make_pet(db, pet_id, level=2, name=f"Pet{pet_id}")
    mine, message = await leave(game, ala, 4)
    assert message.button_labels == [dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]
    questions: list[str] = []
    game.channel.send_hooks.append(lambda sent: questions.append(sent.content) if asks(sent) else None)
    picked = game.auto_click(ala, "pick", values=["3:4"])
    confirmed = game.auto_click(ala, "yes", times=2)
    await game.click(message, ala, "egg-swap")
    assert len(picked) == 1 and len(confirmed) == 2
    menu, first, second = questions
    assert "Wybrany towarzysz zostanie poświęcony" in menu
    # Both questions name the pet and say that it cannot be undone.
    for question, number in ((first, "1 z 2"), (second, "2 z 2")):
        assert "**Pet4** · Kot · poziom 2" in question and "poświęcony" in question
        assert "nie da się" in question and f"pytanie {number}" in question
        assert ("zachowasz go w albumie" in question) is supporter
    owner = await row(db, ala.id)
    egg_id = owner["pet_id_alt3"]
    assert egg_id > 5 and int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = $1", egg_id)) == 0
    assert await db.fetchval("SELECT COUNT(*) FROM PETS WHERE PET_ID = 4") == 0   # really sacrificed ...
    assert await pets_count(db) == 5 and no_modifier == [ala.id]                # ... once, with the game's hook
    kept = await album_of(db, ala)
    assert [(int(entry["pet_id"]), entry["pet_name"], int(entry["pet_lvl"])) for entry in kept] == (
        [(4, "Pet4", 2)] if supporter else [])
    assert (owner["pet_id"], owner["pet_id_alt1"], owner["pet_id_alt2"], owner["pet_id_alt4"]) == (1, 2, 3, 5)
    assert fates(game, ala) == [pets.EGG_SWAPPED] and record_of(game, ala)["state"] == "done"
    text = message.embeds[0].description
    assert "1. jajo zajęło miejsce 3 w stajni - **Pet4** · Kot · poziom 2 został poświęcony" in text
    assert message.view is None and mine.closed
    assert not any(asks(m) and not m.view.is_finished() for m in game.channel.messages)  # no question left


@pytest.mark.parametrize("answers", [["no"], ["yes", "no"], ["yes"], ["cancel"], []])
async def test_anything_but_two_clear_yes_changes_nothing(game_db, luck, no_modifier, monkeypatch, answers):
    """First "no", second "no", a time-out after the first "yes", a cancelled or ignored menu."""
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in range(1, 6):
        await make_pet(db, pet_id, level=2, name=f"Pet{pet_id}")
    mine, message = await leave(game, ala, 4)
    before = await row(db, ala.id)
    questions: list[str] = []
    game.channel.send_hooks.append(lambda sent: questions.append(sent.content) if asks(sent) else None)
    if answers in (["yes"], []):
        # Only these two cases wait for a time-out; the others are answered at the usual 15 / 20 s, so a
        # stalled machine cannot turn a clear "no" into a time-out without this test noticing.
        monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.02)                      # 15 s -> 0.3 s
    clicks: list[list] = []
    if answers == ["cancel"]:
        clicks.append(game.auto_click(ala, "pick-cancel"))
    elif answers:
        clicks.append(game.auto_click(ala, "pick", values=["2:3"]))
        for index, answer in enumerate(answers):
            clicks.append(game.auto_click(
                ala, answer, when=lambda sent, wanted=f"pytanie {index + 1} z 2": wanted in sent.content))
    await game.click(message, ala, "egg-swap")
    # Every answer of the case WAS given, and exactly the questions it leads to were asked.
    assert [len(made) for made in clicks] == [1] * len(clicks)
    for made in clicks:
        assert (await made[0]).response.is_done()
    assert [number for question in questions for number in ("1 z 2", "2 z 2") if f"pytanie {number}" in question] == {
        "no": ["1 z 2"], "yes no": ["1 z 2", "2 z 2"], "yes": ["1 z 2", "2 z 2"], "cancel": [], "": []}[" ".join(answers)]
    assert len(questions) == {"no": 2, "yes no": 3, "yes": 3, "cancel": 1, "": 1}[" ".join(answers)]
    assert await row(db, ala.id) == before and await pets_count(db) == 5 and await album_of(db, ala) == []
    assert no_modifier == [] and fates(game, ala) == [""] and record_of(game, ala)["state"] == "eggs"
    # Back to the choices, with the egg still hers to decide.
    assert dungeon.MSG_NOTHING_CHANGED in message.embeds[0].description
    assert message.button_labels == [dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]
    assert ala.id not in pets._awaiting_confirmation and not mine.expeditions[ala.id].asking
    assert dungeon.is_open(game.bot)


async def test_a_pet_moved_between_the_two_answers_means_choose_again(game_db, luck, no_modifier):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in range(1, 6):
        await make_pet(db, pet_id, level=2, name=f"Pet{pet_id}")
    _mine, message = await leave(game, ala, 4)
    game.auto_click(ala, "pick", values=["2:3"])
    game.auto_click(ala, "yes", when=lambda sent: "pytanie 1 z 2" in sent.content)

    def moved(sent) -> None:
        # Another command takes pet 3 out of place 2 (pet 1 goes there) while the second question waits.
        if "pytanie 2 z 2" in sent.content:
            async def swap_then_confirm():
                await db.execute("UPDATE PETOWNER SET PET_ID = 3, PET_ID_ALT2 = 1 WHERE PLAYER_ID = $1", ala.id)
                await game.click(sent, ala, "yes")
            game._tasks.append(asyncio.ensure_future(swap_then_confirm()))

    game.channel.send_hooks.append(moved)
    await game.click(message, ala, "egg-swap")
    owner = await row(db, ala.id)
    assert (owner["pet_id"], owner["pet_id_alt2"]) == (3, 1)                   # nobody was sacrificed ...
    assert await pets_count(db) == 5 and await album_of(db, ala) == [] and no_modifier == []
    assert fates(game, ala) == [""]                                            # ... and the egg is not lost
    assert pets.MSG_STABLE_CHANGED in message.embeds[0].description
    assert message.button_labels == [dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]


@pytest.mark.parametrize("supporter", [False, True])
async def test_choice_d_the_egg_itself_is_sacrificed(game_db, luck, supporter):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON] if supporter else [])
    await make_owner(db, ala.id, pet_id=1)
    await make_pet(db, 1, level=1)
    _mine, message = await leave(game, ala, 5)
    rolled = record_of(game, ala)["eggs"][0]["egg"]
    questions: list[str] = []
    game.channel.send_hooks.append(lambda sent: questions.append(sent.content) if asks(sent) else None)
    game.auto_click(ala, "no")
    await game.click(message, ala, "egg-sacrifice")                             # "no": nothing happens
    assert fates(game, ala) == ["", ""] and dungeon.MSG_NOTHING_CHANGED in message.embeds[0].description
    assert await album_of(db, ala) == [] and not game.channel.contains("Poświęcono towarzysza!")
    game.auto_click(ala, "yes")
    await game.click(message, ala, "egg-sacrifice")
    assert len(questions) == 2 and "poświęcić to jajo" in questions[1] and "nie da się cofnąć" in questions[1]
    assert ("zachowasz je w albumie" in questions[1]) is supporter
    assert fates(game, ala) == [pets.EGG_SACRIFICED, ""] and await pets_count(db) == 1   # never a row in PETS
    kept = await album_of(db, ala)
    assert len(kept) == (1 if supporter else 0)
    if supporter:                                                              # an egg entry of her album
        assert (int(kept[0]["pet_lvl"]), kept[0]["type"], kept[0]["quality"]) == (0, rolled["TYPE"], rolled["QUALITY"])
        assert game.channel.contains("Twój towarzysz trafił do albumu")
    # The game's sacrifice hook ran: a boss modifier was drawn and announced.
    assert game.channel.count("Poświęcono towarzysza!") == 1
    text = message.embeds[0].description
    assert "1. jajo zostało poświęcone" in text and "Jajo 2 z 2" in text
    assert (await row(db, ala.id))["pet_id"] == 1


async def test_a_sacrifice_the_album_cannot_keep_changes_nothing(game_db, luck, no_modifier):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in range(1, 6):
        await make_pet(db, pet_id, level=2, name=f"Pet{pet_id}")
    await db.execute("ALTER TABLE PET_ALBUM ADD CONSTRAINT TEST_REFUSES CHECK (false) NOT VALID", idempotent=True)
    _mine, message = await leave(game, ala, 4)
    before = await row(db, ala.id)
    game.auto_click(ala, "pick", values=["1:2"])
    game.auto_click(ala, "yes", times=2)
    await game.click(message, ala, "egg-swap")
    assert await row(db, ala.id) == before and await pets_count(db) == 5 and no_modifier == []
    assert fates(game, ala) == [""] and dungeon.MSG_ALBUM_FAILED in message.embeds[0].description
    assert len(game.bot.owner_log) == 1 and "Album" in game.bot.owner_log[0]
    game.auto_click(ala, "yes")
    await game.click(message, ala, "egg-sacrifice")                             # her own egg cannot be kept either
    assert fates(game, ala) == [""] and await pets_count(db) == 5


async def test_an_egg_whose_transaction_breaks_is_never_tried_twice(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    _mine, message = await leave(game, ala, 5)
    real = pets.place_egg
    calls: list[str] = []

    async def broken(scene, player, egg, where, **kwargs):
        calls.append(where)
        if len(calls) == 1:
            raise OSError("could not be confirmed")
        return await real(scene, player, egg, where, **kwargs)

    monkeypatch.setattr(pets, "place_egg", broken)
    await game.click(message, ala, "egg-take")
    assert fates(game, ala) == [dungeon.EGG_UNKNOWN, ""] and await pets_count(db) == 0
    text = message.embeds[0].description
    assert dungeon.MSG_EGG_FAILED in text and "Jajo 2 z 2" in text
    assert len(game.bot.owner_log) == 1 and f"<@{ala.id}>" in game.bot.owner_log[0]
    await game.click(message, ala, "egg-take")                                  # the NEXT egg, not the same again
    assert fates(game, ala) == [dungeon.EGG_UNKNOWN, pets.EGG_ACTIVE] and calls == [pets.EGG_TO_ROOM] * 2
    assert await pets_count(db) == 1 and record_of(game, ala)["state"] == "done"


async def test_concurrent_egg_clicks_place_one_egg(game_db, luck):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    _mine, message = await leave(game, ala, 6)                                  # three eggs
    await asyncio.gather(*(game.click(message, ala, "egg-stable") for _ in range(3)))
    assert fates(game, ala) == [pets.EGG_IN_STABLE, "", ""] and await pets_count(db) == 1
    await game.click(message, ala, "egg-take")
    await game.click(message, ala, "egg-stable")
    owner = await row(db, ala.id)
    assert fates(game, ala) == [pets.EGG_IN_STABLE, pets.EGG_ACTIVE, pets.EGG_IN_STABLE]
    assert owner["pet_id_alt1"] > 0 and owner["pet_id"] > 0 and owner["pet_id_alt2"] > 0
    assert len({owner["pet_id"], owner["pet_id_alt1"], owner["pet_id_alt2"]}) == 3 and await pets_count(db) == 3


async def test_place_egg_checks_the_place_again_under_the_lock(game_db):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1, egg_pouch=True, pouch_pet_id=9, pet_id_alt1=2, pet_id_alt2=3,
                     pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in (1, 2, 3, 4, 5, 9):
        await make_pet(db, pet_id, level=1)
    egg = pets.roll_egg(ala)
    scene = game.scene(ala)
    before = await row(db, ala.id)
    # Nothing is free: every choice that needs room is "choose again"; nothing is stored, nothing lost.
    assert (await pets.place_egg(scene, ala, egg, pets.EGG_TO_ROOM)).status == pets.EGG_CHANGED
    assert (await pets.place_egg(scene, ala, egg, pets.EGG_TO_STABLE)).status == pets.EGG_CHANGED
    assert (await pets.place_egg(scene, ala, egg, pets.EGG_ANYWHERE, announce=False)).status == pets.EGG_NO_ROOM
    for place, expected in ((2, 99), (9, 3), ("x", 3), (2, 0)):                 # not that pet / not a place of hers
        assert (await pets.place_egg(scene, ala, egg, pets.EGG_SWAP, place=place,
                                     expected_pet=expected)).status == pets.EGG_CHANGED
    assert await row(db, ala.id) == before and await pets_count(db) == 6
    # The closing rule: at her side, else the pouch, else a free stable place - in that order.
    await db.execute("UPDATE PETOWNER SET PET_ID = 0, POUCH_PET_ID = 0, PET_ID_ALT3 = 0 WHERE PLAYER_ID = $1", ala.id)
    order = [await pets.place_egg(scene, ala.id, pets.roll_egg(ala), pets.EGG_ANYWHERE, announce=False)
             for _ in range(4)]
    assert [(placed.status, placed.place) for placed in order] == [
        (pets.EGG_ACTIVE, 0), (pets.EGG_IN_POUCH, 0), (pets.EGG_IN_STABLE, 3), (pets.EGG_NO_ROOM, 0)]
    now = await row(db, ala.id)
    assert (now["pet_id"], now["pouch_pet_id"], now["pet_id_alt3"]) == tuple(placed.pet_id for placed in order[:3])
    # A player without any row gets one.
    ola = game.add_member("Ola")
    placed = await pets.place_egg(game.scene(ola), ola, pets.roll_egg(ola), pets.EGG_TO_STABLE)
    assert placed.status == pets.EGG_IN_STABLE and (await row(db, ola.id))["pet_id_alt1"] == placed.pet_id
    assert (await row(db, ola.id))["pet_id"] == 0 and await bot.db.fetchval(
        "SELECT COUNT(*) FROM PETOWNER WHERE PLAYER_ID = $1", ola.id) == 1


async def test_the_stable_helper_uses_the_first_free_place(game_db):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    assert await pets.store_in_free_place(bot, 404) == ("alone", 0, None)
    await make_owner(db, 5, pet_id=0, pet_id_alt1=2)
    assert (await pets.store_in_free_place(bot, 5))[0] == "no_pet"
    await db.execute("UPDATE PETOWNER SET PET_ID = 1, STABLE_PLACES = 5, PET_ID_ALT2 = 3 WHERE PLAYER_ID = 5")
    await make_pet(db, 1, level=1, name="Iskra")
    status, place, pet = await pets.store_in_free_place(bot, 5)
    assert (status, place, pet["pet_name"]) == (pets._OK, 3, "Iskra")
    assert ((await row(db, 5))["pet_id"], (await row(db, 5))["pet_id_alt3"]) == (0, 1)
    await db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT4 = 8, PET_ID_ALT5 = 9 WHERE PLAYER_ID = 5")
    assert (await pets.store_in_free_place(bot, 5))[0] == "full" and (await row(db, 5))["pet_id"] == 7


async def test_an_egg_is_shown_as_the_game_shows_one(game_db):
    game = game_db
    supporter, plain = game.add_member("Ala", roles=[PATRON]), game.add_member("Ola")
    egg = {"TYPE": "Cat", "QUALITY": "Standard", "VARIANT": 3, "SHINY": True, "ULTRA_SHINY": False,
           "MYTHIC": False, "SEAL": 2, "HOLO": False, "FRAME": "", "HOLO_PARTIAL": False}
    lines, image = await pets.egg_shown(game.bot, game.guild, supporter, egg)
    assert lines[0] == "*Rzadki towarzysz. Miałeś niesamowite szczęście.*" and any("Pieczęć" in line for line in lines)
    assert image is not None and image.is_file()
    assert not any("Kot" in line for line in lines)                            # never the species
    # The ornaments only for somebody who supports: everybody else is told that they are hidden.
    lines, image = await pets.egg_shown(game.bot, game.guild, plain, egg)
    assert lines == ["*Rzadki towarzysz. Miałeś niesamowite szczęście.*", pets.MSG_COSMETICS_HIDDEN]
    lines, image = await pets.egg_shown(game.bot, game.guild, plain, {**egg, "SHINY": False, "SEAL": 0})
    assert lines == [] and image.name == "3.png"                               # an ordinary egg: its plain picture


# -- the closing: only who took his loot has it ---------------------------------------------------------

@pytest.mark.parametrize("how", ["event", "boss", "unannounced", "restart"])
async def test_an_expedition_in_any_chamber_gets_nothing_at_the_closing(game_db, luck, how):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=5, pet_id=1)
    await make_pet(db, 1, level=1)
    for chamber in range(1, 9):                                                  # chamber 8: the unclaimed treasury
        mine, message = await go_down(game, ala, chamber)
        draws = len(luck.calls)
        await close_by(game, how)
        assert record_of(game, ala)["state"] == "closed" and record_of(game, ala)["eggs"] == []
        assert game.bot.store.dungeon.load()["mine"]["closed"] is True
        # Nothing is paid and nothing is rolled: no XP, no scrolls, no level, no egg.
        assert xp_of(game, ala) == 0 and await scrolls_of(db, ala.id) == 5 and len(luck.calls) == draws
        assert await pets_count(db) == 1 and int(await db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = 1")) == 1
        # ONE summary names who did not come out in time.
        assert len(summaries(game)) == chamber
        text = summaries(game)[-1].embeds[0].description
        assert text == f"{ala.mention} nie zdążył wyjść przed zamknięciem kopalni - **cały łup z tej wyprawy przepadł**."
        # Closing again changes nothing and says nothing more.
        await dungeon.close(game.bot)
        await dungeon.tick(game.bot)
        if how != "restart":
            await restart(game)
            await dungeon.tick(game.bot)
        assert len(summaries(game)) == chamber and xp_of(game, ala) == 0
    assert game.bot.owner_log == []


@pytest.mark.parametrize("how", ["event", "boss", "restart"])
async def test_undecided_eggs_go_where_there_is_room_or_are_lost(game_db, luck, how):
    game, db = game_db, game_db.bot.db
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola", roles=[PATRON]), game.add_member("Ela")
    await make_owner(db, ala.id, pet_id=0, egg_pouch=True, pet_id_alt1=1, pet_id_alt2=2, pet_id_alt3=3)
    await make_owner(db, ola.id, pet_id=4, pet_id_alt1=5, pet_id_alt2=6, pet_id_alt3=7, pet_id_alt4=8)
    for pet_id in range(1, 9):
        await make_pet(db, pet_id, level=1)

    async def closed_with(player, chamber: int, decide: int = 0) -> str:
        """The player leaves ``chamber``, decides ``decide`` eggs herself, then the mine is closed."""
        _mine, message = await leave(game, player, chamber)
        for _ in range(decide):
            await game.click(message, player, "egg-stable")
        paid = (xp_of(game, player), await scrolls_of(db, player.id))
        posted = len(summaries(game))
        await close_by(game, how)
        assert (xp_of(game, player), await scrolls_of(db, player.id)) == paid      # what she took stays, once
        assert record_of(game, player)["state"] == "done" and len(summaries(game)) == posted + 1
        count = await pets_count(db)
        await dungeon.close(game.bot)                                                # each exactly once
        await restart(game)
        await dungeon.tick(game.bot)
        assert await pets_count(db) == count and len(summaries(game)) == posted + 1
        return summaries(game)[-1].embeds[0].description

    before = await pets_count(db)
    text = await closed_with(ala, 7)                                             # four eggs, room for three
    owner = await row(db, ala.id)
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_POUCH, pets.EGG_IN_STABLE, dungeon.EGG_LOST]
    assert owner["pet_id"] > 8 and owner["pouch_pet_id"] > 8 and owner["pet_id_alt4"] > 8
    assert (owner["pet_id_alt1"], owner["pet_id_alt2"], owner["pet_id_alt3"]) == (1, 2, 3)
    assert await pets_count(db) == before + 3 and "przepadł**" not in text       # she had left by herself
    assert (f"• {ala.mention}: jajo 1 z 4 jest teraz Twoim towarzyszem; jajo 2 z 4 trafiło do sakwy; "
            "jajo 3 z 4 trafiło do stajni (miejsce 4); jajo 4 z 4 przepadło") in text

    text = await closed_with(ola, 5)                                             # two eggs, no room at all
    # Nothing is ever sacrificed for an undecided egg - not even a supporter's.
    assert fates(game, ola) == [dungeon.EGG_LOST, dungeon.EGG_LOST] and await album_of(db, ola) == []
    assert (await row(db, ola.id))["pet_id"] == 4 and await db.fetchval("SELECT COUNT(*) FROM PETS WHERE PET_ID < 9") == 8
    assert await pets_count(db) == before + 3 and f"• {ola.mention}: jajo 1 z 2 przepadło" in text

    text = await closed_with(ela, 6, decide=1)                                   # three eggs, one decided herself
    ela_row = await row(db, ela.id)
    assert fates(game, ela) == [pets.EGG_IN_STABLE, pets.EGG_ACTIVE, pets.EGG_IN_STABLE]
    assert ela_row["pet_id_alt1"] > 8 and ela_row["pet_id"] > 8 and ela_row["pet_id_alt2"] > 8
    assert await pets_count(db) == before + 6
    # A closing between two eggs: the decided egg stays where it is, only the rest is placed.
    assert f"• {ela.mention}: jajo 2 z 3 jest teraz Twoim towarzyszem; jajo 3 z 3 trafiło do stajni (miejsce 2)" in text
    assert "jajo 1 z 3" not in text and game.bot.owner_log == []


async def test_a_half_answered_double_confirmation_counts_as_no_decision(game_db, luck, no_modifier):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in range(1, 6):
        await make_pet(db, pet_id, level=2, name=f"Pet{pet_id}")
    _mine, message = await leave(game, ala, 4)
    game.auto_click(ala, "pick", values=["1:2"])
    game.auto_click(ala, "yes", when=lambda sent: "pytanie 1 z 2" in sent.content)
    second: list = []
    game.channel.send_hooks.append(lambda sent: second.append(sent) if "pytanie 2 z 2" in sent.content else None)
    swap = asyncio.ensure_future(game.click(message, ala, "egg-swap"))
    await until(lambda: second)                                                 # one "yes" given, one open
    game.bot.state.close_event()                                                # the next event / the boss
    await dungeon.close(game.bot)
    assert fates(game, ala) == [dungeon.EGG_LOST] and await pets_count(db) == 5  # no room: lost, nobody sacrificed
    await game.click(second[0], ala, "yes")                                      # the late second "yes"
    await asyncio.wait_for(swap, 5)
    assert fates(game, ala) == [dungeon.EGG_LOST] and await pets_count(db) == 5
    assert await album_of(db, ala) == [] and no_modifier == []
    assert (await row(db, ala.id))["pet_id_alt1"] == 2


@pytest.mark.parametrize("meanwhile", ["hunt", "closed"])
async def test_the_second_question_is_not_asked_into_a_hunt_or_a_closed_mine(game_db, luck, no_modifier, meanwhile):
    """Between the first "yes" and the second question a hunt began / the mine was closed: nothing is posted."""
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in range(1, 6):
        await make_pet(db, pet_id, level=2, name=f"Pet{pet_id}")
    mine, message = await leave(game, ala, 4)
    state = game.bot.state
    questions: list[str] = []
    held: list = []

    def asked(sent) -> None:
        if sent.view is None:
            return
        questions.append(sent.content)
        if "pytanie 1 z 2" in sent.content:
            async def then_confirm():
                if meanwhile == "hunt":
                    held.append(state.hold_busy("hunt"))
                    held[0].__enter__()
                else:
                    state.close_event()
                    await dungeon.close(game.bot)
                await game.click(sent, ala, "yes")
            game._tasks.append(asyncio.ensure_future(then_confirm()))

    game.channel.send_hooks.append(asked)
    game.auto_click(ala, "pick", values=["2:3"])
    try:
        await game.click(message, ala, "egg-swap")
    finally:
        for hold in held:
            hold.__exit__(None, None, None)
    assert len(questions) == 2 and not any("pytanie 2 z 2" in question for question in questions)
    assert await pets_count(db) == 5 and no_modifier == [] and (await row(db, ala.id))["pet_id_alt2"] == 3
    assert not mine.expeditions[ala.id].asking and ala.id not in pets._awaiting_confirmation
    if meanwhile == "hunt":                                                       # still hers to decide
        assert fates(game, ala) == [""] and dungeon.MSG_NOTHING_CHANGED in message.embeds[0].description
        assert message.button_labels == [dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]
        # The sacrifice of the egg itself is not asked about into a hunt either.
        with state.hold_busy("hunt"):
            assert await dungeon._ask_sacrifice(mine, ala) is None
        assert len(questions) == 2
    else:                                                                         # no room anywhere: lost
        assert fates(game, ala) == [dungeon.EGG_LOST] and record_of(game, ala)["state"] == "done"


async def test_a_click_on_a_mine_somebody_closed_closes_it_and_pays_nothing(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 4)
    game.bot.state.close_event()                                                # the boss took the channel
    late = await game.click(message, ala, "leave")
    assert late.answers == [dungeon.MSG_CLOSED] and mine.closed
    assert xp_of(game, ala) == 0 and record_of(game, ala)["state"] == "closed"
    entering = await late_enter(game, mine, game.add_member("Ola"))
    assert entering.answers == [dungeon.MSG_CLOSED] and len(mine.expeditions) == 1
    await dungeon.tick(game.bot)                                                # the channel was not cleared:
    assert message.view is None and message.embeds[0].title == dungeon.CLOSED_TITLE   # the pages say what happened
    assert "komorze 4/8 (Zatopiona pompownia)" in message.embeds[0].description
    assert mine.announcement.view is None and len(summaries(game)) == 1
    assert dungeon.MSG_ANNOUNCE_CLOSED in mine.announcement.embeds[0].description


async def test_the_summary_waits_for_a_quiet_channel(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    await enter(game, mine, ala)
    state = game.bot.state
    state.close_event()
    with state.hold_busy("event:INVASION"):                                      # the next event plays
        await dungeon.close(game.bot)
        await dungeon.tick(game.bot)
        assert summaries(game) == [] and record_of(game, ala)["state"] == "closed"
        assert game.bot.store.dungeon.load()["mine"]["summary"]                  # kept for a restart
    await dungeon.tick(game.bot)
    assert len(summaries(game)) == 1 and game.bot.store.dungeon.load()["mine"]["summary"] == ""
    await dungeon.tick(game.bot)
    assert len(summaries(game)) == 1


# -- persistence and restarts ------------------------------------------------------------------------------

async def test_every_step_is_in_the_file(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    saved = game.bot.store.dungeon.load()["mine"]
    assert saved == {"id": mine.id, "opened": mine.opened, "closed": False, "settled": False, "summary": "",
                     "expeditions": {}}
    message = await enter(game, mine, ala)
    assert record_of(game, ala) == {"state": "chamber", "chamber": 1, "eggs": [], "granted": None}
    await descend(game, message, ala, 5)
    assert record_of(game, ala)["chamber"] == 5
    await game.click(message, ala, "leave")
    record = record_of(game, ala)
    assert record["state"] == "eggs" and record["granted"]["xp"] == 180 and len(record["eggs"]) == 2
    assert all(entry["egg"]["TYPE"] for entry in record["eggs"]) and fates(game, ala) == ["", ""]
    await game.click(message, ala, "egg-take")
    assert fates(game, ala) == [pets.EGG_ACTIVE, ""] and record_of(game, ala)["state"] == "eggs"
    await game.click(message, ala, "egg-stable")
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE] and record_of(game, ala)["state"] == "done"
    saved = game.bot.store.dungeon.load()["mine"]                               # its one expedition is over:
    assert saved["closed"] is True and saved["settled"] is True                 # closed, and nothing left to settle
    mine, message = await go_down(game, ala, 2)
    luck.results = [4]
    await game.click(message, ala, "deeper")
    assert record_of(game, ala)["state"] == "lost" and game.bot.store.dungeon.load()["mine"]["id"] == mine.id


@pytest.mark.parametrize("state", ["chamber", "lost", "done", "eggs", "half"])
async def test_a_restart_at_any_state_pays_nothing_twice(game_db, luck, state):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=0)
    if state == "chamber":
        _mine, message = await go_down(game, ala, 6)                             # still inside
    elif state == "lost":
        _mine, message = await go_down(game, ala, 1)
        luck.results = [9]
        await game.click(message, ala, "deeper")                                 # failed
    elif state == "done":
        _mine, message = await leave(game, ala, 3)                               # left, nothing to decide
    else:
        _mine, message = await leave(game, ala, 5)                               # left, two eggs undecided
        if state == "half":
            await game.click(message, ala, "egg-take")                           # one of two decided
    paid = (xp_of(game, ala), await scrolls_of(db, ala.id))
    assert paid == {"chamber": (0, 0), "lost": (0, 0), "done": (70, 1), "eggs": (180, 2), "half": (180, 2)}[state]
    first_egg = (await row(db, ala.id))["pet_id"]
    eggs_before = await pets_count(db)

    for _ in range(2):                                                            # the bot is restarted - twice
        await restart(game)
        await dungeon.tick(game.bot)
        after = game.bot.store.dungeon.load()["mine"]
        assert after["closed"] is True and after["settled"] is True and after["summary"] == ""
        assert record_of(game, ala)["state"] == {"chamber": "closed", "lost": "lost"}.get(state, "done")
        assert (xp_of(game, ala), await scrolls_of(db, ala.id)) == paid
        if state == "eggs":
            assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE] and await pets_count(db) == 2
        elif state == "half":
            # The egg decided before the restart stays where it is; only the other one is placed.
            now = await row(db, ala.id)
            assert now["pet_id"] == first_egg and now["pet_id_alt1"] > 0 and await pets_count(db) == 2
            assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE]
        else:
            assert await pets_count(db) == eggs_before == 0
        assert len(summaries(game)) == (0 if state in ("lost", "done") else 1)
    if state == "chamber":
        assert ala.mention in summaries(game)[0].embeds[0].description
    assert game.bot.owner_log == []


async def test_a_crash_in_the_middle_of_a_payment_or_of_an_egg_is_not_repeated(game_db, luck):
    game, db = game_db, game_db.bot.db
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    # The file as a crash would leave it: Ala's payment had begun ...
    await leave(game, ala, 5)
    data = game.bot.store.dungeon.load()
    data["mine"]["expeditions"][str(ala.id)].update(state=dungeon.LEAVING, granted=None)
    game.bot.store.dungeon.save(data)
    await restart(game)
    await dungeon.tick(game.bot)
    assert xp_of(game, ala) == 180 and record_of(game, ala)["state"] == "done"      # never paid again
    assert len(game.bot.owner_log) == 1 and f"<@{ala.id}>" in game.bot.owner_log[0]
    assert "przerwał wypłatę" in game.bot.owner_log[0]
    assert "(komora 5: 180 XP + 2 zwoje odrodzenia)" in game.bot.owner_log[0]
    # Her eggs were not placed yet (that comes after the payment): they go by the closing rule - once.
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE] and await pets_count(db) == 2
    [summary] = summaries(game)
    assert f"• {ala.mention}: jajo 1 z 2 jest teraz Twoim towarzyszem; jajo 2 z 2 trafiło do stajni" in (
        summary.embeds[0].description)
    await restart(game)
    await dungeon.tick(game.bot)
    assert await pets_count(db) == 2 and len(summaries(game)) == 1 and len(game.bot.owner_log) == 1
    # ... and, another time, Ola's first egg was in the middle of its transaction.
    await leave(game, ola, 5)
    data = game.bot.store.dungeon.load()
    data["mine"]["expeditions"][str(ola.id)]["eggs"][0]["fate"] = dungeon.PENDING
    game.bot.store.dungeon.save(data)
    await restart(game)
    await dungeon.tick(game.bot)
    assert fates(game, ola) == [dungeon.EGG_UNKNOWN, pets.EGG_ACTIVE] and await pets_count(db) == 3
    log_ = game.bot.owner_log
    assert len(log_) == 2 and f"<@{ola.id}>" in log_[1] and "nie wiadomo, czy jajo zostało zapisane" in log_[1]
    assert len(summaries(game)) == 2
    assert "jajo 1 z 2 nie zostało potwierdzone" in summaries(game)[-1].embeds[0].description


def test_the_owner_is_told_which_row_an_interrupted_payment_was():
    assert dungeon._row_without_eggs(8) == "1000 XP + 16 zwojów odrodzenia + 1 poziom aktywnego peta"
    assert dungeon._row_without_eggs(1) == "20 XP"
    for unknown in (0, 9, "?", None):                                             # not a chamber of today's table
        assert dungeon._row_without_eggs(unknown) == "XP, zwoje i poziom z wiersza tej komory"


async def test_a_restart_without_a_mine_or_with_a_broken_file_is_harmless(game_db):
    game = game_db
    await restart(game)
    game.bot.store.dungeon.path.parent.mkdir(parents=True, exist_ok=True)
    game.bot.store.dungeon.path.write_text('{"mine": "zepsute"', encoding="utf-8")
    await restart(game)
    game.bot.store.dungeon.save({"mine": {"id": "x", "closed": False, "expeditions": {"abc": "?", "5": {}}}})
    await restart(game)
    await dungeon.tick(game.bot)
    assert summaries(game) == [] and game.bot.store.dungeon.load()["mine"]["closed"] is True


async def test_a_state_that_cannot_be_saved_pays_nothing(game_db, luck, monkeypatch):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    message = await enter(game, mine, ala)

    working = game.bot.store.dungeon.save
    monkeypatch.setattr(game.bot.store.dungeon, "save", disk_full)
    await game.click(message, ala, "leave")
    assert xp_of(game, ala) == 0 and mine.expeditions[ala.id].state == "chamber"   # she may try again
    assert ui.MSG_FAILED in message.embeds[0].description
    await game.click(message, ala, "leave")
    assert xp_of(game, ala) == 0
    # The owner is told once, whatever is clicked and however often the watch looks.
    await dungeon.tick(game.bot)
    log_ = game.bot.owner_log
    assert len(log_) == 1 and "dungeon.json" in log_[0] and "disk full" in log_[0] and f"<@{ala.id}>" in log_[0]
    monkeypatch.setattr(game.bot.store.dungeon, "save", working)
    await game.click(message, ala, "leave")
    assert xp_of(game, ala) == 20 and record_of(game, ala)["state"] == "done" and len(game.bot.owner_log) == 1


# -- the closing time, the pictures, the cog ------------------------------------------------------------------

async def test_the_latest_closing_time_is_shown_once_it_is_known(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    assert "najpóźniej" not in mine.announcement.embeds[0].description           # only the rule
    when = datetime.now(timezone.utc) + timedelta(minutes=40)
    await dungeon.closing_time(game.bot, when)
    stamp = f"najpóźniej <t:{math.ceil(when.timestamp())}:R>"
    assert stamp in mine.announcement.embeds[0].description
    message = await enter(game, mine, ala)
    assert stamp in message.embeds[0].description and stamp in mine.announcement.embeds[0].description
    await game.click(message, ala, "deeper")
    assert stamp in message.embeds[0].description
    await game.click(message, ala, "table")
    assert stamp in message.embeds[0].description
    # A moment that has passed is never shown: not on the pages, and the watch takes it off the announcement.
    mine.closes_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert mine.latest() == "" and stamp in mine.announcement.embeds[0].description
    await dungeon.tick(game.bot)
    assert "najpóźniej" not in mine.announcement.embeds[0].description and LOST_ALL in mine.announcement.embeds[0].description
    await game.click(message, ala, "back")
    assert "najpóźniej" not in message.embeds[0].description and LOST_ALL in message.embeds[0].description
    # A moment that changes is shown anew; one that is no longer known leaves the rule alone.
    later = datetime.now(timezone.utc) + timedelta(minutes=50)
    await dungeon.closing_time(game.bot, later)
    assert f"najpóźniej <t:{math.ceil(later.timestamp())}:R>" in mine.announcement.embeds[0].description
    await dungeon.closing_time(game.bot, None)
    assert "najpóźniej" not in mine.announcement.embeds[0].description
    # Once the mine is closed no page counts down to a closing that has already happened.
    mine.closes_at = later
    assert mine.latest() != ""
    await game.click(message, ala, "leave")
    assert mine.closed and mine.closes_at == later and mine.latest() == ""
    assert "najpóźniej" not in message.embeds[0].description
    assert "najpóźniej" not in mine.announcement.embeds[0].description
    # A mine whose closing time nobody knows states the rule alone - on every page.
    other = await open_mine(game)
    page = await enter(game, other, ala)
    assert "najpóźniej" not in page.embeds[0].description and LOST_ALL in page.embeds[0].description


@pytest.fixture(autouse=True)
def no_tournament(monkeypatch):
    """The weekly tournament is far away - in every test, whatever day and hour the tests run on
    (a test of the tournament's own moment replaces this)."""
    monkeypatch.setattr(events_cog.clock, "next_weekly",
                        lambda *when, after=None: datetime.now(timezone.utc) + timedelta(days=3))


async def test_the_event_loop_tells_the_mine_its_next_slot(game_db, no_tournament):
    game = game_db
    cog = events_cog.EventsCog(game.bot)
    assert cog.closing_slot() is None                                            # the loop is not sleeping
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON
    mine = dungeon.current(game.bot)
    assert mine.closes_at is None and "najpóźniej" not in mine.announcement.embeds[0].description
    cog.next_event_at = datetime.now(timezone.utc) + timedelta(minutes=35)       # the loop drew its next gap
    await cog.tell_closing_time()
    assert mine.closes_at == cog.next_event_at and "najpóźniej <t:" in mine.announcement.embeds[0].description
    # A mine called by hand while the loop sleeps knows it from the start.
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON
    again = dungeon.current(game.bot)
    assert again is not mine and mine.closed and again.closes_at == cog.next_event_at


async def test_the_closing_time_is_the_soonest_closing_that_is_no_secret(game_db, no_tournament, monkeypatch):
    """The next slot of the event loop - or, sooner, the end of the boss's quiet hour or the tournament.
    The boss's arrival is never told: the line is an "at the latest"."""
    from altermmo.core.state import BossPhase

    game = game_db
    bot = game.bot
    cog = events_cog.EventsCog(bot)
    cycle = bot.boss_cycle
    now = datetime.now(timezone.utc)
    slot, quiet = now + timedelta(minutes=40), now + timedelta(minutes=10)
    cog.next_event_at = slot
    assert cycle.silence_at() is None and cog.closing_slot() == slot
    # The quiet hour after a fight ends before the slot: the channel is cleared for the "silence" then.
    cycle.quiet_until, cycle.arrives_at = quiet, now + timedelta(minutes=25)
    await bot.state.set_boss_phase(BossPhase.SCHEDULED)
    assert cycle.silence_at() == quiet and cog.closing_slot() == quiet
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON
    mine = dungeon.current(bot)
    stamp = f"najpóźniej <t:{math.ceil(quiet.timestamp())}:R>"
    assert mine.closes_at == quiet and stamp in mine.announcement.embeds[0].description
    # It is known without the event loop too (the loop is running an event: no slot).
    cog.next_event_at = None
    assert cog.closing_slot() == quiet
    cog.next_event_at = now + timedelta(minutes=5)                                 # a slot before it wins
    assert cog.closing_slot() == cog.next_event_at
    # After the silence the boss is on its way: WHEN is a secret, so only the slot is told ...
    cog.next_event_at = slot
    await bot.state.set_boss_phase(BossPhase.WAITING)
    assert cycle.silence_at() is None and cog.closing_slot() == slot
    cog.next_event_at = None
    assert cog.closing_slot() is None                                              # ... or the rule alone
    # ... and a quiet hour that is over tells nothing either.
    await bot.state.set_boss_phase(BossPhase.SCHEDULED)
    cycle.quiet_until = now - timedelta(seconds=1)
    assert cycle.silence_at() is None and cog.closing_slot() is None
    # The Saturday tournament closes passive events as well.
    cog.next_event_at = slot
    tournament = now + timedelta(minutes=7)
    monkeypatch.setattr(events_cog.clock, "next_weekly", lambda *when, after=None: tournament)
    assert cog.closing_slot() == tournament and events_cog.TOURNAMENT_AT == (5, 19, 0)
    # A boss cycle that cannot answer costs the line, never the event.
    monkeypatch.setattr(type(cycle), "silence_at", lambda self: 1 / 0)
    assert cog.closing_slot() is None
    await cog.tell_closing_time()
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON


async def test_stop_spawn_boss_tells_the_open_mine_its_new_latest_closing(game_db, luck, boss, no_tournament,
                                                                         monkeypatch):
    """``!stopSpawnBoss`` drops the boss's quiet hour: the mine is no longer closed at its end, so it no longer
    calls that moment its latest closing - it shows the next slot at once.  And whatever else changes the
    moment, the watch asks the events cog again at every look."""
    game = game_db
    bot, state = game.bot, game.bot.state
    cycle = bot.boss_cycle
    cog = events_cog.EventsCog(bot)
    monkeypatch.setattr(bot, "get_cog", lambda name: cog if name == "Events" else None)   # (the loaded cog)
    now = datetime.now(timezone.utc)
    quiet, slot = now + timedelta(minutes=2), now + timedelta(minutes=32)
    cycle.quiet_until, cycle.arrives_at = quiet, quiet + timedelta(minutes=20)
    await state.set_boss_phase(BossPhase.SCHEDULED)
    cog.next_event_at = slot
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON
    mine = dungeon.current(bot)
    ala = game.add_member("Ala")
    message = await enter(game, mine, ala)
    before = f"najpóźniej <t:{math.ceil(quiet.timestamp())}:R>"
    assert mine.closes_at == quiet and before in mine.announcement.embeds[0].description
    await cycle.disable()                                                          # the owner: !stopSpawnBoss
    after = f"najpóźniej <t:{math.ceil(slot.timestamp())}:R>"
    assert dungeon.is_open(bot, mine) and mine.closes_at == slot
    assert after in mine.announcement.embeds[0].description and before not in mine.announcement.embeds[0].description
    await game.click(message, ala, "table")
    assert after in message.embeds[0].description
    # A moment changed behind the mine's back is put right by the watch's next look ...
    cycle.quiet_until = quiet
    await state.set_boss_phase(BossPhase.SCHEDULED)
    await dungeon.tick(bot)
    assert mine.closes_at == quiet and before in mine.announcement.embeds[0].description
    cycle.quiet_until = None
    await dungeon.tick(bot)
    assert mine.closes_at == slot and after in mine.announcement.embeds[0].description
    # ... and when nothing is known any more (the event loop is not sleeping) only the rule is left.
    cog.next_event_at = None
    await dungeon.tick(bot)
    assert mine.closes_at is None and "najpóźniej" not in mine.announcement.embeds[0].description
    assert dungeon.is_open(bot, mine) and LOST_ALL in mine.announcement.embeds[0].description


async def test_the_shown_latest_closing_is_rounded_up_to_the_second(game_db, luck):
    game = game_db
    mine = await open_mine(game)
    when = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=40, microseconds=1)
    await dungeon.closing_time(game.bot, when)
    shown = int(when.timestamp()) + 1                                              # never a moment before it
    assert f"najpóźniej <t:{shown}:R>" in mine.announcement.embeds[0].description


async def test_a_stopped_event_loop_promises_no_slot(game_db, no_tournament):
    game = game_db
    cog = events_cog.EventsCog(game.bot)
    game.bot.timings = dataclasses.replace(game.bot.timings, event_gap_min=600.0, event_gap_max=601.0)
    loop = asyncio.ensure_future(cog.event_loop())
    await until(lambda: cog.next_event_at is not None)
    assert cog.closing_slot() == cog.next_event_at
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop
    assert cog.next_event_at is None and cog.closing_slot() is None


async def test_two_settlers_of_the_leftover_mine_never_run_at_once(game_db, luck, monkeypatch):
    """The watch and a new mine's spawn may both go on with the closing of the previous run's mine: one at a
    time, so the summary is posted once and no egg is reported to the owner as "unknown"."""
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 5)
    await game.click(message, ala, "leave")                                        # two eggs, undecided
    assert record_of(game, ala)["state"] == "eggs" and len(fates(game, ala)) == 2
    real = pets.place_egg

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real(*args, **kwargs)

    monkeypatch.setattr(pets, "place_egg", slow)
    monkeypatch.setattr(dungeon, "RETRY_EVERY", 0.0)                               # only the lock keeps them apart
    bot._mine, bot._mine_jobs, bot._mine_restart_done = None, [], False          # the process is new
    bot._mine_leftover, bot._mine_leftover_told = None, False
    bot.boss_cycle._tidied = True
    bot.state.close_event()
    first = asyncio.ensure_future(dungeon.close_after_restart(bot))
    await asyncio.sleep(0.05)
    await dungeon._settle_leftover(bot)                                            # the second comes while it works
    await first
    await dungeon.tick(bot)
    assert len(summaries(game)) == 1
    assert not any("nie wiadomo" in note for note in bot.owner_log)
    assert all(fate not in (dungeon.UNDECIDED, dungeon.PENDING) for fate in fates(game, ala))
    assert await pets_count(game.bot.db) == 2                                       # each egg placed once


async def test_through_the_events_cog_the_next_event_closes_the_mine(game_db, luck):
    game = game_db
    ala = game.add_member("Ala")
    cog = events_cog.EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON
    state = game.bot.state
    mine = dungeon.current(game.bot)
    assert state.event_alive and state.event_type is EventType.DUNGEON and not state.busy
    assert state.free_for_activity is False and not game.bot.boss_cycle.activity_in_the_way()
    message = await enter(game, mine, ala)
    await descend(game, message, ala, 2)
    assert await cog.run_one_event(forced=EventType.CHEST) is EventType.CHEST
    assert mine.closed and state.event_type is EventType.CHEST and xp_of(game, ala) == 0
    assert message.deleted and mine.announcement.deleted                          # the channel was cleared
    await dungeon.tick(game.bot)
    assert len(summaries(game)) == 1 and game.channel.messages[-1].embeds[0].title == dungeon.SUMMARY_TITLE
    late = await game.click(message, ala, "leave")                                # an old button: stale
    assert late.answers == [ui.MSG_STALE] and xp_of(game, ala) == 0


async def test_a_broken_config_skips_the_event_and_tells_the_owner(game_db, monkeypatch, tmp_path):
    game = game_db
    broken = tmp_path / "dungeonConfig.json"
    broken.write_text('{"chambers": []}', encoding="utf-8")
    monkeypatch.setattr(dungeon, "CONFIG_FILE", broken)
    cog = events_cog.EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.DUNGEON) is None
    state = game.bot.state
    assert not state.event_alive and not state.busy and dungeon.current(game.bot) is None
    assert len(game.bot.owner_log) == 1 and "ConfigError" in game.bot.owner_log[0]


async def test_missing_pictures_do_not_break_the_event(game_db, luck, monkeypatch, tmp_path):
    game = game_db
    monkeypatch.setattr(dungeon, "PICTURES_DIR", tmp_path / "no-pictures")
    assert dungeon.picture(0) is None and dungeon.picture(3, with_pet=True) is None
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    assert mine.announcement.files == [] and mine.announcement.embeds[0].image.url is None
    message = await enter(game, mine, ala)
    await descend(game, message, ala, 3)
    assert message.files == [] and message.embeds[0].image.url is None
    assert message.embeds[0].title == "⛏️ 3/8 · Most nad Czarną Wodą"
    await game.click(message, ala, "leave")
    assert xp_of(game, ala) == 70


async def test_the_supervised_watch_closes_what_the_previous_run_left_open(game_db, luck, monkeypatch):
    game = game_db
    ala = game.add_member("Ala")
    mine = await open_mine(game)
    await enter(game, mine, ala)
    bot = game.bot
    bot._mine, bot._mine_jobs, bot._mine_restart_done = None, [], False          # the process is new
    bot.state.close_event()
    bot.boss_cycle._tidied = True
    monkeypatch.setattr(dungeon, "WATCH_EVERY", 0.01)
    bot.supervisor.start(dungeon.WATCH_NAME, lambda: dungeon.watch(bot))
    await until(lambda: summaries(game))
    assert record_of(game, ala)["state"] == "closed" and bot.supervisor.is_running(dungeon.WATCH_NAME)


# -- the boss never waits for the mine -----------------------------------------------------------------------

async def full_stable(db, player) -> None:
    """A pet at the player's side and four in the stable: no room for an egg anywhere."""
    await make_owner(db, player.id, pet_id=1, pet_id_alt1=2, pet_id_alt2=3, pet_id_alt3=4, pet_id_alt4=5)
    for pet_id in range(1, 6):
        await make_pet(db, pet_id, level=2, name=f"Pet{pet_id}")


async def test_the_boss_closes_the_mine_the_moment_its_quiet_hour_ends(game_db, luck, boss, no_tournament):
    """The moment the players are shown ("najpóźniej ...") is the moment the mine closes - although the
    "silence" itself still has to wait for a hunt."""
    game = game_db
    bot, state = game.bot, game.bot.state
    cycle = bot.boss_cycle
    ala = game.add_member("Ala")
    cog = events_cog.EventsCog(bot)
    quiet = datetime.now(timezone.utc) + timedelta(seconds=0.6)
    cycle.quiet_until, cycle.arrives_at = quiet, quiet + timedelta(minutes=20)
    await state.set_boss_phase(BossPhase.SCHEDULED)
    cog.next_event_at = quiet + timedelta(minutes=30)
    assert not cycle.wants_the_channel() and cog.closed_at_once() == ()
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON
    mine = dungeon.current(bot)
    stamp = f"najpóźniej <t:{math.ceil(quiet.timestamp())}:R>"
    assert mine.closes_at == quiet and stamp in mine.announcement.embeds[0].description
    message = await enter(game, mine, ala)
    await descend(game, message, ala, 3)
    with state.hold_busy("hunt"):                                                # a hunt is on, and stays on
        started = datetime.now(timezone.utc)
        phase = asyncio.ensure_future(cycle._phase_scheduled())
        await until(lambda: mine.closed)
        closed_at = datetime.now(timezone.utc)
        # At the shown moment: not half an hour before it (the timers of the event loop and the wall
        # clock may differ by a few milliseconds) ...
        assert (closed_at - quiet).total_seconds() > -0.1
        assert (closed_at - max(quiet, started)).total_seconds() < 1.0           # ... and not after it
        await until(lambda: mine.settled)
        assert not game.channel.ever_sent(boss_cycle.MSG_SILENCE) and state.boss_phase is BossPhase.SCHEDULED
        assert record_of(game, ala)["state"] == "closed" and xp_of(game, ala) == 0   # she lost everything
        assert not state.event_alive and mine.latest() == ""
        # From now on a mine would be closed at once: the draw leaves it out.  (The wall clock may lag
        # the event loop's timer by a few milliseconds - Windows' timer resolution - so it is waited for.)
        await until(lambda: datetime.now(timezone.utc) >= quiet)
        assert cycle.wants_the_channel() and cog.closed_at_once() == (EventType.DUNGEON,)
        assert cycle.silence_at() is None
    await asyncio.wait_for(phase, 5)                                             # the hunt is over: the silence
    assert game.channel.ever_sent(boss_cycle.MSG_SILENCE) and state.boss_phase is BossPhase.WAITING
    assert message.deleted and not cycle.wants_the_channel()
    await dungeon.tick(bot)
    assert len(summaries(game)) == 1 and ala.mention in summaries(game)[0].embeds[0].description


async def test_a_mine_opened_while_the_boss_waits_for_the_channel_is_closed_at_its_next_look(
        game_db, luck, boss, monkeypatch):
    game = game_db
    bot, state = game.bot, game.bot.state
    cycle = bot.boss_cycle
    cycle.quiet_until = datetime.now(timezone.utc) - timedelta(seconds=1)       # the quiet hour is over
    await state.set_boss_phase(BossPhase.SCHEDULED)
    state.open_event(EventType.CHEST)                                            # an unclaimed chest: the boss
    phase = asyncio.ensure_future(cycle._phase_scheduled())                      # waits for it, as it always did
    await asyncio.sleep(0.05)
    assert not phase.done() and state.event_type is EventType.CHEST
    mine = await open_mine(game)                                                 # a mine called by hand right then
    await asyncio.wait_for(phase, 5)                                             # ... is not waited for
    assert mine.closed and mine.settled and state.boss_phase is BossPhase.WAITING
    assert game.channel.ever_sent(boss_cycle.MSG_SILENCE) and summaries(game) == []
    # The draw itself never picks the mine in that window (it would live for seconds).
    await state.set_boss_phase(BossPhase.SCHEDULED)
    cog = events_cog.EventsCog(bot)
    left_out: list[tuple] = []
    monkeypatch.setattr(events_cog, "pick_event",
                        lambda now, *, debug, exclude=(): left_out.append(tuple(exclude)))   # (draws nothing)
    assert await cog.run_one_event() is None
    assert len(left_out) == 1 and EventType.DUNGEON in left_out[0]
    cycle.quiet_until = datetime.now(timezone.utc) + timedelta(minutes=5)        # ... but before it, as ever
    assert await cog.run_one_event() is None
    assert len(left_out) == 2 and EventType.DUNGEON not in left_out[1]


async def test_the_boss_on_its_way_never_waits_for_the_mine_or_for_an_answer(game_db, luck, boss, no_modifier):
    """The scheduled boss is due: the mine closes at once - an open question is not waited for."""
    game, db = game_db, game_db.bot.db
    bot, state = game.bot, game.bot.state
    cycle = bot.boss_cycle
    ala = game.add_member("Ala")
    await full_stable(db, ala)
    mine, message = await leave(game, ala, 4)                                    # one egg, no room for it
    menus: list = []
    game.channel.send_hooks.append(lambda sent: menus.append(sent) if sent.view is not None else None)
    swap = asyncio.ensure_future(game.click(message, ala, "egg-swap"))
    await until(lambda: menus)                                                   # the menu is open; she thinks
    cycle.base_rarity = 1
    await state.set_boss_phase(BossPhase.PRE_SPAWN)
    assert state.boss_blocks_activities and cycle.wants_the_channel()
    await asyncio.wait_for(cycle._phase_pre_spawn(), 5)                          # (it used to wait 30 minutes here)
    assert state.boss_phase is BossPhase.VISIBLE and game.channel.ever_sent(boss_cycle.MSG_WIND)
    assert any(call[0] == "image" for call in boss.calls) and mine.closed and mine.settled
    # Closed by the rule: the undecided egg had no room - lost; nothing was sacrificed for it.
    assert fates(game, ala) == [dungeon.EGG_LOST] and await pets_count(db) == 5 and no_modifier == []
    # Her answer comes too late and changes nothing.
    await game.click(menus[0], ala, "pick", values=["1:2"])
    await asyncio.wait_for(swap, 5)
    assert fates(game, ala) == [dungeon.EGG_LOST] and await pets_count(db) == 5 and len(menus) == 1
    assert (await row(db, ala.id))["pet_id_alt1"] == 2 and await album_of(db, ala) == []


async def test_the_boss_on_its_way_closes_a_mine_with_a_player_in_a_chamber_and_an_empty_one(game_db, luck, boss):
    game = game_db
    bot, state = game.bot, game.bot.state
    cycle = bot.boss_cycle
    ala = game.add_member("Ala")
    cycle.base_rarity = 1
    mine, message = await go_down(game, ala, 6)
    await state.set_boss_phase(BossPhase.PRE_SPAWN)
    await asyncio.wait_for(cycle._phase_pre_spawn(), 5)
    assert state.boss_phase is BossPhase.VISIBLE and mine.settled and message.deleted
    assert record_of(game, ala)["state"] == "closed" and xp_of(game, ala) == 0
    await dungeon.tick(bot)
    assert len(summaries(game)) == 1
    await state.set_boss_phase(BossPhase.IDLE)
    empty = await open_mine(game)                                                # nobody enters this one
    await state.set_boss_phase(BossPhase.PRE_SPAWN)
    await asyncio.wait_for(cycle._phase_pre_spawn(), 5)
    assert state.boss_phase is BossPhase.VISIBLE and empty.settled and len(summaries(game)) == 1


async def test_a_summoned_boss_closes_the_mine(game_db, luck, boss):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 8)                                  # the unclaimed treasury
    assert await bot.boss_cycle.summon(game.scene(), 3)
    assert bot.state.boss_phase is BossPhase.VISIBLE and mine.settled and message.deleted
    assert record_of(game, ala)["state"] == "closed" and xp_of(game, ala) == 0
    await dungeon.tick(bot)
    assert len(summaries(game)) == 1


async def test_the_tournament_closes_the_mine_before_it_starts(game_db, luck, monkeypatch):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 2)
    played: list[bool] = []

    async def tournament(scene):
        played.append(mine.settled and record_of(game, ala)["state"] == "closed")

    monkeypatch.setattr(events_feature, "spawn_pvp_tournament", tournament)
    await asyncio.wait_for(events_cog.EventsCog(bot).run_tournament(), 5)
    assert played == [True] and xp_of(game, ala) == 0 and message.deleted


async def test_the_closing_waits_a_moment_for_a_payment_in_flight_and_never_longer(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=0)
    mine, message = await go_down(game, ala, 5)
    started, gate = asyncio.Event(), asyncio.Event()
    working = pets.assign_scroll

    async def slow(scene, number, player_id):
        started.set()
        await gate.wait()                                                        # the database takes its time
        return await working(scene, number, player_id)

    monkeypatch.setattr(pets, "assign_scroll", slow)
    monkeypatch.setattr(dungeon, "CLOSE_WAIT", 0.05)
    leaving = asyncio.ensure_future(game.click(message, ala, "leave"))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(dungeon.close(bot), 2)                                # the boss: not held up by it
    saved = bot.store.dungeon.load()["mine"]
    assert mine.closed and not mine.settled and (saved["closed"], saved["settled"]) == (True, False)
    assert record_of(game, ala)["state"] == "leaving" and summaries(game) == []
    await dungeon.tick(bot)                                                      # still busy: the watch waits on
    assert not mine.settled and await pets_count(db) == 0
    gate.set()
    await asyncio.wait_for(leaving, 5)
    # The payment that was in flight completed - once; no click starts anything any more.
    assert xp_of(game, ala) == 180 and await scrolls_of(db, ala.id) == 2
    assert record_of(game, ala)["state"] == "eggs" and fates(game, ala) == ["", ""]
    await dungeon.tick(bot)                                                      # the watch finishes the closing
    assert mine.settled and record_of(game, ala)["state"] == "done"
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE] and await pets_count(db) == 2
    assert len(summaries(game)) == 1 and bot.owner_log == []
    await dungeon.tick(bot)
    assert xp_of(game, ala) == 180 and await pets_count(db) == 2 and len(summaries(game)) == 1


async def test_a_mine_that_is_still_being_closed_keeps_the_next_one_away(game_db, luck, monkeypatch):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 2)
    monkeypatch.setattr(dungeon, "CLOSE_WAIT", 0.01)
    cog = events_cog.EventsCog(bot)
    async with mine.expeditions[ala.id].lock:                                    # a click is being handled
        assert await cog.run_one_event(forced=EventType.DUNGEON) is None         # the file holds one mine
        assert dungeon.current(bot) is mine and mine.closed and not mine.settled
        assert len(bot.owner_log) == 1 and "NotSettled" in bot.owner_log[0]
        assert bot.store.dungeon.load()["mine"]["id"] == mine.id
    assert await cog.run_one_event(forced=EventType.DUNGEON) is EventType.DUNGEON   # the click is done
    new = dungeon.current(bot)
    assert mine.settled and new is not mine and bot.store.dungeon.load()["mine"]["id"] == new.id
    assert len(bot.owner_log) == 1


# -- a closing that is interrupted goes on -------------------------------------------------------------------

@pytest.mark.parametrize("goes_on", ["watch", "restart"])
async def test_a_closing_cancelled_between_two_eggs_is_finished_later(game_db, luck, monkeypatch, goes_on):
    """A shutdown or a cancelled task (!ResetBoss) in the middle of the closing: every egg still gets its fate once."""
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await leave(game, ala, 7)                                    # four eggs, none decided
    working = pets.place_egg
    calls: list[str] = []

    async def cancelled_at_the_second(scene, player, egg, where, **kwargs):
        calls.append(where)
        if len(calls) == 2:
            raise asyncio.CancelledError()
        return await working(scene, player, egg, where, **kwargs)

    monkeypatch.setattr(pets, "place_egg", cancelled_at_the_second)
    bot.state.close_event()                                                      # the boss took the channel
    with pytest.raises(asyncio.CancelledError):
        await dungeon.close(bot)
    saved = bot.store.dungeon.load()["mine"]
    assert (saved["closed"], saved["settled"], saved["summary"]) == (True, False, "")   # CLOSING, in the file
    assert fates(game, ala) == [pets.EGG_ACTIVE, dungeon.PENDING, "", ""] and summaries(game) == []
    assert mine.closed and not mine.settled
    if goes_on == "watch":
        await dungeon.tick(bot)                                                  # the same run: the watch
        assert mine.settled
    else:
        await restart(game)                                                      # ... or the next start of the bot
        await dungeon.tick(bot)
    # The egg whose transaction was interrupted is reported and never tried again; the rest is placed.
    assert fates(game, ala) == [pets.EGG_ACTIVE, dungeon.EGG_UNKNOWN, pets.EGG_IN_STABLE, pets.EGG_IN_STABLE]
    assert calls == [pets.EGG_ANYWHERE] * 4 and await pets_count(db) == 3
    assert len(bot.owner_log) == 1 and "nie wiadomo, czy jajo zostało zapisane" in bot.owner_log[0]
    saved = bot.store.dungeon.load()["mine"]
    assert (saved["closed"], saved["settled"], saved["summary"]) == (True, True, "")
    [summary] = summaries(game)
    assert (f"• {ala.mention}: jajo 1 z 4 jest teraz Twoim towarzyszem; jajo 2 z 4 nie zostało potwierdzone - "
            "administracja dostała znać; jajo 3 z 4 trafiło do stajni (miejsce 1); "
            "jajo 4 z 4 trafiło do stajni (miejsce 2)") in summary.embeds[0].description
    for _ in range(2):                                                            # ... and that was all of it
        await dungeon.close(bot)
        await restart(game)
        await dungeon.tick(bot)
    assert await pets_count(db) == 3 and len(summaries(game)) == 1 and len(bot.owner_log) == 1


async def test_a_restart_that_dies_while_it_closes_the_mine_is_finished_by_the_next_one(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    await leave(game, ala, 6)                                                    # three eggs, none decided

    class Killed(BaseException):
        """The process dies."""

    working = pets.place_egg

    async def killed(scene, player, egg, where, **kwargs):
        raise Killed()

    monkeypatch.setattr(pets, "place_egg", killed)
    with pytest.raises(Killed):
        await restart(game)                                                      # dies at the first egg
    saved = bot.store.dungeon.load()["mine"]
    assert (saved["closed"], saved["settled"]) == (True, False) and fates(game, ala) == [dungeon.PENDING, "", ""]
    monkeypatch.setattr(pets, "place_egg", working)
    await restart(game)
    await dungeon.tick(bot)
    assert fates(game, ala) == [dungeon.EGG_UNKNOWN, pets.EGG_ACTIVE, pets.EGG_IN_STABLE]
    assert await pets_count(db) == 2 and len(bot.owner_log) == 1 and len(summaries(game)) == 1
    assert bot.store.dungeon.load()["mine"]["settled"] is True
    await restart(game)
    await dungeon.tick(bot)
    assert await pets_count(db) == 2 and len(bot.owner_log) == 1 and len(summaries(game)) == 1


@pytest.mark.parametrize("state", ["chamber", "leaving", "eggs"])
async def test_a_file_that_says_closing_is_settled_by_the_restart(game_db, luck, state):
    """``closed`` in the file is not ``settled``: whatever state the interrupted closing left is finished."""
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    if state == "chamber":
        await go_down(game, ala, 5)
    else:
        await leave(game, ala, 5)
    data = bot.store.dungeon.load()
    data["mine"]["closed"] = True                                                # the closing had begun ...
    if state == "leaving":
        data["mine"]["expeditions"][str(ala.id)].update(state=dungeon.LEAVING, granted=None)
    bot.store.dungeon.save(data)
    paid = xp_of(game, ala)
    for _ in range(2):
        await restart(game)
        await dungeon.tick(bot)
        saved = bot.store.dungeon.load()["mine"]
        assert (saved["closed"], saved["settled"], saved["summary"]) == (True, True, "")
        assert record_of(game, ala)["state"] == ("closed" if state == "chamber" else "done")
        assert xp_of(game, ala) == paid and len(summaries(game)) == 1
        assert await pets_count(db) == (0 if state == "chamber" else 2)
        assert len(bot.owner_log) == (1 if state == "leaving" else 0)
    if state == "leaving":                                                        # what she sees of it, if anything
        assert dungeon.MSG_PAYMENT_UNKNOWN in dungeon.result_embed(
            dungeon.load_config(), ala.id, record_of(game, ala)).description


# -- nothing is placed on an intent that is not on the disk -----------------------------------------------------

async def test_no_egg_is_placed_when_its_pending_step_cannot_be_saved(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await leave(game, ala, 6)                                    # three eggs
    working = bot.store.dungeon.save
    monkeypatch.setattr(bot.store.dungeon, "save", disk_full)
    for _ in range(2):                                                            # however often she clicks
        await game.click(message, ala, "egg-take")
        assert await pets_count(db) == 0 and (await row(db, ala.id))["pet_id"] == 0   # nothing happened ...
        assert [entry["fate"] for entry in mine.expeditions[ala.id].record["eggs"]] == ["", "", ""]
        text = message.embeds[0].description
        assert ui.MSG_FAILED in text and "Jajo 1 z 3" in text                     # ... and she is told: choose again
        assert message.button_labels == [dungeon.EGG_ACTIVE_LABEL, dungeon.EGG_STABLE_LABEL,
                                         dungeon.EGG_SACRIFICE_LABEL]
    assert len(bot.owner_log) == 1 and "dungeon.json" in bot.owner_log[0]        # the owner is told once
    # The bot is restarted before the disk works again: the file still holds three undecided eggs -
    # and three eggs is what she gets (the row holds three), not one more.
    monkeypatch.setattr(bot.store.dungeon, "save", working)
    await restart(game)
    await dungeon.tick(bot)
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE, pets.EGG_IN_STABLE]
    assert await pets_count(db) == 3 and len(bot.owner_log) == 1


async def test_pending_is_on_the_disk_before_an_egg_is_placed(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    mine, message = await leave(game, ala, 6)                                    # three eggs
    working = pets.place_egg
    on_disk: list[list[str]] = []

    async def looking(scene, player, egg, where, **kwargs):
        on_disk.append(fates(game, ala))                                          # the file at that very moment
        return await working(scene, player, egg, where, **kwargs)

    monkeypatch.setattr(pets, "place_egg", looking)
    await game.click(message, ala, "egg-take")                                    # her own choice ...
    await close_by(game, "event")                                                 # ... and the closing rule
    assert on_disk == [[dungeon.PENDING, "", ""], [pets.EGG_ACTIVE, dungeon.PENDING, ""],
                       [pets.EGG_ACTIVE, pets.EGG_IN_STABLE, dungeon.PENDING]]
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE, pets.EGG_IN_STABLE]


async def test_the_closing_places_nothing_while_the_file_cannot_be_written(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await leave(game, ala, 5)                                    # two eggs, none decided
    working = bot.store.dungeon.save
    monkeypatch.setattr(bot.store.dungeon, "save", disk_full)
    bot.state.close_event()
    await dungeon.close(bot)                                                      # the next event / the boss
    await dungeon.tick(bot)
    assert mine.closed and not mine.settled and await pets_count(db) == 0         # closed for clicks, nothing placed
    assert [entry["fate"] for entry in mine.expeditions[ala.id].record["eggs"]] == ["", ""]
    assert len(bot.owner_log) == 1 and "dungeon.json" in bot.owner_log[0] and summaries(game) == []
    assert bot.store.dungeon.load()["mine"]["closed"] is False                    # (the file is where it was)
    monkeypatch.setattr(bot.store.dungeon, "save", working)
    await dungeon.tick(bot)                                                       # the disk works again
    assert mine.settled and fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE]
    assert await pets_count(db) == 2 and len(summaries(game)) == 1 and len(bot.owner_log) == 1
    await restart(game)
    await dungeon.tick(bot)
    assert await pets_count(db) == 2 and len(summaries(game)) == 1


async def test_a_restart_places_nothing_while_the_file_cannot_be_written(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    await leave(game, ala, 5)                                                     # two eggs, none decided
    working = bot.store.dungeon.save
    monkeypatch.setattr(bot.store.dungeon, "save", disk_full)
    await restart(game)
    await dungeon.tick(bot)
    assert await pets_count(db) == 0 and summaries(game) == []                    # nothing that a next start repeats
    assert len(bot.owner_log) == 1 and "dungeon.json" in bot.owner_log[0]
    with pytest.raises(dungeon.NotSettled):                                       # and no new mine over that file
        await open_mine(game)
    monkeypatch.setattr(bot.store.dungeon, "save", working)
    await dungeon.tick(bot)                                                       # the watch goes on with it
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE] and await pets_count(db) == 2
    assert len(summaries(game)) == 1 and len(bot.owner_log) == 1
    assert await open_mine(game) is not None


async def test_a_file_that_falls_behind_is_written_again_by_the_watch(game_db, luck, monkeypatch):
    """The save AFTER a payment fails: the watch repairs the file, so a restart repeats nothing."""
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await leave(game, ala, 5)                                    # two eggs
    working = bot.store.dungeon.save
    saves: list[int] = []

    def fails_at_the_second(value):
        saves.append(1)
        if len(saves) == 2:                                                       # (1: "pending"; 2: the real fate)
            raise OSError("disk full")
        working(value)

    monkeypatch.setattr(bot.store.dungeon, "save", fails_at_the_second)
    await game.click(message, ala, "egg-take")
    assert await pets_count(db) == 1 and fates(game, ala) == [dungeon.PENDING, ""] and mine.unsaved
    assert len(bot.owner_log) == 1 and f"<@{ala.id}>" in bot.owner_log[0]
    await dungeon.tick(bot)
    assert fates(game, ala) == [pets.EGG_ACTIVE, ""] and not mine.unsaved
    await restart(game)
    await dungeon.tick(bot)
    assert fates(game, ala) == [pets.EGG_ACTIVE, pets.EGG_IN_STABLE] and await pets_count(db) == 2
    assert len(bot.owner_log) == 1


# -- what the reviews found besides ------------------------------------------------------------------------------

@pytest.mark.parametrize("question", ["second", "first", "sacrifice"])
async def test_a_word_typed_before_the_question_is_posted_is_no_answer(game_db, luck, no_modifier, monkeypatch,
                                                                       question):
    """``!potwierdzam`` typed once more while the question is still on its way to Discord answers nothing:
    nobody has seen that question yet, and a sacrifice cannot be undone."""
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await full_stable(db, ala)
    mine, message = await leave(game, ala, 4)
    before = await row(db, ala.id)
    wanted = {"second": "pytanie 2 z 2", "first": "pytanie 1 z 2", "sacrifice": "poświęcić to jajo"}[question]
    if question != "sacrifice":
        game.auto_click(ala, "pick", values=["3:4"])
    order: list[str] = []

    def posted(sent) -> None:
        if sent.view is None:
            return
        order.append("posted: " + sent.content)
        if question == "second" and "pytanie 1 z 2" in sent.content:
            game.say_later(ala, "!potwierdzam", 0.0)                             # the one deliberate answer

    game.channel.send_hooks.append(posted)
    send = ui.ClickView.send

    async def slow_send(self, destination, content=None, **kwargs):
        if content and wanted in content:
            order.append("typed")                                                # the word arrives ...
            game.say(ala, "!potwierdzam")
            await asyncio.sleep(0.05)                                            # ... during the round trip of the post
        return await send(self, destination, content, **kwargs)

    monkeypatch.setattr(ui.ClickView, "send", slow_send)
    await game.click(message, ala, "egg-sacrifice" if question == "sacrifice" else "egg-swap")
    # (The word reached the bot before that question was on the channel - and it was the last one asked.)
    assert order.index("typed") == len(order) - 2 and order[-1].startswith("posted: ") and wanted in order[-1]
    # Nothing changed: no pet and no egg was sacrificed, the egg is still hers to decide.
    assert await row(db, ala.id) == before and await pets_count(db) == 5 and await album_of(db, ala) == []
    assert no_modifier == [] and fates(game, ala) == [""] and record_of(game, ala)["state"] == "eggs"
    assert dungeon.MSG_NOTHING_CHANGED in message.embeds[0].description
    assert message.button_labels == [dungeon.EGG_SWAP_LABEL, dungeon.EGG_SACRIFICE_LABEL]
    # The same word typed AFTER the question is on the channel is an answer, as it always was.
    monkeypatch.setattr(ui.ClickView, "send", send)
    game.channel.send_hooks.remove(posted)

    def answers_in_words(sent) -> None:
        if sent.view is not None and ("pytanie" in sent.content or "poświęcić to jajo" in sent.content):
            game.say_later(ala, "!potwierdzam", 0.0)

    game.channel.send_hooks.append(answers_in_words)
    if question != "sacrifice":
        game.auto_click(ala, "pick", values=["3:4"])
    await game.click(message, ala, "egg-sacrifice" if question == "sacrifice" else "egg-swap")
    assert fates(game, ala) == [pets.EGG_SACRIFICED if question == "sacrifice" else pets.EGG_SWAPPED]


async def test_a_second_egg_click_while_a_question_is_open_asks_nothing_more(game_db, luck, no_modifier):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await full_stable(db, ala)
    mine, message = await leave(game, ala, 4)
    asked: list = []
    game.channel.send_hooks.append(lambda sent: asked.append(sent) if sent.view is not None else None)
    swap = asyncio.ensure_future(game.click(message, ala, "egg-swap"))
    await until(lambda: asked)                                                   # the swap's menu is open
    other = await game.click(message, ala, "egg-sacrifice")
    assert other.answers == [pets.MSG_ALREADY_WAITING] and len(asked) == 1       # one question at a time
    assert mine.expeditions[ala.id].asking and fates(game, ala) == [""]
    await game.click(asked[0], ala, "pick-cancel")
    await asyncio.wait_for(swap, 5)
    assert fates(game, ala) == [""] and await pets_count(db) == 5 and not mine.expeditions[ala.id].asking


async def test_the_mine_closing_while_the_eggs_are_rolled_pays_nothing(game_db, luck, monkeypatch):
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, scrolls=0)
    mine, message = await go_down(game, ala, 5)
    working = pets._cosmetic_state
    closing: list = []

    async def closed_meanwhile(bot_):
        answer = await working(bot_)
        bot.state.close_event()                                                  # the boss took the channel
        closing.append(asyncio.ensure_future(dungeon.close(bot)))
        await asyncio.sleep(0)                                                   # (the closing has begun)
        return answer

    monkeypatch.setattr(pets, "_cosmetic_state", closed_meanwhile)
    await game.click(message, ala, "leave")
    await asyncio.wait_for(closing[0], 5)
    assert xp_of(game, ala) == 0 and await scrolls_of(db, ala.id) == 0 and await pets_count(db) == 0
    assert record_of(game, ala) == {"state": "closed", "chamber": 5, "eggs": [], "granted": None}
    assert mine.settled


async def test_an_undecided_mythic_egg_is_placed_quietly_at_the_closing(game_db, luck, monkeypatch):
    """The channel belongs to whoever closed the mine: no announcement, no ping of the hunters' role."""
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    rig(monkeypatch, randint=lambda low, high: 1000 if high == 1000 else 0)       # the roll of a mythic egg
    await leave(game, ala, 4)
    assert record_of(game, ala)["eggs"][0]["egg"]["MYTHIC"] is True
    await close_by(game, "event")
    owner = await row(db, ala.id)
    assert fates(game, ala) == [pets.EGG_ACTIVE]
    assert await db.fetchval("SELECT MYTHIC FROM PETS WHERE PET_ID = $1", owner["pet_id"]) is True
    assert not game.channel.ever_sent("mitycznego")


async def test_an_egg_at_the_players_side_is_no_companion_on_the_pictures(game_db, luck):
    """The "-pet" pictures are for a HATCHED pet at the player's side."""
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    await make_owner(db, ala.id, pet_id=1)
    await make_pet(db, 1, level=0)                                               # an egg
    mine, message = await go_down(game, ala, 3)
    assert message.files == ["03.jpg"] and mine.announcement.files == ["00.jpg"]
    assert not mine.expeditions[ala.id].with_pet


async def test_the_page_of_an_egg_never_names_its_species(game_db, luck, monkeypatch):
    game = game_db
    for name, roles, species in (("Ala", [PATRON], pets.PetType.GHOST), ("Ola", [], pets.PetType.BEAR)):
        player = game.add_member(name, roles=roles)
        rig(monkeypatch, choices=[species], randint=lambda low, high: 0)
        _mine, message = await leave(game, player, 4)
        egg = record_of(game, player)["eggs"][0]["egg"]
        polish = pets.POLISH_TYPES[egg["TYPE"]]
        text = message.embeds[0].title + " " + message.embeds[0].description
        assert "Jajo 1 z 1" in text and egg["TYPE"] == species.value and polish
        assert egg["TYPE"] not in text and polish.lower() not in text.lower()


def test_the_help_mentions_the_mine():
    """In the help's introduction, next to the buttons - the "⚔️ Przygoda" field is nearly full (1024 at most)."""
    assert ("⛏️ **Kopalnia Ostatniej Lampy:** schodzi do niej tylko ten, kto pierwszy kliknie **Wejdź do kopalni**. "
            "Łup trzeba zabrać, zanim kopalnię zamknie następne wydarzenie albo boss - inaczej przepada.") in (
        help_cog.HELP_INTRO)
    embed = help_cog.help_embed()
    assert "Kopalnia" in embed.description and len(embed.description) <= 4096 and len(embed) <= 6000
    adventure = dict(help_cog.HELP_FIELDS)["⚔️ Przygoda"]
    assert "Kopalnia" not in adventure and len(adventure) < 1000                  # room left for the next line


async def test_a_sacrificed_egg_is_not_reported_as_a_stored_rare_egg(game_db, no_modifier):
    """The owner's note about rare cosmetics names a PET_ID: only an egg that IS in PETS gets one."""
    game, db = game_db, game_db.bot.db
    bot = game.bot
    ala = game.add_member("Ala", roles=[PATRON])
    await make_owner(db, ala.id, pet_id=1)
    await make_pet(db, 1, level=1)
    rare = {**pets.roll_egg(ala), "SEAL": pets.MAX_SEAL, "HOLO": True}
    assert pets._rare_finds(rare)
    placed = await pets.place_egg(game.scene(ala), ala, dict(rare), pets.EGG_SACRIFICE)
    assert (placed.status, placed.pet_id) == (pets.EGG_SACRIFICED, 0) and placed.kept is not None
    assert await pets_count(db) == 1 and len(await album_of(db, ala)) == 1       # in her album, not in PETS
    assert bot.owner_log == []
    kept = await pets.place_egg(game.scene(ala), ala, dict(rare), pets.EGG_TO_STABLE)
    assert kept.status == pets.EGG_IN_STABLE and await db.fetchval(
        "SELECT COUNT(*) FROM PETS WHERE PET_ID = $1", kept.pet_id) == 1
    assert len(bot.owner_log) == 1 and f"(PET_ID {kept.pet_id})" in bot.owner_log[0]
    assert "Rzadka kosmetyka" in bot.owner_log[0]


# -- a page Discord refuses ----------------------------------------------------------------------------------------

async def test_a_page_discord_refuses_is_shown_in_words_and_the_buttons_work(game_db, luck):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 2)
    edit = message.edit
    refused: list[str] = []

    async def picky(**kwargs):
        embed = kwargs.get("embed")
        if embed is not None and embed.image.url:                                # Discord refuses the page as it is
            refused.append(embed.title)
            raise bad_request()
        return await edit(**kwargs)

    message.edit = picky
    await game.click(message, ala, "deeper")
    page = message.embeds[0]
    assert refused == ["⛏️ 3/8 · Most nad Czarną Wodą"] and page.title == refused[0]   # shown all the same:
    assert page.image.url is None and message.files == []                         # in words, without the picture
    assert "**Powrót teraz:** 70 XP + 1 zwój" in page.description and LOST_ALL in page.description
    assert message.button_labels == [dungeon.LEAVE_LABEL, dungeon.DEEPER_LABEL, dungeon.STABLE_LABEL,
                                     dungeon.TABLE_LABEL]
    assert len(bot.owner_log) == 1 and "odrzucił stronę wyprawy" in bot.owner_log[0] and f"<@{ala.id}>" in bot.owner_log[0]
    await game.click(message, ala, "table")                                       # every button answers
    assert message.embeds[0].title.endswith(dungeon.TABLE_LABEL)
    await game.click(message, ala, "back")
    await game.click(message, ala, "leave")                                       # ... and she can take her loot
    assert xp_of(game, ala) == 70 and record_of(game, ala)["state"] == "done"
    assert "✅ 70 XP" in message.embeds[0].description and len(bot.owner_log) == 1   # the owner was told once


@pytest.mark.parametrize("gone", [False, True])
async def test_an_expedition_whose_message_takes_no_edit_gets_a_new_message(game_db, luck, gone):
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala")
    mine, message = await go_down(game, ala, 2)
    view = message.view
    if gone:
        await message.delete()                                                    # somebody deleted it
    else:
        async def refusing(**kwargs):
            raise bad_request()

        message.edit = refusing                                                   # Discord takes no edit at all
    message.view = view
    await game.click(message, ala, "deeper")
    fresh = mine.expeditions[ala.id].message
    assert fresh is not message and fresh in game.channel.messages
    assert fresh.embeds[0].title == "⛏️ 3/8 · Most nad Czarną Wodą" and ala.mention in fresh.embeds[0].description
    assert fresh.button_labels == [dungeon.LEAVE_LABEL, dungeon.DEEPER_LABEL, dungeon.STABLE_LABEL,
                                   dungeon.TABLE_LABEL]
    assert ui.is_live(fresh.id) and not ui.is_live(message.id)
    assert len(bot.owner_log) == (0 if gone else 1)                               # (a deleted message is no refusal)
    await game.click(fresh, ala, "leave")
    assert xp_of(game, ala) == 70 and record_of(game, ala)["state"] == "done" and luck.calls == [10, 5]


def test_a_page_in_words_fits_whatever_it_was():
    embed = discord.Embed(title="T" * 400, description="D" * 9000, color=dungeon.COLOUR)
    embed.set_image(url="attachment://03.jpg")
    plain = dungeon.plain_page(embed)
    assert len(plain.title) == 256 and len(plain.description) == 4096 and len(plain) <= 6000
    assert plain.image.url is None and plain.description.endswith("…") and plain.color == embed.color
    short = dungeon.plain_page(discord.Embed(title="Komora", description="Opis"))
    assert (short.title, short.description) == ("Komora", "Opis")
