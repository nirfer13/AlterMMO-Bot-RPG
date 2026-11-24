"""The rank "Kolekcjoner Pupili": the role of place 1 of the collection ranking.

Owner's request of 2026-11-23: "Za 1 miejsce w kolekcji dodsj range kolekcjoner pupili, ktora da
dodstkiea szanse na wygrana w giveawayu u horusa" - the player at place 1 of the collection ranking
gets a Discord role ("ranga") named Kolekcjoner Pupili, and that role gives an additional chance in
the giveaways of Horus.

What this module does - and what it does not:

* The giveaway is Horus's (the other bot).  It adds up the weights of a member's roles **by the
  exact role name** from its own ``ranksConfig.json``; the entry for :data:`ROLE_NAME` is added
  there by hand.  This bot only makes sure that a role of exactly that name is on the server and
  that exactly the right member holds it.  (``config/ranksConfig.json`` of this repository is a
  dead legacy file: not read, not written.)
* **Who holds the role**: the member at place 1 of :func:`albumrank.ranking` - the supporters'
  ranking - and nobody else.  Nobody ranked (and the ranking IS known): nobody holds it.  When the
  ranking is not known right now (``None``, the database does not answer), the server is not
  there, the saved state cannot be read or there is no role to hand out, NOTHING is touched and
  the next synchronisation tries again: a wrong removal is worse than a late one.
* It is **not an achievement**.  Achievement roles are never taken away, this one is; so it is
  not in ``achievements.TROPHIES`` (not in ``!osiagniecia``, not in "X z Y"), and its id is saved
  in a file of its own (``data/petCollector.json``, ``bot.store.collector``) - never in
  ``data/achievementRoles.json`` / ``achievementRolesLater.json``, whose unknown keys
  ``achievements.retire_roles`` (of this or of an older version) deletes from the server.

**The role** (:func:`_role_for`): the saved id (the owner may rename, recolour and move the role);
else a role of exactly :data:`ROLE_NAME` that is not managed and carries no permissions is taken
over (the saved id was lost, or the owner made the role by hand - the server is close to
Discord's limit of 250 roles, so making one can fail); else it is made: no permissions, not shown
separately, not mentionable.  Made only when there is somebody to give it to.  A failure is told to
the owner once per run and tried again after :data:`ROLE_RETRY_AFTER` (the start and
``!kolekcjoner`` try at once - also a ``!kolekcjoner`` typed while the boss channel was busy: it
leaves that wish for the run that follows), not at every synchronisation.  That a role was made or
taken over is told to the owner when it happens (with the note about Horus).  One case is
decided here on purpose: when a role of that name exists but cannot be taken over (it carries
permissions or is managed), NO second role of the same name is made - Horus knows the rank by its
name, so two roles of one name would both count in its giveaway; the owner is told what to do
instead.  For the same reason every synchronisation looks for **another role of that name** beside
the bot's own (:func:`_look_for_twins`: a role made twice by a request whose answer was lost, one
made by hand): the owner is told once per run - its id, how many members hold it, why it matters -
and nothing is deleted or taken from anybody automatically.

When ``data/petCollector.json`` cannot be WRITTEN the role is still handed out and taken away (it
is taken over by its name again each time - the note about that is given once per run and role,
not every time), nothing is announced (see below) and the owner is told once per run that the
file cannot be written.

**When** (:func:`request` - the ONE entry point; it only raises a flag, so it costs nothing and
can be called from anywhere): after the start, after an album entry was kept or removed
(``album.saved``, ``album.remove_from_panel``), when a member's support begins or ends, when the
rank was given or taken by hand, when a supporter or the holder leaves the server
(:func:`member_changed`, :func:`member_left` - the Collector cog's listeners), and every
:data:`PERIOD` as a safety net.  The supervised loop :func:`run` answers the flag: a burst of
requests is one run, a request during a run is one more run after it, and :func:`sync` holds a
lock, so two synchronisations never overlap (``!kolekcjoner`` goes through the same lock).
Without the loop (a test, a cog that did not load) a request does nothing at all.  After a run
that could not look (no server, no ranking) the loop tries again after :data:`RETRY_SOON`; after
a run that knew the ranking but could not give the role to the leader or take it from somebody
else (a network failure, or the role lies above the bot's own) it does so too, but at most
:data:`QUICK_RETRIES` times in a row - then it is back to :data:`PERIOD`.  A ``!kolekcjoner``
whose own run ended like that (:func:`sync_now`) leaves a request, so that the loop - which did
not see that run - takes the tries over instead of coming back only with its safety net; its
quick tries are counted anew then, also when the loop had used them up on the same trouble.

**A supporter role that blinks**: when the support of the CURRENT HOLDER ends, the loop waits
:data:`HOLDER_GRACE` before the run that would move the rank - a renewal or a change of tier can
reach the server as "role taken" and, seconds later, "role given", and the rank must not travel to
the runner-up and back (with two public announcements) for that.  The loop waits outside the
lock; whatever else is requested meanwhile neither shortens the wait nor is lost - it is answered
by the one run after it.  Only three things end the wait early: the holder's support is back, the
holder left the server (no grace then), ``!kolekcjoner``.  A real overtake (an album change while
no such wait runs) is answered at once, as before.

Owner's rule "nothing may make a fight stutter": the loop waits while something plays on the boss
channel (:func:`quiet` - ``achievements._in_action`` and a typed fight there; also while the boss
is about to arrive, see the announcement), the Discord calls go through the helpers of
:mod:`altermmo.core.io` (only making the role and sending the announcement are one attempt, never
repeated blindly), the ranking scores in a worker thread, and nothing here holds the event loop.

**The announcement** (owner's rule "what happens once must happen once"): when the member holding
the rank is another one than the last ANNOUNCED one (``announced`` in the saved state), ONE
public message goes to the boss channel, pinging only him - the previous holder is neither pinged
nor named.  The note is written BEFORE the message is sent (a note that cannot be written: nothing
is sent) and the message is sent with ONE attempt: a crash, a restart or a send whose answer was
lost can cost the announcement, never repeat it; only when Discord certainly refused the message
(no permission ...) is the note put back for the next synchronisation.  While the boss is about
to arrive (``BossPhase.PRE_SPAWN``) the channel does not count as quiet: it is cleared at the
spawn, minutes later, and the one announcement would go with it.  What follows from "the last
announced one":

* a restart, a repeated synchronisation, a role that had to be given again: nothing new;
* A is overtaken by B and takes the lead back (A -> B -> A): B was announced, so A's return is
  news and is announced again - three messages in all;
* A is overtaken by B and is back in front before B was ever announced (no synchronisation in
  between, or a fight held it up): nobody saw A lose the rank - nothing is announced;
* A loses the rank and nobody takes it (his support lapsed and he was the only one ranked) and
  then gets it back: nobody else was announced meanwhile - nothing is announced (a support role
  that blinks at a renewal must not fill the channel; with others ranked, the grace period above
  keeps the rank where it is).

When the role cannot be given to the leader (a network failure, or it lies above the bot's own
role), the others still lose it - the ranking says they are not first.  The owner is told once
(the same for a role that cannot be taken away) and not again until a run leaves the leader with
the role / nobody else with it, so a later problem is told anew; the loop tries again soon, see
above.

**Debug runs** (decided here on purpose as well): a debug run plays on the SAME server with the
TEST database (``settings.load_settings``), so its ranking says nothing about who leads there.  It
would take the role from the real holder at every start and - with the production bot running
beside it - the two would hand the role back and forth for ever.  In debug mode nothing is looked
at and nothing is touched (:data:`TEST_RUN`); ``!kolekcjoner`` says so.

Not covered (accepted): while the server's member list is incomplete (the first moments after a
start or a reconnect) a former holder missing from it keeps the role until the next
synchronisation that sees him - late, never wrong.  A lost ``data/petCollector.json`` means the
role is taken over by its name and the current holder is announced once more.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

from ..core import challenge, io
from ..core.state import BossPhase
from . import achievements, albumrank, patrons

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot

log = logging.getLogger(__name__)

#: The role's name.  Horus's ``ranksConfig.json`` lists the rank under exactly this name - the
#: giveaway finds a member's roles BY NAME - so the two must be changed together.
ROLE_NAME = "Kolekcjoner Pupili"
#: Colour of the role when the bot makes it (the owner may change it: the bot knows the role by id).
ROLE_COLOUR = 0xE67E22
ROLE_REASON = "AlterMMO RPG: ranga pierwszego miejsca w rankingu kolekcji"

#: Name of the supervised loop (``bot.supervisor``, shown by ``!status``).
LOOP_NAME = "collector"
#: The safety net: a synchronisation at least this often (seconds), whatever was requested.
PERIOD = 1800.0
#: After a synchronisation that could not look (no server, no ranking), or that could not give
#: the role or take it away: the next try (seconds).
RETRY_SOON = 120.0
#: So many quick tries in a row after runs that could not give the role or take it away; after
#: them the loop is back to :data:`PERIOD` (a role above the bot's own must not poll for ever).
QUICK_RETRIES = 3
#: How long the loop waits before the run that follows the END of the current holder's support
#: (seconds): a renewal / a change of tier may take the supporter role and give it back.
HOLDER_GRACE = 60.0
#: While that wait runs the loop looks this often whether it was called off (seconds).
GRACE_POLL = 1.0
#: How long the loop lets requests gather before it runs (seconds): one change on the server is
#: several events, and each of them asks.
SETTLE = 2.0
#: The loop waits while something plays on the boss channel (seconds between looks).
QUIET_POLL = 15.0
#: After the role could not be made, it is not tried again on its own for this long (seconds).
ROLE_RETRY_AFTER = 3600.0
#: How long a role the bot has just made counts as being on the server although Discord's cache
#: does not show it yet (seconds), as in features/achievements.py.
FRESH_ROLE_SECONDS = 120.0
#: Longest time the one attempt to make the role may take (never repeated blindly: a request that
#: timed out may have been carried out - the role is then taken over by its name later).
CREATE_TIMEOUT = io.ATTEMPT_TIMEOUT

# -- the saved state (``data/petCollector.json``) -------------------------------------------------

#: The id of the role.
KEY_ROLE = "role_id"
#: The id of the member whose rank was announced last (see the module docstring).
KEY_ANNOUNCED = "announced"

# -- texts (Polish) -------------------------------------------------------------------------------

#: The public announcement on the boss channel: only the new holder is pinged, nobody else named.
MSG_ANNOUNCE = ("👑 <@{player_id}> ma teraz najcenniejszą kolekcję towarzyszy i zdobywa rangę "
                "**" + ROLE_NAME + "**! Ranga należy do pierwszego miejsca w rankingu kolekcji i daje "
                "dodatkową szansę w giveawayach.")
#: The line of the collection ranking's embed (a field: its name and its text; no number, no
#: ornament is named).
MSG_RANKING_NAME = "👑 " + ROLE_NAME
MSG_RANKING_LINE = "Ranga pierwszego miejsca w tym rankingu - daje dodatkową szansę w giveawayach."

_HORUS_NOTE = ("Uwaga: giveaway Horusa rozpoznaje tę rangę **po nazwie** roli. Jeśli zmienisz nazwę roli na "
               "serwerze, zmień ją tak samo w `ranksConfig.json` Horusa - inaczej ranga przestanie dawać "
               "dodatkową szansę. Kolor i miejsce roli możesz zmieniać dowolnie (trzymaj ją poniżej roli "
               "bota, inaczej bot jej nie nada).")
LOG_ROLE_MADE = ("Utworzyłem rolę **" + ROLE_NAME + "** - rangę pierwszego miejsca w rankingu kolekcji. "
                 + _HORUS_NOTE)
LOG_ROLE_ADOPTED = ("Przejąłem istniejącą na serwerze rolę **" + ROLE_NAME + "** - od teraz to ranga pierwszego "
                    "miejsca w rankingu kolekcji i bot sam ją nadaje i zdejmuje. " + _HORUS_NOTE)
LOG_ROLE_FAILED = ("Nie udało się utworzyć roli **" + ROLE_NAME + "** (`{error}`) - serwer może mieć już 250 "
                   "ról albo bot nie ma uprawnienia \"Zarządzanie rolami\". Możesz utworzyć ją ręcznie: rola o "
                   "dokładnie tej nazwie, bez żadnych uprawnień, poniżej roli bota - bot przejmie ją sam w "
                   "ciągu godziny albo od razu po **!kolekcjoner**. Do tego czasu rangi nikt nie dostaje.")
LOG_ROLE_TWIN = ("Na serwerze jest już rola o nazwie **" + ROLE_NAME + "**, ale ma uprawnienia albo jest "
                 "zarządzana przez integrację, więc jej nie przejmuję - i nie tworzę drugiej o tej samej nazwie "
                 "(Horus rozpoznaje rangę po nazwie, liczyłyby się obie). Zdejmij z niej wszystkie uprawnienia "
                 "i wpisz **!kolekcjoner**. Do tego czasu rangi nikt nie dostaje.")
#: Another role of the rank's name beside the bot's own (never removed automatically).
LOG_SECOND_ROLE = ("Na serwerze jest więcej niż jedna rola o nazwie **" + ROLE_NAME + "**. Bot nadaje i zdejmuje "
                   "tylko rolę `{role_id}`; oprócz niej jest: {twins}. Horus rozpoznaje rangę **po nazwie**, więc "
                   "w jego giveawayu liczy się każda rola o tej nazwie - kto ma tę drugą, ma dodatkową szansę bez "
                   "pierwszego miejsca w rankingu kolekcji (a rola zajmuje miejsce na liście ról serwera). "
                   "Niczego sam nie usuwam ani nikomu nie zdejmuję: usuń zbędną rolę albo zmień jej nazwę.")
LOG_SECOND_ROLE_ITEM = "rola `{role_id}` (posiadaczy: {holders})"
LOG_FILE_UNREADABLE = ("Nie mogę odczytać pliku rangi **" + ROLE_NAME + "** (`{path}`). Nikomu jej nie nadaję "
                       "ani nie zdejmuję, żeby nie narobić szkód - sprawdź plik i wpisz **!kolekcjoner**.")
LOG_FILE_UNWRITABLE = ("Nie mogę zapisać pliku rangi **" + ROLE_NAME + "** (`{path}`). Rangę nadaję i zdejmuję "
                       "dalej, ale nowego posiadacza nie ogłaszam na kanale bossa, dopóki pliku nie da się "
                       "zapisać (inaczej ogłoszenie mogłoby się powtarzać). Sprawdź dysk i uprawnienia do pliku.")
#: (Worded neutrally: ``io.add_role`` / ``io.remove_role`` do not say why they failed.)
_NOT_CHANGED_WHY = ("To może być chwilowy problem z połączeniem z Discordem albo rola leży powyżej roli bota "
                    "(wtedy przesuń ją poniżej). Próbuję ponownie sam; nie piszę o tym więcej, dopóki się nie "
                    "uda.")
LOG_NOT_GIVEN = ("Nie udało się nadać rangi **" + ROLE_NAME + "** graczowi <@{player_id}>. " + _NOT_CHANGED_WHY)
LOG_NOT_REMOVED = ("Nie udało się zdjąć rangi **" + ROLE_NAME + "** z gracza <@{player_id}>, który nie jest już "
                   "pierwszy w rankingu kolekcji. " + _NOT_CHANGED_WHY)
LOG_NOT_ANNOUNCED = ("Nie udało się ogłosić na kanale bossa, że rangę **" + ROLE_NAME + "** ma teraz "
                     "<@{player_id}> (`{error}`). Rangę ma; {later}")
LOG_ANNOUNCE_AGAIN = "spróbuję ogłosić to przy następnej synchronizacji."
LOG_ANNOUNCE_LOST = "nie ogłaszam ponownie, żeby wiadomość nie pojawiła się dwa razy."

# -- what ``!kolekcjoner`` answers ----------------------------------------------------------------

MSG_BUSY = ("Na kanale bossa coś się teraz dzieje albo zaraz pojawi się boss - rangę **" + ROLE_NAME
            + "** sprawdzę sam, gdy tylko będzie tam spokój.")
MSG_TEST_RUN = ("Tryb testowy: ranking kolekcji pochodzi tu z testowej bazy danych, więc rangi **" + ROLE_NAME
                + "** nie ruszam - nadaje ją i zdejmuje tylko bot uruchomiony produkcyjnie.")
MSG_NO_GUILD = "Serwer jest chwilowo niedostępny - niczego nie zmieniłem, spróbuję ponownie sam."
MSG_NO_STATE = ("Nie mogę odczytać pliku rangi (`data/petCollector.json`) - niczego nie zmieniłem. Szczegóły "
                "są na kanale logów.")
MSG_NO_RANKING = ("Ranking kolekcji jest chwilowo nieznany (baza danych albo lista członków serwera nie "
                  "odpowiada{detail}) - niczego nie zmieniłem, spróbuję ponownie sam.")
MSG_NO_ROLE = ("Nie ma roli **" + ROLE_NAME + "** i nie mogłem jej teraz utworzyć ani przejąć - niczego nie "
               "zmieniłem. Co zrobić, napisałem na kanale logów.")
MSG_FAILED = "Synchronizacja rangi nie powiodła się (`{detail}`) - szczegóły są w logu bota."
MSG_HOLDER = "Rangę **" + ROLE_NAME + "** ma <@{player_id}> - pierwsze miejsce w rankingu kolekcji."
MSG_HOLDER_UNCHANGED = ("Bez zmian: rangę **" + ROLE_NAME + "** ma <@{player_id}> - pierwsze miejsce w rankingu "
                        "kolekcji.")
MSG_NOBODY = "Nikogo nie ma teraz w rankingu kolekcji, więc rangi **" + ROLE_NAME + "** nikt nie ma."
MSG_NOBODY_UNCHANGED = ("Bez zmian: nikogo nie ma teraz w rankingu kolekcji, więc rangi **" + ROLE_NAME
                        + "** nikt nie ma.")
MSG_NOT_GIVEN = ("Pierwsze miejsce w rankingu kolekcji ma <@{player_id}>, ale nie udało się nadać mu rangi **"
                 + ROLE_NAME + "** - to chwilowy problem z połączeniem albo rola leży powyżej roli bota. "
                 "Spróbuję ponownie sam.")
MSG_GIVEN = "Nadałem ją teraz."
MSG_REMOVED = "Zdjąłem ją z: {players}."
MSG_NOT_REMOVED = "Nie udało się zdjąć jej z: {players}."
MSG_ANNOUNCED = "Ogłosiłem to na kanale bossa."
MSG_ANNOUNCE_LATER = "Ogłoszę to na kanale bossa, gdy będzie tam spokój."
MSG_ANNOUNCE_FAILED = "Ogłoszenie na kanale bossa się nie udało (szczegóły na kanale logów)."


# -- what a synchronisation found -----------------------------------------------------------------

#: The ranking was known and the role is where it belongs (or could not be put there: ``given`` /
#: ``not_removed`` tell).
DONE = "done"
#: Nothing was looked at: something plays on the boss channel (``!kolekcjoner`` only).
BUSY = "busy"
#: Nothing was looked at: a debug run, whose database is the test one (see the module docstring).
TEST_RUN = "test-run"
#: Nothing was touched, because ...
NO_GUILD = "no-guild"        # ... the server is not there,
NO_STATE = "no-state"        # ... the saved state cannot be read,
NO_RANKING = "no-ranking"    # ... the ranking is not known right now,
NO_ROLE = "no-role"          # ... there is no role and none could be made or taken over,
FAILED = "failed"            # ... something unexpected went wrong (logged).

#: The announcement of a synchronisation: sent now / waits for a quiet channel / could not be sent.
ANNOUNCED, LATER, NOT_ANNOUNCED = "announced", "later", "failed"


@dataclass(frozen=True)
class Outcome:
    """What one synchronisation did (``!kolekcjoner`` answers with it, :func:`describe`)."""

    status: str
    #: Place 1 of the ranking (None: nobody is ranked, or nothing was looked at).
    leader_id: int | None = None
    #: True when the leader holds the role now.
    holds: bool = False
    #: True when the role was given in this run.
    given: bool = False
    #: Who lost the role in this run, and who should have but could not be changed.
    removed: tuple[int, ...] = ()
    not_removed: tuple[int, ...] = ()
    #: :data:`ANNOUNCED` / :data:`LATER` / :data:`NOT_ANNOUNCED`; "" when there was nothing to announce.
    announcement: str = ""
    detail: str = ""

    @property
    def looked(self) -> bool:
        """True when the ranking was read and acted on."""
        return self.status == DONE

    @property
    def changed(self) -> bool:
        return self.given or bool(self.removed) or self.announcement == ANNOUNCED

    @property
    def try_soon(self) -> bool:
        """True when the run could not look for a reason that passes by itself."""
        return self.status in (NO_GUILD, NO_RANKING, FAILED)

    @property
    def unfinished(self) -> bool:
        """True when the ranking was known but the role is not where it belongs: it could not be
        given to the leader or not taken from somebody else (the loop tries again soon, a few times)."""
        return self.status == DONE and ((self.leader_id is not None and not self.holds) or bool(self.not_removed))


def _mentions(player_ids: Any) -> str:
    return ", ".join(f"<@{player_id}>" for player_id in player_ids)


def describe(outcome: Outcome) -> str:
    """What ``!kolekcjoner`` answers (sent without pinging anybody)."""
    status = outcome.status
    if status == BUSY:
        return MSG_BUSY
    if status == TEST_RUN:
        return MSG_TEST_RUN
    if status == NO_GUILD:
        return MSG_NO_GUILD
    if status == NO_STATE:
        return MSG_NO_STATE
    if status == NO_RANKING:
        return MSG_NO_RANKING.format(detail=f": {outcome.detail}" if outcome.detail else "")
    if status == NO_ROLE:
        return MSG_NO_ROLE
    if status != DONE:
        return MSG_FAILED.format(detail=outcome.detail or status)
    if outcome.leader_id is None:
        parts = [MSG_NOBODY if outcome.changed or outcome.not_removed else MSG_NOBODY_UNCHANGED]
    elif not outcome.holds:
        parts = [MSG_NOT_GIVEN.format(player_id=outcome.leader_id)]
    else:
        quiet_run = not outcome.changed and not outcome.not_removed and not outcome.announcement
        parts = [(MSG_HOLDER_UNCHANGED if quiet_run else MSG_HOLDER).format(player_id=outcome.leader_id)]
        if outcome.given:
            parts.append(MSG_GIVEN)
    if outcome.removed:
        parts.append(MSG_REMOVED.format(players=_mentions(outcome.removed)))
    if outcome.not_removed:
        parts.append(MSG_NOT_REMOVED.format(players=_mentions(outcome.not_removed)))
    if outcome.announcement:
        parts.append({ANNOUNCED: MSG_ANNOUNCED, LATER: MSG_ANNOUNCE_LATER}.get(outcome.announcement,
                                                                             MSG_ANNOUNCE_FAILED))
    return " ".join(parts)


# -- the state of this run (memory) ---------------------------------------------------------------

@dataclass
class _Desk:
    """What one running bot keeps for the rank (``bot._collector``; nothing here survives a restart)."""

    #: Raised by :func:`request`, answered by :func:`run`.
    wanted: asyncio.Event = field(default_factory=asyncio.Event)
    #: One synchronisation at a time (:func:`sync`).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Owner notes already given in this run.
    told: set = field(default_factory=set)
    #: The ids of the other roles of the rank's name the owner has been told about (and that
    #: were still there at the last synchronisation).
    twins: frozenset = frozenset()
    #: True when the loop's next run is to try making the role at once: the start, and a
    #: ``!kolekcjoner`` that found the boss channel busy.  Used and cleared by the loop.
    force_next: bool = False
    #: Not before this moment (``_clock``) does the loop run: the current holder's support ended
    #: (:data:`HOLDER_GRACE`).  0.0: no such wait.
    grace_until: float = 0.0
    #: Not before this moment (``_clock``) is the role made again after a failure.
    create_retry_at: float = 0.0
    #: The role made a moment ago and when: Discord's cache learns about a new role late.
    fresh: tuple[Any, float] | None = None
    #: The id of the role and of the member the last synchronisation left it with (None: nobody,
    #: or not synchronised yet) - what the member events are compared with.
    role_id: int | None = None
    holder_id: int | None = None
    #: The loop's quick tries in a row after runs that left the role in the wrong hands
    #: (:data:`QUICK_RETRIES`).  Counted by the loop; a ``!kolekcjoner`` that could not finish
    #: sets it back (:func:`sync_now`), so that its promise is kept with a full round of tries.
    quick: int = 0
    #: How many synchronisations ran, and what the last one found.
    runs: int = 0
    last: Outcome | None = None


def _desk(bot: Any) -> _Desk:
    desk = getattr(bot, "_collector", None)
    if not isinstance(desk, _Desk):
        desk = _Desk()
        bot._collector = desk
    return desk


def _clock() -> float:
    return time.monotonic()


def _told_once(bot: Any, what: Any) -> bool:
    """True the first time ``what`` is reported in this run (the owner is not told again and again)."""
    told = _desk(bot).told
    if what in told:
        return False
    told.add(what)
    return True


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_role(member: Any, role_id: int | None) -> bool:
    return bool(role_id) and any(int(role.id) == int(role_id) for role in getattr(member, "roles", None) or [])


# -- the saved state ------------------------------------------------------------------------------

def _read_state(bot: Any) -> dict[str, Any] | None:
    """The saved state; {} when nothing was saved yet, None when the file cannot be read.

    The difference matters, as for the achievements' roles: "nothing saved" lets the bot take a
    role over or make one and announce the holder; an unreadable file must not (a second role
    would be made, the holder announced a second time).
    """
    return achievements._read_mapping(bot.store.collector.path)


def _save_state(bot: Any, state: dict[str, Any]) -> bool:
    """Write the state; False (and a log line) when the file cannot be written.  Never raises."""
    try:
        bot.store.collector.save(state)
        return True
    except Exception:  # noqa: BLE001 - the caller decides what an unsaved note means
        log.exception("Could not save %s.", bot.store.collector.path)
        return False


async def _keep(bot: Any, state: dict[str, Any]) -> bool:
    """:func:`_save_state`, and the owner is told - once per run - when the file cannot be written.

    (Told again when a file that could be written in between cannot be written once more.)
    """
    if _save_state(bot, state):
        _desk(bot).told.discard("state-write")
        return True
    if _told_once(bot, "state-write"):
        await bot.log_to_owner(LOG_FILE_UNWRITABLE.format(path=bot.store.collector.path), ping=True)
    return False


# -- the role -------------------------------------------------------------------------------------

def _known_role(bot: Any, guild: Any, role_id: int) -> Any:
    """The role of a saved id (also one made a moment ago); None when it is not on the server.

    A role the bot made counts without the cache only for :data:`FRESH_ROLE_SECONDS` and only
    until the cache has seen it: a role deleted on the server later must read as gone.
    """
    if not role_id:
        return None
    desk = _desk(bot)
    cached = guild.get_role(role_id)
    fresh = desk.fresh
    if cached is not None:
        if fresh is not None and int(fresh[0].id) == role_id:
            desk.fresh = None
        return cached
    if fresh is not None and int(fresh[0].id) == role_id:
        if _clock() - fresh[1] <= FRESH_ROLE_SECONDS:
            return fresh[0]
        desk.fresh = None
    return None


def _named(role: Any) -> bool:
    return getattr(role, "name", None) == ROLE_NAME


def _adoptable(role: Any) -> bool:
    """A role of exactly our name that is clearly nobody else's: not managed, no permissions."""
    return (_named(role) and not getattr(role, "managed", False)
            and getattr(getattr(role, "permissions", None), "value", 0) == 0)


