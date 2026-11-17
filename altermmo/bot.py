"""The bot object: wiring of configuration, database, game state and cogs."""

from __future__ import annotations

import asyncio
import logging
import traceback
from pathlib import Path

import discord
from discord.ext import commands

from . import schema
from .core import io
from .core.db import Database, DatabaseUnavailable
from .core.net import ConnectionMonitor
from .core.scene import Scene
from .core.state import GameState
from .core.storage import IdListFile, JsonFile, migrate_legacy_file
from .core.supervisor import Supervisor
from .features.boss_cycle import BossCycle
from .settings import COMMAND_PREFIX, PROJECT_ROOT, Ids, Settings, Timings

log = logging.getLogger(__name__)

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
    "altermmo.cogs.xpsteal",
    "altermmo.cogs.twitch",
    "altermmo.cogs.schedule",
    "altermmo.cogs.admin",
    "altermmo.cogs.general",
    "altermmo.cogs.moderation",
    "altermmo.cogs.help",
)

#: State files carried over from the legacy bot (same names, same formats).
LEGACY_STATE_FILES: tuple[str, ...] = (
    "bossModifier.json",
    "expSummary.json",
    "daily_player_cd.txt",
    "weekly_tower_cd.txt",
    "skill_daily_player_cd.txt",
    "skill_weekly_player_cd.txt",
)


class NotReady(RuntimeError):
    """A Discord object (guild/channel) is not available in the cache yet."""


class Stores:
    """File-backed state, see :mod:`altermmo.core.storage`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        #: Players who already used ``$polowanie`` today.
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

    def migrate_legacy(self, legacy_dirs: list[Path]) -> None:
        for name in LEGACY_STATE_FILES:
            migrate_legacy_file(name, self.data_dir, legacy_dirs)

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
            command_prefix=COMMAND_PREFIX,
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
        self.store.migrate_legacy([PROJECT_ROOT])
        try:
            await self.db.connect()
            problems = await schema.verify(self.db)
            for problem in problems:
                log.error("Database structure differs from what the bot expects: %s", problem)
            if not problems:
                log.info("Database structure verified.")
        except Exception as exc:  # noqa: BLE001 - keep starting; queries reconnect lazily
            log.error("Database unreachable at start-up (%s: %s); will keep retrying on use.",
                      type(exc).__name__, exc)
        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                log.info("Loaded extension %s", extension)
            except Exception:  # noqa: BLE001
                log.exception("Failed to load extension %s", extension)

    async def on_ready(self) -> None:
        # Fires again after every re-identify (e.g. a long internet outage).
        # Nothing is (re)started here on purpose: background loops are owned by
        # the supervisor and started exactly once from each cog's ``cog_load``.
        log.info("Logged in as %s (debug=%s, guilds=%d).", self.user, self.debug, len(self.guilds))

    async def close(self) -> None:
        log.info("Shutting down...")
        await self.supervisor.stop_all()
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
            hours = round(error.retry_after / 60 / 60, 2)
            await self._reply(ctx, f"Poczekaj na odnowienie komendy! Zostało {hours} godzin/y "
                                   "<:Bedge:970576892874854400>.")
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                              commands.TooManyArguments)):
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
