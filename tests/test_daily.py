"""The daily hunt (altermmo/features/daily.py) and its commands (altermmo/cogs/hunting.py)."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import random
import re
from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from altermmo.cogs import hunting
from altermmo.core import challenge, fightlog, guards
from altermmo.core.challenge import Outcome, RoundResult
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossPhase
from altermmo.features import achievements, daily, database, items, modifiers, patrons, pets

from fakes import FakeChannel, FakeMember, FakeMessage, requested_word

ZERO = {"CRIT_PERC": 0.0, "REPLACE_PERC": 0.0, "DEF_PERC": 0.0, "DROP_PERC": 0.0,
        "LOWHP_PERC": 0.0, "SLOW_PERC": 0.0}

SHARD = {"id": 9, "descr": "Fragment zwoju. Zbierz 10, żeby zdobyć pełny zwój.", "weight": 100}
EGG = {"id": 10, "descr": "Czekaj... Co to? Jajko? Sprawdź wpisując **!towarzysz**.", "weight": 100}
SCROLL = {"id": 11, "descr": "Dostałeś zwój.", "weight": 100}
LEVEL = {"id": 12, "descr": "Twój towarzysz awansował na wyższy poziom!", "weight": 100}
STONE = {"id": 13, "descr": "Otrzymałeś kamień przebudzenia.", "weight": 100}
MIRROR = {"id": 14, "descr": "Otrzymałeś lustro wizualiów.", "weight": 100}
TEXT_ONLY = {"id": 1, "descr": "Doświadczenie w walce", "weight": 100}


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def quick_lobby(monkeypatch):
    """Nobody joins in most tests: do not wait the real 8 seconds for a second hunter."""
    monkeypatch.setattr(daily, "JOIN_TIME", 0.03)
    monkeypatch.setattr(guards, "_last_notice", {})
    daily._gathering.clear()
    daily._panels.clear()


@pytest.fixture(autouse=True)
def loot(monkeypatch, tmp_path):
    """A small, predictable monster loot config; ``loot(Normal=[...])`` rewrites it."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(daily, "CONFIG_DIR", config_dir)

    def write(**tables) -> None:
        data = {f"loot_details_{name}": tables.get(name, [TEXT_ONLY]) for name in ("Normal", "Rare", "Epic")}
        (config_dir / "mobLootConfig.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    write()
    return write


@pytest.fixture
def skills(monkeypatch):
    """Pet bonuses without a database: ``skills[player.id] = {"DEF_PERC": 100}``."""
    table: dict[int, dict] = {}

    async def fake_pet_skills(bot, player_id):
        return {**ZERO, **{key: float(value) for key, value in table.get(player_id, {}).items()}}

    monkeypatch.setattr(daily, "pet_skills", fake_pet_skills)
    return table


def prompts(channel) -> list[str]:
    """Every numbered fight prompt the bot posted."""
    return [m.content for m in channel.sent if re.match(r"^(~~)?\d+\. ", m.content)]


def team_answers(game, players, *, wrong_rounds=(), silent_rounds=(), intruder_rounds=()) -> None:
    """Everybody answers only the prompts addressed to them (``N. **Name**: ...``)."""
    by_name = {player.name: player for player in players}
    counter = itertools.count(1)

    def hook(message):
        match = re.match(r"^\d+\. \*\*(.+?)\*\*: ", message.content)
        word = requested_word(message.content)
        if match is None or word is None:
            return
        number = next(counter)
        addressed = by_name[match.group(1)]
        if number in silent_rounds:
            return
        if number in wrong_rounds:
            game.say_later(addressed, "zlaodpowiedz", 0)
        elif number in intruder_rounds:
            game.say_later(next(p for p in players if p.id != addressed.id), word, 0)
        else:
            game.say_later(addressed, word, 0)

    game.channel.send_hooks.append(hook)


def take_turns(monkeypatch) -> None:
    """``random.choice`` over hunters goes round-robin instead of being random."""
    real_choice = random.choice
    counter = itertools.count()

    def choice(seq):
        if seq and all(isinstance(item, FakeMember) for item in seq):
            return seq[next(counter) % len(seq)]
        return real_choice(seq)

    monkeypatch.setattr(random, "choice", choice)


def spy_on_rounds(monkeypatch) -> list[float]:
    """Record the time limit of every fight round."""
    real_ask = challenge.ask
    limits: list[float] = []

    async def spy(bot, channel, prompt, **kwargs):
        limits.append(kwargs["timeout"])
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", spy)
    return limits


async def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.001)


def make_ctx(game, author, content="!polowanie", *, channel=None, admin=False, message=None):
    channel = channel or game.channel
    if message is None:
        message = FakeMessage(channel, author, content)
        channel.messages.append(message)
    return SimpleNamespace(bot=game.bot, channel=channel, author=author, message=message, guild=game.guild,
                           permissions=discord.Permissions(administrator=admin), command=None)


async def run_checks(command, ctx) -> None:
    """Run the command's checks in discord.py's order; raises what a failing check raises."""
    for check in command.checks:
        result = check(ctx)
        if inspect.isawaitable(result):
            result = await result
        assert result


async def owner_row(bot, player_id):
    return await bot.db.fetchrow("SELECT * FROM PETOWNER WHERE PLAYER_ID = $1", player_id)


# -- numbers -------------------------------------------------------------------------------

@pytest.mark.parametrize("drawn, rarity", [(0, 0), (69, 0), (70, 1), (98, 1), (99, 2), (100, 2)])
def test_hunt_rarity(monkeypatch, drawn, rarity):
    monkeypatch.setattr(random, "randint", lambda low, high: drawn if (low, high) == (0, 100) else low)
    assert daily.roll_hunt_rarity() == rarity


@pytest.mark.parametrize("rarity, low, high", [(0, 3, 5), (1, 6, 9), (2, 9, 12), (7, 9, 12)])
def test_random_boss_hp_ranges(monkeypatch, rarity, low, high):
    seen = []
    monkeypatch.setattr(random, "randint", lambda a, b: seen.append((a, b)) or a)
    assert daily.random_boss_hp(rarity) == low and seen == [(low, high)]


def test_hp_scaling_never_drops_below_one():
    assert daily.scaled_hp(5, -10) == 4          # legacy int(5 * 0.9)
    assert daily.scaled_hp(20, 15) == 23
    assert daily.scaled_hp(5, -100) == 1 and daily.scaled_hp(5, -250) == 1   # used to divide by zero later


def test_words_and_real_loot_config(monkeypatch):
    assert len(daily.MOB_ACTIONS) == 15
    assert all(requested_word(prompt) == word for word, prompt in daily.MOB_ACTIONS)
    monkeypatch.undo()                            # the real config/mobLootConfig.json
    # The older items, then the new ones of features/items.py - turned on, every row above 0 (a weight-0
    # tier has no row); tests/test_items.py checks their place, their keys and the owner's weights.
    known = {1, 9, 10, 11, 12, 13, 14, *items.BY_LOOT_ID}
    for rarity in (0, 1, 2, 3):
        table = daily._loot_table(rarity)
        assert table and all(item["id"] in known and item["weight"] > 0 and item["descr"] for item in table)
    assert daily._loot_table(3) == daily._loot_table(2)


# -- the cooldown list ---------------------------------------------------------------------

async def test_daily_cooldown_file(game):
    bot = game.bot
    ala = game.add_member("Ala")
    assert not daily.load_daily_from_file(bot, ala.id)
    daily.save_daily_to_file(bot, ala.id)
    daily.save_daily_to_file(bot, str(ala.id))                 # no duplicates
    daily.save_daily_to_file(bot, 123456)
    daily.save_daily_to_file(bot, 9123456)
    assert daily.load_daily_from_file(bot, ala.id) and daily.load_daily_from_file(bot, str(ala.id))
    # The legacy format: one id per line.
    assert bot.store.daily_cd.path.read_text(encoding="utf-8") == f"{ala.id}\n123456\n9123456\n"

    # Exact ids only: the legacy substring replace also damaged "9123456" and left empty lines.
    assert await daily.remove_player_daily_file(bot, ["123456"]) == 1
    assert bot.store.daily_cd.all() == [str(ala.id), "9123456"]
    assert await daily.remove_player_daily_file(bot, [ala, 9123456, "", "777"]) == 2
    assert await daily.remove_player_daily_file(bot, []) == 0
    assert bot.store.daily_cd.all() == []

    daily.save_daily_to_file(bot, ala.id)
    await daily.clear_daily_file(bot)
    assert not daily.load_daily_from_file(bot, ala.id)
    assert bot.owner_log == ["Zresetowano cooldown na daily."]
    assert game.log_channel.messages[-1].content == "Zresetowano cooldown na daily."


