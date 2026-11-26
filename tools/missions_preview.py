"""Preview every message of the companion missions ("Zlecenia") without a database or a Discord login.

What it does
------------
It builds every distinct message the feature can post - the board, the choice
of the pet, the progress, every result, the "towarzysz wrócił" post, the Zwój
przywołania, the hub panel, the help, the stable, the pet card, the patch notes
and every plain-text notice - through the REAL code of
``altermmo/features/missions.py`` (its embed builders, its ``MissionPanel``, its
``MSG_*`` constants; the pets / hub / help / patch-notes code for the places the
feature touches).  The database is replaced by a small in-memory stand-in with
sample data (the player "Łowca" and his pets) and Discord by a channel that
records what is sent, so nothing here needs PostgreSQL, a token or the network.

Each preview is ``{key, name, content, embeds, files, components, followups}``:
``embeds`` are ``discord.Embed.to_dict()`` dicts, ``files`` local paths of the
attached pictures, ``components`` the action rows exactly as discord.py would
send them; ``followups`` holds further messages of a grouped preview (the
notices) in the same shape.

Usage (from the repository root, with the project's virtualenv)::

    python tools/missions_preview.py --list
    python tools/missions_preview.py --html OUT_DIR [--screenshots]
    python tools/missions_preview.py --send CHANNEL_ID [--only KEY ...] [--yes]

``--html OUT_DIR``
    Writes ``OUT_DIR/index.html`` (all previews), one page per preview
    (``OUT_DIR/<key>.html``), ``OUT_DIR/previews.json`` and copies the attached
    pictures to ``OUT_DIR/files/``.  The pages imitate Discord's dark theme
    (markdown, mentions, ``<t:...>`` times, custom emoji from Discord's CDN,
    embeds, buttons, select menus).  ``--screenshots`` also saves one PNG per
    preview to ``OUT_DIR/png/`` with a headless Microsoft Edge / Google Chrome
    (``--browser PATH`` to choose one).

``--send CHANNEL_ID``
    Posts the previews to a Discord channel through the REST API only
    (``POST /channels/{id}/messages``) - it never connects to the gateway, so a
    bot running with the same token is not disturbed.  The token is read like
    the bot reads it (``altermmo.settings.load_settings().token``: ``TOKEN`` in
    ``.env`` or the environment).  First a header message, then before each
    preview a small line with its name.  Every button and menu is disabled (and
    its id replaced) so nobody clicks a stale flow, ``allowed_mentions`` is
    empty so nobody is pinged, 1.2 s between posts and Discord's 429
    ``retry_after`` is honoured.  WITHOUT ``--yes`` NOTHING IS SENT: it only
    prints what it would send (a dry run; no token needed).

``--only KEY`` (repeatable) limits ``--send`` / ``--html`` to the previews whose
key contains KEY (when no key does: whose name does).  ``--player-id ID`` puts a real member's id into the
mentions (e.g. your own: on Discord they then show your name instead of
"unknown user"; nobody is pinged either way).

The tool only reads the project; it changes nothing in it.  User-facing
output is Polish.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import html
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from altermmo.core import clock, ui  # noqa: E402
from altermmo.core.scene import Scene  # noqa: E402
from altermmo.core.state import BossPhase  # noqa: E402
from altermmo.features import dungeon, hub, missions, patchnotes, pets, shortcuts, skills, training  # noqa: E402

log = logging.getLogger("missions_preview")

# -- the sample data ---------------------------------------------------------------------------

DEFAULT_PLAYER_ID = 404040404040404040
PLAYER_NAME = "Łowca"
#: Another player (only in "this button is for <@Łowca>").
OTHER_PLAYER_ID = 404040404040404041
OTHER_PLAYER_NAME = "Wędrowiec"
BOT_NAME = "AlterMMO BOT"
BOT_AVATAR = "https://www.altermmo.pl/wp-content/uploads/altermmo-5-112.png"
CHANNEL_ID = 515151515151515151
#: Names the HTML gives to known mentions (the rest: "nieznany-użytkownik").
ROLE_NAMES = {985071779787730944: "Łowcy", 983798433590673448: "Wspierający"}

P_MRUCZEK, P_ISKRA, P_FENIKS, P_JAJO, P_BUREK, P_EGG1, P_EGG2 = 7101, 7102, 7103, 7104, 7105, 7106, 7107
MISSION_BOARD, MISSION_AWAY = 1201, 1188


def _pet(pet_id: int, name: str, species: str, level: int, talent: int, *, variant: str = "0",
         quality: str = "Standard", shiny: bool = False, ultra: bool = False, mythic: bool = False,
         detection: bool = False, **stats: float) -> dict[str, Any]:
    """A ``PETS`` row (lower-case keys, as asyncpg gives them)."""
    row = {"pet_id": pet_id, "pet_name": name, "pet_lvl": level, "type": species, "pet_skills": talent,
           "quality": quality, "variant": variant, "shiny": shiny, "ultra_shiny": ultra, "mythic": mythic,
           "detection": detection, "seal": "", "holo": False, "frame": "", "holo_partial": False}
    for column in ("crit_perc", "replace_perc", "def_perc", "drop_perc", "lowhp_perc", "slow_perc", "init_perc",
                   "mission_perc"):
        row[column] = stats.get(column, 0)
    return row


PETS_ROWS: dict[int, dict[str, Any]] = {
    P_MRUCZEK: _pet(P_MRUCZEK, "Mruczek", "Cat", 3, 5, crit_perc=9, def_perc=6, drop_perc=4, lowhp_perc=5,
                    detection=True),
    P_ISKRA: _pet(P_ISKRA, "Iskra", "Dragon", 2, 4, variant="1", shiny=True, slow_perc=18, drop_perc=7,
                  replace_perc=6, detection=True),
    P_FENIKS: _pet(P_FENIKS, pets.DEFAULT_NAME, "Phoenix", 3, 8, variant="2", ultra=True, replace_perc=21,
                   def_perc=24, crit_perc=12, lowhp_perc=10, mission_perc=6),
    P_JAJO: _pet(P_JAJO, pets.DEFAULT_NAME, "Sheep", 0, 0, variant="2"),
    P_BUREK: _pet(P_BUREK, "Burek", "Dog", 1, 1, crit_perc=3, def_perc=2),
    P_EGG1: _pet(P_EGG1, pets.DEFAULT_NAME, "Unicorn", 0, 0, variant="1"),
    P_EGG2: _pet(P_EGG2, "Okruszek", "Rabbit", 0, 0, variant="3"),
}

#: The player's ``PETOWNER`` row: Mruczek at his side, five stable places, the Duża sakwa with two eggs.
OWNER_ROW: dict[str, Any] = {
    "player_id": DEFAULT_PLAYER_ID, "pet_id": P_MRUCZEK, "pet_id_alt1": P_ISKRA, "pet_id_alt2": P_FENIKS,
    "pet_id_alt3": P_JAJO, "pet_id_alt4": P_BUREK, **{f"pet_id_alt{place}": 0 for place in range(5, 11)},
    "stable_places": 5, "egg_pouch": True, "big_pouch": True, "pouch_pet_id": P_EGG1, "pouch_pet_id2": P_EGG2,
    "reroll_scroll": 7, "reroll_scroll_shard": 4, "rebirth_stones": 1, "mirrors": 2, "summon_scrolls": 1,
}


# -- recording what the real code sends ------------------------------------------------------

class FakeMessage:
    _ids = iter(range(900000000000000000, 999999999999999999))

    def __init__(self, channel: "CaptureChannel", content: Any) -> None:
        self.id = next(self._ids)
        self.channel = channel
        self.content = content
        self.edits: list[dict[str, Any]] = []

    async def edit(self, **kwargs: Any) -> "FakeMessage":
        self.edits.append(kwargs)
        return self

    async def delete(self, **kwargs: Any) -> None:
        return None


class CaptureChannel:
    """A text channel that records every message the real code sends (and answers questions)."""

    def __init__(self) -> None:
        self.id = CHANNEL_ID
        self.name = "boss"
        self.guild = SimpleNamespace(id=1, filesize_limit=25 * 1024 * 1024, name="AlterMMO")
        self.sent: list[dict[str, Any]] = []
        #: How a ``ui.confirm`` question posted here is answered: "yes", "no" or None (left open).
        self.answer: str | None = None

    def typing(self) -> contextlib.AbstractAsyncContextManager:
        return contextlib.nullcontext()  # type: ignore[return-value]

    async def send(self, content: Any = None, **kwargs: Any) -> FakeMessage:
        self.sent.append(message_of(content, kwargs))
        view = kwargs.get("view")
        if isinstance(view, ui.ClickView) and self.answer is not None:
            view.clicks.put_nowait(ui.Click(self.answer, None, None))
        return FakeMessage(self, content)


def _file_path(file: Any) -> str:
    """The local path of a ``discord.File`` made by ``io.send`` from ``file_paths`` (and close it)."""
    path = getattr(getattr(file, "fp", None), "name", None) or getattr(file, "filename", "")
    with contextlib.suppress(Exception):
        file.close()
    return str(path)


def message_of(content: Any = None, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """One message in the preview shape from what ``send`` got."""
    kwargs = kwargs or {}
    embeds = list(kwargs.get("embeds") or ([kwargs["embed"]] if kwargs.get("embed") is not None else []))
    files = [_file_path(file) for file in (kwargs.get("files") or ([kwargs["file"]] if kwargs.get("file") else []))]
    files += [str(path) for path in kwargs.get("file_paths") or ()]
    if kwargs.get("file_path"):
        files.append(str(kwargs["file_path"]))
    view = kwargs.get("view")
    return {"content": None if content is None else str(content),
            "embeds": [embed.to_dict() for embed in embeds], "files": files,
            "components": view.to_components() if view is not None else []}


def shown_message(shown: Any, content: str | None = None, view: Any = None) -> dict[str, Any]:
    """A message of a ``missions.Shown`` (its embed and pictures) with ``view``'s components."""
    return message_of(content, {"embed": shown.embed, "file_paths": list(shown.files), "view": view})


