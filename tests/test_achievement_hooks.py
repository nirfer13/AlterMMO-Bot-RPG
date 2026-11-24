"""What is left of the achievements in play: the hooks in the boss fight, the hunts, the tower, the events, the pets.

Owner's decision of 2026-11-20 (evening): only the mythic achievements stay -
the mythic slayer (``boss.set_mythic_slayer``) and the three pet roles a
mythic egg stands for.  Everything else a deed used to give (boss kills,
speed, hunts, seasons, tower floors, chests, duels, tournaments, rituals ...)
gives no role and no "zdobywa osiągnięcie" line any more; the old counters of
``ACHIEVEMENTS`` still count (they are statistics).

Every test plays the real flow (fake Discord, real PostgreSQL) and looks at what a
player would see: the roles on the member, the announcements on the channel, the
counters in the database.  The state is the one after a start of the bot: the
withdrawn roles are gone and the roles of the mythic pets are made.
"""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from altermmo.cogs import schedule as schedule_cog
from altermmo.core import timing
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import achievements, boss, daily, database, events, patrons, pets, tower

from test_events import join_on
from test_pets import add_owner, add_pet, confirms, owner_row, pet_row, rig

SUPPORTER = patrons.PATRON_ROLES[0]
ANNOUNCEMENT = "zdobywa osiągnięcie"
PET_KEYS = ("pet_mythic", "pet_mythic_holo", "pet_mythic_seal")


# -- fixtures and helpers ---------------------------------------------------------------

@pytest_asyncio.fixture
async def ready(game_db):
    """The game as after a start: the old achievement roles deleted, the mythic pet roles made."""
    bot = game_db.bot
    for role_id, name in achievements.RETIRED_ROLES.items():   # what the server had before
        game_db.guild.add_role(role_id, name)
    await achievements.start_up(bot)
    assert not set(achievements.RETIRED_ROLES) & {role.id for role in game_db.guild.roles}
    assert all(achievements.role_exists(bot, achievements.trophy(key)) for key in PET_KEYS)
    bot.owner_log.clear()
    game_db.log_channel.messages.clear()
    return game_db


@pytest.fixture
def quick(monkeypatch):
    """Waiting times 100x shorter (sign-ups end quickly)."""
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.01)


@pytest.fixture
def no_loot(monkeypatch):
    """Rewards are recorded, not granted (the boss, the hunt, the tower and the events)."""
    calls: list[tuple] = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        calls.append((rarity, hunter, boost_percent))
        return ""

    monkeypatch.setattr(boss, "rand_loot", rand_loot)
    monkeypatch.setattr(daily, "rand_loot", rand_loot)
    return calls


@pytest.fixture
def duels(monkeypatch):
    """Scripted ``boss.duel_fight``: the first fighter wins unless ``duels.results`` says otherwise."""
    record = SimpleNamespace(calls=[], results=[])

    async def duel_fight(scene, participants, ban=True):
        record.calls.append(list(participants))
        if record.results:
            return record.results.pop(0)
        return True, participants[0]

    monkeypatch.setattr(events.boss, "duel_fight", duel_fight)
    return record


@pytest.fixture
def boss_image(monkeypatch):
    calls: list[int] = []

    async def send_boss_image(scene, rarity):
        calls.append(rarity)
        return False, None

    monkeypatch.setattr(events.boss, "send_boss_image", send_boss_image)
    return calls


@pytest.fixture
def no_cosmetics(monkeypatch):
    """The seal and Holo rolls of a supporter's egg never hit."""
    monkeypatch.setattr(random, "randrange", lambda *args: 999)


def supporter(game, name: str = "Patron"):
    return game.add_member(name, roles=[SUPPORTER])


def role_ids(member) -> set[int]:
    return {role.id for role in member.roles}


def role_of(game, key: str):
    return game.bot.role(achievements.role_id_of(game.bot, achievements.trophy(key)))


