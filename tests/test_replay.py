"""ReplaySource tests (session B)."""
from __future__ import annotations

import threading
import time

from eqoa_parser.sources import HEARTBEAT, LogSource, ReplaySource


def write(tmp_path, text):
    f = tmp_path / "r.log"
    f.write_text(text, encoding="utf-8", newline="")
    return f


def test_is_a_log_source(tmp_path):
    assert isinstance(ReplaySource(write(tmp_path, "")), LogSource)


def test_basic_format_comments_blank_and_unchanged_ts(tmp_path):
    f = write(tmp_path, "# c\n\n1792263600.0\tYou hit\n  \n1792263601.5\tsecond line\twith tab\n")
    assert list(ReplaySource(f)) == [(1792263600.0, "You hit"), (1792263601.5, "second line\twith tab")]


def test_crlf_and_bom_and_utf8(tmp_path):
    f = tmp_path / "r.log"
    f.write_bytes("﻿1.0\tcafé\r\n2.0\tb\r\n".encode("utf-8"))
    assert list(ReplaySource(f)) == [(1.0, "café"), (2.0, "b")]


def test_malformed_lines_warn_to_stderr_and_skip(tmp_path, capsys):
    f = write(tmp_path, "no tab here\nabc\tbad ts\nnan\tx\n1.0\tgood\n")
    assert list(ReplaySource(f)) == [(1.0, "good")]
    err = capsys.readouterr().err
    assert err.count("skipped malformed line") == 3 and "no tab here" in err


def test_repeated_identical_lines_yielded_every_time(tmp_path):
    f = write(tmp_path, "1.0\tX\n1.0\tX\n1.0\tX\n")
    assert list(ReplaySource(f)) == [(1.0, "X")] * 3


def test_speed_zero_no_sleep_no_heartbeats(tmp_path):
    f = write(tmp_path, "0\ta\n1000\tb\n")
    t = time.monotonic()
    out = list(ReplaySource(f, speed=0))
    assert time.monotonic() - t < 0.5
    assert out == [(0.0, "a"), (1000.0, "b")]


def test_speed_scaled_sleep_with_heartbeats(tmp_path):
    f = write(tmp_path, "100.0\ta\n100.6\tb\n100.6\tc\n")
    src = ReplaySource(f, speed=2.0, heartbeat_interval=0.05)  # real gap 0.3s
    t = time.monotonic()
    out = list(src)
    elapsed = time.monotonic() - t
    assert 0.25 <= elapsed < 1.5
    real = [o for o in out if o[1] != HEARTBEAT]
    assert real == [(100.0, "a"), (100.6, "b"), (100.6, "c")]
    hbs = [o for o in out if o[1] == HEARTBEAT]
    assert len(hbs) >= 2
    tss = [o[0] for o in out]
    assert tss == sorted(tss)  # non-decreasing, heartbeats included
    assert all(100.0 <= h[0] <= 100.6 for h in hbs)


def test_close_stops_iteration_midway(tmp_path):
    f = write(tmp_path, "0\ta\n1\tb\n2\tc\n")
    src = ReplaySource(f, speed=0)
    it = iter(src)
    assert next(it) == (0.0, "a")
    src.close()
    src.close()
    assert list(it) == []


def test_close_interrupts_sleep(tmp_path):
    f = write(tmp_path, "0\ta\n3600\tb\n")
    src = ReplaySource(f, speed=1.0, heartbeat_interval=0.05)
    threading.Timer(0.2, src.close).start()
    t = time.monotonic()
    out = list(src)
    assert time.monotonic() - t < 3
    assert out[0] == (0.0, "a") and (3600.0, "b") not in out


def test_engine_heartbeat_closes_fight_in_realtime_replay(tmp_path):
    from eqoa_parser.config import load_config
    from eqoa_parser.pipeline import Engine

    f = write(tmp_path, "0\tYou hit a rat for 5 points of damage.\n40\tYou hit a rat for 6 points of damage.\n")
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), idle_timeout=8.0)
    when = []
    t0 = time.monotonic()
    e = Engine(cfg, ReplaySource(f, speed=100.0, heartbeat_interval=0.02),
               on_closed=lambda s: when.append((s.fight_id, time.monotonic() - t0)))
    e.run()
    # fight 1 closed by a heartbeat (~0.08s real) well before the second line arrived (~0.4s real)
    assert when[0][0] == 1 and when[0][1] < 0.35
    assert [w[0] for w in when] == [1, 2]
