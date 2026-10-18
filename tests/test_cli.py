from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eqoa_parser import app
from test_ui_fixtures import make_fight

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "synthetic_combat.log"


def test_requires_exactly_one_source(capsys):
    for argv in ([], ["--replay", "a.log", "--live"], ["--pcap", "a", "--replay", "b"], ["--live", "--live-pine"],
                 ["--pcap", "a", "--live-pine"]):
        with pytest.raises(SystemExit) as e:
            app.parse_args(argv)
        assert e.value.code == 2
    assert "exactly one of" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--replay", "a", "--speed", "-1"],
    ["--live", "--speed", "2"],
    ["--live-pine", "--speed", "2"],
    ["--pcap", "a", "--connection", "0"],
    ["--pcap", "a", "--connection", "70000"],
    ["--pcap", "a", "--connection", "abc"],
    ["--replay", "a", "--idle-timeout", "0"],
    ["--replay", "a", "--bogus"],
])
def test_invalid_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        app.parse_args(argv)
    assert e.value.code == 2


def test_all_flags_parse_and_config_overrides(tmp_path):
    args = app.parse_args(["--replay", "r.log", "--speed", "5", "--no-gui", "--show-group", "--idle-timeout", "3",
                           "--runs-dir", str(tmp_path), "--no-always-on-top"])
    cfg = app.make_config(args)
    assert (args.speed, args.no_gui, args.live) == (5.0, True, False)
    assert cfg.show_group is True and cfg.idle_timeout == 3.0
    assert cfg.runs_dir == str(tmp_path.resolve()) and cfg.ui.always_on_top is False
    cfg2 = app.make_config(app.parse_args(["--replay", "r.log", "--always-on-top"]))
    assert cfg2.ui.always_on_top is True and cfg2.show_group is False


def test_config_file_used(tmp_path):
    c = tmp_path / "c.yaml"
    c.write_text("idle_timeout: 4\nshow_group: true\nui:\n  always_on_top: false\nrunsdir: x\n".replace("runsdir", "runs_dir"))
    cfg = app.make_config(app.parse_args(["--replay", "r", "--config", str(c)]))
    assert cfg.idle_timeout == 4 and cfg.show_group and not cfg.ui.always_on_top
    assert Path(cfg.runs_dir) == tmp_path / "x"          # relative to the config file


