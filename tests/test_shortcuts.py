"""Shortcut buttons (features/shortcuts.py) and the invasion's live chance (features/events.py).

Owner's request of 2026-11-24: "jeśli ktoś wydropi jajko lub zwiększy się level towarzysza, to pojawi się pod
tym dropem przycisk pokaż towarzysza. Jeśli ktoś poświęci towarzysza, to chcę, żeby pod komunikatem pojawił się
przycisk album, który od razu pokazuje album. Gdy jest inwazja na wioskę, chciałbym, żeby na bieżąco pokazywało
procent szans na uratowanie wioski, w zależności od tego, kto dołączył."
"""

from __future__ import annotations

import asyncio
import random
from fractions import Fraction
from types import SimpleNamespace

import discord
import pytest

from altermmo.core import challenge, ui
from altermmo.features import album, boss, daily, events, hub, pets, shortcuts

from test_album import PATRON, confirms, own_pet, sacrifices  # noqa: F401 - fixture
from test_events import clicks_on, high_rolls, loot, low_rolls, panel, quick  # noqa: F401 - fixtures
from test_pets import add_owner, add_pet, owner_row
from test_pets import plain_cards  # noqa: F401 - autouse: no card is composed here

EGG = {"id": boss.LOOT_EGG, "descr": "Jajko", "weight": 100}
LEVEL = {"id": boss.LOOT_PET_LEVEL, "descr": "Poziom towarzysza", "weight": 100}
MIRROR = {"id": boss.LOOT_MIRROR, "descr": "Lustro wizualiów", "weight": 100}


def table(monkeypatch, module, *items) -> None:
    """Every drop of ``module`` (boss / daily) is exactly ``items`` (each certain, no bonus)."""
    monkeypatch.setattr(module, "_loot_table", lambda rarity: [dict(item) for item in items])
    monkeypatch.setattr(random, "random", lambda: 0.999)                       # no "(Bonus)" second item


def drop_embed(game):
    return next(message for message in reversed(game.channel.sent)
                if message.embeds and "Drop - " in str(message.embeds[0].title))


def pet_key(member) -> str:
    return shortcuts.PET_ID.format(player_id=member.id)


def album_key(member) -> str:
    return shortcuts.ALBUM_ID.format(player_id=member.id)


def shortcut(message):
    """The only button under ``message`` (None: no buttons)."""
    if message.view is None:
        return None
    (item,) = message.view.children
    return item


# -- the buttons themselves ---------------------------------------------------------------------

def test_the_buttons_carry_their_player_and_survive_a_restart():
    pet = shortcuts.pet_view(123456789012345678)
    (button,) = pet.children
    assert isinstance(button, shortcuts.PetButton) and button.player_id == 123456789012345678
    assert button.custom_id == "altermmo:show:pet:123456789012345678"
    assert button.item.label == "Pokaż towarzysza" and str(button.item.emoji) == "🐾"
    assert pet.timeout is None and pet.is_persistent()                        # works after a restart
    shelf = shortcuts.album_view(SimpleNamespace(id=42))
    (button,) = shelf.children
    assert isinstance(button, shortcuts.AlbumButton) and button.custom_id == "altermmo:show:album:42"
    assert button.item.label == "Album" and str(button.item.emoji) == "📖"
    # Neither is a flow button: the stale-click handler leaves them to discord.py's dynamic items.
    assert not button.custom_id.startswith(ui.FLOW_PREFIX) and button.custom_id.startswith(ui.PREFIX)
    for cls, other in ((shortcuts.PetButton, "altermmo:show:album:1"), (shortcuts.AlbumButton, "altermmo:show:pet:1")):
        assert cls.__discord_ui_compiled_template__.fullmatch(other) is None


async def test_a_button_is_rebuilt_from_its_custom_id():
    for cls, custom_id in ((shortcuts.PetButton, "altermmo:show:pet:77"), (shortcuts.AlbumButton, "altermmo:show:album:77")):
        match = cls.__discord_ui_compiled_template__.fullmatch(custom_id)
        rebuilt = await cls.from_custom_id(None, None, match)
        assert isinstance(rebuilt, cls) and rebuilt.player_id == 77 and rebuilt.custom_id == custom_id


