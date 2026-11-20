"""Switching the "a boss is coming" warnings of a pet off and on again.

A pet that can sense bosses warns its owner in a private message
(:func:`altermmo.features.pets.detect_boss`).  A player with closed private
messages gets a public hint instead - every time a boss comes.  The hint now
carries a button that switches the warnings off for that player, and
``$wyczuwanie`` switches them on again.

Who switched the warnings off is kept in ``data/boss_detection_off.txt`` (one
id per line), so nothing changes in the database.

The button is a *persistent* one (fixed ``custom_id``, no timeout): it keeps
working on hints posted before a restart.  The cog registers the view once
with ``bot.add_view``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

import discord

from ..core import io, ui

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

BUTTON_ID = "altermmo:boss-detect-off"
BUTTON_LABEL = "Wyłącz powiadomienia o bossie"

MSG_UNLOCK_HINT = ("<@{player_id}> - Twój towarzysz: *Nadciąga boss! Odblokuj prywatne wiadomości, jeśli "
                   "chcesz otrzymywać powiadomienia!* Nie chcesz ich wcale? Kliknij przycisk poniżej.")
MSG_NOT_YOURS = ("Ten przycisk jest dla <@{player_id}>. Swoje powiadomienia o bossie wyłączysz i włączysz "
                 "komendą **$wyczuwanie**.")
MSG_SWITCHED_OFF = ("Gotowe - Twój towarzysz nie będzie już ostrzegał Cię o nadciągającym bossie. "
                    "Włączysz powiadomienia ponownie komendą **$wyczuwanie**.")
MSG_NOT_SAVED = ("Nie udało się tego zapisać <:Sadge:936907659142111273> Spróbuj za chwilę albo użyj "
                 "komendy **$wyczuwanie**.")
MSG_NOW_OFF = ("<@{player_id}>, powiadomienia o nadciągającym bossie są teraz **wyłączone**. "
               "Wpisz **$wyczuwanie** ponownie, żeby je włączyć.")
MSG_NOW_ON = ("<@{player_id}>, powiadomienia o nadciągającym bossie są teraz **włączone**. Dostaniesz je "
              "w prywatnej wiadomości, jeśli Twój aktywny towarzysz potrafi wyczuwać bossa.")

_FIRST_MENTION = re.compile(r"<@!?(\d{15,20})>")


# -- who switched the warnings off ----------------------------------------------------

def is_off(bot: "AlterBot", player_id: int | str) -> bool:
    """True when the player does not want boss warnings.  Never raises."""
    try:
        return bot.store.detect_off.contains(player_id)
    except OSError:
        log.exception("Could not read the boss warning switch of %s; warning them as usual.", player_id)
        return False


def switch_off(bot: "AlterBot", player_id: int | str) -> None:
    if not bot.store.detect_off.contains(player_id):
        bot.store.detect_off.add(player_id)
    log.info("Boss warnings switched off by %s.", player_id)


def switch_on(bot: "AlterBot", player_id: int | str) -> None:
    bot.store.detect_off.remove([player_id])
    log.info("Boss warnings switched on by %s.", player_id)


async def toggle(scene: "Scene") -> bool:
    """``$wyczuwanie``: flip the author's switch; returns True when warnings are on now."""
    bot = scene.bot
    player_id = scene.author.id
    if is_off(bot, player_id):
        switch_on(bot, player_id)
        await scene.send(MSG_NOW_ON.format(player_id=player_id))
        return True
    switch_off(bot, player_id)
    await scene.send(MSG_NOW_OFF.format(player_id=player_id))
    return False


# -- the button under the public hint -------------------------------------------------

def hinted_player(message: Any) -> int | None:
    """Id of the player a hint was written for (the first mention in its text)."""
    match = _FIRST_MENTION.search(str(getattr(message, "content", "") or ""))
    return int(match.group(1)) if match else None


class DetectionOffView(discord.ui.View):
    """The "switch the warnings off" button under the public hint."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label=BUTTON_LABEL, style=discord.ButtonStyle.secondary, emoji="🔕",
                       custom_id=BUTTON_ID)
    async def switch_off_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await handle_click(interaction)


async def handle_click(interaction: Any) -> None:
    """Only the player the hint was written for can use its button."""
    bot = interaction.client
    owner_id = hinted_player(interaction.message)
    clicker_id = interaction.user.id
    if owner_id is not None and owner_id != clicker_id:
        await ui.reply(interaction, MSG_NOT_YOURS.format(player_id=owner_id))
        return
    try:
        switch_off(bot, clicker_id)
    except Exception as exc:  # noqa: BLE001 - the click must be answered whatever the disk says
        log.exception("Could not switch the boss warnings of %s off.", clicker_id)
        await ui.reply(interaction, MSG_NOT_SAVED)
        await bot.log_to_owner(f"Nie udało się zapisać wyłączenia powiadomień o bossie gracza <@{clicker_id}>: "
                               f"`{type(exc).__name__}: {exc}`")
        return
    await ui.reply(interaction, MSG_SWITCHED_OFF)
    await io.safe_delete(interaction.message)  # the hint did its job


async def send_hint(bot: "AlterBot", channel: Any, player_id: int) -> None:
    """Post the public "unlock your private messages" hint with the button."""
    view = DetectionOffView()
    try:
        await io.send(channel, MSG_UNLOCK_HINT.format(player_id=player_id), view=view, attempts=1)
    finally:
        # discord.py would otherwise keep one view per hint for ever; clicks are
        # handled by the view registered once in register() (same custom_id).
        view.stop()


def register(bot: "AlterBot") -> None:
    """Make buttons under hints from before a restart work again (call once, in a running loop)."""
    try:
        bot.add_view(DetectionOffView())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - old hints lose their button, nothing else
        log.exception("Could not register the boss warning button.")
