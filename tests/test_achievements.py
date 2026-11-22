"""features/achievements.py: the counters against a real PostgreSQL, giving a role, and the roles of the
withdrawn achievements that the bot deletes from the server.

Owner's decision of 2026-11-20 (evening): only the mythic achievements stay; every other achievement
role goes - the 15 roles of the old bot's achievements and the ones the bot made earlier that day -
and the bot deletes them itself.  A deleted role cannot be brought back with its members, so
:func:`achievements.retire_roles` is tested here for what it must never touch as much as for what it
deletes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import discord
import pytest

from altermmo import schema
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import achievements, patrons
from altermmo.settings import Ids

from fakes import FakeMember, FakeRole

PATRON = patrons.PATRON_ROLES[0]
GAME_CHANNELS = ("channel", "announce_channel", "notice_channel")
PET_KEYS = ("pet_mythic", "pet_mythic_holo", "pet_mythic_seal")

#: The old bot's achievement roles by what they were for (the ids of :data:`achievements.RETIRED_ROLES`).
KILL_1, KILL_10, KILL_30 = 1125799224966131712, 1125799392989949973, 1125799470509084762
HUNT_10, HUNT_35, HUNT_100 = 1125799891923374121, 1125800644826112043, 1125800721724489758
BUTCHER_1, BUTCHER_3, BUTCHER_10 = 1125801055758860329, 1125801161631486033, 1125801216375529563
TOWER_3, TOWER_6, TOWER_10 = 1151427369886826496, 1151427619741511710, 1151427790973964328
UNICORN, LEGEND, QUICK = 1125819170534211664, 1125818827809226862, 1125818642450370590

#: The roles the bot made earlier on 2026-11-20 for achievements withdrawn the same evening
#: (``{key in data/achievementRoles.json: role name}``).
EARLIER_MADE = {
    "boss_kills_100": "Pogromca bossów IV", "boss_kills_250": "Pogromca bossów V",
    "boss_kills_500": "Pogromca bossów VI", "hunts_250": "Myśliwy IV", "hunts_500": "Myśliwy V",
    "hunts_1000": "Myśliwy VI", "butchers_25": "Rzeźnik potworów IV", "tower_sprint": "Sprinter Wieży",
    "quick_2": "Błyskawica I", "quick_3": "Błyskawica II", "pet_holo": "Folia Holo",
    "pet_seal": "Pieczęć Arcyłowcy", "pet_ultra": "Ultra Shiny", "pet_divine": "Boski talent",
    "stable_full": "Pełna stajnia", "tournaments_1": "Mistrz turnieju", "duels_5": "Pojedynkowicz I",
    "duels_25": "Pojedynkowicz II", "chests_10": "Łowca skarbów I", "chests_50": "Łowca skarbów II",
    "rituals_5": "Kultysta I", "rituals_20": "Kultysta II",
}


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


def forbidden() -> discord.Forbidden:
    """What Discord answers when the bot may not change a role (it is above the bot's own)."""
    return discord.Forbidden(type("Response", (), {"status": 403, "reason": "Forbidden"})(), "Missing Permissions")


def role_ids(member) -> set[int]:
    return {role.id for role in member.roles} - set(patrons.SUPPORTER_ROLES)


def guild_role_ids(game) -> set[int]:
    return {role.id for role in game.guild.roles}


def pings(game) -> int:
    """Messages on the owner's log channel that ping the owner."""
    return game.log_channel.sent_count(f"<@{game.bot.ids.owner}>")


def owner_lines(bot, start: str) -> list[str]:
    return [line for line in bot.owner_log if line.startswith(start)]


def stored(bot) -> dict:
    """The saved role ids as one mapping (the file keeps the later achievements' apart:
    ``achievements.LATER_ROLES_KEY``)."""
    saved = dict(bot.store.achievement_roles.load())
    saved.update(saved.pop(achievements.LATER_ROLES_KEY, {}))
    return saved


def game_channel_messages(game) -> int:
    return sum(len(getattr(game, name).sent) for name in GAME_CHANNELS)


def announcements(game) -> int:
    return sum(getattr(game, name).sent_count("zdobywa osiągnięcie") for name in GAME_CHANNELS)


# -- the counters: the legacy table ------------------------------------------------------------

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


def test_schema_is_only_the_legacy_table():
    assert len(achievements.SCHEMA) == 1
    assert achievements.SCHEMA[0].lstrip().startswith("CREATE TABLE IF NOT EXISTS ACHIEVEMENTS (")
    assert getattr(achievements, "NEW_TABLES", ()) == ()     # the old bot's database has the table
    assert "altermmo.features.achievements" in schema.SCHEMA_MODULES
    assert "achievement_counters" not in schema.EXPECTED_COLUMNS
    assert not any("ACHIEVEMENT_COUNTERS" in statement.upper() for statement in schema.all_statements())


async def test_counters_table_of_the_withdrawn_achievements_is_neither_made_nor_dropped(game_db):
    """Made on 2026-11-20 for achievements withdrawn the same day: left alone, never dropped."""
    db = game_db.bot.db
    assert await db.fetchval("SELECT to_regclass('achievement_counters')") is None   # not made any more
    await db.execute("CREATE TABLE ACHIEVEMENT_COUNTERS (PLAYER_ID NUMERIC, NAME VARCHAR(64), VALUE NUMERIC)")
    await db.execute("INSERT INTO ACHIEVEMENT_COUNTERS VALUES (42, 'duels', 7)")
    await achievements.create_database(game_db.scene())
    await achievements.create_database(game_db.bot)
    await schema.create_all(db)
    await schema.create_new_tables(db)
    assert await db.fetchval("SELECT VALUE FROM ACHIEVEMENT_COUNTERS WHERE PLAYER_ID = 42") == 7


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


async def test_simultaneous_deeds_are_all_counted(game_db):
    """The legacy SELECT + UPDATE lost increments when two fights ended together."""
    player = game_db.add_member("Ala", roles=[PATRON])
    scene = game_db.scene()
    await asyncio.gather(*(achievements.add_hunts(scene, player) for _ in range(10)),
                         *(achievements.add_boss_kill(scene, player) for _ in range(5)))
    assert await stats(game_db.bot, player.id) == (5, 10, 0)


async def test_counted_deeds_give_no_role_even_at_the_old_milestones(game_db):
    """Since 2026-11-20 (evening) the counters are statistics only: no tier role, no announcement."""
    bot = game_db.bot
    db = bot.db
    scene = game_db.scene()
    for role_id, name in achievements.RETIRED_ROLES.items():      # still on the server, e.g. before the start
        game_db.guild.add_role(role_id, name)
    patron = game_db.add_member("Patron", roles=[PATRON])
    roles_before = guild_role_ids(game_db)

    await achievements.add_boss_kill(scene, patron)               # the old "first kill"
    await achievements.add_butcher(scene, patron)                 # the old "first season"
    for kills, hunts, butchers in ((9, 9, 2), (29, 34, 9), (29, 99, 9)):
        await db.execute("UPDATE ACHIEVEMENTS SET BOSS_KILLS = $2, HUNTS = $3, BUTCHERS = $4 WHERE PLAYER_ID = $1",
                         patron.id, kills, hunts, butchers)
        await achievements.add_boss_kill(scene, patron)           # 10, 30, 30
        await achievements.add_hunts(scene, patron)               # 10, 35, 100
        await achievements.add_butcher(scene, patron)             # 3, 10, 10
    assert await stats(bot, patron.id) == (30, 100, 10)
    assert role_ids(patron) == set()
    assert guild_role_ids(game_db) == roles_before                # no role made either
    assert stored(bot) == {}
    assert announcements(game_db) == 0 and game_channel_messages(game_db) == 0
    assert bot.owner_log == []


async def test_player_who_left_the_server_does_not_crash_the_season_reset(game_db):
    bot = game_db.bot
    scene = game_db.scene()
    # guild.get_member() gave None in the legacy weekly reset -> AttributeError, ranking never reset.
    assert await achievements.add_butcher(scene, None) is False
    assert await bot.db.fetchval("SELECT count(*) FROM ACHIEVEMENTS") == 0

    gone = FakeMember("Były Patron")                        # a stale member object of somebody who left
    gone.roles.append(game_db.guild.add_role(PATRON))
    assert await achievements.add_butcher(scene, gone) is True
    assert await stats(bot, gone.id) == (0, 0, 1)
    assert await achievements.add_butcher(scene, 555) is True      # a bare id of somebody who left
    assert await stats(bot, 555) == (0, 0, 1)
    assert role_ids(gone) == set() and announcements(game_db) == 0


async def test_database_outage_propagates_and_gives_nothing(game):
    """Legacy: a failed SELECT counted as "new player" and gave the first-kill role again."""
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
    assert role_ids(patron) == set() and announcements(game) == 0
    assert not game.bot.state.busy


async def test_failed_increment_leaves_nothing_behind_and_can_be_repeated(game_db, monkeypatch):
    bot = game_db.bot
    player = game_db.add_member("Ala", roles=[PATRON])
    scene = game_db.scene()
    original = achievements._INCREMENT_SQL["BOSS_KILLS"]
    monkeypatch.setitem(achievements._INCREMENT_SQL, "BOSS_KILLS", original + " ; SELECT broken")
    with pytest.raises(Exception):
        await achievements.add_boss_kill(scene, player)
    assert await stats(bot, player.id) is None
    monkeypatch.setitem(achievements._INCREMENT_SQL, "BOSS_KILLS", original)
    await achievements.add_boss_kill(scene, player)          # the pool is still usable
    assert await stats(bot, player.id) == (1, 0, 0)


# -- giving a role ------------------------------------------------------------------------------

@pytest.fixture
def slayer(game) -> FakeRole:
    """The role of the mythic boss (the game's own role, named as on the server)."""
    role = game.guild.get_role(game.bot.ids.mythic_slayer_role)
    role.name = "Mityczny boss"
    return role


async def test_set_achiev_role_gives_and_announces_once(game, slayer):
    scene = game.scene()
    player = game.add_member("Ala")                          # the supporter check is the caller's job
    assert await achievements.set_achiev_role(scene, player, slayer.id) is True
    assert role_ids(player) == {slayer.id}
    assert game.channel.messages[-1].content == f"<@{player.id}> zdobywa osiągnięcie: Mityczny boss!"
    assert await achievements.set_achiev_role(scene, player, slayer.id) is False     # already holds it
    assert announcements(game) == 1 and game.bot.owner_log == []


async def test_set_achiev_role_without_announcement_sends_nothing(game, slayer):
    player = game.add_member("Ala")
    assert await achievements.set_achiev_role(game.scene(), player, slayer.id, announce=False) is True
    assert role_ids(player) == {slayer.id}
    assert game_channel_messages(game) == 0


async def test_player_who_left_gets_no_role_and_no_announcement(game, slayer):
    scene = game.scene()
    gone = FakeMember("Były Gracz")
    assert await achievements.set_achiev_role(scene, gone, slayer.id) is False
    assert await achievements.set_achiev_role(scene, None, slayer.id) is False
    assert await achievements.set_achiev_role(scene, 555, slayer.id) is False
    assert role_ids(gone) == set() and announcements(game) == 0


async def test_role_is_checked_on_the_fresh_member_not_on_a_stale_object(game, slayer):
    current = game.add_member("Ala")
    current.roles.append(slayer)
    stale = FakeMember("Ala", member_id=current.id)          # e.g. captured before the fight
    assert await achievements.set_achiev_role(game.scene(), stale, slayer.id) is False
    assert announcements(game) == 0                          # compared by id: already has it


async def test_lost_announcement_does_not_abort_the_caller(game, slayer):
    """The role is given; a Discord error on the message must not break the reward flow."""
    player = game.add_member("Ala")
    game.channel.fail_with = RuntimeError("discord is down")
    game.channel.fail_sends = 1
    assert await achievements.set_achiev_role(game.scene(), player, slayer.id) is True
    assert role_ids(player) == {slayer.id}
    assert announcements(game) == 0


async def test_owner_hears_about_a_missing_or_ungivable_role_once_per_run(game, slayer):
    """Not at every deed of every player: once per role until the next restart."""
    bot = game.bot
    scene = game.scene()
    holo = game.guild.add_role(990_000_000_000_000_100, "Mityczny Holo")
    players = [game.add_member(f"Gracz {number}") for number in range(3)]
    for player in players:
        assert await achievements.set_achiev_role(scene, player, 424242) is False      # deleted on the server
        assert await achievements.set_achiev_role(scene, player, 434343) is False
    missing = owner_lines(bot, "Rola osiągnięcia")
    assert len(missing) == 2 and "424242" in missing[0] and "434343" in missing[1]
    assert f"<@{players[0].id}>" in missing[0]

    async def refused(*args, **kwargs):
        raise forbidden()

    for player in players:
        player.add_roles = refused                           # the role is above the bot's own
        for role_id in (slayer.id, slayer.id, holo.id):
            assert await achievements.set_achiev_role(scene, player, role_id) is False
    not_given = owner_lines(bot, "Nie udało się nadać roli osiągnięcia")
    assert len(not_given) == 2 and "Mityczny boss" in not_given[0] and "Mityczny Holo" in not_given[1]
    assert len(bot.owner_log) == 4
    assert all(role_ids(player) == set() for player in players)
    assert announcements(game) == 0


# -- the roles of the withdrawn achievements --------------------------------------------------------

class TodaysServer:
    """The server as the start of 2026-11-20 (evening) finds it.

    * the 15 roles of the old bot's achievements (:data:`achievements.RETIRED_ROLES`);
    * the 22 roles the bot made earlier that day for achievements withdrawn since
      (``made``), and the three pet roles of the mythic achievements that stay
      (``current``) - all of them saved in ``achievementRoles.json``;
    * the mythic boss's role (``slayer``) and roles that have nothing to do with
      achievements (``others``): the season winner's, the game's, the supporters',
      a moderator's, and roles named like a withdrawn one under another id.
    """

    def __init__(self, game) -> None:
        guild = game.guild
        bot = game.bot
        self.old = {role_id: guild.add_role(role_id, name) for role_id, name in achievements.RETIRED_ROLES.items()}
        self.made = {key: guild.add_role(990_000_000_000_000_100 + index, name)
                     for index, (key, name) in enumerate(EARLIER_MADE.items())}
        self.current = {key: guild.add_role(990_000_000_000_000_200 + index, achievements.trophy(key).name)
                        for index, key in enumerate(PET_KEYS)}
        self.saved = {**{key: role.id for key, role in self.made.items()},
                      **{key: role.id for key, role in self.current.items()}}
        bot.store.achievement_roles.save(self.saved)
        self.slayer = guild.get_role(bot.ids.mythic_slayer_role)
        self.slayer.name = "Mityczny boss"
        self.winner = guild.get_role(bot.ids.boss_slayer_role)
        assert self.winner.id == 983798433590673448
        self.winner.name = "Rzeźnik Potworów"
        self.others = [
            self.winner,
            guild.get_role(bot.ids.hunters_role), guild.get_role(bot.ids.dead_hunter_role),
            *(guild.add_role(role_id, "Wspierający") for role_id in patrons.SUPPORTER_ROLES),
            guild.add_role(980_000_000_000_000_001, "Moderator"),
            guild.add_role(980_000_000_000_000_002, "zabite bossy I"),       # a withdrawn role's name
            guild.add_role(980_000_000_000_000_003, "Folia Holo"),           # a role the bot made: its name
            guild.add_role(980_000_000_000_000_004, "legendarny boss"),
        ]
        self.keep = [self.slayer, *self.current.values(), *self.others]
        self.veteran = game.add_member("Weteran", roles=[PATRON])
        self.veteran.roles += [self.old[KILL_30], self.old[LEGEND], self.old[TOWER_10], self.made["pet_holo"],
                               self.made["quick_3"], self.current["pet_mythic"], self.current["pet_mythic_holo"],
                               self.slayer, self.winner, self.others[-3], self.others[-2]]
        self.player = game.add_member("Gracz")
        self.player.roles += [self.old[KILL_30], self.old[HUNT_10], self.others[-4]]

    @property
    def withdrawn(self) -> list[FakeRole]:
        return [*self.old.values(), *self.made.values()]

    @property
    def withdrawn_names(self) -> list[str]:
        """What :func:`retire_roles` reports, in its order: the old bot's roles, then the saved ones."""
        return [*achievements.RETIRED_ROLES.values(), *EARLIER_MADE.values()]


@pytest.fixture
def server(game) -> TodaysServer:
    return TodaysServer(game)


async def test_retire_roles_deletes_every_withdrawn_role_and_nothing_else(game, server):
    bot = game.bot
    others_before = guild_role_ids(game) - {role.id for role in server.withdrawn}

    deleted = await achievements.retire_roles(bot)

    assert deleted == server.withdrawn_names and len(deleted) == 15 + 22
    assert not guild_role_ids(game) & {role.id for role in server.withdrawn}
    assert all(game.guild.get_role(role_id) is None for role_id in achievements.RETIRED_ROLES)
    assert all(role.deleted_with == achievements.RETIRE_REASON for role in server.withdrawn)
    assert guild_role_ids(game) == others_before                       # every other role is still there
    for role in server.keep:
        assert game.guild.get_role(role.id) is role and not hasattr(role, "deleted_with"), role.name
    # The members lose only the deleted roles.
    assert {role.id for role in server.veteran.roles} == {
        PATRON, server.current["pet_mythic"].id, server.current["pet_mythic_holo"].id, server.slayer.id,
        server.winner.id, 980_000_000_000_000_002, 980_000_000_000_000_003}
    assert {role.id for role in server.player.roles} == {980_000_000_000_000_001}
    # The file keeps the roles of the achievements that stay.
    assert stored(bot) == {key: role.id for key, role in server.current.items()}
    assert bot._achievement_roles == stored(bot)
    # One summary for the owner, without a ping; nothing on the game channels.
    assert bot.owner_log == [achievements.LOG_ROLES_RETIRED.format(count=37, names=", ".join(deleted))]
    assert "Kultysta II" in bot.owner_log[0] and "zabite bossy I" in bot.owner_log[0]
    assert pings(game) == 0 and game_channel_messages(game) == 0


async def test_the_roles_of_the_mythic_achievements_are_never_deleted(game):
    """Even when the server has nothing else: the mythic boss, the three pet roles and the album's stay."""
    bot = game.bot
    slayer = game.guild.get_role(bot.ids.mythic_slayer_role)
    await achievements.ensure_roles(bot)
    pets = {key: achievements.role_id_of(bot, achievements.trophy(key)) for key in (*PET_KEYS, "album_full")}
    assert all(pets.values())
    holder = game.add_member("Ala", roles=[PATRON, slayer.id, *pets.values()])
    before = guild_role_ids(game)
    assert await achievements.retire_roles(bot) == []
    assert guild_role_ids(game) == before and stored(bot) == pets
    assert {role.id for role in holder.roles} == {PATRON, slayer.id, *pets.values()}


async def test_who_held_a_role_is_logged_before_it_is_deleted(game, server, caplog):
    bot = game.bot
    role = server.old[KILL_30]
    seen_at_deletion: list[list[str]] = []

    async def deleting(*, reason=None):
        seen_at_deletion.append([record.getMessage() for record in caplog.records])
        await FakeRole.delete(role, reason=reason)

    role.delete = deleting
    with caplog.at_level(logging.INFO, logger="altermmo.features.achievements"):
        await achievements.retire_roles(bot)

    def line_about(role_id, messages):
        return [message for message in messages if str(role_id) in message and "being deleted" in message]

    [before] = line_about(KILL_30, seen_at_deletion[0])            # written before the role went
    assert "held by 2 member(s)" in before
    assert str(server.veteran.id) in before and str(server.player.id) in before
    assert game.guild.get_role(KILL_30) is None
    [nobody] = line_about(UNICORN, [record.getMessage() for record in caplog.records])
    assert "held by 0 member(s)" in nobody
    [made] = line_about(server.made["pet_holo"].id, [record.getMessage() for record in caplog.records])
    assert "Folia Holo" in made and str(server.veteran.id) in made and str(server.player.id) not in made


async def test_a_role_that_cannot_be_deleted_stays_is_reported_once_and_goes_later(game, server):
    bot = game.bot
    stubborn_old = server.old[LEGEND]
    stubborn_made = server.made["pet_holo"]
    stubborn_old.delete_error = forbidden()                     # above the bot's own role
    stubborn_made.delete_error = RuntimeError("503 Service Unavailable")

    deleted = await achievements.retire_roles(bot)
    assert len(deleted) == 37 - 2 and "legendarny boss" not in deleted and "Folia Holo" not in deleted
    assert game.guild.get_role(LEGEND) is stubborn_old and game.guild.get_role(stubborn_made.id) is stubborn_made
    assert {LEGEND, stubborn_made.id} <= {role.id for role in server.veteran.roles}   # its holders keep it
    assert stored(bot) == {"pet_holo": stubborn_made.id,                             # remembered for later
                           **{key: role.id for key, role in server.current.items()}}
    assert owner_lines(bot, "Nie udało się usunąć ról wycofanych osiągnięć") == [
        achievements.LOG_ROLES_NOT_RETIRED.format(names="legendarny boss, Folia Holo")]
    assert owner_lines(bot, "Usunąłem z serwera role") == [
        achievements.LOG_ROLES_RETIRED.format(count=35, names=", ".join(deleted))]
    assert pings(game) == 1                                     # the failure pings the owner

    assert await achievements.retire_roles(bot) == []          # $roleosiagniec: still refused ...
    assert len(bot.owner_log) == 2 and pings(game) == 1         # ... the owner was told already

    stubborn_old.delete_error = None                            # the owner moved the roles below the bot's
    stubborn_made.delete_error = None
    assert await achievements.retire_roles(bot) == ["legendarny boss", "Folia Holo"]
    assert game.guild.get_role(LEGEND) is None and game.guild.get_role(stubborn_made.id) is None
    assert stored(bot) == {key: role.id for key, role in server.current.items()}
    assert owner_lines(bot, "Usunąłem z serwera role")[-1] == achievements.LOG_ROLES_RETIRED.format(
        count=2, names="legendarny boss, Folia Holo")
    assert pings(game) == 1


async def test_a_role_that_fails_again_after_a_restart_is_reported_again(game, server):
    """Once per run: the next start tells the owner again (the role is still there)."""
    bot = game.bot
    server.old[QUICK].delete_error = forbidden()
    await achievements.retire_roles(bot)
    bot._achievement_told = set()                               # what a restart forgets
    assert await achievements.retire_roles(bot) == []
    assert owner_lines(bot, "Nie udało się usunąć") == [
        achievements.LOG_ROLES_NOT_RETIRED.format(names="szybkość")] * 2
    assert pings(game) == 2 and game.guild.get_role(QUICK) is not None


async def test_saved_ids_of_withdrawn_roles_that_are_already_gone_are_forgotten(game):
    bot = game.bot
    holo = game.guild.add_role(990_000_000_000_000_200, "Mityczny Holo")
    bot.store.achievement_roles.save({"duels_5": 990_000_000_000_000_998, "pet_mythic_holo": holo.id,
                                      "pet_holo": 990_000_000_000_000_999})   # deleted by hand already
    before = guild_role_ids(game)
    assert await achievements.retire_roles(bot) == []
    assert stored(bot) == {"pet_mythic_holo": holo.id}
    assert guild_role_ids(game) == before and bot.owner_log == []


async def test_a_second_call_does_nothing_and_tells_nobody(game, server):
    bot = game.bot
    assert len(await achievements.retire_roles(bot)) == 37
    roles, file, told = guild_role_ids(game), stored(bot), list(bot.owner_log)
    assert await achievements.retire_roles(bot) == []
    assert guild_role_ids(game) == roles and stored(bot) == file and bot.owner_log == told
    assert len(game.log_channel.sent) == 1


async def test_nothing_to_retire_on_a_clean_server(game):
    bot = game.bot
    before = guild_role_ids(game)
    assert await achievements.retire_roles(bot) == []
    assert guild_role_ids(game) == before and bot.owner_log == []
    assert not bot.store.achievement_roles.path.exists()        # no file written for nothing


async def test_a_withdrawn_key_whose_role_an_achievement_still_uses_is_kept(game, monkeypatch, caplog):
    """A role is deleted only when no achievement that stays uses it - whatever the lists say."""
    bot = game.bot
    slayer = game.guild.get_role(bot.ids.mythic_slayer_role)
    towarzysz = game.guild.add_role(990_000_000_000_000_200, "Mityczny towarzysz")
    saved = {"pet_mythic": towarzysz.id, "pet_holo": towarzysz.id, "legend": slayer.id}
    bot.store.achievement_roles.save(saved)
    monkeypatch.setitem(achievements.RETIRED_ROLES, slayer.id, "Mityczny boss (wrongly listed)")
    monkeypatch.setitem(achievements.RETIRED_ROLES, towarzysz.id, "Mityczny towarzysz (wrongly listed)")
    holder = game.add_member("Ala", roles=[PATRON, slayer.id])
    holder.roles.append(towarzysz)
    with caplog.at_level(logging.ERROR, logger="altermmo.features.achievements"):
        assert await achievements.retire_roles(bot) == []
    assert game.guild.get_role(slayer.id) is slayer and game.guild.get_role(towarzysz.id) is towarzysz
    assert {slayer.id, towarzysz.id} <= {role.id for role in holder.roles}
    assert stored(bot) == saved and bot.owner_log == []
    assert sum("still uses it; kept" in record.getMessage() for record in caplog.records) == 4


async def test_a_saved_id_that_points_at_a_game_or_supporter_role_is_never_deleted(game):
    """E.g. a role the owner made by hand that the bot took over by its name earlier that day, or a
    hand-edited file: the season winner's role or a supporter role must never be deleted."""
    bot = game.bot
    winner = game.guild.get_role(bot.ids.boss_slayer_role)
    patron = game.guild.add_role(PATRON, "Patron")
    hunters = game.guild.get_role(bot.ids.hunters_role)
    bot.store.achievement_roles.save({"pet_holo": winner.id, "duels_5": patron.id, "notes": hunters.id})
    await achievements.retire_roles(bot)
    assert game.guild.get_role(winner.id) is winner
    assert game.guild.get_role(PATRON) is patron
    assert game.guild.get_role(hunters.id) is hunters


def break_file(path, how: str) -> None:
    """Make the saved role ids unreadable: corrupt text, or a directory in the file's place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if how == "directory":
        path.mkdir()                                         # reading it raises an OSError
    else:
        path.write_text(how, encoding="utf-8")


@pytest.mark.parametrize("broken", ["[1, 2, ", '{"pet_holo": 99', "[1, 2]", "directory"])
async def test_nothing_is_deleted_while_the_roles_file_is_unreadable(game, broken):
    bot = game.bot
    old = {role_id: game.guild.add_role(role_id, name) for role_id, name in achievements.RETIRED_ROLES.items()}
    holo = game.guild.add_role(990_000_000_000_000_100, "Folia Holo")
    path = bot.store.achievement_roles.path
    break_file(path, broken)
    before = guild_role_ids(game)

    assert await achievements.retire_roles(bot) == []
    assert await achievements.retire_roles(bot) == []
    assert guild_role_ids(game) == before
    assert bot.owner_log == []                              # (ensure_roles tells the owner, see start_up)
    await achievements.start_up(bot)                        # retire_roles -> ensure_roles -> sync_veterans
    assert guild_role_ids(game) == before                   # nothing deleted, nothing made
    assert owner_lines(bot, "Nie mogę odczytać pliku z rolami osiągnięć") == [
        achievements.LOG_ROLES_FILE_UNREADABLE.format(path=path)]

    if path.is_dir():                                       # the owner mends the file
        path.rmdir()
    bot.store.achievement_roles.save({"pet_holo": holo.id})
    deleted = await achievements.retire_roles(bot)
    assert deleted == [*achievements.RETIRED_ROLES.values(), "Folia Holo"]
    assert not guild_role_ids(game) & {*old, holo.id}
    assert stored(bot) == {}


@pytest.mark.parametrize("outage", ["no guild", "unavailable"])
async def test_nothing_is_deleted_without_the_server(game, server, monkeypatch, outage):
    bot = game.bot
    if outage == "no guild":
        get_guild = bot.get_guild
        monkeypatch.setattr(bot, "get_guild", lambda guild_id: None)
    else:
        monkeypatch.setattr(game.guild, "unavailable", True, raising=False)
    before = guild_role_ids(game)
    assert await achievements.retire_roles(bot) == []
    assert guild_role_ids(game) == before and stored(bot) == server.saved
    assert bot.owner_log == [] and len(game.log_channel.sent) == 0

    if outage == "no guild":                                # the server is back
        monkeypatch.setattr(bot, "get_guild", get_guild)
    else:
        monkeypatch.setattr(game.guild, "unavailable", False)
    assert len(await achievements.retire_roles(bot)) == 37


async def test_retire_roles_never_raises(game, server, monkeypatch):
    bot = game.bot

    def broken(role_id):
        raise RuntimeError("test: the cache is broken")

    lookup = game.guild.get_role
    monkeypatch.setattr(game.guild, "get_role", broken)
    assert await achievements.retire_roles(bot) == []
    monkeypatch.setattr(game.guild, "get_role", lookup)
    assert all(game.guild.get_role(role.id) is role for role in server.withdrawn)

    def unsaveable(value):
        raise OSError("test: disk full")

    monkeypatch.setattr(bot.store.achievement_roles, "save", unsaveable)
    deleted = await achievements.retire_roles(bot)          # logged, not raised
    assert deleted[:15] == list(achievements.RETIRED_ROLES.values())
    assert all(game.guild.get_role(role.id) is role for role in server.keep)


async def test_retire_roles_lets_a_cancellation_through(game, server):
    server.old[KILL_1].delete_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await achievements.retire_roles(game.bot)
    assert all(game.guild.get_role(role.id) is role for role in server.keep)


def test_the_withdrawn_roles_are_not_the_game_or_supporter_roles():
    ids = Ids()
    game_roles = {getattr(ids, item.name) for item in dataclasses.fields(ids) if item.name.endswith("_role")}
    assert 983798433590673448 in game_roles
    assert not set(achievements.RETIRED_ROLES) & game_roles
    assert not set(achievements.RETIRED_ROLES) & set(patrons.SUPPORTER_ROLES)
