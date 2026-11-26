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
from altermmo import schema
from altermmo.cogs import help as help_cog
from altermmo.core import challenge, guards, io, ui
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossPhase
from altermmo.features import achievements, album, albumrank, daily, hub, items, patrons, pets, skills, tower

from fakes import FakeInteraction, FakeMember, FakeMessage
from test_pets import add_owner, add_pet
from test_ui_daily import active_gem, collector

LABELS = ["Polowanie", "Wieża", "Zlecenia", "Towarzysz", "Stajnia", "Klejnoty", "Przedmioty", "Rankingi", "Osiągnięcia",
          "Album", "Pomoc", "Powiadomienia"]
#: The custom ids of the panel's buttons: panels pinned long ago still send exactly these.
BUTTON_IDS = ["altermmo:hub:hunt", "altermmo:hub:tower", "altermmo:hub:missions", "altermmo:hub:pet",
              "altermmo:hub:stable", "altermmo:hub:gems", "altermmo:hub:items", "altermmo:hub:rankings",
              "altermmo:hub:achievements", "altermmo:hub:album", "altermmo:hub:help", "altermmo:hub:notify"]
PATRON = patrons.PATRON_ROLES[0]
#: Typed words the help mentions that are not commands (answers inside a flow).
TYPED_WORDS = {"potwierdzam", "tak"}
#: Player commands the help leaves out on purpose.
NOT_IN_HELP = {"ping", "pomoc"}


# -- fixtures and helpers ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(guards, "_last_notice", {})
    monkeypatch.setattr(hub, "_notify_reported", {})
    monkeypatch.setattr(hub, "_notify_locks", {})
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
def quiet_start(monkeypatch):
    """The achievements cog's start-up hand-out does nothing (it would make roles in the background)."""
    started: list = []

    async def start_up(bot):
        started.append(bot)

    monkeypatch.setattr(achievements, "start_up", start_up)
    return started


@pytest_asyncio.fixture
async def achievements_cog(game, cog, quiet_start):
    await game.bot.load_extension("altermmo.cogs.achievements")
    return game.bot.get_cog("Achievements")


class DatabaseDown:
    """A database that cannot be reached at all."""

    async def fetch(self, *args, **kwargs):
        raise DatabaseUnavailable("test: database is down")

    fetchrow = fetchval = execute = fetch


@pytest.fixture
def fake_hunt(monkeypatch, game):
    """``!polowanie``'s hunt without monsters: records who hunted and who held the channel."""
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
    assert {hub.HUNT_ID, hub.TOWER_ID, hub.MISSIONS_ID, hub.PET_ID, hub.STABLE_ID, hub.GEMS_ID, hub.ITEMS_ID,
            hub.RANKINGS_ID, hub.ACHIEVEMENTS_ID, hub.ALBUM_ID, hub.HELP_ID, hub.NOTIFY_ID} == set(BUTTON_IDS) <= set(ids)
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

    mentioned = {word.lower() for word in re.findall(r"!(\w+)", help_cog.HELP_TEXT)}
    assert not re.search(r"\$[^\W\d_]", help_cog.HELP_TEXT)          # the help names commands with "!" only
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
    for word in ("!skrzynia", "!ikona", "!kradnijxp", "!zabojca", "!wyczuwanie", "!rekordwieży"):
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
    # The supporters' section (before 2026-11-20: "🙏 Patroni", crafters and patrons only).
    assert [field.name for field in embed.fields][-1] == "🙏 Wspierający"
    assert patrons.SUPPORTERS_LONG in embed.fields[-1].value and patrons.SUPPORTERS_LONG in embed.description
    assert "Patroni mogą" not in text and str(patrons.CRAFTER_ROLE) not in text


@pytest.mark.parametrize("text", [hub.PANEL_TEXT, hub.PANEL_FOOTER, help_cog.HELP_TEXT])
def test_panel_and_help_promise_no_private_answers(text):
    """Owner's decision of 2026-11-20: nothing the bot says is "visible only to you"."""
    lowered = text.lower()
    for promise in ("tylko tobie", "tylko ty", "widzisz tylko", "tylko dla ciebie", "prywatnie"):
        assert promise not in lowered


# -- the panel's second part: what supporters get -----------------------------------------------

def test_panel_fits_discord_limits_and_stays_compact():
    embed = hub.panel_embed()
    assert embed.title == hub.PANEL_TITLE and embed.footer.text == hub.PANEL_FOOTER
    assert embed.description == hub.PANEL_TEXT == hub.PANEL_GAME_TEXT + "\n\n" + hub.PANEL_SUPPORT_TEXT
    assert len(embed.description) <= 4096 and len(embed) <= 6000         # Discord's limits
    assert len(embed.title) <= 256 and len(embed.footer.text) <= 2048 and not embed.fields
    # A pinned message everybody sees: the supporters' part is no longer than the game's part
    # plus the role mentions (8 x 23 characters, which Discord shows as short role names).
    # (1400 since 2026-11-22: the premium eggs' and the ornaments' lines say in bold that they
    # are cosmetic only.  1500 since 2026-11-23: the album's line names the rank of the collection
    # ranking's first place.)
    assert len(hub.PANEL_SUPPORT_TEXT) <= 1500
    # (The achievements line also names the "Osiągnięcia" button and !osiagniecia.)
    assert all(len(line) <= 240 for line in hub.PANEL_BENEFITS)

    # One line of the game's part per button, in the order of the buttons.
    for label in LABELS:
        assert f"**{label}**" in hub.PANEL_GAME_TEXT, label
    order = [hub.PANEL_GAME_TEXT.index(f"**{label}** - ")
             for label in ("Rankingi", "Osiągnięcia", "Album", "Pomoc", "Powiadomienia")]
    assert order == sorted(order)
    assert "🏅 **Osiągnięcia** - Twoje kolekcjonerskie osiągnięcia (dla wspierających).\n" in hub.PANEL_GAME_TEXT
    assert "📖 **Album** - kolekcja Twoich poświęconych towarzyszy (dla wspierających).\n" in hub.PANEL_GAME_TEXT


async def test_the_items_button_follows_the_gems_and_shows_the_bag_also_without_a_pet(game_db, cog):
    """(Items, 2026-11-26) "Przedmioty" on row 1 after "Klejnoty", with its permanent id and its line."""
    view = hub.HubView()
    try:
        children = list(view.children)
        button = children[6]
        assert [item.label for item in children[5:7]] == ["Klejnoty", "Przedmioty"]
        assert button.custom_id == hub.ITEMS_ID == "altermmo:hub:items" and button.row == 1
        assert str(button.emoji) == "🎒" and button.style is discord.ButtonStyle.secondary
    finally:
        view.stop()
    assert "🎒 **Przedmioty** - " in hub.PANEL_GAME_TEXT
    ala = game_db.add_member("Ala")                                      # no pet, nothing at all
    panel = await panel_on(game_db)
    click = await game_db.click(panel, ala, "Przedmioty")
    assert click.response.deferred and click.replies == []
    assert game_db.channel.messages[-1].content == items.MSG_EMPTY.format(player_id=ala.id)
    await items.grant(game_db.bot, ala.id, "bait", 2)
    await game_db.click(panel, ala, "Przedmioty")
    assert "🪤 **Przynęty**: 2 - **!przyneta**" in game_db.channel.messages[-1].embeds[0].description


