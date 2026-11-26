"""Aura towarzysza: a rare aura a companion hatches with (owner, 2026-11-26, ``items_decisions.md`` §8).

The rules, and only the rules - this module touches no database and no Discord
(the storage, the sum over a player's pets and every text on the screen are
:mod:`altermmo.features.pets`'):

* **At hatching only.**  When an egg hatches (its first level, ``pets._grow_in``
  at level 0 - every egg source goes through it), after the level's own talent
  draws, ONE roll decides whether the pet is surrounded by an aura
  (:func:`roll`).  Pets that hatched before the auras existed never get one.
* **The chance and the kinds' weights** are the owner's to tune live in
  ``config/auraConfig.json`` (:func:`load`, read anew before every level-up's
  transaction):  ``{"chance_per_10000": N, "kinds": {"mroz": 35, ...}}``.
  Whatever is wrong with the file means chance 0 - it never raises, so a typo
  can never cost a player his level.  The file ships with ``0``; turning the
  auras on is a separate change of that one number.
* **The values are code constants** (:data:`KINDS`): where an aura starts, its
  step and the most ONE pet's aura can reach.  Owner, 2026-11-26: "nie rób
  odgórnych globalnych limitów z wszystkich źródeł. Tylko jak już to limity na
  towarzysza" - the per-pet maximum is the ONLY limit: there is no cap on the
  sum over a player's pets and none on a pet's own stat plus the auras.
* **Growth.**  Every later level-up of an aura pet (1→2, 2→3) has a 50% chance
  of one more step, up to its maximum (:func:`grow`).
* **What it does** (``pets.get_pet_skills``): every HATCHED pet the player owns
  - at his side, in the stable, away on a mission - adds its aura to the stat
  of the aura's kind in every fight that reads the pet's stats (boss, group,
  duel, hunt, tower), also when no pet (or an egg, or a pet on a mission) is at
  his side.  Not in training, missions or the boss detection.
* **What never touches it:** a reroll, ``!oswiecenie``, the Pióro Żar-ptaka,
  ``!transformacja``, ``!nazwij`` (they save the stats without the aura
  columns).  A sacrifice loses it (an album copy is only a picture).

The draws are made with roll sizes nobody else uses (:data:`AURA_ROLL`,
:data:`GROWTH_ROLL` - the test fixtures that fix the cosmetic rolls of an egg
must not decide an aura by accident), and NO draw at all is made while the
chance is 0 or for a pet without an aura: the draws of every other level-up
stay exactly what they were.
"""

from __future__ import annotations

import json
import logging
import math
import random
from fractions import Fraction
from typing import Any, Iterable, Mapping, NamedTuple

from ..settings import CONFIG_DIR

log = logging.getLogger(__name__)

CONFIG_FILE = "auraConfig.json"

#: ``chance_per_10000`` is a chance out of this.
CHANCE_SCALE = 10_000
#: The hatching roll: ``random.randrange(AURA_ROLL)`` below the chance's share of it (0.001% steps,
#: so every ``chance_per_10000`` with one decimal is exact).  A size no other roll of the game uses.
AURA_ROLL = 100_000
#: The growth roll of a later level-up: ``random.randrange(GROWTH_ROLL) < GROWTH_HITS`` - 50%.
GROWTH_ROLL, GROWTH_HITS = 2, 1


class Kind(NamedTuple):
    """One kind of aura: the stat it adds to and its words.  The numbers are the owner's (code constants)."""

    #: The ``PETS`` column it adds to - also what ``PETS.AURA`` stores and the key of ``pets.get_pet_skills``.
    stat: str
    #: Its key in ``auraConfig.json``'s ``kinds``.
    ident: str
    emoji: str
    #: "Aura Mrozu" / ... "otoczony Aurą Mrozu" / "ma Aurę Mrozu".
    name: str
    instrumental: str
    accusative: str
    #: The stat after "do": "+3 % do spowolnienia przeciwnika".
    stat_words: str
    #: The stat as the pet's statistics call it (the stable's sum of auras).
    stat_label: str
    #: The value at hatching, one growth step, the most ONE pet's aura can reach.
    start: int
    step: int
    max: int


