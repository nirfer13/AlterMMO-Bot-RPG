"""The fight archive and the arrival times of the answers (core/fightlog.py, tools/fight_lag_report.py).

Owner, 2026-11-26: "Zrób 2 i zapisuj dane z 1 testowo ... Później loguj te
wszystkie dane na dłużej i załóżmy po tygodniu przeanalizujemy te wyniki.
Zrób dobre logowanie."  (2: the fight journal kept longer than 30 minutes;
1: the time the player's app put into the nonce, recorded only.)
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from altermmo.core import fightlog
from altermmo.core.fightlog import End, LateState, Outcome

from fakes import FakeMessage

ROOT = Path(__file__).resolve().parent.parent


def snowflake(moment: datetime) -> int:
    return (int(moment.timestamp() * 1000) - fightlog.DISCORD_EPOCH_MS) << 22


def load_report():
    spec = importlib.util.spec_from_file_location("fight_lag_report", ROOT / "tools" / "fight_lag_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # (its dataclass looks itself up there)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def archive(tmp_path):
    folder = tmp_path / "fights"
    fightlog.configure_archive(folder)
    yield folder
    fightlog.configure_archive(None)


def lines(folder: Path) -> list[dict]:
    assert fightlog.flush_archive()
    found = []
    for path in sorted(folder.glob("fights-*.jsonl")):
        found += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return found


class Fight:
    """A fight written into the journal by hand, with exact times (as ``challenge.ask`` does it)."""

    def __init__(self, game, player, *, kind=fightlog.KIND_BOSS, detail=4) -> None:
        self.game, self.player = game, player
        self.channel = game.channel
        self.fight = fightlog.begin(self.channel, kind, [player], detail=detail)
        self.prompts: dict[int, FakeMessage] = {}

    def _ask(self, turn, word, limit):
        entry = fightlog.open_round(self.channel.id, turn=turn, asked=self.player, word=word,
                                    prompt=f"wpisz **{word}**", limit=limit, players={self.player.id})
        prompt = FakeMessage(self.channel, self.game.bot.fake_user, "pytanie")
        prompt.created_at = datetime(2026, 11, 26, 7, 25, turn, tzinfo=timezone.utc)
        entry.sent(prompt, latency=0.13, grace=1.0, send_time=0.05)
        self.prompts[turn] = prompt
        return entry, prompt

    def message(self, turn, text, after, *, upload=0.2, nonce="auto"):
        """The player's message created ``after`` s after prompt ``turn``, sent ``upload`` s earlier."""
        message = FakeMessage(self.channel, self.player, text)
        message.created_at = self.prompts[turn].created_at + timedelta(seconds=after)
        message.nonce = (str(snowflake(message.created_at - timedelta(seconds=upload)))
                         if nonce == "auto" else nonce)
        return message

    def answer(self, turn, word, limit, after, *, upload=0.2, nonce="auto"):
        entry, _ = self._ask(turn, word, limit)
        entry.answered(self.message(turn, word, after, upload=upload, nonce=nonce), after)
        return entry.entry

    def timeout(self, turn, word, limit, *, in_grace=None, upload=0.2):
        entry, _ = self._ask(turn, word, limit)
        if in_grace is not None:
            entry.late_in_grace(self.message(turn, word, in_grace, upload=upload), in_grace)
        entry.silent()
        entry.timed_out()
        return entry.entry

    async def late(self, turn, text, after, *, upload=0.2):
        message = self.message(turn, text, after, upload=upload)
        self.channel.messages.append(message)
        self.game.bot.dispatch("message", message)
        await self.game.settle()
        return message


# -- the nonce ------------------------------------------------------------------------------------