def test_the_missions_button_follows_the_tower_and_has_its_line():
    """(Companion missions, 2026-11-25) "Zlecenia" on row 0 after "Wieża", with its permanent id and its line."""
    view = hub.HubView()
    try:
        children = list(view.children)
        button = children[2]
        assert [item.label for item in children[:3]] == ["Polowanie", "Wieża", "Zlecenia"]
        assert button.custom_id == hub.MISSIONS_ID == "altermmo:hub:missions" and button.row == 0
        assert str(button.emoji) == "🧭" and button.style is discord.ButtonStyle.primary
    finally:
        view.stop()
    assert ("🏰 **Wieża** - wyrusz na eksplorację Wieży Śmierci (raz w tygodniu). Im wyżej, tym trudniej!\n"
            "🧭 **Zlecenia** - raz dziennie wyślij towarzysza na zlecenie z tablicy") in hub.PANEL_GAME_TEXT
    assert hub.PANEL_FOOTER == ("Polowanie, Wieża i Zlecenia działają tylko na tym kanale i nie wtedy, gdy trwa "
                                "inne wydarzenie.")
    assert "wysyła towarzysza na zlecenie" in help_cog.HELP_INTRO
    assert [field.name for field in help_cog.help_embed().fields][-2:] == ["🧭 Zlecenia", "🙏 Wspierający"]


def test_panel_buttons_fit_discord_limits_and_keep_their_ids():
    view = hub.HubView()
    try:
        children = list(view.children)
        assert [item.label for item in children] == LABELS
        # Discord: at most 5 rows of at most 5 buttons; ids of at most 100 characters, labels of 80.
        per_row = collections.Counter(item.row for item in children)
        assert per_row == {0: 3, 1: 4, 2: 4, 3: 1} and max(per_row.values()) <= 5 and len(per_row) <= 5
        assert all(len(item.custom_id) <= 100 and len(item.label) <= 80 for item in children)
        # The ids of the buttons that existed before never change (old pinned panels send them);
        # the new button got its own one.
        assert [item.custom_id for item in children] == BUTTON_IDS
        assert len(set(BUTTON_IDS)) == len(BUTTON_IDS) and hub.ACHIEVEMENTS_ID == "altermmo:hub:achievements"
        button = next(item for item in children if item.custom_id == hub.ACHIEVEMENTS_ID)
        assert str(button.emoji) == "🏅" and button.style is discord.ButtonStyle.secondary
        shelf = next(item for item in children if item.custom_id == hub.ALBUM_ID)
        assert hub.ALBUM_ID == "altermmo:hub:album" and shelf.label == "Album" and shelf.row == 2
        assert str(shelf.emoji) == "📖" and shelf.style is discord.ButtonStyle.secondary
        # "Powiadomienia" (2026-11-22): the last button, alone on a row under the others.
        bell = children[-1]
        assert bell.custom_id == hub.NOTIFY_ID == "altermmo:hub:notify" and bell.label == "Powiadomienia"
        assert str(bell.emoji) == "🔔" and bell.style is discord.ButtonStyle.secondary
        assert bell.row == 3 > max(item.row for item in children[:-1])
        assert not bell.custom_id.startswith(ui.FLOW_PREFIX) and view.is_persistent()
    finally:
        view.stop()


def test_panel_names_the_supporter_roles_the_benefits_and_the_links():
    header, *lines = hub.PANEL_SUPPORT_TEXT.split("\n")
    # "Dodatkowe benefity dla" + every supporter role as a role mention (rendered as the server's
    # role names; a mention inside an embed notifies nobody).
    assert header.startswith("💜 **Dodatkowe benefity dla** <@&") and header.endswith(":")
    assert re.findall(r"<@&(\d+)>", header) == [str(role_id) for role_id in patrons.SUPPORTER_ROLES]
    assert {patrons.TWITCH_SUB_ROLE, *patrons.YOUTUBE_ROLES, patrons.CRAFTER_ROLE,
            *patrons.PATRON_ROLES} == set(patrons.SUPPORTER_ROLES)
    assert "<@" not in hub.PANEL_GAME_TEXT and "@everyone" not in hub.PANEL_TEXT and "@here" not in hub.PANEL_TEXT

    # One line per benefit - what the code really gives supporters.
    assert lines[:len(hub.PANEL_BENEFITS)] == list(hub.PANEL_BENEFITS) and len(hub.PANEL_BENEFITS) == 7
    benefits = "\n".join(hub.PANEL_BENEFITS)
    for words in ("Jaja premium", "**!transformacja**", "lustrem wizualiów", "**!modlitwa**", "kapliczce",
                  "zostaniesz bossem", "imprezy u Barda", "Kolekcjonerskie osiągnięcia", "mityczne zdobycze",
                  "przyciskiem **Osiągnięcia** (**!osiagniecia**)", "Kolekcjonerskie ozdoby karty",
                  "dopóki wspierasz", "📖 **Album**", "poświęceni towarzysze", "(**!album**)",
                  # Owner, 2026-11-22: the panel stresses that the ornaments are cosmetic only.
                  "**wyłącznie elementy kosmetyczne**, bez wpływu na grę",
                  "**To tylko element kosmetyczny**: inny wygląd, te same statystyki."):
        assert words in benefits
    # The album's line (2026-11-21): one line, no species, no ornaments (checked below as well);
    # since 2026-11-22 it names the ranking of the collections, since 2026-11-23 the rank its first
    # place carries (features/collector.py) - without a number and without naming Horus.
    assert hub.PANEL_BENEFITS[-1] == ("📖 **Album** - poświęceni towarzysze zostają w Twoim albumie (**!album**) "
                                      "i liczą się w rankingu kolekcji; jego pierwsze miejsce nosi rangę "
                                      "**Kolekcjoner Pupili** (dodatkowa szansa w giveawayach).")
    assert "Horus" not in hub.PANEL_TEXT and "%" not in hub.PANEL_BENEFITS[-1]
    assert not any(character.isdigit() for character in hub.PANEL_BENEFITS[-1])
    # Not spelled out (owner's decision): which species supporters can find, which ornaments exist.
    for secret in ("smok", "feniks", "jednoroż", "holo", "pieczę", "tropiciel", "łowcy", "arcyłowc"):
        assert secret not in benefits.lower(), secret
    # Owner's decision of 2026-11-20 (evening): only the mythic achievements are left - the line
    # no longer promises roles for the withdrawn ones.
    (trophies,) = [line for line in hub.PANEL_BENEFITS if "**Kolekcjonerskie osiągnięcia**" in line]
    for withdrawn in ("zabite bossy", "polowania", "sezony", "Wieży", "wydarzenia", "legendarne",
                      "wyjątkowych towarzyszy"):
        assert withdrawn not in trophies, withdrawn
    # The silver / gold frames of shiny companions are for everybody: not sold as a perk.
    assert "ramk" not in benefits.lower() and "shiny" not in benefits.lower()

    # The links: Patronite first and recommended, exactly the owner's addresses, as markdown links.
    assert lines[-2] == "" and lines[-1] == hub.PANEL_TEXT.rsplit("\n", 1)[-1] == (
        "**Wesprzyj AlterMMO:** **[Patronite](https://patronite.pl/altermmo)** (polecane)"
        " • [YouTube AlterMMO](http://youtube.com/@AlterMMO/join)"
        " • [YouTube Surowe AlterMMO](http://youtube.com/@Surowe_AlterMMO/join)"
        " • [Twitch](https://www.twitch.tv/products/altermmo_pl)")
    assert re.findall(r"\]\((\S+?)\)", lines[-1]) == [url for _, url in patrons.SUPPORT_LINKS]
    assert hub.PANEL_TEXT.count("http") == 4                             # no bare address anywhere else


