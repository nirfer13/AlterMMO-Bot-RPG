"""features/collector.py and cogs/collector.py: the rank "Kolekcjoner Pupili" of place 1 of the collection ranking.

Owner's request of 2026-11-23: the player at place 1 of the collection ranking gets the role
"Kolekcjoner Pupili", which gives an additional chance in the giveaways of Horus (the other bot finds
the role BY ITS NAME).  Tested here: who holds the role and who loses it; that nothing is touched
while the ranking, the database, the server or the saved state is not there; the role found by its
saved id / taken over by its name / made / not made, a second role of the name that is reported and
never touched, a state file that cannot be written; the announcement that happens exactly once (one
attempt, also against the real ``io.retry``); the one coalescing entry point (a burst is one run,
never two at a time), the waiting while something plays on the boss channel or the boss is about to
arrive, the quick tries after a give / a removal that failed, the grace period of a holder whose
supporter role blinks; what asks for a synchronisation (the album, the member events, the safety
net); ``$kolekcjoner``; and that the rank is no achievement.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from altermmo import bot as bot_module
from altermmo.cogs import help as help_cog
from altermmo.core import challenge, io
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossPhase
from altermmo.features import achievements, album, albumrank, collector, hub, patchnotes, patrons, pets

from fakes import FakeMember, FakeRole
from test_album import own_pet, put
from test_album import sacrifices  # noqa: F401 - the fixture
from test_album_ui import cogs, show, until  # noqa: F401 - the fixture
from test_albumrank import cards, recorded
from test_pets import confirms, run
from test_pets import plain_cards  # noqa: F401 - autouse: pets are sent as their plain pictures

PATRON = patrons.PATRON_ROLES[0]
NAME = "Kolekcjoner Pupili"


@pytest.fixture(autouse=True)
def _production(game):
    """The rank is looked after by the production bot only; the test bot is a debug one by default
    (``fakes.make_game``) - a debug run touches nothing, see ``test_a_debug_run_touches_nothing``."""
    game.bot.debug = False


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a $potwierdzam slot was never released"


# -- helpers --------------------------------------------------------------------------------------

def supporter(game, name: str, **kwargs) -> FakeMember:
    return game.add_member(name, roles=[PATRON], **kwargs)


def rank_roles(game) -> list[FakeRole]:
    return [role for role in game.guild.roles if role.name == NAME]


def rank_role(game) -> FakeRole:
    (role,) = rank_roles(game)
    return role


def holders(game) -> list[str]:
    """Names of everybody who holds a role of the rank's name, sorted."""
    return sorted(member.name for role in rank_roles(game) for member in role.members)


def announcements(game) -> list[str]:
    """Every announcement of a holder the bot ever posted on the game's channels."""
    return [message.content for channel in (game.channel, game.announce_channel, game.notice_channel)
            for message in channel.sent if "zdobywa rangę" in message.text]


def announced_text(member) -> str:
    return collector.MSG_ANNOUNCE.format(player_id=member.id)


def saved_state(bot) -> dict:
    return bot.store.collector.load()


def owner_lines(bot, start: str) -> list[str]:
    return [line for line in bot.owner_log if line.startswith(start)]


def restart(game) -> None:
    """The bot is started anew: what it kept in memory is gone, the files stay."""
    del game.bot._collector


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


class DatabaseDown:
    """A database that cannot be reached at all."""

    async def fetch(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    fetchrow = fetchval = execute = fetch


class Clock:
    """``collector._clock`` under the test's control: time passes only when the test says so.

    ``reads`` counts how often the clock was looked at - a loop that waits for a moment of this
    clock looks again and again (``GRACE_POLL``), so "it looked several times more and still
    waits" can be awaited (:meth:`looked_at`) instead of sleeping and hoping the machine is fast.
    """

    def __init__(self, monkeypatch) -> None:
        self.now = 1000.0
        self.reads = 0
        monkeypatch.setattr(collector, "_clock", self._read)

    def _read(self) -> float:
        self.reads += 1
        return self.now

    async def looked_at(self, times: int = 5) -> bool:
        """Wait until the clock was read ``times`` more times; False when that never happened."""
        target = self.reads + times
        return await until(lambda: self.reads >= target)


def spy(monkeypatch, *, delay: float = 0.0) -> SimpleNamespace:
    """Count the synchronisations and how many of them ran at the same time."""
    seen = SimpleNamespace(calls=0, running=0, most=0, forced=[])
    real = collector._sync

    async def watched(bot, *, force):
        seen.calls += 1
        seen.forced.append(force)
        seen.running += 1
        seen.most = max(seen.most, seen.running)
        try:
            if delay:
                await asyncio.sleep(delay)
            return await real(bot, force=force)
        finally:
            seen.running -= 1

    monkeypatch.setattr(collector, "_sync", watched)
    return seen


@pytest.fixture
def fast(monkeypatch):
    """The loop's waits are short; the safety net is far away unless a test brings it near."""
    monkeypatch.setattr(collector, "SETTLE", 0.03)
    monkeypatch.setattr(collector, "QUIET_POLL", 0.01)
    monkeypatch.setattr(collector, "PERIOD", 3600.0)
    monkeypatch.setattr(collector, "RETRY_SOON", 3600.0)


@pytest_asyncio.fixture
async def loop(game_db, fast):
    """``loop()`` starts the supervised loop (as the cog does); it is cancelled after the test."""
    tasks: list[asyncio.Task] = []

    def start() -> asyncio.Task:
        task = asyncio.ensure_future(collector.run(game_db.bot))
        tasks.append(task)
        return task

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def lead(game, name: str = "Ala", count: int = 3) -> FakeMember:
    """A supporter with ``count`` cards who, synchronised, holds the rank."""
    member = supporter(game, name)
    await cards(game.bot.db, member.id, count)
    outcome = await collector.sync(game.bot)
    assert outcome.status == collector.DONE and outcome.holds and holders(game) == [name]
    return member


# -- the name -------------------------------------------------------------------------------------

def test_the_role_name_is_the_one_horus_knows():
    # Horus's giveaway ($lotto) adds up the weights of a member's roles BY EXACT ROLE NAME from its own
    # ranksConfig.json, where the rank is listed as "Kolekcjoner Pupili": the two must change together.
    assert collector.ROLE_NAME == "Kolekcjoner Pupili"


# -- who holds it ---------------------------------------------------------------------------------

async def test_place_one_gets_the_role_and_is_announced_once(game_db, monkeypatch):
    game = game_db
    db = game.bot.db
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    await cards(db, ala.id, 5)
    await cards(db, ola.id, 2)
    sent = recorded(game, monkeypatch)

    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True, given=True,
                                        announcement=collector.ANNOUNCED)
    role = rank_role(game)
    assert holders(game) == ["Ala"] and role not in ola.roles
    # The role as it was asked for: no permissions, not shown apart, nobody can mention it.
    made = role.created_with
    assert made["permissions"] == discord.Permissions.none() and made["hoist"] is False
    assert made["mentionable"] is False and made["colour"] == discord.Colour(collector.ROLE_COLOUR)
    assert saved_state(game.bot) == {"role_id": role.id, "announced": ala.id}

    ((message, mentions),) = sent                                          # ONE public message, on the boss channel
    assert message.channel is game.channel and message.content == announced_text(ala)
    assert message.content.startswith(f"👑 <@{ala.id}> ") and "**Kolekcjoner Pupili**" in message.content
    assert "giveaway" in message.content and "pierwszego miejsca w rankingu kolekcji" in message.content
    assert [user.id for user in mentions.users] == [ala.id] and mentions.roles is False
    assert mentions.everyone is False
    for word in ("lobby", "%", "<@&", "@everyone", "Ola", str(ola.id)):
        assert word not in message.content, word

    for _ in range(3):                                                     # again and again: nothing more
        again = await collector.sync(game.bot)
        assert again == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True)
    assert len(sent) == 1 and len(rank_roles(game)) == 1 and holders(game) == ["Ala"]


async def test_whoever_overtakes_takes_the_role_and_the_old_holder_loses_it(game_db, monkeypatch):
    game = game_db
    db = game.bot.db
    ala = await lead(game, "Ala", 3)
    ola = supporter(game, "Ola")
    await cards(db, ola.id, 4)
    sent = recorded(game, monkeypatch)

    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, leader_id=ola.id, holds=True, given=True,
                                        removed=(ala.id,), announcement=collector.ANNOUNCED)
    assert holders(game) == ["Ola"] and len(rank_roles(game)) == 1
    ((message, mentions),) = sent
    assert message.content == announced_text(ola) and [user.id for user in mentions.users] == [ola.id]
    assert str(ala.id) not in message.content and "Ala" not in message.content   # the old one: not named
    assert saved_state(game.bot)["announced"] == ola.id
    assert announcements(game) == [announced_text(ala), announced_text(ola)]


async def test_everybody_else_who_has_the_role_loses_it(game_db):
    game = game_db
    ala = await lead(game)
    role = rank_role(game)
    stray = game.add_member("Dziki")                                       # given by hand, not even ranked
    other = supporter(game, "Ola")
    await cards(game.bot.db, other.id, 1)
    stray.roles.append(role)
    other.roles.append(role)
    outcome = await collector.sync(game.bot)
    assert sorted(outcome.removed) == sorted([stray.id, other.id]) and not outcome.given
    assert holders(game) == ["Ala"] and outcome.leader_id == ala.id
    assert announcements(game) == [announced_text(ala)]                    # nothing new to announce


async def test_nobody_ranked_means_nobody_holds_it_and_no_role_is_made(game_db):
    game = game_db
    assert await collector.sync(game.bot) == collector.Outcome(collector.DONE)   # known, and empty
    assert rank_roles(game) == [] and game.bot.owner_log == [] and announcements(game) == []
    assert not game.bot.store.collector.path.exists()

    lone = game.add_member("Bez wsparcia")                                 # an album, but no support
    await cards(game.bot.db, lone.id, 9)
    assert await collector.sync(game.bot) == collector.Outcome(collector.DONE)
    assert rank_roles(game) == []


async def test_a_leader_whose_support_lapses_loses_the_role_and_his_return_is_not_announced_again(game_db):
    game = game_db
    ala = await lead(game)
    patron_role = ala.roles[0]
    ala.roles.remove(patron_role)                                          # the support lapsed: not ranked
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, removed=(ala.id,))
    assert holders(game) == [] and len(rank_roles(game)) == 1             # the role itself stays

    ala.roles.append(patron_role)                                          # ... and is back
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True, given=True)
    assert holders(game) == ["Ala"]
    assert announcements(game) == [announced_text(ala)]                    # nobody else was announced meanwhile


async def test_a_leader_who_left_the_server_makes_room_for_the_next(game_db):
    game = game_db
    ala = await lead(game, "Ala", 5)
    ola = supporter(game, "Ola")
    await cards(game.bot.db, ola.id, 2)
    assert (await collector.sync(game.bot)).changed is False
    game.guild.members.remove(ala)
    outcome = await collector.sync(game.bot)
    assert outcome.leader_id == ola.id and outcome.given and holders(game) == ["Ola"]
    assert announcements(game) == [announced_text(ala), announced_text(ola)]


