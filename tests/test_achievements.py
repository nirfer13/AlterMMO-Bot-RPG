"""features/achievements.py: counters against a real PostgreSQL, roles against the fake guild."""

from __future__ import annotations

import asyncio

import pytest

from altermmo.core.db import DatabaseUnavailable
from altermmo.features import achievements, patrons

from fakes import FakeMember

PATRON = patrons.PATRON_ROLES[0]
KILL_1, KILL_10, KILL_30 = achievements.BOSS_KILL_ROLES.values()
HUNT_10, HUNT_35, HUNT_100 = achievements.HUNT_ROLES.values()
BUTCHER_1, BUTCHER_3, BUTCHER_10 = achievements.BUTCHER_ROLES.values()
TOWER_3, TOWER_6, TOWER_10 = achievements.TOWER_ROLES

ROLE_NAMES = {
    KILL_1: "Pogromca I", KILL_10: "Pogromca II", KILL_30: "Pogromca III",
    HUNT_10: "Łowca I", HUNT_35: "Łowca II", HUNT_100: "Łowca III",
    BUTCHER_1: "Rzeźnik I", BUTCHER_3: "Rzeźnik II", BUTCHER_10: "Rzeźnik III",
    TOWER_3: "Wieża I", TOWER_6: "Wieża II", TOWER_10: "Wieża III",
    achievements.UNICORN_ROLE: "Jednorożec",
    achievements.LEGEND_ROLE: "Legenda",
    achievements.QUICKNESS_ROLE: "Szybki jak wiatr",
}


@pytest.fixture
def roles(game):
    """The achievement roles as they exist on the server."""
    return {role_id: game.guild.add_role(role_id, name) for role_id, name in ROLE_NAMES.items()}


def role_ids(member) -> set[int]:
    return {role.id for role in member.roles} - set(patrons.SUPPORTER_ROLES)


async def stats(bot, player_id: int) -> tuple | None:
    row = await bot.db.fetchrow(
        "SELECT BOSS_KILLS, HUNTS, BUTCHERS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", player_id)
    return None if row is None else tuple(None if value is None else int(value) for value in row)