def test_live_without_buffer_address_exits_nonzero_pointing_at_doc():
    r = subprocess.run([sys.executable, "-m", "eqoa_parser", "--live-pine", "--no-gui"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0
    assert "finding-the-buffer.md" in r.stderr and "buffer.address" in r.stderr
    assert r.stdout == ""


def test_main_bad_config_returns_2(tmp_path, capsys):
    c = tmp_path / "c.yaml"
    c.write_text("nope: 1\n")
    assert app.main(["--replay", "r", "--config", str(c), "--no-gui"]) == 2
    assert "unknown config key" in capsys.readouterr().err


class FakeSource:
    closed = False

    def close(self):
        self.closed = True


class FakeEngine:
    """Stands in for pipeline.Engine: closes two fights then finishes."""

    def __init__(self, config, source, *, on_live=None, on_closed=None, on_finished=None, **kw):
        self.on_live, self.on_closed, self.on_finished = on_live, on_closed, on_finished
        self.source = source

    def run(self, stop=None):
        self.on_live(make_fight(1, ongoing=True))
        self.on_closed(make_fight(1))
        self.on_closed(make_fight(2, target="a bat"))
        self.on_finished()

    def finish(self):
        pass


def test_console_main_wires_engine_and_persistence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(app.pipeline, "Engine", FakeEngine)
    monkeypatch.setattr(app, "make_source", lambda args, cfg: (FakeSource(), "replay:fake"))
    argv = ["--replay", "fake", "--no-gui", "--runs-dir", str(tmp_path)]
    assert app.main(argv) == 0
    out = capsys.readouterr().out
    assert "=== Fight 1" in out and "=== Fight 2: a bat" in out and "Fights: 2" in out
    (f,) = list(tmp_path.glob("*.json"))
    doc = json.loads(f.read_text())
    assert [len(r["fights"]) for r in doc["runs"]] == [2]

    assert app.main(argv) == 0                    # second launch: append + reload
    assert "2 fight(s) from earlier runs today" in capsys.readouterr().out
    assert [len(r["fights"]) for r in json.loads(f.read_text())["runs"]] == [2, 2]


def test_save_failure_does_not_stop_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(app.pipeline, "Engine", FakeEngine)
    monkeypatch.setattr(app, "make_source", lambda args, cfg: (FakeSource(), "replay:fake"))
    monkeypatch.setattr(app.RunStore, "add_fight", lambda self, s: (_ for _ in ()).throw(OSError("full")))
    assert app.main(["--replay", "x", "--no-gui", "--runs-dir", str(tmp_path)]) == 0
    cap = capsys.readouterr()
    assert "could not save fight 1" in cap.err and "Fights: 2" in cap.out


def cfg_file(tmp_path, extra=""):
    """Config whose unmatched log lives in tmp_path, so tests never touch the repo's unmatched.log."""
    p = tmp_path / "cfg.yaml"
    p.write_text(f'patterns_file: "{(ROOT / "patterns.yaml").as_posix()}"\n'
                 f'unmatched_log: "{(tmp_path / "unmatched.log").as_posix()}"\n{extra}', encoding="utf-8")
    return p


def run_cli(*argv, timeout=60):
    return subprocess.run([sys.executable, "-m", "eqoa_parser", *map(str, argv)], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize("extra,flags,needle", [
    ("idle_timeout: [unclosed\n", [], "yaml"),
    ("patterns_file: nope.yaml\n", [], "nope.yaml"),
    ("idle_timeout: '8'\n", [], "idle_timeout"),
    ("idle_timeout: 0\n", [], "idle_timeout"),
    ("", ["--replay", "missing.log"], "replay file not found"),
])
def test_startup_failures_are_error_exit_2_without_traceback(tmp_path, extra, flags, needle):
    cfg = cfg_file(tmp_path, extra)
    # patterns_file in extra would duplicate the key; the later definition wins in YAML only if we drop the first
    if extra.startswith("patterns_file"):
        cfg.write_text(extra + f'unmatched_log: "{(tmp_path / "u.log").as_posix()}"\n', encoding="utf-8")
    r = run_cli("--config", cfg, *(flags or ["--replay", FIXTURE]), "--no-gui", "--runs-dir", tmp_path / "runs")
    assert r.returncode == 2 and "Traceback" not in r.stderr
    assert r.stderr.startswith("error:") and needle in r.stderr.lower()
    assert r.stdout == "" and not (tmp_path / "runs").exists()


@pytest.mark.parametrize("flag,val", [("--idle-timeout", "nan"), ("--idle-timeout", "inf"), ("--speed", "nan"),
                                      ("--speed", "inf")])
def test_non_finite_numbers_rejected(flag, val):
    with pytest.raises(SystemExit) as e:
        app.parse_args(["--replay", "x", flag, val])
    assert e.value.code == 2


def test_live_unreachable_pine_is_clean_error(tmp_path):
    cfg = cfg_file(tmp_path, "buffer:\n  address: '0x00100000'\npine:\n  slot: 28997\n  timeout: 0.3\n")
    r = run_cli("--config", cfg, "--live-pine", "--no-gui", "--runs-dir", tmp_path / "runs")
    assert r.returncode == 2 and r.stderr.startswith("error:") and "Traceback" not in r.stderr
    assert r.stdout == "" and not (tmp_path / "runs").exists()


def test_broken_pipe_exits_quietly(tmp_path):
    cfg = cfg_file(tmp_path)
    cmd = [sys.executable, "-m", "eqoa_parser", "--config", str(cfg), "--replay", str(FIXTURE), "--no-gui",
           "--runs-dir", str(tmp_path / "runs")]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.stdout.readline()
    proc.stdout.close()
    err = proc.stderr.read()
    assert proc.wait(timeout=30) == 0 and "Traceback" not in err and "OSError" not in err


def test_live_source_errors_reach_the_sink():
    class Src:
        last_error = None

        def __iter__(self):
            yield 1.0, ""
            self.last_error = RuntimeError("PCSX2 went away")
            yield 2.0, ""
            yield 3.0, ""
            self.last_error = None
            yield 4.0, ""

        def close(self):
            pass

    got = []

    class Sink:
        def on_error(self, m):
            got.append(m)

    assert len(list(app.ErrorWatch(Src(), Sink()))) == 4
    assert got == ["source error: PCSX2 went away", None]


def test_real_replay_end_to_end_no_gui(tmp_path):
    cmd = [sys.executable, "-m", "eqoa_parser", "--config", str(cfg_file(tmp_path)), "--replay", str(FIXTURE),
           "--no-gui", "--runs-dir", str(tmp_path)]
    for expected_prev in (0, 5):
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert f"{expected_prev} fight(s) from earlier runs today" in r.stdout
        assert "=== Run summary ===" in r.stdout and "Fights: 5" in r.stdout
        assert "Bob" not in r.stdout                       # group hidden by default
    (f,) = list(tmp_path.glob("*.json"))
    assert [len(r["fights"]) for r in json.loads(f.read_text())["runs"]] == [5, 5]
    r = subprocess.run(cmd + ["--show-group"], cwd=ROOT, capture_output=True, text=True)
    assert "Bob" in r.stdout


# ---------------------------------------------------------------- Phase 2: packet path
CAP1 = ROOT / "fixtures" / "real" / "cap1_necro_spells.pcapng"
CAP2 = ROOT / "fixtures" / "real" / "cap2_bard_melee_necro_pet.pcapng"


def pcap_run(tmp_path, *argv):
    r = run_cli("--config", cfg_file(tmp_path), *argv, "--no-gui", "--runs-dir", tmp_path / "runs")
    assert r.returncode == 0, r.stderr
    (f,) = (tmp_path / "runs").glob("*.json")
    return r, json.loads(f.read_text(encoding="utf-8"))["runs"][0]["fights"]


def test_pcap_cap1_one_fight_you_7218_over_13_hits(tmp_path):
    r, fights = pcap_run(tmp_path, "--pcap", CAP1)
    assert len(fights) == 1
    you = fights[0]["actors"]["you"]
    assert (you["total"], you["hits"]) == (7218, 13)
    assert "[net] following: You (0x13FF89D8, port 24159)" in r.stdout
    assert "Fights: 1" in r.stdout and "Total damage: 7218" in r.stdout
    assert fights[0]["target"].startswith("0x")


def test_pcap_cap2_connection_selects_pet_only_emulator(tmp_path):
    r, fights = pcap_run(tmp_path, "--pcap", CAP2, "--connection", "20605")
    assert len(fights) == 1
    pet = fights[0]["actors"]["pet"]
    assert (pet["total"], pet["hits"]) == (675, 36)
    assert fights[0]["actors"].get("you", {"total": 0})["total"] == 0
    assert "[net] following: Zecron (0x1751CB82, port 20605)" in r.stdout and "21173" not in r.stdout


def test_pcap_cap2_bard_heals_are_not_damage(tmp_path):
    r, fights = pcap_run(tmp_path, "--pcap", CAP2, "--connection", "21173")
    you = fights[0]["actors"]["you"]
    assert you["hits"] == 56 and fights[0]["damage_taken"] == 56 * 21
    assert fights[0]["combined_total"] == you["total"]          # 56 heals of 300 add nothing


def test_pcap_missing_file_is_clean_error(tmp_path):
    r = run_cli("--pcap", tmp_path / "nope.pcapng", "--no-gui", "--runs-dir", tmp_path / "runs")
    assert r.returncode == 2 and r.stderr.startswith("error:") and "not found" in r.stderr


def test_connection_flag_sets_net_local_port():
    cfg = app.make_config(app.parse_args(["--pcap", "x", "--connection", "20605"]))
    assert cfg.net.local_port == 20605
    assert app.make_config(app.parse_args(["--pcap", "x"])).net.local_port is None


def test_live_without_admin_is_clean_error(monkeypatch, capsys, tmp_path):
    from eqoa_parser.net import capture

    class NoAdmin:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise capture.CaptureError("live capture needs Administrator rights")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(capture, "RawCapture", NoAdmin)
    assert app.main(["--live", "--no-gui", "--runs-dir", str(tmp_path / "runs")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Administrator" in err and err.count("docs/live-capture.md") == 1
    assert not (tmp_path / "runs").exists()


def test_capture_error_after_startup_exits_2_in_console(monkeypatch, capsys, tmp_path):
    from eqoa_parser.net import capture

    class Dies:
        calls = 0

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            Dies.calls += 1
            if Dies.calls > 1:
                raise capture.CaptureError("socket died")
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return None

        def close(self):
            pass

    monkeypatch.setattr(capture, "RawCapture", Dies)
    assert app.main(["--live", "--no-gui", "--runs-dir", str(tmp_path / "runs")]) == 2
    assert "socket died" in capsys.readouterr().err


def test_connection_info_reaches_the_sink():
    class Attr:
        def __init__(self, label, pid):
            self._l, self.player_id = label, pid

        def player_label(self):
            return self._l

    class Router:
        selected = 20605
        ports = [20605]

        def connections(self):
            return list(self.ports)

        def attributor(self, conn):
            return {20605: Attr("you", 0x1751CB82), 21173: Attr("Zecron", None)}[conn]

    class Src:
        router = Router()

        def __iter__(self):
            yield 1.0, ""
            Src.router.ports = [20605, 21173]
            yield 2.0, ""

        def close(self):
            pass

    got = []
    sink = type("S", (), {"on_error": lambda s, m: None, "on_connection": lambda s, t: got.append(t)})()
    list(app.ErrorWatch(Src(), sink))
    assert got == ["following: you (0x1751CB82, port 20605)",
                   "following: you (0x1751CB82, port 20605), Zecron (port 21173)"]


def test_multibox_default_follows_both_emulators_and_connection_limits_it(tmp_path):
    r, fights = pcap_run(tmp_path, "--pcap", CAP2)
    assert [f["actors"].get("you", {"total": 0})["total"] for f in fights] == [20670, 0]
    assert "[net] following: You (0x174A1A40, port 21173), Zecron (0x1751CB82, port 20605)" in r.stdout
    assert "Zecron's pet 675" in r.stdout
    (tmp_path / "one").mkdir()
    r1, fights1 = pcap_run(tmp_path / "one", "--pcap", CAP2, "--connection", "21173")
    assert len(fights1) == 1 and "Zecron" not in r1.stdout



@pytest.mark.parametrize("data", [b"this is not a capture\n" * 5, bytes(range(256)), b"\xd4\xc3\xb2\xa1" + b"\0" * 40])
def test_pcap_not_a_pcapng_is_clean_error_before_banner(tmp_path, data):
    p = tmp_path / "bad.pcapng"
    p.write_bytes(data)
    r = run_cli("--config", cfg_file(tmp_path), "--pcap", p, "--no-gui", "--runs-dir", tmp_path / "runs")
    assert r.returncode == 2 and r.stderr.startswith("error:") and "not a pcapng" in r.stderr
    assert "Traceback" not in r.stderr and r.stdout == "" and not (tmp_path / "runs").exists()


def test_pcap_empty_or_trafficless_file_prints_a_notice(tmp_path):
    empty = tmp_path / "empty.pcapng"
    empty.write_bytes(b"")
    cut = tmp_path / "cut.pcapng"
    cut.write_bytes(CAP2.read_bytes()[:200])
    for p in (empty, cut):
        r = run_cli("--config", cfg_file(tmp_path), "--pcap", p, "--no-gui", "--runs-dir", tmp_path / "runs")
        assert r.returncode == 0 and "No fights" in r.stdout
        assert r.stderr.count("\n") == 1 and "no game-server combat found" in r.stderr and str(p) in r.stderr


def test_pcap_with_combat_prints_no_notice(tmp_path):
    r = run_cli("--config", cfg_file(tmp_path), "--pcap", CAP1, "--no-gui", "--runs-dir", tmp_path / "runs")
    assert "no game-server combat" not in r.stderr
