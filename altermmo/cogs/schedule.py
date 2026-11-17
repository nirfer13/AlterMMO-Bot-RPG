"""Wall-clock jobs: weekly season end, daily cooldown reset, Twitch treasure.

Port of ``weekly_ranking`` and ``twitch_sync`` from the legacy
``cogs/spawnboss.py``.  All times are Polish local time (the legacy code used a
fixed UTC+2 offset, so in winter everything ran an hour early).  Jobs remember
their last run on disk: an occurrence missed during downtime is caught up, and
nothing can run twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable

from discord.ext import commands

from ..core import io, scheduler

log = logging.getLogger(__name__)

WEEKLY_JOB = "weekly-ranking"
DAILY_JOB = "daily-reset"
TWITCH_JOB = "twitch-treasure"

SEASON_END_MESSAGE = ("<@&{hunters}>! Ranking za tydzień polowań został zresetowany. "
                      "Nowa rola <@&{slayer}> została przydzielona <@{winner}>! "
                      "Gratulacje <:GigaChad:970665721321381958>")


class ScheduleCog(commands.Cog, name="Schedule"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        jobs = (
            scheduler.weekly(WEEKLY_JOB, 0, 15, 0, self.weekly_season_end),   # Monday 15:00
            scheduler.daily(DAILY_JOB, 2, 30, self.daily_reset),              # every day 02:30
            scheduler.daily(TWITCH_JOB, 4, 0, self.twitch_treasure),          # every day 04:00
        )
        for job in jobs:
            self.bot.supervisor.start(job.name, self._runner(job))

    def _runner(self, job: scheduler.Job):
        async def run() -> None:
            await self.bot.wait_until_ready()
            await scheduler.run_job(job, self.bot.store)
        return run

    async def cog_unload(self) -> None:
        for name in (WEEKLY_JOB, DAILY_JOB, TWITCH_JOB):
            self.bot.supervisor.stop(name)

    async def _step(self, what: str, awaitable: Awaitable) -> bool:
        """Run one best-effort step; failures are logged and reported to the owner."""
        try:
            await awaitable
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduled step failed: %s", what)
            await self.bot.log_to_owner(f"Błąd zaplanowanego zadania ({what}): "
                                        f"`{type(exc).__name__}: {exc}`", ping=True)
            return False

    # -- jobs ---------------------------------------------------------------------

    async def weekly_season_end(self) -> None:
        """Monday 15:00: announce the ranking, crown the boss slayer, reset everything."""
        from ..features import achievements, boss, daily, database, skills, tower

        bot = self.bot
        scene = bot.boss_scene()
        log.info("Weekly ranking summary!")

        # Until the ranking is emptied a failure aborts the job (it is retried
        # later with the ranking still intact, and nothing was handed out yet).
        # Afterwards every step is best effort: re-running the whole job would
        # crown the wrong winner or count the butcher achievement twice.
        winner_id = await database.read_summary_ranking_table(scene)
        log.info("Season winner: %s", winner_id)
        await database.reset_ranking_table(bot)

        await self._step("rola Rzeźnika Potworów", boss.set_boss_slayer(bot, winner_id))
        winner = await bot.member(winner_id)
        if winner is not None:
            await self._step("osiągnięcie Rzeźnika", achievements.add_butcher(scene, winner))

        await self._step("reset dziennych polowań", daily.clear_daily_file(bot))
        await self._step("reset wieży", tower.clear_weekly_tower_file(bot))
        await self._step("reset tygodniowych umiejętności", skills.clear_weekly_skill(bot))
        await self._step("reset dziennych umiejętności", skills.clear_daily_skill(bot))
        await io.send_patient(scene.channel, SEASON_END_MESSAGE.format(
            hunters=bot.ids.hunters_role, slayer=bot.ids.boss_slayer_role, winner=winner_id))

    async def daily_reset(self) -> None:
        """02:30: everybody may hunt and use daily skills again."""
        from ..features import daily, skills

        ok_hunts = await self._step("reset dziennych polowań", daily.clear_daily_file(self.bot))
        ok_skills = await self._step("reset dziennych umiejętności", skills.clear_daily_skill(self.bot))
        if not (ok_hunts and ok_skills):
            raise RuntimeError("daily reset incomplete")  # the scheduler retries it

    async def twitch_treasure(self) -> None:
        """04:00: reward Twitch viewers (data comes from the Twitch bot's tables)."""
        from ..features import twitch

        log.info("Twitch check - treasure.")
        await twitch.check_treasure(self.bot)


async def setup(bot) -> None:
    await bot.add_cog(ScheduleCog(bot))