async def test_there_and_back_again_is_announced_only_when_the_other_one_was(game_db):
    """A -> B -> A: B was announced, so A's return is news.  A back in front before anybody saw B
    with the rank (no synchronisation in between): nothing new."""
    game = game_db
    db = game.bot.db
    ala = await lead(game, "Ala", 3)
    ola = supporter(game, "Ola")
    extra = [await put(db, ola.id, species, 0) for species in album.collection_species()[:5]]
    await collector.sync(game.bot)                                         # Ola (50) leads, announced
    for entry_id in extra:
        assert await album.remove(db, ola.id, entry_id)
    await collector.sync(game.bot)                                         # Ala again: announced again
    assert holders(game) == ["Ala"]
    assert announcements(game) == [announced_text(ala), announced_text(ola), announced_text(ala)]

    blink = [await put(db, ola.id, species, 0) for species in album.collection_species()[:5]]
    for entry_id in blink:                                                 # Ola in front for a moment ...
        assert await album.remove(db, ola.id, entry_id)
    outcome = await collector.sync(game.bot)                               # ... nobody ever saw it
    assert outcome == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True)
    assert len(announcements(game)) == 3


async def test_a_role_that_cannot_be_given_is_told_once_and_nothing_is_announced(game_db, monkeypatch):
    game = game_db
    ala = await lead(game, "Ala", 2)
    ola = supporter(game, "Ola")
    await cards(game.bot.db, ola.id, 5)

    async def refused(*roles, reason=None):
        raise forbidden()

    with monkeypatch.context() as patch:
        patch.setattr(ola, "add_roles", refused)
        for _ in range(3):
            outcome = await collector.sync(game.bot)
            assert outcome.status == collector.DONE and outcome.leader_id == ola.id and not outcome.holds
    assert holders(game) == []                                             # Ala is not first any more
    assert announcements(game) == [announced_text(ala)]
    assert owner_lines(game.bot, "Nie udało się nadać rangi") == [collector.LOG_NOT_GIVEN.format(player_id=ola.id)]
    assert collector.describe(outcome) == collector.MSG_NOT_GIVEN.format(player_id=ola.id)
    assert outcome.unfinished and not outcome.try_soon                     # (the loop tries again soon)
    # The bot cannot know why (io.add_role does not say): the note blames neither side.
    for text in (collector.LOG_NOT_GIVEN, collector.LOG_NOT_REMOVED, collector.MSG_NOT_GIVEN):
        assert "połączeniem" in text and "powyżej roli bota" in text and "restart" not in text, text

    outcome = await collector.sync(game.bot)
    assert outcome.given and outcome.announcement == collector.ANNOUNCED and holders(game) == ["Ola"]
    assert not outcome.unfinished

    # It worked in between: a later failure is news for the owner again.
    ola.roles.remove(rank_role(game))
    with monkeypatch.context() as patch:
        patch.setattr(ola, "add_roles", refused)
        for _ in range(2):
            assert (await collector.sync(game.bot)).unfinished
    assert owner_lines(game.bot, "Nie udało się nadać rangi") == [collector.LOG_NOT_GIVEN.format(player_id=ola.id)] * 2


async def test_a_role_that_cannot_be_taken_away_is_told_once(game_db, monkeypatch):
    game = game_db
    ala = await lead(game, "Ala", 2)
    ola = supporter(game, "Ola")
    await cards(game.bot.db, ola.id, 5)

    async def refused(*roles, reason=None):
        raise forbidden()

    with monkeypatch.context() as patch:
        patch.setattr(ala, "remove_roles", refused)
        for _ in range(2):
            outcome = await collector.sync(game.bot)
        assert outcome.not_removed == (ala.id,) and holders(game) == ["Ala", "Ola"]
        assert outcome.holds and outcome.unfinished and not outcome.try_soon   # (the loop tries again soon)
    told = collector.LOG_NOT_REMOVED.format(player_id=ala.id)
    assert owner_lines(game.bot, "Nie udało się zdjąć rangi") == [told]
    assert collector.MSG_NOT_REMOVED.format(players=f"<@{ala.id}>") in collector.describe(outcome)

    outcome = await collector.sync(game.bot)                                # it works now
    assert outcome.removed == (ala.id,) and not outcome.unfinished and holders(game) == ["Ola"]
    # ... so a later failure is news for the owner again.
    ala.roles.append(rank_role(game))
    with monkeypatch.context() as patch:
        patch.setattr(ala, "remove_roles", refused)
        for _ in range(2):
            assert (await collector.sync(game.bot)).not_removed == (ala.id,)
    assert owner_lines(game.bot, "Nie udało się zdjąć rangi") == [told, told]


# -- nothing is touched when it cannot be known ---------------------------------------------------

async def overtaken(game) -> tuple[FakeMember, FakeMember, dict, int]:
    """Ala holds the rank, Ola has overtaken her and nothing was synchronised since."""
    ala = await lead(game, "Ala", 2)
    ola = supporter(game, "Ola")
    await cards(game.bot.db, ola.id, 6)
    return ala, ola, saved_state(game.bot), len(game.bot.owner_log)


def untouched(game, ala, state) -> bool:
    return (holders(game) == ["Ala"] and saved_state(game.bot) == state
            and announcements(game) == [announced_text(ala)] and len(rank_roles(game)) == 1)


async def test_an_unknown_ranking_touches_nothing(game_db, monkeypatch):
    game = game_db
    ala, ola, state, told = await overtaken(game)

    async def unknown(bot, *, viewer=None):
        return None

    with monkeypatch.context() as patch:
        patch.setattr(albumrank, "ranking", unknown)
        outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.NO_RANKING) and outcome.try_soon
    assert untouched(game, ala, state) and len(game.bot.owner_log) == told
    assert (await collector.sync(game.bot)).leader_id == ola.id and holders(game) == ["Ola"]


async def test_an_incomplete_member_list_that_cannot_be_completed_touches_nothing(game_db, monkeypatch):
    """The real "not known right now" of the ranking: album owners missing from an unchunked server."""
    game = game_db
    ala, ola, state, _ = await overtaken(game)
    game.guild.chunked = False
    monkeypatch.setattr(albumrank, "LOOKUP_LIMIT", 0)
    await cards(game.bot.db, 777_000_000_000_000_001, 1)                   # an owner who is not in the cache
    assert (await collector.sync(game.bot)).status == collector.NO_RANKING
    assert untouched(game, ala, state)


async def test_a_database_that_is_down_touches_nothing(game_db):
    game = game_db
    ala, ola, state, told = await overtaken(game)
    real = game.bot.db
    game.bot.db = DatabaseDown()
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.NO_RANKING, detail="DatabaseUnavailable")
    assert untouched(game, ala, state) and len(game.bot.owner_log) == told
    game.bot.db = real
    assert (await collector.sync(game.bot)).removed == (ala.id,) and holders(game) == ["Ola"]


@pytest.mark.parametrize("outage", ["no guild", "unavailable"])
async def test_a_server_that_is_not_there_touches_nothing(game_db, monkeypatch, outage):
    game = game_db
    ala, ola, state, told = await overtaken(game)
    asked = []
    real = albumrank.ranking

    async def ranking(bot, *, viewer=None):
        asked.append(1)
        return await real(bot, viewer=viewer)

    monkeypatch.setattr(albumrank, "ranking", ranking)
    with monkeypatch.context() as patch:
        if outage == "no guild":
            patch.setattr(game.bot, "get_guild", lambda guild_id: None)
        else:
            patch.setattr(game.guild, "unavailable", True, raising=False)
        outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.NO_GUILD) and outcome.try_soon and asked == []
    assert untouched(game, ala, state) and len(game.bot.owner_log) == told


@pytest.mark.parametrize("broken", ["[1, 2, ", '{"role_id": 99', "[1, 2]"])
async def test_a_saved_state_that_cannot_be_read_touches_nothing_and_the_owner_is_told_once(game_db, broken):
    game = game_db
    ala, ola, state, told = await overtaken(game)
    path = game.bot.store.collector.path
    good = path.read_text(encoding="utf-8")
    path.write_text(broken, encoding="utf-8")
    for _ in range(3):
        assert await collector.sync(game.bot) == collector.Outcome(collector.NO_STATE)
    assert holders(game) == ["Ala"] and len(rank_roles(game)) == 1 and path.read_text(encoding="utf-8") == broken
    assert announcements(game) == [announced_text(ala)]
    note = collector.LOG_FILE_UNREADABLE.format(path=path)
    assert game.bot.owner_log[told:] == [note]
    path.write_text(good, encoding="utf-8")
    assert (await collector.sync(game.bot)).leader_id == ola.id and holders(game) == ["Ola"]

    # Read in between: a file that breaks AGAIN in the same run is told again (once).
    good = path.read_text(encoding="utf-8")
    path.write_text(broken, encoding="utf-8")
    for _ in range(2):
        assert await collector.sync(game.bot) == collector.Outcome(collector.NO_STATE)
    assert game.bot.owner_log[told:] == [note, note] and holders(game) == ["Ola"]
    path.write_text(good, encoding="utf-8")
    assert (await collector.sync(game.bot)).changed is False


async def test_a_debug_run_touches_nothing(game_db, loop, monkeypatch):
    """A debug run plays on the same server with the TEST database: its ranking is not the server's."""
    game = game_db
    ala, ola, state, told = await overtaken(game)
    asked = []
    real = albumrank.ranking

    async def ranking(bot, *, viewer=None):
        asked.append(1)
        return await real(bot, viewer=viewer)

    monkeypatch.setattr(albumrank, "ranking", ranking)
    game.bot.debug = True
    outcome = await collector.sync(game.bot, force=True)
    assert outcome == collector.Outcome(collector.TEST_RUN) and not outcome.try_soon and not outcome.looked
    assert collector.describe(outcome) == collector.MSG_TEST_RUN
    assert (await collector.sync_now(game.bot)).status == collector.TEST_RUN
    loop()                                                                 # the loop: runs, and does nothing
    assert await until(lambda: collector._desk(game.bot).runs == 3)
    assert asked == [] and untouched(game, ala, state) and len(game.bot.owner_log) == told

    restart(game)                                                          # also with no role at all
    await rank_role(game).delete()
    game.bot.store.collector.path.unlink()
    assert (await collector.sync(game.bot, force=True)).status == collector.TEST_RUN
    assert rank_roles(game) == [] and not game.bot.store.collector.path.exists()
    assert len(game.bot.owner_log) == told and asked == []


async def test_a_synchronisation_never_raises(game_db, monkeypatch, caplog):
    game = game_db

    async def broken(bot, *, force):
        raise RuntimeError("test")

    monkeypatch.setattr(collector, "_sync", broken)
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.FAILED, detail="RuntimeError") and outcome.try_soon
    assert "the synchronisation failed" in caplog.text
    assert collector.describe(outcome) == collector.MSG_FAILED.format(detail="RuntimeError")

    async def cancelled(bot, *, force):
        raise asyncio.CancelledError()

    monkeypatch.setattr(collector, "_sync", cancelled)
    with pytest.raises(asyncio.CancelledError):                            # a shutdown is let through
        await collector.sync(game.bot)
    assert not collector._desk(game.bot).lock.locked()


# -- the role -------------------------------------------------------------------------------------

async def test_a_role_of_that_name_is_taken_over_and_the_owner_is_told_about_horus(game_db):
    game = game_db
    by_hand = game.guild.add_role(4242, NAME)                              # made by the owner, no permissions
    other = game.guild.add_role(4243, "Kolekcjoner pupili")                # not exactly the name
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    outcome = await collector.sync(game.bot)
    assert outcome.given and by_hand in ala.roles and other not in ala.roles
    assert rank_roles(game) == [by_hand] and saved_state(game.bot)["role_id"] == 4242
    assert game.bot.owner_log == [collector.LOG_ROLE_ADOPTED]
    note = game.bot.owner_log[0]
    assert "po nazwie" in note and "`ranksConfig.json` Horusa" in note and NAME in note
    await collector.sync(game.bot)
    assert game.bot.owner_log == [collector.LOG_ROLE_ADOPTED]              # told once


