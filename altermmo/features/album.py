"""The pet album: sacrificed companions kept for good, and a collection to complete.

Owner's request of 2026-11-21 ("coś w stylu osiągnięć") and his answers:

* The album is for **supporters** (:func:`patrons.check_if_patron`).  Every
  sacrifice of a supporter's pet (``$porzucam`` / the pet panel's "Porzuć",
  :func:`pets.discard_pet`) saves the pet here - always, automatically, eggs
  too.  The status counts at the moment of the sacrifice.  A non-supporter's
  sacrificed pet is lost, as it always was.  A former supporter keeps his
  entries (nothing is ever deleted behind a player's back); he sees them again
  when he supports again.
* The **collection** is every non-mythic species (:class:`pets.PetType` without
  :data:`pets.MYTHIC_EGGS`) at every stage - the egg (level 0) and the levels
  1 .. :data:`pets.MAX_LEVEL`: 17 x 4 = 68 slots today, computed from those
  constants (:func:`collection_slots`).  Variant, class, frame, seal, foil and
  name do not matter for a slot; several entries of one slot are welcome (a
  rare frame ...).  The mythic pet can be kept, it is not part of the
  collection (it is listed apart).
* An entry can be **removed** by its owner, after a confirmation, for good
  (:func:`remove`: by ``ENTRY_ID`` *and* ``PLAYER_ID``, so a double click or
  two panels never delete two entries or somebody else's).
* A full collection earns the achievement "Pełny album" (``album_full`` in
  :mod:`altermmo.features.achievements`); it is never taken away, also not when
  entries are removed later.

Table ``PET_ALBUM`` (new in v2, made at start-up by ``schema.create_new_tables``;
the database is shared with other applications - this module touches only this
table, and ``PETS`` only through :func:`pets.discard_pet`)::

    ENTRY_ID BIGINT identity PRIMARY KEY, PLAYER_ID NUMERIC NOT NULL,
    ADDED_AT TIMESTAMPTZ (the moment of the sacrifice), + SNAPSHOT_COLUMNS

The snapshot holds the pet's ``PETS`` row under the SAME column names, so an
entry is handed as it is to ``pets._pet_image`` / ``pets._pet_card`` /
``petcards.look_of``: a card in the album is drawn exactly like the pet was
(and new card ornaments show up here without a change in this module).
Columns that come later (the frame, the partial foil) go into
:data:`LATER_COLUMNS` only: they are then part of the ``CREATE TABLE`` and of
:data:`NEW_COLUMNS` (added to a table made earlier at the next start), and
:func:`keep` copies whatever of :data:`SNAPSHOT_COLUMNS` the pet's row has - a
column ``PETS`` does not have (yet) gets its default.

API (everything takes a database / a connection, nothing caches):

* :func:`keep` ``(con, player_id, pet) -> entry id`` - inside the caller's
  transaction (the one that deletes the pet: in ``PETS`` or in the album, never
  both, never neither); raises :class:`NotSaved` when the row cannot be written,
  which must fail the sacrifice;
* :func:`entries` (album order), :func:`entry`, :func:`remove`;
* :func:`collected`, :func:`progress`, :func:`complete` - the collection maths;
* :func:`note_new_egg` ``(scene, player, egg)`` - the note at an egg drop (never raises);
* :func:`show_album` - ``$album`` / the hub's "Album" button: an overview and a
  :class:`AlbumPanel` to page through the entries (one card per page).  Since
  2026-11-22 the overview also shows the collection's value and the place in
  the ranking of the supporters' collections, and an entry's page the card's
  value (the score and the ranking: :mod:`altermmo.features.albumrank`).

Owner's rules for what is shown: species the player has nothing of are never
named (only how many are still undiscovered), and nothing explains which card
ornaments exist - an entry names only what it has.

**An egg never tells its species** (owner, 2026-11-23: "Przy przeglądaniu albumu
gdy wyświetla się jajko to nie powinien być znany jeszcze gatunek").  In the game
an egg's species is unknown until it hatches (``pets._pet_embed``: an egg's title
is just "Jajko"), so an egg entry (stage 0, :func:`is_egg`) is never attributed
to a species anywhere a player can see:

* its page and the texts of its removal say "Jajko" (+ the pet's own name), never
  the species (:func:`entry_embed`, :func:`describe`); its card is the one
  ``$towarzysz`` shows for the living egg (the same ``pets._pet_card``), and the
  name of the card's file - every player can read it - does not carry the folder
  of the egg's species (``petcards._label``, for the living egg as well);
* the overview names a species only when the album holds a HATCHED stage of it
  and lists per species only the levels 1 .. :data:`pets.MAX_LEVEL`; the eggs are
  one line of their own, counted (they are slots of the collection as before) but
  not attributed; "Nieodkryte gatunki" counts the species without a hatched stage;
* the egg entries are one group after every hatched pet, in the order they were
  kept (:func:`_album_order`) - between the pages of their species the neighbours
  would give the species away; the species menu lists the hatched species and one
  entry "Jajka".

This is display only: the stored rows, the slots, the collection's total, "Pełny
album", the values and the ranking are what they were.

**A new egg and the album** (owner, 2026-11-23: "Dla wspierających dodawaj także
informację gdy wypadnie komuś jajko, czy ma je już w kolekcji, albo czy następne
fazy ewolucji mogą uzupełnić jego kolekcję. Jeśli ma coś to podaj czy to holo z
ramką etc."): :func:`note_new_egg`, called by ``pets.assign_pet`` once the egg is
stored, posts ONE message for a supporter - per stage of the egg's species what
the album holds (the card that counts, by what it has) or that it is missing, and
which stages this companion can still complete.  The species is not named there
either.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

import asyncpg
import discord

from ..core import challenge, ui, waiting
from ..core.db import DatabaseUnavailable
from . import hub, patrons, petcards, pets

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

# -- the table ---------------------------------------------------------------------------

TABLE = "PET_ALBUM"

#: The pet's ``PETS`` row as it is copied, ``(column, definition)`` in the order of ``PETS``.
#: Definitions as in ``PETS`` (pets.SCHEMA), so a value of ``PETS`` always fits.
BASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("PET_ID", "NUMERIC"),
    ("PET_NAME", "VARCHAR(255)"),
    ("PET_LVL", "NUMERIC"),
    ("PET_SKILLS", "NUMERIC"),
    ("QUALITY", "VARCHAR(255)"),
    ("SHINY", "BOOLEAN"),
    ("TYPE", "VARCHAR(255)"),
    ("VARIANT", "VARCHAR(255)"),
    ("CRIT_PERC", "NUMERIC"),
    ("REPLACE_PERC", "NUMERIC"),
    ("DEF_PERC", "NUMERIC"),
    ("DROP_PERC", "NUMERIC"),
    ("LOWHP_PERC", "NUMERIC"),
    ("SLOW_PERC", "NUMERIC"),
    ("INIT_PERC", "NUMERIC"),
    ("DETECTION", "BOOLEAN"),
    ("ULTRA_SHINY", "BOOLEAN"),
    ("MYTHIC", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("SEAL", "NUMERIC NOT NULL DEFAULT 0"),
    ("HOLO", "BOOLEAN NOT NULL DEFAULT FALSE"),
)
#: Columns of ``PETS`` that came after the album's table was first made (each one needs a
#: default: it is also added to an existing table, see :data:`NEW_COLUMNS`): the frame result
#: and the partial Holo of 2026-11-21 (definitions as in ``pets.NEW_COLUMNS``).
LATER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("FRAME", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("HOLO_PARTIAL", "BOOLEAN NOT NULL DEFAULT FALSE"),
)
#: The defaults of :data:`LATER_COLUMNS`.  When such a column could not be added to an existing
#: album (start-up, ``schema.add_new_columns``), :func:`keep` leaves it out for a pet whose value
#: IS the default (nothing is lost); a pet that really has a frame / partial Holo is not kept -
#: the sacrifice is refused (:class:`NotSaved`) rather than the entry losing its ornament.
LATER_DEFAULTS: dict[str, Any] = {"FRAME": "", "HOLO_PARTIAL": False}
#: Every copied column (:func:`keep` copies those the pet's row has).
SNAPSHOT_COLUMNS: tuple[tuple[str, str], ...] = BASE_COLUMNS + LATER_COLUMNS

SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
           ENTRY_ID BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
           PLAYER_ID NUMERIC NOT NULL,
           ADDED_AT TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
           {(',' + chr(10) + '           ').join(f'{column} {definition}' for column, definition in SNAPSHOT_COLUMNS)}
       )""",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_PLAYER_ID_IDX ON {TABLE} (PLAYER_ID)",
)
#: New in v2: the old bot's database has no album (``schema.create_new_tables``).
NEW_TABLES: tuple[str, ...] = SCHEMA
#: Columns a ``PET_ALBUM`` made by an earlier version lacks (``schema.add_new_columns``).
NEW_COLUMNS: tuple[tuple[str, str, str], ...] = tuple(
    (TABLE.lower(), column.lower(), definition) for column, definition in LATER_COLUMNS)

