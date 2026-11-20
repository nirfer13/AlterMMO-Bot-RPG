"""In-memory stand-ins for Discord objects, used by the test-suite.

The real :class:`altermmo.bot.AlterBot` is used (never connected): its event
dispatching, ``wait_for`` and listeners are discord.py's own code, only the
objects it talks to (guild, channels, members, messages) are fakes.

Typical use::

    async def test_something(game):            # ``game`` fixture from conftest
        player = game.add_member("Ala")
        game.auto_answer(player)               # types the requested word instantly
        await some_flow(game.scene(player))
        assert game.channel.contains("Brawo")
"""

from __future__ import annotations

import asyncio
import itertools
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import discord

from altermmo.bot import AlterBot
from altermmo.core.scene import Scene
from altermmo.settings import Ids, Settings, Timings

_ids = itertools.count(1_000_000_000_000_000_000)


def next_id() -> int:
    return next(_ids)


def not_found(what: str = "Unknown Message") -> discord.NotFound:
    """The error Discord answers with when a message is gone (HTTP 404)."""
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),  # type: ignore[arg-type]
                            {"code": 10008, "message": what})


class FakeAsset:
    def __init__(self, url: str = "https://cdn.example/avatar.png") -> None:
        self.url = url

    def __str__(self) -> str:
        return self.url


class FakeRole:
    def __init__(self, role_id: int, name: str = "role", guild: "FakeGuild | None" = None) -> None:
        self.id = role_id
        self.name = name
        self.guild = guild
        self.colour = discord.Colour.default()
        self.edits: list[dict[str, Any]] = []

    @property
    def members(self) -> list["FakeMember"]:
        if self.guild is None:
            return []
        return [m for m in self.guild.members if self in m.roles]

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeRole) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __str__(self) -> str:
        return self.name


class FakeMember:
    def __init__(self, name: str, *, member_id: int | None = None, bot: bool = False,
                 guild: "FakeGuild | None" = None) -> None:
        self.id = member_id or next_id()
        self.name = name
        self.display_name = name
        self.global_name = name
        self.nick = None
        self.bot = bot
        self.guild = guild
        self.roles: list[FakeRole] = []
        self.display_avatar = FakeAsset(f"https://cdn.example/{self.id}.png")
        self.avatar = self.display_avatar
        self.dms: list[str] = []
        self.guild_permissions = discord.Permissions.none()

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    async def add_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self.dms.append(content or "")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeMember) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<FakeMember {self.name} {self.id}>"


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.name = "AlterMMO"
        self.members: list[FakeMember] = []
        self.roles: list[FakeRole] = []
        self.channels: list["FakeChannel"] = []

    def get_member(self, member_id: int) -> FakeMember | None:
        return next((m for m in self.members if m.id == int(member_id)), None)

    async def fetch_member(self, member_id: int) -> FakeMember:
        member = self.get_member(member_id)
        if member is None:
            raise LookupError(f"member {member_id} not found")
        return member

    def get_role(self, role_id: int) -> FakeRole | None:
        return next((r for r in self.roles if r.id == int(role_id)), None)

    def get_channel(self, channel_id: int) -> "FakeChannel | None":
        return next((c for c in self.channels if c.id == int(channel_id)), None)

    def add_role(self, role_id: int, name: str = "role") -> FakeRole:
        role = self.get_role(role_id)
        if role is None:
            role = FakeRole(role_id, name, self)
            self.roles.append(role)
        return role

    #: Set to an exception to make :meth:`create_role` fail (no permission, too many roles).
    create_role_error: BaseException | None = None

    async def create_role(self, *, name: str = "new role", **kwargs: Any) -> FakeRole:
        """Like ``discord.Guild.create_role``: a new role with a fresh id; what was asked is kept."""
        if self.create_role_error is not None:
            raise self.create_role_error
        role_id = max([990_000_000_000_000_000, *(role.id for role in self.roles)]) + 1
        role = self.add_role(role_id, name)
        role.created_with = dict(kwargs)  # type: ignore[attr-defined]
        if "colour" in kwargs:
            role.colour = kwargs["colour"]
        return role


