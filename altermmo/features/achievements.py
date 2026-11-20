"""Achievements: counters of a player's deeds and the roles awarded for them.

Port of the legacy ``functions/functions_achievements.py``.  Table (unchanged,
shared with the legacy bot):

``ACHIEVEMENTS`` (PLAYER_ID NUMERIC PRIMARY KEY, BOSS_KILLS NUMERIC, HUNTS NUMERIC, BUTCHERS NUMERIC)

Everybody's deeds are counted, but only supporters (patrons, crafters, Twitch
subscribers, YouTube supporters - :func:`patrons.check_if_patron`) get the
roles, and only at the moment a counter reaches a milestone exactly (legacy rule).

Differences from the legacy code:

* counters are incremented with one atomic upsert instead of SELECT + UPDATE
  built with string formatting (two fights ending together lost a kill);
* a database failure is no longer swallowed: the legacy retry loop treated
  "could not read" as "new player", so an outage could hand out the first-kill
  role again.  ``DatabaseUnavailable`` now propagates, nothing is awarded;
* a player who left the server, or an achievement role deleted on the server,
  no longer crashes the caller (this used to kill the weekly ranking reset
  when the season winner had left);
* ``create_database`` only creates what is missing - it never drops the table.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..core import io
from . import patrons

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- achievement roles (used only by this feature and, for the tower, by tower.py) ---

#: Boss kills: number of kills -> role.
BOSS_KILL_ROLES: dict[int, int] = {
    1: 1125799224966131712,
    10: 1125799392989949973,
    30: 1125799470509084762,
}
#: Hunts (``$polowanie``) won: number of hunts -> role.
HUNT_ROLES: dict[int, int] = {
    10: 1125799891923374121,
    35: 1125800644826112043,
    100: 1125800721724489758,
}
#: Seasons won ("Rzeźnik Potworów"): number of seasons -> role.
BUTCHER_ROLES: dict[int, int] = {
    1: 1125801055758860329,
    3: 1125801161631486033,
    10: 1125801216375529563,
}
#: Death tower, lowest to highest (levels 3, 6 and 10; awarded by features/tower.py).
TOWER_ROLES: tuple[int, int, int] = (
    1151427369886826496,
    1151427619741511710,
    1151427790973964328,
)
#: Hunted down a legendary monster.
UNICORN_ROLE = 1125819170534211664
#: Took part in killing a legendary boss.
LEGEND_ROLE = 1125818827809226862
#: Average turn time under 2 seconds in a boss fight.
QUICKNESS_ROLE = 1125818642450370590

#: Families of roles where a higher tier replaces the lower ones.
_TIER_CHAINS: tuple[tuple[int, ...], ...] = (
    tuple(BOSS_KILL_ROLES.values()),
    tuple(HUNT_ROLES.values()),
    tuple(BUTCHER_ROLES.values()),
    TOWER_ROLES,
)

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS ACHIEVEMENTS (
           PLAYER_ID NUMERIC PRIMARY KEY,
           BOSS_KILLS NUMERIC,
           HUNTS NUMERIC,
           BUTCHERS NUMERIC
       )""",
)

_COUNTER_COLUMNS = ("BOSS_KILLS", "HUNTS", "BUTCHERS")

#: One atomic "add 1" statement per counter (column names are constants, values are parameters).
_INCREMENT_SQL: dict[str, str] = {
    column: f"""INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS, HUNTS, BUTCHERS)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (PLAYER_ID) DO UPDATE
                    SET {column} = COALESCE(ACHIEVEMENTS.{column}, 0) + 1
                RETURNING {column}"""
    for column in _COUNTER_COLUMNS
}


# -- helpers ----------------------------------------------------------------------

def _player_id(player: Any) -> int | None:
    """Id of a member (or a bare id); None when there is nobody."""
    if player is None:
        return None
    try:
        return int(getattr(player, "id", player))
    except (TypeError, ValueError):
        return None


def _has_role(member: Any, role_id: int) -> bool:
    return any(role.id == role_id for role in getattr(member, "roles", None) or [])


def lower_tier_roles(role_id: int) -> tuple[int, ...]:
    """Roles that ``role_id`` replaces (the lower tiers of the same achievement)."""
    for chain in _TIER_CHAINS:
        if role_id in chain:
            return chain[:chain.index(role_id)]
    return ()


async def _increment(bot: "AlterBot", player_id: int, column: str) -> int:
    """Add 1 to a counter (creating the player's row when needed); returns the new value."""
    start = [1 if name == column else 0 for name in _COUNTER_COLUMNS]
    # In a transaction on purpose: the db layer never re-sends it on its own,
    # so a connection hiccup cannot count the same deed twice.
    async with bot.db.transaction() as con:
        total = await con.fetchval(_INCREMENT_SQL[column], player_id, *start)
    return int(total)


