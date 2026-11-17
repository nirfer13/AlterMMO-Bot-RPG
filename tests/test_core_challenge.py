"""The fight round: no lost answers, fair timing, no punishment for outages."""

from __future__ import annotations

import asyncio
from datetime import timedelta

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
