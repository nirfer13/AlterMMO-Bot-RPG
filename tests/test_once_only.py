"""Owner's rule: what is meant to be done once is done exactly once.

Regression tests for the incident of 2026-11-20: the running bot read
``features/events.py`` from the disk for the first time hours after its start -
after that file had been edited to call ``achievements.add_count``, while the
old ``achievements`` module (without that function) was already in memory.
Opening the treasure chest handed out the loot, then raised ``AttributeError``,
and the chest was put back: one chest was opened seven times.

Two fixes are covered here, by driving the real code with the fakes:

* ``EventsCog._claim``: once the reward step has begun, the chest / shrine is
  used up whatever happens (concurrent clicks and typed commands included);
* ``bot.import_all_modules``: every module of the bot is imported before the
  extensions load, so a running bot never reads its code from the disk later.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import altermmo
from altermmo import bot as bot_module
from altermmo.cogs import events as events_cog
from altermmo.core.state import EventType
from altermmo.features import achievements, events, modifiers, patrons, petcards

from fakes import FakeInteraction

PACKAGE_DIR = Path(altermmo.__file__).resolve().parent
ENTRY_POINT = "altermmo.__main__"


# -- helpers ------------------------------------------------------------------------------

@pytest.fixture
def cog(game):
    cog = events_cog.EventsCog(game.bot)
    cog.register_views()
    return cog


@pytest.fixture
def loot(monkeypatch):
    """Record the chest loot handed out; the hand-out takes a moment (other clicks come in meanwhile)."""
    calls: list = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        calls.append(hunter)
        await asyncio.sleep(0)
        return ""

    monkeypatch.setattr(events.daily, "rand_loot", rand_loot)
    return calls


@pytest.fixture
def blessings(monkeypatch):
    """Record the shrine blessings (the real ones, after a moment)."""
    calls: list = []
    real = modifiers.random_modifiers

    async def random_modifiers(scene, msg=True, sacrefice=False):
        calls.append(scene.author)
        await asyncio.sleep(0)
        return await real(scene, msg, sacrefice)

    monkeypatch.setattr(modifiers, "random_modifiers", random_modifiers)
    return calls


def ctx_of(game, member, content):
    message = game.say(member, content)
    return SimpleNamespace(bot=game.bot, channel=game.channel, author=member, message=message)


async def late_click(game, cog, member, panel, event_type: EventType) -> FakeInteraction:
    """A click on a panel whose button is already gone (a client that did not refresh yet)."""
    interaction = FakeInteraction(game.bot, member, panel, channel=game.channel)
    await cog.passive_clicked(interaction, event_type)
    return interaction


def supporters(game, count: int) -> list:
    return [game.add_member(f"Wierny{index}", roles=[patrons.CRAFTER_ROLE]) for index in range(count)]


# -- (1) the incident -----------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["code-from-disk", "raises"])
async def test_chest_whose_code_fails_after_the_loot_is_opened_exactly_once(game, cog, loot, monkeypatch,
                                                                            failure):
    if failure == "code-from-disk":
        # The new events.py calls a function the achievements module in memory does not have.
        monkeypatch.delattr(achievements, "add_count")
        error = "AttributeError"
    else:
        async def add_count(*args, **kwargs):
            raise RuntimeError("test: counting failed")

        monkeypatch.setattr(achievements, "add_count", add_count)
        error = "RuntimeError"
    players = [game.add_member(f"Gracz{index}") for index in range(7)]
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]

    # Seven players click at once (as on the day of the incident) ...
    clicks = await asyncio.gather(*(game.click(chest, player, events_cog.CHEST_BUTTON_ID)
                                    for player in players))
    # ... and keep clicking and typing after the error.
    later = [await late_click(game, cog, player, chest, EventType.CHEST) for player in players]
    for player in players:
        await cog.open_chest.callback(cog, ctx_of(game, player, "$skrzynia"))

    assert loot == [players[0]]                                          # the loot was handed out once
    assert clicks[0].response.deferred and clicks[0].replies == []
    assert all(click.answers == [events_cog.MSG_NO_CHEST] for click in [*clicks[1:], *later])
    assert game.channel.count(events_cog.MSG_NO_CHEST) == 6 + 7 + 7
    assert not game.bot.state.event_alive and game.bot.state.free_for_activity
    assert chest.view is None and events_cog.MSG_CHEST_OPENED.format(mention=players[0].mention) in chest.text
    assert game.channel.count(events_cog.MSG_REWARD_FAILED) == 1
    told = events_cog.LOG_REWARD_FAILED.format(what="Skrzynia", player_id=players[0].id, error=error)
    assert game.bot.owner_log == [told]
    assert game.log_channel.contains(f"<@{game.bot.ids.owner}>!   {told}")


async def test_chest_whose_opening_is_cancelled_half_way_is_not_put_back(game, cog, monkeypatch):
    """E.g. the bot is stopped in the middle of the hand-out: the chest is gone all the same."""
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    handed: list = []
    never = asyncio.Event()

    async def rand_loot(scene, rarity, hunter, boost_percent):
        handed.append(hunter)
        await never.wait()

    monkeypatch.setattr(events.daily, "rand_loot", rand_loot)
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]
    opening = asyncio.ensure_future(game.click(chest, ala, events_cog.CHEST_BUTTON_ID))
    while not handed:
        await asyncio.sleep(0)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert not game.bot.state.event_alive
    late = await late_click(game, cog, ola, chest, EventType.CHEST)
    assert late.answers == [events_cog.MSG_NO_CHEST] and handed == [ala]
    assert chest.view is None                                            # the stale button goes too


# -- (2) concurrent claims ---------------------------------------------------------------------

async def test_two_concurrent_chest_clicks_open_it_once(game, cog, loot):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]
    first, second = await asyncio.gather(game.click(chest, ala, events_cog.CHEST_BUTTON_ID),
                                         game.click(chest, ola, events_cog.CHEST_BUTTON_ID))
    assert len(loot) == 1
    winner, loser = (first, second) if loot == [ala] else (second, first)
    assert winner.replies == [] and loser.answers == [events_cog.MSG_NO_CHEST]
    assert game.channel.count("Skrzynię otwiera") == 1 and not game.bot.state.event_alive


@pytest.mark.parametrize("typed_first", [False, True])
async def test_chest_click_racing_a_typed_command_opens_it_once(game, cog, loot, typed_first):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await cog.run_one_event(forced=EventType.CHEST)
    chest = game.channel.messages[-1]
    ctx = ctx_of(game, ola, "$skrzynia")
    click = game.click(chest, ala, events_cog.CHEST_BUTTON_ID)
    typed = cog.open_chest.callback(cog, ctx)
    results = await asyncio.gather(*((typed, click) if typed_first else (click, typed)))
    interaction = results[1] if typed_first else results[0]
    assert loot == ([ola] if typed_first else [ala])
    if typed_first:
        assert interaction.answers == [events_cog.MSG_NO_CHEST]
    else:
        assert interaction.replies == [] and game.channel.count(events_cog.MSG_NO_CHEST) == 1
    assert game.channel.count("Skrzynię otwiera") == 1 and chest.view is None


async def test_two_concurrent_prayers_bless_once(game, cog, blessings):
    first_believer, second_believer = supporters(game, 2)
    await cog.run_one_event(forced=EventType.SHRINE)
    shrine = game.channel.messages[-1]
    first, second = await asyncio.gather(game.click(shrine, first_believer, events_cog.SHRINE_BUTTON_ID),
                                         game.click(shrine, second_believer, events_cog.SHRINE_BUTTON_ID))
    assert len(blessings) == 1
    winner, loser = (first, second) if blessings == [first_believer] else (second, first)
    assert winner.replies == [] and loser.answers == [events_cog.MSG_NO_SHRINE]
    assert game.channel.count("W kapliczce modli się") == 1 and not game.bot.state.event_alive


@pytest.mark.parametrize("typed_first", [False, True])
async def test_prayer_click_racing_a_typed_prayer_blesses_once(game, cog, blessings, typed_first):
    clicker, typist = supporters(game, 2)
    await cog.run_one_event(forced=EventType.SHRINE)
    shrine = game.channel.messages[-1]
    ctx = ctx_of(game, typist, "$modlitwa")
    click = game.click(shrine, clicker, events_cog.SHRINE_BUTTON_ID)
    typed = cog.pray.callback(cog, ctx)
    results = await asyncio.gather(*((typed, click) if typed_first else (click, typed)))
    interaction = results[1] if typed_first else results[0]
    assert blessings == ([typist] if typed_first else [clicker])
    if typed_first:
        assert ctx.message.reactions == [events_cog.PRAYGE]
        assert interaction.answers == [events_cog.MSG_NO_SHRINE]
    else:
        assert ctx.message.reactions == [] and interaction.replies == []
        assert game.channel.count(events_cog.MSG_NO_SHRINE) == 1
    assert game.channel.count("W kapliczce modli się") == 1 and shrine.view is None


async def test_a_non_believer_in_the_race_does_not_keep_the_shrine_from_a_supporter(game, cog, blessings):
    """The non-supporter's claim is given back - to the supporter who comes next, not to a second prayer."""
    ala = game.add_member("Ala")
    believer, late_believer = supporters(game, 2)
    await cog.run_one_event(forced=EventType.SHRINE)
    shrine = game.channel.messages[-1]
    refused, prayed, late = await asyncio.gather(
        game.click(shrine, ala, events_cog.SHRINE_BUTTON_ID),
        game.click(shrine, believer, events_cog.SHRINE_BUTTON_ID),
        game.click(shrine, late_believer, events_cog.SHRINE_BUTTON_ID))
    assert refused.answers == [events_cog.MSG_NOT_A_BELIEVER]
    assert prayed.replies == [] and late.answers == [events_cog.MSG_NO_SHRINE]
    assert blessings == [believer] and not game.bot.state.event_alive


