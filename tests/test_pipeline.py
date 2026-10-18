"""Engine tests (session B)."""
from __future__ import annotations

import threading
from pathlib import Path

from eqoa_parser.config import load_config
from eqoa_parser.pipeline import Engine
from eqoa_parser.sources import HEARTBEAT

ROOT = Path(__file__).resolve().parent.parent


class ListSource:
    def __init__(self, items):
        self.items = list(items)
        self.closed = 0

    def __iter__(self):
        return iter(self.items)

    def close(self):
        self.closed += 1


def cfg(tmp_path, **kw):
    return load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"), **kw)


def hit(ts, n=10):
    return (ts, f"You hit a rat for {n} points of damage.")


def test_live_fires_per_event_and_closed_on_timeout_via_feed(tmp_path):
    live, closed = [], []
    src = ListSource([hit(0), (1, "You try to hit a rat, but miss!"), hit(2), hit(20)])
    e = Engine(cfg(tmp_path), src, on_live=live.append, on_closed=closed.append)
    e.run()
    assert [s.combined_total for s in live] == [10, 10, 20, 10]  # miss attached -> live fires too
    assert all(s.ongoing for s in live)
    assert [s.fight_id for s in closed] == [1, 2] and all(not s.ongoing for s in closed)
    assert src.closed >= 1


def test_closed_fires_before_live_of_the_event_that_closed_it(tmp_path):
    order = []
    src = ListSource([hit(0), hit(20)])
    Engine(cfg(tmp_path), src, on_live=lambda s: order.append(("live", s.fight_id)),
           on_closed=lambda s: order.append(("closed", s.fight_id))).run()
    assert order == [("live", 1), ("closed", 1), ("live", 2), ("closed", 2)]


def test_no_live_for_ignored_unmatched_or_orphan_events(tmp_path):
    live = []
    src = ListSource([(0, "You gain experience!"), (1, "garbage"), (2, "You try to hit a rat, but miss!")])
    e = Engine(cfg(tmp_path), src, on_live=live.append)
    e.run()
    assert live == [] and e.closed_fights == [] and e.parser.unmatched_count == 1


def test_heartbeat_driven_close_without_new_events(tmp_path):
    closed = []
    e = Engine(cfg(tmp_path), ListSource([]), on_closed=closed.append)
    e.step(*hit(0))
    e.step(7.9, HEARTBEAT)
    assert closed == [] and e.current_summary() is not None
    e.step(8.0, HEARTBEAT)
    assert len(closed) == 1 and e.current_summary() is None
    assert closed[0].end_ts == 0


def test_end_of_stream_flush_and_finished_once(tmp_path):
    closed, fin = [], []
    e = Engine(cfg(tmp_path), ListSource([hit(0), hit(1)]), on_closed=closed.append, on_finished=lambda: fin.append(1))
    e.run()
    assert len(closed) == 1 and fin == [1]
    e.finish()
    e.finish()
    assert len(closed) == 1 and fin == [1]
    assert len(e.closed_fights) == 1 and e.run_summary().fight_count == 1


def test_run_summary_covers_closed_only_and_current_summary(tmp_path):
    e = Engine(cfg(tmp_path), ListSource([]))
    e.step(*hit(0))
    assert e.run_summary().fight_count == 0
    cur = e.current_summary()
    assert cur.ongoing and cur.combined_total == 10
    e.step(*hit(50))
    assert e.run_summary().fight_count == 1


def test_stop_event_stops_and_closes_source_and_flushes(tmp_path):
    stop = threading.Event()
    closed, fin = [], []

    def gen():
        for i in range(1000):
            yield hit(i * 0.1)
            if i == 3:
                stop.set()

    class Src(ListSource):
        def __iter__(self):
            return gen()

    src = Src([])
    e = Engine(cfg(tmp_path), src, on_closed=closed.append, on_finished=lambda: fin.append(1))
    e.run(stop)
    assert src.closed >= 1 and fin == [1] and len(closed) == 1
    assert closed[0].you.hits == 4  # stopped promptly: no further events consumed


def test_stop_set_before_run(tmp_path):
    stop = threading.Event()
    stop.set()
    src = ListSource([hit(0)])
    e = Engine(cfg(tmp_path), src)
    e.run(stop)
    assert e.closed_fights == [] and src.closed >= 1


def test_callbacks_on_run_thread(tmp_path):
    seen = set()
    e = Engine(cfg(tmp_path), ListSource([hit(0), hit(30)]),
               on_live=lambda s: seen.add(threading.get_ident()),
               on_closed=lambda s: seen.add(threading.get_ident()),
               on_finished=lambda: seen.add(threading.get_ident()))
    t = threading.Thread(target=e.run)
    t.start()
    t.join()
    assert seen == {t.ident}


def test_uses_config_names_and_idle_timeout(tmp_path):
    c = cfg(tmp_path, idle_timeout=2.0, pet_names=["Fluffy"], player_name="Zorg")
    e = Engine(c, ListSource([(0, "Zorg hits a rat for 5 points of damage."), (1, "Fluffy hits a rat for 6 points of damage."),
                              (3.0, "You hit a rat for 1 point of damage.")]))
    e.run()
    f = e.closed_fights
    assert len(f) == 2
    assert f[0].you.total == 5 and f[0].pet.total == 6


def test_custom_parser_is_used_and_not_closed(tmp_path):
    from eqoa_parser.parser import Parser

    p = Parser(ROOT / "patterns.yaml")
    e = Engine(cfg(tmp_path), ListSource([hit(0)]), parser=p)
    e.run()
    assert e.parser is p and e.closed_fights[0].combined_total == 10


def test_unparseable_lines_advance_clock(tmp_path):
    closed = []
    e = Engine(cfg(tmp_path), ListSource([]), on_closed=closed.append)
    e.step(*hit(0))
    e.step(9, "some chat line")
    assert len(closed) == 1
