"""features/missions.py, the rules and the database ("Zlecenia", spec-v2 §3, §5, §7, §10).

The pure rules (the config, the period, the offers and the pity, the chance, the outcome and its rewards)
are driven by :class:`Dice`, a stand-in for the module's ``random``; the flows (roll, reroll, send, claim,
recall, the announcement, the summoning scroll) run against a real PostgreSQL.  The away rules of the
pets module are in test_pet_away.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import statistics
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from altermmo.core import challenge, clock, guards, io, ui
from altermmo.core.state import BossPhase, EventType
from altermmo.features import album, hub, missions, patchnotes, pets

from test_pets import add_owner, add_pet, confirms, owner_row, patron, pet_row, rig
from test_pets import plain_cards  # noqa: F401 - autouse: plain pictures

SHIPPED = missions.CONFIG_FILE
SHIPPED_RAW = json.loads(SHIPPED.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_open_questions():
    pets._awaiting_confirmation.clear()
    yield
    assert not pets._awaiting_confirmation, "a !potwierdzam slot was never released"


@pytest.fixture(autouse=True)
def own_config(monkeypatch, tmp_path):
    """The missions read the tests' own copy of the config (the shipped one; a test may change it)."""
    path = tmp_path / "own-missionsConfig.json"
    path.write_text(json.dumps(SHIPPED_RAW, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(missions, "CONFIG_FILE", path)
    return path


def edit_config(path, change) -> None:
    """Rewrite the tests' config with ``change(raw)`` applied."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    change(raw)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def ordinary_eggs(monkeypatch):
    """Eggs of the claims are never mythic by chance (the class roll is 0; a mission egg's ``min_class``
    redraw still lifts it), and a supporter's cosmetic roll gives nothing (no owner note)."""
    randint, randrange = random.randint, random.randrange

    def plain_randint(low, high):
        return 0 if (low, high) == (0, 1000) else randint(low, high)

    def plain_randrange(stop, *args, **kwargs):
        if not args and not kwargs and stop in (pets.COSMETIC_ROLL, pets.FRAME_ROLL):
            return stop - 1
        return randrange(stop, *args, **kwargs)

    monkeypatch.setattr(random, "randint", plain_randint)
    monkeypatch.setattr(random, "randrange", plain_randrange)


class Dice:
    """Stands in for ``random`` inside missions: queued answers, otherwise the defaults below."""

    def __init__(self) -> None:
        self.tiers: list[int] = []      # choices(): the tier of slot 2 / slot 3 (default: zwykłe)
        self.keys: list[str] = []       # choice(): the mission picked, by key (default: the first one left)
        self.hours: list[int] = []      # randrange(3): the index of the hours (default 0)
        self.rolls: list[int] = []      # randrange(100): the outcome's roll (default 50)
        self.lines: list[int] = []      # randrange(len(failure_lines)) (default 0)
        self.chances: list[float] = []  # random() (default :attr:`chance`)
        self.chance = 0.999
        self.weights: list[list[int]] = []
        self.calls: list[str] = []

    def choices(self, population, weights=None, k=1):
        self.calls.append("choices")
        self.weights.append(list(weights))
        return [self.tiers.pop(0) if self.tiers else list(population)[0]]

    def choice(self, pool):
        self.calls.append("choice")
        pool = list(pool)
        wanted = self.keys.pop(0) if self.keys else None
        return next((mission for mission in pool if mission.key == wanted), pool[0])

    def randrange(self, stop):
        self.calls.append(f"randrange({stop})")
        if stop == 100:
            return self.rolls.pop(0) if self.rolls else 50
        queue = self.hours if stop == 3 else self.lines
        return queue.pop(0) if queue else 0

    def random(self):
        self.calls.append("random")
        return self.chances.pop(0) if self.chances else self.chance


@pytest.fixture
def dice(monkeypatch):
    stand_in = Dice()
    monkeypatch.setattr(missions, "random", stand_in)
    return stand_in


def at(text: str) -> datetime:
    """A Warsaw wall-clock moment, e.g. ``"2026-11-25 14:00"``."""
    return datetime.fromisoformat(text).replace(tzinfo=clock.WARSAW)


@pytest.fixture
def frozen(monkeypatch):
    """``frozen(at(...))`` moves the game's clock (the period); the database keeps its own ``now()``."""
    def move(moment: datetime) -> None:
        monkeypatch.setattr(clock, "now", lambda: moment)

    return move


def config():
    return missions.load_config()


def offer_of(key: str, hours_index: int = 0) -> dict:
    cfg = config()
    return missions._offer(cfg, missions.find_mission(cfg, key), hours_index)


# =========================================== the config ===========================================

def test_the_shipped_config_validates_and_is_the_catalog_of_the_spec():
    cfg = missions.load_config(SHIPPED)
    assert [len([m for m in cfg.missions if m.tier == index]) for index in range(3)] == [10, 9, 9]
    assert cfg.slot2 == {"zwykle": 50, "rzadkie": 45, "epickie": 5}
    assert cfg.slot3 == {"zwykle": 0, "rzadkie": 70, "epickie": 30}
    assert (cfg.pity_after, cfg.reroll_price, cfg.rerolls_per_period, cfg.great_divisor) == (4, 5, 1, 10)
    # (No cap of the Zwoje przywołania since 2026-11-26; the shipped file keeps the key for the code before.)
    assert not hasattr(cfg, "summon_cap") and SHIPPED_RAW["summon_cap"] == 3
    assert [cfg.tiers[key].hours for key in missions.TIERS] == [(3, 4, 6), (6, 8, 12), (12, 18, 24)]
    assert cfg.tiers["epickie"].summon == {18: Fraction(1, 100), 24: Fraction(2, 100)}
    assert cfg.eggs == {"egg": (0, 0), "rare_egg": (900, 3), "great_egg": (980, 6)}
    assert cfg.compensation == {"egg": ("shards", 3), "rare_egg": ("scrolls", 1), "great_egg": ("scrolls", 3),
                                "level": ("shards", 3), "place": ("scrolls", 10), "pouch": ("scrolls", 15),
                                "summon": ("scrolls", 20)}
    # (rev. 2) every stat 2-4 times per tier, the boss detection 2-3 times - counted
    counted = {index: {stat: sum(stat in m.needs for m in cfg.missions if m.tier == index) for stat in missions.STATS}
               for index in range(3)}
    assert counted[0] == {"DROP_PERC": 3, "DETECTION": 2, "CRIT_PERC": 2, "SLOW_PERC": 2, "DEF_PERC": 3,
                          "REPLACE_PERC": 2, "LOWHP_PERC": 2}
    assert counted[1] == {"CRIT_PERC": 3, "DEF_PERC": 3, "LOWHP_PERC": 2, "DETECTION": 3, "SLOW_PERC": 3,
                          "DROP_PERC": 2, "REPLACE_PERC": 2}
    assert counted[2] == {"CRIT_PERC": 3, "LOWHP_PERC": 4, "DEF_PERC": 4, "DROP_PERC": 2, "REPLACE_PERC": 4,
                          "SLOW_PERC": 3, "DETECTION": 3}
    assert set(missions.STAR_WORDS) == set(missions.REWARD_KINDS)
    for mission in cfg.missions:
        for text in (mission.name, mission.flavour, mission.success):
            assert "wyprawa" not in text.lower() and "$" not in text
        assert len(f"10. {mission.name}") <= 80
    assert missions.find_mission(cfg, "bagno").needs == ("DROP_PERC", "DETECTION")            # (rev. 2)


def _broken(change):
    raw = json.loads(json.dumps(SHIPPED_RAW))
    change(raw)
    return raw


@pytest.mark.parametrize("change, said", [
    (lambda raw: raw.update(nowy=1), "unknown nowy"),
    (lambda raw: raw["missions"][0].update(star="rare_egg"), "has no chance"),                # zwykłe: rare_egg 0
    (lambda raw: raw["missions"][3].update(flavour="Wielka Wyprawa po miód."), "'wyprawa'"),
    (lambda raw: raw["missions"][3].update(name="Wpisz $zlecenia"), "'$'"),
    (lambda raw: raw["failure_lines"].append("Wyprawa się nie udała."), "'wyprawa'"),
    (lambda raw: raw["tiers"]["rzadkie"].update(mult=[1.0, 1.2]), "same length"),
    (lambda raw: raw["offers"]["slot3"].update(rzadkie=0, epickie=0), "add up to 0"),
    (lambda raw: raw["offers"]["slot2"].update(zwykle=-1), "offers.slot2.zwykle"),
    (lambda raw: raw["tiers"]["epickie"]["success"].update(summon={"20": 0.01}), "not one of the tier's hours"),
    (lambda raw: raw["tiers"]["zwykle"]["success"].update(egg=1.5), "a chance from 0 to 1"),
    (lambda raw: raw["tiers"]["zwykle"].update(min=96), "min is above max"),
    (lambda raw: raw["tiers"]["zwykle"].update(hours=[3, 4, 49]), "from 1 to 48"),
    (lambda raw: raw["tiers"]["zwykle"]["targets"].pop("SLOW_PERC"), "missing SLOW_PERC"),
    (lambda raw: raw["compensation"].update(egg={"shards": 3, "scrolls": 1}), "compensation.egg"),
    (lambda raw: raw["eggs"]["great_egg"].update(min_class=998), "eggs.great_egg.min_class"),
    (lambda raw: raw["eggs"]["rare_egg"].update(rerolls=11), "eggs.rare_egg.rerolls"),
    (lambda raw: raw.update(great_divisor=0), "great_divisor"),
    (lambda raw: raw.update(reroll_price=True), "whole number"),
    (lambda raw: raw.update(failure_lines=[]), "failure_lines"),
    (lambda raw: raw["missions"][0].update(needs=["DROP_PERC", "DROP_PERC"]), "needs"),
    (lambda raw: raw["missions"][0].update(needs=["INIT_PERC"]), "needs"),
    (lambda raw: raw["missions"][1].update(key="owce"), "used twice"),
    (lambda raw: raw["missions"][1].update(key="Grzyby!"), "key"),
    (lambda raw: raw["missions"][2].update(name="x" * 61), "longer than 60"),
    (lambda raw: raw.update(missions=[m for m in raw["missions"] if m["key"] not in ("smok", "brama", "kraken",
                                                                                     "twierdza", "wyrocznia",
                                                                                     "bestia", "przelecz")]),
     "fewer than 3"),
])
def test_a_broken_config_is_refused(tmp_path, change, said):
    path = tmp_path / "missionsConfig.json"
    path.write_text(json.dumps(_broken(change), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(missions.ConfigError) as error:
        missions.load_config(path)
    assert said in str(error.value) and "missionsConfig.json" in str(error.value)


@pytest.mark.parametrize("change", [lambda raw: raw.pop("summon_cap"), lambda raw: raw.update(summon_cap=0),
                                    lambda raw: raw.update(summon_cap="dowolnie")])
def test_the_summon_cap_is_an_optional_key_that_is_not_read(tmp_path, change):
    """Owner (2026-11-26): no caps anywhere - a file with or without ``summon_cap`` loads the same."""
    path = tmp_path / "missionsConfig.json"
    path.write_text(json.dumps(_broken(change), ensure_ascii=False), encoding="utf-8")
    assert missions.load_config(path) == missions.load_config(SHIPPED)


def test_a_config_that_is_no_json_or_missing_is_refused(tmp_path):
    path = tmp_path / "missionsConfig.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(missions.ConfigError):
        missions.load_config(path)
    with pytest.raises(missions.ConfigError):
        missions.load_config(tmp_path / "nothing.json")


def test_mission_names_come_from_the_config_or_are_the_key(own_config):
    assert missions.mission_name("karawana") == "Eskorta karawany"
    assert missions.mission_name("zniknela") == "zniknela"
    own_config.write_text("{", encoding="utf-8")
    assert missions.mission_name("karawana") == "karawana"                    # a broken config: never raises


# =========================================== the period ===========================================

@pytest.mark.parametrize("now, start", [
    ("2026-11-25 02:29:59", "2026-11-24 02:30"),
    ("2026-11-25 02:30:00", "2026-11-25 02:30"),
    ("2026-11-23 14:59:59", "2026-11-23 02:30"),                 # Monday: before the season end ...
    ("2026-11-23 15:00:00", "2026-11-23 15:00"),                 # ... and the second period of the day
    ("2026-11-24 02:29:00", "2026-11-23 15:00"),
    ("2026-10-25 12:00:00", "2026-10-25 02:30"),                 # the autumn DST day
])
def test_the_period_is_the_hunts(frozen, now, start):
    frozen(at(now))
    period = missions.current_period()
    assert period == at(start).astimezone(clock.UTC) and period.tzinfo is clock.UTC
    assert missions.current_period(at(now)) == period


def test_the_period_holds_through_the_hour_that_repeats_in_october():
    """02:00-03:00 Warsaw comes twice on 2026-10-25; the day's one reset is the first 02:30 (00:30 UTC)."""
    night = datetime(2026, 10, 25, 0, 30, tzinfo=clock.UTC)
    assert {missions.current_period(night + timedelta(minutes=minutes)) for minutes in range(0, 24 * 60, 5)} == {night}
    assert missions.current_period(night - timedelta(seconds=1)) == night - timedelta(days=1)


# =========================================== the offers ===========================================

def test_a_board_has_a_plain_first_slot_three_different_missions_and_their_snapshots(monkeypatch):
    monkeypatch.setattr(missions, "random", random.Random(20261125))
    cfg = config()
    for _ in range(300):
        board = missions.roll_offers(cfg)
        assert len(board) == 3 and board[0]["tier"] == 0
        assert len({offer["key"] for offer in board}) == 3
        for offer in board:
            mission = missions.find_mission(cfg, offer["key"])
            tier = cfg.tier(offer["tier"])
            assert mission.tier == offer["tier"] and offer["star"] == mission.star
            index = tier.hours.index(offer["hours"])
            assert offer["mult"] == tier.mult[index]
            assert offer["needs"] == [[need, 1 if need == "DETECTION" else tier.targets[need]]
                                      for need in mission.needs]
            assert [offer[key] for key in ("base", "talent", "span", "min", "max")] == \
                [tier.base, tier.talent, tier.span, tier.min, tier.max]
    assert json.loads(json.dumps(board)) == board                         # a snapshot is plain JSON


def test_the_slots_are_drawn_by_their_weights_and_the_pity_makes_slot_3_epic(dice):
    dice.tiers = [1, 1]
    board = missions.roll_offers(config())
    assert [offer["tier"] for offer in board] == [0, 1, 1]
    assert dice.weights == [[50, 45, 5], [0, 70, 30]]
    dice.weights.clear()
    dice.tiers = [1]
    board = missions.roll_offers(config(), pity=True)
    assert [offer["tier"] for offer in board] == [0, 1, 2] and dice.weights == [[50, 45, 5]]


def test_the_shares_of_the_offers(monkeypatch):
    monkeypatch.setattr(missions, "random", random.Random(7))
    cfg = config()
    tiers = [offer["tier"] for _ in range(20000) for offer in missions.roll_offers(cfg)]
    share = [tiers.count(index) / len(tiers) for index in range(3)]
    # slot 1 zwykłe; slot 2 50/45/5; slot 3 0/70/30 (no pity here): 50 / 38.3 / 11.7 %
    assert share == pytest.approx([0.5, 0.3833, 0.1167], abs=0.01)


def test_the_pity_counts_the_last_boards():
    plain = [{"tier": 0}, {"tier": 1}, {"tier": 1}]
    epic = [{"tier": 0}, {"tier": 1}, {"tier": 2}]
    assert missions.pity_due([plain] * 4, 4)
    assert not missions.pity_due([plain] * 3, 4)                         # only 3 boards yet
    assert not missions.pity_due([plain, plain, plain, epic], 4)
    assert missions.pity_due([plain] * 4 + [epic], 4)                     # older boards do not count
    assert missions.pity_due([json.dumps(plain)] * 4, 4)                  # the stored JSON text
    assert missions.pity_due([], 0)


# =========================================== the chance ===========================================

def pet(**stats):
    row = {"pet_skills": 0, "detection": False, **{stat.lower(): 0 for stat in missions.NUMERIC_STATS}}
    row.update(stats)
    return row


@pytest.mark.parametrize("tier, needs, stats, chance", [
    # the worked example: talent 6, DEF 9 and SLOW 30 on rzadkie: fit (9/15 + 1)/2 = 4/5 -> 14 + 12 + 41.6 + .5
    (1, [["DEF_PERC", 15], ["SLOW_PERC", 22]], dict(pet_skills=6, def_perc=Decimal(9), slow_perc=Decimal(30)), 68),
    # clamped at max (perfect fit, talent 9: 38 + 18 + 42) and at min (nothing: 0 + .5)
    (0, [["DEF_PERC", 8]], dict(pet_skills=9, def_perc=Decimal(50)), 95),
    (2, [["CRIT_PERC", 24], ["LOWHP_PERC", 24]], dict(pet_skills=0), 3),
    # the talent counts up to 9
    (1, [["CRIT_PERC", 15]], dict(pet_skills=12, crit_perc=15), 84),
    (1, [["CRIT_PERC", 15]], dict(pet_skills=9, crit_perc=15), 84),
    # the boss detection counts 0 or 1
    (1, [["DROP_PERC", 15], ["DETECTION", 1]], dict(pet_skills=2, drop_perc=15, detection=True), 70),
    (1, [["DROP_PERC", 15], ["DETECTION", 1]], dict(pet_skills=2, drop_perc=15, detection=False), 44),
    # SLOW has its own target (12 on zwykłe): 6 is a half
    (0, [["SLOW_PERC", 12]], dict(pet_skills=0, slow_perc=6), 59),
    # half up, exactly: 38 + 42 * 1/4 = 48.5 -> 49; a Decimal just below that -> 48
    (0, [["DEF_PERC", 8]], dict(def_perc=Decimal(2)), 49),
    (0, [["DEF_PERC", 8]], dict(def_perc=Decimal("1.99")), 48),
    # a legacy pet: NULL stats, NULL talent, NULL detection count as nothing; a negative value as 0
    (0, [["DEF_PERC", 8], ["DETECTION", 1]], dict(pet_skills=None, def_perc=None, detection=None), 38),
    (1, [["DEF_PERC", 15]], dict(pet_skills=None, def_perc=Decimal(-5)), 14),
])
def test_chance_of(tier, needs, stats, chance):
    tier_config = config().tier(tier)
    offer = {"needs": needs, "base": tier_config.base, "talent": tier_config.talent, "span": tier_config.span,
             "min": tier_config.min, "max": tier_config.max}
    assert missions.chance_of(offer, pet(**stats)) == chance


def test_the_reachable_range_of_the_chance():
    cfg = config()
    for index, (best, worst) in enumerate([(95, 44), (84, 20), (76, 6)]):
        tier = cfg.tier(index)
        offer = {"needs": [["CRIT_PERC", 1]], "base": tier.base, "talent": tier.talent, "span": tier.span,
                 "min": tier.min, "max": tier.max}
        assert missions.chance_of(offer, pet(pet_skills=9, crit_perc=99)) == best
        assert missions.chance_of(offer, pet(pet_skills=3)) == worst


@pytest.mark.parametrize("tier, stats, resourceful, chance", [
    # after the cut: zwykłe at its max 95 + 6 -> 98 (the ceiling: a mission is never certain)
    (0, dict(pet_skills=9, crit_perc=99), 6, 98),
    (0, dict(pet_skills=9, crit_perc=99), 2, 97),
    # rzadkie: worst 20 + 12; epickie at its min 3 + 30 (the soft cap's neighbourhood)
    (1, dict(pet_skills=3), 12, 32),
    (2, dict(pet_skills=0), 30, 33),
    # nothing, NULL, a negative value and the old table (no column) change nothing; a fraction counts whole
    (1, dict(pet_skills=3), 0, 20),
    (1, dict(pet_skills=3), None, 20),
    (1, dict(pet_skills=3), Decimal(-4), 20),
    (1, dict(pet_skills=3), Decimal("4.9"), 24),
])
def test_resourcefulness_is_added_after_the_cut_up_to_98(tier, stats, resourceful, chance):
    """"Zaradność w zleceniach" (owner, 2026-11-26): ``min(98, cut + MISSION_PERC)`` - not part of ``fit``."""
    tier_config = config().tier(tier)
    offer = {"needs": [["CRIT_PERC", 1]], "base": tier_config.base, "talent": tier_config.talent,
             "span": tier_config.span, "min": tier_config.min, "max": tier_config.max}
    assert missions.MAX_CHANCE == 98
    assert missions.chance_of(offer, pet(**stats, mission_perc=resourceful)) == chance
    assert missions.chance_of(offer, pet(**stats)) == missions.chance_of(offer, pet(**stats, mission_perc=0))
    # It never lowers a chance: a tier whose max were above the ceiling keeps its cut.
    above = {**offer, "base": 200, "min": 99, "max": 99}
    assert missions.chance_of(above, pet(**stats, mission_perc=resourceful)) == 99


def test_the_pick_shows_the_resourcefulness_of_the_chosen_pet():
    offer = offer_of("karawana")
    row = {"pet_id": 6, "pet_name": "Mruczek", "pet_lvl": 3, "type": "Dog", "pet_skills": 5, "def_perc": 20,
           "slow_perc": 30, "detection": False, "mission_perc": 6}
    shown = missions.pick_embed(offer, config(), pets.MissionPet("2:6", 2, row), missions.chance_of(offer, row))
    text = shown.embed.description
    assert "🧭 Zaradność w zleceniach: +6 %\n🎯 **Szansa powodzenia: " in text
    plain = missions.pick_embed(offer, config(), pets.MissionPet("2:6", 2, {**row, "mission_perc": 0}), 50)
    assert "Zaradność" not in plain.embed.description


_GAINS = {"mythic": ((8, 12), (11, 16)), "ultra": ((6, 11), (9, 15)), "shiny": ((5, 8), (8, 12)),
          "ordinary": ((3, 5), (5, 8))}


def grown_pet(dice_: random.Random, kind: str, talent: int) -> dict:
    """A pet grown by the game's talent rules (pets._roll_skill / _GAINS), as the spec's simulation grows one."""
    stats = pet(pet_skills=talent, pet_lvl=3)
    for _ in range(talent):
        pool = [stat for stat in missions.STATS
                if not (stat == "DETECTION" and stats["detection"]) and not (stat == "SLOW_PERC" and
                                                                             stats["slow_perc"] >= 80)]
        stat = dice_.choice(pool)
        if stat == "DETECTION":
            stats["detection"] = True
            continue
        low, high = _GAINS[kind][stat == "SLOW_PERC"]
        stats[stat.lower()] += dice_.randint(low, high)
    return stats


@pytest.mark.parametrize("kind, talent, means", [
    ("ordinary", 3, (54, 28, 12)), ("ordinary", 6, (68, 42, 24)), ("shiny", 6, (73, 48, 29)),
    ("ultra", 9, (86, 65, 47)), ("mythic", 9, (87, 67, 50)),
])
def test_balance_guard_the_archetype_means_of_appendix_a1(kind, talent, means):
    """An accidental change of a constant moves these: chance_of over grown pets matches spec A1 within 3."""
    dice_ = random.Random(20261125)
    cfg = config()
    grown = [grown_pet(dice_, kind, talent) for _ in range(1500)]
    for index, mean in enumerate(means):
        offers = [missions._offer(cfg, mission, 0) for mission in cfg.missions if mission.tier == index]
        assert statistics.mean(missions.chance_of(offer, one) for one in grown for offer in offers) == \
            pytest.approx(mean, abs=3)


# =========================================== the outcome ==========================================

def test_a_failure_brings_its_line_and_the_tiers_shards(dice):
    dice.rolls, dice.lines = [60], [3]
    outcome = missions.roll_outcome(config(), offer_of("smok"), 60, level=1)
    assert outcome == {"result": "fail", "roll": 60, "line": "Przeciwnik okazał się silniejszy.",
                       "rewards": [{"kind": "shards", "n": 2}]}
    assert dice.calls == ["randrange(100)", "randrange(6)"]                # nothing else is drawn


def test_a_success_draws_every_reward_independently(dice):
    dice.rolls, dice.chance = [20], 0.0                                     # every chance comes
    offer = offer_of("brama", 2)                                            # epickie 24 h, m 1.6, star: summon
    outcome = missions.roll_outcome(config(), offer, 76, level=2)
    assert outcome["result"] == "success" and outcome["roll"] == 20
    assert outcome["line"] == "Wrócił z czymś, co wciąż szepcze w jego sierści."
    # scrolls 1.2 * 1.6 = 1.92 -> 1 + 1; shards 5 * 1.6 = 8 exactly (no draw)
    assert outcome["rewards"] == [{"kind": "scrolls", "n": 2}, {"kind": "shards", "n": 8}, {"kind": "egg"},
                                  {"kind": "rare_egg"}, {"kind": "great_egg"}, {"kind": "level"},
                                  {"kind": "stone"}, {"kind": "mirror"}, {"kind": "place"}, {"kind": "pouch"},
                                  {"kind": "summon"}]
    assert dice.calls.count("random") == 10                                  # 8 m exactly: no draw for shards


def test_nothing_but_the_whole_counts_when_the_dice_say_no(dice):
    dice.rolls = [20]
    outcome = missions.roll_outcome(config(), offer_of("brama", 2), 76, level=2)
    assert outcome["rewards"] == [{"kind": "scrolls", "n": 1}, {"kind": "shards", "n": 8}]


def test_a_great_success_doubles_scrolls_and_shards(dice):
    dice.rolls, dice.chances = [6], [0.9, 0.5]                             # 6 < 76 // 10
    outcome = missions.roll_outcome(config(), offer_of("brama", 2), 76, level=3)
    assert outcome["result"] == "great"
    # scrolls 3.84 -> 3 (0.9 is not below .84); shards 16; no level for a pet at level 3
    assert outcome["rewards"][:2] == [{"kind": "scrolls", "n": 3}, {"kind": "shards", "n": 16}]
    assert all(reward["kind"] != "level" for reward in outcome["rewards"])
    dice.rolls = [7]
    assert missions.roll_outcome(config(), offer_of("brama", 2), 76)["result"] == "success"   # 7 = 76 // 10


def test_the_star_doubles_and_the_summon_chance_depends_on_the_hours_only(dice):
    cfg = config()
    # grzyby (zwykłe, star shards) 3 h: shards 2 * 2 = 4, scrolls .3 -> a draw
    dice.rolls = [0]
    assert missions.draw_rewards(cfg, offer_of("grzyby"), level=0)[:1] == [{"kind": "shards", "n": 4}]
    # brama 24 h: summon 2% * 2 (star) = 4% - not times 1.6
    summon = len(missions.REWARD_KINDS) - 3  # the random() draws before the summon's: scrolls + 7 chances
    dice.chances = [0.999] * summon + [0.039]
    assert missions.draw_rewards(cfg, offer_of("brama", 2))[-1] == {"kind": "summon"}
    dice.chances = [0.999] * summon + [0.041]
    assert {"kind": "summon"} not in missions.draw_rewards(cfg, offer_of("brama", 2))
    # bestia 12 h: no summon at all (the 12 h has none), nothing is drawn for it
    dice.calls.clear()
    missions.draw_rewards(cfg, offer_of("bestia", 0))
    assert dice.calls.count("random") == summon                              # (8 kinds of it, 1 count)


def test_a_chance_above_one_is_certain(dice, own_config):
    edit_config(own_config, lambda raw: raw["tiers"]["zwykle"]["success"].update(egg=0.8))
    dice.chance = 0.999
    rewards = missions.draw_rewards(config(), offer_of("owce", 2))           # .8 * 1.6 * 2 (star) > 1
    assert {"kind": "egg"} in rewards


# =========================================== the database =========================================

async def player_with(game, name="Ala", *, pet_id=5, level=2, **owner):
    """A member whose active pet is ``pet_id`` (a cat "Burek" at ``level`` with talent 3, CRIT 11, DEF 7)."""
    member = game.add_member(name)
    await add_owner(game.bot.db, member.id, pet_id, **owner)
    if pet_id:
        await add_pet(game.bot.db, pet_id, pet_name="Burek", pet_lvl=level, pet_skills=3, crit_perc=11, def_perc=7,
                      detection=True)
    return member


async def board(db, player_id):
    return await db.fetchrow("SELECT * FROM PET_MISSIONS WHERE PLAYER_ID = $1 ORDER BY PERIOD_START DESC LIMIT 1",
                             player_id)


async def rows(db, player_id):
    return await db.fetch("SELECT * FROM PET_MISSIONS WHERE PLAYER_ID = $1 ORDER BY PERIOD_START", player_id)


async def come_back(db, mission_id):
    await db.execute("UPDATE PET_MISSIONS SET ENDS_AT = now() - interval '1 second' WHERE MISSION_ID = $1",
                     mission_id)


_SQL_AWAY_ROW = """INSERT INTO PET_MISSIONS (PLAYER_ID, PERIOD_START, OFFERS, STATUS, CHOSEN, PET_ID, PET_PLACE,
                       PET_SNAPSHOT, CHANCE, ROLL, OUTCOME, SENT_AT, ENDS_AT)
                   VALUES ($1, $2, $3, 'away', 0, $4, $5, $6, 60, 10, $7, now() - interval '6 hours',
                           now() + make_interval(secs => $8))
                   RETURNING MISSION_ID"""


async def mission_row(db, player_id, pet_id, rewards, *, result="success", place=0, pet_type="Cat", name="Burek",
                      due=True, period=None):
    """A mission as the send wrote it, with these ``rewards`` (ended unless ``due=False``)."""
    outcome = {"result": result, "roll": 10, "line": "Udało się.", "rewards": rewards}
    snap = {"name": name, "type": pet_type, "level": 2, "talent": 3, "shiny": False, "ultra_shiny": False,
            "mythic": False}
    return await db.fetchval(_SQL_AWAY_ROW, player_id, period or missions.current_period(),
                             json.dumps([offer_of("karawana")]), pet_id, place, json.dumps(snap),
                             json.dumps(outcome), -1.0 if due else 3600.0, idempotent=False)


async def result_of(db, mission_id):
    return json.loads(await db.fetchval("SELECT RESULT FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id))


# -- the roll -----------------------------------------------------------------------------------

async def test_the_first_roll_makes_one_board_and_a_second_makes_none(game_db, dice):
    db = game_db.bot.db
    ala = await player_with(game_db)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    row = await missions.roll_board(game_db.bot, ala.id)
    assert row["status"] == "offered" and row["rerolls"] == 0 and row["period_start"] == missions.current_period()
    offers = json.loads(row["offers"])
    assert [(offer["key"], offer["tier"]) for offer in offers] == [("owce", 0), ("karawana", 1), ("smok", 2)]
    assert offers[1] == offer_of("karawana")
    assert await missions.roll_board(game_db.bot, ala.id) is None                       # the second panel
    assert len(await rows(db, ala.id)) == 1


async def test_two_rolls_at_once_make_one_board(game_db):
    ala = await player_with(game_db)
    results = await asyncio.gather(missions.roll_board(game_db.bot, ala.id), missions.roll_board(game_db.bot, ala))
    assert sum(result is not None for result in results) == 1 and len(await rows(game_db.bot.db, ala.id)) == 1


async def test_no_board_while_a_pet_is_away(game_db, frozen):
    db = game_db.bot.db
    ala = await player_with(game_db)
    frozen(at("2026-11-25 20:00"))
    await mission_row(db, ala.id, 5, [], due=False, period=missions.current_period() - timedelta(days=1))
    assert await missions.roll_board(game_db.bot, ala.id) is None
    assert len(await rows(db, ala.id)) == 1


async def test_after_the_reset_the_old_board_expires_and_a_new_one_comes(game_db, frozen):
    db = game_db.bot.db
    ala = await player_with(game_db)
    frozen(at("2026-11-25 20:00"))
    first = await missions.roll_board(game_db.bot, ala.id)
    frozen(at("2026-11-26 02:29"))
    assert await missions.roll_board(game_db.bot, ala.id) is None                       # still the same period
    frozen(at("2026-11-26 02:30"))
    second = await missions.roll_board(game_db.bot, ala.id)
    assert second is not None and second["period_start"] == at("2026-11-26 02:30")
    assert [row["status"] for row in await rows(db, ala.id)] == ["expired", "offered"]
    assert first["mission_id"] != second["mission_id"]


async def test_four_boards_without_an_epic_make_slot_3_epic(game_db, dice, frozen):
    db = game_db.bot.db
    ala = await player_with(game_db)
    plain = json.dumps([offer_of("owce"), offer_of("karawana"), offer_of("wataha")])
    frozen(at("2026-11-25 20:00"))
    for days in (1, 2, 3):
        await db.execute("INSERT INTO PET_MISSIONS (PLAYER_ID, PERIOD_START, OFFERS, STATUS) VALUES ($1, $2, $3, "
                         "'expired')", ala.id, missions.current_period() - timedelta(days=days), plain)
    dice.tiers = [1, 1]
    row = await missions.roll_board(game_db.bot, ala.id)
    assert [offer["tier"] for offer in json.loads(row["offers"])] == [0, 1, 1]          # 3 boards only
    frozen(at("2026-11-26 20:00"))
    dice.tiers = [1, 1]
    row = await missions.roll_board(game_db.bot, ala.id)                                 # 4 without an epic
    assert [offer["tier"] for offer in json.loads(row["offers"])] == [0, 1, 2]
    frozen(at("2026-11-27 20:00"))
    dice.tiers = [1, 1]
    row = await missions.roll_board(game_db.bot, ala.id)                                 # the last one had one
    assert [offer["tier"] for offer in json.loads(row["offers"])] == [0, 1, 1]


async def test_a_board_keeps_its_offers_when_the_config_changes(game_db, dice, own_config):
    ala = await player_with(game_db)
    dice.keys = ["miod"]
    row = await missions.roll_board(game_db.bot, ala.id)
    before = json.loads(row["offers"])
    edit_config(own_config, lambda raw: raw["tiers"]["zwykle"].update(base=10, targets={**raw["tiers"]["zwykle"][
        "targets"], "DEF_PERC": 30}))
    assert json.loads((await board(game_db.bot.db, ala.id))["offers"]) == before
    chance = missions.chance_of(before[0], await pet_row(game_db.bot.db, 5))             # 38 + 6 + 42 * 7/8
    assert chance == 81
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)
    assert sent.status == "ok" and sent.row["chance"] == 81


# -- the send -----------------------------------------------------------------------------------

async def test_the_send_stores_the_outcome_and_the_pet_stays_where_it_is(game_db, dice):
    db = game_db.bot.db
    ala = await player_with(game_db)
    dice.tiers, dice.keys, dice.hours = [1, 1], ["owce", "karawana", "wataha"], [0, 1, 2]
    row = await missions.roll_board(game_db.bot, ala.id)
    owner_before = await owner_row(db, ala.id)
    offer = json.loads(row["offers"])[1]                                                 # karawana 8 h
    chance = missions.chance_of(offer, await pet_row(db, 5))
    dice.rolls, dice.chance = [chance - 1], 0.999
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 1, "active:5", chance)
    assert sent.status == "ok" and sent.chance == chance
    stored = await board(db, ala.id)
    assert stored["status"] == "away" and stored["chosen"] == 1 and stored["pet_id"] == 5
    assert stored["pet_place"] == 0 and stored["chance"] == chance and stored["roll"] == chance - 1
    assert stored["ends_at"] - stored["sent_at"] == timedelta(hours=8)
    assert json.loads(stored["pet_snapshot"]) == {"name": "Burek", "type": "Cat", "level": 2, "talent": 3,
                                                  "shiny": False, "ultra_shiny": False, "mythic": False}
    outcome = json.loads(stored["outcome"])
    assert outcome["result"] == "success" and outcome["roll"] == chance - 1
    assert outcome["line"] == "Karawana dotarła cała, kupcy sypnęli groszem."
    assert await owner_row(db, ala.id) == owner_before                                  # the pet stays
    assert await pets.away_until(game_db.bot, db, 5) == stored["ends_at"]


async def test_a_stabled_pet_goes_from_its_place(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, pet_id_alt2=6)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=3, pet_skills=5, def_perc=20, type="Dog")
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 6))
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 0, "2:6", chance)
    assert sent.status == "ok" and sent.row["pet_place"] == 2 and sent.row["pet_id"] == 6
    assert json.loads(sent.row["pet_snapshot"])["type"] == "Dog"
    assert (await owner_row(db, ala.id))["pet_id_alt2"] == 6


