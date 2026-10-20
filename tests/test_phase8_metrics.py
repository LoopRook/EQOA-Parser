"""Phase 8: damage taken, healing, kills / XP / tunar from system text (docs/CONTRACTS.md "Phase 8")."""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path

from _net_builders import CLIENT, SERVER, eth, hp_body, ip_udp, msg, payload, pcapng

from eqoa_parser.config import load_config
from eqoa_parser.events import Event
from eqoa_parser.net.systext import (
    Faction, Loot, MasteryXp, ProgressTracker, Tunar, XpGain, decode_text, parse_text,
)
from eqoa_parser.net.sources import PcapReplaySource
from eqoa_parser.persistence import RunStore
from eqoa_parser.pipeline import Engine
from eqoa_parser.segmenter import Fight
from eqoa_parser.stats import CharacterProgress, FightSummary, group_rows, summarize_fight, summarize_run
from eqoa_parser.ui.console import flow_lines, format_fight, format_run, progress_lines

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
P, D = 0x13FF89D8, 0x01312F0A
H = P & 0xFFFF


def sys_body(text: str) -> bytes:
    return b"\x7a\x0a" + struct.pack("<I", len(text)) + text.encode("utf-16-le")


def cfg(tmp_path, **kw):
    return load_config(unmatched_log=str(tmp_path / "u.log"), **kw)


# ---- system text ------------------------------------------------------------------------------------
def test_decode_text_length_prefix():
    body = sys_body("You receive 35 tunar.")
    assert body[2:6] == (21).to_bytes(4, "little") and decode_text(body) == "You receive 35 tunar."
    assert decode_text(body[:-2]) is None            # truncated
    assert decode_text(body + b"\x00\x00") is None   # trailing bytes
    assert decode_text(b"\x7a\x0a\x01") is None


def test_parse_each_kind():
    assert parse_text("You receive 1200 XP.") == XpGain(1200)
    assert parse_text("You receive 0 XP. 166253 was paid towards your debt of 31726780.") == XpGain(0, 166253, 31726780)
    assert parse_text("You received 166,253 class mastery XP.") == MasteryXp(166253)
    assert parse_text("You receive 35 tunar.") == Tunar(35)
    assert parse_text("You receive 13 tunar, your share of the group loot.") == Tunar(13)
    assert parse_text("+12 faction with Citizens of Freeport (+1)") == Faction("Citizens of Freeport", 12, 1)
    assert parse_text("1778 faction with Citizens of Freeport (+1)") == Faction("Citizens of Freeport", 1778, 1)
    assert parse_text("-2000 faction with Bandits of Tunaria (-10)") == Faction("Bandits of Tunaria", -2000, -10)
    assert parse_text("Zed loots Wolf Pelt") == Loot("Zed", "Wolf Pelt")


def test_unrelated_text_is_ignored():
    for t in ("You receive 50000 tunar in bank.", "Bob tells the group: hello", "Out of range.", "", "You receive XP."):
        assert parse_text(t) is None


def test_tracker_totals():
    t = ProgressTracker()
    for r in (XpGain(0, 100, 5000), XpGain(300), MasteryXp(50), Tunar(7), Loot("Zed", "x"), Faction("A", 5, 2),
              Faction("A", 7, 2)):
        t.add(1, r)
    x = t.by_conn[1]
    assert (x.kills, x.xp, x.debt, x.mastery, x.tunar, x.loot, x.factions) == (2, 300, 100, 50, 7, 1, {"A": 4})


# ---- through the packet path: kills per connection, re-sends dropped, per hour ---------------------------
def frame(ts, port, *messages, h16=H):
    return ts, eth(ip_udp(SERVER, 10070, CLIENT, port, payload(h16, *messages)))


def run_pcap(tmp_path, frames, **kw):
    p = tmp_path / "x.pcapng"
    p.write_bytes(pcapng(frames))
    c = cfg(tmp_path, **kw)
    e = Engine(c, PcapReplaySource(p, c))
    e.run()
    return e


def test_kill_markers_count_per_connection_and_per_hour(tmp_path):
    xp1 = msg(10, sys_body("You receive 1200 XP."))
    frames = [
        frame(100.0, 24159, msg(1, hp_body(P, -50, D))),
        frame(101.0, 24159, xp1),
        frame(101.1, 24159, xp1),                                    # server re-send: not a second kill
        frame(101.2, 24159, msg(11, sys_body("You receive 35 tunar."))),
        frame(101.3, 24159, msg(12, sys_body("You received 500 class mastery XP."))),
        frame(102.0, 24159, msg(13, sys_body("You receive 0 XP. 90 was paid towards your debt of 1000."))),
        frame(103.0, 24159, msg(14, sys_body("Bob tells the group: hi"))),
        frame(3700.0, 24159, msg(15, hp_body(P, -5, D))),
    ]
    e = run_pcap(tmp_path, frames)
    run = e.run_summary()
    (p,) = run.progress
    assert (p.kills, p.xp, p.mastery, p.debt, p.tunar) == (2, 1200, 500, 90, 35)
    assert run.elapsed == 3600.0
    text = progress_lines(run)[0]
    assert text == "Kills 2 (2/h), XP 1,200 (1,200/h), mastery 500 (500/h), tunar 35 (35/h), debt paid 90"


