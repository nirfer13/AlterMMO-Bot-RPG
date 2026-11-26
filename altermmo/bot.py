"""The bot object: wiring of configuration, database, game state and cogs."""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import sys
import time
import traceback
from pathlib import Path

import discord
from discord.ext import commands

from . import schema
from .core import fightlog, io, scheduler, ui
from .core.db import Database, DatabaseUnavailable
from .core.net import ConnectionMonitor
from .core.scene import Scene
from .core.state import GameState
from .core.storage import IdListFile, JsonFile, migrate_legacy_file
from .core.supervisor import Supervisor
from .features import boss_cycle, petcards
from .features.boss_cycle import BossCycle
from .settings import COMMAND_PREFIX, COMMAND_PREFIXES, PROJECT_ROOT, Ids, Settings, Timings

log = logging.getLogger(__name__)


def import_all_modules() -> list[str]:
    """Import every module of the bot now; returns the ones that could not be imported.

    Several modules import each other only when they are first needed (to avoid
    circular imports).  The bot runs from the folder its code is edited in, so
    a module first needed hours after the start was read from the disk in
    whatever state the files were THEN - and mixed with the older code already
    in memory.  On 2026-11-20 that let one treasure chest be opened seven
    times (the freshly read event code called a function the running bot did
    not have yet; the reward was given, the error put the chest back).  With
    everything imported before the bot logs in, a running bot never reads its
    code again.  Never raises.
    """
    package = __name__.rpartition(".")[0]
    failed: list[str] = []

    def broken_package(name: str) -> None:  # (walk_packages imports the sub-packages itself)
        log.error("Package %s cannot be imported.", name, exc_info=True)
        failed.append(name)

    try:
        for info in pkgutil.walk_packages(sys.modules[package].__path__, package + ".", onerror=broken_package):
            if info.name.endswith(".__main__"):
                continue  # (the entry point: importing it is how the bot was started)
            try:
                importlib.import_module(info.name)
            except Exception:  # noqa: BLE001 - reported; the bot starts with what works
                log.exception("Module %s cannot be imported.", info.name)
                if info.name not in failed:
                    failed.append(info.name)
    except Exception:  # noqa: BLE001 - whatever the walk itself runs into
        log.exception("Could not walk the modules of %s.", package)
        failed.append(package)
    return failed


#: Cogs loaded at start-up.  A cog that fails to load is logged and skipped so
#: one broken feature cannot take the whole bot down.
EXTENSIONS: tuple[str, ...] = (
    "altermmo.cogs.boss",
    "altermmo.cogs.events",
    "altermmo.cogs.pets",
    "altermmo.cogs.skills",
    "altermmo.cogs.hunting",
    "altermmo.cogs.tower",
    "altermmo.cogs.ranking",
    "altermmo.cogs.slayer",
    "altermmo.cogs.achievements",
    "altermmo.cogs.album",
    "altermmo.cogs.missions",
    "altermmo.cogs.items",
    "altermmo.cogs.collector",
    "altermmo.cogs.xpsteal",
    "altermmo.cogs.twitch",
    "altermmo.cogs.schedule",
    "altermmo.cogs.backup",
    "altermmo.cogs.patchnotes",
    "altermmo.cogs.fightlog",
    "altermmo.cogs.admin",
    "altermmo.cogs.general",
    "altermmo.cogs.moderation",
    "altermmo.cogs.help",
    "altermmo.cogs.hub",
    "altermmo.cogs.ui",
)

#: Answer to a command used again too early; ``{when}`` is Discord's live countdown (``ui.when``).
MSG_COOLDOWN = "Poczekaj na odnowienie komendy! Spróbuj ponownie {when} <:Bedge:970576892874854400>"


def cooldown_text(retry_after: float) -> str:
    """:data:`MSG_COOLDOWN` for a cooldown that ends in ``retry_after`` seconds."""
    return MSG_COOLDOWN.format(when=ui.when(max(1.0, float(retry_after))))

#: Start-up note for the owner: a module of the bot cannot be imported (see ``import_all_modules``).
MSG_MODULES_BROKEN = ("Nie udało się wczytać części kodu bota: {modules}. Funkcje z tych plików nie "
                      "działają - szczegóły są w logu bota.")
