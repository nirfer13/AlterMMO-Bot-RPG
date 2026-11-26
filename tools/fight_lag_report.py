"""The report of the fight archive: does the players' lag explain their lost rounds?

Owner, 2026-11-26: "Zrób 2 i zapisuj dane z 1 testowo ... po tygodniu
przeanalizujemy te wyniki".  The bot writes every fight to
``logs/fights/fights-YYYY-MM-DD.jsonl`` (:mod:`altermmo.core.fightlog`); for
every answer it keeps the time the player's app put into the message's nonce
(the device's clock at Enter), Discord's timestamp and the moment the bot got
it.

Nonce -> Discord ("send") holds the offset of the device's clock, which is
constant over hours: only the excess over the same player's usual value is a
delay on the way up.  The baseline is a low percentile of the player's sends
of the same day (the offset may change from day to day, e.g. a clock sync).

Usage (from the bot's folder)::

    .venv\\Scripts\\python.exe tools\\fight_lag_report.py            # every archived day
    .venv\\Scripts\\python.exe tools\\fight_lag_report.py --days 7   # the last 7 days
    .venv\\Scripts\\python.exe tools\\fight_lag_report.py --player eglerio_

Reads the files only; stdlib only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
#: The bot accepts an answer created at most this much after the limit (core/challenge.py).
TOLERANCE = 0.25
#: An excess of the send time from this many seconds on is called a lag.
LAG = 0.30
#: The baseline of a player's day: this percentile of its sends (the minimum with few samples).
BASELINE_PERCENTILE = 5
BASELINE_MIN_SAMPLES = 20
#: Fewer answers of a player's day give no baseline at all.
BASELINE_LEAST = 3


@dataclass
class Sample:
    """One answer of a player (in time or late) with its times."""

    day: str
    player_id: int
    player: str
    fight: dict[str, Any]
    entry: dict[str, Any]
    late: bool
    upload: float | None
    delivery: float | None
    nonce: str | None
    excess: float | None = None


def read_fights(directory: Path, days: int | None) -> list[dict[str, Any]]:
    files = sorted(directory.glob("fights-*.jsonl"))
    if days is not None:
        first = (date.today() - timedelta(days=days - 1)).isoformat()
        files = [path for path in files if path.stem[len("fights-"):] >= first]
    fights = []
    for path in files:
        with open(path, encoding="utf-8") as file:
            for number, line in enumerate(file, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    fights.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"(pominięto uszkodzony wiersz {path.name}:{number})", file=sys.stderr)
    return fights


def names(fight: dict[str, Any]) -> dict[int, str]:
    return {fighter["id"]: fighter["name"] for fighter in fight.get("fighters", [])}


def samples_of(fights: Iterable[dict[str, Any]]) -> list[Sample]:
    found = []
    for fight in fights:
        day = str(fight.get("started_at") or "")[:10]
        who = names(fight)
        for entry in fight.get("rounds", []):
            pairs = [(entry.get("arrival"), entry.get("typed_by_id"), False)]
            late = entry.get("late") or {}
            pairs.append((late.get("arrival"), entry.get("asked_id"), True))
            for arrival, player_id, is_late in pairs:
                if not arrival or player_id is None:
                    continue
                found.append(Sample(day, player_id, who.get(player_id, str(player_id)), fight, entry, is_late,
                                    arrival.get("upload"), arrival.get("delivery"), arrival.get("nonce")))
    return found


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    position = (len(ordered) - 1) * percent / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def set_excess(samples: list[Sample]) -> None:
    """Each sample's send time over its player's baseline of that day."""
    by_day: dict[tuple[int, str], list[Sample]] = defaultdict(list)
    for sample in samples:
        if sample.upload is not None:
            by_day[(sample.player_id, sample.day)].append(sample)
    for group in by_day.values():
        uploads = [sample.upload for sample in group]
        if len(uploads) < BASELINE_LEAST:
            continue  # (no norm from one or two answers: their excess stays unknown)
        base = (percentile(uploads, BASELINE_PERCENTILE) if len(uploads) >= BASELINE_MIN_SAMPLES
                else min(uploads))
        for sample in group:
            sample.excess = sample.upload - base  # type: ignore[operator]


def kind_text(fight: dict[str, Any]) -> str:
    kind, detail = fight.get("kind"), fight.get("detail")
    if kind == "boss":
        return {4: "boss mityczny"}.get(detail, f"boss (rzadkość {detail})")
    if kind == "tower":
        return f"wieża, piętro {detail}"
    return {"group": "boss grupowy", "duel": "pojedynek", "hunt": f"polowanie ({detail})"}.get(kind, str(kind))


def stats(values: list[float]) -> str:
    if not values:
        return "brak danych"
    text = f"n={len(values)}, mediana {statistics.median(values):.2f} s"
    if len(values) >= 5:
        text += f", 90% ≤ {percentile(values, 90):.2f} s, 99% ≤ {percentile(values, 99):.2f} s"
    return text + f", maks. {max(values):.2f} s"


def verdict(over: float, excess: float | None) -> str:
    if excess is None:
        return "brak czasu wysłania"
    if excess >= over - TOLERANCE:
        return "LAG WYSYŁANIA to tłumaczy"
    if excess >= LAG:
        return "lag był, ale za mały"
    return "nie lag wysyłania"


def report(fights: list[dict[str, Any]], player: str | None) -> str:
    samples = samples_of(fights)
    set_excess(samples)
    if player:
        wanted = player.casefold().lstrip("@")
        samples = [s for s in samples if s.player.casefold() == wanted or str(s.player_id) == wanted]
        fights = [f for f in fights if any(n.casefold() == wanted or str(i) == wanted
                                           for i, n in names(f).items())]
    out: list[str] = []
    days = sorted({str(f.get("started_at") or "")[:10] for f in fights})
    out.append(f"Walki: {len(fights)} ({days[0]} - {days[-1]})" if days else "Walki: 0")
    if not fights:
        return "\n".join(out)
    ends = defaultdict(int)
    for fight in fights:
        ends[fight.get("end") or "bez końca"] += 1
    out.append("Wyniki: " + ", ".join(f"{key} {value}" for key, value in sorted(ends.items())))

    with_nonce = [s for s in samples if s.nonce is not None]
    with_time = [s for s in samples if s.upload is not None]
    out.append(f"Odpowiedzi: {len(samples)}, z nonce {len(with_nonce)}, z czasem wysłania {len(with_time)}"
               + (f" ({100 * len(with_time) / len(samples):.0f}%)" if samples else ""))
    odd = sorted({s.nonce for s in with_nonce if s.upload is None})[:5]
    if odd:
        out.append("  nonce, które nie są czasem (przykłady): " + ", ".join(odd))

    out.append("")
    out.append("Droga Discord -> bot (wszyscy): " + stats([s.delivery for s in samples if s.delivery is not None]))

    out.append("")
    out.append("Opóźnienie wysyłania ponad normę gracza (czas z nonce, norma = jego dzień):")
    by_player: dict[int, list[Sample]] = defaultdict(list)
    for sample in samples:
        by_player[sample.player_id].append(sample)
    for player_id, group in sorted(by_player.items(), key=lambda item: -len(item[1])):
        excess = [s.excess for s in group if s.excess is not None]
        lags = sum(1 for value in excess if value >= LAG)
        out.append(f"  {group[0].player}: {stats(excess)}; ≥{LAG:.1f} s: {lags} z {len(excess)}"
                   + (f"; bez czasu wysłania: {len(group) - len(excess)}" if len(group) > len(excess) else ""))

    out.append("")
    out.append("Rundy przegrane czasem (z odpowiedzią po czasie):")
    lost = [s for s in samples if s.late and (s.entry.get("late") or {}).get("over") is not None]
    if not lost:
        out.append("  brak")
    for sample in sorted(lost, key=lambda s: s.fight.get("started_at") or ""):
        entry, late = sample.entry, sample.entry["late"]
        over = late["over"]
        excess = "?" if sample.excess is None else f"{sample.excess:+.2f} s"
        saved = " (uratowana)" if entry.get("saved") else ""
        decisive = " [przegrała walkę]" if sample.fight.get("decisive_round") == entry.get("number") else ""
        out.append(f"  {str(sample.fight.get('started_at'))[:16].replace('T', ' ')} UTC {sample.player}, "
                   f"{kind_text(sample.fight)}, runda {entry.get('number')} '{entry.get('word')}': "
                   f"limit {entry.get('limit'):.2f} s, odpowiedź po {late['after']:.2f} s "
                   f"(+{over:.2f} s), wysyłanie ponad normę {excess} -> {verdict(over, sample.excess)}"
                   f"{saved}{decisive}")
    silent = [(f, e) for f in fights for e in f.get("rounds", [])
              if e.get("outcome") == "timeout" and not e.get("late")]
    if silent:
        out.append(f"  + rundy bez żadnej odpowiedzi w {20} s: {len(silent)}")

    out.append("")
    out.append(f"Odpowiedzi w czasie mimo lagu wysyłania ≥{LAG:.1f} s:")
    spared = [s for s in samples if not s.late and s.excess is not None and s.excess >= LAG]
    out.append(f"  {len(spared)}" + (": " + ", ".join(
        f"{s.player} {s.excess:+.2f} s ({kind_text(s.fight)}, r. {s.entry.get('number')})"
        for s in sorted(spared, key=lambda s: -(s.excess or 0))[:10]) if spared else ""))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, default=ROOT / "logs" / "fights", help="folder archiwum walk")
    parser.add_argument("--days", type=int, default=None, help="tylko ostatnie N dni")
    parser.add_argument("--player", default=None, help="nick albo id gracza")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    print(report(read_fights(args.dir, args.days), args.player))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