async def _remember_role(bot: Any, state: dict[str, Any], role: Any, how: str, note: str) -> None:
    """Save the id of the role made or taken over and tell the owner (``note``).

    Saved, the role is found by its id from now on, so the note is given once by itself.  While
    the file cannot be written the role is taken over by its name again at every synchronisation:
    then the note goes through the told-once memory, keyed by the role's id - once per run and
    role, not every time.
    """
    desk = _desk(bot)
    role_id = int(role.id)
    state[KEY_ROLE] = role_id
    saved = await _keep(bot, state)  # at once; unsaved, the role is taken over by its name the next time
    desk.told.discard("role-failed")
    desk.told.discard("role-twin")
    log.info("Collector role %r: %s (%s).", ROLE_NAME, how, role_id)
    if _told_once(bot, ("role-known", role_id)):
        await bot.log_to_owner(note)
    if saved:  # (a role made or taken over anew later in this run is news again)
        desk.told.discard(("role-known", role_id))


async def _role_for(bot: "AlterBot", guild: Any, state: dict[str, Any], *, make: bool, force: bool) -> Any:
    """The role to hand out: the saved one, one taken over by its name or - with ``make`` - a new one.

    None when there is none (with ``make``: it could not be made, the owner has been told).  A
    role taken over or made is saved into ``state`` at once and told to the owner
    (:func:`_remember_role`).  ``force``: make it even within :data:`ROLE_RETRY_AFTER` of a failure.
    """
    desk = _desk(bot)
    role = _known_role(bot, guild, _number(state.get(KEY_ROLE)))
    if role is not None:
        return role
    named = [found for found in getattr(guild, "roles", None) or [] if _named(found)]
    role = next((found for found in named if _adoptable(found)), None)
    if role is not None:
        await _remember_role(bot, state, role, "taken over", LOG_ROLE_ADOPTED)
        return role
    if named:  # a role of our name that is not ours to take: never a second one of the same name
        log.warning("A role named %r exists but carries permissions or is managed; not taken over, none made.",
                    ROLE_NAME)
        if _told_once(bot, "role-twin"):
            await bot.log_to_owner(LOG_ROLE_TWIN, ping=True)
        return None
    if not make:
        return None
    if not force and _clock() < desk.create_retry_at:
        return None
    try:
        role = await asyncio.wait_for(guild.create_role(
            name=ROLE_NAME, colour=discord.Colour(ROLE_COLOUR), hoist=False, mentionable=False,
            permissions=discord.Permissions.none(), reason=ROLE_REASON), CREATE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - no permission, 250 roles, Discord is down
        log.warning("Cannot make the role %r: %s: %s", ROLE_NAME, type(exc).__name__, exc)
        desk.create_retry_at = _clock() + ROLE_RETRY_AFTER
        if _told_once(bot, "role-failed"):
            await bot.log_to_owner(LOG_ROLE_FAILED.format(error=type(exc).__name__), ping=True)
        return None
    desk.create_retry_at = 0.0
    if guild.get_role(int(role.id)) is None:  # (the cache has not heard of it yet)
        desk.fresh = (role, _clock())
    await _remember_role(bot, state, role, "made", LOG_ROLE_MADE)
    return role


async def _look_for_twins(bot: "AlterBot", guild: Any, role: Any) -> None:
    """Tell the owner about every OTHER role of the rank's name - once per run and role.

    Horus adds up a member's roles by their name, so a second role of this name counts in its
    giveaway like the bot's own, whoever holds it (and it takes one of the server's 250 role
    places).  How one comes about: a request to make the role whose answer was lost and that was
    repeated or made up for by hand, a role made by hand beside the bot's.  Looked for at every
    synchronisation, whichever way the bot's role was found.  Nothing is deleted and nobody loses
    a role here: that is the owner's to decide.  A twin that went away and is back is told again.
    """
    desk = _desk(bot)
    role_id = int(role.id)
    twins = [found for found in getattr(guild, "roles", None) or [] if _named(found) and int(found.id) != role_id]
    seen = frozenset(int(found.id) for found in twins)
    news = seen - desk.twins
    desk.twins = seen
    if not news:
        return
    log.warning("Collector: other roles named %r beside the bot's (%s): %s.", ROLE_NAME, role_id, sorted(seen))
    listed = ", ".join(
        LOG_SECOND_ROLE_ITEM.format(role_id=found.id, holders=len(list(getattr(found, "members", None) or [])))
        for found in twins)
    await bot.log_to_owner(LOG_SECOND_ROLE.format(role_id=role_id, twins=listed), ping=True)


# -- the announcement -----------------------------------------------------------------------------

def quiet(bot: Any) -> bool:
    """True when nothing plays on the boss channel (owner's rule: nothing may make a fight stutter)
    and the boss is not about to arrive.

    ``BossPhase.PRE_SPAWN``: the boss comes in a few minutes and the channel is cleared at the
    spawn - the one announcement would be deleted at once, so the loop waits here (its quiet poll)
    as it does during a fight.
    """
    return (not achievements._in_action(bot) and bot.state.boss_phase != BossPhase.PRE_SPAWN
            and not challenge.fight_on(bot.get_channel(bot.ids.boss_channel)))


async def _announce(bot: "AlterBot", state: dict[str, Any], player_id: int) -> str:
    """Tell the boss channel once who holds the rank now; see the module docstring.  Never raises.

    The note (``announced``) is written first: unsaved, nothing is sent (and the owner is told
    that the file cannot be written).  The message is sent with ONE attempt - ``io.send`` would
    repeat a send whose answer was lost, and the message would be there twice.  A send Discord
    certainly refused puts the note back (the next synchronisation tries again); a send that
    failed for a network reason may have arrived, so the note stays and nothing is repeated.
    """
    if not quiet(bot):
        request(bot, "the announcement waits for a quiet channel")
        return LATER
    channel = bot.get_channel(bot.ids.boss_channel)
    if channel is None:
        log.warning("Collector: the boss channel is not there; %s is announced later.", player_id)
        return NOT_ANNOUNCED
    before = state.get(KEY_ANNOUNCED)
    state[KEY_ANNOUNCED] = int(player_id)
    if not await _keep(bot, state):
        state[KEY_ANNOUNCED] = before
        return NOT_ANNOUNCED
    try:
        await io.send(channel, MSG_ANNOUNCE.format(player_id=player_id), attempts=1,
                      allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                                               users=[discord.Object(id=int(player_id))]))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:200]
        if io.is_transient(exc):
            log.warning("Collector: announcing %s failed (%s); it may have arrived - not repeated.",
                        player_id, error)
            later = LOG_ANNOUNCE_LOST
        else:
            log.warning("Collector: announcing %s was refused (%s); tried again at the next synchronisation.",
                        player_id, error)
            state[KEY_ANNOUNCED] = before
            await _keep(bot, state)
            later = LOG_ANNOUNCE_AGAIN
        if _told_once(bot, ("not-announced", int(player_id))):
            await bot.log_to_owner(LOG_NOT_ANNOUNCED.format(player_id=player_id, error=error, later=later))
        return NOT_ANNOUNCED
    log.info("Collector: %s announced as the holder of %r.", player_id, ROLE_NAME)
    return ANNOUNCED


