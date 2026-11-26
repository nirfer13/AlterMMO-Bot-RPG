"""The Death Tower (altermmo/features/tower.py) and its commands (altermmo/cogs/tower.py)."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import random
import re
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from altermmo.cogs import tower as tower_cog
from altermmo.core import challenge, guards
from altermmo.core.challenge import Outcome, RoundResult
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.scene import Scene
from altermmo.core.state import BossPhase
from altermmo.features import achievements, boss, daily, modifiers, patrons, pets, tower

from fakes import FakeChannel, FakeMember, FakeMessage, requested_word

ZERO = {"CRIT_PERC": 0.0, "REPLACE_PERC": 0.0, "DEF_PERC": 0.0, "DROP_PERC": 0.0,
        "LOWHP_PERC": 0.0, "SLOW_PERC": 0.0}

MSG_START = ("Rozpoczyna się walka w Wieży Śmierci, <@{id}>! Każde piętro to coraz trudniejszy boss do "
             "pokonania <:REEeee:790963160495947856>! Wpisz pojawiające się słowa tak szybko, jak to możliwe! "
             "Wielkość liter nie ma znaczenia! Wpisz słowa bez spacji! Przygotuj się!")
MSG_MISTAKE = ("Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie w przyszłym tygodniu! "
               "<:RIP:912797982917816341> Twój rekord został zapisany!")
MSG_TOO_SLOW = ("Niestety nie zdążyłeś! <:Bedge:970576892874854400>  Spróbuj ponownie w przyszłym tygodniu! "
                "<:RIP:912797982917816341> Twój rekord został zapisany!")
TOWER_URL = ("https://www.altermmo.pl/wp-content/uploads/alter0000_Huge_mystery_gloomy_tower_with_small_"
             "lights_inside._F_2fe81cd9-bff9-4bbe-af41-8101162ec6b9.png")


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_notices(monkeypatch):
    monkeypatch.setattr(guards, "_last_notice", {})


@pytest.fixture
def skills(monkeypatch):
    """Pet bonuses without a database: ``skills[player.id] = {"DEF_PERC": 100}``."""
    table: dict[int, dict] = {}

    async def fake_pet_skills(bot, player_id):
        return {**ZERO, **{key: float(value) for key, value in table.get(player_id, {}).items()}}

    monkeypatch.setattr(daily, "pet_skills", fake_pet_skills)
    return table


@pytest.fixture
def drops(monkeypatch):
    """Replace the boss loot by a recorder: a list of (rarity, hunter id, boost)."""
    seen: list[tuple] = []

    async def fake_loot(scene, rarity, hunter, boost):
        seen.append((rarity, hunter.id, boost))
        return ""

    monkeypatch.setattr(boss, "rand_loot", fake_loot)
    return seen


def prompts(channel) -> list[str]:
    """Every numbered fight prompt the bot posted."""
    return [m.content for m in channel.sent if m.content and re.match(r"^(~~)?\d+\. ", m.content)]


def spy_on_rounds(monkeypatch) -> list[float]:
    """Record the time limit of every fight round."""
    real_ask = challenge.ask
    limits: list[float] = []

    async def spy(bot, channel, prompt, **kwargs):
        limits.append(kwargs["timeout"])
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", spy)
    return limits


def answers(*, wrong=frozenset(), silent=frozenset()):
    """An ``auto_answer`` responder with several special rounds (1-based prompt numbers)."""
    counter = itertools.count(1)

    def responder(message):
        if not message.content or "~~" in message.content:
            return None
        word = requested_word(message.content)
        if word is None:
            return None
        number = next(counter)
        if number in silent:
            return None
        return "zlaodpowiedz" if number in wrong else word

    return responder


async def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.001)


def make_ctx(game, author, content="!wieża", *, channel=None, admin=False):
    channel = channel or game.channel
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


async def record(bot, player_id):
    row = await bot.db.fetchrow("SELECT TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = $1", int(player_id))
    return None if row is None else (int(row["tower_level"]), row["time"])


# -- numbers -------------------------------------------------------------------------------

def test_floor_numbers():
    assert [tower.random_tower_hp(level) for level in (1, 2, 10)] == [2, 4, 20]
    assert tower.random_tower_hp("3") == 6
    assert [tower.floor_loot_rarity(level) for level in range(1, 12)] == [0, 0, 1, 1, 2, 2, 2, 4, 4, 4, 2]
    assert tower.TOWER_FLOORS == 10


# -- the weekly cooldown list --------------------------------------------------------------

async def test_weekly_cooldown_file(game):
    bot = game.bot
    ala = game.add_member("Ala")
    assert not tower.load_weekly_tower_from_file(bot, ala.id)
    tower.save_weekly_tower_to_file(bot, ala.id)
    tower.save_weekly_tower_to_file(bot, str(ala.id))            # no duplicates
    tower.save_weekly_tower_to_file(bot, 123456)
    assert tower.load_weekly_tower_from_file(bot, ala.id) and tower.load_weekly_tower_from_file(bot, "123456")
    assert bot.store.weekly_tower_cd.path.read_text(encoding="utf-8") == f"{ala.id}\n123456\n"
    assert not daily.load_daily_from_file(bot, ala.id)           # a separate list

    await tower.clear_weekly_tower_file(bot)
    assert bot.store.weekly_tower_cd.all() == []
    assert bot.owner_log == ["Zresetowano cooldown na weekly tower."]
    assert game.log_channel.messages[-1].content == "Zresetowano cooldown na weekly tower."


# -- the floor picture ---------------------------------------------------------------------

async def test_tower_image(game, monkeypatch, tmp_path):
    await tower.tower_image(game.scene(), 1)
    picture, card = game.channel.messages[-2:]
    assert picture.files == ["1.png"]
    embed = card.embeds[0]
    assert embed.title == "<:AlterUp:846344753124999169> 1 piętro Wieży Śmierci! <:Loot:912797849916436570>"
    # (No hint about collector's achievements any more: the floor roles were withdrawn.)
    assert embed.description == "Rozpoczynasz eksplorację Wieży Śmierci. Strzeż się!"
    assert embed.colour.value == 0xF00511

    await tower.tower_image(game.scene(), 7)
    picture, card = game.channel.messages[-2:]
    assert picture.files == ["7.png"]
    assert card.embeds[0].title == "<:AlterUp:846344753124999169> 7 piętro Wieży Śmierci! <:Loot:912797849916436570>"
    assert card.embeds[0].description == "Przechodzisz na kolejne piętro. Powodzenia!"

    # A missing picture does not stop the run.
    monkeypatch.setattr(tower, "ASSETS_DIR", tmp_path)
    before = len(game.channel.messages)
    await tower.tower_image(game.scene(), 2)
    assert len(game.channel.messages) == before + 1 and game.channel.messages[-1].embeds


# -- database ------------------------------------------------------------------------------

async def test_create_database_keeps_existing_records(game_db):
    bot = game_db.bot
    assert await tower.new_record_tower(bot, 42, 3, timedelta(seconds=5)) is True
    await tower.create_database(game_db.scene())
    await tower.create_database(bot)                               # a bare bot works too
    assert await record(bot, 42) == (3, "0:00:05.000000")
    columns = await bot.db.fetch(
        "SELECT column_name, data_type, character_maximum_length FROM information_schema.columns "
        "WHERE table_name = 'tower' ORDER BY ordinal_position")
    assert [tuple(row) for row in columns] == [("player_id", "numeric", None), ("tower_level", "numeric", None),
                                               ("time", "character varying", 255)]


async def test_new_record_tower(game_db):
    bot = game_db.bot
    assert await tower.new_record_tower(bot, 7, 2, "0:00:03.500000") is True
    assert await tower.new_record_tower(bot, 7, 9, "0:00:01.000000") is False      # already has one
    assert await record(bot, 7) == (2, "0:00:03.500000")


async def test_update_tower_level_keeps_the_best_floor_and_time(game_db):
    bot = game_db.bot
    ala = game_db.add_member("O'Brien")
    scene = game_db.scene()
    assert await tower.update_tower_level(scene, ala, 1, timedelta(seconds=3, microseconds=250000)) is True
    assert await record(bot, ala.id) == (1, "0:00:03.250000")
    # A higher floor always wins, even with a longer time.
    await tower.update_tower_level(scene, ala, 2, timedelta(seconds=9))
    assert await record(bot, ala.id) == (2, "0:00:09.000000")     # whole seconds keep the fraction
    # A lower floor never overwrites.
    await tower.update_tower_level(scene, ala, 1, timedelta(seconds=1))
    assert await record(bot, ala.id) == (2, "0:00:09.000000")
    # The same floor: only a better time.
    await tower.update_tower_level(scene, ala, 2, timedelta(seconds=9, microseconds=1))
    assert await record(bot, ala.id) == (2, "0:00:09.000000")
    await tower.update_tower_level(scene, ala, 2, timedelta(seconds=8, microseconds=999999))
    assert await record(bot, ala.id) == (2, "0:00:08.999999")
    # Text in the stored format is accepted too (legacy callers passed str(timedelta)).
    await tower.update_tower_level(scene, ala, 2, "0:00:07.500000")
    assert await record(bot, ala.id) == (2, "0:00:07.500000")
    assert await bot.db.fetchval("SELECT count(*) FROM TOWER") == 1


async def test_update_tower_level_with_legacy_time_formats(game_db):
    """Times written by the legacy bot: without a fraction (crashed strptime) or unreadable."""
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await bot.db.execute("INSERT INTO TOWER (PLAYER_ID, TOWER_LEVEL, TIME) VALUES ($1, 4, '0:00:06'), "
                         "($2, 4, 'brak')", ala.id, ola.id)
    scene = game_db.scene()
    await tower.update_tower_level(scene, ala, 4, timedelta(seconds=6, microseconds=5))
    assert await record(bot, ala.id) == (4, "0:00:06")
    await tower.update_tower_level(scene, ala, 4, timedelta(seconds=5, microseconds=500000))
    assert await record(bot, ala.id) == (4, "0:00:05.500000")
    await tower.update_tower_level(scene, ola, 4, timedelta(seconds=30))            # unreadable = beaten
    assert await record(bot, ola.id) == (4, "0:00:30.000000")


async def test_floors_give_no_roles_any_more(game_db):
    """The collector's roles of floors 3, 6 and 10 were withdrawn on 2026-11-20 (owner: too many roles)."""
    bot = game_db.bot
    old_roles = [game_db.guild.add_role(role_id, name) for role_id, name in achievements.RETIRED_ROLES.items()
                 if name.startswith("Wieża")]
    assert len(old_roles) == 3
    crafter = game_db.add_member("Crafter", roles=[patrons.CRAFTER_ROLE])
    before = list(crafter.roles)
    said = len(game_db.channel.messages)
    scene = game_db.scene()
    for level in (1, 2, 3, 6, 10):
        assert await tower.update_tower_level(scene, crafter, level, timedelta(seconds=level)) is True
    assert crafter.roles == before
    assert await record(bot, crafter.id) == (10, "0:00:10.000000")
    assert len(game_db.channel.messages) == said and bot.owner_log == []      # no role announced

    # A player who left the server in the meantime: the record is stored, no crash.
    gone = FakeMember("Uciekinier")
    gone.roles.append(game_db.guild.add_role(patrons.CRAFTER_ROLE))
    assert await tower.update_tower_level(scene, gone, 3, timedelta(seconds=2)) is True
    assert await record(bot, gone.id) == (3, "0:00:02.000000")


