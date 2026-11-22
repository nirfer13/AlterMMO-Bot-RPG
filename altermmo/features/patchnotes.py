"""Patch notes: after an update the players are told what changed (owner, 2026-11-21).

Owner: "Po każdej aktualizacji i pierwszym uruchomieniu z aktualizacją wysyłaj tutaj patch
notesy po polsku. Bez spoilerów." and "Może zdarzyć się tak, że po zmianach nie uruchomię od
razu, więc sprawdzaj, które zmiany w ostatniej wiadomości zostały wysłane i wyślij pozostałe."

**The notes** are written by the developers into ``config/patchNotes.json`` (part of the
repository, so they arrive with the update they describe): a JSON list, oldest first, of
``{"id": 1, "date": "YYYY-MM-DD", "title": "", "changes": ["...", ...]}``.  The ids are
1, 2, 3 ... without gaps; an entry is final once released - later changes go into a NEW entry
with the next id.  :func:`load_notes` checks the file strictly; a broken file posts nothing and
the owner is told exactly what is wrong.

**What was already announced** (``highest announced`` = the larger of the two):

* the bot's OWN newest patch-notes message on the channel ("które zmiany w ostatniej
  wiadomości zostały wysłane"): the footer of each one carries the marker ``Patch notes nr
  4-6`` (:func:`marker`) - the entries whose LAST line is in that message.  Only messages
  written by the bot itself with exactly that footer under the patch-notes title count
  (:func:`marker_of`); somebody quoting it, another bot, an edited-looking text never do.  A
  short network problem while reading is waited out (:data:`HISTORY_PATIENCE`) before the bot
  falls back to the local number alone;
* the number remembered locally (``runtime.json``, key :data:`RUNTIME_KEY`).

Why both: the channel may be purged or the message deleted (the local number then prevents
re-posting years of notes), the data folder may be lost (the channel then tells).  Neither
of them (the very first run): every entry is posted.  The history cannot be read and nothing
is remembered either: nothing is posted, the owner is told, the next start tries again.  A
number HIGHER than the last entry of the file (the file was rolled back or renumbered) is
told to the owner at every start until ``$patchnotes od <nr>`` puts it right - otherwise the
entries that later get those ids would never be posted.

**Exactly once.**  One :class:`asyncio.Lock` per bot covers the whole "work out + post"
section (start-up and ``$patchnotes`` alike), the start-up runs once per process (not from
``on_ready``, which fires again after every reconnect) and every run works the state out
afresh.  Unsent entries go together, oldest first, as few messages as Discord's limits allow
(:func:`plan_posts`); the local number is advanced after EACH delivered message (and never
lowered by a run that did not finish), so a failure in the middle never repeats what was
delivered and never skips what was not.  An entry too long for one message is split between
its lines; only the LAST part carries the entry's number, so an entry interrupted in the
middle is posted again whole at the next start (accepted: rare, and nothing is ever lost).

A send that failed for a network reason is **not** blindly repeated (a request that timed out
may have been carried out): :func:`_deliver` first looks at the newest messages of the channel
and posts again only when the message is not there.  discord.py itself repeats a send that got
a 500 / 502 / 504 / 524 answer or a connection reset (inside one ``send`` call, invisible to
this module), and such an answer may come after Discord created the message - so after every
delivered message the newest messages are looked at once more and identical copies of it (by
the bot, younger than the first attempt) are deleted, the oldest one stays (:func:`_drop_copies`).

What is left: a copy that becomes visible on Discord only after that look (seconds after a
slow request) stays - very unlikely; when the history cannot be read at all, a failed send is
given up (not repeated) and the next start may post that one message again, and no copies are
looked for; a message identical to one posted less than :data:`VERIFY_CLOCK_SLACK` before the
run (``$patchnotes od`` twice in a row) counts as the same message (one copy stays); a rolled
back file that gets its new entries BEFORE the next start (the number is then not higher than
the file) cannot be told from an ordinary one, and those entries are not posted.

**No spoilers** ("Bez spoilerów"; the owner's rule for this game: players discover things
themselves - no species of pets, no names of card frames / seals / foils, no drop chances):
:func:`spoiler_in` refuses an entry with a percentage or a word of :data:`FORBIDDEN_WORDS`
(plus the frame names of :mod:`altermmo.features.petcards`, when it has a catalogue, in any
grammatical case).  The text is checked as a reader sees it (:func:`plain_text`: Unicode
normalised, invisible and formatting characters removed).  A refused entry and everything
after it waits (the order of the entries stays), the owner is told which entry and which word.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as Date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import discord

from ..core import io
from ..settings import CONFIG_DIR

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot

log = logging.getLogger(__name__)

#: The developers' notes (in the repository: they come with the update they describe).
NOTES_FILE = CONFIG_DIR / "patchNotes.json"
#: ``runtime.json`` key: the highest entry id the bot knows it has announced.  (Start-up drops
#: only the ``job_last_run:*`` markers of a long-idle bot - see ``AlterBot._review_saved_state``.)
RUNTIME_KEY = "patch_notes_announced"

#: How many of the newest messages of the channel are searched for the last patch-notes message.
#: The channel is an information channel: a few messages per update (and maybe some replies) - 500
#: is hundreds of updates of slack, read in at most five API pages once per start, in the
#: background.  Should the marker ever be further back, the locally remembered number still covers it.
HISTORY_LIMIT = 500
#: How long a history read that fails for a network reason is tried again (seconds) before the
#: bot falls back to the local number alone (which may be behind when its write was lost).
HISTORY_PATIENCE = 600.0
#: After a send failed for a network reason: how many newest messages are checked for it.
VERIFY_LIMIT = 50
#: ... how long the bot keeps trying to deliver one message before it gives up (seconds).
SEND_PATIENCE = 600.0
#: ... first pause before the check (also before a history read is tried again), doubled up to
#: the second value.
VERIFY_FIRST_DELAY = 5.0
VERIFY_MAX_DELAY = 60.0
#: ... a message found by the check counts only when it is younger than the first attempt minus
#: this much (clock difference between this machine and Discord).
VERIFY_CLOCK_SLACK = timedelta(minutes=2)

# Discord's limits of one embed (characters, counted by Discord in UTF-16 code units - an emoji
# is two - so :func:`discord_len` counts the same way; a message has no content here).
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FOOTER_LIMIT = 2048
EMBED_TOTAL_LIMIT = 6000
#: The longest change line and entry title the file may hold: one line is never split, so a line
#: plus the entry's header must always fit into one description.
LINE_LIMIT = 1000
TITLE_LIMIT = 200

MSG_TITLE = "📜 Patch notes"
#: Titles a marker message may carry: when :data:`MSG_TITLE` ever changes, append the new one
#: and keep the old ones, so the messages already on the channel keep counting.
MARKER_TITLES: tuple[str, ...] = (MSG_TITLE,)
COLOUR = 0xC9A227
BULLET = "• "
#: Header of the second and later part of an entry too long for one message.
MSG_CONTINUED = " (cd.)"
#: Footer of a message that is NOT the last part of an entry: deliberately no number in it.
FOOTER_TO_BE_CONTINUED = "Patch notes - ciąg dalszy w następnej wiadomości"
MARKER_PREFIX = "Patch notes nr "
#: The marker, matched against the whole (stripped) footer; ASCII digits only, no leading zero.
_MARKER = re.compile(r"Patch notes nr ([1-9][0-9]{0,8})(?:-([1-9][0-9]{0,8}))?")
#: Never ping anybody, whatever a note says (``@everyone``, a role, a player).
NO_MENTIONS = discord.AllowedMentions.none()

# -- what the owner / the administrator is told (Polish) --------------------------------------

MSG_OWNER_FILE_BROKEN = ("Patch notes: plik `config/patchNotes.json` jest błędny - {problem}. Nic nie "
                         "wysłałem. Popraw plik; spróbuję przy następnym starcie (albo wpisz `$patchnotes`).")
MSG_OWNER_NO_CHANNEL = ("Patch notes: nie mogę pisać na kanale {channel} ({error}). Nic nie wysłałem; "
                        "spróbuję przy następnym starcie (albo wpisz `$patchnotes`).")
MSG_OWNER_NO_HISTORY = ("Patch notes: nie mogę przeczytać historii kanału {channel} ({error}), a nie "
                        "pamiętam, co już ogłosiłem - nic nie wysłałem, żeby niczego nie powtórzyć. Daj "
                        "botowi uprawnienie \"Czytanie historii wiadomości\"; spróbuję przy następnym "
                        "starcie (albo wpisz `$patchnotes`).")
MSG_OWNER_HISTORY_FALLBACK = ("Patch notes: nie mogę przeczytać historii kanału {channel} ({error}) - "
                              "korzystam z zapamiętanego numeru ostatniego wpisu (nr {local}).")
MSG_OWNER_SPOILER = ("Patch notes: wpis nr {id} zawiera spoiler (\"{word}\") - nie wysłałem go{after}. "
                     "Popraw wpis w `config/patchNotes.json`.")
MSG_OWNER_SPOILER_AFTER = " ani wpisów po nim"
MSG_OWNER_SEND_FAILED = ("Patch notes: nie udało się wysłać wiadomości na kanał {channel} ({error}). "
                         "Nie wysłane: {left} - pójdą przy następnym starcie (albo po `$patchnotes`).")
MSG_OWNER_REMEMBER_FAILED = ("Patch notes: nie udało się zapisać numeru ostatniego ogłoszonego wpisu "
                             "({error}); przy następnym starcie liczy się ostatnia wiadomość na kanale.")
MSG_OWNER_SENT = "Patch notes: ogłosiłem {what} na kanale {channel}."
MSG_OWNER_BEYOND = ("Patch notes: ostatnio ogłoszony wpis to nr {highest}, a plik `config/patchNotes.json` "
                    "kończy się na nr {last} - plik cofnięty albo przenumerowany? Nowe wpisy o numerach do "
                    "{highest} nie zostałyby ogłoszone. Sprawdź plik i wpisz `$patchnotes od {last}` (wyśle "
                    "ponownie wpis nr {last} i od tej pory liczy się nr {last}).")
MSG_OWNER_BEYOND_EMPTY = ("Patch notes: ostatnio ogłoszony wpis to nr {highest}, a plik "
                          "`config/patchNotes.json` nie ma żadnego wpisu - plik cofnięty albo wyczyszczony? "
                          "Nowe wpisy o numerach do {highest} nie zostałyby ogłoszone.")
MSG_OWNER_UNEXPECTED = ("Patch notes: nieoczekiwany błąd ({error}) - szczegóły są w logu bota. Spróbuję "
                        "przy następnym starcie (albo wpisz `$patchnotes`).")

MSG_STATUS_NONE = "Ostatnio ogłoszony wpis: **żaden** (ani na kanale, ani w pamięci bota)."
MSG_STATUS_UNKNOWN = ("Ostatnio ogłoszony wpis: **nie wiem** - nie mogę przeczytać historii kanału, "
                      "a w pamięci bota nic nie ma.")
MSG_STATUS = "Ostatnio ogłoszony wpis: **nr {highest}** - {source}."
MSG_SOURCE_BOTH = "na kanale i w pamięci bota"
MSG_SOURCE_CHANNEL = "z ostatniej wiadomości na kanale (pamięć bota: {local})"
MSG_SOURCE_LOCAL = "z pamięci bota (na kanale: {channel})"
MSG_NOTHING_THERE = "brak"
MSG_UNREADABLE = "nie mogę go przeczytać"
MSG_FILE_STATE = "Wpisów w pliku: {total}."
MSG_WAITING = "Czeka: {what}."
MSG_NOTHING_WAITS = "Nic nie czeka na ogłoszenie."
MSG_FROM = "Wysyłam od wpisu nr {start}: {what}."
MSG_FROM_AGAIN = "ponownie {what}"
MSG_FROM_NEW = "po raz pierwszy {what}"
MSG_FROM_GAP = ("Nie przeskoczę wpisów nr {gap}: nie zostały jeszcze ogłoszone. Wpisz `$patchnotes` (wyśle "
                "wszystko, co czeka) albo `$patchnotes od {next}`.")
MSG_BAD_FROM = "Nie ma wpisu nr {start} - plik ma wpisy nr 1-{total}."
MSG_BAD_FROM_EMPTY = "Plik nie ma jeszcze żadnego wpisu."
MSG_SENT = "Wysłałem {what} {messages} na kanale {channel}."
MSG_PROBLEM = "⚠️ {problem}"


# -- the notes file ---------------------------------------------------------------------------

class NotesError(ValueError):
    """The notes file cannot be used; ``str(error)`` says why, in Polish, for the owner."""


@dataclass(frozen=True)
class Note:
    """One released entry of the notes file."""

    id: int
    date: str
    title: str
    changes: tuple[str, ...]


_KEYS = {"id", "date", "title", "changes"}
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def discord_len(text: str) -> int:
    """Length as Discord counts it (UTF-16 code units: an emoji outside the BMP counts as 2)."""
    return len(text.encode("utf-16-le")) // 2


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json`` keeps the last of two equal keys silently; in a hand-written file that is a typo."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise NotesError(f"pole \"{key}\" występuje dwa razy w jednym wpisie")
        seen[key] = value
    return seen


def _text(value: Any, where: str, what: str, limit: int, *, empty: bool) -> str:
    if not isinstance(value, str):
        raise NotesError(f"{where}: {what} musi być tekstem w cudzysłowie")
    text = value.strip()
    if not text and not empty:
        raise NotesError(f"{where}: {what} jest puste")
    if "\n" in text or "\r" in text:
        raise NotesError(f"{where}: {what} ma znak nowej linii - każda zmiana to osobny tekst na liście")
    if discord_len(text) > limit:
        raise NotesError(f"{where}: {what} ma {discord_len(text)} znaków (najwyżej {limit})")
    return text


def parse_notes(raw: Any) -> list[Note]:
    """The entries of an already decoded notes file; raises :class:`NotesError` on anything wrong.

    Strict on purpose (a typo must not post nonsense to every player): a list of objects with
    exactly the keys ``id`` (1, 2, 3 ... in order), ``date`` (a real ``YYYY-MM-DD`` day),
    ``title`` (optional text, may be ``""``) and ``changes`` (a non-empty list of non-empty,
    one-line texts of at most :data:`LINE_LIMIT` characters).
    """
    if not isinstance(raw, list):
        raise NotesError("plik musi zawierać listę wpisów: [ {...}, {...} ]")
    notes: list[Note] = []
    for index, entry in enumerate(raw, 1):
        where = f"wpis {index}. na liście"
        if not isinstance(entry, dict):
            raise NotesError(f"{where} nie jest obiektem {{...}}")
        unknown = sorted(set(entry) - _KEYS)
        if unknown:
            raise NotesError(f"{where}: nieznane pole {', '.join(unknown)} (dozwolone: id, date, title, changes)")
        missing = [key for key in ("id", "date", "changes") if key not in entry]
        if missing:
            raise NotesError(f"{where}: brak pola {', '.join(missing)}")
        note_id = entry["id"]
        if type(note_id) is not int:  # (bool is an int to Python, not to a reader of the file)
            raise NotesError(f"{where}: id musi być liczbą całkowitą, jest {json.dumps(note_id, ensure_ascii=False)}")
        if note_id != index:
            raise NotesError(f"{where} ma id {note_id}, a powinien mieć {index} (numery idą po kolei od 1, "
                             "bez przerw i powtórzeń)")
        where = f"wpis nr {note_id}"
        day = entry["date"]
        if not isinstance(day, str) or not _DATE.fullmatch(day):
            raise NotesError(f"{where}: data musi mieć postać RRRR-MM-DD, jest {json.dumps(day, ensure_ascii=False)}")
        try:
            Date.fromisoformat(day)
        except ValueError:
            raise NotesError(f"{where}: nie ma takiego dnia {day}") from None
        title = _text(entry.get("title", ""), where, "tytuł", TITLE_LIMIT, empty=True)
        changes = entry["changes"]
        if not isinstance(changes, list) or not changes:
            raise NotesError(f"{where}: changes musi być niepustą listą tekstów")
        lines = tuple(_text(change, where, f"zmiana {number}.", LINE_LIMIT, empty=False)
                      for number, change in enumerate(changes, 1))
        notes.append(Note(note_id, day, title, lines))
    return notes


def load_notes(path: Path | str | None = None) -> list[Note]:
    """Read and check the notes file (default :data:`NOTES_FILE`); raises :class:`NotesError`."""
    target = Path(path) if path is not None else NOTES_FILE
    try:
        text = target.read_text(encoding="utf-8-sig")  # (a BOM from a Windows editor is fine)
    except FileNotFoundError:
        raise NotesError(f"nie ma pliku {target.name}") from None
    except UnicodeDecodeError:
        raise NotesError("plik nie jest zapisany w UTF-8") from None
    except OSError as exc:
        raise NotesError(f"nie mogę przeczytać pliku ({type(exc).__name__})") from None
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise NotesError(f"to nie jest poprawny JSON (linia {exc.lineno}, kolumna {exc.colno}: {exc.msg})") from None
    return parse_notes(raw)


# -- no spoilers ------------------------------------------------------------------------------

#: Words a note may not contain (case-insensitive, also inside a longer word - so write the
#: stem: "pieczę" catches "pieczęć", "pieczęcie" ...).  The owner's rule: players discover the
#: pets' species, the names of the card frames, seals and foils and the drop chances themselves.
#: Extend freely.  An entry of several words ("złota ramka") is matched word by word in any
#: grammatical case, like the frame names (:func:`_phrase_in`).  (The seal names of
#: ``petcards.SEAL_NAMES`` are deliberately not pulled in: "Łowcy" is also the name of the
#: hunters' role every note may talk about.)
FORBIDDEN_WORDS: tuple[str, ...] = ("holo", "pieczę", "arcyłowc", "tropiciel")
#: A percentage: a number, then (after spaces or a dash) a percent / per-mille sign, "procent..."
#: or "proc." - optionally "punkt(ów) procentowych" / "pkt proc." between: "5%", "12,5 %",
#: "3 procent", "5-procentowa", "5 proc.", "2 punkty procentowe".  A chance, whatever it is the
#: chance of.  Not covered (put such a rule into the deny-list if it ever matters): numbers in
#: words ("pięć procent") and odds like "1 na 100".
_PERCENT = re.compile(r"\d+(?:[.,]\d+)?[\s\-\u2010-\u2015\u2212]*(?:(?:punkt\w*|pkt\.?)\s*)?"
                      r"(?:[%\uff05\ufe6a\u066a\u2030\u2031]|proc(?:ent|\.|\b))", re.IGNORECASE)
#: Discord's markdown (and its escape character): "ho**lo**", "5\\%" are read as "holo", "5%".
_MARKUP = re.compile(r"[*_~|`>\\]")
#: Invisible characters that are not of category Cf (those all go): the rest of Unicode's
#: Default_Ignorable_Code_Point - combining grapheme joiner, Hangul fillers, variation selectors.
_IGNORABLE = re.compile("[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0"
                        "\U000e0100-\U000e01ef]")
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"\w+")
#: A single forbidden word at least this long is a name, not a stem: it also matches as the
#: beginning of a word without its last two letters ("Astrolabium" -> "astrolabia").
_NAME_LENGTH = 8


def plain_text(text: str) -> str:
    """``text`` as a reader of Discord sees it, for the spoiler check.

    NFKC (a decomposed "ę" written as e + U+0328 is "ę"; full-width / small percent signs and
    full-width digits are "%" and digits), every invisible character removed (category Cf -
    zero-width, direction marks, soft hyphen ... - and :data:`_IGNORABLE`), the markdown of
    :data:`_MARKUP` removed, any run of whitespace one space, casefolded.
    """
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = _MARKUP.sub("", _IGNORABLE.sub("", text))
    text = _SPACES.sub(" ", text).strip()
    return unicodedata.normalize("NFKC", text.casefold())


def _fold(word: str) -> str:
    """Without Polish diacritics ("wrót" / "wrota", "okuć" / "okucia" share a stem then)."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).replace("ł", "l")