async def test_a_role_of_that_name_is_taken_over_even_when_nobody_is_ranked(game_db):
    game = game_db
    by_hand = game.guild.add_role(4242, NAME)
    stray = game.add_member("Dziki")
    stray.roles.append(by_hand)
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, removed=(stray.id,))
    assert saved_state(game.bot) == {"role_id": 4242} and holders(game) == []


async def test_a_made_role_is_told_to_the_owner_once_with_the_note_about_horus(game_db):
    game = game_db
    await lead(game)
    await collector.sync(game.bot)
    restart(game)
    await collector.sync(game.bot)
    assert game.bot.owner_log == [collector.LOG_ROLE_MADE]
    note = game.bot.owner_log[0]
    assert "po nazwie" in note and "`ranksConfig.json` Horusa" in note and NAME in note


async def test_the_saved_id_wins_whatever_the_role_is_called_now(game_db):
    game = game_db
    ala = await lead(game)
    role = rank_role(game)
    role.name = "Zbieracz"                                                  # renamed by the owner
    ola = supporter(game, "Ola")
    await cards(game.bot.db, ola.id, 9)
    restart(game)
    outcome = await collector.sync(game.bot)
    assert outcome.removed == (ala.id,) and role in ola.roles and role not in ala.roles
    assert rank_roles(game) == [] and saved_state(game.bot)["role_id"] == role.id   # no second role


async def test_a_role_deleted_on_the_server_is_made_again(game_db):
    game = game_db
    ala = await lead(game)
    old = rank_role(game)
    await old.delete()
    outcome = await collector.sync(game.bot)
    new = rank_role(game)
    assert outcome.given and new is not old and new in ala.roles
    assert saved_state(game.bot) == {"role_id": new.id, "announced": ala.id}
    assert game.bot.owner_log == [collector.LOG_ROLE_MADE, collector.LOG_ROLE_MADE]
    assert announcements(game) == [announced_text(ala)]                    # given again: not announced again


async def test_a_role_the_cache_has_not_heard_of_yet_is_not_made_twice(game_db, monkeypatch):
    game = game_db
    clock = Clock(monkeypatch)
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    made: list[FakeRole] = []

    async def create_role(*, name, **kwargs):                              # Discord made it; the cache lags
        role = FakeRole(990_000_000_000_000_700 + len(made), name, game.guild)
        made.append(role)
        return role

    monkeypatch.setattr(game.guild, "create_role", create_role)
    assert (await collector.sync(game.bot)).given and made[0] in ala.roles
    clock.now += collector.FRESH_ROLE_SECONDS - 1
    assert (await collector.sync(game.bot)).changed is False and len(made) == 1
    game.guild.roles.append(made[0])                                       # the event arrived
    clock.now += 10 * collector.FRESH_ROLE_SECONDS
    assert (await collector.sync(game.bot)).changed is False and len(made) == 1
    assert collector._desk(game.bot).fresh is None


async def test_a_fresh_role_that_never_shows_up_counts_as_gone_after_a_while(game_db, monkeypatch):
    game = game_db
    clock = Clock(monkeypatch)
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    made: list[FakeRole] = []

    async def create_role(*, name, **kwargs):
        role = FakeRole(990_000_000_000_000_800 + len(made), name, game.guild)
        made.append(role)
        return role

    monkeypatch.setattr(game.guild, "create_role", create_role)
    await collector.sync(game.bot)
    clock.now += collector.FRESH_ROLE_SECONDS + 1                          # deleted before the cache saw it
    await collector.sync(game.bot)
    assert len(made) == 2 and saved_state(game.bot)["role_id"] == made[1].id


async def test_a_role_that_cannot_be_made_is_told_once_and_tried_again_later_not_at_every_run(game_db,
                                                                                               monkeypatch):
    game = game_db
    clock = Clock(monkeypatch)
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    attempts = []
    real = game.guild.create_role

    async def create_role(**kwargs):
        attempts.append(kwargs["name"])
        return await real(**kwargs)

    monkeypatch.setattr(game.guild, "create_role", create_role)
    game.guild.create_role_error = forbidden()                             # 250 roles, no permission ...
    for _ in range(4):
        outcome = await collector.sync(game.bot)
        assert outcome == collector.Outcome(collector.NO_ROLE, leader_id=ala.id) and not outcome.try_soon
    assert attempts == [NAME]                                              # one try, not four
    told = collector.LOG_ROLE_FAILED.format(error="Forbidden")
    assert game.bot.owner_log == [told] and "250" in told and "ręcznie" in told and "$kolekcjoner" in told
    assert game.log_channel.sent_count(f"<@{game.bot.ids.owner}>") == 1    # the owner is pinged for it
    assert holders(game) == [] and announcements(game) == []
    assert collector.describe(outcome) == collector.MSG_NO_ROLE

    clock.now += collector.ROLE_RETRY_AFTER - 1
    await collector.sync(game.bot)
    assert len(attempts) == 1
    clock.now += 2                                                         # later: one more try
    await collector.sync(game.bot)
    await collector.sync(game.bot)
    assert len(attempts) == 2
    await collector.sync(game.bot, force=True)                             # the start / $kolekcjoner: at once
    assert len(attempts) == 3 and game.bot.owner_log == [told]             # ... and told only once

    by_hand = game.guild.add_role(4242, NAME)                              # the owner made it by hand
    outcome = await collector.sync(game.bot)
    assert outcome.given and by_hand in ala.roles and len(attempts) == 3
    assert game.bot.owner_log == [told, collector.LOG_ROLE_ADOPTED]
    assert announcements(game) == [announced_text(ala)]


async def test_a_failure_after_a_success_is_told_again(game_db):
    """Fail -> success -> fail: the second failure of one run is news again (told once, again)."""
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    failed = collector.LOG_ROLE_FAILED.format(error="Forbidden")
    game.guild.create_role_error = forbidden()
    for _ in range(2):
        assert (await collector.sync(game.bot, force=True)).status == collector.NO_ROLE
    assert game.bot.owner_log == [failed]
    game.guild.create_role_error = None                                    # a role slot was freed
    assert (await collector.sync(game.bot, force=True)).given
    assert game.bot.owner_log == [failed, collector.LOG_ROLE_MADE]
    await rank_role(game).delete()                                         # ... and the role deleted later
    game.guild.create_role_error = forbidden()
    for _ in range(2):
        assert (await collector.sync(game.bot, force=True)).status == collector.NO_ROLE
    assert game.bot.owner_log == [failed, collector.LOG_ROLE_MADE, failed]
    assert game.log_channel.sent_count(f"<@{game.bot.ids.owner}>") == 2    # pinged both times


@pytest.mark.parametrize("why", ["permissions", "managed"])
async def test_a_role_of_that_name_that_is_not_ours_is_neither_taken_over_nor_doubled(game_db, why):
    """Horus knows the rank by its name: two roles of one name would both count in its giveaway."""
    game = game_db
    twin = game.guild.add_role(4242, NAME)
    if why == "permissions":
        twin.permissions = discord.Permissions(manage_messages=True)
    else:
        twin.managed = True
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    for _ in range(3):
        assert await collector.sync(game.bot) == collector.Outcome(collector.NO_ROLE, leader_id=ala.id)
    assert rank_roles(game) == [twin] and twin not in ala.roles
    assert game.bot.owner_log == [collector.LOG_ROLE_TWIN] and not game.bot.store.collector.path.exists()

    twin.permissions = discord.Permissions.none()                          # the owner cleared it
    twin.managed = False
    outcome = await collector.sync(game.bot)
    assert outcome.given and twin in ala.roles
    assert game.bot.owner_log == [collector.LOG_ROLE_TWIN, collector.LOG_ROLE_ADOPTED]

    # Fail -> success -> fail: the role goes and another one that is not ours shows up - told again.
    await twin.delete()
    other = game.guild.add_role(4250, NAME)
    other.permissions = discord.Permissions(manage_messages=True)
    for _ in range(2):
        assert await collector.sync(game.bot) == collector.Outcome(collector.NO_ROLE, leader_id=ala.id)
    assert game.bot.owner_log == [collector.LOG_ROLE_TWIN, collector.LOG_ROLE_ADOPTED, collector.LOG_ROLE_TWIN]
    assert rank_roles(game) == [other] and other not in ala.roles


def second_role_note(role_id: int, *twins: tuple[int, int]) -> str:
    """The owner note about other roles of the rank's name: ``(id, how many hold it)`` each."""
    return collector.LOG_SECOND_ROLE.format(role_id=role_id, twins=", ".join(
        collector.LOG_SECOND_ROLE_ITEM.format(role_id=twin_id, holders=count) for twin_id, count in twins))


async def test_a_second_role_of_the_name_is_told_to_the_owner_and_never_touched(game_db):
    """Horus counts EVERY role of that name: a twin beside the bot's own role (found by its saved
    id) is noticed at every synchronisation, told once per run, never deleted nor taken away."""
    game = game_db
    ala = await lead(game)
    own = rank_role(game)
    assert (await collector.sync(game.bot)).changed is False and game.bot.owner_log == [collector.LOG_ROLE_MADE]

    twin = game.guild.add_role(4243, NAME)                                 # made by hand, or made twice
    stray = game.add_member("Dziki")
    stray.roles.append(twin)
    for _ in range(3):
        assert await collector.sync(game.bot) == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True)
    note = second_role_note(own.id, (4243, 1))
    assert game.bot.owner_log == [collector.LOG_ROLE_MADE, note]           # once, however often it is seen
    assert game.log_channel.sent_count(f"<@{game.bot.ids.owner}>") == 1    # the owner is pinged for it
    assert f"`{own.id}`" in note and "`4243`" in note and "posiadaczy: 1" in note
    assert "Horus" in note and "po nazwie" in note and NAME in note
    assert twin in game.guild.roles and twin in stray.roles and own in ala.roles   # nothing is touched
    assert saved_state(game.bot)["role_id"] == own.id and announcements(game) == [announced_text(ala)]

    restart(game)                                                          # a new run: told once more
    await collector.sync(game.bot)
    await collector.sync(game.bot)
    assert game.bot.owner_log == [collector.LOG_ROLE_MADE, note, note]

    await twin.delete()                                                    # the owner removed it ...
    assert (await collector.sync(game.bot)).changed is False
    again = game.guild.add_role(4243, NAME)                                # ... and it is there again
    again.permissions = discord.Permissions(manage_messages=True)          # (whatever it carries)
    for _ in range(2):
        await collector.sync(game.bot)
    assert game.bot.owner_log[3:] == [second_role_note(own.id, (4243, 0))]
    another = game.guild.add_role(4244, NAME)                              # one more: both are listed
    another.managed = True
    stray.roles.append(another)
    for _ in range(2):
        await collector.sync(game.bot)
    assert game.bot.owner_log[4:] == [second_role_note(own.id, (4243, 0), (4244, 1))]
    assert again in game.guild.roles and another in stray.roles and holders(game) == ["Ala", "Dziki"]


async def test_of_two_roles_of_the_name_one_is_taken_over_and_the_other_reported(game_db):
    game = game_db
    first = game.guild.add_role(4242, NAME)
    second = game.guild.add_role(4243, NAME)
    third = game.guild.add_role(4244, NAME)
    third.managed = True
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    for _ in range(2):
        outcome = await collector.sync(game.bot)
    assert outcome.holds and first in ala.roles and second not in ala.roles and third not in ala.roles
    assert game.bot.owner_log == [collector.LOG_ROLE_ADOPTED, second_role_note(4242, (4243, 0), (4244, 0))]
    assert rank_roles(game) == [first, second, third]                      # none was deleted


