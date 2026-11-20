"""Pets with buttons: confirmations, the advanced reroll list, the stable list and the pet panel."""

from __future__ import annotations

import asyncio
import dataclasses
import types

import discord
import pytest
import pytest_asyncio

from altermmo.core import challenge, guards, ui
from altermmo.core.scene import Scene
from altermmo.features import hub, pets
from altermmo.features.pets import PetType

from fakes import FakeInteraction, find_component
from test_pets import add_owner, add_pet, owner_row, patron, pet_row, rig, run, until
from test_pets import plain_cards  # noqa: F401 - autouse: pets are sent as their plain pictures here too

QUESTION = "Czy jesteś pewien"
UNSURE = "*Twój towarzysz spogląda na Ciebie niepewnie.*"
PEPOG = "<:PepoG:790963160528977980>"
MONKAS = "<:MonkaS:882181709100097587>"


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a $potwierdzam slot was never released"


@pytest.fixture
def sacrifices(monkeypatch):
    calls: list = []

    async def random_modifiers(scene, msg=True, sacrefice=False):
        calls.append(scene.author)
        return "points_boost"

    monkeypatch.setattr(pets.modifiers, "random_modifiers", random_modifiers)
    return calls


@pytest_asyncio.fixture
async def cog(game_db):
    await game_db.bot.load_extension("altermmo.cogs.pets")
    for channel in game_db.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    return game_db.bot.get_cog("Pets")


def not_yours(member) -> str:
    return ui.MSG_NOT_YOURS_NAMED.format(player_id=member.id)


def dead_hunter(game, name: str = "Jan"):
    """A member with the "dead hunter" role, which may not write on the boss channel."""
    game.channel.silenced_roles.add(game.bot.ids.dead_hunter_role)
    return game.add_member(name, roles=[game.bot.ids.dead_hunter_role])


def purged(message) -> None:
    """The boss channel was purged: the message is gone and editing it fails."""
    message.deleted = True
    if message in message.channel.messages:
        message.channel.messages.remove(message)

    async def gone(**kwargs):
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")

    message.edit = gone


def start_click(game, message, member, key: str):
    """Like ``game.click``, but returns at once: for buttons that open a pop-up window and wait for it."""
    view = message.view
    item = find_component(view, key)
    interaction = FakeInteraction(game.bot, member, message,
                                  data={"custom_id": item.custom_id, "component_type": 2})

    async def go() -> None:
        try:
            if await view.interaction_check(interaction):
                await item.callback(interaction)
        except Exception as exc:  # noqa: BLE001 - what discord.py does
            await view.on_error(interaction, exc, item)

    task = asyncio.ensure_future(go())
    game._tasks.append(task)
    return interaction, task


def scene_on(game, channel, player) -> Scene:
    return Scene(bot=game.bot, channel=channel, author=player)


async def show(game, player, channel=None):
    """``$towarzysz`` of ``player``; returns the message with the pet (and its panel)."""
    assert await pets.show_pet(scene_on(game, channel or game.channel, player), player) is True
    return (channel or game.channel).messages[-1]


async def grown(game, *, name: str = "Ala", patron_rank: bool = False, pet_id: int = 5, **pet) -> object:
    player = patron(game, name) if patron_rank else game.add_member(name)
    await add_owner(game.bot.db, player.id, pet_id, reroll_scroll=3, rebirth_stones=1, mirrors=1)
    await add_pet(game.bot.db, pet_id, **{**dict(pet_lvl=2, pet_skills=3, crit_perc=4, variant="1"), **pet})
    return player


# -- "are you sure?" with buttons -------------------------------------------------------

@pytest.mark.parametrize("flow", ["reroll", "enlight", "transform", "discard"])
async def test_every_question_can_be_confirmed_with_a_button(game_db, monkeypatch, sacrifices, flow):
    db = game_db.bot.db
    player = await grown(game_db, patron_rank=True, pet_lvl=3)
    rig(monkeypatch, choices=[PetType.DOG], randint=1)
    clicks = game_db.auto_click(player, "yes")
    scene = game_db.scene(player)
    run_flow = {"reroll": lambda: pets.reroll_pet(scene, player),
                "enlight": lambda: pets.enlight_pet(scene, player),
                "transform": lambda: pets.transform_pet(scene, player),
                "discard": lambda: pets.discard_pet(scene)}
    assert await asyncio.wait_for(run_flow[flow](), 5) is True

    question = next(m for m in game_db.channel.sent if QUESTION in m.content)
    assert question.content.endswith("Kliknij **Potwierdzam** albo wpisz **$potwierdzam**.")
    assert question.view is None                                             # the buttons are gone
    assert question.reactions == [MONKAS if flow == "discard" else PEPOG]    # the legacy "seen"
    assert (await clicks[0]).replies == [] and (await clicks[0]).response.deferred
    row = await owner_row(db, player.id)
    if flow == "reroll":
        assert row["reroll_scroll"] == 2
    elif flow == "enlight":
        assert row["rebirth_stones"] == 0 and (await pet_row(db, 5))["pet_skills"] > 3
    elif flow == "transform":
        assert row["mirrors"] == 0 and (await pet_row(db, 5))["type"] == "Dog"
    else:
        assert row["pet_id"] == 0 and await pet_row(db, 5) is None and sacrifices == [player]


