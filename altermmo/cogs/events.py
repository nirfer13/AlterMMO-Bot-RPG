"""Random events on the boss channel and the weekly PvP tournament.

Port of ``spawn_event`` / ``pvp_tournament_scheduler`` from the legacy
``cogs/spawnboss.py``.  The legacy loop had no error handling at all: the first
failed Discord or database call inside any event killed it for good, usually
with ``BUSY``/``EVENT_ALIVE`` left set ("events stopped working").  Here the
loop is supervised, every event runs inside ``state.running_event`` (flags are
always released) and a failing event is logged and skipped.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta

from discord.ext import commands

from ..core import clock, guards, io, scheduler
from ..core.scene import Scene
from ..core.state import EventType

log = logging.getLogger(__name__)

LOOP_NAME = "event-loop"
TOURNAMENT_JOB = "pvp-tournament"

#: Order matters: the weight tuples below are positional (legacy order).
EVENT_ORDER = (EventType.SHRINE, EventType.CHEST, EventType.INVASION, EventType.PARTY,
               EventType.MEMORY, EventType.HUNTING, EventType.HAZARD, EventType.DUEL,
               EventType.RITUAL)
#: Evenings (18-21) and weekends: more multiplayer events.  The ritual (last
#: place) was switched off in the old bot (weight 0); the owner enabled it in v2.
PEAK_WEIGHTS = (1, 1, 2, 1, 1, 1, 1, 2, 1)
OFF_PEAK_WEIGHTS = (2, 2, 1, 0, 2, 2, 2, 0, 1)
DEBUG_WEIGHTS = (0, 0, 0, 0, 0, 0, 0, 0, 1)
PEAK_HOURS = range(18, 22)
#: A ritual is not started when the scheduled boss is due sooner than the
#: ritual lasts plus this margin (seconds): its boss could not be summoned.
RITUAL_MARGIN, RITUAL_MARGIN_DEBUG = 180.0, 10.0

#: Passive events: posted, then they wait for ``$modlitwa`` / ``$skrzynia``.
PASSIVE = (EventType.SHRINE, EventType.CHEST)

MSG_NOT_A_BELIEVER = ("Nie umiesz pacierza. Poczekaj na kogoś bardziej wierzącego. "
                      "<:prayge:1063891597760139304>")
MSG_NO_SHRINE = "Do kogo Ty chcesz się modlić? Przecież tu nic nie ma..."
MSG_RITUAL_BOSS_GONE = ("Przywołany potwór rozpłynął się w powietrzu - nadciąga coś innego... "
                        "<:Hmm:984767035617730620>")
MSG_NO_CHEST ="Powiedz mi... Czy ta skrzynia jest tutaj z nami w pokoju? <:Hmm:984767035617730620>"


def pick_event(now, *, debug: bool, exclude: tuple[EventType, ...] = ()) -> EventType | None:
    """Draw the next event type (legacy weights); None when nothing is left to draw."""
    if debug:
        weights = DEBUG_WEIGHTS
    elif now.hour in PEAK_HOURS or clock.is_weekend(now):
        weights = PEAK_WEIGHTS
    else:
        weights = OFF_PEAK_WEIGHTS
    weights = tuple(0 if event in exclude else weight for event, weight in zip(EVENT_ORDER, weights))
    if not any(weights):
        return None
    return random.choices(EVENT_ORDER, weights=weights)[0]


class EventsCog(commands.Cog, name="Events"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.supervisor.start(LOOP_NAME, self.event_loop)
        job = scheduler.weekly(TOURNAMENT_JOB, 5, 19, 0, self.run_tournament,
                               catch_up_within=timedelta(hours=2))
        self.bot.supervisor.start(TOURNAMENT_JOB, lambda: scheduler.run_job(job, self.bot.store))

    async def cog_unload(self) -> None:
        self.bot.supervisor.stop(LOOP_NAME)
        self.bot.supervisor.stop(TOURNAMENT_JOB)

    # -- the loop -----------------------------------------------------------------

    async def event_loop(self) -> None:
        bot = self.bot
        await bot.wait_until_ready()
        log.info("Event loop started.")
        while True:
            timings = bot.timings
            await asyncio.sleep(random.uniform(timings.event_gap_min, timings.event_gap_max))
            await self.run_one_event()

    async def run_one_event(self, forced: EventType | None = None) -> EventType | None:
        """Close the previous passive event and (if the channel is free) run a new one.

        Returns the event that ran, or None when the slot was skipped.
        """
        bot = self.bot
        state = bot.state
        if state.event_alive and state.event_type in PASSIVE:
            state.close_event()  # an unclaimed shrine/chest expires now

        event_type = forced or pick_event(clock.now(), debug=bot.debug)
        if forced is None and event_type is EventType.RITUAL and not self.ritual_fits():
            # The scheduled boss is too close: play something else in this slot.
            event_type = pick_event(clock.now(), debug=bot.debug, exclude=(EventType.RITUAL,))
            log.info("Ritual does not fit before the scheduled boss; drawn instead: %s.",
                     event_type.name if event_type else "nothing")
        if event_type is None:
            return None
        if not state.free_for_activity:
            log.info("Event %s skipped (boss phase %s, busy=%s, event=%s).", event_type.name,
                     state.boss_phase.name, state.busy_owner, state.event_type.name)
            return None

        log.info("Event: %s", event_type.name)
        try:
            await self._run(event_type)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken event must not stop the others
            log.exception("Event %s failed.", event_type.name)
            await bot.log_to_owner(f"Event {event_type.name} przerwany przez błąd: "
                                   f"`{type(exc).__name__}: {exc}`")
            return None
        return event_type

    def ritual_fits(self) -> bool:
        """True when a whole ritual can be played before the scheduled boss arrives.

        The ritual puts its own boss on the channel; that is only possible while
        no boss is there or on its way.  A ritual that ends after the "wind"
        announcement would be wasted, so it is not started that late.
        """
        from ..features import events

        cycle = self.bot.boss_cycle
        if not cycle.enabled or not cycle.can_summon():
            return False  # ($stopSpawnBoss means no bosses at all, ritual ones included)
        remaining = cycle.seconds_to_arrival()
        margin = RITUAL_MARGIN_DEBUG if self.bot.debug else RITUAL_MARGIN
        return remaining is None or remaining > events.ritual_duration(self.bot) + margin

    async def _run(self, event_type: EventType) -> None:
        from ..features import events, modifiers

        bot = self.bot
        state = bot.state
        scene = bot.boss_scene()

        if event_type in PASSIVE:
            with state.hold_busy(f"event-spawn:{event_type.name}"):
                await io.clear_channel(scene.channel)
                if event_type is EventType.SHRINE:
                    await modifiers.spawn_modifier_shrine(scene)
                else:
                    await events.spawn_chest(scene)
            state.open_event(event_type)
            return

        runners = {
            EventType.MEMORY: events.spawn_memory,
            EventType.INVASION: events.spawn_invasion,
            EventType.PARTY: events.spawn_party,
            EventType.HUNTING: events.spawn_hunting,
            EventType.HAZARD: events.spawn_hazard,
            EventType.DUEL: events.spawn_duel,
        }
        # The duel and the ritual keep the channel free while they wait for
        # volunteers (as the old bot did: hunts, the tower and pet commands work
        # meanwhile); they take the busy flag themselves for the fight / for
        # putting the summoned boss on the channel.
        open_sign_up = event_type in (EventType.DUEL, EventType.RITUAL)
        with state.running_event(event_type, exclusive=not open_sign_up):
            await io.clear_channel(scene.channel)
            if event_type is EventType.RITUAL:
                success, rarity, is_player_boss, player_boss = await events.spawn_ritual(scene)
            else:
                await runners[event_type](scene)
                return
        # The ritual summons a boss; hand it to the boss cycle once the event flags are free.
        if success:
            summoned = await bot.boss_cycle.summon(scene, rarity, show_image=False,
                                                   is_player_boss=is_player_boss,
                                                   player_boss=player_boss)
            if not summoned:
                # Cannot happen in practice (spawn_ritual checked a moment ago and
                # nothing was awaited since); if it does, say that this boss is not real.
                log.warning("Ritual boss could not be summoned (boss phase %s).",
                            state.boss_phase.name)
                await io.send(scene.channel, MSG_RITUAL_BOSS_GONE)

    # -- weekly tournament --------------------------------------------------------

    async def run_tournament(self) -> None:
        """PvP tournament, Saturdays 19:00.

        Like the old bot it waits as long as needed for a free channel (a boss
        that nobody attacks, a running event); the owner is told once when the
        wait gets long.
        """
        from ..features import events

        bot = self.bot
        state = bot.state
        log.info("PvP Tournament time reached. Waiting for free state...")
        waiting_since = clock.monotonic()
        owner_told = False
        while not state.free_for_activity:
            if state.event_alive and state.event_type in PASSIVE:
                state.close_event()
                continue
            if not owner_told and clock.monotonic() - waiting_since >= 2 * 3600:
                owner_told = True
                log.warning("PvP tournament still waiting for a free channel (boss phase %s).",
                            state.boss_phase.name)
                await bot.log_to_owner("Turniej PvP czeka już 2 godziny na wolny kanał "
                                       f"(boss: {state.boss_phase.name}). Ruszy, gdy kanał się zwolni.")
            await asyncio.sleep(30)

        scene = bot.boss_scene()
        try:
            with state.running_event(EventType.PVPTOURNAMENT):
                await io.clear_channel(scene.channel)
                await events.spawn_pvp_tournament(scene)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - do not re-run a half-played tournament
            log.exception("PvP tournament failed.")
            await bot.log_to_owner(f"Turniej PvP przerwany przez błąd: `{type(exc).__name__}: {exc}`")
        log.info("Scheduled PvP Tournament finished.")

    # -- player commands ----------------------------------------------------------

    @commands.command(name="modlitwa", brief="Patron or Creator can pray to the shrine.")
    @guards.boss_channel_only()
    async def pray(self, ctx: commands.Context) -> None:
        from ..features import modifiers, patrons

        state = self.bot.state
        if not state.try_claim_event(EventType.SHRINE):
            await io.send(ctx.channel, MSG_NO_SHRINE)
            return
        if not patrons.check_if_patron(ctx.author):
            state.reopen_event(EventType.SHRINE)
            await io.send(ctx.channel, MSG_NOT_A_BELIEVER)
            return
        try:
            await io.safe_react(ctx.message, "<:prayge:1063891597760139304>")
            await modifiers.random_modifiers(Scene.from_ctx(ctx), True)
        except BaseException:
            state.reopen_event(EventType.SHRINE)  # the prayer did not happen - shrine stays
            raise
        state.close_event()

    @commands.command(name="skrzynia", brief="Everyone can open a chest.")
    @guards.boss_channel_only()
    async def open_chest(self, ctx: commands.Context) -> None:
        from ..features import events

        state = self.bot.state
        if not state.try_claim_event(EventType.CHEST):
            await io.send(ctx.channel, MSG_NO_CHEST)
            return
        try:
            await events.open_chest(Scene.from_ctx(ctx))
        except BaseException:
            state.reopen_event(EventType.CHEST)  # nothing was looted - chest stays
            raise
        state.close_event()

    @commands.command(name="zabojca", aliases=["asasyn", "zabójca"], brief="Assinate the ranking player.")
    @commands.cooldown(1, 60 * 60 * 23, commands.BucketType.user)
    async def assassinate(self, ctx: commands.Context) -> None:
        from ..features import events

        if ctx.channel.id != self.bot.ids.boss_channel:
            ctx.command.reset_cooldown(ctx)
            return
        # The 23 h cooldown is only spent on an order that was really placed.
        placed = False
        try:
            placed = bool(await events.assasinate(Scene.from_ctx(ctx)))
        finally:
            if not placed:
                ctx.command.reset_cooldown(ctx)

    # -- admin --------------------------------------------------------------------

    @commands.command(name="SpawnParty", brief="Spawn a party.")
    @guards.owner_or_admin()
    async def spawn_party(self, ctx: commands.Context) -> None:
        await self._forced(ctx, EventType.PARTY)

    @commands.command(name="SpawnMemory", brief="Spawn a memory game.")
    @guards.owner_or_admin()
    async def spawn_memory(self, ctx: commands.Context) -> None:
        await self._forced(ctx, EventType.MEMORY)

    @commands.command(name="SpawnShrine", brief="Spawn a shrine.")
    @guards.owner_or_admin()
    async def spawn_shrine(self, ctx: commands.Context) -> None:
        await self._forced(ctx, EventType.SHRINE)

    @commands.command(name="SpawnEvent", brief="Spawn any event by name.")
    @guards.owner_or_admin()
    async def spawn_event(self, ctx: commands.Context, name: str) -> None:
        try:
            event_type = EventType[name.upper()]
        except KeyError:
            names = ", ".join(e.name for e in EVENT_ORDER)
            await io.send(ctx.channel, f"Nieznany event. Dostępne: {names}")
            return
        if event_type not in EVENT_ORDER:
            await io.send(ctx.channel, "Tego eventu nie da się wywołać ręcznie.")
            return
        await self._forced(ctx, event_type)

    async def _forced(self, ctx: commands.Context, event_type: EventType) -> None:
        if await self.run_one_event(forced=event_type) is None:
            await io.send(ctx.channel, "Nie mogę teraz uruchomić eventu (boss albo inna aktywność w toku, "
                                       "albo event zakończył się błędem - sprawdź logi).")


async def setup(bot) -> None:
    await bot.add_cog(EventsCog(bot))
