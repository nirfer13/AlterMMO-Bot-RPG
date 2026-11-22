"""The fight journal (core/fightlog.py), the answer after the time is up, the answer to "lag"
(features/fightreport.py) and ``$walka`` (cogs/fightlog.py)."""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import re
import time
from datetime import timedelta

import discord
import pytest
from discord.ext import commands

import fakes
from altermmo import bot as bot_module
from altermmo.core import challenge, fightlog, guards
from altermmo.core.fightlog import End, LateState, Outcome
from altermmo.features import achievements, boss, daily, database, fightreport, tower

from fakes import FakeMember, FakeMessage, requested_word

ZERO = {"CRIT_PERC": 0.0, "REPLACE_PERC": 0.0, "DEF_PERC": 0.0, "DROP_PERC": 0.0,
        "LOWHP_PERC": 0.0, "SLOW_PERC": 0.0}

HOW = ("**Jak mierzę czas:** od chwili, gdy moje pytanie pojawiło się na Discordzie, do chwili, gdy "
       "Discord utworzył wiadomość z odpowiedzią (oba znaczniki czasu nadaje Discord) - moje własne "
       "opóźnienie się do tego nie wlicza.")
STEADY = (" W tej turze mój ping wynosił **85 ms**, a połączenie z Discordem było stabilne: po stronie "
          "bota nie widzę w tej walce zakłóceń.")
BLIND = (" Nie widzę natomiast drogi między Twoim urządzeniem a Discordem, w obie strony - jeśli tam coś się "
         "opóźniło, ten czas wliczył się do Twojej odpowiedzi.")
BLIND_NOTHING = (" Nie widzę natomiast drogi między Twoim urządzeniem a Discordem, w obie strony - jeśli "
                 "wiadomość tam utknęła, Discord jej nie dostał.")


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """No waiting for a second hunter, a quick look-again of the waiting answer."""
    monkeypatch.setattr(daily, "JOIN_TIME", 0.03)
    monkeypatch.setattr(guards, "_last_notice", {})
    monkeypatch.setattr(fightreport, "POLL", 0.005)
    daily._gathering.clear()
    daily._panels.clear()


@pytest.fixture
def skills(monkeypatch):
    """Pet bonuses without a database, for every kind of fight: ``skills[player.id] = {"DEF_PERC": 100}``."""
    table: dict[int, dict] = {}

    async def fake_pet_skills(bot, player_id):
        return {**ZERO, **{key: float(value) for key, value in table.get(player_id, {}).items()}}

    monkeypatch.setattr(daily, "pet_skills", fake_pet_skills)
    monkeypatch.setattr(boss, "_pet_skills", fake_pet_skills)
    return table


@pytest.fixture
def no_rewards(monkeypatch):
    """A won fight hands nothing out (no database needed)."""
    async def nothing(*args, **kwargs):
        return ""

    monkeypatch.setattr(boss, "rand_loot", nothing)
    monkeypatch.setattr(daily, "rand_loot", nothing)
    monkeypatch.setattr(tower, "_store_record", nothing)
    monkeypatch.setattr(database, "update_ranking_table", nothing)
    monkeypatch.setattr(achievements, "add_hunts", nothing)


@pytest.fixture
async def cog(game):
    """The extension with the "lag" listener and ``$walka``."""
    await game.bot.load_extension("altermmo.cogs.fightlog")
    for channel in game.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    return game.bot.get_cog("FightLog")


@pytest.fixture
def pings(game):
    """Every message the bot sends to the boss channel, with its ``allowed_mentions``."""
    seen: list[tuple[FakeMessage, discord.AllowedMentions | None]] = []
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        message = await real_send(content, **kwargs)
        seen.append((message, kwargs.get("allowed_mentions")))
        return message

    game.channel.send = send
    return seen


def answers(*, wrong=frozenset(), silent=frozenset()):
    """An ``auto_answer`` responder with several special rounds (1-based prompt numbers)."""
    counter = itertools.count(1)

    def responder(message):
        if not message.content or "~~" in message.content:
            return None
        word = requested_word(message.content)
        if word is None:
            return None
        number = next(counter)
        if number in silent:
            return None
        return "zlaodpowiedz" if number in wrong else word

    return responder


def team_answers(game, players, *, wrong_rounds=(), silent_rounds=(), intruder_rounds=()) -> None:
    """Everybody answers only the prompts addressed to them (``N. **Name**: ...``)."""
    by_name = {player.name: player for player in players}
    counter = itertools.count(1)

    def hook(message):
        match = re.match(r"^\d+\. \*\*(.+?)\*\*: ", message.content)
        word = requested_word(message.content)
        if match is None or word is None:
            return
        number = next(counter)
        addressed = by_name[match.group(1)]
        if number in silent_rounds:
            return
        if number in wrong_rounds:
            game.say_later(addressed, "zlaodpowiedz", 0)
        elif number in intruder_rounds:
            game.say_later(next(p for p in players if p.id != addressed.id), word, 0)
        else:
            game.say_later(addressed, word, 0)

    game.channel.send_hooks.append(hook)


def take_turns(monkeypatch) -> None:
    """``random.choice`` over hunters goes round-robin instead of being random."""
    real_choice = random.choice
    counter = itertools.count()

    def choice(seq):
        if seq and all(isinstance(item, FakeMember) for item in seq):
            return seq[next(counter) % len(seq)]
        return real_choice(seq)

    monkeypatch.setattr(random, "choice", choice)


async def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.002)


async def quiet(game) -> None:
    """Let the listeners run and wait until no answer to a complaint is on its way."""
    await game.settle()
    await wait_until(lambda: fightlog.pending()[1] == 0)
    await game.settle()


async def complain(game, member, text: str = "lag", *, channel=None) -> FakeMessage | None:
    """``member`` writes ``text``; the bot's answer on the boss channel (None when there is none)."""
    before = len(game.channel.sent)
    game.say(member, text, channel=channel)
    await quiet(game)
    new = game.channel.sent[before:]
    assert len(new) <= 1
    return new[0] if new else None


def outcomes(fight) -> list[tuple[int, Outcome | None]]:
    return [(entry.number, entry.outcome) for entry in fight.rounds]


class Recorded:
    """A fight written into the journal by hand, with exact numbers (the way ``challenge.ask`` does it)."""

    def __init__(self, game, kind, fighters, *, detail=None, channel=None) -> None:
        self.game = game
        self.channel = channel or game.channel
        self.fighters = list(fighters)
        self.fight = fightlog.begin(self.channel, kind, self.fighters, detail=detail)
        self.prompts: dict[int, FakeMessage] = {}

    def _ask(self, turn, asked, word, limit, latency, grace):
        entry = fightlog.open_round(self.channel.id, turn=turn, asked=asked, word=word,
                                    prompt=f"{turn}. **{asked}**: wpisz **{' '.join(word.upper())}**",
                                    limit=limit, players={fighter.id for fighter in self.fighters})
        prompt = FakeMessage(self.channel, self.game.bot.fake_user, "pytanie")
        entry.sent(prompt, latency=latency, grace=grace, send_time=0.05)
        self.prompts[turn] = prompt
        return entry, prompt

    def answer(self, turn, asked, word, limit, elapsed, *, typed=None, by=None, latency=0.085, grace=1.0):
        entry, prompt = self._ask(turn, asked, word, limit, latency, grace)
        message = FakeMessage(self.channel, by or asked, typed or word)
        message.created_at = prompt.created_at + timedelta(seconds=elapsed)
        entry.answered(message, elapsed)
        return entry.entry

    def timeout(self, turn, asked, word, limit, *, in_grace=None, latency=0.085, grace=1.0, stall=0.0):
        entry, prompt = self._ask(turn, asked, word, limit, latency, grace)
        if in_grace is not None:
            after, text = in_grace
            message = FakeMessage(self.channel, asked, text)
            message.created_at = prompt.created_at + timedelta(seconds=after)
            entry.late_in_grace(message, after)
        entry.silent()
        entry.timed_out(stall=stall)
        return entry.entry

    def disrupted(self, turn, asked, word, limit, *, latency=0.085, grace=1.0):
        entry, _ = self._ask(turn, asked, word, limit, latency, grace)
        entry.silent()
        entry.disrupted(gateway=True)
        return entry.entry

    async def late(self, turn, player, text, after) -> FakeMessage:
        """``player``'s message arrives now; Discord created it ``after`` s after the prompt of ``turn``."""
        message = FakeMessage(self.channel, player, text)
        message.created_at = self.prompts[turn].created_at + timedelta(seconds=after)
        self.channel.messages.append(message)
        self.game.bot.dispatch("message", message)
        await self.game.settle()
        return message


async def lost_tower_floor(game, player, *, late=(2.31, "unik"), floor=5) -> Recorded:
    """Floor ``floor``: rounds 5 and 6 answered, round 7 ("unik", 1.6 s) timed out; then the late answer."""
    recorded = Recorded(game, fightlog.KIND_TOWER, [player], detail=floor)
    recorded.answer(5, player, "atak", 1.6, 1.21)
    recorded.answer(6, player, "blok", 1.6, 1.48)
    recorded.timeout(7, player, "unik", 1.6)
    fightlog.lost(game.channel, timed_out=True)
    if late is not None:
        await recorded.late(7, player, late[1], late[0])
    return recorded


