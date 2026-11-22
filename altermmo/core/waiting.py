"""A notice for the player while a slow picture is on its way ("🐾 Nadchodzi towarzysz...").

Owner (2026-11-22): "Czy możesz pokazywać wiadomość nadchodzi towarzysz z
animowanymi kropkami... jeśli karta dużo waży i pojawia się po raz pierwszy?"
A pet card with an animated frame or Holo takes seconds to draw on a cold
cache (and longer when other players' cards are drawn before it: the bot draws
one at a time); until it is there the player saw nothing and clicked again.

The rule is "show it when it is slow", not "ask the renderer whether it will
be slow": a card from the cache or a still card is back in a fraction of a
second and never shows the notice (:data:`NOTICE_DELAY`); a heavy card on a
cold cache, or one waiting in the queue, does.

Usage - the block covers drawing the card AND sending / editing the message
that shows it, so the notice disappears when the pet is there, not before
(an upload of a big card takes a moment too); only the work passed to
:meth:`Waiting.run` (the card being drawn) can make the notice appear::

    async with waiting.notice(scene.channel) as wait:
        card = await wait.run(petcards.card(...))
        await scene.send(file_path=card)

or, for a single piece of work, ``await waiting.while_slow(channel, work)``.

Rules (the notice is decoration and must never do harm):

* an ordinary channel message without mentions, its dots animated by editing
  it every :data:`NOTICE_STEP` seconds ("." ".." "..." "." ...; every message
  edit on a channel shares Discord's budget of about 5 per 5 s, so the dots
  take at most half of it), :data:`NOTICE_MAX_EDITS` frames at most - then the
  last frame stays until the wait ends;
* nothing is posted while a typed fight is on (``challenge.fight_on``); a
  fight that starts while the notice is up takes it away;
* the work's result or exception passes through unchanged, cancellation too;
  a failed send / edit / delete of the notice is only logged (a failed edit
  stops the animation, a failed delete is tried once more, then left);
* ONE notice per channel: two players waiting on the same channel share it
  (reference-counted) - it goes when the last of them has his picture;
* no task outlives the waits: the last one to leave waits for the notice's
  task to delete the message and end (every Discord call of the notice is
  limited to :data:`CALL_TIMEOUT` seconds, so that is bounded).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator, Awaitable, TypeVar

import discord

from . import challenge

log = logging.getLogger(__name__)

T = TypeVar("T")

#: The notice; the dots of :data:`DOTS` follow it.
MSG_COMING = "🐾 Nadchodzi towarzysz"
#: The animation, one frame per :data:`NOTICE_STEP` (plain characters: they work everywhere).
DOTS: tuple[str, ...] = (".", "..", "...")
#: Seconds the work may take before the notice is posted: a card from the cache or a
#: still card (~0.2 s) never shows it.  (The test-suite sets it out of reach.)
NOTICE_DELAY = 0.8
#: Seconds between two frames of the animation.  Every message edit on a channel shares one
#: Discord budget (about 5 per 5 s): the album page, the pet panel and the hunt roster edit there
#: too, so the dots take at most half of it - they must never hold those edits back.
NOTICE_STEP = 2.0
#: Frames edited at most (about 12 s of dots); then the last one stays until the wait ends, so a
#: render that hangs (or a long queue of cold cards) does not edit the channel for ever.
NOTICE_MAX_EDITS = 6
#: Longest time one Discord call of the notice (send, edit, delete) may take.
CALL_TIMEOUT = 5.0
#: Pause before the second (and last) try of deleting the notice.
DELETE_RETRY_PAUSE = 0.5

#: channel key -> the notice shown there now.
_notices: dict[int, "_Notice"] = {}
#: The notices' tasks (kept here so that a running one is never garbage-collected).
_tasks: set[asyncio.Task[None]] = set()


def text(step: int) -> str:
    """The notice at frame ``step`` of the animation (0: the message as it is posted)."""
    return MSG_COMING + DOTS[step % len(DOTS)]


def _key(channel: Any) -> int:
    channel_id = getattr(channel, "id", None)
    return int(channel_id) if channel_id is not None else id(channel)


async def _call(work: Awaitable[T]) -> T:
    """One Discord call of the notice, limited to :data:`CALL_TIMEOUT`; raises its error."""
    return await asyncio.wait_for(work, CALL_TIMEOUT)


class _Notice:
    """The notice of one channel: posts it, animates it, deletes it (one task)."""

    def __init__(self, channel: Any) -> None:
        self.channel = channel
        self.users = 0
        self.message: Any = None
        self._stop = asyncio.Event()
        self.task: asyncio.Task[None] = asyncio.get_running_loop().create_task(
            self._run(), name=f"waiting-notice-{_key(channel)}")
        _tasks.add(self.task)
        self.task.add_done_callback(_tasks.discard)

    def stop(self) -> None:
        self._stop.set()

    async def _run(self) -> None:
        try:
            if challenge.fight_on(self.channel) or self._stop.is_set():
                return
            try:
                self.message = await _call(
                    self.channel.send(text(0), allowed_mentions=discord.AllowedMentions.none()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - decoration: the pet is shown without it
                log.info("Waiting notice: could not post it (%s: %s).", type(exc).__name__, exc)
                return
            step, animate = 0, True
            while not self._stop.is_set() and not challenge.fight_on(self.channel):
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), max(0.0, NOTICE_STEP))
                # (After the last frame the loop goes on without edits: a fight that starts
                # still takes the notice away.)
                if (self._stop.is_set() or challenge.fight_on(self.channel) or not animate
                        or step >= NOTICE_MAX_EDITS):
                    continue
                step += 1
                try:
                    await _call(self.message.edit(content=text(step)))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the notice stays as it is until the end
                    log.info("Waiting notice: animation stopped (%s: %s).", type(exc).__name__, exc)
                    animate = False
        finally:
            await self._remove()

    async def _remove(self) -> None:
        """Delete the notice; one more try after a failure, then it is left.

        Raises nothing but cancellation.
        """
        message, self.message = self.message, None
        if message is None:
            return
        for attempt in (1, 2):
            try:
                await _call(message.delete())
                return
            except asyncio.CancelledError:
                raise
            except discord.NotFound:
                return  # gone already (purged)
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log.warning("Waiting notice %s could not be deleted (%s: %s); left.",
                                getattr(message, "id", "?"), type(exc).__name__, exc)
                    return
            await asyncio.sleep(DELETE_RETRY_PAUSE)


class Waiting:
    """One caller's wait on one channel (see the module's docstring); made by :func:`notice`.

    ``channel`` None (or something one cannot post to): no notice, the work is
    simply awaited.
    """

    def __init__(self, channel: Any) -> None:
        self.channel = channel if callable(getattr(channel, "send", None)) else None
        self._notice: _Notice | None = None
        self._closed = False

    @property
    def shown(self) -> bool:
        """Whether this wait has joined a notice (it is posted, or being posted)."""
        return self._notice is not None

    async def run(self, work: Awaitable[T]) -> T:
        """Await ``work``; when it is still running after :data:`NOTICE_DELAY`, the notice goes up.

        The notice stays until the wait ends (the end of the ``async with``),
        not when ``work`` returns.  Result, exception and cancellation of
        ``work`` pass through unchanged.
        """
        if self.channel is None or self._closed or self._notice is not None:
            return await work
        handle = asyncio.get_running_loop().call_later(max(0.0, NOTICE_DELAY), self._join)
        try:
            return await work
        finally:
            handle.cancel()

    def _join(self) -> None:
        """(A timer callback: nothing awaited.)  Show the channel's notice, or share the one up."""
        try:
            if self._closed or self._notice is not None or challenge.fight_on(self.channel):
                return
            key = _key(self.channel)
            shared = _notices.get(key)
            if shared is None or shared.task.done():  # (done: a fight took it away; its waits end soon)
                shared = _notices[key] = _Notice(self.channel)
            shared.users += 1
            self._notice = shared
        except Exception:  # noqa: BLE001 - decoration
            log.exception("Waiting notice: could not be started.")

    async def close(self) -> None:
        """End the wait; the last one waiting on the channel takes the notice away and waits for that."""
        self._closed = True
        shared, self._notice = self._notice, None
        if shared is None:
            return
        shared.users -= 1
        if shared.users > 0:
            return
        key = _key(shared.channel)
        if _notices.get(key) is shared:
            del _notices[key]  # a wait that starts now shows a new notice
        shared.stop()
        # asyncio.wait, not ``await task``: a second cancellation of the caller must not cut
        # the deletion short (it would leave the notice on the channel) - the task ends by itself.
        await asyncio.wait({shared.task})


@contextlib.asynccontextmanager
async def notice(channel: Any) -> AsyncIterator[Waiting]:
    """``async with notice(channel) as wait:`` - see the module's docstring."""
    wait = Waiting(channel)
    try:
        yield wait
    finally:
        await wait.close()


async def while_slow(channel: Any, work: Awaitable[T]) -> T:
    """Await ``work``; the notice is up on ``channel`` while it is slow, and gone when it ends."""
    async with notice(channel) as wait:
        return await wait.run(work)