def _stem(word: str) -> str:
    """The part of one word of a name that stays in every grammatical case.

    Roughly: Polish changes the last one or two letters ("Korona" / "Koronie", "Złoty" /
    "Złotego", "Oko" / "Okiem").  Short stems are fine: a name matches only when ALL its
    words are found one after another.
    """
    if len(word) <= 3:
        return word[:2]
    if len(word) <= 5:
        return word[:-1]
    return word[:-2]


def _phrase_in(parts: list[str], tokens: list[str]) -> bool:
    """True when consecutive words of the text begin with the stems of ``parts`` (folded)."""
    stems = [_stem(_fold(part)) for part in parts]
    for start in range(len(tokens) - len(stems) + 1):
        if all(tokens[start + offset].startswith(stem) for offset, stem in enumerate(stems)):
            return True
    return False


def _word_in(word: str, plain: str, tokens: list[str]) -> bool:
    """True when the forbidden ``word`` is in the text (``plain`` and its folded ``tokens``)."""
    key = plain_text(word)
    parts = _WORD.findall(key)
    if not parts:
        return False
    if key in plain:
        return True
    if len(parts) > 1:
        return _phrase_in(parts, tokens)
    folded = _fold(parts[0])
    return len(folded) >= _NAME_LENGTH and any(token.startswith(folded[:-2]) for token in tokens)