# -- the journal: every kind of fight --------------------------------------------------------

async def test_a_cleared_tower_floor_is_recorded_round_by_round(game, skills, no_rewards):
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    assert await tower.tower_fight(game.scene(hunter), 2) is True

    fight = fightlog.last_fight(hunter.id)
    assert (fight.kind, fight.detail, fight.channel_id) == (fightlog.KIND_TOWER, 2, game.channel.id)
    assert fight.fighters == [(hunter.id, "Ala")] and not fight.party
    assert fight.end is End.WON and fight.winners == (hunter.id,) and fight.losers == () and fight.decisive is None
    assert outcomes(fight) == [(number, Outcome.CORRECT) for number in (1, 2, 3, 4)]
    words = [requested_word(m.content) for m in game.channel.sent if requested_word(m.content or "")]
    assert [entry.word for entry in fight.rounds] == words and len(words) == 4
    assert [entry.limit for entry in fight.rounds] == pytest.approx([15, 4, 4, 4])     # 8 s / floor 2
    for entry in fight.rounds:
        assert (entry.asked_id, entry.asked_name) == (hunter.id, "Ala")
        assert entry.typed == entry.word and entry.typed_by_id == hunter.id
        assert 0.0 <= entry.elapsed < 1.0 and entry.late is None and entry.late_state is LateState.NONE
        assert entry.latency == 0.0 and entry.grace == pytest.approx(0.05) and not entry.disturbed
        assert entry.prompt.startswith(f"{entry.number}. **Ala**: ") and entry.saved is None
    assert fightlog.open_fight(game.channel) is None and fightlog.pending() == (0, 0)


async def test_a_floor_lost_by_a_mistake(game, skills):
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, wrong_on_round=2)
    assert await tower.tower_fight(game.scene(hunter), 3) is False
    fight = fightlog.last_fight(hunter.id)
    assert fight.end is End.LOST and fight.losers == (hunter.id,) and fight.reason == "wrong word"
    assert outcomes(fight) == [(1, Outcome.CORRECT), (2, Outcome.WRONG_WORD)]
    decisive = fight.decisive
    assert decisive is fight.rounds[-1] and decisive.typed == "zlaodpowiedz" and decisive.word != "zlaodpowiedz"
    assert decisive.elapsed is not None and decisive.late_state is LateState.NONE and fightlog.pending() == (0, 0)


async def test_a_floor_lost_by_a_timeout_is_watched_for_the_late_answer(game, skills, short_timeouts):
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, silent_on_round=2)
    assert await tower.tower_fight(game.scene(hunter), 1) is False
    fight = fightlog.last_fight(hunter.id)
    assert fight.end is End.LOST and fight.reason == "timeout"
    assert outcomes(fight) == [(1, Outcome.CORRECT), (2, Outcome.TIMEOUT)]
    decisive = fight.decisive
    assert decisive.limit == pytest.approx(0.008) and decisive.elapsed is None and decisive.typed is None
    assert decisive.late_state is LateState.WATCHING and fightlog.pending() == (1, 0)
    game.say(hunter, decisive.word.upper())                       # the answer, after the fight is over
    await game.settle()
    assert decisive.late_state is LateState.FOUND and decisive.late.text == decisive.word.upper()
    assert decisive.late.after > decisive.limit and not decisive.late.in_grace
    assert decisive.over == pytest.approx(decisive.late.after - 0.008) and fightlog.pending() == (0, 0)


async def test_pet_protection_critical_hits_and_pet_attacks(game, skills, no_rewards, short_timeouts):
    hunter = game.add_member("Ala")
    skills[hunter.id] = {"DEF_PERC": 100, "CRIT_PERC": 100, "LOWHP_PERC": 30}
    game.auto_answer(hunter, responder=answers(wrong={1}, silent={2}))
    assert await tower.tower_fight(game.scene(hunter), 5) is True
    fight = fightlog.last_fight(hunter.id)
    assert fight.end is End.WON
    assert outcomes(fight) == [(1, Outcome.WRONG_WORD), (2, Outcome.TIMEOUT), (3, Outcome.CORRECT),
                               (5, Outcome.CORRECT), (7, Outcome.CORRECT)]      # (critical hits skip numbers)
    assert [entry.saved for entry in fight.rounds] == [fightlog.SAVED_BY_PET, fightlog.SAVED_BY_PET,
                                                       None, None, None]
    # The protected timeout: the player was asked the next word at once - nothing is waited for.
    assert fight.rounds[1].late is None and fight.rounds[1].late_state is LateState.NEXT_PROMPT
    assert fightlog.pending() == (0, 0)

    game.channel.send_hooks.clear()
    skills[hunter.id] = {"REPLACE_PERC": 100}
    assert await tower.tower_fight(game.scene(hunter), 2) is True
    fight = fightlog.last_fight(hunter.id)
    assert outcomes(fight) == [(number, Outcome.PET_ATTACK) for number in (1, 2, 3, 4)]
    assert all(entry.asked_id == hunter.id and entry.word for entry in fight.rounds)
    assert "atak towarzysza, bez pytania" in fightreport.explain(fight, None, subject_id=hunter.id)


async def test_a_disrupted_round_is_recorded_and_repeated(game, skills, no_rewards, short_timeouts):
    hunter = game.add_member("Ala")
    seen = itertools.count(1)

    async def outage():
        game.bot.net.mark_disconnected()
        await asyncio.sleep(0.01)
        game.bot.net.mark_connected()

    def responder(message):
        word = requested_word(message.content or "")
        if word is None:
            return None
        if next(seen) == 2:                                       # the first "2. ..." is lost with the link
            game._tasks.append(asyncio.ensure_future(outage()))
            return None
        return word

    game.auto_answer(hunter, responder=responder)
    assert await tower.tower_fight(game.scene(hunter), 1) is True
    fight = fightlog.last_fight(hunter.id)
    assert outcomes(fight) == [(1, Outcome.CORRECT), (2, Outcome.DISRUPTED), (2, Outcome.CORRECT)]
    assert fight.rounds[1].disturbed and fight.rounds[1].late_state is LateState.NONE
    assert fight.end is End.WON and len(fight.disrupted_rounds()) == 1 and fightlog.pending() == (0, 0)
    assert fightreport.troubles(fight) == [
        "tura 2 została zakłócona przez połączenie i powtórzona (nie liczyła się)"]
    assert "tura 2: zakłócona przez połączenie - powtórzona, nie liczy się" in fightreport.explain(fight, None)


async def test_repeated_disruptions_interrupt_the_recorded_fight(game, skills, monkeypatch):
    hunter = game.add_member("Ala")

    async def dead_line(bot, channel, prompt, **kwargs):
        return challenge.RoundResult(challenge.Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", dead_line)
    assert await tower.tower_fight(game.scene(hunter), 1) is None
    fight = fightlog.last_fight(hunter.id)
    assert fight.end is End.INTERRUPTED and fight.losers == () and "disrupted" in fight.reason
    assert fightlog.open_fight(game.channel) is None


async def test_a_party_hunt_lost_by_the_answer_of_the_wrong_hunter(game, skills, monkeypatch):
    game.bot.debug = False
    monkeypatch.setattr(daily, "JOIN_TIME", 0.3)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    take_turns(monkeypatch)
    team_answers(game, [ala, ola], intruder_rounds={2})
    game.say_later(ola, "$polowanie", 0.01)
    assert await daily.hunt_mobs(game.scene(ala), 1, False, None) is False
    fight = fightlog.last_fight(ola.id)
    assert fight is fightlog.last_fight(ala.id) and fight.party
    assert (fight.kind, fight.detail) == (fightlog.KIND_HUNT, 1)
    assert fight.fighters == [(ala.id, "Ala"), (ola.id, "Ola")]
    assert fight.end is End.LOST and set(fight.losers) == {ala.id, ola.id}
    assert outcomes(fight) == [(1, Outcome.CORRECT), (2, Outcome.WRONG_PLAYER)]
    decisive = fight.decisive
    assert (decisive.asked_id, decisive.typed_by_id, decisive.typed_by_name) == (ola.id, ala.id, "Ala")
    assert decisive.typed == decisive.word


async def test_a_solo_hunt_won_and_one_lost_by_a_timeout(game, skills, no_rewards, short_timeouts):
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is True
    fight = fightlog.last_fight(hunter.id)
    assert fight.kind == fightlog.KIND_HUNT and fight.end is End.WON and not fight.party
    assert all(entry.outcome is Outcome.CORRECT for entry in fight.rounds) and len(fight.rounds) >= 3

    game.channel.send_hooks.clear()
    game.auto_answer(hunter, silent_on_round=2)
    assert await daily.hunt_mobs(game.scene(hunter), 0, False, None) is False
    lost = fightlog.last_fight(hunter.id)
    assert lost is not fight and lost.end is End.LOST and lost.decisive.outcome is Outcome.TIMEOUT
    assert lost.decisive.number == 2 and fightlog.recent() == [fight, lost]


async def test_a_single_boss_fight_lost_by_a_timeout_and_one_won(game_db, skills, no_rewards, monkeypatch,
                                                                 short_timeouts):
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 4)
    game_db.auto_answer(hunter, silent_on_round=3)
    assert not (await boss.single_fight(game_db.scene(hunter), hunter, 2, False, None)).won
    fight = fightlog.last_fight(hunter.id)
    assert (fight.kind, fight.detail, fight.end) == (fightlog.KIND_BOSS, 2, End.LOST)
    assert outcomes(fight) == [(1, Outcome.CORRECT), (2, Outcome.CORRECT), (3, Outcome.TIMEOUT)]
    assert fight.decisive.number == 3 and fight.decisive.asked_id == hunter.id
    assert fight.decisive.limit == pytest.approx(0.003)                    # (5 - rarity) s, scaled down
    assert fightreport.label(fight) == "walka z bossem"

    game_db.channel.send_hooks.clear()
    skills[hunter.id] = {"REPLACE_PERC": 100}
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    won = fightlog.last_fight(hunter.id)
    assert won.end is End.WON and outcomes(won) == [(n, Outcome.PET_ATTACK) for n in (1, 2, 3, 4)]


