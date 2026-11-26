"""The big boss life-cycle: schedule -> silence -> wind -> boss -> fight -> repeat.

Replaces the legacy ``task`` loop of ``cogs/spawnboss.py`` and the ``!zaatakuj``
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
    VISIBLE    wait for !zaatakuj (or a click on "Zaatakuj")
    ENGAGED    choosing the fighter / gathering a party
    FIGHTING   the fight

The "Zaatakuj" button
---------------------
The boss picture (:func:`altermmo.features.boss.send_boss_image`) carries a
permanent button (:class:`AttackView`, fixed ``custom_id``, registered by the
boss cog): a click is the same as typing ``!zaatakuj`` - the same guards, but
the refusals go through ``ui.reply``.  The cycle remembers the messages that carry the
button and takes it off as soon as the boss is engaged or leaves; when a fight
is interrupted and the boss stays, the notice about it carries the button
again.  While a lobby runs, a click on an old button is handed to the lobby
(:attr:`BossCycle.lobby_door`), like a typed ``!zaatakuj`` is.

Owner's rule: the boss cannot be attacked while a hunt, a tower run or an event
plays on the channel (``state.activity_running``) - neither with the command
nor with the button (:meth:`BossCycle.attack`).  Joining a lobby that already
runs is not an attack and is not affected.  (In production hunts and tower
runs do not start once the boss is on its way, so this is a safeguard; a skill
gem or a pet menu that holds the busy flag does not keep the boss away.)

Summoned bosses (:meth:`BossCycle.summon`: the skill gem, the ritual, the Zwój
przywołania) pause the schedule.  A ritual / gem boss nobody attacks leaves
when the scheduled boss is due; a scroll boss (owner, 2026-11-26) leaves after
its half hour whatever the schedule says, its scroll lost - a restart never
lengthens that stay - and gives its scroll back only when it vanished
unclaimed for a technical reason (a restart that could not bring it back,
``!ResetBoss``, ``!stopSpawnBoss``, spawning off at the start, a
``runtime.json`` too old to trust).  Every summoned boss is announced on the
general chat like the scheduled one, in the words of how it came.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from ..core import clock, guards, io, ui
from ..core.scene import Scene
from ..core.state import BossInfo, BossPhase, EventType
from . import bossvariants, items
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
                               "albo wpisz **!zaatakuj**, żeby spróbować ponownie!")
MSG_BOSS_TAKEN = "Ktoś już walczy z tym bossem - poczekaj na kolejnego! <:Bedge:970576892874854400>"
#: A click in the split second between somebody's attack and the message where the
#: others sign up (the party panel of a legendary boss / the minute for a rival).
MSG_PARTY_GATHERS = ("Ktoś zaatakował bossa ułamek sekundy przed Tobą. Za moment pojawi się tu panel "
                     "drużyny - kliknij na nim **Dołącz**, żeby walczyć razem! ⚔️")
MSG_RIVAL_WELCOME = ("Ktoś zaatakował bossa ułamek sekundy przed Tobą. Za moment pojawi się tu wiadomość "
                     "z przyciskiem **Zaatakuj** - kliknij go, jeśli też chcesz zawalczyć o tego bossa! ⚔️")

#: The permanent button under the boss picture.
ATTACK_BUTTON_ID = ui.PREFIX + "boss:attack"
ATTACK_LABEL = "Zaatakuj"
ATTACK_EMOJI = "⚔️"
#: For the owner: a fight broke after it was decided (see ``BossCycle.mark_decided``).
MSG_DECIDED_FIGHT_BROKE = ("Walka z bossem była już rozstrzygnięta, ale przerwał ją błąd przy ogłaszaniu wyniku "
                           "albo wydawaniu nagród (`{error}`). Boss NIE wraca (z jednym bossem walczy się raz) - "
                           "sprawdź w logu, czy nagród nie trzeba przyznać ręcznie.")
MSG_FIGHT_INTERRUPTED_GONE = ("Wystąpił problem techniczny i walka została przerwana "
                              "<:Sadge:936907659142111273> Boss pojawi się później!")
MSG_BOSS_LEFT = ("Nikt nie odważył się stanąć do walki... Potwór odchodzi znudzony. "
                 "Boss pojawi się później! <:RIP:912797982917816341>")
MSG_SUMMONED_LEFT = ("Nikt nie odważył się stanąć do walki... Przywołany potwór rozpływa się w powietrzu, "
                     "bo z oddali nadciąga już coś innego <:MonkaS:882181709100097587>")
#: A boss of a Zwój przywołania that nobody attacked for ``timings.scroll_boss_stay`` (owner, 2026-11-26):
#: it leaves whatever the schedule says, and the scroll is lost.
MSG_SCROLL_BOSS_LEFT = ("Nikt nie odważył się stanąć do walki... Przywołana bestia ziewa, rozpływa się w kłębach "
                        "dymu i wraca tam, skąd przyszła. Zwój przywołania obrócił się w popiół "
                        "<:RIP:912797982917816341>")
#: The scroll of a boss nobody had claimed yet goes back when the boss vanished for a technical reason
#: (a restart that could not bring it back, !ResetBoss, !stopSpawnBoss...): to the player, only while
#: the boss channel is quiet; the owner is always told.
MSG_SCROLL_REFUNDED = ("📯 Przywołana Twoim zwojem bestia zniknęła z przyczyn technicznych, zanim ktokolwiek "
                       "stanął do walki, <@{player_id}> - Zwój przywołania wraca cały i zdrowy do Twojej sakwy.")
LOG_SCROLL_REFUNDED = ("Zwój przywołania gracza <@{player_id}> wrócił do niego: jego mityczny boss zniknął, "
                       "zanim ktoś go zaatakował ({why}).")
LOG_SCROLL_NOT_REFUNDED = ("Zwój przywołania gracza <@{player_id}> NIE wrócił, choć jego boss zniknął, zanim ktoś "
                           "go zaatakował ({why}): `{error}` - oddaj go: `!DajZwojPrzywolania`")
#: Why a scroll went back (the owner's log).
WHY_RESET = "!ResetBoss"
WHY_STOPPED = "!stopSpawnBoss"
WHY_NOT_BACK = "nie wrócił na kanał po restarcie bota"
WHY_SPAWNING_OFF = "respawn bossów był wyłączony przy starcie bota"
WHY_STALE = "bot nie działał ponad 36 godzin"

#: Legendary bosses need a party; outside these hours (and outside weekends)
#: a legendary boss becomes a mythic one that a single player can fight.
LEGENDARY_HOURS = range(16, 23)

_KEY_ENABLED = "boss_spawning_enabled"
_KEY_QUIET_UNTIL = "boss_quiet_until"
#: Rarity of a summoned (ritual / skill gem) boss that is on the channel; kept
#: so that a restart of the bot does not make that boss vanish.  Always an int (or
#: None): ``_restore`` - and the code before 2026-11-26, should it run again - reads it so.
_KEY_SUMMONED = "summoned_boss_rarity"
#: ... and how it came, when, who summoned it and whether anybody has claimed it since
#: (``{"origin": "scroll", "at": "<iso time>", "by": <player id or None>, "claimed": false}``): a key
#: of its own, so that the one above stays a plain int.  Written and cleared with it
#: (``_remember_summoned``).  ``at`` is when its stay began (the summon, or the last interrupted
#: fight): a restart does not begin it anew for a scroll boss.  ``claimed`` stays true once
#: somebody attacked it - such a boss never gives its scroll back.
_KEY_SUMMONED_INFO = "summoned_boss_info"
#: A scroll owed back that the cycle pays after the start (``{"by": id, "why": text}``): written by
#: ``AlterBot._review_saved_state``, which drops a summoned boss of a ``runtime.json`` too old to
#: trust before the cycle exists (see :func:`scroll_refund_owed`).
KEY_REFUND_DUE = "summoned_scroll_refund_due"
#: How a summoned boss may have come (``BossInfo.origin``): '' = not said (the default of
#: :meth:`BossCycle.summon`), the Zwój przywołania, the summoning gem, the ritual.
SUMMON_ORIGINS: tuple[str, ...] = ("", "scroll", "gem", "ritual")
#: An open passive event a boss summoned with its own picture would close (owner, 2026-11-26: it waits
#: instead, :meth:`BossCycle.open_event_in_the_way`): the merchant's cart, the mine, a chest or a shrine.
OPEN_MERCHANT, OPEN_MINE, OPEN_EVENT = "merchant", "mine", "event"
#: ``runtime.json``: a fight was decided and its post-fight reset has not run yet -
#: ``{"losers": [ids to ban]}``; see ``BossCycle.mark_decided`` and ``_finish_after_restart``.
_KEY_PENDING_RESET = "boss_reset_pending"
#: The write that makes a decided fight restart-safe may take this long (seconds): the result
#: is announced right after it, and ``finish`` writes the same row again anyway.
DECIDED_WRITE_TIMEOUT = 2.0
#: Where the "a monster appeared" notice on the general chat is (``{"channel", "message"}``),
#: so that it can be taken away when the boss is gone - also after a restart of the bot.
_KEY_SPAWN_NOTICE = "boss_spawn_notice"
#: How far back the general chat is searched for left-over notices after a start.
NOTICE_SWEEP_LIMIT = 300
#: Events that only wait for a command; everything else is an activity in progress.
_PASSIVE_EVENTS = (EventType.SHRINE, EventType.CHEST, EventType.MERCHANT, EventType.DUNGEON)
#: How long (seconds) the boss waits for a running hunt / event before it takes the channel,
#: and how often it looks (``GameState.wait_until_idle``).  It never waits for the mine.
IDLE_TIMEOUT = 1800.0
IDLE_POLL = 5.0


def somebody_was_first(rarity: int) -> str:
    """Answer to a "Zaatakuj" click that came a moment after somebody else's attack."""
    return MSG_PARTY_GATHERS if int(rarity) == 3 else MSG_RIVAL_WELCOME