_SQL_ENTRIES = f"SELECT * FROM {TABLE} WHERE PLAYER_ID = $1 ORDER BY ENTRY_ID"
_SQL_ENTRY = f"SELECT * FROM {TABLE} WHERE ENTRY_ID = $1 AND PLAYER_ID = $2"
_SQL_REMOVE = f"DELETE FROM {TABLE} WHERE ENTRY_ID = $1 AND PLAYER_ID = $2 RETURNING ENTRY_ID"
_SQL_SLOTS = f"SELECT DISTINCT TYPE, PET_LVL FROM {TABLE} WHERE PLAYER_ID = $1"
#: The player's entries of ONE species (the note at an egg drop, :func:`note_new_egg`).
_SQL_OF_SPECIES = f"SELECT * FROM {TABLE} WHERE PLAYER_ID = $1 AND TYPE = $2 ORDER BY ENTRY_ID"


class NotSaved(Exception):
    """The pet could not be written into the album (the sacrifice must not go on)."""


# -- the collection ----------------------------------------------------------------------

def collection_species() -> tuple[str, ...]:
    """``TYPE`` of every species of the collection, in the order of :class:`pets.PetType`."""
    return tuple(kind.value for kind in pets.PetType if kind not in pets.MYTHIC_EGGS)


def stages() -> tuple[int, ...]:
    """The stages of a pet: 0 (the egg) .. :data:`pets.MAX_LEVEL`."""
    return tuple(range(pets.MAX_LEVEL + 1))


def collection_slots() -> frozenset[tuple[str, int]]:
    """Every ``(TYPE, level)`` the full collection needs (68 today)."""
    return frozenset((species, level) for species in collection_species() for level in stages())


def slot_count() -> int:
    return len(collection_species()) * len(stages())


def stage_name(level: int) -> str:
    """"Jajko" / "Poziom 2" - the stage as the players read it."""
    return STAGE_EGG if level == 0 else STAGE_LEVEL.format(level=level)


def slot_of(row: Any) -> tuple[str, int]:
    """``(TYPE, level)`` of an entry (or of a ``PETS`` row)."""
    return str(row["type"]), pets._int(row["pet_lvl"])


@dataclass(frozen=True)
class Progress:
    """How much of the collection an album covers."""

    #: The filled slots of the collection (what lies outside it is not counted).
    collected: frozenset[tuple[str, int]]
    total: int

    @property
    def count(self) -> int:
        return len(self.collected)

    @property
    def complete(self) -> bool:
        return self.count >= self.total

    def stages_of(self, species: str) -> tuple[int, ...]:
        """The collected stages of one species."""
        return tuple(level for level in stages() if (species, level) in self.collected)


def progress(slots: Iterable[Any]) -> Progress:
    """The collection of an album, from its entries or from ``(TYPE, level)`` pairs."""
    wanted = collection_slots()
    found = set()
    for item in slots:
        slot = item if isinstance(item, tuple) else slot_of(item)
        if slot in wanted:
            found.add(slot)
    return Progress(frozenset(found), len(wanted))


async def _fetch(db: Any, method: str, sql: str, *args: Any) -> Any:
    """A read of the album; a table that is not there (yet) holds nothing (:func:`keep` makes it).

    Its ``fetch`` gives ``[]``, its ``fetchrow`` None - not an error for every
    ``$album`` while the start-up could not make the table.
    """
    try:
        return await getattr(db, method)(sql, *args)
    except asyncpg.UndefinedTableError:
        log.warning("Table %s is missing (not made at start-up): read as empty.", TABLE)
        return [] if method == "fetch" else None


async def collected(db: Any, player_id: int) -> set[tuple[str, int]]:
    """Every ``(TYPE, level)`` the player's album holds (also outside the collection)."""
    rows = await _fetch(db, "fetch", _SQL_SLOTS, int(player_id))
    return {slot_of(row) for row in rows}


async def complete(db: Any, player_id: int) -> bool:
    """True when the player's album covers every slot of the collection."""
    return progress(await collected(db, player_id)).complete


# -- entries -----------------------------------------------------------------------------

def _columns_of(pet: Any) -> set[str]:
    try:
        return {str(key).lower() for key in pet.keys()}
    except AttributeError:
        return set()


async def keep(con: Any, player_id: int, pet: Any) -> int:
    """Write the pet's ``PETS`` row into the player's album; returns the new ``ENTRY_ID``.

    Call it in the transaction that deletes the pet.  Copied are the columns of
    :data:`SNAPSHOT_COLUMNS` the row has; a missing column or a NULL takes the
    album's default (a ``PETS`` without the cosmetic columns: no seal, no foil).
    A row the database refuses raises :class:`NotSaved` - the caller's
    transaction is then rolled back and the pet stays where it was.  An album
    made before :data:`LATER_COLUMNS` that could not get them yet still keeps
    a pet that has only their defaults (see :data:`LATER_DEFAULTS`).  A table
    that is not there at all (the database did not answer at the first start,
    so ``schema.create_new_tables`` did not run) is made here, in the same
    transaction (:data:`SCHEMA`, ``IF NOT EXISTS``) - otherwise no supporter
    could sacrifice a pet until the bot is restarted.
    """
    present = _columns_of(pet)
    columns = [column for column, _ in SNAPSHOT_COLUMNS
               if column.lower() in present and pet[column.lower()] is not None]
    try:
        try:
            entry_id = await _insert(con, player_id, pet, columns)
        except asyncpg.UndefinedTableError:
            log.warning("Table %s is missing (not made at start-up); it is made now.", TABLE)
            async with con.transaction():
                for statement in SCHEMA:
                    await con.execute(statement)
            entry_id = await _insert(con, player_id, pet, columns)
    except asyncpg.UndefinedColumnError as exc:
        later = {column for column, _ in LATER_COLUMNS}
        richer = [column for column in columns
                  if column in later and pet[column.lower()] != LATER_DEFAULTS.get(column)]
        if richer:
            raise NotSaved(f"{type(exc).__name__}: {exc}") from exc
        log.warning("PET_ALBUM lacks a later column (%s); pet %s kept without the default values of %s.",
                    exc, pet["pet_id"], ", ".join(column for column in columns if column in later))
        try:
            entry_id = await _insert(con, player_id, pet, [column for column in columns if column not in later])
        except asyncpg.PostgresError as again:
            raise NotSaved(f"{type(again).__name__}: {again}") from again
    except asyncpg.PostgresError as exc:
        raise NotSaved(f"{type(exc).__name__}: {exc}") from exc
    log.info("Album of %s: pet %s kept as entry %s.", player_id, pet["pet_id"], entry_id)
    return int(entry_id)


