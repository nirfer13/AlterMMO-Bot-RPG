"""Dova's prizes (items_decisions.md §12 and its OWNER UPDATE): ``!kosc`` and ``!karta``.

altermmo/features/dova.py, the boss cycle's part of it (altermmo/features/boss_cycle.py: the windows, the
synchronous writes, the boss shown again) and the commands (altermmo/cogs/items.py) - against a real
PostgreSQL and the fake Discord.  The prizes on a won game of cups are in test_events.py.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from altermmo.core import challenge, guards, io, ui
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossInfo, BossPhase
from altermmo.features import boss, boss_cycle, daily, database, dova, items, modifiers
from altermmo.features.fight_types import FightInterrupted, FightResult

from fakes import FakeChannel
from test_daily import make_ctx, run_checks
from test_items import run, stored
from test_revive import mentions_spy


@pytest_asyncio.fixture
async def cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.items")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    return game_db.bot.get_cog("Przedmioty")


@pytest.fixture
def drawn(monkeypatch):
    """The schedule's distribution, rigged: ``drawn.rarity`` comes out; ``drawn.delays`` - what it was asked with."""
    record = SimpleNamespace(rarity=2, delays=[])

    def rarity_for_respawn_time(seconds, *, debug):
        record.delays.append((seconds, debug))
        return record.rarity

    monkeypatch.setattr(boss, "rarity_for_respawn_time", rarity_for_respawn_time)
    return record


@pytest.fixture
def monsters(monkeypatch):
    """Boss pictures are monsters (no supporter poses as the boss) and the first picture of their rarity."""
    monkeypatch.setattr(boss.patrons, "get_patron", lambda bot: None)
    monkeypatch.setattr(boss.random, "randint", lambda low, high: low)


async def coming(game, rarity: int = 1, *, phase: BossPhase = BossPhase.WAITING, hours: float = 3.0):
    """The scheduled boss of ``rarity`` is on its way, as ``BossCycle._phase_idle`` leaves it; its arrival time."""
    bot, cycle = game.bot, game.bot.boss_cycle
    await database.update_boss_table(bot, rarity, hours * 3600, True)
    row = await database.read_boss_table(bot)
    cycle.base_rarity, cycle.arrives_at = rarity, row.respawn_at
    bot.state.boss = BossInfo(rarity=rarity)
    await bot.state.set_boss_phase(phase)
    return row.respawn_at


async def on_the_channel(game, rarity: int = 1, *, special=None):
    """The scheduled boss of ``rarity`` stands on the channel unclaimed (picture, embed, general-chat notice)."""
    bot = game.bot
    await coming(game, rarity, hours=0)
    shown = await boss.send_boss_image(game.scene(), rarity, special=special)
    bot.state.boss = BossInfo(rarity=rarity, is_player_boss=shown.is_player_boss, player_boss=shown.player_boss,
                              variant=shown.variant)
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    await bot.boss_cycle._announce_spawn()
    return game.channel.messages[-2], game.channel.messages[-1]       # (the picture, the embed with the button)


async def give(game, member, key: str, number: int = 1) -> None:
    await items.grant(game.bot, member.id, key, number)


def last(game):
    return game.channel.messages[-1]


# -- when the items work ---------------------------------------------------------------------------

async def test_the_windows_of_the_two_items(game_db):
    bot = game_db.bot
    cycle = bot.boss_cycle
    # Nothing scheduled (IDLE): the die has no boss to change; the card works - the next boss reads the file.
    assert (dova.window(bot, card=False), dova.window(bot, card=True)) == (dova.CLOSED, dova.NEXT)
    for phase in (BossPhase.SCHEDULED, BossPhase.WAITING):
        await coming(game_db, phase=phase)
        assert (dova.window(bot, card=False), dova.window(bot, card=True)) == (dova.NEXT, dova.NEXT)
    bot.store.runtime_set("boss_spawning_enabled", False)                       # !stopSpawnBoss
    assert (dova.window(bot, card=False), dova.window(bot, card=True)) == (dova.CLOSED, dova.NEXT)
    bot.store.runtime_set("boss_spawning_enabled", True)
    cycle._resume_phase = BossPhase.WAITING                                     # (a summoned boss's schedule waits)
    assert dova.window(bot, card=False) == dova.CLOSED
    cycle._resume_phase = None
    for phase, where in ((BossPhase.PRE_SPAWN, dova.ARRIVING), (BossPhase.VISIBLE, dova.VISIBLE),
                         (BossPhase.ENGAGED, dova.FIGHT), (BossPhase.FIGHTING, dova.FIGHT)):
        await bot.state.set_boss_phase(phase)
        assert dova.window(bot, card=False) == dova.window(bot, card=True) == where, phase


@pytest.mark.parametrize("phase, card, text", [
    (BossPhase.PRE_SPAWN, False, "Boss właśnie nadciąga, <@{id}> - poczekaj, aż się pokaże."),
    (BossPhase.PRE_SPAWN, True, "Boss właśnie nadciąga, <@{id}> - poczekaj, aż się pokaże."),
    (BossPhase.ENGAGED, False, "Walka z bossem już trwa, <@{id}> - za późno na sztuczki Dovy."),
    (BossPhase.ENGAGED, True, "Walka z bossem już trwa, <@{id}> - za późno na sztuczki Dovy."),
    (BossPhase.FIGHTING, False, "Walka z bossem już trwa, <@{id}> - za późno na sztuczki Dovy."),
    (BossPhase.FIGHTING, True, "Walka z bossem już trwa, <@{id}> - za późno na sztuczki Dovy."),
    (BossPhase.IDLE, False, "Kość Dovy działa tylko, zanim następny boss da o sobie znać, <@{id}>."),
])
async def test_outside_its_window_an_item_is_refused_and_not_taken(game_db, phase, card, text):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    key = "dova_card" if card else "dova_die"
    await give(game_db, ala, key)
    await modifiers.modify_modifiers(bot, "hp_boost_perc", 10)
    await bot.state.set_boss_phase(phase)
    use = dova.shuffle_card if card else dova.throw_die
    assert await use(game_db.scene(ala)) is False
    assert last(game_db).content == text.format(id=ala.id)
    assert await stored(bot.db, ala.id, key) == 1 and not bot.state.busy
    assert modifiers.current_modifiers(bot)["hp_boost_perc"] == 10 and bot.boss_cycle.base_rarity == 0


