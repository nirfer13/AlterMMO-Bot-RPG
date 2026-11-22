"""What the bot says about a recorded fight: the answer to "lag" and ``$walka``.

Owner's request (2026-11-22): "Jeśli ktoś napisze po takiej walce lag, lagi albo
inne odmiany wyślij mu loga z walki i wytłumacz dlaczego przegrał."

The fights are recorded by :mod:`altermmo.core.fightlog`.  Here:

* :func:`on_message` - a player who LOST a typed fight on this channel in the
  last :data:`COMPLAINT_WINDOW` s writes a lag complaint
  (:func:`altermmo.core.fightlog.is_lag_complaint`): the bot answers once per
  fight with the fight's log and why it was lost (:func:`explain`);
* :func:`show` - ``$walka <gracz>`` (administrators): the last recorded fight
  of a member, in the same format.

Rules:

* **honest, never accusing**: the answer says what the bot can see (Discord's
  own timestamps of its prompt and of the player's message, its latency, a
  disturbed connection) and what it cannot (the way between the player's
  device and Discord).  When the journal shows trouble on the bot's side - a
  disrupted round, a high latency, a late event loop - it says so plainly and
  the owner is told once (:func:`troubles`);
* an ordinary channel message that pings only the player who asked;
* **never during a typed fight** on the channel (``challenge.fight_on``): the
  answer waits until the channel is free, at most :data:`REPLY_PATIENCE` s,
  and is dropped after that;
* it waits (bounded) for the journal's late-answer watch of the decisive
  round, so that "your answer came 0.7 s too late" / "nothing came" is known;
* once per fight (a second complaint - of the same player or of another member
  of the party - gets nothing: the log is on the channel already), never for a
  fight that was won, never for somebody who did not fight, never for bots,
  never in private messages.  A fight interrupted for technical reasons gets
  one short line saying exactly that;
* nothing here can break message handling: :func:`on_message` is synchronous,
  never raises and does its waiting in a background task that
  ``fightlog.stop`` cancels at shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from ..core import challenge, fightlog, io
from ..core.fightlog import End, Fight, LateState, Outcome, Round
from ..settings import COMMAND_PREFIX

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

#: A complaint is answered when the fight ended at most this many seconds ago.
COMPLAINT_WINDOW = 600.0
#: How long the answer waits for a typed fight on the channel to end before it is dropped.
REPLY_PATIENCE = 120.0
#: How much longer than ``fightlog.LATE_WATCH`` the answer waits for the late-answer watch.
LATE_MARGIN = 5.0
#: How often the waiting answer looks again (seconds).
POLL = 0.25
#: A latency from which the bot's side counts as "not clean" (seconds).
HIGH_LATENCY = 0.4
#: An event loop late by this much in the decisive round is mentioned (seconds; above
#: ``challenge.STALL_LIMIT`` the round is repeated anyway).
STALL_NOTE = 0.25
#: How many of the last rounds the answer lists.
LIST_ROUNDS = 6
#: Discord's limit for a message.
MESSAGE_LIMIT = 2000

NO_MENTIONS = discord.AllowedMentions.none()

# -- texts ------------------------------------------------------------------------------------

MSG_HEAD = "<@{player}> Sprawdziłem zapis tej walki: **{label}**, {result}."
MSG_HEAD_ADMIN = "Ostatnia zapisana walka gracza **{name}**: **{label}**, {result} ({ago})."

RESULT_LOST = "przegrana w turze {number}"
RESULT_LOST_ASKED = "przegrana w turze {number} (pytanie było do **{name}**)"
RESULT_LOST_ASKED_YOU = "przegrana w turze {number} (pytanie było do Ciebie)"
RESULT_LOST_PLAIN = "przegrana"
RESULT_DUEL_LOST = "przegrana z **{winner}** (tur: {turns})"
RESULT_DUEL = "**{winner}** wygrywa z **{loser}** (tur: {turns})"
RESULT_WON = "wygrana"
RESULT_DRAW = "remis"
RESULT_INTERRUPTED = "przerwana przez problemy z połączeniem"
RESULT_UNKNOWN = "bez zapisanego wyniku (trwa albo przerwał ją błąd)"

LABEL_TOWER = "Wieża Śmierci, piętro {floor}"
LABEL_BOSS = "walka z bossem"
LABEL_GROUP = "drużynowa walka z bossem"
LABEL_HUNT = "polowanie"
LABEL_HUNT_PARTY = "polowanie w drużynie"
LABEL_DUEL = "pojedynek"

MSG_WHY = "**Dlaczego:** {verdict} Słowo „{word}”, limit **{limit}** - {detail}"
MSG_WHY_UNKNOWN = ("**Dlaczego:** nie mam zapisu tury, która rozstrzygnęła tę walkę, więc nie potrafię "
                   "powiedzieć nic więcej.")

VERDICT_LATE = "{answer} przyszła po czasie."
VERDICT_NOTHING = "na to pytanie nie dotarła do mnie żadna odpowiedź."
VERDICT_SILENT = "w czasie tury nie dotarła odpowiedź."
VERDICT_WRONG = "pomyłka w słowie - czas nie miał tu znaczenia."
VERDICT_STALE = "spóźniona odpowiedź z poprzedniej tury została policzona jako odpowiedź na to pytanie."
VERDICT_WRONG_PLAYER = "odpisał ktoś inny, niż był pytany."

DETAIL_LATE = ("wiadomość („{text}”) została utworzona na Discordzie **{after}** po moim pytaniu, czyli "
               "**{over} po limicie** (zaliczam odpowiedzi utworzone do {tolerance} po limicie).")
DETAIL_LATE_OTHER = ("{first} („{text}”) została utworzona na Discordzie **{after}** po moim pytaniu, czyli "
                     "**{over} po limicie**, i nie było to wymagane słowo.")
DETAIL_NOTHING = ("w czasie tury nie dotarła {none}, a przez następne {watch} po niej nie przyszła {sender} "
                  "żadna odpowiedź (komend i wiadomości o lagu nie liczę).")
DETAIL_NEXT = "w czasie tury nie dotarła {none}; nasłuchiwałem jeszcze {watch}, do następnego pytania."
DETAIL_WATCHING = "w czasie tury nie dotarła {none}; wciąż nasłuchuję, czy coś jeszcze przyjdzie."
DETAIL_SILENT = "w czasie tury nie dotarła {none}."
DETAIL_WRONG = "odpowiedź przyszła w czasie (po **{elapsed}**), ale brzmiała „{typed}”."
DETAIL_STALE = (" To słowo z tury {number}: dotarło już po jej czasie, więc zostało policzone jako "
                "odpowiedź na następne pytanie.")
DETAIL_WRONG_PLAYER = ("pytanie było do gracza **{asked}**, a w czasie (po **{elapsed}**) odpisał gracz "
                       "**{other}** („{typed}”); w tej walce liczy się to jako pomyłka.")

MSG_DUEL_WHY = ("**Dlaczego:** pojedynek rozstrzygają obrażenia z wielu tur - im szybsza poprawna odpowiedź, "
                "tym większe; tura bez odpowiedzi w czasie albo z błędem nie zadaje ich wcale. {tally}")
DUEL_TALLY_YOU = ("Twoje tury: {asked}, z tego poprawne: {correct}, bez odpowiedzi w czasie: {timeouts}, "
                  "z błędem: {wrong}.")
DUEL_TALLY = ("Tury gracza **{name}**: {asked}, z tego poprawne: {correct}, bez odpowiedzi w czasie: "
              "{timeouts}, z błędem: {wrong}.")
MSG_DUEL_LAST = " Ostatnia nieudana tura ({number}): słowo „{word}”, limit **{limit}** - {detail}"

MSG_ROUNDS = "**Ostatnie tury:**"
LINE_PET = "tura {number}{who}: atak towarzysza, bez pytania"
LINE_DISRUPTED = "tura {number}{who}: zakłócona przez połączenie - powtórzona, nie liczy się"
LINE_CORRECT = "tura {number}{who}: limit {limit} - {time}, dobrze"
LINE_WRONG = "tura {number}{who}: limit {limit} - {time}, błędne słowo"
LINE_WRONG_PLAYER = "tura {number}{who}: limit {limit} - {time}, odpisał gracz {other}"
LINE_LATE = "tura {number}{who}: limit {limit} - {time}, po czasie"
LINE_SILENT = "tura {number}{who}: limit {limit} - brak odpowiedzi"
LINE_RUNNING = "tura {number}{who}: limit {limit} - w toku"
NOTE_SAVED_BY_PET = " (towarzysz ochronił)"
NOTE_SAVED_BY_SPARE = " (zapasowa szansa drużyny)"

MSG_HOW = ("**Jak mierzę czas:** od chwili, gdy moje pytanie pojawiło się na Discordzie, do chwili, gdy "
           "Discord utworzył wiadomość z odpowiedzią (oba znaczniki czasu nadaje Discord) - moje własne "
           "opóźnienie się do tego nie wlicza.")
MSG_STEADY = (" W tej turze mój ping wynosił **{ping} ms**, a połączenie z Discordem było stabilne: po "
              "stronie bota nie widzę w tej walce zakłóceń.")
MSG_BLIND = (" Nie widzę natomiast drogi między {device} a Discordem, w obie strony - jeśli tam coś się "
             "opóźniło, ten czas wliczył się do {reply}.")
MSG_BLIND_NOTHING = (" Nie widzę natomiast drogi między {device} a Discordem, w obie strony - jeśli "
                     "wiadomość tam utknęła, Discord jej nie dostał.")
MSG_TROUBLE = (" W tej walce bot miał jednak problemy: {troubles}. Pomiar opiera się na znacznikach czasu "
               "Discorda, więc nie powinno to zmienić wyniku, ale nie mogę tego wykluczyć - zgłoszenie może "
               "być zasadne i przekazałem je właścicielowi.")
MSG_TROUBLE_ADMIN = (" W tej walce bot miał jednak problemy: {troubles}. Pomiar opiera się na znacznikach "
                     "czasu Discorda, więc nie powinno to zmienić wyniku, ale nie da się tego wykluczyć.")
MSG_TECH = " Ping bota w tej walce: {low}-{high} ms; zakłócone tury: {disrupted}."

TROUBLE_DISRUPTED_ONE = "tura {numbers} została zakłócona przez połączenie i powtórzona (nie liczyła się)"
TROUBLE_DISRUPTED_MANY = "tury {numbers} zostały zakłócone przez połączenie i powtórzone (nie liczyły się)"
TROUBLE_LATENCY_DECISIVE = "mój ping w decydującej turze wynosił {ping} ms"
TROUBLE_LATENCY = "mój ping dochodził w tej walce do {ping} ms"
TROUBLE_DROPPED = "połączenie z Discordem zostało zerwane w trakcie tury {numbers}"
TROUBLE_STALL = "bot zaciął się w decydującej turze na {stall}"
TROUBLE_WATCH = ("moje połączenie z Discordem zostało zerwane, kiedy po decydującej turze nasłuchiwałem "
                 "spóźnionej odpowiedzi - mogłem jej nie usłyszeć")

MSG_INTERRUPTED = ("<@{player}> Ta walka (**{label}**) została przerwana przez problemy z połączeniem: kilka "
                   "tur z rzędu było zakłóconych i nie dało się ich uczciwie ocenić. Nikt jej nie przegrał.")

MSG_USAGE = ("Użycie: **$walka gracz** (wzmianka, id albo nick) - ostatnia zapisana walka gracza. Zapis "
             "jest trzymany przez 30 minut po walce i znika przy restarcie bota.")
MSG_NO_FIGHT = ("Nie mam zapisanej walki tego gracza (zapis jest trzymany przez 30 minut po walce i znika "
                "przy restarcie bota).")

LOG_COMPLAINT = ("Zgłoszenie laga może być zasadne: <@{player}> napisał o lagu po przegranej walce "
                 "({label}), a zapis walki pokazuje problemy po stronie bota: {troubles}. Szczegóły: "
                 "`$walka {player}` (zapis jest trzymany 30 minut).")


# -- small helpers ----------------------------------------------------------------------------

def _s(seconds: float | None) -> str:
    return f"{seconds or 0.0:.2f} s"


def _plain(text: Any) -> str:
    """``text`` shown as it was typed: no formatting, no mention."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text or "")))


