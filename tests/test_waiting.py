"""The "🐾 Nadchodzi towarzysz..." notice while a slow pet card is drawn (core/waiting.py).

Owner (2026-11-22): show that the companion is coming, with animated dots, when its card is
heavy and drawn for the first time - i.e. whenever drawing it is slow; a card from the cache
or a still card never shows it.  The test-suite's autouse fixture (conftest.py) puts the
delay out of reach; the tests here bring it back with the ``quick`` fixture.
"""

from __future__ import annotations

import ast
import asyncio
import types
from pathlib import Path

import discord
import pytest

from altermmo.core import challenge, waiting
from altermmo.features import album, hub, pets

from fakes import FakeInteraction
from test_album_ui import cogs, page, three  # noqa: F401 - the album's fixture
from test_album_ui import show as show_album
from test_pets import add_owner, add_pet, run, until
from test_pets import plain_cards  # noqa: F401 - autouse: instant plain cards unless a test says otherwise
from test_ui_pets import cog, show, start_click  # noqa: F401 - the pets' cog fixture

COMING = waiting.MSG_COMING
DELAY, STEP = 0.05, 0.1
#: Windows' clock ticks every ~16 ms: a measured interval may look that much shorter.
CLOCK_SLACK = 0.03


@pytest.fixture
def quick(monkeypatch):
    """The notice comes after 50 ms and its dots move every 100 ms."""
    monkeypatch.setattr(waiting, "NOTICE_DELAY", DELAY)
    monkeypatch.setattr(waiting, "NOTICE_STEP", STEP)
    monkeypatch.setattr(waiting, "DELETE_RETRY_PAUSE", 0.01)


@pytest.fixture
async def no_leaks():
    """Nothing the notice started may outlive the test's waits."""
    before = asyncio.all_tasks()
    yield
    current = asyncio.current_task()
    left = {task for task in asyncio.all_tasks() - before if task is not current and not task.done()}
    assert not [task for task in left if task.get_name().startswith("waiting-notice")], left
    assert not waiting._tasks and not waiting._notices


def failure() -> discord.HTTPException:
    return discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"), "test: refused")


class Note:
    """A message of :class:`Channel`."""

    def __init__(self, channel: "Channel", content: str) -> None:
        self.channel, self.content = channel, content
        self.id = len(channel.sent) + 1
        self.deleted = False
        self.delete_attempts = 0

    async def edit(self, *, content: str) -> "Note":
        self.channel.log.append(("edit", content, asyncio.get_running_loop().time()))
        if self.channel.fail_edits:
            raise failure()
        self.content = content
        return self

    async def delete(self) -> None:
        self.delete_attempts += 1
        self.channel.log.append(("delete", self.content, asyncio.get_running_loop().time()))
        if self.channel.fail_deletes > 0:
            self.channel.fail_deletes -= 1
            raise failure()
        self.deleted = True


class Channel:
    """Just enough of a text channel: records every send / edit / delete, can be told to fail."""

    def __init__(self, channel_id: int = 4242) -> None:
        self.id = channel_id
        self.sent: list[Note] = []
        self.log: list[tuple] = []
        self.mentions: list = []
        self.fail_sends = False
        self.fail_edits = False
        self.fail_deletes = 0

    async def send(self, content: str, *, allowed_mentions=None) -> Note:
        self.log.append(("send", content, asyncio.get_running_loop().time()))
        self.mentions.append(allowed_mentions)
        if self.fail_sends:
            raise failure()
        note = Note(self, content)
        self.sent.append(note)
        return note

    def edits(self) -> list[str]:
        return [content for kind, content, _ in self.log if kind == "edit"]


def released():
    """A piece of work that ends when the returned event is set (a card in the worker thread)."""
    go = asyncio.Event()

    async def work(result="card.png"):
        await go.wait()
        return result

    return go, work


# -- the helper alone ------------------------------------------------------------------------------

async def test_fast_work_never_shows_the_notice(quick, no_leaks):
    channel = Channel()
    for _ in range(20):
        assert await waiting.while_slow(channel, asyncio.sleep(0, "card")) == "card"   # (a cached card)
    async with waiting.notice(channel) as wait:
        assert await wait.run(asyncio.sleep(0, 1)) == 1 and not wait.shown
        await asyncio.sleep(DELAY * 3)                      # sending the card afterwards: no notice either
    assert channel.log == []