async def test_the_die_with_spawning_off_is_refused(game_db):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await coming(game_db)
    bot.store.runtime_set("boss_spawning_enabled", False)
    assert await dova.throw_die(game_db.scene(ala)) is False
    assert last(game_db).content == dova.MSG_DIE_CLOSED.format(player_id=ala.id)
    assert await stored(bot.db, ala.id, "dova_die") == 1


async def test_without_the_item_nothing_happens(game_db):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await coming(game_db)
    assert await dova.throw_die(game_db.scene(ala)) is False
    assert last(game_db).content == (f"Nie masz żadnej **Kości Dovy**, <@{ala.id}>. Dova od kubków nosi takie w "
                                     "rękawie - podobno dla tych, którzy go ograją.")
    assert await dova.shuffle_card(game_db.scene(ala)) is False
    assert last(game_db).content.startswith(f"Nie masz żadnej **Karty Dovy**, <@{ala.id}>.")
    assert bot.boss_cycle.base_rarity == 1 and not bot.state.busy
    # Without the item store nobody has one.
    await give(game_db, ala, "dova_die")
    bot._player_items = (False, items.clock.monotonic())
    assert await dova.throw_die(game_db.scene(ala)) is False
    assert last(game_db).content.startswith("Nie masz żadnej **Kości Dovy**")


# -- the Kość Dovy on the boss that is coming ----------------------------------------------------------

@pytest.mark.parametrize("phase", [BossPhase.SCHEDULED, BossPhase.WAITING])
async def test_the_die_rerolls_the_coming_boss_and_keeps_its_arrival(game_db, drawn, phase):
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die", 2)
    arrives_at = await coming(game_db, 1, phase=phase)
    drawn.rarity = 3
    allowed = mentions_spy(game_db)
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert [user.id for user in allowed[-1].users] == [ala.id] and allowed[-1].everyone is False   # only him
    # A new base rarity from the schedule's own distribution: a delay drawn as the schedule draws one.
    [(delay, debug)] = drawn.delays
    timings = bot.timings
    assert timings.respawn_min * timings.respawn_multiplier <= delay <= timings.respawn_max * timings.respawn_multiplier
    assert debug is bot.debug
    assert cycle.base_rarity == 3 and bot.state.boss_rarity == 3 and bot.state.boss_phase is phase
    row = await database.read_boss_table(bot)
    assert row.rarity == 3 and row.respawn_started and row.respawn_at == arrives_at   # the arrival did not move
    assert cycle.arrives_at == arrives_at
    assert last(game_db).content == (f"🎲 <@{ala.id}> rzuca **Kością Dovy**... Kość toczy się po stole, podskakuje i "
                                     "staje na: 🟧 **legendarny**! Taki boss nadejdzie jako następny. *Kapliczka i "
                                     "pora dnia wciąż mogą to zmienić.*")
    assert await stored(bot.db, ala.id, "dova_die") == 1 and not bot.state.busy and bot.owner_log == []


async def test_the_shrine_and_the_hour_still_apply_when_the_rerolled_boss_comes(game_db, drawn, monsters):
    """``final_rarity`` is applied when the boss arrives, as for every boss: the die only changed the base."""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await coming(game_db, 0)
    drawn.rarity = 1
    assert await dova.throw_die(game_db.scene(ala)) is True
    await modifiers.modify_modifiers(bot, "rarity_boost", 1)                    # a prayer afterwards
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    await bot.boss_cycle._phase_pre_spawn()
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss.rarity == 2   # 1 + the shrine's boost


