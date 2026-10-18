"""pcapng reader tests (session B)."""
from __future__ import annotations

import pytest
from _net_builders import CLIENT, SERVER, eth, ip_udp, pcapng

from eqoa_parser.net.pcapng import read_pcapng


def dgram_bytes(data=b"hello", sport=10070, dport=24159, src=SERVER, dst=CLIENT, **kw):
    return ip_udp(src, sport, dst, dport, data, **kw)


def write(tmp_path, blob):
    p = tmp_path / "t.pcapng"
    p.write_bytes(blob)
    return p


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("link,wrap", [(1, eth), (101, lambda ip: ip), (228, lambda ip: ip)])
def test_link_types_and_endianness(tmp_path, endian, link, wrap):
    p = write(tmp_path, pcapng([(1700000000.25, wrap(dgram_bytes(b"abc")))], link=link, endian=endian))
    (d,) = list(read_pcapng(p))
    assert (d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload) == (SERVER, 10070, CLIENT, 24159, b"abc")
    assert d.ts == pytest.approx(1700000000.25, abs=1e-5)


@pytest.mark.parametrize("res", [3, 9, 0x80 | 10])
def test_timestamp_resolution_option(tmp_path, res):
    p = write(tmp_path, pcapng([(1000.5, eth(dgram_bytes()))], tsresol=res))
    assert next(read_pcapng(p)).ts == pytest.approx(1000.5, abs=1e-3)


def test_skips_non_ipv4_non_udp_fragments_and_unknown_links(tmp_path):
    frames = [
        (1.0, eth(dgram_bytes(), ethertype=0x86DD)),               # IPv6 ethertype
        (2.0, eth(dgram_bytes(mf=True))),                          # fragment
        (3.0, eth(bytes([0x45]) + dgram_bytes()[1:9] + bytes([6]) + dgram_bytes()[10:])),  # TCP
        (4.0, b"\x00\x01"),                                       # runt
        (5.0, eth(dgram_bytes(b"ok"), vlan=True)),                 # VLAN tagged: accepted
    ]
    out = list(read_pcapng(write(tmp_path, pcapng(frames))))
    assert [d.payload for d in out] == [b"ok"]
    assert list(read_pcapng(write(tmp_path, pcapng(frames, link=147)))) == []


def test_ethernet_padding_is_trimmed(tmp_path):
    frame = eth(dgram_bytes(b"xy")) + b"\x00" * 10
    assert next(read_pcapng(write(tmp_path, pcapng([(1.0, frame)])))).payload == b"xy"


def test_pktmon_duplicates_dropped_within_50ms_only(tmp_path):
    f = eth(dgram_bytes(b"same"))
    other = eth(dgram_bytes(b"other"))
    frames = [(10.000, f), (10.001, f), (10.002, f), (10.003, other), (10.040, f), (10.200, f)]
    out = list(read_pcapng(write(tmp_path, pcapng(frames))))
    assert [(round(d.ts, 3), d.payload) for d in out] == [(10.0, b"same"), (10.003, b"other"), (10.2, b"same")]


def test_distinct_directions_are_not_duplicates(tmp_path):
    a = eth(dgram_bytes(b"x"))
    b = eth(dgram_bytes(b"x", sport=24159, dport=10070, src=CLIENT, dst=SERVER))
    assert len(list(read_pcapng(write(tmp_path, pcapng([(1.0, a), (1.001, b)]))))) == 2


def test_truncated_tail_and_bad_file(tmp_path):
    blob = pcapng([(1.0, eth(dgram_bytes(b"a"))), (2.0, eth(dgram_bytes(b"b")))])
    assert len(list(read_pcapng(write(tmp_path, blob[:-7])))) == 1
    with pytest.raises(ValueError):
        list(read_pcapng(write(tmp_path, b"not a pcapng file at all")))
