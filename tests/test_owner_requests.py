"""Owner requests of 2026-11-18: XP theft how-to, boss warning switch, ritual, gem pouch."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from altermmo import schema
from altermmo.cogs import events as events_cog
from altermmo.core import clock, guards, timing
from altermmo.core.state import BossPhase, EventType
from altermmo.features import achievements, album, bossdetect, daily, events, missions, pets, skills, xpsteal

from fakes import FakeInteraction, FakeMessage

SPARKLES = "✨"
RITUAL = "Rozpoczął się rytuał przywołania!"

OWNER_DEFAULTS = dict(pet_owned=True, reroll_scroll=0, reroll_scroll_shard=0, rebirth_stones=0, mirrors=0,
                      pet_id_alt1=0, pet_id_alt2=0, skill_gem=0, pet_id_alt3=0, pet_id_alt4=0)
PET_DEFAULTS = dict(pet_name="Towarzysz", pet_lvl=1, pet_skills=0, quality="Standard", shiny=False,
                    type="Cat", variant="0", crit_perc=0, replace_perc=0, def_perc=0, drop_perc=0,
                    lowhp_perc=0, slow_perc=0, init_perc=0, detection=True, ultra_shiny=False, mythic=False)


async def _insert(db, table: str, values: dict) -> None:
    marks = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    await db.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({marks})", *values.values())


async def add_owner(db, player_id: int, pet_id: int = 0, **columns) -> None:
    await _insert(db, "PETOWNER", {"player_id": player_id, "pet_id": pet_id, **OWNER_DEFAULTS, **columns})


async def add_gem(db, skill_id: int, variant: int) -> None:
    await _insert(db, "SKILLS", {"skill_id": skill_id, "gem_name": skills.gem_name(variant),
                                 "skill_variant": variant, "slow_perc": 7})


# ===================================== XP theft: how to use it =====================================

async def test_theft_winner_is_told_how_to_steal_on_the_channel_and_privately(game):
    thief = game.add_member("Zlodziej")
    xpsteal.grant(game.bot, thief.id, 10000)
    await xpsteal.remind(game.scene(), thief)
    hint = game.channel.messages[-1].content
    shown = "10" + chr(0xA0) + "000"                         # one way everywhere: xpsteal.number
    assert f"<@{thief.id}>" in hint and "**!kradnijxp nick**" in hint and f"do {shown} doświadczenia" in hint
    assert len(thief.dms) == 1 and "!kradnijxp nick" in thief.dms[0]
    assert f"<#{game.bot.ids.boss_channel}>" in thief.dms[0]
    # Owner's decision of 2026-11-22: the theft takes what the victim has, up to the amount.
    assert "tyle, ile ofiara ma" in hint and "tyle, ile ofiara ma" in thief.dms[0]
    assert f"do {shown} doświadczenia" in thief.dms[0]
    assert len(hint) < 400 and len(thief.dms[0]) < 400                # still short


async def test_theft_hint_survives_closed_private_messages_and_a_dead_channel(game):
    thief = game.add_member("Zlodziej")
    xpsteal.grant(game.bot, thief.id, 2500)

    async def closed(content=None, **kwargs):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")

    thief.send = closed
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("boom"), 1
    await xpsteal.remind(game.scene(), thief)                # must not raise: the right is stored anyway
    assert xpsteal.pending(game.bot, thief.id) == [2500]

    nobody = game.add_member("Ktos")                         # nothing to remind about: silence
    await xpsteal.remind(game.scene(), nobody)
    assert nobody.dms == [] and not game.channel.contains(f"<@{nobody.id}>")


# ===================================== boss warnings: the switch ===================================

@pytest_asyncio.fixture
async def sensing(game_db):
    """Ala and Ola own pets that sense bosses; Ala's private messages are closed."""
    db = game_db.bot.db
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, ala.id, 1)
    await add_owner(db, ola.id, 2)
    for pet_id in (1, 2):
        await _insert(db, "PETS", {"pet_id": pet_id, **PET_DEFAULTS})

    async def closed(content=None, **kwargs):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")

    ala.send = closed
    game_db.bot.debug = False                                # hints go to the notice channel
    return ala, ola