async def test_a_group_boss_fight_uses_the_spare_chance_and_is_lost(game_db, skills, monkeypatch):
    players = [game_db.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 9)
    team_answers(game_db, players, wrong_rounds={2}, intruder_rounds={4})
    assert not (await boss.group_fight(game_db.scene(players[0]), players, False, None)).won
    fight = fightlog.last_fight(players[3].id)
    assert fight.kind == fightlog.KIND_GROUP and fight.end is End.LOST
    assert set(fight.losers) == {player.id for player in players} and len(fight.fighters) == 4
    assert outcomes(fight) == [(1, Outcome.CORRECT), (2, Outcome.WRONG_WORD), (3, Outcome.CORRECT),
                               (4, Outcome.WRONG_PLAYER)]
    assert fight.rounds[1].saved == fightlog.SAVED_BY_SPARE and fight.rounds[3].saved is None
    assert fight.decisive is fight.rounds[3] and fight.decisive.typed_by_id != fight.decisive.asked_id
    text = fightreport.explain(fight, None, subject_id=players[0].id)
    assert "błędne słowo (zapasowa szansa drużyny)" in text and "drużynowa walka z bossem" in text


async def test_a_duel_has_a_winner_a_loser_and_his_last_failed_round(game_db, skills, no_rewards):
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")

    def spell(message):
        if "Zaklęcie to" in message.text:
            game_db.say_later(ola, requested_word(message.content), 0)

    game_db.channel.send_hooks.append(spell)
    team_answers(game_db, [ala, ola], intruder_rounds={1}, wrong_rounds={2})
    finished, winner = await boss.duel_fight(game_db.scene(), [ala, ola], False)
    assert finished and winner is ola
    fight = fightlog.last_fight(ala.id)
    assert fight is fightlog.last_fight(ola.id) and fight.kind == fightlog.KIND_DUEL
    assert fight.end is End.WON and fight.winners == (ola.id,) and fight.losers == (ala.id,)
    first, second = fight.rounds[0], fight.rounds[1]
    assert (first.number, first.asked_id, first.outcome, first.typed_by_id) == (1, ola.id, Outcome.WRONG_PLAYER, ala.id)
    assert (second.number, second.asked_id, second.outcome) == (2, ala.id, Outcome.WRONG_WORD)
    assert fight.decisive is second and fight.reason == "duel"
    assert all(entry.limit == pytest.approx(5.0) for entry in fight.rounds)
    assert fightreport.label(fight) == "pojedynek"


async def test_a_duel_that_ends_in_a_draw_has_no_loser(game_db, skills, monkeypatch, short_timeouts):
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    monkeypatch.setattr(boss, "DUEL_MAX_TURNS", 3)
    assert await boss.duel_fight(game_db.scene(), [ala, ola]) == (False, None)
    fight = fightlog.last_fight(ala.id)
    assert fight.end is End.DRAW and fight.losers == () and fight.decisive is None
    assert [entry.outcome for entry in fight.rounds] == [Outcome.TIMEOUT] * 3
    assert {entry.asked_id for entry in fight.rounds} == {ala.id, ola.id}


async def test_a_question_that_is_not_a_fight_round_is_not_recorded(game):
    """``challenge.ask`` without a word (the skill gem's question) and without a fight: nothing is kept."""
    player = game.add_member("Ala")
    assert (await challenge.ask(game.bot, game.channel, "Oznacz gracza", players=[player], timeout=0.03)).outcome \
        is challenge.Outcome.TIMEOUT
    assert fightlog.recent() == [] and fightlog.pending() == (0, 0)
    # A word without a fight on the channel: nothing either.
    await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**", players=[player], timeout=0.03, word="blok",
                        asked=player, turn=1)
    assert fightlog.recent() == [] and fightlog.pending() == (0, 0)
    # A fight on the channel, a question without a word (it is not one of its rounds).
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=1)
    await challenge.ask(game.bot, game.channel, "Oznacz gracza", players=[player], timeout=0.03)
    assert fight.rounds == [] and fightlog.pending() == (0, 0)


async def test_the_journal_is_bounded_and_forgets(game, monkeypatch):
    players = [game.add_member(f"Gracz{number}") for number in range(fightlog.RING + 5)]
    for player in players:
        fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=1)
        fightlog.lost(game.channel)
    assert len(fightlog.recent()) == fightlog.RING
    assert fightlog.last_fight(players[0].id) is not None          # (the last fight of each player stays)

    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [players[0]], detail=2)
    for turn in range(fightlog.MAX_ROUNDS + 20):
        fightlog.pet_attack(game.channel, turn + 1, players[0], "atak")
    assert len(fight.rounds) == fightlog.MAX_ROUNDS and fight.rounds[-1].number == fightlog.MAX_ROUNDS + 20

    old = fightlog.last_fight(players[10].id)
    assert old in fightlog.recent()
    old.ended = time.monotonic() - fightlog.EXPIRE_AFTER - 1        # 30 minutes later
    assert fightlog.last_fight(players[10].id) is None and old not in fightlog.recent()
    assert fightlog.last_fight(players[11].id) is not None

    monkeypatch.setattr(fightlog, "MAX_PLAYERS", 3)
    fightlog.begin(game.channel, fightlog.KIND_TOWER, [players[3]], detail=1)
    assert len(fightlog._last) == 3


async def test_what_a_player_typed_is_kept_short(game):
    player = game.add_member("Ala")
    recorded = Recorded(game, fightlog.KIND_TOWER, [player], detail=1)
    entry = recorded.answer(1, player, "unik", 5.0, 1.0, typed="x" * 500 + "\n\n drugi wiersz")
    assert len(entry.typed) == fightlog.TEXT_LIMIT and entry.typed.endswith("…") and "\n" not in entry.typed
    assert fightlog.clip("  dwa \n słowa ") == "dwa słowa"


async def test_a_broken_journal_never_reaches_the_fight(game, skills, no_rewards, monkeypatch, caplog):
    hunter = game.add_member("Ala")

    def broken(*args, **kwargs):
        raise RuntimeError("test: the journal is broken")

    game.auto_answer(hunter)
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR, logger="altermmo.core.fightlog"):
        patch.setattr(fightlog, "_append", broken)
        patch.setattr(fightlog, "_close", broken)
        assert await tower.tower_fight(game.scene(hunter), 1) is True       # played and won all the same
    assert "the fight is not affected" in caplog.text
    game.channel.send_hooks.clear()

    # Garbage in: nothing raises, nothing is recorded.
    assert fightlog.begin(object(), fightlog.KIND_TOWER, [hunter]) is None
    assert fightlog.lost(object()) is None and fightlog.won(object()) is None
    assert fightlog.open_round(game.channel.id, turn=1, asked=object(), word="x", prompt="?", limit=1.0,
                               players=None) is fightlog.NO_ROUND
    fightlog.heard(object())
    fightlog.NO_ROUND.sent(None)
    fightlog.NO_ROUND.silent()
    fightlog.NO_ROUND.timed_out()
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [hunter], detail=1)
    entry = fightlog.open_round(game.channel.id, turn=1, asked=hunter, word="unik", prompt="?", limit=1.0,
                                players={hunter.id})
    entry.sent(None)                                                         # no prompt: logged, not raised
    entry.answered(object(), 1.0)
    entry.late_in_grace(object(), 2.0)
    entry.silent()
    assert fight.rounds[0].late_state is LateState.NONE and fightlog.pending() == (0, 0)


# -- the answer after the time is up -----------------------------------------------------------

async def ask_round(game, player, *, turn=7, word="unik", timeout=0.05):
    return await challenge.ask(game.bot, game.channel, f"{turn}. **{player}**: wpisz **{' '.join(word.upper())}**",
                               players=[player], timeout=timeout, word=word, asked=player, turn=turn)