def keys_of(game, member) -> set[str]:
    """Achievements whose roles the member holds."""
    return {item.key for item in achievements.held(game.bot, member)}


def announced(game, member) -> list[str]:
    """Names of the achievement roles announced on the boss channel for ``member``."""
    prefix = f"<@{member.id}> {ANNOUNCEMENT}: "
    return [message.content[len(prefix):-1] for message in game.channel.sent
            if message.content.startswith(prefix)]


def nothing_given(game, members_before: dict) -> None:
    """Nobody got a role, nothing was announced as an achievement."""
    for member, before in members_before.items():
        assert role_ids(member) == before, member.name
        assert keys_of(game, member) == set()
    assert not game.channel.contains(ANNOUNCEMENT)


async def legacy_counter(game, member, column: str) -> int | None:
    value = await game.bot.db.fetchval(f"SELECT {column} FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", member.id)
    return None if value is None else int(value)


async def preload(game, member, *, boss_kills: int = 0, hunts: int = 0, butchers: int = 0) -> None:
    """A veteran's counters (the old roles came at 10 / 50 / 100 ...)."""
    assert await achievements.new_record_achievements(game.bot, member.id, boss_kills, hunts, butchers)


# -- boss --------------------------------------------------------------------------------

@pytest.mark.parametrize("is_supporter", [True, False])
@pytest.mark.parametrize("rarity", [0, 1, 2, 3])
async def test_won_boss_fight_counts_the_kill_and_gives_no_role(ready, no_loot, monkeypatch, rarity,
                                                                is_supporter):
    """Not for the number of kills, not for the speed (the old "szybkość"), not for a legendary boss."""
    game = ready
    hunter = supporter(game, "Szybki") if is_supporter else game.add_member("Zwykły")
    await preload(game, hunter, boss_kills=9)                    # the 10th kill gave a role in the old bot
    before = role_ids(hunter)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 3)
    game.auto_answer(hunter)
    assert (await boss.single_fight(game.scene(hunter), hunter, rarity, False, None)).won
    assert game.channel.contains("Jedna tura zajęła Ci średnio")
    assert await legacy_counter(game, hunter, "BOSS_KILLS") == 10
    nothing_given(game, {hunter: before})
    assert not game.channel.contains("zabił mitycznego bossa")


async def test_won_group_fight_gives_no_legendary_boss_role(ready, no_loot, monkeypatch):
    from test_boss import team_answers

    game = ready
    players = [supporter(game, name) for name in ("Ala", "Ola", "Ela")]
    before = {player: role_ids(player) for player in players}
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 3)
    team_answers(game, players)
    assert (await boss.group_fight(game.scene(players[0]), players, False, None)).won
    assert len(no_loot) == 3
    nothing_given(game, before)


async def test_supporter_who_kills_a_mythic_boss_still_becomes_the_mythic_slayer(ready, no_loot, monkeypatch):
    game = ready
    bot = game.bot
    hunter = supporter(game, "Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)
    game.auto_answer(hunter)
    assert (await boss.single_fight(game.scene(hunter), hunter, 4, False, None)).won
    assert role_ids(hunter) == {SUPPORTER, bot.ids.mythic_slayer_role}
    assert keys_of(game, hunter) == {"mythic_boss"}              # the one achievement a deed still gives
    assert game.channel.contains(f"<@&{bot.ids.hunters_role}>! Łowca <@{hunter.id}> zabił mitycznego bossa! "
                                 "<:POGGERS:936907543849078844> Jest to pierwszy łowca, który tego dokonał!")
    assert await legacy_counter(game, hunter, "BOSS_KILLS") == 1
    assert not game.channel.contains(ANNOUNCEMENT)               # announced as always, not as "osiągnięcie"
    summary = achievements.summary_embed(bot, hunter).description
    assert f"**1 z {len(achievements.TROPHIES)}**" in summary and f"<@&{bot.ids.mythic_slayer_role}>" in summary


async def test_non_supporter_who_kills_a_mythic_boss_gets_no_slayer_role(ready, no_loot, monkeypatch):
    game = ready
    hunter = game.add_member("Ola")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)
    game.auto_answer(hunter)
    assert (await boss.single_fight(game.scene(hunter), hunter, 4, False, None)).won
    nothing_given(game, {hunter: set()})
    assert not game.channel.contains("zabił mitycznego bossa")
    assert await legacy_counter(game, hunter, "BOSS_KILLS") == 1


