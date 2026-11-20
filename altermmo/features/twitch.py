"""Twitch rewards on Discord (port of the legacy ``functions/functions_twitch.py``).

No Twitch API is involved: a separate Twitch bot fills the tables
``chatters_information`` (watch time, message count, VIP/mod flags and the
linked ``discord_id``) and ``treasure_table`` (viewers who won a treasure on
stream).  This module only reads them, hands out Discord roles and pays the
treasures out as monster loot (deleting the paid rows).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any, Sequence

from ..core import io
from ..core.db import DatabaseUnavailable
from . import daily

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

#: The two tables belong to the Twitch bot; they are declared here only so that
#: the test database has them (``IF NOT EXISTS`` never touches the real ones).
SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS chatters_information (
           chtr_id SERIAL PRIMARY KEY,
           chtr_name TEXT NOT NULL,
           chtr_time INTEGER DEFAULT 0,
           chtr_comments INTEGER DEFAULT 0,
           chtr_isvip BOOLEAN,
           chtr_issub BOOLEAN,
           chtr_ismod BOOLEAN,
           chtr_isbroadcaster BOOLEAN,
           discord_id TEXT,
           watchstreak INTEGER NOT NULL DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS treasure_table (
           chtr_id SERIAL PRIMARY KEY,
           chtr_name TEXT NOT NULL,
           windatetime TEXT NOT NULL
       )""",
)

#: Viewer roles by watch time: Podgladacz (125 h), Ogladacz (250 h), Widz (375 h),
#: Pasjonata (500 h), Fanboy (750 h) - lowest to highest.
VIEWER_ROLES = (1044570191239057538, 1044570461511622716, 1044570672313147442,
                1044570838365638667, 1175454109705441410)
#: Chat roles by number of messages: Analfabeta, Niepisaty, Czatownik, Skryba, Wieszcz.
MESSAGER_ROLES = (1175474239890010134, 1175474471637893221, 1175474738789875854,
                  1175474928162721833, 1175475198854709330)
VIP_ROLE = 964845150213906442
MOD_ROLE = 969683014709825627

#: Only chatters above these values are read from the database at all.
MIN_WATCHTIME = 445000
MIN_MESSAGES = 299

MSG_ROLE_WATCHTIME = "<@{user_id}> zdobywa rolę {role} za śledzenie streamów na Twitchu!"
MSG_ROLE_MESSAGES = "<@{user_id}> zdobywa rolę {role} za pisanie na Twitchu!"
MSG_ROLE_VIP = "<@{user_id}> zdobywa rolę {role} za VIPa na Twitchu!"
MSG_ROLE_MOD = "<@{user_id}> zdobywa rolę {role} za moderatora na Twitchu!"
MSG_TWITCH_NAME = "Użytkownik <@{user_id}> ma połączone konto Twitch: **{names}**."
MSG_TWITCH_NONE = "Użytkownik <@{user_id}> nie ma połączonego konta Twitch."


# -- role tables ---------------------------------------------------------------------

def get_twitch_viewer_role(watchtime: int) -> tuple[int, list[int]]:
    """Viewer role for ``watchtime`` seconds: ``(role_id or 0, the other viewer roles)``."""
    watchtime_h = watchtime / 60 / 60

    if 125 <= watchtime_h < 250:
        role_id = VIEWER_ROLES[0]
    elif 250 <= watchtime_h < 375:
        role_id = VIEWER_ROLES[1]
    elif 375 <= watchtime_h < 500:
        role_id = VIEWER_ROLES[2]
    elif 500 <= watchtime_h < 750:
        role_id = VIEWER_ROLES[3]
    elif watchtime_h >= 750:
        role_id = VIEWER_ROLES[4]
    else:
        role_id = 0

    return role_id, [other for other in VIEWER_ROLES if other != role_id]


