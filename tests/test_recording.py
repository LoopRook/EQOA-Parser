"""Phase 6: pcapng writer, marks, the source hook, RecordingController and the GUI buttons."""
from __future__ import annotations

import getpass

import dataclasses
import threading

import pytest
from _net_builders import CLIENT, SERVER, hp_body, msg, payload
from test_net_sources import FakeCapture

from eqoa_parser.config import load_config
from eqoa_parser.net.pcapng import UdpDatagram, read_pcapng
from eqoa_parser.net.pcapng_writer import PcapngWriter
from eqoa_parser.net.sources import LivePacketSource, PcapReplaySource
from eqoa_parser.recording import RecordingController

P, D = 0x13FF89D8, 0x01312F0A
H = P & 0xFFFF


def server_dg(ts, seq=1, amount=5, port=24159):
    return UdpDatagram(ts, SERVER, 10070, CLIENT, port, payload(H, msg(seq, hp_body(P, -amount, D))))


def test_round_trip_same_datagrams_and_ts_within_1us(tmp_path):
    dgs = [UdpDatagram(1_700_000_000.123456 + i * 0.37, SERVER, 10070, CLIENT, 24159, bytes([i]) * (i + 1))
           for i in range(7)]
    w = PcapngWriter(tmp_path / "a.pcapng", SERVER)
    assert all(w.write(d) for d in dgs)
    w.close()
    w.close()  # idempotent
    got = list(read_pcapng(tmp_path / "a.pcapng"))
    assert len(got) == len(dgs) == w.packets
    for a, b in zip(dgs, got):
        assert (a.src_ip, a.src_port, a.dst_ip, a.dst_port, a.payload) == (
            b.src_ip, b.src_port, b.dst_ip, b.dst_port, b.payload)
        assert abs(a.ts - b.ts) <= 1e-6


def test_client_to_server_never_written(tmp_path):
    w = PcapngWriter(tmp_path / "a.pcapng", SERVER)
    up = UdpDatagram(5.0, CLIENT, 24159, SERVER, 10070, b"login:SECRETNAME")
    assert w.write(up) is False
    assert w.write(UdpDatagram(5.0, "10.0.0.9", 1, CLIENT, 2, b"other")) is False
    assert w.write(server_dg(6.0)) is True
    w.close()
    data = (tmp_path / "a.pcapng").read_bytes()
    assert b"SECRETNAME" not in data and w.packets == 1
    assert [d.ts for d in read_pcapng(tmp_path / "a.pcapng")] == [pytest.approx(6.0, abs=1e-6)]


def test_write_after_close_is_ignored(tmp_path):
    w = PcapngWriter(tmp_path / "a.pcapng", SERVER)
    w.close()
    assert w.write(server_dg(1.0)) is False


def test_file_has_no_os_or_host_strings(tmp_path):
    w = PcapngWriter(tmp_path / "a.pcapng", SERVER)
    w.write(server_dg(1.0))
    w.close()
    data = (tmp_path / "a.pcapng").read_bytes()
    # SHB: 28 bytes, no options; IDB: 20 bytes, no options
    assert int.from_bytes(data[4:8], "little") == 28
    assert int.from_bytes(data[28 + 4:28 + 8], "little") == 20
    for needle in (b"Windows", b"Microsoft", b"Intel", getpass.getuser().encode(), b"pktmon", b"Python", b"Users"):
        assert needle not in data


def test_readable_before_close(tmp_path):
    w = PcapngWriter(tmp_path / "a.pcapng", SERVER)
    for i in range(3):
        w.write(server_dg(1.0 + i, seq=i + 1))
    w.mark("x")  # marks flush the capture, so a crash here still leaves these packets
    assert len(list(read_pcapng(tmp_path / "a.pcapng"))) == 3
    w.close()


