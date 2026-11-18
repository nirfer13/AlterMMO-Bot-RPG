"""Buttons, select menus and pop-up windows (``discord.ui``) for the game flows.

The old bot knew only typed commands and reactions.  Everything clickable goes
through this module, so that every panel behaves the same way:

* a click is ALWAYS answered - Discord shows "interaction failed" when a bot
  stays silent for 3 seconds.  Refusals are *ephemeral* (only the clicker sees
  them), so they never clutter the boss channel;
* an error inside a click handler is answered, logged and reported to the owner
  (:class:`GameView`);
* buttons of a flow that is over (also: from before a restart of the bot) say
  so and disappear instead of failing (:func:`handle_stale`, wired up in
  ``altermmo/cogs/ui.py``);
* who may click: a gameplay button works only for members who may write on the
  channel (:func:`may_play`).  The "dead hunter" ban of this server is a role
  that takes the right to write on the boss channel away - a button can be
  clicked without that right, so the same rule is checked here.

Two kinds of buttons:

**Flow buttons** (:class:`ClickView`, :func:`confirm`, :func:`pick`) live as
long as the coroutine that posted them: an event waiting for volunteers, a
"are you sure?" question, a menu.  The flow reads the clicks from a queue::

    async with ui.ClickView([ui.Choice("join", "Wkrocz do lasu", emoji="🌲")]) as view:
        await view.send(scene.channel, embed=embed)
        click = await view.next(timeout=600)        # None: nobody clicked
        if click is not None:
            player = click.member

**Permanent buttons** (a pinned panel, the "attack" button under a boss) must
work after a restart: subclass :class:`GameView` with ``timeout=None``, give
every item a fixed ``custom_id`` starting with :data:`PREFIX` (but not
:data:`FLOW_PREFIX`), keep no state in the view and register one instance at
start-up with :func:`register`.

Texts shown to players are Polish, like the rest of the game.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Sequence

import discord

from . import io
from .scene import Scene

log = logging.getLogger(__name__)

#: Every ``custom_id`` of this bot starts with this.
PREFIX = "altermmo:"
#: ``custom_id`` prefix of flow buttons (they die with their flow).
FLOW_PREFIX = PREFIX + "flow:"

MSG_STALE = "Ten przycisk już nie działa - to wydarzenie dobiegło końca."
MSG_NOT_YOURS = "Ten przycisk nie jest dla Ciebie."
MSG_NOT_YOURS_NAMED = "Ten przycisk jest dla <@{player_id}>."
MSG_CANNOT_PLAY = ("Nie możesz teraz brać udziału w zabawie na tym kanale <:RIP:912797982917816341> "
                   "(polegli łowcy odpoczywają aż do zabicia następnego bossa).")
MSG_WRONG_CHANNEL = "To działa tylko na kanale <#{channel_id}>."
MSG_BUSY = "Trwa teraz inne wydarzenie – spróbuj ponownie za chwilę <:Bedge:970576892874854400>"
MSG_FAILED = "Coś poszło nie tak. Spróbuj ponownie za chwilę <:Sadge:936907659142111273>"

#: Return this from an ``allow`` callback that has answered the click itself.
DROP = ""

#: Ids of the messages whose flow buttons somebody is listening to right now.
_live: set[int] = set()
#: Ids of the last interactions answered as stale: discord.py hands one click to
#: the view AND to the ``on_interaction`` listener - only one of them may answer.
_stale_answered: collections.deque[int] = collections.deque(maxlen=256)


# -- answering an interaction ----------------------------------------------------------

def _files(kwargs: dict[str, Any]) -> dict[str, Any]:
    """``file_path=`` / ``file_paths=`` (as in :func:`altermmo.core.io.send`) -> ``discord.File``."""
    built = dict(kwargs)
    path = built.pop("file_path", None)
    paths = built.pop("file_paths", None)
    if path is not None:
        built["file"] = discord.File(str(path))
    if paths is not None:
        built["files"] = [discord.File(str(item)) for item in paths]
    return built


async def reply(interaction: Any, content: str | None = None, *, ephemeral: bool = True,
                **kwargs: Any) -> bool:
    """Answer a click with a message (ephemeral by default).  Never raises.

    Works before and after the interaction was acknowledged.  ``embed=``,
    ``view=``, ``file_path=`` ... are passed on.  False when Discord refused.
    """
    try:
        built = _files(kwargs)
        if not interaction.response.is_done():
            await interaction.response.send_message(content, ephemeral=ephemeral, **built)
        else:
            await interaction.followup.send(content, ephemeral=ephemeral, **built)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - an answer is never worth an error
        log.info("Could not answer an interaction of %s (%s: %s).",
                 getattr(interaction.user, "id", "?"), type(exc).__name__, exc)
        return False


async def ack(interaction: Any) -> bool:
    """Acknowledge a click without showing anything (no "interaction failed").  Never raises."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("Could not acknowledge an interaction (%s: %s).", type(exc).__name__, exc)
        return False