async def test_cancel_button_and_strangers(game_db, sacrifices):
    db = game_db.bot.db
    player = await grown(game_db)
    other = game_db.add_member("Ola")
    task = asyncio.ensure_future(pets.reroll_pet(game_db.scene(player), player))
    question = await game_db.wait_for_buttons("yes")
    assert question.button_labels == ["Potwierdzam", "Anuluj"]
    refused = await game_db.click(question, other, "yes")                   # on the channel, for Ola
    assert refused.public == [f"<@{other.id}> {not_yours(player)}"]
    assert refused.replies[0].message in game_db.channel.messages and question.view is not None
    await game_db.click(question, player, "no")
    assert await asyncio.wait_for(task, 5) is False
    assert game_db.channel.messages[-1].content == UNSURE and question.view is None
    assert (await owner_row(db, player.id))["reroll_scroll"] == 3

    task = asyncio.ensure_future(pets.discard_pet(game_db.scene(player)))
    await game_db.click(await game_db.wait_for_buttons("no"), player, "no")
    assert await asyncio.wait_for(task, 5) is False
    assert game_db.channel.messages[-1].content == "*Twój towarzysz oddycha z ulgą...*"
    assert await pet_row(db, 5) is not None and sacrifices == []


async def test_question_timing_out_or_purged(game_db, short_timeouts):
    db = game_db.bot.db
    player = await grown(game_db)
    assert await pets.enlight_pet(game_db.scene(player), player) is False                  # level 2: refused
    await db.execute("UPDATE PETS SET PET_LVL = 3")
    assert await pets.enlight_pet(game_db.scene(player), player) is False                  # nobody answers
    question = next(m for m in game_db.channel.sent if QUESTION in m.content)
    assert question.view is None and game_db.channel.messages[-1].content == UNSURE


async def test_purged_question_can_still_be_confirmed_by_typing(game_db, monkeypatch):
    player = await grown(game_db)
    rig(monkeypatch)
    task = asyncio.ensure_future(pets.reroll_pet(game_db.scene(player), player))
    question = await game_db.wait_for_buttons("yes")
    purged(question)
    game_db.say(player, "$potwierdzam")
    assert await asyncio.wait_for(task, 5) is True
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 2


# -- $mocneodrodzenie with a list --------------------------------------------------------

async def test_advanced_reroll_cancel_and_dead_hunters(game_db):
    db = game_db.bot.db
    player = await grown(game_db)
    task = asyncio.ensure_future(pets.adv_reroll_pet(game_db.scene(player)))
    menu = await game_db.wait_for_buttons("pet:skip")
    game_db.channel.silenced_roles.add(game_db.bot.ids.dead_hunter_role)
    player.roles.append(game_db.guild.add_role(game_db.bot.ids.dead_hunter_role))           # died meanwhile
    refused = await game_db.click(menu, player, "pet:skip", values=["0"])
    assert refused.answers == [ui.MSG_CANNOT_PLAY] and "koszt: **1 zwój**" in menu.content
    player.roles.clear()
    await game_db.click(menu, player, "pet:skip-cancel")
    assert await asyncio.wait_for(task, 5) is False
    assert game_db.channel.messages[-1].content == UNSURE and menu.deleted and menu.view is None
    assert not game_db.channel.ever_sent(QUESTION) and (await owner_row(db, player.id))["reroll_scroll"] == 3


async def test_advanced_reroll_survives_a_purged_menu(game_db, monkeypatch):
    player = await grown(game_db, pet_skills=4)
    await game_db.bot.db.execute("UPDATE PETOWNER SET REROLL_SCROLL = 15")
    rig(monkeypatch)
    game_db.auto_click(player, "yes")
    task = asyncio.ensure_future(pets.adv_reroll_pet(game_db.scene(player)))
    menu = await game_db.wait_for_buttons("pet:skip")
    purged(menu)
    await game_db.click(menu, player, "pet:skip", values=["3"])          # the refresh fails quietly
    await game_db.click(menu, player, "pet:skip-accept")
    assert await asyncio.wait_for(task, 5) is True
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 5
    assert (await pet_row(game_db.bot.db, 5))["drop_perc"] == 0


def test_menu_text_counts_the_scrolls():
    assert pets.adv_reroll_text([]).endswith("Pominięte: brak - koszt: **1 zwój**.")
    assert pets.adv_reroll_text([5]).endswith("Pominięte: **Spowolnienie przeciwnika** - koszt: **10 zwojów**.")
    assert pets._scrolls(2) == "2 zwoje" and pets._scrolls(12) == "12 zwojów" and pets._scrolls(22) == "22 zwoje"


# -- the stable list ---------------------------------------------------------------------

async def stable_setup(game):
    db = game.bot.db
    player = game.add_member("Ala")
    await add_owner(db, player.id, 5, pet_id_alt1=6, pet_id_alt2=404, pet_id_alt3=7)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=3, type="Dog")
    await add_pet(db, 6, pet_lvl=0)
    await add_pet(db, 7, pet_name="Mruczek", pet_lvl=2, type="Cat")
    return player


async def slots(db, player) -> list:
    row = await owner_row(db, player.id)
    return [row[name] for name in ("pet_id", "pet_id_alt1", "pet_id_alt2", "pet_id_alt3", "pet_id_alt4")]