def _in_code(text: Any) -> str:
    return str(text or "").replace("`", "'")


def label(fight: Fight) -> str:
    """The Polish name of the fight."""
    if fight.kind == fightlog.KIND_TOWER:
        return LABEL_TOWER.format(floor=fight.detail)
    if fight.kind == fightlog.KIND_GROUP:
        return LABEL_GROUP
    if fight.kind == fightlog.KIND_DUEL:
        return LABEL_DUEL
    if fight.kind == fightlog.KIND_HUNT:
        return LABEL_HUNT_PARTY if fight.party else LABEL_HUNT
    return LABEL_BOSS


@dataclass(frozen=True)
class _Voice:
    """How the player a round was addressed to is spoken about: "you" or by name."""

    answer: str   # nominative, starts a sentence part: "Twoja odpowiedź"
    first: str    # "pierwsza Twoja wiadomość po tym pytaniu"
    none: str     # "żadna Twoja wiadomość"
    sender: str   # "od Ciebie"
    device: str   # "Twoim urządzeniem"
    reply: str    # genitive: "Twojej odpowiedzi"


_YOU = _Voice("Twoja odpowiedź", "pierwsza Twoja wiadomość po tym pytaniu", "żadna Twoja wiadomość",
              "od Ciebie", "Twoim urządzeniem", "Twojej odpowiedzi")


