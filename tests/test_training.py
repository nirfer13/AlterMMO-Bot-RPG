"""features/training.py: the "Trening towarzyszy" event - the simulation, the draw, the sign-up,
the stable during the sign-up, the bracket, the fight message, the rewards."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import random
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from altermmo.cogs import events as events_cog
from altermmo.core import challenge, clock, guards, timing, ui
from altermmo.core.scene import Scene
from altermmo.core.state import EventType, GameState
from altermmo.features import events, hub, pets, training

from fakes import FakeInteraction, FakeMessage
from test_events import clicks_on, files, no_listeners, no_live_buttons, panel
from test_pets import add_owner, add_pet, owner_row, run, until
from test_pets import plain_cards  # noqa: F401 - autouse: pets are sent as their plain pictures here too

TITLE = "Trening towarzyszy!"
EVENING = datetime(2026, 11, 17, 19, 0, tzinfo=clock.WARSAW)   # a Tuesday: peak hours
NOON = datetime(2026, 11, 17, 12, 0, tzinfo=clock.WARSAW)      # ... and off-peak

_pet_ids = itertools.count(100)


# -- helpers -------------------------------------------------------------------------

class Luck:
    """A scripted source of luck: what ``training.simulate`` draws, in the order it draws it.

    ``starter``: who strikes first; ``bases``: the blows (then 250 for ever);
    ``rolls``: the answers of ``random()`` (then 0.5: a 100 % chance succeeds, anything
    up to 50 % fails).
    """

    def __init__(self, starter: int = 0, bases=(), rolls=()) -> None:
        self.starter = starter
        self._bases = iter(bases)
        self._rolls = iter(rolls)
        self.asked: list = []

    def randrange(self, stop: int) -> int:
        self.asked.append(("randrange", stop))
        return self.starter

    def randint(self, low: int, high: int) -> int:
        self.asked.append(("randint", low, high))
        return next(self._bases, 250)

    def random(self) -> float:
        self.asked.append("random")
        return next(self._rolls, 0.5)


def fighter(game, name: str, pet: str | None = None, **stats) -> training.Fighter:
    """A companion ready for the bracket (no database): ``stats`` as ``crit_perc=20``."""
    values = {key: 0.0 for key in training.STAT_KEYS}
    values.update({key.upper(): float(value) for key, value in stats.items()})
    return training.Fighter(member=game.add_member(name), pet_id=next(_pet_ids), name=pet or f"Pupil {name}",
                            species="Kot", level=1, stats=values)


async def with_pet(game, name: str, pet_id: int, *, level: int = 1, pet_name: str = "Towarzysz",
                   species: str = "Cat", owner: dict | None = None, **columns):
    """A member whose active companion is ``pet_id``."""
    member = game.add_member(name)
    await add_owner(game.bot.db, member.id, pet_id, **(owner or {}))
    await add_pet(game.bot.db, pet_id, pet_lvl=level, pet_name=pet_name, type=species, **columns)
    return member


async def slots(db, player) -> list:
    row = await owner_row(db, player.id)
    return [row[name] for name in ("pet_id", "pet_id_alt1", "pet_id_alt2", "pet_id_alt3", "pet_id_alt4")]


def fight_messages(game) -> list:
    """The fight messages of the channel: one per fight, in order."""
    return [message for message in game.channel.sent
            if message.embeds and " kontra " in str(message.embeds[0].title or "")]


def bot_lines(game, since: int) -> list[str]:
    return [message.content for message in game.channel.messages[since:] if message.author is game.bot.user]


async def typed_is_busy(game, member, command: str) -> bool:
    """``member`` types ``command``: True when the only answer is the busy notice."""
    guards._last_notice.clear()
    before = len(game.channel.messages)
    await run(game, member, command)
    return bot_lines(game, before) == [guards.BUSY_NOTICE]


async def clicked_is_busy(game, message, member, key: str, **kwargs) -> bool:
    guards._last_notice.clear()
    return (await game.click(message, member, key, **kwargs)).answers == [ui.MSG_BUSY]


async def hub_stable(game, member) -> FakeInteraction:
    """``member`` clicks "Stajnia" on the pinned game panel."""
    guards._last_notice.clear()
    interaction = FakeInteraction(game.bot, member, channel=game.channel)
    await hub.HubView().stable.callback(interaction)
    return interaction


# -- fixtures ------------------------------------------------------------------------

@pytest.fixture
def folder(tmp_path, monkeypatch):
    """The pictures of the event live in an (empty, not yet existing) temporary folder."""
    path = tmp_path / "training"
    monkeypatch.setattr(training, "PICTURES_DIR", path)
    return path


@pytest.fixture
def picture(folder):
    """The owner has supplied ``0.png``."""
    folder.mkdir()
    (folder / "0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return folder / "0.png"


@pytest.fixture
def enrol(monkeypatch):
    """``enrol(*fighters, dropped=[...])``: the sign-up closes at once with these companions."""
    def enrol(*fighters, dropped=()):
        async def collect(self):
            self.members.extend([one.member for one in fighters] + list(dropped))
            return list(self.members)

        async def snapshot(bot, members):
            return list(fighters), list(dropped)

        monkeypatch.setattr(training._TrainingSignUp, "collect", collect)
        monkeypatch.setattr(training, "_snapshot", snapshot)

    return enrol


@pytest.fixture
def sign_up(game, monkeypatch):
    """A real sign-up that stays open until the test calls ``await sign_up.close()``."""
    box = SimpleNamespace(closed=asyncio.Event())
    monkeypatch.setattr(training, "TRAINING_SIGNUP", (30, 30))   # (a forgotten close() ends the test all the same)
    real = training._TrainingSignUp.next_member

    async def next_member(self):
        waiting = asyncio.ensure_future(real(self))
        closing = asyncio.ensure_future(box.closed.wait())
        await asyncio.wait({waiting, closing}, return_when=asyncio.FIRST_COMPLETED)
        closing.cancel()
        if not waiting.done():
            # Closed: a click that was made before the close is still taken (a moment is enough).
            await asyncio.wait({waiting}, timeout=0.05)
        if waiting.done():
            return waiting.result()
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        return None

    async def close() -> None:
        await game.settle()      # the clicks made so far are on the roster
        box.closed.set()

    monkeypatch.setattr(training._TrainingSignUp, "next_member", next_member)
    box.close = close
    return box


@pytest.fixture
def fights(monkeypatch):
    """Every fight is recorded; ``hold()`` makes the fights wait for ``release()``, ``fail_on`` breaks one."""
    record = SimpleNamespace(pairs=[], results=[], gate=asyncio.Event(), fail_on=None)
    record.gate.set()
    record.hold = record.gate.clear
    record.release = record.gate.set
    real = training.fight

    async def fight(scene, first, second, rng=random):
        record.pairs.append((first, second))
        await record.gate.wait()
        if record.fail_on == len(record.pairs):
            raise RuntimeError("Discord 503")
        result = await real(scene, first, second, rng)
        record.results.append(result)
        return result

    monkeypatch.setattr(training, "fight", fight)
    return record


@pytest.fixture
def rewards(monkeypatch):
    """Record the reward calls instead of granting; ``rewards.broken`` names the calls that fail."""
    record = SimpleNamespace(shards=[], scrolls=[], loot=[], broken=set())

    async def assign_shard(scene, number, player_id):
        if ("shard", player_id) in record.broken:
            raise RuntimeError("database down")
        record.shards.append((number, player_id))
        return True

    async def assign_scroll(scene, number, player_id):
        if ("scroll", player_id) in record.broken:
            raise RuntimeError("database down")
        record.scrolls.append((number, player_id))
        return True

    async def rand_loot(scene, rarity, hunter, boost_percent):
        if ("loot", hunter.id) in record.broken:
            raise RuntimeError("loot table unreadable")
        record.loot.append((rarity, hunter, boost_percent))
        return ""

    monkeypatch.setattr(training.pets, "assign_shard", assign_shard)
    monkeypatch.setattr(training.pets, "assign_scroll", assign_scroll)
    monkeypatch.setattr(training.daily, "rand_loot", rand_loot)
    return record


@pytest.fixture
def sent_with(game, monkeypatch):
    """``allowed_mentions`` each message of the boss channel was sent with, by message id."""
    seen: dict = {}
    real = game.channel.send

    async def send(content=None, **kwargs):
        message = await real(content, **kwargs)
        seen[message.id] = kwargs.get("allowed_mentions")
        return message

    monkeypatch.setattr(game.channel, "send", send)
    return seen


@pytest.fixture
def paced(monkeypatch):
    """What the show of the fights did, in order: ``("pause", seconds)`` and ``("edit", message id)``."""
    steps: list = []
    real = training._edit

    async def pause(seconds):
        steps.append(("pause", seconds))

    async def edit(message, embed):
        steps.append(("edit", message.id))
        return await real(message, embed)

    monkeypatch.setattr(timing, "pause", pause)
    monkeypatch.setattr(training, "_edit", edit)
    return steps


@pytest.fixture
def in_order(monkeypatch):
    """No shuffling: the bracket is played in the order of the sign-up."""
    monkeypatch.setattr(random, "shuffle", lambda items: None)


@pytest_asyncio.fixture
async def pets_cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.pets")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    return game_db.bot.get_cog("Pets")


def edits_of_fights(monkeypatch, fail) -> list:
    """Make edits of the fight messages fail: ``fail(number, kwargs)`` returns an exception, a
    coroutine function to await first, or None.  Returns the list of the edits tried."""
    tried: list = []
    real = FakeMessage.edit

    async def edit(self, **kwargs):
        if self.embeds and " kontra " in str(self.embeds[0].title or ""):
            tried.append(kwargs)
            trouble = fail(len(tried), kwargs)
            if isinstance(trouble, BaseException):
                raise trouble
            if trouble is not None:
                await trouble()
        return await real(self, **kwargs)

    monkeypatch.setattr(FakeMessage, "edit", edit)
    return tried


def says_who_won(kwargs) -> bool:
    return "Wygrywa" in str(kwargs["embed"].description)


# ===================================== the simulation ==================================

def test_a_seeded_fight_is_always_the_same_fight():
    sparring = training.simulate({}, {}, random.Random(1))
    assert sparring == training.simulate({}, {}, random.Random(1))
    assert (sparring.start_life, sparring.first, len(sparring.blows), sparring.winner) == ((3000, 3000), 0, 22, 1)
    assert sparring.blows[-1] == training.Blow(attacker=1, base=291, damage=291, blocked=0, critical=False,
                                               full_power=False, life=(0, 679))
    assert sparring.life_after(0) == (3000, 3000) and sparring.life_after(22) == (0, 679)


def test_plain_blows_are_100_to_400_and_the_companions_strike_in_turns():
    luck = Luck(starter=1, bases=[100, 400, 250])
    sparring = training.simulate({}, {}, luck)
    assert sparring.first == 1
    assert [(blow.attacker, blow.base, blow.damage, blow.life) for blow in sparring.blows[:3]] == [
        (1, 100, 100, (2900, 3000)), (0, 400, 400, (2900, 2600)), (1, 250, 250, (2650, 2600))]
    assert [blow.attacker for blow in sparring.blows] == [(1 + turn) % 2 for turn in range(len(sparring.blows))]
    assert not any(blow.critical or blow.full_power or blow.blocked for blow in sparring.blows)
    # What is drawn, in order: the starter, then per blow the full-power roll, the blow, the critical roll.
    assert luck.asked[:7] == [("randrange", 2), "random", ("randint", 100, 400), "random",
                              "random", ("randint", 100, 400), "random"]
    # 400 + 11 x 250 >= 3000 comes one blow before 100 + 12 x 250 does: the one who hit harder wins.
    assert sparring.winner == 0 and len(sparring.blows) == 24 and sparring.blows[-1].life == (150, 0)


def test_replace_means_full_power():
    luck = Luck(bases=[100] * 40)
    sparring = training.simulate({"REPLACE_PERC": 100}, {}, luck)
    mine = [blow for blow in sparring.blows if blow.attacker == 0]
    assert mine and all(blow.full_power and blow.base == 400 and blow.damage == 400 for blow in mine)
    theirs = [blow for blow in sparring.blows if blow.attacker == 1]
    assert all(not blow.full_power and blow.damage == 100 for blow in theirs)
    assert luck.asked.count(("randint", 100, 400)) == len(theirs)      # no blow is drawn for a full-power one
    # The chance is a chance: a roll of 0.29 gives full power at 30 %, 0.30 does not.
    lucky = training.simulate({"REPLACE_PERC": 30}, {}, Luck(bases=[100], rolls=[0.29]))
    unlucky = training.simulate({"REPLACE_PERC": 30}, {}, Luck(bases=[100], rolls=[0.30]))
    assert (lucky.blows[0].full_power, lucky.blows[0].damage) == (True, 400)
    assert (unlucky.blows[0].full_power, unlucky.blows[0].damage) == (False, 100)


def test_slow_multiplies_the_companions_own_blows():
    sparring = training.simulate({"SLOW_PERC": 50}, {}, Luck(bases=[200] * 40))
    assert {blow.damage for blow in sparring.blows if blow.attacker == 0} == {300}
    assert {blow.damage for blow in sparring.blows if blow.attacker == 1} == {200}


def test_crit_doubles_the_blow():
    sparring = training.simulate({"CRIT_PERC": 100}, {}, Luck(bases=[150] * 40))
    assert all(blow.critical and blow.damage == 300 for blow in sparring.blows if blow.attacker == 0)
    assert all(not blow.critical and blow.damage == 150 for blow in sparring.blows if blow.attacker == 1)
    # (rolls: the first blow's full-power roll, then its critical roll)
    assert training.simulate({"CRIT_PERC": 20}, {}, Luck(bases=[150], rolls=[0.9, 0.19])).blows[0].damage == 300
    assert training.simulate({"CRIT_PERC": 20}, {}, Luck(bases=[150], rolls=[0.9, 0.20])).blows[0].damage == 150


def test_def_reduces_the_incoming_blows():
    sparring = training.simulate({}, {"DEF_PERC": 25}, Luck(bases=[200] * 40))
    assert {(blow.damage, blow.blocked) for blow in sparring.blows if blow.attacker == 0} == {(150, 50)}
    assert {(blow.damage, blow.blocked) for blow in sparring.blows if blow.attacker == 1} == {(200, 0)}
    # Whole percents, as the duel's ``reduction``: 33.9 % blocks 33 %.
    blow = training.simulate({}, {"DEF_PERC": 33.9}, Luck(bases=[200])).blows[0]
    assert (blow.damage, blow.blocked) == (134, 66)


def test_lowhp_lowers_the_opponents_starting_life():
    assert training.simulate({"LOWHP_PERC": 20}, {}, Luck()).start_life == (3000, 2400)
    assert training.simulate({"LOWHP_PERC": 20}, {"LOWHP_PERC": 5}, Luck()).start_life == (2850, 2400)
    assert training.simulate({"LOWHP_PERC": 100}, {}, Luck()).start_life == (3000, 1)       # never below 1
    assert training.simulate({"LOWHP_PERC": 250}, {}, Luck()).start_life == (3000, 1)
    assert training.simulate({"LOWHP_PERC": -50}, {}, Luck()).start_life == (3000, 3000)    # never above the full life
    # With less life the fight is shorter: 2400 falls after 10 blows of 250.
    sparring = training.simulate({"LOWHP_PERC": 20}, {}, Luck(starter=0))
    assert sparring.winner == 0 and len(sparring.blows) == 19 and sparring.blows[-1].life[1] == 0


def test_all_the_stats_work_together():
    strong = {"REPLACE_PERC": 100, "SLOW_PERC": 50, "CRIT_PERC": 100, "LOWHP_PERC": 10}
    sparring = training.simulate(strong, {"DEF_PERC": 10}, Luck(starter=0, bases=[100] * 40))
    blow = sparring.blows[0]                      # 400 x 1.5 x 2 = 1200, a tenth of it blocked
    assert (blow.base, blow.full_power, blow.critical, blow.damage, blow.blocked) == (400, True, True, 1080, 120)
    assert sparring.start_life == (3000, 2700) and blow.life == (3000, 1620)
    assert sparring.winner == 0 and len(sparring.blows) == 5 and sparring.blows[-1].life == (2800, 0)


def test_forty_blows_without_a_winner_are_a_draw():
    sparring = training.simulate({"DEF_PERC": 100}, {"DEF_PERC": 100}, Luck())
    assert sparring.winner is None and len(sparring.blows) == training.MAX_BLOWS == 40
    assert all((blow.damage, blow.blocked, blow.life) == (0, 250, (3000, 3000)) for blow in sparring.blows)
    assert [blow.attacker for blow in sparring.blows] == [0, 1] * 20
    # A defence above 100 % does not heal.
    assert all(blow.damage == 0 for blow in training.simulate({}, {"DEF_PERC": 130}, Luck()).blows
               if blow.attacker == 0)
    # A real pair that cannot hurt each other enough: both block 60 %.
    tough = training.simulate({"DEF_PERC": 60}, {"DEF_PERC": 60}, random.Random(4))
    assert tough.winner is None and len(tough.blows) == 40 and min(tough.blows[-1].life) > 0


def test_life_is_never_below_zero_and_the_last_blow_is_the_winners():
    stats = random.Random(99)
    for seed in range(300):
        first, second = ({key: stats.choice([0, 0, 5, 20, 45]) for key in training.STAT_KEYS} for _ in range(2))
        sparring = training.simulate(first, second, random.Random(seed))
        assert 1 <= len(sparring.blows) <= training.MAX_BLOWS
        before = sparring.start_life
        for turn, blow in enumerate(sparring.blows):
            assert blow.attacker == (sparring.first + turn) % 2
            assert min(blow.life) >= 0 and blow.damage >= 0 and blow.blocked >= 0
            foe = 1 - blow.attacker
            assert blow.life[blow.attacker] == before[blow.attacker]
            assert blow.life[foe] == max(0, before[foe] - blow.damage)
            assert training.BLOW_MIN <= blow.base <= training.BLOW_MAX
            before = blow.life
        if sparring.winner is None:
            assert len(sparring.blows) == training.MAX_BLOWS and min(before) > 0
        else:
            assert sparring.blows[-1].attacker == sparring.winner
            assert before[1 - sparring.winner] == 0 and before[sparring.winner] > 0
        assert all(min(blow.life) > 0 for blow in sparring.blows[:-1])        # the fight ends with the fall


def test_two_identical_companions_win_about_half_of_their_fights_each():
    stats = {"CRIT_PERC": 20, "DEF_PERC": 10, "SLOW_PERC": 15, "REPLACE_PERC": 10, "LOWHP_PERC": 5}
    wins = [0, 0]
    for seed in range(3000):
        sparring = training.simulate(stats, dict(stats), random.Random(seed))
        assert sparring.winner is not None
        wins[sparring.winner] += 1
    assert 0.46 < wins[0] / 3000 < 0.54, wins


def test_stats_may_be_decimals_or_missing_and_nothing_else_counts():
    row = {"crit_perc": Decimal("100"), "CRIT_PERC": Decimal("100"), "SLOW_PERC": None, "DEF_PERC": "x",
           "mythic": True, "ultra_shiny": True, "pet_lvl": 3, "seal": 3, "frame": "M1", "holo": True}
    with_ornaments = training.simulate(row, {}, Luck(bases=[100] * 40))
    assert with_ornaments == training.simulate({"CRIT_PERC": 100}, {}, Luck(bases=[100] * 40))
    assert with_ornaments.blows[0].damage == 200
    assert training.STAT_KEYS == ("CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "LOWHP_PERC", "SLOW_PERC")
    assert (training.START_LIFE, training.BLOW_MIN, training.BLOW_MAX) == (3000, 100, 400)


# ===================================== how a fight is shown =============================

@pytest.mark.parametrize("blows, expected", [
    (1, [1]), (2, [2]), (3, [2, 3]), (6, [2, 4, 6]),
    (23, [*range(2, 23, 2), 23]), (24, list(range(2, 25, 2))),
    (25, [4, 8, 12, 16, 20, 24, 25]), (40, list(range(4, 41, 4)))])
def test_one_exchange_per_edit_and_never_more_than_twelve_edits(blows, expected):
    counts = training.frame_counts(blows)
    assert counts == expected and counts[-1] == blows and len(counts) <= training.MAX_EDITS == 12


def test_the_pace_respects_discords_edit_budget_and_a_fight_takes_about_eighteen_seconds():
    assert training.EDIT_GAP_MIN >= 1.2
    for blows in range(1, training.MAX_BLOWS + 1):
        edits = len(training.frame_counts(blows))
        gap = training.edit_gap(edits)
        assert training.EDIT_GAP_MIN <= gap <= training.EDIT_GAP_MAX
        assert edits * gap <= 25
        if blows >= 9:                                  # (shorter fights are not stretched: 3 s per exchange)
            assert 15 <= edits * gap <= 18.01, blows
    assert training.edit_gap(12) == 1.5 and training.edit_gap(10) == pytest.approx(1.8)
    assert training.edit_gap(1) == 3.0


def test_life_bars():
    assert training.life_bar(3000, "🟥") == "🟥" * 10
    assert training.life_bar(1500, "🟦") == "🟦" * 5 + "⬛" * 5
    assert training.life_bar(1, "🟥") == "🟥" + "⬛" * 9          # still standing: one square
    assert training.life_bar(0, "🟥") == "⬛" * 10


RED, BLUE = training.SQUARES


@pytest.mark.parametrize("life, red", [
    ((3000, 3000), 7), ((1, 1), 7), ((2700, 3000), 7),          # even, or nearly: the middle
    ((3000, 1500), 9), ((1500, 3000), 5),                       # two thirds of the life left: 9.33 of 14 squares
    ((3000, 1000), 10), ((1000, 3000), 4), ((2400, 600), 11), ((600, 2400), 3),
    ((3000, 1), 13), ((1, 3000), 1), ((2999, 20), 13), ((20, 2999), 1),   # still standing: one square stays
    ((3000, 0), 14), ((0, 3000), 0), ((1, 0), 14), ((0, 1), 0)])          # fallen: none - one colour = over
def test_the_advantage_bar_is_split_by_the_two_lives(life, red):
    """Owner, 2026-11-23: who is winning, "w postaci pasków tak jak przy walce PvP"."""
    assert (RED, BLUE) == ("🟥", "🟦") and training.ADVANTAGE_LENGTH == 14
    assert training.advantage_bar(life) == RED * red + BLUE * (14 - red)


def test_the_advantage_bar_at_its_edges():
    assert training.advantage_bar((0, 0)) == "⬛" * 14                       # (no fight ends like this)
    assert training.advantage_bar((-5, 3000)) == BLUE * 14                  # life never shows below 0
    assert training.advantage_bar((Decimal(1500), 1500.0)) == RED * 7 + BLUE * 7
    mirror = str.maketrans(RED + BLUE, BLUE + RED)
    for first in range(0, 3001, 125):
        for second in range(0, 3001, 125):
            bar = training.advantage_bar((first, second))
            assert len(bar) == 14 and set(bar) <= {RED, BLUE, "⬛"}
            # Neither side is favoured: the same lives the other way round are the mirrored bar.
            assert training.advantage_bar((second, first)) == bar[::-1].translate(mirror)
            # More life never means fewer squares.
            if first:
                assert training.advantage_bar((first, second)).count(RED) >= \
                    training.advantage_bar((first - 125, second)).count(RED)
    # Even (equal lives meet in the middle) and no wider than a life bar with its numbers.
    assert training.ADVANTAGE_LENGTH % 2 == 0 and training.BAR_LENGTH < training.ADVANTAGE_LENGTH <= 16


def test_who_leads_is_who_has_more_life_left():
    assert training.leader((3000, 2999)) == 0 and training.leader((1, 2)) == 1 and training.leader((0, 5)) == 1
    assert training.leader((3000, 3000)) is None and training.leader((0, 0)) is None


def bar_and_lead(embed) -> tuple[str, str]:
    """The advantage bar of a fight message and the line under it ("" when the next line is empty)."""
    lines = embed.description.split("\n")
    bars = [index for index, line in enumerate(lines) if line and set(line) <= {RED, BLUE, "⬛"}]
    assert len(bars) == 1, embed.description                                 # one advantage bar, never two
    (at,) = bars
    # Between the two sides (four lines) and the blow log, set off by empty lines.
    assert at == 5 and lines[4] == "" and "" in lines[at + 1:at + 3]
    return lines[at], lines[at + 1]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_every_version_of_the_fight_message_shows_who_is_winning(game, seed):
    burek, mruczek = fighter(game, "Ala", "Burek", crit_perc=20), fighter(game, "Ola", "Mruczek", lowhp_perc=10)
    pair = (burek, mruczek)
    sparring = training.simulate(burek.stats, mruczek.stats, random.Random(seed))
    assert sparring.winner is not None
    counts = training.frame_counts(len(sparring.blows))
    seen = set()
    luck = random.getstate()
    for shown in (0, *counts):
        last = shown == len(sparring.blows)
        embed = training.fight_embed(pair, sparring, shown, winner=sparring.winner if last else None)
        life = sparring.life_after(shown)
        bar, lead = bar_and_lead(embed)
        assert bar == training.advantage_bar(life) and len(bar) == 14
        seen.add(bar)
        if last:                                                             # the final state; the result says who won
            assert lead == "" and "Prowadzi" not in embed.description and "wyrównana" not in embed.description
            assert bar == (RED if sparring.winner == 0 else BLUE) * 14
        elif life[0] == life[1]:
            assert lead == "⚖️ Walka wyrównana"
        else:
            ahead = 0 if life[0] > life[1] else 1
            assert lead == f"{training.SQUARES[ahead]} Prowadzi **{pair[ahead].shown}**"
            assert bar.count(training.SQUARES[ahead]) >= 7                   # the bar never contradicts the line
        # The life bars stay.
        assert embed.description.count(" / 3000") == 2
        assert len(embed.description) <= 4096 and len(embed) <= 6000
    assert len(seen) > 3                                                     # the bar moves as the fight goes
    # Showing the bar draws nothing, and the fight is the one its seed gives.
    assert random.getstate() == luck
    assert sparring == training.simulate(burek.stats, mruczek.stats, random.Random(seed))


def test_two_companions_of_one_name_are_told_apart_by_the_square(game):
    """Unnamed companions are called by their species, so "Kot" may well fight "Kot"."""
    pair = (fighter(game, "Ala", "Kot"), fighter(game, "Ola", "Kot"))
    sparring = training.simulate({}, {}, Luck(starter=0, bases=[400, 100]))
    after_one = training.fight_embed(pair, sparring, 1)                      # the first struck: the second is behind
    assert sparring.life_after(1) == (3000, 2600)
    assert bar_and_lead(after_one) == (RED * 8 + BLUE * 6, "🟥 Prowadzi **Kot**")
    after_two = training.fight_embed(pair, sparring, 2)                      # a small lead: the line still tells
    assert sparring.life_after(2) == (2900, 2600)
    assert bar_and_lead(after_two) == (RED * 7 + BLUE * 7, "🟥 Prowadzi **Kot**")
    start = training.fight_embed(pair, sparring, 0)
    assert bar_and_lead(start) == (RED * 7 + BLUE * 7, "⚖️ Walka wyrównana")


@pytest.mark.parametrize("starter", [0, 1])
def test_the_lead_line_shows_the_name_as_plain_text(game, starter):
    """A companion's name is its owner's free text: in the "Prowadzi" line it is escaped like everywhere
    else in the fight message (``Fighter.shown``) - it neither breaks the bold nor pings anybody."""
    role = "<@&985071779787730944>"
    pair = (fighter(game, "Ala", "**Bu_rek** @everyone"), fighter(game, "Ola", f"__Mru~~czek {role}"))
    sparring = training.simulate({}, {}, Luck(starter=starter, bases=[400, 100]))
    assert training.leader(sparring.life_after(1)) == starter                 # who struck first is ahead
    _, lead = bar_and_lead(training.fight_embed(pair, sparring, 1))
    ahead = pair[starter]
    assert ahead.shown != ahead.name.strip()[:training.NAME_LENGTH]           # (the name needs escaping)
    assert lead == f"{training.SQUARES[starter]} Prowadzi **{ahead.shown}**"
    assert lead == ("🟥 Prowadzi **\\*\\*Bu\\_rek\\*\\* @​everyone**",
                    "🟦 Prowadzi **\\_\\_Mru\\~\\~czek <@​&985071779787730944>**")[starter]
    assert "@everyone" not in lead and role not in lead and ahead.name not in lead


def test_blows_in_words(game):
    burek, mruczek = fighter(game, "Ala", "Burek"), fighter(game, "Ola", "Mruczek")
    pair = (burek, mruczek)

    def line(attacker=0, damage=250, blocked=0, critical=False, full_power=False) -> str:
        return training.blow_line(training.Blow(attacker, 250, damage, blocked, critical, full_power, (1, 1)), pair)

    assert line() == "🟥 **Burek** uderza za **250**"
    assert line(attacker=1, blocked=40) == "🟦 **Mruczek** uderza za **250** (🛡️ zablokowano 40)"
    assert line(critical=True) == "🟥 **Burek** trafia krytycznie za **250** 💥"
    assert line(full_power=True) == "🟥 **Burek** uderza z pełną mocą za **250** 🔄"
    assert line(critical=True, full_power=True, blocked=7) == (
        "🟥 **Burek** trafia krytycznie z pełną mocą za **250** 🔄💥 (🛡️ zablokowano 7)")
    assert line(damage=0, blocked=250) == "🟥 **Burek** atakuje, ale **Mruczek** blokuje cały cios 🛡️"
    assert [training._blows_word(count) for count in (1, 2, 4, 5, 12, 22, 36)] == [
        "cios", "ciosy", "ciosy", "ciosów", "ciosów", "ciosy", "ciosów"]


def test_the_fight_message_from_the_first_to_the_last_version(game):
    burek, mruczek = fighter(game, "Ala", "Burek"), fighter(game, "Ola", "Mruczek", lowhp_perc=10)
    pair = (burek, mruczek)
    sparring = training.simulate(burek.stats, mruczek.stats, Luck(starter=1, bases=[300, 100]))
    start = training.fight_embed(pair, sparring, 0)
    assert start.title == "⚔️ Burek kontra Mruczek" and start.colour.value == training.FIGHT_COLOUR
    assert start.description == (
        "🟥 **Burek** · Ala\n" + "🟥" * 9 + "⬛ **2700** / 3000\n"
        "🟦 **Mruczek** · Ola\n" + "🟦" * 10 + " **3000** / 3000\n\n"
        + "🟥" * 7 + "🟦" * 7 + "\n🟦 Prowadzi **Mruczek**\n\n*Zaczyna **Mruczek**.*")
    middle = training.fight_embed(pair, sparring, 6)
    assert "**1900** / 3000" in middle.description and "**2400** / 3000" in middle.description
    assert "\n\n" + "🟥" * 6 + "🟦" * 8 + "\n🟦 Prowadzi **Mruczek**\n\n*...wcześniej" in middle.description
    assert "*...wcześniej: 2 ciosy*\n🟦 **Mruczek** uderza za **250**\n" in middle.description
    assert middle.description.count("uderza za") == 4 and "Wygrywa" not in middle.description
    last = training.fight_embed(pair, sparring, len(sparring.blows), winner=sparring.winner)
    assert sparring.winner == 1 and last.colour.value == training.FIGHT_OVER_COLOUR
    assert "⬛" * 10 + " **0** / 3000" in last.description
    # The advantage bar in its final state; the result says who won, so no "who leads" line.
    assert "\n\n" + "🟦" * 14 + "\n\n" in last.description and "Prowadzi" not in last.description
    assert last.description.endswith("\n\n🏆 **Wygrywa Mruczek** (Ola)!")
    drawn = training.simulate({"DEF_PERC": 100}, {"DEF_PERC": 100}, Luck())
    even = "\n\n" + "🟥" * 7 + "🟦" * 7
    assert even + "\n⚖️ Walka wyrównana\n\n" in training.fight_embed(pair, drawn, 36).description
    after_a_draw = training.fight_embed(pair, drawn, 40, winner=0).description
    assert even + "\n\n" in after_a_draw and "wyrównana" not in after_a_draw
    assert after_a_draw.endswith("⚖️ Remis po 40 ciosach! Los zdecydował: dalej przechodzi **Burek** (Ala).")


def test_a_companion_is_called_by_its_name_or_its_species_and_names_are_harmless(game):
    ala = game.add_member("Ala *z* @everyone")
    row = {"pet_id": Decimal(5), "pet_name": "Towarzysz", "pet_lvl": Decimal(2), "type": "Dog",
           "crit_perc": Decimal("12.5"), "replace_perc": 1, "def_perc": 2, "lowhp_perc": 3, "slow_perc": 4,
           "drop_perc": 50, "mythic": True}
    unnamed = training._fighter(ala, row)
    assert (unnamed.name, unnamed.species, unnamed.level, unnamed.pet_id) == ("Pies", "Pies", 2, 5)
    assert unnamed.stats == {"CRIT_PERC": 12.5, "REPLACE_PERC": 1.0, "DEF_PERC": 2.0, "LOWHP_PERC": 3.0,
                             "SLOW_PERC": 4.0}
    assert training._fighter(ala, {**row, "pet_name": None, "type": "Nieznany"}).name == "Towarzysz"
    role = "<@&985071779787730944>"
    named = training._fighter(ala, {**row, "pet_name": f"**Sir** @everyone {role} " + "x" * 80})
    assert "@everyone" not in named.shown and role not in named.shown and "**" not in named.shown
    assert named.shown.startswith("\\*\\*Sir\\*\\* @") and "x" not in named.shown and len(named.name) > 80
    assert "@everyone" not in unnamed.owner and unnamed.owner.startswith("Ala \\*z\\* ")


def test_texts_fit_discord_and_never_say_lobby(game):
    texts = {name: value for name, value in vars(training).items()
             if isinstance(value, str) and name.isupper()
             and name.split("_")[0] in ("MSG", "TRAINING", "FIGHT", "LINE", "LOG", "FIGHTER", "REWARD")}
    assert len(texts) > 25
    assert all(len(text) < 1500 and "lobby" not in text.lower() for text in texts.values())
    assert "lobby" not in events_cog.MSG_TRAINING_NO_PICTURE.lower()
    assert len(training.TRAINING_TITLE) <= 256 and len(training.TRAINING_DESCR) <= 3500
    assert "<@&" not in "".join(texts.values())                       # no role is ever pinged
    # The longest names in the worst fight.
    nasty = "*_~`|" * 20
    pair = (training._fighter(game.add_member(nasty), {"pet_id": 1, "pet_name": nasty, "pet_lvl": 1, "type": "Cat"}),
            training._fighter(game.add_member(nasty), {"pet_id": 2, "pet_name": nasty, "pet_lvl": 1, "type": "Cat"}))
    blocked = training.simulate({"DEF_PERC": 100, "CRIT_PERC": 100}, {"DEF_PERC": 100}, Luck())
    crits = training.simulate({"CRIT_PERC": 100, "REPLACE_PERC": 100}, {"DEF_PERC": 5, "CRIT_PERC": 100}, Luck())
    for sparring in (blocked, crits):
        for shown in range(len(sparring.blows) + 1):
            embed = training.fight_embed(pair, sparring, shown, winner=0 if shown == len(sparring.blows) else None)
            assert len(embed.title) <= 256 and len(embed.description) <= 4096 and len(embed) <= 6000
    # Lines that list players are cut, not refused by Discord.
    crowd = [training.FIGHTER_ENTRY.format(pet=pair[0].shown, mention=f"<@{10 ** 18 + index}>") for index in range(150)]
    listed = training._listed(training.MSG_TRAINING_CLOSED, crowd)
    assert len(listed) <= 2000 and listed.startswith("**Zapisy na trening zakończone!** Na plac wychodzą: **")
    assert "\n...i " in listed and listed.endswith(" więcej")
    few = training._listed(training.MSG_TRAINING_DROPPED, ["<@1>", "<@2>"])
    assert few == "🥚 Bez wyklutego towarzysza u boku nie ma treningu - tym razem odpadają: <@1>, <@2>."
    assert len(training._listed(training.MSG_TRAINING_DROPPED, [f"<@{10 ** 18 + index}>" for index in range(300)])) <= 2000


# ===================================== the draw of events ===============================

def test_the_training_has_the_weights_of_the_duel():
    order = events_cog.EVENT_ORDER
    here, duel = order.index(EventType.TRAINING), order.index(EventType.DUEL)
    assert len(order) == len(events_cog.PEAK_WEIGHTS) == len(events_cog.OFF_PEAK_WEIGHTS) == len(
        events_cog.DEBUG_WEIGHTS) == 12                                      # (the mine: 2026-11-23)
    assert events_cog.PEAK_WEIGHTS[here] == events_cog.PEAK_WEIGHTS[duel] == 2
    assert events_cog.OFF_PEAK_WEIGHTS[here] == events_cog.OFF_PEAK_WEIGHTS[duel] == 0
    assert events_cog.DEBUG_WEIGHTS[here] == 0
    assert EventType.TRAINING not in events_cog.PASSIVE and EventType["TRAINING"].value == 11


def test_pictures_are_the_image_files_of_the_folder(folder):
    assert training.pictures() == [] and not training.has_picture()          # no folder at all
    folder.mkdir()
    assert training.pictures() == []
    (folder / "notatki.txt").write_text("namalować!")
    (folder / "0.png").write_bytes(b"")                                       # an empty file is no picture
    (folder / "podfolder.png").mkdir()
    assert training.pictures() == [] and not training.has_picture()
    for name in ("1.JPG", "0.png", "2.gif", "10.jpeg"):
        (folder / name).write_bytes(b"picture")
    assert [path.name for path in training.pictures()] == ["0.png", "1.JPG", "10.jpeg", "2.gif"]
    assert training.has_picture()


async def test_the_training_is_drawn_only_once_its_picture_exists(game, folder, monkeypatch):
    bot = game.bot
    bot.debug = False
    cog = events_cog.EventsCog(bot)
    ran: list = []
    drawn: list = []
    now = [EVENING]

    async def run_event(event_type):
        ran.append(event_type)

    def choices(population, weights):
        weights = dict(zip(population, weights))
        drawn.append(weights)
        return [EventType.TRAINING if weights[EventType.TRAINING] else EventType.MEMORY]

    monkeypatch.setattr(cog, "_run", run_event)
    monkeypatch.setattr(random, "choices", choices)
    monkeypatch.setattr(clock, "now", lambda: now[0])

    assert cog.not_drawable() == (EventType.TRAINING,)
    assert await cog.run_one_event() is EventType.MEMORY                      # peak hours, no picture: left out
    assert drawn[-1][EventType.TRAINING] == 0 and drawn[-1][EventType.DUEL] == 2
    assert {event: weight for event, weight in drawn[-1].items() if event is not EventType.TRAINING} == {
        event: weight for event, weight in zip(events_cog.EVENT_ORDER, events_cog.PEAK_WEIGHTS)
        if event is not EventType.TRAINING}

    folder.mkdir()
    (folder / "0.png").write_bytes(b"picture")                                # the owner adds the picture ...
    assert cog.not_drawable() == ()
    assert await cog.run_one_event() is EventType.TRAINING                    # ... and the same cog draws it
    assert drawn[-1][EventType.TRAINING] == drawn[-1][EventType.DUEL] == 2

    now[0] = NOON                                                             # off-peak: as rare as the duel
    assert await cog.run_one_event() is EventType.MEMORY
    assert drawn[-1][EventType.TRAINING] == drawn[-1][EventType.DUEL] == 0
    assert ran == [EventType.MEMORY, EventType.TRAINING, EventType.MEMORY]

    (folder / "0.png").unlink()                                               # taken away again: left out again
    now[0] = EVENING
    assert await cog.run_one_event() is EventType.MEMORY and drawn[-1][EventType.TRAINING] == 0


async def test_the_ritual_that_does_not_fit_is_not_replaced_by_a_training_without_a_picture(game, folder, monkeypatch):
    cog = events_cog.EventsCog(game.bot)
    asked: list = []

    def pick(now, *, debug, exclude=()):
        asked.append(tuple(exclude))
        return EventType.RITUAL if len(asked) == 1 else None

    monkeypatch.setattr(events_cog, "pick_event", pick)
    monkeypatch.setattr(cog, "ritual_fits", lambda: False)
    assert await cog.run_one_event() is None
    assert asked == [(EventType.TRAINING,), (EventType.RITUAL, EventType.TRAINING)]


async def test_one_of_several_pictures_is_picked(game, folder, enrol, monkeypatch):
    folder.mkdir()
    for name in ("0.png", "1.jpg", "2.gif", "opis.txt"):
        (folder / name).write_bytes(b"picture")
    offered: list = []

    def choice(pool):
        offered.append([path.name for path in pool])
        return pool[1]

    monkeypatch.setattr(random, "choice", choice)
    enrol()
    assert await training.spawn_training(game.scene()) is None
    assert offered == [["0.png", "1.jpg", "2.gif"]] and files(game) == ["1.jpg"]
    assert game.channel.sent[0].files == ["1.jpg"] and TITLE in game.channel.sent[1].text   # the picture, then the panel


async def test_admin_spawn_without_a_picture_says_where_to_put_it(game, folder, enrol):
    cog = events_cog.EventsCog(game.bot)
    ctx = SimpleNamespace(bot=game.bot, channel=game.channel, author=game.add_member("Alter"))
    before = len(game.channel.sent)
    await cog.spawn_event.callback(cog, ctx, "training")
    (answer,) = game.channel.sent[before:]
    assert answer.content == events_cog.MSG_TRAINING_NO_PICTURE
    assert "`assets/events/training/0.png`" in answer.content and "restart bota nie jest potrzebny" in answer.content
    assert await cog.run_one_event(forced=EventType.TRAINING) is None        # not started by any other path either
    assert len(game.channel.sent) == before + 1 and game.bot.state.free_for_activity
    with pytest.raises(training.PictureMissing):
        await training.spawn_training(game.scene())
    assert len(game.channel.sent) == before + 1

    folder.mkdir()
    (folder / "0.png").write_bytes(b"picture")
    enrol()
    await cog.spawn_event.callback(cog, ctx, "TRAINING")                      # with the picture it is played
    assert game.channel.contains(training.MSG_TRAINING_NOBODY) and files(game) == ["0.png"]
    assert game.bot.state.free_for_activity
    await cog.spawn_event.callback(cog, ctx, "trening")
    assert "TRAINING" in game.channel.messages[-1].content                    # listed among the known names


# ===================================== the sign-up ======================================

async def test_only_a_hatched_active_companion_signs_its_owner_up(game_db, picture, sign_up, rewards, sent_with):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, "Ala", 5, level=2, pet_name="Burek", species="Dog")
    egg = await with_pet(game, "Ola", 6, level=0)
    nobody = game.add_member("Ela")                                           # no row at all
    stabled = game.add_member("Ula")                                          # her only companion rests in the stable
    await add_owner(db, stabled.id, 0, pet_id_alt1=7)
    await add_pet(db, 7, pet_lvl=3)

    playing = asyncio.ensure_future(training.spawn_training(game.scene()))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    assert roster.embeds[0].title == training.TRAINING_TITLE and roster.button_labels == ["Dołącz"]
    assert roster.embeds[0].description.startswith(training.TRAINING_DESCR + "\n\n⏳ Zapisy kończą się <t:")
    assert game.channel.sent[0].files == ["0.png"] and game.channel.sent[1] is roster

    joined = await game.click(roster, ala, "join")
    assert joined.response.deferred and joined.replies == []
    assert (await game.click(roster, egg, "join")).answers == [training.MSG_TRAINING_EGG]
    assert (await game.click(roster, nobody, "join")).answers == [training.MSG_TRAINING_NO_PET]
    assert (await game.click(roster, stabled, "join")).answers == [training.MSG_TRAINING_NO_PET]
    assert (await game.click(roster, ala, "join")).answers == [events.MSG_ALREADY_SIGNED]      # a double click
    assert (await game.click(roster, game.bot.user, "join")).replies == []
    double = await asyncio.gather(game.click(roster, stabled, "join"), game.click(roster, stabled, "join"))
    assert [click.answers for click in double] == [[training.MSG_TRAINING_NO_PET]] * 2        # (refused: not remembered)

    await sign_up.close()
    assert await playing is None                                              # one companion: nobody to fight
    assert "Zapisani (1)" in roster.text and ala.mention in roster.text and events.MSG_SIGN_UP_CLOSED in roster.text
    assert all(member.mention not in roster.text for member in (egg, nobody, stabled))
    assert game.channel.contains(f"Towarzysz **Burek** ({ala.mention}) nie ma dziś z kim trenować")
    assert rewards.shards == [(1, ala.id)] and rewards.scrolls == [] and rewards.loot == []
    assert fight_messages(game) == [] and not game.channel.contains("Runda")
    assert not any("<@&" in message.content for message in game.channel.sent)  # the hunters are not pinged
    assert no_listeners(game) and no_live_buttons(game)


async def test_two_quick_clicks_of_one_player_sign_him_up_once(game_db, picture, sign_up, rewards):
    game = game_db
    ala = await with_pet(game, "Ala", 5, level=1)
    playing = asyncio.ensure_future(training.spawn_training(game.scene()))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    first, second = await asyncio.gather(game.click(roster, ala, "join"), game.click(roster, ala, "join"))
    assert sorted([first.answers, second.answers]) == [[], [events.MSG_ALREADY_SIGNED]]
    await sign_up.close()
    assert await playing is None
    assert "Zapisani (1)" in roster.text and rewards.shards == [(1, ala.id)]


async def test_a_refused_player_may_take_a_hatched_companion_out_and_join(game_db, picture, sign_up, rewards, fights):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, "Ala", 6, level=0, owner={"pet_id_alt2": 7})
    await add_pet(db, 7, pet_name="Mruczek", pet_lvl=2)
    ola = await with_pet(game, "Ola", 8, level=1, pet_name="Reksio", species="Dog")
    playing = asyncio.ensure_future(training.spawn_training(game.scene()))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    assert (await game.click(roster, ala, "join")).answers == [training.MSG_TRAINING_EGG]
    assert await pets.unstore_pet(game.scene(ala), 2) is True
    assert (await game.click(roster, ala, "join")).replies == []
    await game.click(roster, ola, "join")
    await sign_up.close()
    assert await playing in (ala, ola)
    assert {one.name for pair in fights.pairs for one in pair} == {"Mruczek", "Reksio"}


async def test_a_database_hiccup_at_a_click_is_a_refusal_not_a_crash(game, picture, sign_up, rewards, monkeypatch):
    ala = game.add_member("Ala")

    async def down(q, player_id, *, lock=False):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(training.pets, "_active_pet", down)
    playing = asyncio.ensure_future(training.spawn_training(game.scene()))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    assert (await game.click(roster, ala, "join")).answers == [ui.MSG_FAILED]
    await sign_up.close()
    assert await playing is None and game.channel.contains(training.MSG_TRAINING_NOBODY)
    assert "Zapisani (0)" in roster.text and game.bot.owner_log == []


# ===================================== the stable during the sign-up ====================

def test_the_stable_exception_belongs_to_the_holder_it_was_made_for():
    state = GameState()
    assert not state.stable_open
    with state.stable_stays_open():                       # nobody holds the channel: nothing to except
        assert not state.stable_open
    with state.hold_busy("event:TRAINING"):
        assert not state.stable_open
        with state.stable_stays_open():
            assert state.stable_open and state.snapshot()["stable_open"] is True
            assert state.force_release_busy() == "event:TRAINING"            # !ResetBoss in the middle
            assert not state.stable_open
            with state.hold_busy("hunt"):                                    # somebody else's channel now
                assert not state.stable_open
        assert not state.stable_open
    with pytest.raises(RuntimeError):
        with state.hold_busy("event:TRAINING"), state.stable_stays_open():
            raise RuntimeError("the sign-up broke")
    assert not state.stable_open and not state.busy and state.snapshot()["stable_open"] is False


async def test_the_stable_works_during_the_sign_up_and_only_then(game_db, picture, pets_cog, sign_up, fights,
                                                                rewards):
    game, db = game_db, game_db.bot.db
    state = game.bot.state
    ala = await with_pet(game, "Ala", 5, level=3, pet_name="Burek", species="Dog",
                         owner={"pet_id_alt3": 7, "reroll_scroll": 3})
    await add_pet(db, 7, pet_name="Mruczek", pet_lvl=2)
    ola = await with_pet(game, "Ola", 8, level=1, pet_name="Reksio", species="Dog")
    elsewhere = game.announce_channel
    assert await pets.show_pet(Scene(bot=game.bot, channel=elsewhere, author=ala), ala) is True
    far = elsewhere.messages[-1]                          # her pet with its panel, shown before the event

    cog = events_cog.EventsCog(game.bot)
    fights.hold()
    playing = asyncio.ensure_future(cog.run_one_event(forced=EventType.TRAINING))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    assert state.busy_owner == "event:TRAINING" and state.event_type is EventType.TRAINING
    assert state.stable_open

    # -- the sign-up is open: the stable works although the event holds the channel
    await run(game, ala, "!stajnia")                      # showing it: the command ...
    shown = game.channel.messages[-1]
    assert shown.embeds[0].title == "Stajnia" and ui.is_live(shown.id)
    clicked = await hub_stable(game, ala)                 # ... the button of the pinned game panel ...
    assert clicked.response.deferred and clicked.replies == []
    assert game.channel.messages[-1] is not shown and game.channel.messages[-1].embeds[0].title == "Stajnia"
    looked = await game.click(far, ala, "pet:stable")     # ... and the button under a shown pet
    assert looked.replies == [] and elsewhere.messages[-1].embeds[0].title == "Stajnia"

    await run(game, ala, "!schowajtowarzysza 2")          # putting a companion in ...
    assert await slots(db, ala) == [0, 0, 5, 7, 0] and game.channel.contains(pets.MSG_STORED)
    await run(game, ala, "!wyciagnijtowarzysza 2")        # ... taking one out ...
    assert await slots(db, ala) == [5, 0, 0, 7, 0] and game.channel.contains(pets.MSG_UNSTORED)
    swapped = await game.click(shown, ala, "pet:stable-take", values=["3:7"])     # ... and swapping with the list
    assert swapped.replies == [] and await slots(db, ala) == [7, 0, 0, 5, 0]
    assert game.channel.contains(pets.MSG_SWAPPED)
    await run(game, ala, "!st 1")                         # (the short names too)
    await run(game, ala, "!wt 1")
    assert await slots(db, ala) == [7, 0, 0, 5, 0] and not game.channel.contains(guards.BUSY_NOTICE)

    # -- ... and nothing else does
    for command in ("!towarzysz", "!rankingtowarzyszy", "!odrodzenie", "!porzucam", "!nazwij Inny"):
        assert await typed_is_busy(game, ala, command), command
    guards._last_notice.clear()
    pet_button = FakeInteraction(game.bot, ala, channel=game.channel)
    assert await hub.show_display(pet_button, hub.show_pet) is False and pet_button.answers == [ui.MSG_BUSY]
    assert await pets.show_pet(game.scene(ala), ala) is True      # (shown without the command's guard)
    near = game.channel.messages[-1]
    for key in ("pet:reroll", "pet:discard", "pet:rename"):
        assert await clicked_is_busy(game, near, ala, key), key
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 3 and await slots(db, ala) == [7, 0, 0, 5, 0]

    await game.click(roster, ala, "join")
    await game.click(roster, ola, "join")

    # -- the sign-up closes: the usual rules of an event until it ends
    await sign_up.close()
    await until(lambda: len(fights.pairs) == 1)           # (the first fight waits for the test)
    assert state.busy_owner == "event:TRAINING" and not state.stable_open
    for command in ("!stajnia", "!s", "!schowajtowarzysza 1", "!wyciagnijtowarzysza 3", "!st 1", "!wt 3"):
        assert await typed_is_busy(game, ala, command), command
    assert await clicked_is_busy(game, shown, ala, "pet:stable-take", values=["3:5"])
    assert (await hub_stable(game, ala)).answers == [ui.MSG_BUSY]
    assert await clicked_is_busy(game, far, ala, "pet:stable")
    assert await clicked_is_busy(game, near, ala, "pet:stable")
    assert await slots(db, ala) == [7, 0, 0, 5, 0]

    fights.release()
    assert await playing is EventType.TRAINING
    assert not state.busy and not state.stable_open and state.free_for_activity
    assert [(one.pet_id, one.name) for one in fights.pairs[0]] in ([(7, "Mruczek"), (8, "Reksio")],
                                                                   [(8, "Reksio"), (7, "Mruczek")])
    await run(game, ala, "!wyciagnijtowarzysza 3")        # the event is over: the stable as always
    assert await slots(db, ala) == [5, 0, 0, 7, 0]


async def test_outside_the_training_the_stable_is_blocked_while_busy_as_before(game_db, pets_cog):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, "Ala", 5, level=3, owner={"pet_id_alt3": 7})
    await add_pet(db, 7, pet_lvl=2)
    await run(game, ala, "!stajnia")
    shown = game.channel.messages[-1]
    for owner in ("hunt", "tower", "event:INVASION", "event:DUEL", "adv-reroll"):
        with game.bot.state.hold_busy(owner):
            assert not game.bot.state.stable_open
            for command in ("!stajnia", "!schowajtowarzysza 1", "!wyciagnijtowarzysza 3"):
                assert await typed_is_busy(game, ala, command), (owner, command)
            assert await clicked_is_busy(game, shown, ala, "pet:stable-take", values=["3:7"])
            assert (await hub_stable(game, ala)).answers == [ui.MSG_BUSY]
    assert await slots(db, ala) == [5, 0, 0, 7, 0]
    # A typed fight stops the stable's buttons even while the exception is on (nothing between its prompts).
    with game.bot.state.hold_busy("event:TRAINING"), game.bot.state.stable_stays_open():
        with pytest.MonkeyPatch.context() as patch:
            patch.setitem(challenge._rounds_running, game.channel.id, 1)
            quiet = await game.click(shown, ala, "pet:stable-take", values=["3:7"])
            assert quiet.response.deferred and quiet.replies == []
    assert await slots(db, ala) == [5, 0, 0, 7, 0]


async def test_the_exception_ends_when_the_sign_up_breaks(game, picture, monkeypatch):
    async def broken(self):
        assert self.scene.bot.state.stable_open
        raise RuntimeError("Discord 503")

    monkeypatch.setattr(training._TrainingSignUp, "collect", broken)
    cog = events_cog.EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.TRAINING) is None
    state = game.bot.state
    assert not state.stable_open and not state.busy and state.free_for_activity
    assert any("TRAINING" in line and "Discord 503" in line for line in game.bot.owner_log)
    assert no_listeners(game) and no_live_buttons(game)


# ===================================== who fights: the snapshot =========================

async def test_the_companion_at_the_close_fights_the_whole_training(game_db, picture, sign_up, fights, rewards,
                                                                   in_order):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, "Ala", 5, level=3, pet_name="Burek", species="Dog", owner={"pet_id_alt1": 7})
    await add_pet(db, 7, pet_name="Mruczek", pet_lvl=2, crit_perc=10)
    ola = await with_pet(game, "Ola", 8, level=1, pet_name="Reksio", species="Dog", def_perc=5)
    ela = await with_pet(game, "Ela", 9, level=1, species="Sheep")           # never named: called by its species

    playing = asyncio.ensure_future(training.spawn_training(game.scene()))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    for member in (ala, ola, ela):
        await game.click(roster, member, "join")
    assert await pets.unstore_pet(game.scene(ala), 1) is True                 # a swap during the sign-up counts

    fights.hold()
    await sign_up.close()
    await until(lambda: len(fights.pairs) == 1)
    # After the close nothing changes who fights - not between the rounds either.
    assert await pets.unstore_pet(game.scene(ala), 1) is True                 # Burek is at her side again
    assert await pets.store_pet(game.scene(ela), 4) is True                   # Ela's companion rests in the stable
    await db.execute("UPDATE PETS SET PET_NAME = 'Inny', CRIT_PERC = 100, DEF_PERC = 100, PET_LVL = 0")
    fights.release()
    winner = await playing

    first_fight, final = fights.pairs
    assert [(one.member, one.pet_id, one.name, one.level) for one in first_fight] == [
        (ala, 7, "Mruczek", 2), (ola, 8, "Reksio", 1)]
    assert first_fight[0].stats == {"CRIT_PERC": 10.0, "REPLACE_PERC": 0.0, "DEF_PERC": 0.0, "LOWHP_PERC": 0.0,
                                    "SLOW_PERC": 0.0}
    assert first_fight[1].stats["DEF_PERC"] == 5.0
    assert (final[0].member, final[0].pet_id, final[0].name) == (ela, 9, "Owca")       # she had the bye
    assert final[1] is fights.results[0][0] and winner is fights.results[1][0].member
    assert game.channel.contains(f"Na plac wychodzą: **Mruczek** ({ala.mention}), **Reksio** ({ola.mention}), "
                                 f"**Owca** ({ela.mention})")
    assert game.channel.contains(f"🍀 Towarzysz **Owca** ({ela.mention}) nie ma pary")
    assert [message.embeds[0].title for message in fight_messages(game)][0] == "⚔️ Mruczek kontra Reksio"
    assert not game.channel.ever_sent("Inny") and not game.channel.ever_sent("Burek")
    assert sorted(rewards.shards) == sorted([(1, ala.id), (1, ola.id), (1, ela.id)])


async def test_a_player_without_a_hatched_companion_at_the_close_is_dropped(game_db, picture, sign_up, rewards,
                                                                          sent_with):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, "Ala", 5, level=3, pet_name="Burek", owner={"pet_id_alt1": 6})
    await add_pet(db, 6, pet_lvl=0)
    ola = await with_pet(game, "Ola", 8, level=1, pet_name="Reksio")
    ela = await with_pet(game, "Ela", 9, level=2, pet_name="Azor", species="Dog")
    playing = asyncio.ensure_future(training.spawn_training(game.scene()))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    for member in (ala, ola, ela):
        assert (await game.click(roster, member, "join")).replies == []
    assert await pets.unstore_pet(game.scene(ala), 1) is True                 # an egg is at her side now
    assert await pets.store_pet(game.scene(ola), 4) is True                   # and nobody at Ola's
    await sign_up.close()
    assert await playing is None
    assert "Zapisani (3)" in roster.text                                      # they did sign up ...
    dropped = panel(game, "tym razem odpadają")                               # ... and are dropped with one line
    assert dropped.content == ("🥚 Bez wyklutego towarzysza u boku nie ma treningu - tym razem odpadają: "
                               f"{ala.mention}, {ola.mention}.")
    assert game.channel.sent_count("tym razem odpadają") == 1 and not sent_with[dropped.id].users
    assert game.channel.contains(f"Towarzysz **Azor** ({ela.mention}) nie ma dziś z kim trenować")
    assert rewards.shards == [(1, ela.id)] and fight_messages(game) == []

    # Everybody dropped: nobody came.
    rewards.shards.clear()
    sign_up.closed.clear()
    game.channel.sent.clear()
    again = asyncio.ensure_future(training.spawn_training(game.scene()))
    game._tasks.append(again)
    roster = await game.wait_for_buttons("join")
    assert (await game.click(roster, ela, "join")).replies == []
    await db.execute("UPDATE PETS SET PET_LVL = 0 WHERE PET_ID = 9")
    await sign_up.close()
    assert await again is None
    assert game.channel.contains(training.MSG_TRAINING_NOBODY) and game.channel.contains(f"odpadają: {ela.mention}.")
    assert rewards.shards == [] and rewards.scrolls == [] and rewards.loot == []


async def test_the_snapshot_is_one_query_and_keeps_the_sign_up_order(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, "Ala", 5, level=1, pet_name="Burek", slow_perc=Decimal("7.5"))
    ola = await with_pet(game, "Ola", 6, level=0)
    ela = await with_pet(game, "Ela", 7, level=3, pet_name="Azor")
    ula = game.add_member("Ula")
    asked: list = []
    real = db.fetch

    async def fetch(query, *args, **kwargs):
        asked.append(query)
        return await real(query, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "fetch", fetch)
        fighters, dropped = await training._snapshot(game.bot, [ela, ula, ola, ala])
    assert len(asked) == 1
    assert [(one.member, one.pet_id, one.name) for one in fighters] == [(ela, 7, "Azor"), (ala, 5, "Burek")]
    assert fighters[1].stats["SLOW_PERC"] == 7.5 and dropped == [ula, ola]
    assert await training._snapshot(game.bot, []) == ([], [])


# ===================================== the bracket ======================================

async def test_nobody_came(game, picture, enrol, rewards):
    enrol()
    assert await training.spawn_training(game.scene()) is None
    assert game.channel.messages[-1].content == "*Plac treningowy świeci pustkami...*"
    assert fight_messages(game) == [] and not game.channel.contains("Runda")
    assert rewards.shards == [] and rewards.scrolls == [] and rewards.loot == []
    assert files(game) == ["0.png"] and no_listeners(game) and no_live_buttons(game)


async def test_one_companion_finds_no_partner_and_gets_the_participants_reward(game, picture, enrol, rewards,
                                                                             sent_with):
    burek = fighter(game, "Ala", "Burek")
    enrol(burek)
    assert await training.spawn_training(game.scene()) is None
    line = game.channel.messages[-1]
    assert line.content == (f"Towarzysz **Burek** ({burek.member.mention}) nie ma dziś z kim trenować "
                            "<:Sadge:936907659142111273> Za same chęci dostaje fragment zwoju.")
    assert sent_with[line.id].users == [burek.member]
    assert rewards.shards == [(1, burek.member.id)] and rewards.scrolls == [] and rewards.loot == []
    assert fight_messages(game) == [] and not game.channel.contains("Runda")
    assert not game.channel.contains("Zapisy na trening zakończone")


@pytest.mark.parametrize("count, rounds, byes", [(2, 1, 0), (3, 2, 1), (5, 3, 2), (8, 3, 0)])
async def test_knock_out_bracket(game, picture, enrol, rewards, fights, in_order, sent_with, count, rounds, byes):
    game.bot.debug = False
    crowd = [fighter(game, f"Gracz{index}", f"Pupil{index}", crit_perc=index * 5) for index in range(count)]
    enrol(*crowd)
    winner = await training.spawn_training(game.scene(), rng=random.Random(count))

    assert len(fights.pairs) == len(fight_messages(game)) == count - 1        # one message per fight
    assert [game.channel.sent_count(f"**Trening towarzyszy - Runda {number}**") for number in range(1, rounds + 2)] == [
        *[1] * rounds, 0]
    assert game.channel.sent_count("nie ma pary i od razu przechodzi do kolejnej rundy!") == byes
    # Everybody but the winner lost exactly once; the winner never did.
    losers = [loser.member for _, loser in fights.results]
    assert len(set(losers)) == count - 1 and winner not in losers
    assert winner is fights.results[-1][0].member
    champion = fights.results[-1][0]
    last = game.channel.sent_count("🏆 **Trening wygrywa towarzysz")
    assert last == 1 and game.channel.contains(
        f"🏆 **Trening wygrywa towarzysz {champion.name}** - gratulacje, {winner.mention}! 🏆")
    # A companion fights only as long as it wins: every pair is made of the still unbeaten ones.
    beaten: set = set()
    for (first, second), (_, loser) in zip(fights.pairs, fights.results):
        assert first.member not in beaten and second.member not in beaten and first is not second
        beaten.add(loser.member)
    # The first round as the PvP tournament pairs it: the last one waits when the number is odd.
    first_round = crowd[:-1] if count % 2 else crowd
    assert fights.pairs[:len(first_round) // 2] == [tuple(first_round[index:index + 2])
                                                     for index in range(0, len(first_round), 2)]
    if count % 2:
        assert game.channel.contains(f"🍀 Towarzysz **{crowd[-1].name}** ({crowd[-1].member.mention}) nie ma pary")
    # Rewards: each exactly once.
    finalist = fights.results[-1][1]
    assert sorted(rewards.shards) == sorted((1, one.member.id) for one in crowd)
    assert rewards.scrolls == [(1, finalist.member.id)]
    assert rewards.loot == [(2, winner, 0)]
    assert game.channel.contains(f"🎁 Nagrody: {winner.mention} - łup zwycięzcy, {finalist.member.mention} - "
                                 "zwój odrodzenia")
    # Who is pinged: the two owners when their fight starts, the winner at the end - nobody else, no role.
    assert not any("<@&" in message.content or "@everyone" in message.content for message in game.channel.sent)
    for message in game.channel.sent:
        allowed = sent_with.get(message.id)
        if message in fight_messages(game):
            assert len(allowed.users) == 2 and not allowed.everyone and not allowed.roles
        elif "Trening wygrywa" in message.content:
            assert allowed.users == [winner] and not allowed.everyone and not allowed.roles
        elif "<@" in message.content:
            assert allowed is not None and not allowed.users and not allowed.everyone, message.content
    assert game.bot.owner_log == [] and no_listeners(game) and no_live_buttons(game)
    assert files(game) == ["0.png"]


async def test_the_bracket_is_shuffled(game, picture, enrol, rewards, fights, monkeypatch):
    crowd = [fighter(game, f"Gracz{index}") for index in range(4)]
    enrol(*crowd)
    monkeypatch.setattr(random, "shuffle", lambda items: items.reverse())
    await training.spawn_training(game.scene(), rng=random.Random(2))
    assert fights.pairs[:2] == [(crowd[3], crowd[2]), (crowd[1], crowd[0])]
    assert game.channel.contains("Na plac wychodzą: " + ", ".join(
        f"**{one.name}** ({one.member.mention})" for one in reversed(crowd)))


# ===================================== the fight message ================================

async def test_a_fight_is_one_message_edited_at_a_safe_pace(game, picture, enrol, rewards, in_order, paced,
                                                           sent_with):
    burek, mruczek = fighter(game, "Ala", "Burek"), fighter(game, "Ola", "Mruczek")
    enrol(burek, mruczek)
    first_versions: dict = {}
    game.channel.send_hooks.append(
        lambda message: first_versions.setdefault(message.id, message.embeds[0] if message.embeds else None))
    winner = await training.spawn_training(game.scene(), rng=random.Random(1))

    sparring = training.simulate(burek.stats, mruczek.stats, random.Random(1))   # the same fight, replayed
    assert (len(sparring.blows), sparring.winner) == (22, 1) and winner is mruczek.member
    (message,) = fight_messages(game)
    # The first version: both companions with their owners, full life bars, who starts - and the only ping.
    assert message.content == f"{burek.member.mention} ⚔️ {mruczek.member.mention}"
    assert sent_with[message.id].users == [burek.member, mruczek.member]
    assert first_versions[message.id].description == training.fight_embed((burek, mruczek), sparring, 0).description
    assert "🟥 **Burek** · Ala\n" in first_versions[message.id].description
    assert "*Zaczyna **Burek**.*" in first_versions[message.id].description
    # Then it is edited, one exchange at a time; the edits change the embed only (nobody is pinged again).
    counts = training.frame_counts(22)
    assert len(message.edits) == len(counts) == 11
    assert all(set(edit) == {"embed", "allowed_mentions"} and not edit["allowed_mentions"].users
               for edit in message.edits)
    assert [edit["embed"].description for edit in message.edits[:-1]] == [
        training.fight_embed((burek, mruczek), sparring, shown).description for shown in counts[:-1]]
    # Every version shows who is winning: the advantage bar of that moment, moving as the fight goes
    # (in the same message and the same edits - no extra message, no extra edit).
    versions = [first_versions[message.id], *(edit["embed"] for edit in message.edits)]
    bars = [bar_and_lead(version)[0] for version in versions]
    assert bars == [training.advantage_bar(sparring.life_after(shown)) for shown in (0, *counts)]
    assert bars[0] == RED * 7 + BLUE * 7 and bars[-1] == BLUE * 14 and len(set(bars)) > 3
    assert all("Prowadzi **" in version.description or "Walka wyrównana" in version.description
               for version in versions[:-1])
    # The last version: the loser has no life left and the winner is named.
    final = message.embeds[0]
    assert final.description == training.fight_embed((burek, mruczek), sparring, 22, winner=1).description
    assert "⬛" * 10 + " **0** / 3000" in final.description and "**679** / 3000" in final.description
    assert final.description.endswith("🏆 **Wygrywa Mruczek** (Ola)!") and final.colour.value == 0xFCBA03
    assert game.channel.sent_count("Wygrywa Mruczek") == 1
    # The pace: a pause before every edit - never less than Discord's budget allows - and about 18 s a fight.
    show = paced[:2 * len(counts)]
    assert [kind for kind, _ in show] == ["pause", "edit"] * len(counts)
    gaps = [seconds for kind, seconds in show if kind == "pause"]
    assert all(gap == pytest.approx(18 / 11) and gap >= 1.2 for gap in gaps) and 15 <= sum(gaps) <= 25
    assert all(target == message.id for kind, target in show if kind == "edit")
    assert paced[2 * len(counts):] == [("pause", training.FIGHT_PAUSE)]       # a short pause after the fight


async def test_a_failed_edit_in_the_middle_does_not_break_the_fight(game, picture, enrol, rewards, in_order,
                                                                   monkeypatch):
    burek, mruczek = fighter(game, "Ala", "Burek"), fighter(game, "Ola", "Mruczek")
    enrol(burek, mruczek)
    tried = edits_of_fights(monkeypatch, lambda number, kwargs: RuntimeError("Discord 500") if number in (2, 3) else None)
    assert await training.spawn_training(game.scene(), rng=random.Random(1)) is mruczek.member
    (message,) = fight_messages(game)                                         # still one message
    assert len(tried) == 11 and len(message.edits) == 9
    assert message.embeds[0].description.endswith("🏆 **Wygrywa Mruczek** (Ola)!")
    assert rewards.loot == [(2, mruczek.member, 0)] and rewards.scrolls == [(1, burek.member.id)]
    assert game.bot.owner_log == []


async def test_the_result_is_posted_when_the_last_edit_fails(game, picture, enrol, rewards, in_order, monkeypatch,
                                                            sent_with):
    burek, mruczek = fighter(game, "Ala", "Burek"), fighter(game, "Ola", "Mruczek")
    enrol(burek, mruczek)
    edits_of_fights(monkeypatch, lambda number, kwargs: RuntimeError("Discord 500") if says_who_won(kwargs) else None)
    assert await training.spawn_training(game.scene(), rng=random.Random(1)) is mruczek.member
    fight, result = fight_messages(game)
    assert "Wygrywa" not in fight.embeds[0].description and len(fight.edits) == 10
    assert result.content == "" and not sent_with[result.id].users            # the result pings nobody again
    assert result.embeds[0].description.endswith("🏆 **Wygrywa Mruczek** (Ola)!")
    assert "**0** / 3000" in result.embeds[0].description
    assert rewards.loot == [(2, mruczek.member, 0)] and sorted(rewards.shards) == sorted(
        [(1, burek.member.id), (1, mruczek.member.id)])


async def test_a_deleted_fight_message_is_not_edited_again_and_the_result_is_posted(game, picture, enrol, rewards,
                                                                                  in_order, monkeypatch, paced):
    burek, mruczek = fighter(game, "Ala", "Burek"), fighter(game, "Ola", "Mruczek")
    enrol(burek, mruczek)
    tried = edits_of_fights(monkeypatch, lambda number, kwargs: discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), "Unknown Message") if number >= 3 else None)
    assert await training.spawn_training(game.scene(), rng=random.Random(1)) is mruczek.member
    assert len(tried) == 3                                                    # gone: no point in editing on
    fight, result = fight_messages(game)
    assert len(fight.edits) == 2 and result.embeds[0].description.endswith("🏆 **Wygrywa Mruczek** (Ola)!")
    assert [kind for kind, _ in paced].count("edit") == 3
    assert rewards.loot == [(2, mruczek.member, 0)]


async def test_an_edit_discord_never_answers_is_given_up(game, picture, enrol, rewards, in_order, monkeypatch):
    burek, mruczek = fighter(game, "Ala", "Burek"), fighter(game, "Ola", "Mruczek")
    enrol(burek, mruczek)
    monkeypatch.setattr(training, "EDIT_TIMEOUT", 0.05)

    async def hang():
        await asyncio.sleep(30)

    edits_of_fights(monkeypatch, lambda number, kwargs: hang if says_who_won(kwargs) else None)
    assert await training.spawn_training(game.scene(), rng=random.Random(1)) is mruczek.member
    fight, result = fight_messages(game)
    assert "Wygrywa" not in fight.embeds[0].description
    assert result.embeds[0].description.endswith("🏆 **Wygrywa Mruczek** (Ola)!")


async def test_a_draw_is_decided_by_lot_and_said_in_the_fight_message(game, picture, enrol, rewards, in_order):
    burek = fighter(game, "Ala", "Burek", def_perc=100)
    mruczek = fighter(game, "Ola", "Mruczek", def_perc=100)
    enrol(burek, mruczek)
    luck = random.Random(5)
    replay = random.Random(5)
    assert training.simulate(burek.stats, mruczek.stats, replay).winner is None
    lot = replay.randrange(2)                                                 # the next draw decides
    winner = await training.spawn_training(game.scene(), rng=luck)
    chosen, other = ((burek, mruczek), (mruczek, burek))[lot]
    assert winner is chosen.member
    (message,) = fight_messages(game)
    assert len(message.edits) == 10                                           # 40 blows: two exchanges per edit
    final = message.embeds[0].description
    assert final.endswith(f"⚖️ Remis po 40 ciosach! Los zdecydował: dalej przechodzi **{chosen.name}** (" +
                          f"{chosen.owner}).")
    assert "*...wcześniej: 36 ciosów*" in final and final.count("blokuje cały cios 🛡️") == 4
    assert "**3000** / 3000" in final and "Wygrywa" not in final
    assert game.channel.sent_count("Remis po 40 ciosach") == 1                # said once, in the fight message
    assert rewards.loot == [(2, chosen.member, 0)] and rewards.scrolls == [(1, other.member.id)]


async def test_a_fight_message_that_cannot_be_posted_ends_the_event_without_rewards(game, picture, enrol, rewards,
                                                                                  monkeypatch):
    enrol(fighter(game, "Ala"), fighter(game, "Ola"))
    real = game.channel.send

    async def send(content=None, **kwargs):
        if kwargs.get("embed") is not None and " kontra " in str(kwargs["embed"].title):
            raise RuntimeError("Discord 503")
        return await real(content, **kwargs)

    monkeypatch.setattr(game.channel, "send", send)
    cog = events_cog.EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.TRAINING) is None
    assert rewards.shards == [] and rewards.scrolls == [] and rewards.loot == []
    assert game.bot.state.free_for_activity and any("TRAINING" in line for line in game.bot.owner_log)


# ===================================== rewards ==========================================

async def test_rewards_are_the_duel_events_loot_a_scroll_and_a_shard_each(game, picture, enrol, rewards, fights,
                                                                        in_order, monkeypatch):
    crowd = [fighter(game, name) for name in ("Ala", "Ola", "Ela", "Ula", "Iza")]
    enrol(*crowd)
    order: list = []
    game.channel.send_hooks.append(lambda message: order.append(message.content))
    real = training._reward

    async def reward(bot, what, member, awaitable):
        order.append(f"reward: {what}")
        return await real(bot, what, member, awaitable)

    monkeypatch.setattr(training, "_reward", reward)
    winner = await training.spawn_training(game.scene(), rng=random.Random(11))
    champion, finalist = fights.results[-1]
    assert winner is champion.member
    # The winner: exactly the call of the duel event (events.spawn_duel: daily.rand_loot(scene, 2, challenger, 0)).
    assert rewards.loot == [(2, champion.member, 0)]
    assert (training.WINNER_LOOT_RARITY, training.WINNER_LOOT_BOOST) == (2, 0)
    assert training.daily is events.daily
    # The finalist - the loser of the last fight - one rebirth scroll; every companion that fought one shard.
    assert rewards.scrolls == [(1, finalist.member.id)]
    assert sorted(rewards.shards) == sorted((1, one.member.id) for one in crowd) and len(rewards.shards) == 5
    # Granted once, in one place, after the winner is known.
    announced = next(index for index, line in enumerate(order) if line.startswith("🏆 **Trening wygrywa"))
    granted = [index for index, line in enumerate(order) if line.startswith("reward: ")]
    assert min(granted) > announced
    assert [order[index] for index in granted] == [
        *["reward: fragment zwoju"] * 5, "reward: zwój odrodzenia", "reward: łup zwycięzcy"]
    assert not game.channel.contains(training.MSG_TRAINING_REWARD_FAILED) and game.bot.owner_log == []


async def test_a_broken_reward_does_not_cost_the_others_theirs(game, picture, enrol, rewards, fights, in_order):
    crowd = [fighter(game, name) for name in ("Ala", "Ola", "Ela")]
    enrol(*crowd)
    rewards.broken.add(("shard", crowd[0].member.id))
    winner = await training.spawn_training(game.scene(), rng=random.Random(3))
    champion, finalist = fights.results[-1]
    assert sorted(rewards.shards) == sorted((1, one.member.id) for one in crowd[1:])
    assert rewards.scrolls == [(1, finalist.member.id)] and rewards.loot == [(2, winner, 0)]
    (report,) = game.bot.owner_log
    assert report == (f"Trening towarzyszy: nie udało się przyznać nagrody (fragment zwoju) graczowi "
                      f"<@{crowd[0].member.id}> (`RuntimeError: database down`). Przyznaj ją ręcznie.")
    assert game.log_channel.messages[-1].content.startswith(f"<@{game.bot.ids.owner}>!")      # the owner is pinged
    assert game.channel.messages[-1].content == training.MSG_TRAINING_REWARD_FAILED


@pytest.mark.parametrize("broken", ["scroll", "loot"])
async def test_a_broken_scroll_or_loot_leaves_the_rest(game, picture, enrol, rewards, fights, in_order, broken):
    crowd = [fighter(game, name) for name in ("Ala", "Ola", "Ela")]
    enrol(*crowd)
    for one in crowd:
        rewards.broken.add((broken, one.member.id))
    winner = await training.spawn_training(game.scene(), rng=random.Random(3))
    assert winner is fights.results[-1][0].member
    assert len(rewards.shards) == 3
    assert (rewards.scrolls, rewards.loot) == (([], [(2, winner, 0)]) if broken == "scroll" else (
        [(1, fights.results[-1][1].member.id)], []))
    assert len(game.bot.owner_log) == 1 and ("zwój odrodzenia" if broken == "scroll" else "łup zwycięzcy") in (
        game.bot.owner_log[0])
    assert game.channel.sent_count(training.MSG_TRAINING_REWARD_FAILED) == 1


async def test_rewards_do_not_wait_for_the_last_announcements(game, picture, enrol, rewards, fights, in_order,
                                                             monkeypatch):
    crowd = [fighter(game, name) for name in ("Ala", "Ola", "Ela")]
    enrol(*crowd)
    real = game.channel.send
    refused: list = []

    async def send(content=None, **kwargs):
        if str(content or "").startswith(("🏆 **Trening wygrywa", "🎁 Nagrody")):
            refused.append(content)
            raise RuntimeError("Discord 503")
        return await real(content, **kwargs)

    monkeypatch.setattr(game.channel, "send", send)
    cog = events_cog.EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.TRAINING) is EventType.TRAINING
    assert len(refused) == 2 and len(fights.results) == 2
    champion, finalist = fights.results[-1]
    assert len(rewards.shards) == 3 and rewards.scrolls == [(1, finalist.member.id)]
    assert rewards.loot == [(2, champion.member, 0)] and game.bot.owner_log == []
    assert game.bot.state.free_for_activity


async def test_the_lone_companions_broken_shard_is_reported(game, picture, enrol, rewards):
    burek = fighter(game, "Ala", "Burek")
    enrol(burek)
    rewards.broken.add(("shard", burek.member.id))
    assert await training.spawn_training(game.scene()) is None
    assert rewards.shards == [] and len(game.bot.owner_log) == 1
    assert game.channel.messages[-1].content == training.MSG_TRAINING_REWARD_FAILED


async def test_rewards_land_in_the_database_once_and_nothing_else_changes(game_db, picture, sign_up, fights,
                                                                        monkeypatch):
    game, db = game_db, game_db.bot.db
    loot: list = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        loot.append((rarity, hunter, boost_percent))
        return ""

    monkeypatch.setattr(training.daily, "rand_loot", rand_loot)
    players = [await with_pet(game, name, 20 + index, level=1 + index % 3, pet_name=f"Pupil{index}",
                              owner={"reroll_scroll": 2, "reroll_scroll_shard": 4})
               for index, name in enumerate(("Ala", "Ola", "Ela", "Ula", "Iza"))]
    newcomer = game.add_member("Ewa")                                         # watches, never signs up
    playing = asyncio.ensure_future(training.spawn_training(game.scene(), rng=random.Random(8)))
    game._tasks.append(playing)
    roster = await game.wait_for_buttons("join")
    for member in players:
        await game.click(roster, member, "join")
    await sign_up.close()
    winner = await playing
    champion, finalist = fights.results[-1]
    assert winner is champion.member and len(fights.pairs) == 4 and loot == [(2, winner, 0)]
    for member in players:
        row = await owner_row(db, member.id)
        assert row["reroll_scroll_shard"] == 5, member                        # one shard each, once
        assert row["reroll_scroll"] == (3 if member is finalist.member else 2), member
    assert await owner_row(db, newcomer.id) is None
    # No ranking points, nobody is banned, the companions are as they were.
    assert await db.fetchval("SELECT COUNT(*) FROM RANKING") == 0
    dead = game.bot.ids.dead_hunter_role
    assert not any(role.id == dead for member in players for role in member.roles)
    levels = [int(row["pet_lvl"]) for row in await db.fetch("SELECT PET_LVL FROM PETS ORDER BY PET_ID")]
    assert levels == [1, 2, 3, 1, 2]


# ===================================== busy and failures ================================

async def test_the_event_holds_the_channel_for_its_whole_length_and_runs_once(game, picture, sign_up, fights,
                                                                            rewards, enrol):
    state = game.bot.state
    cog = events_cog.EventsCog(game.bot)
    playing = asyncio.ensure_future(cog.run_one_event(forced=EventType.TRAINING))
    game._tasks.append(playing)
    await game.wait_for_buttons("join")
    # The sign-up: busy as any multiplayer event - only the stable is excepted.
    assert state.busy_owner == "event:TRAINING" and state.event_alive and state.event_type is EventType.TRAINING
    assert state.activity_running and not state.free_for_activity and state.stable_open
    assert await cog.run_one_event(forced=EventType.TRAINING) is None        # not twice at once
    assert await cog.run_one_event(forced=EventType.MEMORY) is None
    assert game.channel.sent_count(TITLE) == 1

    crowd = [fighter(game, name) for name in ("Ala", "Ola", "Ela")]
    enrol(*crowd)                                                             # (the roster of this sign-up)
    fights.hold()
    await sign_up.close()
    await until(lambda: len(fights.pairs) == 1)
    # The fights: still busy, the stable blocked again; they are not typed fights.
    assert state.busy_owner == "event:TRAINING" and state.activity_running and not state.stable_open
    assert not challenge.fight_on(game.channel)
    assert await cog.run_one_event(forced=EventType.TRAINING) is None
    assert rewards.shards == []                                               # nothing before the winner is known
    fights.release()
    assert await playing is EventType.TRAINING
    assert not state.busy and not state.event_alive and state.free_for_activity
    assert len(rewards.shards) == 3 and len(rewards.loot) == 1 and game.bot.owner_log == []


async def test_a_failure_in_the_middle_frees_the_channel_and_grants_nothing(game, picture, enrol, rewards, fights,
                                                                          in_order):
    crowd = [fighter(game, name) for name in ("Ala", "Ola", "Ela", "Ula")]
    enrol(*crowd)
    fights.fail_on = 2                                                        # the second fight breaks
    cog = events_cog.EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.TRAINING) is None
    state = game.bot.state
    assert not state.busy and not state.stable_open and state.free_for_activity
    assert len(fight_messages(game)) == 1 and not game.channel.contains("Trening wygrywa")
    assert rewards.shards == [] and rewards.scrolls == [] and rewards.loot == []
    (report,) = game.bot.owner_log
    assert report == "Event TRAINING przerwany przez błąd: `RuntimeError: Discord 503`"
    assert no_listeners(game) and no_live_buttons(game)
    # The next event starts as usual - and a new training grants its rewards once.
    fights.fail_on = None
    assert await cog.run_one_event(forced=EventType.TRAINING) is EventType.TRAINING
    assert len(rewards.shards) == 4 and len(rewards.scrolls) == 1 and len(rewards.loot) == 1


async def test_the_training_does_not_start_while_the_channel_is_taken(game, picture, enrol, rewards):
    enrol(fighter(game, "Ala"), fighter(game, "Ola"))
    cog = events_cog.EventsCog(game.bot)
    before = len(game.channel.sent)
    with game.bot.state.hold_busy("hunt"):
        assert await cog.run_one_event(forced=EventType.TRAINING) is None
    assert len(game.channel.sent) == before and rewards.shards == []
    assert await cog.run_one_event(forced=EventType.TRAINING) is EventType.TRAINING
