"""The daily monster hunt (``!polowanie``): monster picture, typing fight, monster loot.

Port of the legacy ``functions/functions_daily.py``.  Random events and the
Twitch module use :func:`rand_loot`, :func:`send_mob_image` and
:func:`hunt_mobs` too; the scheduler, the shrine and skill gems reset the daily
cooldown through :func:`clear_daily_file` / :func:`remove_player_daily_file`.

Differences from the legacy code (the game itself is unchanged):

* every "type the word" round goes through :func:`altermmo.core.challenge.ask`
  (listening starts before the prompt, the reaction time is judged by Discord's
  timestamps, a round broken by a connection problem is repeated instead of
  punishing the hunters).  The legacy "lag compensation" is gone;
* the daily cooldown is still stored before the hunt starts, but a hunt that
  ends because of a technical problem (an exception, or several disrupted
  rounds in a row) gives the attempt back (:func:`daily_hunt`);
* once the monster is dead the hunt is won, whatever happens afterwards: each
  piece of bookkeeping (ranking, achievements, loot) is guarded on its own and a
  failure is reported to the owner's log channel;
* :func:`hunt_mobs` returns True (monster killed) / False (hunters lost) / None
  (interrupted by technical problems); the legacy function returned None for a
  won hunt;
* the cooldown list lives in ``bot.store.daily_cd`` (atomic writes), so the
  three file helpers take the bot as their first argument;
* a "(Bonus)" drop really grants a second item (the legacy code only printed
  the line), and a player who poses as the monster earns no scrolls for a hunt
  he took part in himself.

Joining somebody's hunt (v2 panels): the monster's embed carries a "Dołącz do
polowania" button (:class:`_JoinPanel`).  A click does exactly what typing
``!polowanie`` in that moment does - same window (:data:`JOIN_TIME`, restarted by
every join), same limit (:data:`MAX_HUNTERS`), joining is free (only the player
who started the hunt uses the daily attempt) - and both ways keep working side
by side.  The embed shows who joined; dead hunters cannot click (they cannot
type either).  :func:`start_hunt` is the whole ``!polowanie`` command, so a
button elsewhere (the hub panel) can run exactly the same thing.

The "come back later" text shows the real moment of the next reset
(:func:`next_daily_reset`), as Discord's live "za 3 godziny".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Sequence

import discord

from ..core import challenge, clock, fightlog, guards, io, timing, ui
from ..core.challenge import Outcome
from ..core.state import BossPhase
from ..settings import ASSETS_DIR, CONFIG_DIR, says_command
from . import achievements, boss, database, modifiers, shortcuts

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- numbers of the game ------------------------------------------------------------

#: Monster life by rarity: (min, max) of ``random.randint``; other rarities use the last one.
MOB_HP_RANGES: dict[int, tuple[int, int]] = {0: (3, 5), 1: (6, 9), 2: (9, 12)}
MOB_HP_FALLBACK = (9, 12)

#: What another player types to join a hunt that is about to start.
JOIN_WORD = "!polowanie"
#: Seconds to wait for (another) player to join; every join restarts the wait.
JOIN_TIME = 8
MAX_HUNTERS = 3
#: Seconds for the very first word of a hunt / a tower floor.
FIRST_ROUND_TIME = 15
#: Extra drop chance (percent) for every additional hunter of a party.
PARTY_DROP_BOOST = 10

LOOT_SHARD = 9
LOOT_EGG = 10
LOOT_SCROLL = 11
LOOT_PET_LEVEL = 12
LOOT_REBIRTH_STONE = 13
LOOT_MIRROR = 14

_LOOT_TABLES: dict[int, str] = {
    0: "loot_details_Normal",
    1: "loot_details_Rare",
    2: "loot_details_Epic",
}
_LOOT_TABLE_FALLBACK = "loot_details_Epic"

_PET_SKILL_KEYS = ("CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC", "SLOW_PERC")

# -- words and texts ----------------------------------------------------------------

#: (word to type, prompt) of hunts and of the Death Tower.
MOB_ACTIONS: tuple[tuple[str, str], ...] = (
    ("unik", "Potwór szarżuje na Ciebie! Wpisz **U N I K**"),
    ("atak", "Potwór zawahał się! Teraz! Wpisz **A T A K**"),
    ("paruj", "Potwór atakuje, nie masz miejsca na ucieczkę, wpisz **P A R U J**"),
    ("skok", "Potwór próbuje ataku w nogi, wpisz **S K O K**"),
    ("bieg", "Potwór szykuje potężny atak o szerokim zasięgu, wpisz **B I E G**"),
    ("turlaj", "Potwór atakuje w powietrzu, wpisz **T U R L A J**"),
    ("czaruj", "Potwór rzuca klątwę, wpisz **C Z A R U J**"),
    ("blok", "Potwór atakuje, nie masz miejsca na ucieczkę, wpisz **B L O K**"),
    ("skacz", "Potwór próbuje ataku w nogi, wpisz **S K A C Z**"),
    ("akcja", "Potwór szarżuje na Ciebie, zrób coś, wpisz **A K C J A**"),
    ("krzyk", "Nie masz pojęcia co robić, wpisz **K R Z Y K**"),
    ("ruch", "Musisz zrobić cokolwiek, wpisz **R U C H**"),
    ("posuw", "Potwór rzuca głazem w Twoją stronę, wpisz **P O S U W**"),
    ("impet", "Dostrzegasz szansę na uderzenie, wpisz **I M P E T**"),
    ("zryw", "Pojawiła się chwila zawachania potwora, wpisz **Z R Y W**"),
)

LOOT_IMAGE = "https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png"

_THUMB_BASE = "https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/google/313/"
_MOB_THUMBS: dict[int, str] = {
    0: _THUMB_BASE + "large-green-square_1f7e9.png",
    1: _THUMB_BASE + "large-blue-square_1f7e6.png",
}
_MOB_THUMB_FALLBACK = _THUMB_BASE + "large-orange-square_1f7ea.png"
_MOB_COLOURS: dict[int, int] = {0: 0xFFFFFF, 1: 0x0000FF}
_MOB_COLOUR_FALLBACK = 0xFCBA03
_MOB_ADJECTIVES: dict[int, str] = {0: "Zwykły", 1: "Rzadki"}
_MOB_ADJECTIVE_FALLBACK = "Unikalny"
_MOB_KINDS: dict[int, str] = {0: "zwykły", 1: "rzadki"}
_MOB_KIND_FALLBACK = "UNIKALNY"

MSG_JOIN_HINT = ("\n\n*Inny gracz może dołączyć do polowania, klikając „Dołącz do polowania” albo wpisując "
                 "natychmiast !polowanie*.")

MSG_SOLO_START = ("Polowanie się rozpoczyna {mentions}<:REEeee:790963160495947856>! Wpisz pojawiające się "
                  "słowa tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez "
                  "spacji! Przygotuj się!")
MSG_GROUP_START = ("Rozpoczęliście polowanie {mentions}<:REEeee:790963160495947856>! Wpiszcie **słowa "
                   "przypisane do Was** tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! "
                   "Wpiszcie słowa bez spacji! Przygotujcie się!")
MSG_WON = "Brawo {mentions}<:ok:990161663053422592> Potwór pokonany!"
MSG_MISTAKE_LOST = ("Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie jutro lub poczekaj na "
                    "modlitwę wspierających! <:RIP:912797982917816341>\n\n"
                    "*Możesz również dołączyć do polowania innych graczy.*")
MSG_TOO_SLOW_LOST = ("Niestety nie zdążyłeś! <:Bedge:970576892874854400> Odpocznij i spróbuj jutro! "
                     "<:RIP:912797982917816341>\n\n*Możesz również dołączyć do polowania innych graczy.*")
MSG_MISTAKE_PROTECTED = "Pomyliłeś się, ale Twój towarzysz Cię chroni!"
MSG_TOO_SLOW_PROTECTED = "Nie zdążyłeś, ale Twój towarzysz Cię chroni!"
MSG_PET_ATTACKS = "Twój towarzysz wyprowadza atak!"

LOG_DAILY_RESET = "Zresetowano cooldown na daily."

# New in v2 (the legacy bot had no notion of a hunt broken by technical problems).
MSG_HUNT_INTERRUPTED = ("Polowanie zostało przerwane z powodu problemów technicznych "
                        "<:Sadge:936907659142111273> Nikt nie ponosi konsekwencji.")
#: The hunt broke after it was decided: it counts (what is done once is done once).
MSG_HUNT_CUT_SHORT = ("<@{user_id}>, przy rozliczaniu polowania wystąpił problem techniczny. Polowanie się "
                      "liczy; administracja dostała znać i sprawdzi nagrody.")
LOG_HUNT_CUT_SHORT = ("Polowanie <@{user_id}> zostało przerwane błędem (`{error}`) już po rozstrzygnięciu. "
                      "Próba NIE została zwrócona - sprawdź w logu, czy nagród nie trzeba przyznać ręcznie.")
MSG_ATTEMPT_RETURNED = ("<@{user_id}>, z powodu problemów technicznych Twoje dzisiejsze polowanie się nie "
                        "liczy - możesz zapolować ponownie!")

# Answers of ``!polowanie`` (moved here from cogs/hunting.py: the hub panel runs the same command).
MSG_BOSS_COMING = ("Zaraz pojawi się prawdziwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, "
                   "kliknij **Zaatakuj** pod bossem albo wpisz **!zaatakuj**, żeby stawić mu czoła.")
MSG_BOSS_HERE = ("Teraz pora na walkę z prawdziwym bossem, nie mieszaj się leszczyku "
                 "<:madge:882184635474386974>")
#: ``{when}``: Discord's live countdown to the next reset (:func:`next_daily_reset`).
MSG_ON_COOLDOWN = ("Zregeneruj się i spróbuj zapolować ponownie {when} <:Bedge:970576892874854400> Niektóre "
                   "modlitwy przy kapliczkach również są w stanie zregenerować Twoje siły, więc bądź czujny!")
# Only for a click (a typed !polowanie stays silent during a boss fight, as before).
MSG_BOSS_FIGHT = "Trwa walka z bossem - polowanie musi poczekać <:MonkaS:882181709100097587>"

# The "Dołącz do polowania" button under the monster.
JOIN_BUTTON = "Dołącz do polowania"
JOIN_EMOJI = "⚔️"
ROSTER_NAME = "Łowcy ({count}/{limit})"
MSG_JOIN_UNTIL = "⏳ Dołączanie kończy się {when}."
MSG_ALREADY_HUNTING = "Już jesteś na tym polowaniu <:PepoG:790963160528977980>"
MSG_JOIN_TOO_LATE = "Za późno - polowanie już się zaczęło <:Sadge:936907659142111273>"
# The same for everybody who clicks: one line on the channel is enough (see ui.reply).
ui.GENERAL_NOTICES.add(MSG_JOIN_TOO_LATE)
#: Seconds between two edits of the hunters' list on the monster's embed.
ROSTER_EDIT_GAP = 1.0


# -- small helpers (shared with features/tower.py) ------------------------------------

def _pets():
    """The pets feature (imported on first use, like in ``features/boss.py``)."""
    from . import pets
    return pets


def roll(chance_percent: float) -> bool:
    """True with ``chance_percent`` % probability (the legacy ``random()*100 < x`` test)."""
    return random.random() * 100 < chance_percent


def scaled_hp(base: int, percent: float) -> int:
    """``base`` changed by ``percent`` %, cut down to a whole number, never below 1.

    The legacy ``int(hp * (1 + x / 100))`` could give 0 (the fight then crashed
    on a division by zero while computing the average turn time).
    """
    scaled = Decimal(int(base)) * (Decimal(100) + Decimal(str(percent))) / Decimal(100)
    return max(1, int(scaled))


async def pet_skills(bot: "AlterBot", player_id: int) -> dict[str, float]:
    """The player's pet bonuses as plain floats (zeros without a pet)."""
    raw = await _pets().get_pet_skills(bot, player_id)
    if not isinstance(raw, dict):
        raw = {}
    return {key: float(raw.get(key) or 0) for key in _PET_SKILL_KEYS}


