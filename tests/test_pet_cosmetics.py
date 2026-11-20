"""Pet cosmetics - the seal and Holo: the rolls, where they are kept, the start-up migration, the cards.

The renderer (features/petcards.py) is not tested here: wherever a card would be
composed, ``pets.petcards.card`` is replaced by a stand-in that records how it was
asked and hands back a file (the ``cards`` fixture).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import zipfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import discord
import pytest
import pytest_asyncio

from altermmo import bot as bot_module
from altermmo import schema
from altermmo.cogs import pets as pets_cog
from altermmo.core import guards, ui
from altermmo.features import achievements, backup, boss, hub, patrons, petcards, pets
from altermmo.features.pets import PetType

from fakes import FakeInteraction, FakeMember, FakeRole
from test_pets import add_owner, add_pet, admin, confirms, count, last_embed, owner_row, patron, pet_row, rig, run
from test_ui_pets import rename, show

MIB = 1024 * 1024
#: ``PETS`` as the old bot's database has it: without the two cosmetic columns.
OLD_PETS = pets.SCHEMA[1].replace(
    ",\n           SEAL NUMERIC NOT NULL DEFAULT 0,\n           HOLO BOOLEAN NOT NULL DEFAULT FALSE", "")
#: How the old bot stores a new egg (functions_pets.py): its own column list, no SEAL / HOLO.
OLD_BOT_INSERT = """INSERT INTO PETS
    (PET_ID, PET_NAME, PET_LVL, PET_SKILLS, QUALITY, SHINY, ULTRA_SHINY, MYTHIC, TYPE, VARIANT,
     CRIT_PERC, REPLACE_PERC, DEF_PERC, DROP_PERC, LOWHP_PERC, SLOW_PERC, INIT_PERC, DETECTION) VALUES
    (77, 'Towarzysz', 0, 0, 'Standard', False, False, False, 'Dog', '3', 0, 0, 0, 0, 0, 0, 0, False);"""


# -- fixtures and helpers ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a $potwierdzam slot was never released"


@pytest.fixture
def cards(monkeypatch, tmp_path):
    """Stands in for the renderer: notes every request and "composes" a file named after the look."""
    calls: list[SimpleNamespace] = []

    async def card(source, look, cache_dir, *, max_bytes=petcards.DEFAULT_MAX_BYTES):
        calls.append(SimpleNamespace(source=source, look=look, cache_dir=cache_dir, max_bytes=max_bytes))
        if look.plain:
            return source                                      # as the real one: nothing to compose
        made = tmp_path / (f"card-{source.stem}-{'framed' if look.frame else 'bare'}-{look.seal}"
                           f"{'-holo.gif' if look.holo else '.png'}")
        made.write_bytes(b"a card")
        return made

    monkeypatch.setattr(pets.petcards, "card", card)
    return calls


@pytest_asyncio.fixture
async def cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.pets")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
        channel.permissions_for = lambda member: member.guild_permissions
    guards._last_notice.clear()
    return game_db.bot.get_cog("Pets")


def dice(monkeypatch, *rolls: int, default: int = 999) -> list[tuple]:
    """Script ``random.randrange`` (the cosmetic rolls); returns the list of what was asked.

    The legacy draws (``random.randint`` / ``random.choice``) do not go through it.
    """
    asked: list[tuple] = []
    script = iter(rolls)

    def randrange(*args):
        asked.append(args)
        return next(script, default)

    monkeypatch.setattr(random, "randrange", randrange)
    return asked


def crafter(game=None, name: str = "Crafter") -> FakeMember:
    if game is not None:
        return game.add_member(name, roles=[patrons.CRAFTER_ROLE])
    member = FakeMember(name)
    member.roles.append(FakeRole(patrons.CRAFTER_ROLE))
    return member


async def old_database(db) -> None:
    """Give the test the old bot's ``PETS`` (call it first: before any statement touched the table)."""
    assert "SEAL" not in OLD_PETS and "HOLO" not in OLD_PETS and "MYTHIC" in OLD_PETS
    await db.execute("DROP TABLE PETS", idempotent=True)
    await db.execute(OLD_PETS, idempotent=True)


async def pets_columns(db) -> dict[str, tuple[str, str, str | None]]:
    rows = await db.fetch(
        """SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'pets' ORDER BY ordinal_position""")
    return {row["column_name"]: (row["data_type"], row["is_nullable"], row["column_default"]) for row in rows}


async def cosmetics(db, pet_id: int) -> tuple[int, bool]:
    return pets.cosmetics_of(await pet_row(db, pet_id))


# -- the rolls --------------------------------------------------------------------------

@pytest.mark.parametrize("roll, seal", [(0, 3), (1, 3), (2, 2), (49, 2), (50, 1), (149, 1), (150, 0), (999, 0)])
def test_seal_roll_boundaries(monkeypatch, roll, seal):
    asked = dice(monkeypatch, roll, 500)
    assert pets._roll_cosmetics() == (seal, False)
    assert asked == [(1000,), (1000,)]                       # one roll for the seal, one for Holo


@pytest.mark.parametrize("roll, holo", [(0, True), (1, False), (999, False)])
def test_holo_roll_boundaries(monkeypatch, roll, holo):
    dice(monkeypatch, 500, roll)
    assert pets._roll_cosmetics() == (0, holo)


def test_odds_are_exactly_the_owners_balance(monkeypatch):
    seals: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
    holos = 0
    for roll in range(1000):                                 # every result of each roll, once
        dice(monkeypatch, roll, roll)
        seal, holo = pets._roll_cosmetics()
        seals[seal] += 1
        holos += holo
    assert seals == {0: 850, 1: 100, 2: 48, 3: 2}            # 85% / 10% / 4.8% / 0.2%
    assert holos == 1                                        # 1/1000
    assert set(petcards.SEAL_NAMES) == {1, 2, 3} and pets.MAX_SEAL == 3
    assert (pets.COSMETIC_ROLL, pets.SEAL_ROLL_BELOW, pets.HOLO_ROLL_BELOW) == (1000, ((2, 3), (50, 2), (150, 1)), 1)


def test_the_two_rolls_are_independent(monkeypatch):
    results = set()
    for seal_roll in (0, 10, 100, 500):
        for holo_roll in (0, 1):
            dice(monkeypatch, seal_roll, holo_roll)
            results.add(pets._roll_cosmetics())
    assert results == {(seal, holo) for seal in (3, 2, 1, 0) for holo in (True, False)}


def test_only_supporters_roll_and_only_once(monkeypatch):
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 0, 0)
    for nobody in (FakeMember("Ala"), None):
        egg = pets._roll_egg(nobody)
        assert (egg["SEAL"], egg["HOLO"]) == (0, False)
    assert asked == []                                       # nothing was rolled for them

    egg = pets._roll_egg(crafter())
    assert (egg["SEAL"], egg["HOLO"]) == (3, True) and asked == [(1000,), (1000,)]

    # Several roles do not multiply the tries.
    rich = crafter()
    rich.roles += [FakeRole(role) for role in patrons.PATRON_ROLES]
    asked = dice(monkeypatch, 60, 5)
    egg = pets._roll_egg(rich)
    assert (egg["SEAL"], egg["HOLO"]) == (1, False) and asked == [(1000,), (1000,)]