class FakeDB:
    """The few reads the pets code makes, answered from the sample data (no writes happen)."""

    def __init__(self) -> None:
        self.pets = dict(PETS_ROWS)
        self.owner = dict(OWNER_ROW)
        #: ``(PET_ID, ENDS_AT, OFFERS, CHOSEN)`` of the pet on a mission, or None.
        self.away: tuple[int, datetime, str, int] | None = None

    def _away_row(self) -> dict[str, Any] | None:
        if self.away is None:
            return None
        pet_id, ends_at, offers, chosen = self.away
        return {"pet_id": pet_id, "ends_at": ends_at, "offers": offers, "chosen": chosen}

    async def fetchrow(self, sql: str, *args: Any, **_kwargs: Any) -> Any:
        if "PETOWNER" in sql:
            return self.owner
        if "PET_MISSIONS" in sql:
            row = self._away_row()
            if row is not None and "PET_ID = $1" in sql and int(args[0]) != row["pet_id"]:
                return None
            return row
        if "FROM PETS" in sql:
            return self.pets.get(int(args[0]))
        return None

    async def fetch(self, sql: str, *args: Any, **_kwargs: Any) -> list[Any]:
        if "PET_MISSIONS" in sql and "FROM PET_MISSIONS" in sql.split("WHERE")[0]:
            row = self._away_row()
            return [row] if row is not None and row["pet_id"] in [int(x) for x in args[0]] else []
        if "FROM PETS" in sql:
            away = self.away[0] if self.away is not None and "PET_MISSIONS" in sql else None
            return [self.pets[int(pet_id)] for pet_id in args[0] if int(pet_id) in self.pets and int(pet_id) != away]
        return []

    async def fetchval(self, sql: str, *args: Any, **_kwargs: Any) -> Any:
        if "PET_MISSIONS" in sql:
            row = self._away_row()
            return row["ends_at"] if row is not None else None
        return None

    async def execute(self, sql: str, *args: Any, **_kwargs: Any) -> str:
        return "UPDATE 0"


class FakeBossCycle:
    def __init__(self) -> None:
        self.can = True
        self.in_the_way = False
        self.comes = True

    def can_summon(self) -> bool:
        return self.can

    def activity_in_the_way(self) -> bool:
        return self.in_the_way

    async def summon(self, scene: Any, rarity: int) -> bool:
        return self.comes


class FakeStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.runtime: dict[str, Any] = {}

    def runtime_set(self, key: str, value: Any) -> None:
        self.runtime[key] = value

    def runtime_get(self, key: str, default: Any = None) -> Any:
        return self.runtime.get(key, default)


class FakeBot:
    """What the missions / pets code asks of ``AlterBot`` here (never connected)."""

    def __init__(self, data_dir: Path) -> None:
        self.db = FakeDB()
        self.state = SimpleNamespace(boss_phase=BossPhase.IDLE,
                                     event_alive=False, event_type=None)
        self.boss_cycle = FakeBossCycle()
        self.store = FakeStore(data_dir)
        self.settings = SimpleNamespace(private_answers=False, busy_policy="notify")
        self.owner_notes: list[str] = []
        self._startup_notes: list[str] = []
        self.ready = True

    async def log_to_owner(self, text: str, *, ping: bool = False, **_kwargs: Any) -> None:
        self.owner_notes.append(text)

    async def member(self, player_id: int) -> Any:
        return None

    async def wait_for(self, *_args: Any, **_kwargs: Any) -> Any:
        await asyncio.get_running_loop().create_future()  # never: the typed "!potwierdzam" is not awaited here

    def is_ready(self) -> bool:
        return self.ready


class Member:
    def __init__(self, player_id: int, name: str) -> None:
        self.id = player_id
        self.name = self.display_name = self.global_name = name
        self.bot = False
        self.roles: list[Any] = []
        self.guild = None

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    def __str__(self) -> str:
        return self.name


@contextlib.contextmanager
def patched(obj: Any, **values: Any) -> Iterator[None]:
    """Set attributes of a module / object for the block (the real functions are put back afterwards)."""
    old = {name: getattr(obj, name) for name in values}
    try:
        for name, value in values.items():
            setattr(obj, name, value)
        yield
    finally:
        for name, value in old.items():
            setattr(obj, name, value)


def returning(value: Any):
    async def stub(*_args: Any, **_kwargs: Any) -> Any:
        return value
    return stub


# -- the previews ---------------------------------------------------------------------------

