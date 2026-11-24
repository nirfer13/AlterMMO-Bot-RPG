"""The album note at an egg drop (``album.note_new_egg``, called by ``pets.assign_pet``).

Owner's request of 2026-11-23: "Dla wspierających dodawaj także informację gdy wypadnie komuś jajko, czy ma
je już w kolekcji, albo czy następne fazy ewolucji mogą uzupełnić jego kolekcję. Jeśli ma coś to podaj czy to
holo z ramką etc."  Tested here: the text for every combination of held and missing stages, the counted card
of a slot described by what it has (each ornament and none), the mark where the new egg's card would be worth
more, the mythic species, that the species is never named and that the new egg's own ornaments are not told;
and on the way of a real egg: supporters only, one note per granted egg (also into the pouch), none without an
egg, nothing but the note lost when the album cannot be read or Discord refuses, and the random draws of the
drop untouched (the egg alone, a whole boss drop, a monster drop).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import discord
import pytest

from altermmo.core import challenge, io
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import album, albumrank, boss, daily, patrons, petcards, pets
from altermmo.features.pets import PetType

from test_album import put, specimen
from test_album_ui import names_a_species
from test_pets import add_owner, add_pet, owner_row, pet_row
from test_pets import plain_cards  # noqa: F401 - autouse: no card is composed here

PATRON = patrons.PATRON_ROLES[0]
HEAD = "📖 <@{player_id}>, Twój album a to jajko:"
STAGES = ["Jajko", "Poziom 1", "Poziom 2", "Poziom 3"]
BETTER = "⬆️ = karta z tego jajka byłaby na tym etapie cenniejsza od tej w albumie, więc podniosłaby wartość kolekcji."
EMPTY_NOTE = (HEAD + "\n▫️ Jajko - brak\n▫️ Poziom 1 - brak\n▫️ Poziom 2 - brak\n▫️ Poziom 3 - brak\n"
              "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: Jajko, Poziom 1, Poziom 2, Poziom 3.")


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a !potwierdzam slot was never released"


# -- helpers ------------------------------------------------------------------------------------

def new_egg(species: str = "Cat", **cosmetics) -> dict:
    """A rolled egg as ``pets._roll_egg`` makes it for a supporter (upper-case keys)."""
    kind = PetType(species)
    mythic = kind in pets.MYTHIC_EGGS
    quality = "Mythic" if mythic else "Premium" if kind in pets.PREMIUM_TYPES else "Standard"
    egg = {"TYPE": species, "QUALITY": quality, "VARIANT": 0, "SHINY": mythic, "ULTRA_SHINY": mythic,
           "MYTHIC": mythic, "SEAL": 0, "HOLO": False, "FRAME": "", "HOLO_PARTIAL": False}
    egg.update({key.upper(): value for key, value in cosmetics.items()})
    return egg


def entry(species: str = "Cat", level: int = 0, entry_id: int = 1, added_at=None, **columns) -> dict:
    """An album entry as a dict (the columns of ``PET_ALBUM``, lower case)."""
    return {"entry_id": entry_id, "added_at": added_at, **specimen(species, level, **columns)}


def note(egg: dict, rows: list, player_id: int = 5) -> list[str]:
    """The note's lines without its head."""
    text = album.egg_note(player_id, egg, rows)
    head, *lines = text.split("\n")
    assert head == HEAD.format(player_id=player_id)
    return lines


def force(monkeypatch, egg: dict) -> None:
    """Every new egg is ``egg`` (instead of a random one)."""
    monkeypatch.setattr(pets, "_roll_egg", lambda player, **kwargs: dict(egg))


def recorded(game, monkeypatch) -> list[tuple]:
    """``(message, allowed_mentions)`` of everything the bot posts on the game's channel from now on."""
    sent = []
    real = game.channel.send

    async def send(content=None, **kwargs):
        message = await real(content, **kwargs)
        sent.append((message, kwargs.get("allowed_mentions")))
        return message

    monkeypatch.setattr(game.channel, "send", send)
    return sent


def notes(sent) -> list:
    return [message for message, _ in sent if (message.content or "").startswith("📖 ")]


# -- the text -------------------------------------------------------------------------------------

def test_the_texts_of_the_note():
    assert album.MSG_EGG_NOTE == HEAD and album.MSG_EGG_NOTE_BETTER == BETTER
    assert album.MSG_EGG_NOTE_PLAIN == "zwykła karta bez ozdób"
    assert (album.COLLECTED_MARK, album.MISSING_MARK, album.EGG_NOTE_BETTER_MARK) == ("✅", "▫️", "⬆️")
    for text in (album.MSG_EGG_NOTE, album.MSG_EGG_NOTE_HELD, album.MSG_EGG_NOTE_MISSING, album.MSG_EGG_NOTE_BETTER,
                 album.MSG_EGG_NOTE_CAN, album.MSG_EGG_NOTE_ALL, album.MSG_EGG_NOTE_MYTHIC_CAN,
                 album.MSG_EGG_NOTE_MYTHIC_ALL):
        assert "lobby" not in text.lower() and "%" not in text and "$" not in text, text