# -- the monster appears -------------------------------------------------------------------

async def test_mob_image_of_a_monster(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda low, high: 3)
    assert await daily.send_mob_image(game.scene(), 0) == (False, None)
    picture, card = game.channel.messages[-2:]
    assert picture.files == ["3.png"]
    embed = card.embeds[0]
    assert embed.title == "💀 Zwykły mob! 💀"
    assert embed.description == ("Na polowaniu pojawił się zwykły potwór! ⚔️"
                                 "\n\n*Inny gracz może dołączyć do polowania, klikając „Dołącz do polowania” "
                                 "albo wpisując natychmiast !polowanie*.")
    assert card.button_labels == ["Dołącz do polowania"]
    assert embed.colour.value == 0xFFFFFF and embed.thumbnail.url.endswith("large-green-square_1f7e9.png")

    assert await daily.send_mob_image(game.scene(), 1) == (False, None)
    picture, card = game.channel.messages[-2:]
    assert picture.files == ["13.png"] and card.embeds[0].title == "💀 Rzadki mob! 💀"
    assert card.embeds[0].description.startswith("Na polowaniu pojawił się rzadki potwór! ⚔️")
    assert card.embeds[0].colour.value == 0x0000FF
    assert card.embeds[0].thumbnail.url.endswith("large-blue-square_1f7e6.png")

    for rarity in (2, 5):
        assert await daily.send_mob_image(game.scene(), rarity) == (False, None)
        picture, card = game.channel.messages[-2:]
        assert picture.files == ["100.png"] and card.embeds[0].title == "💀 Unikalny mob! 💀"
        assert card.embeds[0].description.startswith("Na polowaniu pojawił się UNIKALNY potwór! ⚔️")
        assert card.embeds[0].colour.value == 0xFCBA03
        assert card.embeds[0].thumbnail.url.endswith("large-orange-square_1f7ea.png")


async def test_mob_image_of_a_player_chosen_at_the_shrine(game):
    bot = game.bot
    patron = game.add_member("Patron")
    await modifiers.modify_modifiers(bot, "player_id", patron.id)
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 20)
    assert await daily.send_mob_image(game.scene(), 1) == (True, patron)
    avatar, card = game.channel.messages[-2:]
    assert avatar.content == f"https://cdn.example/{patron.id}.png" and avatar.files == []
    embed = card.embeds[0]
    assert embed.title == "💀 Rzadki Patron! 💀"
    assert embed.description == (
        "Na polowaniu pojawił się rzadki potwór! ⚔️"
        "\n\n*Potworem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, to ten gracz zgarnia 2 zwojów!*"
        "\n\nDodatkowe statystyki:\n🔺 Drop zwiększony o 20 %"
        f"\n🔺 Bossem będzie gracz <@{patron.id}>"
        "\n\n*Inny gracz może dołączyć do polowania, klikając „Dołącz do polowania” albo wpisując "
        "natychmiast !polowanie*.")


async def test_mob_image_when_the_chosen_player_left_the_server(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda low, high: 0)
    await modifiers.modify_modifiers(game.bot, "player_id", 424242)
    assert await daily.send_mob_image(game.scene(), 0) == (False, None)
    picture, card = game.channel.messages[-2:]
    assert picture.files == ["0.png"] and card.embeds[0].title == "💀 Zwykły mob! 💀"
    assert "zwojów" not in card.embeds[0].description


async def test_mob_image_without_a_picture_file(game, monkeypatch, tmp_path):
    monkeypatch.setattr(daily, "ASSETS_DIR", tmp_path)
    before = len(game.channel.messages)
    assert await daily.send_mob_image(game.scene(), 0) == (False, None)
    assert len(game.channel.messages) == before + 1 and game.channel.messages[-1].embeds


# -- loot ----------------------------------------------------------------------------------

async def test_loot_grants_every_kind_of_item(game_db, loot):
    bot = game_db.bot
    hunter = game_db.add_member("O'Brien")
    loot(Normal=[SHARD, EGG, SCROLL, LEVEL, STONE, MIRROR, TEXT_ONLY])
    message = await daily.rand_loot(game_db.scene(), "0", hunter, 0)
    # (No LEVEL line: an egg and a level for it never come in one drop - owner's rule of 2026-11-21.)
    assert message == "".join("👉 " + item["descr"] + "\n"
                              for item in (SHARD, EGG, SCROLL, STONE, MIRROR, TEXT_ONLY))
    embed = game_db.channel.messages[-1].embeds[0]
    assert embed.title == "Drop - O'Brien" and embed.description == "Otrzymałeś:\n" + message
    assert embed.footer.text == "Gratulacje!" and embed.colour.value == 0xFCDB03
    assert embed.url == embed.thumbnail.url == "https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png"

    row = await owner_row(bot, hunter.id)
    assert (row["reroll_scroll_shard"], row["reroll_scroll"], row["rebirth_stones"], row["mirrors"]) == (1, 1, 1, 1)
    assert row["pet_id"] > 0
    assert await bot.db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = $1", row["pet_id"]) == 0   # still an egg

    # A second egg is not given to a player who already has a pet: no line, no second pet.
    loot(Normal=[EGG])
    assert await daily.rand_loot(game_db.scene(), 0, hunter, 0) == ""
    assert await bot.db.fetchval("SELECT count(*) FROM PETS") == 1
    assert game_db.channel.messages[-1].embeds[0].description == "Otrzymałeś:\n"


async def test_an_egg_and_a_level_never_come_in_one_drop(game_db, loot, monkeypatch):
    """Owner's rule of 2026-11-21: a brand-new egg does not hatch in the drop it came from."""
    bot = game_db.bot
    hunter = game_db.add_member("Ala")

    async def level() -> int:
        row = await owner_row(bot, hunter.id)
        return await bot.db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = $1", row["pet_id"])

    # Certain egg, certain level and a certain bonus level (weight 200 %): the egg only.
    monkeypatch.setattr(random, "random", lambda: 0.0)
    loot(Epic=[EGG, {**LEVEL, "weight": 100}])
    assert await daily.rand_loot(game_db.scene(), 2, hunter, 100) == "👉 " + EGG["descr"] + "\n"
    assert await level() == 0 and await bot.db.fetchval("SELECT count(*) FROM PETS") == 1

    # The next drop: the egg is no longer new - the level is granted (the second egg is not).
    assert await daily.rand_loot(game_db.scene(), 2, hunter, 0) == "👉 " + LEVEL["descr"] + "\n"
    assert await level() == 1 and await bot.db.fetchval("SELECT count(*) FROM PETS") == 1

    # The level listed BEFORE the egg, for a player without a pet: nothing to level, then the egg.
    other = game_db.add_member("Ola")
    loot(Epic=[LEVEL, EGG])
    assert await daily.rand_loot(game_db.scene(), 2, other, 0) == "👉 " + EGG["descr"] + "\n"
    row = await owner_row(bot, other.id)
    assert await bot.db.fetchval("SELECT PET_LVL FROM PETS WHERE PET_ID = $1", row["pet_id"]) == 0


