"""Achievements: counters of a player's deeds and the roles awarded for them.

Port of the legacy ``functions/functions_achievements.py``, grown in v2.  Tables:

``ACHIEVEMENTS`` (PLAYER_ID NUMERIC PRIMARY KEY, BOSS_KILLS NUMERIC, HUNTS NUMERIC, BUTCHERS NUMERIC)
    unchanged, taken over from the legacy bot;
``ACHIEVEMENT_COUNTERS`` (PLAYER_ID NUMERIC, NAME VARCHAR(64), VALUE NUMERIC, PRIMARY KEY (PLAYER_ID, NAME))
    new in v2: what else is counted (won tournaments and duels, opened chests, rituals).

Everybody's deeds are counted, but only supporters (patrons, crafters, Twitch
subscribers, YouTube supporters - :func:`patrons.check_if_patron`) get the roles.

The catalogue (:data:`TROPHIES`) lists every achievement.  The ones the old
bot had keep their roles; the roles of the new ones are made by the bot itself
(:func:`ensure_roles`, their ids are remembered in ``data/achievementRoles.json``).

Owner's decisions of 2026-11-20:

* a supporter gets the highest tier his counter has reached whenever a deed is
  counted - not only at the moment the counter hits a milestone exactly (the
  legacy rule).  So veterans, and players who started to support later, are
  not left without the roles they earned long ago; :func:`sync_veterans` gives
  them out after every start, ``$osiagniecia`` to the player who asks;
* ``$osiagniecia`` (supporters) shows what the player has and "X of Y" - never
  which ones are missing.

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
import json
import logging
import time
import types
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from ..core import io
from ..core.state import BossPhase
from . import patrons

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- achievement roles the old bot already had ---------------------------------------

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

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS ACHIEVEMENTS (
           PLAYER_ID NUMERIC PRIMARY KEY,
           BOSS_KILLS NUMERIC,
           HUNTS NUMERIC,
           BUTCHERS NUMERIC
       )""",
    """CREATE TABLE IF NOT EXISTS ACHIEVEMENT_COUNTERS (
           PLAYER_ID NUMERIC NOT NULL,
           NAME VARCHAR(64) NOT NULL,
           VALUE NUMERIC NOT NULL DEFAULT 0,
           PRIMARY KEY (PLAYER_ID, NAME)
       )""",
)

#: New in v2: the old bot's database does not have it (created at start-up when missing,
#: see ``schema.create_new_tables``).
NEW_TABLES: tuple[str, ...] = (SCHEMA[1],)

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

# -- the catalogue ---------------------------------------------------------------------

#: Colour of the roles the bot makes (the owner may change names, colours and positions on
#: the server afterwards: the bot knows its roles by id).
ROLE_COLOUR = 0xC9A227
ROLE_REASON = "AlterMMO RPG: rola osiągnięcia"

#: Average seconds per turn of a boss fight: below 2 s (the old role), below 1 s, below 0.75 s.
QUICK_LIMITS: tuple[float, float, float] = (2.0, 1.0, 0.75)
#: "Sprinter Wieży": a floor from this one up, cleared faster than that many seconds.
SPRINT_FLOOR, SPRINT_SECONDS = 6, 20.0
#: ``PETS.PET_SKILLS`` of the highest talent ("Boski") and ``PETS.SEAL`` of the rarest seal.
DIVINE_TALENT, RAREST_SEAL = 9, 3
#: The active pet and the four places of the stable.
_PET_COLUMNS = ("PET_ID", "PET_ID_ALT1", "PET_ID_ALT2", "PET_ID_ALT3", "PET_ID_ALT4")


@dataclass(frozen=True)
class Trophy:
    """One achievement = one role.

    ``chain``: the family it is a tier of ("" = on its own); the tiers of a
    family are listed lowest first and a higher one replaces the lower ones.
    ``threshold``: the count (or the time limit) of a tier.  The role is
    ``role_id`` (a role the server had before), ``bot.ids.<ids_attr>`` (a role
    the rest of the game hands out) or, with neither, a role the bot makes
    itself (:func:`ensure_roles`).
    """

    key: str
    name: str
    chain: str = ""
    role_id: int = 0
    threshold: float = 0.0
    ids_attr: str = ""

    @property
    def bot_made(self) -> bool:
        return not self.role_id and not self.ids_attr


_NUMERALS = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII")


def _tiers(chain: str, name: str, fixed: dict[int, int], new: tuple[int, ...]) -> tuple[Trophy, ...]:
    steps = list(fixed.items()) + [(count, 0) for count in new]
    return tuple(Trophy(f"{chain}_{count}", f"{name} {_NUMERALS[index]}", chain, role_id, float(count))
                 for index, (count, role_id) in enumerate(steps))