def test_an_empty_album():
    assert album.egg_note(5, new_egg(), []) == EMPTY_NOTE.format(player_id=5)
    # Entries of other species say nothing about this egg.
    others = [entry("Dog", 0, 1), entry("Bear", 2, 2, shiny=True), entry("Void", 1, 3)]
    assert album.egg_note(5, new_egg(), others) == EMPTY_NOTE.format(player_id=5)


@pytest.mark.parametrize("held", [combo for size in range(5) for combo in itertools.combinations(range(4), size)])
def test_every_combination_of_held_and_missing_stages(held):
    rows = [entry("Cat", level, entry_id=level + 1) for level in held]
    lines = note(new_egg(), rows)
    assert len(lines) == 5
    for level, line in enumerate(lines[:4]):
        if level in held:
            assert line == f"✅ {STAGES[level]} - masz: zwykła karta bez ozdób"
        else:
            assert line == f"▫️ {STAGES[level]} - brak"
    missing = [STAGES[level] for level in range(4) if level not in held]
    if missing:
        assert lines[4] == f"Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: {', '.join(missing)}."
    else:
        assert lines[4] == "Album ma już każdy etap tego towarzysza."


def test_the_stages_follow_the_games_constants(monkeypatch):
    monkeypatch.setattr(pets, "MAX_LEVEL", 4)
    lines = note(new_egg(), [entry("Cat", 4)])
    assert lines[4] == "✅ Poziom 4 - masz: zwykła karta bez ozdób" and len(lines) == 6
    assert lines[5] == "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: Jajko, Poziom 1, Poziom 2, Poziom 3."


@pytest.mark.parametrize("columns, words", [
    ({}, "zwykła karta bez ozdób"),
    ({"shiny": True}, "Shiny"),                                              # the base frame is no ornament
    ({"ultra_shiny": True, "shiny": True}, "Ultra Shiny"),
    ({"shiny": True, "frame": "S1"}, "Shiny, ramka „Gwiazda Szronu”"),
    ({"shiny": True, "frame": "R7-A"}, "Shiny, ramka „Przeszywana skóra (animowana)”"),
    ({"ultra_shiny": True, "frame": "U1"}, "Ultra Shiny, ramka „Rubinowa Korona”"),
    ({"shiny": True, "frame": "U1"}, "Shiny"),                               # a frame of another class: the base
    ({"frame": "S1"}, "zwykła karta bez ozdób"),                             # an ordinary pet has no frame
    ({"seal": 1}, "pieczęć Tropiciela"),
    ({"seal": 2}, "pieczęć Łowcy"),
    ({"seal": 3}, "pieczęć Arcyłowcy"),
    ({"holo": True}, "Holo"),
    ({"holo_partial": True}, "Holo częściowe"),
    ({"holo": True, "holo_partial": True}, "Holo"),                          # full Holo wins
    ({"shiny": True, "frame": "S1-A", "seal": 2, "holo": True},
     "Shiny, ramka „Gwiazda Szronu (animowana)”, pieczęć Łowcy, Holo"),
    ({"seal": 3, "holo_partial": True}, "pieczęć Arcyłowcy, Holo częściowe"),
    ({"seal": 7}, "zwykła karta bez ozdób"),                                 # a seal nobody knows
])
def test_a_held_card_is_described_by_what_it_has(columns, words):
    assert album.card_words(entry("Cat", 2, **columns)) == words
    lines = note(new_egg(), [entry("Cat", 2, **columns)])
    assert lines[2].startswith(f"✅ Poziom 2 - masz: {words}")


def test_a_mythic_card_and_rows_the_game_cannot_read():
    assert album.card_words(entry("Void", 1)) == "Mityczny"
    assert album.card_words(entry("Void", 1, frame="R9-A", seal=3, holo=True)) == (
        "Mityczny, ramka „Oko Otchłani (animowana)”, pieczęć Arcyłowcy, Holo")
    old = {"type": "Cat", "pet_lvl": 1}                                      # before the cosmetic columns
    assert album.card_words(old) == "zwykła karta bez ozdób"
    nulls = entry(shiny=None, ultra_shiny=None, mythic=None, frame=None, seal=None, holo=None, holo_partial=None)
    assert album.card_words(nulls) == "zwykła karta bez ozdób"
    assert album.card_words(None) == album.card_words({}) == "zwykła karta bez ozdób"
    # The words are the ones the pet's own statistics and card use.
    assert pets.FOIL_WORDS == {petcards.FOIL_FULL: "Holo", petcards.FOIL_PARTIAL: "Holo częściowe"}
    assert album.CLASS_NAMES["S"] == "Shiny" and petcards.SEAL_NAMES[2] == "Łowcy"


