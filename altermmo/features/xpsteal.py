"""Stealing Discord experience (``$kradnijxp``).

Rare bosses can drop "Ukradnij wybranemu użytkownikowi 2500 doświadczenia na
Discordzie" (loot id 2).  The winner receives a *theft right*, kept in
``data/xpThefts.json`` as ``{"<discord id>": [2500, ...]}``, and spends it with
``$kradnijxp <nick>``.

The victim must stay secret: the command message is deleted at once and the
public confirmation names only the thief.  Discord experience itself belongs to
another bot (Horus), so the transfer is written to the experience summary
(thief ``+amount``, victim ``-amount``, shown by ``$Exp``) and reported in the
owner's log channel, exactly like every other experience reward of this bot.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ..core import io
from . import expsum

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

#: Loot id of the "steal experience" drop in config/lootConfig.json.
LOOT_ID = 2
DEFAULT_AMOUNT = 2500
COMMAND_HINT = ("Wpisz **$kradnijxp nick**, żeby wskazać ofiarę "
                "(wiadomość od razu zniknie - nikt nie zobaczy, kogo okradasz).")

MSG_NOTHING_TO_USE = ("<@{thief}>, nie masz żadnej kradzieży doświadczenia do wykorzystania "
                      "<:Sadge:936907659142111273> Taką okazję można zdobyć na rzadkim bossie.")
MSG_NO_TARGET = ("<@{thief}>, podaj kogo chcesz okraść: **$kradnijxp nick** "
                 "(możesz też oznaczyć osobę). Twoja okazja nie przepadła.")
MSG_NOT_FOUND = ("<@{thief}>, nie znalazłem takiej osoby na serwerze. Wpisz dokładny nick albo oznacz "
                 "ofiarę - Twoja okazja nie przepadła.")
MSG_AMBIGUOUS = ("<@{thief}>, kilka osób pasuje do tego nicku. Oznacz ofiarę (@nick) albo podaj jej ID - "
                 "Twoja okazja nie przepadła.")
MSG_INVALID_TARGET = ("<@{thief}>, tej osoby nie da się okraść. Wybierz kogoś innego - "
                      "Twoja okazja nie przepadła.")
MSG_DONE = ("🥷 <@{thief}> zakradł się po cichu i ukradł komuś **{amount} doświadczenia** na Discordzie! "
            "Kto jest ofiarą? Tego nikt nie wie... <:Susge:973591024322633858>")

_MENTION = re.compile(r"^<@!?(\d{15,20})>$")


# -- theft rights ---------------------------------------------------------------------

def _load(bot: "AlterBot") -> dict[str, list[int]]:
    data = bot.store.xp_thefts.load()
    return data if isinstance(data, dict) else {}


def amount_from_description(description: str) -> int:
    """``"Ukradnij ... 2500 doświadczenia ..."`` -> 2500 (the legacy way of reading exp)."""
    numbers = [int(word) for word in str(description).split() if word.isdigit()]
    return numbers[0] if numbers else DEFAULT_AMOUNT


def grant(bot: "AlterBot", player_id: int | str, amount: int = DEFAULT_AMOUNT) -> None:
    """Give the player one theft right worth ``amount`` experience.  Never raises."""
    try:
        data = _load(bot)
        data.setdefault(str(player_id), []).append(int(amount))
        bot.store.xp_thefts.save(data)
        log.info("XP theft right (%s exp) granted to %s.", amount, player_id)
    except Exception:  # noqa: BLE001 - loot bookkeeping must not break a fight
        log.exception("Could not store an XP theft right for %s.", player_id)


def pending(bot: "AlterBot", player_id: int | str) -> list[int]:
    """Unused theft rights of the player (amounts, oldest first)."""
    return [int(amount) for amount in _load(bot).get(str(player_id), [])]


def _consume(bot: "AlterBot", player_id: int | str) -> int | None:
    data = _load(bot)
    amounts = data.get(str(player_id)) or []
    if not amounts:
        return None
    amount = int(amounts.pop(0))
    if amounts:
        data[str(player_id)] = amounts
    else:
        data.pop(str(player_id), None)
    bot.store.xp_thefts.save(data)
    return amount


# -- choosing the victim --------------------------------------------------------------

def find_members(guild: Any, text: str) -> list[Any]:
    """Members matching ``text``: a mention, an id, or a (case-insensitive) name/nick."""
    text = (text or "").strip().strip('"').strip()
    if not text or guild is None:
        return []
    mention = _MENTION.match(text)
    if mention or text.isdigit():
        member = guild.get_member(int(mention.group(1) if mention else text))
        return [member] if member is not None else []

    wanted = text.lstrip("@").casefold()
    found: list[Any] = []
    for member in guild.members:
        names = {getattr(member, "name", None), getattr(member, "display_name", None),
                 getattr(member, "global_name", None), getattr(member, "nick", None)}
        if any(name and str(name).casefold() == wanted for name in names):
            found.append(member)
    return found


async def steal(scene: "Scene", target_text: str | None) -> bool:
    """``$kradnijxp <nick>``: spend a theft right on the chosen member.

    Returns True when the theft happened.  The invoking message is deleted
    first, whatever happens next, so the victim's name never stays on screen.
    """
    bot = scene.bot
    thief = scene.author
    await io.safe_delete(scene.message)

    if not pending(bot, thief.id):
        await scene.send(MSG_NOTHING_TO_USE.format(thief=thief.id))
        return False
    if not target_text or not target_text.strip():
        await scene.send(MSG_NO_TARGET.format(thief=thief.id))
        return False

    matches = find_members(scene.guild, target_text)
    if not matches:
        await scene.send(MSG_NOT_FOUND.format(thief=thief.id))
        return False
    if len(matches) > 1:
        await scene.send(MSG_AMBIGUOUS.format(thief=thief.id))
        return False
    victim = matches[0]
    if getattr(victim, "bot", False) or victim.id == thief.id:
        await scene.send(MSG_INVALID_TARGET.format(thief=thief.id))
        return False

    amount = _consume(bot, thief.id)
    if amount is None:  # spent by a parallel command of the same player
        await scene.send(MSG_NOTHING_TO_USE.format(thief=thief.id))
        return False

    expsum.add_file_exp(bot, thief.id, amount)
    expsum.add_file_exp(bot, victim.id, -amount)
    log.info("XP theft: %s (%s) stole %s exp from %s (%s).", thief, thief.id, amount, victim, victim.id)
    await bot.log_to_owner(
        f"{thief.name} (<@{thief.id}>) ukradł {amount} expa użytkownikowi {victim.name} (<@{victim.id}>). "
        f"Przenieś doświadczenie w Horusie (w podsumowaniu $Exp: +{amount} / -{amount}).", ping=True)
    await scene.send(MSG_DONE.format(thief=thief.id, amount=amount))
    return True