async def test_slow_work_shows_the_notice_and_its_dots_then_takes_it_away(quick, no_leaks):
    channel = Channel()
    go, work = released()
    start = asyncio.get_running_loop().time()
    task = asyncio.create_task(waiting.while_slow(channel, work("pet.gif")))
    await until(lambda: len(channel.edits()) >= 4)
    go.set()
    assert await task == "pet.gif"

    (note,) = channel.sent
    kinds = [kind for kind, _, _ in channel.log]
    assert kinds[0] == "send" and kinds[-1] == "delete" and note.deleted
    assert channel.log[0][1] == COMING + "." and COMING == "🐾 Nadchodzi towarzysz"
    assert channel.edits()[:4] == [COMING + "..", COMING + "...", COMING + ".", COMING + ".."]
    assert channel.log[0][2] - start >= DELAY - CLOCK_SLACK                       # not before the delay
    times = [when for kind, _, when in channel.log if kind in ("send", "edit")]
    assert all(later - earlier >= STEP - CLOCK_SLACK for earlier, later in zip(times, times[1:]))
    assert channel.mentions[0] is not None and channel.mentions[0].users is False  # pings nobody
    assert channel.mentions[0].everyone is False and channel.mentions[0].roles is False


def test_production_pace():
    """A still or cached card (~0.2 s) never shows it.  The dots use at most half of a channel's
    edit budget (~5 per 5 s, shared by every message edit there: the album page, the pet panel,
    the hunt roster), and stop moving after about a dozen seconds (a render that hangs)."""
    tree = ast.parse(Path(waiting.__file__).read_text(encoding="utf-8"))
    values = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in ("NOTICE_DELAY", "NOTICE_STEP", "NOTICE_MAX_EDITS")}
    assert 0.5 <= values["NOTICE_DELAY"] <= 1.5 and values["NOTICE_STEP"] >= 2.0
    assert 3 <= values["NOTICE_MAX_EDITS"] and values["NOTICE_MAX_EDITS"] * values["NOTICE_STEP"] <= 15
    assert waiting.NOTICE_DELAY >= 3600                                          # (the suite keeps it away)
    assert [waiting.text(step) for step in range(4)] == [COMING + ".", COMING + "..", COMING + "...",
                                                         COMING + "."]


async def test_work_that_raises_takes_the_notice_away_and_the_error_passes(quick, no_leaks):
    channel = Channel()

    async def broken():
        await until(lambda: channel.sent)
        raise LookupError("test: the card cannot be drawn")

    with pytest.raises(LookupError, match="cannot be drawn"):
        await waiting.while_slow(channel, broken())
    assert channel.sent[0].deleted


async def test_an_error_after_the_work_also_takes_the_notice_away(quick, no_leaks):
    channel = Channel()
    go, work = released()
    with pytest.raises(OSError):
        async with waiting.notice(channel) as wait:
            task = asyncio.create_task(wait.run(work()))
            await until(lambda: channel.sent)
            go.set()
            await task
            raise OSError("test: sending the pet failed")
    assert channel.sent[0].deleted