def test_of_several_entries_of_a_slot_the_counted_one_is_described():
    """Only the most valuable card of a slot counts - that one is named, not the first or the last."""
    rows = [entry("Cat", 2, 1), entry("Cat", 2, 2, shiny=True, seal=2), entry("Cat", 2, 3, holo=True),
            entry("Cat", 2, 4, seal=1)]
    assert [albumrank.card_points(row) for row in rows] == [20, 180, 220, 86]
    assert note(new_egg(), rows)[2] == "✅ Poziom 2 - masz: Holo"
    assert note(new_egg(), list(reversed(rows)))[2] == "✅ Poziom 2 - masz: Holo"
    # Two cards of equal value: the one that came first counts (as in the ranking).
    day = datetime(2026, 11, 1, tzinfo=timezone.utc)
    framed = entry("Cat", 0, 7, added_at=day + timedelta(days=1), shiny=True, frame="R1-A")
    sealed = entry("Cat", 0, 9, added_at=day, shiny=True, seal=3)
    assert albumrank.card_points(framed) == albumrank.card_points(sealed) == 136
    assert note(new_egg(), [framed, sealed])[0] == "✅ Jajko - masz: Shiny, pieczęć Arcyłowcy"
    assert note(new_egg(), [sealed, framed])[0] == "✅ Jajko - masz: Shiny, pieczęć Arcyłowcy"


def test_a_stage_where_the_new_egg_would_be_worth_more_is_marked():
    """Only the rarest card of a slot counts, so a rarer card improves the collection as well."""
    rows = [entry("Cat", 0, 1),                                               # 10: the shiny egg beats it
            entry("Cat", 1, 2, shiny=True),                                   # 69: as much as the new one
            entry("Cat", 2, 3, holo=True)]                                    # 220: more than the new one (92)
    shiny = new_egg(shiny=True)
    assert note(shiny, rows) == [
        "✅ Jajko - masz: zwykła karta bez ozdób ⬆️",
        "✅ Poziom 1 - masz: Shiny",
        "✅ Poziom 2 - masz: Holo",
        "▫️ Poziom 3 - brak",
        "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: Poziom 3.",
        BETTER]
    # An ordinary egg improves nothing: no mark, no line about it.
    plain = note(new_egg(), rows)
    assert all("⬆️" not in line for line in plain) and len(plain) == 5
    # With every stage held the mark is the only way this egg still helps.
    full = [entry("Cat", level, level + 1) for level in range(4)]
    assert note(new_egg(seal=1, holo_partial=True), full) == [
        *(f"✅ {stage} - masz: zwykła karta bez ozdób ⬆️" for stage in STAGES),
        "Album ma już każdy etap tego towarzysza.", BETTER]
    # The comparison is the score's own: the egg's look at that stage against the counted card.
    assert albumrank.card_points({"type": "Cat", "pet_lvl": 2, "shiny": True}) == 92 < 220
    # An egg stored without the cosmetic columns (an old PETS table) compares as a card with nothing.
    bare = {key: value for key, value in new_egg().items() if key not in ("SEAL", "HOLO", "FRAME", "HOLO_PARTIAL")}
    assert note(bare, rows) == plain


def test_the_mark_compares_the_cards_at_that_stage_not_the_egg_as_an_egg():
    """The new egg's card is taken AT THE STAGE of the counted card (the stage multiplies the whole card):
    as an egg a Holo is worth 110, less than a Shiny of level 3 (138) - but at level 3 it is 330."""
    grown_shiny = entry("Cat", 3, 1, shiny=True)
    assert albumrank.card_points(grown_shiny) == 138
    holo = new_egg(holo=True)
    assert albumrank.card_points({"type": "Cat", "pet_lvl": 0, "holo": True}) == 110 < 138
    assert albumrank.card_points({"type": "Cat", "pet_lvl": 3, "holo": True}) == 330 > 138
    assert note(holo, [grown_shiny]) == [
        "▫️ Jajko - brak", "▫️ Poziom 1 - brak", "▫️ Poziom 2 - brak",
        "✅ Poziom 3 - masz: Shiny ⬆️",
        "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: Jajko, Poziom 1, Poziom 2.", BETTER]
    # Stage by stage: a Shiny egg (46 / 69 / 92 / 138) against a card with the first seal at every stage
    # (43 / 65 / 86 / 129) is worth more everywhere; against a card with the second seal (54 / 81 / 108 /
    # 162) nowhere.
    shiny = new_egg(shiny=True)
    first_seal = [entry("Cat", level, level + 1, seal=1) for level in range(4)]
    assert [albumrank.card_points(row) for row in first_seal] == [43, 65, 86, 129]
    assert note(shiny, first_seal)[:4] == [f"✅ {stage} - masz: pieczęć Tropiciela ⬆️" for stage in STAGES]
    second_seal = [entry("Cat", level, level + 1, seal=2) for level in range(4)]
    assert [albumrank.card_points(row) for row in second_seal] == [54, 81, 108, 162]
    assert note(shiny, second_seal) == [*(f"✅ {stage} - masz: pieczęć Łowcy" for stage in STAGES),
                                        "Album ma już każdy etap tego towarzysza."]
    # An egg with the first seal (43 as an egg) against an ordinary card of level 3 (30): 129 there.
    assert note(new_egg(seal=1), [entry("Cat", 3, 1)])[3] == "✅ Poziom 3 - masz: zwykła karta bez ozdób ⬆️"
    # ... and against a Shiny of level 3 (138): at level 3 only 129 - no mark.
    assert note(new_egg(seal=1), [grown_shiny])[3] == "✅ Poziom 3 - masz: Shiny"
    # The egg's value as an egg must not decide: a Shiny egg (46) against a card with the first seal at
    # level 1 (65) - at level 1 the Shiny is 69.
    assert note(shiny, [entry("Cat", 1, 1, seal=1)])[1] == "✅ Poziom 1 - masz: pieczęć Tropiciela ⬆️"


