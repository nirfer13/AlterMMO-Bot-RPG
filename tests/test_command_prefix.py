"""Commands start with "!"; the old "$" keeps working silently (owner's request of 2026-11-24).

* both prefixes run a command, an unknown word after either is silent;
* a message that starts with either prefix followed by a word is a command for the
  fight journal and the "lag" answers (never a late answer, never a complaint);
  "!" as mere punctuation ("!!! lag", "! lagi") stays chat;
* the usage hint names the command with "!" whichever prefix was typed;
* no text of the bot names a known command with "$" - a scan of the sources
  and of the config texts with the real command names (it fails as soon as
  somebody writes a ``$komenda`` text again).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from altermmo import bot as bot_module
from altermmo import settings
from altermmo.cogs import help as help_cog
from altermmo.core import fightlog
from altermmo.core.fightlog import LateState
from altermmo.features import boss, daily, pets

from fakes import FakeMessage
from test_fightlog import ask_round

ROOT = Path(__file__).resolve().parent.parent

#: Words the bot reads itself from typed messages (not registered commands): the
#: confirmations and the legendary boss's "ready".
TYPED_WORDS = {"potwierdzam", "tak"}

#: Patch notes already posted to the players: they stay byte-identical (and true, "$" still works).
POSTED_PATCH_NOTES = range(1, 9)

#: Config files that are not read by the bot any more (``config/ranksConfig.json``, dead) and
#: the patch notes (scanned per entry below).
NOT_SCANNED = {"ranksConfig.json", "patchNotes.json"}


async def deliver(game, member, content: str, *, channel=None) -> FakeMessage:
    """``member`` writes ``content``: the bot's real ``on_message`` (journal + commands) handles it."""
    channel = channel or game.channel
    message = FakeMessage(channel, member, content)
    message.edited_at, message._state = None, None
    channel.messages.append(message)
    await game.bot.on_message(message)
    await game.settle()
    return message


# -- the settings -----------------------------------------------------------------------------

def test_the_prefix_is_the_exclamation_mark_and_the_dollar_still_works():
    assert settings.COMMAND_PREFIX == "!"
    assert settings.LEGACY_PREFIXES == ("$",)
    assert settings.COMMAND_PREFIXES == ("!", "$")


def test_the_bot_answers_to_both_prefixes(game):
    assert tuple(game.bot.command_prefix) == ("!", "$")


@pytest.mark.parametrize("text, expected", [
    ("!pomoc", True), ("$pomoc", True), ("$5", True), ("!lag", True), ("!Ćwicz", True), ("$wieża 3", True),
    ("pomoc", False), (" !pomoc", False), ("lag!", False), ("", False), (None, False), ("#pomoc", False),
    # "!" is punctuation in chat: a prefix counts only with a word right after it (discord.py agrees)
    ("!", False), ("$", False), ("!!!", False), ("!!! lag", False), ("! lagi", False), ("!!!lag", False),
    ("$ lag", False), ("!$pomoc", False), ("!?", False),
])
def test_is_command_text(text, expected):
    assert settings.is_command_text(text) is expected


@pytest.mark.parametrize("text, command, expected", [
    ("!tak", "!tak", True), ("$tak", "!tak", True), (" !TAK ", "!tak", True), ("$Tak", "tak", True),
    ("!tak", "$tak", True),
    ("tak", "!tak", False), ("!takk", "!tak", False), ("! tak", "!tak", False), ("!tak teraz", "!tak", False),
    ("#tak", "!tak", False), ("", "!tak", False), (None, "!tak", False),
])
def test_says_command_takes_any_prefix(text, command, expected):
    assert settings.says_command(text, command) is expected


def test_without_legacy_prefixes_only_the_exclamation_mark_counts(monkeypatch):
    monkeypatch.setattr(settings, "COMMAND_PREFIXES", ("!",))
    assert settings.is_command_text("!pomoc") and not settings.is_command_text("$pomoc")
    assert settings.says_command("!tak", "!tak") and not settings.says_command("$tak", "!tak")


def test_the_typed_words_of_the_game_are_written_with_the_exclamation_mark():
    assert pets.CONFIRM_WORD == "!potwierdzam"
    assert daily.JOIN_WORD == "!polowanie"


# -- both prefixes run a command; an unknown word is silent ----------------------------------

@pytest.mark.parametrize("prefix", ["!", "$"])
async def test_both_prefixes_invoke_a_command(game, prefix):
    ran: list[str] = []

    async def probe(ctx):
        ran.append(ctx.prefix)

    game.bot.add_command(commands.Command(probe, name="sonda"))
    await deliver(game, game.add_member("Ala"), prefix + "sonda")
    assert ran == [prefix]


