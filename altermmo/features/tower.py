"""The weekly Death Tower (``!wieża``): up to ten floors, each a harder typing fight.

Port of the legacy ``functions/functions_tower.py``.

Differences from the legacy code (the game itself is unchanged):

* fight rounds go through :func:`altermmo.core.challenge.ask` (see
  ``features/daily.py``); the legacy "lag compensation" is gone;
* the answering player is recognised by id (the legacy ``msg.author is player``
  identity test only worked by accident of discord.py's member cache);
* the weekly cooldown is still stored before the first floor, but a run that
  ends because of a technical problem gives the attempt back
  (:func:`explore_tower`);
* a cleared floor stays cleared: loot, the tower record and the achievement
  role are guarded one by one, a failure is reported to the owner;
* the record is stored with one statement inside a transaction, the time
  always as ``H:MM:SS.ffffff`` (``str(timedelta)`` dropped the fraction for
  whole seconds and the legacy comparison then crashed);
* ``create_database`` no longer drops the table.

The run has no sign-up and no confirmation (one player, typed fights), so the
v2 panels change only the "come back later" text: it shows the real moment of
the weekly reset (:func:`altermmo.features.daily.next_season_end`) as
Discord's live "za 3 dni".  :func:`start_tower` is the whole ``!wieża``
command, so a button elsewhere (the hub panel) can run exactly the same thing.

The Klucz do wieży (owner, 2026-11-26): a stored item (``PLAYER_ITEMS``
``tower_key``, :mod:`altermmo.features.items`; it drops, and the merchant
sells it) that opens the door once more in a week whose run the player has
already had.  It is used on purpose - ``!klucz`` or the "Użyj klucza" button
under the closed-door answer (``shortcuts.key_view``) - both are
``start_tower(use_key=True)``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import discord

from ..core import challenge, fightlog, guards, io, timing, ui
from ..core.challenge import Outcome
from ..core.state import BossPhase
from ..settings import ASSETS_DIR
from . import boss, daily, database, items

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS TOWER (
           PLAYER_ID NUMERIC PRIMARY KEY,
           TOWER_LEVEL NUMERIC,
           TIME VARCHAR(255)
       )""",
)

# -- numbers of the game ------------------------------------------------------------

#: Floors of one run.
TOWER_FLOORS = 10
#: Seconds for the first word of a floor; later words get ``8 / floor`` seconds.
FIRST_ROUND_TIME = daily.FIRST_ROUND_TIME
ROUND_TIME_BASE = 8

#: Floor -> rarity of the boss loot table used for its reward (other floors: epic).
_FLOOR_LOOT_RARITY: dict[int, int] = {1: 0, 2: 0, 3: 1, 4: 1, 5: 2, 6: 2, 7: 2, 8: 4, 9: 4, 10: 4}
_FLOOR_LOOT_FALLBACK = 2

# -- texts ---------------------------------------------------------------------------

TOWER_IMAGE = ("https://www.altermmo.pl/wp-content/uploads/alter0000_Huge_mystery_gloomy_tower_with_small_"
               "lights_inside._F_2fe81cd9-bff9-4bbe-af41-8101162ec6b9.png")
TOWER_COLOUR = 0xFF0019

MSG_FIGHT_START = ("Rozpoczyna się walka w Wieży Śmierci, <@{user_id}>! Każde piętro to coraz trudniejszy "
                   "boss do pokonania <:REEeee:790963160495947856>! Wpisz pojawiające się słowa tak szybko, "
                   "jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez spacji! Przygotuj się!")
MSG_FLOOR_CLEARED = "Brawo <@{user_id}> <:ok:990161663053422592> Piętro zaliczone!"
MSG_MISTAKE_LOST = ("Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie w przyszłym tygodniu - albo "
                    "wcześniej, z Kluczem do wieży! <:RIP:912797982917816341> Twój rekord został zapisany!")
MSG_TOO_SLOW_LOST = ("Niestety nie zdążyłeś! <:Bedge:970576892874854400>  Spróbuj ponownie w przyszłym "
                     "tygodniu - albo wcześniej, z Kluczem do wieży! <:RIP:912797982917816341> Twój rekord został "
                     "zapisany!")
MSG_NO_RECORD = "Jeszcze nie udało Ci się zeksplorować Wieży Śmierci.\n"

LOG_WEEKLY_RESET = "Zresetowano cooldown na weekly tower."