async def test_a_state_file_that_cannot_be_written_is_told_once_and_the_role_note_is_not_repeated(game_db,
                                                                                                    monkeypatch):
    game = game_db
    bot = game.bot
    game.guild.add_role(4242, NAME)
    ala = supporter(game, "Ala")
    await cards(bot.db, ala.id, 1)
    path = bot.store.collector.path
    real = bot.store.collector.save
    broken = [True]

    def save(value):
        if broken:
            raise OSError("test: disk full")
        real(value)

    monkeypatch.setattr(bot.store.collector, "save", save)
    for _ in range(4):                                                     # the role: taken over by name each time
        outcome = await collector.sync(bot)
        assert outcome.status == collector.DONE and outcome.holds
        assert outcome.announcement == collector.NOT_ANNOUNCED
    unwritable = collector.LOG_FILE_UNWRITABLE.format(path=path)
    assert bot.owner_log == [unwritable, collector.LOG_ROLE_ADOPTED]       # each of them ONCE
    assert game.log_channel.sent_count(f"<@{bot.ids.owner}>") == 1         # the owner is pinged for the file
    assert holders(game) == ["Ala"] and announcements(game) == [] and not path.exists()

    broken.clear()                                                         # the disk is back
    outcome = await collector.sync(bot)
    assert outcome.announcement == collector.ANNOUNCED and saved_state(bot) == {"role_id": 4242, "announced": ala.id}
    assert bot.owner_log == [unwritable, collector.LOG_ROLE_ADOPTED]

    ola = supporter(game, "Ola")                                           # fail -> success -> fail: told again,
    await cards(bot.db, ola.id, 5)                                         # this time by the announcement
    broken.append(True)
    for _ in range(2):
        outcome = await collector.sync(bot)
        assert outcome.leader_id == ola.id and outcome.holds and outcome.announcement == collector.NOT_ANNOUNCED
    assert bot.owner_log == [unwritable, collector.LOG_ROLE_ADOPTED, unwritable]
    assert holders(game) == ["Ola"] and announcements(game) == [announced_text(ala)]
    assert saved_state(bot)["announced"] == ala.id


# -- the announcement: exactly once ---------------------------------------------------------------

async def test_a_restart_and_a_role_given_again_announce_nothing(game_db):
    game = game_db
    ala = await lead(game)
    for _ in range(3):
        restart(game)
        assert (await collector.sync(game.bot, force=True)).changed is False
    ala.roles.remove(rank_role(game))                                      # somebody took it by hand
    restart(game)
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True, given=True)
    assert announcements(game) == [announced_text(ala)] and game.bot.owner_log == [collector.LOG_ROLE_MADE]


async def test_the_note_is_written_before_the_message_is_sent(game_db, monkeypatch):
    """A crash between the two can cost the announcement, never repeat it."""
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    seen = []
    real = game.channel.send

    async def send(content=None, **kwargs):
        seen.append(saved_state(game.bot).get("announced"))
        return await real(content, **kwargs)

    monkeypatch.setattr(game.channel, "send", send)
    await collector.sync(game.bot)
    assert seen == [ala.id]


async def test_an_announcement_that_cannot_be_noted_is_not_sent(game_db, monkeypatch):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    game.guild.add_role(4242, NAME)
    real = game.bot.store.collector.save
    refuse = []

    def save(value):
        if refuse and "announced" in value and value["announced"]:
            raise OSError("test: disk full")
        real(value)

    monkeypatch.setattr(game.bot.store.collector, "save", save)
    refuse.append(True)
    outcome = await collector.sync(game.bot)
    assert outcome.given and outcome.announcement == collector.NOT_ANNOUNCED and announcements(game) == []
    assert saved_state(game.bot) == {"role_id": 4242}
    # The owner learns why nothing is announced (once, pinged).
    unwritable = collector.LOG_FILE_UNWRITABLE.format(path=game.bot.store.collector.path)
    assert game.bot.owner_log == [collector.LOG_ROLE_ADOPTED, unwritable]
    refuse.clear()                                                         # the disk is back: announced now
    assert (await collector.sync(game.bot)).announcement == collector.ANNOUNCED
    assert announcements(game) == [announced_text(ala)]
    assert game.bot.owner_log == [collector.LOG_ROLE_ADOPTED, unwritable]


async def test_the_boss_channel_that_is_not_there_announces_nothing_and_notes_nothing(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    ala = supporter(game, "Ala")
    await cards(bot.db, ala.id, 1)
    channel = bot.fake_channels.pop(bot.ids.boss_channel)                  # Discord's cache does not have it
    for _ in range(2):
        outcome = await collector.sync(bot)
        assert outcome.status == collector.DONE and outcome.holds
        assert outcome.announcement == collector.NOT_ANNOUNCED
    assert holders(game) == ["Ala"] and "announced" not in saved_state(bot)   # the role is given all the same
    assert bot.owner_log == [collector.LOG_ROLE_MADE]                      # (no send was tried at all)
    assert collector.MSG_ANNOUNCE_FAILED in collector.describe(outcome)

    bot.fake_channels[bot.ids.boss_channel] = channel                      # it is back: announced, once
    assert (await collector.sync(bot)).announcement == collector.ANNOUNCED
    assert (await collector.sync(bot)).changed is False
    assert announcements(game) == [announced_text(ala)] and saved_state(bot)["announced"] == ala.id


@pytest.mark.parametrize("lost", [asyncio.TimeoutError(), ConnectionResetError("test: connection reset"),
                                  discord.DiscordServerError(SimpleNamespace(status=502, reason="Bad Gateway"),
                                                             "test")],
                         ids=["timeout", "connection reset", "502"])
async def test_an_announcement_whose_answer_was_lost_is_never_sent_a_second_time(game_db, monkeypatch, lost):
    """With the REAL ``io.retry``: Discord posts the message, the answer is lost - ``io.send`` would
    send the same text again.  The announcement is one attempt: one message, for good."""
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    real = game.channel.send
    answers_lost = [lost]
    attempts = []

    async def send(content=None, **kwargs):
        attempts.append(content)
        message = await real(content, **kwargs)                            # it IS on the channel ...
        if answers_lost:
            raise answers_lost.pop()                                       # ... but the bot never hears so
        return message

    monkeypatch.setattr(game.channel, "send", send)
    assert io.is_transient(lost)                                           # (what io.retry would repeat)
    outcome = await collector.sync(game.bot)
    assert outcome.holds and outcome.announcement == collector.NOT_ANNOUNCED
    assert attempts == [announced_text(ala)] and announcements(game) == [announced_text(ala)]
    assert saved_state(game.bot)["announced"] == ala.id                    # the note stays: never repeated
    (told,) = owner_lines(game.bot, "Nie udało się ogłosić")
    assert collector.LOG_ANNOUNCE_LOST in told
    for _ in range(3):
        assert (await collector.sync(game.bot)).changed is False
    restart(game)
    assert (await collector.sync(game.bot, force=True)).changed is False
    assert attempts == [announced_text(ala)] and announcements(game) == [announced_text(ala)]


async def test_a_refused_announcement_is_tried_again_a_lost_one_never(game_db):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    game.channel.fail_with, game.channel.fail_sends = forbidden(), 1       # certainly not delivered
    outcome = await collector.sync(game.bot)
    assert outcome.holds and outcome.announcement == collector.NOT_ANNOUNCED
    assert saved_state(game.bot).get("announced") is None and announcements(game) == []
    assert len(owner_lines(game.bot, "Nie udało się ogłosić")) == 1
    assert collector.LOG_ANNOUNCE_AGAIN in game.bot.owner_log[-1]
    assert (await collector.sync(game.bot)).announcement == collector.ANNOUNCED
    assert announcements(game) == [announced_text(ala)]

    ola = supporter(game, "Ola")
    await cards(game.bot.db, ola.id, 5)
    game.channel.fail_with, game.channel.fail_sends = asyncio.TimeoutError(), 1   # it may have arrived
    outcome = await collector.sync(game.bot)
    assert outcome.holds and outcome.announcement == collector.NOT_ANNOUNCED
    assert saved_state(game.bot)["announced"] == ola.id
    assert collector.LOG_ANNOUNCE_LOST in game.bot.owner_log[-1]
    for _ in range(2):
        assert (await collector.sync(game.bot)).changed is False
    assert announcements(game) == [announced_text(ala)]                    # never a second time


# -- nothing may make a fight stutter -------------------------------------------------------------

async def test_quiet_is_no_action_no_typed_fight_and_no_boss_about_to_arrive(game_db):
    game = game_db
    state = game.bot.state
    assert collector.quiet(game.bot)
    with state.hold_busy("event:chest"):
        assert not collector.quiet(game.bot)
    for phase in BossPhase:
        await state.set_boss_phase(phase)
        fight = phase in (BossPhase.ENGAGED, BossPhase.FIGHTING)
        assert achievements._in_action(game.bot) is fight, phase
        # PRE_SPAWN: nothing plays, but the channel is cleared at the spawn a few minutes later -
        # the one announcement would be deleted at once.
        assert collector.quiet(game.bot) is (not fight and phase is not BossPhase.PRE_SPAWN), phase
    await state.set_boss_phase(BossPhase.IDLE)
    with challenge.round_running(game.channel):                            # a typed fight on the boss channel
        assert not collector.quiet(game.bot)
    with challenge.round_running(game.announce_channel):                   # elsewhere: not our business
        assert collector.quiet(game.bot)


async def test_the_loop_waits_while_something_plays_on_the_boss_channel(game_db, loop, monkeypatch):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    seen = spy(monkeypatch)
    with game.bot.state.hold_busy("hunt"):
        loop()
        await asyncio.sleep(0.3)
        assert seen.calls == 0 and rank_roles(game) == [] and announcements(game) == []
    assert await until(lambda: collector._desk(game.bot).runs == 1)
    assert holders(game) == ["Ala"] and announcements(game) == [announced_text(ala)]

    await game.bot.state.set_boss_phase(BossPhase.FIGHTING)                # a boss fight: the same
    ola = supporter(game, "Ola")
    await cards(game.bot.db, ola.id, 4)
    collector.request(game.bot, "test")
    await asyncio.sleep(0.3)
    assert seen.calls == 1 and holders(game) == ["Ala"] and collector.pending(game.bot)
    await game.bot.state.set_boss_phase(BossPhase.IDLE)
    assert await until(lambda: collector._desk(game.bot).runs == 2)
    assert holders(game) == ["Ola"] and len(announcements(game)) == 2 and seen.most == 1


async def test_an_announcement_that_finds_the_channel_busy_waits_for_the_next_run(game_db, monkeypatch):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    real = io.add_role
    holding = []

    async def add_role(member, role, *, reason=None):                      # a hunt starts during the run
        given = await real(member, role, reason=reason)
        hold = game.bot.state.hold_busy("hunt")
        hold.__enter__()
        holding.append(hold)
        return given

    monkeypatch.setattr(io, "add_role", add_role)
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True, given=True,
                                        announcement=collector.LATER)
    assert holders(game) == ["Ala"] and announcements(game) == []
    assert "announced" not in saved_state(game.bot) and collector.pending(game.bot)   # asked for again
    assert collector.MSG_ANNOUNCE_LATER in collector.describe(outcome)

    holding.pop().__exit__(None, None, None)
    outcome = await collector.sync(game.bot)
    assert outcome == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True,
                                        announcement=collector.ANNOUNCED)
    assert announcements(game) == [announced_text(ala)]


