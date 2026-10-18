"""Independent verification, Phase 2 "after fixes" (session D): tests for the Phase 2 amendments in docs/CONTRACTS.md.

Expectations come from the amendment text ("Phase 2 amendments after verification").  Tests whose docstring starts "BUG-N2-x"
assert contract behaviour and are expected to FAIL if the amendment is incompletely implemented.  Fakes only; no raw socket.
"""
from __future__ import annotations

import dataclasses
import struct
import threading
import time

import pytest

from eqoa_parser import app
from eqoa_parser.config import load_config
from eqoa_parser.events import Event
from eqoa_parser.net.capture import IPv4UdpParser
from eqoa_parser.net.pcapng import iter_raw_datagrams, parse_ip_udp, read_pcapng
from eqoa_parser.net.sources import LivePacketSource, PcapReplaySource
from eqoa_parser.pipeline import Engine
from eqoa_parser.sources import HEARTBEAT
from test_verify_net import (
    BASE, CLIENT, H16, MOB, PLAYER, SERVER, FakeCapture, dg, eth, hp, idb, ip_udp, msg, payload, pcap_bytes, epb, shb,
    server_frame, write,
)


# ============================================================================== truncated frames (amendment: reader and RawCapture agree)
def _snap_capture(frame: bytes, cut: int) -> bytes:
    """A capture whose single frame was cut by `cut` bytes by the capture's snap length (caplen < original length)."""
    return shb() + idb() + epb(1, frame, caplen=len(frame) - cut, orig=len(frame))


@pytest.mark.parametrize("cut", [1, 4, 5, 20, 60])
def test_snaplen_truncated_frames_are_dropped_by_the_pcapng_reader(tmp_path, cut):
    f = server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB)), msg(2, hp(PLAYER, -6, MOB))))
    assert list(iter_raw_datagrams(write(tmp_path, _snap_capture(f, cut)))) == []
    assert list(iter_raw_datagrams(write(tmp_path, pcap_bytes([(BASE, f)]), "full"))) != []     # untruncated control


def test_snaplen_truncation_that_removes_whole_headers_is_also_dropped(tmp_path):
    f = server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))
    for caplen in (0, 5, 14, 20, 34):
        data = shb() + idb() + epb(1, f, caplen=caplen, orig=len(f))
        assert list(iter_raw_datagrams(write(tmp_path, data, f"c{caplen}"))) == []


def test_real_captures_have_no_truncated_frames_so_nothing_real_is_lost():
    for cap, n in (("cap1_necro_spells.pcapng", 13), ("cap2_bard_melee_necro_pet.pcapng", 168)):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "fixtures" / "real"
        got = [e for _, e in PcapReplaySource(root / cap, load_config(), speed=0)]
        assert got                                                                                # still decodes after the amendment


@pytest.mark.parametrize("case", ["ip_total_gt_frame", "udp_len_gt_segment", "frame_cut_but_orig_equals_cap", "ip_total_below_header"])
def test_pcapng_reader_and_rawcapture_agree_on_inconsistent_length_frames(case):
    """BUG-N2-1b (low, residual after the snaplen fix): the amendment says the pcapng reader and RawCapture 'must agree on what is
    dropped'.  They still disagree for a frame whose IP total length / UDP length claims more bytes than the frame holds while the
    capture header says caplen == orig_len (a cut that the capture did not flag): RawCapture drops it as truncated, parse_ip_udp
    keeps the shortened payload (its last four bytes then play the checksum)."""
    pl = payload(H16, msg(1, hp(PLAYER, -5, MOB)))
    good = ip_udp(SERVER, 10070, CLIENT, 20000, pl)
    pkt = {
        "ip_total_gt_frame": ip_udp(SERVER, 10070, CLIENT, 20000, pl, total=len(good) + 40),
        "udp_len_gt_segment": ip_udp(SERVER, 10070, CLIENT, 20000, pl, ulen=8 + len(pl) + 40),
        "frame_cut_but_orig_equals_cap": good[:-5],
        "ip_total_below_header": ip_udp(SERVER, 10070, CLIENT, 20000, pl, total=12),
    }[case]
    by_reader = parse_ip_udp(pkt, 1.0)
    by_capture = IPv4UdpParser(SERVER, lambda: 1.0).feed(pkt)
    assert (by_reader is None) == (by_capture is None), f"reader {'drops' if by_reader is None else 'keeps'}, RawCapture {'drops' if by_capture is None else 'keeps'}"