async def test_every_command_the_panel_names_exists_and_is_for_players(game, monkeypatch):
    bot = game.bot
    monkeypatch.setattr(bot.supervisor, "start", lambda *args, **kwargs: None)
    for extension in bot_module.EXTENSIONS:
        await bot.load_extension(extension)
    named = set(re.findall(r"!(\w+)", hub.PANEL_TEXT))
    assert not re.search(r"\$[^\W\d_]", hub.PANEL_TEXT)                 # commands are named with "!" only
    assert {"zaatakuj", "transformacja", "modlitwa"} <= named
    for word in named:
        command = bot.get_command(word)
        assert command is not None and command.cog_name != "Admin", word
        assert not any("has_permissions" in getattr(check, "__qualname__", "") for check in command.checks)


async def test_typed_pomoc_shows_the_help_and_its_cooldown_answers_with_a_live_time(game):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.help")
    for channel in bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    ala = game.add_member("Ala")
    await run(game, ala, "!pomoc", channel=game.announce_channel)
    shown = game.announce_channel.messages[-1].embeds[0]
    assert shown.title == help_cog.HELP_TITLE and shown.to_dict() == help_cog.help_embed().to_dict()
    await run(game, ala, "!komendy", channel=game.announce_channel)
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
    assert rows == [0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3]
    assert [item.custom_id for item in message.view.children] == BUTTON_IDS
    styles = {item.label: item.style for item in message.view.children}
    assert styles["Polowanie"] is styles["Wieża"] is styles["Zlecenia"] is discord.ButtonStyle.primary
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
    await run(game, ala, "!panel")
    assert game.channel.count(hub.PANEL_TITLE) == 0 and bot.store.runtime_get(hub.PANEL_KEY) is None

    # Typed elsewhere: the panel goes to the boss channel and the administrator is told.
    await run(game, alter, "!panel", channel=game.announce_channel)
    panel = game.channel.messages[-1]
    assert panel.embeds[0].title == hub.PANEL_TITLE and panel.pinned
    assert game.announce_channel.messages[-1].content == hub.MSG_POSTED.format(channel_id=game.channel.id)

    # Typed on the boss channel: a tick under the command, the old panel is gone.
    typed = await run(game, alter, "!centrum")
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
    posted = len(game.channel.sent)
    assert await hub.show_display(click, hub.show_help) is False and game.bot.owner_log == []
    assert len(game.channel.sent) == posted                             # no help embed for nobody


async def test_panel_command_when_pinning_or_posting_fails(game, cog, monkeypatch):
    alter = administrator(game)

    async def refused(self, *, reason=None):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    monkeypatch.setattr(FakeMessage, "pin", refused, raising=False)
    await run(game, alter, "!panel")
    assert game.channel.messages[-1].content == hub.MSG_POSTED_NOT_PINNED.format(channel_id=game.channel.id)
    assert game.channel.count(hub.PANEL_TITLE) == 1                     # posted, only not pinned

    game.channel.fail_with = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
    game.channel.fail_sends = 1                                          # only the panel fails (not retried)
    remembered = game.bot.store.runtime_get(hub.PANEL_KEY)
    await run(game, alter, "!panel")
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
    assert again.answers == [daily.cooldown_text()] and len(fake_hunt) == 1
    # The refusal is an ordinary message on the channel, addressed to the clicker.
    assert again.public == [f"<@{ala.id}> {daily.cooldown_text()}"]
    (reply,) = again.replies
    assert not reply.ephemeral and reply.message is game.channel.messages[-1]
    assert reply.allowed_mentions.users == [ala] and not reply.allowed_mentions.everyone


async def test_hunt_button_refusals_are_public_and_addressed(game, cog, fake_hunt):
    bot = game.bot
    bot.debug = False
    ala, jan = game.add_member("Ala"), dead_hunter(game)
    panel = await panel_on(game)
    before = len(game.channel.bot_messages)                             # the pinned rules + the panel

    assert (await game.click(panel, jan, "Polowanie")).answers == [ui.MSG_CANNOT_PLAY]
    with bot.state.hold_busy("tower"):
        assert (await game.click(panel, ala, "Polowanie")).answers == [ui.MSG_BUSY]
        # The busy notice is shared with the typed commands: one per 20 s on the channel.
        swallowed = await game.click(panel, ala, "Polowanie")
        assert swallowed.response.deferred and swallowed.replies == []
    await bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
    assert (await game.click(panel, ala, "Polowanie")).answers == [daily.MSG_BOSS_COMING]
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    assert (await game.click(panel, ala, "Polowanie")).answers == [daily.MSG_BOSS_FIGHT]
    assert fake_hunt == []
    assert [m.content for m in game.channel.bot_messages[before:]] == [
        f"<@{jan.id}> {ui.MSG_CANNOT_PLAY}", f"<@{ala.id}> {ui.MSG_BUSY}",
        f"<@{ala.id}> {daily.MSG_BOSS_COMING}", f"<@{ala.id}> {daily.MSG_BOSS_FIGHT}"]

    # Off the boss channel (a panel posted elsewhere): gameplay is refused there, on that channel.
    elsewhere = await panel_on(game, game.announce_channel)
    wrong = await game.click(elsewhere, ala, "Polowanie")
    assert wrong.answers == [ui.MSG_WRONG_CHANNEL.format(channel_id=bot.ids.boss_channel)]
    assert wrong.replies[0].message is game.announce_channel.messages[-1]


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
    await run(game, ala, "!polowanie")
    assert fake_hunt == [(ala, "hunt")]


