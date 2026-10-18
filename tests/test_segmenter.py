"""Segmenter tests (session B)."""
from __future__ import annotations

from eqoa_parser.events import Event
from eqoa_parser.segmenter import Segmenter


def ev(ts, kind="damage", actor="you", target="rat", amount=1):
    return Event(ts, actor, target, amount, "melee", kind, "raw")


def test_first_activity_starts_fight_ids_sequential():
    s = Segmenter(8.0)
    assert s.current is None
    assert s.feed(ev(100)) == []
    assert s.current.fight_id == 1 and s.current.start_ts == s.current.end_ts == 100
    closed = s.feed(ev(200))
    assert [f.fight_id for f in closed] == [1] and s.current.fight_id == 2
    s2 = Segmenter(8.0, first_fight_id=5)
    s2.feed(ev(1))
    assert s2.current.fight_id == 5


def test_gap_exactly_idle_timeout_starts_new_fight():
    s = Segmenter(8.0)
    s.feed(ev(0))
    closed = s.feed(ev(8.0))
    assert len(closed) == 1 and closed[0].end_ts == 0 and s.current.start_ts == 8.0


def test_gap_just_under_idle_timeout_extends():
    s = Segmenter(8.0)
    s.feed(ev(0))
    assert s.feed(ev(7.999)) == []
    assert s.current.end_ts == 7.999 and len(s.current.events) == 2


def test_damage_taken_starts_and_extends():
    s = Segmenter(8.0)
    s.feed(ev(0, "damage_taken", actor="rat", target="you"))
    s.feed(ev(5, "damage"))
    assert s.current.end_ts == 5 and s.current.duration == 5


def test_misses_and_deaths_never_start_fights():
    s = Segmenter(8.0)
    assert s.feed(ev(1, "miss")) == [] and s.current is None
    assert s.feed(ev(2, "death")) == [] and s.current is None


def test_misses_do_not_extend_fights():
    s = Segmenter(8.0)
    s.feed(ev(0))
    s.feed(ev(5, "miss"))
    s.feed(ev(7, "death"))
    assert s.current.end_ts == 0 and len(s.current.events) == 3
    closed = s.feed(ev(8.5))  # 8.5 - 0 >= 8 even though a miss at 5 / death at 7 happened
    assert len(closed) == 1 and closed[0].end_ts == 0 and len(closed[0].events) == 3


def test_late_miss_closes_fight_and_is_dropped():
    s = Segmenter(8.0)
    s.feed(ev(0))
    closed = s.feed(ev(9, "miss"))
    assert len(closed) == 1 and s.current is None
    assert all(e.kind == "damage" for e in closed[0].events)


def test_tick_closes_only_at_threshold_and_is_event_time():
    s = Segmenter(8.0)
    assert s.tick(100) == []
    s.feed(ev(10))
    assert s.tick(17.9) == [] and s.current is not None
    closed = s.tick(18.0)
    assert len(closed) == 1 and s.current is None
    assert s.tick(500) == []


def test_flush_closes_and_empty_flush():
    s = Segmenter()
    assert s.flush() == []
    s.feed(ev(1))
    assert [f.fight_id for f in s.flush()] == [1]
    assert s.current is None and s.flush() == []


def test_out_of_order_ts_keeps_max_end_and_does_not_close():
    s = Segmenter(8.0)
    s.feed(ev(10))
    s.feed(ev(15))
    assert s.feed(ev(12)) == []
    assert s.current.end_ts == 15 and s.current.start_ts == 10 and s.current.duration == 5
    assert s.tick(14) == []  # clock behind end_ts: nothing to close


def test_overlapping_targets_are_one_fight():
    s = Segmenter(8.0)
    s.feed(ev(0, target="a"))
    s.feed(ev(3, target="b"))
    s.feed(ev(6, target="a"))
    assert s.current.fight_id == 1 and len(s.current.events) == 3


def test_events_in_feed_order():
    s = Segmenter()
    es = [ev(0), ev(1, "miss"), ev(2)]
    for e in es:
        s.feed(e)
    assert s.current.events == es
