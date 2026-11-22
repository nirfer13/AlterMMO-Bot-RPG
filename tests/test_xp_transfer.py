"""XP theft settled by Horus (protocol version 1, SPEC_xp_transfer.md): the RPG bot's side.

* the request: exact fields, written atomically, one per right, every failure point of
  ``xpsteal.steal`` (write -> spend -> put in place), two ``$kradnijxp`` at once;
* the results (``TransferWatch``): full / partial / nothing / error, broken and foreign files,
  a fight in progress, a failing send, the bounded duplicate rule;
* the owner's note about requests Horus has not settled after ten minutes;
* the loop: supervised, survives exceptions, leaves no task behind;
* the data folder: the sub-folders, the backup, the start-up check, debug mode.

Owner's decision of 2026-11-22: "Niech kradnie tyle, ile ma ofiara [...] To już wina złodzieja,
że wybrał sobie lichą ofiarę".  The victim is never named on a public channel.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import re
import time
import uuid
from datetime import datetime, timezone

import discord
import pytest
from fakes import settle_xp_transfers, write_xp_transfer_result, xp_transfer_requests

from altermmo.core import challenge
from altermmo.features import boss, xpsteal

NBSP = chr(0xA0)


@pytest.fixture
def thief(game):
    member = game.add_member("Zlodziej")
    xpsteal.grant(game.bot, member.id, 10000)
    return member


@pytest.fixture
def victim(game):
    return game.add_member("Biedak")


@pytest.fixture
def watch(game):
    return xpsteal.TransferWatch(game.bot)


def files(folder: pathlib.Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def owner_pings(game) -> list[str]:
    return [m.content for m in game.log_channel.messages if m.content.startswith(f"<@{game.bot.ids.owner}>!")]


def patch_replace(monkeypatch, folder: pathlib.Path, behaviour) -> None:
    """``os.replace`` into ``folder`` calls ``behaviour(source, target, real_replace)``; everything else
    (e.g. the atomic save of ``xpThefts.json``) is left alone."""
    real_replace = os.replace

    def replace(source, target, *args, **kwargs):
        if pathlib.Path(target).parent == folder:
            return behaviour(pathlib.Path(source), pathlib.Path(target), real_replace)
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def fail(error: BaseException):
    def behaviour(source, target, real_replace):
        raise error
    return behaviour


def assert_secret(game, victim) -> None:
    """The victim's name and id never appear on the game channel (deleted messages included)."""
    for message in game.channel.sent:
        assert "Biedak" not in (message.content or "") and str(victim.id) not in (message.content or "")


# ===================================== the request ===============================================

async def test_request_file_has_exactly_the_protocol_fields(game, thief, victim):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    assert await xpsteal.steal(game.scene(thief, "$kradnijxp Biedak"), "Biedak") is True

    folder = game.bot.store.xp_transfers_dir
    assert folder == game.bot.settings.data_dir / "xp_transfers"
    (name,) = files(folder)                                         # no temporary file is left
    assert re.fullmatch(r"[0-9a-f]{32}\.json", name)
    raw = (folder / name).read_bytes()
    request = json.loads(raw.decode("utf-8"))
    assert list(request) == ["version", "id", "from", "to", "amount", "created"]
    assert request["version"] == 1 and request["id"] == name[:-5] and uuid.UUID(hex=request["id"]).version == 4
    assert request["from"] == str(victim.id) and request["to"] == str(thief.id)          # strings of digits
    assert isinstance(request["amount"], int) and request["amount"] == 10000
    created = datetime.fromisoformat(request["created"])
    assert created.utcoffset().total_seconds() == 0 and before <= created <= datetime.now(timezone.utc)
    assert xp_transfer_requests(game.bot) == [request]
    assert_secret(game, victim)


async def test_request_is_written_under_its_temporary_name_then_put_in_place(game, thief, victim, monkeypatch):
    seen = []

    def replace(source, target, real_replace):
        seen.append((source.name, target.name, json.loads(source.read_text(encoding="utf-8")),
                     target.exists(), xpsteal.pending(game.bot, thief.id)))
        real_replace(source, target)

    patch_replace(monkeypatch, game.bot.store.xp_transfers_dir, replace)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is True
    ((source, target, content, target_existed, rights_then),) = seen
    assert source == target + ".tmp" and not target_existed            # <id>.json.tmp -> <id>.json
    assert content["id"] + ".json" == target and content["amount"] == 10000
    assert rights_then == []                                          # the right was spent BEFORE
    assert files(game.bot.store.xp_transfers_dir) == [target]


async def test_one_request_per_right(game, thief, victim):
    xpsteal.grant(game.bot, thief.id, 5000)
    for _ in range(4):
        await xpsteal.steal(game.scene(thief), "Biedak")
    requests = xp_transfer_requests(game.bot)
    assert sorted(request["amount"] for request in requests) == [5000, 10000]
    assert len({request["id"] for request in requests}) == 2
    assert game.channel.count("nie masz żadnej kradzieży") == 2
    assert game.bot.store.exp_summary.load() == {}                  # nothing in the experience summary


async def test_two_thefts_at_once_spend_the_right_once(game, thief, victim, monkeypatch):
    from altermmo.core import io

    real_delete = io.safe_delete

    async def slow_delete(message):                                 # both commands get past the first check
        await asyncio.sleep(0.01)
        return await real_delete(message)

    monkeypatch.setattr(xpsteal.io, "safe_delete", slow_delete)
    other = game.add_member("Biedak2")
    outcomes = await asyncio.gather(xpsteal.steal(game.scene(thief), "Biedak"),
                                    xpsteal.steal(game.scene(thief), "Biedak2"))
    assert sorted(outcomes) == [False, True]
    (request,) = xp_transfer_requests(game.bot)
    assert request["from"] == str((victim if outcomes[0] else other).id)
    assert files(game.bot.store.xp_transfers_dir) == [request["id"] + ".json"]
    assert xpsteal.pending(game.bot, thief.id) == [] and game.channel.count("nie masz żadnej kradzieży") == 1
    assert game.bot.store.exp_summary.load() == {}