@pytest.mark.parametrize("egg_roll, shiny, ultra, mythic", [
    (0, False, False, False), (905, True, False, False), (985, True, True, False), (1000, True, True, True)])
def test_rolls_do_not_depend_on_the_class_of_the_pet(monkeypatch, egg_roll, shiny, ultra, mythic):
    for seal_roll, holo_roll, expected in ((0, 0, (3, True)), (20, 7, (2, False)), (700, 0, (0, True))):
        pools, ranges = rig(monkeypatch, randint=lambda low, high: egg_roll if high == 1000 else 0)
        asked = dice(monkeypatch, seal_roll, holo_roll)
        egg = pets._roll_egg(crafter())
        assert (egg["SHINY"], egg["ULTRA_SHINY"], egg["MYTHIC"]) == (shiny, ultra, mythic)
        assert (egg["SEAL"], egg["HOLO"]) == expected and len(asked) == 2      # a mythic egg rolls too
        # The legacy draws are what they were: the cosmetic rolls come after them, from another die.
        assert ranges[0] == (0, 1000) and pools == [list(pets.MYTHIC_EGGS if mythic else pets.PATRON_EGGS)]


def test_nothing_is_rolled_without_the_columns(monkeypatch):
    asked = dice(monkeypatch, 0, 0)
    egg = pets._roll_egg(crafter(), cosmetics=False)
    assert "SEAL" not in egg and "HOLO" not in egg and asked == []
    assert set(egg) == {"TYPE", "QUALITY", "VARIANT", "SHINY", "ULTRA_SHINY", "MYTHIC"}


# -- storing the egg --------------------------------------------------------------------

async def test_supporters_egg_is_stored_with_its_cosmetics(game_db, monkeypatch):
    db, bot = game_db.bot.db, game_db.bot
    rig(monkeypatch, randint=0)
    lucky, plain = crafter(game_db), game_db.add_member("Ala")
    asked = dice(monkeypatch, 30, 400)                       # Łowcy, no Holo
    assert await pets.assign_pet(game_db.scene(), lucky) is True
    assert await cosmetics(db, 1) == (2, False)
    assert bot.owner_log == []                               # not rare enough to tell the owner

    assert await pets.assign_pet(game_db.scene(), plain) is True
    assert await cosmetics(db, 2) == (0, False) and len(asked) == 2            # no roll for a non-supporter
    row = await pet_row(db, 2)
    assert row["seal"] == 0 and row["holo"] is False

    # A supporter who already has a pet gets no egg - and no roll.
    assert await pets.assign_pet(game_db.scene(), lucky) is False
    assert len(asked) == 2


@pytest.mark.parametrize("seal_roll, holo_roll, mythic, stored, told, achieved", [
    (1, 500, False, (3, False), "pieczęć Arcyłowcy", []),
    (500, 0, False, (0, True), "folię Holo", []),
    (0, 0, False, (3, True), "pieczęć Arcyłowcy i folię Holo", []),
    (1, 500, True, (3, False), "pieczęć Arcyłowcy", ["Mityczny towarzysz", "Mityczna Pieczęć Arcyłowcy"]),
    (500, 0, True, (0, True), "folię Holo", ["Mityczny towarzysz", "Mityczny Holo"]),
    (0, 0, True, (3, True), "pieczęć Arcyłowcy i folię Holo",
     ["Mityczny towarzysz", "Mityczny Holo", "Mityczna Pieczęć Arcyłowcy"])])
async def test_owner_hears_about_rare_cosmetics_and_nobody_else(game_db, monkeypatch, caplog, seal_roll,
                                                                holo_roll, mythic, stored, told, achieved):
    db, bot = game_db.bot.db, game_db.bot
    await achievements.ensure_roles(bot)                   # as after a start: the achievement roles exist
    bot.owner_log.clear()
    game_db.log_channel.messages.clear()
    rig(monkeypatch, randint=lambda low, high: 1000 if mythic and high == 1000 else 0)
    dice(monkeypatch, seal_roll, holo_roll)
    lucky = crafter(game_db)
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        assert await pets.assign_pet(game_db.scene(), lucky) is True
    assert await cosmetics(db, 1) == stored
    assert bot.owner_log == [f"Rzadka kosmetyka towarzysza: jajko gracza <@{lucky.id}> (PET_ID 1) ma {told}."]
    assert game_db.log_channel.messages[-1].content == bot.owner_log[0]        # in the log channel, no ping
    assert f"<@{bot.ids.owner}>" not in game_db.log_channel.messages[-1].content
    # The notice itself is never public.  Since the evening of 2026-11-20 only a MYTHIC egg is an
    # achievement (with Holo / the rarest seal one more each), announced after the mythic egg itself;
    # a Holo or a seal on any other egg is no achievement - and nothing else is posted.
    public = [message.content for message in game_db.channel.bot_messages[1:]]
    mythic_news = [text for text in public if "zdobył mitycznego towarzysza" in text]
    assert len(mythic_news) == int(mythic)
    assert [text for text in public if text not in mythic_news] == [
        f"<@{lucky.id}> zdobywa osiągnięcie: {name}!" for name in achieved]
    assert all(told not in message.text for message in game_db.channel.sent)
    assert any("rare cosmetics" in record.message for record in caplog.records)


async def test_generated_egg_rolls_as_for_the_owner(game_db, monkeypatch):
    db, bot = game_db.bot.db, game_db.bot
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 0, 0)
    assert await pets.generate_pet_egg(game_db.scene()) == 1                   # the owner is not on the server
    assert await cosmetics(db, 1) == (0, False) and asked == []
    game_db.add_member("Alter", roles=[patrons.PATRON_ROLES[0]], member_id=bot.ids.owner)
    assert await pets.generate_pet_egg(game_db.scene()) == 2
    assert await cosmetics(db, 2) == (3, True)
    assert bot.owner_log == [f"Rzadka kosmetyka towarzysza: jajko gracza <@{bot.ids.owner}> (PET_ID 2) ma "
                             "pieczęć Arcyłowcy i folię Holo."]


async def test_the_columns_are_looked_up_once_per_run(game_db, monkeypatch):
    bot = game_db.bot
    lookups = []
    real = bot.db.fetchval

    async def fetchval(query, *args, **kwargs):
        if "pg_attribute" in query:
            lookups.append(args)
        return await real(query, *args, **kwargs)

    monkeypatch.setattr(bot.db, "fetchval", fetchval)
    assert await pets.cosmetics_available(bot) is True
    for name in ("Ala", "Ola", "Ela"):
        assert await pets.assign_pet(game_db.scene(), game_db.add_member(name)) is True
    assert await pets.cosmetics_available(bot, refresh=True) is True           # a "yes" is never asked again
    assert lookups == [(["seal", "holo"],)]


# -- cosmetics are for the pet's whole life ----------------------------------------------

