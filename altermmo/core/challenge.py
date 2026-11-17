"""The "type the word in time" round used by every fight.

Legacy implementation and why players saw "the bot does not respond in a fight"::

    await channel.send(prompt)            # (1) HTTP round trip
    msg = await bot.wait_for("message")   # (2) only now does the bot start listening

Discord shows the prompt to players *before* the HTTP call (1) returns.  A fast
player (or a slow API response) means the answer arrives over the gateway while
nobody is listening yet - it is lost, the bot then sits out the whole timeout
and finally declares "you were too slow".  The measured time also included the
bot's own network latency, which matters a lot when a mythic boss allows 1.5 s.

:func:`ask` fixes both problems:

* it starts listening **before** the prompt is sent, so no answer can be missed;
* it judges the answer by Discord's own timestamps (``answer.created_at -
  prompt.created_at``), i.e. the player's real reaction time, independent of
  the bot's lag;
* when the gateway connection dropped or the event loop stalled during the
  round, the outcome is ``DISRUPTED`` and the caller must not punish the player.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

import discord

from . import io

log = logging.getLogger(__name__)

#: Extra reaction time granted on top of the timeout, to absorb the tiny clock
#: differences between Discord's servers stamping the prompt and the answer.
TOLERANCE = 0.25
#: How long to keep listening locally after the nominal timeout, so that a
#: valid answer that is merely *delivered* late still counts.
MIN_GRACE = 1.0
MAX_GRACE = 3.0
#: Event-loop stall (seconds) above which a timeout is not trusted.
STALL_LIMIT = 0.5
#: After this many disrupted rounds in a row a fight should give up
#: (raise ``FightInterrupted`` / end the activity without punishing the player).
MAX_CONSECUTIVE_DISRUPTIONS = 3

MSG_DISRUPTED = ("Wykryto laga po stronie Discorda <:Sadge:936907659142111273> "
                 "Ta tura się nie liczy - graj dalej!")


class Outcome(enum.Enum):
    ANSWERED = "answered"    # a player wrote something in time (may still be the wrong word)
    TIMEOUT = "timeout"      # nobody answered in time
    DISRUPTED = "disrupted"  # connection/lag problem - do not punish, repeat the round


@dataclass
class RoundResult:
    outcome: Outcome
    #: The prompt message sent by the bot (None when sending failed).
    prompt: discord.Message | None = None
    #: The player's message for ``ANSWERED``.
    message: discord.Message | None = None
    #: Player's reaction time in seconds (server-side clock); for TIMEOUT the full timeout.
    elapsed: float = 0.0

    @property
    def answered(self) -> bool:
        return self.outcome is Outcome.ANSWERED

    @property
    def author(self) -> Any:
        return self.message.author if self.message is not None else None

    @property
    def text(self) -> str:
        """The answer, lower-cased and stripped (empty when there is none)."""
        return self.message.content.strip().lower() if self.message is not None else ""

    def is_correct(self, expected: str, *, by: Any = None) -> bool:
        """True when the answer equals ``expected`` (and was written by ``by`` if given)."""
        if not self.answered:
            return False
        if by is not None and getattr(self.author, "id", None) != getattr(by, "id", by):
            return False
        return self.text == expected.strip().lower()


@contextlib.contextmanager
def _collect_messages(bot: discord.Client, channel_id: int,
                      author_ids: set[int] | None) -> Iterator[asyncio.Queue]:
    """Queue every matching message for the duration of the block."""
    queue: asyncio.Queue = asyncio.Queue()

    async def on_message(message: discord.Message) -> None:
        if message.author.bot or message.channel.id != channel_id:
            return
        if author_ids is not None and message.author.id not in author_ids:
            return
        queue.put_nowait(message)

    bot.add_listener(on_message, "on_message")
    try:
        yield queue
    finally:
        bot.remove_listener(on_message, "on_message")


def _reaction_time(prompt: discord.Message, answer: discord.Message) -> float:
    return (answer.created_at - prompt.created_at).total_seconds()


async def ask(
    bot: discord.Client,
    channel: discord.abc.Messageable,
    prompt_text: str,
    *,
    players: Iterable[Any] | None,
    timeout: float,
    **send_kwargs: Any,
) -> RoundResult:
    """Send ``prompt_text`` and wait up to ``timeout`` s for a message from ``players``.

    ``players`` - members (or ids) whose messages end the round; ``None`` means
    anybody (except bots).  The first such message written after the prompt is
    returned, whatever its content: the caller decides whether it is the right
    word (:meth:`RoundResult.is_correct`) or the right person.

    Never raises ``asyncio.TimeoutError``; sending the prompt may raise when
    Discord is unreachable for several attempts (callers treat that as a
    disrupted fight).
    """
    author_ids = None if players is None else {int(getattr(p, "id", p)) for p in players}
    net = getattr(bot, "net", None)
    channel_id = channel.id  # type: ignore[attr-defined]

    with _collect_messages(bot, channel_id, author_ids) as queue:
        started = time.monotonic()
        prompt = await io.send(channel, prompt_text, **send_kwargs)
        latency = getattr(bot, "latency", 0.0) or 0.0
        if latency != latency:  # NaN before the first heartbeat
            latency = 0.0
        grace = min(MAX_GRACE, max(MIN_GRACE, 3 * latency))
        deadline = time.monotonic() + timeout + grace

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(queue.get(), remaining)
            except asyncio.TimeoutError:
                break
            if message.id <= prompt.id:
                continue  # written before the prompt existed - not an answer to it
            elapsed = _reaction_time(prompt, message)
            if elapsed <= timeout + TOLERANCE:
                return RoundResult(Outcome.ANSWERED, prompt, message, max(elapsed, 0.0))
            log.debug("Answer arrived %.2fs after the prompt (limit %.2fs).", elapsed, timeout)
            break
        overshoot = time.monotonic() - deadline

    # Nobody answered in time - make sure it was not our fault.
    if net is not None and net.disrupted_since(started):
        log.warning("Round disrupted: the gateway connection dropped while waiting.")
        await net.wait_connected(timeout=120)
        return RoundResult(Outcome.DISRUPTED, prompt, None, timeout)
    if overshoot > STALL_LIMIT:
        log.warning("Round disrupted: the event loop stalled for %.2fs.", overshoot)
        return RoundResult(Outcome.DISRUPTED, prompt, None, timeout)

    # Safety net: an answer the gateway never delivered may still be in history.
    late = await io.last_message(channel)
    if (
        late is not None
        and late.id > prompt.id
        and not late.author.bot
        and (author_ids is None or late.author.id in author_ids)
        and _reaction_time(prompt, late) <= timeout + TOLERANCE
    ):
        log.info("Recovered an undelivered answer from channel history.")
        return RoundResult(Outcome.ANSWERED, prompt, late, max(_reaction_time(prompt, late), 0.0))

    return RoundResult(Outcome.TIMEOUT, prompt, None, timeout)


async def notify_disrupted(channel: discord.abc.Messageable) -> None:
    """Tell the players that the last round did not count (never raises)."""
    await io.send_patient(channel, MSG_DISRUPTED, give_up_after=120)


async def countdown(channel: discord.abc.Messageable, *, pause: float = 1.0) -> None:
    """The classic "Uwaga!!! 3... / ... 2... / ... 1..." before a fight starts.

    One message that is edited in place: three separate messages used to eat
    most of Discord's 5-messages-per-5-seconds channel budget right before the
    first fight prompts, which then stalled for seconds.
    """
    message = await io.send(channel, "Uwaga!!! 3...")
    for text in ("Uwaga!!! 3... 2...", "Uwaga!!! 3... 2... 1..."):
        await asyncio.sleep(pause)
        try:
            await message.edit(content=text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - cosmetic; the fight goes on
            log.debug("Could not edit the countdown message.", exc_info=True)
    await asyncio.sleep(pause)
