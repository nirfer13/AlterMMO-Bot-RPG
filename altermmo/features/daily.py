"""The daily monster hunt (``$polowanie``): monster picture, typing fight, monster loot.

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
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Iterable, Sequence

import discord

from ..core import challenge, io, timing
from ..core.challenge import Outcome
from ..settings import ASSETS_DIR, CONFIG_DIR
from . import achievements, boss, database, modifiers

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- numbers of the game ------------------------------------------------------------

#: Monster life by rarity: (min, max) of ``random.randint``; other rarities use the last one.
MOB_HP_RANGES: dict[int, tuple[int, int]] = {0: (3, 5), 1: (6, 9), 2: (9, 12)}
MOB_HP_FALLBACK = (9, 12)

#: What another player types to join a hunt that is about to start.
JOIN_WORD = "$polowanie"
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

MSG_JOIN_HINT = "\n\n*Inny gracz może dołączyć do polowania wpisując natychmiast $polowanie*."

MSG_SOLO_START = ("Polowanie się rozpoczyna {mentions}<:REEeee:790963160495947856>! Wpisz pojawiające się "
                  "słowa tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! Wpisz słowa bez "
                  "spacji! Przygotuj się!")
MSG_GROUP_START = ("Rozpoczęliście polowanie {mentions}<:REEeee:790963160495947856>! Wpiszcie **słowa "
                   "przypisane do Was** tak szybko, jak to możliwe! Wielkość liter nie ma znaczenia! "
                   "Wpiszcie słowa bez spacji! Przygotujcie się!")
MSG_WON = "Brawo {mentions}<:ok:990161663053422592> Potwór pokonany!"
MSG_MISTAKE_LOST = ("Pomyliłeś się! <:PepeHands:783992337377918986> Spróbuj ponownie jutro lub poczekaj na "
                    "modlitwę Crafterów i Patronów! <:RIP:912797982917816341>\n\n"
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
MSG_ATTEMPT_RETURNED = ("<@{user_id}>, z powodu problemów technicznych Twoje dzisiejsze polowanie się nie "
                        "liczy - możesz zapolować ponownie!")


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
                      limit: float) -> challenge.RoundResult | None:
    """One "type the word" round that never blames the players for our lag.

    A disrupted round is announced and asked again; None after
    ``challenge.MAX_CONSECUTIVE_DISRUPTIONS`` disrupted rounds in a row (the
    caller then ends the activity without punishing anybody).
    """
    for attempt in range(1, challenge.MAX_CONSECUTIVE_DISRUPTIONS + 1):
        result = await challenge.ask(bot, channel, prompt, players=players,
                                     timeout=timing.timeout(max(float(limit), 0.0)))
        if result.outcome is not Outcome.DISRUPTED:
            return result
        log.warning("Fight round disrupted (%d/%d in a row).", attempt, challenge.MAX_CONSECUTIVE_DISRUPTIONS)
        if attempt < challenge.MAX_CONSECUTIVE_DISRUPTIONS:
            await challenge.notify_disrupted(channel)
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


async def _grant_loot(scene: "Scene", hunter: Any, loot: dict[str, Any]) -> bool:
    """Hand one loot item to ``hunter``; False when there was nothing to give.

    (A player who already has a pet gets no egg, a pet at the top level cannot
    level up...).  Items without a mechanic of their own are text only.
    """
    loot_id = loot["id"]
    if loot_id == LOOT_EGG:
        return bool(await _pets().assign_pet(scene, hunter))
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


async def _drop(scene: "Scene", hunter: Any, loot: dict[str, Any], *, bonus: bool) -> str:
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
    if not granted:
        log.info("Monster loot %s rolled for %s but there was nothing to grant.", loot["id"], hunter.id)
        return ""
    return "👉 " + loot["descr"] + (" (Bonus)\n" if bonus else "\n")


async def rand_loot(scene: "Scene", rarity: int, hunter: Any, boost_percent: float) -> str:
    """Draw the loot of a monster of ``rarity`` for ``hunter``, grant it and post the drop embed.

    ``boost_percent`` raises every weight; a weight above 100 % is a certain
    drop plus a chance for a second, "(Bonus)" one.  Returns the drop message
    (one ``👉 ...`` line per item).
    """
    bot = scene.bot
    boost = int(boost_percent)
    drop_message = ""
    for loot in _loot_table(int(rarity)):
        weight = (boost / 100 + 1) * loot["weight"]
        if weight > 100:
            drop_message += await _drop(scene, hunter, loot, bonus=False)
            if random.random() * 100 <= weight - 100:
                drop_message += await _drop(scene, hunter, loot, bonus=True)
        elif random.random() * 100 <= weight:
            drop_message += await _drop(scene, hunter, loot, bonus=False)

    embed = discord.Embed(title="Drop - " + str(hunter), url=LOOT_IMAGE,
                          description="Otrzymałeś:\n" + drop_message, color=0xFCDB03)
    embed.set_thumbnail(url=LOOT_IMAGE)
    embed.set_footer(text="Gratulacje!")
    try:
        await scene.send(embed=embed)
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
    monster.  Pass both on to :func:`hunt_mobs`.
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
    await scene.send(embed=embed)
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

#: Channels on which a hunt is gathering hunters right now.
_gathering: set[int] = set()


def is_gathering(channel: Any) -> bool:
    """True while a hunt on ``channel`` waits for more hunters.

    The ``$polowanie`` command asks: typed in that moment the word joins the
    running hunt, so the command itself must neither start a second hunt nor
    answer with the "bot is busy" notice.
    """
    return getattr(channel, "id", None) in _gathering


def _mentions(hunters: Sequence[Any]) -> str:
    """Legacy ``player_list_string``: ``<@1> <@2> `` (a space after every mention)."""
    return "".join(f"<@{hunter.id}> " for hunter in hunters)


async def _next_joiner(queue: asyncio.Queue, hunters: Sequence[Any], seconds: float, debug: bool) -> Any:
    """The author of the next ``$polowanie`` within ``seconds``; None when nobody joins."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message = await asyncio.wait_for(queue.get(), remaining)
        except asyncio.TimeoutError:
            return None
        if str(message.content).strip().lower() != JOIN_WORD:
            continue
        if not debug and any(hunter.id == message.author.id for hunter in hunters):
            continue  # already on the list (debug mode lets one tester fill the party)
        return message.author