async def test_the_boss_table_failing_keeps_the_new_rarity_and_tells_the_owner(game_db, drawn, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await coming(game_db, 1)

    async def broken(bot_, rarity):
        raise DatabaseUnavailable("down")

    monkeypatch.setattr(database, "set_boss_rarity", broken)
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert bot.boss_cycle.base_rarity == 2 and await stored(bot.db, ala.id, "dova_die") == 0   # nothing given back
    assert bot.owner_log == [dova.LOG_BOSS_ROW.format(rarity=2, error="DatabaseUnavailable")]


# -- the Kość Dovy on the boss on the channel ---------------------------------------------------------------

async def test_the_die_on_the_boss_on_the_channel_shows_it_again(game_db, drawn, monsters, monkeypatch):
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await modifiers.modify_modifiers(bot, "rarity_boost", 1)
    old_picture, old_embed = await on_the_channel(game_db, 1)
    arrives_at = (await database.read_boss_table(bot)).respawn_at
    token = cycle.boss_token()
    drawn.rarity = 2
    assert await dova.throw_die(game_db.scene(ala)) is True
    # The rules of every boss apply at once: drawn 2 + the shrine's 1 = legendary (test bot: no off-peak rule).
    assert bot.state.boss == BossInfo(rarity=3) and bot.state.boss_phase is BossPhase.VISIBLE
    assert cycle.base_rarity == 2 and cycle.boss_token() == token
    row = await database.read_boss_table(bot)
    assert row.rarity == 2 and row.respawn_at == arrives_at                     # a restart brings the same boss
    # The old picture and its button are gone; the line, then the boss as a spawn shows it.
    assert old_picture.deleted and old_embed.deleted
    line, picture, embed = game_db.channel.messages[-3:]
    assert line.content == (f"🎲 <@{ala.id}> rzuca **Kością Dovy**... Kość toczy się po stole, podskakuje i staje na: "
                            "🟧 **legendarny**! Boss na kanale zmienia się na oczach wszystkich!")
    assert picture.files == ["1000.gif"]
    assert embed.embeds[0].title == "💀 🟧 Legendarny boss! 🟧 💀" and "Nie dasz rady sam" in embed.embeds[0].description
    assert "Dodatkowe statystyki:\n🔺 Rzadkość zwiększona o 1 poziom" in embed.embeds[0].description
    assert embed.button("Zaatakuj").custom_id == boss_cycle.ATTACK_BUTTON_ID
    assert cycle._attack_buttons == [embed] and cycle._boss_pictures == [picture]
    # The notice on the general chat says the new rarity (edited in place: a legendary boss asks for a party).
    notice = game_db.announce_channel.messages[-1]
    assert notice.content == boss_cycle.spawn_notice(bot.ids.boss_channel, 3) and len(game_db.announce_channel.sent) == 1
    assert not bot.state.busy and bot.owner_log == [] and await stored(bot.db, ala.id, "dova_die") == 0

    # A rarity of 3 means the usual party: the new button attacks the new boss.
    calls = []

    async def group_init(scene, rarity):
        calls.append(("group_init", rarity))
        return [scene.author]

    async def group_fight(scene, players, is_player_boss, player_boss):
        calls.append(("group_fight", len(players)))
        return FightResult(won=True)

    async def reset_end(bot_):
        return None

    monkeypatch.setattr(boss, "group_init", group_init)
    monkeypatch.setattr(boss, "group_fight", group_fight)
    monkeypatch.setattr(boss, "reset_end", reset_end)
    assert ui.register(bot, boss_cycle.AttackView())
    await game_db.click(embed, ala, "Zaatakuj")
    assert calls == [("group_init", 3), ("group_fight", 1)]


async def test_an_old_boss_message_that_cannot_be_deleted_loses_its_button(game_db, drawn, monsters):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    _picture, old_embed = await on_the_channel(game_db, 1)

    async def forbidden(**kwargs):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    old_embed.delete = forbidden
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert not old_embed.deleted and old_embed.view is None                     # no button of the old boss is left
    assert last(game_db).button("Zaatakuj") is not None and bot.boss_cycle._attack_buttons == [last(game_db)]


async def test_a_summoned_boss_keeps_its_origin_and_its_stay(game_db, drawn, monsters):
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ola, "dova_die")
    await coming(game_db, 1)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    note = bot.store.runtime_get("summoned_boss_info")
    leaves_at, row = cycle.summoned_leaves_at(), await database.read_boss_table(bot)
    drawn.rarity = 0
    assert await dova.throw_die(game_db.scene(ola)) is True
    assert bot.state.boss.rarity == 0 and bot.state.boss.origin == "scroll"
    assert bot.store.runtime_get("summoned_boss_rarity") == 0                   # a restart brings it back so
    assert bot.store.runtime_get("summoned_boss_info") == note                  # its summoner, stay, claim: as they were
    assert cycle.summoned_leaves_at() == leaves_at and cycle._resume_phase is BossPhase.WAITING
    assert await database.read_boss_table(bot) == row and cycle.base_rarity == 1   # the boss still to come: untouched
    assert last(game_db).embeds[0].title == "💀 🟩 Zwykły boss! 🟩 💀"
    # The scroll's notice still names its reader and says the new rarity.
    assert game_db.announce_channel.messages[-1].content == boss_cycle.summon_notice(
        bot.ids.boss_channel, 0, "scroll", "Ala")
    assert "🟩 **zwykły boss**" in game_db.announce_channel.messages[-1].content


async def test_the_scroll_boss_notice_follows_every_die(game_db, drawn, monsters):
    """discord.py 2.7: ``Message.edit`` returns a NEW message and leaves the old object as it was - so the notice
    is written anew from the boss each time (never from the old object's text); a legendary boss asks for a party."""
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ola, "dova_die", 3)
    await coming(game_db, 1)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    notice = game_db.announce_channel.messages[-1]
    shown = []                                                                  # the notice, edit after edit

    def discord_like(message):
        async def edit(**kwargs):
            shown.append(kwargs["content"])
            fresh = SimpleNamespace(content=kwargs["content"], channel=message.channel, id=message.id,
                                    delete=message.delete)
            fresh.edit = discord_like(fresh)
            return fresh
        return edit

    notice.edit = discord_like(notice)
    channel = bot.ids.boss_channel
    for rarity in (1, 2, 3):
        drawn.rarity = rarity
        assert await dova.throw_die(game_db.scene(ola)) is True
        assert shown[-1] == boss_cycle.summon_notice(channel, rarity, "scroll", "Ala"), rarity
    assert len(shown) == 3 and notice.content == boss_cycle.summon_notice(channel, 4, "scroll", "Ala")
    assert shown[-1].endswith("🟧 **legendarny boss**. Sam nikt mu nie podoła - zbierzcie drużynę, bo bestia nie "
                              "będzie czekać wiecznie!")
    await cycle.remove_spawn_notice()                                           # (the kept message is the notice)
    assert notice.deleted


async def test_a_player_posing_as_the_boss_stays_the_boss(game_db, drawn, monsters):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ala, "dova_die")
    await modifiers.modify_modifiers(bot, "player_id", ola.id)                 # the shrine chose Ola
    old_avatar, _embed = await on_the_channel(game_db, 1)
    assert bot.state.boss.player_boss is ola and old_avatar.content.startswith("https://")
    drawn.rarity = 0
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert bot.state.boss == BossInfo(rarity=0, is_player_boss=True, player_boss=ola)
    avatar, embed = game_db.channel.messages[-2:]
    assert avatar.content == old_avatar.content and old_avatar.deleted
    assert embed.embeds[0].title == f"💀 🟩 Zwykły {ola.name}! 🟩 💀" and "zgarnia 500 doświadczenia" in embed.text


