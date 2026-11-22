"""Posting the patch notes: the start-up run, ``$patchnotes`` and everything that can go wrong.

Owner's request of 2026-11-21: after an update the bot posts what changed to channel
1093905013480378519, once, in Polish, without spoilers - and as the bot may be started only after
several updates, it looks at what its last message announced and posts the rest.  House rule: what
must happen once happens once (two runs at once, a crash between posting and remembering, a retried
send, ``on_ready`` again after a reconnect).  The notes file, the marker and the message planning
without Discord: test_patchnotes.py.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import discord
import pytest

from altermmo import bot as bot_module
from altermmo import settings as settings_module
from altermmo.cogs import patchnotes as patchnotes_cog
from altermmo.features import patchnotes
from altermmo.settings import Ids, load_settings

from fakes import FakeChannel, FakeMember, FakeMessage, not_found

PATCH_CHANNEL = Ids().patch_notes_channel
BULLET = patchnotes.BULLET
#: Three lines of ~1000 characters: one such entry per message (two would exceed 4096).
BIG = 3


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


class NotesChannel(FakeChannel):
    """The patch-notes channel: records every send attempt and fails sends / history reads on cue."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: Keyword arguments of every send attempt (also the failed ones).
        self.attempts: list[dict] = []
        #: One step per attempt: None = fine, an exception = fails and nothing is posted,
        #: ``("arrived", exc)`` = the message IS posted but the answer is lost (``exc`` is raised),
        #: ``"twice"`` = posted twice, the second one is returned (discord.py's own repeat after a
        #: 504 / connection reset), ``("arrived twice", exc)`` = posted twice and ``exc`` raised.
        self.script: list = []
        #: Raised by every send attempt (after the script) while set.
        self.send_always: BaseException | None = None
        #: Seconds every send takes (to make runs overlap).
        self.send_delay = 0.0
        #: One step per ``history()`` call, then ``history_always``.
        self.history_script: list = []
        self.history_always: BaseException | None = None
        self.history_limits: list = []

    async def send(self, content=None, **kwargs):
        self.attempts.append(kwargs)
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        step = self.script.pop(0) if self.script else self.send_always
        if step == "twice":                       # discord.py repeated it by itself (a 504 / reset)
            await super().send(content, **kwargs)
            return await super().send(content, **kwargs)
        if isinstance(step, tuple) and step[0] == "arrived twice":
            await super().send(content, **kwargs)
            await super().send(content, **kwargs)
            raise step[1]
        if isinstance(step, tuple):
            await super().send(content, **kwargs)
            raise step[1]
        if step is not None:
            raise step
        return await super().send(content, **kwargs)

    async def history(self, *, limit=100, oldest_first=False, **kwargs):
        self.history_limits.append(limit)
        error = self.history_script.pop(0) if self.history_script else self.history_always
        if error is not None:
            raise error
        async for message in super().history(limit=limit, oldest_first=oldest_first, **kwargs):
            yield message


# -- fixtures and helpers ------------------------------------------------------------------------

def quick(monkeypatch) -> None:
    """Almost no waiting between the delivery checks / history reads; a short patience."""
    monkeypatch.setattr(patchnotes, "VERIFY_FIRST_DELAY", 0.005)
    monkeypatch.setattr(patchnotes, "VERIFY_MAX_DELAY", 0.05)
    monkeypatch.setattr(patchnotes, "SEND_PATIENCE", 0.3)
    monkeypatch.setattr(patchnotes, "HISTORY_PATIENCE", 0.3)


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    quick(monkeypatch)


@pytest.fixture
def notes_file(tmp_path, monkeypatch):
    """``notes_file([...])`` writes the developers' file (the real one is never touched)."""
    path = tmp_path / "patchNotes.json"
    monkeypatch.setattr(patchnotes, "NOTES_FILE", path)

    def write(content, *, raw: bool = False) -> None:
        path.write_text(content if raw else json.dumps(content, ensure_ascii=False), encoding="utf-8")

    write.path = path  # type: ignore[attr-defined]
    return write


@pytest.fixture
def channel(game):
    notes = NotesChannel(PATCH_CHANNEL, game.guild, game.bot.fake_user, name="patch_notes")
    notes.client = game.bot
    game.bot.fake_channels[PATCH_CHANNEL] = notes
    return notes


def entry(note_id: int, *, lines: int = 1, size: int = 0, title: str = "", text: str | None = None) -> dict:
    changes = []
    for number in range(1, lines + 1):
        line = text if text is not None else f"Zmiana {note_id}.{number}."
        changes.append(line + (" " + "x" * (size - len(line) - 1) if size else ""))
    return {"id": note_id, "date": "2026-11-21", "title": title, "changes": changes}


def entries(*ids: int, **kwargs) -> list[dict]:
    return [entry(note_id, **kwargs) for note_id in ids]


def patch_messages(channel) -> list:
    """The bot's patch-notes messages ever posted on the channel (also deleted ones), in order."""
    return [message for message in channel.sent
            if message.embeds and message.embeds[0].title == patchnotes.MSG_TITLE]


def footers(channel) -> list[str]:
    return [message.embeds[0].footer.text for message in patch_messages(channel)]


def posted_lines(channel) -> list[str]:
    lines = []
    for message in patch_messages(channel):
        lines += [line[len(BULLET):] for line in message.embeds[0].description.split("\n")
                  if line.startswith(BULLET)]
    return lines


def lines_of(content: list[dict]) -> list[str]:
    return [change for item in content for change in item["changes"]]


def remembered(game):
    return game.bot.store.runtime_get(patchnotes.RUNTIME_KEY)


def lose_local(game) -> None:
    game.bot.store.runtime.path.unlink(missing_ok=True)