def _voice(entry: Round, viewer_id: int | None) -> _Voice:
    if viewer_id is not None and entry.asked_id == viewer_id:
        return _YOU
    name = _plain(entry.asked_name)
    return _Voice(f"odpowiedź gracza **{name}**", f"pierwsza wiadomość gracza **{name}** po tym pytaniu",
                  f"żadna wiadomość gracza **{name}**", f"od gracza **{name}**", "urządzeniem gracza",
                  f"odpowiedzi gracza **{name}**")


# -- what went wrong on the bot's side ---------------------------------------------------------

def troubles(fight: Fight) -> list[str]:
    """What the journal shows on the bot's side that could matter (empty: nothing).

    A round repeated because the connection dropped or the event loop
    stalled, a latency of :data:`HIGH_LATENCY` or more, a connection that
    dropped during an answered round, an event loop late by
    :data:`STALL_NOTE` or more in the decisive round, a connection that
    dropped while the journal listened for the decisive round's late answer
    and heard nothing.  The timing itself
    rests on Discord's timestamps, so none of this proves the result wrong -
    but with any of it a complaint may be justified, and the owner is told.
    """
    found: list[str] = []
    disrupted = [str(entry.number) for entry in fight.disrupted_rounds()]
    if len(disrupted) == 1:
        found.append(TROUBLE_DISRUPTED_ONE.format(numbers=disrupted[0]))
    elif disrupted:
        found.append(TROUBLE_DISRUPTED_MANY.format(numbers=", ".join(disrupted)))
    decisive = fight.decisive
    if decisive is not None and decisive.latency >= HIGH_LATENCY:
        found.append(TROUBLE_LATENCY_DECISIVE.format(ping=round(decisive.latency * 1000)))
    else:
        worst = max((entry.latency for entry in fight.rounds), default=0.0)
        if worst >= HIGH_LATENCY:
            found.append(TROUBLE_LATENCY.format(ping=round(worst * 1000)))
    dropped = [str(entry.number) for entry in fight.rounds
               if entry.disturbed and entry.outcome is not Outcome.DISRUPTED]
    if dropped:
        found.append(TROUBLE_DROPPED.format(numbers=", ".join(dropped)))
    if decisive is not None and decisive.stall >= STALL_NOTE:
        found.append(TROUBLE_STALL.format(stall=_s(decisive.stall)))
    if decisive is not None and decisive.late is None and decisive.watch_disturbed:
        found.append(TROUBLE_WATCH)
    return found