def catalogue_words() -> tuple[str, ...]:
    """Names of the pet-card frames, when :mod:`altermmo.features.petcards` has a ``FRAMES`` catalogue.

    Every frame name is a spoiler (also the base ones: "no names of frames"), in any grammatical
    case (a name of several words is matched word by word on the stems, :func:`_phrase_in`).
    Read with ``getattr`` and a default, so this module never depends on how that catalogue looks.
    """
    try:
        from . import petcards
    except Exception:  # noqa: BLE001 - no catalogue, no extra words
        return ()
    frames = getattr(petcards, "FRAMES", None)
    try:
        items = list(frames.values()) if isinstance(frames, dict) else list(frames or ())
    except TypeError:
        return ()
    names = []
    for frame in items:
        name = getattr(frame, "name", None)
        if isinstance(name, str) and len(name.strip()) >= 3:
            names.append(name.strip())
    return tuple(dict.fromkeys(names))


def forbidden_words() -> tuple[str, ...]:
    """:data:`FORBIDDEN_WORDS` plus the names from the game's catalogues (:func:`catalogue_words`)."""
    return tuple(dict.fromkeys((*FORBIDDEN_WORDS, *catalogue_words())))


def spoiler_in_text(text: str, words: Iterable[str] | None = None) -> str | None:
    """The first spoiler in ``text`` (a percentage or a forbidden word), or None.

    Checked on :func:`plain_text` (what a reader sees); a percentage is also looked for in the
    text as written, so the owner is shown it the way it is in the file.
    """
    words = forbidden_words() if words is None else tuple(words)
    plain = plain_text(text)
    for candidate in (text, plain):
        percent = _PERCENT.search(candidate)
        if percent is not None:
            return percent.group(0)
    tokens = [_fold(token) for token in _WORD.findall(plain)]
    for word in words:
        if word and _word_in(word, plain, tokens):
            return word
    return None


