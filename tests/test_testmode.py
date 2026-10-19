"""Phase 7: timed tests (testmode.py + Engine hook), pet names, persistence of tests, junk character names."""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eqoa_parser import app
from eqoa_parser.config import load_config
from eqoa_parser.events import KIND_DAMAGE, KIND_DAMAGE_TAKEN, KIND_MISS, Event
from eqoa_parser.net.attribution import Attributor, ConnectionRouter
from eqoa_parser.net.memory import MemorySnapshot
from eqoa_parser.net.names import NameTable, OwnNames, is_character_name
from eqoa_parser.net.pcapng import UdpDatagram, read_pcapng
from eqoa_parser.net.protocol import HpChange, PacketDecoder
from eqoa_parser.net.sources import PcapReplaySource
from eqoa_parser.persistence import RunStore
from eqoa_parser.pipeline import Engine
from eqoa_parser.segmenter import Fight
from eqoa_parser.sources import HEARTBEAT
from eqoa_parser.stats import FightSummary, group_rows, summarize_fight
from eqoa_parser.testmode import TestResult, TestRun
from eqoa_parser.ui.console import format_test, result_rows

ROOT = Path(__file__).resolve().parent.parent
CAP5 = ROOT / "fixtures" / "real" / "cap5_mage_spells_then_pet.pcapng"
SERVER = "5.161.217.188"
MAIGE = 0x1C08FB90


def dmg(ts, actor="you", amount=100, target="dummy", kind=KIND_DAMAGE):
    return Event(ts, actor, target, amount, None, kind, "")


def make_engine(tmp_path, results=None, source=None):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
    return Engine(cfg, source or [], on_test=(results.append if results is not None else None))


# -- TestRun boundaries --------------------------------------------------------------------------------------------
def test_clock_starts_at_first_own_damage_and_event_at_end_is_excluded():
    t = TestRun("x", 10)
    t.feed(dmg(5, "someone else"))
    t.feed(dmg(6, "you", 50, kind=KIND_DAMAGE_TAKEN))
    t.feed(dmg(7, "you", 0, kind=KIND_MISS))
    assert t.state == "armed" and t.start_ts is None
    t.feed(dmg(100.0, "you", 10))
    t.feed(dmg(109.999, "you", 20))
    t.feed(dmg(110.0, "you", 40))      # exactly start + length: not counted, and it ends the test
    assert t.finished
    r = t.result()
    assert (r.total, r.hits, r.start_ts) == (30, 2, 100.0)
    assert r.dps == pytest.approx(3.0)  # fixed denominator


def test_no_own_damage_stays_armed_even_with_heartbeats():
    t = TestRun("x", 5)
    for ts in (1, 100, 1000):
        t.tick(ts)
    assert t.state == "armed" and t.result() is None
    t.end_of_stream()
    assert t.state == "cancelled"


def test_heartbeats_end_the_test_on_time():
    t = TestRun("x", 30)
    t.feed(dmg(10, "you", 300))
    t.tick(39.9)
    assert t.state == "running"
    assert 0 < t.remaining() < 1 and "0:01 left" in t.status_text()
    t.tick(40.0)
    assert t.finished and t.result().dps == pytest.approx(10.0)


def test_pet_only_test_and_per_character_split():
    groups = [{"name": "Maige", "key": "you", "player": "you", "pet": "pet"},
              {"name": "Zecron", "key": "Zecron", "player": "Zecron", "pet": "Zecron's pet"}]
    t = TestRun("pets", 20)
    t.feed(dmg(1, "pet", 500), groups, {"pet": "Djinni Vizier"})
    t.feed(dmg(2, "pet", 700), groups, {"pet": "Djinni Vizier"})
    t.feed(dmg(3, "Zecron", 90), groups)
    t.feed(dmg(4, "Zecron's pet", 10), groups)
    t.tick(21)
    r = t.result()
    maige, zecron = r.characters
    assert (maige.name, maige.player_total, maige.pet_total, maige.pet_hits, maige.pet_max) == ("Maige", 0, 1200, 2, 700)
    assert maige.pet_name == "Djinni Vizier" and maige.combined == 1200 and maige.dps == pytest.approx(60.0)
    assert (zecron.player_total, zecron.pet_total, zecron.combined) == (90, 10, 100)
    assert r.max_hit == 700 and r.hits == 4 and r.pet_names == ["Djinni Vizier"]
    text = format_test(r)
    assert "Maige's pet (Djinni Vizier)" in text and "All characters" in text


