"""The album on the channel: ``!album``, the hub's "Album" button and the album's own panel.

Owner's rules tested here: supporters only (the others are told how to become one, publicly); every
answer is an ordinary channel message; display buttons wait while an event / hunt / tower run holds
the channel and say nothing between the prompts of a typed fight; the overview never names a species
the player has not found nor explains which ornaments exist; a removal is confirmed, permanent and
happens once (two panels, a double click, a page turned during the question).

Owner's rule of 2026-11-23: an egg in the album never tells its species ("Przy przeglądaniu albumu gdy
wyświetla się jajko to nie powinien być znany jeszcze gatunek") - the overview, the pages, the species
menu and the texts of a removal; the eggs are one group after the hatched pets.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import types

import discord
import pytest
import pytest_asyncio

from altermmo.cogs import help as help_cog
from altermmo.core import challenge, guards, timing, ui
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.scene import Scene
from altermmo.features import album, hub, patrons, petcards, pets

from test_album import album_rows, own_pet, put
from test_pets import ASK, confirms, run
from test_pets import plain_cards  # noqa: F401 - autouse: pets are sent as their plain pictures
from test_ui_pets import start_click

PATRON = patrons.PATRON_ROLES[0]


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a !potwierdzam slot was never released"


@pytest_asyncio.fixture
async def cogs(game_db):
    for extension in ("altermmo.cogs.album", "altermmo.cogs.hub"):
        await game_db.bot.load_extension(extension)
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    return game_db


class DatabaseDown:
    async def fetch(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    fetchrow = fetchval = execute = fetch


def supporters_only(member=None) -> str:
    links = " • ".join(f"[{label}](<{url}>)" for label, url in patrons.SUPPORT_LINKS)
    text = album.MSG_SUPPORTERS_ONLY.format(groups=patrons.SUPPORTERS_LONG, links=links)
    return text if member is None else f"<@{member.id}> {text}"


async def three(game, name: str = "Ala"):
    """A supporter whose album holds a bear (level 1), a cat of level 2 and a cat egg - in that order
    (the hatched pets by species, then the eggs)."""
    player = game.add_member(name, roles=[PATRON])
    db = game.bot.db
    await put(db, player.id, "Cat", 0)
    await put(db, player.id, "Cat", 2, pet_name="Mruczek")
    await put(db, player.id, "Bear", 1)
    return player


async def show(game, player, channel=None):
    """``!album`` of ``player`` (the function behind it); returns the message it posted."""
    channel = channel or game.channel
    before = len(channel.sent)
    assert await album.show_album(Scene(bot=game.bot, channel=channel, author=player)) is True
    (posted,) = channel.sent[before:]
    return posted


def page(message) -> str:
    return message.embeds[0].description or ""


def files(message) -> list[str]:
    """The pictures the last edit attached (None: the edit left the attachment alone)."""
    edit = message.edits[-1]
    return None if "attachments" not in edit else [item.filename for item in edit["attachments"]]


async def until(condition, timeout: float = 5.0) -> bool:
    """Wait until ``condition()`` is true, at most ``timeout`` seconds; returns whether it became true.

    For a state reached only after a real database round trip: a fixed number of
    ``asyncio.sleep(0)`` turns is sometimes too few, and the test would fail for nothing.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


# -- !album -----------------------------------------------------------------------------------

async def test_album_is_for_supporters_only(cogs):
    game = cogs
    ola = game.add_member("Ola")
    await put(game.bot.db, ola.id, "Cat", 0)                                  # kept while she supported
    await run(game, ola, "!album")
    answer = game.channel.messages[-1]
    assert answer.content == supporters_only(ola) and answer.embeds == [] and answer.view is None
    assert answer.content.startswith(f"<@{ola.id}> Album towarzyszy jest dla wspierających (")
    assert all(url in answer.content for _, url in patrons.SUPPORT_LINKS)
    assert " z 68" not in answer.content and "Kot" not in answer.content
    assert len(await album_rows(game.bot.db, ola.id)) == 1                    # nothing is deleted
    await run(game, ola, "!kolekcja")                                         # the alias
    assert game.channel.messages[-1].content == supporters_only(ola)


async def test_an_empty_album(cogs):
    game = cogs
    ala = game.add_member("Ala", roles=[patrons.CRAFTER_ROLE])
    await run(game, ala, "!album")
    answer = game.channel.messages[-1]
    (embed,) = answer.embeds
    assert embed.title == album.MSG_TITLE and answer.view is None
    assert embed.description == album.MSG_EMPTY.format(player_id=ala.id, total=68)
    assert "**!porzucam**" in embed.description and "Kolekcja: **0 z 68**" in embed.description


async def test_the_overview_names_only_what_the_player_has(cogs):
    game = cogs
    ala = await three(game)
    await put(game.bot.db, ala.id, "Void", 0, seal=3, holo=True)
    await put(game.bot.db, ala.id, "Cat", 0, shiny=True, seal=3)            # a second cat egg
    await run(game, ala, "!album")
    message = game.channel.messages[-1]
    text = page(message)
    assert isinstance(message.view, album.AlbumPanel) and message.view.owner_id == ala.id
    assert text.startswith(f"<@{ala.id}>, w Twoim albumie jest **5** towarzyszy.\nKolekcja: **3 z 68**")
    assert "**Niedźwiedź** (1 z 3): ✅ Poziom 1 · ▫️ Poziom 2 · ▫️ Poziom 3" in text
    assert "**Kot** (1 z 3): ▫️ Poziom 1 · ✅ Poziom 2 · ▫️ Poziom 3" in text
    assert text.index("Niedźwiedź") < text.index("**Kot**") < text.index("🥚 **Jajka**: **1** z 17")
    assert "❔ Nieodkryte gatunki: **15**" in text
    assert "✨ **Poza kolekcją:**\n🥚 Jajko ×1" in text                       # the mythic egg: not attributed
    # Never a species he has not found (the void creature is an egg: not found), never a word about the ornaments.
    found = {"Niedźwiedź", "Kot"}
    for species, name in pets.POLISH_TYPES.items():
        if name not in found:
            assert name not in message.text, species
    for secret in ("Pieczęć", "pieczęć", "Holo", "Ramka", "ramk", "Shiny", "Arcyłowc", "Srebrne"):
        assert secret not in message.text, secret
    assert message.view.species.placeholder == "Przejdź do…"                # (not "do gatunku": "Jajka" is none)
    assert [option.label for option in message.view.species.options] == ["Niedźwiedź", "Kot", "Jajka"]
    assert [option.description for option in message.view.species.options] == ["1 wpis", "1 wpis", "3 wpisy"]
    assert [option.value for option in message.view.species.options] == ["Bear", "Cat", album.EGGS_OPTION]
    assert message.button_labels == ["Następny"]                              # on the overview: forward only


