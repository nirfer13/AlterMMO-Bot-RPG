"""The pinned "Panel gry", the help text and how the four areas fit together."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import re
import time
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from discord.ext import commands

from altermmo import bot as bot_module
from altermmo.cogs import help as help_cog
from altermmo.core import guards, io, ui
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossPhase
from altermmo.features import daily, hub, pets, skills, tower

from fakes import FakeInteraction, FakeMessage
from test_pets import add_owner, add_pet

LABELS = ["Polowanie", "Wieża", "Towarzysz", "Stajnia", "Klejnoty", "Rankingi", "Pomoc"]
#: Typed words the help mentions that are not commands (answers inside a flow).
TYPED_WORDS = {"potwierdzam", "tak"}
#: Player commands the help leaves out on purpose.
NOT_IN_HELP = {"ping", "pomoc"}


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(guards, "_last_notice", {})
    daily._gathering.clear()
    yield
    daily._gathering.clear()


@pytest.fixture
def pins(monkeypatch):
    """Fake messages learn to be pinned (Discord's ``Message.pin`` / ``unpin``)."""
    calls: list = []

    async def pin(self, *, reason=None):
        self.pinned = True
        calls.append(("pin", self.id))

    async def unpin(self, *, reason=None):
        self.pinned = False
        calls.append(("unpin", self.id))

    monkeypatch.setattr(FakeMessage, "pin", pin, raising=False)
    monkeypatch.setattr(FakeMessage, "unpin", unpin, raising=False)
    return calls


@pytest_asyncio.fixture
async def cog(game, pins):
    await game.bot.load_extension("altermmo.cogs.hub")
    for channel in game.bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    return game.bot.get_cog("Hub")


async def run(game, member, content: str, *, channel=None) -> FakeMessage:
    """``member`` types a command; it goes through discord.py's real command machinery."""
    channel = channel or game.channel
    message = FakeMessage(channel, member, content)
    message.edited_at = None
    message._state = None
    channel.messages.append(message)
    await game.bot.invoke(await game.bot.get_context(message))
    await game.settle()
    return message


async def panel_on(game, channel=None) -> FakeMessage:
    message, pinned = await hub.post_panel(game.bot, channel or game.channel)
    assert pinned
    return message


def dead_hunter(game, name: str = "Jan"):
    game.channel.silenced_roles.add(game.bot.ids.dead_hunter_role)
    return game.add_member(name, roles=[game.bot.ids.dead_hunter_role])


def stamp_of(text: str) -> int:
    return int(re.search(r"<t:(\d+):R>", text).group(1))


@pytest.fixture
def fake_hunt(monkeypatch, game):
    """``$polowanie``'s hunt without monsters: records who hunted and who held the channel."""
    hunts: list = []

    async def daily_hunt(scene):
        daily.save_daily_to_file(game.bot, scene.author.id)             # as the real hunt does first
        hunts.append((scene.author, game.bot.state.busy_owner))

    monkeypatch.setattr(daily, "daily_hunt", daily_hunt)
    return hunts


@pytest.fixture
def fake_tower(monkeypatch, game):
    runs: list = []

    async def explore_tower(scene):
        tower.save_weekly_tower_to_file(game.bot, scene.author.id)      # as the real run does first
        runs.append((scene.author, game.bot.state.busy_owner))

    monkeypatch.setattr(tower, "explore_tower", explore_tower)
    return runs


# -- loading everything together ------------------------------------------------------------

async def test_every_extension_loads_and_permanent_buttons_never_share_an_id(game, monkeypatch):
    bot = game.bot
    monkeypatch.setattr(bot.supervisor, "start", lambda *args, **kwargs: None)  # no background loops
    assert "altermmo.cogs.hub" in bot_module.EXTENSIONS
    for extension in bot_module.EXTENSIONS:
        await bot.load_extension(extension)                                 # raises when one breaks
    assert set(bot.extensions) == set(bot_module.EXTENSIONS)
    assert len(bot.cogs) == len(bot_module.EXTENSIONS)

    ids = [item.custom_id for view in bot.persistent_views for item in view.children]
    duplicated = [custom_id for custom_id, n in collections.Counter(ids).items() if n > 1]
    assert duplicated == []
    assert all(custom_id.startswith(ui.PREFIX) and not custom_id.startswith(ui.FLOW_PREFIX)
               for custom_id in ids)
    assert {"altermmo:boss:attack", "altermmo:events:chest", "altermmo:events:pray",
            "altermmo:boss-detect-off"} <= set(ids)
    assert {hub.HUNT_ID, hub.TOWER_ID, hub.PET_ID, hub.STABLE_ID, hub.GEMS_ID, hub.RANKINGS_ID,
            hub.HELP_ID} <= set(ids)
    # Every permanent view is stateless and never times out.
    assert all(view.timeout is None for view in bot.persistent_views)


async def test_every_player_command_is_in_the_help_and_every_mentioned_one_exists(game, monkeypatch):
    bot = game.bot
    monkeypatch.setattr(bot.supervisor, "start", lambda *args, **kwargs: None)
    for extension in bot_module.EXTENSIONS:
        await bot.load_extension(extension)

    def admin_only(command) -> bool:
        return command.cog_name == "Admin" or any(
            "has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)

    mentioned = {word.lower() for word in re.findall(r"\$(\w+)", help_cog.HELP_TEXT)}
    names = {}
    for command in bot.walk_commands():
        for name in (command.name, *command.aliases):
            names[name.lower()] = command
    missing = [command.name for command in bot.walk_commands()
               if not admin_only(command) and command.name not in NOT_IN_HELP
               and not ({command.name, *command.aliases} & mentioned)]
    assert missing == []
    unknown = sorted(word for word in mentioned if word not in names and word not in TYPED_WORDS)
    assert unknown == []
    assert not any(admin_only(names[word]) for word in mentioned if word in names)
    for word in ("$skrzynia", "$ikona", "$kradnijxp", "$zabojca", "$wyczuwanie", "$rekordwieży"):
        assert word in help_cog.HELP_TEXT


def test_help_embed_fits_discord_limits_and_mentions_the_buttons():
    embed = help_cog.help_embed()
    assert len(embed.description) <= 4096 and len(embed.fields) <= 25
    assert all(len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields)
    assert len(embed) <= 6000
    text = help_cog.HELP_TEXT
    assert hub.PANEL_TITLE == "🎮 Panel gry" and "Centrum gry" not in text
    for button in ("Panel gry", "Zaatakuj", "Dołącz", "Potwierdzam", "Dołącz do polowania",
                   "Otwórz skrzynię", "Pomódl się", "Gotowy"):
        assert f"**{button}**" in text
    assert "reakcj" not in text.lower()
    # Mentions render only in field values, never in names.
    assert not any("<@" in field.name for field in embed.fields)


async def test_typed_pomoc_shows_the_help_and_its_cooldown_answers_with_a_live_time(game):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.help")
    for channel in bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    ala = game.add_member("Ala")
    await run(game, ala, "$pomoc", channel=game.announce_channel)
    shown = game.announce_channel.messages[-1].embeds[0]
    assert shown.title == help_cog.HELP_TITLE and shown.to_dict() == help_cog.help_embed().to_dict()
    await run(game, ala, "$komendy", channel=game.announce_channel)
    answer = game.announce_channel.messages[-1].content
    assert answer.startswith("Poczekaj na odnowienie komendy! Spróbuj ponownie <t:")
    assert abs(stamp_of(answer) - (time.time() + 600)) < 30


# -- the central cooldown answer -------------------------------------------------------------

async def test_command_on_cooldown_shows_discords_live_countdown(game):
    error = commands.CommandOnCooldown(commands.Cooldown(1, 1800), 1800.0, commands.BucketType.user)
    ctx = SimpleNamespace(command=None, channel=game.channel, author=game.add_member("Ala"))
    await game.bot.on_command_error(ctx, error)
    text = game.channel.messages[-1].content
    assert text == bot_module.MSG_COOLDOWN.format(when=f"<t:{stamp_of(text)}:R>")
    assert abs(stamp_of(text) - (time.time() + 1800)) < 5
    assert "godzin" not in text


# -- posting and replacing the panel --------------------------------------------------------

async def test_panel_is_posted_pinned_remembered_and_survives_the_purge(game, cog, pins):
    bot = game.bot
    message = await panel_on(game)
    assert message.embeds[0].title == hub.PANEL_TITLE
    assert "toczą się na klawiaturze" in message.embeds[0].description
    assert message.view.is_finished()                                    # served by the registered view
    labels = [item.label for item in message.view.children]
    assert labels == LABELS
    rows = [item.row for item in message.view.children]
    assert rows == [0, 0, 1, 1, 1, 2, 2]
    styles = {item.label: item.style for item in message.view.children}
    assert styles["Polowanie"] is styles["Wieża"] is discord.ButtonStyle.primary
    assert message.pinned and pins == [("pin", message.id)]
    assert bot.store.runtime_get(hub.PANEL_KEY) == {"channel": game.channel.id, "message": message.id}

    for _ in range(3):
        await io.send(game.channel, "spam")
    await io.clear_channel(game.channel)
    assert message in game.channel.messages and not game.channel.contains("spam")


async def test_posting_again_replaces_the_previous_panel(game, cog, pins):
    bot = game.bot
    first = await panel_on(game)
    second = await panel_on(game)
    assert first.deleted and not first.pinned and second.pinned
    assert ("unpin", first.id) in pins
    assert bot.store.runtime_get(hub.PANEL_KEY)["message"] == second.id
    assert game.channel.count(hub.PANEL_TITLE) == 1

    # Somebody deleted the panel by hand: posting again simply works.
    await second.delete()
    third = await panel_on(game)
    assert third.pinned and game.channel.count(hub.PANEL_TITLE) == 1
    # A remembered panel on a channel the bot no longer sees is skipped, too.
    bot.store.runtime_set(hub.PANEL_KEY, {"channel": 1, "message": third.id})
    fourth = await panel_on(game)
    assert not third.deleted and fourth.pinned


def administrator(game, name: str = "Alter"):
    member = game.add_member(name)
    member.guild_permissions = discord.Permissions(administrator=True)
    return member


async def test_panel_command_for_administrators(game, cog, pins):
    bot = game.bot
    alter = administrator(game)
    command = bot.get_command("panel")
    assert any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)

    # Not for players: nothing is posted, nothing is said.
    ala = game.add_member("Ala")
    await run(game, ala, "$panel")
    assert game.channel.count(hub.PANEL_TITLE) == 0 and bot.store.runtime_get(hub.PANEL_KEY) is None

    # Typed elsewhere: the panel goes to the boss channel and the administrator is told.
    await run(game, alter, "$panel", channel=game.announce_channel)
    panel = game.channel.messages[-1]
    assert panel.embeds[0].title == hub.PANEL_TITLE and panel.pinned
    assert game.announce_channel.messages[-1].content == hub.MSG_POSTED.format(channel_id=game.channel.id)

    # Typed on the boss channel: a tick under the command, the old panel is gone.
    typed = await run(game, alter, "$centrum")
    assert typed.reactions == ["✅"] and panel.deleted
    assert game.channel.count(hub.PANEL_TITLE) == 1


async def test_panel_on_the_test_channel_leaves_the_real_one_alone(game, cog, pins):
    bot = game.bot
    real = await panel_on(game)
    trial = await panel_on(game, game.announce_channel)
    assert not real.deleted and real.pinned and trial.pinned
    assert bot.store.runtime_get(hub.PANEL_KEY)["message"] == real.id
    assert bot.store.runtime_get(hub.panel_key(bot, game.announce_channel.id))["message"] == trial.id
    again = await panel_on(game, game.announce_channel)
    assert trial.deleted and not real.deleted and again.pinned


async def test_click_discord_gave_up_starts_nothing(game, cog, fake_hunt):
    """The bot lagged for more than 3 s: the player saw "interaction failed" - no hunt is spent."""
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    for handler in (hub.hunt_clicked, hub.tower_clicked):
        click = FakeInteraction(game.bot, ala, panel)
        click.response.fail_with = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                                                    "Unknown interaction")
        assert await handler(click) is False
    assert fake_hunt == [] and not daily.load_daily_from_file(game.bot, ala.id)
    click = FakeInteraction(game.bot, ala, panel)
    click.response.fail_with = ConnectionError("down")
    assert await hub.show_privately(click, hub.show_help) is False and game.bot.owner_log == []