# -- (3) import_all_modules ---------------------------------------------------------------------

def modules_on_disk() -> set[str]:
    """Every module of the package, found by walking its folder (not by pkgutil, as the bot does)."""
    names = set()
    for path in PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(PACKAGE_DIR.parent).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join(parts))
    return names


def test_import_all_modules_imports_every_module_but_the_entry_point():
    assert bot_module.import_all_modules() == []
    expected = modules_on_disk() - {ENTRY_POINT}
    assert len(expected) > 50                                            # (the walk found the package)
    assert sorted(expected - set(sys.modules)) == []


def test_import_all_modules_never_raises_and_names_what_failed(monkeypatch, caplog):
    tried: list[str] = []
    broken = {"altermmo.features.xpsteal": ImportError("test: cannot import name 'add_count'"),
              "altermmo.cogs.slayer": SyntaxError("test: half-saved file")}

    def import_module(name):
        tried.append(name)
        if name in broken:
            raise broken[name]
        return importlib.import_module(name)

    monkeypatch.setattr(bot_module, "importlib", SimpleNamespace(import_module=import_module))
    failed = bot_module.import_all_modules()
    assert sorted(failed) == sorted(broken)
    # The others are imported all the same (the package itself is in memory already).
    assert set(tried) == modules_on_disk() - {"altermmo", ENTRY_POINT}
    assert ENTRY_POINT not in tried
    assert all(f"Module {name} cannot be imported." in caplog.text for name in broken)


