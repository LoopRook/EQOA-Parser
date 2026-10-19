"""Regression test on a real capture that separates a caster's own spells from its pet's damage.

fixtures/real/cap5_mage_spells_then_pet.pcapng (server->client only, scrubbed with tools/sanitize_pcapng.py), 2026-10-19:
the magician Maige (0x1C08FB90, client port 9032) first casts spells at a training dummy with the pet backed off
(user-observed popups 1064 1219 1219 1220, one 5xx, one 3xx, then a stray 1220; a 1072 followed), then lets the pet
fight alone without casting. Confirms: the player's own spells are plain db-00 records; every pet action, including
melee, pet abilities and 1-damage procs, is an extended record with the red trailer ff 00 00, credited to the owner.
"""
from __future__ import annotations

from pathlib import Path

from eqoa_parser.config import load_config
from eqoa_parser.net.pcapng import read_pcapng
from eqoa_parser.net.protocol import HpChange, PacketDecoder
from eqoa_parser.net.sources import PcapReplaySource
from eqoa_parser.pipeline import Engine

ROOT = Path(__file__).resolve().parent.parent
CAP5 = ROOT / "fixtures" / "real" / "cap5_mage_spells_then_pet.pcapng"
SERVER = "5.161.217.188"
MAIGE = 0x1C08FB90
SPELLS = [1064, 1219, 1219, 1220, 575, 384, 1220, 1072]


def _maige_damage():
    dec = PacketDecoder(SERVER)
    recs = [r for d in read_pcapng(CAP5) for r in dec.feed(d)]
    return [r for r in recs if isinstance(r, HpChange) and r.source == MAIGE and r.amount < 0]


def test_own_spells_are_plain_records_in_cast_order():
    plain = [-r.amount for r in _maige_damage() if not r.extended]
    assert plain == SPELLS


def test_every_pet_record_carries_the_red_trailer():
    ext = [r for r in _maige_damage() if r.extended]
    assert len(ext) == 36
    assert {r.raw[-3:] for r in ext} == {b"\xff\x00\x00"}
    assert sum(-r.amount for r in ext) == 28566


def test_meter_splits_spells_and_pet(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
    cfg.net.local_port = 9032
    closed = []
    Engine(cfg, PcapReplaySource(CAP5, cfg, speed=0), on_closed=closed.append).run()
    assert sum(f.you.total for f in closed) == sum(SPELLS) == 7973
    assert sum(f.pet.total for f in closed) == 28566
    pet_fight = max(closed, key=lambda f: f.pet.total)
    assert pet_fight.you.total == 0          # the caster didn't cast during the pet-only part
