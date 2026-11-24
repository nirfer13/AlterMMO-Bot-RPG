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
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any

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
    modifiers = _read(bot)
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
    if not lines:
        return ""
    return "\n\nDodatkowe statystyki:" + "".join(lines)


def _apply(bot: "AlterBot", modifier_name: str, value: int) -> dict[str, int]:
    """Read-modify-write without a single ``await``: two players cannot interleave."""
    if modifier_name not in INIT_MODIFIERS:
        raise KeyError(f"Unknown modifier {modifier_name!r} (known: {', '.join(INIT_MODIFIERS)}).")
    modifiers = _read(bot)
    if modifier_name == "player_id":
        modifiers[modifier_name] = value  # an id is a value, not a counter
    else:
        modifiers[modifier_name] += value
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


def _eligible(scene: "Scene", current: dict[str, int]) -> list[str]:
    """Outcomes that can be drawn now.

    The legacy code drew again when it hit a one-off modifier that was already
    taken (a player already is the next boss / the ban is already on); drawing
    from the outcomes that are still possible gives the same odds without a loop.
    The changed commands are a one-off as well.
    """
    pool = []
    for name in RANDOM_POOL:
        if name == "player_id" and (current["player_id"] != 0 or scene.author is None):
            continue
        if name == "ban_loser" and current["ban_loser"] != 0:
            continue
        if name == COMMANDS_CHANGED and current[COMMANDS_CHANGED] != 0:
            continue
        pool.append(name)
    return pool


async def random_modifiers(scene: "Scene", msg: bool = True, sacrefice: bool = False) -> str:
    """Draw one random modifier for the next boss, save it and (``msg``) announce it.

    ``sacrefice`` (legacy spelling) only changes the wording: a pet was
    sacrificed instead of a prayer.  Returns the name of the drawn modifier.
    """
    bot = scene.bot

    if sacrefice:
        title = "Poświęcono towarzysza!"
        base_desc = "Poświęcenie sprawiło, że "
    else:
        title = 'Błogosławieństwo kapliczki!'
        base_desc = "Modlitwa w kapliczce sprawiła, że "

    modifier = random.choice(_eligible(scene, _read(bot)))

    if modifier == "hp_reduced_perc":
        random_value = random.randint(5, 15)
        desc = base_desc + \
            "następny boss i potwory **będą miały o " + str(random_value) + "% mniej życia!**"
    elif modifier == "hp_boost_perc":
        random_value = random.randint(5, 15)
        desc = base_desc + \
            "następny boss i potwory **będą miały o " + str(random_value) + "% więcej życia!**"
    elif modifier == "rarity_boost":
        random_value = 1
        desc = base_desc + \
            "następny boss **będzie o " + str(random_value) + " poziom rzadszy!**"
    elif modifier == "drop_boost_perc":
        random_value = random.randint(5, 25)
        desc = base_desc + \
            "następny boss **będzie miał o " + str(random_value) + "% rzadsze przedmioty!**"
    elif modifier == "player_id":
        random_value = scene.author.id
        desc = base_desc + \
            "**zostaniesz następnym bossem i potworami <@" + str(scene.author.id) + ">!**"
    elif modifier == "ban_loser":
        random_value = 1
        desc = base_desc + \
            "**następny gracz, który przegra z bossem lub potworami zostanie zbanowany do następnego bossa!**"
    elif modifier == "time_reduced_perc":
        random_value = random.randint(5, 25)
        desc = base_desc + \
            "podczas walki z następnym bossem i potworami **będzie o " + str(random_value) + "% mniej czasu na reakcję!**"
    elif modifier == "time_increased_perc":
        random_value = random.randint(5, 25)
        desc = base_desc + \
            "podczas walki z następnym bossem i potworami **będzie o " + str(random_value) + "% więcej czasu na reakcję!**"
    elif modifier == "points_boost":
        random_value = random.randint(1, 5)
        desc = base_desc + \
            "za wygraną z kolejnym bossem **będzie o " + str(random_value) + " więcej punktów!**"
    elif modifier == COMMANDS_CHANGED:
        random_value = 1
        desc = base_desc + \
            "w walce z następnym bossem i potworami **obowiązują inne komendy!** " \
            "*(Nie dotyczy mitycznego bossa.)*"
    else:  # reset_cd
        random_value = 1
        desc = base_desc + \
            "**wszyscy bohaterowie są wypoczęci i mogą wyruszyć na polowanie!**"

    # The effect comes first and without an await between the draw and the
    # write, so two prayers at the same moment cannot both take a one-off modifier.
    if modifier == RESET_CD:
        from . import daily  # lazy: daily imports this module
        await daily.clear_daily_file(bot)
        log.info("Shrine: daily hunting cooldown reset.")
    else:
        _apply(bot, modifier, int(random_value))

    if msg:
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
            # The modifier is already in force: failing here would make the
            # caller treat the prayer as "did not happen" and allow a second one.
            log.warning("Modifier %s applied but could not be announced: %s: %s",
                        modifier, type(exc).__name__, exc)
            await bot.log_to_owner(f"Nie udało się ogłosić modyfikatora na kanale ({type(exc).__name__}). "
                                   f"Modyfikator działa: {desc}")
    return modifier