@dataclass
class FakeAttachment:
    filename: str = "icon.png"
    content_type: str = "image/png"
    data: bytes = b"\x89PNG"

    async def read(self) -> bytes:
        return self.data


class FakeMessage:
    def __init__(self, channel: "FakeChannel", author: FakeMember, content: str | None = None,
                 *, embed: discord.Embed | None = None, embeds: list[discord.Embed] | None = None,
                 files: list[str] | None = None, attachments: list[FakeAttachment] | None = None) -> None:
        self.id = next_id()
        self.channel = channel
        self.guild = channel.guild
        self.author = author
        self.content = content or ""
        self.embeds = list(embeds or ([embed] if embed is not None else []))
        self.files = list(files or [])
        self.attachments = list(attachments or [])
        self.created_at = datetime.now(timezone.utc)
        self.pinned = False
        self.reactions: list[str] = []
        self.deleted = False
        self.edits: list[dict[str, Any]] = []
        #: The ``discord.ui.View`` (buttons) sent with the message, if any.
        self.view: Any = None

    async def add_reaction(self, emoji: Any) -> None:
        if self.deleted:
            raise not_found()
        self.reactions.append(str(emoji))

    async def delete(self, **kwargs: Any) -> None:
        if self.deleted:
            raise not_found()
        self.deleted = True
        if self in self.channel.messages:
            self.channel.messages.remove(self)

    async def pin(self, **kwargs: Any) -> None:
        if self.deleted:
            raise not_found()
        self.pinned = True

    async def unpin(self, **kwargs: Any) -> None:
        if self.deleted:
            raise not_found()
        self.pinned = False

    async def edit(self, **kwargs: Any) -> "FakeMessage":
        if self.deleted:
            raise not_found()  # as Discord does for a purged message
        self.edits.append(kwargs)
        if "content" in kwargs and kwargs["content"] is not None:
            self.content = kwargs["content"]
        if "embed" in kwargs and kwargs["embed"] is not None:
            self.embeds = [kwargs["embed"]]
        if "embeds" in kwargs and kwargs["embeds"] is not None:
            self.embeds = list(kwargs["embeds"])
        if "view" in kwargs:
            self.view = kwargs["view"]
            self.channel._store_view(self.view, self.id)
        return self

    def button(self, key_or_label: str | None = None) -> Any:
        """A component of the message's view: by ``custom_id`` (suffix) or label; None: the first one."""
        return find_component(self.view, key_or_label)

    @property
    def button_labels(self) -> list[str]:
        """Labels of the buttons under the message that can be clicked right now."""
        if self.view is None:
            return []
        return [str(item.label) for item in self.view.children
                if isinstance(item, discord.ui.Button) and not item.disabled]

    @property
    def text(self) -> str:
        """Content plus embed titles/descriptions - handy for assertions."""
        parts = [self.content]
        for embed in self.embeds:
            parts.append(str(embed.title or ""))
            parts.append(str(embed.description or ""))
            for embed_field in embed.fields:
                parts.append(f"{embed_field.name} {embed_field.value}")
            if embed.footer and embed.footer.text:
                parts.append(str(embed.footer.text))
        return "\n".join(p for p in parts if p)

    def __repr__(self) -> str:
        return f"<FakeMessage {self.author}: {self.content[:40]!r}>"


