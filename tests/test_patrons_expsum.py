from __future__ import annotations

import random
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import altermmo
from altermmo.cogs import help as help_cog
from altermmo.features import achievements, daily, events, expsum, hub, modifiers, patrons, tower, twitch
from altermmo.settings import Ids

#: The roles the owner named on 2026-11-20 (Twitch subscribers, members of the two YouTube channels).
TWITCH_SUB = 1199797849660063834
YOUTUBE_1, YOUTUBE_2 = 891314182459371520, 1546795477176025120


async def test_patron_detection_and_random_pick(game):
    plain = game.add_member("Zwykly")
    crafter = game.add_member("Crafter", roles=[patrons.CRAFTER_ROLE])
    patron = game.add_member("Patron", roles=[patrons.PATRON_ROLES[2]])
    assert not patrons.check_if_patron(plain)
    assert patrons.check_if_patron(crafter) and patrons.check_if_patron(patron)
    assert patrons.get_patron(game.bot) in (crafter, patron)


async def test_get_patron_without_patrons_returns_none(game):
    assert patrons.get_patron(game.bot) is None


def test_supporter_roles_are_patrons_crafters_twitch_subscribers_and_youtube_supporters():
    assert patrons.TWITCH_SUB_ROLE == TWITCH_SUB
    assert patrons.YOUTUBE_ROLES == (YOUTUBE_1, YOUTUBE_2)
    assert set(patrons.SUPPORTER_ROLES) == {patrons.CRAFTER_ROLE, *patrons.PATRON_ROLES, TWITCH_SUB,
                                            YOUTUBE_1, YOUTUBE_2}
    assert len(patrons.SUPPORTER_ROLES) == len(set(patrons.SUPPORTER_ROLES)) == 8
    assert len(patrons.PATRON_ROLES) == 4                                # the legacy tiers are untouched


@pytest.mark.parametrize("role_id", [TWITCH_SUB, YOUTUBE_1, YOUTUBE_2])
async def test_twitch_subscribers_and_youtube_supporters_are_supporters(game, role_id):
    """Owner's decision of 2026-11-20: holding only one of these roles is enough."""
    game.add_member("Zwykly")
    member = game.add_member("Widz", roles=[role_id])
    assert [role.id for role in member.roles] == [role_id]
    assert patrons.check_if_patron(member)
    assert patrons.get_patron(game.bot) is member                        # the only supporter on the server


async def test_an_unrelated_role_makes_nobody_a_supporter(game):
    others = [twitch.VIP_ROLE, twitch.MOD_ROLE, twitch.VIEWER_ROLES[-1], twitch.MESSAGER_ROLES[0],
              game.bot.ids.hunters_role, game.bot.ids.boss_slayer_role, 123456789]
    member = game.add_member("Widz", roles=others)
    assert not patrons.check_if_patron(member)
    assert patrons.get_patron(game.bot) is None


def test_no_role_the_bot_hands_out_itself_is_a_supporter_role():
    """Otherwise the bot could make somebody a "supporter" by accident (Twitch watch time, achievements...)."""
    ids = Ids()
    granted = {*twitch.VIEWER_ROLES, *twitch.MESSAGER_ROLES, twitch.VIP_ROLE, twitch.MOD_ROLE,
               ids.hunters_role, ids.boss_slayer_role, ids.dead_hunter_role, ids.mythic_slayer_role}
    assert not granted & set(patrons.SUPPORTER_ROLES)


def test_the_bot_deletes_no_role_it_still_needs():
    """The withdrawn achievement roles are deleted from the server: never a supporter's or a game role."""
    ids = Ids()
    retired = set(achievements.RETIRED_ROLES)
    assert len(retired) == 15
    assert not retired & set(patrons.SUPPORTER_ROLES)
    assert not retired & {*twitch.VIEWER_ROLES, *twitch.MESSAGER_ROLES, twitch.VIP_ROLE, twitch.MOD_ROLE,
                          ids.hunters_role, ids.boss_slayer_role, ids.dead_hunter_role, ids.mythic_slayer_role}
    bot = SimpleNamespace(ids=ids)  # (the mythic slayer is the one achievement whose role is not bot-made)
    assert achievements.role_id_of(bot, achievements.trophy("mythic_boss")) == ids.mythic_slayer_role


async def test_get_patron_keeps_the_legacy_weighting(game, monkeypatch):
    """The member lists of the roles are concatenated: two supporter roles, two tickets."""
    both = game.add_member("Oba", roles=[patrons.PATRON_ROLES[0], TWITCH_SUB])
    youtube = game.add_member("YouTube", roles=[YOUTUBE_2])
    pools = []
    monkeypatch.setattr(random, "choice", lambda pool: pools.append(list(pool)) or pool[-1])
    assert patrons.get_patron(game.bot) is youtube
    assert sorted(member.name for member in pools[0]) == ["Oba", "Oba", "YouTube"]


def test_supporter_mentions_list_every_role_once():
    text = patrons.supporter_mentions()
    assert re.findall(r"<@&(\d+)>", text) == [str(role_id) for role_id in patrons.SUPPORTER_ROLES]
    assert text.endswith(f" i <@&{patrons.SUPPORTER_ROLES[-1]}>") and text.count(", ") == 6
    # Patronite - the way the owner recommends - comes first, everywhere.
    assert patrons.SUPPORTER_ROLES[:4] == patrons.PATRON_ROLES
    assert patrons.SUPPORT_LINKS[0] == ("Patronite", "https://patronite.pl/altermmo")
    assert [url for _, url in patrons.SUPPORT_LINKS] == [
        "https://patronite.pl/altermmo", "http://youtube.com/@AlterMMO/join",
        "http://youtube.com/@Surowe_AlterMMO/join", "https://www.twitch.tv/products/altermmo_pl"]