async def test_tower_button_runs_the_tower_and_refuses_publicly(game, cog, fake_tower):
    bot = game.bot
    bot.debug = False
    ala, jan = game.add_member("Ala"), dead_hunter(game)
    panel = await panel_on(game)
    click = await game.click(panel, ala, "Wieża")
    assert click.response.deferred and click.replies == [] and fake_tower == [(ala, "tower")]
    assert (await game.click(panel, ala, "Wieża")).answers == [tower.cooldown_text()]
    assert (await game.click(panel, jan, "Wieża")).answers == [ui.MSG_CANNOT_PLAY]
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    ola = game.add_member("Ola")
    here = await game.click(panel, ola, "Wieża")
    assert here.public == [f"<@{ola.id}> {tower.MSG_BOSS_HERE}"] and here.replies[0].message in game.channel.messages
    assert len(fake_tower) == 1
    assert game.channel.count(f"<@{ala.id}> {tower.cooldown_text()}") == 1   # everybody sees whom it is for
    assert game.channel.count(f"<@{jan.id}> {ui.MSG_CANNOT_PLAY}") == 1


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


# -- display buttons (the typed commands' posts, on the channel) ------------------------------

DISPLAY_COMMANDS = {"Towarzysz": ["!towarzysz"], "Stajnia": ["!stajnia"], "Klejnoty": ["!klejnoty"],
                    "Przedmioty": ["!przedmioty"],
                    "Rankingi": ["!ranking", "!rankingwiezy", "!rankingtowarzyszy", "!rankingalbumow"],
                    "Osiągnięcia": ["!osiagniecia"], "Album": ["!album"], "Pomoc": ["!pomoc"]}


def shape(message) -> tuple:
    """What a post shows: its text, pictures and the kind of buttons / menus under it."""
    view = message.view
    items = [] if view is None else [(type(item).__name__, getattr(item, "label", None),
                                      getattr(item, "placeholder", None)) for item in view.children]
    return message.text, list(message.files), None if view is None else type(view).__name__, items


async def test_display_buttons_post_what_the_typed_commands_post(game_db, pins, monkeypatch, quiet_start):
    """The panel's display buttons are the typed commands: on the channel, with their buttons and menus."""
    game = game_db
    bot = game.bot
    await schema.create_new_tables(bot.db)                               # (the bot makes them at start-up)
    monkeypatch.setattr(bot.supervisor, "start", lambda *args, **kwargs: None)
    for extension in ("altermmo.cogs.hub", "altermmo.cogs.pets", "altermmo.cogs.skills",
                      "altermmo.cogs.ranking", "altermmo.cogs.tower", "altermmo.cogs.achievements",
                      "altermmo.cogs.album", "altermmo.cogs.help", "altermmo.cogs.items"):
        await bot.load_extension(extension)
    for channel in bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    ala = await collector(game)                                          # three gems: the pouch has a menu
    slayer = game.guild.get_role(bot.ids.mythic_slayer_role)            # killed a mythic boss
    ala.roles += [game.guild.add_role(PATRON, "Patron"), slayer]
    await add_pet(bot.db, 7, pet_name="Burek")
    await add_pet(bot.db, 8, pet_name="Mruczek")
    await bot.db.execute("UPDATE PETOWNER SET PET_ID = 7, PET_ID_ALT1 = 8, REROLL_SCROLL = 2 WHERE PLAYER_ID = $1",
                         ala.id)
    panel = await panel_on(game)

    clicked: dict[str, list] = {}
    for label in DISPLAY_COMMANDS:
        before = len(game.channel.sent)
        click = await game.click(panel, ala, label)
        assert click.response.deferred and click.replies == []           # acknowledged, then posted
        clicked[label] = game.channel.sent[before:]
        assert all(message in game.channel.messages for message in clicked[label])
    for label, commands_typed in DISPLAY_COMMANDS.items():
        before = len(game.channel.sent)
        for command in commands_typed:
            await run(game, ala, command)
        typed = game.channel.sent[before:]
        if label == "Pomoc":
            # The button used up the player's !pomoc cooldown (one help per 10 minutes, typed
            # or clicked): the typed command is answered with the cooldown notice.
            assert [m.embeds for m in typed] == [[]] and typed[0].content.startswith("Poczekaj na odnowienie")
            continue
        assert [shape(m) for m in clicked[label]] == [shape(m) for m in typed], label

    pet, = clicked["Towarzysz"]
    assert "Burek" in pet.text and isinstance(pet.view, pets.PetPanel) and pet.button_labels
    stable, = clicked["Stajnia"]
    assert isinstance(stable.view, pets.StablePanel) and "Mruczek" in stable.text
    bag, = clicked["Przedmioty"]                                         # (features/items.py, 2026-11-26)
    assert bag.embeds[0].title == items.VIEW_TITLE and f"<@{ala.id}>" in bag.text and bag.view is None
    assert [m.embeds[0].title for m in clicked["Rankingi"]] == [            # (a supporter: four rankings)
        "Ranking łowców potworów!", "Eksploratorzy Wieży Śmierci!", "Najbardziej utalentowani towarzysze!",
        albumrank.MSG_TITLE]
    assert "Sezon kończy się <t:" in clicked["Rankingi"][0].embeds[0].description
    trophies, = clicked["Osiągnięcia"]                                   # a supporter's summary, for everybody
    assert trophies.embeds[0].title == achievements.MSG_SUMMARY_TITLE and trophies.view is None
    assert f"<@{ala.id}>, masz **1 z 5** osiągnięć." in trophies.text and len(achievements.TROPHIES) == 5
    assert re.findall(r"<@&(\d+)>", trophies.text) == [str(slayer.id)] and "Nowe" not in trophies.text
    collection, = clicked["Album"]                                       # a supporter's (empty) album
    assert collection.embeds[0].title == album.MSG_TITLE and collection.view is None
    assert f"<@{ala.id}>, Twój album jest jeszcze pusty." in collection.text
    assert [m.embeds[0].title for m in clicked["Pomoc"]] == [help_cog.HELP_TITLE]

    again = await game.click(panel, ala, "Pomoc")                        # a second click: the cooldown, no embed
    assert len(again.answers) == 1 and again.answers[0].startswith("Poczekaj na odnowienie komendy")
    assert game.channel.messages[-1].embeds == []

    # The pouch shown from the panel carries its own menu, and it works.
    pouch, = clicked["Klejnoty"]
    chosen = await game.click(pouch, ala, "skills:choose", values=["2"])
    assert chosen.answers == [skills.MSG_GEM_CHOSEN.format(player_id=ala.id, name="Klejnot Przywołania")]
    assert await active_gem(game, ala) == 8