def start_tasks() -> list[asyncio.Task]:
    return [task for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__qualname__", "") == "PatchNotesCog._after_start"]


async def run(game, member, content: str, *, channel=None) -> FakeMessage:
    """``member`` types a command; it goes through discord.py's real command machinery."""
    channel = channel or game.channel
    message = FakeMessage(channel, member, content)
    message.edited_at = None
    message._state = None
    channel.messages.append(message)
    await game.bot.invoke(await game.bot.get_context(message))
    await game.settle()
    return message


def administrator(game, name: str = "Alter") -> FakeMember:
    member = game.add_member(name)
    member.guild_permissions = discord.Permissions(administrator=True)
    return member


async def load_cog(game, *, start: bool = False):
    """Load the cog explicitly (bot.py's EXTENSIONS is not this file's); ``start=False``: no start-up run."""
    if not start:
        game.bot._patch_notes_started = True  # (as if this process had done its start-up already)
    await game.bot.load_extension("altermmo.cogs.patchnotes")
    for item in game.bot.fake_channels.values():
        item.type = discord.ChannelType.text
    await game.settle()
    return game.bot.get_cog("PatchNotes")


# -- the start-up rule: what was announced, what is posted ---------------------------------------

async def test_first_run_posts_every_entry_together_oldest_first(game, channel, notes_file):
    content = [entry(1), entry(2, lines=2, title="Wieża"), entry(3)]
    notes_file(content)
    outcome = await patchnotes.announce(game.bot)
    (message,) = channel.bot_messages
    assert message.content == "" and len(message.embeds) == 1
    embed = message.embeds[0]
    assert embed.title == "📜 Patch notes" and embed.footer.text == "Patch notes nr 1-3"
    assert embed.description == ("📅 **21.11.2026**\n• Zmiana 1.1.\n\n"
                                 "📅 **21.11.2026** - **Wieża**\n• Zmiana 2.1.\n• Zmiana 2.2.\n\n"
                                 "📅 **21.11.2026**\n• Zmiana 3.1.")
    assert outcome.highest is None and outcome.waiting == [1, 2, 3] and outcome.sent == [1, 2, 3]
    assert outcome.messages == 1 and outcome.problems == [] and remembered(game) == 3
    # The marker is looked for once; after the message one more look for copies (none here).
    assert channel.history_limits == [patchnotes.HISTORY_LIMIT, patchnotes.VERIFY_LIMIT]


async def test_nothing_new_posts_nothing(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.highest == 2 and outcome.waiting == [] and outcome.sent == [] and outcome.problems == []
    assert footers(channel) == ["Patch notes nr 1-2"] and len(channel.attempts) == 1


async def test_after_several_updates_only_the_unsent_entries_are_posted(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    content = entries(1, 2, 3, 4)
    notes_file(content)                                                   # two updates later
    outcome = await patchnotes.announce(game.bot)
    assert outcome.highest == 2 and outcome.sent == [3, 4]
    assert footers(channel) == ["Patch notes nr 1-2", "Patch notes nr 3-4"]
    assert posted_lines(channel) == lines_of(content) and remembered(game) == 4


async def test_a_purged_channel_does_not_bring_the_old_notes_back(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    channel.messages.clear()                                              # the owner cleaned the channel
    notes_file(entries(1, 2, 3))
    outcome = await patchnotes.announce(game.bot)
    assert outcome.channel_value is None and outcome.local_value == 2 and outcome.highest == 2
    assert footers(channel) == ["Patch notes nr 1-2", "Patch notes nr 3"] and remembered(game) == 3


async def test_a_lost_data_folder_is_covered_by_the_marker_on_the_channel(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    lose_local(game)
    outcome = await patchnotes.announce(game.bot)                         # nothing new: the number is healed
    assert outcome.channel_value == 2 and outcome.local_value is None and outcome.sent == []
    assert remembered(game) == 2
    lose_local(game)
    notes_file(entries(1, 2, 3))
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [3] and footers(channel) == ["Patch notes nr 1-2", "Patch notes nr 3"]
    assert remembered(game) == 3


async def test_the_higher_of_the_two_numbers_wins(game, channel, notes_file):
    notes_file(entries(1, 2, 3, 4))
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, 3)                 # remembered 3, the channel says 1
    await FakeChannel.send(channel, embed=patchnotes.Post("x", "Patch notes nr 1", (1,)).embed())
    outcome = await patchnotes.announce(game.bot)
    assert (outcome.channel_value, outcome.local_value, outcome.highest) == (1, 3, 3)
    assert footers(channel)[-1] == "Patch notes nr 4"


@pytest.mark.parametrize("garbage", ["3", 2.5, True, -1, [3], {"id": 3}])
async def test_a_garbled_remembered_number_is_ignored(game, channel, notes_file, garbage):
    notes_file(entries(1, 2))
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, garbage)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.local_value is None and outcome.sent == [1, 2] and remembered(game) == 2


async def test_the_last_patch_notes_message_is_found_behind_other_messages(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    lose_local(game)
    ala = game.add_member("Ala")
    for number in range(300):                                            # players chatting, other bot posts
        channel.messages.append(FakeMessage(channel, ala, f"Super {number}!"))
        if number % 50 == 0:
            await FakeChannel.send(channel, "Jakaś inna wiadomość bota")
    notes_file(entries(1, 2, 3))
    outcome = await patchnotes.announce(game.bot)
    assert outcome.channel_value == 2 and outcome.sent == [3]


async def test_beyond_the_history_limit_only_the_remembered_number_helps(game, channel, notes_file, monkeypatch):
    monkeypatch.setattr(patchnotes, "HISTORY_LIMIT", 10)
    notes_file(entries(1))
    await patchnotes.announce(game.bot)
    ala = game.add_member("Ala")
    channel.messages += [FakeMessage(channel, ala, "hej") for _ in range(10)]
    notes_file(entries(1, 2))
    outcome = await patchnotes.announce(game.bot)                         # the number saves the day
    assert outcome.channel_value is None and outcome.highest == 1 and outcome.sent == [2]


async def test_look_alike_messages_of_others_are_never_taken_for_a_marker(game, channel, notes_file):
    ala = game.add_member("Ala")
    other_bot = FakeMember("Horus", bot=True, guild=game.guild)
    quoted = patchnotes.Post("cytat", "Patch notes nr 9", (9,)).embed()
    channel.messages += [
        FakeMessage(channel, ala, embed=quoted),                          # a player's copy of the embed
        FakeMessage(channel, other_bot, embed=quoted),                    # another bot
        FakeMessage(channel, ala, "Patch notes nr 9"),                    # quoted as text
    ]
    await FakeChannel.send(channel, "Patch notes nr 9")                    # the bot itself, but as text
    garbled = patchnotes.Post("x", "Patch notes nr 9 (edytowane)", ()).embed()
    await FakeChannel.send(channel, embed=garbled)
    other_title = patchnotes.Post("x", "Patch notes nr 9", ()).embed()
    other_title.title = "📜 Ogłoszenie"
    await FakeChannel.send(channel, embed=other_title)
    notes_file(entries(1, 2))
    outcome = await patchnotes.announce(game.bot)
    assert outcome.channel_value is None and outcome.highest is None and outcome.sent == [1, 2]


async def test_unreadable_history_falls_back_to_the_remembered_number(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    channel.history_always = forbidden()
    notes_file(entries(1, 2, 3))
    outcome = await patchnotes.announce(game.bot)
    assert outcome.history_error == "Forbidden" and outcome.highest == 2 and outcome.sent == [3]
    assert outcome.problems == [patchnotes.MSG_OWNER_HISTORY_FALLBACK.format(
        channel=f"<#{PATCH_CHANNEL}>", error="Forbidden", local=2)]


async def test_unreadable_history_and_nothing_remembered_posts_nothing_and_tells_the_owner(game, channel,
                                                                                         notes_file):
    notes_file(entries(1, 2))
    channel.history_always = forbidden()
    await patchnotes.start_up(game.bot)
    await patchnotes.start_up(game.bot)                                   # (once per process)
    assert patch_messages(channel) == [] and remembered(game) is None
    assert game.bot.owner_log == [patchnotes.MSG_OWNER_NO_HISTORY.format(channel=f"<#{PATCH_CHANNEL}>",
                                                                         error="Forbidden")]
    # The next start, with the permission back, posts everything.
    channel.history_always = None
    game.bot._patch_notes_started = False                                 # (a new process)
    await patchnotes.start_up(game.bot)
    assert footers(channel) == ["Patch notes nr 1-2"]
    assert game.bot.owner_log[-1] == f"Patch notes: ogłosiłem 2 wpisy (nr 1-2) na kanale <#{PATCH_CHANNEL}>."


async def test_a_bot_that_does_not_know_itself_does_not_guess(game, channel, notes_file):
    notes_file(entries(1, 2))
    game.bot.fake_user = None                                             # (its own messages unrecognisable)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.blind and outcome.sent == [] and channel.attempts == []


async def test_a_missing_channel_is_told_once_and_never_crashes(game, notes_file, monkeypatch):
    notes_file(entries(1))
    calls = []

    async def fetch_channel(channel_id):
        calls.append(channel_id)
        raise not_found("Unknown Channel")

    monkeypatch.setattr(game.bot, "fetch_channel", fetch_channel)
    await patchnotes.start_up(game.bot)
    assert calls == [PATCH_CHANNEL] and remembered(game) is None
    assert game.bot.owner_log == [patchnotes.MSG_OWNER_NO_CHANNEL.format(channel=f"<#{PATCH_CHANNEL}>",
                                                                         error="NotFound")]


async def test_a_channel_not_in_the_cache_is_fetched(game, notes_file, monkeypatch):
    """An archived thread is not in discord.py's cache: the bot asks Discord for it."""
    notes_file(entries(1))
    thread = NotesChannel(PATCH_CHANNEL, game.guild, game.bot.fake_user, name="archived")

    async def fetch_channel(channel_id):
        return thread

    monkeypatch.setattr(game.bot, "fetch_channel", fetch_channel)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and footers(thread) == ["Patch notes nr 1"]


async def test_a_channel_the_bot_cannot_write_to_is_refused(game, notes_file, monkeypatch):
    notes_file(entries(1))

    async def fetch_channel(channel_id):
        return SimpleNamespace(id=channel_id, name="forum")               # e.g. a forum, not a thread

    monkeypatch.setattr(game.bot, "fetch_channel", fetch_channel)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.channel_error == "LookupError" and outcome.sent == []


async def test_no_permission_to_post_remembers_nothing(game, channel, notes_file):
    notes_file(entries(1, 2))
    channel.send_always = forbidden()
    await patchnotes.start_up(game.bot)
    assert patch_messages(channel) == [] and remembered(game) is None and len(channel.attempts) == 1
    assert game.bot.owner_log == [patchnotes.MSG_OWNER_SEND_FAILED.format(
        channel=f"<#{PATCH_CHANNEL}>", error="Forbidden", left="2 wpisy (nr 1-2)")]


# -- failures in the middle of a batch -----------------------------------------------------------

async def test_a_permanent_failure_in_the_middle_keeps_what_was_delivered(game, channel, notes_file):
    content = entries(1, 2, 3, 4, lines=BIG, size=990)
    notes_file(content)
    channel.script = [None, forbidden()]
    outcome = await patchnotes.announce(game.bot)
    assert outcome.messages == 1 and outcome.sent == [1] and outcome.left == [2, 3, 4]
    assert remembered(game) == 1 and footers(channel) == ["Patch notes nr 1"]
    # The next start posts exactly the rest.
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [2, 3, 4] and outcome.messages == 3
    assert footers(channel) == [f"Patch notes nr {number}" for number in (1, 2, 3, 4)]
    assert posted_lines(channel) == lines_of(content) and remembered(game) == 4


async def test_a_network_failure_is_retried_only_after_checking_it_did_not_arrive(game, channel, notes_file):
    content = entries(1, 2, 3, lines=BIG, size=990)
    notes_file(content)
    channel.script = [None, OSError("connection reset"), None]
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1, 2, 3] and outcome.problems == [] and len(channel.attempts) == 4
    assert posted_lines(channel) == lines_of(content) and remembered(game) == 3


@pytest.mark.parametrize("error", [asyncio.TimeoutError(), OSError("reset"), discord.DiscordServerError(
    SimpleNamespace(status=503, reason="Service Unavailable"), "down")])
async def test_a_send_that_arrived_although_it_failed_is_not_posted_again(game, channel, notes_file, error):
    content = entries(1, 2, 3, lines=BIG, size=990)
    notes_file(content)
    channel.script = [None, ("arrived", error)]
    outcome = await patchnotes.announce(game.bot)
    assert len(channel.attempts) == 3                                     # message 2 was not sent twice
    assert outcome.sent == [1, 2, 3] and footers(channel) == ["Patch notes nr 1", "Patch notes nr 2",
                                                              "Patch notes nr 3"]
    assert posted_lines(channel) == lines_of(content) and remembered(game) == 3


async def test_a_lasting_outage_gives_up_and_the_next_start_posts_the_rest(game, channel, notes_file):
    content = entries(1, 2, 3, lines=BIG, size=990)
    notes_file(content)
    channel.script = [None]
    channel.send_always = OSError("no route to host")
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and outcome.left == [2, 3] and outcome.send_error == "OSError"
    assert remembered(game) == 1 and footers(channel) == ["Patch notes nr 1"]
    channel.send_always = None
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [2, 3] and posted_lines(channel) == lines_of(content)


async def test_a_failed_send_that_cannot_be_checked_is_not_repeated_blindly(game, channel, notes_file):
    notes_file(entries(1))
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, 0)                 # (known: nothing announced yet)
    channel.history_always = forbidden()
    channel.script = [("arrived", asyncio.TimeoutError())]
    outcome = await patchnotes.announce(game.bot)
    assert len(channel.attempts) == 1 and outcome.send_error == "TimeoutError" and outcome.left == [1]
    assert len(patch_messages(channel)) == 1                              # (it did arrive: posted once)


async def test_the_check_waits_out_a_moment_of_unreadable_history(game, channel, notes_file):
    notes_file(entries(1))
    channel.script = [OSError("reset")]
    channel.history_script = [None, OSError("still offline"), OSError("still offline")]  # (start-up read ok)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and len(channel.attempts) == 2 and len(patch_messages(channel)) == 1


async def test_an_entry_split_into_parts_and_interrupted_is_posted_again_whole(game, channel, notes_file):
    big = entry(2, lines=12, size=900)
    content = [entry(1), big, entry(3)]
    notes_file(content)
    plan = patchnotes.plan_posts(patchnotes.load_notes())
    parts = [post for post in plan if post.part_of == 2]
    assert len(parts) >= 3
    channel.script = [None, None, forbidden()]                            # the 2nd part of entry 2 fails
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and remembered(game) == 1                  # entry 2 is NOT counted as sent
    assert footers(channel) == ["Patch notes nr 1", patchnotes.FOOTER_TO_BE_CONTINUED]
    outcome = await patchnotes.announce(game.bot)                         # next start: entry 2 again, whole
    assert outcome.sent == [2, 3] and remembered(game) == 3
    first_part = lines_of([big])[:len(posted_lines(channel)) - len(lines_of(content))]
    assert posted_lines(channel) == lines_of([entry(1)]) + first_part + lines_of([big, entry(3)])


async def test_a_crash_between_posting_and_remembering_does_not_post_again(game, channel, notes_file, monkeypatch):
    notes_file(entries(1, 2))

    def broken(key, value):
        raise OSError("disk full")

    monkeypatch.setattr(game.bot.store, "runtime_set", broken)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1, 2] and outcome.remember_error == "OSError"
    assert patchnotes.MSG_OWNER_REMEMBER_FAILED.format(error="OSError") in outcome.problems
    monkeypatch.undo()
    quick(monkeypatch)
    monkeypatch.setattr(patchnotes, "NOTES_FILE", notes_file.path)        # (undone too: not the real file)
    outcome = await patchnotes.announce(game.bot)                         # "the next start"
    assert outcome.sent == [] and footers(channel) == ["Patch notes nr 1-2"] and remembered(game) == 2


# -- copies made by discord.py's own repeats -------------------------------------------------------

def visible(channel) -> list:
    """The bot's patch-notes messages still on the channel, in order."""
    return [message for message in patch_messages(channel) if not message.deleted]


def visible_lines(channel) -> list[str]:
    lines = []
    for message in visible(channel):
        lines += [line[len(BULLET):] for line in message.embeds[0].description.split("\n")
                  if line.startswith(BULLET)]
    return lines


async def test_a_copy_made_by_discord_pys_own_repeat_is_deleted(game, channel, notes_file):
    """discord.py repeats a send by itself after a 500 / 502 / 504 / 524 answer or a connection reset
    (inside ONE ``send`` call); when Discord had created the message already, there are two copies."""
    content = entries(1, 2, 3, lines=BIG, size=990)
    notes_file(content)
    channel.script = [None, "twice", None]
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1, 2, 3] and outcome.problems == [] and len(channel.attempts) == 3
    assert len(patch_messages(channel)) == 4                              # (the copy was posted ...)
    assert [message.embeds[0].footer.text for message in visible(channel)] == [
        "Patch notes nr 1", "Patch notes nr 2", "Patch notes nr 3"]       # (... and deleted)
    first, second = [message for message in patch_messages(channel)
                     if message.embeds[0].footer.text == "Patch notes nr 2"]
    assert not first.deleted and second.deleted                           # the oldest one stays
    assert visible_lines(channel) == lines_of(content) and remembered(game) == 3


async def test_copies_of_a_message_that_arrived_although_it_failed_are_deleted_too(game, channel, notes_file):
    notes_file(entries(1))
    channel.script = [("arrived twice", OSError("reset"))]
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and len(channel.attempts) == 1             # not sent a third time
    assert len(patch_messages(channel)) == 2 and len(visible(channel)) == 1


async def test_a_copy_that_cannot_be_deleted_stays_and_the_run_goes_on(game, channel, notes_file, monkeypatch):
    content = entries(1, 2, lines=BIG, size=990)
    notes_file(content)
    channel.script = ["twice"]

    async def refuse(self, **kwargs):
        raise forbidden()

    monkeypatch.setattr(FakeMessage, "delete", refuse)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1, 2] and outcome.problems == [] and len(visible(channel)) == 3
    assert remembered(game) == 2