async def test_the_buttons_are_registered_at_start_up(game):
    await game.bot.load_extension("altermmo.cogs.hub")
    registered = set(game.bot._connection._view_store._dynamic_items.values())
    assert {shortcuts.PetButton, shortcuts.AlbumButton} <= registered


def test_registering_never_raises():
    class Broken:
        def add_dynamic_items(self, *items):
            raise RuntimeError("test: no")

    assert shortcuts.register(Broken()) is False


# -- "Pokaż towarzysza" under a drop ----------------------------------------------------------

@pytest.mark.parametrize("module", [boss, daily])
async def test_a_drop_with_a_new_egg_has_the_button_and_it_shows_the_egg(game_db, monkeypatch, module):
    game = game_db
    table(monkeypatch, module, EGG, MIRROR)
    ala = game.add_member("Ala")
    await module.rand_loot(game.scene(ala), 1, ala, 0)
    message = drop_embed(game)
    button = shortcut(message)
    assert isinstance(button, shortcuts.PetButton) and button.player_id == ala.id

    before = len(game.channel.sent)
    click = await game.click(message, ala, pet_key(ala))
    assert click.response.deferred and click.replies == []                   # acknowledged, then posted
    (shown,) = game.channel.sent[before:]
    assert f"Oto Twój towarzysz, <@{ala.id}>" in shown.text and shown.embeds[0].title.startswith("Jajko")
    assert isinstance(shown.view, pets.PetPanel)                             # as the typed !towarzysz shows it


@pytest.mark.parametrize("module", [boss, daily])
async def test_a_drop_with_a_level_has_the_button(game_db, monkeypatch, module):
    game, db = game_db, game_db.bot.db
    table(monkeypatch, module, LEVEL)
    ala = game.add_member("Ala")
    await add_pet(db, 7, pet_name="Burek", pet_lvl=1, type="Dog")
    await add_owner(db, ala.id, 7)
    await module.rand_loot(game.scene(ala), 1, ala, 0)
    message = drop_embed(game)
    assert "Poziom towarzysza" in message.text and isinstance(shortcut(message), shortcuts.PetButton)
    before = len(game.channel.sent)
    await game.click(message, ala, pet_key(ala))
    shown = next(m for m in game.channel.sent[before:] if m.embeds)
    assert "Burek" in shown.text and "Poziom: 2" in shown.text


@pytest.mark.parametrize("module", [boss, daily])
async def test_a_drop_without_an_egg_or_a_level_has_no_button(game_db, monkeypatch, module):
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala")
    # A level that could not be granted (no pet at all) gives no button.
    table(monkeypatch, module, LEVEL)
    await module.rand_loot(game.scene(ala), 1, ala, 0)
    assert "Poziom towarzysza" not in drop_embed(game).text and shortcut(drop_embed(game)) is None
    # A drop of other things gives none.
    table(monkeypatch, module, MIRROR)
    await module.rand_loot(game.scene(ala), 1, ala, 0)
    assert "Lustro" in drop_embed(game).text and shortcut(drop_embed(game)) is None
    # A pet at its last level gets no level, and an egg finds no room beside it (no pouch): no button.
    await add_pet(db, 7, pet_lvl=pets.MAX_LEVEL)
    await db.execute("UPDATE PETOWNER SET PET_ID = 7 WHERE PLAYER_ID = $1", ala.id)
    table(monkeypatch, module, LEVEL)
    await module.rand_loot(game.scene(ala), 1, ala, 0)
    assert "Poziom towarzysza" not in drop_embed(game).text and shortcut(drop_embed(game)) is None
    table(monkeypatch, module, EGG)
    await module.rand_loot(game.scene(ala), 1, ala, 0)
    assert "Jajko" not in drop_embed(game).text and shortcut(drop_embed(game)) is None
    assert (await owner_row(db, ala.id))["pet_id"] == 7


