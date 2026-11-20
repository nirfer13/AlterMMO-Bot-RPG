"""features/achievements.py: counters against a real PostgreSQL, roles against the fake guild."""

from __future__ import annotations

import asyncio

import pytest

from altermmo import schema
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


async def counter(bot, player_id: int, name: str) -> int | None:
    value = await bot.db.fetchval(
        "SELECT VALUE FROM ACHIEVEMENT_COUNTERS WHERE PLAYER_ID = $1 AND NAME = $2", player_id, name)
    return None if value is None else int(value)


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


def test_schema_has_both_tables_and_only_the_counters_are_new_in_v2():
    assert len(achievements.SCHEMA) == 2
    assert "TABLE IF NOT EXISTS ACHIEVEMENTS (" in achievements.SCHEMA[0]
    assert "TABLE IF NOT EXISTS ACHIEVEMENT_COUNTERS (" in achievements.SCHEMA[1]
    # The old bot's database has ACHIEVEMENTS; only the counters are made at start-up.
    assert achievements.NEW_TABLES == (achievements.SCHEMA[1],)
    assert all(statement.lstrip().startswith("CREATE TABLE IF NOT EXISTS") for statement in achievements.SCHEMA)
    assert "altermmo.features.achievements" in schema.SCHEMA_MODULES


async def test_counters_table_is_made_when_missing_and_never_emptied(game_db):
    bot = game_db.bot
    scene = game_db.scene()
    player = game_db.add_member("Ala")
    await achievements.add_boss_kill(scene, player)
    assert await achievements.add_count(scene, player, "duels", 3) == 3
    await achievements.create_database(scene)
    assert await counter(bot, player.id, "duels") == 3        # kept
    columns = await bot.db.fetch(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_name = 'achievement_counters' ORDER BY ordinal_position""")
    assert [(row["column_name"], row["data_type"]) for row in columns] == [
        ("player_id", "numeric"), ("name", "character varying"), ("value", "numeric")]

    await bot.db.execute("DROP TABLE ACHIEVEMENT_COUNTERS")  # the old bot's database at the first start
    await schema.create_new_tables(bot.db)
    assert await achievements.add_count(scene, player, "duels") == 1
    await bot.db.execute("DROP TABLE ACHIEVEMENT_COUNTERS")
    await achievements.create_database(bot)
    assert await achievements.add_count(scene, player, "chests") == 1
    assert await stats(bot, player.id) == (1, 0, 0)          # ACHIEVEMENTS untouched throughout


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


async def test_late_supporter_gets_the_highest_tier_reached_at_the_next_counted_deed(game_db, roles):
    """Owner's decision of 2026-11-20, replacing the legacy "only when the milestone is hit exactly".

    A player who starts to support after passing milestones gets the highest tier the
    counter has reached as soon as the next deed is counted (not the lowest tier, and not
    only at the next exact milestone); a higher tier then replaces the lower one.
    """
    bot = game_db.bot
    scene = game_db.scene()
    player = game_db.add_member("Ala")
    for _ in range(12):
        await achievements.add_boss_kill(scene, player)
    assert role_ids(player) == set()                         # counted, but not a supporter: no role
    assert not game_db.channel.contains("zdobywa osiągnięcie")

    player.roles.append(game_db.guild.add_role(PATRON))
    await achievements.add_boss_kill(scene, player)          # the 13th kill, the first as a supporter
    assert role_ids(player) == {KILL_10}                     # the highest tier reached, not KILL_1
    assert game_db.channel.count("zdobywa osiągnięcie") == 1
    assert game_db.channel.messages[-1].content == f"<@{player.id}> zdobywa osiągnięcie: Pogromca II!"

    for _ in range(16):
        await achievements.add_boss_kill(scene, player)      # ... the 29th: nothing new
    assert role_ids(player) == {KILL_10} and game_db.channel.count("zdobywa osiągnięcie") == 1
    await achievements.add_boss_kill(scene, player)          # the 30th
    assert role_ids(player) == {KILL_30}                     # replaces KILL_10
    assert game_db.channel.count("zdobywa osiągnięcie") == 2
    assert await stats(bot, player.id) == (30, 0, 0)


async def test_catching_up_replaces_the_lower_tier_the_supporter_held(game_db, roles):
    """A supporter holding a low tier while the counter is far beyond it (e.g. a lost role)."""
    scene = game_db.scene()
    patron = game_db.add_member("Ala", roles=[PATRON])
    patron.roles.append(roles[HUNT_10])
    await game_db.bot.db.execute("INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS, HUNTS, BUTCHERS) "
                                 "VALUES ($1, 0, 99, 0)", patron.id)
    await achievements.add_hunts(scene, patron)              # the 100th hunt
    assert role_ids(patron) == {HUNT_100}                    # HUNT_10 taken away, HUNT_35 skipped
    assert game_db.channel.count("zdobywa osiągnięcie") == 1


async def test_player_who_stops_supporting_gets_no_new_role(game_db, roles):
    """Deeds are still counted, but only supporters get roles; what was given stays."""
    scene = game_db.scene()
    player = game_db.add_member("Ala", roles=[PATRON])
    await achievements.add_boss_kill(scene, player)
    assert role_ids(player) == {KILL_1}
    player.roles = [role for role in player.roles if role.id != PATRON]
    for _ in range(40):
        await achievements.add_boss_kill(scene, player)
    assert role_ids(player) == {KILL_1}
    assert game_db.channel.count("zdobywa osiągnięcie") == 1
    assert await stats(game_db.bot, player.id) == (41, 0, 0)


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
    """As in the legacy bot: lower tiers are only cleaned up at the moment a higher role is granted."""
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


async def test_owner_hears_about_a_missing_or_ungivable_role_once_per_run(game, roles):
    """Not at every deed of every player: once per role until the next restart."""
    bot = game.bot
    scene = game.scene()
    players = [game.add_member(f"Gracz {number}") for number in range(3)]
    for player in players:
        assert await achievements.set_achiev_role(scene, player, 424242) is False      # deleted on the server
        assert await achievements.set_achiev_role(scene, player, 434343) is False
    missing = [line for line in bot.owner_log if line.startswith("Rola osiągnięcia")]
    assert len(missing) == 2 and "424242" in missing[0] and "434343" in missing[1]
    assert f"<@{players[0].id}>" in missing[0]

    async def forbidden(*args, **kwargs):
        raise RuntimeError("403 Forbidden: Missing Permissions")

    for player in players:
        player.add_roles = forbidden                         # the role is above the bot's own
        for role_id in (KILL_10, KILL_10, HUNT_10):
            assert await achievements.set_achiev_role(scene, player, role_id) is False
    not_given = [line for line in bot.owner_log if line.startswith("Nie udało się nadać roli osiągnięcia")]
    assert len(not_given) == 2 and "Pogromca II" in not_given[0] and "Łowca I " in not_given[1]
    assert len(bot.owner_log) == 4
    assert all(role_ids(player) == set() for player in players)
    assert not game.channel.contains("zdobywa osiągnięcie")


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