async def test_history_unreadable_after_a_send_does_not_fail_the_run(game, channel, notes_file):
    notes_file(entries(1))
    channel.history_script = [None]                                       # the marker is read ...
    channel.history_always = forbidden()                                  # ... then no more
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and outcome.problems == [] and len(visible(channel)) == 1


async def test_identical_parts_of_one_long_entry_are_never_taken_for_copies(game, channel, notes_file):
    """Two parts of one entry may read exactly the same: neither the check after a failed send
    nor the look for copies may take one for the other."""
    big = entry(1, lines=16, text="To samo.", size=900)
    notes_file([big])
    plan = patchnotes.plan_posts(patchnotes.load_notes())
    middle = [post for post in plan if post.footer == patchnotes.FOOTER_TO_BE_CONTINUED][1:]
    assert len(middle) >= 2 and middle[0].description == middle[1].description
    channel.script = [None, None, OSError("reset")]                       # the 3rd part fails
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and outcome.problems == []
    assert len(visible(channel)) == len(plan) and visible_lines(channel) == lines_of([big])


# -- the history and the remembered number (review, second pass) ------------------------------------

async def test_a_short_history_outage_is_waited_out_before_the_local_number_is_trusted(game, channel, notes_file):
    """The channel says 3, the local number is behind (its write was lost): a two-second hiccup must not
    make the bot trust the local number and post entry 3 again."""
    notes_file(entries(1, 2, 3))
    await patchnotes.announce(game.bot)
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, 2)
    channel.history_script = [OSError("reset"), asyncio.TimeoutError(), OSError("reset")]
    outcome = await patchnotes.announce(game.bot)
    assert outcome.history_error is None and outcome.channel_value == 3 and outcome.sent == []
    assert footers(channel) == ["Patch notes nr 1-3"] and remembered(game) == 3