def test_import_all_modules_survives_a_broken_sub_package(monkeypatch):
    class Broken:
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name == "altermmo.cogs":
                raise SyntaxError("test: half-saved file")
            return None

    for name in [name for name in sys.modules if name == "altermmo.cogs" or name.startswith("altermmo.cogs.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [Broken(), *sys.meta_path])
    failed = bot_module.import_all_modules()
    assert "altermmo.cogs" in failed and "altermmo.features.events" not in failed


@pytest.fixture
def offline_start(game, monkeypatch, tmp_path):
    """``setup_hook`` with nothing reachable: no database, no project folder, extensions recorded."""
    steps: list[str] = []

    async def refused(self):
        raise ConnectionRefusedError("test: no database here")

    async def load_extension(name):
        steps.append(name)

    monkeypatch.setattr(type(game.bot.db), "connect", refused)
    monkeypatch.setattr(game.bot, "load_extension", load_extension)
    monkeypatch.setattr(bot_module, "PROJECT_ROOT", tmp_path)            # no state files are looked for there
    monkeypatch.setattr(petcards, "pillow_available", lambda: True)
    return steps


def notes_besides_the_database(bot) -> list[str]:
    down = bot_module.MSG_DATABASE_DOWN_AT_START.format(error="ConnectionRefusedError")
    return [note for note in bot._startup_notes if note != down]


async def test_setup_hook_imports_everything_before_the_extensions(game, offline_start, monkeypatch):
    steps = offline_start
    real = bot_module.import_all_modules

    def import_all_modules():
        steps.append("import_all_modules")
        return real()

    monkeypatch.setattr(bot_module, "import_all_modules", import_all_modules)
    await game.bot.setup_hook()
    assert steps == ["import_all_modules", *bot_module.EXTENSIONS]
    assert notes_besides_the_database(game.bot) == []                    # everything was importable


async def test_setup_hook_tells_the_owner_about_modules_that_cannot_be_imported(game, offline_start,
                                                                               monkeypatch):
    steps = offline_start
    broken = ["altermmo.features.xpsteal", "altermmo.cogs.slayer"]
    monkeypatch.setattr(bot_module, "import_all_modules", lambda: steps.append("import_all_modules") or broken)
    await game.bot.setup_hook()                                          # starts all the same
    assert steps == ["import_all_modules", *bot_module.EXTENSIONS]
    note = bot_module.MSG_MODULES_BROKEN.format(modules="altermmo.features.xpsteal, altermmo.cogs.slayer")
    assert notes_besides_the_database(game.bot) == [note]
    await game.bot.on_ready()
    told = game.log_channel.messages[-1].content
    assert told.startswith(f"<@{game.bot.ids.owner}>!   Uwagi po starcie bota:") and note in told


# -- (4) static guard: no lazy import is left for later -------------------------------------------

def _module_of(path: Path) -> str:
    parts = path.relative_to(PACKAGE_DIR.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def lazy_imports() -> dict[str, set[str]]:
    """``{module: project modules it imports inside a function body}`` for the whole package."""
    on_disk = modules_on_disk()
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_of(path)
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                targets: list[str] = []
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        base = ".".join(package.split(".")[:len(package.split(".")) - node.level + 1])
                        base = f"{base}.{node.module}" if node.module else base
                    else:
                        base = node.module or ""
                    targets = [base, *(f"{base}.{alias.name}" for alias in node.names)]
                elif isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                targets = [name for name in targets if name.split(".")[0] == "altermmo"]
                # (``from . import x``: x is a module when it has a file; otherwise a name of the package.)
                found.setdefault(module, set()).update(name for name in targets if name in on_disk)
    return {module: names for module, names in found.items() if names}


def test_the_guard_sees_the_lazy_imports_of_the_incident():
    imports = lazy_imports()
    assert "altermmo.features.events" in imports["altermmo.cogs.events"]
    assert "altermmo.features.achievements" in imports["altermmo.features.hub"]
    assert "altermmo.cogs.help" in imports["altermmo.features.hub"]


def test_every_lazily_imported_module_is_imported_at_start_up():
    """A fresh interpreter: only ``altermmo.bot`` (what ``__main__`` imports), then ``import_all_modules``.

    Afterwards no module of the package may be missing from memory - in
    particular none that some function imports only when it is first needed.
    """
    script = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import altermmo.bot\n"
        "before = sorted(name for name in sys.modules if name.split('.')[0] == 'altermmo')\n"
        "failed = altermmo.bot.import_all_modules()\n"
        "after = sorted(name for name in sys.modules if name.split('.')[0] == 'altermmo')\n"
        "print(json.dumps({'before': before, 'failed': failed, 'after': after}))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(PACKAGE_DIR.parent)],
                          capture_output=True, text=True, timeout=180, cwd=str(Path(__file__).resolve().parent))
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    expected = modules_on_disk() - {ENTRY_POINT}
    lazily_imported = set().union(*lazy_imports().values()) - {ENTRY_POINT}
    assert result["failed"] == []
    assert sorted(expected - set(result["after"])) == []
    assert sorted(lazily_imported - set(result["after"])) == []
    # Without the call these would have been read from the disk later, while the bot runs.
    assert "altermmo.cogs.events" not in result["before"] and "altermmo.cogs.events" in result["after"]
