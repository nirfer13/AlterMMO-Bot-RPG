"""altermmo.core.ui: flow buttons, confirmations, menus, pop-up windows, stale clicks."""

from __future__ import annotations

import asyncio
import time

import discord

from altermmo.core import ui

from fakes import FakeInteraction


def join_view(**kwargs) -> ui.ClickView:
    return ui.ClickView([ui.Choice("join", "Dołącz", emoji="⚔️", style=discord.ButtonStyle.primary)], **kwargs)


# -- ClickView -----------------------------------------------------------------------

async def test_click_is_queued_acknowledged_and_buttons_go_away(game):
    ala = game.add_member("Ala")
    async with join_view() as view:
        message = await view.send(game.channel, "Mroczny las!")
        assert message.button_labels == ["Dołącz"] and ui.is_live(message.id)
        interaction = await game.click(message, ala, "join")
        click = await view.next(1)
        assert click.key == "join" and click.member is ala and click.interaction is interaction
        assert interaction.response.deferred and interaction.replies == []
        assert await view.next(0) is None and await view.next(0.01) is None
    assert message.view is None and not ui.is_live(message.id)            # taken off when the flow ends


async def test_only_listed_members_and_only_players_may_click(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    dead = game.add_member("Jan", roles=[game.bot.ids.dead_hunter_role])
    game.channel.silenced_roles.add(game.bot.ids.dead_hunter_role)
    async with join_view(only=[ala]) as view:
        message = await view.send(game.channel, "Pytanie")
        refused = await game.click(message, ola)
        assert refused.whispers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
        assert view.pending() == []
    async with join_view() as view:
        message = await view.send(game.channel, "Event")
        refused = await game.click(message, dead)
        assert refused.whispers == [ui.MSG_CANNOT_PLAY] and view.pending() == []
        assert not ui.may_play(game.channel, dead) and ui.may_play(game.channel, ala)
        await game.click(message, ala)
        assert [click.member for click in view.pending()] == [ala]


async def test_allow_callback_refuses_with_a_private_text_or_drops(game):
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola"), game.add_member("Ela")

    async def allow(interaction, key):
        if interaction.user is ola:
            return "Masz za mało zwojów."
        if interaction.user is ela:
            await ui.reply(interaction, "Sam odpowiedziałem.")
            return ui.DROP
        return None

    async with join_view(allow=allow) as view:
        message = await view.send(game.channel, "Hazard")
        assert (await game.click(message, ola)).whispers == ["Masz za mało zwojów."]
        assert (await game.click(message, ela)).whispers == ["Sam odpowiedziałem."]
        await game.click(message, ala)
        assert [click.member for click in view.pending()] == [ala]


async def test_relabel_refresh_and_keep_disabled_buttons(game):
    ala = game.add_member("Ala")
    view = join_view()
    message = await view.send(game.channel, "Inwazja")
    view.relabel("join", "Dołącz (1)")
    assert await view.refresh(content="Inwazja - 1 obrońca")
    assert message.button_labels == ["Dołącz (1)"] and message.content == "Inwazja - 1 obrońca"
    await view.close(keep_buttons=True, content="Koniec zapisów")
    assert message.content == "Koniec zapisów" and message.button_labels == []          # visible but disabled
    assert message.view.children[0].disabled and not ui.is_live(message.id)
    stale = await game.click(message, ala)                                                # nobody listens
    assert stale.whispers == [ui.MSG_STALE] and message.view is None
    await view.close()                                                                    # idempotent


async def test_a_switched_off_button_no_longer_counts(game):
    """A second quick click on a button the flow has just switched off never reaches the flow."""
    ala = game.add_member("Ala")
    choices = [ui.Choice("a", "A"), ui.Choice("b", "B")]
    async with ui.ClickView(choices) as view:
        message = await view.send(game.channel, "Runy")
        first, second = await asyncio.gather(game.click(message, ala, "a"), game.click(message, ala, "a"))
        assert (await view.next(0)).key == "a"
        view.relabel("a", disabled=True)                     # the flow accepted "a"; the edit is on its way
        await game.click(message, ala, "b")
        assert (await view.next(0)).key == "b"               # the queued second "a" is skipped
        late = await game.click(message, ala, "a")           # pressed before the edit was shown
        assert late.response.deferred and late.replies == [] and await view.next(0) is None
        view.relabel("a", disabled=False)
        await game.click(message, ala, "a")
        assert (await view.next(0)).key == "a"
    assert first.response.deferred and second.response.deferred


async def test_click_accepted_while_the_flow_closes_is_told_so(game, monkeypatch):
    ala = game.add_member("Ala")
    view = join_view()
    message = await view.send(game.channel, "Event")
    real_ack = ui.ack

    async def slow_ack(interaction):
        done = await real_ack(interaction)
        await view.close()                                   # the deadline passes during the acknowledgement
        return done

    monkeypatch.setattr(ui, "ack", slow_ack)
    interaction = await game.click(message, ala)
    assert interaction.whispers == [ui.MSG_STALE] and view.pending() == []


async def test_stale_click_is_answered_once_by_view_and_listener(game):
    from altermmo.cogs.ui import UiCog

    ala = game.add_member("Ala")
    view = join_view()
    message = await view.send(game.channel, "Event")
    await view.close(keep_buttons=True)
    interaction = await game.click(message, ala)             # the view's side
    await UiCog(game.bot).on_interaction(interaction)        # ...and the listener sees the same click
    assert interaction.whispers == [ui.MSG_STALE]


async def test_close_survives_a_purged_message(game):
    view = join_view()
    message = await view.send(game.channel, "Event")
    await message.delete()

    async def gone(**kwargs):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": "Not Found"})(), "unknown message")

    message.edit = gone
    await view.close()
    assert view.closed and not ui.is_live(message.id)


async def test_failing_click_handler_is_answered_and_reported(game):
    ala = game.add_member("Ala")

    async def allow(interaction, key):
        raise RuntimeError("database on fire")

    async with join_view(allow=allow) as view:
        message = await view.send(game.channel, "Event")
        interaction = await game.click(message, ala)
    assert interaction.whispers == [ui.MSG_FAILED]
    assert any("database on fire" in line for line in game.bot.owner_log)


async def test_answers_never_raise_when_discord_is_down(game):
    ala = game.add_member("Ala")
    interaction = FakeInteraction(game.bot, ala, channel=game.channel)
    interaction.response.fail_with = ConnectionError("down")
    assert await ui.reply(interaction, "x") is False
    assert await ui.ack(interaction) is False
    assert await ui.update(interaction, content="x") is False
    assert await ui.retire(None) is False


# -- gate ----------------------------------------------------------------------------

async def test_gate_mirrors_the_command_guards(game):
    bot = game.bot
    ala = game.add_member("Ala")
    dead = game.add_member("Jan", roles=[bot.ids.dead_hunter_role])
    game.channel.silenced_roles.add(bot.ids.dead_hunter_role)

    ok = FakeInteraction(bot, ala, channel=game.channel)
    assert await ui.gate(ok) is True and ok.replies == []

    elsewhere = FakeInteraction(bot, ala, channel=game.notice_channel)
    assert await ui.gate(elsewhere) is False
    assert elsewhere.whispers == [ui.MSG_WRONG_CHANNEL.format(channel_id=bot.ids.boss_channel)]
    assert await ui.gate(FakeInteraction(bot, ala, channel=game.notice_channel), boss_channel=False) is True

    refused = FakeInteraction(bot, dead, channel=game.channel)
    assert await ui.gate(refused) is False and refused.whispers == [ui.MSG_CANNOT_PLAY]

    with bot.state.hold_busy("hunt"):
        busy = FakeInteraction(bot, ala, channel=game.channel)
        assert await ui.gate(busy) is False and busy.whispers == [ui.MSG_BUSY]
        assert await ui.gate(FakeInteraction(bot, ala, channel=game.channel), unless_busy=False) is True


async def test_scene_of_a_click_and_whispering(game):
    ala = game.add_member("Ala")
    interaction = FakeInteraction(game.bot, ala, channel=game.channel)
    scene = ui.scene_of(interaction)
    assert scene.bot is game.bot and scene.channel is game.channel and scene.author is ala
    assert scene.message is None

    whisper = ui.whisper_scene(interaction)
    assert await whisper.send("Tylko dla Ciebie", embed=discord.Embed(title="Towarzysz")) is None
    await whisper.send("I jeszcze to")                                   # the second one is a follow-up
    assert interaction.whispers == ["Tylko dla Ciebie\nTowarzysz", "I jeszcze to"]
    assert game.channel.bot_messages == [game.channel.messages[0]]       # nothing public


def test_when_renders_discord_timestamps():
    from datetime import datetime, timezone
    assert ui.when(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "<t:1767225600:R>"
    assert ui.when(datetime(2026, 1, 1, tzinfo=timezone.utc), "f") == "<t:1767225600:f>"
    stamp = int(ui.when(3600)[3:-3])
    assert abs(stamp - (time.time() + 3600)) < 5


# -- confirm -------------------------------------------------------------------------

async def test_confirm_by_button(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    task = asyncio.ensure_future(ui.confirm(game.bot, game.channel, ala, "Na pewno?", timeout=2))
    question = await game.wait_for_buttons("yes")
    assert question.button_labels == ["Potwierdzam", "Anuluj"]
    assert (await game.click(question, ola, "yes")).whispers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    interaction = await game.click(question, ala, "yes")
    answer = await task
    assert answer and answer.confirmed and not answer.timed_out
    assert answer.interaction is interaction and answer.message is question and question.view is None


async def test_confirm_refused_typed_and_timed_out(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    task = asyncio.ensure_future(ui.confirm(game.bot, game.channel, ala, "Na pewno?", timeout=2))
    await game.click(await game.wait_for_buttons("no"), ala, "no")
    answer = await task
    assert not answer and not answer.timed_out

    task = asyncio.ensure_future(ui.confirm(game.bot, game.channel, ala, "Na pewno?", timeout=2))
    question = await game.wait_for_buttons("yes")
    game.say(ola, "$potwierdzam")                                        # somebody else typing it is ignored
    game.say(ala, " $Potwierdzam ")
    answer = await task
    assert answer and answer.typed.author is ala and answer.interaction is None and question.view is None

    answer = await ui.confirm(game.bot, game.channel, ala, "Na pewno?", timeout=0.05)
    assert not answer and answer.timed_out and answer.message.view is None

    task = asyncio.ensure_future(ui.confirm(game.bot, game.channel, ala, "Na pewno?", timeout=0.1, typed=None))
    await game.wait_for_buttons("yes")
    game.say(ala, "$potwierdzam")                                        # typing is switched off here
    assert (await task).timed_out


# -- pick ----------------------------------------------------------------------------

OPTIONS = [ui.Option("1", "Burek", "poziom 3"), ui.Option("2", "Mruczek", emoji="🐈")]


async def test_pick_from_a_menu(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    task = asyncio.ensure_future(ui.pick(game.channel, ala, "Wybierz towarzysza", options=OPTIONS,
                                         timeout=2, placeholder="Towarzysz..."))
    menu = await game.wait_for_buttons("pick")
    select = menu.button("pick")
    assert [option.label for option in select.options] == ["Burek", "Mruczek"] and ui.is_live(menu.id)
    refused = await game.click(menu, ola, "pick", values=["2"])
    assert refused.whispers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    interaction = await game.click(menu, ala, "pick", values=["2"])
    choice = await task
    assert choice and choice.value == "2" and choice.interaction is interaction
    assert menu.view is None and not ui.is_live(menu.id)


async def test_pick_cancelled_and_timed_out(game):
    ala = game.add_member("Ala")
    task = asyncio.ensure_future(ui.pick(game.channel, ala, "Wybierz", options=OPTIONS, timeout=2))
    await game.click(await game.wait_for_buttons("pick-cancel"), ala, "pick-cancel")
    choice = await task
    assert not choice and choice.value is None and not choice.timed_out

    choice = await ui.pick(game.channel, ala, "Wybierz", options=OPTIONS, timeout=0.05, cancel_label=None)
    assert not choice and choice.timed_out and choice.message.view is None


# -- pop-up window -------------------------------------------------------------------

async def test_ask_text_in_a_pop_up_window(game):
    ala = game.add_member("Ala")
    click = FakeInteraction(game.bot, ala, channel=game.channel)
    task = asyncio.ensure_future(ui.ask_text(click, "Nowe imię", "Imię towarzysza", default="Burek", max_length=20))
    await asyncio.sleep(0)
    assert click.response.modal.title == "Nowe imię"
    submitted = await game.submit_modal(click, "  Sir Puszek  ")
    modal = await task
    assert modal.value == "Sir Puszek" and modal.interaction is submitted
    assert await ui.reply(modal.interaction, "Zapisane") and submitted.whispers == ["Zapisane"]


async def test_ask_text_that_cannot_be_opened(game):
    ala = game.add_member("Ala")
    click = FakeInteraction(game.bot, ala, channel=game.channel)
    click.response.fail_with = ConnectionError("down")
    assert await ui.ask_text(click, "Nowe imię", "Imię") is None


# -- a player's own panel ------------------------------------------------------------

class PetPanel(ui.PanelView):
    def __init__(self, owner, **kwargs) -> None:
        super().__init__(owner=owner, **kwargs)
        self.renamed = 0

    @discord.ui.button(label="Zmień imię", custom_id=ui.FLOW_PREFIX + "pet:rename")
    async def rename(self, interaction, button) -> None:
        self.renamed += 1
        await ui.reply(interaction, "Nowe imię?")

    @discord.ui.button(label="Zamknij", custom_id=ui.FLOW_PREFIX + "pet:close")
    async def shut(self, interaction, button) -> None:
        await ui.ack(interaction)
        await self.finish(content="Zamknięte")


async def test_panel_view_serves_its_owner_until_it_is_finished(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    panel = PetPanel(ala)
    message = await panel.send(game.channel, "Towarzysz Ali")
    assert ui.is_live(message.id) and message.button_labels == ["Zmień imię", "Zamknij"]
    assert (await game.click(message, ola, "pet:rename")).whispers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    assert (await game.click(message, ala, "pet:rename")).whispers == ["Nowe imię?"] and panel.renamed == 1
    await game.click(message, ala, "pet:close")
    assert message.view is None and message.content == "Zamknięte" and not ui.is_live(message.id)


async def test_panel_view_times_out_and_takes_its_buttons_away(game):
    ala = game.add_member("Ala")
    panel = PetPanel(ala, timeout=0.05)
    message = await panel.send(game.channel, "Towarzysz Ali")
    await asyncio.sleep(0.2)
    assert message.view is None and not ui.is_live(message.id) and panel.is_finished()


# -- stale buttons and permanent views -----------------------------------------------

async def test_stale_listener_ignores_live_and_foreign_buttons(game):
    from altermmo.cogs.ui import UiCog

    cog = UiCog(game.bot)
    ala = game.add_member("Ala")
    async with join_view() as view:
        message = await view.send(game.channel, "Event")
        live = FakeInteraction(game.bot, ala, message, data={"custom_id": ui.FLOW_PREFIX + "join"})
        await cog.on_interaction(live)
        assert live.replies == [] and not live.response.is_done()         # the running flow answers it
    dead = FakeInteraction(game.bot, ala, message, data={"custom_id": ui.FLOW_PREFIX + "join"})
    await cog.on_interaction(dead)
    assert dead.whispers == [ui.MSG_STALE]
    permanent = FakeInteraction(game.bot, ala, message, data={"custom_id": ui.PREFIX + "boss:attack"})
    await cog.on_interaction(permanent)
    assert permanent.replies == []
    command = FakeInteraction(game.bot, ala, message, data={"custom_id": ui.FLOW_PREFIX + "join"},
                              kind=discord.InteractionType.application_command)
    await cog.on_interaction(command)
    assert command.replies == []


async def test_permanent_view_answers_clicks_on_old_messages(game):
    ala = game.add_member("Ala")
    seen = []

    class Panel(ui.GameView):
        def __init__(self) -> None:
            super().__init__(timeout=None)

        @discord.ui.button(label="Polowanie", custom_id=ui.PREFIX + "test:hunt")
        async def hunt(self, interaction, button) -> None:
            seen.append(interaction.user)
            await ui.reply(interaction, "Ruszasz na polowanie")

    assert ui.register(game.bot, Panel())
    old_view = Panel()
    message = await game.channel.send("Centrum gry", view=old_view)
    old_view.stop()                                                      # e.g. posted before a restart
    interaction = await game.click(message, ala, "test:hunt")
    assert seen == [ala] and interaction.whispers == ["Ruszasz na polowanie"]