async def test_a_special_boss_stays_special_in_its_variant(game_db, drawn, monsters, special_bosses):
    special_bosses()                                                            # "mgla" on the first picture of each rarity
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die", 2)
    await on_the_channel(game_db, 1, special=True)
    assert bot.state.boss.variant == "mgla"
    drawn.rarity = 2
    assert await dova.throw_die(game_db.scene(ala)) is True
    picture, embed = game_db.channel.messages[-2:]
    assert bot.state.boss.variant == "mgla" and picture.files == ["100.gif"]
    assert "🌫️ Tego bossa spowija **Mgła**." in embed.embeds[0].description

    # An ordinary boss stays ordinary - also while every boss that appears would be special.
    game_db.channel.messages.clear()
    await on_the_channel(game_db, 1, special=False)
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert bot.state.boss.variant == "" and "Mgła" not in last(game_db).embeds[0].description


async def test_the_card_keeps_the_very_same_picture(game_db, monsters, special_bosses):
    special_bosses()
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_card")
    old_picture, old_embed = await on_the_channel(game_db, 2, special=True)
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    picture, embed = game_db.channel.messages[-2:]
    assert old_picture.deleted and old_embed.deleted and picture.files == old_picture.files == ["100.gif"]
    assert bot.state.boss == BossInfo(rarity=2, variant="mgla")
    assert bot.boss_cycle.shown_picture == boss.bossvariants.special_picture(100)


# -- the Karta Dovy ---------------------------------------------------------------------------------------

def rig(monkeypatch, *names):
    """The card's draws come out as ``names`` (values: the lowest); returns the pools each draw was offered."""
    pools: list[list[str]] = []
    queue = list(names)

    def choice(pool):
        pools.append(list(pool))
        return queue.pop(0)

    monkeypatch.setattr(modifiers.random, "choice", choice)
    monkeypatch.setattr(modifiers.random, "randint", lambda low, high: low)
    return pools


def test_how_many_modifiers_are_on():
    zeros = dict(modifiers.INIT_MODIFIERS)
    assert dova.modifiers_on(zeros) == 0
    assert dova.modifiers_on({**zeros, "hp_boost_perc": 10, "ban_loser": 1}) == 2
    assert dova.modifiers_on({**zeros, "player_id": 123456789012345678}) == 1    # a player-boss counts once
    assert dova.modifiers_on({**zeros, "time_reduced_perc": -5}) == 1             # anything but 0 is "on"
    assert dova.modifiers_on({key: 1 for key in zeros}) == len(modifiers.INIT_MODIFIERS) == 10


async def test_the_card_draws_as_many_new_outcomes_as_were_on(game_db, monkeypatch):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ala, "dova_card")
    await coming(game_db)
    for name, value in (("hp_boost_perc", 10), ("ban_loser", 1), ("player_id", ola.id), ("time_reduced_perc", -5)):
        await modifiers.modify_modifiers(bot, name, value)
    pools = rig(monkeypatch, "ban_loser", "points_boost", "player_id", "points_boost")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    # Four were on: the file starts from nothing and gets four draws, one after another, as four prayers would.
    assert modifiers.current_modifiers(bot) == {**modifiers.INIT_MODIFIERS, "ban_loser": 1, "points_boost": 2,
                                      "player_id": ala.id}
    full = list(modifiers.RANDOM_POOL)
    assert pools[0] == full and "ban_loser" not in pools[1] and "player_id" in pools[1]
    assert "player_id" not in pools[3] and "ban_loser" not in pools[3]          # a one-off outcome never twice
    embed = last(game_db).embeds[0]
    assert embed.title == "🃏 Karta Dovy!"
    assert embed.description == (
        f"<@{ala.id}> tasuje **Kartę Dovy**... Stare błogosławieństwa i klątwy następnego bossa rozwiewają się, a "
        "na ich miejsce przychodzą nowe:\n🔻 Przegrany zostanie zbanowany\n🔺 Punkty za wygraną zwiększone o 2 "
        f"(nie dotyczy potworów)\n🔺 Bossem będzie gracz <@{ala.id}>")
    assert await stored(bot.db, ala.id, "dova_card") == 0 and not bot.state.busy


