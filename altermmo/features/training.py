"""The "Trening towarzyszy" event: the companions of the players who sign up spar in a knock-out bracket.

Owner's request of 2026-11-22: "Jest minuta na zapisanie i wszyscy aktualni
towarzysze zapisanych walczą ze sobą jak w turnieju.  System podobny jak przy
walkach PvP, ale bez wpisywania poleceń."  The rules he approved:

* **picture** - the owner paints it himself and puts it into
  ``assets/events/training`` (``0.png``; more files are welcome, one is drawn
  at random).  Without a picture there is no event: the events cog leaves it
  out of the draw (:func:`pictures` is asked every time an event is drawn, so
  the first file switches the event on without a restart);
* **sign-up** - the picture, then one panel with "Dołącz" open for a minute.
  Only a player whose ACTIVE companion is hatched may join (eggs do not
  take part); the others get a short refusal and are not on the roster;
* **the stable** - while the sign-up is open the stable works although the
  event holds the channel (``GameState.stable_stays_open``): players choose
  which companion they send.  Nothing else becomes available, and the
  exception ends when the sign-up closes;
* **who fights** - at the close every signed-up player's active companion is
  read ONCE (:func:`_snapshot`) and that one fights the whole training,
  whatever its owner does afterwards ("nie wolno zmieniać między rundami").
  A player whose active companion is by then gone or an egg is dropped;
* **bracket** - as the PvP tournament: shuffle, an odd one passes to the next
  round, rounds until one companion is left;
* **a fight** - nobody types.  :func:`simulate` plays the players' duel
  (``boss.duel_fight``) with luck in place of the reaction time, as a pure
  function; :func:`_play` only shows the blows it returns: ONE message per
  fight, edited as the fight goes - a life bar per companion and, since
  2026-11-23, the advantage bar of the players' duel between them
  (:func:`advantage_bar`: one bar in the two sides' colours, split by the
  lives, with who leads), so the owners see at a glance who is winning;
* **rewards** - once, after the winner is known: the winner a loot roll (the
  call of the duel event in ``features/events.py``, where the challenger
  who wins without a fight gets ``daily.rand_loot(scene, 2, challenger, 0)``:
  the same function, rarity and boost), the finalist one rebirth scroll,
  every companion that fought (also a lone one that found no partner) one
  scroll shard for its owner.  No ranking points, no "dead hunter" role.

Like every event function :func:`spawn_training` does not take the event
flags itself: the events cog runs it inside ``state.running_event`` (the
channel is busy for the whole event) and handles a failure in the middle
(the owner is told, the flags are released).  Rewards are handed out in one
place at the very end, so an event that breaks earlier grants nothing and
nothing can be granted twice; once the last fight is over, a message Discord
refuses no longer stands between the players and their rewards.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Mapping, Sequence

import discord

from ..core import timing, ui
from . import boss, daily, events, pets

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- the picture -------------------------------------------------------------------------

#: Where the owner puts the picture(s) of the event.
PICTURES_DIR = events.EVENTS_DIR / "training"
#: What counts as a picture there (the owner: ``0.png``, maybe ``1.png``, ``.jpg``, ``.gif``).
PICTURE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# -- waiting and pacing (seconds) --------------------------------------------------------

#: The sign-up: (production, debug mode), like the waits of the other events.
TRAINING_SIGNUP = (60, 10)
#: A fight is shown in at most this many edits of its message (one exchange - both
#: companions' blows - per edit; a longer fight shows several exchanges per edit).
MAX_EDITS = 12
#: How long the show of one fight should take when it has enough exchanges to fill it.
FIGHT_SECONDS = 18.0
#: Edits of the fight message are at least / at most this far apart.  Discord allows about
#: 5 edits per 5 s on a channel and other messages share that budget: 1.5 s leaves room.
EDIT_GAP_MIN, EDIT_GAP_MAX = 1.5, 3.0
#: An edit that Discord does not answer within this many seconds counts as failed.
EDIT_TIMEOUT = 10.0
#: Pause after a fight (before the next one / before the winner is announced).
FIGHT_PAUSE = 3.0

# -- the rules of a fight ----------------------------------------------------------------

#: Life of a companion and the longest fight: those of the players' duel.
START_LIFE = boss.DUEL_HP
MAX_BLOWS = boss.DUEL_MAX_TURNS
#: A blow before the companion's stats: a random whole number in this range.
BLOW_MIN, BLOW_MAX = 100, 400
#: The stats of a companion that matter in a fight (class, frame, seal, foil and level do not).
STAT_KEYS = ("CRIT_PERC", "REPLACE_PERC", "DEF_PERC", "LOWHP_PERC", "SLOW_PERC")

# -- rewards -----------------------------------------------------------------------------

#: The winner's loot: the call of the duel event in ``features/events.py``
#: (``daily.rand_loot(scene, 2, challenger, 0)``) - the same function, rarity and boost.
WINNER_LOOT_RARITY = 2
WINNER_LOOT_BOOST = 0
FINALIST_SCROLLS = 1
FIGHTER_SHARDS = 1

# -- texts -------------------------------------------------------------------------------

PAW = "🐾"
SQUARES = ("🟥", "🟦")
EMPTY_SQUARE = "⬛"
BAR_LENGTH = 10
#: Squares of the advantage bar (:func:`advantage_bar`).  Even, so that equal lives split it in
#: the middle; no wider than a life bar with its numbers, so it stays one line on a phone.
ADVANTAGE_LENGTH = 14

TRAINING_TITLE = "<:peepoBlush:984769061340737586> Trening towarzyszy! <a:PeepoRiot:1067317732942565416>"
TRAINING_DESCR = ('Na placu treningowym robi się tłoczno! Wyślijcie swoich towarzyszy na sparing - zmierzą się '
                  'ze sobą jak w turnieju, a Wy możecie spokojnie kibicować: niczego nie trzeba wpisywać.'
                  '\n\n**Kliknij „Dołącz” pod postem, żeby zgłosić towarzysza, który jest teraz przy Tobie.** '
                  'Do końca zapisów możesz jeszcze zajrzeć do stajni (**!stajnia**) i zmienić zawodnika - '
                  'walczy ten, który będzie u Twojego boku, gdy zapisy się skończą. Jajka zostają w domu.'
                  '\n\n*Zwycięzca zgarnia łup, finalista zwój odrodzenia, a każdy walczący towarzysz '
                  'przynosi właścicielowi fragment zwoju.*')
TRAINING_COLOUR = 0x2E8B57

MSG_TRAINING_NO_PET = ("Nie masz przy sobie towarzysza, którego można wysłać na trening "
                       "<:Sadge:936907659142111273> Zajrzyj do stajni (**!stajnia**).")
MSG_TRAINING_EGG = ("Twój towarzysz to jeszcze jajko - na trening jest za wcześnie <:Bedge:970576892874854400> "
                    "Wyciągnij ze stajni wyklutego towarzysza (**!stajnia**) i kliknij jeszcze raz.")
#: The companion at his side is on a companion mission (features/missions.py) until ``when``.
MSG_TRAINING_AWAY = ("Twój towarzysz jest na zleceniu - wróci {when} i dopiero wtedy będzie mógł trenować "
                     "<:Bedge:970576892874854400>")
MSG_TRAINING_NOBODY = "*Plac treningowy świeci pustkami...*"
MSG_TRAINING_DROPPED = "🥚 Bez wyklutego towarzysza u boku nie ma treningu - tym razem odpadają: {players}."
MSG_TRAINING_ALONE = ("Towarzysz **{pet}** ({mention}) nie ma dziś z kim trenować <:Sadge:936907659142111273> "
                      "Za same chęci dostaje fragment zwoju.")
MSG_TRAINING_CLOSED = "**Zapisy na trening zakończone!** Na plac wychodzą: {fighters}"
#: One companion in the lines above and below: its name (or species) and its owner.
FIGHTER_ENTRY = "**{pet}** ({mention})"
MSG_TRAINING_ROUND = "**Trening towarzyszy - Runda {round}**"
MSG_TRAINING_BYE = "🍀 Towarzysz **{pet}** ({mention}) nie ma pary i od razu przechodzi do kolejnej rundy!"
MSG_TRAINING_WINNER = "🏆 **Trening wygrywa towarzysz {pet}** - gratulacje, {mention}! 🏆"
MSG_TRAINING_REWARDS = ("🎁 Nagrody: {winner} - łup zwycięzcy, {finalist} - zwój odrodzenia, a każdy walczący "
                        "towarzysz przynosi właścicielowi fragment zwoju.")
MSG_TRAINING_REWARD_FAILED = ("Coś poszło nie tak przy wydawaniu części nagród <:Sadge:936907659142111273> "
                              "Administracja dostała już znać.")
LOG_TRAINING_REWARD_FAILED = ("Trening towarzyszy: nie udało się przyznać nagrody ({what}) graczowi "
                              "<@{player_id}> (`{error}`). Przyznaj ją ręcznie.")
#: What the owner's log calls the three rewards.
REWARD_SHARD, REWARD_SCROLL, REWARD_LOOT = "fragment zwoju", "zwój odrodzenia", "łup zwycięzcy"

# The fight message.
FIGHT_TITLE = "⚔️ {first} kontra {second}"
FIGHT_VERSUS = "{first} ⚔️ {second}"
FIGHT_SIDE = "{square} **{pet}** · {owner}\n{bar} **{life}** / {full}"
#: Under the advantage bar: who has more life left (the square tells two companions of one name
#: apart) / nobody.  Not shown in the last version - there the result says who won.
FIGHT_LEADS = "{square} Prowadzi **{pet}**"
FIGHT_EVEN = "⚖️ Walka wyrównana"
FIGHT_STARTS = "*Zaczyna **{pet}**.*"
FIGHT_EARLIER = "*...wcześniej: {count} {blows}*"
LINE_HIT = "{square} **{pet}** uderza za **{damage}**"
LINE_FULL = "{square} **{pet}** uderza z pełną mocą za **{damage}** 🔄"
LINE_CRIT = "{square} **{pet}** trafia krytycznie za **{damage}** 💥"
LINE_FULL_CRIT = "{square} **{pet}** trafia krytycznie z pełną mocą za **{damage}** 🔄💥"
LINE_BLOCKED_PART = " (🛡️ zablokowano {blocked})"
LINE_BLOCKED_ALL = "{square} **{pet}** atakuje, ale **{foe}** blokuje cały cios 🛡️"
FIGHT_WON = "🏆 **Wygrywa {pet}** ({owner})!"
FIGHT_DRAWN = "⚖️ Remis po {blows} ciosach! Los zdecydował: dalej przechodzi **{pet}** ({owner})."
FIGHT_COLOUR, FIGHT_OVER_COLOUR = 0x2E8B57, 0xFCBA03
#: At most this many of the latest blows are told in words under the life bars.
LOG_LINES = 4
#: Names of companions and owners are cut to this length in the texts.
NAME_LENGTH = 40
#: The lines that list players stay below this length (Discord: 2000 characters).
LINE_LIMIT = 1800

#: Lines that only tell what happened ping nobody (the owners are pinged where it matters:
#: when their companion's fight starts and when it wins the training).
QUIET = discord.AllowedMentions.none()


def _ping(*members: Any) -> discord.AllowedMentions:
    """Only ``members`` are notified (a companion called ``@everyone`` pings nobody)."""
    return discord.AllowedMentions(everyone=False, roles=False, users=list(members))


# -- the picture -------------------------------------------------------------------------

def pictures() -> list[Path]:
    """The pictures of the event the owner has supplied, by name; empty when there is none yet.

    Looked up on every call (the events cog asks before each draw): the event
    starts to be drawn as soon as the first file is there, without a restart.
    """
    try:
        found = [path for path in PICTURES_DIR.iterdir()
                 if path.suffix.lower() in PICTURE_SUFFIXES and path.is_file() and path.stat().st_size > 0]
    except OSError:  # no such folder (yet)
        return []
    return sorted(found, key=lambda path: path.name)


def has_picture() -> bool:
    """True when the event can be shown (see :func:`pictures`)."""
    return bool(pictures())


class PictureMissing(RuntimeError):
    """The event was started although :data:`PICTURES_DIR` holds no picture."""


# -- the fight: a pure simulation --------------------------------------------------------

@dataclass(frozen=True)
class Blow:
    """One blow of a fight."""

    #: Who struck: 0 (the first companion) or 1.
    attacker: int
    #: The blow before the stats: 100..400, 400 when the attack was replaced (full power).
    base: int
    #: Life the opponent lost.
    damage: int
    #: The part of the blow the opponent's defence took off.
    blocked: int
    critical: bool
    full_power: bool
    #: Life of both companions after the blow (never below 0).
    life: tuple[int, int]


@dataclass(frozen=True)
class Sparring:
    """A whole fight, blow by blow."""

    #: Life of both companions before the first blow (lowered by the opponent's LOWHP_PERC).
    start_life: tuple[int, int]
    #: Who struck first: 0 or 1.
    first: int
    blows: tuple[Blow, ...]
    #: 0 or 1; None: a draw (:data:`MAX_BLOWS` blows and both still stand).
    winner: int | None

    def life_after(self, blows: int) -> tuple[int, int]:
        """Life of both companions after the first ``blows`` blows."""
        return self.blows[blows - 1].life if blows > 0 else self.start_life


def _stat(stats: Mapping[str, Any], key: str) -> float:
    """A stat as a plain float (the database hands out ``Decimal``; a missing one is 0)."""
    try:
        return float(stats.get(key) or 0)
    except (TypeError, ValueError, ArithmeticError):
        return 0.0


def _roll(rng: Any, chance_percent: float) -> bool:
    """True with ``chance_percent`` % probability (as ``boss._roll``)."""
    return rng.random() * 100 < chance_percent


def simulate(first: Mapping[str, Any], second: Mapping[str, Any], rng: Any = random) -> Sparring:
    """Play a fight of two companions and return every blow and the result.

    The players' duel (``boss.duel_fight``) with luck in place of the reaction
    time.  ``first`` / ``second``: the stats of the two companions
    (:data:`STAT_KEYS`; a missing one counts as 0).  ``rng``: a
    ``random.Random`` (or anything with ``random()``, ``randint()`` and
    ``randrange()``).  Nothing else is read and nothing is changed, so a fight
    can be replayed from a seed.

    * each companion starts with :data:`START_LIFE`, lowered by the OPPONENT's
      LOWHP_PERC percent (never below 1);
    * who strikes first is drawn; then they strike in turns;
    * a blow: a random whole number :data:`BLOW_MIN`..:data:`BLOW_MAX` - or,
      with the attacker's REPLACE_PERC chance, :data:`BLOW_MAX` (full power);
      times (100 + the attacker's SLOW_PERC) / 100; doubled with the attacker's
      CRIT_PERC chance; reduced by the DEFENDER's DEF_PERC percent (whole
      percents, as the duel's ``reduction``); rounded, never negative;
    * the companion whose life reaches 0 loses; after :data:`MAX_BLOWS` blows
      without that it is a draw (``winner`` None - the caller draws who goes on).

    Draws from ``rng``, in this order: the starter (``randrange(2)``); then for
    every blow the full-power roll (``random()``), the blow (``randint``, only
    when the roll failed) and the critical roll (``random()``).
    """
    stats = (first, second)
    start = tuple(max(1, min(START_LIFE, round(START_LIFE * (100 - _stat(stats[1 - index], "LOWHP_PERC")) / 100)))
                  for index in (0, 1))
    life = list(start)
    starter = rng.randrange(2)
    blows: list[Blow] = []
    winner: int | None = None
    for turn in range(MAX_BLOWS):
        me = (starter + turn) % 2
        foe = 1 - me
        full_power = _roll(rng, _stat(stats[me], "REPLACE_PERC"))
        base = BLOW_MAX if full_power else rng.randint(BLOW_MIN, BLOW_MAX)
        power = base * (100 + _stat(stats[me], "SLOW_PERC")) / 100
        critical = _roll(rng, _stat(stats[me], "CRIT_PERC"))
        if critical:
            power *= 2
        power = max(power, 0.0)
        reduction = (100 - int(_stat(stats[foe], "DEF_PERC"))) / 100
        damage = max(0, round(power * reduction))
        blocked = max(0, round(power) - damage)
        life[foe] = max(0, life[foe] - damage)
        blows.append(Blow(me, base, damage, blocked, critical, full_power, (life[0], life[1])))
        if life[foe] <= 0:
            winner = me
            break
    return Sparring((start[0], start[1]), starter, tuple(blows), winner)


# -- who fights --------------------------------------------------------------------------

@dataclass(frozen=True)
class Fighter:
    """A companion in the training: what was read from its ``PETS`` row when the sign-up closed."""

    member: Any
    pet_id: int
    #: How the texts call it: its own name, or its species when it was never named.
    name: str
    species: str
    level: int
    stats: Mapping[str, float]

    @property
    def shown(self) -> str:
        """:attr:`name`, safe inside a message (no markdown, no mentions)."""
        return _safe(self.name)

    @property
    def owner(self) -> str:
        """The owner's name as plain text (it pings nobody)."""
        return _safe(str(getattr(self.member, "display_name", None) or self.member))


def _safe(text: str) -> str:
    cut = str(text).strip()[:NAME_LENGTH]
    return discord.utils.escape_mentions(discord.utils.escape_markdown(cut))


def _fighter(member: Any, pet: Any) -> Fighter:
    species = pets.POLISH_TYPES.get(pet["type"], "") or pets.DEFAULT_NAME
    name = str(pet["pet_name"] or "").strip()
    if not name or name == pets.DEFAULT_NAME:
        name = species
    return Fighter(member=member, pet_id=pets._int(pet["pet_id"]), name=name, species=species,
                   level=pets._int(pet["pet_lvl"]),
                   stats={key: _stat(pet, key.lower()) for key in STAT_KEYS})


#: ``{away}``: ``pets.sql_not_away`` - a companion on a mission is "no companion" (a read-only query).
_SQL_SNAPSHOT = """SELECT o.PLAYER_ID AS OWNER_ID, p.* FROM PETOWNER o JOIN PETS p ON p.PET_ID = o.PET_ID
                   WHERE o.PLAYER_ID = ANY($1::numeric[]) AND o.PET_ID > 0 AND {away}"""


async def _snapshot(bot: "AlterBot", members: Sequence[Any]) -> tuple[list[Fighter], list[Any]]:
    """The active companion of every signed-up player, read once: ``(fighters, dropped players)``.

    One query for everybody - one moment.  From here on the training knows
    only these rows: a companion put into the stable, renamed or rerolled
    later fights as it was.  A player without an active companion, or with an
    egg, is dropped - and so is one whose companion is on a companion mission
    (defensive: a mission cannot be sent while the sign-up holds the channel).
    """
    if not members:
        return [], []
    away = pets.sql_not_away("p.PET_ID", await pets.missions_table_ready(bot))
    rows = await bot.db.fetch(_SQL_SNAPSHOT.format(away=away), [int(member.id) for member in members])
    active: dict[int, Any] = {}
    for row in rows:
        active.setdefault(pets._int(row["owner_id"]), row)
    fighters: list[Fighter] = []
    dropped: list[Any] = []
    for member in members:
        pet = active.get(int(member.id))
        if pet is None or pets._int(pet["pet_lvl"]) < 1:
            dropped.append(member)
        else:
            fighters.append(_fighter(member, pet))
    return fighters, dropped


class _TrainingSignUp(events._SignUp):
    """The sign-up panel of the training: only a hatched active companion gets its owner in.

    A refused player is not remembered: he may take a hatched companion out
    of the stable and click again.
    """

    async def _allow(self, interaction: Any, key: str) -> str | None:
        user_id = interaction.user.id
        if user_id in self._clicked:
            return events.MSG_ALREADY_SIGNED
        bot = self.scene.bot
        try:
            pet = await pets._active_pet(bot.db, int(user_id))
            until = await pets.away_until(bot, bot.db, pet["pet_id"]) if pet is not None else None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the database hiccuped: the player may click again
            log.exception("Training: the companion of %s could not be read.", user_id)
            return ui.MSG_FAILED
        if until is not None:  # (on a companion mission: back at ``until``)
            return MSG_TRAINING_AWAY.format(when=ui.when(until))
        if pet is None:
            return MSG_TRAINING_NO_PET
        if pets._int(pet["pet_lvl"]) < 1:
            return MSG_TRAINING_EGG
        # Check-and-add with no await in between: a second click that got here while the
        # database was asked is told so and counted once.
        if user_id in self._clicked:
            return events.MSG_ALREADY_SIGNED
        self._clicked.add(user_id)
        return None


# -- showing a fight ---------------------------------------------------------------------

def frame_counts(blows: int) -> list[int]:
    """How many blows the fight message shows after each of its edits (the last one: all).

    One exchange (two blows) per edit; a fight of more than :data:`MAX_EDITS`
    exchanges shows several per edit, so that no fight needs more edits.
    """
    exchanges = math.ceil(blows / 2)
    step = 2 * max(1, math.ceil(exchanges / MAX_EDITS))
    return [*range(step, blows, step), blows]


def edit_gap(edits: int) -> float:
    """Seconds between two edits of a fight shown in ``edits`` edits.

    The show takes about :data:`FIGHT_SECONDS`; the gap stays between
    :data:`EDIT_GAP_MIN` (Discord's edit budget) and :data:`EDIT_GAP_MAX` (a
    very short fight is not stretched).
    """
    return min(EDIT_GAP_MAX, max(EDIT_GAP_MIN, FIGHT_SECONDS / max(1, edits)))


def life_bar(life: int, square: str) -> str:
    """``🟥🟥🟥⬛⬛⬛⬛⬛⬛⬛``: ``life`` out of :data:`START_LIFE` (a living companion keeps one square)."""
    filled = 0 if life <= 0 else max(1, min(BAR_LENGTH, round(BAR_LENGTH * life / START_LIFE)))
    return square * filled + EMPTY_SQUARE * (BAR_LENGTH - filled)


def advantage_bar(life: Sequence[int]) -> str:
    """``🟥🟥🟥🟥🟥🟥🟥🟥🟥🟦🟦🟦🟦🟦``: ONE bar shared by the two lives - who is winning, at a glance.

    The bar of the players' duel (``general.victory_bar``) in the colours of the two sides
    (:data:`SQUARES`): each companion gets its share of the life both have left, so the border
    moves towards the one that is losing.  A companion that still stands keeps one square; one
    that fell has none - only a fight that is over shows a bar of one colour.  (Both at 0 cannot
    happen in a fight; the bar is empty then.)
    """
    first, second = (max(0, int(value)) for value in life)
    if first + second <= 0:
        return EMPTY_SQUARE * ADVANTAGE_LENGTH
    share = round(ADVANTAGE_LENGTH * first / (first + second))
    share = max(1 if first else 0, min(ADVANTAGE_LENGTH - (1 if second else 0), share))
    return SQUARES[0] * share + SQUARES[1] * (ADVANTAGE_LENGTH - share)


def leader(life: Sequence[int]) -> int | None:
    """Who leads - the companion with more life left: 0 or 1; None while they are even."""
    if life[0] == life[1]:
        return None
    return 0 if life[0] > life[1] else 1


def blow_line(blow: Blow, pair: Sequence[Fighter]) -> str:
    """One blow in words: who hit, how hard, critical / full power / what was blocked."""
    me, foe = pair[blow.attacker], pair[1 - blow.attacker]
    square = SQUARES[blow.attacker]
    if blow.damage <= 0:
        return LINE_BLOCKED_ALL.format(square=square, pet=me.shown, foe=foe.shown)
    text = {(False, False): LINE_HIT, (True, False): LINE_FULL,
            (False, True): LINE_CRIT, (True, True): LINE_FULL_CRIT}[(blow.full_power, blow.critical)]
    line = text.format(square=square, pet=me.shown, damage=blow.damage)
    if blow.blocked > 0:
        line += LINE_BLOCKED_PART.format(blocked=blow.blocked)
    return line


def _blows_word(count: int) -> str:
    """``1 cios`` / ``2 ciosy`` / ``5 ciosów``."""
    if count == 1:
        return "cios"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "ciosy"
    return "ciosów"


def fight_embed(pair: Sequence[Fighter], sparring: Sparring, shown: int, *,
                winner: int | None = None) -> discord.Embed:
    """The fight message after ``shown`` blows; with ``winner`` (0 / 1) its last version.

    Both companions with their owners and life bars, the advantage bar
    (owner, 2026-11-23: "żeby właściciele w napięciu mogli śledzić postęp
    walki" - it moves with every edit) with who leads, the latest blows in
    words and - at the end - who won (after a draw: whom the lot chose).  The
    last version keeps the bar in its final state and says who won instead of
    who leads.
    """
    life = sparring.life_after(shown)
    lines = [FIGHT_SIDE.format(square=SQUARES[index], pet=fighter.shown, owner=fighter.owner,
                               bar=life_bar(life[index], SQUARES[index]), life=life[index], full=START_LIFE)
             for index, fighter in enumerate(pair)]
    lines += ["", advantage_bar(life)]
    if winner is None:
        ahead = leader(life)
        lines.append(FIGHT_EVEN if ahead is None
                     else FIGHT_LEADS.format(square=SQUARES[ahead], pet=pair[ahead].shown))
    lines.append("")
    if shown <= 0:
        lines.append(FIGHT_STARTS.format(pet=pair[sparring.first].shown))
    else:
        told = sparring.blows[max(0, shown - LOG_LINES):shown]
        if shown > len(told):
            lines.append(FIGHT_EARLIER.format(count=shown - len(told), blows=_blows_word(shown - len(told))))
        lines += [blow_line(blow, pair) for blow in told]
    if winner is not None:
        text = FIGHT_DRAWN if sparring.winner is None else FIGHT_WON
        lines += ["", text.format(pet=pair[winner].shown, owner=pair[winner].owner, blows=len(sparring.blows))]
    title = FIGHT_TITLE.format(first=pair[0].name.strip()[:NAME_LENGTH], second=pair[1].name.strip()[:NAME_LENGTH])
    return discord.Embed(title=title, description="\n".join(lines),
                         color=FIGHT_COLOUR if winner is None else FIGHT_OVER_COLOUR)


async def _edit(message: Any, embed: discord.Embed) -> str:
    """Show the next version of the fight message: ``"ok"``, ``"failed"`` or ``"gone"``.  Never raises.

    Only the embed changes: the owners' mentions stay in the content of the
    first version (an edit notifies nobody).
    """
    try:
        await asyncio.wait_for(message.edit(embed=embed, allowed_mentions=QUIET), EDIT_TIMEOUT)
        return "ok"
    except asyncio.CancelledError:
        raise
    except discord.NotFound:
        log.info("Training: the fight message was deleted.")
        return "gone"
    except Exception as exc:  # noqa: BLE001 - the fight is decided already; this is only its picture
        log.warning("Training: the fight message could not be edited (%s: %s).", type(exc).__name__, exc)
        return "failed"


async def _play(scene: "Scene", pair: Sequence[Fighter], sparring: Sparring, winner: int) -> None:
    """Show a fight that is already decided: one message, edited exchange by exchange.

    The owners are mentioned once, in the first version.  A failed edit is
    skipped; when the last version could not be shown (or the message was
    deleted) the result is posted as a new message - the fight is never lost
    with its message.
    """
    first, second = pair
    message = await scene.send(FIGHT_VERSUS.format(first=first.member.mention, second=second.member.mention),
                               embed=fight_embed(pair, sparring, 0),
                               allowed_mentions=_ping(first.member, second.member))
    counts = frame_counts(len(sparring.blows))
    gap = edit_gap(len(counts))
    result_shown = False
    for index, shown in enumerate(counts):
        await timing.pause(gap)
        last = index == len(counts) - 1
        outcome = await _edit(message, fight_embed(pair, sparring, shown, winner=winner if last else None))
        if outcome == "gone":
            break
        result_shown = last and outcome == "ok"
    if not result_shown:
        await scene.send(embed=fight_embed(pair, sparring, len(sparring.blows), winner=winner),
                         allowed_mentions=QUIET)


async def fight(scene: "Scene", first: Fighter, second: Fighter, rng: Any = random) -> tuple[Fighter, Fighter]:
    """Two companions fight; returns ``(winner, loser)``.

    The fight is computed first (:func:`simulate`) and shown afterwards.  A
    draw is decided by lot (one more ``rng.randrange(2)``), as the PvP
    tournament does; the fight message says so.
    """
    pair = (first, second)
    sparring = simulate(first.stats, second.stats, rng)
    winner = sparring.winner if sparring.winner is not None else rng.randrange(2)
    log.info("Training: %s (pet %s) vs %s (pet %s): %d blows, %s wins%s.", first.member, first.pet_id,
             second.member, second.pet_id, len(sparring.blows), pair[winner].member,
             " (a draw decided by lot)" if sparring.winner is None else "")
    await _play(scene, pair, sparring, winner)
    return pair[winner], pair[1 - winner]


# -- the event ---------------------------------------------------------------------------

def _entry(fighter: Fighter) -> str:
    return FIGHTER_ENTRY.format(pet=fighter.shown, mention=fighter.member.mention)


def _listed(head: str, entries: Sequence[str]) -> str:
    """``head`` with ``entries`` in place of its only field - as many as fit, the rest as a number."""
    field = head[head.index("{"):head.index("}") + 1]
    shown: list[str] = []
    for index, entry in enumerate(entries):
        if len(head) + len(", ".join([*shown, entry])) > LINE_LIMIT:
            return head.replace(field, ", ".join(shown) + events.ROSTER_MORE.format(count=len(entries) - index))
        shown.append(entry)
    return head.replace(field, ", ".join(shown))


async def _reward(bot: "AlterBot", what: str, member: Any, awaitable: Awaitable[Any]) -> bool:
    """Hand one reward out; a failure is logged and reported to the owner.  True when it worked.

    One broken reward must not cost the other players theirs.
    """
    try:
        await awaitable
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported; the other rewards are still handed out
        log.exception("Training: the reward (%s) of %s failed.", what, member.id)
        await bot.log_to_owner(LOG_TRAINING_REWARD_FAILED.format(
            what=what, player_id=member.id, error=f"{type(exc).__name__}: {exc}"), ping=True)
        return False


async def _say(scene: "Scene", text: str, **kwargs: Any) -> bool:
    """Post a line of the end of the training; a failure is only logged.  Never raises.

    Once the fights are over the rewards do not depend on Discord taking one
    more message: the players have earned them.
    """
    try:
        await scene.send(text, **kwargs)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - only an announcement
        log.warning("Training: a line could not be posted (%s: %s): %r", type(exc).__name__, exc, text)
        return False


async def _hand_out(scene: "Scene", winner: Fighter, finalist: Fighter, fought: Sequence[Fighter]) -> None:
    """The rewards of a training that was fought - called once, when the winner is known."""
    bot = scene.bot
    granted = [await _reward(bot, REWARD_SHARD, fighter.member,
                             pets.assign_shard(scene, FIGHTER_SHARDS, fighter.member.id))
               for fighter in fought]
    granted.append(await _reward(bot, REWARD_SCROLL, finalist.member,
                                 pets.assign_scroll(scene, FINALIST_SCROLLS, finalist.member.id)))
    await _say(scene, MSG_TRAINING_REWARDS.format(winner=winner.member.mention, finalist=finalist.member.mention),
               allowed_mentions=QUIET)
    granted.append(await _reward(bot, REWARD_LOOT, winner.member,
                                 daily.rand_loot(scene, WINNER_LOOT_RARITY, winner.member, WINNER_LOOT_BOOST)))
    if not all(granted):
        await _say(scene, MSG_TRAINING_REWARD_FAILED)


async def spawn_training(scene: "Scene", *, rng: Any = random) -> Any:
    """Play the whole event; returns the member whose companion won (None: no fight took place).

    The caller holds the event flags (``state.running_event``).  ``rng``: the
    source of luck of the fights (tests seed it).
    """
    bot = scene.bot
    log.info("Companion training spawn.")
    choice = pictures()
    if not choice:
        raise PictureMissing(f"no picture of the companion training in {PICTURES_DIR}")
    await scene.send(file_path=random.choice(choice))

    embed = discord.Embed(title=TRAINING_TITLE, description=TRAINING_DESCR, color=TRAINING_COLOUR)
    async with _TrainingSignUp(scene, embed, emoji=PAW) as sign_up:
        # Only while the sign-up is open: the stable works although the event holds the channel.
        with bot.state.stable_stays_open():
            await sign_up.open(events._wait(bot, TRAINING_SIGNUP))
            members = await sign_up.collect()
        fighters, dropped = await _snapshot(bot, members)
    log.info("Companion training: %s fight, %s dropped.",
             [(str(fighter.member), fighter.pet_id) for fighter in fighters], [str(member) for member in dropped])

    if dropped:
        mentions = [member.mention for member in dropped]
        await scene.send(_listed(MSG_TRAINING_DROPPED, mentions), allowed_mentions=QUIET)
    if not fighters:
        await scene.send(MSG_TRAINING_NOBODY)
        return None
    if len(fighters) == 1:
        alone = fighters[0]
        await _say(scene, MSG_TRAINING_ALONE.format(pet=alone.shown, mention=alone.member.mention),
                   allowed_mentions=_ping(alone.member))
        if not await _reward(bot, REWARD_SHARD, alone.member,
                             pets.assign_shard(scene, FIGHTER_SHARDS, alone.member.id)):
            await _say(scene, MSG_TRAINING_REWARD_FAILED)
        return None

    random.shuffle(fighters)
    await scene.send(_listed(MSG_TRAINING_CLOSED, [_entry(fighter) for fighter in fighters]),
                     allowed_mentions=QUIET)

    #: Everybody whose companion really fought (by player; in the order of their first fight).
    fought: dict[int, Fighter] = {}
    finalist = fighters[1]  # (replaced by the loser of every fight; the last fight is the final)
    round_number = 1
    while len(fighters) > 1:
        await scene.send(MSG_TRAINING_ROUND.format(round=round_number))
        next_round: list[Fighter] = []
        if len(fighters) % 2 == 1:
            lucky = fighters.pop()
            next_round.append(lucky)
            await scene.send(MSG_TRAINING_BYE.format(pet=lucky.shown, mention=lucky.member.mention),
                             allowed_mentions=QUIET)
        for index in range(0, len(fighters), 2):
            first, second = fighters[index], fighters[index + 1]
            winner, finalist = await fight(scene, first, second, rng)
            for fighter in (first, second):
                fought.setdefault(int(fighter.member.id), fighter)
            next_round.append(winner)
            await timing.pause(FIGHT_PAUSE)
        fighters = next_round
        round_number += 1

    champion = fighters[0]
    log.info("Companion training won by %s (pet %s); finalist %s.", champion.member, champion.pet_id,
             finalist.member)
    # From here on nothing but a shutdown keeps the rewards from the players (see _say).
    await _say(scene, MSG_TRAINING_WINNER.format(pet=champion.shown, mention=champion.member.mention),
               allowed_mentions=_ping(champion.member))
    await _hand_out(scene, champion, finalist, list(fought.values()))
    return champion.member
