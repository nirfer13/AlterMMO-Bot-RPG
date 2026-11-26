"""The pinned "Panel gry" (first called "Centrum gry"): the game's front door on the boss channel.

An administrator posts it with ``!panel`` (see ``altermmo/cogs/hub.py``); the
bot pins it, so the regular purge of the boss channel
(:func:`altermmo.core.io.clear_channel` keeps pinned messages) never removes
it.  Posting it again replaces the previous panel: its id is remembered in
``runtime.json`` (:func:`panel_key`, one entry per channel, so a panel tried
out on the test channel never removes the real one) and the old message is
deleted - a panel somebody already removed by hand is simply skipped.

Its text has two parts: how the game is played (:data:`PANEL_GAME_TEXT`) and,
since 2026-11-20, what supporters get on top and where one becomes a
supporter (:data:`PANEL_SUPPORT_TEXT`: the supporter roles of
:mod:`altermmo.features.patrons` as role mentions, the benefits, the links).
A panel that is already pinned gets the current text by itself after the next
start of the bot (:func:`refresh_panel`); nobody has to type ``!panel`` again.

The buttons are *permanent* (fixed ``custom_id``\\ s ``altermmo:hub:...``, no
timeout, no state in the view): the cog registers one :class:`HubView` at
start-up, so the panel keeps working after every restart.

Two kinds of buttons:

* **gameplay** - "Polowanie" and "Wieża" run exactly what ``!polowanie`` /
  ``!wieża`` run (:func:`altermmo.features.daily.start_hunt`,
  :func:`altermmo.features.tower.start_tower`) for the member who clicked.
  Boss channel only, never for dead hunters (:func:`ui.gate`); the busy flag
  is checked by the commands' own bodies, so a click during a hunt that is
  still gathering hunters JOINS it, exactly like typing ``!polowanie``.
  Refusals (cooldown, a boss on the way, busy...) are answered with
  :func:`ui.reply`; the hunt or the tower run itself is public, as when typed.
  "Zlecenia" (2026-11-25, the companion missions) shows what ``!zlecenia``
  shows - the player's mission board, its progress or its result
  (:func:`altermmo.features.missions.show_board`): on the boss channel only,
  but with the gate of a display button (:func:`display_gate`) - it only
  shows the board; what changes the game (sending a pet, a new board, the
  recall) is gated by the board's own buttons.
* **display** - "Towarzysz", "Stajnia", "Klejnoty", "Przedmioty", "Rankingi", "Osiągnięcia",
  "Album", "Pomoc" show exactly what ``!towarzysz``, ``!stajnia``, ``!klejnoty``, ``!przedmioty``
  (2026-11-26, features/items.py: also for a player without a pet),
  the ranking commands (the fourth, ``!rankingalbumow``, only for a supporter - a member
  who does not support gets the three open ones), ``!osiagniecia``, ``!album`` and ``!pomoc`` show
  (:func:`display_gate` is also the gate of the album's own buttons): on the
  channel, for everybody, with the same buttons and menus under them (owner's
  decision of 2026-11-20: nothing is visible "only to you").  They follow the busy policy of the display commands
  (``guards.unless_busy(read_only=True)``).  A member who may not write on
  the channel (a dead hunter) cannot make the bot post there for him.
  "Pomoc" shares the cooldown of ``!pomoc``.
* **notifications** - "Powiadomienia" (owner's request of 2026-11-22, a row of
  its own) switches the hunters' role (``bot.ids.hunters_role``, "Łowca
  Potworów": the role the bot pings for bosses, events and patch notes) on
  and off for the clicker (:func:`notify_clicked`).  It is not gameplay, but
  it posts an answer, so it goes through :func:`display_gate` like the display
  buttons: refused while the channel is busy (busy policy), while a fight is
  typed (then not even the refusal is posted and NOTHING changes - a role
  switched silently would leave the player guessing, and a second blind click
  would switch it back), and for a dead hunter on the boss channel (he cannot
  make the bot post there; after the next boss he can click again).

Every click is acknowledged at once; the fights themselves stay typed.  A
click Discord has already given up on (the bot lagged for more than three
seconds - the player was told "interaction failed") starts nothing: no hunt
and no tower run is spent behind the player's back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import types
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from ..core import challenge, io, ui
from ..core.db import DatabaseUnavailable
from . import patrons

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
#: The player items (2026-11-26, ``!przedmioty``); permanent like the others - it must never change.
ITEMS_ID = BUTTON_PREFIX + "items"
RANKINGS_ID = BUTTON_PREFIX + "rankings"
ACHIEVEMENTS_ID = BUTTON_PREFIX + "achievements"
ALBUM_ID = BUTTON_PREFIX + "album"
#: The companion missions (2026-11-25); permanent like the others - it must never change.
MISSIONS_ID = BUTTON_PREFIX + "missions"
HELP_ID = BUTTON_PREFIX + "help"
NOTIFY_ID = BUTTON_PREFIX + "notify"

PANEL_TITLE = "🎮 Panel gry"
#: What the buttons do and how the game is played.
PANEL_GAME_TEXT = (
    "Witaj, łowco! Stąd ruszysz na przygodę jednym kliknięciem. Wpisywane komendy działają tak jak "
    "dotąd - ich listę znajdziesz pod przyciskiem **Pomoc**.\n\n"
    "⚔️ **Polowanie** - zapoluj na zwykłe stwory (raz dziennie, modlitwa w kapliczce może to odnowić). "
    "Inni łowcy dołączą przyciskiem pod potworem.\n"
    "🏰 **Wieża** - wyrusz na eksplorację Wieży Śmierci (raz w tygodniu). Im wyżej, tym trudniej!\n"
    "🧭 **Zlecenia** - raz dziennie wyślij towarzysza na zlecenie z tablicy: szansę powodzenia poznasz przed "
    "wysłaniem, a im trudniejsze i dłuższe zlecenie, tym lepszy łup.\n"
    "🐾 **Towarzysz**, 🐴 **Stajnia**, 💎 **Klejnoty** - podejrzyj swojego towarzysza, stajnię "
    "i klejnoty umiejętności.\n"
    "🎒 **Przedmioty** - wszystko, co nosisz w sakwie, w jednym miejscu (także zanim zdobędziesz "
    "towarzysza).\n"
    "🏆 **Rankingi** - sezonowy ranking łowców, Wieża Śmierci, najbardziej utalentowani towarzysze "
    "i ranking kolekcji albumów (dla wspierających).\n"
    "🏅 **Osiągnięcia** - Twoje kolekcjonerskie osiągnięcia (dla wspierających).\n"
    "📖 **Album** - kolekcja Twoich poświęconych towarzyszy (dla wspierających).\n"
    "❓ **Pomoc** - wszystkie komendy gry.\n"
    "🔔 **Powiadomienia** - kliknij, żeby dostać rolę **Łowca Potworów** i wiedzieć o bossach, "
    "wydarzeniach i nowościach w grze; kolejne kliknięcie ją zdejmuje.\n\n"
    "👹 **Boss** pojawia się na tym kanale co kilkanaście minut do kilkunastu godzin - kliknij "
    "**Zaatakuj** pod jego obrazkiem albo wpisz **!zaatakuj**.\n"
    "⌨️ **Walki** toczą się na klawiaturze: wpisz podane słowo, zanim minie czas! "
    "<:PepoG:790963160528977980>"
)
#: What supporters get, in player language: one line per benefit, each one checked against the
#: code that grants it (every place that asks ``patrons.check_if_patron`` / ``patrons.get_patron``):
#: ``pets.PATRON_EGGS``; ``pets.transform_pet``; the shrine in ``cogs/events.py``;
#: ``boss.send_boss_image`` and ``events.spawn_party``; ``achievements.py`` and the
#: mythic slayer role in ``boss.py``; the seals and the Holo foil rolled for a new pet in ``pets.py``
#: (stored with the pet for good, shown only while its owner supports); the album of sacrificed pets
#: (``album.py``, ``pets.discard_pet``) - its line names no species and no ornament either.
#: (The album's note at a new egg, ``album.note_new_egg`` of 2026-11-23, is part of the album and
#: has no line of its own here: the panel's text was not changed for it.)
#: (The silver / gold frames of shiny pets are for everybody, so they are not listed.)
#: Owner's decision of 2026-11-20: the panel does not name WHICH species supporters can find nor
#: WHICH card ornaments exist (seals, Holo) - players discover them.
PANEL_BENEFITS: tuple[str, ...] = (
    # (Owner, 2026-11-22: "Jaja premium też dopisz, że to element kosmetyczny." True: what a
    # talent point gives depends on the pet's class - pets._GAINS - never on its species.)
    "🥚 **Jaja premium** - z jaj wykluwają się też rzadsze gatunki towarzyszy. **To tylko element "
    "kosmetyczny**: inny wygląd, te same statystyki.",
    "🪞 **Transformacja** - zmiana wyglądu towarzysza lustrem wizualiów (**!transformacja**).",
    "🕯️ **Modlitwa w kapliczce** - pomodlić się mogą tylko wspierający (**!modlitwa**), a korzystają "
    "wszyscy.",
    "👹 **Rola bossa** - szansa, że to Ty zostaniesz bossem (i zgarniesz doświadczenie za pokonanego "
    "łowcę) albo bohaterem imprezy u Barda.",
    "🏅 **Kolekcjonerskie osiągnięcia** - role za najrzadsze, mityczne zdobycze. Swoje sprawdzisz "
    "przyciskiem **Osiągnięcia** (**!osiagniecia**).",
    # (Owner, 2026-11-22: "W Panelu gry podkreśl, że są to tylko elementy kosmetyczne.")
    "🔖 **Kolekcjonerskie ozdoby karty** - **wyłącznie elementy kosmetyczne**, bez wpływu na grę. "
    "Każdy nowy towarzysz ma na nie szansę; zdobią kartę, dopóki wspierasz (po przerwie wracają).",
    # (2026-11-23: the rank of the ranking's first place, features/collector.py - no number, no
    # ornament is named.)
    "📖 **Album** - poświęceni towarzysze zostają w Twoim albumie (**!album**) i liczą się w rankingu "
    "kolekcji; jego pierwsze miejsce nosi rangę **Kolekcjoner Pupili** (dodatkowa szansa w giveawayach).",
)
#: Where to become a supporter: markdown links (they work in an embed description), the
#: recommended way first and in bold.
PANEL_LINKS = " • ".join(
    f"**[{label}]({url})** (polecane)" if url == patrons.PATRONITE_URL else f"[{label}]({url})"
    for label, url in patrons.SUPPORT_LINKS)
#: The supporters' section.  The roles are role mentions: inside an embed they show the names
#: the server gives them and notify nobody.
PANEL_SUPPORT_TEXT = (
    f"💜 **Dodatkowe benefity dla** {patrons.supporter_mentions()}:\n"
    + "\n".join(PANEL_BENEFITS)
    + f"\n\n**Wesprzyj AlterMMO:** {PANEL_LINKS}"
)
#: The whole description of the panel (Discord's limit: 4096 characters; ``tests/test_ui_hub.py``).
PANEL_TEXT = PANEL_GAME_TEXT + "\n\n" + PANEL_SUPPORT_TEXT
PANEL_FOOTER = "Polowanie, Wieża i Zlecenia działają tylko na tym kanale i nie wtedy, gdy trwa inne wydarzenie."
PANEL_COLOUR = 0x42F34C

MSG_DB_DOWN = "Baza danych jest chwilowo niedostępna. Spróbuj ponownie za moment <:Sadge:936907659142111273>"

# Answers of "Powiadomienia" (the role's name comes from the server; this one when it cannot be read).
NOTIFY_ROLE_NAME = "Łowca Potworów"
MSG_NOTIFY_ON = ("🔔 Od teraz dostajesz powiadomienia o grze (rola **{role}**). Kliknij **Powiadomienia** "
                 "jeszcze raz, żeby je wyłączyć.")
MSG_NOTIFY_OFF = ("🔕 Nie dostajesz już powiadomień o grze (rola **{role}** zdjęta). Kliknij **Powiadomienia**, "
                  "żeby wrócić.")
MSG_NOTIFY_FAILED = ("Nie udało się zmienić Twoich powiadomień o grze <:Sadge:936907659142111273> "
                     "Administracja już wie o problemie.")
MSG_NOTIFY_RETRY = ("Discord nie odpowiedział na czas, więc Twoje powiadomienia zostały bez zmian. "
                    "Spróbuj ponownie za chwilę <:Sadge:936907659142111273>")
# What the owner is told (once per problem per :data:`NOTIFY_REPORT_GAP`).
MSG_OWNER_NOTIFY_FORBIDDEN = ("Przycisk **Powiadomienia**: Discord nie pozwolił {action} roli **{role}** (`{role_id}`) "
                              "graczowi {player} (`{error}`). Bot potrzebuje uprawnienia **Zarządzanie rolami**, "
                              "a jego najwyższa rola musi stać nad tą rolą.")
MSG_OWNER_NOTIFY_NO_ROLE = ("Przycisk **Powiadomienia**: roli Łowców Potworów (`{role_id}`) nie ma na serwerze "
                            "(usunięta?). Ustaw właściwe id `hunters_role`.")
MSG_OWNER_NOTIFY_GONE = ("Przycisk **Powiadomienia**: Discord nie zna już gracza {player} (`{error}`) - "
                         "opuścił serwer zaraz po kliknięciu?")
MSG_OWNER_NOTIFY_ERROR = "Przycisk **Powiadomienia** ({player}): nieoczekiwany błąd `{error}`."
#: The same problem of the notifications button reaches the owner at most once per this many
#: seconds: every player who clicks would otherwise report a missing permission again.
NOTIFY_REPORT_GAP = 600.0
#: Longest wait for one Discord call of the toggle (the click is acknowledged before; retried twice).
NOTIFY_ATTEMPT_TIMEOUT = 15.0
#: Pause before the first repetition of such a call (then three times longer).
NOTIFY_RETRY_DELAY = 0.5

# Answers of ``!panel`` (for the administrator).
MSG_POSTED = "**Panel gry** jest na kanale <#{channel_id}> i został przypięty."
MSG_POSTED_NOT_PINNED = ("**Panel gry** jest na kanale <#{channel_id}>, ale nie udało się go przypiąć "
                         "(bot nie ma uprawnienia **Przypinanie wiadomości** albo kanał ma już 50 przypiętych "
                         "wiadomości?). Kanał jest regularnie czyszczony - nadaj uprawnienie i wpisz **!panel** "
                         "jeszcze raz albo przypnij panel ręcznie.")
MSG_NOT_POSTED = "Nie udało się opublikować **Panelu gry** <:Sadge:936907659142111273>"


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

    @discord.ui.button(label="Wieża", emoji="🏰", style=discord.ButtonStyle.primary, row=0,
                       custom_id=TOWER_ID)
    async def tower(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await tower_clicked(interaction)

    @discord.ui.button(label="Zlecenia", emoji="🧭", style=discord.ButtonStyle.primary, row=0,
                       custom_id=MISSIONS_ID)
    async def missions(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await missions_clicked(interaction)

    @discord.ui.button(label="Towarzysz", emoji="🐾", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=PET_ID)
    async def pet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_display(interaction, show_pet)

    @discord.ui.button(label="Stajnia", emoji="🐴", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=STABLE_ID)
    async def stable(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # (stable=True: the stable is also shown during the sign-up of the "Trening towarzyszy" event.)
        await show_display(interaction, show_stable, stable=True)

    @discord.ui.button(label="Klejnoty", emoji="💎", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=GEMS_ID)
    async def gems(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_display(interaction, show_gems)

    @discord.ui.button(label="Przedmioty", emoji="🎒", style=discord.ButtonStyle.secondary, row=1,
                       custom_id=ITEMS_ID)
    async def items(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_display(interaction, show_items)

    @discord.ui.button(label="Rankingi", emoji="🏆", style=discord.ButtonStyle.secondary, row=2,
                       custom_id=RANKINGS_ID)
    async def rankings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_display(interaction, show_rankings)

    @discord.ui.button(label="Osiągnięcia", emoji="🏅", style=discord.ButtonStyle.secondary, row=2,
                       custom_id=ACHIEVEMENTS_ID)
    async def achievements(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_display(interaction, show_achievements)

    @discord.ui.button(label="Album", emoji="📖", style=discord.ButtonStyle.secondary, row=2,
                       custom_id=ALBUM_ID)
    async def album(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_display(interaction, show_album)

    @discord.ui.button(label="Pomoc", emoji="❓", style=discord.ButtonStyle.secondary, row=2,
                       custom_id=HELP_ID)
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await help_clicked(interaction)

    @discord.ui.button(label="Powiadomienia", emoji="🔔", style=discord.ButtonStyle.secondary, row=3,
                       custom_id=NOTIFY_ID)
    async def notifications(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await notify_clicked(interaction)


def _refusal(interaction: Any) -> Callable[[str], Awaitable[Any]]:
    async def refuse(text: str) -> None:
        await ui.reply(interaction, text)

    return refuse


async def hunt_clicked(interaction: Any) -> bool:
    """"Polowanie": ``!polowanie`` for the clicker; True when a hunt took place (minutes later)."""
    from . import daily

    # The busy flag is checked by start_hunt itself: during a gathering hunt a click joins it.
    if not await ui.gate(interaction, unless_busy=False):
        return False
    scene = ui.scene_of(interaction)
    if daily.is_gathering(scene.channel) and not daily.is_hunting(scene.channel, scene.author):
        # Queue first, acknowledge second: the click counts from the moment it was made,
        # like a typed !polowanie or the button under the monster.
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
                                                                refuse=_refusal(interaction)))


async def tower_clicked(interaction: Any) -> bool:
    """"Wieża": ``!wieża`` for the clicker; True when a run took place (minutes later)."""
    from . import tower

    if not await ui.gate(interaction, unless_busy=False):  # start_tower answers "busy" itself
        return False
    if not await ui.ack(interaction):
        return False  # Discord gave the click up: the player saw "interaction failed"
    log.info("Hub: tower clicked by %s.", interaction.user.id)
    return await _guarded(interaction, lambda: tower.start_tower(ui.scene_of(interaction),
                                                                 refuse=_refusal(interaction)))


async def missions_clicked(interaction: Any, *, seen: int = 0) -> bool:
    """"Zlecenia": the player's mission board (or its progress / result) on the boss channel.

    ``seen``: the mission whose result post the button was under (``shortcuts.MissionsButton``):
    the player has read that result, so the board does not show it again.
    """
    from . import missions

    if not await display_gate(interaction, stable=True, boss_channel=True):
        return False
    if not await ui.ack(interaction):
        return False  # Discord gave the click up: the player saw "interaction failed"
    log.info("Hub: missions clicked by %s.", interaction.user.id)
    return bool(await _guarded(interaction, lambda: missions.show_board(ui.scene_of(interaction), seen=seen)))


def display_blocked_while_busy(bot: "AlterBot") -> bool:
    """Whether display-only buttons wait for a free channel (as ``guards.unless_busy(read_only=True)``)."""
    return getattr(getattr(bot, "settings", None), "busy_policy", "notify") != "read_only_free"


async def show_display(interaction: Any, show: Callable[["Scene"], Awaitable[Any]], *,
                       stable: bool = False) -> bool:
    """A display button: run ``show`` for the clicker, on the channel, like the typed command.

    ``stable``: the button shows the stable (see :func:`display_gate`).  True when shown.
    """
    if not await display_gate(interaction, stable=stable):
        return False
    if not await ui.ack(interaction):  # the database may take a moment; the answer follows on the channel
        return False  # an expired click: the player saw "interaction failed", nothing is posted
    await _guarded(interaction, lambda: show(ui.scene_of(interaction)))
    return True


async def display_gate(interaction: Any, *, stable: bool = False, boss_channel: bool = False) -> bool:
    """May a display button show something now?  Refusals are answered here.

    Also the gate of the buttons under the album (features/album.py) and of
    the mission board's buttons that only change the view (features/missions.py):
    they only show something, and must not make a fight stutter either.
    ``stable``: the button of the stable - it also works while the busy holder
    keeps the stable open (the sign-up of the "Trening towarzyszy" event), as
    ``!stajnia`` does.  ``boss_channel``: only on the boss channel (the missions).
    """
    if not await ui.gate(interaction, boss_channel=boss_channel,
                         unless_busy=display_blocked_while_busy(interaction.client), stable=stable):
        return False
    if challenge.fight_on(interaction.channel):
        # (Also under the "read_only_free" policy.)  A boss fight does not hold the busy flag,
        # but a pet card or three rankings between its prompts would be exactly the stutter
        # the owner does not want.  During a fight this answer itself is swallowed.
        await ui.reply(interaction, ui.MSG_BUSY)
        return False
    return True


def _help_bucket(bot: "AlterBot", member: Any) -> Any:
    """The ``!pomoc`` cooldown of ``member`` (shared by the command and the button); None without one."""
    command = bot.get_command("pomoc") if hasattr(bot, "get_command") else None
    buckets = getattr(command, "_buckets", None)
    if buckets is None or not getattr(buckets, "valid", False):
        return None
    try:
        return buckets.get_bucket(types.SimpleNamespace(author=member, guild=getattr(member, "guild", None)))
    except Exception:  # noqa: BLE001 - a cooldown that cannot be read is no reason to fail the click
        log.exception("Could not read the !pomoc cooldown of %s.", getattr(member, "id", member))
        return None


async def help_clicked(interaction: Any) -> bool:
    """"Pomoc": the help, as ``!pomoc`` posts it - with the same cooldown (one per player per 10 minutes).

    The help is a long embed everybody sees: without the cooldown one player
    could fill the channel with it by clicking.
    """
    from ..bot import cooldown_text

    if not await display_gate(interaction):
        return False
    bucket = _help_bucket(interaction.client, interaction.user)
    if bucket is not None:
        retry_after = bucket.update_rate_limit()  # (no await since the gate: charged exactly once)
        if retry_after:
            await ui.reply(interaction, cooldown_text(retry_after))
            return False
    if not await ui.ack(interaction):
        return False
    await _guarded(interaction, lambda: show_help(ui.scene_of(interaction)))
    return True


# -- "Powiadomienia": the hunters' role on / off ----------------------------------------

#: Per player: ``[lock, how many clicks use it]`` - his clicks on "Powiadomienia" run one after another.
_notify_locks: dict[int, list[Any]] = {}
#: Problem of the notifications button -> when the owner was last told about it (``time.monotonic``).
_notify_reported: dict[str, float] = {}


@contextlib.asynccontextmanager
async def _one_at_a_time(player_id: int):
    """Serialise the toggles of one player (two quick clicks: on, then off - never both "on")."""
    entry = _notify_locks.setdefault(player_id, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0 and _notify_locks.get(player_id) is entry:
            del _notify_locks[player_id]


async def _tell_owner_once(bot: Any, problem: str, text: str) -> None:
    """``bot.log_to_owner(text)`` unless the same ``problem`` was reported less than 10 minutes ago."""
    now = time.monotonic()
    last = _notify_reported.get(problem)
    if last is not None and now - last < NOTIFY_REPORT_GAP:
        log.info("Notifications button: %s again (the owner was told %.0f s ago).", problem, now - last)
        return
    _notify_reported[problem] = now
    report = getattr(bot, "log_to_owner", None)
    if report is not None:
        await report(text)


def _player(interaction: Any) -> str:
    """The clicker for the owner's log: name and id, no mention (nobody is pinged in the log)."""
    user = interaction.user
    return f"{user} ({getattr(user, 'id', '?')})"


