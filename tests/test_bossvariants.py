"""Special boss variants (altermmo/features/bossvariants.py).

Owner's request of 2026-11-22: "Czasami tacy bossowie mogą się wylosować z elementami
kosmetycznymi, im rzadszy boss tym większe szanse na drop jajek z elementami kosmetycznymi."
Here: the config and how tolerantly it is read, the roll, the boss picture and its embed, the
repeated cosmetic roll of an egg, the admin preview and the patch notes entry.  How the variant
travels from the picture to the loot is tested where those parts are tested: test_boss.py (the
fights and the loot), test_boss_cycle.py (the life-cycle), test_owner_requests.py (the ritual)
and test_tower.py.

Every test starts with a config whose chance is 0 (conftest ``_ordinary_bosses``); the tests
here point ``bossvariants.CONFIG_DIR`` at their own file (``config``).  The shipped file
(``shipped``) is checked for its SHAPE only - it loads without a warning, every boss picture
has a variant and a framed portrait, its numbers are within their limits: the chance and the
repeats are the owner's to tune (the chance at 0 switches the special bosses off, a rarity
at 0 repeats keeps the look without the bonus), and no retune may turn a test red.  How the
game behaves for given numbers is tested on the tests' own numbers.
"""

from __future__ import annotations

import json
import logging
import random
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from altermmo import settings
from altermmo.cogs import boss as boss_cog
from altermmo.core.state import BossInfo, BossPhase
from altermmo.features import boss, bossvariants, modifiers, patchnotes, patrons, petcards, pets
from altermmo.features.bossvariants import Config, Variant

from fakes import FakeMember, FakeMessage
from test_pet_cosmetics import cosmetics, crafter, dice, everything
from test_pets import rig

FULL, PARTIAL = petcards.FOIL_FULL, petcards.FOIL_PARTIAL
ORDINARY_DIR = settings.ASSETS_DIR / "boss"
SPECIAL_DIR = ORDINARY_DIR / "special"
EPIC = ("Pojawił się epicki boss! Zabij go natychmiast, żeby zgarnąć nagrody! "
        "Kliknij **Zaatakuj** albo wpisz **!zaatakuj**, żeby rozpocząć walkę! ⚔️")
MIST = Variant("mgla", "Mgła", "🌫️ Tego bossa spowija **Mgła**.")
#: A small config of the tests' own: every boss picture the tests draw is "Mgła", always.
OWN = {
    "chance_percent": 100,
    "cosmetic_rerolls": {"0": 1, "1": 2, "2": 3, "3": 4, "4": 5},
    "variants": {MIST.id: {"name": MIST.name, "line": MIST.line}},
    "bosses": {"0": MIST.id, "7": MIST.id, "100": MIST.id, "1000": MIST.id, "10007": MIST.id},
}


# -- fixtures and helpers ---------------------------------------------------------------------

@pytest.fixture
def shipped(monkeypatch) -> Config:
    """The game's own ``config/bossVariants.json``."""
    monkeypatch.setattr(bossvariants, "CONFIG_DIR", settings.CONFIG_DIR)
    return bossvariants.load()


@pytest.fixture
def config(monkeypatch, tmp_path):
    """``config(chance_percent=0)`` writes :data:`OWN` with those keys replaced; ``config(raw="...")`` any text."""
    folder = tmp_path / "variants"
    folder.mkdir()
    monkeypatch.setattr(bossvariants, "CONFIG_DIR", folder)

    def write(raw: str | None = None, *, encoding: str = "utf-8", **changes) -> Path:
        path = folder / bossvariants.CONFIG_FILE
        text = json.dumps({**OWN, **changes}, ensure_ascii=False) if raw is None else raw
        path.write_text(text, encoding=encoding)
        return path

    write()
    return write


@pytest.fixture
def draws(monkeypatch) -> list[str]:
    """Scripted dice of ``send_boss_image``; returns the order in which they were cast.

    ``randint`` gives its lowest value (no player boss, the first picture of the rarity) and
    ``random`` 0.0 (the special variant, whatever the chance above 0).
    """
    order: list[str] = []
    monkeypatch.setattr(random, "randint", lambda low, high: order.append("randint") or low)
    monkeypatch.setattr(random, "random", lambda: order.append("random") or 0.0)
    return order


def uploads(game, monkeypatch) -> list[Path]:
    """Every file the bot uploads to the game channel from now on, as its full path.

    (The fake channel keeps only the file name - and a special portrait has the name of the
    ordinary one.)
    """
    paths: list[Path] = []
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        if kwargs.get("file") is not None:
            paths.append(Path(kwargs["file"].fp.name))
        return await real_send(content, **kwargs)

    monkeypatch.setattr(game.channel, "send", send)
    return paths


def boss_embed(game):
    return game.channel.messages[-1].embeds[0]


def warnings_of(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == bossvariants.log.name and record.levelno >= logging.WARNING]


# -- the shipped config -------------------------------------------------------------------------

def test_the_shipped_config_is_valid_and_covers_every_boss_picture(shipped, caplog):
    with caplog.at_level(logging.WARNING, logger=bossvariants.log.name):
        loaded = bossvariants.load()
    assert warnings_of(caplog) == []                              # nothing had to be left out
    # (0 is the owner's switch-off, 100 "every boss": both are valid settings.)
    assert loaded == shipped and 0 <= shipped.chance_percent <= 100
    raw = json.loads((settings.CONFIG_DIR / bossvariants.CONFIG_FILE).read_text(encoding="utf-8"))
    assert set(raw) == {"chance_percent", "cosmetic_rerolls", "variants", "bosses"}
    assert raw["chance_percent"] == shipped.chance_percent        # as written: nothing was cut to the limits
    assert len(shipped.bosses) == len(raw["bosses"]) and len(shipped.variants) == len(raw["variants"]) >= 1

    pictures = {int(path.stem) for path in ORDINARY_DIR.glob("*.gif")}
    assert len(pictures) == 40 and set(shipped.bosses) == pictures
    # Exactly the pictures a boss of each rarity can draw (boss._boss_picture).
    assert pictures == {first + offset for first in (0, 10, 100, 1000, 10000) for offset in range(8)}
    assert {int(path.stem) for path in SPECIAL_DIR.glob("*.gif")} == pictures        # no stray file either
    for number in sorted(pictures):
        variant = bossvariants.variant_for(number)
        assert variant is not None and variant == shipped.variants[shipped.bosses[number]]
        assert bossvariants.special_picture(number) == SPECIAL_DIR / f"{number}.gif"
        assert variant.name in variant.line and variant.line.strip() == variant.line


