"""Phase 4: per-character display (docs/CONTRACTS.md "Phase 4"). Own-name rule, groups, group_rows, reload, acceptance."""
from __future__ import annotations

import json
import struct
from pathlib import Path

from _net_builders import CLIENT, SERVER

from eqoa_parser.config import load_config
from eqoa_parser.net.names import OwnNames
from eqoa_parser.net.pcapng import UdpDatagram, read_pcapng
from eqoa_parser.net.sources import PcapReplaySource
from eqoa_parser.persistence import RunStore
from eqoa_parser.pipeline import Engine
from eqoa_parser.stats import (
    ALL_CHARACTERS, FightSummary, character_runs, group_rows, summarize_run,
)
from eqoa_parser.ui.console import ConsoleUI, display_rows, format_fight, format_run
from test_ui_fixtures import actor, make_fight

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
CAP1 = REAL / "cap1_necro_spells.pcapng"
CAP2 = REAL / "cap2_bard_melee_necro_pet.pcapng"


def run_capture(path, tmp_path, **net):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    for k, v in net.items():
        setattr(cfg.net, k, v)
    src = PcapReplaySource(path, cfg)
    closed = []
    Engine(cfg, src, on_closed=closed.append).run()
    return closed, src


def esf_dgram(name: bytes, port=20605, src=SERVER, ts=1.0, varint=b"\xce\x81\x04", length=None):
    n = len(name) if length is None else length
    data = b"\x00\r\x00\x00\x10\x00\x00\x00data\\tunaria.esf" + varint + struct.pack("<I", n) + name + b"\x16\x04."
    return UdpDatagram(ts, src, 10070, CLIENT, port, data)


# ---------------------------------------------------------------------------- own-name rule
def test_own_name_on_real_captures():
    t1 = OwnNames("5.161.217.188")
    for d in read_pcapng(CAP1):
        t1.feed(d)
    assert t1.as_dict() == {}                      # cap1: the necro was already logged in

    t2 = OwnNames("5.161.217.188")
    seen = []
    t0 = None
    for d in read_pcapng(CAP2):
        t0 = d.ts if t0 is None else t0
        before = t2.as_dict()
        t2.feed(d)
        if t2.as_dict() != before:
            seen.append((d.ts - t0, d.dst_port, t2.get(d.dst_port)))
    assert t2.as_dict() == {20605: "Zecron"}       # nothing else, on that connection only
    assert len(seen) == 1 and seen[0][1:] == (20605, "Zecron") and 112 < seen[0][0] < 116


def test_own_name_rule_details():
    t = OwnNames(SERVER)
    t.feed(esf_dgram(b"Zecron"))
    assert t.get(20605) == "Zecron" and t.get(1) is None
    t.feed(esf_dgram(b"Maige", port=2222, varint=b"\x05"))              # a one-byte varint
    assert t.get(2222) == "Maige"
    t.feed(esf_dgram(b"Zecronb", ts=2.0))                               # a later name replaces
    assert t.get(20605) == "Zecronb"
    for bad in (esf_dgram(b"Ab"), esf_dgram(b"Zec1on"), esf_dgram(b"A" * 21), esf_dgram(b"Zecron", length=9),
                esf_dgram(b"Zecron", src=CLIENT), esf_dgram(b"Zecron", varint=b"\x80\x80\x80\x80\x80\x80")):
        o = OwnNames(SERVER)
        o.feed(bad)
        assert o.as_dict() == {}, bad


# ---------------------------------------------------------------------------- groups from the real captures
def test_cap2_default_groups_per_fight(tmp_path):
    closed, src = run_capture(CAP2, tmp_path)
    assert [(f.combined_total) for f in closed] == [20670, 675]
    assert [g["name"] for g in closed[0].groups] == ["You"]            # Zecron's connection first appears later
    assert closed[0].groups == [{"name": "You", "key": "you", "player": "you", "pet": "pet"}]
    assert closed[1].groups == [{"name": "You", "key": "you", "player": "you", "pet": "pet"},
                                {"name": "Zecron", "key": "Zecron", "player": "Zecron", "pet": "Zecron's pet"}]   # keys = the label fixed at its first Event
    g = src.character_groups()
    assert [(x.name, x.port, x.player_key, x.pet_key) for x in g][0] == ("You", 21173, "you", "pet")
    assert g[1].name == "Zecron" and g[1].port == 20605 and g[1].entity_id == 0x1751CB82
    rows2 = {r.name: r for r in group_rows(closed[1])}
    assert (rows2["You"].combined_total, rows2["Zecron"].player.total, rows2["Zecron"].pet.total,
            rows2["Zecron"].combined_total) == (0, 0, 675, 675)
    assert rows2["Zecron"].pet_name == "Zecron's pet"
    r1 = group_rows(closed[0])
    assert len(r1) == 1 and r1[0].combined_total == 20670 and r1[0].pet_name == "Pet"
    assert [(c.name, c.total) for c in character_runs(closed)] == [("You", 20670), ("Zecron", 675), (ALL_CHARACTERS, 21345)]
    assert [(r.label, r.total) for r in display_rows(closed[1])][-1] == (ALL_CHARACTERS, 675)


def test_cap2_connection_20605_is_one_group_named_zecron(tmp_path):
    closed, src = run_capture(CAP2, tmp_path, local_port=20605)
    (f,) = closed
    assert f.groups == [{"name": "Zecron", "key": "you", "player": "you", "pet": "pet"}]
    (r,) = group_rows(f)
    assert (r.name, r.pet_name, r.player.total, r.pet.total, r.combined_total) == ("Zecron", "Zecron's pet", 0, 675, 675)
    assert [x.label for x in display_rows(f)] == ["Zecron", "Zecron's pet", "Zecron combined"]   # no All line
    runs = character_runs(closed)
    assert [(c.name, c.total) for c in runs] == [("Zecron", 675)]