#: Every achievement, in the order ``$osiagniecia`` shows them.  The names of the old ones
#: are only for the logs (their roles have their own names on the server).
TROPHIES: tuple[Trophy, ...] = (
    *_tiers("boss_kills", "Pogromca bossów", BOSS_KILL_ROLES, (100, 250, 500)),
    *_tiers("hunts", "Myśliwy", HUNT_ROLES, (250, 500, 1000)),
    *_tiers("butchers", "Rzeźnik potworów", BUTCHER_ROLES, (25,)),
    Trophy("tower_3", "Wieża Śmierci I", "tower", TOWER_ROLES[0], 3),
    Trophy("tower_6", "Wieża Śmierci II", "tower", TOWER_ROLES[1], 6),
    Trophy("tower_10", "Wieża Śmierci III", "tower", TOWER_ROLES[2], 10),
    Trophy("tower_sprint", "Sprinter Wieży"),
    Trophy("quick_1", "Szybkość", "quick", QUICKNESS_ROLE, QUICK_LIMITS[0]),
    Trophy("quick_2", "Błyskawica I", "quick", 0, QUICK_LIMITS[1]),
    Trophy("quick_3", "Błyskawica II", "quick", 0, QUICK_LIMITS[2]),
    Trophy("unicorn", "Legendarny potwór", role_id=UNICORN_ROLE),
    Trophy("legend", "Legendarny boss", role_id=LEGEND_ROLE),
    Trophy("mythic_boss", "Mityczny boss", ids_attr="mythic_slayer_role"),
    Trophy("pet_mythic", "Mityczny towarzysz"),
    Trophy("pet_holo", "Folia Holo"),
    Trophy("pet_seal", "Pieczęć Arcyłowcy"),
    Trophy("pet_ultra", "Ultra Shiny"),
    Trophy("pet_divine", "Boski talent"),
    Trophy("stable_full", "Pełna stajnia"),
    Trophy("tournaments_1", "Mistrz turnieju", "tournaments", 0, 1),
    Trophy("duels_5", "Pojedynkowicz I", "duels", 0, 5),
    Trophy("duels_25", "Pojedynkowicz II", "duels", 0, 25),
    Trophy("chests_10", "Łowca skarbów I", "chests", 0, 10),
    Trophy("chests_50", "Łowca skarbów II", "chests", 0, 50),
    Trophy("rituals_5", "Kultysta I", "rituals", 0, 5),
    Trophy("rituals_20", "Kultysta II", "rituals", 0, 20),
)
_BY_KEY: dict[str, Trophy] = {item.key: item for item in TROPHIES}
#: Counter column of ``ACHIEVEMENTS`` -> family of tiers.
_COLUMN_CHAINS: dict[str, str] = {"BOSS_KILLS": "boss_kills", "HUNTS": "hunts", "BUTCHERS": "butchers"}
#: New in v2: what else is counted (``ACHIEVEMENT_COUNTERS.NAME``); each is a family of tiers.
COUNTERS: tuple[str, ...] = ("tournaments", "duels", "chests", "rituals")
#: Families whose tier follows from a number that only grows.
_COUNTED_CHAINS = frozenset({*_COLUMN_CHAINS.values(), *COUNTERS, "tower"})

#: The start-up hand-out waits while something plays on the boss channel (seconds between looks).
QUIET_POLL = 15.0
#: After a role could not be made, the game does not try again on its own for this long
#: (seconds).  The next start and ``$roleosiagniec`` try at once.
ROLE_RETRY_AFTER = 3600.0
#: How long a role the bot has just made counts as being on the server although Discord's
#: cache does not show it yet (seconds; the cache learns about it from an event a moment later).
FRESH_ROLE_SECONDS = 120.0
#: Longest note for the owner's log channel (Discord: 2000 characters, the bot cuts at 1990).
LOG_CHUNK = 1800

MSG_SUPPORTERS_ONLY = ("Osiągnięcia kolekcjonerskie są dla wspierających ({groups}) "
                       "<:PepoG:790963160528977980>\nWesprzyj AlterMMO: {links}")
MSG_SUMMARY_TITLE = "🏅 Osiągnięcia"
MSG_SUMMARY = "<@{player_id}>, masz **{earned} z {total}** osiągnięć."
MSG_SUMMARY_NONE = "Jeszcze żadnego - ruszaj na łowy!"
MSG_SUMMARY_NEW = "🎉 **Nowe:** {roles}"
MSG_SUMMARY_FOOTER = "Których brakuje? Tego nie zdradzę - odkryj je w grze!"
LOG_ROLES_CREATED = ("Utworzyłem role osiągnięć ({count}): {names}. Możesz zmienić ich nazwy, kolory "
                     "i miejsce na liście ról (trzymaj je poniżej roli bota, inaczej bot ich nie nada) - "
                     "bot rozpoznaje je po ID.")
LOG_ROLES_ADOPTED = ("Role osiągnięć, które już były na serwerze, przypisałem na nowo po nazwie ({count}): "
                     "{names}.")
LOG_ROLES_FAILED = ("Nie udało się utworzyć ról osiągnięć: {names}. Sprawdź, czy bot ma uprawnienie "
                    "\"Zarządzanie rolami\" (i czy serwer nie ma już 250 ról), a potem wpisz "
                    "**$roleosiagniec**. Do tego czasu tych osiągnięć nie nadaję.")
