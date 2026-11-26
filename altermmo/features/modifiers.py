"""Shrine of changes and the modifiers of the next boss.

Port of the legacy ``functions/functions_modifiers.py``.

The modifiers live in ``data/bossModifier.json`` (``bot.store.modifiers``) in
the legacy format - one flat JSON object with the keys of
:data:`INIT_MODIFIERS` - so the file of the production bot can be copied over
as it is (a key the old file does not have yet simply counts as 0).  They are filled by prayers at the shrine (``!modlitwa``), by pet
sacrifices and by skill gems, used by the next boss fight and by hunts, and
zeroed again when the boss fight is over (``boss.reset_end``).  Only supporters
(patrons, crafters, Twitch subscribers, YouTube supporters - see
:mod:`altermmo.features.patrons`) may pray; the events cog checks that, this
module only says so on the shrine.

Differences from the legacy code:

* a missing, empty, corrupt or incomplete file no longer crashes the caller
  (it used to raise in the middle of the boss spawn / a fight): every reader
  gets all the keys, unusable values count as 0;
* the file is written atomically (``bot.store``), never rewritten in place;
* ``load_modifiers`` / ``init_modifiers`` / ``load_desc_modifiers`` /
  ``modify_modifiers`` take the bot instead of a command context: they never
  talked to Discord, and background flows have no context.

New in v2 (owner's request): the "changed commands" modifier
(:data:`COMMANDS_CHANGED`).  While it is on, the fight with the next boss and
the hunts ask for the words of :data:`CHANGED_ACTIONS` instead of the usual
ones (:func:`fight_actions`).  It does not apply to the mythic boss (which
has its own words), to the Death Tower or to duels.

Owner, 2026-11-26: the Karta Dovy (``features/dova.py``) throws the set away
and draws it anew with the prayer's pool and values - so a prayer is one draw
(:func:`draw_modifier`, the values of :data:`VALUE_RANGES`) plus its
announcement (:func:`announce_modifier`), and the card uses the same draw.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any, Iterable

import discord

from ..settings import ASSETS_DIR
from . import patrons

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

#: New in v2: the fights ask for other words (see :data:`CHANGED_ACTIONS`).  One-off, like the ban.
COMMANDS_CHANGED = "commands_changed"

#: The content of a fresh ``bossModifier.json`` (legacy keys in the legacy order, then the new ones).
INIT_MODIFIERS: dict[str, int] = {
    "hp_reduced_perc": 0,
    "hp_boost_perc": 0,
    "drop_boost_perc": 0,
    "time_reduced_perc": 0,
    "time_increased_perc": 0,
    "rarity_boost": 0,
    "ban_loser": 0,
    "points_boost": 0,
    "player_id": 0,
    COMMANDS_CHANGED: 0,
}

#: (word to type, prompt) while the "changed commands" modifier is on; ``{foe}`` is "Boss" /
#: "Potwór".  Short on purpose (3-5 letters, no Polish letters): the difficulty is that
#: nobody has them in their fingers, not their length.  Approved by the owner word by word.
CHANGED_ACTIONS: tuple[tuple[str, str], ...] = (
    ("sus", "Ziemia pęka pod Twoimi nogami, wpisz **S U S**"),
    ("mur", "{foe} ciska w Ciebie głazem, wpisz **M U R**"),
    ("hak", "{foe} opuścił gardę! Wpisz **H A K**"),
    ("kop", "{foe} stracił równowagę! Wpisz **K O P**"),
    ("ryk", "{foe} próbuje Cię zastraszyć, odpowiedz tym samym, wpisz **R Y K**"),
    ("tnij", "{foe} się odsłonił! Teraz! Wpisz **T N I J**"),
    ("rzut", "Masz pod ręką kamień, wpisz **R Z U T**"),
    ("urok", "{foe} rzuca klątwę, odpowiedz własną, wpisz **U R O K**"),
    ("garda", "{foe} bierze potężny zamach, wpisz **G A R D A**"),
    ("wypad", "Dostrzegasz lukę w obronie, wpisz **W Y P A D**"),
    ("zwrot", "{foe} zachodzi Cię od tyłu, wpisz **Z W R O T**"),
    ("finta", "Zmyl przeciwnika, wpisz **F I N T A**"),
    ("odwet", "{foe} Cię trafił, oddaj mu! Wpisz **O D W E T**"),
)


def fight_actions(mods: dict[str, int], usual: tuple[tuple[str, str], ...],
                  foe: str) -> tuple[tuple[str, str], ...]:
    """The (word, prompt) pairs a fight draws from.

    ``usual`` - or, while the "changed commands" modifier is on, the whole of
    :data:`CHANGED_ACTIONS` with ``foe`` ("Boss" / "Potwór") in the prompts.
    ``mods``: what :func:`load_modifiers` returned at the start of the fight
    (one fight never mixes the two sets).
    """
    if mods.get(COMMANDS_CHANGED, 0) > 0:
        return tuple((word, prompt.format(foe=foe)) for word, prompt in CHANGED_ACTIONS)
    return usual

#: Pseudo modifier: resets the daily hunting cooldown at once, is never stored.
RESET_CD = "reset_cd"

#: What a prayer / sacrifice can draw (each outcome is equally likely).
RANDOM_POOL: tuple[str, ...] = (*INIT_MODIFIERS, RESET_CD)

#: The value a drawn outcome gets: ``random.randint(low, high)`` for these; 1 for every other outcome
#: (no die is rolled for it), the praying player's id for ``player_id``.  One table for every draw -
#: a prayer, a sacrifice, a skill gem's silent prayers (:func:`random_modifiers`) and the Karta Dovy
#: (``features/dova.py``) give the same values.
VALUE_RANGES: dict[str, tuple[int, int]] = {
    "hp_reduced_perc": (5, 15),
    "hp_boost_perc": (5, 15),
    "drop_boost_perc": (5, 25),
    "time_reduced_perc": (5, 25),
    "time_increased_perc": (5, 25),
    "points_boost": (1, 5),
}
#: The line of a drawn ``reset_cd`` in the style of the boss embed's "Dodatkowe statystyki"
#: (:func:`describe_modifiers`; it is never stored, so the file cannot show it).
RESET_CD_LINE = "\n🔺 Wszyscy bohaterowie są wypoczęci i mogą wyruszyć na polowanie!"

SHRINE_IMAGES = 4  # assets/events/shrines/0.png .. 3.png
SHRINE_COLOR = 0x609AF7
SHRINE_THUMBNAIL = "https://www.altermmo.pl/wp-content/uploads/Prayge.png"


# -- the file ---------------------------------------------------------------------

def _as_int(key: str, value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        log.warning("Modifier %s has an unusable value %r; using 0.", key, value)
        return 0


def _read(bot: "AlterBot") -> dict[str, int]:
    """The modifiers file as a complete ``{legacy key: int}`` dict (never raises)."""
    try:
        raw = bot.store.modifiers.load()
    except Exception:  # noqa: BLE001 - a broken file must never stop a boss or a fight
        log.exception("Cannot read the modifiers file; using the initial values.")
        raw = None
    if not isinstance(raw, dict):
        if raw is not None:
            log.warning("Modifiers file holds %s instead of an object; using the initial values.",
                        type(raw).__name__)
        raw = {}
    return {key: _as_int(key, raw.get(key, default)) for key, default in INIT_MODIFIERS.items()}


def current_modifiers(bot: "AlterBot") -> dict[str, int]:
    """The modifiers of the next boss, read synchronously (every key of :data:`INIT_MODIFIERS`; never raises).

    For callers that must not wait between reading and writing the file
    (the Karta Dovy): :func:`load_modifiers` is the same read behind an ``await``.
    """
    return _read(bot)


def replace_modifiers(bot: "AlterBot", modifiers: dict[str, int]) -> None:
    """Write ``modifiers`` (the keys of :data:`INIT_MODIFIERS`) as the whole file, synchronously.

    The Karta Dovy throws the old set away and puts its own draws in its place
    in one write (``features/dova.py``).
    """
    bot.store.modifiers.save({key: int(modifiers.get(key, default)) for key, default in INIT_MODIFIERS.items()})
    log.info("Modifiers replaced: %s", modifiers)


async def init_modifiers(bot: "AlterBot") -> None:
    """Set every modifier back to 0 (after a boss fight, on ``!ResetBoss``)."""
    bot.store.modifiers.save(dict(INIT_MODIFIERS))
    log.info("Modifiers reset.")


async def load_modifiers(bot: "AlterBot") -> dict[str, int]:
    """The modifiers of the next boss; every key of :data:`INIT_MODIFIERS` is present.

    Should be called at the beginning of a fight.
    """
    modifiers = _read(bot)
    log.debug("Modifiers loaded: %s", modifiers)
    return modifiers


async def load_desc_modifiers(bot: "AlterBot") -> str:
    """The active modifiers as the "Dodatkowe statystyki" part of a boss/mob description."""
    lines = describe_modifiers(_read(bot))
    if not lines:
        return ""
    return "\n\nDodatkowe statystyki:" + "".join(lines)


def describe_modifiers(modifiers: dict[str, int]) -> list[str]:
    """One line (starting with a new line) per active modifier, as the boss embed's "Dodatkowe statystyki"
    shows them; also the Karta Dovy's new set (``features/dova.py``)."""
    lines: list[str] = []
    for key, value in modifiers.items():
        if value <= 0:
            continue
        if key == "hp_reduced_perc":
            lines.append(f"\n🔺 Życie jest zmniejszone o {value} %")
        elif key == "hp_boost_perc":
            lines.append(f"\n🔻 Życie jest zwiększone o {value} %")
        elif key == "drop_boost_perc":
            lines.append(f"\n🔺 Drop zwiększony o {value} %")
        elif key == "time_reduced_perc":
            lines.append(f"\n🔻 Czas na reakcję zmniejszony o {value} %")
        elif key == "time_increased_perc":
            lines.append(f"\n🔺 Czas na reakcję zwiększony o {value} %")
        elif key == "rarity_boost":
            lines.append(f"\n🔺 Rzadkość zwiększona o {value} poziom (nie dotyczy potworów)")
        elif key == "points_boost":
            lines.append(f"\n🔺 Punkty za wygraną zwiększone o {value} (nie dotyczy potworów)")
        elif key == "player_id":
            lines.append("\n🔺 Bossem będzie gracz <@" + str(value) + ">")
        elif key == "ban_loser":
            lines.append("\n🔻 Przegrany zostanie zbanowany")
        elif key == COMMANDS_CHANGED:
            lines.append("\n🔀 Komendy w walce są zmienione (nie dotyczy mitycznego bossa)")
    return lines


