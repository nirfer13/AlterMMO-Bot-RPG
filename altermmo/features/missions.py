"""Companion missions ("Zlecenia"): once a period a pet goes on one of three offered missions.

Owner's decisions (2026-11-25):

* the name is **Zlecenia** (a pet "na zleceniu"; "wyprawa" belongs to the
  Kopalnia and the tower, so no mission text uses it - :func:`load_config`
  refuses one);
* one mission per period - the hunt's period: it starts at 02:30 Warsaw time
  or at the season end (Monday 15:00), whichever came later
  (``daily.previous_daily_reset``, :func:`current_period`).  A new board is
  rolled only when the player has no row of the period AND no pet away (the
  previous mission is claimed first);
* the pet that went **stays in its PETOWNER column** and is only flagged away
  by its ``PET_MISSIONS`` row ("Model A"): away is ``STATUS = 'away' AND
  ENDS_AT > now()``, so it is back on time even while the bot is down.  Until
  then it does not fight, detect bosses, train, grow, change or move - the
  pets module owns that predicate (``pets.sql_not_away``, ``pets.away_until``,
  ``pets.missions_table_ready``) and every place that reads or changes the
  active pet asks it;
* the cosmetics (seal, frame, Holo) stay a supporters' benefit, mission eggs
  included; the rewards are the spec's (+30% on today's hard scrolls).

The rules (spec-v2 §3), every number in ``config/missionsConfig.json`` (read
at every roll, send and claim - a change needs no restart, a board already
rolled keeps its snapshot):

* three offers: slot 1 is zwykłe, slots 2 and 3 are drawn by their weights;
  after ``pity_after`` boards in a row without an epic one, slot 3 is epic
  (:func:`roll_offers`, :func:`pity_due`).  Three different missions, each
  with its hours (and their reward multiplier) and the constants of its tier
  copied into the offer;
* the chance is ``base + talent * talent_of_the_pet (at most 9) + span * fit``
  rounded half up and cut to ``min..max``; ``fit`` is the mean of
  ``min(1, stat / target)`` over the mission's needs (the boss detection
  counts 0 or 1) - exact, with :class:`fractions.Fraction`
  (:func:`chance_of`); the pet's "Zaradność w zleceniach" (``PETS.MISSION_PERC``,
  a talent stat since 2026-11-26) is added after that cut, up to
  :data:`MAX_CHANCE`;
* the outcome and the whole list of rewards are drawn ONCE, at the send, in
  the send's transaction (:func:`roll_outcome`), and paid at the claim in ONE
  transaction with the status flip (:func:`claim`, :func:`_grant`) - a crash
  rolls everything back and the mission stays due; a second claim finds
  nothing to pay.  A reward that cannot be given (no room for an egg, every
  stable place already, the Duża sakwa already, ...) is paid as scrolls /
  shards instead (``compensation``);
* the paid reroll ("Nowe zlecenia", :func:`reroll`), the recall ("Zawołaj z
  powrotem", :func:`recall`: no reward, the period's mission is used up) and
  the Zwój przywołania (:func:`use_summon_scroll`: a mythic boss; nothing is
  taken when it cannot come, and it is given back when it did not come).

What the player sees (§8): ONE panel (:class:`MissionPanel`, posted by
:func:`show_board` - the hub's "Zlecenia" button, ``!zlecenia`` and the button
under a "towarzysz wrócił" post) that shows whatever his missions are at:
the board of the period, the choice of the pet with its chance, the progress
of the pet that is away, the result of the mission that came back, or "done
for today".  Every view is rebuilt from the database at every click (a purge,
a restart or a second panel lose nothing), every answer is public, and the
buttons that only change the view have the gate of the display buttons (no
edit between the prompts of a fight); the ones that change the game (send,
reroll, recall, summon) have the gameplay gate.  A send and a reroll name the
board the panel showed (its reroll count) and the reroll its price, so a
double click or a panel left behind pays and sends nothing it did not show;
no pet leaves while a boss fight runs (the fight has read its stats already,
:func:`_boss_fight`).  A mission that has ended is
paid when its player opens the panel - or by the notifier :func:`watch`
("missions-watch"), which also tells the channel that the pet is back, but
only while the channel is quiet.  A result is shown to the player once in
full (``SEEN_AT``): in his panel, or as the post whose "Zlecenia" button he
clicked.  The pictures (``assets/missions/``) are optional: without them the
views have no image.

The locks (§7.1): every roll / reroll / send / claim / recall runs under the
player's advisory lock (``pets._lock_player``); the send then locks his
PETOWNER row, the pet's PETS row and the mission row, in that order.  Every
write of PETOWNER / PETS is a core of the pets module.

This module owns the table.  The rows are never deleted: the table is the
history of every board (``offered`` -> ``away`` -> ``paid``, or ``expired`` /
``recalled``).  JSON lives in TEXT columns (the project has no jsonb codec).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, NamedTuple, Sequence

import asyncpg
import discord

from ..core import challenge, clock, io, ui
from ..core.db import DatabaseUnavailable
from ..core.state import BossPhase, EventType
from ..settings import ASSETS_DIR, CONFIG_DIR
from . import auras, daily, pets, shortcuts

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- the table ---------------------------------------------------------------------------

TABLE = "PET_MISSIONS"

SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        MISSION_ID    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        PLAYER_ID     NUMERIC NOT NULL,
        PERIOD_START  TIMESTAMP WITH TIME ZONE NOT NULL,
        ROLLED_AT     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        OFFERS        TEXT NOT NULL,
        REROLLS       INTEGER NOT NULL DEFAULT 0,
        STATUS        TEXT NOT NULL DEFAULT 'offered',
        CHOSEN        INTEGER,
        PET_ID        NUMERIC,
        PET_PLACE     INTEGER,
        PET_SNAPSHOT  TEXT,
        CHANCE        INTEGER,
        ROLL          INTEGER,
        OUTCOME       TEXT,
        SENT_AT       TIMESTAMP WITH TIME ZONE,
        ENDS_AT       TIMESTAMP WITH TIME ZONE,
        PAID_AT       TIMESTAMP WITH TIME ZONE,
        RESULT        TEXT,
        ANNOUNCED_AT  TIMESTAMP WITH TIME ZONE,
        SEEN_AT       TIMESTAMP WITH TIME ZONE,
        CONSTRAINT PET_MISSIONS_STATUS_CK CHECK (STATUS IN ('offered', 'away', 'paid', 'recalled', 'expired')),
        CONSTRAINT PET_MISSIONS_SENT_CK CHECK (STATUS IN ('offered', 'expired')
            OR (PET_ID IS NOT NULL AND ENDS_AT IS NOT NULL AND CHANCE IS NOT NULL AND OUTCOME IS NOT NULL)),
        CONSTRAINT PET_MISSIONS_PERIOD_UQ UNIQUE (PLAYER_ID, PERIOD_START))""",
    f"CREATE UNIQUE INDEX IF NOT EXISTS PET_MISSIONS_ONE_AWAY_IDX ON {TABLE} (PLAYER_ID) WHERE STATUS = 'away'",
    f"CREATE INDEX IF NOT EXISTS PET_MISSIONS_AWAY_PET_IDX ON {TABLE} (PET_ID) WHERE STATUS = 'away'",
    f"CREATE INDEX IF NOT EXISTS PET_MISSIONS_DUE_IDX ON {TABLE} (ENDS_AT) WHERE STATUS = 'away'",
    f"CREATE INDEX IF NOT EXISTS PET_MISSIONS_NEWS_IDX ON {TABLE} (PAID_AT) WHERE STATUS = 'paid' AND ANNOUNCED_AT IS NULL",
    f"CREATE INDEX IF NOT EXISTS PET_MISSIONS_UNSEEN_IDX ON {TABLE} (PLAYER_ID) WHERE STATUS = 'paid' AND SEEN_AT IS NULL",
)
#: New in v2: the old bot's database has no missions (``schema.create_new_tables``).
NEW_TABLES: tuple[str, ...] = SCHEMA
#: The table has no columns that came later (yet).
NEW_COLUMNS: tuple[tuple[str, str, str], ...] = ()

# -- names of the rules ------------------------------------------------------------------

CONFIG_FILE = CONFIG_DIR / "missionsConfig.json"
#: The pictures of the views and of the summon (``assets/missions/<name>.*``, all optional).
PICTURES_DIR = ASSETS_DIR / "missions"
PICTURE_SUFFIXES = (".gif", ".png", ".jpg", ".jpeg", ".webp")

