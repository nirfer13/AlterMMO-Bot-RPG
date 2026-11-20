from __future__ import annotations

from altermmo.features import expsum, patrons


async def test_patron_detection_and_random_pick(game):
    plain = game.add_member("Zwykly")
    crafter = game.add_member("Crafter", roles=[patrons.CRAFTER_ROLE])
    patron = game.add_member("Patron", roles=[patrons.PATRON_ROLES[2]])
    assert not patrons.check_if_patron(plain)
    assert patrons.check_if_patron(crafter) and patrons.check_if_patron(patron)
    assert patrons.get_patron(game.bot) in (crafter, patron)


async def test_get_patron_without_patrons_returns_none(game):
    assert patrons.get_patron(game.bot) is None


async def test_exp_summary_accumulates_and_resets(game):
    scene = game.scene()
    await expsum.show_file_exp(scene)
    assert game.channel.contains(expsum.EMPTY_SUMMARY)
    expsum.add_file_exp(game.bot, 1, 500)
    expsum.add_file_exp(game.bot, "1", 1500)
    expsum.add_file_exp(game.bot, 2, 100)
    await expsum.show_file_exp(scene)
    assert game.channel.messages[-1].content == "<@1>: 2000\n<@2>: 100"
    expsum.init_file_exp(game.bot)
    assert game.bot.store.exp_summary.load() == {}


async def test_exp_command_only_shows_the_summary(game):
    """Horus collects the file by itself: $Exp must not throw away what it has not picked up yet."""
    from types import SimpleNamespace

    from altermmo.cogs.admin import AdminCog

    cog = AdminCog(game.bot)
    alter = game.add_member("Alter")
    expsum.add_file_exp(game.bot, 7, 250)
    ctx = SimpleNamespace(bot=game.bot, channel=game.channel, author=alter, message=game.say(alter, "$Exp"))
    await cog.exp_summary.callback(cog, ctx)
    assert game.channel.messages[-1].content == "<@7>: 250"
    assert game.bot.store.exp_summary.load() == {"7": 250}                # still there for Horus


async def test_exp_summary_reads_legacy_file(game):
    game.bot.store.exp_summary.path.parent.mkdir(parents=True, exist_ok=True)
    game.bot.store.exp_summary.path.write_text('{"1234212213": 0, "5": 300}', encoding="utf-8")
    expsum.add_file_exp(game.bot, 5, 200)
    assert game.bot.store.exp_summary.load()["5"] == 500
