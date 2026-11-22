"""Owner request of 2026-11-21: the private boss warning can switch itself off (a button in the DM).

"Pet gdy w prywatnej wiadomości ostrzega przed bossem powinien też dawać możliwość anulowania
takich powiadomień."  No command works in private messages, so the warning carries a button
(its own custom_id) and a line naming ``$wyczuwanie`` on the boss channel.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest_asyncio

from altermmo import settings as settings_module
from altermmo.features import bossdetect, pets

from fakes import FakeInteraction, open_dm
from test_owner_requests import PET_DEFAULTS, _insert, add_owner


def closed_dms():
    async def closed(content=None, **kwargs):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
    return closed


@pytest_asyncio.fixture
async def sensing(game_db):
    """Ala, Ola and Ela own pets that sense bosses; their private messages are kept (``open_dm``)."""
    db, bot = game_db.bot.db, game_db.bot
    members = [game_db.add_member(name) for name in ("Ala", "Ola", "Ela")]
    for pet_id, member in enumerate(members, start=1):
        await add_owner(db, member.id, pet_id)
        await _insert(db, "PETS", {"pet_id": pet_id, **PET_DEFAULTS})
    dms = [open_dm(member, bot) for member in members]
    bot.debug = False                                        # hints go to the notice channel
    bossdetect.register(bot)                                 # what the cog does at start-up
    return SimpleNamespace(members=members, dms=dms)


def expected_warning(bot, word: str) -> str:
    return (f"Nadciąga boss! Będzie {word}.\nNie chcesz tych powiadomień? Kliknij przycisk poniżej albo "
            f"wpisz **$wyczuwanie** na kanale <#{bot.ids.boss_channel}>.")


# -- the warning ---------------------------------------------------------------------------

async def test_warning_has_the_button_and_says_how_to_stop_it(game_db, sensing):
    bot = game_db.bot
    ala, _, _ = sensing.members
    await pets.detect_boss(bot, 2)
    (warning,) = sensing.dms[0].messages
    assert warning.content == expected_warning(bot, "epicki") and ala.dms == [warning.content]
    assert warning.content.split("\n")[0] == "Nadciąga boss! Będzie epicki."      # the line players know
    assert f"<#{bot.ids.boss_channel}>" in warning.content and bot.ids.boss_channel == 970684202880204831
    (button,) = warning.view.children
    assert button.custom_id == bossdetect.DM_BUTTON_ID == "altermmo:boss-detect-off-dm"
    assert button.custom_id != bossdetect.BUTTON_ID
    assert button.label == "Wyłącz powiadomienia o bossie" and str(button.emoji) == "🔕"
    assert warning.view.timeout is None and warning.view.is_persistent()
    assert warning.view.is_finished()                        # not kept by discord.py per warning
    assert game_db.notice_channel.messages == [] and game_db.log_channel.messages == []


async def test_debug_mode_links_the_test_channel(game_db, sensing, monkeypatch):
    bot = game_db.bot
    debug_ids = settings_module._DEBUG_IDS
    monkeypatch.setattr(bot, "ids", debug_ids)
    await pets.detect_boss(bot, 1)
    (warning,) = sensing.dms[1].messages
    assert f"<#{debug_ids.boss_channel}>" in warning.content and str(970684202880204831) not in warning.content
    click = await game_db.click(warning, sensing.members[1])
    assert f"<#{debug_ids.boss_channel}>" in click.replies[0].content


# -- the click -------------------------------------------------------------------------------

async def test_click_in_the_dm_switches_the_warnings_off_and_takes_the_button_away(game_db, sensing, monkeypatch):
    bot = game_db.bot
    ala, ola, _ = sensing.members
    await pets.detect_boss(bot, 2)
    (warning,) = sensing.dms[1].messages

    acknowledged_before_saving = []
    add = bot.store.detect_off.add

    def watching(player_id):
        acknowledged_before_saving.append(click.response.is_done())
        return add(player_id)

    monkeypatch.setattr(bot.store.detect_off, "add", watching)
    click = FakeInteraction(bot, ola, warning)
    assert click.guild is None
    view = next(view for view in bot.persistent_views if isinstance(view, bossdetect.DetectionOffDmView))
    await view.children[0].callback(click)                   # as discord.py: the view registered at start-up

    assert acknowledged_before_saving == [True]              # answered within Discord's 3 s whatever the disk does
    (answer,) = click.replies
    assert answer.content == bossdetect.MSG_DM_SWITCHED_OFF.format(channel_id=bot.ids.boss_channel)
    assert not answer.content.startswith("<@") and f"<@{ola.id}>" not in answer.content   # one reader: no mention
    assert "**$wyczuwanie**" in answer.content and f"<#{bot.ids.boss_channel}>" in answer.content
    assert not answer.ephemeral and answer.allowed_mentions.users is False
    assert sensing.dms[1].messages == [warning, answer.message]   # an ordinary message in that conversation
    assert bossdetect.is_off(bot, ola.id) and bot.store.detect_off.all() == [str(ola.id)]
    assert warning.view is None and warning.edits == [{"view": None}]   # the warning keeps its text
    assert warning.content == expected_warning(bot, "epicki")

    await pets.detect_boss(bot, 2)                           # the next boss: Ola is not warned, the others are
    assert len(ola.dms) == 1 and len(ala.dms) == 2 and len(sensing.members[2].dms) == 2


async def test_second_click_on_another_old_warning_answers_the_same(game_db, sensing):
    bot = game_db.bot
    ola = sensing.members[1]
    await pets.detect_boss(bot, 1)
    await pets.detect_boss(bot, 3)
    first, second = sensing.dms[1].messages
    one = await game_db.click(first, ola)
    two = await game_db.click(second, ola)                   # already off: no error, the same answer
    text = bossdetect.MSG_DM_SWITCHED_OFF.format(channel_id=bot.ids.boss_channel)
    assert one.answers == two.answers == [text]
    assert bot.store.detect_off.all() == [str(ola.id)]
    assert first.view is None and second.view is None


async def test_click_is_answered_when_the_switch_cannot_be_saved(game_db, sensing, monkeypatch):
    bot = game_db.bot
    ola = sensing.members[1]
    await pets.detect_boss(bot, 2)
    (warning,) = sensing.dms[1].messages

    def broken(player_id):
        raise OSError("disk full")

    monkeypatch.setattr(bot.store.detect_off, "add", broken)
    click = await game_db.click(warning, ola)
    assert click.answers == [bossdetect.MSG_DM_NOT_SAVED.format(channel_id=bot.ids.boss_channel)]
    assert any("powiadomień o bossie" in line and str(ola.id) in line for line in bot.owner_log)
    assert warning.edits == [] and not bossdetect.is_off(bot, ola.id)   # the button stays: try again


async def test_answer_and_lost_edit_never_break_the_click(game_db, sensing):
    bot = game_db.bot
    ola = sensing.members[1]
    await pets.detect_boss(bot, 2)
    (warning,) = sensing.dms[1].messages
    await warning.delete()                                   # the player deleted the conversation meanwhile
    click = FakeInteraction(bot, ola, warning)
    click.response.fail_with = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "down")
    view = next(view for view in bot.persistent_views if isinstance(view, bossdetect.DetectionOffDmView))
    await view.children[0].callback(click)                   # must not raise
    assert bossdetect.is_off(bot, ola.id)


async def test_dm_button_clicked_on_a_server_channel_is_refused(game_db, sensing):
    bot = game_db.bot
    ola = sensing.members[1]
    message = await game_db.channel.send("cokolwiek", view=bossdetect.DetectionOffDmView())
    click = await game_db.click(message, ola)
    assert click.public == [f"<@{ola.id}> {bossdetect.MSG_DM_ONLY}"]
    assert not bossdetect.is_off(bot, ola.id) and bot.store.detect_off.all() == []
    assert message.edits == [] and not message.deleted


async def test_public_hint_button_still_works_only_for_the_hinted_player(game_db, sensing):
    bot = game_db.bot
    ala, ola, _ = sensing.members
    ala.send = closed_dms()
    await pets.detect_boss(bot, 2)
    (hint,) = game_db.notice_channel.messages
    assert hint.button().custom_id == bossdetect.BUTTON_ID
    stranger = await game_db.click(hint, ola)
    assert stranger.answers == [bossdetect.MSG_NOT_YOURS.format(player_id=ala.id)]
    assert not bossdetect.is_off(bot, ola.id) and not bossdetect.is_off(bot, ala.id)
    own = await game_db.click(hint, ala)
    assert own.public == [f"<@{ala.id}> {bossdetect.MSG_SWITCHED_OFF}"] and hint.deleted
    assert bossdetect.is_off(bot, ala.id)


async def test_both_buttons_are_registered_as_persistent(game_db, monkeypatch):
    bot = game_db.bot
    await bot.load_extension("altermmo.cogs.pets")
    kinds = {type(view) for view in bot.persistent_views}
    assert {bossdetect.DetectionOffView, bossdetect.DetectionOffDmView} <= kinds
    ids = {child.custom_id for view in bot.persistent_views for child in view.children}
    assert {bossdetect.BUTTON_ID, bossdetect.DM_BUTTON_ID} <= ids


async def test_one_view_that_cannot_be_registered_does_not_take_the_other(game, monkeypatch):
    bot = game.bot
    registered = []

    def add_view(view, **kwargs):
        if isinstance(view, bossdetect.DetectionOffView):
            raise RuntimeError("boom")
        registered.append(view)

    monkeypatch.setattr(bot, "add_view", add_view)
    bossdetect.register(bot)                                 # must not raise
    assert [type(view) for view in registered] == [bossdetect.DetectionOffDmView]


# -- detect_boss keeps its rules ------------------------------------------------------------

async def test_closed_dms_still_get_the_public_hint(game_db, sensing):
    bot = game_db.bot
    ala, ola, _ = sensing.members
    ala.send = closed_dms()
    await pets.detect_boss(bot, 2)
    assert [m.content for m in game_db.notice_channel.messages] == [
        bossdetect.MSG_UNLOCK_HINT.format(player_id=ala.id)]
    assert ola.dms == [expected_warning(bot, "epicki")]


async def test_transient_dm_failure_gives_no_hint_and_is_retried_once_as_before(game_db, sensing):
    bot = game_db.bot
    ala, ola, _ = sensing.members
    sensing.dms[0].fail_with, sensing.dms[0].fail_sends = ConnectionError("network down"), 2
    await pets.detect_boss(bot, 1)
    assert sensing.dms[0].fail_sends == 0 and ala.dms == []  # two attempts (today's), then given up
    assert game_db.notice_channel.messages == [] and game_db.log_channel.messages == []
    assert ola.dms == [expected_warning(bot, "rzadki")]

    sensing.dms[0].fail_with, sensing.dms[0].fail_sends = ConnectionError("blip"), 1
    await pets.detect_boss(bot, 1)                           # one blip: the second attempt delivers it once
    assert ala.dms == [expected_warning(bot, "rzadki")]


async def test_switched_off_players_get_no_warning(game_db, sensing):
    bot = game_db.bot
    ala, ola, ela = sensing.members
    bossdetect.switch_off(bot, ola.id)
    await pets.detect_boss(bot, 0)
    assert ola.dms == [] and sensing.dms[1].messages == []
    assert ala.dms == ela.dms == [expected_warning(bot, "zwykły")]


async def test_one_players_failing_warning_does_not_stop_the_others(game_db, sensing, monkeypatch):
    bot = game_db.bot
    ala, ola, ela = sensing.members
    real = pets._warn_about_boss

    async def warn(bot_, player_id, text):
        if player_id == ola.id:
            raise RuntimeError("something unexpected")
        await real(bot_, player_id, text)

    monkeypatch.setattr(pets, "_warn_about_boss", warn)
    await pets.detect_boss(bot, 2)                           # must not raise
    assert ala.dms == ela.dms == [expected_warning(bot, "epicki")] and ola.dms == []


async def test_warning_goes_without_the_button_when_the_button_is_the_problem(game_db, sensing, monkeypatch):
    bot = game_db.bot
    ala, ola, ela = sensing.members
    refused = discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"),
                                    {"code": 50035, "message": "Invalid Form Body"})
    dm = sensing.dms[1]
    send = ola.send

    async def picky(content=None, **kwargs):
        if kwargs.get("view") is not None:
            raise refused
        return await send(content, **kwargs)

    ola.send = picky
    await pets.detect_boss(bot, 2)
    (warning,) = dm.messages
    assert warning.content == expected_warning(bot, "epicki") and warning.view is None
    assert game_db.notice_channel.messages == []            # her private messages are open: no hint

    def unbuildable():
        raise RuntimeError("no view today")

    monkeypatch.setattr(bossdetect, "DetectionOffDmView", unbuildable)
    await pets.detect_boss(bot, 2)                           # the view cannot even be made: warned anyway
    assert len(ala.dms) == len(ela.dms) == 2 and sensing.dms[0].messages[-1].view is None