async def test_display_buttons_follow_the_busy_policy_and_do_not_post_for_the_silenced(game, cog, monkeypatch):
    bot = game.bot
    jan = dead_hunter(game)
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    shown = []

    async def show_help(scene):
        shown.append(scene.author)
        await scene.send("pomoc")

    monkeypatch.setattr(hub, "show_help", show_help)
    looked = await game.click(panel, ala, "Pomoc")
    assert looked.replies == [] and game.channel.messages[-1].content == "pomoc"
    # Somebody who may not write on the channel cannot make the bot post there for him
    # (the answers are public now).
    refused = await game.click(panel, jan, "Pomoc")
    assert refused.answers == [ui.MSG_CANNOT_PLAY] and shown == [ala]
    with bot.state.hold_busy("hunt"):
        busy = await game.click(panel, ala, "Pomoc")
        assert busy.public == [f"<@{ala.id}> {ui.MSG_BUSY}"] and busy.replies[0].message in game.channel.messages
        monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="read_only_free"))
        assert (await game.click(panel, ala, "Pomoc")).replies == []
        assert game.channel.messages[-1].content == "pomoc"
    assert shown == [ala, ala]

    # Display buttons work on a panel anywhere a player may write - and post there.
    elsewhere = await panel_on(game, game.announce_channel)
    assert (await game.click(elsewhere, jan, "Pomoc")).replies == []      # (silenced on the boss channel only)
    assert game.announce_channel.messages[-1].content == "pomoc" and shown == [ala, ala, jan]


def supporters_only_text(member=None) -> str:
    """The answer to somebody who does not support; public, so it starts with his mention."""
    links = " • ".join(f"[{label}](<{url}>)" for label, url in patrons.SUPPORT_LINKS)
    text = achievements.MSG_SUPPORTERS_ONLY.format(groups=patrons.SUPPORTERS_LONG, links=links)
    return text if member is None else f"<@{member.id}> {text}"


async def test_achievements_button_answers_a_non_supporter_like_the_typed_command(game, achievements_cog):
    """Owner's decision: the achievements are for supporters; the others are told how to become one."""
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    click = await game.click(panel, ala, "Osiągnięcia")
    assert click.response.deferred and click.replies == []              # acknowledged, then posted
    clicked = game.channel.messages[-1]
    await run(game, ala, "!osiagniecia")
    typed = game.channel.messages[-1]
    assert typed is not clicked and shape(clicked) == shape(typed)
    # Public, on the channel: the support links and not a word about achievements anybody has.
    assert clicked.content == supporters_only_text(ala) and clicked.embeds == []
    assert clicked.content.startswith(f"<@{ala.id}> Osiągnięcia kolekcjonerskie są dla wspierających")
    assert all(url in clicked.content for _, url in patrons.SUPPORT_LINKS)
    assert "<@&" not in clicked.content and f" z {len(achievements.TROPHIES)}" not in clicked.content
    assert ala.roles == []


async def test_achievements_button_gives_a_supporter_his_mythic_pet_role_like_the_typed_command(
        game_db, achievements_cog):
    """A supporter whose mythic pet is from before the roles existed: the click hands the role out quietly."""
    game = game_db
    bot = game.bot
    ala = game.add_member("Ala", roles=[PATRON])
    await add_owner(bot.db, ala.id, 5)
    await add_pet(bot.db, 5, pet_name="Burek", mythic=True)
    panel = await panel_on(game)
    click = await game.click(panel, ala, "Osiągnięcia")
    assert click.response.deferred and click.replies == []
    summary = game.channel.messages[-1]
    (embed,) = summary.embeds
    mythic = achievements.role_id_of(bot, achievements.trophy("pet_mythic"))
    assert mythic is not None and game.guild.get_role(mythic).name == "Mityczny towarzysz"
    assert {role.id for role in ala.roles} == {PATRON, mythic}
    assert f"<@{ala.id}>, masz **1 z 5** osiągnięć." in embed.description
    assert achievements.MSG_SUMMARY_NEW.format(roles=f"<@&{mythic}>") in embed.description
    assert re.findall(r"<@&(\d+)>", summary.text) == [str(mythic), str(mythic)]   # held + "Nowe"
    assert not game.channel.contains("zdobywa osiągnięcie")                      # no announcement

    # The typed command afterwards: the same summary, nothing new any more.
    await run(game, ala, "!osiagniecia")
    typed = game.channel.messages[-1]
    assert typed is not summary and "Nowe" not in typed.embeds[0].description
    assert typed.embeds[0].description == embed.description.split("\n\n🎉")[0]


async def test_achievements_button_follows_the_busy_policy_and_the_silenced_rule(game, achievements_cog, monkeypatch):
    bot = game.bot
    jan = dead_hunter(game)
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    text = supporters_only_text(ala)

    # A dead hunter cannot make the bot post on the boss channel - told publicly, addressed to him.
    refused = await game.click(panel, jan, "Osiągnięcia")
    assert refused.answers == [ui.MSG_CANNOT_PLAY] and refused.public == [f"<@{jan.id}> {ui.MSG_CANNOT_PLAY}"]
    (reply,) = refused.replies
    assert not reply.ephemeral and reply.message is game.channel.messages[-1]
    assert reply.allowed_mentions.users == [jan] and not reply.allowed_mentions.everyone
    assert game.channel.count(supporters_only_text()) == 0

    with bot.state.hold_busy("hunt"):
        busy = await game.click(panel, ala, "Osiągnięcia")
        assert busy.public == [f"<@{ala.id}> {ui.MSG_BUSY}"] and not busy.replies[0].ephemeral
        assert busy.replies[0].allowed_mentions.users == [ala]
        # The busy notice is shared with the typed commands: one per 20 s on the channel.
        await run(game, ala, "!osiagniecia")
        swallowed = await game.click(panel, ala, "Osiągnięcia")
        assert swallowed.response.deferred and swallowed.replies == []
        assert game.channel.count(text) == 0 and game.channel.count(ui.MSG_BUSY) == 1
        # Under "read_only_free" display commands work while busy: the button as the typed one.
        monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="read_only_free"))
        assert (await game.click(panel, ala, "Osiągnięcia")).replies == []
        await run(game, ala, "!osiagniecia")
        assert game.channel.count(text) == 2

    # On a panel elsewhere it posts there (a dead hunter is silenced on the boss channel only).
    elsewhere = await panel_on(game, game.announce_channel)
    assert (await game.click(elsewhere, jan, "Osiągnięcia")).replies == []
    assert game.announce_channel.messages[-1].content == supporters_only_text(jan)


