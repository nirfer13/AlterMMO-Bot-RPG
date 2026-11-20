"""Achievements earned in play: the hooks in the boss fight, the tower, the events and the pets.

Every test plays the real flow (fake Discord, real PostgreSQL) and looks at what a
player would see: the roles on the member, the announcements on the channel, the
counters in ``ACHIEVEMENT_COUNTERS``.  The state is the one after a start of the
bot: the new tables exist and the roles of the new achievements are made.
"""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from altermmo import schema
from altermmo.core import timing
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import achievements, boss, events, patrons, pets, tower

from test_events import clicks_on, join_on
from test_pets import add_owner, add_pet, owner_row, pet_row, rig

SUPPORTER = patrons.PATRON_ROLES[0]
#: The roles the server had before v2 (the bot finds them, it does not make them).
LEGACY_ROLES = {
    **{role_id: f"Pogromca {count}" for count, role_id in achievements.BOSS_KILL_ROLES.items()},
    **{role_id: f"Wieża {index}" for index, role_id in enumerate(achievements.TOWER_ROLES, 1)},
    achievements.QUICKNESS_ROLE: "Szybkość",
}


# -- fixtures and helpers ---------------------------------------------------------------

@pytest_asyncio.fixture
async def ready(game_db):
    """The game as after a start: new tables created, every achievement role on the server."""
    bot = game_db.bot
    await schema.create_new_tables(bot.db)
    for role_id, name in LEGACY_ROLES.items():
        game_db.guild.add_role(role_id, name)
    assert len(await achievements.ensure_roles(bot)) == sum(item.bot_made for item in achievements.TROPHIES)
    bot.owner_log.clear()
    game_db.log_channel.messages.clear()
    events._counting.clear()
    return game_db


@pytest.fixture
def quick(monkeypatch):
    """Waiting times 100x shorter (sign-ups end quickly)."""
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.01)


@pytest.fixture
def loot(monkeypatch):
    """Record ``daily.rand_loot`` of the events instead of granting loot."""
    calls: list[tuple] = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        calls.append((rarity, hunter, boost_percent))
        return ""

    monkeypatch.setattr(events.daily, "rand_loot", rand_loot)
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
def no_cosmetics(monkeypatch):
    """The seal and Holo rolls of a supporter's egg never hit (only the cosmetics tests want them)."""
    monkeypatch.setattr(random, "randrange", lambda *args: 999)


def fight_clock(module, monkeypatch, seconds: float) -> None:
    """``module``'s clock says the fight (from its first to its last reading) took ``seconds``."""
    readings = iter([0.0, float(seconds)])
    last = [0.0]

    def monotonic() -> float:
        last[0] = next(readings, last[0])
        return last[0]

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))


def role_of(game, key: str):
    return game.bot.role(achievements.role_id_of(game.bot, achievements.trophy(key)))


def keys_of(game, member) -> set[str]:
    """Achievements whose roles the member holds."""
    return {item.key for item in achievements.TROPHIES if role_of(game, item.key) in member.roles}


def announced(game, member) -> list[str]:
    """Names of the achievement roles announced on the boss channel for ``member``."""
    prefix = f"<@{member.id}> zdobywa osiągnięcie: "
    return [message.content[len(prefix):-1] for message in game.channel.sent
            if message.content.startswith(prefix)]


async def counter(game, member, name: str) -> int | None:
    value = await game.bot.db.fetchval(
        "SELECT VALUE FROM ACHIEVEMENT_COUNTERS WHERE PLAYER_ID = $1 AND NAME = $2", member.id, name)
    return None if value is None else int(value)


async def preload(game, member, name: str, value: int) -> None:
    await game.bot.db.execute(
        "INSERT INTO ACHIEVEMENT_COUNTERS (PLAYER_ID, NAME, VALUE) VALUES ($1, $2, $3)", member.id, name, value)


def supporter(game, name: str = "Patron"):
    return game.add_member(name, roles=[SUPPORTER])


# -- boss: speed ------------------------------------------------------------------------

async def won_single_fight(game, monkeypatch, hunter, seconds: float) -> None:
    """``hunter`` beats a boss of 3 hp; the fight takes ``seconds`` by the boss module's clock."""
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 3)

    async def rand_loot(scene, rarity, player, boost):
        return ""

    monkeypatch.setattr(boss, "rand_loot", rand_loot)
    fight_clock(boss, monkeypatch, seconds)
    game.auto_answer(hunter)
    result = await boss.single_fight(game.scene(hunter), hunter, 1, False, None)
    assert result.won


