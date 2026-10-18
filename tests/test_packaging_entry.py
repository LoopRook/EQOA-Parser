"""packaging/entry.py logic (default --live injection, windowed error reporting). The exe itself is smoke-tested by hand."""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parent.parent / "packaging" / "entry.py"


@pytest.fixture
def entry():
    spec = importlib.util.spec_from_file_location("eqoa_entry", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("args,expected", [
    ([], True), (["--no-gui"], True), (["--connection", "20605"], True),
    (["--pcap", "a.pcapng"], False), (["--pcap=a.pcapng"], False), (["--live"], False),
    (["--replay", "x.log"], False), (["--live-pine"], False), (["--help"], False), (["-h"], False),
])
def test_default_live_injection(entry, args, expected):
    assert entry._needs_default_live(args) is expected


def test_run_injects_live_for_the_live_exe_only(entry, monkeypatch):
    seen = []
    import eqoa_parser.app as app

    monkeypatch.setattr(app, "main", lambda argv: seen.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "executable", r"C:\x\EQOAParser.exe")
    assert entry.run(["--no-gui"]) == 0 and seen[-1] == ["--live", "--no-gui"]
    monkeypatch.setattr(sys, "executable", r"C:\x\EQOAParser-replay.exe")
    entry.run(["--pcap", "f"])
    entry.run([])
    assert seen[-2:] == [["--pcap", "f"], []]


def test_windowed_errors_go_to_a_message_box_not_a_silent_exit(entry, monkeypatch):
    import eqoa_parser.app as app

    boxes = []
    monkeypatch.setattr(entry, "_message_box", lambda title, text: boxes.append(text))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(app, "main", lambda argv: (sys.stderr.write("error: needs Administrator\n"), 2)[1])
    assert entry.run(["--live"]) == 2
    assert boxes == ["error: needs Administrator"]
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(app, "main", lambda argv: 1 / 0)
    assert entry.run(["--live"]) == 1 and "ZeroDivisionError" in boxes[-1]


def test_shipped_config_and_readme_are_valid():
    from eqoa_parser.config import load_config

    root = Path(__file__).resolve().parent.parent / "packaging"
    cfg = load_config(root / "config.dist.yaml")
    assert cfg.net.server_ip == "5.161.217.188" and cfg.net.local_port is None and cfg.ui.always_on_top
    readme = (root / "READ-ME-FIRST.txt").read_text(encoding="utf-8")
    assert len(readme.splitlines()) < 60 and "More info" in readme and "Run anyway" in readme and "Sockets" in readme