def get_twitch_messager_role(messages: int) -> tuple[int, list[int]]:
    """Chat role for ``messages`` written: ``(role_id or 0, the other chat roles)``."""
    if 1000 <= messages < 3600:
        role_id = MESSAGER_ROLES[0]
    elif 3600 <= messages < 7500:
        role_id = MESSAGER_ROLES[1]
    elif 7500 <= messages < 15000:
        role_id = MESSAGER_ROLES[2]
    elif 15000 <= messages < 30000:
        role_id = MESSAGER_ROLES[3]
    elif messages >= 30000:
        role_id = MESSAGER_ROLES[4]
    else:
        role_id = 0

    return role_id, [other for other in MESSAGER_ROLES if other != role_id]


# -- helpers -------------------------------------------------------------------------

def _discord_id(raw: Any) -> int | None:
    """``discord_id`` is free text filled by another application: accept only real ids."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _has_role(member: Any, role_id: int) -> bool:
    return any(role.id == role_id for role in getattr(member, "roles", None) or [])


async def _announce(bot: "AlterBot", text: str) -> None:
    """Tell the server about a new role (cosmetic: never raises)."""
    channel = bot.get_channel(bot.ids.log_channel if bot.debug else bot.ids.notice_channel)
    if channel is None:
        log.warning("No channel to announce: %s", text)
        return
    try:
        await io.send(channel, text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the role is granted, the notice is a bonus
        log.warning("Could not announce %r: %s: %s", text, type(exc).__name__, exc)


async def _grant_tier(bot: "AlterBot", user_id: int, role_id: int, others: Sequence[int],
                      text: str, *, never_downgrade: bool, missing: set[int]) -> bool:
    """Give ``role_id`` to the member and take the other tiers away; True when granted."""
    if role_id <= 0:
        return False
    member = await bot.member(user_id)
    if member is None:
        return False  # left the server (or never joined)
    if _has_role(member, role_id):
        return False
    role = bot.role(role_id)
    if role is None:
        missing.add(role_id)
        return False
    if never_downgrade:
        tiers = [*others, role_id]
        tiers.sort(key=lambda tier: (VIEWER_ROLES + MESSAGER_ROLES).index(tier))
        held = [index for index, tier in enumerate(tiers) if _has_role(member, tier)]
        if held and max(held) > tiers.index(role_id):
            return False  # already holds a better one
    if not await io.add_role(member, role, reason="AlterMMO RPG: Twitch"):
        return False
    for other_id in others:
        if _has_role(member, other_id):
            await io.remove_role(member, bot.role(other_id), reason="AlterMMO RPG: Twitch")
    log.info("Twitch role %s granted to %s (%s).", role, member, user_id)
    await _announce(bot, text.format(user_id=member.id, role=role))
    return True


async def _report_missing(bot: "AlterBot", missing: set[int]) -> None:
    if missing:
        ids = ", ".join(str(role_id) for role_id in sorted(missing))
        log.error("Twitch roles missing on the server: %s", ids)
        await bot.log_to_owner(f"Role za Twitcha nie istnieją na serwerze: {ids}")


# -- roles ---------------------------------------------------------------------------

async def assign_roles_watchtime(bot: "AlterBot") -> int:
    """Give viewer roles to linked accounts with enough watch time; returns how many were granted."""
    rows = await bot.db.fetch(
        """SELECT discord_id, MAX(chtr_time) AS chtr_time FROM chatters_information
           WHERE chtr_time > $1 AND discord_id IS NOT NULL GROUP BY discord_id""",
        MIN_WATCHTIME)
    granted = 0
    missing: set[int] = set()
    for row in rows:
        user_id = _discord_id(row["discord_id"])
        if user_id is None:
            continue
        role_id, others = get_twitch_viewer_role(int(row["chtr_time"]))
        if await _grant_tier(bot, user_id, role_id, others, MSG_ROLE_WATCHTIME,
                             never_downgrade=True, missing=missing):
            granted += 1
    await _report_missing(bot, missing)
    return granted


async def assign_roles_messages(bot: "AlterBot") -> int:
    """Give chat roles to linked accounts with enough messages; returns how many were granted."""
    rows = await bot.db.fetch(
        """SELECT discord_id, MAX(chtr_comments) AS chtr_comments FROM chatters_information
           WHERE chtr_comments > $1 AND discord_id IS NOT NULL GROUP BY discord_id""",
        MIN_MESSAGES)
    granted = 0
    missing: set[int] = set()
    for row in rows:
        user_id = _discord_id(row["discord_id"])
        if user_id is None:
            continue
        role_id, others = get_twitch_messager_role(int(row["chtr_comments"]))
        if await _grant_tier(bot, user_id, role_id, others, MSG_ROLE_MESSAGES,
                             never_downgrade=False, missing=missing):
            granted += 1
    await _report_missing(bot, missing)
    return granted


async def assign_roles_vip_mod(bot: "AlterBot") -> int:
    """Give the VIP / moderator role to linked Twitch VIPs and mods; returns how many were granted."""
    rows = await bot.db.fetch(
        """SELECT discord_id, chtr_isvip, chtr_ismod FROM chatters_information
           WHERE (chtr_isvip = TRUE OR chtr_ismod = TRUE) AND discord_id IS NOT NULL
           ORDER BY chtr_id""")
    granted = 0
    missing: set[int] = set()
    for row in rows:
        user_id = _discord_id(row["discord_id"])
        if user_id is None:
            continue
        # A VIP who is also a moderator gets the VIP role (legacy).
        role_id, text = (VIP_ROLE, MSG_ROLE_VIP) if row["chtr_isvip"] else (MOD_ROLE, MSG_ROLE_MOD)
        if await _grant_tier(bot, user_id, role_id, (), text, never_downgrade=False, missing=missing):
            granted += 1
    await _report_missing(bot, missing)
    return granted


# -- linked account ------------------------------------------------------------------

async def twitch_names(bot: "AlterBot", user_id: int | str) -> list[str]:
    """Twitch names linked to the Discord account (most watched first)."""
    rows = await bot.db.fetch(
        """SELECT chtr_name FROM chatters_information WHERE discord_id = $1
           ORDER BY chtr_time DESC NULLS LAST, chtr_id""",
        str(user_id).strip())
    return [row["chtr_name"] for row in rows]


async def get_user_twitch_name(scene: "Scene", user_id: int | str) -> str | None:
    """``$checkTwitch``: tell whether ``user_id`` linked a Twitch account; returns its name."""
    names = await twitch_names(scene.bot, user_id)
    if not names:
        await scene.send(MSG_TWITCH_NONE.format(user_id=user_id))
        return None
    await scene.send(MSG_TWITCH_NAME.format(user_id=user_id, names=", ".join(names)))
    return names[0]


# -- treasures -----------------------------------------------------------------------

async def _treasure_winner(bot: "AlterBot", twitch_name: str) -> Any:
    """The server member who linked ``twitch_name``; None when there is none."""
    rows = await bot.db.fetch(
        "SELECT discord_id FROM chatters_information WHERE chtr_name = $1 ORDER BY chtr_id", twitch_name)
    for row in rows:
        user_id = _discord_id(row["discord_id"])
        if user_id is None:
            continue
        member = await bot.member(user_id)
        if member is not None:
            return member
    return None


#: One payout run at a time (the nightly job and ``$checkTreasure`` must not interleave).
_treasure_lock = asyncio.Lock()


async def check_treasure(bot: "AlterBot") -> int:
    """Pay out the treasures won on Twitch (run daily); returns the number of paid ones.

    A treasure is paid once.  The winner's row is taken out of the table
    FIRST (``DELETE ... RETURNING``: whoever gets the row back owns the
    payout) and paid afterwards - the other way round, a failed or interrupted
    delete let the next run pay the same treasure again.  The price: a payout
    that fails after its row is gone is not retried; the owner is told (with a
    ping) whom to pay by hand.  A winner without a linked Discord account on
    the server cannot be paid at all - that row is dropped, as it always was.
    """
    async with _treasure_lock:
        return await _check_treasure(bot)


async def _check_treasure(bot: "AlterBot") -> int:
    rows = await bot.db.fetch("SELECT chtr_id, chtr_name FROM treasure_table ORDER BY chtr_id ASC")
    if not rows:
        return 0
    log.info("Twitch treasures to pay out: %s", [row["chtr_name"] for row in rows])
    scene = bot.boss_scene()
    paid = 0
    unlinked: list[str] = []
    for index, row in enumerate(rows):
        name = row["chtr_name"]
        try:
            treasure_id = int(row["chtr_id"])
        except (TypeError, ValueError):
            log.error("Twitch treasure of %r has no usable chtr_id (%r); skipped.", name, row["chtr_id"])
            await bot.log_to_owner(f"Skarb z Twitcha dla `{name}` ma nieprawidłowe chtr_id "
                                   f"({row['chtr_id']!r}) - pomijam go, popraw wiersz w treasure_table.")
            continue
        try:
            winner = await _treasure_winner(bot, name)
        except (asyncio.CancelledError, DatabaseUnavailable):
            raise  # nothing was taken or paid: the rows stay for the next run
        except Exception as exc:  # noqa: BLE001 - keep this row, go on with the other winners
            log.exception("Twitch treasure of %r: the winner could not be looked up.", name)
            await bot.log_to_owner(f"Skarb z Twitcha dla `{name}` nie został wypłacony "
                                   f"(`{type(exc).__name__}: {exc}`) - spróbuję przy następnym sprawdzeniu.")
            continue
        # Take the row first (never re-sent by the db layer: a row that is gone is gone once).
        # Exactly ONE row: should a viewer have two wins under one id, each is taken and paid on its own.
        try:
            claimed = await bot.db.fetchval(
                """DELETE FROM treasure_table
                   WHERE ctid = (SELECT ctid FROM treasure_table
                                 WHERE chtr_id = $1 AND chtr_name IS NOT DISTINCT FROM $2 LIMIT 1)
                   RETURNING chtr_id""", treasure_id, name, idempotent=False)
        except asyncio.CancelledError:
            log.error("Twitch treasure of %r: interrupted while its row was being taken.", name)
            raise
        except Exception as exc:  # noqa: BLE001 - the row may or may not be gone: the owner must look
            log.exception("Twitch treasure of %r: could not take the row.", name)
            await bot.log_to_owner(f"Skarb z Twitcha dla `{name}`: nie wiadomo, czy wiersz został zabrany "
                                   f"(`{type(exc).__name__}`). Sprawdź treasure_table - jeśli wiersza nie ma, "
                                   "przyznaj nagrodę ręcznie.", ping=True)
            raise
        if claimed is None:
            continue  # somebody else took it meanwhile
        if winner is None:
            unlinked.append(name)
            continue
        try:
            await daily.rand_loot(scene, 2, winner, random.randint(0, 25))
            paid += 1
            log.info("Twitch treasure of %r paid to %s (%s).", name, winner, winner.id)
        except asyncio.CancelledError:
            log.error("Twitch treasure of %r for %s (%s) was interrupted while being paid.", name, winner,
                      winner.id)
            raise
        except Exception as exc:  # noqa: BLE001 - the row is gone: told to the owner, never paid twice
            log.exception("Twitch treasure of %r could not be paid.", name)
            await bot.log_to_owner(f"Skarb z Twitcha dla `{name}` (<@{winner.id}>) NIE został wypłacony w "
                                   f"całości (`{type(exc).__name__}: {exc}`). Nie próbuję ponownie, żeby nie "
                                   "wypłacić go dwa razy - przyznaj nagrodę ręcznie.", ping=True)
            if isinstance(exc, DatabaseUnavailable):
                raise  # the database is away: the rows not taken yet stay for the next run
            # Whatever broke may break for everybody: stop here, so the rows not taken yet stay
            # for the next run instead of being emptied one failed payout after another.
            waiting = len(rows) - index - 1
            if waiting > 0:
                await bot.log_to_owner(f"Pozostałe skarby z Twitcha ({waiting}) czekają w tabeli do "
                                       "następnego sprawdzenia (albo **$checkTreasure**).")
            break
    if unlinked:
        log.info("Twitch treasures dropped (no linked Discord member): %s", unlinked)
        await bot.log_to_owner("Skarby z Twitcha przepadły (brak połączonego konta Discord na serwerze): "
                               + ", ".join(f"`{name}`" for name in unlinked))
    return paid
