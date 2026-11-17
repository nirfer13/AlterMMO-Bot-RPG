"""features/twitch.py + cogs/twitch.py: roles and treasures from the Twitch bot's tables."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from altermmo.cogs.twitch import TwitchCog
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import twitch

HOUR = 3600
ROLE_NAMES = {
    **dict(zip(twitch.VIEWER_ROLES, ("Podgladacz", "Ogladacz", "Widz", "Pasjonata", "Fanboy"))),
    **dict(zip(twitch.MESSAGER_ROLES, ("Analfabeta", "Niepisaty", "Czatownik", "Skryba", "Wieszcz"))),
    twitch.VIP_ROLE: "VIP",
    twitch.MOD_ROLE: "Moderator",
}


@pytest.fixture
def tw(game_db):
    """``game_db`` with every Twitch role present on the server."""
    for role_id, name in ROLE_NAMES.items():
        game_db.guild.add_role(role_id, name)
    return game_db


async def add_chatter(game, name, discord_id=None, *, time=0, comments=0, vip=False, mod=False) -> None:
    await game.bot.db.execute(
        """INSERT INTO chatters_information (chtr_name, chtr_time, chtr_comments, chtr_isvip, chtr_ismod, discord_id)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        name, time, comments, vip, mod, None if discord_id is None else str(discord_id))


async def add_treasure(game, name) -> None:
    await game.bot.db.execute("INSERT INTO treasure_table (chtr_name, windatetime) VALUES ($1, $2)",
                              name, "2026-11-17 20:00")


async def treasures(game) -> list[str]:
    rows = await game.bot.db.fetch("SELECT chtr_name FROM treasure_table ORDER BY chtr_id")
    return [row["chtr_name"] for row in rows]


def role_ids(member) -> set[int]:
    return {role.id for role in member.roles}


@pytest.fixture
def loot(monkeypatch):
    calls: list[tuple] = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        calls.append((scene.channel.id, rarity, hunter, boost_percent))
        return ""

    monkeypatch.setattr(twitch.daily, "rand_loot", rand_loot)
    return calls


# -- role tables ---------------------------------------------------------------------

@pytest.mark.parametrize("hours, index", [(124.9, None), (125, 0), (249.9, 0), (250, 1), (375, 2),
                                          (500, 3), (749, 3), (750, 4), (5000, 4)])
def test_viewer_role_thresholds(hours, index):
    role_id, others = twitch.get_twitch_viewer_role(int(hours * HOUR))
    assert role_id == (0 if index is None else twitch.VIEWER_ROLES[index])
    assert others == [role for role in twitch.VIEWER_ROLES if role != role_id]


@pytest.mark.parametrize("messages, index", [(999, None), (1000, 0), (3599, 0), (3600, 1), (7500, 2),
                                             (15000, 3), (29999, 3), (30000, 4)])
def test_messager_role_thresholds(messages, index):
    role_id, others = twitch.get_twitch_messager_role(messages)
    assert role_id == (0 if index is None else twitch.MESSAGER_ROLES[index])
    assert others == [role for role in twitch.MESSAGER_ROLES if role != role_id]


# -- watch time ----------------------------------------------------------------------

async def test_watchtime_roles(tw):
    game = tw
    newcomer = game.add_member("Nowy")
    climber = game.add_member("Awans", roles=[twitch.VIEWER_ROLES[0]])
    veteran = game.add_member("Weteran", roles=[twitch.VIEWER_ROLES[2]])
    settled = game.add_member("Staly", roles=[twitch.VIEWER_ROLES[0]])
    short = game.add_member("Krotko")
    await add_chatter(game, "nowy_ttv", newcomer.id, time=130 * HOUR)
    await add_chatter(game, "nowy_alt", newcomer.id, time=124 * HOUR)       # second account, less time
    await add_chatter(game, "awans", climber.id, time=260 * HOUR)
    await add_chatter(game, "weteran", veteran.id, time=130 * HOUR)        # holds a better role already
    await add_chatter(game, "staly", settled.id, time=200 * HOUR)
    await add_chatter(game, "krotko", short.id, time=124 * HOUR)           # above 445000 s, below 125 h
    await add_chatter(game, "o'neil", 123456789, time=900 * HOUR)          # left the server
    await add_chatter(game, "smiec", "nie-liczba", time=900 * HOUR)
    await add_chatter(game, "pusty", "", time=900 * HOUR)
    await add_chatter(game, "bez_discorda", None, time=900 * HOUR)

    assert await twitch.assign_roles_watchtime(game.bot) == 2
    assert role_ids(newcomer) == {twitch.VIEWER_ROLES[0]}
    assert role_ids(climber) == {twitch.VIEWER_ROLES[1]}
    assert role_ids(veteran) == {twitch.VIEWER_ROLES[2]}
    assert role_ids(settled) == {twitch.VIEWER_ROLES[0]}
    assert role_ids(short) == set()
    notices = [m.content for m in game.log_channel.messages]   # debug mode: the log channel
    assert sorted(notices) == sorted([   # GROUP BY gives no particular order
        f"<@{newcomer.id}> zdobywa rolę Podgladacz za śledzenie streamów na Twitchu!",
        f"<@{climber.id}> zdobywa rolę Ogladacz za śledzenie streamów na Twitchu!"])

    # Running it again changes nothing and announces nothing.
    assert await twitch.assign_roles_watchtime(game.bot) == 0
    assert len(game.log_channel.messages) == 2


