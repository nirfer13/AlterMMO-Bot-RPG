"""The fight round: no lost answers, fair timing, no punishment for outages."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from altermmo.core import challenge
from altermmo.core.challenge import Outcome

from fakes import FakeMessage, requested_word


async def test_answer_in_time_is_returned(game):
    player = game.add_member("Ala")
    game.say_later(player, "UNIK", 0.05)
    result = await challenge.ask(game.bot, game.channel, "1. Wpisz **U N I K**",
                                 players=[player], timeout=1.0)
    assert result.outcome is Outcome.ANSWERED
    assert result.is_correct("unik", by=player)
    assert 0.0 <= result.elapsed < 1.0


async def test_answer_sent_while_prompt_is_still_being_delivered_is_not_lost(game):
    """Regression for the legacy race: the answer arrives before ``send`` returns."""
    player = game.add_member("Szybki")
    original_send = game.channel.send

    async def slow_send(*args, **kwargs):
        message = await original_send(*args, **kwargs)
        # Discord already shows the prompt; the player answers at once, but our
        # HTTP call needs another 300 ms to return.
        game.say(player, "atak")
        await asyncio.sleep(0.3)
        return message

    game.channel.send = slow_send
    result = await challenge.ask(game.bot, game.channel, "1. Wpisz **A T A K**",
                                 players=[player], timeout=0.2)
    assert result.outcome is Outcome.ANSWERED
    assert result.is_correct("atak")
    assert result.elapsed < 0.2


async def test_message_written_before_the_prompt_is_ignored(game):
    player = game.add_member("Ala")
    original_send = game.channel.send

    async def send_after_stale(*args, **kwargs):
        game.say(player, "unik")  # leftover from the previous round
        await asyncio.sleep(0.01)
        return await original_send(*args, **kwargs)

    game.channel.send = send_after_stale
    result = await challenge.ask(game.bot, game.channel, "2. Wpisz **U N I K**",
                                 players=[player], timeout=0.2)
    assert result.outcome is Outcome.TIMEOUT


async def test_timeout_when_nobody_answers(game):
    player = game.add_member("Ala")
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                 players=[player], timeout=0.1)
    assert result.outcome is Outcome.TIMEOUT
    assert not result.is_correct("blok")


async def test_other_people_and_other_channels_do_not_count(game):
    player = game.add_member("Ala")
    stranger = game.add_member("Obcy")
    game.say_later(stranger, "blok", 0.02)
    game.say_later(player, "blok", 0.03, channel=game.log_channel)
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                 players=[player], timeout=0.15)
    assert result.outcome is Outcome.TIMEOUT


async def test_wrong_word_still_ends_the_round(game):
    player = game.add_member("Ala")
    game.say_later(player, "zle", 0.02)
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                 players=[player], timeout=1.0)
    assert result.outcome is Outcome.ANSWERED
    assert not result.is_correct("blok")


async def test_answer_after_the_deadline_is_too_late_even_if_delivered_in_grace(game, monkeypatch):
    monkeypatch.setattr(challenge, "MIN_GRACE", 1.0)
    player = game.add_member("Wolny")
    game.say_later(player, "blok", 0.6)  # inside the local grace period, but too slow
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                 players=[player], timeout=0.1)
    assert result.outcome is Outcome.TIMEOUT


async def test_gateway_drop_during_round_is_not_the_players_fault(game):
    player = game.add_member("Ala")

    async def outage():
        await asyncio.sleep(0.05)
        game.bot.net.mark_disconnected()
        await asyncio.sleep(0.2)
        game.bot.net.mark_connected()

    task = asyncio.ensure_future(outage())
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                 players=[player], timeout=0.1)
    await task
    assert result.outcome is Outcome.DISRUPTED


async def test_undelivered_answer_is_recovered_from_history(game):
    """The gateway swallowed the event, but the message is on the channel."""
    player = game.add_member("Ala")
    original_send = game.channel.send

    async def send_then_silent_answer(*args, **kwargs):
        prompt = await original_send(*args, **kwargs)
        answer = FakeMessage(game.channel, player, "blok")
        answer.created_at = prompt.created_at + timedelta(seconds=0.05)
        game.channel.messages.append(answer)  # no dispatch: the bot never "hears" it
        return prompt

    game.channel.send = send_then_silent_answer
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                 players=[player], timeout=0.1)
    assert result.outcome is Outcome.ANSWERED
    assert result.is_correct("blok", by=player)


def _undelivered(channel, author, text, prompt, after: float) -> FakeMessage:
    """A message that is on the channel ``after`` s after the prompt but never reached the bot."""
    message = FakeMessage(channel, author, text)
    message.created_at = prompt.created_at + timedelta(seconds=after)
    channel.messages.append(message)
    return message


def _prompt_then(game, write) -> None:
    """Make the prompt be followed by what ``write(prompt, bot_send)`` puts on the channel."""
    bot_send = game.channel.send

    async def send(*args, **kwargs):
        prompt = await bot_send(*args, **kwargs)
        await write(prompt, bot_send)
        return prompt

    game.channel.send = send


async def test_safety_net_finds_an_answer_behind_later_messages(game):
    """A bot message and a bystander's message written after the answer do not hide it."""
    player, stranger = game.add_member("Ala"), game.add_member("Obcy")

    async def write(prompt, bot_send):
        _undelivered(game.channel, player, "blok", prompt, 0.05)
        game.say(stranger, "hej")                                    # heard, but not a fighter
        await bot_send("Ola dołącza do walki!")                       # the bot writes last

    _prompt_then(game, write)
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**", players=[player], timeout=0.1)
    assert game.channel.messages[-1].author.bot                     # the newest message is the bot's
    assert result.outcome is Outcome.ANSWERED and result.is_correct("blok", by=player)
    assert result.elapsed == pytest.approx(0.05, abs=0.001)