class _Typing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeChannel:
    def __init__(self, channel_id: int, guild: FakeGuild, bot_user: FakeMember, name: str = "channel") -> None:
        self.id = channel_id
        self.name = name
        self.guild = guild
        self._bot_user = bot_user
        self.messages: list[FakeMessage] = []
        #: Everything the bot ever sent here, including messages deleted since.
        self.sent: list[FakeMessage] = []
        #: Callbacks invoked (synchronously) after every message the *bot* sends.
        self.send_hooks: list[Callable[[FakeMessage], None]] = []
        #: Set to an exception instance to make the next ``fail_sends`` sends raise.
        self.fail_with: BaseException | None = None
        self.fail_sends = 0
        #: Ids of roles that may not write here (the server's "dead hunter" set-up).
        self.silenced_roles: set[int] = set()
        #: The bot (set by ``make_game``): views sent here are registered with it.
        self.client: Any = None
        guild.channels.append(self)

    def _store_view(self, view: Any, message_id: int) -> None:
        """What discord.py does when a message with a view is sent: its timeout starts to run."""
        if view is not None and self.client is not None and not view.is_finished():
            self.client._connection.store_view(view, message_id)

    def permissions_for(self, member: Any) -> discord.Permissions:
        permissions = discord.Permissions.all()
        granted = getattr(member, "guild_permissions", None)
        if granted is None or not granted.administrator:
            permissions.update(administrator=False)  # ordinary members are not administrators
        if any(getattr(role, "id", None) in self.silenced_roles for role in getattr(member, "roles", [])):
            permissions.update(send_messages=False)
        return permissions

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def send(self, content: str | None = None, *, embed: discord.Embed | None = None,
                   embeds: list[discord.Embed] | None = None, file: discord.File | None = None,
                   files: list[discord.File] | None = None, **kwargs: Any) -> FakeMessage:
        names: list[str] = []
        for item in ([file] if file is not None else []) + list(files or []):
            names.append(getattr(item, "filename", str(item)))
            close = getattr(item, "close", None)
            if close is not None:
                close()
        if self.fail_sends > 0 and self.fail_with is not None:
            self.fail_sends -= 1
            raise self.fail_with
        message = FakeMessage(self, self._bot_user, None if content is None else str(content),
                              embed=embed, embeds=embeds, files=names)
        message.view = kwargs.get("view")
        self._store_view(message.view, message.id)
        self.messages.append(message)
        self.sent.append(message)
        for hook in list(self.send_hooks):
            hook(message)
        return message

    def typing(self) -> _Typing:
        return _Typing()

    async def history(self, *, limit: int | None = 100, oldest_first: bool = False,
                      **kwargs: Any) -> AsyncIterator[FakeMessage]:
        ordered = list(self.messages) if oldest_first else list(reversed(self.messages))
        if limit is not None:
            ordered = ordered[:limit]
        for message in ordered:
            yield message

    async def purge(self, *, limit: int | None = 100, check: Callable[[FakeMessage], bool] | None = None,
                    **kwargs: Any) -> list[FakeMessage]:
        candidates = list(reversed(self.messages))
        if limit is not None:
            candidates = candidates[:limit]
        doomed = [m for m in candidates if check is None or check(m)]
        for message in doomed:
            self.messages.remove(message)
            message.deleted = True
        return doomed

    async def fetch_message(self, message_id: int) -> FakeMessage:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise not_found()

    # -- assertion helpers --------------------------------------------------------

    @property
    def bot_messages(self) -> list[FakeMessage]:
        return [m for m in self.messages if m.author is self._bot_user]

    @property
    def transcript(self) -> str:
        return "\n".join(f"{m.author}: {m.text}" for m in self.messages)

    def contains(self, fragment: str) -> bool:
        """True when a message currently on the channel contains ``fragment``."""
        return any(fragment in m.text for m in self.messages)

    def count(self, fragment: str) -> int:
        return sum(1 for m in self.messages if fragment in m.text)

    def ever_sent(self, fragment: str) -> bool:
        """Like :meth:`contains`, but also sees bot messages that were purged since."""
        return any(fragment in m.text for m in self.sent)

    def sent_count(self, fragment: str) -> int:
        return sum(1 for m in self.sent if fragment in m.text)


def find_component(view: Any, key_or_label: str | None = None) -> Any:
    """Item of ``view`` whose ``custom_id`` is / ends with ``key_or_label`` or whose label equals it."""
    assert view is not None, "the message has no buttons"
    items = list(view.children)
    if key_or_label is None:
        return items[0]
    for item in items:
        custom_id = str(getattr(item, "custom_id", "") or "")
        if custom_id == key_or_label or custom_id.endswith(":" + key_or_label):
            return item
    for item in items:
        if getattr(item, "label", None) == key_or_label:
            return item
    raise AssertionError(f"no component {key_or_label!r} among "
                         f"{[(getattr(i, 'custom_id', None), getattr(i, 'label', None)) for i in items]}")


