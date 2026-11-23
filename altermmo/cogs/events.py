"""Random events on the boss channel and the weekly PvP tournament.

Port of ``spawn_event`` / ``pvp_tournament_scheduler`` from the legacy
``cogs/spawnboss.py``.  The legacy loop had no error handling at all: the first
failed Discord or database call inside any event killed it for good, usually
with ``BUSY``/``EVENT_ALIVE`` left set ("events stopped working").  Here the
loop is supervised, every event runs inside ``state.running_event`` (flags are
always released) and a failing event is logged and skipped.

The passive events (chest, shrine) wait for a player with no coroutine
running: their panel carries a *permanent* button ("Otwórz skrzynię" /
"Pomódl się", fixed ``custom_id``, registered in ``cog_load``) that does
exactly what ``$skrzynia`` / ``$modlitwa`` do - same guards, same atomic
claim, the refusals answered with ``ui.reply``.  The cog remembers the panel and takes
its button off once the event is claimed (by a click or by the command) or
closed by the next event / the tournament; a click on a panel the cog no longer
knows (e.g. from before a restart) is refused and its button removed.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Any, Awaitable, Callable

import discord
from discord.ext import commands

from ..core import clock, guards, io, scheduler, ui
from ..core.scene import Scene
from ..core.state import EventType

log = logging.getLogger(__name__)

LOOP_NAME = "event-loop"
TOURNAMENT_JOB = "pvp-tournament"

#: Order matters: the weight tuples below are positional (legacy order; the companion
#: training of 2026-11-22 stands next to the duel, then the wandering merchant of the same
#: day; the ritual stays last).
EVENT_ORDER = (EventType.SHRINE, EventType.CHEST, EventType.INVASION, EventType.PARTY,
               EventType.MEMORY, EventType.HUNTING, EventType.HAZARD, EventType.DUEL,
               EventType.TRAINING, EventType.MERCHANT, EventType.RITUAL)
#: Evenings (18-21) and weekends: more multiplayer events.  The ritual (last
#: place) was switched off in the old bot (weight 0); the owner enabled it in v2.
#: The companion training has the weights of the duel; it is left out of the draw
#: until the owner has supplied its picture (``EventsCog.not_drawable``).  The wandering
#: merchant has weight 1 at every hour (like the ritual) but comes at most once a day
#: (``merchant.came_today``); what he sells and how often each ware comes is in
#: ``config/merchantConfig.json``.
PEAK_WEIGHTS = (1, 1, 2, 1, 1, 1, 1, 2, 2, 1, 1)
OFF_PEAK_WEIGHTS = (2, 2, 1, 0, 2, 2, 2, 0, 0, 1, 1)
DEBUG_WEIGHTS = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1)
PEAK_HOURS = range(18, 22)
#: A ritual is not started when the scheduled boss is due sooner than the
#: ritual lasts plus this margin (seconds): its boss could not be summoned.
RITUAL_MARGIN, RITUAL_MARGIN_DEBUG = 180.0, 10.0

#: Passive events: posted, then they wait for ``$modlitwa`` / ``$skrzynia``.
PASSIVE = (EventType.SHRINE, EventType.CHEST)
#: Events that stay open with the channel free and are closed by the next event / the
#: tournament: the chest, the shrine and the wandering merchant (passive since 2026-11-23,
#: his own coroutine notices the close and leaves).
CLOSED_BY_NEXT = (*PASSIVE, EventType.MERCHANT)

MSG_NOT_A_BELIEVER = ("Nie umiesz pacierza. Poczekaj na kogoś bardziej wierzącego. "
                      "<:prayge:1063891597760139304>")
MSG_NO_SHRINE = "Do kogo Ty chcesz się modlić? Przecież tu nic nie ma..."
MSG_RITUAL_BOSS_GONE = ("Przywołany potwór rozpłynął się w powietrzu - nadciąga coś innego... "
                        "<:Hmm:984767035617730620>")
MSG_NO_CHEST = "Powiedz mi... Czy ta skrzynia jest tutaj z nami w pokoju? <:Hmm:984767035617730620>"
# The same for everybody who clicks: one line on the channel is enough (see ui.reply).
ui.GENERAL_NOTICES.update({MSG_NO_CHEST, MSG_NO_SHRINE, MSG_NOT_A_BELIEVER})
MSG_CHEST_OPENED = "🗝️ Skrzynię otwiera {mention}!"
#: The reward of a chest / shrine failed half-way: the event is used up all the same.
MSG_REWARD_FAILED = ("Coś poszło nie tak przy wydawaniu nagrody <:Sadge:936907659142111273> "
                     "Administracja dostała już znać.")
LOG_REWARD_FAILED = ("{what}: błąd przy wydawaniu nagrody graczowi <@{player_id}> (`{error}`). Wydarzenie "
                     "zostało zamknięte (nie da się go użyć drugi raz) - sprawdź w logu, co gracz dostał.")
MSG_SHRINE_USED = "🕯️ W kapliczce modli się {mention}."
#: ``$SpawnEvent training`` before the owner has supplied the picture of the event
#: (the folder is ``features/training.PICTURES_DIR``, named here as the owner knows it).
MSG_TRAINING_NO_PICTURE = ("Trening towarzyszy nie ma jeszcze obrazka, więc nie da się go uruchomić (i nie jest "
                           "losowany). Wrzuć obrazek do folderu bota jako `assets/events/training/0.png` "
                           "(kolejne: `1.png`, `.jpg`, `.gif`...) - restart bota nie jest potrzebny.")

PRAYGE = "<:prayge:1063891597760139304>"
LOOT = "<:Loot:912797849916436570>"
CHEST_BUTTON_ID = ui.PREFIX + "events:chest"
SHRINE_BUTTON_ID = ui.PREFIX + "events:pray"
CHEST_BUTTON = "Otwórz skrzynię"
SHRINE_BUTTON = "Pomódl się"


class ChestView(ui.GameView):
    """The "Otwórz skrzynię" button under a chest (permanent, stateless)."""

    def __init__(self, cog: "EventsCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label=CHEST_BUTTON, emoji=LOOT, style=discord.ButtonStyle.primary,
                       custom_id=CHEST_BUTTON_ID)
    async def open_chest_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.passive_clicked(interaction, EventType.CHEST)


class ShrineView(ui.GameView):
    """The "Pomódl się" button under a shrine (permanent, stateless)."""

    def __init__(self, cog: "EventsCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label=SHRINE_BUTTON, emoji=PRAYGE, style=discord.ButtonStyle.primary,
                       custom_id=SHRINE_BUTTON_ID)
    async def pray_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.passive_clicked(interaction, EventType.SHRINE)


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
        #: The panel of the open chest / shrine (its button is taken off when the event ends).
        self._panel: Any = None
        self._panel_type: EventType | None = None

    def register_views(self) -> None:
        """Make the chest / shrine buttons work, also under panels posted before a restart."""
        ui.register(self.bot, ChestView(self))
        ui.register(self.bot, ShrineView(self))

    async def cog_load(self) -> None:
        self.register_views()
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
        if state.event_alive and state.event_type in CLOSED_BY_NEXT:
            state.close_event()  # an unclaimed shrine/chest expires now, the merchant leaves
        await self.retire_panel()

        left_out = self.not_drawable()
        not_drawn = (*left_out, *self.not_again_today())
        event_type = forced or pick_event(clock.now(), debug=bot.debug, exclude=not_drawn)
        if forced is None and event_type is EventType.RITUAL and not self.ritual_fits():
            # The scheduled boss is too close: play something else in this slot.
            event_type = pick_event(clock.now(), debug=bot.debug, exclude=(EventType.RITUAL, *not_drawn))
            log.info("Ritual does not fit before the scheduled boss; drawn instead: %s.",
                     event_type.name if event_type else "nothing")
        if event_type is None:
            return None
        if event_type in left_out:  # (only when forced; ``_forced`` has told the administrator why)
            log.info("Event %s skipped: it cannot be played now (no picture).", event_type.name)
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

    def not_drawable(self) -> tuple[EventType, ...]:
        """Events that cannot be played right now and are left out of the draw.

        The companion training, until the owner has put its picture into
        ``assets/events/training``.  The folder is looked at every time an
        event is drawn, so the first picture switches the event on without a
        restart.
        """
        from ..features import training

        return () if training.has_picture() else (EventType.TRAINING,)

    def not_again_today(self) -> tuple[EventType, ...]:
        """Events left out of the draw for the rest of the day: the merchant once he has come.

        Owner, 2026-11-23: "Jeden kupiec na dzień jest wystarczający".  Only the
        draw: ``$SpawnEvent merchant`` still calls him.
        """
        from ..features import merchant

        return (EventType.MERCHANT,) if merchant.came_today(self.bot) else ()

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
        from ..features import events, merchant, modifiers, training

        bot = self.bot
        state = bot.state
        scene = bot.boss_scene()

        if event_type is EventType.MERCHANT:
            # Passive like the chest, but he sells on his own: he holds the channel only while
            # his panel is posted, then waits with the channel free (see features/merchant.py).
            await merchant.spawn_merchant(scene)
            return

        if event_type in PASSIVE:
            with state.hold_busy(f"event-spawn:{event_type.name}"):
                await io.clear_channel(scene.channel)
                view: ui.GameView
                if event_type is EventType.SHRINE:
                    view = ShrineView(self)
                    panel = await modifiers.spawn_modifier_shrine(scene, view=view)
                else:
                    view = ChestView(self)
                    panel = await events.spawn_chest(scene, view=view)
                # Clicks are answered by the views registered in cog_load (same custom_id);
                # discord.py would otherwise keep one view per panel for ever.
                view.stop()
            self._panel, self._panel_type = panel, event_type
            state.open_event(event_type)
            return

        runners = {
            EventType.MEMORY: events.spawn_memory,
            EventType.INVASION: events.spawn_invasion,
            EventType.PARTY: events.spawn_party,
            EventType.HUNTING: events.spawn_hunting,
            EventType.HAZARD: events.spawn_hazard,
            EventType.DUEL: events.spawn_duel,
            EventType.TRAINING: training.spawn_training,
        }
        # The duel and the ritual keep the channel free while they wait for
        # volunteers (as the old bot did: hunts, the tower and pet commands work
        # meanwhile); they take the busy flag themselves for the fight / for
        # putting the summoned boss on the channel.  (The companion training holds
        # the channel from its first second; during its sign-up only the stable
        # works - see ``GameState.stable_open``.)
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
            if state.event_alive and state.event_type in CLOSED_BY_NEXT:
                state.close_event()
                await self.retire_panel()
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

    # -- player commands; chest and shrine: the command and the button share one path

    async def _claim(self, event_type: EventType, scene: Scene, refuse: Callable[[str], Awaitable[Any]],
                     started: Callable[[], Awaitable[Any]], busy: Callable[[], Awaitable[Any]]) -> bool:
        """``scene.author`` opens the chest / prays at the shrine; True when it happened.

        ``refuse(text)`` answers a refusal, ``started()`` runs once the event is
        claimed (the command reacts to the message, a click is acknowledged).

        Owner's rule: not while a hunt / tower run plays on the channel -
        ``busy()`` says so and the chest / shrine stays for later.  (Only when
        there is something to claim: otherwise "there is no chest" is the answer.)
        """
        from ..features import events, modifiers, patrons

        state = self.bot.state
        may_claim = event_type is not EventType.SHRINE or patrons.check_if_patron(scene.author)
        if state.event_alive and state.event_type is event_type and state.activity_running and may_claim:
            # (Somebody who may never pray gets that answer below, not "try again in a moment".)
            await busy()
            return False
        if not state.try_claim_event(event_type):
            await refuse(MSG_NO_SHRINE if event_type is EventType.SHRINE else MSG_NO_CHEST)
            return False
        if event_type is EventType.SHRINE and not patrons.check_if_patron(scene.author):
            state.reopen_event(EventType.SHRINE)
            await refuse(MSG_NOT_A_BELIEVER)
            return False
        try:
            await started()
        except BaseException:
            state.reopen_event(event_type)  # nothing happened - the chest / shrine stays
            raise
        # From here on the chest / shrine is used up, whatever happens.  Owner's rule: it works
        # exactly once.  (It used to be put back when the reward raised - also after the loot
        # had been handed out, so on 2026-11-20 one chest was opened seven times.)
        failure: Exception | None = None
        try:
            if event_type is EventType.SHRINE:
                await modifiers.random_modifiers(scene, True)
            else:
                await events.open_chest(scene)
        except Exception as exc:  # noqa: BLE001 - reported below; the event is over all the same
            failure = exc
            log.exception("%s: the reward of %s failed.", event_type.name, scene.author.id)
        finally:
            state.close_event()  # (also when the task is cancelled)
        line = MSG_SHRINE_USED if event_type is EventType.SHRINE else MSG_CHEST_OPENED
        await self.retire_panel(line.format(mention=scene.author.mention))
        if failure is not None:
            await self.bot.log_to_owner(LOG_REWARD_FAILED.format(
                what="Kapliczka" if event_type is EventType.SHRINE else "Skrzynia",
                player_id=scene.author.id, error=type(failure).__name__), ping=True)
            try:
                await io.send(scene.channel, MSG_REWARD_FAILED)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("Could not tell the channel about the failed reward.")
        return True

    async def passive_clicked(self, interaction: Any, event_type: EventType) -> None:
        """A click on "Otwórz skrzynię" / "Pomódl się"."""
        if not await ui.gate(interaction, unless_busy=False):  # "busy" is answered by _claim, as for the commands
            return
        message = interaction.message
        if message is None or self._panel is None or message.id != self._panel.id:
            # A panel the cog no longer knows (a restart, an old message): nothing to claim there.
            await ui.reply(interaction, MSG_NO_SHRINE if event_type is EventType.SHRINE else MSG_NO_CHEST)
            await ui.retire(message)
            return
        log.info("%s button clicked by %s.", event_type.name, interaction.user.id)

        async def refuse(text: str) -> None:
            await ui.reply(interaction, text)
            await self.retire_panel()  # e.g. the boss closed the event: the button goes (no-op otherwise)

        await self._claim(event_type, ui.scene_of(interaction), refuse, lambda: ui.ack(interaction),
                          lambda: ui.reply(interaction, ui.MSG_BUSY))

    async def retire_panel(self, line: str | None = None) -> None:
        """Take the button off the chest / shrine panel once that event is over.  Never raises.

        ``line`` (who opened / prayed) is added to the panel.  Nothing happens
        while the event is still open or being claimed.
        """
        if self._panel is None or self.bot.state.event_type is self._panel_type:
            return
        panel, self._panel, self._panel_type = self._panel, None, None
        changes: dict[str, Any] = {}
        embeds = getattr(panel, "embeds", None) or []
        if line is not None and embeds:
            embed = embeds[0].copy()
            embed.description = f"{embed.description or ''}\n\n{line}"
            changes["embed"] = embed
        await ui.retire(panel, **changes)

    @commands.command(name="modlitwa", brief="Supporters can pray at the shrine.")
    @guards.boss_channel_only()
    async def pray(self, ctx: commands.Context) -> None:
        await self._claim(EventType.SHRINE, Scene.from_ctx(ctx), lambda text: io.send(ctx.channel, text),
                          lambda: io.safe_react(ctx.message, PRAYGE), lambda: guards.notify_busy(ctx))

    @commands.command(name="skrzynia", brief="Everyone can open a chest.")
    @guards.boss_channel_only()
    async def open_chest(self, ctx: commands.Context) -> None:
        await self._claim(EventType.CHEST, Scene.from_ctx(ctx), lambda text: io.send(ctx.channel, text),
                          lambda: asyncio.sleep(0), lambda: guards.notify_busy(ctx))

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
        if event_type is EventType.TRAINING and event_type in self.not_drawable():
            await io.send(ctx.channel, MSG_TRAINING_NO_PICTURE)
            return
        if await self.run_one_event(forced=event_type) is None:
            await io.send(ctx.channel, "Nie mogę teraz uruchomić eventu (boss albo inna aktywność w toku, "
                                       "albo event zakończył się błędem - sprawdź logi).")


async def setup(bot) -> None:
    await bot.add_cog(EventsCog(bot))