async def test_safety_net_takes_the_first_undelivered_answer(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")

    async def write(prompt, bot_send):
        _undelivered(game.channel, ola, "zle", prompt, 0.03)
        _undelivered(game.channel, ala, "blok", prompt, 0.05)

    _prompt_then(game, write)
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**", players=[ala, ola], timeout=0.1)
    assert result.answered and result.author is ola and result.text == "zle"
    assert not result.is_correct("blok")


async def test_safety_net_ignores_an_answer_written_too_late(game):
    player = game.add_member("Ala")

    async def write(prompt, bot_send):
        _undelivered(game.channel, player, "blok", prompt, 0.1 + challenge.TOLERANCE + 0.2)

    _prompt_then(game, write)
    result = await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**", players=[player], timeout=0.1)
    assert result.outcome is Outcome.TIMEOUT and result.message is None


async def test_a_fight_is_on_while_a_round_runs_and_for_a_while_after(game, monkeypatch):
    player = game.add_member("Ala")
    assert not challenge.fight_on(game.channel) and not challenge.fight_on(None)
    task = asyncio.ensure_future(challenge.ask(game.bot, game.channel, "Wpisz **B L O K**",
                                               players=[player], timeout=2))
    while not game.channel.contains("B L O K"):
        await asyncio.sleep(0)
    assert challenge.fight_on(game.channel) and not challenge.fight_on(game.notice_channel)
    game.say(player, "blok")
    await task
    ended = time.monotonic()
    assert not challenge.fight_on(game.channel)                     # FIGHT_LINGER is 0 in the tests

    clock = [ended + 14.0]
    monkeypatch.setattr(challenge, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(challenge, "FIGHT_LINGER", 15.0)
    assert challenge.fight_on(game.channel) and not challenge.fight_on(game.notice_channel)
    clock[0] = ended + 15.0
    assert not challenge.fight_on(game.channel)                     # the linger is over

    clock[0] = ended + 100.0
    await challenge.countdown(game.channel, pause=0)                # "Uwaga!!! 3..." starts a fight
    assert challenge.fight_on(game.channel) and not challenge.fight_on(game.notice_channel)
    clock[0] += 14.9
    assert challenge.fight_on(game.channel)
    clock[0] += 0.1
    assert not challenge.fight_on(game.channel)


async def test_listener_is_removed_after_the_round(game):
    player = game.add_member("Ala")
    before = len(game.bot.extra_events.get("on_message", []))
    await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**", players=[player], timeout=0.05)
    assert len(game.bot.extra_events.get("on_message", [])) == before


def test_requested_word_helper():
    assert requested_word("3. Boss szarżuje na Ciebie! Wpisz **U N I K**") == "unik"
    assert requested_word("Boss rzuca klątwę, wpisz **Z A K L Ę C I E**") == "zaklęcie"
    assert requested_word("5. **Ala**: Boss atakuje, wpisz **P A R U J**") == "paruj"
    assert requested_word("Brawo **Ala**!") is None
