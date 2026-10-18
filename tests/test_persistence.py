from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from eqoa_parser import persistence
from eqoa_parser.persistence import RunStore, day_file
from eqoa_parser.stats import FightSummary
from test_ui_fixtures import make_fight

T0 = datetime(2026, 10, 17, 19, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 17, 21, 30, 5, tzinfo=timezone.utc)


def read(tmp_path):
    return json.loads((tmp_path / "2026-10-17.json").read_text(encoding="utf-8"))


def test_round_trip_and_schema(tmp_path):
    s = RunStore(tmp_path, "replay:x.log", now=T0)
    f1, f2 = make_fight(1, bob=7), make_fight(2, target="a bat", pet=0)
    s.add_fight(f1)
    s.add_fight(f2)
    doc = read(tmp_path)
    assert doc["schema_version"] == 1 and doc["date"] == "2026-10-17"
    (run,) = doc["runs"]
    assert run["run_id"] == "20261017T190000" and run["source"] == "replay:x.log"
    assert run["started_at"].startswith("2026-10-17T19:00:00")
    assert [FightSummary.from_dict(d) for d in run["fights"]] == [f1, f2]


def test_second_launch_appends_and_reloads_earlier_runs(tmp_path):
    a = RunStore(tmp_path, "replay:a", now=T0)
    a.add_fight(make_fight(1))
    a.add_fight(make_fight(2))
    b = RunStore(tmp_path, "live:pine", now=T1)
    assert [(p.run_id, p.summary.fight_id) for p in b.previous_fights] == [
        ("20261017T190000", 1), ("20261017T190000", 2)]
    b.add_fight(make_fight(1, target="a bat"))
    doc = read(tmp_path)
    assert [r["run_id"] for r in doc["runs"]] == ["20261017T190000", "20261017T213005"]
    assert [len(r["fights"]) for r in doc["runs"]] == [2, 1]
    assert doc["runs"][0]["source"] == "replay:a"  # earlier run untouched


def test_same_second_launch_gets_unique_run_id(tmp_path):
    RunStore(tmp_path, "a", now=T0).add_fight(make_fight(1))
    RunStore(tmp_path, "b", now=T0).add_fight(make_fight(1))
    ids = [r["run_id"] for r in read(tmp_path)["runs"]]
    assert len(set(ids)) == 2


def test_different_day_uses_different_file(tmp_path):
    RunStore(tmp_path, "a", now=T0).add_fight(make_fight(1))
    nxt = RunStore(tmp_path, "b", now=datetime(2026, 10, 18, 1, 0, 0, tzinfo=timezone.utc))
    assert nxt.previous_fights == []
    assert nxt.path == day_file(tmp_path, nxt.now)


@pytest.mark.parametrize("content", [b"{not json", b"", b"[1,2]", b'{"schema_version": 99, "runs": []}',
                                     b'{"schema_version": 1, "runs": "x"}', b"\xff\xfe\x00bad"])
def test_corrupt_file_renamed_never_lost(tmp_path, content):
    path = tmp_path / "2026-10-17.json"
    path.write_bytes(content)
    warnings = []
    s = RunStore(tmp_path, "x", now=T0, warn=warnings.append)
    assert s.previous_fights == [] and warnings and s.corrupt_path is not None
    assert s.corrupt_path.read_bytes() == content          # original bytes preserved
    assert ".corrupt-" in s.corrupt_path.name
    s.add_fight(make_fight(1))
    assert len(read(tmp_path)["runs"][0]["fights"]) == 1


def test_unreadable_fight_is_skipped_but_kept_in_file(tmp_path):
    RunStore(tmp_path, "a", now=T0).add_fight(make_fight(1))
    path = tmp_path / "2026-10-17.json"
    doc = json.loads(path.read_text())
    doc["runs"][0]["fights"].append({"bogus": True})
    path.write_text(json.dumps(doc))
    warnings = []
    b = RunStore(tmp_path, "b", now=T1, warn=warnings.append)
    assert len(b.previous_fights) == 1 and warnings
    b.add_fight(make_fight(1))
    assert {"bogus": True} in read(tmp_path)["runs"][0]["fights"]


def test_atomic_write_failure_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    s = RunStore(tmp_path, "a", now=T0)
    s.add_fight(make_fight(1))
    path = tmp_path / "2026-10-17.json"
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(persistence.os, "replace", boom)
    with pytest.raises(OSError):
        s.add_fight(make_fight(2))
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert os.listdir(tmp_path) == ["2026-10-17.json"]  # temp file cleaned up


def test_write_goes_through_temp_then_replace(tmp_path, monkeypatch):
    calls = []
    real = os.replace

    def spy(a, b):
        calls.append((str(a), str(b)))
        real(a, b)

    monkeypatch.setattr(persistence.os, "replace", spy)
    RunStore(tmp_path, "a", now=T0).add_fight(make_fight(1))
    ((src, dst),) = calls
    assert dst.endswith("2026-10-17.json") and src.endswith(".tmp") and src != dst


def test_concurrent_launches_merge_by_run_id(tmp_path):
    a = RunStore(tmp_path, "replay:a", now=T0)
    b = RunStore(tmp_path, "replay:b", now=T0)          # same second: ids must still differ
    b.add_fight(make_fight(1, target="b1"))
    a.add_fight(make_fight(1, target="a1"))     # same base id as b: a must be renamed, not overwrite b
    b.add_fight(make_fight(2, target="b2"))
    assert a.run_id != b.run_id
    runs = {r["source"]: [f["target"] for f in r["fights"]] for r in read(tmp_path)["runs"]}
    assert runs == {"replay:a": ["a1"], "replay:b": ["b1", "b2"]}
    assert not list(tmp_path.glob("*.lock"))


def test_lock_released_after_failed_save(tmp_path, monkeypatch):
    s = RunStore(tmp_path, "a", now=T0)
    monkeypatch.setattr(persistence, "atomic_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        s.add_fight(make_fight(1))
    assert not list(tmp_path.glob("*.lock"))


def test_stale_lock_from_crashed_launch_is_broken(tmp_path):
    lock = tmp_path / "2026-10-17.json.lock"
    lock.write_text("")
    old = os.path.getmtime(lock) - 3600
    os.utime(lock, (old, old))
    RunStore(tmp_path, "a", now=T0).add_fight(make_fight(1))
    assert len(read(tmp_path)["runs"][0]["fights"]) == 1


def test_live_lock_times_out_with_oserror(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "LOCK_TIMEOUT", 0.2)
    (tmp_path / "2026-10-17.json.lock").write_text("")
    with pytest.raises(OSError, match="lock"):
        RunStore(tmp_path, "a", now=T0)
