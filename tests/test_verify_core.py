"""Independent verification (session D): adversarial tests for parser / segmenter / stats / pipeline / replay /
persistence / CLI. Written without access to the implementers' reasoning; expectations come from docs/CONTRACTS.md.

Tests whose docstring starts with "BUG-n" assert the behaviour the contract (or plain robustness) requires and are
EXPECTED TO FAIL against the current code: each failure is a real finding listed in docs/VERIFICATION.md.
They are deliberately not marked xfail.  Everything here uses SYNTHETIC data.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eqoa_parser import persistence
from eqoa_parser.config import load_config
from eqoa_parser.events import Event
from eqoa_parser.parser import Parser
from eqoa_parser.persistence import RunStore
from eqoa_parser.pipeline import Engine
from eqoa_parser.segmenter import Segmenter
from eqoa_parser.sources import HEARTBEAT, ReplaySource
from eqoa_parser.stats import FightSummary, summarize_fight, summarize_run

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "synthetic_combat.log"
PATTERNS = ROOT / "patterns.yaml"
BASE = 1000.0


# ---------------------------------------------------------------------------------------------------- helpers
class ListSource:
    """In-memory LogSource."""

    def __init__(self, items):
        self.items = list(items)
        self.closed = 0

    def __iter__(self):
        yield from self.items

    def close(self):
        self.closed += 1


def cfg_for(tmp_path, **kw):
    return load_config(unmatched_log=str(tmp_path / "unmatched.log"), runs_dir=str(tmp_path / "runs"), **kw)


def run_engine(tmp_path, items, **kw):
    closed, live, finished = [], [], []
    eng = Engine(cfg_for(tmp_path, **kw), ListSource(items), on_closed=closed.append, on_live=live.append,
                 on_finished=lambda: finished.append(1))
    eng.run()
    return eng, closed, live, finished


def you(ts, amount, target="a rat", how="hit"):
    return ts, f"You {how} {target} for {amount} points of damage."


def pet(ts, amount, target="a rat"):
    return ts, f"Your pet bites {target} for {amount} points of damage."


def ev(ts, kind="damage", actor="you", target="a rat", amount=10, ability="melee"):
    return Event(ts, actor, target, amount, ability, kind, "x")


def write_replay(path, lines, header=True):
    body = "".join(f"{ts}\t{raw}\n" for ts, raw in lines)
    path.write_text(body, encoding="utf-8")
    return path


# ============================================================================== segmenter / overlapping fights
def test_two_mobs_interleaved_are_one_fight(tmp_path):
    items = [you(BASE + i, 10, "a rat") if i % 2 == 0 else you(BASE + i, 20, "a bat") for i in range(10)]
    _, closed, _, _ = run_engine(tmp_path, items)
    assert len(closed) == 1
    f = closed[0]
    assert f.targets == ["a rat", "a bat"]            # first-hit order
    assert f.you.total == 5 * 10 + 5 * 20 and f.you.hits == 10
    assert f.target == "a bat"                        # 100 vs 50 received


def test_primary_target_tie_goes_to_earliest_first_hit(tmp_path):
    _, closed, _, _ = run_engine(tmp_path, [you(BASE, 10, "a bat"), you(BASE + 1, 10, "a rat")])
    assert closed[0].target == "a bat"


@pytest.mark.parametrize("gap,fights", [(7.99, 1), (8.0, 2), (8.01, 2), (7.999999, 1)])
def test_second_mob_pulled_around_the_idle_timeout(tmp_path, gap, fights):
    items = [you(BASE, 10, "a rat"), you(BASE + gap, 10, "a bat")]
    _, closed, _, _ = run_engine(tmp_path, items)
    assert len(closed) == fights
    if fights == 2:
        assert [f.target for f in closed] == ["a rat", "a bat"]
        assert [f.fight_id for f in closed] == [1, 2]
    else:
        assert closed[0].targets == ["a rat", "a bat"] and closed[0].duration == pytest.approx(gap)


def test_idle_clock_runs_from_last_damage_of_any_mob(tmp_path):
    # rat hit at 0..5, bat first pulled at 12.9 (7.9 after the LAST rat hit) -> joins; bat again 7.9 later -> still joins.
    items = [you(BASE + t, 5, "a rat") for t in range(6)] + [you(BASE + 12.9, 5, "a bat"), you(BASE + 20.8, 5, "a bat")]
    _, closed, _, _ = run_engine(tmp_path, items)
    assert len(closed) == 1 and closed[0].targets == ["a rat", "a bat"]


def test_miss_and_death_neither_start_nor_extend_a_fight(tmp_path):
    items = [you(BASE, 10),
             (BASE + 7.9, "You try to hit a rat, but miss!"),
             (BASE + 8.0, "You have slain a rat!"),                    # 8.0 after last damage: closes fight 1, dropped
             (BASE + 8.5, "You try to hit a rat, but miss!"),         # no fight: dropped
             you(BASE + 9.0, 10)]
    eng, closed, _, _ = run_engine(tmp_path, items)
    assert [f.fight_id for f in closed] == [1, 2]
    assert closed[0].end_ts == BASE and closed[1].start_ts == BASE + 9.0
    # the slain event arrived exactly at the timeout, so it must not be attached to fight 1
    seg = Segmenter(8.0)
    seg.feed(ev(BASE))
    assert seg.feed(ev(BASE + 8.0, kind="death", amount=0, ability=None))[0].events[-1].kind == "damage"


def test_heartbeat_closes_fight_exactly_at_timeout_not_before(tmp_path):
    seg = Segmenter(8.0)
    seg.feed(ev(BASE))
    assert seg.tick(BASE + 7.999) == [] and seg.current is not None
    (f,) = seg.tick(BASE + 8.0)
    assert f.fight_id == 1 and seg.current is None


def test_callback_order_close_then_live_when_event_starts_new_fight(tmp_path):
    order = []
    eng = Engine(cfg_for(tmp_path), ListSource([you(BASE, 10), you(BASE + 20, 10)]),
                 on_closed=lambda s: order.append(("closed", s.fight_id)),
                 on_live=lambda s: order.append(("live", s.fight_id, s.ongoing)),
                 on_finished=lambda: order.append(("finished",)))
    eng.run()
    assert order == [("live", 1, True), ("closed", 1), ("live", 2, True), ("closed", 2), ("finished",)]


def test_unmatched_and_ignored_lines_do_not_produce_live_or_events(tmp_path):
    eng, closed, live, fin = run_engine(tmp_path, [(BASE, "You gain experience!"), (BASE + 1, "garbage"), (BASE + 2, "")])
    assert closed == [] and live == [] and fin == [1] and eng.parser.unmatched_count == 1


# ============================================================================== zero-duration / stats
def test_single_hit_fight_is_zero_duration_with_min_one_second_dps(tmp_path):
    _, closed, _, _ = run_engine(tmp_path, [you(BASE, 37)])
    (f,) = closed
    assert f.duration == 0.0 and f.you.dps == 37.0 and f.combined_dps == 37.0 and f.you.hits == 1


def test_many_hits_same_timestamp_are_zero_duration(tmp_path):
    _, closed, _, _ = run_engine(tmp_path, [you(BASE, 10), you(BASE, 20), pet(BASE, 5), you(BASE, 1)])
    (f,) = closed
    assert f.duration == 0.0 and f.combined_total == 36 and f.combined_dps == 36.0
    assert f.you.hits == 3 and f.pet.hits == 1


def test_sub_second_fight_uses_one_second_floor(tmp_path):
    _, closed, _, _ = run_engine(tmp_path, [you(BASE, 10), you(BASE + 0.5, 10)])
    assert closed[0].duration == 0.5 and closed[0].you.dps == pytest.approx(20.0)  # 20 / max(0.5, 1.0)


def test_run_summary_with_only_zero_duration_fights(tmp_path):
    items = [you(BASE, 10), you(BASE + 20, 30), you(BASE + 40, 20)]
    eng, closed, _, _ = run_engine(tmp_path, items)
    r = eng.run_summary()
    assert r.fight_count == 3 and r.total_damage == 60
    assert r.average_dps == pytest.approx(60 / 3.0)            # sum of effective durations = 3 * 1.0
    assert (r.best_fight_id, r.best_fight_dps) == (2, 30.0)


def test_run_summary_best_fight_tie_goes_to_earliest(tmp_path):
    eng, _, _, _ = run_engine(tmp_path, [you(BASE, 10), you(BASE + 20, 10)])
    assert eng.run_summary().best_fight_id == 1


def test_empty_run_summary():
    r = summarize_run([])
    assert (r.fight_count, r.total_damage, r.average_dps, r.best_fight_id, r.best_fight_target, r.best_fight_dps) == (0, 0, 0.0, None, None, 0.0)


def test_damage_taken_only_fight(tmp_path):
    items = [(BASE, "a rat hits you for 5 points of damage."), (BASE + 3, "a rat hits your pet for 7 points of damage.")]
    eng, closed, _, _ = run_engine(tmp_path, items)
    (f,) = closed
    assert f.target is None and f.targets == [] and f.damage_taken == 12 and f.combined_total == 0 and f.combined_dps == 0.0
    assert f.top_abilities == [] and f.duration == 3.0
    r = eng.run_summary()
    assert (r.fight_count, r.total_damage, r.average_dps, r.best_fight_id, r.best_fight_target) == (1, 0, 0.0, 1, None)
    assert FightSummary.from_dict(json.loads(json.dumps(f.to_dict()))) == f


def test_zero_amount_hit_still_counts_as_activity(tmp_path):
    _, closed, _, _ = run_engine(tmp_path, [you(BASE, 0)])
    (f,) = closed
    assert f.you.hits == 1 and f.you.total == 0 and f.you.max_hit == 0 and f.combined_dps == 0.0


def test_amount_one_and_pluralisation(tmp_path):
    items = [(BASE, "You hit a rat for 1 point of damage."), (BASE + 1, "You hit a rat for 2 points of damage.")]
    _, closed, _, _ = run_engine(tmp_path, items)
    assert closed[0].you.total == 3


def test_nine_digit_amount_is_counted(tmp_path):
    amt = 999_999_999
    _, closed, _, _ = run_engine(tmp_path, [you(BASE, amt)])
    assert closed[0].you.total == amt
    json.dumps(closed[0].to_dict())


# Amended by main chat after the contract amendment (docs/CONTRACTS.md, "Amendments after verification"):
# amounts longer than 9 digits are garbage from a misread buffer -> unmatched, not counted, no crash.
@pytest.mark.parametrize("digits", [10, 18, 100, 300])
def test_over_nine_digit_amounts_are_unmatched_not_counted(tmp_path, digits):
    eng, closed, _, _ = run_engine(tmp_path, [you(BASE, 10 ** (digits - 1))])
    assert closed == []
    assert eng.parser.unmatched_count == 1
    assert len((tmp_path / "unmatched.log").read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("digits", [400, 4301, 5000])
def test_absurd_amounts_do_not_crash_the_engine(tmp_path, digits):
    """BUG-4 (low): int() digit limit (4300) -> ValueError out of Parser.parse; >308 digits -> OverflowError in stats
    (int/float). A garbled/decoded line like 'for 99999...9 points of damage.' kills Engine.run()."""
    items = [you(BASE, 10), (BASE + 1, "You hit a rat for " + "9" * digits + " points of damage."), you(BASE + 2, 10)]
    run_engine(tmp_path, items)


# ============================================================================== pet + player same time / ordering
@pytest.mark.parametrize("order", ["you_first", "pet_first"])
@pytest.mark.parametrize("same_ts", [True, False])
def test_pet_and_player_in_same_second_any_order(tmp_path, order, same_ts):
    t_pet = BASE if same_ts else BASE + 0.4
    a, b = you(BASE, 10), pet(t_pet, 10)
    items = [a, b] if order == "you_first" else [b, a]
    items.sort(key=lambda x: x[0])                      # replay protocol: non-decreasing ts
    _, closed, _, _ = run_engine(tmp_path, items)
    (f,) = closed
    assert (f.you.total, f.pet.total, f.combined_total) == (10, 10, 20)
    assert [(t["actor"], t["ability"]) for t in f.top_abilities] == [("you", "melee"), ("pet", "melee")]  # tie: you first


def test_top_abilities_tiebreak_actor_then_name_and_limit_three():
    seg = Segmenter(8.0)
    for i, (actor, ability, amt) in enumerate([("pet", "Zap", 10), ("you", "Zap", 10), ("you", "Alpha", 10), ("pet", "Alpha", 10), ("you", "Big", 99)]):
        seg.feed(ev(BASE + i, actor=actor, ability=ability, amount=amt))
    (f,) = seg.flush()
    top = [(t["actor"], t["ability"], t["total"]) for t in summarize_fight(f).top_abilities]
    assert top == [("you", "Big", 99), ("you", "Alpha", 10), ("you", "Zap", 10)]


def test_none_ability_key_is_unknown():
    seg = Segmenter(8.0)
    seg.feed(ev(BASE, ability=None))
    s = summarize_fight(seg.flush()[0])
    assert "(unknown)" in s.you.abilities and s.top_abilities[0]["ability"] == "(unknown)"


def test_group_actor_damage_never_counts_toward_combined(tmp_path):
    _, closed, _, _ = run_engine(tmp_path, [you(BASE, 10), (BASE + 1, "Bob hits a rat for 500 points of damage.")])
    f = closed[0]
    assert f.combined_total == 10 and f.actor("Bob").total == 500 and f.target == "a rat"


# ============================================================================== out-of-order timestamps
def test_out_of_order_timestamps_do_not_crash_or_go_negative(tmp_path):
    items = [you(BASE + 10, 5), you(BASE + 5, 5), you(BASE + 30, 5), you(BASE + 2, 5), (BASE + 1, ""), you(BASE + 40, 5)]
    eng, closed, _, _ = run_engine(tmp_path, items)
    assert sum(f.combined_total for f in closed) == 25
    assert all(f.duration >= 0 and f.you.dps >= 0 for f in closed)


def test_replaysource_out_of_order_line_is_flagged_or_clamped(tmp_path, capsys):
    """BUG-6 (low): LogSource protocol requires non-decreasing ts; ReplaySource passes a regressing ts straight through
    and says nothing, so Segmenter attaches the late event to the wrong fight (see VERIFICATION.md)."""
    p = write_replay(tmp_path / "r.log", [you(BASE, 5), you(BASE + 8, 5), you(BASE + 1, 5)])
    got = list(ReplaySource(p, speed=0))
    err = capsys.readouterr().err
    ts = [t for t, _ in got]
    assert ts == sorted(ts) or "order" in err.lower() or "regress" in err.lower() or "decreas" in err.lower()


# ============================================================================== parser: unmatched lines / odd input
def parser_with_log(tmp_path):
    u = tmp_path / "u.log"
    return Parser(PATTERNS, unmatched_path=u), u


NOT_LOGGED = ["", " ", "   \t  ", "\t", HEARTBEAT]
UNMATCHED_LINES = [
    "Bob tells you, 'nice pull'",
    "You hit a rat for 5 points of damage",            # missing period
    "You hit a rat for 5 points of damage. (crit)",    # trailing junk
    "you hit a rat for 5 points of damage.",           # wrong case of verb subject
    "You hit a rat 5 points of damage.",               # missing 'for'
    "You hit a rat for five points of damage.",
    "You hit a rat for 1,234 points of damage.",
    "You hit a rat for -5 points of damage.",
    "A rat hits Fido for 5 points of damage.",
    "x" * 5000,
    "tab\tinside\tline",
    "nul\x00inside",
    "emoji \U0001f600 and kana テスト and üñî",
    "Z̸̡algo",
    "﻿BOM at start of text",
    "carriage\rreturn",
    "new\nline",
    "windows\r\nline",
]


@pytest.mark.parametrize("raw", NOT_LOGGED)
def test_blank_and_heartbeat_lines_are_not_logged(tmp_path, raw):
    p, u = parser_with_log(tmp_path)
    assert p.parse(1.0, raw) is None
    p.close()
    assert p.unmatched_count == 0 and (not u.exists() or u.read_bytes() == b"")


@pytest.mark.parametrize("raw", UNMATCHED_LINES)
def test_unmatched_line_is_logged_once_on_one_physical_line(tmp_path, raw):
    p, u = parser_with_log(tmp_path)
    assert p.parse(12.5, raw) is None
    assert p.unmatched_count == 1
    data = u.read_bytes()                                  # flushed immediately, before close()
    assert data.count(b"\n") == 1 and data.endswith(b"\n") and data.count(b"\r") == 0
    ts, _, rest = data.decode("utf-8").rstrip("\n").partition("\t")
    assert float(ts) == 12.5
    assert "\t" not in rest.replace("\\t", "")             # no raw tabs after the ts separator
    p.close()


def test_unmatched_log_is_one_entry_per_line_for_a_batch(tmp_path):
    p, u = parser_with_log(tmp_path)
    for i, raw in enumerate(UNMATCHED_LINES):
        p.parse(float(i), raw)
    p.close()
    assert u.read_bytes().count(b"\n") == len(UNMATCHED_LINES) == p.unmatched_count
    assert len(u.read_text(encoding="utf-8").split("\n")) == len(UNMATCHED_LINES) + 1


def test_unmatched_log_is_appended_not_truncated(tmp_path):
    u = tmp_path / "u.log"
    for _ in range(2):
        p = Parser(PATTERNS, unmatched_path=u)
        p.parse(1.0, "junk")
        p.close()
    assert u.read_text(encoding="utf-8").count("\n") == 2


@pytest.mark.parametrize("sep", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "])
def test_unmatched_entries_survive_str_splitlines(tmp_path, sep):
    """BUG-5 (low): only \\r \\n \\t are escaped, so other Unicode line boundaries (which decoded garbage can contain,
    e.g. U+2028 in utf-16 noise) split an entry when the log is read with str.splitlines() -- the very method
    tests/test_contracts.py uses to count entries."""
    p, u = parser_with_log(tmp_path)
    p.parse(1.0, f"junk{sep}more junk")
    p.close()
    assert len(u.read_text(encoding="utf-8").splitlines()) == 1


def test_lone_surrogate_in_unmatched_line_does_not_crash(tmp_path):
    """BUG-3 (low): raw containing a lone surrogate -> UnicodeEncodeError from the unmatched.log write."""
    p, u = parser_with_log(tmp_path)
    p.parse(1.0, "garbled \ud800 text")
    p.close()


def test_near_miss_lines_never_produce_events(tmp_path):
    p, _ = parser_with_log(tmp_path)
    for raw in UNMATCHED_LINES:
        assert p.parse(1.0, raw) is None
    p.close()


def test_event_raw_is_untouched_but_match_ignores_surrounding_whitespace(tmp_path):
    p, _ = parser_with_log(tmp_path)
    raw = "  \tYou hit a rat for 5 points of damage.  \t"
    e = p.parse(3.0, raw)
    p.close()
    assert e is not None and e.raw == raw and e.amount == 5 and e.target == "a rat" and e.ts == 3.0


@pytest.mark.parametrize("raw,actor,target", [
    ("You hit A Rat for 5 points of damage.", "you", "a Rat"),
    ("You hit The Bone King for 5 points of damage.", "you", "the Bone King"),
    ("You hit AN owlbear for 5 points of damage.", "you", "an owlbear"),
    ("You hit yourself for 5 points of damage.", "you", "you"),
    ("Your pet bites YOURSELF for 5 points of damage.", "pet", "you"),
    ("Fido hits a rat for 5 points of damage.", "pet", "a rat"),
    ("Aragorn hits a rat for 5 points of damage.", "you", "a rat"),
    ("A rat hits you for 5 points of damage.", "a rat", "you"),
    ("Bob's Flame Lick hits a rat for 5 points of damage.", "Bob", "a rat"),
])
def test_actor_and_target_normalisation(tmp_path, raw, actor, target):
    p = Parser(PATTERNS, unmatched_path=tmp_path / "u.log", pet_names=["fido"], player_name="ARAGORN")
    e = p.parse(1.0, raw)
    p.close()
    assert e is not None and (e.actor, e.target) == (actor, target)


# ============================================================================== replay source / duplicates
def test_identical_replay_lines_are_distinct_messages(tmp_path):
    line = "You hit a rat for 7 points of damage."
    p = write_replay(tmp_path / "r.log", [(BASE, line)] * 3 + [(BASE + 1, line)])
    got = list(ReplaySource(p, speed=0))
    assert len(got) == 4
    eng, closed, _, _ = run_engine(tmp_path, got)
    assert closed[0].you.hits == 4 and closed[0].you.total == 28


def test_replay_file_format_edge_cases(tmp_path, capsys):
    p = tmp_path / "r.log"
    p.write_bytes(
        b"\xef\xbb\xbf# bom + comment\n"
        b"\n"
        b"   \n"
        b"1000.0\tYou hit a rat for 5 points of damage.\r\n"
        b"no tab here\n"
        b"abc\tbad ts\n"
        b"nan\tnan ts\n"
        b"inf\tinf ts\n"
        b"1001.0\t\n"                                       # empty text == HEARTBEAT value
        b"  # indented comment-like line\n"
        b"1002.0\tbad utf8 \xff\xfe here\n"
        b"1003.5\tlast line no newline"
    )
    got = list(ReplaySource(p))
    err = capsys.readouterr().err
    assert [t for t, _ in got] == [1000.0, 1001.0, 1002.0, 1003.5]
    assert got[0][1] == "You hit a rat for 5 points of damage."          # CRLF stripped
    assert got[1][1] == HEARTBEAT and "�" in got[2][1] and got[3][1] == "last line no newline"
    # malformed: "no tab here", "abc", "nan", "inf" and the indented '#' line (no tab) -> 5 warnings
    assert err.count("skipped malformed line") == 5


def test_empty_and_comment_only_replay_files(tmp_path):
    for body in ("", "# only a comment\n", "\n\n   \n"):
        p = tmp_path / "e.log"
        p.write_text(body, encoding="utf-8")
        eng, closed, live, fin = run_engine(tmp_path, list(ReplaySource(p)))
        assert closed == [] and fin == [1] and eng.run_summary().fight_count == 0


def test_replay_missing_file_raises_oserror_on_iteration(tmp_path):
    with pytest.raises(OSError):
        list(ReplaySource(tmp_path / "nope.log"))


def test_replay_speed_validation():
    with pytest.raises(ValueError):
        ReplaySource("x", speed=-1)
    with pytest.raises(ValueError):
        ReplaySource("x", heartbeat_interval=0)


def test_realtime_replay_yields_monotonic_heartbeats_and_closes_idle_fight_early(tmp_path):
    p = write_replay(tmp_path / "r.log", [you(BASE, 10), you(BASE + 30, 10)])
    src = ReplaySource(p, speed=20.0, heartbeat_interval=0.05)       # 30 file-seconds = 1.5 real seconds
    t0 = time.monotonic()
    closed_at = []
    items = []
    for ts, raw in src:
        items.append((ts, raw))
    hb = [(t, r) for t, r in items if r == HEARTBEAT]
    assert hb, "no heartbeats were produced while sleeping"
    ts_all = [t for t, _ in items]
    assert ts_all == sorted(ts_all) and all(BASE <= t <= BASE + 30 for t in ts_all)
    assert items[0] == (BASE, "You hit a rat for 10 points of damage.") and items[-1][0] == BASE + 30
    # drive an engine from a fresh real-time source and check fight 1 closes via heartbeat well before the 2nd line (~1.5s)
    src2 = ReplaySource(p, speed=20.0, heartbeat_interval=0.05)
    t0 = time.monotonic()
    closed_at.clear()
    eng = Engine(cfg_for(tmp_path), src2, on_closed=lambda s: closed_at.append((s.fight_id, time.monotonic() - t0)))
    eng.run()
    first = dict(closed_at)[1]
    assert 0.3 <= first <= 1.0, closed_at                                # 8 file-seconds / 20 = 0.4s real


def test_engine_stop_mid_replay_flushes_and_finishes(tmp_path):
    stop = threading.Event()
    closed, finished = [], []
    src = ListSource([you(BASE + i, 10) for i in range(10)])

    def on_live(s):
        if s.you.hits == 3:
            stop.set()

    eng = Engine(cfg_for(tmp_path), src, on_live=on_live, on_closed=closed.append, on_finished=lambda: finished.append(1))
    eng.run(stop)
    assert src.closed >= 1 and finished == [1]
    assert len(closed) == 1 and closed[0].you.hits == 3                   # the partial fight is flushed, nothing after stop
    eng.finish()
    assert finished == [1] and len(closed) == 1                           # idempotent


def test_engine_stop_preset_processes_nothing(tmp_path):
    stop = threading.Event()
    stop.set()
    eng, closed, finished = None, [], []
    src = ListSource([you(BASE, 10)])
    eng = Engine(cfg_for(tmp_path), src, on_closed=closed.append, on_finished=lambda: finished.append(1))
    eng.run(stop)
    assert closed == [] and finished == [1] and src.closed >= 1


def test_engine_stop_from_other_thread_interrupts_slow_replay(tmp_path):
    p = write_replay(tmp_path / "r.log", [you(BASE, 10), you(BASE + 5000, 10)])
    src = ReplaySource(p, speed=1.0)
    stop = threading.Event()
    done = []
    eng = Engine(cfg_for(tmp_path), src, on_finished=lambda: done.append(1))
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.6)
    stop.set()
    src.close()
    th.join(timeout=3)
    assert not th.is_alive() and done == [1]
    assert len(eng.closed_fights) == 1


def test_heartbeat_only_stream(tmp_path):
    items = [(BASE + i * 0.25, HEARTBEAT) for i in range(2000)]
    eng, closed, live, fin = run_engine(tmp_path, items)
    assert closed == [] and live == [] and fin == [1]
    assert eng.parser.unmatched_count == 0 and eng.current_summary() is None
    assert eng.run_summary().fight_count == 0
    assert not (tmp_path / "unmatched.log").read_bytes()


def test_heartbeats_drive_fight_close_and_never_open_one(tmp_path):
    items = [(BASE, HEARTBEAT), you(BASE + 1, 10), (BASE + 4, HEARTBEAT), (BASE + 8.99, HEARTBEAT), (BASE + 9.0, HEARTBEAT)]
    seen = []
    eng = Engine(cfg_for(tmp_path), ListSource(items), on_closed=lambda s: seen.append(s.fight_id))
    gen = iter(items)
    for ts, raw in items[:4]:
        eng.step(ts, raw)
    assert seen == [] and eng.current_summary() is not None
    eng.step(*items[4])
    assert seen == [1] and eng.current_summary() is None


# ============================================================================== persistence
def mk_summary(fid=1, total=10):
    seg = Segmenter(8.0, first_fight_id=fid)
    seg.feed(ev(BASE, amount=total))
    return summarize_fight(seg.flush()[0])


NOW = datetime(2026, 10, 17, 19, 0, 0, tzinfo=timezone(timedelta(hours=-7)))


def test_store_writes_documented_schema(tmp_path):
    st = RunStore(tmp_path, "replay:x.log", now=NOW)
    st.add_fight(mk_summary())
    doc = json.loads((tmp_path / "2026-10-17.json").read_text(encoding="utf-8"))
    assert set(doc) == {"schema_version", "date", "runs"} and doc["schema_version"] == 1 and doc["date"] == "2026-10-17"
    (run,) = doc["runs"]
    assert run["run_id"] == "20261017T190000" and run["started_at"] == "2026-10-17T19:00:00-07:00" and run["source"] == "replay:x.log"
    assert FightSummary.from_dict(run["fights"][0]) == mk_summary()
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_two_sequential_launches_append_and_reload(tmp_path):
    a = RunStore(tmp_path, "replay:a", now=NOW)
    a.add_fight(mk_summary(1, 10))
    a.add_fight(mk_summary(2, 20))
    b = RunStore(tmp_path, "replay:b", now=NOW)                 # same second -> run id must be made unique
    assert [pf.summary.fight_id for pf in b.previous_fights] == [1, 2] and b.previous_fights[0].run_id == a.run_id
    assert b.run_id != a.run_id
    b.add_fight(mk_summary(1, 5))
    doc = json.loads(b.path.read_text(encoding="utf-8"))
    assert [len(r["fights"]) for r in doc["runs"]] == [2, 1]
    c = RunStore(tmp_path, "replay:c", now=NOW + timedelta(hours=1))
    assert len(c.previous_fights) == 3


def test_run_with_no_fights_does_not_create_a_file(tmp_path):
    RunStore(tmp_path, "replay:none", now=NOW)
    assert not list(tmp_path.glob("*.json"))


def test_concurrent_launches_do_not_lose_each_others_runs(tmp_path):
    """BUG-2 (medium): every RunStore caches the whole day document at start and rewrites it wholesale, so a run
    started while another launch is open (GUI + a --no-gui replay, or two GUIs) is silently erased by whichever
    saves last. The earlier run's data is lost with no warning."""
    a = RunStore(tmp_path, "replay:gui-left-open", now=NOW)
    b = RunStore(tmp_path, "replay:second-launch", now=NOW + timedelta(minutes=5))
    b.add_fight(mk_summary(1, 99))
    a.add_fight(mk_summary(1, 11))                              # a was open first, saves later
    runs = json.loads(a.path.read_text(encoding="utf-8"))["runs"]
    assert {r["source"] for r in runs} == {"replay:gui-left-open", "replay:second-launch"}, \
        f"lost a run: {[r['source'] for r in runs]}"