async def test_a_double_send_sends_once(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    results = await asyncio.gather(*(missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)
                                     for _ in range(2)))
    assert sorted(result.status for result in results) == ["gone", "ok"]
    assert await db.fetchval("SELECT COUNT(*) FROM PET_MISSIONS WHERE STATUS = 'away'") == 1
    again = await missions.send(game_db.bot, ala, row["mission_id"], 1, "active:5", chance)
    assert again.status == "gone"                                     # (not "changed", although the pet is away)


async def test_a_board_of_the_previous_period_expired(game_db, frozen):
    ala = await player_with(game_db)
    frozen(at("2026-11-25 20:00"))
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(game_db.bot.db, 5))
    frozen(at("2026-11-26 03:00"))
    assert (await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)).status == "expired"
    assert (await board(game_db.bot.db, ala.id))["status"] == "offered"


async def test_a_pet_that_moved_or_is_an_egg_cannot_go(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, pet_id_alt1=7)
    await add_pet(db, 7, pet_lvl=0)                                                    # an egg in place 1
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    for key in ("1:7", "2:5", "active:6", "0:5", "pouch:5", "nonsense"):
        assert (await missions.send(game_db.bot, ala, row["mission_id"], 0, key, chance)).status == "changed", key
    await db.execute("UPDATE PETOWNER SET PET_ID = 0, PET_ID_ALT2 = 5 WHERE PLAYER_ID = $1", ala.id)
    assert (await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)).status == "changed"
    assert (await missions.send(game_db.bot, ala, row["mission_id"], 0, "2:5", chance)).status == "ok"