async def test_a_boss_without_perks_gets_one_and_a_drawn_rest_resets_the_hunts(game_db, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_card", 2)
    resets = []

    async def clear_daily_file(bot_):
        resets.append(bot_)

    monkeypatch.setattr(daily, "clear_daily_file", clear_daily_file)
    rig(monkeypatch, "ban_loser")
    assert await dova.shuffle_card(game_db.scene(ala)) is True                 # IDLE: the card works already
    assert modifiers.current_modifiers(bot) == {**modifiers.INIT_MODIFIERS, "ban_loser": 1}
    assert last(game_db).embeds[0].description == (
        f"<@{ala.id}> tasuje **Kartę Dovy**... Następny boss nie miał żadnych perków - Karta Dovy dorzuca mu jeden:"
        "\n🔻 Przegrany zostanie zbanowany")
    await modifiers.init_modifiers(bot)
    rig(monkeypatch, "reset_cd")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert resets == [bot] and modifiers.current_modifiers(bot) == modifiers.INIT_MODIFIERS
    # A rest is no perk of the boss: the card says what it brought instead.
    assert last(game_db).embeds[0].description == (
        f"<@{ala.id}> tasuje **Kartę Dovy**... Następny boss nadal nie ma żadnych perków - zamiast nich karta "
        "przynosi wszystkim chwilę wytchnienia:\n🔺 Wszyscy bohaterowie są wypoczęci i mogą wyruszyć na polowanie!")

    async def broken(bot_):
        raise OSError("disk full")

    monkeypatch.setattr(daily, "clear_daily_file", broken)
    rig(monkeypatch, "reset_cd")
    await give(game_db, ala, "dova_card")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert last(game_db).embeds[0].description == (                             # (no rest: not told; no ":" left hanging)
        f"<@{ala.id}> tasuje **Kartę Dovy**... Następny boss wciąż nie ma żadnych perków - karta zapłonęła i zgasła "
        "jak mokra hubka.")
    assert bot.owner_log == [dova.LOG_HUNTS_NOT_RESET.format(error="OSError")]


async def test_a_card_that_took_the_perks_and_drew_only_a_rest_says_so(game_db, monsters, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_card", 3)
    monkeypatch.setattr(daily, "clear_daily_file", lambda bot_: asyncio.sleep(0))
    await modifiers.modify_modifiers(bot, "hp_boost_perc", 10)
    rig(monkeypatch, "reset_cd")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert modifiers.current_modifiers(bot) == modifiers.INIT_MODIFIERS
    assert last(game_db).embeds[0].description == (
        f"<@{ala.id}> tasuje **Kartę Dovy**... Stare błogosławieństwa i klątwy następnego bossa rozwiewają się, a "
        "zamiast nowych karta przynosi wszystkim chwilę wytchnienia:\n🔺 Wszyscy bohaterowie są wypoczęci i mogą "
        "wyruszyć na polowanie!")

    # On the boss on the channel: it loses its perk and is shown anew without it...
    await modifiers.modify_modifiers(bot, "hp_boost_perc", 10)
    old_picture, old_embed = await on_the_channel(game_db, 1)
    rig(monkeypatch, "reset_cd")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    line, _picture, embed = game_db.channel.messages[-3:]
    assert "klątwy bossa na kanale rozwiewają się, a zamiast nowych" in line.embeds[0].description
    assert old_embed.deleted and "Dodatkowe statystyki" not in embed.embeds[0].description
    # ...but a boss without perks that gets only a rest keeps its face (nothing on it changed).
    _picture, kept = game_db.channel.messages[-2:]
    rig(monkeypatch, "reset_cd")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert not kept.deleted and kept.button("Zaatakuj") is not None and bot.boss_cycle._attack_buttons == [kept]
    assert last(game_db).embeds[0].description.startswith(
        f"<@{ala.id}> tasuje **Kartę Dovy**... Boss na kanale nadal nie ma żadnych perków - zamiast nich")


async def test_the_card_on_the_boss_on_the_channel_leaves_out_what_it_cannot_use(game_db, monsters, monkeypatch):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ala, "dova_card")
    await modifiers.modify_modifiers(bot, "rarity_boost", 1)
    old_picture, old_embed = await on_the_channel(game_db, 1)
    await modifiers.modify_modifiers(bot, "player_id", ola.id)                 # (too late to make her the boss)
    pools = rig(monkeypatch, "hp_reduced_perc", "time_increased_perc")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    # Both still counted and were cleared like the rest - but neither could be drawn for a boss already here.
    assert all("rarity_boost" not in pool and "player_id" not in pool for pool in pools) and len(pools) == 2
    assert modifiers.current_modifiers(bot) == {**modifiers.INIT_MODIFIERS, "hp_reduced_perc": 5, "time_increased_perc": 5}
    line, picture, embed = game_db.channel.messages[-3:]
    assert line.embeds[0].description.startswith(
        f"<@{ala.id}> tasuje **Kartę Dovy**... Stare błogosławieństwa i klątwy bossa na kanale rozwiewają się")
    assert old_picture.deleted and old_embed.deleted and picture.files == old_picture.files
    # The new face shows exactly what the fight will read.
    assert embed.embeds[0].description.endswith("Dodatkowe statystyki:\n🔺 Życie jest zmniejszone o 5 %"
                                                "\n🔺 Czas na reakcję zwiększony o 5 %")
    assert bot.state.boss == BossInfo(rarity=1) and embed.button("Zaatakuj") is not None


async def test_the_card_on_the_boss_on_the_channel_keeps_its_rarity_over_a_restart(game_db, monsters, monkeypatch):
    """The shrine's boost goes from the file, not from the boss it raised: a restart draws the scheduled boss again
    from its BOSS row and the file - so the row takes the rarity shown in, and the same boss comes back."""
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_card")
    await modifiers.modify_modifiers(bot, "rarity_boost", 1)
    arrives_at = await coming(game_db, 2, hours=0)                              # drawn epic...
    await boss.send_boss_image(game_db.scene(), 3)                              # ...shown legendary
    bot.state.boss = BossInfo(rarity=3)
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    rig(monkeypatch, "hp_reduced_perc")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert modifiers.current_modifiers(bot)["rarity_boost"] == 0 and bot.state.boss.rarity == 3
    row = await database.read_boss_table(bot)
    assert cycle.base_rarity == row.rarity == 3 and row.respawn_at == arrives_at
    assert boss_cycle.final_rarity(row.rarity, 0, boss_cycle.clock.now(), debug=bot.debug) == 3   # not epic
    assert bot.owner_log == []

    # Without a boost to keep nothing is written; a summoned boss's row is the paused schedule's - never touched.
    await give(game_db, ala, "dova_card", 2)
    rig(monkeypatch, "hp_boost_perc")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert (await database.read_boss_table(bot)).rarity == 3
    await coming(game_db, 1)                                                    # (the next boss is on its way)
    await modifiers.modify_modifiers(bot, "rarity_boost", 1)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    rig(monkeypatch, "hp_reduced_perc", "hp_boost_perc")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert (await database.read_boss_table(bot)).rarity == 1 and cycle.base_rarity == 1
    assert bot.store.runtime_get("summoned_boss_rarity") == 4 and bot.state.boss.rarity == 4


# -- once only: the take, the race, the refund ---------------------------------------------------------------

def claim(bot) -> None:
    """The boss is claimed as ``BossCycle.attack`` claims it (the phase and the encounter it notes)."""
    cycle = bot.boss_cycle
    assert bot.state.try_engage_boss()
    cycle._encounters += 1
    cycle._active_encounter = cycle._encounters


def leave(bot) -> None:
    """A summoned boss starts leaving as ``BossCycle._phase_visible`` sends it away (its first, synchronous steps)."""
    assert bot.state.try_engage_boss()
    bot.boss_cycle._encounters += 1


async def test_an_attack_while_the_boss_is_changed_waits_for_it(game_db, drawn, monsters, monkeypatch):
    """Owner: busy = the notice.  Nobody claims the boss while an item of Dova changes it - neither a typed
    !zaatakuj during the take nor a click on the old button while it is being deleted; the boss waits, and the
    new button then attacks the new boss."""
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ala, "dova_die")
    _picture, old_embed = await on_the_channel(game_db, 1)
    token = cycle.boss_token()
    assert ui.register(bot, boss_cycle.AttackView())
    real_take, real_delete = items.take, io.safe_delete
    seen = {}

    async def take_and_attack(*args, **kwargs):
        left = await real_take(*args, **kwargs)
        await cycle.attack(game_db.scene(ola))                                  # Ola types !zaatakuj meanwhile
        seen["typed"] = (bot.state.boss_phase, last(game_db).content)
        return left

    async def delete_and_click(message):
        if "clicked" not in seen:                                               # the old button, as it goes
            await game_db.click(old_embed, ola, "Zaatakuj")
            seen["clicked"] = (bot.state.boss_phase, cycle.boss_token())
        return await real_delete(message)

    monkeypatch.setattr(items, "take", take_and_attack)
    monkeypatch.setattr(io, "safe_delete", delete_and_click)
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert seen["typed"] == (BossPhase.VISIBLE, guards.BUSY_NOTICE)
    assert seen["clicked"] == (BossPhase.VISIBLE, token)
    assert bot.state.boss_phase is BossPhase.VISIBLE and bot.state.boss.rarity == 2 and old_embed.deleted
    calls = []

    async def single_init(scene, rarity):
        calls.append((scene.author.id, rarity))
        return None

    async def reset_end(bot_):
        return None

    monkeypatch.setattr(boss, "single_init", single_init)
    monkeypatch.setattr(boss, "reset_end", reset_end)
    await game_db.click(last(game_db), ola, "Zaatakuj")
    assert calls == [(ola.id, 2)]