async def test_hint_has_a_button_that_switches_the_warnings_off(game_db, sensing):
    ala, ola = sensing
    bot = game_db.bot
    await pets.detect_boss(bot, 2)
    (hint,) = game_db.notice_channel.messages
    assert hint.content == bossdetect.MSG_UNLOCK_HINT.format(player_id=ala.id)
    button = hint.view.children[0]
    assert button.custom_id == bossdetect.BUTTON_ID and button.label == bossdetect.BUTTON_LABEL
    assert hint.view.timeout is None and hint.view.is_persistent()

    # Somebody else cannot switch Ala's warnings off; the answer is on the channel, for Ola.
    stranger = FakeInteraction(bot, ola, hint)
    await button.callback(stranger)
    refusal = f"<@{ola.id}> " + bossdetect.MSG_NOT_YOURS.format(player_id=ala.id)
    assert stranger.public == [refusal] and stranger.replies[0].allowed_mentions.users == [ola]
    assert not bossdetect.is_off(bot, ala.id) and not bossdetect.is_off(bot, ola.id)
    assert game_db.notice_channel.messages == [hint, stranger.replies[0].message]

    # Ala can: the answer is on the channel, the hint disappears, the next boss comes quietly.
    own = FakeInteraction(bot, ala, hint)
    await button.callback(own)
    assert own.public == [f"<@{ala.id}> {bossdetect.MSG_SWITCHED_OFF}"]
    assert bossdetect.is_off(bot, ala.id) and hint.deleted
    assert bot.store.detect_off.all() == [str(ala.id)]
    answers = [stranger.replies[0].message, own.replies[0].message]
    assert game_db.notice_channel.messages == answers

    await pets.detect_boss(bot, 2)
    assert game_db.notice_channel.messages == answers        # no new hint for Ala
    assert ola.dms == [bossdetect.dm_warning(bot, "Nadciąga boss! Będzie epicki.")] * 2  # Ola is still warned

    again = FakeInteraction(bot, ala, hint)
    await button.callback(again)                             # a second click changes nothing
    assert again.answers == [bossdetect.MSG_SWITCHED_OFF] and bot.store.detect_off.all() == [str(ala.id)]


async def test_wyczuwanie_switches_the_warnings_off_and_on(game_db, sensing):
    ala, ola = sensing
    bot = game_db.bot
    assert await bossdetect.toggle(game_db.scene(ola)) is False
    assert game_db.channel.messages[-1].content == bossdetect.MSG_NOW_OFF.format(player_id=ola.id)
    await pets.detect_boss(bot, 1)
    assert ola.dms == []
    assert await bossdetect.toggle(game_db.scene(ola)) is True
    assert game_db.channel.messages[-1].content == bossdetect.MSG_NOW_ON.format(player_id=ola.id)
    await pets.detect_boss(bot, 1)
    assert ola.dms == [bossdetect.dm_warning(bot, "Nadciąga boss! Będzie rzadki.")]


async def test_button_is_registered_for_hints_from_before_a_restart(game_db):
    bot = game_db.bot
    await bot.load_extension("altermmo.cogs.pets")
    assert any(isinstance(view, bossdetect.DetectionOffView) for view in bot.persistent_views)
    assert any(isinstance(view, bossdetect.DetectionOffDmView) for view in bot.persistent_views)
    assert bot.get_command("wyczuwanie") is not None and bot.get_command("powiadomienia") is not None


async def test_unreadable_switch_file_means_warn_as_usual(game, monkeypatch):
    def broken(player_id):
        raise OSError("disk")

    monkeypatch.setattr(game.bot.store.detect_off, "contains", broken)
    assert bossdetect.is_off(game.bot, 5) is False


# ===================================== ritual =====================================================

def test_ritual_is_drawn_now(monkeypatch):
    assert events_cog.PEAK_WEIGHTS[-1] > 0 and events_cog.OFF_PEAK_WEIGHTS[-1] > 0
    assert events_cog.EVENT_ORDER[-1] is EventType.RITUAL
    seen = []

    def choices(population, weights):
        seen.append(tuple(weights))
        return [population[-1]]

    monkeypatch.setattr(random, "choices", choices)
    evening = datetime(2026, 11, 17, 19, 0, tzinfo=clock.WARSAW)
    noon = datetime(2026, 11, 17, 12, 0, tzinfo=clock.WARSAW)
    assert events_cog.pick_event(evening, debug=False) is EventType.RITUAL
    assert events_cog.pick_event(noon, debug=False, exclude=(EventType.RITUAL,)) is EventType.RITUAL  # stubbed draw
    assert seen == [events_cog.PEAK_WEIGHTS, (*events_cog.OFF_PEAK_WEIGHTS[:-1], 0)]
    # Debug mode draws only the ritual: without it nothing is left.
    assert events_cog.pick_event(noon, debug=True, exclude=(EventType.RITUAL,)) is None


