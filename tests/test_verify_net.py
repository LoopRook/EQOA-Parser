"""Independent verification, Phase 2 (session D): packet path attacked with hand-built bytes.

Covers net/pcapng.py, net/protocol.py, net/attribution.py, net/sources.py and the heal/Event plumbing.  The builders
below are written from docs/CONTRACTS.md ("Phase 2: packet source"), NOT imported from the implementers' tests.
Everything is SYNTHETIC unless it reads fixtures/real/*.  Tests whose docstring starts "BUG-N2-x" assert the required /
robust behaviour and are EXPECTED TO FAIL against the current code (real findings in docs/VERIFICATION.md, "Phase 2").
"LIMIT" / "RISK" tests pin down documented or inherent properties and pass.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from eqoa_parser.config import load_config
from eqoa_parser.events import ACTIVITY_KINDS, KIND_DAMAGE, KIND_DAMAGE_TAKEN, KIND_HEAL, Event
from eqoa_parser.net.attribution import Attributor, ConnectionRouter
from eqoa_parser.net.pcapng import UdpDatagram, iter_raw_datagrams, read_pcapng
from eqoa_parser.net.protocol import HpChange, PacketDecoder, Swing, decode_datagram, read_varint, zigzag_decode
from eqoa_parser.net.sources import LivePacketSource, PacketSource, PcapReplaySource
from eqoa_parser.pipeline import Engine
from eqoa_parser.segmenter import Segmenter
from eqoa_parser.sources import HEARTBEAT
from eqoa_parser.stats import summarize_fight

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "fixtures" / "real"
SERVER = "5.161.217.188"
CLIENT = "192.168.1.156"
PLAYER = 0x174A1A40            # low16 = 0x1A40
H16 = PLAYER & 0xFFFF
PET = 0x01374606
MOB = 0x01312F0A
MOB2 = 0x0155AA01
BASE = 1_790_000_000.0


# ----------------------------------------------------------------------------------------------- byte builders
def zz(n: int) -> int:
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def vi(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def hp(source: int, amount: int, target: int, extra: bytes = b"") -> bytes:
    return b"\xdb\x00" + struct.pack("<I", source) + vi(zz(amount)) + struct.pack("<I", target) + extra


def swing(att: int, tgt: int, extra: int = 768000, tail: bytes = b"\x00") -> bytes:
    return b"\xb1\x00" + struct.pack("<II", att, tgt) + struct.pack("<i", extra) + tail


def msg(seq: int, body: bytes) -> bytes:
    assert len(body) < 256
    return bytes([0xFB, len(body)]) + struct.pack("<H", seq) + body


def payload(h16: int, *msgs: bytes, prefix: bytes = b"\x01\x02\x03", checksum: bytes = b"\xaa\xbb\xcc\xdd") -> bytes:
    return struct.pack("<H", h16) + prefix + b"".join(msgs) + checksum


def dg(ts: float, pl: bytes, port: int = 20000, src: str = SERVER) -> UdpDatagram:
    return UdpDatagram(ts, src, 10070, CLIENT, port, pl)


def ip_udp(src, sport, dst, dport, data, *, ident=1, flags_frag=0, options=b"", ulen=None, total=None, proto=17) -> bytes:
    ihl = 5 + len(options) // 4
    udp = struct.pack(">HHHH", sport, dport, 8 + len(data) if ulen is None else ulen, 0) + data
    tl = 4 * ihl + len(udp) if total is None else total
    hdr = struct.pack(">BBHHHBBH4s4s", 0x40 | ihl, 0, tl, ident, flags_frag, 64, proto, 0, socket.inet_aton(src),
                      socket.inet_aton(dst)) + options
    return hdr + udp


def eth(ip: bytes, ethertype: int = 0x0800, vlans: int = 0, pad: int = 0) -> bytes:
    f = b"\x00" * 12
    for _ in range(vlans):
        f += b"\x81\x00\x00\x05"
    return f + struct.pack(">H", ethertype) + ip + b"\x00" * pad


def block(btype: int, body: bytes, e: str = "<", length: int | None = None, trailer: int | None = None) -> bytes:
    body += b"\x00" * (-len(body) % 4)
    total = 12 + len(body)
    return struct.pack(e + "II", btype, total if length is None else length) + body + \
        struct.pack(e + "I", total if trailer is None else trailer)


def shb(e: str = "<") -> bytes:
    return block(0x0A0D0D0A, struct.pack(e + "IHHq", 0x1A2B3C4D, 1, 0, -1), e)


def idb(link: int = 1, e: str = "<", opts: bytes = b"") -> bytes:
    return block(1, struct.pack(e + "HHI", link, 0, 0) + opts, e)


def tsresol_opt(v: int, e: str = "<") -> bytes:
    return struct.pack(e + "HH", 9, 1) + bytes([v]) + b"\x00\x00\x00" + struct.pack(e + "HH", 0, 0)


def epb(ts_ticks: int, frame: bytes, e: str = "<", iface: int = 0, caplen: int | None = None, orig: int | None = None) -> bytes:
    cl = len(frame) if caplen is None else caplen
    body = struct.pack(e + "IIIII", iface, ts_ticks >> 32, ts_ticks & 0xFFFFFFFF, cl, len(frame) if orig is None else orig) + frame
    return block(6, body, e)


def pcap_bytes(frames, link: int = 1, e: str = "<", res: float = 1e-6, opts: bytes = b"") -> bytes:
    out = shb(e) + idb(link, e, opts)
    for ts, f in frames:
        out += epb(round(ts / res), f, e)
    return out


def write(tmp_path, data: bytes, name="x.pcapng") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def server_frame(pl: bytes, port: int = 20000) -> bytes:
    return eth(ip_udp(SERVER, 10070, CLIENT, port, pl))


def cfg_net(**kw):
    c = load_config()
    for k, v in kw.items():
        setattr(c.net, k, v)
    return c


def feed_all(dgrams, cfg=None):
    """datagrams -> events through PacketSource (decoder + router), like the sources do."""
    src = PacketSource(cfg or load_config())
    out: list[Event] = []
    for d in dgrams:
        out.extend(src.events_from(d))
    return out, src


# ============================================================================== real captures vs the oracle
def _oracle():
    return json.loads((REAL / "expected_records.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("cap", ["cap1_necro_spells.pcapng", "cap2_bard_melee_necro_pet.pcapng"])
def test_decoder_reproduces_oracle_exactly_with_no_extra_records(cap):
    exp = _oracle()["captures"][cap]["hp_records"]
    dec = PacketDecoder(SERVER)
    got = []
    for d in read_pcapng(REAL / cap):
        got.extend(r for r in dec.feed(d) if isinstance(r, HpChange))
    assert [(r.conn, r.header_low16, r.seq, r.source, r.amount, r.target, r.extended) for r in got] == \
           [(e["local_port"], e["header_low16"], e["seq"], int(e["source"], 16), e["amount"], int(e["target"], 16), e["extended"])
            for e in exp]
    # event times: offsets from the first datagram, within 1 ms of the oracle's
    t0 = next(iter(read_pcapng(REAL / cap))).ts
    assert all(abs((g.ts - t0) - e["t"]) < 0.002 for g, e in zip(got, exp))


def test_real_capture_attribution_matches_contract_numbers():
    """PHASE 3 contract: the default follows every connection.  cap2 -> the bard (first to fight) is `you` with 56 hits / 20,670;
    the necro connection is a non-primary character: its pet has 27 x 25 + 9 x 0 = 675 over 36 hits, labelled with the character
    name when known at its first Event (the NameTable knows 'Zecron' there), never `you`/`pet`.  `--connection 20605` keeps the
    old single-connection result (necro = you, pet = pet)."""
    cfg = load_config()
    src = PcapReplaySource(REAL / "cap2_bard_melee_necro_pet.pcapng", cfg, speed=0)
    evs = [e for _, e in src]
    assert src.router.selected == 21173
    bard = [e for e in evs if e.actor == "you" or e.target == "you" or e.kind == "heal" and e.target == "you"]
    kinds = [e.kind for e in bard]
    assert kinds.count("damage") == 56 and kinds.count("damage_taken") == 56 and kinds.count("heal") == 56
    assert {e.actor for e in evs if e.kind == "damage" and e.actor in ("you", "pet")} == {"you"}
    assert sum(e.amount for e in evs if e.kind == "damage" and e.actor == "you") == 20670
    assert {e.amount for e in evs if e.kind == "damage_taken"} == {21} and {e.amount for e in evs if e.kind == "heal"} == {300}
    necro = [e for e in evs if e.kind == "damage" and e.actor not in ("you", "pet")]
    label = src.router.attributor(20605).label
    assert label and label != "you" and not label.startswith("0x")
    assert len(necro) == 36 and {e.actor for e in necro} == {f"{label}'s pet"} and sum(e.amount for e in necro) == 675
    assert [e.amount for e in necro].count(25) == 27 and [e.amount for e in necro].count(0) == 9
    assert src.own_actors() == ["you", f"{label}'s pet"] and src.router.attributor(20605).pet_id == PET
    for port, (n25, n0) in ((20605, (27, 9)),):
        s = PcapReplaySource(REAL / "cap2_bard_melee_necro_pet.pcapng", cfg_net(local_port=port), speed=0)
        ev = [e for _, e in s]
        d = [e for e in ev if e.kind == "damage"]
        assert [e.amount for e in d].count(25) == n25 and [e.amount for e in d].count(0) == n0 and {e.actor for e in d} == {"pet"}
        assert s.router.attributor(port).pet_id == PET and all(e.kind != "damage_taken" for e in ev)
    s1 = PcapReplaySource(REAL / "cap1_necro_spells.pcapng", cfg, speed=0)
    assert [e.amount for _, e in s1] == [598, 598, 599, 599, 599, 444, 599, 599, 598, 460, 460, 467, 598]


def test_real_capture_events_are_in_non_decreasing_time_order():
    for cap in ("cap1_necro_spells.pcapng", "cap2_bard_melee_necro_pet.pcapng"):
        ts = [t for t, _ in PcapReplaySource(REAL / cap, load_config(), speed=0)]
        assert ts == sorted(ts)


def test_real_capture_unrelated_server_filter():
    # a different server_ip in the config means nothing is decoded (and nothing crashes)
    s = PcapReplaySource(REAL / "cap1_necro_spells.pcapng", cfg_net(server_ip="10.9.9.9"), speed=0)
    assert list(s) == []


# ============================================================================== pcapng: malformed / exotic input
def _good_frames(n=3):
    return [(BASE + i, server_frame(payload(H16, msg(i, hp(PLAYER, -10 - i, MOB))), port=20000 + i)) for i in range(n)]


def test_pcapng_little_and_big_endian_sections_agree(tmp_path):
    le = list(iter_raw_datagrams(write(tmp_path, pcap_bytes(_good_frames(), e="<"), "le")))
    be = list(iter_raw_datagrams(write(tmp_path, pcap_bytes(_good_frames(), e=">"), "be")))
    assert len(le) == 3 and le == be and le[0].src_ip == SERVER and le[0].dst_port == 20000


def test_pcapng_mixed_endian_sections_in_one_file(tmp_path):
    data = pcap_bytes(_good_frames(2), e="<") + pcap_bytes(_good_frames(2), e=">")
    assert len(list(iter_raw_datagrams(write(tmp_path, data)))) == 4


@pytest.mark.parametrize("link,wrap", [(1, eth), (101, lambda ip: ip), (228, lambda ip: ip)])
def test_pcapng_supported_link_types(tmp_path, link, wrap):
    ip = ip_udp(SERVER, 10070, CLIENT, 20000, payload(H16, msg(1, hp(PLAYER, -5, MOB))))
    out = list(iter_raw_datagrams(write(tmp_path, pcap_bytes([(BASE, wrap(ip))], link=link))))
    assert len(out) == 1 and out[0].dst_port == 20000


@pytest.mark.parametrize("link", [0, 113, 127, 12, 9999])
def test_pcapng_unknown_link_types_are_skipped(tmp_path, link):
    f = server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))
    assert list(iter_raw_datagrams(write(tmp_path, pcap_bytes([(BASE, f)], link=link)))) == []


def test_pcapng_ipv6_and_non_udp_and_arp_skipped(tmp_path):
    v6 = b"\x60" + b"\x00" * 39 + b"\x00" * 8
    tcp = ip_udp(SERVER, 10070, CLIENT, 20000, b"xxxxxxxx", proto=6)
    frames = [(BASE, eth(v6, ethertype=0x86DD)), (BASE + 1, eth(tcp)), (BASE + 2, eth(b"\x00" * 28, ethertype=0x0806)),
              (BASE + 3, v6), (BASE + 4, server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB)))))]
    out = list(iter_raw_datagrams(write(tmp_path, pcap_bytes(frames))))
    assert len(out) == 1 and out[0].ts == pytest.approx(BASE + 4)
    out6 = list(iter_raw_datagrams(write(tmp_path, pcap_bytes([(BASE, v6)], link=101), "r6")))
    assert out6 == []


def test_pcapng_ip_fragments_are_skipped_but_df_flag_is_not(tmp_path):
    pl = payload(H16, msg(1, hp(PLAYER, -5, MOB)))
    frames = [
        (BASE, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl, flags_frag=0x2000))),        # MF
        (BASE + 1, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl, flags_frag=0x00B9))),      # offset != 0
        (BASE + 2, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl, flags_frag=0x4000))),      # DF only
    ]
    out = list(iter_raw_datagrams(write(tmp_path, pcap_bytes(frames))))
    assert [d.ts for d in out] == [pytest.approx(BASE + 2)]


def test_pcapng_ip_options_vlan_and_padding_and_udp_length(tmp_path):
    pl = payload(H16, msg(1, hp(PLAYER, -5, MOB)))
    frames = [
        (BASE, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl, options=b"\x01\x01\x01\x00"))),        # IHL 6
        (BASE + 1, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl), vlans=1)),
        (BASE + 2, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl), vlans=2)),
        (BASE + 3, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl), pad=14)),                          # Ethernet trailer
        (BASE + 4, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl, ulen=8 + len(pl) - 3))),            # UDP shorter than IP
        (BASE + 5, eth(ip_udp(SERVER, 10070, CLIENT, 20000, pl, ulen=8 + len(pl) + 50))),           # UDP longer than IP
    ]
    out = list(iter_raw_datagrams(write(tmp_path, pcap_bytes(frames))))
    assert [d.payload for d in out[:4]] == [pl] * 4
    # Amended by main chat (Phase 2 amendments, N2-1b): a UDP length longer than the IP packet is dropped, as RawCapture does.
    assert len(out) == 5 and out[4].payload == pl[:-3]


def test_pcapng_timestamp_resolutions(tmp_path):
    f = server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))
    for opt, res in ((tsresol_opt(9), 1e-9), (tsresol_opt(3), 1e-3), (tsresol_opt(0x80 | 10), 2.0 ** -10), (b"", 1e-6)):
        d = list(iter_raw_datagrams(write(tmp_path, pcap_bytes([(BASE, f)], res=res, opts=opt))))
        assert d[0].ts == pytest.approx(BASE, abs=1e-3), (opt, d[0].ts)


@pytest.mark.parametrize("what", ["empty", "tiny", "garbage", "classic-pcap", "text"])
def test_pcapng_non_pcapng_files(tmp_path, what):
    data = {"empty": b"", "tiny": b"\x0a\x0d\x0d", "garbage": bytes(range(256)) * 4,
            "classic-pcap": bytes.fromhex("d4c3b2a1020004000000000000000000ffff000001000000") + b"\x00" * 64,
            "text": b"hello, this is not a capture file at all\n"}[what]
    p = write(tmp_path, data)
    try:
        out = list(iter_raw_datagrams(p))
    except ValueError:
        return                                                                # a clear ValueError is acceptable
    assert out == []


def test_pcapng_truncated_and_corrupt_tail_keeps_earlier_datagrams(tmp_path):
    good = pcap_bytes(_good_frames(3))
    for cut in (len(good) - 1, len(good) - 7, len(good) - 40):
        assert 2 <= len(list(iter_raw_datagrams(write(tmp_path, good[:cut], f"t{cut}")))) <= 3
    for bad_len in (0, 3, 11, 13, 0xFFFFFFFF, 0x7FFFFFF0):
        data = good + block(6, b"\x00" * 40, length=bad_len) + good[len(shb()):]
        assert len(list(iter_raw_datagrams(write(tmp_path, data, f"b{bad_len}")))) >= 3


def test_pcapng_epb_oddities(tmp_path):
    f = server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))
    data = shb() + idb() + epb(1, f, iface=7) + epb(2, f, caplen=10 * len(f)) + epb(3, f, caplen=0) + epb(4, f) \
        + block(0x00000005, b"\x00" * 20) + block(0x00000003, struct.pack("<I", len(f)) + f) + block(0xBAD, b"\x01\x02")
    out = list(iter_raw_datagrams(write(tmp_path, data)))
    assert [round(d.ts * 1e6) for d in out] == [2, 4]            # iface 7 unknown -> skipped; caplen 0 -> nothing; SPB/stat/unknown skipped


def test_pcapng_idb_option_garbage_cannot_hang_or_crash(tmp_path):
    f = server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))
    for opts in (struct.pack("<HH", 9, 0), struct.pack("<HH", 5, 0xFFFF) + b"x" * 8, struct.pack("<HH", 9, 1),
                 struct.pack("<HH", 1, 0) * 50, b"\xff" * 7):
        out = list(iter_raw_datagrams(write(tmp_path, pcap_bytes([(BASE, f)], opts=opts))))
        assert len(out) == 1


def test_pcapng_snaplen_truncated_frame_is_not_trusted(tmp_path):
    """Was BUG-N2-1 (fixed by the Phase 2 amendment; residual: see test_verify_net_amend.py): a frame cut by the capture's snap length (caplen < original length) is decoded as if complete:
    parse_ip_udp() clamps to what is there and yields a shortened payload, whose last 4 bytes the protocol then treats as the
    checksum.  (net/capture.py's own parser drops such packets as truncated; the pcapng reader is inconsistent with it.)"""
    pl = payload(H16, msg(1, hp(PLAYER, -5, MOB)), msg(2, hp(PLAYER, -6, MOB)))
    f = server_frame(pl)
    data = shb() + idb() + epb(1, f, caplen=len(f) - 12, orig=len(f))
    out = list(iter_raw_datagrams(write(tmp_path, data)))
    assert out == [], "truncated capture frame was accepted as a complete datagram"


def test_pktmon_duplicate_suppression_windows(tmp_path):
    pl = payload(H16, msg(1, hp(PLAYER, -5, MOB)))
    f = server_frame(pl)
    other_port = server_frame(pl, port=20001)
    frames = [(BASE, f), (BASE + 0.010, f), (BASE + 0.010, other_port), (BASE + 0.049, f),   # 2 dups in window (vs last KEPT)
              (BASE + 0.100, f), (BASE + 0.130, f)]
    out = list(read_pcapng(write(tmp_path, pcap_bytes(frames))))
    assert [(d.dst_port, round(d.ts - BASE, 3)) for d in out] == [(20000, 0.0), (20001, 0.01), (20000, 0.1)]


def test_pktmon_duplicate_suppression_ignores_earlier_timestamps(tmp_path):
    """Out-of-order capture timestamps for byte-identical datagrams (multi-component pktmon logs) are treated as dups."""
    f = server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))
    out = list(read_pcapng(write(tmp_path, pcap_bytes([(BASE + 10, f), (BASE, f)]))))
    assert len(out) == 1