async def _count(scene: "Scene", player: Any, column: str, milestones: dict[int, int]) -> bool:
    bot = scene.bot
    player_id = _player_id(player)
    if player_id is None:
        log.warning("Achievement %s not counted: no player given (left the server?).", column)
        return False

    number = await _increment(bot, player_id, column)
    log.info("Achievements: player %s has %s = %d", player_id, column, number)

    role_id = milestones.get(number)
    if role_id is not None:
        member = player if hasattr(player, "roles") else await bot.member(player_id)
        if patrons.check_if_patron(member):
            await set_achiev_role(scene, member, role_id)
    return True


# -- database ---------------------------------------------------------------------

async def create_database(scene: "Scene | AlterBot") -> None:
    """Create the ACHIEVEMENTS table when it is missing (existing data is kept).

    Takes a scene like the legacy function took a ``ctx``; a bare bot is
    accepted too because the legacy "create all databases" command called it
    without one.
    """
    bot = getattr(scene, "bot", scene)
    for statement in SCHEMA:
        await bot.db.execute(statement, idempotent=True)
    log.info("Table ACHIEVEMENTS is in place.")


async def new_record_achievements(bot: "AlterBot", player_id: int, boss_kills: int, hunts: int,
                                  butchers: int) -> bool:
    """Insert a player's row; False when the player already has one (nothing is changed)."""
    status = await bot.db.execute(
        """INSERT INTO ACHIEVEMENTS (PLAYER_ID, BOSS_KILLS, HUNTS, BUTCHERS)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (PLAYER_ID) DO NOTHING""",
        int(player_id), int(boss_kills), int(hunts), int(butchers), idempotent=True,
    )
    return status.endswith(" 1")


# -- counted achievements -----------------------------------------------------------

async def add_boss_kill(scene: "Scene", player: Any) -> bool:
    """Count a killed boss; supporters get a role at 1, 10 and 30 kills.

    Returns True when the kill was counted (False only when ``player`` is None).
    """
    return await _count(scene, player, "BOSS_KILLS", BOSS_KILL_ROLES)


async def add_butcher(scene: "Scene", player: Any) -> bool:
    """Count a won season; supporters get a role at 1, 3 and 10 seasons."""
    return await _count(scene, player, "BUTCHERS", BUTCHER_ROLES)


async def add_hunts(scene: "Scene", player: Any) -> bool:
    """Count a won hunt; supporters get a role at 10, 35 and 100 hunts."""
    return await _count(scene, player, "HUNTS", HUNT_ROLES)


# -- one-off achievements -----------------------------------------------------------

async def unicorn(scene: "Scene", player: Any) -> bool:
    """A supporter hunted down a legendary monster."""
    if patrons.check_if_patron(player):
        await set_achiev_role(scene, player, UNICORN_ROLE)
    return True


async def legend(scene: "Scene", player: Any) -> bool:
    """A supporter took part in killing a legendary boss."""
    if patrons.check_if_patron(player):
        await set_achiev_role(scene, player, LEGEND_ROLE)
    return True


async def quickness(scene: "Scene", player: Any) -> bool:
    """A supporter beat a boss with an average turn time under 2 seconds."""
    if patrons.check_if_patron(player):
        await set_achiev_role(scene, player, QUICKNESS_ROLE)
    return True


# -- roles ------------------------------------------------------------------------

async def set_achiev_role(scene: "Scene", player: Any, role_id: int) -> bool:
    """Give an achievement role and announce it on the scene's channel.

    A higher tier takes the lower tiers of the same achievement away.  Returns
    True when the role was granted now; False when the player already had it,
    left the server, or the role cannot be given (the owner is told why).
    Whether the player deserves the role (supporter check) is the caller's job.
    """
    bot = scene.bot
    player_id = _player_id(player)
    if player_id is None:
        return False

    role = bot.role(role_id)
    if role is None:
        log.error("Achievement role %s does not exist on the server.", role_id)
        await bot.log_to_owner(f"Rola osiągnięcia {role_id} nie istnieje na serwerze - "
                               f"nie przyznano jej graczowi <@{player_id}>.")
        return False

    member = await bot.member(player_id)  # fresh from the guild, as the legacy code did
    if member is None:
        log.info("Achievement role %s not granted: player %s left the server.", role, player_id)
        return False
    if _has_role(member, role_id):
        return False

    if not await io.add_role(member, role, reason="AlterMMO RPG: osiągnięcie"):
        await bot.log_to_owner(f"Nie udało się nadać roli osiągnięcia {role} graczowi <@{player_id}>.")
        return False
    log.info("Achievement role %s granted to %s (%s).", role, member, player_id)

    try:
        await scene.send(f"<@{player_id}> zdobywa osiągnięcie: {role}!")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        # The role is already given; a lost announcement must not abort the
        # fight/hunt that is handing out its rewards.
        log.warning("Could not announce achievement %s of %s: %s: %s",
                    role, player_id, type(exc).__name__, exc)

    for lower_id in lower_tier_roles(role_id):
        if _has_role(member, lower_id):
            await io.remove_role(member, bot.role(lower_id), reason="AlterMMO RPG: wyższe osiągnięcie")
    return True
