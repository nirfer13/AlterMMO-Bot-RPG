"""Shortcut buttons under game messages: "Pokaż towarzysza" and "Album".

Owner's request of 2026-11-24: "jeśli ktoś wydropi jajko lub zwiększy się level
towarzysza, to pojawi się pod tym dropem przycisk pokaż towarzysza. Jeśli ktoś
poświęci towarzysza, to chcę, żeby pod komunikatem pojawił się przycisk album,
który od razu pokazuje album."

* :func:`pet_view` goes under a drop that gave the player a new egg at his side
  or a level of his pet (``boss.rand_loot``, ``daily.rand_loot``);
* :func:`album_view` goes under the message that a sacrificed pet was kept in
  the album (``album.saved`` - supporters only, as the album itself).

A button belongs to the player of its message: his id is part of the
``custom_id`` (``altermmo:show:pet:<id>``), so the button keeps working after a
restart without any stored state - discord.py's :class:`discord.ui.DynamicItem`
rebuilds it from the id (:func:`register`, called once at start-up).  Anybody
else who clicks is told whose button it is.  A click does exactly what the
panel's "Towarzysz" / "Album" buttons do (``hub.show_display``): the same
gates - nothing during a fight, nothing while the channel is busy - and the
answer is posted on the channel like the typed ``!towarzysz`` / ``!album``.

A view made of these buttons only is "fully dynamic": discord.py keeps nothing
per message for it, so sending many drops does not grow the bot's memory.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord

from ..core import ui

log = logging.getLogger(__name__)

PET_LABEL = "Pokaż towarzysza"
PET_EMOJI = "🐾"
ALBUM_LABEL = "Album"
ALBUM_EMOJI = "📖"
PET_ID = ui.PREFIX + "show:pet:{player_id}"
ALBUM_ID = ui.PREFIX + "show:album:{player_id}"


async def _clicked(interaction: Any, player_id: int, what: str) -> bool:
    """A shortcut was clicked: its owner gets the display, anybody else a refusal.  Never raises."""
    from . import hub  # (lazily: the hub imports half of the game)

    if getattr(interaction.user, "id", None) != player_id:
        await ui.reply(interaction, ui.MSG_NOT_YOURS_NAMED.format(player_id=player_id))
        return False
    show = hub.show_pet if what == "pet" else hub.show_album
    log.info("Shortcut %s clicked by %s.", what, player_id)
    try:
        return await hub.show_display(interaction, show)
    except Exception as exc:  # noqa: BLE001 - discord.py would only log it: answer and report as a view does
        log.error("Shortcut %s of %s failed.", what, player_id, exc_info=exc)
        await ui.reply(interaction, ui.MSG_FAILED)
        report = getattr(interaction.client, "log_to_owner", None)
        if report is not None:
            await report(f"Błąd przycisku `{what}` ({interaction.user}): `{type(exc).__name__}: {exc}`")
        return False


class PetButton(discord.ui.DynamicItem[discord.ui.Button], template=r"altermmo:show:pet:(?P<player>[0-9]{1,20})"):
    """"Pokaż towarzysza" of one player."""

    def __init__(self, player_id: int) -> None:
        self.player_id = int(player_id)
        super().__init__(discord.ui.Button(label=PET_LABEL, emoji=PET_EMOJI, style=discord.ButtonStyle.secondary,
                                           custom_id=PET_ID.format(player_id=self.player_id)))

    @classmethod
    async def from_custom_id(cls, interaction: Any, item: Any, match: re.Match[str], /) -> "PetButton":
        return cls(int(match["player"]))

    async def callback(self, interaction: Any) -> None:
        await _clicked(interaction, self.player_id, "pet")


class AlbumButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"altermmo:show:album:(?P<player>[0-9]{1,20})"):
    """"Album" of one player."""

    def __init__(self, player_id: int) -> None:
        self.player_id = int(player_id)
        super().__init__(discord.ui.Button(label=ALBUM_LABEL, emoji=ALBUM_EMOJI, style=discord.ButtonStyle.secondary,
                                           custom_id=ALBUM_ID.format(player_id=self.player_id)))

    @classmethod
    async def from_custom_id(cls, interaction: Any, item: Any, match: re.Match[str], /) -> "AlbumButton":
        return cls(int(match["player"]))

    async def callback(self, interaction: Any) -> None:
        await _clicked(interaction, self.player_id, "album")


def _view(item: discord.ui.Item) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(item)
    return view


def pet_view(player_id: Any) -> discord.ui.View:
    """The "Pokaż towarzysza" button of ``player_id`` (a member or an id)."""
    return _view(PetButton(int(getattr(player_id, "id", player_id))))


def album_view(player_id: Any) -> discord.ui.View:
    """The "Album" button of ``player_id`` (a member or an id)."""
    return _view(AlbumButton(int(getattr(player_id, "id", player_id))))


def register(bot: Any) -> bool:
    """Make the shortcut buttons of messages posted before a restart work.  Call once; never raises."""
    try:
        bot.add_dynamic_items(PetButton, AlbumButton)
        return True
    except Exception:  # noqa: BLE001 - the buttons of old messages stay dead, nothing else
        log.exception("Could not register the shortcut buttons.")
        return False