def test_pcap_replay_events_are_time_ordered_even_if_capture_timestamps_are_not(tmp_path):
    """Was BUG-N2-2 (fixed by the amendment: clamped like ReplaySource): the LogSource protocol (and Amendment: ReplaySource clamps) needs non-decreasing ts.  PcapReplaySource
    yields Events in capture order with their own datagram time; pktmon logs written per network component can interleave
    slightly out of order, and nothing clamps or warns."""
    frames = [(BASE + 5, server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))),
              (BASE + 1, server_frame(payload(H16, msg(2, hp(PLAYER, -6, MOB)))))]
    ts = [t for t, _ in PcapReplaySource(write(tmp_path, pcap_bytes(frames)), load_config(), speed=0)]
    assert ts == sorted(ts), ts


# ============================================================================== protocol: framing, varints, false positives
def _dec(pl: bytes, port=20000):
    return decode_datagram(dg(BASE, pl, port))


def test_basic_record_roundtrip_and_fields():
    (r,) = _dec(payload(H16, msg(0x1234, hp(PLAYER, -598, MOB))))
    assert isinstance(r, HpChange) and (r.seq, r.source, r.amount, r.target, r.extended, r.conn, r.header_low16) == \
        (0x1234, PLAYER, -598, MOB, False, 20000, H16)
    (r2,) = _dec(payload(H16, msg(5, hp(PLAYER, 300, MOB))))
    assert r2.amount == 300
    (r3,) = _dec(payload(H16, msg(5, hp(PLAYER, -25, MOB, extra=b"\x00\x01\x02"))))
    assert r3.extended is True and r3.amount == -25