async def test_nothing_is_announced_while_the_boss_is_about_to_arrive(game_db, loop, monkeypatch):
    """PRE_SPAWN: the channel is cleared at the spawn, minutes later - the loop waits, as in a fight."""
    game = game_db
    bot = game.bot
    ala = supporter(game, "Ala")
    await cards(bot.db, ala.id, 1)
    seen = spy(monkeypatch)
    desk = collector._desk(bot)
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    loop()
    await asyncio.sleep(0.3)
    assert seen.calls == 0 and rank_roles(game) == [] and announcements(game) == []
    assert (await collector.sync_now(bot)).status == collector.BUSY        # $kolekcjoner: the same
    assert seen.calls == 0
    await bot.state.set_boss_phase(BossPhase.VISIBLE)                      # the boss is there: quiet again
    assert await until(lambda: desk.runs == 1)
    assert holders(game) == ["Ala"] and announcements(game) == [announced_text(ala)]


async def test_a_boss_that_gets_near_during_a_run_holds_the_announcement_back_without_spinning(game_db, loop,
                                                                                               monkeypatch):
    """The announcement that finds PRE_SPAWN asks for another run - which must WAIT for a quiet
    channel (the loop's poll), not run again and again until the boss is there."""
    game = game_db
    bot = game.bot
    ala = supporter(game, "Ala")
    await cards(bot.db, ala.id, 1)
    real = io.add_role

    async def add_role(member, role, *, reason=None):                      # the phase changes during the run
        given = await real(member, role, reason=reason)
        await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
        return given

    monkeypatch.setattr(io, "add_role", add_role)
    desk = collector._desk(bot)
    loop()
    assert await until(lambda: desk.runs == 1)
    assert desk.last.announcement == collector.LATER and holders(game) == ["Ala"]
    await asyncio.sleep(0.4)                                               # many SETTLEs and QUIET_POLLs later
    assert desk.runs == 1 and collector.pending(bot) and announcements(game) == []
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    assert await until(lambda: desk.runs == 2)
    assert desk.last.announcement == collector.ANNOUNCED and announcements(game) == [announced_text(ala)]


# -- one entry point: a burst is one run, never two at a time -------------------------------------

async def test_a_request_without_the_loop_does_nothing_and_never_raises(game_db):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    assert not collector.pending(game.bot)
    collector.request(game.bot, "test")
    await asyncio.sleep(0.05)
    assert collector.pending(game.bot) and collector._desk(game.bot).runs == 0 and rank_roles(game) == []
    collector.request(None, "no bot")                                      # a scene without a bot
    collector.request(object(), "odd")                                     # (logged, never raised)


async def test_the_loop_synchronises_after_the_start_and_a_burst_of_requests_is_one_run(game_db, loop,
                                                                                         monkeypatch):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    seen = spy(monkeypatch)
    desk = collector._desk(game.bot)
    loop()
    assert await until(lambda: desk.runs == 1)                              # the start: nobody asked
    assert seen.forced == [True] and holders(game) == ["Ala"] and not collector.pending(game.bot)
    await asyncio.sleep(0.2)
    assert seen.calls == 1                                                  # and then it rests

    for n in range(40):                                                     # a burst ...
        collector.request(game.bot, f"burst {n}")
    assert await until(lambda: desk.runs == 2)
    await asyncio.sleep(0.3)
    assert seen.calls == 2 and seen.forced == [True, False] and seen.most == 1   # ... is ONE run


async def test_a_burst_spread_over_time_is_one_run(game_db, loop, monkeypatch):
    """One change on the server is several events, some hundred milliseconds apart: the loop lets
    them gather (SETTLE) before it runs - the flag alone would make it one run per event."""
    game = game_db
    seen = spy(monkeypatch)
    desk = collector._desk(game.bot)
    loop()
    assert await until(lambda: desk.runs == 1)
    monkeypatch.setattr(collector, "SETTLE", 0.6)
    for n in range(6):                                                      # 0.25 s in all: well within SETTLE
        collector.request(game.bot, f"event {n}")
        await asyncio.sleep(0.05)
    assert seen.calls == 1                                                  # still gathering
    assert await until(lambda: desk.runs == 2)
    await asyncio.sleep(0.3)
    assert seen.calls == 2 and seen.most == 1 and not collector.pending(game.bot)


async def test_kolekcjoner_tries_to_make_the_role_at_once_also_when_it_had_to_wait(game_db, loop, monkeypatch):
    """After a failure the role is not made again for an hour - except by the start and by
    ``$kolekcjoner``, also by one typed while something played on the boss channel."""
    game = game_db
    bot = game.bot
    ala = supporter(game, "Ala")
    await cards(bot.db, ala.id, 1)
    seen = spy(monkeypatch)
    desk = collector._desk(bot)
    game.guild.create_role_error = forbidden()                             # 250 roles
    loop()
    assert await until(lambda: desk.runs == 1)
    assert desk.last.status == collector.NO_ROLE and seen.forced == [True]
    game.guild.create_role_error = None                                    # the owner freed a role slot

    collector.request(bot, "anything else")                                # the loop by itself: not yet
    assert await until(lambda: desk.runs == 2)
    assert desk.last.status == collector.NO_ROLE and rank_roles(game) == [] and seen.forced == [True, False]

    with bot.state.hold_busy("hunt"):                                      # $kolekcjoner during a hunt
        assert (await collector.sync_now(bot)).status == collector.BUSY
        await asyncio.sleep(0.2)
        assert desk.runs == 2 and rank_roles(game) == []
    assert await until(lambda: desk.runs == 3)                             # the run that follows: at once
    assert seen.forced == [True, False, True] and holders(game) == ["Ala"]
    collector.request(bot, "anything else")                                # the wish was used up by that run
    assert await until(lambda: desk.runs == 4)
    assert seen.forced == [True, False, True, False] and not desk.force_next


