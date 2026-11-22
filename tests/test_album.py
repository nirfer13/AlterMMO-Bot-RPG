"""features/album.py: the table, the snapshot, the collection maths, the sacrifice that saves, "Pełny album".

Owner's decisions of 2026-11-21 tested here: the album is for supporters; every sacrifice of a
supporter's pet (eggs too) is saved, in the same transaction that removes the pet; the collection is
every non-mythic species at every stage (68 slots, variant / class / ornaments do not matter); the
mythic pet may be kept but is not needed; "Pełny album" is given for a full collection and never taken
away.  The album's panel and ``$album``: test_album_ui.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from altermmo import schema
from altermmo.core.db import DatabaseUnavailable
from altermmo.features import achievements, album, patrons, pets
from altermmo.features.pets import PetType

from test_pets import ASK, PET_DEFAULTS, add_owner, add_pet, confirms, owner_row, pet_row
from test_pets import plain_cards  # noqa: F401 - autouse: no card is composed here

PATRON = patrons.PATRON_ROLES[0]
MONKAS = "<:MonkaS:882181709100097587>"
LEGACY_QUESTION = ("Czy jesteś pewien, że chcesz porzucić swojego towarzysza <@{player_id}>? "
                   "<:MonkaS:882181709100097587> Kliknij **Potwierdzam** albo wpisz **$potwierdzam**.")


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a $potwierdzam slot was never released"


@pytest.fixture
def sacrifices(monkeypatch):
    """Record pet sacrifices instead of drawing a real boss modifier."""
    calls: list = []

    async def random_modifiers(scene, msg=True, sacrefice=False):
        calls.append(scene.author)
        await scene.send("Poświęcono towarzysza!")
        return "points_boost"

    monkeypatch.setattr(pets.modifiers, "random_modifiers", random_modifiers)
    return calls


# -- helpers --------------------------------------------------------------------------------

def specimen(species: str = "Cat", level: int = 0, pet_id: int = 5, **columns) -> dict:
    """A ``PETS`` row of ``species`` at ``level`` (as features/pets.py stores one)."""
    kind = PetType(species)
    quality = "Mythic" if kind in pets.MYTHIC_EGGS else "Premium" if kind in pets.PREMIUM_TYPES else "Standard"
    return {"pet_id": pet_id, **PET_DEFAULTS, "type": species, "quality": quality, "pet_lvl": level,
            "mythic": kind in pets.MYTHIC_EGGS, "seal": 0, "holo": False, **columns}


async def put(db, player_id: int, species: str = "Cat", level: int = 0, **columns) -> int:
    """Keep a pet in the player's album directly (as a sacrifice does); returns the entry id."""
    async with db.transaction() as con:
        return await album.keep(con, player_id, specimen(species, level, **columns))


async def fill(db, player_id: int, *, leave_out=()) -> int:
    """Every slot of the collection but ``leave_out``; returns how many entries were made."""
    made = 0
    for species in album.collection_species():
        for level in album.stages():
            if (species, level) not in leave_out:
                await put(db, player_id, species, level)
                made += 1
    return made


async def album_rows(db, player_id: int | None = None) -> list:
    if player_id is None:
        return await db.fetch("SELECT * FROM PET_ALBUM ORDER BY ENTRY_ID")
    return await db.fetch("SELECT * FROM PET_ALBUM WHERE PLAYER_ID = $1 ORDER BY ENTRY_ID", player_id)


def role_of(bot) -> int | None:
    return achievements.role_id_of(bot, achievements.trophy("album_full"))


def holds_album_role(bot, member) -> bool:
    role_id = role_of(bot)
    return role_id is not None and any(role.id == role_id for role in member.roles)


async def own_pet(db, player_id: int, pet_id: int = 5, **columns) -> None:
    """The player's active pet (a ``PETOWNER`` row and the ``PETS`` row)."""
    await add_owner(db, player_id, pet_id)
    row = specimen(pet_id=pet_id, **columns)
    await add_pet(db, pet_id, **{key: value for key, value in row.items() if key != "pet_id"})


# -- the collection, pure ---------------------------------------------------------------------

def test_the_collection_is_every_non_mythic_species_at_every_stage():
    species = album.collection_species()
    assert species == tuple(kind.value for kind in PetType if kind is not PetType.VOID)
    assert len(species) == 17 and "Void" not in species and len(set(species)) == 17
    assert album.stages() == (0, 1, 2, 3) == tuple(range(pets.MAX_LEVEL + 1))
    assert album.slot_count() == len(album.collection_slots()) == 68
    assert ("Void", 0) not in album.collection_slots() and ("Cat", 3) in album.collection_slots()
    assert [album.stage_name(level) for level in album.stages()] == ["Jajko", "Poziom 1", "Poziom 2", "Poziom 3"]
    assert set(pets.POLISH_TYPES) >= set(species)                   # every species has its Polish name


def test_the_collection_follows_the_constants(monkeypatch):
    """Computed, not written down: a new species or stage changes the target by itself."""
    monkeypatch.setattr(pets, "MAX_LEVEL", 4)
    assert album.slot_count() == 17 * 5 and album.stages()[-1] == 4
    monkeypatch.setattr(pets, "MYTHIC_EGGS", (PetType.VOID, PetType.GHOST))
    assert album.slot_count() == 16 * 5 and "Ghost" not in album.collection_species()


def test_progress_counts_slots_not_entries():
    every = [(species, level) for species in album.collection_species() for level in album.stages()]
    state = album.progress(every)
    assert (state.count, state.total, state.complete) == (68, 68, True)
    almost = album.progress(every[1:] + every[1:5])                # duplicates count once
    assert (almost.count, almost.complete) == (67, False)
    assert album.progress([("Void", 0), ("Void", 3), ("Type", 0), ("Cat", 7)]).count == 0   # outside
    assert album.progress([]).count == 0 and not album.progress([]).complete
    rows = [specimen("Cat", 0), specimen("Cat", 0, variant="7", shiny=True), specimen("Cat", 2, holo=True)]
    state = album.progress(rows)                                   # entries work as well
    assert state.collected == {("Cat", 0), ("Cat", 2)} and state.stages_of("Cat") == (0, 2)
    assert state.stages_of("Dog") == ()


