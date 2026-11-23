"""Special boss variants: the boss portrait in an ornate frame with a moving sheen.

Owner's request (2026-11-22): sometimes a boss appears as a special variant -
its picture is the ready, framed GIF of the owner's package
(``assets/boss/special/<n>.gif``, the same numbers as ``assets/boss/<n>.gif``).
A pet egg dropped by such a boss has a better chance of card ornaments, and the
rarer the boss, the better: the cosmetic roll of the egg may be repeated when
it gave only the base look (``pets._roll_egg(..., rerolls=...)``).  The rarity
of a boss and its special look are separate traits: any boss can be special.

Everything the owner tunes is in ``config/bossVariants.json``, read from the
disk at every use like ``lootConfig.json`` (an edit works without a restart):

``chance_percent``
    chance that a monster boss that is being shown is special (0 switches the
    special bosses off);
``cosmetic_rerolls``
    boss rarity (``"0"`` common .. ``"4"`` mythic) -> how many times the
    cosmetic roll of an egg from a special boss of that rarity may be repeated
    (0 .. :data:`MAX_REROLLS`).  0, or no entry, for a rarity means "the look
    without the bonus": such a boss still appears in its frame, but its embed
    does not promise a better egg (:func:`description`);
``variants``
    variant id -> ``name`` and ``line`` (the sentence added to the boss embed);
``bosses``
    picture number -> variant id.

This is decoration: nothing here may ever stop a boss from appearing or loot
from being granted.  A missing or broken file, a bad value, an unknown variant
id or a missing picture only means "this boss is an ordinary one" (with a
warning in the log); no function of this module raises.

Only a monster boss can be special - a player posing as the boss has no framed
avatar.  The variant of the boss on the channel lives in memory
(``state.BossInfo.variant``): a boss shown again after a restart is rolled anew
together with its picture.

What the players are told and what is paid are both read from
``cosmetic_rerolls``: the embed of a special boss carries :data:`HINT` (the
promise of a better egg) only when the rarity of that boss has a bonus above 0
at the moment the boss is shown, and the bonus itself is read when the fight is
won - whatever happened to the other keys of the file meanwhile (the chance
set to 0, a picture mapped anew), a boss that was shown as special keeps it.
The one gap left on purpose (config edits are live): lowering the bonus of a
rarity to 0, or breaking the file, while a special boss with the hint already
stands on the channel - that boss then pays an ordinary egg.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from ..settings import ASSETS_DIR, CONFIG_DIR

log = logging.getLogger(__name__)

CONFIG_FILE = "bossVariants.json"
#: The most repeats of an egg's cosmetic roll the config may ask for.
MAX_REROLLS = 10
#: A variant's sentence is cut to this length (the boss embed has room for it many times over).
MAX_LINE_LENGTH = 300

#: Under the variant's own sentence in the boss embed - only when the egg of this boss really
#: has the bonus (see :func:`description`).  Owner's rules: the ornaments are for the players
#: to discover (no names, no chances) and they are purely cosmetic, supporters only.
HINT = ("*To wyjątkowe spotkanie: jajko zdobyte na tym bossie ma większą szansę na ozdoby karty "
        "towarzysza - im rzadszy boss, tym większą. Ozdoby to wyłącznie elementy kosmetyczne, "
        "tylko dla wspierających.*")
#: The sentence of a variant whose ``line`` is missing in the config.
DEFAULT_LINE = "✨ Ten boss pojawił się w wyjątkowej odsłonie: **{name}**."


class Variant(NamedTuple):
    """One special look: its id (as in ``bosses``), its name and its sentence for the boss embed."""

    id: str
    name: str
    line: str


@dataclass(frozen=True)
class Config:
    """``config/bossVariants.json`` with everything unusable left out.  The default: no special bosses."""

    chance_percent: float = 0.0
    #: Boss rarity -> allowed repeats of the cosmetic roll (0 .. :data:`MAX_REROLLS`).
    rerolls: Mapping[int, int] = field(default_factory=dict)
    variants: Mapping[str, Variant] = field(default_factory=dict)
    #: Picture number -> id of a variant of :attr:`variants`.
    bosses: Mapping[int, str] = field(default_factory=dict)


# -- reading the config ---------------------------------------------------------------------

def _number(value: Any) -> float | None:
    """A finite number of the JSON file (``true`` / ``false`` are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _whole(value: Any) -> int | None:
    """A whole number: a JSON number, or the digits of a JSON key (``"10007"``)."""
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isascii() and text.isdigit() else None
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _section(raw: Mapping[str, Any], key: str) -> Mapping[Any, Any]:
    """An object of the file; anything else (also a missing key) reads as empty."""
    value = raw.get(key)
    if isinstance(value, dict):
        return value
    if value is not None:
        log.warning("%s: %r must be an object, not %s; ignored.", CONFIG_FILE, key, type(value).__name__)
    return {}


def parse(raw: Any) -> Config:
    """The usable part of a decoded ``bossVariants.json``; every bad entry is left out with a warning."""
    if not isinstance(raw, dict):
        log.warning("%s: the file must hold an object, not %s; no special bosses.",
                    CONFIG_FILE, type(raw).__name__)
        return Config()

    chance = _number(raw.get("chance_percent"))
    if chance is None:
        log.warning("%s: chance_percent %r is not a number; no special bosses.",
                    CONFIG_FILE, raw.get("chance_percent"))
        chance = 0.0
    elif not 0 <= chance <= 100:
        log.warning("%s: chance_percent %r is outside 0-100; using the nearest of them.", CONFIG_FILE, chance)
        chance = min(max(chance, 0.0), 100.0)

    rerolls: dict[int, int] = {}
    for key, value in _section(raw, "cosmetic_rerolls").items():
        rarity, count = _whole(key), _whole(value)
        if rarity is None or count is None:
            log.warning("%s: cosmetic_rerolls %r: %r is not a whole number; no bonus for it.",
                        CONFIG_FILE, key, value)
            continue
        if not 0 <= count <= MAX_REROLLS:
            log.warning("%s: cosmetic_rerolls %r: %r is outside 0-%d; using the nearest of them.",
                        CONFIG_FILE, key, count, MAX_REROLLS)
            count = min(max(count, 0), MAX_REROLLS)
        rerolls[rarity] = count

    variants: dict[str, Variant] = {}
    for key, value in _section(raw, "variants").items():
        ident = _text(key)
        name = _text(value.get("name")) if isinstance(value, dict) else ""
        if not ident or not name:
            log.warning("%s: variant %r has no name; ignored.", CONFIG_FILE, key)
            continue
        line = _text(value.get("line")) or DEFAULT_LINE.format(name=name)
        variants[ident] = Variant(ident, name, line[:MAX_LINE_LENGTH])

    bosses: dict[int, str] = {}
    for key, value in _section(raw, "bosses").items():
        number, ident = _whole(key), _text(value)
        if number is None or ident not in variants:
            log.warning("%s: boss %r -> %r is not a picture number with a known variant; "
                        "that boss stays ordinary.", CONFIG_FILE, key, value)
            continue
        bosses[number] = ident
    return Config(chance, rerolls, variants, bosses)


def load() -> Config:
    """The config as it is on the disk right now; an empty one when it cannot be used.  Never raises."""
    path = CONFIG_DIR / CONFIG_FILE
    try:
        # (utf-8-sig: a file saved by an editor that adds a BOM is read all the same.)
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        log.warning("%s is missing; no special bosses.", path)
        return Config()
    except (OSError, ValueError, RecursionError) as exc:  # unreadable, not UTF-8, not JSON, absurdly nested
        log.warning("%s cannot be read (%s: %s); no special bosses.", path, type(exc).__name__, exc)
        return Config()
    try:
        return parse(raw)
    except Exception:  # noqa: BLE001 - decoration: whatever the file holds, the game goes on
        log.exception("%s could not be understood; no special bosses.", path)
        return Config()


# -- the variant of a boss --------------------------------------------------------------------

def special_picture(picture_number: int) -> Path:
    """Where the framed portrait of boss picture ``picture_number`` is kept."""
    return ASSETS_DIR / "boss" / "special" / f"{int(picture_number)}.gif"


def _variant_in(config: Config, picture_number: Any) -> Variant | None:
    """The variant ``config`` gives to a boss picture - None also when its framed portrait is missing."""
    number = _whole(picture_number)
    if number is None:
        return None
    variant = config.variants.get(config.bosses.get(number, ""))
    if variant is None:
        return None
    picture = special_picture(number)
    if not picture.is_file():
        log.warning("Special boss picture %s is missing; boss %s stays ordinary.", picture, number)
        return None
    return variant


def variant_for(picture_number: Any) -> Variant | None:
    """The special variant boss picture ``picture_number`` can appear as, or None.  Never raises."""
    try:
        return _variant_in(load(), picture_number)
    except Exception:  # noqa: BLE001
        log.exception("Could not look up the special variant of boss picture %r.", picture_number)
        return None


def roll(picture_number: Any, *, special: bool | None = None) -> Variant | None:
    """Decide whether the boss with picture ``picture_number`` appears as its special variant.

    ``special=None`` rolls with the config's ``chance_percent``; ``True`` forces
    the variant whenever the boss has one (the admin preview, whatever the
    chance); ``False`` forbids it.  At most ONE ``random.random()`` is drawn,
    and none at all when the answer is known without it (chance 0, no variant
    for this picture, forced or forbidden).  Never raises.
    """
    if special is False:
        return None
    try:
        config = load()
        if special is None and config.chance_percent <= 0:
            return None
        variant = _variant_in(config, picture_number)
        if variant is None or special:
            return variant
        return variant if random.random() * 100 < config.chance_percent else None
    except Exception:  # noqa: BLE001 - decoration: an ordinary boss comes instead
        log.exception("Could not decide the special variant of boss picture %r.", picture_number)
        return None


def rerolls_for(variant_id: Any, rarity: Any) -> int:
    """How many times the cosmetic roll of an egg from this boss may be repeated.

    0 for an ordinary boss (``variant_id`` empty), for a rarity the config does
    not name and whenever the config cannot be used; never more than
    :data:`MAX_REROLLS`.  Never raises.
    """
    if not variant_id:
        return 0
    try:
        return min(max(int(load().rerolls.get(int(rarity), 0)), 0), MAX_REROLLS)
    except Exception:  # noqa: BLE001 - no bonus, but the loot is granted
        log.exception("Could not read the cosmetic bonus of a special boss (variant %r, rarity %r).",
                      variant_id, rarity)
        return 0


def description(variant: Variant, *, bonus: bool = True) -> str:
    """What a special boss adds to the description of its embed (starts on a new paragraph).

    The variant's own sentence and, under it, :data:`HINT` - the promise of a
    better egg.  ``bonus=False`` (the config gives the rarity of this boss no
    repeat, ``rerolls_for(...) == 0``) leaves the hint out: the boss keeps its
    look, but nothing is promised that the loot would not pay.
    """
    return "\n\n" + variant.line + ("\n" + HINT if bonus else "")


def variant_of(shown: Any) -> str:
    """The variant id in what ``boss.send_boss_image`` returned ('' for an ordinary boss).

    Accepts the ``BossShown`` tuple as well as a bare ``(is_player_boss,
    player_boss)`` pair (older callers, stand-ins in tests).
    """
    try:
        value = shown[2]
    except (IndexError, KeyError, TypeError):
        return ""
    return value if isinstance(value, str) else ""