@pytest.mark.parametrize("case", ["good", "udp_shorter_than_ip", "total_zero"])
def test_pcapng_reader_and_rawcapture_agree_on_the_consistent_cases(case):
    pl = payload(H16, msg(1, hp(PLAYER, -5, MOB)))
    pkt = {"good": ip_udp(SERVER, 10070, CLIENT, 20000, pl), "udp_shorter_than_ip": ip_udp(SERVER, 10070, CLIENT, 20000, pl, ulen=8 + len(pl) - 5),
           "total_zero": ip_udp(SERVER, 10070, CLIENT, 20000, pl, total=0)}[case]
    a, b = parse_ip_udp(pkt, 1.0), IPv4UdpParser(SERVER, lambda: 1.0).feed(pkt)
    assert a is not None and b is not None and a.payload == b.payload


# ============================================================================== time order clamp
def _out_of_order(tmp_path):
    frames = [(BASE + 5, server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))),
              (BASE + 1, server_frame(payload(H16, msg(2, hp(PLAYER, -6, MOB))))),
              (BASE + 9, server_frame(payload(H16, msg(3, hp(PLAYER, -7, MOB))))),
              (BASE + 2, server_frame(payload(H16, msg(4, hp(PLAYER, -8, MOB)))))]
    return write(tmp_path, pcap_bytes(frames))


def test_regressing_datagram_ts_is_clamped_in_both_tuple_and_event(tmp_path, capsys):
    src = PcapReplaySource(_out_of_order(tmp_path), load_config(), speed=0)
    items = list(src)
    assert [t for t, _ in items] == [BASE + 5, BASE + 5, BASE + 9, BASE + 9]
    assert [e.ts for _, e in items] == [t for t, _ in items]              # Event.ts clamped too, not only the tuple
    assert [e.amount for _, e in items] == [5, 6, 7, 8]                    # nothing dropped or reordered
    assert src.clamped == 2
    assert capsys.readouterr().err.count("regresses") == 1                 # one warning, further clamps counted


def test_clamping_leaves_ordered_captures_untouched(tmp_path):
    frames = [(BASE + i, server_frame(payload(H16, msg(i, hp(PLAYER, -5 - i, MOB))))) for i in range(4)]
    src = PcapReplaySource(write(tmp_path, pcap_bytes(frames)), load_config(), speed=0)
    assert [round(t - BASE, 3) for t, _ in src] == [0, 1, 2, 3] and src.clamped == 0


def test_clamped_replay_does_not_merge_fights_that_the_true_order_separates(tmp_path):
    # true order: hit at +0, hit at +20, then a late-stamped datagram at +1: clamped to +20, so the second fight is not polluted
    frames = [(BASE, server_frame(payload(H16, msg(1, hp(PLAYER, -5, MOB))))), (BASE + 20, server_frame(payload(H16, msg(2, hp(PLAYER, -6, MOB))))),
              (BASE + 1, server_frame(payload(H16, msg(3, hp(PLAYER, -7, MOB)))))]
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    closed = []
    Engine(cfg, PcapReplaySource(write(tmp_path, pcap_bytes(frames)), cfg, speed=0), on_closed=closed.append).run()
    assert [f.you.total for f in closed] == [5, 13] and closed[1].duration == 0


def test_clamp_with_speed_positive_still_paces_and_is_monotonic(tmp_path):
    src = PcapReplaySource(_out_of_order(tmp_path), load_config(), speed=200.0, heartbeat_interval=0.02)
    ts = [t for t, _ in src]
    assert ts == sorted(ts)


# ============================================================================== not a pcapng / notices
def test_not_a_pcapng_gui_launch_fails_before_any_window(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    bad = tmp_path / "bad.pcapng"
    bad.write_bytes(b"not a capture file at all" * 5)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f'patterns_file: "{(root / "patterns.yaml").as_posix()}"\nunmatched_log: "{(tmp_path / "u.log").as_posix()}"\n', encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "eqoa_parser", "--config", str(cfg), "--pcap", str(bad), "--runs-dir", str(tmp_path / "runs")],
                       cwd=root, capture_output=True, text=True, timeout=30)             # no --no-gui: would open a window if it got that far
    assert r.returncode == 2 and r.stderr.startswith("error:") and not (tmp_path / "runs").exists()