@pytest.mark.parametrize("seconds, per_turn, expected", [
    (1.8, "0.60", "quick_3"),          # under 0.75 s per turn
    (2.25, "0.75", "quick_2"),         # exactly 0.75 s is not "under 0.75 s"
    (2.7, "0.90", "quick_2"),          # under 1 s
    (3.0, "1.00", "quick_1"),          # exactly 1 s is not "under 1 s"
    (4.5, "1.50", "quick_1"),          # under 2 s: the old role
    (6.0, "2.00", None),
    (7.5, "2.50", None)])
async def test_speed_of_a_won_boss_fight_gives_the_tier_reached(ready, monkeypatch, seconds, per_turn, expected):
    game = ready
    hunter = supporter(game, "Szybki")
    await won_single_fight(game, monkeypatch, hunter, seconds)
    assert game.channel.contains(f"Jedna tura zajęła Ci średnio {per_turn} sekundy!")
    speed = keys_of(game, hunter) & {"quick_1", "quick_2", "quick_3"}
    assert speed == ({expected} if expected else set())
    if expected:
        assert str(role_of(game, expected)) in announced(game, hunter)
    # The kill is counted whatever the speed.
    assert await game.bot.db.fetchval("SELECT BOSS_KILLS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1


async def test_faster_fight_replaces_the_old_speed_role(ready, monkeypatch):
    game = ready
    hunter = supporter(game, "Weteran")
    hunter.roles.append(role_of(game, "quick_1"))                  # earned in the old bot
    await won_single_fight(game, monkeypatch, hunter, 1.5)       # 0.5 s per turn
    assert keys_of(game, hunter) & {"quick_1", "quick_2", "quick_3"} == {"quick_3"}
    assert announced(game, hunter).count("Błyskawica II") == 1


async def test_slower_fight_does_not_take_a_higher_speed_role_away(ready, monkeypatch):
    game = ready
    hunter = supporter(game, "Mistrz")
    hunter.roles.append(role_of(game, "quick_3"))
    await won_single_fight(game, monkeypatch, hunter, 4.5)       # 1.5 s per turn
    assert keys_of(game, hunter) & {"quick_1", "quick_2", "quick_3"} == {"quick_3"}
    assert not any(name in announced(game, hunter) for name in ("Szybkość", "Błyskawica I"))


async def test_fast_fight_of_a_non_supporter_gives_no_speed_role(ready, monkeypatch):
    game = ready
    hunter = game.add_member("Zwykły")
    await won_single_fight(game, monkeypatch, hunter, 1.2)
    assert keys_of(game, hunter) == set() and announced(game, hunter) == []
    assert await game.bot.db.fetchval("SELECT BOSS_KILLS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1


# -- tower: "Sprinter Wieży" -------------------------------------------------------------

@pytest.mark.parametrize("level, seconds, sprint", [
    (6, 19.9, True), (10, 5, True), (6, 20, False), (6, 45, False), (5, 3, False), (3, 1, False)])
async def test_update_tower_level_gives_the_sprint_for_a_fast_high_floor(ready, level, seconds, sprint):
    game = ready
    climber = supporter(game, "Wspinacz")
    assert await tower.update_tower_level(game.scene(), climber, level, timedelta(seconds=seconds)) is True
    assert ("tower_sprint" in keys_of(game, climber)) is sprint
    assert ("Sprinter Wieży" in announced(game, climber)) is sprint


async def test_update_tower_level_reads_the_stored_time_format(ready):
    game = ready
    climber = supporter(game, "Wspinacz")
    await tower.update_tower_level(game.scene(), climber, 7, "0:00:12.500000")
    assert "tower_sprint" in keys_of(game, climber)
    await tower.update_tower_level(game.scene(), climber, 8, "0:00:12.500000")   # once only
    assert announced(game, climber).count("Sprinter Wieży") == 1


async def test_sprint_is_only_for_supporters(ready):
    game = ready
    plain = game.add_member("Zwykły")
    await tower.update_tower_level(game.scene(), plain, 10, timedelta(seconds=2))
    assert keys_of(game, plain) == set() and announced(game, plain) == []


# -- tower: the floor roles (through achievements.grant) -----------------------------------

@pytest.mark.parametrize("held, floors", [("tower_10", (3, 6)), ("tower_10", (10,)), ("tower_6", (3, 6))])
async def test_floor_role_is_not_given_back_to_a_holder_of_this_or_a_higher_tier(ready, held, floors):
    """The old bot gave the lower floor roles back every week; a supporter keeps only the best one."""
    game = ready
    climber = supporter(game, "Weteran")
    climber.roles.append(role_of(game, held))
    for level in floors:
        assert await tower.update_tower_level(game.scene(), climber, level, timedelta(seconds=45)) is True
    assert keys_of(game, climber) == {held}
    assert announced(game, climber) == [] and not game.channel.contains("zdobywa osiągnięcie")
    assert await tower._grant_floor_role(game.scene(), climber, 3) is False


async def test_higher_floor_replaces_the_lower_floor_role(ready):
    game = ready
    climber = supporter(game, "Wspinacz")
    climber.roles.append(role_of(game, "tower_3"))
    await tower.update_tower_level(game.scene(), climber, 6, timedelta(seconds=45))
    assert keys_of(game, climber) == {"tower_6"}
    assert announced(game, climber) == [str(role_of(game, "tower_6"))]
    await tower.update_tower_level(game.scene(), climber, 6, timedelta(seconds=40))   # the next week
    await tower.update_tower_level(game.scene(), climber, 3, timedelta(seconds=40))
    assert keys_of(game, climber) == {"tower_6"} and len(announced(game, climber)) == 1


@pytest.mark.parametrize("level", [3, 6, 10])
async def test_floor_role_is_only_for_supporters(ready, level):
    game = ready
    plain = game.add_member("Zwykły")
    assert await tower._grant_floor_role(game.scene(), plain, level) is False
    await tower.update_tower_level(game.scene(), plain, level, timedelta(seconds=45))
    assert keys_of(game, plain) == set() and announced(game, plain) == []


@pytest.fixture
def tower_drops(monkeypatch):
    async def rand_loot(scene, rarity, hunter, boost):
        return ""

    monkeypatch.setattr(boss, "rand_loot", rand_loot)


@pytest.mark.parametrize("level, seconds, sprint", [(6, 19.5, True), (6, 20.0, False), (5, 4.0, False)])
async def test_a_floor_fought_fast_gives_the_sprint(ready, tower_drops, monkeypatch, level, seconds, sprint):
    game = ready
    climber = supporter(game, "Wspinacz")
    fight_clock(tower, monkeypatch, seconds)
    game.auto_answer(climber)
    assert await tower.tower_fight(game.scene(climber), level) is True
    assert ("tower_sprint" in keys_of(game, climber)) is sprint
    assert ("Sprinter Wieży" in announced(game, climber)) is sprint
    assert game.bot.owner_log == []                              # no step after the floor failed


# -- events: chests ------------------------------------------------------------------------

async def test_open_chest_counts_for_the_opener(ready, loot):
    game = ready
    ala, ola = supporter(game, "Ala"), game.add_member("Ola")
    await events.open_chest(game.scene(ala, "$skrzynia"))
    await events.open_chest(game.scene(ala, "$skrzynia"))
    await events.open_chest(game.scene(ola, "$skrzynia"))
    assert [hunter for _rarity, hunter, _boost in loot] == [ala, ala, ola]
    assert await counter(game, ala, "chests") == 2 and await counter(game, ola, "chests") == 1
    assert keys_of(game, ala) == set()                           # 10 are needed


async def test_tenth_chest_gives_the_role_to_a_supporter_only(ready, loot):
    game = ready
    ala, ola = supporter(game, "Ala"), game.add_member("Ola")
    await preload(game, ala, "chests", 9)
    await preload(game, ola, "chests", 9)
    await events.open_chest(game.scene(ala, "$skrzynia"))
    await events.open_chest(game.scene(ola, "$skrzynia"))
    assert keys_of(game, ala) == {"chests_10"} and announced(game, ala) == ["Łowca skarbów I"]
    assert await counter(game, ola, "chests") == 10                 # counted all the same
    assert keys_of(game, ola) == set() and announced(game, ola) == []


async def test_a_veteran_gets_the_highest_tier_reached_at_once(ready, loot):
    game = ready
    ala = supporter(game, "Ala")
    await preload(game, ala, "chests", 60)                       # opened long before the roles existed
    await events.open_chest(game.scene(ala, "$skrzynia"))
    assert keys_of(game, ala) == {"chests_50"} and announced(game, ala) == ["Łowca skarbów II"]


async def test_database_failure_while_counting_does_not_spoil_the_chest(ready, loot):
    game = ready
    ala = supporter(game, "Ala")
    await game.bot.db.execute("DROP TABLE ACHIEVEMENT_COUNTERS", idempotent=True)
    await events.open_chest(game.scene(ala, "$skrzynia"))        # does not raise
    assert loot == [(loot[0][0], ala, loot[0][2])] and keys_of(game, ala) == set()


# -- events: duels ---------------------------------------------------------------------------

async def test_duel_counts_for_the_winner_only(ready, loot, duels):
    game = ready
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    await preload(game, ala, "duels", 4)
    join_on(game, "Pojedynek!", ala, ola)
    assert await events.spawn_duel(game.scene()) is True
    assert duels.calls == [[ala, ola]]
    assert await counter(game, ala, "duels") == 5 and await counter(game, ola, "duels") is None
    assert keys_of(game, ala) == {"duels_5"} and announced(game, ala) == ["Pojedynkowicz I"]
    assert keys_of(game, ola) == set()


async def test_higher_duel_tier_replaces_the_lower(ready, loot, duels):
    game = ready
    ala, ola = supporter(game, "Ala"), game.add_member("Ola")
    ala.roles.append(role_of(game, "duels_5"))
    await preload(game, ala, "duels", 24)
    join_on(game, "Pojedynek!", ala, ola)
    assert await events.spawn_duel(game.scene()) is True
    assert keys_of(game, ala) == {"duels_25"} and announced(game, ala) == ["Pojedynkowicz II"]


async def test_duel_draw_counts_for_nobody(ready, loot, duels):
    game = ready
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    duels.results.append((False, None))
    join_on(game, "Pojedynek!", ala, ola)
    assert await events.spawn_duel(game.scene()) is False
    assert await counter(game, ala, "duels") is None and await counter(game, ola, "duels") is None


async def test_duel_walkover_counts_for_nobody(ready, loot, duels, short_timeouts):
    game = ready
    ala = supporter(game, "Ala")
    join_on(game, "Pojedynek!", ala)
    assert await events.spawn_duel(game.scene()) is False
    assert duels.calls == [] and await counter(game, ala, "duels") is None


async def test_database_failure_while_counting_does_not_spoil_the_duel(ready, loot, duels, monkeypatch):
    game = ready
    ala, ola = supporter(game, "Ala"), game.add_member("Ola")
    join_on(game, "Pojedynek!", ala, ola)

    def database_down(*args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    monkeypatch.setattr(game.bot.db, "transaction", database_down)
    assert await events.spawn_duel(game.scene()) is True
    assert keys_of(game, ala) == set()


# -- events: tournament ----------------------------------------------------------------------

async def test_tournament_counts_fought_wins_and_the_winner(ready, loot, duels, quick, monkeypatch):
    game = ready
    monkeypatch.setattr(random, "shuffle", lambda players: None)
    monkeypatch.setattr(random, "choice", lambda pool: pool[1])
    ala, ola, ela = supporter(game, "Ala"), supporter(game, "Ola"), supporter(game, "Ela")
    join_on(game, "Tygodniowy turniej PvP!", ala, ola, ela)
    # Round 1: Ela has a bye, Ala - Ola is a draw decided by lot (Ola); round 2: Ola beats Ela.
    duels.results.extend([(False, None), (True, ola)])
    assert await events.spawn_pvp_tournament(game.scene()) == ola
    assert duels.calls == [[ala, ola], [ela, ola]]
    assert await counter(game, ola, "duels") == 1                # only the win by fighting
    assert await counter(game, ola, "tournaments") == 1
    for other in (ala, ela):
        assert await counter(game, other, "duels") is None and await counter(game, other, "tournaments") is None
    assert keys_of(game, ola) == {"tournaments_1"} and announced(game, ola) == ["Mistrz turnieju"]
    assert keys_of(game, ala) == set() and keys_of(game, ela) == set()


async def test_tournament_of_two_counts_one_duel_and_one_tournament(ready, loot, duels, quick, monkeypatch):
    game = ready
    monkeypatch.setattr(random, "shuffle", lambda players: None)
    ala, ola = supporter(game, "Ala"), game.add_member("Ola")
    join_on(game, "Tygodniowy turniej PvP!", ala, ola)
    assert await events.spawn_pvp_tournament(game.scene()) == ala
    assert await counter(game, ala, "duels") == 1 and await counter(game, ala, "tournaments") == 1
    assert await counter(game, ola, "duels") is None and await counter(game, ola, "tournaments") is None


async def test_lone_tournament_player_has_not_won_a_tournament(ready, loot, duels, quick):
    game = ready
    ala = supporter(game, "Ala")
    join_on(game, "Tygodniowy turniej PvP!", ala)
    assert await events.spawn_pvp_tournament(game.scene()) == ala
    assert loot == [(2, ala, 0)] and duels.calls == []
    assert await counter(game, ala, "tournaments") is None
    assert keys_of(game, ala) == set() and announced(game, ala) == []


# -- events: ritual ------------------------------------------------------------------------

@pytest.fixture
def boss_image(monkeypatch):
    calls: list[int] = []

    async def send_boss_image(scene, rarity):
        calls.append(rarity)
        return False, None

    monkeypatch.setattr(events.boss, "send_boss_image", send_boss_image)
    return calls


async def counted(timeout: float = 5.0) -> None:
    """Wait for the achievement counting the ritual left running in the background."""
    await asyncio.wait_for(asyncio.gather(*list(events._counting)), timeout)


async def test_successful_ritual_counts_every_participant_in_the_background(ready, boss_image, quick,
                                                                            monkeypatch):
    game = ready
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: 1.0)
    ala, ola = supporter(game, "Ala"), game.add_member("Ola")
    await preload(game, ala, "rituals", 4)
    join_on(game, "Rozpoczął się rytuał przywołania!", ala, ola)
    assert (await events.spawn_ritual(game.scene()))[0] is True
    assert boss_image == [1]
    await counted()
    assert await counter(game, ala, "rituals") == 5 and await counter(game, ola, "rituals") == 1
    # Given quietly: the summoned boss is standing on the channel, a fight may be starting.
    assert keys_of(game, ala) == {"rituals_5"} and announced(game, ala) == []
    assert not game.channel.contains("zdobywa osiągnięcie")
    assert keys_of(game, ola) == set()
    assert not events._counting                                  # the finished tasks are let go


async def test_ritual_tier_replaces_the_lower_one_quietly(ready, boss_image, quick, monkeypatch):
    game = ready
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: 1.0)
    ala = supporter(game, "Ala")
    ala.roles.append(role_of(game, "rituals_5"))
    await preload(game, ala, "rituals", 19)                      # the 20th ritual: the next tier up
    join_on(game, "Rozpoczął się rytuał przywołania!", ala)
    assert (await events.spawn_ritual(game.scene()))[0] is True
    await counted()
    assert keys_of(game, ala) == {"rituals_20"} and announced(game, ala) == []


async def test_failed_ritual_counts_nobody(ready, boss_image, quick):
    game = ready
    assert await events.spawn_ritual(game.scene()) == (False, 0, False, None)
    assert not events._counting
    assert await game.bot.db.fetchval("SELECT COUNT(*) FROM ACHIEVEMENT_COUNTERS") == 0


async def test_overtaken_ritual_counts_nobody(ready, boss_image, quick, monkeypatch):
    game = ready
    ala = supporter(game, "Ala")
    monkeypatch.setattr(game.bot.boss_cycle, "can_summon", lambda: False)  # the scheduled boss is coming
    join_on(game, "Rozpoczął się rytuał przywołania!", ala)
    assert await events.spawn_ritual(game.scene()) == (False, 0, False, None)
    assert game.channel.contains(events.MSG_RITUAL_OVERTAKEN)
    assert not events._counting and await counter(game, ala, "rituals") is None


async def test_ritual_counting_survives_a_database_failure(ready, boss_image, quick, monkeypatch):
    game = ready
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: 1.0)
    ala = supporter(game, "Ala")
    join_on(game, "Rozpoczął się rytuał przywołania!", ala)
    await game.bot.db.execute("DROP TABLE ACHIEVEMENT_COUNTERS", idempotent=True)
    assert (await events.spawn_ritual(game.scene()))[0] is True
    await counted()                                              # finishes, raises nothing
    assert keys_of(game, ala) == set()