async def test_somebody_claiming_during_the_take_gives_the_die_back(game_db, drawn, monsters, monkeypatch):
    """(An attack waits for the use - see above; should a claim get in all the same, the die goes back.)"""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    _picture, old_embed = await on_the_channel(game_db, 1)
    real_take = items.take

    async def take_and_claim(*args, **kwargs):
        left = await real_take(*args, **kwargs)
        claim(bot)
        return left

    monkeypatch.setattr(items, "take", take_and_claim)
    assert await dova.throw_die(game_db.scene(ala)) is False
    assert last(game_db).content == "Ktoś był szybszy i już stanął do walki - Kość Dovy wraca do Twojej sakwy."
    assert await stored(bot.db, ala.id, "dova_die") == 1 and drawn.delays == []
    assert bot.state.boss.rarity == 1 and not old_embed.deleted and not bot.state.busy


async def test_a_summoned_boss_leaving_during_the_take_is_no_claim(game_db, drawn, monsters, monkeypatch):
    """It passes through ENGAGED as it goes - but nobody "was faster": the die goes back with the plain word."""
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ola, "dova_die")
    await coming(game_db, 1)
    assert await bot.boss_cycle.summon(game_db.scene(ala), 4, origin="scroll")
    real_take = items.take

    async def take_and_leave(*args, **kwargs):
        left = await real_take(*args, **kwargs)
        leave(bot)                                                              # its half hour is over
        return left

    monkeypatch.setattr(items, "take", take_and_leave)
    assert await dova.throw_die(game_db.scene(ola)) is False
    assert last(game_db).content == (f"Na kanale bossa coś się właśnie zmieniło, <@{ola.id}> - Kość Dovy wraca do "
                                     "Twojej sakwy.")
    assert await stored(bot.db, ola.id, "dova_die") == 1 and drawn.delays == []


async def test_a_card_taken_while_the_boss_starts_arriving_goes_back(game_db, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_card")
    await coming(game_db)
    await modifiers.modify_modifiers(bot, "hp_boost_perc", 10)
    real_take = items.take

    async def take_and_wind(*args, **kwargs):
        left = await real_take(*args, **kwargs)
        await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)                    # the boss reads the file now
        return left

    monkeypatch.setattr(items, "take", take_and_wind)
    assert await dova.shuffle_card(game_db.scene(ala)) is False
    assert last(game_db).content == (f"Na kanale bossa coś się właśnie zmieniło, <@{ala.id}> - Karta Dovy wraca do "
                                     "Twojej sakwy.")
    assert await stored(bot.db, ala.id, "dova_card") == 1
    assert modifiers.current_modifiers(bot) == {**modifiers.INIT_MODIFIERS, "hp_boost_perc": 10}


