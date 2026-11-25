"""A pet on a companion mission ("Model A", features/missions.py + features/pets.py), against a real PostgreSQL.

The pet stays in its PETOWNER column; a ``PET_MISSIONS`` row says it is away until ``ENDS_AT``.  These
tests write such rows directly (the missions' own flows come with the rest of features/missions.py) and
check every place that reads or changes the active pet (spec §11.4) - each one also works again once
``ENDS_AT`` has passed, before anybody claims the mission.  The mine, the shiny gem and the training
have their away cases next to their own tests (test_dungeon.py, test_skills.py, test_training.py).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
from pathlib import Path

import asyncpg
import pytest

from altermmo import schema
from altermmo.core import clock, ui
from altermmo.features import boss, daily, missions, pets, training

from fakes import FakeInteraction
from test_pets import add_owner, add_pet, confirms, owner_row, patron, pet_row, rig
from test_pets import plain_cards, sacrifices  # noqa: F401 - autouse: plain pictures; the sacrifice's modifier

ROOT = Path(__file__).resolve().parent.parent / "altermmo"
#: The key of the mission the rows of these tests were sent on, and its name in the missions' config.
MISSION_KEY = "karawana"
MISSION_NAME = "Eskorta karawany"


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a !potwierdzam slot was never released"


# -- helpers ---------------------------------------------------------------------------------

_SQL_SEND = """INSERT INTO PET_MISSIONS (PLAYER_ID, PERIOD_START, OFFERS, STATUS, CHOSEN, PET_ID, PET_PLACE,
                   PET_SNAPSHOT, CHANCE, ROLL, OUTCOME, SENT_AT, ENDS_AT)
               VALUES ($1, now(), $3, 'away', 0, $2, $4, '{}', 50, 10, '{"result": "success"}', now(),
                       now() + make_interval(hours => $5))
               RETURNING MISSION_ID"""


async def send_away(db, player_id: int, pet_id: int, *, place: int = 0, hours: int = 6) -> int:
    """``pet_id`` of ``player_id`` is on a mission from now for ``hours`` (a row as the send writes it)."""
    return await db.fetchval(_SQL_SEND, player_id, pet_id, json.dumps([{"key": MISSION_KEY}]), place, hours,
                             idempotent=False)


async def come_back(db, mission_id: int) -> None:
    """The mission's time is over (nobody claimed it yet): the pet is back."""
    await db.execute("UPDATE PET_MISSIONS SET ENDS_AT = now() - interval '1 second' WHERE MISSION_ID = $1",
                     mission_id)


async def paid(db, mission_id: int) -> None:
    """The mission was claimed (what the claim writes, without its rewards)."""
    await db.execute("UPDATE PET_MISSIONS SET STATUS = 'paid', PAID_AT = now() WHERE MISSION_ID = $1", mission_id)