async def test_cosmetics_survive_everything_that_happens_to_a_pet(game_db, monkeypatch, cards):
    db, bot = game_db.bot.db, game_db.bot
    player, heir = patron(game_db), game_db.add_member("Ola")
    await add_owner(db, player.id, 5, reroll_scroll=3, rebirth_stones=1, mirrors=1)
    await add_pet(db, 5, variant="7", seal=2, holo=True)                       # an egg
    rig(monkeypatch, randint=lambda low, high: high)
    asked = dice(monkeypatch, 0, 0)                                            # would change them, if rolled
    confirms(game_db, player)
    scene = game_db.scene(player)

    for level in (1, 2, 3):                                                    # evolution 0 -> 1 -> 2 -> 3
        assert await pets.level_up_pet(scene, player.id) is True
        row = await pet_row(db, 5)
        assert row["pet_lvl"] == level and pets.cosmetics_of(row) == (2, True)
    await db.execute("UPDATE PETS SET PET_SKILLS = 4 WHERE PET_ID = 5")        # leaves room for an enlightenment

    assert await pets.reroll_pet(scene, player) is True                        # $odrodzenie
    assert await cosmetics(db, 5) == (2, True)
    assert await pets.enlight_pet(scene, player) is True                       # $oswiecenie
    assert await cosmetics(db, 5) == (2, True)
    before = await pet_row(db, 5)
    rig(monkeypatch, choices=[PetType.DRAGON], randint=lambda low, high: high)
    assert await pets.transform_pet(scene, player) is True                     # the mirror
    row = await pet_row(db, 5)
    assert (before["type"], row["type"]) == ("Cat", "Dragon") and pets.cosmetics_of(row) == (2, True)
    assert await pets.name_pet(scene, "Reksio") is True                        # a new name; it turns shiny
    row = await pet_row(db, 5)
    assert row["pet_name"] == "Reksio" and row["shiny"] is True and pets.cosmetics_of(row) == (2, True)

    assert await pets.store_pet(scene, 2) is True                              # into the stable ...
    assert (await owner_row(db, player.id))["pet_id_alt2"] == 5 and await cosmetics(db, 5) == (2, True)
    assert await pets.unstore_pet(scene, 2) is True                            # ... and out again
    assert (await owner_row(db, player.id))["pet_id"] == 5 and await cosmetics(db, 5) == (2, True)

    # A new owner - one without any rank: the specimen keeps what it has (it is only not shown
    # to him, see test_cosmetics_are_shown_only_while_the_owner_supports).
    await db.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", player.id)
    assert await pets.reassign_pet(bot, 5, heir.id) is True
    assert await cosmetics(db, 5) == (2, True)
    cards.clear()
    assert await pets.show_pet(game_db.scene(heir), heir) is True
    assert await cosmetics(db, 5) == (2, True)
    heir.roles.append(FakeRole(patrons.TWITCH_SUB_ROLE))                       # the heir starts to support
    assert await pets.show_pet(game_db.scene(heir), heir) is True
    assert (cards[-1].look.seal, cards[-1].look.holo) == (2, True)
    assert "Pieczęć: Łowcy\nFolia: Holo\n" in last_embed(game_db).description

    # The end of the support changes nothing in the database either: the first owner lost the rank.
    rank = list(player.roles)
    player.roles.clear()
    await db.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", heir.id)
    assert await pets.reassign_pet(bot, 5, player.id) is True
    assert await pets.show_pet(scene, player) is True
    assert await cosmetics(db, 5) == (2, True)
    player.roles.extend(rank)                                                  # ... and it is all back with the rank
    assert await pets.show_pet(scene, player) is True
    assert (cards[-1].look.seal, cards[-1].look.holo) == (2, True)
    assert asked == []                                                         # nothing was ever rolled again


def test_only_the_admin_statement_writes_cosmetics():
    """Every UPDATE / INSERT of PETS in the bot's code: SEAL and HOLO appear in ``set_cosmetics`` alone.

    (The INSERT of a new egg adds them through its ``{columns}`` placeholder.)
    """
    writers = []
    statements = 0
    for path in sorted(Path(pets.__file__).parents[1].rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'(UPDATE\s+PETS\s+SET|INSERT\s+INTO\s+PETS\b)[^"]*', source):
            statements += 1
            if re.search(r"\b(SEAL|HOLO)\b", match.group(0)):
                writers.append((path.name, match.group(1).split()[0]))
    assert statements >= 7 and writers == [("pets.py", "UPDATE")]
    assert "{columns}" in pets._SQL_INSERT_EGG and "SEAL" not in pets._SQL_INSERT_EGG
    assert "SEAL" not in pets._SQL_SAVE_PET.upper().replace("SLOW_PERC", "") and "HOLO" not in pets._SQL_SAVE_PET


# -- reading rows with and without the columns --------------------------------------------

def test_rows_without_the_columns_read_as_no_cosmetics():
    assert pets.cosmetics_of({"pet_id": 1}) == (0, False)                      # the old bot's table
    assert pets.cosmetics_of({"seal": None, "holo": None}) == (0, False)
    assert pets.cosmetics_of({"seal": Decimal("2"), "holo": True}) == (2, True)
    assert pets.cosmetics_of({"seal": 3, "holo": False}) == (3, False)
    assert pets.cosmetics_of({"seal": 7, "holo": True}) == (0, True)            # a tier nobody knows
    assert pets.cosmetics_of({"seal": -1}) == (0, False)
    assert pets.cosmetics_of({"seal": "x", "holo": "tak"}) == (0, False)


ITEMS = dict(reroll_scroll=0, reroll_scroll_shard=0, rebirth_stones=0, mirrors=0)


def grown_row(**columns) -> dict:
    return {"pet_id": 5, "pet_name": "Burek", "pet_lvl": 2, "pet_skills": 3, "quality": "Standard",
            "shiny": False, "type": "Cat", "variant": "1", "crit_perc": 4, "replace_perc": 0, "def_perc": 0,
            "drop_perc": 0, "lowhp_perc": 0, "slow_perc": 0, "init_perc": 0, "detection": False,
            "ultra_shiny": False, "mythic": False, **columns}


@pytest.mark.parametrize("columns, lines", [
    (dict(seal=1, holo=False), "Pieczęć: Tropiciela\n"),
    (dict(seal=2, holo=False), "Pieczęć: Łowcy\n"),
    (dict(seal=3, holo=True), "Pieczęć: Arcyłowcy\nFolia: Holo\n"),
    (dict(seal=0, holo=True), "Folia: Holo\n"),
    (dict(seal=0, holo=False), ""),
    (dict(), ""),                                                              # a row of the old table
    (dict(seal=None, holo=None), "")])
def test_embed_names_the_cosmetics_of_a_hatched_pet(columns, lines):
    embed = pets._pet_embed(1, grown_row(**columns), ITEMS)
    assert embed.description == (
        "Oto Twój towarzysz, <@1>.\n\n📜 **STATYSTYKI:**\nPoziom: 2\nTalent: Przeciętny\nWygląd: Standard\n"
        + lines + "\nSzansa na krytyczne uderzenie: 4 %\n"
        "\n**💎 PRZEDMIOTY:**\nZwoje odrodzenia: 0\nFragmenty zwojów: 0\nKamienie olśnienia: 0\nLustra wizualiów: 0")