def spoiler_in(note: Note, words: Iterable[str] | None = None) -> str | None:
    """The first spoiler in the entry's title or changes, or None (the entry may be posted)."""
    words = forbidden_words() if words is None else tuple(words)
    for text in (note.title, *note.changes):
        found = spoiler_in_text(text, words)
        if found is not None:
            return found
    return None


def validate_spoilers(notes: Iterable[Note]) -> list[tuple[int, str]]:
    """``(entry id, word)`` of every refused entry (empty: everything may be posted)."""
    words = forbidden_words()
    refused = []
    for note in notes:
        found = spoiler_in(note, words)
        if found is not None:
            refused.append((note.id, found))
    return refused


# -- the marker -------------------------------------------------------------------------------

def marker(first: int, last: int | None = None) -> str:
    """Footer of a message holding the end of the entries ``first`` .. ``last``."""
    if last is None or last == first:
        return f"{MARKER_PREFIX}{first}"
    return f"{MARKER_PREFIX}{first}-{last}"


def parse_marker(text: Any) -> int | None:
    """The highest entry id a footer text stands for, or None when it is not exactly a marker."""
    if not isinstance(text, str):
        return None
    match = _MARKER.fullmatch(text.strip())  # (Discord trims the ends of a footer)
    if match is None:
        return None
    first = int(match.group(1))
    if match.group(2) is None:
        return first
    last = int(match.group(2))
    return last if last > first else None  # "6-4" / "4-4" are never written by the bot


