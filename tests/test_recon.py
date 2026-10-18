"""recon commands against the mock server (real socket). UNVERIFIED against real PCSX2."""
from __future__ import annotations

import json
import threading
import time

import pytest

from eqoa_parser import recon
from eqoa_parser.mock_pine import MockPineServer

RAM = 8192


@pytest.fixture
def server():
    with MockPineServer(bytearray(RAM)) as s:
        yield s


def run(server, capsys, *argv):
    code = recon.main(["--slot", str(server.tcp_port), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


ENCODED = {"ascii": "ascii", "utf16le": "utf-16-le", "shiftjis": "shift_jis"}


@pytest.mark.parametrize("encoding,text", [("ascii", "You hit a rat"), ("utf16le", "You hit a rat"), ("shiftjis", "ゴブリン hit")])
@pytest.mark.parametrize("chunk", [64, 100, 4096])
def test_scan_finds_match_spanning_every_chunk_boundary(server, capsys, encoding, text, chunk):
    needle = text.encode(ENCODED[encoding])
    # place the needle so it straddles a chunk boundary for every offset within the needle
    for off in range(len(needle)):
        boundary = chunk * 3 if chunk < 1000 else chunk
        addr = boundary - off
        server.write_ram(0, bytes(RAM))
        server.write_ram(addr, needle)
        code, out, _ = run(server, capsys, "--chunk", str(chunk), "scan", "--text", text, "--encoding", encoding, "--end", str(RAM))
        assert code == 0
        assert out.count("match ") == 1 and f"0x{addr:08X}" in out, (off, out)


def test_scan_multiple_matches_and_no_match(server, capsys):
    server.write_ram(100, b"rat"); server.write_ram(5000, b"rat")
    code, out, _ = run(server, capsys, "scan", "--text", "rat", "--end", str(RAM))
    assert code == 0 and "0x00000064" in out and "0x00001388" in out and "2 match(es)" in out
    code, out, _ = run(server, capsys, "scan", "--text", "zzzz", "--end", str(RAM))
    assert code == 1 and "0 match(es)" in out


def test_scan_shows_hex_and_text_context(server, capsys):
    server.write_ram(0x200, b"before You hit a rat after")
    _, out, _ = run(server, capsys, "scan", "--text", "You hit", "--end", str(RAM))
    assert "59 6f 75 20 68 69 74" in out and "before You hit a rat after" in out


def test_find_matches_overlapping_needle(server):
    from eqoa_parser.pine import PineClient
    from eqoa_parser.config import PineConfig
    server.write_ram(10, b"aaaa")
    with PineClient(PineConfig(slot=server.tcp_port), platform="win32") as c:
        assert list(recon.find_matches(c, b"aa", 0, 64, chunk=11)) == [10, 11, 12]


def test_scan_rejects_unencodable_text(server, capsys):
    code, _, err = run(server, capsys, "scan", "--text", "ゴ", "--encoding", "ascii")
    assert code == 2 and "cannot be encoded" in err


def test_not_running_message(capsys):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    code = recon.main(["--slot", str(port), "--timeout", "0.5", "scan", "--text", "x"])
    assert code == 2 and "PCSX2" in capsys.readouterr().err


def test_snapshot_full_and_partial(server, capsys, tmp_path):
    server.write_ram(0, bytes(range(256)) * 32)
    f = tmp_path / "a.bin"
    code, out, _ = run(server, capsys, "snapshot", "--out", str(f), "--end", str(RAM))
    assert code == 0 and f.read_bytes() == server.read_ram(0, RAM)
    g = tmp_path / "b.bin"
    run(server, capsys, "snapshot", "--out", str(g), "--start", "0x100", "--end", "0x180")
    assert g.read_bytes() == server.read_ram(0x100, 0x80)
    assert json.loads((tmp_path / "b.bin.meta.json").read_text())["start"] == 0x100


def test_changed_ranges_merge_gap():
    a = bytes(100); b = bytearray(a)
    b[10] = 1; b[14] = 1; b[50] = 1
    assert recon.changed_ranges(a, bytes(b), 0x1000, gap=8) == [(0x100A, 0x100F), (0x1032, 0x1033)]
    assert recon.changed_ranges(a, bytes(b), 0, gap=0) == [(10, 11), (14, 15), (50, 51)]


def test_diff_two_snapshots_and_text_filter(server, capsys, tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    run(server, capsys, "snapshot", "--out", str(a), "--end", str(RAM))
    server.write_ram(0x300, b"You hit a rat for 5 points")   # text change
    server.write_ram(0x900, bytes([0xDE, 0xAD, 0x01, 0x02]))  # binary noise
    run(server, capsys, "snapshot", "--out", str(b), "--end", str(RAM))
    code, out, _ = run(server, capsys, "diff", str(a), str(b))
    assert code == 0 and "region 0x00000300-0x00000319" in out and "region 0x00000900" in out
    code, out, _ = run(server, capsys, "diff", str(a), str(b), "--text-only", "--encoding", "ascii")
    assert "region 0x00000300" in out and "0x00000900" not in out and "2 changed region(s) total, 1 shown" in out
    assert "You hit a rat for 5 points" in out


def test_diff_text_filter_utf16_and_shiftjis(server, capsys, tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    run(server, capsys, "snapshot", "--out", str(a), "--end", str(RAM))
    server.write_ram(0x400, "You hit".encode("utf-16-le"))
    server.write_ram(0x800, "ゴブリンを倒した".encode("shift_jis"))
    run(server, capsys, "snapshot", "--out", str(b), "--end", str(RAM))
    _, out, _ = run(server, capsys, "diff", str(a), str(b), "--text-only", "--encoding", "utf16le")
    assert "0x00000400" in out and "0x00000800" not in out
    _, out, _ = run(server, capsys, "diff", str(a), str(b), "--text-only", "--encoding", "shiftjis", "--min-len", "4")
    assert "0x00000800" in out and "ゴブリンを倒した" in out


def test_diff_snapshot_vs_live(server, capsys, tmp_path):
    a = tmp_path / "a.bin"
    run(server, capsys, "snapshot", "--out", str(a), "--end", str(RAM))
    server.write_ram(0x50, b"hello world")
    code, out, _ = run(server, capsys, "diff", str(a), "live")
    assert code == 0 and "region 0x00000050-0x0000005A" in out


def test_diff_range_mismatch(server, capsys, tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(bytes(10)); b.write_bytes(bytes(20))
    code = recon.main(["diff", str(a), str(b)])
    assert code == 2


def test_watch_prints_initial_and_change_then_stops(server, capsys):
    server.write_ram(0x100, b"first line\x00")
    t = threading.Timer(0.4, lambda: server.write_ram(0x100, b"second one\x00"))
    t.start()
    t0 = time.monotonic()
    code, out, _ = run(server, capsys, "watch", "--addr", "0x100", "--len", "16", "--interval", "0.05", "--count", "2")
    t.join()
    assert code == 0 and time.monotonic() - t0 < 5
    assert "initial" in out and "first line" in out and "second one" in out and "byte(s) changed" in out


def test_watch_duration_bound_no_change(server, capsys):
    t0 = time.monotonic()
    code, out, _ = run(server, capsys, "watch", "--addr", "0", "--len", "8", "--interval", "0.05", "--duration", "0.3")
    assert code == 0 and out.count("initial") == 1 and time.monotonic() - t0 < 3


# --- the manual workflow from docs/finding-the-buffer.md: mock_pine CLI --plant, then recon scan over the socket ----
@pytest.mark.parametrize("encoding,text", [
    ("ascii", "You hit a decaying skeleton for 12 points of damage."),
    ("utf16le", "You hit a decaying skeleton for 12 points of damage."),
    ("shiftjis", "ゴブリン decaying skeleton for 12 points of damage."),
])
def test_mock_cli_plant_then_recon_scan(capsys, encoding, text):
    import socket
    from eqoa_parser import mock_pine

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    stop = threading.Event()
    t = threading.Thread(target=mock_pine.main, daemon=True, args=(
        ["--slot", str(port), "--encoding", encoding, "--plant", f"0x00ABC000={text}"], stop))
    t.start()
    for _ in range(100):  # wait for the listener
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    try:
        for needle in (text, "decaying skeleton"):
            capsys.readouterr()
            code = recon.main(["--slot", str(port), "scan", "--text", needle, "--encoding", encoding, "--end", "0x00AD0000"])
            out = capsys.readouterr().out
            assert code == 0 and "1 match(es)" in out
            assert ("match 1: 0x00ABC000" in out) if needle == text else ("match 1: 0x00ABC0" in out)
    finally:
        stop.set()
        t.join(timeout=5)


def test_scan_output_survives_non_utf8_console(server):
    """Regression: printing decoded Japanese text crashed on a cp1252 console (found in manual shiftjis run)."""
    import os, subprocess, sys
    server.write_ram(0x100, "\u30b4\u30d6\u30ea\u30f3 decaying skeleton".encode("shift_jis"))
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    r = subprocess.run([sys.executable, "-m", "eqoa_parser.recon", "--slot", str(server.tcp_port), "scan",
                        "--text", "decaying skeleton", "--encoding", "shiftjis", "--end", str(RAM)],
                       capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert r.returncode == 0 and "1 match(es)" in r.stdout and r.stderr == ""