def test_egg_names_its_cosmetics_too():
    """Owner's decision: the seal and the Holo are visible from the egg on."""
    plain = pets._pet_embed(1, grown_row(pet_lvl=0), ITEMS)
    rich = pets._pet_embed(1, grown_row(pet_lvl=0, seal=3, holo=True), ITEMS)
    assert rich.title == plain.title == "Jajko Burek"
    assert "Pieczęć" not in plain.description and "Folia" not in plain.description
    assert "wyrośnie...\n\n**💎 PRZEDMIOTY:**" in plain.description            # an egg without them: as it was
    assert "wyrośnie...\n\nPieczęć: Arcyłowcy\nFolia: Holo\n\n**💎 PRZEDMIOTY:**" in rich.description
    assert rich.description.replace("\nPieczęć: Arcyłowcy\nFolia: Holo\n", "") == plain.description
    # The owner does not support right now: one line says why they are gone.
    hidden = pets._pet_embed(1, grown_row(pet_lvl=0, seal=3, holo=True), ITEMS, cosmetics=False)
    assert "Pieczęć:" not in hidden.description and "Folia: Holo" not in hidden.description
    assert f"wyrośnie...\n\n{pets.MSG_COSMETICS_HIDDEN}\n\n**💎 PRZEDMIOTY:**" in hidden.description


# -- the card that is sent --------------------------------------------------------------

async def test_hatched_pet_is_shown_as_its_card(game_db, cards):
    db, bot = game_db.bot.db, game_db.bot
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, variant="1", type="Cat", seal=2, holo=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert message.files == ["card-11-bare-2-holo.gif"]                        # the card, not the illustration
    assert message.embeds[0].image.url == "attachment://card-11-bare-2-holo.gif"
    assert message.view.image.name == "card-11-bare-2-holo.gif"                # the panel remembers what it sent
    assert "Wygląd: Standard\nPieczęć: Łowcy\nFolia: Holo\n\n" in message.embeds[0].description

    [asked] = cards
    assert asked.source == pets.ASSETS_DIR / "pets" / "Cat" / "11.png"         # the plain illustration
    assert asked.look == petcards.look_of(await pet_row(db, 5))
    assert (asked.look.seal, asked.look.holo, asked.look.plain) == (2, True, False)
    assert asked.cache_dir == bot.store.data_dir / "cache" / "petcards"
    assert asked.max_bytes == int(10 * MIB * 0.95)                             # 10 MiB less 5%

    # Without the panel (buttons=False) the same file goes out.
    assert await pets.show_pet(game_db.scene(player), player, buttons=False) is True
    assert game_db.channel.messages[-1].files == ["card-11-bare-2-holo.gif"]
    assert game_db.channel.messages[-1].view is None


async def test_real_renderer_and_the_pet_code_fit_together(game_db):
    """No stand-in here: a still card (a frame and a seal) composed by features/petcards.py itself."""
    pytest.importorskip("PIL")
    db, bot = game_db.bot.db, game_db.bot
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, variant="1", type="Cat", shiny=True, seal=2)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    sent = message.view.image
    assert sent.parent == bot.store.data_dir / "cache" / "petcards" and sent.is_file()
    assert re.fullmatch(r"card-Cat-11-[A-Za-z0-9_-]+\.png", sent.name)          # a name Discord takes as it is
    assert message.files == [sent.name] and message.embeds[0].image.url == f"attachment://{sent.name}"
    assert sent.read_bytes() != (pets.ASSETS_DIR / "pets" / "Cat" / "11.png").read_bytes()
    # The same pet again: the same file out of the cache; the panel has nothing new to send.
    assert await message.view.redraw(bot) is True and "attachments" not in message.edits[-1]
    # An egg and an ordinary pet without cosmetics never get into the cache.
    await db.execute("UPDATE PETS SET SHINY = FALSE, SEAL = 0")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["11.png"]
    assert [path.name for path in sent.parent.iterdir()] == [sent.name]


async def test_card_may_be_as_big_as_the_servers_upload_limit_less_a_margin(game_db, cards):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=1, shiny=True)
    game_db.guild.filesize_limit = 25 * MIB                                    # a boosted server
    await pets.show_pet(game_db.scene(player), player)
    assert cards[-1].max_bytes == int(25 * MIB * 0.95)
    game_db.guild.filesize_limit = 0                                           # unknown: Discord's 10 MiB
    await pets.show_pet(game_db.scene(player), player)
    assert cards[-1].max_bytes == int(10 * MIB * 0.95)
    assert pets._card_max_bytes(None) == int(10 * MIB * 0.95)
    assert pets.DEFAULT_UPLOAD_LIMIT == boss.DEFAULT_UPLOAD_LIMIT              # the repeated constant


@pytest.mark.parametrize("flags, framed", [
    (dict(), False), (dict(shiny=True), True), (dict(shiny=True, ultra_shiny=True), True),
    (dict(shiny=True, ultra_shiny=True, mythic=True, quality="Mythic", type="Void", variant="0"), False)])
async def test_frame_follows_the_class_of_the_pet(game_db, cards, flags, framed):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, **{"pet_lvl": 1, "variant": "1", **flags})
    assert await pets.show_pet(game_db.scene(player), player) is True
    message, look = game_db.channel.messages[-1], cards[-1].look
    assert bool(look.frame) is framed and look.plain is not framed             # mythic: its own GIF, no frame
    source = cards[-1].source.name
    assert message.files == (["card-1-framed-0.png"] if framed else [source])
    assert message.embeds[0].image.url == f"attachment://{message.files[0]}"


async def test_egg_is_a_card_like_a_hatched_pet(game_db, cards):
    """Frame, seal and Holo are visible from the egg on (owner's decision)."""
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, variant="7", shiny=True, ultra_shiny=True, seal=3, holo=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert cards[-1].source == pets.ASSETS_DIR / "eggs" / "standard" / "7.png"
    assert cards[-1].look == petcards.Look(frame="ultra", seal=3, holo=True)   # the frame of its class too
    assert message.files == ["card-7-framed-3-holo.gif"]
    assert message.embeds[0].image.url == "attachment://card-7-framed-3-holo.gif"
    assert message.embeds[0].title == "Jajko"
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in message.embeds[0].description

    # Hatching changes the illustration, not the look.
    assert await pets.level_up_pet(game_db.scene(player), player.id) is True
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert cards[-1].look == petcards.Look(frame="ultra", seal=3, holo=True)
    assert cards[-1].source == pets.ASSETS_DIR / "pets" / "Cat" / "1.png"

    # A shiny egg of somebody who does not support: the frame alone; a mythic egg: its own GIF, no frame.
    stranger = game_db.add_member("Ola")
    await add_owner(db, stranger.id, 6)
    await add_pet(db, 6, variant="3", shiny=True, seal=1)
    assert await pets.show_pet(game_db.scene(stranger), stranger) is True
    assert cards[-1].look == petcards.Look(frame="shiny")
    assert game_db.channel.messages[-1].files == ["card-3-framed-0.png"]
    await db.execute("UPDATE PETS SET QUALITY = 'Mythic', TYPE = 'Void', VARIANT = '0', MYTHIC = TRUE, "
                     "ULTRA_SHINY = TRUE WHERE PET_ID = 6")
    assert await pets.show_pet(game_db.scene(stranger), stranger) is True
    assert cards[-1].look.plain and game_db.channel.messages[-1].files == ["0.gif"]


async def test_ordinary_egg_is_shown_exactly_as_before(game_db, cards):
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, variant="7")                                          # no class, no seal, no Holo
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert message.files == ["7.png"] and message.embeds[0].image.url == "attachment://7.png"
    assert [call.look.plain for call in cards] == [True]                       # nothing is composed
    assert message.embeds[0].title == "Jajko"
    assert "Pieczęć" not in message.embeds[0].description and "Folia" not in message.embeds[0].description