async def test_loot_weights_boost_and_bonus(game_db, loot, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    rolls = iter([0.99, 0.5, 0.5])
    monkeypatch.setattr(random, "random", lambda: next(rolls))
    loot(Rare=[{**SHARD, "weight": 60}, {**SCROLL, "weight": 80}])
    # Boost 49.9 counts as 49 %: shard 89.4 % (roll 99 misses), scroll 119.2 % (certain, bonus roll 50 > 19.2).
    assert await daily.rand_loot(game_db.scene(), 1, hunter, 49.9) == "👉 Dostałeś zwój.\n"
    row = await owner_row(bot, hunter.id)
    assert (row["reroll_scroll_shard"], row["reroll_scroll"]) == (0, 1)

    # A bonus drop is a real second item (the legacy bot only printed the line) - but never a second egg.
    monkeypatch.setattr(random, "random", lambda: 0.0)
    loot(Rare=[SHARD, EGG])
    message = await daily.rand_loot(game_db.scene(), 1, hunter, 100)
    assert message == ("👉 " + SHARD["descr"] + "\n" + "👉 " + SHARD["descr"] + " (Bonus)\n"
                       + "👉 " + EGG["descr"] + "\n")
    assert (await owner_row(bot, hunter.id))["reroll_scroll_shard"] == 2
    assert await bot.db.fetchval("SELECT count(*) FROM PETS") == 1


async def test_loot_rows_drop_only_from_their_sources_and_weight_0_never(game, loot, monkeypatch):
    """(Items, 2026-11-26) a row's ``"sources"`` and ``"boost": false``; no die is rolled for a row that
    cannot drop, so the draws of the other rows stay what they were."""
    rolls: list[float] = []
    monkeypatch.setattr(random, "random", lambda: rolls.append(0.0) or 0.0)    # every row that is rolled drops
    hunter = game.add_member("Ala")
    loot(Epic=[{**TEXT_ONLY, "descr": "Wszędzie"}, {**TEXT_ONLY, "descr": "Zero", "weight": 0},
               {**TEXT_ONLY, "descr": "Świat", "sources": ["world"]},
               {**TEXT_ONLY, "descr": "Pojedynek", "sources": ["duel", "boss"]},
               {**TEXT_ONLY, "descr": "Bez wzmocnienia", "weight": 60, "boost": False}])
    assert await daily.rand_loot(game.scene(), 2, hunter, 100) == (
        "👉 Wszędzie\n👉 Wszędzie (Bonus)\n👉 Świat\n👉 Świat (Bonus)\n👉 Bez wzmocnienia\n")
    assert len(rolls) == 3                                     # two bonus rolls and the 60 % (no bonus: 60, not 120)
    rolls.clear()
    with items.loot_source(items.SOURCE_DUEL):                 # a walkover, a tournament's lone player
        assert await daily.rand_loot(game.scene(), 2, hunter, 0) == "👉 Wszędzie\n👉 Pojedynek\n👉 Bez wzmocnienia\n"
    assert len(rolls) == 3                                     # "Zero" and "Świat" (not from a duel): no die
    assert items.LOOT_SOURCE.get() is None


async def test_a_new_item_is_granted_by_the_items_module_and_hinted(game, loot, monkeypatch):
    """Ids 16-25 go to ``items.grant_drop`` (its "no" hides the line); a reserved id stays a text-only drop."""
    given: list = []
    answer = [True]

    async def grant_drop(bot, hunter, loot_id):
        given.append((hunter.id, loot_id))
        return answer[0]

    monkeypatch.setattr(items, "grant_drop", grant_drop)
    hunter = game.add_member("Ala")
    rope = {"id": 22, "descr": "🪢 **Lina sztygara**!", "weight": 100}
    loot(Rare=[rope, {"id": 21, "descr": "List gończy", "weight": 100}])
    assert await daily.rand_loot(game.scene(), 1, hunter, 0) == (
        f"👉 🪢 **Lina sztygara**!\n{items.DROP_HINT}\n👉 List gończy\n")
    assert given == [(hunter.id, 22)]
    answer[0] = False                                          # the store is not there: no line
    assert await daily.rand_loot(game.scene(), 1, hunter, 0) == "👉 List gończy\n"
    monkeypatch.setattr(random, "random", lambda: 0.0)         # a bonus copy: granted, no second hint
    answer[0] = True
    loot(Rare=[{**rope, "weight": 60}])
    assert await daily.rand_loot(game.scene(), 1, hunter, 100) == (
        f"👉 🪢 **Lina sztygara**!\n{items.DROP_HINT}\n👉 🪢 **Lina sztygara**! (Bonus)\n")
    assert given[-2:] == [(hunter.id, 22), (hunter.id, 22)]


async def test_loot_item_that_fails_does_not_cost_the_others(game_db, loot, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")

    async def broken(scene, number, player_id):
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(pets, "assign_shard", broken)
    loot(Epic=[SHARD, SCROLL])
    assert await daily.rand_loot(game_db.scene(), 9, hunter, 0) == "👉 Dostałeś zwój.\n"   # 9 -> the epic table
    assert any("Nie udało się przyznać łupu graczowi Ala" in line and "Fragment zwoju" in line
               and "DatabaseUnavailable" in line for line in bot.owner_log)
    assert (await owner_row(bot, hunter.id))["reroll_scroll"] == 1

    # The drop embed cannot be posted: the loot stays granted and the owner gets the list.
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("discord broke"), 1
    assert await daily.rand_loot(game_db.scene(), 2, hunter, 0) == "👉 Dostałeś zwój.\n"
    assert (await owner_row(bot, hunter.id))["reroll_scroll"] == 2
    assert any("Nie udało się pokazać łupu gracza Ala" in line and "Dostałeś zwój" in line
               for line in bot.owner_log)


# -- the hunt: winning ---------------------------------------------------------------------

async def test_solo_hunt_won(game_db, loot, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("O'Brien")
    loot(Rare=[SHARD])
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 3)
    limits = spy_on_rounds(monkeypatch)
    game_db.auto_answer(hunter)
    assert await daily.hunt_mobs(game_db.scene(hunter), 1, False, None) is True

    channel = game_db.channel
    assert channel.contains(
        f"Polowanie się rozpoczyna <@{hunter.id}> <:REEeee:790963160495947856>! Wpisz pojawiające się słowa "
        "tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez spacji! Przygotuj się!")
    assert channel.contains("Uwaga!!! 3... 2... 1...")
    asked = prompts(channel)
    assert len(asked) == 3 and all(re.match(rf"^{n}\. \*\*O'Brien\*\*: ", p) for n, p in enumerate(asked, 1))
    assert all(requested_word(p) in dict(daily.MOB_ACTIONS) for p in asked)
    assert limits == [15, 4.0, 4.0]
    assert channel.contains(f"Brawo <@{hunter.id}> <:ok:990161663053422592> Potwór pokonany!")
    assert re.search(r"Zabicie potwora zajęło: \d+\.\d\d sekundy! Jedna tura zajęła średnio \d+\.\d\d sekundy!",
                     channel.transcript)
    assert channel.messages[-1].embeds[0].title == "Drop - O'Brien"

    assert tuple(await bot.db.fetchrow("SELECT NICK, POINTS FROM RANKING WHERE ID = $1",
                                       str(hunter.id))) == ("O'Brien", 3)
    assert await bot.db.fetchval("SELECT HUNTS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1
    assert (await owner_row(bot, hunter.id))["reroll_scroll_shard"] == 1
    assert bot.owner_log == []                       # hunts are not reported to the owner (legacy)
    assert not bot.state.busy


async def test_party_hunt_won(game_db, monkeypatch):
    bot = game_db.bot
    bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    for role_id, name in achievements.RETIRED_ROLES.items():       # (still on a server that was not tidied)
        game_db.guild.add_role(role_id, name)
    ala = game_db.add_member("Ala")
    ola = game_db.add_member("Ola")
    ela = game_db.add_member("Ela", roles=[patrons.CRAFTER_ROLE])
    roles_before = {hunter.id: list(hunter.roles) for hunter in (ala, ola, ela)}
    outsider = game_db.add_member("Obcy")
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 5)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 4)
    take_turns(monkeypatch)
    team_answers(game_db, [ala, ola, ela])
    real_loot, boosts = daily.rand_loot, {}

    async def loot_spy(scene, rarity, hunter, boost):
        boosts[hunter.id] = (rarity, boost)
        return await real_loot(scene, rarity, hunter, boost)

    monkeypatch.setattr(daily, "rand_loot", loot_spy)
    other_channel = FakeChannel(555, game_db.guild, bot.fake_user, name="inny")
    game_db.say_later(ala, "!polowanie", 0.005)                       # already hunting: ignored
    game_db.say_later(outsider, "!polowanie", 0.01, channel=other_channel)   # another channel: ignored
    game_db.say_later(outsider, "!pol", 0.015)                        # only the full word joins
    game_db.say_later(ola, "!polowanie", 0.02)
    game_db.say_later(ela, " $POLOWANIE ", 0.04)

    assert await daily.hunt_mobs(game_db.scene(ala), 2, False, None) is True
    channel = game_db.channel
    mentions = f"<@{ala.id}> <@{ola.id}> <@{ela.id}> "
    assert channel.contains(
        "Rozpoczęliście polowanie " + mentions + "<:REEeee:790963160495947856>! Wpiszcie **słowa przypisane "
        "do Was** tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpiszcie słowa bez spacji! "
        "Przygotujcie się!")
    assert [re.match(r"^\d+\. \*\*(.+?)\*\*", p).group(1) for p in prompts(channel)] == ["Ala", "Ola", "Ela", "Ala"]
    assert channel.contains("Brawo " + mentions + "<:ok:990161663053422592> Potwór pokonany!")

    assert not daily.is_gathering(channel)

    for hunter in (ala, ola, ela):
        assert await bot.db.fetchval("SELECT POINTS FROM RANKING WHERE ID = $1", str(hunter.id)) == 6
        assert await bot.db.fetchval("SELECT HUNTS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1
        assert boosts[hunter.id] == (2, 25.0)          # 5 % from the shrine + 10 % per extra hunter
        assert channel.contains(f"Drop - {hunter.name}")
    assert await bot.db.fetchval("SELECT count(*) FROM RANKING") == 3
    # The unique monster gives no collector's role any more (withdrawn on 2026-11-20), not even to a supporter.
    assert {hunter.id: list(hunter.roles) for hunter in (ala, ola, ela)} == roles_before
    assert not channel.contains("zdobywa osiągnięcie")


async def test_debug_mode_lets_one_tester_fill_the_party(game_db, monkeypatch):
    bot = game_db.bot
    assert bot.debug
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    ala = game_db.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)
    game_db.auto_answer(ala)
    game_db.say_later(ala, "!polowanie", 0.01)
    game_db.say_later(ala, "!polowanie", 0.02)
    assert await daily.hunt_mobs(game_db.scene(ala), 0, False, None) is True
    assert game_db.channel.contains(f"Rozpoczęliście polowanie <@{ala.id}> <@{ala.id}> <@{ala.id}> ")


async def test_hunt_started_by_an_event_for_a_player_who_left_meanwhile(game_db, monkeypatch):
    """Events pass the player explicitly (their scene has no author); a leaver is still rewarded."""
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)
    game_db.auto_answer(hunter)
    game_db.guild.members.remove(hunter)                 # left the server after reacting
    assert await daily.hunt_mobs(game_db.scene(), 1, False, None, hunter) is True
    assert tuple(await bot.db.fetchrow("SELECT NICK, POINTS FROM RANKING WHERE ID = $1",
                                       str(hunter.id))) == (str(hunter.id), 3)
    assert await bot.db.fetchval("SELECT HUNTS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1

    with pytest.raises(ValueError):
        await daily.hunt_mobs(game_db.scene(), 1, False, None)


async def test_won_hunt_stays_won_when_bookkeeping_fails(game_db, loot, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    loot(Normal=[SHARD])
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)

    async def no_ranking(*args, **kwargs):
        raise ConnectionError("database gone")

    monkeypatch.setattr(database, "update_ranking_table", no_ranking)
    original = game_db.channel.send

    async def no_cheering(content=None, **kwargs):
        if content and "Brawo" in content:
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game_db.channel.send = no_cheering
    game_db.auto_answer(hunter)
    assert await daily.hunt_mobs(game_db.scene(hunter), 0, False, None) is True
    assert any("po wygranym polowaniu (ogłoszenie zwycięstwa)" in line and "RuntimeError" in line
               for line in bot.owner_log)
    assert any("punkty rankingowe" in line and "ConnectionError" in line for line in bot.owner_log)
    # Everything else was still handed out.
    assert await bot.db.fetchval("SELECT HUNTS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1
    assert (await owner_row(bot, hunter.id))["reroll_scroll_shard"] == 1
    assert game_db.channel.contains("Drop - Ala")


# -- the hunt: losing ----------------------------------------------------------------------

async def test_mistake_loses_pays_the_player_monster_and_bans(game_db, monkeypatch):
    bot = game_db.bot
    bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    patron = game_db.add_member("Patron")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 5)
    take_turns(monkeypatch)
    team_answers(game_db, [ala, ola], wrong_rounds={2})
    game_db.say_later(ola, "!polowanie", 0.01)

    assert await daily.hunt_mobs(game_db.scene(ala), 1, True, patron) is False
    assert game_db.channel.messages[-1].content == (
        "Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie jutro lub poczekaj na modlitwę "
        "wspierających! <:RIP:912797982917816341>\n\n*Możesz również dołączyć do polowania innych graczy.*")
    assert len(prompts(game_db.channel)) == 2
    # The player who posed as the monster gets rarity + 1 scrolls.
    assert (await owner_row(bot, patron.id))["reroll_scroll"] == 2
    # "Ban the loser": the whole party is dead until the next boss.
    dead = bot.role(bot.ids.dead_hunter_role)
    assert dead in ala.roles and dead in ola.roles and dead not in patron.roles
    assert [m.content for m in game_db.notice_channel.messages] == [
        f"Na kanale <#970684202880204831> poległeś z ręki potwora <@{hunter.id}>, a groziło to banem... "
        "<:RIP:912797982917816341> Musisz odpocząć od przygód aż do momentu zabicia następnego bossa."
        for hunter in (ala, ola)]
    assert await bot.db.fetchval("SELECT count(*) FROM RANKING") == 0
    assert bot.owner_log == []


async def test_timeout_loses(game, skills, short_timeouts):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, silent_on_round=2)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is False
    assert game.channel.messages[-1].content == (
        "Niestety nie zdążyłeś! <:Bedge:970576892874854400> Odpocznij i spróbuj jutro! "
        "<:RIP:912797982917816341>\n\n*Możesz również dołączyć do polowania innych graczy.*")
    assert bot.role(bot.ids.dead_hunter_role) not in hunter.roles     # no "ban the loser" modifier
    assert game.notice_channel.messages == [] and bot.owner_log == []