async def test_ritual_is_not_started_right_before_the_scheduled_boss(game, monkeypatch):
    bot = game.bot
    bot.debug = False
    cog = events_cog.EventsCog(bot)
    ran: list[EventType] = []

    async def run(event_type):
        ran.append(event_type)

    monkeypatch.setattr(cog, "_run", run)
    draws = iter([EventType.RITUAL, EventType.CHEST, EventType.RITUAL, EventType.RITUAL])

    def pick(now, *, debug, exclude=()):
        event = next(draws)
        assert event not in exclude or event is EventType.RITUAL and not exclude
        return event

    monkeypatch.setattr(events_cog, "pick_event", pick)

    cycle = bot.boss_cycle
    cycle.arrives_at = clock.utcnow() + timedelta(seconds=600)     # sooner than 10 min + margin
    await bot.state.set_boss_phase(BossPhase.WAITING)
    assert not cog.ritual_fits()
    assert await cog.run_one_event() is EventType.CHEST            # another event is played instead

    cycle.arrives_at = clock.utcnow() + timedelta(seconds=3600)
    assert cog.ritual_fits()
    assert await cog.run_one_event() is EventType.RITUAL

    # The admin command is not second-guessed.
    cycle.arrives_at = clock.utcnow() + timedelta(seconds=60)
    assert await cog.run_one_event(forced=EventType.RITUAL) is EventType.RITUAL
    assert ran == [EventType.CHEST, EventType.RITUAL, EventType.RITUAL]

    await bot.state.set_boss_phase(BossPhase.VISIBLE)              # a boss is standing there
    assert not cog.ritual_fits()


@pytest.fixture
def boss_image(game, monkeypatch):
    calls: list[tuple[int, bool]] = []

    async def send_boss_image(scene, rarity):
        calls.append((rarity, game.bot.state.busy))
        await scene.send(f"BOSS rarity {rarity}")
        return True, "Szef"

    monkeypatch.setattr(events.boss, "send_boss_image", send_boss_image)
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.01)             # debug wait 5 s -> 0.05 s
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: 2.0)
    return calls


def join_ritual(game, *members, then=None) -> None:
    """``members`` click "Dołącz" under the ritual panel, then ``then()`` runs (still during the sign-up)."""
    def hook(message) -> None:
        if RITUAL in message.text and message.view is not None:
            async def join() -> None:
                for member in members:
                    await game.click(message, member, "join")
                if then is not None:
                    then()
            game._tasks.append(asyncio.ensure_future(join()))
    game.channel.send_hooks.append(hook)


async def test_ritual_sign_up_leaves_the_channel_free_and_the_boss_can_be_attacked(game, boss_image):
    bot = game.bot
    ala = game.add_member("Ala")
    busy_during_sign_up = []
    join_ritual(game, ala, then=lambda: busy_during_sign_up.append((bot.state.busy, bot.state.event_alive)))

    cog = events_cog.EventsCog(bot)
    assert await cog.run_one_event(forced=EventType.RITUAL) is EventType.RITUAL
    assert busy_during_sign_up == [(False, True)]                  # hunts work, other events wait
    assert boss_image == [(2, True)]                               # the channel is held while the boss appears
    state = bot.state
    assert state.boss_phase is BossPhase.VISIBLE and not state.busy and not state.event_alive
    assert (state.boss.rarity, state.boss.is_player_boss, state.boss.player_boss) == (2, True, "Szef")
    assert game.channel.contains("BOSS rarity 2") and not game.channel.contains(RITUAL)
    assert not game.channel.ever_sent(events.MSG_RITUAL_OVERTAKEN)


async def test_ritual_can_summon_a_special_boss_and_its_variant_reaches_the_boss_cycle(game, special_bosses,
                                                                                       monkeypatch):
    """The real boss picture (features/bossvariants.py): what the ritual showed is what is fought."""
    from altermmo.features import bossvariants, fight_types

    bot = game.bot
    special_bosses(chance_percent=20)                                      # the tests' own config
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.01)
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: 2.0)            # an epic boss
    monkeypatch.setattr(random, "randint", lambda low, high: low)          # a monster, its first picture
    monkeypatch.setattr(random, "random", lambda: 0.0)                     # ...in its special variant
    join_ritual(game, game.add_member("Ala"))

    cog = events_cog.EventsCog(bot)
    assert await cog.run_one_event(forced=EventType.RITUAL) is EventType.RITUAL
    state = bot.state
    assert state.boss_phase is BossPhase.VISIBLE and not state.busy and not state.event_alive
    assert (state.boss.rarity, state.boss.is_player_boss, state.boss.variant) == (2, False, "mgla")
    picture, embed_message = game.channel.messages[-2:]
    assert picture.files == ["100.gif"] and bossvariants.HINT in embed_message.embeds[0].description

    # The fight the boss cycle starts is told the variant (and only a special boss's fight is).
    fights: list[dict] = []

    async def single_init(scene, rarity):
        return scene.author

    async def single_fight(scene, hunter, rarity, is_player_boss, player_boss, **special):
        fights.append(special)
        return fight_types.FightResult(won=True)

    async def reset_end(bot_):
        return None

    monkeypatch.setattr(events.boss, "single_init", single_init)
    monkeypatch.setattr(events.boss, "single_fight", single_fight)
    monkeypatch.setattr(events.boss, "reset_end", reset_end)
    await bot.boss_cycle.attack(game.scene(game.add_member("Ola")))
    assert fights == [{"variant": "mgla"}] and bot.state.boss.variant == ""