def test_the_note_never_tells_what_the_new_egg_has():
    """What a new egg has is seen on the egg itself (``!towarzysz``) and is announced nowhere (the owner
    alone hears about rare finds): the note names the ALBUM's cards only - of the new egg at most that
    its card would be worth more."""
    rich = new_egg(shiny=True, frame="R7-A", seal=3, holo=True)
    for rows in ([], [entry("Cat", 0, 1)], [entry("Cat", level, level + 1) for level in range(4)]):
        text = album.egg_note(5, rich, rows)
        for ornament in ("Shiny", "ramk", "Przeszywana", "ieczę", "Arcyłowc", "Holo"):
            assert ornament not in text, ornament
    assert note(rich, [entry("Cat", 0, 1)])[0] == "✅ Jajko - masz: zwykła karta bez ozdób ⬆️"
    # ... and the same egg against a card that has all of it: only the album's card is described.
    twin = entry("Cat", 0, 1, shiny=True, frame="R7-A", seal=3, holo=True)
    assert note(new_egg(), [twin])[0] == (
        "✅ Jajko - masz: Shiny, ramka „Przeszywana skóra (animowana)”, pieczęć Arcyłowcy, Holo")
    assert note(rich, [twin])[0] == note(new_egg(), [twin])[0]                 # as much: no mark


def test_a_mythic_egg():
    """The mythic species is no part of the collection ("Pełny album"), but its cards score."""
    egg = new_egg("Void", frame="R5", seal=1)
    assert note(egg, []) == [
        "▫️ Jajko - brak", "▫️ Poziom 1 - brak", "▫️ Poziom 2 - brak", "▫️ Poziom 3 - brak",
        "Mityczny towarzysz nie należy do kolekcji, ale jego karty liczą się do jej wartości - w albumie "
        "brakuje: Jajko, Poziom 1, Poziom 2, Poziom 3."]
    rows = [entry("Void", 0, 1), entry("Void", 3, 2, frame="R9-A", seal=3, holo=True)]
    assert note(egg, rows) == [
        "✅ Jajko - masz: Mityczny ⬆️",
        "▫️ Poziom 1 - brak", "▫️ Poziom 2 - brak",
        "✅ Poziom 3 - masz: Mityczny, ramka „Oko Otchłani (animowana)”, pieczęć Arcyłowcy, Holo",
        "Mityczny towarzysz nie należy do kolekcji, ale jego karty liczą się do jej wartości - w albumie "
        "brakuje: Poziom 1, Poziom 2.", BETTER]
    full = [entry("Void", level, level + 1) for level in range(4)]
    assert note(new_egg("Void"), full)[-1] == "Mityczny towarzysz nie należy do kolekcji, a album ma już każdy jego etap."


@pytest.mark.parametrize("species", [kind.value for kind in PetType])
def test_the_species_is_never_named(species):
    """An egg does not tell its species - neither by the note's words nor by the cards it lists."""
    rich = {"shiny": True, "frame": "S1-A", "seal": 3, "holo": True} if species != "Void" else {
        "frame": "R6-A", "seal": 3, "holo": True}
    rows = [entry(species, 0, 1, pet_name="Burek"), entry(species, 2, 2, **rich), entry(species, 2, 3)]
    for egg in (new_egg(species), new_egg(species, seal=2, holo_partial=True)):
        for album_rows in ([], rows, rows[:1]):
            text = album.egg_note(123, egg, album_rows)
            assert names_a_species(text) == [], text
            assert "Burek" not in text and text.count("<@") == 1 and "<@123>" in text
            assert len(text) < 700 and len(text.split("\n")) <= 7              # a handful of short lines