def test_cancel_and_incomplete_result():
    t = TestRun("x", 60)
    t.feed(dmg(1, "you", 5))
    t.cancel()
    assert t.state == "cancelled" and t.result() is None
    u = TestRun("y", 60)
    u.feed(dmg(1, "you", 5))
    u.end_of_stream()
    r = u.result()
    assert r.complete is False and r.dps == pytest.approx(5 / 60)
    assert "source ended early" in format_test(r)


def test_length_must_be_positive():
    with pytest.raises(ValueError):
        TestRun("x", 0)


def test_result_round_trip_ignores_unknown_keys():
    t = TestRun("a", 10)
    t.feed(dmg(1, "pet", 7), None, {"pet": "Imp"})
    t.tick(11)
    d = t.result().to_dict()
    d["future"] = 1
    d["characters"][0]["future"] = 2
    again = TestResult.from_dict(json.loads(json.dumps(d)))
    assert again.to_dict() == t.result().to_dict()


# -- Engine hook ---------------------------------------------------------------------------------------------------
def test_engine_feeds_test_without_changing_fights(tmp_path):
    def run(arm):
        results = []
        eng = make_engine(tmp_path, results)
        if arm:
            eng.arm_test(TestRun("e", 20))
        for ts, ev in [(0, dmg(0, "you", 10)), (5, dmg(5, "you", 10)), (30, dmg(30, "you", 99)), (31, HEARTBEAT),
                       (60, HEARTBEAT)]:
            eng.step(ts, ev)
        eng.finish()
        return [f.to_dict() for f in eng.closed_fights], results

    plain, _ = run(False)
    with_test, results = run(True)
    assert plain == with_test            # segmentation is untouched
    (r,) = results
    assert (r.total, r.hits, r.start_ts, r.complete) == (20, 2, 0, True)


def test_engine_end_of_stream_finalizes_running_test(tmp_path):
    results = []
    eng = make_engine(tmp_path, results)
    eng.arm_test(TestRun("e", 100))
    eng.step(1, dmg(1, "you", 10))
    eng.finish()
    assert len(results) == 1 and results[0].complete is False


# -- deterministic replay on the real capture ---------------------------------------------------------------------
def test_cap5_pet_test_matches_independent_sum(tmp_path):
    dec = PacketDecoder(SERVER)
    recs = [r for d in read_pcapng(CAP5) for r in dec.feed(d)]
    pet = [r for r in recs if isinstance(r, HpChange) and r.source == MAIGE and r.amount < 0 and r.extended]
    first = pet[0].ts
    window = [r for r in pet if first <= r.ts < first + 30]
    assert 0 < len(window) < len(pet)

    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
    cfg.net.local_port = 9032
    results = []
    src = PcapReplaySource(CAP5, cfg, speed=0)
    eng = Engine(cfg, src, on_test=results.append)
    test = TestRun("pet 30s", 30)
    armed = False
    for ts, item in src:
        if not armed and isinstance(item, Event) and item.actor == "pet":
            eng.arm_test(test)           # armed just before the pet phase
            armed = True
        eng.step(ts, item)
    eng.finish()
    assert armed and len(results) == 1
    r = results[0]
    (c,) = r.characters
    assert c.pet_total == sum(-x.amount for x in window)
    assert c.pet_hits == len(window) and c.player_total == 0
    assert r.start_ts == pytest.approx(first) and r.complete
    assert r.dps == pytest.approx(c.pet_total / 30)


def test_cap5_console_test_prints_and_saves(tmp_path, capsys):
    runs = tmp_path / "runs"
    code = app.main(["--pcap", str(CAP5), "--connection", "9032", "--no-gui", "--test", "30", "--test-label",
                     "pet 30s", "--runs-dir", str(runs), "--config", str(ROOT / "config.yaml")])
    out = capsys.readouterr().out
    assert code == 0 and "=== Test 'pet 30s': 30s ===" in out
    (f,) = runs.glob("*.json")
    doc = json.loads(f.read_text(encoding="utf-8"))
    (saved,) = doc["runs"][0]["tests"]
    assert saved["label"] == "pet 30s" and saved["length_s"] == 30