async def _insert(con: Any, player_id: int, pet: Any, columns: Sequence[str]) -> Any:
    """One INSERT of the snapshot ``columns`` of ``pet``, in a savepoint (a refusal leaves the
    caller's transaction usable)."""
    values = [pet[column.lower()] for column in columns]
    names = "".join(f", {column}" for column in columns)
    marks = "".join(f", ${index}" for index in range(2, len(columns) + 2))
    sql = f"INSERT INTO {TABLE} (PLAYER_ID{names}) VALUES ($1{marks}) RETURNING ENTRY_ID"
    async with con.transaction():
        return await con.fetchval(sql, int(player_id), *values)


def _species_rank(species: str) -> int:
    order = [kind.value for kind in pets.PetType]
    return order.index(species) if species in order else len(order)


def is_egg(row: Any) -> bool:
    """True for an egg entry (stage 0; a NULL level is the egg, as everywhere in the game).

    Such an entry is never attributed to a species where a player can see it (module docstring).
    """
    return slot_of(row)[1] == 0


def _album_order(row: Any) -> tuple[int, int, str, int, int]:
    """Hatched pets by species, stage and when kept; then the eggs, by when they were kept only -
    an egg between the pages of its species would be given away by its neighbours."""
    species, level = slot_of(row)
    if level == 0:
        return 1, 0, "", 0, int(row["entry_id"])
    return 0, _species_rank(species), species, level, int(row["entry_id"])


async def entries(db: Any, player_id: int) -> list[Any]:
    """The player's entries in album order: the hatched pets by species (as :class:`pets.PetType`),
    stage and when kept, then every egg in the order they were kept (:func:`_album_order`)."""
    rows = await _fetch(db, "fetch", _SQL_ENTRIES, int(player_id))
    return sorted(rows, key=_album_order)


async def entry(db: Any, player_id: int, entry_id: int) -> Any:
    """One entry of the player's album; None when it is not there (removed, or somebody else's)."""
    return await _fetch(db, "fetchrow", _SQL_ENTRY, int(entry_id), int(player_id))


async def remove(db: Any, player_id: int, entry_id: int) -> bool:
    """Remove one entry of the player's album for good; False when it was not there (any more).

    Never sent twice by the database layer (``idempotent=False``): the answer
    "removed" / "already gone" must be the truth.
    """
    removed = await db.fetchval(_SQL_REMOVE, int(entry_id), int(player_id), idempotent=False)
    if removed is not None:
        log.info("Album of %s: entry %s removed.", player_id, entry_id)
    return removed is not None


# -- texts -------------------------------------------------------------------------------

STAGE_EGG = "Jajko"
STAGE_LEVEL = "Poziom {level}"
COLLECTED_MARK, MISSING_MARK = "✅", "▫️"
ALBUM_COLOUR = 0x8E6CCF
#: Name of a species the game does not know (a row of the legacy table).
UNKNOWN_SPECIES = "Nieznany towarzysz"

MSG_SUPPORTERS_ONLY = ("Album towarzyszy jest dla wspierających ({groups}) <:PepoG:790963160528977980>\n"
                       "Wesprzyj AlterMMO: {links}")
MSG_TITLE = "📖 Album towarzyszy"
MSG_OVERVIEW = "<@{player_id}>, w Twoim albumie {entries}.\nKolekcja: **{count} z {total}**"
MSG_EMPTY = ("<@{player_id}>, Twój album jest jeszcze pusty. Każdy towarzysz, którego poświęcisz "
             "(**$porzucam**), zostanie w nim na zawsze - chyba że sam go usuniesz "
             "<:peepoBlush:984769061340737586>\nKolekcja: **0 z {total}**")
#: One species of the collection: its HATCHED stages only (an egg does not tell its species).
MSG_SPECIES_LINE = "**{species}** ({count} z {stages}): {marks}"
#: The eggs of the collection: how many of its egg slots are filled - not whose eggs they are.
MSG_EGGS_LINE = "🥚 **Jajka**: **{count}** z {total}"
#: The species without a hatched stage in the album.
MSG_UNDISCOVERED = "❔ Nieodkryte gatunki: **{count}**"
MSG_COMPLETE = "🎉 **Kolekcja skompletowana!** <:POGGERS:936907543849078844>"
MSG_OUTSIDE = "✨ **Poza kolekcją:**"
MSG_OUTSIDE_LINE = "{species}: {stages}"
#: Eggs outside the collection (a mythic one ...): counted, not attributed.
MSG_OUTSIDE_EGGS = "🥚 {stage} ×{count}"
MSG_OVERVIEW_FOOTER = "Przeglądaj album przyciskami pod spodem."
MSG_ENTRY_FOOTER = "Usunięcia wpisu z albumu nie da się cofnąć."
MSG_POSITION = "Wpis **{position} z {total}**"
#: The list under the album: only the species the player has HATCHED entries of, and the eggs
#: (so not "... do gatunku": its entry "Jajka" is no species).
MSG_SPECIES_PLACEHOLDER = "Przejdź do…"
#: The list's entry for the group of eggs, and its value (no species is called so).
MSG_EGGS_OPTION = "Jajka"
EGGS_OPTION = "#eggs"
MSG_ADDED = "W albumie od: {when}"
CLASS_NAMES: dict[str, str] = {"M": "Mityczny", "U": "Ultra Shiny", "S": "Shiny", "": "Zwykły"}
CLASS_COLOURS: dict[str, int] = {"M": 0xFF4815, "U": 0xFF8D15, "S": 0xFFDD00, "": 0xFFFFFC}

MSG_REMOVE_ASK = ("<@{player_id}>, czy na pewno chcesz usunąć z albumu **{what}**{when}? Tego nie da się "
                  "cofnąć - wpis przepadnie na zawsze. <:MonkaS:882181709100097587> " + pets.CONFIRM_HINT)