async def test_a_changed_chance_sends_nothing(game_db, dice):
    db = game_db.bot.db
    ala = await player_with(game_db)
    dice.keys = ["miod"]                                                                 # DEF 8: 81 %
    row = await missions.roll_board(game_db.bot, ala.id)
    shown = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    await db.execute("UPDATE PETS SET DROP_PERC = 8, CRIT_PERC = 8, DEF_PERC = 8 WHERE PET_ID = 5")   # rerolled
    now = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    assert now != shown
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", shown)
    assert sent == missions.Sent("chance_changed", now)
    assert (await board(db, ala.id))["status"] == "offered"
    assert (await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", now)).status == "ok"


async def test_no_send_while_a_question_is_open_or_without_the_table(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    pets._awaiting_confirmation.add(ala.id)
    try:
        assert (await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)).status == "waiting"
    finally:
        pets._awaiting_confirmation.discard(ala.id)
    assert (await board(db, ala.id))["status"] == "offered"
    assert (await missions.send(game_db.bot, ala, row["mission_id"], 7, "active:5", chance)).status == "gone"
    await db.execute("DROP TABLE PET_MISSIONS", idempotent=True)
    game_db.bot._pet_missions = None
    assert (await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)).status == "off"
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "off"
    assert await missions.ensure_table(game_db.bot) is True and await missions.ready(game_db.bot)
    assert await missions.roll_board(game_db.bot, ala.id) is not None


@pytest.mark.parametrize("level, offered", [(1, True), (2, True), (3, False)])
async def test_the_level_is_offered_only_to_a_young_pet(game_db, dice, level, offered):
    db = game_db.bot.db
    ala = await player_with(game_db, level=level)
    dice.keys = ["zniwa"]
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    dice.rolls, dice.chance = [chance - 1], 0.0
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)
    kinds = [reward["kind"] for reward in json.loads(sent.row["outcome"])["rewards"]]
    assert ("level" in kinds) is offered


# -- the reroll ---------------------------------------------------------------------------------

async def test_the_reroll_costs_five_scrolls_once_a_period(game_db, dice):
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=11)
    dice.keys = ["owce"]
    row = await missions.roll_board(game_db.bot, ala.id)
    dice.keys = ["miod"]
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "ok"
    new = await board(db, ala.id)
    assert new["mission_id"] == row["mission_id"] and new["rerolls"] == 1
    assert json.loads(new["offers"])[0]["key"] == "miod" and json.loads(row["offers"])[0]["key"] == "owce"
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 6
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "used"
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 6


async def test_a_reroll_without_the_scrolls_changes_nothing(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=4)
    row = await missions.roll_board(game_db.bot, ala.id)
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "poor"
    same = await board(db, ala.id)
    assert same["offers"] == row["offers"] and same["rerolls"] == 0
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 4


async def test_no_reroll_after_the_send_or_for_free(game_db, own_config):
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=20)
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "gone"
    assert await missions.reroll(game_db.bot, ala, 999999) == "gone"
    edit_config(own_config, lambda raw: raw.update(reroll_price=0))
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "off"
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 20


async def test_the_reroll_keeps_the_pity(game_db, dice, frozen):
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=5)
    plain = json.dumps([offer_of("owce"), offer_of("karawana"), offer_of("wataha")])
    frozen(at("2026-11-25 20:00"))
    for days in (1, 2, 3, 4):
        await db.execute("INSERT INTO PET_MISSIONS (PLAYER_ID, PERIOD_START, OFFERS, STATUS) VALUES ($1, $2, $3, "
                         "'expired')", ala.id, missions.current_period() - timedelta(days=days), plain)
    dice.tiers = [1]
    row = await missions.roll_board(game_db.bot, ala.id)
    dice.tiers = [1]
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "ok"
    assert [offer["tier"] for offer in json.loads((await board(db, ala.id))["offers"])] == [0, 1, 2]


# -- the recall ---------------------------------------------------------------------------------

async def test_the_recall_brings_the_pet_back_with_nothing(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=1)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 9}], due=False)
    assert await missions.recall(game_db.bot, ala.id, mission_id) is True
    row = await board(db, ala.id)
    assert row["status"] == "recalled" and json.loads(row["result"]) == {"recalled": True}
    assert row["paid_at"] is not None and row["announced_at"] is not None and row["seen_at"] is not None
    assert await pets.away_until(game_db.bot, db, 5) is None
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 1                         # no reward
    assert await missions.roll_board(game_db.bot, ala.id) is None                       # the period is used up
    assert await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True) is None
    assert await missions.recall(game_db.bot, ala.id, mission_id) is False


async def test_a_recall_after_the_end_is_refused_and_the_mission_is_paid(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 2}])
    assert await missions.recall(game_db.bot, ala.id, mission_id) is False
    assert await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True) is not None
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 2


# -- the claim ----------------------------------------------------------------------------------

async def test_a_claim_pays_once_and_keeps_the_result(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=1)
    rewards = [{"kind": "scrolls", "n": 2}, {"kind": "shards", "n": 3}, {"kind": "stone"}, {"kind": "mirror"}]
    mission_id = await mission_row(db, ala.id, 5, rewards)
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    assert claimed.granted.lines == [{"kind": "scrolls", "n": 2}, {"kind": "shards", "n": 3}, {"kind": "stone"},
                                     {"kind": "mirror"}]
    owner = await owner_row(db, ala.id)
    assert (owner["reroll_scroll"], owner["reroll_scroll_shard"], owner["rebirth_stones"], owner["mirrors"]) == \
        (3, 3, 1, 1)
    row = await board(db, ala.id)
    assert row["status"] == "paid" and row["paid_at"] is not None
    assert row["announced_at"] is not None and row["seen_at"] is not None                 # shown in his panel
    assert await result_of(db, mission_id) == {"lines": claimed.granted.lines, "notes": []}
    assert await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True) is None
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 3


async def test_two_claims_at_once_pay_once(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 4}, {"kind": "egg"}])
    eggs_before = await db.fetchval("SELECT COUNT(*) FROM PETS")
    results = await asyncio.gather(
        missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True),
        missions.claim(game_db.bot.boss_scene(), ala.id, mission_id, announce=True, shown=False))
    assert sum(result is not None for result in results) == 1
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 4
    assert await db.fetchval("SELECT COUNT(*) FROM PETS") == eggs_before + 1


async def test_a_mission_that_has_not_ended_is_not_paid(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 4}], due=False)
    assert await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True) is None
    other = game_db.add_member("Ola")
    await come_back(db, mission_id)
    assert await missions.claim(game_db.scene(other), other.id, mission_id, announce=False, shown=True) is None
    claimed = await missions.claim(game_db.bot.boss_scene(), ala.id, mission_id, announce=True, shown=False)
    row = await board(db, ala.id)
    assert claimed is not None and row["announced_at"] is None and row["seen_at"] is None   # the notifier's claim


async def test_a_claim_that_fails_half_way_pays_nothing_and_stays_due(game_db, monkeypatch):
    db = game_db.bot.db
    ala = await player_with(game_db, stable_places=4)
    rewards = [{"kind": "scrolls", "n": 2}, {"kind": "egg"}, {"kind": "place"}]
    mission_id = await mission_row(db, ala.id, 5, rewards)
    before_owner, before_pets = await owner_row(db, ala.id), await db.fetchval("SELECT COUNT(*) FROM PETS")
    real = pets.add_items_in

    async def broken(*args, **kwargs):
        raise RuntimeError("test: the database fell over")

    monkeypatch.setattr(pets, "add_items_in", broken)
    with pytest.raises(RuntimeError):
        await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    assert await owner_row(db, ala.id) == before_owner
    assert await db.fetchval("SELECT COUNT(*) FROM PETS") == before_pets
    row = await board(db, ala.id)
    assert row["status"] == "away" and row["result"] is None and row["paid_at"] is None
    monkeypatch.setattr(pets, "add_items_in", real)
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    assert [line["kind"] for line in claimed.granted.lines] == ["scrolls", "egg", "place"]
    owner = await owner_row(db, ala.id)
    assert owner["reroll_scroll"] == 2 and owner["stable_places"] == 5
    assert await db.fetchval("SELECT COUNT(*) FROM PETS") == before_pets + 1


async def test_a_new_place_takes_the_egg_won_with_it(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, pet_id_alt1=11, pet_id_alt2=12, pet_id_alt3=13, pet_id_alt4=14)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "egg"}, {"kind": "place"}])
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    egg_line, place_line = claimed.granted.lines
    assert place_line == {"kind": "place", "places": 5}
    assert egg_line["kind"] == "egg" and egg_line["class"] == "egg" and egg_line["status"] == "stable"
    assert egg_line["place"] == 5
    assert (await owner_row(db, ala.id))["pet_id_alt5"] == egg_line["pet_id"] == claimed.granted.eggs[0][0]


async def test_eggs_go_to_the_side_then_both_pouch_slots_then_the_stable(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, pet_id=0, pet_id_alt1=5, egg_pouch=True, big_pouch=True)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=2, pet_skills=3)
    rewards = [{"kind": "egg"}, {"kind": "rare_egg"}, {"kind": "great_egg"}]
    mission_id = await mission_row(db, ala.id, 5, rewards, place=1)
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    lines = claimed.granted.lines
    assert [(line["class"], line["status"]) for line in lines] == [("egg", "active"), ("rare_egg", "pouch"),
                                                                  ("great_egg", "pouch")]
    owner = await owner_row(db, ala.id)
    assert (owner["pet_id"], owner["pouch_pet_id"], owner["pouch_pet_id2"]) == tuple(line["pet_id"] for line in lines)
    rare, great = await pet_row(db, lines[1]["pet_id"]), await pet_row(db, lines[2]["pet_id"])
    assert rare["shiny"] and lines[1]["shiny"]                                   # min_class 900: shiny at least
    assert great["ultra_shiny"] and lines[2]["ultra"]                            # 980: ultra at least
    second = await mission_row(db, ala.id, 5, [{"kind": "egg"}], place=1,
                               period=missions.current_period() - timedelta(days=1))
    claimed = await missions.claim(game_db.scene(ala), ala.id, second, announce=False, shown=True)
    assert claimed.granted.lines[0]["status"] == "stable" and claimed.granted.lines[0]["place"] == 2