async def ends_at(db, mission_id: int):
    return await db.fetchval("SELECT ENDS_AT FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id)


async def away_text(db, player_id: int, name: str, mission_id: int) -> str:
    return pets._away_message(player_id, name, await ends_at(db, mission_id))


async def slot_away_text(db, player_id: int, slot: int, mission_id: int) -> str:
    return pets.MSG_SLOT_AWAY.format(slot=slot, when=ui.when(await ends_at(db, mission_id)), player_id=player_id)


async def with_pet(game, name: str = "Ala", pet_id: int = 5, *, player=None, **owner):
    """A member whose active pet is ``pet_id`` ("Burek", a level-2 cat with some stats)."""
    player = player or game.add_member(name)
    await add_owner(game.bot.db, player.id, pet_id, **owner)
    await add_pet(game.bot.db, pet_id, pet_name="Burek", pet_lvl=2, pet_skills=3, crit_perc=11, def_perc=7,
                  detection=True)
    return player


@contextlib.asynccontextmanager
async def sending(db, player_id: int, pet_id: int):
    """An open send transaction, as missions.send runs it (§7.4): the player's lock, his PETOWNER row and
    the pet's PETS row locked, the ``away`` row written - committed when the block ends."""
    locked, release = asyncio.Event(), asyncio.Event()

    async def send() -> None:
        async with db.transaction() as con:
            await pets._lock_player(con, player_id)
            await con.fetchrow("SELECT * FROM PETOWNER WHERE PLAYER_ID = $1 FOR UPDATE", player_id)
            await con.fetchrow("SELECT * FROM PETS WHERE PET_ID = $1 FOR UPDATE", pet_id)
            await con.fetchval(_SQL_SEND, player_id, pet_id, json.dumps([{"key": MISSION_KEY}]), 0, 6)
            locked.set()
            await release.wait()

    task = asyncio.ensure_future(send())
    await asyncio.wait_for(locked.wait(), 5)
    try:
        yield
    finally:
        release.set()
        await task


async def blocked(db, timeout: float = 5.0) -> None:
    """Wait until some statement waits for a lock (the concurrent change has reached the send's locks)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await db.fetchval("SELECT COUNT(*) FROM pg_locks WHERE NOT granted"):
        assert loop.time() < deadline, "nothing ever waited for the send's locks"
        await asyncio.sleep(0.01)


# ======================================= the table ===========================================

async def test_the_table_is_made_at_start_up_with_its_keys_and_checks(db):
    assert "altermmo.features.missions" in schema.SCHEMA_MODULES
    assert missions.NEW_TABLES == missions.SCHEMA and missions.NEW_COLUMNS == ()
    assert pets._MISSIONS_TABLE == missions.TABLE == "PET_MISSIONS"
    await db.execute("DROP TABLE PET_MISSIONS", idempotent=True)
    assert "brak tabeli pet_missions" in await schema.verify(db)
    await schema.create_new_tables(db)
    await schema.create_new_tables(db)                                 # again: nothing breaks
    assert await schema.verify(db) == []
    names = {row[0] for row in await db.fetch("SELECT indexname FROM pg_indexes WHERE tablename = 'pet_missions'")}
    assert names >= {"pet_missions_pkey", "pet_missions_period_uq", "pet_missions_one_away_idx",
                     "pet_missions_away_pet_idx", "pet_missions_due_idx", "pet_missions_news_idx",
                     "pet_missions_unseen_idx"}

    offered = "INSERT INTO PET_MISSIONS (PLAYER_ID, PERIOD_START, OFFERS) VALUES ($1, $2, '[]') RETURNING *"
    period = clock.utcnow().replace(microsecond=0)
    row = await db.fetchrow(offered, 1, period)
    assert (row["status"], row["rerolls"], row["mission_id"]) == ("offered", 0, 1) and row["rolled_at"] is not None
    with pytest.raises(asyncpg.UniqueViolationError):                   # one board per player and period
        await db.fetchrow(offered, 1, period)
    await send_away(db, 2, 7)
    with pytest.raises(asyncpg.UniqueViolationError):                   # one pet away per player
        await send_away(db, 2, 8)
    with pytest.raises(asyncpg.CheckViolationError):                    # sent without an outcome
        await db.execute("UPDATE PET_MISSIONS SET STATUS = 'away', PET_ID = 5 WHERE MISSION_ID = 1")
    with pytest.raises(asyncpg.CheckViolationError):                    # an unknown status
        await db.execute("UPDATE PET_MISSIONS SET STATUS = 'lost' WHERE MISSION_ID = 1")


async def test_readiness_is_asked_once_and_a_no_again_after_a_while(game_db, monkeypatch):
    bot, db = game_db.bot, game_db.bot.db
    await db.execute("DROP TABLE PET_MISSIONS")
    assert await pets.missions_table_ready(bot) is False
    assert pets.sql_not_away("p.PET_ID", False) == "TRUE"
    assert pets.sql_not_away("p.PET_ID", True) == (
        "NOT EXISTS (SELECT 1 FROM PET_MISSIONS m WHERE m.PET_ID = p.PET_ID AND m.STATUS = 'away' "
        "AND m.ENDS_AT > now())")
    await schema.create_new_tables(db)
    assert await pets.missions_table_ready(bot) is False                # the "no" is remembered ...
    later = clock.monotonic() + pets._READY_RECHECK + 1
    monkeypatch.setattr(pets.clock, "monotonic", lambda: later)
    assert await pets.missions_table_ready(bot) is True                 # ... for ten minutes
    asked: list = []
    real = db.fetchval

    async def fetchval(query, *args, **kwargs):
        asked.append(query)
        return await real(query, *args, **kwargs)

    monkeypatch.setattr(db, "fetchval", fetchval)
    assert await pets.missions_table_ready(bot) is True and await pets.missions_table_ready(bot, refresh=True)
    assert asked == []                                                  # a "yes" is final
    bot._pet_missions = (False, clock.monotonic())
    assert await pets.missions_table_ready(bot, refresh=True) is True and len(asked) == 1


async def test_an_old_database_without_the_table_plays_as_before(game_db, monkeypatch, sacrifices):
    """Nobody can be away without the table, and nothing asks it - a fight never fails on it."""
    game, db = game_db, game_db.bot.db
    await db.execute("DROP TABLE PET_MISSIONS")
    ala = await with_pet(game, pet_id=5, pet_id_alt1=6, reroll_scroll=2)
    await add_pet(db, 6, pet_lvl=1)
    warned: list[int] = []

    async def warn(bot, player_id, text):
        warned.append(player_id)

    monkeypatch.setattr(pets, "_warn_about_boss", warn)
    assert (await pets.get_pet_skills(game.bot, ala.id))["CRIT_PERC"] == 11
    await pets.detect_boss(game.bot, 2)
    assert warned == [ala.id]
    fighters, dropped = await training._snapshot(game.bot, [ala])
    assert [one.pet_id for one in fighters] == [5] and dropped == []
    assert await pets.level_up_pet(game.scene(ala), ala.id) is True
    assert await pets.away_until(game.bot, db, 5) is None and await pets.away_of(game.bot, db, ala.id) is None
    places = await pets.egg_places(game.bot, ala.id)
    assert places.occupied[0][:2] == (1, 6) and not places.active_away
    assert await pets.store_pet(game.scene(ala), 1) is True             # swapped
    assert await pets.unstore_pet(game.scene(ala), 1) is True
    assert (await pets._stable_view(game.bot, ala.id))[1]
    confirms(game, ala)
    assert await pets.reroll_pet(game.scene(ala), ala) is True
    assert await pets.discard_pet(game.scene(ala)) is True
    assert await pets.missions_table_ready(game.bot) is False


# ================================ fights, detection, growing =================================

async def test_a_pet_on_a_mission_gives_no_fight_bonus(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game)
    ola = await with_pet(game, "Ola", 6)
    mission = await send_away(db, ala.id, 5)
    zeros = dict(pets.NO_PET_SKILLS)
    assert await pets.get_pet_skills(game.bot, ala.id) == zeros
    assert set((await boss._pet_skills(game.bot, ala.id)).values()) == {0.0}       # boss fights and duels
    assert set((await daily.pet_skills(game.bot, ala.id)).values()) == {0.0}        # the hunt and the tower
    assert (await pets.get_pet_skills(game.bot, ola.id))["CRIT_PERC"] == 11         # somebody else's is there
    await come_back(db, mission)                                                    # back, not claimed yet
    skills = await pets.get_pet_skills(game.bot, ala.id)
    assert (skills["CRIT_PERC"], skills["DEF_PERC"], skills["DETECTION"]) == (11, 7, True)
    assert (await daily.pet_skills(game.bot, ala.id))["CRIT_PERC"] == 11.0


async def test_a_pet_on_a_mission_detects_no_boss(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game)
    ola = await with_pet(game, "Ola", 6)
    warned: list[int] = []

    async def warn(bot, player_id, text):
        warned.append(player_id)

    monkeypatch.setattr(pets, "_warn_about_boss", warn)
    mission = await send_away(db, ala.id, 5)
    await pets.detect_boss(game.bot, 3)
    assert warned == [ola.id]
    await come_back(db, mission)
    await pets.detect_boss(game.bot, 3)
    assert sorted(warned[1:]) == sorted([ala.id, ola.id])


async def test_a_pet_on_a_mission_does_not_grow_and_the_drop_leaves_the_level_out(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game)
    mission = await send_away(db, ala.id, 5)
    before = await pet_row(db, 5)
    assert await pets.level_up_pet(game.scene(ala), ala.id) is False
    loot = {"id": daily.LOOT_PET_LEVEL, "descr": "Poziom towarzysza"}
    assert await daily._drop(game.scene(ala), ala, loot, bonus=False) == ""
    assert await pet_row(db, 5) == before
    await come_back(db, mission)
    assert await daily._drop(game.scene(ala), ala, loot, bonus=False) == "👉 Poziom towarzysza\n"
    assert (await pet_row(db, 5))["pet_lvl"] == 3


async def test_the_training_drops_a_pet_on_a_mission(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game)
    ola = await with_pet(game, "Ola", 6)
    mission = await send_away(db, ala.id, 5)
    fighters, dropped = await training._snapshot(game.bot, [ala, ola])
    assert [one.member for one in fighters] == [ola] and dropped == [ala]
    sign_up = type("SignUp", (), {"_clicked": set(), "scene": game.scene()})()
    allow = training._TrainingSignUp._allow
    until = await ends_at(db, mission)
    assert await allow(sign_up, FakeInteraction(game.bot, ala), "join") == (
        training.MSG_TRAINING_AWAY.format(when=ui.when(until)))
    assert ala.id not in sign_up._clicked
    assert await allow(sign_up, FakeInteraction(game.bot, ola), "join") is None
    await come_back(db, mission)
    assert await allow(sign_up, FakeInteraction(game.bot, ala), "join") is None
    fighters, dropped = await training._snapshot(game.bot, [ala, ola])
    assert [one.member for one in fighters] == [ala, ola] and dropped == []


# ======================================= changes ============================================

async def test_reroll_enlight_transform_discard_and_name_refuse_a_pet_on_a_mission(game_db, monkeypatch, sacrifices):
    game, db = game_db, game_db.bot.db
    ala = patron(game, "Ala")
    await add_owner(db, ala.id, 5, reroll_scroll=30, rebirth_stones=1, mirrors=1)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=3, pet_skills=4, crit_perc=11)
    confirms(game, ala)
    rig(monkeypatch, randint=99)                                               # a name would make it shiny
    mission = await send_away(db, ala.id, 5)
    text = await away_text(db, ala.id, "Burek", mission)
    before_pet, before_owner = await pet_row(db, 5), await owner_row(db, ala.id)
    scene = game.scene(ala)
    for flow in (pets.reroll_pet(scene, ala), pets.reroll_pet(scene, ala, [0, 1]), pets.enlight_pet(scene, ala),
                 pets.transform_pet(scene, ala), pets.discard_pet(scene), pets.name_pet(scene, "Reksio"),
                 pets.adv_reroll_pet(scene)):
        assert await flow is False
        assert game.channel.messages[-1].content == text
    assert not game.channel.contains(pets.CONFIRM_HINT) and game.last_with_buttons() is None  # nothing asked
    assert await pet_row(db, 5) == before_pet and await owner_row(db, ala.id) == before_owner
    assert sacrifices == []
    await come_back(db, mission)
    assert await pets.name_pet(scene, "Reksio") is True
    assert (await pet_row(db, 5))["pet_name"] == "Reksio"
    assert await pets.reroll_pet(scene, ala) is True and await pets.enlight_pet(scene, ala) is True


async def test_a_change_asked_before_the_send_is_refused_after_the_question(game_db, monkeypatch):
    """The pet left while the question was open: the locked re-check refuses it."""
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, reroll_scroll=3)
    sent: list[int] = []
    real_confirm = pets._confirm

    async def confirm(scene, player, prompt, **kwargs):
        sent.append(await send_away(db, ala.id, 5))                    # sent during the 15 seconds
        return await real_confirm(scene, player, prompt, **kwargs)

    confirms(game, ala)
    monkeypatch.setattr(pets, "_confirm", confirm)
    before = await pet_row(db, 5)
    assert await pets.reroll_pet(game.scene(ala), ala) is False
    assert game.channel.messages[-1].content == await away_text(db, ala.id, "Burek", sent[0])
    assert await pet_row(db, 5) == before and (await owner_row(db, ala.id))["reroll_scroll"] == 3


async def test_reassign_and_the_sacrifice_core_refuse_a_pet_on_a_mission(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game)
    ola = game.add_member("Ola")
    mission = await send_away(db, ala.id, 5)
    assert await pets.reassign_pet(game.bot, 5, ola.id) is False
    assert await owner_row(db, ola.id) is None
    with pytest.raises(pets.PetAway):
        async with db.transaction() as con:
            await pets._sacrifice_in(con, ala.id, 5, False, ready=True)
    assert await pet_row(db, 5) is not None                              # rolled back: nothing sacrificed
    await come_back(db, mission)
    async with db.transaction() as con:
        assert await pets._sacrifice_in(con, ala.id, 5, False, ready=True) is None
    assert await pet_row(db, 5) is None


async def test_a_pet_on_a_mission_is_still_the_players_and_new_eggs_go_round_it(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt2=6, egg_pouch=True)
    ola = await with_pet(game, "Ola", 7)
    await add_pet(db, 6, pet_lvl=1)
    await send_away(db, ala.id, 5)
    await send_away(db, ola.id, 7)
    assert sorted(await pets.owned_pet_ids(game.bot, ala.id)) == [5, 6]       # achievements count it
    rig(monkeypatch, randint=0)
    assert await pets.assign_pet(game.scene(ala), ala) == pets.INTO_POUCH      # into the pouch ...
    owner = await owner_row(db, ala.id)
    assert owner["pet_id"] == 5 and owner["pouch_pet_id"] > 7                 # ... the pet stays where it is
    assert await pets.assign_pet(game.scene(ola), ola) is False               # no pouch: the egg is lost
    assert (await owner_row(db, ola.id))["pet_id"] == 7


# ======================================== moves =============================================

async def test_no_move_takes_a_pet_on_a_mission_anywhere(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt1=6, egg_pouch=True, pouch_pet_id=9)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=1)
    await add_pet(db, 9)
    scene = game.scene(ala)

    async def slots():
        owner = await owner_row(db, ala.id)
        return [owner[column] for column in ("pet_id", "pet_id_alt1", "pet_id_alt2", "pouch_pet_id")]

    # The pet at her side is away: it can go nowhere, and nothing can take its place.
    mission = await send_away(db, ala.id, 5)
    text = await away_text(db, ala.id, "Burek", mission)
    for move in (pets.store_pet(scene, 2), pets.store_pet(scene, 1), pets.unstore_pet(scene, 1),
                 pets.take_egg_from_pouch(scene)):
        assert await move is False
        assert game.channel.messages[-1].content == text
    status, place, row = await pets.store_in_free_place(game.bot, ala.id)
    assert (status, place, row["pet_name"]) == (pets.AWAY, 0, "Burek")
    assert await pets._take_from_pouch(game.bot, ala.id, 9) == (pets.AWAY, 0)
    assert await slots() == [5, 6, 0, 9]
    await come_back(db, mission)
    assert await pets.store_pet(scene, 2) is True and await slots() == [0, 6, 5, 9]

    # The stabled pet is away: it stays in its place.
    await paid(db, mission)
    mission = await send_away(db, ala.id, 6, place=1)
    await db.execute("UPDATE PETOWNER SET PET_ID = 5, PET_ID_ALT2 = 0 WHERE PLAYER_ID = $1", ala.id)
    text = await slot_away_text(db, ala.id, 1, mission)
    for move in (pets.store_pet(scene, 1), pets.unstore_pet(scene, 1)):
        assert await move is False
        assert game.channel.messages[-1].content == text
    assert await slots() == [5, 6, 0, 9]
    assert await pets.store_pet(scene, 2) is True and await slots() == [0, 6, 5, 9]       # another place: fine
    assert await pets.take_egg_from_pouch(game.scene(ala)) is True and await slots() == [9, 6, 5, 0]
    await come_back(db, mission)
    assert await pets.unstore_pet(scene, 1) is True and await slots() == [6, 9, 5, 0]


async def test_the_stable_shows_who_is_on_a_mission_and_offers_it_to_nobody(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt1=6, pet_id_alt2=7)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=1)
    await add_pet(db, 7, pet_name="Azor", pet_lvl=2, type="Dog")
    mission = await send_away(db, ala.id, 6, place=1)
    embed, options = await pets._stable_view(game.bot, ala.id)
    marker = pets.STABLE_AWAY.format(when=ui.when(await ends_at(db, mission)))
    assert f"**1.** **Mruczek** · Kot · poziom 1{marker}" in embed.description
    assert marker == f" - 🧭 na zleceniu, wraca {ui.when(await ends_at(db, mission))}"
    assert [option.value for option in options] == ["2:7"]
    assert await pets.check_stable(game.scene(ala)) is True
    stable = game.channel.messages[-1]
    assert [option.value for option in stable.button("pet:stable-take").options] == ["2:7"]
    await come_back(db, mission)
    embed, options = await pets._stable_view(game.bot, ala.id)
    assert "na zleceniu" not in embed.description and [option.value for option in options] == ["1:6", "2:7"]


async def test_the_stable_list_refuses_while_the_pet_at_the_side_is_on_a_mission(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt1=6)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=1)
    assert await pets.check_stable(game.scene(ala)) is True
    stable = game.channel.messages[-1]
    mission = await send_away(db, ala.id, 5)                              # sent while the list is shown
    await game.click(stable, ala, "pet:stable-take", values=["1:6"])
    assert game.channel.contains(await away_text(db, ala.id, "Burek", mission))
    assert (await owner_row(db, ala.id))["pet_id"] == 5
    assert "🧭 na zleceniu" in stable.embeds[0].description                # redrawn with the marker


async def test_the_stable_offers_nothing_while_the_pet_at_the_side_is_on_a_mission(game_db):
    """Every entry would swap the pet at her side out (refused): no list at all, and the stable says why."""
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt1=6, egg_pouch=True, pouch_pet_id=9)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=1)
    await add_pet(db, 9)
    mission = await send_away(db, ala.id, 5)
    embed, options = await pets._stable_view(game.bot, ala.id)
    assert options == [] and embed.description.endswith(pets.STABLE_SIDE_AWAY)
    assert "**Mruczek** · Kot · poziom 1" in embed.description                      # still listed, not offered
    assert await pets.check_stable(game.scene(ala)) is True
    assert game.channel.messages[-1].view is None
    await come_back(db, mission)
    embed, options = await pets._stable_view(game.bot, ala.id)
    assert [option.value for option in options] == ["1:6", "pouch:9"]
    assert pets.STABLE_SIDE_AWAY not in embed.description


async def test_the_mine_never_swaps_or_counts_a_pet_on_a_mission(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt1=6, pet_id_alt2=7, pet_id_alt3=8, pet_id_alt4=10)
    for pet_id in (6, 7, 8, 10):
        await add_pet(db, pet_id, pet_lvl=1)
    mission = await send_away(db, ala.id, 7, place=2)
    places = await pets.egg_places(game.bot, ala.id)
    assert [place for place, _pet, _row in places.occupied] == [1, 3, 4] and places.away == frozenset({2})
    assert places.free == () and not places.active_away and places.active_pet["pet_id"] == 5
    egg = pets.roll_egg(ala)
    placed = await pets.place_egg(game.scene(ala), ala, egg, pets.EGG_SWAP, place=2, expected_pet=7)
    assert placed == pets.EggPlaced(pets.EGG_CHANGED)
    assert await pet_row(db, 7) is not None and (await owner_row(db, ala.id))["pet_id_alt2"] == 7
    # The pet at her side.
    await come_back(db, mission)
    await paid(db, mission)
    mission = await send_away(db, ala.id, 5)
    places = await pets.egg_places(game.bot, ala.id)
    assert places.active_away and places.away == frozenset() and len(places.occupied) == 4
    assert places == pets.EggPlaces(False, False, 4, (), places.occupied, places.active_pet, True, frozenset())
    await come_back(db, mission)
    assert not (await pets.egg_places(game.bot, ala.id)).active_away


# ===================================== what is shown ========================================

async def test_the_pet_shown_says_it_is_on_a_mission_and_has_no_gameplay_buttons(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, summon_scrolls=2)
    mission = await send_away(db, ala.id, 5)
    assert await pets.show_pet(game.scene(ala), ala) is True
    shown = game.channel.messages[-1]
    when = ui.when(await ends_at(db, mission))
    assert f"\n🧭 **Na zleceniu:** „{MISSION_NAME}” - wraca {when}" in shown.embeds[0].description
    assert "Zwoje przywołania: 2" in shown.embeds[0].description
    assert shown.button_labels == ["Stajnia"]
    await come_back(db, mission)
    await db.execute("UPDATE PETOWNER SET SUMMON_SCROLLS = 0")
    assert await pets.show_pet(game.scene(ala), ala) is True
    shown = game.channel.messages[-1]
    assert "Na zleceniu" not in shown.embeds[0].description and "przywołania" not in shown.embeds[0].description
    assert shown.button_labels == ["Zmień imię", "Stajnia", "Odrodzenie", "Mocne odrodzenie", "Porzuć"]


async def test_a_panel_click_after_the_send_is_refused(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, reroll_scroll=3)
    assert await pets.show_pet(game.scene(ala), ala) is True
    panel = game.channel.messages[-1]
    mission = await send_away(db, ala.id, 5)
    click = await game.click(panel, ala, "pet:reroll")
    assert click.answers == [await away_text(db, ala.id, "Burek", mission)]
    assert panel.view is None or panel.view.is_finished()
    assert not game.channel.contains(pets.CONFIRM_HINT)
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 3


async def test_away_of_names_the_mission(game_db, monkeypatch):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game)
    assert await pets.away_of(game.bot, db, ala.id) is None
    mission = await send_away(db, ala.id, 5)
    assert await pets.away_of(game.bot, db, ala.id) == (5, await ends_at(db, mission), MISSION_NAME)
    monkeypatch.delattr(missions, "mission_name")                           # (without it: the key)
    assert (await pets.away_of(game.bot, db, ala.id))[2] == MISSION_KEY
    monkeypatch.setattr(missions, "mission_name", lambda key: "Karawana kupiecka", raising=False)
    assert (await pets.away_of(game.bot, db, ala.id))[2] == "Karawana kupiecka"
    assert pets._mission_name("not json", 0) == "zlecenie"
    await come_back(db, mission)
    assert await pets.away_of(game.bot, db, ala.id) is None


# ================================ who can go on a mission ===================================

async def test_the_pets_that_can_go_are_hatched_present_and_not_in_the_pouch(game_db):
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt1=6, pet_id_alt2=7, pet_id_alt3=404, pet_id_alt4=8, egg_pouch=True,
                         pouch_pet_id=9, stable_places=5, pet_id_alt5=10)
    await add_pet(db, 6)                                                    # an egg in the stable
    await add_pet(db, 7, pet_name="Azor", pet_lvl=3, type="Dog")
    await add_pet(db, 8, pet_lvl=1)
    await add_pet(db, 9, pet_lvl=1)                                         # (the pouch never goes)
    await add_pet(db, 10, pet_lvl=2)
    assert await pets.mission_pets(game.bot, game.add_member("Nikt")) == ([], 0)
    found, eggs = await pets.mission_pets(game.bot, ala)
    assert [(one.key, one.place, one.row["pet_name"]) for one in found] == [
        ("active:5", 0, "Burek"), ("2:7", 2, "Azor"), ("4:8", 4, "Towarzysz"), ("5:10", 5, "Towarzysz")]
    assert eggs == 1
    mission = await send_away(db, ala.id, 7, place=2)
    assert [one.key for one in (await pets.mission_pets(game.bot, ala))[0]] == ["active:5", "4:8", "5:10"]

    async def pick(key: str):
        async with db.transaction() as con:
            status, row, place = await pets.mission_pet_in(con, game.bot, ala.id, key)
        return status, None if row is None else int(row["pet_id"]), place

    assert await pick("active:5") == ("ok", 5, 0)
    assert await pick("4:8") == ("ok", 8, 4)
    assert await pick("2:7") == (pets.AWAY, 7, 2)
    assert await pick("1:6") == ("egg", 6, 1)
    assert await pick("3:404") == ("no_data", None, 3)
    for stale in ("active:7", "4:5", "0:5", "11:8", "9:9", "pouch:9", "nonsense", "5:"):
        assert await pick(stale) == ("changed", None, 0), stale
    await come_back(db, mission)
    assert await pick("2:7") == ("ok", 7, 2)


# =================================== the lock races (§7.1) ===================================

async def test_a_change_waiting_for_the_send_sees_it(game_db, monkeypatch, sacrifices):
    """Each change waits for the send's locks; when the send commits, it must refuse.

    (Why the away check is a statement of its own: under READ COMMITTED a ``SELECT ... FOR UPDATE`` - or
    an ``UPDATE ... WHERE NOT EXISTS (... PET_MISSIONS ...)`` - that had to wait for the send's row lock is
    re-checked by PostgreSQL only on the new version of the LOCKED row; its sub-select still runs under the
    statement's old snapshot and does not see the ``away`` row that just committed.  ``level_up_pet`` with
    the check folded into its ``FOR UPDATE OF p`` would level the pet that was just sent.)
    """
    game, db = game_db, game_db.bot.db
    ala = await with_pet(game, pet_id_alt1=6, egg_pouch=True)
    await add_pet(db, 6, pet_lvl=1)
    await pets.missions_table_ready(game.bot)                          # (asked before the race starts)
    await pets.upgrades_available(game.bot)
    await pets.mission_columns_available(game.bot)
    await pets.cosmetics_available(game.bot)
    confirms(game, ala)
    rig(monkeypatch, randint=99)
    scene = game.scene(ala)
    egg = pets.roll_egg(ala)

    async def raced(change):
        async with sending(db, ala.id, 5):
            task = asyncio.ensure_future(change())
            await blocked(db)
        result = await task
        await db.execute("DELETE FROM PET_MISSIONS")
        return result

    before_pet, before_owner = await pet_row(db, 5), await owner_row(db, ala.id)
    assert await raced(lambda: pets.level_up_pet(scene, ala.id)) is False
    assert await raced(lambda: pets.name_pet(scene, "Reksio")) is False
    assert await raced(lambda: pets.store_pet(scene, 2)) is False
    assert await raced(lambda: pets._take_out(game.bot, ala.id, 1, None)) == pets.AWAY
    assert (await raced(lambda: pets.store_in_free_place(game.bot, ala.id)))[0] == pets.AWAY
    assert await pet_row(db, 5) == before_pet and await owner_row(db, ala.id) == before_owner

    # The pet in the stable place the mine's swap names.
    async def swap():
        return await pets.place_egg(scene, ala, egg, pets.EGG_SWAP, place=1, expected_pet=6)

    async with sending(db, ala.id, 6):
        task = asyncio.ensure_future(swap())
        await blocked(db)
    assert (await task).status == pets.EGG_CHANGED and await pet_row(db, 6) is not None
    await db.execute("DELETE FROM PET_MISSIONS")

    # !porzucam: the question is answered while the send holds the row; its transaction then refuses.
    real_confirm = pets._confirm
    holder: dict = {}

    async def confirm(scene_, player, prompt, **kwargs):
        answer = await real_confirm(scene_, player, prompt, **kwargs)
        holder["send"] = sending(db, ala.id, 5)
        await holder["send"].__aenter__()
        return answer

    monkeypatch.setattr(pets, "_confirm", confirm)
    task = asyncio.ensure_future(pets.discard_pet(scene))
    await blocked(db)
    await holder["send"].__aexit__(None, None, None)
    assert await task is False and await pet_row(db, 5) is not None and sacrifices == []
    assert (await owner_row(db, ala.id))["pet_id"] == 5


# ================================ the guard against new readers ===============================

#: What reads or changes the active pet / a pet's place (spec §11.5).
_PATTERNS = ("o.PET_ID > 0", "PET_ID > 0)", "JOIN PETS p ON p.PET_ID = o.PET_ID", "UPDATE PETS SET",
             "DELETE FROM PETS", "UPDATE PETOWNER SET")
#: A function that asks one of these knows about the missions.
_AWARE = ("away_until", "_present_pet", "sql_not_away", "_away_check", "_away_among", "_away_ends")
#: Reviewed by hand: ``(file, function or module-level name)`` -> why it needs no away check.
REVIEWED: dict[tuple[str, str], str] = {
    ("features/pets.py", "_SQL_ACTIVE_PET"): "the SQL of _active_pet",
    ("features/pets.py", "_SQL_PRESENT_PET"): "the SQL of _present_pet (formatted with sql_not_away)",
    ("features/pets.py", "_SQL_SAVE_PET"): "used by _save_pet; every caller asks away first",
    ("features/pets.py", "_SQL_LOCK_ACTIVE_PET"): "the admin's !DajPieczec / !DajRamke: allowed (looks only)",
    ("features/pets.py", "_active_pet"): "the lock primitive itself; its callers ask away_until afterwards",
    ("features/pets.py", "new_record_petowners"): "counters, and a PET_ID only into an empty place",
    ("features/pets.py", "show_pet"): "shards into scrolls",
    ("features/pets.py", "assign_pet"): "an egg into an empty PET_ID or the pouch: never replaces a pet",
    ("features/pets.py", "give_egg_pouch"): "the pouch flag",
    ("features/pets.py", "add_stable_place_in"): "the number of places",
    ("features/pets.py", "upgrade_pouch_in"): "the pouch flags",
    ("features/pets.py", "add_summon_scroll_in"): "a counter",
    ("features/pets.py", "take_summon_scroll"): "a counter",
    ("features/pets.py", "return_summon_scroll"): "a counter",
    ("features/pets.py", "give_summon_scrolls"): "a counter",
    ("features/pets.py", "set_cosmetics"): "admin, looks only: allowed for a pet on a mission",
    ("features/pets.py", "set_frame"): "admin, looks only: allowed for a pet on a mission",
    ("features/pets.py", "add_items_in"): "counters",
    ("features/pets.py", "take_scrolls_in"): "a counter",
    ("features/pets.py", "_level_note"): "runs only after a level that was granted",
    ("features/training.py", "_SQL_SNAPSHOT"): "formatted with pets.sql_not_away in _snapshot",
    ("features/skills.py", "assign_skill"): "the gem column",
    ("features/skills.py", "_choose"): "the gem column",
    ("features/events.py", "_hazard_bet"): "the scroll counter",
    ("features/events.py", "assasinate"): "the scroll counter",
}


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _owners(tree: ast.Module):
    """``(name, node)`` of every top-level function, method and module-level assignment."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{item.name}", item
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            yield ",".join(target.id for target in targets if isinstance(target, ast.Name)), node


def _hits(path: Path):
    """``(owner, what)`` of every reader / writer of a pet's place in one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstrings(tree)
    for owner, node in _owners(tree):
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and id(inner) not in docstrings:
                for pattern in _PATTERNS:
                    if pattern in inner.value:
                        yield owner, node, pattern
            elif isinstance(inner, ast.Call):
                func = inner.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name == "_active_pet":
                    yield owner, node, "_active_pet("
            elif isinstance(inner, (ast.Name, ast.Attribute)):
                name = inner.id if isinstance(inner, ast.Name) else inner.attr
                if name == "_SQL_ACTIVE_PET":
                    yield owner, node, "_SQL_ACTIVE_PET"


def _aware(node: ast.AST) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in _AWARE:
                return True
    return False


def test_every_active_pet_reader_is_reviewed():
    """A new reader of the active pet / a new writer of a pet's place fails here until it is reviewed:
    it asks the missions (away_until / _present_pet / sql_not_away ...) or is added to REVIEWED."""
    unreviewed, used = [], set()
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        for owner, node, pattern in _hits(path):
            key = (relative, owner)
            if key in REVIEWED:
                used.add(key)
            elif not _aware(node):
                unreviewed.append(f"{relative}:{owner} ({pattern})")
    assert sorted(set(unreviewed)) == []
    assert set(REVIEWED) - used == set(), "a reviewed entry no longer reads anything: take it off the list"


def test_no_away_check_is_folded_into_a_locking_statement():
    """(§7.1) ``FOR UPDATE OF p`` and ``PET_MISSIONS`` never meet in one literal; _present_pet cannot lock."""
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert not ("FOR UPDATE" in node.value and "PET_MISSIONS" in node.value), path
    assert "FOR UPDATE" not in pets._SQL_PRESENT_PET and "FOR UPDATE" not in pets._SQL_AWAY
    assert "lock" not in inspect.signature(pets._present_pet).parameters


def test_the_pets_module_never_imports_the_missions_at_its_top():
    tree = ast.parse((ROOT / "features" / "pets.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert "missions" not in [alias.name for alias in node.names] and "missions" not in (node.module or "")