def test_progress_of_a_muted_connection_is_ignored(tmp_path):
    frames = [
        frame(100.0, 24159, msg(1, hp_body(P, -50, D))),
        frame(101.0, 24159, msg(2, sys_body("You receive 10 XP."))),
        frame(101.0, 30000, msg(2, sys_body("You receive 10 XP."))),
    ]
    assert [p.kills for p in run_pcap(tmp_path, frames).run_summary().progress] == [1, 1]   # both followed
    p = tmp_path / "y.pcapng"
    p.write_bytes(pcapng(frames))
    c = cfg(tmp_path)
    c.net.local_port = 24159
    e = Engine(c, PcapReplaySource(p, c))
    e.run()
    assert [(r.key, r.kills) for r in e.run_summary().progress] == [("you", 1)]


def test_no_fights_still_reports_progress(tmp_path):
    frames = [frame(100.0, 24159, msg(1, sys_body("You receive 10 XP.")))]
    run = run_pcap(tmp_path, frames).run_summary()
    assert run.fight_count == 0 and run.progress[0].kills == 1
    assert "Kills 1" in format_run(run)


# ---- cap2: damage taken and healing received -----------------------------------------------------------
def test_cap2_damage_taken_and_healing_received(tmp_path):
    c = cfg(tmp_path)
    c.net.local_port = 21173
    e = Engine(c, PcapReplaySource(REAL / "cap2_bard_melee_necro_pet.pcapng", c))
    e.run()
    (f,) = e.closed_fights
    (g,) = group_rows(f)
    assert (g.taken_total, g.taken_hits, g.taken_max) == (1176, 56, 21)
    assert g.taken_sources == [("0x01312F0A", 1176, 56)]
    assert g.dtps == 1176 / f.duration
    assert (g.heal_done, g.heal_recv) == (0, 16800)
    assert g.recv_sources == [("0x01312F0A", 16800)]
    lines = flow_lines(f)
    assert lines[0].startswith("Taken: 1176 (DTPS ") and "biggest hit 21, top: 0x01312F0A 1176" in lines[0]
    assert lines[1].startswith("Healing: done 0 (HPS 0.0), received 16800")
    assert "Taken: 1176" in format_fight(f)


# ---- stats on hand-built events -----------------------------------------------------------------------
def ev(ts, actor, target, amount, kind):
    return Event(ts, actor, target, amount, None, kind, "")


def test_pet_as_target_and_heal_done_by_pet():
    fight = Fight(1, 0.0, 4.0, [
        ev(0.0, "orc", "you", 10, "damage_taken"), ev(1.0, "orc", "pet", 30, "damage_taken"),
        ev(2.0, "gnoll", "you", 20, "damage_taken"), ev(3.0, "you", "orc", 5, "damage"),
        ev(3.5, "pet", "you", 40, "heal"), ev(4.0, "you", "you", 60, "heal"), ev(4.0, "you", "Bob", 25, "heal"),
    ])
    f = summarize_fight(fight)
    (g,) = group_rows(f)
    assert (g.taken_total, g.taken_hits, g.taken_max) == (60, 3, 30)
    assert g.taken_sources == [("orc", 40, 2), ("gnoll", 20, 1)]
    assert g.dtps == 15.0
    assert (g.heal_done, g.heal_recv, g.hps) == (125, 100, 125 / 4.0)
    assert g.recv_sources == [("you", 60), ("pet", 40)]


def test_round_trip_and_old_dicts_without_new_fields():
    fight = Fight(1, 0.0, 2.0, [ev(0.0, "orc", "you", 10, "damage_taken"), ev(1.0, "you", "orc", 5, "damage"),
                                ev(2.0, "x", "you", 7, "heal")])
    f = summarize_fight(fight)
    assert FightSummary.from_dict(json.loads(json.dumps(f.to_dict()))) == f
    d = f.to_dict()
    del d["taken"], d["heals"]
    old = FightSummary.from_dict(d)
    assert old.taken is None and old.heals is None
    assert (group_rows(old)[0].taken_total, group_rows(old)[0].heal_recv) == (0, 0)
    assert flow_lines(old) == []
    plain = summarize_fight(Fight(1, 0.0, 1.0, [ev(0.0, "you", "orc", 5, "damage")])).to_dict()
    assert "taken" not in plain and "heals" not in plain


def test_run_store_keeps_progress_and_old_files_load(tmp_path):
    now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    old = {"schema_version": 1, "date": "2026-10-19", "runs": [{"run_id": "r0", "started_at": "x", "source": "s",
                                                                "fights": []}]}
    (tmp_path / "2026-10-19.json").write_text(json.dumps(old), encoding="utf-8")
    store = RunStore(tmp_path, "pcap:x", now=now, warn=lambda m: None)
    assert not store.set_progress([CharacterProgress("you", "You")], 10.0)   # nothing to keep: no entry
    assert store.set_progress([CharacterProgress("you", "You", kills=3, xp=9, tunar=4)], 123.46)
    store.save()
    doc = json.loads((tmp_path / "2026-10-19.json").read_text(encoding="utf-8"))
    assert "progress" not in doc["runs"][0]
    assert doc["runs"][1]["progress"]["elapsed"] == 123.5
    assert doc["runs"][1]["progress"]["characters"][0]["kills"] == 3
    again = RunStore(tmp_path, "pcap:y", now=now, warn=lambda m: None)
    assert again.previous_fights == []


def test_run_summary_without_progress_unchanged():
    s = summarize_run([])
    assert s.progress == [] and s.elapsed == 0.0
    assert format_run(s).endswith("No fights.")