def test_the_shipped_repeats_never_fall_with_the_rarity(shipped):
    """ "im rzadszy boss tym większe szanse": a rarer special boss never has fewer repeats.

    The shape of the owner's numbers, not the numbers: 0 (the look without the bonus) and
    equal neighbours are his to choose.
    """
    assert set(shipped.rerolls) >= set(range(5))                                       # every rarity is named
    repeats = [bossvariants.rerolls_for(MIST.id, rarity) for rarity in range(5)]
    assert repeats == [shipped.rerolls[rarity] for rarity in range(5)]
    assert all(0 <= low <= high for low, high in zip(repeats, repeats[1:]))
    # What the egg code is asked for is never more than it accepts.
    assert 0 <= repeats[0] and repeats[-1] <= bossvariants.MAX_REROLLS <= pets.MAX_COSMETIC_REROLLS
    assert bossvariants.rerolls_for(MIST.id, 5) == 0                                   # no such rarity
    assert bossvariants.rerolls_for("", 4) == 0                                        # an ordinary boss


def test_the_rarer_the_boss_the_more_repeats(config):
    """The bonus of a special boss is the number the config gives its rarity (the tests' own numbers)."""
    assert [bossvariants.rerolls_for(MIST.id, rarity) for rarity in range(6)] == [1, 2, 3, 4, 5, 0]
    assert [bossvariants.rerolls_for("", rarity) for rarity in range(5)] == [0] * 5    # an ordinary boss
    # The variant id is not looked up: a boss that was shown as special keeps its bonus.
    assert bossvariants.rerolls_for("no-longer-in-the-config", 4) == 5
    config(cosmetic_rerolls={"0": 0, "1": 0, "2": 2, "3": 2, "4": 10})                 # whatever the owner chose
    assert [bossvariants.rerolls_for(MIST.id, rarity) for rarity in range(5)] == [0, 0, 2, 2, 10]


def test_special_portraits_are_gifs_that_fit_the_upload_limit():
    for path in SPECIAL_DIR.glob("*.gif"):
        with path.open("rb") as file:
            assert file.read(6) == b"GIF89a", path.name
        assert path.stat().st_size <= boss.DEFAULT_UPLOAD_LIMIT, path.name


def test_what_players_read_gives_nothing_away(shipped):
    """Owner's rules: no names of card ornaments, no chances; purely cosmetic, supporters only."""
    texts = [bossvariants.HINT, bossvariants.DEFAULT_LINE.format(name="Mgła"),
             *(variant.line for variant in shipped.variants.values())]
    for text in texts:
        assert patchnotes.spoiler_in_text(text) is None, text
        assert not any(char.isdigit() for char in text) and "lobby" not in text.casefold()
    assert "wyłącznie elementy kosmetyczne" in bossvariants.HINT and "tylko dla wspierających" in bossvariants.HINT
    assert "im rzadszy boss, tym większą" in bossvariants.HINT
    assert bossvariants.description(MIST) == "\n\n" + MIST.line + "\n" + bossvariants.HINT
    assert bossvariants.description(MIST, bonus=True) == bossvariants.description(MIST)
    # A special boss whose rarity has no bonus keeps its sentence and promises nothing.
    assert bossvariants.description(MIST, bonus=False) == "\n\n" + MIST.line


# -- a config that cannot be used: ordinary bosses, never an exception ----------------------------

def assert_no_special_bosses() -> None:
    assert bossvariants.load() == Config()
    for special in (None, True, False):
        assert bossvariants.roll(100, special=special) is None
    assert bossvariants.variant_for(100) is None
    assert bossvariants.rerolls_for(MIST.id, 2) == 0


def test_the_default_of_the_tests_is_no_special_boss():
    assert bossvariants.load() == Config() and bossvariants.roll(100) is None


def test_a_missing_file_means_ordinary_bosses(config, caplog):
    config().unlink()
    with caplog.at_level(logging.WARNING, logger=bossvariants.log.name):
        assert_no_special_bosses()
    assert any("is missing" in line for line in warnings_of(caplog))


@pytest.mark.parametrize("raw", ["", "{ to nie jest json", "[]", '"tekst"', "null", "12", '{"chance_percent": 1e999}',
                                 '{"chance_percent": 50, "variants": [], "bosses": 7, "cosmetic_rerolls": "x"}'])
def test_a_broken_file_means_ordinary_bosses(config, caplog, raw):
    config(raw=raw)
    with caplog.at_level(logging.WARNING, logger=bossvariants.log.name):
        assert bossvariants.roll(100, special=True) is None
        assert bossvariants.variant_for(100) is None and bossvariants.rerolls_for(MIST.id, 2) == 0
        loaded = bossvariants.load()
    assert not loaded.variants and not loaded.bosses and not loaded.rerolls
    assert warnings_of(caplog)


def test_a_file_that_is_not_utf8_means_ordinary_bosses(config):
    plain = {**OWN, "variants": {MIST.id: {"name": MIST.name}}}
    config(raw=json.dumps(plain, ensure_ascii=False), encoding="cp1250")     # "Mgła" in a Windows code page
    assert_no_special_bosses()
    config(raw=json.dumps(plain, ensure_ascii=False))                        # (the same text as UTF-8 works)
    assert bossvariants.roll(100, special=True) == Variant(MIST.id, MIST.name, "✨ Ten boss pojawił się "
                                                           "w wyjątkowej odsłonie: **Mgła**.")


def test_a_byte_order_mark_from_a_windows_editor_is_fine(config):
    config(raw=json.dumps(OWN, ensure_ascii=False), encoding="utf-8-sig")
    assert bossvariants.roll(100, special=True) == MIST


@pytest.mark.parametrize("value, chance", [
    (20, 20.0), (0, 0.0), (100, 100.0), (12.5, 12.5), (-5, 0.0), (250, 100.0),
    ("20", 0.0), (True, 0.0), (None, 0.0), ([20], 0.0)])
def test_chance_is_a_number_between_0_and_100(config, value, chance):
    config(chance_percent=value)
    assert bossvariants.load().chance_percent == chance


