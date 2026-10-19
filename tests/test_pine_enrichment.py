"""Phase 5: optional PINE enrichment (net/memory.py, router group members, CLI). Mock PINE with a 32 MB image."""
from __future__ import annotations

import io
import struct
from pathlib import Path

import pytest

from eqoa_parser import app
from eqoa_parser.config import PineConfig, load_config
from eqoa_parser.mock_pine import EE_RAM_SIZE, MockPineServer
from eqoa_parser.net import memory as M
from eqoa_parser.net.attribution import ConnectionRouter
from eqoa_parser.net.memory import GameMemory, MemoryEnricher, MemorySnapshot
from eqoa_parser.net.protocol import HpChange
from eqoa_parser.net.sources import PcapReplaySource
from eqoa_parser.pine import PineClient

ROOT = Path(__file__).resolve().parent.parent
CELL = 0x500000
ZAP, MAIGE, GNARL, PET, MOB = 0x1C0A58E6, 0x1C08FB90, 0x1C0B1234, 0x1C0B9999, 0x01312F0A


def build_image(*, members=((ZAP, "Zaptir"), (MAIGE, "Maige")), in_group=1, count=None, entities=None) -> bytearray:
    ram = bytearray(EE_RAM_SIZE)
    ram[M.OWN_NAME_ADDR:M.OWN_NAME_ADDR + 7] = b"Zaptir\x00"
    struct.pack_into("<I", ram, M.CELL_ADDR, CELL)
    struct.pack_into("<I", ram, CELL + M.OWN_ID_OFF, ZAP)
    struct.pack_into("<I", ram, CELL + M.PET_ID_OFF, PET)
    struct.pack_into("<I", ram, CELL + M.IN_GROUP_OFF, in_group)
    struct.pack_into("<I", ram, CELL + M.GROUP_COUNT_OFF, len(members) if count is None else count)
    for i, (mid, name) in enumerate(members):
        base = CELL + M.GROUP_MEMBERS_OFF + i * M.GROUP_MEMBER_SIZE
        struct.pack_into("<I", ram, base, mid)
        ram[base + M.GROUP_NAME_OFF:base + M.GROUP_NAME_OFF + len(name) + 1] = name.encode() + b"\x00"
    if entities is None:
        entities = [(ZAP, "Zaptir", 20, 2), (PET, "Zaptir`s pet", 5, 3), (MOB, "a gnoll", 12, 1)]
    slot = 0
    for eid, name, level, cls in entities:
        ptr = 0x600000 + slot * 0x400
        struct.pack_into("<I", ram, M.ENTITY_LIST_ADDR + 4 * slot, ptr)
        struct.pack_into("<I", ram, ptr + M.ENT_ID, eid)
        ram[ptr + M.ENT_NAME:ptr + M.ENT_NAME + len(name) + 1] = name.encode() + b"\x00"
        ram[ptr + M.ENT_LEVEL] = level
        ram[ptr + M.ENT_CLASS] = cls
        slot += 1
    return ram


@pytest.fixture
def server():
    with MockPineServer(build_image()) as s:
        yield s


def gm(server) -> GameMemory:
    return GameMemory(PineClient(PineConfig(slot=server.tcp_port, timeout=3.0), platform="win32"))


# ---- GameMemory -------------------------------------------------------------------------------------------------
def test_game_memory_reads_structures(server):
    mem = gm(server)
    assert mem.own_name() == "Zaptir"
    assert mem.own_id() == ZAP and mem.pet_id() == PET
    assert mem.group() == (True, [(ZAP, "Zaptir"), (MAIGE, "Maige")])
    assert mem.entities() == [(ZAP, "Zaptir", 20, "player"), (PET, "Zaptir`s pet", 5, "pet"), (MOB, "a gnoll", 12, "npc")]
    mem.client.close()


def test_game_memory_skips_junk_and_empty_slots():
    ents = [(ZAP, "Zaptir", 20, 2), (0x11, "Empty", 0, 2), (0x12, "Bad\x01name", 9, 2), (0x13, "", 9, 2)]
    ram = build_image(entities=ents)
    struct.pack_into("<I", ram, M.ENTITY_LIST_ADDR + 4 * 10, 0xFFFFFFF0)  # pointer outside RAM
    with MockPineServer(ram) as s:
        mem = gm(s)
        assert mem.entities() == [(ZAP, "Zaptir", 20, "player")]
        mem.client.close()


@pytest.mark.parametrize("kw", [dict(in_group=0), dict(count=9), dict(count=0)])
def test_group_junk_is_empty(kw):
    with MockPineServer(build_image(**kw)) as s:
        mem = gm(s)
        assert mem.group() == (False, [])
        mem.client.close()