class Builder:
    """Builds the previews with the real code (see the module docstring)."""

    def __init__(self, player_id: int, work_dir: Path) -> None:
        self.pid = player_id
        self.member = Member(player_id, PLAYER_NAME)
        OWNER_ROW["player_id"] = player_id
        self.bot = FakeBot(work_dir)
        self.channel = CaptureChannel()
        self.config = missions.load_config()
        self.previews: list[dict[str, Any]] = []
        self.now = clock.utcnow()

    # -- helpers -------------------------------------------------------------------------

    def add(self, key: str, name: str, first: dict[str, Any], *rest: dict[str, Any], note: str = "") -> None:
        preview = {"key": key, "name": name, **first, "followups": list(rest)}
        if note:
            preview["note"] = note
        self.previews.append(preview)

    def notice(self, text: str) -> dict[str, Any]:
        return message_of(text)

    def addressed(self, text: str) -> str:
        """A notice as ``ui.reply`` posts it for the clicker (his mention in front unless it is there)."""
        return ui._addressed(SimpleNamespace(user=self.member, guild=self.channel.guild), text)

    def offer(self, key: str, hours: int) -> dict[str, Any]:
        mission = missions.find_mission(self.config, key)
        if mission is None:
            raise SystemExit(f"Brak zlecenia {key!r} w config/missionsConfig.json")
        tier = self.config.tier(mission.tier)
        return missions._offer(self.config, mission, tier.hours.index(hours))

    def scene(self) -> Scene:
        return Scene(bot=self.bot, channel=self.channel, author=self.member)

    def mission_pet(self, pet_id: int, place: int) -> pets.MissionPet:
        key = f"{pets.MISSION_ACTIVE if place == 0 else place}:{pet_id}"
        return pets.MissionPet(key, place, PETS_ROWS[pet_id])

    def candidates(self) -> list[pets.MissionPet]:
        return [self.mission_pet(P_MRUCZEK, 0), self.mission_pet(P_ISKRA, 1), self.mission_pet(P_FENIKS, 2),
                self.mission_pet(P_BUREK, 4)]

    @contextlib.contextmanager
    def game(self, *, summons: int = 0, has_board: bool = True) -> Iterator[None]:
        """The pets / missions reads the real code makes, answered from the sample data."""
        with patched(pets, summon_scrolls=returning(summons), missions_table_ready=returning(True),
                     mission_columns_available=returning(True), upgrades_available=returning(True),
                     _owner_shape=returning((True, True))), \
                patched(missions, _has_board=returning(has_board)):
            yield

    async def panel(self, look: missions.Look, *, pick: int | None = None, pet_key: str | None = None,
                    sent: bool = False, summons: int = 0, has_board: bool = True) -> dict[str, Any]:
        """A view of the real ``MissionPanel`` (its embed, pictures and items)."""
        panel = missions.MissionPanel(self.member)
        if pick is not None:
            row = look.row
            panel.state, panel.choice, panel.pet_key = missions.PICK, pick, pet_key
            panel.mission_id, panel.rerolls = int(row["mission_id"]), int(row["rerolls"])
        with self.game(summons=summons, has_board=has_board):
            shown, _notice = await panel.build(self.bot, look, sent=sent)
        message = shown_message(shown, view=panel)
        panel.stop()
        return message

    # -- rows ----------------------------------------------------------------------------

    def board_offers(self) -> list[dict[str, Any]]:
        return [self.offer("owce", 4), self.offer("jelen", 8), self.offer("brama", 24)]

    def rerolled_offers(self) -> list[dict[str, Any]]:
        return [self.offer("szczury", 3), self.offer("troll", 8), self.offer("twierdza", 18)]

    def board_row(self, *, rerolls: int = 0, offers: list[dict[str, Any]] | None = None,
                  status: str = "offered") -> dict[str, Any]:
        return {"mission_id": MISSION_BOARD, "player_id": self.pid, "period_start": missions.current_period(),
                "offers": json.dumps(offers or self.board_offers(), ensure_ascii=False), "rerolls": rerolls,
                "status": status, "chosen": None, "pet_id": None, "pet_place": None, "pet_snapshot": None,
                "chance": None, "roll": None, "outcome": None, "sent_at": None, "ends_at": None, "paid_at": None,
                "result": None}

    def mission_row(self, offer: dict[str, Any], pet_id: int, place: int, *, status: str, elapsed: float = 0.0,
                    outcome: dict[str, Any] | None = None, lines: list[dict[str, Any]] | None = None,
                    mission_id: int = MISSION_AWAY, chance: int | None = None) -> dict[str, Any]:
        """A row of a pet sent on ``offer`` (``elapsed``: the share of its hours behind it)."""
        pet = PETS_ROWS[pet_id]
        hours = int(offer["hours"])
        sent_at = self.now - timedelta(hours=hours * elapsed)
        ends_at = sent_at + timedelta(hours=hours)
        chance = missions.chance_of(offer, pet) if chance is None else chance
        row = {"mission_id": mission_id, "player_id": self.pid, "period_start": missions.current_period(),
               "offers": json.dumps([offer], ensure_ascii=False), "rerolls": 0, "status": status, "chosen": 0,
               "pet_id": pet_id, "pet_place": place, "pet_snapshot": json.dumps(missions.snapshot(pet)),
               "chance": chance, "roll": (outcome or {}).get("roll", 0),
               "outcome": json.dumps(outcome or {"result": missions.SUCCESS, "roll": 0, "rewards": []},
                                     ensure_ascii=False),
               "sent_at": sent_at, "ends_at": ends_at, "due": ends_at <= self.now,
               "paid_at": self.now if status in ("paid", "recalled") else None,
               "result": (json.dumps(missions.RECALLED_RESULT) if status == "recalled" else
                          json.dumps({"lines": lines or [], "notes": []}, ensure_ascii=False)
                          if status == "paid" else None)}
        return row

    def success_line(self, key: str) -> str:
        mission = missions.find_mission(self.config, key)
        return mission.success if mission is not None else missions.MSG_SUCCESS_FALLBACK

    def outcome(self, result: str, key: str, roll: int, rewards: list[dict[str, Any]],
                *, line: str | None = None) -> dict[str, Any]:
        if line is None:
            line = self.config.failure_lines[1] if result == missions.FAIL else self.success_line(key)
        return {"result": result, "roll": roll, "line": line, "rewards": rewards}

    # -- the previews ----------------------------------------------------------------------

    async def build(self) -> list[dict[str, Any]]:
        await self.hub_and_help()
        await self.board_views()
        await self.pick_views()
        await self.progress_views()
        await self.result_views()
        await self.done_view()
        await self.questions()
        await self.summon_flow()
        await self.pet_and_stable()
        self.patch_notes()
        self.reassign_texts = await self.reassign_answers()
        self.notices()
        return self.previews

    async def reassign_answers(self) -> list[str]:
        """``!ReassignPet`` (owner) driven for real: its refusal names a pet "na zleceniu" (an inline f-string)."""
        from altermmo.cogs import pets as pets_cog

        cog = pets_cog.PetsCog(self.bot)
        ctx = SimpleNamespace(channel=self.channel, bot=self.bot, author=self.member, message=None)
        texts = []
        for done in (False, True):
            self.channel.sent.clear()
            with patched(pets, reassign_pet=returning(done)):
                await pets_cog.PetsCog.reassign_pet.callback(cog, ctx, P_ISKRA, self.pid)
            texts += [message["content"] for message in self.channel.sent if message["content"]]
        self.channel.sent.clear()
        return texts

    async def closed_panel(self) -> None:
        """The board after **Zamknij** (or 15 minutes without a click): the real ``finish`` takes the buttons off."""
        panel = missions.MissionPanel(self.member)
        with self.game():
            shown, _notice = await panel.build(self.bot, missions.Look(missions.BOARD, self.board_row()))
        panel.message = FakeMessage(self.channel, None)
        await panel.finish()
        view = panel.message.edits[-1].get("view", panel) if panel.message.edits else panel
        self.add("tablica-zamknieta", "Tablica po „Zamknij” albo po czasie (bez przycisków)",
                 shown_message(shown, view=view))

    async def hub_and_help(self) -> None:
        view = hub.HubView()
        self.add("panel-gry", "Panel gry z przyciskiem Zlecenia",
                 message_of(None, {"embed": hub.panel_embed(), "view": view}))
        view.stop()
        from altermmo.cogs import help as help_cog

        self.add("pomoc", "Pomoc (!pomoc) z sekcją Zlecenia", message_of(None, {"embed": help_cog.help_embed()}))

    async def board_views(self) -> None:
        row = self.board_row()
        self.add("tablica", "Tablica zleceń (3 oferty, jedna epicka)",
                 await self.panel(missions.Look(missions.BOARD, row)))
        rerolled = self.board_row(rerolls=1, offers=self.rerolled_offers())
        self.add("tablica-po-losowaniu", "Tablica po Nowych zleceniach (bez przycisku losowania, 2 Zwoje przywołania)",
                 await self.panel(missions.Look(missions.BOARD, rerolled), summons=2))
        await self.closed_panel()

    async def pick_views(self) -> None:
        row = self.board_row()
        look = missions.Look(missions.BOARD, row)
        self.add("wybor-towarzysza", "Wybór towarzysza - epickie zlecenie, najlepszy jest pet ze stajni",
                 await self.panel(look, pick=2))
        rerolled = missions.Look(missions.BOARD, self.board_row(rerolls=1, offers=self.rerolled_offers()))
        self.add("wybor-towarzysza-przy-tobie", "Wybór towarzysza - wybrany pet przy Tobie (ostrzeżenie)",
                 await self.panel(rerolled, pick=2, pet_key=f"{pets.MISSION_ACTIVE}:{P_MRUCZEK}"))
        self.add("wybor-towarzysza-rzadkie", "Wybór towarzysza - rzadkie zlecenie, pet spełnia wymaganie w pełni",
                 await self.panel(rerolled, pick=1, pet_key=f"2:{P_FENIKS}"))

    async def progress_views(self) -> None:
        offer = self.offer("brama", 24)
        stages = (("wyslany", "Potwierdzenie wysłania (zlecenie w toku, 0%)", 0.0, True, False),
                  ("postep-35", "Postęp ~35%", 0.35, False, False),
                  ("postep-60", "Postęp ~60%", 0.6, False, False),
                  ("postep-92", "Postęp - prawie wrócił (~92%)", 0.92, False, False),
                  ("postep-koniec", "Postęp - czas minął (wraca, zaraz rozliczenie)", 1.0002, False, True))
        for key, name, elapsed, sent, due in stages:
            row = self.mission_row(offer, P_ISKRA, 1, status="away", elapsed=elapsed)
            row["due"] = due
            self.add(key, name, await self.panel(missions.Look(missions.PROGRESS, row), sent=sent))
        row = self.mission_row(self.offer("jelen", 8), P_MRUCZEK, 0, status="away", elapsed=0.4)
        self.add("postep-przy-tobie", "Postęp - pet przy Tobie na zleceniu, z Zwojem przywołania",
                 await self.panel(missions.Look(missions.PROGRESS, row), summons=1))

    def result_rows(self) -> list[tuple[str, str, dict[str, Any], bool]]:
        """``(key, name, row, has_board)`` of every result variant."""
        rows = []
        owce = self.offer("owce", 4)
        rows.append(("wynik-sukces", "Wynik - sukces (jajko do sakwy)", self.mission_row(
            owce, P_MRUCZEK, 0, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.SUCCESS, "owce", 31, [{"kind": "scrolls", "n": 1},
                                                                {"kind": "shards", "n": 3}, {"kind": "egg"}]),
            lines=[{"kind": "scrolls", "n": 1}, {"kind": "shards", "n": 3},
                   {"kind": "egg", "class": "egg", "status": "pouch", "place": 0, "pet_id": 7201,
                    "mythic": False, "shiny": True, "ultra": False}]), False))
        karawana = self.offer("karawana", 12)
        rows.append(("wynik-miejsce", "Wynik - sukces z miejscem w stajni (miniatura)", self.mission_row(
            karawana, P_FENIKS, 2, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.SUCCESS, "karawana", 40, []),
            lines=[{"kind": "scrolls", "n": 1}, {"kind": "shards", "n": 5}, {"kind": "stone"},
                   {"kind": "place", "places": 6}]), True))
        brama = self.offer("brama", 24)
        rows.append(("wynik-wielki-sukces", "Wynik - Wielki sukces z wieloma nagrodami", self.mission_row(
            brama, P_ISKRA, 1, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.GREAT, "brama", 2, []),
            lines=[{"kind": "place", "places": 7}, {"kind": "pouch", "to": "big"}, {"kind": "summon", "count": 1},
                   {"kind": "egg", "class": "great_egg", "status": "stable", "place": 6, "pet_id": 7202,
                    "mythic": False, "shiny": False, "ultra": True},
                   {"kind": "egg", "class": "rare_egg", "status": "pouch", "place": 0, "pet_id": 7203,
                    "mythic": True, "shiny": False, "ultra": False},
                   {"kind": "level", "name": "Iskra", "new": 3},
                   {"kind": "scrolls", "n": 4}, {"kind": "shards", "n": 16}, {"kind": "stone"},
                   {"kind": "mirror"}]), False))
        grobowiec = self.offer("grobowiec", 18)
        rows.append(("wynik-duza-sakwa", "Wynik - Duża sakwa (miniatura)", self.mission_row(
            grobowiec, P_FENIKS, 2, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.SUCCESS, "grobowiec", 25, []),
            lines=[{"kind": "pouch", "to": "big"}, {"kind": "scrolls", "n": 2}, {"kind": "shards", "n": 6}]), True))
        kaplica = self.offer("kaplica", 6)
        rows.append(("wynik-sakwa", "Wynik - Sakwa na jajko (miniatura)", self.mission_row(
            kaplica, P_MRUCZEK, 0, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.SUCCESS, "kaplica", 12, []),
            lines=[{"kind": "pouch", "to": "pouch"}, {"kind": "shards", "n": 3}]), True))
        wyrocznia = self.offer("wyrocznia", 12)
        rows.append(("wynik-nadzwyczajne-jajko", "Wynik - Nadzwyczajne jajko (miniatura)", self.mission_row(
            wyrocznia, P_FENIKS, 2, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.SUCCESS, "wyrocznia", 20, []),
            lines=[{"kind": "egg", "class": "great_egg", "status": "active", "place": 0, "pet_id": 7204,
                    "mythic": False, "shiny": False, "ultra": True}, {"kind": "scrolls", "n": 1},
                   {"kind": "shards", "n": 5}]), True))
        jelen = self.offer("jelen", 8)
        rows.append(("wynik-rzadkie-jajko", "Wynik - Rzadkie jajko (miniatura)", self.mission_row(
            jelen, P_ISKRA, 1, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.SUCCESS, "jelen", 18, []),
            lines=[{"kind": "egg", "class": "rare_egg", "status": "stable", "place": 5, "pet_id": 7205,
                    "mythic": False, "shiny": True, "ultra": False}, {"kind": "shards", "n": 4}]), True))
        rows.append(("wynik-porazka", "Wynik - porażka", self.mission_row(
            brama, P_ISKRA, 1, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.FAIL, "brama", 77, [{"kind": "shards", "n": 2}]),
            lines=[{"kind": "shards", "n": 2}]), False))
        rows.append(("wynik-zawolany", "Wynik - zawołany z powrotem", self.mission_row(
            brama, P_ISKRA, 1, status="recalled", elapsed=0.4,
            outcome=self.outcome(missions.SUCCESS, "brama", 5, [])), True))
        smok = self.offer("smok", 24)
        rows.append(("wynik-rekompensaty", "Wynik - nagrody zamienione na zwoje (rekompensaty)", self.mission_row(
            smok, P_FENIKS, 2, status="paid", elapsed=1.1,
            outcome=self.outcome(missions.SUCCESS, "smok", 9, []),
            lines=[{"kind": "place", "comp": {"scrolls": 10}}, {"kind": "pouch", "comp": {"scrolls": 15}},
                   {"kind": "egg", "class": "egg", "comp": {"shards": 3}},
                   {"kind": "egg", "class": "rare_egg", "comp": {"scrolls": 1}},
                   {"kind": "egg", "class": "great_egg", "comp": {"scrolls": 3}},
                   {"kind": "level", "name": "Feniks", "comp": {"shards": 3}},
                   {"kind": "scrolls", "n": 2}, {"kind": "shards", "n": 5}]), True))
        rows.append(("wynik-niedostepne", "Wynik - nagrody chwilowo niedostępne (brak kolumn w bazie)",
                     self.mission_row(smok, P_FENIKS, 2, status="paid", elapsed=1.1,
                                      outcome=self.outcome(missions.SUCCESS, "smok", 9, []),
                                      lines=[{"kind": "place", "off": True, "comp": {"scrolls": 10}},
                                             {"kind": "pouch", "off": True, "comp": {"scrolls": 15}},
                                             {"kind": "summon", "off": True, "comp": {"scrolls": 20}},
                                             {"kind": "scrolls", "n": 1}]), True))
        lost = dict(owce, key="stare_zlecenie")
        rows.append(("wynik-bez-konfiguracji", "Wynik - zlecenia nie ma już w konfiguracji (tekst zastępczy)",
                     self.mission_row(lost, P_MRUCZEK, 0, status="paid", elapsed=1.1, chance=70,
                                      outcome=self.outcome(missions.SUCCESS, "stare_zlecenie", 3, [],
                                                           line=missions.MSG_SUCCESS_FALLBACK),
                                      lines=[{"kind": "shards", "n": 2}]), True))
        return rows

    async def result_views(self) -> None:
        rows = self.result_rows()
        for key, name, row, has_board in rows:
            summons = 1 if key == "wynik-wielki-sukces" else 0
            self.add(key, name, await self.panel(missions.Look(missions.RESULT, row), summons=summons,
                                                 has_board=has_board))
        # The notifier's post (missions._announce): the ping, the result, the "Zlecenia" shortcut.
        _key, _name, row, _has_board = rows[1]
        shown = missions.result_embed(self.pid, row, missions._config_or_none(), next_board=False)
        view = shortcuts.missions_view(self.pid, int(row["mission_id"]))
        self.add("powrot-ogloszenie", "Ogłoszenie „towarzysz wrócił” (z przyciskiem Zlecenia)",
                 shown_message(shown, missions.MSG_RETURN_PING.format(player_id=self.pid), view))
        view.stop()

    async def done_view(self) -> None:
        row = self.mission_row(self.offer("owce", 4), P_MRUCZEK, 0, status="paid", elapsed=1.1,
                               outcome=self.outcome(missions.SUCCESS, "owce", 31, []),
                               lines=[{"kind": "shards", "n": 3}], mission_id=MISSION_BOARD)
        self.add("na-dzis-koniec", "Dzisiejsze zlecenie już za Tobą (wróć jutro)",
                 await self.panel(missions.Look(missions.DONE, row), summons=1))

    async def questions(self) -> None:
        """The recall question as ``pets._confirm`` posts it (the real ``ui.confirm``; answered "no")."""
        self.channel.sent.clear()
        self.channel.answer = "no"
        name = missions._pet_name(self.mission_row(self.offer("brama", 24), P_ISKRA, 1, status="away"))
        await pets._confirm(self.scene(), self.member,
                            missions.MSG_RECALL_QUESTION.format(name=name, player_id=self.pid),
                            allowed_mentions=missions._mention_of(self.pid))
        self.add("pytanie-zawolaj", "Pytanie: Zawołaj z powrotem", *self.channel.sent[:1])
        self.channel.sent.clear()

    async def summon_flow(self) -> None:
        """``missions.use_summon_scroll`` driven for real: the question and every outcome."""
        bot, channel = self.bot, self.channel

        async def run(*, scrolls: int = 1, columns: bool = True, answer: str | None = "yes") -> list[dict]:
            channel.sent.clear()
            channel.answer = answer
            with patched(pets, mission_columns_available=returning(columns), summon_scrolls=returning(scrolls),
                         take_summon_scroll=returning(max(scrolls - 1, 0)), return_summon_scroll=returning(None)):
                await missions.use_summon_scroll(self.scene())
            sent = list(channel.sent)
            channel.sent.clear()
            return sent

        cycle = bot.boss_cycle
        cycle.can, cycle.in_the_way, cycle.comes = True, False, True
        done = await run()
        question, outcome_done = done[0], done[-1]
        self.add("przywolanie-pytanie", "Zwój przywołania - pytanie (!przywolaj / przycisk)", question)
        cycle.comes = False
        failed = (await run())[-1]
        cycle.comes = True
        none = (await run(scrolls=0))[-1]
        off = (await run(columns=False))[-1]
        cycle.can = False
        there = (await run())[-1]
        cycle.can, cycle.in_the_way = True, True
        busy = (await run())[-1]
        cycle.in_the_way = False
        async with missions._summoning:  # the player's own question is still open
            missions._summoner["player"] = self.pid
            waiting_again = (await run())[-1]
            missions._summoner["player"] = self.pid + 1  # somebody else reads one now
            other = (await run())[-1]
            missions._summoner.pop("player", None)
        self.add("przywolanie-wyniki", "Zwój przywołania - wszystkie odpowiedzi", outcome_done, failed, none, off,
                 there, busy, waiting_again, other,
                 note="Kolejno: boss przybył, boss nie przybył (zwój wraca), brak zwoju, zwoje wyłączone, boss już "
                      "jest, trwa inne wydarzenie, ponowne kliknięcie w trakcie pytania, ktoś inny właśnie "
                      "odczytuje zwój. Sam mityczny boss to już zwykły post bossa (nie część Zleceń).")

    async def pet_and_stable(self) -> None:
        """``pets.show_pet`` and ``pets.check_stable`` driven for real with a pet on a mission."""
        db, channel = self.bot.db, self.channel
        brama = self.offer("brama", 24)
        ends_at = self.now + timedelta(hours=15, minutes=20)
        db.away = (P_ISKRA, ends_at, json.dumps([brama]), 0)
        with self.game(summons=1):
            channel.sent.clear()
            await pets.check_stable(self.scene())
            stable = [m for m in channel.sent if m["embeds"]][-1]
            self.add("stajnia-na-zleceniu", "Stajnia: pet na zleceniu i Duża sakwa z dwoma jajkami", stable)
            db.owner = dict(OWNER_ROW, pet_id=P_ISKRA, pet_id_alt1=P_MRUCZEK)
            channel.sent.clear()
            await pets.check_stable(self.scene())
            side = [m for m in channel.sent if m["embeds"]][-1]
            self.add("stajnia-przy-tobie-na-zleceniu", "Stajnia: towarzysz przy Tobie jest na zleceniu", side)
            channel.sent.clear()
            await pets.show_pet(self.scene(), self.member)
            card = [m for m in channel.sent if m["embeds"]][-1]
            self.add("towarzysz-na-zleceniu", "Karta towarzysza (!towarzysz) z notką „Na zleceniu”", card)
        db.owner, db.away = dict(OWNER_ROW), None
        channel.sent.clear()

    def patch_notes(self) -> None:
        notes = [note for note in patchnotes.load_notes() if "zlecen" in (note.title or "").lower()]
        posts = patchnotes.plan_posts(notes[-1:])
        messages = [message_of(post.content or None, {"embed": post.embed()}) for post in posts]
        self.add("patch-notes", "Wpis w patch notes", *messages)

    def notices(self) -> None:
        pid = self.pid
        f = {"player_id": pid}
        when_next = ui.when(clock.utcnow() + timedelta(hours=11))
        panel = [
            missions.MSG_UNAVAILABLE.format(**f), missions.MSG_NO_PET.format(**f),
            self.addressed(missions.MSG_PANEL_OLD.format(**f)),
            self.addressed(missions.MSG_NOT_CHOSEN.format(**f)),
            self.addressed(missions.MSG_SEND_CHECK.format(**f)),
            self.addressed(missions.MSG_SEND_CHANGED.format(**f)),
            self.addressed(missions.MSG_SEND_CHANCE.format(name="Iskra", chance=38, **f)),
            self.addressed(missions.MSG_SEND_EXPIRED.format(**f)),
            self.addressed(missions.MSG_SEND_GONE.format(**f)),
            self.addressed(missions.MSG_SEND_BOSS.format(**f)),
            self.addressed(pets.MSG_ALREADY_WAITING),
            self.addressed(missions.MSG_RECALL_LATE.format(name="Iskra", **f)),
            self.addressed(hub.MSG_DB_DOWN),
        ]
        self.add("komunikaty-panelu", "Komunikaty tablicy i wysyłania (tekst)", *map(self.notice, panel),
                 note="Kolejno: zlecenia niedostępne, brak towarzysza, tablica się zmieniła, nie wybrano "
                      "towarzysza, na liście inny towarzysz, stajnia się zmieniła, szansa się zmieniła, "
                      "zlecenia wygasły, zlecenie już wybrane, trwa walka z bossem, czekam na potwierdzenie, "
                      "zawołanie za późno (wraca sam), baza danych niedostępna.")
        price = pets._scrolls(self.config.reroll_price)
        reroll = [
            self.addressed(missions.MSG_REROLL_PRICE.format(price=price, **f)),
            self.addressed(missions.MSG_REROLL_POOR.format(price=price, have=pets._scrolls(3), **f)),
            self.addressed(missions.MSG_REROLL_USED.format(when=when_next, **f)),
        ]
        self.add("komunikaty-nowe-zlecenia", "Nowe zlecenia - odmowy (tekst)", *map(self.notice, reroll),
                 note="Przycisk „Nowe zlecenia” działa od razu, bez pytania o potwierdzenie - to są jedyne "
                      "odpowiedzi, jakie może dać.")
        back = ui.when(clock.utcnow() + timedelta(hours=15, minutes=20))
        other = [
            pets.MSG_PET_AWAY.format(name="Iskra", when=back, **f),
            pets.MSG_SLOT_AWAY.format(slot=1, when=back, **f),
            pets.MSG_PET_RETURNING.format(**f),
            pets.STABLE_SIDE_AWAY,
            training.MSG_TRAINING_AWAY.format(when=back),
            dungeon.MSG_LEVEL_AWAY,
            dungeon.MSG_STABLE_AWAY,
            dungeon.MSG_GOT_NO_LEVEL_AWAY,
            skills.MSG_TRAVELLER_PET_AWAY.format(when=back, **f),
            pets.MSG_EGG_TO_BIG_POUCH.format(filled=2, **f),
            pets.MSG_POUCH_TAKEN.format(**f) + pets.MSG_POUCH_ONE_LEFT,
            pets.MSG_POUCH_NONE.format(**f),
        ]
        self.add("komunikaty-inne-moduly", "Zlecenia w innych miejscach gry (tekst)", *map(self.notice, other),
                 note="Kolejno: zmiana peta na zleceniu, pet z miejsca w stajni na zleceniu, pet właśnie wraca, "
                      "dopisek stajni, trening, Kopalnia (awans, odprowadzenie do stajni, podsumowanie), "
                      "klejnot Podróżnika (towarzysz na zleceniu), jajko do Dużej sakwy, jajko wyjęte z Dużej "
                      "sakwy, brak sakwy.")
        other_player = SimpleNamespace(user=Member(OTHER_PLAYER_ID, OTHER_PLAYER_NAME), guild=self.channel.guild)
        buttons = [
            ui._addressed(other_player, ui.MSG_NOT_YOURS_NAMED.format(player_id=pid)),
            self.addressed(ui.MSG_BUSY),
            self.addressed(ui.MSG_WRONG_CHANNEL.format(channel_id=CHANNEL_ID)),
            self.addressed(ui.MSG_CANNOT_PLAY),
            self.addressed(ui.MSG_STALE),
        ]
        self.add("komunikaty-przyciskow", "Ogólne odpowiedzi przycisków tablicy (tekst)",
                 *map(self.notice, buttons),
                 note="Wspólne dla wszystkich paneli gry, także Zleceń. Kolejno: ktoś inny klika cudzą tablicę, "
                      "trwa inne wydarzenie / walka, przycisk Zlecenia poza kanałem bossa, poległy łowca, "
                      "kliknięcie po restarcie bota.")
        self.add("komunikaty-wlasciciela", "Komunikaty dla właściciela / admina (tekst)",
                 *map(self.notice, self.owner_texts()),
                 note="Trafiają na kanał logów właściciela albo są odpowiedzią na komendy admina "
                      "(!ZakonczZlecenie, !DajZwojPrzywolania, !ReassignPet: odmowa i sukces); nie widzą ich "
                      "gracze.")

    def owner_texts(self) -> list[str]:
        from altermmo import bot as bot_module
        from altermmo.cogs import missions as missions_cog

        pid = self.pid
        broken = Path(tempfile.mkdtemp(prefix="missions-preview-")) / "missionsConfig.json"
        raw = json.loads(missions.CONFIG_FILE.read_text(encoding="utf-8"))
        raw["missions"][0]["flavour"] = "Wielka wyprawa po owce."  # a forbidden word: the real check refuses it
        broken.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        try:
            missions.load_config(broken)
            error: Any = "?"
        except missions.ConfigError as exc:
            error = exc
        shutil.rmtree(broken.parent, ignore_errors=True)
        # The start-up note of a summon interrupted by a crash, made by the real resolve_summon_marker.
        bot = FakeBot(self.bot.store.data_dir)
        bot.ready = False
        bot.store.runtime_set(missions.SUMMON_MARKER, {"player": pid, "at": clock.utcnow().isoformat()})
        missions.resolve_summon_marker(bot)
        interrupted = bot._startup_notes[-1] if bot._startup_notes else missions.MSG_SUMMON_INTERRUPTED
        return [
            missions.MSG_OWNER_CONFIG.format(error=error),
            missions.MSG_OWNER_POISON.format(mission_id=MISSION_AWAY, player_id=pid,
                                             error="UniqueViolationError: duplicate key value"),
            missions.MSG_SUMMON_NOT_RETURNED.format(player_id=pid, error="ConnectionResetError"),
            missions.MSG_SUMMON_UNSURE.format(player_id=pid),
            interrupted,
            missions_cog.MSG_ENDED.format(player_id=pid),
            missions_cog.MSG_NOTHING_AWAY.format(player_id=pid),
            missions_cog.MSG_GIVEN.format(player_id=pid, count=2),
            missions_cog.MSG_NO_COLUMNS,
            bot_module.MSG_MISSION_COLUMNS_FAILED.format(problems="petowner.summon_scrolls: permission denied"),
            bot_module.MSG_OWNER_COLUMNS_FAILED.format(problems="petowner.big_pouch: permission denied"),
            *self.reassign_texts,
        ]