# -- hunts and seasons ------------------------------------------------------------------------

@pytest.mark.parametrize("rarity", [0, 1, 2])
async def test_won_hunt_counts_and_gives_no_role(ready, no_loot, monkeypatch, rarity):
    """Not for the number of hunts, not for a legendary (unique) monster."""
    game = ready
    hunter = supporter(game, "Łowca")
    await preload(game, hunter, hunts=99)
    before = role_ids(hunter)
    monkeypatch.setattr(daily, "random_boss_hp", lambda rarity: 3)
    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(hunter), rarity, False, None) is True
    assert await legacy_counter(game, hunter, "HUNTS") == 100
    assert [call[1] for call in no_loot] == [hunter]
    nothing_given(game, {hunter: before})


async def test_won_season_counts_and_gives_only_the_season_role(ready):
    game = ready
    bot = game.bot
    winner = supporter(game, "Rzeźnik")
    await preload(game, winner, butchers=9)
    await database.update_ranking_table(bot, winner.id, 3, 0)
    await schedule_cog.ScheduleCog(bot).weekly_season_end()
    assert role_ids(winner) == {SUPPORTER, bot.ids.boss_slayer_role}   # "Rzeźnik Potworów" is no achievement
    assert keys_of(game, winner) == set() and not game.channel.contains(ANNOUNCEMENT)
    assert await legacy_counter(game, winner, "BUTCHERS") == 10
    assert not any("Błąd zaplanowanego zadania" in line for line in bot.owner_log)


# -- tower -----------------------------------------------------------------------------------

@pytest.mark.parametrize("level", [3, 6, 10])
async def test_cleared_tower_floor_gives_no_role(ready, no_loot, monkeypatch, level):
    """Not for the floor (the old "Wieża Śmierci I-III"), not for a fast floor."""
    game = ready
    climber = supporter(game, "Wspinacz")
    before = role_ids(climber)
    monkeypatch.setattr(tower, "random_tower_hp", lambda level: 2)
    game.auto_answer(climber)
    assert await tower.tower_fight(game.scene(climber), level) is True
    assert game.channel.contains("Piętro zaliczone!")
    assert await tower.update_tower_level(game.scene(), climber, level, timedelta(seconds=3)) is True
    nothing_given(game, {climber: before})
    assert game.bot.owner_log == []


# -- events -----------------------------------------------------------------------------------

async def test_open_chest_gives_the_loot_and_no_role(ready, no_loot):
    game = ready
    ala = supporter(game, "Ala")
    for _ in range(10):                                          # the 10th chest was to give a role
        await events.open_chest(game.scene(ala, "!skrzynia"))
    assert [hunter for _rarity, hunter, _boost in no_loot] == [ala] * 10
    nothing_given(game, {ala: {SUPPORTER}})


async def test_won_duel_gives_no_role(ready, no_loot, duels):
    game = ready
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    join_on(game, "Pojedynek!", ala, ola)
    assert await events.spawn_duel(game.scene()) is True
    assert duels.calls == [[ala, ola]]
    nothing_given(game, {ala: {SUPPORTER}, ola: {SUPPORTER}})