def test_slot_of_reads_a_numeric_level():
    from decimal import Decimal
    assert album.slot_of({"type": "Dog", "pet_lvl": Decimal("2")}) == ("Dog", 2)
    assert album.slot_of({"type": "Dog", "pet_lvl": None}) == ("Dog", 0)


# -- the table ----------------------------------------------------------------------------------

def test_the_table_is_declared_for_the_start_up_and_for_verify():
    assert "altermmo.features.album" in schema.SCHEMA_MODULES
    assert album.NEW_TABLES == album.SCHEMA and len(album.SCHEMA) == 2     # the table and its index
    # Added later: the frame and the partial Holo of 2026-11-21, with the definitions of PETS.
    assert album.LATER_COLUMNS == (("FRAME", "VARCHAR(16) NOT NULL DEFAULT ''"),
                                   ("HOLO_PARTIAL", "BOOLEAN NOT NULL DEFAULT FALSE"))
    assert album.NEW_COLUMNS == (("pet_album", "frame", "VARCHAR(16) NOT NULL DEFAULT ''"),
                                 ("pet_album", "holo_partial", "BOOLEAN NOT NULL DEFAULT FALSE"))
    assert {column: definition for _, column, definition in album.NEW_COLUMNS} == {
        column: definition for table, column, definition in pets.NEW_COLUMNS if column in ("frame", "holo_partial")}
    assert set(album.LATER_DEFAULTS) == {column for column, _ in album.LATER_COLUMNS}
    expected = schema.EXPECTED_COLUMNS["pet_album"]
    assert list(expected)[:3] == ["entry_id", "player_id", "added_at"]
    assert [column.lower() for column, _ in album.SNAPSHOT_COLUMNS] == list(expected)[3:]
    # The snapshot has every column of PETS the bot knows, under the same names and types.
    assert {column: kind for column, kind in expected.items() if column in schema.EXPECTED_COLUMNS["pets"]} == \
        schema.EXPECTED_COLUMNS["pets"]


async def test_the_table_has_the_snapshot_of_every_pets_column(db):
    async def columns(table: str) -> dict:
        rows = await db.fetch(
            """SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = $1 ORDER BY ordinal_position""", table)
        return {row["column_name"]: (row["data_type"], row["is_nullable"], row["column_default"]) for row in rows}

    pets_table, album_table = await columns("pets"), await columns("pet_album")
    assert list(album_table)[:3] == ["entry_id", "player_id", "added_at"]
    assert {name: album_table[name] for name in pets_table} == pets_table   # same names, types, defaults
    assert album_table["entry_id"][:2] == ("bigint", "NO") and album_table["player_id"][:2] == ("numeric", "NO")
    assert album_table["added_at"][:2] == ("timestamp with time zone", "NO")
    index = await db.fetchval("SELECT indexdef FROM pg_indexes WHERE tablename = 'pet_album' "
                              "AND indexname = 'pet_album_player_id_idx'")
    assert index is not None and "(player_id)" in index
    assert await schema.verify(db) == []


async def test_the_old_bots_database_gets_the_table_at_start_up_and_keeps_it(db):
    await db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    assert "brak tabeli pet_album" in await schema.verify(db)
    await schema.create_new_tables(db)
    entry_id = await put(db, 1)
    await schema.create_new_tables(db)                                    # the next start: nothing is lost
    assert [row["entry_id"] for row in await album_rows(db)] == [entry_id]
    assert await schema.verify(db) == [] and await schema.add_new_columns(db) == []


async def test_a_column_added_later_reaches_an_existing_table(db, monkeypatch):
    """What the next stage does with FRAME / HOLO_PARTIAL: one entry in LATER_COLUMNS."""
    later = (("FRAME", "VARCHAR(16) NOT NULL DEFAULT ''"),)
    monkeypatch.setattr(album, "LATER_COLUMNS", later)
    monkeypatch.setattr(album, "NEW_COLUMNS", (("pet_album", "frame", later[0][1]),))
    monkeypatch.setattr(album, "SNAPSHOT_COLUMNS", album.BASE_COLUMNS + later)
    old = await put(db, 1)                                                # an entry from before
    assert await schema.add_new_columns(db) == []
    rows = await album_rows(db)
    assert rows[0]["entry_id"] == old and rows[0]["frame"] == ""
    # PETS has no FRAME yet: the copy takes what the row has; a row that has it is copied with it.
    assert (await album.entry(db, 1, await put(db, 1)))["frame"] == ""
    assert (await album.entry(db, 1, await put(db, 1, frame="S1-A")))["frame"] == "S1-A"


# -- keep / entries / remove ---------------------------------------------------------------------

async def test_keep_copies_the_pets_row_under_the_same_names(db):
    await add_pet(db, 77, **{**PET_DEFAULTS, "pet_name": "Mruczek O'Neil", "pet_lvl": 2, "pet_skills": 5,
                             "quality": "Premium", "type": "Dragon", "variant": "1", "shiny": True,
                             "ultra_shiny": True, "crit_perc": 12, "slow_perc": 33, "detection": True,
                             "seal": 3, "holo": True})
    row = await db.fetchrow("SELECT * FROM PETS WHERE PET_ID = 77")
    before = datetime.now(timezone.utc)
    async with db.transaction() as con:
        entry_id = await album.keep(con, 4242, row)
    kept = await album.entry(db, 4242, entry_id)
    assert {name: kept[name] for name in row.keys()} == dict(row)       # everything, as it was
    assert kept["player_id"] == 4242 and kept["entry_id"] == entry_id
    assert kept["added_at"].tzinfo is not None and before - timedelta(seconds=5) <= kept["added_at"]
    assert kept["added_at"] <= datetime.now(timezone.utc) + timedelta(seconds=5)
    # ... so the existing helpers take it as a pet.
    assert pets.cosmetics_of(kept) == (3, True)
    assert pets._pet_image(kept) == pets._pet_image(row)
    assert pets.petcards.look_of(kept) == pets.petcards.look_of(row)