def build_previews(player_id: int, work_dir: Path) -> list[dict[str, Any]]:
    async def main() -> list[dict[str, Any]]:
        return await Builder(player_id, work_dir).build()

    logging.disable(logging.WARNING)  # the real code logs what it does; the previews do not need it
    try:
        return asyncio.run(main())
    finally:
        logging.disable(logging.NOTSET)


# -- checking that every text of missions.py is shown ------------------------------------------

_TEXT_NAMES = re.compile(r"^(MSG_|LABEL_|RESULT_|STAGE|BOARD_|PICK_|PROGRESS_|WHERE_|EGG_WORDS|EGG_PLACES|LOOT_|"
                         r"TIER_WORD|MARK_|PET_PLACEHOLDER|GOT_MARK)")
#: Words of the catalogue: which of them a view shows depends on the missions on the board.
_CATALOGUE_WORDS = ("STAR_WORDS", "NEED_LOOK", "NEED_LABELS")


def _texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _texts(item)


def _all_text(previews: list[dict[str, Any]]) -> str:
    return json.dumps(previews, ensure_ascii=False).replace("\\n", "\n").replace('\\"', '"')


#: The texts about the missions outside features/missions.py (cogs/missions.py: every ``MSG_*``).
_OTHER_TEXTS = (
    ("pets", ("MSG_PET_AWAY", "MSG_SLOT_AWAY", "STABLE_AWAY", "STABLE_SIDE_AWAY", "MSG_SHOWN_AWAY",
              "MSG_PET_RETURNING", "MSG_EGG_TO_BIG_POUCH", "MSG_POUCH_ONE_LEFT", "MSG_POUCH_NONE")),
    ("training", ("MSG_TRAINING_AWAY",)),
    ("dungeon", ("MSG_LEVEL_AWAY", "MSG_STABLE_AWAY", "MSG_GOT_NO_LEVEL_AWAY")),
    ("skills", ("MSG_TRAVELLER_PET_AWAY",)),
    ("bot", ("MSG_MISSION_COLUMNS_FAILED", "MSG_OWNER_COLUMNS_FAILED")),
)