@dataclass
class FakeReply:
    """One answer to an interaction (``response.send_message`` or ``followup.send``)."""

    content: str
    ephemeral: bool
    embeds: list = field(default_factory=list)
    view: Any = None
    files: list = field(default_factory=list)
    #: The channel message a PUBLIC answer became (None for an ephemeral one).
    message: Any = None
    #: ``allowed_mentions=`` the answer was sent with (None: Discord's default - everybody named is pinged).
    allowed_mentions: Any = None

    @property
    def text(self) -> str:
        parts = [self.content]
        for embed in self.embeds:
            parts += [str(embed.title or ""), str(embed.description or "")]
            parts += [f"{f.name} {f.value}" for f in embed.fields]
            if embed.footer and embed.footer.text:
                parts.append(str(embed.footer.text))
        return "\n".join(p for p in parts if p)


def _reply_from(content: Any, kwargs: dict[str, Any]) -> FakeReply:
    embeds = list(kwargs.get("embeds") or ([kwargs["embed"]] if kwargs.get("embed") is not None else []))
    names = []
    for item in ([kwargs["file"]] if kwargs.get("file") is not None else []) + list(kwargs.get("files") or []):
        names.append(getattr(item, "filename", str(item)))
        close = getattr(item, "close", None)
        if close is not None:
            close()
    return FakeReply("" if content is None else str(content), bool(kwargs.get("ephemeral", False)),
                     embeds, kwargs.get("view"), names, allowed_mentions=kwargs.get("allowed_mentions"))


