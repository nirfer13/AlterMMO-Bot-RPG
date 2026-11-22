"""features/albumrank.py: the value of an album's cards and collections, the ranking of the supporters.

Owner's request of 2026-11-22: the album shows the collection's value and the place in the ranking;
a ranking of the collections; per species and stage only the rarest card counts; and "those who do
not support are not counted in the ranking, and they cannot look at it either".  Tested here: the
score table and its examples, odd rows, best-of-slot, who is ranked (supporters right now) and in
which order, the incomplete member cache, the overview / entry page of the album, ``$rankingalbumow``
and the hub's "Rankingi", and what a ranking of 300 players x 100 entries costs.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest

from altermmo.core import ui
from altermmo.features import album, albumrank, hub, patrons, petcards, pets
from altermmo.features.albumrank import card_points

from fakes import not_found
from test_album import album_rows, put, specimen
from test_album_ui import cogs, page, show  # noqa: F401 - the fixture
from test_pets import run
from test_pets import plain_cards  # noqa: F401 - autouse: pets are sent as their plain pictures

PATRON = patrons.PATRON_ROLES[0]
NBSP = chr(0xA0)


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a $potwierdzam slot was never released"


def row(species: str = "Cat", level=0, **columns) -> dict:
    """An album entry as a dict (the columns of ``PET_ALBUM``, lower case)."""
    return {"entry_id": columns.pop("entry_id", 1), "added_at": columns.pop("added_at", None),
            **specimen(species, level, **columns)}


# -- the score table ----------------------------------------------------------------------------

def test_the_score_table_states_its_numbers():
    """Written down on purpose: a change of the drop chances must not reshuffle the ranking silently."""
    assert albumrank.CARD_POINTS == 10 and albumrank.BONUS_SCALE == 10
    assert albumrank.CLASS_PERCENT == {"S": 8.0, "U": 1.8, "M": 0.3}
    assert albumrank.SEAL_PERCENT == {1: 10.0, 2: 4.8, 3: 0.2}
    assert albumrank.FOIL_PERCENT == {petcards.FOIL_PARTIAL: 1.0, petcards.FOIL_FULL: 0.1}
    assert {tier: albumrank.bonus(p) for tier, p in albumrank.CLASS_PERCENT.items()} == {"S": 36, "U": 58, "M": 84}
    assert {tier: albumrank.bonus(p) for tier, p in albumrank.SEAL_PERCENT.items()} == {1: 33, 2: 44, 3: 90}
    assert [albumrank.bonus(albumrank.FOIL_PERCENT[foil])
            for foil in (petcards.FOIL_PARTIAL, petcards.FOIL_FULL)] == [66, 100]
    assert [albumrank.bonus(p) for p in (8, 4, 2, 1, 5, 3, 0.55, 0.5, 0.3, 0.2, 0.15, 0.1)] == [
        36, 46, 56, 66, 43, 51, 75, 76, 84, 90, 94, 100]
    for nothing in (100, 150, 0, -3, None, "x"):
        assert albumrank.bonus(nothing) == 0


def test_the_frame_bonuses_follow_the_catalogue(monkeypatch):
    expected = {"S1": 36, "S1-A": 75, "R1": 46, "R1-A": 90, "R2": 56, "R2-A": 94, "R7": 66, "R7-A": 100,
                "U1": 36, "U1-A": 75, "R3": 46, "R3-A": 90, "R4": 56, "R4-A": 94, "R8": 66, "R8-A": 100,
                "R5": 43, "R5-A": 76, "R6": 51, "R6-A": 84, "R9": 66, "R9-A": 90,
                "S0": 0, "U0": 0, "M0": 0}
    flags = {"S": {"shiny": True}, "U": {"ultra_shiny": True}, "M": {"mythic": True}}
    found = {}
    for frame in petcards.FRAMES.values():
        found[frame.id] = albumrank.frame_points(row(frame=frame.id, **flags[frame.tier]))
        if not frame.base:
            assert found[frame.id] == round(10 * math.log2(100 / frame.percent)), frame.id
    assert found == expected
    # A frame added to the catalogue later scores by itself.
    new = petcards.Frame("R10", "S", "Nowa", "frames/R10.png", False, 0.05)
    monkeypatch.setitem(petcards.FRAMES, "R10", new)
    assert albumrank.frame_points(row(frame="R10", shiny=True)) == 110


def test_the_owners_examples():
    assert card_points(row()) == 10                                           # an ordinary card
    assert card_points(row(shiny=True)) == 46                                 # Shiny, base frame
    assert card_points(row(ultra_shiny=True)) == 68                           # Ultra Shiny, base frame
    assert card_points(row("Void", 0)) == 94                                  # mythic, nothing else
    assert card_points(row("Void", 2, frame="R9-A", seal=3, holo=True)) == 10 + 84 + 90 + 90 + 100 == 374
    assert card_points(row(shiny=True, frame="R7-A", seal=3, holo=True)) == 336
    assert card_points(row(shiny=True, seal=1, holo_partial=True)) == 10 + 36 + 33 + 66


def test_odd_rows_never_raise():
    assert card_points({"type": "Cat", "pet_lvl": 0}) == 10                    # no cosmetic column at all
    before_frames = row(shiny=True, seal=2)                                    # before FRAME / HOLO_PARTIAL
    assert "frame" not in before_frames and card_points(before_frames) == 10 + 36 + 44
    nulls = row(shiny=None, ultra_shiny=None, mythic=None, frame=None, seal=None, holo=None, holo_partial=None)
    assert card_points(nulls) == 10
    assert card_points(row(seal=Decimal("2"))) == 54 and card_points(row(seal=Decimal("3.0"))) == 100
    for no_seal in (Decimal("2.5"), Decimal("NaN"), 7, -1, 0, "x"):
        assert card_points(row(seal=no_seal)) == 10, no_seal
    assert card_points(row(shiny=True, frame="R99")) == 46                     # unknown id: the base
    assert card_points(row(shiny=True, frame="R9")) == 46                      # a mythic frame on a Shiny
    assert card_points(row(ultra_shiny=True, frame="S1")) == 68                # a Shiny frame on an Ultra
    assert card_points(row(holo=True, holo_partial=True)) == 110               # full Holo wins
    assert card_points(row(mythic=True, ultra_shiny=True, shiny=True)) == 94   # the class: mythic first
    assert card_points(row(ultra_shiny=True, shiny=True)) == 68
    assert card_points({**row(), "type": "Dinozaur"}) == 0                     # unknown species
    assert card_points({**row(), "type": None}) == 0
    assert card_points(row(level=7)) == 0 and card_points(row(level=-1)) == 0
    assert card_points(row(level=Decimal("1.5"))) == 0
    assert card_points(row(level=Decimal("3"))) == 10
    assert card_points(row(level=None)) == 10                                  # NULL is the egg, as everywhere
    assert card_points(None) == 0 and card_points({}) == 0


def test_the_slots_are_every_species_at_every_stage_the_void_included(monkeypatch):
    slots = albumrank.score_slots()
    assert len(slots) == len(pets.PetType) * 4 == 72
    assert ("Void", 0) in slots and ("Void", 3) in slots and ("Cat", 4) not in slots
    monkeypatch.setattr(pets, "MAX_LEVEL", 4)
    assert ("Cat", 4) in albumrank.score_slots() and card_points(row(level=4)) == 10


# -- one collection -----------------------------------------------------------------------------

def test_only_the_best_card_of_a_slot_counts():
    plain = row(entry_id=1)
    assert albumrank.collection([plain]).points == 10
    assert albumrank.collection([plain, row(entry_id=2), row(entry_id=3)]).points == 10   # duplicates: nothing
    shiny = row(entry_id=4, shiny=True)
    value = albumrank.collection([plain, row(entry_id=2), shiny])
    assert value.points == 46 and value.slots == 1                                         # +36, the difference
    assert value.counts(shiny) and not value.counts(plain)
    assert value.beaten(plain) and not value.beaten(shiny)
    others = albumrank.collection([plain, shiny, row("Cat", 1, entry_id=5), row("Void", 0, entry_id=6),
                                   row("Cat", 7, entry_id=7), {**row(entry_id=8), "type": "Dinozaur"}])
    assert others.points == 46 + 10 + 94 and others.slots == 3                             # the Void counts
    assert albumrank.collection([]).points == 0 and albumrank.collection([]).slots == 0


def test_of_two_equal_cards_the_first_one_counts_and_neither_is_beaten():
    old = datetime(2026, 11, 1, tzinfo=timezone.utc)
    first, second = row(entry_id=9, added_at=old), row(entry_id=3, added_at=old + timedelta(days=1))
    value = albumrank.collection([second, first])
    assert value.counts(first) and not value.counts(second) and value.newest == old
    assert not value.beaten(first) and not value.beaten(second)
    same_moment = albumrank.collection([row(entry_id=9, added_at=old), row(entry_id=3, added_at=old)])
    assert same_moment.best[("Cat", 0)] == (10, 3)                                         # then the smaller id


def test_numbers_and_words():
    assert [albumrank.points_text(n) for n in (0, 94, 999, 1234, 12345, 26928)] == [
        "0", "94", "999", f"1{NBSP}234", f"12{NBSP}345", f"26{NBSP}928"]
    assert [albumrank.cards_word(n) for n in (1, 2, 4, 5, 12, 13, 22, 25, 72)] == [
        "1 karta", "2 karty", "4 karty", "5 kart", "12 kart", "13 kart", "22 karty", "25 kart", "72 karty"]


# -- the ranking --------------------------------------------------------------------------------

async def at(db, entry_id: int, moment: datetime) -> None:
    await db.execute("UPDATE PET_ALBUM SET ADDED_AT = $1 WHERE ENTRY_ID = $2", moment, entry_id)


def table(board) -> list[tuple]:
    return [(item.place, item.player_id, item.points, item.cards) for item in board.standings]


SLOTS = [(species, level) for species in album.collection_species() for level in album.stages()]


async def cards(db, player_id: int, count: int, **columns) -> list[int]:
    """``count`` plain cards of different slots."""
    return [await put(db, player_id, species, level, **columns) for species, level in SLOTS[:count]]


async def test_the_ranking_order_and_every_tie_break(game_db):
    game = game_db
    db = game.bot.db
    day = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ala, ola, ela, iza, ewa = (game.add_member(name, roles=[PATRON], member_id=member_id)
                               for name, member_id in (("Ala", 105), ("Ola", 104), ("Ela", 103),
                                                       ("Iza", 102), ("Ewa", 101)))
    await put(db, ala.id, "Void", 0)                                  # 188, two slots: the most points
    await put(db, ala.id, "Void", 1)
    for member in (ola, iza, ewa):                                    # 100 in ten slots each ...
        for entry_id in await cards(db, member.id, 10):
            await at(db, entry_id, day)
    await at(db, (await album_rows(db, iza.id))[-1]["entry_id"], day + timedelta(days=1))  # Iza: a day later
    await at(db, await put(db, ela.id, "Cat", 0, seal=3), day - timedelta(days=30))       # 100 in ONE slot
    board = await albumrank.ranking(game.bot)
    assert [item.points for item in board.standings] == [188, 100, 100, 100, 100]
    # Points first; then more slots (Ela, earliest of all, is last); then the older newest counted
    # card (Iza after Ewa and Ola); then the player id (Ewa 101 before Ola 104).
    assert [item.player_id for item in board.standings] == [ala.id, ewa.id, ola.id, iza.id, ela.id]
    assert [item.place for item in board.standings] == [1, 2, 3, 4, 5] and board.total == 5
    assert board.of(ola.id).place == 3 and board.of(12345) is None


async def test_a_better_duplicate_and_removals(game_db):
    game = game_db
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])

    async def value() -> int:
        return (await albumrank.ranking(game.bot)).of(ala.id).points

    plain = await put(db, ala.id, "Cat", 0)
    await put(db, ala.id, "Dog", 1)
    assert await value() == 20
    twin = await put(db, ala.id, "Cat", 0)
    assert await value() == 20                                        # a duplicate adds nothing
    shiny = await put(db, ala.id, "Cat", 0, shiny=True)
    assert await value() == 20 + 36                                   # a better one: the difference only
    ultra = await put(db, ala.id, "Cat", 0, ultra_shiny=True)
    assert await value() == 10 + 68
    assert await album.remove(db, ala.id, twin)                       # a duplicate that did not count
    assert await value() == 10 + 68
    assert await album.remove(db, ala.id, ultra)                      # the best one: the next best counts
    assert await value() == 10 + 46
    assert await album.remove(db, ala.id, shiny) and await value() == 20
    assert await album.remove(db, ala.id, plain) and await value() == 10


async def test_only_supporters_right_now_are_ranked(game_db):
    game = game_db
    db = game.bot.db
    big = game.add_member("Duży", roles=[PATRON])
    ala = game.add_member("Ala", roles=[patrons.TWITCH_SUB_ROLE])
    ola = game.add_member("Ola", roles=[patrons.YOUTUBE_ROLES[1]])
    gone = game.add_member("Gone", roles=[PATRON])
    await cards(db, big.id, 30, shiny=True)
    await cards(db, gone.id, 40, shiny=True)
    await cards(db, ala.id, 3)
    await cards(db, ola.id, 2)
    nobody = game.add_member("Nikt", roles=[PATRON])                  # a supporter with an empty album
    game.guild.members.remove(gone)                                    # left the server
    big.roles.clear()                                                  # his support lapsed

    board = await albumrank.ranking(game.bot)
    assert table(board) == [(1, ala.id, 30, 3), (2, ola.id, 20, 2)]  # no lapsed one, no gone one, no empty
    assert board.of(nobody.id) is None and board.of(big.id) is None
    assert len(await album_rows(db, big.id)) == 30                     # his rows stay

    big.roles.append(game.guild.get_role(PATRON))                      # supports again: back with his cards
    board = await albumrank.ranking(game.bot)
    assert table(board) == [(1, big.id, 30 * 46, 30), (2, ala.id, 30, 3), (3, ola.id, 20, 2)]


async def test_an_album_without_the_later_columns_is_ranked(game_db):
    game = game_db
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await put(db, ala.id, "Cat", 0, shiny=True, seal=3)
    await db.execute("ALTER TABLE PET_ALBUM DROP COLUMN FRAME, DROP COLUMN HOLO_PARTIAL")
    assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 10 + 36 + 90, 1)]
    await db.execute("DROP TABLE PET_ALBUM")
    assert table(await albumrank.ranking(game.bot)) == []


@pytest.mark.parametrize("missing, kept", [("HOLO_PARTIAL", "frame"), ("FRAME", "holo_partial")])
async def test_an_album_lacking_one_later_column_still_reads_the_other(cogs, missing, kept):
    """Only the missing column is left out: the ranking and the album's overview read the same
    columns, so the value beside the place is the value the place was given for."""
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    ola = game.add_member("Ola", roles=[PATRON])
    await put(db, ala.id, "Cat", 0, shiny=True, frame="R7", holo_partial=True)   # 10 + 36 + 66 + 66
    await cards(db, ola.id, 12)                                                  # 120: between the two
    assert albumrank.collection(await album_rows(db, ala.id)).points == 178
    await db.execute(f"ALTER TABLE PET_ALBUM DROP COLUMN {missing}")
    rows = await album_rows(db, ala.id)
    assert kept in rows[0].keys() and missing.lower() not in rows[0].keys()
    own = albumrank.collection(rows).points
    assert own == 10 + 36 + 66                                                    # the other one still scores
    board = await albumrank.ranking(game.bot)
    assert table(board) == [(1, ola.id, 120, 12), (2, ala.id, own, 1)]
    text = page(await show(game, ala))
    assert f"Wartość kolekcji: **{own} pkt**\nMiejsce w rankingu: **2.** (z 2)" in text


def test_the_later_columns_are_tried_most_first():
    assert [album_column for album_column, _ in album.LATER_COLUMNS] == ["FRAME", "HOLO_PARTIAL"]
    assert albumrank._later_choices() == [("FRAME", "HOLO_PARTIAL"), ("FRAME",), ("HOLO_PARTIAL",), ()]


async def test_the_overview_shows_the_value_the_place_was_given_for(cogs, monkeypatch):
    """A card kept between reading the viewer's rows and making the ranking: one source for both."""
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await put(db, ala.id, "Cat", 0)
    rows = await album_rows(db, ala.id)
    await put(db, ala.id, "Dog", 1)                                              # (meanwhile)
    overview = await albumrank.overview_of(game.bot, ala, rows)
    assert (overview.points, overview.place, overview.total) == (20, 1, 1)