@pytest.mark.parametrize("n", range(0, 8))
def test_datagrams_shorter_than_header_plus_checksum(n):
    assert _dec(bytes([0xFB] * n)) == []


def test_every_truncation_of_a_valid_datagram_is_safe():
    pl = payload(H16, msg(1, hp(PLAYER, -598, MOB)), msg(2, swing(PET, MOB)), msg(3, hp(PLAYER, -25, MOB, b"\x07\x07")))
    full = _dec(pl)
    assert len(full) == 3
    for cut in range(len(pl) + 1):
        recs = _dec(pl[:cut])
        assert len(recs) <= 3
    # chopping the 4 checksum bytes off shifts the "checksum" into the last message: that message must NOT be decoded
    assert len(_dec(pl[:-4])) == 2


def test_message_must_end_before_the_checksum():
    body = hp(PLAYER, -5, MOB)
    for ck in (b"", b"\x00", b"\x00\x00\x00", b"\x00\x00\x00\x00"):
        pl = struct.pack("<H", H16) + b"\x01" + msg(1, body) + ck
        assert len(_dec(pl)) == (1 if len(ck) == 4 else 0), len(ck)


def test_length_byte_lies():
    body = hp(PLAYER, -5, MOB)
    ok = msg(1, body)
    for fake_len in (0, 1, 2, 5, 10, len(body) + 1, len(body) + 20, 255):
        bad = bytes([0xFB, fake_len]) + ok[2:]
        recs = _dec(payload(H16, bad))
        for r in recs:                                # whatever is accepted must be a self-consistent record
            assert isinstance(r, (HpChange, Swing))
    assert len(_dec(payload(H16, bytes([0xFB, len(body) - 3]) + ok[2:]))) == 0


@pytest.mark.parametrize("amount", [0, 1, -1, 63, -64, 64, -65, 8191, -8192, 598, -598, 2**20, -(2**20), 2**31 - 1, -(2**31)])
def test_varint_amounts_roundtrip(amount):
    (r,) = _dec(payload(H16, msg(1, hp(PLAYER, amount, MOB))))
    assert r.amount == amount and r.target == MOB