@pytest.mark.parametrize("result, variant", [
    ((True, 2, False, None), ""),                 # the four values of before the special bosses
    ((True, 2, False, None, None), ""),           # a fifth value that is not a text
    ((True, 2, False, None, "mgla"), "mgla")])
async def test_ritual_result_without_a_variant_means_an_ordinary_boss(game, monkeypatch, result, variant):
    """The cog reads the fifth value of ``spawn_ritual`` tolerantly: without it the boss is an ordinary one."""
    bot = game.bot

    async def spawn_ritual(scene):
        return result

    monkeypatch.setattr(events, "spawn_ritual", spawn_ritual)
    cog = events_cog.EventsCog(bot)
    assert await cog.run_one_event(forced=EventType.RITUAL) is EventType.RITUAL
    state = bot.state
    assert state.boss_phase is BossPhase.VISIBLE and not state.busy and not state.event_alive
    assert (state.boss.rarity, state.boss.is_player_boss, state.boss.variant) == (2, False, variant)


async def test_ritual_waits_for_a_running_hunt_before_showing_the_boss(game, boss_image, monkeypatch):
    bot = game.bot
    ala = game.add_member("Ala")
    order: list[str] = []

    async def hunt() -> None:
        with bot.state.hold_busy("hunt"):
            await asyncio.sleep(0.3)
            order.append("hunt over")

    join_ritual(game, ala, then=lambda: asyncio.ensure_future(hunt()))
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *args, **kwargs):
        await real_sleep(min(seconds, 0.05))

    monkeypatch.setattr(events.asyncio, "sleep", fast_sleep)
    result = await events.spawn_ritual(game.scene())
    order.append("boss shown")
    assert result == (True, 2, True, "Szef", "") and order == ["hunt over", "boss shown"]
    assert boss_image == [(2, True)] and not bot.state.busy


async def test_ritual_never_pushes_the_scheduled_boss_aside(game, boss_image):
    """The scheduled boss starts to arrive during the sign-up: the ritual ends without a boss."""
    bot = game.bot
    ala = game.add_member("Ala")
    join_ritual(game, ala, then=lambda: asyncio.ensure_future(bot.state.set_boss_phase(BossPhase.PRE_SPAWN)))

    cog = events_cog.EventsCog(bot)
    await bot.state.set_boss_phase(BossPhase.WAITING)
    assert await cog.run_one_event(forced=EventType.RITUAL) is EventType.RITUAL
    assert boss_image == []                                        # no picture of a boss nobody can attack
    assert game.channel.contains(events.MSG_RITUAL_OVERTAKEN)
    assert bot.state.boss_phase is BossPhase.PRE_SPAWN             # the scheduled boss goes on
    assert not bot.state.busy and not bot.state.event_alive and bot.state.free_for_activity is False


# ===================================== the gem pouch ==============================================

@pytest_asyncio.fixture
async def collector(game_db):
    """Ala owns three gems: REST (id 5, active), SUMMON (id 8) and SLOW (id 9) in the pouch."""
    db = game_db.bot.db
    ala = game_db.add_member("Ala")
    for skill_id, variant in ((5, skills.REST), (8, skills.SUMMON), (9, skills.SLOW)):
        await add_gem(db, skill_id, variant)
    await add_owner(db, ala.id, skill_gem=5)
    for skill_id in (9, 8):
        await db.execute("INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID) VALUES ($1, $2)", ala.id, skill_id)
    return ala


async def active_gem(db, player_id: int) -> int:
    return int(await db.fetchval("SELECT SKILL_GEM FROM PETOWNER WHERE PLAYER_ID = $1", player_id))


async def pouch(db, player_id: int) -> list[int]:
    rows = await db.fetch("SELECT SKILL_ID FROM SKILLPOUCH WHERE PLAYER_ID = $1 ORDER BY SKILL_ID", player_id)
    return [int(row["skill_id"]) for row in rows]


async def test_pouch_lists_every_gem_with_a_stable_number(game_db, collector):
    ala = collector
    assert await skills.show_skills(game_db.scene(ala)) is True
    embed = game_db.channel.messages[-1].embeds[0]
    assert embed.title == skills.POUCH_TITLE
    assert ("**1. Klejnot Regeneracji** ✅ aktywny\n2. Klejnot Przywołania\n3. Klejnot Spowolnienia"
            in embed.description)
    assert "!wybierzklejnot" in embed.description and embed.footer.text == skills.POUCH_FOOTER

    skills.daily_skill_used(game_db.bot, ala.id)                   # one gem a day: all of them wait now
    await skills.show_skills(game_db.scene(ala))
    text = game_db.channel.messages[-1].embeds[0].description
    tomorrow = f"<t:{int(daily.next_daily_reset().timestamp())}:R>"   # the next daily reset, live in Discord
    assert f"**1. Klejnot Regeneracji** ✅ aktywny *(regeneruje się, gotowy {tomorrow})*" in text
    assert (f"2. Klejnot Przywołania *(regeneruje się, gotowy {tomorrow})*\n" in text
            and f"3. Klejnot Spowolnienia *(regeneruje się, gotowy {tomorrow})*" in text)

    await skills.clear_daily_skill(game_db.bot)
    skills.weekly_skill_used(game_db.bot, ala.id)                  # only the weekly gem regenerates
    await skills.show_skills(game_db.scene(ala))
    text = game_db.channel.messages[-1].embeds[0].description
    monday = f"<t:{int(daily.next_season_end().timestamp())}:R>"     # the weekly reset: Monday 15:00
    assert "**1. Klejnot Regeneracji** ✅ aktywny\n" in text
    assert f"2. Klejnot Przywołania *(regeneruje się, gotowy {monday})*\n3. Klejnot Spowolnienia\n" in text