# -- the text ---------------------------------------------------------------------------------

def _detail(entry: Round, viewer_id: int | None) -> tuple[str, str]:
    """``(verdict, detail)``: why the round failed, in plain words and with the numbers."""
    voice = _voice(entry, viewer_id)
    if entry.outcome is Outcome.WRONG_WORD:
        detail = DETAIL_WRONG.format(elapsed=_s(entry.elapsed), typed=_plain(entry.typed))
        if entry.stale_of is not None:
            return VERDICT_STALE, detail + DETAIL_STALE.format(number=entry.stale_of)
        return VERDICT_WRONG, detail
    if entry.outcome is Outcome.WRONG_PLAYER:
        return VERDICT_WRONG_PLAYER, DETAIL_WRONG_PLAYER.format(
            asked=_plain(entry.asked_name), elapsed=_s(entry.elapsed), other=_plain(entry.typed_by_name),
            typed=_plain(entry.typed))
    late = entry.late
    if late is not None:
        numbers = {"text": _plain(late.text), "after": _s(late.after), "over": _s(late.after - entry.limit)}
        if late.text.strip().lower() == entry.word:
            return (VERDICT_LATE.format(answer=voice.answer),
                    DETAIL_LATE.format(tolerance=_s(challenge.TOLERANCE), **numbers))
        return VERDICT_SILENT, DETAIL_LATE_OTHER.format(first=voice.first, **numbers)
    if entry.late_state is LateState.NOTHING:
        return (VERDICT_NOTHING,
                DETAIL_NOTHING.format(none=voice.none, sender=voice.sender, watch=f"{entry.watched:.0f} s"))
    if entry.late_state is LateState.NEXT_PROMPT:
        return VERDICT_SILENT, DETAIL_NEXT.format(none=voice.none, watch=f"{entry.watched:.1f} s")
    if entry.late_state is LateState.WATCHING:
        return VERDICT_SILENT, DETAIL_WATCHING.format(none=voice.none)
    return VERDICT_SILENT, DETAIL_SILENT.format(none=voice.none)