def add_modifier(modifiers: dict[str, int], modifier_name: str, value: int) -> dict[str, int]:
    """Add ``value`` to one modifier of ``modifiers`` (in place; also returned) - ``player_id`` is replaced.

    The one rule of how a drawn outcome lands in the set: a prayer
    (:func:`_apply`) and the Karta Dovy (``features/dova.py``) both use it.
    """
    if modifier_name not in INIT_MODIFIERS:
        raise KeyError(f"Unknown modifier {modifier_name!r} (known: {', '.join(INIT_MODIFIERS)}).")
    if modifier_name == "player_id":
        modifiers[modifier_name] = value  # an id is a value, not a counter
    else:
        modifiers[modifier_name] += value
    return modifiers


def _apply(bot: "AlterBot", modifier_name: str, value: int) -> dict[str, int]:
    """Read-modify-write without a single ``await``: two players cannot interleave."""
    if modifier_name not in INIT_MODIFIERS:
        raise KeyError(f"Unknown modifier {modifier_name!r} (known: {', '.join(INIT_MODIFIERS)}).")
    modifiers = add_modifier(_read(bot), modifier_name, value)
    bot.store.modifiers.save(modifiers)
    log.info("Modifier %s changed by %s -> %s", modifier_name, value, modifiers[modifier_name])
    return modifiers


