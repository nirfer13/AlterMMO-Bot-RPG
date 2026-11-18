"""The big boss with buttons: "Zaatakuj" under the picture, the single lobby, the legendary lobby panel."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import discord
import pytest

from altermmo.core import io, timing, ui
from altermmo.core.state import BossInfo, BossPhase
from altermmo.features import boss, boss_cycle
from altermmo.features.fight_types import FightInterrupted, FightResult

from fakes import FakeInteraction, requested_word


def not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown message")


@pytest.fixture
def registered(game):
    """What the boss cog does at start-up: the permanent "Zaatakuj" view is registered."""
    assert ui.register(game.bot, boss_cycle.AttackView())
    return game


@pytest.fixture
def fights(monkeypatch):
    """Stand-ins for the lobby and the fight: the encounter itself is not under test here."""
    record = SimpleNamespace(calls=[], error=None)

    async def single_init(scene, rarity):
        record.calls.append(("init", scene.author.name, rarity))
        return scene.author

    async def single_fight(scene, hunter, rarity, is_player_boss, player_boss):
        record.calls.append(("fight", hunter.name, rarity))
        if record.error is not None:
            error, record.error = record.error, None
            raise error
        return FightResult(won=True)

    async def reset_end(bot):
        record.calls.append("reset_end")

    monkeypatch.setattr(boss, "single_init", single_init)
    monkeypatch.setattr(boss, "single_fight", single_fight)
    monkeypatch.setattr(boss, "reset_end", reset_end)
    return record


async def show_boss(game, monkeypatch, rarity: int = 0) -> FakeMessage:
    """Post the real boss picture and make the boss attackable; returns the message with the button."""
    monkeypatch.setattr(random, "randint", lambda a, b: a)          # a monster, not a player
    await boss.send_boss_image(game.scene(), rarity)
    game.bot.state.boss = BossInfo(rarity=rarity)
    await game.bot.state.set_boss_phase(BossPhase.VISIBLE)
    return game.channel.messages[-1]


# -- "Zaatakuj" under the boss picture ----------------------------------------------------

async def test_boss_picture_carries_a_permanent_attack_button(game, monkeypatch):
    picture = await show_boss(game, monkeypatch)
    button = picture.button("Zaatakuj")
    assert button.custom_id == boss_cycle.ATTACK_BUTTON_ID == "altermmo:boss:attack"
    assert button.style is discord.ButtonStyle.primary and str(button.emoji) == "⚔️"
    assert picture.view.is_finished()                     # answered by the registered view, also after a restart
    assert await game.bot.boss_cycle.retire_attack_buttons() == [picture]
    assert picture.view is None


async def test_the_boss_cog_registers_the_button(game, monkeypatch):
    from altermmo.cogs.boss import BossCog

    monkeypatch.setattr(game.bot.boss_cycle, "start", lambda: None)
    await BossCog(game.bot).cog_load()
    assert any(getattr(item, "custom_id", None) == boss_cycle.ATTACK_BUTTON_ID
               for view in game.bot.persistent_views for item in view.children)


async def test_click_attacks_like_the_typed_command(registered, fights, monkeypatch):
    game = registered
    ala = game.add_member("Ala")
    picture = await show_boss(game, monkeypatch, rarity=1)
    click = await game.click(picture, ala, "Zaatakuj")              # runs the whole (stubbed) encounter
    assert click.response.deferred and click.replies == []          # acknowledged at once, nothing to say
    assert fights.calls == [("init", "Ala", 1), ("fight", "Ala", 1), "reset_end"]
    assert picture.view is None                                     # gone as soon as the boss was engaged
    assert game.bot.state.boss_phase is BossPhase.IDLE


async def test_attack_button_refusals_are_private(registered, fights, monkeypatch):
    game = registered
    bot = game.bot
    ala = game.add_member("Ala")
    dead = game.add_member("Jan", roles=[bot.ids.dead_hunter_role])
    game.channel.silenced_roles.add(bot.ids.dead_hunter_role)

    # No boss: the typed command says it in public, the button privately.
    await bot.state.set_boss_phase(BossPhase.WAITING)
    old = await boss_cycle.with_attack_button(lambda view: game.channel.send("stary boss", view=view))
    refused = await game.click(old, ala, "Zaatakuj")
    assert refused.whispers == [boss_cycle.MSG_NOT_ATTACKABLE.format(user_id=ala.id)]
    assert not game.channel.contains("Nie możesz zaatakować bossa")

    picture = await show_boss(game, monkeypatch)
    assert (await game.click(picture, dead, "Zaatakuj")).whispers == [ui.MSG_CANNOT_PLAY]   # a dead hunter
    elsewhere = await boss_cycle.with_attack_button(lambda view: game.notice_channel.send("boss", view=view))
    assert (await game.click(elsewhere, ala, "Zaatakuj")).whispers == [
        ui.MSG_WRONG_CHANNEL.format(channel_id=bot.ids.boss_channel)]
    assert fights.calls == [] and bot.state.boss_phase is BossPhase.VISIBLE

    assert bot.state.try_engage_boss()                               # somebody is choosing the fighter
    assert (await game.click(picture, ala, "Zaatakuj")).whispers == [boss_cycle.MSG_BOSS_TAKEN]
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    assert (await game.click(picture, ala, "Zaatakuj")).whispers == [
        boss_cycle.MSG_NOT_ATTACKABLE.format(user_id=ala.id)]
    assert fights.calls == []


async def test_typed_zaatakuj_still_works_and_takes_the_button_off(game, fights, monkeypatch):
    ala = game.add_member("Ala")
    picture = await show_boss(game, monkeypatch)
    await game.bot.boss_cycle.attack(game.scene(ala, "$zaatakuj"))
    assert fights.calls[:2] == [("init", "Ala", 0), ("fight", "Ala", 0)]
    assert picture.view is None


async def test_interrupted_fight_brings_the_button_back(registered, fights, monkeypatch):
    game = registered
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    picture = await show_boss(game, monkeypatch)
    fights.error = FightInterrupted("lag")
    await game.click(picture, ala, "Zaatakuj")
    assert game.bot.state.boss_phase is BossPhase.VISIBLE and picture.view is None
    await asyncio.sleep(0.05)                                        # the notice is posted in the background
    notice = game.channel.messages[-1]
    assert notice.content == boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS and "kliknij **Zaatakuj**" in notice.content
    assert notice.button_labels == ["Zaatakuj"]
    click = await game.click(notice, ola, "Zaatakuj")                # the second try, by a click
    assert click.response.deferred and ("fight", "Ola", 0) in fights.calls
    assert notice.view is None and game.bot.state.boss_phase is BossPhase.IDLE


async def test_button_goes_back_under_the_picture_when_the_notice_cannot_be_posted(registered, fights,
                                                                                  monkeypatch):
    game = registered
    ala = game.add_member("Ala")
    picture = await show_boss(game, monkeypatch)
    fights.error = FightInterrupted("lag")
    await game.click(picture, ala, "Zaatakuj")
    game.channel.fail_with = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
    game.channel.fail_sends = 1
    await asyncio.sleep(0.05)
    assert not game.channel.contains(boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS)
    assert picture.button_labels == ["Zaatakuj"]
    await game.click(picture, ala, "Zaatakuj")
    assert fights.calls.count(("fight", "Ala", 0)) == 2


@pytest.mark.parametrize("delivered", [True, False])
async def test_late_interrupted_notice_brings_no_button_after_the_boss_was_engaged_again(registered, fights,
                                                                                       monkeypatch, delivered):
    """The "still here" notice is late (Discord is slow) and the boss was fought again meanwhile."""
    game = registered
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    gate = asyncio.Event()
    real_send_patient = io.send_patient

    async def send_patient(destination, content=None, **kwargs):
        if content == boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS:
            await gate.wait()
            if not delivered:
                return None                                          # Discord finally refused it
        return await real_send_patient(destination, content, **kwargs)

    monkeypatch.setattr(io, "send_patient", send_patient)
    picture = await show_boss(game, monkeypatch)
    fights.error = FightInterrupted("lag")
    await game.click(picture, ala, "Zaatakuj")
    assert game.bot.state.boss_phase is BossPhase.VISIBLE
    await asyncio.sleep(0.05)                                        # the notice is on its way
    await game.bot.boss_cycle.attack(game.scene(ola, "$zaatakuj"))  # the second try, before the notice
    assert ("fight", "Ola", 0) in fights.calls and game.bot.state.boss_phase is BossPhase.IDLE
    gate.set()
    await asyncio.sleep(0.1)
    if delivered:
        notice = next(m for m in game.channel.messages if m.content == boss_cycle.MSG_FIGHT_INTERRUPTED_STAYS)
        assert notice.view is None                                   # no live "Zaatakuj" for a boss that is gone
    assert picture.view is None                                      # ...and none put back under the picture
    assert game.bot.boss_cycle._attack_buttons == []


async def test_click_right_after_another_claimed_the_legendary_boss_joins_the_lobby(registered, monkeypatch):
    """Ala and Ola click "Zaatakuj" at the same moment and Discord answers slowly: Ola joins the party."""
    game = registered
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    real_ack, real_retire = ui.ack, ui.retire

    async def slow_ack(interaction):
        await asyncio.sleep(0.2)                                     # a round trip to Discord
        return await real_ack(interaction)

    async def slow_retire(message, **kwargs):
        await asyncio.sleep(0.2)
        return await real_retire(message, **kwargs)

    monkeypatch.setattr(ui, "ack", slow_ack)
    monkeypatch.setattr(ui, "retire", slow_retire)
    picture = await show_boss(game, monkeypatch, rarity=3)
    encounter = game.click_later(picture, ala, "Zaatakuj")
    second = game.click_later(picture, ola, "Zaatakuj", 0.05)
    click = await asyncio.wait_for(second, 3)
    assert click.whispers == [] and click.response.deferred        # taken by the lobby door
    panel = await game.wait_for_buttons("join")
    await eventually(lambda: field(panel)[0] == "Drużyna (2/4)")
    assert field(panel)[1] == f"⚔️ <@{ala.id}>\n⚔️ <@{ola.id}>"
    encounter.cancel()
    await asyncio.gather(encounter, return_exceptions=True)
    await asyncio.sleep(0.3)                                         # the background notice of the cancelled fight


async def test_click_while_the_lobby_opens_is_not_sent_away(registered, fights, monkeypatch):
    """A click between the claim of the boss and the opening of its lobby: not "wait for the next one"."""
    game = registered
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    opening, spell = asyncio.Event(), asyncio.Event()

    async def single_init(scene, rarity):
        await opening.wait()                                         # the lobby takes a moment to open
        with boss._lobby_door(scene.bot, ui.ack):
            pass                                                     # its door was open; now the spell is typed
        await spell.wait()
        return scene.author

    monkeypatch.setattr(boss, "single_init", single_init)
    picture = await show_boss(game, monkeypatch)
    old = await boss_cycle.with_attack_button(lambda view: game.channel.send("boss", view=view))
    encounter = game.click_later(picture, ala, "Zaatakuj")
    await asyncio.sleep(0.05)
    assert game.bot.state.boss_phase is BossPhase.ENGAGED
    assert (await game.click(old, ola, "Zaatakuj")).whispers == [boss_cycle.MSG_LOBBY_OPENING]
    opening.set()
    await asyncio.sleep(0.05)
    assert (await game.click(old, ola, "Zaatakuj")).whispers == [boss_cycle.MSG_BOSS_TAKEN]
    spell.set()
    await asyncio.wait_for(encounter, 3)
    assert ("fight", "Ala", 0) in fights.calls


async def test_boss_that_leaves_takes_its_button_along(game, fights, monkeypatch):
    cycle = game.bot.boss_cycle
    picture = await show_boss(game, monkeypatch)
    assert game.bot.state.try_engage_boss()                          # claimed by the cycle: it leaves
    await cycle._summoned_boss_leaves()
    assert picture.view is None and game.bot.state.boss_phase is BossPhase.WAITING

    picture = await show_boss(game, monkeypatch)
    await cycle.finish(FightResult(won=False))                       # e.g. boss_visible_timeout ran out
    assert picture.view is None


# -- the single lobby --------------------------------------------------------------------

async def test_single_lobby_rival_objects_with_a_click(registered, monkeypatch):
    game = registered
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.05)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    dead = game.add_member("Jan", roles=[game.bot.ids.dead_hunter_role])
    game.channel.silenced_roles.add(game.bot.ids.dead_hunter_role)
    answers = {}

    def lobby_posted(message):
        if "Jesteś sam?" in message.content and message.view is not None:
            async def clicks():
                answers["attacker"] = await game.click(message, ala, "object")
                answers["dead"] = await game.click(message, dead, "object")
                answers["rival"] = await game.click(message, ola, "Zaatakuj")
            game._tasks.append(asyncio.ensure_future(clicks()))
        if "Zaklęcie to" in message.content:
            game.say_later(ola, requested_word(message.content), 0)

    game.channel.send_hooks.append(lobby_posted)
    assert await boss.single_init(game.scene(ala), 1) is ola
    lobby = game.channel.sent[0]
    assert answers["attacker"].whispers == [boss.MSG_ALREADY_FIGHTING]
    assert answers["dead"].whispers == [ui.MSG_CANNOT_PLAY]
    assert answers["rival"].response.deferred and answers["rival"].replies == []
    assert lobby.view is None and lobby.content == boss.MSG_SINGLE_LOBBY_RIVAL.format(user_id=ola.id)
    assert not ui.is_live(lobby.id) and game.bot.boss_cycle.lobby_door is None


async def test_single_lobby_takes_a_click_on_an_old_picture_button(registered, monkeypatch):
    game = registered
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.05)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    picture = await show_boss(game, monkeypatch)                    # its button was not taken off (purge...)
    assert game.bot.state.try_engage_boss()

    def lobby_posted(message):
        if "Jesteś sam?" in message.content and message.view is not None:
            game.click_later(picture, ola, "Zaatakuj")
        if "Zaklęcie to" in message.content:
            game.say_later(ola, requested_word(message.content), 0)

    game.channel.send_hooks.append(lobby_posted)
    assert await boss.single_init(game.scene(ala), 0) is ola


async def test_single_lobby_nobody_objects_and_the_button_goes(registered, short_timeouts):
    game = registered
    ala = game.add_member("Ala")
    assert await boss.single_init(game.scene(ala), 0) is ala
    lobby = game.channel.sent[0]
    assert lobby.view is None and lobby.content == boss.MSG_SINGLE_LOBBY_ALONE.format(user_id=ala.id)
    assert not ui.is_live(lobby.id) and game.bot.boss_cycle.lobby_door is None


# -- the legendary lobby panel -----------------------------------------------------------

def field(message) -> tuple[str, str]:
    embed_field = message.embeds[0].fields[0]
    return embed_field.name, embed_field.value


async def eventually(condition, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


async def test_legendary_lobby_by_buttons(registered, monkeypatch):
    game = registered
    monkeypatch.setattr(boss, "PANEL_EDIT_INTERVAL", 0.0)
    ala, ola, ela, ula = (game.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula"))
    outsider = game.add_member("Obcy")
    clicked = ui.scene_of(FakeInteraction(game.bot, ala, channel=game.channel))     # started by a click
    lobby = asyncio.ensure_future(boss.group_init(clicked, 3))

    panel = await game.wait_for_buttons("join")
    assert panel.button_labels == ["Dołącz"] and panel.content == ""
    assert field(panel) == ("Drużyna (1/4)", f"⚔️ <@{ala.id}>")
    assert "boss odejdzie <t:" in panel.embeds[0].description
    assert (await game.click(panel, ala, "join")).whispers == [boss.MSG_ALREADY_IN_PARTY]   # the leader
    joined = await game.click(panel, ola, "join")
    assert joined.response.deferred and joined.replies == []
    assert (await game.click(panel, ola, "join")).whispers == [boss.MSG_ALREADY_IN_PARTY]   # not twice
    await eventually(lambda: field(panel)[0] == "Drużyna (2/4)")
    assert field(panel)[1] == f"⚔️ <@{ala.id}>\n⚔️ <@{ola.id}>"
    assert "Nieźle, jest już Was 2." in panel.embeds[0].description
    await game.click(panel, ela, "join")
    game.say(ula, "$zaatakuj")                                       # typing still works

    ready = await game.wait_for_buttons("ready")
    assert ready is not panel and panel.deleted and panel.view is None
    mentions = f" <@{ala.id}> <@{ola.id}> <@{ela.id}> <@{ula.id}> "
    assert ready.content == "Jesteście gotowi do ataku?" + mentions  # posted anew: the party is pinged
    assert ready.button_labels == ["Gotowy"]
    assert "Czas na potwierdzenie mija <t:" in ready.embeds[0].description
    assert (await game.click(ready, outsider, "ready")).whispers == [boss.MSG_NOT_IN_PARTY]
    await game.click(ready, ola, "ready")
    assert (await game.click(ready, ola, "ready")).whispers == [boss.MSG_ALREADY_READY]
    await eventually(lambda: "✅ <@" in field(ready)[1])
    assert field(ready)[1] == f"⏳ <@{ala.id}>\n✅ <@{ola.id}>\n⏳ <@{ela.id}>\n⏳ <@{ula.id}>"
    await game.click(ready, ala, "ready")
    await game.click(ready, ela, "ready")
    game.say(ula, "$tak")
    assert await asyncio.wait_for(lobby, 3) == [ala, ola, ela, ula]

    assert ready.view is None and ready.content == "Do walki z legendarnym bossem stają:" + mentions
    assert field(ready) == ("Drużyna (4/4)", "\n".join(f"✅ <@{p.id}>" for p in (ala, ola, ela, ula)))
    assert not ui.is_live(ready.id) and game.bot.boss_cycle.lobby_door is None


async def test_legendary_lobby_refusals(registered, monkeypatch):
    game = registered
    bot = game.bot
    ala, ola, ela, ula, iza = (game.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula", "Iza"))
    dead = game.add_member("Jan", roles=[bot.ids.dead_hunter_role])
    game.channel.silenced_roles.add(bot.ids.dead_hunter_role)
    picture = await show_boss(game, monkeypatch, rarity=3)
    assert bot.state.try_engage_boss()
    lobby = asyncio.ensure_future(boss.group_init(game.scene(ala, "$zaatakuj"), 3))
    panel = await game.wait_for_buttons("join")

    assert (await game.click(panel, dead, "join")).whispers == [ui.MSG_CANNOT_PLAY]
    assert (await game.click(picture, dead, "Zaatakuj")).whispers == [ui.MSG_CANNOT_PLAY]
    door = await game.click(picture, ola, "Zaatakuj")               # an old "Zaatakuj": like "Dołącz"
    assert door.response.deferred and door.replies == []
    assert (await game.click(picture, ola, "Zaatakuj")).whispers == [boss.MSG_ALREADY_IN_PARTY]
    for member in (ela, ula):
        await game.click(panel, member, "join")
    ready = await game.wait_for_buttons("ready")
    assert (await game.click(picture, iza, "Zaatakuj")).whispers == [boss.MSG_PARTY_FULL]
    lobby.cancel()
    await asyncio.gather(lobby, return_exceptions=True)
    assert ready.view is None and bot.boss_cycle.lobby_door is None  # no buttons stay behind


async def test_legendary_lobby_edits_are_gathered(registered, monkeypatch):
    game = registered
    monkeypatch.setattr(boss, "PANEL_EDIT_INTERVAL", 0.3)
    ala, ola, ela = (game.add_member(name) for name in ("Ala", "Ola", "Ela"))
    lobby = asyncio.ensure_future(boss.group_init(game.scene(ala), 3))
    panel = await game.wait_for_buttons("join")
    await asyncio.gather(game.click(panel, ola, "join"), game.click(panel, ela, "join"))
    await asyncio.sleep(0.1)
    assert panel.edits == []                                         # not more than one edit per interval...
    await eventually(lambda: panel.edits)
    await asyncio.sleep(0.4)
    assert len(panel.edits) == 1 and field(panel)[0] == "Drużyna (3/4)"   # ...and both clicks in one edit
    lobby.cancel()
    await asyncio.gather(lobby, return_exceptions=True)


async def test_legendary_lobby_panel_deleted_midway_is_posted_again(registered, monkeypatch):
    game = registered
    monkeypatch.setattr(boss, "PANEL_EDIT_INTERVAL", 0.0)
    ala, ola, ela = (game.add_member(name) for name in ("Ala", "Ola", "Ela"))
    lobby = asyncio.ensure_future(boss.group_init(game.scene(ala), 3))
    panel = await game.wait_for_buttons("join")
    await panel.delete()                                             # e.g. a moderator cleaned up

    async def gone(**kwargs):
        raise not_found()

    panel.edit = gone
    game.say(ola, "$zaatakuj")
    again = await game.wait_for_buttons("join")
    assert again is not panel and field(again) == ("Drużyna (2/4)", f"⚔️ <@{ala.id}>\n⚔️ <@{ola.id}>")
    await game.click(again, ela, "join")
    await eventually(lambda: field(again)[0] == "Drużyna (3/4)")
    lobby.cancel()
    await asyncio.gather(lobby, return_exceptions=True)
    assert again.view is None


async def test_legendary_lobby_panel_put_back_after_a_deletion_pings_nobody_again(registered, monkeypatch):
    game = registered
    monkeypatch.setattr(boss, "PANEL_EDIT_INTERVAL", 0.0)
    ala, ola, ela, ula = (game.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula"))
    sent_with: list = []
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        message = await real_send(content, **kwargs)
        sent_with.append((message, kwargs.get("allowed_mentions")))
        return message

    monkeypatch.setattr(game.channel, "send", send)
    lobby = asyncio.ensure_future(boss.group_init(game.scene(ala), 3))
    panel = await game.wait_for_buttons("join")
    for member in (ola, ela, ula):
        await game.click(panel, member, "join")
    ready = await game.wait_for_buttons("ready")
    assert ready.content.startswith("Jesteście gotowi do ataku?")
    assert next(mentions for message, mentions in sent_with if message is ready) is None   # the party is pinged
    await ready.delete()                                             # e.g. a moderator cleaned up

    async def gone(**kwargs):
        raise not_found()

    ready.edit = gone
    game.say(ola, "$tak")
    again = await game.wait_for_buttons("ready")
    assert again is not ready and again.content == ready.content     # the same text...
    mentions = next(mentions for message, mentions in sent_with if message is again)
    assert mentions is not None                                      # ...but nobody is notified again
    assert not mentions.everyone and not mentions.users and not mentions.roles
    lobby.cancel()
    await asyncio.gather(lobby, return_exceptions=True)


async def test_legendary_lobby_through_the_cycle(registered, monkeypatch):
    """A click on the legendary boss's picture opens the lobby; the others join on the panel."""
    game = registered
    bot = game.bot
    party = [game.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    fought = []

    async def group_fight(scene, players, is_player_boss, player_boss):
        fought.append(list(players))
        return FightResult(won=True)

    async def reset_end(bot_):
        return None

    monkeypatch.setattr(boss, "group_fight", group_fight)
    monkeypatch.setattr(boss, "reset_end", reset_end)
    picture = await show_boss(game, monkeypatch, rarity=3)
    encounter = game.click_later(picture, party[0], "Zaatakuj")
    panel = await game.wait_for_buttons("join")
    await eventually(lambda: picture.view is None)                   # taken off while the lobby opens
    for member in party[1:]:
        await game.click(panel, member, "join")
    ready = await game.wait_for_buttons("ready")
    for member in party:
        await game.click(ready, member, "ready")
    click = await asyncio.wait_for(encounter, 3)
    assert click.response.deferred and fought == [party]
    assert bot.state.boss_phase is BossPhase.IDLE