def test_varint_overlong_and_unterminated():
    def rec_with(varint_bytes: bytes, tail_target=True):
        body = b"\xdb\x00" + struct.pack("<I", PLAYER) + varint_bytes + (struct.pack("<I", MOB) if tail_target else b"")
        return _dec(payload(H16, msg(1, body)))
    assert [r.amount for r in rec_with(b"\x80\x00")] == [0]                       # non-canonical zero: accepted
    assert rec_with(b"\x80\x80\x80\x80\x80") == []                                # 5 continuation bytes: unterminated
    assert rec_with(b"\x80\x80\x80\x80\x80\x00") == []                            # 6-byte overlong: rejected
    assert rec_with(b"\x80\x80") == [] and rec_with(b"\x96") == []                # runs into the target bytes / body end
    assert rec_with(b"\x96\x05", tail_target=False) == []                         # varint ok, no target
    assert read_varint(b"\xac\x02", 0, 1) is None and read_varint(b"\xac\x02", 0, 2) == (300, 2)
    assert read_varint(b"", 0, 0) is None and read_varint(b"\x01", 1, 1) is None
    assert zigzag_decode(0) == 0 and zigzag_decode(1) == -1 and zigzag_decode(2) == 1 and zigzag_decode(3) == -2


def test_varint_ending_exactly_at_checksum_boundary_is_not_read_into_the_checksum():
    # message body claims a varint continuing, with the next bytes being the datagram checksum
    body = b"\xdb\x00" + struct.pack("<I", PLAYER) + b"\x96"       # continuation bit set, body ends here
    pl = struct.pack("<H", H16) + b"\x01" + msg(1, body) + b"\x05\x00\x00\x00"
    assert _dec(pl) == []


def test_huge_varint_amounts_do_not_crash_decoder_or_stats():
    (r,) = _dec(payload(H16, msg(1, b"\xdb\x00" + struct.pack("<I", PLAYER) + b"\xff\xff\xff\xff\x0f" + struct.pack("<I", MOB))))
    assert r.amount == zigzag_decode(0xFFFFFFFF)
    (r2,) = _dec(payload(H16, msg(1, b"\xdb\x00" + struct.pack("<I", PLAYER) + b"\xff\xff\xff\xff\x7f" + struct.pack("<I", MOB))))
    evs, _ = feed_all([dg(BASE, payload(H16, msg(1, b"\xdb\x00" + struct.pack("<I", PLAYER) + b"\xff\xff\xff\xff\x7f" + struct.pack("<I", MOB))))])
    seg = Segmenter(8.0)
    for e in evs:
        seg.feed(e)
    s = summarize_fight(seg.flush()[0])
    assert s.you.total == -r2.amount and s.you.dps > 0


def test_scan_does_not_decode_a_complete_message_inside_a_decoded_message_body():
    inner = msg(9, hp(MOB2, -777, MOB))
    outer = hp(PLAYER, -5, MOB, extra=inner)               # an extended record whose extra bytes look like a message
    recs = _dec(payload(H16, msg(1, outer)))
    assert [type(r) for r in recs] == [HpChange] and recs[0].amount == -5 and recs[0].extended


def test_fb_byte_inside_a_field_of_a_decoded_record_is_not_a_message():
    # source id and amount bytes containing 0xFB / valid-looking headers
    src = 0xFB0B00FB
    recs = _dec(payload(H16, msg(1, hp(src, -(0xFB >> 1), 0x0B0000FB)), msg(2, hp(PLAYER, -3, MOB))))
    assert [(r.source, r.amount) for r in recs] == [(src, -125), (PLAYER, -3)]


def test_risk_scan_decodes_a_phantom_message_inside_an_unknown_message():
    """RISK-1 (inherent to 'scan for 0xFB', as the contract specifies): bytes after an unrecognised message's header are
    re-scanned, so a valid-looking record inside an unknown message's body (or in the unparsed header region) is
    decoded as if it were real.  Passes today; documents the false-positive exposure.  Real captures show none (see the
    oracle test: no extra records)."""
    unknown = b"\x08\x00" + b"\x11" * 3 + msg(0x4242, hp(MOB2, -999, MOB)) + b"\x22"
    recs = _dec(payload(H16, msg(7, unknown)))
    assert [r.amount for r in recs] == [-999]
    recs2 = _dec(payload(H16, msg(0x4242, hp(MOB2, -999, MOB)), prefix=b"\x00"))         # fake in the 'header' region
    assert [r.amount for r in recs2] == [-999]


def test_unknown_opcodes_and_known_opcodes_with_short_bodies_are_skipped():
    msgs = [msg(1, b"\x08\x00" + b"\x00" * 20), msg(2, b"\xcd\x00" + b"\x00" * 9), msg(3, b"\xdb\x00" + b"\x00" * 5),
            msg(4, b"\xb1\x00" + b"\x00" * 8), msg(5, hp(PLAYER, -4, MOB))]
    assert [r.amount for r in _dec(payload(H16, *msgs)) if isinstance(r, HpChange)] == [-4]


def test_many_fb_bytes_scan_is_linear_and_safe():
    pl = struct.pack("<H", H16) + b"\xfb" * 60000 + b"\x00" * 4
    t0 = time.perf_counter()
    assert _dec(pl) == []
    assert time.perf_counter() - t0 < 2.0


# ============================================================================== dedupe per connection
def _hp_dgram(ts, seq, amount, port=20000, body=None):
    return dg(ts, payload(H16, msg(seq, body if body is not None else hp(PLAYER, amount, MOB))), port)


def test_same_seq_and_body_resent_is_dropped_same_datagram_and_later():
    d = PacketDecoder(SERVER)
    one = d.feed(_hp_dgram(BASE, 5, -10))
    two = d.feed(_hp_dgram(BASE + 0.4, 5, -10))                        # server re-send in a later datagram
    both = PacketDecoder(SERVER).feed(dg(BASE, payload(H16, msg(5, hp(PLAYER, -10, MOB)), msg(5, hp(PLAYER, -10, MOB)))))
    assert len(one) == 1 and two == [] and len(both) == 1


def test_same_seq_different_body_is_kept():
    d = PacketDecoder(SERVER)
    a = d.feed(_hp_dgram(BASE, 5, -10))
    b = d.feed(_hp_dgram(BASE + 0.1, 5, -11))
    c = d.feed(_hp_dgram(BASE + 0.2, 6, -10))                           # different seq, same body (a real second equal hit)
    assert len(a) == len(b) == len(c) == 1


def test_dedupe_is_per_connection_and_per_server_only():
    d = PacketDecoder(SERVER)
    assert len(d.feed(_hp_dgram(BASE, 5, -10, port=20000))) == 1
    assert len(d.feed(_hp_dgram(BASE + 0.1, 5, -10, port=20001))) == 1
    assert d.feed(dg(BASE + 0.2, payload(H16, msg(9, hp(PLAYER, -1, MOB))), src="1.2.3.4")) == []     # not from the server
    c2s = UdpDatagram(BASE, CLIENT, 20000, SERVER, 10070, payload(H16, msg(9, hp(PLAYER, -1, MOB))))
    assert d.feed(c2s) == []                                                                         # client -> server ignored


def test_dedupe_memory_expires_after_window():
    d = PacketDecoder(SERVER)
    assert len(d.feed(_hp_dgram(BASE, 5, -10))) == 1
    assert len(d.feed(_hp_dgram(BASE + 119.0, 5, -10))) == 0
    assert len(d.feed(_hp_dgram(BASE + 250.0, 5, -10))) == 1


def test_seq_wraparound_does_not_confuse_dedupe():
    d = PacketDecoder(SERVER)
    n = sum(len(d.feed(_hp_dgram(BASE + i * 0.01, s, -7))) for i, s in enumerate([65534, 65535, 0, 1]))
    assert n == 4


def test_dedupe_prune_keeps_memory_bounded_over_long_streams():
    d = PacketDecoder(SERVER)
    for i in range(5000):
        d.feed(_hp_dgram(BASE + i * 0.5, i % 65536, -(i % 500) - 1))
    assert sum(len(t) for t in d._seen.values()) < 600       # ~120 s window at 2 msgs/s, not 5000


# ============================================================================== attribution
def _stream(*items, port=20000, h16=H16):
    """items = (ts, [bodies]) -> datagrams with increasing seq."""
    out, seq = [], 100
    for ts, bodies in items:
        ms = []
        for b in bodies:
            seq += 1
            ms.append(msg(seq, b))
        out.append(dg(ts, payload(h16, *ms), port))
    return out