async def modify_modifiers(bot: "AlterBot", modifier_name: str, value: int) -> None:
    """Add ``value`` to one modifier and save the file.

    ``player_id`` is the exception: the given id replaces the stored one.
    Raises ``KeyError`` for a name that is not a modifier.
    """
    _apply(bot, modifier_name, int(value))


# -- the shrine -------------------------------------------------------------------

async def spawn_modifier_shrine(scene: "Scene", *, view: discord.ui.View | None = None) -> Any:
    """Post the shrine of changes; returns the panel (the embed message).

    The event flags and the "Pomódl się" button (``view``) belong to the
    events cog, which also takes the button off once the shrine is used.
    """
    e_title = "🕯️ Kapliczka zmian! 🕯️"

    e_descr = 'Oto kapliczka zmian, która wpłynie na następnego bossa! '\
        'Jeśli chcesz wpłynąć na następnego bossa, to pomódl się przyciskiem pod tym postem '\
        'albo wpisując **!modlitwa**.\n\n'\
        f'*Tylko {patrons.SUPPORTERS_SHORT} ({patrons.SUPPORTERS_LONG}) znają odpowiednią modlitwę,'\
        ' ale każdy może skorzystać z błogosławieństw rzuconych na potwora.*'

    embed = discord.Embed(title=e_title, description=e_descr, color=SHRINE_COLOR)
    embed.set_thumbnail(url=SHRINE_THUMBNAIL)

    image = ASSETS_DIR / "events" / "shrines" / f"{random.randint(0, SHRINE_IMAGES - 1)}.png"
    if image.is_file():
        try:
            await scene.send(file_path=image)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the picture is decoration, the embed is the event
            log.warning("Could not post the shrine picture %s: %s: %s", image, type(exc).__name__, exc)
    else:
        log.warning("Shrine picture %s is missing; posting the shrine without it.", image)
    extra = {} if view is None else {"view": view}
    panel = await scene.send(embed=embed, **extra)
    log.info("Shrine spawned.")
    return panel