async def test_an_answer_inside_the_grace_window_is_recorded_with_its_time(game, monkeypatch, caplog):
    monkeypatch.setattr(challenge, "MIN_GRACE", 1.0)
    player = game.add_member("Ala")
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    game.say_later(player, "UNIK", 0.6)                 # heard by the round, but created after the limit
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        result = await ask_round(game, player, timeout=0.1)
    assert result.outcome is challenge.Outcome.TIMEOUT
    entry = fight.rounds[0]
    assert entry.outcome is Outcome.TIMEOUT and entry.elapsed is None and entry.grace == pytest.approx(1.0)
    assert entry.late.in_grace and entry.late.text == "UNIK" and entry.late_state is LateState.FOUND
    assert 0.55 < entry.late.after < 1.1 and entry.over == pytest.approx(entry.late.after - 0.1)
    assert fightlog.pending() == (0, 0)                 # (known already: nothing to wait for)
    assert re.search(r"Late answer: tower floor 5 \| Ala \(\d+\) \| round 7, word 'unik', limit 0\.10 s: "
                     r"first message \d\.\d\d s after the prompt, \d\.\d\d s over the limit: 'UNIK'", caplog.text)


async def test_an_answer_after_the_bot_stopped_listening_is_recorded(game, monkeypatch, caplog):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player = game.add_member("Ala")
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    listeners = len(game.bot.extra_events.get("on_message", []))
    result = await ask_round(game, player)              # listens 0.05 s + 0.05 s
    assert result.outcome is challenge.Outcome.TIMEOUT
    entry = fight.rounds[0]
    assert entry.late_state is LateState.WATCHING and entry.late is None and fightlog.pending() == (1, 0)
    # The watch is no listener and no task: the round left nothing behind.
    assert len(game.bot.extra_events.get("on_message", [])) == listeners
    fightlog.lost(game.channel, timed_out=True)

    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        await asyncio.sleep(0.2)
        message = game.say(player, "unik")
        await game.settle()
    assert entry.late_state is LateState.FOUND and not entry.late.in_grace and entry.late.text == "unik"
    assert entry.late.after == pytest.approx((message.created_at - entry.prompt_at).total_seconds())
    assert 0.25 < entry.late.after < 1.0 and 0.15 < entry.watched < 1.0
    assert fightlog.pending() == (0, 0)
    assert re.search(r"Late answer: tower floor 5 \| Ala \(\d+\) \| round 7, word 'unik', limit 0\.05 s: "
                     r"first message \d\.\d\d s after the prompt, \d\.\d\d s over the limit: 'unik'", caplog.text)
    game.say(player, "unik")                            # only the first message counts
    await game.settle()
    assert entry.late.after == pytest.approx((message.created_at - entry.prompt_at).total_seconds())


async def test_an_answer_that_never_comes_is_recorded_as_such(game, monkeypatch, caplog):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 0.1)
    player = game.add_member("Ala")
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player)
    entry = fight.rounds[0]
    assert entry.late_state is LateState.WATCHING
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        await wait_until(lambda: entry.late_state is not LateState.WATCHING)
    assert entry.late_state is LateState.NOTHING and entry.late is None and entry.over is None
    assert 0.05 < entry.watched < 1.0 and fightlog.pending() == (0, 0) and not entry.watch_disturbed
    assert re.search(r"Late answer: tower floor 5 \| Ala \(\d+\) \| round 7, word 'unik', limit 0\.05 s: "
                     r"no answer, none within \d s after the round either", caplog.text)
    game.say(player, "unik")                            # far too late: the watch is over
    await game.settle()
    assert entry.late is None


async def test_only_the_asked_player_on_this_channel_can_give_the_late_answer(game, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player, stranger = game.add_member("Ala"), game.add_member("Obcy")
    robot = FakeMember("Robot", bot=True, guild=game.guild)
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player)
    entry = fight.rounds[0]
    game.say(stranger, "unik")
    game.say(robot, "unik")
    game.say(player, "unik", channel=game.announce_channel)
    game.say(player, "LAG!!!")                          # a complaint is not an answer
    game.say(player, "$wieża")                          # nor is a command
    await game.settle()
    assert entry.late_state is LateState.WATCHING and entry.late is None
    game.say(player, "no nie")
    await game.settle()
    assert entry.late_state is LateState.FOUND and entry.late.text == "no nie"


async def test_a_message_for_the_next_round_is_not_a_late_answer(game, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player = game.add_member("Ala")
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player, turn=1, word="unik")                       # timed out, watched
    assert fight.rounds[0].late_state is LateState.WATCHING
    game.say_later(player, "blok", 0.02)
    result = await ask_round(game, player, turn=2, word="blok", timeout=1.0)   # the next word, same player
    assert result.is_correct("blok", by=player)
    first, second = fight.rounds
    assert first.late is None and first.late_state is LateState.NEXT_PROMPT and first.watched < 1.0
    assert second.outcome is Outcome.CORRECT and second.stale_of is None
    assert fightlog.pending() == (0, 0)


async def test_the_word_of_the_previous_round_typed_in_the_next_one_is_its_late_answer(game, monkeypatch, caplog):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player = game.add_member("Ala")
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player, turn=1, word="unik")
    game.say_later(player, "Unik", 0.02)                 # still answering round 1
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        result = await ask_round(game, player, turn=2, word="blok", timeout=1.0)
    assert result.answered and not result.is_correct("blok")
    first, second = fight.rounds
    assert second.outcome is Outcome.WRONG_WORD and second.stale_of == 1 and second.typed == "Unik"
    assert first.late_state is LateState.FOUND and first.late.text == "Unik" and first.late.after > 0.1
    assert "Late answer: tower floor 5" in caplog.text and "round 1, word 'unik'" in caplog.text
    fightlog.lost(game.channel)
    text = fightreport.explain(fight, player.id)
    assert ("**Dlaczego:** spóźniona odpowiedź z poprzedniej tury została policzona jako odpowiedź na to pytanie. "
            "Słowo „blok”, limit **1.00 s** - odpowiedź przyszła w czasie (po **") in text
    assert ("ale brzmiała „Unik”. To słowo z tury 1: dotarło już po jej czasie, więc zostało policzone jako "
            "odpowiedź na następne pytanie.") in text
    assert "czas nie miał tu znaczenia" not in text