# -- every failure point of steal ---------------------------------------------------------------

async def test_temporary_file_cannot_be_written(game, thief, victim, monkeypatch):
    def broken(path, request):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": 1, "id"', encoding="utf-8")   # half written, then the disk is full
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(xpsteal, "_write_temp", broken)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    assert files(game.bot.store.xp_transfers_dir) == []              # the half-written file is gone
    assert xpsteal.pending(game.bot, thief.id) == [10000]            # the right is kept
    assert game.channel.messages[-1].content == xpsteal.MSG_NOT_SAVED.format(thief=thief.id)
    (ping,) = owner_pings(game)
    assert "Zlodziej" in ping and "Biedak" in ping and f"10{NBSP}000 expa" in ping and "zachował okazję" in ping
    assert_secret(game, victim)


async def test_right_cannot_be_spent(game, thief, victim, monkeypatch):
    def broken(value):
        raise OSError("disk full")

    monkeypatch.setattr(game.bot.store.xp_thefts, "save", broken)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    assert files(game.bot.store.xp_transfers_dir) == []              # no request without the right spent
    monkeypatch.undo()
    assert xpsteal.pending(game.bot, thief.id) == [10000]
    assert game.channel.messages[-1].content == xpsteal.MSG_NOT_SAVED.format(thief=thief.id)
    assert len(owner_pings(game)) == 1 and "zachował okazję" in owner_pings(game)[0]


async def test_right_saved_although_the_save_reported_an_error(game, thief, victim, monkeypatch):
    store = game.bot.store.xp_thefts
    real_save = store.save

    def saved_then_failed(value):
        real_save(value)
        raise OSError("reported late")

    monkeypatch.setattr(store, "save", saved_then_failed)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is True   # spent: the theft goes on
    assert xpsteal.pending(game.bot, thief.id) == []
    assert [request["amount"] for request in xp_transfer_requests(game.bot)] == [10000]
    assert owner_pings(game) == []


async def test_request_cannot_be_put_in_place_the_right_is_given_back(game, thief, victim, monkeypatch):
    xpsteal.grant(game.bot, thief.id, 5000)
    patch_replace(monkeypatch, game.bot.store.xp_transfers_dir, fail(OSError("the folder went away")))
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    assert files(game.bot.store.xp_transfers_dir) == []              # temporary file deleted
    assert xpsteal.pending(game.bot, thief.id) == [10000, 5000]       # given back, in its place
    assert game.channel.messages[-1].content == xpsteal.MSG_NOT_SAVED.format(thief=thief.id)
    (ping,) = owner_pings(game)
    assert "zachował okazję" in ping and "Usuń też plik" not in ping


async def test_replace_waits_out_a_reader(game, thief, victim, monkeypatch):
    calls = []

    def busy(source, target, real_replace):
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError("held by the antivirus")
        real_replace(source, target)

    patch_replace(monkeypatch, game.bot.store.xp_transfers_dir, busy)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is True
    assert len(calls) == 3 and len(xp_transfer_requests(game.bot)) == 1


async def test_right_lost_and_not_given_back_is_reported_with_the_exact_numbers(game, thief, victim,
                                                                             monkeypatch):
    store = game.bot.store.xp_thefts
    real_save = store.save
    saves = []

    def first_only(value):
        saves.append(value)
        if len(saves) > 1:
            raise OSError("disk died")
        real_save(value)

    monkeypatch.setattr(store, "save", first_only)
    patch_replace(monkeypatch, game.bot.store.xp_transfers_dir, fail(OSError("gone")))
    monkeypatch.setattr(xpsteal, "_discard", lambda path: False)     # the temporary file stays, too
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    assert xpsteal.pending(game.bot, thief.id) == []                 # spent, and could not be given back
    assert game.channel.messages[-1].content == xpsteal.MSG_RIGHT_HELD.format(thief=thief.id)
    (ping,) = owner_pings(game)
    assert f"$DajKradziez {thief.id} 10000" in ping and "Zlodziej" in ping and "Biedak" in ping
    (temp,) = files(game.bot.store.xp_transfers_dir)
    assert temp.endswith(".json.tmp") and temp in ping and "nie zmieniaj jego nazwy" in ping
    assert xp_transfer_requests(game.bot) == []                      # Horus sees no request
    assert_secret(game, victim)


def lock_rights_file(monkeypatch, store, when) -> None:
    """Reading ``xpThefts.json`` raises PermissionError while ``when()`` (another program holds it)."""
    real_read = pathlib.Path.read_text

    def read_text(self, *args, **kwargs):
        if self == store.path and when():
            raise PermissionError(13, "The process cannot access the file")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)


async def test_right_not_spent_and_file_unreadable_gives_no_request(game, thief, victim, monkeypatch):
    """The save fails AND the file cannot be read back: "unknown" is never taken for "spent"."""
    store = game.bot.store.xp_thefts
    locked = {"on": False}

    def save_locked(value):
        locked["on"] = True                                         # another program holds the file
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(store, "save", save_locked)
    lock_rights_file(monkeypatch, store, lambda: locked["on"])
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    monkeypatch.undo()
    assert files(game.bot.store.xp_transfers_dir) == []              # no request, no temporary file
    assert xpsteal.pending(game.bot, thief.id) == [10000]            # the right is still there
    assert game.channel.messages[-1].content == xpsteal.MSG_NOT_SAVED.format(thief=thief.id)
    (ping,) = owner_pings(game)
    assert "nie da się teraz odczytać" in ping and "Zlodziej" in ping and "Biedak" in ping
    assert f"okazji: 1 (10{NBSP}000)" in ping and f"$DajKradziez {thief.id} 10000" in ping
    assert_secret(game, victim)