async def test_stable_list_makes_a_stabled_pet_the_active_one(game_db):
    db = game_db.bot.db
    player = await stable_setup(game_db)
    other = game_db.add_member("Ola")
    assert await pets.check_stable(game_db.scene(player)) is True
    stable = game_db.channel.messages[-1]
    select = stable.button("pet:stable-take")
    assert [(o.value, o.label, o.description) for o in select.options] == [
        ("1:6", "Towarzysz · jajko", "Miejsce w stajni 1"),
        ("3:7", "Mruczek · Kot · poziom 2", "Miejsce w stajni 3")]
    assert ui.is_live(stable.id)                                        # (slot 2 has no PETS row: not offered)

    assert (await game_db.click(stable, other, "pet:stable-take", values=["3:7"])).answers == [not_yours(player)]
    interaction = await game_db.click(stable, player, "pet:stable-take", values=["3:7"])
    assert interaction.response.deferred and interaction.replies == []
    assert await slots(db, player) == [7, 6, 404, 5, 0]
    assert game_db.channel.contains(pets.MSG_SWAPPED)
    assert "🐾 **Przy Tobie**\n**Mruczek** · Kot · poziom 2\n" in stable.embeds[0].description  # redrawn
    assert [o.label for o in stable.button("pet:stable-take").options] == [
        "Towarzysz · jajko", "Burek · Pies · poziom 3"]

    # The typed command keeps working next to the list.
    await pets.unstore_pet(game_db.scene(player), 1)
    assert await slots(db, player) == [6, 7, 404, 5, 0]


async def test_stable_list_has_the_guards_of_the_command(game_db):
    db = game_db.bot.db
    player = await stable_setup(game_db)
    elsewhere = game_db.announce_channel
    assert await pets.check_stable(scene_on(game_db, elsewhere, player)) is True
    far = elsewhere.messages[-1]                                        # display works anywhere ...
    refused = await game_db.click(far, player, "pet:stable-take", values=["1:6"])
    assert refused.answers == [ui.MSG_WRONG_CHANNEL.format(channel_id=game_db.bot.ids.boss_channel)]
    assert elsewhere.messages[-1] is refused.replies[0].message                # said where it was clicked

    await pets.check_stable(game_db.scene(player))
    stable = game_db.channel.messages[-1]
    with game_db.bot.state.hold_busy("hunt"):
        assert (await game_db.click(stable, player, "pet:stable-take", values=["1:6"])).answers == [ui.MSG_BUSY]
    pets._awaiting_confirmation.add(player.id)                          # a question is open
    redraws = len(stable.edits)
    await game_db.click(stable, player, "pet:stable-take", values=["1:6"])
    pets._awaiting_confirmation.clear()
    assert game_db.channel.contains("Już czekam na Twoje potwierdzenie.")
    assert len(stable.edits) == redraws + 1 and stable.view is not None   # redrawn: the entry is not left chosen
    game_db.channel.silenced_roles.add(game_db.bot.ids.dead_hunter_role)
    player.roles.append(game_db.guild.add_role(game_db.bot.ids.dead_hunter_role))
    assert (await game_db.click(stable, player, "pet:stable-take", values=["1:6"])).answers == [ui.MSG_CANNOT_PLAY]
    assert await slots(db, player) == [5, 6, 404, 7, 0]


async def test_stable_without_stabled_pets_has_no_list_and_the_list_times_out(game_db, monkeypatch):
    db = game_db.bot.db
    player = game_db.add_member("Ala")
    await add_owner(db, player.id, 5)
    await add_pet(db, 5)
    await pets.check_stable(game_db.scene(player))
    assert game_db.channel.messages[-1].view is None

    await db.execute("UPDATE PETOWNER SET PET_ID = 0, PET_ID_ALT4 = 5")
    await pets.check_stable(game_db.scene(player))
    stable = game_db.channel.messages[-1]
    await game_db.click(stable, player, "pet:stable-take", values=["4:5"])
    assert await slots(db, player) == [5, 0, 0, 0, 0] and game_db.channel.contains(pets.MSG_UNSTORED)
    assert stable.view is None and "**Przy Tobie**\n**Towarzysz** · jajko" in stable.embeds[0].description

    monkeypatch.setattr(pets, "PANEL_TIMEOUT", 0.05)
    await db.execute("UPDATE PETOWNER SET PET_ID_ALT2 = 5, PET_ID = 0")
    await pets.check_stable(game_db.scene(player))
    stable = game_db.channel.messages[-1]
    await asyncio.sleep(0.2)
    assert stable.view is None and not ui.is_live(stable.id)


async def test_stable_list_survives_a_purged_stable(game_db):
    player = await stable_setup(game_db)
    await pets.check_stable(game_db.scene(player))
    stable = game_db.channel.messages[-1]
    purged(stable)
    interaction = await game_db.click(stable, player, "pet:stable-take", values=["1:6"])
    assert interaction.replies == [] and await slots(game_db.bot.db, player) == [6, 5, 404, 7, 0]


# -- the pet panel -----------------------------------------------------------------------

ALL_BUTTONS = ["Zmień imię", "Stajnia", "Odrodzenie", "Mocne odrodzenie", "Oświecenie", "Transformacja",
               "Porzuć"]