async def test_tower_ranking(game_db):
    bot = game_db.bot
    names = ["O'Brien", "Ala", "Ola"]
    members = [game_db.add_member(name) for name in names]
    await tower.new_record_tower(bot, members[0].id, 5, "0:00:20.000000")
    await tower.new_record_tower(bot, members[1].id, 7, "0:00:30.000000")
    await tower.new_record_tower(bot, members[2].id, 5, "0:00:10.500000")
    await tower.new_record_tower(bot, 424242, 1, "0:00:01.000000")       # left the server
    for extra in range(8):                                               # only the best ten are shown
        await tower.new_record_tower(bot, 5000 + extra, 0, f"0:00:0{extra}.000000")

    other_channel = FakeChannel(555, game_db.guild, bot.fake_user, name="inny")
    scene = Scene(bot=bot, channel=other_channel, author=None, message=None)   # display-only: any channel
    await tower.tower_ranking(scene)
    embed = other_channel.messages[-1].embeds[0]
    assert embed.title == "Eksploratorzy Wieży Śmierci!" and embed.colour.value == 0xFF0019
    assert embed.url == embed.thumbnail.url == TOWER_URL
    assert embed.footer.text == "Gratulacje dla nieustraszonych!"
    lines = embed.description.splitlines()
    assert lines[:4] == [
        "🥇 **Ala** · piętro **7** · ⏱️ 30,00 s",
        "🥈 **Ola** · piętro **5** · ⏱️ 10,50 s",
        "🥉 **O'Brien** · piętro **5** · ⏱️ 20,00 s",
        "**4.** **<@424242>** · piętro **1** · ⏱️ 1,00 s",
    ]
    assert len(lines) == 10 and lines[-1].startswith("**10.** **<@5005>**")

    await bot.db.execute("DELETE FROM TOWER")
    await tower.tower_ranking(game_db.scene())
    assert game_db.channel.messages[-1].embeds[0].title == "Eksploratorzy Wieży Śmierci!"
    assert game_db.channel.messages[-1].embeds[0].description == tower.MSG_NOBODY_YET