async def test_keep_tolerates_a_row_without_the_cosmetic_columns(db):
    """The old bot's PETS (no SEAL / HOLO): the entry has no seal and no foil."""
    old_row = {key: value for key, value in specimen("Dog", 1).items() if key not in ("seal", "holo")}
    old_row["mythic"] = None                                            # NULL in an old row
    async with db.transaction() as con:
        entry_id = await album.keep(con, 1, old_row)
    kept = await album.entry(db, 1, entry_id)
    assert (kept["seal"], kept["holo"], kept["mythic"]) == (0, False, False) and kept["type"] == "Dog"


async def refuse_every_entry(db) -> None:
    """PET_ALBUM refuses every new row (as a database that cannot write the album would)."""
    await db.execute("ALTER TABLE PET_ALBUM ADD CONSTRAINT TEST_REFUSES CHECK (false) NOT VALID", idempotent=True)


async def test_keep_refused_by_the_database_raises_not_saved(db):
    await refuse_every_entry(db)
    with pytest.raises(album.NotSaved):
        async with db.transaction() as con:
            await album.keep(con, 1, specimen())


async def test_a_table_the_start_up_could_not_make_is_made_by_the_first_keep(db):
    """The database did not answer at the first start: ``create_new_tables`` never ran this run."""
    await db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    assert await album.entries(db, 1) == [] and await album.collected(db, 1) == set()   # nothing, no error
    assert await album.entry(db, 1, 1) is None and await album.complete(db, 1) is False
    async with db.transaction() as con:
        entry_id = await album.keep(con, 1, specimen("Cat", 2, seal=2))
    assert [row["entry_id"] for row in await album.entries(db, 1)] == [entry_id]
    assert (await album.entry(db, 1, entry_id))["seal"] == 2
    assert await schema.verify(db) == []                                  # the table as the start-up makes it
    async with db.transaction() as con:                                   # (made once)
        assert await album.keep(con, 1, specimen("Cat", 3)) > entry_id
    # A sacrifice that is rolled back takes the new table back with it: the next one makes it again.
    await db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    with pytest.raises(RuntimeError):
        async with db.transaction() as con:
            await album.keep(con, 1, specimen())
            raise RuntimeError("test: the sacrifice failed after the album was written")
    assert await album.entries(db, 1) == []
    async with db.transaction() as con:
        await album.keep(con, 1, specimen())
    assert len(await album.entries(db, 1)) == 1


#: ``PET_ALBUM`` as the first version made it (2026-11-21, before FRAME / HOLO_PARTIAL).
FIRST_ALBUM = album.SCHEMA[0].replace(
    ",\n           FRAME VARCHAR(16) NOT NULL DEFAULT '',\n           HOLO_PARTIAL BOOLEAN NOT NULL DEFAULT FALSE", "")


async def first_album(db) -> None:
    assert "FRAME" not in FIRST_ALBUM and "HOLO BOOLEAN" in FIRST_ALBUM
    await db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    await db.execute(FIRST_ALBUM, idempotent=True)


async def test_an_album_made_before_the_frames_gets_their_columns_at_start_up(db):
    await first_album(db)
    old = await put(db, 1, "Cat", 2, seal=2, holo=True)                  # kept before: PETS had no FRAME yet
    assert await schema.verify(db) == ["pet_album: brak kolumny frame", "pet_album: brak kolumny holo_partial"]
    assert await schema.add_new_columns(db) == []
    (row,) = await album_rows(db)
    assert row["entry_id"] == old and (row["seal"], row["holo"], row["frame"], row["holo_partial"]) == (
        2, True, "", False)
    assert await schema.verify(db) == []


async def test_keep_copies_the_frame_and_the_partial_holo(db):
    await add_pet(db, 77, **{**PET_DEFAULTS, "pet_lvl": 1, "shiny": True, "frame": "S1-A", "holo_partial": True,
                             "seal": 1})
    row = await db.fetchrow("SELECT * FROM PETS WHERE PET_ID = 77")
    async with db.transaction() as con:
        entry_id = await album.keep(con, 4242, row)
    kept = await album.entry(db, 4242, entry_id)
    assert {name: kept[name] for name in row.keys()} == dict(row)
    assert (kept["frame"], kept["holo_partial"], kept["holo"]) == ("S1-A", True, False)
    assert pets.petcards.look_of(kept) == pets.petcards.Look(frame="S1-A", seal=1, foil=pets.petcards.FOIL_PARTIAL)
    assert pets.ornaments_of(kept) == pets.Ornaments("S1-A", 1, pets.petcards.FOIL_PARTIAL)


async def test_an_album_without_the_frame_columns_keeps_what_it_can_and_refuses_the_rest(db):
    """The new columns could not be added to an earlier album: a pet with nothing but their defaults is
    kept as it is; a pet that really has a frame / a partial Holo is NOT kept (nothing may be lost)."""
    await first_album(db)
    plain = await put(db, 1, "Cat", 1, seal=3, holo=True, frame="", holo_partial=False)
    (row,) = await album_rows(db)
    assert row["entry_id"] == plain and (row["seal"], row["holo"]) == (3, True) and "frame" not in row
    for rich in (dict(shiny=True, frame="S1"), dict(holo_partial=True)):
        with pytest.raises(album.NotSaved, match="UndefinedColumnError"):
            await put(db, 1, "Cat", 1, **rich)
    assert len(await album_rows(db)) == 1


async def test_a_sacrifice_that_would_lose_the_frame_is_refused(game_db, sacrifices):
    db = game_db.bot.db
    await first_album(db)
    player = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, player.id, 5, pet_lvl=2, shiny=True, frame="R7-A")
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is False
    assert (await pet_row(db, 5))["frame"] == "R7-A" and (await owner_row(db, player.id))["pet_id"] == 5
    assert sacrifices == [] and game_db.channel.contains(album.MSG_NOT_SAVED.format(player_id=player.id))
    # The same player's pet with the base frame goes into that album all the same.
    await db.execute("UPDATE PETS SET FRAME = '' WHERE PET_ID = 5")
    game_db.channel.send_hooks.clear()
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert len(await album_rows(db, player.id)) == 1 and await pet_row(db, 5) is None