async def update(interaction: Any, **kwargs: Any) -> bool:
    """Edit the message the clicked component sits on (this also answers the click).  Never raises."""
    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        else:
            await interaction.message.edit(**kwargs)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("Could not update the message of an interaction (%s: %s).", type(exc).__name__, exc)
        return False


async def retire(message: Any, **kwargs: Any) -> bool:
    """Take the buttons off ``message`` (``content=`` / ``embed=`` may be changed too).  Never raises."""
    if message is None:
        return False
    try:
        await message.edit(view=None, **kwargs)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - usually: the message was purged meanwhile
        return False


# -- who, where, when --------------------------------------------------------------------

def scene_of(interaction: Any) -> Scene:
    """The :class:`Scene` of a click: its channel, the member who clicked, no message."""
    return Scene(bot=interaction.client, channel=interaction.channel, author=interaction.user)


@dataclass(frozen=True)
class WhisperScene(Scene):
    """A scene whose :meth:`send` answers the clicker privately (ephemeral).

    For display-only panels opened by a button (pet, rankings ...): the
    answer is visible only to the player and does not fill the channel.  Only
    ``scene.send`` whispers; code that writes to ``scene.channel`` directly
    stays public.  ``send`` returns None (there is no message to react to).
    """

    interaction: Any = None

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        kwargs.pop("attempts", None)
        if not await reply(self.interaction, content, ephemeral=True, **kwargs):
            raise RuntimeError("the private answer could not be delivered")
        return None


def whisper_scene(interaction: Any) -> WhisperScene:
    return WhisperScene(bot=interaction.client, channel=interaction.channel, author=interaction.user,
                        interaction=interaction)


def may_play(channel: Any, member: Any) -> bool:
    """True when ``member`` may write on ``channel`` (see the module docstring: dead hunters)."""
    try:
        return bool(channel.permissions_for(member).send_messages)
    except Exception:  # noqa: BLE001 - unknown member/channel type: do not lock anybody out
        return True


async def gate(interaction: Any, *, boss_channel: bool = True, unless_busy: bool = True,
               players_only: bool = True) -> bool:
    """The guards of a gameplay command, for a click.  Refusals are answered ephemerally.

    ``boss_channel``  only on the boss channel (owner's rule for gameplay);
    ``unless_busy``   not while an event / hunt / tower run holds the channel;
    ``players_only``  only members who may write on the channel.
    Returns True when the click may go on (nothing was answered yet).
    """
    bot = interaction.client
    if boss_channel and not bot.is_boss_channel(interaction.channel):
        await reply(interaction, MSG_WRONG_CHANNEL.format(channel_id=bot.ids.boss_channel))
        return False
    if players_only and not may_play(interaction.channel, interaction.user):
        await reply(interaction, MSG_CANNOT_PLAY)
        return False
    if unless_busy and bot.state.busy:
        await reply(interaction, MSG_BUSY)
        return False
    return True


def when(moment: datetime | float | int, style: str = "R") -> str:
    """Discord time markup, e.g. ``<t:1700000000:R>`` - shown as "za 3 godziny".

    ``moment``: an aware datetime, or seconds from now.  Styles: ``R`` relative,
    ``t`` time, ``f`` date and time.  Discord renders it in every reader's own
    language and time zone - but only in message content and embed
    descriptions / field values, NOT in embed titles, footers or button labels.
    """
    if isinstance(moment, datetime):
        unix = moment.timestamp()
    else:
        unix = time.time() + float(moment)
    return f"<t:{int(unix)}:{style}>"


# -- views -------------------------------------------------------------------------------