async def test_eggs_cosmetics_are_hidden_while_the_owner_does_not_support(game_db, cards):
    db = game_db.bot.db
    player = game_db.add_member("Ala")                                         # no supporter role
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, variant="7", seal=2, holo=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert message.files == ["7.png"] and cards[-1].look.plain                 # the plain egg
    assert pets.MSG_COSMETICS_HIDDEN in message.embeds[0].description
    player.roles.append(FakeRole(patrons.PATRON_ROLES[2]))                     # supports: there they are
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["card-7-bare-2-holo.gif"]
    assert "Pieczęć: Łowcy\nFolia: Holo\n" in last_embed(game_db).description


async def test_plain_picture_is_sent_when_there_is_no_card(game_db, monkeypatch, tmp_path):
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=3, variant="1", type="Dog", shiny=True, seal=1)

    async def source_again(source, look, cache_dir, *, max_bytes):            # how the renderer reports a failure
        return source

    async def broken(source, look, cache_dir, *, max_bytes):                  # it promised never to raise; if it does
        raise RuntimeError("no Pillow today")

    async def nowhere(source, look, cache_dir, *, max_bytes):                 # a card that is not on the disk
        return tmp_path / "missing.png"

    async def nothing(source, look, cache_dir, *, max_bytes):
        return None

    for renderer in (source_again, broken, nowhere, nothing):
        monkeypatch.setattr(pets.petcards, "card", renderer)
        assert await pets.show_pet(game_db.scene(player), player) is True
        message = game_db.channel.messages[-1]
        assert message.files == ["101.png"] and message.embeds[0].image.url == "attachment://101.png"
        assert "Pieczęć: Tropiciela\n" in message.embeds[0].description       # the words stay

    # A pet without a picture has nothing to compose a card from.
    calls = []
    monkeypatch.setattr(pets.petcards, "card", lambda *args, **kwargs: calls.append(args))
    await db.execute("UPDATE PETS SET TYPE = 'Hydra'")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == [] and calls == []


async def test_panel_gets_a_new_card_when_the_look_changes(game_db, monkeypatch, cards):
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=2, variant="1", seal=1)
    panel = await show(game_db, player)
    assert panel.files == ["card-11-bare-1.png"] and panel.embeds[0].image.url == "attachment://card-11-bare-1.png"

    rig(monkeypatch, randint=0)                                                # the name does not make it shiny
    await rename(game_db, panel, player, "Reksio")
    assert "attachments" not in panel.edits[-1]                                # the same card: not sent again
    assert panel.embeds[0].title == "Kot Reksio"
    assert panel.embeds[0].image.url == "attachment://card-11-bare-1.png"

    rig(monkeypatch, randint=100)                                              # this one does: a frame appears
    await rename(game_db, panel, player, "Azor")
    assert (await pet_row(db, 5))["shiny"] is True
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["card-11-framed-1.png"]
    assert panel.embeds[0].image.url == "attachment://card-11-framed-1.png"
    assert panel.view.image.name == "card-11-framed-1.png"

    await db.execute("UPDATE PETS SET PET_LVL = 3")                            # it grew: another illustration
    assert await panel.view.redraw(game_db.bot) is True
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["card-101-framed-1.png"]
    assert cards[-1].max_bytes == int(10 * MIB * 0.95) and cards[-1].look.seal == 1


# -- shown only while the owner supports (owner's decision of 2026-11-20) -----------------

SUPPORT_LOST_LINE = "*Ozdoby karty (pieczęć, folia Holo) widać tylko u wspierających.*"


@pytest.mark.parametrize("role", [patrons.PATRON_ROLES[0], patrons.CRAFTER_ROLE, patrons.TWITCH_SUB_ROLE,
                                  *patrons.YOUTUBE_ROLES])
async def test_cosmetics_are_shown_only_while_the_owner_supports(game_db, cards, role):
    """Losing the rank hides the seal and the Holo (card and words); getting it back shows them again."""
    db = game_db.bot.db
    player = game_db.add_member("Ala", roles=[role])
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=2, variant="1", type="Cat", shiny=True, seal=3, holo=True)
    scene = game_db.scene(player)

    assert await pets.show_pet(scene, player) is True                          # supports: everything is there
    shown = game_db.channel.messages[-1]
    assert shown.files == ["card-11-framed-3-holo.gif"]
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in shown.embeds[0].description
    assert SUPPORT_LOST_LINE not in shown.embeds[0].description

    player.roles.clear()                                                       # the support ended
    assert await pets.show_pet(scene, player) is True
    hidden = game_db.channel.messages[-1]
    assert cards[-1].look == petcards.Look(frame="shiny")                      # the frame is the pet's class: it stays
    assert hidden.files == ["card-11-framed-0.png"]
    assert hidden.embeds[0].image.url == "attachment://card-11-framed-0.png"
    description = hidden.embeds[0].description
    assert "Pieczęć" not in description.replace(SUPPORT_LOST_LINE, "") and "Folia: Holo" not in description
    assert f"Wygląd: Standard\n{SUPPORT_LOST_LINE}\n\n" in description         # one line says why
    assert await cosmetics(db, 5) == (3, True)                                 # nothing was taken away

    player.roles.append(FakeRole(role))                                        # supports again: it is all back
    assert await pets.show_pet(scene, player) is True
    assert game_db.channel.messages[-1].files == ["card-11-framed-3-holo.gif"]
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in last_embed(game_db).description


async def test_pet_without_cosmetics_of_a_non_supporter_looks_as_always(game_db, cards):
    """Nothing to hide: no line about supporters, and an ordinary pet stays the plain picture."""
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_lvl=1, variant="1", type="Cat", ultra_shiny=True, shiny=True)
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert cards[-1].look == petcards.Look(frame="ultra")                      # frames are for everybody
    assert "wspierających" not in last_embed(game_db).description
    await db.execute("UPDATE PETS SET SHINY = FALSE, ULTRA_SHINY = FALSE, SEAL = 2")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["1.png"] and cards[-1].look.plain   # hidden seal: plain picture
    # The same goes for an egg: hidden for him, with the line that says why.
    await db.execute("UPDATE PETS SET PET_LVL = 0, HOLO = TRUE")
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert cards[-1].look.plain and pets.MSG_COSMETICS_HIDDEN in last_embed(game_db).description


