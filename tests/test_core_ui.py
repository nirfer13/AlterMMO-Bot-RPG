"""altermmo.core.ui: flow buttons, confirmations, menus, pop-up windows, stale clicks."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from types import SimpleNamespace

import discord
import pytest

from altermmo.core import challenge, guards, ui

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
        # Public, addressed to the clicker (Ola), naming the player the button is for (Ala).
        assert refused.public == [f"<@{ola.id}> Ten przycisk jest dla <@{ala.id}>."]
        assert refused.whispers == [] and game.channel.messages[-1] is refused.replies[0].message
        assert view.pending() == []
    async with join_view() as view:
        message = await view.send(game.channel, "Event")
        refused = await game.click(message, dead)
        assert refused.answers == [ui.MSG_CANNOT_PLAY] and view.pending() == []
        assert game.channel.contains(f"<@{dead.id}> {ui.MSG_CANNOT_PLAY}")
        assert not ui.may_play(game.channel, dead) and ui.may_play(game.channel, ala)
        await game.click(message, ala)
        assert [click.member for click in view.pending()] == [ala]


async def test_allow_callback_refuses_with_a_text_or_drops(game):
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
        assert (await game.click(message, ola)).answers == ["Masz za mało zwojów."]
        assert (await game.click(message, ela)).answers == ["Sam odpowiedziałem."]   # answered once
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
    assert stale.public == [f"<@{ala.id}> {ui.MSG_STALE}"] and message.view is None
    assert game.channel.messages[-1].content == f"<@{ala.id}> {ui.MSG_STALE}"
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
    assert interaction.answers == [ui.MSG_STALE] and view.pending() == []


async def test_stale_click_is_answered_once_by_view_and_listener(game):
    from altermmo.cogs.ui import UiCog

    ala = game.add_member("Ala")
    view = join_view()
    message = await view.send(game.channel, "Event")
    await view.close(keep_buttons=True)
    interaction = await game.click(message, ala)             # the view's side
    await UiCog(game.bot).on_interaction(interaction)        # ...and the listener sees the same click
    assert interaction.answers == [ui.MSG_STALE] and game.channel.count(ui.MSG_STALE) == 1


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
    assert interaction.answers == [ui.MSG_FAILED]
    assert any("database on fire" in line for line in game.bot.owner_log)


async def test_answers_never_raise_when_discord_is_down(game):
    ala = game.add_member("Ala")
    interaction = FakeInteraction(game.bot, ala, channel=game.channel)
    interaction.response.fail_with = ConnectionError("down")
    assert await ui.reply(interaction, "x") is False
    assert await ui.ack(interaction) is False
    assert await ui.update(interaction, content="x") is False
    assert await ui.retire(None) is False


# -- reply: public answers -------------------------------------------------------------

async def test_reply_is_public_and_names_the_clicker(game):
    ala = game.add_member("Ala")

    def click() -> FakeInteraction:
        return FakeInteraction(game.bot, ala, channel=game.channel)

    plain = click()
    assert await ui.reply(plain, "Gotowe")
    assert plain.public == [f"<@{ala.id}> Gotowe"] and plain.whispers == [] and plain.answers == ["Gotowe"]
    assert game.channel.messages[-1].content == f"<@{ala.id}> Gotowe"

    later = click()                                              # acknowledged first: a follow-up
    await ui.ack(later)
    assert await ui.reply(later, "Po chwili") and later.public == [f"<@{ala.id}> Po chwili"]

    for named in (f"<@{ala.id}> zdobywa łup", f"Brawo <@!{ala.id}>!"):   # already names the clicker
        answer = click()
        await ui.reply(answer, named)
        assert answer.public == [named]

    embed_only = click()
    await ui.reply(embed_only, embed=discord.Embed(title="Ranking"))
    posted = embed_only.replies[0].message
    assert posted is game.channel.messages[-1] and posted.embeds[0].title == "Ranking"
    assert not posted.content                                    # no lone mention above an embed

    direct = click()
    direct.guild = None                                          # a private message: one reader, no name
    await ui.reply(direct, "Gotowe")
    assert direct.public == ["Gotowe"]


async def test_private_answers_setting_and_explicit_override(game):
    ala = game.add_member("Ala")

    def click() -> FakeInteraction:
        return FakeInteraction(game.bot, ala, channel=game.channel)

    secret = click()
    assert not ui.answers_are_private(secret)
    await ui.reply(secret, "Sekret", ephemeral=True)             # explicit: only the clicker, no name
    assert secret.whispers == ["Sekret"] and secret.public == [] and not game.channel.contains("Sekret")

    game.bot.settings = dataclasses.replace(game.bot.settings, private_answers=True)
    private = click()
    assert ui.answers_are_private(private)
    await ui.reply(private, "Tylko dla Ciebie")
    assert private.whispers == ["Tylko dla Ciebie"] and not game.channel.contains("Tylko dla Ciebie")
    shown = click()
    await ui.reply(shown, "Dla wszystkich", ephemeral=False)
    assert shown.public == [f"<@{ala.id}> Dla wszystkich"] and game.channel.contains("Dla wszystkich")


async def test_a_public_answer_pings_only_the_clicker(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    refused = FakeInteraction(game.bot, ola, channel=game.channel)
    await ui.reply(refused, ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id))
    assert refused.public == [f"<@{ola.id}> Ten przycisk jest dla <@{ala.id}>."]
    mentions = refused.replies[0].allowed_mentions                 # Ala is named, but not pinged
    assert mentions.users == [ola] and mentions.everyone is False and mentions.roles is False

    own = discord.AllowedMentions(users=[ala])                     # the caller's own choice wins
    call = FakeInteraction(game.bot, ola, channel=game.channel)
    await ui.reply(call, f"<@{ala.id}>, pomóż!", allowed_mentions=own)
    assert call.replies[0].allowed_mentions is own and call.public == [f"<@{ola.id}> <@{ala.id}>, pomóż!"]


def _click(game, member, channel=None) -> FakeInteraction:
    return FakeInteraction(game.bot, member, channel=channel or game.channel)


def _swallowed(interaction: FakeInteraction) -> bool:
    """The click was only acknowledged: nothing was posted."""
    return interaction.replies == [] and interaction.response.deferred


async def test_the_busy_notice_is_shared_with_the_typed_commands(game, monkeypatch):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    clock = [1000.0]
    monkeypatch.setattr(guards, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    typed = SimpleNamespace(channel=game.channel)                  # what notify_busy reads of a ctx
    assert ui.MSG_BUSY == guards.BUSY_NOTICE

    clicked = _click(game, ala)
    assert await ui.reply(clicked, ui.MSG_BUSY) and clicked.public == [f"<@{ala.id}> {ui.MSG_BUSY}"]
    clock[0] += 5
    await guards.notify_busy(typed)                                # a click blocks the typed notice...
    second = _click(game, ola)
    assert await ui.reply(second, ui.MSG_BUSY) and _swallowed(second)   # ...and another click
    assert game.channel.count(ui.MSG_BUSY) == 1
    elsewhere = _click(game, ola, game.notice_channel)             # another channel is independent
    await ui.reply(elsewhere, ui.MSG_BUSY)
    assert elsewhere.answers == [ui.MSG_BUSY] and game.notice_channel.count(ui.MSG_BUSY) == 1

    clock[0] += 20                                                 # 25 s after the first one
    await guards.notify_busy(typed)                                # the typed notice is due again...
    assert game.channel.count(ui.MSG_BUSY) == 2 and game.channel.messages[-1].content == guards.BUSY_NOTICE
    after_typed = _click(game, ala)
    assert await ui.reply(after_typed, ui.MSG_BUSY) and _swallowed(after_typed)   # ...and blocks a click
    assert game.channel.count(ui.MSG_BUSY) == 2


async def _start_round(game, player) -> asyncio.Future:
    """A typed fight round on the boss channel, waiting for ``player`` (answer with ``game.say``)."""
    task = asyncio.ensure_future(challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                               players=[player], timeout=3))
    while not game.channel.contains("B L O K"):
        await asyncio.sleep(0)
    assert challenge.fight_on(game.channel)
    return task


async def test_no_busy_notice_and_no_new_command_during_a_typed_fight(game):
    """(Items, 2026-11-26) owner's rule: nothing posts into a running fight - the typed commands' busy notice
    neither (it is not even charged to the rate limit), and ``guards.not_during_fight`` says nothing at all."""
    ala = game.add_member("Ala")
    typed = SimpleNamespace(channel=game.channel)
    ctx = SimpleNamespace(channel=game.channel)
    predicate = _predicate_of(guards.not_during_fight())
    round_task = await _start_round(game, ala)
    await guards.notify_busy(typed)
    with pytest.raises(guards.SilentCheckFailure):
        await predicate(ctx)
    assert await predicate(SimpleNamespace(channel=game.notice_channel)) is True   # another channel: free
    game.say(ala, "blok")
    await round_task
    assert not game.channel.contains(guards.BUSY_NOTICE)
    assert await predicate(ctx) is True                                      # the fight is over
    await guards.notify_busy(typed)                                          # ... and the notice was not used up
    assert game.channel.messages[-1].content == guards.BUSY_NOTICE


async def test_guards_in_order_run_in_the_order_given_however_often_the_command_is_copied():
    """discord.py reverses a command's stacked checks in place on every copy (a cog instance copies its
    commands): ``guards.in_order`` is one check, so its guards run in the written order whatever happens."""
    from discord.ext import commands

    seen: list[str] = []

    def guard(name: str, answer: bool = True):
        async def predicate(ctx):
            seen.append(name)
            if name == "refuses":
                raise guards.SilentCheckFailure(name)
            return answer
        return commands.check(predicate)

    @commands.command(name="probe")
    @guard("first")
    @guards.in_order(guard("one"), guard("two"), guard("refuses"), guard("never"))
    async def probe(ctx):
        pass

    for copy in (probe, probe.copy(), probe.copy().copy()):
        seen.clear()
        with pytest.raises(guards.SilentCheckFailure):
            for check in copy.checks:
                await check(SimpleNamespace())
        assert seen[seen.index("one"):] == ["one", "two", "refuses"]
    seen.clear()
    assert await _predicate_of(guards.in_order(guard("one"), guard("no", False), guard("never")))(None) is False
    assert seen == ["one", "no"]


def _predicate_of(decorator):
    """The predicate a ``commands.check`` decorator adds to a command."""
    async def command():
        pass

    decorator(command)
    return command.__commands_checks__[-1]


async def test_during_a_typed_fight_only_real_content_is_posted(game, tmp_path):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    picture = tmp_path / "mapa.png"
    picture.write_bytes(b"\x89PNG")
    fight = await _start_round(game, ala)
    on_channel = len(game.channel.messages)

    for text in ("Masz za mało zwojów.", ui.MSG_STALE, ui.MSG_CANNOT_PLAY, ui.MSG_FAILED):
        quiet = _click(game, ola)
        assert await ui.reply(quiet, text) and _swallowed(quiet)
    assert len(game.channel.messages) == on_channel                # nothing between the prompts

    busy = _click(game, ola)
    assert await ui.reply(busy, ui.MSG_BUSY) and _swallowed(busy)  # not even the busy notice
    assert len(game.channel.messages) == on_channel

    shown = [_click(game, ola) for _ in range(3)]                  # real content is not a notice
    await ui.reply(shown[0], embed=discord.Embed(title="Ranking"))
    await ui.reply(shown[1], "Panel", view=discord.ui.View(timeout=None))
    await ui.reply(shown[2], "Mapa", file_path=picture)
    assert all(interaction.replies[0].message in game.channel.messages for interaction in shown)
    private = _click(game, ola)
    await ui.reply(private, "Tylko dla Ciebie", ephemeral=True)   # private answers are never swallowed
    assert private.whispers == ["Tylko dla Ciebie"]

    game.say(ala, "blok")
    assert (await fight).is_correct("blok", by=ala)
    assert not challenge.fight_on(game.channel)                    # (FIGHT_LINGER is 0 in the tests)
    after = _click(game, ola)
    await ui.reply(after, "Masz za mało zwojów.")
    assert after.answers == ["Masz za mało zwojów."]
    late = _click(game, ola)                                       # the swallowed busy notice used up
    await ui.reply(late, ui.MSG_BUSY)                              # nothing of the 20 s limit
    assert late.answers == [ui.MSG_BUSY]


async def test_a_stale_click_during_a_fight_loses_its_buttons_quietly(game):
    ala = game.add_member("Ala")
    view = join_view()
    message = await view.send(game.channel, "Zapisy")
    await view.close(keep_buttons=True)
    fight = await _start_round(game, ala)
    stale = await game.click(message, ala)
    assert message.view is None and stale.response.is_done() and stale.replies == []
    assert not game.channel.ever_sent(ui.MSG_STALE)
    game.say(ala, "blok")
    await fight


async def test_a_repeated_notice_is_swallowed_per_player_or_per_channel(game, monkeypatch):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    clock = [1000.0]
    monkeypatch.setattr(ui, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(ui, "NOTICE_GAP", 10.0)
    poor = "Masz za mało zwojów."

    async def answer(member, content, channel=None) -> FakeInteraction:
        interaction = _click(game, member, channel)
        assert await ui.reply(interaction, content)
        return interaction

    assert (await answer(ala, poor)).public == [f"<@{ala.id}> {poor}"]
    clock[0] += 5
    assert _swallowed(await answer(ala, poor))                     # still on the screen
    assert (await answer(ola, poor)).answers == [poor]             # per player: Ola is told
    assert (await answer(ala, poor, game.notice_channel)).answers == [poor]   # another channel
    assert (await answer(ala, "Inny tekst")).answers == ["Inny tekst"]
    assert game.channel.count(poor) == 2

    assert (await answer(ala, ui.MSG_STALE)).answers == [ui.MSG_STALE]
    assert _swallowed(await answer(ola, ui.MSG_STALE))             # a general notice covers everybody
    assert game.channel.count(ui.MSG_STALE) == 1

    clock[0] += 5                                                  # 10 s after Ala's first notice
    assert (await answer(ala, poor)).answers == [poor] and game.channel.count(poor) == 3
    assert _swallowed(await answer(ala, ui.MSG_STALE))             # only 5 s after the general one


async def test_a_notice_that_could_not_be_sent_does_not_silence_the_next_one(game, monkeypatch):
    ala = game.add_member("Ala")
    monkeypatch.setattr(ui, "time", SimpleNamespace(monotonic=lambda: 1000.0, time=time.time))
    monkeypatch.setattr(ui, "NOTICE_GAP", 10.0)
    for text in ("Masz za mało zwojów.", ui.MSG_STALE):
        failed = _click(game, ala)
        failed.response.fail_with = ConnectionError("down")
        assert await ui.reply(failed, text) is False
        retried = _click(game, ala)
        assert await ui.reply(retried, text) and retried.public == [f"<@{ala.id}> {text}"]


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
    wrong_channel = ui.MSG_WRONG_CHANNEL.format(channel_id=bot.ids.boss_channel)
    assert elsewhere.answers == [wrong_channel]
    assert game.notice_channel.contains(f"<@{ala.id}> {wrong_channel}")         # where it was clicked
    assert await ui.gate(FakeInteraction(bot, ala, channel=game.notice_channel), boss_channel=False) is True

    refused = FakeInteraction(bot, dead, channel=game.channel)
    assert await ui.gate(refused) is False and refused.answers == [ui.MSG_CANNOT_PLAY]

    with bot.state.hold_busy("hunt"):
        busy = FakeInteraction(bot, ala, channel=game.channel)
        assert await ui.gate(busy) is False and busy.answers == [ui.MSG_BUSY]
        assert await ui.gate(FakeInteraction(bot, ala, channel=game.channel), unless_busy=False) is True


async def test_scene_of_a_click_writes_on_the_channel(game):
    """A panel shown from a button is an ordinary channel message, like the typed command's."""
    ala = game.add_member("Ala")
    interaction = FakeInteraction(game.bot, ala, channel=game.channel)
    scene = ui.scene_of(interaction)
    assert scene.bot is game.bot and scene.channel is game.channel and scene.author is ala
    assert scene.message is None
    assert not hasattr(ui, "whisper_scene") and not hasattr(ui, "WhisperScene")   # no private scenes

    posted = await scene.send("Towarzysz Ali", embed=discord.Embed(title="Towarzysz"))
    assert posted is game.channel.messages[-1] and posted.text == "Towarzysz Ali\nTowarzysz"
    assert interaction.replies == []                    # the scene does not answer the click itself


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
    refused = await game.click(question, ola, "yes")
    assert refused.public == [f"<@{ola.id}> {ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)}"]
    assert game.channel.messages[-1] is refused.replies[0].message and not task.done()
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
    game.say(ola, "!potwierdzam")                                        # somebody else typing it is ignored
    game.say(ala, " $Potwierdzam ")
    answer = await task
    assert answer and answer.typed.author is ala and answer.interaction is None and question.view is None

    answer = await ui.confirm(game.bot, game.channel, ala, "Na pewno?", timeout=0.05)
    assert not answer and answer.timed_out and answer.message.view is None

    task = asyncio.ensure_future(ui.confirm(game.bot, game.channel, ala, "Na pewno?", timeout=0.1, typed=None))
    await game.wait_for_buttons("yes")
    game.say(ala, "!potwierdzam")                                        # typing is switched off here
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
    assert refused.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)] and not refused.whispers
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
    assert await ui.reply(modal.interaction, "Zapisane") and submitted.public == [f"<@{ala.id}> Zapisane"]


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
    refused = await game.click(message, ola, "pet:rename")
    assert refused.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)] and panel.renamed == 0
    assert refused.replies[0].message in game.channel.messages
    assert (await game.click(message, ala, "pet:rename")).answers == ["Nowe imię?"] and panel.renamed == 1
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
    assert dead.answers == [ui.MSG_STALE] and game.channel.contains(f"<@{ala.id}> {ui.MSG_STALE}")
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
    assert seen == [ala] and interaction.answers == ["Ruszasz na polowanie"]
    assert game.channel.messages[-1].content == f"<@{ala.id}> Ruszasz na polowanie"