# -- an incomplete member cache -------------------------------------------------------------------

def hide(game, monkeypatch, hidden, *, answer=None):
    """The guild is not chunked and ``hidden`` are not in its cache; lookups go through ``answer``."""
    guild = game.guild
    guild.chunked = False
    shown = guild.get_member
    looked_up = []

    def get_member(member_id):
        return None if int(member_id) in {member.id for member in hidden} else shown(member_id)

    async def fetch_member(member_id):
        looked_up.append(int(member_id))
        if answer is not None:
            return await answer(int(member_id))
        member = next((member for member in hidden if member.id == int(member_id)), None)
        if member is None:
            raise not_found("Unknown Member")
        return member

    monkeypatch.setattr(guild, "get_member", get_member)
    monkeypatch.setattr(guild, "fetch_member", fetch_member)
    return looked_up


async def test_an_incomplete_cache_looks_up_only_the_missing_album_owners(game_db, monkeypatch):
    game = game_db
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    hidden = [game.add_member(f"Ukryty {n}", roles=[PATRON]) for n in range(6)]
    lapsed = game.add_member("Bez wsparcia")
    for n, member in enumerate([ala, *hidden, lapsed], start=1):
        await cards(db, member.id, n)
    for _ in range(20):
        game.add_member("Ktoś", roles=[PATRON])                       # never looked up: no album
    left = 777_000_000_000_000_001
    await cards(db, left, 9)                                           # left the server
    looked_up = hide(game, monkeypatch, [*hidden, lapsed])
    running, most = [0], [0]
    real = game.guild.fetch_member

    async def counted(member_id):
        running[0] += 1
        most[0] = max(most[0], running[0])
        await asyncio.sleep(0.01)
        try:
            return await real(member_id)
        finally:
            running[0] -= 1

    monkeypatch.setattr(game.guild, "fetch_member", counted)
    board = await albumrank.ranking(game.bot)
    assert sorted(looked_up) == sorted([*(member.id for member in hidden), lapsed.id, left])
    assert most[0] <= albumrank.LOOKUP_CONCURRENCY
    assert [item.player_id for item in board.standings] == [member.id for member in reversed(hidden)] + [ala.id]