# -- pets ----------------------------------------------------------------------------------

def mythic_egg(monkeypatch) -> None:
    rig(monkeypatch, randint=lambda low, high: 1000 if high == 1000 else 0)


async def test_mythic_egg_gives_a_supporter_the_pet_achievements(ready, no_cosmetics, monkeypatch):
    game = ready
    mythic_egg(monkeypatch)
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    assert keys_of(game, ala) == {"pet_mythic", "pet_ultra"}
    assert announced(game, ala) == ["Mityczny towarzysz", "Ultra Shiny"]
    assert game.channel.contains(f"Łowca <@{ala.id}> zdobył mitycznego towarzysza!")


async def test_mythic_egg_of_a_non_supporter_gives_no_achievement(ready, monkeypatch):
    game = ready
    mythic_egg(monkeypatch)
    ola = game.add_member("Ola")
    assert await pets.assign_pet(game.scene(), ola) is True
    assert (await pet_row(game.bot.db, 1))["mythic"] is True
    assert keys_of(game, ola) == set() and announced(game, ola) == []
    assert not any(role.id in set(achievements._stored_roles(game.bot).values()) for role in ola.roles)


async def test_ultra_shiny_egg(ready, no_cosmetics, monkeypatch):
    game = ready
    rig(monkeypatch, randint=lambda low, high: 985 if high == 1000 else 0)
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    row = await pet_row(game.bot.db, 1)
    assert row["ultra_shiny"] is True and row["mythic"] is False
    assert keys_of(game, ala) == {"pet_ultra"} and announced(game, ala) == ["Ultra Shiny"]