async def test_only_one_gem_can_be_used_a_day(game_db, collector):
    """Owner's rule of 2026-11-18: one gem use a day, whichever gem it is."""
    ala = collector
    bot = game_db.bot
    assert await skills.choose_skill(game_db.scene(ala), "3") is True          # the slowing gem (daily)
    assert await skills.use_skill(game_db.scene(ala), ala) == skills.SLOW
    assert bot.store.skill_daily_cd.all() == [str(ala.id)]

    assert await skills.choose_skill(game_db.scene(ala), "2") is True          # the summoning gem (weekly)
    assert await skills.use_skill(game_db.scene(ala), ala) == 0
    assert game_db.channel.messages[-1].content == skills.one_gem_text(ala.id)
    assert f"<t:{int(daily.next_daily_reset().timestamp())}:R>" in skills.one_gem_text(ala.id)
    assert bot.store.skill_weekly_cd.all() == []                               # the weekly use is not spent

    await skills.clear_daily_skill(bot)
    assert await skills.use_skill(game_db.scene(ala), ala) == skills.SUMMON    # a weekly gem is that day's use too
    assert bot.store.skill_daily_cd.all() == [str(ala.id)] and bot.store.skill_weekly_cd.all() == [str(ala.id)]
    assert await skills.choose_skill(game_db.scene(ala), "3") is True          # never used, but today's use is gone:
    assert await skills.use_skill(game_db.scene(ala), ala) == 0                # the answer names the rule,
    assert game_db.channel.messages[-1].content == skills.one_gem_text(ala.id)  # not "your gem regenerates"

    await skills.clear_daily_skill(bot)                                        # the next day: a daily gem works,
    assert await skills.use_skill(game_db.scene(ala), ala) == skills.SLOW      # the weekly one still regenerates
    assert await skills.choose_skill(game_db.scene(ala), "2") is True
    assert await skills.use_skill(game_db.scene(ala), ala) == 0
    assert game_db.channel.messages[-1].content == skills.weekly_cd_text()
    assert f"<t:{int(daily.next_season_end().timestamp())}:R>" in skills.weekly_cd_text()


async def test_summoning_gem_that_summons_nothing_gives_the_day_back(game_db, collector):
    ala = collector
    bot = game_db.bot
    assert await skills.choose_skill(game_db.scene(ala), "2") is True
    assert await skills.use_skill(game_db.scene(ala), ala) == skills.SUMMON
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)                        # the scheduled boss started to arrive
    assert await skills.skill_summon_boss(game_db.scene(ala)) is False
    assert bot.store.skill_daily_cd.all() == [] and bot.store.skill_weekly_cd.all() == []


async def test_choosing_a_gem_swaps_it_with_the_active_one(game_db, collector):
    ala = collector
    db = game_db.bot.db
    assert await skills.choose_skill(game_db.scene(ala), "3") is True
    assert game_db.channel.messages[-1].content == skills.MSG_GEM_CHOSEN.format(
        player_id=ala.id, name="Klejnot Spowolnienia")
    assert await active_gem(db, ala.id) == 9 and await pouch(db, ala.id) == [5, 8]

    # The numbers did not move: 1 is still the gem of rest.
    await skills.show_skills(game_db.scene(ala))
    assert ("1. Klejnot Regeneracji\n2. Klejnot Przywołania\n**3. Klejnot Spowolnienia** ✅ aktywny"
            in game_db.channel.messages[-1].embeds[0].description)

    assert await skills.choose_skill(game_db.scene(ala), 3) is False
    assert game_db.channel.messages[-1].content == skills.MSG_ALREADY_ACTIVE.format(
        player_id=ala.id, name="Klejnot Spowolnienia")
    for bad in ("0", "4", "-1", "abc", "", None):
        assert await skills.choose_skill(game_db.scene(ala), bad) is False
        assert game_db.channel.messages[-1].content == skills.MSG_NO_SUCH_NUMBER.format(player_id=ala.id)
    assert await active_gem(db, ala.id) == 9 and await pouch(db, ala.id) == [5, 8]
    assert len(await db.fetch("SELECT 1 FROM SKILLS")) == 3        # gems are never lost or copied


