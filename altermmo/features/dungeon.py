"""The "Kopalnia Ostatniej Lampy" event: press your luck through eight chambers.

The owner's package of 2026-11-23 (``Opis wydarzenia.md``, ``Ogłoszenie na
Discord.txt`` and the pictures) and his messages of the same day:

* the mine comes like the other random events (the events cog draws it with
  the merchant's weight, ``!SpawnEvent dungeon`` calls it) and is PASSIVE like
  the merchant: the channel is held only while the announcement is posted,
  then the event stays open (``state.open_event``) and hunts, the tower and
  the stable work.  Its buttons wait while a hunt / tower run / event plays on
  the channel (``ui.MSG_BUSY``) and nothing of the mine is posted during a
  typed fight;
* there is ONE entrance per appearance (owner: "Wejscie do kopalni jest tylko
  jedno. Jeden gracz moze slorzystac"): the first player who clicks "Wejdź do
  kopalni" goes down - any player - and the button is used up like a chest's
  (the announcement then says who went down; everybody else gets one short
  notice).  His expedition is ONE public message, edited from chamber to
  chamber.  Entering is safe (chamber 1); "Schodzę niżej" makes exactly one
  draw with the chamber's chance (``random.randrange(denominator) <
  numerator`` - two of three is two of three, not 66%); a failed descent ends
  the expedition with nothing;
* **only taking the loot gives loot** (owner: "Bez zabrania nagrod wszystko
  przepada. Tylko zabranie nagrod daje nagrody").  "Wracam z łupem" (in the
  treasury "Odbierz łup") pays the row of the chamber the player stands in:
  XP (``expsum``), reroll scrolls, in the treasury a level for the pet at his
  side, and the eggs of the row - deeper rows hold more of them (owner:
  "Zwieksz liczbe jajek i na pietrach 4 i nizej dawaj kolejne");
* the eggs are the game's normal eggs (owner: "Jajko moze by. Tez premium
  jesli ktos wspierq" - ``pets.roll_egg``, exactly the roll of
  ``pets.assign_pet``), rolled at the moment of leaving and decided by the
  player ONE AFTER THE OTHER, each in its own transaction (owner: "gracz na
  biezaco decyduje czy wrzucic je do stajni
  czy poswiecic. Jesli nie ma miejsce w stajni to moze wybrac zajete miejsce i
  jajo zabiera miejsce tamtego peta a tamten pet zostanie poswiecony. Zapytaj
  podwojnie"): take it by the game's rule (at his side, else the pouch), put
  it into a free stable place, swap it for a stabled pet that is then
  sacrificed (two confirmations in a row) or sacrifice the egg itself (one).
  All of it goes through ``pets.place_egg`` - the game's own sacrifice path;
* the mine is CLOSED by the next event, by the boss (also when the boss cycle
  wipes the channel for its "silence") and by a restart of the bot - and, like
  a chest that was opened, by itself once its one expedition is over.  At the
  closing an expedition still in a chamber (the unclaimed treasury too) gets NOTHING;
  eggs a player took out but did not decide about go where there is room
  without a sacrifice (his side, the pouch, a free stable place) or are lost;
  one summary message says who and what.  The announcement and every panel say
  so beforehand, with the latest moment of the closing when the events cog
  knows it (:func:`closing_time`).

Paid exactly once
-----------------
Every click is handled under the expedition's lock, against the state it
finds there; a click made before the page the player is looking at was shown
(a double click, a click during the roll) is only acknowledged
(:func:`_stale`).  Everything is written to ``data/dungeon.json`` BEFORE it is
paid:

* leaving: the eggs are rolled, the state ``leaving`` is saved, THEN XP,
  scrolls and the level are paid, then ``eggs`` / ``done`` is saved with what
  was really granted;
* one egg: its fate ``pending`` is saved, THEN the one transaction of
  ``pets.place_egg`` runs, then the real fate is saved.

So nothing is ever paid twice.  A crash exactly between the two saves costs
the player that one payment / that one egg: the restart finds ``leaving`` /
``pending``, pays nothing again and tells the owner what to check and grant by
hand (as the merchant does for a hand-over that failed).  The eggs of a
payment that was interrupted are not lost with it: they are placed only after
the payment, so the restart knows that none of them is anywhere yet and places
them by the closing rule.  A payment that fails technically is reported the
same way and shown to the player.

Pictures: ``assets/events/dungeon/00.jpg`` (the entrance, in the
announcement), ``01.jpg`` .. ``08.jpg`` (the chambers) and ``NN-pet.jpg``
(shown instead for a player with a hatched pet at his side; the package has
one for the entrance - the announcement gets it once such a player went down -
and one for the bridge).  Without a picture the page is words only.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import discord

from ..core import challenge, io, timing, ui
from ..core.scene import Scene
from ..core.state import BossPhase, EventType
from ..settings import CONFIG_DIR
from . import album, events, expsum, patrons, pets

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot

log = logging.getLogger(__name__)

CONFIG_FILE = CONFIG_DIR / "dungeonConfig.json"
#: The converted pictures of the owner's package (``<chamber>.jpg``, ``00`` = the entrance).
PICTURES_DIR = events.EVENTS_DIR / "dungeon"
PICTURE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
#: Name of the supervised loop (:func:`watch`) and how often (seconds) it looks at the mine.
WATCH_NAME = "dungeon-watch"
WATCH_EVERY = 2.0
#: A click that reached the bot less than this many seconds after the page it sits on was
#: shown is taken for a second click on the PREVIOUS page (nobody reads a chamber that fast)
#: and only acknowledged - a double click must not descend twice.
CLICK_GUARD = 0.7
#: A click on a button the expedition's page no longer has (the page changed between the
#: click and its arrival: discord.py drops such a click) is acknowledged after this many
#: seconds when nobody answered it - Discord shows "interaction failed" after three.
ORPHAN_WAIT = 1.5
#: The closing summary waits this long (seconds) for whoever closed the mine to take the
#: channel (the next event clears it first), then until the channel is quiet - and is given
#: up (written to the log only) after :data:`SUMMARY_GIVE_UP` seconds of a busy channel.
SUMMARY_DELAY = 5.0
SUMMARY_GIVE_UP = 1800.0
#: Whoever closes the mine (the next event, the boss) waits at most this long (seconds) for a
#: click that is being handled right now (a payment in flight) - never for a player's answer.
#: An expedition that is still busy then is finished by :func:`tick` as soon as it is free.
CLOSE_WAIT = 5.0
#: A closing that could not be finished (a busy expedition, a state that cannot be written)
#: is tried again by :func:`tick` this many seconds later; a file that is behind the memory
#: is written again every :data:`SAVE_RETRY` seconds until it works.
RETRY_EVERY = 5.0
SAVE_RETRY = 30.0
#: The pictures cover eight chambers; a row holds at most this many eggs.
MAX_CHAMBERS = 8
MAX_EGGS = 10
#: The longest name / text of a chamber the config may hold - so that every page fits
#: Discord's limits whatever the owner writes (the pages are built once more to be sure).
NAME_MAX = 80
TEXT_MAX = 1200
#: Discord's limits of an embed: its title, its description, all of it.
EMBED_TITLE, EMBED_TEXT, EMBED_TOTAL = 256, 4096, 6000

COLOUR = 0x2F6DB5
COLOUR_LOST = 0x5C5C5C
KEY_PREFIX = ui.FLOW_PREFIX + "dungeon:"

# -- expedition states and egg fates (what ``data/dungeon.json`` holds) ----------------------

#: In a chamber / failed descent / being paid / left with eggs to decide / left and settled /
#: the mine closed while the player stood in a chamber.
CHAMBER, LOST, LEAVING, EGGS, DONE, CLOSED = "chamber", "lost", "leaving", "eggs", "done", "closed"
#: States with something still to happen.
UNFINISHED = (CHAMBER, LEAVING, EGGS)
#: An egg nobody decided about yet / one whose transaction is running; the settled fates are
#: ``pets.EGG_*`` plus: lost at the closing (no room), unknown (interrupted - the owner checks).
UNDECIDED, PENDING, EGG_LOST, EGG_UNKNOWN = "", "pending", "lost", "unknown"

# -- texts (the package's narration verbatim; the rules as the owner changed them) -----------

TITLE = "⛏️ Kopalnia Ostatniej Lampy"
INTRO = ("Od trzydziestu lat nad szybem wisiała zgaszona lampa. Dziś znów płonie. Po trzecim uderzeniu kilofa "
         "płomień kurczy się do błękitnej kropki. Nikt nie wie, czy wytrzyma do czwartego. Wracasz z łupem czy "
         "schodzisz, póki widać tory?")
#: How it works, in short (owner, 2026-11-24: "Zrób tylko wprowadzenie, takie lorowe, krótkie. Wytłumacz, na
#: czym polega" - no reward table, no chance of reaching the treasury; each chamber shows its own loot).
RULES = ("Schodzisz w głąb kopalni komora po komorze. W każdej widzisz łup, który wyniesiesz, jeśli wrócisz "
         "teraz, i szansę na zejście niżej - im głębiej, tym cenniejszy łup. Nieudane zejście to powrót z "
         "pustymi rękami.")
#: On the announcement and on every page of a running expedition.
WARNING = ("⚠️ **Kopalnia zamyka się, gdy nadejdzie następne wydarzenie albo boss{latest}. Kto do tej chwili "
           "nie zabierze łupu, traci wszystko z tej wyprawy.**")
WARNING_HOW = "Łup zabierasz przyciskiem „Wracam z łupem” (w skarbcu „Odbierz łup”)."
LATEST = " - najpóźniej {when}"
#: The last line of the announcement: before anybody went down, once somebody did, after the closing.
MSG_ONE_ENTRANCE = "⛏️ **Wejście jest tylko jedno - schodzi ten, kto pierwszy kliknie „Wejdź do kopalni”.**"
MSG_WENT_DOWN = "⛏️ Do kopalni schodzi {mention}!"
MSG_HAD_GONE_DOWN = "⛏️ Do kopalni zszedł {mention}."
MSG_ANNOUNCE_CLOSED = "⌛ Kopalnia została zamknięta."
ENTER_LABEL = "Wejdź do kopalni"

LEAVE_LABEL, DEEPER_LABEL, STABLE_LABEL = "Wracam z łupem", "Schodzę niżej", "Stajnia"
CLAIM_LABEL, REFRESH_LABEL = "Odbierz łup", "Odśwież"
EGG_ACTIVE_LABEL, EGG_POUCH_LABEL = "Weź jako towarzysza", "Do sakwy"
EGG_STABLE_LABEL, EGG_SWAP_LABEL, EGG_SACRIFICE_LABEL = "Do stajni", "Zamień w stajni", "Poświęć jajko"

PAGE_TITLE = "⛏️ {number}/{total} · {name}"
#: A chamber shows only its own loot and the chance of the next descent - what lies deeper is found
#: there (owner, 2026-11-24: "jak przejdzie do kolejnego etapu, to kolejny etap pokazuje znowu kolejną
#: nagrodę, a nie wszystko od razu").
MSG_NOW = "💰 **Łup za powrót teraz:** {loot}"
MSG_NEXT = "⬇️ **Szansa na zejście niżej:** {chance}"
MSG_TREASURY = "Masz już drogę powrotną. Zanim zamkniesz kufer, sprawdź swoją zdobycz."
MSG_TO_CLAIM = "🎁 **Do odebrania:** {loot}"
MSG_EGGS_RULE = "🥚 **Jaja:** o każdym zdecydujesz po wyjściu z łupem. {room}"
MSG_ROOM = "Teraz masz: {parts}."
MSG_NO_ROOM = ("Obecnie nie masz wolnego miejsca na jajo - po wyjściu możesz zamienić je z petem ze stajni "
               "(tamten zostanie poświęcony) albo poświęcić samo jajo.")
ROOM_ACTIVE, ROOM_POUCH, ROOM_STABLE = "wolne miejsce towarzysza", "wolną sakwę na jajko", "wolne miejsca w stajni: {count}"
MSG_PLACES_UNKNOWN = "*Nie udało się teraz sprawdzić Twojej stajni.*"
MSG_LEVEL = "🐾 **Awans:** {name} · poziom {old} → {new}. Awans przysługuje petowi aktywnemu przy odbiorze."
MSG_LEVEL_EGG = ("🐾 **Awans:** jajo przy Tobie ({name}) wykluje się · poziom 0 → 1. Awans przysługuje petowi "
                 "aktywnemu przy odbiorze.")
MSG_LEVEL_MAX = "🐾 **Awans:** {name} ma już najwyższy poziom - awansu nie będzie."
MSG_LEVEL_NONE = ("🐾 **Awans:** nie masz przy sobie towarzysza - poziom ze skarbca przepadnie (dostaje go pet "
                  "aktywny przy odbiorze).")

MSG_STABLED = "{name} odpoczywa w stajni. Masz teraz miejsce na jajo."
MSG_STABLE_NOBODY = "Nie masz przy sobie towarzysza, którego można schować do stajni."
MSG_STABLE_FULL = "W stajni nie ma wolnego miejsca - Twój towarzysz zostaje przy Tobie."

RESULT_TITLE = "⛏️ Powrót z łupem · {number}/{total} · {name}"
MSG_SAFE_RETURN = ("„Dopiero w świetle dnia sprawdzasz zawartość torby. Poniżej znajdziesz wszystko, co udało ci "
                   "się wynieść z poziomu {level}.”")
MSG_GOT_XP = "✅ {xp} XP"
MSG_GOT_SCROLLS = "✅ {scrolls} odrodzenia"
MSG_GOT_LEVEL = "✅ awans: {name} · poziom {old} → {new}"
MSG_GOT_NO_LEVEL_MAX = "➖ awans: Twój towarzysz ma już najwyższy poziom"
MSG_GOT_NO_LEVEL_NONE = "➖ awans: nie masz przy sobie towarzysza"
MSG_GOT_FAILED = ("⚠️ Nie udało się przyznać: {what} <:Sadge:936907659142111273> Administracja dostała już znać "
                  "i przyzna to ręcznie.")
MSG_BEING_PAID = "*Łup jest właśnie wypłacany...*"
MSG_PAYMENT_UNKNOWN = ("⚠️ Wypłata łupu została przerwana <:Sadge:936907659142111273> Administracja dostała już "
                       "znać i sprawdzi, co udało się przyznać.")
MSG_EGG_LIST = "🥚 **Jaja z tej wyprawy:**"
MSG_EGG_LINE = "{number}. jajo {fate}"
FATE_TEXT = {
    pets.EGG_ACTIVE: "jest teraz Twoim towarzyszem",
    pets.EGG_IN_POUCH: "trafiło do sakwy",
    pets.EGG_IN_STABLE: "trafiło do stajni (miejsce {place})",
    pets.EGG_SWAPPED: "zajęło miejsce {place} w stajni - {pet} został poświęcony",
    pets.EGG_SACRIFICED: "zostało poświęcone",
    EGG_LOST: "przepadło - nie było dla niego wolnego miejsca",
    EGG_UNKNOWN: "nie zostało potwierdzone - administracja dostała znać",
    PENDING: "jest właśnie zapisywane",
    UNDECIDED: "czeka na Twoją decyzję",
}
MSG_EGG_NOW = "🥚 **Jajo {number} z {total}** - wybierz, co z nim zrobić:"
MSG_EGG_PLAIN = "*Co z niego wyrośnie, okaże się po wykluciu.*"
#: On every page that still waits for an egg decision.
EGG_WARNING = ("⚠️ **Gdy kopalnia się zamknie (następne wydarzenie albo boss{latest}), jaja bez decyzji trafią tam, "
               "gdzie jest wolne miejsce (przy Tobie, w sakwie, w stajni) - a gdy miejsca nie ma, przepadną.**")

LOST_TITLE = "⛏️ Nieudane zejście"
MSG_FAILED_DESCENT = ("„Pod stopą osuwa się podkład. Rzucasz zdobycz i obiema dłońmi chwytasz łańcuch. Na "
                      "powierzchnię wychodzisz z pustymi rękami.”")
MSG_LOST_DETAIL = ("Zejście do komory {number} ({name}) nie powiodło się. Łup z tej wyprawy ({loot}) przepadł - "
                   "wszystko, co miałeś przed wyprawą, jest bezpieczne.")
CLOSED_TITLE = "⛏️ Kopalnia zamknięta"
MSG_CLOSED_INSIDE = ("Lampa zgasła, zanim zdążyłeś wyjść. Kopalnia zamknęła się, gdy stałeś w komorze "
                     "{number}/{total} ({name}) - łup z tej wyprawy ({loot}) przepadł.")

MSG_CLOSED = "Kopalnia jest już zamknięta."
#: The entrance is used up (as the chest's "there is no chest"): one line for everybody who clicks.
MSG_TAKEN = "Do kopalni schodzi tylko jedna osoba, a wejście jest już zajęte <:Sadge:936907659142111273>"
MSG_NOTHING_CHANGED = "*Nic się nie zmieniło - wybierz jeszcze raz.*"
MSG_TOO_LATE = "*Kopalnia zamknęła się w międzyczasie - o tym jaju zdecydowano już bez Ciebie.*"
MSG_EGG_FAILED = ("⚠️ Zapis jaja przerwał błąd <:Sadge:936907659142111273> Administracja dostała już znać i "
                  "sprawdzi, co się z nim stało.")
MSG_ALBUM_FAILED = ("⚠️ Nie udało się zapisać w albumie, więc nic nie zostało poświęcone "
                    "<:Sadge:936907659142111273> Wybierz jeszcze raz.")

MSG_SWAP_PICK = ("<@{player_id}>, wybierz towarzysza ze stajni, którego miejsce zajmie jajo. **Wybrany towarzysz "
                 "zostanie poświęcony** - zapytam jeszcze dwa razy.")
SWAP_PLACEHOLDER = "Towarzysz do poświęcenia…"
MSG_SWAP_FIRST = ("<@{player_id}>, jajo zajmie miejsce {place} w stajni, a stojący tam {pet} zostanie "
                  "**poświęcony**{kept}. Tego nie da się cofnąć. Czy na pewno? (pytanie 1 z 2) ")
MSG_SWAP_SECOND = ("<@{player_id}>, pytam drugi raz: {pet} z miejsca {place} zostanie **poświęcony na zawsze**"
                   "{kept} i nie da się tego cofnąć. Potwierdzasz? (pytanie 2 z 2) ")
MSG_SACRIFICE_EGG = ("<@{player_id}>, czy na pewno chcesz **poświęcić to jajo**{kept}? Tego nie da się cofnąć. ")
KEPT_PET = " (jako wspierający zachowasz go w albumie - **!album**)"
KEPT_EGG = " (jako wspierający zachowasz je w albumie - **!album**)"
PET_WITHOUT_DATA = "towarzysz bez danych (nr {pet_id})"

SUMMARY_TITLE = "⛏️ Kopalnia Ostatniej Lampy została zamknięta"
SUMMARY_LOST = "{players} nie zdążył wyjść przed zamknięciem kopalni - **cały łup z tej wyprawy przepadł**."
SUMMARY_LOST_MANY = "**Nie zdążyli wyjść przed zamknięciem kopalni i stracili cały łup z wyprawy:** {players}."
SUMMARY_EGGS = "**Jaja, o których nikt nie zdecydował:**"
SUMMARY_EGG_LINE = "• <@{player_id}>: {eggs}"
SUMMARY_EGG = "jajo {number} z {total} {fate}"
SUMMARY_MORE = "…i jeszcze {count}."

LOG_REWARD_FAILED = ("Kopalnia Ostatniej Lampy: gracz <@{player_id}> wyszedł z łupem z komory {chamber}, ale nie "
                     "udało się przyznać: {what} (`{error}`). Przyznaj to ręcznie.")
LOG_EGG_UNKNOWN = ("Kopalnia Ostatniej Lampy: zapis jaja gracza <@{player_id}> przerwał błąd albo restart "
                   "(`{error}`) - nie wiadomo, czy jajo zostało zapisane. Sprawdź **!stajnia** gracza i w razie "
                   "potrzeby przyznaj mu jajo ręcznie.")
LOG_PAYMENT_INTERRUPTED = ("Kopalnia Ostatniej Lampy: restart bota przerwał wypłatę łupu gracza <@{player_id}> "
                           "(komora {chamber}: {loot}). Nie wiadomo, co z tego zdążył dostać - nic nie zostało "
                           "wypłacone drugi raz. Sprawdź i w razie potrzeby przyznaj ręcznie. Jaja z tego wiersza "
                           "nie były jeszcze rozdane: trafiają teraz tam, gdzie jest wolne miejsce, jak przy "
                           "zamknięciu kopalni.")
LOG_NOT_SAVED = ("Kopalnia Ostatniej Lampy: nie udało się zapisać stanu kopalni w `data/dungeon.json` (`{error}`). "
                 "Dopóki zapis nie działa, łup nie jest wypłacany i żadne jajo nie jest rozdawane; próbuję dalej. "
                 "Sprawdź dysk i plik. Gdyby bot został zrestartowany, zanim zapis zadziała, sprawdź ręcznie łup "
                 "i jaja z tej wyprawy{player}.")
LOG_PAGE_REFUSED = ("Kopalnia Ostatniej Lampy: Discord odrzucił stronę wyprawy gracza <@{player_id}> (`{error}`). "
                    "Pokazuję ją w wersji uproszczonej (sam tekst), przyciski działają dalej. Sprawdź teksty w "
                    "`config/dungeonConfig.json` i log.")
# The same for everybody who clicks: one line on the channel is enough (see ui.reply).
ui.GENERAL_NOTICES.update({MSG_CLOSED, MSG_TAKEN})


def eggs_text(number: int) -> str:
    """``1 jajo`` / ``2 jaja`` / ``5 jaj`` (22 jaja, 12 jaj)."""
    number = int(number)
    if number == 1:
        return "1 jajo"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f"{number} jaja"
    return f"{number} jaj"


def chance_text(chance: Fraction, *, approximate: bool = False) -> str:
    """``90%`` / ``62,5%`` / ``2 na 3`` - a chance exactly as it is rolled.

    A chance that is no whole number of tenths of a percent is written as the
    fraction it is ("2 na 3": two of three equally likely results) - never
    rounded.  ``approximate`` (the chance of the whole way, a product): such a
    fraction is written as "ok. 1,23%" instead.
    """
    tenths = chance * 1000
    if tenths.denominator == 1:
        whole, rest = divmod(tenths.numerator, 10)
        return f"{whole},{rest}%" if rest else f"{whole}%"
    if approximate:
        return "ok. " + f"{float(chance) * 100:.2f}".replace(".", ",") + "%"
    return f"{chance.numerator} na {chance.denominator}"


# -- the config ----------------------------------------------------------------------------

class ConfigError(ValueError):
    """``config/dungeonConfig.json`` cannot be used (the event is skipped; the owner is told by the cog)."""


@dataclass(frozen=True)
class Chamber:
    """One chamber: the chance of the descent INTO it and the loot of returning from it."""

    number: int
    name: str
    chance: Fraction
    xp: int
    scrolls: int
    eggs: int
    pet_level: bool
    text: str

    @property
    def loot(self) -> str:
        """``110 XP + 2 zwoje + 1 jajo`` - the whole row, as the table and the pages state it."""
        parts = [f"{self.xp} XP"]
        if self.scrolls:
            parts.append(pets._scrolls(self.scrolls))
        if self.eggs:
            parts.append(eggs_text(self.eggs))
        if self.pet_level:
            parts.append("1 poziom aktywnego peta")
        return " + ".join(parts)


@dataclass(frozen=True)
class Config:
    chambers: tuple[Chamber, ...]

    @property
    def last(self) -> int:
        """The number of the treasury (the last chamber)."""
        return len(self.chambers)

    def chamber(self, number: int) -> Chamber:
        return self.chambers[int(number) - 1]

    def reach(self, number: int) -> Fraction:
        """The chance of standing in chamber ``number`` for somebody who always goes deeper."""
        chance = Fraction(1)
        for chamber in self.chambers[:int(number)]:
            chance *= chamber.chance
        return chance

    def expected_eggs(self, number: int) -> Fraction:
        """How many eggs an expedition brings on average when the player always stops at ``number``."""
        return self.reach(number) * self.chamber(number).eggs


def _chance(raw: Any) -> Fraction:
    """``"90%"`` / ``"62.5%"`` / ``"2/3"`` / ``90`` (a bare number is a percentage) as an exact fraction."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"chance {raw!r}")
    if isinstance(raw, (int, float)):
        value = Fraction(str(raw)) / 100
    else:
        text = str(raw).strip().replace(",", ".")
        if "/" in text:
            numerator, _, denominator = text.partition("/")
            value = Fraction(int(numerator.strip()), int(denominator.strip()))
        else:
            value = Fraction(text.removesuffix("%").strip()) / 100
    if not 0 < value <= 1:
        raise ValueError(f"chance {raw!r} is not above 0 and at most 100%")
    return value