async def test_panel_follows_the_support_when_it_is_redrawn(game_db, monkeypatch, cards):
    """The panel asks again at every redraw: a rename after the rank was lost shows the bare card."""
    db = game_db.bot.db
    player = crafter(game_db, "Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=2, variant="1", seal=1, holo=True)
    panel = await show(game_db, player)
    assert panel.files == ["card-11-bare-1-holo.gif"]

    player.roles.clear()
    rig(monkeypatch, randint=0)                                                # the name does not make it shiny
    await rename(game_db, panel, player, "Reksio")
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["11.png"]   # the plain illustration
    assert panel.embeds[0].image.url == "attachment://11.png" and panel.view.image.name == "11.png"
    assert SUPPORT_LOST_LINE in panel.embeds[0].description

    player.roles.append(FakeRole(patrons.TWITCH_SUB_ROLE))
    assert await panel.view.redraw(game_db.bot) is True
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["card-11-bare-1-holo.gif"]
    assert "Pieczęć: Tropiciela\nFolia: Holo\n" in panel.embeds[0].description


async def test_owner_given_as_an_id_is_looked_up(game_db, monkeypatch, cards):
    """A bare id (no roles to read): the member is looked up; a stranger or a failed lookup hides the cosmetics."""
    db, bot = game_db.bot.db, game_db.bot
    supporter, plain = crafter(game_db, "Ala"), game_db.add_member("Ola")
    for owner_id, pet_id in ((supporter.id, 5), (plain.id, 6), (424242, 7)):
        await add_owner(db, owner_id, pet_id)
        await add_pet(db, pet_id, pet_lvl=1, variant="1", seal=2)
    assert await pets._is_supporter(bot, supporter.id) is True
    assert await pets._is_supporter(bot, plain.id) is False
    assert await pets._is_supporter(bot, 424242) is False                      # left the server

    scene = game_db.scene(plain)
    assert await pets.show_pet(scene, supporter.id, buttons=False) is True
    assert cards[-1].look.seal == 2 and "Pieczęć: Łowcy\n" in last_embed(game_db).description
    assert await pets.show_pet(scene, 424242, buttons=False) is True
    assert cards[-1].look.plain and SUPPORT_LOST_LINE in last_embed(game_db).description

    async def broken(user_id):
        raise RuntimeError("Discord is down")

    monkeypatch.setattr(bot, "member", broken)
    assert await pets._is_supporter(bot, supporter.id) is False                # decoration: never an error
    assert await pets.show_pet(scene, supporter.id, buttons=False) is True     # the pet is shown in any case
    assert cards[-1].look.plain

    # A pet with nothing to show costs no lookup at all.
    await db.execute("UPDATE PETS SET SEAL = 0")
    lookups = []
    monkeypatch.setattr(bot, "member", lambda user_id: lookups.append(user_id))
    assert await pets.show_pet(scene, supporter.id, buttons=False) is True and lookups == []


async def test_click_is_acknowledged_before_the_card_is_composed(game_db, monkeypatch, cards):
    """A cold Holo card can take seconds; Discord gives a click three."""
    db = game_db.bot.db
    player = patron(game_db)
    await add_owner(db, player.id, 5, mirrors=1)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=1, variant="1", holo=True)
    events: list[str] = []
    real_card, real_ack = pets.petcards.card, ui.ack

    async def card(*args, **kwargs):
        events.append("card")
        return await real_card(*args, **kwargs)

    async def ack(interaction):
        events.append("ack")
        return await real_ack(interaction)

    monkeypatch.setattr(pets.petcards, "card", card)
    monkeypatch.setattr(ui, "ack", ack)

    # The "Towarzysz" button of the pinned panel.
    clicked = FakeInteraction(game_db.bot, player, channel=game_db.channel)
    assert await hub.show_display(clicked, hub.show_pet) is True
    panel = game_db.channel.messages[-1]
    assert events == ["ack", "card"] and clicked.response.deferred
    assert panel.files == ["card-1-bare-0-holo.gif"]

    # A gameplay button under the pet: the flow shows the pet anew.
    events.clear()
    rig(monkeypatch, choices=[PetType.DOG], randint=0)
    confirms(game_db, player)
    await game_db.click(panel, player, "pet:transform")
    assert events[0] == "ack" and events[-1] == "card"
    assert game_db.channel.messages[-1].files == ["card-0-bare-0-holo.gif"]

    # The rename window: the panel is redrawn after the submitted window was acknowledged.
    events.clear()
    panel = game_db.channel.messages[-1]
    rig(monkeypatch, randint=100)
    await rename(game_db, panel, player, "Azor")
    assert events == ["ack", "card"]


# -- the start-up migration -------------------------------------------------------------

async def test_migration_adds_the_columns_to_the_old_table(db, monkeypatch):
    await old_database(db)
    await add_pet(db, 5, pet_lvl=2, shiny=True)
    await add_pet(db, 6)
    before = await pets_columns(db)
    assert "seal" not in before and "holo" not in before
    # Only the two columns: every other table (ACHIEVEMENT_COUNTERS too) is made by create_all.
    assert await schema.verify(db) == ["pets: brak kolumny seal", "pets: brak kolumny holo"]

    assert await schema.add_new_columns(db) == []
    columns = await pets_columns(db)
    assert list(columns) == [*before, "seal", "holo"]                          # appended, nothing else touched
    assert {name: columns[name] for name in before} == before
    assert columns["seal"] == ("numeric", "NO", "0") and columns["holo"] == ("boolean", "NO", "false")
    for pet_id in (5, 6):                                                      # the pets that were there
        row = await pet_row(db, pet_id)
        assert row["seal"] == 0 and row["holo"] is False
    assert (await pet_row(db, 5))["shiny"] is True and await count(db, "PETS") == 2
    assert await schema.verify(db) == []

    # Every later start: nothing is altered any more (not even a lock on the table is asked for).
    transactions = []
    real = db.transaction
    monkeypatch.setattr(db, "transaction", lambda *args, **kwargs: transactions.append(1) or real(*args, **kwargs))
    assert await schema.add_new_columns(db) == [] and await schema.add_new_columns(db) == []
    assert transactions == [] and await pets_columns(db) == columns

    # The old bot's INSERT (its own column list) still works; its pet has no cosmetics.
    await db.execute(OLD_BOT_INSERT)
    assert await cosmetics(db, 77) == (0, False)


async def test_migration_is_what_the_feature_modules_declare(db):
    assert schema.new_columns() == [("pets", "seal", "NUMERIC NOT NULL DEFAULT 0"),
                                    ("pets", "holo", "BOOLEAN NOT NULL DEFAULT FALSE")]
    assert pets.COSMETIC_COLUMNS == ("seal", "holo")
    assert schema.EXPECTED_COLUMNS["pets"]["seal"] == "numeric"
    assert schema.EXPECTED_COLUMNS["pets"]["holo"] == "boolean"
    assert await schema.add_new_columns(db) == []                              # a fresh database: nothing to add
    await db.execute("DROP TABLE PETS", idempotent=True)                       # no table at all: verify's business
    assert await schema.add_new_columns(db) == []
    assert "brak tabeli pets" in await schema.verify(db)


async def test_migration_gives_up_on_a_busy_table_instead_of_hanging(db, monkeypatch):
    await old_database(db)
    await add_pet(db, 5)
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    async with db.transaction() as somebody:                                   # a long transaction of somebody else
        await somebody.execute("LOCK TABLE PETS IN ACCESS SHARE MODE")
        started = time.monotonic()
        problems = await schema.add_new_columns(db)
        assert time.monotonic() - started < 5
    assert len(problems) == 1 and problems[0].startswith("pets (seal, holo): LockNotAvailableError")
    assert "seal" not in await pets_columns(db) and await count(db, "PETS") == 1
    assert await schema.add_new_columns(db) == []                              # the next start: the table is free
    assert await cosmetics(db, 5) == (0, False)


async def test_migration_adds_all_columns_of_a_table_or_none(db, monkeypatch):
    await old_database(db)
    monkeypatch.setattr(pets, "NEW_COLUMNS", (*pets.NEW_COLUMNS[:1], ("pets", "holo", "NO SUCH TYPE")))
    problems = await schema.add_new_columns(db)
    assert len(problems) == 1 and "pets (seal, holo)" in problems[0]
    assert "seal" not in await pets_columns(db)                                # rolled back with the failed one