def test_times_are_shown_the_way_players_read_them():
    assert tower.shown_time("0:00:12.345678") == "12,35 s"
    assert tower.shown_time("0:00:01.000000") == "1,00 s"
    assert tower.shown_time("0:01:05.200000") == "1 min 05,20 s"
    assert tower.shown_time("0:00:59.999000") == "1 min 00,00 s"           # rounded, never "60,00 s"
    assert tower.shown_time("1:02:03.500000") == "1 h 2 min 03,50 s"
    assert tower.shown_time("00:00:20.000") == "20,00 s"                    # an old record without microseconds
    assert tower.shown_time("coś dziwnego") == "coś dziwnego"               # unreadable: shown as stored
    from datetime import timedelta
    assert tower.shown_time(timedelta(seconds=7.5)) == "7,50 s"


async def test_personal_record(game_db):
    bot = game_db.bot
    ala = game_db.add_member("O'Brien")
    await tower.check_personal_tower(game_db.scene(ala))
    embed = game_db.channel.messages[-1].embeds[0]
    assert embed.title == "Personalny rekord Wieży Śmierci!" and embed.footer.text == "Gratulacje!"
    assert embed.description == "Jeszcze nie udało Ci się zeksplorować Wieży Śmierci.\n"
    assert embed.url == embed.thumbnail.url == TOWER_URL and embed.colour.value == 0xFF0019

    await tower.new_record_tower(bot, ala.id, 6, "0:00:12.345678")
    await tower.check_personal_tower(game_db.scene(ala))
    assert game_db.channel.messages[-1].embeds[0].description == (
        f"<@{ala.id}> udało Ci się ukończyć Wieżę Śmierci na poziomie **6** w czasie **12,35 s**.\n")


