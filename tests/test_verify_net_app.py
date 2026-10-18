"""Independent verification, Phase 2 (session D): CLI / app wiring of --pcap, --live (packets), --connection.

--live is exercised ONLY with a fake RawCapture patched into eqoa_parser.net.capture; no raw socket is ever opened.
Tests whose docstring starts "BUG-N2-x" are expected to fail against the current code (see docs/VERIFICATION.md).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SHB_SIG = bytes.fromhex("0a0d0d0a")
NOT_PCAP = {
    "garbage": bytes(range(256)) * 3,
    "text": b"this is not a capture " * 20,
    "one-line": b"hello world, not a capture",
    "classic-pcap": bytes.fromhex("d4c3b2a1020004000000000000000000ffff000001000000") + bytes(64),
}

from eqoa_parser import app
from eqoa_parser.net import capture as capture_mod
from eqoa_parser.net.capture import CaptureError
from test_verify_net import (
    BASE, H16, MOB, PLAYER, dg, hp, msg, payload, pcap_bytes, server_frame, write,
)

ROOT = Path(__file__).resolve().parent.parent
CAP2 = ROOT / "fixtures" / "real" / "cap2_bard_melee_necro_pet.pcapng"
CAP1 = ROOT / "fixtures" / "real" / "cap1_necro_spells.pcapng"
PATTERNS = ROOT / "patterns.yaml"


def write_cfg(tmp_path, extra=""):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f'patterns_file: "{PATTERNS.as_posix()}"\nunmatched_log: "{(tmp_path / "u.log").as_posix()}"\n{extra}', encoding="utf-8")
    return cfg


def cli(tmp_path, *args, extra="", timeout=90):
    cmd = [sys.executable, "-m", "eqoa_parser", "--config", str(write_cfg(tmp_path, extra)), "--runs-dir", str(tmp_path / "runs"), *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


# ============================================================================== --pcap end to end
def test_cli_pcap_cap2_default_follows_both_emulators(tmp_path):
    """PHASE 3 acceptance on cap2 with default settings: fight 1 = the bard (primary, `you`) 20,670 over 56 hits; fight 2 = the necro
    connection as a non-primary: its pet 675 over 36 hits under the label rule ('Zecron' when the name table knows it at the first
    Event); `you` 0 there; combined for fight 2 = 675."""
    r = cli(tmp_path, "--pcap", str(CAP2), "--no-gui")
    assert r.returncode == 0 and "Traceback" not in r.stderr
    assert "following connection (client port) 21173, player id 0x174A1A40, as you" in r.stderr
    assert re.search(r"also following connection \(client port\) 20605, player id 0x1751CB82, as \S+", r.stderr)
    assert "Fights: 2" in r.stdout and "Total damage: 21345" in r.stdout
    blocks = chr(10).join(ln for ln in r.stdout.splitlines() if not ln.startswith("[live]"))     # fight blocks without the live lines
    f1 = blocks.split("=== Fight 1")[1].split("=== Fight 2")[0]
    f2 = blocks.split("=== Fight 2")[1].split("=== Run summary")[0]
    assert re.search(r"You\s+total\s+20670", f1) and "damage taken 1176" in f1 and "'s pet" not in f1
    assert re.search(r"You\s+total\s+0 ", f2) and re.search(r"'s pet\s+total\s+675", f2) and re.search(r"Zecron combined\s+total\s+675", f2)
    doc = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    fights = doc["runs"][0]["fights"]
    assert [f["combined_total"] for f in fights] == [20670, 675] and fights[0]["actors"]["you"]["hits"] == 56
    (pet_actor,) = [a for a in fights[1]["actors"] if a.endswith("'s pet")]
    assert fights[1]["actors"][pet_actor]["hits"] == 36 and fights[1]["actors"][pet_actor]["total"] == 675


@pytest.mark.parametrize("port,needle", [("21173", r"You\s+total\s+20670"), ("20605", r"Zecron's pet\s+total\s+675")])
def test_cli_pcap_cap2_explicit_connection_keeps_the_single_emulator_behaviour(tmp_path, port, needle):
    r = cli(tmp_path, "--pcap", str(CAP2), "--no-gui", "--connection", port)
    assert r.returncode == 0 and re.search(needle, r.stdout), r.stdout[-600:]
    assert "following connection (client port)" not in r.stderr          # explicit choice: no auto-pick message
    assert "Fights: 1" in r.stdout and (port == "20605" or "'s pet" not in r.stdout) and "also following" not in r.stderr
    assert re.search(r"\[net\] following: (You|Zecron) \(0x[0-9A-F]{8}, port " + port + r"\)", r.stdout)


def test_cli_pcap_connection_flag_equals_config_local_port(tmp_path):
    a = cli(tmp_path, "--pcap", str(CAP2), "--no-gui", "--connection", "20605")
    b = cli(tmp_path / "b", "--pcap", str(CAP2), "--no-gui", extra="net:\n  local_port: 20605\n") if (tmp_path / "b").mkdir() is None else None
    strip = lambda s: [ln for ln in s.splitlines() if not ln.startswith("[pcap:")]
    assert strip(a.stdout) == strip(b.stdout)


def test_cli_pcap_cap1_matches_user_observed_numbers(tmp_path):
    r = cli(tmp_path, "--pcap", str(CAP1), "--no-gui")
    assert r.returncode == 0 and re.search(r"You\s+total\s+7218", r.stdout) and "max hit   599" in r.stdout
    doc = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    (fight,) = doc["runs"][0]["fights"]
    assert doc["runs"][0]["source"].startswith("pcap:") and fight["actors"]["you"]["hits"] == 13 and fight["target"] == "0x01312F0A"


def test_cli_pcap_show_group_does_not_change_you_pet_numbers(tmp_path):
    a = cli(tmp_path, "--pcap", str(CAP2), "--no-gui", "--connection", "20605")
    (tmp_path / "g").mkdir()
    b = cli(tmp_path / "g", "--pcap", str(CAP2), "--no-gui", "--connection", "20605", "--show-group")
    assert re.search(r"Zecron's pet\s+total\s+675", a.stdout) and re.search(r"Zecron's pet\s+total\s+675", b.stdout)


def test_cli_pcap_twice_appends_runs_and_reloads(tmp_path):
    for expected_prev in (0, 1):
        r = cli(tmp_path, "--pcap", str(CAP1), "--no-gui")
        assert f"{expected_prev} fight(s) from earlier runs today" in r.stdout
    doc = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert [len(x["fights"]) for x in doc["runs"]] == [1, 1]


def test_cli_pcap_missing_file_and_directory_are_clean_errors(tmp_path):
    for target in (tmp_path / "nope.pcapng", tmp_path):
        r = cli(tmp_path, "--pcap", str(target), "--no-gui")
        assert r.returncode == 2 and r.stderr.startswith("error:") and "Traceback" not in r.stderr


@pytest.mark.parametrize("what", ["garbage", "text", "one-line", "classic-pcap"])
def test_cli_pcap_not_a_pcapng_is_a_clean_error_before_the_banner(tmp_path, what):
    """Was BUG-N2-3.  AMENDED contract: not a pcapng -> `error: ...`, exit 2, before the run banner, no traceback, no run file."""
    p = tmp_path / "bad.pcapng"
    p.write_bytes(NOT_PCAP[what])
    r = cli(tmp_path, "--pcap", str(p), "--no-gui")
    assert "Traceback" not in r.stderr, r.stderr.strip().splitlines()[-1]
    assert r.returncode == 2 and r.stderr.startswith("error:") and "pcapng" in r.stderr
    assert r.stdout == "" and not (tmp_path / "runs").exists()               # nothing printed, nothing saved


def test_pcap_replay_source_validates_header_on_construction(tmp_path):
    from eqoa_parser.config import load_config
    from eqoa_parser.net.sources import PcapReplaySource

    bad = tmp_path / "bad.bin"
    bad.write_bytes(SHB_SIG + bytes(4) + bytes.fromhex('deadbeef'))        # right block type, wrong byte-order magic
    with pytest.raises(ValueError, match="pcapng"):
        PcapReplaySource(bad, load_config())
    for body in (b'', SHB_SIG[:1], SHB_SIG + bytes.fromhex('1c00')):          # empty / tiny valid prefixes: accepted, no datagrams
        ok = tmp_path / "ok.bin"
        ok.write_bytes(body)
        assert list(PcapReplaySource(ok, load_config())) == []


@pytest.mark.parametrize("case", ["empty", "truncated", "no-server-traffic", "wrong-server-ip", "wrong-connection"])
def test_cli_pcap_without_usable_game_combat_prints_a_one_line_notice(tmp_path, case):
    """AMENDED contract: empty / truncated / no-server-traffic input prints a one-line stderr notice instead of a silent 'No fights.'
    Exit stays 0 (the run itself is valid)."""
    extra = ""
    conn = []
    if case == "empty":
        p = tmp_path / "x.pcapng"
        p.write_bytes(b"")
    elif case == "truncated":
        p = tmp_path / "x.pcapng"
        p.write_bytes(CAP2.read_bytes()[:3000])
    elif case == "no-server-traffic":
        p = tmp_path / "x.pcapng"
        p.write_bytes(pcap_bytes([(BASE, eth_other())]))
    elif case == "wrong-server-ip":
        p, extra = CAP1, "net:" + chr(10) + "  server_ip: 10.9.9.9" + chr(10)
    else:
        p, conn = CAP1, ["--connection", "33333"]
    r = cli(tmp_path, "--pcap", str(p), "--no-gui", *conn, extra=extra)
    assert r.returncode == 0 and "No fights" in r.stdout and "Traceback" not in r.stderr
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "no game-server" in lines[0].lower(), r.stderr


def test_cli_pcap_notice_absent_when_there_is_combat(tmp_path):
    r = cli(tmp_path, "--pcap", str(CAP1), "--no-gui")
    assert "no game-server" not in r.stderr.lower() and "Fights: 1" in r.stdout


def test_cli_pcap_with_traffic_but_no_player_id_gives_the_generic_notice_only(tmp_path):
    """Observation (passes): a capture whose header word never matches any entity (player never identified) drops every record and
    ends with the same notice as 'no traffic' -- its wording ('empty, truncated, or no traffic') does not describe this case, and
    the dropped count is not shown anywhere."""
    frames = [(BASE + i, server_frame(payload(0x1111, msg(i, hp(PLAYER, -5, MOB))))) for i in range(5)]
    p = tmp_path / "x.pcapng"
    p.write_bytes(pcap_bytes(frames))
    r = cli(tmp_path, "--pcap", str(p), "--no-gui")
    assert r.returncode == 0 and "No fights" in r.stdout and "no game-server combat found" in r.stderr and "dropped" not in r.stderr


def eth_other():
    from test_verify_net import eth, ip_udp

    return eth(ip_udp("8.8.8.8", 53, "192.168.1.156", 20000, b"\x00" * 30))


@pytest.mark.parametrize("extra", ["net:\n  local_port: 99999\n", "net:\n  local_port: 0\n", "net:\n  server_ip: not-an-ip\n",
                                   "net:\n  server_ip: ''\n"])
def test_cli_net_config_values_are_validated(tmp_path, extra):
    """Was BUG-N2-4 (fixed by the amendment): load_config validates types and poll_interval, but not net.local_port range or IPv4 syntax.  An impossible
    local_port or a mistyped server_ip is accepted and the run ends with 'No fights.' and exit 0 (silent)."""
    r = cli(tmp_path, "--pcap", str(CAP1), "--no-gui", extra=extra)
    assert r.returncode == 2 and r.stderr.startswith("error:"), (r.returncode, r.stdout[-200:])


def test_cli_source_flag_combinations(tmp_path):
    for argv in (["--pcap", str(CAP1), "--live"], ["--pcap", str(CAP1), "--replay", "x"], ["--live", "--speed", "2"],
                 ["--live", "--live-pine"], []):
        r = cli(tmp_path, *argv, "--no-gui")
        assert r.returncode == 2 and "exactly one of" in r.stderr or "--speed" in r.stderr, argv


def test_cli_connection_flag_is_silently_ignored_for_text_replay(tmp_path):
    r = cli(tmp_path, "--replay", str(ROOT / "fixtures" / "synthetic_combat.log"), "--no-gui", "--connection", "12345")
    assert r.returncode == 0 and "Fights: 5" in r.stdout


def test_pcap_mode_needs_a_valid_text_patterns_file_even_though_it_never_uses_it(tmp_path):
    """Observation (passes): --pcap still validates patterns.yaml and opens unmatched.log (coupling to the dead text path)."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text('patterns_file: "nope.yaml"\n', encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "eqoa_parser", "--config", str(cfg), "--pcap", str(CAP1), "--no-gui",
                        "--runs-dir", str(tmp_path / "r")], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "nope.yaml" in r.stderr