async def test_migration_reports_an_unreachable_database_instead_of_raising():
    class Down:
        async def fetch(self, *args, **kwargs):
            raise RuntimeError("database is down")

    assert await schema.add_new_columns(Down()) == ["RuntimeError: database is down"]


async def test_start_up_adds_the_columns_or_tells_the_owner_once(game_db, monkeypatch):
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(schema, "NEW_COLUMN_LOCK_TIMEOUT", "200ms")
    await old_database(db)

    async with db.transaction() as somebody:
        await somebody.execute("LOCK TABLE PETS IN ACCESS SHARE MODE")
        await bot.setup_hook()                                                 # starts all the same
    [note] = bot._startup_notes                                                # one note, saying what it means
    problem = note.removeprefix("Nie udało się dodać do bazy danych kolumn nowych w v2: ").split(". Kosmetyka")[0]
    assert problem.startswith("pets (seal, holo): LockNotAvailableError")
    assert note == bot_module.MSG_NEW_COLUMNS_FAILED.format(problems=problem)
    assert "Kosmetyka towarzyszy (pieczęcie, folia Holo) jest WYŁĄCZONA" in note
    assert "Baza danych różni się" not in note                                 # not the alarm for a broken database
    assert await pets.cosmetics_available(bot) is False

    await bot.on_ready()
    told = game_db.log_channel.messages[-1].content
    assert told.startswith(f"<@{bot.ids.owner}>!   Uwagi po starcie bota:\n- Nie udało się dodać") and note in told
    posted = len(game_db.log_channel.messages)
    await bot.on_ready()                                                       # a reconnect: not told again
    assert len(game_db.log_channel.messages) == posted and bot._startup_notes == []

    # The next start finds the table free: the columns are added, nothing to report.
    await bot.setup_hook()
    assert bot._startup_notes == [] and await schema.verify(db) == []
    assert (await pets_columns(db))["holo"] == ("boolean", "NO", "false")


async def test_owner_is_told_at_start_up_when_cards_cannot_be_composed(game_db, monkeypatch):
    """A machine without Pillow shows every pet plain: that must not go unnoticed."""
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))
    monkeypatch.setattr(type(db), "connect", lambda self: asyncio.sleep(0))
    assert petcards.pillow_available() is True
    await bot.setup_hook()
    assert bot._startup_notes == []                                            # Pillow is there: nothing to say

    monkeypatch.setattr(petcards, "Image", None)
    assert petcards.pillow_available() is False
    await bot.setup_hook()
    assert bot._startup_notes == [bot_module.MSG_NO_PILLOW]
    assert "pip install -r requirements.txt" in bot_module.MSG_NO_PILLOW and "\\\\" not in bot_module.MSG_NO_PILLOW
    await bot.on_ready()
    assert "Brak biblioteki Pillow" in game_db.log_channel.messages[-1].content


async def test_owner_is_told_when_the_database_was_down_at_start_up(game_db, monkeypatch):
    """No connection at start-up: nothing was verified and no column added - the owner hears why."""
    bot, db = game_db.bot, game_db.bot.db
    monkeypatch.setattr(bot, "load_extension", lambda name: asyncio.sleep(0))

    async def refused(self):
        raise ConnectionRefusedError("the database is restarting")

    monkeypatch.setattr(type(db), "connect", refused)
    await bot.setup_hook()                                                     # starts all the same
    assert bot._startup_notes == [bot_module.MSG_DATABASE_DOWN_AT_START.format(error="ConnectionRefusedError")]
    assert "pieczęcie, folia Holo" in bot._startup_notes[0]
    await bot.on_ready()
    assert "Baza danych nie odpowiadała przy starcie bota (ConnectionRefusedError)" in (
        game_db.log_channel.messages[-1].content)


# -- the game without the columns (the migration failed) ----------------------------------

async def test_game_goes_on_without_cosmetics_when_the_columns_are_missing(game_db, monkeypatch, cards, cog, caplog):
    db, bot = game_db.bot.db, game_db.bot
    await old_database(db)
    boss_admin, player = admin(game_db), crafter(game_db)
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 0, 0)                                            # the best seal and Holo, if rolled

    with caplog.at_level(logging.WARNING, logger="altermmo.features.pets"):
        assert await pets.assign_pet(game_db.scene(), player) is True          # the egg is given all the same
    assert asked == [] and bot.owner_log == []                                 # nothing rolled, nothing reported
    assert sum("pet cosmetics are off" in record.message for record in caplog.records) == 1
    row = await pet_row(db, 1)
    assert "seal" not in row and "holo" not in row and row["pet_lvl"] == 0
    assert await pets.generate_pet_egg(game_db.scene(), player) == 2 and asked == []
    assert await pets.cosmetics_available(bot) is False

    # Pets are shown as before: an egg, then the hatched pet - a card without a seal or Holo.
    assert await pets.show_pet(game_db.scene(player), player) is True
    assert game_db.channel.messages[-1].files == ["0.png"] and [call.look.plain for call in cards] == [True]
    assert await pets.level_up_pet(game_db.scene(), player.id) is True
    assert await pets.show_pet(game_db.scene(player), player) is True
    message = game_db.channel.messages[-1]
    assert (cards[-1].look.seal, cards[-1].look.holo, cards[-1].look.plain) == (0, False, True)
    assert message.files == ["0.png"] and "Pieczęć" not in message.embeds[0].description
    assert "Wygląd: Standard\n\n" in message.embeds[0].description

    # The rest of the pet game does not notice.
    assert (await pets.get_pet_skills(bot, player.id))["CRIT_PERC"] >= 0
    await pets.pet_ranking(game_db.scene(player))
    assert game_db.channel.messages[-1].embeds[0].title == "Najbardziej utalentowani towarzysze!"
    assert await pets.check_stable(game_db.scene(player)) is True
    assert await pets.store_pet(game_db.scene(player), 1) is True
    assert await pets.unstore_pet(game_db.scene(player), 1) is True
    assert await pets.name_pet(game_db.scene(player), "Burek") is True

    # The owner's command says why it cannot work ...
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 tak")
    assert game_db.channel.messages[-1].content == pets_cog.MSG_SEAL_NO_COLUMNS
    assert await pets.set_cosmetics(bot, player.id, 3, True) == ("no_columns", None)
    # ... and works as soon as the columns are there - without a restart.
    assert await schema.add_new_columns(db) == []
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 tak")
    assert game_db.channel.messages[-1].content == pets_cog.MSG_SEAL_SET.format(
        player_id=player.id, pet_id=1, seal="Arcyłowcy", holo="tak")
    assert await pets.cosmetics_available(bot) is True and await cosmetics(db, 1) == (3, True)