#: Every aura, in the order the texts list them (owner's table of 2026-11-26).
KINDS: dict[str, Kind] = {kind.stat: kind for kind in (
    Kind("SLOW_PERC", "mroz", "❄️", "Aura Mrozu", "Aurą Mrozu", "Aurę Mrozu", "spowolnienia przeciwnika",
         "Spowolnienie przeciwnika", 3, 2, 7),
    Kind("CRIT_PERC", "furia", "🔥", "Aura Furii", "Aurą Furii", "Aurę Furii", "szansy na krytyczne uderzenie",
         "Szansa na krytyczne uderzenie", 2, 1, 4),
    Kind("DROP_PERC", "szczescie", "🍀", "Aura Szczęścia", "Aurą Szczęścia", "Aurę Szczęścia", "rzadszego dropu",
         "Rzadszy drop", 3, 2, 7),
    Kind("DEF_PERC", "opieka", "🛡️", "Aura Opieki", "Aurą Opieki", "Aurę Opieki", "szansy na zablokowanie ataku",
         "Szansa na zablokowanie ataku", 1, 1, 3),
)}
#: The kinds by their key in the config.
BY_IDENT: dict[str, Kind] = {kind.ident: kind for kind in KINDS.values()}
#: The owner's weights of the kinds: used when the config has no ``kinds`` at all.
DEFAULT_WEIGHTS: dict[str, int] = {"mroz": 35, "furia": 30, "szczescie": 25, "opieka": 10}

# -- texts ---------------------------------------------------------------------------------------

#: The note of a hatching with an aura (under the drop, the mine's loot, the owner's !PetLvlUp).
MSG_HATCHED = ("🌟 Z jajka wykluł się towarzysz otoczony **{aura}**! Aura pomaga Ci zawsze - nawet gdy "
               "towarzysz odpoczywa w stajni.")
#: The note of a growth (``value``: the aura's new value) - the drop, the mine, !PetLvlUp, a mission's level.
MSG_GREW = "🌟 **{aura}** Twojego towarzysza przybrała na sile (+{value} %)!"
#: ``!towarzysz`` of an aura pet, after its statistics.
MSG_SHOWN = "🌟 {aura}: +{value} % do {stat} - działa zawsze, także gdy towarzysz jest w stajni."
#: ``!stajnia``: the auras of all the player's pets, summed per stat (no cap, so no "maks.").
STABLE_HEADER = "🌟 **Aury Twoich towarzyszy:** {parts}"
STABLE_PART = "{emoji} {label} +{value} %"
#: ... and the line of each aura pet.
STABLE_MARK = " · 🌟 {aura}"
#: Before a sacrifice of an aura pet (``!porzucam``, the mine's swap).
MSG_SACRIFICE_WARNING = "Uwaga: ten towarzysz ma **{aura}** - porzucając go, stracisz ją na zawsze."


# -- the config ----------------------------------------------------------------------------------

class Config(NamedTuple):
    """The usable part of ``auraConfig.json``."""

    #: How many of the :data:`AURA_ROLL` outcomes give an aura (0: none, and no draw is made).
    hits: int = 0
    #: ``(stat, weight)`` of the kinds that can come, whole weights above 0.
    weights: tuple[tuple[str, int], ...] = ()

    @property
    def on(self) -> bool:
        """True when a hatching can bring an aura at all."""
        return self.hits > 0 and bool(self.weights)


#: No auras (a broken config, a missing file, chance 0).
NO_AURAS = Config()