async def _notify_failed(interaction: Any, problem: str, owner_text: str) -> None:
    await ui.reply(interaction, MSG_NOTIFY_FAILED)
    await _tell_owner_once(interaction.client, problem, owner_text)


async def notify_clicked(interaction: Any) -> bool | None:
    """"Powiadomienia": give the clicker the hunters' role, or take it away when he has it.

    Returns True when he holds the role now, False when it was taken away,
    None when nothing changed (refused, expired click, a failure - always answered).

    * The member's CURRENT roles decide, read from Discord's API after the
      acknowledgement (``guild.fetch_member``): ``interaction.user`` is the
      member as he was when he clicked, and the cache learns about a role
      change only when the gateway reports it - a second quick click could
      otherwise see the role "still missing" and give it twice.
    * One player's clicks run one after another (:func:`_one_at_a_time`), so
      two quick clicks end in a consistent state with two true answers (on,
      off); the answers are posted even when the same one was on the channel
      seconds ago (``ui.reply(repeat=True)``).
    * Anybody who may click the panel may use it: it is not a supporters'
      benefit and not gameplay (the gate is :func:`display_gate`, see the
      module docstring).
    * A role that cannot be changed (no permission, the role above the bot's,
      the role deleted, the member gone) is answered in Polish and reported
      to the owner once per :data:`NOTIFY_REPORT_GAP`; a network hiccup asks
      the player to try again.  Never raises into the view.
    """
    if not await display_gate(interaction):
        return None
    if not await ui.ack(interaction):  # within the 3 s; the role calls may take longer
        return None  # an expired click: the player saw "interaction failed", nothing changes
    try:
        async with _one_at_a_time(int(interaction.user.id)):
            return await _toggle_hunters_role(interaction)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the click is answered, the owner told
        log.exception("Notifications button of %s failed.", getattr(interaction.user, "id", "?"))
        await _notify_failed(interaction, f"error:{type(exc).__name__}", MSG_OWNER_NOTIFY_ERROR.format(
            player=_player(interaction), error=f"{type(exc).__name__}: {exc}"))
        return None