async def test_the_help_answers_to_both_prefixes(game):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.help")
    for channel in bot.fake_channels.values():
        channel.type = discord.ChannelType.text
    for prefix, name in (("!", "Ala"), ("$", "Ola")):                 # (two players: the help has a cooldown)
        before = len(game.announce_channel.sent)
        await deliver(game, game.add_member(name), prefix + "pomoc", channel=game.announce_channel)
        shown = game.announce_channel.sent[before:]
        assert len(shown) == 1 and shown[0].embeds[0].title == help_cog.HELP_TITLE


@pytest.mark.parametrize("text", ["!nieznanakomenda", "$nieznanakomenda", "!", "$", "!!!", "! pomoc"])
async def test_an_unknown_word_after_a_prefix_is_silent(game, text):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.help")
    errors: list[Exception] = []

    async def record(ctx, error):
        errors.append(error)

    bot.add_listener(record, "on_command_error")
    channels = list(bot.fake_channels.values())
    before = {id(channel): len(channel.sent) for channel in channels}
    await deliver(game, game.add_member("Ala"), text)
    assert all(len(channel.sent) == before[id(channel)] for channel in channels)       # nothing said anywhere
    assert all(isinstance(error, commands.CommandNotFound) for error in errors)       # (no word: not even tried)
    if text in ("!nieznanakomenda", "$nieznanakomenda"):
        assert len(errors) == 1


# -- the usage hint ---------------------------------------------------------------------------

async def test_the_usage_line_names_the_command_with_the_exclamation_mark(game):
    command = SimpleNamespace(qualified_name="wybierzklejnot", signature="<numer>",
                              has_error_handler=lambda: False)
    ctx = SimpleNamespace(command=command, channel=game.channel, author=game.add_member("Ala"))
    await game.bot.on_command_error(ctx, commands.BadArgument("x"))
    assert game.channel.sent[-1].content == "Niepoprawne użycie komendy. Spróbuj: **!wybierzklejnot <numer>**"


@pytest.mark.parametrize("prefix", ["!", "$"])
async def test_a_wrong_use_typed_with_either_prefix_is_answered_with_the_exclamation_mark(game, prefix):
    async def liczba(ctx, numer: int):
        raise AssertionError("not reached: the argument is wrong")

    game.bot.add_command(commands.Command(liczba, name="liczba"))
    await deliver(game, game.add_member("Ala"), prefix + "liczba abc")
    assert game.channel.sent[-1].content == "Niepoprawne użycie komendy. Spróbuj: **!liczba <numer>**"


# -- a typed fight: a message with either prefix is what a "$" message always was -----------

@pytest.mark.parametrize("text", ["!nieznana", "$nieznana", "!unik", "$unik", "!5", "$Unik teraz"])
async def test_a_message_with_a_prefix_is_never_the_late_answer_of_a_round(game, monkeypatch, text):
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player = game.add_member("Ala")
    fight = fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player)
    entry = fight.rounds[0]
    assert entry.late_state is LateState.WATCHING
    await deliver(game, player, text)
    assert entry.late_state is LateState.WATCHING and entry.late is None     # a command, not the answer
    await deliver(game, player, "unik")
    assert entry.late_state is LateState.FOUND and entry.late.text == "unik"


@pytest.mark.parametrize("text", ["!lag", "$lag", "!lagi", "$Lagi", "!laguje teraz"])
def test_a_message_with_a_prefix_is_never_a_lag_complaint(text):
    assert not fightlog.is_lag_complaint(text)
    assert fightlog.is_lag_complaint(text.lstrip("!$ "))                     # (the same words without it are)


@pytest.mark.parametrize("text", ["!!! lag", "! lagi", "!!!lag mnie wywaliło", "! lag", "$ lag"])
def test_punctuation_before_lag_is_still_a_complaint(text):
    """"!" is punctuation in Polish chat: with no word right after it, it is not a command
    (discord.py looks up "!!" or nothing for these), so the complaint is answered as before."""
    assert not settings.is_command_text(text)
    assert fightlog.is_lag_complaint(text)


@pytest.mark.parametrize("text", ["!!!", "! unik", "!"])
async def test_punctuation_alone_is_an_ordinary_late_answer(game, monkeypatch, text):
    """A message that only starts with "!" (no command word) is what the player wrote, like any chat."""
    monkeypatch.setattr(fightlog, "LATE_WATCH", 5.0)
    player = game.add_member("Ala")
    fightlog.begin(game.channel, fightlog.KIND_TOWER, [player], detail=5)
    await ask_round(game, player)
    entry = fightlog.last_fight(player.id).rounds[0]
    await deliver(game, player, text)
    assert entry.late_state is LateState.FOUND and entry.late.text == text


@pytest.mark.parametrize("prefix", ["!", "$"])
async def test_the_boss_lobby_hears_both_prefixes(prefix):
    message = SimpleNamespace(content=f" {prefix}TAK ")
    assert boss._is(message, "!tak", "ready")
    assert boss._is(SimpleNamespace(content=prefix + "zaatakuj"), "!zaatakuj", "join")
    assert not boss._is(SimpleNamespace(content="tak"), "!tak", "ready")


