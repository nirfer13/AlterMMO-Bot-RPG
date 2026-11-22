"""Switching the "a boss is coming" warnings of a pet off and on again.

A pet that can sense bosses warns its owner in a private message
(:func:`altermmo.features.pets.detect_boss`).  A player with closed private
messages gets a public hint instead - every time a boss comes.  The hint now
carries a button that switches the warnings off for that player, and
``$wyczuwanie`` switches them on again.

The private warning itself carries the same button (owner's request of
2026-11-21: "Pet gdy w prywatnej wiadomości ostrzega przed bossem powinien też
dawać możliwość anulowania takich powiadomień") and one line saying in words
what it does and that ``$wyczuwanie`` on the game's channel does the same
(the information is there also where buttons are not shown, e.g. in a push
notification).  No command works in private messages (the global check of
``bot.py``), so in a private message the button is THE way.  It has its own
``custom_id`` (:data:`DM_BUTTON_ID`): under the public hint only the hinted
player may click (:func:`handle_click`); in a private message the clicker is
the recipient (:func:`handle_dm_click`), and that button does nothing on a
server channel - the two cases can never be mixed up.

Who switched the warnings off is kept in ``data/boss_detection_off.txt`` (one
id per line), so nothing changes in the database.

Both buttons are *persistent* ones (fixed ``custom_id``, no timeout): they keep
working on messages posted before a restart.  The cog registers each view once
with ``bot.add_view`` (:func:`register`).
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
#: The button under the PRIVATE warning (a different id on purpose, see the module docstring).
DM_BUTTON_ID = "altermmo:boss-detect-off-dm"
BUTTON_LABEL = "Wyłącz powiadomienia o bossie"
BUTTON_EMOJI = "🔕"

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

#: The second line of the private warning (under "Nadciąga boss! Będzie ...").  ``{channel_id}``:
#: the boss channel (``bot.ids.boss_channel`` - the test channel in debug mode).
MSG_DM_HOW_TO_STOP = ("Nie chcesz tych powiadomień? Kliknij przycisk poniżej albo wpisz **$wyczuwanie** "
                      "na kanale <#{channel_id}>.")
#: Answers to the button under the private warning (a private conversation: no mention in front).
MSG_DM_SWITCHED_OFF = ("Gotowe - Twój towarzysz nie będzie już ostrzegał Cię o nadciągającym bossie. "
                       "Żeby włączyć powiadomienia ponownie, wpisz **$wyczuwanie** na kanale <#{channel_id}>.")
MSG_DM_NOT_SAVED = ("Nie udało się tego zapisać <:Sadge:936907659142111273> Spróbuj za chwilę albo wpisz "
                    "**$wyczuwanie** na kanale <#{channel_id}>.")
#: The private warning's button clicked anywhere but in a private message (it is never posted there).
MSG_DM_ONLY = ("Ten przycisk działa tylko w prywatnej wiadomości od Twojego towarzysza. Swoje powiadomienia "
               "o bossie wyłączysz i włączysz komendą **$wyczuwanie**.")

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

    @discord.ui.button(label=BUTTON_LABEL, style=discord.ButtonStyle.secondary, emoji=BUTTON_EMOJI,
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


# -- the button under the private warning ---------------------------------------------

def dm_warning(bot: "AlterBot", first_line: str) -> str:
    """The private warning: ``first_line`` ("Nadciąga boss! Będzie epicki.") + how to stop them."""
    return f"{first_line}\n{MSG_DM_HOW_TO_STOP.format(channel_id=bot.ids.boss_channel)}"


class DetectionOffDmView(discord.ui.View):
    """The "switch the warnings off" button under the private warning."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label=BUTTON_LABEL, style=discord.ButtonStyle.secondary, emoji=BUTTON_EMOJI,
                       custom_id=DM_BUTTON_ID)
    async def switch_off_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await handle_dm_click(interaction)