async def test_kolekcjoner_on_a_quiet_channel_tries_to_make_the_role_at_once(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    clock = Clock(monkeypatch)
    ala = supporter(game, "Ala")
    await cards(bot.db, ala.id, 1)
    game.guild.create_role_error = forbidden()
    assert (await collector.sync(bot)).status == collector.NO_ROLE
    game.guild.create_role_error = None
    clock.now += 60                                                        # long before the bot's own next try
    assert (await collector.sync(bot)).status == collector.NO_ROLE and rank_roles(game) == []
    seen = spy(monkeypatch)
    desk = collector._desk(bot)
    desk.force_next, desk.grace_until = True, clock.now + 30               # (left over; a grace period runs)
    outcome = await collector.sync_now(bot)
    assert outcome.given and holders(game) == ["Ala"] and seen.forced == [True]
    assert not desk.force_next and desk.grace_until == 0.0 and not collector.pending(bot)


@pytest.mark.parametrize("what", ["give", "removal"])
async def test_a_give_or_a_removal_that_failed_is_tried_again_soon_a_few_times(game_db, loop, monkeypatch, what):
    """A network failure at the very moment the lead changes must not leave the rank with nobody
    (or with the old holder) for half an hour; a role above the bot's own must not poll for ever."""
    game = game_db
    bot = game.bot
    monkeypatch.setattr(collector, "RETRY_SOON", 0.05)                     # (the safety net stays an hour away)
    ala = await lead(game, "Ala", 2)
    role = rank_role(game)
    ola = supporter(game, "Ola")
    await cards(bot.db, ola.id, 5)
    member, method = (ola, "add_roles") if what == "give" else (ala, "remove_roles")
    real = getattr(member, method)
    refuse = [True]

    async def change(*roles, reason=None):
        if refuse:
            raise forbidden()
        await real(*roles, reason=reason)

    monkeypatch.setattr(member, method, change)
    start = "Nie udało się nadać rangi" if what == "give" else "Nie udało się zdjąć rangi"
    desk = collector._desk(bot)
    runs = desk.runs
    loop()
    assert await until(lambda: desk.runs == runs + 1 + collector.QUICK_RETRIES)   # the run and its quick tries
    await asyncio.sleep(0.4)
    assert desk.runs == runs + 1 + collector.QUICK_RETRIES                 # ... then back to the slow net
    assert desk.last.unfinished and not desk.last.try_soon and len(owner_lines(bot, start)) == 1
    assert holders(game) == ([] if what == "give" else ["Ala", "Ola"])

    refuse.clear()                                                         # whatever it was, it is over
    runs = desk.runs
    collector.request(bot, "test")
    assert await until(lambda: desk.runs == runs + 1)
    assert not desk.last.unfinished and holders(game) == ["Ola"]
    await asyncio.sleep(0.3)
    assert desk.runs == runs + 1                                           # done: nothing to try again

    if what == "give":                                                     # the same trouble later:
        ola.roles.remove(role)
    else:
        ala.roles.append(role)
    refuse.append(True)
    runs = desk.runs
    collector.request(bot, "test")
    assert await until(lambda: desk.runs == runs + 1 + collector.QUICK_RETRIES)   # quick tries anew
    await asyncio.sleep(0.4)
    assert desk.runs == runs + 1 + collector.QUICK_RETRIES
    assert len(owner_lines(bot, start)) == 2                               # and the owner is told again


@pytest.mark.parametrize("trouble", ["no ranking", "no guild", "failed", "not given", "not removed"])
async def test_kolekcjoner_that_could_not_finish_leaves_the_retry_to_the_loop(game_db, loop, monkeypatch, trouble):
    """``$kolekcjoner`` answers "spróbuję ponownie sam".  The loop did not see that run: without a
    request it would come back only with its safety net, half an hour later."""
    game = game_db
    bot = game.bot
    monkeypatch.setattr(collector, "RETRY_SOON", 0.05)                     # (the safety net stays an hour away)
    monkeypatch.setattr(collector, "QUICK_RETRIES", 10_000)                # (no race with the end of the quick tries)
    ala = await lead(game, "Ala", 2)
    ola = supporter(game, "Ola")
    desk = collector._desk(bot)
    loop()
    assert await until(lambda: desk.runs == 2)                             # the run of the start: all is well
    await asyncio.sleep(0.2)
    assert desk.runs == 2 and not collector.pending(bot)                   # ... and the loop sleeps for an hour

    await cards(bot.db, ola.id, 6)                                         # Ola takes the lead - and trouble comes
    with monkeypatch.context() as patch:
        if trouble == "no ranking":
            patch.setattr(bot, "db", DatabaseDown())
        elif trouble == "no guild":
            patch.setattr(game.guild, "unavailable", True, raising=False)
        elif trouble == "failed":
            async def broken(*args, **kwargs):
                raise RuntimeError("test: something unexpected")

            patch.setattr(collector, "_role_for", broken)
        else:
            member, method = (ola, "add_roles") if trouble == "not given" else (ala, "remove_roles")

            async def refused(*roles, reason=None):
                raise forbidden()

            patch.setattr(member, method, refused)

        outcome = await collector.sync_now(bot)                            # the administrator types $kolekcjoner
        assert outcome.try_soon is (trouble in ("no ranking", "no guild", "failed"))
        assert outcome.unfinished is (trouble in ("not given", "not removed"))
        assert desk.runs == 3 and collector.pending(bot)                   # the promise is with the loop now
        # The loop takes it over: it tries, and tries again after RETRY_SOON - not after PERIOD.
        assert await until(lambda: desk.runs >= 5)
        assert desk.last.try_soon or desk.last.unfinished
    # Whatever it was, it is over: the loop's next try puts the rank where it belongs.
    assert await until(lambda: desk.last.status == collector.DONE and not desk.last.unfinished)
    assert holders(game) == ["Ola"] and desk.last.leader_id == ola.id
    runs = desk.runs
    await asyncio.sleep(0.3)
    assert desk.runs == runs and not collector.pending(bot)                # done: back to the slow net


@pytest.mark.parametrize("what", ["give", "removal"])
async def test_kolekcjoner_gets_the_loops_quick_tries_also_when_the_loop_had_used_its_own_up(game_db, loop,
                                                                                          monkeypatch, what):
    """The loop noticed the trouble by itself, made its quick tries and is back to the slow net.  The
    administrator then types ``$kolekcjoner`` and reads "spróbuję ponownie sam": the loop must try again
    after ``RETRY_SOON`` (its quick tries anew), not once two seconds later and then in half an hour -
    and it must not poll for ever either."""
    game = game_db
    bot = game.bot
    monkeypatch.setattr(collector, "RETRY_SOON", 0.05)                     # (the safety net stays an hour away)
    ala = await lead(game, "Ala", 2)
    ola = supporter(game, "Ola")
    await cards(bot.db, ola.id, 5)
    member, method = (ola, "add_roles") if what == "give" else (ala, "remove_roles")
    real = getattr(member, method)
    refuse = [True]

    async def change(*roles, reason=None):
        if refuse:
            raise forbidden()
        await real(*roles, reason=reason)

    monkeypatch.setattr(member, method, change)
    desk = collector._desk(bot)
    runs = desk.runs
    loop()
    assert await until(lambda: desk.runs == runs + 1 + collector.QUICK_RETRIES)   # the run and its quick tries
    await asyncio.sleep(0.4)
    assert desk.runs == runs + 1 + collector.QUICK_RETRIES and desk.last.unfinished   # ... back to the slow net

    runs = desk.runs
    outcome = await collector.sync_now(bot)                                # the administrator types $kolekcjoner
    assert outcome.unfinished and desk.runs == runs + 1 and collector.pending(bot)
    if what == "give":
        assert collector.describe(outcome).endswith("Spróbuję ponownie sam.")   # the promise the loop must keep
    # The loop takes it over as if the trouble were new: its run and the quick tries after RETRY_SOON ...
    assert await until(lambda: desk.runs == runs + 1 + 1 + collector.QUICK_RETRIES)
    await asyncio.sleep(0.4)
    assert desk.runs == runs + 1 + 1 + collector.QUICK_RETRIES             # ... and then the slow net again
    assert desk.last.unfinished and not collector.pending(bot)

    # Typed again and again it is still one round of tries per command, never a loop that spins.
    runs = desk.runs
    for _ in range(3):
        assert (await collector.sync_now(bot)).unfinished
    assert await until(lambda: desk.runs == runs + 3 + 1 + collector.QUICK_RETRIES)
    await asyncio.sleep(0.4)
    assert desk.runs == runs + 3 + 1 + collector.QUICK_RETRIES

    refuse.clear()                                                         # whatever it was, it is over
    runs = desk.runs
    outcome = await collector.sync_now(bot)
    assert outcome.status == collector.DONE and not outcome.unfinished and holders(game) == ["Ola"]
    await asyncio.sleep(0.3)
    assert desk.runs == runs + 1 and not collector.pending(bot)            # done: nothing is left for the loop


async def test_kolekcjoner_that_finished_leaves_nothing_for_the_loop(game_db):
    game = game_db
    bot = game.bot
    await lead(game, "Ala", 2)
    ola = supporter(game, "Ola")
    await cards(bot.db, ola.id, 6)
    collector.request(bot, "an album entry was kept")
    outcome = await collector.sync_now(bot)
    assert outcome.status == collector.DONE and outcome.given and not outcome.unfinished and not outcome.try_soon
    assert holders(game) == ["Ola"] and not collector.pending(bot)         # what was asked for is done
    for status in (collector.NO_STATE, collector.NO_ROLE, collector.TEST_RUN):   # nothing passes by itself there
        assert not collector.Outcome(status).try_soon and not collector.Outcome(status, leader_id=1).unfinished


def test_an_unfinished_outcome_is_one_that_knew_the_ranking_and_left_the_role_in_wrong_hands():
    done = collector.DONE
    assert collector.Outcome(done, leader_id=7).unfinished                  # the leader does not hold it
    assert collector.Outcome(done, leader_id=7, holds=True, not_removed=(3,)).unfinished
    assert collector.Outcome(done, not_removed=(3,)).unfinished
    assert not collector.Outcome(done).unfinished and not collector.Outcome(done, leader_id=7, holds=True).unfinished
    for status in (collector.NO_ROLE, collector.NO_RANKING, collector.NO_GUILD, collector.NO_STATE,
                   collector.FAILED, collector.BUSY, collector.TEST_RUN):
        assert not collector.Outcome(status, leader_id=7).unfinished, status


# -- a supporter role that blinks: the holder's grace period --------------------------------------

async def test_only_the_end_of_the_holders_support_begins_a_grace_period(game_db, monkeypatch):
    game = game_db
    bot = game.bot
    clock = Clock(monkeypatch)
    ala = await lead(game, "Ala", 5)
    ola = supporter(game, "Ola")
    await cards(bot.db, ola.id, 2)
    desk = collector._desk(bot)
    assert collector.HOLDER_GRACE == 60.0 and desk.grace_until == 0.0

    plain_ola = as_before(ola, [])                                         # somebody else: at once, as ever
    assert collector.member_changed(bot, ola, plain_ola) is True and asked(bot) and desk.grace_until == 0.0
    assert collector.member_changed(bot, plain_ola, ola) is True and asked(bot) and desk.grace_until == 0.0

    plain_ala = as_before(ala, [rank_role(game)])                          # the holder's support ended
    assert collector.member_changed(bot, ala, plain_ala) is True and asked(bot)
    assert desk.grace_until == clock.now + collector.HOLDER_GRACE
    clock.now += 20
    collector.request(bot, "an album entry was kept")                      # other reasons: no shorter, no longer
    assert collector.member_changed(bot, ola, plain_ola) is True
    assert desk.grace_until == clock.now + collector.HOLDER_GRACE - 20 and asked(bot)

    assert collector.member_changed(bot, plain_ala, ala) is True and asked(bot)   # it is back: no wait
    assert desk.grace_until == 0.0

    assert collector.member_changed(bot, ala, plain_ala) is True and desk.grace_until > clock.now
    assert collector.member_left(bot, plain_ala) is True and asked(bot)    # the holder left: no grace
    assert desk.grace_until == 0.0

    assert collector.member_changed(bot, ala, plain_ala) is True and desk.grace_until > clock.now
    assert collector.member_left(bot, ola) is True and desk.grace_until > clock.now   # (somebody else left)
    assert (await collector.sync_now(bot)).status == collector.DONE        # $kolekcjoner: the owner's word
    assert desk.grace_until == 0.0


async def test_a_supporter_role_that_blinks_keeps_the_rank_where_it_is(game_db, loop, monkeypatch):
    """A renewal / a change of tier: the supporter role is taken and, seconds later, given back.
    Without the grace period the runner-up would get the rank (announced) and the leader would
    get it back (announced again).

    (The grace period runs on the test's clock, which stands still: however slow the machine is,
    the wait cannot end by itself here - only by the support that is back.)"""
    game = game_db
    bot = game.bot
    clock = Clock(monkeypatch)
    monkeypatch.setattr(collector, "GRACE_POLL", 0.01)
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    await cards(bot.db, ala.id, 5)
    await cards(bot.db, ola.id, 2)
    seen = spy(monkeypatch)
    desk = collector._desk(bot)
    loop()
    assert await until(lambda: desk.runs == 1)
    assert holders(game) == ["Ala"]

    patron_role = ala.roles[0]
    before = as_before(ala, ala.roles)
    ala.roles.remove(patron_role)                                          # the supporter role is taken ...
    assert collector.member_changed(bot, before, ala) is True
    assert desk.grace_until == clock.now + collector.HOLDER_GRACE
    assert await clock.looked_at()                                         # (SETTLE is over: the loop waits it out)
    assert seen.calls == 1 and holders(game) == ["Ala"] and collector.pending(bot)
    collector.request(bot, "an album entry was kept")                      # other requests: not sooner for it
    clock.now += collector.HOLDER_GRACE / 2                                # ... and half of the wait is over
    assert await clock.looked_at()
    assert seen.calls == 1 and holders(game) == ["Ala"] and seen.running == 0

    before = as_before(ala, ala.roles)
    ala.roles.append(patron_role)                                          # ... and given back
    assert collector.member_changed(bot, before, ala) is True
    assert await until(lambda: desk.runs == 2)                             # ONE run answers all of it
    assert desk.last == collector.Outcome(collector.DONE, leader_id=ala.id, holds=True)
    await asyncio.sleep(0.2)
    assert seen.calls == 2 and seen.most == 1 and not collector.pending(bot)
    assert holders(game) == ["Ala"] and announcements(game) == [announced_text(ala)]


async def test_support_that_really_ended_moves_the_rank_after_the_grace_period(game_db, loop, monkeypatch):
    """(On the test's clock: the grace period is over when the test says so, not when a slow
    machine happens to get there.)"""
    game = game_db
    bot = game.bot
    clock = Clock(monkeypatch)
    monkeypatch.setattr(collector, "GRACE_POLL", 0.01)
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    await cards(bot.db, ala.id, 5)
    await cards(bot.db, ola.id, 2)
    desk = collector._desk(bot)
    loop()
    assert await until(lambda: desk.runs == 1)
    before = as_before(ala, ala.roles)
    ala.roles.remove(ala.roles[0])
    assert collector.member_changed(bot, before, ala) is True
    collector.request(bot, "an album entry was kept")                      # (not lost: the same run answers it)
    assert await clock.looked_at()
    assert desk.runs == 1 and holders(game) == ["Ala"]                     # the grace period runs
    clock.now += collector.HOLDER_GRACE - 0.5                              # ... to its very end
    assert await clock.looked_at()
    assert desk.runs == 1 and holders(game) == ["Ala"]
    clock.now += 0.5                                                       # and now it is over
    assert await until(lambda: desk.runs == 2)
    assert desk.last == collector.Outcome(collector.DONE, leader_id=ola.id, holds=True, given=True,
                                          removed=(ala.id,), announcement=collector.ANNOUNCED)
    assert holders(game) == ["Ola"] and announcements(game) == [announced_text(ala), announced_text(ola)]
    assert desk.grace_until == 0.0 and not collector.pending(bot)


async def test_a_holder_who_left_the_server_gets_no_grace_period(game_db, loop, monkeypatch):
    game = game_db
    bot = game.bot
    monkeypatch.setattr(collector, "HOLDER_GRACE", 30.0)                   # (far longer than the test waits)
    monkeypatch.setattr(collector, "GRACE_POLL", 0.01)
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    await cards(bot.db, ala.id, 5)
    await cards(bot.db, ola.id, 2)
    desk = collector._desk(bot)
    loop()
    assert await until(lambda: desk.runs == 1)
    before = as_before(ala, ala.roles)
    ala.roles.remove(ala.roles[0])                                         # his support ended: the wait begins
    assert collector.member_changed(bot, before, ala) is True
    await asyncio.sleep(0.2)
    assert desk.runs == 1
    game.guild.members.remove(ala)                                         # ... and he left
    assert collector.member_left(bot, ala) is True
    assert await until(lambda: desk.runs == 2)
    assert desk.last.leader_id == ola.id and desk.last.given and ola.roles[-1].name == NAME


async def test_two_synchronisations_never_run_at_the_same_time(game_db, loop, monkeypatch):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    seen = spy(monkeypatch, delay=0.05)
    desk = collector._desk(game.bot)
    loop()
    assert await until(lambda: seen.running == 1)                           # the start-up run is under way
    collector.request(game.bot, "during the run")                           # -> one more run after it
    answers = await asyncio.gather(collector.sync(game.bot), collector.sync_now(game.bot),
                                   collector.sync(game.bot))
    assert all(outcome.status == collector.DONE for outcome in answers)
    assert await until(lambda: desk.runs == seen.calls and not collector.pending(game.bot) and seen.running == 0)
    await asyncio.sleep(0.3)
    assert seen.most == 1 and seen.calls == desk.runs <= 5
    assert holders(game) == ["Ala"] and announcements(game) == [announced_text(ala)]
    assert len(rank_roles(game)) == 1 and game.bot.owner_log == [collector.LOG_ROLE_MADE]


async def test_a_request_during_a_run_is_answered_by_one_more_run(game_db, loop, monkeypatch):
    game = game_db
    seen = spy(monkeypatch, delay=0.1)
    desk = collector._desk(game.bot)
    loop()
    assert await until(lambda: seen.running == 1)
    for _ in range(10):
        collector.request(game.bot, "during the run")
    assert await until(lambda: desk.runs == 2)
    await asyncio.sleep(0.3)
    assert seen.calls == 2 and seen.most == 1


async def test_the_safety_net_runs_without_any_request(game_db, loop, monkeypatch):
    game = game_db
    monkeypatch.setattr(collector, "PERIOD", 0.05)
    seen = spy(monkeypatch)
    loop()
    assert await until(lambda: seen.calls >= 3)
    assert seen.most == 1 and seen.forced[:3] == [True, False, False]


async def test_a_run_that_could_not_look_is_tried_again_soon(game_db, loop, monkeypatch):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 1)
    monkeypatch.setattr(collector, "RETRY_SOON", 0.05)                      # (the safety net stays an hour away)
    real = game.bot.db
    game.bot.db = DatabaseDown()
    desk = collector._desk(game.bot)
    loop()
    assert await until(lambda: desk.runs >= 3)
    assert desk.last.status == collector.NO_RANKING and rank_roles(game) == []
    game.bot.db = real
    assert await until(lambda: desk.last.status == collector.DONE)
    assert holders(game) == ["Ala"]
    runs = desk.runs
    await asyncio.sleep(0.3)
    assert desk.runs == runs                                                # known: back to the slow net