async def test_a_full_collection_says_so(cogs):
    from test_album import fill

    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await fill(game.bot.db, ala.id)
    text = page(await show(game, ala))
    assert "Kolekcja: **68 z 68**" in text and album.MSG_COMPLETE in text and "Nieodkryte" not in text
    assert len(text) <= 4096


def test_counting_words():
    assert [album._plural_pets(n) for n in (1, 2, 4, 5, 12, 22, 25, 104)] == [
        "jest **1** towarzysz", "są **2** towarzysze", "są **4** towarzysze", "jest **5** towarzyszy",
        "jest **12** towarzyszy", "są **22** towarzysze", "jest **25** towarzyszy", "są **104** towarzysze"]
    assert [album._entries_word(n) for n in (1, 3, 5, 13)] == ["1 wpis", "3 wpisy", "5 wpisów", "13 wpisów"]


# -- paging ----------------------------------------------------------------------------------------

async def test_paging_through_the_album(cogs):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)

    click = await game.click(panel, ala, "Następny")
    assert click.response.deferred and click.replies == []                   # acknowledged, edited in place
    assert page(panel).endswith("Wpis **1 z 3**") and panel.embeds[0].title == "📖 Niedźwiedź"
    assert "Etap: **Poziom 1**" in page(panel)
    assert files(panel) == ["0.png"] and panel.embeds[0].image.url == "attachment://0.png"
    assert panel.button_labels == ["Poprzedni", "Następny", "Przegląd", "Usuń z albumu"]

    await game.click(panel, ala, "Następny")
    assert page(panel).endswith("Wpis **2 z 3**") and panel.embeds[0].title == "📖 Kot Mruczek"
    assert files(panel) == ["10.png"]
    await game.click(panel, ala, "Następny")                                  # the eggs come last
    assert page(panel).endswith("Wpis **3 z 3**") and "Etap: **Jajko**" in page(panel)
    assert panel.embeds[0].title == "📖 Jajko" and "Gatunek" not in page(panel)
    assert files(panel) == ["0.png"]
    assert "Następny" not in panel.button_labels                              # the last page
    await game.click(panel, ala, "Następny")                                  # (a click seen before the edit)
    assert page(panel).endswith("Wpis **3 z 3**")

    await game.click(panel, ala, "Poprzedni")
    assert page(panel).endswith("Wpis **2 z 3**")
    await game.click(panel, ala, "Przegląd")
    assert page(panel).startswith(f"<@{ala.id}>, w Twoim albumie są **3** towarzysze.")
    assert files(panel) == [] and panel.embeds[0].image.url is None          # the card is taken off
    assert panel.button_labels == ["Następny"]
    await game.click(panel, ala, "Następny")
    await game.click(panel, ala, "Poprzedni")                                 # from the first page: the overview
    assert page(panel).startswith(f"<@{ala.id}>, w Twoim albumie")
    assert all(message is panel for message in game.channel.sent[-1:])        # nothing new was posted


async def test_two_pictures_of_one_name_are_both_attached(cogs):
    """The bear's and the dog's pictures are both "0.png": the second one is attached all the same."""
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await put(game.bot.db, ala.id, "Bear", 1)
    await put(game.bot.db, ala.id, "Dog", 1)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    assert panel.embeds[0].title == "📖 Niedźwiedź" and files(panel) == ["0.png"]
    await game.click(panel, ala, "Następny")
    assert panel.embeds[0].title == "📖 Pies" and files(panel) == ["0.png"]   # another picture of that name


async def test_jumping_to_a_species(cogs):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    click = await game.click(panel, ala, "album:species", values=["Cat"])
    assert click.response.deferred and click.replies == []
    assert page(panel).endswith("Wpis **2 z 3**") and "Gatunek: **Kot**" in page(panel)
    await game.click(panel, ala, "album:species", values=["Dragon"])         # nothing of it (any more)
    assert page(panel).startswith(f"<@{ala.id}>, w Twoim albumie")
    await game.click(panel, ala, "album:species", values=[album.EGGS_OPTION])   # "Jajka": the first egg
    assert page(panel).endswith("Wpis **3 z 3**") and panel.embeds[0].title == "📖 Jajko"


# -- an egg never tells its species ---------------------------------------------------------------------

#: Every name a species has: as the players read it and as the database / the pictures' folders call it.
SPECIES_NAMES = sorted({*pets.POLISH_TYPES.values(), *(kind.value for kind in pets.PetType)})


def names_a_species(text: str) -> list[str]:
    """The species named in ``text`` (in any case)."""
    return [name for name in SPECIES_NAMES if name.lower() in str(text).lower()]


def everything_on(message) -> str:
    """Every text a player can read on a message: its content, its embeds (title, description, footer,
    fields, the picture's name), its attachments and what its list offers."""
    parts = [message.content or ""]
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or "", embed.footer.text or "", embed.image.url or ""]
        parts += [f"{field.name} {field.value}" for field in embed.fields]
    for edit in message.edits:
        parts += [item.filename for item in edit.get("attachments") or []]
    view = message.view
    if view is not None:
        for item in view.children:
            parts.append(str(getattr(item, "label", "") or ""))
            parts.append(str(getattr(item, "placeholder", "") or ""))
            for option in getattr(item, "options", None) or []:
                parts += [option.label, option.description or ""]
    return "\n".join(parts)


async def eggs_only(game):
    """A supporter whose album holds nothing but eggs: ordinary ones, premium ones, a mythic one, two
    of one species (the plain one does not count), one with a name of its own."""
    ala = game.add_member("Ala", roles=[PATRON])
    db = game.bot.db
    for species, columns in (("Dragon", {}), ("Cat", {}), ("Void", {"seal": 3, "holo": True}),
                             ("Cat", {"shiny": True, "frame": "S1-A"}), ("Unicorn", {"pet_name": "Iskra"}),
                             ("Rabbit", {"variant": "7", "ultra_shiny": True})):
        await put(db, ala.id, species, 0, **columns)
    return ala


