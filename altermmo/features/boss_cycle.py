"""The big boss life-cycle: schedule -> silence -> wind -> boss -> fight -> repeat.

Replaces the legacy ``task`` loop of ``cogs/spawnboss.py`` and the ``$zaatakuj``
plumbing around the fights.  The legacy loop

* was started only when the database said a respawn was in progress, so after
  an unlucky restart no boss ever came again;
* died silently on the first failed Discord/database call, leaving the
  ``BOSSALIVE`` global at 1, 2 or 3 for good;
* depended on fights returning the next ``BOSSALIVE`` value - a fight that
  raised left it at 5 or 6 for ever.

Here the cycle is a supervised loop over explicit phases
(:class:`altermmo.core.state.BossPhase`).  Each phase handler is safe to enter
again after a crash, the arrival time is persisted in the ``BOSS`` table, and a
fight always ends in a defined state, whatever happens inside it.

Timeline of one cycle (production timings)::

    IDLE       draw respawn delay (30 min - 12 h) => rarity; persist arrival time
    SCHEDULED  1 h of peace after the previous fight, then clear the channel
               and post "Dookoła rozlega się cisza..."
    WAITING    sleep until the arrival time
    PRE_SPAWN  apply shrine modifiers to the rarity, warn pet owners, post
               "Wiatr wzmaga się...", wait 4-7 minutes, show the boss
    VISIBLE    wait for $zaatakuj (or a click on "Zaatakuj")
    ENGAGED    choosing the fighter / gathering a party
    FIGHTING   the fight

The "Zaatakuj" button
---------------------
The boss picture (:func:`altermmo.features.boss.send_boss_image`) carries a
permanent button (:class:`AttackView`, fixed ``custom_id``, registered by the
boss cog): a click is the same as typing ``$zaatakuj`` - the same guards, but
the refusals are private.  The cycle remembers the messages that carry the
button and takes it off as soon as the boss is engaged or leaves; when a fight
is interrupted and the boss stays, the notice about it carries the button
again.  While a lobby runs, a click on an old button is handed to the lobby
(:attr:`BossCycle.lobby_door`), like a typed ``$zaatakuj`` is.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from ..core import clock, io, ui
from ..core.scene import Scene
from ..core.state import BossInfo, BossPhase, EventType
from .fight_types import FightInterrupted, FightResult

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot

log = logging.getLogger(__name__)

LOOP_NAME = "boss-cycle"

MSG_SILENCE = "Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu..."
MSG_WIND = ("Wiatr wzmaga się coraz mocniej, z oddali słychać ryk, "
            "a ziemią targają coraz mocniejsze wstrząsy... <:MonkaS:882181709100097587>")
MSG_NOT_ATTACKABLE = "Nie możesz zaatakować bossa, poczekaj na pojawienie się kolejnego <@{user_id}>!"
MSG_FIGHT_INTERRUPTED_STAYS = ("Wystąpił problem techniczny i walka została przerwana "
                               "<:Sadge:936907659142111273> Boss wciąż tu jest - kliknij **Zaatakuj** "
                               "albo wpisz **$zaatakuj**, żeby spróbować ponownie!")
MSG_BOSS_TAKEN = "Ktoś już walczy z tym bossem - poczekaj na kolejnego! <:Bedge:970576892874854400>"

#: The permanent button under the boss picture.
ATTACK_BUTTON_ID = ui.PREFIX + "boss:attack"
ATTACK_LABEL = "Zaatakuj"
ATTACK_EMOJI = "⚔️"
MSG_FIGHT_INTERRUPTED_GONE = ("Wystąpił problem techniczny i walka została przerwana "
                              "<:Sadge:936907659142111273> Boss pojawi się później!")
MSG_BOSS_LEFT = ("Nikt nie odważył się stanąć do walki... Potwór odchodzi znudzony. "
                 "Boss pojawi się później! <:RIP:912797982917816341>")
MSG_SUMMONED_LEFT = ("Nikt nie odważył się stanąć do walki... Przywołany potwór rozpływa się w powietrzu, "
                     "bo z oddali nadciąga już coś innego <:MonkaS:882181709100097587>")

#: Legendary bosses need a party; outside these hours (and outside weekends)
#: a legendary boss becomes a mythic one that a single player can fight.
LEGENDARY_HOURS = range(16, 23)

_KEY_ENABLED = "boss_spawning_enabled"
_KEY_QUIET_UNTIL = "boss_quiet_until"
#: Rarity of a summoned (ritual / skill gem) boss that is on the channel; kept
#: so that a restart of the bot does not make that boss vanish.
_KEY_SUMMONED = "summoned_boss_rarity"
#: Events that only wait for a command; everything else is an activity in progress.
_PASSIVE_EVENTS = (EventType.SHRINE, EventType.CHEST)


def spawn_notice(boss_channel_id: int) -> str:
    return (f"Na kanale <#{boss_channel_id}> pojawił się właśnie potwór! "
            "Zabijcie go, żeby zgarnąć nagrody!")


def final_rarity(base_rarity: int, rarity_boost: int, now: datetime, *, debug: bool) -> int:
    """Rarity of the boss that actually appears (legacy "episode 2" rules)."""
    rarity = min(int(base_rarity) + int(rarity_boost), 4)
    off_peak = now.hour not in LEGENDARY_HOURS and not clock.is_weekend(now)
    if rarity == 3 and off_peak and not debug:
        rarity = 4
    return rarity


# -- the "Zaatakuj" button -------------------------------------------------------------

class AttackView(ui.GameView):
    """The permanent, stateless "Zaatakuj" button (works on pictures from before a restart)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label=ATTACK_LABEL, emoji=ATTACK_EMOJI, style=discord.ButtonStyle.primary,
                       custom_id=ATTACK_BUTTON_ID)
    async def attack_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await handle_attack_click(interaction)