def test_player_damage_taken_and_heal_kinds():
    evs, _ = feed_all(_stream((BASE, [hp(PLAYER, -50, MOB), hp(MOB, -20, PLAYER), hp(MOB2, 300, PLAYER)])))
    assert [(e.kind, e.actor, e.target, e.amount) for e in evs] == [
        (KIND_DAMAGE, "you", "0x%08X" % MOB, 50), (KIND_DAMAGE_TAKEN, "0x%08X" % MOB, "you", 20), (KIND_HEAL, "0x%08X" % MOB2, "you", 300)]
    assert all(e.raw and e.ability is None for e in evs)


def test_player_id_learned_late_and_events_before_are_not_misattributed_to_you():
    """AMENDED contract: combat between other entities seen before the player's first record is dropped (and counted),
    never turned into an event; the player's first record is kept (the id is learned from that very record)."""
    stream = _stream((BASE, [hp(MOB, -9, MOB2)]), (BASE + 1, [hp(PLAYER, -5, MOB)]))
    evs, src = feed_all(stream)
    attr = src.router.attributor(20000)
    assert attr.player_id == PLAYER and attr.dropped_unrelated == 1
    assert [(e.actor, e.amount) for e in evs] == [("you", 5)]


def test_player_id_never_matches_header_means_no_events_counted_as_dropped_and_no_crash():
    """AMENDED: with no player id nothing is related, so every record is dropped and counted; no 'you' event can appear
    (and no hex-named 'damage' events either, unlike the old rule).  See test_pcap_with_traffic_but_no_player_...:
    the user-visible notice for this situation."""
    evs, src = feed_all(_stream((BASE, [hp(PLAYER, -5, MOB), hp(MOB, -3, PLAYER)]), h16=0x1111))
    attr = src.router.attributor(20000)
    assert attr.player_id is None and attr.dropped_unrelated == 2 and evs == []


def test_player_id_cleared_when_header_changes_on_a_connection():
    a = _stream((BASE, [hp(PLAYER, -5, MOB)]))
    other = 0x1751CB82
    b = _stream((BASE + 60, [hp(other, -7, MOB)]), h16=other & 0xFFFF)
    evs, src = feed_all(a + b)
    assert [e.actor for e in evs] == ["you", "you"] and src.router.attributor(20000).player_id == other


def test_risk_npc_with_matching_low16_before_the_player_is_taken_for_the_player():
    """RISK-2 (INFERRED player-id rule): the first entity id with low16 == header wins.  An NPC whose id happens to share
    those 16 bits and acts before the player's first record becomes 'you'; the real player is then hex.  1/65536 per entity."""
    impostor = 0x02000000 | H16
    stream = _stream((BASE, [hp(impostor, -99, MOB)]), (BASE + 1, [hp(PLAYER, -5, MOB)]))
    evs, src = feed_all(stream)
    # AMENDED: the real player's own records are now DROPPED as unrelated (before: shown under a hex name), so the whole
    # session silently attributes the impostor's hits to 'you' and loses the player's.  Deferred risk, still present.
    assert [(e.actor, e.amount) for e in evs] == [("you", 99)]
    assert src.router.attributor(20000).player_id == impostor and src.router.attributor(20000).dropped_unrelated == 1


def test_swing_does_not_produce_events_but_teaches_player_id():
    evs, src = feed_all(_stream((BASE, [swing(PLAYER, MOB)])))
    assert evs == [] and src.router.attributor(20000).player_id == PLAYER


def test_pet_learning_window_and_first_wins():
    stream = _stream((BASE, [swing(PET, MOB)]), (BASE + 0.4, [hp(PLAYER, -25, MOB, extra=b"\x00")]),
                     (BASE + 3, [swing(MOB2, MOB)]), (BASE + 3.2, [hp(PLAYER, -25, MOB, extra=b"\x00")]),
                     (BASE + 4, [hp(MOB, -9, PET)]))
    evs, src = feed_all(_stream((BASE - 1, [hp(PLAYER, -1, MOB)])) + stream)
    assert src.router.attributor(20000).pet_id == PET
    assert evs[-1].kind == KIND_DAMAGE_TAKEN and evs[-1].target == "pet"
    assert [e.actor for e in evs if e.kind == KIND_DAMAGE][1:] == ["pet", "pet"]


@pytest.mark.parametrize("dt,learned", [(0.0, True), (0.999, True), (1.0, True), (1.001, False), (-0.2, False)])
def test_pet_swing_to_hit_time_boundary(dt, learned):
    base = _stream((BASE - 5, [hp(PLAYER, -1, MOB)]))
    sw = _stream((BASE, [swing(PET, MOB)]))
    hit = _stream((BASE + dt, [hp(PLAYER, -25, MOB, extra=b"\x00")]))
    stream = base + (sw + hit if dt >= 0 else hit + sw)
    _, src = feed_all(stream)
    assert (src.router.attributor(20000).pet_id == PET) is learned


def test_pet_not_learned_from_player_swing_other_target_or_non_extended_hit():
    for stream in (
        _stream((BASE, [swing(PLAYER, MOB)]), (BASE + 0.2, [hp(PLAYER, -25, MOB, extra=b"\x00")])),           # attacker is the player
        _stream((BASE, [swing(PET, MOB2)]), (BASE + 0.2, [hp(PLAYER, -25, MOB, extra=b"\x00")])),             # other target
        _stream((BASE, [swing(PET, MOB)]), (BASE + 0.2, [hp(PLAYER, -25, MOB)])),                              # not extended
    ):
        _, src = feed_all(_stream((BASE - 2, [hp(PLAYER, -1, MOB)])) + stream)
        assert src.router.attributor(20000).pet_id is None


def test_pet_hit_before_player_id_known_never_learns_pet_until_player_known():
    stream = _stream((BASE, [swing(PET, MOB)]), (BASE + 0.1, [hp(PLAYER, -25, MOB, extra=b"\x00")]))
    evs, src = feed_all(stream)
    # the swing and hit both carry ids; player id is learned from the swing? No: PET's low16 differs, MOB differs; hit teaches player
    assert src.router.attributor(20000).player_id == PLAYER
    assert src.router.attributor(20000).pet_id == PET           # swing was remembered, so the same-datagram-order case still works
    assert evs[0].actor == "pet"


def test_risk_group_member_swing_is_taken_for_the_pet():
    """RISK-3 (INFERRED pet rule): any non-player attacker that swings at T within 1 s before the player's extended hit at T
    is 'the pet'.  A group member's swing makes them the pet; hostile damage to that member then shows as damage_taken on 'pet'."""
    member = 0x01998877
    stream = _stream((BASE - 5, [hp(PLAYER, -1, MOB)]), (BASE, [swing(member, MOB)]),
                     (BASE + 0.3, [hp(PLAYER, -25, MOB, extra=b"\x00")]), (BASE + 0.4, [hp(MOB, -40, member)]))
    evs, src = feed_all(stream)
    assert src.router.attributor(20000).pet_id == member and evs[-1].kind == KIND_DAMAGE_TAKEN and evs[-1].target == "pet"


def test_risk_pet_is_never_relearned_after_it_changes():
    pet2 = 0x01445566
    stream = _stream((BASE - 5, [hp(PLAYER, -1, MOB)]), (BASE, [swing(PET, MOB)]), (BASE + 0.3, [hp(PLAYER, -25, MOB, extra=b"\x00")]),
                     (BASE + 60, [swing(pet2, MOB)]), (BASE + 60.3, [hp(PLAYER, -25, MOB, extra=b"\x00")]),
                     (BASE + 61, [hp(MOB, -40, pet2)]))
    evs, src = feed_all(stream)
    attr = src.router.attributor(20000)
    assert attr.pet_id == PET
    # AMENDED: a hostile hit on the re-summoned pet is now dropped as unrelated (before: a hex-named 'damage' event)
    assert evs[-1].kind == KIND_DAMAGE and evs[-1].actor == "pet" and evs[-1].target == "0x%08X" % MOB   # the pet's own hit
    assert attr.dropped_unrelated == 1 and not [e for e in evs if e.kind == KIND_DAMAGE_TAKEN]