def test_group_skips_member_with_junk_name():
    with MockPineServer(build_image(members=((ZAP, "Zaptir"), (MAIGE, "Ma\x02ge")))) as s:
        mem = gm(s)
        assert mem.group() == (True, [(ZAP, "Zaptir")])
        mem.client.close()


def test_junk_cell_gives_nothing():
    ram = build_image()
    struct.pack_into("<I", ram, M.CELL_ADDR, 0x7FFFFFFF)
    with MockPineServer(ram) as s:
        mem = gm(s)
        assert mem.own_id() is None and mem.pet_id() is None and mem.group() == (False, [])
        mem.client.close()


def test_memory_module_has_no_write_calls():
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert ".write" not in src and "OP_WRITE" not in src and not hasattr(PineClient, "write32")


# ---- MemoryEnricher ---------------------------------------------------------------------------------------------
def test_enricher_status_and_snapshot(server):
    e = MemoryEnricher(PineConfig(slot=server.tcp_port, timeout=3.0))
    assert e.status_text() == "PINE: not reachable"  # nothing polled yet
    assert e.poll_once()
    s = e.snapshot()
    assert s.ok and s.own_id == ZAP and s.in_group and len(s.members) == 2
    assert e.status_text() == f"PINE: on (slot {server.tcp_port}), group of 2"
    v = s.version
    assert e.poll_once() and e.snapshot().version == v  # unchanged content: same version
    e.close()


def test_enricher_survives_pine_down_and_up():
    server = MockPineServer(build_image()).start()
    port = server.tcp_port
    e = MemoryEnricher(PineConfig(slot=port, timeout=0.5))
    assert e.poll_once()
    server.stop()
    assert not e.poll_once()
    snap = e.snapshot()
    assert not snap.ok and snap.error and e.status_text() == "PINE: not reachable"
    assert snap.members  # last known group is kept
    server2 = MockPineServer(build_image(), port=port).start()
    try:
        assert e.poll_once() and e.snapshot().ok
    finally:
        e.close()
        server2.stop()


def test_enricher_thread_never_raises_when_unreachable():
    e = MemoryEnricher(PineConfig(slot=1, timeout=0.2), interval=0.05, retry=0.05).start()
    import time
    time.sleep(0.3)
    assert not e.snapshot().ok
    e.close()


# ---- router integration -----------------------------------------------------------------------------------------
class FakeMemory:
    def __init__(self, members=((ZAP, "Zaptir"), (GNARL, "Gnarl")), own_id=ZAP, entities=()):
        self.snap = MemorySnapshot(version=1, ok=True, own_name="Zaptir", own_id=own_id, entities=tuple(entities),
                                   in_group=True, members=tuple(members))

    def snapshot(self):
        return self.snap


def hp(ts, src, amt, tgt, conn=1, ext=False, own=ZAP):
    return HpChange(ts, conn, own & 0xFFFF, 0, src, amt, tgt, ext, b"\xdb\x00")


def make(memory):
    from eqoa_parser.net.names import NameTable
    return ConnectionRouter(load_config(), log=io.StringIO(), names=NameTable(), memory=memory)


def test_member_damage_counted_under_name_and_taken():
    r = make(FakeMemory())
    ev = r.feed(hp(0.0, ZAP, -50, MOB)) + r.feed(hp(1.0, GNARL, -30, MOB)) + r.feed(hp(2.0, MOB, -7, GNARL))
    assert [(e.actor, e.target, e.amount, e.kind) for e in ev] == [
        ("you", "0x01312F0A", 50, "damage"), ("Gnarl", "0x01312F0A", 30, "damage"),
        ("0x01312F0A", "Gnarl", 7, "damage_taken")]
    assert r.own_actors() == ["you", "Gnarl"]
    names = [(g.name, g.player_key, g.pet_key) for g in r.character_groups()]
    assert names == [("Zaptir", "you", "pet"), ("Gnarl", "Gnarl", "Gnarl's pet")]


def test_member_extended_record_is_pet():
    r = make(FakeMemory())
    r.feed(hp(0.0, ZAP, -1, MOB))
    ev = r.feed(hp(1.0, GNARL, -20, MOB, ext=True))
    assert [(e.actor, e.amount) for e in ev] == [("Gnarl's pet", 20)]


def test_unrelated_non_member_still_dropped():
    r = make(FakeMemory())
    r.feed(hp(0.0, ZAP, -1, MOB))
    assert r.feed(hp(1.0, 0x1DEAD, -20, MOB)) == []
    assert r.attributor(1).dropped_unrelated == 1