# ============================================================================== --live with a fake RawCapture (no sockets)
class FakeRawCapture:
    """Patched over eqoa_parser.net.capture.RawCapture.  Class attributes script the behaviour of every instance."""
    script: list = []
    fail_open: Exception | None = None
    instances: list = []

    def __init__(self, net, **kw):
        self.net = net
        self.stop = threading.Event()
        self.opened = self.closed = 0
        FakeRawCapture.instances.append(self)

    def __enter__(self):
        if FakeRawCapture.fail_open:
            raise FakeRawCapture.fail_open
        self.opened += 1
        return self

    def __exit__(self, *a):
        self.close()

    def read(self):
        if FakeRawCapture.script:
            item = FakeRawCapture.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.stop.wait(0.02)
        return None

    def close(self):
        self.closed += 1
        self.stop.set()


@pytest.fixture()
def fake_capture(monkeypatch):
    FakeRawCapture.script, FakeRawCapture.fail_open, FakeRawCapture.instances = [], None, []
    monkeypatch.setattr(capture_mod, "RawCapture", FakeRawCapture)
    return FakeRawCapture


def run_main(tmp_path, *args):
    return app.main(["--config", str(write_cfg(tmp_path)), "--runs-dir", str(tmp_path / "runs"), *args])


def test_live_not_admin_fails_before_banner_with_doc_pointer(tmp_path, fake_capture, capsys):
    fake_capture.fail_open = CaptureError("live capture needs Administrator rights: reopen the terminal")
    assert run_main(tmp_path, "--live", "--no-gui") == 2
    out, err = capsys.readouterr()
    assert err.startswith("error:") and "Administrator" in err and "docs/live-capture.md" in err
    assert out == "" and not (tmp_path / "runs").exists()