async def test_an_egg_into_the_pouch_gives_no_button(game_db, monkeypatch):
    """The egg in the pouch is not the pet at his side: "Pokaż towarzysza" would show the old one."""
    game, db = game_db, game_db.bot.db
    table(monkeypatch, daily, EGG)
    ala = game.add_member("Ala")
    await add_pet(db, 7, pet_lvl=1)
    await add_owner(db, ala.id, 7)
    await db.execute("UPDATE PETOWNER SET EGG_POUCH = TRUE WHERE PLAYER_ID = $1", ala.id)
    await daily.rand_loot(game.scene(ala), 1, ala, 0)
    assert (await owner_row(db, ala.id))["pouch_pet_id"] > 0
    assert shortcut(drop_embed(game)) is None


async def test_the_button_is_only_for_the_player_of_the_drop(game_db, monkeypatch):
    game = game_db
    table(monkeypatch, daily, EGG)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await daily.rand_loot(game.scene(ala), 1, ala, 0)
    message = drop_embed(game)
    before = len(game.channel.sent)
    click = await game.click(message, ola, pet_key(ala))
    assert click.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    assert not any(m.embeds for m in game.channel.sent[before:])            # nothing shown


async def test_the_button_waits_for_the_end_of_a_fight(game_db, monkeypatch):
    """The same gate as the panel's "Towarzysz": nothing may make a fight stutter."""
    game = game_db
    table(monkeypatch, daily, EGG)
    ala = game.add_member("Ala")
    await daily.rand_loot(game.scene(ala), 1, ala, 0)
    message = drop_embed(game)
    calls = []

    async def gate(interaction, *, stable=False):
        calls.append(interaction.user.id)
        return False

    monkeypatch.setattr(hub, "display_gate", gate)
    before = len(game.channel.sent)
    await game.click(message, ala, pet_key(ala))
    assert calls == [ala.id] and game.channel.sent[before:] == []


async def test_a_failing_display_is_answered_and_reported(game_db, monkeypatch):
    game = game_db
    table(monkeypatch, daily, EGG)
    ala = game.add_member("Ala")
    await daily.rand_loot(game.scene(ala), 1, ala, 0)
    message = drop_embed(game)

    async def broken(scene):
        raise RuntimeError("test: broken")

    monkeypatch.setattr(hub, "show_pet", broken)
    click = await game.click(message, ala, pet_key(ala))
    assert ui.MSG_FAILED in click.answers
    assert any("Błąd przycisku `pet`" in text for text in game.bot.owner_log)


async def test_the_drop_is_what_it_was(game_db, monkeypatch):
    """The button changes nothing of the loot: same lines, same dice afterwards."""
    game = game_db
    monkeypatch.setattr(daily, "_loot_table", lambda rarity: [dict(EGG), dict(MIRROR), dict(LEVEL)])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    results = []
    for member, view in ((ala, True), (ola, False)):
        if not view:
            monkeypatch.setattr(shortcuts, "pet_view", lambda player: None)
        random.seed(20261124)
        lines = await daily.rand_loot(game.scene(member), 1, member, 0)
        results.append((lines, random.getstate()))
    assert results[0] == results[1]


# -- "Album" under a sacrifice ----------------------------------------------------------------

async def test_a_supporters_sacrifice_has_the_album_button(game_db, sacrifices):  # noqa: F811
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await own_pet(db, ala.id, 5, pet_name="Mruczek", pet_lvl=2)
    confirms(game, ala)
    assert await pets.discard_pet(game.scene(ala)) is True
    saved = next(m for m in game.channel.sent if m.content == album.MSG_DISCARD_SAVED.format(player_id=ala.id))
    button = shortcut(saved)
    assert isinstance(button, shortcuts.AlbumButton) and button.player_id == ala.id

    before = len(game.channel.sent)
    click = await game.click(saved, ala, album_key(ala))
    assert click.response.deferred and click.replies == []
    shown = next(m for m in game.channel.sent[before:] if m.embeds)
    assert shown.embeds[0].title == album.MSG_TITLE and "jest **1** towarzysz" in shown.text   # with Mruczek

    ola = game.add_member("Ola")
    click = await game.click(saved, ola, album_key(ala))
    assert click.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]