def marker_of(message: Any, bot_user_id: int | None) -> int | None:
    """The highest entry id a message announced, or None when it is not one of the bot's patch notes.

    Only a message written by the bot itself, without text, with one embed titled
    :data:`MARKER_TITLES` whose footer is exactly a marker.
    """
    if bot_user_id is None or getattr(getattr(message, "author", None), "id", None) != bot_user_id:
        return None
    if getattr(message, "content", ""):
        return None
    embeds = list(getattr(message, "embeds", None) or ())
    if len(embeds) != 1:
        return None
    embed = embeds[0]
    if getattr(embed, "title", None) not in MARKER_TITLES:
        return None
    footer = getattr(embed, "footer", None)
    return parse_marker(getattr(footer, "text", None))


# -- the messages -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Post:
    """One message to post: an embed with ``description`` and ``footer``."""

    description: str
    footer: str
    #: Entries whose last line is in this message (the marker stands for them).
    covers: tuple[int, ...] = ()
    #: For a part of an entry too long for one message: that entry's id.
    part_of: int | None = None

    @property
    def last_id(self) -> int | None:
        return max(self.covers) if self.covers else None

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=MSG_TITLE, description=self.description, colour=COLOUR)
        embed.set_footer(text=self.footer)
        return embed


def fits(description: str, footer: str) -> bool:
    """True when an embed with :data:`MSG_TITLE`, this description and footer is within Discord's limits."""
    title, text, foot = discord_len(MSG_TITLE), discord_len(description), discord_len(footer)
    return (title <= EMBED_TITLE_LIMIT and text <= EMBED_DESCRIPTION_LIMIT and foot <= EMBED_FOOTER_LIMIT
            and title + text + foot <= EMBED_TOTAL_LIMIT)


def header(note: Note, *, continued: bool = False) -> str:
    """``📅 **21.11.2026** - **Title**`` (the day the players read, the title when there is one)."""
    day = Date.fromisoformat(note.date).strftime("%d.%m.%Y")
    text = f"📅 **{day}**"
    if note.title:
        text += f" - **{note.title}**"
    return text + (MSG_CONTINUED if continued else "")


def block(note: Note) -> str:
    """The whole entry: its header and one bullet per change."""
    return "\n".join([header(note), *(BULLET + change for change in note.changes)])


def _split(note: Note) -> list[Post]:
    """An entry too long for one message, split between its lines; only the last part is marked."""
    final_footer = marker(note.id)
    longest = max((FOOTER_TO_BE_CONTINUED, final_footer), key=discord_len)
    parts: list[tuple[str, list[str]]] = []
    head, lines = header(note), []
    for change in note.changes:
        bullet = BULLET + change
        if not fits(f"{head}\n{bullet}", longest):
            raise ValueError(f"line of entry {note.id} cannot fit into one message")
        if lines and not fits("\n".join([head, *lines, bullet]), longest):
            parts.append((head, lines))
            head, lines = header(note, continued=True), []
        lines.append(bullet)
    parts.append((head, lines))
    posts = [Post("\n".join([part_head, *part_lines]), FOOTER_TO_BE_CONTINUED, (), note.id)
             for part_head, part_lines in parts[:-1]]
    last_head, last_lines = parts[-1]
    posts.append(Post("\n".join([last_head, *last_lines]), final_footer, (note.id,), note.id))
    return posts


def plan_posts(notes: Iterable[Note]) -> list[Post]:
    """The messages announcing ``notes`` (oldest first): as few as Discord's limits allow.

    Whole entries are packed together; a message is closed before an entry that would not fit
    (messages split on entry boundaries).  Only an entry too long for a message on its own is
    split between its lines (:func:`_split`).  Every line appears exactly once, in order.
    """
    posts: list[Post] = []
    group: list[Note] = []

    def joined(entries: list[Note]) -> str:
        return "\n\n".join(block(entry) for entry in entries)

    def flush() -> None:
        if group:
            posts.append(Post(joined(group), marker(group[0].id, group[-1].id), tuple(n.id for n in group)))
            group.clear()

    for note in notes:
        if not fits(block(note), marker(note.id)):
            flush()
            posts.extend(_split(note))
            continue
        if group and not fits(joined([*group, note]), marker(group[0].id, note.id)):
            flush()
        group.append(note)
    flush()
    return posts


# -- working out and posting ------------------------------------------------------------------

