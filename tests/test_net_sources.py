"""PcapReplaySource / LivePacketSource / Engine Event support (session B)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from _net_builders import CLIENT, SERVER, eth, hp_body, ip_udp, msg, payload, pcapng

from eqoa_parser.config import load_config
from eqoa_parser.events import Event
from eqoa_parser.net.pcapng import UdpDatagram
from eqoa_parser.net.sources import LivePacketSource, PcapReplaySource
from eqoa_parser.pipeline import Engine
from eqoa_parser.sources import HEARTBEAT, LogSource

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
P, D = 0x13FF89D8, 0x01312F0A
H = P & 0xFFFF


def cfg(tmp_path, **kw):
    return load_config(unmatched_log=str(tmp_path / "u.log"), **kw)


def hit_frame(seq, amount, port=24159):
    return eth(ip_udp(SERVER, 10070, CLIENT, port, payload(H, msg(seq, hp_body(P, -amount, D)))))


def write_pcap(tmp_path, frames):
    p = tmp_path / "x.pcapng"
    p.write_bytes(pcapng(frames))
    return p


def test_cap1_through_engine_one_fight_13_hits_7218(tmp_path):
    c = cfg(tmp_path)
    closed = []
    e = Engine(c, PcapReplaySource(REAL / "cap1_necro_spells.pcapng", c), on_closed=closed.append)
    e.run()
    assert len(closed) == 1
    f = closed[0]
    assert (f.you.hits, f.you.total, f.combined_total) == (13, sum([598, 598, 599, 599, 599, 444, 599, 599, 598, 460, 460, 467, 598]), 7218)
    assert f.target == "0x01312F0A" and f.damage_taken == 0 and f.you.max_hit == 599


def test_cap2_engine_pet_and_you(tmp_path):
    c = cfg(tmp_path)
    c.net.local_port = 20605
    e = Engine(c, PcapReplaySource(REAL / "cap2_bard_melee_necro_pet.pcapng", c))
    e.run()
    assert sum(f.pet.hits for f in e.closed_fights) == 36
    assert sum(f.pet.total for f in e.closed_fights) == 27 * 25
    assert sum(f.you.hits for f in e.closed_fights) == 0


def test_replay_source_protocol_and_event_ts(tmp_path):
    c = cfg(tmp_path)
    p = write_pcap(tmp_path, [(100.0, hit_frame(1, 5)), (101.5, hit_frame(2, 6))])
    src = PcapReplaySource(p, c)
    assert isinstance(src, LogSource)
    items = list(src)
    assert [(round(ts, 3), ev.amount) for ts, ev in items] == [(100.0, 5), (101.5, 6)]
    assert all(isinstance(ev, Event) and ev.ts == ts for ts, ev in items)


def test_replay_speed_scaled_with_heartbeats(tmp_path):
    c = cfg(tmp_path)
    p = write_pcap(tmp_path, [(100.0, hit_frame(1, 5)), (100.6, hit_frame(2, 6))])
    src = PcapReplaySource(p, c, speed=2.0, heartbeat_interval=0.05)
    t = time.monotonic()
    out = list(src)
    assert 0.25 <= time.monotonic() - t < 1.5
    assert [i for i in out if i[1] != HEARTBEAT and not isinstance(i[1], str)].__len__() == 2
    hbs = [i for i in out if i[1] == HEARTBEAT]
    assert len(hbs) >= 2 and all(100.0 <= h[0] <= 100.6 for h in hbs)


def test_heartbeat_closes_fight_in_realtime_pcap_replay(tmp_path):
    c = cfg(tmp_path)
    p = write_pcap(tmp_path, [(0.0, hit_frame(1, 5)), (40.0, hit_frame(2, 6))])
    when = []
    t0 = time.monotonic()
    Engine(c, PcapReplaySource(p, c, speed=100.0, heartbeat_interval=0.02),
           on_closed=lambda s: when.append((s.fight_id, time.monotonic() - t0))).run()
    assert [w[0] for w in when] == [1, 2] and when[0][1] < 0.35


def test_close_stops_replay(tmp_path):
    c = cfg(tmp_path)
    p = write_pcap(tmp_path, [(0.0, hit_frame(1, 5)), (3600.0, hit_frame(2, 6))])
    src = PcapReplaySource(p, c, speed=1.0, heartbeat_interval=0.05)
    threading.Timer(0.2, src.close).start()
    t = time.monotonic()
    out = list(src)
    assert time.monotonic() - t < 3 and sum(1 for _, i in out if isinstance(i, Event)) == 1


class ListSource:
    def __iter__(self):
        return iter(())

    def close(self):
        pass


def test_engine_event_items_skip_parser_and_heal_never_starts_fight(tmp_path):
    c = cfg(tmp_path)
    e = Engine(c, ListSource())
    live = []
    e._on_live = live.append
    heal = Event(0.0, "you", "you", 40, None, "heal", "")
    e.step(0.0, heal)
    assert e.current_summary() is None and live == [] and e.parser.unmatched_count == 0
    dmg = Event(1.0, "you", "x", 10, None, "damage", "")
    e.step(1.0, dmg)
    e.step(2.0, heal)
    assert len(live) == 2 and live[-1].combined_total == 10
    e.step(20.0, HEARTBEAT)
    assert len(e.closed_fights) == 1 and e.closed_fights[0].end_ts == 1.0


# ---- live source with a fake capture --------------------------------------------------------------
class FakeCapture:
    def __init__(self, items):
        self.items = list(items)
        self.closed = False
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        self.closed = True

    def read(self):
        if self.items:
            return self.items.pop(0)
        time.sleep(0.01)
        return None

    def close(self):
        self.closed = True


def test_live_source_yields_events_and_heartbeats_and_close_stops(tmp_path):
    c = cfg(tmp_path)
    data = payload(H, msg(1, hp_body(P, -5, D)))
    fake = FakeCapture([UdpDatagram(50.0, SERVER, 10070, CLIENT, 24159, data), None])
    src = LivePacketSource(c, capture_factory=lambda net: fake)
    it = iter(src)
    ts, ev = next(it)
    assert ts == 50.0 and ev.amount == 5 and ev.actor == "you" and fake.entered
    ts2, hb = next(it)
    assert hb == HEARTBEAT and ts2 > 1e9
    src.close()
    assert list(it) == [] and fake.closed