async def test_a_lasting_history_outage_falls_back_to_the_local_number_and_tells(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    notes_file(entries(1, 2, 3))
    channel.history_always = OSError("no route to host")
    outcome = await patchnotes.announce(game.bot)
    assert outcome.history_error == "OSError" and len(channel.history_limits) > 3   # (tried again and again)
    assert outcome.highest == 2 and outcome.problems == [patchnotes.MSG_OWNER_HISTORY_FALLBACK.format(
        channel=f"<#{PATCH_CHANNEL}>", error="OSError", local=2)]


async def test_a_permanent_history_error_falls_back_at_once(game, channel, notes_file):
    notes_file(entries(1, 2))
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, 1)
    channel.history_always = forbidden()
    outcome = await patchnotes.announce(game.bot)
    assert outcome.history_error == "Forbidden" and outcome.sent == [2]
    # One look for the marker (not repeated: no permission is no network hiccup), one for copies.
    assert channel.history_limits == [patchnotes.HISTORY_LIMIT, patchnotes.VERIFY_LIMIT]


async def test_the_newest_patch_notes_message_counts_not_the_highest(game, channel, notes_file):
    """'które zmiany w ostatniej wiadomości zostały wysłane': after ``od`` that message is the state."""
    await FakeChannel.send(channel, embed=patchnotes.Post("x", "Patch notes nr 1-6", (6,)).embed())
    await FakeChannel.send(channel, embed=patchnotes.Post("x", "Patch notes nr 5", (5,)).embed())
    notes_file(entries(1, 2, 3, 4, 5))
    outcome = await patchnotes.announce(game.bot)
    assert outcome.channel_value == 5 and not outcome.beyond and outcome.sent == []