class FakeResponse:
    """``interaction.response``: the first (and only) direct answer to a click."""

    def __init__(self, interaction: "FakeInteraction") -> None:
        self._interaction = interaction
        self._done = False
        self.deferred = False
        self.modal: Any = None
        #: Set to an exception to make every answer fail (Discord is down).
        self.fail_with: BaseException | None = None

    def is_done(self) -> bool:
        return self._done

    def _answer(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        assert not self._done, "the interaction was answered twice"
        self._done = True

    async def send_message(self, content: Any = None, **kwargs: Any) -> None:
        self._answer()
        self._interaction._record(_reply_from(content, kwargs))

    async def defer(self, **kwargs: Any) -> None:
        self._answer()
        self.deferred = True

    async def edit_message(self, **kwargs: Any) -> None:
        self._answer()
        if self._interaction.message is not None:
            await self._interaction.message.edit(**kwargs)

    async def send_modal(self, modal: Any) -> None:
        self._answer()
        self.modal = modal


class FakeFollowup:
    def __init__(self, interaction: "FakeInteraction") -> None:
        self._interaction = interaction

    async def send(self, content: Any = None, **kwargs: Any) -> FakeReply:
        if self._interaction.response.fail_with is not None:
            raise self._interaction.response.fail_with
        assert self._interaction.response.is_done(), "followup before the interaction was answered"
        reply = _reply_from(content, kwargs)
        self._interaction._record(reply)
        return reply


class FakeInteraction:
    """A click on a button / a choice in a menu / a submitted pop-up window."""

    def __init__(self, client: Any, user: FakeMember, message: FakeMessage | None = None,
                 channel: "FakeChannel | None" = None, data: dict[str, Any] | None = None,
                 kind: discord.InteractionType = discord.InteractionType.component) -> None:
        self.id = next_id()
        self.client = client
        self.user = user
        self.message = message
        self.channel = channel if channel is not None else (message.channel if message is not None else None)
        self.guild = getattr(self.channel, "guild", None)
        self.data = data or {}
        self.type = kind
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        #: Everything the bot answered, in order.
        self.replies: list[FakeReply] = []

    def _record(self, reply: FakeReply) -> None:
        """Keep an answer; a public one is an ordinary message on the channel, as on Discord."""
        self.replies.append(reply)
        channel = self.channel
        if reply.ephemeral or not hasattr(channel, "messages"):
            return
        message = FakeMessage(channel, channel._bot_user, reply.content or None,
                              embeds=list(reply.embeds) or None, files=list(reply.files))
        message.view = reply.view
        channel._store_view(message.view, message.id)
        channel.messages.append(message)
        channel.sent.append(message)
        reply.message = message
        for hook in list(channel.send_hooks):
            hook(message)

    @property
    def whispers(self) -> list[str]:
        """Texts of the ephemeral answers (only with ``Settings.private_answers``)."""
        return [reply.text for reply in self.replies if reply.ephemeral]

    @property
    def public(self) -> list[str]:
        """Texts of the answers everybody sees, exactly as posted (with the clicker's mention)."""
        return [reply.text for reply in self.replies if not reply.ephemeral]

    @property
    def answers(self) -> list[str]:
        """Texts of all answers, without the clicker's mention a public answer starts with.

        The convenient form for ``== [ui.MSG_BUSY]`` assertions; use :attr:`public` to
        check the exact text on the channel.
        """
        prefix = f"<@{self.user.id}> "
        return [reply.text[len(prefix):] if reply.text.startswith(prefix) else reply.text
                for reply in self.replies]

    @property
    def said(self) -> str:
        """All answers as one text - handy for ``in`` assertions."""
        return "\n".join(reply.text for reply in self.replies)


class GameBot(AlterBot):
    """The real bot class with Discord's cache replaced by fakes."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.fake_user = FakeMember("AlterBot", bot=True)
        self.fake_guild = FakeGuild(settings.ids.guild)
        self.fake_user.guild = self.fake_guild
        self.fake_channels: dict[int, FakeChannel] = {}
        self.owner_log: list[str] = []

    @property
    def user(self) -> FakeMember:  # type: ignore[override]
        return self.fake_user

    def get_guild(self, guild_id: int) -> FakeGuild | None:  # type: ignore[override]
        return self.fake_guild if guild_id == self.fake_guild.id else None

    def get_channel(self, channel_id: int) -> FakeChannel | None:  # type: ignore[override]
        return self.fake_channels.get(channel_id)

    def get_user(self, user_id: int) -> FakeMember | None:  # type: ignore[override]
        return self.fake_guild.get_member(user_id)

    async def fetch_user(self, user_id: int) -> FakeMember:  # type: ignore[override]
        return await self.fake_guild.fetch_member(user_id)

    async def log_to_owner(self, text: str, *, ping: bool = False) -> None:
        self.owner_log.append(text)
        await super().log_to_owner(text, ping=ping)


#: Finds the word a fight prompt asks for: ``wpisz **U N I K**`` -> ``unik``.
_SPACED_WORD = re.compile(r"\*\*(.+?)\*\*")


def requested_word(text: str) -> str | None:
    """Extract the spaced-out word from a fight prompt (``**P A R U J**`` -> ``paruj``)."""
    matches = _SPACED_WORD.findall(text)
    for raw in reversed(matches):
        letters = raw.split(" ")
        if len(letters) >= 2 and all(len(letter) == 1 for letter in letters):
            return "".join(letters).lower()
    return None


@dataclass
class Game:
    """Everything a flow test needs, bundled (see the ``game`` fixture)."""

    bot: GameBot
    guild: FakeGuild
    channel: FakeChannel          # the boss channel
    log_channel: FakeChannel
    announce_channel: FakeChannel
    notice_channel: FakeChannel
    _tasks: list[asyncio.Task] = field(default_factory=list)

    def add_member(self, name: str, *, roles: list[int] | None = None, member_id: int | None = None) -> FakeMember:
        member = FakeMember(name, member_id=member_id, guild=self.guild)
        for role_id in roles or []:
            member.roles.append(self.guild.add_role(role_id))
        self.guild.members.append(member)
        return member

    def scene(self, author: FakeMember | None = None, content: str = "") -> Scene:
        """A scene on the boss channel, optionally started by ``author``."""
        message = None
        if author is not None:
            message = FakeMessage(self.channel, author, content)
            self.channel.messages.append(message)
        return Scene(bot=self.bot, channel=self.channel, author=author, message=message)  # type: ignore[arg-type]

    def say(self, member: FakeMember, content: str, *, channel: FakeChannel | None = None) -> FakeMessage:
        """A player writes a message *now* (delivered through the real dispatcher)."""
        target = channel or self.channel
        message = FakeMessage(target, member, content)
        target.messages.append(message)
        self.bot.dispatch("message", message)
        return message

    def say_later(self, member: FakeMember, content: str, delay: float,
                  *, channel: FakeChannel | None = None) -> asyncio.Task:
        async def _later() -> None:
            await asyncio.sleep(delay)
            self.say(member, content, channel=channel)

        task = asyncio.ensure_future(_later())
        self._tasks.append(task)
        return task

    def auto_answer(self, member: FakeMember, *, delay: float = 0.0, wrong_on_round: int | None = None,
                    silent_on_round: int | None = None,
                    responder: Callable[[FakeMessage], str | None] | None = None) -> None:
        """Make ``member`` answer every fight prompt the bot posts.

        ``responder`` may override how a prompt is answered (return None to stay
        silent).  ``wrong_on_round`` / ``silent_on_round`` (1-based) inject a
        mistake / a missed turn.
        """
        counter = itertools.count(1)

        def hook(message: FakeMessage) -> None:
            if responder is not None:
                answer = responder(message)
            else:
                if "~~" in message.content:
                    return  # the pet attacked instead of the player
                answer = requested_word(message.content)
            if answer is None:
                return
            round_no = next(counter)
            if silent_on_round == round_no:
                return
            if wrong_on_round == round_no:
                answer = "zlaodpowiedz"
            self.say_later(member, answer, delay)

        self.channel.send_hooks.append(hook)

    async def click(self, message: FakeMessage, member: FakeMember, key_or_label: str | None = None,
                    *, values: list[str] | None = None) -> FakeInteraction:
        """``member`` clicks a button (or chooses ``values`` in a menu) under ``message``.

        Goes the way discord.py dispatches a component: the view's
        ``interaction_check``, the item's callback, ``on_error`` on failure.
        Buttons of a flow that is over are answered by the stale-click handler
        (as ``altermmo/cogs/ui.py`` does), permanent buttons by the view registered
        at start-up.  Returns the interaction with the answers.
        """
        from altermmo.core import ui

        view = message.view
        item = find_component(view, key_or_label)
        if view.is_finished():
            # Not listened to through this message any more: a permanent button is
            # answered by the view registered at start-up, a flow button is stale.
            registered = next((candidate for candidate in self.bot.persistent_views
                               for child in candidate.children
                               if getattr(child, "custom_id", None) == item.custom_id), None)
            if registered is None:
                interaction = FakeInteraction(self.bot, member, message,
                                              data={"custom_id": item.custom_id, "component_type": 2})
                await ui.handle_stale(interaction)
                return interaction
            view = registered
            item = find_component(view, item.custom_id)
        # In production cogs/ui.py sees every click as well and answers the ones on flow
        # buttons nobody registered as live - such a click would be answered twice.
        assert not str(item.custom_id).startswith(ui.FLOW_PREFIX) or ui.is_live(message.id),             "flow buttons on a message that is not registered as live (send it with view.send / panel.send)"
        data: dict[str, Any] = {"custom_id": item.custom_id,
                                "component_type": 3 if values is not None else 2}
        if values is not None:
            data["values"] = list(values)
        interaction = FakeInteraction(self.bot, member, message, data=data)
        try:
            item._refresh_state(interaction, data)
            if await view.interaction_check(interaction):
                await item.callback(interaction)
        except Exception as exc:  # noqa: BLE001 - exactly what discord.py does
            await view.on_error(interaction, exc, item)
        return interaction

    def click_later(self, message: FakeMessage, member: FakeMember, key_or_label: str | None = None,
                    delay: float = 0.0, *, values: list[str] | None = None) -> asyncio.Task:
        async def _later() -> FakeInteraction:
            await asyncio.sleep(delay)
            return await self.click(message, member, key_or_label, values=values)

        task = asyncio.ensure_future(_later())
        self._tasks.append(task)
        return task

    def auto_click(self, member: FakeMember, key_or_label: str | None = None, *, delay: float = 0.0,
                   values: list[str] | None = None, times: int | None = 1,
                   when: Callable[[FakeMessage], bool] | None = None) -> list[asyncio.Task]:
        """Make ``member`` click as soon as the bot posts a message with a matching component.

        ``times`` limits how many messages are clicked (None: every one);
        ``when`` filters the messages.  Returns the list the click tasks are
        appended to (await them to read the interactions).
        """
        clicks: list[asyncio.Task] = []
        remaining = [times]

        def hook(message: FakeMessage) -> None:
            if message.view is None or (remaining[0] is not None and remaining[0] <= 0):
                return
            if when is not None and not when(message):
                return
            try:
                find_component(message.view, key_or_label)
            except AssertionError:
                return
            if remaining[0] is not None:
                remaining[0] -= 1
            clicks.append(self.click_later(message, member, key_or_label, delay, values=values))

        self.channel.send_hooks.append(hook)
        return clicks

    async def submit_modal(self, interaction: FakeInteraction, text: str,
                           member: FakeMember | None = None) -> FakeInteraction:
        """Fill in and submit the pop-up window that was opened as the answer to ``interaction``."""
        modal = interaction.response.modal
        assert modal is not None, "no pop-up window was opened"
        submitted = FakeInteraction(self.bot, member or interaction.user, interaction.message,
                                    channel=interaction.channel, kind=discord.InteractionType.modal_submit)
        field_item = next(item for item in modal.children if isinstance(item, discord.ui.TextInput))
        field_item._value = text
        try:
            if await modal.interaction_check(submitted):
                await modal.on_submit(submitted)
        except Exception as exc:  # noqa: BLE001 - exactly what discord.py does
            await modal.on_error(submitted, exc)
        else:
            modal.stop()
        return submitted

    def last_with_buttons(self, channel: FakeChannel | None = None) -> FakeMessage | None:
        """The newest message on the channel that carries (live) buttons."""
        for message in reversed((channel or self.channel).messages):
            if message.view is not None and not message.view.is_finished():
                return message
        return None

    async def wait_for_buttons(self, key_or_label: str | None = None, *, timeout: float = 3.0,
                               channel: FakeChannel | None = None) -> FakeMessage:
        """Wait until a message with live buttons (with ``key_or_label`` when given) is on the channel."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for message in reversed((channel or self.channel).messages):
                if message.view is None or message.view.is_finished():
                    continue
                if key_or_label is None:
                    return message
                try:
                    find_component(message.view, key_or_label)
                except AssertionError:
                    continue
                return message
            assert asyncio.get_running_loop().time() < deadline, f"no buttons {key_or_label!r} appeared"
            await asyncio.sleep(0.01)

    async def settle(self) -> None:
        """Let scheduled player messages and listeners run."""
        for _ in range(5):
            await asyncio.sleep(0)

    async def cleanup(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def make_game(tmp_path, *, debug: bool = True, database_url: str = "") -> Game:
    """Build a ready-to-use :class:`Game` (call from an async test/fixture)."""
    ids = Ids()
    settings = Settings(
        token="test-token",
        database_url=database_url,
        debug=debug,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        ids=ids,
        timings=Timings(
            respawn_min=1, respawn_max=2, respawn_multiplier=1, post_fight_pause=0.01,
            pre_spawn_min=0.01, pre_spawn_max=0.02, event_gap_min=0.01, event_gap_max=0.02,
        ),
    )
    bot = GameBot(settings)
    await bot._async_setup_hook()  # binds the running loop; no network involved
    bot._ready.set()               # background loops start with wait_until_ready()
    bot.net.attach(bot)
    bot.net.mark_connected()

    guild = bot.fake_guild
    guild.members.append(bot.fake_user)
    channels = {}
    for attr in ("boss_channel", "log_channel", "announce_channel", "notice_channel"):
        channel_id = getattr(ids, attr)
        channels[attr] = FakeChannel(channel_id, guild, bot.fake_user, name=attr)
        channels[attr].client = bot
        bot.fake_channels[channel_id] = channels[attr]
    for role_attr in ("hunters_role", "boss_slayer_role", "dead_hunter_role", "mythic_slayer_role"):
        guild.add_role(getattr(ids, role_attr), role_attr)

    # The permanent first message of the boss channel (never purged).
    pinned = FakeMessage(channels["boss_channel"], bot.fake_user, "Zasady gry")
    channels["boss_channel"].messages.append(pinned)

    return Game(
        bot=bot,
        guild=guild,
        channel=channels["boss_channel"],
        log_channel=channels["log_channel"],
        announce_channel=channels["announce_channel"],
        notice_channel=channels["notice_channel"],
    )