async def test_only_the_active_gem_works(game_db, collector):
    ala = collector
    bot = game_db.bot
    await skills.choose_skill(game_db.scene(ala), 3)               # the gem of slowing
    assert await skills.use_skill(game_db.scene(ala), ala) == skills.SLOW
    assert bot.store.modifiers.load()["time_increased_perc"] == skills.SLOW_TIME_BONUS
    assert bot.store.skill_daily_cd.all() == [str(ala.id)]
    await skills.show_skill(game_db.scene(ala))
    assert game_db.channel.messages[-1].embeds[0].title == "Klejnot Spowolnienia"


async def test_players_without_gems_and_two_players_at_once(game_db, collector):
    ala = collector
    db = game_db.bot.db
    ola = game_db.add_member("Ola")
    ela = game_db.add_member("Ela")
    await add_owner(db, ela.id)
    for player in (ola, ela):
        assert await skills.show_skills(game_db.scene(player)) is False
        assert await skills.choose_skill(game_db.scene(player), 1) is False
        assert game_db.channel.messages[-1].content == skills.MSG_NO_SKILL.format(player_id=player.id)

    # Racing commands of one player never lose or duplicate a gem.
    await asyncio.gather(*(skills.choose_skill(game_db.scene(ala), number) for number in (2, 3, 1, 2, 3)))
    assert sorted([await active_gem(db, ala.id), *await pouch(db, ala.id)]) == [5, 8, 9]


async def test_a_gem_without_its_row_is_skipped_and_an_orphaned_pouch_still_works(game_db):
    db = game_db.bot.db
    ala = game_db.add_member("Ala")
    await add_gem(db, 8, skills.SUMMON)
    await add_owner(db, ala.id, skill_gem=77)                      # the active gem has no SKILLS row
    await db.execute("INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID) VALUES ($1, 8), ($1, 8)", ala.id)
    assert await skills.show_skills(game_db.scene(ala)) is True
    assert "1. Klejnot Przywołania" in game_db.channel.messages[-1].embeds[0].description
    assert await skills.choose_skill(game_db.scene(ala), 1) is True
    assert await active_gem(db, ala.id) == 8
    assert await skills.assign_skill(game_db.scene(), ala) is True   # the next gem goes to the pouch
    assert await active_gem(db, ala.id) == 8


@pytest_asyncio.fixture
async def skills_cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.skills")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
        channel.permissions_for = lambda member: member.guild_permissions
    guards._last_notice.clear()
    return game_db.bot.get_cog("Skills")


async def run(game, member, content: str, *, channel=None) -> FakeMessage:
    channel = channel or game.channel
    message = FakeMessage(channel, member, content)
    message.edited_at = None
    message._state = None
    channel.messages.append(message)
    await game.bot.invoke(await game.bot.get_context(message))
    await game.settle()
    return message


async def test_pouch_commands(game_db, collector, skills_cog):
    ala = collector
    db = game_db.bot.db
    elsewhere = game_db.announce_channel
    for command in ("!klejnoty", "!skille", "!sakwa"):
        await run(game_db, ala, command, channel=elsewhere)        # display only: any channel
    assert [m.embeds[0].title for m in elsewhere.bot_messages] == [skills.POUCH_TITLE] * 3

    await run(game_db, ala, "!wybierzklejnot 2", channel=elsewhere)  # gameplay: boss channel only
    assert await active_gem(db, ala.id) == 5 and len(elsewhere.bot_messages) == 3
    await run(game_db, ala, "!wybierzskill 2")                     # the alias
    assert await active_gem(db, ala.id) == 8
    await run(game_db, ala, "!ws 1")
    assert await active_gem(db, ala.id) == 5

    await run(game_db, ala, "!wybierzklejnot")                       # a missing number gets a hint
    assert "!wybierzklejnot 2" in game_db.channel.messages[-1].content

    with game_db.bot.state.hold_busy("hunt"):                      # not in the middle of a hunt
        await run(game_db, ala, "!wybierzklejnot 3")
    assert await active_gem(db, ala.id) == 5
    assert game_db.channel.messages[-1].content == guards.BUSY_NOTICE


async def test_admin_can_hand_out_gems(game_db, skills_cog):
    db = game_db.bot.db
    admin = game_db.add_member("Alter")
    admin.guild_permissions = discord.Permissions(administrator=True)
    ala = game_db.add_member("Ala")
    await run(game_db, admin, f"!DajKlejnot {ala.id} 5")
    await run(game_db, admin, f"!DajKlejnot {ala.id} 5")           # the same kind twice: refused
    await run(game_db, admin, f"!DajKlejnot {ala.id}")
    await run(game_db, admin, f"!DajKlejnot {ala.id} 9")           # no such kind
    await run(game_db, ala, f"!DajKlejnot {ala.id}")               # not an admin: nothing
    rows = await db.fetch("SELECT SKILL_VARIANT FROM SKILLS ORDER BY SKILL_ID")
    assert len(rows) == 2 and int(rows[0]["skill_variant"]) == skills.SLOW
    assert int(rows[1]["skill_variant"]) in (skills.REST, skills.SHINY, skills.SUMMON)
    assert await active_gem(db, ala.id) == 1 and await pouch(db, ala.id) == [2]