@pytest.mark.parametrize("how", ["locked", "broken"])
async def test_give_back_never_saves_over_a_file_it_cannot_read(game, thief, victim, monkeypatch, how):
    """The rename fails and ``xpThefts.json`` cannot be read: the other players' rights stay."""
    xpsteal.grant(game.bot, 999, 10000)                              # somebody else's right
    store = game.bot.store.xp_thefts
    state = {"locked": False}

    def behaviour(source, target, real_replace):
        if how == "locked":
            state["locked"] = True
        else:
            store.path.write_text('{"999": [10000], ', encoding="utf-8")   # broken meanwhile
        raise OSError("rename failed")

    patch_replace(monkeypatch, game.bot.store.xp_transfers_dir, behaviour)
    lock_rights_file(monkeypatch, store, lambda: state["locked"])
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    monkeypatch.undo()
    if how == "broken":
        assert store.path.read_text(encoding="utf-8") == '{"999": [10000], '   # not overwritten
        store.save({"999": [10000]})                                  # (the owner repairs it)
    assert xpsteal.pending(game.bot, 999) == [10000]
    assert xpsteal.pending(game.bot, thief.id) == []                  # spent, not given back...
    assert game.channel.messages[-1].content == xpsteal.MSG_RIGHT_HELD.format(thief=thief.id)
    (ping,) = owner_pings(game)
    assert f"$DajKradziez {thief.id} 10000" in ping                   # ...and reported with the numbers
    assert files(game.bot.store.xp_transfers_dir) == []
    assert_secret(game, victim)


async def test_stuck_temporary_file_never_gets_a_rename_instruction(game, thief, victim, monkeypatch):
    """The rename and the delete both fail (an antivirus holds the file); the thief then steals again.
    The owner must not be told later to rename the old file: Horus would settle one right twice."""
    folder = game.bot.store.xp_transfers_dir
    real_unlink = pathlib.Path.unlink
    held: set[pathlib.Path] = set()

    def behaviour(source, target, real_replace):
        if not held or source in held:
            held.add(source)
            raise PermissionError(13, "held by the antivirus")
        return real_replace(source, target)

    def unlink(self, *args, **kwargs):
        if self in held:
            raise PermissionError(13, "held by the antivirus")
        return real_unlink(self, *args, **kwargs)

    patch_replace(monkeypatch, folder, behaviour)
    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    (first,) = owner_pings(game)
    assert "nie zmieniaj jego nazwy" in first and xpsteal.pending(game.bot, thief.id) == [10000]
    assert await xpsteal.steal(game.scene(thief), "Biedak") is True   # tried again, as he was asked
    assert xpsteal.pending(game.bot, thief.id) == []
    (temp,) = held
    age(temp, 3600)
    await xpsteal.TransferWatch(game.bot).check()
    assert owner_pings(game) == [first]                               # no second, contradicting note
    del game.bot._xp_temps_reported                                   # after a restart: noted again...
    await xpsteal.TransferWatch(game.bot).check()
    second = owner_pings(game)[-1]
    assert temp.name in second and "nigdy nie zmieniaj ich nazwy" in second   # ...and never "rename"
    assert "zmień nazwę" not in second
    assert len(xp_transfer_requests(game.bot)) == 1


async def test_confirmation_that_cannot_be_posted_does_not_undo_the_theft(game, thief, victim):
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("Discord is down"), 1
    assert await xpsteal.steal(game.scene(thief), "Biedak") is True
    assert len(xp_transfer_requests(game.bot)) == 1 and xpsteal.pending(game.bot, thief.id) == []


# ===================================== the results ================================================

async def test_full_theft_is_announced_to_the_thief_only(game, thief, victim, watch, monkeypatch):
    sent_kwargs = []
    real_send = game.channel.send

    async def send(content=None, **kwargs):
        sent_kwargs.append(kwargs)
        return await real_send(content, **kwargs)

    monkeypatch.setattr(game.channel, "send", send)
    await xpsteal.steal(game.scene(thief), "Biedak")
    (result,) = settle_xp_transfers(game.bot, {victim.id: 50000})
    await watch.check()

    text = game.channel.messages[-1].content
    assert text == xpsteal.MSG_RESULT_FULL.format(thief=thief.id, moved="10" + NBSP + "000")
    assert text.startswith(f"🥷 <@{thief.id}>, Twoja kradzież przyniosła **10{NBSP}000** doświadczenia")
    mentions = sent_kwargs[-1]["allowed_mentions"]
    assert [user.id for user in mentions.users] == [thief.id] and not mentions.everyone and not mentions.roles
    assert files(game.bot.store.xp_transfer_results_dir) == []       # deleted after the announcement
    line = game.bot.owner_log[-1]
    assert result["id"] in line and "Zlodziej" in line and "Biedak" in line and f"10{NBSP}000 z 10{NBSP}000" in line
    assert_secret(game, victim)
    assert game.bot.store.exp_summary.load() == {}


async def test_partial_theft_says_the_victim_had_no_more(game, thief, victim, watch):
    await xpsteal.steal(game.scene(thief), "Biedak")
    xp = {victim.id: 4000}
    settle_xp_transfers(game.bot, xp)
    assert xp == {victim.id: 0, thief.id: 4000}                      # what the victim lost, never more
    await watch.check()
    text = game.channel.messages[-1].content
    assert text.startswith(f"🥷 <@{thief.id}>, Twoja kradzież przyniosła **4{NBSP}000** doświadczenia")
    assert f"z 10{NBSP}000" in text and "nie miała więcej" in text
    assert f"4{NBSP}000 z 10{NBSP}000" in game.bot.owner_log[-1] and "Ofiara miała tylko tyle" in game.bot.owner_log[-1]
    assert_secret(game, victim)


async def test_theft_from_a_poor_victim_brings_nothing(game, thief, victim, watch):
    await xpsteal.steal(game.scene(thief), "Biedak")
    settle_xp_transfers(game.bot, {})
    await watch.check()
    assert game.channel.messages[-1].content == xpsteal.MSG_RESULT_NOTHING.format(thief=thief.id)
    assert "okazja przepadła" in game.channel.messages[-1].content
    assert f"ukradł 0 z 10{NBSP}000" in game.bot.owner_log[-1] and "kradzież przepadła" in game.bot.owner_log[-1]
    assert owner_pings(game) == [] and xpsteal.pending(game.bot, thief.id) == []
    assert_secret(game, victim)