def _why(fight: Fight, viewer_id: int | None, subject_id: int | None) -> str:
    entry = fight.decisive
    if fight.kind == fightlog.KIND_DUEL:
        loser = fight.losers[0] if fight.losers else subject_id
        mine = fight.rounds_of(loser) if loser is not None else []
        counts = {"asked": len(mine),
                  "correct": sum(1 for item in mine if item.outcome is Outcome.CORRECT),
                  "timeouts": sum(1 for item in mine if item.outcome is Outcome.TIMEOUT),
                  "wrong": sum(1 for item in mine if item.outcome is Outcome.WRONG_WORD)}
        if viewer_id is not None and loser == viewer_id:
            tally = DUEL_TALLY_YOU.format(**counts)
        else:
            tally = DUEL_TALLY.format(name=_plain(fight.name_of(loser)), **counts)
        text = MSG_DUEL_WHY.format(tally=tally)
        if entry is not None:
            text += MSG_DUEL_LAST.format(number=entry.number, word=_plain(entry.word), limit=_s(entry.limit),
                                         detail=_detail(entry, viewer_id)[1])
        return text
    if entry is None:
        return MSG_WHY_UNKNOWN
    verdict, detail = _detail(entry, viewer_id)
    return MSG_WHY.format(verdict=verdict, word=_plain(entry.word), limit=_s(entry.limit), detail=detail)


def _line(fight: Fight, entry: Round) -> str:
    who = f" ({_in_code(entry.asked_name)})" if fight.party and entry.asked_name else ""
    values = {"number": entry.number, "who": who, "limit": _s(entry.limit)}
    if entry.outcome is Outcome.PET_ATTACK:
        return LINE_PET.format(**values)
    if entry.outcome is Outcome.DISRUPTED:
        return LINE_DISRUPTED.format(**values)
    if entry.outcome is Outcome.CORRECT:
        line = LINE_CORRECT.format(time=_s(entry.elapsed), **values)
    elif entry.outcome is Outcome.WRONG_WORD:
        line = LINE_WRONG.format(time=_s(entry.elapsed), **values)
    elif entry.outcome is Outcome.WRONG_PLAYER:
        line = LINE_WRONG_PLAYER.format(time=_s(entry.elapsed), other=_in_code(entry.typed_by_name), **values)
    elif entry.outcome is Outcome.TIMEOUT:
        line = (LINE_LATE.format(time=_s(entry.late.after), **values) if entry.late is not None
                else LINE_SILENT.format(**values))
    else:
        return LINE_RUNNING.format(**values)
    if entry.saved == fightlog.SAVED_BY_PET:
        line += NOTE_SAVED_BY_PET
    elif entry.saved == fightlog.SAVED_BY_SPARE:
        line += NOTE_SAVED_BY_SPARE
    return line