# -- details of the public answers fixed after the review ----------------------------------------

async def test_a_text_answer_stays_a_notice_whatever_else_is_passed(game, monkeypatch):
    """Only an embed, a view or a file makes an answer "content" - not e.g. allowed_mentions."""
    monkeypatch.setattr(ui, "NOTICE_GAP", 10.0)
    ala = game.add_member("Ala")
    quiet = discord.AllowedMentions.none()
    first = _click(game, ala)
    await ui.reply(first, "Masz za mało zwojów.", allowed_mentions=quiet)
    assert first.answers == ["Masz za mało zwojów."] and first.replies[0].allowed_mentions is quiet
    again = _click(game, ala)
    assert await ui.reply(again, "Masz za mało zwojów.", allowed_mentions=quiet) and _swallowed(again)


async def test_a_busy_notice_that_could_not_be_sent_does_not_use_up_the_limit(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    failed = _click(game, ala)
    failed.response.fail_with = RuntimeError("discord is down")
    assert await ui.reply(failed, ui.MSG_BUSY) is False and failed.replies == []
    seen = _click(game, ola)                                       # the next click still gets the notice
    await ui.reply(seen, ui.MSG_BUSY)
    assert seen.answers == [ui.MSG_BUSY]
    later = _click(game, ala)                                      # ...and from now on the 20 s count
    assert await ui.reply(later, ui.MSG_BUSY) and _swallowed(later)


async def test_notices_that_mean_the_same_for_everybody_are_registered(game, monkeypatch):
    """One line on the channel instead of one per player (ui.GENERAL_NOTICES)."""
    from altermmo.cogs import events as events_cog
    from altermmo.features import boss, daily, events

    general = {ui.MSG_STALE, ui.MSG_CANNOT_PLAY, events_cog.MSG_NO_CHEST, events_cog.MSG_NO_SHRINE,
               events_cog.MSG_NOT_A_BELIEVER, events.MSG_TOO_LATE, events.MSG_SIGN_UP_TOO_LATE,
               boss.MSG_PARTY_FULL, boss.MSG_NOT_IN_PARTY, daily.MSG_JOIN_TOO_LATE}
    assert general <= ui.GENERAL_NOTICES
    monkeypatch.setattr(ui, "NOTICE_GAP", 10.0)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    first = _click(game, ala)
    await ui.reply(first, boss.MSG_NOT_IN_PARTY)
    assert first.public == [f"<@{ala.id}> {boss.MSG_NOT_IN_PARTY}"]
    second = _click(game, ola)                                     # another player, the same notice: covered
    assert await ui.reply(second, boss.MSG_NOT_IN_PARTY) and _swallowed(second)


async def test_gate_refuses_silently_while_a_fight_is_typed(game):
    """A boss fight does not hold the busy flag - buttons that start something wait all the same."""
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    fight = await _start_round(game, ala)
    assert not game.bot.state.busy
    click = _click(game, ola)
    assert await ui.gate(click) is False and _swallowed(click)
    free = _click(game, ola)
    assert await ui.gate(free, unless_busy=False) is True          # (who asks for no busy check gets none)
    game.say(ala, "blok")
    await fight
    after = _click(game, ola)
    assert await ui.gate(after) is True and after.replies == []


async def test_contests_typed_outside_ask_count_as_a_fight_too(game):
    assert not challenge.fight_on(game.channel)
    with challenge.round_running(game.channel):                    # the toast of a party, the spell for a boss
        assert challenge.fight_on(game.channel)
        with challenge.round_running(game.channel):
            pass
        assert challenge.fight_on(game.channel)                    # nested rounds are counted
    assert not challenge.fight_on(game.channel) and challenge._rounds_running == {}
    with challenge.round_running(None):                            # no channel: nothing to mark, no error
        pass