async def test_error_result_tells_the_owner_and_the_thief(game, thief, victim, watch):
    write_xp_transfer_result(game.bot, thief=thief, victim=victim, moved=0, error="bad amount")
    await watch.check()
    assert game.channel.messages[-1].content == xpsteal.MSG_RESULT_ERROR.format(thief=thief.id)
    (ping,) = owner_pings(game)
    assert "bad amount" in ping and "Zlodziej" in ping and "Biedak" in ping
    assert f"$DajKradziez {thief.id} 10000" in ping
    assert files(game.bot.store.xp_transfer_results_dir) == []
    assert_secret(game, victim)


async def test_error_result_without_a_usable_thief_goes_to_the_owner_only(game, victim, watch):
    write_xp_transfer_result(game.bot, thief=None, victim="12x", requested=None, moved=0, error="bad to id")
    await watch.check()
    assert game.channel.sent == []
    (ping,) = owner_pings(game)
    assert "bad to id" in ping and "nieznany" in ping
    assert "Nikt nie dostał wiadomości" in ping and "złodzieja ani kwoty" in ping
    assert "`$DajKradziez" not in ping and "<id złodzieja>" not in ping   # no command that cannot work
    assert files(game.bot.store.xp_transfer_results_dir) == []


async def test_error_result_without_a_usable_amount_gives_no_invalid_command(game, thief, victim, watch):
    """Horus could not read the amount (requested null): no "$DajKradziez <id> ?" - the amount is
    unknown, and the owner is told where to find it."""
    await xpsteal.steal(game.scene(thief), "Biedak")
    (request,) = xp_transfer_requests(game.bot)
    write_xp_transfer_result(game.bot, thief=thief, victim=victim, requested=None, moved=0, error="bad amount",
                             transfer_id=request["id"])
    await watch.check()
    assert game.channel.messages[-1].content == xpsteal.MSG_RESULT_ERROR.format(thief=thief.id)
    (ping,) = owner_pings(game)
    assert "kwota nieznana" in ping and "odczytać kwoty" in ping and "Złodziej dostał wiadomość" in ping
    assert "$DajKradziez" in ping and "`$DajKradziez" not in ping and "?" not in ping.split("$DajKradziez")[1]
    assert f"Znajdź w tym logu wpis o zleceniu `{request['id']}`" in ping
    # ...and that entry is there, with the amount:
    assert any(request["id"] in line and f"do 10{NBSP}000 expa" in line for line in game.bot.owner_log)
    assert_secret(game, victim)


@pytest.mark.parametrize("content, problem", [
    (b"{not json", "not JSON"),
    (b"[1, 2]", "not a JSON object"),
    (None, "unsupported version"),
    (None, "id does not match the file name"),
    (None, "bad moved"),
    (None, "moved more than requested"),
])
async def test_broken_result_is_moved_aside_and_reported_once(game, thief, victim, watch, content, problem):
    changes = {"unsupported version": {"version": 2}, "id does not match the file name": {"id": "0" * 32},
               "bad moved": {"moved": "4000"}, "moved more than requested": {"moved": 10001}}.get(problem, {})
    path = write_xp_transfer_result(game.bot, thief=thief, victim=victim, **changes)
    if content is not None:
        path.write_bytes(content)
    await watch.check()
    await watch.check()
    assert game.channel.sent == []
    (ping,) = owner_pings(game)
    assert path.name in ping and problem in ping
    results = game.bot.store.xp_transfer_results_dir
    assert files(results) == ["rejected"] and files(results / "rejected") == [path.name]


async def test_broken_result_that_cannot_be_moved_is_reported_once(game, watch, monkeypatch):
    path = write_xp_transfer_result(game.bot, thief="1", victim="2")
    path.write_text("nonsense", encoding="utf-8")
    patch_replace(monkeypatch, game.bot.store.xp_transfer_results_dir / "rejected", fail(PermissionError("held")))
    for _ in range(3):
        await watch.check()
    assert len(owner_pings(game)) == 1 and "leży, gdzie leżał" in owner_pings(game)[0]
    assert path.exists() and game.channel.sent == []


async def test_foreign_names_are_ignored(game, thief, victim, watch):
    results = game.bot.store.xp_transfer_results_dir
    good = write_xp_transfer_result(game.bot, thief=thief, victim=victim)
    content = good.read_text(encoding="utf-8")
    good.unlink()
    foreign = ["notes.json", f"{'a' * 32}.json.tmp", f"{'A' * 32}.json", f"{'a' * 31}.json", f"{'a' * 32}.txt",
               f"{'a' * 32}.json.0123abcd.tmp", "desktop.ini"]
    for name in foreign:
        (results / name).write_text(content, encoding="utf-8")
    (results / f"{'b' * 32}.json").mkdir()                          # a folder with a result's name
    await watch.check()
    assert game.channel.sent == [] and game.bot.owner_log == []
    assert files(results) == sorted([*foreign, f"{'b' * 32}.json"])


async def test_nothing_is_announced_during_a_typed_fight(game, thief, victim, watch):
    await xpsteal.steal(game.scene(thief), "Biedak")
    settle_xp_transfers(game.bot, {victim.id: 20000})
    before = len(game.channel.sent)
    with challenge.round_running(game.channel):
        await watch.check()
        assert len(game.channel.sent) == before
        assert len(files(game.bot.store.xp_transfer_results_dir)) == 1   # waits for the next pass
    await watch.check()
    assert len(game.channel.sent) == before + 1 and files(game.bot.store.xp_transfer_results_dir) == []