def _rounds(fight: Fight) -> str:
    """The last rounds as a short list (empty when nothing was recorded)."""
    shown = fight.rounds[-LIST_ROUNDS:]
    if not shown:
        return ""
    return MSG_ROUNDS + "\n```\n" + "\n".join(_line(fight, entry) for entry in shown) + "\n```"


def _how(fight: Fight, viewer_id: int | None, *, admin: bool) -> str:
    """How the time is measured, what the bot's side looked like, what the bot cannot see."""
    entry = fight.decisive
    text = MSG_HOW
    found = troubles(fight)
    if found:
        text += (MSG_TROUBLE_ADMIN if admin else MSG_TROUBLE).format(troubles="; ".join(found))
    elif entry is not None:
        text += MSG_STEADY.format(ping=round(entry.latency * 1000))
    if entry is not None and entry.outcome is Outcome.TIMEOUT:
        voice = _voice(entry, viewer_id)
        if entry.late is not None:
            text += MSG_BLIND.format(device=voice.device, reply=voice.reply)
        else:
            text += MSG_BLIND_NOTHING.format(device=voice.device)
    return text


def _result(fight: Fight, viewer_id: int | None, subject_id: int | None) -> str:
    if fight.kind == fightlog.KIND_DUEL and fight.end is End.WON:
        winner = _plain(", ".join(fight.name_of(player_id) for player_id in fight.winners))
        turns = max((entry.number for entry in fight.rounds), default=0)
        if viewer_id is not None and viewer_id in fight.losers:
            return RESULT_DUEL_LOST.format(winner=winner, turns=turns)
        loser = _plain(", ".join(fight.name_of(player_id) for player_id in fight.losers))
        return RESULT_DUEL.format(winner=winner, loser=loser, turns=turns)
    if fight.end is End.LOST:
        entry = fight.decisive
        if entry is None:
            return RESULT_LOST_PLAIN
        if not fight.party:
            return RESULT_LOST.format(number=entry.number)
        if viewer_id is not None and entry.asked_id == viewer_id:
            return RESULT_LOST_ASKED_YOU.format(number=entry.number)
        return RESULT_LOST_ASKED.format(number=entry.number, name=_plain(entry.asked_name))
    if fight.end is End.WON:
        return RESULT_WON
    if fight.end is End.DRAW:
        return RESULT_DRAW
    if fight.end is End.INTERRUPTED:
        return RESULT_INTERRUPTED
    return RESULT_UNKNOWN


def _ago(fight: Fight) -> str:
    age = time.monotonic() - (fight.ended if fight.ended is not None else fight.started)
    return "przed chwilą" if age < 60 else f"{int(age // 60)} min temu"


def explain(fight: Fight, viewer_id: int | None = None, *, subject_id: int | None = None) -> str:
    """The fight's log and why it was lost, as one Discord message.

    ``viewer_id``: the player the answer is for ("you" where a round was
    his); None for the administrators' view of ``subject_id``'s fight, where
    everybody is named.  Always within :data:`MESSAGE_LIMIT`.
    """
    admin = viewer_id is None
    result = _result(fight, viewer_id, subject_id)
    if admin:
        head = MSG_HEAD_ADMIN.format(name=_plain(fight.name_of(subject_id)), label=label(fight),
                                     result=result, ago=_ago(fight))
    else:
        head = MSG_HEAD.format(player=viewer_id, label=label(fight), result=result)
    lost = bool(fight.losers)
    parts = [head]
    if lost:
        parts.append(_why(fight, viewer_id, subject_id))
    rounds = _rounds(fight)
    if rounds:
        parts.append(rounds)
    if lost:
        parts.append(_how(fight, viewer_id, admin=admin))
    elif fight.rounds:
        latencies = [entry.latency for entry in fight.rounds if entry.outcome is not Outcome.PET_ATTACK]
        parts.append(MSG_TECH.strip().format(low=round(min(latencies, default=0.0) * 1000),
                                             high=round(max(latencies, default=0.0) * 1000),
                                             disrupted=len(fight.disrupted_rounds())))
    text = "\n".join(parts)
    if len(text) > MESSAGE_LIMIT and rounds:   # (long names: the list goes first)
        parts.remove(rounds)
        text = "\n".join(parts)
    return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT - 1] + "…"


