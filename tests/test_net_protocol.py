"""Protocol decoder tests (session B), including the main chat's oracle on the two real captures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from _net_builders import CLIENT, SERVER, hp_body, msg, payload, swing_body, varint, zigzag

from eqoa_parser.net.pcapng import UdpDatagram, iter_raw_datagrams, read_pcapng
from eqoa_parser.net.protocol import HpChange, PacketDecoder, Swing, decode_datagram, read_varint, zigzag_decode

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
ORACLE = json.loads((REAL / "expected_records.json").read_text(encoding="utf-8"))["captures"]
PLAYER, DUMMY, PET = 0x13FF89D8, 0x01312F0A, 0x01374606


def dg(data, ts=1.0, src=SERVER, dport=24159):
    return UdpDatagram(ts, src, 10070, CLIENT, dport, data)


@pytest.mark.parametrize("n", [0, 1, -1, 2, -598, 599, -(2**31), 2**31 - 1, 300])
def test_zigzag_varint_round_trip(n):
    v, end = read_varint(varint(zigzag(n)) + b"\xff", 0, 10)
    assert zigzag_decode(v) == n and end == len(varint(zigzag(n)))


def test_varint_rejects_overlong_and_truncated():
    assert read_varint(b"\x80\x80\x80\x80\x80\x01", 0, 6) is None
    assert read_varint(b"\x80", 0, 1) is None


def test_hp_change_and_swing_decode():
    p = payload(0x89D8, msg(683, hp_body(PLAYER, -598, DUMMY)), msg(684, swing_body(PET, DUMMY)))
    hp, sw = decode_datagram(dg(p))
    assert isinstance(hp, HpChange) and isinstance(sw, Swing)
    assert (hp.source, hp.amount, hp.target, hp.seq, hp.extended, hp.conn, hp.header_low16) == (
        PLAYER, -598, DUMMY, 683, False, 24159, 0x89D8)
    assert (sw.attacker, sw.target, sw.extra, sw.seq) == (PET, DUMMY, 768000, 684)


def test_extended_flag_means_longer_than_base():
    p = payload(1, msg(1, hp_body(PLAYER, -25, DUMMY, b"\x00\x01")), msg(2, hp_body(PLAYER, -25, DUMMY)))
    a, b = decode_datagram(dg(p))
    assert a.extended and not b.extended


def test_heal_is_positive():
    (r,) = decode_datagram(dg(payload(1, msg(1, hp_body(DUMMY, 300, PLAYER)))))
    assert r.amount == 300


def test_message_must_fit_before_checksum_and_unknown_opcodes_skipped():
    good = msg(1, hp_body(PLAYER, -5, DUMMY))
    truncated = good[:-3]
    assert decode_datagram(dg(b"\x01\x00" + truncated + b"\xde\xad\xbe\xef")) == []
    # message that would eat into the 4-byte checksum is rejected
    assert decode_datagram(dg(b"\x01\x00" + good[:-4] + b"\x00" * 4)) == []
    unknown = msg(9, b"\x60\x00" + b"\x01" * 6)
    recs = decode_datagram(dg(payload(1, unknown, good)))
    assert [r.amount for r in recs] == [-5]
    assert decode_datagram(dg(b"")) == [] and decode_datagram(dg(b"\x01\x02\x03")) == []


def test_wrong_direction_ignored_and_redelivery_deduped_per_connection():
    d = PacketDecoder(SERVER)
    p = payload(1, msg(10, hp_body(PLAYER, -5, DUMMY)))
    assert len(d.feed(dg(p, 1.0))) == 1
    assert d.feed(dg(p, 2.0)) == []                      # server re-send
    assert len(d.feed(dg(p, 2.0, dport=5555))) == 1      # other connection has its own table
    assert d.feed(dg(p, 3.0, src=CLIENT)) == []          # client->server never decoded
    p2 = payload(1, msg(11, hp_body(PLAYER, -5, DUMMY)))  # same body, new seq: a real second hit
    assert len(d.feed(dg(p2, 4.0))) == 1
    assert len(d.feed(dg(p, 1000.0))) == 1               # seq wrapped long after: remembered no more


def test_identical_records_in_one_datagram_deduped():
    m = msg(5, hp_body(PLAYER, -5, DUMMY))
    assert len(PacketDecoder(SERVER).feed(dg(payload(1, m, m)))) == 1


# ---- oracle on the real captures -------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(ORACLE))
def test_real_capture_matches_oracle_exactly(name):
    path = REAL / name
    first = next(iter_raw_datagrams(path)).ts
    dec = PacketDecoder("5.161.217.188")
    got = []
    for d in read_pcapng(path):
        for r in dec.feed(d):
            if isinstance(r, HpChange):
                got.append({
                    "t": round(r.ts - first, 3), "local_port": r.conn, "header_low16": r.header_low16,
                    "seq": r.seq, "source": "0x%08X" % r.source, "amount": r.amount,
                    "target": "0x%08X" % r.target, "extended": r.extended,
                })
    assert got == ORACLE[name]["hp_records"]
    assert len(got) == (13 if name.startswith("cap1") else 204)