def _checked_texts() -> Iterator[tuple[str, Any]]:
    """``(name, value)`` of every text :func:`coverage` looks for."""
    from altermmo import bot as bot_module
    from altermmo.cogs import missions as missions_cog

    for name in sorted(vars(missions)):
        if _TEXT_NAMES.match(name):
            yield name, getattr(missions, name)
    for name in sorted(vars(missions_cog)):
        if name.startswith("MSG_"):
            yield f"cogs.missions.{name}", getattr(missions_cog, name)
    modules = {"pets": pets, "training": training, "dungeon": dungeon, "skills": skills, "bot": bot_module}
    for module, names in _OTHER_TEXTS:
        for name in names:
            yield f"{module}.{name}", getattr(modules[module], name)


def coverage(previews: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """How many texts of the missions the previews show (missions.py, cogs/missions.py and the away texts of
    the other modules), and the names of those they do not."""
    shown = _all_text(previews)
    total, missing = 0, []
    for name, value in _checked_texts():
        for text in _texts(value):
            parts = [part for part in re.split(r"\{[^{}]*\}", text) if len(part.strip()) >= 3]
            if not parts:
                continue
            total += 1
            if not all(part in shown for part in parts):
                missing.append(f"{name}: {text[:60]!r}")
    return total, missing


def catalogue_coverage(previews: list[dict[str, Any]]) -> tuple[int, int]:
    """How many of the catalogue's words (stars, needs) the previews show, of how many."""
    shown = _all_text(previews)
    words = [text for name in _CATALOGUE_WORDS for text in _texts(getattr(missions, name))]
    return sum(1 for word in words if word in shown), len(words)


def select(previews: list[dict[str, Any]], only: list[str] | None) -> list[dict[str, Any]]:
    if not only:
        return previews
    chosen = []
    for word in (word.casefold() for word in only):
        # The keys first (``--only tablica`` must not pull in every name that mentions a button); a word no
        # key contains is looked for in the names.
        found = [p for p in previews if word in p["key"].casefold()] or \
            [p for p in previews if word in p["name"].casefold()]
        chosen += [p for p in found if p not in chosen]
    return [p for p in previews if p in chosen]


def messages_of(preview: dict[str, Any]) -> list[dict[str, Any]]:
    first = {key: preview[key] for key in ("content", "embeds", "files", "components")}
    return [first, *preview.get("followups", [])]


# -- Discord markdown as HTML ------------------------------------------------------------------

_POLISH_MONTHS = ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
                  "października", "listopada", "grudnia")
_POLISH_DAYS = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")


def _plural(number: int, one: str, few: str, many: str) -> str:
    if number == 1:
        return one
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return few
    return many


def relative_pl(seconds: float) -> str:
    """Discord's relative time in Polish (moment.js thresholds): ``za 3 godziny``, ``5 minut temu``."""
    future = seconds >= 0
    s = abs(seconds)
    minutes, hours, days = s / 60, s / 3600, s / 86400
    if s < 45:
        text = "kilka sekund"
    elif s < 90:
        text = "minutę" if future else "minutę"
    elif minutes < 45:
        n = round(minutes)
        text = f"{n} {_plural(n, 'minutę', 'minuty', 'minut')}"
    elif minutes < 90:
        text = "godzinę"
    elif hours < 22:
        n = round(hours)
        text = f"{n} {_plural(n, 'godzinę', 'godziny', 'godzin')}"
    elif hours < 36:
        text = "1 dzień"
    elif days < 26:
        n = round(days)
        text = f"{n} {_plural(n, 'dzień', 'dni', 'dni')}"
    elif days < 320:
        n = max(1, round(days / 30.4))
        text = f"{n} {_plural(n, 'miesiąc', 'miesiące', 'miesięcy')}"
    else:
        n = max(1, round(days / 365))
        text = f"{n} {_plural(n, 'rok', 'lata', 'lat')}"
    return f"za {text}" if future else f"{text} temu"


def timestamp_pl(unix: int, style: str, now: float) -> str:
    moment = datetime.fromtimestamp(unix, tz=timezone.utc).astimezone(clock.WARSAW)
    day = f"{moment.day} {_POLISH_MONTHS[moment.month - 1]} {moment.year}"
    return {
        "t": moment.strftime("%H:%M"), "T": moment.strftime("%H:%M:%S"), "d": moment.strftime("%d.%m.%Y"),
        "D": day, "f": f"{day} {moment:%H:%M}", "F": f"{_POLISH_DAYS[moment.weekday()]}, {day} {moment:%H:%M}",
        "R": relative_pl(unix - now),
    }.get(style or "f", f"{day} {moment:%H:%M}")