#: Start-up notes for the owner: ``schema.add_new_columns`` could not add what v2 needs.  Such
#: columns are the pet cosmetics (``pets.NEW_COLUMNS`` of ``PETS``), their copy in the pet album
#: (``album.NEW_COLUMNS``) and the ``PETOWNER`` columns of the wandering merchant's goods
#: (``pets.UPGRADE_COLUMNS``) and of the companion missions (``pets.MISSION_COLUMNS``); the game
#: plays on without them.  The note says what is really off (:func:`new_columns_note`) - one of
#: these about ``PETS`` (or the album), one about ``PETOWNER``:
#: the first cosmetic columns of ``PETS`` (seal / Holo) are missing - all cosmetics are off;
MSG_NEW_COLUMNS_FAILED = (
    "Nie udało się dodać do bazy danych kolumn nowych w v2: {problems}. Kosmetyka towarzyszy "
    "(pieczęcie, folia Holo) jest WYŁĄCZONA: nic nie jest losowane, a towarzysze nie mają pieczęci "
    "ani folii (ramki Shiny / Ultra Shiny działają normalnie) - reszta gry działa normalnie. "
    "Bot spróbuje ponownie przy następnym starcie.")
#: ... only the frame / partial Holo columns of ``PETS`` are missing (``pets.frames_available``);
MSG_FRAME_COLUMNS_FAILED = (
    "Nie udało się dodać do bazy danych kolumn nowych w v2: {problems}. Pieczęcie i pełna folia Holo "
    "działają normalnie, ale NOWE RAMKI i częściowa folia Holo są WYŁĄCZONE: nie są losowane ani "
    "zapisywane, a każdy towarzysz ma podstawową ramkę swojej klasy - reszta gry działa normalnie. "
    "Bot spróbuje ponownie przy następnym starcie.")
#: ... ``PETS`` is complete, the album lacks the frame / partial Holo columns (``album.keep``).
MSG_ALBUM_COLUMNS_FAILED = (
    "Nie udało się dodać do bazy danych kolumn nowych w v2: {problems}. Kosmetyka towarzyszy działa "
    "normalnie, ale album nie ma miejsca na ramkę i częściową folię Holo: towarzysza z rzadką ramką "
    "albo z częściową folią Holo nie da się zapisać w albumie, więc jego porzucenie jest ODRZUCANE "
    "(gracz dostaje komunikat, towarzysz zostaje u niego). Pozostali towarzysze trafiają do albumu "
    "normalnie - reszta gry działa normalnie. Bot spróbuje ponownie przy następnym starcie.")
#: ``PETOWNER`` lacks the merchant's columns (stable places 5-10, the egg pouch; then also the missions');
MSG_OWNER_COLUMNS_FAILED = (
    "Nie udało się dodać do bazy danych kolumn nowych w v2: {problems}. Miejsca w stajni 5-10, Sakwa na jajko, "
    "Duża sakwa i Zwoje przywołania są WYŁĄCZONE: stajnia ma 4 miejsca, kupiec nie sprzedaje miejsc ani sakwy, "
    "a takie nagrody ze zleceń zamieniają się w zwoje odrodzenia - reszta gry działa normalnie. Bot spróbuje "
    "ponownie przy następnym starcie.")
#: ... ``PETOWNER`` lacks only the companion missions' columns (the Duża sakwa, the summoning scrolls).
MSG_MISSION_COLUMNS_FAILED = (
    "Nie udało się dodać do bazy danych kolumn nowych w v2: {problems}. Duża sakwa i Zwoje przywołania są "
    "WYŁĄCZONE: takie nagrody ze zleceń zamieniają się w zwoje odrodzenia, a **!przywolaj** odpowiada, że zwoje "
    "są niedostępne - reszta gry działa normalnie. Bot spróbuje ponownie przy następnym starcie.")