@pytest.mark.parametrize("seal_roll, holo_roll, expected", [
    (0, 999, {"pet_seal"}), (999, 0, {"pet_holo"}), (0, 0, {"pet_seal", "pet_holo"}), (30, 999, set())])
async def test_seal_and_holo_eggs(ready, monkeypatch, seal_roll, holo_roll, expected):
    game = ready
    rig(monkeypatch, randint=0)
    rolls = iter([seal_roll, holo_roll])
    monkeypatch.setattr(random, "randrange", lambda *args: next(rolls, 999))
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    assert keys_of(game, ala) == expected


async def test_full_stable_on_a_new_egg(ready, no_cosmetics, monkeypatch):
    game = ready
    db = game.bot.db
    rig(monkeypatch, randint=0)
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    for pet_id in (11, 12, 13, 14, 21, 22, 23):
        await add_pet(db, pet_id)
    await add_owner(db, ala.id, 0, pet_id_alt1=11, pet_id_alt2=12, pet_id_alt3=13, pet_id_alt4=14)
    await add_owner(db, ola.id, 0, pet_id_alt1=21, pet_id_alt2=22, pet_id_alt3=23)
    assert await pets.assign_pet(game.scene(), ala) is True
    assert await pets.assign_pet(game.scene(), ola) is True
    assert (await owner_row(db, ala.id))["pet_id"] > 0
    assert keys_of(game, ala) == {"stable_full"} and announced(game, ala) == ["Pełna stajnia"]
    assert keys_of(game, ola) == set()                           # four places of five