def test_bad_entries_are_left_out_and_the_rest_works(config, caplog):
    config(
        cosmetic_rerolls={"0": 1, "1": "dwa", "2": 99, "3": -4, "4": 2.0, "x": 3, "5": True, "6": 1.5},
        variants={"mgla": OWN["variants"]["mgla"], "bez-nazwy": {"line": "x"}, "pusta": {"name": "  "},
                  "tekst": "Mgła", "bez-zdania": {"name": "Szron"}},
        bosses={"100": "mgla", "101": "nieznany", "102": "bez-nazwy", "sto": "mgla", "103": 5,
                "104": "bez-zdania", " 105 ": "mgla"})
    with caplog.at_level(logging.WARNING, logger=bossvariants.log.name):
        loaded = bossvariants.load()
    assert loaded.rerolls == {0: 1, 2: bossvariants.MAX_REROLLS, 3: 0, 4: 2}
    assert loaded.variants == {"mgla": MIST, "bez-zdania": Variant(
        "bez-zdania", "Szron", "✨ Ten boss pojawił się w wyjątkowej odsłonie: **Szron**.")}
    assert loaded.bosses == {100: "mgla", 104: "bez-zdania", 105: "mgla"}
    assert len(warnings_of(caplog)) == 4 + 2 + 3 + 4          # rerolls (2 of them cut), variants, bosses

    assert bossvariants.roll(100, special=True) == MIST
    assert bossvariants.roll(101, special=True) is None       # an unknown variant: an ordinary boss
    assert bossvariants.roll(102, special=True) is None
    assert [bossvariants.rerolls_for("mgla", rarity) for rarity in range(7)] == [1, 0, 10, 0, 2, 0, 0]
    for rarity in ("x", None, [], {}, "", "2.5"):
        assert bossvariants.rerolls_for("mgla", rarity) == 0            # no rarity: no bonus, never an exception
    for rarity in (2, "2", Decimal("2")):
        assert bossvariants.rerolls_for("mgla", rarity) == 10           # a rarity in any form int() takes


def test_a_very_long_line_is_cut(config):
    config(variants={"mgla": {"name": "Mgła", "line": "x" * 5000}})
    assert len(bossvariants.load().variants["mgla"].line) == bossvariants.MAX_LINE_LENGTH


# -- the roll -------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, special", [(0.0, True), (0.1999, True), (0.2, False), (0.99, False)])
def test_the_roll_is_one_draw_against_the_chance(config, monkeypatch, value, special):
    config(chance_percent=20)
    cast: list[float] = []
    monkeypatch.setattr(random, "random", lambda: cast.append(value) or value)
    assert bossvariants.roll(100) == (MIST if special else None)
    assert len(cast) == 1


def test_nothing_is_drawn_when_the_answer_is_known(config, monkeypatch, tmp_path, caplog):
    cast: list[int] = []
    monkeypatch.setattr(random, "random", lambda: cast.append(1) or 0.0)
    assert bossvariants.roll(100, special=True) == MIST           # forced
    assert bossvariants.roll(100, special=False) is None          # forbidden
    assert bossvariants.roll(55) is None                          # no variant for this picture
    assert bossvariants.roll("x") is None and bossvariants.roll(None) is None
    config(chance_percent=0)
    assert bossvariants.roll(100) is None                         # switched off...
    assert bossvariants.roll(100, special=True) == MIST           # ...but the admin can still look at it
    config()
    monkeypatch.setattr(bossvariants, "ASSETS_DIR", tmp_path)     # the framed portrait is not there
    with caplog.at_level(logging.WARNING, logger=bossvariants.log.name):
        assert bossvariants.roll(100) is None and bossvariants.roll(100, special=True) is None
        assert bossvariants.variant_for(100) is None
    assert any("is missing" in line for line in warnings_of(caplog))
    assert cast == []


def test_the_roll_never_raises(config, monkeypatch):
    def broken():
        raise RuntimeError("no dice")

    monkeypatch.setattr(random, "random", broken)
    assert bossvariants.roll(100) is None
    monkeypatch.setattr(bossvariants, "load", broken)
    assert bossvariants.roll(100, special=True) is None
    assert bossvariants.variant_for(100) is None and bossvariants.rerolls_for("mgla", 2) == 0


def test_the_module_is_imported_before_the_bot_logs_in():
    """Like every module (bot.import_all_modules): a running bot never reads its code from the disk again."""
    import pkgutil
    import sys

    import altermmo
    from altermmo import bot as bot_module

    names = {info.name for info in pkgutil.walk_packages(altermmo.__path__, "altermmo.")}
    assert bossvariants.__name__ in names and bot_module.import_all_modules() == []
    assert sys.modules[bossvariants.__name__] is bossvariants and boss.bossvariants is bossvariants


def test_variant_of_reads_every_shape():
    assert bossvariants.variant_of(boss.BossShown(False, None, "mgla")) == "mgla"
    assert bossvariants.variant_of(boss.BossShown(True, "Szef")) == ""
    assert bossvariants.variant_of((False, None)) == ""           # the old pair (stand-ins in tests)
    assert bossvariants.variant_of((False, None, None)) == "" and bossvariants.variant_of(None) == ""
    assert tuple(boss.BossShown(False, None)) == (False, None, "")


# -- the boss picture -------------------------------------------------------------------------------

async def test_special_boss_shows_the_framed_portrait_and_says_so(game, config, draws, monkeypatch):
    config(chance_percent=20)                                     # now and then; the dice say "now" (0.0)
    files = uploads(game, monkeypatch)
    shown = await boss.send_boss_image(game.scene(), 2)
    assert isinstance(shown, boss.BossShown) and shown == (False, None, MIST.id)
    assert files == [SPECIAL_DIR / "100.gif"]                     # instead of assets/boss/100.gif
    picture, embed_message = game.channel.messages[-2:]
    assert picture.files == ["100.gif"]
    embed = embed_message.embeds[0]
    # The title, the colour and the button are those of any epic boss...
    assert embed.title == "💀 🟪 Epicki boss! 🟪 💀" and embed.colour.value == 0x800080
    assert embed_message.button_labels == ["Zaatakuj"]
    # ...the description says what is special, on its own lines.
    assert embed.description == EPIC + "\n\n" + MIST.line + "\n" + bossvariants.HINT
    # The variant is rolled after the draws the picture always made.
    assert draws == ["randint", "randint", "random"]