def _eligible(scene: "Scene", current: dict[str, int], leave_out: Iterable[str] = ()) -> list[str]:
    """Outcomes that can be drawn now.

    The legacy code drew again when it hit a one-off modifier that was already
    taken (a player already is the next boss / the ban is already on); drawing
    from the outcomes that are still possible gives the same odds without a loop.
    The changed commands are a one-off as well.  ``leave_out``: outcomes the
    caller does not want at all (the Karta Dovy on a boss already on the
    channel: the rarity and the player-boss could not change it any more).
    """
    skipped = set(leave_out)
    pool = []
    for name in RANDOM_POOL:
        if name in skipped:
            continue
        if name == "player_id" and (current["player_id"] != 0 or scene.author is None):
            continue
        if name == "ban_loser" and current["ban_loser"] != 0:
            continue
        if name == COMMANDS_CHANGED and current[COMMANDS_CHANGED] != 0:
            continue
        pool.append(name)
    return pool


def draw_modifier(scene: "Scene", current: dict[str, int], *, leave_out: Iterable[str] = ()) -> tuple[str, int]:
    """Draw one outcome of a prayer for a boss whose set is ``current``: ``(name, value)``.  Synchronous.

    Each outcome that is still possible (:func:`_eligible`) is equally likely;
    the value comes from :data:`VALUE_RANGES` (1 for the one-off outcomes and
    ``reset_cd``, the praying player's id for ``player_id``).  Nothing is
    stored here: the caller writes it without an ``await`` in between, so two
    draws at the same moment cannot both take a one-off outcome.  The random
    numbers are drawn exactly as the legacy prayer drew them (one ``choice``,
    then a ``randint`` only for an outcome with a range).
    """
    modifier = random.choice(_eligible(scene, current, leave_out))
    if modifier == "player_id":
        return modifier, int(scene.author.id)
    low_high = VALUE_RANGES.get(modifier)
    return modifier, (random.randint(*low_high) if low_high is not None else 1)


