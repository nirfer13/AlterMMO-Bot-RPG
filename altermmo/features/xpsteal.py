"""Stealing Discord experience (``$kradnijxp``).

Rare bosses can drop "Ukradnij wybranemu użytkownikowi 10000 doświadczenia na
Discordzie" (loot id 2).  The winner receives a *theft right*, kept in
``data/xpThefts.json`` as ``{"<discord id>": [10000, ...]}``, and spends it with
``$kradnijxp <nick>``.

The victim must stay secret: the command message is deleted at once, the
public confirmation names only the thief, and the victim's name or id never
appears on a public channel (only in the owner's log).

Owner's decision of 2026-11-22: a theft takes **what the victim has, up to the
amount** - the thief gets exactly what the victim lost, never more ("To już
wina złodzieja, że wybrał sobie lichą ofiarę").  Discord experience belongs to
another bot (Horus), and only Horus can settle both sides in one transaction.
So the theft no longer goes through the experience summary (``expSummary.json``,
where the victim's deduction floored at zero while the thief always got the full
amount - experience was created).  Instead (protocol version 1, see
``SPEC_xp_transfer.md``; Horus: ``cogs/discord_xp.py``):

* the bot writes one **request** per theft into ``<data dir>/xp_transfers``:
  ``<id>.json`` (``id`` = ``uuid4().hex``), written as ``<id>.json.tmp`` and
  put in place with ``os.replace``, content
  ``{"version": 1, "id", "from": victim, "to": thief, "amount", "created"}``
  (ids as strings).  The bot never rewrites or deletes a request;
* Horus settles it exactly once, writes a **result** into
  ``<data dir>/xp_transfer_results`` (``{"version": 1, "id", "from", "to",
  "requested", "moved", "applied"}``, plus ``"error"`` for a request it
  rejected) and deletes the request;
* :class:`TransferWatch` (a supervised loop of the XpSteal cog) tells the thief
  on the game channel what the theft brought, writes the details into the
  owner's log and deletes the result.  A request Horus has not settled after
  ten minutes is reported to the owner once.

Exactly once on this side - one right gives at most one request, and no request
exists without its right spent - is the order of :func:`steal`: note the id as
this bot's own, write the request under its temporary name, spend the right,
put the request in place.

Only results of this bot's own requests are announced (Horus's protocol notes):
every id the bot writes is noted in ``xpTransfersWritten.json`` before its
request exists, and forgotten once its result is announced.  A result for any
other id - a request this bot never wrote, or an old one that came back from a
backup after it was finished (Horus answers a broken copy with an error result)
- is put aside and only noted in the owner's log: nobody is told anything, and
nobody gets a right back for a theft that was already settled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from ..core import challenge, io

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

#: Loot id of the "steal experience" drop in config/lootConfig.json.
LOOT_ID = 2
DEFAULT_AMOUNT = 10000
COMMAND_HINT = ("Wpisz **$kradnijxp nick**, żeby wskazać ofiarę "
                "(wiadomość od razu zniknie - nikt nie zobaczy, kogo okradasz). "
                "Zabierzesz tyle, ile ofiara ma - najwyżej tę kwotę.")

#: Posted (with a ping) right under the drop, so the winner cannot miss it.
#: The texts offer the nick or the ID and never a mention: a mention pushes a notification to the
#: victim (author and text included) the moment the message exists, before the bot can delete it.
#: A mention is still understood (find_members) - the texts just never suggest it.
MSG_HOW_TO = ("🥷 <@{thief}>, masz do wykorzystania **kradzież do {amount} doświadczenia** - tyle, ile "
              "ofiara ma! Wpisz na tym kanale **$kradnijxp nick** (albo ID ofiary). Nie oznaczaj ofiary - "
              "dostałaby powiadomienie. Twoja wiadomość od razu zniknie - nikt nie zobaczy, kogo okradasz. "
              "Okazja nie przepada: możesz jej użyć, kiedy chcesz.")
#: The same as a private message: the boss channel is cleared regularly.
DM_HOW_TO = ("Masz do wykorzystania **kradzież do {amount} doświadczenia** na Discordzie (łup z bossa; "
             "zabierzesz tyle, ile ofiara ma). Wejdź na kanał <#{channel}> i wpisz **$kradnijxp nick** albo "
             "ID ofiary (nie oznaczaj jej - dostałaby powiadomienie). Wiadomość od razu zniknie i nikt nie "
             "zobaczy, kogo okradasz. Okazja nie przepada, użyj jej, kiedy chcesz.")

MSG_NOTHING_TO_USE = ("<@{thief}>, nie masz żadnej kradzieży doświadczenia do wykorzystania "
                      "<:Sadge:936907659142111273> Taką okazję można zdobyć na rzadkim bossie.")
MSG_NO_TARGET = ("<@{thief}>, podaj kogo chcesz okraść: **$kradnijxp nick** albo ID tej osoby "
                 "(nie oznaczaj jej - dostałaby powiadomienie). Twoja okazja nie przepadła.")
MSG_NOT_FOUND = ("<@{thief}>, nie znalazłem takiej osoby na serwerze. Wpisz dokładny nick albo ID "
                 "ofiary - Twoja okazja nie przepadła.")
MSG_AMBIGUOUS = ("<@{thief}>, kilka osób pasuje do tego nicku. Podaj ID ofiary - "
                 "Twoja okazja nie przepadła.")
MSG_INVALID_TARGET = ("<@{thief}>, tej osoby nie da się okraść. Wybierz kogoś innego - "
                      "Twoja okazja nie przepadła.")
#: The public confirmation: only the thief is named.  How much it brought follows (TransferWatch).
MSG_DONE = ("🥷 <@{thief}> zakradł się po cichu i sięgnął po cudze doświadczenie na Discordzie - zabierze "
            "ofierze tyle, ile ona ma, najwyżej **{amount}**. Ile udało się ukraść? Wynik pojawi się tutaj, "
            "gdy tylko kradzież zostanie rozliczona. Kto jest ofiarą? Tego nikt nie wie... "
            "<:Susge:973591024322633858>")
#: The theft could not be written and the right is still there.
MSG_NOT_SAVED = ("<@{thief}>, nie udało się zapisać kradzieży - spróbuj ponownie za chwilę. "
                 "Twoja okazja nie przepadła.")
#: The right is spent but the theft could not be written, and the right could not be given back:
#: the owner has the exact numbers (MSG_OWNER_RIGHT_LOST) and gives it back by hand.
MSG_RIGHT_HELD = ("<@{thief}>, coś poszło nie tak przy zapisywaniu kradzieży. Właściciel gry już o tym "
                  "wie i odda Ci okazję.")

#: What the theft brought (TransferWatch), on the game channel - pings the thief only, never names the victim.
MSG_RESULT_FULL = ("🥷 <@{thief}>, Twoja kradzież przyniosła **{moved}** doświadczenia! "
                   "Ofiara nawet nie zauważyła... <:Susge:973591024322633858>")
MSG_RESULT_PARTIAL = ("🥷 <@{thief}>, Twoja kradzież przyniosła **{moved}** doświadczenia (z {requested} "
                      "możliwych) - ofiara po prostu nie miała więcej.")
MSG_RESULT_NOTHING = ("🥷 <@{thief}>, Twoja kradzież nic nie przyniosła - ofiara nie miała ani odrobiny "
                      "doświadczenia, więc okazja przepadła. Następnym razem wybierz bogatszą ofiarę! "
                      "<:Sadge:936907659142111273>")
MSG_RESULT_ERROR = ("🥷 <@{thief}>, coś poszło nie tak przy rozliczaniu Twojej kradzieży i doświadczenie "
                    "nie zostało przeniesione. Właściciel gry już o tym wie i to sprawdzi.")

#: ``$DajKradziez`` (the owner gives a right by hand).
MSG_GRANTED = "<@{player}> może teraz ukraść do {amount} doświadczenia (**$kradnijxp nick**)."
MSG_GRANT_BAD_AMOUNT = "Kwota kradzieży musi być większa od zera - nic nie zostało zapisane."
MSG_GRANT_NOT_SAVED = ("Nie udało się zapisać okazji kradzieży dla <@{player}>: pliku `xpThefts.json` nie da się "
                       "teraz odczytać albo zapisać (szczegóły w logu bota). Nic nie zostało zapisane, a plik nie "
                       "został nadpisany - sprawdź go i spróbuj ponownie.")

# Owner's log (may name the victim: only the owner reads it).  Amounts are shown with number();
# inside a ``$DajKradziez`` command they stay plain digits (the command reads them as a number).
#: A boss dropped a theft right, but it could not be stored: never saved over a file that could not be read.
MSG_OWNER_GRANT_FAILED = ("Nie udało się zapisać kradzieży doświadczenia z łupu bossa dla {who} ({amount} expa, "
                          "`{error}`). Plik `xpThefts.json` nie został nadpisany - okazje innych graczy są w nim "
                          "nietknięte. Sprawdź ten plik (zablokowany przez inny program albo uszkodzony), a potem "
                          "przyznaj okazję ręcznie: `$DajKradziez {player_id} {raw}`.")
MSG_OWNER_REQUESTED = ("{thief} kradnie użytkownikowi {victim} do {shown} expa (zlecenie `{id}`). "
                       "Horus przeniesie tyle, ile ofiara ma; wynik pojawi się tutaj i na kanale gry.")
MSG_OWNER_NOT_WRITTEN = ("Nie udało się zapisać kradzieży: {thief} -> {victim}, {shown} expa (`{error}`). "
                         "Nic nie zostało przeniesione, a złodziej zachował okazję (dostał prośbę, żeby "
                         "spróbował ponownie).")
MSG_OWNER_RIGHT_LOST = ("Kradzież NIE została zapisana, a okazja złodzieja przepadła (nie dało się jej "
                        "oddać): złodziej {thief}, ofiara {victim}, kwota {shown} (`{error}`). Nic nie "
                        "zostało przeniesione. Oddaj okazję komendą `$DajKradziez {thief_id} {amount}`.")
MSG_OWNER_DELETE_TEMP = " Usuń też plik `{path}` (niedokończona kradzież - nie zmieniaj jego nazwy)."
MSG_OWNER_RESULT = "Kradzież `{id}` rozliczona: {thief} ukradł {moved} z {requested} expa użytkownikowi {victim}."
MSG_OWNER_RESULT_PARTIAL = " Ofiara miała tylko tyle."
MSG_OWNER_RESULT_NOTHING = " Ofiara nie miała doświadczenia - kradzież przepadła."
MSG_OWNER_RESULT_ERROR = ("Horus odrzucił kradzież `{id}` ({error}) - nic nie zostało przeniesione: złodziej "
                          "{thief}, ofiara {victim}, kwota {shown}. Złodziej dostał wiadomość, że to "
                          "sprawdzisz; okazję oddasz komendą `$DajKradziez {thief_id} {requested}`.")
#: An error result without a usable thief id or amount: no command (it could not be a valid one).
MSG_OWNER_RESULT_ERROR_UNKNOWN = ("Horus odrzucił kradzież `{id}` ({error}) - nic nie zostało przeniesione: "
                                  "złodziej {thief}, ofiara {victim}, kwota {shown}. {told} Z wyniku nie da się "
                                  "odczytać {missing}, więc nie podaję komendy. Znajdź w tym logu wpis o zleceniu "
                                  "`{id}` („… kradnie użytkownikowi … do … expa”) - są w nim złodziej i kwota - i "
                                  "jeśli okazja mu się należy, oddaj ją komendą $DajKradziez z jego id i tą kwotą.")
MSG_OWNER_RESULT_TOLD = "Złodziej dostał wiadomość, że to sprawdzisz."
MSG_OWNER_RESULT_NOT_TOLD = "Nikt nie dostał wiadomości (nie wiadomo, kto jest złodziejem)."
#: A result for an id this bot never wrote, or whose result it has announced already.
MSG_OWNER_FOREIGN_RESULT = ("Pomijam wynik kradzieży `{name}` od Horusa: ten bot nie zlecił takiej kradzieży "
                            "albo już ją ogłosił (np. zlecenie wróciło z kopii zapasowej). Nikt nie dostał "
                            "wiadomości i nikomu nie trzeba oddawać okazji.{details} Plik {where}.")
MSG_OWNER_FOREIGN_DETAILS = " Horus podaje: od {victim} do {thief}, przeniesiono {moved} expa{error}."
MSG_OWNER_WRITTEN_UNREADABLE = ("Nie mogę odczytać pliku `{name}` (`{error}`), w którym bot zapisuje swoje "
                                "zlecenia kradzieży - wyniki od Horusa czekają w folderze `{folder}` i zostaną "
                                "ogłoszone, gdy plik znów da się odczytać. Sprawdź go (zablokowany przez inny "
                                "program albo uszkodzony); do tego czasu nowe kradzieże się nie zapiszą.")
MSG_OWNER_BROKEN_RESULT = ("Plik wyniku kradzieży `{name}` jest uszkodzony ({problem}) i {where}. Złodziej "
                           "nie dostał wiadomości - sprawdź ten plik i w razie potrzeby rozlicz kradzież ręcznie.")
MSG_OWNER_STUCK_RESULT = ("Nie mogę usunąć pliku wyniku kradzieży `{path}` ({error}). Złodziej już dostał "
                          "wiadomość i drugiej nie dostanie; usuń ten plik ręcznie.")
MSG_OWNER_SILENT = ("Od {minutes} min nie mogę napisać na kanale gry wyniku kradzieży `{id}` (`{error}`). "
                    "Wynik czeka i zostanie ogłoszony, gdy tylko się uda.")
MSG_OWNER_UNSETTLED = ("Kradzieże czekają ponad {minutes} min na rozliczenie przez Horusa - Horus nie działa "
                       "albo nie ma jeszcze wersji, która je rozlicza. Nic nie przepadło: zlecenia czekają w "
                       "folderze `{folder}`, dopóki Horus ich nie rozliczy.")
#: The right may or may not be spent (the file could not be read back): the owner gets the count to compare.
MSG_OWNER_RIGHT_UNKNOWN = ("Nie udało się zapisać kradzieży: {thief} -> {victim}, {shown} expa (`{error}`), a "
                           "pliku `xpThefts.json` nie da się teraz odczytać (`{read_error}`), więc nie wiem, czy "
                           "okazja została zużyta. Nic nie zostało przeniesione (zlecenie usunięte), a złodziej "
                           "dostał prośbę, żeby spróbował ponownie. Przed tą próbą miał okazji: {count} "
                           "({amounts}). Jeśli w `xpThefts.json` ma ich teraz mniej, a w tym logu nie ma po tej "
                           "wiadomości wpisu o jego udanej kradzieży („kradnie”), oddaj okazję komendą "
                           "`$DajKradziez {thief_id} {amount}`.")
#: Never "rename it": whether the right is still there says nothing once the thief may have used it again.
MSG_OWNER_TEMP_LEFT = ("Bot przerwał zapisywanie kradzieży (pliki `.tmp` w folderze `{folder}`) - Horus ich nie "
                       "rozlicza i nic nie zostało przeniesione. Usuń te pliki i nigdy nie zmieniaj ich nazwy "
                       "(złodziej mógł już wykorzystać okazję jeszcze raz - kradzież zostałaby rozliczona dwa "
                       "razy). Złodziej nie dostał potwierdzenia, więc jego okazja mogła zostać albo przepaść: "
                       "jeśli w `xpThefts.json` jej nie ma, a w tym logu nie ma po czasie zapisania pliku wpisu "
                       "o jego udanej kradzieży („kradnie”), oddaj ją komendą podaną przy pliku.")

# -- the protocol with Horus (version 1) ----------------------------------------------

PROTOCOL_VERSION = 1
#: Request and result files: ``<32 lower-case hex>.json``; every other name is ignored.
FILE_NAME = re.compile(r"[0-9a-f]{32}\.json")
TEMP_SUFFIX = ".tmp"
DISCORD_ID = re.compile(r"[0-9]{1,20}")
#: Sub-folder of the results folder for result files that cannot be read.
REJECTED_FOLDER = "rejected"
#: A result file bigger than this is not a result (Horus writes ~200 bytes).
MAX_RESULT_BYTES = 64 * 1024
#: How often the results folder is looked at (seconds).
POLL_EVERY = 10.0
#: A request older than this without a result: the owner is told (once per request and start).
UNSETTLED_AFTER = 600.0
#: A temporary request file older than this is a theft whose writing was cut off (see steal).
TEMP_LEFT_AFTER = 60.0
#: Failed attempts to post one result before the owner is told (once; it is still retried).
SEND_FAILURES_REPORTED = 6
#: How long an announced result id is remembered (data/xpTransfersAnnounced.json).
ANNOUNCED_KEEP = 30 * 24 * 3600.0
#: At most this many requests are listed in one owner note (it may take several messages, see note_parts).
NOTE_LINES = 10
#: The longest message of an owner note: ``AlterBot.log_to_owner`` cuts a message at 1990 characters,
#: and a ping puts ``"<@owner id>!   "`` in front - at most 27 characters (a Discord id has <= 20 digits).
NOTE_LIMIT = 1990 - len("<@" + "9" * 20 + ">!   ")
#: Supervised loop of the XpSteal cog.
TRANSFERS_LOOP = "xp-transfers"

_MENTION = re.compile(r"^<@!?(\d{15,20})>$")


def number(value: int) -> str:
    """``4000`` -> ``"4 000"`` (a no-break space, so Discord never splits the number)."""
    return f"{int(value):,}".replace(",", chr(0xA0))


def _shown(raw: Any) -> str:
    """An amount read from a file for the owner: ``number()`` when it is an integer, else (cut) as it is."""
    value = _integer(raw)
    return number(value) if value is not None else f"`{str(raw)[:20]}`"


def requests_dir(bot: "AlterBot") -> Path:
    return Path(bot.store.xp_transfers_dir)


def results_dir(bot: "AlterBot") -> Path:
    return Path(bot.store.xp_transfer_results_dir)


# -- theft rights ---------------------------------------------------------------------

def _load(bot: "AlterBot") -> dict[str, list[int]]:
    data = bot.store.xp_thefts.load()
    return data if isinstance(data, dict) else {}


def _load_strict(bot: "AlterBot") -> dict[str, Any]:
    """``xpThefts.json`` as it is on the disk; raises when it cannot be read or is no JSON object.

    For the reads that a save is built on (:func:`grant`, :func:`_consume`,
    :func:`_give_back`) and for "was the right spent?" in :func:`steal`.
    ``JsonFile.load`` answers ``{}`` for a file it cannot read (another program
    holding it, a broken file): saving on top of that would wipe every other
    player's rights, and "no rights left" would count a locked file as a spent
    right.  A missing or empty file really is "no rights" (as for ``JsonFile.load``).
    """
    return _strict(bot.store.xp_thefts)


def _strict(store: Any) -> dict[str, Any]:
    """A ``JsonFile`` holding a JSON object, read strictly (see :func:`_load_strict`)."""
    path = store.path
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def _valid(raw: Any, player_id: int | str) -> list[int]:
    """The usable amounts of one player's entry (broken ones are logged and skipped).

    An amount of 0 or less is broken too: the protocol's ``amount`` is a positive
    integer, and Horus would only reject such a request.
    """
    if not isinstance(raw, list):
        return []
    amounts = []
    for amount in raw:
        try:
            value = int(amount)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            amounts.append(value)
        else:
            log.warning("Ignoring a broken XP theft right of %s: %r", player_id, amount)
    return amounts


def amount_from_description(description: str) -> int:
    """``"Ukradnij ... 10000 doświadczenia ..."`` -> 10000 (the legacy way of reading exp)."""
    numbers = [int(word) for word in str(description).split() if word.isdigit()]
    return numbers[0] if numbers else DEFAULT_AMOUNT


def _add_right(bot: "AlterBot", player_id: int | str, amount: int) -> None:
    """Store one more right; raises when ``amount`` is not positive or ``xpThefts.json`` cannot be
    read or saved - it never saves over a file it could not read (see :func:`_load_strict`)."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError(f"an XP theft right must be worth more than 0, not {amount}")
    data = _load_strict(bot)
    key = str(player_id)
    data[key] = [*_valid(data.get(key), player_id), amount]  # (broken entries are dropped on the way)
    bot.store.xp_thefts.save(data)
    log.info("XP theft right (%s exp) granted to %s.", amount, player_id)