async def test_panel_shows_the_buttons_that_make_sense(game_db):
    db = game_db.bot.db
    egg_owner = game_db.add_member("Ala")
    await add_owner(db, egg_owner.id, 1)
    await add_pet(db, 1)
    assert (await show(game_db, egg_owner)).button_labels == ["Zmień imię", "Stajnia", "Porzuć"]

    young = game_db.add_member("Ola")
    await add_owner(db, young.id, 2)
    await add_pet(db, 2, pet_lvl=2, pet_skills=3)
    assert (await show(game_db, young)).button_labels == [
        "Zmień imię", "Stajnia", "Odrodzenie", "Mocne odrodzenie", "Porzuć"]

    rich = patron(game_db, "Ela")
    await add_owner(db, rich.id, 3)
    await add_pet(db, 3, pet_lvl=3, pet_skills=6)
    panel = await show(game_db, rich)
    assert panel.button_labels == ALL_BUTTONS and ui.is_live(panel.id)
    assert panel.button("pet:discard").style is discord.ButtonStyle.danger
    assert panel.button("pet:reroll").style is discord.ButtonStyle.primary

    mythic = patron(game_db, "Ula")
    await add_owner(db, mythic.id, 4)
    await add_pet(db, 4, pet_lvl=3, pet_skills=7, mythic=True, quality="Mythic", type="Void")
    assert (await show(game_db, mythic)).button_labels == [
        "Zmień imię", "Stajnia", "Odrodzenie", "Mocne odrodzenie", "Porzuć"]

    # Shown for a click (the pinned panel's "Towarzysz"): nothing private any more - the same
    # pet with the same live panel, posted on the channel like $towarzysz.
    click = FakeInteraction(game_db.bot, rich, channel=game_db.channel)
    assert await pets.show_pet(ui.scene_of(click), rich) is True
    shown = game_db.channel.messages[-1]
    assert shown is not panel and shown.button_labels == ALL_BUTTONS and ui.is_live(shown.id)
    assert shown.files == ["100.png"] and click.replies == []
    # buttons=False still shows the pet alone.
    assert await pets.show_pet(scene_on(game_db, game_db.channel, rich), rich, buttons=False) is True
    assert game_db.channel.messages[-1].view is None and game_db.channel.messages[-1].embeds


async def test_panel_is_its_owners_and_display_works_anywhere(game_db, monkeypatch):
    player = await grown(game_db)
    other = game_db.add_member("Ola")
    elsewhere = game_db.announce_channel
    panel = await show(game_db, player, elsewhere)
    assert panel.button_labels[:2] == ["Zmień imię", "Stajnia"]
    assert (await game_db.click(panel, other, "pet:stable")).answers == [not_yours(player)]

    stable_click = await game_db.click(panel, player, "pet:stable")        # display only: any channel
    assert stable_click.response.deferred and stable_click.replies == []
    assert elsewhere.messages[-1].embeds[0].title == "Stajnia"
    for key in ("pet:reroll", "pet:adv-reroll", "pet:discard", "pet:rename"):
        refused = await game_db.click(panel, player, key)
        assert refused.answers == [ui.MSG_WRONG_CHANNEL.format(channel_id=game_db.bot.ids.boss_channel)]
        assert refused.response.modal is None
    assert panel.button_labels[:2] == ["Zmień imię", "Stajnia"] and not game_db.channel.ever_sent(QUESTION)

    # Busy: gameplay is refused; the stable follows the busy policy of $stajnia.  (Each click is
    # its own case here, so the one-busy-notice-per-20-s limiter is emptied before each.)
    panel = await show(game_db, player)
    with game_db.bot.state.hold_busy("hunt"):
        for key in ("pet:reroll", "pet:rename", "pet:stable"):
            guards._last_notice.clear()
            refused = await game_db.click(panel, player, key)
            assert refused.public == [f"<@{player.id}> {ui.MSG_BUSY}"] and refused.response.modal is None, key
        monkeypatch.setattr(game_db.bot, "settings",
                            dataclasses.replace(game_db.bot.settings, busy_policy="read_only_free"))
        assert (await game_db.click(panel, player, "pet:stable")).replies == []
        assert game_db.channel.messages[-1].embeds[0].title == "Stajnia"

    game_db.channel.silenced_roles.add(game_db.bot.ids.dead_hunter_role)
    player.roles.append(game_db.guild.add_role(game_db.bot.ids.dead_hunter_role))
    assert (await game_db.click(panel, player, "pet:reroll")).answers == [ui.MSG_CANNOT_PLAY]
    assert (await game_db.click(panel, player, "pet:stable")).replies == []           # looking is allowed
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 3


async def test_panel_buttons_start_the_command_flows(game_db, monkeypatch, sacrifices):
    db = game_db.bot.db
    player = await grown(game_db, patron_rank=True, pet_lvl=3)
    rig(monkeypatch, randint=1)
    game_db.auto_click(player, "yes", times=None)

    panel = await show(game_db, player)
    for key, check in (("pet:reroll", lambda row, pet: row["reroll_scroll"] == 2),
                       ("pet:enlight", lambda row, pet: row["rebirth_stones"] == 0 and pet["pet_skills"] == 4),
                       ("pet:transform", lambda row, pet: row["mirrors"] == 0 and pet["type"] == "Bear")):
        clicked = await game_db.click(panel, player, key)
        assert clicked.response.deferred and clicked.replies == []
        assert panel.view is None and not ui.is_live(panel.id)              # this panel is done
        assert check(await owner_row(db, player.id), await pet_row(db, 5)), key
        panel = game_db.channel.messages[-1]                               # the flow shows the pet anew
        assert panel.embeds and panel.button_labels[0] == "Zmień imię"

    await game_db.click(panel, player, "pet:discard")
    assert await pet_row(db, 5) is None and sacrifices == [player]