async def test_the_loop_waits_until_the_bot_is_ready(game_db, loop, monkeypatch):
    game = game_db
    ready = asyncio.Event()

    async def wait_until_ready():
        await ready.wait()

    monkeypatch.setattr(game.bot, "wait_until_ready", wait_until_ready)
    seen = spy(monkeypatch)
    loop()
    await asyncio.sleep(0.2)
    assert seen.calls == 0 and not collector.pending(game.bot)
    ready.set()
    assert await until(lambda: seen.calls == 1)


# -- what asks for a synchronisation: the album ---------------------------------------------------

async def test_a_kept_sacrifice_asks_for_a_synchronisation(game_db, sacrifices):  # noqa: F811
    game = game_db
    db = game.bot.db
    ala = supporter(game, "Ala")
    await own_pet(db, ala.id, 5, pet_lvl=2)
    confirms(game, ala)
    assert not collector.pending(game.bot)
    assert await pets.discard_pet(game.scene(ala)) is True
    assert collector.pending(game.bot)
    assert rank_roles(game) == [] and announcements(game) == []            # only a flag: nothing was done here

    ola = game.add_member("Ola")                                           # no support: nothing is kept
    await own_pet(db, ola.id, 6, pet_lvl=1)
    collector._desk(game.bot).wanted.clear()
    game.channel.send_hooks.clear()
    confirms(game, ola)
    assert await pets.discard_pet(game.scene(ola)) is True
    assert not collector.pending(game.bot)


async def test_the_sacrifice_that_takes_the_lead_gets_the_rank_from_the_loop(game_db, sacrifices, loop):  # noqa: F811
    game = game_db
    db = game.bot.db
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    await cards(db, ala.id, 1)
    await put(db, ola.id, "Dog", 0)
    desk = collector._desk(game.bot)
    loop()
    assert await until(lambda: desk.runs == 1)
    assert holders(game) == ["Ala"]                                        # 10 : 10, Ala was first
    await own_pet(db, ola.id, 5, pet_lvl=2)
    confirms(game, ola)
    assert await pets.discard_pet(game.scene(ola)) is True
    assert await until(lambda: holders(game) == ["Ola"])
    assert await until(lambda: announcements(game) == [announced_text(ala), announced_text(ola)])


async def test_album_saved_asks_even_when_its_answer_cannot_be_sent(game_db):
    game = game_db
    ala = supporter(game, "Ala")
    game.channel.fail_with, game.channel.fail_sends = forbidden(), 5
    await album.saved(game.scene(ala), ala)
    assert collector.pending(game.bot)


async def test_an_entry_removed_in_the_panel_asks_for_a_synchronisation(cogs):  # noqa: F811
    game = cogs
    db = game.bot.db
    ala = supporter(game, "Ala")
    await put(db, ala.id, "Cat", 0)
    await put(db, ala.id, "Cat", 1)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    assert not collector.pending(game.bot)                                  # looking asks for nothing
    game.auto_click(ala, "no")
    await game.click(panel, ala, "Usuń z albumu")                           # asked, not confirmed
    assert not collector.pending(game.bot) and len(await album.entries(db, ala.id)) == 2
    game.channel.send_hooks.clear()
    game.auto_click(ala, "yes")
    await game.click(panel, ala, "Usuń z albumu")
    assert len(await album.entries(db, ala.id)) == 1 and collector.pending(game.bot)


# -- what asks for a synchronisation: the member events -------------------------------------------

def as_before(member: FakeMember, roles) -> FakeMember:
    """The member as he was before an update: the same id, other roles."""
    before = FakeMember(member.name, member_id=member.id, guild=member.guild)
    before.roles = list(roles)
    return before


def asked(bot) -> bool:
    """True when a synchronisation was requested since the last look."""
    wanted = collector._desk(bot).wanted
    was = wanted.is_set()
    wanted.clear()
    return was


async def test_a_change_of_support_asks_for_a_synchronisation(game_db):
    game = game_db
    bot = game.bot
    ala = supporter(game, "Ala")
    plain = as_before(ala, [])
    assert collector.member_changed(bot, plain, ala) is True and asked(bot)          # support began
    assert collector.member_changed(bot, ala, plain) is True and asked(bot)          # support ended
    twitch = as_before(ala, [game.guild.add_role(patrons.TWITCH_SUB_ROLE)])
    assert collector.member_changed(bot, ala, twitch) is False and not asked(bot)    # another way to support
    renamed = as_before(ala, ala.roles)
    renamed.nick = "Alicja"
    assert collector.member_changed(bot, ala, renamed) is False and not asked(bot)   # a nick: nothing
    hunter = as_before(ala, [*ala.roles, game.guild.get_role(bot.ids.hunters_role)])
    assert collector.member_changed(bot, ala, hunter) is False and not asked(bot)    # any other role: nothing


async def test_the_rank_changed_by_hand_asks_and_the_bots_own_changes_do_not(game_db):
    game = game_db
    bot = game.bot
    ala = await lead(game)
    role = rank_role(game)
    ola = supporter(game, "Ola")
    without = as_before(ala, [r for r in ala.roles if r != role])
    assert collector.member_changed(bot, without, ala) is False and not asked(bot)   # what the run just did
    assert collector.member_changed(bot, ala, without) is True and asked(bot)        # taken from the holder
    with_role = as_before(ola, [*ola.roles, role])
    assert collector.member_changed(bot, ola, with_role) is True and asked(bot)      # given to somebody else
    assert collector.member_changed(bot, with_role, ola) is False and not asked(bot)  # ... and taken back: fine

    await cards(bot.db, ola.id, 9)                                                   # Ola takes over
    await collector.sync(bot)
    assert collector._desk(bot).holder_id == ola.id
    assert collector.member_changed(bot, ala, without) is False and not asked(bot)   # the bot took Ala's
    assert collector.member_changed(bot, ola, with_role) is False and not asked(bot)  # the bot gave Ola's


async def test_the_events_of_a_run_come_before_it_ends_and_still_ask_for_nothing(game_db, monkeypatch):
    """Discord's event of a role change can arrive before the request that made it has answered."""
    game = game_db
    bot = game.bot
    ala = supporter(game, "Ala")
    await cards(bot.db, ala.id, 1)
    real = FakeMember.add_roles
    events = []

    async def add_roles(self, *roles, reason=None):
        before = as_before(self, self.roles)
        await real(self, *roles, reason=reason)
        events.append(collector.member_changed(bot, before, self))

    monkeypatch.setattr(FakeMember, "add_roles", add_roles)
    await collector.sync(bot)
    assert events == [False] and not collector.pending(bot) and holders(game) == ["Ala"]


async def test_a_supporter_or_the_holder_who_leaves_asks_for_a_synchronisation(game_db):
    game = game_db
    bot = game.bot
    ala = await lead(game)
    role = rank_role(game)
    assert collector.member_left(bot, game.add_member("Ktoś")) is False and not asked(bot)
    assert collector.member_left(bot, supporter(game, "Ola")) is True and asked(bot)
    lapsed = as_before(ala, [role])                                                  # the holder, whatever else
    assert collector.member_left(bot, lapsed) is True and asked(bot)
    bare = as_before(ala, [])                                                        # ... even without a role
    assert collector.member_left(bot, bare) is True and asked(bot)


async def test_events_of_another_server_and_odd_events_ask_for_nothing(game_db):
    game = game_db
    bot = game.bot
    ala = supporter(game, "Ala")
    elsewhere = SimpleNamespace(id=1)
    there = FakeMember("Ala", member_id=ala.id, guild=elsewhere)
    there.roles = list(ala.roles)
    assert collector.member_changed(bot, as_before(there, []), there) is False
    assert collector.member_left(bot, there) is False
    assert collector.member_changed(bot, None, None) is False and collector.member_left(bot, None) is False
    assert collector.member_changed(bot, object(), SimpleNamespace(guild=game.guild)) is False
    assert not asked(bot)


# -- the cog --------------------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cog(game_db, monkeypatch):
    """The Collector cog with its loop only recorded, not started."""
    game = game_db
    started: list = []
    monkeypatch.setattr(game.bot.supervisor, "start", lambda name, factory: started.append((name, factory)))
    await game.bot.load_extension("altermmo.cogs.collector")
    for channel in game.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    game.started = started
    return game


def administrator(game, name: str = "Alter") -> FakeMember:
    member = game.add_member(name)
    member.guild_permissions = discord.Permissions(administrator=True)
    return member


async def test_the_extension_starts_the_supervised_loop_and_registers_the_command(cog):
    game = cog
    bot = game.bot
    assert "altermmo.cogs.collector" in bot_module.EXTENSIONS
    ((name, factory),) = game.started
    assert name == collector.LOOP_NAME == "collector"
    coroutine = factory()                                                   # a fresh coroutine at every start
    assert asyncio.iscoroutine(coroutine) and coroutine.cr_code.co_name == "_loop"
    coroutine.close()
    the_cog = bot.get_cog("Collector")
    assert type(the_cog).__name__ == "CollectorCog" and type(the_cog).__module__ == "altermmo.cogs.collector"
    assert {command.name: command.aliases for command in the_cog.get_commands()} == {"kolekcjoner": []}
    command = bot.get_command("kolekcjoner")
    assert any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)
    assert {name for name, _ in the_cog.get_listeners()} == {"on_member_update", "on_member_remove"}
    assert bot.intents.members                                              # without it: no member events