def new_columns_note(not_added: list[str], problems: list[str]) -> str | None:
    """The start-up note for columns ``schema.add_new_columns`` could not add (``not_added``).

    Which columns are really missing is read from what ``schema.verify`` found
    (``problems``), so the note says what is off and not more: all cosmetics
    (seal / Holo of ``PETS`` missing), the new frames and the partial Holo
    (only those columns of ``PETS``), or keeping such a pet in the album (only
    the album's columns).  ``add_new_columns`` adds a table's columns all or
    none, and without the frame columns of ``PETS`` no pet has a rare frame -
    so the first case that applies is the whole truth.  ``PETOWNER`` is
    another table: its note (the merchant's columns - the missions' are then
    missing too -, or only the missions') is added to that one, joined with a
    space.  None when nothing new is missing after all (the columns are
    there: nothing is off).

    A feature module that cannot be imported is skipped (never raised): its
    own start-up note (:data:`MSG_MODULES_BROKEN`) tells the owner, and an
    import error escaping from here was reported as "the database did not
    answer".  Without ``pets`` no pet works at all, so there is nothing to add.
    """
    found = set(problems)
    missing = {(table, column) for table, column, _ in _importable_new_columns()
               if schema.missing_column(table, column) in found}
    details = "; ".join(not_added)
    try:
        from .features import pets  # (the feature modules import this module's neighbours)
    except Exception:  # noqa: BLE001 - see MSG_MODULES_BROKEN
        return None
    owner_missing = {column for table, column in missing if table == "petowner"}
    owner_note = None
    if owner_missing & set(pets.UPGRADE_COLUMNS):
        owner_note = MSG_OWNER_COLUMNS_FAILED.format(problems=details)
    elif owner_missing & set(pets.MISSION_COLUMNS):
        owner_note = MSG_MISSION_COLUMNS_FAILED.format(problems=details)
    pets_note = None
    if any(("pets", column) in missing for column in pets.COSMETIC_COLUMNS):
        pets_note = MSG_NEW_COLUMNS_FAILED.format(problems=details)
    elif any(("pets", column) in missing for column in pets.FRAME_COLUMNS):
        pets_note = MSG_FRAME_COLUMNS_FAILED.format(problems=details)
    else:
        try:
            from .features import album
        except Exception:  # noqa: BLE001 - see MSG_MODULES_BROKEN (and its columns are not in ``missing``)
            album = None
        if album is not None and any(table == album.TABLE.lower() for table, _ in missing):
            pets_note = MSG_ALBUM_COLUMNS_FAILED.format(problems=details)
    return " ".join(note for note in (pets_note, owner_note) if note) or None


def _importable_new_columns() -> list[tuple[str, str, str]]:
    """``schema.new_columns()`` of the feature modules that can be imported; never raises.

    ``schema.new_columns`` imports every schema module and raises when one is
    broken.  The start-up notes about the columns must not: a broken module is
    the business of :data:`MSG_MODULES_BROKEN` (and of the log).
    """
    columns: list[tuple[str, str, str]] = []
    for module_name in schema.SCHEMA_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - reported by import_all_modules
            log.warning("Schema module %s cannot be imported; its new columns are not looked at.", module_name)
            continue
        columns.extend((table.lower(), column.lower(), definition)
                       for table, column, definition in getattr(module, "NEW_COLUMNS", ()))
    return columns


