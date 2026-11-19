"""Configuration: environment, Discord ids, timings, paths.

Everything that used to be scattered through the code as magic numbers
(channel ids, role ids, the ``DebugMode`` switch from ``globals/``) lives here.

Environment variables (``.env`` file in the project root):

``TOKEN``                         Discord bot token (required)
``DATABASE_URL``                  production PostgreSQL URL
``HEROKU_POSTGRESQL_BRONZE_URL``  test PostgreSQL URL (used in debug mode)
``ALTERMMO_DEBUG``                ``1`` -> debug mode: test channel, test database,
                                  short timers (replaces ``globals/globalvariables.py``)
``ALTERMMO_DATA_DIR``             where cooldown/modifier files live (default ``data``,
                                  in debug mode ``data-debug``)
``ALTERMMO_LOG_DIR``              where log files go (default ``logs``)
``ALTERMMO_BACKUP_DIR``           where the copies of the database go (default ``backups``,
                                  in debug mode ``backups-debug``); see features/backup.py
``ALTERMMO_LEGACY_DIR``           folder of the old bot; its state files are copied into the
                                  data folder on the first production start (default: the
                                  sibling folder ``AlterMMO-Bot-RPG``; ``none`` = do not copy)

Optional gameplay switches (see the policy fields of :class:`Settings`):

``ALTERMMO_BUSY_POLICY``              ``notify`` (default) | ``read_only_free`` | ``silent``
``ALTERMMO_BOSS_VISIBLE_MINUTES``     minutes before an unattacked boss leaves (0/unset = never)
``ALTERMMO_BOSS_RETURNS_AFTER_ERROR`` ``1`` (default) boss stays after a technical failure, ``0`` it escapes
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
CONFIG_DIR = PROJECT_ROOT / "config"

COMMAND_PREFIX = "$"


@dataclass(frozen=True)
class Ids:
    """Discord ids shared by several features.

    Ids used by exactly one feature (patron tiers, Twitch roles, achievement
    roles) are constants at the top of that feature's module.
    """

    guild: int = 686137998177206281
    #: The owner (Alter) - pinged in the log channel, default boss slayer.
    owner: int = 291836779495948288

    #: Channel where the whole RPG happens.
    boss_channel: int = 970684202880204831
    #: General chat: "a monster appeared" announcements.
    announce_channel: int = 696932659833733131
    #: Chat where "you died" notices are posted.
    notice_channel: int = 776379796367212594
    #: Private log channel for the owner (loot, exp to hand out, errors).
    log_channel: int = 881090112576962560

    #: Role pinged for boss fights ("łowcy").
    hunters_role: int = 985071779787730944
    #: Weekly ranking winner ("Rzeźnik Potworów" / boss slayer).
    boss_slayer_role: int = 983798433590673448
    #: Players banned from fights until the next boss dies.
    dead_hunter_role: int = 1091050836303544402
    #: Patrons who killed a mythic boss.
    mythic_slayer_role: int = 1118242273772175401


#: Debug mode plays on the test channel and announces into the log channel.
_DEBUG_IDS = Ids(
    boss_channel=970571647226642442,
    announce_channel=881090112576962560,
)


@dataclass(frozen=True)
class Timings:
    """Durations in seconds.  Debug mode shortens them to make testing bearable."""

    #: Random respawn delay is ``randint(min, max) * respawn_multiplier`` seconds.
    respawn_min: int = 150
    respawn_max: int = 3600
    respawn_multiplier: int = 12
    #: Quiet period after a fight before the channel is cleared.
    post_fight_pause: float = 3600.0
    #: "The wind grows stronger" -> boss appears.
    pre_spawn_min: float = 4 * 60.0
    pre_spawn_max: float = 7 * 60.0
    #: Pause between random events.
    event_gap_min: float = 2000.0
    event_gap_max: float = 3000.0
    #: A summoned (ritual / skill gem) boss leaves when the scheduled boss is due,
    #: but never sooner than this after it appeared.
    summoned_min_stay: float = 600.0


_DEBUG_TIMINGS = Timings(
    respawn_min=75,
    respawn_max=84,
    respawn_multiplier=1,
    post_fight_pause=3.0,
    pre_spawn_min=3.0,
    pre_spawn_max=10.0,
    event_gap_min=15.0,
    event_gap_max=20.0,
    summoned_min_stay=20.0,
)


@dataclass(frozen=True)
class Settings:
    token: str
    database_url: str
    debug: bool
    data_dir: Path
    log_dir: Path
    #: Folder of the old bot.  On the first production start the state files
    #: that are still missing in ``data_dir`` (cooldown lists, shrine modifiers,
    #: exp summary) are copied from there.  None = nothing to take over.
    legacy_dir: Path | None = None
    #: Folder for the copies of the database (features/backup.py).  None = ``backups``
    #: next to ``data_dir``.
    backup_dir: Path | None = None
    ids: Ids = field(default_factory=Ids)
    timings: Timings = field(default_factory=Timings)

    # -- gameplay policies (the places where v2 can deliberately differ) ---------

    #: What commands do while an event/hunt/tower run is in progress:
    #: "read_only_free" | "notify" | "silent" - see altermmo/core/guards.py.
    #: Owner's decision: everything is blocked, but the bot says so.
    busy_policy: str = "notify"
    #: Seconds an unattacked boss stays on the channel before it leaves and a
    #: new respawn is scheduled.  None = it waits forever (legacy behaviour).
    boss_visible_timeout: float | None = None
    #: A fight broken by a technical problem (Discord/database outage):
    #: True  -> the boss stays and can be attacked again,
    #: False -> the boss escapes and a new respawn is scheduled.
    boss_returns_after_interrupted_fight: bool = True
    #: Hard limit for one whole boss encounter (gathering players + fight).
    boss_fight_time_limit: float = 2 * 3600.0

    @property
    def boss_channel_ids(self) -> tuple[int, int]:
        """Both boss channels (production and test), as accepted by legacy commands."""
        return (Ids().boss_channel, _DEBUG_IDS.boss_channel)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on", "tak"}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def load_settings(*, env_file: Path | None = None) -> Settings:
    """Read ``.env`` + the process environment into a :class:`Settings`."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    debug = _flag(os.environ.get("ALTERMMO_DEBUG"))
    database_url = _env("HEROKU_POSTGRESQL_BRONZE_URL") if debug else _env("DATABASE_URL")
    # Debug runs keep their state apart: test cooldowns, a test exp summary or a
    # "spawning off" switch must never end up in the production files.
    data_dir = Path(_env("ALTERMMO_DATA_DIR") or PROJECT_ROOT / ("data-debug" if debug else "data"))
    log_dir = Path(_env("ALTERMMO_LOG_DIR") or PROJECT_ROOT / "logs")
    backup_dir = Path(_env("ALTERMMO_BACKUP_DIR") or PROJECT_ROOT / ("backups-debug" if debug else "backups"))
    policies: dict = {}
    # The old bot's files are production state: never mix them into a debug run.
    legacy_text = _env("ALTERMMO_LEGACY_DIR")
    if not debug and legacy_text.lower() not in {"none", "-", "0"}:
        legacy_dir = Path(legacy_text) if legacy_text else PROJECT_ROOT.parent / "AlterMMO-Bot-RPG"
        if not legacy_dir.is_absolute():
            legacy_dir = PROJECT_ROOT / legacy_dir
        if legacy_dir.is_dir() and legacy_dir.resolve() != PROJECT_ROOT.resolve():
            policies["legacy_dir"] = legacy_dir
    if _env("ALTERMMO_BUSY_POLICY"):
        policies["busy_policy"] = _env("ALTERMMO_BUSY_POLICY")
    if _env("ALTERMMO_BOSS_VISIBLE_MINUTES"):
        minutes = float(_env("ALTERMMO_BOSS_VISIBLE_MINUTES"))
        policies["boss_visible_timeout"] = minutes * 60 if minutes > 0 else None
    if _env("ALTERMMO_BOSS_RETURNS_AFTER_ERROR"):
        policies["boss_returns_after_interrupted_fight"] = _flag(_env("ALTERMMO_BOSS_RETURNS_AFTER_ERROR"))
    return Settings(
        token=_env("TOKEN"),
        database_url=database_url,
        debug=debug,
        data_dir=data_dir if data_dir.is_absolute() else PROJECT_ROOT / data_dir,
        log_dir=log_dir if log_dir.is_absolute() else PROJECT_ROOT / log_dir,
        backup_dir=backup_dir if backup_dir.is_absolute() else PROJECT_ROOT / backup_dir,
        ids=_DEBUG_IDS if debug else Ids(),
        timings=_DEBUG_TIMINGS if debug else Timings(),
        **policies,
    )