async def _toggle_hunters_role(interaction: Any) -> bool | None:
    """The work of :func:`notify_clicked` (under the player's lock, the click acknowledged)."""
    bot = interaction.client
    role_id = int(bot.ids.hunters_role)
    guild = getattr(interaction, "guild", None) or bot.get_guild(bot.ids.guild)
    role = guild.get_role(role_id) if guild is not None else None
    if role is None:
        await _notify_failed(interaction, "no-role", MSG_OWNER_NOTIFY_NO_ROLE.format(role_id=role_id))
        return None
    user_id = int(interaction.user.id)
    try:
        member = await io.retry(lambda: guild.fetch_member(user_id), what="fetch_member",
                                attempt_timeout=NOTIFY_ATTEMPT_TIMEOUT, first_delay=NOTIFY_RETRY_DELAY)
    except asyncio.CancelledError:
        raise
    except discord.NotFound as exc:
        await _notify_failed(interaction, "member-gone", MSG_OWNER_NOTIFY_GONE.format(
            player=_player(interaction), error=f"{type(exc).__name__}: {exc}"))
        return None
    except Exception as exc:  # noqa: BLE001
        if not io.is_transient(exc):
            raise
        log.warning("Notifications button: member %s not fetched (%s: %s).", user_id, type(exc).__name__, exc)
        await ui.reply(interaction, MSG_NOTIFY_RETRY)
        return None

    had = any(getattr(held, "id", None) == role_id for held in getattr(member, "roles", ()))
    reason = "AlterMMO RPG: przycisk Powiadomienia"
    try:
        if had:
            await io.retry(lambda: member.remove_roles(role, reason=reason), what="remove_roles",
                           attempt_timeout=NOTIFY_ATTEMPT_TIMEOUT, first_delay=NOTIFY_RETRY_DELAY)
        else:
            await io.retry(lambda: member.add_roles(role, reason=reason), what="add_roles",
                           attempt_timeout=NOTIFY_ATTEMPT_TIMEOUT, first_delay=NOTIFY_RETRY_DELAY)
    except asyncio.CancelledError:
        raise
    except discord.Forbidden as exc:
        await _notify_failed(interaction, "forbidden", MSG_OWNER_NOTIFY_FORBIDDEN.format(
            action="zdjąć" if had else "nadać", role=role.name, role_id=role_id,
            player=_player(interaction), error=f"{type(exc).__name__}: {exc}"))
        return None
    except discord.NotFound as exc:
        if getattr(exc, "code", None) == 10011:  # Unknown Role: deleted a moment ago
            await _notify_failed(interaction, "no-role", MSG_OWNER_NOTIFY_NO_ROLE.format(role_id=role_id))
        else:  # Unknown Member (10007): left the server
            await _notify_failed(interaction, "member-gone", MSG_OWNER_NOTIFY_GONE.format(
                player=_player(interaction), error=f"{type(exc).__name__}: {exc}"))
        return None
    except Exception as exc:  # noqa: BLE001
        if not io.is_transient(exc):
            raise
        log.warning("Notifications button: role of %s not changed (%s: %s).", user_id, type(exc).__name__, exc)
        await ui.reply(interaction, MSG_NOTIFY_RETRY)
        return None

    name = getattr(role, "name", None) or NOTIFY_ROLE_NAME
    log.info("Hub: hunters' role %s for %s.", "taken from" if had else "given to", user_id)
    await ui.reply(interaction, (MSG_NOTIFY_OFF if had else MSG_NOTIFY_ON).format(role=name), repeat=True)
    return not had