def test_hostile_damage_to_third_parties_is_dropped_and_cannot_change_the_fight_target():
    """AMENDED (was RISK-4): a mob hitting a group member / unknown pet produces no event, so the fight's primary target stays
    the mob you hit and the damage is not visible anywhere except Attributor.dropped_unrelated."""
    victim = 0x01AAAAAA
    stream = _stream((BASE, [hp(PLAYER, -10, MOB)]), (BASE + 1, [hp(MOB, -500, victim)]))
    evs, src = feed_all(stream)
    assert len(evs) == 1 and src.router.attributor(20000).dropped_unrelated == 1
    seg = Segmenter(8.0)
    for e in evs:
        seg.feed(e)
    s = summarize_fight(seg.flush()[0])
    assert s.target == "0x%08X" % MOB and s.targets == ["0x%08X" % MOB] and s.damage_taken == 0


def test_unrelated_filter_keeps_everything_that_involves_player_or_learned_pet():
    victim = 0x01AAAAAA
    stream = _stream(
        (BASE - 5, [hp(PLAYER, -1, MOB)]), (BASE, [swing(PET, MOB)]), (BASE + 0.3, [hp(PLAYER, -25, MOB, extra=b"\x00")]),
        (BASE + 1, [hp(MOB, -40, PET)]),             # hostile -> pet: kept as damage_taken on 'pet'
        (BASE + 2, [hp(MOB, -3, PLAYER)]),           # hostile -> player: kept
        (BASE + 3, [hp(MOB2, 300, PLAYER)]),         # stranger heals player: kept as heal
        (BASE + 4, [hp(PLAYER, 300, victim)]),       # player heals a stranger: kept (source is related)
        (BASE + 5, [hp(MOB, -9, victim)]),           # unrelated: dropped
        (BASE + 6, [hp(victim, 300, MOB2)]),         # unrelated heal: dropped
        (BASE + 7, [swing(victim, MOB)]),            # swings never produce events
    )
    evs, src = feed_all(stream)
    assert [(e.kind, e.actor, e.target) for e in evs[2:]] == [
        (KIND_DAMAGE_TAKEN, "0x%08X" % MOB, "pet"), (KIND_DAMAGE_TAKEN, "0x%08X" % MOB, "you"),
        (KIND_HEAL, "0x%08X" % MOB2, "you"), (KIND_HEAL, "you", "0x%08X" % victim)]
    assert src.router.attributor(20000).dropped_unrelated == 2


def test_dropped_unrelated_is_counted_per_connection_and_includes_unselected_ones():
    other = 0x1751CB82
    items = [dg(BASE, payload(H16, msg(1, hp(PLAYER, -5, MOB))), 20000),
             dg(BASE + 1, payload(H16, msg(2, hp(MOB, -9, MOB2))), 20000),
             dg(BASE + 1, payload(other & 0xFFFF, msg(1, hp(MOB, -9, MOB2))), 20001)]
    evs, src = feed_all(items)
    assert len(evs) == 1 and src.router.attributor(20000).dropped_unrelated == 1 and src.router.attributor(20001).dropped_unrelated == 1


def test_extended_zero_amount_records():
    pre = _stream((BASE - 2, [hp(PLAYER, -1, MOB)]))
    evs, _ = feed_all(pre + _stream((BASE, [hp(PLAYER, 0, MOB, extra=b"\x01\x02"), hp(PLAYER, 0, MOB), hp(MOB2, 0, MOB, extra=b"\x01")])))
    zero = evs[1:]
    # AMENDED: the third record (a stranger's 0 at the mob) is dropped as unrelated
    assert [(e.kind, e.actor, e.amount) for e in zero] == [(KIND_DAMAGE, "pet", 0), (KIND_DAMAGE, "you", 0)]
    seg = Segmenter(8.0)
    for e in zero:
        seg.feed(e)
    s = summarize_fight(seg.flush()[0])
    assert s.pet.hits == 1 and s.you.hits == 1 and s.combined_total == 0 and s.combined_dps == 0


def test_heal_events_never_start_or_extend_a_fight():
    pre = _stream((BASE - 2, [hp(PLAYER, -1, MOB)]))
    evs, _ = feed_all(pre + _stream((BASE, [hp(MOB, 300, PLAYER)])))
    heal = evs[1]
    assert heal.kind == KIND_HEAL and KIND_HEAL not in ACTIVITY_KINDS
    seg = Segmenter(8.0)
    assert seg.feed(heal) == [] and seg.current is None                          # does not start
    seg.feed(evs[0])
    seg.feed(Event(evs[0].ts + 7.9, "you", "you", 300, None, KIND_HEAL, ""))      # inside the window: attached, no extension
    assert seg.current.end_ts == evs[0].ts
    closed = seg.tick(evs[0].ts + 8.0)
    assert len(closed) == 1 and closed[0].duration == 0
    s = summarize_fight(closed[0])
    assert s.combined_total == 1 and s.damage_taken == 0 and 300 not in [a.total for a in s.actors.values()]