async def test_panel_advanced_reroll_holds_the_channel(game_db, monkeypatch):
    player = await grown(game_db)
    await game_db.bot.db.execute("UPDATE PETOWNER SET REROLL_SCROLL = 10")
    rig(monkeypatch)
    game_db.auto_click(player, "yes")
    panel = await show(game_db, player)
    clicking = game_db.click_later(panel, player, "pet:adv-reroll")
    menu = await game_db.wait_for_buttons("pet:skip")
    assert game_db.bot.state.busy_owner == "adv-reroll" and panel.view is None
    await game_db.click(menu, player, "pet:skip", values=["6"])
    await game_db.click(menu, player, "pet:skip-accept")
    await asyncio.wait_for(clicking, 5)
    assert not game_db.bot.state.busy and (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 0


async def test_panel_survives_being_purged_and_times_out(game_db, monkeypatch):
    player = await grown(game_db)
    rig(monkeypatch)
    game_db.auto_click(player, "yes")
    panel = await show(game_db, player)
    purged(panel)
    await game_db.click(panel, player, "pet:reroll")                         # the flow runs anyway
    assert (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 2

    monkeypatch.setattr(pets, "PANEL_TIMEOUT", 0.05)
    panel = await show(game_db, player)
    await asyncio.sleep(0.2)
    assert panel.view is None and not ui.is_live(panel.id)


async def test_panel_refusal_is_public_and_pings_only_the_clicker(game_db):
    player = await grown(game_db)
    other = game_db.add_member("Ola")
    panel = await show(game_db, player)
    refused = await game_db.click(panel, other, "pet:reroll")
    (answer,) = refused.replies
    assert answer.text == f"<@{other.id}> Ten przycisk jest dla <@{player.id}>." and not answer.ephemeral
    assert game_db.channel.messages[-1] is answer.message
    mentions = answer.allowed_mentions                   # the owner is named but not notified
    assert mentions.users == [other] and mentions.everyone is False and mentions.roles is False
    assert panel.view is not None and (await owner_row(game_db.bot.db, player.id))["reroll_scroll"] == 3


async def test_panel_notices_are_not_posted_between_the_prompts_of_a_typed_fight(game_db, monkeypatch):
    player = await two_pets(game_db)
    other = game_db.add_member("Ola")
    panel = await show(game_db, player)
    monkeypatch.setitem(challenge._rounds_running, game_db.channel.id, 1)   # a round of a fight is on
    before = len(game_db.channel.messages)
    stranger = await game_db.click(panel, other, "pet:discard")
    await game_db.bot.db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT3 = 5")
    stale = await game_db.click(panel, player, "pet:discard")
    for click in (stranger, stale):                      # acknowledged, nothing said
        assert click.response.deferred and click.replies == []
    # Nothing at all happens to the panel during a fight (it is looked at again afterwards).
    assert len(game_db.channel.messages) == before and panel.view is not None
    assert await pet_row(game_db.bot.db, 5) is not None and not game_db.channel.ever_sent(QUESTION)


async def test_pinned_panel_pet_and_stable_buttons_do_what_the_commands_do(game_db):
    db = game_db.bot.db
    player = await stable_setup(game_db)
    other = game_db.add_member("Ola")

    clicked = FakeInteraction(game_db.bot, player, channel=game_db.channel)
    assert await hub.show_display(clicked, hub.show_pet) is True
    shown = game_db.channel.messages[-1]                 # the pet with its own panel, for the clicker
    assert clicked.replies == [] and shown.embeds[0].title == "Pies Burek" and ui.is_live(shown.id)
    assert shown.button_labels[:2] == ["Zmień imię", "Stajnia"]
    assert (await game_db.click(shown, other, "pet:stable")).answers == [not_yours(player)]

    clicked = FakeInteraction(game_db.bot, player, channel=game_db.channel)
    assert await hub.show_display(clicked, hub.show_stable) is True
    stable = game_db.channel.messages[-1]                # the stable with its list, as $stajnia
    assert stable.embeds[0].title == "Stajnia" and ui.is_live(stable.id)
    await game_db.click(stable, player, "pet:stable-take", values=["3:7"])
    assert await slots(db, player) == [7, 6, 404, 5, 0] and game_db.channel.contains(pets.MSG_SWAPPED)

    # While a fight is typed on the channel the display buttons wait - silently.
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(challenge._rounds_running, game_db.channel.id, 1)
        before = len(game_db.channel.messages)
        clicked = FakeInteraction(game_db.bot, player, channel=game_db.channel)
        assert await hub.show_display(clicked, hub.show_stable) is False
        assert clicked.response.deferred and clicked.replies == []
        assert len(game_db.channel.messages) == before


# -- renaming ----------------------------------------------------------------------------

async def rename(game, panel, player, text: str | None):
    """Click ``Zmień imię`` and submit ``text`` in the pop-up window (None: close it)."""
    interaction, task = start_click(game, panel, player, "pet:rename")
    await until(lambda: interaction.response.modal is not None or task.done())
    if text is None:
        interaction.response.modal.stop()
        await asyncio.wait_for(task, 5)
        return interaction, None
    submitted = await game.submit_modal(interaction, text)
    await asyncio.wait_for(task, 5)
    return interaction, submitted


async def test_rename_in_a_pop_up_window(game_db, monkeypatch):
    db = game_db.bot.db
    player = await grown(game_db, pet_name="Burek", type="Cat")
    rig(monkeypatch, randint=0)
    panel = await show(game_db, player)
    assert panel.embeds[0].title == "Kot Burek"
    interaction, submitted = await rename(game_db, panel, player, "  Sir Puszek z Wielkiego Lasu ")
    modal = interaction.response.modal
    assert modal.title == "Nowe imię towarzysza" and modal.field.default == "Burek"
    assert modal.field.max_length == pets.MAX_NAME_LENGTH
    assert (await pet_row(db, 5))["pet_name"] == "Sir Puszek z Wielkiego Lasu"
    assert submitted.public == [f"<@{player.id}> Twój towarzysz ma teraz na imię **Sir Puszek z Wielkiego Lasu** "
                                "<:peepoBlush:984769061340737586>"]
    assert game_db.channel.messages[-1] is submitted.replies[0].message      # on the channel, after the pop-up
    assert panel.embeds[0].title == "Kot Sir Puszek z Wielkiego Lasu" and panel.button_labels[0] == "Zmień imię"

    _, submitted = await rename(game_db, panel, player, "   ")
    assert submitted.answers == [pets.MSG_RENAME_EMPTY]
    interaction, _ = await rename(game_db, panel, player, None)              # closed the window
    assert interaction.replies == []
    await rename(game_db, panel, player, "Reksio")
    assert (await pet_row(db, 5))["pet_name"] == "Reksio"                    # no cog: no cooldown here


async def test_rename_window_submitted_while_busy_or_after_the_pet_is_gone(game_db, monkeypatch):
    db = game_db.bot.db
    player = await grown(game_db, pet_name="Burek")
    rig(monkeypatch, randint=0)
    panel = await show(game_db, player)
    interaction, task = start_click(game_db, panel, player, "pet:rename")
    await until(lambda: interaction.response.modal is not None)
    with game_db.bot.state.hold_busy("event:TEST"):
        submitted = await game_db.submit_modal(interaction, "Reksio")
        await asyncio.wait_for(task, 5)
    assert submitted.answers == [ui.MSG_BUSY] and (await pet_row(db, 5))["pet_name"] == "Burek"

    await db.execute("UPDATE PETOWNER SET PET_ID = 0")
    interaction, submitted = await rename(game_db, panel, player, "Reksio")
    assert game_db.channel.messages[-1].content == pets.MSG_ALONE_MAD and submitted.replies == []
    assert submitted.response.deferred


async def test_rename_shares_the_cooldown_of_the_command(game_db, cog, monkeypatch):
    db = game_db.bot.db
    player = await grown(game_db, pet_name="Burek")
    rig(monkeypatch, randint=0)
    panel = await show(game_db, player)
    await rename(game_db, panel, player, "Reksio")
    await run(game_db, player, "$nazwij Azor")
    assert game_db.channel.messages[-1].content.startswith("Poczekaj na odnowienie komendy!")
    refused = await game_db.click(panel, player, "pet:rename")               # no window, a live countdown
    assert refused.response.modal is None and len(refused.public) == 1
    assert refused.public[0].startswith(f"<@{player.id}> Poczekaj na odnowienie komendy! Imię zmienisz ponownie <t:")
    assert game_db.channel.messages[-1] is refused.replies[0].message
    assert (await pet_row(db, 5))["pet_name"] == "Reksio"

    # The other way round, and a refused naming does not use the cooldown up.
    other = await grown(game_db, name="Ola", pet_id=6, pet_name="Mruczek")
    await db.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", other.id)
    await run(game_db, other, "$nazwij Filemon")                             # no pet: nothing named
    await db.execute("UPDATE PETOWNER SET PET_ID = 6 WHERE PLAYER_ID = $1", other.id)
    panel = await show(game_db, other)
    _, submitted = await rename(game_db, panel, other, "Filemon")
    assert submitted.answers[0].startswith("Twój towarzysz ma teraz na imię **Filemon**")
    assert (await game_db.click(panel, other, "pet:rename")).answers[0].startswith("Poczekaj na odnowienie")


async def test_typed_rename_takes_the_whole_rest_of_the_message(game_db, cog, monkeypatch):
    db = game_db.bot.db
    rig(monkeypatch, randint=0)
    names = {}
    for typed, expected in (("$nazwij Sir Burek Wielki", "Sir Burek Wielki"),
                            ('$n "Pan  Kotek"', "Pan  Kotek"),
                            ("$nazwij „Zjawa z Lasu”", "Zjawa z Lasu"),
                            ("$n O'Neil", "O'Neil")):
        player = game_db.add_member(f"Gracz{len(names)}")
        pet_id = 10 + len(names)
        await add_owner(db, player.id, pet_id)
        await add_pet(db, pet_id)
        await run(game_db, player, typed)
        names[typed] = (await pet_row(db, pet_id))["pet_name"]
        assert names[typed] == expected, typed

    player = game_db.add_member("Pusty")
    await add_owner(db, player.id, 30)
    await add_pet(db, 30)
    await run(game_db, player, '$nazwij ""')
    assert game_db.channel.messages[-1].content == "Po spacji podaj imię towarzysza np. **$nazwij Burek**."
    await run(game_db, player, "$nazwij Azor")                               # the cooldown was not used up
    assert (await pet_row(db, 30))["pet_name"] == "Azor"


async def open_rename_window(game, panel, player):
    """Click ``Zmień imię`` and leave the pop-up window open; returns (interaction, task)."""
    interaction, task = start_click(game, panel, player, "pet:rename")
    await until(lambda: interaction.response.modal is not None or task.done())
    assert interaction.response.modal is not None
    return interaction, task


async def test_rename_cooldown_holds_when_somebody_else_names_while_the_window_is_open(game_db, cog, monkeypatch):
    # discord.py drops a never-used cooldown from its cache whenever anybody's $nazwij
    # looks at the cooldowns: the window must not charge such an orphan.
    db = game_db.bot.db
    player = await grown(game_db, pet_name="Burek")
    other = await grown(game_db, name="Ola", pet_id=6, pet_name="Mruczek")
    rig(monkeypatch, randint=0)
    panel = await show(game_db, player)
    interaction, task = await open_rename_window(game_db, panel, player)
    await run(game_db, other, "$nazwij Filemon")
    assert (await pet_row(db, 6))["pet_name"] == "Filemon"
    submitted = await game_db.submit_modal(interaction, "Reksio")
    await asyncio.wait_for(task, 5)
    assert submitted.answers[0].startswith("Twój towarzysz ma teraz na imię **Reksio**")

    await run(game_db, player, "$nazwij Azor")                               # exactly one name per 23 h
    assert game_db.channel.messages[-1].content.startswith("Poczekaj na odnowienie komendy!")
    refused = await game_db.click(panel, player, "pet:rename")
    assert refused.response.modal is None and refused.answers[0].startswith("Poczekaj na odnowienie")
    assert (await pet_row(db, 5))["pet_name"] == "Reksio"


async def test_rename_cooldown_holds_when_the_player_types_nazwij_while_the_window_is_open(
        game_db, cog, monkeypatch):
    db = game_db.bot.db
    player = await grown(game_db, pet_name="Burek")
    rig(monkeypatch, randint=0)
    panel = await show(game_db, player)
    interaction, task = await open_rename_window(game_db, panel, player)
    await run(game_db, player, "$nazwij Azor")                               # a second device
    assert (await pet_row(db, 5))["pet_name"] == "Azor"
    submitted = await game_db.submit_modal(interaction, "Reksio")
    await asyncio.wait_for(task, 5)
    assert len(submitted.answers) == 1
    assert submitted.answers[0].startswith("Poczekaj na odnowienie komendy! Imię zmienisz ponownie <t:")
    assert (await pet_row(db, 5))["pet_name"] == "Azor"
    await run(game_db, player, "$nazwij Filemon")
    assert game_db.channel.messages[-1].content.startswith("Poczekaj na odnowienie komendy!")
    assert (await pet_row(db, 5))["pet_name"] == "Azor"


async def test_rename_picture_follows_a_pet_that_grew_meanwhile(game_db, monkeypatch):
    db = game_db.bot.db
    player = await grown(game_db, pet_name="Burek", type="Cat")                # level 2, variant 1: 11.png
    rig(monkeypatch, randint=0)
    panel = await show(game_db, player)
    assert panel.files == ["11.png"] and panel.embeds[0].image.url == "attachment://11.png"
    await db.execute("UPDATE PETS SET PET_LVL = 3 WHERE PET_ID = 5")          # it grew in a fight
    await rename(game_db, panel, player, "Reksio")
    assert [file.filename for file in panel.edits[-1]["attachments"]] == ["101.png"]
    assert panel.embeds[0].image.url == "attachment://101.png" and panel.embeds[0].title == "Kot Reksio"
    await rename(game_db, panel, player, "Azor")                             # the same picture: not sent again
    assert "attachments" not in panel.edits[-1] and panel.embeds[0].image.url == "attachment://101.png"


async def test_redraw_does_not_put_another_pet_on_the_panel(game_db):
    db = game_db.bot.db
    player = await stable_setup(game_db)
    panel = await show(game_db, player)
    await db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT3 = 5")
    assert await panel.view.redraw(game_db.bot) is False
    assert panel.view is None and panel.embeds[0].title == "Pies Burek"


# -- a panel acts only on the pet it shows -----------------------------------------------

async def two_pets(game):
    """A patron with pet 5 (Pies Burek, level 3) active and pet 7 (Kot Mruczek) in stable slot 3."""
    db = game.bot.db
    player = patron(game, "Ala")
    await add_owner(db, player.id, 5, pet_id_alt3=7, reroll_scroll=20, rebirth_stones=1, mirrors=1)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=3, pet_skills=3, crit_perc=4, type="Dog", variant="1")
    await add_pet(db, 7, pet_name="Mruczek", pet_lvl=3, pet_skills=5, crit_perc=9, type="Cat", variant="1")
    return player


async def test_stale_panel_cannot_discard_the_pet_taken_out_of_the_stable(game_db, sacrifices):
    # The reviewer's scenario: the panel's own "Stajnia", the list, then "Porzuć" under the old pet.
    db = game_db.bot.db
    player = await two_pets(game_db)
    panel = await show(game_db, player)
    await game_db.click(panel, player, "pet:stable")
    stable = game_db.channel.messages[-1]
    (mruczek,) = stable.button("pet:stable-take").options
    await game_db.click(stable, player, "pet:stable-take", values=[mruczek.value])
    assert await slots(db, player) == [7, 0, 0, 5, 0] and ui.is_live(panel.id)
    refused = await game_db.click(panel, player, "pet:discard")
    assert refused.public == [f"<@{player.id}> {pets.MSG_OTHER_PET}"]
    assert game_db.channel.messages[-1] is refused.replies[0].message
    assert pets.MSG_OTHER_PET == ("Ten panel pokazuje innego towarzysza - wpisz **$towarzysz**, "
                                  "żeby zobaczyć aktualnego.")
    assert panel.view is None and not ui.is_live(panel.id)
    assert not game_db.channel.ever_sent(QUESTION) and sacrifices == []
    assert await pet_row(db, 7) is not None and await pet_row(db, 5) is not None


@pytest.mark.parametrize("key", ["pet:rename", "pet:reroll", "pet:adv-reroll", "pet:enlight", "pet:transform",
                                 "pet:discard"])
async def test_every_pet_button_of_a_stale_panel_is_refused(game_db, cog, sacrifices, key):
    db = game_db.bot.db
    player = await two_pets(game_db)
    panel = await show(game_db, player)
    assert panel.button(key) is not None
    await db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT3 = 5")      # $wyciagnij 3 meanwhile
    before = (await owner_row(db, player.id), await pet_row(db, 5), await pet_row(db, 7))
    refused, task = start_click(game_db, panel, player, key)
    await until(lambda: task.done() or refused.response.modal is not None)
    assert refused.response.modal is None
    await asyncio.wait_for(task, 5)
    assert refused.answers == [pets.MSG_OTHER_PET]
    assert panel.view is None and not game_db.bot.state.busy
    assert not game_db.channel.ever_sent(QUESTION) and not game_db.channel.ever_sent(pets.MSG_ADV_REROLL_MENU)
    assert (await owner_row(db, player.id), await pet_row(db, 5), await pet_row(db, 7)) == before
    assert sacrifices == []


@pytest.mark.parametrize("key", ["pet:reroll", "pet:enlight", "pet:transform", "pet:discard"])
async def test_pet_changed_while_the_question_was_open(game_db, monkeypatch, sacrifices, key):
    db = game_db.bot.db
    player = await two_pets(game_db)
    rig(monkeypatch, choices=[PetType.BEAR], randint=1)
    panel = await show(game_db, player)
    clicking = game_db.click_later(panel, player, key)
    question = await game_db.wait_for_buttons("yes")
    await db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT3 = 5")      # swapped behind the question
    before = (await owner_row(db, player.id), await pet_row(db, 5), await pet_row(db, 7))
    await game_db.click(question, player, "yes")
    await asyncio.wait_for(clicking, 5)
    assert game_db.channel.messages[-1].content == pets.MSG_OTHER_PET
    assert (await owner_row(db, player.id), await pet_row(db, 5), await pet_row(db, 7)) == before
    assert sacrifices == []


async def test_typed_commands_still_mean_the_active_pet(game_db, monkeypatch, sacrifices):
    db = game_db.bot.db
    player = await two_pets(game_db)
    await db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT3 = 5")
    game_db.auto_click(player, "yes", times=None)
    rig(monkeypatch, randint=1)
    assert await pets.reroll_pet(game_db.scene(player), player) is True
    assert (await owner_row(db, player.id))["reroll_scroll"] == 19 and (await pet_row(db, 7))["crit_perc"] != 9
    assert await pets.discard_pet(game_db.scene(player)) is True
    assert await pet_row(db, 7) is None and await pet_row(db, 5) is not None


async def test_stale_rename_window_names_nothing(game_db, cog, monkeypatch):
    db = game_db.bot.db
    player = await two_pets(game_db)
    rig(monkeypatch, randint=0)
    panel = await show(game_db, player)
    interaction, task = await open_rename_window(game_db, panel, player)
    assert interaction.response.modal.field.default == "Burek"
    await db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT3 = 5")
    submitted = await game_db.submit_modal(interaction, "Reksio")
    await asyncio.wait_for(task, 5)
    assert submitted.answers == [pets.MSG_OTHER_PET] and panel.view is None
    assert (await pet_row(db, 5))["pet_name"] == "Burek" and (await pet_row(db, 7))["pet_name"] == "Mruczek"
    await run(game_db, player, "$nazwij Filemon")                            # the cooldown was not used up
    assert (await pet_row(db, 7))["pet_name"] == "Filemon"


# -- a stale stable list ------------------------------------------------------------------

async def test_stale_stable_list_takes_out_only_the_pet_it_names(game_db):
    db = game_db.bot.db
    player = await stable_setup(game_db)
    await pets.check_stable(game_db.scene(player))
    stable = game_db.channel.messages[-1]
    await pets.unstore_pet(game_db.scene(player), 3)                         # typed: Mruczek comes out
    assert await slots(db, player) == [7, 6, 404, 5, 0]

    chosen = await game_db.click(stable, player, "pet:stable-take", values=["3:7"])   # still "Kot Mruczek"
    assert chosen.public == [f"<@{player.id}> {pets.MSG_STABLE_CHANGED}"]
    assert game_db.channel.messages[-1] is chosen.replies[0].message
    assert pets.MSG_STABLE_CHANGED == "Stajnia zmieniła się w międzyczasie - wybierz jeszcze raz."
    assert await slots(db, player) == [7, 6, 404, 5, 0]                       # nothing swapped back
    assert [(o.value, o.label) for o in stable.button("pet:stable-take").options] == [
        ("1:6", "Towarzysz · jajko"), ("3:5", "Burek · Pies · poziom 3")]    # redrawn

    assert (await game_db.click(stable, player, "pet:stable-take", values=["3"])).answers == [
        pets.MSG_STABLE_CHANGED]                                              # no pet in the value
    chosen = await game_db.click(stable, player, "pet:stable-take", values=["3:5"])
    assert chosen.replies == [] and await slots(db, player) == [5, 6, 404, 7, 0]
    assert game_db.channel.messages[-1].content == pets.MSG_SWAPPED