#: Start-up note: the database did not answer (at the connection, or at the check after a failed
#: ``add_new_columns``), so nothing above was checked and it is NOT known what is missing.
#: Queries reconnect by themselves, but the new tables and columns are only added at a start.
#: It says what each missing piece would mean (production has had SEAL / HOLO since 2026-11-20,
#: so the likely gap is only the frame columns or the album - the seals then work).
MSG_DATABASE_DOWN_AT_START = (
    "Baza danych nie odpowiadała przy starcie bota ({error}). Bot połączy się z nią sam, ale nie "
    "sprawdził jej struktury i mógł nie dodać tabel ani kolumn nowych w v2 (spróbuje ponownie przy "
    "następnym uruchomieniu). Jeśli czegoś z tego brakuje w bazie, do tego czasu: bez kolumn "
    "SEAL / HOLO tabeli PETS cała kosmetyka towarzyszy (pieczęcie, folia Holo, nowe ramki) jest "
    "wyłączona; bez samych kolumn FRAME / HOLO_PARTIAL pieczęcie i pełna folia Holo działają, ale "
    "nowe ramki i częściowa folia Holo nie są losowane ani zapisywane; tabelę PET_ALBUM bot utworzy "
    "sam przy pierwszym porzuceniu towarzysza przez wspierającego (do tego czasu album jest pusty), "
    "a bez samych jej kolumn FRAME / HOLO_PARTIAL "
    "odrzucane jest porzucenie towarzysza z rzadką ramką albo częściową folią Holo; "
    "tabelę PET_MISSIONS (zlecenia towarzyszy) bot utworzy sam przy pierwszym otwarciu tablicy zleceń (do tego "
    "czasu nikt nie jest na zleceniu), a bez kolumn BIG_POUCH / POUCH_PET_ID2 / SUMMON_SCROLLS tabeli PETOWNER "
    "Duża sakwa i Zwoje przywołania są wyłączone; tabelę PLAYER_ITEMS (przedmioty graczy) bot utworzy sam, gdy "
    "będzie jej potrzebował (do tego czasu nowe przedmioty nie wypadają z łupów, a **!przedmioty** ich nie "
    "pokazuje). "
    "Reszta gry działa normalnie.")
#: Start-up note: the library that composes the pet cards is missing on this machine.
MSG_NO_PILLOW = (
    "Brak biblioteki Pillow - karty towarzyszy (ramki, pieczęcie, folia Holo) są WYŁĄCZONE i "
    "towarzysze są pokazywani bez nich. Zainstaluj ją: `.venv\\Scripts\\python.exe -m pip install "
    "-r requirements.txt` i uruchom bota ponownie.")

#: State files carried over from the legacy bot (same names, same formats).
#:
#: ``expSummary.json`` is deliberately not one of them: the Horus bot collects that file
#: every few seconds (it renames it away and credits the experience), so (1) there is
#: nothing to carry over once the old bot is stopped, (2) a copy taken at the wrong moment
#: would be credited twice, and (3) Horus keeps re-creating the old file, which made the
#: "the old bot's files are newer" alarm below fire on every restart.
LEGACY_STATE_FILES: tuple[str, ...] = (
    "bossModifier.json",
    "daily_player_cd.txt",
    "weekly_tower_cd.txt",
    "skill_daily_player_cd.txt",
    "skill_weekly_player_cd.txt",
)


#: Folders shared with Horus inside the data folder (the folder of Horus's ``external_xp_file``):
#: XP theft requests written by this bot, results written by Horus (features/xpsteal.py).
XP_TRANSFERS_FOLDER = "xp_transfers"
XP_TRANSFER_RESULTS_FOLDER = "xp_transfer_results"


#: ``runtime.json`` older than this at start-up = the bot has not been running
#: (a running bot rewrites it every night at 02:30 and 04:00).
STALE_STATE_AFTER = 36 * 3600.0


class NotReady(RuntimeError):
    """A Discord object (guild/channel) is not available in the cache yet."""