async def test_an_album_of_eggs_only_names_no_species_anywhere(cogs, monkeypatch):
    """A scan of every text the panel can produce for an album of eggs only."""
    game = cogs
    db = game.bot.db
    ala = await eggs_only(game)
    seen = []
    real = game.channel.send

    async def send(content=None, **kwargs):
        message = await real(content, **kwargs)
        seen.append(message)
        return message

    monkeypatch.setattr(game.channel, "send", send)
    await run(game, ala, "!album")
    panel = game.channel.messages[-1]
    text = page(panel)
    assert text.startswith(f"<@{ala.id}>, w Twoim albumie jest **6** towarzyszy.\nKolekcja: **4 z 68**\n")
    assert "🥚 **Jajka**: **4** z 17\n\n❔ Nieodkryte gatunki: **17**" in text       # counted, not attributed
    assert "✨ **Poza kolekcją:**\n🥚 Jajko ×1" in text and " z 3):" not in text    # no species line at all
    assert [option.label for option in panel.view.species.options] == ["Jajka"]
    assert [option.description for option in panel.view.species.options] == ["6 wpisów"]
    assert names_a_species(everything_on(panel)) == []

    rows = await album.entries(db, ala.id)
    assert [row["entry_id"] for row in rows] == sorted(row["entry_id"] for row in rows)   # as they were kept
    for position, row in enumerate(rows, start=1):
        await game.click(panel, ala, "Następny")
        assert page(panel).endswith(f"Wpis **{position} z 6**") and "Etap: **Jajko**" in page(panel)
        assert panel.embeds[0].title == ("📖 Jajko Iskra" if row["pet_name"] == "Iskra" else "📖 Jajko")
        assert "Gatunek" not in page(panel) and "Wartość karty: **" in page(panel)
        assert names_a_species(everything_on(panel)) == [], (position, everything_on(panel))
        assert names_a_species(album.describe(row)) == [] and names_a_species(album._remove_question(ala.id, row)) == []
    assert albumrank_not_counted() in "\n".join(embed["embed"].description for embed in panel.edits
                                                if embed.get("embed") is not None)   # the plain cat egg
    # The removal of an egg: the question, the answer, the page that follows.
    await game.click(panel, ala, "album:species", values=[album.EGGS_OPTION])
    game.auto_click(ala, "yes")
    await game.click(panel, ala, "Usuń z albumu")
    question = next(m for m in seen if "usunąć z albumu" in (m.content or ""))
    assert question.content.startswith(f"<@{ala.id}>, czy na pewno chcesz usunąć z albumu **Jajko** (w albumie od ")
    assert game.channel.contains(album.MSG_REMOVED.format(what="Jajko", player_id=ala.id))
    await game.click(panel, ala, "Przegląd")
    assert "🥚 **Jajka**: **3** z 17" in page(panel)                          # the dragon egg is gone
    for message in seen:
        assert names_a_species(everything_on(message)) == [], everything_on(message)
    # ... and the species menu of a species nobody can see finds nothing.
    await game.click(panel, ala, "album:species", values=["Cat"])
    assert page(panel).startswith(f"<@{ala.id}>, w Twoim albumie")


def albumrank_not_counted() -> str:
    from altermmo.features import albumrank

    assert names_a_species(albumrank.MSG_NOT_COUNTED) == [] and names_a_species(albumrank.MSG_CARD_VALUE) == []
    return albumrank.MSG_NOT_COUNTED


async def test_eggs_and_hatched_pets_of_one_species(cogs):
    """The species is named by its hatched pets only; its eggs stand with the other eggs."""
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    ids = {"dog egg": await put(db, ala.id, "Dog", 0), "cat 3": await put(db, ala.id, "Cat", 3),
           "cat egg": await put(db, ala.id, "Cat", 0, pet_name="Mruczek"), "cat 1": await put(db, ala.id, "Cat", 1),
           "void 2": await put(db, ala.id, "Void", 2), "void egg": await put(db, ala.id, "Void", 0),
           "cat egg 2": await put(db, ala.id, "Cat", 0)}
    # The hatched pets by species and stage; then the eggs as they were kept - not next to their species.
    assert [row["entry_id"] for row in await album.entries(db, ala.id)] == [
        ids[key] for key in ("cat 1", "cat 3", "void 2", "dog egg", "cat egg", "void egg", "cat egg 2")]
    panel = await show(game, ala)
    text = page(panel)
    assert text.startswith(f"<@{ala.id}>, w Twoim albumie jest **7** towarzyszy.\nKolekcja: **4 z 68**\n")
    assert "**Kot** (2 z 3): ✅ Poziom 1 · ▫️ Poziom 2 · ✅ Poziom 3\n🥚 **Jajka**: **2** z 17" in text
    assert "❔ Nieodkryte gatunki: **16**" in text and "Pies" not in text       # the dog is only an egg
    assert "✨ **Poza kolekcją:**\nIstota pustki: Poziom 2 ×1\n🥚 Jajko ×1" in text   # hatched: named
    assert [(option.label, option.description) for option in panel.view.species.options] == [
        ("Kot", "2 wpisy"), ("Istota pustki", "1 wpis"), ("Jajka", "4 wpisy")]
    await game.click(panel, ala, "album:species", values=["Cat"])               # the first HATCHED cat
    assert page(panel).endswith("Wpis **1 z 7**") and "Etap: **Poziom 1**" in page(panel)
    await game.click(panel, ala, "album:species", values=["Dog"])               # only an egg: nothing to jump to
    assert page(panel).startswith(f"<@{ala.id}>, w Twoim albumie")
    await game.click(panel, ala, "album:species", values=[album.EGGS_OPTION])
    assert page(panel).endswith("Wpis **4 z 7**") and panel.embeds[0].title == "📖 Jajko"
    await game.click(panel, ala, "Następny")
    assert panel.embeds[0].title == "📖 Jajko Mruczek" and "Imię: **Mruczek**" in page(panel)
    assert names_a_species(panel.embeds[0].title + page(panel)) == [] and "Gatunek" not in page(panel)
    rows = {row["entry_id"]: row for row in await album_rows(db, ala.id)}
    assert album.describe(rows[ids["cat egg"]]) == "Jajko Mruczek" and album.describe(rows[ids["cat egg 2"]]) == "Jajko"
    assert album.describe(rows[ids["cat 3"]]) == "Kot (Poziom 3)" and album.describe(rows[ids["void egg"]]) == "Jajko"
    assert album.is_egg(rows[ids["void egg"]]) and not album.is_egg(rows[ids["void 2"]])
    assert album.is_egg({"type": "Cat", "pet_lvl": None})                       # NULL is the egg, as everywhere


async def test_the_counts_of_a_full_album_stay_right(cogs):
    """Eggs still are slots: 68 of 68, every species 3 of 3, 17 eggs of 17."""
    from test_album import fill

    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await fill(game.bot.db, ala.id, leave_out={("Cat", 0), ("Dog", 2)})
    text = page(await show(game, ala))
    assert "Kolekcja: **66 z 68**" in text and "🥚 **Jajka**: **16** z 17" in text
    assert "**Kot** (3 z 3): ✅ Poziom 1 · ✅ Poziom 2 · ✅ Poziom 3" in text
    assert "**Pies** (2 z 3): ✅ Poziom 1 · ▫️ Poziom 2 · ✅ Poziom 3" in text
    assert "❔ Nieodkryte gatunki: **0**" in text and text.count("z 3):") == 17
    lines = [line for line in text.split("\n") if "z 3):" in line]
    assert sum(line.count("✅") for line in lines) + 16 == 66                  # the lines add up to the count
    assert len(text) <= 4096