async def test_achievements_button_when_the_database_is_down(game, achievements_cog):
    """The roles a supporter holds are enough to answer: the summary is built from them alone."""
    bot = game.bot
    bot.db = DatabaseDown()
    slayer = bot.ids.mythic_slayer_role                                  # killed a mythic boss
    ala = game.add_member("Ala", roles=[PATRON, slayer])
    panel = await panel_on(game)
    click = await game.click(panel, ala, "Osiągnięcia")
    assert click.response.deferred and click.replies == []              # acknowledged, then posted
    summary = game.channel.messages[-1]
    (embed,) = summary.embeds
    assert embed.title == achievements.MSG_SUMMARY_TITLE and summary.view is None
    assert f"<@{ala.id}>, masz **1 z 5** osiągnięć." in embed.description and len(achievements.TROPHIES) == 5
    assert re.findall(r"<@&(\d+)>", summary.text) == [str(slayer)] and "Nowe" not in embed.description
    assert not game.channel.contains(hub.MSG_DB_DOWN) and bot.owner_log == []

    # The typed command answers the same.
    await run(game, ala, "!osiagniecia")
    typed = game.channel.messages[-1]
    assert typed is not summary and typed.embeds[0].description == embed.description


async def test_display_buttons_wait_while_a_typed_fight_is_on(game, cog, fake_hunt, monkeypatch):
    """A boss fight does not hold the busy flag, yet nothing but its prompts may appear between them."""
    bot = game.bot
    bot.debug = False
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    panel = await panel_on(game)
    shown = []

    async def show_help(scene):
        shown.append(scene.author)
        await scene.send("pomoc")

    monkeypatch.setattr(hub, "show_help", show_help)
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    fight = asyncio.ensure_future(challenge.ask(bot, game.channel, "1. **Ala**: wpisz **miecz**",
                                                players=[ala], timeout=30))
    try:
        deadline = time.monotonic() + 3
        while not challenge.fight_on(game.channel):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        assert not bot.state.busy
        posted = len(game.channel.sent)

        for player, label in ((ala, "Pomoc"), (ola, "Rankingi"), (ola, "Towarzysz"), (ola, "Osiągnięcia")):
            silent = await game.click(panel, player, label)             # acknowledged only: not even
            assert silent.response.deferred and silent.replies == []    # the busy notice during a fight
        hunt = await game.click(panel, ola, "Polowanie")                 # "a boss fight is on": not posted now
        assert hunt.response.deferred and hunt.replies == []
        assert shown == [] and fake_hunt == []
        assert game.channel.sent[posted:] == []
    finally:
        fight.cancel()
        await asyncio.gather(fight, return_exceptions=True)

    assert not challenge.fight_on(game.channel)                          # (FIGHT_LINGER is 0 in the tests)
    assert (await game.click(panel, ola, "Pomoc")).replies == [] and shown == [ola]
    assert (await game.click(panel, ola, "Osiągnięcia")).replies == []    # ... and the achievements, too
    assert game.channel.messages[-1].content == supporters_only_text(ola)


async def test_display_button_when_the_database_is_down_or_something_breaks(game, cog, monkeypatch):
    bot = game.bot
    ala = game.add_member("Ala")
    panel = await panel_on(game)

    async def down(scene, player, **kwargs):
        raise DatabaseUnavailable("no connection")

    monkeypatch.setattr(pets, "show_pet", down)
    assert (await game.click(panel, ala, "Towarzysz")).answers == [hub.MSG_DB_DOWN]
    assert bot.owner_log == []

    async def broken(scene):
        raise RuntimeError("boom")

    monkeypatch.setattr(skills, "show_skills", broken)
    failed = await game.click(panel, ala, "Klejnoty")
    assert failed.public == [f"<@{ala.id}> {ui.MSG_FAILED}"] and game.channel.messages[-1].content == failed.public[0]
    assert any("altermmo:hub:gems" in line for line in bot.owner_log)


# -- texts of the other areas that point at buttons -----------------------------------------

def test_boss_hints_of_hunting_and_tower_mention_the_attack_button():
    for text in (daily.MSG_BOSS_COMING, tower.MSG_BOSS_COMING, tower.MSG_BOSS_HERE):
        assert "liknij **Zaatakuj** pod bossem albo wpisz **!zaatakuj**" in text


# -- the pinned panel is brought up to date after a start ---------------------------------------

async def test_pinned_panel_gets_the_current_text_after_a_start(game, cog):
    """A panel posted by older code keeps its old text for ever otherwise (pins survive every clearing)."""
    bot = game.bot
    panel = await panel_on(game)
    old = discord.Embed(title="🎮 Centrum gry AlterMMO",
                        description="*Odpowiedzi tych pięciu przycisków widzisz tylko Ty.*")
    await panel.edit(embed=old)
    assert await hub.refresh_panel(bot) is True
    assert panel.embeds[0].title == hub.PANEL_TITLE == "🎮 Panel gry"
    assert "tylko Ty" not in panel.embeds[0].description and panel.pinned
    # ... so the supporters' part (2026-11-20) reaches a panel pinned earlier without a new !panel.
    assert panel.embeds[0].description == hub.PANEL_TEXT and "Dodatkowe benefity dla" in hub.PANEL_TEXT
    assert (await game.click(panel, game.add_member("Ala"), "Pomoc")).response.deferred   # the buttons work

    await panel.delete()                                                 # removed by hand: no error
    assert await hub.refresh_panel(bot) is False
    bot.store.runtime_set(hub.panel_key(bot, bot.ids.boss_channel), None)
    assert await hub.refresh_panel(bot) is False                         # no panel was ever posted


def test_tower_has_a_fantasy_icon():
    """Owner's request of 2026-11-20: a castle instead of the Tokyo Tower emoji."""
    button = next(item for item in hub.HubView().children if item.custom_id == hub.TOWER_ID)
    assert str(button.emoji) == "🏰" and button.label == "Wieża"
    assert "🏰 **Wieża**" in hub.PANEL_TEXT and "🗼" not in hub.PANEL_TEXT


# -- "Powiadomienia": the hunters' role on / off (owner's request of 2026-11-22) -------------------

NOTIFY = "Powiadomienia"


def hunters(game, name: str = "Łowca Potworów"):
    """The role the bot pings for bosses, events and patch notes, named as on the server."""
    role = game.guild.get_role(game.bot.ids.hunters_role)
    role.name = name
    return role


def on_text(name: str = "Łowca Potworów") -> str:
    return hub.MSG_NOTIFY_ON.format(role=name)


def off_text(name: str = "Łowca Potworów") -> str:
    return hub.MSG_NOTIFY_OFF.format(role=name)


def http_error(kind, status: int, code: int, message: str):
    return kind(SimpleNamespace(status=status, reason=message), {"code": code, "message": message})