#: ``{when}`` of :data:`MSG_REMOVE_ASK`: the moment the entry was kept (as on the entry's page).
MSG_REMOVE_WHEN = " (w albumie od {when})"
MSG_REMOVED = "Wpis **{what}** został usunięty z albumu na zawsze, <@{player_id}>."
MSG_REMOVE_CANCELLED = "*Wpis zostaje w albumie.*"
MSG_ALREADY_GONE = "Tego wpisu nie ma już w albumie, <@{player_id}> - został usunięty wcześniej."

#: The sacrifice (``pets.discard_pet``) of a supporter: in the question, before ``pets.CONFIRM_HINT`` ...
MSG_DISCARD_KEPT = "Jako wspierający zachowasz go w swoim albumie (**$album**). "
#: ... and after it.
MSG_DISCARD_SAVED = ("Twój towarzysz trafił do albumu, <@{player_id}> <:peepoBlush:984769061340737586> "
                     "Zajrzyj do niego: **$album**.")
#: The album could not be written: nothing was sacrificed.
MSG_NOT_SAVED = ("Nie udało się zapisać towarzysza w albumie, więc nic nie zostało porzucone, <@{player_id}> "
                 "<:Sadge:936907659142111273> Spróbuj ponownie za chwilę.")
LOG_NOT_SAVED = ("Album: nie udało się zapisać towarzysza (PET_ID {pet_id}) gracza <@{player_id}> - "
                 "poświęcenie odwołane, towarzysz został u gracza. Błąd: `{error}`")


def _plural_pets(number: int) -> str:
    """``jest **1** towarzysz`` / ``są **3** towarzysze`` / ``jest **5** towarzyszy``."""
    if number == 1:
        return "jest **1** towarzysz"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f"są **{number}** towarzysze"
    return f"jest **{number}** towarzyszy"


def _entries_word(number: int) -> str:
    if number == 1:
        return "1 wpis"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f"{number} wpisy"
    return f"{number} wpisów"


def species_name(species: str) -> str:
    return pets.POLISH_TYPES.get(species, UNKNOWN_SPECIES)


#: How much of a pet's name the album shows (before escaping; escaped it is at most twice as long).
NAME_SHOWN = 100


def _folded_name(row: Any) -> str:
    """The pet's name on one line: line breaks and runs of whitespace are single spaces, cut to
    :data:`NAME_SHOWN` ("" for a name of blanks only)."""
    return " ".join(str(row["pet_name"] or "").split())[:NAME_SHOWN]


def shown_name(row: Any) -> str:
    """The pet's name as plain text wherever Discord renders markdown (an embed's title and
    description, a message): :func:`_folded_name` with its markdown escaped
    (``discord.utils.escape_markdown``, as features/pets.py shows a name; it escapes
    backslashes too, so a name ending in one cannot escape the closing ``**`` around it).
    Mentions in it are not neutralised here - a message carrying it is sent with
    ``allowed_mentions``."""
    return discord.utils.escape_markdown(_folded_name(row))


def _named(row: Any) -> bool:
    """True when the pet has a name of its own (not the default one, not only blanks)."""
    name = _folded_name(row)
    return bool(name) and name != pets.DEFAULT_NAME


def describe(row: Any) -> str:
    """``Kot Mruczek (Poziom 2)`` - one entry in a sentence (plain text, see :func:`shown_name`).

    An egg is ``Jajko`` / ``Jajko Mruczek``, as ``$towarzysz`` titles it: never its species.
    """
    if is_egg(row):
        return f"{STAGE_EGG} {shown_name(row)}" if _named(row) else STAGE_EGG
    title = species_name(str(row["type"]))
    if _named(row):
        title += " " + shown_name(row)
    return f"{title} ({stage_name(pets._int(row['pet_lvl']))})"


def supporters_only_text(member: Any) -> str:
    links = " • ".join(f"[{label}](<{url}>)" for label, url in patrons.SUPPORT_LINKS)
    text = MSG_SUPPORTERS_ONLY.format(groups=patrons.SUPPORTERS_LONG, links=links)
    return f"<@{member.id}> {text}" if member is not None else text


def _value_lines(value: Any) -> list[str]:
    """The collection's value and the place in the ranking (:class:`albumrank.Overview`; None:
    nothing).  No line for an album that scores nothing, no place line when there is none."""
    from . import albumrank

    if value is None or value.points <= 0:
        return []
    lines = [albumrank.MSG_VALUE.format(points=albumrank.points_text(value.points))]
    if not value.available:
        lines.append(albumrank.MSG_PLACE_UNKNOWN)
    elif value.place is not None:
        lines.append(albumrank.MSG_PLACE.format(place=value.place, total=value.total))
    return lines


def overview_embed(player_id: int, rows: Sequence[Any], value: Any = None) -> discord.Embed:
    """The album at a glance: how many entries, the collection, its value, what lies outside it.

    A species of the collection is named only when the player has a HATCHED
    stage of it (its line shows the levels 1 .. MAX only); the others are only
    counted.  The eggs are one line: how many egg slots of the collection are
    filled, never whose eggs they are (module docstring) - so the species lines
    and the eggs line add up to the collection's count.  ``value`` is what
    :func:`albumrank.overview_of` says (the points and the place; None: no such lines).
    """
    total = slot_count()
    if not rows:
        return discord.Embed(title=MSG_TITLE, description=MSG_EMPTY.format(player_id=player_id, total=total),
                             color=ALBUM_COLOUR)
    state = progress(rows)
    lines = [MSG_OVERVIEW.format(player_id=player_id, entries=_plural_pets(len(rows)),
                                 count=state.count, total=state.total), *_value_lines(value), ""]
    hatched_stages = [level for level in stages() if level != 0]
    discovered = []
    for species in collection_species():
        have = [level for level in state.stages_of(species) if level != 0]
        if not have:
            continue
        discovered.append(species)
        marks = " · ".join(f"{COLLECTED_MARK if level in have else MISSING_MARK} {stage_name(level)}"
                           for level in hatched_stages)
        lines.append(MSG_SPECIES_LINE.format(species=species_name(species), count=len(have),
                                             stages=len(hatched_stages), marks=marks))
    eggs = sum(1 for species in collection_species() if 0 in state.stages_of(species))
    lines.append(MSG_EGGS_LINE.format(count=eggs, total=len(collection_species())))
    undiscovered = len(collection_species()) - len(discovered)
    lines += ["", MSG_COMPLETE if state.complete else MSG_UNDISCOVERED.format(count=undiscovered)]

    outside: dict[str, dict[int, int]] = {}
    outside_eggs = 0
    wanted = collection_slots()
    for row in rows:
        species, level = slot_of(row)
        if (species, level) in wanted:
            continue
        if level == 0:  # (an egg: counted, its species is not told)
            outside_eggs += 1
            continue
        per_stage = outside.setdefault(species, {})
        per_stage[level] = per_stage.get(level, 0) + 1
    if outside or outside_eggs:
        lines += ["", MSG_OUTSIDE]
        for species in sorted(outside, key=lambda name: (_species_rank(name), name)):
            stages_text = ", ".join(f"{stage_name(level)} ×{number}"
                                    for level, number in sorted(outside[species].items()))
            lines.append(MSG_OUTSIDE_LINE.format(species=species_name(species), stages=stages_text))
        if outside_eggs:
            lines.append(MSG_OUTSIDE_EGGS.format(stage=STAGE_EGG, count=outside_eggs))
    embed = discord.Embed(title=MSG_TITLE, description="\n".join(lines)[:4096], color=ALBUM_COLOUR)
    embed.set_footer(text=MSG_OVERVIEW_FOOTER)
    return embed