class _Sink:
    def __init__(self):
        self.errors, self.conns, self.closed, self.finished = [], [], [], []

    def on_live(self, s): pass
    def on_closed(self, s, run): self.closed.append(s)
    def on_finished(self, run): self.finished.append(run)
    def on_error(self, text): self.errors.append(text)
    def on_connection(self, text): self.conns.append(text)


def _runtime(tmp_path, source, cfg):
    store = app.RunStore(str(tmp_path / "runs"), "pcap:test")
    sink = _Sink()
    return app.Runtime(cfg, source, store, sink), sink


def test_notice_reaches_the_sink_for_unusable_captures_but_not_for_good_ones(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
    empty = tmp_path / "empty.pcapng"
    empty.write_bytes(b"")
    rt, sink = _runtime(tmp_path, PcapReplaySource(empty, cfg, speed=0), cfg)
    rt.engine.run()
    assert len(sink.errors) == 1 and "no game-server" in sink.errors[0] and sink.finished
    from pathlib import Path

    good = Path(__file__).resolve().parent.parent / "fixtures" / "real" / "cap1_necro_spells.pcapng"
    rt2, sink2 = _runtime(tmp_path / "g", PcapReplaySource(good, cfg, speed=0), cfg) if (tmp_path / "g").mkdir() is None else (None, None)
    rt2.engine.run()
    assert sink2.errors == [] and len(sink2.closed) == 1 and len(sink2.conns) == 1


def test_notice_when_only_strangers_fight(tmp_path):
    """Capture with server traffic whose only combat is other entities' (all dropped): the user gets the 'no game-server combat' notice."""
    frames = [(BASE + i, server_frame(payload(H16, msg(i, hp(0x01111111, -9, 0x02222222))))) for i in range(5)]
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
    src = PcapReplaySource(write(tmp_path, pcap_bytes(frames)), cfg, speed=0)
    rt, sink = _runtime(tmp_path, src, cfg)
    rt.engine.run()
    assert len(sink.errors) == 1 and src.has_game_traffic and src.records == 5
    assert src.router.attributor(20000).dropped_unrelated == 5


def test_live_source_never_prints_the_file_notice(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
    src = LivePacketSource(cfg, capture_factory=lambda net: FakeCapture(net, []))
    rt, sink = _runtime(tmp_path, src, cfg)
    stop = threading.Event()
    th = threading.Thread(target=rt.engine.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.2)
    stop.set()
    src.close()
    th.join(timeout=3)
    assert sink.errors == []


# ============================================================================== live idle clock
def _busy(script_fn):
    class Cap(FakeCapture):
        def read(self):
            self.read_calls += 1
            return script_fn(self)
    return lambda net: Cap(net)


def test_every_eventless_datagram_is_followed_by_a_heartbeat_with_non_decreasing_ts():
    noise = payload(H16, msg(9, bytes.fromhex("7b0a") + bytes(12)))
    bodies = [payload(H16, msg(1, hp(PLAYER, -5, MOB))), noise, noise, payload(H16, msg(2, hp(MOB, -9, PLAYER)))]

    def read(cap):
        return dg(time.time(), bodies.pop(0)) if bodies else (time.sleep(0.01) or None)     # stamped at read time, like RawCapture

    cfg = load_config()
    src = LivePacketSource(cfg, capture_factory=_busy(read))
    items = []
    for it in src:
        items.append(it)
        if len(items) >= 9:
            src.close()
    kinds = ["E" if isinstance(r, Event) else "H" for _, r in items]
    # E (event datagram, no beat needed), H H (two combat-less datagrams), E, H (read timeout / idle)
    assert kinds[:5] == ["E", "H", "H", "E", "H"], kinds
    ts = [t for t, _ in items]
    assert ts == sorted(ts), "heartbeat/event timestamps went backwards"


def test_heartbeat_at_least_every_poll_interval_even_during_a_steady_stream_of_events():
    cfg = load_config()
    cfg.net.poll_interval = 0.1
    n = [0]

    def read(cap):
        n[0] += 1
        time.sleep(0.02)
        return dg(time.time(), payload(H16, msg(n[0] % 65536, hp(PLAYER, -(n[0] % 400) - 1, MOB))))

    src = LivePacketSource(cfg, capture_factory=_busy(read))
    t0 = time.monotonic()
    beats, evs = [], 0
    for ts, it in src:
        if isinstance(it, Event):
            evs += 1
        else:
            beats.append(time.monotonic() - t0)
        if time.monotonic() - t0 > 0.7:
            src.close()
    assert evs >= 20 and len(beats) >= 4, (evs, beats)
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    assert max(gaps) < 0.1 + 0.08


def _two_emulator_live(tmp_path, other_fights: bool, seconds: float):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), idle_timeout=0.4)
    other = 0x1751CB82
    state = {"n": 0}

    def read(cap):
        state["n"] += 1
        if state["n"] == 1:
            return dg(time.time(), payload(H16, msg(1, hp(PLAYER, -5, MOB))), 20000)
        time.sleep(0.01)
        if other_fights:                                      # the other emulator keeps fighting
            return dg(time.time(), payload(other & 0xFFFF, msg(state["n"] % 65536, hp(other, -9, MOB))), 20001)
        noise = payload(other & 0xFFFF, msg(state["n"] % 65536, hp(0x01111111, -9, 0x02222222)))     # only strangers' combat
        return dg(time.time(), noise, 20001)

    src = LivePacketSource(cfg, capture_factory=_busy(read))
    closed, stop = [], threading.Event()
    eng = Engine(cfg, src, on_closed=closed.append)
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(seconds)
    n = len(closed)
    snapshot = eng.current_summary()
    stop.set()
    src.close()
    th.join(timeout=3)
    return n, closed, snapshot, src