async def test_an_incomplete_cache_that_cannot_be_completed_gives_no_ranking(cogs, monkeypatch):
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    ola = game.add_member("Ola", roles=[PATRON])
    await cards(db, ala.id, 2)
    await cards(db, ola.id, 3)

    async def broken(member_id):                                         # not "not a member": unknown
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                {"code": 50001, "message": "Missing Access"})

    hide(game, monkeypatch, [ola], answer=broken)
    assert await albumrank.ranking(game.bot) is None
    await run(game, ala, "$rankingalbumow")
    assert game.channel.messages[-1].content == albumrank.MSG_UNAVAILABLE
    text = page(await show(game, ala))                                  # the album: its value, no place
    assert f"Wartość kolekcji: **20 pkt**\n{albumrank.MSG_PLACE_UNKNOWN}" in text and "(z " not in text

    monkeypatch.setattr(albumrank, "LOOKUP_LIMIT", 0)                    # too many to look up: the same
    hide(game, monkeypatch, [ola])
    assert await albumrank.ranking(game.bot) is None
    game.guild.chunked = True                                            # complete: Ola is not there
    assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 20, 2)]


async def test_a_member_who_is_not_on_the_server_is_skipped_when_looked_up(game_db, monkeypatch):
    game = game_db
    ala = game.add_member("Ala", roles=[PATRON])
    await cards(game.bot.db, ala.id, 1)
    await cards(game.bot.db, 5, 4)
    hide(game, monkeypatch, [])                                          # id 5: "Unknown Member"
    assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 10, 1)]