# -- one floor -----------------------------------------------------------------------------

async def test_floor_cleared(game_db, drops, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("O'Brien")
    limits = spy_on_rounds(monkeypatch)
    game_db.auto_answer(hunter)
    assert await tower.tower_fight(game_db.scene(hunter), 2) is True

    channel = game_db.channel
    assert channel.contains(MSG_START.format(id=hunter.id))
    assert channel.contains("Uwaga!!! 3... 2... 1...")
    asked = prompts(channel)
    assert len(asked) == 4 and all(re.match(rf"^{n}\. \*\*O'Brien\*\*: ", p) for n, p in enumerate(asked, 1))
    assert all(requested_word(p) in dict(daily.MOB_ACTIONS) for p in asked)
    assert limits == [15, 4.0, 4.0, 4.0]                    # 15 s for the first word, then 8 / floor
    assert channel.contains(f"Brawo <@{hunter.id}> <:ok:990161663053422592> Piętro zaliczone!")
    assert re.search(r"Zabicie potwora zajęło: \d+\.\d\d sekundy! Jedna tura zajęła średnio \d+\.\d\d sekundy!",
                     channel.transcript)
    assert drops == [(0, hunter.id, 0.0)]
    level, stored = await record(bot, hunter.id)
    assert level == 2 and re.fullmatch(r"0:00:\d\d\.\d{6}", stored)
    assert bot.owner_log == []


async def test_floor_with_real_boss_loot_and_pet_from_the_database(game_db):
    """No stand-ins: pet bonuses come from PETS, the reward from the boss loot table."""
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    game_db.auto_answer(hunter)
    assert await tower.tower_fight(game_db.scene(hunter), 1, hunter) is True
    assert game_db.channel.contains("Piętro zaliczone!")
    assert any(m.embeds and (m.embeds[0].title or "") == "Boss Drop - Ala" for m in game_db.channel.messages)
    assert (await record(bot, hunter.id))[0] == 1
    assert bot.owner_log == []


async def test_tower_loot_never_gets_the_bonus_of_a_special_boss(game, skills, special_bosses, monkeypatch):
    """The cosmetic bonus belongs to the fight with the special boss itself (features/bossvariants.py)."""
    from altermmo.core.state import BossInfo
    from altermmo.features import bossvariants

    special_bosses()                                                       # the tests' own config: bonuses are on
    assert bossvariants.rerolls_for("mgla", 4) == 5 and bossvariants.rerolls_for("mgla", 0) == 1
    game.bot.state.boss = BossInfo(rarity=4, variant="mgla")               # even with such a boss around
    seen: list[tuple] = []

    async def loot(scene, rarity, hunter, boost, **extra):
        seen.append((rarity, hunter.id, extra))
        return ""

    monkeypatch.setattr(boss, "rand_loot", loot)
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 2) is True
    assert seen == [(0, hunter.id, {})]


async def test_tower_loot_is_drawn_as_the_towers(game, skills, monkeypatch):
    """(Items, 2026-11-26) a floor's loot comes from ``"tower"``: some rows of the boss tables never drop there."""
    from altermmo.features import items

    sources: list = []

    async def loot(scene, rarity, hunter, boost, **extra):
        sources.append(items.LOOT_SOURCE.get())
        return ""

    monkeypatch.setattr(boss, "rand_loot", loot)
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 1) is True
    assert sources == ["tower"] and items.LOOT_SOURCE.get() is None


async def test_mistake_loses_the_floor(game, skills, drops):
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, wrong_on_round=2)
    assert await tower.tower_fight(game.scene(hunter), 3) is False
    assert game.channel.messages[-1].content == MSG_MISTAKE
    assert len(prompts(game.channel)) == 2 and drops == []


async def test_timeout_loses_the_floor(game, skills, drops, short_timeouts):
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, silent_on_round=2)
    assert await tower.tower_fight(game.scene(hunter), 1) is False
    assert game.channel.messages[-1].content == MSG_TOO_SLOW
    assert drops == [] and game.bot.owner_log == []