class GameView(discord.ui.View):
    """Base of every view: a failing click handler is answered and reported."""

    async def on_error(self, interaction: Any, error: Exception, item: Any, /) -> None:
        log.error("Click handler failed (view %s, item %s, user %s).", type(self).__name__,
                  getattr(item, "custom_id", item), getattr(interaction.user, "id", "?"), exc_info=error)
        await reply(interaction, MSG_FAILED)
        bot = interaction.client
        report = getattr(bot, "log_to_owner", None)
        if report is not None:
            await report(f"Błąd przycisku `{getattr(item, 'custom_id', '?')}` ({interaction.user}): "
                         f"`{type(error).__name__}: {error}`")


def register(bot: Any, view: discord.ui.View) -> bool:
    """Register a permanent view at start-up (``cog_load``), so old messages keep working.  Never raises."""
    try:
        bot.add_view(view)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the buttons of old messages stay dead, nothing else
        log.exception("Could not register the permanent view %s.", type(view).__name__)
        return False


@dataclass(frozen=True)
class Choice:
    """One button of a :class:`ClickView`."""

    key: str
    label: str | None = None
    emoji: Any = None
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    row: int | None = None
    disabled: bool = False


@dataclass
class Click:
    """One accepted click: which button, who, and the interaction (already acknowledged)."""

    key: str
    member: Any
    interaction: Any