# New in v2 (the legacy bot had no notion of a run broken by technical problems).
MSG_TOWER_INTERRUPTED = ("Eksploracja Wieży Śmierci została przerwana z powodu problemów technicznych "
                         "<:Sadge:936907659142111273>")
MSG_ATTEMPT_RETURNED = ("<@{user_id}>, z powodu problemów technicznych Twoja wyprawa do Wieży Śmierci się "
                        "nie liczy - możesz spróbować ponownie w tym tygodniu!")
#: The run broke after floors were already rewarded: it counts (owner's rule of 2026-11-20 -
#: what is done once is done once; a second run would hand out the loot of those floors again).
MSG_RUN_CUT_SHORT = ("<@{user_id}>, z powodu problemów technicznych Twoja wyprawa do Wieży Śmierci kończy się "
                     "po {floors}. piętrze. Zdobyte piętra i łupy zostają; administracja dostała znać.")
LOG_RUN_CUT_SHORT = ("Wyprawa <@{user_id}> do Wieży Śmierci została przerwana z przyczyn technicznych po "
                     "{floors}. piętrze. Próba NIE została zwrócona (łupy za zaliczone piętra są wydane - "
                     "druga wyprawa wydałaby je ponownie). Jeśli chcesz, wynagrodź gracza ręcznie.")

# Answers of ``!wieża`` (moved here from cogs/tower.py: the hub panel runs the same command).
MSG_BOSS_COMING = ("Zaraz pojawi się prawdziwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, "
                   "kliknij **Zaatakuj** pod bossem albo wpisz **!zaatakuj**, żeby stawić mu czoła.")
MSG_BOSS_HERE = ("Nie możesz ruszyć na eksplorację Wieży Śmierci, na Twojej drodze stoi boss "
                 "<:MonkaS:882181709100097587> Kliknij **Zaatakuj** pod bossem albo wpisz **!zaatakuj**, "
                 "żeby stawić mu czoła.")
#: ``{when}``: Discord's live countdown to the weekly reset.  (Owner, 2026-11-26: with the hint
#: that a Klucz do wieży opens the door again - :data:`MSG_ON_COOLDOWN_KEYS` for who has one.)
MSG_ON_COOLDOWN = ("Drzwi Wieży Śmierci ani drgną. Otworzą się ponownie {when} - chyba że ktoś ma "
                   "**Klucz do wieży** <:Bedge:970576892874854400>")
#: ... for a player who has keys (``{keys}``); the "Użyj klucza" button comes with it (``shortcuts.key_view``).
MSG_ON_COOLDOWN_KEYS = ("Drzwi Wieży Śmierci ani drgną. Otworzą się ponownie {when} - ale masz **Klucz do "
                        "wieży** (sztuk: {keys})! Kliknij **Użyj klucza** albo wpisz **!klucz** 🗝️")
# Only for a click (a typed !wieża stays silent during a boss fight, as before).
MSG_BOSS_FIGHT = "Trwa walka z bossem - Wieża Śmierci musi poczekać <:MonkaS:882181709100097587>"

# The Klucz do wieży (owner, 2026-11-26): ``!klucz`` / the "Użyj klucza" button (:func:`start_tower`
# with ``use_key=True``).
MSG_KEY_USED = ("🗝️ <@{user_id}> przekręca w zamku **Klucz do wieży**. Zgrzyt, jęk zawiasów - i Drzwi Wieży "
                "Śmierci stają otworem! (zostało kluczy: {left})")
MSG_KEY_KEPT = "Drzwi Wieży Śmierci są otwarte, <@{user_id}> - klucz zostaje w kieszeni."
#: ``!klucz`` without a key while the door is closed for the player.
MSG_NO_KEY = ("Nie masz przy sobie **Klucza do wieży**, <@{user_id}>. Drzwi Wieży Śmierci otworzą się ponownie "
              "{when} <:Bedge:970576892874854400>")
#: To the owner (with a ping) when the database did not answer the taking of a key (taken or not: unknown).
LOG_KEY_UNSURE = ("Nie wiadomo, czy Klucz do wieży gracza <@{user_id}> został zabrany - baza danych nie "
                  "odpowiedziała (`{error}`), wyprawa do Wieży się nie zaczęła. Sprawdź PLAYER_ITEMS i w razie "
                  "potrzeby oddaj klucz: `!DajPrzedmiot @gracz klucz do wieży 1`")