def test_heal_stream_alone_through_engine_creates_no_fights(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    heals = [(BASE + i, Event(BASE + i, "x", "you", 300, None, KIND_HEAL, "")) for i in range(30)]

    class S:
        def __iter__(self):
            return iter(heals)

        def close(self):
            pass

    eng = Engine(cfg, S())
    eng.run()
    assert eng.closed_fights == [] and eng.run_summary().fight_count == 0


# ============================================================================== connections: interleave / selection
def _two_conn_stream():
    other = 0x1751CB82
    items = []
    for i in range(6):
        items.append(dg(BASE + i, payload(H16, msg(100 + i, hp(PLAYER, -10 - i, MOB))), 20000))
        items.append(dg(BASE + i + 0.5, payload(other & 0xFFFF, msg(200 + i, hp(other, -100 - i, MOB2, extra=b"\x00"))), 20001))
    return items


def test_two_connections_interleaved_default_follows_both_first_with_combat_is_you(capsys):
    """PHASE 3: default = every connection.  The first to produce an Event is `you`; the other is its fixed label (`char:<port>`
    here: no name known), its extended hits are "<label>'s pet".  Nothing is double counted."""
    evs, src = feed_all(_two_conn_stream())
    assert src.router.selected == 20000
    mine = [e for e in evs if e.actor == "you"]
    theirs = [e for e in evs if e.actor != "you"]
    assert [e.amount for e in mine] == [10, 11, 12, 13, 14, 15] and [e.amount for e in theirs] == [100, 101, 102, 103, 104, 105]
    assert {e.actor for e in theirs} == {"char:20001's pet"} and src.own_actors() == ["you", "char:20001's pet"]
    err = capsys.readouterr().err
    assert err.count("eqoa net: following connection") == 1 and err.count("also following connection") == 1 and "char:20001" in err
    assert src.router.attributor(20001).player_id == 0x1751CB82 and src.router.attributor(20001).label == "char:20001"


def test_two_connections_explicit_selection_either_way():
    for port, amounts in ((20000, [10, 11, 12, 13, 14, 15]), (20001, [100, 101, 102, 103, 104, 105])):
        evs, src = feed_all(_two_conn_stream(), cfg_net(local_port=port))
        assert [e.amount for e in evs] == amounts and src.router.selected == port


def test_explicit_selection_of_a_port_that_never_appears_yields_nothing_silently(capsys):
    evs, src = feed_all(_two_conn_stream(), cfg_net(local_port=33333))
    assert evs == [] and src.router.selected == 33333
    assert capsys.readouterr().err == ""                                   # (observation: no hint that 33333 was never seen)


def test_swing_only_connection_gets_no_role_until_it_produces_an_event():
    """PHASE 3: the primary is the first connection that produces an EVENT, not the first one that sends a record."""
    other = 0x1751CB82
    items = [dg(BASE, payload(other & 0xFFFF, msg(1, swing(PET, MOB))), 20001),
             dg(BASE + 1, payload(H16, msg(1, hp(PLAYER, -5, MOB))), 20000),
             dg(BASE + 2, payload(other & 0xFFFF, msg(2, hp(other, -9, MOB))), 20001)]
    evs, src = feed_all(items)
    assert src.router.selected == 20000 and [(e.actor, e.amount) for e in evs] == [("you", 5), ("char:20001", 9)]


def test_connection_that_appears_later_from_a_new_port_is_followed_with_its_own_label(capsys):
    """PHASE 3 (was RISK-5): a reconnect from a new client port is now followed, as another character label; the old port's
    state stays but it never fires again.  The primary is still the first port, so after a reconnect the player shows as a
    label, not `you` (documented consequence of 'fixed per run')."""
    first = [dg(BASE, payload(H16, msg(1, hp(PLAYER, -5, MOB))), 20000)]
    later = [dg(BASE + 600, payload(H16, msg(1, hp(PLAYER, -7, MOB))), 20555)]
    evs, src = feed_all(first + later)
    assert [(e.actor, e.amount) for e in evs] == [("you", 5), ("char:20555", 7)] and src.router.selected == 20000
    assert "20555" in capsys.readouterr().err


def test_router_with_explicit_port_still_updates_other_connections_state():
    r = ConnectionRouter(cfg_net(local_port=20001))
    d = PacketDecoder(SERVER)
    out = []
    for dgram in _two_conn_stream():
        for rec in d.feed(dgram):
            out.extend(r.feed(rec))
    assert r.attributor(20000).player_id == PLAYER and len(out) == 6


# ============================================================================== PcapReplaySource pacing / close
def _timed_capture(tmp_path, gap=30.0):
    frames = [(BASE, server_frame(payload(H16, msg(1, hp(PLAYER, -10, MOB))))),
              (BASE + gap, server_frame(payload(H16, msg(2, hp(PLAYER, -20, MOB)))))]
    return write(tmp_path, pcap_bytes(frames))


def test_pcap_speed_zero_has_no_heartbeats_and_yields_event_times(tmp_path):
    items = list(PcapReplaySource(_timed_capture(tmp_path), load_config(), speed=0))
    assert len(items) == 2 and all(isinstance(e, Event) for _, e in items)
    assert [t for t, _ in items] == [e.ts for _, e in items] and items[1][0] - items[0][0] == pytest.approx(30.0)


def test_pcap_speed_positive_yields_bounded_monotonic_heartbeats_and_closes_fight_early(tmp_path):
    p = _timed_capture(tmp_path)
    src = PcapReplaySource(p, load_config(), speed=30.0, heartbeat_interval=0.05)     # 30 file-seconds = 1 real second
    items = list(src)
    hb = [(t, r) for t, r in items if isinstance(r, str)]
    assert hb and all(r == HEARTBEAT for _, r in hb)
    ts = [t for t, _ in items]
    assert ts == sorted(ts) and all(BASE <= t <= BASE + 30.0001 for t in ts)
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    t0 = time.monotonic()
    marks = []
    eng = Engine(cfg, PcapReplaySource(p, cfg, speed=30.0, heartbeat_interval=0.05), on_closed=lambda s: marks.append((s.fight_id, time.monotonic() - t0)))
    eng.run()
    assert marks[0][0] == 1 and 0.15 <= marks[0][1] <= 0.6, marks            # 8 file-seconds / 30 = 0.27 s real, not 1.0 s
    assert len(marks) == 2


def test_pcap_close_stops_a_slow_replay_promptly(tmp_path):
    p = _timed_capture(tmp_path, gap=5000.0)
    src = PcapReplaySource(p, load_config(), speed=1.0, heartbeat_interval=0.05)
    got = []
    th = threading.Thread(target=lambda: [got.append(x) for x in src], daemon=True)
    th.start()
    time.sleep(0.4)
    src.close()
    th.join(timeout=2)
    assert not th.is_alive() and len([g for g in got if isinstance(g[1], Event)]) == 1


def test_pcap_replay_validates_arguments(tmp_path):
    with pytest.raises(ValueError):
        PcapReplaySource(tmp_path / "x", load_config(), speed=-1)
    with pytest.raises(ValueError):
        PcapReplaySource(tmp_path / "x", load_config(), heartbeat_interval=0)


def test_pcap_replay_of_capture_with_no_game_traffic_is_empty_not_an_error(tmp_path):
    other = eth(ip_udp("8.8.8.8", 53, CLIENT, 20000, b"\x00" * 30))
    assert list(PcapReplaySource(write(tmp_path, pcap_bytes([(BASE, other)])), load_config(), speed=0)) == []


# ============================================================================== LivePacketSource with a fake capture
class FakeCapture:
    """Stands in for net.capture.RawCapture: read() pops a script; Exceptions in the script are raised."""
    instances: list = []

    def __init__(self, net, script=None, fail_open=None):
        self.net, self.script, self.fail_open = net, list(script or []), fail_open
        self.stop = threading.Event()
        self.opened = self.closed = 0
        self.read_calls = 0
        FakeCapture.instances.append(self)

    def __enter__(self):
        if self.fail_open:
            raise self.fail_open
        self.opened += 1
        return self

    def __exit__(self, *a):
        self.close()

    def read(self):
        self.read_calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.stop.wait(0.02)
        return None

    def close(self):
        self.closed += 1
        self.stop.set()


def _live(script, cfg=None, fail_open=None):
    cfg = cfg or load_config()
    return LivePacketSource(cfg, capture_factory=lambda net: FakeCapture(net, script, fail_open))


def test_live_source_events_heartbeats_and_ordering():
    now = time.time()
    script = [None, dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB)))), None, dg(now + 0.1, payload(H16, msg(2, hp(PLAYER, -6, MOB))))]
    src = _live(script)
    items = []
    for it in src:
        items.append(it)
        if len([i for i in items if isinstance(i[1], Event)]) == 2:
            src.close()
    kinds = ["E" if isinstance(r, Event) else "H" for _, r in items]
    assert kinds[:4] == ["H", "E", "H", "E"] and [r.amount for _, r in items if isinstance(r, Event)] == [5, 6]
    assert all(r == HEARTBEAT for _, r in items if isinstance(r, str))


def test_live_source_close_from_another_thread_unblocks_and_closes_capture():
    src = _live([])
    done = threading.Event()
    seen = []

    def run():
        for it in src:
            seen.append(it)
        done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.2)
    assert not done.is_set() and seen                                  # alive, heartbeating
    t0 = time.monotonic()
    src.close()
    assert done.wait(2.0) and time.monotonic() - t0 < 1.0
    cap = FakeCapture.instances[-1]
    assert cap.closed >= 1
    src.close()                                                        # idempotent


def test_live_source_startup_error_surfaces_on_first_next():
    from eqoa_parser.net.capture import CaptureError

    src = _live([], fail_open=CaptureError("live capture needs Administrator rights"))
    with pytest.raises(CaptureError, match="Administrator"):
        next(iter(src))


def test_live_source_capture_error_after_startup_surfaces_and_engine_still_flushes(tmp_path):
    from eqoa_parser.net.capture import CaptureError

    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    now = time.time()
    src = _live([dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB)))), CaptureError("capture socket failed: boom")], cfg)
    closed, fin = [], []
    eng = Engine(cfg, src, on_closed=closed.append, on_finished=lambda: fin.append(1))
    with pytest.raises(CaptureError, match="boom"):
        eng.run()
    assert len(closed) == 1 and closed[0].you.total == 5 and fin == [1]          # in-progress fight flushed, finish fired
    assert FakeCapture.instances[-1].closed >= 1