async def test_egg_is_stored_even_when_the_columns_vanished_after_the_bot_looked(game_db, monkeypatch, caplog):
    db, bot = game_db.bot.db, game_db.bot
    await old_database(db)
    bot._pet_cosmetics = True                                                  # they were there at start-up
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch, 0, 0)
    player = crafter(game_db)
    await add_owner(db, player.id, 0, reroll_scroll=4)
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        assert await pets.assign_pet(game_db.scene(), player) is True
    assert len(asked) == 2                                                     # rolled, but there is nowhere to keep it
    owner = await owner_row(db, player.id)
    assert (owner["pet_id"], owner["reroll_scroll"]) == (1, 4)                 # the whole transaction went through
    assert (await pet_row(db, 1))["pet_name"] == "Towarzysz" and await count(db, "PETS") == 1
    assert any("the cosmetics of pet egg 1 are lost (seal 3, holo True)" in record.message
               for record in caplog.records)
    assert bot.owner_log == []                                                 # nothing rare was kept
    assert await pets.cosmetics_available(bot) is False                        # the bot knows now

    asked.clear()
    assert await pets.generate_pet_egg(game_db.scene(), player) == 2 and asked == []


async def test_only_a_missing_column_is_forgiven(game_db, monkeypatch):
    """Any other refusal of the INSERT is an error as before - and nothing of the egg is kept."""
    db = game_db.bot.db
    await db.execute("ALTER TABLE PETS ADD CONSTRAINT no_big_seal CHECK (SEAL < 3)")
    rig(monkeypatch, randint=0)
    dice(monkeypatch, 0, 500)
    player = crafter(game_db)
    with pytest.raises(asyncpg.CheckViolationError):
        await pets.assign_pet(game_db.scene(), player)
    assert await count(db, "PETS") == 0 and await owner_row(db, player.id) is None


# -- $DajPieczec ------------------------------------------------------------------------

async def test_admin_sets_the_cosmetics_of_the_active_pet(game_db, cog, cards):
    db = game_db.bot.db
    boss_admin, player, lonely = admin(game_db), game_db.add_member("Ala"), game_db.add_member("Ola")
    await add_owner(db, player.id, 5, pet_id_alt1=6)
    await add_pet(db, 5, pet_lvl=1, variant="1")
    await add_pet(db, 6, pet_lvl=1)                                            # in the stable: never touched
    await add_owner(db, lonely.id, 0)

    def said() -> str:
        return game_db.channel.messages[-1].content

    await run(game_db, player, f"$DajPieczec {player.id} 3 tak")               # not an administrator: nothing
    assert await cosmetics(db, 5) == (0, False)

    set_text = (f"Towarzysz gracza <@{player.id}> (PET_ID 5) ma teraz: pieczęć - **Łowcy**, folia Holo - "
                "**nie**. Widać to na karcie towarzysza (**$towarzysz**).")
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 2")
    assert await cosmetics(db, 5) == (2, False)
    # The player does not support: it is stored, and the administrator is told why nobody sees it.
    assert said() == set_text + pets_cog.MSG_SEAL_NOT_SHOWN and "nie ma teraz rangi wspierającego" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 0")               # nothing to hide: no warning
    assert said().endswith("(**$towarzysz**).") and await cosmetics(db, 5) == (0, False)
    player.roles.append(FakeRole(patrons.YOUTUBE_ROLES[1]))                    # a supporter from now on
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 2")
    assert said() == set_text and await cosmetics(db, 5) == (2, False)
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 TAK")
    assert await cosmetics(db, 5) == (3, True) and "pieczęć - **Arcyłowcy**, folia Holo - **tak**" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 1")               # Holo left out: unchanged
    assert await cosmetics(db, 5) == (1, True) and "pieczęć - **Tropiciela**, folia Holo - **tak**" in said()
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 0 nie")
    assert await cosmetics(db, 5) == (0, False) and "pieczęć - **brak**, folia Holo - **nie**" in said()
    assert await cosmetics(db, 6) == (0, False)

    # What it set is what the player sees.
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 3 tak")
    await pets.show_pet(game_db.scene(player), player)
    assert game_db.channel.messages[-1].files == ["card-1-bare-3-holo.gif"]
    assert "Pieczęć: Arcyłowcy\nFolia: Holo\n" in last_embed(game_db).description

    # Polite refusals; nothing changes.
    for tier in ("4", "-1"):
        await run(game_db, boss_admin, f"$DajPieczec {player.id} {tier}")
        assert said() == "Pieczęć: 0-3 (0 = brak, 1 = Tropiciela, 2 = Łowcy, 3 = Arcyłowcy)."
    await run(game_db, boss_admin, f"$DajPieczec {player.id} 2 może")
    assert said() == (f"Folia Holo: **tak** albo **nie** (pominięta - bez zmian), np. "
                      f"**$DajPieczec {player.id} 3 tak**.")
    for nobody in (lonely.id, 12345):                                          # no active pet / no row at all
        await run(game_db, boss_admin, f"$DajPieczec {nobody} 2")
        assert said() == f"<@{nobody}> nie ma przy sobie towarzysza - nie ma komu dać pieczęci."
    await run(game_db, boss_admin, "$DajPieczec")                              # bad arguments: a usage hint
    assert said().startswith("Niepoprawne użycie komendy.")
    await run(game_db, boss_admin, f"$DajPieczec {player.id} dużo")
    assert said().startswith("Niepoprawne użycie komendy.")
    assert await cosmetics(db, 5) == (3, True)


async def test_set_cosmetics_statuses(game_db):
    db, bot = game_db.bot.db, game_db.bot
    await add_owner(db, 1, 5)
    await add_pet(db, 5, seal=1, holo=True)                                    # an egg can be given a seal too
    assert await pets.set_cosmetics(bot, 1, 2) == ("ok", {"pet_id": 5, "seal": 2, "holo": True})
    assert await pets.set_cosmetics(bot, "1", "3", False) == ("ok", {"pet_id": 5, "seal": 3, "holo": False})
    for bad in (4, -1, "x", None):
        assert await pets.set_cosmetics(bot, 1, bad) == ("bad_seal", None)
    assert await pets.set_cosmetics(bot, 2, 1) == ("alone", None)
    await db.execute("DELETE FROM PETS")                                       # the owner row points nowhere
    assert await pets.set_cosmetics(bot, 1, 1) == ("alone", None)


# -- copies of the database -------------------------------------------------------------

async def test_backup_copies_the_table_with_and_without_the_columns(db, tmp_path):
    """The copy lists the columns the table really has, so both shapes are copied as they are."""
    for folder in ("data", "new", "old"):
        (tmp_path / folder).mkdir()
    await add_pet(db, 5, seal=2, holo=True)
    made = await backup.create_backup(db, tmp_path / "data", tmp_path / "new", tables=("pets",))
    with zipfile.ZipFile(made.path) as archive:
        header, row = archive.read("db/pets.csv").decode("utf-8").splitlines()
        assert header.split(",")[-3:] == ["mythic", "seal", "holo"] and row.split(",")[-2:] == ["2", "t"]
        assert "SEAL NUMERIC NOT NULL DEFAULT 0" in archive.read("schema.sql").decode("utf-8")
    assert made.tables[0].columns[-2:] == ("seal", "holo")

    await old_database(db)
    await add_pet(db, 6)
    made = await backup.create_backup(db, tmp_path / "data", tmp_path / "old", tables=("pets",))
    with zipfile.ZipFile(made.path) as archive:
        header = archive.read("db/pets.csv").decode("utf-8").splitlines()[0]
        assert header.split(",")[-1] == "mythic" and "seal" not in header
    assert made.tables[0].rows == 1 and "seal" not in made.tables[0].columns