def test_marks_file_format(tmp_path):
    w = PcapngWriter(tmp_path / "EQOA-1.pcapng", SERVER)
    w.mark("boss\tpull\nnow", ts=1_700_000_000.5)
    w.mark("second")
    w.close()
    lines = (tmp_path / "EQOA-1.pcapng.marks.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    iso, ts, text = lines[0].split("\t")
    assert float(ts) == 1_700_000_000.5 and text == "boss pull now" and iso[4] == "-" and "T" in iso
    assert lines[1].split("\t")[2] == "second"


def test_thread_safe_writes(tmp_path):
    w = PcapngWriter(tmp_path / "a.pcapng", SERVER)

    def work(k):
        for i in range(200):
            w.write(server_dg(k * 1000 + i, seq=i + 1))

    ts = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    w.close()
    assert w.packets == 800 and len(list(read_pcapng(tmp_path / "a.pcapng"))) == 800


def _cfg(tmp_path):
    return load_config(unmatched_log=str(tmp_path / "u.log"))


def _drain_events(src, n=4):
    out = []
    for ts, ev in src:
        if ev != "":
            out.append((ts, ev))
        if len(out) >= n:
            break
    src.close()
    return out


def test_record_fake_live_then_replay_gives_same_events(tmp_path):
    c = _cfg(tmp_path)
    dgs = [server_dg(1000.25 + i, seq=i + 1, amount=10 + i) for i in range(4)]
    dgs.insert(2, UdpDatagram(1001.0, CLIENT, 24159, SERVER, 10070, b"client side"))
    fake = FakeCapture(dgs)
    live = LivePacketSource(c, capture_factory=lambda net: fake)
    rec = RecordingController(live, tmp_path / "captures", SERVER)
    path = rec.start()
    assert path.parent == tmp_path / "captures" and path.name.startswith("EQOA-") and path.suffix == ".pcapng"
    live_events = _drain_events(live)
    assert rec.active and rec.packets == 4
    assert rec.status_text() == f"Recording: {path.name} (4 packets)"
    rec.stop()
    assert not rec.active and rec.status_text() is None

    replay_events = [(ts, ev) for ts, ev in PcapReplaySource(path, _cfg(tmp_path)) if ev != ""]
    assert len(replay_events) == len(live_events) == 4
    for (t1, e1), (t2, e2) in zip(live_events, replay_events):
        assert t1 == pytest.approx(t2, abs=1e-6)
        assert dataclasses.replace(e1, ts=0) == dataclasses.replace(e2, ts=0)


def test_detach_stops_recording_and_broken_recorder_is_dropped(tmp_path):
    live = LivePacketSource(_cfg(tmp_path), capture_factory=lambda net: FakeCapture([]))
    w = PcapngWriter(tmp_path / "a.pcapng", SERVER)
    live.attach_recorder(w)
    live.events_from(server_dg(1.0))
    live.detach_recorder()
    live.events_from(server_dg(2.0, seq=2))
    assert w.packets == 1

    class Boom:
        def write(self, dg):
            raise RuntimeError("x")

    live.attach_recorder(Boom())
    live.events_from(server_dg(3.0, seq=3))  # must not raise
    assert live._recorder is None
    w.close()


def test_controller_mark_requires_recording(tmp_path):
    live = LivePacketSource(_cfg(tmp_path), capture_factory=lambda net: FakeCapture([]))
    rec = RecordingController(live, tmp_path, SERVER)
    assert rec.mark("x") is False
    path = rec.start()
    assert rec.start() == path  # second start is a no-op
    assert rec.mark("hello") is True
    rec.stop()
    assert rec.mark("late") is False
    assert path.with_name(path.name + ".marks.txt").read_text(encoding="utf-8").count("\n") == 1


def test_record_flag_needs_live():
    from eqoa_parser.app import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--pcap", "x.pcapng", "--record"])
    assert parse_args(["--live", "--record"]).record == ""
    assert parse_args(["--live", "--record", "f.pcapng"]).record == "f.pcapng"
    assert parse_args(["--live"]).record is None


# ---- GUI buttons (skip without a display) ------------------------------------------------------
pytest.importorskip("tkinter")
from test_ui_tk import app_factory, shared_root  # noqa: E402,F401  (fixtures)


def _state(btn):
    return str(btn.cget("state"))


def test_buttons_disabled_without_recording(app_factory):
    app = app_factory(source_label="replay:x")
    assert _state(app.record_button) == "disabled" and _state(app.mark_button) == "disabled"
    app.toggle_record()
    app.mark_clicked()  # no-ops


def test_buttons_enable_logic_and_status(app_factory, tmp_path):
    live = LivePacketSource(_cfg(tmp_path), capture_factory=lambda net: FakeCapture([]))
    rec = RecordingController(live, tmp_path / "captures", SERVER)
    app = app_factory(source_label="live:capture")
    app.set_recording(rec)
    assert app.record_button.cget("text") == "Record"
    assert _state(app.record_button) == "normal" and _state(app.mark_button) == "disabled"

    app.toggle_record()
    assert rec.active and app.record_button.cget("text") == "Stop recording"
    assert _state(app.mark_button) == "normal"
    assert "Recording: EQOA-" in app.status_var.get() and "(0 packets)" in app.status_var.get()

    live.events_from(server_dg(1.0))
    app._sync_record()
    assert "(1 packets)" in app.status_var.get()

    marks_path = rec.path.with_name(rec.path.name + ".marks.txt")
    app.ask_note = lambda: "pet died"
    app.mark_clicked()
    marks = marks_path.read_text(encoding="utf-8")
    assert marks.strip().endswith("pet died")
    app.ask_note = lambda: None  # cancelled: nothing more written
    app.mark_clicked()
    assert marks == marks_path.read_text(encoding="utf-8")

    app.toggle_record()
    assert not rec.active and app.record_button.cget("text") == "Record"
    assert _state(app.mark_button) == "disabled" and "Recording" not in app.status_var.get()