async def with_attack_button(send: Callable[[AttackView], Awaitable[Any]]) -> Any:
    """Post a message with the "Zaatakuj" button: ``await with_attack_button(lambda view: ...send(view=view))``.

    The view of the single message is stopped right away (discord.py would
    otherwise keep one view per boss for ever); clicks are answered by the
    view the boss cog registers at start-up (same ``custom_id``).
    """
    view = AttackView()
    try:
        return await send(view)
    finally:
        view.stop()


async def handle_attack_click(interaction: Any) -> None:
    """A click on "Zaatakuj": exactly ``$zaatakuj``, with private refusals.

    The click is acknowledged as soon as the boss is claimed; the encounter
    (lobby and fight) then runs inside this handler.
    """
    bot = interaction.client
    # Same guards as the command: the boss channel only (and no dead hunters -
    # they cannot type the command either).  ``$zaatakuj`` is not blocked by
    # the busy flag, so the button is not either.
    if not await ui.gate(interaction, unless_busy=False):
        return
    cycle: BossCycle = bot.boss_cycle
    door = cycle.lobby_door
    if bot.state.boss_phase is BossPhase.ENGAGED and door is not None:
        await door(interaction)  # an old button clicked while a lobby runs: like a typed $zaatakuj
        return
    await cycle.attack(ui.scene_of(interaction), interaction=interaction)