async def test_cancelling_the_caller_takes_the_notice_away(quick, no_leaks):
    channel = Channel()
    _go, work = released()
    task = asyncio.create_task(waiting.while_slow(channel, work()))
    await until(lambda: len(channel.edits()) >= 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and channel.sent[0].deleted


async def test_a_notice_that_cannot_be_posted_changes_nothing(quick, no_leaks):
    channel = Channel()
    channel.fail_sends = True
    go, work = released()
    task = asyncio.create_task(waiting.while_slow(channel, work("pet.png")))
    await until(lambda: channel.log)                                             # it was tried
    await asyncio.sleep(STEP * 2)
    go.set()
    assert await task == "pet.png"
    assert [kind for kind, _, _ in channel.log] == ["send"]                       # no edits, no retries


async def test_a_failed_edit_stops_the_dots_but_the_notice_still_goes(quick, no_leaks):
    channel = Channel()
    channel.fail_edits = True
    go, work = released()
    task = asyncio.create_task(waiting.while_slow(channel, work()))
    await until(lambda: channel.edits())
    await asyncio.sleep(STEP * 3)                                                # three more steps
    go.set()
    await task
    assert len(channel.edits()) == 1 and channel.sent[0].deleted


async def test_work_that_hangs_stops_the_dots_after_a_while(quick, no_leaks, monkeypatch):
    """A render that never ends must not edit the channel forever: after NOTICE_MAX_EDITS frames
    the last one stays, and the notice still goes when the wait ends."""
    monkeypatch.setattr(waiting, "NOTICE_MAX_EDITS", 3)
    channel = Channel()
    go, work = released()
    task = asyncio.create_task(waiting.while_slow(channel, work()))
    await until(lambda: len(channel.edits()) >= 3)
    await asyncio.sleep(STEP * 5)                                                # five more steps
    assert channel.edits() == [COMING + "..", COMING + "...", COMING + "."]
    assert not channel.sent[0].deleted
    go.set()
    await task
    assert channel.sent[0].deleted and len(channel.edits()) == 3


@pytest.mark.parametrize("failures, deleted", [(1, True), (5, False)])
async def test_a_failed_delete_is_tried_once_more_then_left(quick, no_leaks, failures, deleted):
    channel = Channel()
    channel.fail_deletes = failures
    go, work = released()
    task = asyncio.create_task(waiting.while_slow(channel, work("pet.png")))
    await until(lambda: channel.sent)
    go.set()
    assert await task == "pet.png"                                               # never an error
    assert channel.sent[0].delete_attempts == 2 and channel.sent[0].deleted is deleted


async def test_nothing_is_posted_during_a_typed_fight(quick, no_leaks):
    channel = Channel()
    go, work = released()
    with challenge.round_running(channel):
        task = asyncio.create_task(waiting.while_slow(channel, work()))
        await asyncio.sleep(DELAY + STEP * 2)
        go.set()
        await task
    assert channel.log == []


async def test_a_fight_that_starts_takes_the_notice_away(quick, no_leaks):
    channel = Channel()
    go, work = released()
    task = asyncio.create_task(waiting.while_slow(channel, work()))
    await until(lambda: channel.sent)
    with challenge.round_running(channel):
        edits = len(channel.edits())
        await until(lambda: channel.sent[0].deleted)
        await asyncio.sleep(STEP * 2)
        assert len(channel.edits()) == edits                                     # no frame while it runs
        go.set()
        await task
    assert len(channel.sent) == 1


async def test_two_players_waiting_on_one_channel_share_one_notice(quick, no_leaks):
    channel = Channel()
    first_go, first = released()
    second_go, second = released()
    one = asyncio.create_task(waiting.while_slow(channel, first("a")))
    await until(lambda: channel.sent)
    two = asyncio.create_task(waiting.while_slow(channel, second("b")))
    await until(lambda: waiting._notices and next(iter(waiting._notices.values())).users == 2)
    first_go.set()
    assert await one == "a"
    await asyncio.sleep(STEP * 2)
    assert len(channel.sent) == 1 and not channel.sent[0].deleted               # still waiting for the second
    second_go.set()
    assert await two == "b"
    assert len(channel.sent) == 1 and channel.sent[0].deleted

    # The next wait gets a notice of its own; another channel has its own anyway.
    other = Channel(77)
    third_go, third = released()
    fourth_go, fourth = released()
    three_ = asyncio.create_task(waiting.while_slow(channel, third()))
    four = asyncio.create_task(waiting.while_slow(other, fourth()))
    await until(lambda: len(channel.sent) == 2 and other.sent)
    third_go.set()
    fourth_go.set()
    await asyncio.gather(three_, four)
    assert channel.sent[1].deleted and other.sent[0].deleted


async def test_without_a_channel_the_work_is_simply_awaited(quick, no_leaks):
    go, work = released()
    task = asyncio.create_task(waiting.while_slow(None, work("x")))
    await asyncio.sleep(DELAY * 3)
    go.set()
    assert await task == "x"


# -- the flows -------------------------------------------------------------------------------------

def slow_card(monkeypatch, channel, *, edits: int = 2, error: Exception | None = None):
    """A card that is drawn until the notice has shown ``edits`` frames (then fails with ``error``)."""

    def frames() -> int:
        notices = [m for m in channel.sent if COMING in m.content and not m.deleted]
        return len(notices[-1].edits) if notices else -1

    async def card(source, look, cache_dir, *, max_bytes=None):
        await until(lambda: frames() >= edits)
        if error is not None:
            raise error
        return source

    monkeypatch.setattr(pets.petcards, "card", card)


def watch_notice(channel, moments: list):
    """Note what the channel holds at the moment the notice is deleted."""

    def hook(message) -> None:
        if COMING not in message.content:
            return
        real = message.delete

        async def delete(**kwargs):
            moments.append([m for m in channel.sent if m is not message])
            await real(**kwargs)

        message.delete = delete

    channel.send_hooks.append(hook)


async def pet_owner(game, pet_id: int = 5):
    player = game.add_member("Ala")
    await add_owner(game.bot.db, player.id, pet_id)
    await add_pet(game.bot.db, pet_id, pet_lvl=1, variant="1")
    return player


async def test_towarzysz_with_a_slow_card_shows_the_notice_then_the_pet(cog, game_db, quick, monkeypatch,
                                                                        no_leaks):
    game = game_db
    player = await pet_owner(game)
    slow_card(monkeypatch, game.channel)
    moments: list = []
    watch_notice(game.channel, moments)
    await run(game, player, "$towarzysz")

    notice, shown = game.channel.sent[-2:]
    assert notice.content.startswith(COMING) and notice.deleted and notice not in game.channel.messages
    assert [edit["content"] for edit in notice.edits][:2] == [COMING + "..", COMING + "..."]
    assert isinstance(shown.view, pets.PetPanel) and shown.files == ["1.png"]
    assert shown is game.channel.messages[-1]
    assert moments == [[*game.channel.sent[:-2], shown]]                         # gone once the pet was there


async def test_a_quick_card_shows_no_notice(cog, game_db, monkeypatch, no_leaks):
    game = game_db
    monkeypatch.setattr(waiting, "NOTICE_DELAY", 0.5)                            # a cached card: no await at all
    player = await pet_owner(game)
    await run(game, player, "$towarzysz")
    clicked = FakeInteraction(game.bot, player, channel=game.channel)
    assert await hub.show_display(clicked, hub.show_pet) is True
    assert not game.channel.ever_sent(COMING)
    assert sum(isinstance(m.view, pets.PetPanel) for m in game.channel.sent) == 2


async def test_the_hub_button_and_a_redraw_show_the_notice_too(cog, game_db, quick, monkeypatch, no_leaks):
    game = game_db
    player = await pet_owner(game)
    slow_card(monkeypatch, game.channel, edits=0)
    clicked = FakeInteraction(game.bot, player, channel=game.channel)
    assert await hub.show_display(clicked, hub.show_pet) is True
    panel = game.channel.messages[-1]
    assert isinstance(panel.view, pets.PetPanel) and game.channel.sent_count(COMING) == 1
    assert not game.channel.contains(COMING)

    assert await panel.view.redraw(game.bot) is True                             # (after a rename)
    assert game.channel.sent_count(COMING) == 2 and not game.channel.contains(COMING)
    assert game.channel.messages[-1] is panel


async def test_a_card_that_fails_takes_the_notice_away_and_shows_the_plain_picture(cog, game_db, quick,
                                                                                  monkeypatch, no_leaks):
    game = game_db
    player = await pet_owner(game)
    slow_card(monkeypatch, game.channel, edits=1, error=RuntimeError("test: Pillow gave up"))
    assert await pets.show_pet(game.scene(player), player) is True
    notice, shown = game.channel.sent[-2:]
    assert notice.content.startswith(COMING) and notice.deleted
    assert shown.files == ["1.png"] and shown.embeds[0].image.url == "attachment://1.png"


async def test_no_notice_when_the_pet_is_shown_during_a_typed_fight(cog, game_db, quick, monkeypatch,
                                                                   no_leaks):
    game = game_db
    player = await pet_owner(game)

    async def card(source, look, cache_dir, *, max_bytes=None):
        await asyncio.sleep(DELAY + STEP * 2)
        return source

    monkeypatch.setattr(pets.petcards, "card", card)
    with challenge.round_running(game.channel):
        assert await pets.show_pet(game.scene(player), player) is True
    assert not game.channel.ever_sent(COMING)


async def test_turning_an_album_page_with_a_slow_card_shows_the_notice_under_the_panel(cogs, quick,
                                                                                      monkeypatch, no_leaks):
    game = cogs
    ala = await three(game)
    panel = await show_album(game, ala)
    started, go = asyncio.Event(), asyncio.Event()

    async def slow(source, look, cache_dir, *, max_bytes=None):
        started.set()
        await go.wait()
        return source

    monkeypatch.setattr(pets.petcards, "card", slow)
    moments: list = []

    def hook(message) -> None:
        if COMING in message.content:
            real = message.delete

            async def delete(**kwargs):
                moments.append(page(panel))
                await real(**kwargs)

            message.delete = delete

    game.channel.send_hooks.append(hook)
    _interaction, task = start_click(game, panel, ala, "album:next")
    await asyncio.wait_for(started.wait(), 2)
    await until(lambda: game.channel.contains(COMING))
    notice = game.channel.messages[-1]
    assert notice.content.startswith(COMING) and game.channel.messages.index(panel) < len(game.channel.messages) - 1
    assert not page(panel).endswith("Wpis **1 z 3**")                            # the old page meanwhile
    go.set()
    await asyncio.wait_for(task, 2)
    assert page(panel).endswith("Wpis **1 z 3**") and notice.deleted
    assert len(moments) == 1 and moments[0].endswith("Wpis **1 z 3**")           # gone after the edit
    assert game.channel.messages[-1] is panel


async def test_pages_drawn_in_a_row_share_one_notice(cogs, quick, monkeypatch, no_leaks):
    """A click while a slow page is drawn queues the next page: one notice covers both pages - it
    neither blinks off and on between them nor holds the next page back until it is deleted."""
    game = cogs
    ala = await three(game)
    panel = await show_album(game, ala)
    calls: list[asyncio.Event] = []

    async def slow(source, look, cache_dir, *, max_bytes=None):
        go = asyncio.Event()
        calls.append(go)
        await go.wait()
        return source

    monkeypatch.setattr(pets.petcards, "card", slow)
    deletes: list[int] = []

    def hook(message) -> None:
        if COMING in message.content:
            real = message.delete

            async def delete(**kwargs):
                deletes.append(len(calls))
                await real(**kwargs)

            message.delete = delete

    game.channel.send_hooks.append(hook)
    _first, first = start_click(game, panel, ala, "album:next")
    await until(lambda: calls and game.channel.contains(COMING))
    _second, second = start_click(game, panel, ala, "album:next")
    await until(lambda: len(panel.view._pending) == 1)                          # waits for the first page
    calls[0].set()
    await until(lambda: len(calls) == 2)                                         # the second page is drawn
    assert page(panel).endswith("Wpis **1 z 3**")
    await asyncio.sleep(DELAY * 3)
    assert deletes == [] and game.channel.sent_count(COMING) == 1               # the same notice stands
    assert game.channel.contains(COMING)
    calls[1].set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert page(panel).endswith("Wpis **2 z 3**")
    assert deletes == [2] and game.channel.sent_count(COMING) == 1 and not game.channel.contains(COMING)


async def test_a_click_while_the_notice_is_deleted_is_not_lost(cogs, quick, monkeypatch, no_leaks):
    game = cogs
    ala = await three(game)
    panel = await show_album(game, ala)
    go = asyncio.Event()

    async def slow(source, look, cache_dir, *, max_bytes=None):
        await go.wait()
        return source

    monkeypatch.setattr(pets.petcards, "card", slow)
    deleting, deleted = asyncio.Event(), asyncio.Event()

    def hook(message) -> None:
        if COMING in message.content:
            real = message.delete

            async def delete(**kwargs):
                deleting.set()
                await deleted.wait()                                              # Discord is slow to answer
                await real(**kwargs)

            message.delete = delete

    game.channel.send_hooks.append(hook)
    _first, first = start_click(game, panel, ala, "album:next")
    await until(lambda: game.channel.contains(COMING))
    go.set()
    await asyncio.wait_for(deleting.wait(), 2)
    assert page(panel).endswith("Wpis **1 z 3**")
    _second, second = start_click(game, panel, ala, "album:next")
    await until(lambda: len(panel.view._pending) == 1)
    deleted.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert page(panel).endswith("Wpis **2 z 3**") and not panel.view._pending


async def test_an_album_page_from_the_cache_shows_no_notice(cogs, monkeypatch, no_leaks):
    game = cogs
    monkeypatch.setattr(waiting, "NOTICE_DELAY", 0.5)
    ala = await three(game)
    panel = await show_album(game, ala)
    for _ in range(3):
        await game.click(panel, ala, "Następny")
    assert page(panel).endswith("Wpis **3 z 3**") and not game.channel.ever_sent(COMING)
    assert album.AlbumPanel is type(panel.view)