#: To the owner (with a ping) when a key taken during the Monday reset could not be given back.
LOG_KEY_NOT_RETURNED = ("Klucz do wieży gracza <@{user_id}> nie wrócił (`{error}`): Drzwi Wieży otworzył "
                        "w tej samej chwili poniedziałkowy reset, więc klucz się nie przydał - oddaj go: "
                        "`!DajPrzedmiot @gracz klucz do wieży 1`")


# -- database ------------------------------------------------------------------------

async def create_database(scene: "Scene | AlterBot") -> None:
    """Create the TOWER table when it is missing (existing records are kept).

    Takes a scene like the legacy function took a ``ctx``; a bare bot is
    accepted too because the legacy "create all databases" command called it
    without one.
    """
    bot = getattr(scene, "bot", scene)
    for statement in SCHEMA:
        await bot.db.execute(statement, idempotent=True)
    log.info("Table TOWER is in place.")


def _record_text(record_time: Any) -> str:
    """A floor time as it is stored: ``H:MM:SS.ffffff``."""
    if isinstance(record_time, timedelta):
        return database.format_turn_time(record_time)
    return str(record_time).strip()


def _as_timedelta(record_time: Any) -> timedelta | None:
    if isinstance(record_time, timedelta):
        return record_time
    return database.parse_turn_time(str(record_time))


async def new_record_tower(bot: "AlterBot", player_id: int, level: int, record_time: Any) -> bool:
    """Insert a player's first tower record; False when he already has one (nothing changes)."""
    status = await bot.db.execute(
        """INSERT INTO TOWER (PLAYER_ID, TOWER_LEVEL, TIME) VALUES ($1, $2, $3)
           ON CONFLICT (PLAYER_ID) DO NOTHING""",
        int(player_id), int(level), _record_text(record_time), idempotent=True)
    return status.endswith(" 1")


async def _store_record(bot: "AlterBot", player_id: int, level: int, record_time: Any) -> bool:
    """Keep the player's best floor (and the best time on it); True when the record changed."""
    player_id, level = int(player_id), int(level)
    text = _record_text(record_time)
    new_time = _as_timedelta(record_time)
    async with bot.db.transaction() as con:
        row = await con.fetchrow(
            "SELECT TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = $1 FOR UPDATE", player_id)
        if row is None:
            await con.execute(
                """INSERT INTO TOWER (PLAYER_ID, TOWER_LEVEL, TIME) VALUES ($1, $2, $3)
                   ON CONFLICT (PLAYER_ID) DO UPDATE
                       SET TOWER_LEVEL = EXCLUDED.TOWER_LEVEL, TIME = EXCLUDED.TIME
                       WHERE COALESCE(TOWER.TOWER_LEVEL, 0) < EXCLUDED.TOWER_LEVEL""",
                player_id, level, text)
            return True
        stored_level = int(row["tower_level"] or 0)
        if stored_level > level:
            return False
        if stored_level == level:
            stored_time = database.parse_turn_time(row["time"])
            # An unreadable stored time counts as beaten, like an unreadable boss record.
            if stored_time is not None and (new_time is None or new_time >= stored_time):
                return False
        await con.execute("UPDATE TOWER SET TOWER_LEVEL = $2, TIME = $3 WHERE PLAYER_ID = $1",
                          player_id, level, text)
        return True


async def update_tower_level(scene: "Scene", player: Any, level: int, record_time: Any) -> bool:
    """Store a cleared floor: the tower record.

    (The floor roles of the old bot were withdrawn on 2026-11-20, see
    features/achievements.py.)  ``record_time`` is the time the floor took (a
    ``timedelta``, or text in the stored ``H:MM:SS.ffffff`` format).  Returns
    True, like the legacy function.
    """
    improved = await _store_record(scene.bot, player.id, level, record_time)
    log.info("Tower: %s (%s) cleared floor %s in %s (record %s).", player, player.id, level,
             _record_text(record_time), "updated" if improved else "kept")
    return True


# -- a floor ---------------------------------------------------------------------------