async def test_every_reward_that_cannot_be_given_is_compensated(game_db):
    db = game_db.bot.db
    full = {f"pet_id_alt{place}": 100 + place for place in range(1, 11)}
    ala = await player_with(game_db, level=3, stable_places=10, egg_pouch=True, big_pouch=True, pouch_pet_id=98,
                            pouch_pet_id2=99, summon_scrolls=3, reroll_scroll=0, **full)
    rewards = [{"kind": "scrolls", "n": 2}, {"kind": "shards", "n": 1}, {"kind": "egg"}, {"kind": "rare_egg"},
               {"kind": "great_egg"}, {"kind": "level"}, {"kind": "stone"}, {"kind": "mirror"}, {"kind": "place"},
               {"kind": "pouch"}, {"kind": "summon"}]
    mission_id = await mission_row(db, ala.id, 5, rewards)
    eggs_before = await db.fetchval("SELECT COUNT(*) FROM PETS")
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    assert claimed.granted.lines == [
        {"kind": "scrolls", "n": 2}, {"kind": "shards", "n": 1},
        {"kind": "egg", "class": "egg", "comp": {"shards": 3}},
        {"kind": "egg", "class": "rare_egg", "comp": {"scrolls": 1}},
        {"kind": "egg", "class": "great_egg", "comp": {"scrolls": 3}},
        {"kind": "level", "name": "Burek", "comp": {"shards": 3}},
        {"kind": "stone"}, {"kind": "mirror"},
        {"kind": "place", "comp": {"scrolls": 10}},
        {"kind": "pouch", "comp": {"scrolls": 15}},
        {"kind": "summon", "count": 4}]                                       # no cap since 2026-11-26
    owner = await owner_row(db, ala.id)
    assert (owner["reroll_scroll"], owner["reroll_scroll_shard"], owner["rebirth_stones"], owner["mirrors"]) == \
        (2 + 1 + 3 + 10 + 15, 1 + 3 + 3, 1, 1)
    assert (owner["stable_places"], owner["summon_scrolls"]) == (10, 4)
    assert await db.fetchval("SELECT COUNT(*) FROM PETS") == eggs_before
    assert (await pet_row(db, 5))["pet_lvl"] == 3


async def test_the_pouch_steps_up_and_the_summoning_scrolls_are_kept(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    scene = game_db.scene(ala)
    lines = []
    for days in range(4):
        mission_id = await mission_row(db, ala.id, 5, [{"kind": "pouch"}, {"kind": "summon"}],
                                       period=missions.current_period() - timedelta(days=days))
        lines.append((await missions.claim(scene, ala.id, mission_id, announce=False, shown=True)).granted.lines)
    assert lines == [
        [{"kind": "pouch", "to": "pouch"}, {"kind": "summon", "count": 1}],
        [{"kind": "pouch", "to": "big"}, {"kind": "summon", "count": 2}],
        [{"kind": "pouch", "comp": {"scrolls": 15}}, {"kind": "summon", "count": 3}],
        [{"kind": "pouch", "comp": {"scrolls": 15}}, {"kind": "summon", "count": 4}]]   # no cap since 2026-11-26
    owner = await owner_row(db, ala.id)
    assert owner["egg_pouch"] and owner["big_pouch"] and owner["summon_scrolls"] == 4
    assert owner["reroll_scroll"] == 15 + 15


async def test_without_the_new_columns_the_items_are_compensated(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, egg_pouch=True)
    game_db.bot._mission_columns = False                                      # BIG_POUCH / SUMMON_SCROLLS missing
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "pouch"}, {"kind": "summon"}, {"kind": "place"}])
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    assert claimed.granted.lines == [{"kind": "pouch", "off": True, "comp": {"scrolls": 15}},
                                     {"kind": "summon", "off": True, "comp": {"scrolls": 20}},
                                     {"kind": "place", "places": 5}]
    # The texts do not claim a cap he never reached ("Duża sakwa jest już największa", "masz już 3").
    texts = [missions.result_line(line) for line in claimed.granted.lines]
    assert texts[:2] == [missions.MSG_GOT_POUCH_OFF.format(comp="15 zwojów odrodzenia"),
                         missions.MSG_GOT_SUMMON_OFF.format(comp="20 zwojów odrodzenia")]
    game_db.bot._owner_upgrades = False                                       # the stable / pouch columns too
    bob = await player_with(game_db, "Bob", pet_id=8)
    second = await mission_row(db, bob.id, 8, [{"kind": "place"}, {"kind": "pouch"}, {"kind": "egg"}])
    claimed = await missions.claim(game_db.scene(bob), bob.id, second, announce=False, shown=True)
    assert [line.get("comp") for line in claimed.granted.lines] == [{"scrolls": 10}, {"scrolls": 15}, None]
    assert [line.get("off") for line in claimed.granted.lines] == [True, True, None]
    assert missions.result_line(claimed.granted.lines[0]) == missions.MSG_GOT_PLACE_OFF.format(
        comp="10 zwojów odrodzenia")
    assert claimed.granted.lines[2]["status"] == "stable" and claimed.granted.lines[2]["place"] == 1
    assert (await owner_row(db, bob.id))["reroll_scroll"] == 25


async def test_the_level_goes_to_the_mission_pet_wherever_it_is(game_db, monkeypatch):
    db = game_db.bot.db
    supporter = patron(game_db)
    await add_owner(db, supporter.id, 5, pet_id_alt3=6)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=1)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=2, pet_skills=3, type="Dog")
    noted = []

    async def note_level_up(scene, player, pet, *, into=None):
        noted.append((player, pet["pet_id"], pet["pet_lvl"]))
        into.append("Album: Mruczek")
        return True

    monkeypatch.setattr(album, "note_level_up", note_level_up)
    mission_id = await mission_row(db, supporter.id, 6, [{"kind": "level"}], place=3, pet_type="Dog",
                                   name="Mruczek")
    claimed = await missions.claim(game_db.scene(supporter), supporter.id, mission_id, announce=False, shown=True)
    assert claimed.granted.lines == [{"kind": "level", "name": "Mruczek", "new": 3}]
    assert (await pet_row(db, 6))["pet_lvl"] == 3 and (await pet_row(db, 5))["pet_lvl"] == 1
    assert (await pet_row(db, 6))["pet_skills"] in (4, 5)
    assert noted == [(supporter, 6, 3)] and claimed.notes == ["Album: Mruczek"]
    assert await result_of(db, mission_id) == {"lines": claimed.granted.lines, "notes": ["Album: Mruczek"]}


async def test_a_mission_level_can_grow_the_pets_aura_and_the_result_says_so(game_db, monkeypatch):
    """The aura grows with the level (50%, items_decisions.md §8); the line keeps it as data, the result says it."""
    db = game_db.bot.db
    ala = game_db.add_member("Ala")
    await add_owner(db, ala.id, 5, pet_id_alt3=6)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=1)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=2, pet_skills=3, type="Dog", aura="CRIT_PERC", aura_perc=2)
    real = random.randrange
    monkeypatch.setattr(random, "randrange", lambda stop, *args: 0 if stop == 2 and not args else real(stop, *args))
    mission_id = await mission_row(db, ala.id, 6, [{"kind": "level"}], place=3, pet_type="Dog", name="Mruczek")
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    assert claimed.granted.lines == [{"kind": "level", "name": "Mruczek", "new": 3, "aura": "CRIT_PERC",
                                      "aura_perc": 3}]
    row = await pet_row(db, 6)
    assert (row["pet_lvl"], row["aura"], row["aura_perc"]) == (3, "CRIT_PERC", 3)
    stored = await db.fetchrow("SELECT * FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id)
    text = missions.result_embed(ala.id, stored, config()).embed.description
    assert ("⬆️ **Mruczek** awansuje na poziom 3!\n🌟 **Aura Furii** Twojego towarzysza przybrała na sile - teraz to +3 %!"
            in text)


@pytest.mark.parametrize("change", ["gone", "species", "pouch", "max"])
async def test_a_level_for_a_pet_that_cannot_take_it_is_compensated(game_db, change):
    db = game_db.bot.db
    ala = await player_with(game_db, egg_pouch=True)
    if change == "gone":                                       # sacrificed after its return, its id given again
        await db.execute("UPDATE PETOWNER SET PET_ID = 0 WHERE PLAYER_ID = $1", ala.id)
    elif change == "species":
        await db.execute("UPDATE PETS SET TYPE = 'Dog' WHERE PET_ID = 5")
    elif change == "pouch":
        await db.execute("UPDATE PETOWNER SET PET_ID = 0, POUCH_PET_ID = 5 WHERE PLAYER_ID = $1", ala.id)
    else:
        await db.execute("UPDATE PETS SET PET_LVL = 3 WHERE PET_ID = 5")
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "level"}])
    level_before = (await pet_row(db, 5))["pet_lvl"]
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    assert claimed.granted.lines == [{"kind": "level", "name": "Burek", "comp": {"shards": 3}}]
    assert (await pet_row(db, 5))["pet_lvl"] == level_before
    assert (await owner_row(db, ala.id))["reroll_scroll_shard"] == 3


async def test_mission_eggs_are_rolled_by_their_class_and_cosmetics_stay_for_supporters(game_db, monkeypatch):
    db = game_db.bot.db
    asked = []
    real = pets.roll_egg

    def roll_egg(player, **kwargs):
        asked.append((player, kwargs["rerolls"], kwargs["min_class"]))
        return real(player, **kwargs)

    monkeypatch.setattr(pets, "roll_egg", roll_egg)
    supporter = patron(game_db)
    await add_owner(db, supporter.id, 0, stable_places=4)
    mission_id = await mission_row(db, supporter.id, 0, [{"kind": "rare_egg"}, {"kind": "great_egg"},
                                                         {"kind": "egg"}])
    await missions.claim(game_db.scene(supporter), supporter.id, mission_id, announce=False, shown=True)
    assert [(rerolls, min_class) for _player, rerolls, min_class in asked] == [(3, 900), (6, 980), (0, 0)]
    assert all(player is not None and player.id == supporter.id for player, _r, _m in asked)
    # A non-supporter's mission egg: better by class only - no seal, no foil, the base frame.
    asked.clear()
    ala = await player_with(game_db, pet_id=0)
    mission_id = await mission_row(db, ala.id, 0, [{"kind": "great_egg"}])
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=False, shown=True)
    egg = await pet_row(db, claimed.granted.lines[0]["pet_id"])
    assert egg["ultra_shiny"] and egg["seal"] == 0 and not egg["holo"] and egg["frame"] == "" and \
        not egg["holo_partial"]
    # A player who left the server: no supporter, the egg is placed all the same.
    gone = game_db.add_member("Gone")
    await add_owner(db, gone.id, 0)
    game_db.guild.members.remove(gone)
    mission_id = await mission_row(db, gone.id, 0, [{"kind": "egg"}])
    asked.clear()
    claimed = await missions.claim(game_db.bot.boss_scene(), gone.id, mission_id, announce=True, shown=False)
    assert asked[0][0] is None and claimed.granted.lines[0]["status"] == "active"


@pytest.mark.parametrize("announce", [False, True])
async def test_a_mythic_mission_egg_is_announced_only_when_the_channel_is_free(game_db, monkeypatch, announce):
    db = game_db.bot.db
    rig(monkeypatch, randint=lambda low, high: high)                           # the class roll: 1000 - mythic
    ala = await player_with(game_db, pet_id=0)
    mission_id = await mission_row(db, ala.id, 0, [{"kind": "egg"}])
    claimed = await missions.claim(game_db.scene(ala), ala.id, mission_id, announce=announce, shown=True)
    assert claimed.granted.lines[0]["mythic"] is True
    assert game_db.channel.contains("zdobył mitycznego towarzysza") is announce


async def test_a_failed_mission_brings_its_shards(game_db, dice):
    db = game_db.bot.db
    ala = await player_with(game_db)
    dice.keys = ["owce"]
    row = await missions.roll_board(game_db.bot, ala.id)
    chance = missions.chance_of(json.loads(row["offers"])[0], await pet_row(db, 5))
    dice.rolls, dice.lines = [99], [5]
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance)
    assert json.loads(sent.row["outcome"]) == {"result": "fail", "roll": 99, "line": "Wrócił z podkulonym ogonem.",
                                               "rewards": [{"kind": "shards", "n": 1}]}
    await come_back(db, row["mission_id"])
    claimed = await missions.claim(game_db.scene(ala), ala.id, row["mission_id"], announce=False, shown=True)
    assert claimed.granted.lines == [{"kind": "shards", "n": 1}]
    assert (await owner_row(db, ala.id))["reroll_scroll_shard"] == 1


async def test_from_the_board_to_the_reward(game_db, dice):
    """The whole way: roll, send, the pet is away, it comes back, the claim pays exactly the outcome."""
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=0)
    dice.tiers, dice.keys, dice.hours = [1, 2], ["grzyby", "karawana", "wulkan"], [0, 0, 2]
    row = await missions.roll_board(game_db.bot, ala.id)
    offer = json.loads(row["offers"])[2]                                       # wulkan 24 h
    chance = missions.chance_of(offer, await pet_row(db, 5))
    dice.rolls, dice.chance = [0], 0.999                                       # a Wielki sukces
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 2, "active:5", chance)
    rewards = json.loads(sent.row["outcome"])["rewards"]
    assert json.loads(sent.row["outcome"])["result"] == "great"
    assert rewards == [{"kind": "scrolls", "n": 3}, {"kind": "shards", "n": 16}]   # 1.92 * 2, 8 * 2
    assert await missions.claim(game_db.scene(ala), ala.id, row["mission_id"], announce=False, shown=True) is None
    assert await pets.away_until(game_db.bot, db, 5) is not None
    await come_back(db, row["mission_id"])
    claimed = await missions.claim(game_db.scene(ala), ala.id, row["mission_id"], announce=False, shown=True)
    assert claimed.granted.lines == rewards
    owner = await owner_row(db, ala.id)
    assert (owner["reroll_scroll"], owner["reroll_scroll_shard"]) == (3, 16)
    assert await missions.roll_board(game_db.bot, ala.id) is None                    # the period's row is there


# -- the announcement ------------------------------------------------------------------------------

async def test_the_news_of_a_mission_is_reserved_once_and_released_when_the_post_fails(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [])
    await missions.claim(game_db.bot.boss_scene(), ala.id, mission_id, announce=True, shown=False)
    reserved = await missions.reserve_announcement(game_db.bot, mission_id)
    assert reserved["mission_id"] == mission_id and reserved["announced_at"] is not None
    assert await missions.reserve_announcement(game_db.bot, mission_id) is None
    await missions.release_announcement(game_db.bot, mission_id)
    assert (await missions.reserve_announcement(game_db.bot, mission_id)) is not None


async def test_stale_news_is_marked_without_a_post(game_db):
    db = game_db.bot.db
    ala, bob = await player_with(game_db), await player_with(game_db, "Bob", pet_id=8)
    old, new = await mission_row(db, ala.id, 5, []), await mission_row(db, bob.id, 8, [])
    for mission_id, player in ((old, ala), (new, bob)):
        await missions.claim(game_db.bot.boss_scene(), player.id, mission_id, announce=True, shown=False)
    await db.execute("UPDATE PET_MISSIONS SET PAID_AT = now() - interval '13 hours' WHERE MISSION_ID = $1", old)
    assert await missions.mark_stale_news(game_db.bot) == 1
    assert await db.fetchval("SELECT ANNOUNCED_AT FROM PET_MISSIONS WHERE MISSION_ID = $1", old) is not None
    assert await db.fetchval("SELECT ANNOUNCED_AT FROM PET_MISSIONS WHERE MISSION_ID = $1", new) is None
    assert await missions.mark_stale_news(game_db.bot) == 0


# =========================================== the summoning scroll =================================

@pytest.fixture
def summons(game, monkeypatch):
    """Record ``bot.boss_cycle.summon`` calls (and whether the marker was kept meanwhile)."""
    box = type("Summons", (), {})()
    box.calls, box.result, box.error, box.markers = [], True, None, []

    async def summon(scene, rarity, **kwargs):
        box.calls.append((scene.author, rarity, kwargs))
        box.markers.append(game.bot.store.runtime_get(missions.SUMMON_MARKER))
        if box.error is not None:
            raise box.error
        return box.result

    monkeypatch.setattr(game.bot.boss_cycle, "summon", summon)
    return box


async def scrolls_of(db, player_id):
    return (await owner_row(db, player_id))["summon_scrolls"]


async def test_a_scroll_summons_a_mythic_boss(game_db, summons):
    db = game_db.bot.db
    ala = await player_with(game_db, summon_scrolls=2)
    confirms(game_db, ala)
    assert await missions.use_summon_scroll(game_db.scene(ala, "!przywolaj")) is True
    assert summons.calls == [(ala, 4, {"origin": "scroll"})]                          # BossInfo.origin
    assert summons.markers[0]["player"] == ala.id                              # kept while the boss comes ...
    assert game_db.bot.store.runtime_get(missions.SUMMON_MARKER) is None       # ... and cleared after
    assert await scrolls_of(db, ala.id) == 1
    assert game_db.channel.contains(missions.MSG_SUMMON_DONE.format(player_id=ala.id))