async def test_an_entry_shows_and_draws_its_frame_and_partial_holo(game_db, monkeypatch):
    db, bot = game_db.bot.db, game_db.bot
    entry_id = await put(db, 1, "Cat", 2, variant="1", shiny=True, ultra_shiny=True, frame="U1-A",
                         holo_partial=True, seal=2)
    row = await album.entry(db, 1, entry_id)
    asked = []

    async def card(source, look, cache_dir, *, max_bytes=None):
        asked.append(look)
        return source

    monkeypatch.setattr(pets.petcards, "card", card)
    embed, image = await album.entry_view(bot, game_db.guild, row, 0, 1)
    assert asked == [pets.petcards.Look(frame="U1-A", seal=2, foil=pets.petcards.FOIL_PARTIAL)]
    for line in ("Klasa: **Ultra Shiny**", "Ramka: **Rubinowa Korona (animowana)**", "Pieczęć: **Łowcy**",
                 "Folia: **Holo częściowe**"):
        assert line in embed.description
    assert image is not None and embed.image.url == f"attachment://{image.name}"


async def test_entries_are_in_album_order_and_only_the_players_own(db):
    ids = {
        "cat2": await put(db, 1, "Cat", 2), "bear0": await put(db, 1, "Bear", 0),
        "void0": await put(db, 1, "Void", 0), "cat0": await put(db, 1, "Cat", 0),
        "cat0b": await put(db, 1, "Cat", 0, variant="9"), "other": await put(db, 2, "Bear", 0),
    }
    order = [row["entry_id"] for row in await album.entries(db, 1)]
    # Species in the order of PetType (Bear, ..., Cat, ..., Void last), then the stage, then when kept.
    assert order == [ids["bear0"], ids["cat0"], ids["cat0b"], ids["cat2"], ids["void0"]]
    assert [row["entry_id"] for row in await album.entries(db, 2)] == [ids["other"]]
    assert await album.entries(db, 3) == []
    assert await album.entry(db, 1, ids["other"]) is None                  # somebody else's
    assert await album.collected(db, 1) == {("Bear", 0), ("Cat", 0), ("Cat", 2), ("Void", 0)}


async def test_remove_happens_once_and_only_for_the_owner(db):
    mine, theirs = await put(db, 1), await put(db, 2)
    assert await album.remove(db, 2, mine) is False                       # not his
    assert await album.remove(db, 1, mine) is True
    assert await album.remove(db, 1, mine) is False                       # gone already
    assert [row["entry_id"] for row in await album_rows(db)] == [theirs]


async def test_complete_from_the_database(db):
    await fill(db, 1, leave_out={("Ghost", 3)})
    await put(db, 1, "Void", 3)                                           # the mythic pet does not help
    assert await album.complete(db, 1) is False
    assert album.progress(await album.collected(db, 1)).count == 67
    await put(db, 1, "Ghost", 3)
    assert await album.complete(db, 1) is True
    assert await album.complete(db, 2) is False


# -- the sacrifice ---------------------------------------------------------------------------------

async def test_a_supporters_sacrifice_is_saved_in_the_album(game_db, sacrifices):
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, player.id, 5, pet_name="Mruczek", pet_lvl=2, seal=2, holo=True, shiny=True)
    await add_pet(db, 6)
    before = dict(await db.fetchrow("SELECT * FROM PETS WHERE PET_ID = 5"))
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True

    question = next(m for m in game_db.channel.sent if ASK in m.content)
    assert question.content == (f"Czy jesteś pewien, że chcesz porzucić swojego towarzysza <@{player.id}>? "
                                f"{MONKAS} Jako wspierający zachowasz go w swoim albumie (**$album**). "
                                "Kliknij **Potwierdzam** albo wpisz **$potwierdzam**.")
    assert question.content.endswith(pets.CONFIRM_HINT)
    assert await pet_row(db, 5) is None and await pet_row(db, 6) is not None
    assert (await owner_row(db, player.id))["pet_id"] == 0
    (kept,) = await album_rows(db)
    assert kept["player_id"] == player.id and {name: kept[name] for name in before} == before
    assert sacrifices == [player]
    texts = [m.content for m in game_db.channel.sent]
    saved = album.MSG_DISCARD_SAVED.format(player_id=player.id)
    assert texts.count(saved) == 1 and "**$album**" in saved
    assert texts.index("Poświęcono towarzysza!") < texts.index(saved)    # after the sacrifice itself
    answer = next(m for m in game_db.channel.messages if m.content == "$potwierdzam")
    assert answer.reactions == [MONKAS]                                   # the legacy "seen", as before


async def test_an_egg_is_saved_too(game_db, sacrifices):
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[patrons.TWITCH_SUB_ROLE])
    await own_pet(db, player.id, 5, pet_lvl=0, type="Rabbit", variant="7")
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    (kept,) = await album_rows(db, player.id)
    assert album.slot_of(kept) == ("Rabbit", 0) and kept["variant"] == "7"


async def test_everybody_elses_sacrifice_is_what_it_always_was(game_db, sacrifices):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await own_pet(db, player.id, 5, pet_lvl=3)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    question = next(m for m in game_db.channel.sent if ASK in m.content)
    assert question.content == LEGACY_QUESTION.format(player_id=player.id)
    assert "album" not in " ".join(m.text for m in game_db.channel.sent).lower()
    assert await pet_row(db, 5) is None and await album_rows(db) == [] and sacrifices == [player]


async def test_the_status_at_the_sacrifice_counts(game_db, sacrifices):
    """Asked as a supporter, no longer one when he confirms: lost as for everybody (and vice versa)."""
    db = game_db.bot.db
    ala = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, ala.id, 5)

    def answer(message):
        if ASK not in message.content:
            return None
        ala.roles.clear()                                                 # the support ended meanwhile
        return "$potwierdzam"

    game_db.auto_answer(ala, responder=answer)
    assert await pets.discard_pet(game_db.scene(ala)) is True
    assert album.MSG_DISCARD_KEPT in next(m for m in game_db.channel.sent if ASK in m.content).content
    assert await pet_row(db, 5) is None and await album_rows(db) == []
    assert not game_db.channel.ever_sent(album.MSG_DISCARD_SAVED.format(player_id=ala.id))

    game_db.channel.send_hooks.clear()
    ola = game_db.add_member("Ola")
    await own_pet(db, ola.id, 6)

    def becomes_supporter(message):
        if ASK not in message.content or f"<@{ola.id}>" not in message.content:
            return None
        ola.roles.append(game_db.guild.add_role(PATRON))
        return "$potwierdzam"

    game_db.auto_answer(ola, responder=becomes_supporter)
    assert await pets.discard_pet(game_db.scene(ola)) is True
    assert len(await album_rows(db, ola.id)) == 1