@pytest.mark.parametrize("argv", [["--no-gui", "--test", "0"], ["--no-gui", "--test", "-5"],
                                  ["--no-gui", "--test-label", "x"], ["--test", "5"]])
def test_cli_rejects_bad_test_options(argv):
    with pytest.raises(SystemExit) as e:
        app.parse_args(["--pcap", "x.pcapng", *argv])
    assert e.value.code == 2


# -- persistence -------------------------------------------------------------------------------------------------
T0 = datetime(2026, 10, 19, 19, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 19, 21, 0, 0, tzinfo=timezone.utc)


def _result(label="a"):
    t = TestRun(label, 10)
    t.feed(dmg(1, "you", 40))
    t.feed(dmg(2, "pet", 60), None, {"pet": "Imp"})
    t.tick(20)
    return t.result()


def test_tests_persist_and_reload_same_day(tmp_path):
    s = RunStore(tmp_path, "live:capture", now=T0)
    assert s.previous_tests == []
    s.add_test(_result("one"))
    s.add_test(_result("two"))
    s2 = RunStore(tmp_path, "live:capture", now=T1)
    assert [r.label for r in s2.previous_tests] == ["one", "two"]
    assert s2.previous_tests[0].characters[0].pet_name == "Imp"
    s2.add_test(_result("three"))
    s3 = RunStore(tmp_path, "live:capture", now=T1.replace(hour=22))
    assert [r.label for r in s3.previous_tests] == ["one", "two", "three"]


def test_old_files_without_tests_load_and_stay_untouched(tmp_path):
    path = tmp_path / "2026-10-19.json"
    old = {"schema_version": 1, "date": "2026-10-19",
           "runs": [{"run_id": "20261019T100000", "started_at": "x", "source": "s", "fights": []}]}
    path.write_text(json.dumps(old), encoding="utf-8")
    s = RunStore(tmp_path, "live:capture", now=T0)
    assert s.previous_tests == []
    s.add_fight(summarize_fight(Fight(1, 0.0, 1.0, [dmg(0), dmg(1)])))
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "tests" not in doc["runs"][0] and "tests" not in doc["runs"][1]


def test_unreadable_test_entry_is_skipped_but_kept(tmp_path):
    path = tmp_path / "2026-10-19.json"
    doc = {"schema_version": 1, "date": "2026-10-19", "runs": [
        {"run_id": "r1", "started_at": "x", "source": "s", "fights": [], "tests": [{"nonsense": 1}, _result().to_dict()]}]}
    path.write_text(json.dumps(doc), encoding="utf-8")
    warnings = []
    s = RunStore(tmp_path, "live:capture", now=T0, warn=warnings.append)
    assert len(s.previous_tests) == 1 and warnings
    s.add_test(_result("n"))
    assert len(json.loads(path.read_text(encoding="utf-8"))["runs"][0]["tests"]) == 2


# -- pet names ---------------------------------------------------------------------------------------------------
def _fight():
    return Fight(1, 0.0, 5.0, [dmg(0, "you", 10), dmg(1, "pet", 20)])


def test_pet_names_absent_leaves_output_unchanged():
    g = [{"name": "Maige", "key": "you", "player": "you", "pet": "pet"}]
    s = summarize_fight(_fight(), groups=g)
    d = s.to_dict()
    assert s.pet_names is None and "pet_names" not in d
    assert FightSummary.from_dict(d).to_dict() == d
    assert group_rows(s)[0].pet_name == "Maige's pet"


def test_pet_names_shown_and_round_trip():
    g = [{"name": "Maige", "key": "you", "player": "you", "pet": "pet"}]
    s = summarize_fight(_fight(), groups=g, pet_names={"pet": "Djinni Vizier"})
    assert group_rows(s)[0].pet_name == "Maige's pet (Djinni Vizier)"
    back = FightSummary.from_dict(json.loads(json.dumps(s.to_dict())))
    assert back.pet_names == {"pet": "Djinni Vizier"}
    assert group_rows(summarize_fight(_fight(), pet_names={"pet": "Imp"}))[0].pet_name == "Pet (Imp)"