def _count(entry: dict[str, Any], key: str, limit: int) -> int:
    value = entry.get(key, 0)
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= limit:
        raise ValueError(f"{key} {value!r} is not a whole number from 0 to {limit}")
    return value


def load_config(path: Path | None = None) -> Config:
    """Read and check the mine's config; :class:`ConfigError` when it is unusable.

    Read at every appearance (a change needs no restart).  The first chamber
    must be safe (100%: entering never rolls), every other chance above 0.
    """
    path = CONFIG_FILE if path is None else path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        chambers = []
        for number, entry in enumerate(raw["chambers"], start=1):
            chambers.append(Chamber(
                number, str(entry["name"]).strip(), _chance(entry["chance"]),
                _count(entry, "xp", 1_000_000), _count(entry, "scrolls", 100_000), _count(entry, "eggs", MAX_EGGS),
                bool(_count(entry, "pet_level", 1)), str(entry.get("text") or "").strip()))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ZeroDivisionError) as exc:
        raise ConfigError(f"{path.name}: {type(exc).__name__}: {exc}") from exc
    if not 2 <= len(chambers) <= MAX_CHAMBERS:
        raise ConfigError(f"{path.name}: {len(chambers)} chambers (2 to {MAX_CHAMBERS} are possible)")
    if chambers[0].chance != 1:
        raise ConfigError(f"{path.name}: the first chamber must have a chance of 100% (entering is safe)")
    if any(not chamber.name for chamber in chambers):
        raise ConfigError(f"{path.name}: a chamber has no name")
    for chamber in chambers:
        if len(chamber.name) > NAME_MAX:
            raise ConfigError(f"{path.name}: the name of chamber {chamber.number} is longer than {NAME_MAX} characters")
        if len(chamber.text) > TEXT_MAX:
            raise ConfigError(f"{path.name}: the text of chamber {chamber.number} is longer than {TEXT_MAX} characters")
    config = Config(tuple(chambers))
    too_long = page_too_long(config)
    if too_long:
        raise ConfigError(f"{path.name}: {too_long} would not fit Discord's limits - shorten the names / texts")
    return config