def test_fight_closes_while_another_connection_is_busy_with_unrelated_combat(tmp_path):
    """Phase 3: a second connection that only sees strangers fight (all dropped) must not keep your fight open."""
    n, closed, _snap, src = _two_emulator_live(tmp_path, other_fights=False, seconds=1.2)
    assert n == 1 and closed[0].you.total == 5 and src.own_actors() == ["you"]


def test_second_followed_character_fighting_keeps_the_shared_fight_open_and_is_counted(tmp_path):
    """Phase 3 (documented behaviour): one Segmenter serves all followed characters, so while the second character keeps hitting the
    same mob, the one shared fight stays open (no early close) and both characters' damage lands in it."""
    n, closed, cur, src = _two_emulator_live(tmp_path, other_fights=True, seconds=1.0)
    assert n == 0
    assert src.own_actors() == ["you", "char:20001"]                                # non-extended hits -> the character itself
    assert cur is not None and cur.actor("char:20001").total >= 9 * 20 and cur.you.total == 5
    assert cur.combined_total == cur.you.total + cur.actor("char:20001").total      # combined covers every own actor


def test_live_stream_with_only_unrelated_combat_produces_no_fights_and_keeps_ticking(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), idle_timeout=0.3)

    def read(cap):
        time.sleep(0.01)
        return dg(time.time(), payload(H16, msg(int(time.time() * 1000) % 65536, hp(0x01111111, -9, 0x02222222))))

    src = LivePacketSource(cfg, capture_factory=_busy(read))
    closed, stop = [], threading.Event()
    eng = Engine(cfg, src, on_closed=closed.append)
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.6)
    stop.set()
    src.close()
    th.join(timeout=3)
    assert closed == [] and eng.current_summary() is None
    assert src.router.attributor(20000) is not None and src.router.attributor(20000).dropped_unrelated > 10


# ============================================================================== amended config validation (net)
@pytest.mark.parametrize("key,val,ok", [
    ("local_port", 1, True), ("local_port", 65535, True), ("local_port", None, True), ("local_port", 0, False), ("local_port", 65536, False),
    ("local_port", -1, False), ("local_port", True, False), ("server_ip", "5.161.217.188", True), ("server_ip", "256.1.1.1", False),
    ("server_ip", "1.2.3", False), ("server_ip", "::1", False), ("server_ip", "example.com", False), ("server_ip", " 1.2.3.4", False),
    ("interface_ip", None, True), ("interface_ip", "10.0.0.5", True), ("interface_ip", "banana", False), ("interface_ip", "10.0.0.5/24", False),
])
def test_net_config_validation(tmp_path, key, val, ok):
    """Was BUG-N2-4.  AMENDED: local_port 1-65535 or null; server_ip / interface_ip IPv4 (interface_ip may be null)."""
    import yaml

    from eqoa_parser.config import load_config as lc

    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"net": {key: val}}), encoding="utf-8")
    if ok:
        assert getattr(lc(p).net, key) == val
    else:
        with pytest.raises(ValueError):
            lc(p)