# -- no text names a known command with "$" ------------------------------------------------

async def known_names(game, monkeypatch) -> set[str]:
    """Every command name and alias of the bot (all extensions loaded), plus the typed words."""
    bot = game.bot
    monkeypatch.setattr(bot.supervisor, "start", lambda *args, **kwargs: None)
    for extension in bot_module.EXTENSIONS:
        await bot.load_extension(extension)
    names = set(TYPED_WORDS)
    for command in bot.walk_commands():
        names.update(name.casefold() for name in (command.name, *command.aliases))
    assert {"polowanie", "zaatakuj", "towarzysz", "pomoc", "kradnijxp", "wieża", "t"} <= names
    return names


def dollar_mentions(text: str, names: set[str]) -> list[str]:
    """``$name`` mentions in ``text`` of a known name (whole words; any case)."""
    return [match.group(0) for match in re.finditer(r"\$([^\W\d_]\w*)", text)
            if match.group(1).casefold() in names]


async def test_no_source_or_config_text_names_a_known_command_with_the_dollar(game, monkeypatch):
    names = await known_names(game, monkeypatch)
    found: list[str] = []
    for path in sorted((ROOT / "altermmo").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for mention in dollar_mentions(line, names):
                found.append(f"{path.relative_to(ROOT)}:{number}: {mention}")
    for path in sorted((ROOT / "config").glob("*.json")):
        if path.name in NOT_SCANNED:
            continue
        for mention in dollar_mentions(path.read_text(encoding="utf-8"), names):
            found.append(f"{path.relative_to(ROOT)}: {mention}")
    for note in json.loads((ROOT / "config" / "patchNotes.json").read_text(encoding="utf-8")):
        if int(note["id"]) in POSTED_PATCH_NOTES:
            continue
        for mention in dollar_mentions(json.dumps(note, ensure_ascii=False), names):
            found.append(f"patch notes, entry {note['id']}: {mention}")
    assert found == []


async def test_the_scan_would_catch_a_new_dollar_text(game, monkeypatch):
    names = await known_names(game, monkeypatch)
    assert dollar_mentions("Wpisz **$polowanie**, a potem $Towarzysz.", names) == ["$polowanie", "$Towarzysz"]
    assert dollar_mentions("Kliknij albo wpisz $potwierdzam", names) == ["$potwierdzam"]
    assert dollar_mentions("VALUES ($1, $2) ... ^<@!?(\\d+)>$ ... $lotto ... $polowaniee", names) == []


def test_the_patch_notes_already_posted_still_say_dollar():
    """Entries 1-8 were posted with "$" and are never changed (they stay true: "$" still works)."""
    notes = {note["id"]: note for note in json.loads((ROOT / "config" / "patchNotes.json").read_text(encoding="utf-8"))}
    assert "$kradnijxp" in json.dumps(notes[1], ensure_ascii=False)
    prefix = notes[13]                                                     # the entry of this change
    assert prefix["date"] == "2026-11-24" and "!" in " ".join(prefix["changes"])


#: SHA-256 of the posted entries 1-8 (``json.dumps(..., ensure_ascii=False, sort_keys=True)`` of
#: the list ordered by id), taken from commit 6bf8deb - they were posted and never change.
POSTED_PATCH_NOTES_SHA256 = "4fcf135a9646679f2326399d4e170e75ac4acb959c66ced7551e48b8d4cf6f5e"


def test_the_posted_patch_notes_are_never_changed():
    notes = json.loads((ROOT / "config" / "patchNotes.json").read_text(encoding="utf-8"))
    posted = sorted((note for note in notes if int(note["id"]) in POSTED_PATCH_NOTES), key=lambda note: int(note["id"]))
    assert [int(note["id"]) for note in posted] == list(POSTED_PATCH_NOTES)
    text = json.dumps(posted, ensure_ascii=False, sort_keys=True)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == POSTED_PATCH_NOTES_SHA256
    # (and their "$" mentions stay: "$" still works, so they stay true)
    for mention in ("$kradnijxp", "$porzucam", "$rankingalbumow", "$wyjmijjajko", "$wj", "$stajnia"):
        assert mention in text, mention


def test_the_bad_slot_hint_names_the_command_with_the_exclamation_mark():
    assert pets._msg_bad_slot(1, "schowajtowarzysza").endswith("Wpisz np. **!schowajtowarzysza 1**.")
    assert pets._msg_bad_slot(1, "wyciagnijtowarzysza", 6) == (
        "Twoja stajnia ma 6 miejsc - od 1 do 6 <@1>! Wpisz np. **!wyciagnijtowarzysza 1**.")