def _frame_text(row: Any) -> str:
    """The frame the entry is drawn with (only one that is drawn: a mythic pet's own picture is none)."""
    try:
        frame = petcards.frame_of(row)
    except Exception:  # noqa: BLE001 - decoration: the entry is shown without the line
        return ""
    if frame is None or not getattr(frame, "file", ""):
        return ""
    return str(getattr(frame, "label", "") or getattr(frame, "name", ""))


def _card_value_lines(row: Any, rows: Sequence[Any] | None) -> list[str]:
    """The card's value and - when the album (``rows``, the player's entries) holds a card of the
    same species and stage worth more - that this one does not count (so the player knows which
    duplicates matter before he removes one).  Nothing for an entry that scores nothing."""
    from . import albumrank

    points = albumrank.card_points(row)
    if points <= 0:
        return []
    lines = [albumrank.MSG_CARD_VALUE.format(points=albumrank.points_text(points))]
    if rows and albumrank.collection(rows).beaten(row, points):
        lines.append(albumrank.MSG_NOT_COUNTED)
    return lines


def entry_embed(row: Any, position: int, total: int, rows: Sequence[Any] | None = None) -> discord.Embed:
    """One entry: what it is and what it has (its picture is set by :func:`entry_view`).

    The pet's name is plain text in the title and the description (:func:`shown_name`).
    ``rows`` (the player's entries) tell whether a better card of the slot makes this one not count.
    An egg is "Jajko" (+ its own name) and has no species line: it does not tell its species
    (module docstring).
    """
    name = shown_name(row)
    tier = petcards.tier_of(row)
    if is_egg(row):
        title, lines = (f"📖 {STAGE_EGG} {name}" if _named(row) else f"📖 {STAGE_EGG}"), []
    else:
        species = species_name(str(row["type"]))
        title = f"📖 {species} {name}" if _named(row) else f"📖 {species}"
        lines = [f"Gatunek: **{species}**"]
    if name:
        lines.append(f"Imię: **{name}**")
    lines += [f"Etap: **{stage_name(pets._int(row['pet_lvl']))}**", f"Klasa: **{CLASS_NAMES[tier]}**"]
    frame = _frame_text(row)
    if frame:
        lines.append(f"Ramka: **{frame}**")
    try:
        look = petcards.look_of(row)
    except Exception:  # noqa: BLE001 - decoration
        look = None
    if look is not None and look.seal in petcards.SEAL_NAMES:
        lines.append(f"Pieczęć: **{petcards.SEAL_NAMES[look.seal]}**")
    foil_names = getattr(petcards, "FOIL_NAMES", {})
    if look is not None and look.foil in foil_names:
        lines.append(f"Folia: **{foil_names[look.foil]}**")
    added = row["added_at"]
    if added is not None:
        lines.append(MSG_ADDED.format(when=ui.when(added, "f")))
    lines += _card_value_lines(row, rows)
    lines += ["", MSG_POSITION.format(position=position + 1, total=total)]
    embed = discord.Embed(title=title[:256], description="\n".join(lines), color=CLASS_COLOURS[tier])
    embed.set_footer(text=MSG_ENTRY_FOOTER)
    return embed


async def entry_view(bot: "AlterBot", guild: Any, row: Any, position: int, total: int, *,
                     wait: waiting.Waiting | None = None,
                     rows: Sequence[Any] | None = None) -> tuple[discord.Embed, Path | None]:
    """The embed of an entry and its card (None: shown without a picture).

    The card has everything the pet had (the viewer is a supporter: the album
    is for supporters).  A cold card can take seconds - the caller has
    acknowledged its interaction; it is composed in a worker thread
    (:func:`petcards.card`), and ``wait`` (the caller's :func:`waiting.notice`)
    shows "🐾 Nadchodzi towarzysz..." while it is slow.  A card that cannot be
    made never fails the page.  ``rows``: the player's entries (:func:`entry_embed`).
    """
    embed = entry_embed(row, position, total, rows)
    try:
        source = pets._pet_image(row)
        if source is None or not source.is_file():
            log.warning("Album entry %s (%s, %s, level %s, variant %s) has no picture (%s).", row["entry_id"],
                        row["quality"], row["type"], row["pet_lvl"], row["variant"], source)
            return embed, None
        image = await pets._pet_card(bot, guild, row, source, cosmetics=True, wait=wait)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the entry is shown without its picture
        log.exception("Album entry %s: no picture could be made; shown without one.", row["entry_id"])
        return embed, None
    if image is None:
        return embed, None
    embed.set_image(url=f"attachment://{image.name}")
    return embed, image


# -- $album ------------------------------------------------------------------------------

async def show_album(scene: "Scene") -> bool:
    """``$album`` / the hub's "Album" button; False for somebody who does not support (he is told)."""
    member = scene.author
    if member is None or not patrons.check_if_patron(member):
        await scene.send(supporters_only_text(member))
        return False
    from . import albumrank

    rows = await entries(scene.bot.db, member.id)
    value = await albumrank.overview_of(scene.bot, member, rows) if rows else None
    embed = overview_embed(member.id, rows, value)
    if not rows:
        await scene.send(embed=embed)
        return True
    panel = AlbumPanel(member, rows)
    try:
        await panel.send(scene.channel, embed=embed)
    except BaseException:
        panel.stop()
        raise
    return True


