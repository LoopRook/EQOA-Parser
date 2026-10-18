"""Regression tests for the post-verification amendments (BUG-3/4/5/6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from eqoa_parser.parser import MAX_LINE_LEN, Parser, Pattern
from eqoa_parser.sources import ReplaySource
from eqoa_parser.stats import summarize_fight
from eqoa_parser.segmenter import Segmenter

PATTERNS = Path(__file__).resolve().parent.parent / "patterns.yaml"


def test_amount_over_9_digits_is_unmatched_not_crash(tmp_path):
    p = Parser(PATTERNS, unmatched_path=tmp_path / "u.log")
    assert p.parse(1, "You hit a rat for 999999999 points of damage.").amount == 999999999
    assert p.parse(1, "You hit a rat for 1000000000 points of damage.") is None
    assert p.parse(1, "You hit a rat for " + "9" * 5000 + " points of damage.") is None
    assert p.unmatched_count == 2


def test_stats_handles_max_parser_amounts():
    p = Parser(PATTERNS)
    s = Segmenter()
    for i in range(3):
        s.feed(p.parse(i, "You hit a rat for 999999999 points of damage."))
    f = summarize_fight(s.flush()[0])
    assert f.combined_total == 3 * 999999999 and f.you.max_hit == 999999999


def test_long_line_unmatched_without_regex(tmp_path):
    class Boom(Parser):
        pass

    p = Parser(PATTERNS, unmatched_path=tmp_path / "u.log")
    long = "Bob's " + "hits " * 20000
    assert p.parse(1, long) is None and p.unmatched_count == 1
    ok = "You hit a rat for 5 points of damage."
    padded = ok + " " * 1000  # stripped length is short: still parsed
    assert p.parse(1, padded).amount == 5
    edge = "x" * MAX_LINE_LEN
    assert p.parse(1, edge) is None


def test_long_line_skips_regex_engine():
    pats = [Pattern("t", "damage", r"(?P<actor>.*) (?P<target>.*) (?P<amount>\d+)")]
    p = Parser(pats)
    assert p.parse(1, "a b 3" + " 3" * 400) is None  # >512 chars: not even tried


@pytest.mark.parametrize("ch", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\r", "\n", "\x00", "\x7f"])
def test_unmatched_entries_stay_one_line(tmp_path, ch):
    log = tmp_path / "u.log"
    p = Parser(PATTERNS, unmatched_path=log)
    p.parse(1.0, f"a{ch}b")
    p.parse(2.0, f"c{ch}d")
    text = log.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 2 and text.count("\n") == 2


def test_backslash_escaped_and_lone_surrogate_survives(tmp_path):
    log = tmp_path / "u.log"
    p = Parser(PATTERNS, unmatched_path=log)
    p.parse(1.0, "a\\nb")
    assert log.read_text(encoding="utf-8") == "1.0\ta\\\\nb\n"
    p.parse(2.0, "bad \ud800 surrogate")
    assert p.unmatched_count == 2
    assert "surrogate" in log.read_text(encoding="utf-8")


def test_parse_never_raises_on_garbage():
    p = Parser(PATTERNS)
    for raw in ["\ud800", "\x00" * 10, "You hit \ud800 for 5 points of damage.", "é" * 3000, " "]:
        p.parse(1.0, raw)


def test_non_numeric_amount_group_does_not_raise():
    pats = [Pattern("t", "damage", r"(?P<actor>\w+) hits (?P<target>\w+) (?P<amount>\w+)")]
    p = Parser(pats)
    assert p.parse(1, "Bob hits Al lots") is None and p.unmatched_count == 1


def test_replay_regressing_ts_clamped_with_warning(tmp_path, capsys):
    f = tmp_path / "r.log"
    f.write_text("10\ta\n5\tb\n12\tc\n11\td\n", encoding="utf-8")
    out = list(ReplaySource(f))
    assert out == [(10.0, "a"), (10.0, "b"), (12.0, "c"), (12.0, "d")]
    assert capsys.readouterr().err.count("clamped") == 2