class Clock:
    """``albumrank._clock`` under the test's control."""

    def __init__(self, monkeypatch) -> None:
        self.now = 1000.0
        monkeypatch.setattr(albumrank, "_clock", lambda: self.now)


async def test_a_not_a_member_answer_is_remembered_for_a_while(game_db, monkeypatch):
    """With an incomplete member list, owners who left the server are asked about once, not at every look."""
    game = game_db
    db = game.bot.db
    clock = Clock(monkeypatch)
    ala = game.add_member("Ala", roles=[PATRON])
    await cards(db, ala.id, 1)
    gone = [777_000_000_000_000_001 + n for n in range(5)]
    for player_id in gone:
        await cards(db, player_id, 2)
    looked_up = hide(game, monkeypatch, [])                              # every lookup: "Unknown Member"
    assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 10, 1)]
    assert sorted(looked_up) == gone
    for _ in range(3):                                                   # the album, the ranking, again ...
        assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 10, 1)]
    assert len(looked_up) == 5                                           # ... cost nothing
    clock.now += albumrank.NOT_MEMBER_TTL - 1
    await albumrank.ranking(game.bot)
    assert len(looked_up) == 5
    clock.now += 2                                                       # expired: asked again, once
    await albumrank.ranking(game.bot)
    await albumrank.ranking(game.bot)
    assert len(looked_up) == 10 and sorted(looked_up[5:]) == gone