async def tower_image(scene: "Scene", level: int) -> None:
    """Show the picture and the announcement of a floor."""
    level = int(level)
    if level == 1:
        description = "Rozpoczynasz eksplorację Wieży Śmierci. Strzeż się!"
    else:
        description = "Przechodzisz na kolejne piętro. Powodzenia!"
    embed = discord.Embed(
        title=f"<:AlterUp:846344753124999169> {level} piętro Wieży Śmierci! <:Loot:912797849916436570>",
        description=description, color=0xF00511)

    picture = ASSETS_DIR / "tower" / f"{level}.png"
    if picture.is_file():
        await scene.send(file_path=picture)
    else:
        log.warning("Tower picture %s is missing; showing the floor without it.", picture)
    await scene.send(embed=embed)


def random_tower_hp(level: int) -> int:
    """Number of correct words needed on a floor (before the pet's bonus)."""
    return 2 * int(level)


def floor_loot_rarity(level: int) -> int:
    """Rarity of the boss loot table that rewards a floor."""
    return _FLOOR_LOOT_RARITY.get(int(level), _FLOOR_LOOT_FALLBACK)


async def tower_fight(scene: "Scene", tower_level: int, player: Any = None, *,
                      progress: dict[str, Any] | None = None) -> bool | None:
    """One floor of the tower: ``player`` (default: ``scene.author``) types the words.

    Returns True when the floor was cleared, False when the player lost and
    None when the fight was interrupted by repeated connection problems (the
    player is told, nothing is lost).  Technical failures before the last word
    propagate; afterwards the floor is cleared whatever happens.
    ``progress["rewarded"]`` is set the moment the floor is cleared, before
    the first reward is handed out (see :func:`explore_tower`).

    ``progress["dust"]``: the run's ``items.DustGuard`` (read once per run by
    :func:`explore_tower`) - a mistake the pet did not protect is undone by
    the explorer's Nieuchwytny pył once per run (``progress["dust_floor"]``
    notes the floor); without ``progress`` there is none.
    """
    bot = scene.bot
    channel = scene.channel
    tower_level = int(tower_level)
    hunter = player if player is not None else scene.author
    if hunter is None:
        raise ValueError("tower_fight needs a player: pass one or use a scene with an author")

    await scene.send(MSG_FIGHT_START.format(user_id=hunter.id))
    pet = await daily.pet_skills(bot, hunter.id)
    await timing.pause(8)
    await challenge.countdown(channel)

    floor_hp = daily.scaled_hp(random_tower_hp(tower_level), -pet["LOWHP_PERC"])
    shown_hp = floor_hp  # (the shown turn time: a round undone by a dust adds time, never a turn)
    dust = progress.get("dust") if progress is not None else None
    round_time = float(ROUND_TIME_BASE / max(tower_level, 1)) * (100 + pet["SLOW_PERC"]) / 100
    log.info("Tower: %s (%s) on floor %s, hp %s, %.2f s per word.", hunter, hunter.id, tower_level,
             floor_hp, round_time)

    fightlog.begin(channel, fightlog.KIND_TOWER, [hunter], detail=tower_level)
    started = time.monotonic()
    turn = 0
    while True:
        turn += 1
        word, prompt = daily.MOB_ACTIONS[random.randint(0, len(daily.MOB_ACTIONS) - 1)]

        if not daily.roll(pet["REPLACE_PERC"]):
            limit = FIRST_ROUND_TIME if turn == 1 else round_time
            result = await daily.fight_round(bot, channel,
                                             str(turn) + ". **" + str(hunter) + "**: " + prompt,
                                             players=[hunter], limit=limit,
                                             word=word, asked=hunter, turn=turn)
            if result is None:
                log.warning("Tower fight interrupted: too many disrupted rounds in a row.")
                await io.send_patient(channel, MSG_TOWER_INTERRUPTED, give_up_after=120)
                return None
            timed_out = result.outcome is Outcome.TIMEOUT
            correct = result.is_correct(word, by=hunter)
        else:
            fightlog.pet_attack(channel, turn, hunter, word)
            await scene.send("~~" + str(turn) + ". " + str(hunter) + ": " + prompt + "~~ "
                             + daily.MSG_PET_ATTACKS)
            timed_out = False
            correct = True

        if correct:
            if daily.roll(pet["CRIT_PERC"]):
                turn += 1
            if turn >= floor_hp:
                break
            continue

        if daily.roll(pet["DEF_PERC"]):
            fightlog.saved(channel)
            await scene.send(daily.MSG_TOO_SLOW_PROTECTED if timed_out else daily.MSG_MISTAKE_PROTECTED)
            continue
        if dust is not None and dust.left:  # (the explorer's dust has not worked in this run yet)
            progress["dust_floor"] = tower_level  # (noted before anything is awaited: _settle_run_dust)
            if await items.dust_saves(scene, dust, hunter, timed_out=timed_out):
                floor_hp += 1  # the round counts for nothing: the floor needs one word more
                continue

        fightlog.lost(channel, timed_out=timed_out)
        # (Guarded: a lost floor stays lost when the message cannot be sent - it must not turn
        # into "a technical problem" that gives the attempt back.)
        await daily.say_lost(scene, MSG_TOO_SLOW_LOST if timed_out else MSG_MISTAKE_LOST)
        log.info("Tower: %s lost on floor %s (%s) in turn %s.", hunter, tower_level,
                 "timeout" if timed_out else "mistake", turn)
        return False

    # -- the floor is cleared: from here on nothing may turn it into a failure --
    fightlog.won(channel)
    if progress is not None:
        progress["rewarded"] = True
    fight_time = timedelta(seconds=time.monotonic() - started)
    context = "po zaliczonym piętrze Wieży Śmierci"
    await daily.guarded(bot, "ogłoszenie zwycięstwa",
                        scene.send(MSG_FLOOR_CLEARED.format(user_id=hunter.id)), context=context)
    await daily.guarded(bot, "czas walki", scene.send(daily.fight_time_message(fight_time, shown_hp)),
                        context=context)
    with items.loot_source(items.SOURCE_TOWER):  # (some rows never drop in the tower: features/items.py)
        await daily.guarded(bot, f"łup <@{hunter.id}> za piętro {tower_level}",
                            boss.rand_loot(scene, floor_loot_rarity(tower_level), hunter, pet["DROP_PERC"]),
                            context=context)
    await daily.guarded(bot, f"rekord wieży <@{hunter.id}> (piętro {tower_level})",
                        _store_record(bot, hunter.id, tower_level, fight_time), context=context)
    return True