class BossCycle:
    """Owns the boss phase transitions.  One instance lives on ``bot.boss_cycle``."""

    def __init__(self, bot: "AlterBot") -> None:
        self.bot = bot
        #: Rarity drawn together with the respawn delay (before modifiers).
        self.base_rarity = 0
        #: When the scheduled boss arrives ("wind" message), aware UTC.
        self.arrives_at: datetime | None = None
        #: End of the quiet hour after the previous fight, aware UTC.
        self.quiet_until: datetime | None = None
        #: Phase to go back to after a summoned (skill/ritual) boss is done.
        self._resume_phase: BossPhase | None = None
        #: When the summoned boss appeared (aware UTC); None without one.
        self._summoned_at: datetime | None = None
        self._restored = False
        self._wind_sent = False
        self._appears_at: datetime | None = None
        self._spawn_notice_message: Any = None
        #: Messages that carry the "Zaatakuj" button right now.
        self._attack_buttons: list[Any] = []
        #: Where the button was taken off when the boss was engaged (restored when needed).
        self._engaged_buttons: list[Any] = []
        #: Set by a running lobby: ``async (interaction) -> None`` takes a click on an old
        #: "Zaatakuj" button as a typed ``$zaatakuj`` (and answers it).
        self.lobby_door: Callable[[Any], Awaitable[None]] | None = None

    # -- the "Zaatakuj" button ------------------------------------------------------

    def note_attack_button(self, message: Any) -> None:
        """Remember a message that carries the "Zaatakuj" button (taken off when the boss is engaged)."""
        if message is not None and message not in self._attack_buttons:
            self._attack_buttons.append(message)

    async def retire_attack_buttons(self) -> list[Any]:
        """Take the "Zaatakuj" button off every message that carries it.  Never raises.

        Returns those messages.  A purged message is simply skipped.
        """
        messages, self._attack_buttons = self._attack_buttons, []
        for message in messages:
            await ui.retire(message)
        return messages

    async def _restore_attack_buttons(self, messages: list[Any]) -> None:
        """Put the button back on ``messages`` (when no new message could carry it).  Never raises."""
        for message in messages:
            try:
                await with_attack_button(lambda view, message=message: message.edit(view=view))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - usually: the message was purged meanwhile
                continue
            self.note_attack_button(message)

    # -- features (imported lazily: they import this module's types) -------------

    @property
    def _boss(self):
        from . import boss
        return boss

    @property
    def _database(self):
        from . import database
        return database

    # -- switches ---------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.bot.store.runtime_get(_KEY_ENABLED, True))

    def start(self) -> None:
        """(Re)start the supervised loop (idempotent)."""
        self.bot.supervisor.start(LOOP_NAME, self.run)

    async def enable(self) -> None:
        """``$startSpawnBoss``: switch spawning on and restart the loop."""
        self.bot.store.runtime_set(_KEY_ENABLED, True)
        self._restored = False
        self.start()

    async def disable(self) -> None:
        """``$stopSpawnBoss``: no more bosses until enabled again (survives restarts)."""
        self.bot.store.runtime_set(_KEY_ENABLED, False)
        self.bot.supervisor.stop(LOOP_NAME)
        self._forget_schedule()
        await self.bot.state.set_boss_phase(BossPhase.IDLE)
        await self.retire_attack_buttons()
        await self._database.update_boss_table(self.bot, 0, 0, False)

    async def reset(self) -> None:
        """``$ResetBoss``: drop whatever is going on and start a fresh cycle."""
        bot = self.bot
        bot.supervisor.stop(LOOP_NAME)
        self._forget_schedule()
        bot.state.force_release_busy()
        bot.state.close_event()
        bot.state.boss = BossInfo()
        await bot.state.set_boss_phase(BossPhase.IDLE)
        await self.retire_attack_buttons()
        await self._guarded("reset boss table", self._database.update_boss_table(bot, 0, 0, False))
        await self._guarded("post-fight reset", self._boss.reset_end(bot))
        self._restored = True  # the schedule was wiped on purpose - do not resume it
        if self.enabled:
            self.start()

    def _forget_schedule(self) -> None:
        self.arrives_at = None
        self.quiet_until = None
        self._resume_phase = None
        self._summoned_at = None
        self._wind_sent = False
        self._appears_at = None
        self.bot.store.runtime_set(_KEY_QUIET_UNTIL, None)
        self._remember_summoned(None)

    def _remember_summoned(self, rarity: int | None) -> None:
        """Note (or forget) the summoned boss standing on the channel.  Never raises."""
        try:
            if self.bot.store.runtime_get(_KEY_SUMMONED) != rarity:
                self.bot.store.runtime_set(_KEY_SUMMONED, rarity)
        except OSError:
            log.exception("Could not persist the summoned boss (%s).", rarity)

    def seconds_to_arrival(self) -> float | None:
        return clock.seconds_until(self.arrives_at) if self.arrives_at is not None else None

    # -- the loop ---------------------------------------------------------------

    async def run(self) -> None:
        """The supervised loop.  Any exception restarts it; phases are re-entrant."""
        bot = self.bot
        await bot.wait_until_ready()
        if not self.enabled:
            log.info("Boss spawning is switched off ($startSpawnBoss enables it).")
            await asyncio.Event().wait()
        if not self._restored:
            await self._restore()
            self._restored = True

        handlers = {
            BossPhase.IDLE: self._phase_idle,
            BossPhase.SCHEDULED: self._phase_scheduled,
            BossPhase.WAITING: self._phase_waiting,
            BossPhase.PRE_SPAWN: self._phase_pre_spawn,
            BossPhase.VISIBLE: self._phase_visible,
            BossPhase.ENGAGED: self._phase_fight_in_progress,
            BossPhase.FIGHTING: self._phase_fight_in_progress,
        }
        while True:
            await handlers[bot.state.boss_phase]()

    async def _restore(self) -> None:
        """After a (re)start: continue the persisted schedule or begin a new one."""
        bot = self.bot
        row = await self._database.read_boss_table(bot)
        if bot.state.boss_phase >= BossPhase.VISIBLE:
            # Spawning was switched on while a summoned boss is on the channel (or
            # being fought): that boss stays; only the schedule is picked up.
            if row is not None and row.respawn_started and row.respawn_at is not None:
                self.base_rarity = row.rarity
                self.arrives_at = row.respawn_at
                self._resume_phase = BossPhase.SCHEDULED
                if self._summoned_at is None:
                    self._summoned_at = clock.utcnow()
            log.info("Boss loop (re)started during an encounter (phase %s); it goes on.",
                     bot.state.boss_phase.name)
            return
        if row is not None and row.respawn_started and row.respawn_at is not None:
            self.base_rarity = row.rarity
            self.arrives_at = row.respawn_at
            raw_quiet = bot.store.runtime_get(_KEY_QUIET_UNTIL)
            try:
                self.quiet_until = datetime.fromisoformat(raw_quiet) if raw_quiet else None
            except (TypeError, ValueError):
                self.quiet_until = None
            bot.state.boss_rarity = row.rarity
            log.info("Resuming boss schedule: rarity %s, arrives at %s (in %.0f s).",
                     row.rarity, row.respawn_at.isoformat(), clock.seconds_until(row.respawn_at))
            await bot.state.set_boss_phase(BossPhase.SCHEDULED)
        else:
            log.info("No boss respawn in progress - starting a new cycle.")
            await bot.state.set_boss_phase(BossPhase.IDLE)

        # A ritual / skill gem boss that was standing there when the bot stopped comes back.
        summoned = bot.store.runtime_get(_KEY_SUMMONED)
        if summoned is not None:
            back = False
            try:
                back = await self.summon(bot.boss_scene(), int(summoned))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the regular schedule matters more
                log.exception("Could not bring the summoned boss back after a restart.")
            if back:
                log.info("Summoned boss of rarity %s is back on the channel after a restart.", summoned)
            else:
                self._remember_summoned(None)

    async def _phase_idle(self) -> None:
        """Legacy episode 0: draw the respawn delay, derive the rarity, persist."""
        bot = self.bot
        timings = bot.timings
        delay = random.randint(timings.respawn_min, timings.respawn_max) * timings.respawn_multiplier
        rarity = self._boss.rarity_for_respawn_time(delay, debug=bot.debug)
        now = clock.utcnow()
        quiet_until = now + timedelta(seconds=timings.post_fight_pause)
        arrives_at = quiet_until + timedelta(seconds=delay)

        await self._database.update_boss_table(bot, rarity, timings.post_fight_pause + delay, True)
        bot.store.runtime_set(_KEY_QUIET_UNTIL, quiet_until.isoformat())
        self.base_rarity = rarity
        self.quiet_until = quiet_until
        self.arrives_at = arrives_at
        self._wind_sent = False
        self._appears_at = None
        log.info("Boss scheduled: delay %s s, rarity %s, arrives at %s.", delay, rarity, arrives_at.isoformat())
        if bot.state.boss_phase is not BossPhase.IDLE:
            # A boss was summoned (ritual, skill gem) while the schedule was being
            # saved: it keeps the channel and the schedule goes on after it.
            self._resume_phase = BossPhase.SCHEDULED
            return
        bot.state.boss = BossInfo(rarity=rarity)
        await bot.state.set_boss_phase(BossPhase.SCHEDULED)

    async def _phase_scheduled(self) -> None:
        """Quiet hour after the last fight, then clear the channel and announce silence."""
        bot = self.bot
        if self.quiet_until is not None:
            remaining = clock.seconds_until(self.quiet_until)
            if remaining > 0 and not await bot.state.sleep_while_boss_phase(BossPhase.SCHEDULED, remaining):
                return
        await bot.state.wait_until_idle()
        if bot.state.boss_phase is not BossPhase.SCHEDULED:
            return
        channel = bot.boss_channel
        await io.clear_channel(channel)
        await io.send_patient(channel, MSG_SILENCE)
        if bot.state.boss_phase is BossPhase.SCHEDULED:
            await bot.state.set_boss_phase(BossPhase.WAITING)

    async def _phase_waiting(self) -> None:
        """Sleep until the arrival time (in slices, so clock changes cannot strand us)."""
        bot = self.bot
        while True:
            remaining = clock.seconds_until(self.arrives_at) if self.arrives_at is not None else 0.0
            if remaining <= 0:
                break
            if not await bot.state.sleep_while_boss_phase(BossPhase.WAITING, min(remaining, 300.0)):
                return
        if bot.state.boss_phase is BossPhase.WAITING:
            await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)

    async def _phase_pre_spawn(self) -> None:
        """Legacy episodes 2 and 3: "wind" warning, a few minutes of suspense, the boss."""
        bot = self.bot
        state = bot.state
        timings = bot.timings

        if not self._wind_sent:
            await state.wait_until_idle()
            if state.boss_phase is not BossPhase.PRE_SPAWN:
                return
            rarity = self.base_rarity
            try:
                from . import modifiers
                loaded = await modifiers.load_modifiers(bot)
                rarity = final_rarity(self.base_rarity, loaded.get("rarity_boost", 0), clock.now(),
                                      debug=bot.debug)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let a broken modifiers file stop the boss
                log.exception("Could not apply modifiers to the boss rarity; using the base rarity.")
            state.boss = BossInfo(rarity=rarity)
            await self._guarded("pet boss detection", self._detect(rarity))
            await io.send_patient(bot.boss_channel, MSG_WIND)
            self._appears_at = clock.utcnow() + timedelta(
                seconds=random.uniform(timings.pre_spawn_min, timings.pre_spawn_max))
            self._wind_sent = True

        remaining = clock.seconds_until(self._appears_at) if self._appears_at is not None else 0.0
        if remaining > 0 and not await state.sleep_while_boss_phase(BossPhase.PRE_SPAWN, remaining):
            return

        if not await state.wait_until_idle() and state.busy:
            # An activity still holds the channel after half an hour: the loop comes
            # back here and waits on.  (An unclaimed shrine/chest does not stop the
            # boss: as in the old bot it is closed below once the wait is over.)
            return
        if state.boss_phase is not BossPhase.PRE_SPAWN:
            return
        with state.hold_busy("boss-spawn"):
            state.close_event()
            channel = bot.boss_channel
            await io.clear_channel(channel)
            await self._announce_spawn()
            scene = bot.boss_scene()
            is_player_boss, player_boss = await self._boss.send_boss_image(scene, state.boss.rarity)
            state.boss = BossInfo(rarity=state.boss.rarity, is_player_boss=is_player_boss,
                                  player_boss=player_boss)
        self._wind_sent = False
        self._appears_at = None
        self._resume_phase = None
        await state.set_boss_phase(BossPhase.VISIBLE)

    async def _detect(self, rarity: int) -> None:
        from . import pets
        await pets.detect_boss(self.bot, rarity)

    async def _announce_spawn(self) -> None:
        """General-chat notice; the previous one is removed.  Purely best effort."""
        bot = self.bot
        await io.safe_delete(self._spawn_notice_message)
        self._spawn_notice_message = None
        try:
            announce = bot.text_channel(bot.ids.announce_channel)
            self._spawn_notice_message = await io.send(announce, spawn_notice(bot.ids.boss_channel))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Could not post the boss spawn notice.")

    def summoned_leaves_at(self) -> datetime | None:
        """When an unattacked summoned boss makes room for the scheduled one (None: never).

        Owner's rule: a ritual / skill gem boss waits only until the scheduled
        boss is due - but at least ``timings.summoned_min_stay`` after it
        appeared, so nobody summons a boss that is gone within seconds.
        """
        if self._resume_phase is None or self.arrives_at is None or not self.enabled:
            return None
        if self._summoned_at is None:
            self._summoned_at = clock.utcnow()
        stays_until = self._summoned_at + timedelta(seconds=self.bot.timings.summoned_min_stay)
        return max(self.arrives_at, stays_until)

    async def _summoned_boss_leaves(self) -> None:
        """The scheduled boss is due: the summoned boss nobody attacked is gone.

        Called with the boss already claimed (ENGAGED), so nobody can attack it
        any more.  Nothing is reset: the shrine modifiers, the dead hunters and
        the schedule belong to the boss that arrives now - without the quiet
        hour that follows a fight.
        """
        bot = self.bot
        log.info("Nobody attacked the summoned boss (rarity %s); it leaves, the scheduled boss is due.",
                 bot.state.boss.rarity)
        try:
            await self.retire_attack_buttons()  # in case clearing the channel fails
            await io.clear_channel(bot.boss_channel)
            await io.send_patient(bot.boss_channel, MSG_SUMMONED_LEFT)
        finally:
            bot.state.boss = BossInfo(rarity=self.base_rarity)
            self._remember_summoned(None)
            self._resume_phase = None
            self._summoned_at = None
            await bot.state.set_boss_phase(BossPhase.WAITING)

    async def _phase_visible(self) -> None:
        """The boss waits for an attacker.

        The scheduled boss waits for ever (optionally: ``boss_visible_timeout``);
        a summoned one only until the scheduled boss is due.
        """
        bot = self.bot
        timeout = bot.settings.boss_visible_timeout
        leaves_at = self.summoned_leaves_at()
        if leaves_at is not None:
            remaining = clock.seconds_until(leaves_at)
            if timeout is None or remaining <= timeout:
                if remaining > 0:
                    # In slices, like the wait for the scheduled boss: a changed clock cannot strand us.
                    await bot.state.sleep_while_boss_phase(BossPhase.VISIBLE, min(remaining, 300.0))
                elif bot.state.try_engage_boss():
                    await self._summoned_boss_leaves()
                return
        if timeout is None:
            await bot.state.wait_boss_phase(*(p for p in BossPhase if p is not BossPhase.VISIBLE))
            return
        if await bot.state.sleep_while_boss_phase(BossPhase.VISIBLE, timeout):
            if bot.state.boss_phase is BossPhase.VISIBLE and bot.state.try_engage_boss():
                log.info("Nobody attacked the boss for %.0f s - it leaves.", timeout)
                await io.send_patient(bot.boss_channel, MSG_BOSS_LEFT)
                await self.finish(FightResult(won=False))

    async def _phase_fight_in_progress(self) -> None:
        """A fight is running in the command's task; just wait for it to end.

        The fight itself is bounded by ``boss_fight_time_limit``.  If the loop
        finds an ENGAGED/FIGHTING phase without a running fight (the process of
        elimination: a restart of this loop cannot cause it, a bug could), it
        releases the boss after the same limit instead of waiting for ever.
        """
        bot = self.bot
        limit = bot.settings.boss_fight_time_limit + 300.0
        waiting_for = (BossPhase.IDLE, BossPhase.SCHEDULED, BossPhase.WAITING,
                       BossPhase.PRE_SPAWN, BossPhase.VISIBLE)
        if not await bot.state.wait_boss_phase(*waiting_for, timeout=limit):
            log.error("Boss stuck in %s for %.0f s - forcing the end of the encounter.",
                      bot.state.boss_phase.name, limit)
            await bot.log_to_owner("Boss utknął w walce - wymuszam zakończenie i nowy respawn.", ping=True)
            await self.finish(FightResult(won=False))

    # -- summoned bosses (skill gem, ritual) --------------------------------------

    def can_summon(self) -> bool:
        """A boss may be summoned only while no boss is on (or coming to) the channel."""
        return self.bot.state.boss_phase <= BossPhase.WAITING

    def activity_in_the_way(self) -> bool:
        """True while a hunt / tower run / interactive event (also its open sign-up) runs.

        A boss that brings its own picture (skill gem) must not barge in: it
        would wipe the sign-up post of a duel or ritual, or appear twice.
        """
        state = self.bot.state
        return state.busy or (state.event_alive and state.event_type not in _PASSIVE_EVENTS)

    async def summon(self, scene: Scene, rarity: int, *, show_image: bool = True,
                     is_player_boss: bool = False, player_boss: Any = None) -> bool:
        """Put an extra boss on the channel right now (skill gem / ritual).

        The regular schedule is only paused: when the summoned boss is dealt
        with, the scheduled one still arrives at its original time - and when
        nobody attacks the summoned boss until then, it leaves (see
        :meth:`summoned_leaves_at`).  Returns False (and does nothing) when a
        boss is already there or on its way.

        ``show_image=True`` clears the channel and posts the boss picture (skill
        gem); with ``show_image=False`` the caller already presented the boss
        (ritual) and passes ``is_player_boss`` / ``player_boss`` itself.
        """
        bot = self.bot
        state = bot.state
        if not self.can_summon():
            return False
        if show_image:
            if self.activity_in_the_way():
                return False
            # Posting the picture takes a moment: the channel is kept meanwhile, so
            # nothing else (a ritual that has just ended...) can show a second boss.
            with state.hold_busy("boss-summon"):
                state.close_event()
                await io.clear_channel(scene.channel)
                is_player_boss, player_boss = await self._boss.send_boss_image(scene, rarity)
            if not self.can_summon():
                return False  # the scheduled boss started arriving while we were posting
        else:
            state.close_event()
        # Read only now: the phase may have moved on (IDLE -> SCHEDULED) during the upload.
        self._resume_phase = state.boss_phase
        self._summoned_at = clock.utcnow()
        state.boss = BossInfo(rarity=int(rarity), is_player_boss=is_player_boss, player_boss=player_boss)
        self._remember_summoned(int(rarity))
        await state.set_boss_phase(BossPhase.VISIBLE)
        return True

    # -- fights -----------------------------------------------------------------

    async def attack(self, scene: Scene, *, interaction: Any = None) -> None:
        """``$zaatakuj``: claim the visible boss and run the whole encounter.

        ``interaction``: set when the attack is a click on "Zaatakuj" - the
        refusals are then private answers to the click, and the click is
        acknowledged as soon as the boss is claimed.
        """
        bot = self.bot
        state = bot.state
        if state.boss_phase is BossPhase.ENGAGED:
            # Other players' $zaatakuj during the lobby is handled by the lobby itself.
            if interaction is not None:
                await ui.reply(interaction, MSG_BOSS_TAKEN)
            return
        if not state.try_engage_boss():
            refusal = MSG_NOT_ATTACKABLE.format(user_id=scene.author.id)
            if interaction is not None:
                await ui.reply(interaction, refusal)
            else:
                await io.send(scene.channel, refusal)
            return
        if interaction is not None:
            await ui.ack(interaction)
        # Wake the cycle loop: it then watches the encounter with its time limit
        # (try_engage_boss is synchronous and cannot notify the waiters itself).
        await state.set_boss_phase(BossPhase.ENGAGED)
        self._engaged_buttons = await self.retire_attack_buttons()

        info = state.boss
        try:
            result = await asyncio.wait_for(self._encounter(scene, info),
                                            timeout=bot.settings.boss_fight_time_limit)
        except asyncio.CancelledError:
            await self._interrupted(scene, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - whatever happened, leave a defined state
            if isinstance(exc, (FightInterrupted, asyncio.TimeoutError)):
                log.warning("Boss fight interrupted: %s: %s", type(exc).__name__, exc)
            else:
                log.exception("Boss fight crashed.")
            await self._interrupted(scene, f"{type(exc).__name__}: {exc}")
            return
        await self.finish(result)

    async def _encounter(self, scene: Scene, info: BossInfo) -> FightResult:
        boss = self._boss
        state = self.bot.state
        if info.rarity == 3:
            players = await boss.group_init(scene, info.rarity)
            if not players:
                return FightResult(won=False)
            await state.set_boss_phase(BossPhase.FIGHTING)
            return await boss.group_fight(scene, players, info.is_player_boss, info.player_boss)
        hunter = await boss.single_init(scene, info.rarity)
        if hunter is None:
            return FightResult(won=False)
        await state.set_boss_phase(BossPhase.FIGHTING)
        return await boss.single_fight(scene, hunter, info.rarity, info.is_player_boss, info.player_boss)

    async def _interrupted(self, scene: Scene, reason: str) -> None:
        """A fight ended because of a technical problem, not because of the players."""
        bot = self.bot
        stays = bot.settings.boss_returns_after_interrupted_fight
        if stays:
            await bot.state.set_boss_phase(BossPhase.VISIBLE)
            message = MSG_FIGHT_INTERRUPTED_STAYS
        else:
            await self.finish(FightResult(won=False))
            message = MSG_FIGHT_INTERRUPTED_GONE
        # Notify in the background: Discord may be unreachable right now and the
        # state must not wait for it.
        asyncio.ensure_future(self._notify_interrupted(scene, message, reason, attack_button=stays))

    async def _notify_interrupted(self, scene: Scene, message: str, reason: str, *,
                                  attack_button: bool = False) -> None:
        if attack_button:
            # The boss stays: the notice carries the "Zaatakuj" button.  When it
            # cannot be posted, the button goes back under the boss picture.
            engaged, self._engaged_buttons = self._engaged_buttons, []
            posted = await with_attack_button(
                lambda view: io.send_patient(scene.channel, message, view=view))
            if posted is not None:
                self.note_attack_button(posted)
            else:
                await self._restore_attack_buttons(engaged)
        else:
            await io.send_patient(scene.channel, message)
        await self.bot.log_to_owner(f"Walka z bossem przerwana z powodu błędu: `{reason}`", ping=True)

    async def finish(self, result: FightResult) -> None:
        """End of an encounter: post-fight reset, dead-hunter roles, next phase.

        Bookkeeping is best effort - a failing step is logged and reported, but
        the boss phase always moves on, so the next boss always comes.
        """
        bot = self.bot
        self._engaged_buttons = []
        try:
            await self.retire_attack_buttons()  # e.g. a boss that left unattacked
            await self._guarded("post-fight reset", self._boss.reset_end(bot))
            for loser in result.losers_to_ban:
                await self._guarded(f"dead hunter role for {loser}", self._boss.set_dead_hunters(bot, loser))
        finally:
            bot.state.boss = BossInfo()
            self._remember_summoned(None)
            self._summoned_at = None
            resume, self._resume_phase = self._resume_phase, None
            if (resume in (BossPhase.SCHEDULED, BossPhase.WAITING) and self.arrives_at is not None
                    and self.enabled):
                # A summoned boss: the regular schedule goes on as planned, after
                # the usual quiet hour that keeps the fight result on the channel.
                quiet_until = clock.utcnow() + timedelta(seconds=bot.timings.post_fight_pause)
                self.quiet_until = quiet_until
                self.arrives_at = max(self.arrives_at, quiet_until)
                try:
                    bot.store.runtime_set(_KEY_QUIET_UNTIL, quiet_until.isoformat())
                except OSError:  # the phase below must change whatever the disk says
                    log.exception("Could not persist the quiet period after a summoned boss.")
                bot.state.boss = BossInfo(rarity=self.base_rarity)
                await self._guarded("restore boss schedule", self._database.update_boss_table(
                    bot, self.base_rarity, max(clock.seconds_until(self.arrives_at), 0), True))
                await bot.state.set_boss_phase(BossPhase.SCHEDULED)
            else:
                await bot.state.set_boss_phase(BossPhase.IDLE)

    async def _guarded(self, what: str, awaitable) -> None:
        """Await a bookkeeping step; failures are logged and reported, never raised."""
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Boss cycle step failed: %s", what)
            await self.bot.log_to_owner(f"Błąd w cyklu bossa ({what}): `{type(exc).__name__}: {exc}`")