async def refresh_panel(bot: "AlterBot") -> bool:
    """Bring the pinned panel on the boss channel up to date (text and buttons).  Never raises.

    Called once after a start (``BossCycle.tidy_after_start``): a panel posted
    by an older version of the code keeps its old text for ever otherwise
    (pinned messages survive every clearing of the channel).  True when a
    panel was found and edited.
    """
    try:
        remembered = bot.store.runtime_get(panel_key(bot, bot.ids.boss_channel))
        if not isinstance(remembered, dict):
            return False
        channel = bot.get_channel(int(remembered.get("channel") or 0))
        if channel is None:
            return False
        message = await channel.fetch_message(int(remembered.get("message") or 0))
        view = HubView()
        try:
            await message.edit(embed=panel_embed(), view=view)
        finally:
            view.stop()  # clicks are answered by the view registered at start-up
        log.info("Hub panel %s brought up to date.", message.id)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. the panel was removed by hand
        log.warning("Hub panel not refreshed (%s: %s) - it keeps its old text until the next start "
                    "or !panel.", type(exc).__name__, exc)
        return False


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


async def show_items(scene: "Scene") -> Any:
    from . import items

    return await items.show_items(scene)


async def show_rankings(scene: "Scene") -> None:
    """The season's hunters, the Death Tower, the most talented pets (for everybody) and, for a
    supporter, the supporters' album collections (four answers).

    A member who does not support gets the three open rankings and nothing about the collection
    ranking: most players are not supporters, and a "this is for supporters" line with his ping
    under every click would only fill the channel (the typed ``!rankingalbumow`` still answers it).
    """
    from . import albumrank, database, pets, tower

    await database.read_ranking_table(scene)
    await tower.tower_ranking(scene)
    await pets.pet_ranking(scene)
    if scene.author is not None and patrons.check_if_patron(scene.author):
        await albumrank.show_ranking(scene)


async def show_achievements(scene: "Scene") -> Any:
    from . import achievements

    return await achievements.show_achievements(scene)


async def show_album(scene: "Scene") -> Any:
    from . import album

    return await album.show_album(scene)


async def show_help(scene: "Scene") -> None:
    from ..cogs import help as help_cog

    await scene.send(embed=help_cog.help_embed())


# -- posting the panel ----------------------------------------------------------------

async def _pin(message: Any) -> bool:
    """Pin ``message``; False when Discord refused.  Never raises."""
    try:
        await message.pin(reason="AlterMMO RPG: Panel gry")
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
        await message.unpin(reason="AlterMMO RPG: nowy Panel gry")
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