async def test_won_tournament_gives_no_role(ready, no_loot, duels, quick, monkeypatch):
    game = ready
    monkeypatch.setattr(random, "shuffle", lambda players: None)
    ala, ola, ela = supporter(game, "Ala"), supporter(game, "Ola"), supporter(game, "Ela")
    join_on(game, "Tygodniowy turniej PvP!", ala, ola, ela)
    duels.results.extend([(True, ola), (True, ola)])
    assert await events.spawn_pvp_tournament(game.scene()) == ola
    nothing_given(game, {ala: {SUPPORTER}, ola: {SUPPORTER}, ela: {SUPPORTER}})


async def test_successful_ritual_gives_no_role(ready, boss_image, quick, monkeypatch):
    game = ready
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: 1.0)
    ala, ola = supporter(game, "Ala"), game.add_member("Ola")
    join_on(game, "Rozpoczął się rytuał przywołania!", ala, ola)
    assert (await events.spawn_ritual(game.scene()))[0] is True
    assert boss_image == [1]
    for _ in range(20):                                          # (nothing may be left running to give one later)
        await asyncio.sleep(0)
    nothing_given(game, {ala: {SUPPORTER}, ola: set()})


# -- pets: a mythic egg -------------------------------------------------------------------------

def egg(monkeypatch, *, roll: int, seal_roll: int = 999, holo_roll: int = 999) -> None:
    """The next egg: ``roll`` of 1000 (1000 = mythic, 985 = ultra shiny, 0 = ordinary) and its cosmetics."""
    rig(monkeypatch, randint=lambda low, high: roll if high == 1000 else 0)
    rolls = iter([seal_roll, holo_roll])
    monkeypatch.setattr(random, "randrange", lambda *args: next(rolls, 999))


@pytest.mark.parametrize("seal_roll, holo_roll, expected", [
    (999, 999, ["Mityczny towarzysz"]),
    (999, 0, ["Mityczny towarzysz", "Mityczny Holo"]),
    (0, 999, ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]),
    (20, 999, ["Mityczny towarzysz"]),                           # a seal, but not the rarest one
    (0, 0, ["Mityczny towarzysz", "Mityczny Holo", "Mityczna Pieczęć Arcyłowcy"])])
async def test_mythic_egg_gives_a_supporter_the_pet_achievements(ready, monkeypatch, seal_roll, holo_roll,
                                                                 expected):
    game = ready
    egg(monkeypatch, roll=1000, seal_roll=seal_roll, holo_roll=holo_roll)
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    assert (await pet_row(game.bot.db, 1))["mythic"] is True
    assert announced(game, ala) == expected                      # announced, in the order of the catalogue
    assert [item.name for item in achievements.held(game.bot, ala)] == expected
    assert role_ids(ala) == {SUPPORTER, *(role_of(game, item.key).id for item in achievements.held(game.bot, ala))}
    assert game.channel.contains(f"Łowca <@{ala.id}> zdobył mitycznego towarzysza!")


@pytest.mark.parametrize("roll, seal_roll, holo_roll", [
    (0, 0, 999),                                                 # the rarest seal on an ordinary egg
    (0, 999, 0),                                                 # Holo on an ordinary egg
    (0, 0, 0),
    (985, 999, 999),                                             # ultra shiny
    (985, 0, 0)])
async def test_egg_that_is_not_mythic_gives_no_achievement(ready, monkeypatch, roll, seal_roll, holo_roll):
    game = ready
    egg(monkeypatch, roll=roll, seal_roll=seal_roll, holo_roll=holo_roll)
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    row = await pet_row(game.bot.db, 1)
    assert row["mythic"] is False and row["ultra_shiny"] is (roll == 985)
    assert (row["seal"], row["holo"]) == (3 if seal_roll == 0 else 0, holo_roll == 0)
    nothing_given(game, {ala: {SUPPORTER}})