async def test_watchtime_announces_on_the_notice_channel_in_production(tw):
    game = tw
    game.bot.debug = False
    fan = game.add_member("Fan")
    await add_chatter(game, "fan", fan.id, time=800 * HOUR)
    assert await twitch.assign_roles_watchtime(game.bot) == 1
    assert game.notice_channel.contains(f"<@{fan.id}> zdobywa rolę Fanboy za śledzenie streamów na Twitchu!")
    assert game.log_channel.messages == []


async def test_missing_role_is_reported_not_fatal(game_db):
    game = game_db
    game.guild.add_role(twitch.VIEWER_ROLES[1], "Ogladacz")
    fan, viewer = game.add_member("Fan"), game.add_member("Widz")
    await add_chatter(game, "fan", fan.id, time=800 * HOUR)       # role does not exist on the server
    await add_chatter(game, "widz", viewer.id, time=260 * HOUR)
    assert await twitch.assign_roles_watchtime(game.bot) == 1
    assert role_ids(viewer) == {twitch.VIEWER_ROLES[1]}
    assert any(str(twitch.VIEWER_ROLES[4]) in line for line in game.bot.owner_log)


async def test_role_that_cannot_be_added_is_not_announced(tw, monkeypatch):
    game = tw
    fan = game.add_member("Fan")
    await add_chatter(game, "fan", fan.id, time=800 * HOUR)

    async def add_roles(*roles, reason=None):
        raise RuntimeError("Missing Permissions")

    monkeypatch.setattr(fan, "add_roles", add_roles)
    assert await twitch.assign_roles_watchtime(game.bot) == 0
    assert game.log_channel.messages == []


# -- messages ------------------------------------------------------------------------

async def test_message_roles(tw):
    game = tw
    writer = game.add_member("Pisarz")
    climber = game.add_member("Awans", roles=[twitch.MESSAGER_ROLES[0]])
    quiet = game.add_member("Cichy")
    await add_chatter(game, "pisarz", writer.id, comments=1200)
    await add_chatter(game, "pisarz2", writer.id, comments=400)
    await add_chatter(game, "awans", climber.id, comments=8000)
    await add_chatter(game, "cichy", quiet.id, comments=999)
    await add_chatter(game, "odszedl", 123456789, comments=50000)

    assert await twitch.assign_roles_messages(game.bot) == 2
    assert role_ids(writer) == {twitch.MESSAGER_ROLES[0]}
    assert role_ids(climber) == {twitch.MESSAGER_ROLES[2]}
    assert role_ids(quiet) == set()
    assert game.log_channel.contains(f"<@{writer.id}> zdobywa rolę Analfabeta za pisanie na Twitchu!")
    assert game.log_channel.contains(f"<@{climber.id}> zdobywa rolę Czatownik za pisanie na Twitchu!")
    assert await twitch.assign_roles_messages(game.bot) == 0


# -- VIP / moderator -----------------------------------------------------------------

async def test_vip_and_mod_roles(tw):
    game = tw
    vip, mod, both, plain = (game.add_member(name) for name in ("Vip", "Mod", "Oba", "Zwykly"))
    old_vip = game.add_member("StaryVip", roles=[twitch.VIP_ROLE])
    await add_chatter(game, "vip", vip.id, vip=True)
    await add_chatter(game, "mod", mod.id, mod=True)
    await add_chatter(game, "oba", both.id, vip=True, mod=True)
    await add_chatter(game, "zwykly", plain.id)
    await add_chatter(game, "staryvip", old_vip.id, vip=True)
    await add_chatter(game, "odszedl", 123456789, vip=True)

    assert await twitch.assign_roles_vip_mod(game.bot) == 3
    assert role_ids(vip) == {twitch.VIP_ROLE}
    assert role_ids(mod) == {twitch.MOD_ROLE}
    assert role_ids(both) == {twitch.VIP_ROLE}
    assert role_ids(plain) == set()
    assert game.log_channel.contains(f"<@{vip.id}> zdobywa rolę VIP za VIPa na Twitchu!")
    assert game.log_channel.contains(f"<@{mod.id}> zdobywa rolę Moderator za moderatora na Twitchu!")
    assert len(game.log_channel.messages) == 3


# -- linked account ------------------------------------------------------------------

async def test_get_user_twitch_name(tw):
    game = tw
    admin, ala = game.add_member("Admin"), game.add_member("Ala")
    await add_chatter(game, "o'neil_ttv", ala.id, time=50)
    await add_chatter(game, "ala_main", ala.id, time=500)
    scene = game.scene(admin, "$checkTwitch")
    assert await twitch.get_user_twitch_name(scene, ala.id) == "ala_main"
    assert game.channel.contains(f"Użytkownik <@{ala.id}> ma połączone konto Twitch: **ala_main, o'neil_ttv**.")
    assert await twitch.get_user_twitch_name(scene, "987654321") is None
    assert game.channel.contains("Użytkownik <@987654321> nie ma połączonego konta Twitch.")
    assert await twitch.get_user_twitch_name(scene, "x'; DROP TABLE treasure_table; --") is None
    assert await treasures(game) == []