def grant(bot: "AlterBot", player_id: int | str, amount: int = DEFAULT_AMOUNT) -> bool:
    """Give the player one theft right worth ``amount`` experience; False (logged) when it could not
    be stored - the file is then left as it was.  Never raises."""
    try:
        _add_right(bot, player_id, amount)
        return True
    except Exception:  # noqa: BLE001 - loot bookkeeping must not break a fight
        log.exception("Could not store an XP theft right (%r exp) for %s.", amount, player_id)
        return False


async def grant_drop(bot: "AlterBot", winner: Any, amount: int = DEFAULT_AMOUNT) -> bool:
    """A boss drop: :func:`grant`, and when the right cannot be stored, tell the owner at once with a
    ping and the exact ``$DajKradziez`` line that gives it by hand.  Never raises (the fight goes on).

    Before, a file that could not be read at that moment was taken for "no rights" and
    ``{winner: [amount]}`` saved over it - every other player's unused rights were lost.
    """
    try:
        _add_right(bot, winner.id, amount)
        return True
    except Exception as exc:  # noqa: BLE001 - loot bookkeeping must not break a fight
        log.exception("Could not store the XP theft right (%r exp) dropped for %s.", amount, winner.id)
        await bot.log_to_owner(MSG_OWNER_GRANT_FAILED.format(
            who=_who(winner), amount=_shown(amount), error=_error(exc), player_id=winner.id, raw=amount),
            ping=True)
        return False