async def test_answer_of_the_wrong_hunter_is_a_mistake(game, skills, monkeypatch):
    game.bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    take_turns(monkeypatch)
    team_answers(game, [ala, ola], intruder_rounds={1})
    game.say_later(ola, "!polowanie", 0.01)
    assert await daily.hunt_mobs(game.scene(ala), 0, False, None) is False
    assert game.channel.messages[-1].content == daily.MSG_MISTAKE_LOST


async def test_player_monster_cannot_farm_scrolls_by_losing_to_himself(game_db):
    bot = game_db.bot
    cheat = game_db.add_member("Cwaniak")
    game_db.auto_answer(cheat, wrong_on_round=1)
    assert await daily.hunt_mobs(game_db.scene(cheat), 2, True, cheat) is False
    assert await owner_row(bot, cheat.id) is None

    # Paying the monster's player fails: the hunt is still simply lost and the owner is told.
    victim, patron = game_db.add_member("Ala"), game_db.add_member("Patron")

    async def no_scrolls(scene, number, player_id):
        raise DatabaseUnavailable("database gone")

    game_db.channel.send_hooks.clear()
    game_db.auto_answer(victim, wrong_on_round=1)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pets, "assign_scroll", no_scrolls)
        assert await daily.hunt_mobs(game_db.scene(victim), 0, True, patron) is False
    assert any(f"po przegranym polowaniu (1 zwojów dla gracza-potwora <@{patron.id}>)" in line
               for line in bot.owner_log)


# -- the hunt: pets, modifiers, technical problems -----------------------------------------

async def test_pet_skills_in_a_hunt(game, skills, monkeypatch, short_timeouts):
    hunter = game.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 7)
    # 7 hp lowered by 20 % -> 5; every hit is critical and counts twice.
    skills[hunter.id] = {"DEF_PERC": 100, "CRIT_PERC": 100, "LOWHP_PERC": 20}
    game.auto_answer(hunter, responder=_answers(wrong={1}, silent={2}))
    monkeypatch.setattr(daily, "rand_loot", _no_loot)
    monkeypatch.setattr(database, "update_ranking_table", _nothing)
    monkeypatch.setattr(achievements, "add_hunts", _nothing)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is True
    assert game.channel.contains("Pomyliłeś się, ale Twój towarzysz Cię chroni!")
    assert game.channel.contains("Nie zdążyłeś, ale Twój towarzysz Cię chroni!")
    assert [p.split(".")[0] for p in prompts(game.channel)] == ["1", "2", "3", "5"]

    game.channel.sent.clear()
    game.channel.send_hooks.clear()
    skills[hunter.id] = {"REPLACE_PERC": 100}
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is True
    pet_lines = prompts(game.channel)
    assert len(pet_lines) == 7 and all(line.startswith("~~") for line in pet_lines)   # 7 hp, no crit now
    assert re.match(r"^~~1\. Ala: .+\*\*~~\. Twój towarzysz wyprowadza atak!$", pet_lines[0])