async def test_in_a_party_only_the_asked_hunter_is_waited_for(game, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    monkeypatch.setattr(challenge, "MIN_GRACE", 0.5)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    fight = fightlog.begin(game.channel, fightlog.KIND_HUNT, [ala, ola], detail=0)
    game.say_later(ola, "unik", 0.4)                     # the other hunter, too late: ends the listening
    await challenge.ask(game.bot, game.channel, "3. **Ala**: wpisz **U N I K**", players=[ala, ola], timeout=0.05,
                        word="unik", asked=ala, turn=3)
    entry = fight.rounds[0]
    assert entry.outcome is Outcome.TIMEOUT and entry.late is None and entry.late_state is LateState.WATCHING
    game.say(ola, "unik")
    await game.settle()
    assert entry.late_state is LateState.WATCHING
    await ask_round(game, ola, turn=4, word="blok", timeout=0.02)      # Ola is asked next: Ala is still waited for
    assert entry.late_state is LateState.WATCHING
    game.say(ala, "unik")
    await game.settle()
    assert entry.late_state is LateState.FOUND and entry.late.text == "unik"


async def test_a_command_typed_later_is_still_a_command(game, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player = game.add_member("Ala")
    ran: list[int] = []

    async def probe(ctx):
        ran.append(ctx.author.id)

    game.bot.add_command(commands.Command(probe, name="sonda"))
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player)
    entry = fight.rounds[0]
    message = game.say(player, "$sonda")
    message.edited_at, message._state = None, None
    await wait_until(lambda: ran == [player.id])                    # processed by the commands as always
    assert entry.late_state is LateState.WATCHING                   # ... and not taken for an answer
    # A round started now still gets its own messages (nothing was swallowed by the watch).
    game.say_later(player, "blok", 0.02)
    assert (await ask_round(game, player, turn=8, word="blok", timeout=1.0)).is_correct("blok", by=player)


async def test_no_task_and_no_listener_is_left_behind(game, skills, short_timeouts, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 0.3)
    hunter = game.add_member("Ala")
    current = asyncio.current_task()
    before = asyncio.all_tasks()
    listeners = len(game.bot.extra_events.get("on_message", []))
    game.auto_answer(hunter, silent_on_round=2)
    assert await tower.tower_fight(game.scene(hunter), 1) is False
    assert fightlog.pending() == (1, 0)                              # watching - with a timer, not a task
    await game.cleanup()
    await game.settle()
    left = {task for task in asyncio.all_tasks() - before if task is not current and not task.done()}
    assert left == set() and len(game.bot.extra_events.get("on_message", [])) == listeners
    entry = fightlog.last_fight(hunter.id).decisive
    await wait_until(lambda: entry.late_state is LateState.NOTHING)   # it never outlives its bound
    assert fightlog.pending() == (0, 0) and fightlog._watches == {}
    assert {task for task in asyncio.all_tasks() - before if task is not current and not task.done()} == set()


async def test_shutdown_stops_the_watches_and_the_waiting_answers(game, cog, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 60.0)
    player = game.add_member("Ala")
    recorded = await lost_tower_floor(game, player, late=None)
    entry = recorded.fight.decisive
    watch = fightlog._watch_of(entry)
    assert entry.late_state is LateState.WATCHING and watch.handle is not None
    game.say(player, "lag")                              # the answer waits for the watch
    await game.settle()
    assert fightlog.pending() == (1, 1)
    waiting = [task for task in asyncio.all_tasks() if task.get_name() == "fight-report"]
    assert len(waiting) == 1

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(commands.Bot, "close", nothing)
    monkeypatch.setattr(game.bot.db, "close", nothing)
    await game.bot.close()                               # AlterBot.close stops the journal
    assert fightlog.pending() == (0, 0) and waiting[0].cancelled() and watch.handle is None
    assert entry.late_state is LateState.STOPPED and entry.late is None
    assert not game.channel.contains("Sprawdziłem zapis")
    game.say(player, "unik")                             # nothing listens any more
    await game.settle()
    assert entry.late is None


# -- the lines in the log file ------------------------------------------------------------------

async def test_one_info_line_per_lost_fight_and_one_for_the_late_answer(game, caplog):
    player = game.add_member("Ala")
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        recorded = await lost_tower_floor(game, player)
    lines = [record.getMessage() for record in caplog.records if record.name == "altermmo.core.fightlog"]
    assert lines == [
        f"Fight lost: tower floor 5 | Ala ({player.id}) | round 7, word 'unik', limit 1.60 s: no answer in time "
        "(listening 20 s more for a late one) | latency 85 ms, grace 1.00 s, connection steady, "
        "0 disrupted round(s)",
        f"Late answer: tower floor 5 | Ala ({player.id}) | round 7, word 'unik', limit 1.60 s: first message "
        "2.31 s after the prompt, 0.71 s over the limit: 'unik'",
    ]
    assert all(record.levelno == logging.INFO for record in caplog.records
               if record.name == "altermmo.core.fightlog")
    assert fightlog.lost_line(recorded.fight).endswith(
        "first message 2.31 s after the prompt, 0.71 s over the limit: 'unik' | latency 85 ms, grace 1.00 s, "
        "connection steady, 0 disrupted round(s)")


async def test_the_lost_line_for_a_mistake_a_party_and_a_troubled_fight(game, caplog):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    recorded = Recorded(game, fightlog.KIND_HUNT, [ala, ola], detail=2)
    recorded.disrupted(1, ala, "atak", 15.0)
    recorded.answer(1, ala, "atak", 15.0, 2.0)
    recorded.answer(2, ola, "blok", 3.0, 1.25, typed="blik", latency=0.62)
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        fightlog.lost(game.channel, timed_out=False)
    assert caplog.records[-1].getMessage() == (
        f"Fight lost: hunt (rarity 2) | Ala ({ala.id}), Ola ({ola.id}) | round 2 for Ola, word 'blok', "
        "limit 3.00 s: wrong word 'blik' after 1.25 s | latency 620 ms, grace 1.00 s, connection steady, "
        "1 disrupted round(s)")

    recorded = Recorded(game, fightlog.KIND_GROUP, [ala, ola])
    recorded.answer(4, ala, "unik", 5.0, 0.9, by=ola)
    assert fightlog.lost_line(fightlog.lost(game.channel)) == (
        f"Fight lost: group boss | Ala ({ala.id}), Ola ({ola.id}) | round 4 for Ala, word 'unik', limit 5.00 s: "
        "answered by Ola instead of Ala after 0.90 s: 'unik' | latency 85 ms, grace 1.00 s, connection steady, "
        "0 disrupted round(s)")

    recorded = Recorded(game, fightlog.KIND_DUEL, [ala, ola])
    recorded.answer(1, ala, "atak", 5.0, 1.0)
    recorded.timeout(2, ola, "blok", 5.0, in_grace=(5.9, "blok"))
    recorded.answer(3, ala, "unik", 5.0, 1.0)
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        fightlog.won(game.channel, winner=ala, loser=ola)
    assert caplog.records[-1].getMessage() == (
        f"Fight lost: duel | Ola ({ola.id}) lost to Ala | 1 round(s) asked, 1 without an answer in time, "
        "0 with a wrong word; last failed round 2 for Ola, word 'blok', limit 5.00 s: first message 5.90 s after "
        "the prompt, 0.90 s over the limit: 'blok' | latency 85 ms, grace 1.00 s, connection steady, "
        "0 disrupted round(s)")


async def test_a_won_fight_writes_no_lost_line(game, skills, no_rewards, caplog):
    hunter = game.add_member("Ala")
    game.auto_answer(hunter)
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        assert await tower.tower_fight(game.scene(hunter), 1) is True
    assert [record for record in caplog.records if record.name == "altermmo.core.fightlog"] == []


# -- "lag" ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "lag", "lagi", "laga", "lagów", "lagu", "lagiem", "lagami", "laguje", "lagują", "lagowało", "zlagowało",
    "ale lag", "laaag", "lagg", "LAG!!!", "co za lagi", "przez laga", "Lagi...", "LAAAAG", "ale lagiiii",
    "ale mnie zlagowało", "przylagowało mi", "zalagowało", "lagnęło", "no i lag", "lag?", "miałem laga :(",
    "ten lag mnie zabił", "lagow", "lagowalo", "lagging", "laggy", "lags", "to przez lagi, serio",
    "  Lag  ", "lag.", "(lag)", "lag,lag,lag",
])
def test_a_lag_complaint_is_recognised(text):
    assert fightlog.is_lag_complaint(text)


@pytest.mark.parametrize("text", [
    "flaga", "plaga", "lagier", "blaga", "błaga", "szlag", "szlag by to trafił", "biała flaga", "laguna",
    "łagodnie", "kolaga", "Lagos", "lagry", "zlag", "slag", "unik", "", "   ", None, "$lag", "$lagi teraz",
    "ping", "ping mi skoczył", "zacięło", "zacięło mi się", "zwiecha", "przycięło", "ale ścina",
    "wczoraj wieczorem graliśmy w pięciu i każdy z nas miał w tej samej chwili takiego laga, że aż strach pomyśleć "
    "co by było gdyby",
])
def test_other_messages_are_not_lag_complaints(text):
    assert not fightlog.is_lag_complaint(text)


# -- the answer to a complaint -------------------------------------------------------------------

async def test_a_late_answer_is_explained_with_the_log_and_pings_only_the_player(game, cog, pings):
    player = game.add_member("Ala")
    await lost_tower_floor(game, player)
    reply = await complain(game, player, "lagi")
    assert reply.content == (
        f"<@{player.id}> Sprawdziłem zapis tej walki: **Wieża Śmierci, piętro 5**, przegrana w turze 7.\n"
        "**Dlaczego:** Twoja odpowiedź przyszła po czasie. Słowo „unik”, limit **1.60 s** - wiadomość („unik”) "
        "została utworzona na Discordzie **2.31 s** po moim pytaniu, czyli **0.71 s po limicie** (zaliczam "
        "odpowiedzi utworzone do 0.25 s po limicie).\n"
        "**Ostatnie tury:**\n"
        "```\n"
        "tura 5: limit 1.60 s - 1.21 s, dobrze\n"
        "tura 6: limit 1.60 s - 1.48 s, dobrze\n"
        "tura 7: limit 1.60 s - 2.31 s, po czasie\n"
        "```\n" + HOW + STEADY + BLIND)
    assert len(reply.content) <= 2000 and not reply.embeds
    # An ordinary message on the channel that pings the player and nobody else.
    (message, mentions), = pings
    assert message is reply and mentions.users == [player]
    assert mentions.everyone is False and mentions.roles is False and mentions.replied_user is False
    assert game.bot.owner_log == []                                  # nothing wrong on the bot's side


async def test_no_answer_at_all_is_said_plainly(game, cog, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 0.05)
    player = game.add_member("Ala")
    recorded = await lost_tower_floor(game, player, late=None)
    await wait_until(lambda: recorded.fight.decisive.late_state is LateState.NOTHING)
    recorded.fight.decisive.watched = 20.0                             # (as in production)
    reply = await complain(game, player, "ale lag")
    assert reply.content.splitlines()[1] == (
        "**Dlaczego:** na to pytanie nie dotarła do mnie żadna odpowiedź. Słowo „unik”, limit **1.60 s** - "
        "w czasie tury nie dotarła żadna Twoja wiadomość, a przez następne 20 s po niej nie przyszła od Ciebie "
        "żadna odpowiedź (komend i wiadomości o lagu nie liczę).")
    assert "tura 7: limit 1.60 s - brak odpowiedzi\n" in reply.content
    assert reply.content.endswith(HOW + STEADY + BLIND_NOTHING)


async def test_a_late_message_that_is_not_the_word(game, cog):
    player = game.add_member("Ala")
    await lost_tower_floor(game, player, late=(4.0, "co jest *z tym* @everyone"))
    reply = await complain(game, player, "LAG!!!")
    assert reply.content.splitlines()[1] == (
        "**Dlaczego:** w czasie tury nie dotarła odpowiedź. Słowo „unik”, limit **1.60 s** - pierwsza Twoja "
        "wiadomość po tym pytaniu („co jest \\*z tym\\* @\u200beveryone”) została utworzona na Discordzie "
        "**4.00 s** po moim pytaniu, czyli **2.40 s po limicie**, i nie było to wymagane słowo.")