def _number(value: Any) -> float | None:
    """A finite number of the JSON file (``true`` / ``false`` are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def parse(raw: Any) -> Config:
    """The usable part of a decoded ``auraConfig.json``; every problem is logged.

    Not an object, or a chance that is not a number: no auras.  A chance
    outside 0-10000 is cut to the nearest end.  ``kinds`` missing: the owner's
    weights (:data:`DEFAULT_WEIGHTS`); ``kinds`` not an object: no auras; an
    unknown kind or a weight that is not a whole number of at least 0 is left
    out.
    """
    if not isinstance(raw, dict):
        log.warning("%s: the file must hold an object, not %s; no auras.", CONFIG_FILE, type(raw).__name__)
        return NO_AURAS
    chance = _number(raw.get("chance_per_10000", 0))
    if chance is None:
        log.warning("%s: chance_per_10000 %r is not a number; no auras.", CONFIG_FILE, raw.get("chance_per_10000"))
        return NO_AURAS
    if not 0 <= chance <= CHANCE_SCALE:
        log.warning("%s: chance_per_10000 %r is outside 0-%d; using the nearest of them.",
                    CONFIG_FILE, chance, CHANCE_SCALE)
        chance = min(max(chance, 0.0), float(CHANCE_SCALE))
    hits = math.floor(Fraction(str(chance)) * AURA_ROLL / CHANCE_SCALE)

    kinds = raw.get("kinds", DEFAULT_WEIGHTS)
    if not isinstance(kinds, dict):
        log.warning("%s: kinds must be an object, not %s; no auras.", CONFIG_FILE, type(kinds).__name__)
        return NO_AURAS
    weights: list[tuple[str, int]] = []
    for ident, value in kinds.items():
        kind = BY_IDENT.get(str(ident).strip().lower())
        weight = _number(value)
        if kind is None:
            log.warning("%s: unknown aura %r (known: %s); left out.", CONFIG_FILE, ident, ", ".join(BY_IDENT))
            continue
        if weight is None or weight < 0 or not weight.is_integer():
            log.warning("%s: the weight %r of aura %r is not a whole number of at least 0; left out.",
                        CONFIG_FILE, value, ident)
            continue
        if weight > 0 and all(stat != kind.stat for stat, _ in weights):
            weights.append((kind.stat, int(weight)))
    return Config(hits, tuple(weights))


def load() -> Config:
    """The config as it is on the disk right now; :data:`NO_AURAS` when it cannot be used.  Never raises.

    Called BEFORE a level-up's transaction (``pets.level_up_pet``): an error in
    here must never roll a player's level back.
    """
    path = CONFIG_DIR / CONFIG_FILE
    try:
        # (utf-8-sig: a file saved by an editor that adds a BOM is read all the same.)
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        log.warning("%s is missing; no auras.", path)
        return NO_AURAS
    except (OSError, ValueError, RecursionError) as exc:  # unreadable, not UTF-8, not JSON, absurdly nested
        log.warning("%s cannot be read (%s: %s); no auras.", path, type(exc).__name__, exc)
        return NO_AURAS
    try:
        return parse(raw)
    except Exception:  # noqa: BLE001 - whatever the file holds, the level goes on
        log.exception("%s could not be understood; no auras.", path)
        return NO_AURAS


# -- the draws -----------------------------------------------------------------------------------

def roll(config: Config) -> tuple[str, int] | None:
    """The hatching roll: ``(stat, its start value)`` of the aura the new pet has, or None.

    Nothing is drawn at all while the config gives no aura (:attr:`Config.on`).
    Otherwise ``randrange(AURA_ROLL)`` decides whether there is one and, only
    then, ``randrange(sum of the weights)`` which kind - whole numbers, so the
    odds are exact.
    """
    if not config.on:
        return None
    if random.randrange(AURA_ROLL) >= config.hits:
        return None
    pick = random.randrange(sum(weight for _stat, weight in config.weights))
    for stat, weight in config.weights:
        if pick < weight:
            return stat, KINDS[stat].start
        pick -= weight
    return None  # pragma: no cover - the walk always ends inside the weights


def grow(stat: str, value: int) -> int | None:
    """A later level-up of a pet with the aura ``stat`` at ``value``: its new value, or None (no step).

    50% (``randrange(GROWTH_ROLL)``) for one step, never above the kind's
    maximum.  A pet at its maximum, or an aura nobody knows, draws nothing.
    """
    kind = KINDS.get(stat)
    if kind is None or value >= kind.max:
        return None
    if random.randrange(GROWTH_ROLL) >= GROWTH_HITS:
        return None
    return min(kind.max, max(value, 0) + kind.step)


class Change(NamedTuple):
    """What a level did to a pet's aura (``pets._grow_in``): it hatched with one, or it grew."""

    #: :data:`HATCHED` / :data:`GREW`.
    event: str
    stat: str
    #: The aura's value now.
    value: int