async def test_panel_command_when_pinning_or_posting_fails(game, cog, monkeypatch):
    alter = administrator(game)

    async def refused(self, *, reason=None):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    monkeypatch.setattr(FakeMessage, "pin", refused, raising=False)
    await run(game, alter, "$panel")
    assert game.channel.messages[-1].content == hub.MSG_POSTED_NOT_PINNED.format(channel_id=game.channel.id)
    assert game.channel.count(hub.PANEL_TITLE) == 1                     # posted, only not pinned

    game.channel.fail_with = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
    game.channel.fail_sends = 1                                          # only the panel fails (not retried)
    remembered = game.bot.store.runtime_get(hub.PANEL_KEY)
    await run(game, alter, "$panel")
    assert game.channel.messages[-1].content.startswith(hub.MSG_NOT_POSTED)
    assert game.bot.store.runtime_get(hub.PANEL_KEY) == remembered      # the old panel stays


# -- gameplay buttons --------------------------------------------------------------------

async def test_hunt_button_runs_the_hunt_of_the_clicker(game, cog, fake_hunt):
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    click = await game.click(panel, ala, "Polowanie")
    assert click.response.deferred and click.replies == []
    assert fake_hunt == [(ala, "hunt")] and not game.bot.state.busy
    assert daily.load_daily_from_file(game.bot, ala.id)                 # the day's hunt is used up

    again = await game.click(panel, ala, "Polowanie")
    assert again.whispers == [daily.cooldown_text()] and len(fake_hunt) == 1
    assert not game.channel.ever_sent("Zregeneruj się")                  # refusals stay private