async def test_only_the_explorer_answers_and_is_recognised_by_id(game, skills, drops, monkeypatch):
    hunter = game.add_member("Ala")
    outsider = game.add_member("Obcy")
    # A second object for the same user (discord.py hands out fresh Member objects after a cache
    # refresh); the legacy ``msg.author is player`` ignored such answers until the timeout.
    twin = FakeMember("Ala", member_id=hunter.id, guild=game.guild)

    def hook(message):
        word = requested_word(message.content or "")
        if word is None:
            return
        game.say_later(outsider, word, 0)              # somebody else types the word: ignored
        game.say_later(outsider, "zle", 0)             # ... or a wrong one: ignored as well
        game.say_later(twin, word.upper(), 0.01)       # capital letters are fine

    game.channel.send_hooks.append(hook)
    assert await tower.tower_fight(game.scene(hunter), 2) is True
    assert len(prompts(game.channel)) == 4 and drops == [(0, hunter.id, 0.0)]


async def test_pet_skills_in_the_tower(game, skills, drops, monkeypatch, short_timeouts):
    hunter = game.add_member("Ala")
    monkeypatch.setattr(tower, "_store_record", _stored)
    # Floor 5: 10 hp lowered by 30 % -> 7; every hit is critical and counts twice.
    skills[hunter.id] = {"DEF_PERC": 100, "CRIT_PERC": 100, "LOWHP_PERC": 30, "DROP_PERC": 12.5}
    game.auto_answer(hunter, responder=answers(wrong={1}, silent={2}))
    assert await tower.tower_fight(game.scene(hunter), 5) is True
    assert game.channel.contains("Pomyliłeś się, ale Twój towarzysz Cię chroni!")
    assert game.channel.contains("Nie zdążyłeś, ale Twój towarzysz Cię chroni!")
    assert [p.split(".")[0] for p in prompts(game.channel)] == ["1", "2", "3", "5", "7"]
    assert drops == [(2, hunter.id, 12.5)]

    game.channel.sent.clear()
    game.channel.send_hooks.clear()
    skills[hunter.id] = {"REPLACE_PERC": 100}
    assert await tower.tower_fight(game.scene(hunter), 2) is True
    pet_lines = prompts(game.channel)
    assert len(pet_lines) == 4 and all(line.startswith("~~") for line in pet_lines)
    assert re.match(r"^~~1\. Ala: .+\*\*~~ Twój towarzysz wyprowadza atak!$", pet_lines[0])

    # A pet that lowers the life by 100 % (or more) leaves one word, not a division by zero.
    game.channel.sent.clear()
    skills[hunter.id] = {"LOWHP_PERC": 100}
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 4) is True
    assert len(prompts(game.channel)) == 1


async def test_slowing_pet_gives_more_time(game, skills, drops, monkeypatch):
    hunter = game.add_member("Ala")
    monkeypatch.setattr(tower, "_store_record", _stored)
    skills[hunter.id] = {"SLOW_PERC": 25}
    limits = spy_on_rounds(monkeypatch)
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 4) is True
    assert limits == pytest.approx([15] + [2.5] * 7)        # 8 / 4 s * 125 %
    assert drops == [(1, hunter.id, 0.0)]


async def test_disrupted_round_is_repeated_without_punishment(game, skills, drops, monkeypatch):
    hunter = game.add_member("Ala")
    monkeypatch.setattr(tower, "_store_record", _stored)
    real_ask = challenge.ask
    calls = itertools.count(1)

    async def flaky(bot, channel, prompt, **kwargs):
        if next(calls) in (2, 3):                             # two lost rounds in a row
            return RoundResult(Outcome.DISRUPTED)
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", flaky)
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 1) is True
    assert game.channel.count(challenge.MSG_DISRUPTED) == 2
    assert [p.split(".")[0] for p in prompts(game.channel)] == ["1", "2"]


async def test_repeated_disruptions_interrupt_the_floor(game, skills, drops, monkeypatch):
    hunter = game.add_member("Ala")

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", dead_line)
    assert await tower.tower_fight(game.scene(hunter), 1) is None
    assert game.channel.count(challenge.MSG_DISRUPTED) == challenge.MAX_CONSECUTIVE_DISRUPTIONS - 1
    assert game.channel.messages[-1].content == tower.MSG_TOWER_INTERRUPTED
    assert drops == []