async def test_time_limits_follow_rarity_modifiers_and_the_pet(game, skills, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    await modifiers.modify_modifiers(bot, "time_reduced_perc", 20)
    await modifiers.modify_modifiers(bot, "time_increased_perc", 40)     # ignored by hunts, as in legacy
    await modifiers.modify_modifiers(bot, "hp_boost_perc", 50)
    await modifiers.modify_modifiers(bot, "hp_reduced_perc", 10)
    skills[hunter.id] = {"SLOW_PERC": 10, "LOWHP_PERC": 15}
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 4)
    monkeypatch.setattr(daily, "rand_loot", _no_loot)
    monkeypatch.setattr(database, "update_ranking_table", _nothing)
    monkeypatch.setattr(achievements, "add_hunts", _nothing)
    limits = spy_on_rounds(monkeypatch)
    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(hunter), 1, False, None) is True
    # hp: 4 * (100 + 50 - 10 - 15) % = 5; time: (5 - 1) s * (100 - 20 + 10) % = 3.6 s
    assert limits == pytest.approx([15, 3.6, 3.6, 3.6, 3.6])


async def test_disrupted_round_is_repeated_without_punishment(game, skills, monkeypatch):
    hunter = game.add_member("Ala")
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 3)
    monkeypatch.setattr(daily, "rand_loot", _no_loot)
    monkeypatch.setattr(database, "update_ranking_table", _nothing)
    monkeypatch.setattr(achievements, "add_hunts", _nothing)
    real_ask = challenge.ask
    calls = itertools.count(1)

    async def flaky(bot, channel, prompt, **kwargs):
        if next(calls) in (2, 3):                         # two lost rounds in a row
            return RoundResult(Outcome.DISRUPTED)
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", flaky)
    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is True
    assert game.channel.count(challenge.MSG_DISRUPTED) == 2
    assert [p.split(".")[0] for p in prompts(game.channel)] == ["1", "2", "3"]


async def test_repeated_disruptions_end_the_hunt_without_consequences(game_db, monkeypatch):
    bot = game_db.bot
    hunter, patron = game_db.add_member("Ala"), game_db.add_member("Patron")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", dead_line)
    assert await daily.hunt_mobs(game_db.scene(hunter), 1, True, patron) is None
    assert game_db.channel.count(challenge.MSG_DISRUPTED) == challenge.MAX_CONSECUTIVE_DISRUPTIONS - 1
    assert game_db.channel.messages[-1].content == daily.MSG_HUNT_INTERRUPTED
    assert await owner_row(bot, patron.id) is None                     # no scrolls for the monster
    assert bot.role(bot.ids.dead_hunter_role) not in hunter.roles      # nobody is banned
    assert await bot.db.fetchval("SELECT count(*) FROM RANKING") == 0


async def test_database_failure_before_the_fight_propagates(game, monkeypatch):
    hunter = game.add_member("Ala")

    async def no_database(bot, player_id):
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(pets, "get_pet_skills", no_database)
    with pytest.raises(DatabaseUnavailable):
        await daily.hunt_mobs(game.scene(hunter), 0, False, None)
    assert not game.channel.contains("Błąd bazy danych")               # the db layer retries, not the hunt


# -- the hunt: the Nieuchwytny pył (owner, 2026-11-26: features/items.py) ---------------------

def dust_line(name: str, *, timed_out: bool = False) -> str:
    return (("Nie zdążyłeś" if timed_out else "Pomyliłeś się") + f", ale **Nieuchwytny pył** gracza **{name}** "
            "rozwiewa się w powietrzu - nic się nie stało! ✨")


async def test_hunt_dust_of_the_hunter_at_fault_used_up_also_without_progress(game_db, monkeypatch):
    """The Mroczny las calls ``hunt_mobs`` without ``progress``: the dust leaves the items all the same."""
    bot = game_db.bot
    bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    for holder in (ala, ola):
        await items.grant(bot, holder.id, items.DUST)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 3)
    take_turns(monkeypatch)
    team_answers(game_db, [ala, ola], intruder_rounds={2})            # round 2 is Ola's, Ala types it
    game_db.say_later(ola, "!polowanie", 0.02)
    assert await daily.hunt_mobs(game_db.scene(ala), 0, False, None) is True
    assert game_db.channel.count(dust_line("Ala")) == 1                # the mistake was Ala's: her dust
    assert len(prompts(game_db.channel)) == 4                          # one word more for the monster
    fight = fightlog.last_fight(ala.id)
    assert (fight.rounds[1].outcome, fight.rounds[1].saved) == (fightlog.Outcome.WRONG_PLAYER,
                                                                 fightlog.SAVED_BY_DUST)
    assert await items.count(bot, ala.id, items.DUST) == 0
    assert await items.count(bot, ola.id, items.DUST) == 1


async def test_a_lost_hunt_uses_the_dust_up_and_an_interrupted_one_takes_none(game_db, monkeypatch,
                                                                             short_timeouts):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await items.grant(bot, hunter.id, items.DUST, 2)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 5)
    game_db.auto_answer(hunter, responder=_answers(wrong={1}, silent={2}))
    progress = {"decided": False}
    assert await daily.hunt_mobs(game_db.scene(hunter), 0, False, None, progress=progress) is False
    assert game_db.channel.count(dust_line("Ala")) == 1                # once per hunt: the missed word loses
    assert game_db.channel.messages[-1].content == daily.MSG_TOO_SLOW_LOST and progress["decided"]
    assert await items.count(bot, hunter.id, items.DUST) == 1

    # Interrupted after the dust saved him: nobody lost or won - nothing is taken.
    game_db.channel.send_hooks.clear()
    real_ask = challenge.ask
    calls = itertools.count(1)

    async def lag_after_the_first_word(bot, channel, prompt, **kwargs):
        if next(calls) > 1:
            return RoundResult(Outcome.DISRUPTED)
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", lag_after_the_first_word)
    game_db.auto_answer(hunter, wrong_on_round=1)
    assert await daily.hunt_mobs(game_db.scene(hunter), 0, False, None) is None
    assert game_db.channel.count(dust_line("Ala")) == 2
    assert await items.count(bot, hunter.id, items.DUST) == 1


# -- the hunt: changed commands from the shrine --------------------------------------------

#: The prompts of the changed commands as a hunt must show them: the foe is the monster.
CHANGED_PROMPTS = {word: prompt.replace("{foe}", "Potwór") for word, prompt in modifiers.CHANGED_ACTIONS}
CHANGED_WORDS = [word for word, _ in modifiers.CHANGED_ACTIONS]


def cycle_actions(monkeypatch) -> list[tuple[int, int]]:
    """``random.randint`` walks its range in order, so every word gets asked; records the ranges asked for."""
    ranges: list[tuple[int, int]] = []
    counter = itertools.count()

    def randint(low, high):
        ranges.append((low, high))
        return low + next(counter) % (high - low + 1)

    monkeypatch.setattr(random, "randint", randint)
    return ranges


def no_bookkeeping(monkeypatch) -> None:
    monkeypatch.setattr(daily, "rand_loot", _no_loot)
    monkeypatch.setattr(database, "update_ranking_table", _nothing)
    monkeypatch.setattr(achievements, "add_hunts", _nothing)


async def test_hunt_with_changed_commands_asks_only_for_the_new_words(game, skills, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    assert await daily.send_mob_image(game.scene(), 0) == (False, None)
    assert "\n🔀 Komendy w walce są zmienione" in game.channel.messages[-1].embeds[0].description

    ranges = cycle_actions(monkeypatch)
    no_bookkeeping(monkeypatch)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2 * len(CHANGED_WORDS))   # every word twice
    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(hunter), 1, False, None) is True

    asked = prompts(game.channel)
    assert asked == [f"{n}. **Ala**: {CHANGED_PROMPTS[CHANGED_WORDS[(n - 1) % 13]]}" for n in range(1, 27)]
    assert set(ranges) == {(0, 12)}                       # drawn from the 13 new words only, never a mix
    assert not any(requested_word(p) in dict(daily.MOB_ACTIONS) for p in asked)
    assert not any("Boss" in p or "{" in p or "}" in p for p in asked)
    assert sum("Potwór" in p for p in asked) == 18        # 9 of the 13 prompts name the foe
    assert game.channel.contains(f"Brawo <@{hunter.id}> <:ok:990161663053422592> Potwór pokonany!")