async def test_most_bosses_stay_ordinary(game, config, draws, monkeypatch):
    config(chance_percent=20)
    monkeypatch.setattr(random, "random", lambda: draws.append("random") or 0.999)     # the dice say "not now"
    files = uploads(game, monkeypatch)
    assert await boss.send_boss_image(game.scene(), 2) == (False, None, "")
    assert files == [ORDINARY_DIR / "100.gif"] and boss_embed(game).description == EPIC
    assert draws == ["randint", "randint", "random"]


@pytest.mark.parametrize("rarity, picture", [(0, 0), (1, 10), (2, 100), (3, 1000), (4, 10000)])
async def test_a_boss_of_any_rarity_can_be_special(game, config, draws, monkeypatch, rarity, picture):
    config(bosses={str(number): MIST.id for number in (0, 10, 100, 1000, 10000)})
    files = uploads(game, monkeypatch)
    shown = await boss.send_boss_image(game.scene(), rarity)
    assert shown.variant == MIST.id and files == [SPECIAL_DIR / f"{picture}.gif"]
    description = boss_embed(game).description
    assert description.endswith("\n\n" + MIST.line + "\n" + bossvariants.HINT)
    assert description.count(bossvariants.HINT) == 1 and description.count(MIST.line) == 1


@pytest.mark.parametrize("rarity", range(5))
async def test_the_shipped_portraits_and_sentences_can_be_shown(game, shipped, draws, monkeypatch, rarity):
    """The owner's own file, whatever its numbers: forced (no chance is asked), the embed says what the loot pays."""
    files = uploads(game, monkeypatch)
    shown = await boss.send_boss_image(game.scene(), rarity, special=True)
    number = int(files[0].stem)
    variant = shipped.variants[shipped.bosses[number]]
    assert shown == (False, None, variant.id) and files == [SPECIAL_DIR / f"{number}.gif"]
    description = boss_embed(game).description
    assert description.count(variant.line) == 1 and "random" not in draws
    # The promise of a better egg stands exactly when this rarity has a bonus (0 = the look alone).
    assert (bossvariants.HINT in description) == (shipped.rerolls.get(rarity, 0) > 0)
    assert boss._special_loot(variant.id, rarity) == (
        {"cosmetic_rerolls": shipped.rerolls[rarity]} if shipped.rerolls.get(rarity, 0) > 0 else {})


# -- the promise in the embed is the bonus the loot pays ----------------------------------------------

@pytest.mark.parametrize("rerolls", [
    {"2": 0},                                     # the owner: "the look alone for epic bosses"
    {},                                           # no bonus anywhere: the frames as pure decoration
    {"0": 1, "1": 2, "3": 4, "4": 5},             # the entry of this rarity is not there
    {"2": 1.5}, {"2": "trzy"}, {"2": True},       # a slip of the hand: left out with a warning
    [1, 2, 3], None,                              # not an object at all
])
async def test_special_boss_without_a_bonus_keeps_its_look_and_promises_nothing(game, config, draws, monkeypatch,
                                                                                 rerolls):
    """No false promise: the hint about the egg is shown only when the loot of this rarity pays a bonus."""
    config(cosmetic_rerolls=rerolls)
    files = uploads(game, monkeypatch)
    shown = await boss.send_boss_image(game.scene(), 2)
    # Still a special boss: the framed portrait, its sentence, its variant for the boss cycle...
    assert shown == (False, None, MIST.id) and files == [SPECIAL_DIR / "100.gif"]
    assert boss_embed(game).description == EPIC + "\n\n" + MIST.line
    assert bossvariants.HINT not in boss_embed(game).description
    assert game.channel.messages[-1].button_labels == ["Zaatakuj"]
    # ...and the loot pays what the embed said: an ordinary egg, by the old call.
    assert bossvariants.rerolls_for(shown.variant, 2) == 0 and boss._special_loot(shown.variant, 2) == {}
    assert draws == ["randint", "randint", "random"]              # (asking for the bonus casts no dice)


async def test_the_hint_follows_the_bonus_of_each_rarity(game, config, draws, monkeypatch):
    config(cosmetic_rerolls={"0": 0, "2": 0, "3": 4, "4": 1},
           bosses={str(number): MIST.id for number in (0, 10, 100, 1000, 10000)})
    for rarity, promised in ((0, False), (1, False), (2, False), (3, True), (4, True)):
        shown = await boss.send_boss_image(game.scene(), rarity)
        description = boss_embed(game).description
        assert shown.variant == MIST.id and description.count(MIST.line) == 1
        assert (bossvariants.HINT in description) is promised, rarity
        assert bool(boss._special_loot(shown.variant, rarity)) is promised
        assert description.endswith(MIST.line + ("\n" + bossvariants.HINT if promised else ""))


async def test_the_hint_is_left_out_before_the_modifiers_of_the_shrine(game, config, draws):
    config(cosmetic_rerolls={})
    await modifiers.modify_modifiers(game.bot, "drop_boost_perc", 20)
    assert (await boss.send_boss_image(game.scene(), 2)).variant == MIST.id
    assert boss_embed(game).description == (EPIC + "\n\n" + MIST.line
                                            + "\n\nDodatkowe statystyki:\n🔺 Drop zwiększony o 20 %")


async def test_a_bonus_that_cannot_be_read_never_stops_the_boss(game, config, draws, monkeypatch, caplog):
    """Asking for the bonus never raises - and should it, the boss still appears (without the promise)."""
    real_load, loads = bossvariants.load, []

    def load_once():
        loads.append(1)
        if len(loads) > 1:                        # the roll read the file; the second read fails
            raise OSError("the disk is gone")
        return real_load()

    monkeypatch.setattr(bossvariants, "load", load_once)
    with caplog.at_level(logging.INFO, logger=boss.log.name):
        shown = await boss.send_boss_image(game.scene(), 2)
    assert shown == (False, None, MIST.id) and boss_embed(game).description == EPIC + "\n\n" + MIST.line
    assert any("shown without the hint" in record.getMessage() for record in caplog.records)


async def test_the_modifiers_of_the_shrine_are_still_listed(game, config, draws):
    await modifiers.modify_modifiers(game.bot, "drop_boost_perc", 20)
    assert (await boss.send_boss_image(game.scene(), 2)).variant == MIST.id
    assert boss_embed(game).description == (EPIC + "\n\n" + MIST.line + "\n" + bossvariants.HINT
                                            + "\n\nDodatkowe statystyki:\n🔺 Drop zwiększony o 20 %")