async def test_cleared_floor_stays_cleared_when_bookkeeping_fails(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala", roles=[patrons.CRAFTER_ROLE])

    async def no_loot(scene, rarity, player, boost):
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(boss, "rand_loot", no_loot)
    original = game_db.channel.send

    async def no_cheering(content=None, **kwargs):
        if content and "Brawo" in content:
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game_db.channel.send = no_cheering
    game_db.auto_answer(hunter)
    assert await tower.tower_fight(game_db.scene(hunter), 3) is True
    context = "po zaliczonym piętrze Wieży Śmierci"
    assert any(f"{context} (ogłoszenie zwycięstwa)" in line and "RuntimeError" in line for line in bot.owner_log)
    assert any(f"{context} (łup <@{hunter.id}> za piętro 3)" in line and "DatabaseUnavailable" in line
               for line in bot.owner_log)
    # The record was still stored.
    assert (await record(bot, hunter.id))[0] == 3

    # The record cannot be stored: the floor still counts, the owner is told.
    async def no_record(*args, **kwargs):
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(tower, "_store_record", no_record)
    game_db.channel.send = original
    game_db.channel.send_hooks.clear()
    game_db.auto_answer(hunter)
    assert await tower.tower_fight(game_db.scene(hunter), 1) is True
    assert any(f"rekord wieży <@{hunter.id}> (piętro 1)" in line for line in bot.owner_log)


async def test_database_failure_before_the_fight_propagates(game, monkeypatch):
    hunter = game.add_member("Ala")

    async def no_database(bot, player_id):
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(pets, "get_pet_skills", no_database)
    with pytest.raises(DatabaseUnavailable):
        await tower.tower_fight(game.scene(hunter), 1)
    assert not game.channel.contains("Błąd bazy danych")      # the db layer retries, not the tower
    with pytest.raises(ValueError):
        await tower.tower_fight(game.scene(), 1)              # nobody to fight


# -- changed commands from the shrine ------------------------------------------------------

async def test_changed_commands_do_not_apply_to_the_tower(game, skills, drops, monkeypatch):
    """The tower compares record times: it keeps the usual words whatever the shrine gave."""
    bot = game.bot
    hunter = game.add_member("Ala")
    monkeypatch.setattr(tower, "_store_record", _stored)
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    ranges: list[tuple[int, int]] = []
    counter = itertools.count()

    def randint(low, high):                                    # walk the range: every word gets asked
        ranges.append((low, high))
        return low + next(counter) % (high - low + 1)

    monkeypatch.setattr(random, "randint", randint)
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 8) is True    # 16 words: all 15, then the first again

    asked = prompts(game.channel)
    usual = [prompt for _, prompt in daily.MOB_ACTIONS]
    assert asked == [f"{n}. **Ala**: {prompt}" for n, prompt in enumerate(usual + usual[:1], 1)]
    assert set(ranges) == {(0, 14)}
    changed = {word for word, _ in modifiers.CHANGED_ACTIONS}
    assert not any(requested_word(p) in changed for p in asked)
    assert game.channel.contains("Piętro zaliczone!")
    assert (await modifiers.load_modifiers(bot))[modifiers.COMMANDS_CHANGED] == 1   # still there for the boss


# -- !wieża: the whole run -----------------------------------------------------------------

async def test_whole_tower_cleared(game_db, drops):
    bot = game_db.bot
    for role_id in achievements.RETIRED_ROLES:
        game_db.guild.add_role(role_id, f"Stara rola {role_id}")
    hunter = game_db.add_member("O'Brien", roles=[patrons.CRAFTER_ROLE])
    before = list(hunter.roles)
    game_db.auto_answer(hunter)
    assert await tower.explore_tower(game_db.scene(hunter)) == 10
    channel = game_db.channel
    assert [m.files[0] for m in channel.messages if m.files] == [f"{level}.png" for level in range(1, 11)]
    assert channel.count("Piętro zaliczone!") == 10
    assert len(prompts(channel)) == sum(2 * level for level in range(1, 11))
    assert [rarity for rarity, _, _ in drops] == [0, 0, 1, 1, 2, 2, 2, 4, 4, 4]
    assert (await record(bot, hunter.id))[0] == 10
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    # No floor role any more (withdrawn on 2026-11-20), not even for a supporter.
    assert hunter.roles == before
    assert not channel.contains("problemów technicznych")
    assert bot.owner_log == []


async def test_run_ends_on_the_first_lost_floor_and_keeps_the_cooldown(game_db, drops):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    game_db.auto_answer(hunter, wrong_on_round=4)               # floor 1 has 2 words, floor 2 has 4
    assert await tower.explore_tower(game_db.scene(hunter)) == 1
    assert game_db.channel.messages[-1].content == MSG_MISTAKE
    assert [m.files[0] for m in game_db.channel.messages if m.files] == ["1.png", "2.png"]
    assert await record(bot, hunter.id) is not None and (await record(bot, hunter.id))[0] == 1
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    assert not game_db.channel.contains("problemów technicznych")
    with pytest.raises(ValueError):
        await tower.explore_tower(game_db.scene())


async def test_run_gives_the_attempt_back_after_a_technical_failure_before_the_first_kill(game, skills, drops):
    bot = game.bot
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    original = game.channel.send

    async def broken_on_floor_one(content=None, **kwargs):
        if content and content.startswith("2. "):               # the last word of floor 1
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game.channel.send = broken_on_floor_one
    with pytest.raises(RuntimeError):
        await tower.explore_tower(game.scene(hunter))
    assert not tower.load_weekly_tower_from_file(bot, hunter.id)
    assert game.channel.messages[-1].content == (
        f"<@{hunter.id}>, z powodu problemów technicznych Twoja wyprawa do Wieży Śmierci się nie liczy - "
        "możesz spróbować ponownie w tym tygodniu!")
    assert drops == [] and not game.channel.contains("Piętro zaliczone!")
    assert bot.owner_log == []                                  # nothing to settle by hand