async def test_level_up_to_the_divine_talent(ready, monkeypatch):
    game = ready
    db = game.bot.db
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    await add_owner(db, ala.id, 5)
    await add_pet(db, 5, pet_lvl=2, pet_skills=8)
    await add_owner(db, ola.id, 6)
    await add_pet(db, 6, pet_lvl=1, pet_skills=7)
    rig(monkeypatch, randint=lambda low, high: low)              # +1 talent point
    assert await pets.level_up_pet(game.scene(), ala.id) is True
    assert await pets.level_up_pet(game.scene(), str(ola.id)) is True
    assert (await pet_row(db, 5))["pet_skills"] == 9 and (await pet_row(db, 6))["pet_skills"] == 8
    assert keys_of(game, ala) == {"pet_divine"} and announced(game, ala) == ["Boski talent"]
    assert keys_of(game, ola) == set()


@pytest.mark.parametrize("broken", ["_pet_keys", "grant"])
async def test_a_failing_pet_achievement_never_breaks_the_egg_or_the_level_up(ready, no_cosmetics, monkeypatch,
                                                                              broken):
    game = ready
    db = game.bot.db
    mythic_egg(monkeypatch)

    async def boom(*args, **kwargs):
        raise DatabaseUnavailable("test: database is down") if broken == "grant" else RuntimeError("boom")

    monkeypatch.setattr(achievements, broken, boom)
    ala = supporter(game, "Ala")
    assert await pets.assign_pet(game.scene(), ala) is True
    assert (await owner_row(db, ala.id))["pet_id"] == 1 and (await pet_row(db, 1))["mythic"] is True
    rig(monkeypatch, randint=lambda low, high: low)
    assert await pets.level_up_pet(game.scene(), ala.id) is True
    assert (await pet_row(db, 1))["pet_lvl"] == 1
    assert keys_of(game, ala) == set() and announced(game, ala) == []