async def test_a_wrong_word_is_a_mistake_not_a_delay(game, cog):
    player = game.add_member("Ala")
    recorded = Recorded(game, fightlog.KIND_BOSS, [player], detail=1)
    recorded.answer(1, player, "atak", 7.0, 2.5)
    recorded.answer(2, player, "paruj", 4.0, 1.02, typed="parj")
    fightlog.lost(game.channel, timed_out=False)
    reply = await complain(game, player, "laguje")
    assert reply.content == (
        f"<@{player.id}> Sprawdziłem zapis tej walki: **walka z bossem**, przegrana w turze 2.\n"
        "**Dlaczego:** pomyłka w słowie - czas nie miał tu znaczenia. Słowo „paruj”, limit **4.00 s** - "
        "odpowiedź przyszła w czasie (po **1.02 s**), ale brzmiała „parj”.\n"
        "**Ostatnie tury:**\n"
        "```\n"
        "tura 1: limit 7.00 s - 2.50 s, dobrze\n"
        "tura 2: limit 4.00 s - 1.02 s, błędne słowo\n"
        "```\n" + HOW + STEADY)                                     # (no word about the player's connection)


async def test_an_answer_inside_the_grace_window_is_explained_at_once(game, cog):
    player = game.add_member("Ala")
    recorded = Recorded(game, fightlog.KIND_TOWER, [player], detail=3)
    recorded.timeout(1, player, "atak", 15.0, in_grace=(15.4, "atak"))
    fightlog.lost(game.channel, timed_out=True)
    assert fightlog.pending() == (0, 0)
    reply = await complain(game, player)
    assert ("wiadomość („atak”) została utworzona na Discordzie **15.40 s** po moim pytaniu, czyli "
            "**0.40 s po limicie**") in reply.content


async def test_trouble_on_the_bots_side_is_admitted_and_the_owner_is_told_once(game, cog):
    player = game.add_member("Ala")
    recorded = Recorded(game, fightlog.KIND_TOWER, [player], detail=5)
    recorded.answer(1, player, "atak", 15.0, 1.0)
    recorded.disrupted(2, player, "blok", 1.6)
    recorded.answer(2, player, "blok", 1.6, 1.0)
    recorded.timeout(3, player, "unik", 1.6, latency=0.62, stall=0.3)
    fightlog.lost(game.channel, timed_out=True)
    await recorded.late(3, player, "unik", 2.0)
    reply = await complain(game, player, "przez laga")
    found = ("tura 2 została zakłócona przez połączenie i powtórzona (nie liczyła się); mój ping w decydującej "
             "turze wynosił 620 ms; bot zaciął się w decydującej turze na 0.30 s")
    assert fightreport.troubles(recorded.fight) == found.split("; ")
    assert reply.content.endswith(
        HOW + f" W tej walce bot miał jednak problemy: {found}. Pomiar opiera się na znacznikach czasu Discorda, "
        "więc nie powinno to zmienić wyniku, ale nie mogę tego wykluczyć - zgłoszenie może być zasadne i "
        "przekazałem je właścicielowi." + BLIND)
    assert "tura 2: zakłócona przez połączenie - powtórzona, nie liczy się\n" in reply.content
    assert "stabilne" not in reply.content
    assert game.bot.owner_log == [
        f"Zgłoszenie laga może być zasadne: <@{player.id}> napisał o lagu po przegranej walce (Wieża Śmierci, "
        f"piętro 5), a zapis walki pokazuje problemy po stronie bota: {found}. Szczegóły: "
        f"`$walka {player.id}` (zapis jest trzymany 30 minut)."]
    assert await complain(game, player, "lagi!") is None and len(game.bot.owner_log) == 1


async def test_a_connection_drop_while_listening_for_the_late_answer_is_admitted(game, cog, monkeypatch, caplog):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 0.1)
    player = game.add_member("Ala")
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player)                        # a clean timeout: the journal listens on
    fightlog.lost(game.channel, timed_out=True)
    entry = fight.decisive
    assert entry.outcome is Outcome.TIMEOUT and not entry.disturbed and entry.late_state is LateState.WATCHING
    game.bot.net.mark_disconnected()                     # ... and the link drops meanwhile
    game.bot.net.mark_connected()
    with caplog.at_level(logging.INFO, logger="altermmo.core.fightlog"):
        await wait_until(lambda: entry.late_state is LateState.NOTHING)
    assert entry.watch_disturbed and "either (the connection DROPPED while listening)" in caplog.text
    trouble = ("moje połączenie z Discordem zostało zerwane, kiedy po decydującej turze nasłuchiwałem "
               "spóźnionej odpowiedzi - mogłem jej nie usłyszeć")
    assert fightreport.troubles(fight) == [trouble]
    reply = await complain(game, player)
    assert f" W tej walce bot miał jednak problemy: {trouble}. Pomiar opiera się" in reply.content
    assert "na to pytanie nie dotarła do mnie żadna odpowiedź" in reply.content
    assert len(game.bot.owner_log) == 1 and trouble in game.bot.owner_log[0]


async def test_a_high_latency_in_another_round_and_a_dropped_connection_count_as_trouble(game):
    player = game.add_member("Ala")
    recorded = Recorded(game, fightlog.KIND_TOWER, [player], detail=5)
    recorded.answer(1, player, "atak", 15.0, 1.0, latency=0.4)
    dropped = recorded.answer(2, player, "blok", 1.6, 1.0)
    dropped.disturbed = True
    recorded.timeout(3, player, "unik", 1.6, latency=0.399, stall=0.249)
    fightlog.lost(game.channel)
    assert fightreport.troubles(recorded.fight) == [
        "mój ping dochodził w tej walce do 400 ms", "połączenie z Discordem zostało zerwane w trakcie tury 2"]
    clean = await lost_tower_floor(game, player)
    assert fightreport.troubles(clean.fight) == []


async def test_only_one_answer_per_fight(game, cog):
    player = game.add_member("Ala")
    recorded = await lost_tower_floor(game, player)
    assert await complain(game, player, "lag") is not None
    assert await complain(game, player, "lagi") is None                # the log is on the channel already
    assert await complain(game, player, "LAAAG") is None
    assert recorded.fight.answered
    await lost_tower_floor(game, player, floor=6)                            # the next lost fight is a new matter
    assert "piętro 6" in (await complain(game, player, "znowu lag")).content


async def test_only_the_loser_and_only_about_a_fight_just_lost_here(game, cog, skills, no_rewards):
    player, stranger = game.add_member("Ala"), game.add_member("Obcy")
    robot = FakeMember("Robot", bot=True, guild=game.guild)
    assert await complain(game, player, "lag") is None                 # nobody fought at all
    recorded = await lost_tower_floor(game, player)
    assert await complain(game, stranger, "lag") is None               # somebody who did not fight
    assert await complain(game, robot, "lag") is None                  # a bot
    assert await complain(game, player, "flaga") is None               # not a complaint
    assert await complain(game, player, "$lag") is None                # a command
    assert await complain(game, player, "lag", channel=game.announce_channel) is None   # another channel
    private = fakes.open_dm(player, game.bot)                          # a private message
    assert await complain(game, player, "lag", channel=private) is None and private.sent == []
    assert not recorded.fight.answered

    recorded.fight.ended = time.monotonic() - fightreport.COMPLAINT_WINDOW - 1     # more than 10 minutes ago
    assert await complain(game, player, "lag") is None
    recorded.fight.ended = time.monotonic() - fightreport.COMPLAINT_WINDOW + 30
    assert await complain(game, player, "lag") is not None

    # A fight that was won gets no answer, even right after a lost one.
    await lost_tower_floor(game, player)
    game.auto_answer(player)
    assert await tower.tower_fight(game.scene(player), 1) is True
    assert await complain(game, player, "ale lagi") is None


async def test_lag_typed_in_a_running_fight_is_an_answer_not_a_complaint(game, cog):
    player = game.add_member("Ala")
    await lost_tower_floor(game, player)                                    # lost a moment ago ...
    fight = fightlog.begin(game.channel, fightlog.KIND_HUNT, [player], detail=0)    # ... and fighting again
    game.say_later(player, "lag", 0.02)
    result = await ask_round(game, player, turn=1, word="atak", timeout=1.0)
    await quiet(game)
    assert result.answered and fight.rounds[0].outcome is Outcome.WRONG_WORD and fight.rounds[0].typed == "lag"
    assert game.channel.sent_count("Sprawdziłem zapis") == 0 and not fight.answered