class FakeMemory:
    def __init__(self, snap):
        self._snap = snap

    def snapshot(self):
        return self._snap


def _hp(ts, source, amount, target, extended=False, conn=9032):
    return HpChange(ts, conn, MAIGE & 0xFFFF, 1, source, amount, target, extended, b"")


def test_router_reports_pet_entity_name_from_memory():
    cfg = load_config()
    snap = MemorySnapshot(version=1, ok=True, own_name="Maige", own_id=MAIGE, pet_id=0x5001,
                          entities=((0x5001, "Djinni Vizier", 30, "pet"),))
    router = ConnectionRouter(cfg, memory=FakeMemory(snap))
    assert router.feed(_hp(1.0, MAIGE, -100, 0x01313186, extended=True))
    (g,) = router.character_groups()
    assert g.name == "Maige" and g.pet_npc == "Djinni Vizier"
    # the entity leaves the list: the name is remembered
    router.memory = FakeMemory(MemorySnapshot(version=2, ok=True, own_name="Maige", own_id=MAIGE, pet_id=0x5001))
    router.feed(_hp(2.0, MAIGE, -100, 0x01313186, extended=True))
    assert router.character_groups()[0].pet_npc == "Djinni Vizier"


def test_router_without_pine_has_no_pet_name():
    router = ConnectionRouter(load_config())
    router.feed(_hp(1.0, MAIGE, -100, 0x01313186, extended=True))
    assert router.character_groups()[0].pet_npc is None


# -- junk character names ------------------------------------------------------------------------------------------
def _spawn_payload(entity_id: int, name: bytes) -> bytes:
    return struct.pack("<I", entity_id) + b"\x82\x31\x00" + bytes([0x80 | len(name), 0x04]) + name


def _dgram(payload):
    return UdpDatagram(1.0, SERVER, 1, "10.0.0.2", 9032, payload)


@pytest.mark.parametrize("junk", [b"tsrq", b"xqzv", b"ab cd"])
def test_name_table_ignores_lowercase_junk(junk):
    t = NameTable(SERVER)
    t.feed(_dgram(_spawn_payload(0x1234, junk)))
    assert t.get(0x1234) is None


@pytest.mark.parametrize("good", [b"Maige", b"Rotting Flesh", b"a bat", b"The Guard", b"Captain Scilond"])
def test_name_table_keeps_real_names(good):
    t = NameTable(SERVER)
    t.feed(_dgram(_spawn_payload(0x1234, good)))
    assert t.get(0x1234) == good.decode()


@pytest.mark.parametrize("name,ok", [("Maige", True), ("Zecron", True), ("Abc", True), ("Abcdefghijklmno", True), ("O'Brien-Smith", True),
                                     ("tsrq", False), ("TSRQ", False), ("Ab", False), ("Abcdefghijklmnop", False),
                                     ("Mai ge", False), ("Maige2", False), ("", False), (None, False)])
def test_is_character_name(name, ok):
    assert is_character_name(name) is ok


def test_own_name_rule_rejects_junk():
    def feed(name: bytes):
        o = OwnNames(SERVER)
        o.feed(_dgram(b"data\\x.esf" + b"\x01" + struct.pack("<I", len(name)) + name))
        return o.get(9032)

    assert feed(b"Zecron") == "Zecron"
    assert feed(b"tsrq") is None


def test_junk_name_cannot_become_a_character_label():
    names = NameTable(SERVER)
    pid = 0x174A1A40
    names._names[pid] = "tsrq"            # even if one slipped into the table
    attr = Attributor(names, claim_primary=lambda a: False)
    rec = HpChange(1.0, 22154, pid & 0xFFFF, 1, pid, -50, 0x01313186, False, b"")
    assert attr.feed(rec)
    assert attr.label == "char:22154"

    names._names[pid] = "Curie"
    attr2 = Attributor(names, claim_primary=lambda a: False)
    attr2.feed(rec)
    assert attr2.label == "Curie"