class Stores:
    """File-backed state, see :mod:`altermmo.core.storage`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        #: Players who already used ``!polowanie`` today.
        self.daily_cd = IdListFile(data_dir / "daily_player_cd.txt")
        #: Players who already entered the Death Tower this week.
        self.weekly_tower_cd = IdListFile(data_dir / "weekly_tower_cd.txt")
        #: Skill gem cooldowns.
        self.skill_daily_cd = IdListFile(data_dir / "skill_daily_player_cd.txt")
        self.skill_weekly_cd = IdListFile(data_dir / "skill_weekly_player_cd.txt")
        #: Shrine modifiers for the next boss (features/modifiers.py owns the schema).
        self.modifiers = JsonFile(data_dir / "bossModifier.json", dict)
        #: Experience the owner still has to hand out (features/expsum.py).
        self.exp_summary = JsonFile(data_dir / "expSummary.json", dict)
        #: v2 bookkeeping: spawn switch, timestamps of the last scheduled resets.
        self.runtime = JsonFile(data_dir / "runtime.json", dict)
        #: Unused "steal experience" rights won from bosses (features/xpsteal.py).
        self.xp_thefts = JsonFile(data_dir / "xpThefts.json", dict)
        #: XP thefts settled by Horus (features/xpsteal.py, protocol version 1): one request file
        #: per theft written here, one result file per settled theft written there by Horus.
        #: Sub-folders: the backup copies only the files lying directly in ``data_dir``.
        self.xp_transfers_dir = data_dir / XP_TRANSFERS_FOLDER
        self.xp_transfer_results_dir = data_dir / XP_TRANSFER_RESULTS_FOLDER
        #: Results already told to the thief: ``{"<transfer id>": unix time}`` (never told twice).
        self.xp_transfers_announced = JsonFile(data_dir / "xpTransfersAnnounced.json", dict)
        #: Requests this bot wrote whose result is not announced yet: ``{"<transfer id>": unix time}``.
        #: A result for any other id is not announced (a request copy back from a backup...).
        self.xp_transfers_written = JsonFile(data_dir / "xpTransfersWritten.json", dict)
        #: Roles of the achievements the bot made itself: ``{key: role id}`` (features/achievements.py).
        self.achievement_roles = JsonFile(data_dir / "achievementRoles.json", dict)
        #: The rank of place 1 of the collection ranking (features/collector.py): the id of its role
        #: and whose rank was announced last.  A file of its own on purpose - the role is taken away
        #: again, so it is no achievement, and ``achievements.retire_roles`` deletes the role of every
        #: key of ``achievementRoles.json`` it does not know.
        self.collector = JsonFile(data_dir / "petCollector.json", dict)
        #: The open "Kopalnia Ostatniej Lampy" event and its expeditions (features/dungeon.py): what was
        #: paid, which eggs wait for a decision - so that a restart pays nothing twice.
        self.dungeon = JsonFile(data_dir / "dungeon.json", dict)
        #: Players who switched the "a boss is coming" warnings off (features/bossdetect.py).
        self.detect_off = IdListFile(data_dir / "boss_detection_off.txt")

    def migrate_legacy(self, legacy_dirs: list[Path]) -> list[str]:
        """Copy missing state files from the old bot; returns the names copied now."""
        return [name for name in LEGACY_STATE_FILES
                if migrate_legacy_file(name, self.data_dir, legacy_dirs)]

    def runtime_get(self, key: str, default=None):
        return self.runtime.load().get(key, default)

    def runtime_set(self, key: str, value) -> None:
        data = self.runtime.load()
        data[key] = value
        self.runtime.save(data)


class AlterBot(commands.Bot):
    """AlterMMO RPG bot."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True  # prefix commands and typed fight answers
        super().__init__(
            command_prefix=COMMAND_PREFIXES,  # "!" and the silent legacy "$" (settings.py)
            description="AlterMMO Discord Bot",
            intents=intents,
            help_command=None,
        )
        self.settings: Settings = settings
        self.ids: Ids = settings.ids
        self.timings: Timings = settings.timings
        self.debug: bool = settings.debug

        self.db = Database(settings.database_url)
        self.state = GameState()
        self.net = ConnectionMonitor()
        self.supervisor = Supervisor()
        self.store = Stores(settings.data_dir)
        self.boss_cycle = BossCycle(self)
        #: Things found at start-up that the owner should hear about once connected.
        self._startup_notes: list[str] = []
        self.add_check(self._guild_only)

    @staticmethod
    async def _guild_only(ctx: commands.Context) -> bool:
        """No command works in private messages (they bypassed channel permissions)."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # -- lifecycle --------------------------------------------------------------

    async def setup_hook(self) -> None:
        self.net.attach(self)
        # Every fight round by round, with the times of the answers, for the owner's analyses.
        fightlog.configure_archive(self.settings.log_dir / "fights")
        self.supervisor.on_crash = self._report_loop_crash
        scheduler.failure_hook = self._report_job_failure
        legacy_dirs = [PROJECT_ROOT]
        if self.settings.legacy_dir is not None:
            legacy_dirs.append(self.settings.legacy_dir)
        migrated = self.store.migrate_legacy(legacy_dirs)
        missing = [name for name in LEGACY_STATE_FILES if not (self.store.data_dir / name).exists()]
        if migrated:
            log.info("State files taken over from the old bot: %s", ", ".join(migrated))
        if missing:
            log.warning("No state file found for: %s - starting them empty (cooldown lists "
                        "and shrine modifiers of the old bot are NOT carried over). "
                        "Looked in: %s", ", ".join(missing), ", ".join(str(d) for d in legacy_dirs))
        self._review_saved_state()
        try:
            await self.db.connect()
            try:
                await schema.create_new_tables(self.db)  # tables the old bot's database lacks
            except Exception as exc:  # noqa: BLE001 - reported by verify() below
                log.error("Could not create the tables that are new in v2 (%s: %s).",
                          type(exc).__name__, exc)
            # Columns the old bot's tables lack (the pet cosmetics).  Never fatal: when they
            # cannot be added the owner is told and the game goes on without that feature.
            try:
                not_added = await schema.add_new_columns(self.db)
            except Exception as exc:  # noqa: BLE001 - it reports instead of raising; just in case
                log.error("Could not add the columns that are new in v2 (%s: %s).",
                          type(exc).__name__, exc)
                not_added = [f"{type(exc).__name__}: {exc}"]
            problems = await schema.verify(self.db)
            if not_added:
                note = new_columns_note(not_added, problems)
                if note is not None:
                    self._startup_notes.append(note)
                else:
                    log.warning("Some new columns could not be added (%s), but none the game needs is "
                                "missing now; no note for the owner.", "; ".join(not_added))
            for problem in problems:
                log.error("Database structure differs from what the bot expects: %s", problem)
            noted = problems
            if not_added:
                # The columns that could not be added are explained by the note above (in words
                # that say what it means); the general note below is for everything else.
                explained = {schema.missing_column(table, column)
                             for table, column, _ in _importable_new_columns()}
                noted = [problem for problem in problems if problem not in explained]
            if noted:
                self._startup_notes.append("Baza danych różni się od oczekiwanej: " + "; ".join(noted)
                                           + ". Część komend może nie działać (spróbuj `!createAllDatabases`).")
            if not problems:
                log.info("Database structure verified.")
        except Exception as exc:  # noqa: BLE001 - keep starting; queries reconnect lazily
            log.error("Database unreachable at start-up (%s: %s); will keep retrying on use.",
                      type(exc).__name__, exc)
            # Nothing was verified and columns may not have been added: the owner should know
            # what may be off for this run (nothing retries the ALTER before a restart).  Only a
            # database failure gets here - the notes above never raise for a broken module.
            self._startup_notes.append(MSG_DATABASE_DOWN_AT_START.format(error=type(exc).__name__))
        if not petcards.pillow_available():
            log.error("Pillow is not installed: pet cards (frames, seals, Holo) are off.")
            self._startup_notes.append(MSG_NO_PILLOW)
        broken = import_all_modules()  # nothing is read from the disk later, see there
        if broken:
            self._startup_notes.append(MSG_MODULES_BROKEN.format(modules=", ".join(broken)))
        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                log.info("Loaded extension %s", extension)
            except Exception:  # noqa: BLE001
                log.exception("Failed to load extension %s", extension)

    def _review_saved_state(self) -> None:
        """Start-up checks of the data folder; what the owner should know goes to ``_startup_notes``.

        * Job markers (``runtime.json``) are trusted only when the bot was running
          recently.  A running bot writes that file every night; when it is older
          than :data:`STALE_STATE_AFTER`, somebody else (the old bot) may have done
          the resets meanwhile - re-running a "missed" season end days later would
          wipe the ranking in the middle of a week.  The markers are dropped, so
          every job simply waits for its next occurrence.
        * State files of the old bot that are newer than ours mean it has been
          running since v2 last wrote them; nothing is overwritten automatically.
        * Boss spawning that was switched off before the restart stays off.
        """
        notes = self._startup_notes
        runtime = self.store.runtime
        try:
            idle_for = time.time() - runtime.path.stat().st_mtime
        except OSError:
            idle_for = 0.0  # no runtime file yet: a first start, nothing to distrust
        if idle_for > STALE_STATE_AFTER:
            data = runtime.load()
            # A summoned boss from days ago is not brought back (its rarity and how it came,
            # see boss_cycle._KEY_SUMMONED / _KEY_SUMMONED_INFO).  A boss of a Zwój przywołania
            # that nobody had claimed gives its scroll back: the boss cycle pays it once it runs.
            summoned = data.pop("summoned_boss_rarity", None) is not None
            info = data.pop("summoned_boss_info", None)
            owed = boss_cycle.scroll_refund_owed(info) if summoned else None
            if owed is not None:
                data[boss_cycle.KEY_REFUND_DUE] = {**owed, "why": boss_cycle.WHY_STALE}
            if info is not None or summoned:
                runtime.save(data)
            stale = [key for key in data if key.startswith("job_last_run:")]
            if stale:
                for key in stale:
                    del data[key]
                runtime.save(data)
                days = idle_for / 86400
                log.warning("The bot last ran %.1f days ago: scheduled jobs missed meanwhile are "
                            "not caught up (%s).", days, ", ".join(stale))
                notes.append(f"Bot nie działał przez {days:.1f} dnia - zaległe zaplanowane zadania "
                             "(koniec sezonu, resety dzienne, skarb z Twitcha) NIE zostaną nadrobione; "
                             "ruszą w najbliższym zwykłym terminie.")

        legacy_dir = self.settings.legacy_dir
        if legacy_dir is not None:
            newer = []
            for name in LEGACY_STATE_FILES:
                try:
                    if ((legacy_dir / name).stat().st_mtime
                            > (self.store.data_dir / name).stat().st_mtime + 2):
                        newer.append(name)
                except OSError:
                    continue
            if newer:
                log.warning("State files in %s are newer than the ones in %s: %s", legacy_dir,
                            self.store.data_dir, ", ".join(newer))
                notes.append("Pliki stanu starego bota są nowsze niż pliki nowej wersji ("
                             + ", ".join(newer) + "). Jeśli stary bot działał po ostatnim uruchomieniu "
                             "nowej wersji: zatrzymaj bota, usuń folder `" + self.store.data_dir.name
                             + "` i uruchom go ponownie - stan zostanie przejęty od nowa.")

        if not self.boss_cycle.enabled:
            notes.append("Respawn bossów jest WYŁĄCZONY (zapamiętane `!stopSpawnBoss`). "
                         "Włącz go komendą `!startSpawnBoss`.")

    async def on_ready(self) -> None:
        # Fires again after every re-identify (e.g. a long internet outage).
        # Nothing is (re)started here on purpose: background loops are owned by
        # the supervisor and started exactly once from each cog's ``cog_load``.
        log.info("Logged in as %s (debug=%s, guilds=%d).", self.user, self.debug, len(self.guilds))
        if self._startup_notes:
            notes, self._startup_notes = self._startup_notes, []
            await self.log_to_owner("Uwagi po starcie bota:\n- " + "\n- ".join(notes), ping=True)

    async def on_message(self, message: discord.Message) -> None:
        """Every message: the fight journal looks at it, then the commands run as always.

        The journal only notes the answer a player owes a round that timed
        out (:func:`altermmo.core.fightlog.heard`: a dictionary look-up, it
        never raises and keeps nothing for itself); commands, the answers of a
        running round and every other listener get the message as before.
        """
        fightlog.heard(message)
        await self.process_commands(message)

    async def close(self) -> None:
        log.info("Shutting down...")
        await self.supervisor.stop_all()
        await fightlog.stop()  # the late-answer watches and the answers to "lag" still waiting
        await super().close()
        await self.db.close()

    # -- Discord objects --------------------------------------------------------

    @property
    def guild(self) -> discord.Guild:
        guild = self.get_guild(self.ids.guild)
        if guild is None:
            raise NotReady("Main guild is not in the cache (not connected yet?).")
        return guild

    def text_channel(self, channel_id: int) -> discord.TextChannel:
        channel = self.get_channel(channel_id)
        if channel is None:
            raise NotReady(f"Channel {channel_id} is not in the cache.")
        return channel  # type: ignore[return-value]

    @property
    def boss_channel(self) -> discord.TextChannel:
        return self.text_channel(self.ids.boss_channel)

    def boss_scene(self) -> Scene:
        """Scene for background flows playing out on the boss channel."""
        return Scene(bot=self, channel=self.boss_channel)

    def role(self, role_id: int) -> discord.Role | None:
        guild = self.get_guild(self.ids.guild)
        return guild.get_role(role_id) if guild is not None else None

    async def member(self, user_id: int | str) -> discord.Member | None:
        """Guild member by id; None when they left the server."""
        return await io.resolve_member(self.get_guild(self.ids.guild), user_id)

    def is_boss_channel(self, channel: discord.abc.Messageable | None) -> bool:
        """True on the boss channel (production or test), as legacy commands checked."""
        return getattr(channel, "id", None) in self.settings.boss_channel_ids

    # -- owner log channel ------------------------------------------------------

    async def log_to_owner(self, text: str, *, ping: bool = False) -> None:
        """Post into the owner's log channel.  Never raises."""
        try:
            channel = self.get_channel(self.ids.log_channel)
            if channel is None:
                log.warning("Log channel unavailable; message was: %s", text)
                return
            if ping:
                text = f"<@{self.ids.owner}>!   {text}"
            await io.send(channel, text[:1990])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Could not write to the log channel; message was: %s", text)

    async def _report_loop_crash(self, name: str, error: str, crashes: int) -> None:
        await self.log_to_owner(
            f"Pętla `{name}` przerwana przez błąd (raz {crashes}. z rzędu): `{error}`. "
            "Bot sam ją wznawia; szczegóły w pliku logów i w `!status`.", ping=True)

    async def _report_job_failure(self, name: str, error: str, failures: int) -> None:
        await self.log_to_owner(
            f"Zaplanowane zadanie `{name}` nie powiodło się (próba {failures}.): `{error}`. "
            "Bot będzie próbował ponownie.", ping=True)

    # -- errors -----------------------------------------------------------------

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError,
                               *, force: bool = False) -> None:
        """Central command error handling (the legacy bot only printed errors).

        Commands with their own ``@command.error`` handler are skipped; such a
        handler passes everything it does not handle itself back with
        ``await ctx.bot.on_command_error(ctx, error, force=True)``.
        """
        if not force and ctx.command is not None and ctx.command.has_error_handler():
            return
        original = getattr(error, "original", error)

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandOnCooldown):
            await self._reply(ctx, cooldown_text(error.retry_after))
            return
        if isinstance(error, commands.UserInputError):  # missing / bad argument, unbalanced quote...
            usage = f"{COMMAND_PREFIX}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            await self._reply(ctx, f"Niepoprawne użycie komendy. Spróbuj: **{usage}**")
            return
        if isinstance(error, commands.CheckFailure):
            return  # no permission / wrong place: stay silent, as before
        if isinstance(original, DatabaseUnavailable):
            log.error("Command %s failed: database unavailable (%s)", ctx.command, original)
            await self._reply(ctx, "Baza danych jest chwilowo niedostępna. Spróbuj ponownie za moment "
                                   "<:Sadge:936907659142111273>")
            return

        log.error("Unhandled error in command %s (author %s): %s",
                  ctx.command, ctx.author, "".join(traceback.format_exception(original)))
        await self._reply(ctx, "Coś poszło nie tak przy tej komendzie. Spróbuj ponownie za chwilę "
                               "<:Sadge:936907659142111273>")
        await self.log_to_owner(f"Błąd komendy `{ctx.command}` ({ctx.author}): "
                                f"`{type(original).__name__}: {original}`")

    async def _reply(self, ctx: commands.Context, text: str) -> None:
        try:
            await io.send(ctx.channel, text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("Could not send an error reply.", exc_info=True)

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        log.exception("Unhandled error in event handler %s", event_method)