async def test_an_album_that_cannot_be_written_stops_the_sacrifice(game_db, sacrifices):
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, player.id, 5, pet_lvl=1)
    await refuse_every_entry(db)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is False
    assert await pet_row(db, 5) is not None and (await owner_row(db, player.id))["pet_id"] == 5
    assert sacrifices == [] and game_db.channel.contains(album.MSG_NOT_SAVED.format(player_id=player.id))
    (note,) = [line for line in game_db.bot.owner_log if line.startswith("Album: nie udało się zapisać")]
    assert "PET_ID 5" in note and f"<@{player.id}>" in note and "CheckViolationError" in note
    assert pets._awaiting_confirmation == set()


async def test_a_sacrifice_works_when_the_start_up_could_not_make_the_album(game_db, sacrifices):
    """The database did not answer at the first start: the first kept sacrifice makes the table."""
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, player.id, 5, pet_lvl=1)
    await db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    assert await achievements.check_album(game_db.scene(player), player) == []   # nothing, no error
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert await pet_row(db, 5) is None and len(await album_rows(db, player.id)) == 1
    assert game_db.channel.contains(album.MSG_DISCARD_SAVED.format(player_id=player.id))
    assert not game_db.channel.ever_sent(album.MSG_NOT_SAVED.format(player_id=player.id))
    assert game_db.bot.owner_log == [] and sacrifices == [player]


async def test_a_failure_after_the_album_was_written_takes_both_back(game_db, monkeypatch, sacrifices):
    """The INSERT and the DELETE are one transaction: never in both places, never in neither."""
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, player.id, 5, pet_lvl=1)
    keep = album.keep

    async def written_then_broken(con, player_id, pet):
        await keep(con, player_id, pet)
        assert len(await con.fetch("SELECT * FROM PET_ALBUM")) == 1          # (inside: it is written)
        raise DatabaseUnavailable("test: the connection broke after the INSERT")

    monkeypatch.setattr(album, "keep", written_then_broken)
    confirms(game_db, player)
    with pytest.raises(DatabaseUnavailable):
        await pets.discard_pet(game_db.scene(player))
    assert await album_rows(db) == [] and await pet_row(db, 5) is not None
    assert (await owner_row(db, player.id))["pet_id"] == 5 and sacrifices == []
    monkeypatch.undo()
    game_db.channel.send_hooks.clear()
    monkeypatch.setattr(pets.modifiers, "random_modifiers", lambda *args: asyncio.sleep(0))
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True             # ... and simply again
    assert len(await album_rows(db)) == 1 and await pet_row(db, 5) is None


async def test_a_stale_panel_saves_nothing(game_db, sacrifices):
    """The panel's button acts only on its own pet - and keeps nothing when it may not act."""
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, player.id, 5)
    assert await pets.discard_pet(game_db.scene(player), expected_pet=9) is False
    assert game_db.channel.messages[-1].content == pets.MSG_OTHER_PET
    assert await album_rows(db) == [] and await pet_row(db, 5) is not None

    # Another pet comes out of the stable while the question is open: refused after it, nothing kept.
    await add_pet(db, 6)

    def swap(message):
        if ASK not in message.content:
            return None
        asyncio.ensure_future(db.execute("UPDATE PETOWNER SET PET_ID = 6 WHERE PLAYER_ID = $1", player.id))
        return "$potwierdzam"

    game_db.auto_answer(player, responder=swap, delay=0.05)
    assert await pets.discard_pet(game_db.scene(player), expected_pet=5) is False
    assert game_db.channel.messages[-1].content == pets.MSG_OTHER_PET
    assert await album_rows(db) == [] and await pet_row(db, 5) is not None and sacrifices == []


async def test_sacrifice_without_a_pet_row_saves_nothing(game_db, sacrifices):
    """A PETOWNER row pointing at a pet that is not in PETS: emptied as before, nothing to keep."""
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await add_owner(db, player.id, 77)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert (await owner_row(db, player.id))["pet_id"] == 0 and await album_rows(db) == []
    assert not game_db.channel.ever_sent(album.MSG_DISCARD_SAVED.format(player_id=player.id))


async def test_two_sacrifices_of_one_species_and_stage_are_two_entries(game_db, sacrifices, monkeypatch):
    """Several pets of one type are welcome (a rare frame ...)."""
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    for pet_id in (5, 6):
        await db.execute("DELETE FROM PETOWNER WHERE PLAYER_ID = $1", player.id)
        await own_pet(db, player.id, pet_id, pet_lvl=1, shiny=pet_id == 6)
        game_db.channel.send_hooks.clear()
        confirms(game_db, player)
        assert await pets.discard_pet(game_db.scene(player)) is True
    rows = await album_rows(db, player.id)
    assert [album.slot_of(row) for row in rows] == [("Cat", 1), ("Cat", 1)]
    assert [row["shiny"] for row in rows] == [False, True]


# -- "Pełny album" ---------------------------------------------------------------------------------

async def test_the_last_missing_slot_gives_pelny_album(game_db, sacrifices):
    bot, db = game_db.bot, game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    assert await fill(db, player.id, leave_out={("Ghost", 2)}) == 67
    await own_pet(db, player.id, 5, type="Ghost", quality="Premium", pet_lvl=2)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert holds_album_role(bot, player)
    assert game_db.guild.get_role(role_of(bot)).name == "Pełny album"
    texts = [m.content for m in game_db.channel.sent]
    announced = f"<@{player.id}> zdobywa osiągnięcie: Pełny album!"
    assert texts.count(announced) == 1
    assert texts.index(album.MSG_DISCARD_SAVED.format(player_id=player.id)) < texts.index(announced)