async def test_a_refund_that_fails_is_for_the_owner(game_db, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await coming(game_db)
    real_take = items.take

    async def take_and_wind(*args, **kwargs):
        left = await real_take(*args, **kwargs)
        await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
        return left

    async def no_grant(*args, **kwargs):
        raise DatabaseUnavailable("down")

    monkeypatch.setattr(items, "take", take_and_wind)
    monkeypatch.setattr(items, "grant", no_grant)
    assert await dova.throw_die(game_db.scene(ala)) is False
    assert bot.owner_log == [dova.LOG_NOT_REFUNDED.format(name="Kość Dovy", player_id=ala.id,
                                                          error="DatabaseUnavailable", key="dova_die")]


async def test_a_take_the_database_may_or_may_not_have_done_changes_nothing(game_db, drawn, monkeypatch):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await coming(game_db)

    async def lost_reply(*args, **kwargs):
        raise DatabaseUnavailable("the reply was lost")

    monkeypatch.setattr(items, "take", lost_reply)
    assert await dova.throw_die(game_db.scene(ala)) is False
    assert last(game_db).content == ui.MSG_FAILED and bot.boss_cycle.base_rarity == 1 and drawn.delays == []
    assert bot.owner_log == [dova.LOG_TAKE_UNSURE.format(name="Kość Dovy", player_id=ala.id,
                                                         error="DatabaseUnavailable", key="dova_die")]


async def test_two_dice_at_once_never_both_work(game_db, drawn, monkeypatch):
    """The channel is held for a use: the second player gets the busy notice and keeps his die."""
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    for member in (ala, ola):
        await give(game_db, member, "dova_die")
    await coming(game_db)
    gate = asyncio.Event()
    real_take = items.take

    async def slow_take(*args, **kwargs):
        await gate.wait()
        return await real_take(*args, **kwargs)

    monkeypatch.setattr(items, "take", slow_take)
    first = asyncio.ensure_future(dova.throw_die(game_db.scene(ala)))
    await asyncio.sleep(0.01)
    assert bot.state.busy_owner == dova.BUSY_OWNER
    assert await dova.throw_die(game_db.scene(ola)) is False
    assert last(game_db).content == guards.BUSY_NOTICE
    gate.set()
    assert await first is True and len(drawn.delays) == 1
    assert await stored(bot.db, ala.id, "dova_die") == 0 and await stored(bot.db, ola.id, "dova_die") == 1
    assert game_db.channel.sent_count("rzuca **Kością Dovy**") == 1 and not bot.state.busy


async def test_a_boss_claimed_while_it_is_shown_again_keeps_no_live_button(game_db, drawn, monsters, monkeypatch):
    """(An attack waits for the change; should a claim get in all the same, the boss is fought with its new face,
    which carries no live button meanwhile - and is its face again should the fight break.)"""
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await on_the_channel(game_db, 1)
    real_post = boss.post_boss

    async def post_and_claim(*args, **kwargs):
        shown = await real_post(*args, **kwargs)
        claim(bot)                                                              # an attack while the picture went up
        return shown

    monkeypatch.setattr(boss, "post_boss", post_and_claim)
    assert await dova.throw_die(game_db.scene(ala)) is True                    # (the new rarity is in force)
    picture, embed = game_db.channel.messages[-2:]
    assert embed.view is None and not embed.deleted and not picture.deleted and cycle._attack_buttons == []
    assert cycle._engaged_buttons == [embed] and cycle._engaged_pictures == [picture]
    assert bot.state.boss.rarity == 2


async def test_a_summoned_boss_that_leaves_while_it_is_shown_again_takes_its_new_face_along(
        game_db, drawn, monsters, monkeypatch):
    """Its stay ends while the new face goes up: nothing of it stays under its farewell, no button lives on."""
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ola, "dova_die")
    await coming(game_db, 1)
    assert await cycle.summon(game_db.scene(ala), 4, origin="scroll")
    real_post = boss.post_boss
    posted = []

    async def leave_and_post(*args, **kwargs):
        leave(bot)                                                              # (its farewell clears the channel)
        shown = await real_post(*args, **kwargs)
        posted.extend(game_db.channel.messages[-2:])
        return shown

    monkeypatch.setattr(boss, "post_boss", leave_and_post)
    assert await dova.throw_die(game_db.scene(ola)) is True
    assert len(posted) == 2 and all(message.deleted for message in posted)
    assert cycle._attack_buttons == [] and cycle._boss_pictures == []
    assert last(game_db).content.startswith("🎲 ")                             # (the die's own line is the last word)


async def test_the_face_of_a_boss_back_after_an_interrupted_fight_is_replaced_too(game_db, drawn, monsters,
                                                                                 monkeypatch):
    """Claimed, the fight broke, the boss stays: a die then replaces its old picture and embed (old stats) too."""
    bot = game_db.bot
    cycle = bot.boss_cycle
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await give(game_db, ala, "dova_die")
    old_picture, old_embed = await on_the_channel(game_db, 1)

    async def broken(scene, rarity):
        raise FightInterrupted("lag")

    monkeypatch.setattr(boss, "single_init", broken)
    await cycle.attack(game_db.scene(ola))
    await asyncio.sleep(0.05)                                                   # (the "still here" notice)
    assert bot.state.boss_phase is BossPhase.VISIBLE
    notice = next(message for message in reversed(game_db.channel.messages) if "przerwana" in message.content)
    assert notice.button("Zaatakuj") is not None and old_embed.view is None
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert old_picture.deleted and old_embed.deleted and notice.deleted
    picture, embed = game_db.channel.messages[-2:]
    assert cycle._boss_pictures == [picture] and cycle._attack_buttons == [embed]


async def test_the_boss_on_the_channel_is_shown_anew_before_its_row_is_written(game_db, drawn, monsters,
                                                                             monkeypatch):
    """The BOSS row matters only after a restart: the line and the new face come first."""
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    _picture, old_embed = await on_the_channel(game_db, 1)
    real_write = database.set_boss_rarity
    seen = []

    async def set_boss_rarity(bot_, rarity):
        seen.append((old_embed.deleted, last(game_db).embeds[0].title if last(game_db).embeds else None))
        return await real_write(bot_, rarity)

    monkeypatch.setattr(database, "set_boss_rarity", set_boss_rarity)
    drawn.rarity = 0
    assert await dova.throw_die(game_db.scene(ala)) is True
    assert seen == [(True, "💀 🟩 Zwykły boss! 🟩 💀")] and (await database.read_boss_table(bot)).rarity == 0


