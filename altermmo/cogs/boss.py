"""Boss commands: ``!zaatakuj`` and the admin controls of the boss cycle.

The life-cycle itself lives in :mod:`altermmo.features.boss_cycle`; this cog
only starts its supervised loop, registers the permanent "Zaatakuj" button
(it works under boss pictures from before a restart too) and forwards
commands to the cycle.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core import guards, io, ui
from ..core.scene import Scene
from ..features import boss_cycle

log = logging.getLogger(__name__)

#: Second word of ``!image`` that asks for the special variant of the boss (compared casefolded).
SPECIAL_WORDS = frozenset({"s", "special", "specjalny", "specjalna", "wariant", "1", "tak", "t", "yes", "true"})
#: Under an ``!image`` preview that shows a special boss: its embed says what a real one says.
#: (Public like every answer of the bot; names no ornament and no chance.)
MSG_PREVIEW_ONLY = ("*To tylko podgląd wyjątkowej odsłony bossa - nie zmienia gry i niczego nie obiecuje. "
                    "Przycisk pod podglądem atakuje wyłącznie prawdziwego bossa, jeśli ten stoi na kanale.*")
#: ``!image <rarity> s`` could not show the special variant of the picture it drew.
MSG_PREVIEW_NO_VARIANT = ("Podgląd: ten portret nie ma wyjątkowej odsłony albo brakuje jej pliku, więc widać "
                          "zwykłego bossa. Sprawdź `config/bossVariants.json` i folder `assets/boss/special` "
                          "(szczegóły w logu bota).")


class BossCog(commands.Cog, name="Boss"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        ui.register(self.bot, boss_cycle.AttackView())
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
        await io.send(ctx.channel, "Respawn bossów wyłączony (do ponownego !startSpawnBoss).")

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
    async def boss_image(self, ctx: commands.Context, rarity: int, variant: str = "") -> None:
        """Post the picture of a boss of the given rarity (0-4): ``!image <rarity> [wariant]``.

        Without the second word the picture is rolled like a real spawn (now
        and then a special variant, now and then a player as the boss, see
        features/bossvariants.py); with one of :data:`SPECIAL_WORDS` ("s",
        "specjalny" ...) the special variant is forced: a monster is always
        shown (``monster_only`` - no supporter is drawn, the shrine's player
        is not asked for), in its framed portrait.  A preview only: the boss
        on the channel, its variant and the loot are not touched.

        The preview looks exactly like a real special boss (the hint about the
        egg, the "Zaatakuj" button, which attacks the REAL boss if one stands
        there), so a special portrait is followed by :data:`MSG_PREVIEW_ONLY`.
        A forced preview that could not be special (the drawn picture has no
        variant, its file is missing, the config is broken) says so instead of
        silently showing an ordinary boss (:data:`MSG_PREVIEW_NO_VARIANT`).
        """
        from ..features import boss, bossvariants
        forced = variant.strip().casefold() in SPECIAL_WORDS
        shown = await boss.send_boss_image(Scene.from_ctx(ctx), max(0, min(4, rarity)),
                                           special=True if forced else None, monster_only=forced,
                                           register=False)  # (not the boss: never replaced nor retired as it)
        if bossvariants.variant_of(shown):
            await io.send(ctx.channel, MSG_PREVIEW_ONLY)
        elif forced:
            await io.send(ctx.channel, MSG_PREVIEW_NO_VARIANT)

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
        # (An empty text - boss_variant of an ordinary boss - is shown as "-": an empty code span
        # is no code span on Discord, just two stray backticks.  0 and False are values: they stay.)
        lines += [f"- {key}: `{'-' if value == '' else value}`" for key, value in bot.state.snapshot().items()]
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