async def test_party_hunt_with_changed_commands(game, skills, monkeypatch):
    bot = game.bot
    bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    ranges = cycle_actions(monkeypatch)
    no_bookkeeping(monkeypatch)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: len(CHANGED_WORDS) + 1)
    take_turns(monkeypatch)
    team_answers(game, [ala, ola])
    game.say_later(ola, "!polowanie", 0.01)

    assert await daily.hunt_mobs(game.scene(ala), 2, False, None) is True
    names = ["Ala", "Ola"]
    words = CHANGED_WORDS + CHANGED_WORDS[:1]
    assert prompts(game.channel) == [f"{n}. **{names[(n - 1) % 2]}**: {CHANGED_PROMPTS[word]}"
                                     for n, word in enumerate(words, 1)]
    assert set(ranges) == {(0, 12)}
    assert game.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> <:ok:990161663053422592> Potwór pokonany!")


async def test_hunt_without_changed_commands_keeps_the_usual_words(game, skills, monkeypatch):
    hunter = game.add_member("Ala")
    assert (await modifiers.load_modifiers(game.bot))[modifiers.COMMANDS_CHANGED] == 0
    ranges = cycle_actions(monkeypatch)
    no_bookkeeping(monkeypatch)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: len(daily.MOB_ACTIONS))
    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is True
    assert prompts(game.channel) == [f"{n}. **Ala**: {prompt}" for n, (_, prompt) in enumerate(daily.MOB_ACTIONS, 1)]
    assert set(ranges) == {(0, 14)}
    assert not any(requested_word(p) in CHANGED_PROMPTS for p in prompts(game.channel))


async def test_hunts_do_not_switch_the_changed_commands_off(game, skills, monkeypatch):
    """Only the end of the boss fight resets the modifiers: won or lost, every hunt until then is changed."""
    bot = game.bot
    hunter = game.add_member("Ala")
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    await modifiers.modify_modifiers(bot, "drop_boost_perc", 5)
    before = await modifiers.load_modifiers(bot)
    cycle_actions(monkeypatch)
    no_bookkeeping(monkeypatch)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 3)

    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is True
    assert await modifiers.load_modifiers(bot) == before
    assert "🔀 Komendy w walce są zmienione" in await modifiers.load_desc_modifiers(bot)

    game.channel.sent.clear()
    game.channel.send_hooks.clear()
    game.auto_answer(hunter, wrong_on_round=2)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is False
    asked = prompts(game.channel)
    assert len(asked) == 2 and all(requested_word(p) in CHANGED_PROMPTS for p in asked)
    assert await modifiers.load_modifiers(bot) == before


# -- !polowanie: the whole daily hunt ------------------------------------------------------

async def test_daily_hunt_won_and_lost_keep_the_cooldown(game_db, monkeypatch):
    bot = game_db.bot
    monkeypatch.setattr(random, "randint", lambda low, high: low)     # normal monster, 3 hp, picture 0
    winner, loser = game_db.add_member("Ala"), game_db.add_member("Ola")
    game_db.auto_answer(winner)
    assert await daily.daily_hunt(game_db.scene(winner)) is True
    assert daily.load_daily_from_file(bot, winner.id)
    assert any(m.files == ["0.png"] for m in game_db.channel.messages)
    assert game_db.channel.contains("💀 Zwykły mob! 💀") and len(prompts(game_db.channel)) == 3
    assert await bot.db.fetchval("SELECT POINTS FROM RANKING WHERE ID = $1", str(winner.id)) == 1

    game_db.channel.send_hooks.clear()
    game_db.auto_answer(loser, wrong_on_round=1)
    assert await daily.daily_hunt(game_db.scene(loser)) is False
    assert daily.load_daily_from_file(bot, loser.id)
    assert not game_db.channel.contains("możesz zapolować ponownie")


async def test_daily_hunt_gives_the_attempt_back_after_a_technical_failure(game, skills, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    original = game.channel.send

    async def broken_on_second_prompt(content=None, **kwargs):
        if content and content.startswith("2. "):
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game.channel.send = broken_on_second_prompt
    with pytest.raises(RuntimeError):
        await daily.daily_hunt(game.scene(hunter))
    assert not daily.load_daily_from_file(bot, hunter.id)
    assert game.channel.messages[-1].content == (
        f"<@{hunter.id}>, z powodu problemów technicznych Twoje dzisiejsze polowanie się nie liczy - "
        "możesz zapolować ponownie!")


async def test_daily_hunt_gives_the_attempt_back_after_disruptions_and_on_shutdown(game, skills, monkeypatch):
    bot = game.bot
    hunter = game.add_member("Ala")

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(challenge, "ask", dead_line)
        assert await daily.daily_hunt(game.scene(hunter)) is None
    assert not daily.load_daily_from_file(bot, hunter.id)
    assert game.channel.messages[-2].content == daily.MSG_HUNT_INTERRUPTED
    assert game.channel.messages[-1].content == daily.MSG_ATTEMPT_RETURNED.format(user_id=hunter.id)

    # The bot is stopped in the middle of a hunt: the attempt is not lost either.
    task = asyncio.ensure_future(daily.daily_hunt(game.scene(hunter)))
    await wait_until(lambda: len(prompts(game.channel)) == 1)
    assert daily.load_daily_from_file(bot, hunter.id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not daily.load_daily_from_file(bot, hunter.id)


# -- !przyneta: the daily hunt with a bait (items_decisions.md §4) ---------------------------

async def baits(bot, player) -> int | None:
    value = await bot.db.fetchval("SELECT COUNT FROM PLAYER_ITEMS WHERE PLAYER_ID = $1 AND ITEM = 'bait'", player.id)
    return None if value is None else int(value)


def monster_card(game) -> discord.Embed:
    return next(m for m in reversed(game.channel.messages) if m.embeds and m.embeds[0].title.startswith("💀")).embeds[0]


@pytest.mark.parametrize("rolled, fought, title", [(0, 1, "💀 Rzadki mob! 💀"), (1, 2, "💀 Unikalny mob! 💀")])
async def test_a_bait_lures_the_monster_one_step_up_and_is_used_once(game_db, skills, monkeypatch, rolled, fought,
                                                                    title):
    bot = game_db.bot
    bot.debug = False
    ala = game_db.add_member("Ala")
    await items.grant(bot, ala.id, "bait", 2)
    monkeypatch.setattr(daily, "roll_hunt_rarity", lambda: rolled)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)
    real_hunt, fights = daily.hunt_mobs, []

    async def hunt_spy(scene, rarity, *args, **kwargs):
        fights.append(rarity)
        return await real_hunt(scene, rarity, *args, **kwargs)

    monkeypatch.setattr(daily, "hunt_mobs", hunt_spy)
    game_db.auto_answer(ala)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is True
    card = monster_card(game_db)
    assert card.title == title and fights == [fought]                        # every hunter fights the rarer one
    assert card.description.startswith(f"Na polowaniu pojawił się {'rzadki' if fought == 1 else 'UNIKALNY'} "
                                       "potwór! ⚔️\n\n🪤 Przynęta zwabiła z puszczy groźniejszą bestię!")
    assert await baits(bot, ala) == 1 and daily.load_daily_from_file(bot, ala.id)   # the starter's attempt
    assert game_db.channel.contains(f"Brawo <@{ala.id}> <:ok:990161663053422592> Potwór pokonany!")
    assert not bot.state.busy and bot.owner_log == []


@pytest.mark.parametrize("rolled, note", [(0, daily.MSG_BAITED), (2, daily.MSG_BAIT_NOT_NEEDED)],
                         ids=["baited", "not-needed"])
async def test_a_baited_hunt_keeps_its_line_when_a_player_poses_as_the_monster(game_db, skills, monkeypatch,
                                                                             rolled, note):
    """The shrine's player-monster line is added after the bait's line, never instead of it."""
    bot = game_db.bot
    bot.debug = False
    ala, patron = game_db.add_member("Ala"), game_db.add_member("Patron")
    await items.grant(bot, ala.id, "bait")
    await modifiers.modify_modifiers(bot, "player_id", patron.id)
    monkeypatch.setattr(daily, "roll_hunt_rarity", lambda: rolled)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)
    game_db.auto_answer(ala)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is True
    card = monster_card(game_db)
    fought = min(rolled + 1, 2)
    assert card.title == f"💀 {'Rzadki' if fought == 1 else 'Unikalny'} Patron! 💀"
    assert card.description.startswith(
        f"Na polowaniu pojawił się {'rzadki' if fought == 1 else 'UNIKALNY'} potwór! ⚔️\n\n{note}"
        "\n\n*Potworem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, "
        f"to ten gracz zgarnia {fought + 1} zwojów!*")
    assert await baits(bot, ala) == (0 if rolled == 0 else 1)