def test_the_longest_note_is_far_from_discords_limit():
    longest = max(petcards.FRAMES.values(), key=lambda frame: len(frame.label))
    flag = {"S": "shiny", "U": "ultra_shiny", "M": "mythic"}[longest.tier]
    species = "Void" if longest.tier == "M" else "Unicorn"
    rows = [entry(species, level, level + 1, frame=longest.id, seal=3, holo_partial=True, **{flag: True})
            for level in range(4)]
    egg = new_egg(species, frame=longest.id, seal=3, holo=True, **{flag: True})
    text = album.egg_note(123456789012345678, egg, rows)
    assert text.count("⬆️") == 5 and len(text) < 1000 < 2000


def test_no_note_for_an_egg_of_a_species_the_game_does_not_know():
    assert album.egg_note(5, {"TYPE": "Dinozaur"}, [entry()]) == ""
    assert album.egg_note(5, {}, []) == ""


# -- on the way of a real egg ---------------------------------------------------------------------

async def test_a_supporter_gets_the_note_and_nobody_else_gets_anything_new(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))
    sent = recorded(game, monkeypatch)
    ola = game.add_member("Ola")                                              # does not support
    assert await pets.assign_pet(game.scene(), ola) is True
    assert sent == []                                                         # as it always was: nothing
    assert await pets.assign_pet(game.scene(), 777_000_000_000_000_001) is True   # a bare id: no roles
    assert sent == []

    for role in (PATRON, patrons.CRAFTER_ROLE, patrons.TWITCH_SUB_ROLE, patrons.YOUTUBE_ROLES[0]):
        sent.clear()
        ala = game.add_member("Ala", roles=[role])
        assert await pets.assign_pet(game.scene(), ala) is True
        ((message, mentions),) = sent                                         # ONE message
        assert message.content == EMPTY_NOTE.format(player_id=ala.id)
        assert message.embeds == [] and message.view is None and message.files == []   # an ordinary public one
        assert message in game.channel.messages
        assert mentions is not None and not mentions.everyone and not mentions.users and not mentions.roles
        assert (await pet_row(db, (await owner_row(db, ala.id))["pet_id"]))["type"] == "Cat"


async def test_the_note_follows_the_player_who_got_the_egg_not_the_scenes_author(game_db, monkeypatch):
    """A party hunt, a group boss, the chest, a duel, the training's winner: the egg goes to a hunter who is
    usually NOT the scene's author.  Whether there is a note, whom it addresses and whose album it reads all
    follow the player who got the egg."""
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))
    starter = game.add_member("Starter", roles=[PATRON])                      # supports and began the scene ...
    hunter = game.add_member("Hunter")                                        # ... the egg goes to one who does not
    await put(db, starter.id, "Cat", 0, holo=True)
    sent = recorded(game, monkeypatch)
    assert await pets.assign_pet(game.scene(starter), hunter) is True
    assert sent == []                                                         # supporters only: nothing for him

    other = game.add_member("Other")                                          # the other way round
    fan = game.add_member("Fan", roles=[PATRON])
    await put(db, other.id, "Cat", 0, holo=True)                              # (the author's album: not read)
    await put(db, fan.id, "Cat", 1, seal=2)
    assert await pets.assign_pet(game.scene(other), fan) is True
    ((message, _),) = sent
    assert message.content == "\n".join([
        HEAD.format(player_id=fan.id),
        "▫️ Jajko - brak",
        "✅ Poziom 1 - masz: pieczęć Łowcy",
        "▫️ Poziom 2 - brak",
        "▫️ Poziom 3 - brak",
        "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: Jajko, Poziom 2, Poziom 3."])
    assert str(other.id) not in message.content and "Holo" not in message.content
    assert (await owner_row(db, fan.id))["pet_id"] > 0 and await owner_row(db, other.id) is None


async def test_the_note_comes_with_the_drop_right_after_a_fight(game_db, monkeypatch):
    """The loot of a boss, a hunt or the tower is handed out moments after the last typed round, while
    ``challenge.fight_on`` is still true (``FIGHT_LINGER``).  The note belongs to the drop - like the pouch
    line and the drop's embed it is posted then, not swallowed as an answer to a click would be."""
    game = game_db
    force(monkeypatch, new_egg("Cat"))
    monkeypatch.setattr(challenge, "FIGHT_LINGER", 15.0)                      # the production value (tests: 0)
    challenge._mark_fight(game.channel)
    assert challenge.fight_on(game.channel)
    ala = game.add_member("Ala", roles=[PATRON])
    sent = recorded(game, monkeypatch)
    assert await pets.assign_pet(game.scene(), ala) is True
    assert challenge.fight_on(game.channel)
    assert [message.content for message in notes(sent)] == [EMPTY_NOTE.format(player_id=ala.id)]