async def test_failed_send_keeps_the_result_for_the_next_pass(game, thief, victim, watch):
    write_xp_transfer_result(game.bot, thief=thief, victim=victim, moved=3000)
    write_xp_transfer_result(game.bot, thief=thief, victim=victim, moved=2000)
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("Discord 503"), 1
    await watch.check()
    assert game.channel.sent == [] and len(files(game.bot.store.xp_transfer_results_dir)) == 2
    assert game.bot.owner_log == []                                  # nothing logged as done yet
    await watch.check()
    assert len(game.channel.sent) == 2 and files(game.bot.store.xp_transfer_results_dir) == []


async def test_long_failing_send_is_reported_once_and_still_retried(game, thief, victim, watch):
    write_xp_transfer_result(game.bot, thief=thief, victim=victim)
    passes = xpsteal.SEND_FAILURES_REPORTED + 3
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("Missing access"), passes
    for _ in range(passes):
        await watch.check()
    assert game.channel.sent == []
    assert sum("nie mogę napisać na kanale gry" in line for line in game.bot.owner_log) == 1
    await watch.check()
    assert len(game.channel.sent) == 1


async def test_a_result_is_announced_once_although_it_cannot_be_deleted(game, thief, victim, watch,
                                                                       monkeypatch):
    path = write_xp_transfer_result(game.bot, thief=thief, victim=victim)
    real_unlink = pathlib.Path.unlink

    def stuck(self, *args, **kwargs):
        if self.parent == game.bot.store.xp_transfer_results_dir:
            raise PermissionError("held by a backup")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", stuck)
    for _ in range(4):
        await watch.check()
    assert len(game.channel.sent) == 1 and path.exists()
    assert sum("Nie mogę usunąć pliku wyniku" in line for line in game.bot.owner_log) == 1
    monkeypatch.undo()
    await watch.check()
    assert not path.exists() and len(game.channel.sent) == 1


async def test_a_result_written_again_after_a_restart_is_not_announced_again(game, thief, victim, watch):
    path = write_xp_transfer_result(game.bot, thief=thief, victim=victim)
    content = path.read_text(encoding="utf-8")
    await watch.check()
    assert len(game.channel.sent) == 1
    path.write_text(content, encoding="utf-8")                      # Horus wrote it once more
    restarted = xpsteal.TransferWatch(game.bot)                     # a new run: nothing in memory
    await restarted.check()
    assert len(game.channel.sent) == 1 and not path.exists()        # only deleted


@pytest.mark.parametrize("written_saves", [True, False], ids=["written-note-saved", "no-note-saved"])
async def test_duplicates_are_bounded_when_the_note_cannot_be_saved(game, thief, victim, watch, monkeypatch,
                                                                     written_saves):
    """The announced note cannot be saved: this run remembers the id; the next start ignores the
    result because the id is no longer noted as waiting.  When that cannot be saved either (a crash
    between the send and both notes), one more announcement, at most once per start."""
    def broken(value):
        raise OSError("disk full")

    path = write_xp_transfer_result(game.bot, thief=thief, victim=victim)
    monkeypatch.setattr(game.bot.store.xp_transfers_announced, "save", broken)
    if not written_saves:
        monkeypatch.setattr(game.bot.store.xp_transfers_written, "save", broken)
    content = path.read_text(encoding="utf-8")
    await watch.check()
    path.write_text(content, encoding="utf-8")
    await watch.check()                                             # this run remembers it
    assert len(game.channel.sent) == 1 and not path.exists()
    path.write_text(content, encoding="utf-8")
    await xpsteal.TransferWatch(game.bot).check()                   # the next start
    assert len(game.channel.sent) == (1 if written_saves else 2)
    assert owner_pings(game) == []


async def test_old_notes_of_announced_results_are_dropped(game, thief, victim, watch):
    store = game.bot.store.xp_transfers_announced
    store.save({"a" * 32: time.time() - xpsteal.ANNOUNCED_KEEP - 1, "b" * 32: time.time() - 60, "c": "junk"})
    path = write_xp_transfer_result(game.bot, thief=thief, victim=victim)
    await watch.check()
    assert set(store.load()) == {"b" * 32, path.name[:-5]}


async def test_results_before_the_bot_is_connected_wait(game, thief, victim, watch):
    write_xp_transfer_result(game.bot, thief=thief, victim=victim)
    channel = game.bot.fake_channels.pop(game.bot.ids.boss_channel)
    await watch.check()
    assert len(files(game.bot.store.xp_transfer_results_dir)) == 1
    game.bot.fake_channels[game.bot.ids.boss_channel] = channel
    await watch.check()
    assert len(channel.sent) == 1


async def test_no_folders_nothing_to_do(game, watch):
    await watch.check()
    assert not game.bot.store.xp_transfers_dir.exists() and not game.bot.store.xp_transfer_results_dir.exists()
    assert game.bot.owner_log == [] and game.channel.sent == []


async def test_the_whole_way_with_horus(game, thief, victim, watch):
    await xpsteal.steal(game.scene(thief, "$kradnijxp Biedak"), "Biedak")
    await watch.check()                                             # Horus has not settled it yet
    assert len(game.channel.sent) == 1                              # only the confirmation
    settle_xp_transfers(game.bot, {victim.id: 7500})
    await watch.check()
    assert f"**7{NBSP}500**" in game.channel.messages[-1].content
    assert files(game.bot.store.xp_transfers_dir) == [] and files(game.bot.store.xp_transfer_results_dir) == []
    assert_secret(game, victim)
    assert game.bot.store.exp_summary.load() == {}


# ===================================== the ten-minute note =========================================

def age(path: pathlib.Path, seconds: float) -> None:
    when = time.time() - seconds
    os.utime(path, (when, when))