async def test_hunt_button_refusals_are_private(game, cog, fake_hunt):
    bot = game.bot
    bot.debug = False
    ala, jan = game.add_member("Ala"), dead_hunter(game)
    panel = await panel_on(game)

    assert (await game.click(panel, jan, "Polowanie")).whispers == [ui.MSG_CANNOT_PLAY]
    with bot.state.hold_busy("tower"):
        assert (await game.click(panel, ala, "Polowanie")).whispers == [ui.MSG_BUSY]
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    assert (await game.click(panel, ala, "Polowanie")).whispers == [daily.MSG_BOSS_COMING]
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    assert (await game.click(panel, ala, "Polowanie")).whispers == [daily.MSG_BOSS_FIGHT]
    assert fake_hunt == [] and len(game.channel.bot_messages) == 2      # the pinned rules + the panel

    # Off the boss channel (a panel posted elsewhere): gameplay is refused.
    elsewhere = await panel_on(game, game.announce_channel)
    wrong = await game.click(elsewhere, ala, "Polowanie")
    assert wrong.whispers == [ui.MSG_WRONG_CHANNEL.format(channel_id=bot.ids.boss_channel)]


async def test_hunt_button_joins_a_hunt_that_is_gathering(game, cog, fake_hunt, monkeypatch):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    panel = await panel_on(game)
    asked = []
    monkeypatch.setattr(daily, "is_gathering", lambda channel: True)
    monkeypatch.setattr(daily, "request_join", lambda channel, member, answer: asked.append(member) or True)
    with game.bot.state.hold_busy("hunt"):                               # the gathering hunt holds the channel
        click = await game.click(panel, ola, "Polowanie")
    assert asked == [ola] and click.replies == [] and click.response.deferred and fake_hunt == []