@dataclass
class Outcome:
    """What one run found and did (for the owner's note and the ``$patchnotes`` answer)."""

    channel: str = ""
    from_id: int | None = None
    total: int = 0
    notes_error: str | None = None
    bad_from: bool = False
    channel_error: str | None = None
    history_error: str | None = None
    channel_value: int | None = None
    local_value: int | None = None
    highest: int | None = None
    #: The run stopped before posting: history unreadable and nothing remembered.
    blind: bool = False
    #: ``$patchnotes od`` refused: these entries were never announced and would be jumped over.
    gap: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)
    blocked: tuple[int, str] | None = None
    blocked_more: bool = False
    sent: list[int] = field(default_factory=list)
    messages: int = 0
    left: list[int] = field(default_factory=list)
    send_error: str | None = None
    remember_error: str | None = None
    unexpected: str | None = None

    @property
    def beyond(self) -> bool:
        """The announced number is past the file's last entry (the file was rolled back / renumbered).

        Only on an ordinary run: ``$patchnotes od`` is the way to put it right.
        """
        return (self.from_id is None and self.notes_error is None and self.highest is not None
                and self.highest > self.total)

    @property
    def problems(self) -> list[str]:
        """What went wrong, in Polish (empty: all is well)."""
        lines = []
        if self.unexpected:
            lines.append(MSG_OWNER_UNEXPECTED.format(error=self.unexpected))
        if self.notes_error:
            lines.append(MSG_OWNER_FILE_BROKEN.format(problem=self.notes_error))
        if self.channel_error:
            lines.append(MSG_OWNER_NO_CHANNEL.format(channel=self.channel, error=self.channel_error))
        if self.history_error:
            if self.blind:
                lines.append(MSG_OWNER_NO_HISTORY.format(channel=self.channel, error=self.history_error))
            elif self.from_id is None and self.local_value is not None:
                lines.append(MSG_OWNER_HISTORY_FALLBACK.format(channel=self.channel, error=self.history_error,
                                                               local=self.local_value))
        if self.beyond:
            template = MSG_OWNER_BEYOND if self.total else MSG_OWNER_BEYOND_EMPTY
            lines.append(template.format(highest=self.highest, last=self.total))
        if self.blocked is not None:
            note_id, word = self.blocked
            lines.append(MSG_OWNER_SPOILER.format(id=note_id, word=word,
                                                  after=MSG_OWNER_SPOILER_AFTER if self.blocked_more else ""))
        if self.send_error:
            lines.append(MSG_OWNER_SEND_FAILED.format(channel=self.channel, error=self.send_error,
                                                      left=entries_text(self.left)))
        if self.remember_error:
            lines.append(MSG_OWNER_REMEMBER_FAILED.format(error=self.remember_error))
        return lines


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count == 1:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def ranges(ids: Iterable[int]) -> str:
    """``[4, 5, 6, 8]`` -> ``"4-6, 8"``."""
    values = sorted(set(ids))
    spans: list[str] = []
    index = 0
    while index < len(values):
        end = index
        while end + 1 < len(values) and values[end + 1] == values[end] + 1:
            end += 1
        spans.append(str(values[index]) if end == index else f"{values[index]}-{values[end]}")
        index = end + 1
    return ", ".join(spans)


def entries_text(ids: list[int]) -> str:
    """``"2 wpisy (nr 5-6)"``."""
    count = len(set(ids))
    return f"{count} {_plural(count, 'wpis', 'wpisy', 'wpisów')} (nr {ranges(ids)})"