def test_live_source_idle_fight_closes_via_heartbeats(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), idle_timeout=0.3)
    src = _live([dg(time.time(), payload(H16, msg(1, hp(PLAYER, -5, MOB))))], cfg)
    closed, stop = [], threading.Event()
    eng = Engine(cfg, src, on_closed=closed.append)
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.9)
    assert len(closed) == 1                                            # closed by heartbeat, before stop/finish
    stop.set()
    src.close()
    th.join(timeout=2)
    assert not th.is_alive()


def test_live_source_non_server_and_garbage_datagrams_are_harmless():
    now = time.time()
    script = [dg(now, b"", 20000), dg(now, b"\xfb" * 40), dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB))), src="9.9.9.9"),
              dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB))))]
    src = _live(script)
    evs = []
    for _, it in src:
        if isinstance(it, Event):
            evs.append(it)
            src.close()
    assert [e.amount for e in evs] == [5]


# ============================================================================== fuzz (seeded) and semantics worth pinning down
def _mutate(rnd, b: bytes, n: int = 3) -> bytes:
    b = bytearray(b)
    for _ in range(rnd.randint(1, n)):
        k = rnd.random()
        if k < .5 and b:
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        elif k < .7 and b:
            del b[rnd.randrange(len(b)):]
        elif k < .85:
            b.insert(rnd.randrange(len(b) + 1), rnd.randrange(256))
        elif b:
            i = rnd.randrange(len(b))
            b[i:i + rnd.randint(1, 8)] = bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 8)))
    return bytes(b)


def test_fuzz_decoder_pcapng_and_attribution_never_raise(tmp_path, capsys):
    import random

    rnd = random.Random(20261017)
    seed_pl = payload(H16, msg(1, hp(PLAYER, -598, MOB)), msg(2, swing(PET, MOB)), msg(3, hp(PLAYER, -25, MOB, b"\x07\x07")),
                      msg(4, hp(MOB, 300, PLAYER)))
    src = PacketSource(load_config())
    for i in range(6000):
        pl = _mutate(rnd, seed_pl) if rnd.random() < .8 else bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 80)))
        src.events_from(dg(BASE + i * .01, pl, 20000 + rnd.randrange(3)))
    good = pcap_bytes([(BASE + i, server_frame(seed_pl, 20000 + i)) for i in range(6)])
    for i in range(800):
        p = write(tmp_path, _mutate(rnd, good, 6), "f.pcapng")
        try:
            list(read_pcapng(p))
        except ValueError:
            pass                                                   # only the "not a pcapng" error is allowed to escape
    ids = [PLAYER, PET, MOB, MOB2, H16, 0xFFFFFFFF, 0]
    for _ in range(100):
        a = Attributor()
        for j in range(150):
            t = BASE + j * rnd.choice([0, .1, 1, -1])
            h = rnd.choice([H16, 0x1111, 0])
            rec = HpChange(t, 1, h, j, rnd.choice(ids), rnd.randint(-2 ** 31, 2 ** 31), rnd.choice(ids), rnd.random() < .3, b"") \
                if rnd.random() < .7 else Swing(t, 1, h, j, rnd.choice(ids), rnd.choice(ids), 0, b"")
            a.feed(rec)


def test_risk_zero_amount_hostile_record_starts_a_fight():
    """RISK-6 (contract: 0 counts as a landed hit; INFERRED that amount-0 records are hits): a 0-amount record from a mob at the
    player (a resisted/absorbed or non-damage action, if the server sends those) starts a fight with zero damage."""
    evs, _ = feed_all(_stream((BASE, [hp(PLAYER, -1, MOB)]), (BASE + 30, [hp(MOB, 0, PLAYER)])))
    seg = Segmenter(8.0)
    closed = []
    for e in evs:
        closed += seg.feed(e)
    closed += seg.flush()
    assert len(closed) == 2 and summarize_fight(closed[1]).damage_taken == 0 and closed[1].events[0].kind == KIND_DAMAGE_TAKEN


def test_risk_other_peoples_combat_in_view_extends_your_fight():
    """AMENDED (was RISK-7): hits between two unrelated entities no longer start or extend YOUR fight.  Only the dropped counter sees
    them.  The fight closes idle_timeout after your last hit, driven by the later records' timestamps through tick()."""
    stranger_a, stranger_b = 0x01111111, 0x02222222
    items = [(BASE, [hp(PLAYER, -10, MOB)])] + [(BASE + 5 * k, [hp(stranger_a, -50, stranger_b)]) for k in range(1, 8)]
    evs, src = feed_all(_stream(*items))
    assert len(evs) == 1 and src.router.attributor(20000).dropped_unrelated == 7
    seg = Segmenter(8.0)
    closed = []
    for e in evs:
        closed += seg.feed(e)
    assert closed == [] and seg.current.end_ts == BASE
    (f,) = seg.tick(BASE + 8.0)
    assert f.duration == 0 and len(f.events) == 1


def test_connection_that_only_sees_strangers_fight_does_not_become_the_primary():
    """PHASE 3 (was RISK-8, resolved): the primary is the first connection that produces an EVENT.  A connection whose records are all
    dropped as unrelated gets no role, so the connection where you actually fight is `you`."""
    other = 0x1751CB82
    items = [dg(BASE, payload(H16, msg(1, hp(0x01111111, -50, 0x02222222))), 20000),             # idle char sees strangers
             dg(BASE + 1, payload(other & 0xFFFF, msg(1, hp(other, -9, MOB))), 20001)]           # the char actually fighting
    evs, src = feed_all(items)
    assert src.router.selected == 20001 and [(e.actor, e.amount) for e in evs] == [("you", 9)]
    assert src.router.attributor(20000).dropped_unrelated == 1 and not src.router.attributor(20000).role_fixed


def test_live_idle_fight_closes_even_while_the_server_keeps_sending_non_combat_datagrams(tmp_path):
    """Was BUG-N2-5 (fixed by the amendment): LivePacketSource only yields a heartbeat when capture.read() times out (or returns an unrelated
    packet).  The server sends datagrams continuously (cap2: median gap 60-200 ms); each non-combat server datagram yields no item,
    so the segmenter clock does not advance and a finished fight stays 'ongoing' until a quiet 250 ms gap happens or the next hit."""
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), idle_timeout=0.4)
    noise = payload(H16, msg(9, b"\x7b\x0a" + b"\x00" * 12))                 # server datagram with no combat record

    class Busy(FakeCapture):
        def read(self):
            self.read_calls += 1
            if self.read_calls == 1:
                return dg(time.time(), payload(H16, msg(1, hp(PLAYER, -5, MOB))))
            time.sleep(0.01)
            return dg(time.time(), noise)

    src = LivePacketSource(cfg, capture_factory=lambda net: Busy(net))
    closed, stop = [], threading.Event()
    eng = Engine(cfg, src, on_closed=closed.append)
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(1.5)
    n_closed = len(closed)
    stop.set()
    src.close()
    th.join(timeout=3)
    assert n_closed == 1, "fight still open 1.1 s after its last hit (idle_timeout 0.4)"


# ============================================================================== zero-UI-imports for the new modules (tests/test_contracts.py's core list does not include them)
@pytest.mark.parametrize("mod", ["net", "net.pcapng", "net.protocol", "net.attribution", "net.sources", "net.capture"])
def test_net_modules_are_stdlib_only_and_ui_free(mod):
    import ast
    import subprocess
    import sys

    path = ROOT / "eqoa_parser" / (mod.replace(".", "/") + ".py" if mod != "net" else "net/__init__.py")
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [(a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "", node.level)]
        for name, level in names:
            top = name.split(".")[0]
            if level == 0 and top:
                assert top in sys.stdlib_module_names or top == "eqoa_parser", (mod, name)
            assert top not in {"tkinter", "ui"} and not name.startswith("eqoa_parser.ui"), (mod, name)
    code = ("import sys, importlib; importlib.import_module('eqoa_parser.%s'); "
            "bad = [m for m in sys.modules if m.split('.')[0] in ('tkinter', '_tkinter', 'yaml') or m.startswith('eqoa_parser.ui')]; "
            "print(bad); sys.exit(1 if [b for b in bad if not b.startswith('yaml')] else 0)") % mod
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
