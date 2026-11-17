"""$kradnijxp: the victim stays secret, the right is spent exactly once."""

from __future__ import annotations

from altermmo.features import xpsteal


def _exp(game):
    return game.bot.store.exp_summary.load()


async def test_theft_hides_the_victim_and_books_the_transfer(game):
    thief = game.add_member("Zlodziej")
    victim = game.add_member("Ofiara")
    xpsteal.grant(game.bot, thief.id, 2500)
    scene = game.scene(thief, "$kradnijxp Ofiara")
    command_message = scene.message

    assert await xpsteal.steal(scene, "Ofiara") is True

    assert command_message.deleted and command_message not in game.channel.messages
    confirmation = game.channel.messages[-1].content
    assert f"<@{thief.id}>" in confirmation and "2500" in confirmation
    assert "Ofiara" not in game.channel.transcript and str(victim.id) not in game.channel.transcript
    assert _exp(game) == {str(thief.id): 2500, str(victim.id): -2500}
    assert any("Ofiara" in line and "2500" in line for line in game.bot.owner_log)
    assert xpsteal.pending(game.bot, thief.id) == []


async def test_right_is_needed_and_spent_once(game):
    thief = game.add_member("Zlodziej")
    victim = game.add_member("Ofiara")
    assert await xpsteal.steal(game.scene(thief), "Ofiara") is False
    assert game.channel.contains("nie masz żadnej kradzieży")
    xpsteal.grant(game.bot, thief.id, 2500)
    assert await xpsteal.steal(game.scene(thief), f"<@{victim.id}>") is True
    assert await xpsteal.steal(game.scene(thief), f"<@{victim.id}>") is False
    assert _exp(game)[str(victim.id)] == -2500


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
    assert _exp(game) == {}
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


async def test_several_rights_stack(game):
    thief = game.add_member("Zlodziej")
    victim = game.add_member("Ofiara")
    xpsteal.grant(game.bot, thief.id, 2500)
    xpsteal.grant(game.bot, thief.id, 1000)
    assert await xpsteal.steal(game.scene(thief), "Ofiara")
    assert xpsteal.pending(game.bot, thief.id) == [1000]
    assert await xpsteal.steal(game.scene(thief), "Ofiara")
    assert _exp(game) == {str(thief.id): 3500, str(victim.id): -3500}