async def test_a_unique_roll_needs_no_bait(game_db, skills, monkeypatch):
    bot = game_db.bot
    bot.debug = False
    ala = game_db.add_member("Ala")
    await items.grant(bot, ala.id, "bait")
    monkeypatch.setattr(daily, "roll_hunt_rarity", lambda: 2)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 2)
    game_db.auto_answer(ala)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is True
    card = monster_card(game_db)
    assert card.title == "💀 Unikalny mob! 💀" and daily.MSG_BAIT_NOT_NEEDED in card.description
    assert daily.MSG_BAITED not in card.description and await baits(bot, ala) == 1


async def test_no_bait_no_hunt_and_nothing_is_used(game_db, monkeypatch):
    bot = game_db.bot
    bot.debug = False
    ala = game_db.add_member("Ala")
    hunts: list = []

    async def fake_hunt(scene, **kwargs):
        hunts.append(kwargs)

    monkeypatch.setattr(daily, "daily_hunt", fake_hunt)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is False
    assert game_db.channel.messages[-1].content == (
        f"Nie masz żadnej **Przynęty**, <@{ala.id}>. Zwykle wypada z potworów - albo kupisz ją u Wędrownego kupca.")
    assert hunts == [] and not daily.load_daily_from_file(bot, ala.id) and not bot.state.busy
    # With a bait it is the hunt of !polowanie (the same rules: the boss, the cooldown) with bait=True.
    await items.grant(bot, ala.id, "bait")
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is True and hunts == [{"bait": True}]
    daily.save_daily_to_file(bot, ala.id)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is False
    assert game_db.channel.messages[-1].content == daily.cooldown_text() and len(hunts) == 1
    await daily.remove_player_daily_file(bot, [ala.id])
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is False
    assert game_db.channel.messages[-1].content == daily.MSG_BOSS_HERE and len(hunts) == 1
    # Without the item store nobody has a bait.
    await bot.state.set_boss_phase(BossPhase.WAITING)
    bot._player_items = (False, items.clock.monotonic())
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is False
    assert game_db.channel.messages[-1].content.startswith("Nie masz żadnej **Przynęty**") and len(hunts) == 1


async def test_a_bait_gone_by_the_time_it_is_taken_starts_no_hunt(game_db, skills, monkeypatch):
    """The count before the checks said one; the conditional take inside the busy hold finds none."""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    monkeypatch.setattr(daily, "baits_of", lambda bot, player_id: _value(1))
    monkeypatch.setattr(daily, "roll_hunt_rarity", lambda: 0)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is True
    assert game_db.channel.messages[-1].content.startswith(f"Nie masz żadnej **Przynęty**, <@{ala.id}>.")
    assert not daily.load_daily_from_file(bot, ala.id) and not any(m.embeds for m in game_db.channel.messages)
    assert await baits(bot, ala) is None


async def test_a_bait_the_database_may_or_may_not_have_taken_is_for_the_owner(game_db, skills, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await items.grant(bot, ala.id, "bait")
    monkeypatch.setattr(daily, "roll_hunt_rarity", lambda: 1)

    async def lost_reply(*args, **kwargs):
        raise DatabaseUnavailable("the reply was lost")

    monkeypatch.setattr(items, "take", lost_reply)
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is True
    assert game_db.channel.messages[-1].content == daily.ui.MSG_FAILED
    assert not daily.load_daily_from_file(bot, ala.id) and await baits(bot, ala) == 1   # nothing given on a guess
    assert bot.owner_log == [daily.LOG_BAIT_UNSURE.format(user_id=ala.id, error="DatabaseUnavailable")]


async def test_the_bait_comes_back_with_the_attempt_while_the_hunt_is_undecided(game_db, skills, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await items.grant(bot, hunter.id, "bait")
    monkeypatch.setattr(daily, "roll_hunt_rarity", lambda: 0)

    # Repeated disruptions: the hunt is interrupted - the attempt and the bait come back.
    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(challenge, "ask", dead_line)
        assert await daily.daily_hunt(game_db.scene(hunter), bait=True) is None
    assert not daily.load_daily_from_file(bot, hunter.id) and await baits(bot, hunter) == 1
    assert game_db.channel.messages[-1].content == (daily.MSG_ATTEMPT_RETURNED.format(user_id=hunter.id)
                                                    + daily.MSG_BAIT_RETURNED)

    # A technical failure before the hunt is decided.
    original = game_db.channel.send

    async def broken_on_second_prompt(content=None, **kwargs):
        if content and content.startswith("2. "):
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game_db.auto_answer(hunter)
    game_db.channel.send = broken_on_second_prompt
    with pytest.raises(RuntimeError):
        await daily.daily_hunt(game_db.scene(hunter), bait=True)
    game_db.channel.send = original
    assert not daily.load_daily_from_file(bot, hunter.id) and await baits(bot, hunter) == 1
    assert game_db.channel.messages[-1].content.endswith(daily.MSG_BAIT_RETURNED)

    # The bot stops in the middle of the hunt: the bait comes back as well (the CancelledError path).
    game_db.channel.send_hooks.clear()
    asked = len(prompts(game_db.channel))
    task = asyncio.ensure_future(daily.daily_hunt(game_db.scene(hunter), bait=True))
    await wait_until(lambda: len(prompts(game_db.channel)) > asked)
    assert await baits(bot, hunter) == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not daily.load_daily_from_file(bot, hunter.id) and await baits(bot, hunter) == 1

    # A lost hunt is decided: the bait is used for good, as is the attempt.
    returned = game_db.channel.sent_count(daily.MSG_BAIT_RETURNED)
    game_db.auto_answer(hunter, wrong_on_round=1)
    assert await daily.daily_hunt(game_db.scene(hunter), bait=True) is False
    assert daily.load_daily_from_file(bot, hunter.id) and await baits(bot, hunter) == 0
    assert game_db.channel.sent_count(daily.MSG_BAIT_RETURNED) == returned


async def test_a_bait_that_cannot_be_given_back_is_for_the_owner(game_db, skills, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await items.grant(bot, hunter.id, "bait")
    monkeypatch.setattr(daily, "roll_hunt_rarity", lambda: 0)

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    async def no_grant(*args, **kwargs):
        raise DatabaseUnavailable("down")

    monkeypatch.setattr(challenge, "ask", dead_line)
    monkeypatch.setattr(items, "grant", no_grant)
    assert await daily.daily_hunt(game_db.scene(hunter), bait=True) is None
    assert game_db.channel.messages[-1].content == daily.MSG_ATTEMPT_RETURNED.format(user_id=hunter.id)
    assert bot.owner_log == [daily.LOG_BAIT_NOT_RETURNED.format(user_id=hunter.id, error="DatabaseUnavailable")]


async def test_przyneta_command_rules(game_db, monkeypatch):
    bot = game_db.bot
    bot.debug = False
    await bot.load_extension("altermmo.cogs.hunting")
    command = bot.get_command("przyneta")
    assert command is not None and command.cog_name == "Hunting" and bot.get_command("przynęta") is command
    # ONE check (guards.in_order): discord.py flips the order of stacked checks with every copy of a command.
    assert len(command.checks) == 1
    cog = bot.get_cog("Hunting")
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    starts: list = []
    real_start = daily.start_hunt

    async def fake_start(scene, **kwargs):
        starts.append((scene.author, kwargs))
        return True

    monkeypatch.setattr(daily, "start_hunt", fake_start)
    # Only on the boss channel; silent during a typed fight.
    elsewhere = FakeChannel(555, game_db.guild, bot.fake_user, name="inny")
    with pytest.raises(guards.SilentCheckFailure):
        await run_checks(command, make_ctx(game_db, ala, "!przyneta", channel=elsewhere))
    with challenge.round_running(game_db.channel):
        before = len(game_db.channel.sent)
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, "!przyneta"))
        assert len(game_db.channel.sent) == before
    ctx = make_ctx(game_db, ala, "!przyneta")
    await run_checks(command, ctx)
    await command.callback(cog, ctx)
    assert starts == [(ala, {"bait": True})]

    # Somebody's hunt is gathering hunters: refused with its own text (once per 20 s), not the busy notice.
    daily._gathering[game_db.channel.id] = daily._Gathering(asyncio.Queue(), [ola])
    with bot.state.hold_busy("hunt"):
        for _ in range(2):
            with pytest.raises(guards.SilentCheckFailure):
                await run_checks(command, make_ctx(game_db, ala, "!przyneta"))
    del daily._gathering[game_db.channel.id]
    assert game_db.channel.count(daily.MSG_BAIT_GATHERING.format(user_id=ala.id)) == 1
    assert not game_db.channel.contains(guards.BUSY_NOTICE)
    # Busy with something else: the busy notice, as for every gameplay command.
    guards._last_notice.clear()
    with bot.state.hold_busy("event:PARTY"):
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, "!przyneta"))
    assert game_db.channel.messages[-1].content == guards.BUSY_NOTICE and len(starts) == 1
    # ... and start_hunt says the same when it finds a gathering hunt itself (nobody joins with a bait).
    monkeypatch.setattr(daily, "start_hunt", real_start)
    daily._gathering[game_db.channel.id] = daily._Gathering(asyncio.Queue(), [ola])
    await items.grant(bot, ala.id, "bait")
    assert await daily.start_hunt(game_db.scene(ala), bait=True) is False
    del daily._gathering[game_db.channel.id]
    assert game_db.channel.messages[-1].content == daily.MSG_BAIT_GATHERING.format(user_id=ala.id)
    assert await baits(bot, ala) == 1