async def test_run_keeps_the_cooldown_after_a_technical_failure_past_a_rewarded_floor(game, skills, drops,
                                                                                      monkeypatch):
    """Owner's rule of 2026-11-20: floor 1 was rewarded, so a second run would hand its loot out again."""
    bot = game.bot
    monkeypatch.setattr(tower, "_store_record", _stored)
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    original = game.channel.send

    async def broken_on_floor_two(content=None, **kwargs):
        if content and content.startswith("3. "):               # only floor 2 has a third word
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game.channel.send = broken_on_floor_two
    # Not re-raised: the player is told the run counts ("try again" would contradict it).
    assert await tower.explore_tower(game.scene(hunter)) == 1
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    assert game.channel.messages[-1].content == (
        f"<@{hunter.id}>, z powodu problemów technicznych Twoja wyprawa do Wieży Śmierci kończy się po 1. "
        "piętrze. Zdobyte piętra i łupy zostają; administracja dostała znać.")
    assert not game.channel.contains("możesz spróbować ponownie")
    assert len(drops) == 1                                      # floor 1 was cleared and rewarded, once
    assert bot.owner_log == [tower.LOG_RUN_CUT_SHORT.format(user_id=hunter.id, floors=1)]
    assert game.log_channel.messages[-1].content.startswith(f"<@{bot.ids.owner}>!")    # with a ping

    # A second !wieża the same week is refused: the loot of floor 1 is not handed out again.
    game.channel.send = original
    refused: list[str] = []

    async def refuse(text):
        refused.append(text)

    assert await tower.start_tower(game.scene(hunter), refuse=refuse) is False
    assert refused == [tower.cooldown_text()] and len(drops) == 1


async def test_run_gives_the_attempt_back_after_disruptions_and_on_shutdown(game, skills, drops):
    bot = game.bot
    hunter = game.add_member("Ala")

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(challenge, "ask", dead_line)
        assert await tower.explore_tower(game.scene(hunter)) == 0
    assert not tower.load_weekly_tower_from_file(bot, hunter.id)
    assert game.channel.messages[-2].content == tower.MSG_TOWER_INTERRUPTED
    assert game.channel.messages[-1].content == tower.MSG_ATTEMPT_RETURNED.format(user_id=hunter.id)

    # The bot is stopped in the middle of a run: the attempt is not lost either.
    task = asyncio.ensure_future(tower.explore_tower(game.scene(hunter)))
    await wait_until(lambda: len(prompts(game.channel)) == 1)
    assert tower.load_weekly_tower_from_file(bot, hunter.id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not tower.load_weekly_tower_from_file(bot, hunter.id)


# -- the cog -------------------------------------------------------------------------------

async def test_tower_cog_registers_the_legacy_commands(game):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.tower")
    run = bot.get_command("wieża")
    assert run is not None and run.cog_name == "Tower" and bot.get_command("wieza") is run
    assert len(run.checks) == 2 and run._buckets._cooldown is None
    ranking = bot.get_command("rankingwiezy")
    for alias in ("wieze", "rankingwieza", "rankingwieży", "rankingwieża", "rw"):
        assert bot.get_command(alias) is ranking
    personal = bot.get_command("rekordwiezy")
    for alias in ("rekordwieży", "rekordwieża", "rekordwieza"):
        assert bot.get_command(alias) is personal
    for name in ("ClearTowerCD", "createTowerDatabase"):
        admin = bot.get_command(name)
        assert admin is not None and len(admin.checks) == 1
        with pytest.raises(commands.MissingPermissions):
            await run_checks(admin, make_ctx(game, game.add_member("Gracz"), "!" + name))
        await run_checks(admin, make_ctx(game, game.add_member("Admin"), "!" + name, admin=True))


async def test_wieza_command_rules(game, monkeypatch):
    bot = game.bot
    bot.debug = False
    cog = tower_cog.TowerCog(bot)
    command = cog.explore_tower
    ala = game.add_member("Ala")
    runs: list = []

    async def fake_run(scene):
        runs.append((scene.author, scene.channel, bot.state.busy_owner))
        return 0

    monkeypatch.setattr(tower, "explore_tower", fake_run)

    # Only on the boss channel.
    elsewhere = FakeChannel(555, game.guild, bot.fake_user, name="inny")
    with pytest.raises(guards.SilentCheckFailure):
        await run_checks(command, make_ctx(game, ala, channel=elsewhere))

    ctx = make_ctx(game, ala)
    await run_checks(command, ctx)
    await command.callback(cog, ctx)
    assert runs == [(ala, game.channel, "tower")] and not bot.state.busy

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
        "Nie możesz ruszyć na eksplorację Wieży Śmierci, na Twojej drodze stoi boss "
        "<:MonkaS:882181709100097587> Kliknij **Zaatakuj** pod bossem albo wpisz **!zaatakuj**, "
        "żeby stawić mu czoła.")
    assert len(game.channel.bot_messages) == before + 2
    for phase in (BossPhase.ENGAGED, BossPhase.FIGHTING):
        await bot.state.set_boss_phase(phase)
        await command.callback(cog, ctx)
    assert len(game.channel.bot_messages) == before + 2 and len(runs) == 1

    # Debug mode skips the boss restriction (legacy DebugMode).
    bot.debug = True
    await command.callback(cog, ctx)
    assert len(runs) == 2
    bot.debug = False

    # Already explored this week.
    await bot.state.set_boss_phase(BossPhase.WAITING)
    tower.save_weekly_tower_to_file(bot, ala.id)
    await command.callback(cog, ctx)
    stamp = int(daily.next_season_end().timestamp())                  # the weekly reset: Monday 15:00
    assert game.channel.messages[-1].content == (
        f"Drzwi Wieży Śmierci ani drgną. Otworzą się ponownie <t:{stamp}:R> <:Bedge:970576892874854400>")
    assert len(runs) == 2

    # Busy with something else: the guard answers with the notice, nothing starts.
    await tower.clear_weekly_tower_file(bot)
    with bot.state.hold_busy("hunt"):
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, ctx)
        assert game.channel.messages[-1].content == guards.BUSY_NOTICE
        await command.callback(cog, ctx)                  # even past the guard nothing starts
    assert len(runs) == 2

    # A failing run releases the busy flag.
    async def crashing_run(scene):
        raise RuntimeError("boom")

    monkeypatch.setattr(tower, "explore_tower", crashing_run)
    with pytest.raises(RuntimeError):
        await command.callback(cog, ctx)
    assert not bot.state.busy