async def test_a_player_posing_as_the_boss_is_never_special(game, config, monkeypatch):
    cast: list[int] = []
    monkeypatch.setattr(random, "randint", lambda low, high: high)          # 100 % -> a supporter is the boss
    monkeypatch.setattr(random, "random", lambda: cast.append(1) or 0.0)
    patron = game.add_member("Patron", roles=[patrons.CRAFTER_ROLE])
    files = uploads(game, monkeypatch)
    for special in (None, True):                                            # not even when forced
        assert await boss.send_boss_image(game.scene(), 2, special=special) == (True, patron, "")
        assert game.channel.messages[-2].content == patron.display_avatar.url
        assert bossvariants.HINT not in boss_embed(game).description and MIST.line not in boss_embed(game).description
    assert files == [] and cast == []                                       # no picture, no extra draw

    # The player the shrine chose, although the dice said "a monster".
    monkeypatch.setattr(random, "randint", lambda low, high: low)
    chosen = game.add_member("Wybraniec")
    await modifiers.modify_modifiers(game.bot, "player_id", chosen.id)
    assert await boss.send_boss_image(game.scene(), 1) == (True, chosen, "")
    assert bossvariants.HINT not in boss_embed(game).description and cast == []


async def test_a_missing_framed_portrait_means_an_ordinary_boss(game, config, draws, monkeypatch, tmp_path):
    monkeypatch.setattr(bossvariants, "ASSETS_DIR", tmp_path)
    files = uploads(game, monkeypatch)
    for special in (None, True):
        assert await boss.send_boss_image(game.scene(), 2, special=special) == (False, None, "")
        assert boss_embed(game).description == EPIC
    assert files == [ORDINARY_DIR / "100.gif"] * 2 and "random" not in draws


async def test_forced_and_forbidden(game, config, draws, monkeypatch):
    files = uploads(game, monkeypatch)
    config(chance_percent=0)                                                # the feature is switched off
    assert await boss.send_boss_image(game.scene(), 2) == (False, None, "")
    assert await boss.send_boss_image(game.scene(), 2, special=True) == (False, None, MIST.id)
    assert bossvariants.HINT in boss_embed(game).description
    config(chance_percent=100)                                              # every boss is special...
    assert await boss.send_boss_image(game.scene(), 2, special=False) == (False, None, "")
    assert boss_embed(game).description == EPIC                             # ...but this one may not be
    assert files == [ORDINARY_DIR / "100.gif", SPECIAL_DIR / "100.gif", ORDINARY_DIR / "100.gif"]
    assert "random" not in draws                                            # none of the three needed the dice
    assert await boss.send_boss_image(game.scene(), 2) == (False, None, MIST.id)
    assert draws.count("random") == 1


async def test_a_boss_without_a_variant_is_ordinary_even_when_forced(game, config, draws, monkeypatch):
    files = uploads(game, monkeypatch)
    assert await boss.send_boss_image(game.scene(), 1, special=True) == (False, None, "")   # picture 10: no variant
    assert files == [ORDINARY_DIR / "10.gif"] and "random" not in draws


@pytest.mark.parametrize("raw", [None, "{ zepsuty", "[]"])
async def test_a_broken_config_never_stops_the_boss(game, config, draws, monkeypatch, raw):
    if raw is None:
        config().unlink()
    else:
        config(raw=raw)
    files = uploads(game, monkeypatch)
    for special in (None, True):
        assert await boss.send_boss_image(game.scene(), 2, special=special) == (False, None, "")
    assert files == [ORDINARY_DIR / "100.gif"] * 2 and boss_embed(game).description == EPIC
    assert game.channel.messages[-1].button_labels == ["Zaatakuj"]