def page_too_long(config: Config) -> str:
    """Which page of the mine Discord would refuse with this config ('' = every page fits).

    Every page is built once with the longest inserts it can get (a full
    stable, the longest pet name, every notice, the most eggs of a row, a
    known closing time).  A page Discord refuses cannot be shown - so such a
    config is refused before the event starts (:func:`load_config`).
    """
    latest = LATEST.format(when="<t:99999999999:R>")
    player_id = 10 ** 19
    name = "*" * 60  # (escaped on the page: twice as long)
    pet = {"pet_id": 1, "pet_name": name, "pet_lvl": 1, "type": "Cat"}
    crowded = pets.EggPlaces(False, False, 99, (), (), pet)
    roomy = pets.EggPlaces(True, True, 99, tuple(range(1, 100)), ())
    notice = max(MSG_STABLED.format(name=_pet_name(pet)), MSG_STABLE_NOBODY, MSG_STABLE_FULL, MSG_PLACES_UNKNOWN,
                 ui.MSG_FAILED, key=len)
    pages = [("the announcement", announcement_embed(config, went_down=f"<@{player_id}>", latest=latest))]
    for chamber in config.chambers:
        number = chamber.number
        pages += [(f"the page of chamber {number}",
                   chamber_embed(config, player_id, number, places, latest=latest, notice=notice))
                  for places in (crowded, roomy, None)]
        eggs: list[dict[str, Any]] = [{"egg": {}, "fate": pets.EGG_SWAPPED, "place": 99, "pet": "W" * 150}
                                      for _ in range(chamber.eggs - 1)]
        if chamber.eggs:
            eggs.append({"egg": {}, "fate": UNDECIDED})
        record = {"state": EGGS if eggs else DONE, "chamber": number, "eggs": eggs,
                  "granted": {"xp": chamber.xp, "scrolls": chamber.scrolls,
                              "level": {"name": _pet_name(pet), "old": 1, "new": 2}, "failed": [chamber.loot]}}
        pages.append((f"the result page of chamber {number}",
                      result_embed(config, player_id, record, latest=latest, notice=MSG_EGG_FAILED,
                                   egg_lines=["W" * 300])))
        pages += [(f"the last page of chamber {number}", ended_embed(config, player_id, {"state": state,
                                                                                         "chamber": number}))
                  for state in (LOST, CLOSED)]
    for what, embed in pages:
        if (len(embed.title or "") > EMBED_TITLE or len(embed.description or "") > EMBED_TEXT
                or len(embed) > EMBED_TOTAL):
            return what
    return ""


# -- the pictures --------------------------------------------------------------------------

def picture(number: int, *, with_pet: bool = False) -> Path | None:
    """``assets/events/dungeon/<NN>[-pet].<jpg|...>`` of chamber ``number`` (0: the entrance), or None.

    ``with_pet`` (a hatched pet is at the player's side) prefers the "-pet"
    picture and falls back to the plain one.  Looked up every time; a missing
    picture is logged and the page is shown in words only.
    """
    names = ([f"{int(number):02d}-pet"] if with_pet else []) + [f"{int(number):02d}"]
    for name in names:
        for suffix in PICTURE_SUFFIXES:
            path = PICTURES_DIR / f"{name}{suffix}"
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except OSError:
                continue
    log.warning("The mine has no picture %02d in %s; the page is shown without one.", int(number), PICTURES_DIR)
    return None


# -- the mine and its expeditions (in memory; ``record`` is what the file holds) --------------

class Expedition:
    """One player's way through the mine: the persisted record and what lives only in memory."""

    def __init__(self, player_id: int, member: Any = None) -> None:
        self.player_id = int(player_id)
        self.member = member
        #: Exactly what ``data/dungeon.json`` holds for this player.
        self.record: dict[str, Any] = {"state": CHAMBER, "chamber": 1, "eggs": [], "granted": None}
        #: Serialises every change of the record (and its payments).
        self.lock = asyncio.Lock()
        #: Serialises the edits of the message (they are made outside :attr:`lock`).
        self.paint_lock = asyncio.Lock()
        self.message: Any = None
        self.view: "ExpeditionView | None" = None
        #: The picture attached to the message right now (uploaded again only when it changes).
        self.picture: Path | None = None
        #: The reward table is shown instead of the chamber.
        self.table = False
        #: One line about what the last click did (the pet went to the stable ...).
        self.notice = ""
        #: A question (the swap's menu, a confirmation) is open - other clicks wait for its answer.
        self.asking = False
        #: The record changed and the message does not show it yet.
        self.dirty = False
        self.version = 0
        #: When (``time.monotonic``) the page on the message was shown.
        self.shown_at = 0.0
        #: ``{egg index: (lines, picture)}`` - how each egg is shown (drawn once).
        self.previews: dict[int, tuple[list[str], Path | None]] = {}
        #: A hatched pet was at the player's side when a chamber was last shown (the "-pet" pictures).
        self.with_pet = False

    @property
    def state(self) -> str:
        return str(self.record["state"])

    @property
    def chamber(self) -> int:
        return int(self.record["chamber"])

    def touch(self) -> None:
        """The record (or what the page shows of it) changed: the message must be painted again."""
        self.version += 1
        self.dirty = True

    def egg_index(self) -> int | None:
        """Index of the egg the player decides about now (the first undecided one); None: none left."""
        return next((index for index, entry in enumerate(self.record["eggs"])
                     if entry["fate"] in (UNDECIDED, PENDING)), None)


class Mine:
    """One appearance of the mine."""

    def __init__(self, bot: "AlterBot", config: Config, channel: Any, *,
                 closes_at: datetime | None = None) -> None:
        self.bot = bot
        self.config = config
        self.channel = channel
        self.id = uuid.uuid4().hex
        self.opened = time.time()
        #: The latest moment the mine can still be open, when the events cog knows one
        #: (``EventsCog.closing_slot``: its next slot, the end of the boss's quiet hour, the tournament).
        self.closes_at = closes_at
        #: Set before anything else when the mine closes: no click starts anything from then on.
        self.closed = False
        #: The closing is finished: every expedition is closed, every undecided egg has its fate
        #: and the summary is written.  Between ``closed`` and ``settled`` the mine is CLOSING -
        #: a state of its own in the file, so that a closing that was interrupted (a shutdown, a
        #: cancelled task, an expedition that was busy) is finished later (:func:`tick`, or the
        #: next start of the bot) instead of being forgotten.
        self.settled = False
        #: One closing at a time (whoever comes second finds the mine settled).
        self.settling = asyncio.Lock()
        #: When (``time.monotonic``) :func:`tick` tries an unfinished closing again.
        self.retry_at = 0.0
        self.announcement: Any = None
        self.view: "EntranceView | None" = None
        self.picture: Path | None = None
        #: What the announcement says about the latest closing right now ('' = only the rule).
        self.shown_latest = ""
        #: The closing summary that is not posted yet ('' = none / posted).
        self.summary = ""
        #: The file is behind the memory (the last save failed): :func:`tick` writes it again,
        #: not before ``save_retry_at`` (``time.monotonic``).
        self.unsaved = False
        self.save_retry_at = 0.0
        #: The error of the save that failed ('' = none to report), whether the owner knows it.
        self.save_error = ""
        self.storage_told = False
        #: The owner was told that Discord refused a page of this mine (once is enough).
        self.page_told = False
        #: The ONE expedition of this appearance, by its player's id (empty: nobody went down yet).
        #: Filled before the first await of the click that enters, so nobody else gets in.
        self.expeditions: dict[int, Expedition] = {}

    @property
    def expedition(self) -> Expedition | None:
        return next(iter(self.expeditions.values()), None)

    def latest(self) -> str:
        """``" - najpóźniej <t:...:R>"`` while the mine is open and its latest closing is known and ahead, else ``""``."""
        if self.closed or self.closes_at is None or self.closes_at <= datetime.now(timezone.utc):
            return ""
        # Rounded UP to the second (``ui.when`` rounds down): the shown moment is never before the closing.
        return LATEST.format(when=f"<t:{math.ceil(self.closes_at.timestamp())}:R>")

    def record(self) -> dict[str, Any]:
        return {"id": self.id, "opened": self.opened, "closed": self.closed, "settled": self.settled,
                "summary": self.summary,
                "expeditions": {str(player_id): expedition.record
                                for player_id, expedition in self.expeditions.items()}}


def current(bot: "AlterBot") -> Mine | None:
    """The mine of this run of the bot (open or already closed), or None."""
    return getattr(bot, "_mine", None)


def _event_is_the_mine(bot: "AlterBot") -> bool:
    state = bot.state
    return state.event_alive and state.event_type is EventType.DUNGEON


def is_open(bot: "AlterBot", mine: Mine | None = None) -> bool:
    """True while ``mine`` (default: the current one) is open - nobody closed its event."""
    mine = current(bot) if mine is None else mine
    return mine is not None and mine is current(bot) and not mine.closed and _event_is_the_mine(bot)


def _save(mine: Mine) -> bool:
    """Write the mine and its expeditions to ``data/dungeon.json``; False (logged) when that failed.

    The caller decides what a state that was not saved means - nothing is paid
    and no egg is placed on an intent that is not on the disk.  A failed save
    is remembered (``unsaved``): :func:`tick` writes the file again until it
    works, and the owner is told once (:func:`_report_storage`).
    """
    try:
        mine.bot.store.dungeon.save({"mine": mine.record()})
    except Exception as exc:  # noqa: BLE001
        if not mine.unsaved:  # (the first failure of a row with its traceback, the next ones in one line)
            log.exception("The mine's state could not be saved.")
        else:
            log.warning("The mine's state still cannot be saved (%s: %s).", type(exc).__name__, exc)
        mine.unsaved = True
        mine.save_retry_at = time.monotonic() + SAVE_RETRY
        mine.save_error = mine.save_error or f"{type(exc).__name__}: {exc}"[:200]
        return False
    mine.unsaved = False
    return True


async def _report_storage(bot: "AlterBot", mine: Mine | None) -> None:
    """Tell the owner - once per mine - that the mine's file could not be written.  Never raises."""
    if mine is None or not mine.save_error or mine.storage_told:
        return
    mine.storage_told = True
    expedition = mine.expedition
    player = "" if expedition is None else f" gracza <@{expedition.player_id}>"
    try:
        await bot.log_to_owner(LOG_NOT_SAVED.format(error=mine.save_error, player=player), ping=True)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - it is in the log
        log.exception("Mine: the owner could not be told about the state that cannot be saved.")


def _busy(bot: "AlterBot", channel: Any) -> bool:
    """True while a hunt / tower run / event plays on the channel or a fight is being typed."""
    return bot.state.activity_running or challenge.fight_on(channel)