def test_live_capture_error_after_startup_exits_2_but_keeps_data(tmp_path, fake_capture, capsys):
    now = time.time()
    fake_capture.script = [dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB)))), dg(now + 0.1, payload(H16, msg(2, hp(PLAYER, -7, MOB)))),
                           CaptureError("capture socket failed: network down")]
    assert run_main(tmp_path, "--live", "--no-gui") == 2
    out, err = capsys.readouterr()
    assert "error:" in err and "network down" in err and "Traceback" not in err
    assert "=== Fight 1" in out and re.search(r"You\s+total\s+12", out) and "=== Run summary ===" in out
    doc = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert doc["runs"][0]["source"] == "live:capture" and len(doc["runs"][0]["fights"]) == 1


def test_live_ctrl_c_flushes_fight_and_exits_0(tmp_path, fake_capture, capsys):
    now = time.time()
    fake_capture.script = [dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB)))), KeyboardInterrupt()]
    assert run_main(tmp_path, "--live", "--no-gui") == 0
    out = capsys.readouterr().out
    assert "=== Fight 1" in out and (tmp_path / "runs").exists()


def test_live_connection_flag_reaches_the_router(tmp_path, fake_capture, capsys):
    now = time.time()
    other = 0x1751CB82
    fake_capture.script = [dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB))), 20000),
                           dg(now + .1, payload(other & 0xFFFF, msg(1, hp(other, -9, MOB))), 20001), CaptureError("stop")]
    assert run_main(tmp_path, "--live", "--no-gui", "--connection", "20001") == 2
    out = capsys.readouterr().out
    assert re.search(r"You\s+total\s+9 ", out) and "following: You (0x1751CB82, port 20001)" in out and "port 20000" not in out


