"""Discord I/O helpers that survive short network problems.

Game flows must never talk to Discord "naked": a single failed ``send`` used to
abort a whole fight and leave the bot in a stuck state.  Use these helpers:

* :func:`send` - send a message, retrying transient failures a few times;
* :func:`send_patient` - for background announcements: keeps retrying for minutes;
* :func:`wait_for_message` - wait for a player's message, scoped to a channel
  and (optionally) to a set of authors, never matching bots;
* :func:`clear_channel` - wipe the boss channel but keep its first message and pins;
* :func:`resolve_member`, :func:`last_message`, :func:`safe_delete`,
  :func:`safe_react`, :func:`safe_typing`, :func:`retry`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, TypeVar

import aiohttp
import discord

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Failures worth retrying: network trouble or Discord having a bad moment.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    TimeoutError,
    discord.DiscordServerError,
    discord.GatewayNotFound,
    ConnectionError,
    OSError,
)


def is_transient(exc: BaseException) -> bool:
    """True for errors caused by the network / Discord outages (not by our request)."""
    if isinstance(exc, (discord.Forbidden, discord.NotFound)):
        return False
    if isinstance(exc, discord.HTTPException) and not isinstance(exc, discord.DiscordServerError):
        return exc.status >= 500 or exc.status == 429
    return isinstance(exc, TRANSIENT_ERRORS)


#: Longest time one attempt of a Discord call may take.  discord.py itself sets
#: no limit, so aiohttp's default of 5 minutes would apply: with a black-holed
#: connection one ``send`` could freeze a fight for a quarter of an hour.
ATTEMPT_TIMEOUT = 60.0


async def retry(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    first_delay: float = 0.5,
    max_delay: float = 30.0,
    what: str = "Discord call",
    attempt_timeout: float | None = None,
) -> T:
    """Await ``factory()`` and retry it on transient errors with backoff.

    ``factory`` must create a *new* awaitable on every call, e.g.
    ``await retry(lambda: member.add_roles(role))``.  Every attempt is limited
    to ``attempt_timeout`` seconds (default :data:`ATTEMPT_TIMEOUT`).
    """
    limit = ATTEMPT_TIMEOUT if attempt_timeout is None else attempt_timeout
    delay = first_delay
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(factory(), limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc) or attempt == attempts:
                raise
            log.warning("%s failed (%s: %s); retry %d/%d in %.1fs.",
                        what, type(exc).__name__, exc, attempt, attempts - 1, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 3, max_delay)
    raise AssertionError("unreachable")


class _MessageRef:
    """Just the id of a message that is not in discord.py's message cache."""

    __slots__ = ("id",)

    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _RawReaction:
    """Stand-in for :class:`discord.Reaction` built from a raw gateway event."""

    __slots__ = ("emoji", "message")

    def __init__(self, emoji: Any, message_id: int) -> None:
        self.emoji = emoji
        self.message = _MessageRef(message_id)


@contextlib.contextmanager
def reaction_listener(
    bot: Any,
    on_add: Callable[[Any, Any], Awaitable[None]],
    on_remove: Callable[[Any, Any], Awaitable[None]] | None = None,
):
    """Call ``on_add(reaction, user)`` / ``on_remove(reaction, user)`` for every click.

    discord.py only dispatches ``reaction_add`` / ``reaction_remove`` for
    messages in its message cache, and that cache is emptied whenever the bot
    has to log in again after a longer connection loss - reactions under an
    event message posted before the outage would then never be heard.  This
    listens to the raw events as well and forwards the ones the cache missed.
    For those, ``reaction`` only has ``.emoji`` and ``.message.id`` and ``user``
    may be a bare object with an ``id`` when the member is unknown.
    """

    def cached(message_id: int) -> bool:
        return any(message.id == message_id for message in bot.cached_messages)

    def user_of(payload: Any) -> Any:
        member = getattr(payload, "member", None)
        if member is not None:
            return member
        guild = bot.get_guild(payload.guild_id) if getattr(payload, "guild_id", None) else None
        found = guild.get_member(payload.user_id) if guild is not None else None
        return found or bot.get_user(payload.user_id) or discord.Object(id=payload.user_id)

    def forwarder(callback: Callable[[Any, Any], Awaitable[None]]):
        async def on_raw(payload: Any) -> None:
            if cached(payload.message_id):
                return  # the ordinary event delivers this one
            if bot.user is not None and payload.user_id == bot.user.id:
                return
            await callback(_RawReaction(payload.emoji, payload.message_id), user_of(payload))
        return on_raw

    listeners: list[tuple[Any, str]] = [(on_add, "on_reaction_add"),
                                        (forwarder(on_add), "on_raw_reaction_add")]
    if on_remove is not None:
        listeners += [(on_remove, "on_reaction_remove"),
                      (forwarder(on_remove), "on_raw_reaction_remove")]
    for callback, name in listeners:
        bot.add_listener(callback, name)
    try:
        yield
    finally:
        for callback, name in listeners:
            bot.remove_listener(callback, name)


def _build_kwargs(kwargs: dict[str, Any], file_path: str | Path | None,
                  file_paths: Iterable[str | Path] | None) -> dict[str, Any]:
    """Create fresh ``discord.File`` objects for every attempt (they are single-use)."""
    built = dict(kwargs)
    if file_path is not None:
        built["file"] = discord.File(str(file_path))
    if file_paths is not None:
        built["files"] = [discord.File(str(path)) for path in file_paths]
    return built


