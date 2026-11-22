"""$kradnijxp: the victim stays secret, the right is spent exactly once, Horus settles the theft.

The request files, every failure point and the results are tested in test_xp_transfer.py.
"""

from __future__ import annotations

from fakes import xp_transfer_requests

from altermmo.features import xpsteal


def _exp(game):
    return game.bot.store.exp_summary.load()


async def test_theft_hides_the_victim_and_hands_the_transfer_to_horus(game):
    thief = game.add_member("Zlodziej")
    victim = game.add_member("Ofiara")
    xpsteal.grant(game.bot, thief.id, 2500)
    scene = game.scene(thief, "$kradnijxp Ofiara")
    command_message = scene.message

    assert await xpsteal.steal(scene, "Ofiara") is True

    assert command_message.deleted and command_message not in game.channel.messages
    confirmation = game.channel.messages[-1].content
    assert confirmation == xpsteal.MSG_DONE.format(thief=thief.id, amount=xpsteal.number(2500))
    assert f"<@{thief.id}>" in confirmation and "2" + chr(0xA0) + "500" in confirmation
    assert "tyle, ile ona ma" in confirmation and "Wynik pojawi się tutaj" in confirmation
    assert "Ofiara" not in game.channel.transcript and str(victim.id) not in game.channel.transcript
    (request,) = xp_transfer_requests(game.bot)
    assert (request["from"], request["to"], request["amount"]) == (str(victim.id), str(thief.id), 2500)
    # Owner's decision of 2026-11-22: the theft no longer goes through the experience summary.
    assert _exp(game) == {}
    assert any("Ofiara" in line and f"do {xpsteal.number(2500)} expa" in line and request["id"] in line
               for line in game.bot.owner_log)
    # Nothing to do by hand: no ping.
    assert not any(f"<@{game.bot.ids.owner}>!" in m.content for m in game.log_channel.messages)
    assert xpsteal.pending(game.bot, thief.id) == []


async def test_right_is_needed_and_spent_once(game):
    thief = game.add_member("Zlodziej")
    victim = game.add_member("Ofiara")
    assert await xpsteal.steal(game.scene(thief), "Ofiara") is False
    assert game.channel.contains("nie masz żadnej kradzieży")
    xpsteal.grant(game.bot, thief.id, 2500)
    assert await xpsteal.steal(game.scene(thief), f"<@{victim.id}>") is True
    assert await xpsteal.steal(game.scene(thief), f"<@{victim.id}>") is False
    assert [request["amount"] for request in xp_transfer_requests(game.bot)] == [2500]
    assert _exp(game) == {}


async def test_bad_targets_do_not_consume_the_right_or_leak_names(game):
    thief = game.add_member("Zlodziej")
    game.add_member("Jan")
    game.add_member("jan")
    xpsteal.grant(game.bot, thief.id, 2500)
    for text in (None, "  ", "NieMaTakiego", "Jan", "Zlodziej", str(game.bot.fake_user.id)):
        scene = game.scene(thief, f"$kradnijxp {text}")
        assert await xpsteal.steal(scene, text) is False
        assert scene.message.deleted
    assert xpsteal.pending(game.bot, thief.id) == [2500]
    assert xp_transfer_requests(game.bot) == [] and _exp(game) == {}
    assert "NieMaTakiego" not in game.channel.transcript


async def test_target_lookup_by_mention_id_and_names(game):
    victim = game.add_member("Ofiara")
    victim.display_name = "Wielka Ofiara"
    assert xpsteal.find_members(game.guild, f"<@{victim.id}>") == [victim]
    assert xpsteal.find_members(game.guild, f"<@!{victim.id}>") == [victim]
    assert xpsteal.find_members(game.guild, str(victim.id)) == [victim]
    assert xpsteal.find_members(game.guild, "ofiara") == [victim]
    assert xpsteal.find_members(game.guild, '"wielka ofiara"') == [victim]
    assert xpsteal.find_members(game.guild, "@Ofiara") == [victim]
    assert xpsteal.find_members(game.guild, "ktos") == []


def test_amount_is_read_from_the_loot_description():
    text = "Ukradnij wybranemu użytkownikowi 2500 doświadczenia na Discordzie"
    assert xpsteal.amount_from_description(text) == 2500
    assert xpsteal.amount_from_description("Ukradnij komuś doświadczenie") == xpsteal.DEFAULT_AMOUNT