async def test_a_remembered_leaver_who_joins_and_supports_is_ranked(game_db, monkeypatch):
    game = game_db
    db = game.bot.db
    Clock(monkeypatch)
    ala = game.add_member("Ala", roles=[PATRON])
    await cards(db, ala.id, 1)
    returning = 777_000_000_000_000_007
    await cards(db, returning, 3)
    looked_up = hide(game, monkeypatch, [])
    assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 10, 1)] and looked_up == [returning]
    back = game.add_member("Wraca", roles=[PATRON], member_id=returning)  # joined: in the member cache
    assert table(await albumrank.ranking(game.bot)) == [(1, back.id, 30, 3), (2, ala.id, 10, 1)]
    assert looked_up == [returning]                                       # (found without asking)


async def test_a_remembered_leaver_counts_again_once_the_guild_is_chunked(game_db, monkeypatch):
    """The memory is only for an incomplete member list: a complete one is the truth by itself."""
    game = game_db
    db = game.bot.db
    Clock(monkeypatch)
    ala = game.add_member("Ala", roles=[PATRON])
    await cards(db, ala.id, 1)
    hidden = game.add_member("Ukryty", roles=[PATRON])
    await cards(db, hidden.id, 2)
    real_get_member = game.guild.get_member

    async def not_yet(member_id):                                         # (Discord did not know him yet)
        raise not_found("Unknown Member")

    looked_up = hide(game, monkeypatch, [hidden], answer=not_yet)
    assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 10, 1)] and looked_up == [hidden.id]
    assert table(await albumrank.ranking(game.bot)) == [(1, ala.id, 10, 1)] and looked_up == [hidden.id]
    monkeypatch.setattr(game.guild, "get_member", real_get_member)        # the member list arrives complete
    game.guild.chunked = True
    assert table(await albumrank.ranking(game.bot)) == [(1, hidden.id, 20, 2), (2, ala.id, 10, 1)]
    assert looked_up == [hidden.id]