@pytest.mark.parametrize("situation, text", [
    ("boss", missions.MSG_SUMMON_BOSS_THERE), ("busy", missions.MSG_SUMMON_BUSY),
    ("duel", missions.MSG_SUMMON_BUSY), ("ritual", missions.MSG_SUMMON_BUSY),
])
async def test_no_scroll_is_taken_while_no_boss_can_come(game_db, summons, situation, text):
    state = game_db.bot.state
    ala = await player_with(game_db, summon_scrolls=1)
    confirms(game_db, ala)
    if situation == "boss":
        await state.set_boss_phase(BossPhase.VISIBLE)
    elif situation == "busy":
        state._busy_owner = "hunt"
    else:
        state.open_event(EventType.DUEL if situation == "duel" else EventType.RITUAL)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert summons.calls == [] and await scrolls_of(game_db.bot.db, ala.id) == 1
    assert game_db.channel.contains(text.format(player_id=ala.id))
    assert not game_db.channel.contains("Czy na pewno chcesz odczytać")
    state._busy_owner = None


@pytest.mark.parametrize("situation, text", [
    (EventType.MERCHANT, missions.MSG_SUMMON_MERCHANT), (EventType.DUNGEON, missions.MSG_SUMMON_MINE),
    (EventType.CHEST, missions.MSG_SUMMON_WAIT), (EventType.SHRINE, missions.MSG_SUMMON_WAIT),
    ("spawning off", missions.MSG_SUMMON_WAIT),
])
async def test_a_scroll_waits_for_an_open_event_the_mine_and_spawning(game_db, summons, situation, text):
    """(Owner, 2026-11-26) the scroll closes nothing: it waits for the merchant, the mine, a chest, the shrine -
    and for boss spawning (``!stopSpawnBoss``: its boss would never leave).  Nothing is taken."""
    state = game_db.bot.state
    ala = await player_with(game_db, summon_scrolls=1)
    confirms(game_db, ala)
    if situation == "spawning off":
        game_db.bot.store.runtime_set("boss_spawning_enabled", False)
    else:
        state.open_event(situation)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert summons.calls == [] and await scrolls_of(game_db.bot.db, ala.id) == 1
    assert game_db.channel.messages[-1].content == text.format(player_id=ala.id)
    assert not game_db.channel.contains("Czy na pewno chcesz odczytać")
    state.close_event()
    game_db.bot.store.runtime_set("boss_spawning_enabled", True)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is True        # ... and comes once it is over


async def test_a_typed_fight_hears_nothing_of_a_scroll(game_db, summons):
    """The fight check comes FIRST - before even the "no columns" and "no scroll" answers."""
    ala = await player_with(game_db)
    game_db.bot._mission_columns = False
    said = len(game_db.channel.bot_messages)
    with challenge.round_running(game_db.channel):
        assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert len(game_db.channel.bot_messages) == said and summons.calls == []
    # The typed command carries the fight guard too: its checks say nothing during a fight, busy or not.
    from altermmo.cogs import missions as missions_cog

    cog = missions_cog.MissionsCog(game_db.bot)
    assert {check.__qualname__.split(".")[0] for check in cog.przywolaj.checks} == {
        "boss_channel_only", "not_during_fight", "unless_busy"}
    ctx = SimpleNamespace(bot=game_db.bot, channel=game_db.channel, author=ala, guild=game_db.guild)
    with game_db.bot.state.hold_busy("hunt"), challenge.round_running(game_db.channel):
        for check in cog.przywolaj.checks:
            try:
                await check(ctx)
            except guards.SilentCheckFailure:
                pass
    assert len(game_db.channel.bot_messages) == said


def test_the_scroll_texts_name_its_new_sources_and_its_half_hour():
    assert missions.MSG_SUMMON_NONE.format(player_id=1) == (
        "Nie masz Zwoju przywołania, <@1>. Czasem wypada z bossów i najgroźniejszych potworów, przynoszą go "
        "towarzysze z najdłuższych zleceń, a podobno i Wędrowny kupiec chowa jakiś pod ladą.")
    assert ("Jeśli przez pół godziny nikt go nie zaatakuje, bestia odejdzie, a zwój obróci się w popiół."
            in missions.MSG_SUMMON_QUESTION) and missions.MSG_SUMMON_QUESTION.endswith(pets.CONFIRM_HINT)


async def test_a_question_left_unanswered_takes_nothing(game_db, summons, monkeypatch):
    ala = await player_with(game_db, summon_scrolls=1)

    async def no(scene, player, prompt, **kwargs):
        assert "Czy na pewno chcesz odczytać" in prompt
        return pets.ui.Confirmation(False)

    monkeypatch.setattr(pets, "_confirm", no)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert summons.calls == [] and await scrolls_of(game_db.bot.db, ala.id) == 1


async def test_a_boss_arriving_during_the_question_takes_nothing(game_db, summons, monkeypatch):
    ala = await player_with(game_db, summon_scrolls=1)
    real = pets._confirm

    async def meanwhile(scene, player, prompt, **kwargs):
        answer = await real(scene, player, prompt, **kwargs)
        await game_db.bot.state.set_boss_phase(BossPhase.PRE_SPAWN)
        return answer

    confirms(game_db, ala)
    monkeypatch.setattr(pets, "_confirm", meanwhile)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert summons.calls == [] and await scrolls_of(game_db.bot.db, ala.id) == 1
    assert game_db.channel.contains(missions.MSG_SUMMON_BOSS_THERE.format(player_id=ala.id))


async def test_a_boss_that_does_not_come_gives_the_scroll_back(game_db, summons):
    ala = await player_with(game_db, summon_scrolls=1)
    confirms(game_db, ala)
    summons.result = False
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert await scrolls_of(game_db.bot.db, ala.id) == 1
    assert game_db.channel.contains(missions.MSG_SUMMON_FAILED.format(player_id=ala.id))
    summons.result, summons.error = True, ConnectionError("discord down")
    with pytest.raises(ConnectionError):
        await missions.use_summon_scroll(game_db.scene(ala))
    assert await scrolls_of(game_db.bot.db, ala.id) == 1
    assert game_db.bot.store.runtime_get(missions.SUMMON_MARKER) is None


async def test_a_scroll_that_cannot_be_given_back_is_told_to_the_owner(game_db, summons, monkeypatch):
    ala = await player_with(game_db, summon_scrolls=1)
    confirms(game_db, ala)
    summons.result = False

    async def broken(bot, player_id):
        raise RuntimeError("test: no database")

    monkeypatch.setattr(pets, "return_summon_scroll", broken)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert game_db.log_channel.contains(f"Zwój przywołania gracza {ala.id} nie wrócił")
    assert game_db.log_channel.contains("!DajZwojPrzywolania")


async def test_two_scrolls_read_at_once_summon_once(game_db, summons, monkeypatch):
    ala = await player_with(game_db, summon_scrolls=2)
    bob = await player_with(game_db, "Bob", pet_id=8, summon_scrolls=1)
    asked, release = asyncio.Event(), asyncio.Event()

    async def slow_yes(scene, player, prompt, **kwargs):
        asked.set()
        await release.wait()
        return pets.ui.Confirmation(True)

    monkeypatch.setattr(pets, "_confirm", slow_yes)
    first = asyncio.ensure_future(missions.use_summon_scroll(game_db.scene(ala)))
    await asyncio.wait_for(asked.wait(), 5)
    assert await missions.use_summon_scroll(game_db.scene(bob)) is False                 # one summon at a time
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert game_db.channel.contains(missions.MSG_SUMMON_BUSY.format(player_id=bob.id))
    release.set()
    assert await first is True
    assert len(summons.calls) == 1
    assert (await scrolls_of(game_db.bot.db, ala.id), await scrolls_of(game_db.bot.db, bob.id)) == (1, 1)


async def test_no_scroll_or_no_columns(game_db, summons):
    ala = await player_with(game_db)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert game_db.channel.contains(missions.MSG_SUMMON_NONE.format(player_id=ala.id))
    game_db.bot._mission_columns = False
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    assert game_db.channel.contains(missions.MSG_SUMMON_OFF.format(player_id=ala.id))
    assert summons.calls == []


async def test_an_open_question_keeps_the_scroll(game_db, summons):
    ala = await player_with(game_db, summon_scrolls=1)
    pets._awaiting_confirmation.add(ala.id)
    try:
        assert await missions.use_summon_scroll(game_db.scene(ala)) is False
    finally:
        pets._awaiting_confirmation.discard(ala.id)
    assert game_db.channel.contains(pets.MSG_ALREADY_WAITING) and summons.calls == []


async def test_a_marker_found_at_the_start_is_told_to_the_owner_and_nothing_is_given_back(game):
    bot = game.bot
    assert missions.resolve_summon_marker(bot) is False
    bot.store.runtime_set(missions.SUMMON_MARKER, {"player": 42, "at": "2026-11-25T20:00:00+00:00"})
    bot._ready.clear()                                                         # still starting: a start-up note
    bot._startup_notes = []
    assert missions.resolve_summon_marker(bot) is True
    assert len(bot._startup_notes) == 1 and "<@42>" in bot._startup_notes[0]
    assert "!DajZwojPrzywolania" in bot._startup_notes[0]
    assert bot.store.runtime_get(missions.SUMMON_MARKER) is None
    bot._ready.set()                                                           # a reload on a running bot
    bot.store.runtime_set(missions.SUMMON_MARKER, {"player": 43, "at": "nonsense"})
    assert missions.resolve_summon_marker(bot) is True
    for _ in range(20):
        await asyncio.sleep(0)
    assert game.log_channel.contains("<@43>") and len(bot._startup_notes) == 1


# =========================================== the panel (§8) =======================================

@pytest.fixture(autouse=True)
def no_pictures(monkeypatch, tmp_path):
    """The views find no picture (``assets/missions`` is optional); a test adds its own."""
    folder = tmp_path / "mission-pictures"
    folder.mkdir()
    monkeypatch.setattr(missions, "PICTURES_DIR", folder)
    return folder


@pytest.fixture(autouse=True)
def _no_panel_notice_gap(monkeypatch):
    monkeypatch.setattr(guards, "_last_notice", {})


async def two_pets(game, name="Ala", **owner):
    """A member with Burek (a cat at level 2, at his side), Mruczek (a dog at level 3, place 2) and an egg (place 1)."""
    db = game.bot.db
    member = await player_with(game, name, pet_id_alt1=7, pet_id_alt2=6, **owner)
    await add_pet(db, 6, pet_name="Mruczek", pet_lvl=3, pet_skills=5, def_perc=20, drop_perc=9, slow_perc=30,
                  type="Dog")
    await add_pet(db, 7, pet_lvl=0)
    return member


async def open_panel(game, member):
    assert await missions.show_board(game.scene(member)) is True
    return game.last_with_buttons()


def embed_of(message):
    return message.embeds[0]


def labels(message):
    return [getattr(item, "label", None) or getattr(item, "placeholder", None) for item in message.view.children]


def picker(message):
    return next(item for item in message.view.children if isinstance(item, discord.ui.Select))


async def chance_on(db, row_offers, index, pet_id):
    return missions.chance_of(json.loads(row_offers)[index], await pet_row(db, pet_id))


async def test_the_board_shows_three_offers_with_what_they_need_and_who_fits_best(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db, reroll_scroll=12)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "brama"]
    dice.hours = [0, 0, 2]                                                              # brama 24 h
    message = await open_panel(game_db, ala)
    row = await board(db, ala.id)
    embed = embed_of(message)
    assert embed.title == "🧭 Tablica zleceń" and embed.footer.text == missions.BOARD_FOOTER
    text = embed.description
    assert text.startswith(missions.MSG_BOARD.format(player_id=ala.id))
    assert "🟩 **1. 🐑 Zagubione owce** · zwykłe · ⏳ 3 h" in text
    assert "*Stado pasterza Bartka rozbiegło się po halach - trzeba je odnaleźć przed zmrokiem.*" in text
    assert "Liczy się: 🍀 rzadszy drop 8% · 🔮 wyczuwanie bossa" in text
    assert "Łup: 🧩 fragmenty, 📜 zwoje, czasem 🥚 jajko albo ⬆️ poziom · ★ Tu częściej: 🥚 jajko" in text
    assert "🟦 **2. 🐫 Eskorta karawany** · rzadkie · ⏳ 6 h" in text
    assert "Liczy się: 🛡️ blok 15% · 🐢 spowolnienie 22%" in text
    assert "🟪 **3. 🌑 Brama Pustki** · epickie · ⏳ 24 h" in text
    assert "a bardzo rzadko 📯 Zwój przywołania · ★ Tu częściej: 📯 Zwój przywołania" in text
    best = max((await chance_on(db, row["offers"], 1, 6), 6), (await chance_on(db, row["offers"], 1, 5), 5))
    assert best[1] == 6 and f"🐾 Najlepiej pasuje: **Mruczek** (stajnia, miejsce 2) - **{best[0]}%**" in text
    assert "Nowe zlecenia <t:" in text and "Zwoje przywołania" not in text
    assert labels(message) == ["1. Zagubione owce", "2. Eskorta karawany", "3. Brama Pustki",
                               "Nowe zlecenia (5 zwojów)", "Zamknij"]
    assert [item.row for item in message.view.children] == [0, 0, 0, 1, 1]
    assert all(item.custom_id.startswith(ui.FLOW_PREFIX + "missions:") for item in message.view.children)
    assert message.files == [] and embed.image.url is None                     # no pictures: no image


def test_an_epic_offer_of_12_hours_promises_no_summon():
    offer = offer_of("brama", 0)                                                         # 12 h
    text = missions._loot_text(config(), offer)
    assert "Zwój przywołania" not in text and "★" not in text
    assert "★ Tu częściej: 🥚 jajko" in missions._loot_text(config(), offer_of("owce"))


