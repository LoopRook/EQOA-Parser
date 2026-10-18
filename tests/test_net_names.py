"""Entity-name table tests (session B). INFERRED heuristic; oracle from fixtures/real/expected_names.json."""
from __future__ import annotations

import json
import struct
from pathlib import Path

from _net_builders import CLIENT, SERVER, hp_body, msg, payload

from eqoa_parser.config import load_config
from eqoa_parser.events import KIND_DAMAGE
from eqoa_parser.net.names import NameTable
from eqoa_parser.net.pcapng import UdpDatagram, read_pcapng
from eqoa_parser.net.sources import PcapReplaySource
from eqoa_parser.pipeline import Engine

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
EXPECTED = json.loads((REAL / "expected_names.json").read_text(encoding="utf-8"))["names"]
P, MOB, PET = 0x13FF89D8, 0x01312F0A, 0x01374606
H = P & 0xFFFF


def spawn(entity, name: str, filler=b"\x11" * 7) -> bytes:
    nb = name.encode()
    return struct.pack("<I", entity) + b"\x82\x31" + filler + bytes([0x80 | len(nb), 4]) + nb


def dg(data, ts=1.0, src=SERVER, port=24159):
    return UdpDatagram(ts, src, 10070, CLIENT, port, data)


def test_both_real_captures_reproduce_expected_names_exactly():
    t = NameTable("5.161.217.188")
    for f in ("cap1_necro_spells.pcapng", "cap2_bard_melee_necro_pet.pcapng"):
        for d in read_pcapng(REAL / f):
            t.feed(d)
    assert {"0x%08X" % k: v for k, v in t.as_dict().items()} == EXPECTED
    assert t.get(0x01312F0A) is None  # the dummy has no name in the captures


def test_hand_built_name_extraction_rules():
    t = NameTable(SERVER)
    t.feed(dg(b"\x00\x01" + spawn(0x1234, "Rotting Flesh ") + b"\xde\xad\xbe\xef"))
    assert t.get(0x1234) == "Rotting Flesh"
    t.feed(dg(b"\x00\x01" + spawn(0x1234, "Bat") + b"\xde\xad\xbe\xef"))
    assert t.get(0x1234) == "Bat"                       # later name replaces earlier


def test_rejects_non_names_far_markers_and_wrong_source():
    t = NameTable(SERVER)
    t.feed(dg(b"\x00\x01" + spawn(1, "A1b2c3") + b"xxxx"))            # digits: not a name
    t.feed(dg(b"\x00\x01" + spawn(2, "Ab") + b"xxxx"))                # too short
    far = struct.pack("<I", 3) + b"\x82\x31" + b"\x11" * 130 + bytes([0x83, 4]) + b"Bat"
    t.feed(dg(b"\x00\x01" + far + b"xxxx"))                           # marker > 120 bytes back
    t.feed(dg(b"\x00\x01" + spawn(4, "Bat") + b"xxxx", src=CLIENT))   # not from the server
    t.feed(dg(b"\x00\x01" + bytes([0x83, 4]) + b"Bat" + b"xxxx"))     # no marker before
    t.feed(dg(b""))
    assert t.as_dict() == {}


def test_name_arriving_after_first_hit_leaves_hex_then_name(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    hit1 = dg(payload(H, msg(1, hp_body(P, -10, MOB))), ts=1.0)
    named = dg(payload(H, spawn(MOB, "Sidewinder")), ts=2.0)
    hit2 = dg(payload(H, msg(2, hp_body(P, -20, MOB))), ts=3.0)

    class Src(PcapReplaySource):
        def __init__(self):
            from eqoa_parser.net.sources import PacketSource
            PacketSource.__init__(self, cfg)

        def __iter__(self):
            return self._events([hit1, named, hit2])

    closed = []
    Engine(cfg, Src(), on_closed=closed.append).run()
    (f,) = closed
    assert f.targets == ["0x01312F0A", "Sidewinder"] and f.you.total == 30 and f.you.hits == 2
    assert f.target == "Sidewinder"


def test_you_and_pet_are_never_renamed():
    cfg = load_config()
    cfg.net.local_port = 24159
    from eqoa_parser.net.sources import PacketSource

    class S(PacketSource):
        pass

    s = S(cfg)
    s.events_from(dg(payload(H, spawn(P, "Zecron") + spawn(PET, "Wilting Flesh") + msg(1, hp_body(P, -5, MOB)))))
    evs = s.events_from(dg(payload(H, msg(2, hp_body(P, -25, MOB, b"\x00")))))
    assert evs[0].actor == "pet" and s.names.get(P) == "Zecron"
    evs = s.events_from(dg(payload(H, msg(3, hp_body(MOB, -4, P)))))
    assert (evs[0].target, evs[0].kind) == ("you", "damage_taken")


def test_cap2_necro_connection_counts_unchanged_with_names(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    cfg.net.local_port = 20605
    src = PcapReplaySource(REAL / "cap2_bard_melee_necro_pet.pcapng", cfg)
    evs = [e for _, e in src if not isinstance(e, str)]
    assert len(evs) == 36 and {e.actor for e in evs} == {"pet"} and {e.kind for e in evs} == {KIND_DAMAGE}
    assert {e.target for e in evs} == {"0x01312F0A"}
    assert sum(e.amount == 25 for e in evs) == 27 and sum(e.amount == 0 for e in evs) == 9