@pytest.mark.parametrize("name,content", [
    ("garbage", b"\x00\x01 not json at all"),
    ("empty", b""),
    ("truncated", b'{"schema_version": 1, "date": "2026-10-17", "runs": [{"run_id": "x", "fights": [{"fight'),
    ("array", b"[]"),
    ("scalar", b"42"),
    ("future-schema", json.dumps({"schema_version": 2, "date": "d", "runs": []}).encode()),
    ("runs-not-list", json.dumps({"schema_version": 1, "date": "d", "runs": {}}).encode()),
    ("run-not-dict", json.dumps({"schema_version": 1, "date": "d", "runs": [1]}).encode()),
    ("bad-utf8", b'{"a": "\xff\xfe"}'),
    ("bom", b"\xef\xbb\xbf" + json.dumps({"schema_version": 1, "date": "d", "runs": []}).encode()),
])
def test_corrupt_runs_file_is_quarantined_not_overwritten(tmp_path, name, content):
    f = tmp_path / "2026-10-17.json"
    f.write_bytes(content)
    warns = []
    st = RunStore(tmp_path, "replay:x", now=NOW, warn=warns.append)
    assert st.corrupt_path is not None and st.corrupt_path.read_bytes() == content
    assert warns and "unreadable" in warns[0]
    assert st.previous_fights == []
    st.add_fight(mk_summary())
    assert len(json.loads(f.read_text(encoding="utf-8"))["runs"]) == 1