def _lock(bot: Any) -> asyncio.Lock:
    """The one lock of the "work out + post" section (start-up and ``$patchnotes``)."""
    lock = getattr(bot, "_patch_notes_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bot._patch_notes_lock = lock
    return lock


def busy(bot: Any) -> bool:
    """True while a run is posting (a second one waits for it)."""
    return _lock(bot).locked()


def _describe(exc: BaseException) -> str:
    return type(exc).__name__


def local_value(bot: Any) -> int | None:
    """The remembered highest announced id; None when nothing (usable) is remembered."""
    try:
        value = bot.store.runtime_get(RUNTIME_KEY)
    except Exception:  # noqa: BLE001 - an unreadable file is "nothing remembered"
        log.exception("Cannot read the remembered patch notes number.")
        return None
    if value is None:
        return None
    if type(value) is not int or value < 0:
        log.warning("Ignoring the remembered patch notes number %r (not a whole number).", value)
        return None
    return value


def _remember(bot: Any, value: int, outcome: Outcome) -> None:
    try:
        bot.store.runtime_set(RUNTIME_KEY, int(value))
    except Exception as exc:  # noqa: BLE001 - the channel marker still tells the next start
        log.exception("Cannot remember the patch notes number %s.", value)
        outcome.remember_error = _describe(exc)


async def _resolve_channel(bot: Any) -> Any:
    """The patch-notes channel (from the cache, else from Discord: an archived thread is not cached)."""
    channel_id = bot.ids.patch_notes_channel
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await io.retry(lambda: bot.fetch_channel(channel_id), what="fetch the patch notes channel")
    if not callable(getattr(channel, "send", None)) or not callable(getattr(channel, "history", None)):
        raise LookupError(f"{type(channel).__name__} is not a text channel")
    return channel


async def read_channel_marker(bot: Any, channel: Any) -> int | None:
    """The id announced by the bot's NEWEST patch-notes message among the newest :data:`HISTORY_LIMIT`.

    The newest one, not the highest one ("sprawdzaj, które zmiany w ostatniej wiadomości zostały
    wysłane"): after ``$patchnotes od`` that message is the state, also when the file was rolled
    back and an older message names a higher number.  A read that fails for a network reason is
    tried again for up to :data:`HISTORY_PATIENCE` seconds (falling back to the local number at
    once could repeat entries whose local write was lost); a permanent error (no permission)
    raises at once.  Also raises when the bot does not know who it is: its own messages could
    not be told apart, and "nothing found" would post everything again.
    """
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    if bot_id is None:
        raise LookupError("the bot's own user is unknown")

    async def scan() -> int | None:
        async for message in channel.history(limit=HISTORY_LIMIT):
            value = marker_of(message, bot_id)
            if value is not None:
                return value
        return None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + HISTORY_PATIENCE
    delay = VERIFY_FIRST_DELAY
    while True:
        try:
            return await asyncio.wait_for(scan(), io.ATTEMPT_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - sorted here
            if not io.is_transient(exc) or loop.time() + delay > deadline:
                raise
            log.warning("Reading the patch notes history failed (%s: %s); trying again in %.0fs.",
                        type(exc).__name__, exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, VERIFY_MAX_DELAY)


def _same_post(message: Any, post: Post, bot_id: int | None, since: Any) -> bool:
    """True when ``message`` is the bot's ``post`` (the same footer and text) created after ``since``."""
    if bot_id is None or getattr(getattr(message, "author", None), "id", None) != bot_id:
        return False
    created = getattr(message, "created_at", None)
    if created is not None and created < since:
        return False
    for embed in getattr(message, "embeds", None) or ():
        footer = getattr(getattr(embed, "footer", None), "text", None) or ""
        if footer.strip() == post.footer.strip() and (embed.description or "").strip() == post.description.strip():
            return True
    return False


async def _copies(channel: Any, post: Post, bot_id: int | None, since: Any, kept: set[int]) -> list[Any]:
    """The messages among the newest :data:`VERIFY_LIMIT` that are ``post``, oldest first.

    ``kept``: ids of the messages this run has already delivered - never counted (two parts of
    one long entry may read the same).
    """
    found = [message async for message in channel.history(limit=VERIFY_LIMIT)
             if getattr(message, "id", None) not in kept and _same_post(message, post, bot_id, since)]
    return sorted(found, key=lambda message: (getattr(message, "created_at", None) or since,
                                              getattr(message, "id", 0)))


async def _drop_copies(channel: Any, post: Post, bot_id: int | None, since: Any, kept: set[int],
                       sent: Any = None) -> None:
    """After a delivered message: delete identical copies of it, keep the oldest one.

    discord.py repeats a send by itself after a 500 / 502 / 504 / 524 answer or a connection
    reset, and Discord may have created the message before such an answer - one ``send`` can
    then leave two or more copies.  Best effort: when the history cannot be read or a delete
    fails, the copies stay (logged), the run goes on.
    """
    try:
        copies = await asyncio.wait_for(_copies(channel, post, bot_id, since, kept), io.ATTEMPT_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - only a clean-up
        log.warning("Cannot look for copies of the patch notes message %r (%s).", post.footer, type(exc).__name__)
        copies = []
    if copies:
        kept.add(copies[0].id)
        for copy in copies[1:]:
            if await io.safe_delete(copy):
                log.warning("Deleted a duplicate patch notes message %r (repeated by discord.py).", post.footer)
            else:
                log.error("Cannot delete a duplicate patch notes message %r.", post.footer)
                kept.add(copy.id)
    if sent is not None and getattr(sent, "id", None) is not None and not copies:
        kept.add(sent.id)  # (not visible in the history yet: it is this run's message all the same)


async def _deliver(bot: Any, channel: Any, post: Post, kept: set[int] | None = None) -> None:
    """Post one message; raises when it cannot be delivered.  Never posts it twice knowingly.

    A permanent error (no permission, channel gone, a request Discord refuses) raises at once.
    After a network error the message is not simply sent again - the request may have been
    carried out although its answer was lost (``io.send`` would repeat it): the newest
    messages are checked first, and only when it is not there is it sent again, for up to
    :data:`SEND_PATIENCE` seconds.  When that check is impossible (the history cannot be read
    for a permanent reason) the bot gives up rather than risk a duplicate.  Once delivered,
    copies made by discord.py's own repeats are deleted (:func:`_drop_copies`).  ``kept``
    collects the ids of the messages this run delivered.
    """
    kept = set() if kept is None else kept
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SEND_PATIENCE
    delay = VERIFY_FIRST_DELAY
    since = discord.utils.utcnow() - VERIFY_CLOCK_SLACK
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    while True:
        try:
            sent = await io.send(channel, embed=post.embed(), allowed_mentions=NO_MENTIONS, attempts=1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - sorted below
            if not io.is_transient(exc):
                raise
            failure = exc
            log.warning("Patch notes message failed (%s: %s); checking whether it arrived.", type(exc).__name__, exc)
        else:
            await _drop_copies(channel, post, bot_id, since, kept, sent)
            return
        while True:  # did it arrive after all?
            if loop.time() + delay > deadline:
                raise failure
            await asyncio.sleep(delay)
            delay = min(delay * 2, VERIFY_MAX_DELAY)
            try:
                arrived = await _copies(channel, post, bot_id, since, kept)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not io.is_transient(exc):
                    log.error("Cannot check whether a patch notes message arrived (%s); giving up.",
                              type(exc).__name__)
                    raise failure from exc
                continue  # still offline: look again later
            break
        if arrived:
            log.info("The patch notes message %r did arrive; not sending it again.", post.footer)
            await _drop_copies(channel, post, bot_id, since, kept)
            return


async def announce(bot: "AlterBot", *, from_id: int | None = None) -> Outcome:
    """Work out what waits and post it (``from_id``: post again from that entry on).  Never raises.

    The whole run holds :func:`_lock`: a second run (the start-up and ``$patchnotes`` at
    the same moment, two commands) waits and then finds nothing left.
    """
    async with _lock(bot):
        outcome = Outcome(from_id=from_id)
        try:
            await _announce(bot, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - told, never raised into the start-up / the command
            log.exception("Patch notes failed.")
            outcome.unexpected = _describe(exc)
        return outcome


async def _announce(bot: Any, outcome: Outcome) -> None:
    channel_id = bot.ids.patch_notes_channel
    outcome.channel = f"<#{channel_id}>"
    try:
        notes = load_notes()
    except NotesError as exc:
        outcome.notes_error = str(exc)
        log.error("The patch notes file is broken: %s", exc)
        return
    outcome.total = len(notes)
    outcome.local_value = local = local_value(bot)
    if outcome.from_id is not None and not 1 <= outcome.from_id <= len(notes):
        outcome.bad_from = True
        return
    try:
        channel = await _resolve_channel(bot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - not found / no access / offline
        log.error("Patch notes channel %s unavailable: %s: %s", channel_id, type(exc).__name__, exc)
        outcome.channel_error = _describe(exc)
        return
    history_ok = True
    try:
        outcome.channel_value = await read_channel_marker(bot, channel)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - no permission / offline
        log.error("Cannot read the patch notes history: %s: %s", type(exc).__name__, exc)
        outcome.history_error = _describe(exc)
        history_ok = False
    known = [value for value in (outcome.channel_value, local) if value is not None]
    outcome.highest = max(known) if known else None
    # The remembered number is never lowered by a run that does not finish (a later history
    # outage would then fall back to a number below what is on the channel and post entries again).
    floor = outcome.highest or 0
    if outcome.from_id is None:
        if not history_ok and local is None:
            outcome.blind = True
            return
        start = floor + 1
        if outcome.beyond:
            log.error("Patch notes: announced number %d is past the last entry %d of the file.",
                      outcome.highest, outcome.total)
        if outcome.highest is not None and local != outcome.highest:
            _remember(bot, outcome.highest, outcome)  # (the local file was lost: the channel told)
    else:
        if outcome.from_id > floor + 1:  # (would jump over entries that were never announced)
            outcome.gap = list(range(floor + 1, outcome.from_id))
            return
        start = outcome.from_id
    waiting = [note for note in notes if note.id >= start]
    outcome.waiting = [note.id for note in waiting]
    words = forbidden_words()
    postable: list[Note] = []
    for index, note in enumerate(waiting):
        word = spoiler_in(note, words)
        if word is not None:
            outcome.blocked = (note.id, word)
            outcome.blocked_more = index + 1 < len(waiting)
            log.error("Patch notes entry %d refused: spoiler %r.", note.id, word)
            break
        postable.append(note)
    posts = plan_posts(postable)
    delivered: set[int] = set()
    kept: set[int] = set()
    for post in posts:
        try:
            await _deliver(bot, channel, post, kept)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - what was delivered stays remembered
            log.error("Patch notes: cannot post (%s: %s); %d message(s) delivered.",
                      type(exc).__name__, exc, outcome.messages)
            outcome.send_error = _describe(exc)
            break
        outcome.messages += 1
        if post.covers:
            delivered.update(post.covers)
            outcome.sent.extend(post.covers)
            # After EACH message: a failure later repeats nothing.
            _remember(bot, max(floor, post.last_id), outcome)
    outcome.left = [note.id for note in postable if note.id not in delivered]
    if (outcome.from_id is not None and outcome.sent and outcome.send_error is None and outcome.blocked is None
            and max(outcome.sent) < floor):
        # ``od`` went through to the end of a file shorter than the announced number (rolled back /
        # renumbered): from now on the file's last entry is the announced one.
        _remember(bot, max(outcome.sent), outcome)
    if outcome.sent:
        log.info("Patch notes posted: %s in %d message(s).", ranges(outcome.sent), outcome.messages)


def owner_note(outcome: Outcome) -> str | None:
    """The one note for the owner after the start-up run (None: nothing worth saying)."""
    lines = list(outcome.problems)
    if outcome.sent:
        lines.insert(0, MSG_OWNER_SENT.format(what=entries_text(outcome.sent), channel=outcome.channel))
    return "\n".join(lines) if lines else None


def _status_line(outcome: Outcome) -> str:
    """The highest announced id and where that number comes from."""
    if outcome.highest is None:
        return MSG_STATUS_NONE if outcome.history_error is None else MSG_STATUS_UNKNOWN
    if outcome.history_error is not None:
        channel_text = MSG_UNREADABLE
    elif outcome.channel_value is not None:
        channel_text = f"nr {outcome.channel_value}"
    else:
        channel_text = MSG_NOTHING_THERE
    local_text = f"nr {outcome.local_value}" if outcome.local_value is not None else MSG_NOTHING_THERE
    if outcome.channel_value == outcome.local_value:
        source = MSG_SOURCE_BOTH
    elif outcome.highest == outcome.channel_value:
        source = MSG_SOURCE_CHANNEL.format(local=local_text)
    else:
        source = MSG_SOURCE_LOCAL.format(channel=channel_text)
    return MSG_STATUS.format(highest=outcome.highest, source=source)


def command_answer(outcome: Outcome) -> str:
    """The answer to ``$patchnotes`` / ``$patchnotes od <id>`` (Polish)."""
    if outcome.unexpected or outcome.notes_error:
        return "\n".join(MSG_PROBLEM.format(problem=problem) for problem in outcome.problems)
    if outcome.bad_from:
        return (MSG_BAD_FROM.format(start=outcome.from_id, total=outcome.total) if outcome.total
                else MSG_BAD_FROM_EMPTY)
    lines = []
    if outcome.channel_error is None:
        lines.append(_status_line(outcome))
    lines.append(MSG_FILE_STATE.format(total=outcome.total))
    if outcome.gap:
        lines.append(MSG_FROM_GAP.format(gap=ranges(outcome.gap), next=outcome.gap[0]))
    elif outcome.from_id is not None:
        if outcome.waiting:
            highest = outcome.highest or 0
            again = [note_id for note_id in outcome.waiting if note_id <= highest]
            new = [note_id for note_id in outcome.waiting if note_id > highest]
            parts = ([MSG_FROM_AGAIN.format(what=entries_text(again))] if again else []) + (
                [MSG_FROM_NEW.format(what=entries_text(new))] if new else [])
            lines.append(MSG_FROM.format(start=outcome.from_id, what=", ".join(parts)))
    elif not outcome.blind and outcome.channel_error is None:
        lines.append(MSG_WAITING.format(what=entries_text(outcome.waiting)) if outcome.waiting
                     else MSG_NOTHING_WAITS)
    if outcome.sent:
        messages = f"w {outcome.messages} {'wiadomości' if outcome.messages == 1 else 'wiadomościach'}"
        lines.append(MSG_SENT.format(what=entries_text(outcome.sent), messages=messages, channel=outcome.channel))
    lines += [MSG_PROBLEM.format(problem=problem) for problem in outcome.problems]
    return "\n".join(lines)


async def start_up(bot: "AlterBot") -> None:
    """Once per process, after the bot is ready: post what waits, one note for the owner.  Never raises."""
    if getattr(bot, "_patch_notes_started", False):
        return
    bot._patch_notes_started = True
    try:
        outcome = await announce(bot)
        note = owner_note(outcome)
        if note:
            await bot.log_to_owner(note, ping=bool(outcome.problems))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the game goes on
        log.exception("Patch notes at start-up failed.")