def fight_time_message(fight_time: timedelta, hp: int) -> str:
    """The "it took you ..." line shown after a killed monster / a cleared tower floor."""
    turn_time = fight_time / max(int(hp), 1)
    return (f"Zabicie potwora zajęło: {round(fight_time.total_seconds(), 2):.2f} sekundy! "
            f"Jedna tura zajęła średnio {round(turn_time.total_seconds(), 2):.2f} sekundy!")


async def say_lost(scene: "Scene", text: str) -> None:
    """Tell the channel a fight is lost; a message that cannot be sent changes nothing.

    (Only logged: the owner has no reward to hand out by hand after a lost fight.)
    """
    try:
        await scene.send(text)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the fight stays lost
        log.exception("Could not announce the lost fight.")


async def guarded(bot: "AlterBot", what: str, awaitable: Awaitable[Any], default: Any = None,
                  *, context: str = "po wygranym polowaniu") -> Any:
    """Await one piece of bookkeeping that must not take the rest down with it.

    A failing step is logged and reported to the owner (who can hand the
    reward out by hand); ``default`` is returned instead of its result.
    """
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Step failed (%s): %s", context, what)
        await bot.log_to_owner(f"Błąd {context} ({what}): `{type(exc).__name__}: {exc}`. "
                               "Sprawdź, czy nagrody nie trzeba przyznać ręcznie.", ping=True)
        return default


