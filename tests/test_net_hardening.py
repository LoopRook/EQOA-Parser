"""Phase 2 post-verification fixes (session B): truncated frames, ts clamp, header check, live idle clock."""
from __future__ import annotations

import struct
import threading
import time

import pytest
from _net_builders import CLIENT, SERVER, _block, eth, hp_body, ip_udp, msg, payload, pcapng

from eqoa_parser.config import load_config
from eqoa_parser.net.pcapng import UdpDatagram, read_pcapng
from eqoa_parser.net.sources import LivePacketSource, PcapReplaySource
from eqoa_parser.pipeline import Engine
from eqoa_parser.sources import HEARTBEAT

P, D = 0x13FF89D8, 0x01312F0A
H = P & 0xFFFF


def frame(seq, amount=5, ip=SERVER):
    return eth(ip_udp(ip, 10070, CLIENT, 24159, payload(H, msg(seq, hp_body(P, -amount, D)))))


def test_snaplen_truncated_epb_dropped(tmp_path):
    blob = pcapng([(1.0, frame(1)), (2.0, frame(2))])
    # rewrite the second EPB's original length to be larger than its captured length
    pos = blob.rindex(struct.pack("<I", 6) + struct.pack("<I", 0)[:0])  # last EPB block type 6
    orig_off = pos + 8 + 16
    bad = bytearray(blob)
    cap = struct.unpack_from("<I", bad, orig_off - 4)[0]
    struct.pack_into("<I", bad, orig_off, cap + 20)
    f = tmp_path / "t.pcapng"
    f.write_bytes(bytes(bad))
    assert len(list(read_pcapng(f))) == 1


def test_replay_clamps_regressing_ts(tmp_path, capsys):
    f = tmp_path / "t.pcapng"
    f.write_bytes(pcapng([(105.0, frame(1)), (101.0, frame(2, 6)), (106.0, frame(3, 7))]))
    src = PcapReplaySource(f, load_config())
    ts = [t for t, _ in src]
    assert ts == [105.0, 105.0, 106.0] and src.clamped == 1
    assert "clamped" in capsys.readouterr().err


def test_not_a_pcapng_raises_at_construction(tmp_path):
    f = tmp_path / "bad"
    f.write_bytes(b"this is definitely not a capture file")
    with pytest.raises(ValueError):
        PcapReplaySource(f, load_config())
    bad_magic = tmp_path / "bad2"
    bad_magic.write_bytes(struct.pack("<II", 0x0A0D0D0A, 28) + b"\x00" * 20)
    with pytest.raises(ValueError):
        PcapReplaySource(bad_magic, load_config())


def test_empty_and_truncated_files_construct_and_yield_nothing(tmp_path):
    e = tmp_path / "empty"
    e.write_bytes(b"")
    src = PcapReplaySource(e, load_config())
    assert list(src) == [] and not src.has_game_traffic
    t = tmp_path / "trunc"
    t.write_bytes(pcapng([(1.0, frame(1))])[:10])
    assert list(PcapReplaySource(t, load_config())) == []


def test_no_game_traffic_flag(tmp_path):
    other = eth(ip_udp("8.8.8.8", 53, CLIENT, 20000, b"\x00" * 30))
    f = tmp_path / "t.pcapng"
    f.write_bytes(pcapng([(1.0, other)]))
    src = PcapReplaySource(f, load_config())
    assert list(src) == [] and src.server_datagrams == 0 and not src.has_game_traffic
    f.write_bytes(pcapng([(1.0, frame(1)), (1.1, other)]))
    src = PcapReplaySource(f, load_config())
    assert len(list(src)) == 1 and src.has_game_traffic and (src.datagrams, src.server_datagrams, src.records) == (2, 1, 1)


class Scripted:
    def __init__(self, fn):
        self.fn = fn
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def read(self):
        return self.fn()

    def close(self):
        self.closed = True


NOISE = payload(H, msg(9, b"\x7b\x0a" + b"\x00" * 12))


def test_live_heartbeat_after_datagram_without_event():
    calls = iter([UdpDatagram(1.0, SERVER, 10070, CLIENT, 24159, NOISE)])
    src = LivePacketSource(load_config(), capture_factory=lambda n: Scripted(lambda: next(calls, None)))
    it = iter(src)
    ts, item = next(it)
    assert item == HEARTBEAT and ts > 1e9
    src.close()


def test_live_heartbeat_at_least_every_poll_interval_even_when_events_flow():
    cfg = load_config()
    cfg.net.poll_interval = 0.05
    n = [0]

    def read():
        n[0] += 1
        time.sleep(0.02)
        return UdpDatagram(time.time(), SERVER, 10070, CLIENT, 24159,
                           payload(H, msg(n[0] % 60000, hp_body(P, -n[0], D))))

    src = LivePacketSource(cfg, capture_factory=lambda net: Scripted(read))
    items = []
    t = time.monotonic()
    for it in src:
        items.append(it)
        if time.monotonic() - t > 0.4:
            src.close()
    assert sum(1 for _, i in items if i == HEARTBEAT) >= 3 and sum(1 for _, i in items if i != HEARTBEAT) >= 10


def test_live_fight_closes_while_noise_keeps_flowing(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), idle_timeout=0.3)
    state = {"n": 0}

    def read():
        state["n"] += 1
        time.sleep(0.01)
        pl = payload(H, msg(1, hp_body(P, -5, D))) if state["n"] == 1 else NOISE
        return UdpDatagram(time.time(), SERVER, 10070, CLIENT, 24159, pl)

    src = LivePacketSource(cfg, capture_factory=lambda n: Scripted(read))
    closed, stop = [], threading.Event()
    eng = Engine(cfg, src, on_closed=closed.append)
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(1.0)
    n = len(closed)
    stop.set()
    src.close()
    th.join(3)
    assert n == 1