def is_spawn_notice(content: Any, boss_channel_id: int) -> bool:
    """True for the text of a "a monster appeared" notice - in this bot's and in the old bot's wording."""
    text = str(content or "")
    return f"<#{int(boss_channel_id)}>" in text and "pojawił się" in text and "potwór" in text


#: Symbol and name of a boss rarity (the usual loot colours; the first three are the
#: squares of the boss embed's thumbnail).  Shown wherever a boss is announced.
RARITY_EMOJI: dict[int, str] = {0: "🟩", 1: "🟦", 2: "🟪", 3: "🟧", 4: "🟥"}
RARITY_NAMES: dict[int, str] = {0: "zwykły", 1: "rzadki", 2: "epicki", 3: "legendarny", 4: "mityczny"}
#: Rarity of a boss that needs a party (see ``boss.GROUP_RARITY``).
_PARTY_RARITY = 3


def rarity_emoji(rarity: int) -> str:
    """The symbol of ``rarity``; an unknown one is shown as legendary (as the boss embed does)."""
    return RARITY_EMOJI.get(int(rarity), RARITY_EMOJI[_PARTY_RARITY])


def spawn_notice(boss_channel_id: int, rarity: int | None = None) -> str:
    """The notice on the general chat when a boss appears (wording refreshed on the owner's request).

    With ``rarity`` it also says how rare the boss is, with the symbol of that
    rarity (owner's request of 2026-11-20).  Whatever is added, the words
    :func:`is_spawn_notice` looks for must stay.
    """
    if rarity is None or int(rarity) not in RARITY_NAMES:
        return (f"⚔️ Przygotujcie się! Na kanale <#{boss_channel_id}> właśnie pojawił się potwór. "
                "Kto pierwszy stanie do walki, ten zgarnie nagrody!")
    rarity = int(rarity)
    ending = ("Sam nikt mu nie podoła - zbierzcie drużynę!" if rarity == _PARTY_RARITY
              else "Kto pierwszy stanie do walki, ten zgarnie nagrody!")
    return (f"⚔️ Przygotujcie się! Na kanale <#{boss_channel_id}> właśnie pojawił się potwór: "
            f"{RARITY_EMOJI[rarity]} **{RARITY_NAMES[rarity]} boss**. {ending}")


#: The general-chat notice of a summoned boss, by how it came (owner, 2026-11-26).  Each keeps the words
#: :func:`is_spawn_notice` looks for ("pojawił się", "potwór"): it is removed like the scheduled boss's.
NOTICE_SCROLL = ("📯 **{name}** odczytał Zwój przywołania! Na kanale <#{channel}> właśnie pojawił się potwór: "
                 "🟥 **mityczny boss**. Zaatakować go może każdy - kto pierwszy, ten lepszy, a bestia nie będzie "
                 "czekać wiecznie!")
NOTICE_GEM = ("💎 Ktoś użył Klejnotu Przywołania! Na kanale <#{channel}> właśnie pojawił się potwór: {emoji} "
              "**{rarity} boss**. Kto pierwszy stanie do walki, ten zgarnie nagrody!")
NOTICE_RITUAL = ("🕯️ Rytuał się udał! Na kanale <#{channel}> właśnie pojawił się potwór: {emoji} **{rarity} boss**. "
                 "Kto pierwszy stanie do walki, ten zgarnie nagrody!")
#: Who read the scroll, when the notice cannot find his name (he left the server meanwhile).
NOTICE_SOMEBODY = "Ktoś"


def summon_notice(boss_channel_id: int, rarity: int, origin: str, name: str = "") -> str:
    """The general-chat notice of a boss summoned by ``origin`` (``name``: who read the scroll).

    The name is escaped here (a ``*`` in a nickname must not break the bold);
    the notice is posted without any mention.  A summon that names no origin
    gets the scheduled boss's notice (:func:`spawn_notice`).
    """
    if origin == "scroll":
        shown = discord.utils.escape_markdown(str(name).strip()) or NOTICE_SOMEBODY
        return NOTICE_SCROLL.format(name=shown, channel=int(boss_channel_id))
    text = {"gem": NOTICE_GEM, "ritual": NOTICE_RITUAL}.get(origin)
    if text is None or int(rarity) not in RARITY_NAMES:
        return spawn_notice(boss_channel_id, rarity)
    return text.format(channel=int(boss_channel_id), emoji=RARITY_EMOJI[int(rarity)],
                       rarity=RARITY_NAMES[int(rarity)])


def scroll_refund_owed(info: Any) -> dict[str, Any] | None:
    """``{"by": id}`` when the summoned boss noted by ``info`` gives its scroll back should it vanish now.

    Only a boss of a Zwój przywołania whose summoner is known and that nobody
    has claimed (owner, 2026-11-26: a claimed or fought boss never gives its
    scroll back; one that simply left after half an hour neither - that path
    never asks).  ``info`` is the note of :data:`_KEY_SUMMONED_INFO`.
    """
    if not isinstance(info, dict) or info.get("origin") != "scroll" or info.get("claimed"):
        return None
    try:
        player_id = int(info.get("by") or 0)
    except (TypeError, ValueError):
        return None
    return {"by": player_id} if player_id > 0 else None