async def test_unloading_the_extension_stops_the_loop(game_db, fast):
    game = game_db
    bot = game.bot
    await bot.load_extension("altermmo.cogs.collector")
    assert bot.supervisor.is_running(collector.LOOP_NAME)
    assert await until(lambda: collector._desk(bot).runs == 1)              # the real loop, started by the cog
    await bot.unload_extension("altermmo.cogs.collector")
    assert collector.LOOP_NAME not in bot.supervisor.names()


async def test_the_member_events_reach_the_cog(cog):
    game = cog
    bot = game.bot
    ala = supporter(game, "Ala")
    bot.dispatch("member_update", as_before(ala, []), ala)
    await game.settle()
    assert asked(bot)
    bot.dispatch("member_update", ala, as_before(ala, ala.roles))
    await game.settle()
    assert not asked(bot)
    bot.dispatch("member_remove", ala)
    await game.settle()
    assert asked(bot)


async def test_kolekcjoner_synchronises_now_and_says_what_happened(cog, monkeypatch):
    game = cog
    bot = game.bot
    alter = administrator(game)
    ala, ola = supporter(game, "Ala"), supporter(game, "Ola")
    sent = recorded(game, monkeypatch, game.announce_channel)

    await run(game, alter, "$kolekcjoner", channel=game.announce_channel)   # nobody has an album yet
    assert sent[-1][0].content == collector.MSG_NOBODY_UNCHANGED

    await cards(bot.db, ala.id, 2)
    collector.request(bot, "test")
    await run(game, alter, "$kolekcjoner", channel=game.announce_channel)
    answer, mentions = sent[-1]
    assert answer.content == " ".join([collector.MSG_HOLDER.format(player_id=ala.id), collector.MSG_GIVEN,
                                       collector.MSG_ANNOUNCED])
    assert mentions is not None and not mentions.users and not mentions.everyone and not mentions.roles
    assert holders(game) == ["Ala"] and announcements(game) == [announced_text(ala)]
    assert not collector.pending(bot)                                       # what was asked for is done

    await run(game, alter, "$kolekcjoner", channel=game.announce_channel)
    assert sent[-1][0].content == collector.MSG_HOLDER_UNCHANGED.format(player_id=ala.id)

    await cards(bot.db, ola.id, 5)
    await run(game, alter, "$kolekcjoner", channel=game.announce_channel)
    assert sent[-1][0].content == " ".join([
        collector.MSG_HOLDER.format(player_id=ola.id), collector.MSG_GIVEN,
        collector.MSG_REMOVED.format(players=f"<@{ala.id}>"), collector.MSG_ANNOUNCED])
    assert holders(game) == ["Ola"] and len(sent) == 4                      # one public answer per command
    assert all(message.channel is game.announce_channel for message, _ in sent)


async def test_kolekcjoner_says_why_it_could_not_run(cog, monkeypatch):
    game = cog
    bot = game.bot
    alter = administrator(game)
    ala, ola, state, _ = await overtaken(game)

    with bot.state.hold_busy("event:chest"):                                # something plays: not now
        await run(game, alter, "$kolekcjoner", channel=game.announce_channel)
    assert game.announce_channel.sent[-1].content == collector.MSG_BUSY
    assert untouched(game, ala, state) and asked(bot)                       # ... but left for the loop,
    assert collector._desk(bot).force_next is True                          # with its "try the role now"

    real = bot.db
    bot.db = DatabaseDown()
    await run(game, alter, "$kolekcjoner", channel=game.announce_channel)
    assert game.announce_channel.sent[-1].content == collector.MSG_NO_RANKING.format(
        detail=": DatabaseUnavailable")
    assert "spróbuję ponownie sam" in collector.MSG_NO_RANKING and asked(bot)   # the promise is left for the loop
    bot.db = real

    with monkeypatch.context() as patch:
        patch.setattr(game.guild, "unavailable", True, raising=False)
        await run(game, alter, "$kolekcjoner", channel=game.announce_channel)
    assert game.announce_channel.sent[-1].content == collector.MSG_NO_GUILD
    assert "spróbuję ponownie sam" in collector.MSG_NO_GUILD and asked(bot)

    path = bot.store.collector.path
    good = path.read_text(encoding="utf-8")
    path.write_text("{", encoding="utf-8")
    await run(game, alter, "$kolekcjoner", channel=game.announce_channel)
    assert game.announce_channel.sent[-1].content == collector.MSG_NO_STATE
    assert "ponownie" not in collector.MSG_NO_STATE and not asked(bot)       # (the owner has to mend the file)
    path.write_text(good, encoding="utf-8")
    assert untouched(game, ala, state)


async def test_kolekcjoner_is_for_administrators_only(cog):
    game = cog
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 2)
    before = len(game.channel.sent)
    await run(game, ala, "$kolekcjoner")
    assert len(game.channel.sent) == before and rank_roles(game) == []
    assert collector._desk(game.bot).runs == 0


def test_every_answer_of_the_command_is_plain_polish_without_numbers_to_spoil():
    outcomes = [collector.Outcome(status) for status in (collector.BUSY, collector.NO_GUILD, collector.NO_STATE,
                                                         collector.NO_RANKING, collector.NO_ROLE,
                                                         collector.FAILED, collector.DONE, "odd")]
    outcomes.append(collector.Outcome(collector.TEST_RUN))
    outcomes += [collector.Outcome(collector.DONE, removed=(1, 2)),
                 collector.Outcome(collector.DONE, leader_id=7, holds=True, not_removed=(3,),
                                   announcement=collector.NOT_ANNOUNCED),
                 collector.Outcome(collector.DONE, leader_id=7)]
    texts = [collector.describe(outcome) for outcome in outcomes]
    assert all(text and len(text) < 1990 and "%" not in text and "lobby" not in text for text in texts)
    assert texts[9] == collector.MSG_NOBODY + " " + collector.MSG_REMOVED.format(players="<@1>, <@2>")
    assert texts[10] == " ".join([collector.MSG_HOLDER.format(player_id=7),
                                 collector.MSG_NOT_REMOVED.format(players="<@3>"), collector.MSG_ANNOUNCE_FAILED])


# -- the rank is no achievement -------------------------------------------------------------------

def test_the_rank_is_not_in_the_catalogue_of_achievements():
    assert NAME not in {item.name for item in achievements.TROPHIES}
    assert not any("kolekcjoner" in item.key.lower() or "collector" in item.key.lower()
                   for item in achievements.TROPHIES)


async def test_the_role_is_saved_in_a_file_of_its_own_never_with_the_achievements(game_db):
    game = game_db
    bot = game.bot
    await lead(game)
    role = rank_role(game)
    await achievements.ensure_roles(bot)                                    # the achievements make theirs
    own = bot.store.collector.path
    assert own.name == "petCollector.json" and own.parent == bot.store.data_dir
    assert json.loads(own.read_text(encoding="utf-8"))["role_id"] == role.id
    later = bot.store.achievement_roles.path.with_name(achievements.LATER_ROLES_FILE)
    # What retire_roles - of this or of an older version - reads:
    for path in (bot.store.achievement_roles.path, later):
        assert path.is_file() and str(role.id) not in path.read_text(encoding="utf-8"), path
    assert role.id not in achievements._stored_roles(bot).values()
    assert role.id not in {role_id for role_id, _, _ in achievements._withdrawn(bot)}


async def test_the_achievements_never_delete_or_count_the_rank(game_db):
    game = game_db
    bot = game.bot
    ala = await lead(game)
    role = rank_role(game)
    # A role the bot made earlier for an achievement that was withdrawn: THAT one goes.
    doomed = game.guild.add_role(990_000_000_000_000_501, "Kultysta II")
    bot.store.achievement_roles.save({"rituals_20": doomed.id})
    await achievements.start_up(bot)                                        # retire, make, hand out
    assert doomed not in game.guild.roles
    assert rank_roles(game) == [role] and role in ala.roles                 # the rank: untouched
    assert all(NAME not in line for line in bot.owner_log[1:])
    assert await achievements.retire_roles(bot) == [] and rank_roles(game) == [role]

    embed = achievements.summary_embed(bot, ala)                            # $osiagniecia: "X z Y" without it
    assert f"masz **0 z {len(achievements.TROPHIES)}** osiągnięć" in embed.description
    assert str(role.id) not in embed.description and achievements.held(bot, ala) == []
    await collector.sync(bot)                                               # and the rank leaves theirs alone
    assert {item.name for item in achievements.TROPHIES if item.bot_made} <= {r.name for r in game.guild.roles}
    assert holders(game) == ["Ala"]


# -- what the players are told --------------------------------------------------------------------

async def test_the_ranking_embed_says_what_place_one_carries(game_db):
    game = game_db
    ala = supporter(game, "Ala")
    await cards(game.bot.db, ala.id, 2)
    for board in (await albumrank.ranking(game.bot), albumrank.Ranking(())):   # also while nobody is ranked
        embed = albumrank.ranking_embed(board, ala)
        ((name, value),) = [(item.name, item.value) for item in embed.fields]
        assert name == "👑 Kolekcjoner Pupili" == collector.MSG_RANKING_NAME
        assert value == collector.MSG_RANKING_LINE
        assert "pierwszego miejsca" in value and "dodatkową szansę w giveawayach" in value
        assert embed.footer.text == albumrank.MSG_HOW                       # the supporters' rules: as they were
        for word in ("%", "<@", "lobby", "Horus"):
            assert word not in name + value, word
    assert patchnotes.spoiler_in_text(collector.MSG_RANKING_LINE) is None
    assert patchnotes.spoiler_in_text(collector.MSG_ANNOUNCE) is None


def test_the_help_mentions_the_rank_where_the_collection_ranking_is_described():
    line = next(line for line in help_cog.HELP_TEXT.split("\n") if line.startswith("- $rankingalbumow"))
    assert "**Kolekcjoner Pupili**" in line and "giveaway" in line and "wspierających" in line
    assert "$kolekcjoner" not in help_cog.HELP_TEXT                         # the administrators' command: not there
    embed = help_cog.help_embed()
    assert all(len(item.value) <= 1024 for item in embed.fields) and len(embed) <= 6000


def test_the_game_panel_names_the_rank_among_the_supporters_benefits():
    """The help sends players to the panel for "all the benefits of supporters": the rank is there,
    as one clause of the album's line (no number, no ornament, not the other bot's name)."""
    (line,) = [line for line in hub.PANEL_BENEFITS if "**Album**" in line]
    assert line == hub.PANEL_BENEFITS[-1] and line in hub.PANEL_SUPPORT_TEXT
    assert "rankingu kolekcji" in line and "pierwsze miejsce" in line
    assert f"**{collector.ROLE_NAME}**" in line and "giveaway" in line
    assert patchnotes.spoiler_in_text(line) is None and not any(character.isdigit() for character in line)
    for word in ("%", "<@", "lobby", "Horus", "$kolekcjoner"):
        assert word not in line, word
    assert len(line) <= 240 and len(hub.PANEL_TEXT) <= 4096


def test_the_patch_notes_tell_the_players():
    notes = {item.id: item for item in patchnotes.load_notes()}
    note = notes[10]
    assert note.date == "2026-11-23" and note.title == "Ranga Kolekcjoner Pupili"
    text = " ".join(note.changes)
    assert NAME in text and "giveaway" in text and "$rankingalbumow" in text
    assert "ranking wspierających" in text                                  # said honestly: supporters only
    assert patchnotes.spoiler_in(note) is None and "lobby" not in text and "Horus" not in text
    assert [item.id for item in notes.values()] == list(range(1, len(notes) + 1))