async def test_the_memory_of_leavers_is_bounded(game_db, monkeypatch):
    game = game_db
    db = game.bot.db
    Clock(monkeypatch)
    monkeypatch.setattr(albumrank, "NOT_MEMBER_LIMIT", 3)
    gone = [777_000_000_000_000_101 + n for n in range(5)]
    for player_id in gone:
        await cards(db, player_id, 1)
    looked_up = hide(game, monkeypatch, [])
    await albumrank.ranking(game.bot)
    memory = game.bot._album_not_members
    assert len(memory) == 3 and len(looked_up) == 5
    await albumrank.ranking(game.bot)                                     # the two forgotten ones are asked again
    assert len(looked_up) == 7 and len(memory) == 3


# -- the album's overview and entry page ------------------------------------------------------------

async def test_the_overview_shows_the_value_and_the_place(cogs):
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await put(db, ala.id, "Cat", 0, shiny=True)
    text = page(await show(game, ala))                                   # a single player
    assert text.startswith(f"<@{ala.id}>, w Twoim albumie jest **1** towarzysz.\nKolekcja: **1 z 68**\n"
                           "Wartość kolekcji: **46 pkt**\nMiejsce w rankingu: **1.** (z 1)\n\n")
    day = datetime(2026, 11, 1, tzinfo=timezone.utc)
    await at(db, (await album_rows(db, ala.id))[0]["entry_id"], day + timedelta(days=1))
    ola = game.add_member("Ola", roles=[patrons.CRAFTER_ROLE])
    await at(db, await put(db, ola.id, "Dog", 2, shiny=True), day)       # as much, but earlier: she leads
    assert "Miejsce w rankingu: **2.** (z 2)" in page(await show(game, ala))
    assert "Miejsce w rankingu: **1.** (z 2)" in page(await show(game, ola))
    # Several players, no ornament named, no species he has not found.
    text = page(await show(game, ala))
    for secret in ("Shiny", "Holo", "Pieczęć", "Ramka", "Pies"):
        assert secret not in text, secret


async def test_an_album_that_scores_nothing_has_no_value_line(cogs):
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await put(game.bot.db, ala.id, "Cat", 7)                              # a stage the game does not have
    text = page(await show(game, ala))
    assert "Wartość kolekcji" not in text and "Miejsce w rankingu" not in text


async def test_a_full_album_with_five_digit_value_fits(cogs):
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    for species, level in SLOTS:
        await put(db, ala.id, species, level, shiny=True, frame="R7-A", seal=3, holo=True)
    await put(db, ala.id, "Void", 3, frame="R9-A", seal=3, holo=True)
    text = page(await show(game, ala))
    assert 68 * 336 + 374 == 23222
    assert f"Wartość kolekcji: **23{NBSP}222 pkt**\nMiejsce w rankingu: **1.** (z 1)" in text
    assert len(text) <= 4096


async def test_the_entry_page_shows_the_cards_value_and_what_does_not_count(cogs):
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await put(db, ala.id, "Cat", 0)                                       # beaten by the shiny one
    await put(db, ala.id, "Cat", 0, shiny=True)
    await put(db, ala.id, "Dog", 1)                                       # two equal ones: nothing said
    await put(db, ala.id, "Dog", 1)
    panel = await show(game, ala)
    assert "Wartość kolekcji: **56 pkt**" in page(panel)
    await game.click(panel, ala, "Następny")
    assert "Wartość karty: **10 pkt**" in page(panel) and albumrank.MSG_NOT_COUNTED in page(panel)
    assert page(panel).endswith("Wpis **1 z 4**")
    await game.click(panel, ala, "Następny")
    assert "Wartość karty: **46 pkt**" in page(panel) and albumrank.MSG_NOT_COUNTED not in page(panel)
    for _ in range(2):
        await game.click(panel, ala, "Następny")
        assert "Wartość karty: **10 pkt**" in page(panel) and albumrank.MSG_NOT_COUNTED not in page(panel)
    for text in (albumrank.MSG_NOT_COUNTED, albumrank.MSG_HOW):           # no ornament is named
        for secret in ("Shiny", "shiny", "Holo", "holo", "ieczę", "amk", "Tropiciel", "Łowc"):
            assert secret not in text, secret


async def test_the_overview_after_a_removal_shows_the_new_value_and_place(cogs):
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    ola = game.add_member("Ola", roles=[PATRON])
    await put(db, ala.id, "Cat", 0, ultra_shiny=True)                     # 68: first
    await put(db, ala.id, "Cat", 1)
    await cards(db, ola.id, 6)                                            # 60
    panel = await show(game, ala)
    assert "Wartość kolekcji: **78 pkt**\nMiejsce w rankingu: **1.** (z 2)" in page(panel)
    await game.click(panel, ala, "Następny")
    game.auto_click(ala, "yes")
    await game.click(panel, ala, "Usuń z albumu")                         # the ultra shiny egg
    await game.click(panel, ala, "Przegląd")
    assert "Wartość kolekcji: **10 pkt**\nMiejsce w rankingu: **2.** (z 2)" in page(panel)


