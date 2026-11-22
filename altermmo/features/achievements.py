"""Achievements: the mythic collector's roles, and the old counters of a player's deeds.

Owner's decision of 2026-11-20 (evening): the server has no room for many
roles, so only the mythic achievements stay -

* **Mityczny boss** - killed a mythic boss (the role the game always gave:
  ``bot.ids.mythic_slayer_role``, handed out by ``boss.set_mythic_slayer``);
* **Mityczny towarzysz** - owns a mythic pet;
* **Mityczny Holo** - owns a mythic pet with the Holo foil;
* **Mityczna Pieczęć Arcyłowcy** - owns a mythic pet with the rarest seal.

and, added on 2026-11-21 together with the pet album (owner: "Zrób osiągnięcie
za pełną kolekcję każdego typu peta") - the only addition to the cut-down catalogue:

* **Pełny album** - the album (:mod:`altermmo.features.album`) covers every
  slot of the collection: every non-mythic species at every stage, whatever
  the frame / foil / seal.  Checked after every sacrifice that was kept
  (:func:`check_album`) and by the start-up / ``$nadajosiagniecia`` hand-out
  (:func:`deserved`); like every role here it is never taken away - also not
  when entries are removed from the album later.

The four roles of the pets and of the album are made by the bot itself (:func:`ensure_roles`; their
ids are remembered in ``data/achievementRoles.json``, the album's also in
``data/achievementRolesLater.json``, see :data:`LATER_KEYS`) and are for supporters
(:func:`patrons.check_if_patron`), like every collector's extra.

Everything else is gone - also the achievements the old bot had (boss kills,
hunts, seasons, tower floors, legendary monster / boss, speed) and the ones
added earlier that day.  Their roles are deleted from the server by the bot
(:func:`retire_roles`, owner: "bot kasuje role"); who held them is written to
the log first.

The counters of the legacy table stay and keep counting - they are a player's
statistics and cost nothing:

``ACHIEVEMENTS`` (PLAYER_ID NUMERIC PRIMARY KEY, BOSS_KILLS NUMERIC, HUNTS NUMERIC, BUTCHERS NUMERIC)

(``ACHIEVEMENT_COUNTERS``, made on 2026-11-20 for the achievements that were
withdrawn the same day, is no longer used; the table is left alone - the bot
never drops anything.)

``$osiagniecia`` (supporters) shows what the player has and "X of Y" - never
which ones are missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
import discord

from ..core import io
from ..core.db import DatabaseUnavailable
from ..core.state import BossPhase
from ..core.storage import JsonFile
from . import patrons

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- the legacy counters ---------------------------------------------------------------

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

# -- roles that are withdrawn ------------------------------------------------------------

#: Roles of the achievements the old bot had: deleted from the server by :func:`retire_roles`.
#: (Not the "Rzeźnik Potworów" role of the season's winner - that one is no achievement.)
RETIRED_ROLES: dict[int, str] = {
    1125799224966131712: "zabite bossy I",
    1125799392989949973: "zabite bossy II",
    1125799470509084762: "zabite bossy III",
    1125799891923374121: "polowania I",
    1125800644826112043: "polowania II",
    1125800721724489758: "polowania III",
    1125801055758860329: "wygrane sezony I",
    1125801161631486033: "wygrane sezony II",
    1125801216375529563: "wygrane sezony III",
    1151427369886826496: "Wieża Śmierci I",
    1151427619741511710: "Wieża Śmierci II",
    1151427790973964328: "Wieża Śmierci III",
    1125819170534211664: "legendarny potwór",
    1125818827809226862: "legendarny boss",
    1125818642450370590: "szybkość",
}

# -- the catalogue ---------------------------------------------------------------------

#: Colour of the roles the bot makes (the owner may change names, colours and positions on
#: the server afterwards: the bot knows its roles by id).
ROLE_COLOUR = 0xC9A227
ROLE_REASON = "AlterMMO RPG: rola osiągnięcia"
RETIRE_REASON = "AlterMMO RPG: osiągnięcie wycofane"
#: ``PETS.SEAL`` of the rarest seal ("Pieczęć Arcyłowcy").
RAREST_SEAL = 3
#: The active pet and the four places of the stable.
_PET_COLUMNS = ("PET_ID", "PET_ID_ALT1", "PET_ID_ALT2", "PET_ID_ALT3", "PET_ID_ALT4")


@dataclass(frozen=True)
class Trophy:
    """One achievement = one role.

    The role is ``bot.ids.<ids_attr>`` (a role the rest of the game hands out)
    or, without it, a role the bot makes itself (:func:`ensure_roles`).
    """

    key: str
    name: str
    ids_attr: str = ""

    @property
    def bot_made(self) -> bool:
        return not self.ids_attr


#: Every achievement, in the order ``$osiagniecia`` shows them.
TROPHIES: tuple[Trophy, ...] = (
    Trophy("mythic_boss", "Mityczny boss", ids_attr="mythic_slayer_role"),
    Trophy("pet_mythic", "Mityczny towarzysz"),
    Trophy("pet_mythic_holo", "Mityczny Holo"),
    Trophy("pet_mythic_seal", "Mityczna Pieczęć Arcyłowcy"),
    Trophy("album_full", "Pełny album"),
)
_BY_KEY: dict[str, Trophy] = {item.key: item for item in TROPHIES}

#: Bot-made achievements added after the version of 2026-11-20 (commit bde7ce0).  That version's
#: :func:`retire_roles` deletes the role of every key in ``data/achievementRoles.json`` it does not
#: know - a rollback to it would delete "Pełny album" with the role of every holder, and the role is
#: never to be taken away.  So their role ids are never a plain key there: they are saved in
#: :data:`LATER_ROLES_FILE` (next to it), a file that version never opens, and - as a spare copy -
#: in ``data/achievementRoles.json`` under :data:`LATER_ROLES_KEY`, in a mapping, which that version
#: reads as no id at all.  That version rewrites its file whenever it deletes a withdrawn role or
#: makes one of its own, and drops the spare copy then; the id in :data:`LATER_ROLES_FILE` stays, so
#: the role is still known after the upgrade even when the owner has renamed it (a role the bot no
#: longer knows is taken over only by its exact name, :func:`ensure_roles`).  Every bot-made
#: achievement added later goes here too.
LATER_KEYS: frozenset[str] = frozenset({"album_full"})
LATER_ROLES_KEY = "added_later"
#: The file of the :data:`LATER_KEYS` roles, in the folder of ``data/achievementRoles.json``:
#: ``{achievement key: role id}``.  Keys this version does not know are kept in it, never retired.
LATER_ROLES_FILE = "achievementRolesLater.json"

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
LOG_ROLES_FILE_UNREADABLE = ("Nie mogę odczytać pliku z rolami osiągnięć (`{path}`). Nie tworzę ani nie "
                             "usuwam ról, żeby nie narobić szkód - sprawdź plik i wpisz **$roleosiagniec**.")
LOG_ROLE_MISSING = ("Rola osiągnięcia {role_id} nie istnieje na serwerze - nie przyznano jej graczowi "
                    "<@{player_id}>. (O tej roli nie piszę więcej do następnego restartu.)")
LOG_ROLE_NOT_GIVEN = ("Nie udało się nadać roli osiągnięcia {role} graczowi <@{player_id}> - czy rola jest "
                      "poniżej roli bota? (O tej roli nie piszę więcej do następnego restartu.)")
LOG_ROLES_RETIRED = "Usunąłem z serwera role wycofanych osiągnięć ({count}): {names}."
LOG_ROLES_NOT_RETIRED = ("Nie udało się usunąć ról wycofanych osiągnięć: {names}. Sprawdź, czy są poniżej "
                         "roli bota, i wpisz **$roleosiagniec** (albo usuń je ręcznie).")
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


def trophy(key: str) -> Trophy:
    return _BY_KEY[key]


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


# -- the roles of the achievements -------------------------------------------------------

def _read_mapping(path: Path) -> dict[str, Any] | None:
    """The JSON mapping saved in ``path``; {} when the file is missing or empty, None when unreadable."""
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
    return loaded


def _later_path(bot: Any) -> Path:
    """``data/achievementRolesLater.json`` (:data:`LATER_ROLES_FILE`)."""
    return bot.store.achievement_roles.path.with_name(LATER_ROLES_FILE)


def _later_ids(loaded: dict[str, Any]) -> dict[str, int]:
    """The :data:`LATER_KEYS` ids of a saved mapping (anything else in it is ignored)."""
    return {str(key): _number(value) for key, value in loaded.items()
            if str(key) in LATER_KEYS and _number(value) > 0}


def _read_roles_file(bot: Any) -> dict[str, int] | None:
    """The saved ``{achievement key: role id}``; {} when nothing was saved yet, None when unreadable.

    One flat mapping of both files: ``data/achievementRoles.json`` and, for
    :data:`LATER_KEYS`, :data:`LATER_ROLES_FILE` (see there; its id wins over
    the spare copy nested under :data:`LATER_ROLES_KEY`, which wins over a
    plain key).  :func:`_save_roles` splits them again.  A nested key this
    version does not know, or a :data:`LATER_ROLES_KEY` that is no mapping, is
    no id: it is never retired.

    The difference matters: "nothing saved" lets the bot make the roles, an
    unreadable file - either of the two - must not (a role would be made a
    second time and the ids of the roles players already hold would be
    overwritten).  The unreadable one is remembered for the owner's note.
    """
    loaded: dict[str, dict[str, Any]] = {}
    for path in (bot.store.achievement_roles.path, _later_path(bot)):
        mapping = _read_mapping(path)
        if mapping is None:
            bot._achievement_roles_unreadable = path
            return None
        loaded[path.name] = mapping
    roles: dict[str, int] = {}
    nested: dict[str, int] = {}
    for key, value in loaded[bot.store.achievement_roles.path.name].items():
        if key == LATER_ROLES_KEY:
            if isinstance(value, dict):
                nested = _later_ids(value)
        elif _number(value) > 0:
            roles[str(key)] = _number(value)
    roles.update(nested)
    roles.update(_later_ids(loaded[LATER_ROLES_FILE]))
    return roles


def _save_roles(bot: Any, stored: dict[str, int]) -> None:
    """Write ``{achievement key: role id}`` to ``data/achievementRoles.json`` and :data:`LATER_ROLES_FILE`.

    The roles of :data:`LATER_KEYS` go to :data:`LATER_ROLES_FILE` (first: a
    crash between the two writes must not lose them) and, as a spare copy,
    under :data:`LATER_ROLES_KEY`; never as a plain key (see :data:`LATER_KEYS`).
    The keys of :data:`LATER_ROLES_FILE` this version does not know are kept.
    """
    plain: dict[str, Any] = {key: role_id for key, role_id in stored.items() if key not in LATER_KEYS}
    later = {key: role_id for key, role_id in stored.items() if key in LATER_KEYS}
    path = _later_path(bot)
    if later or path.exists():
        kept = _read_mapping(path)
        if kept is None:  # (became unreadable since it was read: the spare copy below holds the ids)
            log.error("%s is not rewritten: it cannot be read now.", path)
        else:
            JsonFile(path, dict).save({**{key: value for key, value in kept.items() if key not in LATER_KEYS},
                                       **later})
    if later:
        plain[LATER_ROLES_KEY] = later
    bot.store.achievement_roles.save(plain)


def _later_saved(bot: Any, stored: dict[str, int]) -> bool:
    """True when both files hold the :data:`LATER_KEYS` ids the bot knows, as :func:`_save_roles` writes them.

    False after the version before rewrote ``data/achievementRoles.json``
    (the spare copy is gone), or when an id is only in that file (nested or
    as a plain key) - :func:`ensure_roles` then saves them again.
    """
    later = {key: role_id for key, role_id in stored.items() if key in LATER_KEYS}
    if not later:
        return True
    main = _read_mapping(bot.store.achievement_roles.path) or {}
    nested = main.get(LATER_ROLES_KEY)
    return (_later_ids(_read_mapping(_later_path(bot)) or {}) == later
            and isinstance(nested, dict) and _later_ids(nested) == later
            and not LATER_KEYS & set(main))


def _stored_roles(bot: Any) -> dict[str, int]:
    """``{achievement key: role id}`` of the roles the bot made (read once, then remembered).

    Also holds the roles of withdrawn achievements until :func:`retire_roles`
    has deleted them.  While the file cannot be read this is empty and nothing
    is remembered (the next call reads again); see :func:`roles_file_readable`.
    """
    stored = getattr(bot, "_achievement_roles", None)
    if stored is None:
        stored = _read_roles_file(bot)
        if stored is None:
            return {}
        bot._achievement_roles = stored
    return stored


def roles_file_readable(bot: Any) -> bool:
    """False while the saved role ids cannot be read (no role is made or deleted then)."""
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


def role_id_of(bot: Any, item: Trophy) -> int | None:
    """The role of an achievement; None while the bot has not made it yet."""
    if bot is None:
        return None
    if item.ids_attr:
        return _number(getattr(getattr(bot, "ids", None), item.ids_attr, 0)) or None
    return _stored_roles(bot).get(item.key) or None


def role_exists(bot: Any, item: Trophy) -> bool:
    """True when the achievement's role is on the server."""
    return _role(bot, role_id_of(bot, item)) is not None


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
    """Make the roles of the achievements that do not exist yet; returns the names made.

    Called after every start and by ``$roleosiagniec`` (everything, at once)
    and by :func:`grant` when the role it needs is missing (``only`` = that
    achievement, ``force=False``: not within :data:`ROLE_RETRY_AFTER` of a
    failure, so a server that refuses new roles is not asked at every deed).
    A role somebody deleted on the server is made again.  A role of that name
    without permissions that is already there (the saved ids were lost) is
    taken over instead of being made twice.  Nothing is made while the saved
    ids cannot be read or the server's roles are not known.  The ids of
    :data:`LATER_KEYS` are written again when a file lacks them (after a
    rollback rewrote ``data/achievementRoles.json``).  The roles give no
    permissions and are not shown separately.  The owner is told what was
    made and - once per role - what could not be.  Never raises.
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
                    path = getattr(bot, "_achievement_roles_unreadable", None) or bot.store.achievement_roles.path
                    await bot.log_to_owner(LOG_ROLES_FILE_UNREADABLE.format(path=path), ping=True)
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
                _save_roles(bot, stored)  # at once: a crash must not orphan the role
                log.info("Achievement role %r: %s (%s).", item.name,
                         "taken over" if item.name in adopted else "made", role.id)
            if not _later_saved(bot, stored):  # (the version before rewrote its file, or an older layout)
                _save_roles(bot, stored)
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


async def retire_roles(bot: "AlterBot") -> list[str]:
    """Delete the roles of withdrawn achievements from the server; returns the names deleted.

    Owner's decision of 2026-11-20: the server has no room for them.  Deleted are

    * the roles of the old bot's achievements (:data:`RETIRED_ROLES` - fixed ids), and
    * the roles the bot made itself for achievements that are no longer in
      the catalogue (their ids are in ``data/achievementRoles.json``).

    Nothing else is ever touched: a role is deleted only by one of those ids,
    never by its name - and never a role the game or the supporters use
    (:func:`_protected_roles`), nor a saved one that is managed or carries
    permissions (a role the bot made has none), whatever the file says.  Who
    held a role is written to the log before it goes (a deleted role cannot
    be brought back with its members), so nothing is deleted while the
    server's member list is incomplete.  A role that cannot be deleted stays
    on the list for the next start / ``$roleosiagniec`` and the owner is told.
    Run after every start; once everything is gone it does nothing.  Never raises.
    """
    deleted: list[str] = []
    failed: list[str] = []
    try:
        async with _roles_lock(bot):
            guild = bot.get_guild(bot.ids.guild)
            if guild is None or getattr(guild, "unavailable", False):
                return []
            if not roles_file_readable(bot):
                return []  # (ensure_roles tells the owner)
            stored = _stored_roles(bot)
            keep = _protected_roles(bot)
            targets = _withdrawn(bot)
            if any(guild.get_role(role_id) is not None for role_id, _, _ in targets) \
                    and not getattr(guild, "chunked", True):
                # The holders are written down before a role goes: the whole member list is needed.
                try:
                    await guild.chunk()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("Cannot load the member list (%s: %s).", type(exc).__name__, exc)
                if not getattr(guild, "chunked", True):
                    log.warning("The member list is incomplete: no withdrawn role is deleted now.")
                    return []
            for role_id, label, key in targets:
                if role_id in keep:
                    log.error("Role %s (%s) is listed as withdrawn but the game still uses it; kept.",
                              role_id, label)
                    continue
                role = guild.get_role(role_id)
                if role is not None and key is not None and (
                        getattr(role, "managed", False)
                        or getattr(getattr(role, "permissions", None), "value", 0) != 0):
                    # Not a role this bot made (those have no permissions): hands off.
                    log.error("Saved role %s (%r) does not look like a role the bot made; kept.",
                              role_id, getattr(role, "name", label))
                    failed.append(str(getattr(role, "name", label)))
                    continue
                if role is not None:
                    name = str(getattr(role, "name", label))
                    holders = [int(member.id) for member in getattr(role, "members", None) or []]
                    log.info("Withdrawn achievement role %r (%s) is being deleted; held by %d member(s): %s",
                             name, role_id, len(holders), holders)
                    try:
                        await role.delete(reason=RETIRE_REASON)
                    except asyncio.CancelledError:
                        raise
                    except discord.NotFound:
                        log.info("Withdrawn achievement role %r (%s) was already gone.", name, role_id)
                    except Exception as exc:  # noqa: BLE001 - above the bot's role, no permission, Discord down
                        log.warning("Cannot delete the withdrawn achievement role %r (%s): %s: %s",
                                    name, role_id, type(exc).__name__, exc)
                        failed.append(name)
                        continue
                    deleted.append(name)
                if key is not None:  # gone (now or before): forget its id
                    stored.pop(key, None)
                    _save_roles(bot, stored)
        if deleted:
            await bot.log_to_owner(LOG_ROLES_RETIRED.format(count=len(deleted), names=", ".join(deleted)))
        if [name for name in failed if _told_once(bot, ("role-not-retired", name))]:
            await bot.log_to_owner(LOG_ROLES_NOT_RETIRED.format(names=", ".join(failed)), ping=True)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Could not delete the roles of withdrawn achievements.")
    return deleted


def _protected_roles(bot: Any) -> set[int]:
    """Roles :func:`retire_roles` never deletes, whatever the saved ids say.

    The roles of the achievements that stay, every role the game itself uses
    (``bot.ids.*_role``: hunters, the season's winner, the dead, the mythic
    slayer ...) and the supporters' roles.
    """
    protected = {role_id for role_id in (role_id_of(bot, item) for item in TROPHIES) if role_id}
    ids = getattr(bot, "ids", None)
    for name in dir(ids) if ids is not None else ():
        if name.endswith("_role") and not name.startswith("_"):
            protected.add(_number(getattr(ids, name, 0)))
    protected.update(int(role_id) for role_id in patrons.SUPPORTER_ROLES)
    protected.discard(0)
    return protected


def _withdrawn(bot: Any) -> list[tuple[int, str, str | None]]:
    """``(role id, label, saved key or None)`` of every role :func:`retire_roles` is after."""
    current = {item.key for item in TROPHIES if item.bot_made}
    targets: list[tuple[int, str, str | None]] = [(role_id, name, None) for role_id, name in RETIRED_ROLES.items()]
    targets += [(role_id, key, key) for key, role_id in list(_stored_roles(bot).items()) if key not in current]
    return targets


def retired_left(bot: Any) -> list[str]:
    """Names of withdrawn roles that are still on the server (they could not be deleted)."""
    guild = bot.get_guild(bot.ids.guild)
    if guild is None or not roles_file_readable(bot):
        return []
    protected = _protected_roles(bot)
    left = []
    for role_id, label, _key in _withdrawn(bot):
        role = guild.get_role(role_id)
        if role is not None and role_id not in protected:
            left.append(str(getattr(role, "name", label)))
    return left


def _holds(bot: Any, member: Any, item: Trophy) -> bool:
    role_id = role_id_of(bot, item)
    return role_id is not None and _has_role(member, role_id)


async def grant(scene: "Scene", player: Any, key: str, *, announce: bool = True) -> bool:
    """Give a supporter the achievement ``key``; True when it was given now.

    Nothing happens for somebody who does not support or who has it already.
    A role the bot has not made yet (or that was deleted on the server) is made first.
    """
    bot = scene.bot
    player_id = _player_id(player)
    if player_id is None:
        return False
    member = player if hasattr(player, "roles") else await bot.member(player_id)
    if member is None or not patrons.check_if_patron(member):
        return False
    item = trophy(key)
    if _holds(bot, member, item):
        return False
    if item.bot_made and not roles_file_readable(bot):
        return False  # which role it is cannot be known now (the owner has been told)
    role_id = role_id_of(bot, item)
    if item.bot_made and _role(bot, role_id) is None:
        await ensure_roles(bot, only=(item.key,), force=False)
        role_id = role_id_of(bot, item)
        if _role(bot, role_id) is None:
            return False  # (the owner has been told; tried again after ROLE_RETRY_AFTER)
    if role_id is None:
        return False
    return await set_achiev_role(scene, member, role_id, announce=announce)


# -- the legacy counters -----------------------------------------------------------------

async def _increment(bot: "AlterBot", player_id: int, column: str) -> int:
    """Add 1 to a counter (creating the player's row when needed); returns the new value."""
    start = [1 if name == column else 0 for name in _COUNTER_COLUMNS]
    # In a transaction on purpose: the db layer never re-sends it on its own,
    # so a connection hiccup cannot count the same deed twice.
    async with bot.db.transaction() as con:
        total = await con.fetchval(_INCREMENT_SQL[column], player_id, *start)
    return int(total)


async def _count(scene: "Scene", player: Any, column: str) -> bool:
    player_id = _player_id(player)
    if player_id is None:
        log.warning("Deed %s not counted: no player given (left the server?).", column)
        return False
    number = await _increment(scene.bot, player_id, column)
    log.info("Achievements: player %s has %s = %d", player_id, column, number)
    return True


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


async def add_boss_kill(scene: "Scene", player: Any) -> bool:
    """Count a killed boss (a statistic; no role comes with it any more).

    Returns True when the kill was counted (False only when ``player`` is None).
    """
    return await _count(scene, player, "BOSS_KILLS")


async def add_butcher(scene: "Scene", player: Any) -> bool:
    """Count a won season (a statistic; no role comes with it any more)."""
    return await _count(scene, player, "BUTCHERS")


async def add_hunts(scene: "Scene", player: Any) -> bool:
    """Count a won hunt (a statistic; no role comes with it any more)."""
    return await _count(scene, player, "HUNTS")


# -- what the pets stand for -----------------------------------------------------------

async def _pet_keys(bot: "AlterBot", player_id: int) -> set[str]:
    """Achievements the player's pets (the active one and the stable) stand for."""
    owner = await bot.db.fetchrow(
        f"SELECT {', '.join(_PET_COLUMNS)} FROM PETOWNER WHERE PLAYER_ID = $1", player_id)
    if owner is None:
        return set()
    owned = [pet_id for pet_id in (_number(owner[column.lower()]) for column in _PET_COLUMNS) if pet_id > 0]
    if not owned:
        return set()
    keys: set[str] = set()
    for pet in await bot.db.fetch("SELECT * FROM PETS WHERE PET_ID = ANY($1::numeric[])", owned):
        row = dict(pet)  # (SEAL and HOLO are new in v2: a table without them means "none")
        if row.get("mythic") is not True:
            continue
        keys.add("pet_mythic")
        if row.get("holo") is True:
            keys.add("pet_mythic_holo")
        if _number(row.get("seal")) == RAREST_SEAL:
            keys.add("pet_mythic_seal")
    return keys


async def check_pets(scene: "Scene", player: Any, *, announce: bool = True) -> list[Trophy]:
    """Give a supporter what their pets stand for (a mythic pet, also one with Holo / the rarest seal).

    Call it whenever a player's pets changed (a new egg ...).  Returns what
    was given now.  Never raises.
    """
    player_id = _player_id(player)
    if player_id is None:
        return []
    try:
        bot = scene.bot
        member = player if hasattr(player, "roles") else await bot.member(player_id)
        if member is None or not patrons.check_if_patron(member):
            return []
        keys = await _pet_keys(bot, player_id)
        given = []
        for item in TROPHIES:  # (in the order of the catalogue)
            if item.key in keys and await grant(scene, member, item.key, announce=announce):
                given.append(item)
        return given
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a drop must not fail over an achievement
        log.exception("Could not check the pet achievements of %s.", player_id)
        return []


# -- what the album stands for ---------------------------------------------------------

async def _album_keys(bot: "AlterBot", player_id: int, *,
                      unread: list[tuple[int, str]] | None = None) -> set[str]:
    """``{"album_full"}`` when the player's album covers the whole collection.

    An album that cannot be read - the database refuses the query, or the
    connection broke / timed out (the db layer's :class:`DatabaseUnavailable`)
    - stands for nothing: it costs only "Pełny album" this time, the pets'
    achievements are handed out all the same.  It is logged here, or - with
    ``unread`` (the start-up hand-out, one call per supporter) - noted there
    as ``(player id, error)`` for the caller's one log line.
    """
    from . import album  # (lazily: the album builds on features/pets.py, which imports this module)

    try:
        full = await album.complete(bot.db, player_id)
    except (asyncpg.PostgresError, DatabaseUnavailable) as exc:
        error = f"{type(exc).__name__}: {exc}"
        if unread is None:
            log.warning("The album of %s cannot be read (%s).", player_id, error)
        else:
            unread.append((player_id, error))
        return set()
    return {"album_full"} if full else set()


async def check_album(scene: "Scene", player: Any, *, announce: bool = True) -> list[Trophy]:
    """Give a supporter "Pełny album" when his album is full; returns what was given now.

    Called after every sacrifice that was kept in the album
    (``album.saved``).  Never raises: the sacrifice is done by then.
    """
    player_id = _player_id(player)
    if player_id is None:
        return []
    try:
        bot = scene.bot
        member = player if hasattr(player, "roles") else await bot.member(player_id)
        if member is None or not patrons.check_if_patron(member):
            return []
        if "album_full" not in await _album_keys(bot, player_id):
            return []
        return [trophy("album_full")] if await grant(scene, member, "album_full", announce=announce) else []
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a sacrifice must not fail over an achievement
        log.exception("Could not check the album achievement of %s.", player_id)
        return []


# -- what the data says (players who had it before the roles existed) --------------------

async def deserved(bot: "AlterBot", player_id: int, *,
                   unread: list[tuple[int, str]] | None = None) -> set[str]:
    """Keys of everything the game's data says the player has achieved.

    The pets (:func:`_pet_keys`) and the album (:func:`_album_keys`: an album
    that was full before the role existed; one that cannot be read gives
    nothing, see ``unread`` there).  A killed mythic boss leaves no
    trace in the data: its role is given at the kill (``boss.set_mythic_slayer``)
    and only read here.
    """
    return await _pet_keys(bot, player_id) | await _album_keys(bot, player_id, unread=unread)


async def sync(scene: "Scene", member: Any, *, announce: bool = False,
               unread: list[tuple[int, str]] | None = None) -> list[Trophy]:
    """Give a supporter every role the data says they have earned; returns what was given now.

    ``unread``: see :func:`_album_keys`.
    """
    player_id = _player_id(member)
    if player_id is None or not patrons.check_if_patron(member):
        return []
    keys = await deserved(scene.bot, player_id, unread=unread)
    given: list[Trophy] = []
    for item in TROPHIES:
        if item.key in keys and await grant(scene, member, item.key, announce=announce):
            given.append(item)
    return given


def _in_action(bot: Any) -> bool:
    """True while something plays on the boss channel (the hand-out waits: nothing may stutter)."""
    state = bot.state
    return bool(state.busy) or state.boss_phase in (BossPhase.ENGAGED, BossPhase.FIGHTING)


async def sync_veterans(bot: "AlterBot") -> tuple[int, int]:
    """Give every supporter on the server the roles their pets and their album stand for, quietly.

    Run after every start (and by ``$nadajosiagniecia``): players who had a
    mythic pet (or a full album) before the roles existed, or started to
    support later, get them.  (Everybody with an album has a ``PETOWNER`` row:
    a sacrifice empties the row's pet, it never deletes the row.)  Nothing is announced on the game channels; the owner gets one
    summary.  Returns ``(players who got something, roles given)``.  Albums
    that could not be read cost only "Pełny album" this time and are logged in
    ONE line for the whole run (an outage would otherwise write one per player).
    """
    guild = bot.get_guild(bot.ids.guild)
    if guild is None:
        return 0, 0
    rows = await bot.db.fetch("SELECT PLAYER_ID FROM PETOWNER")
    quiet = types.SimpleNamespace(bot=bot)  # nothing is sent: a scene in name only
    lines: list[str] = []
    unread: list[tuple[int, str]] = []
    roles = 0
    for row in rows:
        member = guild.get_member(_number(row["player_id"]))
        if member is None or not patrons.check_if_patron(member):
            continue
        while _in_action(bot):
            await asyncio.sleep(QUIET_POLL)
        try:
            given = await sync(quiet, member, announce=False, unread=unread)  # type: ignore[arg-type]
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
    if unread:
        log.warning("The album of %d supporter(s) could not be read; 'Pełny album' was not checked for them "
                    "this time (first: %s, %s).", len(unread), unread[0][0], unread[0][1])
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
    """After a start: delete withdrawn roles, make the missing ones, hand out what is earned.  Never raises."""
    try:
        await retire_roles(bot)
        await ensure_roles(bot)  # (everything, whatever failed in an earlier run)
        players, roles = await sync_veterans(bot)
        log.info("Achievements at start-up: %d role(s) given to %d player(s).", roles, players)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the game goes on; $osiagniecia hands out one by one
        log.exception("Achievements at start-up failed.")


# -- $osiagniecia ---------------------------------------------------------------------

def held(bot: Any, member: Any, extra: Any = ()) -> list[Trophy]:
    """The achievements the member's roles say they have, in the order of the catalogue.

    ``extra``: achievements given a moment ago (the member's roles may not show them yet).
    """
    extra_keys = {item.key for item in extra}
    return [item for item in TROPHIES if item.key in extra_keys or _holds(bot, member, item)]


def summary_embed(bot: Any, member: Any, new: Any = ()) -> discord.Embed:
    """``$osiagniecia``: what the member has and how many of how many - never what is missing."""
    shown = held(bot, member, new)
    lines = [MSG_SUMMARY.format(player_id=member.id, earned=len(shown), total=len(TROPHIES)), ""]
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

    Returns True when the role was granted now; False when the player already
    had it, left the server, or the role cannot be given (the owner is told
    why, once per role).  Whether the player deserves the role (supporter
    check) is the caller's job.  ``announce=False``: nothing is sent (the
    hand-out to veterans).
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
    return True
