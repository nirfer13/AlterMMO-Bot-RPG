"""The pinned "Centrum gry" panel: the game's front door on the boss channel.

An administrator posts it with ``$panel`` (see ``altermmo/cogs/hub.py``); the
bot pins it, so the regular purge of the boss channel
(:func:`altermmo.core.io.clear_channel` keeps pinned messages) never removes
it.  Posting it again replaces the previous panel: its id is remembered in
``runtime.json`` (:func:`panel_key`, one entry per channel, so a panel tried
out on the test channel never removes the real one) and the old message is
deleted - a panel somebody already removed by hand is simply skipped.

The buttons are *permanent* (fixed ``custom_id``\\ s ``altermmo:hub:...``, no
timeout, no state in the view): the cog registers one :class:`HubView` at
start-up, so the panel keeps working after every restart.

Two kinds of buttons:

* **gameplay** - "Polowanie" and "Wieża" run exactly what ``$polowanie`` /
  ``$wieża`` run (:func:`altermmo.features.daily.start_hunt`,
  :func:`altermmo.features.tower.start_tower`) for the member who clicked.
  Boss channel only, never for dead hunters (:func:`ui.gate`); the busy flag
  is checked by the commands' own bodies, so a click during a hunt that is
  still gathering hunters JOINS it, exactly like typing ``$polowanie``.
  Refusals (cooldown, a boss on the way, busy...) are private; the hunt or
  the tower run itself is public, as when typed.
* **display** - "Towarzysz", "Stajnia", "Klejnoty", "Rankingi", "Pomoc"
  answer privately (:func:`ui.whisper_scene`), so a look at one's pet does
  not fill the channel.  They follow the busy policy of the display commands
  (``guards.unless_busy(read_only=True)``) and work for dead hunters too,
  like the commands they mirror.  Shown privately, the pet, the stable and
  the gem pouch carry no buttons of their own (the typed commands still
  show them with buttons).

Every click is acknowledged at once; the fights themselves stay typed.  A
click Discord has already given up on (the bot lagged for more than three
seconds - the player was told "interaction failed") starts nothing: no hunt
and no tower run is spent behind the player's back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from ..core import io, ui
from ..core.db import DatabaseUnavailable

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

#: ``runtime.json`` key of the posted panel: ``{"channel": <id>, "message": <id>}``.
PANEL_KEY = "hub_panel"

#: Fixed ``custom_id`` of every button (they must never change: old panels use them).
BUTTON_PREFIX = ui.PREFIX + "hub:"
HUNT_ID = BUTTON_PREFIX + "hunt"
TOWER_ID = BUTTON_PREFIX + "tower"
PET_ID = BUTTON_PREFIX + "pet"
STABLE_ID = BUTTON_PREFIX + "stable"
GEMS_ID = BUTTON_PREFIX + "gems"
RANKINGS_ID = BUTTON_PREFIX + "rankings"
HELP_ID = BUTTON_PREFIX + "help"

PANEL_TITLE = "🎮 Centrum gry AlterMMO"
PANEL_TEXT = (
    "Witaj, łowco! Stąd ruszysz na przygodę jednym kliknięciem. Wpisywane komendy działają tak jak "
    "dotąd - ich listę znajdziesz pod przyciskiem **Pomoc**.\n\n"
    "⚔️ **Polowanie** - zapoluj na zwykłe stwory (raz dziennie, modlitwa w kapliczce może to odnowić). "
    "Inni łowcy dołączą przyciskiem pod potworem.\n"
    "🗼 **Wieża** - wyrusz na eksplorację Wieży Śmierci (raz w tygodniu). Im wyżej, tym trudniej!\n"
    "🐾 **Towarzysz**, 🐴 **Stajnia**, 💎 **Klejnoty** - podejrzyj swojego towarzysza, stajnię "
    "i klejnoty umiejętności.\n"
    "🏆 **Rankingi** - sezonowy ranking łowców, Wieża Śmierci i najbardziej utalentowani towarzysze.\n"
    "❓ **Pomoc** - wszystkie komendy gry.\n"
    "*Odpowiedzi tych pięciu przycisków widzisz tylko Ty.*\n\n"
    "👹 **Boss** pojawia się na tym kanale co kilkanaście minut do kilkunastu godzin - kliknij "
    "**Zaatakuj** pod jego obrazkiem albo wpisz **$zaatakuj**.\n"
    "⌨️ **Walki** toczą się na klawiaturze: wpisz podane słowo, zanim minie czas! "
    "<:PepoG:790963160528977980>"
)
PANEL_FOOTER = "Polowanie i Wieża ruszają tylko na tym kanale i nie wtedy, gdy trwa inne wydarzenie."
PANEL_COLOUR = 0x42F34C

MSG_DB_DOWN = "Baza danych jest chwilowo niedostępna. Spróbuj ponownie za moment <:Sadge:936907659142111273>"

# Answers of ``$panel`` (for the administrator).
MSG_POSTED = "Panel **Centrum gry** jest na kanale <#{channel_id}> i został przypięty."
MSG_POSTED_NOT_PINNED = ("Panel **Centrum gry** jest na kanale <#{channel_id}>, ale nie udało się go przypiąć "
                         "(brak uprawnienia do zarządzania wiadomościami albo 50 przypiętych wiadomości?). "
                         "Kanał jest regularnie czyszczony - przypnij go ręcznie.")
MSG_NOT_POSTED = "Nie udało się opublikować panelu **Centrum gry** <:Sadge:936907659142111273>"


def panel_key(bot: "AlterBot", channel_id: int) -> str:
    """``runtime.json`` key of the panel on ``channel_id`` (:data:`PANEL_KEY` for the boss channel)."""
    return PANEL_KEY if int(channel_id) == int(bot.ids.boss_channel) else f"{PANEL_KEY}:{int(channel_id)}"


def panel_embed() -> discord.Embed:
    embed = discord.Embed(title=PANEL_TITLE, description=PANEL_TEXT, color=PANEL_COLOUR)
    embed.set_footer(text=PANEL_FOOTER)
    return embed


# -- the buttons ----------------------------------------------------------------------

class HubView(ui.GameView):
    """The buttons of the panel: permanent and stateless (one instance registered at start-up)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Polowanie", emoji="⚔️", style=discord.ButtonStyle.primary, row=0,
                       custom_id=HUNT_ID)
    async def hunt(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await hunt_clicked(interaction)

    @discord.ui.button(label="Wieża", emoji="🗼", style=discord.ButtonStyle.primary, row=0,
                       custom_id=TOWER_ID)
    async def tower(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await tower_clicked(interaction)

    @discord.ui.button(label="Towarzysz", emoji="🐾", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=PET_ID)
    async def pet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_privately(interaction, show_pet)

    @discord.ui.button(label="Stajnia", emoji="🐴", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=STABLE_ID)
    async def stable(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_privately(interaction, show_stable)

    @discord.ui.button(label="Klejnoty", emoji="💎", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=GEMS_ID)
    async def gems(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_privately(interaction, show_gems)

    @discord.ui.button(label="Rankingi", emoji="🏆", style=discord.ButtonStyle.secondary, row=2,
                       custom_id=RANKINGS_ID)
    async def rankings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_privately(interaction, show_rankings)

    @discord.ui.button(label="Pomoc", emoji="❓", style=discord.ButtonStyle.secondary, row=2,
                       custom_id=HELP_ID)
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_privately(interaction, show_help)


def _private_refusal(interaction: Any) -> Callable[[str], Awaitable[Any]]:
    async def refuse(text: str) -> None:
        await ui.reply(interaction, text)

    return refuse


async def hunt_clicked(interaction: Any) -> bool:
    """"Polowanie": ``$polowanie`` for the clicker; True when a hunt took place (minutes later)."""
    from . import daily

    # The busy flag is checked by start_hunt itself: during a gathering hunt a click joins it.
    if not await ui.gate(interaction, unless_busy=False):
        return False
    scene = ui.scene_of(interaction)
    if daily.is_gathering(scene.channel) and not daily.is_hunting(scene.channel, scene.author):
        # Queue first, acknowledge second: the click counts from the moment it was made,
        # like a typed $polowanie or the button under the monster.
        acked = asyncio.ensure_future(ui.ack(interaction))

        async def answer(text: str) -> None:
            await asyncio.wait({acked})
            await ui.reply(interaction, text)

        if daily.request_join(scene.channel, scene.author, answer):
            await asyncio.wait({acked})
            log.info("Hub: %s asks to join the gathering hunt.", interaction.user.id)
            return False
        await asyncio.wait({acked})
    elif not await ui.ack(interaction):
        return False  # Discord gave the click up: the player saw "interaction failed"
    log.info("Hub: hunt clicked by %s.", interaction.user.id)
    return await _guarded(interaction, lambda: daily.start_hunt(ui.scene_of(interaction),
                                                                refuse=_private_refusal(interaction)))


async def tower_clicked(interaction: Any) -> bool:
    """"Wieża": ``$wieża`` for the clicker; True when a run took place (minutes later)."""
    from . import tower

    if not await ui.gate(interaction, unless_busy=False):  # start_tower answers "busy" itself
        return False
    if not await ui.ack(interaction):
        return False  # Discord gave the click up: the player saw "interaction failed"
    log.info("Hub: tower clicked by %s.", interaction.user.id)
    return await _guarded(interaction, lambda: tower.start_tower(ui.scene_of(interaction),
                                                                 refuse=_private_refusal(interaction)))


def display_blocked_while_busy(bot: "AlterBot") -> bool:
    """Whether display-only buttons wait for a free channel (as ``guards.unless_busy(read_only=True)``)."""
    return getattr(getattr(bot, "settings", None), "busy_policy", "notify") != "read_only_free"


async def show_privately(interaction: Any, show: Callable[["Scene"], Awaitable[Any]]) -> bool:
    """A display button: run ``show`` on a scene that answers only the clicker.  True when shown."""
    if not await ui.gate(interaction, boss_channel=False, players_only=False,
                         unless_busy=display_blocked_while_busy(interaction.client)):
        return False
    if not await ui.ack(interaction):  # the database may take a moment; the answers follow privately
        return False  # an expired click cannot be answered any more
    await _guarded(interaction, lambda: show(ui.whisper_scene(interaction)))
    return True


async def _guarded(interaction: Any, run: Callable[[], Awaitable[Any]]) -> Any:
    """Run a click's work; a database outage is answered like the commands answer it.

    Any other error goes on to :meth:`ui.GameView.on_error` (answered, logged,
    reported to the owner).
    """
    try:
        return await run()
    except DatabaseUnavailable as exc:
        log.error("Hub click of %s failed: database unavailable (%s).", interaction.user.id, exc)
        await ui.reply(interaction, MSG_DB_DOWN)
        return None


# -- what the display buttons show ----------------------------------------------------

async def show_pet(scene: "Scene") -> Any:
    from . import pets

    return await pets.show_pet(scene, scene.author)


async def show_stable(scene: "Scene") -> Any:
    from . import pets

    return await pets.check_stable(scene)


async def show_gems(scene: "Scene") -> Any:
    from . import skills

    return await skills.show_skills(scene)


async def show_rankings(scene: "Scene") -> None:
    """The season's hunters, the Death Tower and the most talented pets (three answers)."""
    from . import database, pets, tower

    await database.read_ranking_table(scene)
    await tower.tower_ranking(scene)
    await pets.pet_ranking(scene)


async def show_help(scene: "Scene") -> None:
    from ..cogs import help as help_cog

    await scene.send(embed=help_cog.help_embed())


# -- posting the panel ----------------------------------------------------------------

async def _pin(message: Any) -> bool:
    """Pin ``message``; False when Discord refused.  Never raises."""
    try:
        await message.pin(reason="AlterMMO RPG: panel Centrum gry")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - no permission / 50 pins: the owner is told
        log.warning("Could not pin the hub panel (%s: %s).", type(exc).__name__, exc)
        return False


async def _drop_pin_notice(channel: Any, message: Any) -> None:
    """Delete Discord's "the bot pinned a message" line right away.  Never raises."""
    try:
        async for recent in channel.history(limit=5):
            if getattr(recent, "type", None) is not discord.MessageType.pins_add:
                continue
            reference = getattr(recent, "reference", None)
            if reference is None or getattr(reference, "message_id", None) == message.id:
                await io.safe_delete(recent)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - cosmetic; the channel purge removes it anyway
        log.debug("Could not remove the pin notice of the hub panel.", exc_info=True)


async def _remove(bot: "AlterBot", where: Any) -> bool:
    """Unpin and delete a panel remembered as ``{"channel": id, "message": id}``.  Never raises.

    False when there was nothing to remove (deleted by hand, unknown channel).
    """
    try:
        channel = bot.get_channel(int(where["channel"]))
        message_id = int(where["message"])
    except (TypeError, ValueError, KeyError):
        return False
    if channel is None:
        return False
    try:
        message = await channel.fetch_message(message_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - usually NotFound: removed by hand already
        log.info("Previous hub panel %s is gone already (%s).", message_id, type(exc).__name__)
        return False
    try:
        await message.unpin(reason="AlterMMO RPG: nowy panel Centrum gry")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - deleting it below unpins it as well
        log.debug("Could not unpin the previous hub panel.", exc_info=True)
    await io.safe_delete(message)
    return True


async def post_panel(bot: "AlterBot", channel: Any) -> tuple[Any, bool]:
    """Post the panel on ``channel``, pin it and remove the previous one.

    Returns ``(message, pinned)``.  Posting may raise like :func:`io.send`;
    the previous panel is left alone then.
    """
    key = panel_key(bot, channel.id)
    previous = bot.store.runtime_get(key)
    view = HubView()
    try:
        message = await io.send(channel, embed=panel_embed(), view=view)
    finally:
        # Clicks are answered by the view registered at start-up (same custom_ids);
        # discord.py would otherwise keep one more view per panel for ever.
        view.stop()
    pinned = await _pin(message)
    if pinned:
        await _drop_pin_notice(channel, message)
    bot.store.runtime_set(key, {"channel": int(channel.id), "message": int(message.id)})
    if isinstance(previous, dict) and previous.get("message") != message.id:
        await _remove(bot, previous)
    log.info("Hub panel posted on %s (message %s, pinned=%s).", channel.id, message.id, pinned)
    return message, pinned


def register(bot: "AlterBot") -> bool:
    """Make the buttons of panels posted before a restart work (call once, from ``cog_load``)."""
    return ui.register(bot, HubView())