def test_the_species_list_fits_discords_limit():
    """25 entries at most: with eggs there are 24 species and "Jajka"."""
    rows = [{"type": f"Species{n:02}", "pet_lvl": 1, "entry_id": n} for n in range(40)]
    assert len(album.AlbumPanel._options(rows)) == album.MAX_OPTIONS == 25
    with_eggs = album.AlbumPanel._options([*rows, {"type": "Cat", "pet_lvl": 0, "entry_id": 99}])
    assert len(with_eggs) == 25 and with_eggs[-1].label == "Jajka" and with_eggs[-1].value == album.EGGS_OPTION
    assert album.EGGS_OPTION not in {kind.value for kind in pets.PetType}
    assert [option.label for option in album.AlbumPanel._options([])] == [album.MSG_TITLE]


async def test_an_entry_shows_what_it_is_and_what_it_has(cogs, monkeypatch):
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await put(game.bot.db, ala.id, "Dragon", 2, pet_name="Smok *Wawelski*", variant="1", shiny=True,
              seal=3, holo=True)
    looks = []

    async def card(source, look, cache_dir, *, max_bytes=None):
        looks.append(look)
        return source

    monkeypatch.setattr(pets.petcards, "card", card)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    text = page(panel)
    assert panel.embeds[0].title == "📖 Smok Smok \\*Wawelski\\*"                # plain text, as the name was typed
    for line in ("Gatunek: **Smok**", "Imię: **Smok \\*Wawelski\\***", "Etap: **Poziom 2**", "Klasa: **Shiny**",
                 "Ramka: **Srebrne okucia**", "Pieczęć: **Arcyłowcy**", "Folia: **Holo pełne**"):
        assert line in text, line
    assert re.search(r"W albumie od: <t:\d+:f>", text)
    assert panel.embeds[0].footer.text == album.MSG_ENTRY_FOOTER
    # The card has everything the pet had (the viewer is a supporter).
    assert looks == [petcards.Look(frame="S0", seal=3, foil=petcards.FOIL_FULL)]
    assert files(panel) == ["11.png"]


async def test_a_pets_name_is_shown_as_plain_text_everywhere(cogs):
    """Markdown in a name renders as typed (escaped), a line break / a run of spaces is one space -
    in the title, the description and the removal question and answer; within Discord's limits."""
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    await put(db, ala.id, "Cat", 2, pet_name="Pan\n**Kot**   _x_ ||y|| `z`")
    await put(db, ala.id, "Cat", 3, pet_name="*" * 255)                     # the longest name !nazwij takes
    await put(db, ala.id, "Dog", 1, pet_name=" \n ")                         # nothing but blanks: no name
    shown = "Pan \\*\\*Kot\\*\\* \\_x\\_ \\|\\|y\\|\\| \\`z\\`"
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    assert panel.embeds[0].title == f"📖 Kot {shown}"
    assert f"Imię: **{shown}**" in page(panel) and "\n**Kot" not in page(panel)
    rows = {album.slot_of(row): row for row in await album_rows(db, ala.id)}
    assert album.describe(rows[("Cat", 2)]) == f"Kot {shown} (Poziom 2)"
    assert "\n" not in album._remove_question(ala.id, rows[("Cat", 2)])
    backslash = {"type": "Cat", "pet_lvl": 1, "pet_name": "Mruczek\\"}         # would escape the closing **
    assert album.describe(backslash) == "Kot Mruczek\\\\ (Poziom 1)"
    await game.click(panel, ala, "Następny")
    assert len(panel.embeds[0].title) <= 256 and panel.embeds[0].title.endswith("\\*")
    assert panel.embeds[0].title == "📖 Kot " + "\\*" * 100                 # cut to 100, then escaped
    await game.click(panel, ala, "Następny")
    assert panel.embeds[0].title == "📖 Pies" and "Imię" not in page(panel)
    assert album.describe(rows[("Dog", 1)]) == "Pies (Poziom 1)"
    game.auto_click(ala, "yes")
    await game.click(panel, ala, "Poprzedni")
    await game.click(panel, ala, "Poprzedni")
    await game.click(panel, ala, "Usuń z albumu")
    assert game.channel.contains(album.MSG_REMOVED.format(what=f"Kot {shown} (Poziom 2)", player_id=ala.id))


async def test_an_ordinary_entry_names_no_ornament(cogs):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    text = page(panel)
    assert "Klasa: **Zwykły**" in text and "Imię: **Towarzysz**" in text
    assert "Ramka" not in text and "Pieczęć" not in text and "Folia" not in text


async def test_an_entry_without_a_picture_is_shown_without_one(cogs, monkeypatch):
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await put(game.bot.db, ala.id, "Cat", 1, variant="99")                   # no such picture
    await put(game.bot.db, ala.id, "Dog", 1)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    assert page(panel).endswith("Wpis **1 z 2**") and panel.embeds[0].image.url is None
    assert files(panel) is None                                               # (the overview had no picture either)

    async def broken(*args, **kwargs):
        raise RuntimeError("test: the card cannot be drawn")

    monkeypatch.setattr(pets, "_pet_card", broken)
    click = await game.click(panel, ala, "Następny")
    assert click.replies == [] and page(panel).endswith("Wpis **2 z 2**")
    assert panel.embeds[0].image.url is None


async def test_a_slow_card_is_drawn_after_the_click_was_acknowledged(cogs, monkeypatch):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    started, go = asyncio.Event(), asyncio.Event()

    async def slow(source, look, cache_dir, *, max_bytes=None):
        started.set()
        await go.wait()                                                       # (a worker thread, in the bot)
        return source

    monkeypatch.setattr(pets.petcards, "card", slow)
    interaction, task = start_click(game, panel, ala, "album:next")
    await asyncio.wait_for(started.wait(), 2)
    assert interaction.response.deferred                                     # Discord is answered first
    assert not page(panel).endswith("Wpis **1 z 3**")
    go.set()
    await asyncio.wait_for(task, 2)
    assert page(panel).endswith("Wpis **1 z 3**")


# -- who may click, and when ------------------------------------------------------------------------

async def test_only_the_owner_can_use_the_panel(cogs):
    game = cogs
    ala = await three(game)
    ola = game.add_member("Ola", roles=[PATRON])
    panel = await show(game, ala)
    edits = len(panel.edits)
    refused = await game.click(panel, ola, "Następny")
    assert refused.public == [f"<@{ola.id}> " + ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    assert refused.replies[0].message in game.channel.messages and not refused.replies[0].ephemeral
    assert len(panel.edits) == edits
    await game.click(panel, ala, "Następny")
    stranger = await game.click(panel, ola, "Usuń z albumu")
    assert stranger.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    assert len(await album_rows(game.bot.db, ala.id)) == 3


async def test_the_panel_waits_while_the_channel_is_busy(cogs, monkeypatch):
    game = cogs
    bot = game.bot
    ala = await three(game)
    panel = await show(game, ala)
    edits = len(panel.edits)
    with bot.state.hold_busy("hunt"):
        busy = await game.click(panel, ala, "Następny")
        assert busy.public == [f"<@{ala.id}> {ui.MSG_BUSY}"] and len(panel.edits) == edits
        removing = await game.click(panel, ala, "Usuń z albumu")             # (off on the overview anyway)
        assert removing.replies == [] or removing.answers == [ui.MSG_BUSY]
        monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="read_only_free"))
        assert (await game.click(panel, ala, "Następny")).replies == []
        assert page(panel).endswith("Wpis **1 z 3**")
    assert len(await album_rows(bot.db, ala.id)) == 3