async def test_the_pick_lists_the_pets_by_chance_and_the_best_is_chosen_already(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    message = await open_panel(game_db, ala)
    row = await board(db, ala.id)
    click = await game_db.click(message, ala, "offer:1")
    assert click.replies == [] and click.response.deferred
    embed = embed_of(message)
    assert embed.title == "🟦 🐫 Eskorta karawany - kogo wysyłasz?" and embed.color.value == 0x3B82F6
    chances = {pet_id: await chance_on(db, row["offers"], 1, pet_id) for pet_id in (5, 6)}
    select = picker(message)
    assert [option.value for option in select.options] == ["2:6", "active:5"]
    assert [option.default for option in select.options] == [True, False]
    assert select.options[0].description == f"Szansa {chances[6]}% · stajnia, miejsce 2"
    assert select.options[1].description == f"Szansa {chances[5]}% · przy Tobie"
    assert select.options[0].label == "Mruczek · Pies · poziom 3" and str(select.options[0].emoji) == "🐴"
    text = embed.description
    assert "**Mruczek** · Pies · poziom 3 (stajnia, miejsce 2)" in text and "📈 Talent: 5/9" in text
    assert "✅ 🛡️ Szansa na zablokowanie ataku: 20/15%" in text
    assert "✅ 🐢 Spowolnienie przeciwnika: 30/22%" in text
    assert f"🎯 **Szansa powodzenia: {chances[6]}%**" in text
    assert missions.MSG_WARN_STABLE.format(name="Mruczek") in text and missions.MSG_EGGS_STAY in text
    assert "rzadkie · ⏳ 6 h · wróci około <t:" in text
    assert labels(message) == [missions.PET_PLACEHOLDER, "Wyślij", "Inne zlecenie", "Zamknij"]
    assert [item.row for item in message.view.children] == [0, 1, 1, 1]
    # Another pet from the list: the view follows (and keeps it chosen after the redraw).
    await game_db.click(message, ala, "pet", values=["active:5"])
    assert [option.default for option in picker(message).options] == [False, True]
    assert missions.MSG_WARN_ACTIVE.format(hours=6) in embed_of(message).description
    assert "🟨 🛡️ Szansa na zablokowanie ataku: 7/15%" in embed_of(message).description
    assert "❌ 🐢 Spowolnienie przeciwnika: 0/22%" in embed_of(message).description
    # Back to the board, and another offer.
    await game_db.click(message, ala, "back")
    assert embed_of(message).title == missions.BOARD_TITLE and "offer:0" in message.view.children[0].custom_id


async def test_wyslij_sends_the_chosen_pet_with_the_chance_it_showed(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    message = await open_panel(game_db, ala)
    await game_db.click(message, ala, "offer:1")
    shown = picker(message).options[0]
    click = await game_db.click(message, ala, "send")
    assert click.replies == []
    stored = await board(db, ala.id)
    assert stored["status"] == "away" and stored["pet_id"] == 6 and stored["pet_place"] == 2
    assert f"Szansa {stored['chance']}%" in shown.description
    embed = embed_of(message)
    assert embed.title == missions.PROGRESS_TITLE
    assert embed.description.startswith(missions.MSG_SENT.format(name="Mruczek"))
    assert "Twój towarzysz **Mruczek** jest na zleceniu 🟦 **Eskorta karawany** (rzadkie)" in embed.description
    assert "▱▱▱▱▱▱▱▱▱▱▱▱ **0%**" in embed.description and missions.STAGES[0][1] in embed.description
    assert missions.MSG_AWAY_STABLE in embed.description and f"szansa powodzenia {stored['chance']}%" in \
        embed.description
    assert labels(message) == ["Odśwież", "Zawołaj z powrotem", "Zamknij"]
    assert {item.row for item in message.view.children} == {0}
    # A second panel shows the progress; nothing else can be sent.
    second = await open_panel(game_db, ala)
    assert embed_of(second).title == missions.PROGRESS_TITLE and missions.MSG_SENT.split("**")[0] not in \
        embed_of(second).description


async def test_a_changed_chance_is_told_and_nothing_is_sent(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    message = await open_panel(game_db, ala)
    await game_db.click(message, ala, "offer:1")
    await db.execute("UPDATE PETS SET DEF_PERC = 1, SLOW_PERC = 1 WHERE PET_ID = 6")    # !odrodzenie meanwhile
    click = await game_db.click(message, ala, "send")
    row = await board(db, ala.id)
    assert row["status"] == "offered"
    new = await chance_on(db, row["offers"], 1, 6)
    assert click.answers == [missions.MSG_SEND_CHANCE.format(name="Mruczek", chance=new, player_id=ala.id)]
    # The list now shows the chance as it is (Mruczek stays chosen) - and Wyślij sends with that one.
    assert embed_of(message).title.endswith("kogo wysyłasz?")
    assert f"🎯 **Szansa powodzenia: {new}%**" in embed_of(message).description
    await game_db.click(message, ala, "send")
    assert (await board(db, ala.id))["status"] == "away" and (await board(db, ala.id))["chance"] == new


async def test_a_pet_that_moved_since_the_list_is_told(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    message = await open_panel(game_db, ala)
    await game_db.click(message, ala, "offer:1")
    await db.execute("UPDATE PETOWNER SET PET_ID_ALT2 = 0, PET_ID_ALT3 = 6 WHERE PLAYER_ID = $1", ala.id)
    click = await game_db.click(message, ala, "send")
    assert click.answers == [missions.MSG_SEND_CHANGED.format(player_id=ala.id)]
    assert (await board(db, ala.id))["status"] == "offered"
    assert [option.value for option in picker(message).options] == ["3:6", "active:5"]
    assert picker(message).options[0].default


async def test_no_send_while_a_question_is_open_busy_or_during_a_fight(game_db, dice):
    db = game_db.bot.db
    state = game_db.bot.state
    ala = await two_pets(game_db)
    message = await open_panel(game_db, ala)
    await game_db.click(message, ala, "offer:0")
    pets._awaiting_confirmation.add(ala.id)
    try:
        click = await game_db.click(message, ala, "send")
    finally:
        pets._awaiting_confirmation.discard(ala.id)
    assert click.answers == [pets.MSG_ALREADY_WAITING]
    state._busy_owner = "training"
    with state.stable_stays_open():                                     # the training's sign-up
        click = await game_db.click(message, ala, "send")
        assert click.answers == [ui.MSG_BUSY]
        await game_db.click(message, ala, "back")                       # the view still changes
        assert embed_of(message).title == missions.BOARD_TITLE
    state._busy_owner = None
    with challenge.round_running(game_db.channel):                       # a typed fight: nothing at all
        before = list(message.edits)
        click = await game_db.click(message, ala, "offer:0")
        assert click.replies == [] and message.edits == before
        click = await game_db.click(message, ala, "close")
    assert (await board(db, ala.id))["status"] == "offered"


async def test_nowe_zlecenia_costs_five_scrolls_once(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db, reroll_scroll=9)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    message = await open_panel(game_db, ala)
    dice.tiers, dice.keys = [1, 2], ["miod", "wataha", "kraken"]
    click = await game_db.click(message, ala, "reroll")
    assert click.replies == []
    row = await board(db, ala.id)
    assert row["rerolls"] == 1 and [offer["key"] for offer in json.loads(row["offers"])] == ["miod", "wataha",
                                                                                              "kraken"]
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 4
    assert labels(message) == ["1. Miód dzikich pszczół", "2. Wataha z Wilczego Jaru", "3. Kraken z Głębin",
                               "Zamknij"]


async def test_a_reroll_without_the_scrolls_says_what_is_missing(game_db, own_config):
    db = game_db.bot.db
    ala = await two_pets(game_db, reroll_scroll=4)
    message = await open_panel(game_db, ala)
    before = (await board(db, ala.id))["offers"]
    click = await game_db.click(message, ala, "reroll")
    assert click.answers == [missions.MSG_REROLL_POOR.format(price="5 zwojów", have="4 zwoje", player_id=ala.id)]
    assert (await board(db, ala.id))["offers"] == before
    edit_config(own_config, lambda raw: raw.update(reroll_price=0))
    second = await open_panel(game_db, ala)
    assert "Nowe zlecenia (5 zwojów)" not in labels(second)


async def test_a_panel_that_shows_an_old_board_redraws_it(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db, reroll_scroll=10)
    first = await open_panel(game_db, ala)
    second = await open_panel(game_db, ala)
    await game_db.click(second, ala, "reroll")                                            # through the other panel
    click = await game_db.click(first, ala, "offer:0")
    assert click.answers == [missions.MSG_PANEL_OLD.format(player_id=ala.id)]
    assert embed_of(first).title == missions.BOARD_TITLE
    offers = json.loads((await board(db, ala.id))["offers"])
    assert first.view.children[0].label == f"1. {missions.find_mission(config(), offers[0]['key']).name}"


def test_the_progress_bar_and_its_lines():
    sent = datetime(2026, 11, 25, 12, 0, tzinfo=clock.UTC)
    ends = sent + timedelta(hours=8)
    assert missions.progress(sent, ends, sent) == ("▱" * 12, 0, 0.0)
    bar, percent, share = missions.progress(sent, ends, sent + timedelta(hours=3))
    assert (bar, percent, share) == ("▰" * 4 + "▱" * 8, 37, 0.375)
    assert missions.progress(sent, ends, ends + timedelta(hours=1)) == ("▰" * 12, 100, 1.0)
    assert missions.progress(sent, ends, sent - timedelta(hours=1))[1] == 0
    assert [missions.stage_of(share) for share in (0.0, 0.3, 0.6, 0.9, 1.0)] == [
        "Wyrusza w drogę, ogon do góry...", "Jest już daleko od domu...", "Najtrudniejsze dopiero przed nim...",
        "Wraca! Już widać go na horyzoncie.", "Wraca! Już widać go na horyzoncie."]
    assert missions.stage_of(0.5, due=True) == missions.STAGE_DUE


async def test_odswiez_shows_the_result_once_the_pet_is_back(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 2}, {"kind": "stone"}], due=False)
    message = await open_panel(game_db, ala)
    assert embed_of(message).title == missions.PROGRESS_TITLE
    assert missions.MSG_AWAY_ACTIVE in embed_of(message).description
    await come_back(db, mission_id)
    await game_db.click(message, ala, "refresh")
    embed = embed_of(message)
    assert embed.title == "✅ Burek wrócił ze zlecenia!" and embed.color.value == missions.COLOUR_SUCCESS
    assert "Otrzymujesz:\n👉 2 zwoje odrodzenia\n👉 💎 Kamień olśnienia" in embed.description
    assert "*Udało się.*" in embed.description
    row = await db.fetchrow("SELECT * FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id)
    assert row["status"] == "paid" and row["seen_at"] is not None and row["announced_at"] is not None
    # The mission was of this period: the next board comes after the reset ("Dalej" -> done for today).
    assert labels(message) == ["Dalej", "Zamknij"] and missions.MSG_NEXT_LATER.split(" {")[0] in embed.description
    await game_db.click(message, ala, "board")
    assert embed_of(message).description.startswith(missions.MSG_DONE.split(",")[0])
    assert "Ostatnio: ✅ **Burek** - 🟦 „Eskorta karawany”" in embed_of(message).description
    assert labels(message) == ["Zamknij"]


async def test_a_mission_of_yesterday_leads_to_a_new_board(game_db, dice):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [], result="fail",
                                   period=missions.current_period() - timedelta(days=1))
    await db.execute("UPDATE PET_MISSIONS SET OUTCOME = $2 WHERE MISSION_ID = $1", mission_id,
                     json.dumps({"result": "fail", "roll": 90, "line": "Pogoda pokrzyżowała plany.",
                                 "rewards": [{"kind": "shards", "n": 1}]}))
    message = await open_panel(game_db, ala)
    embed = embed_of(message)
    assert embed.title == "❌ Burek wrócił z pustymi łapami" and embed.color.value == missions.COLOUR_FAIL
    assert "się nie powiodło <:Sadge:936907659142111273> (szansa była 60%)" in embed.description
    assert "Na pocieszenie przyniósł:\n👉 1 fragment zwoju" in embed.description
    assert missions.MSG_NEXT_NOW in embed.description
    assert labels(message) == ["Tablica zleceń", "Zamknij"]
    assert message.view.children[0].style is discord.ButtonStyle.primary
    await game_db.click(message, ala, "board")
    assert embed_of(message).title == missions.BOARD_TITLE and (await board(db, ala.id))["status"] == "offered"


async def test_zawolaj_z_powrotem_after_the_question(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 2}], due=False)
    message = await open_panel(game_db, ala)
    confirms(game_db, ala)
    await game_db.click(message, ala, "recall")
    assert game_db.channel.ever_sent("Czy na pewno chcesz zawołać **Burek** z powrotem")
    row = await db.fetchrow("SELECT * FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id)
    assert row["status"] == "recalled" and row["seen_at"] is not None
    assert await pets.away_until(game_db.bot, db, 5) is None
    embed = embed_of(message)
    assert embed.title == "↩️ Burek wrócił na Twoje wezwanie" and "przerwał zlecenie" in embed.description
    assert "Otrzymujesz" not in embed.description and (await owner_row(db, ala.id))["reroll_scroll"] == 0
    await game_db.click(message, ala, "board")                                          # used up: no new board
    assert embed_of(message).description.startswith(missions.MSG_DONE.split(",")[0])
    assert len(await rows(db, ala.id)) == 1


async def test_a_recall_left_unanswered_changes_nothing(game_db, monkeypatch):
    db = game_db.bot.db
    ala = await player_with(game_db)
    await mission_row(db, ala.id, 5, [], due=False)
    message = await open_panel(game_db, ala)

    async def no(scene, player, prompt, **kwargs):
        return pets.ui.Confirmation(False)

    monkeypatch.setattr(pets, "_confirm", no)
    await game_db.click(message, ala, "recall")
    assert (await board(db, ala.id))["status"] == "away" and embed_of(message).title == missions.PROGRESS_TITLE


async def test_a_recall_after_the_end_shows_what_the_pet_brought(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "mirror"}], due=False)
    message = await open_panel(game_db, ala)
    await come_back(db, mission_id)
    click = await game_db.click(message, ala, "recall")
    assert click.answers == [missions.MSG_RECALL_LATE.format(name="Burek", player_id=ala.id)]
    assert (await board(db, ala.id))["status"] == "paid" and (await owner_row(db, ala.id))["mirrors"] == 1
    assert "🪞 Lustro wizualiów" in embed_of(message).description


def test_every_result_line():
    line = missions.result_line
    assert line({"kind": "scrolls", "n": 1}) == "1 zwój odrodzenia"
    assert line({"kind": "shards", "n": 12}) == "12 fragmentów zwoju"
    assert line({"kind": "egg", "class": "rare_egg", "status": "stable", "place": 5, "shiny": True}) == (
        "🥚 Rzadkie jajko - trafiło do stajni (miejsce 5) - *Rzadki towarzysz. Miałeś niesamowite szczęście.*")
    assert line({"kind": "egg", "class": "egg", "status": "active"}) == "🥚 Jajko - jest teraz przy Tobie"
    assert line({"kind": "egg", "class": "great_egg", "status": "pouch", "ultra": True}) == (
        "🥚 Nadzwyczajne jajko - trafiło do sakwy - *Absolutnie nadzwyczajny towarzysz.*")
    assert line({"kind": "egg", "class": "great_egg", "comp": {"scrolls": 3}}) == (
        "🥚 Nadzwyczajne jajko - nie było dla niego miejsca (przy Tobie, w sakwie ani w stajni), więc dostajesz "
        "3 zwoje odrodzenia")
    assert line({"kind": "level", "name": "Mruczek", "new": 3}) == "⬆️ **Mruczek** awansuje na poziom 3!"
    assert line({"kind": "level", "name": "Burek", "comp": {"shards": 3}}) == (
        "⬆️ Poziom - Burek nie może już awansować, więc dostajesz 3 fragmenty zwoju")
    assert line({"kind": "stone"}) == "💎 Kamień olśnienia" and line({"kind": "mirror"}) == "🪞 Lustro wizualiów"
    assert line({"kind": "place", "places": 7}) == "🐴 Nowe miejsce w stajni - masz ich teraz 7!"
    assert line({"kind": "place", "comp": {"scrolls": 10}}) == (
        "🐴 Miejsce w stajni - masz już wszystkie, więc dostajesz 10 zwojów odrodzenia")
    assert line({"kind": "pouch", "to": "pouch"}).startswith("👝 **Sakwa na jajko!**")
    assert line({"kind": "pouch", "to": "big"}) == "🎒 **Duża sakwa!** Twoja sakwa mieści teraz dwa jajka."
    assert line({"kind": "pouch", "comp": {"scrolls": 15}}).endswith("więc dostajesz 15 zwojów odrodzenia")
    assert line({"kind": "summon", "count": 1}).startswith("📯 **Zwój przywołania!**")
    assert line({"kind": "summon", "cap": 3, "comp": {"scrolls": 20}}) == (
        "📯 Zwój przywołania - masz już 3, więc dostajesz 20 zwojów odrodzenia")
    assert line({"recalled": True}) is None
    # The thumbnail: the most notable item (never a compensation).
    thumb = missions._thumbnail
    assert thumb([{"kind": "egg", "class": "rare_egg"}, {"kind": "place", "places": 5}]) == "item-miejsce"
    assert thumb([{"kind": "pouch", "to": "big"}, {"kind": "summon", "count": 1}]) == "item-zwoj-przywolania"
    assert thumb([{"kind": "summon", "comp": {"scrolls": 20}}, {"kind": "pouch", "to": "pouch"}]) == "item-sakwa"
    assert thumb([{"kind": "scrolls", "n": 2}]) is None


def test_the_titles_of_a_result_and_the_notes():
    def row(result, status="paid", lines=(), notes=()):
        return {"status": status, "chance": 42, "chosen": 0, "offers": json.dumps([offer_of("smok", 2)]),
                "pet_snapshot": json.dumps({"name": "Mruczek"}),
                "outcome": json.dumps({"result": result, "line": "Smok nawet się nie obudził.", "rewards": []}),
                "result": json.dumps({"lines": list(lines), "notes": list(notes)})}

    great = missions.result_embed(7, row("great", lines=[{"kind": "scrolls", "n": 4}], notes=["📖 notatka"]),
                                  config(), next_board=True).embed
    assert great.title == "🌟 Wielki sukces! Mruczek wrócił ze zlecenia!"
    assert "wykonał zlecenie 🟪 **Legowisko Starego Smoka** brawurowo - łup jest podwójny! (szansa była 42%)" in \
        great.description
    assert "👉 4 zwoje odrodzenia\n\n📖 notatka\n\nNowe zlecenia <t:" in great.description
    recalled = missions.result_embed(7, row("success", status="recalled"), config()).embed
    assert recalled.title == "↩️ Mruczek wrócił na Twoje wezwanie" and "Smok nawet" not in recalled.description
    assert recalled.color.value == missions.COLOUR_RECALLED