def _moment(text: Any) -> datetime | None:
    """An aware UTC moment of ``runtime.json`` (an ISO text), or None when it cannot be read."""
    try:
        moment = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


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
    """A click on "Zaatakuj": exactly ``!zaatakuj``; the refusals go through ``ui.reply``.

    The click is acknowledged as soon as the boss is claimed; the encounter
    (lobby and fight) then runs inside this handler.
    """
    bot = interaction.client
    # Same guards as the command: the boss channel only (and no dead hunters -
    # they cannot type the command either).  The busy flag is checked by
    # ``attack`` itself: it stops an attack, not a click that joins a running lobby.
    if not await ui.gate(interaction, unless_busy=False):
        return
    cycle: BossCycle = bot.boss_cycle
    door = cycle.lobby_door
    if bot.state.boss_phase is BossPhase.ENGAGED and door is not None:
        await door(interaction)  # an old button clicked while a lobby runs: like a typed !zaatakuj
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
        #: The clean-up after a start of the process was done (once, see :meth:`tidy_after_start`).
        self._tidied = False
        self._wind_sent = False
        self._appears_at: datetime | None = None
        self._spawn_notice_message: Any = None
        #: Messages that carry the "Zaatakuj" button right now.
        self._attack_buttons: list[Any] = []
        #: Where the button was taken off when the boss was engaged (restored when needed).
        self._engaged_buttons: list[Any] = []
        self._lobby_door: Callable[[Any], Awaitable[None]] | None = None
        #: The encounter whose lobby is about to open: set by :meth:`attack` at the claim
        #: of the boss, None again as soon as its lobby opens the door (or it ends).
        self._lobby_opening: int | None = None
        #: Who claimed the boss of the running encounter (a second click of theirs says nothing).
        self._claimer_id: int | None = None
        #: Counts the encounters (claims of the boss by :meth:`attack`) and the bosses that
        #: are gone, so that a late notice about an interrupted fight can tell it is out of date.
        self._encounters = 0
        #: ``(encounter, result)`` once the running fight is decided, see :meth:`mark_decided`.
        self._decided: tuple[int, FightResult] | None = None
        #: The encounter whose fight :meth:`attack` is running now (None: none), and its task.
        self._active_encounter: int | None = None
        self._fight_task: asyncio.Task | None = None

    @property
    def lobby_door(self) -> Callable[[Any], Awaitable[None]] | None:
        """Set by a running lobby: ``async (interaction) -> None`` takes a click on an old
        "Zaatakuj" button as a typed ``!zaatakuj`` (and answers it)."""
        return self._lobby_door

    @lobby_door.setter
    def lobby_door(self, door: Callable[[Any], Awaitable[None]] | None) -> None:
        self._lobby_door = door
        if door is not None:
            self._lobby_opening = None

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
        """``!startSpawnBoss``: switch spawning on and restart the loop."""
        self.bot.store.runtime_set(_KEY_ENABLED, True)
        self._restored = False
        self.start()

    async def disable(self) -> None:
        """``!stopSpawnBoss``: no more bosses until enabled again (survives restarts).

        An open mine stays open, but no longer closes at the end of the quiet
        hour (:meth:`silence_at`): it is told its new latest closing at once.
        A boss of a Zwój przywołania that nobody had claimed is gone with it: its
        scroll goes back (:meth:`_refund_scroll`, once - the note is dropped
        before anything is awaited).
        """
        from . import dungeon

        owed = self._owed_refund()
        self.bot.store.runtime_set(_KEY_ENABLED, False)
        self.bot.supervisor.stop(LOOP_NAME)
        self._forget_schedule()
        await self.bot.state.set_boss_phase(BossPhase.IDLE)
        await dungeon.recheck_closing_time(self.bot)
        await self.retire_attack_buttons()
        await self.remove_spawn_notice()
        await self._database.update_boss_table(self.bot, 0, 0, False)
        await self._refund_scroll(owed, WHY_STOPPED)

    async def reset(self) -> None:
        """``!ResetBoss``: drop whatever is going on and start a fresh cycle.

        A boss of a Zwój przywołania that nobody had claimed gives its scroll
        back (as :meth:`disable`); a claimed or fought one does not.
        """
        bot = self.bot
        owed = self._owed_refund()
        bot.supervisor.stop(LOOP_NAME)
        self._forget_schedule()
        bot.state.force_release_busy()
        bot.state.close_event()
        bot.state.boss = BossInfo()
        await bot.state.set_boss_phase(BossPhase.IDLE)
        await self.retire_attack_buttons()
        await self.remove_spawn_notice()
        await self._guarded("reset boss table", self._database.update_boss_table(bot, 0, 0, False))
        await self._guarded("post-fight reset", self._boss.reset_end(bot))
        self._restored = True  # the schedule was wiped on purpose - do not resume it
        if self.enabled:
            self.start()
        await self._refund_scroll(owed, WHY_RESET)

    def _owed_refund(self) -> dict[str, Any] | None:
        """The scroll owed back should the boss on the channel vanish NOW (:func:`scroll_refund_owed`).

        Only while it stands there unclaimed (VISIBLE): a boss that is being
        fought, or leaving on its own, gives nothing back.  Synchronous: the
        callers drop the note right after it (``_forget_schedule``), before any
        await - so no scroll can go back twice.
        """
        if self.bot.state.boss_phase is not BossPhase.VISIBLE:
            return None
        return scroll_refund_owed(self._remembered_info())

    async def _refund_scroll(self, owed: dict[str, Any] | None, why: str) -> bool:
        """Give the Zwój przywołania of ``owed`` (:func:`scroll_refund_owed`) back; True when it went back.

        Never raises.  The note of the boss is already gone (the caller dropped
        it before the first await), so this runs once.  The owner is told
        either way (with a ping when it failed: he gives it back by hand); the
        player only while the boss channel is quiet - nothing may post into a
        fight.  ``give_summon_scrolls``: one transaction, never retried.
        """
        if not owed:
            return False
        bot = self.bot
        player_id = int(owed["by"])
        try:
            from . import pets

            await pets.give_summon_scrolls(bot, player_id, 1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the owner gives it back by hand
            log.exception("The summoning scroll of %s could not be given back (%s).", player_id, why)
            await bot.log_to_owner(LOG_SCROLL_NOT_REFUNDED.format(player_id=player_id, why=why,
                                                                  error=type(exc).__name__), ping=True)
            return False
        log.info("The summoning scroll of %s went back: its boss vanished unclaimed (%s).", player_id, why)
        await bot.log_to_owner(LOG_SCROLL_REFUNDED.format(player_id=player_id, why=why))
        try:
            from . import dungeon

            if dungeon.channel_quiet(bot, after_restart=True):
                await io.send(bot.boss_channel, MSG_SCROLL_REFUNDED.format(player_id=player_id),
                              allowed_mentions=discord.AllowedMentions(
                                  everyone=False, roles=False, users=[discord.Object(id=player_id)]))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the scroll is back; the word about it is only a courtesy
            log.exception("Could not tell %s that the summoning scroll went back.", player_id)
        return True

    async def _pay_refund_due(self) -> None:
        """A scroll owed back since the start (:data:`KEY_REFUND_DUE`, see ``AlterBot._review_saved_state``).

        Never raises; the note is taken off ``runtime.json`` before the scroll
        is given back (once).
        """
        try:
            due = self.bot.store.runtime_get(KEY_REFUND_DUE)
            if due is None:
                return
            self.bot.store.runtime_set(KEY_REFUND_DUE, None)
        except OSError:
            log.exception("Could not read the summoning scroll owed back after the start.")
            return
        owed = scroll_refund_owed({"origin": "scroll", "by": due.get("by")} if isinstance(due, dict) else None)
        await self._refund_scroll(owed, str(due.get("why") or WHY_STALE) if isinstance(due, dict) else WHY_STALE)

    def _forget_schedule(self) -> None:
        self._encounters += 1  # whatever stood there is gone: a late "still here" notice is out of date
        self.arrives_at = None
        self.quiet_until = None
        self._resume_phase = None
        self._summoned_at = None
        self._wind_sent = False
        self._appears_at = None
        self.bot.store.runtime_set(_KEY_QUIET_UNTIL, None)
        self._remember_summoned(None)

    def _remember_summoned(self, rarity: int | None, *, origin: str = "", at: datetime | None = None,
                           by: int | None = None, claimed: bool = False) -> None:
        """Note (or forget, ``None``) the summoned boss standing on the channel.  Never raises.

        The rarity goes under :data:`_KEY_SUMMONED` (an int, as always), its
        origin, the moment its stay began, its summoner and whether it was
        claimed under :data:`_KEY_SUMMONED_INFO` - both are cleared together,
        so every path that forgets the boss forgets both.
        """
        info = None if rarity is None else {"origin": origin if origin in SUMMON_ORIGINS else "",
                                            "at": (at or clock.utcnow()).isoformat(),
                                            "by": int(by) if by else None, "claimed": bool(claimed)}
        try:
            if self.bot.store.runtime_get(_KEY_SUMMONED) != rarity:
                self.bot.store.runtime_set(_KEY_SUMMONED, rarity)
            if self.bot.store.runtime_get(_KEY_SUMMONED_INFO) != info:
                self.bot.store.runtime_set(_KEY_SUMMONED_INFO, info)
        except OSError:
            log.exception("Could not persist the summoned boss (%s).", rarity)

    def _remembered_info(self) -> dict[str, Any] | None:
        """The note of the summoned boss in ``runtime.json`` (None when none or not readable).  Never raises."""
        try:
            info = self.bot.store.runtime_get(_KEY_SUMMONED_INFO)
        except OSError:
            log.exception("Could not read how the summoned boss came.")
            return None
        return dict(info) if isinstance(info, dict) else None

    def _remembered_origin(self) -> str:
        """The origin of the summoned boss kept in ``runtime.json`` ('' when none or not readable).  Never raises."""
        origin = (self._remembered_info() or {}).get("origin")
        return origin if isinstance(origin, str) and origin in SUMMON_ORIGINS else ""

    def _amend_summoned(self, **changes: Any) -> None:
        """Change the note of the summoned boss (``claimed=True``, a new ``at``) - when there is one.  Never raises."""
        info = self._remembered_info()
        if info is None:
            return
        info.update({key: value.isoformat() if isinstance(value, datetime) else value
                     for key, value in changes.items()})
        try:
            self.bot.store.runtime_set(_KEY_SUMMONED_INFO, info)
        except OSError:
            log.exception("Could not update the note of the summoned boss (%s).", changes)

    def seconds_to_arrival(self) -> float | None:
        return clock.seconds_until(self.arrives_at) if self.arrives_at is not None else None

    # -- the loop ---------------------------------------------------------------

    async def run(self) -> None:
        """The supervised loop.  Any exception restarts it; phases are re-entrant."""
        bot = self.bot
        await bot.wait_until_ready()
        if not self._tidied:
            # Once per start of the process - not when the loop is merely restarted.
            self._tidied = True
            await self.tidy_after_start()
            await self._pay_refund_due()  # (a scroll boss of a runtime.json too old to trust)
            if bot.state.boss_phase < BossPhase.VISIBLE:
                # A reset owed by a fight decided before the restart (also with spawning switched off).
                await self._finish_after_restart()
        if not self.enabled:
            # A scroll boss lives on this loop (it leaves after its half hour): with spawning off it
            # would never come back nor leave - its scroll goes back now, if nobody had claimed it.
            # (A gem / ritual boss waits for !startSpawnBoss, as it always did.)
            owed = scroll_refund_owed(self._remembered_info())
            if owed is not None and bot.state.boss_phase < BossPhase.VISIBLE:
                self._remember_summoned(None)
                await self._refund_scroll(owed, WHY_SPAWNING_OFF)
            log.info("Boss spawning is switched off (!startSpawnBoss enables it).")
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

    async def tidy_after_start(self) -> None:
        """The bot has just started: what the previous run left behind goes.  Never raises.

        Owner's request.  Nothing from before a restart is alive any more - a
        boss picture whose boss is not attackable, buttons of events and hunts
        that died with the process, half a fight.  So:

        * the boss channel is cleared exactly as it is before an event (pinned
          messages and the oldest one - the "how to play" post - stay);
        * "a monster appeared" notices of this bot on the general chat go: no
          boss is standing there now (a boss that was, comes back with a fresh
          notice - see :meth:`_restore`);
        * the pinned game panel gets the current text and buttons (a pinned
          message survives every clearing, also one posted by older code).

        Runs before the schedule is restored, so a summoned boss that comes
        back is posted on the clean channel.
        """
        bot = self.bot
        state = bot.state
        try:
            if state.busy:
                log.info("Start-up: the boss channel is not cleared - %s already runs.", state.busy_owner)
            else:
                # Held for the moment of the purge: a hunt started in that second would lose its first lines.
                with state.hold_busy("startup-clear"):
                    deleted = await io.clear_channel(bot.boss_channel)
                log.info("Start-up: boss channel cleared (%s messages removed; pinned ones and the oldest stay).",
                         deleted)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - tidying up must never stop the boss loop
            log.exception("Start-up: could not clear the boss channel.")
        try:
            await self.remove_spawn_notice()
            removed = await self._sweep_spawn_notices()
            if removed:
                log.info("Start-up: removed %s left-over boss notice(s) from the general chat.", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Start-up: could not remove left-over boss notices.")
        try:
            from . import hub
            await hub.refresh_panel(bot)  # never raises
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Start-up: could not refresh the game panel.")

    async def _sweep_spawn_notices(self) -> int:
        """Delete this bot's "a monster appeared" notices from the general chat; returns how many.

        Only messages written by the bot itself whose text is such a notice
        (:func:`is_spawn_notice`); the last :data:`NOTICE_SWEEP_LIMIT` messages
        are looked at.  Nothing else on that channel is touched.
        """
        bot = self.bot
        me = getattr(bot.user, "id", None)
        if me is None:
            return 0
        announce = bot.text_channel(bot.ids.announce_channel)
        doomed = []
        async for message in announce.history(limit=NOTICE_SWEEP_LIMIT):
            if (getattr(message.author, "id", None) == me and not getattr(message, "pinned", False)
                    and is_spawn_notice(message.content, bot.ids.boss_channel)):
                doomed.append(message)
        removed = 0
        for message in doomed:
            if await io.safe_delete(message):
                removed += 1
        return removed

    def _remember_spawn_notice(self, message: Any) -> None:
        """Note (or forget) the notice on the general chat.  Never raises."""
        self._spawn_notice_message = message
        try:
            value = None if message is None else {"channel": int(message.channel.id), "message": int(message.id)}
            if self.bot.store.runtime_get(_KEY_SPAWN_NOTICE) != value:
                self.bot.store.runtime_set(_KEY_SPAWN_NOTICE, value)
        except Exception:  # noqa: BLE001 - only bookkeeping
            log.exception("Could not persist the boss spawn notice.")

    async def remove_spawn_notice(self) -> None:
        """Take the "a monster appeared" notice off the general chat.  Never raises.

        Called when the boss it announced is gone (fought, left, reset) - a
        notice about a boss that is not there is only clutter (owner's request) -
        and before a new one is posted.  Works after a restart too: the
        notice is remembered in ``runtime.json``.
        """
        bot = self.bot
        message = self._spawn_notice_message
        try:
            if message is None:
                remembered = bot.store.runtime_get(_KEY_SPAWN_NOTICE)
                if isinstance(remembered, dict):
                    channel = bot.get_channel(int(remembered.get("channel") or 0))
                    if channel is not None:
                        try:
                            message = await channel.fetch_message(int(remembered.get("message") or 0))
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001 - usually: deleted by hand meanwhile
                            message = None
            await io.safe_delete(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Could not remove the boss spawn notice.")
        finally:
            self._remember_spawn_notice(None)

    async def _restore(self) -> None:
        """After a (re)start: continue the persisted schedule or begin a new one."""
        bot = self.bot
        if bot.state.boss_phase < BossPhase.VISIBLE:
            # (Normally done by run() already; here for a reset that failed then - the marker says so.)
            await self._finish_after_restart()
        row = await self._database.read_boss_table(bot)
        if bot.state.boss_phase >= BossPhase.VISIBLE:
            # Spawning was switched on while a summoned boss is on the channel (or
            # being fought): that boss stays; only the schedule is picked up.  The
            # scheduled boss itself (!startSpawnBoss while it waits) is not a
            # summoned one: it keeps waiting for ever, nothing is picked up.
            summoned = bot.store.runtime_get(_KEY_SUMMONED) is not None
            if summoned and row is not None and row.respawn_started and row.respawn_at is not None:
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

        # A ritual / skill gem / scroll boss that was standing there when the bot stopped comes back
        # (a scroll boss with what is left of its half hour, see summon's ``restored``).  One that
        # cannot come back is forgotten - a scroll boss nobody had claimed gives its scroll back.
        summoned = bot.store.runtime_get(_KEY_SUMMONED)
        if summoned is not None:
            info = self._remembered_info()
            back = False
            try:
                back = await self.summon(bot.boss_scene(), int(summoned), origin=self._remembered_origin(),
                                         restored=info or {})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the regular schedule matters more
                log.exception("Could not bring the summoned boss back after a restart.")
            if back:
                log.info("Summoned boss of rarity %s is back on the channel after a restart.", summoned)
            else:
                self._remember_summoned(None)
                await self._refund_scroll(scroll_refund_owed(info), WHY_NOT_BACK)

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
        """Quiet hour after the last fight, then clear the channel and announce silence.

        The moment the quiet hour ends the boss cycle needs the channel: an
        open mine is closed at once (:meth:`_wait_for_the_channel`) - that is
        the moment :meth:`silence_at` tells.
        """
        bot = self.bot
        if self.quiet_until is not None:
            remaining = clock.seconds_until(self.quiet_until)
            if remaining > 0 and not await bot.state.sleep_while_boss_phase(BossPhase.SCHEDULED, remaining):
                return
        await self._wait_for_the_channel()
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
            await self._wait_for_the_channel()
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

        if not await self._wait_for_the_channel() and state.busy:
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
            shown = await self._boss.send_boss_image(scene, state.boss.rarity)
            is_player_boss, player_boss, *_ = shown
            # (The special variant the boss appeared as goes with it into the fight: the loot needs it.)
            state.boss = BossInfo(rarity=state.boss.rarity, is_player_boss=is_player_boss,
                                  player_boss=player_boss, variant=bossvariants.variant_of(shown))
        self._wind_sent = False
        self._appears_at = None
        self._resume_phase = None
        await state.set_boss_phase(BossPhase.VISIBLE)

    async def _close_mine(self) -> None:
        """The boss needs the channel: an open mine (``features/dungeon.py``) closes NOW.  Never raises.

        Owner's rule: the mine is closed BY the boss - the boss never waits
        for it.  It is closed by its own rule (only who took his loot has it:
        an expedition still in a chamber loses everything, undecided eggs go
        where there is room or are lost), and that waits for nobody's answer -
        at most a few seconds for a click that is being handled
        (``dungeon.CLOSE_WAIT``).  A no-op without an open mine.
        """
        from . import dungeon

        await self._guarded("closing the mine", dungeon.close(self.bot))

    async def _wait_for_the_channel(self) -> bool:
        """``state.wait_until_idle`` for the boss; False when the channel is still taken after half an hour.

        A hunt, a tower run, an interactive event and an unclaimed chest /
        shrine are waited for as they always were.  The mine is not: it is
        closed at the first look, and at every later one should it have been
        opened while the boss still waits for something else.
        """
        return await self.bot.state.wait_until_idle(timeout=IDLE_TIMEOUT, poll=IDLE_POLL, each_look=self._close_mine)

    def wants_the_channel(self) -> bool:
        """True while an open mine would be closed at once: the boss cycle is taking the channel.

        From the end of the quiet hour until the "silence" line is posted, and
        from the moment the scheduled boss is due.  The events cog does not
        draw the mine then (it would live for seconds).
        """
        phase = self.bot.state.boss_phase
        if phase is BossPhase.SCHEDULED:
            return self.quiet_until is None or self.quiet_until <= clock.utcnow()
        return phase >= BossPhase.PRE_SPAWN

    @property
    def tidied(self) -> bool:
        """True once the start-up tidying of the boss channel has begun (see :meth:`tidy_after_start`).

        The channel is held busy while it is cleared, so "tidied and not busy"
        means: what is posted now stays.
        """
        return self._tidied

    def silence_at(self) -> datetime | None:
        """When the quiet hour after the last fight ends, while that is still ahead - else None.

        At that moment the boss cycle needs the channel for its "silence"
        line and closes an open mine AT ONCE (:meth:`_phase_scheduled` - also
        when it then still has to wait for a hunt before it clears the
        channel), so no mine is open after it.  It is the only moment of the
        boss's way that is no secret (a fixed pause after the fight): the
        arrival itself is never told - the rarity follows from it and the
        pets' boss detection is a reward - so a mine opened after the silence
        only knows the next slot of the event loop.
        """
        if self.bot.state.boss_phase is not BossPhase.SCHEDULED or self.quiet_until is None:
            return None
        return self.quiet_until if self.quiet_until > clock.utcnow() else None

    async def _detect(self, rarity: int) -> None:
        from . import pets
        await pets.detect_boss(self.bot, rarity)

    async def _announce_spawn(self, text: str | None = None) -> None:
        """General-chat notice; the previous one is removed.  Purely best effort.

        ``text``: the notice of a summoned boss (:func:`summon_notice`); None -
        the scheduled boss's (:func:`spawn_notice`).  Posted without any
        mention (a summoner's name is only a name).  The notice itself is
        removed again when its boss is gone (:meth:`remove_spawn_notice`).
        """
        bot = self.bot
        await self.remove_spawn_notice()
        try:
            announce = bot.text_channel(bot.ids.announce_channel)
            if text is None:
                text = spawn_notice(bot.ids.boss_channel, getattr(bot.state.boss, "rarity", None))
            self._remember_spawn_notice(
                await io.send(announce, text, allowed_mentions=discord.AllowedMentions.none()))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Could not post the boss spawn notice.")

    async def _announce_summoned(self, scene: Scene, rarity: int, origin: str, summoner: int | None) -> None:
        """The general-chat notice of a boss :meth:`summon` has just put on the channel.  Never raises.

        Posted once the boss is there (``BossInfo`` set, the phase VISIBLE) -
        on every way a boss is summoned, a boss brought back after a restart
        included.  The scroll's notice names the reader: the scene's author, or
        (after a restart) the member noted as the summoner.  Should the boss be
        gone already when the notice is out (fought within a second, reset), the
        notice goes at once.
        """
        try:
            name = ""
            if origin == "scroll":
                member = scene.author
                if member is None and summoner:
                    member = await self.bot.member(summoner)
                name = (getattr(member, "display_name", None) or str(member)) if member is not None else ""
            await self._announce_spawn(summon_notice(self.bot.ids.boss_channel, rarity, origin, name))
            if self.bot.state.boss_phase < BossPhase.VISIBLE:
                await self.remove_spawn_notice()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a notice is never worth an error
            log.exception("Could not post the notice of the summoned boss.")

    def summoned_leaves_at(self) -> datetime | None:
        """When an unattacked summoned boss leaves (None: never).

        Owner's rule: a ritual / skill gem boss waits only until the scheduled
        boss is due - but at least ``timings.summoned_min_stay`` after it
        appeared, so nobody summons a boss that is gone within seconds.

        A boss of a Zwój przywołania (owner, 2026-11-26) leaves
        ``timings.scroll_boss_stay`` (half an hour) after it appeared, whatever
        the schedule says: a scheduled boss that falls due meanwhile waits.
        """
        if self._resume_phase is None:
            return None
        if getattr(self.bot.state.boss, "origin", "") == "scroll":
            if self._summoned_at is None:
                self._summoned_at = clock.utcnow()
            return self._summoned_at + timedelta(seconds=self.bot.timings.scroll_boss_stay)
        if self.arrives_at is None or not self.enabled:
            return None
        if self._summoned_at is None:
            self._summoned_at = clock.utcnow()
        stays_until = self._summoned_at + timedelta(seconds=self.bot.timings.summoned_min_stay)
        return max(self.arrives_at, stays_until)

    def _phase_after_scroll_boss(self) -> BossPhase:
        """Where the schedule stands once a scroll boss nobody attacked has left.

        It leaves at its own time, not when the scheduled boss is due: SCHEDULED
        while the quiet hour after the last fight lasts (its "silence" and the
        mine's closing are still to come), WAITING after it, IDLE without a
        schedule at all (a new one is drawn).
        """
        if self.arrives_at is None or not self.enabled:
            return BossPhase.IDLE
        if self.quiet_until is not None and self.quiet_until > clock.utcnow():
            return BossPhase.SCHEDULED
        return BossPhase.WAITING

    async def _summoned_boss_leaves(self) -> None:
        """The summoned boss nobody attacked is gone: the scheduled boss is due (or its half hour is over).

        Called with the boss already claimed (ENGAGED), so nobody can attack it
        any more.  Nothing is reset: the shrine modifiers, the dead hunters and
        the schedule belong to the boss that arrives now - without the quiet
        hour that follows a fight.  Its general-chat notice goes.

        A boss of a Zwój przywołania leaves at the end of its half hour with a
        text of its own; its scroll is lost (owner, 2026-11-26: a refund would
        let anybody block the channel for free) and the schedule goes on where
        it stands (:meth:`_phase_after_scroll_boss`).  A ritual / gem boss
        leaves for the scheduled boss, which is due now (WAITING).

        When the loop is cancelled meanwhile by !ResetBoss / !stopSpawnBoss, the
        phase is left alone: they have set their own (IDLE) already.  A plain
        restart of the loop (!startSpawnBoss while spawning is on) sets nothing,
        so the boss - claimed, with no fight - is still sent away: ENGAGED
        without a fight would block the channel for two hours.
        """
        bot = self.bot
        scroll = getattr(bot.state.boss, "origin", "") == "scroll"
        log.info("Nobody attacked the summoned boss (rarity %s, %s); it leaves.", bot.state.boss.rarity,
                 "its half hour is over" if scroll else "the scheduled boss is due")
        self._encounters += 1  # the boss is gone: a late "still here" notice is out of date
        cancelled = False
        try:
            await self.retire_attack_buttons()  # in case clearing the channel fails
            await self.remove_spawn_notice()
            await io.clear_channel(bot.boss_channel)
            await io.send_patient(bot.boss_channel, MSG_SCROLL_BOSS_LEFT if scroll else MSG_SUMMONED_LEFT)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._remember_summoned(None)
            resume = self._phase_after_scroll_boss() if scroll else BossPhase.WAITING
            self._resume_phase = None
            self._summoned_at = None
            if not cancelled or bot.state.boss_phase is BossPhase.ENGAGED:
                bot.state.boss = BossInfo(rarity=self.base_rarity)
                await bot.state.set_boss_phase(resume)

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

    def open_event_in_the_way(self) -> str | None:
        """The open passive event a summoned boss would close now - :data:`OPEN_MERCHANT`,
        :data:`OPEN_MINE` (``dungeon.is_open`` implies its event), :data:`OPEN_EVENT` (a chest,
        a shrine) - or None.

        Owner, 2026-11-26: a summon no longer sends them away (the merchant's
        cart, an expedition in the mine, an unclaimed chest): the Zwój
        przywołania and the summoning gem wait until they are gone, and nothing
        is used up meanwhile.  (The scheduled boss still closes them: it never
        waits for them.)
        """
        state = self.bot.state
        if not state.event_alive or state.event_type not in _PASSIVE_EVENTS:
            return None
        if state.event_type is EventType.MERCHANT:
            return OPEN_MERCHANT
        if state.event_type is EventType.DUNGEON:
            return OPEN_MINE
        return OPEN_EVENT

    async def summon(self, scene: Scene, rarity: int, *, show_image: bool = True,
                     is_player_boss: bool = False, player_boss: Any = None, variant: str = "",
                     origin: str = "", restored: dict[str, Any] | None = None) -> bool:
        """Put an extra boss on the channel right now (skill gem / ritual / Zwój przywołania).

        The regular schedule is only paused: when the summoned boss is dealt
        with, the scheduled one still arrives at its original time - and when
        nobody attacks the summoned boss until then, it leaves (see
        :meth:`summoned_leaves_at`).  Returns False (and does nothing) when a
        boss is already there or on its way.

        ``show_image=True`` clears the channel and posts the boss picture (skill
        gem, scroll); with ``show_image=False`` the caller already presented the
        boss (ritual) and passes ``is_player_boss`` / ``player_boss`` itself -
        and ``variant``, the special variant it showed the boss as ('' =
        ordinary; with ``show_image=True`` the picture posted here decides).
        With its own picture the boss also waits for an activity on the channel
        and for an open passive event (:meth:`open_event_in_the_way`: False,
        nothing is closed).

        ``origin`` (:data:`SUMMON_ORIGINS`): how the boss came - the Zwój
        przywołania passes ``"scroll"``, the summoning gem ``"gem"``, the ritual
        ``"ritual"``; it is kept in ``BossInfo.origin`` and in ``runtime.json``
        (a restart brings it back with it).  '' (the default) says nothing.  A
        scroll boss is refused while boss spawning is off: only the running
        loop sends it away after its half hour.  The summoner is the scene's
        author (named in the scroll's general-chat notice, given his scroll back
        should the boss vanish unclaimed).

        ``restored``: the note of the boss in ``runtime.json`` when it comes back
        after a restart (:meth:`_restore`; the scene has no author then) - its
        summoner and whether it was claimed go on, and a scroll boss keeps the
        moment its stay began: a restart never makes it stay longer, it only
        gets at least ``timings.summoned_min_stay`` more.

        Once the boss is there its notice goes to the general chat
        (:meth:`_announce_summoned`).
        """
        bot = self.bot
        state = bot.state
        origin = origin if origin in SUMMON_ORIGINS else ""
        if not self.can_summon():
            return False
        if origin == "scroll" and not self.enabled:
            return False
        if show_image:
            if self.activity_in_the_way() or self.open_event_in_the_way() is not None:
                return False
            # Posting the picture takes a moment: the channel is kept meanwhile, so
            # nothing else (a ritual that has just ended...) can show a second boss.
            with state.hold_busy("boss-summon"):
                await self._close_mine()  # (a no-op: an open mine refused the summon above)
                state.close_event()
                await io.clear_channel(scene.channel)
                shown = await self._boss.send_boss_image(scene, rarity)
                is_player_boss, player_boss, *_ = shown
                variant = bossvariants.variant_of(shown)
            if not self.can_summon():
                return False  # the scheduled boss started arriving while we were posting
        else:
            state.close_event()
        # Read only now: the phase may have moved on (IDLE -> SCHEDULED) during the upload.
        self._resume_phase = state.boss_phase
        now = clock.utcnow()
        summoner = getattr(scene.author, "id", None)
        claimed, began = False, now
        if restored is not None:
            claimed = bool(restored.get("claimed"))
            try:
                summoner = summoner or int(restored.get("by") or 0) or None
            except (TypeError, ValueError):
                summoner = None
            if origin == "scroll":
                timings = bot.timings
                latest_start = now - timedelta(seconds=timings.scroll_boss_stay - timings.summoned_min_stay)
                stored = _moment(restored.get("at"))
                began = min(max(stored, latest_start), now) if stored is not None else latest_start
        self._summoned_at = began
        self._encounters += 1  # a new boss: notices about the previous one are out of date
        state.boss = BossInfo(rarity=int(rarity), is_player_boss=is_player_boss, player_boss=player_boss,
                              variant=variant if isinstance(variant, str) else "", origin=origin)
        self._remember_summoned(int(rarity), origin=origin, at=began, by=summoner, claimed=claimed)
        await state.set_boss_phase(BossPhase.VISIBLE)
        await self._announce_summoned(scene, int(rarity), origin, summoner)
        return True

    # -- fights -----------------------------------------------------------------

    async def attack(self, scene: Scene, *, interaction: Any = None) -> None:
        """``!zaatakuj``: claim the visible boss and run the whole encounter.

        ``interaction``: set when the attack is a click on "Zaatakuj" - the
        refusals are then private answers to the click, and the click is
        acknowledged as soon as the boss is claimed.

        Acknowledging the click and taking the buttons off are Discord round
        trips: they run in the background, so the lobby - and with it the door
        for the other players' clicks (:attr:`lobby_door`) - opens right after
        the claim.  A click that still comes in between is told where to sign
        up in a moment (:func:`somebody_was_first`), not that the boss is gone.

        Owner's rule: no attack while a hunt / tower run / event plays on the
        channel - the boss stays and waits.
        """
        bot = self.bot
        state = bot.state
        if state.boss_phase is BossPhase.ENGAGED:
            # Other players' !zaatakuj during the lobby is handled by the lobby itself.
            if interaction is not None:
                if self._lobby_opening is None:
                    await ui.reply(interaction, MSG_BOSS_TAKEN)
                elif getattr(interaction.user, "id", None) == self._claimer_id:
                    await ui.ack(interaction)  # the attacker clicked twice: the attack is already on
                else:
                    await ui.reply(interaction, somebody_was_first(state.boss.rarity))
            return
        if state.boss_phase is BossPhase.VISIBLE and state.activity_running:
            # (Only with a boss to attack: without one the answer below is the true one.)
            if interaction is not None:
                await ui.reply(interaction, ui.MSG_BUSY)
            else:
                await guards.notify_busy(scene)  # type: ignore[arg-type] - needs only ``.channel``
            return
        if not state.try_engage_boss():
            refusal = MSG_NOT_ATTACKABLE.format(user_id=scene.author.id)
            if interaction is not None:
                await ui.reply(interaction, refusal)
            else:
                await io.send(scene.channel, refusal)
            return
        if self._resume_phase is not None:
            # A summoned boss was claimed: from now on it never gives a Zwój przywołania back
            # (also after a restart - the note says so).
            self._amend_summoned(claimed=True)
        self._encounters += 1
        encounter = self._encounters
        self._decided = None
        self._active_encounter = encounter
        self._fight_task = asyncio.current_task()
        self._lobby_opening = encounter
        self._claimer_id = getattr(scene.author, "id", None)
        self._engaged_buttons, self._attack_buttons = self._attack_buttons, []
        claimed = asyncio.ensure_future(self._claimed(interaction, self._engaged_buttons))
        try:
            # Wake the cycle loop: it then watches the encounter with its time limit
            # (try_engage_boss is synchronous and cannot notify the waiters itself).
            await state.set_boss_phase(BossPhase.ENGAGED)
            info = state.boss
            try:
                result = await asyncio.wait_for(self._encounter(scene, info),
                                                timeout=bot.settings.boss_fight_time_limit)
                await asyncio.wait({claimed})
            except asyncio.CancelledError:
                if self._decided_result(encounter) is None:  # (a decided boss never comes back)
                    await self._interrupted(scene, "cancelled", claimed=claimed)
                raise
            except Exception as exc:  # noqa: BLE001 - whatever happened, leave a defined state
                if isinstance(exc, (FightInterrupted, asyncio.TimeoutError)):
                    log.warning("Boss fight interrupted: %s: %s", type(exc).__name__, exc)
                else:
                    log.exception("Boss fight crashed.")
                await asyncio.wait({claimed})  # the old buttons are off before the boss is attackable again
                decided = self._decided_result(encounter)
                if decided is None:
                    await self._interrupted(scene, f"{type(exc).__name__}: {exc}", claimed=claimed)
                    return
                # The fight was decided and broke afterwards (while the result was being told or
                # the rewards handed out): the boss is gone all the same - it is fought once.
                await bot.log_to_owner(MSG_DECIDED_FIGHT_BROKE.format(error=f"{type(exc).__name__}: {exc}"),
                                       ping=True)
                result = decided
        finally:
            if self._lobby_opening == encounter:
                self._lobby_opening = None
        await self.finish(result)

    async def _claimed(self, interaction: Any, messages: list[Any]) -> None:
        """The boss was claimed: acknowledge the click and take the buttons off ``messages``.  Never raises."""
        if interaction is not None:
            await ui.ack(interaction)
        for message in messages:
            await ui.retire(message)

    async def _encounter(self, scene: Scene, info: BossInfo) -> FightResult:
        """The fight with the boss ``info`` (:meth:`_fight`), with the loot of a summoned boss when it is one.

        A summoned boss - it has an origin, or the schedule waits behind it
        (``_resume_phase``: also a boss of the code before, brought back after a
        restart without its origin) - draws its loot as ``summoned``
        (``items.LOOT_SOURCE``): some rows of the loot tables never drop from a
        summoned boss (a Zwój przywołania cannot bring the next one).  Set here,
        so that no signature of the fights changed.
        """
        summoned = bool(getattr(info, "origin", "")) or self._resume_phase is not None
        if not summoned:
            return await self._fight(scene, info)
        with items.loot_source(items.SOURCE_SUMMONED):
            return await self._fight(scene, info)

    async def _fight(self, scene: Scene, info: BossInfo) -> FightResult:
        boss = self._boss
        state = self.bot.state
        # A special boss (features/bossvariants.py) tells the fight which variant it is: the
        # loot of a won fight depends on it.  An ordinary boss is fought by exactly the call
        # it always was.
        special = {"variant": info.variant} if getattr(info, "variant", "") else {}
        if info.rarity == 3:
            players = await boss.group_init(scene, info.rarity)
            if not players:
                return FightResult(won=False)
            await state.set_boss_phase(BossPhase.FIGHTING)
            return await boss.group_fight(scene, players, info.is_player_boss, info.player_boss, **special)
        hunter = await boss.single_init(scene, info.rarity)
        if hunter is None:
            return FightResult(won=False)
        await state.set_boss_phase(BossPhase.FIGHTING)
        return await boss.single_fight(scene, hunter, info.rarity, info.is_player_boss, info.player_boss,
                                       **special)

    async def mark_decided(self, result: FightResult) -> None:
        """The running fight is decided - the boss is dead or the hunters have lost.  Never raises.

        Called by the fight before it says or hands out anything.  A boss is
        fought once: from here on neither an error, nor the time limit of the
        encounter, nor a restart of the bot may bring this boss back.

        * :meth:`attack` ends a decided encounter with :meth:`finish` instead
          of treating a late failure as "interrupted - the boss stays".
        * What a restart would bring the boss back from is cleared now, not
          after the rewards: the note about a summoned boss, and for the
          scheduled boss its row in ``BOSS`` (the paused schedule of a
          summoned boss stays in the row - that boss is still to come).
        """
        if self.bot.state.boss_phase not in (BossPhase.ENGAGED, BossPhase.FIGHTING):
            return  # (not an encounter of the cycle)
        if self._active_encounter != self._encounters:
            return  # (a fight left over from before !ResetBoss: the boss standing now is another one)
        if self._fight_task is not None and asyncio.current_task() is not self._fight_task:
            return  # (the same, when somebody has attacked the new boss meanwhile)
        self._decided = (self._encounters, result)
        self._remember_summoned(None)
        # The reset after the fight (shrine modifiers, the dead, the bans) runs in finish();
        # should the bot stop before that, the next start makes up for it.
        self._remember_pending_reset([getattr(loser, "id", loser) for loser in result.losers_to_ban])
        if self._resume_phase is None:
            await self._guarded("boss table after a decided fight", asyncio.wait_for(
                self._database.update_boss_table(self.bot, 0, 0, False), timeout=DECIDED_WRITE_TIMEOUT))

    def _remember_pending_reset(self, losers: list[Any] | None) -> None:
        """Note (None: forget) that a decided fight still owes its post-fight reset.  Never raises."""
        try:
            value = None if losers is None else {"losers": [int(loser) for loser in losers]}
            if self.bot.store.runtime_get(_KEY_PENDING_RESET) != value:
                self.bot.store.runtime_set(_KEY_PENDING_RESET, value)
        except (OSError, TypeError, ValueError):
            log.exception("Could not persist the pending post-fight reset (%s).", losers)

    async def _finish_after_restart(self) -> None:
        """The bot stopped between a decided fight and its reset: do the reset now.  Never raises.

        Without it the shrine modifiers of the fought boss would also work for
        the next one, the dead would stay dead and the losers unbanned.  (Not
        the boss table: ``mark_decided`` wrote it, and for a summoned boss it
        holds the paused schedule.)
        """
        bot = self.bot
        try:
            pending = bot.store.runtime_get(_KEY_PENDING_RESET)
        except OSError:
            log.exception("Could not read the pending post-fight reset.")
            return
        if pending is None:
            return
        losers = pending.get("losers", []) if isinstance(pending, dict) else []
        log.warning("The bot stopped before the reset after a decided boss fight; doing it now (bans: %s).",
                    losers)
        from . import modifiers
        steps = [("modifiers after a restart", lambda: modifiers.init_modifiers(bot)),
                 ("dead hunters after a restart", lambda: self._boss.reset_dead_hunters(bot))]
        steps += [(f"dead hunter role for {loser}", lambda loser=loser: self._boss.set_dead_hunters(bot, loser))
                  for loser in losers]
        failed: list[str] = []
        for what, step in steps:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the others still run; the marker stays for the next start
                failed.append(what)
                log.exception("Step failed: %s", what)
        if not failed:
            self._remember_pending_reset(None)
        else:
            await bot.log_to_owner("Po restarcie nie udało się dokończyć resetu po walce z bossem "
                                   f"({', '.join(failed)}) - spróbuję przy następnym starcie; szczegóły są w "
                                   "logu bota.", ping=True)

    def _decided_result(self, encounter: int) -> FightResult | None:
        """The result of ``encounter`` when its fight was decided before something broke."""
        if self._decided is not None and self._decided[0] == encounter:
            return self._decided[1]
        return None

    async def _interrupted(self, scene: Scene, reason: str, *, claimed: asyncio.Future | None = None) -> None:
        """A fight ended because of a technical problem, not because of the players.

        By policy (``boss_returns_after_interrupted_fight``) the boss stays.  A
        summoned boss then gets its minimal stay anew: the rule that makes it
        leave for the scheduled boss is about a boss nobody attacked, and it
        must not leave at the very moment the "still here" notice appears.  A
        scroll boss gets a fresh half hour - noted in ``runtime.json`` too, so
        that a restart counts from here.
        """
        bot = self.bot
        stays = bot.settings.boss_returns_after_interrupted_fight
        if stays:
            if self._resume_phase is not None:
                self._summoned_at = clock.utcnow()
                self._amend_summoned(at=self._summoned_at)
            await bot.state.set_boss_phase(BossPhase.VISIBLE)
            message = MSG_FIGHT_INTERRUPTED_STAYS
        else:
            await self.finish(FightResult(won=False))
            message = MSG_FIGHT_INTERRUPTED_GONE
        # Notify in the background: Discord may be unreachable right now and the
        # state must not wait for it.
        asyncio.ensure_future(self._notify_interrupted(scene, message, reason, attack_button=stays,
                                                       encounter=self._encounters, claimed=claimed))

    async def _notify_interrupted(self, scene: Scene, message: str, reason: str, *,
                                  attack_button: bool = False, encounter: int | None = None,
                                  claimed: asyncio.Future | None = None) -> None:
        """Tell the channel (and the owner) about an interrupted fight.  Never raises.

        ``attack_button``: the boss stays and the notice carries the "Zaatakuj"
        button - but only while that is still true when the notice is finally
        posted (``encounter``: the value of the encounter counter at the
        interruption): a notice that arrives after the boss was engaged again,
        or is gone, carries no live button, and no button is put back.
        """
        if attack_button:
            if claimed is not None:
                await asyncio.wait({claimed})  # the buttons are off before they may be put back
            # The boss stays: the notice carries the "Zaatakuj" button.  When it
            # cannot be posted, the button goes back under the boss picture.
            engaged, self._engaged_buttons = self._engaged_buttons, []
            posted = await with_attack_button(
                lambda view: io.send_patient(scene.channel, message, view=view))
            current = (self.bot.state.boss_phase is BossPhase.VISIBLE
                       and (encounter is None or encounter == self._encounters))
            if not current:
                log.info("The interrupted-fight notice came too late: the boss was engaged again or is gone.")
                await ui.retire(posted)
            elif posted is not None:
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
        self._encounters += 1  # the boss is gone: a late "still here" notice is out of date
        try:
            await self.retire_attack_buttons()  # e.g. a boss that left unattacked
            await self.remove_spawn_notice()  # the boss it announced is gone
            await self._guarded("post-fight reset", self._boss.reset_end(bot))
            for loser in result.losers_to_ban:
                await self._guarded(f"dead hunter role for {loser}", self._boss.set_dead_hunters(bot, loser))
        finally:
            bot.state.boss = BossInfo()
            self._remember_summoned(None)
            self._remember_pending_reset(None)
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
