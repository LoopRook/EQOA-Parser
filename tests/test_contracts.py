"""Contract tests written by the main chat. Owned by main chat; sessions must not edit this file."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "synthetic_combat.log"
EXPECTED = json.loads((ROOT / "fixtures" / "synthetic_combat.expected.json").read_text(encoding="utf-8"))

CORE_MODULES = [
    "eqoa_parser.events",
    "eqoa_parser.config",
    "eqoa_parser.sources",
    "eqoa_parser.parser",
    "eqoa_parser.segmenter",
    "eqoa_parser.stats",
    "eqoa_parser.pipeline",
    "eqoa_parser.persistence",
    "eqoa_parser.pine",
    "eqoa_parser.pine_source",
    "eqoa_parser.mock_pine",
    "eqoa_parser.recon",
]
UI_ROOTS = {"tkinter", "_tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "pygame", "kivy"}


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_has_no_ui_imports(module):
    code = (
        "import sys, importlib\n"
        f"importlib.import_module({module!r})\n"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {UI_ROOTS!r} or m.startswith('eqoa_parser.ui'))\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"{module} pulled in UI modules: {r.stdout}{r.stderr}"


def test_event_fields_in_contract_order():
    from dataclasses import fields

    from eqoa_parser.events import Event

    assert [f.name for f in fields(Event)] == ["ts", "actor", "target", "amount", "ability", "kind", "raw"]


def test_patterns_file_is_marked_synthetic_until_verified():
    doc = yaml.safe_load((ROOT / "patterns.yaml").read_text(encoding="utf-8"))
    assert doc["status"] in {"SYNTHETIC", "VERIFIED"}
    for p in doc["patterns"]:
        assert p.get("status") in {"ASSUMED", "VERIFIED"}, p["name"]
        if doc["status"] == "SYNTHETIC":
            assert p["status"] == "ASSUMED", p["name"]


def test_config_defaults_and_unknown_keys(tmp_path):
    from eqoa_parser.config import load_config

    cfg = load_config()
    assert cfg.idle_timeout == 8.0 and cfg.pine.slot == 28011 and cfg.show_group is False
    assert cfg.buffer.address is None
    p = tmp_path / "c.yaml"
    p.write_text("buffer:\n  address: '0x00123400'\nidle_timeout: 5\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.buffer.address == 0x00123400 and cfg.idle_timeout == 5
    p.write_text("idle_timout: 5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)


def _close(a, b, tol=0.01):
    return abs(a - b) <= tol


def test_replay_fixture_matches_hand_computed_oracle(tmp_path):
    """End-to-end acceptance: ReplaySource -> Engine on the SYNTHETIC fixture vs hand-computed numbers."""
    from eqoa_parser.config import load_config
    from eqoa_parser.pipeline import Engine
    from eqoa_parser.sources import ReplaySource

    cfg = load_config(unmatched_log=str(tmp_path / "unmatched.log"), runs_dir=str(tmp_path / "runs"))
    closed = []
    engine = Engine(cfg, ReplaySource(FIXTURE, speed=0), on_closed=closed.append)
    engine.run()

    base = EXPECTED["base_ts"]
    assert len(closed) == len(EXPECTED["fights"])
    assert [f.fight_id for f in engine.closed_fights] == [f["fight_id"] for f in EXPECTED["fights"]]
    for got, exp in zip(closed, EXPECTED["fights"]):
        assert got.fight_id == exp["fight_id"]
        assert got.ongoing is False
        assert got.target == exp["target"]
        assert got.targets == exp["targets"]
        assert _close(got.start_ts - base, exp["start_offset"])
        assert _close(got.end_ts - base, exp["end_offset"])
        assert _close(got.duration, exp["duration"])
        for who in ("you", "pet"):
            a, e = got.actor(who), exp[who]
            assert (a.total, a.hits, a.max_hit) == (e["total"], e["hits"], e["max_hit"]), (exp["fight_id"], who)
            assert _close(a.dps, e["dps"]), (exp["fight_id"], who, a.dps)
        for name, e in exp.get("group", {}).items():
            a = got.actor(name)
            assert (a.total, a.hits, a.max_hit) == (e["total"], e["hits"], e["max_hit"])
            assert _close(a.dps, e["dps"])
        assert got.combined_total == exp["combined_total"]
        assert _close(got.combined_dps, exp["combined_dps"])
        assert got.damage_taken == exp["damage_taken"]
        assert got.top_abilities == exp["top_abilities"]

    run, er = engine.run_summary(), EXPECTED["run"]
    assert run.fight_count == er["fight_count"]
    assert run.total_damage == er["total_damage"]
    assert _close(run.average_dps, er["average_dps"])
    assert (run.best_fight_id, run.best_fight_target) == (er["best_fight_id"], er["best_fight_target"])
    assert _close(run.best_fight_dps, er["best_fight_dps"])

    unmatched = (tmp_path / "unmatched.log").read_text(encoding="utf-8").splitlines()
    assert len(unmatched) == EXPECTED["unmatched_count"]
    assert all("\t" in line for line in unmatched)


def test_fight_summary_round_trips_through_dict():
    from eqoa_parser.stats import AbilityStats, ActorStats, FightSummary

    s = FightSummary(
        fight_id=1, target="a rat", targets=["a rat"], start_ts=1.0, end_ts=2.0, duration=1.0,
        actors={"you": ActorStats("you", 5, 1, 5, 5.0, {"melee": AbilityStats("melee", 5, 1, 5)})},
        combined_total=5, combined_dps=5.0, damage_taken=0,
        top_abilities=[{"actor": "you", "ability": "melee", "total": 5}],
    )
    assert FightSummary.from_dict(json.loads(json.dumps(s.to_dict()))) == s