def test_live_rejects_speed_and_pcap_rejects_nothing_else(tmp_path):
    with pytest.raises(SystemExit) as e:
        app.parse_args(["--live", "--speed", "3"])
    assert e.value.code == 2


def test_error_watch_reports_connection_once_per_change(tmp_path, fake_capture):
    from eqoa_parser.config import load_config
    from eqoa_parser.net.sources import LivePacketSource

    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    now = time.time()
    fake_capture.script = [dg(now, payload(H16, msg(1, hp(PLAYER, -5, MOB)))), dg(now + .1, payload(H16, msg(2, hp(PLAYER, -6, MOB)))),
                           CaptureError("x")]
    src = LivePacketSource(cfg)

    class Sink:
        def __init__(self):
            self.conns, self.errors = [], []

        def on_connection(self, text):
            self.conns.append(text)

        def on_error(self, text):
            self.errors.append(text)

    sink = Sink()
    got = []
    with pytest.raises(CaptureError):
        for item in app.ErrorWatch(src, sink):
            got.append(item)
    assert sink.conns == ["following: You (0x174A1A40, port 20000)"] and sink.errors == []
    assert len([g for g in got if not isinstance(g[1], str)]) == 2


def test_gui_path_surfaces_live_capture_error_in_the_window(tmp_path, fake_capture):
    pytest.importorskip("tkinter")
    import tkinter as tk

    from eqoa_parser.config import load_config
    from eqoa_parser.net.sources import LivePacketSource
    from eqoa_parser.ui.tk_app import TkApp

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    win = rt = store = None
    try:
        cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
        fake_capture.script = [dg(time.time(), payload(H16, msg(1, hp(PLAYER, -5, MOB)))), CaptureError("socket died")]
        store = app.RunStore(cfg.runs_dir, "live:capture")
        win = TkApp(root=tk.Toplevel(root), source_label="x")
        rt = app.Runtime(cfg, LivePacketSource(cfg), store, win.sink)
        try:
            rt.engine.run(threading.Event())
        except CaptureError as exc:
            win.sink.on_error(f"{type(exc).__name__}: {exc}")
        win.drain()
        assert "socket died" in win.status_var.get()
        win.close()
    finally:
        # Tk cleanup matters beyond this test: Tk variables left as cyclic garbage with a live interpreter get finalised by the
        # garbage collector in WHATEVER thread triggers it; from a non-main thread that blocks ~1 s per variable ('main thread is
        # not in main loop').  That is what made test_verify_pine's mock-server round trips time out in full-suite runs.
        win = rt = store = None
        import gc
        gc.collect()
        root.destroy()
        gc.collect()