@pytest.mark.parametrize("lost", [asyncio.TimeoutError(), ConnectionResetError("test: connection reset"),
                                  discord.DiscordServerError(SimpleNamespace(status=502, reason="Bad Gateway"),
                                                             "test")],
                         ids=["timeout", "connection reset", "502"])
async def test_a_note_whose_answer_was_lost_is_never_sent_a_second_time(game_db, monkeypatch, caplog, lost):
    """With the REAL ``io.retry``: Discord posts the note, its answer is lost - a send that is repeated
    would put the same note on the channel twice.  The note is ONE attempt (as the other once-only
    messages: the rank's announcement, the patch notes): exactly one note per granted egg."""
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))
    ala = game.add_member("Ala", roles=[PATRON])
    real = game.channel.send
    answers_lost = [lost]
    attempts = []

    async def send(content=None, **kwargs):
        attempts.append(content)
        message = await real(content, **kwargs)                               # it IS on the channel ...
        if answers_lost:
            raise answers_lost.pop()                                          # ... but the bot never hears so
        return message

    monkeypatch.setattr(game.channel, "send", send)
    assert io.is_transient(lost)                                              # (what io.retry would repeat)
    with caplog.at_level(logging.WARNING):
        assert await pets.assign_pet(game.scene(), ala) is True               # the egg is hers all the same
    on_channel = [message.content for message in game.channel.messages if (message.content or "").startswith("📖 ")]
    assert attempts == on_channel == [EMPTY_NOTE.format(player_id=ala.id)]
    assert (await owner_row(db, ala.id))["pet_id"] > 0
    assert any("no note about the new egg" in record.getMessage() for record in caplog.records)