def in_private_messages(interaction: Any) -> bool:
    """True when the click came from a private conversation with the bot (no server around it)."""
    if getattr(interaction, "guild_id", None) is not None or getattr(interaction, "guild", None) is not None:
        return False
    kind = getattr(getattr(interaction, "channel", None), "type", None)
    return kind is None or kind == discord.ChannelType.private


async def _answer_dm(interaction: Any, text: str) -> bool:
    """Answer a click in a private conversation with an ordinary message.  Never raises.

    Not :func:`ui.reply`: that one is made for server channels - it would put
    the reader's own mention in front (pointless with one reader; it skips it
    without a server anyway) and it swallows a notice repeated within seconds,
    while here every click on an old warning should be answered.
    """
    quiet = discord.AllowedMentions.none()
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(text, allowed_mentions=quiet)
        else:
            await interaction.followup.send(text, allowed_mentions=quiet)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - an answer is never worth an error
        log.info("Could not answer the boss warning button of %s (%s: %s).",
                 getattr(getattr(interaction, "user", None), "id", "?"), type(exc).__name__, exc)
        return False


async def handle_dm_click(interaction: Any) -> None:
    """The button under a private warning: switch the clicker's warnings off.  Never raises.

    The click is acknowledged first (Discord gives 3 seconds, a slow disk
    must not cost the answer), then the switch is saved, answered, and the
    button is taken off that warning.  Clicking it again (also on an older
    warning) answers the same - the switch simply stays off.
    """
    bot = interaction.client
    if not in_private_messages(interaction):
        # Never posted on a server; answered like every refusal there (it names the clicker).
        await ui.reply(interaction, MSG_DM_ONLY)
        return
    clicker_id = interaction.user.id
    channel_id = bot.ids.boss_channel
    await ui.ack(interaction)
    try:
        switch_off(bot, clicker_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the click must be answered whatever the disk says
        log.exception("Could not switch the boss warnings of %s off.", clicker_id)
        await _answer_dm(interaction, MSG_DM_NOT_SAVED.format(channel_id=channel_id))
        try:
            await bot.log_to_owner(f"Nie udało się zapisać wyłączenia powiadomień o bossie gracza "
                                   f"<@{clicker_id}>: `{type(exc).__name__}: {exc}`")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("Could not tell the owner about the boss warning switch.", exc_info=True)
        return
    await _answer_dm(interaction, MSG_DM_SWITCHED_OFF.format(channel_id=channel_id))
    await ui.retire(interaction.message)  # the warning keeps its text; a failed edit changes nothing


def _dm_view() -> DetectionOffDmView | None:
    try:
        return DetectionOffDmView()
    except Exception:  # noqa: BLE001 - the warning matters more than its button
        log.exception("Could not build the boss warning button; the warning goes without it.")
        return None


async def send_warning(member: Any, text: str) -> None:
    """Send the private warning with its button; raises like :func:`io.send` (the caller decides).

    The same two attempts as before the button (a transient failure is
    retried once - as it always was).  Should Discord refuse the message
    because of its button (a 400 that is not "closed private messages"), the
    warning goes again without it: the warning matters, the button is extra.
    """
    view = _dm_view()
    try:
        if view is None:
            await io.send(member, text, attempts=2)
            return
        try:
            await io.send(member, text, view=view, attempts=2)
        except discord.HTTPException as exc:
            if isinstance(exc, (discord.Forbidden, discord.NotFound)) or io.is_transient(exc):
                raise
            log.warning("Boss detection: the warning with its button was refused (%s: %s); sending it "
                        "without the button.", type(exc).__name__, exc)
            await io.send(member, text, attempts=1)
    finally:
        # discord.py would otherwise keep one view per warning for ever; clicks are
        # handled by the view registered once in register() (same custom_id).
        if view is not None:
            view.stop()


def register(bot: "AlterBot") -> None:
    """Make the buttons under hints and warnings from before a restart work again.

    Call once, in a running loop.  Each view on its own: one that cannot be
    registered does not take the other with it.
    """
    for build in (DetectionOffView, DetectionOffDmView):
        try:
            bot.add_view(build())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - old messages lose their button, nothing else
            log.exception("Could not register the boss warning button (%s).", build.__name__)