async def test_the_panel_says_nothing_during_a_typed_fight(cogs, monkeypatch):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    edits, posted = len(panel.edits), len(game.channel.sent)
    real = challenge.fight_on
    monkeypatch.setattr(challenge, "fight_on", lambda channel: True)
    for label in ("Następny", "Usuń z albumu"):
        silent = await game.click(panel, ala, label)
        assert silent.response.deferred and silent.replies == []
    assert len(panel.edits) == edits and len(game.channel.sent) == posted
    monkeypatch.setattr(challenge, "fight_on", real)
    assert (await game.click(panel, ala, "Następny")).replies == [] and page(panel).endswith("Wpis **1 z 3**")


@pytest.mark.parametrize("answer", ["yes", "no"])
async def test_a_fight_that_starts_during_the_question_silences_the_answer(cogs, monkeypatch, answer):
    """Owner's rule: nothing is posted between a typed fight's prompts.  A fight that starts during the
    15 s question: the confirmed removal still happens, but no answer is posted and the panel is not
    redrawn (its next click reads the album anew)."""
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")                                  # the bear
    fighting: list[bool] = []
    monkeypatch.setattr(challenge, "fight_on", lambda channel: bool(fighting))
    game.auto_click(ala, answer, when=lambda message: fighting.append(True) or True)
    edits = len(panel.edits)
    click = await game.click(panel, ala, "Usuń z albumu")
    assert fighting and click.replies == []
    question = next(m for m in game.channel.sent if "usunąć z albumu" in m.content)
    assert game.channel.sent[-1] is question                                  # nothing after the question
    assert len(panel.edits) == edits and not pets._awaiting_confirmation
    assert len(await album_rows(game.bot.db, ala.id)) == (2 if answer == "yes" else 3)
    fighting.clear()                                                          # the fight is over
    await game.click(panel, ala, "Następny")
    assert page(panel).endswith("Wpis **1 z 2**" if answer == "yes" else "Wpis **2 z 3**")
    assert panel.embeds[0].title == "📖 Kot Mruczek"


async def test_a_dead_hunter_cannot_make_the_bot_post(cogs):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    game.channel.silenced_roles.add(game.bot.ids.dead_hunter_role)
    ala.roles.append(game.guild.add_role(game.bot.ids.dead_hunter_role))
    refused = await game.click(panel, ala, "Następny")
    assert refused.answers == [ui.MSG_CANNOT_PLAY] and not page(panel).endswith("Wpis **1 z 3**")


async def test_the_panel_is_for_supporters_too(cogs):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    ala.roles.clear()                                                         # the support ended
    refused = await game.click(panel, ala, "Następny")
    assert refused.public == [supporters_only(ala)] and panel.view is None   # told; the buttons are gone
    assert len(await album_rows(game.bot.db, ala.id)) == 3


async def test_the_panel_when_the_database_is_down(cogs):
    game = cogs
    ala = await three(game)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")                                  # an entry: "Usuń" is on
    assert "Usuń z albumu" in panel.button_labels
    real = game.bot.db
    game.bot.db = DatabaseDown()
    click = await game.click(panel, ala, "Następny")
    assert click.answers == [hub.MSG_DB_DOWN] and game.bot.owner_log == []
    assert page(panel).endswith("Wpis **1 z 3**")
    remove = await game.click(panel, ala, "Usuń z albumu")
    assert remove.answers == [hub.MSG_DB_DOWN] and not pets._awaiting_confirmation   # the slot is free again
    assert all("usunąć z albumu" not in message.text for message in game.channel.sent)
    game.bot.db = real
    assert len(await album_rows(real, ala.id)) == 3


async def test_the_album_before_its_table_exists(cogs):
    """The database did not answer at the first start: no table yet - an empty album, not an error."""
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await game.bot.db.execute("DROP TABLE PET_ALBUM", idempotent=True)
    await run(game, ala, "!album")
    (embed,) = game.channel.messages[-1].embeds
    assert embed.description == album.MSG_EMPTY.format(player_id=ala.id, total=68)
    assert game.bot.owner_log == []


async def test_the_panel_times_out_like_the_pets_panels():
    view = album.AlbumPanel(types.SimpleNamespace(id=1), [])
    try:
        assert view.timeout == pets.PANEL_TIMEOUT
        assert all(item.custom_id.startswith(ui.FLOW_PREFIX + "album:") for item in view.children)
        assert len({item.custom_id for item in view.children}) == len(view.children) == 5
    finally:
        view.stop()


# -- removing an entry --------------------------------------------------------------------------------

async def test_removing_an_entry_after_the_confirmation(cogs):
    game = cogs
    db = game.bot.db
    ala = await three(game)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")                                  # the bear
    bear = panel.view.entry_id
    clicks = game.auto_click(ala, "yes")
    click = await game.click(panel, ala, "Usuń z albumu")
    assert click.response.deferred and click.replies == []
    question = next(m for m in game.channel.sent if "usunąć z albumu" in m.content)
    when = re.search(r"<t:\d+:f>", question.content).group(0)                # as on the entry's page
    assert question.content == album.MSG_REMOVE_ASK.format(player_id=ala.id, what="Niedźwiedź (Poziom 1)",
                                                           when=f" (w albumie od {when})")
    assert "Tego nie da się cofnąć" in question.content and question.content.endswith(pets.CONFIRM_HINT)
    assert question.view is None and (await clicks[0]).response.deferred
    assert game.channel.contains(album.MSG_REMOVED.format(what="Niedźwiedź (Poziom 1)", player_id=ala.id))
    assert bear not in [row["entry_id"] for row in await album_rows(db, ala.id)]
    assert len(await album_rows(db, ala.id)) == 2
    # The panel shows what took the entry's place.
    assert page(panel).endswith("Wpis **1 z 2**") and panel.embeds[0].title == "📖 Kot Mruczek"