async def test_a_failing_boss_modifier_still_announces_the_kept_pet_and_checks_the_album(game_db, monkeypatch):
    """The sacrifice is committed (in the album, gone from PETS) before the modifier is drawn: when the
    modifier step fails (Discord down ...) its error still surfaces, but the player is told the pet was
    kept and "Pełny album" is checked all the same."""
    bot, db = game_db.bot, game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await fill(db, player.id, leave_out={("Ghost", 2)})
    await own_pet(db, player.id, 5, type="Ghost", quality="Premium", pet_lvl=2)

    async def broken(scene, msg=True, sacrefice=False):
        raise RuntimeError("test: Discord does not answer")

    monkeypatch.setattr(pets.modifiers, "random_modifiers", broken)
    confirms(game_db, player)
    with pytest.raises(RuntimeError, match="Discord does not answer"):
        await pets.discard_pet(game_db.scene(player))
    assert await pet_row(db, 5) is None and await album.complete(db, player.id)
    assert game_db.channel.contains(album.MSG_DISCARD_SAVED.format(player_id=player.id))
    assert holds_album_role(bot, player)
    assert game_db.channel.sent_count("zdobywa osiągnięcie: Pełny album") == 1
    assert pets._awaiting_confirmation == set()


async def test_a_failing_announcement_never_hides_the_modifiers_error(game_db, monkeypatch):
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, player.id, 5)

    async def gone(*args, **kwargs):
        raise RuntimeError("test: the channel is gone")

    async def broken(scene, msg=True, sacrefice=False):
        monkeypatch.setattr(game_db.channel, "send", gone)                     # the announcement fails too
        raise RuntimeError("test: the modifier failed")

    monkeypatch.setattr(pets.modifiers, "random_modifiers", broken)
    confirms(game_db, player)
    with pytest.raises(RuntimeError, match="the modifier failed"):
        await pets.discard_pet(game_db.scene(player))
    assert len(await album_rows(db, player.id)) == 1 and await pet_row(db, 5) is None
    assert pets._awaiting_confirmation == set()


async def test_a_fight_that_starts_during_the_question_silences_the_album_notice(game_db, monkeypatch,
                                                                                 sacrifices):
    """Owner's rule: nothing is posted between a typed fight's prompts.  The pet is kept and "Pełny
    album" is given, but neither "kept in the album" nor the achievement is announced then."""
    from altermmo.core import challenge

    bot, db = game_db.bot, game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await fill(db, player.id, leave_out={("Ghost", 2)})
    await own_pet(db, player.id, 5, type="Ghost", quality="Premium", pet_lvl=2)
    fighting: list[bool] = []
    monkeypatch.setattr(challenge, "fight_on", lambda channel: bool(fighting))

    def answer(message):
        if ASK not in message.content:
            return None
        fighting.append(True)                                             # a boss fight starts meanwhile
        return "$potwierdzam"

    game_db.auto_answer(player, responder=answer)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert await pet_row(db, 5) is None and await album.complete(db, player.id)
    assert holds_album_role(bot, player)
    assert not game_db.channel.ever_sent(album.MSG_DISCARD_SAVED.format(player_id=player.id))
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 0


async def test_sixty_seven_slots_are_not_enough(game_db, sacrifices):
    bot, db = game_db.bot, game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await fill(db, player.id, leave_out={("Ghost", 2), ("Bear", 0)})
    await put(db, player.id, "Ghost", 1)                                  # a second one of a slot he has
    await put(db, player.id, "Void", 2)                                   # the mythic pet: not needed
    await own_pet(db, player.id, 5, type="Ghost", quality="Premium", pet_lvl=2)
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True         # 67 of 68
    assert not holds_album_role(bot, player) and not game_db.channel.ever_sent("Pełny album")
    assert await achievements.check_album(game_db.scene(), player) == []


async def test_pelny_album_is_never_taken_away(game_db):
    bot, db = game_db.bot, game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await fill(db, player.id)
    assert [item.key for item in await achievements.check_album(game_db.scene(), player)] == ["album_full"]
    for row in await album.entries(db, player.id):
        assert await album.remove(db, player.id, row["entry_id"])
    assert await album.complete(db, player.id) is False
    await achievements.sync_veterans(bot)
    assert await achievements.check_album(game_db.scene(), player) == []
    assert await achievements.show_achievements(game_db.scene(player, "$osiagniecia")) is True
    assert holds_album_role(bot, player)
    assert f"<@{player.id}>, masz **1 z 5** osiągnięć." in game_db.channel.bot_messages[-1].text


async def test_an_album_full_before_the_role_existed_is_handed_out_at_start_up(game_db):
    bot, db = game_db.bot, game_db.bot.db
    veteran = game_db.add_member("Weteran", roles=[patrons.YOUTUBE_ROLES[0]])
    plain = game_db.add_member("Zwykły")
    for member in (veteran, plain):
        await add_owner(db, member.id, 0)                                 # (a sacrifice leaves the row)
        await fill(db, member.id)
    assert await achievements.deserved(bot, veteran.id) == {"album_full"}
    assert await achievements.sync_veterans(bot) == (1, 1)                # start-up / $nadajosiagniecia
    assert holds_album_role(bot, veteran) and not holds_album_role(bot, plain)
    notes = [line for line in bot.owner_log if line.startswith("Zaległe osiągnięcia")]
    assert notes == [achievements.LOG_VETERANS.format(roles=1, players=1) + f"\n<@{veteran.id}>: Pełny album"]
    assert game_db.channel.sent_count("zdobywa osiągnięcie") == 0         # quietly
    assert await achievements.sync_veterans(bot) == (0, 0)


async def test_a_non_supporter_gets_nothing_for_a_full_album(game_db):
    bot, db = game_db.bot, game_db.bot.db
    plain = game_db.add_member("Zwykły")
    await fill(db, plain.id)                                              # (kept while he supported)
    assert await achievements.check_album(game_db.scene(), plain) == []
    assert await achievements.sync(game_db.scene(), plain) == []
    assert not holds_album_role(bot, plain) and bot.store.achievement_roles.load() == {}


