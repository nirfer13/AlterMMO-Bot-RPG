"""Dova's prizes: the Kość Dovy (``!kosc``) and the Karta Dovy (``!karta``).

Owner's request (2026-11-26, ``items_decisions.md`` §12 and its update):
Dova, the host of the cups (``events.spawn_hazard``), now and then pays the
player who beat him with more than scrolls.  A won game keeps its 5 scrolls and
its monster loot and rolls two prizes besides, each on its own
(:data:`DIE_CHANCE`, :data:`CARD_CHANCE`; :func:`roll_prizes`).  Only Dova gives
them - no loot table has a row of them, the merchant never sells them
(``items.DOVA_PRIZES``).  They are stored like every item (``PLAYER_ITEMS``, no
cap), shown by ``!przedmioty`` and given by the owner's ``!DajPrzedmiot``.

When they work (:func:`window`):

* **the next boss** - the scheduled boss that is coming and has not shown
  itself yet: phase SCHEDULED or WAITING, spawning on, no summoned boss on the
  channel.  The Karta Dovy also earlier (IDLE): the next boss reads the
  modifiers whenever it comes, so the card works up to the "wind";
* **the boss on the channel** (VISIBLE: shown, nobody has attacked it yet) -
  the scheduled one and a summoned one alike, a player posing as the boss
  included.  It is shown again with what changed, the way a spawn shows it
  (``BossCycle.show_again``): the old picture and its "Zaatakuj" button go;
* never while the boss is arriving (PRE_SPAWN) or being fought (ENGAGED /
  FIGHTING): the player is told so (silence during a typed fight) and nothing
  is taken.

**Kość Dovy**: a new base rarity drawn with the schedule's own distribution - a
fresh delay drawn like the schedule draws it, turned into a rarity by
``boss.rarity_for_respawn_time`` - while the arrival time stays as it is.  For
the next boss it replaces the drawn rarity (in memory and in the ``BOSS`` row);
the shrine's boost and the hour still apply when it comes
(``boss_cycle.final_rarity``).  For the boss on the channel those rules apply
at once (the cap, the off-peak legendary -> mythic).  A legendary boss then
needs its usual party; a summoned boss keeps its origin and its stay.

**Karta Dovy**: the boss's modifiers (``bossModifier.json``) are thrown away
and drawn anew - as many outcomes as were on (every key with a value other
than 0, the player-boss counted once), at least one - from the prayer's pool
with the prayer's values (``modifiers.draw_modifier``).  A drawn rest resets
the hunts as a prayer does.  On the boss already on the channel the rarity
boost and the player-boss are left out of the pool (they could not change it
any more); they still count and are cleared like the rest.

**The order of a use** (the once-only rules of the items): the gate of a
gameplay command (the boss channel, silent during a typed fight, the busy
notice - ``cogs/items.py``), the window, the channel held busy for the use (two
uses never interleave, :data:`BUSY_OWNER`; nobody attacks the boss on the
channel meanwhile - he gets the busy notice), the item taken (conditional,
never retried), the window checked again WITHOUT an ``await``, the draw and the
write - still without one - and only then the messages, the boss shown anew,
and last the ``BOSS`` row (it matters only after a restart).  When the window
moved during the take (the boss started arriving, a summoned boss left, a
reset) the item goes back and nothing changes; once the write is done nothing
is given back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import TYPE_CHECKING, Any

import discord

from ..core import challenge, clock, guards, ui
from ..core.state import BossPhase
from . import boss, boss_cycle, database, items, modifiers

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

#: The chance of each prize on a won game of cups (independent of each other).
DIE_CHANCE = 0.30
CARD_CHANCE = 0.30

#: Who holds the busy flag while an item of Dova works: a second use gets the busy notice, and so does an
#: attack on the boss on the channel (``BossCycle.attack``) - it is being changed and shown anew.
BUSY_OWNER = boss_cycle.BOSS_CHANGING

#: When an item works (:func:`window`).
NEXT = "next"            # the boss that is coming (SCHEDULED / WAITING; the card also IDLE)
VISIBLE = "visible"      # the boss on the channel, unclaimed
ARRIVING = "arriving"    # PRE_SPAWN: the boss is arriving right now
FIGHT = "fight"          # ENGAGED / FIGHTING
CLOSED = "closed"        # the Kość Dovy without a scheduled boss to change (IDLE, spawning off)

#: Left out of the Karta Dovy's pool for the boss on the channel (it could not use them any more).
LEFT_OUT_ON_VISIBLE: tuple[str, ...] = ("rarity_boost", "player_id")

#: The colour of Dova's own embed (``events.spawn_hazard``).
DOVA_COLOUR = 0x827B7A

# -- the prizes on a won game of cups -----------------------------------------------------------

#: Appended to Dova's "you won" line (``{prizes}``: the emoji and name of each prize).
MSG_PRIZE = ("Dova sięga pod stół, mruczy coś o przegranym zakładzie i wciska Ci w dłoń: {prizes}. Co z tym zrobić, "
             "podpowie **!przedmioty**.")
PRIZE = "{emoji} **{name}**"
PRIZES_JOIN = " i "
#: To the owner (with a ping): a prize could not be stored, or nobody knows whether it was.
LOG_PRIZE_UNSURE = ("Nagroda Dovy ({name}) dla gracza <@{player_id}>: baza danych nie odpowiedziała (`{error}`) - "
                    "nie wiadomo, czy trafiła do sakwy. Sprawdź PLAYER_ITEMS i w razie potrzeby daj ją: "
                    "`!DajPrzedmiot @gracz {key} 1`")

# -- the uses -----------------------------------------------------------------------------------

MSG_DIE_NEXT = ("🎲 <@{player_id}> rzuca **Kością Dovy**... Kość toczy się po stole, podskakuje i wypada: "
                "{emoji} **{rarity}**! Taki boss nadejdzie jako następny. *Kapliczka i pora dnia wciąż mogą to "
                "zmienić.*")
MSG_DIE_VISIBLE = ("🎲 <@{player_id}> rzuca **Kością Dovy**... Kość toczy się po stole, podskakuje i wypada: "
                   "{emoji} **{rarity}**! Boss na kanale zmienia się na oczach wszystkich!")
#: The die landed on the rarity the boss on the channel already has: nothing changes, nothing is re-posted.
MSG_DIE_SAME = ("🎲 <@{player_id}> rzuca **Kością Dovy**... Kość toczy się po stole, podskakuje i wypada: "
                "{emoji} **{rarity}** - dokładnie to, co już stoi na kanale! Dova chichocze pod nosem, a boss "
                "nawet nie drgnie.")
MSG_DIE_CLOSED = ("Kość Dovy zadziała, gdy następny boss będzie już w drodze albo stanie na kanale, "
                  "<@{player_id}>.")
MSG_ARRIVING = "Boss właśnie nadciąga, <@{player_id}> - poczekaj, aż się pokaże."
MSG_FIGHT = "Walka z bossem już trwa, <@{player_id}> - za późno na sztuczki Dovy."
#: The boss on the channel was claimed while the item was being taken (``{name}``: the item's name).
MSG_TOO_LATE = "Ktoś był szybszy i już stanął do walki, <@{player_id}> - {name} wraca do Twojej sakwy."
#: The window moved otherwise during the take (the boss started arriving, left, was reset...).
MSG_MOVED = "Na kanale bossa coś się właśnie zmieniło, <@{player_id}> - {name} wraca do Twojej sakwy."
MSG_NO_DIE = ("Nie masz żadnej **Kości Dovy**, <@{player_id}>. Dova od kubków nosi takie w rękawie - podobno dla "
              "tych, którzy go ograją.")
MSG_NO_CARD = ("Nie masz żadnej **Karty Dovy**, <@{player_id}>. Dova od kubków nosi takie w rękawie - podobno dla "
               "tych, którzy go ograją.")
CARD_TITLE = "🃏 Karta Dovy!"
CARD_HEAD = ("<@{player_id}> tasuje **Kartę Dovy**... Stare błogosławieństwa i klątwy następnego bossa rozwiewają "
             "się, a na ich miejsce przychodzą nowe:")
CARD_HEAD_EMPTY = ("<@{player_id}> tasuje **Kartę Dovy**... Następny boss nie miał żadnych perków - Karta Dovy "
                   "dorzuca mu jeden:")
CARD_HEAD_VISIBLE = ("<@{player_id}> tasuje **Kartę Dovy**... Stare błogosławieństwa i klątwy bossa na kanale "
                     "rozwiewają się, a na ich miejsce przychodzą nowe:")
CARD_HEAD_VISIBLE_EMPTY = ("<@{player_id}> tasuje **Kartę Dovy**... Boss na kanale nie miał żadnych perków - Karta "
                           "Dovy dorzuca mu jeden:")
#: The card drew only a rest - no perk of the boss (``{whose}`` / ``{who}``: :data:`WHOSE` / :data:`WHO`).
CARD_HEAD_REST = ("<@{player_id}> tasuje **Kartę Dovy**... Stare błogosławieństwa i klątwy {whose} rozwiewają się, "
                  "a zamiast nowych karta przynosi wszystkim chwilę wytchnienia:")
CARD_HEAD_REST_EMPTY = ("<@{player_id}> tasuje **Kartę Dovy**... {who} nadal nie ma żadnych perków - zamiast nich "
                        "karta przynosi wszystkim chwilę wytchnienia:")
#: ...and even the rest could not be given (the hunts' file failed; the owner is told).
CARD_FIZZLED = ("<@{player_id}> tasuje **Kartę Dovy**... Stare błogosławieństwa i klątwy {whose} rozwiewają się, "
                "ale na ich miejsce nic nie przychodzi - karta zatliła się i zgasła jak mokra hubka.")
CARD_FIZZLED_EMPTY = ("<@{player_id}> tasuje **Kartę Dovy**... {who} wciąż nie ma żadnych perków - karta "
                      "zatliła się i zgasła jak mokra hubka.")
#: Which boss the card's words are about: the next one, or the one on the channel (``visible``).
WHOSE = {False: "następnego bossa", True: "bossa na kanale"}
WHO = {False: "Następny boss", True: "Boss na kanale"}
#: To the owner.
LOG_TAKE_UNSURE = ("Nie wiadomo, czy {name} gracza <@{player_id}> została zabrana - baza danych nie odpowiedziała "
                   "(`{error}`), nic się nie zmieniło. Sprawdź PLAYER_ITEMS i w razie potrzeby oddaj ją: "
                   "`!DajPrzedmiot @gracz {key} 1`")
LOG_NOT_REFUNDED = ("{name} gracza <@{player_id}> NIE wróciła do niego (`{error}`), choć nie zadziałała - oddaj ją: "
                    "`!DajPrzedmiot @gracz {key} 1`")
LOG_BOSS_ROW = ("Kość Dovy zmieniła rzadkość nadchodzącego bossa na {rarity}, ale zapis w tabeli BOSS się nie udał "
                "(`{error}`) - po restarcie bota boss przyjdzie ze starą rzadkością.")
LOG_BOSS_ROW_CARD = ("Karta Dovy zdjęła z bossa na kanale premię rzadkości z kapliczki, ale zapis jego rzadkości "
                     "({rarity}) w tabeli BOSS się nie udał (`{error}`) - po restarcie bota boss wróci z niższą "
                     "rzadkością (bez tej premii).")
LOG_HUNTS_NOT_RESET = ("Karta Dovy wylosowała odpoczynek łowców, ale nie udało się wyzerować polowań (`{error}`) - "
                       "w razie potrzeby zrób to ręcznie.")


# -- the prizes -----------------------------------------------------------------------------------

def prize_line(prizes: list[items.Item]) -> str:
    """The line appended to Dova's "you won" for ``prizes`` ('' for none)."""
    if not prizes:
        return ""
    return MSG_PRIZE.format(prizes=PRIZES_JOIN.join(PRIZE.format(emoji=item.emoji, name=item.name)
                                                    for item in prizes))