HATCHED, GREW = "hatched", "grew"


def note(change: Change) -> str:
    """The player's note about a :class:`Change` ('' for an aura nobody knows)."""
    kind = KINDS.get(change.stat)
    if kind is None:
        return ""
    if change.event == HATCHED:
        return MSG_HATCHED.format(aura=kind.instrumental)
    return MSG_GREW.format(aura=kind.name, value=int(change.value))


# -- a pet's aura --------------------------------------------------------------------------------

def _field(row: Any, column: str) -> Any:
    """``row[column]`` of a record / mapping, None when it has no such column (the old table)."""
    try:
        return row[column]
    except (KeyError, IndexError, TypeError):
        return None


def _whole(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError, ArithmeticError):
        return 0


def _hatched(row: Any) -> bool:
    """False for an egg (level 0); a row without ``pet_lvl`` is taken as the caller's word that it hatched."""
    level = _field(row, "pet_lvl")
    return level is None or _whole(level) >= 1


def aura_of(row: Any) -> tuple[Kind, int] | None:
    """``(kind, value)`` of a ``PETS`` row's aura, or None (none, a kind nobody knows, a value of 0 or less).

    The value is never above the kind's maximum (an owner's hand edit cannot
    make one pet's aura bigger than the rules allow).  A row without the
    columns (the old table) has none.
    """
    kind = KINDS.get(str(_field(row, "aura") or "").strip())
    value = _whole(_field(row, "aura_perc"))
    if kind is None or value <= 0:
        return None
    return kind, min(value, kind.max)


def totals(rows: Iterable[Any]) -> dict[str, int]:
    """The auras of a player's pets summed per stat: ``{stat: value}`` (only stats with an aura).

    One pet counts once (``PETS`` has no key: a pet's row may be there twice),
    and only a hatched one - an egg has no aura (a row without ``pet_lvl``
    is taken as the caller's word that it is hatched).  No cap on the sum
    (owner, 2026-11-26).
    """
    seen: set[int] = set()
    found: dict[str, int] = {}
    for row in rows:
        pet_id = _whole(_field(row, "pet_id"))
        if pet_id in seen:
            continue
        seen.add(pet_id)
        if not _hatched(row):
            continue
        aura = aura_of(row)
        if aura is not None:
            kind, value = aura
            found[kind.stat] = found.get(kind.stat, 0) + value
    return {stat: found[stat] for stat in KINDS if stat in found}


def shown_line(row: Any) -> str:
    """``!towarzysz``'s line about the pet's aura ('' without one)."""
    aura = aura_of(row)
    if aura is None:
        return ""
    kind, value = aura
    return MSG_SHOWN.format(aura=kind.name, value=value, stat=kind.stat_words)


def stable_mark(row: Any) -> str:
    """What the stable adds to the line of an aura pet ('' without one, and for an egg)."""
    aura = aura_of(row)
    if aura is None or not _hatched(row):
        return ""
    return STABLE_MARK.format(aura=aura[0].name)


def stable_header(summed: Mapping[str, int]) -> str:
    """The stable's line with the auras of all the player's pets (:func:`totals`); '' without any."""
    parts = [STABLE_PART.format(emoji=kind.emoji, label=kind.stat_label, value=summed[stat])
             for stat, kind in KINDS.items() if summed.get(stat, 0) > 0]
    return STABLE_HEADER.format(parts=" · ".join(parts)) if parts else ""


def sacrifice_warning(row: Any) -> str:
    """The warning before an aura pet is sacrificed ('' without one)."""
    aura = aura_of(row)
    if aura is None or not _hatched(row):
        return ""
    return MSG_SACRIFICE_WARNING.format(aura=aura[0].accusative)