LOG_ROLES_FILE_UNREADABLE = ("Nie mogę odczytać pliku z rolami osiągnięć (`{path}`). Nie tworzę ról od nowa, "
                             "żeby nie powstały duplikaty - sprawdź plik i wpisz **$roleosiagniec**.")
LOG_ROLE_MISSING = ("Rola osiągnięcia {role_id} nie istnieje na serwerze - nie przyznano jej graczowi "
                    "<@{player_id}>. (O tej roli nie piszę więcej do następnego restartu.)")
LOG_ROLE_NOT_GIVEN = ("Nie udało się nadać roli osiągnięcia {role} graczowi <@{player_id}> - czy rola jest "
                      "poniżej roli bota? (O tej roli nie piszę więcej do następnego restartu.)")
LOG_VETERANS = "Zaległe osiągnięcia: nadałem {roles} ról {players} wspierającym."


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


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _seconds(text: Any) -> float | None:
    """A stored time (``H:MM:SS.ffffff``) in seconds; None when it cannot be read."""
    try:
        hours, minutes, seconds = str(text).strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


def trophy(key: str) -> Trophy:
    return _BY_KEY[key]


def chain_of(item: Trophy) -> tuple[Trophy, ...]:
    """The tiers of the achievement's family, lowest first (itself when it has no family)."""
    if not item.chain:
        return (item,)
    return tuple(other for other in TROPHIES if other.chain == item.chain)


def _tier_for(chain: str, value: float) -> Trophy | None:
    """The highest tier of a counted family that ``value`` has reached (None: none yet)."""
    reached = [item for item in TROPHIES if item.chain == chain and value >= item.threshold]
    return reached[-1] if reached else None


def _quick_tier(seconds: float) -> Trophy | None:
    """The best speed tier of a boss fight with ``seconds`` per turn (None: too slow)."""
    reached = [item for item in TROPHIES if item.chain == "quick" and seconds < item.threshold]
    return reached[-1] if reached else None


# -- the roles of the achievements -------------------------------------------------------

def _read_roles_file(bot: Any) -> dict[str, int] | None:
    """The saved ``{achievement key: role id}``; {} when nothing was saved yet, None when unreadable.

    The difference matters: "nothing saved" lets the bot make the roles, an
    unreadable file must not (every role would be made a second time and the
    ids of the roles players already hold would be overwritten).
    """
    path = bot.store.achievement_roles.path
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        log.exception("Cannot read %s.", path)
        return None
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        log.error("File %s is corrupt (%d bytes).", path, len(raw))
        return None
    if not isinstance(loaded, dict):
        log.error("File %s does not hold a mapping.", path)
        return None
    return {str(key): _number(value) for key, value in loaded.items() if _number(value) > 0}


def _stored_roles(bot: Any) -> dict[str, int]:
    """``{achievement key: role id}`` of the roles the bot made (read once, then remembered).

    While the file cannot be read this is empty and nothing is remembered (the
    next call reads again); see :func:`roles_file_readable`.
    """
    stored = getattr(bot, "_achievement_roles", None)
    if stored is None:
        stored = _read_roles_file(bot)
        if stored is None:
            return {}
        bot._achievement_roles = stored
    return stored


def roles_file_readable(bot: Any) -> bool:
    """False while the saved role ids cannot be read (no role is made then)."""
    _stored_roles(bot)
    return getattr(bot, "_achievement_roles", None) is not None


def _made_roles(bot: Any) -> dict[int, tuple[Any, float]]:
    """Roles made a moment ago, ``{id: (role, when)}``: Discord's cache learns about a new role late."""
    made = getattr(bot, "_achievement_made", None)
    if made is None:
        made = {}
        bot._achievement_made = made
    return made


def _role(bot: Any, role_id: int | None) -> Any:
    """The role object of an id (also one made a moment ago); None when it is not on the server.

    A role the bot made counts without the cache only for :data:`FRESH_ROLE_SECONDS`
    and only until the cache has seen it - a role deleted on the server later
    must read as gone, so it can be made again.
    """
    if not role_id:
        return None
    fresh = _made_roles(bot)
    cached = bot.role(role_id)
    if cached is not None:
        fresh.pop(role_id, None)
        return cached
    role, made_at = fresh.get(role_id, (None, 0.0))
    if role is not None and time.monotonic() - made_at > FRESH_ROLE_SECONDS:
        del fresh[role_id]
        return None
    return role


def role_exists(bot: Any, item: Trophy) -> bool:
    """True when the achievement's role is on the server."""
    return _role(bot, role_id_of(bot, item)) is not None


def _told_once(bot: Any, what: Any) -> bool:
    """True the first time ``what`` is reported in this run (the owner is not told again and again)."""
    told = getattr(bot, "_achievement_told", None)
    if told is None:
        told = set()
        bot._achievement_told = told
    if what in told:
        return False
    told.add(what)
    return True