async def saved(scene: "Scene", player: Any) -> None:
    """After a supporter's sacrifice was kept: tell him, then check the achievement.  Never raises.

    When a typed fight began on the channel during the sacrifice's question
    (``challenge.fight_on``: nothing may be posted between its prompts) nothing
    is said - "Pełny album" is still given, without its announcement.
    """
    from . import achievements, collector  # (lazily: both build on the album)

    # The entry may have changed who leads the collection ranking (only a flag is raised here).
    collector.request(getattr(scene, "bot", None), "an album entry was kept")
    fighting = challenge.fight_on(getattr(scene, "channel", None))
    if fighting:
        log.info("Album: %s's pet was kept during a typed fight; not announced.", getattr(player, "id", player))
    else:
        try:
            await scene.send(MSG_DISCARD_SAVED.format(player_id=pets._pid(player)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the pet is in the album, whatever Discord says
            log.warning("Album: could not tell %s that the pet was kept (%s: %s).",
                        getattr(player, "id", player), type(exc).__name__, exc)
    await achievements.check_album(scene, player, announce=not fighting)  # never raises


# -- a new egg and the album ---------------------------------------------------------------

#: The note a supporter gets with a new egg (:func:`egg_note`).  It never names the species (an
#: egg does not tell it) and explains nothing about chances or about what else exists - it names
#: only what the player's OWN cards have.
MSG_EGG_NOTE = "📖 <@{player_id}>, Twój album a to jajko:"
MSG_EGG_NOTE_HELD = "{mark} {stage} - masz: {card}"
MSG_EGG_NOTE_MISSING = "{mark} {stage} - brak"
#: A counted card in words: its class when it is not an ordinary one, a rarer frame, the seal,
#: the foil (``pets.FOIL_WORDS``) - or, with none of them:
MSG_EGG_NOTE_PLAIN = "zwykła karta bez ozdób"
MSG_EGG_NOTE_FRAME = "ramka „{name}”"
MSG_EGG_NOTE_SEAL = "pieczęć {name}"
#: Behind a held stage where the card of the NEW egg would be worth more than the counted one
#: (only the rarest card of a slot counts, so a rarer one improves the collection too) ...
EGG_NOTE_BETTER_MARK = "⬆️"
#: ... and the line that says what the mark means.
MSG_EGG_NOTE_BETTER = (EGG_NOTE_BETTER_MARK + " = karta z tego jajka byłaby na tym etapie cenniejsza od tej w "
                       "albumie, więc podniosłaby wartość kolekcji.")
#: The closing line: the owner's question answered directly.
MSG_EGG_NOTE_CAN = "Ten towarzysz może jeszcze uzupełnić Twoją kolekcję: {stages}."
MSG_EGG_NOTE_ALL = "Album ma już każdy etap tego towarzysza."
#: ... for a mythic egg: it is not part of the collection ("Pełny album"), but its cards score.
MSG_EGG_NOTE_MYTHIC_CAN = ("Mityczny towarzysz nie należy do kolekcji, ale jego karty liczą się do jej wartości - "
                           "w albumie brakuje: {stages}.")
MSG_EGG_NOTE_MYTHIC_ALL = "Mityczny towarzysz nie należy do kolekcji, a album ma już każdy jego etap."
#: Nobody is pinged by the note (the player is addressed, as ``pets.MSG_EGG_TO_POUCH`` does).
EGG_NOTE_MENTIONS = discord.AllowedMentions.none()


def card_words(row: Any) -> str:
    """An album card by what it has: ``Shiny, ramka „Gwiazda Szronu”, pieczęć Łowcy, Holo``.

    The class when it is not an ordinary one (:data:`CLASS_NAMES`), a rarer frame by its name
    (the base frame of a class is no ornament: ``pets.ornaments_of``), the seal by its name, the
    foil as the pet's statistics call it (``pets.FOIL_WORDS``); :data:`MSG_EGG_NOTE_PLAIN` for a
    card with none of them.  Never raises: what cannot be read counts as not there.
    """
    parts = []
    tier = petcards.tier_of(row)
    if tier and tier in CLASS_NAMES:
        parts.append(CLASS_NAMES[tier])
    frame, seal, foil = pets.ornaments_of(row)
    if frame in petcards.FRAMES:
        parts.append(MSG_EGG_NOTE_FRAME.format(name=petcards.FRAMES[frame].label))
    if seal in petcards.SEAL_NAMES:
        parts.append(MSG_EGG_NOTE_SEAL.format(name=petcards.SEAL_NAMES[seal]))
    if foil in pets.FOIL_WORDS:
        parts.append(pets.FOIL_WORDS[foil])
    return ", ".join(parts) or MSG_EGG_NOTE_PLAIN


def egg_note(player_id: int, egg: Any, rows: Sequence[Any]) -> str:
    """What the album says about a new egg; "" for an egg of a species that has no cards.

    ``egg``: the rolled egg as ``pets._roll_egg`` makes it (``TYPE``, the class flags and
    whatever cosmetics were stored).  ``rows``: the player's album entries (those of the egg's
    species are enough).  Per stage of the species - the egg and the levels 1 ..
    :data:`pets.MAX_LEVEL` - one line: the album has no card of that stage, or the card that
    COUNTS for the collection there (``albumrank.collection``: the most valuable one of the
    slot) by what it has (:func:`card_words`).  A held stage is marked when the new egg's card
    would be worth more there than the counted one (``albumrank.card_points`` of the egg's
    look at that stage: ornaments do not change while a pet grows).  The closing line answers
    the owner's question: which stages this companion can still complete, or that the album has
    them all - for a mythic egg, which is no part of the collection, what its cards are for.
    The species is never named (module docstring).
    """
    from . import albumrank

    look = {str(key).lower(): value for key, value in dict(egg).items()}
    species = str(look.get("type"))
    if (species, 0) not in albumrank.score_slots():
        return ""
    value = albumrank.collection(rows)
    by_id = {pets._int(row["entry_id"]): row for row in rows}
    lines = [MSG_EGG_NOTE.format(player_id=player_id)]
    missing: list[str] = []
    better = False
    for level in stages():
        name = stage_name(level)
        held = value.best.get((species, level))
        counted = by_id.get(held[1]) if held is not None else None
        if held is None or counted is None:
            missing.append(name)
            lines.append(MSG_EGG_NOTE_MISSING.format(mark=MISSING_MARK, stage=name))
            continue
        line = MSG_EGG_NOTE_HELD.format(mark=COLLECTED_MARK, stage=name, card=card_words(counted))
        if albumrank.card_points({**look, "pet_lvl": level}) > held[0]:
            better = True
            line += " " + EGG_NOTE_BETTER_MARK
        lines.append(line)
    if species in collection_species():
        lines.append(MSG_EGG_NOTE_CAN.format(stages=", ".join(missing)) if missing else MSG_EGG_NOTE_ALL)
    else:
        lines.append(MSG_EGG_NOTE_MYTHIC_CAN.format(stages=", ".join(missing)) if missing
                     else MSG_EGG_NOTE_MYTHIC_ALL)
    if better:
        lines.append(MSG_EGG_NOTE_BETTER)
    return "\n".join(lines)


async def note_new_egg(scene: "Scene", player: Any, egg: Any) -> bool:
    """After a new egg was stored for ``player``: tell a supporter what his album says about it.

    Called by ``pets.assign_pet`` - the one place an egg is granted (boss loot, monster loot,
    the merchant), outside its transaction, also for an egg that went into the egg pouch.  ONE
    public message (:func:`egg_note`), only for a player who supports right now (the album is
    for supporters; everybody else gets nothing new), pinging nobody.  It costs one small read
    (the player's entries of that one species) and draws nothing at random.  Never raises: the
    egg is the player's already, so a database that does not answer, a missing table or a
    message Discord refuses costs only this note (logged).  True when the note was posted.

    The note is sent with ONE attempt (as the other once-only messages, e.g. the rank's
    announcement in features/collector.py): a send whose answer got lost would be repeated by
    ``io.send`` and put the same note on the channel twice - and the pauses between the tries
    would hold back the drop's embed, which comes right after.  A note that fails is only lost.
    """
    try:
        if not patrons.check_if_patron(player):
            return False
        player_id = pets._pid(player)
        rows = await _fetch(scene.bot.db, "fetch", _SQL_OF_SPECIES, player_id, str(egg["TYPE"]))
        text = egg_note(player_id, egg, rows)
        if not text:
            return False
        await scene.send(text[:2000], allowed_mentions=EGG_NOTE_MENTIONS, attempts=1)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the egg is granted, whatever happens to the note
        log.warning("Album: no note about the new egg of %s (%s: %s).", getattr(player, "id", player),
                    type(exc).__name__, exc)
        return False


# -- the panel ---------------------------------------------------------------------------

#: How long the album's buttons stay (seconds without a click), as under a shown pet.
PANEL_TIMEOUT = pets.PANEL_TIMEOUT
#: Discord's limit of entries in a select menu.
MAX_OPTIONS = 25


class _Gap(int):
    """A page index where the shown entry WAS (it is gone): the entry now at that index took its
    place.  Works as that index everywhere (a redraw shows what took the place);
    :meth:`AlbumPanel._step` reads it so that "Następny" shows that entry instead of skipping it."""


class AlbumPanel(ui.PanelView):
    """The buttons under ``$album``: page through the entries, jump to a species, remove one.

    Only the album's owner can use them (:class:`ui.PanelView`), only while
    he supports, and - like every display button - not while an event / hunt
    / tower run holds the channel nor between the prompts of a typed fight
    (:func:`hub.display_gate`).  Every click is acknowledged first (a card may
    take seconds) and the message is edited in place.  Each click reads the
    album anew: what another panel removed meanwhile is simply gone (and
    "Następny" from its page shows the entry that took its place).

    Page ``None`` is the overview; pages 0 .. N-1 are the entries in album
    order (the hatched pets by species, then the eggs).  The list jumps to the
    first entry of a hatched species or to the first egg ("Jajka": the eggs are
    not sorted by species, module docstring).  "Usuń z albumu" asks first
    (button **Potwierdzam** or a typed ``$potwierdzam``, one open question per
    player as in features/pets.py) and removes exactly the entry the message
    showed at the click - not a page still being drawn, nor one whose edit
    Discord refused.  Quick page clicks are drawn together: only the page they
    lead to is drawn (:meth:`_go`).
    """

    def __init__(self, owner: Any, rows: Sequence[Any], *, timeout: float | None = None) -> None:
        super().__init__(owner=owner, timeout=PANEL_TIMEOUT if timeout is None else timeout)
        self.player = owner
        #: The entry the message shows (None: the overview) and its place in the album.  Set only
        #: once the edit that shows it went through: a page being drawn does not count yet.
        self.entry_id: int | None = None
        self.position: int | None = None
        #: The file the message carries now (the shown entry's card).
        self.image: Path | None = None
        #: Page picks of the clicks that came while a page was being drawn (:meth:`_go`).
        self._pending: list[Callable[[Sequence[Any], int | None], int | None]] = []
        self._lock = asyncio.Lock()
        self.species: discord.ui.Select = discord.ui.Select(
            custom_id=ui.FLOW_PREFIX + "album:species", placeholder=MSG_SPECIES_PLACEHOLDER,
            min_values=1, max_values=1, options=self._options(rows), row=1)
        self.species.callback = self._species_chosen  # type: ignore[method-assign]
        self.add_item(self.species)
        self._arrange(rows)

    # -- what the panel shows -------------------------------------------------------

    @staticmethod
    def _options(rows: Sequence[Any]) -> list[discord.SelectOption]:
        """The species the album holds a HATCHED pet of (with how many), then one entry for all
        the eggs: an egg is not listed under its species."""
        counted: dict[str, int] = {}
        eggs = 0
        for row in rows:
            if is_egg(row):
                eggs += 1
                continue
            species, _ = slot_of(row)
            counted[species] = counted.get(species, 0) + 1
        ordered = sorted(counted, key=lambda name: (_species_rank(name), name))[:MAX_OPTIONS - bool(eggs)]
        options = [discord.SelectOption(label=species_name(species)[:100], value=species[:100],
                                        description=_entries_word(counted[species]))
                   for species in ordered]
        if eggs:
            options.append(discord.SelectOption(label=MSG_EGGS_OPTION, value=EGGS_OPTION, emoji="🥚",
                                                description=_entries_word(eggs)))
        return options or [discord.SelectOption(label=MSG_TITLE, value="-")]

    def _arrange(self, rows: Sequence[Any], position: int | None = None) -> None:
        """Switch the buttons on and off for page ``position`` (None: the overview)."""
        overview = position is None
        last = len(rows) - 1
        self.previous.disabled = overview
        self.next.disabled = not rows or (not overview and position >= last)
        self.overview.disabled = overview
        self.remove.disabled = overview
        self.species.options = self._options(rows)
        self.species.disabled = not rows

    async def _show(self, bot: "AlterBot", rows: Sequence[Any], position: int | None, *,
                    wait: waiting.Waiting | None = None) -> bool:
        """Edit the message to show page ``position`` (None: the overview).  Never raises on Discord.

        :attr:`entry_id` / :attr:`position` change only when the edit went
        through: while the card is drawn, and after an edit Discord refused,
        the message still shows the page before - and that is the entry
        "Usuń z albumu" means.  ``wait`` is :meth:`_go`'s notice: while a slow
        card is drawn, "🐾 Nadchodzi towarzysz..." stands under the panel.
        """
        if not rows:
            self.entry_id = self.position = None
            self.image = None
            await self.finish(embed=overview_embed(self.owner_id or 0, rows), attachments=[])
            return False
        if position is not None:
            position = max(0, min(int(position), len(rows) - 1))  # (a _Gap is shown as its index)
        if position is None:
            from . import albumrank

            entry_id = None
            value = await albumrank.overview_of(bot, self.player, rows)  # (read anew: after a removal too)
            embed, image = overview_embed(self.owner_id or 0, rows, value), None
        else:
            row = rows[position]
            entry_id = int(row["entry_id"])
            guild = getattr(self.message, "guild", None) or getattr(self.player, "guild", None)
            embed, image = await entry_view(bot, guild, row, position, len(rows), wait=wait, rows=rows)
        self._arrange(rows, position)
        if self.is_finished():
            return False
        if image == self.image:
            shown = await pets._edit_panel(self.message, embed=embed, view=self)
        else:
            try:
                attachments = [] if image is None else [discord.File(str(image))]
            except OSError as exc:  # the card vanished since it was made
                log.warning("Album entry %s: card %s could not be read (%s).", entry_id, image, exc)
                embed.set_image(url=None)
                attachments, image = [], None
            shown = await pets._edit_panel(self.message, embed=embed, view=self, attachments=attachments)
        if not shown:
            return False
        self.position, self.entry_id, self.image = position, entry_id, image
        return True

    def _current(self, rows: Sequence[Any]) -> int | None:
        """Where the shown entry is now; None for the overview.

        When it is gone (removed through another panel / a second ``$album``) its
        place, as a :class:`_Gap`: the entry that slid into it has not been shown yet.
        """
        if self.position is None:
            return None
        for index, row in enumerate(rows):
            if int(row["entry_id"]) == self.entry_id:
                return index
        return _Gap(self.position)

    # -- clicks ---------------------------------------------------------------------

    async def _may_click(self, interaction: Any) -> bool:
        """The display buttons' guards, and the album is for supporters (asked at every click)."""
        if not await hub.display_gate(interaction):
            return False
        if not patrons.check_if_patron(interaction.user):
            await ui.reply(interaction, supporters_only_text(None))
            await self.finish()
            return False
        return True

    async def _turn(self, interaction: Any, where: Callable[[Sequence[Any], int | None], int | None]) -> None:
        """A page button: show the page ``where`` picks (:meth:`_go`)."""
        if not await self._may_click(interaction):
            return
        await ui.ack(interaction)
        try:
            await self._go(interaction.client, where)
        except DatabaseUnavailable as exc:
            log.error("Album panel of %s: database unavailable (%s).", self.owner_id, exc)
            await ui.reply(interaction, hub.MSG_DB_DOWN)

    async def _go(self, bot: "AlterBot", where: Callable[[Sequence[Any], int | None], int | None]) -> None:
        """Read the album and show the page ``where(rows, page)`` picks from the page the panel is at.

        Quick clicks are not drawn one by one: a click that comes while a page
        is being drawn only leaves its pick here and returns.  Once that page is
        shown, the picks that waited are applied together (five "Następny" =
        five entries on) and only the page they lead to is drawn - a cold card
        takes seconds, and the bot draws one card at a time for everybody.
        While a slow card is drawn, "🐾 Nadchodzi towarzysz..." stands under the
        panel (:mod:`altermmo.core.waiting`); ONE notice covers the pages drawn
        in a row and goes after the last of them is edited in (a notice per page
        would blink off and on, and the next page would wait for its deletion).
        Raises :class:`DatabaseUnavailable` (the picks that waited are dropped).
        """
        self._pending.append(where)
        if self._lock.locked():  # the page being drawn now is followed by this one
            return
        async with self._lock:
            try:
                while self._pending:  # (again: clicks that came while the notice was deleted)
                    async with waiting.notice(getattr(self.message, "channel", None)) as wait:
                        while self._pending:
                            picks, self._pending = self._pending, []
                            rows = await entries(bot.db, self.owner_id or 0)
                            position = self._current(rows)
                            for pick in picks:
                                position = pick(rows, position)
                            await self._show(bot, rows, position, wait=wait)
            finally:
                self._pending.clear()

    @staticmethod
    def _step(rows: Sequence[Any], step: int, current: int | None) -> int | None:
        """``step`` entries on from page ``current`` (None: the overview).

        From the place of an entry that is gone (a :class:`_Gap`) the first step
        forward is the entry that took that place - it was never shown.
        """
        if current is None:
            return 0 if step > 0 else None
        if current >= len(rows):  # the last entry was removed meanwhile
            return len(rows) - 1
        if isinstance(current, _Gap) and step > 0:
            step -= 1
        target = int(current) + step
        return None if target < 0 else min(target, len(rows) - 1)

    @discord.ui.button(label="Poprzedni", emoji="◀️", style=discord.ButtonStyle.secondary, row=0,
                       custom_id=ui.FLOW_PREFIX + "album:previous")
    async def previous(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._turn(interaction, lambda rows, current: self._step(rows, -1, current))

    @discord.ui.button(label="Następny", emoji="▶️", style=discord.ButtonStyle.primary, row=0,
                       custom_id=ui.FLOW_PREFIX + "album:next")
    async def next(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._turn(interaction, lambda rows, current: self._step(rows, +1, current))

    @discord.ui.button(label="Przegląd", emoji="📖", style=discord.ButtonStyle.secondary, row=0,
                       custom_id=ui.FLOW_PREFIX + "album:overview")
    async def overview(self, interaction: Any, button: discord.ui.Button) -> None:
        await self._turn(interaction, lambda rows, current: None)

    async def _species_chosen(self, interaction: Any) -> None:
        values = (getattr(interaction, "data", None) or {}).get("values") or list(self.species.values)
        chosen = str(values[0]) if values else ""

        def first_of(rows: Sequence[Any], current: int | None) -> int | None:
            if chosen == EGGS_OPTION:
                return next((index for index, row in enumerate(rows) if is_egg(row)), None)
            return next((index for index, row in enumerate(rows)
                         if not is_egg(row) and slot_of(row)[0] == chosen), None)

        await self._turn(interaction, first_of)

    @discord.ui.button(label="Usuń z albumu", emoji="🗑️", style=discord.ButtonStyle.danger, row=0,
                       custom_id=ui.FLOW_PREFIX + "album:remove")
    async def remove(self, interaction: Any, button: discord.ui.Button) -> None:
        await remove_from_panel(self, interaction)

    async def refresh_page(self, bot: "AlterBot") -> None:
        """Show the page anew (after a removal): the same entry, or what took its place.

        While another page is being drawn, that one is shown first and then
        redrawn from the album as it is now (:meth:`_go`).
        """
        await self._go(bot, lambda rows, current: current)


def _remove_question(player_id: int, row: Any) -> str:
    """:data:`MSG_REMOVE_ASK` for an entry - with the moment it was kept, which tells apart two
    entries of one slot and name (the entry page shows the same moment)."""
    added = row["added_at"]
    when = "" if added is None else MSG_REMOVE_WHEN.format(when=ui.when(added, "f"))
    return MSG_REMOVE_ASK.format(player_id=player_id, what=describe(row), when=when)


async def remove_from_panel(panel: AlbumPanel, interaction: Any) -> bool:
    """"Usuń z albumu": after a confirmation, remove the entry the panel showed at the click.

    One open ``$potwierdzam`` question per player (shared with features/pets.py:
    one typed answer must never confirm two things).  The entry is the one the
    message showed at the click (:attr:`AlbumPanel.entry_id`: not a page still
    being drawn) and is fixed then - turning the page during the question
    changes nothing - and removed by its id and the player's id: a second
    panel, a double click or an entry already gone remove nothing more (the
    player is told).  The pet's name is the player's text: the question and
    the answers ping nobody but him.  When a typed fight began on the channel
    during the question (``challenge.fight_on``: nothing may be posted between
    its prompts) the confirmed entry is removed all the same, but no answer is
    posted and the panel is not redrawn - its next click reads the album anew.
    True when removed.
    """
    if not await panel._may_click(interaction):
        return False
    entry_id = panel.entry_id
    await ui.ack(interaction)
    if entry_id is None:  # (the button is off on the overview)
        return False
    bot = interaction.client
    scene = ui.scene_of(interaction)
    player = interaction.user
    player_id = int(player.id)
    quiet = discord.AllowedMentions(everyone=False, roles=False, users=[player])
    removed = False
    try:
        with pets._confirmation_slot(player_id) as free:
            if not free:
                await scene.send(pets.MSG_ALREADY_WAITING)
                return False
            row = await entry(bot.db, player_id, entry_id)
            if row is None:
                await scene.send(MSG_ALREADY_GONE.format(player_id=player_id), allowed_mentions=quiet)
            else:
                what = describe(row)
                answer = await pets._confirm(scene, player, _remove_question(player_id, row),
                                             allowed_mentions=quiet)
                if answer:
                    removed = await remove(bot.db, player_id, entry_id)
                    if removed:  # (the leader of the collection ranking may have changed: a flag only)
                        from . import collector

                        collector.request(bot, "an album entry was removed")
                    await pets._thank(answer, "<:MonkaS:882181709100097587>")
                if challenge.fight_on(scene.channel):  # a typed fight began during the question
                    log.info("Album of %s: entry %s %s during a typed fight; nothing posted.", player_id,
                             entry_id, "removed" if removed else "not removed")
                    return removed
                if not answer:
                    await scene.send(MSG_REMOVE_CANCELLED)
                    return False
                await scene.send(MSG_REMOVED.format(what=what, player_id=player_id) if removed
                                 else MSG_ALREADY_GONE.format(player_id=player_id), allowed_mentions=quiet)
        await panel.refresh_page(bot)
    except DatabaseUnavailable as exc:
        log.error("Album panel of %s: database unavailable (%s).", player_id, exc)
        await ui.reply(interaction, hub.MSG_DB_DOWN)
    return removed