async def test_mythic_egg_of_a_non_supporter_gives_no_achievement(ready, monkeypatch):
    game = ready
    egg(monkeypatch, roll=1000, seal_roll=0, holo_roll=0)       # (a non-supporter rolls no cosmetics)
    ola = game.add_member("Ola")
    assert await pets.assign_pet(game.scene(), ola) is True
    assert (await pet_row(game.bot.db, 1))["mythic"] is True
    nothing_given(game, {ola: set()})
    assert game.channel.contains(f"Łowca <@{ola.id}> zdobył mitycznego towarzysza!")


@pytest.mark.parametrize("broken", ["_pet_keys", "grant", "set_achiev_role"])
async def test_a_failing_pet_achievement_never_breaks_the_egg(ready, no_cosmetics, monkeypatch, broken):
    game = ready
    db = game.bot.db
    egg(monkeypatch, roll=1000)

    async def boom(*args, **kwargs):
        raise DatabaseUnavailable("test: database is down") if broken == "_pet_keys" else RuntimeError("boom")

    monkeypatch.setattr(achievements, broken, boom)
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    assert (await owner_row(db, ala.id))["pet_id"] == 1 and (await pet_row(db, 1))["mythic"] is True
    assert game.channel.contains(f"Łowca <@{ala.id}> zdobył mitycznego towarzysza!")
    nothing_given(game, {ala: {SUPPORTER}})


# -- pets: the other changes no longer look at the achievements --------------------------------

@pytest.fixture
def checks(monkeypatch):
    """Record the calls of ``achievements.check_pets`` (the real one runs)."""
    calls: list = []
    real = achievements.check_pets

    async def check_pets(scene, player, **kwargs):
        calls.append(player)
        return await real(scene, player, **kwargs)

    monkeypatch.setattr(achievements, "check_pets", check_pets)
    return calls


async def mythic_veteran(game, *, pet_lvl: int = 3, **owner):
    """A supporter whose mythic Holo pet with the rarest seal is from before the roles existed."""
    ala = supporter(game, "Ala")
    await add_owner(game.bot.db, ala.id, 5, **owner)
    await add_pet(game.bot.db, 5, pet_lvl=pet_lvl, pet_skills=6, crit_perc=10, variant="1", mythic=True,
                  seal=3, holo=True)
    return ala


async def test_level_up_does_not_look_at_the_achievements(ready, checks, monkeypatch):
    game = ready
    ala = await mythic_veteran(game, pet_lvl=pets.MAX_LEVEL - 1)
    rig(monkeypatch, randint=lambda low, high: low)
    assert await pets.level_up_pet(game.scene(), ala.id) is True
    assert (await pet_row(game.bot.db, 5))["pet_lvl"] == pets.MAX_LEVEL
    assert checks == []
    nothing_given(game, {ala: {SUPPORTER}})


async def test_enlightenment_does_not_look_at_the_achievements(ready, checks, monkeypatch):
    game = ready
    ala = await mythic_veteran(game, rebirth_stones=1)
    rig(monkeypatch, choices=["CRIT_PERC", "DROP_PERC", "DETECTION"])
    confirms(game, ala)
    assert await pets.enlight_pet(game.scene(ala), ala) is True
    assert (await pet_row(game.bot.db, 5))["pet_skills"] == 9
    assert checks == []
    nothing_given(game, {ala: {SUPPORTER}})


async def test_reroll_does_not_look_at_the_achievements(ready, checks, monkeypatch):
    game = ready
    ala = await mythic_veteran(game, reroll_scroll=1)
    rig(monkeypatch, choices=["CRIT_PERC", "DETECTION", "SLOW_PERC"])
    confirms(game, ala)
    assert await pets.reroll_pet(game.scene(ala), ala) is True
    assert (await owner_row(game.bot.db, ala.id))["reroll_scroll"] == 0
    assert checks == []
    nothing_given(game, {ala: {SUPPORTER}})


async def test_only_a_new_egg_looks_at_the_achievements(ready, checks, no_cosmetics, monkeypatch):
    game = ready
    egg(monkeypatch, roll=0)
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    assert checks == [ala]