# -- the cog -------------------------------------------------------------------------------

async def test_hunting_cog_registers_the_legacy_commands(game):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.hunting")
    command = bot.get_command("polowanie")
    assert command is not None and command.cog_name == "Hunting"
    assert bot.get_command("pol") is command and bot.get_command("p") is command
    assert len(command.checks) == 3 and command._buckets._cooldown is None
    for name in ("ClearDaily", "ClearDailyAlter"):
        admin = bot.get_command(name)
        assert admin is not None and len(admin.checks) == 1
        with pytest.raises(commands.MissingPermissions):
            await run_checks(admin, make_ctx(game, game.add_member("Gracz"), "!" + name))
        await run_checks(admin, make_ctx(game, game.add_member("Admin"), "!" + name, admin=True))


async def test_polowanie_command_rules(game, monkeypatch):
    bot = game.bot
    bot.debug = False
    cog = hunting.HuntingCog(bot)
    command = cog.hunting
    ala = game.add_member("Ala")
    hunts: list = []

    async def fake_hunt(scene):
        hunts.append((scene.author, scene.channel, bot.state.busy_owner))

    monkeypatch.setattr(daily, "daily_hunt", fake_hunt)

    # Only on the boss channel.
    elsewhere = FakeChannel(555, game.guild, bot.fake_user, name="inny")
    with pytest.raises(guards.SilentCheckFailure):
        await run_checks(command, make_ctx(game, ala, channel=elsewhere))

    ctx = make_ctx(game, ala)
    await run_checks(command, ctx)
    await command.callback(cog, ctx)
    assert hunts == [(ala, game.channel, "hunt")] and not bot.state.busy

    # The boss is coming / standing / being fought.
    before = len(game.channel.bot_messages)
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    await command.callback(cog, ctx)
    assert game.channel.messages[-1].content == (
        "Zaraz pojawi się prawdziwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, kliknij "
        "**Zaatakuj** pod bossem albo wpisz **!zaatakuj**, żeby stawić mu czoła.")
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    await command.callback(cog, ctx)
    assert game.channel.messages[-1].content == (
        "Teraz pora na walkę z prawdziwym bossem, nie mieszaj się leszczyku <:madge:882184635474386974>")
    assert len(game.channel.bot_messages) == before + 2
    for phase in (BossPhase.ENGAGED, BossPhase.FIGHTING):
        await bot.state.set_boss_phase(phase)
        await command.callback(cog, ctx)
    assert len(game.channel.bot_messages) == before + 2 and len(hunts) == 1

    # Debug mode skips the boss restriction (legacy DebugMode).
    bot.debug = True
    await command.callback(cog, ctx)
    assert len(hunts) == 2
    bot.debug = False

    # Already hunted today.
    await bot.state.set_boss_phase(BossPhase.WAITING)
    daily.save_daily_to_file(bot, ala.id)
    await command.callback(cog, ctx)
    stamp = int(daily.next_daily_reset().timestamp())
    assert game.channel.messages[-1].content == (
        f"Zregeneruj się i spróbuj zapolować ponownie <t:{stamp}:R> <:Bedge:970576892874854400> Niektóre "
        "modlitwy przy kapliczkach również są w stanie zregenerować Twoje siły, więc bądź czujny!")
    assert len(hunts) == 2

    # Busy with something else: the guard answers with the notice, nothing starts.
    await daily.remove_player_daily_file(bot, [ala.id])
    with bot.state.hold_busy("event:PARTY"):
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, ctx)
        assert game.channel.messages[-1].content == guards.BUSY_NOTICE
        await command.callback(cog, ctx)                  # even past the guard nothing starts
    assert len(hunts) == 2

    # A failing hunt releases the busy flag.
    async def crashing_hunt(scene):
        raise RuntimeError("boom")

    monkeypatch.setattr(daily, "daily_hunt", crashing_hunt)
    with pytest.raises(RuntimeError):
        await command.callback(cog, ctx)
    assert not bot.state.busy


async def test_second_player_joins_through_the_command_without_a_busy_notice(game_db, monkeypatch):
    bot = game_db.bot
    bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    monkeypatch.setattr(random, "randint", lambda low, high: low)
    cog = hunting.HuntingCog(bot)
    command = cog.hunting
    ala, ola, late = game_db.add_member("Ala"), game_db.add_member("Ola"), game_db.add_member("Spóźniony")
    take_turns(monkeypatch)
    team_answers(game_db, [ala, ola])

    ctx = make_ctx(game_db, ala)
    await run_checks(command, ctx)
    task = asyncio.ensure_future(command.callback(cog, ctx))
    await wait_until(lambda: game_db.channel.contains("Inny gracz może dołączyć do polowania"))
    assert bot.state.busy_owner == "hunt"

    # Ola's "!polowanie" joins the hunt; the command it also triggers stays silent.
    joining = game_db.say(ola, "!polowanie")
    with pytest.raises(guards.SilentCheckFailure):
        await run_checks(command, make_ctx(game_db, ola, message=joining))
    assert not game_db.channel.contains(guards.BUSY_NOTICE)

    await wait_until(lambda: game_db.channel.contains("Rozpoczęliście polowanie"))
    # Too late to join: an ordinary blocked command, answered with the notice.
    too_late = game_db.say(late, "!polowanie")
    with pytest.raises(guards.SilentCheckFailure):
        await run_checks(command, make_ctx(game_db, late, message=too_late))
    assert game_db.channel.contains(guards.BUSY_NOTICE)

    await task
    assert not bot.state.busy
    assert game_db.channel.contains(f"Brawo <@{ala.id}> <@{ola.id}> <:ok:990161663053422592> Potwór pokonany!")
    # Joining somebody's hunt is free: only the player who started it used the daily attempt.
    assert daily.load_daily_from_file(bot, ala.id) and not daily.load_daily_from_file(bot, ola.id)
    for hunter in (ala, ola):
        assert await bot.db.fetchval("SELECT POINTS FROM RANKING WHERE ID = $1", str(hunter.id)) == 1


async def test_admin_cooldown_commands(game):
    bot = game.bot
    cog = hunting.HuntingCog(bot)
    admin, ala = game.add_member("Alter"), game.add_member("Ala")
    daily.save_daily_to_file(bot, admin.id)
    daily.save_daily_to_file(bot, ala.id)

    await cog.clear_cd_daily_alter.callback(cog, make_ctx(game, admin, "!ClearDailyAlter", admin=True))
    assert game.channel.messages[-1].content == "Zresetowano daily cd Altera."
    assert not daily.load_daily_from_file(bot, admin.id) and daily.load_daily_from_file(bot, ala.id)

    await cog.clear_cd_daily.callback(cog, make_ctx(game, admin, "!ClearDaily", admin=True))
    assert game.channel.messages[-1].content == "Zresetowano daily cd."
    assert bot.store.daily_cd.all() == [] and bot.owner_log == ["Zresetowano cooldown na daily."]


# -- small stand-ins -----------------------------------------------------------------------

async def _nothing(*args, **kwargs):
    return None


async def _no_loot(scene, rarity, hunter, boost):
    return ""


async def _value(value):
    return value


def _answers(*, wrong=frozenset(), silent=frozenset()):
    """An ``auto_answer`` responder with several special rounds (1-based prompt numbers)."""
    counter = itertools.count(1)

    def responder(message):
        if "~~" in message.content:
            return None
        word = requested_word(message.content)
        if word is None:
            return None
        number = next(counter)
        if number in silent:
            return None
        return "zlaodpowiedz" if number in wrong else word

    return responder