# -- !wieża ----------------------------------------------------------------------------

async def _give_back_attempt(scene: "Scene", player: Any) -> None:
    """Take the player off the weekly cooldown list and tell him (never raises)."""
    daily.release_cooldown(scene.bot.store.weekly_tower_cd, player.id)
    log.info("Tower run of %s (%s) given back after a technical problem.", player, player.id)
    await io.send_patient(scene.channel, MSG_ATTEMPT_RETURNED.format(user_id=player.id), give_up_after=60)


async def _settle_run_dust(bot: "AlterBot", progress: dict[str, Any], *, decided: int | None = None) -> None:
    """The run is over: its Nieuchwytny pył leaves the explorer's items if it saved him (never raises).

    Once per run, at its end (:func:`explore_tower`, :func:`_end_broken_run`) -
    never inside a floor's rounds.  ``decided``: the run broke; only a dust
    used on a floor up to this one (a floor that was decided) is taken.  The
    floor the run broke on was neither won nor lost: like every interrupted
    fight it costs nothing, the dust used there stays with the player.
    """
    guard = progress.get("dust")
    if guard is None or not guard.used:
        return
    if decided is not None and progress.get("dust_floor", 0) > decided:
        log.info("Tower: the dust of the run was used on floor %s, which the run broke on; it is not taken.",
                 progress.get("dust_floor"))
        guard.used.clear()
        return
    await items.settle_dust(bot, guard)


async def _end_broken_run(scene: "Scene", player: Any, cleared: int, rewarded: bool,
                          progress: dict[str, Any] | None = None) -> None:
    """A run ended by a technical problem (never raises).

    Before any floor was rewarded the attempt is given back.  Afterwards it is
    not: the loot of the cleared floors is out, and a second run in the same
    week would hand it out again.  The player and the owner are told.
    ``progress``: the run's, for its Nieuchwytny pył (:func:`_settle_run_dust`).
    """
    if progress is not None:
        await _settle_run_dust(scene.bot, progress, decided=cleared)
    if not rewarded:
        await _give_back_attempt(scene, player)
        return
    floors = max(int(cleared), 1)
    log.warning("Tower run of %s (%s) cut short after rewarded floors (%s cleared); the attempt is NOT "
                "given back.", player, player.id, cleared)
    await io.send_patient(scene.channel, MSG_RUN_CUT_SHORT.format(user_id=player.id, floors=floors),
                          give_up_after=60)
    await scene.bot.log_to_owner(LOG_RUN_CUT_SHORT.format(user_id=player.id, floors=floors), ping=True)