async def test_new_table_is_created_at_start_up_and_then_verified(db):
    # The old bot's database: no gem pouch (new in v2).
    await db.execute("DROP TABLE SKILLPOUCH", idempotent=True)
    await db.execute("DROP TABLE IF EXISTS ACHIEVEMENT_COUNTERS", idempotent=True)
    problems = await schema.verify(db)
    assert "brak tabeli skillpouch" in problems
    # The achievement counters (made and withdrawn on 2026-11-20) are no table of the bot any more.
    assert not any("achievement_counters" in problem for problem in problems)
    # the gem pouch; the pet album (2026-11-21); the companion missions (2026-11-25)
    new_tables = 1 + len(album.NEW_TABLES) + len(missions.NEW_TABLES)
    assert await schema.create_new_tables(db) == new_tables
    await db.execute("INSERT INTO SKILLPOUCH (PLAYER_ID, SKILL_ID) VALUES (1, 2)")
    assert await schema.create_new_tables(db) == new_tables        # again: nothing breaks, nothing is lost
    assert await db.fetchval("SELECT COUNT(*) FROM SKILLPOUCH") == 1
    assert await db.fetchval("SELECT to_regclass('achievement_counters')") is None   # not made again
    assert await schema.verify(db) == []
    rows = await db.fetch(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'skillpouch' ORDER BY ordinal_position""")
    assert [tuple(row) for row in rows] == [("player_id", "numeric"), ("skill_id", "numeric")]


async def test_withdrawn_counters_table_is_left_alone(db):
    """The table made on the morning of 2026-11-20 stays as it is: the bot never drops anything."""
    await db.execute("""CREATE TABLE IF NOT EXISTS ACHIEVEMENT_COUNTERS (
                            PLAYER_ID NUMERIC, NAME VARCHAR(32), VALUE NUMERIC, PRIMARY KEY (PLAYER_ID, NAME))""",
                     idempotent=True)
    await db.execute("INSERT INTO ACHIEVEMENT_COUNTERS (PLAYER_ID, NAME, VALUE) VALUES (1, 'duels', 3)")
    await schema.create_new_tables(db)
    await achievements.create_database(SimpleNamespace(db=db))     # (the legacy table only)
    assert await schema.verify(db) == []
    assert await db.fetchval("SELECT VALUE FROM ACHIEVEMENT_COUNTERS WHERE PLAYER_ID = 1") == 3


# ===================================== review fixes ===============================================

async def test_ritual_is_not_drawn_while_spawning_is_switched_off(game):
    bot = game.bot
    cog = events_cog.EventsCog(bot)
    assert cog.ritual_fits()
    bot.store.runtime_set("boss_spawning_enabled", False)
    assert not cog.ritual_fits()


async def test_summoning_gem_waits_for_a_ritual_or_duel_sign_up_to_end(game_db):
    db = game_db.bot.db
    bot = game_db.bot
    ala = game_db.add_member("Ala")
    await add_gem(db, 5, skills.SUMMON)
    await add_owner(db, ala.id, skill_gem=5)
    for event in (EventType.RITUAL, EventType.DUEL):
        with bot.state.running_event(event, exclusive=False):
            assert await skills.use_skill(game_db.scene(ala), ala) == 0
        assert game_db.channel.messages[-1].content == skills.MSG_EVENT_IN_THE_WAY.format(player_id=ala.id)
    assert bot.store.skill_weekly_cd.all() == []                   # the weekly use was not spent
    assert await skills.use_skill(game_db.scene(ala), ala) == skills.SUMMON


async def test_pouch_table_is_created_on_first_use_when_start_up_could_not(game_db):
    db = game_db.bot.db
    ala = game_db.add_member("Ala")
    await db.execute("DROP TABLE SKILLPOUCH", idempotent=True)     # first start: the database was down
    assert await skills.assign_skill(game_db.scene(), ala) is True
    assert await skills.assign_skill(game_db.scene(), ala) is True
    assert await skills.show_skills(game_db.scene(ala)) is True
    assert len(await pouch(db, ala.id)) == 1


async def test_start_up_creates_the_new_table_and_reports_a_broken_database(game_db, monkeypatch):
    bot = game_db.bot
    db = bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    await db.execute("DROP TABLE SKILLPOUCH", idempotent=True)
    await bot.setup_hook()
    assert await schema.verify(db) == [] and bot._startup_notes == []

    await db.execute("DROP TABLE TOWER", idempotent=True)          # something the bot cannot repair itself
    await bot.setup_hook()
    assert len(bot._startup_notes) == 1 and "brak tabeli tower" in bot._startup_notes[0]


def test_start_up_note_about_the_petowner_columns():
    """(Companion missions, spec rev. 2 §13.5) the columns of PETOWNER that could not be added say what is off."""
    from altermmo import bot as bot_module

    note = bot_module.new_columns_note
    missing = schema.missing_column
    mission_only = [missing("petowner", column) for column in pets.MISSION_COLUMNS]
    owner_all = [missing("petowner", column) for column in (*pets.UPGRADE_COLUMNS, *pets.MISSION_COLUMNS)]
    album_only = [missing("pet_album", "frame"), missing("pet_album", "holo_partial")]
    assert note(["x"], mission_only) == bot_module.MSG_MISSION_COLUMNS_FAILED.format(problems="x")
    assert note(["x"], owner_all) == bot_module.MSG_OWNER_COLUMNS_FAILED.format(problems="x")
    assert note(["x", "y"], [*mission_only, *album_only]) == (
        bot_module.MSG_ALBUM_COLUMNS_FAILED.format(problems="x; y") + " "
        + bot_module.MSG_MISSION_COLUMNS_FAILED.format(problems="x; y"))
    assert "Duża sakwa i Zwoje przywołania są WYŁĄCZONE" in bot_module.MSG_MISSION_COLUMNS_FAILED
    assert "Miejsca w stajni 5-10" in bot_module.MSG_OWNER_COLUMNS_FAILED and "$" not in (
        bot_module.MSG_OWNER_COLUMNS_FAILED + bot_module.MSG_MISSION_COLUMNS_FAILED)
    # Nothing was checked when the database did not answer: the missions are said conditionally.
    down = bot_module.MSG_DATABASE_DOWN_AT_START
    assert "tabelę PET_MISSIONS (zlecenia towarzyszy) bot utworzy sam przy pierwszym otwarciu tablicy zleceń" in down
    assert "BIG_POUCH / POUCH_PET_ID2 / SUMMON_SCROLLS" in down and down.endswith("Reszta gry działa normalnie.")


async def test_start_up_notes_when_only_the_mission_columns_cannot_be_added(game_db, monkeypatch):
    """Today's production PETOWNER (the merchant's columns there): only the three missions' columns are tried."""
    bot = game_db.bot
    db = bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    for column in pets.MISSION_COLUMNS:
        await db.execute(f"ALTER TABLE PETOWNER DROP COLUMN {column}", idempotent=True)
    async with db.transaction() as somebody:
        await somebody.execute("LOCK TABLE PETOWNER IN ACCESS SHARE MODE")
        await bot.setup_hook()
    [note] = bot._startup_notes
    problem = note.removeprefix("Nie udało się dodać do bazy danych kolumn nowych w v2: ").split(". Duża sakwa")[0]
    assert problem.startswith("petowner (big_pouch, pouch_pet_id2, summon_scrolls): LockNotAvailableError")
    assert note == bot_module_note(problem)
    bot._startup_notes = []
    await bot.setup_hook()                                         # the next start adds them: nothing to say
    assert bot._startup_notes == [] and await schema.verify(db) == []