def test_panel_names_the_notifications_button():
    (line,) = [line for line in hub.PANEL_GAME_TEXT.split("\n") if "**Powiadomienia**" in line]
    assert line.startswith("🔔 **Powiadomienia** - ") and "**Łowca Potworów**" in line
    for what in ("bossach", "wydarzeniach", "nowościach"):                # bosses, events, patch notes
        assert what in line
    # It says what a click does AND that the next one takes the role away again.
    assert "kliknij, żeby dostać rolę" in line and "kolejne kliknięcie ją zdejmuje" in line
    assert len(line) <= 200 and "<@" not in line
    assert hub.MSG_NOTIFY_ON.startswith("🔔") and hub.MSG_NOTIFY_OFF.startswith("🔕")
    # The help names it next to the role it gives.
    assert "przyciskiem **Powiadomienia** na **Panelu gry**" in help_cog.HELP_TEXT


async def test_notifications_button_gives_and_takes_the_hunters_role(game, cog):
    bot = game.bot
    role = hunters(game)
    ala = game.add_member("Ala")                                         # no supporter: it is for everybody
    assert not patrons.check_if_patron(ala)
    panel = await panel_on(game)

    on = await game.click(panel, ala, NOTIFY)
    assert on.response.deferred and on.answers == [on_text()] and role in ala.roles
    # An ordinary message on the channel, addressed to the clicker; the role is named, never pinged.
    assert on.public == [f"<@{ala.id}> {on_text()}"]
    (reply,) = on.replies
    assert not reply.ephemeral and reply.message is game.channel.messages[-1]
    assert reply.allowed_mentions.users == [ala] and not reply.allowed_mentions.roles
    assert "<@&" not in reply.text

    off = await game.click(panel, ala, NOTIFY)
    assert off.answers == [off_text()] and role not in ala.roles
    # On again seconds later: the answer is posted again (the last word on the channel is the truth).
    again = await game.click(panel, ala, NOTIFY)
    assert again.answers == [on_text()] and ala.roles == [role]
    assert game.channel.count(f"<@{ala.id}> {on_text()}") == 2

    # The server's own name of the role; the usual one when it has none.
    role.name = "Łowcy"
    assert (await game.click(panel, ala, NOTIFY)).answers == [off_text("Łowcy")]
    role.name = ""
    assert (await game.click(panel, ala, NOTIFY)).answers == [on_text(hub.NOTIFY_ROLE_NAME)]
    assert bot.owner_log == [] and hub._notify_locks == {}


async def test_notifications_button_reads_the_members_current_roles(game, cog):
    """``interaction.user`` is the member as he was when he clicked: the API's answer decides."""
    role = hunters(game)
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    snapshot = FakeMember("Ala", member_id=ala.id, guild=game.guild)
    snapshot.roles = [role]                                              # "still has it" - he does not
    assert (await game.click(panel, snapshot, NOTIFY)).answers == [on_text()] and role in ala.roles
    snapshot.roles = []                                                  # "still without it" - he has it
    assert (await game.click(panel, snapshot, NOTIFY)).answers == [off_text()] and role not in ala.roles


async def test_two_quick_clicks_end_in_a_consistent_state_with_true_answers(game, cog, monkeypatch):
    role = hunters(game)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    panel = await panel_on(game)
    calls = []
    add, remove = FakeMember.add_roles, FakeMember.remove_roles

    async def slow_add(self, *roles, reason=None):
        calls.append(("add", self.name))
        await asyncio.sleep(0.05)                                        # Discord takes its time
        await add(self, *roles, reason=reason)

    async def slow_remove(self, *roles, reason=None):
        calls.append(("remove", self.name))
        await asyncio.sleep(0.05)
        await remove(self, *roles, reason=reason)

    monkeypatch.setattr(FakeMember, "add_roles", slow_add)
    monkeypatch.setattr(FakeMember, "remove_roles", slow_remove)
    first, second = await asyncio.gather(game.click(panel, ala, NOTIFY), game.click(panel, ala, NOTIFY))
    assert first.response.deferred and second.response.deferred      # both acknowledged at once
    assert first.answers == [on_text()] and second.answers == [off_text()]
    assert calls == [("add", "Ala"), ("remove", "Ala")] and role not in ala.roles

    # Different players do not wait for each other; each ends with the role.
    calls.clear()
    clicks = await asyncio.gather(game.click(panel, ala, NOTIFY), game.click(panel, ola, NOTIFY))
    assert [click.answers for click in clicks] == [[on_text()], [on_text()]]
    assert role in ala.roles and role in ola.roles and sorted(calls) == [("add", "Ala"), ("add", "Ola")]
    assert hub._notify_locks == {}


async def test_notifications_button_waits_while_a_typed_fight_is_on(game, cog):
    """Nothing may make a fight stutter: not posted and NOT switched (no silent change either)."""
    bot = game.bot
    bot.debug = False
    role = hunters(game)
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    await bot.state.set_boss_phase(BossPhase.FIGHTING)
    fight = asyncio.ensure_future(challenge.ask(bot, game.channel, "1. **Ala**: wpisz **miecz**",
                                                players=[ala], timeout=30))
    try:
        deadline = time.monotonic() + 3
        while not challenge.fight_on(game.channel):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        posted = len(game.channel.sent)
        silent = await game.click(panel, ala, NOTIFY)
        assert silent.response.deferred and silent.replies == [] and role not in ala.roles
        assert game.channel.sent[posted:] == []
    finally:
        fight.cancel()
        await asyncio.gather(fight, return_exceptions=True)
    assert (await game.click(panel, ala, NOTIFY)).answers == [on_text()] and role in ala.roles


async def test_notifications_button_follows_the_busy_policy_and_the_silenced_rule(game, cog, monkeypatch):
    bot = game.bot
    role = hunters(game)
    ala, jan = game.add_member("Ala"), dead_hunter(game)
    panel = await panel_on(game)
    with bot.state.hold_busy("hunt"):
        busy = await game.click(panel, ala, NOTIFY)
        assert busy.public == [f"<@{ala.id}> {ui.MSG_BUSY}"] and role not in ala.roles
        monkeypatch.setattr(bot, "settings", dataclasses.replace(bot.settings, busy_policy="read_only_free"))
        assert (await game.click(panel, ala, NOTIFY)).answers == [on_text()] and role in ala.roles

    # A dead hunter cannot make the bot post on the boss channel (as with every display button)...
    refused = await game.click(panel, jan, NOTIFY)
    assert refused.answers == [ui.MSG_CANNOT_PLAY] and role not in jan.roles
    # ... but on a panel where he may write, the button works for him.
    elsewhere = await panel_on(game, game.announce_channel)
    there = await game.click(elsewhere, jan, NOTIFY)
    assert there.answers == [on_text()] and role in jan.roles
    assert there.replies[0].message is game.announce_channel.messages[-1]