async def test_od_failing_midway_does_not_lower_the_remembered_number(game, channel, notes_file):
    content = entries(1, 2, 3, lines=BIG, size=990)                       # one entry per message
    notes_file(content)
    await patchnotes.announce(game.bot)
    patch_messages(channel)[0].deleted = True                             # the owner removed entry 1
    channel.messages.remove(patch_messages(channel)[0])
    channel.script = [None, forbidden()]                                  # od 1: entry 1 again, then fails
    outcome = await patchnotes.announce(game.bot, from_id=1)
    assert outcome.sent == [1] and outcome.send_error == "Forbidden"
    assert remembered(game) == 3                                          # NOT lowered to 1
    # The next start: the newest message says 1, the local number 3 - nothing is posted again, also
    # when the history then cannot be read at all.
    channel.history_always = OSError("offline")
    outcome = await patchnotes.announce(game.bot)
    assert outcome.highest == 3 and outcome.sent == []
    channel.history_always = None
    outcome = await patchnotes.announce(game.bot)
    assert (outcome.channel_value, outcome.highest, outcome.sent) == (1, 3, [])
    assert footers(channel) == ["Patch notes nr 1", "Patch notes nr 2", "Patch notes nr 3", "Patch notes nr 1"]


async def test_a_number_past_the_end_of_the_file_is_told_and_od_puts_it_right(game, channel, notes_file,
                                                                              monkeypatch):
    notes_file(entries(1, 2, 3, 4, 5, 6))
    await patchnotes.announce(game.bot)                                   # "nr 1-6" on the channel
    notes_file(entries(1, 2, 3, 4, 5))                                    # the code was rolled back
    pings = []
    log_to_owner = game.bot.log_to_owner

    async def record(text, *, ping=False):
        pings.append(ping)
        await log_to_owner(text, ping=ping)

    monkeypatch.setattr(game.bot, "log_to_owner", record)
    await patchnotes.start_up(game.bot)
    beyond = patchnotes.MSG_OWNER_BEYOND.format(highest=6, last=5)
    assert "nr 6" in beyond and "`$patchnotes od 5`" in beyond
    assert game.bot.owner_log == [beyond] and pings == [True]
    # $patchnotes shows it as well.
    outcome = await patchnotes.announce(game.bot)
    assert outcome.beyond and "⚠️ " + beyond in patchnotes.command_answer(outcome)
    # The owner puts it right: entry 5 again, from now on 5 is the announced number.
    outcome = await patchnotes.announce(game.bot, from_id=5)
    assert outcome.sent == [5] and remembered(game) == 5 and not outcome.problems
    notes_file(entries(1, 2, 3, 4, 5, 6))                                 # the developers' new entry 6
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [6] and footers(channel) == ["Patch notes nr 1-6", "Patch notes nr 5", "Patch notes nr 6"]


@pytest.mark.parametrize("garbage", [7, 10 ** 12])
async def test_a_remembered_number_past_the_file_is_never_silent(game, channel, notes_file, garbage):
    notes_file(entries(1, 2, 3, 4, 5))
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, garbage)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.beyond and outcome.problems == [patchnotes.MSG_OWNER_BEYOND.format(highest=garbage, last=5)]
    assert patchnotes.owner_note(outcome) is not None


async def test_a_number_past_an_empty_file_is_told(game, channel, notes_file):
    notes_file([])
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, 3)
    outcome = await patchnotes.announce(game.bot)
    assert outcome.problems == [patchnotes.MSG_OWNER_BEYOND_EMPTY.format(highest=3, last=0)]


# -- exactly once: concurrency and restarts ------------------------------------------------------

async def test_concurrent_runs_post_once(game, channel, notes_file):
    notes_file(entries(1, 2, 3))
    channel.send_delay = 0.05
    outcomes = await asyncio.gather(*(patchnotes.announce(game.bot) for _ in range(3)))
    assert sorted(len(outcome.sent) for outcome in outcomes) == [0, 0, 3]
    assert footers(channel) == ["Patch notes nr 1-3"] and len(channel.attempts) == 1