#: The rarities (the config's keys, in order: an offer stores the index).
TIERS: tuple[str, ...] = ("zwykle", "rzadkie", "epickie")
#: How the texts show them (the boss rarity squares; "zlecenie" is neuter).
TIER_SQUARE: tuple[str, ...] = ("🟩", "🟦", "🟪")
TIER_WORD: tuple[str, ...] = ("zwykłe", "rzadkie", "epickie")
#: The ``PETS`` stats a mission can need with a target; :data:`DETECTION` counts 0 or 1.
NUMERIC_STATS: tuple[str, ...] = ("CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "DROP_PERC", "LOWHP_PERC", "SLOW_PERC")
DETECTION = "DETECTION"
STATS: tuple[str, ...] = (*NUMERIC_STATS, DETECTION)
#: The talent the chance counts at most (an enlightened pet reaches 9).
MAX_TALENT = 9
#: The most a chance can be once "Zaradność w zleceniach" is added (:func:`chance_of`): never certain.
MAX_CHANCE = 98

#: Every kind of reward, in the order :func:`draw_rewards` draws them (and a result lists them).
REWARD_KINDS: tuple[str, ...] = ("scrolls", "shards", "egg", "rare_egg", "great_egg", "level", "stone", "mirror",
                                 "place", "pouch", "summon")
#: The rewards whose ``success`` value is an expected count (the rest are chances).
COUNTED_KINDS: tuple[str, ...] = ("scrolls", "shards")
EGG_KINDS: tuple[str, ...] = ("egg", "rare_egg", "great_egg")
#: The rewards that can fail to be given at the claim (``compensation`` of the config).
COMPENSATED_KINDS: tuple[str, ...] = ("egg", "rare_egg", "great_egg", "level", "place", "pouch", "summon")
#: What a compensation can be paid in.
COMPENSATION_ITEMS: tuple[str, ...] = ("scrolls", "shards")
#: The ★ specialty as the texts name it (its chance - or count - is doubled).
STAR_WORDS: dict[str, str] = {
    "egg": "🥚 jajko", "rare_egg": "🥚 rzadkie jajko", "great_egg": "🥚 nadzwyczajne jajko",
    "scrolls": "📜 zwoje odrodzenia", "shards": "🧩 fragmenty zwoju", "level": "⬆️ poziom dla towarzysza",
    "stone": "💎 kamień olśnienia", "mirror": "🪞 lustro wizualiów", "place": "🐴 miejsce w stajni",
    "pouch": "👝 lepsza sakwa", "summon": "📯 Zwój przywołania",
}

#: ``OUTCOME.result``.
SUCCESS, GREAT, FAIL = "success", "great", "fail"
#: The statuses of :func:`send` / :func:`reroll`.
OK, CHANGED, CHANCE_CHANGED, EXPIRED, GONE, OFF, WAITING = (
    "ok", "changed", "chance_changed", "expired", "gone", "off", "waiting")
USED, POOR = "used", "poor"
#: ... also: the board was rerolled since the panel showed it (``stale``), a boss fight runs (``boss``:
#: a fight reads the pet's stats before its countdown), the reroll's price is not the one shown (``price``).
STALE, BOSS, PRICE = "stale", "boss", "price"
#: ``RESULT`` of a recalled mission.
RECALLED_RESULT = {"recalled": True}
#: News of a paid mission older than this is marked announced without a post (§7.8).
STALE_NEWS_HOURS = 12

#: The text limits of the catalog (a button label is ``"N. " + name``, at most 80).
NAME_MAX, TEXT_MAX = 60, 200
_KEY = re.compile(r"[a-z0-9_]{1,24}")
#: Words no mission text may use: "wyprawa" is the Kopalnia's and the tower's; "$" was the old prefix.
FORBIDDEN_WORDS: tuple[str, ...] = ("wyprawa", "$")
#: The line of a success whose mission is no longer in the config.
MSG_SUCCESS_FALLBACK = "Zlecenie wykonane!"


# -- the config ----------------------------------------------------------------------------

class ConfigError(ValueError):
    """``config/missionsConfig.json`` cannot be used (the board cannot be rolled; the owner is told)."""


@dataclass(frozen=True)
class Tier:
    """One rarity of the config (``tiers.<key>``)."""

    key: str
    index: int
    #: The hours an offer can take and their reward multipliers (same length).
    hours: tuple[int, ...]
    mult: tuple[int | float, ...]
    base: int
    talent: int
    span: int
    min: int
    max: int
    #: ``{numeric stat: target}``.
    targets: Mapping[str, int | float]
    #: ``{kind: expected count (scrolls, shards) / chance (the rest)}`` - every kind but ``summon``.
    success: Mapping[str, Fraction]
    #: ``{hours: chance}`` of the Zwój przywołania (not multiplied).
    summon: Mapping[int, Fraction]
    failure_shards: int


@dataclass(frozen=True)
class Mission:
    """One mission of the catalog (``missions``)."""

    key: str
    tier: int
    emoji: str
    name: str
    needs: tuple[str, ...]
    star: str
    flavour: str
    success: str


class EggRule(NamedTuple):
    """How a mission egg is rolled (``eggs.<kind>``): at least this class, this many cosmetic rerolls."""

    min_class: int
    rerolls: int


@dataclass(frozen=True)
class Config:
    #: The weights ``{tier key: weight}`` of slot 2 and slot 3 (slot 1 is always zwykłe).
    slot2: Mapping[str, int]
    slot3: Mapping[str, int]
    pity_after: int
    reroll_price: int
    rerolls_per_period: int
    great_divisor: int
    tiers: Mapping[str, Tier]
    #: ``{kind: ("scrolls" | "shards", count)}``.
    compensation: Mapping[str, tuple[str, int]]
    eggs: Mapping[str, EggRule]
    failure_lines: tuple[str, ...]
    missions: tuple[Mission, ...] = field(default=())

    def tier(self, index: int) -> Tier:
        return self.tiers[TIERS[index]]


_TOP_KEYS = ("offers", "reroll_price", "rerolls_per_period", "great_divisor", "tiers",
             "compensation", "eggs", "failure_lines", "missions")
#: Keys the file may have and that are not read: ``_opis`` (the owner's note) and ``summon_cap`` - the
#: cap of the Zwoje przywołania, gone since 2026-11-26 (owner: "no caps anywhere").  The shipped file
#: keeps it, because a bot of the code before (still running until its restart) re-reads the file
#: and requires it.
_IGNORED_TOP_KEYS = ("_opis", "summon_cap")
_TIER_KEYS = ("hours", "mult", "base", "talent", "span", "min", "max", "targets", "success", "failure_shards")
_MISSION_KEYS = ("key", "tier", "emoji", "name", "needs", "star", "flavour", "success")


def load_config(path: Path | None = None) -> Config:
    """Read and check the missions' config; :class:`ConfigError` when it is unusable.

    Strict (spec §5.1): every key must be there, an unknown one is refused
    (``_opis`` at the top is the owner's note), every number in its range, no
    mission text says "wyprawa" or "$", each tier has three missions at least
    (a board of three different missions is always possible).
    """
    path = CONFIG_FILE if path is None else path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _parse(raw)
    except ConfigError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ConfigError(f"{path.name}: {type(exc).__name__}: {exc}") from exc


def _parse(raw: Any) -> Config:
    _keys(raw, _TOP_KEYS, "the file", optional=_IGNORED_TOP_KEYS)
    offers = raw["offers"]
    _keys(offers, ("slot2", "slot3", "pity_after"), "offers")
    slot2, slot3 = (_weights(offers[slot], f"offers.{slot}") for slot in ("slot2", "slot3"))
    tiers_raw = raw["tiers"]
    _keys(tiers_raw, TIERS, "tiers")
    tiers = {key: _tier(key, index, tiers_raw[key]) for index, key in enumerate(TIERS)}
    compensation_raw = raw["compensation"]
    _keys(compensation_raw, COMPENSATED_KINDS, "compensation")
    compensation = {kind: _compensation(compensation_raw[kind], f"compensation.{kind}") for kind in COMPENSATED_KINDS}
    eggs_raw = raw["eggs"]
    _keys(eggs_raw, EGG_KINDS, "eggs")
    eggs = {}
    for kind in EGG_KINDS:
        entry = eggs_raw[kind]
        _keys(entry, ("min_class", "rerolls"), f"eggs.{kind}")
        eggs[kind] = EggRule(_whole(entry["min_class"], f"eggs.{kind}.min_class", 0, pets.MYTHIC_FROM - 1),
                             _whole(entry["rerolls"], f"eggs.{kind}.rerolls", 0, pets.MAX_COSMETIC_REROLLS))
    lines = raw["failure_lines"]
    if not isinstance(lines, list) or not lines:
        raise ConfigError("failure_lines must be a list that is not empty")
    failure_lines = tuple(_text(line, f"failure_lines[{number}]", TEXT_MAX) for number, line in enumerate(lines))
    missions = tuple(_mission(entry, number, tiers) for number, entry in enumerate(_list(raw["missions"], "missions")))
    if len({mission.key for mission in missions}) != len(missions):
        raise ConfigError("a mission key is used twice")
    for index, key in enumerate(TIERS):
        if sum(1 for mission in missions if mission.tier == index) < 3:
            raise ConfigError(f"tier {key} has fewer than 3 missions (a board needs 3 different ones)")
    return Config(slot2, slot3, _whole(offers["pity_after"], "offers.pity_after", 0),
                  _whole(raw["reroll_price"], "reroll_price", 0),
                  _whole(raw["rerolls_per_period"], "rerolls_per_period", 0),
                  _whole(raw["great_divisor"], "great_divisor", 1), tiers, compensation, eggs, failure_lines, missions)


def _tier(key: str, index: int, raw: Any) -> Tier:
    what = f"tiers.{key}"
    _keys(raw, _TIER_KEYS, what)
    hours = tuple(_whole(hour, f"{what}.hours", 1, 48) for hour in _list(raw["hours"], f"{what}.hours"))
    mult_raw = _list(raw["mult"], f"{what}.mult")
    for value in mult_raw:
        if _number(value, f"{what}.mult") <= 0:
            raise ConfigError(f"{what}.mult must be above 0 ({_shown(value)})")
    if not hours or len(hours) != len(mult_raw):
        raise ConfigError(f"{what}: hours and mult must be lists of the same length (not empty)")
    low, high = _whole(raw["min"], f"{what}.min", 0, 100), _whole(raw["max"], f"{what}.max", 0, 100)
    if low > high:
        raise ConfigError(f"{what}: min is above max")
    targets_raw = raw["targets"]
    _keys(targets_raw, NUMERIC_STATS, f"{what}.targets")
    targets = {}
    for stat in NUMERIC_STATS:
        if _number(targets_raw[stat], f"{what}.targets.{stat}") <= 0:
            raise ConfigError(f"{what}.targets.{stat} must be above 0")
        targets[stat] = targets_raw[stat]
    success_raw = raw["success"]
    _keys(success_raw, REWARD_KINDS, f"{what}.success")
    success = {}
    for kind in REWARD_KINDS:
        if kind == "summon":
            continue
        value = _number(success_raw[kind], f"{what}.success.{kind}")
        if value < 0 or (kind not in COUNTED_KINDS and value > 1):
            raise ConfigError(f"{what}.success.{kind} must be "
                              + ("0 or more" if kind in COUNTED_KINDS else "a chance from 0 to 1"))
        success[kind] = value
    summon_raw = success_raw["summon"]
    if not isinstance(summon_raw, dict):
        raise ConfigError(f"{what}.success.summon must be an object {{hours: chance}}")
    summon = {}
    for hour, chance in summon_raw.items():
        if not str(hour).isdigit() or int(hour) not in hours:
            raise ConfigError(f"{what}.success.summon: {_shown(hour)} is not one of the tier's hours")
        value = _number(chance, f"{what}.success.summon.{hour}")
        if not 0 <= value <= 1:
            raise ConfigError(f"{what}.success.summon.{hour} must be a chance from 0 to 1")
        summon[int(hour)] = value
    return Tier(key, index, hours, tuple(mult_raw), _whole(raw["base"], f"{what}.base"),
                _whole(raw["talent"], f"{what}.talent"), _whole(raw["span"], f"{what}.span"), low, high, targets,
                success, summon, _whole(raw["failure_shards"], f"{what}.failure_shards", 0))


def _mission(raw: Any, number: int, tiers: Mapping[str, Tier]) -> Mission:
    what = f"missions[{number}]"
    _keys(raw, _MISSION_KEYS, what)
    key = raw["key"]
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ConfigError(f"{what}.key must be 1-24 of a-z, 0-9, _ ({_shown(key)})")
    what = f"mission {key}"
    if raw["tier"] not in TIERS:
        raise ConfigError(f"{what}: tier must be one of {', '.join(TIERS)}")
    tier = tiers[raw["tier"]]
    needs = _list(raw["needs"], f"{what}.needs")
    if not 1 <= len(needs) <= 3 or len(set(map(str, needs))) != len(needs) or any(need not in STATS for need in needs):
        raise ConfigError(f"{what}: needs must be 1-3 different stats of {', '.join(STATS)}")
    star = raw["star"]
    if star not in REWARD_KINDS:
        raise ConfigError(f"{what}: star must be one of {', '.join(REWARD_KINDS)}")
    has_chance = bool(tier.summon) if star == "summon" else tier.success[star] > 0
    if not has_chance:
        raise ConfigError(f"{what}: the star {star} has no chance in tier {tier.key}")
    return Mission(key, tier.index, _text(raw["emoji"], f"{what}.emoji", 16),
                   _text(raw["name"], f"{what}.name", NAME_MAX), tuple(needs), star, _text(raw["flavour"], f"{what}.flavour", TEXT_MAX),
                   _text(raw["success"], f"{what}.success", TEXT_MAX))


def _keys(raw: Any, wanted: Sequence[str], what: str, *, optional: Sequence[str] = ()) -> None:
    if not isinstance(raw, dict):
        raise ConfigError(f"{what} must be an object")
    missing = [key for key in wanted if key not in raw]
    unknown = [key for key in raw if key not in wanted and key not in optional]
    if missing:
        raise ConfigError(f"{what}: missing {', '.join(missing)}")
    if unknown:
        raise ConfigError(f"{what}: unknown {', '.join(map(str, unknown))}")


def _list(raw: Any, what: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ConfigError(f"{what} must be a list")
    return raw


def _whole(value: Any, what: str, low: int | None = None, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{what} must be a whole number ({_shown(value)})")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ConfigError(f"{what} must be from {low if low is not None else '...'} "
                          f"to {high if high is not None else '...'} ({value})")
    return value


def _number(value: Any, what: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{what} must be a number ({_shown(value)})")
    return _exact(value)


def _weights(raw: Any, what: str) -> dict[str, int]:
    _keys(raw, TIERS, what)
    weights = {key: _whole(raw[key], f"{what}.{key}", 0) for key in TIERS}
    if not sum(weights.values()):
        raise ConfigError(f"{what}: the weights add up to 0")
    return weights


def _compensation(raw: Any, what: str) -> tuple[str, int]:
    if not isinstance(raw, dict) or len(raw) != 1 or next(iter(raw)) not in COMPENSATION_ITEMS:
        raise ConfigError(f'{what} must be {{"scrolls": n}} or {{"shards": n}}')
    item, count = next(iter(raw.items()))
    return item, _whole(count, f"{what}.{item}", 0)


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{what} must be a text")
    if len(value) > limit:
        raise ConfigError(f"{what} is longer than {limit} characters")
    lowered = value.lower()
    for word in FORBIDDEN_WORDS:
        if word in lowered:
            raise ConfigError(f"{what} contains {word!r}")
    return value


def _shown(value: Any) -> str:
    """A config value for an error text: its ``repr``, cut short."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001
        text = f"<{type(value).__name__}>"
    return text if len(text) <= 40 else text[:37] + "..."


def _exact(value: Any) -> Fraction:
    """A number of the config / a snapshot / the database as an exact fraction (``1.2`` is 6/5, not the float)."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(repr(value))
    return Fraction(value)


def find_mission(config: Config, key: Any) -> Mission | None:
    """The catalog's mission ``key`` (None when the config no longer has it)."""
    return next((mission for mission in config.missions if mission.key == key), None)


def mission_name(key: Any) -> str:
    """The name of the mission ``key`` for a text (the key itself when the config cannot tell).  Never raises."""
    try:
        mission = find_mission(load_config(), key)
    except Exception:  # noqa: BLE001 - a text: the key is enough
        mission = None
    return mission.name if mission is not None else str(key)


def picture(name: str) -> Path | None:
    """``assets/missions/<name>.<gif|png|jpg|jpeg|webp>`` (first found that is not empty), or None.

    Looked up every time: a picture the owner adds works without a restart.
    """
    for suffix in PICTURE_SUFFIXES:
        path = PICTURES_DIR / f"{name}{suffix}"
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    log.debug("Missions: no picture %s.", name)
    return None


# -- the period, the offers, the chance, the outcome (pure; ``random`` is this module's) -------------

def current_period(at: datetime | None = None) -> datetime:
    """The start of the period ``at`` (now) is in, in UTC: the key of the player's board (``PERIOD_START``).

    ``daily.previous_daily_reset``, but steady through the hour that repeats
    when the clocks go back (the last Sunday of October, 02:00-03:00 Warsaw
    twice): the wall-clock arithmetic of ``clock.previous_daily`` would give
    that second pass the day before's reset and then a second 02:30 - so a
    moment of the second pass counts as 03:00, whose period is the day's one
    reset (the first 02:30).
    """
    local = (at if at is not None else clock.now()).astimezone(clock.WARSAW)
    if local.fold:
        local = local.replace(minute=0, second=0, microsecond=0, fold=0) + timedelta(hours=1)
    return daily.previous_daily_reset(local).astimezone(clock.UTC)


def _offer(config: Config, mission: Mission, choice: int) -> dict[str, Any]:
    """The snapshot of ``mission`` with the ``choice``-th hours of its tier - what ``OFFERS`` keeps.

    Everything the chance needs is copied, so a later change of the config
    never changes an offer that is already shown.  :data:`DETECTION` has the
    target 1 (it counts 0 or 1).
    """
    tier = config.tier(mission.tier)
    return {"key": mission.key, "tier": mission.tier, "hours": tier.hours[choice], "mult": tier.mult[choice],
            "star": mission.star,
            "needs": [[need, 1 if need == DETECTION else tier.targets[need]] for need in mission.needs],
            "base": tier.base, "talent": tier.talent, "span": tier.span, "min": tier.min, "max": tier.max}


def _draw_tier(weights: Mapping[str, int]) -> int:
    return random.choices(range(len(TIERS)), weights=[weights[key] for key in TIERS])[0]


def roll_offers(config: Config, *, pity: bool = False) -> list[dict[str, Any]]:
    """Three offers: slot 1 zwykłe, slots 2 and 3 by their weights (slot 3 epic with ``pity``).

    Three different missions (each drawn evenly from the missions of its tier
    not on the board yet), each with hours drawn evenly from its tier's.
    """
    tiers = [0, _draw_tier(config.slot2), 2 if pity else _draw_tier(config.slot3)]
    offers: list[dict[str, Any]] = []
    used: set[str] = set()
    for index in tiers:
        pool = [mission for mission in config.missions if mission.tier == index and mission.key not in used]
        mission = random.choice(pool)
        used.add(mission.key)
        offers.append(_offer(config, mission, random.randrange(len(config.tier(index).hours))))
    return offers


def _has_epic(offers: Any) -> bool:
    """Whether a stored board (``OFFERS``, the JSON text or the list) had an epic offer (unreadable: no)."""
    try:
        board = _load(offers) if isinstance(offers, str) else offers
        return any(int(offer["tier"]) == 2 for offer in board)
    except Exception:  # noqa: BLE001 - an unreadable board counts as one without an epic
        return False


def pity_due(boards: Sequence[Any], pity_after: int) -> bool:
    """Whether slot 3 must be epic: the last ``pity_after`` boards (newest first, any status) had no epic offer.

    ``pity_after`` 0: always (after 0 boards without an epic one).
    """
    return len(boards) >= pity_after and not any(_has_epic(board) for board in boards[:max(pity_after, 0)])


def _value(pet: Any, column: str) -> Any:
    """``pet[column]`` of a record / mapping (None when it has no such key)."""
    try:
        return pet[column]
    except (KeyError, IndexError, TypeError):
        return None


def _part(stat: str, target: Any, pet: Any) -> Fraction:
    """How much of one need the pet meets: 0 .. 1 (NULL and negative values count as 0)."""
    if stat == DETECTION:
        return Fraction(1 if _value(pet, "detection") else 0)
    value = _value(pet, stat.lower())
    try:
        have = Fraction(0) if value is None else _exact(value)
    except (TypeError, ValueError, ArithmeticError):
        have = Fraction(0)
    if have <= 0:
        return Fraction(0)
    return min(Fraction(1), have / _exact(target))


def fit_of(offer: Mapping[str, Any], pet: Any) -> Fraction:
    """The mean over the offer's needs of what the pet meets of each (exact)."""
    needs = offer["needs"]
    if not needs:
        return Fraction(0)
    return sum((_part(stat, target, pet) for stat, target in needs), Fraction(0)) / len(needs)


def resourcefulness_of(pet: Any) -> int:
    """The pet's "Zaradność w zleceniach" (``MISSION_PERC``) in whole percent; 0 without it (or the column)."""
    value = _value(pet, pets.MISSION_PERC_COLUMN)
    try:
        return max(math.floor(_exact(value)), 0) if value is not None else 0
    except (TypeError, ValueError, ArithmeticError):
        return 0


def chance_of(offer: Mapping[str, Any], pet: Any) -> int:
    """The whole percent of success of ``pet`` (a ``PETS`` row: lower-case keys) on ``offer`` (a snapshot).

    ``floor(base + talent * min(PET_SKILLS, 9) + span * fit + 1/2)``, cut to
    ``min .. max`` - computed with fractions, so no float decides a percent.
    A NULL stat or talent counts as 0, a NULL detection as none.

    Then the pet's "Zaradność w zleceniach" (owner, 2026-11-26) is added AFTER
    that cut: ``min(MAX_CHANCE, cut + MISSION_PERC)`` - it always helps (it is
    not part of ``fit`` and no mission needs it), but :data:`MAX_CHANCE` is the
    absolute ceiling: a mission is never certain.  A pet without it keeps the
    cut as it is.
    """
    talent = min(max(pets._int(_value(pet, "pet_skills")), 0), MAX_TALENT)
    raw = (_exact(offer["base"]) + _exact(offer["talent"]) * talent + _exact(offer["span"]) * fit_of(offer, pet)
           + Fraction(1, 2))
    cut = max(int(offer["min"]), min(int(offer["max"]), math.floor(raw)))
    bonus = resourcefulness_of(pet)
    if bonus <= 0:
        return cut
    return max(cut, min(MAX_CHANCE, cut + bonus))


def _paid(expected: Fraction) -> int:
    """An expected count as a whole one: its floor, plus 1 with the chance of its fraction."""
    whole = math.floor(expected)
    rest = expected - whole
    return whole + (1 if rest > 0 and random.random() < rest else 0)


def draw_rewards(config: Config, offer: Mapping[str, Any], *, great: bool = False, level: int = 0) -> list[dict]:
    """The rewards of a success on ``offer`` (``level``: the pet's level at the send).

    Each kind is an independent draw (in :data:`REWARD_KINDS` order; a kind
    with no chance draws nothing): scrolls and shards are expected counts
    times the offer's multiplier (twice that on a Wielki sukces), the rest
    chances times the multiplier (capped at 1); the ★ kind counts double; the
    Zwój przywołania's chance depends on the hours only; a level only for a
    pet at level 1-2.
    """
    tier = config.tier(int(offer["tier"]))
    mult, star, hours = _exact(offer["mult"]), offer.get("star"), int(offer["hours"])
    rewards: list[dict[str, Any]] = []
    for kind in REWARD_KINDS:
        boost = 2 if kind == star else 1
        if kind in COUNTED_KINDS:
            count = _paid(tier.success[kind] * mult * boost * (2 if great else 1))
            if count > 0:
                rewards.append({"kind": kind, "n": count})
            continue
        if kind == "level" and not 1 <= level < pets.MAX_LEVEL:
            continue
        chance = tier.summon.get(hours, Fraction(0)) * boost if kind == "summon" else tier.success[kind] * mult * boost
        if chance > 0 and random.random() < min(chance, Fraction(1)):
            rewards.append({"kind": kind})
    return rewards


def roll_outcome(config: Config, offer: Mapping[str, Any], chance: int, *, level: int = 0) -> dict[str, Any]:
    """What the mission will bring - drawn at the send (§3.4); the claim only pays it.

    ``roll = randrange(100)``: a success when below ``chance``, a Wielki sukces
    when below ``chance // great_divisor``.  A success keeps the mission's
    own line and the rewards (:func:`draw_rewards`); a failure one of the
    shared failure lines and the tier's ``failure_shards``.
    """
    roll = random.randrange(100)
    if roll >= chance:
        line = config.failure_lines[random.randrange(len(config.failure_lines))]
        shards = config.tier(int(offer["tier"])).failure_shards
        return {"result": FAIL, "roll": roll, "line": line,
                "rewards": [{"kind": "shards", "n": shards}] if shards > 0 else []}
    great = roll < chance // config.great_divisor
    mission = find_mission(config, offer.get("key"))
    return {"result": GREAT if great else SUCCESS, "roll": roll,
            "line": mission.success if mission is not None else MSG_SUCCESS_FALLBACK,
            "rewards": draw_rewards(config, offer, great=great, level=level)}


def snapshot(pet: Any) -> dict[str, Any]:
    """What the texts remember of the pet that went (``PET_SNAPSHOT``): they work after it is gone.

    ``name`` is how the texts call it (``pets._called``: markdown-safe, its
    species when it was never named); ``type`` guards the level reward
    against a reused ``PET_ID``.
    """
    return {"name": pets._called(pet), "type": str(_value(pet, "type")), "level": pets._int(_value(pet, "pet_lvl")),
            "talent": pets._int(_value(pet, "pet_skills")), "shiny": bool(_value(pet, "shiny")),
            "ultra_shiny": bool(_value(pet, "ultra_shiny")), "mythic": bool(_value(pet, "mythic"))}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(text: Any) -> Any:
    """A JSON column (None for NULL or what cannot be read)."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        log.warning("Missions: a JSON column cannot be read: %r", str(text)[:80])
        return None


# -- the table's state ---------------------------------------------------------------------

async def ready(bot: "AlterBot") -> bool:
    """True when ``PET_MISSIONS`` exists (``pets.missions_table_ready``: cached)."""
    return await pets.missions_table_ready(bot)


async def ensure_table(bot: "AlterBot") -> bool:
    """Make the table when it is not there (the database did not answer at the start); True when it is now.

    Idempotent (``IF NOT EXISTS``), in one transaction, as the album makes its
    own.  A failure is logged: the caller says the missions are unavailable.
    """
    try:
        async with bot.db.transaction() as con:
            for statement in SCHEMA:
                await con.execute(statement)
        log.warning("Table %s was missing (not made at start-up); it is made now.", TABLE)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - (two panels at once: the other one made it)
        log.warning("Table %s could not be made: %s: %s", TABLE, type(exc).__name__, exc)
    return await pets.missions_table_ready(bot, refresh=True)


# -- the roll (§7.2) and the reroll (§7.3) ---------------------------------------------------------

_SQL_EXPIRE = f"""UPDATE {TABLE} SET STATUS = 'expired'
                  WHERE PLAYER_ID = $1 AND STATUS = 'offered' AND PERIOD_START < $2"""
#: The boards before the period ``$2`` (the reroll's own board does not count), newest first.
_SQL_RECENT = f"""SELECT OFFERS FROM {TABLE} WHERE PLAYER_ID = $1 AND PERIOD_START < $2
                  ORDER BY PERIOD_START DESC LIMIT $3"""
_SQL_ROLL = f"""INSERT INTO {TABLE} (PLAYER_ID, PERIOD_START, OFFERS)
                SELECT $1, $2, $3
                WHERE NOT EXISTS (SELECT 1 FROM {TABLE} WHERE PLAYER_ID = $1 AND STATUS = 'away')
                ON CONFLICT (PLAYER_ID, PERIOD_START) DO NOTHING
                RETURNING *"""
_SQL_REROLL = f"""UPDATE {TABLE} SET OFFERS = $3, REROLLS = REROLLS + 1, ROLLED_AT = now()
                  WHERE MISSION_ID = $1 AND PLAYER_ID = $4 AND STATUS = 'offered' AND PERIOD_START = $2
                        AND REROLLS < $5 AND ($6::integer IS NULL OR REROLLS = $6::integer)
                  RETURNING *"""


async def _pity(con: Any, player_id: int, period: datetime, config: Config) -> bool:
    if config.pity_after <= 0:
        return True
    rows = await con.fetch(_SQL_RECENT, player_id, period, config.pity_after)
    return pity_due([row["offers"] for row in rows], config.pity_after)


async def roll_board(bot: "AlterBot", player_id: Any) -> Any:
    """Roll the player's board of the current period; its new row, or None (nothing was rolled).

    Under the player's lock: the boards of past periods that were never sent
    expire, then ONE insert that adds nothing when the period has a row
    already (two panels, a double click) or a pet of his is away (the previous
    mission is claimed first).  On None the caller reads the state again.
    """
    player_id = pets._pid(player_id)
    config = load_config()
    period = current_period()
    async with bot.db.transaction() as con:
        await pets._lock_player(con, player_id)
        await con.execute(_SQL_EXPIRE, player_id, period)
        offers = roll_offers(config, pity=await _pity(con, player_id, period, config))
        row = await con.fetchrow(_SQL_ROLL, player_id, period, _dump(offers))
    if row is not None:
        log.info("Missions: player %s got the board %s (%s).", player_id, row["mission_id"],
                 ", ".join(offer["key"] for offer in offers))
    return row


class _Rollback(Exception):
    """Leave a transaction with nothing written; ``status`` is the answer."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


async def reroll(bot: "AlterBot", member: Any, mission_id: int, *, rerolls: int | None = None,
                 price: int | None = None) -> str:
    """"Nowe zlecenia": a fresh board in place of the offered one, for ``reroll_price`` scrolls.

    ``ok``; ``off`` (the price or the limit is 0, or no table); ``used`` (the
    period's rerolls are spent); ``poor`` (not enough scrolls - nothing
    changes); ``gone`` (the board was sent, or is from a past period).  The
    new board has the same pity as the old one.  ONE transaction: the board
    and the payment both, or neither.

    ``rerolls`` / ``price``: what the panel showed - the board's reroll count
    (a second click of a double click, or a panel left behind by a reroll in
    another one, answers ``stale`` instead of paying for a board nobody saw)
    and the price on the button (the owner changed it meanwhile: ``price``).
    """
    player_id = pets._pid(member)
    if not await ready(bot):
        return OFF
    config = load_config()
    if config.reroll_price <= 0 or config.rerolls_per_period <= 0:
        return OFF
    if price is not None and price != config.reroll_price:
        return PRICE
    period = current_period()
    try:
        async with bot.db.transaction() as con:
            await pets._lock_player(con, player_id)
            offers = roll_offers(config, pity=await _pity(con, player_id, period, config))
            row = await con.fetchrow(_SQL_REROLL, mission_id, period, _dump(offers), player_id,
                                     config.rerolls_per_period, rerolls)
            if row is None:
                board = await con.fetchrow(f"SELECT STATUS, PERIOD_START, REROLLS FROM {TABLE} "
                                           "WHERE MISSION_ID = $1 AND PLAYER_ID = $2", mission_id, player_id)
                if board is None or board["status"] != "offered" or board["period_start"] != period:
                    raise _Rollback(GONE)
                raise _Rollback(STALE if rerolls is not None and board["rerolls"] != rerolls else USED)
            if await pets.take_scrolls_in(con, player_id, config.reroll_price) is None:
                raise _Rollback(POOR)
    except _Rollback as stop:
        return stop.status
    log.info("Missions: player %s rerolled the board %s for %s scrolls (%s).", player_id, mission_id,
             config.reroll_price, ", ".join(offer["key"] for offer in offers))
    return OK


# -- the send (§7.4) -------------------------------------------------------------------------

class Sent(NamedTuple):
    """What :func:`send` did: ``ok`` (the row is away now: ``row``), ``changed`` (the pet moved, is an egg,
    lost its data or is away), ``chance_changed`` (``chance``: the new one), ``expired``, ``gone``,
    ``off`` (no table), ``waiting`` (the player has an open ``!potwierdzam`` question)."""

    status: str
    chance: int = 0
    row: Any = None


_SQL_LOCK_MISSION = f"SELECT * FROM {TABLE} WHERE MISSION_ID = $1 AND PLAYER_ID = $2 FOR UPDATE"
_SQL_SEND = f"""UPDATE {TABLE} SET STATUS = 'away', CHOSEN = $2, PET_ID = $3, PET_PLACE = $4, PET_SNAPSHOT = $5,
                       CHANCE = $6, ROLL = $7, OUTCOME = $8, SENT_AT = now(),
                       ENDS_AT = now() + make_interval(hours => $9)
                WHERE MISSION_ID = $1 AND STATUS = 'offered' RETURNING *"""


def _boss_fight(bot: "AlterBot") -> bool:
    """A boss was attacked and its fight is not over (``ENGAGED`` / ``FIGHTING``).

    The fights read the pets' stats right after the attack, seconds before
    their countdown (``boss.single_fight`` / ``boss.group_fight``) - a pet
    sent in that pause would still lend that fight all its bonuses.
    """
    return bot.state.boss_phase in (BossPhase.ENGAGED, BossPhase.FIGHTING)


async def send(bot: "AlterBot", member: Any, mission_id: int, choice: int, pet_key: str,
               shown_chance: int, *, rerolls: int | None = None) -> Sent:
    """Send the pet ``pet_key`` (``pets.MissionPet.key``) on offer ``choice`` of the board ``mission_id``.

    ONE transaction under the player's lock: his PETOWNER row, the pet's PETS
    row and the mission row are locked in that order (§7.1), the chance is
    computed again from the locked pet and must be the one the player saw
    (``shown_chance``), then the outcome is drawn (:func:`roll_outcome`) and
    the row becomes ``away`` until ``now() + hours``.  The pet stays where it
    is.  A board already sent answers ``gone`` before a pet that is away now
    answers ``changed`` (the double click).  ``rerolls``: the reroll count of
    the board the panel showed - a board rerolled since (another panel) has
    other offers under the same id: ``stale``.  While a boss fight runs
    nothing is sent (``boss``, :func:`_boss_fight`).
    """
    player_id = pets._pid(member)
    if not await ready(bot):
        return Sent(OFF)
    config = load_config()
    if player_id in pets._awaiting_confirmation:
        return Sent(WAITING)
    try:
        async with bot.db.transaction() as con:
            await pets._lock_player(con, player_id)
            status, pet, place = await pets.mission_pet_in(con, bot, player_id, pet_key)
            row = await con.fetchrow(_SQL_LOCK_MISSION, mission_id, player_id)
            if row is None or row["status"] != "offered":
                return Sent(GONE)
            if row["period_start"] != current_period():
                return Sent(EXPIRED)
            if rerolls is not None and pets._int(row["rerolls"]) != rerolls:
                return Sent(STALE)
            if status != OK:
                return Sent(CHANGED)
            offers = _load(row["offers"]) or []
            if not isinstance(choice, int) or not 0 <= choice < len(offers):
                return Sent(GONE)
            offer = offers[choice]
            chance = chance_of(offer, pet)
            if chance != shown_chance:
                return Sent(CHANCE_CHANGED, chance)
            if _boss_fight(bot):  # (as late as it can be: the attack may have come during the locks)
                return Sent(BOSS)
            outcome = roll_outcome(config, offer, chance, level=pets._int(pet["pet_lvl"]))
            sent = await con.fetchrow(_SQL_SEND, mission_id, choice, pet["pet_id"], place, _dump(snapshot(pet)),
                                      chance, outcome["roll"], _dump(outcome), int(offer["hours"]))
    except asyncpg.UniqueViolationError:  # (PET_MISSIONS_ONE_AWAY_IDX: impossible under the lock)
        log.warning("Missions: player %s already has a mission away (board %s).", player_id, mission_id)
        return Sent(GONE)
    if sent is None:
        return Sent(GONE)
    log.info("Missions: player %s sent pet %s on %s (board %s, %s h, chance %s%%, %s).", player_id,
             pet["pet_id"], offer.get("key"), mission_id, offer.get("hours"), chance, outcome["result"])
    return Sent(OK, chance, sent)


# -- the claim (§7.5, §7.6) ------------------------------------------------------------------------

@dataclass
class Granted:
    """What a claim gave: the ``RESULT`` lines, the eggs stored, the pet that levelled up."""

    #: One entry per reward of the outcome, in its order (the ``lines`` of ``RESULT``).
    lines: list[dict[str, Any]] = field(default_factory=list)
    #: ``(PET_ID, the rolled egg)`` of every egg stored.
    eggs: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    #: The ``PETS`` row of the mission pet after its level (None: no level was given).
    level: Any = None


class Claimed(NamedTuple):
    """A claim that paid: the row as the flip returned it, what was given, the album notes."""

    row: Any
    granted: Granted
    notes: list[str]


_SQL_CLAIM = f"""UPDATE {TABLE} SET STATUS = 'paid', PAID_AT = now(),
                        ANNOUNCED_AT = CASE WHEN $3::boolean THEN now() ELSE NULL END,
                        SEEN_AT = CASE WHEN $3::boolean THEN now() ELSE NULL END
                 WHERE MISSION_ID = $1 AND PLAYER_ID = $2 AND STATUS = 'away' AND ENDS_AT <= now()
                 RETURNING *"""
_SQL_RESULT = f"UPDATE {TABLE} SET RESULT = $2 WHERE MISSION_ID = $1"
#: ``EggPlaced.status`` -> ``status`` of a result's egg line.
_EGG_STATUS = {pets.EGG_ACTIVE: "active", pets.EGG_IN_POUCH: "pouch", pets.EGG_IN_STABLE: "stable"}
#: The keyword of ``pets.add_items_in`` per counter.
_ITEMS = {"scrolls": "scrolls", "shards": "shards", "stone": "stones", "mirror": "mirrors"}


async def claim(scene: "Scene", player_id: Any, mission_id: int, *, announce: bool, shown: bool) -> Claimed | None:
    """Pay a mission that has ended - exactly once; None when there is nothing to pay (paid already, not due).

    ONE transaction under the player's lock: the flip ``away`` -> ``paid``
    (``shown``: the player sees the result now, so it counts as announced and
    seen) and every grant (:func:`_grant`) commit together or not at all.
    After the commit the hooks run (:func:`_after_claim`: the eggs' hooks with
    ``announce``, the album notes) - best effort, they never raise.  A broken
    config or a database error raises (the row stays due; the caller logs it).
    """
    bot = scene.bot
    player_id = pets._pid(player_id)
    member = await bot.member(player_id)  # None: he left the server - no supporter, eggs still placed
    config = load_config()
    upgrades, big = await pets._owner_shape(bot)
    cosmetics, frames = await pets._cosmetic_state(bot)
    async with bot.db.transaction() as con:
        await pets._lock_player(con, player_id)
        row = await con.fetchrow(_SQL_CLAIM, mission_id, player_id, bool(shown))
        if row is None:
            return None
        granted = await _grant(con, bot, member, player_id, row, config, upgrades=upgrades, big=big,
                               cosmetics=cosmetics, frames=frames)
        await con.execute(_SQL_RESULT, mission_id, _dump({"lines": granted.lines, "notes": []}))
    log.info("Missions: mission %s of player %s paid: %s.", mission_id, player_id,
             ", ".join(line.get("kind", "?") + ("*" if "comp" in line else "") for line in granted.lines) or "nothing")
    notes: list[str] = []
    await _after_claim(scene, member, player_id, granted, cosmetics=cosmetics, frames=frames, announce=announce,
                       notes=notes)
    if notes:  # best effort: the album notes are kept for a later redraw
        try:
            await bot.db.execute(_SQL_RESULT, mission_id, _dump({"lines": granted.lines, "notes": notes}),
                                 idempotent=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - only the notes are lost
            log.warning("Missions: the album notes of mission %s were not kept: %s: %s", mission_id,
                        type(exc).__name__, exc)
    return Claimed(row, granted, notes)


async def _grant(con: Any, bot: "AlterBot", member: Any, player_id: int, row: Any, config: Config, *,
                 upgrades: bool, big: bool, cosmetics: bool, frames: bool) -> Granted:
    """Give the rewards of ``row``'s outcome inside the claim's transaction; every write is a pets core.

    The order (§3.5): a stable place, the pouch step, the Zwój przywołania,
    the eggs (in reward order - a new place or pouch slot can take them), the
    level, then ONE update of every counter (scrolls, shards, stones,
    mirrors and all compensations).  A reward that cannot be given is
    compensated by the current config.
    """
    outcome = _load(row["outcome"]) or {}
    pet = _load(row["pet_snapshot"]) or {}
    rewards = [reward for reward in outcome.get("rewards") or ()
               if isinstance(reward, dict) and reward.get("kind") in REWARD_KINDS]
    lines: list[dict[str, Any] | None] = [None] * len(rewards)
    counters: Counter[str] = Counter()
    granted = Granted()

    def compensate(index: int, reward_kind: str, **line: Any) -> None:
        item, count = config.compensation[reward_kind]
        counters[item] += count
        lines[index] = {**line, "comp": {item: count}}

    def each(*kinds: str):
        return [(index, reward) for index, reward in enumerate(rewards) if reward["kind"] in kinds]

    for index, _reward in each("place"):
        places = await pets.add_stable_place_in(con, player_id, pets.STABLE_MAX_PLACES) if upgrades else 0
        if places:
            lines[index] = {"kind": "place", "places": places}
        else:  # ("off": the columns are missing - not "every place already")
            compensate(index, "place", kind="place", **({} if upgrades else {"off": True}))
    for index, _reward in each("pouch"):
        to = await pets.upgrade_pouch_in(con, player_id, upgrades=upgrades, big=big)
        if to:
            lines[index] = {"kind": "pouch", "to": to}
        else:
            compensate(index, "pouch", kind="pouch", **({} if upgrades and big else {"off": True}))
    for index, _reward in each("summon"):
        # (No cap since 2026-11-26: only missing columns are compensated - a stored result of the
        # older code may still say ``"cap"``, see :data:`MSG_GOT_SUMMON_COMP`.)
        count = await pets.add_summon_scroll_in(con, player_id) if big else 0
        if count:
            lines[index] = {"kind": "summon", "count": count}
        else:
            compensate(index, "summon", kind="summon", off=True)
    for index, reward in each(*EGG_KINDS):
        kind = reward["kind"]
        rule = config.eggs[kind]
        egg = pets.roll_egg(member, cosmetics=cosmetics, frames=frames, rerolls=rule.rerolls,
                            min_class=rule.min_class)
        placed = await pets.place_egg_in(con, player_id, egg, pets.EGG_ANYWHERE, upgrades=upgrades, big=big,
                                         ready=True)
        if placed.done and placed.pet_id:
            lines[index] = {"kind": "egg", "class": kind, "status": _EGG_STATUS.get(placed.status, placed.status),
                            "place": placed.place, "pet_id": placed.pet_id, "mythic": bool(egg["MYTHIC"]),
                            "shiny": bool(egg["SHINY"]), "ultra": bool(egg["ULTRA_SHINY"])}
            granted.eggs.append((placed.pet_id, egg))
        else:
            compensate(index, kind, kind="egg", **{"class": kind})
    for index, _reward in each("level"):
        grown: list[auras.Change] = []
        done = await pets.level_mission_pet_in(con, player_id, row["pet_id"], upgrades=upgrades, big=big,
                                               species=str(pet.get("type")), aura_out=grown)
        if done is not None:
            granted.level, new = done
            lines[index] = {"kind": "level", "name": pets._called(granted.level), "new": new}
            for change in grown:  # (its aura grew: the result says so under the level - stored as data)
                lines[index].update(aura=change.stat, aura_perc=change.value)
        else:
            compensate(index, "level", kind="level", name=str(pet.get("name") or pets.DEFAULT_NAME))
    for index, reward in each(*_ITEMS):
        kind = reward["kind"]
        count = max(pets._int(reward.get("n", 1)), 0) if kind in COUNTED_KINDS else 1
        counters[kind] += count
        lines[index] = {"kind": kind, "n": count} if kind in COUNTED_KINDS else {"kind": kind}
    if any(counters.values()):
        await pets.add_items_in(con, player_id, **{_ITEMS[kind]: count for kind, count in counters.items()})
    granted.lines = [line for line in lines if line is not None]
    return granted


async def _after_claim(scene: "Scene", member: Any, player_id: int, granted: Granted, *, cosmetics: bool,
                       frames: bool, announce: bool, notes: list[str]) -> None:
    """The hooks of a paid mission, after the commit.  Never raises: the rewards are the player's already.

    Every stored egg: ``pets.after_new_egg`` (the mythic announcement only with
    ``announce``, the achievements, a supporter's album note into ``notes``).
    A level: the album's note about the MISSION pet (``granted.level``, not the
    pet at his side - it may have gone from the stable), supporters only.
    """
    for pet_id, egg in granted.eggs:
        await pets.after_new_egg(scene, member, player_id, pet_id, egg, cosmetics, frames, announce=announce,
                                 notes=notes)
    if granted.level is not None and member is not None:
        try:
            from . import album  # (lazily, as the pets module: the album builds on pets)

            await album.note_level_up(scene, member, granted.level, into=notes)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the level is the player's already
            log.exception("Missions: the album note about the level of player %s failed.", player_id)


_SQL_DUE_OF = f"SELECT MISSION_ID FROM {TABLE} WHERE PLAYER_ID = $1 AND STATUS = 'away' AND ENDS_AT <= now()"


async def claim_due(scene: "Scene", player_id: Any) -> bool:
    """Pay the player's mission that has ended but was not claimed yet - before a change the claim must
    not miss (``pets.transform_pet``: the level's guard compares the ``TYPE`` the pet was sent with).

    Nothing is posted: the result is news (the notifier tells it, the panel
    shows it once).  True when one was paid.  Never raises: the caller then
    refuses its change while the mission is still unpaid.
    """
    bot = scene.bot
    player_id = pets._pid(player_id)
    try:
        if not await ready(bot):
            return False
        mission_id = await bot.db.fetchval(_SQL_DUE_OF, player_id)
        if mission_id is None:
            return False
        return await claim(scene, player_id, int(mission_id), announce=_quiet(bot), shown=False) is not None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the caller refuses its change; the claim is tried again later
        log.exception("Missions: the ended mission of player %s could not be paid before a change.", player_id)
        return False


# -- the recall (§7.7) and the announcement (§7.8) ---------------------------------------------------

_SQL_RECALL = f"""UPDATE {TABLE} SET STATUS = 'recalled', PAID_AT = now(), ANNOUNCED_AT = now(), SEEN_AT = now(),
                         RESULT = $3
                  WHERE MISSION_ID = $1 AND PLAYER_ID = $2 AND STATUS = 'away' AND ENDS_AT > now()
                  RETURNING MISSION_ID"""


async def recall(bot: "AlterBot", player_id: Any, mission_id: int) -> bool:
    """"Zawołaj z powrotem" (asked and confirmed by the caller): the pet is back at once, with no reward.

    The period's mission is used up.  False when the mission has ended
    meanwhile (it is due: the caller shows it, which claims it).
    """
    player_id = pets._pid(player_id)
    async with bot.db.transaction() as con:
        await pets._lock_player(con, player_id)
        done = await con.fetchval(_SQL_RECALL, mission_id, player_id, _dump(RECALLED_RESULT))
    if done is not None:
        log.info("Missions: player %s recalled the pet of mission %s.", player_id, mission_id)
    return done is not None


async def reserve_announcement(bot: "AlterBot", mission_id: int) -> Any:
    """The notifier takes the news of a paid mission (its row), or None: somebody else told it already."""
    async with bot.db.transaction() as con:
        return await con.fetchrow(f"UPDATE {TABLE} SET ANNOUNCED_AT = now() "
                                  "WHERE MISSION_ID = $1 AND ANNOUNCED_AT IS NULL RETURNING *", mission_id)


async def release_announcement(bot: "AlterBot", mission_id: int) -> None:
    """The post of a reserved news failed: it is news again (posted at a later tick)."""
    await bot.db.execute(f"UPDATE {TABLE} SET ANNOUNCED_AT = NULL WHERE MISSION_ID = $1", mission_id,
                         idempotent=True)


async def mark_stale_news(bot: "AlterBot") -> int:
    """News older than :data:`STALE_NEWS_HOURS` is marked announced without a post; how many."""
    tag = await bot.db.execute(
        f"""UPDATE {TABLE} SET ANNOUNCED_AT = now()
            WHERE STATUS = 'paid' AND ANNOUNCED_AT IS NULL AND PAID_AT < now() - make_interval(hours => $1)""",
        STALE_NEWS_HOURS, idempotent=True)
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


# -- the Zwój przywołania (§10) --------------------------------------------------------------------

#: One summon at a time, whoever asks (the question included) - and who holds it (``"player"``).
_summoning = asyncio.Lock()
_summoner: dict[str, int] = {}
#: ``runtime.json``: a scroll was taken and the boss is being summoned (a crash in between: the owner is told).
SUMMON_MARKER = "missions_summon_pending"
#: The rarity of the boss a scroll summons (mythic; the modifiers do not change it).
SUMMON_RARITY = 4

MSG_SUMMON_NONE = ("Nie masz Zwoju przywołania, <@{player_id}>. Czasem wypada z bossów i najgroźniejszych "
                   "potworów, przynoszą go towarzysze z najdłuższych zleceń, a podobno i Wędrowny kupiec chowa "
                   "jakiś pod ladą.")
MSG_SUMMON_OFF = "Zwoje przywołania są chwilowo niedostępne, <@{player_id}>."
MSG_SUMMON_QUESTION = ("Czy na pewno chcesz odczytać **Zwój przywołania**, <@{player_id}>? Na kanał przybędzie "
                       "🟥 **mityczny boss** - zaatakować go może każdy <:Hmm:984767035617730620> Jeśli przez pół "
                       "godziny nikt go nie zaatakuje, bestia odejdzie, a zwój obróci się w popiół. "
                       + pets.CONFIRM_HINT)
MSG_SUMMON_BOSS_THERE = ("Boss już jest na kanale albo właśnie nadciąga, <@{player_id}>! Zwój przywołania nie "
                         "został użyty - spróbuj ponownie później <:Bedge:970576892874854400>")
MSG_SUMMON_BUSY = ("Trwa teraz inne wydarzenie, <@{player_id}>! Zwój przywołania nie został użyty - spróbuj "
                   "ponownie, gdy się zakończy <:Bedge:970576892874854400>")
#: Refusals beside an open passive event (owner, 2026-11-26: the scroll waits, nothing is closed by it).
MSG_SUMMON_MERCHANT = ("Wędrowny kupiec jeszcze nie zwinął kramu, <@{player_id}> - mityczny boss rozniósłby mu "
                       "wóz w drzazgi. Zwój przywołania nie został użyty.")
MSG_SUMMON_MINE = ("Kopalnia Ostatniej Lampy jest jeszcze otwarta, <@{player_id}> - ryk mitycznego bossa "
                   "zawaliłby sztolnię. Zwój nie został użyty.")
#: ... beside anything else (a chest, the shrine; boss spawning switched off by the owner).
MSG_SUMMON_WAIT = "Na kanale bossa jeszcze coś się dzieje, <@{player_id}> - zwój poczeka."
MSG_SUMMON_FAILED = ("Boss nie odpowiedział na wezwanie, <@{player_id}> - Zwój przywołania wraca do Ciebie. "
                     "Spróbuj ponownie później.")
MSG_SUMMON_DONE = "📯 <@{player_id}> odczytał Zwój przywołania!"
#: To the owner (with a ping) when the scroll taken for a failed summon could not be given back.
MSG_SUMMON_NOT_RETURNED = ("Zwój przywołania gracza {player_id} nie wrócił po nieudanym przywołaniu: `{error}` - "
                           "oddaj go: `!DajZwojPrzywolania`")
#: To the owner (with a ping) when the database did not answer the taking of a scroll (taken or not: unknown).
MSG_SUMMON_UNSURE = ("Nie wiadomo, czy Zwój przywołania gracza {player_id} został zabrany - baza danych nie "
                     "odpowiedziała, boss nie przybył. Sprawdź SUMMON_SCROLLS i w razie potrzeby oddaj go: "
                     "`!DajZwojPrzywolania`")
#: To the owner after a start that found the marker (a crash between taking the scroll and the boss).
MSG_SUMMON_INTERRUPTED = ("Bot wyłączył się w trakcie odczytywania Zwoju przywołania gracza <@{player_id}> "
                          "({at}). Jeśli mityczny boss się nie pojawił, oddaj zwój komendą !DajZwojPrzywolania.")


def _mention_only(member: Any) -> discord.AllowedMentions:
    return discord.AllowedMentions(everyone=False, roles=False, users=[member] if member is not None else False)


def _summon_refusal(bot: "AlterBot") -> str | None:
    """Why no boss can be summoned now (a text with ``{player_id}``), or None.

    Everything ``BossCycle.summon`` would refuse for a scroll, checked before
    anything is taken (owner, 2026-11-26): a boss there or on its way; a hunt,
    a tower run, an interactive event or a duel / ritual sign-up; an open
    passive event - the merchant, the mine, a chest or the shrine (the scroll
    waits, it closes nothing); boss spawning switched off (``!stopSpawnBoss``:
    the boss would never leave).  The caller checks a typed fight first.
    """
    from . import boss_cycle

    cycle = bot.boss_cycle
    if not cycle.can_summon():
        return MSG_SUMMON_BOSS_THERE
    state = bot.state
    if cycle.activity_in_the_way() or (
            state.event_alive and state.event_type in (EventType.DUEL, EventType.RITUAL)):
        return MSG_SUMMON_BUSY
    open_event = cycle.open_event_in_the_way()
    if open_event == boss_cycle.OPEN_MERCHANT:
        return MSG_SUMMON_MERCHANT
    if open_event == boss_cycle.OPEN_MINE:
        return MSG_SUMMON_MINE
    if open_event is not None or not cycle.enabled:
        return MSG_SUMMON_WAIT
    return None


def _mark_summon(bot: "AlterBot", value: Any) -> None:
    """Keep (or clear) :data:`SUMMON_MARKER`.  Never raises: ``runtime.json`` may be held by another
    process (``os.replace`` on Windows) - the marker only tells the owner about a crash, the summon goes on
    (as ``boss_cycle._remember_summoned``)."""
    try:
        bot.store.runtime_set(SUMMON_MARKER, value)
    except OSError:
        log.exception("Missions: the summoning marker could not be %s.", "kept" if value else "cleared")


async def use_summon_scroll(scene: "Scene") -> bool:
    """Read a Zwój przywołania (**!przywolaj** / the panel's button): a mythic boss comes to the channel.

    Nothing is taken while no boss can come (:func:`_summon_refusal`: a boss
    is there or coming, a hunt / tower / interactive event runs, a duel or
    ritual sign-up is open, the merchant / the mine / a chest / the shrine is
    open, boss spawning is off) or when the player does not confirm; the scroll is taken atomically after
    the confirmation and the conditions are checked again.  When the boss did
    not come (``summon`` answered False or failed) the scroll is given back.
    Between the taking and the boss :data:`SUMMON_MARKER` is kept in
    ``runtime.json`` - a crash there is told to the owner at the next start
    (:func:`resolve_summon_marker`) and nothing is given back on its own.
    A second call while the player's own question is open (a double click,
    **!przywolaj** twice) is told that the bot waits for his answer, not that
    another event runs; a typed fight - on the channel already (the first
    check of all, before any post) or begun during the question - gets no post
    at all (nothing is taken: no boss could come).  True when the boss came.
    """
    bot, member = scene.bot, scene.author
    player_id = pets._pid(member)
    mentions = _mention_only(member)

    async def say(text: str) -> None:
        await scene.send(text.format(player_id=player_id), allowed_mentions=mentions)

    if challenge.fight_on(scene.channel):  # (owner's rule: nothing between a fight's prompts)
        return False
    if not await pets.mission_columns_available(bot):
        await say(MSG_SUMMON_OFF)
        return False
    if _summoning.locked():
        if _summoner.get("player") == player_id:
            await scene.send(pets.MSG_ALREADY_WAITING)
        else:
            await say(MSG_SUMMON_BUSY)
        return False
    async with _summoning:
        _summoner["player"] = player_id
        try:
            summoned = await _read_scroll(scene, player_id, say, mentions)
        finally:
            _summoner.pop("player", None)
    if summoned is None:
        return False
    if summoned:
        log.info("Missions: player %s summoned a mythic boss with a scroll.", player_id)
        await say(MSG_SUMMON_DONE)
    else:
        await say(MSG_SUMMON_FAILED)
    return summoned


async def _read_scroll(scene: "Scene", player_id: int, say: Callable[[str], Awaitable[None]],
                       mentions: discord.AllowedMentions) -> bool | None:
    """:func:`use_summon_scroll` under :data:`_summoning`: whether the boss came, None when no scroll was
    taken (the player was told why - or, during a typed fight, nothing was posted)."""
    bot, member = scene.bot, scene.author
    if await pets.summon_scrolls(bot, player_id) <= 0:
        await say(MSG_SUMMON_NONE)
        return None
    refusal = _summon_refusal(bot)
    if refusal is not None:
        await say(refusal)
        return None
    with pets._confirmation_slot(player_id) as free:
        if not free:
            await scene.send(pets.MSG_ALREADY_WAITING)
            return None
        image = picture("przywolanie")
        answer = await pets._confirm(scene, member, MSG_SUMMON_QUESTION.format(player_id=player_id),
                                     allowed_mentions=mentions,
                                     **({"file_paths": [image]} if image is not None else {}))
    if not answer:
        return None
    if challenge.fight_on(scene.channel):  # a typed fight began during the question: nothing between its prompts
        log.info("Missions: player %s confirmed a summoning scroll during a typed fight; nothing taken.",
                 player_id)
        return None
    refusal = _summon_refusal(bot)  # (the channel may have changed during the question)
    if refusal is not None:
        await say(refusal)
        return None
    try:
        taken = await pets.take_summon_scroll(bot, player_id)
    except DatabaseUnavailable:  # (never retried: the scroll may be gone or not - the owner checks)
        await bot.log_to_owner(MSG_SUMMON_UNSURE.format(player_id=player_id), ping=True)
        raise
    if taken is None:
        await say(MSG_SUMMON_NONE)
        return None
    summoned = False
    try:
        _mark_summon(bot, {"player": player_id, "at": clock.utcnow().isoformat()})
        summoned = bool(await bot.boss_cycle.summon(scene, SUMMON_RARITY, origin="scroll"))
    finally:
        if not summoned:
            try:
                await pets.return_summon_scroll(bot, player_id)
                log.info("Missions: the summoning scroll of player %s was given back.", player_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the owner gives it back by hand
                log.exception("Missions: the summoning scroll of player %s was not given back.", player_id)
                await bot.log_to_owner(MSG_SUMMON_NOT_RETURNED.format(player_id=player_id, error=exc), ping=True)
        _mark_summon(bot, None)
    return summoned


def resolve_summon_marker(bot: "AlterBot") -> bool:
    """At the start: a :data:`SUMMON_MARKER` left behind means a crash between the scroll and the boss.

    Nothing is given back (nothing is ever paid twice); the owner is told -
    through the start-up notes before the bot is ready, else with a message of
    its own - and the marker is cleared.  True when there was one.  Never raises.
    """
    try:
        marker = bot.store.runtime_get(SUMMON_MARKER)
        if not marker:
            return False
        player_id = marker.get("player") if isinstance(marker, dict) else marker
        at = marker.get("at") if isinstance(marker, dict) else None
        try:
            when = ui.when(datetime.fromisoformat(str(at)))
        except (TypeError, ValueError):
            when = str(at or "?")
        note = MSG_SUMMON_INTERRUPTED.format(player_id=player_id, at=when)
        log.warning("Missions: %s", note)
        if bot.is_ready():
            asyncio.get_running_loop().create_task(bot.log_to_owner(note, ping=True))
        else:
            bot._startup_notes.append(note)
        bot.store.runtime_set(SUMMON_MARKER, None)
        return True
    except Exception:  # noqa: BLE001 - a start must never fail on this
        log.exception("Missions: the summoning marker could not be resolved.")
        return False


# -- what the player sees (§8.3, §8.4) -------------------------------------------------------------

#: The emoji and the short name of every stat a mission can need, on the board (the emoji of the duel's embed).
NEED_LOOK: dict[str, tuple[str, str]] = {
    "CRIT_PERC": ("💥", "krytyk"), "REPLACE_PERC": ("🔄", "zastąpienie"), "DEF_PERC": ("🛡️", "blok"),
    "DROP_PERC": ("🍀", "rzadszy drop"), "LOWHP_PERC": ("⚔️", "osłabienie"), "SLOW_PERC": ("🐢", "spowolnienie"),
    DETECTION: ("🔮", "wyczuwanie bossa"),
}
#: Their full names when the pet is chosen (the names of ``!mocneodrodzenie``).
NEED_LABELS: dict[str, str] = dict(zip(pets.SKILL_POOL, pets.SKILL_LABELS))
#: What a tier's missions bring, in words - no number (``LOOT_SUMMON`` is added to an epic offer whose hours
#: can bring a Zwój przywołania).
LOOT_WORDS: tuple[str, ...] = (
    "🧩 fragmenty, 📜 zwoje, czasem 🥚 jajko albo ⬆️ poziom",
    "🧩 📜 więcej zwojów, 🥚 jajka (też rzadkie), 💎 kamienie i 🪞 lustra, rzadko 🐴 miejsce w stajni albo "
    "👝 lepsza sakwa",
    "🧩 📜 najwięcej zwojów, 🥚 rzadkie i nadzwyczajne jajka, 💎 🪞, 🐴, 👝",
)
LOOT_SUMMON = ", a bardzo rzadko 📯 Zwój przywołania"

#: The colours: the board (and "done"), the choice and the progress by tier, the results.
COLOUR_BOARD = 0xC8963E
TIER_COLOURS: tuple[int, ...] = (0x3BA55C, 0x3B82F6, 0x9B59B6)
COLOUR_SUCCESS, COLOUR_GREAT, COLOUR_FAIL, COLOUR_RECALLED = 0xFCDB03, 0xFF8D15, 0x7F8C8D, 0x95A5A6
#: ``assets/missions/<name>.*`` of the views (all optional).
PICTURE_BOARD, PICTURE_ROAD = "tablica", "droga"
PICTURE_RESULTS: dict[str, str] = {SUCCESS: "sukces", GREAT: "wielki-sukces", FAIL: "porazka",
                                   "recalled": "zawolany"}
#: The thumbnail of a result: the most notable item it brought, in this order.
THUMBNAILS: tuple[tuple[str, str], ...] = (
    ("summon", "item-zwoj-przywolania"), ("big", "item-duza-sakwa"), ("pouch", "item-sakwa"),
    ("place", "item-miejsce"), ("great_egg", "item-nadzwyczajne-jajko"), ("rare_egg", "item-rzadkie-jajko"),
)

#: The progress bar of a pet that is away: this many cells.
BAR_CELLS = 12
BAR_FULL, BAR_EMPTY = "▰", "▱"
#: The line under the bar: (below this share of the way, the line); the last one from there on.
STAGES: tuple[tuple[float, str], ...] = (
    (0.25, "Wyrusza w drogę, ogon do góry..."),
    (0.5, "Jest już daleko od domu..."),
    (0.75, "Najtrudniejsze dopiero przed nim..."),
    (math.inf, "Wraca! Już widać go na horyzoncie."),
)
#: ... and once the mission has ended (the claim is a click away).
STAGE_DUE = "Właśnie wraca - zaraz zobaczysz, co przyniósł."

BOARD_TITLE = "🧭 Tablica zleceń"
MSG_BOARD = ("Zlecenia na dziś, <@{player_id}>. Wybierz jedno i wyślij na nie jednego towarzysza - tego przy "
             "Tobie albo ze stajni. Szansę powodzenia zobaczysz, zanim go wyślesz.")
MSG_OFFER = "{square} **{number}. {emoji} {name}** · {tier} · ⏳ {hours} h"
MSG_OFFER_NEEDS = "Liczy się: {needs}"
MSG_OFFER_LOOT = "Łup: {loot}"
MSG_OFFER_STAR = "★ Tu częściej: {star}"
MSG_OFFER_BEST = "🐾 Najlepiej pasuje: **{name}** ({where}) - **{chance}%**"
MSG_NEXT_BOARD = "Nowe zlecenia {when}."
MSG_SUMMONS = "📯 Zwoje przywołania: {count} - odczytasz je przyciskiem poniżej albo komendą **!przywolaj**."
BOARD_FOOTER = ("Jedno zlecenie dziennie. Towarzysz na zleceniu nie walczy, nie wyczuwa bossów i nie zmienia "
                "miejsca, dopóki nie wróci.")
WHERE_ACTIVE = "przy Tobie"
WHERE_STABLE = "stajnia, miejsce {place}"

PICK_TITLE = "{square} {emoji} {name} - kogo wysyłasz?"
MSG_PICK = "*{flavour}*\n{tier} · ⏳ {hours} h · wróci około {when_t} ({when_r})"
MSG_PICK_PET = "{pet} ({where})"
MSG_PICK_TALENT = "📈 Talent: {talent}/9"
MSG_PICK_NEED = "{mark} {emoji} {label}: {value}/{target}%"
MSG_PICK_DETECTION = "{mark} 🔮 Wyczuwanie bossa: {answer}"
#: The pet's "Zaradność w zleceniach" (only when it has some): added to the chance after its cut.
MSG_PICK_RESOURCEFUL = "🧭 Zaradność w zleceniach: +{value} %"
MSG_PICK_CHANCE = "🎯 **Szansa powodzenia: {chance}%**"
#: How well the pet meets a need: all of it, some of it, nothing.
MARK_FULL, MARK_PART, MARK_NONE = "✅", "🟨", "❌"
MSG_WARN_ACTIVE = ("⚠️ To towarzysz przy Tobie. Przez {hours} h nie pomoże Ci w walkach (boss, polowanie, wieża, "
                   "pojedynki), nie wyczuje bossa, nie weźmie udziału w treningu, nie dostanie poziomu z łupów i "
                   "nie zamienisz go na innego - nowe jajka trafią najwyżej do sakwy.")
MSG_WARN_STABLE = ("🐴 **{name}** wyrusza ze stajni - towarzysz przy Tobie zostaje i walczy dalej. Do powrotu nie "
                   "wyjmiesz go ani nie zamienisz.")
MSG_EGGS_STAY = "🥚 Jajka nie mogą wyruszyć."
PET_PLACEHOLDER = "Wybierz towarzysza"
MSG_OPTION_ACTIVE = "Szansa {chance}% · przy Tobie"
MSG_OPTION_STABLE = "Szansa {chance}% · stajnia, miejsce {place}"

PROGRESS_TITLE = "🧭 Zlecenie w toku"
MSG_SENT = "🧭 **{name}** wyruszył! Trzymamy kciuki <:PepoG:790963160528977980>"
MSG_PROGRESS = ("Twój towarzysz **{name}** jest na zleceniu {square} **{mission}** ({tier}), <@{player_id}>.\n"
                "{bar} **{percent}%**\n"
                "⏱️ Wraca {when_r} (o {when_t}) · 🎯 szansa powodzenia {chance}%\n"
                "*{stage}*")
MSG_AWAY_ACTIVE = "Dopóki nie wróci, walczysz bez niego, a jego miejsca przy Tobie nie zajmie nikt inny."
MSG_AWAY_STABLE = "Dopóki nie wróci, nie wyjmiesz go ze stajni."

RESULT_TITLES: dict[str, str] = {
    SUCCESS: "✅ {name} wrócił ze zlecenia!", GREAT: "🌟 Wielki sukces! {name} wrócił ze zlecenia!",
    FAIL: "❌ {name} wrócił z pustymi łapami", "recalled": "↩️ {name} wrócił na Twoje wezwanie",
}
RESULT_TEXTS: dict[str, str] = {
    SUCCESS: "<@{player_id}>, Twój towarzysz wykonał zlecenie {square} **{mission}** (szansa była {chance}%).",
    GREAT: ("<@{player_id}>, Twój towarzysz wykonał zlecenie {square} **{mission}** brawurowo - łup jest "
            "podwójny! (szansa była {chance}%)"),
    FAIL: ("<@{player_id}>, zlecenie {square} **{mission}** się nie powiodło <:Sadge:936907659142111273> "
           "(szansa była {chance}%)."),
    "recalled": "<@{player_id}>, **{name}** przerwał zlecenie {square} **{mission}** i wrócił bez łupu.",
}
#: The marks of the last mission on the "done" view.
RESULT_MARKS: dict[str, str] = {SUCCESS: "✅", GREAT: "🌟", FAIL: "❌", "recalled": "↩️"}
MSG_GOT = "Otrzymujesz:"
MSG_GOT_FAIL = "Na pocieszenie przyniósł:"
GOT_MARK = "👉 "
MSG_NEXT_LATER = "Nowe zlecenia {when}."
MSG_NEXT_NOW = "Na tablicy czekają już nowe zlecenia - kliknij **Zlecenia**."

MSG_DONE = "Dzisiejsze zlecenie już za Tobą, <@{player_id}>. Nowe zlecenia {when}."
MSG_DONE_LAST = "Ostatnio: {mark} **{name}** - {square} „{mission}”"

#: The lines of a result (one per entry of ``RESULT``); ``{comp}`` is what was paid instead.
EGG_WORDS: dict[str, str] = {"egg": "Jajko", "rare_egg": "Rzadkie jajko", "great_egg": "Nadzwyczajne jajko"}
EGG_PLACES: dict[str, str] = {"active": "jest teraz przy Tobie", "pouch": "trafiło do sakwy",
                              "stable": "trafiło do stajni (miejsce {place})"}
MSG_GOT_EGG = "🥚 {egg} - {where}"
MSG_GOT_EGG_COMP = "🥚 {egg} - nie było dla niego miejsca (przy Tobie, w sakwie ani w stajni), więc dostajesz {comp}"
MSG_GOT_LEVEL = "⬆️ **{name}** awansuje na poziom {new}!"
MSG_GOT_LEVEL_COMP = "⬆️ Poziom - {name} nie może już awansować, więc dostajesz {comp}"
MSG_GOT_STONE = "💎 Kamień olśnienia"
MSG_GOT_MIRROR = "🪞 Lustro wizualiów"
MSG_GOT_PLACE = "🐴 Nowe miejsce w stajni - masz ich teraz {places}!"
MSG_GOT_PLACE_COMP = "🐴 Miejsce w stajni - masz już wszystkie, więc dostajesz {comp}"
MSG_GOT_POUCH = "👝 **Sakwa na jajko!** Nowe jajko, dla którego nie ma miejsca przy Tobie, trafi do niej."
MSG_GOT_BIG_POUCH = "🎒 **Duża sakwa!** Twoja sakwa mieści teraz dwa jajka."
MSG_GOT_POUCH_COMP = "🎒 Sakwa - Twoja Duża sakwa jest już największa, więc dostajesz {comp}"
MSG_GOT_SUMMON = ("📯 **Zwój przywołania!** Odczytaj go przyciskiem **Zwój przywołania** albo komendą **!przywolaj**, "
                  "gdy kanał będzie wolny - przybędzie mityczny boss.")
#: (Results paid before 2026-11-26, when the scrolls had a cap - such stored results are still shown.)
MSG_GOT_SUMMON_COMP = "📯 Zwój przywołania - masz już {cap}, więc dostajesz {comp}"
#: ... the same three when the reward could not be given because its database columns are missing (``"off"``).
MSG_GOT_PLACE_OFF = "🐴 Miejsce w stajni - chwilowo niedostępne, więc dostajesz {comp}"
MSG_GOT_POUCH_OFF = "🎒 Sakwa - chwilowo niedostępna, więc dostajesz {comp}"
MSG_GOT_SUMMON_OFF = "📯 Zwój przywołania - chwilowo niedostępny, więc dostajesz {comp}"

MSG_UNAVAILABLE = ("Zlecenia są chwilowo niedostępne, <@{player_id}> <:Sadge:936907659142111273> Spróbuj ponownie za "
                   "jakiś czas.")
MSG_NO_PET = ("Nie masz towarzysza, który mógłby wyruszyć, <@{player_id}> - jajka zostają w domu. Wykluj jajko "
              "(zdobądź dla niego poziom) i wróć tutaj <:Hmm:984767035617730620>")
MSG_PANEL_OLD = "Tablica zmieniła się w międzyczasie, <@{player_id}> - oto jej aktualny stan."
MSG_SEND_CHANGED = ("Twoja stajnia zmieniła się od pokazania listy, <@{player_id}> - wybierz towarzysza jeszcze "
                    "raz.")
MSG_SEND_CHANCE = ("Szansa towarzysza **{name}** zmieniła się (teraz **{chance}%**), <@{player_id}> - sprawdź i "
                   "kliknij **Wyślij** jeszcze raz.")
MSG_SEND_EXPIRED = "Te zlecenia już wygasły, <@{player_id}> - oto nowa tablica."
MSG_SEND_GONE = "Dzisiejsze zlecenie jest już wybrane, <@{player_id}>."
MSG_NOT_CHOSEN = "Najpierw wybierz towarzysza z listy, <@{player_id}>."
MSG_SEND_BOSS = ("Trwa walka z bossem, <@{player_id}> - towarzysz wyruszy, gdy się skończy. Kliknij wtedy "
                 "**Wyślij** jeszcze raz.")
#: The list shows another pet than the panel holds (a pick that was refused - busy, a fight): it is shown first.
MSG_SEND_CHECK = ("Na liście wybrany jest inny towarzysz, <@{player_id}> - oto jego szansa. Kliknij **Wyślij** "
                  "jeszcze raz, jeśli to on ma wyruszyć.")
MSG_REROLL_PRICE = ("Cena nowych zleceń zmieniła się, <@{player_id}> - teraz to {price}. Kliknij jeszcze raz, "
                    "jeśli nadal chcesz.")
MSG_REROLL_POOR = "Nowe zlecenia kosztują {price}, a masz {have}, <@{player_id}> <:Sadge:936907659142111273>"
MSG_REROLL_USED = "Na dziś nowych zleceń już nie będzie, <@{player_id}> - kolejne {when}."
# (rev. 2: not "dzisiejsze" - a 24 h mission recalled the next day belongs to the period before.)
MSG_RECALL_QUESTION = ("Czy na pewno chcesz zawołać **{name}** z powrotem, <@{player_id}>? Wróci od razu, ale z "
                       "pustymi łapami - a to zlecenie przepadnie i nie zastąpi go żadne inne "
                       "<:Hmm:984767035617730620> " + pets.CONFIRM_HINT)
MSG_RECALL_LATE = "**{name}** właśnie wraca sam, <@{player_id}> - zobacz, co przyniósł."
#: The notifier's post (its embed is the result).
MSG_RETURN_PING = "🐾 <@{player_id}>, Twój towarzysz wrócił ze zlecenia!"
#: To the owner: the config cannot be read (at most once per :data:`CONFIG_REPORT_GAP`).
MSG_OWNER_CONFIG = ("Zlecenia nie działają: plik `config/missionsConfig.json` jest błędny (`{error}`). Popraw go - "
                    "bot wczyta go sam przy następnym kliknięciu, bez restartu.")
CONFIG_REPORT_GAP = 600.0

#: The labels of the panel's buttons.
LABEL_REROLL = "Nowe zlecenia ({price})"
LABEL_SUMMON = "Zwój przywołania ({count})"
LABEL_CLOSE = "Zamknij"
LABEL_SEND = "Wyślij"
LABEL_BACK = "Inne zlecenie"
LABEL_REFRESH = "Odśwież"
LABEL_RECALL = "Zawołaj z powrotem"
LABEL_BOARD = "Tablica zleceń"
LABEL_NEXT = "Dalej"
#: ``custom_id`` of every item of the panel (``ui.FLOW_PREFIX`` + this): a click after a restart is stale.
FLOW = ui.FLOW_PREFIX + "missions:"

#: What the panel shows (and what :func:`_look` found).
BOARD, PICK, PROGRESS, RESULT, DONE = "board", "pick", "progress", "result", "done"
NO_PET = "no_pet"


def _figure(value: Any) -> str:
    """A number of a text: ``8``, ``12``, ``7.5`` (never ``8.0``)."""
    try:
        exact = _exact(value)
    except (TypeError, ValueError, ArithmeticError):
        return str(value)
    if exact.denominator == 1:
        return str(exact.numerator)
    return f"{float(exact):g}"


def _shards(number: int) -> str:
    """``1 fragment zwoju`` / ``3 fragmenty zwoju`` / ``5 fragmentów zwoju`` (the rule of ``pets._scrolls``)."""
    if number == 1:
        return "1 fragment zwoju"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f"{number} fragmenty zwoju"
    return f"{number} fragmentów zwoju"


def _items_text(kind: str, number: int) -> str:
    """Scrolls (``5 zwojów odrodzenia``) or shards in words."""
    return _shards(number) if kind == "shards" else pets._scrolls(number) + " odrodzenia"


def _comp_text(comp: Any) -> str:
    """What a compensation paid (``{"scrolls": 10}``) in words."""
    try:
        kind, number = next(iter(dict(comp).items()))
        return _items_text(str(kind), pets._int(number))
    except (StopIteration, TypeError, ValueError, AttributeError):
        return "zwoje"


def _where(place: int) -> str:
    return WHERE_ACTIVE if int(place or 0) <= 0 else WHERE_STABLE.format(place=int(place))


def _tier_of(offer: Mapping[str, Any]) -> int:
    try:
        return min(max(int(offer.get("tier", 0)), 0), len(TIERS) - 1)
    except (TypeError, ValueError):
        return 0


def _looks(config: Config | None, offer: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(emoji, name, flavour)`` of an offer's mission (the key and no flavour when the config lost it)."""
    mission = find_mission(config, offer.get("key")) if config is not None else None
    if mission is None:
        return "🧭", str(offer.get("key") or "zlecenie"), ""
    return mission.emoji, mission.name, mission.flavour


def _needs_text(offer: Mapping[str, Any]) -> str:
    """``🍀 rzadszy drop 8% · 🔮 wyczuwanie bossa`` (the board)."""
    parts = []
    for stat, target in offer.get("needs") or ():
        emoji, short = NEED_LOOK.get(stat, ("❔", str(stat)))
        parts.append(f"{emoji} {short}" if stat == DETECTION else f"{emoji} {short} {_figure(target)}%")
    return " · ".join(parts)


def _summon_hours(config: Config | None, offer: Mapping[str, Any]) -> bool:
    """Whether the offer's hours can bring a Zwój przywołania (its tier's summon table has them)."""
    if config is None:
        return False
    try:
        return int(offer.get("hours", 0)) in config.tier(_tier_of(offer)).summon
    except (TypeError, ValueError, KeyError):
        return False


def _loot_text(config: Config | None, offer: Mapping[str, Any]) -> str:
    """The loot of the offer in words, with its ★ (no ★ of a summon the hours cannot bring)."""
    tier = _tier_of(offer)
    summon = _summon_hours(config, offer)
    loot = LOOT_WORDS[tier] + (LOOT_SUMMON if tier == len(TIERS) - 1 and summon else "")
    star = offer.get("star")
    if star in STAR_WORDS and (star != "summon" or summon):
        loot += " · " + MSG_OFFER_STAR.format(star=STAR_WORDS[star])
    return MSG_OFFER_LOOT.format(loot=loot)


def ranked(offer: Mapping[str, Any], candidates: Sequence[pets.MissionPet]) -> list[tuple[int, pets.MissionPet]]:
    """``(chance, pet)`` of every candidate on the offer, the best first.

    By chance; on a tie a stabled pet before the one at the player's side
    (sending it costs him nothing in the fights), then the lower place.
    """
    scored = [(chance_of(offer, candidate.row), candidate) for candidate in candidates]
    return sorted(scored, key=lambda item: (-item[0], item[1].place == 0, item[1].place))


def progress(sent_at: datetime, ends_at: datetime, now: datetime | None = None) -> tuple[str, int, float]:
    """``(bar, percent, share of the way)`` of a mission sent at ``sent_at`` that ends at ``ends_at``."""
    now = clock.utcnow() if now is None else now
    total = (ends_at - sent_at).total_seconds()
    share = 1.0 if total <= 0 else min(max((now - sent_at).total_seconds() / total, 0.0), 1.0)
    filled = math.floor(BAR_CELLS * share)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_CELLS - filled), math.floor(100 * share), share


def stage_of(share: float, *, due: bool = False) -> str:
    """The line under the progress bar."""
    if due:
        return STAGE_DUE
    return next(line for below, line in STAGES if share < below)


def result_line(entry: Mapping[str, Any]) -> str | None:
    """One line of a result (an entry of ``RESULT.lines``, without the 👉), or None for what it cannot tell."""
    kind = entry.get("kind")
    comp = entry.get("comp")
    if kind in COUNTED_KINDS:
        return _items_text(str(kind), pets._int(entry.get("n")))
    if kind == "egg":
        egg = EGG_WORDS.get(str(entry.get("class")), EGG_WORDS["egg"])
        if comp is not None:
            return MSG_GOT_EGG_COMP.format(egg=egg, comp=_comp_text(comp))
        where = EGG_PLACES.get(str(entry.get("status")), EGG_PLACES["stable"]).format(
            place=pets._int(entry.get("place")))
        line = MSG_GOT_EGG.format(egg=egg, where=where)
        _colour, words = pets._class_look({"mythic": bool(entry.get("mythic")), "shiny": bool(entry.get("shiny")),
                                           "ultra_shiny": bool(entry.get("ultra"))})
        return line + (f" - {words}" if words else "")
    if kind == "level":
        name = str(entry.get("name") or pets.DEFAULT_NAME)
        if comp is not None:
            return MSG_GOT_LEVEL_COMP.format(name=name, comp=_comp_text(comp))
        line = MSG_GOT_LEVEL.format(name=name, new=pets._int(entry.get("new")))
        if entry.get("aura"):  # its aura grew with the level (2026-11-26): the note under the line
            grew = auras.note(auras.Change(auras.GREW, str(entry["aura"]), pets._int(entry.get("aura_perc"))))
            line += f"\n{grew}" if grew else ""
        return line
    if kind == "stone":
        return MSG_GOT_STONE
    if kind == "mirror":
        return MSG_GOT_MIRROR
    if kind == "place":
        if comp is not None:
            return (MSG_GOT_PLACE_OFF if entry.get("off") else MSG_GOT_PLACE_COMP).format(comp=_comp_text(comp))
        return MSG_GOT_PLACE.format(places=pets._int(entry.get("places")))
    if kind == "pouch":
        if comp is not None:
            return (MSG_GOT_POUCH_OFF if entry.get("off") else MSG_GOT_POUCH_COMP).format(comp=_comp_text(comp))
        return MSG_GOT_BIG_POUCH if entry.get("to") == "big" else MSG_GOT_POUCH
    if kind == "summon":
        if comp is not None:
            if entry.get("off"):
                return MSG_GOT_SUMMON_OFF.format(comp=_comp_text(comp))
            return MSG_GOT_SUMMON_COMP.format(cap=pets._int(entry.get("cap")), comp=_comp_text(comp))
        return MSG_GOT_SUMMON
    return None


def _thumbnail(lines: Sequence[Mapping[str, Any]]) -> str | None:
    """The item picture of a result: the most notable thing it brought (never a compensation)."""
    found = set()
    for entry in lines:
        if "comp" in entry:
            continue
        kind = entry.get("kind")
        if kind == "pouch":
            found.add("big" if entry.get("to") == "big" else "pouch")
        elif kind == "egg":
            found.add(str(entry.get("class")))
        elif kind in ("summon", "place"):
            found.add(str(kind))
    return next((name for what, name in THUMBNAILS if what in found), None)


@dataclass
class Shown:
    """A view of the panel: its embed and the pictures it carries (none when they are not there)."""

    embed: discord.Embed
    files: tuple[Path, ...] = ()

    def send_kwargs(self) -> dict[str, Any]:
        return {"embed": self.embed, **({"file_paths": list(self.files)} if self.files else {})}


def _pictured(embed: discord.Embed, image: str | None, thumbnail: str | None = None) -> Shown:
    """``embed`` with its picture and its item thumbnail, when the files are there."""
    files: list[Path] = []
    image_path = picture(image) if image else None
    if image_path is not None:
        embed.set_image(url=f"attachment://{image_path.name}")
        files.append(image_path)
    thumb_path = picture(thumbnail) if thumbnail else None
    if thumb_path is not None and thumb_path not in files:
        embed.set_thumbnail(url=f"attachment://{thumb_path.name}")
        files.append(thumb_path)
    return Shown(embed, tuple(files))


def _summons_line(count: int) -> str:
    return f"\n{MSG_SUMMONS.format(count=count)}" if count > 0 else ""


def _offers_of(row: Any) -> list[dict[str, Any]]:
    offers = _load(_value(row, "offers"))
    return [offer for offer in offers if isinstance(offer, dict)] if isinstance(offers, list) else []


def _chosen_of(row: Any) -> dict[str, Any]:
    offers = _offers_of(row)
    try:
        return offers[int(_value(row, "chosen"))]
    except (TypeError, ValueError, IndexError):
        return offers[0] if offers else {}


def _snapshot_of(row: Any) -> dict[str, Any]:
    snap = _load(_value(row, "pet_snapshot"))
    return snap if isinstance(snap, dict) else {}


def _pet_name(row: Any) -> str:
    return str(_snapshot_of(row).get("name") or pets.DEFAULT_NAME)


def _result_kind(row: Any) -> str:
    """``success`` / ``great`` / ``fail`` of a paid mission, ``recalled`` of a recalled one."""
    if _value(row, "status") == "recalled":
        return "recalled"
    outcome = _load(_value(row, "outcome"))
    kind = outcome.get("result") if isinstance(outcome, dict) else None
    return kind if kind in (SUCCESS, GREAT, FAIL) else SUCCESS


def board_embed(player_id: int, row: Any, config: Config | None, candidates: Sequence[pets.MissionPet], *,
                summons: int = 0) -> Shown:
    """BOARD: the three offers of ``row`` with what they need and bring, and the pet that fits each best."""
    blocks = [MSG_BOARD.format(player_id=player_id)]
    for number, offer in enumerate(_offers_of(row), start=1):
        tier = _tier_of(offer)
        emoji, name, flavour = _looks(config, offer)
        lines = [MSG_OFFER.format(square=TIER_SQUARE[tier], number=number, emoji=emoji, name=name,
                                  tier=TIER_WORD[tier], hours=_figure(offer.get("hours")))]
        if flavour:
            lines.append(f"*{flavour}*")
        lines.append(MSG_OFFER_NEEDS.format(needs=_needs_text(offer)))
        lines.append(_loot_text(config, offer))
        best = ranked(offer, candidates)
        if best:
            chance, pet = best[0]
            lines.append(MSG_OFFER_BEST.format(name=pets._called(pet.row), where=_where(pet.place), chance=chance))
        blocks.append("\n".join(lines))
    blocks.append(MSG_NEXT_BOARD.format(when=ui.when(daily.next_daily_reset())) + _summons_line(summons))
    embed = discord.Embed(title=BOARD_TITLE, description="\n\n".join(blocks)[:4096], color=COLOUR_BOARD)
    embed.set_footer(text=BOARD_FOOTER)
    return _pictured(embed, PICTURE_BOARD)


def pick_embed(offer: Mapping[str, Any], config: Config | None, pet: pets.MissionPet, chance: int, *,
               eggs: int = 0, now: datetime | None = None) -> Shown:
    """PICK: the chosen offer and how the chosen pet meets each of its needs, with the chance."""
    tier = _tier_of(offer)
    emoji, name, flavour = _looks(config, offer)
    hours = pets._int(offer.get("hours"))
    back = (clock.utcnow() if now is None else now) + timedelta(hours=hours)
    lines = [MSG_PICK.format(flavour=flavour or name, tier=TIER_WORD[tier], hours=hours, when_t=ui.when(back, "t"),
                             when_r=ui.when(back)), "",
             MSG_PICK_PET.format(pet=pets._pet_line(pet.row), where=_where(pet.place)),
             MSG_PICK_TALENT.format(talent=min(pets._int(_value(pet.row, "pet_skills")), MAX_TALENT))]
    for stat, target in offer.get("needs") or ():
        part = _part(stat, target, pet.row)
        mark = MARK_FULL if part >= 1 else MARK_PART if part > 0 else MARK_NONE
        if stat == DETECTION:
            lines.append(MSG_PICK_DETECTION.format(mark=mark, answer="Tak" if part else "Nie"))
        else:
            look_emoji = NEED_LOOK.get(stat, ("❔", ""))[0]
            lines.append(MSG_PICK_NEED.format(mark=mark, emoji=look_emoji, label=NEED_LABELS.get(stat, stat),
                                              value=_figure(pets._num(_value(pet.row, stat.lower()))),
                                              target=_figure(target)))
    if (resourceful := resourcefulness_of(pet.row)) > 0:
        lines.append(MSG_PICK_RESOURCEFUL.format(value=resourceful))
    lines.append(MSG_PICK_CHANCE.format(chance=chance))
    lines.append("")
    lines.append(MSG_WARN_ACTIVE.format(hours=hours) if pet.place <= 0
                 else MSG_WARN_STABLE.format(name=pets._called(pet.row)))
    if eggs > 0:
        lines.append(MSG_EGGS_STAY)
    embed = discord.Embed(title=PICK_TITLE.format(square=TIER_SQUARE[tier], emoji=emoji, name=name)[:256],
                          description="\n".join(lines)[:4096], color=TIER_COLOURS[tier])
    return _pictured(embed, TIERS[tier])


def progress_embed(player_id: int, row: Any, config: Config | None, *, summons: int = 0, sent: bool = False,
                   due: bool = False, now: datetime | None = None) -> Shown:
    """PROGRESS: where the pet that is away is on its way (``sent``: it has just left)."""
    offer = _chosen_of(row)
    tier = _tier_of(offer)
    _emoji, mission, _flavour = _looks(config, offer)
    sent_at, ends_at = row["sent_at"], row["ends_at"]
    bar, percent, share = progress(sent_at, ends_at, now)
    name = _pet_name(row)
    text = MSG_PROGRESS.format(name=name, square=TIER_SQUARE[tier], mission=mission, tier=TIER_WORD[tier],
                               player_id=player_id, bar=bar, percent=percent, when_r=ui.when(ends_at),
                               when_t=ui.when(ends_at, "t"), chance=pets._int(row["chance"]),
                               stage=stage_of(share, due=due))
    text += "\n" + (MSG_AWAY_ACTIVE if pets._int(_value(row, "pet_place")) <= 0 else MSG_AWAY_STABLE)
    text += _summons_line(summons)
    if sent:
        text = MSG_SENT.format(name=name) + "\n\n" + text
    embed = discord.Embed(title=PROGRESS_TITLE, description=text[:4096], color=TIER_COLOURS[tier])
    return _pictured(embed, PICTURE_ROAD)


def result_embed(player_id: int, row: Any, config: Config | None = None, *, next_board: bool = False,
                 notes: Sequence[str] = ()) -> Shown:
    """RESULT: what the mission of ``row`` (paid or recalled) brought - rebuilt from its stored ``RESULT``.

    ``next_board``: the player has a board of the current period already (else
    a new one waits for him).  ``notes``: the album notes, when the stored
    ``RESULT`` lost them.
    """
    kind = _result_kind(row)
    offer = _chosen_of(row)
    tier = _tier_of(offer)
    _emoji, mission, _flavour = _looks(config, offer)
    name = _pet_name(row)
    outcome = _load(_value(row, "outcome"))
    outcome = outcome if isinstance(outcome, dict) else {}
    result = _load(_value(row, "result"))
    result = result if isinstance(result, dict) else {}
    entries = [entry for entry in result.get("lines") or () if isinstance(entry, dict)]
    text = RESULT_TEXTS[kind].format(player_id=player_id, square=TIER_SQUARE[tier], mission=mission,
                                     chance=pets._int(_value(row, "chance")), name=name)
    line = str(outcome.get("line") or "") if kind != "recalled" else ""
    if line:
        text += f"\n*{line}*"
    got = [GOT_MARK + text_line for text_line in map(result_line, entries) if text_line]
    if got and kind != "recalled":
        text += "\n\n" + (MSG_GOT_FAIL if kind == FAIL else MSG_GOT) + "\n" + "\n".join(got)
    shown_notes = [str(note) for note in (result.get("notes") or notes or ()) if note]
    if shown_notes:
        text += "\n\n" + "\n\n".join(shown_notes)
    text += "\n\n" + (MSG_NEXT_LATER.format(when=ui.when(daily.next_daily_reset())) if next_board else MSG_NEXT_NOW)
    colour = {SUCCESS: COLOUR_SUCCESS, GREAT: COLOUR_GREAT, FAIL: COLOUR_FAIL}.get(kind, COLOUR_RECALLED)
    embed = discord.Embed(title=RESULT_TITLES[kind].format(name=name)[:256], description=text[:4096], color=colour)
    return _pictured(embed, PICTURE_RESULTS[kind], _thumbnail(entries) if kind != "recalled" else None)


def done_embed(player_id: int, row: Any, config: Config | None = None, *, summons: int = 0) -> Shown:
    """DONE: the period's mission is behind the player (and what it was)."""
    text = MSG_DONE.format(player_id=player_id, when=ui.when(daily.next_daily_reset()))
    if _value(row, "status") in ("paid", "recalled"):
        offer = _chosen_of(row)
        _emoji, mission, _flavour = _looks(config, offer)
        text += "\n\n" + MSG_DONE_LAST.format(mark=RESULT_MARKS[_result_kind(row)], name=_pet_name(row),
                                              square=TIER_SQUARE[_tier_of(offer)], mission=mission)
    text += _summons_line(summons)
    embed = discord.Embed(title=BOARD_TITLE, description=text[:4096], color=COLOUR_BOARD)
    embed.set_footer(text=BOARD_FOOTER)
    return _pictured(embed, PICTURE_BOARD)


def _config_or_none() -> Config | None:
    """The config for a text (None when it is broken: the texts then name the mission by its key)."""
    try:
        return load_config()
    except ConfigError:
        return None


async def _config_broken(bot: "AlterBot", error: Exception) -> None:
    """The owner hears that the config is broken - at most once per :data:`CONFIG_REPORT_GAP`."""
    log.error("Missions: the config cannot be used: %s", error)
    now = time.monotonic()
    last = getattr(bot, "_missions_config_told", None)
    if last is not None and now - last < CONFIG_REPORT_GAP:
        return
    bot._missions_config_told = now  # type: ignore[attr-defined]
    report = getattr(bot, "log_to_owner", None)
    if report is not None:
        await report(MSG_OWNER_CONFIG.format(error=error))


def _mention_of(player_id: int) -> discord.AllowedMentions:
    return discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=int(player_id))])


def _quiet(bot: "AlterBot", *, after_restart: bool = False) -> bool:
    """``dungeon.channel_quiet`` (imported lazily: the mine builds on half of the game)."""
    from . import dungeon

    return dungeon.channel_quiet(bot, after_restart=after_restart)


# -- which view (§8.1) -------------------------------------------------------------------------

class Look(NamedTuple):
    """What :func:`look` found: the view to show (:data:`BOARD` ...; :data:`NO_PET`, :data:`OFF`) and its row."""

    state: str
    row: Any = None
    #: The album notes of a mission this look has just paid.
    notes: tuple[str, ...] = ()
    #: This look has just marked its :data:`RESULT` seen (the claim, or a result paid by the notifier): when
    #: the view never reaches the player (the post or the edit failed) it is unmarked (:func:`_unsee`).
    seen_now: bool = False


_SQL_AWAY_NOW = f"SELECT *, ENDS_AT <= now() AS DUE FROM {TABLE} WHERE PLAYER_ID = $1 AND STATUS = 'away'"
_SQL_UNSEEN = f"""SELECT * FROM {TABLE} WHERE PLAYER_ID = $1 AND STATUS = 'paid' AND SEEN_AT IS NULL
                  ORDER BY PAID_AT DESC LIMIT 1"""
_SQL_SEEN = f"UPDATE {TABLE} SET SEEN_AT = now() WHERE MISSION_ID = $1 AND SEEN_AT IS NULL RETURNING MISSION_ID"
_SQL_SEEN_OLDER = f"""UPDATE {TABLE} SET SEEN_AT = now()
                      WHERE PLAYER_ID = $1 AND STATUS = 'paid' AND SEEN_AT IS NULL AND PAID_AT <= $2"""
_SQL_SEEN_POST = f"UPDATE {TABLE} SET SEEN_AT = now() WHERE MISSION_ID = $1 AND PLAYER_ID = $2 AND SEEN_AT IS NULL"
_SQL_PERIOD_ROW = f"SELECT * FROM {TABLE} WHERE PLAYER_ID = $1 AND PERIOD_START = $2"
_SQL_ROW = f"SELECT * FROM {TABLE} WHERE MISSION_ID = $1 AND PLAYER_ID = $2"
#: A view looks again at most this many times when the rows change under it (two panels, the notifier).
_LOOKS = 3


async def look(scene: "Scene", *, seen: int = 0) -> Look:
    """Which view the player's missions are at now - paying a mission that has ended (§8.1).

    In this order: no table (made now when it can be, else :data:`OFF`); a pet
    away - :data:`PROGRESS`, or, when its mission has ended, the claim and
    :data:`RESULT` (``shown``: he sees it now); a result the notifier paid
    that he has not seen yet - :data:`RESULT` once (it is marked seen, and so
    are older ones; a view that then never reaches him unmarks it again,
    :func:`_unsee`); the row of the period - :data:`BOARD` while it is
    offered, else :data:`DONE`; no row - :data:`NO_PET` without a pet that
    could go (nothing is rolled), else the new board.  ``seen``: first mark
    that mission seen (the button under its post).  Raises :class:`ConfigError`
    and the database's errors.
    """
    bot = scene.bot
    player_id = pets._pid(scene.author)
    if not await ready(bot) and not await ensure_table(bot):
        return Look(OFF)
    if seen:
        await bot.db.execute(_SQL_SEEN_POST, int(seen), player_id, idempotent=True)
    for _attempt in range(_LOOKS):
        away = await bot.db.fetchrow(_SQL_AWAY_NOW, player_id)
        if away is not None:
            if not away["due"]:
                return Look(PROGRESS, away)
            claimed = await claim(scene, player_id, int(away["mission_id"]), announce=_quiet(bot), shown=True)
            if claimed is None:
                continue  # paid a moment ago (the notifier, another panel): look again
            row = await bot.db.fetchrow(_SQL_ROW, int(away["mission_id"]), player_id)
            return Look(RESULT, row if row is not None else claimed.row, tuple(claimed.notes), seen_now=True)
        unseen = await bot.db.fetchrow(_SQL_UNSEEN, player_id)
        if unseen is not None:
            if await bot.db.fetchval(_SQL_SEEN, int(unseen["mission_id"]), idempotent=False) is None:
                continue  # another panel showed it a moment ago
            await bot.db.execute(_SQL_SEEN_OLDER, player_id, unseen["paid_at"], idempotent=True)
            return Look(RESULT, unseen, seen_now=True)
        row = await bot.db.fetchrow(_SQL_PERIOD_ROW, player_id, current_period())
        if row is not None:
            return Look(BOARD if row["status"] == "offered" else DONE, row)
        candidates, _eggs = await pets.mission_pets(bot, player_id)
        if not candidates:
            return Look(NO_PET)
        row = await roll_board(bot, player_id)
        if row is not None:
            return Look(BOARD, row)
    log.warning("Missions: the rows of player %s kept changing; the panel shows the period as it is.", player_id)
    row = await bot.db.fetchrow(_SQL_PERIOD_ROW, player_id, current_period())
    if row is None:
        return Look(OFF)
    return Look(BOARD if row["status"] == "offered" else DONE, row)


async def _look_safely(scene: "Scene", *, seen: int = 0) -> Look:
    """:func:`look`, a broken config answered as :data:`OFF` (the owner is told)."""
    try:
        return await look(scene, seen=seen)
    except ConfigError as exc:
        await _config_broken(scene.bot, exc)
        return Look(OFF)


_SQL_UNSEE = f"UPDATE {TABLE} SET SEEN_AT = NULL WHERE MISSION_ID = $1 AND STATUS = 'paid'"


async def _unsee(bot: "AlterBot", now: Look) -> None:
    """The result ``now`` was marked seen but never reached the player: it is shown again at his next look
    (step 2b of :func:`look`) - what a mission brought is never hidden.  Never raises."""
    if not now.seen_now or now.row is None:
        return
    mission_id = pets._int(now.row["mission_id"])
    try:
        await bot.db.execute(_SQL_UNSEE, mission_id, idempotent=True)
        log.info("Missions: the result of mission %s did not reach its player; it is shown again.", mission_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - only the full view of it is lost
        log.warning("Missions: the result of mission %s could not be unmarked (%s: %s).", mission_id,
                    type(exc).__name__, exc)


async def _has_board(bot: "AlterBot", player_id: int) -> bool:
    """Whether the player has a row of the current period (a new board does not wait for him)."""
    return await bot.db.fetchval(f"SELECT 1 FROM {TABLE} WHERE PLAYER_ID = $1 AND PERIOD_START = $2",
                                 player_id, current_period()) is not None


async def show_board(scene: "Scene", *, seen: int = 0) -> bool:
    """The player's missions as one panel (the hub's "Zlecenia", ``!zlecenia``, the button under a post).

    :func:`look` decides the view (and pays a mission that has ended).  False
    when there is no panel: the missions are unavailable, or he has no pet
    that could go (he is told).  ``seen``: the mission whose post he clicked.
    """
    member = scene.author
    player_id = pets._pid(member)
    now = await _look_safely(scene, seen=seen)
    if now.state in (OFF, NO_PET):
        await scene.send((MSG_UNAVAILABLE if now.state == OFF else MSG_NO_PET).format(player_id=player_id),
                         allowed_mentions=_mention_of(player_id))
        return False
    panel = MissionPanel(member)
    try:
        shown, _notice = await panel.build(scene.bot, now)
        await panel.send(scene.channel, **shown.send_kwargs())
    except Exception:
        panel.stop()
        await _unsee(scene.bot, now)  # (a result that was never posted is shown at the next look)
        raise
    except BaseException:
        panel.stop()
        raise
    panel.image = shown.files
    return True


# -- the panel (§8.2) ---------------------------------------------------------------------------

class MissionPanel(ui.PanelView):
    """The buttons under the player's missions: the offers, the pet, send, reroll, recall, summon.

    Only its player can use them (:class:`ui.PanelView`).  Every click is
    acknowledged, looks at the database again (:func:`look`), acts and redraws
    the message; when the missions changed meanwhile (another panel, the
    notifier, a new period) the panel shows them as they are now and says so
    (:data:`MSG_PANEL_OLD`).  The buttons that only change the view have the
    gate of the display buttons (``hub.display_gate``: boss channel; nothing
    between the prompts of a fight; also during the training sign-up), the
    ones that change the game the gameplay gate (``ui.gate``).  The clicks of
    one panel run one after another; a click that waited for another one
    whose view replaced the one it was made on (a double click) does nothing
    (:attr:`drawn`).
    """

    def __init__(self, owner: Any, *, timeout: float | None = None) -> None:
        super().__init__(owner=owner, timeout=pets.PANEL_TIMEOUT if timeout is None else timeout)
        self.player = owner
        #: What the message shows: :data:`BOARD`, :data:`PICK`, :data:`PROGRESS`, :data:`RESULT`, :data:`DONE`.
        self.state = ""
        #: The mission row it shows, and the reroll count of its board (a reroll changes the offers).
        self.mission_id: int | None = None
        self.rerolls = 0
        #: The offer chosen (0..2) and the pet chosen for it (``pets.MissionPet.key``), with the chance shown.
        self.choice: int | None = None
        self.pet_key: str | None = None
        self.shown_chance = 0
        #: The price on the "Nowe zlecenia" button (None: no button).
        self.reroll_price: int | None = None
        #: The pet the player picked in the list since the last view - Discord shows it even when the click
        #: was refused (busy, a fight), so "Wyślij" must not send :attr:`pet_key` then (None: the list shows it).
        self.listed: str | None = None
        #: How many views were built: a click remembers it when it comes, and a view built while it waited
        #: means it was made on a view that is gone (:meth:`_run`).
        self.drawn = 0
        #: The pictures the message carries now.
        self.image: tuple[Path, ...] = ()
        self._lock = asyncio.Lock()

    # -- what the panel shows -------------------------------------------------------

    def _button(self, key: str, label: str, *, emoji: str | None = None, row: int = 0,
                style: discord.ButtonStyle = discord.ButtonStyle.secondary, disabled: bool = False,
                handler: Callable[[Any], Awaitable[None]]) -> discord.ui.Button:
        button: discord.ui.Button = discord.ui.Button(label=label[:80], emoji=emoji, style=style, row=row,
                                                      disabled=disabled, custom_id=FLOW + key)
        button.callback = handler  # type: ignore[method-assign]
        self.add_item(button)
        return button

    def _close_and_summon(self, summons: int, row: int) -> None:
        if summons > 0:
            self._button("summon", LABEL_SUMMON.format(count=summons), emoji="📯", row=row,
                         style=discord.ButtonStyle.danger, handler=self._summon)
        self._button("close", LABEL_CLOSE, emoji="✖️", row=row, handler=self._close)

    async def build(self, bot: "AlterBot", now: Look, *, sent: bool = False) -> tuple[Shown, str | None]:
        """Arrange the items for ``now`` and make its view; also a notice for the player (or None).

        The panel's memory follows: a new board forgets the choice; the choice
        of the pet is kept while that pet can still go (else the best one is
        chosen, and the notice says the stable changed).
        """
        config = _config_or_none()
        player_id = self.owner_id or pets._pid(self.player)
        row = now.row
        summons = await pets.summon_scrolls(bot, player_id)
        notice = None
        shown: Shown | None = None
        self.clear_items()
        self.drawn += 1
        self.reroll_price, self.listed = None, None
        if now.state == BOARD and self.state == PICK and self.mission_id == pets._int(row["mission_id"]) \
                and self.rerolls == pets._int(row["rerolls"]) and self.choice is not None:
            state = PICK
        else:
            state = now.state
        if state != PICK:
            self.choice, self.pet_key, self.shown_chance = None, None, 0
        if state == PICK:
            shown, notice = await self._pick(bot, row, config, player_id)
            if shown is None:  # nobody can go any more: back to the board
                state, notice = BOARD, self._text(MSG_NO_PET)
                self.choice, self.pet_key, self.shown_chance = None, None, 0
        if state == BOARD:
            candidates, _eggs = await pets.mission_pets(bot, player_id)
            shown = board_embed(player_id, row, config, candidates, summons=summons)
            for index, offer in enumerate(_offers_of(row)[:3]):
                emoji, name, _flavour = _looks(config, offer)
                self._button(f"offer:{index}", f"{index + 1}. {name}", emoji=emoji, row=0,
                             style=discord.ButtonStyle.primary, handler=functools.partial(self._offer, index))
            if config is not None and config.reroll_price > 0 and \
                    pets._int(row["rerolls"]) < config.rerolls_per_period and row["status"] == "offered":
                self._button("reroll", LABEL_REROLL.format(price=pets._scrolls(config.reroll_price)), emoji="🎲",
                             row=1, handler=self._reroll)
                self.reroll_price = config.reroll_price
            self._close_and_summon(summons, 1)
        elif state == PROGRESS:
            due = bool(_value(row, "due")) or row["ends_at"] <= clock.utcnow()
            shown = progress_embed(player_id, row, config, summons=summons, sent=sent, due=due)
            self._button("refresh", LABEL_REFRESH, emoji="🔄", handler=self._refresh)
            self._button("recall", LABEL_RECALL, emoji="↩️", style=discord.ButtonStyle.danger, handler=self._recall)
            self._close_and_summon(summons, 0)
        elif state == RESULT:
            fresh = not await _has_board(bot, player_id)
            shown = result_embed(player_id, row, config, next_board=not fresh, notes=now.notes)
            self._button("board", LABEL_BOARD if fresh else LABEL_NEXT, emoji="🧭",
                         style=discord.ButtonStyle.primary if fresh else discord.ButtonStyle.secondary,
                         handler=self._board)
            self._close_and_summon(summons, 0)
        elif state == DONE:
            shown = done_embed(player_id, row, config, summons=summons)
            self._close_and_summon(summons, 0)
        self.state = state
        self.mission_id = pets._int(row["mission_id"]) if row is not None else None
        self.rerolls = pets._int(_value(row, "rerolls"))
        return shown, notice

    async def _pick(self, bot: "AlterBot", row: Any, config: Config | None,
                    player_id: int) -> tuple[Shown | None, str | None]:
        """The PICK view of the offer :attr:`choice`: the list of the pets with their chances, the chosen one."""
        offers = _offers_of(row)
        if self.choice is None or not 0 <= self.choice < len(offers):
            return None, None
        offer = offers[self.choice]
        candidates, eggs = await pets.mission_pets(bot, player_id)
        scored = ranked(offer, candidates)
        if not scored:
            return None, None
        notice = None
        chosen = next(((chance, pet) for chance, pet in scored if pet.key == self.pet_key), None)
        if chosen is None:
            if self.pet_key is not None:
                notice = self._text(MSG_SEND_CHANGED)
            chosen = scored[0]
        chance, pet = chosen
        self.pet_key, self.shown_chance = pet.key, chance
        select: discord.ui.Select = discord.ui.Select(
            custom_id=FLOW + "pet", placeholder=PET_PLACEHOLDER, min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(
                label=pets._pet_brief(option.row)[:100], value=option.key,
                description=(MSG_OPTION_ACTIVE.format(chance=option_chance) if option.place <= 0 else
                             MSG_OPTION_STABLE.format(chance=option_chance, place=option.place))[:100],
                emoji="🐾" if option.place <= 0 else "🐴", default=option.key == pet.key)
                for option_chance, option in scored[:25]])
        select.callback = self._pet_chosen  # type: ignore[method-assign]
        self.add_item(select)
        self._button("send", LABEL_SEND, emoji="🧭", row=1, style=discord.ButtonStyle.success,
                     disabled=self.pet_key is None, handler=self._send)
        self._button("back", LABEL_BACK, emoji="⬅️", row=1, handler=self._back)
        self._button("close", LABEL_CLOSE, emoji="✖️", row=1, handler=self._close)
        return pick_embed(offer, config, pet, chance, eggs=eggs), notice

    async def _redraw(self, shown: Shown) -> bool:
        """Edit the message to ``shown``; the pictures are sent again only when they changed.  Never raises."""
        if self.is_finished():
            return False
        if shown.files == self.image:
            done = await pets._edit_panel(self.message, embed=shown.embed, view=self)
        else:
            try:
                attachments = [discord.File(str(path)) for path in shown.files]
            except OSError as exc:  # a picture vanished since it was found
                log.warning("Missions: a picture could not be read (%s).", exc)
                shown.embed.set_image(url=None)
                shown.embed.set_thumbnail(url=None)
                attachments, shown = [], Shown(shown.embed)
            done = await pets._edit_panel(self.message, embed=shown.embed, view=self, attachments=attachments)
        if done:
            self.image = shown.files
        else:  # the message is gone (purged): nothing to listen to
            await self.finish()
        return done

    async def _show(self, interaction: Any, now: Look, notice: str | None = None, *, sent: bool = False) -> None:
        """Show ``now`` (a notice first: what the player is told comes with the new view)."""
        if now.state in (OFF, NO_PET):
            await ui.reply(interaction, (MSG_UNAVAILABLE if now.state == OFF else MSG_NO_PET).format(
                player_id=self.owner_id))
            await self.finish()
            return
        try:
            shown, own_notice = await self.build(interaction.client, now, sent=sent)
            delivered = await self._redraw(shown)
        except Exception:
            await _unsee(interaction.client, now)
            raise
        if not delivered:  # (the message is gone: a result it was to show is shown at the next look)
            await _unsee(interaction.client, now)
        for text in dict.fromkeys(item for item in (notice, own_notice) if item):
            await ui.reply(interaction, text)

    def _text(self, template: str, **values: Any) -> str:
        """A notice for the panel's player."""
        return template.format(player_id=self.owner_id, **values)

    async def _current(self, interaction: Any) -> Look:
        """The missions as they are now (:func:`look`; a broken config is :data:`OFF`)."""
        return await _look_safely(ui.scene_of(interaction))

    def _same(self, now: Look, *states: str) -> bool:
        """Whether the message still shows what the database holds (a board: the same board, not rerolled)."""
        if self.state not in states or now.row is None or pets._int(now.row["mission_id"]) != self.mission_id:
            return False
        if now.state == BOARD:
            return pets._int(now.row["rerolls"]) == self.rerolls
        return True

    async def _run(self, interaction: Any, work: Callable[[], Awaitable[None]], *, drawn: int | None = None) -> None:
        """One click's work, after the others of this panel; a database outage is answered.

        ``drawn``: :attr:`drawn` when the click came - a view built while it
        waited (the first click of a double click) means the click was made on
        a view that is no longer there: nothing is done (it was acknowledged).
        """
        try:
            async with self._lock:
                if drawn is not None and drawn != self.drawn:
                    log.info("Mission panel of %s: a click on a view that was replaced meanwhile is dropped.",
                             self.owner_id)
                    return
                await work()
        except DatabaseUnavailable as exc:
            log.error("Mission panel of %s: database unavailable (%s).", self.owner_id, exc)
            from . import hub  # (lazily: the hub imports half of the game)

            await ui.reply(interaction, hub.MSG_DB_DOWN)

    @staticmethod
    async def _view_gate(interaction: Any) -> bool:
        """The gate of a button that only changes the view (spec rev. 2: no edit during a fight)."""
        from . import hub  # (lazily: the hub imports half of the game)

        if not await hub.display_gate(interaction, stable=True, boss_channel=True):
            return False
        return await ui.ack(interaction)

    @staticmethod
    async def _play_gate(interaction: Any) -> bool:
        """The gate of a button that changes the game: boss channel, alive, not busy, no fight."""
        if not await ui.gate(interaction):
            return False
        return await ui.ack(interaction)

    # -- clicks ---------------------------------------------------------------------

    async def _offer(self, index: int, interaction: Any) -> None:
        drawn = self.drawn
        if not await self._view_gate(interaction):
            return

        async def work() -> None:
            now = await self._current(interaction)
            if now.state != BOARD or not self._same(now, BOARD, PICK):
                await self._show(interaction, now, self._text(MSG_PANEL_OLD))
                return
            if not 0 <= index < len(_offers_of(now.row)):
                await self._show(interaction, now, self._text(MSG_PANEL_OLD))
                return
            self.state, self.choice, self.pet_key = PICK, index, None
            await self._show(interaction, now)

        await self._run(interaction, work, drawn=drawn)

    async def _pet_chosen(self, interaction: Any) -> None:
        drawn = self.drawn
        values = (getattr(interaction, "data", None) or {}).get("values") or []
        if not values:
            values = [item.values[0] for item in self.children
                      if isinstance(item, discord.ui.Select) and item.values][:1]
        if values:  # (Discord shows the pick until the message is edited - also when the gate refuses it)
            self.listed = str(values[0])
        if not await self._view_gate(interaction):
            return

        async def work() -> None:
            now = await self._current(interaction)
            if now.state != BOARD or not self._same(now, PICK):
                await self._show(interaction, now, self._text(MSG_PANEL_OLD))
                return
            if values:
                self.pet_key = str(values[0])  # (one that is no longer there: the best one, and the notice)
            await self._show(interaction, now)

        await self._run(interaction, work, drawn=drawn)

    async def _back(self, interaction: Any) -> None:
        drawn = self.drawn
        if not await self._view_gate(interaction):
            return

        async def work() -> None:
            now = await self._current(interaction)
            self.state = BOARD if now.state == BOARD else self.state
            await self._show(interaction, now)

        await self._run(interaction, work, drawn=drawn)

    async def _send(self, interaction: Any) -> None:
        drawn = self.drawn
        if not await self._play_gate(interaction):
            return

        async def work() -> None:
            player_id = self.owner_id or 0
            if self.state != PICK or self.pet_key is None or self.mission_id is None or self.choice is None:
                await ui.reply(interaction, MSG_NOT_CHOSEN.format(player_id=player_id))
                return
            if self.listed is not None and self.listed != self.pet_key:
                # The list shows a pet whose pick was refused: its chance was never shown - show it first.
                self.pet_key = self.listed
                await self._show(interaction, await self._current(interaction), self._text(MSG_SEND_CHECK))
                return
            try:
                sent = await send(interaction.client, interaction.user, self.mission_id, self.choice,
                                  self.pet_key, self.shown_chance, rerolls=self.rerolls)
            except ConfigError as exc:
                await _config_broken(interaction.client, exc)
                sent = Sent(OFF)
            if sent.status == OK:
                await self._show(interaction, Look(PROGRESS, sent.row), sent=True)
                return
            if sent.status == WAITING:
                await ui.reply(interaction, pets.MSG_ALREADY_WAITING)
                return
            if sent.status == BOSS:  # (the view stays: the same send works once the fight is over)
                await ui.reply(interaction, self._text(MSG_SEND_BOSS))
                return
            if sent.status == OFF:
                await self._show(interaction, Look(OFF))
                return
            if sent.status == CHANCE_CHANGED:
                notice = self._text(MSG_SEND_CHANCE, name=self._chosen_name(), chance=sent.chance)
            else:
                notice = self._text({CHANGED: MSG_SEND_CHANGED, EXPIRED: MSG_SEND_EXPIRED,
                                     STALE: MSG_PANEL_OLD}.get(sent.status, MSG_SEND_GONE))
            if sent.status == CHANGED:
                self.pet_key = None  # (the best one now; the notice is this one, not a second)
            await self._show(interaction, await self._current(interaction), notice)

        await self._run(interaction, work, drawn=drawn)

    def _chosen_name(self) -> str:
        """The name of the pet chosen in the list (as the list shows it)."""
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                for option in item.options:
                    if option.value == self.pet_key:
                        return discord.utils.escape_markdown(option.label.split(" · ")[0] or pets.DEFAULT_NAME)
        return pets.DEFAULT_NAME

    async def _reroll(self, interaction: Any) -> None:
        drawn = self.drawn
        if not await self._play_gate(interaction):
            return

        async def work() -> None:
            player_id = self.owner_id or 0
            if self.state not in (BOARD, PICK) or self.mission_id is None:
                await self._show(interaction, await self._current(interaction), self._text(MSG_PANEL_OLD))
                return
            try:
                config = load_config()
                status = await reroll(interaction.client, interaction.user, self.mission_id, rerolls=self.rerolls,
                                      price=self.reroll_price)
            except ConfigError as exc:
                await _config_broken(interaction.client, exc)
                await self._show(interaction, Look(OFF))
                return
            notice = None
            if status == USED:
                notice = self._text(MSG_REROLL_USED, when=ui.when(daily.next_daily_reset()))
            elif status == POOR:
                have = pets._int(await interaction.client.db.fetchval(
                    "SELECT MAX(REROLL_SCROLL) FROM PETOWNER WHERE PLAYER_ID = $1", player_id))
                notice = self._text(MSG_REROLL_POOR, price=pets._scrolls(config.reroll_price),
                                    have=pets._scrolls(have))
            elif status == PRICE:
                notice = self._text(MSG_REROLL_PRICE, price=pets._scrolls(config.reroll_price))
            elif status in (GONE, OFF, STALE):
                notice = self._text(MSG_PANEL_OLD)
            else:
                log.info("Missions: player %s took a new board (%s).", player_id, self.mission_id)
            self.state = BOARD
            await self._show(interaction, await self._current(interaction), notice)

        await self._run(interaction, work, drawn=drawn)

    async def _refresh(self, interaction: Any) -> None:
        drawn = self.drawn
        if not await self._view_gate(interaction):
            return

        async def work() -> None:
            await self._show(interaction, await self._current(interaction))

        await self._run(interaction, work, drawn=drawn)

    async def _board(self, interaction: Any) -> None:
        """"Tablica zleceń" / "Dalej" under a result: the missions as they are now (a new board, maybe)."""
        await self._refresh(interaction)

    async def _recall(self, interaction: Any) -> None:
        drawn = self.drawn
        if not await self._play_gate(interaction):
            return

        async def work() -> None:
            bot, member = interaction.client, interaction.user
            scene = ui.scene_of(interaction)
            player_id = self.owner_id or pets._pid(member)
            now = await self._current(interaction)
            if now.state == RESULT and self.state == PROGRESS and now.row is not None \
                    and pets._int(now.row["mission_id"]) == self.mission_id:
                await self._show(interaction, now, self._text(MSG_RECALL_LATE, name=_pet_name(now.row)))
                return
            if now.state != PROGRESS or not self._same(now, PROGRESS):
                await self._show(interaction, now, self._text(MSG_PANEL_OLD))
                return
            name = _pet_name(now.row)
            with pets._confirmation_slot(player_id) as free:
                if not free:
                    await ui.reply(interaction, pets.MSG_ALREADY_WAITING)
                    return
                answer = await pets._confirm(scene, member,
                                             MSG_RECALL_QUESTION.format(name=name, player_id=player_id),
                                             allowed_mentions=_mention_of(player_id))
                if not answer:
                    return
                done = await recall(bot, player_id, int(now.row["mission_id"]))
            if challenge.fight_on(scene.channel):  # a typed fight began during the question: no edit now
                log.info("Mission panel of %s: the recall was %s during a typed fight; not redrawn.", player_id,
                         "done" if done else "late")
                return
            if not done:
                await self._show(interaction, await self._current(interaction),
                                 self._text(MSG_RECALL_LATE, name=name))
                return
            row = await bot.db.fetchrow(_SQL_ROW, int(now.row["mission_id"]), player_id)
            await self._show(interaction, Look(RESULT, row))

        await self._run(interaction, work, drawn=drawn)

    async def _summon(self, interaction: Any) -> None:
        if not await self._play_gate(interaction):
            return
        scene = ui.scene_of(interaction)
        await use_summon_scroll(scene)  # (not under the panel's lock: a question, a boss)
        if challenge.fight_on(scene.channel):  # a typed fight began during the question: no edit now
            return

        async def work() -> None:
            if self.state == RESULT and self.mission_id is not None:  # the same result, one scroll less
                row = await interaction.client.db.fetchrow(_SQL_ROW, self.mission_id, self.owner_id or 0)
                if row is not None:
                    await self._show(interaction, Look(RESULT, row))
                    return
            await self._show(interaction, await self._current(interaction))

        await self._run(interaction, work)

    async def _close(self, interaction: Any) -> None:
        await ui.ack(interaction)
        await self.finish()


# -- the notifier "missions-watch" (§9) -------------------------------------------------------

WATCH_NAME = "missions-watch"
WATCH_STEP = 60.0
MAX_POSTS_PER_TICK = 3
#: A mission whose claim keeps failing: after this many failures the owner is told and it is tried hourly.
POISON_AFTER = 3
POISON_RETRY = 3600.0
MSG_OWNER_POISON = "Zlecenie {mission_id} gracza {player_id} nie daje się rozliczyć: `{error}`"

_SQL_DUE = f"""SELECT MISSION_ID, PLAYER_ID FROM {TABLE}
               WHERE STATUS = 'away' AND ENDS_AT <= now() AND NOT (MISSION_ID = ANY($1::bigint[]))
               ORDER BY ENDS_AT LIMIT $2"""
_SQL_NEWS = f"""SELECT * FROM {TABLE} WHERE STATUS = 'paid' AND ANNOUNCED_AT IS NULL
                ORDER BY PAID_AT LIMIT $1"""


async def watch(bot: "AlterBot") -> None:
    """Supervised loop: pay the missions that have ended and tell their players, only while the channel is quiet."""
    await bot.wait_until_ready()
    while True:
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except DatabaseUnavailable:
            pass
        except Exception:  # noqa: BLE001 - the next tick tries again
            log.exception("Missions: a tick failed.")
        await asyncio.sleep(WATCH_STEP)


def _failures(bot: "AlterBot") -> dict[int, list[float]]:
    """``{MISSION_ID: [failures, not before (monotonic)]}`` of the claims that failed (in memory)."""
    failures = getattr(bot, "_missions_failures", None)
    if failures is None:
        failures = bot._missions_failures = {}  # type: ignore[attr-defined]
    return failures


async def _claim_failed(bot: "AlterBot", mission_id: int, player_id: int, error: Exception) -> None:
    """A claim of the notifier failed: tried again at the next tick; after :data:`POISON_AFTER` failures
    the owner is told (once) and it is tried hourly.  A click of the player tries at once."""
    entry = _failures(bot).setdefault(mission_id, [0, 0.0])
    entry[0] += 1
    if entry[0] >= POISON_AFTER:
        entry[1] = time.monotonic() + POISON_RETRY
        if entry[0] == POISON_AFTER:
            await bot.log_to_owner(MSG_OWNER_POISON.format(mission_id=mission_id, player_id=player_id,
                                                           error=f"{type(error).__name__}: {error}"))


def _may_post(bot: "AlterBot") -> bool:
    """The channel is quiet (no busy holder, no fight, no boss engaged; tidied after a start) and no
    interactive event or its sign-up is open (a passive one - a merchant, a chest, the mine - is no obstacle)."""
    return _quiet(bot, after_restart=True) and not bot.boss_cycle.activity_in_the_way()


async def tick(bot: "AlterBot") -> int:
    """One look of the notifier; the number of posts.

    Nothing while the table is missing or the channel is not quiet.  Stale news
    is marked announced; the missions that have ended (at most
    :data:`MAX_POSTS_PER_TICK`, none in its back-off) are paid; the paid ones
    nobody was told about are posted - "towarzysz wrócił" with the result and
    a "Zlecenia" button for its player - at most :data:`MAX_POSTS_PER_TICK`.
    It never clears the channel, opens an event or holds the busy flag.
    """
    if not await ready(bot) or not _may_post(bot):
        return 0
    await mark_stale_news(bot)
    failures = _failures(bot)
    now = time.monotonic()
    waiting = [mission_id for mission_id, (_count, not_before) in failures.items() if not_before > now]
    for row in await bot.db.fetch(_SQL_DUE, waiting, MAX_POSTS_PER_TICK):
        if not _may_post(bot):
            return 0
        mission_id, player_id = int(row["mission_id"]), pets._pid(row["player_id"])
        try:
            member = await bot.member(player_id)
            await claim(bot.boss_scene().with_author(member), player_id, mission_id, announce=True, shown=False)
        except (asyncio.CancelledError, DatabaseUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - a poison row must not stop the others
            log.exception("Missions: mission %s of player %s could not be paid.", mission_id, player_id)
            await _claim_failed(bot, mission_id, player_id, exc)
        else:
            failures.pop(mission_id, None)
    posts = 0
    for row in await bot.db.fetch(_SQL_NEWS, MAX_POSTS_PER_TICK):
        if posts >= MAX_POSTS_PER_TICK or not _may_post(bot):
            break
        if await _announce(bot, int(row["mission_id"])):
            posts += 1
    return posts


async def _announce(bot: "AlterBot", mission_id: int) -> bool:
    """Post "towarzysz wrócił" of one paid mission (reserved first, released when the post fails)."""
    row = await reserve_announcement(bot, mission_id)
    if row is None:
        return False  # told already (another tick, the player's own panel)
    player_id = pets._pid(row["player_id"])
    try:
        shown = result_embed(player_id, row, _config_or_none(), next_board=await _has_board(bot, player_id))
        await io.send(bot.boss_channel, MSG_RETURN_PING.format(player_id=player_id),
                      view=shortcuts.missions_view(player_id, mission_id), allowed_mentions=_mention_of(player_id),
                      **shown.send_kwargs())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - told at a later tick
        log.warning("Missions: the return of mission %s could not be posted (%s: %s).", mission_id,
                    type(exc).__name__, exc)
        try:
            await release_announcement(bot, mission_id)
        except Exception:  # noqa: BLE001 - the player still sees the result in his panel (SEEN_AT)
            log.exception("Missions: the news of mission %s could not be released.", mission_id)
        return False
    log.info("Missions: the return of mission %s of player %s is posted.", mission_id, player_id)
    return True


# -- the owner's commands -----------------------------------------------------------------------

async def end_now(bot: "AlterBot", player_id: Any) -> int | None:
    """``!ZakonczZlecenie``: the player's pet that is away comes back now (its mission is due); its id, or None."""
    if not await ready(bot):
        return None
    return await bot.db.fetchval(
        f"""UPDATE {TABLE} SET ENDS_AT = now()
            WHERE PLAYER_ID = $1 AND STATUS = 'away' AND ENDS_AT > now() RETURNING MISSION_ID""",
        pets._pid(player_id), idempotent=False)