def role_id_of(bot: Any, item: Trophy) -> int | None:
    """The role of an achievement; None while the bot has not made it yet."""
    if item.role_id:
        return item.role_id
    if bot is None:
        return None
    if item.ids_attr:
        return _number(getattr(getattr(bot, "ids", None), item.ids_attr, 0)) or None
    return _stored_roles(bot).get(item.key) or None


def lower_tier_roles(role_id: int, bot: Any = None) -> tuple[int, ...]:
    """Roles that ``role_id`` replaces (the lower tiers of the same achievement).

    Without ``bot`` only the roles the server had before are known.
    """
    for item in TROPHIES:
        if role_id_of(bot, item) == role_id:
            tiers = chain_of(item)
            lower = (role_id_of(bot, tier) for tier in tiers[:tiers.index(item)])
            return tuple(found for found in lower if found)
    return ()


def _roles_lock(bot: Any) -> asyncio.Lock:
    lock = getattr(bot, "_achievement_roles_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bot._achievement_roles_lock = lock
    return lock


def _adoptable(role: Any, name: str, taken: set[int]) -> bool:
    """A role on the server that is clearly one of ours whose id was lost (same name, no permissions)."""
    return (getattr(role, "name", None) == name and int(role.id) not in taken
            and not getattr(role, "managed", False)
            and getattr(getattr(role, "permissions", None), "value", 0) == 0)


async def ensure_roles(bot: "AlterBot", *, only: Any = None, force: bool = True) -> list[str]:
    """Make the roles of the new achievements that do not exist yet; returns the names made.

    Called after every start and by ``$roleosiagniec`` (everything, at once)
    and by :func:`grant` when the role it needs is missing (``only`` = that
    achievement, ``force=False``: not within :data:`ROLE_RETRY_AFTER` of a
    failure, so a server that refuses new roles is not asked at every deed).
    A role somebody deleted on the server is made again.  A role of that name
    without permissions that is already there (the saved ids were lost) is
    taken over instead of being made twice.  Nothing is made while the saved
    ids cannot be read or the server's roles are not known.  The roles give no
    permissions and are not shown separately.  The owner is told what was
    made and - once per change - what could not be.  Never raises.
    """
    made: list[str] = []
    adopted: list[str] = []
    failed: list[str] = []
    wanted = [item for item in TROPHIES if item.bot_made and (only is None or item.key in only)]
    try:
        if not force and time.monotonic() < getattr(bot, "_achievement_roles_retry_at", 0.0):
            return []
        async with _roles_lock(bot):
            guild = bot.get_guild(bot.ids.guild)
            if guild is None or getattr(guild, "unavailable", False):
                return []
            if not roles_file_readable(bot):
                if _told_once(bot, "roles-file"):
                    await bot.log_to_owner(
                        LOG_ROLES_FILE_UNREADABLE.format(path=bot.store.achievement_roles.path), ping=True)
                return []
            stored = _stored_roles(bot)
            for item in wanted:
                known = stored.get(item.key)
                if known and _role(bot, known) is not None:
                    continue
                taken = set(stored.values())
                role = next((found for found in getattr(guild, "roles", None) or []
                             if _adoptable(found, item.name, taken)), None)
                if role is not None:
                    adopted.append(item.name)
                else:
                    try:
                        role = await guild.create_role(
                            name=item.name, colour=discord.Colour(ROLE_COLOUR), hoist=False,
                            mentionable=False, permissions=discord.Permissions.none(), reason=ROLE_REASON)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - no permission, too many roles, Discord is down
                        log.warning("Cannot make the achievement role %r: %s: %s",
                                    item.name, type(exc).__name__, exc)
                        failed.append(item.name)
                        continue
                    if guild.get_role(int(role.id)) is None:  # (the cache has not heard of it yet)
                        _made_roles(bot)[int(role.id)] = (role, time.monotonic())
                    made.append(item.name)
                stored[item.key] = int(role.id)
                bot.store.achievement_roles.save(dict(stored))  # at once: a crash must not orphan the role
                log.info("Achievement role %r: %s (%s).", item.name,
                         "taken over" if item.name in adopted else "made", role.id)
            if failed:
                bot._achievement_roles_retry_at = time.monotonic() + ROLE_RETRY_AFTER
            elif only is None:
                bot._achievement_roles_retry_at = 0.0
        if made:
            await bot.log_to_owner(LOG_ROLES_CREATED.format(count=len(made), names=", ".join(made)))
        if adopted:
            await bot.log_to_owner(LOG_ROLES_ADOPTED.format(count=len(adopted), names=", ".join(adopted)))
        if [name for name in failed if _told_once(bot, ("role-failed", name))]:  # (not at every attempt)
            await bot.log_to_owner(LOG_ROLES_FAILED.format(names=", ".join(failed)), ping=True)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Could not make the achievement roles.")
    return made


def _holds(bot: Any, member: Any, item: Trophy) -> bool:
    role_id = role_id_of(bot, item)
    return role_id is not None and _has_role(member, role_id)


def _holds_this_or_higher(bot: Any, member: Any, item: Trophy) -> bool:
    tiers = chain_of(item)
    return any(_holds(bot, member, tier) for tier in tiers[tiers.index(item):])


async def grant(scene: "Scene", player: Any, key: str, *, announce: bool = True) -> bool:
    """Give a supporter the achievement ``key``; True when it was given now.

    Nothing happens for somebody who does not support, who has it already or
    who holds a higher tier of the same family.  A role the bot has not made
    yet (or that was deleted on the server) is made first.
    """
    bot = scene.bot
    player_id = _player_id(player)
    if player_id is None:
        return False
    member = player if hasattr(player, "roles") else await bot.member(player_id)
    if member is None or not patrons.check_if_patron(member):
        return False
    item = trophy(key)
    if _holds_this_or_higher(bot, member, item):
        return False
    if any(tier.bot_made for tier in chain_of(item)) and not roles_file_readable(bot):
        # The roles of this family's tiers are not known now: the member may hold a higher
        # one, so nothing of the family is given until the file can be read again.
        return False
    role_id = role_id_of(bot, item)
    if item.bot_made and _role(bot, role_id) is None:
        await ensure_roles(bot, only=(item.key,), force=False)
        role_id = role_id_of(bot, item)
        if _role(bot, role_id) is None:
            return False  # (the owner has been told; tried again after ROLE_RETRY_AFTER)
    if role_id is None:
        return False
    return await set_achiev_role(scene, member, role_id, announce=announce)


async def grant_reached(scene: "Scene", player: Any, chain: str, value: float, *,
                        announce: bool = True) -> Trophy | None:
    """Give a supporter the highest tier of a counted family that ``value`` has reached.

    Returns the tier given now (None: nothing new).  Should the role of that
    tier be impossible to give (the server refused to make it), the highest
    one below it that can be given is - the player is not left with nothing.
    """
    reached = [item for item in TROPHIES if item.chain == chain and value >= item.threshold]
    player_id = _player_id(player)
    if not reached or player_id is None:
        return None
    bot = scene.bot
    member = player if hasattr(player, "roles") else await bot.member(player_id)
    if member is None or not patrons.check_if_patron(member):
        return None
    for item in reversed(reached):
        if _holds_this_or_higher(bot, member, item):
            return None
        if await grant(scene, member, item.key, announce=announce):
            return item
    return None


async def _increment(bot: "AlterBot", player_id: int, column: str) -> int:
    """Add 1 to a counter (creating the player's row when needed); returns the new value."""
    start = [1 if name == column else 0 for name in _COUNTER_COLUMNS]
    # In a transaction on purpose: the db layer never re-sends it on its own,
    # so a connection hiccup cannot count the same deed twice.
    async with bot.db.transaction() as con:
        total = await con.fetchval(_INCREMENT_SQL[column], player_id, *start)
    return int(total)


async def _count(scene: "Scene", player: Any, column: str) -> bool:
    bot = scene.bot
    player_id = _player_id(player)
    if player_id is None:
        log.warning("Achievement %s not counted: no player given (left the server?).", column)
        return False

    number = await _increment(bot, player_id, column)
    log.info("Achievements: player %s has %s = %d", player_id, column, number)

    await grant_reached(scene, player, _COLUMN_CHAINS[column], number)
    return True


# -- database ---------------------------------------------------------------------

async def create_database(scene: "Scene | AlterBot") -> None:
    """Create the achievement tables when they are missing (existing data is kept).

    Takes a scene like the legacy function took a ``ctx``; a bare bot is
    accepted too because the legacy "create all databases" command called it
    without one.
    """
    bot = getattr(scene, "bot", scene)
    for statement in SCHEMA:
        await bot.db.execute(statement, idempotent=True)
    log.info("Tables ACHIEVEMENTS and ACHIEVEMENT_COUNTERS are in place.")


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
    """Count a killed boss; supporters get the role of the tier reached (1, 10, 30, 100, 250, 500).

    Returns True when the kill was counted (False only when ``player`` is None).
    """
    return await _count(scene, player, "BOSS_KILLS")


async def add_butcher(scene: "Scene", player: Any) -> bool:
    """Count a won season; supporters get the role of the tier reached (1, 3, 10, 25)."""
    return await _count(scene, player, "BUTCHERS")


async def add_hunts(scene: "Scene", player: Any) -> bool:
    """Count a won hunt; supporters get the role of the tier reached (10, 35, 100, 250, 500, 1000)."""
    return await _count(scene, player, "HUNTS")


async def add_count(scene: "Scene", player: Any, counter: str, amount: int = 1, *,
                    announce: bool = True) -> int:
    """Count a deed of the new kind (``counter`` in :data:`COUNTERS`); returns the new total.

    Supporters get the role of the tier the total has reached (``announce=False``:
    without a line on the channel).  Never raises (0 when it could not be
    counted): an event must not fail over its statistics.
    """
    player_id = _player_id(player)
    if player_id is None or counter not in COUNTERS:
        return 0
    try:
        async with scene.bot.db.transaction() as con:
            total = _number(await con.fetchval(
                """INSERT INTO ACHIEVEMENT_COUNTERS (PLAYER_ID, NAME, VALUE) VALUES ($1, $2, $3)
                   ON CONFLICT (PLAYER_ID, NAME) DO UPDATE
                       SET VALUE = ACHIEVEMENT_COUNTERS.VALUE + EXCLUDED.VALUE
                   RETURNING VALUE""", player_id, counter, int(amount)))
        log.info("Achievements: player %s has %s = %d", player_id, counter, total)
        await grant_reached(scene, player, counter, total, announce=announce)
        return total
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Could not count %s for player %s.", counter, player_id)
        return 0


# -- one-off achievements -----------------------------------------------------------

async def unicorn(scene: "Scene", player: Any) -> bool:
    """A supporter hunted down a legendary monster."""
    await grant(scene, player, "unicorn")
    return True


async def legend(scene: "Scene", player: Any) -> bool:
    """A supporter took part in killing a legendary boss."""
    await grant(scene, player, "legend")
    return True


async def quickness(scene: "Scene", player: Any, seconds: float | None = None) -> bool:
    """A supporter beat a boss fast: the role of the best speed tier reached.

    ``seconds``: the average time of a turn (see :data:`QUICK_LIMITS`: under
    2 s, under 1 s, under 0.75 s); None = "under 2 s", as the old bot called it.
    """
    tier = trophy("quick_1") if seconds is None else _quick_tier(float(seconds))
    if tier is not None:
        await grant(scene, player, tier.key)
    return True


async def tower_sprint(scene: "Scene", player: Any, level: int, seconds: float) -> bool:
    """A supporter cleared a high floor of the tower fast (see :data:`SPRINT_FLOOR`)."""
    if int(level) >= SPRINT_FLOOR and 0 <= float(seconds) < SPRINT_SECONDS:
        return await grant(scene, player, "tower_sprint")
    return False


# -- what the pets stand for -----------------------------------------------------------

async def _pet_keys(bot: "AlterBot", player_id: int) -> set[str]:
    """Achievements the player's pets (the active one and the stable) stand for."""
    owner = await bot.db.fetchrow(
        f"SELECT {', '.join(_PET_COLUMNS)} FROM PETOWNER WHERE PLAYER_ID = $1", player_id)
    if owner is None:
        return set()
    owned = [pet_id for pet_id in (_number(owner[column.lower()]) for column in _PET_COLUMNS) if pet_id > 0]
    keys: set[str] = set()
    if len(owned) == len(_PET_COLUMNS):
        keys.add("stable_full")
    if not owned:
        return keys
    for pet in await bot.db.fetch("SELECT * FROM PETS WHERE PET_ID = ANY($1::numeric[])", owned):
        row = dict(pet)  # (SEAL and HOLO are new in v2: a table without them means "none")
        if row.get("mythic") is True:
            keys.add("pet_mythic")
        if row.get("ultra_shiny") is True:
            keys.add("pet_ultra")
        if _number(row.get("pet_skills")) >= DIVINE_TALENT:
            keys.add("pet_divine")
        if _number(row.get("seal")) == RAREST_SEAL:
            keys.add("pet_seal")
        if row.get("holo") is True:
            keys.add("pet_holo")
    return keys


async def check_pets(scene: "Scene", player: Any, *, announce: bool = True) -> list[Trophy]:
    """Give a supporter what their pets stand for (a mythic pet, Holo, the rarest seal ...).

    Call it whenever a player's pets changed: a new egg, a pet grew or was
    rerolled, a pet went to the stable.  Returns what was given now.  Never raises.
    """
    player_id = _player_id(player)
    if player_id is None:
        return []
    try:
        bot = scene.bot
        member = player if hasattr(player, "roles") else await bot.member(player_id)
        if member is None or not patrons.check_if_patron(member):
            return []
        given = []
        for key in sorted(await _pet_keys(bot, player_id)):
            if await grant(scene, member, key, announce=announce):
                given.append(trophy(key))
        return given
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a drop or a reroll must not fail over an achievement
        log.exception("Could not check the pet achievements of %s.", player_id)
        return []


# -- what the data says (veterans) -----------------------------------------------------

async def deserved(bot: "AlterBot", player_id: int) -> set[str]:
    """Keys of everything the game's data says the player has achieved (every tier reached).

    What leaves no trace in the data cannot be found here: the speed of a past
    boss fight, a legendary monster or boss, a mythic boss.
    """
    keys: set[str] = set()
    values: dict[str, float] = {}
    row = await bot.db.fetchrow(
        "SELECT BOSS_KILLS, HUNTS, BUTCHERS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", player_id)
    if row is not None:
        values.update({chain: _number(row[column.lower()]) for column, chain in _COLUMN_CHAINS.items()})
    for counted in await bot.db.fetch(
            "SELECT NAME, VALUE FROM ACHIEVEMENT_COUNTERS WHERE PLAYER_ID = $1", player_id):
        if str(counted["name"]) in COUNTERS:
            values[str(counted["name"])] = _number(counted["value"])
    tower = await bot.db.fetchrow("SELECT TOWER_LEVEL, TIME FROM TOWER WHERE PLAYER_ID = $1", player_id)
    if tower is not None:
        level = _number(tower["tower_level"])
        values["tower"] = level
        seconds = _seconds(tower["time"])  # the time of the best floor
        if level >= SPRINT_FLOOR and seconds is not None and seconds < SPRINT_SECONDS:
            keys.add("tower_sprint")
    for item in TROPHIES:
        if item.chain in _COUNTED_CHAINS and values.get(item.chain, 0) >= item.threshold:
            keys.add(item.key)
    keys |= await _pet_keys(bot, player_id)
    return keys


async def sync(scene: "Scene", member: Any, *, announce: bool = False) -> list[Trophy]:
    """Give a supporter every role the data says they have earned; returns what was given now.

    This is how veterans get the tiers they passed long ago: of each family
    the highest tier reached is given (it replaces the lower ones).
    """
    player_id = _player_id(member)
    if player_id is None or not patrons.check_if_patron(member):
        return []
    bot = scene.bot
    keys = await deserved(bot, player_id)
    given: list[Trophy] = []
    handled: set[str] = set()
    for item in reversed(TROPHIES):  # the highest tier of a family first
        if item.key not in keys or (item.chain and item.chain in handled):
            continue
        if _holds_this_or_higher(bot, member, item):
            handled.add(item.chain)
        elif await grant(scene, member, item.key, announce=announce):
            given.append(item)
            handled.add(item.chain)
        # (else: the role of this tier cannot be given now - the next lower tier is tried)
    await _tidy_tiers(bot, member, {item.chain for item in given})
    return given[::-1]


async def _tidy_tiers(bot: Any, member: Any, skip: Any = ()) -> None:
    """Take away the roles of tiers below the highest one the member holds (never raises).

    The old bot gave the tower roles floor by floor every week, also to
    somebody who had a higher one, so players hold two tiers of a family.
    ``skip``: families a role was given in a moment ago (already tidied; the
    member's roles may not show the new one yet).
    """
    seen: set[str] = set(skip) | {""}
    for item in TROPHIES:
        if item.chain in seen:
            continue
        seen.add(item.chain)
        tiers = chain_of(item)
        top = max((index for index, tier in enumerate(tiers) if _holds(bot, member, tier)), default=0)
        for tier in tiers[:top]:
            if _holds(bot, member, tier):
                await io.remove_role(member, _role(bot, role_id_of(bot, tier)),
                                     reason="AlterMMO RPG: wyższe osiągnięcie")


def _in_action(bot: Any) -> bool:
    """True while something plays on the boss channel (the hand-out waits: nothing may stutter)."""
    state = bot.state
    return bool(state.busy) or state.boss_phase in (BossPhase.ENGAGED, BossPhase.FIGHTING)


async def sync_veterans(bot: "AlterBot") -> tuple[int, int]:
    """Give every supporter on the server the roles their data stands for, quietly.

    Run after every start (and by ``$nadajosiagniecia``): veterans get the new
    tiers, players who started to support get what they earned before.
    Nothing is announced on the game channels; the owner gets one summary.
    Returns ``(players who got something, roles given)``.
    """
    guild = bot.get_guild(bot.ids.guild)
    if guild is None:
        return 0, 0
    rows = await bot.db.fetch(
        """SELECT PLAYER_ID FROM ACHIEVEMENTS UNION SELECT PLAYER_ID FROM ACHIEVEMENT_COUNTERS
           UNION SELECT PLAYER_ID FROM TOWER UNION SELECT PLAYER_ID FROM PETOWNER""")
    quiet = types.SimpleNamespace(bot=bot)  # nothing is sent: a scene in name only
    lines: list[str] = []
    roles = 0
    for row in rows:
        member = guild.get_member(_number(row["player_id"]))
        if member is None or not patrons.check_if_patron(member):
            continue
        while _in_action(bot):
            await asyncio.sleep(QUIET_POLL)
        try:
            given = await sync(quiet, member, announce=False)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one player's trouble must not stop the others
            log.exception("Could not hand out the achievements of %s.", member.id)
            continue
        if given:
            roles += len(given)
            lines.append(f"<@{member.id}>: " + ", ".join(item.name for item in given))
            log.info("Veteran achievements of %s: %s", member.id, ", ".join(item.key for item in given))
        await asyncio.sleep(0)
    if lines:
        chunk = LOG_VETERANS.format(roles=roles, players=len(lines))
        for line in lines:
            if len(chunk) + len(line) + 1 > LOG_CHUNK:
                await bot.log_to_owner(chunk)
                chunk = line[:LOG_CHUNK]
            else:
                chunk += "\n" + line
        await bot.log_to_owner(chunk)
    return len(lines), roles


async def start_up(bot: "AlterBot") -> None:
    """After a start: make the missing roles, then hand out what veterans have earned.  Never raises."""
    try:
        await ensure_roles(bot)  # (everything, whatever failed in an earlier run)
        players, roles = await sync_veterans(bot)
        log.info("Achievements at start-up: %d role(s) given to %d player(s).", roles, players)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the game goes on; $osiagniecia hands out one by one
        log.exception("Achievements at start-up failed.")


# -- $osiagniecia ---------------------------------------------------------------------

def held(bot: Any, member: Any, extra: Any = ()) -> tuple[list[Trophy], int]:
    """What the member's roles say: ``(what to show, how many achievements that is)``.

    Shown: the highest tier of every family and the single achievements, in
    the order of the catalogue.  Counted: a tier counts together with the
    tiers below it (their roles were taken away when the higher one came).
    ``extra``: achievements given a moment ago (the member's roles may not show them yet).
    """
    extra_keys = {item.key for item in extra}

    def has(item: Trophy) -> bool:
        return item.key in extra_keys or _holds(bot, member, item)

    shown: list[Trophy] = []
    count = 0
    seen: set[str] = set()
    for item in TROPHIES:
        if not item.chain:
            if has(item):
                shown.append(item)
                count += 1
            continue
        if item.chain in seen:
            continue
        seen.add(item.chain)
        tiers = chain_of(item)
        top = max((index for index, tier in enumerate(tiers) if has(tier)), default=None)
        if top is not None:
            shown.append(tiers[top])
            count += top + 1
    return shown, count


def summary_embed(bot: Any, member: Any, new: Any = ()) -> discord.Embed:
    """``$osiagniecia``: what the member has and how many of how many - never what is missing."""
    shown, count = held(bot, member, new)
    lines = [MSG_SUMMARY.format(player_id=member.id, earned=count, total=len(TROPHIES)), ""]
    lines += [f"🏅 <@&{role_id_of(bot, item)}>" for item in shown] or [MSG_SUMMARY_NONE]
    if new:
        lines += ["", MSG_SUMMARY_NEW.format(roles=", ".join(f"<@&{role_id_of(bot, item)}>" for item in new))]
    embed = discord.Embed(title=MSG_SUMMARY_TITLE, description="\n".join(lines)[:4000], color=ROLE_COLOUR)
    embed.set_footer(text=MSG_SUMMARY_FOOTER)
    return embed


async def show_achievements(scene: "Scene") -> bool:
    """``$osiagniecia`` / the panel button; False for somebody who does not support (he is told)."""
    member = scene.author
    if member is None or not patrons.check_if_patron(member):
        links = " • ".join(f"[{label}](<{url}>)" for label, url in patrons.SUPPORT_LINKS)
        text = MSG_SUPPORTERS_ONLY.format(groups=patrons.SUPPORTERS_LONG, links=links)
        # (Public, like every answer: it says whom it answers.)
        await scene.send(f"<@{member.id}> {text}" if member is not None else text)
        return False
    try:
        new = await sync(scene, member, announce=False)  # what was earned before the roles existed
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the database is away: the roles alone are enough to answer
        log.exception("Could not check what %s has earned; showing the roles held.", member.id)
        new = []
    await scene.send(embed=summary_embed(scene.bot, member, new))
    return True


# -- roles ------------------------------------------------------------------------

async def set_achiev_role(scene: "Scene", player: Any, role_id: int, *, announce: bool = True) -> bool:
    """Give an achievement role and announce it on the scene's channel.

    A higher tier takes the lower tiers of the same achievement away.  Returns
    True when the role was granted now; False when the player already had it,
    left the server, or the role cannot be given (the owner is told why).
    Whether the player deserves the role (supporter check) is the caller's job.
    ``announce=False``: nothing is sent (the hand-out to veterans).
    """
    bot = scene.bot
    player_id = _player_id(player)
    if player_id is None:
        return False

    role = _role(bot, role_id)
    if role is None:
        log.error("Achievement role %s does not exist on the server.", role_id)
        if _told_once(bot, ("role-missing", role_id)):
            await bot.log_to_owner(LOG_ROLE_MISSING.format(role_id=role_id, player_id=player_id))
        return False

    member = await bot.member(player_id)  # fresh from the guild, as the legacy code did
    if member is None:
        log.info("Achievement role %s not granted: player %s left the server.", role, player_id)
        return False
    if _has_role(member, role_id):
        return False

    if not await io.add_role(member, role, reason="AlterMMO RPG: osiągnięcie"):
        if _told_once(bot, ("role-not-given", role_id)):
            await bot.log_to_owner(LOG_ROLE_NOT_GIVEN.format(role=role, player_id=player_id))
        return False
    log.info("Achievement role %s granted to %s (%s).", role, member, player_id)

    if announce:
        try:
            await scene.send(f"<@{player_id}> zdobywa osiągnięcie: {role}!")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # The role is already given; a lost announcement must not abort the
            # fight/hunt that is handing out its rewards.
            log.warning("Could not announce achievement %s of %s: %s: %s",
                        role, player_id, type(exc).__name__, exc)

    for lower_id in lower_tier_roles(role_id, bot):
        if _has_role(member, lower_id):
            await io.remove_role(member, _role(bot, lower_id), reason="AlterMMO RPG: wyższe osiągnięcie")
    return True