async def fight_round(bot: "AlterBot", channel: Any, prompt: str, *, players: Sequence[Any],
                      limit: float, **journal: Any) -> challenge.RoundResult | None:
    """One "type the word" round that never blames the players for our lag.

    A disrupted round is announced and asked again; None after
    ``challenge.MAX_CONSECUTIVE_DISRUPTIONS`` disrupted rounds in a row (the
    caller then ends the activity without punishing anybody; the fight journal
    has the fight as interrupted).  ``journal``: what ``challenge.ask`` records
    about the round (``word=``, ``asked=``, ``turn=``).
    """
    for attempt in range(1, challenge.MAX_CONSECUTIVE_DISRUPTIONS + 1):
        result = await challenge.ask(bot, channel, prompt, players=players,
                                     timeout=timing.timeout(max(float(limit), 0.0)), **journal)
        if result.outcome is not Outcome.DISRUPTED:
            return result
        log.warning("Fight round disrupted (%d/%d in a row).", attempt, challenge.MAX_CONSECUTIVE_DISRUPTIONS)
        if attempt < challenge.MAX_CONSECUTIVE_DISRUPTIONS:
            await challenge.notify_disrupted(channel)
    fightlog.interrupted(channel, f"{challenge.MAX_CONSECUTIVE_DISRUPTIONS} rounds in a row were disrupted")
    return None


def release_cooldown(cooldown: Any, player_id: int | str) -> bool:
    """Give a consumed attempt back: drop the id from a cooldown list.  Never raises."""
    try:
        return cooldown.remove([player_id]) > 0
    except Exception:  # noqa: BLE001 - called from error paths; must not hide the real error
        log.exception("Could not remove %s from the cooldown list %s.", player_id,
                      getattr(cooldown, "path", cooldown))
        return False


# -- loot ----------------------------------------------------------------------------

def _loot_table(rarity: int) -> list[dict[str, Any]]:
    config = json.loads((CONFIG_DIR / "mobLootConfig.json").read_text(encoding="utf-8"))
    return config[_LOOT_TABLES.get(int(rarity), _LOOT_TABLE_FALLBACK)]


async def _grant_loot(scene: "Scene", hunter: Any, loot: dict[str, Any]) -> bool | str:
    """Hand one loot item to ``hunter``; False when there was nothing to give.

    (A player who already has a pet gets no egg, a pet at the top level cannot
    level up...).  Items without a mechanic of their own are text only.
    """
    loot_id = loot["id"]
    if loot_id == LOOT_EGG:
        return await _pets().assign_pet(scene, hunter)  # (``pets.INTO_POUCH`` is truthy as well)
    if loot_id == LOOT_SCROLL:
        await _pets().assign_scroll(scene, 1, hunter.id)
        return True
    if loot_id == LOOT_SHARD:
        await _pets().assign_shard(scene, 1, hunter.id)
        return True
    if loot_id == LOOT_PET_LEVEL:
        return bool(await _pets().level_up_pet(scene, hunter.id))
    if loot_id == LOOT_REBIRTH_STONE:
        return bool(await _pets().assign_rebirth_stone(scene, 1, hunter.id))
    if loot_id == LOOT_MIRROR:
        return bool(await _pets().assign_mirror(scene, 1, hunter.id))
    return True