def test_bom_prefixed_runs_file_is_quarantined_cleanly(tmp_path):
    f = tmp_path / "2026-10-17.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"schema_version": 1, "date": "d", "runs": []}).encode())
    st = RunStore(tmp_path, "replay:x", now=NOW, warn=lambda m: None)
    assert st.corrupt_path is not None     # Windows editors add BOMs; acceptable to quarantine (data is preserved)


def test_two_corrupt_files_in_the_same_second_get_distinct_quarantine_names(tmp_path):
    f = tmp_path / "2026-10-17.json"
    names = set()
    for _ in range(3):
        f.write_bytes(b"junk")
        st = RunStore(tmp_path, "replay:x", now=NOW, warn=lambda m: None)
        names.add(st.corrupt_path.name)
    assert len(names) == 3


def test_runs_path_is_a_directory(tmp_path):
    (tmp_path / "2026-10-17.json").mkdir()
    warns = []
    st = RunStore(tmp_path, "replay:x", now=NOW, warn=warns.append)
    st.add_fight(mk_summary())                                   # must not crash; original directory untouched
    assert (tmp_path / "2026-10-17.json").is_dir() or st.path.exists()


def test_unreadable_fight_entries_are_kept_byte_for_byte(tmp_path):
    good = mk_summary().to_dict()
    odd = {"fight_id": 7, "mystery": [1, 2, {"x": None}]}
    doc = {"schema_version": 1, "date": "2026-10-17",
           "runs": [{"run_id": "old", "started_at": "t", "source": "s", "fights": [good, odd, 5, "str", None]}]}
    f = tmp_path / "2026-10-17.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    warns = []
    st = RunStore(tmp_path, "replay:x", now=NOW, warn=warns.append)
    assert len(st.previous_fights) == 1 and len(warns) == 4
    st.add_fight(mk_summary())
    saved = json.loads(f.read_text(encoding="utf-8"))
    assert saved["runs"][0]["fights"] == [good, odd, 5, "str", None] and len(saved["runs"]) == 2