async def test_typed_polowanie_still_works_next_to_the_button(game, cog, fake_hunt):
    await game.bot.load_extension("altermmo.cogs.hunting")
    ala = game.add_member("Ala")
    await run(game, ala, "$polowanie")
    assert fake_hunt == [(ala, "hunt")]


async def test_tower_button_runs_the_tower_and_refuses_privately(game, cog, fake_tower):
    bot = game.bot
    bot.debug = False
    ala, jan = game.add_member("Ala"), dead_hunter(game)
    panel = await panel_on(game)
    click = await game.click(panel, ala, "Wieża")
    assert click.response.deferred and click.replies == [] and fake_tower == [(ala, "tower")]
    assert (await game.click(panel, ala, "Wieża")).whispers == [tower.cooldown_text()]
    assert (await game.click(panel, jan, "Wieża")).whispers == [ui.MSG_CANNOT_PLAY]
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    ola = game.add_member("Ola")
    assert (await game.click(panel, ola, "Wieża")).whispers == [tower.MSG_BOSS_HERE]
    assert len(fake_tower) == 1 and not game.channel.ever_sent("Drzwi Wieży")


async def test_a_long_run_survives_the_panel_being_deleted(game, cog, monkeypatch):
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    started, release = asyncio.Event(), asyncio.Event()

    async def daily_hunt(scene):
        started.set()
        await release.wait()
        await scene.send("Polowanie skończone")

    monkeypatch.setattr(daily, "daily_hunt", daily_hunt)
    click = game.click_later(panel, ala, "Polowanie")
    await asyncio.wait_for(started.wait(), 2)
    await panel.delete()                                                 # e.g. removed by hand
    release.set()
    interaction = await asyncio.wait_for(click, 2)
    assert interaction.replies == [] and game.channel.contains("Polowanie skończone")
    assert not game.bot.state.busy