class DatabaseDown:
    """A database that cannot be reached at all."""

    def _fail(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    transaction = _fail

    async def execute(self, *args, **kwargs):
        self._fail()

    fetch = fetchrow = fetchval = execute


# -- legacy rules, pure -------------------------------------------------------------

def test_role_ids_and_milestones_are_the_legacy_ones():
    assert achievements.BOSS_KILL_ROLES == {1: 1125799224966131712, 10: 1125799392989949973,
                                            30: 1125799470509084762}
    assert achievements.HUNT_ROLES == {10: 1125799891923374121, 35: 1125800644826112043,
                                       100: 1125800721724489758}
    assert achievements.BUTCHER_ROLES == {1: 1125801055758860329, 3: 1125801161631486033,
                                          10: 1125801216375529563}
    assert achievements.TOWER_ROLES == (1151427369886826496, 1151427619741511710, 1151427790973964328)
    assert achievements.UNICORN_ROLE == 1125819170534211664
    assert achievements.LEGEND_ROLE == 1125818827809226862
    assert achievements.QUICKNESS_ROLE == 1125818642450370590


def test_higher_tier_replaces_lower_tiers_as_in_the_legacy_table():
    legacy = {
        1125799392989949973: [1125799224966131712],
        1125799470509084762: [1125799392989949973, 1125799224966131712],
        1125800644826112043: [1125799891923374121],
        1125800721724489758: [1125799891923374121, 1125800644826112043],
        1125801161631486033: [1125801055758860329],
        1125801216375529563: [1125801055758860329, 1125801161631486033],
        1151427619741511710: [1151427369886826496],
        1151427790973964328: [1151427369886826496, 1151427619741511710],
    }
    for role_id in ROLE_NAMES:
        assert sorted(achievements.lower_tier_roles(role_id)) == sorted(legacy.get(role_id, []))
    assert achievements.lower_tier_roles(123) == ()


# -- database -----------------------------------------------------------------------

async def test_schema_matches_the_legacy_table(game_db):
    columns = await game_db.bot.db.fetch(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_name = 'achievements' ORDER BY ordinal_position""")
    assert [(row["column_name"], row["data_type"]) for row in columns] == [
        ("player_id", "numeric"), ("boss_kills", "numeric"), ("hunts", "numeric"), ("butchers", "numeric")]
    primary_key = await game_db.bot.db.fetchval(
        """SELECT a.attname FROM pg_index i
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
           WHERE i.indrelid = 'achievements'::regclass AND i.indisprimary""")
    assert primary_key == "player_id"


async def test_create_database_never_destroys_data(game_db):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    await achievements.add_boss_kill(game_db.scene(), player)
    await achievements.create_database(game_db.scene())     # legacy: DROP TABLE + CREATE
    await achievements.create_database(bot)                 # "create all databases" passes no ctx
    assert await stats(bot, player.id) == (1, 0, 0)

    await bot.db.execute("DROP TABLE ACHIEVEMENTS")
    await achievements.create_database(game_db.scene())
    assert await bot.db.fetchval("SELECT count(*) FROM ACHIEVEMENTS") == 0


async def test_new_record_is_inserted_once(game_db):
    bot = game_db.bot
    assert await achievements.new_record_achievements(bot, 42, 1, 0, 0) is True
    assert await achievements.new_record_achievements(bot, 42, 5, 5, 5) is False
    assert await stats(bot, 42) == (1, 0, 0)


async def test_every_counter_has_its_own_column(game_db):
    bot = game_db.bot
    scene = game_db.scene()
    player = game_db.add_member("O'Neil")                   # apostrophes used to break legacy SQL
    assert await achievements.add_hunts(scene, player) is True
    assert await stats(bot, player.id) == (0, 1, 0)         # row created like the legacy INSERT
    assert await achievements.add_boss_kill(scene, player) is True
    assert await achievements.add_boss_kill(scene, player) is True
    assert await achievements.add_butcher(scene, player) is True
    assert await stats(bot, player.id) == (2, 1, 1)
    assert await bot.db.fetchval("SELECT count(*) FROM ACHIEVEMENTS") == 1


async def test_null_counter_is_treated_as_zero(game_db):
    bot = game_db.bot
    player = game_db.add_member("Ala")
    await bot.db.execute("INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS) VALUES ($1, 4)", player.id)
    await achievements.add_hunts(game_db.scene(), player)
    await achievements.add_boss_kill(game_db.scene(), player)
    assert await stats(bot, player.id) == (5, 1, None)


async def test_boss_kill_milestones_for_a_patron(game_db, roles):
    bot = game_db.bot
    scene = game_db.scene()
    patron = game_db.add_member("O'Neil", roles=[PATRON])

    await achievements.add_boss_kill(scene, patron)
    assert role_ids(patron) == {KILL_1}
    assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Pogromca I!"

    for _ in range(8):
        await achievements.add_boss_kill(scene, patron)
    assert role_ids(patron) == {KILL_1} and game_db.channel.count("zdobywa osiągnięcie") == 1

    await achievements.add_boss_kill(scene, patron)         # the 10th kill
    assert role_ids(patron) == {KILL_10}                    # the lower tier is taken away
    assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Pogromca II!"

    for _ in range(20):
        await achievements.add_boss_kill(scene, patron)     # ... the 30th
    assert role_ids(patron) == {KILL_30}
    assert game_db.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: Pogromca III!"
    assert game_db.channel.count("zdobywa osiągnięcie") == 3
    assert await stats(bot, patron.id) == (30, 0, 0)


@pytest.mark.parametrize("function, counts, expected", [
    ("add_hunts", (10, 35, 100), (HUNT_10, HUNT_35, HUNT_100)),
    ("add_butcher", (1, 3, 10), (BUTCHER_1, BUTCHER_3, BUTCHER_10)),
])
async def test_hunt_and_butcher_milestones(game_db, roles, function, counts, expected):
    scene = game_db.scene()
    crafter = game_db.add_member("Crafter", roles=[patrons.CRAFTER_ROLE])
    add = getattr(achievements, function)
    done = 0
    for count, role_id in zip(counts, expected):
        while done < count - 1:
            await add(scene, crafter)
            done += 1
        before = role_ids(crafter)
        assert role_id not in before
        await add(scene, crafter)
        done += 1
        assert role_ids(crafter) == {role_id}
        assert game_db.channel.messages[-1].content == (
            f"<@{crafter.id}> zdobywa osiągnięcie: {ROLE_NAMES[role_id]}!")
    assert game_db.channel.count("zdobywa osiągnięcie") == 3


@pytest.mark.parametrize("role_id", [patrons.TWITCH_SUB_ROLE, *patrons.YOUTUBE_ROLES])
async def test_twitch_subscribers_and_youtube_supporters_get_the_roles(game_db, roles, role_id):
    """Since 2026-11-20 every supporter collects the achievement roles, not only patrons and crafters."""
    scene = game_db.scene()
    supporter = game_db.add_member("Widz", roles=[role_id])
    await achievements.add_boss_kill(scene, supporter)
    assert role_ids(supporter) == {KILL_1}
    assert game_db.channel.messages[-1].content == f"<@{supporter.id}> zdobywa osiągnięcie: Pogromca I!"
    for award, role in ((achievements.unicorn, achievements.UNICORN_ROLE),
                        (achievements.legend, achievements.LEGEND_ROLE),
                        (achievements.quickness, achievements.QUICKNESS_ROLE)):
        await award(scene, supporter)
        assert role in role_ids(supporter)


async def test_non_patron_is_counted_but_gets_no_role(game_db, roles):
    scene = game_db.scene()
    player = game_db.add_member("Zwykły")
    for _ in range(10):
        await achievements.add_boss_kill(scene, player)
        await achievements.add_hunts(scene, player)
    await achievements.add_butcher(scene, player)
    assert await stats(game_db.bot, player.id) == (10, 10, 1)
    assert role_ids(player) == set()
    assert not game_db.channel.contains("zdobywa osiągnięcie")


async def test_role_is_awarded_only_when_the_milestone_is_hit_as_a_patron(game_db, roles):
    """Legacy rule: no catching up - becoming a patron at kill 2 gives nothing until kill 10."""
    scene = game_db.scene()
    player = game_db.add_member("Ala")
    await achievements.add_boss_kill(scene, player)
    player.roles.append(game_db.guild.add_role(PATRON))
    for _ in range(8):
        await achievements.add_boss_kill(scene, player)
    assert role_ids(player) == set()
    await achievements.add_boss_kill(scene, player)
    assert role_ids(player) == {KILL_10}


async def test_simultaneous_hunts_are_all_counted_and_the_role_is_given_once(game_db, roles):
    """The legacy SELECT + UPDATE lost increments when two fights ended together."""
    patron = game_db.add_member("Ala", roles=[PATRON])
    scene = game_db.scene()
    await asyncio.gather(*(achievements.add_hunts(scene, patron) for _ in range(10)))
    assert await stats(game_db.bot, patron.id) == (0, 10, 0)
    assert role_ids(patron) == {HUNT_10}
    assert game_db.channel.count("zdobywa osiągnięcie: Łowca I!") == 1


async def test_winner_who_left_the_server_does_not_crash_the_season_reset(game_db, roles):
    bot = game_db.bot
    scene = game_db.scene()
    # guild.get_member() gave None in the legacy weekly reset -> AttributeError, ranking never reset.
    assert await achievements.add_butcher(scene, None) is False
    assert await bot.db.fetchval("SELECT count(*) FROM ACHIEVEMENTS") == 0

    # A stale member object (still carrying the patron role) of somebody who left meanwhile.
    gone = FakeMember("Były Patron")
    gone.roles.append(game_db.guild.add_role(PATRON))
    assert await achievements.add_butcher(scene, gone) is True
    assert await stats(bot, gone.id) == (0, 0, 1)
    assert role_ids(gone) == set() and not game_db.channel.contains("zdobywa osiągnięcie")

    # A bare id works too: counted, and the role goes to the member when they are still here.
    patron = game_db.add_member("Ala", roles=[PATRON])
    assert await achievements.add_butcher(scene, patron.id) is True
    assert role_ids(patron) == {BUTCHER_1}
    assert await achievements.add_butcher(scene, 555) is True      # id of somebody who left
    assert await stats(bot, 555) == (0, 0, 1)


async def test_database_outage_propagates_and_awards_nothing(game, roles):
    """Legacy: a failed SELECT counted as "new player" and re-awarded the first-kill role."""
    game.bot.db = DatabaseDown()
    patron = game.add_member("Ala", roles=[PATRON])
    scene = game.scene()
    for add in (achievements.add_boss_kill, achievements.add_hunts, achievements.add_butcher):
        with pytest.raises(DatabaseUnavailable):
            await add(scene, patron)
    with pytest.raises(DatabaseUnavailable):
        await achievements.new_record_achievements(game.bot, patron.id, 1, 0, 0)
    with pytest.raises(DatabaseUnavailable):
        await achievements.create_database(scene)
    assert role_ids(patron) == set()
    assert not game.channel.contains("zdobywa osiągnięcie")
    assert not game.bot.state.busy


async def test_failed_increment_leaves_nothing_behind_and_can_be_repeated(game_db, roles, monkeypatch):
    bot = game_db.bot
    patron = game_db.add_member("Ala", roles=[PATRON])
    scene = game_db.scene()
    original = achievements._INCREMENT_SQL["BOSS_KILLS"]
    monkeypatch.setitem(achievements._INCREMENT_SQL, "BOSS_KILLS", original + " ; SELECT broken")
    with pytest.raises(Exception):
        await achievements.add_boss_kill(scene, patron)
    assert await stats(bot, patron.id) is None and role_ids(patron) == set()
    monkeypatch.setitem(achievements._INCREMENT_SQL, "BOSS_KILLS", original)
    await achievements.add_boss_kill(scene, patron)          # the pool is still usable
    assert await stats(bot, patron.id) == (1, 0, 0) and role_ids(patron) == {KILL_1}


# -- one-off achievements -------------------------------------------------------------

@pytest.mark.parametrize("function, role_id", [
    ("unicorn", achievements.UNICORN_ROLE),
    ("legend", achievements.LEGEND_ROLE),
    ("quickness", achievements.QUICKNESS_ROLE),
])
async def test_one_off_achievements(game, roles, function, role_id):
    scene = game.scene()
    award = getattr(achievements, function)
    patron = game.add_member("O'Neil", roles=[PATRON])
    plain = game.add_member("Zwykły")

    assert await award(scene, plain) is True
    assert role_ids(plain) == set() and not game.channel.contains("zdobywa osiągnięcie")

    assert await award(scene, patron) is True
    assert role_ids(patron) == {role_id}
    assert game.channel.messages[-1].content == f"<@{patron.id}> zdobywa osiągnięcie: {ROLE_NAMES[role_id]}!"

    assert await award(scene, patron) is True                # already has it: silent
    assert game.channel.count("zdobywa osiągnięcie") == 1
    assert await award(scene, None) is True                  # nobody: nothing happens


# -- roles ----------------------------------------------------------------------------

async def test_tower_tiers_replace_each_other(game, roles):
    scene = game.scene()
    player = game.add_member("Ala")                          # the patron check is the tower's job
    assert await achievements.set_achiev_role(scene, player, TOWER_3) is True
    assert await achievements.set_achiev_role(scene, player, TOWER_6) is True
    assert role_ids(player) == {TOWER_6}
    assert await achievements.set_achiev_role(scene, player, TOWER_6) is False
    player.roles.append(roles[TOWER_3])                      # e.g. handed back by a moderator
    assert await achievements.set_achiev_role(scene, player, TOWER_10) is True
    assert role_ids(player) == {TOWER_10}
    assert game.channel.count("zdobywa osiągnięcie") == 3


async def test_lower_tier_is_kept_when_the_player_already_has_the_higher_one(game, roles):
    """Legacy: lower tiers are only cleaned up at the moment the higher role is granted."""
    scene = game.scene()
    player = game.add_member("Ala")
    player.roles.extend([roles[KILL_1], roles[KILL_10]])
    assert await achievements.set_achiev_role(scene, player, KILL_10) is False
    assert role_ids(player) == {KILL_1, KILL_10}
    assert not game.channel.contains("zdobywa osiągnięcie")


async def test_role_deleted_on_the_server_is_reported_not_raised(game):
    player = game.add_member("Ala", roles=[PATRON])
    scene = game.scene()
    assert await achievements.set_achiev_role(scene, player, achievements.LEGEND_ROLE) is False
    assert await achievements.legend(scene, player) is True
    assert any(str(achievements.LEGEND_ROLE) in line for line in game.bot.owner_log)
    assert not game.channel.contains("zdobywa osiągnięcie")


async def test_player_who_left_gets_no_role_and_no_announcement(game, roles):
    scene = game.scene()
    gone = FakeMember("Były Gracz")
    assert await achievements.set_achiev_role(scene, gone, achievements.UNICORN_ROLE) is False
    assert await achievements.set_achiev_role(scene, None, achievements.UNICORN_ROLE) is False
    assert not game.channel.contains("zdobywa osiągnięcie")


async def test_role_that_cannot_be_given_is_not_announced(game, roles):
    player = game.add_member("Ala")
    player.roles.append(roles[KILL_1])

    async def forbidden(*args, **kwargs):
        raise RuntimeError("403 Forbidden: Missing Permissions")

    player.add_roles = forbidden
    assert await achievements.set_achiev_role(game.scene(), player, KILL_10) is False
    assert role_ids(player) == {KILL_1}                      # nothing taken away either
    assert not game.channel.contains("zdobywa osiągnięcie")
    assert any("Pogromca II" in line for line in game.bot.owner_log)


async def test_lost_announcement_does_not_abort_the_caller(game, roles):
    """The role is given; a Discord error on the message must not break the reward flow."""
    patron = game.add_member("Ala", roles=[PATRON])
    patron.roles.append(roles[BUTCHER_1])
    game.channel.fail_with = RuntimeError("discord is down")
    game.channel.fail_sends = 1
    assert await achievements.set_achiev_role(game.scene(), patron, BUTCHER_3) is True
    assert role_ids(patron) == {BUTCHER_3}
    assert not game.channel.contains("zdobywa osiągnięcie")


async def test_role_is_checked_on_the_fresh_member_not_on_a_stale_object(game, roles):
    current = game.add_member("Ala")
    current.roles.append(roles[achievements.LEGEND_ROLE])
    stale = FakeMember("Ala", member_id=current.id)          # e.g. captured before the fight
    stale.roles.append(game.guild.add_role(PATRON))
    assert await achievements.legend(game.scene(), stale) is True
    assert not game.channel.contains("zdobywa osiągnięcie")  # compared by id: already has it