class Markdown:
    def __init__(self, names: dict[int, str], now: float) -> None:
        self.names = names
        self.now = now

    def inline(self, text: str) -> str:
        stash: list[str] = []

        def keep(markup: str) -> str:
            stash.append(markup)
            return f"\x00{len(stash) - 1}\x00"

        text = re.sub(r"\\([\\*_~`|>#\-\[\]()<:.!])", lambda m: keep(html.escape(m.group(1))), text)
        text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
        text = re.sub(r"<(a?):(\w+):(\d+)>", lambda m: keep(
            f'<img class="emoji" alt=":{html.escape(m.group(2))}:" title=":{html.escape(m.group(2))}:" '
            f'src="https://cdn.discordapp.com/emojis/{m.group(3)}.{"gif" if m.group(1) else "webp"}?size=48">'), text)
        text = re.sub(r"<t:(-?\d+)(?::([tTdDfFR]))?>", lambda m: keep(
            f'<span class="time">{html.escape(timestamp_pl(int(m.group(1)), m.group(2) or "f", self.now))}'
            f"</span>"), text)
        text = re.sub(r"<@&(\d+)>", lambda m: keep(
            f'<span class="mention">@{html.escape(ROLE_NAMES.get(int(m.group(1)), "rola"))}</span>'), text)
        text = re.sub(r"<@!?(\d+)>", lambda m: keep(
            f'<span class="mention">@{html.escape(self.names.get(int(m.group(1)), "nieznany-użytkownik"))}'
            f"</span>"), text)
        text = re.sub(r"<#(\d+)>", lambda m: keep('<span class="mention">#kanał</span>'), text)
        text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: keep(
            f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>'), text)
        text = re.sub(r"https?://[^\s<>]+[^\s<>.,;:!?)]", lambda m: keep(
            f'<a href="{html.escape(m.group(0))}">{html.escape(m.group(0))}</a>'), text)
        text = html.escape(text, quote=False)
        text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)
        text = re.sub(r"\*(?=\S)(.+?)(?<=\S)\*", r"<em>\1</em>", text)
        text = re.sub(r"(?<![\w])_(?=\S)(.+?)(?<=\S)_(?![\w])", r"<em>\1</em>", text)
        text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
        text = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', text)
        for _ in range(3):
            text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
        return text

    def block(self, text: str | None) -> str:
        """A message's content / an embed's description with Discord's block markdown."""
        if not text:
            return ""
        out: list[str] = []
        kinds: list[str] = []
        lines = text.split("\n")
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.startswith("```"):
                body = []
                index += 1
                while index < len(lines) and not lines[index].startswith("```"):
                    body.append(lines[index])
                    index += 1
                out.append(f'<pre class="codeblock">{html.escape(chr(10).join(body))}</pre>')
                kinds.append("block")
            elif line.startswith(">>> "):
                rest = "\n".join([line[4:], *lines[index + 1:]])
                out.append(f'<div class="quote"><div class="quote-bar"></div><div>{self.block(rest)}</div></div>')
                kinds.append("block")
                break
            elif line.startswith("> ") or line == ">":
                quoted = []
                while index < len(lines) and (lines[index].startswith("> ") or lines[index] == ">"):
                    quoted.append(lines[index][2:])
                    index += 1
                out.append(f'<div class="quote"><div class="quote-bar"></div>'
                           f'<div>{self.block(chr(10).join(quoted))}</div></div>')
                kinds.append("block")
                continue
            elif line.startswith("-# "):
                out.append(f'<div class="subtext">{self.inline(line[3:])}</div>')
                kinds.append("block")
            elif re.match(r"^#{1,3} ", line):
                level = len(line.split(" ", 1)[0])
                out.append(f'<div class="h{level}">{self.inline(line[level + 1:])}</div>')
                kinds.append("block")
            elif re.match(r"^\s*[-*] ", line):
                items = []
                while index < len(lines) and re.match(r"^\s*[-*] ", lines[index]):
                    items.append(f"<li>{self.inline(re.sub(r'^\s*[-*] ', '', lines[index]))}</li>")
                    index += 1
                out.append(f'<ul class="list">{"".join(items)}</ul>')
                kinds.append("block")
                continue
            else:
                out.append(self.inline(line))
                kinds.append("text")
            index += 1
        joined = ""
        for number, (part, kind) in enumerate(zip(out, kinds)):
            if number and kind == "text" and kinds[number - 1] == "text":
                joined += "<br>"
            joined += part
        return joined


# -- the HTML pages -------------------------------------------------------------------------

CSS = """
:root { --bg:#313338; --text:#dbdee1; --muted:#949ba4; --header:#f2f3f5; --embed:#2b2d31; --dark:#1e1f22; }
* { box-sizing: border-box; }
html, body { margin:0; background:var(--bg); color:var(--text);
  font-family:"gg sans","Noto Sans","Segoe UI","Helvetica Neue",Helvetica,Arial,sans-serif; font-size:16px; }
.chat { width:760px; padding:12px 0 20px; }
.msg { position:relative; padding:2px 48px 2px 72px; margin-top:17px; min-height:44px; }
.msg.cont { margin-top:2px; min-height:0; }
.msg:first-child { margin-top:4px; }
.avatar { position:absolute; left:16px; top:4px; width:40px; height:40px; border-radius:50%; overflow:hidden;
  background:linear-gradient(135deg,#d9a441,#8a5a12); color:#fff; font-weight:700; font-size:18px;
  display:flex; align-items:center; justify-content:center; }
.avatar img { width:100%; height:100%; object-fit:cover; position:absolute; inset:0; }
.header { display:flex; align-items:center; line-height:22px; height:22px; }
.username { color:var(--header); font-weight:500; font-size:16px; }
.apptag { background:#5865f2; color:#fff; font-size:10px; font-weight:600; height:15px; padding:0 4px;
  border-radius:3px; display:inline-flex; align-items:center; margin-left:4px; position:relative; top:1px;
  gap:2px; letter-spacing:.02em; }
.ts { color:var(--muted); font-size:12px; margin-left:6px; font-weight:500; }
.content { font-size:16px; line-height:22px; white-space:pre-wrap; word-wrap:break-word; }
.content, .embed-desc, .field-value { overflow-wrap:anywhere; }
.mention { background:rgba(88,101,242,.3); color:#c9cdfb; border-radius:3px; padding:0 2px; font-weight:500; }
.time { background:rgba(78,80,88,.48); border-radius:3px; padding:0 2px; }
.emoji { width:1.375em; height:1.375em; vertical-align:-.3em; object-fit:contain; margin:0 .05em; }
code { background:var(--dark); border-radius:4px; padding:.1em .25em; font-size:85%;
  font-family:Consolas,"Andale Mono WT","Andale Mono","Lucida Console",monospace; }
.codeblock { background:var(--embed); border:1px solid var(--dark); border-radius:4px; padding:7px;
  font-family:Consolas,monospace; font-size:14px; white-space:pre-wrap; margin:4px 0 0; }
.quote { display:flex; margin:2px 0; } .quote-bar { width:4px; border-radius:4px; background:#4e5058;
  margin-right:12px; flex-shrink:0; } .quote > div:last-child { min-width:0; }
.subtext { font-size:12px; line-height:16px; color:var(--muted); }
.h1 { font-size:24px; line-height:30px; font-weight:700; color:var(--header); margin:8px 0 0; }
.h2 { font-size:20px; line-height:26px; font-weight:700; color:var(--header); margin:8px 0 0; }
.h3 { font-size:16px; line-height:22px; font-weight:700; color:var(--header); margin:8px 0 0; }
.list { margin:2px 0 2px 0; padding-left:22px; white-space:normal; } .list li { margin:0 0 2px; }
.spoiler { background:rgba(255,255,255,.1); border-radius:3px; padding:0 2px; }
a { color:#00a8fc; text-decoration:none; }
strong { font-weight:700; }
.embed { position:relative; display:grid; width:fit-content; max-width:520px; background:var(--embed); border-left:4px solid var(--dark);
  border-radius:4px; margin-top:4px; }
.embed-grid { display:grid; grid-template-columns:minmax(0,1fr) auto; padding:2px 16px 16px 12px; min-width:0; }
.embed-grid > * { grid-column:1/2; min-width:0; }
.embed-author { margin-top:8px; font-size:14px; font-weight:600; color:var(--header); display:flex; align-items:center; gap:8px; }
.embed-title { margin-top:8px; font-size:16px; line-height:22px; font-weight:600; color:var(--header); }
.embed-desc { margin-top:8px; font-size:14px; line-height:18px; white-space:pre-wrap; }
.embed-desc code, .field-value code { background:var(--dark); }
.embed-fields { display:grid; grid-template-columns:repeat(12,minmax(0,1fr)); gap:8px; margin-top:8px; }
.field { font-size:14px; line-height:18px; min-width:0; }
.field-name { font-weight:600; color:var(--header); margin-bottom:2px; }
.field-value { white-space:pre-wrap; }
.embed-image { grid-column:1/3 !important; margin-top:16px; }
.embed-image img { display:block; max-width:300px; max-height:300px; border-radius:4px; }
.embed-thumb { grid-column:2/3 !important; grid-row:1/8; margin:8px 0 0 16px; justify-self:end; }
.embed-thumb img { display:block; max-width:80px; max-height:80px; border-radius:4px; object-fit:contain; }
.embed-footer { grid-column:1/3 !important; margin-top:8px; font-size:12px; line-height:16px; font-weight:500;
  color:var(--text); }
.attachments img { display:block; max-width:350px; max-height:350px; border-radius:8px; margin-top:6px; }
.components { margin-top:6px; display:flex; flex-direction:column; gap:8px; }
.row { display:flex; flex-wrap:wrap; gap:8px; }
.btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; height:32px; min-width:60px;
  padding:2px 16px; border-radius:8px; font-size:14px; font-weight:500; color:#fff; line-height:16px; white-space:nowrap; }
.btn .emoji, .btn .uemoji { width:18px; height:18px; font-size:16px; line-height:18px; margin:0; vertical-align:0;
  display:inline-flex; align-items:center; justify-content:center; }
.s1 { background:#5865f2; } .s2, .s5 { background:#4e5058; } .s3 { background:#248046; } .s4 { background:#da373c; }
.btn.off { opacity:.5; cursor:not-allowed; }
.select { width:400px; max-width:100%; }
.select-box { background:var(--dark); border:1px solid var(--dark); border-radius:8px; min-height:40px;
  padding:8px 8px 8px 12px; display:flex; align-items:center; justify-content:space-between; font-size:16px; font-weight:500; }
.select-box .ph { color:var(--muted); } .select-box .val { color:var(--text); display:flex; align-items:center; gap:8px; }
.chev { color:#b5bac1; font-size:12px; margin-left:8px; }
.select-menu { margin-top:4px; background:var(--embed); border:1px solid var(--dark); border-radius:8px;
  overflow:hidden; box-shadow:0 8px 16px rgba(0,0,0,.24); }
.opt { display:flex; align-items:center; gap:10px; padding:8px 12px; }
.opt + .opt { border-top:1px solid rgba(78,80,88,.3); }
.opt.sel { background:#404249; }
.opt-emoji { font-size:18px; width:22px; text-align:center; flex-shrink:0; }
.opt-text { flex:1; min-width:0; } .opt-label { font-size:16px; font-weight:500; color:var(--text); }
.opt-desc { font-size:14px; color:#b5bac1; margin-top:1px; }
.opt-check { color:#5865f2; font-weight:700; font-size:16px; }
.off-note { font-size:12px; color:var(--muted); margin-top:2px; }
.page-title { font-size:12px; color:var(--muted); padding:10px 16px 0; text-transform:uppercase; letter-spacing:.04em; font-weight:600; }
.index-top { padding:16px; color:var(--header); }
.index-top h1 { font-size:20px; margin:0 0 6px; } .index-top p { margin:4px 0; color:var(--muted); font-size:14px; }
.index-top a { margin-right:8px; }
.divider { display:flex; align-items:center; gap:8px; margin:28px 16px 0; color:var(--muted); font-size:12px; font-weight:600; }
.divider::before, .divider::after { content:""; flex:1; height:1px; background:#3f4147; }
.note { margin:4px 16px 0 72px; font-size:12px; color:var(--muted); font-style:italic; }
"""