async def test_the_album_and_the_pets_stand_for_their_own_achievements(game_db):
    bot, db = game_db.bot, game_db.bot.db
    patron = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, patron.id, 5, type="Void", quality="Mythic", mythic=True, holo=True)
    await fill(db, patron.id)
    assert await achievements.deserved(bot, patron.id) == {"pet_mythic", "pet_mythic_holo", "album_full"}
    assert [item.key for item in await achievements.check_pets(game_db.scene(), patron)] == [
        "pet_mythic", "pet_mythic_holo"]                                   # the pets do not give the album's
    assert [item.key for item in await achievements.check_album(game_db.scene(), patron)] == ["album_full"]


async def test_check_album_never_raises(game_db, monkeypatch):
    patron = game_db.add_member("Ala", roles=[PATRON])
    assert await achievements.check_album(game_db.scene(), None) == []
    assert await achievements.check_album(game_db.scene(), 555) == []     # left the server

    async def broken(*args, **kwargs):
        raise RuntimeError("test")

    monkeypatch.setattr(album, "complete", broken)
    assert await achievements.check_album(game_db.scene(), patron) == []


async def test_an_unreadable_album_does_not_stop_the_pet_achievements(game_db):
    bot, db = game_db.bot, game_db.bot.db
    patron = game_db.add_member("Ala", roles=[PATRON])
    await own_pet(db, patron.id, 5, type="Void", quality="Mythic", mythic=True)
    await db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    assert await achievements.deserved(bot, patron.id) == {"pet_mythic"}
    assert [item.key for item in await achievements.sync(game_db.scene(), patron)] == ["pet_mythic"]


@pytest.mark.parametrize("error", [DatabaseUnavailable("test: the connection broke"),
                                   asyncpg.InsufficientPrivilegeError("test: permission denied")])
async def test_an_album_that_cannot_be_read_costs_only_the_album_achievement(game_db, monkeypatch, caplog, error):
    """A broken connection / time-out (the db layer's DatabaseUnavailable) or a refusal of the database:
    the pets' achievements are still handed out, and the start-up hand-out logs it once, not per player."""
    bot, db = game_db.bot, game_db.bot.db
    supporters = [game_db.add_member(name, roles=[PATRON]) for name in ("Ala", "Ola", "Ela")]
    for pet_id, member in enumerate(supporters, start=5):
        await own_pet(db, member.id, pet_id, type="Void", quality="Mythic", mythic=True)
        await fill(db, member.id)

    async def unreadable(*args, **kwargs):
        raise error

    monkeypatch.setattr(album, "complete", unreadable)
    assert await achievements.deserved(bot, supporters[0].id) == {"pet_mythic"}
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await achievements.sync_veterans(bot) == (3, 3)                   # the mythic pet, each
    assert all(not holds_album_role(bot, member) for member in supporters)
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    assert "3" in warnings[0].getMessage() and type(error).__name__ in warnings[0].getMessage()
    # Outside the hand-out (one player): one line for that player, and nothing raised.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await achievements.check_album(game_db.scene(), supporters[0]) == []
    assert len([record for record in caplog.records if record.levelno >= logging.WARNING]) == 1


async def test_the_album_role_is_kept_by_the_role_retirement(game):
    bot = game.bot
    await achievements.ensure_roles(bot)
    role_id = role_of(bot)
    assert role_id is not None and game.guild.get_role(role_id) is not None
    assert role_id in achievements._protected_roles(bot)
    assert all(key != "album_full" for _, _, key in achievements._withdrawn(bot))
    assert await achievements.retire_roles(bot) == [] and game.guild.get_role(role_id) is not None
    assert achievements.retired_left(bot) == []