async def test_buttons_of_a_panel_from_before_a_restart_still_work(game, pins, fake_hunt):
    """The panel's message outlives the bot; the view registered at start-up answers it."""
    ala = game.add_member("Ala")
    old = await io.send(game.channel, embed=hub.panel_embed(), view=hub.HubView())
    old.view.stop()                                                      # the process that sent it is gone
    await game.bot.load_extension("altermmo.cogs.hub")                   # a new start
    assert (await game.click(old, ala, "Polowanie")).replies == [] and fake_hunt == [(ala, "hunt")]


# -- display buttons (private answers) ------------------------------------------------------

async def test_display_buttons_answer_privately(game_db, pins, monkeypatch):
    game = game_db
    await game.bot.load_extension("altermmo.cogs.hub")
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    public_before = len(game.channel.messages)

    alone = await game.click(panel, ala, "Towarzysz")
    assert alone.response.deferred and alone.whispers == [pets._msg_alone(ala.id)]
    assert (await game.click(panel, ala, "Stajnia")).whispers == [pets._msg_alone(ala.id)]
    assert (await game.click(panel, ala, "Klejnoty")).whispers == [skills.MSG_NO_SKILL.format(player_id=ala.id)]

    rankings = await game.click(panel, ala, "Rankingi")
    titles = [reply.embeds[0].title for reply in rankings.replies]
    assert titles == ["Ranking łowców potworów!", "Eksploratorzy Wieży Śmierci!",
                      "Najbardziej utalentowani towarzysze!"]
    assert all(reply.ephemeral for reply in rankings.replies)
    assert "Sezon kończy się <t:" in rankings.replies[0].embeds[0].description

    helped = await game.click(panel, ala, "Pomoc")
    assert [reply.embeds[0].title for reply in helped.replies] == [help_cog.HELP_TITLE]
    assert helped.replies[0].ephemeral

    await add_owner(game.bot.db, ala.id, 7)
    await add_pet(game.bot.db, 7, pet_name="Burek")
    shown = await game.click(panel, ala, "Towarzysz")
    (reply,) = shown.replies
    assert reply.ephemeral and reply.view is None and "Burek" in reply.text
    assert len(game.channel.messages) == public_before                  # nothing landed on the channel


async def test_display_buttons_follow_the_busy_policy_and_let_dead_hunters_look(game, cog, monkeypatch):
    bot = game.bot
    jan = dead_hunter(game)
    panel = await panel_on(game)
    shown = []

    async def show_help(scene):
        shown.append(scene.author)
        await scene.send("pomoc")

    monkeypatch.setattr(hub, "show_help", show_help)
    assert (await game.click(panel, jan, "Pomoc")).whispers == ["pomoc"]     # looking is allowed
    with bot.state.hold_busy("hunt"):
        assert (await game.click(panel, jan, "Pomoc")).whispers == [ui.MSG_BUSY]
        monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="read_only_free"))
        assert (await game.click(panel, jan, "Pomoc")).whispers == ["pomoc"]
    assert shown == [jan, jan]

    # Display buttons work on a panel anywhere (it is only a look).
    elsewhere = await panel_on(game, game.announce_channel)
    assert (await game.click(elsewhere, jan, "Pomoc")).whispers == ["pomoc"]


async def test_display_button_when_the_database_is_down_or_something_breaks(game, cog, monkeypatch):
    bot = game.bot
    ala = game.add_member("Ala")
    panel = await panel_on(game)

    async def down(scene, player, **kwargs):
        raise DatabaseUnavailable("no connection")

    monkeypatch.setattr(pets, "show_pet", down)
    assert (await game.click(panel, ala, "Towarzysz")).whispers == [hub.MSG_DB_DOWN]
    assert bot.owner_log == []

    async def broken(scene):
        raise RuntimeError("boom")

    monkeypatch.setattr(skills, "show_skills", broken)
    assert (await game.click(panel, ala, "Klejnoty")).whispers == [ui.MSG_FAILED]
    assert any("altermmo:hub:gems" in line for line in bot.owner_log)


# -- texts of the other areas that point at buttons -----------------------------------------

def test_boss_hints_of_hunting_and_tower_mention_the_attack_button():
    for text in (daily.MSG_BOSS_COMING, tower.MSG_BOSS_COMING, tower.MSG_BOSS_HERE):
        assert "liknij **Zaatakuj** pod bossem albo wpisz **$zaatakuj**" in text