async def test_wieza_command_end_to_end(game_db, drops):
    bot = game_db.bot
    bot.debug = False
    cog = tower_cog.TowerCog(bot)
    hunter = game_db.add_member("Ala")
    game_db.auto_answer(hunter, wrong_on_round=3)             # clears floor 1, falls on floor 2
    ctx = make_ctx(game_db, hunter)
    await run_checks(cog.explore_tower, ctx)
    await cog.explore_tower.callback(cog, ctx)
    assert not bot.state.busy and tower.load_weekly_tower_from_file(bot, hunter.id)
    assert (await record(bot, hunter.id))[0] == 1

    # Display-only commands work on any channel of the server.
    elsewhere = FakeChannel(555, game_db.guild, bot.fake_user, name="inny")
    for command, title in ((cog.tower_ranking, "Eksploratorzy Wieży Śmierci!"),
                           (cog.check_personal_tower, "Personalny rekord Wieży Śmierci!")):
        shown = make_ctx(game_db, hunter, "!rw", channel=elsewhere)
        await run_checks(command, shown)
        await command.callback(cog, shown)
        assert elsewhere.messages[-1].embeds[0].title == title
    assert "**Ala** · piętro **1**" in elsewhere.bot_messages[-2].embeds[0].description
    assert f"<@{hunter.id}> udało Ci się ukończyć Wieżę Śmierci na poziomie **1**" in \
        elsewhere.messages[-1].embeds[0].description


async def test_admin_commands(game_db):
    bot = game_db.bot
    cog = tower_cog.TowerCog(bot)
    admin = game_db.add_member("Alter")
    tower.save_weekly_tower_to_file(bot, admin.id)
    await tower.new_record_tower(bot, admin.id, 4, "0:00:04.000000")

    await cog.clear_cd.callback(cog, make_ctx(game_db, admin, "!ClearTowerCD", admin=True))
    assert game_db.channel.messages[-1].content == "Zresetowano tower cd."
    assert bot.store.weekly_tower_cd.all() == [] and bot.owner_log == ["Zresetowano cooldown na weekly tower."]

    await cog.create_tower_database.callback(cog, make_ctx(game_db, admin, "!createTowerDatabase", admin=True))
    assert game_db.channel.messages[-1].content == "Baza danych TOWER utworzona."
    assert await record(bot, admin.id) == (4, "0:00:04.000000")       # nothing was dropped


# -- small stand-ins -----------------------------------------------------------------------

async def _stored(*args, **kwargs):
    return True


async def test_display_commands_are_never_blocked(game):
    """As in the old bot the tower rankings answer even while an activity runs."""
    bot = game.bot
    cog = tower_cog.TowerCog(bot)
    ala = game.add_member("Ala")
    elsewhere = FakeChannel(555, game.guild, bot.fake_user, name="inny")
    with bot.state.hold_busy("tower"):
        for command in (cog.tower_ranking, cog.check_personal_tower):
            await run_checks(command, make_ctx(game, ala, "!rw", channel=elsewhere))
    assert elsewhere.bot_messages == []                                            # no busy notice