def test_a_snowflake_nonce_is_the_moment_of_sending():
    created = datetime(2026, 11, 26, 7, 25, 20, 490000, tzinfo=timezone.utc)
    sent = created - timedelta(seconds=1.25)
    assert fightlog.nonce_time(snowflake(sent), created) == sent.replace(microsecond=sent.microsecond // 1000 * 1000)
    assert fightlog.nonce_time(str(snowflake(sent)), created) is not None        # (a string of digits)
    for not_a_time in (None, "", "abc", "12ab", "-5", 0, True, "9" * 25, 1234):
        assert fightlog.nonce_time(not_a_time, created) is None, not_a_time
    far = snowflake(created - timedelta(days=3))                                  # not near the message
    assert fightlog.nonce_time(far, created) is None


def test_the_arrival_of_a_message(game):
    member = game.add_member("Ala")
    message = FakeMessage(game.channel, member, "atak")
    message.created_at = datetime(2026, 11, 26, 7, 0, 0, tzinfo=timezone.utc)
    message.nonce = str(snowflake(message.created_at - timedelta(seconds=0.4)))
    received = message.created_at + timedelta(seconds=0.09)
    arrival = fightlog.arrival_of(message, received)
    assert arrival.message_id == message.id and arrival.nonce == message.nonce
    assert arrival.upload == pytest.approx(0.4, abs=0.002) and arrival.delivery == pytest.approx(0.09)
    plain = FakeMessage(game.channel, member, "atak")                             # no nonce at all
    assert fightlog.arrival_of(plain).nonce is None and fightlog.arrival_of(plain).upload is None
    odd = FakeMessage(game.channel, member, "atak")
    odd.nonce = "web-client-1"
    assert fightlog.arrival_of(odd).nonce == "web-client-1" and fightlog.arrival_of(odd).upload is None
    assert fightlog.arrival_of(object()) is not None                              # never raises


# -- the archive ----------------------------------------------------------------------------------

async def test_a_lost_fight_is_archived_with_every_round_and_its_times(game, archive, caplog):
    hunter = game.add_member("eglerio_")
    caplog.set_level(logging.INFO, logger="altermmo.core.fightlog")
    recorded = Fight(game, hunter)
    for turn in range(1, 7):
        recorded.answer(turn, "uskok", 2.73, 1.6, upload=0.20 + turn / 100)
    recorded.timeout(7, "agresja", 2.73, in_grace=3.4, upload=1.45)              # 1.24 s over its best
    fightlog.lost(game.channel, timed_out=True)

    [record] = lines(archive)
    assert archive.joinpath(f"fights-{record['started_at'][:10]}.jsonl").exists()      # the UTC day it began
    assert record["v"] == fightlog.ARCHIVE_VERSION and record["kind"] == "boss" and record["detail"] == 4
    assert record["end"] == "lost" and record["decisive_round"] == 7 and record["losers"] == [hunter.id]
    assert record["fighters"] == [{"id": hunter.id, "name": "eglerio_"}]
    assert [entry["outcome"] for entry in record["rounds"]] == ["correct"] * 6 + ["timeout"]
    first = record["rounds"][0]
    assert first["arrival"]["upload"] == pytest.approx(0.21, abs=0.002) and first["elapsed"] == 1.6
    assert first["latency"] == 0.13 and first["grace"] == 1.0 and first["prompt_at"].startswith("2026-11-26T07:25:01")
    last = record["rounds"][-1]
    assert last["arrival"] is None and last["late"]["in_grace"] is True
    assert last["late"]["after"] == 3.4 and last["late"]["over"] == pytest.approx(0.67)
    assert last["late"]["arrival"]["upload"] == pytest.approx(1.45, abs=0.002)
    assert last["late"]["arrival"]["nonce_at"] and last["late"]["arrival"]["created_at"]

    lost = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Fight lost:"))
    assert "client send +1.45 s (+1.24 s over this fight's best of 7)" in lost and "Discord->bot" in lost
    summary = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Fight archived:"))
    assert "lost, 7 round(s), client send time in 7/7 answer(s)" in summary
    assert "slowest send +1.24 s over the player's best (round 7)" in summary


async def test_an_ended_fight_waits_for_its_late_answer_before_it_is_archived(game, archive):
    hunter = game.add_member("Ala")
    recorded = Fight(game, hunter, kind=fightlog.KIND_TOWER, detail=6)
    recorded.answer(1, "blok", 1.33, 0.9)
    recorded.answer(2, "atak", 1.33, 1.0)
    recorded.timeout(3, "impet", 1.33)
    fightlog.lost(game.channel, timed_out=True)
    assert lines(archive) == []                                                   # still listening
    await recorded.late(3, "impet", 1.71, upload=0.6)
    [record] = lines(archive)
    late = record["rounds"][-1]["late"]
    assert late["after"] == 1.71 and late["in_grace"] is False
    assert late["arrival"]["upload"] == pytest.approx(0.6, abs=0.002)
    assert record["rounds"][-1]["late_state"] == LateState.FOUND.value


async def test_won_drawn_interrupted_and_never_ended_fights_are_archived_once(game, archive):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    won = Fight(game, ala)
    won.answer(1, "unik", 7, 2.0, nonce=None)                                     # (an answer without a nonce)
    fightlog.won(game.channel)
    fightlog.won(game.channel)                                                    # closed already: nothing more
    Fight(game, ola, kind=fightlog.KIND_DUEL, detail=None)
    fightlog.draw(game.channel)
    Fight(game, ala, kind=fightlog.KIND_HUNT, detail=1)
    fightlog.interrupted(game.channel, "gateway")
    Fight(game, ola, kind=fightlog.KIND_HUNT, detail=2)                           # raised instead of ending ...
    Fight(game, ala, kind=fightlog.KIND_HUNT, detail=3)                           # ... and the next one began
    records = lines(archive)
    assert [(r["kind"], r["detail"], r["end"]) for r in records] == [
        ("boss", 4, "won"), ("duel", None, "draw"), ("hunt", 1, "interrupted"), ("hunt", 2, None)]
    assert records[0]["rounds"][0]["arrival"]["nonce"] is None
    assert records[2]["reason"] == "gateway"
    await fightlog.stop()                                                         # shutdown: the running one too
    assert [r["detail"] for r in lines(archive)][-1] == 3 and len(lines(archive)) == 5


def test_old_days_are_deleted_and_nothing_else(game, archive):
    archive.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    old = archive / f"fights-{(today - timedelta(days=fightlog.ARCHIVE_KEEP_DAYS + 1)).isoformat()}.jsonl"
    kept = archive / f"fights-{(today - timedelta(days=fightlog.ARCHIVE_KEEP_DAYS - 1)).isoformat()}.jsonl"
    other = archive / "notes.txt"
    for path in (old, kept, other):
        path.write_text("{}\n", encoding="utf-8")
    Fight(game, game.add_member("Ala"))
    fightlog.won(game.channel)
    lines(archive)
    assert not old.exists() and kept.exists() and other.exists()


def test_a_broken_disk_costs_the_line_never_the_fight(game, tmp_path, caplog):
    blocked = tmp_path / "file-not-folder"
    blocked.write_text("x", encoding="utf-8")
    fightlog.configure_archive(blocked)                                           # mkdir fails there
    try:
        Fight(game, game.add_member("Ala"))
        fight = fightlog.won(game.channel)
        assert fight is not None and fight.end is End.WON and fight.archived
        assert fightlog.flush_archive()
        assert any("writing a fight" in r.getMessage() for r in caplog.records)
    finally:
        fightlog.configure_archive(None)


def test_without_an_archive_nothing_is_written_and_reset_switches_it_off(game, tmp_path):
    folder = tmp_path / "fights"
    fightlog.configure_archive(folder)
    assert fightlog.archive_directory() == folder
    fightlog.reset()
    assert fightlog.archive_directory() is None
    Fight(game, game.add_member("Ala"))
    assert fightlog.won(game.channel).archived is False
    assert not folder.exists()


async def test_the_bot_archives_into_its_log_folder(game, monkeypatch, tmp_path):
    from altermmo import bot as bot_module
    from altermmo.features import petcards

    monkeypatch.setattr(petcards, "pillow_available", lambda: True)

    async def refused(self):
        raise ConnectionRefusedError("test: no database here")

    async def load_extension(name):
        pass

    monkeypatch.setattr(type(game.bot.db), "connect", refused)
    monkeypatch.setattr(game.bot, "load_extension", load_extension)
    monkeypatch.setattr(bot_module, "PROJECT_ROOT", tmp_path)
    await game.bot.setup_hook()
    assert fightlog.archive_directory() == game.bot.settings.log_dir / "fights"


# -- the report -----------------------------------------------------------------------------------

async def test_the_report_tells_a_send_lag_from_a_slow_answer(game, archive):
    report = load_report()
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    lagged = Fight(game, ala)
    for turn in range(1, 5):
        lagged.answer(turn, "uskok", 2.0, 1.2, upload=0.15)
    lagged.answer(5, "parada", 2.0, 1.4, upload=0.75)                             # in time despite 0.6 s
    lagged.timeout(6, "agresja", 2.0, in_grace=2.9, upload=1.15)                  # 1.0 s lag, 0.9 s over
    fightlog.lost(game.channel, timed_out=True)
    slow = Fight(game, ola)
    for turn in range(1, 4):
        slow.answer(turn, "uskok", 2.0, 1.5, upload=0.3)
    slow.timeout(4, "agresja", 2.0, in_grace=2.9, upload=0.35)                    # no lag: just slow
    fightlog.lost(game.channel, timed_out=True)
    lines(archive)

    text = report.report(report.read_fights(archive, None), None)
    assert "Walki: 2" in text and "Wyniki: lost 2" in text
    assert "Odpowiedzi: 10, z nonce 10, z czasem wysłania 10 (100%)" in text
    ala_line = next(line for line in text.splitlines() if "Ala, boss mityczny" in line)
    assert "'agresja'" in ala_line and "+0.90 s" in ala_line and "+1.00 s" in ala_line
    assert "LAG WYSYŁANIA to tłumaczy" in ala_line and "[przegrała walkę]" in ala_line
    ola_line = next(line for line in text.splitlines() if "Ola, boss mityczny" in line)
    assert "nie lag wysyłania" in ola_line
    assert "Odpowiedzi w czasie mimo lagu wysyłania ≥0.3 s:\n  1: Ala +0.60 s" in text
    only = report.report(report.read_fights(archive, None), "ola")
    assert "Walki: 1" in only and "Ala" not in only
    assert report.report([], None) == "Walki: 0"
    assert report.verdict(0.9, None) == "brak czasu wysłania"
    assert report.verdict(0.9, 0.4) == "lag był, ale za mały"