# -- one vocabulary in every text ------------------------------------------------------

def test_the_shared_wording():
    assert patrons.SUPPORTERS_SHORT == "wspierający"
    assert patrons.SUPPORTERS_LONG == "Patroni, Crafterzy, subskrybenci Twitch i wspierający na YouTube"


def test_short_hints_speak_of_supporters():
    hint = "*Wspierający mogą wydropić dodatkowe, kosmetyczne przedmioty!*"
    assert events.CHEST_DESCR.endswith(hint) and events.INVASION_DESCR.endswith(hint)
    assert "za zdrowie jednego z naszych wspierających?" in events.PARTY_DESCR
    assert "poczekaj na modlitwę wspierających!" in daily.MSG_MISTAKE_LOST


def test_the_groups_are_spelled_out_where_there_is_room():
    assert f"Wspierający ({patrons.SUPPORTERS_LONG}) mogą zostać bossami." in help_cog.HELP_INTRO
    name, value = help_cog.HELP_FIELDS[-1]
    assert name == "🙏 Wspierający"
    assert value.startswith(f"Komendy tylko dla wspierających ({patrons.SUPPORTERS_LONG}):")
    assert "!modlitwa" in value and "!transformacja" in value and "**Panelu gry**" in value
    for role_id in patrons.SUPPORTER_ROLES:                              # the roles themselves: on the panel
        assert f"<@&{role_id}>" in hub.PANEL_SUPPORT_TEXT


def test_no_text_names_only_patrons_and_crafters_any_more():
    """Every module of the game: the old phrases are gone (they were true before 2026-11-20)."""
    old = re.compile(r"Crafterzy i Patroni|Crafterów i Patronów|patronów lub crafterów|oraz Patroni|i patronów")
    sources = {path.name: path.read_text(encoding="utf-8")
               for path in sorted(Path(altermmo.__file__).parent.rglob("*.py"))}
    assert [name for name, source in sources.items() if old.search(source)] == []
    # ... and no module but patrons.py knows a supporter role by its id (role lists, hard-coded mentions).
    known = [name for name, source in sources.items()
             if any(str(role_id) in source for role_id in patrons.SUPPORTER_ROLES)]
    assert known == ["patrons.py"]


async def test_shrine_and_tower_hints(game):
    await modifiers.spawn_modifier_shrine(game.scene())
    assert (f"*Tylko wspierający ({patrons.SUPPORTERS_LONG}) znają odpowiednią modlitwę, ale każdy może "
            "skorzystać z błogosławieństw rzuconych na potwora.*") in game.channel.messages[-1].embeds[0].description
    # The tower gives no collector's role any more (withdrawn on 2026-11-20): no promise of one.
    for floor in (1, 2):
        await tower.tower_image(game.scene(), floor)
        assert "osiągnięcia" not in game.channel.messages[-1].embeds[0].description


async def test_exp_summary_accumulates_and_resets(game):
    scene = game.scene()
    await expsum.show_file_exp(scene)
    assert game.channel.contains(expsum.EMPTY_SUMMARY)
    expsum.add_file_exp(game.bot, 1, 500)
    expsum.add_file_exp(game.bot, "1", 1500)
    expsum.add_file_exp(game.bot, 2, 100)
    await expsum.show_file_exp(scene)
    assert game.channel.messages[-1].content == "<@1>: 2000\n<@2>: 100"
    expsum.init_file_exp(game.bot)
    assert game.bot.store.exp_summary.load() == {}


async def test_several_players_are_saved_in_one_write(game, monkeypatch):
    """An XP theft is +N and -N: Horus must never collect one half without the other."""
    expsum.add_file_exp(game.bot, 1, 300)
    saves = []
    real_save = game.bot.store.exp_summary.save
    monkeypatch.setattr(game.bot.store.exp_summary, "save", lambda value: (saves.append(dict(value)), real_save(value)))
    expsum.add_file_exp_many(game.bot, {1: 2500, "2": -2500})
    assert saves == [{"1": 2800, "2": -2500}]                              # one save, both halves
    assert game.bot.store.exp_summary.load() == {"1": 2800, "2": -2500}


async def test_exp_command_only_shows_the_summary(game):
    """Horus collects the file by itself: !Exp must not throw away what it has not picked up yet."""
    from types import SimpleNamespace

    from altermmo.cogs.admin import AdminCog

    cog = AdminCog(game.bot)
    alter = game.add_member("Alter")
    expsum.add_file_exp(game.bot, 7, 250)
    ctx = SimpleNamespace(bot=game.bot, channel=game.channel, author=alter, message=game.say(alter, "!Exp"))
    await cog.exp_summary.callback(cog, ctx)
    assert game.channel.messages[-1].content == "<@7>: 250"
    assert game.bot.store.exp_summary.load() == {"7": 250}                # still there for Horus


async def test_exp_summary_reads_legacy_file(game):
    game.bot.store.exp_summary.path.parent.mkdir(parents=True, exist_ok=True)
    game.bot.store.exp_summary.path.write_text('{"1234212213": 0, "5": 300}', encoding="utf-8")
    expsum.add_file_exp(game.bot, 5, 200)
    assert game.bot.store.exp_summary.load()["5"] == 500