def prayer_text(modifier: str, value: int, *, sacrefice: bool = False) -> tuple[str, str]:
    """``(title, description)`` of the embed that announces a drawn outcome (a prayer / a sacrifice)."""
    if sacrefice:
        title = "Poświęcono towarzysza!"
        base_desc = "Poświęcenie sprawiło, że "
    else:
        title = 'Błogosławieństwo kapliczki!'
        base_desc = "Modlitwa w kapliczce sprawiła, że "

    if modifier == "hp_reduced_perc":
        desc = base_desc + \
            "następny boss i potwory **będą miały o " + str(value) + "% mniej życia!**"
    elif modifier == "hp_boost_perc":
        desc = base_desc + \
            "następny boss i potwory **będą miały o " + str(value) + "% więcej życia!**"
    elif modifier == "rarity_boost":
        desc = base_desc + \
            "następny boss **będzie o " + str(value) + " poziom rzadszy!**"
    elif modifier == "drop_boost_perc":
        desc = base_desc + \
            "następny boss **będzie miał o " + str(value) + "% rzadsze przedmioty!**"
    elif modifier == "player_id":
        desc = base_desc + \
            "**zostaniesz następnym bossem i potworami <@" + str(value) + ">!**"
    elif modifier == "ban_loser":
        desc = base_desc + \
            "**następny gracz, który przegra z bossem lub potworami zostanie zbanowany do następnego bossa!**"
    elif modifier == "time_reduced_perc":
        desc = base_desc + \
            "podczas walki z następnym bossem i potworami **będzie o " + str(value) + "% mniej czasu na reakcję!**"
    elif modifier == "time_increased_perc":
        desc = base_desc + \
            "podczas walki z następnym bossem i potworami **będzie o " + str(value) + "% więcej czasu na reakcję!**"
    elif modifier == "points_boost":
        desc = base_desc + \
            "za wygraną z kolejnym bossem **będzie o " + str(value) + " więcej punktów!**"
    elif modifier == COMMANDS_CHANGED:
        desc = base_desc + \
            "w walce z następnym bossem i potworami **obowiązują inne komendy!** " \
            "*(Nie dotyczy mitycznego bossa.)*"
    else:  # reset_cd
        desc = base_desc + \
            "**wszyscy bohaterowie są wypoczęci i mogą wyruszyć na polowanie!**"
    return title, desc


async def announce_modifier(scene: "Scene", modifier: str, value: int, *, sacrefice: bool = False) -> None:
    """Announce a drawn outcome that is already in force (the shrine's embed).  Never raises.

    A failed post must not make the caller treat the prayer as "did not
    happen" (and allow a second one): the owner is told what works instead.
    """
    title, desc = prayer_text(modifier, value, sacrefice=sacrefice)
    embed = discord.Embed(title=title,
                          url='https://www.altermmo.pl/wp-content/uploads/Prayge.png',
                          description=desc, color=SHRINE_COLOR)
    embed.set_thumbnail(url=SHRINE_THUMBNAIL)
    embed.set_footer(text='Powodzenia!')
    try:
        await scene.send(embed=embed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Modifier %s applied but could not be announced: %s: %s",
                    modifier, type(exc).__name__, exc)
        await scene.bot.log_to_owner(f"Nie udało się ogłosić modyfikatora na kanale ({type(exc).__name__}). "
                                     f"Modyfikator działa: {desc}")


async def random_modifiers(scene: "Scene", msg: bool = True, sacrefice: bool = False) -> str:
    """Draw one random modifier for the next boss, save it and (``msg``) announce it.

    ``sacrefice`` (legacy spelling) only changes the wording: a pet was
    sacrificed instead of a prayer.  Returns the name of the drawn modifier.
    The draw (:func:`draw_modifier`) and the announcement
    (:func:`announce_modifier`) are shared with the Karta Dovy.
    """
    bot = scene.bot
    modifier, value = draw_modifier(scene, _read(bot))

    # The effect comes first and without an await between the draw and the
    # write, so two prayers at the same moment cannot both take a one-off modifier.
    if modifier == RESET_CD:
        from . import daily  # lazy: daily imports this module
        await daily.clear_daily_file(bot)
        log.info("Shrine: daily hunting cooldown reset.")
    else:
        _apply(bot, modifier, int(value))

    if msg:
        await announce_modifier(scene, modifier, value, sacrefice=sacrefice)
    return modifier