async def test_a_failing_roll_never_stops_the_boss(game, config, draws, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the variants are broken")

    monkeypatch.setattr(bossvariants, "roll", broken)
    assert await boss.send_boss_image(game.scene(), 2) == (False, None, "")
    assert boss_embed(game).description == EPIC and game.channel.messages[-2].files == ["100.gif"]


# -- the admin preview: !image <rarity> [wariant] -------------------------------------------------------

def image_ctx(game, admin):
    message = FakeMessage(game.channel, admin, "!image")
    return SimpleNamespace(bot=game.bot, channel=game.channel, author=admin, message=message, guild=game.guild)


def preview(game, since: int) -> tuple[list[str], str]:
    """What an ``!image`` call posted after message ``since``: (texts and file names, the boss embed's text)."""
    posted = game.channel.messages[since:]
    embed = next(message.embeds[0] for message in posted if message.embeds)
    return [message.content or ",".join(message.files) for message in posted if not message.embeds], embed.description


def test_every_special_word_is_a_plain_lowercase_word():
    assert {"s", "special", "specjalny", "1", "tak"} <= boss_cog.SPECIAL_WORDS      # the ones the owner was told
    assert all(word == word.strip().casefold() and word for word in boss_cog.SPECIAL_WORDS)
    for text in (boss_cog.MSG_PREVIEW_ONLY, boss_cog.MSG_PREVIEW_NO_VARIANT):
        assert patchnotes.spoiler_in_text(text) is None and "lobby" not in text.casefold()
        assert "%" not in text and "procent" not in text.casefold()


@pytest.mark.parametrize("word", [*sorted(boss_cog.SPECIAL_WORDS), "S", "Specjalny", " TAK ", "True"])
async def test_image_command_can_force_the_special_variant(game, config, draws, monkeypatch, word):
    config(chance_percent=0)                                      # only the second word can make it special
    cog = boss_cog.BossCog(game.bot)
    ctx = image_ctx(game, game.add_member("Admin"))
    files = uploads(game, monkeypatch)
    before = (game.bot.state.boss, game.bot.state.boss_phase, game.bot.state.busy)
    since = len(game.channel.messages)

    await cog.boss_image.callback(cog, ctx, 2, word)
    texts, description = preview(game, since)
    assert files == [SPECIAL_DIR / "100.gif"] and bossvariants.HINT in description
    # The portrait, the embed - and the note that this is no boss of the game.
    assert texts == ["100.gif", boss_cog.MSG_PREVIEW_ONLY]
    assert game.channel.messages[-1].content == boss_cog.MSG_PREVIEW_ONLY
    # A preview: the boss of the game is not touched.
    assert (game.bot.state.boss, game.bot.state.boss_phase, game.bot.state.busy) == before
    assert before[:2] == (BossInfo(), BossPhase.IDLE) and "random" not in draws
    assert draws == ["randint"]                                   # the picture; nobody is drawn as the boss


async def test_forced_preview_always_shows_a_monster(game, config, monkeypatch):
    """``!image <rarity> s`` exists to look at the framed portraits: no player takes the boss's place.

    Not the supporter the dice would pick (about every second real boss), not the player the
    shrine chose (who would be the boss EVERY time until that boss is gone).
    """
    cast: list[tuple] = []
    monkeypatch.setattr(random, "randint", lambda low, high: cast.append((low, high)) or high)
    monkeypatch.setattr(random, "random", lambda: cast.append(("random",)) or 0.0)
    patron = game.add_member("Patron", roles=[patrons.CRAFTER_ROLE])
    chosen = game.add_member("Wybraniec")
    cog = boss_cog.BossCog(game.bot)
    ctx = image_ctx(game, game.add_member("Admin"))
    files = uploads(game, monkeypatch)

    since = len(game.channel.messages)
    await cog.boss_image.callback(cog, ctx, 0, "s")               # the dice (all high) say "a supporter"
    texts, description = preview(game, since)
    assert files == [SPECIAL_DIR / "7.gif"] and texts == ["7.gif", boss_cog.MSG_PREVIEW_ONLY]
    assert MIST.line in description and "Bossem jest gracz" not in description
    assert cast == [(0, 7)]                                       # only the picture was drawn

    await modifiers.modify_modifiers(game.bot, "player_id", chosen.id)      # the shrine chose a player
    since = len(game.channel.messages)
    await cog.boss_image.callback(cog, ctx, 0, "specjalny")
    texts, description = preview(game, since)
    assert files[-1] == SPECIAL_DIR / "7.gif" and texts == ["7.gif", boss_cog.MSG_PREVIEW_ONLY]
    assert "Bossem jest gracz" not in description
    # ...and the choice of the shrine is still there for the real boss.
    assert (await modifiers.load_modifiers(game.bot))["player_id"] == chosen.id
    assert await boss.send_boss_image(game.scene(), 0) == (True, chosen, "")

    # Without the word the command rolls like a real spawn: here the player poses as the boss.
    since = len(game.channel.messages)
    await cog.boss_image.callback(cog, ctx, 0)
    texts, description = preview(game, since)
    assert texts == [chosen.display_avatar.url] and "Bossem jest gracz" in description
    assert bossvariants.HINT not in description and len(files) == 2
    assert patron.display_avatar.url not in game.channel.transcript
    assert game.bot.state.boss == BossInfo() and game.bot.state.boss_phase is BossPhase.IDLE


async def test_send_boss_image_monster_only_skips_every_player_boss(game, config, monkeypatch):
    cast: list[tuple] = []
    monkeypatch.setattr(random, "randint", lambda low, high: cast.append((low, high)) or high)
    monkeypatch.setattr(random, "random", lambda: cast.append(("random",)) or 0.0)
    game.add_member("Patron", roles=[patrons.CRAFTER_ROLE])
    config(chance_percent=50)
    assert await boss.send_boss_image(game.scene(), 0, monster_only=True) == (False, None, MIST.id)
    assert cast == [(0, 7), ("random",)]                          # the picture, then the variant as always
    cast.clear()
    assert await boss.send_boss_image(game.scene(), 0, monster_only=True, special=False) == (False, None, "")
    assert cast == [(0, 7)]
    # The game never passes it: a real boss makes the draws it always made, in their order.
    cast.clear()
    assert (await boss.send_boss_image(game.scene(), 0)).is_player_boss
    assert cast == [(0, 100)]


@pytest.mark.parametrize("why", ["no variant", "no file", "broken config"])
async def test_forced_preview_that_cannot_be_special_says_so(game, config, draws, monkeypatch, tmp_path, why):
    if why == "no variant":
        config(bosses={"0": MIST.id})                             # picture 100 has none
    elif why == "no file":
        monkeypatch.setattr(bossvariants, "ASSETS_DIR", tmp_path)
    else:
        config(raw="{ zepsuty")
    cog = boss_cog.BossCog(game.bot)
    ctx = image_ctx(game, game.add_member("Admin"))
    files = uploads(game, monkeypatch)
    since = len(game.channel.messages)
    await cog.boss_image.callback(cog, ctx, 2, "s")
    texts, description = preview(game, since)
    assert files == [ORDINARY_DIR / "100.gif"] and description == EPIC
    assert texts == ["100.gif", boss_cog.MSG_PREVIEW_NO_VARIANT]  # not a silent ordinary boss

    # Without the word an ordinary boss is nothing to explain.
    since = len(game.channel.messages)
    await cog.boss_image.callback(cog, ctx, 2)
    assert preview(game, since) == (["100.gif"], EPIC)


async def test_image_command_without_the_word_rolls_like_a_real_spawn(game, config, draws, monkeypatch):
    cog = boss_cog.BossCog(game.bot)
    ctx = image_ctx(game, game.add_member("Admin"))
    files = uploads(game, monkeypatch)
    config(chance_percent=0)
    since = len(game.channel.messages)
    await cog.boss_image.callback(cog, ctx, 2)
    await cog.boss_image.callback(cog, ctx, 2, "nie")             # any other word is not "special"
    await cog.boss_image.callback(cog, ctx, 9, "0")               # (the rarity is cut to 0-4, as before)
    assert files == [ORDINARY_DIR / "100.gif"] * 2 + [ORDINARY_DIR / "10000.gif"] and "random" not in draws
    # An ordinary boss: its picture and its embed, not a word more.
    assert [message.content for message in game.channel.messages[since:] if message.content] == []
    assert draws == ["randint", "randint"] * 3                    # the draws of a real spawn
    config(chance_percent=50)
    await cog.boss_image.callback(cog, ctx, 2)                    # the dice say "special" (0.0)
    assert files[-1] == SPECIAL_DIR / "100.gif" and draws.count("random") == 1
    assert game.channel.messages[-1].content == boss_cog.MSG_PREVIEW_ONLY   # it looks like the real thing
    assert game.bot.state.boss == BossInfo() and game.bot.state.boss_phase is BossPhase.IDLE


async def test_a_preview_on_the_boss_channel_does_not_touch_the_boss_standing_there(game, config, draws):
    """The preview's button attacks the real boss - an ordinary one keeps being ordinary."""
    state = game.bot.state
    state.boss = BossInfo(rarity=2)
    await state.set_boss_phase(BossPhase.VISIBLE)
    cog = boss_cog.BossCog(game.bot)
    await cog.boss_image.callback(cog, image_ctx(game, game.add_member("Admin")), 2, "s")
    assert state.boss == BossInfo(rarity=2) and state.boss.variant == "" and state.boss_phase is BossPhase.VISIBLE
    assert game.channel.messages[-1].content == boss_cog.MSG_PREVIEW_ONLY
    assert "nie zmienia gry" in boss_cog.MSG_PREVIEW_ONLY and "prawdziwego bossa" in boss_cog.MSG_PREVIEW_ONLY


async def test_status_shows_a_dash_for_an_ordinary_boss(game_db, monkeypatch):
    """``!status``: an empty value (the variant of an ordinary boss) is no empty code span."""
    bot = game_db.bot
    # (A bot that never logged in has no gateway ping - NaN; the report of a running one has a number.)
    monkeypatch.setattr(type(bot), "latency", 0.085)
    cog = boss_cog.BossCog(bot)
    ctx = image_ctx(game_db, game_db.add_member("Admin"))
    assert bot.state.snapshot()["boss_variant"] == ""             # the state keeps the real value...
    await cog.status.callback(cog, ctx)
    report = game_db.channel.messages[-1].content
    assert "- boss_variant: `-`" in report and "``" not in report  # ...the report shows a dash
    # 0 and False are values, not "nothing".
    assert "- boss_rarity: `0`" in report and "- is_player_boss: `False`" in report

    bot.state.boss = BossInfo(rarity=2, variant="mgla")
    await cog.status.callback(cog, ctx)
    report = game_db.channel.messages[-1].content
    assert "- boss_variant: `mgla`" in report and "- boss_rarity: `2`" in report and "``" not in report


# -- the egg of a special boss: the cosmetic roll is repeated ---------------------------------------------

def look(egg: dict) -> tuple:
    return egg["SEAL"], egg["HOLO"], egg.get("FRAME"), egg.get("HOLO_PARTIAL")


def test_base_look_is_no_seal_no_foil_and_the_base_frame():
    base = pets.Rolled(0, petcards.FOIL_NONE, "")
    assert pets._only_base_look(base) and pets._only_base_look(base, frames=False)
    for ornament in (pets.Rolled(1, petcards.FOIL_NONE, ""), pets.Rolled(0, FULL, ""),
                     pets.Rolled(0, petcards.FOIL_NONE, "S1")):
        assert not pets._only_base_look(ornament) and not pets._only_base_look(ornament, frames=False)
    # A partial Holo is an ornament only where it can be stored.
    assert not pets._only_base_look(pets.Rolled(0, PARTIAL, ""))
    assert pets._only_base_look(pets.Rolled(0, PARTIAL, ""), frames=False)


def test_a_base_result_is_rolled_again_and_the_first_ornament_is_kept(monkeypatch, caplog):
    rig(monkeypatch, randint=0)                                   # an ordinary egg: seal and foil only
    asked = dice(monkeypatch, 999, 999, 450, 33, 200, 999, 0, 0)  # base, base, then a seal (and it stops there)
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        egg = pets._roll_egg(crafter(), rerolls=5)
    assert look(egg) == (1, False, "", False)
    assert asked == [(1000,)] * 6                                 # three whole rolls, not the fourth
    logged = [record.getMessage() for record in caplog.records if "repeated" in record.getMessage()]
    assert len(logged) == 1 and "repeated 2 time(s) of 5 allowed; result: seal 1, foil 0, frame ''" in logged[0]


def test_a_first_result_with_an_ornament_is_never_rolled_again(monkeypatch, caplog):
    rig(monkeypatch, randint=0)
    for first in ((449, 999), (999, 32), (999, 0), (0, 0)):       # a seal / partial Holo / full Holo / both
        asked = dice(monkeypatch, *first, 0, 0)
        with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
            egg = pets._roll_egg(crafter(), rerolls=10)
        assert asked == [(1000,), (1000,)]
        assert (egg["SEAL"], egg["HOLO"] or egg["HOLO_PARTIAL"]) != (0, False)
    assert not any("repeated" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("rerolls, rolls", [(0, 1), (1, 2), (3, 4), (10, 11), (11, 11), (10 ** 9, 11), (-3, 1)])
def test_the_repeats_are_capped(monkeypatch, rerolls, rolls):
    rig(monkeypatch, randint=0)
    asked = dice(monkeypatch)                                     # every roll gives the base look
    egg = pets._roll_egg(crafter(), rerolls=rerolls)
    assert look(egg) == (0, False, "", False)                     # the last base result is kept
    assert asked == [(1000,), (1000,)] * rolls and pets.MAX_COSMETIC_REROLLS == 10


def test_the_whole_roll_is_repeated_also_the_frame(monkeypatch):
    rig(monkeypatch, randint=lambda low, high: 905 if high == 1000 else 0)     # a shiny egg: three dice
    asked = dice(monkeypatch, 999, 999, 0, 999, 999, 5199, 999, 999, 5200, 0, 0, 9999)
    egg = pets._roll_egg(crafter(), rerolls=4)
    assert look(egg) == (0, False, "S1", False) and egg["SHINY"] is True
    assert asked == [(1000,), (1000,), (10000,)] * 3              # the frame alone is an ornament: it stops


def test_without_repeats_the_draws_are_exactly_what_they_were(monkeypatch):
    """``rerolls=0`` (every egg that is not from a special boss): the same dice in the same order."""
    for egg_roll in (0, 905, 985, 1000):
        order: list[tuple] = []
        rig(monkeypatch, randint=lambda low, high: egg_roll if high == 1000 else 1)
        real_choice, real_randint = random.choice, random.randint
        monkeypatch.setattr(random, "choice", lambda pool: order.append(("choice",)) or real_choice(pool))
        monkeypatch.setattr(random, "randint", lambda low, high: order.append(("randint", low, high))
                            or real_randint(low, high))
        monkeypatch.setattr(random, "randrange", lambda *args: order.append(("randrange", *args)) or 999)
        plain = pets._roll_egg(crafter())
        before = list(order)
        order.clear()
        assert pets._roll_egg(crafter(), rerolls=0) == plain and order == before
        # With repeats: the same legacy draws first, then only cosmetic dice (the first roll's included).
        order.clear()
        repeated = pets._roll_egg(crafter(), rerolls=2)
        legacy = [draw for draw in before if draw[0] != "randrange"]
        cosmetic = [draw for draw in before if draw[0] == "randrange"]
        assert order == legacy + cosmetic * 3 and repeated == plain
        assert before == legacy + cosmetic                        # (the cosmetic rolls come last)


def test_nothing_is_rolled_for_a_non_supporter_whatever_the_boss(monkeypatch):
    rig(monkeypatch, randint=lambda low, high: 905 if high == 1000 else 0)
    asked = dice(monkeypatch, 0, 0, 9999)
    for nobody in (FakeMember("Ala"), None):
        egg = pets._roll_egg(nobody, rerolls=10)
        assert look(egg) == (0, False, "", False)
    assert asked == []
    # ... and without the columns nothing is rolled for anybody.
    egg = pets._roll_egg(crafter(), cosmetics=False, rerolls=10)
    assert asked == [] and "SEAL" not in egg


def test_without_the_frame_columns_a_partial_holo_counts_as_the_base_look(monkeypatch, caplog):
    """It cannot be stored there, so the egg would look like any other: the roll is repeated."""
    rig(monkeypatch, randint=lambda low, high: 985 if high == 1000 else 0)     # an ultra shiny egg
    asked = dice(monkeypatch, 999, 5, 999, 20, 999, 0, 0, 0)                   # partial, partial, full Holo
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        egg = pets._roll_egg(crafter(), frames=False, rerolls=5)
    assert (egg["SEAL"], egg["HOLO"]) == (0, True) and "FRAME" not in egg and "HOLO_PARTIAL" not in egg
    assert asked == [(1000,), (1000,)] * 3                        # no frame die without the columns
    assert not any("partial Holo of a new" in record.getMessage() for record in caplog.records)

    # Only partial results until the repeats run out: the last one is lost, as it always was (logged once).
    asked = dice(monkeypatch, 999, 5, 999, 5)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="altermmo.features.pets"):
        egg = pets._roll_egg(crafter(), frames=False, rerolls=1)
    assert (egg["SEAL"], egg["HOLO"]) == (0, False) and len(asked) == 4
    assert len([record for record in caplog.records if "partial Holo of a new" in record.getMessage()]) == 1

    # With the columns a partial Holo is an ornament like any other: kept at once.
    asked = dice(monkeypatch, 999, 5, 0, 0, 0, 9999)
    egg = pets._roll_egg(crafter(), rerolls=5)
    assert look(egg) == (0, False, "", True) and asked == [(1000,), (1000,), (10000,)]


async def test_assign_pet_stores_what_the_repeated_roll_gave(game_db, monkeypatch):
    db, bot = game_db.bot.db, game_db.bot
    rig(monkeypatch, randint=0)
    lucky, plain, other = crafter(game_db), game_db.add_member("Ala"), crafter(game_db, "Drugi")
    asked = dice(monkeypatch, 999, 999, 30, 400)                  # the base look, then the middle seal
    assert await pets.assign_pet(game_db.scene(), lucky, cosmetic_rerolls=1) is True
    assert await cosmetics(db, 1) == (2, False) and len(asked) == 4
    assert await everything(db, 1) == (2, False, "", False)
    assert bot.owner_log == []                                    # not rare enough to tell the owner

    # A non-supporter's egg rolls nothing, whatever the boss.
    assert await pets.assign_pet(game_db.scene(), plain, cosmetic_rerolls=5) is True
    assert await everything(db, 2) == (0, False, "", False) and len(asked) == 4

    # Without the bonus the same dice give the base look: one roll, as always.
    asked = dice(monkeypatch, 999, 999, 30, 400)
    assert await pets.assign_pet(game_db.scene(), other) is True
    assert await everything(db, 3) == (0, False, "", False) and len(asked) == 2

    # A supporter who already has a pet gets no egg - and no roll, repeated or not.
    assert await pets.assign_pet(game_db.scene(), lucky, cosmetic_rerolls=5) is False
    assert len(asked) == 2 and await db.fetchval("SELECT COUNT(*) FROM PETS") == 3


async def test_a_rare_find_of_a_repeated_roll_is_reported_like_any_other(game_db, monkeypatch):
    rig(monkeypatch, randint=0)
    lucky = crafter(game_db)
    dice(monkeypatch, 999, 999, 999, 0)                           # the base look, then full Holo
    assert await pets.assign_pet(game_db.scene(), lucky, cosmetic_rerolls=2) is True
    assert await cosmetics(game_db.bot.db, 1) == (0, True)
    assert len(game_db.bot.owner_log) == 1 and "folię Holo" in game_db.bot.owner_log[0]


# -- the patch notes ------------------------------------------------------------------------------------

def test_the_patch_notes_entry_is_valid_and_gives_nothing_away(shipped):
    notes = {item.id: item for item in patchnotes.load_notes()}
    note = notes[9]
    assert (note.date, note.title) == ("2026-11-23", "Wyjątkowe spotkania z bossami")
    assert patchnotes.spoiler_in(note) is None and len(note.changes) == 2
    text = " ".join((note.title, *note.changes))
    # No chances, no numbers of repeats, no names: not of the ornaments, not of the variants.
    assert not any(char.isdigit() for char in text)
    plain = patchnotes.plain_text(text)
    for variant in shipped.variants.values():
        assert patchnotes.plain_text(variant.name) not in plain
        assert not any(word in plain for word in patchnotes.plain_text(variant.name).split())
    assert "ramk" not in plain and "foli" not in plain and "lobby" not in plain
    # It promises what the game does: now and then, the rarer the better, cosmetic, supporters only.
    # (A dated entry: what the owner sets the chance to later is not its business.)
    assert "czasem" in note.changes[0]
    assert "im rzadszy boss, tym ta szansa większa" in note.changes[1]
    assert "wyłącznie elementy kosmetyczne" in note.changes[1] and "tylko dla wspierających" in note.changes[1]