def _mention_only(member: Any) -> discord.AllowedMentions:
    return discord.AllowedMentions(everyone=False, roles=False, users=[member] if member is not None else False)


# -- what the pages say ----------------------------------------------------------------------

def announcement_embed(config: Config, *, went_down: str = "", latest: str = "", closed: bool = False,
                       picture_name: str | None = None) -> discord.Embed:
    """The announcement: a short introduction, how the mine works and the closing rule (no table).

    ``went_down``: the mention of the player who took the one entrance ('' = it is still free).
    ``closed``: the mine is closed - the post then warns of nothing any more (no closing rule
    in the present tense, no moment that has passed) and says who had gone down.
    """
    if closed:
        status = [*([MSG_HAD_GONE_DOWN.format(mention=went_down)] if went_down else []), MSG_ANNOUNCE_CLOSED]
        warning = []
    else:
        status = [MSG_WENT_DOWN.format(mention=went_down) if went_down else MSG_ONE_ENTRANCE]
        warning = [WARNING.format(latest=latest) + " " + WARNING_HOW]
    parts = [INTRO, RULES, *warning, "\n".join(status)]
    embed = discord.Embed(title=TITLE, description="\n\n".join(parts), color=COLOUR)
    if picture_name is not None:
        embed.set_image(url=f"attachment://{picture_name}")
    return embed


def _pet_name(pet: Any) -> str:
    return discord.utils.escape_markdown(str(pet["pet_name"] or pets.DEFAULT_NAME)[:60])


def room_line(places: pets.EggPlaces | None) -> str:
    """Where an egg could go right now, in words (the state of the egg's places on a chamber page)."""
    if places is None:
        return MSG_EGGS_RULE.format(room=MSG_PLACES_UNKNOWN)
    parts = []
    if places.active:
        parts.append(ROOM_ACTIVE)
    if places.pouch:
        parts.append(ROOM_POUCH)
    if places.free:
        parts.append(ROOM_STABLE.format(count=len(places.free)))
    return MSG_EGGS_RULE.format(room=MSG_ROOM.format(parts=", ".join(parts)) if parts else MSG_NO_ROOM)


def level_line(places: pets.EggPlaces | None) -> str:
    """Which pet would level up in the treasury (named, with the change), or why none would."""
    if places is None:
        return MSG_PLACES_UNKNOWN
    pet = places.active_pet
    if pet is None:
        return MSG_LEVEL_NONE
    level = pets._int(pet["pet_lvl"])
    if level >= pets.MAX_LEVEL:
        return MSG_LEVEL_MAX.format(name=_pet_name(pet))
    if level == 0:
        return MSG_LEVEL_EGG.format(name=_pet_name(pet))
    return MSG_LEVEL.format(name=_pet_name(pet), old=level, new=level + 1)


def chamber_embed(config: Config, player_id: int, number: int, places: pets.EggPlaces | None, *,
                  latest: str = "", notice: str = "", picture_name: str | None = None) -> discord.Embed:
    """The offer of chamber ``number``: its text, the loot of returning now and the chance of the next descent.

    Nothing of the deeper chambers is told (owner, 2026-11-24).  In the treasury:
    what waits to be claimed.  Always with the closing warning; the state of the
    player's places for an egg and the pet that would level up only when THIS
    chamber's loot holds eggs / the level.
    """
    chamber = config.chamber(number)
    lines = [f"<@{player_id}>"]
    if notice:
        lines.append(notice)
    lines.append(f"*{chamber.text}*" if chamber.text else "")
    eggs_at_stake = bool(chamber.eggs)
    level_at_stake = chamber.pet_level
    if number >= config.last:
        lines += [MSG_TREASURY, MSG_TO_CLAIM.format(loot=chamber.loot)]
    else:
        following = config.chamber(number + 1)
        lines.append(MSG_NOW.format(loot=chamber.loot) + "\n" + MSG_NEXT.format(chance=chance_text(following.chance)))
    if level_at_stake:
        lines.append(level_line(places))
    if eggs_at_stake:
        lines.append(room_line(places))
    lines.append(WARNING.format(latest=latest))
    embed = discord.Embed(title=PAGE_TITLE.format(number=number, total=config.last, name=chamber.name),
                          description="\n\n".join(line for line in lines if line), color=COLOUR)
    if picture_name is not None:
        embed.set_image(url=f"attachment://{picture_name}")
    return embed


def fate_text(entry: dict[str, Any]) -> str:
    template = FATE_TEXT.get(str(entry.get("fate", "")), FATE_TEXT[EGG_UNKNOWN])
    return template.format(place=entry.get("place", 0), pet=entry.get("pet") or "towarzysz")


def granted_lines(granted: dict[str, Any] | None) -> list[str]:
    """What the player REALLY got at leaving (and what could not be granted)."""
    if not granted:
        return [MSG_BEING_PAID]
    if granted.get("unknown"):  # (the payment was interrupted: nobody knows what of it was granted)
        return [MSG_PAYMENT_UNKNOWN]
    lines = []
    if granted.get("xp"):
        lines.append(MSG_GOT_XP.format(xp=granted["xp"]))
    if granted.get("scrolls"):
        lines.append(MSG_GOT_SCROLLS.format(scrolls=pets._scrolls(int(granted["scrolls"]))))
    level = granted.get("level")
    if isinstance(level, dict):
        lines.append(MSG_GOT_LEVEL.format(name=level.get("name", ""), old=level.get("old", 0), new=level.get("new", 0)))
    elif level == "max":
        lines.append(MSG_GOT_NO_LEVEL_MAX)
    elif level == "none":
        lines.append(MSG_GOT_NO_LEVEL_NONE)
    if granted.get("failed"):
        lines.append(MSG_GOT_FAILED.format(what=", ".join(granted["failed"])))
    return lines


def result_embed(config: Config, player_id: int, record: dict[str, Any], *, latest: str = "", notice: str = "",
                 egg_lines: Sequence[str] = (), picture_name: str | None = None) -> discord.Embed:
    """The page after "Wracam z łupem": what was granted, every egg's fate and the egg to decide now."""
    number = int(record["chamber"])
    chamber = config.chamber(number)
    lines = [f"<@{player_id}>", MSG_SAFE_RETURN.format(level=number), "\n".join(granted_lines(record.get("granted")))]
    eggs = list(record.get("eggs") or [])
    if eggs:
        lines.append(MSG_EGG_LIST + "\n" + "\n".join(
            MSG_EGG_LINE.format(number=index, fate=fate_text(entry)) for index, entry in enumerate(eggs, start=1)))
    undecided = next((index for index, entry in enumerate(eggs) if entry["fate"] in (UNDECIDED, PENDING)), None)
    if record["state"] == EGGS and undecided is not None:
        lines.append(MSG_EGG_NOW.format(number=undecided + 1, total=len(eggs)) + "\n"
                     + "\n".join(egg_lines or [MSG_EGG_PLAIN]))
        if notice:
            lines.append(notice)
        lines.append(EGG_WARNING.format(latest=latest))
    elif notice:
        lines.append(notice)
    embed = discord.Embed(title=RESULT_TITLE.format(number=number, total=config.last, name=chamber.name),
                          description="\n\n".join(line for line in lines if line), color=COLOUR)
    if picture_name is not None:
        embed.set_image(url=f"attachment://{picture_name}")
    return embed


def ended_embed(config: Config, player_id: int, record: dict[str, Any], *,
                picture_name: str | None = None) -> discord.Embed:
    """The page of an expedition that brought nothing: a failed descent, or the mine closed on the player."""
    number = int(record["chamber"])
    chamber = config.chamber(number)
    if record["state"] == CLOSED:
        title = CLOSED_TITLE
        text = MSG_CLOSED_INSIDE.format(number=number, total=config.last, name=chamber.name, loot=chamber.loot)
    else:
        title = LOST_TITLE
        target = config.chamber(min(number + 1, config.last))
        text = MSG_FAILED_DESCENT + "\n\n" + MSG_LOST_DETAIL.format(number=target.number, name=target.name,
                                                                   loot=chamber.loot)
    embed = discord.Embed(title=title, description=f"<@{player_id}>\n\n{text}", color=COLOUR_LOST)
    if picture_name is not None:
        embed.set_image(url=f"attachment://{picture_name}")
    return embed


#: One button of an expedition: ``(key, label, style, emoji)``.
Button = tuple[str, str, discord.ButtonStyle, str]


def chamber_buttons(config: Config, number: int) -> list[Button]:
    take = (("claim", CLAIM_LABEL, discord.ButtonStyle.success, "🎁") if number >= config.last
            else ("leave", LEAVE_LABEL, discord.ButtonStyle.success, "💰"))
    deeper = [] if number >= config.last else [("deeper", DEEPER_LABEL, discord.ButtonStyle.danger, "⬇️")]
    return [take, *deeper, ("stable", STABLE_LABEL, discord.ButtonStyle.secondary, "🐴")]


def egg_buttons(places: pets.EggPlaces | None) -> list[Button]:
    """The choices for one egg - only those that apply to the player's places right now.

    Take it by the game's rule (at his side, else the free pouch); a free
    stable place; when the stable has no free place, the swap for a stabled
    pet; always the sacrifice of the egg itself.  When the places could not
    be read: only "try again".
    """
    if places is None:
        return [("egg-refresh", REFRESH_LABEL, discord.ButtonStyle.secondary, "🔄")]
    buttons: list[Button] = []
    if places.active:
        buttons.append(("egg-take", EGG_ACTIVE_LABEL, discord.ButtonStyle.success, "🐾"))
    elif places.pouch:
        buttons.append(("egg-take", EGG_POUCH_LABEL, discord.ButtonStyle.success, "👝"))
    if places.free:
        buttons.append(("egg-stable", EGG_STABLE_LABEL, discord.ButtonStyle.primary, "🐴"))
    elif places.occupied:
        buttons.append(("egg-swap", EGG_SWAP_LABEL, discord.ButtonStyle.danger, "🔁"))
    buttons.append(("egg-sacrifice", EGG_SACRIFICE_LABEL, discord.ButtonStyle.secondary, "🕯️"))
    return buttons


def summary_text(lost: Sequence[int], eggs: dict[int, list[str]]) -> str:
    """The one closing summary: who lost everything, where each undecided egg went ('' = nothing to say)."""
    if not lost and not eggs:
        return ""
    lines: list[str] = []
    if lost:  # (one expedition per appearance: one player - more only in a file from before that rule)
        lines.append((SUMMARY_LOST if len(lost) == 1 else SUMMARY_LOST_MANY).format(
            players=", ".join(f"<@{player_id}>" for player_id in lost)))
    if eggs:
        lines.append(SUMMARY_EGGS)
        lines += [SUMMARY_EGG_LINE.format(player_id=player_id, eggs="; ".join(fates))
                  for player_id, fates in eggs.items()]
    text = ""
    for index, line in enumerate(lines):
        more = SUMMARY_MORE.format(count=len(lines) - index)
        if len(text) + len(line) + len(more) + 2 > 3900:
            return text + more
        text += line + "\n"
    return text.rstrip()


# -- the views -------------------------------------------------------------------------------

class EntranceView(ui.PanelView):
    """The "Wejdź do kopalni" button under the announcement: everybody may click it, the first one goes down."""

    def __init__(self, mine: Mine) -> None:
        super().__init__(owner=None, timeout=None)
        self.mine = mine
        button: discord.ui.Button = discord.ui.Button(style=discord.ButtonStyle.success, label=ENTER_LABEL,
                                                      emoji="⛏️", custom_id=KEY_PREFIX + "enter")
        button.callback = self._enter  # type: ignore[method-assign]
        self.add_item(button)

    async def _enter(self, interaction: Any) -> None:
        await enter(self.mine, interaction)


class ExpeditionView(ui.PanelView):
    """The buttons of one expedition; only its player can use them.

    The buttons change with the page (:meth:`show`); every click goes to
    :func:`clicked`.  The ids are flow ids: after a restart (the mine is closed
    by it) a click is answered as stale by ``cogs/ui.py``.
    """

    def __init__(self, mine: Mine, expedition: Expedition) -> None:
        super().__init__(owner=expedition.player_id, timeout=None)
        self.mine = mine
        self.expedition = expedition

    def show(self, buttons: Sequence[Button]) -> None:
        self.clear_items()
        for key, label, style, emoji in buttons:
            button: discord.ui.Button = discord.ui.Button(style=style, label=label, emoji=emoji,
                                                          custom_id=KEY_PREFIX + key)
            button.callback = functools.partial(self._clicked, key)  # type: ignore[method-assign]
            self.add_item(button)

    async def _clicked(self, key: str, interaction: Any) -> None:
        await clicked(self.mine, self.expedition, key, interaction)