async def test_the_owners_preview_is_not_the_boss(game_db, monsters, monkeypatch):
    """``!image`` (any channel, here the boss channel itself): neither deleted by a boss shown anew nor its picture."""
    from altermmo.cogs import boss as boss_cog

    bot = game_db.bot
    cycle = bot.boss_cycle
    ala, admin = game_db.add_member("Ala"), game_db.add_member("Admin")
    await give(game_db, ala, "dova_card")
    old_picture, old_embed = await on_the_channel(game_db, 2)
    monkeypatch.setattr(boss.random, "randint", lambda low, high: high)        # (the preview: another monster)
    cog = boss_cog.BossCog(bot)
    await cog.boss_image.callback(cog, make_ctx(game_db, admin, "!image"), 2)
    preview_picture, preview_embed = game_db.channel.messages[-2:]
    assert preview_picture.files == ["107.gif"] and preview_embed.button("Zaatakuj") is not None
    assert cycle._attack_buttons == [old_embed] and cycle._boss_pictures == [old_picture]
    assert cycle.shown_picture.name == "100.gif"
    rig(monkeypatch, "hp_reduced_perc")
    assert await dova.shuffle_card(game_db.scene(ala)) is True
    assert not preview_picture.deleted and not preview_embed.deleted and old_picture.deleted
    assert game_db.channel.messages[-2].files == ["100.gif"]                   # the boss standing there, as it was


def test_nothing_is_awaited_between_the_window_check_and_the_write():
    """The invariant in the code itself: the window is checked again after the take, then drawn and written
    without an await (the effects start synchronously - awaiting a coroutine runs it up to its own first await)."""
    for function in (dova.window, dova.draw_base_rarity, dova.shuffle_modifiers, dova.modifiers_on,
                     modifiers.draw_modifier, modifiers.add_modifier, modifiers.replace_modifiers,
                     modifiers.current_modifiers, boss.picture_like, boss_cycle.final_rarity,
                     boss_cycle.BossCycle.set_next_rarity,
                     boss_cycle.BossCycle.change_visible_boss, boss_cycle.BossCycle.next_boss_waits,
                     boss_cycle.BossCycle.boss_token):
        assert not inspect.iscoroutinefunction(function), function

    def code(segment: str) -> list[str]:
        return [line.split("#")[0] for line in segment.splitlines()]

    use = inspect.getsource(dova._use)
    check = use.index("now = window(bot, card=card)")
    effect = use[check:use.index("if card:", check)]
    refund = effect[effect.index("if now != where"):effect.index("return False") + len("return False")]
    assert "await _give_back" in refund                                         # (only a use that did not happen)
    assert not any("await" in line for line in code(effect.replace(refund, "")))
    checked = 0
    for function, marker in ((dova._throw, "draw_base_rarity(bot)"), (dova._shuffle, "shuffle_modifiers(")):
        source = inspect.getsource(function)
        start = source.find(marker)
        while start >= 0:
            end = source.index("# -- the write is done", start)
            assert not any("await" in line for line in code(source[start:end])), function
            checked += 1
            start = source.find(marker, end)
    assert checked == 3                                                         # the die twice, the card once


# -- the commands ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, aliases", [("kosc", {"kość", "kostka"}), ("karta", {"karty"})])
async def test_the_commands_are_gameplay_commands(game_db, cog, drawn, name, aliases):
    bot = game_db.bot
    command = bot.get_command(name)
    assert command.cog_name == "Przedmioty" and set(command.aliases) == aliases and len(command.checks) == 1
    ala = game_db.add_member("Ala")
    key = "dova_die" if name == "kosc" else "dova_card"
    await give(game_db, ala, key)
    await coming(game_db)
    # Only on the boss channel (no busy notice elsewhere either); silent during a typed fight; the busy notice.
    elsewhere = FakeChannel(555, game_db.guild, bot.fake_user, name="inny")
    with bot.state.hold_busy("hunt"):
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, f"!{name}", channel=elsewhere))
    assert elsewhere.sent == []
    with challenge.round_running(game_db.channel):
        before = len(game_db.channel.sent)
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, f"!{name}"))
        assert len(game_db.channel.sent) == before
    with bot.state.hold_busy("event:PARTY"):
        with pytest.raises(guards.SilentCheckFailure):
            await run_checks(command, make_ctx(game_db, ala, f"!{name}"))
    assert last(game_db).content == guards.BUSY_NOTICE
    # Typed for real (an alias too): it works, once.
    for word in (name, *sorted(aliases)):
        await run(game_db, ala, f"!{word}")
    assert await stored(bot.db, ala.id, key) == 0
    assert game_db.channel.sent_count("rzuca **Kością Dovy**" if name == "kosc" else "🃏 Karta Dovy!") == 1


async def test_typed_during_the_boss_fight_the_commands_say_nothing(game_db, cog):
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await give(game_db, ala, "dova_die")
    await give(game_db, ala, "dova_card")
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    with challenge.round_running(game_db.channel):
        before = len(game_db.channel.sent)
        await run(game_db, ala, "!kosc")
        await run(game_db, ala, "!karta")
        assert len(game_db.channel.sent) == before
    assert await stored(bot.db, ala.id, "dova_die") == 1 and await stored(bot.db, ala.id, "dova_card") == 1


async def test_the_items_view_and_the_help_name_the_commands(game_db):
    from altermmo.cogs import help as help_cog

    bot = game_db.bot
    ala = game_db.add_member("Ala")
    assert not any("Dovy" in line for line in await items.view_lines(bot, ala.id))   # (none: not listed)
    await give(game_db, ala, "dova_die", 2)
    await give(game_db, ala, "dova_card")
    lines = await items.view_lines(bot, ala.id)
    assert "🎲 **Kości Dovy**: 2 - **!kosc**" in lines and "🃏 **Karty Dovy**: 1 - **!karta**" in lines
    assert "!kosc" in help_cog.HELP_TEXT and "!karta" in help_cog.HELP_TEXT
    line = next(line for line in help_cog.HELP_TEXT.split("\n") if "!kosc" in line)
    assert "Dovy" in line and "!karta" in line