async def test_the_pictures_are_used_when_they_are_there(game_db, dice, no_pictures):
    for name in ("tablica", "zwykle", "item-miejsce", "sukces"):
        (no_pictures / f"{name}.png").write_bytes(b"\x89PNG not really")
    (no_pictures / "droga.png").write_bytes(b"")                                          # empty: not a picture
    ala = await two_pets(game_db)
    message = await open_panel(game_db, ala)
    assert message.files == ["tablica.png"] and embed_of(message).image.url == "attachment://tablica.png"
    await game_db.click(message, ala, "offer:0")
    assert message.files == ["zwykle.png"] and embed_of(message).image.url == "attachment://zwykle.png"
    await game_db.click(message, ala, "send")
    assert message.files == [] and embed_of(message).image.url is None                  # no "droga" picture
    shown = missions.result_embed(1, {"status": "paid", "chance": 50, "outcome": json.dumps({"result": "success"}),
                                      "result": json.dumps({"lines": [{"kind": "place", "places": 5}]})})
    assert [path.name for path in shown.files] == ["sukces.png", "item-miejsce.png"]
    assert shown.embed.thumbnail.url == "attachment://item-miejsce.png"


async def test_no_panel_without_a_pet_that_could_go(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db, level=0)                                            # only an egg
    assert await missions.show_board(game_db.scene(ala)) is False
    assert game_db.channel.contains(missions.MSG_NO_PET.format(player_id=ala.id))
    assert await rows(db, ala.id) == []
    ola = game_db.add_member("Ola")                                                      # no row at all
    assert await missions.show_board(game_db.scene(ola)) is False


async def test_the_board_makes_the_missing_table_and_is_unavailable_when_it_cannot(game_db, monkeypatch):
    db = game_db.bot.db
    ala = await player_with(game_db)
    await db.execute("DROP TABLE PET_MISSIONS", idempotent=True)
    game_db.bot._pet_missions = (False, clock.monotonic())
    assert await missions.show_board(game_db.scene(ala)) is True                       # made at the first open
    assert (await board(db, ala.id))["status"] == "offered"
    await db.execute("DROP TABLE PET_MISSIONS", idempotent=True)
    game_db.bot._pet_missions = (False, clock.monotonic())

    async def cannot(bot):
        return False

    monkeypatch.setattr(missions, "ensure_table", cannot)
    assert await missions.show_board(game_db.scene(ala)) is False
    assert game_db.channel.contains(missions.MSG_UNAVAILABLE.format(player_id=ala.id))


async def test_a_broken_config_is_answered_and_told_to_the_owner_once(game_db, own_config):
    ala = await player_with(game_db)
    own_config.write_text("{", encoding="utf-8")
    for _ in range(2):
        assert await missions.show_board(game_db.scene(ala)) is False
    assert game_db.channel.count(missions.MSG_UNAVAILABLE.format(player_id=ala.id)) == 2
    assert sum("missionsConfig.json" in line for line in game_db.bot.owner_log) == 1


async def test_the_summoning_button_reads_a_scroll(game_db, monkeypatch):
    ala = await two_pets(game_db, summon_scrolls=2)
    used = []

    async def use(scene):
        used.append(scene.author)
        await game_db.bot.db.execute("UPDATE PETOWNER SET SUMMON_SCROLLS = 1 WHERE PLAYER_ID = $1", ala.id)
        return True

    monkeypatch.setattr(missions, "use_summon_scroll", use)
    message = await open_panel(game_db, ala)
    assert "📯 Zwoje przywołania: 2 - odczytasz je przyciskiem poniżej albo komendą **!przywolaj**." in \
        embed_of(message).description
    button = message.button("summon")
    assert button.label == "Zwój przywołania (2)" and button.row == 1 and button.style is discord.ButtonStyle.danger
    await game_db.click(message, ala, "summon")
    assert used == [ala] and message.button("summon").label == "Zwój przywołania (1)"


async def test_the_panel_is_its_players_and_zamknij_closes_it(game_db):
    ala = await two_pets(game_db)
    ola = game_db.add_member("Ola")
    message = await open_panel(game_db, ala)
    click = await game_db.click(message, ola, "offer:0")
    assert click.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    await game_db.click(message, ala, "close")
    assert message.view is None


async def test_the_hub_button_shows_the_board_on_the_boss_channel_only(game_db):
    from fakes import FakeInteraction

    ala = await two_pets(game_db)
    interaction = FakeInteraction(game_db.bot, ala, channel=game_db.channel)
    assert await hub.missions_clicked(interaction) is True
    assert embed_of(game_db.last_with_buttons()).title == missions.BOARD_TITLE
    elsewhere = FakeInteraction(game_db.bot, ala, channel=game_db.log_channel)
    assert await hub.missions_clicked(elsewhere) is False
    assert elsewhere.answers == [ui.MSG_WRONG_CHANNEL.format(channel_id=game_db.bot.ids.boss_channel)]


# =========================================== the notifier (§9) ====================================

@pytest.fixture
def quiet(game_db):
    """The boss channel is tidied after the start and nobody holds it."""
    game_db.bot.boss_cycle._tidied = True
    return game_db


async def test_the_notifier_pays_and_posts_with_the_players_button(quiet, monkeypatch):
    game, db = quiet, quiet.bot.db
    ala = await player_with(game)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 2}])

    def never(*args, **kwargs):
        raise AssertionError("the notifier must not take the channel")

    monkeypatch.setattr(io, "clear_channel", never)
    monkeypatch.setattr(game.bot.state, "open_event", never)
    monkeypatch.setattr(game.bot.state, "running_event", never)
    monkeypatch.setattr(game.bot.state, "hold_busy", never)
    assert await missions.tick(game.bot) == 1
    row = await db.fetchrow("SELECT * FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id)
    assert row["status"] == "paid" and row["announced_at"] is not None and row["seen_at"] is None
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 2
    post = game.channel.messages[-1]
    assert post.content == missions.MSG_RETURN_PING.format(player_id=ala.id)
    assert post.embeds[0].title == "✅ Burek wrócił ze zlecenia!"
    (button,) = post.view.children
    assert button.custom_id == f"altermmo:show:missions:{ala.id}:{mission_id}"
    assert await missions.tick(game.bot) == 0                                          # told once


async def test_the_notifier_waits_for_a_quiet_channel(quiet):
    game, db = quiet, quiet.bot.db
    state = game.bot.state
    ala = await player_with(game)
    await mission_row(db, ala.id, 5, [])
    game.bot.boss_cycle._tidied = False                                                  # (still clearing after a start)
    assert await missions.tick(game.bot) == 0
    game.bot.boss_cycle._tidied = True
    state._busy_owner = "hunt"
    assert await missions.tick(game.bot) == 0
    state._busy_owner = None
    state.open_event(EventType.DUEL)                                                     # a sign-up holds no busy flag
    assert await missions.tick(game.bot) == 0
    with challenge.round_running(game.channel):
        assert await missions.tick(game.bot) == 0
    assert (await board(db, ala.id))["status"] == "away"                               # not even paid
    state.close_event()
    state.open_event(EventType.CHEST)                                                    # a passive event: posts
    assert await missions.tick(game.bot) == 1


async def test_the_notifier_posts_three_at_most_and_retries_a_failed_post(quiet):
    game, db = quiet, quiet.bot.db
    for number in range(5):
        member = await player_with(game, f"Gracz{number}", pet_id=20 + number)
        await mission_row(db, member.id, 20 + number, [])
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("test: Discord says no"), 1
    assert await missions.tick(game.bot) == 2                                           # one failed
    assert await db.fetchval("SELECT COUNT(*) FROM PET_MISSIONS WHERE STATUS = 'paid' AND ANNOUNCED_AT IS NULL") \
        == 1
    assert await missions.tick(game.bot) == 3                                           # the failed one, two more
    assert await missions.tick(game.bot) == 0
    assert game.channel.count("Twój towarzysz wrócił ze zlecenia!") == 5


async def test_stale_news_is_not_posted(quiet):
    game, db = quiet, quiet.bot.db
    ala = await player_with(game)
    mission_id = await mission_row(db, ala.id, 5, [])
    await missions.claim(game.scene(ala), ala.id, mission_id, announce=False, shown=False)
    await db.execute("UPDATE PET_MISSIONS SET PAID_AT = now() - interval '13 hours'")
    assert await missions.tick(game.bot) == 0
    assert not game.channel.contains("wrócił ze zlecenia")
    assert await db.fetchval("SELECT ANNOUNCED_AT FROM PET_MISSIONS") is not None
    message = await open_panel(game, ala)                                               # ... but his panel shows it
    assert embed_of(message).title == "✅ Burek wrócił ze zlecenia!"


async def test_a_mission_that_cannot_be_paid_backs_off_and_the_owner_hears_once(quiet, monkeypatch):
    game, db = quiet, quiet.bot.db
    ala = await player_with(game)
    mission_id = await mission_row(db, ala.id, 5, [])
    calls = []

    async def broken(scene, player_id, mission, **kwargs):
        calls.append(mission)
        raise RuntimeError("test: poison")

    monkeypatch.setattr(missions, "claim", broken)
    for _ in range(5):
        assert await missions.tick(game.bot) == 0
    assert calls == [mission_id] * missions.POISON_AFTER                                # then hourly
    notes = [line for line in game.bot.owner_log if f"Zlecenie {mission_id} gracza {ala.id}" in line]
    assert len(notes) == 1 and "RuntimeError: test: poison" in notes[0]
    monkeypatch.undo()
    message = await open_panel(game, ala)                                               # his click pays at once
    assert embed_of(message).title == "✅ Burek wrócił ze zlecenia!"


async def test_the_post_button_marks_the_result_seen_and_opens_the_board(quiet):
    game, db = quiet, quiet.bot.db
    ala = await player_with(game)
    ola = game.add_member("Ola")
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "stone"}],
                                   period=missions.current_period() - timedelta(days=1))
    await missions.tick(game.bot)
    post = game.channel.messages[-1]
    key = f"altermmo:show:missions:{ala.id}:{mission_id}"
    click = await game.click(post, ola, key)
    assert click.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    click = await game.click(post, ala, key)
    assert click.response.deferred
    assert await db.fetchval("SELECT SEEN_AT FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id) is not None
    assert embed_of(game.last_with_buttons()).title == missions.BOARD_TITLE            # no second result


async def test_a_result_whose_post_is_gone_is_shown_once_in_the_panel(quiet):
    game, db = quiet, quiet.bot.db
    ala = await player_with(game)
    await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 3}],
                      period=missions.current_period() - timedelta(days=1))
    await missions.tick(game.bot)
    game.channel.messages.clear()                                                       # purged before he read it
    message = await open_panel(game, ala)
    assert embed_of(message).title == "✅ Burek wrócił ze zlecenia!" and "3 zwoje odrodzenia" in \
        embed_of(message).description
    again = await open_panel(game, ala)                                                 # once: then the board
    assert embed_of(again).title == missions.BOARD_TITLE
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 3                         # paid once


async def test_a_restart_after_the_claim_shows_the_stored_result_and_pays_nothing_again(quiet, tmp_path):
    """The bot keeps nothing about a mission in memory: a new bot over the same database (a restart
    between the notifier's claim and the player's look) shows the result from RESULT, once."""
    import fakes

    game, db = quiet, quiet.bot.db
    ala = await player_with(game)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 3}, {"kind": "stone"}])
    assert await missions.tick(game.bot) == 1                                           # paid and posted
    stored = await result_of(db, mission_id)
    assert stored["lines"] == [{"kind": "scrolls", "n": 3}, {"kind": "stone"}]

    restarted = await fakes.make_game(tmp_path / "restarted")
    try:
        restarted.bot.db = db
        restarted.bot.boss_cycle._tidied = True
        same = restarted.add_member("Ala", member_id=ala.id)
        assert await missions.tick(restarted.bot) == 0                                  # told already: no 2nd post
        message = await open_panel(restarted, same)
        embed = embed_of(message)
        assert embed.title == "✅ Burek wrócił ze zlecenia!"
        assert "3 zwoje odrodzenia" in embed.description and "Kamień olśnienia" in embed.description
        assert embed.description.count(missions.GOT_MARK) == 2
        done = embed_of(await open_panel(restarted, same))                              # seen: today's board is done
        assert done.title == missions.BOARD_TITLE and "Dzisiejsze zlecenie już za Tobą" in done.description
        assert await result_of(db, mission_id) == stored
        owner = await owner_row(db, ala.id)
        assert (owner["reroll_scroll"], owner["rebirth_stones"]) == (3, 1)               # paid once
    finally:
        await restarted.cleanup()
        await restarted.bot.supervisor.stop_all()


# =========================================== the cog ==============================================

async def typed(game, member, content):
    from fakes import FakeMessage

    message = FakeMessage(game.channel, member, content)
    message.edited_at = None
    message._state = None
    game.channel.messages.append(message)
    await game.bot.invoke(await game.bot.get_context(message))
    await game.settle()
    return message


@pytest_asyncio.fixture
async def missions_cog(game_db, monkeypatch):
    started = []
    monkeypatch.setattr(game_db.bot.supervisor, "start", lambda name, factory: started.append(name))
    await game_db.bot.load_extension("altermmo.cogs.missions")
    cog = game_db.bot.get_cog("Zlecenia")
    cog.started = started
    return cog


async def test_the_cog_runs_the_notifier_and_answers_the_commands(game_db, missions_cog, monkeypatch):
    assert missions_cog.started == [missions.WATCH_NAME]
    ala = await two_pets(game_db)
    await typed(game_db, ala, "!zl")
    assert embed_of(game_db.last_with_buttons()).title == missions.BOARD_TITLE
    used = []

    async def use(scene):
        used.append(scene.author)
        return True

    monkeypatch.setattr(missions, "use_summon_scroll", use)
    await typed(game_db, ala, "!przywołaj")
    assert used == [ala]
    command = game_db.bot.get_command("zlecenia")
    assert set(command.aliases) == {"zlecenie", "zl"}
    assert set(game_db.bot.get_command("przywolaj").aliases) == {"przywołaj", "przywolanie", "przywołanie"}


async def test_the_owners_commands(game_db, missions_cog):
    db = game_db.bot.db
    ala = await player_with(game_db, summon_scrolls=1)
    mission_id = await mission_row(db, ala.id, 5, [], due=False)
    assert await missions.end_now(game_db.bot, ala) == mission_id
    assert await pets.away_until(game_db.bot, db, 5) is None
    assert await missions.end_now(game_db.bot, ala) is None                           # nothing away any more
    ctx = SimpleNamespace(channel=game_db.channel, message=game_db.scene(ala).message)
    await missions_cog.give_summon.callback(missions_cog, ctx, ala, 5)                 # no cap (2026-11-26)
    assert (await owner_row(db, ala.id))["summon_scrolls"] == 6
    assert game_db.channel.contains(f"Gracz <@{ala.id}> ma teraz Zwoje przywołania: 6.")
    await missions_cog.give_summon.callback(missions_cog, ctx, ala, -9)                # a minus takes, never below 0
    assert (await owner_row(db, ala.id))["summon_scrolls"] == 0
    assert game_db.channel.contains(f"Gracz <@{ala.id}> ma teraz Zwoje przywołania: 0.")
    await missions_cog.end_mission.callback(missions_cog, ctx, ala)
    assert game_db.channel.contains(f"Gracz <@{ala.id}> nie ma towarzysza na zleceniu.")


# ================================= the review's fixes (rev. 2 follow-up) ==========================

async def test_a_send_on_a_board_rerolled_since_the_panel_showed_it_is_refused(game_db, dice):
    """A reroll keeps the board's id and status: the shown reroll count tells the boards apart."""
    db = game_db.bot.db
    ala = await player_with(game_db, reroll_scroll=5)
    dice.keys = ["owce"]
    row = await missions.roll_board(game_db.bot, ala.id)
    dice.keys = ["miod"]
    assert await missions.reroll(game_db.bot, ala, row["mission_id"]) == "ok"
    new = await board(db, ala.id)
    chance = missions.chance_of(json.loads(new["offers"])[0], await pet_row(db, 5))
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance, rerolls=0)
    assert sent.status == "stale" and (await board(db, ala.id))["status"] == "offered"
    sent = await missions.send(game_db.bot, ala, row["mission_id"], 0, "active:5", chance, rerolls=1)
    assert sent.status == "ok" and json.loads(sent.row["offers"])[0]["key"] == "miod"


async def test_wyslij_in_a_panel_left_behind_by_a_reroll_sends_nothing(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db, reroll_scroll=5)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    first = await open_panel(game_db, ala)
    await game_db.click(first, ala, "offer:0")                                            # PICK: "Zagubione owce"
    second = await open_panel(game_db, ala)
    dice.tiers, dice.keys = [1, 2], ["miod", "wataha", "kraken"]
    await game_db.click(second, ala, "reroll")
    click = await game_db.click(first, ala, "send")
    assert click.answers == [missions.MSG_PANEL_OLD.format(player_id=ala.id)]
    assert (await board(db, ala.id))["status"] == "offered"
    assert embed_of(first).title == missions.BOARD_TITLE and labels(first)[0] == "1. Miód dzikich pszczół"