def pending(bot: "AlterBot", player_id: int | str) -> list[int]:
    """Unused theft rights of the player (amounts, oldest first).  Never raises."""
    try:
        raw = _load(bot).get(str(player_id))
    except Exception:  # noqa: BLE001 - an unreadable file must not cost anybody their loot
        log.exception("Could not read the XP theft rights of %s.", player_id)
        return []
    return _valid(raw, player_id)


def _consume(bot: "AlterBot", player_id: int | str) -> int | None:
    """Spend the oldest right; None when there is none.  Raises when the file cannot be read or saved."""
    data = _load_strict(bot)
    key = str(player_id)
    amounts = _valid(data.get(key), player_id)  # (broken entries are dropped on the way)
    if not amounts:
        return None
    amount = amounts.pop(0)
    if amounts:
        data[key] = amounts
    else:
        data.pop(key, None)
    bot.store.xp_thefts.save(data)
    return amount


def _give_back(bot: "AlterBot", player_id: int | str, amount: int) -> None:
    """Put a spent right back in front of the others.  Raises when the file cannot be read or saved
    (never saves over a file it could not read: the other players' rights would be lost)."""
    data = _load_strict(bot)
    key = str(player_id)
    data[key] = [int(amount), *_valid(data.get(key), player_id)]
    bot.store.xp_thefts.save(data)