async def _drop(scene: "Scene", hunter: Any, loot: dict[str, Any], *, bonus: bool,
                granted_out: list[Any] | None = None) -> str:
    """Grant one drop and return its line for the drop message ("" when nothing dropped)."""
    if bonus and loot["id"] == LOOT_EGG:
        return ""  # a second egg never drops (legacy rule)
    try:
        granted = await _grant_loot(scene, hunter, loot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one broken item must not cost the player the others
        log.exception("Could not grant monster loot %s to %s (%s).", loot.get("id"), hunter, hunter.id)
        await scene.bot.log_to_owner(
            f"Nie udało się przyznać łupu graczowi {hunter} (<@{hunter.id}>): {loot.get('descr')} "
            f"(`{type(exc).__name__}: {exc}`). Przyznaj ręcznie.", ping=True)
        return ""
    if granted_out is not None:
        granted_out.append(granted)  # (an egg: ``True`` = the active pet, ``pets.INTO_POUCH`` = the pouch)
    if not granted:
        log.info("Monster loot %s rolled for %s but there was nothing to grant.", loot["id"], hunter.id)
        return ""
    return "👉 " + loot["descr"] + (" (Bonus)\n" if bonus else "\n")


async def rand_loot(scene: "Scene", rarity: int, hunter: Any, boost_percent: float) -> str:
    """Draw the loot of a monster of ``rarity`` for ``hunter``, grant it and post the drop embed.

    ``boost_percent`` raises every weight; a weight above 100 % is a certain
    drop plus a chance for a second, "(Bonus)" one.  Returns the drop message
    (one ``👉 ...`` line per item).

    Owner's rule (2026-11-21): an egg and a pet level never come in the same
    drop - once this drop has given the hunter a new egg, a pet level rolled
    in it is not granted (the egg would hatch before the player has seen it).
    An egg that went into the egg pouch does not count (owner, 2026-11-23):
    the active pet is still the old one and gets the level.
    The level's own weight roll is still made, but a level that is not granted
    does not call ``pets.level_up_pet``, so the random draws of a level-up (the
    talent, the skill) are not made either and the draws after it in this drop
    differ from what they would have been.
    """
    bot = scene.bot
    boost = int(boost_percent)
    drop_message = ""
    new_egg = False  # this very drop gave the hunter an egg that is now the ACTIVE pet
    # The drop changed the pet at the hunter's side (a new egg there, a level): the drop embed
    # gets the "Pokaż towarzysza" button (owner, 2026-11-24; features/shortcuts.py).
    pet_changed = False

    async def drop(loot: dict[str, Any], bonus: bool) -> str:
        nonlocal new_egg, pet_changed
        if new_egg and loot["id"] == LOOT_PET_LEVEL:
            log.info("Pet level rolled for %s together with a new egg: not granted (never both in one drop).",
                     hunter.id)
            return ""
        granted: list[Any] = []
        line = await _drop(scene, hunter, loot, bonus=bonus, granted_out=granted)
        if line and loot["id"] == LOOT_EGG and granted and granted[0] is True:  # (not pets.INTO_POUCH)
            new_egg = True
            pet_changed = True
        if line and loot["id"] == LOOT_PET_LEVEL:
            pet_changed = True
        return line

    for loot in _loot_table(int(rarity)):
        weight = (boost / 100 + 1) * loot["weight"]
        if weight > 100:
            drop_message += await drop(loot, False)
            if random.random() * 100 <= weight - 100:
                drop_message += await drop(loot, True)
        elif random.random() * 100 <= weight:
            drop_message += await drop(loot, False)

    embed = discord.Embed(title="Drop - " + str(hunter), url=LOOT_IMAGE,
                          description="Otrzymałeś:\n" + drop_message, color=0xFCDB03)
    embed.set_thumbnail(url=LOOT_IMAGE)
    embed.set_footer(text="Gratulacje!")
    try:
        await scene.send(embed=embed, **({"view": shortcuts.pet_view(hunter)} if pet_changed else {}))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the loot is already granted
        log.warning("Loot of %s granted but the drop embed could not be posted: %s: %s",
                    hunter, type(exc).__name__, exc)
        await bot.log_to_owner(f"Nie udało się pokazać łupu gracza {hunter} na kanale "
                               f"(`{type(exc).__name__}`); łup został przyznany: \n{drop_message}")
    log.info("Monster loot for %s (rarity %s, boost %s%%): %r", hunter, rarity, boost, drop_message)
    return drop_message


# -- the monster appears ---------------------------------------------------------------

def _mob_picture(rarity: int):
    """A random picture of a monster of ``rarity`` (``assets/mobs/<n>.png``)."""
    if 0 <= rarity <= 1:
        first = 0 if rarity == 0 else 10
        return ASSETS_DIR / "mobs" / f"{random.randint(0, 9) + first}.png"
    return ASSETS_DIR / "mobs" / "100.png"


async def send_mob_image(scene: "Scene", rarity: int) -> tuple[bool, Any]:
    """Show the monster: its picture (or the avatar of the player posing as it) and the embed.

    Returns ``(is_player_boss, boss_player)``; ``boss_player`` is the member
    who earns scrolls when the monster is not beaten, or None for an ordinary
    monster.  Pass both on to :func:`hunt_mobs`.  The embed carries the "Dołącz
    do polowania" button; :func:`hunt_mobs` on the same channel listens to it.
    """
    bot = scene.bot
    rarity = int(rarity)

    # The shrine may have chosen a player who poses as every monster until the next boss dies.
    boss_player = None
    forced_id = (await modifiers.load_modifiers(bot))["player_id"]
    if forced_id > 0:
        boss_player = await bot.member(forced_id)
        if boss_player is None:
            log.info("The player chosen as the monster (%s) left the server; a monster comes instead.",
                     forced_id)
    is_player_boss = boss_player is not None

    add_desc = ""
    if is_player_boss:
        add_desc = ("\n\n*Potworem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, "
                    "to ten gracz zgarnia " + str(rarity + 1) + " zwojów!*")
    add_desc += await modifiers.load_desc_modifiers(bot)
    add_desc += MSG_JOIN_HINT

    name = str(boss_player) if is_player_boss else "mob"
    embed = discord.Embed(
        title=f"💀 {_MOB_ADJECTIVES.get(rarity, _MOB_ADJECTIVE_FALLBACK)} {name}! 💀",
        description=(f"Na polowaniu pojawił się {_MOB_KINDS.get(rarity, _MOB_KIND_FALLBACK)} potwór! ⚔️"
                     + add_desc),
        color=_MOB_COLOURS.get(rarity, _MOB_COLOUR_FALLBACK))
    embed.set_thumbnail(url=_MOB_THUMBS.get(rarity, _MOB_THUMB_FALLBACK))

    if is_player_boss:
        avatar = getattr(getattr(boss_player, "display_avatar", None), "url", None)
        if avatar:
            await scene.send(str(avatar))
    else:
        picture = _mob_picture(rarity)
        if picture.is_file():
            await scene.send(file_path=picture)
        else:
            log.warning("Monster picture %s is missing; showing the monster without it.", picture)
    panel = _JoinPanel(bot, embed)
    try:
        await panel.view.send(scene.channel, embed=embed)
    except BaseException:
        await panel.view.close()
        raise
    await _keep_panel(scene.channel, panel)
    log.info("Monster shown: rarity %s, player monster: %s.", rarity, boss_player)
    return is_player_boss, boss_player


def random_boss_hp(rarity: int) -> int:
    """Number of correct words needed to kill a monster of ``rarity`` (before modifiers)."""
    low, high = MOB_HP_RANGES.get(int(rarity), MOB_HP_FALLBACK)
    return random.randint(low, high)


async def set_dead_hunters(scene: "Scene", user_id: Any) -> bool:
    """Give the "dead hunter" role to a hunter killed by a monster (never raises).

    The legacy module had its own copy of ``functions_boss.setDeadHunters``;
    here it simply forwards to :func:`altermmo.features.boss.set_dead_hunters`.
    """
    return await boss.set_dead_hunters(scene.bot, user_id)


# -- the hunt --------------------------------------------------------------------------

#: Answers a join request (a click, through ``ui.reply``); None for a typed ``!polowanie`` (stays silent).
Answer = Callable[[str], Awaitable[Any]]


@dataclass
class _JoinRequest:
    """Somebody wants to join the hunt gathering on a channel (typed, clicked or from the hub)."""

    member: Any
    answer: Answer | None = None


def _on_list(hunters: Sequence[Any], member: Any, debug: bool) -> bool:
    """True when ``member`` hunts already (debug mode lets one tester fill the party)."""
    return not debug and any(hunter.id == member.id for hunter in hunters)


@dataclass
class _Gathering:
    """A hunt waiting for more hunters on a channel: where join requests go, who is in already."""

    queue: asyncio.Queue
    #: The hunters so far (the very list :func:`_gather_hunters` fills).
    hunters: list[Any]
    debug: bool = False


#: Channel id -> the hunt gathering hunters there right now.
_gathering: dict[int, _Gathering] = {}
#: Channel id -> the join button of the monster shown last (taken by :func:`hunt_mobs`).
_panels: dict[int, "_JoinPanel"] = {}
#: Answers still on their way (kept here so they are not garbage-collected).
_answers: set[asyncio.Future] = set()


def is_gathering(channel: Any) -> bool:
    """True while a hunt on ``channel`` waits for more hunters.

    The ``!polowanie`` command asks: typed in that moment the word joins the
    running hunt, so the command itself must neither start a second hunt nor
    answer with the "bot is busy" notice.
    """
    return getattr(channel, "id", None) in _gathering


def is_hunting(channel: Any, member: Any) -> bool:
    """True when ``member`` is on the list of the hunt gathering on ``channel`` already."""
    gathering = _gathering.get(getattr(channel, "id", None))
    return gathering is not None and _on_list(gathering.hunters, member, gathering.debug)


def request_join(channel: Any, member: Any, answer: Answer | None = None) -> bool:
    """Ask to join the hunt gathering on ``channel`` (what typing ``!polowanie`` does now).

    ``answer`` receives the refusal ("already on this hunt", "too
    late"); an accepted member is shown on the monster's embed.  False when no
    hunt is gathering there.  A click should be refused with
    :func:`is_hunting` first: the gathering itself never waits for an answer.
    """
    gathering = _gathering.get(getattr(channel, "id", None))
    if gathering is None:
        return False
    gathering.queue.put_nowait(_JoinRequest(member, answer))
    return True


def _answer_later(answer: Answer, text: str) -> None:
    """Deliver an answer in the background (the join window keeps running meanwhile)."""

    def done(task: asyncio.Future) -> None:
        _answers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.info("An answer to a join request failed (%s).", task.exception())

    task = asyncio.ensure_future(answer(text))
    _answers.add(task)
    task.add_done_callback(done)


async def _refuse_leftovers(queue: asyncio.Queue) -> None:
    """Tell every join request nobody took that the hunt has started.  Never raises.

    The answers go out in the background: the hunt does not wait for Discord.
    """
    while not queue.empty():
        request = queue.get_nowait()
        if request.answer is not None:
            _answer_later(request.answer, MSG_JOIN_TOO_LATE)


def _mentions(hunters: Sequence[Any]) -> str:
    """Legacy ``player_list_string``: ``<@1> <@2> `` (a space after every mention)."""
    return "".join(f"<@{hunter.id}> " for hunter in hunters)


class _JoinPanel:
    """The "Dołącz do polowania" button under the monster and the list of its hunters.

    Posted by :func:`send_mob_image`, read by :func:`_gather_hunters`.  A click
    becomes a :class:`_JoinRequest` in :attr:`requests` - the same queue the
    typed ``!polowanie`` goes to - so both ways follow one set of rules.  The
    click is queued the moment it arrives and acknowledged afterwards, so a
    click counts exactly like a ``!polowanie`` typed at the same moment (the
    acknowledgement is a round trip to Discord).  A click of somebody already
    on the list is refused right away and never reaches the queue.  The
    list is repainted by a small background task, at most once per
    :data:`ROSTER_EDIT_GAP` seconds; every edit survives a purged message.
    """

    def __init__(self, bot: "AlterBot", embed: discord.Embed) -> None:
        self.bot = bot
        self.embed = embed
        self.requests: asyncio.Queue[_JoinRequest] = asyncio.Queue()
        self.hunters: list[Any] = []
        self._ends_at: datetime | None = None
        self._dirty = asyncio.Event()
        self._painter: asyncio.Task | None = None
        self.view = ui.ClickView([ui.Choice("hunt-join", JOIN_BUTTON, emoji=JOIN_EMOJI,
                                            style=discord.ButtonStyle.primary)], allow=self._clicked)

    async def _clicked(self, interaction: Any, key: str) -> str | None:
        member = interaction.user
        if self.view.closed:
            return MSG_JOIN_TOO_LATE
        if _on_list(self.hunters, member, self.bot.debug):
            return MSG_ALREADY_HUNTING  # answered by the view; the gathering never waits for it
        acked = asyncio.ensure_future(ui.ack(interaction))

        async def answer(text: str) -> None:
            # A late answer ("too late") must not race the acknowledgement still on its way.
            await asyncio.wait({acked})
            await ui.reply(interaction, text)

        # Queued before the round trip to Discord: the moment of the click counts.
        self.requests.put_nowait(_JoinRequest(member, answer))
        await asyncio.wait({acked})
        return ui.DROP

    def render(self, *, closed: bool = False) -> discord.Embed:
        """The monster's embed with the hunters (and, while gathering, the deadline)."""
        embed = self.embed.copy()
        embed.clear_fields()
        if closed:
            embed.description = (embed.description or "").replace(MSG_JOIN_HINT, "")
        value = ", ".join(f"<@{hunter.id}>" for hunter in self.hunters) or "-"
        if not closed and self._ends_at is not None:
            value += "\n" + MSG_JOIN_UNTIL.format(when=ui.when(self._ends_at))
        embed.add_field(name=ROSTER_NAME.format(count=len(self.hunters), limit=MAX_HUNTERS),
                        value=value[:1024], inline=False)
        return embed

    def start(self, hunters: list[Any]) -> None:
        """The gathering begins: show ``hunters`` (the same list object, kept up to date)."""
        self.hunters = hunters
        if self._painter is None and not self.view.closed:
            self._painter = asyncio.ensure_future(self._paint())

    def changed(self, seconds: float | None = None) -> None:
        """Repaint soon; ``seconds``: joining ends that many seconds from now."""
        if seconds is not None:
            self._ends_at = clock.utcnow() + timedelta(seconds=seconds)
        self._dirty.set()

    async def _paint(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self.view.refresh(embed=self.render())  # never raises
            await asyncio.sleep(ROSTER_EDIT_GAP)

    async def close(self) -> None:
        """Final list, button off, unanswered clicks told it is too late.  Never raises."""
        if self._painter is not None:
            self._painter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._painter
            self._painter = None
        await self.view.close(embed=self.render(closed=True) if self.hunters else self.embed)
        await _refuse_leftovers(self.requests)


async def _keep_panel(channel: Any, panel: _JoinPanel) -> None:
    """Remember the join button of the monster just shown on ``channel`` (for :func:`hunt_mobs`)."""
    old = _panels.pop(channel.id, None)
    _panels[channel.id] = panel
    if old is not None and old.bot is panel.bot:  # a monster nobody hunted (should not happen)
        await old.close()


def _take_panel(scene: "Scene") -> _JoinPanel | None:
    panel = _panels.pop(scene.channel.id, None)
    if panel is None or panel.bot is not scene.bot or panel.view.closed:
        return None
    return panel


async def _next_joiner(queue: asyncio.Queue, hunters: Sequence[Any], seconds: float, debug: bool) -> Any:
    """The member of the next acceptable join request within ``seconds``; None when nobody joins.

    Nothing in here waits for Discord: a refusal is delivered in the
    background, so a burst of repeated requests cannot push a request made in
    time past the deadline.  Requests already queued when the deadline passes
    are still read.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                request: _JoinRequest = queue.get_nowait()
            else:
                request = await asyncio.wait_for(queue.get(), remaining)
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return None
        if getattr(request.member, "bot", False):
            continue
        if _on_list(hunters, request.member, debug):
            if request.answer is not None:
                _answer_later(request.answer, MSG_ALREADY_HUNTING)
            continue
        return request.member


async def _gather_hunters(scene: "Scene", first: Any, panel: _JoinPanel | None = None) -> list[Any]:
    """``first`` plus up to two players who join; each join restarts the 8 s wait.

    Players join by typing ``!polowanie`` or with the button of ``panel``.
    Both are queued for the whole lobby (as in the boss lobbies): with one
    ``wait_for`` per join, a second ``!polowanie`` arriving right behind the
    first one was lost.
    """
    bot = scene.bot
    channel_id = scene.channel.id
    hunters = [first]
    queue: asyncio.Queue = panel.requests if panel is not None else asyncio.Queue()

    async def on_message(message: discord.Message) -> None:
        if message.author.bot or message.channel.id != channel_id:
            return
        if says_command(message.content, JOIN_WORD):  # "!polowanie", also with the old "$" prefix
            queue.put_nowait(_JoinRequest(message.author))

    gathering = _Gathering(queue, hunters, bool(bot.debug))
    bot.add_listener(on_message, "on_message")
    _gathering[channel_id] = gathering
    try:
        if panel is not None:
            panel.start(hunters)
        while len(hunters) < MAX_HUNTERS:
            seconds = timing.timeout(JOIN_TIME)
            if panel is not None:
                panel.changed(seconds)
            joiner = await _next_joiner(queue, hunters, seconds, bot.debug)
            if joiner is None:
                break
            hunters.append(joiner)
            log.info("Hunt: %s (%s) joined, %d hunters.", joiner, joiner.id, len(hunters))
    finally:
        if _gathering.get(channel_id) is gathering:
            del _gathering[channel_id]
        bot.remove_listener(on_message, "on_message")
        if panel is not None:
            await panel.close()
        else:  # e.g. a hub click that arrived right at the deadline of a hunt without a card
            await _refuse_leftovers(queue)
    return hunters


async def _settle_lost_hunt(scene: "Scene", hunters: Sequence[Any], rarity: int, ban_losers: bool,
                            is_player_boss: bool, player_boss: Any) -> None:
    """After a lost hunt: scrolls for the player who posed as the monster, bans for the losers."""
    bot = scene.bot
    boss_id = getattr(player_boss, "id", None)
    if is_player_boss and boss_id is not None:
        if any(hunter.id == boss_id for hunter in hunters):
            # Losing to "yourself" on purpose must not be a way to farm scrolls.
            log.info("Player monster %s took part in the lost hunt; no scrolls.", boss_id)
        else:
            await guarded(bot, f"{rarity + 1} zwojów dla gracza-potwora <@{boss_id}>",
                          _pets().assign_scroll(scene, rarity + 1, boss_id),
                          context="po przegranym polowaniu")
    if ban_losers:
        for hunter in hunters:
            await guarded(bot, f"rola martwego łowcy <@{hunter.id}>", boss.set_dead_hunters(bot, hunter),
                          context="po przegranym polowaniu")


async def hunt_mobs(scene: "Scene", rarity: int, is_player_boss: bool, player_boss: Any,
                    player: Any = None, *, progress: dict[str, Any] | None = None) -> bool | None:
    """Fight the monster shown by :func:`send_mob_image`.

    ``player`` (default: ``scene.author``) starts the hunt; up to two more
    players may join by typing ``!polowanie`` right away or with the "Dołącz do
    polowania" button of the monster's embed.  Every round one of
    the hunters, drawn at random, has to type the requested word in time; one
    mistake or one missed word loses the hunt (unless the pet protects).

    Returns True when the monster was killed, False when the hunters lost and
    None when the hunt was interrupted by repeated connection problems (the
    players are told, nobody is punished).  Technical failures before the kill
    propagate; after the kill the hunt is won whatever happens.
    """
    bot = scene.bot
    channel = scene.channel
    rarity = int(rarity)
    panel = _take_panel(scene)
    first = player if player is not None else scene.author
    if first is None:
        if panel is not None:
            await panel.close()
        raise ValueError("hunt_mobs needs a player: pass one or use a scene with an author")

    hunters = await _gather_hunters(scene, first, panel)
    mentions = _mentions(hunters)
    await scene.send((MSG_GROUP_START if len(hunters) > 1 else MSG_SOLO_START).format(mentions=mentions))

    mods = await modifiers.load_modifiers(bot)
    pets = {hunter.id: await pet_skills(bot, hunter.id) for hunter in hunters}
    await timing.pause(9)
    await challenge.countdown(channel)

    mob_hp = scaled_hp(random_boss_hp(rarity), mods["hp_boost_perc"] - mods["hp_reduced_perc"]
                       - pets[hunters[0].id]["LOWHP_PERC"])
    actions = modifiers.fight_actions(mods, MOB_ACTIONS, "Potwór")
    log.info("Hunt: %s vs a monster of rarity %s, hp %s.", [str(hunter) for hunter in hunters], rarity, mob_hp)

    fightlog.begin(channel, fightlog.KIND_HUNT, hunters, detail=rarity)
    started = time.monotonic()
    turn = 0
    while True:
        turn += 1
        word, prompt = actions[random.randint(0, len(actions) - 1)]
        hunter = random.choice(hunters)
        pet = pets[hunter.id]

        if not roll(pet["REPLACE_PERC"]):
            if turn == 1:
                limit = FIRST_ROUND_TIME
            else:
                limit = (5 - rarity) * (100 - mods["time_reduced_perc"] + pet["SLOW_PERC"]) / 100
            result = await fight_round(bot, channel, str(turn) + ". **" + str(hunter) + "**: " + prompt,
                                       players=hunters, limit=limit, word=word, asked=hunter, turn=turn)
            if result is None:
                log.warning("Hunt interrupted: too many disrupted rounds in a row.")
                await io.send_patient(channel, MSG_HUNT_INTERRUPTED, give_up_after=120)
                return None
            timed_out = result.outcome is Outcome.TIMEOUT
            correct = result.is_correct(word, by=hunter)
        else:
            fightlog.pet_attack(channel, turn, hunter, word)
            await scene.send("~~" + str(turn) + ". " + str(hunter) + ": " + prompt + "~~. " + MSG_PET_ATTACKS)
            timed_out = False
            correct = True

        if correct:
            if roll(pet["CRIT_PERC"]):
                turn += 1
            if turn >= mob_hp:
                break
            continue

        if roll(pet["DEF_PERC"]):
            fightlog.saved(channel)
            await scene.send(MSG_TOO_SLOW_PROTECTED if timed_out else MSG_MISTAKE_PROTECTED)
            continue

        # The hunt is lost - decided: from here on nothing gives the attempt back.
        fightlog.lost(channel, timed_out=timed_out)
        if progress is not None:
            progress["decided"] = True
        await say_lost(scene, MSG_TOO_SLOW_LOST if timed_out else MSG_MISTAKE_LOST)
        log.info("Hunt lost by %s (%s) in turn %s.", hunter, "timeout" if timed_out else "mistake", turn)
        await _settle_lost_hunt(scene, hunters, rarity, mods["ban_loser"] > 0, is_player_boss, player_boss)
        return False

    # -- the monster is dead: from here on nothing may turn the victory into a failure --
    fightlog.won(channel)
    if progress is not None:
        progress["decided"] = True
    fight_time = timedelta(seconds=time.monotonic() - started)
    log.info("Hunt won by %s in %.2f s.", [str(hunter) for hunter in hunters], fight_time.total_seconds())
    await guarded(bot, "ogłoszenie zwycięstwa", scene.send(MSG_WON.format(mentions=mentions)))
    await guarded(bot, "czas polowania", scene.send(fight_time_message(fight_time, mob_hp)))

    party_boost = PARTY_DROP_BOOST * (len(hunters) - 1)
    for hunter in hunters:
        await guarded(bot, f"punkty rankingowe <@{hunter.id}>",
                      database.update_ranking_table(bot, hunter.id, rarity, 0))
        await guarded(bot, f"licznik polowań <@{hunter.id}>", achievements.add_hunts(scene, hunter))
        drop_boost = mods["drop_boost_perc"] + pets[hunter.id]["DROP_PERC"] + party_boost
        await guarded(bot, f"łup <@{hunter.id}>", rand_loot(scene, rarity, hunter, drop_boost))
    return True


# -- !polowanie ------------------------------------------------------------------------

def roll_hunt_rarity() -> int:
    """Rarity of the monster of a daily hunt: 70 % normal, 29 % rare, 2 in 101 unique."""
    drawn = random.randint(0, 100)
    if 0 <= drawn < 70:
        return 0
    if 70 <= drawn <= 98:
        return 1
    return 2


async def _give_back_attempt(scene: "Scene", player: Any) -> None:
    """Take the player off the daily cooldown list and tell him (never raises)."""
    release_cooldown(scene.bot.store.daily_cd, player.id)
    log.info("Daily hunt of %s (%s) given back after a technical problem.", player, player.id)
    await io.send_patient(scene.channel, MSG_ATTEMPT_RETURNED.format(user_id=player.id), give_up_after=60)


async def daily_hunt(scene: "Scene") -> bool | None:
    """The whole ``!polowanie`` of ``scene.author``: cooldown, monster, fight.

    The caller has already checked the cooldown and holds the busy flag.  The
    cooldown is stored before the monster appears (as in the legacy bot); when
    the hunt ends because of a technical problem the attempt is given back -
    but only while the hunt is undecided: once the monster is dead (the
    rewards are being handed out) or the hunters have lost, the hunt counts,
    whatever happens next (a monster is fought once).
    Returns what :func:`hunt_mobs` returned.
    """
    bot = scene.bot
    player = scene.author
    if player is None:
        raise ValueError("daily_hunt needs a scene with an author")
    save_daily_to_file(bot, player.id)
    progress: dict[str, Any] = {"decided": False}
    try:
        rarity = roll_hunt_rarity()
        is_player_boss, boss_player = await send_mob_image(scene, rarity)
        result = await hunt_mobs(scene, rarity, is_player_boss, boss_player, progress=progress)
    except asyncio.CancelledError:
        if progress["decided"]:
            log.warning("Daily hunt of %s (%s) cancelled after it was decided; the attempt is kept.",
                        player, player.id)
        else:
            release_cooldown(bot.store.daily_cd, player.id)  # e.g. the bot is shutting down mid-hunt
        raise
    except Exception as exc:
        if not progress["decided"]:
            log.exception("Daily hunt of %s (%s) failed; giving the attempt back.", player, player.id)
            await _give_back_attempt(scene, player)
            raise
        # Decided already: the hunt counts.  Not re-raised - "something went wrong, try again"
        # would be the wrong thing to tell the player.
        log.exception("Daily hunt of %s (%s) failed after it was decided; the attempt is NOT given back.",
                      player, player.id)
        await io.send_patient(scene.channel, MSG_HUNT_CUT_SHORT.format(user_id=player.id), give_up_after=60)
        await bot.log_to_owner(LOG_HUNT_CUT_SHORT.format(user_id=player.id, error=type(exc).__name__),
                               ping=True)
        return False
    if result is None:
        await _give_back_attempt(scene, player)
    return result


async def start_hunt(scene: "Scene", *, refuse: Answer | None = None) -> bool:
    """The whole ``!polowanie`` command of ``scene.author`` (boss phase, cooldown, busy flag, the hunt).

    The command guards (boss channel, busy notice) run before; this checks
    everything again because a click (the hub panel) has no command guards:

    * while a hunt gathers hunters on the channel, a click joins it (as typing
      ``!polowanie`` does; somebody on the list already is told so);
      a typed command is silent here (the hunt's own listener has already
      seen the word);
    * ``refuse`` answers a click (``ui.reply``); None (a typed command) answers on
      the channel as before.

    Returns True when a hunt took place.  The hunt itself runs to its end
    inside this call (minutes): acknowledge a click before calling it.
    """
    bot = scene.bot
    state = bot.state
    player = scene.author
    if player is None:
        raise ValueError("start_hunt needs a scene with an author")
    if is_gathering(scene.channel):
        if refuse is not None:
            if is_hunting(scene.channel, player):
                await refuse(MSG_ALREADY_HUNTING)  # here, so the gathering never waits for it
            else:
                request_join(scene.channel, player, refuse)
        return False
    if state.busy:  # somebody took the channel between the guard and here
        if refuse is None:
            await guards.notify_busy(scene)  # type: ignore[arg-type] - needs only ``.channel``
        else:
            await refuse(ui.MSG_BUSY)
        return False
    phase = state.boss_phase
    on_cd = load_daily_from_file(bot, player.id)
    # No await between the checks above and hold_busy: two players cannot both start.
    if (not state.boss_blocks_activities or bot.debug) and not on_cd:
        with state.hold_busy("hunt"):
            await daily_hunt(scene)
        return True

    async def tell(text: str) -> None:
        if refuse is not None:
            await refuse(text)
        else:
            await io.send(scene.channel, text)

    if phase is BossPhase.PRE_SPAWN:
        await tell(MSG_BOSS_COMING)
    elif phase is BossPhase.VISIBLE:
        await tell(MSG_BOSS_HERE)
    elif phase > BossPhase.VISIBLE:
        if refuse is not None:  # a typed command stays silent during a boss fight, as before
            await refuse(MSG_BOSS_FIGHT)
    elif on_cd:
        await tell(cooldown_text())
    return False


def cooldown_text() -> str:
    """"Come back later" with Discord's live countdown to the next daily reset."""
    return MSG_ON_COOLDOWN.format(when=ui.when(next_daily_reset()))


# -- when do the cooldowns end ---------------------------------------------------------

#: The scheduled resets, Warsaw time - the moments of the jobs in ``cogs/schedule.py``
#: (``tests/test_ui_daily.py`` checks that they agree).
DAILY_RESET_AT = (2, 30)        # every day: the daily hunt and the daily skill gems
SEASON_END_AT = (0, 15, 0)      # Monday 15:00: ranking, tower, weekly gems - and the daily lists again


def next_season_end(after: datetime | None = None) -> datetime:
    """The next end of the ranking season (also the weekly reset of the tower and the weekly gems)."""
    weekday, hour, minute = SEASON_END_AT
    return clock.next_weekly(weekday, hour, minute, after=after)


def next_daily_reset(after: datetime | None = None) -> datetime:
    """The next time the daily lists (hunt, daily gems) are cleared: 02:30 or the season end, if earlier."""
    hour, minute = DAILY_RESET_AT
    return min(clock.next_daily(hour, minute, after=after), next_season_end(after))


# -- the daily cooldown list -----------------------------------------------------------

def save_daily_to_file(bot: "AlterBot", player_id: int | str) -> None:
    """Remember that the player used today's hunt (no duplicates)."""
    if not bot.store.daily_cd.contains(player_id):
        bot.store.daily_cd.add(player_id)


def load_daily_from_file(bot: "AlterBot", player_id: int | str) -> bool:
    """True when the player already hunted today."""
    return bot.store.daily_cd.contains(player_id)


async def clear_daily_file(bot: "AlterBot") -> None:
    """Everybody may hunt again (daily reset, season end, a prayer at the shrine)."""
    bot.store.daily_cd.clear()
    log.info("Daily hunting cooldown reset.")
    await bot.log_to_owner(LOG_DAILY_RESET)


async def remove_player_daily_file(bot: "AlterBot", player_id_list: Iterable[Any]) -> int:
    """Let the given players (ids or members) hunt again; returns how many were on the list."""
    ids = [str(getattr(entry, "id", entry)).strip() for entry in player_id_list]
    ids = [entry for entry in ids if entry]
    if not ids:
        return 0
    removed = bot.store.daily_cd.remove(ids)
    log.info("Daily hunting cooldown removed for %d of %d players.", removed, len(ids))
    return removed