async def test_two_fast_restarts_post_once(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.start_up(game.bot)
    game.bot._patch_notes_started = False                                 # a second process, same data
    await patchnotes.start_up(game.bot)
    assert footers(channel) == ["Patch notes nr 1-2"]


async def test_start_up_runs_once_per_process_also_when_on_ready_fires_again(game, channel, notes_file):
    notes_file(entries(1))
    await load_cog(game, start=True)
    (task,) = start_tasks() or [None]
    if task is not None:
        await asyncio.wait_for(task, 2)
    assert footers(channel) == ["Patch notes nr 1"]
    notes_file(entries(1, 2))                                             # (a file changed under a running bot)
    for _ in range(2):
        game.bot.dispatch("ready")                                        # reconnects
        await game.settle()
    await patchnotes.start_up(game.bot)
    assert footers(channel) == ["Patch notes nr 1"] and start_tasks() == []


async def test_the_start_up_waits_until_the_bot_is_ready(game, channel, notes_file, monkeypatch):
    notes_file(entries(1))
    ready = asyncio.Event()

    async def wait_until_ready():
        await ready.wait()

    monkeypatch.setattr(game.bot, "wait_until_ready", wait_until_ready)
    await load_cog(game, start=True)
    (task,) = start_tasks()
    await game.settle()
    assert channel.attempts == [] and not task.done()
    ready.set()
    await asyncio.wait_for(task, 2)
    assert task.exception() is None and footers(channel) == ["Patch notes nr 1"]


async def test_unloading_the_cog_cancels_a_waiting_start(game, channel, notes_file, monkeypatch):
    notes_file(entries(1))
    monkeypatch.setattr(game.bot, "wait_until_ready", asyncio.Event().wait)
    await load_cog(game, start=True)
    (task,) = start_tasks()
    await game.bot.unload_extension("altermmo.cogs.patchnotes")
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and channel.attempts == []


async def test_trouble_never_escapes_the_start_up(game, channel, notes_file, monkeypatch, caplog):
    notes_file(entries(1))

    def broken(*args, **kwargs):
        raise RuntimeError("test")

    monkeypatch.setattr(patchnotes, "plan_posts", broken)
    await patchnotes.start_up(game.bot)                                    # (would raise otherwise)
    assert channel.attempts == []
    assert game.bot.owner_log == [patchnotes.MSG_OWNER_UNEXPECTED.format(error="RuntimeError")]


# -- the file, spoilers, mentions, debug mode -------------------------------------------------------

async def test_a_broken_file_posts_nothing_and_tells_the_owner_what_is_wrong(game, channel, notes_file):
    notes_file('[{"id": 1, "date": "2026-11-21", "changes": ["a"]},', raw=True)
    await patchnotes.start_up(game.bot)
    assert channel.attempts == [] and remembered(game) is None
    (note,) = game.bot.owner_log
    assert note.startswith("Patch notes: plik `config/patchNotes.json` jest błędny - to nie jest poprawny JSON "
                           "(linia 1, kolumna")


async def test_a_missing_file_posts_nothing_and_tells_the_owner(game, channel, notes_file):
    await patchnotes.start_up(game.bot)
    assert channel.attempts == [] and game.bot.owner_log == [patchnotes.MSG_OWNER_FILE_BROKEN.format(
        problem="nie ma pliku patchNotes.json")]


async def test_wrong_ids_post_nothing(game, channel, notes_file):
    notes_file([entry(1), entry(3)])
    outcome = await patchnotes.announce(game.bot)
    assert outcome.notes_error == "wpis 2. na liście ma id 3, a powinien mieć 2 (numery idą po kolei od 1, " \
                                  "bez przerw i powtórzeń)"
    assert channel.attempts == []


async def test_an_entry_with_a_spoiler_waits_with_everything_after_it(game, channel, notes_file):
    notes_file([entry(1), entry(2, text="Nowa folia Holo dla towarzyszy."), entry(3)])
    await patchnotes.start_up(game.bot)
    assert footers(channel) == ["Patch notes nr 1"] and remembered(game) == 1
    assert game.bot.owner_log == [
        f"Patch notes: ogłosiłem 1 wpis (nr 1) na kanale <#{PATCH_CHANNEL}>.\n"
        + patchnotes.MSG_OWNER_SPOILER.format(id=2, word="holo", after=patchnotes.MSG_OWNER_SPOILER_AFTER)]
    notes_file([entry(1), entry(2, text="Nowości dla towarzyszy."), entry(3)])  # corrected
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [2, 3] and "Holo" not in "\n".join(posted_lines(channel))


async def test_a_spoiler_in_the_last_entry(game, channel, notes_file):
    notes_file([entry(1), entry(2, text="Szansa na rzadkość: 2%.")])
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and outcome.blocked == (2, "2%") and not outcome.blocked_more
    assert outcome.problems == [patchnotes.MSG_OWNER_SPOILER.format(id=2, word="2%", after="")]


async def test_nobody_is_ever_pinged_in_debug_mode(game, channel, notes_file):
    assert game.bot.debug                                                 # (the test bot runs in debug mode)
    notes_file([entry(1, text="@everyone <@&985071779787730944> <@291836779495948288> @here"), entry(2)])
    channel.script = [("arrived", OSError("reset"))]
    await patchnotes.announce(game.bot)
    assert channel.attempts
    for attempt in channel.attempts:
        mentions = attempt["allowed_mentions"]
        assert isinstance(mentions, discord.AllowedMentions)
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()
        assert not (mentions.everyone or mentions.users or mentions.roles or mentions.replied_user)
    assert all(message.content == "" for message in patch_messages(channel))


# -- the ping of the hunters' role (owner, 2026-11-22: "W patch notesach oznaczaj rolę łowca potworów.") --

HUNTERS = Ids().hunters_role
MENTION = f"<@&{HUNTERS}>"
ONLY_HUNTERS = {"parse": [], "roles": [HUNTERS]}
NOBODY = discord.AllowedMentions.none().to_dict()


@pytest.fixture
def live(game):
    """The bot as it runs for the players (not in debug mode: there it never pings)."""
    game.bot.debug = False
    return game


def contents(channel) -> list[str]:
    return [message.content for message in patch_messages(channel)]


def mentions(channel) -> list[dict]:
    return [attempt["allowed_mentions"].to_dict() for attempt in channel.attempts]


async def test_the_first_message_of_a_run_pings_the_hunters_once(live, channel, notes_file):
    content = entries(1, 2, 3, lines=BIG, size=990)                       # one entry per message
    notes_file(content)
    outcome = await patchnotes.announce(live.bot)
    assert outcome.sent == [1, 2, 3] and outcome.messages == 3 and outcome.problems == []
    assert contents(channel) == [MENTION, "", ""]                          # the later ones: no ping
    assert mentions(channel) == [ONLY_HUNTERS, NOBODY, NOBODY]
    first = channel.attempts[0]["allowed_mentions"]
    assert first.everyone is False and first.users is False and first.replied_user is False
    assert posted_lines(channel) == lines_of(content) and remembered(live) == 3


async def test_the_second_message_of_a_split_entry_does_not_ping(live, channel, notes_file):
    notes_file([entry(1, lines=12, size=900)])
    outcome = await patchnotes.announce(live.bot)
    assert outcome.sent == [1] and outcome.messages >= 3
    assert contents(channel) == [MENTION] + [""] * (outcome.messages - 1)
    assert mentions(channel) == [ONLY_HUNTERS] + [NOBODY] * (outcome.messages - 1)


async def test_a_pinged_marker_message_is_read_back_and_nothing_new_posts_nothing(live, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(live.bot)
    assert contents(channel) == [MENTION]
    lose_local(live)                                                      # only the channel tells now
    outcome = await patchnotes.announce(live.bot)
    assert outcome.channel_value == 2 and outcome.sent == [] and len(channel.attempts) == 1
    notes_file(entries(1, 2, 3))                                          # the next update pings again
    outcome = await patchnotes.announce(live.bot)
    assert outcome.sent == [3] and contents(channel) == [MENTION, MENTION]


async def test_a_mention_of_another_role_is_no_marker_message(live, channel, notes_file):
    other = patchnotes.Post("x", "Patch notes nr 2", (2,)).embed()
    await FakeChannel.send(channel, f"<@&{HUNTERS + 1}>", embed=other)
    await FakeChannel.send(channel, f"{MENTION} Nowości!", embed=other)
    notes_file(entries(1, 2))
    outcome = await patchnotes.announce(live.bot)
    assert outcome.channel_value is None and outcome.sent == [1, 2]


async def test_od_over_entries_announced_before_pings_nobody(live, channel, notes_file):
    notes_file(entries(1, 2, 3))
    await patchnotes.announce(live.bot)
    await load_cog(live)
    await run(live, administrator(live), "$patchnotes od 2")
    assert footers(channel) == ["Patch notes nr 1-3", "Patch notes nr 2-3"]
    assert contents(channel) == [MENTION, ""] and mentions(channel) == [ONLY_HUNTERS, NOBODY]


async def test_od_that_reaches_new_entries_pings(live, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(live.bot)
    notes_file(entries(1, 2, 3))
    outcome = await patchnotes.announce(live.bot, from_id=2)
    assert outcome.sent == [2, 3] and footers(channel) == ["Patch notes nr 1-2", "Patch notes nr 2-3"]
    assert contents(channel) == [MENTION, MENTION] and mentions(channel) == [ONLY_HUNTERS, ONLY_HUNTERS]


async def test_od_that_reaches_new_entries_pings_only_its_first_message(live, channel, notes_file):
    notes_file(entries(1, 2, lines=BIG, size=990))
    await patchnotes.announce(live.bot)
    notes_file(entries(1, 2, 3, lines=BIG, size=990))
    outcome = await patchnotes.announce(live.bot, from_id=1)              # 1 and 2 again, 3 for the first time
    assert outcome.sent == [1, 2, 3]
    assert contents(channel) == [MENTION, "", MENTION, "", ""]


async def test_in_debug_mode_nobody_is_pinged(game, channel, notes_file):
    game.bot.debug = True
    notes_file(entries(1, 2, lines=BIG, size=990))
    await patchnotes.announce(game.bot)
    assert contents(channel) == ["", ""] and mentions(channel) == [NOBODY, NOBODY]


async def test_a_note_text_with_mentions_still_pings_nobody_but_the_hunters(live, channel, notes_file):
    notes_file([entry(1, text="@everyone <@&123456789012345678> <@291836779495948288> @here"), entry(2)])
    channel.script = [("arrived", OSError("reset"))]                      # (the check after a lost answer)
    outcome = await patchnotes.announce(live.bot)
    assert outcome.sent == [1, 2] and len(channel.attempts) == 1          # it did arrive: not sent again
    assert mentions(channel) == [ONLY_HUNTERS]
    (message,) = patch_messages(channel)
    assert message.content == MENTION and "@everyone" in message.embeds[0].description


async def test_a_pinged_copy_made_by_discord_pys_own_repeat_is_deleted(live, channel, notes_file):
    notes_file(entries(1))
    channel.script = ["twice"]
    outcome = await patchnotes.announce(live.bot)
    assert outcome.sent == [1] and len(patch_messages(channel)) == 2 and len(visible(channel)) == 1
    assert visible(channel)[0].content == MENTION


async def test_an_unpinged_twin_is_not_taken_for_the_pinged_message(live, channel, notes_file):
    """The check after a lost answer compares the content too: the same embed without the mention
    (posted by ``od`` a moment ago, say) is another message, so the pinged one is sent again."""
    notes_file([entry(1, lines=12, size=900)])                            # split: its first part is no marker
    plan = patchnotes.plan_posts(patchnotes.load_notes())
    assert plan[0].footer == patchnotes.FOOTER_TO_BE_CONTINUED
    await FakeChannel.send(channel, embed=plan[0].embed())                # the same part, without the mention
    channel.script = [OSError("reset")]                                    # lost, NOT arrived
    outcome = await patchnotes.announce(live.bot)
    assert outcome.sent == [1] and outcome.problems == []
    assert len(channel.attempts) == len(plan) + 1                          # the first part was sent again
    assert contents(channel) == ["", MENTION] + [""] * (len(plan) - 1)


def test_debug_mode_posts_into_the_log_channel_not_the_players_channel(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("ALTERMMO_DEBUG", "1")
    debug = load_settings(env_file=env)
    assert debug.ids.patch_notes_channel == debug.ids.log_channel == 881090112576962560
    monkeypatch.setenv("ALTERMMO_DEBUG", "0")
    production = load_settings(env_file=env)
    assert production.ids.patch_notes_channel == 1093905013480378519 != debug.ids.patch_notes_channel
    assert settings_module._DEBUG_IDS.patch_notes_channel == Ids().log_channel


async def test_in_debug_mode_the_notes_go_to_the_log_channel(game, channel, notes_file, monkeypatch):
    monkeypatch.setattr(game.bot, "ids", settings_module._DEBUG_IDS)
    notes_file(entries(1))
    outcome = await patchnotes.announce(game.bot)
    assert outcome.sent == [1] and channel.attempts == []
    assert [message.embeds[0].footer.text for message in game.log_channel.sent if message.embeds] == [
        "Patch notes nr 1"]


# -- $patchnotes ------------------------------------------------------------------------------------

def answers(target, since: int = 0) -> list[str]:
    return [message.content for message in target.sent[since:] if message.content]


async def test_the_command_is_for_administrators_only(game, channel, notes_file):
    notes_file(entries(1))
    await load_cog(game)
    command = game.bot.get_command("patchnotes")
    assert command.cog_name == "PatchNotes" and command.aliases == []
    assert any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)
    ala = game.add_member("Ala")
    for text in ("$patchnotes", "$patchnotes od 1"):
        await run(game, ala, text)
    assert answers(game.channel) == [] and channel.attempts == [] and remembered(game) is None


async def test_status_and_posting_what_waits(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    notes_file(entries(1, 2, 3, 4))
    await load_cog(game)
    alter = administrator(game)
    await run(game, alter, "$patchnotes", channel=game.announce_channel)  # (any channel)
    assert answers(game.announce_channel) == [
        "Ostatnio ogłoszony wpis: **nr 2** - na kanale i w pamięci bota.\n"
        "Wpisów w pliku: 4.\n"
        "Czeka: 2 wpisy (nr 3-4).\n"
        f"Wysłałem 2 wpisy (nr 3-4) w 1 wiadomości na kanale <#{PATCH_CHANNEL}>."]
    assert footers(channel) == ["Patch notes nr 1-2", "Patch notes nr 3-4"] and remembered(game) == 4
    await run(game, alter, "$patchnotes", channel=game.announce_channel)
    assert answers(game.announce_channel)[-1] == ("Ostatnio ogłoszony wpis: **nr 4** - na kanale i w pamięci "
                                                  "bota.\nWpisów w pliku: 4.\nNic nie czeka na ogłoszenie.")
    assert len(patch_messages(channel)) == 2


async def test_status_says_where_the_number_comes_from(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    await load_cog(game)
    alter = administrator(game)
    channel.messages.clear()
    await run(game, alter, "$patchnotes")
    assert answers(game.channel)[-1].startswith("Ostatnio ogłoszony wpis: **nr 2** - z pamięci bota (na kanale: "
                                                "brak).")
    channel.messages.append(patch_messages(channel)[0])
    lose_local(game)
    await run(game, alter, "$patchnotes")
    assert answers(game.channel)[-1].startswith("Ostatnio ogłoszony wpis: **nr 2** - z ostatniej wiadomości na "
                                                "kanale (pamięć bota: brak).")
    channel.history_always = forbidden()
    await run(game, alter, "$patchnotes")
    answer = answers(game.channel)[-1]
    assert answer.startswith("Ostatnio ogłoszony wpis: **nr 2** - z pamięci bota (na kanale: nie mogę go "
                             "przeczytać).")
    assert "⚠️ Patch notes: nie mogę przeczytać historii kanału" in answer
    lose_local(game)
    await run(game, alter, "$patchnotes")
    answer = answers(game.channel)[-1]
    assert answer.startswith(patchnotes.MSG_STATUS_UNKNOWN) and "nic nie wysłałem" in answer
    assert len(patch_messages(channel)) == 1


async def test_od_posts_again_from_the_given_entry(game, channel, notes_file):
    content = entries(1, 2, 3, 4)
    notes_file(content)
    await patchnotes.announce(game.bot)
    patch_messages(channel)[0].deleted = True
    channel.messages.clear()                                              # the owner removed the message
    await load_cog(game)
    alter = administrator(game)
    await run(game, alter, "$patchnotes od 2")
    assert answers(game.channel)[-1] == (
        "Ostatnio ogłoszony wpis: **nr 4** - z pamięci bota (na kanale: brak).\n"
        "Wpisów w pliku: 4.\n"
        "Wysyłam od wpisu nr 2: ponownie 3 wpisy (nr 2-4).\n"
        f"Wysłałem 3 wpisy (nr 2-4) w 1 wiadomości na kanale <#{PATCH_CHANNEL}>.")
    assert footers(channel) == ["Patch notes nr 1-4", "Patch notes nr 2-4"] and remembered(game) == 4
    await run(game, alter, "$patchnotes OD 4")
    assert footers(channel)[-1] == "Patch notes nr 4" and remembered(game) == 4


async def test_od_sets_the_remembered_number_to_the_highest_sent(game, channel, notes_file):
    notes_file(entries(1, 2, 3))
    game.bot.store.runtime_set(patchnotes.RUNTIME_KEY, 7)                 # (a number from a longer file)
    await load_cog(game)
    await run(game, administrator(game), "$patchnotes od 1")
    assert footers(channel) == ["Patch notes nr 1-3"] and remembered(game) == 3


async def test_od_never_jumps_over_entries_that_were_not_announced(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    notes_file(entries(1, 2, 3, 4, 5))                                    # an update adds 3, 4, 5
    await load_cog(game)
    alter = administrator(game)
    await run(game, alter, "$patchnotes od 4")
    assert answers(game.channel)[-1] == (
        "Ostatnio ogłoszony wpis: **nr 2** - na kanale i w pamięci bota.\n"
        "Wpisów w pliku: 5.\n"
        "Nie przeskoczę wpisów nr 3: nie zostały jeszcze ogłoszone. Wpisz `$patchnotes` (wyśle wszystko, co "
        "czeka) albo `$patchnotes od 3`.")
    assert footers(channel) == ["Patch notes nr 1-2"] and remembered(game) == 2
    await run(game, alter, "$patchnotes od 5")
    assert "Nie przeskoczę wpisów nr 3-4" in answers(game.channel)[-1] and len(patch_messages(channel)) == 1
    # From the next unsent entry: nothing is "again".
    await run(game, alter, "$patchnotes od 3")
    assert "Wysyłam od wpisu nr 3: po raz pierwszy 3 wpisy (nr 3-5)." in answers(game.channel)[-1]
    assert footers(channel) == ["Patch notes nr 1-2", "Patch notes nr 3-5"] and remembered(game) == 5


async def test_od_says_which_entries_go_again_and_which_for_the_first_time(game, channel, notes_file):
    notes_file(entries(1, 2))
    await patchnotes.announce(game.bot)
    notes_file(entries(1, 2, 3))
    await load_cog(game)
    await run(game, administrator(game), "$patchnotes od 2")
    assert ("Wysyłam od wpisu nr 2: ponownie 1 wpis (nr 2), po raz pierwszy 1 wpis (nr 3)."
            in answers(game.channel)[-1])
    assert footers(channel) == ["Patch notes nr 1-2", "Patch notes nr 2-3"] and remembered(game) == 3


async def test_od_when_nothing_is_known_starts_only_at_the_first_entry(game, channel, notes_file):
    notes_file(entries(1, 2))
    channel.history_always = forbidden()
    await load_cog(game)
    alter = administrator(game)
    await run(game, alter, "$patchnotes od 2")
    assert "Nie przeskoczę wpisów nr 1" in answers(game.channel)[-1] and channel.attempts == []
    await run(game, alter, "$patchnotes od 1")
    assert footers(channel) == ["Patch notes nr 1-2"] and remembered(game) == 2


@pytest.mark.parametrize("text, answer", [
    ("$patchnotes od 9", "Nie ma wpisu nr 9 - plik ma wpisy nr 1-2."),
    ("$patchnotes od 0", "Nie ma wpisu nr 0 - plik ma wpisy nr 1-2."),
    ("$patchnotes od", patchnotes_cog.MSG_USAGE),
    ("$patchnotes od dwa", patchnotes_cog.MSG_USAGE),
    ("$patchnotes od -1", patchnotes_cog.MSG_USAGE),
    ("$patchnotes od ２", patchnotes_cog.MSG_USAGE),
    ("$patchnotes 3", patchnotes_cog.MSG_USAGE),
    ("$patchnotes od 2 3", patchnotes_cog.MSG_USAGE),
])
async def test_wrong_use_of_the_command_posts_nothing(game, channel, notes_file, text, answer):
    notes_file(entries(1, 2))
    await load_cog(game)
    await run(game, administrator(game), text)
    assert answers(game.channel)[-1] == answer and channel.attempts == []


async def test_the_command_reports_a_broken_file(game, channel, notes_file):
    notes_file([entry(1, text="")])
    await load_cog(game)
    await run(game, administrator(game), "$patchnotes")
    assert answers(game.channel)[-1] == "⚠️ " + patchnotes.MSG_OWNER_FILE_BROKEN.format(
        problem="wpis nr 1: zmiana 1. jest puste")


async def test_the_command_during_the_start_up_waits_and_posts_nothing_twice(game, channel, notes_file):
    notes_file(entries(1, 2))
    channel.send_delay = 0.1
    await load_cog(game, start=True)
    (task,) = start_tasks()
    await asyncio.sleep(0.02)                                             # the start-up is posting now
    assert patchnotes.busy(game.bot)
    await run(game, administrator(game), "$patchnotes")
    await asyncio.wait_for(task, 2)
    assert answers(game.channel) == [
        patchnotes_cog.MSG_WAIT,
        "Ostatnio ogłoszony wpis: **nr 2** - na kanale i w pamięci bota.\nWpisów w pliku: 2.\n"
        "Nic nie czeka na ogłoszenie."]
    assert footers(channel) == ["Patch notes nr 1-2"] and len(channel.attempts) == 1


def test_the_command_names_do_not_clash_with_the_other_extensions():
    """Checked on the cog classes (nothing is loaded: the other cogs are being changed meanwhile)."""
    from discord.ext import commands

    names: dict[str, list[str]] = {}
    for extension in dict.fromkeys((*bot_module.EXTENSIONS, "altermmo.cogs.patchnotes")):
        try:
            module = importlib.import_module(extension)
        except Exception:  # noqa: BLE001 - another engineer's file mid-change is not this test's business
            continue
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, commands.Cog) and value.__module__ == module.__name__:
                for command in value.__cog_commands__:
                    for name in (command.name, *command.aliases):
                        names.setdefault(name.lower(), []).append(f"{extension}:{command.qualified_name}")
    assert names["patchnotes"] == ["altermmo.cogs.patchnotes:patchnotes"]