async def test_removing_by_typing_potwierdzam_and_cancelling(cogs, monkeypatch):
    game = cogs
    db = game.bot.db
    ala = await three(game)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    game.auto_click(ala, "no")
    await game.click(panel, ala, "Usuń z albumu")
    assert game.channel.messages[-1].content == album.MSG_REMOVE_CANCELLED
    assert len(await album_rows(db, ala.id)) == 3
    scale = timing.TIMEOUT_SCALE
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.001)
    await game.click(panel, ala, "Usuń z albumu")                             # nobody answers in time
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", scale)
    assert game.channel.messages[-1].content == album.MSG_REMOVE_CANCELLED
    assert len(await album_rows(db, ala.id)) == 3

    game.channel.send_hooks.clear()
    confirms(game, ala)
    await game.click(panel, ala, "Usuń z albumu")
    assert len(await album_rows(db, ala.id)) == 2
    typed = next(m for m in game.channel.messages if m.content == "!potwierdzam")
    assert typed.reactions == ["<:MonkaS:882181709100097587>"]


async def test_a_double_click_removes_one_entry(cogs):
    game = cogs
    db = game.bot.db
    ala = await three(game)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    first = game.click_later(panel, ala, "Usuń z albumu")
    question = await game.wait_for_buttons("yes")
    again = await game.click(panel, ala, "Usuń z albumu")                    # while the question is open
    assert again.response.deferred and game.channel.messages[-1].content == pets.MSG_ALREADY_WAITING
    await game.click(question, ala, "yes")
    await asyncio.wait_for(first, 2)
    assert len(await album_rows(db, ala.id)) == 2
    assert game.channel.sent_count("usunąć z albumu") == 1


async def test_two_panels_never_remove_two_entries(cogs):
    game = cogs
    db = game.bot.db
    ala = await three(game)
    one, two = await show(game, ala), await show(game, ala)
    for panel in (one, two):
        await game.click(panel, ala, "Następny")                              # both show the bear
    game.auto_click(ala, "yes")
    await game.click(one, ala, "Usuń z albumu")
    assert len(await album_rows(db, ala.id)) == 2
    gone = await game.click(two, ala, "Usuń z albumu")                        # the bear is gone already
    assert gone.response.deferred
    assert game.channel.messages[-1].content == album.MSG_ALREADY_GONE.format(player_id=ala.id)
    assert game.channel.sent_count("usunąć z albumu") == 1                    # no second question
    assert len(await album_rows(db, ala.id)) == 2
    assert page(two).endswith("Wpis **1 z 2**")                               # redrawn: what is there now


async def test_after_a_removal_elsewhere_the_next_page_is_what_took_the_entrys_place(cogs):
    """Panel one shows an entry that another panel removes: "Następny" there shows the entry that slid
    into its place (not the one after it), "Poprzedni" the one before it."""
    game = cogs
    ala = await three(game)
    one, two = await show(game, ala), await show(game, ala)
    for panel in (one, two):
        await game.click(panel, ala, "Następny")                              # both show the bear
    game.auto_click(ala, "yes")
    await game.click(two, ala, "Usuń z albumu")                               # removed in panel two
    assert page(one).endswith("Wpis **1 z 3**") and "Niedźwiedź" in one.embeds[0].title   # (not redrawn)
    await game.click(one, ala, "Następny")
    assert page(one).endswith("Wpis **1 z 2**") and one.embeds[0].title == "📖 Kot Mruczek"   # the cat of level 2
    await game.click(one, ala, "Następny")
    assert page(one).endswith("Wpis **2 z 2**") and one.embeds[0].title == "📖 Jajko"

    # The middle one goes: back from its place is the entry before it, forth the entry after it.
    await put(game.bot.db, ala.id, "Bear", 1)
    for panel in (one, two):
        await game.click(panel, ala, "Przegląd")
        await game.click(panel, ala, "Następny")
        await game.click(panel, ala, "Następny")
        assert page(panel).endswith("Wpis **2 z 3**") and panel.embeds[0].title == "📖 Kot Mruczek"
    game.auto_click(ala, "yes")
    await game.click(two, ala, "Usuń z albumu")
    await game.click(one, ala, "Poprzedni")
    assert page(one).endswith("Wpis **1 z 2**") and "Niedźwiedź" in one.embeds[0].title
    await game.click(two, ala, "Przegląd")
    await game.click(two, ala, "Następny")
    await game.click(two, ala, "Następny")                                    # two: the egg
    game.auto_click(ala, "yes")
    await game.click(one, ala, "Następny")
    await game.click(one, ala, "Usuń z albumu")                                # one removes the egg ...
    await game.click(two, ala, "Poprzedni")                                    # ... two goes back: the bear
    assert page(two).endswith("Wpis **1 z 1**") and "Niedźwiedź" in two.embeds[0].title


async def test_quick_clicks_after_a_removal_elsewhere_count_from_the_entrys_place(cogs, monkeypatch):
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    for level in range(4):
        await put(db, ala.id, "Cat", level)
    await put(db, ala.id, "Dog", 1)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")                                  # the cat of level 1
    first = panel.view.entry_id
    assert "Etap: **Poziom 1**" in page(panel) and "Gatunek: **Kot**" in page(panel)
    assert await album.remove(db, ala.id, first)                              # (a second !album)
    go = asyncio.Event()

    async def slow(source, look, cache_dir, *, max_bytes=None):
        await go.wait()
        return source

    monkeypatch.setattr(pets.petcards, "card", slow)
    clicks = [start_click(game, panel, ala, "album:next")[1] for _ in range(3)]
    for _ in range(50):
        await asyncio.sleep(0)
    go.set()
    await asyncio.wait_for(asyncio.gather(*clicks), 3)
    for _ in range(50):
        await asyncio.sleep(0)
    # Level 2 took its place: three clicks are level 2, level 3 and the dog (the egg is the last page).
    assert page(panel).endswith("Wpis **3 z 4**") and panel.embeds[0].title == "📖 Pies"
    assert not panel.view._pending


async def test_the_entry_shown_at_the_click_is_removed_whatever_the_page_shows_later(cogs):
    game = cogs
    db = game.bot.db
    ala = await three(game)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")                                  # the bear
    bear = panel.view.entry_id
    removal = game.click_later(panel, ala, "Usuń z albumu")
    question = await game.wait_for_buttons("yes")
    await game.click(panel, ala, "Następny")                                  # the cat, during the question
    cat = panel.view.entry_id
    await game.click(question, ala, "yes")
    await asyncio.wait_for(removal, 2)
    left = [row["entry_id"] for row in await album_rows(db, ala.id)]
    assert bear not in left and cat in left and len(left) == 2
    assert panel.view.entry_id == cat and page(panel).endswith("Wpis **1 z 2**")


async def twins(game):
    """A supporter with two entries of one slot and one name: a plain cat, then one with a rare frame."""
    ala = game.add_member("Ala", roles=[PATRON])
    db = game.bot.db
    plain = await put(db, ala.id, "Cat", 2, pet_name="Mruczek", shiny=True, frame="")
    rare = await put(db, ala.id, "Cat", 2, pet_name="Mruczek", shiny=True, frame="R7-A", seal=3)
    await db.execute("UPDATE PET_ALBUM SET ADDED_AT = ADDED_AT - interval '1 day' WHERE ENTRY_ID = $1", plain)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    assert page(panel).endswith("Wpis **1 z 2**") and "Pieczęć" not in page(panel)
    return ala, panel, plain, rare