async def test_everybody_elses_sacrifice_has_no_album_button(game_db, sacrifices):  # noqa: F811
    game, db = game_db, game_db.bot.db
    ola = game.add_member("Ola")
    await own_pet(db, ola.id, 5)
    confirms(game, ola)
    assert await pets.discard_pet(game.scene(ola)) is True
    assert not any(isinstance(item, shortcuts.AlbumButton) for m in game.channel.sent if m.view is not None
                   for item in m.view.children)


async def test_a_sacrifice_during_a_fight_says_nothing_and_shows_no_button(game_db, sacrifices, monkeypatch):  # noqa: F811
    game, db = game_db, game_db.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await own_pet(db, ala.id, 5)
    monkeypatch.setattr(challenge, "fight_on", lambda channel: True)
    await album.saved(game.scene(ala), ala)
    assert not game.channel.ever_sent(album.MSG_DISCARD_SAVED.format(player_id=ala.id))


# -- the invasion's chance --------------------------------------------------------------------

@pytest.mark.parametrize("defenders, percent", [(0, 0), (1, 21), (2, 31), (3, 41), (4, 50), (5, 60),
                                                (6, 70), (7, 80), (8, 90), (9, 100), (10, 100), (40, 100)])
def test_the_chance_is_the_chance_of_the_roll(defenders, percent):
    # The roll: randint(0, 100) - 101 results - at most the threshold, and somebody must come.
    wins = sum(1 for roll in range(101) if defenders and roll <= events.invasion_threshold(defenders))
    assert events.invasion_chance(defenders) == Fraction(wins, 101)
    assert events.invasion_percent(defenders) == percent


def test_the_percent_is_never_rounded_up_to_a_certainty():
    assert all(events.invasion_percent(n) < 100 for n in range(0, 9))
    assert events.invasion_percent(9) == 100 and events.invasion_chance(9) == 1


def test_the_chance_line():
    assert events.invasion_status(0) == "🛡️ Szansa na uratowanie wioski: **0%** - bez obrońców wioska upadnie!"
    assert events.invasion_status(1) == ("🛡️ Szansa na uratowanie wioski: **21%** "
                                         "(każdy kolejny obrońca ją zwiększa)")
    assert events.invasion_status(9) == "🛡️ Szansa na uratowanie wioski: **100%**"


async def test_the_panel_shows_the_chance_as_defenders_join(game, loot, quick, monkeypatch):  # noqa: F811
    low_rolls(monkeypatch)
    monkeypatch.setattr(events, "ROSTER_EDIT_GAP", 0.0)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    posted: list[str] = []
    game.channel.send_hooks.append(
        lambda message: posted.append(message.embeds[0].description) if message.embeds else None)
    clicks_on(game, "Inwazja na wioskę!", (ala, "join"), (ola, "join"))
    await events.spawn_invasion(game.scene())
    first = panel(game, "Inwazja na wioskę!")
    assert events.invasion_status(0) in posted[0]                             # posted with nobody yet
    painted = [edit["embed"].description for edit in first.edits if edit.get("embed") is not None]
    lines = [line for text in painted for line in text.splitlines() if line.startswith("🛡️")]
    assert events.invasion_status(2) in lines                                 # repainted as they joined
    assert all(line in {events.invasion_status(n) for n in range(3)} for line in lines)
    final = painted[-1]
    assert events.invasion_status(2) in final and events.MSG_SIGN_UP_CLOSED in final
    assert loot == [(1, ala, 20), (1, ola, 20)]


async def test_other_sign_ups_have_no_chance_line(game, quick, monkeypatch):
    monkeypatch.setattr(events.daily, "rand_loot", lambda *args, **kwargs: asyncio.sleep(0))
    sign_up = events._SignUp(game.scene(), discord.Embed(title="Coś", description="Opis"), emoji="⚔️")
    assert "🛡️" not in sign_up.render().description