async def test_notifications_button_when_discord_refuses(game, cog, monkeypatch):
    """No permission / the role above the bot's: the player is told, the owner once per 10 minutes."""
    bot = game.bot
    role = hunters(game)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    panel = await panel_on(game)
    forbidden = http_error(discord.Forbidden, 403, 50013, "Missing Permissions")

    async def refused(self, *roles, reason=None):
        raise forbidden

    monkeypatch.setattr(FakeMember, "add_roles", refused)
    monkeypatch.setattr(FakeMember, "remove_roles", refused)
    first = await game.click(panel, ala, NOTIFY)
    assert first.response.deferred and first.public == [f"<@{ala.id}> {hub.MSG_NOTIFY_FAILED}"]
    assert (await game.click(panel, ola, NOTIFY)).answers == [hub.MSG_NOTIFY_FAILED]
    (note,) = bot.owner_log                                              # two players, one note
    assert "Zarządzanie rolami" in note and "nadać" in note and str(role.id) in note and "Ala" in note
    assert "<@" not in note                                              # nobody is pinged in the log
    assert role not in ala.roles and role not in ola.roles

    hub._notify_reported["forbidden"] -= hub.NOTIFY_REPORT_GAP          # ten minutes later
    ola.roles.append(role)
    assert (await game.click(panel, ola, NOTIFY)).answers == [hub.MSG_NOTIFY_FAILED]
    assert len(bot.owner_log) == 2 and "zdjąć" in bot.owner_log[-1] and role in ola.roles
    assert not any("Błąd przycisku" in line for line in bot.owner_log)  # never an error in the view


async def test_notifications_button_when_the_role_or_the_member_is_gone(game, cog, monkeypatch):
    bot = game.bot
    role = hunters(game)
    ala = game.add_member("Ala")
    panel = await panel_on(game)

    game.guild.roles.remove(role)                                        # deleted on the server
    assert (await game.click(panel, ala, NOTIFY)).answers == [hub.MSG_NOTIFY_FAILED]
    assert bot.owner_log == [hub.MSG_OWNER_NOTIFY_NO_ROLE.format(role_id=role.id)]
    game.guild.roles.append(role)

    async def unknown_role(self, *roles, reason=None):                   # deleted while the click ran
        raise http_error(discord.NotFound, 404, 10011, "Unknown Role")

    monkeypatch.setattr(FakeMember, "add_roles", unknown_role)
    assert (await game.click(panel, ala, NOTIFY)).answers == [hub.MSG_NOTIFY_FAILED]
    assert len(bot.owner_log) == 1                                       # the same problem: told once

    async def gone(member_id):
        raise http_error(discord.NotFound, 404, 10007, "Unknown Member")

    monkeypatch.setattr(game.guild, "fetch_member", gone)                 # left right after the click
    assert (await game.click(panel, ala, NOTIFY)).answers == [hub.MSG_NOTIFY_FAILED]
    assert len(bot.owner_log) == 2 and "Ala" in bot.owner_log[-1] and "Unknown Member" in bot.owner_log[-1]
    assert role not in ala.roles and hub._notify_locks == {}


async def test_notifications_button_after_a_network_hiccup_or_an_unexpected_error(game, cog, monkeypatch):
    bot = game.bot
    monkeypatch.setattr(hub, "NOTIFY_RETRY_DELAY", 0.0)
    role = hunters(game)
    ala = game.add_member("Ala")
    panel = await panel_on(game)
    add = FakeMember.add_roles
    failures = [ConnectionError("reset by peer")]

    async def flaky(self, *roles, reason=None):
        if failures:
            raise failures.pop()
        await add(self, *roles, reason=reason)

    monkeypatch.setattr(FakeMember, "add_roles", flaky)
    # One hiccup: repeated at once, the player notices nothing.
    assert (await game.click(panel, ala, NOTIFY)).answers == [on_text()] and role in ala.roles
    ala.roles.clear()
    # Discord stays away (every attempt fails): try again - nothing changed, the owner is not bothered.
    failures[:] = [discord.DiscordServerError(SimpleNamespace(status=503, reason="Unavailable"), "down")] * 3
    assert (await game.click(panel, ala, NOTIFY)).answers == [hub.MSG_NOTIFY_RETRY] and role not in ala.roles
    fetch = game.guild.fetch_member

    async def slow_network(member_id):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(game.guild, "fetch_member", slow_network)
    assert (await game.click(panel, ala, NOTIFY)).answers == [hub.MSG_NOTIFY_RETRY] and role not in ala.roles
    assert bot.owner_log == []
    monkeypatch.setattr(game.guild, "fetch_member", fetch)

    async def broken(self, *roles, reason=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(FakeMember, "add_roles", broken)
    failed = await game.click(panel, ala, NOTIFY)
    assert failed.answers == [hub.MSG_NOTIFY_FAILED] and len(failed.replies) == 1
    (note,) = bot.owner_log
    assert "RuntimeError: boom" in note and "Błąd przycisku" not in note   # answered here, not in on_error

    # A click Discord gave up on changes nothing and posts nothing.
    monkeypatch.setattr(FakeMember, "add_roles", add)
    expired = FakeInteraction(bot, ala, panel)
    expired.response.fail_with = http_error(discord.NotFound, 404, 10062, "Unknown interaction")
    posted = len(game.channel.sent)
    assert await hub.notify_clicked(expired) is None and role not in ala.roles
    assert len(game.channel.sent) == posted


async def test_an_old_panel_gets_the_notifications_button_after_a_start(game, cog):
    """A panel pinned before the button existed: its buttons keep working, the start adds the new one."""
    bot = game.bot
    role = hunters(game)
    ala = game.add_member("Ala")
    old_view = hub.HubView()
    old_view.remove_item(next(item for item in old_view.children if item.custom_id == hub.NOTIFY_ID))
    old_embed = discord.Embed(title=hub.PANEL_TITLE, description="Stary opis panelu")
    panel = await io.send(game.channel, embed=old_embed, view=old_view)
    old_view.stop()                                                      # the process that sent it is gone
    bot.store.runtime_set(hub.PANEL_KEY, {"channel": game.channel.id, "message": panel.id})
    assert NOTIFY not in panel.button_labels and len(panel.button_labels) == len(LABELS) - 1

    # Not brought up to date (yet): the old buttons still answer.
    assert (await game.click(panel, ala, "Pomoc")).replies == []
    assert game.channel.messages[-1].embeds[0].title == help_cog.HELP_TITLE

    assert await hub.refresh_panel(bot) is True
    assert panel.button_labels == LABELS and panel.embeds[0].description == hub.PANEL_TEXT
    assert (await game.click(panel, ala, NOTIFY)).answers == [on_text()] and role in ala.roles