async def send(
    destination: discord.abc.Messageable,
    content: str | None = None,
    *,
    file_path: str | Path | None = None,
    file_paths: Iterable[str | Path] | None = None,
    attempts: int = 3,
    **kwargs: Any,
) -> discord.Message:
    """Send a message; transient failures are retried ``attempts`` times.

    Pass files as ``file_path=`` / ``file_paths=`` (paths), not as ``discord.File``
    objects, so they can be re-opened for a retry.  Other keyword arguments
    (``embed=``, ``view=``, ``allowed_mentions=`` ...) go straight to ``send``.
    Raises the last error when all attempts fail.
    """
    paths = list(file_paths) if file_paths is not None else None
    return await retry(
        lambda: destination.send(content, **_build_kwargs(kwargs, file_path, paths)),
        attempts=attempts,
        what=f"send to {getattr(destination, 'name', destination)}",
    )


async def send_patient(
    destination: discord.abc.Messageable,
    content: str | None = None,
    *,
    give_up_after: float = 900.0,
    **kwargs: Any,
) -> discord.Message | None:
    """Like :func:`send` but keeps trying for up to ``give_up_after`` seconds.

    For background announcements (boss spawn, events) that should simply be
    delivered once the internet is back.  Returns None when it finally gives up
    or the error is permanent (missing permissions, channel deleted) - it never
    raises, so a background loop cannot die on it.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + give_up_after
    delay = 1.0
    while True:
        try:
            return await send(destination, content, attempts=2, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc):
                log.error("Cannot send message (permanent error): %s: %s", type(exc).__name__, exc)
                return None
            if loop.time() + delay >= deadline:
                log.error("Giving up sending a message after %.0fs: %s", give_up_after, exc)
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


@contextlib.asynccontextmanager
async def safe_typing(channel: discord.abc.Messageable) -> AsyncIterator[None]:
    """``async with safe_typing(channel):`` - "bot is typing..." that can never fail."""
    manager = None
    try:
        manager = channel.typing()
        await manager.__aenter__()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        manager = None
    try:
        yield
    finally:
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.__aexit__(None, None, None)


async def wait_for_message(
    bot: discord.Client,
    channel: discord.abc.Messageable | None,
    *,
    authors: Iterable[Any] | None = None,
    check: Callable[[discord.Message], bool] | None = None,
    timeout: float | None,
) -> discord.Message:
    """Wait for the next matching message; raises ``asyncio.TimeoutError``.

    * only messages from ``channel`` match (pass None to accept any channel);
    * messages written by bots never match;
    * ``authors`` (members/users or ids) restricts who may answer;
    * ``check`` is an extra predicate.
    """
    channel_id = getattr(channel, "id", None)
    author_ids: set[int] | None = None
    if authors is not None:
        author_ids = {int(getattr(a, "id", a)) for a in authors}

    def predicate(message: discord.Message) -> bool:
        if message.author.bot:
            return False
        if channel_id is not None and message.channel.id != channel_id:
            return False
        if author_ids is not None and message.author.id not in author_ids:
            return False
        if check is not None and not check(message):
            return False
        return True

    return await bot.wait_for("message", check=predicate, timeout=timeout)


async def last_message(channel: discord.abc.Messageable) -> discord.Message | None:
    """Newest message on the channel, or None (also on any failure)."""
    try:
        async for message in channel.history(limit=1):
            return message
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.debug("Could not read channel history.", exc_info=True)
    return None


async def clear_channel(channel: discord.TextChannel, *, limit: int = 1000) -> int:
    """Delete the channel's messages except the oldest one and pinned ones.

    The oldest message on the boss channel is the permanent "how to play" post.
    Returns the number of deleted messages; never raises.
    """
    try:
        oldest: discord.Message | None = None
        async for message in channel.history(limit=1, oldest_first=True):
            oldest = message
        if oldest is None:
            return 0
        keep_id = oldest.id
        deleted = await channel.purge(
            limit=limit,
            check=lambda m: m.id != keep_id and not m.pinned,
            reason="AlterMMO RPG: czyszczenie kanału",
        )
        return len(deleted)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Problem with deleting messages: %s: %s", type(exc).__name__, exc)
        return 0


async def resolve_member(guild: discord.Guild | None, user_id: int | str) -> discord.Member | None:
    """Member by id from cache or the API; None when they left the server."""
    if guild is None:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    member = guild.get_member(uid)
    if member is not None:
        return member
    try:
        return await retry(lambda: guild.fetch_member(uid), what="fetch_member")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - NotFound (left the guild) or a persistent outage
        return None


async def safe_delete(message: discord.Message | None) -> bool:
    """Delete a message, ignoring every failure."""
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False


async def safe_react(message: discord.Message | None, emoji: str) -> bool:
    """Add a reaction, ignoring every failure (reactions are cosmetic)."""
    if message is None:
        return False
    try:
        await message.add_reaction(emoji)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False


async def add_role(member: discord.Member | None, role: discord.Role | None, *, reason: str | None = None) -> bool:
    """Give a role; False (and a log line) when it is impossible.  Never raises."""
    if member is None or role is None:
        return False
    try:
        await retry(lambda: member.add_roles(role, reason=reason), what="add_roles")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot add role %s to %s: %s: %s", role, member, type(exc).__name__, exc)
        return False


async def remove_role(member: discord.Member | None, role: discord.Role | None, *, reason: str | None = None) -> bool:
    """Take a role away; False (and a log line) when it is impossible.  Never raises."""
    if member is None or role is None:
        return False
    try:
        await retry(lambda: member.remove_roles(role, reason=reason), what="remove_roles")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot remove role %s from %s: %s: %s", role, member, type(exc).__name__, exc)
        return False