async def remind(scene: "Scene", winner: Any) -> None:
    """Tell ``winner`` how to use the theft right they have just won.  Never raises.

    A ping under the drop and - because the boss channel is wiped regularly -
    the same instructions as a private message (closed DMs are simply skipped).
    """
    bot = scene.bot
    amounts = pending(bot, winner.id)
    if not amounts:
        return
    amount = number(amounts[-1])
    try:
        await scene.send(MSG_HOW_TO.format(thief=winner.id, amount=amount))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the right is stored; the hint is a courtesy
        log.warning("Could not post the XP theft hint for %s: %s: %s", winner.id, type(exc).__name__, exc)
    try:
        await io.send(winner, DM_HOW_TO.format(amount=amount, channel=bot.ids.boss_channel), attempts=1)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - closed private messages are fine
        log.info("XP theft hint not delivered privately to %s (%s).", winner.id, type(exc).__name__)


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


# -- the request ----------------------------------------------------------------------

def new_request(thief_id: int | str, victim_id: int | str, amount: int) -> dict[str, Any]:
    """The request of one theft (protocol version 1): ids as strings, ``created`` in UTC."""
    transfer_id = uuid.uuid4().hex
    return {
        "version": PROTOCOL_VERSION,
        "id": transfer_id,
        "from": str(victim_id),
        "to": str(thief_id),
        "amount": int(amount),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _write_temp(path: Path, request: dict[str, Any]) -> None:
    """Write the request under its temporary name, on the disk (fsync) before it is put in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(request, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())


def _put_in_place(temp: Path, final: Path) -> None:
    """``os.replace`` that waits out a reader (an antivirus, a backup) - see core/storage.py."""
    for attempt in range(1, 7):
        try:
            os.replace(temp, final)
            return
        except PermissionError:
            if attempt == 6:
                raise
            time.sleep(0.02 * attempt)


def _discard(path: Path) -> bool:
    """Delete a file; True when it is gone.  Never raises."""
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        log.exception("Could not delete %s.", path)
        return False
    return True


def _who(member: Any) -> str:
    return f"{member.name} (<@{member.id}>)"


def _note_written(bot: "AlterBot", transfer_id: str) -> None:
    """Note ``transfer_id`` as this bot's own request (``xpTransfersWritten.json``: ``{id: unix time}``).

    Raises when the note cannot be read or saved (strict, so that a file that cannot be
    read is never overwritten - the ids of the other waiting thefts would be lost and
    their results ignored).  :func:`steal` calls it before the request exists.
    """
    store = bot.store.xp_transfers_written
    data = _strict(store)
    data[transfer_id] = time.time()
    store.save(data)


def _reported_temps(bot: "AlterBot") -> set[str]:
    """Temporary request files :func:`steal` could not delete and has already told the owner about
    in this run (with the right instruction): :class:`TransferWatch` gives no second note on them."""
    found = getattr(bot, "_xp_temps_reported", None)
    if found is None:
        found = set()
        bot._xp_temps_reported = found
    return found


async def steal(scene: "Scene", target_text: str | None) -> bool:
    """``$kradnijxp <nick>``: spend a theft right on the chosen member.

    Returns True when the request for Horus is in place.  The invoking message
    is deleted first, whatever happens next, so the victim's name never stays
    on screen.

    Exactly once, in this order (no ``await`` between the steps, so a second
    ``$kradnijxp`` of the same player cannot slip in between):

    0. the id is noted as this bot's own (:func:`_note_written`; only results
       of noted ids are announced).  Fails: nothing happened, as in 1.  (A
       theft that fails later leaves its id noted: harmless, no result ever
       comes for it.)
    1. the request is written as ``<id>.json.tmp`` - Horus ignores that name.
       Fails: nothing happened; the thief keeps the right and is asked to try
       again, the owner is told;
    2. the right is spent (``xpThefts.json``, written atomically).  Fails: the
       file is read again - still there means nothing happened (the temporary
       file is deleted, as in 1.); gone means it was spent after all and the
       theft goes on; cannot be read means "unknown", never "spent": the
       temporary file is deleted and the owner gets the numbers to compare;
    3. the request is put in place (``os.replace``): from now on Horus settles
       it.  Fails: the temporary file is deleted and the right given back; when
       even that fails the owner gets a ping with the exact numbers and the
       command that gives the right back.

    A request is never in place without its right spent (3. comes after 2.),
    and a spent right without a request is either given back or reported.  The
    one case left - the bot killed between 2. and 3. - leaves the temporary
    file behind, which :class:`TransferWatch` reports to the owner (delete it,
    never rename it: by then the thief may have used the right again).
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

    rights = pending(bot, thief.id)
    if not rights:  # spent by a parallel command of the same player
        await scene.send(MSG_NOTHING_TO_USE.format(thief=thief.id))
        return False
    amount = rights[0]  # what _consume spends
    request = new_request(thief.id, victim.id, amount)
    folder = requests_dir(bot)
    final = folder / f"{request['id']}.json"
    temp = folder / f"{request['id']}.json{TEMP_SUFFIX}"
    details = {"thief": _who(thief), "victim": _who(victim), "amount": amount, "shown": number(amount),
               "thief_id": thief.id}

    # 0. the id noted as this bot's own
    try:
        _note_written(bot, request["id"])
    except Exception as exc:  # noqa: BLE001 - nothing happened yet
        log.exception("XP theft of %s: the request id could not be noted.", thief.id)
        await bot.log_to_owner(MSG_OWNER_NOT_WRITTEN.format(**details, error=_error(exc)), ping=True)
        await scene.send(MSG_NOT_SAVED.format(thief=thief.id))
        return False

    # 1. the request under its temporary name
    try:
        _write_temp(temp, request)
    except Exception as exc:  # noqa: BLE001 - nothing happened yet
        log.exception("XP theft of %s: the request could not be written.", thief.id)
        _discard(temp)
        await bot.log_to_owner(MSG_OWNER_NOT_WRITTEN.format(**details, error=_error(exc)), ping=True)
        await scene.send(MSG_NOT_SAVED.format(thief=thief.id))
        return False

    # 2. the right
    try:
        spent = _consume(bot, thief.id)
    except Exception as exc:  # noqa: BLE001
        try:  # (strict: a file that cannot be read is "unknown", never "spent")
            left: int | None = len(_valid(_load_strict(bot).get(str(thief.id)), thief.id))
        except Exception as read_exc:  # noqa: BLE001
            left, unknown = None, read_exc
        if left is not None and left < len(rights):
            log.warning("XP theft of %s: saving the spent right reported %s, but it is spent.",
                        thief.id, _error(exc))
            spent = amount
        else:
            log.exception("XP theft of %s: the right could not be spent.", thief.id)
            _discard(temp)  # no request: whatever the file says, Horus never settles this one
            if left is None:
                text = MSG_OWNER_RIGHT_UNKNOWN.format(**details, error=_error(exc), read_error=_error(unknown),
                                                      count=len(rights), amounts=", ".join(map(number, rights)))
            else:
                text = MSG_OWNER_NOT_WRITTEN.format(**details, error=_error(exc))
            await bot.log_to_owner(text, ping=True)
            await scene.send(MSG_NOT_SAVED.format(thief=thief.id))
            return False
    if spent is None:  # (cannot happen without an await in between; kept for safety)
        _discard(temp)
        await scene.send(MSG_NOTHING_TO_USE.format(thief=thief.id))
        return False

    # 3. the request in place
    try:
        _put_in_place(temp, final)
    except Exception as exc:  # noqa: BLE001
        log.exception("XP theft of %s: the request could not be put in place.", thief.id)
        temp_gone = _discard(temp)
        if not temp_gone:
            _reported_temps(bot).add(temp.name)  # (the note below says what to do with it)
        try:
            _give_back(bot, thief.id, amount)
        except Exception:  # noqa: BLE001
            log.exception("XP theft of %s: the spent right could not be given back.", thief.id)
            text = MSG_OWNER_RIGHT_LOST.format(**details, error=_error(exc))
            if not temp_gone:
                text += MSG_OWNER_DELETE_TEMP.format(path=temp)
            await bot.log_to_owner(text, ping=True)
            await scene.send(MSG_RIGHT_HELD.format(thief=thief.id))
            return False
        text = MSG_OWNER_NOT_WRITTEN.format(**details, error=_error(exc))
        if not temp_gone:
            text += MSG_OWNER_DELETE_TEMP.format(path=temp)
        await bot.log_to_owner(text, ping=True)
        await scene.send(MSG_NOT_SAVED.format(thief=thief.id))
        return False

    log.info("XP theft %s: %s (%s) steals up to %s exp from %s (%s); waiting for Horus.",
             request["id"], thief, thief.id, amount, victim, victim.id)
    await bot.log_to_owner(MSG_OWNER_REQUESTED.format(**details, id=request["id"]))
    try:
        await scene.send(MSG_DONE.format(thief=thief.id, amount=number(amount)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the theft is on its way; the result is announced anyway
        log.warning("Could not confirm the XP theft %s: %s", request["id"], _error(exc))
    return True


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


# -- the results ----------------------------------------------------------------------

def _discord_id(raw: Any) -> str | None:
    """A Discord id as the protocol writes it (a string of digits), else None."""
    if isinstance(raw, str) and DISCORD_ID.fullmatch(raw) and int(raw) > 0:
        return raw
    return None


def _integer(raw: Any) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def read_result(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(result, None)`` for a valid result file, ``(None, reason)`` for a broken one.

    A valid result: ``version`` 1, ``id`` = the file name's stem, ``moved`` an
    integer >= 0.  A settled one also has the thief and victim ids (strings of
    digits) and ``requested`` > 0 with ``moved <= requested``.  An ``"error"``
    result (Horus rejected the request) may lack ids and amounts - Horus echoes
    what it could read.  Raises OSError when the file cannot be read.
    """
    if path.stat().st_size > MAX_RESULT_BYTES:
        return None, "too large"
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "not JSON"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    if _integer(data.get("version")) != PROTOCOL_VERSION:
        return None, "unsupported version"
    if data.get("id") != path.name[:-len(".json")]:
        return None, "id does not match the file name"
    moved = _integer(data.get("moved"))
    if moved is None or moved < 0:
        return None, "bad moved"
    error = data.get("error")
    if error is not None:
        if not isinstance(error, str) or not error.strip():
            return None, "bad error"
        return data, None
    if _discord_id(data.get("to")) is None:
        return None, "bad to id"
    if _discord_id(data.get("from")) is None:
        return None, "bad from id"
    requested = _integer(data.get("requested"))
    if requested is None or requested <= 0:
        return None, "bad requested"
    if moved > requested:
        return None, "moved more than requested"
    return data, None


def public_result_text(result: dict[str, Any]) -> str | None:
    """What the thief reads on the game channel; None when the thief is not known (an error result).

    Never names the victim.
    """
    thief = _discord_id(result.get("to"))
    if thief is None:
        return None
    if result.get("error") is not None:
        return MSG_RESULT_ERROR.format(thief=thief)
    moved, requested = result["moved"], result["requested"]
    if moved == 0:
        return MSG_RESULT_NOTHING.format(thief=thief)
    if moved < requested:
        return MSG_RESULT_PARTIAL.format(thief=thief, moved=number(moved), requested=number(requested))
    return MSG_RESULT_FULL.format(thief=thief, moved=number(moved))


def _person(bot: "AlterBot", raw: Any) -> str:
    """``Name (<@id>)`` for the owner's log; whatever the file said when it is no id."""
    user_id = _discord_id(raw)
    if user_id is None:
        return f"nieznany (`{str(raw)[:40]}`)"
    user = None
    try:
        user = bot.get_user(int(user_id))
    except Exception:  # noqa: BLE001 - a name is a courtesy
        user = None
    return f"{user.name} (<@{user_id}>)" if user is not None else f"<@{user_id}>"


def owner_result_text(bot: "AlterBot", result: dict[str, Any]) -> str:
    """The owner's log line of a result: thief, victim, requested, moved (and Horus's error)."""
    thief, victim = _person(bot, result.get("to")), _person(bot, result.get("from"))
    requested = _integer(result.get("requested"))  # (None only in an error result: Horus echoes what it read)
    if result.get("error") is not None:
        thief_id = _discord_id(result.get("to"))
        error = str(result["error"])[:200]
        if thief_id is None or requested is None:  # no command: it could not be a valid one
            missing = ("złodzieja ani kwoty" if thief_id is None and requested is None
                       else "złodzieja" if thief_id is None else "kwoty")
            return MSG_OWNER_RESULT_ERROR_UNKNOWN.format(
                id=result["id"], error=error, thief=thief, victim=victim, missing=missing,
                shown=number(requested) if requested is not None else "nieznana",
                told=MSG_OWNER_RESULT_TOLD if thief_id is not None else MSG_OWNER_RESULT_NOT_TOLD)
        return MSG_OWNER_RESULT_ERROR.format(id=result["id"], error=error, thief=thief, victim=victim,
                                             shown=number(requested), requested=requested, thief_id=thief_id)
    moved = result["moved"]
    text = MSG_OWNER_RESULT.format(id=result["id"], thief=thief, victim=victim, moved=number(moved),
                                   requested=number(requested))
    if moved == 0:
        text += MSG_OWNER_RESULT_NOTHING
    elif moved < requested:
        text += MSG_OWNER_RESULT_PARTIAL
    return text


def _transfer_files(folder: Path) -> list[Path]:
    """The request / result files of ``folder`` (``<32 hex>.json``), oldest first; [] without the folder."""
    try:
        entries = list(os.scandir(folder))
    except FileNotFoundError:
        return []
    found = []
    for entry in entries:
        try:
            if FILE_NAME.fullmatch(entry.name) and entry.is_file():
                found.append((entry.stat().st_mtime, entry.name, Path(entry.path)))
        except OSError:
            continue  # (gone meanwhile)
    return [path for _, _, path in sorted(found)]


class TransferWatch:
    """Tells the thieves what their thefts brought; tells the owner what Horus has not settled.

    One per running bot (the XpSteal cog's supervised loop, :data:`TRANSFERS_LOOP`).
    Every :data:`POLL_EVERY` seconds:

    * every result file (``<data dir>/xp_transfer_results/<32 hex>.json``; other
      names are ignored) is announced to the thief on the game channel (only he
      is pinged, the victim is never named), written into the owner's log and
      deleted.  Not while a typed fight is on there (``challenge.fight_on``) -
      it waits for the next pass.  A send that fails keeps the file for the next
      pass (after :data:`SEND_FAILURES_REPORTED` failures the owner is told once).
    * a result that cannot be read (not JSON, ...) is moved into ``rejected/``
      and reported to the owner once.
    * a request older than :data:`UNSETTLED_AFTER` is reported to the owner once
      per request and start (Horus not running or not updated; nothing is lost),
      and so is a temporary request file left by an interrupted :func:`steal`.

    Duplicates are bounded: an announced id is remembered for this run (memory)
    and, written right after the send, for :data:`ANNOUNCED_KEEP` in
    ``xpTransfersAnnounced.json``; it is also dropped from the ids waiting for a
    result (``xpTransfersWritten.json``).  A result whose id is remembered is
    only deleted, never announced again - also when Horus writes it once more
    (it does after a crash between writing a result and noting it).  A result
    whose id is not waiting (never written by this bot, or finished before) is
    put aside unannounced (:meth:`_ignore`).  Only a crash between the send and
    both notes announces a result a second time, so a result is announced at
    most twice in all (when neither note can be saved at all: at most once per
    start, as the file is deleted right after the send).  The experience
    itself is moved exactly once by Horus whatever happens here.
    """

    def __init__(self, bot: "AlterBot") -> None:
        self.bot = bot
        self._announced: set[str] = set()
        self._send_failures: dict[str, int] = {}
        #: Owner notes already given in this run (``"<kind>:<name>"``).
        self._reported: set[str] = set()
        #: Steps of :meth:`check` failing right now -> their error (logged in full once).
        self._failing: dict[str, str] = {}

    async def run(self) -> None:
        """The supervised loop."""
        await self.bot.wait_until_ready()
        while True:
            await self.check()
            await asyncio.sleep(POLL_EVERY)

    async def check(self) -> None:
        """One pass over both folders.  Never raises (a cancellation passes)."""
        for step in (self.announce_results, self.report_unsettled):
            name = getattr(step, "__name__", "step")
            try:
                await step()
                self._failing.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the next pass tries again
                # (A folder that cannot be read fails every 10 s: the traceback once, then a line.)
                if self._failing.get(name) == _error(exc):
                    log.debug("XP transfers: %s still fails (%s).", name, _error(exc))
                else:
                    self._failing[name] = _error(exc)
                    log.exception("XP transfers: %s failed; trying again at the next pass.", name)

    # -- results ------------------------------------------------------------------

    def _remembered(self) -> dict[str, float]:
        data = self.bot.store.xp_transfers_announced.load()
        return data if isinstance(data, dict) else {}

    def _remember(self, transfer_id: str) -> None:
        self._announced.add(transfer_id)
        try:
            now = time.time()
            data = {key: when for key, when in self._remembered().items()
                    if isinstance(when, (int, float)) and now - when < ANNOUNCED_KEEP}
            data[transfer_id] = now
            self.bot.store.xp_transfers_announced.save(data)
        except Exception:  # noqa: BLE001 - this run still remembers it
            log.exception("Could not note the announced XP transfer %s.", transfer_id)

    async def announce_results(self) -> int:
        """Announce, log and delete the results; returns how many were announced now."""
        bot = self.bot
        paths = _transfer_files(results_dir(bot))
        if not paths:
            return 0
        written = await self._written()
        remembered = self._remembered()
        channel = bot.get_channel(bot.ids.boss_channel)
        told = 0
        for path in paths:
            transfer_id = path.name[:-len(".json")]
            if transfer_id in self._announced or transfer_id in remembered:
                log.info("XP transfer %s was announced before; its result file is only deleted.", transfer_id)
                await self._delete(path)
                continue
            if f"broken:{path.name}" in self._reported or f"foreign:{path.name}" in self._reported:
                continue  # reported, could not be moved aside
            if transfer_id not in written:
                await self._ignore(path)
                continue
            try:
                result, problem = read_result(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning("XP transfer result %s cannot be read now (%s).", path, _error(exc))
                continue  # (a reader holding it; the next pass)
            if result is None:
                await self._reject(path, problem or "?")
                continue
            if channel is None or challenge.fight_on(channel):
                return told  # not connected yet / a typed fight is on: the next pass
            text = public_result_text(result)
            if text is not None:
                thief = int(result["to"])
                try:
                    await io.send(channel, text, attempts=1, allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=[discord.Object(id=thief)]))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the file stays for the next pass
                    failures = self._send_failures.get(transfer_id, 0) + 1
                    self._send_failures[transfer_id] = failures
                    log.warning("Could not announce the XP transfer %s (attempt %d): %s",
                                transfer_id, failures, _error(exc))
                    if failures == SEND_FAILURES_REPORTED:
                        await bot.log_to_owner(MSG_OWNER_SILENT.format(
                            minutes=int(failures * POLL_EVERY // 60) or 1, id=transfer_id, error=_error(exc)))
                    return told  # the channel is in trouble: the others wait as well
            self._send_failures.pop(transfer_id, None)
            self._remember(transfer_id)
            self._forget_written(transfer_id)
            log.info("XP transfer %s announced: from %s to %s, requested %s, moved %s%s.", transfer_id,
                     result.get("from"), result.get("to"), result.get("requested"), result.get("moved"),
                     f", error {result['error']!r}" if result.get("error") is not None else "")
            await bot.log_to_owner(owner_result_text(bot, result), ping=result.get("error") is not None)
            await self._delete(path)
            told += 1
        return told

    async def _written(self) -> dict[str, Any]:
        """The ids this bot wrote (``xpTransfersWritten.json``), read strictly.

        Raises when the file cannot be read - the results then wait for the next
        pass instead of being taken for foreign ones (the owner is told once).
        """
        store = self.bot.store.xp_transfers_written
        try:
            written = _strict(store)
        except Exception as exc:
            if "written-unreadable" not in self._reported:
                self._reported.add("written-unreadable")
                await self.bot.log_to_owner(MSG_OWNER_WRITTEN_UNREADABLE.format(
                    name=store.path.name, error=_error(exc), folder=results_dir(self.bot)), ping=True)
            raise
        self._reported.discard("written-unreadable")
        return written

    def _forget_written(self, transfer_id: str) -> None:
        """The result is announced: the id is no longer waiting (a later copy of it is ignored)."""
        store = self.bot.store.xp_transfers_written
        try:
            data = _strict(store)
            if data.pop(transfer_id, None) is not None:
                store.save(data)
        except Exception:  # noqa: BLE001 - xpTransfersAnnounced.json still has it
            log.exception("Could not drop the announced XP transfer %s from %s.", transfer_id, store.path.name)

    async def _ignore(self, path: Path) -> None:
        """A result for an id this bot never wrote or already finished: put aside, the owner's log only.

        Horus's protocol notes: such results come e.g. from a request copy restored from a
        backup after its theft was finished (a broken copy gets an error result).  Nobody is
        told anything - above all the owner is not asked to give a right back.
        """
        key = f"foreign:{path.name}"
        self._reported.add(key)
        details = ""
        try:
            result, _ = read_result(path)
        except OSError:
            result = None
        if result is not None:
            error = result.get("error")
            details = MSG_OWNER_FOREIGN_DETAILS.format(
                victim=_person(self.bot, result.get("from")), thief=_person(self.bot, result.get("to")),
                moved=number(result["moved"]), error=f", błąd: {str(error)[:200]}" if error is not None else "")
        where = self._put_aside(path)
        log.warning("Ignoring the XP transfer result %s: not a request of this bot, or announced before.", path)
        await self.bot.log_to_owner(MSG_OWNER_FOREIGN_RESULT.format(name=path.name, details=details, where=where))

    def _put_aside(self, path: Path) -> str:
        """Move a result file into ``rejected/``; returns where it is now (for the owner's note)."""
        target = path.parent / REJECTED_FOLDER / path.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = target.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}")
            os.replace(path, target)
            return f"został odłożony do `{target}`"
        except OSError as exc:
            log.warning("Could not move the XP transfer result %s aside: %s", path, _error(exc))
            return f"nie dało się go odłożyć do `{target.parent}` (`{_error(exc)}`), więc leży, gdzie leżał"

    async def _delete(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            key = f"stuck:{path.name}"
            if key not in self._reported:  # retried at every pass, reported once
                self._reported.add(key)
                log.warning("Could not delete the XP transfer result %s: %s", path, _error(exc))
                await self.bot.log_to_owner(MSG_OWNER_STUCK_RESULT.format(path=path, error=_error(exc)))

    async def _reject(self, path: Path, problem: str) -> None:
        """Move a broken result aside (``rejected/``) and tell the owner once."""
        key = f"broken:{path.name}"
        if key in self._reported:
            return
        self._reported.add(key)
        where = self._put_aside(path)
        log.error("Broken XP transfer result %s (%s).", path, problem)
        await self.bot.log_to_owner(MSG_OWNER_BROKEN_RESULT.format(name=path.name, problem=problem, where=where),
                                    ping=True)

    # -- requests Horus has not settled ---------------------------------------------

    async def report_unsettled(self) -> int:
        """Tell the owner once about every request older than :data:`UNSETTLED_AFTER` and every
        temporary request file older than :data:`TEMP_LEFT_AFTER`; returns how many were reported now.

        Found by the files' age, so it works the same after a restart (each
        such request is then reported once more - once per start).
        """
        folder = requests_dir(self.bot)
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:
            return 0
        now = time.time()
        late: list[tuple[Path, float]] = []
        cut: list[tuple[Path, float]] = []
        for entry in entries:
            name = entry.name
            if FILE_NAME.fullmatch(name):
                found, after = late, UNSETTLED_AFTER
            elif name.endswith(TEMP_SUFFIX) and FILE_NAME.fullmatch(name[:-len(TEMP_SUFFIX)]):
                if name in _reported_temps(self.bot):
                    continue  # steal() has told the owner what to do with it
                found, after = cut, TEMP_LEFT_AFTER
            else:
                continue
            key = f"waiting:{name}"
            if key in self._reported:
                continue
            try:
                if not entry.is_file():
                    continue
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age >= after:
                self._reported.add(key)
                found.append((Path(entry.path), age))
        if late:
            await self._note(MSG_OWNER_UNSETTLED.format(minutes=int(UNSETTLED_AFTER // 60), folder=folder),
                             self._lines(late))
        if cut:
            await self._note(MSG_OWNER_TEMP_LEFT.format(folder=folder), self._lines(cut))
        return len(late) + len(cut)

    async def _note(self, head: str, lines: list[str]) -> None:
        """One owner note (``head`` + one line per file) in as many messages as it needs; the first pings."""
        for number_of_part, part in enumerate(note_parts(head, lines)):
            await self.bot.log_to_owner(part, ping=number_of_part == 0)

    def _lines(self, found: list[tuple[Path, float]]) -> list[str]:
        lines = [self._describe(path, age) for path, age in sorted(found, key=lambda item: -item[1])[:NOTE_LINES]]
        if len(found) > NOTE_LINES:
            lines.append(f"... i {len(found) - NOTE_LINES} więcej")
        return lines

    def _describe(self, path: Path, age: float) -> str:
        """One request for the owner: who steals from whom, how much, how long it waits."""
        minutes = int(age // 60)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            amount = data.get("amount")
            line = (f"- `{path.name}`: złodziej {_person(self.bot, data.get('to'))}, ofiara "
                    f"{_person(self.bot, data.get('from'))}, kwota {_shown(amount)}, czeka {minutes} min")
            if path.name.endswith(TEMP_SUFFIX):  # (MSG_OWNER_TEMP_LEFT: when the right is to be given back)
                thief_id = _discord_id(data.get("to"))
                line += f", zapisany {str(data.get('created'))[:40]}; "
                if thief_id is not None and _integer(amount) is not None:
                    line += f"okazję oddasz komendą `$DajKradziez {thief_id} {amount}`"
                else:
                    line += "złodzieja albo kwoty nie da się odczytać z pliku"
            return line
        except Exception:  # noqa: BLE001 - the name and the age are enough to find it
            return f"- `{path.name}` (nie da się odczytać), czeka {minutes} min"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def note_parts(head: str, lines: list[str], limit: int = NOTE_LIMIT) -> list[str]:
    """``head``, then ``lines``, packed into as few messages as possible, none longer than ``limit``.

    A line is never split between two messages; one longer than ``limit`` by itself
    (only a broken file could give that) is cut.  With real ids and 32-character
    names a line is ~200 characters, so ten of them and the header (~330) do not fit
    in one Discord message of 2000.
    """
    parts: list[str] = []
    current = _cut(head, limit)
    for line in lines:
        line = _cut(line, limit)
        if len(current) + 1 + len(line) <= limit:
            current += "\n" + line
        else:
            parts.append(current)
            current = line
    parts.append(current)
    return parts