async def test_unsettled_request_is_reported_once(game, thief, victim, watch):
    await xpsteal.steal(game.scene(thief), "Biedak")
    xpsteal.grant(game.bot, thief.id, 10000)
    await xpsteal.steal(game.scene(thief), "Biedak")
    old, fresh = sorted(game.bot.store.xp_transfers_dir.iterdir())
    age(old, xpsteal.UNSETTLED_AFTER + 5)
    age(fresh, xpsteal.UNSETTLED_AFTER - 60)
    await watch.check()
    await watch.check()
    (ping,) = owner_pings(game)
    assert old.name in ping and fresh.name not in ping and "Horus nie działa" in ping
    assert "Nic nie przepadło" in ping and "Zlodziej" in ping and "Biedak" in ping and f"kwota 10{NBSP}000" in ping
    assert old.exists()                                             # the request waits, untouched
    age(fresh, xpsteal.UNSETTLED_AFTER + 1)
    await watch.check()
    assert len(owner_pings(game)) == 2 and fresh.name in owner_pings(game)[1] and old.name not in owner_pings(game)[1]
    await xpsteal.TransferWatch(game.bot).check()                   # after a restart: once more
    assert len(owner_pings(game)) == 3
    assert_secret(game, victim)


async def test_left_temporary_request_is_reported_once(game, watch):
    folder = game.bot.store.xp_transfers_dir
    folder.mkdir(parents=True)
    request = xpsteal.new_request(1111, 2222, 10000)
    temp = folder / f"{request['id']}.json.tmp"
    temp.write_text(json.dumps(request), encoding="utf-8")
    (folder / "rejected").mkdir()
    (folder / "rejected" / f"{'c' * 32}.json").write_text("{}", encoding="utf-8")   # Horus's, not ours
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    await watch.check()
    assert owner_pings(game) == []                                  # may still be being written
    age(temp, xpsteal.TEMP_LEFT_AFTER + 1)
    age(folder / "rejected" / f"{'c' * 32}.json", 3600)
    await watch.check()
    await watch.check()
    (ping,) = owner_pings(game)
    assert temp.name in ping and "xpThefts.json" in ping and "<@1111>" in ping and "<@2222>" in ping
    assert "nigdy nie zmieniaj ich nazwy" in ping and "zmień nazwę" not in ping      # delete, never rename
    assert "$DajKradziez 1111 10000" in ping and request["created"] in ping
    assert temp.exists()


async def test_many_unsettled_requests_make_one_short_note(game, watch):
    folder = game.bot.store.xp_transfers_dir
    folder.mkdir(parents=True)
    for _ in range(xpsteal.NOTE_LINES + 5):
        request = xpsteal.new_request(1111, 2222, 10000)
        path = folder / f"{request['id']}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        age(path, xpsteal.UNSETTLED_AFTER + 1)
    (folder / f"{'d' * 32}.json").write_text("broken", encoding="utf-8")
    age(folder / f"{'d' * 32}.json", xpsteal.UNSETTLED_AFTER + 30)
    await watch.check()
    (ping,) = owner_pings(game)
    assert "nie da się odczytać" in ping and "... i 6 więcej" in ping and len(ping) < 1990


# ===================================== the loop ====================================================

async def test_a_failing_step_does_not_stop_the_other(game, thief, victim, watch, monkeypatch):
    async def broken():
        raise RuntimeError("bug")

    monkeypatch.setattr(watch, "announce_results", broken)
    await xpsteal.steal(game.scene(thief), "Biedak")
    (path,) = game.bot.store.xp_transfers_dir.iterdir()
    age(path, xpsteal.UNSETTLED_AFTER + 1)
    await watch.check()                                             # does not raise
    assert len(owner_pings(game)) == 1


async def test_the_loop_is_supervised_survives_exceptions_and_leaves_no_task(game, thief, victim, monkeypatch):
    bot = game.bot
    monkeypatch.setattr(xpsteal, "POLL_EVERY", 0.01)
    await bot.load_extension("altermmo.cogs.xpsteal")
    cog = bot.get_cog("XpSteal")
    assert bot.supervisor.names().count(xpsteal.TRANSFERS_LOOP) == 1
    assert bot.supervisor.is_running(xpsteal.TRANSFERS_LOOP)

    real = cog.watch.announce_results
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("bug")
        return await real()

    monkeypatch.setattr(cog.watch, "announce_results", flaky)
    write_xp_transfer_result(bot, thief=thief, victim=victim, moved=1234)
    for _ in range(300):
        if game.channel.sent:
            break
        await asyncio.sleep(0.01)
    assert f"**1{NBSP}234**" in game.channel.sent[-1].content
    assert bot.supervisor.status()[xpsteal.TRANSFERS_LOOP]["restarts"] == 0   # survived without a restart

    await bot.unload_extension("altermmo.cogs.xpsteal")
    await asyncio.sleep(0)
    assert xpsteal.TRANSFERS_LOOP not in bot.supervisor.names()
    await asyncio.sleep(0.05)
    assert not [task for task in asyncio.all_tasks()
                if task.get_name() == f"supervised:{xpsteal.TRANSFERS_LOOP}" and not task.done()]


async def test_the_loop_is_cancelled_at_shutdown(game):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.xpsteal")
    (task,) = [task for task in asyncio.all_tasks() if task.get_name() == f"supervised:{xpsteal.TRANSFERS_LOOP}"]
    await bot.supervisor.stop_all()                                 # what AlterBot.close does
    assert task.done() and task.cancelled()


async def test_the_cog_is_loaded_with_the_bot():
    from altermmo.bot import EXTENSIONS

    assert "altermmo.cogs.xpsteal" in EXTENSIONS


# ===================================== the data folder =============================================