async def _gather_hunters(scene: "Scene", first: Any) -> list[Any]:
    """``first`` plus up to two players who type ``$polowanie``; each join restarts the 8 s wait.

    The players' messages are queued for the whole lobby (as in the boss
    lobbies): with one ``wait_for`` per join, a second ``$polowanie`` arriving
    right behind the first one was lost.
    """
    bot = scene.bot
    channel_id = scene.channel.id
    hunters = [first]
    queue: asyncio.Queue = asyncio.Queue()

    async def on_message(message: discord.Message) -> None:
        if message.author.bot or message.channel.id != channel_id:
            return
        queue.put_nowait(message)

    bot.add_listener(on_message, "on_message")
    _gathering.add(channel_id)
    try:
        while len(hunters) < MAX_HUNTERS:
            joiner = await _next_joiner(queue, hunters, timing.timeout(JOIN_TIME), bot.debug)
            if joiner is None:
                break
            hunters.append(joiner)
            log.info("Hunt: %s (%s) joined, %d hunters.", joiner, joiner.id, len(hunters))
    finally:
        _gathering.discard(channel_id)
        bot.remove_listener(on_message, "on_message")
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
            await boss.set_dead_hunters(bot, hunter)


async def hunt_mobs(scene: "Scene", rarity: int, is_player_boss: bool, player_boss: Any,
                    player: Any = None) -> bool | None:
    """Fight the monster shown by :func:`send_mob_image`.

    ``player`` (default: ``scene.author``) starts the hunt; up to two more
    players may join by typing ``$polowanie`` right away.  Every round one of
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
    first = player if player is not None else scene.author
    if first is None:
        raise ValueError("hunt_mobs needs a player: pass one or use a scene with an author")

    hunters = await _gather_hunters(scene, first)
    mentions = _mentions(hunters)
    await scene.send((MSG_GROUP_START if len(hunters) > 1 else MSG_SOLO_START).format(mentions=mentions))

    mods = await modifiers.load_modifiers(bot)
    pets = {hunter.id: await pet_skills(bot, hunter.id) for hunter in hunters}
    await timing.pause(9)
    await challenge.countdown(channel)

    mob_hp = scaled_hp(random_boss_hp(rarity), mods["hp_boost_perc"] - mods["hp_reduced_perc"]
                       - pets[hunters[0].id]["LOWHP_PERC"])
    log.info("Hunt: %s vs a monster of rarity %s, hp %s.", [str(hunter) for hunter in hunters], rarity, mob_hp)

    started = time.monotonic()
    turn = 0
    while True:
        turn += 1
        word, prompt = MOB_ACTIONS[random.randint(0, len(MOB_ACTIONS) - 1)]
        hunter = random.choice(hunters)
        pet = pets[hunter.id]

        if not roll(pet["REPLACE_PERC"]):
            if turn == 1:
                limit = FIRST_ROUND_TIME
            else:
                limit = (5 - rarity) * (100 - mods["time_reduced_perc"] + pet["SLOW_PERC"]) / 100
            result = await fight_round(bot, channel, str(turn) + ". **" + str(hunter) + "**: " + prompt,
                                       players=hunters, limit=limit)
            if result is None:
                log.warning("Hunt interrupted: too many disrupted rounds in a row.")
                await io.send_patient(channel, MSG_HUNT_INTERRUPTED, give_up_after=120)
                return None
            timed_out = result.outcome is Outcome.TIMEOUT
            correct = result.is_correct(word, by=hunter)
        else:
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
            await scene.send(MSG_TOO_SLOW_PROTECTED if timed_out else MSG_MISTAKE_PROTECTED)
            continue

        await scene.send(MSG_TOO_SLOW_LOST if timed_out else MSG_MISTAKE_LOST)
        log.info("Hunt lost by %s (%s) in turn %s.", hunter, "timeout" if timed_out else "mistake", turn)
        await _settle_lost_hunt(scene, hunters, rarity, mods["ban_loser"] > 0, is_player_boss, player_boss)
        return False

    # -- the monster is dead: from here on nothing may turn the victory into a failure --
    fight_time = timedelta(seconds=time.monotonic() - started)
    log.info("Hunt won by %s in %.2f s.", [str(hunter) for hunter in hunters], fight_time.total_seconds())
    await guarded(bot, "ogłoszenie zwycięstwa", scene.send(MSG_WON.format(mentions=mentions)))
    await guarded(bot, "czas polowania", scene.send(fight_time_message(fight_time, mob_hp)))

    party_boost = PARTY_DROP_BOOST * (len(hunters) - 1)
    for hunter in hunters:
        await guarded(bot, f"punkty rankingowe <@{hunter.id}>",
                      database.update_ranking_table(bot, hunter.id, rarity, 0))
        await guarded(bot, f"osiągnięcie za polowania <@{hunter.id}>", achievements.add_hunts(scene, hunter))
        if rarity == 2:
            await guarded(bot, f"osiągnięcie za unikalnego potwora <@{hunter.id}>",
                          achievements.unicorn(scene, hunter))
        drop_boost = mods["drop_boost_perc"] + pets[hunter.id]["DROP_PERC"] + party_boost
        await guarded(bot, f"łup <@{hunter.id}>", rand_loot(scene, rarity, hunter, drop_boost))
    return True


# -- $polowanie ------------------------------------------------------------------------

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
    """The whole ``$polowanie`` of ``scene.author``: cooldown, monster, fight.

    The caller has already checked the cooldown and holds the busy flag.  The
    cooldown is stored before the monster appears (as in the legacy bot); when
    the hunt ends because of a technical problem the attempt is given back.
    Returns what :func:`hunt_mobs` returned.
    """
    bot = scene.bot
    player = scene.author
    if player is None:
        raise ValueError("daily_hunt needs a scene with an author")
    save_daily_to_file(bot, player.id)
    try:
        rarity = roll_hunt_rarity()
        is_player_boss, boss_player = await send_mob_image(scene, rarity)
        result = await hunt_mobs(scene, rarity, is_player_boss, boss_player)
    except asyncio.CancelledError:
        release_cooldown(bot.store.daily_cd, player.id)  # e.g. the bot is shutting down mid-hunt
        raise
    except Exception:
        log.exception("Daily hunt of %s (%s) failed; giving the attempt back.", player, player.id)
        await _give_back_attempt(scene, player)
        raise
    if result is None:
        await _give_back_attempt(scene, player)
    return result


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