def test_atomic_write_failure_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch):
    f = tmp_path / "x.json"
    persistence.atomic_write_json(f, {"a": 1})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(persistence.os, "replace", boom)
    with pytest.raises(OSError):
        persistence.atomic_write_json(f, {"a": 2})
    assert json.loads(f.read_text()) == {"a": 1} and [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_day_file_uses_local_start_date_across_midnight(tmp_path):
    late = datetime(2026, 10, 17, 23, 59, 59, tzinfo=timezone(timedelta(hours=-4)))
    st = RunStore(tmp_path, "replay:x", now=late)
    st.add_fight(mk_summary())
    assert st.path.name == "2026-10-17.json"


# ============================================================================== CLI end to end (subprocess)
def cli(tmp_path, *args, timeout=60, cfg_extra=""):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f'patterns_file: "{PATTERNS.as_posix()}"\nunmatched_log: "{(tmp_path / "u.log").as_posix()}"\n{cfg_extra}', encoding="utf-8")
    cmd = [sys.executable, "-m", "eqoa_parser", "--config", str(cfg), *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def test_cli_replay_twice_appends_and_matches_oracle(tmp_path):
    runs = tmp_path / "runs"
    outs = []
    for expected_prev in (0, 5):
        r = cli(tmp_path, "--replay", str(FIXTURE), "--no-gui", "--runs-dir", str(runs))
        assert r.returncode == 0 and r.stderr == ""
        assert f"{expected_prev} fight(s) from earlier runs today" in r.stdout
        outs.append(r.stdout.split("\n", 1)[1])
    assert outs[0] == outs[1]
    (f,) = runs.glob("*.json")
    doc = json.loads(f.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1 and re.fullmatch(r"\d{4}-\d\d-\d\d", doc["date"]) and f.name == doc["date"] + ".json"
    assert len(doc["runs"]) == 2 and doc["runs"][0]["run_id"] != doc["runs"][1]["run_id"]
    for run in doc["runs"]:
        assert re.fullmatch(r"\d{8}T\d{6}(-\d+)?", run["run_id"]) and run["source"].startswith("replay:")
        datetime.fromisoformat(run["started_at"])
        fights = [FightSummary.from_dict(x) for x in run["fights"]]
        assert [x.fight_id for x in fights] == [1, 2, 3, 4, 5]
        assert sum(x.combined_total for x in fights) == 274
    assert (tmp_path / "u.log").read_text(encoding="utf-8").count("\n") == 6          # 3 unmatched lines per launch


def test_cli_idle_timeout_changes_segmentation_as_reasoned(tmp_path):
    # fixture gaps between damage events: 8.0 (fight1->2), 16.0, 22.5, 10.0; largest intra-fight gap 2.0.
    def fights(timeout):
        r = cli(tmp_path, "--replay", str(FIXTURE), "--no-gui", "--runs-dir", str(tmp_path / f"r{timeout}"), "--idle-timeout", str(timeout))
        assert r.returncode == 0, r.stderr
        return int(re.search(r"Fights: (\d+)", r.stdout).group(1)), r.stdout
    assert fights(3)[0] == 5          # nothing in the fixture has a 2.0..8.0 gap inside a fight: same as default
    assert fights(8)[0] == 5
    assert fights(8.01)[0] == 4       # the exactly-8.0 gap now merges fights 1 and 2
    n20, out20 = fights(20)
    assert n20 == 2 and "Total damage: 274" in out20
    assert fights(1.9)[0] > 5         # shorter than the 2.0s intra-fight gaps -> fights split further


def test_cli_show_group_toggles_group_rows(tmp_path):
    base = ["--replay", str(FIXTURE), "--no-gui"]
    off = cli(tmp_path, *base, "--runs-dir", str(tmp_path / "a"))
    on = cli(tmp_path, *base, "--runs-dir", str(tmp_path / "b"), "--show-group")
    assert "Bob" not in off.stdout and "Bob" in on.stdout
    assert re.search(r"Bob\s+total\s+34", on.stdout)
    # group damage persists in the JSON regardless of the flag
    doc = json.loads(next((tmp_path / "a").glob("*.json")).read_text(encoding="utf-8"))
    assert "Bob" in doc["runs"][0]["fights"][2]["actors"]


def test_cli_live_without_address_fails_cleanly(tmp_path):
    # Phase 2 (main chat): the PINE text-buffer mode moved from --live to --live-pine.
    r = cli(tmp_path, "--live-pine", "--no-gui", "--runs-dir", str(tmp_path / "runs"))
    assert r.returncode != 0 and "Traceback" not in r.stderr
    assert "buffer.address" in r.stderr and "finding-the-buffer.md" in r.stderr and r.stdout == ""
    assert not (tmp_path / "runs").exists()


def test_cli_live_with_unreachable_pine_fails_cleanly(tmp_path):
    """BUG-1b (medium): address configured but PCSX2/PINE unreachable -> raw PineTimeoutError traceback, exit 1."""
    r = cli(tmp_path, "--live-pine", "--no-gui", "--runs-dir", str(tmp_path / "runs"),
            cfg_extra="buffer:\n  address: '0x00100000'\npine:\n  slot: 28997\n  timeout: 0.5\n")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr[-400:]


@pytest.mark.parametrize("case,cfg_extra,args", [
    ("bad-yaml", "idle_timeout: [unclosed\n", []),
    ("missing-patterns", "patterns_file: nope-patterns.yaml\n", []),
    ("bad-regex", None, []),
    ("missing-replay", "", ["--replay-override", "nope.log"]),
    ("string-idle-timeout", "idle_timeout: '8'\n", []),
    ("zero-idle-timeout", "idle_timeout: 0\n", []),
    ("negative-idle-timeout", "idle_timeout: -3\n", []),
])
def test_cli_bad_inputs_exit_cleanly_without_traceback(tmp_path, case, cfg_extra, args):
    """BUG-1 (medium): bad config YAML, missing/invalid patterns file, missing replay file and invalid idle_timeout in
    the config file all end in a raw Python traceback (exit 1) or silently nonsensical output (idle_timeout 0 -> 22
    fights) instead of 'error: ...' with exit 2 like an unknown config key does."""
    replay = str(FIXTURE)
    if "--replay-override" in args:
        replay, args = str(tmp_path / "nope.log"), []
    if case == "bad-regex":
        bad = tmp_path / "bad_patterns.yaml"
        bad.write_text("version: 1\npatterns:\n  - name: x\n    kind: damage\n    regex: '(unclosed'\n", encoding="utf-8")
        cfg_extra = f'patterns_file: "{bad.as_posix()}"\n'
    cfgp = tmp_path / "cfg.yaml"
    body = cfg_extra
    if "patterns_file" not in body:
        body = f'patterns_file: "{PATTERNS.as_posix()}"\n' + body
    body += f'unmatched_log: "{(tmp_path / "u.log").as_posix()}"\n'
    cfgp.write_text(body, encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "eqoa_parser", "--config", str(cfgp), "--replay", replay, "--no-gui",
                        "--runs-dir", str(tmp_path / "runs")], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert "Traceback" not in r.stderr, f"{case}: {r.stderr.strip().splitlines()[-1]}"
    assert r.returncode == 2 and "error" in r.stderr.lower(), f"{case}: rc={r.returncode}"


@pytest.mark.parametrize("flag,val", [("--idle-timeout", "nan"), ("--idle-timeout", "inf"), ("--speed", "nan")])
def test_cli_rejects_non_finite_numbers(tmp_path, flag, val):
    """BUG-7 (low): argparse float() accepts nan/inf; --speed nan hangs forever, --idle-timeout nan/inf never closes a fight."""
    try:
        r = cli(tmp_path, "--replay", str(FIXTURE), "--no-gui", "--runs-dir", str(tmp_path / "runs"), flag, val, timeout=15)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{flag} {val} hung")
    assert r.returncode == 2


def test_cli_missing_replay_file_does_not_create_a_run_or_leave_a_store(tmp_path):
    r = cli(tmp_path, "--replay", str(tmp_path / "nope.log"), "--no-gui", "--runs-dir", str(tmp_path / "runs"))
    assert r.returncode != 0
    assert not list((tmp_path / "runs").glob("*.json")) if (tmp_path / "runs").exists() else True


# ============================================================================== static contract checks
CORE = ["events", "config", "sources", "parser", "segmenter", "stats", "pipeline", "persistence", "pine", "pine_source",
        "mock_pine", "recon"]
UI_NAMES = {"tkinter", "_tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "pygame", "kivy", "turtle", "idlelib"}


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):                         # walk() sees function-local (lazy) imports too
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield (node.module or ""), node.level


@pytest.mark.parametrize("mod", CORE)
def test_core_module_static_imports_have_no_ui(mod):
    for name, level in _imports(ROOT / "eqoa_parser" / f"{mod}.py"):
        assert name.split(".")[0] not in UI_NAMES, (mod, name)
        assert not (level == 1 and name.split(".")[0] == "ui"), (mod, name)
        assert not name.startswith("eqoa_parser.ui"), (mod, name)


def test_requirements_minimal_and_complete():
    reqs = [ln.split("#")[0].strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()]
    names = {re.split(r"[<>=!~\[ ]", r)[0].lower() for r in reqs if r}
    assert names == {"pyyaml", "pytest"}
    third = set()
    for p in list((ROOT / "eqoa_parser").rglob("*.py")):
        for name, level in _imports(p):
            top = name.split(".")[0]
            if level == 0 and top and top not in sys.stdlib_module_names and top != "eqoa_parser":
                third.add(top)
    assert third == {"yaml"}, third
    pyproj = (ROOT / "pyproject.toml").read_text()
    assert 'dependencies = ["PyYAML>=6.0"]' in pyproj


def test_no_pine_write_opcodes_anywhere_in_client_code():
    """Identifiers (not docstrings) in the PINE client + buffer source never mention write/save/load-state, and the
    only opcodes the client can put on the wire are the read/info set."""
    for fname in ("pine.py", "pine_source.py"):
        tree = ast.parse((ROOT / "eqoa_parser" / fname).read_text(encoding="utf-8"))
        idents = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                idents.add(n.name)
            elif isinstance(n, ast.Name):
                idents.add(n.id)
            elif isinstance(n, ast.Attribute):
                idents.add(n.attr)
        bad = {i for i in idents if re.search(r"write|save_?state|load_?state", i, re.I)}
        assert not bad, (fname, bad)
    import eqoa_parser.pine as pine
    assert pine.IMPLEMENTED_OPCODES == {0, 1, 2, 3, 0x8, 0xB, 0xC, 0xD, 0xE, 0xF}


def test_event_is_frozen_and_slotted():
    e = ev(1.0)
    with pytest.raises(Exception):
        e.amount = 5
    assert not hasattr(e, "__dict__")


def test_config_edge_cases(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("pine:\n  slot: 1\n  bogus: 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)
    p.write_text("buffer:\n", encoding="utf-8")                   # null section -> defaults
    assert load_config(p).buffer.address is None
    p.write_text("pine: 5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)
    p.write_text("", encoding="utf-8")
    assert load_config(p).idle_timeout == 8.0
    assert load_config(idle_timeout=None).idle_timeout == 8.0     # None overrides ignored
    with pytest.raises(ValueError):
        load_config(nope=1)
    p.write_text("unmatched_log: sub/u.log\n", encoding="utf-8")
    assert load_config(p).resolve("sub/u.log") == tmp_path.resolve() / "sub" / "u.log"