async def explore_tower(scene: "Scene") -> int:
    """The whole ``!wieża`` run of ``scene.author``; returns the number of floors cleared.

    The caller has already checked the cooldown and holds the busy flag.  The
    weekly cooldown is stored before the first floor (as in the legacy bot).
    When the run ends because of a technical problem the attempt is given back
    - but only while no floor has been rewarded yet (:func:`_end_broken_run`).

    The explorer's Nieuchwytny pył works once per run: it is read once, before
    the first floor (``progress["dust"]``), and leaves his items when the run
    is over (:func:`_settle_run_dust`) - not when the run is cancelled (no
    database is awaited then; nothing is taken, in the player's favour).
    """
    bot = scene.bot
    player = scene.author
    if player is None:
        raise ValueError("explore_tower needs a scene with an author")
    save_weekly_tower_to_file(bot, player.id)
    cleared = 0
    progress: dict[str, Any] = {"rewarded": False}
    try:
        progress["dust"] = await items.reserve_dust(bot, [player])
        for level in range(1, TOWER_FLOORS + 1):
            await tower_image(scene, level)
            result = await tower_fight(scene, level, player, progress=progress)
            if result is None:
                await _end_broken_run(scene, player, cleared, progress["rewarded"], progress)
                return cleared
            if not result:
                break
            cleared = level
    except asyncio.CancelledError:
        if progress["rewarded"]:
            log.warning("Tower run of %s (%s) cancelled after rewarded floors (%s cleared); the attempt "
                        "is kept.", player, player.id, cleared)
        else:
            daily.release_cooldown(bot.store.weekly_tower_cd, player.id)  # e.g. the bot is shutting down
        raise
    except Exception:
        log.exception("Tower run of %s (%s) failed on floor %s.", player, player.id, cleared + 1)
        await _end_broken_run(scene, player, cleared, progress["rewarded"], progress)
        if not progress["rewarded"]:
            raise
        # (Not re-raised: the player was told the run counts - "try again" would contradict it.)
        return cleared
    await _settle_run_dust(bot, progress)  # (every floor fought was decided: the last one lost, or all won)
    log.info("Tower: %s (%s) finished the run with %s floors cleared.", player, player.id, cleared)
    return cleared


async def start_tower(scene: "Scene", *, refuse: Any = None, use_key: bool = False) -> bool:
    """The whole ``!wieża`` command of ``scene.author`` (boss phase, cooldown, busy flag, the run).

    The command guards (boss channel, busy notice) run before; this checks the
    busy flag again because a click (the hub panel) has no command guards.
    ``refuse`` (``async (text, **message) -> Any``) answers a click
    (``ui.reply``); None (a typed command) answers on the channel as before.
    Returns True when a run took place.  The run lasts minutes inside this
    call: acknowledge a click before calling it.

    ``use_key=True`` is ``!klucz`` / the "Użyj klucza" button (owner,
    2026-11-26): with the door open it is a normal free run and the key stays
    in the pocket (said only to a player who has one); with the door closed
    for the player a Klucz do wieży is taken - INSIDE the busy hold
    (:func:`_open_with_key`), so that there is still no await between the
    cooldown check and ``hold_busy``: two clicks
    cannot both start, nor one player run twice on one key.  The door closed
    without a key (``use_key`` or not) is answered with the reset's countdown
    - for a player who has keys with the "Użyj klucza" button under it.
    """
    bot = scene.bot
    state = bot.state
    player = scene.author
    if player is None:
        raise ValueError("start_tower needs a scene with an author")

    async def tell(text: str, **message: Any) -> None:
        if refuse is not None:
            await (refuse(text, **message) if message else refuse(text))
        else:
            await io.send(scene.channel, text, **message)

    # Counted before the checks below (they must reach hold_busy without an
    # await), so that "the key stays in the pocket" is said only to a player
    # who has one: ``!klucz`` typed with no key, or an old "Użyj klucza" button
    # clicked after the Monday reset, is just the week's free run.
    has_key = use_key and await keys_of(bot, player.id) > 0
    if state.busy:  # somebody took the channel between the guard and here
        if refuse is None:
            await guards.notify_busy(scene)  # type: ignore[arg-type] - needs only ``.channel``
        else:
            await refuse(ui.MSG_BUSY)
        return False
    phase = state.boss_phase
    on_cd = load_weekly_tower_from_file(bot, player.id)
    # No await between the checks above and hold_busy: two players cannot both start.
    if (not state.boss_blocks_activities or bot.debug) and (not on_cd or use_key):
        with state.hold_busy("tower"):
            if on_cd:
                if not await _open_with_key(scene, tell):
                    return False
            elif has_key:
                await _say(scene, MSG_KEY_KEPT.format(user_id=player.id))
            await explore_tower(scene)
        return True

    if phase is BossPhase.PRE_SPAWN:
        await tell(MSG_BOSS_COMING)
    elif phase is BossPhase.VISIBLE:
        await tell(MSG_BOSS_HERE)
    elif phase > BossPhase.VISIBLE:
        if refuse is not None:  # a typed command stays silent during a boss fight, as before
            await refuse(MSG_BOSS_FIGHT)
    elif on_cd:
        keys = await keys_of(bot, player.id)
        if keys > 0 and not challenge.fight_on(scene.channel):
            from . import shortcuts  # (lazily, as everywhere: the shortcuts reach the hub)

            await tell(cooldown_text(keys), view=shortcuts.key_view(player.id))
        else:
            await tell(cooldown_text())
    return False