# -- $rankingalbumow and the hub -----------------------------------------------------------------

def recorded(game, monkeypatch, channel=None):
    channel = channel or game.channel
    sent = []
    real = channel.send

    async def send(content=None, **kwargs):
        message = await real(content, **kwargs)
        sent.append((message, kwargs.get("allowed_mentions")))
        return message

    monkeypatch.setattr(channel, "send", send)
    return sent


async def test_the_ranking_top_ten_and_the_viewers_own_line(cogs, monkeypatch):
    game = cogs
    db = game.bot.db
    players = [game.add_member(f"Gracz {n}" if n else "@everyone *Ala*", roles=[PATRON]) for n in range(12)]
    for n, member in enumerate(players):
        await cards(db, member.id, 12 - n)                                  # 120, 110 ... 10
    viewer = players[-1]
    sent = recorded(game, monkeypatch)
    await run(game, viewer, "$rankingalbumow")
    ((message, mentions),) = sent
    (embed,) = message.embeds
    assert embed.title == albumrank.MSG_TITLE and embed.footer.text == albumrank.MSG_HOW
    lines = embed.description.split("\n")
    assert lines[0] == "🥇 **@everyone \\*Ala\\*** · **120 pkt** · 12 kart"
    assert lines[1] == "🥈 **Gracz 1** · **110 pkt** · 11 kart"
    assert lines[3] == "**4.** **Gracz 3** · **90 pkt** · 9 kart"
    assert lines[9] == "**10.** **Gracz 9** · **30 pkt** · 3 karty"
    assert lines[10:] == ["", "Twoje miejsce: **12.** z 12 · **10 pkt** · 1 karta"]
    assert mentions is not None and not mentions.everyone and not mentions.users and not mentions.roles
    assert "<@" not in message.text
    sent.clear()
    await run(game, players[3], "$ra")                                      # in the top ten: no own line
    ((message, _),) = sent
    assert len(message.embeds[0].description.split("\n")) == 10


async def test_the_own_line_starts_exactly_below_the_top_ten(cogs, monkeypatch):
    """Place 10 is on the list (no own line); place 11 is not (own line)."""
    game = cogs
    db = game.bot.db
    players = [game.add_member(f"Gracz {n + 1}", roles=[PATRON]) for n in range(11)]
    for n, member in enumerate(players):
        await cards(db, member.id, 11 - n)                                  # places 1 .. 11
    assert albumrank.TOP == 10
    sent = recorded(game, monkeypatch)
    await run(game, players[9], "$rankingalbumow")                          # place 10
    ((message, _),) = sent
    lines = message.embeds[0].description.split("\n")
    assert len(lines) == 10 and lines[9] == "**10.** **Gracz 10** · **20 pkt** · 2 karty"
    assert all("Twoje miejsce" not in line for line in lines)
    sent.clear()
    await run(game, players[10], "$rankingalbumow")                         # place 11
    ((message, _),) = sent
    lines = message.embeds[0].description.split("\n")
    assert len(lines) == 12 and lines[9] == "**10.** **Gracz 10** · **20 pkt** · 2 karty"
    assert lines[10:] == ["", "Twoje miejsce: **11.** z 11 · **10 pkt** · 1 karta"]
    assert "Gracz 11" not in message.embeds[0].description


async def test_the_ranking_when_nobody_or_not_yet_the_viewer_is_in_it(cogs):
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await run(game, ala, "$rankingkolekcji")
    assert game.channel.messages[-1].embeds[0].description == albumrank.MSG_NOBODY
    ola = game.add_member("Ola", roles=[PATRON])
    await cards(game.bot.db, ola.id, 1)
    await run(game, ala, "$albumy")
    assert game.channel.messages[-1].embeds[0].description == (
        f"🥇 **Ola** · **10 pkt** · 1 karta\n\n{albumrank.MSG_OWN_NONE}")


async def test_the_ranking_is_for_supporters_only(cogs):
    game = cogs
    ola = game.add_member("Ola")
    ala = game.add_member("Ala", roles=[PATRON])
    await cards(game.bot.db, ala.id, 3)
    await cards(game.bot.db, ola.id, 9)                                     # kept while she supported
    for command in ("$rankingalbumow", "$rankingalbumów", "$rankingkolekcji", "$albumy", "$ra"):
        await run(game, ola, command)
        answer = game.channel.messages[-1]
        assert answer.content == albumrank.supporters_only_text(ola) and answer.embeds == [], command
        assert answer.content.startswith(f"<@{ola.id}> Ranking kolekcji jest dla wspierających (")
        assert "Ala" not in answer.text and "pkt" not in answer.text