async def test_the_note_tells_what_the_album_holds(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    ola = game.add_member("Ola", roles=[PATRON])
    await put(db, ala.id, "Cat", 0)
    await put(db, ala.id, "Cat", 2, pet_name="Mruczek", shiny=True, frame="S1-A", seal=2, holo=True)
    await put(db, ala.id, "Cat", 2)                                           # does not count
    await put(db, ala.id, "Dog", 1)                                           # another species
    await put(db, ola.id, "Cat", 1)                                           # somebody else's album
    force(monkeypatch, new_egg("Cat", shiny=True, seal=1))
    sent = recorded(game, monkeypatch)
    assert await pets.assign_pet(game.scene(), ala) is True
    (message,) = notes(sent)
    assert message.content == "\n".join([
        HEAD.format(player_id=ala.id),
        "✅ Jajko - masz: zwykła karta bez ozdób ⬆️",
        "▫️ Poziom 1 - brak",
        "✅ Poziom 2 - masz: Shiny, ramka „Gwiazda Szronu (animowana)”, pieczęć Łowcy, Holo",
        "▫️ Poziom 3 - brak",
        "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: Poziom 1, Poziom 3.",
        BETTER])
    assert names_a_species(message.content) == [] and "Mruczek" not in message.content
    assert len(sent) == 1


async def test_an_egg_into_the_pouch_comes_with_the_note_too(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await add_owner(db, ala.id, 5, egg_pouch=True)
    await add_pet(db, 5, pet_lvl=2)
    await put(db, ala.id, "Dragon", 0, holo=True)
    force(monkeypatch, new_egg("Dragon"))
    sent = recorded(game, monkeypatch)
    assert await pets.assign_pet(game.scene(), ala) == pets.INTO_POUCH
    assert [message.content.split("\n")[0] for message, _ in sent] == [
        pets.MSG_EGG_TO_POUCH.format(player_id=ala.id), HEAD.format(player_id=ala.id)]
    assert sent[1][0].content.split("\n")[1:] == [
        "✅ Jajko - masz: Holo", "▫️ Poziom 1 - brak", "▫️ Poziom 2 - brak", "▫️ Poziom 3 - brak",
        "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: Poziom 1, Poziom 2, Poziom 3."]
    owner = await owner_row(db, ala.id)
    assert owner["pet_id"] == 5 and owner["pouch_pet_id"] > 5


async def test_nothing_is_posted_when_no_egg_was_granted(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))
    ala = game.add_member("Ala", roles=[PATRON])
    await add_owner(db, ala.id, 5)                                            # a pet at her side, no pouch
    await add_pet(db, 5)
    ola = game.add_member("Ola", roles=[PATRON])
    await add_owner(db, ola.id, 6, egg_pouch=True, pouch_pet_id=7)            # a pouch that is full
    await add_pet(db, 6)
    await add_pet(db, 7)
    sent = recorded(game, monkeypatch)
    assert await pets.assign_pet(game.scene(), ala) is False
    assert await pets.assign_pet(game.scene(), ola) is False
    assert sent == []


async def test_one_note_per_granted_egg(game_db, monkeypatch):
    game = game_db
    force(monkeypatch, new_egg("Sheep"))
    sent = recorded(game, monkeypatch)
    ala = game.add_member("Ala", roles=[PATRON])
    ela = game.add_member("Ela", roles=[PATRON])
    results = [await pets.assign_pet(game.scene(), member) for member in (ala, ala, ela, ala, ela)]
    assert results == [True, False, True, False, False]
    assert [message.content.split("\n")[0] for message in notes(sent)] == [
        HEAD.format(player_id=ala.id), HEAD.format(player_id=ela.id)]
    assert len(sent) == 2


async def test_a_mythic_egg_is_announced_and_then_noted(game_db, monkeypatch):
    game = game_db
    force(monkeypatch, new_egg("Void"))
    sent = recorded(game, monkeypatch)
    ala = game.add_member("Ala", roles=[PATRON])
    assert await pets.assign_pet(game.scene(), ala) is True
    texts = [message.content for message, _ in sent]
    assert texts[0] == f"Łowca <@{ala.id}> zdobył mitycznego towarzysza! <:POGGERS:936907543849078844>"
    (message,) = notes(sent)
    assert sent[-1][0] is message                                             # after everything else of the egg
    assert message.content.endswith("w albumie brakuje: Jajko, Poziom 1, Poziom 2, Poziom 3.")
    assert names_a_species(message.content) == []


@pytest.mark.parametrize("error", [DatabaseUnavailable("test: the connection broke"),
                                   asyncpg.InsufficientPrivilegeError("test: permission denied"),
                                   RuntimeError("test: anything else")])
async def test_an_album_that_cannot_be_read_costs_only_the_note(game_db, monkeypatch, caplog, error):
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat", seal=3))
    ala = game.add_member("Ala", roles=[PATRON])
    real = album._fetch

    async def unreadable(database, method, sql, *args):
        if sql == album._SQL_OF_SPECIES:
            raise error
        return await real(database, method, sql, *args)

    monkeypatch.setattr(album, "_fetch", unreadable)
    sent = recorded(game, monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert await pets.assign_pet(game.scene(), ala) is True               # the egg is granted all the same
    assert notes(sent) == []
    egg = await pet_row(db, (await owner_row(db, ala.id))["pet_id"])
    assert (egg["type"], egg["seal"], egg["pet_lvl"]) == ("Cat", 3, 0)
    warnings = [record.getMessage() for record in caplog.records if "no note about the new egg" in record.getMessage()]
    assert len(warnings) == 1 and type(error).__name__ in warnings[0] and str(ala.id) in warnings[0]
    assert not any("Album" in line or "album" in line for line in game.bot.owner_log)


async def test_an_album_whose_table_is_missing_is_an_empty_album(game_db, monkeypatch):
    """The database did not answer at the first start: no table yet - nothing is held, and nothing fails."""
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))
    await db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    ala = game.add_member("Ala", roles=[PATRON])
    sent = recorded(game, monkeypatch)
    assert await pets.assign_pet(game.scene(), ala) is True
    assert [message.content for message in notes(sent)] == [EMPTY_NOTE.format(player_id=ala.id)]
    assert (await owner_row(db, ala.id))["pet_id"] > 0


async def test_a_note_discord_refuses_costs_only_the_note(game_db, monkeypatch, caplog):
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))
    ala = game.add_member("Ala", roles=[PATRON])
    before = list(game.channel.bot_messages)
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("test: Discord refuses"), 1
    with caplog.at_level(logging.WARNING):
        assert await pets.assign_pet(game.scene(), ala) is True
    assert (await owner_row(db, ala.id))["pet_id"] > 0 and game.channel.bot_messages == before
    assert any("no note about the new egg" in record.getMessage() for record in caplog.records)
    # The next egg (another player) is noted again: nothing was left behind.
    ela = game.add_member("Ela", roles=[PATRON])
    assert await pets.assign_pet(game.scene(), ela) is True
    assert [message.content for message in game.channel.bot_messages[len(before):]] == [
        EMPTY_NOTE.format(player_id=ela.id)]