async def _open_with_key(scene: "Scene", tell: Any) -> bool:
    """The door is closed for ``scene.author``: open it with a Klucz do wieży.  True: the run may start.

    Called inside ``hold_busy("tower")`` (see :func:`start_tower`).  The key
    is taken conditionally (``items.take``: never below 0, never retried).
    Then the weekly cooldown is looked at again: should the Monday reset have
    opened the door while the database was asked, the key is given back and
    the run is the week's free one.  Otherwise the cooldown is released (the
    run puts it back before floor 1, :func:`explore_tower`) and the key's line
    is posted.  No key: the reset's countdown.  A database error: no run, the
    owner is pinged (the key may be gone or not - he checks).  A key run that
    breaks before any reward gives the attempt back as every run does; the
    key does not come back on top of it.
    """
    bot, player = scene.bot, scene.author
    try:
        ready = await items.table_ready(bot)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - nothing was taken
        log.exception("Tower: the item store could not be asked for the key of %s.", player.id)
        await tell(ui.MSG_FAILED)
        return False
    try:
        left = await items.take(bot, player.id, items.TOWER_KEY) if ready else None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - taken or not: unknown, the owner checks
        log.exception("Tower: the key of %s could not be taken.", player.id)
        await bot.log_to_owner(LOG_KEY_UNSURE.format(user_id=player.id, error=type(exc).__name__), ping=True)
        await tell(ui.MSG_FAILED)
        return False
    if left is None:
        await tell(MSG_NO_KEY.format(user_id=player.id, when=ui.when(daily.next_season_end())))
        return False
    if not load_weekly_tower_from_file(bot, player.id):
        # The Monday reset opened the door meanwhile: the key was not needed.
        log.info("Tower: the weekly reset came while the key of %s was taken; it goes back.", player.id)
        try:
            await items.grant(bot, player.id, items.TOWER_KEY)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the run is the free one anyway; the owner gives it back
            log.exception("Tower: the key of %s could not be given back.", player.id)
            await bot.log_to_owner(LOG_KEY_NOT_RETURNED.format(user_id=player.id, error=type(exc).__name__),
                                   ping=True)
        await _say(scene, MSG_KEY_KEPT.format(user_id=player.id))
        return True
    daily.release_cooldown(bot.store.weekly_tower_cd, player.id)
    log.info("Tower: %s (%s) opened the door with a key (%s left).", player, player.id, left)
    await _say(scene, MSG_KEY_USED.format(user_id=player.id, left=left))
    return True


async def _say(scene: "Scene", text: str) -> None:
    """Post a line of the key that names the player (only him is pinged).  Never raises: once a key is
    used (or kept), the run starts whatever Discord says."""
    try:
        await scene.send(text, allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=False, users=[scene.author]))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Tower: a line of the key could not be posted.")