async def test_the_entry_on_screen_is_removed_while_the_next_card_is_drawn(cogs, monkeypatch):
    """"Następny" draws the rare twin's card (seconds); meanwhile the message still shows the plain one,
    and "Usuń z albumu" means that one - never the entry whose page is not shown yet."""
    game = cogs
    db = game.bot.db
    ala, panel, plain, rare = await twins(game)
    assert panel.view.entry_id == plain
    started, go = asyncio.Event(), asyncio.Event()

    async def slow(source, look, cache_dir, *, max_bytes=None):
        started.set()
        await go.wait()                                                       # a cold animated card
        return source

    monkeypatch.setattr(pets.petcards, "card", slow)
    _, turning = start_click(game, panel, ala, "album:next")
    await asyncio.wait_for(started.wait(), 2)
    assert page(panel).endswith("Wpis **1 z 2**") and panel.view.entry_id == plain   # still on screen
    game.auto_click(ala, "yes")
    await asyncio.wait_for(game.click(panel, ala, "Usuń z albumu"), 3)
    question = next(m for m in game.channel.sent if "usunąć z albumu" in m.content)
    assert [row["entry_id"] for row in await album_rows(db, ala.id)] == [rare]   # the plain one is gone
    assert "(w albumie od <t:" in question.content
    go.set()
    await asyncio.wait_for(turning, 3)
    # The page drawn meanwhile is followed by the album as it is now: the rare twin, alone.
    assert panel.view.entry_id == rare and page(panel).endswith("Wpis **1 z 1**")


async def test_the_twins_questions_tell_them_apart(cogs):
    game = cogs
    ala, panel, plain, rare = await twins(game)
    rows = {row["entry_id"]: row for row in await album_rows(game.bot.db, ala.id)}
    first, second = (album._remove_question(ala.id, rows[entry_id]) for entry_id in (plain, rare))
    assert first != second and album.describe(rows[plain]) == album.describe(rows[rare])
    assert album.MSG_REMOVE_WHEN.format(when=ui.when(rows[plain]["added_at"], "f")) in first


async def test_after_a_page_edit_failed_the_entry_still_on_screen_is_removed(cogs, monkeypatch):
    game = cogs
    db = game.bot.db
    ala, panel, plain, rare = await twins(game)
    real = pets._edit_panel
    refused = []

    async def refusing_once(message, **kwargs):
        if not refused:
            refused.append(kwargs)
            return False                                                      # e.g. HTTP 5xx on the edit
        return await real(message, **kwargs)

    monkeypatch.setattr(pets, "_edit_panel", refusing_once)
    await game.click(panel, ala, "Następny")
    assert refused and page(panel).endswith("Wpis **1 z 2**") and panel.view.entry_id == plain
    game.auto_click(ala, "yes")
    await game.click(panel, ala, "Usuń z albumu")
    assert [row["entry_id"] for row in await album_rows(db, ala.id)] == [rare]


async def test_a_pets_name_pings_nobody(cogs, monkeypatch):
    """The name is the player's text: "@everyone" or a role in it must not make the bot ping."""
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await put(game.bot.db, ala.id, "Cat", 2, pet_name="@everyone <@&985071779787730944> <@1234>")
    await put(game.bot.db, ala.id, "Cat", 3)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    sent = []
    real = game.channel.send

    async def send(content=None, **kwargs):
        sent.append((content, kwargs.get("allowed_mentions")))
        return await real(content, **kwargs)

    monkeypatch.setattr(game.channel, "send", send)
    game.auto_click(ala, "yes")
    await game.click(panel, ala, "Usuń z albumu")
    named = [(content, mentions) for content, mentions in sent if content and "@everyone" in content]
    assert len(named) == 2                                                    # the question and the answer
    for content, mentions in named:
        assert mentions is not None and mentions.everyone is False and mentions.roles is False, content
        assert [user.id for user in mentions.users] == [ala.id]               # only the player himself
    assert len(await album_rows(game.bot.db, ala.id)) == 1


async def test_quick_clicks_draw_only_the_page_they_lead_to(cogs, monkeypatch):
    """Five quick "Następny": five entries on, but the cards in between are never drawn (each takes
    seconds, and the bot draws one card at a time for every player)."""
    game = cogs
    db = game.bot.db
    ala = game.add_member("Ala", roles=[PATRON])
    for level in range(4):
        await put(db, ala.id, "Cat", level, shiny=True)
    await put(db, ala.id, "Dog", 1, shiny=True)
    await put(db, ala.id, "Dog", 2, shiny=True)
    panel = await show(game, ala)
    drawn, go = [], asyncio.Event()

    async def slow(source, look, cache_dir, *, max_bytes=None):
        drawn.append(source)
        await go.wait()
        return source

    monkeypatch.setattr(pets.petcards, "card", slow)
    clicks = [start_click(game, panel, ala, "album:next")[1] for _ in range(5)]
    # (Waited for in real time: the first page reads the album from the database before it is drawn,
    # which takes more than a fixed number of turns of the event loop.)
    assert await until(lambda: drawn and len(panel.view._pending) == 4)    # the others wait for it
    await asyncio.sleep(0.05)
    assert len(drawn) == 1                                                    # the first page only, so far
    go.set()
    await asyncio.wait_for(asyncio.gather(*clicks), 3)
    assert await until(lambda: len(drawn) >= 2)
    await asyncio.sleep(0.05)
    assert len(drawn) == 2 and page(panel).endswith("Wpis **5 z 6**")
    assert panel.view.position == 4 and not panel.view._pending
    await game.click(panel, ala, "Poprzedni")                                 # one by one as before
    assert page(panel).endswith("Wpis **4 z 6**") and len(drawn) == 3


async def test_removing_the_last_entry_ends_the_panel(cogs):
    game = cogs
    ala = game.add_member("Ala", roles=[PATRON])
    await put(game.bot.db, ala.id, "Sheep", 3)
    panel = await show(game, ala)
    await game.click(panel, ala, "Następny")
    game.auto_click(ala, "yes")
    await game.click(panel, ala, "Usuń z albumu")
    assert await album_rows(game.bot.db, ala.id) == []
    assert panel.view is None and panel.embeds[0].description == album.MSG_EMPTY.format(player_id=ala.id, total=68)
    assert files(panel) == []