class ClickView(GameView):
    """Buttons whose clicks are queued for the flow that posted them.

    ``only``          members (or ids) who may click; everybody else gets a private refusal;
    ``allow``         ``async (interaction, key) -> str | None``: None accepts the click, a text
                      refuses it with that (ephemeral) text, :data:`DROP` when the callback has
                      answered the click itself;
    ``players_only``  refuse members who may not write on the channel (dead hunters);
    ``auto_ack``      acknowledge accepted clicks at once (default).  Switch off when the flow
                      answers each click itself within 3 seconds (a pop-up window, an
                      ``interaction.response.edit_message``).

    Use it as ``async with`` (or call :meth:`close`): the buttons are taken off
    the message when the flow is over, whatever happens.
    """

    def __init__(self, choices: Sequence[Choice], *, only: Iterable[Any] | None = None,
                 allow: Callable[[Any, str], Awaitable[str | None]] | None = None,
                 players_only: bool = True, auto_ack: bool = True) -> None:
        super().__init__(timeout=None)
        self.clicks: asyncio.Queue[Click] = asyncio.Queue()
        self.message: Any = None
        self.closed = False
        self._only: set[int] | None = None if only is None else {int(getattr(m, "id", m)) for m in only}
        self._allow = allow
        self._players_only = players_only
        self._auto_ack = auto_ack
        self._buttons: dict[str, discord.ui.Button] = {}
        #: Keys of buttons the flow switched off (see :meth:`relabel`).
        self._switched_off: set[str] = set()
        for choice in choices:
            self.add_choice(choice)

    # -- buttons ------------------------------------------------------------------

    def add_choice(self, choice: Choice) -> discord.ui.Button:
        button: discord.ui.Button = discord.ui.Button(
            style=choice.style, label=choice.label, emoji=choice.emoji, row=choice.row,
            disabled=choice.disabled, custom_id=f"{FLOW_PREFIX}{choice.key}")
        button.callback = functools.partial(self._clicked, choice.key)  # type: ignore[method-assign]
        self._buttons[choice.key] = button
        self.add_item(button)
        return button

    def button(self, key: str) -> discord.ui.Button:
        return self._buttons[key]

    def relabel(self, key: str, label: str | None = None, *, disabled: bool | None = None,
                style: discord.ButtonStyle | None = None) -> None:
        """Change a button; call :meth:`refresh` (or edit the message with ``view=``) to show it.

        A button switched off with ``disabled=True`` stops counting at once:
        the player's screen shows it as clickable until the edit arrives, and a
        second quick click must not reach the flow (a memory rune pressed twice).
        """
        button = self._buttons[key]
        if label is not None:
            button.label = label
        if disabled is not None:
            button.disabled = disabled
            if disabled:
                self._switched_off.add(key)
            else:
                self._switched_off.discard(key)
        if style is not None:
            button.style = style

    def allow_only(self, members: Iterable[Any] | None) -> None:
        """Change who may click from now on (None: everybody)."""
        self._only = None if members is None else {int(getattr(m, "id", m)) for m in members}

    async def _clicked(self, key: str, interaction: Any) -> None:
        user = interaction.user
        if self.closed:
            await handle_stale(interaction)
            return
        if getattr(user, "bot", False):
            return
        if key in self._switched_off:
            await ack(interaction)  # pressed again before the edit that switched it off was shown
            return
        if self._only is not None and user.id not in self._only:
            text = (MSG_NOT_YOURS_NAMED.format(player_id=next(iter(self._only)))
                    if len(self._only) == 1 else MSG_NOT_YOURS)
            await reply(interaction, text)
            return
        if self._players_only and not may_play(interaction.channel, user):
            await reply(interaction, MSG_CANNOT_PLAY)
            return
        if self._allow is not None:
            reason = await self._allow(interaction, key)
            if reason is not None:
                if reason != DROP:
                    await reply(interaction, reason)
                elif not interaction.response.is_done():
                    await ack(interaction)
                return
        if self.closed:  # the flow ended while ``allow`` was asking the database
            await handle_stale(interaction)
            return
        if self._auto_ack:
            await ack(interaction)
            if self.closed:  # the flow ended during the acknowledgement: nobody would read the click
                await reply(interaction, MSG_STALE)
                return
        self.clicks.put_nowait(Click(key, user, interaction))

    # -- the flow's side ----------------------------------------------------------

    async def send(self, destination: Any, content: str | None = None, **kwargs: Any) -> Any:
        """Post the message that carries the buttons (like :func:`altermmo.core.io.send`)."""
        self.message = await io.send(destination, content, view=self, **kwargs)
        if not self.closed:
            _live.add(self.message.id)
        return self.message

    def attach(self, message: Any) -> None:
        """Tell the view which message carries it when it was not sent with :meth:`send`."""
        self.message = message
        if not self.closed:
            _live.add(message.id)

    async def refresh(self, **kwargs: Any) -> bool:
        """Show changed buttons (``content=`` / ``embed=`` may be changed too).  Never raises."""
        if self.message is None or self.closed:
            return False
        try:
            await self.message.edit(view=self, **kwargs)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - cosmetic; usually the message was purged
            return False

    async def next(self, timeout: float | None) -> Click | None:
        """The next accepted click; None after ``timeout`` seconds (None: wait for ever).

        Clicks on a button the flow has switched off meanwhile (:meth:`relabel`)
        are skipped: they were made before the player could see the change.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            try:
                if remaining is not None and remaining <= 0:
                    click = self.clicks.get_nowait()
                else:
                    click = await asyncio.wait_for(self.clicks.get(), remaining)
            except (asyncio.QueueEmpty, asyncio.TimeoutError):
                return None
            if click.key not in self._switched_off:
                return click

    def pending(self) -> list[Click]:
        """Every accepted click not read yet (does not wait)."""
        clicks: list[Click] = []
        while not self.clicks.empty():
            clicks.append(self.clicks.get_nowait())
        return clicks

    async def close(self, *, keep_buttons: bool = False, **kwargs: Any) -> None:
        """Stop listening and take the buttons off the message (idempotent, never raises).

        ``keep_buttons=True`` leaves them visible but disabled; ``content=`` /
        ``embed=`` change the message in the same edit.
        """
        if self.closed:
            return
        self.closed = True
        self.stop()
        if self.message is None:
            return
        _live.discard(self.message.id)
        if keep_buttons:
            for button in self._buttons.values():
                button.disabled = True
            try:
                await self.message.edit(view=self, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
        else:
            await retire(self.message, **kwargs)

    async def __aenter__(self) -> "ClickView":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


def is_live(message_id: int | None) -> bool:
    """True while a flow listens to the buttons under this message."""
    return message_id in _live


class PanelView(GameView):
    """Buttons that live for a while with no coroutine waiting for them.

    For a player's own panel (``$towarzysz`` with its action buttons): subclass
    it, add the buttons with ``@discord.ui.button(custom_id=ui.FLOW_PREFIX +
    "...")`` and post it with :meth:`send`.  After ``timeout`` seconds without
    a click - or when a handler calls :meth:`finish` - the buttons are taken
    off the message.  Give the items ``custom_id``\\ s that start with
    :data:`FLOW_PREFIX`: a click after a restart is then answered as stale.

    ``owner``  the only member who may click (None: everybody).
    """

    def __init__(self, *, owner: Any = None, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.owner_id: int | None = None if owner is None else int(getattr(owner, "id", owner))
        self.message: Any = None

    async def interaction_check(self, interaction: Any, /) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        await reply(interaction, MSG_NOT_YOURS_NAMED.format(player_id=self.owner_id))
        return False

    async def send(self, destination: Any, content: str | None = None, **kwargs: Any) -> Any:
        """Post the panel (like :func:`altermmo.core.io.send`)."""
        self.message = await io.send(destination, content, view=self, **kwargs)
        if not self.is_finished():
            _live.add(self.message.id)
        return self.message

    async def finish(self, **kwargs: Any) -> None:
        """Take the buttons off now (``content=`` / ``embed=`` may be changed too).  Never raises."""
        self.stop()
        if self.message is not None:
            _live.discard(self.message.id)
            await retire(self.message, **kwargs)

    async def on_timeout(self) -> None:
        await self.finish()


async def handle_stale(interaction: Any) -> None:
    """Answer a click on a flow button nobody listens to any more and take the buttons away.

    Safe to call twice for one click (the view and the listener of
    ``altermmo/cogs/ui.py`` may both notice it): only the first call answers.
    """
    interaction_id = getattr(interaction, "id", None)
    if interaction_id is not None:
        if interaction_id in _stale_answered:
            return
        _stale_answered.append(interaction_id)
    message = getattr(interaction, "message", None)
    if not interaction.response.is_done():
        try:
            await interaction.response.edit_message(view=None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - e.g. an ephemeral message that is gone
            log.debug("Could not remove stale buttons.", exc_info=True)
            await retire(message)
    await reply(interaction, MSG_STALE)


# -- "are you sure?" ---------------------------------------------------------------------

@dataclass
class Confirmation:
    """Result of :func:`confirm`; truthy when the member confirmed."""

    confirmed: bool
    #: False when the member answered "no", True when they did not answer at all.
    timed_out: bool = False
    #: The question (its buttons are already gone).
    message: Any = None
    #: Set when the answer was a click (acknowledged; ``reply`` works on it).
    interaction: Any = None
    #: Set when the answer was typed.
    typed: Any = None

    def __bool__(self) -> bool:
        return self.confirmed


async def confirm(bot: Any, channel: Any, member: Any, content: str | None = None, *,
                  timeout: float, yes_label: str = "Potwierdzam", no_label: str = "Anuluj",
                  typed: str | None = "$potwierdzam", **send_kwargs: Any) -> Confirmation:
    """Ask ``member`` a yes/no question with two buttons only they can use.

    The old way - typing ``$potwierdzam`` - keeps working (``typed``; None
    switches it off).  The bot listens before it asks, so a quick answer is
    never lost.  Sending the question may raise, like :func:`io.send`.
    """
    choices = [Choice("yes", yes_label, emoji="✅", style=discord.ButtonStyle.success),
               Choice("no", no_label, emoji="✖️", style=discord.ButtonStyle.secondary)]
    waiter: asyncio.Future | None = None
    if typed is not None:
        wanted = typed.strip().lower()
        waiter = asyncio.ensure_future(io.wait_for_message(
            bot, channel, authors=[member],
            check=lambda message: str(message.content).strip().lower() == wanted, timeout=None))
        await asyncio.sleep(0)  # let the listener register itself
    view = ClickView(choices, only=[member], players_only=False)
    clicker: asyncio.Future | None = None
    try:
        message = await view.send(channel, content, **send_kwargs)
        clicker = asyncio.ensure_future(view.next(timeout))
        done, _ = await asyncio.wait({clicker} | ({waiter} if waiter is not None else set()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if clicker in done:
            click = clicker.result()
            if click is None:
                return Confirmation(False, timed_out=True, message=message)
            return Confirmation(click.key == "yes", message=message, interaction=click.interaction)
        return Confirmation(True, message=message, typed=waiter.result())  # type: ignore[union-attr]
    finally:
        for task in (clicker, waiter):
            if task is not None and not task.done():
                task.cancel()
        await view.close()


# -- choosing from a list ----------------------------------------------------------------

@dataclass(frozen=True)
class Option:
    """One entry of a :func:`pick` menu (Discord allows 25 of them)."""

    value: str
    label: str
    description: str | None = None
    emoji: Any = None
    default: bool = False


@dataclass
class Pick:
    """Result of :func:`pick`; ``value`` is None when nothing was chosen."""

    value: str | None
    timed_out: bool = False
    message: Any = None
    #: The interaction of the choice (acknowledged; ``reply`` works on it).
    interaction: Any = None

    def __bool__(self) -> bool:
        return self.value is not None


class PickView(GameView):
    """A select menu (and an optional "cancel" button) answered by one member."""

    def __init__(self, options: Sequence[Option], *, only: Any = None, placeholder: str | None = None,
                 cancel_label: str | None = "Anuluj") -> None:
        super().__init__(timeout=None)
        self.result: asyncio.Future[tuple[str | None, Any]] = asyncio.get_running_loop().create_future()
        self._only = None if only is None else int(getattr(only, "id", only))
        self.select: discord.ui.Select = discord.ui.Select(
            custom_id=f"{FLOW_PREFIX}pick", placeholder=placeholder, min_values=1, max_values=1,
            options=[discord.SelectOption(label=option.label[:100], value=option.value,
                                          description=(option.description or None) and option.description[:100],
                                          emoji=option.emoji, default=option.default)
                     for option in options[:25]])
        self.select.callback = self._chosen  # type: ignore[method-assign]
        self.add_item(self.select)
        if cancel_label is not None:
            cancel: discord.ui.Button = discord.ui.Button(style=discord.ButtonStyle.secondary,
                                                          label=cancel_label, custom_id=f"{FLOW_PREFIX}pick-cancel")
            cancel.callback = self._cancelled  # type: ignore[method-assign]
            self.add_item(cancel)

    async def _mine(self, interaction: Any) -> bool:
        if self._only is None or interaction.user.id == self._only:
            return True
        await reply(interaction, MSG_NOT_YOURS_NAMED.format(player_id=self._only))
        return False

    async def _chosen(self, interaction: Any) -> None:
        if not await self._mine(interaction):
            return
        await ack(interaction)
        if not self.result.done():
            self.result.set_result((self.select.values[0], interaction))

    async def _cancelled(self, interaction: Any) -> None:
        if not await self._mine(interaction):
            return
        await ack(interaction)
        if not self.result.done():
            self.result.set_result((None, interaction))


async def pick(channel: Any, member: Any, content: str | None = None, *, options: Sequence[Option],
               timeout: float, placeholder: str | None = None, cancel_label: str | None = "Anuluj",
               **send_kwargs: Any) -> Pick:
    """Post a select menu only ``member`` can use and wait for their choice.

    The menu is taken off the message afterwards.  Sending may raise, like :func:`io.send`.
    """
    view = PickView(options, only=member, placeholder=placeholder, cancel_label=cancel_label)
    message = None
    try:
        message = await io.send(channel, content, view=view, **send_kwargs)
        _live.add(message.id)
        try:
            value, interaction = await asyncio.wait_for(asyncio.shield(view.result), timeout)
        except asyncio.TimeoutError:
            return Pick(None, timed_out=True, message=message)
        return Pick(value, message=message, interaction=interaction)
    finally:
        view.stop()
        if not view.result.done():
            view.result.cancel()
        if message is not None:
            _live.discard(message.id)
            await retire(message)


# -- a pop-up window with one text field -------------------------------------------------

class TextModal(discord.ui.Modal):
    """A pop-up window with one text field; see :func:`ask_text`."""

    def __init__(self, title: str, label: str, *, default: str | None = None, placeholder: str | None = None,
                 min_length: int = 1, max_length: int = 32, timeout: float = 300.0) -> None:
        super().__init__(title=title[:45], timeout=timeout)
        self.field: discord.ui.TextInput = discord.ui.TextInput(
            label=label[:45], default=default, placeholder=placeholder, required=True,
            min_length=min_length, max_length=max_length)
        self.add_item(self.field)
        #: What the member typed (stripped); None until the window is submitted.
        self.value: str | None = None
        #: The interaction of the submission - the caller must answer it within 3 seconds.
        self.interaction: Any = None

    async def on_submit(self, interaction: Any) -> None:
        self.value = str(self.field.value).strip()
        self.interaction = interaction

    async def on_error(self, interaction: Any, error: Exception, /) -> None:
        log.error("Pop-up window failed (user %s).", getattr(interaction.user, "id", "?"), exc_info=error)
        await reply(interaction, MSG_FAILED)


async def ask_text(interaction: Any, title: str, label: str, **kwargs: Any) -> TextModal | None:
    """Open a pop-up window as the answer to ``interaction`` (which must not be acknowledged yet).

    Returns the submitted window - read ``.value`` and answer ``.interaction``
    (``ui.reply`` / ``ui.ack``) at once - or None when the member closed it,
    let it time out or it could not be opened.
    """
    modal = TextModal(title, label, **kwargs)
    try:
        await interaction.response.send_modal(modal)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("Could not open a pop-up window (%s: %s).", type(exc).__name__, exc)
        return None
    timed_out = await modal.wait()
    if timed_out or modal.value is None or modal.interaction is None:
        return None
    return modal