async def roll_prizes(bot: "AlterBot", player: Any) -> str:
    """The prizes of a won game of cups for ``player``: rolled, stored, and the line that tells him.  Never raises.

    Both are rolled every time, each on its own (two ``random.random()``, the
    Kość first).  A prize is stored before it is told (claim first, reward
    afterwards); one the store cannot take (the table is not there) is left
    out with a log line; one the database did not answer for is left out of
    the line and the owner is pinged (taken or not is unknown - he checks).
    '' when nothing came.
    """
    rolls = [(items.DOVA_DIE, random.random() < DIE_CHANCE), (items.DOVA_CARD, random.random() < CARD_CHANCE)]
    won = [items.BY_KEY[key] for key, hit in rolls if hit]
    player_id = int(getattr(player, "id", player))
    if not won:
        return ""
    try:
        ready = await items.table_ready(bot)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the win itself stands
        log.exception("Dova: the item store could not be asked for the prizes of %s.", player_id)
        ready = False
    if not ready:
        log.warning("Dova: the prizes %s of %s are left out: the item store is not there.",
                    [item.key for item in won], player_id)
        return ""
    given: list[items.Item] = []
    for item in won:
        try:
            left = await items.grant(bot, player_id, item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - given or not: unknown, the owner checks
            log.exception("Dova: the prize %s of %s could not be stored.", item.key, player_id)
            with contextlib.suppress(Exception):
                await bot.log_to_owner(LOG_PRIZE_UNSURE.format(name=item.name, player_id=player_id,
                                                               error=type(exc).__name__, key=item.key), ping=True)
            continue
        log.info("Dova: %s won the prize %s (has %s now).", player_id, item.key, left)
        given.append(item)
    return prize_line(given)


# -- when the items work --------------------------------------------------------------------------

def window(bot: "AlterBot", *, card: bool) -> str:
    """Where the boss cycle stands for an item of Dova now (:data:`NEXT`, :data:`VISIBLE`...).  Synchronous.

    ``card``: the Karta Dovy - it also works before a boss is scheduled (the
    next boss reads the modifiers whenever it comes); the Kość Dovy needs a
    scheduled boss that is coming (``BossCycle.next_boss_waits``).
    """
    phase = bot.state.boss_phase
    if phase is BossPhase.VISIBLE:
        return VISIBLE
    if phase is BossPhase.PRE_SPAWN:
        return ARRIVING
    if phase in (BossPhase.ENGAGED, BossPhase.FIGHTING):
        return FIGHT
    if card or bot.boss_cycle.next_boss_waits():
        return NEXT
    return CLOSED


def _refusal(where: str) -> str | None:
    """The refusal for an item used outside its window (``{player_id}`` to fill), None inside it."""
    if where == ARRIVING:
        return MSG_ARRIVING
    if where == FIGHT:
        return MSG_FIGHT
    if where == CLOSED:
        return MSG_DIE_CLOSED
    return None


async def _say(scene: "Scene", text: str | None = None, **kwargs: Any) -> Any:
    """Post on the channel; only the acting player is pinged; nothing during a typed fight.  Never raises."""
    if challenge.fight_on(scene.channel):
        return None
    mentions = discord.AllowedMentions(everyone=False, roles=False,
                                       users=[discord.Object(id=int(scene.author.id))])
    try:
        return await scene.send(text, allowed_mentions=mentions, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - what happened, happened
        log.exception("Dova: a line could not be posted.")
        return None


async def _take(scene: "Scene", item: items.Item) -> int | None:
    """Take one ``item`` from the acting player: the count left, or None (he has none, or it failed - told).

    Conditional and never retried (``items.take``).  A database that did not
    answer the take gives nothing back on a guess: the owner is pinged.
    """
    bot, player_id = scene.bot, int(scene.author.id)
    try:
        ready = await items.table_ready(bot)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - nothing was taken
        log.exception("Dova: the item store could not be asked for the %s of %s.", item.key, player_id)
        await _say(scene, ui.MSG_FAILED)
        return None
    try:
        left = await items.take(bot, player_id, item) if ready else None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - taken or not: unknown, the owner checks
        log.exception("Dova: the %s of %s could not be taken.", item.key, player_id)
        with contextlib.suppress(Exception):
            await bot.log_to_owner(LOG_TAKE_UNSURE.format(name=item.name, player_id=player_id,
                                                          error=type(exc).__name__, key=item.key), ping=True)
        await _say(scene, ui.MSG_FAILED)
        return None
    if left is None:
        text = MSG_NO_DIE if item.key == items.DOVA_DIE else MSG_NO_CARD
        await _say(scene, text.format(player_id=player_id))
    return left


async def _give_back(bot: "AlterBot", player_id: int, item: items.Item) -> bool:
    """The item of a use that did not happen goes back (``items.grant``).  Never raises; a failure pings the owner."""
    try:
        await items.grant(bot, player_id, item)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Dova: the %s of %s could not be given back.", item.key, player_id)
        with contextlib.suppress(Exception):
            await bot.log_to_owner(LOG_NOT_REFUNDED.format(name=item.name, player_id=player_id,
                                                           error=type(exc).__name__, key=item.key), ping=True)
        return False


async def throw_die(scene: "Scene") -> bool:
    """``!kosc``: the acting player throws a Kość Dovy; True when the rarity was re-rolled."""
    return await _use(scene, items.BY_KEY[items.DOVA_DIE])


async def shuffle_card(scene: "Scene") -> bool:
    """``!karta``: the acting player shuffles a Karta Dovy; True when the modifiers were drawn anew."""
    return await _use(scene, items.BY_KEY[items.DOVA_CARD])


async def _use(scene: "Scene", item: items.Item) -> bool:
    """One use of a Kość / Karta Dovy, in the order of the module docstring."""
    bot, player_id = scene.bot, int(scene.author.id)
    card = item.key == items.DOVA_CARD
    where = window(bot, card=card)
    refusal = _refusal(where)
    if refusal is not None:
        await _say(scene, refusal.format(player_id=player_id))
        return False
    state = bot.state
    if state.busy:  # (taken since the command's check: the usual notice)
        await guards.notify_busy(scene)  # type: ignore[arg-type] - needs only ``.channel``
        return False
    cycle = bot.boss_cycle
    with state.hold_busy(BUSY_OWNER):
        token = cycle.boss_token()
        if await _take(scene, item) is None:
            return False
        # -- no await from here until the write is done ----------------------------------------
        now = window(bot, card=card)
        if now != where or (where == VISIBLE and cycle.boss_token() != token):
            log.info("Dova: the %s of %s goes back - the window moved from %s to %s.", item.key, player_id,
                     where, now)
            await _give_back(bot, player_id, item)
            # (Only an attack is "somebody was faster": a summoned boss leaving passes through ENGAGED too.)
            text = MSG_TOO_LATE if cycle.claimed_since(token) else MSG_MOVED
            await _say(scene, text.format(player_id=player_id, name=item.name))
            return False
        # (Awaiting a coroutine runs it at once, up to its first own await: the effects draw and write
        # first, synchronously, and only then wait for anything.)
        if card:
            await _shuffle(scene, visible=where == VISIBLE, token=token)
        else:
            await _throw(scene, visible=where == VISIBLE, token=token)
    return True


# -- the Kość Dovy --------------------------------------------------------------------------------

def draw_base_rarity(bot: "AlterBot") -> int:
    """A base rarity drawn with the schedule's own distribution (``BossCycle._phase_idle``).  Synchronous.

    A fresh delay drawn exactly as the schedule draws one, turned into a rarity
    by ``boss.rarity_for_respawn_time``; the delay itself is thrown away (the
    arrival of the boss never moves).
    """
    timings = bot.timings
    delay = random.randint(timings.respawn_min, timings.respawn_max) * timings.respawn_multiplier
    return int(boss.rarity_for_respawn_time(delay, debug=bot.debug))


async def _throw(scene: "Scene", *, visible: bool, token: int) -> None:
    """The Kość Dovy's effect (the window was just checked; the draw and the write come before any await)."""
    bot, player_id = scene.bot, int(scene.author.id)
    cycle = bot.boss_cycle
    if not visible:
        rarity = draw_base_rarity(bot)
        cycle.set_next_rarity(rarity)
        # -- the write is done ---------------------------------------------------------------------
        log.info("Dova: %s threw the Kość Dovy; the coming boss is of base rarity %s now.", player_id, rarity)
        await _save_base_rarity(bot, rarity)
        await _say(scene, MSG_DIE_NEXT.format(player_id=player_id, emoji=boss_cycle.rarity_emoji(rarity),
                                              rarity=boss_cycle.RARITY_NAMES.get(rarity, "")))
        return
    info = bot.state.boss
    old = int(info.rarity)
    drawn = draw_base_rarity(bot)
    boost = modifiers.current_modifiers(bot).get("rarity_boost", 0)
    rarity = boss_cycle.final_rarity(drawn, boost, clock.now(), debug=bot.debug)
    same = rarity == old  # nothing to change: the boss on the channel stays as it is (no re-post, no row)
    picture, variant = (None, None) if info.is_player_boss or same else boss.picture_like(rarity,
                                                                                         variant=info.variant)
    scheduled = False if same else cycle.change_visible_boss(rarity, variant=variant.id if variant is not None
                                                             else "", base=drawn)
    # -- the write is done -------------------------------------------------------------------------
    if same:
        log.info("Dova: %s threw the Kość Dovy on the boss on the channel: rarity %s again (drawn %s).",
                 player_id, rarity, drawn)
        await _say(scene, MSG_DIE_SAME.format(player_id=player_id, emoji=boss_cycle.rarity_emoji(rarity),
                                              rarity=boss_cycle.RARITY_NAMES.get(rarity, "")))
        return
    log.info("Dova: %s threw the Kość Dovy on the boss on the channel: rarity %s -> %s (drawn %s).", player_id,
             old, rarity, drawn)
    # The boss is shown anew at once; its BOSS row matters only after a restart, so it comes last.
    await _say(scene, MSG_DIE_VISIBLE.format(player_id=player_id, emoji=boss_cycle.rarity_emoji(rarity),
                                             rarity=boss_cycle.RARITY_NAMES.get(rarity, "")))
    await cycle.show_again(scene, token=token, picture=picture, variant=variant)
    await cycle.refresh_spawn_notice(old)
    if scheduled:
        await _save_base_rarity(bot, drawn, token=token)


async def _save_base_rarity(bot: "AlterBot", rarity: int, *, token: int | None = None,
                            failed: str = LOG_BOSS_ROW) -> None:
    """The new base rarity into the ``BOSS`` row (its arrival stays).  Never raises: a failure tells the owner.

    The change is already in force in memory, so nothing is given back - only
    a restart would bring the old rarity back from the row.  ``token``: the boss
    on the channel whose row it is (:meth:`BossCycle.boss_token`) - should it
    have gone meanwhile, the row is no longer its (a new schedule may stand
    there) and is left alone.  ``failed``: what the owner is told.
    """
    if token is not None and bot.boss_cycle.boss_token() != token:
        log.info("Dova: the boss on the channel is gone; its new rarity %s is not written to the BOSS row.", rarity)
        return
    try:
        if not await database.set_boss_rarity(bot, rarity):
            log.warning("Dova: no BOSS row to note the new rarity %s in.", rarity)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Dova: the new rarity %s could not be written to the BOSS table.", rarity)
        with contextlib.suppress(Exception):
            await bot.log_to_owner(failed.format(rarity=rarity, error=type(exc).__name__))


# -- the Karta Dovy -------------------------------------------------------------------------------

def modifiers_on(current: dict[str, int]) -> int:
    """How many modifiers of ``current`` are on: every key with a value other than 0 (the player-boss once)."""
    return sum(1 for key in modifiers.INIT_MODIFIERS if int(current.get(key, 0) or 0) != 0)


def shuffle_modifiers(scene: "Scene", *, visible: bool) -> tuple[int, dict[str, int], bool]:
    """The Karta Dovy's draw and write: ``(how many were on, the new set, whether a rest was drawn)``.  Synchronous.

    The set goes back to :data:`modifiers.INIT_MODIFIERS` and ``max(1, n)``
    outcomes are drawn into it one after another, as that many prayers would
    (a one-off outcome never twice; the prayer's values).  ``visible``: the
    boss is already on the channel - the rarity boost and the player-boss are
    left out of the pool.  Written in one go; no ``await`` anywhere in here.
    """
    bot = scene.bot
    count = modifiers_on(modifiers.current_modifiers(bot))
    fresh = dict(modifiers.INIT_MODIFIERS)
    rest = False
    for _ in range(max(1, count)):
        name, value = modifiers.draw_modifier(scene, fresh, leave_out=LEFT_OUT_ON_VISIBLE if visible else ())
        if name == modifiers.RESET_CD:
            rest = True
        else:
            modifiers.add_modifier(fresh, name, value)
    modifiers.replace_modifiers(bot, fresh)
    return count, fresh, rest


async def _shuffle(scene: "Scene", *, visible: bool, token: int) -> None:
    """The Karta Dovy's effect (the window was just checked; the draw and the write come before any await).

    On the scheduled boss on the channel the shrine's rarity boost goes from
    the file, but not from the boss it raised: a restart draws that boss again
    from its ``BOSS`` row and the file (``BossCycle._restore``), so the row then
    takes the rarity shown in (the boost included) - the same boss comes back.
    """
    bot, player_id = scene.bot, int(scene.author.id)
    cycle = bot.boss_cycle
    boost = modifiers.current_modifiers(bot).get("rarity_boost", 0)
    count, fresh, rest = shuffle_modifiers(scene, visible=visible)
    picture = variant = None
    base = scheduled = None
    # (Nothing on the boss on the channel before, nothing now - only a rest drawn: its face stays as it is.)
    reshow = visible and bool(count or modifiers.describe_modifiers(fresh))
    if reshow:
        info = bot.state.boss
        if not info.is_player_boss:
            picture, variant = boss.picture_like(info.rarity, variant=info.variant, same=cycle.shown_picture)
        base = int(info.rarity) if boost else None
        scheduled = cycle.change_visible_boss(info.rarity, variant=variant.id if variant is not None else "",
                                              base=base)
    # -- the write is done -------------------------------------------------------------------------
    log.info("Dova: %s shuffled the Karta Dovy (%s on before%s): %s%s.", player_id, count,
             ", the boss on the channel" if visible else "", fresh, " + a rest" if rest else "")
    if rest:
        rest = await _reset_hunts(bot)
    await _say(scene, embed=discord.Embed(title=CARD_TITLE, description=card_text(player_id, count, fresh, rest,
                                                                                   visible=visible),
                                          color=DOVA_COLOUR))
    if reshow:
        await cycle.show_again(scene, token=token, picture=picture, variant=variant)
    if scheduled:
        await _save_base_rarity(bot, base, token=token, failed=LOG_BOSS_ROW_CARD)


def card_text(player_id: int, count: int, fresh: dict[str, int], rest: bool, *, visible: bool) -> str:
    """The Karta Dovy's embed: what went (``count`` were on), what came (``fresh``; ``rest``: the hunts were reset).

    The new perks of the boss, in the style of its embed's "Dodatkowe
    statystyki", the rest after them.  A card that drew only a rest says so -
    a rest is no perk of the boss; one whose rest could not be given either
    says it fizzled (never a line that ends on a colon).
    """
    perks = modifiers.describe_modifiers(fresh)
    words = {"player_id": int(player_id), "whose": WHOSE[visible], "who": WHO[visible]}
    rest_line = modifiers.RESET_CD_LINE if rest else ""
    if perks:
        if visible:
            head = CARD_HEAD_VISIBLE if count else CARD_HEAD_VISIBLE_EMPTY
        else:
            head = CARD_HEAD if count else CARD_HEAD_EMPTY
        return head.format(**words) + "".join(perks) + rest_line
    if rest:
        return (CARD_HEAD_REST if count else CARD_HEAD_REST_EMPTY).format(**words) + rest_line
    return (CARD_FIZZLED if count else CARD_FIZZLED_EMPTY).format(**words)


async def _reset_hunts(bot: "AlterBot") -> bool:
    """A drawn rest: everybody may hunt again, as after a prayer.  Never raises (a failure tells the owner)."""
    from . import daily  # lazy, as in modifiers: daily imports the modifiers

    try:
        await daily.clear_daily_file(bot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the new set is in force; only the rest is missing
        log.exception("Dova: the hunts could not be reset after the Karta Dovy drew a rest.")
        with contextlib.suppress(Exception):
            await bot.log_to_owner(LOG_HUNTS_NOT_RESET.format(error=type(exc).__name__))
        return False
    log.info("Dova: the Karta Dovy drew a rest - the daily hunting cooldown is reset.")
    return True