async def test_trouble_inside_the_note_never_reaches_the_drop(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    force(monkeypatch, new_egg("Cat"))

    def broken(*args, **kwargs):
        raise RuntimeError("test: the note cannot be written")

    monkeypatch.setattr(album, "egg_note", broken)
    before = list(game.channel.bot_messages)
    ala = game.add_member("Ala", roles=[PATRON])
    assert await pets.assign_pet(game.scene(), ala) is True and game.channel.bot_messages == before

    async def worse(*args, **kwargs):
        raise RuntimeError("test: the whole note failed")

    monkeypatch.setattr(album, "note_new_egg", worse)
    ela = game.add_member("Ela", roles=[PATRON])
    assert await pets.assign_pet(game.scene(), ela) is True and game.channel.bot_messages == before
    assert (await owner_row(db, ela.id))["pet_id"] > 0


@pytest.mark.parametrize("seed", [1, 20261123, 424242])
async def test_the_note_draws_nothing_at_random(game_db, monkeypatch, seed):
    """The same seed with and without the note: the same egg and the same state of the dice after it."""
    game, db = game_db, game_db.bot.db
    with_note = game.add_member("Ala", roles=[PATRON])
    without = game.add_member("Ola", roles=[PATRON])
    for member in (with_note, without):
        for species in ("Cat", "Dragon", "Void"):
            await put(db, member.id, species, 0)
            await put(db, member.id, species, 2, shiny=True)
    sent = recorded(game, monkeypatch)

    async def drop(member) -> tuple:
        random.seed(seed)
        assert await pets.assign_pet(game.scene(), member, cosmetic_rerolls=2) is True
        egg = await pet_row(db, (await owner_row(db, member.id))["pet_id"])
        return {key: value for key, value in egg.items() if key != "pet_id"}, random.getstate()

    noted = await drop(with_note)
    assert len(notes(sent)) == 1

    async def nothing(scene, player, egg):
        return None

    monkeypatch.setattr(pets, "_album_note", nothing)
    assert await drop(without) == noted
    assert len(notes(sent)) == 1


@pytest.mark.parametrize("seed", [7, 20261123])
async def test_a_whole_boss_drop_is_what_it_was(game_db, monkeypatch, seed):
    """A boss drop with an egg in it (``boss.rand_loot``, the egg of a special boss): what drops, the egg and
    the state of the dice after the drop are the same with the note and without it - the note only comes
    between the egg and the drop's embed."""
    game, db = game_db, game_db.bot.db
    table = [{"id": boss.LOOT_EGG, "descr": "Jajko", "weight": 150},          # certain; the bonus egg never drops
             {"id": boss.LOOT_PET_LEVEL, "descr": "Poziom towarzysza", "weight": 60},   # never with a new egg
             {"id": boss.LOOT_MIRROR, "descr": "Lustro wizualiów", "weight": 60}]
    monkeypatch.setattr(boss, "_loot_table", lambda rarity: [dict(item) for item in table])
    with_note = game.add_member("Ala", roles=[PATRON])
    without = game.add_member("Ola", roles=[PATRON])
    for member in (with_note, without):
        for species in ("Cat", "Dragon", "Void"):
            await put(db, member.id, species, 0, seal=1)
    sent = recorded(game, monkeypatch)

    async def drop(member) -> tuple:
        random.seed(seed)
        lines = await boss.rand_loot(game.scene(member), 3, member, 0, cosmetic_rerolls=2)
        egg = await pet_row(db, (await owner_row(db, member.id))["pet_id"])
        assert egg["pet_lvl"] == 0 and lines.startswith("👉 Jajko\n") and "Poziom towarzysza" not in lines
        return lines, {key: value for key, value in egg.items() if key != "pet_id"}, random.getstate()

    noted = await drop(with_note)
    (message,) = notes(sent)
    assert message.content.startswith(HEAD.format(player_id=with_note.id)) and names_a_species(message.content) == []
    posted = [posted for posted, _ in sent if posted is message or posted.embeds]
    assert posted[0] is message and posted[-1].embeds[0].title.startswith("Boss Drop")   # the note, then the drop

    async def nothing(scene, player, egg):
        return None

    monkeypatch.setattr(pets, "_album_note", nothing)
    assert await drop(without) == noted
    assert len(notes(sent)) == 1


async def test_the_egg_of_a_monster_drop_is_noted_once_and_before_the_drop_is_shown(game_db, monkeypatch):
    """``pets.assign_pet`` is the one place an egg is granted: the note comes with every such egg."""
    game = game_db
    monkeypatch.setattr(daily, "_loot_table", lambda rarity: [{"id": daily.LOOT_EGG, "descr": "Jajko", "weight": 100}])
    monkeypatch.setattr(random, "random", lambda: 0.0)
    force(monkeypatch, new_egg("Rabbit"))
    sent = recorded(game, monkeypatch)
    ala = game.add_member("Ala", roles=[PATRON])
    assert await daily.rand_loot(game.scene(ala), 1, ala, 0) == "👉 Jajko\n"
    (message,) = notes(sent)
    assert message.content == EMPTY_NOTE.format(player_id=ala.id)
    assert [bool(posted.embeds) for posted, _ in sent] == [False, True]      # the note, then the drop's embed
    sent.clear()
    assert await daily.rand_loot(game.scene(ala), 1, ala, 0) == ""           # no room: no egg, no note
    assert notes(sent) == []