class Renderer:
    def __init__(self, player_id: int, files_rel: str = "files") -> None:
        self.md = Markdown({player_id: PLAYER_NAME, OTHER_PLAYER_ID: OTHER_PLAYER_NAME}, time.time())
        self.files_rel = files_rel
        self.clock = datetime.now(clock.WARSAW).strftime("%H:%M")

    def emoji(self, emoji: Any, cls: str = "uemoji") -> str:
        if not emoji:
            return ""
        if emoji.get("id"):
            ext = "gif" if emoji.get("animated") else "webp"
            return (f'<img class="emoji" alt=":{html.escape(emoji.get("name") or "")}:" '
                    f'src="https://cdn.discordapp.com/emojis/{emoji["id"]}.{ext}?size=48">')
        return f'<span class="{cls}">{html.escape(emoji.get("name") or "")}</span>'

    def url(self, url: str | None) -> str:
        if not url:
            return ""
        if url.startswith("attachment://"):
            return f"{self.files_rel}/{url[len('attachment://'):]}"
        return url

    def embed(self, embed: dict[str, Any]) -> str:
        colour = embed.get("color")
        border = f"#{colour:06x}" if isinstance(colour, int) else "#1e1f22"
        parts = []
        author = embed.get("author") or {}
        if author.get("name"):
            icon = f'<img src="{html.escape(self.url(author.get("icon_url")))}" width="24" height="24" ' \
                   f'style="border-radius:50%">' if author.get("icon_url") else ""
            parts.append(f'<div class="embed-author">{icon}{html.escape(author["name"])}</div>')
        if embed.get("title"):
            parts.append(f'<div class="embed-title">{self.md.inline(embed["title"])}</div>')
        if embed.get("description"):
            parts.append(f'<div class="embed-desc">{self.md.block(embed["description"])}</div>')
        fields = embed.get("fields") or []
        if fields:
            cells, run = [], []

            def flush() -> None:
                span = 12 // max(len(run), 1) if run else 12
                for field in run:
                    cells.append((field, span))
                run.clear()

            for field in fields:
                if field.get("inline"):
                    run.append(field)
                    if len(run) == 3:
                        flush()
                else:
                    flush()
                    cells.append((field, 12))
            flush()
            html_fields = "".join(
                f'<div class="field" style="grid-column:span {span}"><div class="field-name">'
                f'{self.md.inline(field.get("name", ""))}</div><div class="field-value">'
                f'{self.md.block(field.get("value", ""))}</div></div>' for field, span in cells)
            parts.append(f'<div class="embed-fields">{html_fields}</div>')
        if (embed.get("image") or {}).get("url"):
            parts.append(f'<div class="embed-image"><img src="{html.escape(self.url(embed["image"]["url"]))}"></div>')
        if (embed.get("thumbnail") or {}).get("url"):
            parts.append(f'<div class="embed-thumb"><img src="{html.escape(self.url(embed["thumbnail"]["url"]))}">'
                         f"</div>")
        footer = embed.get("footer") or {}
        if footer.get("text"):
            parts.append(f'<div class="embed-footer">{html.escape(footer["text"])}</div>')
        return (f'<div class="embed" style="border-left-color:{border}"><div class="embed-grid">{"".join(parts)}'
                f"</div></div>")

    def select(self, item: dict[str, Any]) -> str:
        options = item.get("options") or []
        chosen = next((o for o in options if o.get("default")), None)
        if chosen:
            box = f'<span class="val">{self.emoji(chosen.get("emoji"))}{html.escape(chosen.get("label", ""))}</span>'
        else:
            box = f'<span class="ph">{html.escape(item.get("placeholder") or "Wybierz...")}</span>'
        menu = "".join(
            f'<div class="opt{" sel" if o.get("default") else ""}"><span class="opt-emoji">'
            f'{self.emoji(o.get("emoji"))}</span><div class="opt-text"><div class="opt-label">'
            f'{html.escape(o.get("label", ""))}</div>'
            + (f'<div class="opt-desc">{html.escape(o["description"])}</div>' if o.get("description") else "")
            + "</div>" + ('<span class="opt-check">✓</span>' if o.get("default") else "") + "</div>"
            for o in options)
        off = " off" if item.get("disabled") else ""
        return (f'<div class="select{off}"><div class="select-box">{box}<span class="chev">▼</span></div>'
                f'<div class="subtext" style="margin-top:4px">Lista po rozwinięciu ({len(options)}): '
                f'{html.escape(item.get("placeholder") or "")}</div><div class="select-menu">{menu}</div></div>')

    def components(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return ""
        out = []
        for row in rows:
            items = []
            for item in row.get("components", []):
                if item.get("type") == 2:
                    off = " off" if item.get("disabled") else ""
                    label = html.escape(item.get("label") or "")
                    arrow = " ↗" if item.get("style") == 5 else ""
                    items.append(f'<span class="btn s{item.get("style", 2)}{off}">{self.emoji(item.get("emoji"))}'
                                 f"{label}{arrow}</span>")
                elif item.get("type") in (3, 5, 6, 7, 8):
                    items.append(self.select(item))
            out.append(f'<div class="row">{"".join(items)}</div>')
        return f'<div class="components">{"".join(out)}</div>'

    def message(self, message: dict[str, Any], *, first: bool) -> str:
        head = ""
        if first:
            head = (f'<div class="avatar">A<img src="{BOT_AVATAR}" onerror="this.remove()"></div>'
                    f'<div class="header"><span class="username">{BOT_NAME}</span>'
                    f'<span class="apptag">✓ APP</span><span class="ts">Dzisiaj o {self.clock}</span></div>')
        body = ""
        if message.get("content"):
            body += f'<div class="content">{self.md.block(message["content"])}</div>'
        referenced = set()
        for embed in message.get("embeds", []):
            body += self.embed(embed)
            for slot in ("image", "thumbnail"):
                url = (embed.get(slot) or {}).get("url") or ""
                if url.startswith("attachment://"):
                    referenced.add(url[len("attachment://"):])
        loose = [Path(path).name for path in message.get("files", []) if Path(path).name not in referenced]
        if loose:
            body += '<div class="attachments">' + "".join(
                f'<img src="{self.files_rel}/{html.escape(name)}">' for name in loose) + "</div>"
        body += self.components(message.get("components", []))
        return f'<div class="msg{"" if first else " cont"}">{head}{body}</div>'

    def preview(self, preview: dict[str, Any]) -> str:
        messages = messages_of(preview)
        return "".join(self.message(message, first=index == 0) for index, message in enumerate(messages))


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="pl"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><style>{CSS}</style></head>'
            f'<body>{body}</body></html>')