async def test_a_double_click_on_wyslij_sends_once_and_says_nothing_more(game_db, dice):
    db = game_db.bot.db
    ala = await two_pets(game_db)
    message = await open_panel(game_db, ala)
    await game_db.click(message, ala, "offer:0")
    clicks = await asyncio.gather(game_db.click(message, ala, "send"), game_db.click(message, ala, "send"))
    assert [click.answers for click in clicks] == [[], []]                    # no "Najpierw wybierz towarzysza"
    assert (await board(db, ala.id))["status"] == "away"
    assert embed_of(message).title == missions.PROGRESS_TITLE


async def test_a_double_click_on_zawolaj_asks_once_and_keeps_the_result(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    await mission_row(db, ala.id, 5, [], due=False)
    message = await open_panel(game_db, ala)
    confirms(game_db, ala)
    clicks = await asyncio.gather(game_db.click(message, ala, "recall"), game_db.click(message, ala, "recall"))
    assert [click.answers for click in clicks] == [[], []]                    # no "Tablica zmieniła się"
    assert len([sent for sent in game_db.channel.sent if "Czy na pewno chcesz zawołać" in (sent.content or "")]) \
        == 1
    assert (await board(db, ala.id))["status"] == "recalled"
    assert embed_of(message).title == "↩️ Burek wrócił na Twoje wezwanie"


async def test_a_double_click_on_nowe_zlecenia_pays_once(game_db, own_config):
    db = game_db.bot.db
    edit_config(own_config, lambda raw: raw.update(rerolls_per_period=2))
    ala = await two_pets(game_db, reroll_scroll=20)
    message = await open_panel(game_db, ala)
    clicks = await asyncio.gather(game_db.click(message, ala, "reroll"), game_db.click(message, ala, "reroll"))
    assert [click.answers for click in clicks] == [[], []]
    assert (await board(db, ala.id))["rerolls"] == 1 and (await owner_row(db, ala.id))["reroll_scroll"] == 15
    # The backend alone: a reroll of a board that is not the one shown pays nothing.
    row = await board(db, ala.id)
    assert await missions.reroll(game_db.bot, ala, row["mission_id"], rerolls=0) == "stale"
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 15
    assert await missions.reroll(game_db.bot, ala, row["mission_id"], rerolls=1) == "ok"
    assert await missions.reroll(game_db.bot, ala, row["mission_id"], rerolls=2) == "used"


async def test_a_reroll_whose_price_changed_since_the_button_pays_nothing(game_db, own_config):
    db = game_db.bot.db
    ala = await two_pets(game_db, reroll_scroll=20)
    message = await open_panel(game_db, ala)
    assert "Nowe zlecenia (5 zwojów)" in labels(message)
    edit_config(own_config, lambda raw: raw.update(reroll_price=6))
    click = await game_db.click(message, ala, "reroll")
    assert click.answers == [missions.MSG_REROLL_PRICE.format(price="6 zwojów", player_id=ala.id)]
    assert (await board(db, ala.id))["rerolls"] == 0 and (await owner_row(db, ala.id))["reroll_scroll"] == 20
    assert "Nowe zlecenia (6 zwojów)" in labels(message)
    await game_db.click(message, ala, "reroll")                                           # now at the price shown
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 14


@pytest.mark.parametrize("phase", [BossPhase.ENGAGED, BossPhase.FIGHTING])
async def test_no_pet_leaves_while_a_boss_fight_runs(game_db, phase):
    """The fights read the pets' stats right after the attack (before their countdown): a pet sent in that
    pause would still lend the fight its bonuses."""
    db = game_db.bot.db
    ala = await two_pets(game_db)
    message = await open_panel(game_db, ala)
    await game_db.click(message, ala, "offer:0")
    await game_db.bot.state.set_boss_phase(phase)
    click = await game_db.click(message, ala, "send")
    assert click.answers == [missions.MSG_SEND_BOSS.format(player_id=ala.id)]
    assert (await board(db, ala.id))["status"] == "offered" and embed_of(message).title.endswith("kogo wysyłasz?")
    await game_db.bot.state.set_boss_phase(BossPhase.VISIBLE)                       # waiting for an attack: fine
    await game_db.click(message, ala, "send")
    assert (await board(db, ala.id))["status"] == "away"


async def test_a_pet_picked_while_the_click_was_refused_is_shown_before_it_is_sent(game_db, dice):
    """Discord keeps the pick in the list although the gate refused it: Wyślij shows that pet first."""
    db = game_db.bot.db
    ala = await two_pets(game_db)
    dice.tiers, dice.keys = [1, 2], ["owce", "karawana", "smok"]
    message = await open_panel(game_db, ala)
    await game_db.click(message, ala, "offer:1")
    assert [option.default for option in picker(message).options] == [True, False]    # Mruczek chosen
    with challenge.round_running(game_db.channel):                                       # a typed fight: refused
        click = await game_db.click(message, ala, "pet", values=["active:5"])
    assert click.replies == [] and [option.default for option in picker(message).options] == [True, False]
    click = await game_db.click(message, ala, "send")
    assert click.answers == [missions.MSG_SEND_CHECK.format(player_id=ala.id)]
    assert (await board(db, ala.id))["status"] == "offered"
    assert [option.default for option in picker(message).options] == [False, True]    # Burek now, with his chance
    assert missions.MSG_WARN_ACTIVE.format(hours=6) in embed_of(message).description
    await game_db.click(message, ala, "send")
    row = await board(db, ala.id)
    assert row["status"] == "away" and row["pet_id"] == 5


async def test_a_result_whose_panel_was_never_posted_is_shown_again(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "scrolls", "n": 4}])
    game_db.channel.fail_with, game_db.channel.fail_sends = RuntimeError("test: Discord refuses"), 1
    with pytest.raises(RuntimeError):
        await missions.show_board(game_db.scene(ala))
    row = await db.fetchrow("SELECT * FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id)
    assert row["status"] == "paid" and row["seen_at"] is None                         # paid, but not seen
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 4
    message = await open_panel(game_db, ala)
    assert embed_of(message).title == "✅ Burek wrócił ze zlecenia!" and "4 zwoje odrodzenia" in \
        embed_of(message).description
    assert embed_of(await open_panel(game_db, ala)).title == missions.BOARD_TITLE      # then once only
    assert (await owner_row(db, ala.id))["reroll_scroll"] == 4                          # paid once


async def test_a_result_whose_panel_edit_failed_is_shown_again(game_db):
    db = game_db.bot.db
    ala = await player_with(game_db)
    mission_id = await mission_row(db, ala.id, 5, [{"kind": "stone"}], due=False)
    message = await open_panel(game_db, ala)
    await come_back(db, mission_id)
    message.deleted = True                                                              # purged by a boss
    await game_db.click(message, ala, "refresh")
    assert await db.fetchval("SELECT SEEN_AT FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id) is None
    again = await open_panel(game_db, ala)
    assert embed_of(again).title == "✅ Burek wrócił ze zlecenia!" and "Kamień olśnienia" in \
        embed_of(again).description


async def test_a_summon_confirmed_during_a_typed_fight_takes_nothing_and_posts_nothing(game_db, summons,
                                                                                      monkeypatch):
    ala = await player_with(game_db, summon_scrolls=1)
    fight = contextlib.ExitStack()

    async def yes_then_a_fight(scene, player, prompt, **kwargs):
        fight.enter_context(challenge.round_running(game_db.channel))
        return pets.ui.Confirmation(True)

    monkeypatch.setattr(pets, "_confirm", yes_then_a_fight)
    scene = game_db.scene(ala)
    before = len(game_db.channel.sent)
    with fight:
        assert await missions.use_summon_scroll(scene) is False
    assert len(game_db.channel.sent) == before and summons.calls == []                 # no "boss już jest"
    assert await scrolls_of(game_db.bot.db, ala.id) == 1


async def test_a_recall_confirmed_during_a_typed_fight_does_not_redraw(game_db, monkeypatch):
    db = game_db.bot.db
    ala = await player_with(game_db)
    await mission_row(db, ala.id, 5, [], due=False)
    message = await open_panel(game_db, ala)
    fight = contextlib.ExitStack()

    async def yes_then_a_fight(scene, player, prompt, **kwargs):
        fight.enter_context(challenge.round_running(game_db.channel))
        return pets.ui.Confirmation(True)

    monkeypatch.setattr(pets, "_confirm", yes_then_a_fight)
    edits = len(message.edits)
    with fight:
        click = await game_db.click(message, ala, "recall")
    assert (await board(db, ala.id))["status"] == "recalled"                          # the recall itself counts
    assert len(message.edits) == edits and click.replies == []


async def test_a_second_summon_of_the_same_player_is_told_to_answer_first(game_db, summons, monkeypatch):
    ala = await player_with(game_db, summon_scrolls=1)
    asked, release = asyncio.Event(), asyncio.Event()

    async def slow_yes(scene, player, prompt, **kwargs):
        asked.set()
        await release.wait()
        return pets.ui.Confirmation(True)

    monkeypatch.setattr(pets, "_confirm", slow_yes)
    first = asyncio.ensure_future(missions.use_summon_scroll(game_db.scene(ala)))
    await asyncio.wait_for(asked.wait(), 5)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False                # a double click
    assert game_db.channel.messages[-1].content == pets.MSG_ALREADY_WAITING
    assert not game_db.channel.contains(missions.MSG_SUMMON_BUSY.format(player_id=ala.id))
    release.set()
    assert await first is True and len(summons.calls) == 1


async def test_a_marker_that_cannot_be_written_does_not_cost_the_scroll(game_db, summons, monkeypatch):
    ala = await player_with(game_db, summon_scrolls=2)
    confirms(game_db, ala)

    def held(key, value):
        raise PermissionError("test: runtime.json is held by another process")

    monkeypatch.setattr(game_db.bot.store, "runtime_set", held)
    assert await missions.use_summon_scroll(game_db.scene(ala)) is True                 # the boss comes anyway
    assert await scrolls_of(game_db.bot.db, ala.id) == 1
    assert game_db.channel.contains(missions.MSG_SUMMON_DONE.format(player_id=ala.id))
    summons.result = False
    assert await missions.use_summon_scroll(game_db.scene(ala)) is False                # and it is given back
    assert await scrolls_of(game_db.bot.db, ala.id) == 1


async def test_taking_a_scroll_is_never_retried_and_an_unknown_end_is_told(game_db, summons, monkeypatch):
    from altermmo.core.db import DatabaseUnavailable

    ala = await player_with(game_db, summon_scrolls=2)
    calls = []
    real = game_db.bot.db.fetchval

    async def fetchval(query, *args, **kwargs):
        calls.append(kwargs)
        return await real(query, *args, **kwargs)

    monkeypatch.setattr(game_db.bot.db, "fetchval", fetchval)
    assert await pets.take_summon_scroll(game_db.bot, ala.id) == 1
    assert calls == [{"idempotent": False}]                    # a lost reply must not take a second scroll
    monkeypatch.setattr(game_db.bot.db, "fetchval", real)

    async def down(bot, player_id):
        raise DatabaseUnavailable("test: no reply")

    confirms(game_db, ala)
    monkeypatch.setattr(pets, "take_summon_scroll", down)
    with pytest.raises(DatabaseUnavailable):
        await missions.use_summon_scroll(game_db.scene(ala))
    assert game_db.log_channel.contains(f"Nie wiadomo, czy Zwój przywołania gracza {ala.id} został zabrany")
    assert summons.calls == []


async def test_a_transform_after_the_return_pays_the_mission_first(game_db, monkeypatch):
    """The level reward knows the pet by the TYPE it was sent with: a transform before the claim must not
    turn it into its compensation."""
    db = game_db.bot.db
    supporter = patron(game_db)
    await add_owner(db, supporter.id, 5, mirrors=2)
    await add_pet(db, 5, pet_name="Burek", pet_lvl=1, type="Cat")
    mission_id = await mission_row(db, supporter.id, 5, [{"kind": "level"}])          # ended, not claimed yet
    monkeypatch.setattr(album, "note_level_up", lambda *args, **kwargs: asyncio.sleep(0))
    confirms(game_db, supporter)
    assert await pets.transform_pet(game_db.scene(supporter), supporter) is True
    row = await db.fetchrow("SELECT * FROM PET_MISSIONS WHERE MISSION_ID = $1", mission_id)
    assert row["status"] == "paid" and row["seen_at"] is None                          # news: shown once later
    assert json.loads(row["result"])["lines"] == [{"kind": "level", "name": "Burek", "new": 2}]
    assert (await pet_row(db, 5))["pet_lvl"] == 2 and (await owner_row(db, supporter.id))["mirrors"] == 1
    # A claim that fails: the transform waits for it.
    second = await mission_row(db, supporter.id, 5, [{"kind": "level"}],
                               period=missions.current_period() + timedelta(days=1))

    async def broken(*args, **kwargs):
        raise RuntimeError("test: the claim fails")

    monkeypatch.setattr(missions, "claim", broken)
    before = await pet_row(db, 5)
    assert await pets.transform_pet(game_db.scene(supporter), supporter) is False
    assert game_db.channel.messages[-1].content == pets.MSG_PET_RETURNING.format(player_id=supporter.id)
    assert await pet_row(db, 5) == before and (await owner_row(db, supporter.id))["mirrors"] == 1
    assert (await db.fetchval("SELECT STATUS FROM PET_MISSIONS WHERE MISSION_ID = $1", second)) == "away"


# ======================================= the patch notes ==========================================

def test_the_patch_notes_entry_tells_the_players_and_spoils_nothing():
    """Entry 18 (spec-v2 §13.6): for players, spoiler-free, no numbers - and only what the game does."""
    from altermmo.cogs import missions as missions_cog_module

    # (Found by its title: its number changes when the lead merges, and every later update adds an entry.)
    [entry] = [note for note in patchnotes.load_notes() if note.title == "Zlecenia towarzyszy"]
    assert entry.date == "2026-11-25" and len(entry.changes) == 3
    text = " ".join(entry.changes)
    assert patchnotes.spoiler_in(entry) is None                              # no percentage, no forbidden word
    assert not any(char.isdigit() for char in text)                           # no chances, prices or rewards
    assert "$" not in text and "wyprawa" not in text.lower()                 # "!" commands; the mine's word
    # The button and the commands it names are the real ones.
    assert hub.PANEL_TITLE.endswith("Panel gry") and "**Zlecenia** w **Panelu gry**" in text
    view = hub.HubView()
    try:
        assert any(getattr(item, "custom_id", None) == hub.MISSIONS_ID and item.label == "Zlecenia"
                   for item in view.children)
    finally:
        view.stop()
    commands = {command.name for command in missions_cog_module.MissionsCog.__cog_commands__}
    assert {"zlecenia", "przywolaj"} <= commands and "!zlecenia" in text and "!przywolaj" in text
    for told in ("trzy zlecenia", "zwykłe, rzadkie albo epickie", "tego przy sobie albo ze stajni",
                 "zanim go wyślesz", "talent", "po dobie", "nie wyczuwa bossów", "nie trenuje",
                 "na kanale bossa", "Sakwa na jajko może tam urosnąć", "Zwój przywołania"):
        assert told in text, told
    # What players find out themselves stays out: the big pouch, the mythic boss, the missions' names.
    low = text.lower()
    assert "duża sakwa" not in low and "mityczn" not in low
    assert not any(mission.name.lower() in low for mission in missions.load_config(SHIPPED).missions)


def test_every_shipped_picture_is_found(monkeypatch):
    """The owner's pictures (WebP, 2026-11-25) are in ``assets/missions`` under the names the views ask for."""
    monkeypatch.setattr(missions, "PICTURES_DIR", missions.ASSETS_DIR / "missions")  # the real folder
    names = {missions.PICTURE_BOARD, missions.PICTURE_ROAD, *missions.PICTURE_RESULTS.values(),
             *(name for _, name in missions.THUMBNAILS), *missions.TIERS, "przywolanie"}
    assert len(names) == 16
    found = {name: missions.picture(name) for name in sorted(names)}
    assert all(path is not None and path.suffix == ".webp" for path in found.values()), found
    # Nothing in the folder that no view asks for (a typo in a file name would hide a picture).
    assert {path.stem for path in missions.PICTURES_DIR.iterdir()} == names