async def test_the_answer_waits_for_the_late_answer_watch(game, cog, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player = game.add_member("Ala")
    recorded = await lost_tower_floor(game, player, late=None)
    game.say(player, "lag")
    await asyncio.sleep(0.08)
    assert not game.channel.contains("Sprawdziłem zapis") and fightlog.pending() == (1, 1)
    await recorded.late(7, player, "unik", 2.31)                       # now it is known
    await quiet(game)
    assert game.channel.sent_count("Sprawdziłem zapis") == 1
    assert "**2.31 s** po moim pytaniu" in game.channel.sent[-1].content


async def test_the_wait_for_the_watch_is_bounded(game, cog, monkeypatch):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 0.05)
    monkeypatch.setattr(fightreport, "LATE_MARGIN", 0.05)
    player = game.add_member("Ala")
    recorded = await lost_tower_floor(game, player, late=None)
    fightlog._watch_of(recorded.fight.decisive).handle.cancel()        # a watch that would never end
    reply = await complain(game, player)
    assert recorded.fight.decisive.late_state is LateState.WATCHING
    assert reply.content.splitlines()[1].endswith(
        "w czasie tury nie dotarła żadna Twoja wiadomość; wciąż nasłuchuję, czy coś jeszcze przyjdzie.")


async def test_never_during_a_typed_fight_the_answer_comes_after_it(game, cog, pings):
    player, other = game.add_member("Ala"), game.add_member("Ola")
    await lost_tower_floor(game, player)
    running = asyncio.ensure_future(challenge.ask(game.bot, game.channel, "1. Wpisz **A T A K**",
                                                  players=[other], timeout=5))
    await wait_until(lambda: challenge.fight_on(game.channel))
    posted = len(game.channel.sent)
    game.say(player, "lag")
    await asyncio.sleep(0.1)
    assert len(game.channel.sent) == posted and fightlog.pending() == (0, 1)     # nothing between the prompts
    game.say(other, "atak")
    assert (await running).is_correct("atak")
    await quiet(game)
    assert len(game.channel.sent) == posted + 1 and "Sprawdziłem zapis" in game.channel.sent[-1].content


async def test_the_answer_is_dropped_when_the_channel_stays_busy(game, cog, monkeypatch, caplog):
    monkeypatch.setattr(fightreport, "REPLY_PATIENCE", 0.05)
    player, other = game.add_member("Ala"), game.add_member("Ola")
    recorded = await lost_tower_floor(game, player)
    running = asyncio.ensure_future(challenge.ask(game.bot, game.channel, "1. Wpisz **A T A K**",
                                                  players=[other], timeout=5))
    await wait_until(lambda: challenge.fight_on(game.channel))
    with caplog.at_level(logging.INFO, logger="altermmo.features.fightreport"):
        assert await complain(game, player) is None
    assert "the channel stayed busy" in caplog.text and recorded.fight.answered
    game.say(other, "atak")
    await running
    assert await complain(game, player) is None                        # dropped is dropped
    assert not game.channel.contains("Sprawdziłem zapis")


async def test_a_member_of_the_losing_party_may_ask(game, cog, pings):
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola"), game.add_member("Ela")
    recorded = Recorded(game, fightlog.KIND_HUNT, [ala, ola, ela], detail=1)
    recorded.answer(1, ala, "atak", 15.0, 2.2)
    recorded.answer(2, ela, "blok", 4.0, 1.9)
    recorded.timeout(3, ola, "unik", 4.0)
    fightlog.lost(game.channel, timed_out=True)
    await recorded.late(3, ola, "unik", 5.5)
    reply = await complain(game, ala, "co za lagi")
    assert reply.content == (
        f"<@{ala.id}> Sprawdziłem zapis tej walki: **polowanie w drużynie**, przegrana w turze 3 "
        "(pytanie było do **Ola**).\n"
        "**Dlaczego:** odpowiedź gracza **Ola** przyszła po czasie. Słowo „unik”, limit **4.00 s** - wiadomość "
        "(„unik”) została utworzona na Discordzie **5.50 s** po moim pytaniu, czyli **1.50 s po limicie** "
        "(zaliczam odpowiedzi utworzone do 0.25 s po limicie).\n"
        "**Ostatnie tury:**\n"
        "```\n"
        "tura 1 (Ala): limit 15.00 s - 2.20 s, dobrze\n"
        "tura 2 (Ela): limit 4.00 s - 1.90 s, dobrze\n"
        "tura 3 (Ola): limit 4.00 s - 5.50 s, po czasie\n"
        "```\n" + HOW + STEADY +
        " Nie widzę natomiast drogi między urządzeniem gracza a Discordem, w obie strony - jeśli tam coś się "
        "opóźniło, ten czas wliczył się do odpowiedzi gracza **Ola**.")
    assert pings[-1][1].users == [ala]                                 # Ola is named, not pinged
    assert await complain(game, ola, "lag") is None                    # one answer per fight
    assert await complain(game, ela, "lagi") is None


async def test_the_asked_member_of_a_party_is_spoken_to_directly(game):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    recorded = Recorded(game, fightlog.KIND_GROUP, [ala, ola])
    recorded.answer(1, ala, "atak", 7.0, 2.0)
    recorded.timeout(2, ola, "unik", 5.0)
    recorded.answer(3, ala, "blok", 5.0, 1.0, by=ola, typed="blok")
    fight = fightlog.lost(game.channel, timed_out=False)
    text = fightreport.explain(fight, ala.id)
    assert text.splitlines()[0] == (f"<@{ala.id}> Sprawdziłem zapis tej walki: **drużynowa walka z bossem**, "
                                    "przegrana w turze 3 (pytanie było do Ciebie).")
    assert text.splitlines()[1] == (
        "**Dlaczego:** odpisał ktoś inny, niż był pytany. Słowo „blok”, limit **5.00 s** - pytanie było do "
        "gracza **Ala**, a w czasie (po **1.00 s**) odpisał gracz **Ola** („blok”); w tej walce liczy się to jako "
        "pomyłka.")
    assert "tura 3 (Ala): limit 5.00 s - 1.00 s, odpisał gracz Ola" in text
    assert text.endswith(HOW + STEADY)


async def test_the_loser_of_a_duel_gets_his_rounds_explained_the_winner_nothing(game, cog):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    recorded = Recorded(game, fightlog.KIND_DUEL, [ala, ola])
    recorded.answer(1, ala, "atak", 5.0, 1.0)
    recorded.answer(2, ola, "blok", 5.0, 2.0)
    recorded.answer(3, ala, "unik", 5.0, 1.1)
    recorded.timeout(4, ola, "atak", 5.0)
    recorded.answer(5, ala, "blok", 5.0, 0.9)
    recorded.answer(6, ola, "unik", 5.0, 3.0, typed="unki")
    recorded.answer(7, ala, "atak", 5.0, 1.2)
    fightlog.won(game.channel, winner=ala, loser=ola)
    assert await complain(game, ala, "lag") is None                    # the winner
    reply = await complain(game, ola, "lagi")
    lines = reply.content.splitlines()
    assert lines[0] == f"<@{ola.id}> Sprawdziłem zapis tej walki: **pojedynek**, przegrana z **Ala** (tur: 7)."
    assert lines[1] == (
        "**Dlaczego:** pojedynek rozstrzygają obrażenia z wielu tur - im szybsza poprawna odpowiedź, tym większe; "
        "tura bez odpowiedzi w czasie albo z błędem nie zadaje ich wcale. Twoje tury: 3, z tego poprawne: 1, bez "
        "odpowiedzi w czasie: 1, z błędem: 1. Ostatnia nieudana tura (6): słowo „unik”, limit **5.00 s** - "
        "odpowiedź przyszła w czasie (po **3.00 s**), ale brzmiała „unki”.")
    assert "tura 4 (Ola): limit 5.00 s - brak odpowiedzi\n" in reply.content
    assert reply.content.endswith(HOW + STEADY)


async def test_an_interrupted_fight_gets_one_short_honest_line(game, cog, pings):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    recorded = Recorded(game, fightlog.KIND_HUNT, [ala, ola], detail=0)
    for _ in range(3):
        recorded.disrupted(1, ala, "atak", 15.0)
    fightlog.interrupted(game.channel, "3 rounds in a row were disrupted")
    reply = await complain(game, ola, "lagi")
    assert reply.content == (
        f"<@{ola.id}> Ta walka (**polowanie w drużynie**) została przerwana przez problemy z połączeniem: kilka "
        "tur z rzędu było zakłóconych i nie dało się ich uczciwie ocenić. Nikt jej nie przegrał.")
    assert pings[-1][1].users == [ola] and game.bot.owner_log == []
    assert await complain(game, ala, "lag") is None


async def test_the_answer_fits_discords_limit_whatever_the_names(game):
    players = [game.add_member(("*_`" + letter * 29)[:32]) for letter in "ABC"]
    recorded = Recorded(game, fightlog.KIND_GROUP, players)
    for turn in range(1, 12):
        recorded.disrupted(turn, players[turn % 3], "zaklęcie", 5.0)
        recorded.answer(turn, players[turn % 3], "zaklęcie", 5.0, 1.0, latency=0.9)
    recorded.answer(12, players[0], "zaklęcie", 5.0, 4.99, by=players[1], typed="`" * 200)
    fight = fightlog.lost(game.channel)
    for viewer in (players[0].id, players[2].id, None):
        text = fightreport.explain(fight, viewer, subject_id=players[0].id)
        assert len(text) <= 2000 and "Dlaczego" in text and "Jak mierzę czas" in text
    # ... and when even that is too long, the list of rounds goes first.
    tiny = fightreport.explain(fight, players[0].id)
    assert tiny.count("```") in (0, 2)