def write_html(previews: list[dict[str, Any]], out_dir: Path, player_id: int) -> list[Path]:
    """``index.html``, one page per preview, the pictures in ``files/``, ``previews.json``; the pages written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files_dir = out_dir / "files"
    files_dir.mkdir(exist_ok=True)
    for preview in previews:
        for message in messages_of(preview):
            for path in message.get("files", []):
                source = Path(path)
                if source.is_file():
                    shutil.copy2(source, files_dir / source.name)
    renderer = Renderer(player_id)
    pages = []
    blocks = []
    for preview in previews:
        chat = f'<div class="chat">{renderer.preview(preview)}</div>'
        page = out_dir / f"{preview['key']}.html"
        page.write_text(_page(preview["name"], chat), encoding="utf-8")
        pages.append(page)
        note = f'<div class="note">{html.escape(preview["note"])}</div>' if preview.get("note") else ""
        blocks.append(f'<div class="divider" id="{preview["key"]}"><a href="{preview["key"]}.html">'
                      f'{html.escape(preview["name"])}</a></div>{note}{chat}')
    links = " · ".join(f'<a href="#{p["key"]}">{html.escape(p["name"])}</a>' for p in previews)
    top = (f'<div class="index-top"><h1>Podgląd komunikatów: Zlecenia ({len(previews)})</h1>'
           f"<p>Wygenerowano {datetime.now(clock.WARSAW):%d.%m.%Y %H:%M} prawdziwym kodem "
           f"altermmo/features/missions.py na przykładowych danych.</p><p>{links}</p></div>")
    (out_dir / "index.html").write_text(_page("Podgląd: Zlecenia", top + "".join(blocks)), encoding="utf-8")
    (out_dir / "previews.json").write_text(json.dumps(previews, ensure_ascii=False, indent=1, default=str),
                                           encoding="utf-8")
    return pages


# -- screenshots ------------------------------------------------------------------------------

BROWSERS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge")


def find_browser(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for candidate in BROWSERS:
        if Path(candidate).is_file():
            return candidate
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def screenshot(browser: str, page: Path, png: Path, *, width: int = 760) -> Path:
    """One PNG of ``page``, cropped to its content (taller windows are tried until nothing is cut off)."""
    from PIL import Image, ImageChops

    height = 1200
    profile = Path(tempfile.mkdtemp(prefix="missions-preview-browser-"))
    try:
        while True:
            raw = png.with_suffix(".raw.png")
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                            "--force-device-scale-factor=1", f"--user-data-dir={profile}",
                            "--virtual-time-budget=8000", f"--window-size={width},{height}",
                            f"--screenshot={raw}", page.resolve().as_uri()],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            image = Image.open(raw).convert("RGB")
            background = Image.new("RGB", image.size, (0x31, 0x33, 0x38))
            box = ImageChops.difference(image, background).getbbox()
            bottom = box[3] if box else 40
            if bottom >= image.height - 4 and height < 16000:
                height *= 2
                continue
            image.crop((0, 0, image.width, min(image.height, bottom + 16))).save(png)
            raw.unlink(missing_ok=True)
            return png
    finally:
        shutil.rmtree(profile, ignore_errors=True)


# -- posting through the REST API ---------------------------------------------------------------

API = "https://discord.com/api/v10"
POST_GAP = 1.2


def disabled_components(rows: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    """The rows with every item disabled and its id replaced (a click can never reach the real flows)."""
    rows = copy.deepcopy(rows)
    number = 0
    for row in rows:
        for item in row.get("components", []):
            item["disabled"] = True
            if "custom_id" in item:
                number += 1
                item["custom_id"] = f"preview:{tag}:{number}"
    return rows


def outgoing(previews: list[dict[str, Any]],
             channel_id: str | None = None) -> list[tuple[str, dict[str, Any], list[Path]]]:
    """Every message to post: ``(what, payload, files)`` - the header, then each preview's name and messages.

    ``channel_id``: the channel the previews go to - the sample channel's mention becomes its mention (Discord
    would show "#unknown" for the sample id)."""
    here = f"<#{int(channel_id)}>" if channel_id and str(channel_id).isdigit() else None
    total = sum(len(messages_of(p)) for p in previews)
    posts: list[tuple[str, dict[str, Any], list[Path]]] = [
        ("nagłówek", {"content": f"## 🔎 Podgląd komunikatów: Zlecenia ({total} wiadomości)\n"
                                 "-# Przyciski i listy są wyłączone - to tylko podgląd, nikt nie został oznaczony.",
                      "allowed_mentions": {"parse": []}}, [])]
    for number, preview in enumerate(previews, start=1):
        posts.append((f"{preview['key']} (nazwa)",
                      {"content": f"-# ▶ {number}. {preview['name']}"
                                  + (f" - {preview['note']}" if preview.get("note") else ""),
                       "allowed_mentions": {"parse": []}}, []))
        for index, message in enumerate(messages_of(preview)):
            files = [Path(path) for path in message.get("files", [])]
            names = [path.name for path in files]
            content = message.get("content") or ""
            if here is not None:
                content = content.replace(f"<#{CHANNEL_ID}>", here)
            payload: dict[str, Any] = {"content": content, "embeds": message["embeds"],
                                       "components": disabled_components(message["components"], f"{number}.{index}"),
                                       "allowed_mentions": {"parse": []}}
            if files:
                payload["attachments"] = [{"id": i, "filename": name} for i, name in enumerate(names)]
            posts.append((f"{preview['key']}" + (f" [{index + 1}]" if index else ""), payload, files))
    return posts


def describe(what: str, payload: dict[str, Any], files: list[Path]) -> str:
    items = sum(len(row.get("components", [])) for row in payload.get("components", []))
    content = (payload.get("content") or "").replace("\n", " ⏎ ")
    return (f"{what}: treść {len(payload.get('content') or '')} zn., embedy {len(payload.get('embeds', []))}, "
            f"pliki {len(files)}, elementy {items} (wyłączone)" + (f" | {content[:70]}" if content else ""))


def _retry_after(body: str, headers: Any) -> float:
    """Seconds to wait after a 429 (the body's ``retry_after``, else the ``Retry-After`` header, else 2)."""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        return max(0.0, float(json.loads(body).get("retry_after")))
    with contextlib.suppress(ValueError, TypeError):
        return max(0.0, float(headers.get("Retry-After")))
    return 2.0


async def post_all(channel_id: str, token: str, posts: list[tuple[str, dict[str, Any], list[Path]]]) -> int:
    """POST every message to ``channel_id`` (REST only).  The token goes only into the request header: it is
    never printed - an error is shown as its type and Discord's answer (which never contains it)."""
    import aiohttp

    mimetypes.add_type("image/webp", ".webp")  # (older Windows registries do not know it)
    url = f"{API}/channels/{int(channel_id)}/messages"
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (missions-preview, 1.0)"}
    sent = 0
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for number, (what, payload, files) in enumerate(posts, start=1):
            for attempt in range(1, 8):
                form = aiohttp.FormData()
                form.add_field("payload_json", json.dumps(payload, ensure_ascii=False),
                               content_type="application/json")
                handles = []
                for index, path in enumerate(files):
                    handle = path.open("rb")
                    handles.append(handle)
                    form.add_field(f"files[{index}]", handle, filename=path.name,
                                   content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                try:
                    async with session.post(url, data=form, allow_redirects=False) as response:
                        body = await response.text()
                        if response.status == 429:
                            wait = _retry_after(body, response.headers)
                            print(f"  limit Discorda (429) - czekam {wait:.1f} s...")
                            await asyncio.sleep(wait + 0.1)
                            continue
                        if response.status >= 500:
                            print(f"  błąd serwera Discorda ({response.status}) - ponawiam...")
                            await asyncio.sleep(2.0 * attempt)
                            continue
                        if response.status >= 400:
                            print(f"[{number}/{len(posts)}] {what}: BŁĄD {response.status}: {body[:300]}")
                            if response.status in (401, 403, 404):
                                print("Przerywam: sprawdź token i ID kanału oraz uprawnienia bota na tym kanale.")
                                return sent
                            break
                        sent += 1
                        print(f"[{number}/{len(posts)}] wysłano: {what}")
                        if response.headers.get("X-RateLimit-Remaining") == "0":
                            with contextlib.suppress(ValueError, TypeError):
                                await asyncio.sleep(float(response.headers.get("X-RateLimit-Reset-After", "1")))
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:  # (only the type: never the request)
                    print(f"  błąd połączenia ({type(exc).__name__}) - ponawiam...")
                    await asyncio.sleep(2.0 * attempt)
                    continue
                finally:
                    for handle in handles:
                        handle.close()
            else:
                print(f"[{number}/{len(posts)}] {what}: NIE WYSŁANO po 7 próbach.")
            await asyncio.sleep(POST_GAP)
    return sent


# -- the command line ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Podgląd wszystkich wiadomości Zleceń (prawdziwy kod, przykładowe dane).")
    parser.add_argument("--list", action="store_true", help="wypisz podglądy (klucz i nazwa)")
    parser.add_argument("--html", metavar="OUT_DIR", help="zapisz strony HTML w stylu Discorda do OUT_DIR")
    parser.add_argument("--screenshots", action="store_true", help="z --html: także PNG każdego podglądu (OUT_DIR/png)")
    parser.add_argument("--browser", metavar="EXE", help="przeglądarka do zrzutów (domyślnie Edge albo Chrome)")
    parser.add_argument("--send", metavar="CHANNEL_ID", help="wyślij podglądy na kanał Discorda (REST API)")
    parser.add_argument("--yes", action="store_true", help="z --send: naprawdę wyślij (bez tego tylko próba)")
    parser.add_argument("--only", action="append", metavar="KEY", help="tylko podglądy, których klucz/nazwa zawiera KEY")
    parser.add_argument("--player-id", type=int, default=DEFAULT_PLAYER_ID,
                        help="ID gracza we wzmiankach (np. Twoje - nikt nie zostanie oznaczony)")
    args = parser.parse_args(argv)
    if not (args.list or args.html or args.send):
        parser.print_help()
        return 0

    work = Path(tempfile.mkdtemp(prefix="missions-preview-data-"))
    try:
        previews = build_previews(args.player_id, work)
        total, missing = coverage(previews)
        chosen = select(previews, args.only)
        if args.only and not chosen:
            print("Żaden podgląd nie pasuje do --only. Dostępne klucze: " + ", ".join(p["key"] for p in previews))
            return 2
        if args.list or args.html:
            print(f"Podglądy ({len(chosen)} z {len(previews)}):")
            for preview in chosen:
                print(f"  {preview['key']:<34} {preview['name']}  [{len(messages_of(preview))} wiad.]")
            print(f"Pokrycie tekstów Zleceń (missions.py, cogs/missions.py, inne moduły): "
                  f"{total - len(missing)}/{total}"
                  + ("" if not missing else " - nie pokazano: " + "; ".join(missing)))
            seen, words = catalogue_coverage(previews)
            print(f"Słowa katalogu (★, wymagania) widoczne na przykładowych tablicach: {seen}/{words} "
                  "(reszta zależy od wylosowanych zleceń)")
        if args.html:
            out = Path(args.html)
            pages = write_html(chosen, out, args.player_id)
            print(f"Zapisano {len(pages)} stron i {out / 'index.html'}")
            if args.screenshots:
                browser = find_browser(args.browser)
                if browser is None:
                    print("Nie znaleziono Edge ani Chrome - podaj --browser ŚCIEŻKA.")
                    return 1
                (out / "png").mkdir(exist_ok=True)
                for page in pages:
                    png = screenshot(browser, page, out / "png" / f"{page.stem}.png")
                    print(f"  zrzut: {png}")
        if args.send:
            if not str(args.send).isdigit():
                print("ID kanału musi być liczbą.")
                return 2
            posts = outgoing(chosen, str(args.send))
            if not args.yes:
                print(f"TRYB PRÓBNY - nic nie zostało wysłane. Na kanał {args.send} poszłoby {len(posts)} wiadomości:")
                for what, payload, files in posts:
                    print("  " + describe(what, payload, files))
                print("Aby naprawdę wysłać, uruchom ponownie z --yes.")
                return 0
            from altermmo.settings import load_settings

            token = load_settings().token
            if not token:
                print("Brak tokena: ustaw TOKEN w .env (jak dla bota).")
                return 1
            print(f"Wysyłam {len(posts)} wiadomości na kanał {args.send} (tylko REST API, bez logowania bota)...")
            sent = asyncio.run(post_all(str(args.send), token, posts))
            print(f"Gotowe: wysłano {sent} z {len(posts)}.")
            return 0 if sent == len(posts) else 1
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