async def keys_of(bot: "AlterBot", player_id: Any) -> int:
    """How many Klucze do wieży the player has (0 when the store is not there or cannot be read).  Never raises."""
    try:
        return await items.count(bot, player_id, items.TOWER_KEY)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - only a hint under a refusal
        log.info("Tower: the keys of %s could not be counted.", getattr(player_id, "id", player_id))
        return 0


def cooldown_text(keys: int = 0) -> str:
    """"Come back later" with Discord's live countdown to the weekly reset (Monday 15:00).

    ``keys``: how many Klucze do wieży the player has - with any, the text
    says so and points at the "Użyj klucza" button / ``!klucz``.
    """
    when = ui.when(daily.next_season_end())
    if keys > 0:
        return MSG_ON_COOLDOWN_KEYS.format(when=when, keys=int(keys))
    return MSG_ON_COOLDOWN.format(when=when)


# -- rankings --------------------------------------------------------------------------

def _tower_embed(title: str, description: str, footer: str) -> discord.Embed:
    embed = discord.Embed(title=title, url=TOWER_IMAGE, description=description, color=TOWER_COLOUR)
    embed.set_thumbnail(url=TOWER_IMAGE)
    embed.set_footer(text=footer)
    return embed


MSG_NOBODY_YET = "Nikt jeszcze nie zdobył żadnego piętra Wieży Śmierci."


def shown_time(record_time: Any) -> str:
    """A stored time as players read it: ``12,35 s``, ``1 min 05,20 s`` (not ``0:00:12.345678``).

    The stored text itself when it cannot be read.
    """
    parsed = _as_timedelta(record_time)
    if parsed is None:
        return str(record_time)
    total = round(max(parsed.total_seconds(), 0.0), 2)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(int(minutes), 60)
    if not hours and not minutes:
        return f"{seconds:.2f} s".replace(".", ",")
    text = f"{minutes} min {seconds:05.2f} s".replace(".", ",")
    return f"{hours} h {text}" if hours else text


async def tower_ranking(scene: "Scene") -> None:
    """Post the ten best explorers of the tower (highest floor first, then the shortest time)."""
    bot = scene.bot
    rows = await bot.db.fetch(
        "SELECT PLAYER_ID, TOWER_LEVEL, TIME FROM TOWER ORDER BY TOWER_LEVEL DESC, TIME LIMIT 10")
    lines = []
    for place, row in enumerate(rows, start=1):
        player_id = int(row["player_id"])
        member = await bot.member(player_id)
        # (A name is escaped; somebody who left the server is shown as a mention, as before.)
        who = discord.utils.escape_markdown(member.name) if member is not None else f"<@{player_id}>"
        lines.append(f"{ui.place(place)} **{who}** · piętro **{int(row['tower_level'] or 0)}** "
                     f"· ⏱️ {shown_time(row['time'])}")
    await scene.send(embed=_tower_embed("Eksploratorzy Wieży Śmierci!", "\n".join(lines) or MSG_NOBODY_YET,
                                        "Gratulacje dla nieustraszonych!"))


async def check_personal_tower(scene: "Scene") -> None:
    """Post the tower record of ``scene.author``."""
    player = scene.author
    row = await scene.bot.db.fetchrow(
        "SELECT TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = $1", int(player.id))
    if row is not None:
        text = (f"<@{player.id}> udało Ci się ukończyć Wieżę Śmierci na poziomie "
                f"**{int(row['tower_level'] or 0)}** w czasie **{shown_time(row['time'])}**.\n")
    else:
        text = MSG_NO_RECORD
    await scene.send(embed=_tower_embed("Personalny rekord Wieży Śmierci!", text, "Gratulacje!"))


# -- the weekly cooldown list ----------------------------------------------------------

def save_weekly_tower_to_file(bot: "AlterBot", player_id: int | str) -> None:
    """Remember that the player used this week's tower run (no duplicates)."""
    if not bot.store.weekly_tower_cd.contains(player_id):
        bot.store.weekly_tower_cd.add(player_id)


def load_weekly_tower_from_file(bot: "AlterBot", player_id: int | str) -> bool:
    """True when the player already entered the tower this week."""
    return bot.store.weekly_tower_cd.contains(player_id)


async def clear_weekly_tower_file(bot: "AlterBot") -> None:
    """Everybody may enter the tower again (weekly reset)."""
    bot.store.weekly_tower_cd.clear()
    log.info("Weekly tower cooldown reset.")
    await bot.log_to_owner(LOG_WEEKLY_RESET)