async def test_a_real_lost_floor_then_lag_end_to_end(game, cog, skills, short_timeouts, monkeypatch, pings):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    hunter = game.add_member("Ala")
    game.auto_answer(hunter, silent_on_round=2)
    assert await tower.tower_fight(game.scene(hunter), 1) is False
    word = fightlog.last_fight(hunter.id).decisive.word
    game.say(hunter, word)                                             # typed, but far too late
    await game.settle()
    reply = await complain(game, hunter, "no i lag...")
    lines = reply.content.splitlines()
    assert lines[0] == (f"<@{hunter.id}> Sprawdziłem zapis tej walki: **Wieża Śmierci, piętro 1**, "
                        "przegrana w turze 2.")
    assert re.fullmatch(
        r"\*\*Dlaczego:\*\* Twoja odpowiedź przyszła po czasie\. Słowo „" + word + r"”, limit \*\*0\.01 s\*\* - "
        r"wiadomość \(„" + word + r"”\) została utworzona na Discordzie \*\*\d+\.\d\d s\*\* po moim pytaniu, czyli "
        r"\*\*\d+\.\d\d s po limicie\*\* \(zaliczam odpowiedzi utworzone do 0\.25 s po limicie\)\.", lines[1])
    assert "W tej turze mój ping wynosił **0 ms**" in reply.content
    assert pings[-1][1].users == [hunter]


async def test_a_failure_in_the_answer_never_breaks_message_handling(game, cog, monkeypatch, caplog):
    player = game.add_member("Ala")
    await lost_tower_floor(game, player)

    def broken(*args, **kwargs):
        raise RuntimeError("test: the report is broken")

    monkeypatch.setattr(fightreport, "explain", broken)
    with caplog.at_level(logging.ERROR, logger="altermmo.features.fightreport"):
        assert await complain(game, player) is None
    assert "Could not answer the lag complaint" in caplog.text

    monkeypatch.setattr(fightlog, "last_fight", broken)
    await lost_tower_floor(game, player)
    with caplog.at_level(logging.ERROR, logger="altermmo.features.fightreport"):
        assert fightreport.on_message(game.bot, game.say(player, "lag")) is False
    assert "Could not look at a possible lag complaint" in caplog.text
    # The round of a fight still hears its player.
    game.say_later(player, "blok", 0.02)
    assert (await challenge.ask(game.bot, game.channel, "Wpisz **B L O K**", players=[player],
                                timeout=1.0)).is_correct("blok")


# -- $walka ---------------------------------------------------------------------------------------

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


async def test_walka_shows_the_last_fight_of_a_member_to_administrators(game, cog):
    assert "altermmo.cogs.fightlog" in bot_module.EXTENSIONS
    command = game.bot.get_command("walka")
    assert any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)
    admin, player = administrator(game), game.add_member("Ala")
    await lost_tower_floor(game, player)
    sent: list[tuple[str, discord.AllowedMentions | None]] = []
    real_send = game.announce_channel.send

    async def send(content=None, **kwargs):
        sent.append((content, kwargs.get("allowed_mentions")))
        return await real_send(content, **kwargs)

    game.announce_channel.send = send
    await run(game, admin, f"$walka <@{player.id}>", channel=game.announce_channel)      # any channel
    text, mentions = sent[-1]
    assert text == (
        "Ostatnia zapisana walka gracza **Ala**: **Wieża Śmierci, piętro 5**, przegrana w turze 7 "
        "(przed chwilą).\n"
        "**Dlaczego:** odpowiedź gracza **Ala** przyszła po czasie. Słowo „unik”, limit **1.60 s** - wiadomość "
        "(„unik”) została utworzona na Discordzie **2.31 s** po moim pytaniu, czyli **0.71 s po limicie** "
        "(zaliczam odpowiedzi utworzone do 0.25 s po limicie).\n"
        "**Ostatnie tury:**\n"
        "```\n"
        "tura 5: limit 1.60 s - 1.21 s, dobrze\n"
        "tura 6: limit 1.60 s - 1.48 s, dobrze\n"
        "tura 7: limit 1.60 s - 2.31 s, po czasie\n"
        "```\n" + HOW + STEADY +
        " Nie widzę natomiast drogi między urządzeniem gracza a Discordem, w obie strony - jeśli tam coś się "
        "opóźniło, ten czas wliczył się do odpowiedzi gracza **Ala**.")
    assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()
    assert not fightlog.last_fight(player.id).answered                 # (it does not use up the player's answer)

    await run(game, admin, f"$walka {player.id}", channel=game.announce_channel)
    await run(game, admin, "$walka ala", channel=game.announce_channel)
    assert sent[-1][0] == sent[-2][0] == text
    await run(game, admin, "$walka", channel=game.announce_channel)
    assert sent[-1][0] == fightreport.MSG_USAGE
    await run(game, admin, "$walka Nikt", channel=game.announce_channel)
    assert sent[-1][0] == fightreport.MSG_NO_FIGHT

    count = len(sent)
    await run(game, player, f"$walka {player.id}", channel=game.announce_channel)   # not an administrator
    assert len(sent) == count


async def test_walka_shows_won_drawn_and_interrupted_fights_too(game, cog, skills, no_rewards):
    admin, ala, ola = administrator(game), game.add_member("Ala"), game.add_member("Ola")
    game.auto_answer(ala)
    assert await tower.tower_fight(game.scene(ala), 1) is True
    game.channel.send_hooks.clear()
    await run(game, admin, f"$walka {ala.id}")
    text = game.channel.sent[-1].content
    assert text.startswith("Ostatnia zapisana walka gracza **Ala**: **Wieża Śmierci, piętro 1**, wygrana "
                           "(przed chwilą).\n**Ostatnie tury:**\n```\ntura 1: limit 15.00 s - 0.")
    assert text.endswith("```\nPing bota w tej walce: 0-0 ms; zakłócone tury: 0.") and "Dlaczego" not in text

    recorded = Recorded(game, fightlog.KIND_DUEL, [ala, ola])
    recorded.answer(1, ala, "atak", 5.0, 1.0)
    fightlog.draw(game.channel)
    recorded.fight.ended = time.monotonic() - 185
    await run(game, admin, "$walka Ola")
    assert game.channel.sent[-1].content.startswith(
        "Ostatnia zapisana walka gracza **Ola**: **pojedynek**, remis (3 min temu).\n")

    recorded = Recorded(game, fightlog.KIND_DUEL, [ala, ola])
    recorded.answer(1, ala, "atak", 5.0, 1.0)
    recorded.timeout(2, ola, "blok", 5.0, in_grace=(5.5, "blok"))
    fightlog.won(game.channel, winner=ala, loser=ola)
    await run(game, admin, f"$walka {ala.id}")
    lines = game.channel.sent[-1].content.splitlines()
    assert lines[0] == ("Ostatnia zapisana walka gracza **Ala**: **pojedynek**, **Ala** wygrywa z **Ola** "
                        "(tur: 2) (przed chwilą).")
    assert lines[1].startswith("**Dlaczego:** pojedynek rozstrzygają obrażenia") and (
        "Tury gracza **Ola**: 1, z tego poprawne: 0, bez odpowiedzi w czasie: 1, z błędem: 0. Ostatnia nieudana "
        "tura (2): słowo „blok”, limit **5.00 s** - wiadomość („blok”) została utworzona na Discordzie "
        "**5.50 s** po moim pytaniu") in lines[1]

    Recorded(game, fightlog.KIND_HUNT, [ala], detail=0)
    fightlog.interrupted(game.channel)
    await run(game, admin, f"$walka {ala.id}")
    assert game.channel.sent[-1].content == ("Ostatnia zapisana walka gracza **Ala**: **polowanie**, przerwana "
                                             "przez problemy z połączeniem (przed chwilą).")
    Recorded(game, fightlog.KIND_BOSS, [ala], detail=0)                # still running (or it crashed)
    await run(game, admin, f"$walka {ala.id}")
    assert game.channel.sent[-1].content == ("Ostatnia zapisana walka gracza **Ala**: **walka z bossem**, bez "
                                             "zapisanego wyniku (trwa albo przerwał ją błąd) (przed chwilą).")


async def test_walka_is_not_posted_between_the_prompts_of_a_fight(game, cog):
    admin, player, other = administrator(game), game.add_member("Ala"), game.add_member("Ola")
    await lost_tower_floor(game, player)
    running = asyncio.ensure_future(challenge.ask(game.bot, game.channel, "1. Wpisz **A T A K**",
                                                  players=[other], timeout=5))
    await wait_until(lambda: challenge.fight_on(game.channel))
    asked = asyncio.ensure_future(run(game, admin, f"$walka {player.id}"))
    await asyncio.sleep(0.05)
    assert not game.channel.contains("Ostatnia zapisana walka")
    game.say(other, "atak")
    await running
    await asked
    assert game.channel.sent[-1].content.startswith("Ostatnia zapisana walka gracza **Ala**")