# -- the complaint ----------------------------------------------------------------------------

def on_message(bot: "AlterBot", message: Any) -> bool:
    """A message was written: answer it when it is a lag complaint about a fight just lost.

    True when an answer was started (it is sent by a background task).
    Synchronous and silent about everything else; never raises.
    """
    try:
        return _on_message(bot, message)
    except Exception:  # noqa: BLE001 - this feature must never break message handling
        log.exception("Could not look at a possible lag complaint.")
        return False


def _on_message(bot: "AlterBot", message: Any) -> bool:
    author = message.author
    if getattr(author, "bot", False) or getattr(message, "guild", None) is None:
        return False
    text = str(message.content or "")
    if text.startswith(COMMAND_PREFIX) or not fightlog.is_lag_complaint(text):
        return False
    fight = fightlog.last_fight(author.id)
    if fight is None or fight.answered or fight.channel_id != message.channel.id:
        return False
    # (A fight that is still running has no end: "lag" typed in it is an answer to its round.)
    if fight.ended is None or time.monotonic() - fight.ended > COMPLAINT_WINDOW:
        return False
    if author.id not in fight.losers and fight.end is not End.INTERRUPTED:
        return False
    fight.answered = True  # claimed before anything is awaited: a second complaint gets nothing
    return fightlog.spawn(_answer(bot, message.channel, author, fight), name="fight-report") is not None


async def _late_answer_known(fight: Fight) -> None:
    """Wait (bounded) until the watch for the decisive round's late answer is over."""
    entry = fight.decisive
    deadline = time.monotonic() + fightlog.LATE_WATCH + LATE_MARGIN
    while entry is not None and entry.late_state is LateState.WATCHING and time.monotonic() < deadline:
        await asyncio.sleep(POLL)


async def _channel_free(channel: Any) -> bool:
    """Wait until no typed fight is on the channel; False when that takes too long."""
    deadline = time.monotonic() + REPLY_PATIENCE
    while challenge.fight_on(channel):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL)
    return True


async def _answer(bot: "AlterBot", channel: Any, player: Any, fight: Fight) -> None:
    """Send the answer to a complaint (see the module docstring for the rules).  Never raises."""
    try:
        await _late_answer_known(fight)
        if not await _channel_free(channel):
            log.info("Lag complaint of %s (%s): the channel stayed busy with a fight for %.0f s, "
                     "no answer.", player, player.id, REPLY_PATIENCE)
            return
        if fight.end is End.INTERRUPTED:
            text = MSG_INTERRUPTED.format(player=player.id, label=label(fight))
            found: list[str] = []
        else:
            text = explain(fight, player.id)
            found = troubles(fight)
        await io.send(channel, text, allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=False, replied_user=False, users=[player]))
        log.info("Lag complaint of %s (%s) answered: %s, %s.", player, player.id, fightlog.kind_text(fight),
                 "trouble on the bot's side: " + "; ".join(found) if found else "nothing wrong on the bot's side")
        if found and not fight.owner_told:
            fight.owner_told = True
            await bot.log_to_owner(LOG_COMPLAINT.format(player=player.id, label=label(fight),
                                                        troubles="; ".join(found)), ping=True)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - an explanation is never worth an error
        log.exception("Could not answer the lag complaint of %s.", getattr(player, "id", player))


# -- $walka -----------------------------------------------------------------------------------

async def show(scene: "Scene", who: str | None) -> None:
    """``$walka <gracz>``: the last recorded fight of a member (pings nobody).

    Like every other answer it is not posted between the prompts of a typed
    fight: on a channel where one is on, it waits for the fight to end (at
    most :data:`REPLY_PATIENCE` s, then it is dropped - ask again, or on
    another channel).
    """
    if not who or not who.strip():
        text = MSG_USAGE
    else:
        found = fightlog.find_fight(who)
        text = MSG_NO_FIGHT if found is None else explain(found[0], None, subject_id=found[1])
    if not await _channel_free(scene.channel):
        log.info("$walka: the channel stayed busy with a fight for %.0f s, no answer.", REPLY_PATIENCE)
        return
    await io.send(scene.channel, text, allowed_mentions=NO_MENTIONS)
