"""Stats tests (session B)."""
from __future__ import annotations

import pytest

from eqoa_parser.events import Event
from eqoa_parser.segmenter import Fight
from eqoa_parser.stats import FightSummary, effective_duration, summarize_fight, summarize_run


def ev(ts, actor="you", target="rat", amount=10, ability="melee", kind="damage"):
    return Event(ts, actor, target, amount, ability, kind, "r")


def fight(events, fid=1):
    act = [e.ts for e in events if e.kind in ("damage", "damage_taken")]
    return Fight(fid, min(act), max(act), list(events))


def test_effective_duration():
    assert effective_duration(0) == 1.0 and effective_duration(0.5) == 1.0 and effective_duration(4) == 4


def test_zero_duration_fight_dps_uses_one_second():
    s = summarize_fight(fight([ev(5, amount=30)]))
    assert s.duration == 0 and s.you.dps == 30.0 and s.combined_dps == 30.0


def test_sub_second_fight_uses_one_second():
    s = summarize_fight(fight([ev(5, amount=10), ev(5.5, amount=10)]))
    assert s.combined_dps == 20.0


def test_you_and_pet_same_second_combined_and_group_still_computed():
    s = summarize_fight(fight([ev(0, "you", amount=10), ev(0, "pet", amount=7), ev(4, "Bob", amount=8)]))
    assert s.duration == 4
    assert s.combined_total == 17 and s.combined_dps == 17 / 4
    assert s.actor("Bob").total == 8 and s.actor("Bob").dps == 2.0
    assert list(s.actors) == ["you", "pet", "Bob"]
    assert s.actor("nobody").total == 0


def test_only_damage_counts_taken_separate_and_misses_ignored():
    s = summarize_fight(fight([
        ev(0, amount=10), ev(1, "rat", "you", 4, kind="damage_taken"),
        ev(2, amount=0, kind="miss"), ev(3, amount=0, kind="death"),
    ]))
    assert s.combined_total == 10 and s.damage_taken == 4 and "rat" not in s.actors
    assert s.you.hits == 1


def test_hits_max_and_ability_key_unknown():
    s = summarize_fight(fight([ev(0, amount=5, ability=None), ev(1, amount=9, ability=None), ev(2, amount=1)]))
    ab = s.you.abilities["(unknown)"]
    assert (ab.total, ab.hits, ab.max_hit) == (14, 2, 9)
    assert (s.you.total, s.you.hits, s.you.max_hit) == (15, 3, 9)


def test_top_abilities_limit_three_and_order_with_ties():
    events = [
        ev(0, "pet", ability="Bite", amount=20),
        ev(0, "you", ability="Zap", amount=20),
        ev(0, "you", ability="Alpha", amount=20),
        ev(0, "you", ability="Big", amount=50),
        ev(0, "you", ability="Small", amount=1),
        ev(0, "Bob", ability="Huge", amount=999),
    ]
    s = summarize_fight(fight(events))
    assert s.top_abilities == [
        {"actor": "you", "ability": "Big", "total": 50},
        {"actor": "you", "ability": "Alpha", "total": 20},
        {"actor": "you", "ability": "Zap", "total": 20},
    ]
    events2 = [ev(0, "pet", ability="Zap", amount=20), ev(0, "you", ability="Zap", amount=20)]
    assert [r["actor"] for r in summarize_fight(fight(events2)).top_abilities] == ["you", "pet"]


def test_primary_target_most_damage_and_tie_first_hit():
    s = summarize_fight(fight([ev(0, target="a", amount=5), ev(1, target="b", amount=50), ev(2, target="a", amount=10)]))
    assert s.target == "b" and s.targets == ["a", "b"]
    tie = summarize_fight(fight([ev(0, target="a", amount=5), ev(1, target="b", amount=10), ev(2, target="a", amount=5)]))
    assert tie.target == "a"


def test_primary_target_counts_all_actors():
    s = summarize_fight(fight([ev(0, "you", target="a", amount=10), ev(1, "Bob", target="b", amount=30)]))
    assert s.target == "b"


def test_no_damage_events_target_none():
    s = summarize_fight(fight([ev(0, "rat", "you", 3, kind="damage_taken")]))
    assert s.target is None and s.targets == [] and s.combined_total == 0 and s.top_abilities == []


def test_ongoing_flag():
    f = fight([ev(0)])
    assert summarize_fight(f).ongoing is False and summarize_fight(f, ongoing=True).ongoing is True


def test_empty_run_summary():
    r = summarize_run([])
    assert (r.fight_count, r.total_damage, r.average_dps) == (0, 0, 0.0)
    assert (r.best_fight_id, r.best_fight_target, r.best_fight_dps) == (None, None, 0.0)


def test_run_summary_average_and_best_with_ties():
    f1 = summarize_fight(fight([ev(0, amount=10), ev(10, amount=10)], 1))   # 20 / 10 = 2
    f2 = summarize_fight(fight([ev(100, amount=30, target="boss")], 2))      # 30 / 1 = 30
    f3 = summarize_fight(fight([ev(200, amount=30, target="other")], 3))     # ties f2 -> f2 wins
    r = summarize_run([f1, f2, f3])
    assert r.fight_count == 3 and r.total_damage == 80
    assert r.average_dps == pytest.approx(80 / 12)
    assert (r.best_fight_id, r.best_fight_target) == (2, "boss") and r.best_fight_dps == 30


def test_summary_round_trips():
    s = summarize_fight(fight([ev(0), ev(2, "pet", ability="Bite")]))
    assert FightSummary.from_dict(s.to_dict()) == s