def what_the_version_before_reads(raw: dict) -> dict:
    """``data/achievementRoles.json`` as the version of 2026-11-20 (bde7ce0) reads it: every key with a
    positive number (``int(value or 0)``, 0 on an error).  Its ``retire_roles`` deletes the role of
    every key there that is not in its catalogue."""
    def number(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {str(key): number(value) for key, value in raw.items() if number(value) > 0}


async def test_a_rollback_to_the_version_before_the_album_leaves_its_role_alone(game):
    """Owner's rule: "Pełny album" is never taken away - also not by a rollback of the bot."""
    bot = game.bot
    holder = game.add_member("Ala", roles=[PATRON])
    await achievements.ensure_roles(bot)
    role_id = role_of(bot)
    holder.roles.append(game.guild.get_role(role_id))
    raw = bot.store.achievement_roles.load()
    assert set(what_the_version_before_reads(raw)) == {"pet_mythic", "pet_mythic_holo", "pet_mythic_seal"}
    assert raw[achievements.LATER_ROLES_KEY] == {"album_full": role_id}
    # That version rewrites the file without the album's id: the role (still there, with its holders) is
    # still known by the id in data/achievementRolesLater.json - not made a second time - and the spare
    # copy is written back.
    bot.store.achievement_roles.save(what_the_version_before_reads(raw))
    for remembered in ("_achievement_roles", "_achievement_made"):          # (a restart)
        if hasattr(bot, remembered):
            delattr(bot, remembered)
    roles = len(game.guild.roles)
    await achievements.ensure_roles(bot)
    assert role_of(bot) == role_id and len(game.guild.roles) == roles and holds_album_role(bot, holder)
    assert bot.store.achievement_roles.load()[achievements.LATER_ROLES_KEY] == {"album_full": role_id}


def restart(bot) -> None:
    """What the bot remembers of its roles is forgotten (a new start of the bot)."""
    for remembered in ("_achievement_roles", "_achievement_made", "_achievement_told",
                       "_achievement_roles_retry_at", "_achievement_roles_unreadable"):
        if hasattr(bot, remembered):
            delattr(bot, remembered)


async def boot(bot) -> None:
    """The role part of a start (``achievements.start_up`` without the hand-out, which needs the database)."""
    await achievements.retire_roles(bot)
    await achievements.ensure_roles(bot)


#: The bot-made achievements of the version of 2026-11-20 (bde7ce0).
KEYS_BEFORE = {"pet_mythic", "pet_mythic_holo", "pet_mythic_seal"}


async def roll_back(game) -> None:
    """A start of the version of 2026-11-20 (bde7ce0): it deletes the role of every saved key that is
    not in its catalogue and rewrites ``data/achievementRoles.json`` with what it read (no album id)."""
    head = what_the_version_before_reads(game.bot.store.achievement_roles.load())
    for key in [key for key in head if key not in KEYS_BEFORE]:
        role = game.guild.get_role(head.pop(key))
        if role is not None:
            game.guild.roles.remove(role)
    game.bot.store.achievement_roles.save(head)
    restart(game.bot)


async def test_a_rollback_that_rewrites_the_roles_file_keeps_the_renamed_album_role(game, monkeypatch):
    """The first start of this version cannot finish the retirement (the member list is incomplete), so
    the withdrawn keys stay in ``data/achievementRoles.json``.  The owner renames "Pełny album" (the bot
    told him he may).  A rollback then deletes those roles and rewrites that file without the album's
    id.  After the upgrade the id is still known - it is kept in a file that version never opens - so no
    second "Pełny album" is made and its holders still count as holding it."""
    bot = game.bot
    saved = {f"old_key_{n}": game.guild.add_role(880_000_000_000_000_000 + n, f"stare osiągnięcie {n}").id
             for n in range(23)}
    saved["pet_mythic"] = game.guild.add_role(881_000_000_000_000_000, "Mityczny towarzysz").id
    bot.store.achievement_roles.save(saved)
    monkeypatch.setattr(game.guild, "chunked", False, raising=False)

    async def chunk():                                                    # (the list stays incomplete)
        return None

    monkeypatch.setattr(game.guild, "chunk", chunk, raising=False)
    await boot(bot)
    role_id = role_of(bot)
    assert role_id is not None and len(what_the_version_before_reads(bot.store.achievement_roles.load())) == 26
    holder = game.add_member("Ala", roles=[PATRON])
    holder.roles.append(game.guild.get_role(role_id))
    game.guild.get_role(role_id).name = "📖 Pełny album"                  # renamed by the owner

    monkeypatch.setattr(game.guild, "chunked", True)
    await roll_back(game)
    assert "album_full" not in str(bot.store.achievement_roles.load())   # (that version lost the id)
    roles = len(game.guild.roles)
    await boot(bot)                                      # upgraded again
    assert role_of(bot) == role_id and len(game.guild.roles) == roles
    assert holds_album_role(bot, holder) and achievements.held(bot, holder) == [achievements.trophy("album_full")]
    made = [line for line in bot.owner_log if line.startswith("Utworzyłem") and "Pełny album" in line]
    assert len(made) == 1                                                 # (made once, at the first start)


async def test_the_album_role_id_is_kept_in_both_files(game):
    """The id of "Pełny album" is written to ``data/achievementRolesLater.json`` (a file the version
    before never opens) and, as a spare, nested in ``data/achievementRoles.json``.  An id saved only in
    the old file (nested or flat) is copied to the new one at the next start."""
    bot = game.bot
    await achievements.ensure_roles(bot)
    role_id = role_of(bot)
    later = bot.store.achievement_roles.path.with_name(achievements.LATER_ROLES_FILE)
    assert json.loads(later.read_text(encoding="utf-8")) == {"album_full": role_id}
    assert bot.store.achievement_roles.load()[achievements.LATER_ROLES_KEY] == {"album_full": role_id}

    for old_file in ({"album_full": role_id}, {achievements.LATER_ROLES_KEY: {"album_full": role_id}}):
        later.unlink()
        bot.store.achievement_roles.save({**bot.store.achievement_roles.load(), **old_file})
        restart(bot)
        roles = len(game.guild.roles)
        await boot(bot)
        assert role_of(bot) == role_id and len(game.guild.roles) == roles
        assert json.loads(later.read_text(encoding="utf-8")) == {"album_full": role_id}
        assert "album_full" not in bot.store.achievement_roles.load()      # (only nested there)


@pytest.mark.parametrize("broken", ["[1, 2, ", '{"album_full": 99', "[1, 2]", "directory"])
async def test_nothing_is_made_or_deleted_while_the_later_roles_file_is_unreadable(game, broken):
    """Without the album's id the bot could make "Pełny album" a second time: it touches no role then."""
    bot = game.bot
    withdrawn = game.guild.add_role(880_000_000_000_000_000, "stare osiągnięcie")
    bot.store.achievement_roles.save({"old_key": withdrawn.id})
    later = bot.store.achievement_roles.path.with_name(achievements.LATER_ROLES_FILE)
    if broken == "directory":
        later.mkdir(parents=True)
    else:
        later.write_text(broken, encoding="utf-8")
    roles = sorted(role.id for role in game.guild.roles)
    await boot(bot)
    assert sorted(role.id for role in game.guild.roles) == roles
    assert [line for line in bot.owner_log if line.startswith("Nie mogę odczytać")] == [
        achievements.LOG_ROLES_FILE_UNREADABLE.format(path=later)]


async def test_an_unknown_id_in_the_later_roles_file_is_kept(game):
    """A key a later version saved there is neither retired nor dropped when this version rewrites the file."""
    bot = game.bot
    future = game.guild.add_role(990_000_000_000_000_600, "Przyszłe osiągnięcie")
    later = bot.store.achievement_roles.path.with_name(achievements.LATER_ROLES_FILE)
    later.parent.mkdir(parents=True, exist_ok=True)
    later.write_text(json.dumps({"future_thing": future.id}), encoding="utf-8")
    await boot(bot)
    assert game.guild.get_role(future.id) is future and role_of(bot) is not None
    assert json.loads(later.read_text(encoding="utf-8")) == {"future_thing": future.id, "album_full": role_of(bot)}


async def test_the_sacrifice_survives_trouble_with_the_achievement(game_db, monkeypatch, sacrifices):
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[PATRON])
    await fill(db, player.id, leave_out={("Cat", 0)})
    await own_pet(db, player.id, 5)
    game_db.guild.create_role_error = RuntimeError("test: no more roles (250)")
    confirms(game_db, player)
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert await album.complete(db, player.id) and await pet_row(db, 5) is None
    assert not holds_album_role(game_db.bot, player)
    assert any(line.startswith("Nie udało się utworzyć ról osiągnięć") for line in game_db.bot.owner_log)