def test_member_that_is_a_followed_connection_counts_once():
    a, b = ZAP, MAIGE
    r = make(FakeMemory(members=((a, "Zaptir"), (b, "Maige"), (GNARL, "Gnarl"))))
    r.feed(hp(0.0, a, -1, MOB, conn=1, own=a))
    r.feed(hp(0.0, b, -1, MOB, conn=2, own=b))
    out = []
    # Maige hits the mob: conn 2 (her own) and conn 1 (Zaptir's client sees the group fight)
    out += r.feed(hp(1.0, b, -40, MOB, conn=2, own=b))
    out += r.feed(hp(1.0, b, -40, MOB, conn=1, own=a))
    assert [(e.actor, e.amount) for e in out] == [("Maige", 40)]  # conn 2's own count only
    assert not any(g.player_key == "Maige" and g.port == 0 for g in r.character_groups())
    assert [g.name for g in r.character_groups()] == ["Zaptir", "Maige"]


def test_groupmate_seen_by_two_connections_counts_once():
    r = make(FakeMemory(members=((ZAP, "Zaptir"), (MAIGE, "Maige"), (GNARL, "Gnarl"))))
    r.feed(hp(0.0, ZAP, -1, MOB, conn=1, own=ZAP))
    r.feed(hp(0.0, MAIGE, -1, MOB, conn=2, own=MAIGE))
    out = r.feed(hp(1.0, GNARL, -25, MOB, conn=1, own=ZAP)) + r.feed(hp(1.0, GNARL, -25, MOB, conn=2, own=MAIGE))
    assert [(e.actor, e.amount) for e in out] == [("Gnarl", 25)]  # fed by the own-id connection (conn 1)


def test_memory_names_feed_name_table_and_display_name():
    ents = [(ZAP, "Zaptir", 20, 2), (MOB, "a gnoll", 12, 1)]
    r = make(FakeMemory(entities=ents))
    ev = r.feed(hp(0.0, ZAP, -9, MOB))
    assert ev[0].target == "a gnoll"
    assert r.names.get(ZAP) == "Zaptir"
    assert r.character_groups()[0].name == "Zaptir"


def test_memory_name_wins_over_packet_name():
    r = make(FakeMemory())
    r.feed(hp(0.0, ZAP, -1, MOB))
    r.names._names[ZAP] = "Zapt"  # a packet-derived fragment
    assert r.character_groups()[0].name == "Zaptir"


def test_snapshot_updates_are_picked_up_live():
    mem = FakeMemory(members=((ZAP, "Zaptir"),))
    r = make(mem)
    assert r.feed(hp(0.0, ZAP, -1, MOB)) and r.feed(hp(1.0, GNARL, -5, MOB)) == []
    mem.snap = MemorySnapshot(version=2, ok=True, own_id=ZAP, in_group=True, members=((ZAP, "Zaptir"), (GNARL, "Gnarl")))
    assert [e.actor for e in r.feed(hp(2.0, GNARL, -5, MOB))] == ["Gnarl"]


# ---- PINE off is identical to today -----------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["cap1_necro_spells", "cap2_bard_melee_necro_pet", "cap5_mage_spells_then_pet"])
def test_pine_off_and_empty_memory_give_identical_events(name):
    path = ROOT / "fixtures" / "real" / f"{name}.pcapng"
    cfg = load_config()

    def run(memory):
        src = PcapReplaySource(path, cfg, speed=0.0, memory=memory)
        events = [ev for _ts, ev in src if not isinstance(ev, str)]
        return events, src.own_actors(), [(g.name, g.player_key, g.pet_key) for g in src.character_groups()]

    off = run(None)
    empty = run(FakeMemory(members=(), own_id=None))
    assert off == empty
    assert off[0]  # the capture does contain events


# ---- CLI / config -----------------------------------------------------------------------------------------------
def test_pine_defaults_off_and_cli_flags():
    assert PineConfig().enabled is False
    args = app.parse_args(["--pcap", "x.pcapng", "--pine", "--pine-slot", "28012"])
    cfg = app.make_config(args)
    assert cfg.pine.enabled and cfg.pine.slot == 28012
    assert not app.make_config(app.parse_args(["--pcap", "x.pcapng"])).pine.enabled


def test_pine_slot_needs_pine():
    with pytest.raises(SystemExit):
        app.parse_args(["--pcap", "x.pcapng", "--pine-slot", "28012"])


def test_config_validates_enabled_type(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("pine:\n  enabled: yes please\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)
    p.write_text("pine:\n  enabled: true\n  slot: 28012\n", encoding="utf-8")
    assert load_config(p).pine.enabled


def test_make_source_without_pine_has_no_memory():
    args = app.parse_args(["--pcap", str(ROOT / "fixtures/real/cap5_mage_spells_then_pet.pcapng"), "--no-gui"])
    src, _ = app.make_source(args, app.make_config(args))
    assert src.memory is None and src.pine_status() == "PINE: off"