# -- painting a page ---------------------------------------------------------------------------

async def _places(bot: "AlterBot", player_id: int) -> pets.EggPlaces | None:
    """The player's places for an egg; None (logged) when the database did not answer."""
    try:
        return await pets.egg_places(bot, player_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the page is shown without that line
        log.warning("Mine: the places of %s could not be read (%s: %s).", player_id, type(exc).__name__, exc)
        return None


def _hatched(places: pets.EggPlaces | None) -> bool:
    return places is not None and places.active_pet is not None and pets._int(places.active_pet["pet_lvl"]) > 0


async def _page(mine: Mine, expedition: Expedition) -> tuple[discord.Embed, Path | None, list[Button]]:
    """``(embed, picture, buttons)`` of what the expedition's message must show now."""
    bot, config = mine.bot, mine.config
    record, player_id = expedition.record, expedition.player_id
    state, number = expedition.state, expedition.chamber
    if state == CHAMBER:
        places = await _places(bot, player_id)
        expedition.with_pet = _hatched(places)
        shown = picture(number, with_pet=expedition.with_pet)
        name = None if shown is None else shown.name
        return (chamber_embed(config, player_id, number, places, latest=mine.latest(), notice=expedition.notice,
                              picture_name=name), shown, chamber_buttons(config, number))
    shown = expedition.picture
    if state in (LOST, CLOSED):
        return ended_embed(config, player_id, record, picture_name=None if shown is None else shown.name), shown, []
    index = expedition.egg_index() if state == EGGS else None
    egg_lines: list[str] = []
    buttons: list[Button] = []
    if index is not None:
        if index not in expedition.previews:
            guild = getattr(mine.channel, "guild", None)
            try:
                expedition.previews[index] = await pets.egg_shown(bot, guild, expedition.member or player_id,
                                                                  record["eggs"][index]["egg"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the egg is decided about in words only
                log.exception("Mine: egg %s of %s could not be shown.", index + 1, player_id)
                expedition.previews[index] = ([], None)
        egg_lines, card = expedition.previews[index]
        shown = card if card is not None else shown
        buttons = egg_buttons(await _places(bot, player_id))
    return (result_embed(config, player_id, record, latest=mine.latest(), notice=expedition.notice,
                         egg_lines=egg_lines, picture_name=None if shown is None else shown.name), shown, buttons)


async def _edit(message: Any, *, new_picture: bool, shown: Path | None, **kwargs: Any) -> Exception | None:
    """Edit an expedition's message (with the picture when it changed); the error when that failed.  Never raises."""
    def build() -> dict[str, Any]:
        built = dict(kwargs)
        if new_picture:  # (a fresh file for every attempt: they are single-use)
            built["attachments"] = [] if shown is None else [discord.File(str(shown))]
        return built

    try:
        await io.retry(lambda: message.edit(**build()), what="edit of a mine expedition")
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - usually: the message was purged with the channel
        log.info("Mine: an expedition's message could not be edited (%s: %s).", type(exc).__name__, exc)
        return exc


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def plain_page(embed: discord.Embed) -> discord.Embed:
    """``embed`` in words only, cut to what Discord takes: the fallback of a page that was refused."""
    return discord.Embed(title=_cut(embed.title or "", EMBED_TITLE), color=embed.color,
                         description=_cut(embed.description or "", min(EMBED_TEXT, EMBED_TOTAL - EMBED_TITLE)))


async def _paint_plain(mine: Mine, expedition: Expedition, embed: discord.Embed, error: Exception) -> bool:
    """The page could not be put on the message while the expedition goes on: show it some simpler way.

    A player must never be left with buttons that answer nothing (he could
    not take his loot).  So the page is shown in words only, cut to Discord's
    limits and without its picture (:func:`plain_page`) - on the same message,
    or, when that message is gone or refuses this too, on a new one with the
    same buttons.  The owner is told once per mine that Discord refused a page
    (a message somebody deleted is simply replaced).  True when the page is
    shown now; when even that failed the next click tries again.  Never raises.
    """
    bot = mine.bot
    view, old = expedition.view, expedition.message
    plain = plain_page(embed)
    gone = isinstance(error, discord.NotFound)
    shown = (not gone and view is not None
             and await _edit(old, new_picture=True, shown=None, embed=plain, view=view) is None)
    if not shown and view is not None:
        try:
            expedition.message = await view.send(mine.channel, embed=plain,
                                                 allowed_mentions=_mention_only(expedition.member))
            shown = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the next click tries again
            log.warning("Mine: the page of %s could not be posted anew either (%s: %s).", expedition.player_id,
                        type(exc).__name__, exc)
        else:
            ui._live.discard(old.id)
            if not gone:
                await ui.retire(old)  # (never raises)
    if not gone and not mine.page_told:
        mine.page_told = True
        log.error("Mine: Discord refused the page of %s (%s: %s); shown in words only: %s.", expedition.player_id,
                  type(error).__name__, error, shown)
        try:
            await bot.log_to_owner(LOG_PAGE_REFUSED.format(player_id=expedition.player_id,
                                                           error=f"{type(error).__name__}: {error}"[:200]))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - it is in the log
            log.exception("Mine: the owner could not be told about a refused page.")
    return shown


async def _paint(mine: Mine, expedition: Expedition) -> bool:
    """Show the expedition's current state on its message; True when the message shows it now.

    One painter at a time, always from the state at that moment - so the last
    edit is the truth whatever order the clicks finished in.  An expedition
    that is over loses its buttons (and its view) even when the message is gone.
    A page that cannot be shown while the expedition goes on in an open mine
    is shown in a simpler way (:func:`_paint_plain`).
    """
    async with expedition.paint_lock:
        if not expedition.dirty or expedition.message is None or expedition.view is None:
            return not expedition.dirty
        version = expedition.version
        try:
            embed, shown, buttons = await _page(mine, expedition)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next click paints again
            log.exception("Mine: the page of %s could not be built.", expedition.player_id)
            return False
        view = expedition.view
        # (While the loot is being paid the page has no buttons, but the expedition is not over.)
        over = not buttons and expedition.state not in UNFINISHED
        if not over:
            view.show(buttons)
        error = await _edit(expedition.message, new_picture=shown != expedition.picture, shown=shown,
                            embed=embed, view=None if over else view)
        done = error is None
        if not done and not over and not mine.closed:
            done, shown = await _paint_plain(mine, expedition, embed, error), None
        if over:
            view.stop()
            ui._live.discard(expedition.message.id)
        if done:
            expedition.picture = shown
            expedition.shown_at = time.monotonic()
        if (done or over) and expedition.version == version:
            expedition.dirty = False
        return done


def _stale(mine: Mine, expedition: Expedition, received: float) -> bool:
    """True for a click made on a page that is not the current one (it is only acknowledged).

    The page is being repainted (``dirty``: the click of a moment ago changed
    the state) or the click reached the bot before / right after the current
    page was shown (:data:`CLICK_GUARD`): the player cannot have read it.
    Called under the expedition's lock.
    """
    return mine.closed or expedition.dirty or received < expedition.shown_at + CLICK_GUARD


async def answer_orphan(interaction: Any) -> None:
    """Acknowledge a click on an expedition button that no longer exists.  Never raises.

    The buttons change with the page (chamber, table, eggs).  A second click
    made on the page before - "Wracam z łupem" pressed twice - names a button
    the message's view has no more; discord.py discards it without a word and
    the player would read "interaction failed".  Nothing is done for it: it is
    only acknowledged, and only when its view did not answer.  (A click under
    a message nobody listens to any more is answered by ``cogs/ui.py``.)
    """
    try:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        message = interaction.message
        if not custom_id.startswith(KEY_PREFIX) or message is None or not ui.is_live(message.id):
            return
        await asyncio.sleep(ORPHAN_WAIT)
        if not interaction.response.is_done():
            await ui.ack(interaction)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - an acknowledgement is never worth an error
        log.debug("Mine: could not acknowledge an orphan click.", exc_info=True)


# -- entering ----------------------------------------------------------------------------------

async def enter(mine: Mine, interaction: Any) -> None:
    """A click on "Wejdź do kopalni": the first player goes down - the ONE expedition of this appearance.

    Everybody after him (the same player again as well) starts nothing and
    gets one short notice, as for a chest somebody has opened.  Should the
    expedition's message fail to be posted, nothing has happened and the
    entrance is free again.
    """
    bot = mine.bot
    if not await ui.gate(interaction, unless_busy=False):
        return
    if not is_open(bot, mine):
        if mine is current(bot):
            await close(bot)  # (whoever closed the event: the mine settles now, not at the next look)
        await ui.reply(interaction, MSG_CLOSED)
        return
    if mine.expeditions:
        await ui.reply(interaction, MSG_TAKEN)
        return
    if _busy(bot, interaction.channel):
        await ui.reply(interaction, ui.MSG_BUSY)
        return
    player = interaction.user
    player_id = int(player.id)
    # Taken before the first await: of two clicks at the same moment exactly one goes down.
    expedition = Expedition(player_id, player)
    mine.expeditions[player_id] = expedition
    await ui.ack(interaction)
    async with expedition.lock:
        posted = False
        try:
            if _save(mine) and not mine.closed:
                expedition.touch()
                embed, shown, buttons = await _page(mine, expedition)
                view = ExpeditionView(mine, expedition)
                view.show(buttons)
                expedition.view = view
                extra = {} if shown is None else {"file_path": shown}
                expedition.message = await view.send(mine.channel, embed=embed,
                                                     allowed_mentions=_mention_only(player), **extra)
                expedition.picture, expedition.dirty, expedition.shown_at = shown, False, time.monotonic()
                posted = True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nothing was rolled or paid: the player may simply try again
            log.exception("Mine: the expedition of %s could not be started.", player_id)
        if not posted:
            if expedition.view is not None:
                expedition.view.stop()
            if mine.expeditions.get(player_id) is expedition and not mine.closed:
                del mine.expeditions[player_id]
                _save(mine)
    if not posted:
        await ui.reply(interaction, MSG_CLOSED if mine.closed else ui.MSG_FAILED)
        await _report_storage(bot, mine)
        return
    log.info("Mine: %s (%s) went down.", player, player_id)
    # The entrance is used up: its button goes, the announcement says who went down - and
    # shows the entrance with a companion when a hatched pet walks at the player's side.
    # The view itself keeps listening until the mine closes: a click from a client that still
    # shows the button is answered "the entrance is taken" (above), not "this event is over".
    if mine.view is not None and not mine.closed:
        extra: dict[str, Any] = {}
        entrance = picture(0, with_pet=True) if expedition.with_pet else mine.picture
        if entrance is not None and entrance != mine.picture:
            mine.picture = entrance
            extra["attachments"] = [discord.File(str(entrance))]
        latest = mine.latest()
        if await ui.retire(mine.announcement, embed=_announcement(mine), **extra):
            mine.shown_latest = latest


# -- the clicks of an expedition -----------------------------------------------------------------

async def clicked(mine: Mine, expedition: Expedition, key: str, interaction: Any) -> None:
    """One click under an expedition (its player's: the view refused everybody else)."""
    received = time.monotonic()
    bot = mine.bot
    if not await ui.gate(interaction, unless_busy=False):
        return
    if not is_open(bot, mine):
        if mine is current(bot):
            await close(bot)  # (whoever closed the event: the mine settles now, not at the next look)
        await ui.reply(interaction, MSG_CLOSED)
        return
    if _busy(bot, interaction.channel):
        await ui.reply(interaction, ui.MSG_BUSY)
        return
    expedition.member = interaction.user
    await ui.ack(interaction)
    if key.startswith("egg-"):
        await _egg_clicked(mine, expedition, key, interaction, received)
    else:
        async with expedition.lock:
            if not _stale(mine, expedition, received) and expedition.state == CHAMBER:
                if key in ("table", "back"):
                    # (Buttons of the reward table that existed until 2026-11-24, on a page drawn before
                    # the update: the page is simply drawn anew, without them.)
                    expedition.table = False
                    expedition.touch()
                elif key == "stable" and expedition.player_id in pets._awaiting_confirmation:
                    await ui.reply(interaction, pets.MSG_ALREADY_WAITING)  # as !schowajtowarzysza
                elif key == "stable":
                    await _stable(mine, expedition)
                elif key == "deeper" and expedition.chamber < mine.config.last:
                    _descend(mine, expedition)
                elif key in ("leave", "claim"):
                    await _leave(mine, expedition, interaction)
    await _paint(mine, expedition)
    await _report_storage(bot, mine)
    if expedition.state not in UNFINISHED and not mine.closed:
        # Its one expedition is over: the mine is used up, like a chest that was opened.
        log.info("Mine: the expedition of %s is over (%s) - the mine closes.", expedition.player_id, expedition.state)
        await close(bot, at_once=True)


async def _stable(mine: Mine, expedition: Expedition) -> None:
    """ "Stajnia": the pet at the player's side goes to his first free stable place.  Never rolls."""
    try:
        status, _place, pet = await pets.store_in_free_place(mine.bot, expedition.player_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - nothing was moved
        log.exception("Mine: the stable of %s failed.", expedition.player_id)
        expedition.notice = MSG_PLACES_UNKNOWN
        expedition.touch()
        return
    if status == pets._OK:
        expedition.notice = MSG_STABLED.format(name=_pet_name(pet) if pet is not None else pets.DEFAULT_NAME)
    elif status == "full":
        expedition.notice = MSG_STABLE_FULL
    else:
        expedition.notice = MSG_STABLE_NOBODY
    expedition.touch()


def _descend(mine: Mine, expedition: Expedition) -> None:
    """ "Schodzę niżej": ONE draw with the exact chance of the next chamber; the result is saved at once."""
    target = mine.config.chamber(expedition.chamber + 1)
    success = random.randrange(target.chance.denominator) < target.chance.numerator
    expedition.notice = ""
    if success:
        expedition.record["chamber"] = target.number
    else:
        expedition.record["state"] = LOST
    expedition.touch()
    _save(mine)
    log.info("Mine: %s goes down to chamber %s (%s): %s.", expedition.player_id, target.number,
             chance_text(target.chance), "success" if success else "failed - the expedition is over")


async def _leave(mine: Mine, expedition: Expedition, interaction: Any) -> None:
    """ "Wracam z łupem" / "Odbierz łup": the row of the chamber is paid - once (see the module docstring)."""
    bot = mine.bot
    player = interaction.user
    player_id = expedition.player_id
    chamber = mine.config.chamber(expedition.chamber)
    record = expedition.record
    failed: list[str] = []
    errors: list[str] = []

    eggs: list[dict[str, Any]] = []
    if chamber.eggs:
        try:
            cosmetics, frames = await pets._cosmetic_state(bot)
            eggs = [pets.roll_egg(player, cosmetics=cosmetics, frames=frames) for _ in range(chamber.eggs)]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported below with the rest
            log.exception("Mine: the eggs of %s could not be rolled.", player_id)
            failed.append(eggs_text(chamber.eggs))
            errors.append(type(exc).__name__)
    if mine.closed or expedition.state != CHAMBER:
        return  # (closed while the database was asked: nothing was taken)
    # 1. The intent is on the disk before anything is paid.
    record["state"], record["eggs"] = LEAVING, [{"egg": egg, "fate": UNDECIDED} for egg in eggs]
    expedition.notice, expedition.table = "", False
    expedition.touch()
    if not _save(mine):
        record["state"], record["eggs"] = CHAMBER, []
        expedition.notice = ui.MSG_FAILED
        return
    # 2. The payment.
    granted: dict[str, Any] = {"xp": 0, "scrolls": 0, "level": None, "failed": failed}
    scene = Scene(bot=bot, channel=mine.channel, author=player)
    if chamber.xp:
        if expsum.add_file_exp(bot, player_id, chamber.xp):
            granted["xp"] = chamber.xp
        else:
            failed.append(f"{chamber.xp} XP")
            errors.append("expSummary")
    if chamber.scrolls:
        try:
            await pets.assign_scroll(scene, chamber.scrolls, player_id)
            granted["scrolls"] = chamber.scrolls
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Mine: %s scrolls of %s could not be granted.", chamber.scrolls, player_id)
            failed.append(pets._scrolls(chamber.scrolls) + " odrodzenia")
            errors.append(type(exc).__name__)
    if chamber.pet_level:
        try:
            before = await pets._active_pet(bot.db, player_id)
            if await pets.level_up_pet(scene, player_id):
                level = pets._int(before["pet_lvl"]) if before is not None else 0
                granted["level"] = {"name": _pet_name(before) if before is not None else pets.DEFAULT_NAME,
                                    "old": level, "new": level + 1}
            else:
                granted["level"] = "none" if before is None else "max"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Mine: the pet level of %s could not be granted.", player_id)
            failed.append("1 poziom aktywnego peta")
            errors.append(type(exc).__name__)
    # 3. What was really granted is on the disk; the eggs wait for the player.
    record["granted"] = granted
    record["state"] = EGGS if eggs else DONE
    expedition.touch()
    _save(mine)
    log.info("Mine: %s (%s) left from chamber %s: %s; eggs to decide: %s.", player, player_id, chamber.number,
             granted, len(eggs))
    if failed:
        await bot.log_to_owner(LOG_REWARD_FAILED.format(player_id=player_id, chamber=chamber.number,
                                                        what=", ".join(failed), error=", ".join(errors)), ping=True)


# -- the eggs ------------------------------------------------------------------------------------

async def _egg_clicked(mine: Mine, expedition: Expedition, key: str, interaction: Any, received: float) -> None:
    """A choice for the egg the player decides about now."""
    member = interaction.user
    player_id = expedition.player_id
    ask = key in ("egg-swap", "egg-sacrifice")
    async with expedition.lock:
        if _stale(mine, expedition, received) or expedition.state != EGGS:
            return
        index = expedition.egg_index()
        if index is None or expedition.record["eggs"][index]["fate"] != UNDECIDED:
            return  # (an egg whose transaction was interrupted is never tried again)
        if expedition.asking or player_id in pets._awaiting_confirmation:
            await ui.reply(interaction, pets.MSG_ALREADY_WAITING)
            return
        expedition.notice = ""
        if key == "egg-take":
            await _settle_egg(mine, expedition, index, member, pets.EGG_TO_ROOM)
        elif key == "egg-stable":
            await _settle_egg(mine, expedition, index, member, pets.EGG_TO_STABLE)
        elif ask:
            expedition.asking = True
        else:  # "egg-refresh"
            expedition.touch()
    if not ask:
        return
    try:
        # One open question per player, shared with the pet commands: a typed "!potwierdzam"
        # must never answer two questions at once.
        with pets._confirmation_slot(player_id) as free:
            if not free:
                decision: Any = None
            elif key == "egg-swap":
                decision = await _ask_swap(mine, member)
            else:
                decision = await _ask_sacrifice(mine, member)
        async with expedition.lock:
            if decision is None:
                expedition.notice = MSG_NOTHING_CHANGED
            elif mine.closed or expedition.state != EGGS or expedition.egg_index() != index:
                expedition.notice = MSG_TOO_LATE
            elif key == "egg-swap":
                place, pet_id, name = decision
                await _settle_egg(mine, expedition, index, member, pets.EGG_SWAP, place=place,
                                  expected_pet=pet_id, pet=name)
            else:
                await _settle_egg(mine, expedition, index, member, pets.EGG_SACRIFICE)
            expedition.touch()
    finally:
        expedition.asking = False


def _may_ask(mine: Mine) -> bool:
    """True while the mine may post a question: it is open and nothing plays on its channel.

    Looked at before EVERY question of a decision: between the first "yes"
    and the second question the mine may have been closed (the channel is
    then somebody else's) or a hunt may have begun - nothing of the mine is
    posted into either; the decision simply was not made.
    """
    return is_open(mine.bot, mine) and not _busy(mine.bot, mine.channel)


async def _ask_swap(mine: Mine, member: Any) -> tuple[int, int, str] | None:
    """Which stabled pet gives its place to the egg - and two "yes" in a row; None: nothing was decided.

    The pets are listed the way the stable lists them.  Each question names
    the pet and says that it cannot be undone; a "no", no answer or a closed
    menu at any step means nothing changes - and so does a mine that was
    closed, or a channel that got busy, before a question could be asked
    (:func:`_may_ask`).  The questions are taken off the channel again.
    """
    bot = mine.bot
    player_id = int(member.id)
    places = await _places(bot, player_id)
    if places is None or places.free or not places.occupied:
        return None  # (the stable is not what the button was offered for: the page shows the new choices)
    if not _may_ask(mine):
        return None
    options = [ui.Option(f"{place}:{pet_id}",
                         (pets._pet_brief(row) if row is not None else PET_WITHOUT_DATA.format(pet_id=pet_id))[:100],
                         f"Miejsce w stajni {place}", emoji="🐾") for place, pet_id, row in places.occupied]
    chosen = await ui.pick(mine.channel, member, MSG_SWAP_PICK.format(player_id=player_id), options=options,
                           timeout=timing.timeout(pets.MENU_TIMEOUT), placeholder=SWAP_PLACEHOLDER,
                           allowed_mentions=_mention_only(member))
    await io.safe_delete(chosen.message)
    found = next(((place, pet_id, row) for place, pet_id, row in places.occupied
                  if chosen.value == f"{place}:{pet_id}"), None)
    if found is None:
        return None
    place, pet_id, row = found
    name = pets._pet_line(row) if row is not None else PET_WITHOUT_DATA.format(pet_id=pet_id)
    for question in (MSG_SWAP_FIRST, MSG_SWAP_SECOND):
        if not _may_ask(mine):
            return None
        kept = KEPT_PET if patrons.check_if_patron(member) else ""
        if not await _confirm(mine, member, question.format(player_id=player_id, place=place, pet=name, kept=kept)):
            return None
    return place, pet_id, name


async def _confirm(mine: Mine, member: Any, question: str) -> bool:
    """One "are you sure?" of an egg decision; True only for a clear "yes" to THIS question.

    ``ui.confirm`` listens for the typed word before it posts the question (a
    quick answer is never lost) - so a ``!potwierdzam`` typed once more while
    the question was still on its way to Discord would answer a question
    nobody has seen yet.  For a sacrifice that cannot be undone that is no
    "yes": a typed word counts only when its message is newer than the
    question (a click can only be made on the posted question).  The question
    is taken off the channel again.
    """
    answer = await ui.confirm(mine.bot, mine.channel, member, question + pets.CONFIRM_HINT,
                              timeout=timing.timeout(pets.CONFIRM_TIMEOUT), typed=pets.CONFIRM_WORD,
                              allowed_mentions=_mention_only(member))
    await io.safe_delete(answer.message)
    if not answer:
        return False
    if answer.typed is not None and not _newer(answer.typed, answer.message):
        log.info("Mine: a typed confirmation of %s came before its question was posted - it is no answer.",
                 getattr(member, "id", "?"))
        return False
    return True


def _newer(message: Any, than: Any) -> bool:
    """True when ``message`` was written after ``than`` (Discord's ids grow with time); False when unknown."""
    try:
        return int(message.id) > int(than.id)
    except (AttributeError, TypeError, ValueError):
        return False


async def _ask_sacrifice(mine: Mine, member: Any) -> bool | None:
    """One "are you sure?" before the egg itself is sacrificed; None: the player did not say yes."""
    if not _may_ask(mine):
        return None
    kept = KEPT_EGG if patrons.check_if_patron(member) else ""
    return True if await _confirm(mine, member, MSG_SACRIFICE_EGG.format(player_id=int(member.id), kept=kept)) else None


async def _settle_egg(mine: Mine, expedition: Expedition, index: int, member: Any, where: str, *,
                      place: int | None = None, expected_pet: int | None = None, pet: str = "") -> None:
    """Carry out the player's choice for egg ``index`` - its own transaction, its own saved step.

    Called under the expedition's lock, with no await since the caller checked
    the state.  ``pending`` is saved first (see the module docstring), then
    ``pets.place_egg`` runs, then the real fate is saved.  A place that changed
    meanwhile leaves the egg undecided ("choose again"); an error with an
    unknown outcome settles it as ``unknown`` and tells the owner - it is
    never tried twice.
    """
    bot = mine.bot
    entry = expedition.record["eggs"][index]
    entry["fate"] = PENDING
    expedition.touch()
    if not _save(mine):
        # The intent is not on the disk: nothing happens (an egg placed now would be placed
        # once more by the next start of the bot).  The player chooses again.
        entry["fate"] = UNDECIDED
        expedition.notice = ui.MSG_FAILED
        return
    scene = Scene(bot=bot, channel=mine.channel, author=member)
    try:
        placed = await pets.place_egg(scene, member, entry["egg"], where, place=place, expected_pet=expected_pet)
    except album.NotSaved as exc:  # rolled back: nothing was sacrificed, the egg is still the player's to place
        entry["fate"] = UNDECIDED
        expedition.notice = MSG_ALBUM_FAILED
        log.error("Mine: the album of %s could not be written (%s); nothing was sacrificed.", expedition.player_id, exc)
        await bot.log_to_owner(album.LOG_NOT_SAVED.format(pet_id=expected_pet or 0, player_id=expedition.player_id,
                                                          error=str(exc)[:300]))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - "could not be confirmed": never a second try
        entry["fate"] = EGG_UNKNOWN
        expedition.notice = MSG_EGG_FAILED
        log.exception("Mine: egg %s of %s could not be placed (%s).", index + 1, expedition.player_id, where)
        await bot.log_to_owner(LOG_EGG_UNKNOWN.format(player_id=expedition.player_id, error=type(exc).__name__),
                               ping=True)
    else:
        if placed.done:
            entry["fate"] = placed.status
            if placed.place:
                entry["place"] = placed.place
            if pet:
                entry["pet"] = pet
        else:
            entry["fate"] = UNDECIDED
            expedition.notice = pets.MSG_STABLE_CHANGED
    if expedition.egg_index() is None:
        expedition.record["state"] = DONE
    expedition.touch()
    _save(mine)


async def _place_leftover(bot: "AlterBot", player_id: int, entries: list[dict[str, Any]], save: Any,
                          scene: Scene | None) -> bool:
    """The closing rule for the eggs nobody decided about; False when it could not be finished now.

    Each goes where there is room without a sacrifice (``pets.EGG_ANYWHERE``:
    at the player's side, else the pouch, else a free stable place - the egg
    before may have taken the place of the next one) or is lost.  Saved step
    by step like a player's own choice: ``pending`` is on the disk BEFORE the
    egg is placed - when that cannot be written the egg is not touched and the
    closing waits (``False``; :func:`tick` or the next start goes on).  An egg
    found ``pending`` (a restart or a cancelled task in the middle of its
    transaction) is never tried again - the owner checks it.  Every egg this
    rule settled is marked (``closing``) for the summary.  ``save``: writes
    the mine's file, True when it worked.
    """
    for index, entry in enumerate(entries):
        if entry["fate"] not in (UNDECIDED, PENDING):
            continue
        if entry["fate"] == PENDING or scene is None:
            await bot.log_to_owner(LOG_EGG_UNKNOWN.format(player_id=player_id, error="restart"), ping=True)
            entry["fate"], entry["closing"] = EGG_UNKNOWN, True
            save()
            continue
        entry["fate"], entry["closing"] = PENDING, True
        if not save():
            entry["fate"] = UNDECIDED
            del entry["closing"]
            return False
        try:
            placed = await pets.place_egg(scene, player_id, entry["egg"], pets.EGG_ANYWHERE, announce=False)
        except asyncio.CancelledError:
            raise  # (it stays "pending": whoever goes on with the closing reports it, nobody places it again)
        except Exception as exc:  # noqa: BLE001 - never a second try
            log.exception("Mine: the undecided egg %s of %s could not be placed.", index + 1, player_id)
            entry["fate"] = EGG_UNKNOWN
            await bot.log_to_owner(LOG_EGG_UNKNOWN.format(player_id=player_id, error=type(exc).__name__),
                                   ping=True)
        else:
            entry["fate"] = placed.status if placed.done else EGG_LOST
            if placed.place:
                entry["place"] = placed.place
        save()
    return True


async def _close_record(bot: "AlterBot", player_id: int, record: dict[str, Any], save: Any,
                        scene: Scene | None) -> bool:
    """The closing rule for ONE expedition's record; False when it could not be finished now.

    Used for the mine of this run (under the expedition's lock) and for the
    file a previous run left behind - one rule, and every step of it can be
    repeated: what is done is skipped, what was interrupted is reported.

    * ``chamber`` (the unclaimed treasury too): the player loses everything -
      nothing is paid, nothing is rolled;
    * ``leaving`` (a payment that was interrupted: a crash, a cancelled task):
      XP, scrolls and the level are not paid again - the owner is told what to
      check.  The eggs of that row were rolled and saved BEFORE the payment
      and are placed only after it, so none of them is anywhere yet: they go
      by the closing rule like everybody's undecided eggs;
    * ``eggs``: the undecided eggs are placed or lost (:func:`_place_leftover`).
    """
    state = record.get("state")
    if state == CHAMBER:
        record["state"] = CLOSED
        save()
        return True
    if state == LEAVING:
        await bot.log_to_owner(LOG_PAYMENT_INTERRUPTED.format(
            player_id=player_id, chamber=record.get("chamber", "?"),
            loot=_row_without_eggs(record.get("chamber"))), ping=True)
        record["state"], record["granted"] = EGGS, {"unknown": True}
        save()
        state = EGGS
    if state == EGGS:
        if not await _place_leftover(bot, player_id, record.get("eggs") or [], save, scene):
            return False
        record["state"] = DONE
        save()
    return True


def _closing_summary(expeditions: dict[Any, Any]) -> str:
    """The text of the ONE closing summary, read from the records the closing left."""
    lost: list[int] = []
    eggs: dict[int, list[str]] = {}
    for key, record in expeditions.items():
        if not isinstance(record, dict):
            continue
        try:
            player_id = int(key)
        except (TypeError, ValueError):
            continue
        if record.get("state") == CLOSED:
            lost.append(player_id)
        entries = [entry for entry in record.get("eggs") or [] if isinstance(entry, dict)]
        fragments = [SUMMARY_EGG.format(number=index, total=len(entries), fate=fate_text(entry))
                     for index, entry in enumerate(entries, start=1) if entry.get("closing")]
        if fragments:
            eggs[player_id] = fragments
    return summary_text(lost, eggs)


# -- opening and closing -------------------------------------------------------------------------

class NotSettled(RuntimeError):
    """The mine before this one is not settled yet, so a new one cannot open (the cog tells the owner)."""


async def spawn_dungeon(scene: Scene, *, closes_at: datetime | None = None) -> Mine:
    """The mine appears: the announcement with the open table and the "Wejdź do kopalni" button.

    Passive like the merchant: the channel is held only while the announcement
    is posted, then the event stays open and this function returns - the
    clicks are handled by the views, the closing by :func:`close` (the events
    cog, the boss cycle, :func:`watch`).  ``closes_at``: the latest moment the
    mine can be open, when the events cog knows one (see :func:`closing_time`).
    :class:`ConfigError` propagates (the cog tells the owner) - and so does
    :class:`NotSettled`: the file holds ONE mine, so a new one opens only when
    the one before is settled (it nearly always is: the closing waits for a
    busy expedition a few seconds only, and :func:`tick` finishes the rest).
    """
    bot = scene.bot
    state = bot.state
    config = load_config()
    await close_after_restart(bot)  # (a mine of the previous run that nobody has looked at yet)
    await close(bot)  # (a mine nobody closed - the cog does it before every event)
    await _settle_leftover(bot)
    before = current(bot)
    if (before is not None and not before.settled) or _leftover(bot) is not None:
        raise NotSettled("the previous mine is still being closed (an expedition is busy or its state cannot "
                         "be written) - the mine does not open this time")
    mine = Mine(bot, config, scene.channel, closes_at=closes_at)
    view = EntranceView(mine)
    mine.view = view
    try:
        with state.hold_busy("event-spawn:DUNGEON"):
            await io.clear_channel(scene.channel)
            mine.picture = picture(0)
            extra = {} if mine.picture is None else {"file_path": mine.picture}
            mine.shown_latest = mine.latest()
            mine.announcement = await view.send(scene.channel, embed=_announcement(mine), **extra)
    except BaseException:
        view.stop()
        raise
    bot._mine = mine  # type: ignore[attr-defined]
    state.open_event(EventType.DUNGEON)
    _save(mine)
    await _report_storage(bot, mine)
    log.info("Mine spawn: %d chambers.", config.last)
    return mine


def _announcement(mine: Mine) -> discord.Embed:
    expedition = mine.expedition
    went_down = f"<@{expedition.player_id}>" if expedition is not None and expedition.message is not None else ""
    return announcement_embed(mine.config, went_down=went_down, latest=mine.latest(), closed=mine.closed,
                              picture_name=None if mine.picture is None else mine.picture.name)


async def _refresh_announcement(mine: Mine) -> None:
    """Edit the announcement when what it says about the latest closing is no longer true.  Never raises.

    The moment became known, changed - or has passed with the mine still
    open (a moment in the past is never shown).  Not while something plays on
    the channel: :func:`tick` comes back to it.
    """
    latest = mine.latest()
    if latest == mine.shown_latest or mine.announcement is None or _busy(mine.bot, mine.channel):
        return
    mine.shown_latest = latest  # (also when the edit fails: it is cosmetic, the pages say it anyway)
    try:
        await mine.announcement.edit(embed=_announcement(mine))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("Mine: the announcement could not be updated (%s: %s).", type(exc).__name__, exc)


async def closing_time(bot: "AlterBot", when: datetime | None) -> None:
    """The events cog knows the latest moment the open mine can still be open (None: it does not).  Never raises.

    Shown from now on by the announcement (edited when it changes) and by every page.
    """
    mine = current(bot)
    if mine is None or not is_open(bot, mine):
        return
    mine.closes_at = when
    await _refresh_announcement(mine)


def _events_cog(bot: "AlterBot") -> Any:
    """The loaded events cog (it knows the latest closing, ``EventsCog.closing_slot``), or None."""
    try:
        return bot.get_cog("Events")
    except Exception:  # noqa: BLE001 - no cog: the mine keeps what it was told
        return None


async def recheck_closing_time(bot: "AlterBot") -> None:
    """Ask the events cog again for the latest closing of the open mine.  Never raises.

    The moment it was told can stop being true without the event loop
    noticing: ``!stopSpawnBoss`` drops the boss's quiet hour, so the mine is
    no longer closed at its end - only at the next slot.  The boss cycle calls
    this when it drops its schedule, and :func:`tick` at every look, so the
    "najpóźniej" line is an upper bound again within seconds at the latest.
    """
    cog = _events_cog(bot)
    if cog is None:
        return
    try:
        await closing_time(bot, cog.closing_slot())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a line on a page is never worth more
        log.exception("Mine: could not ask again when it closes.")


async def close(bot: "AlterBot", *, at_once: bool = False) -> None:
    """Close the mine: only who took his loot has it.  Idempotent, never raises.

    Called by whoever ends the event (the events cog before the next event,
    the boss cycle as soon as it needs the channel), by :func:`watch` / a
    click that notices the event was closed by somebody else, and by the mine
    itself when its one expedition is over.  An expedition in a chamber gets
    nothing; undecided eggs go where there is room or are lost
    (:func:`_close_record`); the summary and the last edits of the messages
    wait for a quiet channel (:func:`_afterwards`) - and, unless ``at_once``
    (nobody is about to clear the channel), a moment for whoever closed it.

    Nobody waits for a player here: a question that is still open (the swap's
    menu, a confirmation) simply comes too late.  Only a click that is being
    handled at this very moment is waited for, :data:`CLOSE_WAIT` seconds at
    most; an expedition that is still busy then - and a closing that was
    interrupted or could not be written - is finished by :func:`tick` (or by
    the next start of the bot): the mine is ``closed`` at once and ``settled``
    when all of it is done.
    """
    mine = current(bot)
    if mine is None or mine.settled:
        return
    mine.closed = True  # before any await: from here on no click enters, rolls or pays
    try:
        if _event_is_the_mine(bot):
            bot.state.close_event()
        async with mine.settling:
            if mine.settled:
                return
            mine.retry_at = time.monotonic() + RETRY_EVERY
            _save(mine)  # "closing" is on the disk: a restart from here on goes on with the closing
            scene = Scene(bot=bot, channel=mine.channel)
            complete = True
            for expedition in list(mine.expeditions.values()):
                try:
                    complete = await _close_expedition(mine, expedition, scene) and complete
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one broken expedition must not keep the others open
                    log.exception("Mine: the expedition of %s could not be closed.", expedition.player_id)
            if complete:
                mine.summary = _closing_summary({player_id: expedition.record
                                                 for player_id, expedition in mine.expeditions.items()})
                mine.settled = True
                if not _save(mine):
                    mine.settled, mine.summary = False, ""  # (not on the disk: not done - tried again)
            await _report_storage(bot, mine)
            if not mine.settled:
                log.info("Mine: the closing is not finished yet (a busy expedition or a state that cannot be "
                         "written); the watch goes on with it.")
                return
            log.info("Mine closed: %d expedition(s); summary: %s", len(mine.expeditions), mine.summary or "-")
            _jobs(bot).append(_Job(mine=mine, summary=mine.summary, mine_id=mine.id,
                                   not_before=time.monotonic() + (0.0 if at_once else SUMMARY_DELAY)))
        if at_once:
            await _run_jobs(bot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - whoever closes the mine goes on with his own work
        log.exception("Closing the mine failed.")
        await bot.log_to_owner(f"Kopalnia Ostatniej Lampy: błąd przy zamykaniu (`{type(exc).__name__}: {exc}`) - "
                               "sprawdź w logu, co zostało rozliczone.")


async def _close_expedition(mine: Mine, expedition: Expedition, scene: Scene) -> bool:
    """The closing of one expedition of this run; False when it has to be tried again.

    Under the expedition's lock - waited for :data:`CLOSE_WAIT` seconds at
    most (a click that is being handled: its payment completes, then the
    closing rule applies to what is left).
    """
    if expedition.state not in UNFINISHED:
        return True
    try:
        await asyncio.wait_for(expedition.lock.acquire(), CLOSE_WAIT)
    except asyncio.TimeoutError:
        log.info("Mine: the expedition of %s is busy - it is closed as soon as it is free.", expedition.player_id)
        return False
    try:
        if expedition.state not in UNFINISHED:
            return True
        if expedition.state == CHAMBER and expedition.message is None:
            # Entering was still being posted: there was nothing to lose.
            mine.expeditions.pop(expedition.player_id, None)
            return True
        try:
            return await _close_record(mine.bot, expedition.player_id, expedition.record, lambda: _save(mine), scene)
        finally:
            expedition.notice = ""
            expedition.touch()
    finally:
        expedition.lock.release()


def _row_without_eggs(number: Any) -> str:
    """``180 XP + 2 zwoje`` - what the row of chamber ``number`` pays at once, for a note to the owner.

    Read from today's config (the mine of the previous run is gone); when
    that cannot be done, the row is only named.  Never raises.
    """
    try:
        chamber = load_config().chamber(int(number))
        if chamber.number != int(number):
            raise IndexError(number)
        parts = [f"{chamber.xp} XP"]
        if chamber.scrolls:
            parts.append(pets._scrolls(chamber.scrolls) + " odrodzenia")
        if chamber.pet_level:
            parts.append("1 poziom aktywnego peta")
        return " + ".join(parts)
    except Exception:  # noqa: BLE001 - the note still names the chamber
        return "XP, zwoje i poziom z wiersza tej komory"


def _leftover(bot: "AlterBot") -> dict[str, Any] | None:
    """The record of a mine the previous run left unsettled, while it is not settled yet."""
    return getattr(bot, "_mine_leftover", None)


async def close_after_restart(bot: "AlterBot") -> None:
    """Once per start of the bot: a mine the previous run left unsettled is closed from its file.  Never raises.

    A restart closes the mine like the next event does: expeditions in a
    chamber get nothing, undecided eggs are placed by the rule
    (:func:`_close_record`).  That holds for a mine that was open AND for one
    whose closing was interrupted (``closed`` but not ``settled`` in the
    file): the closing simply goes on - what is done stays, what a crash
    interrupted in the middle (``leaving``: the payment of XP, scrolls and the
    level; ``pending``: one egg) is not repeated and the owner is told what to
    check.  A closing that cannot be finished now (the file cannot be
    written) is tried again by :func:`tick`.
    """
    if getattr(bot, "_mine_restart_done", False):
        return
    bot._mine_restart_done = True  # type: ignore[attr-defined]
    if current(bot) is not None:
        return
    try:
        data = bot.store.dungeon.load()
        record = data.get("mine") if isinstance(data, dict) else None
        if not isinstance(record, dict):
            return
        if not record.get("settled"):
            bot._mine_leftover = record  # type: ignore[attr-defined]
            bot._mine_leftover_retry = 0.0  # type: ignore[attr-defined]
            await _settle_leftover(bot)
        elif record.get("summary"):
            _jobs(bot).append(_Job(mine=None, summary=str(record["summary"]), mine_id=str(record.get("id", "")),
                                   not_before=time.monotonic(), after_restart=True))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Closing the mine of the previous run failed.")
        await bot.log_to_owner(f"Kopalnia Ostatniej Lampy: błąd przy zamykaniu kopalni sprzed restartu "
                               f"(`{type(exc).__name__}: {exc}`) - sprawdź plik `dungeon.json` i log.")


async def _settle_leftover(bot: "AlterBot") -> None:
    """Go on with the closing of the mine a previous run left unsettled (see :func:`close_after_restart`).

    Nothing is placed before ``closed`` is on the disk, and no egg before its
    ``pending`` is - so a file that cannot be written costs time, never a
    second egg.  One settler at a time: whoever comes while another one is
    at it (the watch and a new mine's spawn) leaves it to him - two would
    post the summary twice and report the other's eggs as unknown.  May
    raise (the callers report it).
    """
    lock = getattr(bot, "_mine_leftover_lock", None)
    if lock is None:
        lock = bot._mine_leftover_lock = asyncio.Lock()  # type: ignore[attr-defined]
    if lock.locked():
        return
    async with lock:
        await _settle_leftover_now(bot)


async def _settle_leftover_now(bot: "AlterBot") -> None:
    """:func:`_settle_leftover` itself, under its lock."""
    record = _leftover(bot)
    if record is None or time.monotonic() < getattr(bot, "_mine_leftover_retry", 0.0):
        return
    bot._mine_leftover_retry = time.monotonic() + RETRY_EVERY  # type: ignore[attr-defined]
    expeditions = record.get("expeditions")
    expeditions = expeditions if isinstance(expeditions, dict) else {}
    failed: list[str] = []

    def save() -> bool:
        try:
            bot.store.dungeon.save({"mine": record})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("The state of the mine of the previous run could not be saved (%s: %s).",
                        type(exc).__name__, exc)
            failed.append(f"{type(exc).__name__}: {exc}"[:200])
            return False

    async def report() -> None:
        if failed and not getattr(bot, "_mine_leftover_told", False):
            bot._mine_leftover_told = True  # type: ignore[attr-defined]
            await bot.log_to_owner(LOG_NOT_SAVED.format(error=failed[0], player=""), ping=True)

    record["closed"] = True
    if not save():
        await report()
        return
    try:
        scene: Scene | None = bot.boss_scene()
    except Exception:  # noqa: BLE001 - no channel: the eggs cannot be placed, the owner checks them
        scene = None
    complete = True
    for key, entry in expeditions.items():
        try:
            if isinstance(entry, dict):
                complete = await _close_record(bot, int(key), entry, save, scene) and complete
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the other expeditions are still closed
            log.exception("Mine: the expedition %r left by the previous run could not be closed.", key)
    if complete:
        record["summary"], record["settled"] = _closing_summary(expeditions), True
        if not save():
            record["summary"], record["settled"] = "", False
    await report()
    if not record.get("settled"):
        return
    bot._mine_leftover = None  # type: ignore[attr-defined]
    log.info("Mine left unsettled by the previous run closed; summary: %s", record["summary"] or "-")
    if record["summary"]:
        _jobs(bot).append(_Job(mine=None, summary=str(record["summary"]), mine_id=str(record.get("id", "")),
                               not_before=time.monotonic(), after_restart=True))


# -- after the closing: the last edits and the summary ---------------------------------------------

@dataclass
class _Job:
    """What is still to be shown of a closed mine once the channel is quiet."""

    #: The mine of this run (its messages get their last edit); None for one closed after a restart.
    mine: Mine | None
    summary: str
    mine_id: str
    not_before: float
    after_restart: bool = False
    give_up: float = 0.0

    def __post_init__(self) -> None:
        self.give_up = self.not_before + SUMMARY_GIVE_UP


def _jobs(bot: "AlterBot") -> list[_Job]:
    jobs = getattr(bot, "_mine_jobs", None)
    if jobs is None:
        jobs = bot._mine_jobs = []  # type: ignore[attr-defined]
    return jobs


def _quiet(bot: "AlterBot", *, after_restart: bool = False) -> bool:
    """True when the mine may post on the boss channel: nobody holds it and no fight is on.

    After a restart also: the start-up clearing of the channel is done (it
    would take the summary away at once).
    """
    state = bot.state
    try:
        channel = bot.boss_channel
    except Exception:  # noqa: BLE001 - not connected yet
        return False
    if state.busy or challenge.fight_on(channel) or state.boss_phase in (BossPhase.ENGAGED, BossPhase.FIGHTING):
        return False
    return not after_restart or bool(getattr(bot.boss_cycle, "tidied", True))


async def _still_there(message: Any) -> bool:
    """True when ``message`` was not purged with the channel (one look instead of an edit per expedition)."""
    if message is None:
        return False
    try:
        await message.channel.fetch_message(message.id)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False


def _forget_views(mine: Mine) -> None:
    """Stop every view of a closed mine (their messages are gone or final)."""
    for view, message in [(mine.view, mine.announcement),
                          *((expedition.view, expedition.message) for expedition in mine.expeditions.values())]:
        if view is not None:
            view.stop()
        if message is not None:
            ui._live.discard(message.id)


async def _afterwards(bot: "AlterBot", job: _Job, *, post: bool = True) -> None:
    """The channel is quiet: the closed mine's messages get their last word, then the ONE summary."""
    mine = job.mine
    if mine is not None:
        if post and await _still_there(mine.announcement):
            if mine.view is not None:
                await mine.view.finish(embed=_announcement(mine))
            for expedition in mine.expeditions.values():
                await _paint(mine, expedition)
        _forget_views(mine)
    if not job.summary:
        return
    # "Posted" is noted before the post: a restart right here costs the summary, never a second one.
    try:
        if mine is not None and mine is current(bot):
            mine.summary = ""
            _save(mine)
        else:
            data = bot.store.dungeon.load()
            record = data.get("mine") if isinstance(data, dict) else None
            if isinstance(record, dict) and str(record.get("id", "")) == job.mine_id and record.get("summary"):
                record["summary"] = ""
                bot.store.dungeon.save({"mine": record})
    except Exception:  # noqa: BLE001
        log.exception("Mine: could not note that the closing summary is posted.")
    if not post:
        log.warning("Mine: the closing summary was never posted (the channel stayed busy): %s", job.summary)
        return
    try:
        await io.send(bot.boss_channel, embed=discord.Embed(title=SUMMARY_TITLE, description=job.summary,
                                                            color=COLOUR_LOST),
                      allowed_mentions=discord.AllowedMentions.none())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - everything it reports has happened already
        log.warning("Mine: the closing summary could not be posted (%s: %s): %s", type(exc).__name__, exc, job.summary)


async def _run_jobs(bot: "AlterBot") -> None:
    """Show what waits of closed mines, as far as the channel is quiet (:func:`_afterwards`)."""
    jobs = _jobs(bot)
    for job in list(jobs):
        now = time.monotonic()
        if now < job.not_before:
            continue
        quiet = _quiet(bot, after_restart=job.after_restart)
        if not quiet and now < job.give_up:
            continue
        jobs.remove(job)
        await _afterwards(bot, job, post=quiet)


async def tick(bot: "AlterBot") -> None:
    """One look at the mine (every :data:`WATCH_EVERY` seconds, see :func:`watch`).  Never raises.

    * the mine's event was closed by somebody who did not call :func:`close`
      (``!ResetBoss``, a summoned boss): it closes now;
    * its one expedition is over and the mine is still open (the click that
      ended it could not close it): it closes now;
    * a closing that is not finished - it was interrupted (a cancelled task),
      an expedition was busy, the file could not be written - goes on, here
      and for a mine the previous run left behind;
    * a file that is behind the memory is written again; the latest closing
      is asked for again (:func:`recheck_closing_time`) and the announcement
      no longer shows a closing time that has changed or passed;
    * what waits for a quiet channel is shown (:func:`_run_jobs`).
    """
    try:
        mine = current(bot)
        if mine is not None and not mine.settled:
            expedition = mine.expedition
            if mine.closed:
                if time.monotonic() >= mine.retry_at:
                    await close(bot)
            elif not _event_is_the_mine(bot):
                await close(bot)
            elif expedition is not None and expedition.state not in UNFINISHED and not expedition.lock.locked():
                await close(bot, at_once=True)
            else:
                await recheck_closing_time(bot)  # (the moment may have changed: !stopSpawnBoss ...)
                await _refresh_announcement(mine)
        if (mine is not None and mine.unsaved and not mine.settling.locked()
                and time.monotonic() >= mine.save_retry_at):
            _save(mine)
            await _report_storage(bot, mine)
        await _settle_leftover(bot)
        await _run_jobs(bot)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the next look tries again
        log.exception("The mine's watch failed.")


async def watch(bot: "AlterBot") -> None:
    """The supervised loop of the mine (started by the events cog)."""
    await bot.wait_until_ready()
    await close_after_restart(bot)
    while True:
        await asyncio.sleep(WATCH_EVERY)
        await tick(bot)