# -- treasures -----------------------------------------------------------------------

async def test_check_treasure_pays_and_deletes_row_by_row(tw, loot, monkeypatch):
    game = tw
    monkeypatch.setattr(random, "randint", lambda low, high: high)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await add_chatter(game, "o'neil", ala.id)
    await add_chatter(game, "ola_ttv", ola.id)
    await add_chatter(game, "odszedl", 123456789)
    await add_chatter(game, "niepolaczony", None)
    for name in ("o'neil", "nieznany", "odszedl", "niepolaczony", "ola_ttv", "o'neil"):
        await add_treasure(game, name)

    assert await twitch.check_treasure(game.bot) == 3
    boss_channel = game.bot.ids.boss_channel
    assert loot == [(boss_channel, 2, ala, 25), (boss_channel, 2, ola, 25), (boss_channel, 2, ala, 25)]
    assert await treasures(game) == []
    assert any("nieznany" in line and "odszedl" in line and "niepolaczony" in line for line in game.bot.owner_log)


async def test_check_treasure_one_failure_does_not_lose_the_others(tw, monkeypatch):
    game = tw
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola"), game.add_member("Ela")
    for member in (ala, ola, ela):
        await add_chatter(game, member.name.lower(), member.id)
        await add_treasure(game, member.name.lower())
    paid = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        if hunter == ola:
            raise RuntimeError("Discord 503")
        paid.append(hunter)

    monkeypatch.setattr(twitch.daily, "rand_loot", rand_loot)
    assert await twitch.check_treasure(game.bot) == 2   # legacy: every row was deleted up front
    assert paid == [ala, ela]
    assert await treasures(game) == ["ola"]
    assert any("`ola`" in line for line in game.bot.owner_log)

    # The next run pays the one that failed.
    async def works(scene, rarity, hunter, boost_percent):
        paid.append(hunter)

    monkeypatch.setattr(twitch.daily, "rand_loot", works)
    assert await twitch.check_treasure(game.bot) == 1
    assert paid == [ala, ela, ola]
    assert await treasures(game) == []


async def test_check_treasure_database_down_keeps_unpaid_rows(tw, monkeypatch):
    game = tw
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    for member in (ala, ola):
        await add_chatter(game, member.name.lower(), member.id)
        await add_treasure(game, member.name.lower())
    paid = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        if hunter == ola:
            raise DatabaseUnavailable("down")
        paid.append(hunter)

    monkeypatch.setattr(twitch.daily, "rand_loot", rand_loot)
    with pytest.raises(DatabaseUnavailable):
        await twitch.check_treasure(game.bot)
    assert paid == [ala]
    assert await treasures(game) == ["ola"]


async def test_check_treasure_nothing_to_do(tw, loot):
    assert await twitch.check_treasure(tw.bot) == 0
    assert loot == [] and tw.bot.owner_log == [] and tw.channel.sent == []


async def test_check_treasure_with_real_loot(tw, monkeypatch):
    game = tw
    monkeypatch.setattr(random, "random", lambda: 1.0)
    ala = game.add_member("O'Brien")
    await add_chatter(game, "obrien", ala.id)
    await add_treasure(game, "obrien")
    assert await twitch.check_treasure(game.bot) == 1
    assert game.channel.contains("Drop - O'Brien")
    assert await treasures(game) == []


# -- cog -----------------------------------------------------------------------------

def test_cog_commands_are_admin_only(game):
    cog = TwitchCog(game.bot)
    commands = {command.name: command for command in cog.get_commands()}
    assert set(commands) == {"checkTwitch", "getActiveTwitch", "getVipModTwitch", "getWriterTwitch", "checkTreasure"}
    assert all(len(command.checks) == 1 for command in commands.values())


async def test_cog_commands_call_the_feature(tw, loot):
    game = tw
    admin, ala = game.add_member("Admin"), game.add_member("Ala")
    await add_chatter(game, "ala_ttv", ala.id, time=800 * HOUR, comments=40000, vip=True)
    await add_treasure(game, "ala_ttv")
    cog = TwitchCog(game.bot)
    ctx = SimpleNamespace(bot=game.bot, channel=game.channel, author=admin, message=None)

    await cog.check_twitch.callback(cog, ctx, f"<@{ala.id}>")
    assert game.channel.contains("ma połączone konto Twitch: **ala_ttv**")
    await cog.assign_roles_watchtime.callback(cog, ctx)
    await cog.assign_roles_messages.callback(cog, ctx)
    await cog.assign_roles_vip_mod.callback(cog, ctx)
    assert role_ids(ala) == {twitch.VIEWER_ROLES[4], twitch.MESSAGER_ROLES[4], twitch.VIP_ROLE}
    await cog.check_treasure.callback(cog, ctx)
    assert [call[2] for call in loot] == [ala]