def test_folders_are_inside_the_data_folder_also_in_debug_mode(monkeypatch, tmp_path):
    from altermmo import settings
    from altermmo.bot import Stores

    for name in ("ALTERMMO_DATA_DIR", "ALTERMMO_LEGACY_DIR", "ALTERMMO_BACKUP_DIR"):
        monkeypatch.delenv(name, raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    for flag, folder in (("1", "data-debug"), ("0", "data")):
        monkeypatch.setenv("ALTERMMO_DEBUG", flag)
        data_dir = settings.load_settings(env_file=empty_env).data_dir
        assert data_dir.name == folder
        stores = Stores(data_dir)
        assert stores.xp_transfers_dir == data_dir / "xp_transfers"                 # SPEC names
        assert stores.xp_transfer_results_dir == data_dir / "xp_transfer_results"
        assert stores.xp_transfers_announced.path.parent == data_dir


async def test_backup_and_start_up_check_are_fine_with_the_sub_folders(game, thief, victim):
    from altermmo.features import backup

    bot = game.bot
    await xpsteal.steal(game.scene(thief), "Biedak")
    write_xp_transfer_result(bot, thief=thief, victim=victim)
    (bot.store.xp_transfer_results_dir / "rejected").mkdir()
    names = [name for name, _ in backup._read_state_files(bot.store.data_dir)]
    assert "xpThefts.json" in names and not any("xp_transfer" in name for name in names)
    bot._startup_notes.clear()
    bot._review_saved_state()
    assert not any("xp_transfer" in note for note in bot._startup_notes)


# ============================ only results of this bot's own requests (Horus's notes) ==============

async def test_the_id_is_noted_before_the_request_exists(game, thief, victim, monkeypatch):
    seen = []

    def replace(source, target, real_replace):
        seen.append(set(game.bot.store.xp_transfers_written.load()))
        real_replace(source, target)

    patch_replace(monkeypatch, game.bot.store.xp_transfers_dir, replace)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is True
    (request,) = xp_transfer_requests(game.bot)
    assert seen == [{request["id"]}]


async def test_a_result_for_an_id_this_bot_never_wrote_is_ignored(game, thief, victim, watch):
    """E.g. somebody's test file in the request folder: Horus answers it - the thief is not told
    anything, and the owner is not asked to give a right back (only a line in his log)."""
    path = write_xp_transfer_result(game.bot, thief=thief, victim=victim, moved=0, error="unsupported version",
                                    written=False)
    for _ in range(3):
        await watch.check()
    assert game.channel.sent == [] and owner_pings(game) == []
    (line,) = game.bot.owner_log
    assert path.name in line and "ten bot nie zlecił" in line and "nikomu nie trzeba oddawać okazji" in line
    assert "$DajKradziez" not in line and "unsupported version" in line
    results = game.bot.store.xp_transfer_results_dir
    assert files(results) == ["rejected"] and files(results / "rejected") == [path.name]


async def test_a_finished_theft_coming_back_from_a_backup_is_ignored(game, thief, victim, watch):
    """Months later (the announced note is forgotten) a broken copy of the request is restored and
    Horus answers it with an error result without ids: nothing is told, no right is given back."""
    await xpsteal.steal(game.scene(thief), "Biedak")
    (result,) = settle_xp_transfers(game.bot, {victim.id: 50000})
    await watch.check()
    assert len(game.channel.sent) == 2                               # the confirmation and the result
    assert result["id"] not in game.bot.store.xp_transfers_written.load()   # no longer waiting
    game.bot.store.xp_transfers_announced.save({})
    write_xp_transfer_result(game.bot, thief=None, victim=None, requested=None, moved=0, error="not JSON",
                             transfer_id=result["id"], written=False)
    await xpsteal.TransferWatch(game.bot).check()                     # (a later start)
    assert len(game.channel.sent) == 2 and owner_pings(game) == []
    assert "Pomijam wynik kradzieży" in game.bot.owner_log[-1] and "$DajKradziez" not in game.bot.owner_log[-1]


async def test_an_unreadable_note_of_written_ids_makes_results_wait(game, thief, victim, watch, monkeypatch):
    """Never taken for "no ids": the results would all be put aside unannounced."""
    await xpsteal.steal(game.scene(thief), "Biedak")
    settle_xp_transfers(game.bot, {victim.id: 50000})
    store = game.bot.store.xp_transfers_written
    good = store.path.read_text(encoding="utf-8")
    store.path.write_text('{"broken', encoding="utf-8")
    for _ in range(3):
        await watch.check()
    assert len(game.channel.sent) == 1 and len(files(game.bot.store.xp_transfer_results_dir)) == 1
    (ping,) = owner_pings(game)
    assert "xpTransfersWritten.json" in ping and "czekają" in ping
    # A new theft meanwhile is not made (its id could not be noted): the right stays.
    xpsteal.grant(game.bot, thief.id, 10000)
    assert await xpsteal.steal(game.scene(thief), "Biedak") is False
    assert game.channel.messages[-1].content == xpsteal.MSG_NOT_SAVED.format(thief=thief.id)
    assert xpsteal.pending(game.bot, thief.id) == [10000] and xp_transfer_requests(game.bot) == []
    assert store.path.read_text(encoding="utf-8") == '{"broken'                 # not overwritten
    store.path.write_text(good, encoding="utf-8")                              # the owner repairs it
    await watch.check()
    assert game.channel.messages[-1].content.startswith(f"🥷 <@{thief.id}>, Twoja kradzież przyniosła")


# ===================================== a theft right that cannot be stored ==========================

THEFT_LOOT = {"id": xpsteal.LOOT_ID, "descr": "Ukradnij wybranemu użytkownikowi 10000 doświadczenia na Discordzie",
              "weight": 2.5}


@pytest.mark.parametrize("how", ["locked", "broken"])
async def test_a_boss_drop_never_saves_over_a_rights_file_it_cannot_read(game, monkeypatch, how):
    """Before: the drop saved {winner: [amount]} over the file - every other player's rights lost."""
    winner = game.add_member("Zwyciezca")
    xpsteal.grant(game.bot, 999, 10000)                              # somebody else's unused right
    store = game.bot.store.xp_thefts
    if how == "broken":
        store.path.write_text('{"999": [10000], ', encoding="utf-8")
    else:
        lock_rights_file(monkeypatch, store, lambda: True)
    line = await boss._drop(game.scene(), winner, THEFT_LOOT, bonus=False)   # the fight goes on
    assert line == ""                                                # nothing granted, nothing shown
    monkeypatch.undo()
    if how == "broken":
        assert store.path.read_text(encoding="utf-8") == '{"999": [10000], '   # not overwritten
        store.save({"999": [10000]})
    assert xpsteal.pending(game.bot, 999) == [10000] and xpsteal.pending(game.bot, winner.id) == []
    (ping,) = owner_pings(game)                                       # one loud note, with the line
    assert f"`$DajKradziez {winner.id} 10000`" in ping and "Zwyciezca" in ping
    assert "nie został nadpisany" in ping and f"10{NBSP}000 expa" in ping


async def test_grant_never_raises_and_refuses_amounts_that_are_not_positive(game, monkeypatch):
    bot = game.bot
    assert xpsteal.grant(bot, 5, 2500) is True
    assert xpsteal.grant(bot, 5, 0) is False and xpsteal.grant(bot, 5, -100) is False
    monkeypatch.setattr(bot.store.xp_thefts, "save", lambda value: (_ for _ in ()).throw(OSError("disk full")))
    assert xpsteal.grant(bot, 5, 1000) is False
    monkeypatch.undo()
    assert xpsteal.pending(bot, 5) == [2500]
    # A hand-edited file: 0 and negative amounts are no rights (Horus would only reject the request).
    bot.store.xp_thefts.save({"6": [0, -5, 3000]})
    assert xpsteal.pending(bot, 6) == [3000]


async def test_the_owner_gives_a_right_by_hand(game):
    from fakes import FakeMessage

    await game.bot.load_extension("altermmo.cogs.xpsteal")
    for channel in game.bot.fake_channels.values():
        channel.type = discord.ChannelType.text                       # (as tests/test_achievements_cog.py)
    owner = game.add_member("Alter")
    owner.guild_permissions = discord.Permissions(administrator=True)
    player = game.add_member("Gracz")

    async def run(content):
        message = FakeMessage(game.channel, owner, content)
        message.edited_at = None
        message._state = None
        game.channel.messages.append(message)
        await game.bot.invoke(await game.bot.get_context(message))
        await game.settle()
        return game.channel.messages[-1].content

    answer = await run(f"$DajKradziez {player.id}")
    assert answer == xpsteal.MSG_GRANTED.format(player=player.id, amount=f"10{NBSP}000")   # "10 000", not "10000"
    assert await run(f"$DajKradziez {player.id} 0") == xpsteal.MSG_GRANT_BAD_AMOUNT
    game.bot.store.xp_thefts.path.write_text("[broken", encoding="utf-8")
    assert await run(f"$DajKradziez {player.id} 2500") == xpsteal.MSG_GRANT_NOT_SAVED.format(player=player.id)
    assert game.bot.store.xp_thefts.path.read_text(encoding="utf-8") == "[broken"


async def test_amounts_are_shown_one_way_everywhere(game, thief, victim, watch):
    """The drop ping, the private message, the confirmation and the result all say "10 000"."""
    await xpsteal.remind(game.scene(), thief)
    await xpsteal.steal(game.scene(thief), "Biedak")
    settle_xp_transfers(game.bot, {victim.id: 50000})
    await watch.check()
    texts = [m.content for m in game.channel.sent if m.content] + thief.dms
    assert len(texts) == 4 and all(f"10{NBSP}000" in text for text in texts)
    assert not any("10000" in text.replace(str(thief.id), "") for text in texts)


# ===================================== the owner's notes fit into Discord ===========================

def test_note_parts_keep_every_line_whole():
    head, lines = "H" * 50, ["a" * 40, "b" * 40, "c" * 40]
    assert xpsteal.note_parts(head, lines, limit=100) == ["H" * 50 + "\n" + "a" * 40, "b" * 40 + "\n" + "c" * 40]
    assert xpsteal.note_parts(head, [], limit=100) == [head]
    (only,) = xpsteal.note_parts("x" * 150, [], limit=100)
    assert len(only) == 100 and only.endswith("…")


@pytest.mark.parametrize("kind", ["unsettled", "temporary"])
async def test_the_longest_note_is_never_cut(game, watch, monkeypatch, kind):
    """Worst case: a 20-digit owner id, 19-digit player ids, 32-character names, ten listed files
    plus "... i N więcej" - computed, every message within Discord's limit and nothing cut off."""
    monkeypatch.setattr(game.bot, "ids", dataclasses.replace(game.bot.ids, owner=10 ** 20 - 1))
    folder = game.bot.store.xp_transfers_dir
    folder.mkdir(parents=True)
    names = []
    for n in range(xpsteal.NOTE_LINES + 3):
        thief = game.add_member(f"T{n:02d}" + "x" * 29, member_id=9_000_000_000_000_000_000 + n)
        victim = game.add_member(f"V{n:02d}" + "y" * 29, member_id=9_100_000_000_000_000_000 + n)
        request = xpsteal.new_request(thief.id, victim.id, 10000)
        path = folder / (f"{request['id']}.json" + ("" if kind == "unsettled" else ".tmp"))
        path.write_text(json.dumps(request), encoding="utf-8")
        age(path, xpsteal.UNSETTLED_AFTER + 60 + n)
        names.append(path.name)
    await watch.check()
    head = (xpsteal.MSG_OWNER_UNSETTLED.format(minutes=10, folder=folder) if kind == "unsettled"
            else xpsteal.MSG_OWNER_TEMP_LEFT.format(folder=folder))
    lines = watch._lines([(folder / name, xpsteal.UNSETTLED_AFTER + 60 + n) for n, name in enumerate(names)])
    assert len(head) + sum(len(line) + 1 for line in lines) > 1990     # one message would have been cut
    parts = xpsteal.note_parts(head, lines)
    assert len(parts) >= 2
    sent = [m.content for m in game.log_channel.sent]
    prefix = f"<@{10 ** 20 - 1}>!   "
    assert sent == [prefix + parts[0], *parts[1:]]                    # exactly what was computed: nothing cut
    assert all(len(text) <= 1990 for text in sent)
    shown = "\n".join(sent)
    assert sum(name in shown for name in names) == xpsteal.NOTE_LINES and "... i 3 więcej" in shown
    for line in lines:
        assert line in shown                                          # every line whole