def test_the_real_drop_steals_ten_thousand():
    """Owner's decision of 2026-11-22 (it was 2500): the drop of the real loot table and the default agree."""
    import json

    from altermmo.settings import CONFIG_DIR

    config = json.loads((CONFIG_DIR / "lootConfig.json").read_text(encoding="utf-8"))
    thefts = [item for table in config.values() for item in table if item["id"] == xpsteal.LOOT_ID]
    assert thefts, "the theft drop is gone from the loot table"
    assert {xpsteal.amount_from_description(item["descr"]) for item in thefts} == {10000}
    assert xpsteal.DEFAULT_AMOUNT == 10000


def test_the_how_to_texts_say_the_victim_gives_what_it_has():
    """Owner's decision of 2026-11-22: "do 10 000 doświadczenia - tyle, ile ofiara ma"."""
    assert "tyle, ile ofiara ma" in xpsteal.COMMAND_HINT
    shown = xpsteal.number(10000)
    assert f"kradzież do 10{chr(0xA0)}000 doświadczenia" in xpsteal.MSG_HOW_TO.format(thief=1, amount=shown)
    assert "tyle, ile ofiara ma" in xpsteal.MSG_HOW_TO
    assert f"kradzież do 10{chr(0xA0)}000 doświadczenia" in xpsteal.DM_HOW_TO.format(amount=shown, channel=1)
    assert "tyle, ile ofiara ma" in xpsteal.DM_HOW_TO
    assert xpsteal.number(10000) == "10" + chr(0xA0) + "000"


def test_no_text_tells_the_thief_to_tag_the_victim():
    """A mention pushes a notification to the victim (with the author and the text) the moment the
    message is created - deleting it afterwards cannot take that back.  So no text may suggest
    tagging the victim; they offer the nick or the ID and warn against tagging."""
    texts = {
        "COMMAND_HINT": xpsteal.COMMAND_HINT,
        "MSG_HOW_TO": xpsteal.MSG_HOW_TO.format(thief=1, amount="10"),
        "DM_HOW_TO": xpsteal.DM_HOW_TO.format(amount="10", channel=1),
        "MSG_NO_TARGET": xpsteal.MSG_NO_TARGET.format(thief=1),
        "MSG_NOT_FOUND": xpsteal.MSG_NOT_FOUND.format(thief=1),
        "MSG_AMBIGUOUS": xpsteal.MSG_AMBIGUOUS.format(thief=1),
    }
    for name, text in texts.items():
        lowered = text.casefold()
        for advice in ("albo oznacz", "oznacz ofiarę", "możesz też oznaczyć", "(@nick)"):
            assert advice not in lowered, (name, advice)
    for name in ("MSG_HOW_TO", "DM_HOW_TO", "MSG_NO_TARGET", "MSG_NOT_FOUND", "MSG_AMBIGUOUS"):
        assert "ID" in texts[name], name
    for name in ("MSG_HOW_TO", "DM_HOW_TO", "MSG_NO_TARGET"):
        assert "nie oznaczaj" in texts[name].casefold() and "powiadomienie" in texts[name], name


async def test_several_rights_stack(game):
    thief = game.add_member("Zlodziej")
    game.add_member("Ofiara")
    xpsteal.grant(game.bot, thief.id, 2500)
    xpsteal.grant(game.bot, thief.id, 1000)
    assert await xpsteal.steal(game.scene(thief), "Ofiara")
    assert xpsteal.pending(game.bot, thief.id) == [1000]
    assert await xpsteal.steal(game.scene(thief), "Ofiara")
    requests = xp_transfer_requests(game.bot)
    assert sorted(request["amount"] for request in requests) == [1000, 2500]
    assert len({request["id"] for request in requests}) == 2
    assert _exp(game) == {}


async def test_a_broken_entry_in_front_does_not_block_the_theft(game):
    thief = game.add_member("Zlodziej")
    game.add_member("Ofiara")
    game.bot.store.xp_thefts.save({str(thief.id): ["x", None, 2500]})
    assert await xpsteal.steal(game.scene(thief), "Ofiara") is True
    assert [request["amount"] for request in xp_transfer_requests(game.bot)] == [2500]
    assert xpsteal.pending(game.bot, thief.id) == [] and game.bot.store.xp_thefts.load() == {}