def test_cap1_is_a_single_you_group(tmp_path):
    closed, src = run_capture(CAP1, tmp_path)
    (f,) = closed
    assert [g["name"] for g in f.groups] == ["You"]
    (r,) = group_rows(f)
    assert r.name == "You" and r.combined_total == 7218 and f.combined_total == 7218
    assert ALL_CHARACTERS not in [x.label for x in display_rows(f)]
    assert [(c.name, c.total) for c in character_runs(closed)] == [("You", 7218)]


def test_display_name_updates_live_but_keys_stay(tmp_path):
    closed, src = run_capture(CAP2, tmp_path)
    before = [g for g in src.character_groups()]
    src.own_names._names[20605] = "Renamed"          # a name arriving later changes the display only
    after = src.character_groups()
    assert after[1].name == "Renamed" and after[1].player_key == before[1].player_key and after[1].pet_key == before[1].pet_key


# ---------------------------------------------------------------------------- group_rows and stats
def two_group_fight():
    f = make_fight(4, you=0, pet=0)
    f.actors.clear()
    for k, t in (("Ann", 10), ("Ann's pet", 5), ("Bob", 7)):
        f.actors[k] = actor(k, t, 1, t, float(t))
    f.groups = [{"name": "Ann", "player": "Ann", "pet": "Ann's pet"}, {"name": "Bob", "player": "Bob", "pet": "Bob's pet"},
                {"name": "Cy", "player": "Cy", "pet": "Cy's pet"}]
    return f


def test_group_rows_pure():
    f = two_group_fight()
    rows = group_rows(f)
    assert [(r.name, r.player.total, r.pet.total, r.combined_total, r.combined_dps) for r in rows] == [
        ("Ann", 10, 5, 15, 3.0), ("Bob", 7, 0, 7, 1.4), ("Cy", 0, 0, 0, 0.0)]   # followed groups with no damage still show
    assert [r.pet_name for r in rows] == ["Ann's pet", "Bob's pet", "Cy's pet"]
    labels = [(r.label, r.kind) for r in display_rows(f)]
    assert labels[-1] == (ALL_CHARACTERS, "all") and ("Ann combined", "combined") in labels
    text = format_fight(f)
    assert "Ann's pet" in text and "All characters" in text


def test_group_rows_zero_duration_uses_effective_duration():
    f = make_fight(1, you=30, pet=0, duration=0.0)
    (r,) = group_rows(f)
    assert r.combined_dps == 30.0


def test_old_summary_without_groups_reloads_as_one_you_group():
    f = make_fight(1, you=60, pet=20)
    d = f.to_dict()
    d.pop("groups")                                  # a file written before Phase 4
    g = FightSummary.from_dict(d)
    assert g.groups is None
    (r,) = group_rows(g)
    assert (r.name, r.pet_name, r.player.total, r.pet.total, r.combined_total) == ("You", "Pet", 60, 20, 80)


def test_legacy_own_actors_without_groups_keep_the_other_characters():
    f = make_fight(2, you=0, pet=0)
    f.actors.clear()
    f.actors["Zecron's pet"] = actor("Zecron's pet", 675, 36, 25, 8.1)
    f.own_actors = ["you", "pet", "Zecron's pet"]
    rows = group_rows(f)
    assert [(r.name, r.combined_total) for r in rows] == [("You", 0), ("Zecron", 675)]


def test_groups_survive_save_and_reload(tmp_path):
    closed, src = run_capture(CAP2, tmp_path)
    store = RunStore(str(tmp_path / "runs"), "pcap:test")
    for f in closed:
        store.add_fight(f)
    again = RunStore(str(tmp_path / "runs"), "pcap:test2")
    prev = [p.summary for p in again.previous_fights]
    assert [p.groups for p in prev] == [f.groups for f in closed]
    doc = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert [g["name"] for g in doc["runs"][0]["fights"][1]["groups"]] == ["You", "Zecron"]
    assert [(c.name, c.total) for c in character_runs(prev)] == [("You", 20670), ("Zecron", 675), (ALL_CHARACTERS, 21345)]


def test_character_runs_dps_best_and_matching_by_name():
    a = make_fight(1, you=100, pet=0, duration=10.0)
    b = make_fight(2, you=90, pet=0, duration=3.0)
    c = make_fight(3, you=0, pet=0, duration=4.0)
    runs = character_runs([a, b, c])
    assert [(r.name, r.total, r.best_fight_id) for r in runs] == [("You", 190, 2)]
    assert round(runs[0].dps, 2) == round(190 / 13.0, 2)      # fights where it dealt no damage don't count
    run = summarize_run([a, b])
    assert [r.name for r in run.characters] == ["You"]
    out = format_run(run)
    assert "You" in out and "total    190" in out


def test_console_ui_prints_grouped_fight_and_run(tmp_path):
    import io
    closed, src = run_capture(CAP2, tmp_path)
    buf = io.StringIO()
    ui = ConsoleUI(out=buf)
    ui.on_closed(closed[1], summarize_run(closed))
    ui.on_finished(summarize_run(closed))
    text = buf.getvalue()
    for needle in ("Zecron's pet", "Zecron combined", "All characters"):
        assert needle in text
    assert "total  21345" in text