async def test_the_command_its_aliases_and_the_busy_policy(cogs):
    game = cogs
    command = game.bot.get_command("ra")
    assert command.name == "rankingalbumow" and command.cog_name == "Album"
    assert sorted(command.aliases) == ["albumy", "ra", "rankingalbumów", "rankingkolekcji"]
    ala = game.add_member("Ala", roles=[PATRON])
    with game.bot.state.hold_busy("hunt"):
        await run(game, ala, "$rankingalbumow", channel=game.announce_channel)
        assert all(albumrank.MSG_TITLE not in m.text for m in game.announce_channel.messages)
    await run(game, ala, "$rankingalbumow", channel=game.announce_channel)   # any channel of the server
    assert game.announce_channel.messages[-1].embeds[0].title == albumrank.MSG_TITLE


async def test_the_hubs_rankings_button(cogs, monkeypatch):
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    ola = game.add_member("Ola")
    await cards(db, ala.id, 2)
    await cards(db, ola.id, 9)
    hub_panel, _ = await hub.post_panel(game.bot, game.channel)
    open_titles = ["Ranking łowców potworów!", "Eksploratorzy Wieży Śmierci!", "Najbardziej utalentowani towarzysze!"]

    sent = recorded(game, monkeypatch)
    click = await game.click(hub_panel, ala, "Rankingi")                   # a supporter: four rankings
    assert click.response.deferred and click.replies == []
    assert [m.embeds[0].title for m, _ in sent] == [*open_titles, albumrank.MSG_TITLE]
    ranking, mentions = sent[-1]
    assert ranking.embeds[0].description == "🥇 **Ala** · **20 pkt** · 2 karty"     # Ola does not support
    assert mentions is not None and not mentions.everyone and not mentions.users

    for _ in range(2):                                                     # the others: the three open ones
        sent.clear()                                                       # and NOTHING about the collections
        await game.click(hub_panel, ola, "Rankingi")
        assert [m.embeds[0].title for m, _ in sent] == open_titles
        assert all(f"<@{ola.id}>" not in m.text and "Ranking kolekcji" not in m.text for m, _ in sent)
    # The typed command still tells her that it is for supporters.
    await run(game, ola, "$rankingalbumow")
    assert game.channel.messages[-1].content == albumrank.supporters_only_text(ola)


# -- the cost --------------------------------------------------------------------------------------

async def test_the_ranking_is_cheap(game_db, capsys):
    """300 supporters x 100 entries: one query and the score in Python, measured (no cache needed)."""
    game = game_db
    db = game.bot.db
    players = [game.add_member(f"P{n}", roles=[PATRON], member_id=900_000 + n) for n in range(300)]
    species = [kind.value for kind in pets.PetType]
    await db.execute(
        """INSERT INTO PET_ALBUM (PLAYER_ID, TYPE, PET_LVL, SHINY, ULTRA_SHINY, MYTHIC, SEAL, HOLO,
                                  HOLO_PARTIAL, FRAME, PET_NAME)
           SELECT p, ($1::text[])[1 + (g * 7 + p) % 18], (g + p) % 5, g % 7 = 0, g % 13 = 0, g % 97 = 0,
                  (g + p) % 9, g % 101 = 0, g % 31 = 0, CASE WHEN g % 11 = 0 THEN 'R7-A' ELSE '' END, 'x'
           FROM generate_series(900000, 900299) AS p, generate_series(1, 100) AS g""", species)
    assert await db.fetchval("SELECT COUNT(*) FROM PET_ALBUM") == 30_000
    await albumrank.ranking(game.bot)                                       # (warm: connection, statement)
    times, stalls = [], [0.0]
    done = asyncio.Event()

    async def ticker():                                                     # how long the loop is held
        loop = asyncio.get_running_loop()
        while not done.is_set():
            before = loop.time()
            await asyncio.sleep(0.001)
            stalls[0] = max(stalls[0], loop.time() - before)

    ticking = asyncio.ensure_future(ticker())
    try:
        for _ in range(3):
            started = time.perf_counter()
            board = await albumrank.ranking(game.bot, viewer=players[0])
            times.append(time.perf_counter() - started)
    finally:
        done.set()
        await ticking
    assert board.total == 300 and [item.place for item in board.standings] == list(range(1, 301))
    started = time.perf_counter()
    albumrank.collection(await album.entries(db, players[0].id))
    own = time.perf_counter() - started
    with capsys.disabled():
        print(f"\n[album ranking: 300 x 100 entries: {min(times) * 1000:.0f} ms best, "
              f"{max(times) * 1000:.0f} ms worst of 3; longest hold of the event loop "
              f"{stalls[0] * 1000:.0f} ms; one album of 100: {own * 1000:.1f} ms]")
    assert min(times) < 2.0 and stalls[0] < 0.5