def bot_module_note(problem: str) -> str:
    from altermmo import bot as bot_module

    return bot_module.MSG_MISSION_COLUMNS_FAILED.format(problems=problem)


async def test_click_is_answered_even_when_the_switch_cannot_be_saved(game, monkeypatch):
    bot = game.bot
    ala = game.add_member("Ala")
    await bossdetect.send_hint(bot, game.notice_channel, ala.id)
    (hint,) = game.notice_channel.messages
    assert hint.view.is_finished()                                 # not kept by discord.py per hint

    def broken(player_id):
        raise OSError("disk full")

    monkeypatch.setattr(bot.store.detect_off, "add", broken)
    interaction = FakeInteraction(bot, ala, hint)
    await hint.view.children[0].callback(interaction)
    assert interaction.answers == [bossdetect.MSG_NOT_SAVED] and interaction.replies[0].message is not None
    assert not hint.deleted and any("powiadomień o bossie" in line for line in bot.owner_log)


def test_broken_theft_file_entries_are_ignored(game):
    bot = game.bot
    bot.store.xp_thefts.save({"1": None, "2": 2500, "3": ["x", 1000, None], "4": [2500]})
    assert [xpsteal.pending(bot, player) for player in (1, 2, 3, 4, 5)] == [[], [], [1000], [2500], []]
    assert xpsteal._consume(bot, 3) == 1000 and xpsteal._consume(bot, 1) is None   # broken ones skipped
    assert bot.store.xp_thefts.load() == {"1": None, "2": 2500, "4": [2500]}