# -- one synchronisation --------------------------------------------------------------------------

async def _sync(bot: "AlterBot", *, force: bool) -> Outcome:
    """Put the role where the ranking says; see the module docstring for what stops it."""
    desk = _desk(bot)
    if getattr(bot, "debug", False):  # the test database knows nothing about the server's leader
        return Outcome(TEST_RUN)
    guild = bot.get_guild(bot.ids.guild)
    if guild is None or getattr(guild, "unavailable", False):
        return Outcome(NO_GUILD)
    state = _read_state(bot)
    if state is None:
        if _told_once(bot, "state-file"):
            await bot.log_to_owner(LOG_FILE_UNREADABLE.format(path=bot.store.collector.path), ping=True)
        return Outcome(NO_STATE)
    desk.told.discard("state-file")
    try:
        board = await albumrank.ranking(bot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the database does not answer: nothing is touched
        log.warning("Collector: the ranking could not be made (%s: %s); nothing is touched.",
                    type(exc).__name__, exc)
        return Outcome(NO_RANKING, detail=type(exc).__name__)
    if board is None:
        log.info("Collector: the ranking is not known right now; nothing is touched.")
        return Outcome(NO_RANKING)
    leader = board.standings[0] if board.standings else None
    leader_id = None if leader is None else int(leader.player_id)

    role = await _role_for(bot, guild, state, make=leader is not None, force=force)
    if role is None:
        desk.role_id = _number(state.get(KEY_ROLE)) or None
        if leader is None:  # nobody to give it to and no role anybody could hold
            desk.holder_id = None
            return Outcome(DONE)
        return Outcome(NO_ROLE, leader_id=leader_id)
    role_id = int(role.id)
    desk.role_id = role_id
    await _look_for_twins(bot, guild, role)
    wrong = [member for member in list(getattr(role, "members", None) or []) if int(member.id) != leader_id]

    holds = given = False
    if leader is not None:
        # (Noted before the call: the event of this very change must not ask for another run.)
        desk.holder_id = leader_id
        if _has_role(leader.member, role_id):
            holds = True
        elif await io.add_role(leader.member, role, reason=ROLE_REASON):
            holds = given = True
            log.info("Collector: %r given to %s (place 1 of the collection ranking).", ROLE_NAME, leader_id)
        else:
            desk.holder_id = None
            if _told_once(bot, ("not-given", role_id)):
                await bot.log_to_owner(LOG_NOT_GIVEN.format(player_id=leader_id), ping=True)
    else:
        desk.holder_id = None
    if holds:  # (the leader has it: a later failure is news for the owner again)
        desk.told.discard(("not-given", role_id))

    removed: list[int] = []
    not_removed: list[int] = []
    for member in wrong:
        member_id = int(member.id)
        if await io.remove_role(member, role, reason=ROLE_REASON):
            removed.append(member_id)
            log.info("Collector: %r taken from %s (not place 1 of the collection ranking).", ROLE_NAME, member_id)
        else:
            not_removed.append(member_id)
            if _told_once(bot, ("not-removed", role_id)):
                await bot.log_to_owner(LOG_NOT_REMOVED.format(player_id=member_id), ping=True)
    if not not_removed:  # (nobody is left who should not have it: a later failure is news again)
        desk.told.discard(("not-removed", role_id))

    announcement = ""
    if holds and _number(state.get(KEY_ANNOUNCED)) != leader_id:
        announcement = await _announce(bot, state, leader_id)
    return Outcome(DONE, leader_id=leader_id, holds=holds, given=given, removed=tuple(removed),
                   not_removed=tuple(not_removed), announcement=announcement)


async def sync(bot: "AlterBot", *, force: bool = False) -> Outcome:
    """One synchronisation, one at a time (a second caller waits for the first).  Never raises.

    ``force``: a role that could not be made is tried again at once (the start, ``!kolekcjoner``).
    Does NOT wait for a quiet boss channel - the loop (:func:`run`) and :func:`sync_now` look
    first; an announcement that finds the channel busy after all is left for the next run.
    """
    desk = _desk(bot)
    async with desk.lock:
        try:
            outcome = await _sync(bot, force=force)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the game goes on; the safety net tries again
            log.exception("Collector: the synchronisation failed.")
            outcome = Outcome(FAILED, detail=type(exc).__name__)
        desk.runs += 1
        desk.last = outcome
    return outcome


async def sync_now(bot: "AlterBot") -> Outcome:
    """``!kolekcjoner``: synchronise now and say what happened.  Never raises.

    While something plays on the boss channel nothing is done now (:data:`BUSY`): the request is
    left for the loop, which runs it once the channel is quiet - and tries to make the role at
    once then, as this command would have (``force_next``).  The command is the owner's word: it
    does not wait for the grace period of a holder whose support ended, and ends it.

    A run that ends with something the loop would try again soon (it could not look -
    ``try_soon`` - or left the role in the wrong hands - ``unfinished``) leaves a request for
    the loop: the answer promises "spróbuję ponownie sam", and the loop, which did not see this
    run, would otherwise come back only with its safety net (:data:`PERIOD`).  The loop answers
    the request like any other and from there on follows its own rule (:data:`RETRY_SOON`) - with
    its quick tries counted anew: a loop that had already used them up on the same trouble would
    answer the request once, moments later, and then be silent for :data:`PERIOD` after all.
    (One round of at most :data:`QUICK_RETRIES` tries per command; nothing polls for ever.)
    """
    desk = _desk(bot)
    if not quiet(bot):
        desk.force_next = True
        request(bot, "!kolekcjoner while the boss channel is busy")
        return Outcome(BUSY)
    desk.wanted.clear()  # (what was asked for so far is done by this run)
    desk.force_next = False
    desk.grace_until = 0.0
    outcome = await sync(bot, force=True)
    if outcome.try_soon or outcome.unfinished:
        desk.quick = 0  # (the owner asked: the loop's quick tries begin anew)
        request(bot, f"!kolekcjoner ended with {outcome.status}: to be tried again")
    return outcome


# -- when ------------------------------------------------------------------------------------------

def request(bot: Any, why: str = "") -> None:
    """Ask for a synchronisation - the one entry point of everything that may change the holder.

    Only raises a flag for the loop (:func:`run`): any number of requests before the loop runs
    is ONE run, and it can be called from any game flow at no cost.  Never raises.
    """
    if bot is None:
        return
    try:
        desk = _desk(bot)
        if not desk.wanted.is_set():
            log.debug("Collector: a synchronisation is requested (%s).", why or "no reason given")
        desk.wanted.set()
    except Exception:  # noqa: BLE001 - a game flow must never fail over the rank
        log.exception("Collector: could not request a synchronisation (%s).", why)


def pending(bot: Any) -> bool:
    """True while a requested synchronisation has not been started yet."""
    return _desk(bot).wanted.is_set()


async def run(bot: "AlterBot") -> None:
    """The supervised loop: synchronise after the start, on request and every :data:`PERIOD`.

    One run at a time; requests gather for :data:`SETTLE`, while the grace period of a holder
    whose support ended runs (:data:`HOLDER_GRACE`, see :func:`member_changed`) and while
    something plays on the boss channel (:func:`quiet`), then ONE run answers them all.  All the
    waiting is done here, outside the lock of :func:`sync`.  A request that comes during a run is
    answered by one more run.  After a run that could not look (no server, no ranking) the next
    one comes after :data:`RETRY_SOON` instead of :data:`PERIOD`; so it does after a run that
    could not give the role or take it away, at most :data:`QUICK_RETRIES` times in a row
    (``desk.quick``; a ``!kolekcjoner`` that could not finish starts that count anew).
    """
    await bot.wait_until_ready()
    desk = _desk(bot)
    request(bot, "start-up")
    desk.force_next = True  # (the first run of a start tries the role at once, whatever failed before)
    patience = PERIOD
    desk.quick = 0  # quick tries in a row after runs that left the role in the wrong hands
    while True:
        try:
            await asyncio.wait_for(desk.wanted.wait(), patience)
        except asyncio.TimeoutError:
            pass  # the safety net
        await asyncio.sleep(SETTLE)
        while True:
            grace = desk.grace_until - _clock()
            if grace > 0:  # (in short steps: the wait can be called off or begin anew meanwhile)
                await asyncio.sleep(min(grace, GRACE_POLL))
            elif not quiet(bot):
                await asyncio.sleep(QUIET_POLL)
            else:
                break
        desk.wanted.clear()
        desk.grace_until = 0.0
        force, desk.force_next = desk.force_next, False
        outcome = await sync(bot, force=force)
        if outcome.try_soon:
            patience = RETRY_SOON
        elif outcome.unfinished and desk.quick < QUICK_RETRIES:
            desk.quick += 1
            patience = RETRY_SOON
        else:
            patience = PERIOD
        if outcome.looked and not outcome.unfinished:
            desk.quick = 0


def member_changed(bot: Any, before: Any, after: Any) -> bool:
    """``on_member_update``: ask for a synchronisation when it may change the holder; True when asked.

    That is: the member's support began or ended (he enters or leaves the ranking), or the rank
    was given to / taken from somebody by hand so that it disagrees with what the last
    synchronisation left (the bot's own changes agree with it and ask for nothing).  Never raises.

    The END of the support of the CURRENT HOLDER begins a grace period (:data:`HOLDER_GRACE`):
    the loop runs only after it, so a supporter role that is taken and given back a moment later
    (a renewal, a change of tier) does not send the rank to the runner-up and back.  Support
    that is back ends the wait.  Requests for other reasons do not touch it.
    """
    try:
        if getattr(getattr(after, "guild", None), "id", None) != bot.ids.guild:
            return False
        desk = _desk(bot)
        supports = patrons.check_if_patron(after)
        if patrons.check_if_patron(before) != supports:
            if desk.holder_id == int(after.id):
                desk.grace_until = 0.0 if supports else _clock() + HOLDER_GRACE
            request(bot, f"support of {after.id} changed")
            return True
        has = _has_role(after, desk.role_id)
        if _has_role(before, desk.role_id) != has and has != (desk.holder_id == int(after.id)):
            request(bot, f"the rank of {after.id} was changed by hand")
            return True
    except Exception:  # noqa: BLE001 - an event handler must never fail over the rank
        log.exception("Collector: a member update could not be looked at.")
    return False


def member_left(bot: Any, member: Any) -> bool:
    """``on_member_remove``: ask for a synchronisation when a supporter or the holder left; True when asked.

    (A supporter who leaves is out of the ranking - when he was first, the rank moves on.)  A
    holder who left gets no grace period: one that runs for him is ended.  Never raises.
    """
    try:
        if getattr(getattr(member, "guild", None), "id", None) != bot.ids.guild:
            return False
        desk = _desk(bot)
        if desk.holder_id == int(member.id):
            desk.grace_until = 0.0
        if (patrons.check_if_patron(member) or _has_role(member, desk.role_id)
                or desk.holder_id == int(member.id)):
            request(bot, f"{member.id} left the server")
            return True
    except Exception:  # noqa: BLE001
        log.exception("Collector: a member who left could not be looked at.")
    return False