async def test_nobody_removes_somebody_elses_entry(cogs):
    game = cogs
    db = game.bot.db
    ala = await three(game)
    ola = game.add_member("Ola", roles=[PATRON])
    theirs = await put(db, ola.id, "Cat", 0)
    panel = await show(game, ala)
    panel.view.entry_id, panel.view.position = theirs, 0                     # (as if the panel pointed there)
    await game.click(panel, ala, "Usuń z albumu")
    assert game.channel.messages[-1].content == album.MSG_ALREADY_GONE.format(player_id=ala.id)
    assert [row["entry_id"] for row in await album_rows(db, ola.id)] == [theirs]
    assert len(await album_rows(db, ala.id)) == 3


# -- the hub's button and the help -----------------------------------------------------------------------

async def test_the_hub_button_shows_what_the_command_shows(cogs, monkeypatch):
    game = cogs
    ala = await three(game)
    ola = game.add_member("Ola")
    hub_panel, _ = await hub.post_panel(game.bot, game.channel)
    for member in (ala, ola):
        click = await game.click(hub_panel, member, "Album")
        assert click.response.deferred and click.replies == []
        clicked = game.channel.messages[-1]
        await run(game, member, "!album")
        typed = game.channel.messages[-1]
        assert typed is not clicked and clicked.text == typed.text and type(clicked.view) is type(typed.view)
    assert isinstance(clicked.view, type(None)) and clicked.content == supporters_only(ola)

    # Busy: the button waits like the other display buttons (and the typed command).
    with game.bot.state.hold_busy("hunt"):
        busy = await game.click(hub_panel, ala, "Album")
        assert busy.public == [f"<@{ala.id}> {ui.MSG_BUSY}"]
        before = len(game.channel.sent)
        await run(game, ala, "!album")
        assert all(album.MSG_TITLE not in m.text for m in game.channel.sent[before:])


async def test_typed_album_follows_the_busy_policy(cogs, monkeypatch):
    game = cogs
    ala = await three(game)
    with game.bot.state.hold_busy("hunt"):
        await run(game, ala, "!album")
        assert game.channel.messages[-1].content == guards.BUSY_NOTICE
        monkeypatch.setattr(game.bot, "settings",
                            dataclasses.replace(game.bot.settings, busy_policy="read_only_free"))
        await run(game, ala, "!album")
        assert game.channel.messages[-1].embeds[0].title == album.MSG_TITLE


async def test_album_works_on_any_channel_of_the_server(cogs):
    game = cogs
    ala = await three(game)
    await run(game, ala, "!album", channel=game.announce_channel)
    message = game.announce_channel.messages[-1]
    assert message.embeds[0].title == album.MSG_TITLE
    await game.click(message, ala, "Następny")
    assert page(message).endswith("Wpis **1 z 3**")


def test_the_help_and_the_panel_mention_the_album():
    assert "!album" in help_cog.HELP_TEXT and "!kolekcja" in help_cog.HELP_TEXT
    supporters = dict(help_cog.HELP_FIELDS)["🙏 Wspierający"]
    assert "- !album - " in supporters and len(supporters) <= 1024
    assert "📖 **Album**" in hub.PANEL_GAME_TEXT and "(**!album**)" in hub.PANEL_SUPPORT_TEXT
    for secret in ("holo", "pieczę", "ramk", "smok", "zjaw", "68"):
        assert secret not in hub.PANEL_BENEFITS[-1].lower()


async def test_the_command_and_its_alias(cogs):
    command = cogs.bot.get_command("kolekcja")
    assert command is not None and command.name == "album" and command.cog_name == "Album"
    assert not any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)


async def test_a_sacrifice_then_the_album(cogs, monkeypatch):
    """The whole way: !porzucam of a supporter, then !album shows the pet with its card."""
    game = cogs
    db = game.bot.db
    await game.bot.load_extension("altermmo.cogs.pets")
    monkeypatch.setattr(pets.modifiers, "random_modifiers", lambda *args: asyncio.sleep(0))
    ala = game.add_member("Ala", roles=[PATRON])
    await own_pet(db, ala.id, 5, type="Unicorn", quality="Premium", pet_lvl=3, pet_name="Iskra", variant="1")
    confirms(game, ala)
    await run(game, ala, "!porzucam")
    assert len(await album_rows(db, ala.id)) == 1 and await pets._pet(db, 5) is None
    assert game.channel.contains(album.MSG_DISCARD_SAVED.format(player_id=ala.id))
    game.channel.send_hooks.clear()
    await run(game, ala, "!album")
    panel = game.channel.messages[-1]
    assert "**Jednorożec** (1 z 3): ▫️ Poziom 1 · ▫️ Poziom 2 · ✅ Poziom 3" in page(panel)
    await game.click(panel, ala, "Następny")
    assert panel.embeds[0].title == "📖 Jednorożec Iskra" and files(panel) == ["101.png"]
    assert "Imię: **Iskra**" in page(panel) and ASK not in page(panel)


async def test_a_supporters_sacrifice_through_the_pet_panel(cogs, monkeypatch):
    """The pet panel's "Porzuć" (the scene built from the click, ``expected_pet`` set): the pet is kept,
    both texts name the album, and the 68th slot gives "Pełny album"."""
    from test_album import fill, holds_album_role

    game = cogs
    db = game.bot.db
    await game.bot.load_extension("altermmo.cogs.pets")
    sacrificed = []

    async def random_modifiers(scene, msg=True, sacrefice=False):
        sacrificed.append(scene.author)

    monkeypatch.setattr(pets.modifiers, "random_modifiers", random_modifiers)
    ala = game.add_member("Ala", roles=[PATRON])
    assert await fill(db, ala.id, leave_out={("Ghost", 2)}) == 67
    await own_pet(db, ala.id, 5, type="Ghost", quality="Premium", pet_lvl=2, pet_name="Kacper", variant="1")
    assert await pets.show_pet(Scene(bot=game.bot, channel=game.channel, author=ala), ala) is True
    panel = game.channel.messages[-1]
    clicks = game.auto_click(ala, "yes")
    click = await game.click(panel, ala, "pet:discard")
    assert click.response.deferred and click.replies == [] and (await clicks[0]).response.deferred

    question = next(m for m in game.channel.sent if ASK in m.content)
    assert album.MSG_DISCARD_KEPT in question.content and "**!album**" in question.content
    assert await pets._pet(db, 5) is None and sacrificed == [ala]
    rows = await album_rows(db, ala.id)
    assert len(rows) == 68 and {(row["type"], row["pet_lvl"], row["pet_name"]) for row in rows
                                if row["pet_name"] == "Kacper"} == {("Ghost", 2, "Kacper")}
    saved = album.MSG_DISCARD_SAVED.format(player_id=ala.id)
    texts = [m.content for m in game.channel.sent]
    assert texts.count(saved) == 1 and "**!album**" in saved
    announced = f"<@{ala.id}> zdobywa osiągnięcie: Pełny album!"
    assert holds_album_role(game.bot, ala) and texts.count(announced) == 1
    assert texts.index(saved) < texts.index(announced)
