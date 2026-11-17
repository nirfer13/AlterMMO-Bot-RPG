"""Boss commands: ``$zaatakuj`` and the admin controls of the boss cycle.

The life-cycle itself lives in :mod:`altermmo.features.boss_cycle`; this cog
only starts its supervised loop and forwards commands to it.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import boss_cycle

log = logging.getLogger(__name__)


class BossCog(commands.Cog, name="Boss"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.boss_cycle.start()

    async def cog_unload(self) -> None:
        self.bot.supervisor.stop(boss_cycle.LOOP_NAME)

    # -- players ------------------------------------------------------------------

    @commands.command(name="zaatakuj", brief="Attacking the boss")
    @guards.boss_channel_only()
    async def attack(self, ctx: commands.Context) -> None:
        """Attack the boss standing on the channel."""
        await self.bot.boss_cycle.attack(Scene.from_ctx(ctx))

    # -- admin: spawn control -----------------------------------------------------

    @commands.command(name="startSpawnBoss", brief="Starts spawning boss")
    @guards.owner_or_admin()
    async def start_spawning(self, ctx: commands.Context) -> None:
        await self.bot.boss_cycle.enable()
        await io.send(ctx.channel, "Respawn bossów włączony.")

    @commands.command(name="stopSpawnBoss", brief="Stops spawning boss")
    @guards.owner_or_admin()
    async def stop_spawning(self, ctx: commands.Context) -> None:
        await self.bot.boss_cycle.disable()
        await io.send(ctx.channel, "Respawn bossów wyłączony (do ponownego $startSpawnBoss).")

    @commands.command(name="checkSpawnBoss", brief="Checking boss spawn time")
    @guards.owner_or_admin()
    async def check_spawn(self, ctx: commands.Context) -> None:
        cycle = self.bot.boss_cycle
        seconds = cycle.seconds_to_arrival()
        phase = self.bot.state.boss_phase.name
        if not cycle.enabled:
            await io.send(ctx.channel, "Respawn bossów jest wyłączony.")
        elif seconds is None:
            await io.send(ctx.channel, f"Brak zaplanowanego respawnu (faza: {phase}).")
        else:
            await io.send(ctx.channel, f"Resp time is {max(seconds, 0) / 60 / 60} hours. "
                                       f"(faza: {phase}, rzadkość: {self.bot.state.boss_rarity})")

    @commands.command(name="ResetBoss")
    @guards.owner_or_admin()
    async def reset_boss(self, ctx: commands.Context) -> None:
        """Drop the current boss/event state and restart every background loop."""
        await self.bot.boss_cycle.reset()
        for name in self.bot.supervisor.names():
            if name != boss_cycle.LOOP_NAME:
                self.bot.supervisor.restart(name)
        await io.send(ctx.channel, "Boss reset.")

    # -- admin: debug helpers -----------------------------------------------------

    @commands.command(name="rarity")
    @guards.owner_or_admin()
    async def rarity(self, ctx: commands.Context, time: int) -> None:
        """Rarity a boss would get for a respawn delay of ``time`` seconds."""
        from ..features import boss
        await io.send(ctx.channel, str(boss.rarity_for_respawn_time(time, debug=self.bot.debug)))

    @commands.command(name="image")
    @guards.owner_or_admin()
    async def boss_image(self, ctx: commands.Context, rarity: int) -> None:
        """Post the picture of a boss of the given rarity (0-4)."""
        from ..features import boss
        await boss.send_boss_image(Scene.from_ctx(ctx), max(0, min(4, rarity)))

    @commands.command(name="loot")
    @guards.owner_or_admin()
    async def loot(self, ctx: commands.Context, rarity: int, boost_percent: int) -> None:
        """Roll boss loot for yourself (debug)."""
        from ..features import boss
        await boss.rand_loot(Scene.from_ctx(ctx), rarity, ctx.author, boost_percent)

    @commands.command(name="status")
    @guards.owner_or_admin()
    async def status(self, ctx: commands.Context) -> None:
        """Health report: game state, background loops, database, gateway."""
        bot = self.bot
        lines = ["**Stan gry**"]
        lines += [f"- {key}: `{value}`" for key, value in bot.state.snapshot().items()]
        seconds = bot.boss_cycle.seconds_to_arrival()
        lines.append(f"- boss za: `{round(seconds) if seconds is not None else '-'} s` "
                     f"(respawn włączony: `{bot.boss_cycle.enabled}`)")
        lines.append("**Pętle w tle**")
        for name, info in bot.supervisor.status().items():
            lines.append(f"- {name}: działa=`{info['running']}`, restarty=`{info['restarts']}`, "
                         f"ostatni błąd=`{info['last_error']}`")
        lines.append("**Połączenia**")
        lines.append(f"- baza: `{'OK' if await bot.db.ping() else 'NIEDOSTĘPNA'}`")
        lines.append(f"- gateway: połączony=`{bot.net.connected}`, rozłączeń=`{bot.net.disconnect_count}`, "
                     f"ping=`{round((bot.latency or 0) * 1000)} ms`")
        await io.send(ctx.channel, "\n".join(lines)[:1990])


async def setup(bot) -> None:
    await bot.add_cog(BossCog(bot))
