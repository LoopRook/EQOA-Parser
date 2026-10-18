"""Independent verification, Phase 3 (session D): the portable build in dist/.

Only the replay build (EQOAParser-replay.exe, asInvoker) is ever executed here, from a temporary copy of the unzipped
dist/EQOAParser-<version>.zip.  EQOAParser.exe (requireAdministrator -> UAC prompt) is inspected statically, never run.
Skipped if dist/ or Windows is missing.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
from eqoa_parser import __version__  # main chat: follow the package version
ZIP = DIST / f"EQOAParser-{__version__}.zip"
CAP2 = ROOT / "fixtures" / "real" / "cap2_bard_melee_necro_pet.pcapng"

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not ZIP.exists(), reason=f"needs Windows and dist/EQOAParser-{__version__}.zip")


@pytest.fixture(scope="module")
def unzipped(tmp_path_factory):
    d = tmp_path_factory.mktemp("exe_unzip")
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(d)
    shutil.copy(CAP2, d / CAP2.name)
    return d


def run_exe(unzipped: Path, *args: str, cwd: Path | None = None, timeout: int = 120):
    return subprocess.run([str(unzipped / "EQOAParser-replay.exe"), *args], cwd=cwd or unzipped, capture_output=True, text=True, timeout=timeout)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_zip_contents_and_exes_match_dist(unzipped):
    names = {p.name for p in unzipped.iterdir()}
    assert {"EQOAParser.exe", "EQOAParser-replay.exe", "config.yaml", "READ-ME-FIRST.txt"} <= names
    assert sha(unzipped / "EQOAParser.exe") == sha(DIST / "EQOAParser.exe")
    assert sha(unzipped / "EQOAParser-replay.exe") == sha(DIST / "EQOAParser-replay.exe")


def test_manifests_live_build_asks_for_admin_replay_build_does_not():
    """Static inspection only (nothing is executed)."""
    live = (DIST / "EQOAParser.exe").read_bytes()
    replay = (DIST / "EQOAParser-replay.exe").read_bytes()
    assert b'requestedExecutionLevel level="requireAdministrator"' in live
    assert b'requestedExecutionLevel level="asInvoker"' in replay and b"requireAdministrator" not in replay


def test_packaged_config_matches_the_documented_defaults(unzipped):
    import yaml

    cfg = yaml.safe_load((unzipped / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["idle_timeout"] == 8.0 and cfg["net"]["local_port"] is None and cfg["net"]["server_ip"] == "5.161.217.188"
    assert cfg["runs_dir"] == "runs"


def test_replay_exe_runs_cap2_from_another_cwd_and_writes_next_to_the_exe(unzipped, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = run_exe(unzipped, "--pcap", str(unzipped / CAP2.name), "--no-gui", cwd=other)
    assert r.returncode == 0, r.stderr
    out = "\n".join(ln for ln in r.stdout.splitlines() if not ln.startswith("[live]"))
    assert "Fights: 2" in out and "Total damage: 21345" in out
    f1 = out.split("=== Fight 1")[1].split("=== Fight 2")[0]
    f2 = out.split("=== Fight 2")[1].split("=== Run summary")[0]
    assert re.search(r"You\s+total\s+20670", f1) and "damage taken 1176" in f1
    assert re.search(r"Zecron's pet\s+total\s+675", f2) and re.search(r"Zecron combined\s+total\s+675", f2)  # Phase 4 wording (main chat)
    assert "as you" in r.stderr and "as Zecron" in r.stderr
    assert (unzipped / "runs").is_dir() and (unzipped / "unmatched.log").exists()           # next to the exe ...
    assert list(other.iterdir()) == []                                                      # ... and not in the working directory
    doc = json.loads(next((unzipped / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert [f["combined_total"] for f in doc["runs"][-1]["fights"]] == [20670, 675]


def test_replay_exe_second_run_appends_and_reloads(unzipped):
    r = run_exe(unzipped, "--pcap", str(unzipped / CAP2.name), "--no-gui", "--connection", "20605")
    assert r.returncode == 0 and re.search(r"Zecron.s pet\s+total\s+675", r.stdout) and "Fights: 1" in r.stdout  # Phase 4 wording (main chat)
    assert "fight(s) from earlier runs today" in r.stdout.splitlines()[0]
    assert "[net] following: Zecron (0x1751CB82, port 20605)" in r.stdout


def test_replay_exe_reads_config_yaml_next_to_it_then_falls_back_to_the_bundled_default(unzipped):
    cfgp = unzipped / "config.yaml"
    original = cfgp.read_text(encoding="utf-8")
    try:
        cfgp.write_text(original.replace("local_port: null", "local_port: 21173"), encoding="utf-8")
        r = run_exe(unzipped, "--pcap", str(unzipped / CAP2.name), "--no-gui")
        assert "Fights: 1" in r.stdout and "Total damage: 20670" in r.stdout
        cfgp.unlink()
        r = run_exe(unzipped, "--pcap", str(unzipped / CAP2.name), "--no-gui")
        assert r.returncode == 0 and "Fights: 2" in r.stdout                               # bundled default = follow both
        cfgp.write_text("idle_timeout: banana\n", encoding="utf-8")
        r = run_exe(unzipped, "--pcap", str(unzipped / CAP2.name), "--no-gui")
        assert r.returncode == 2 and r.stderr.startswith("error:") and "Traceback" not in r.stderr
    finally:
        cfgp.write_text(original, encoding="utf-8")


def test_replay_exe_cli_errors_are_clean(unzipped, tmp_path):
    bad = tmp_path / "bad.pcapng"
    bad.write_bytes(b"not a capture at all")
    for args in (["--pcap", str(bad), "--no-gui"], ["--pcap", str(tmp_path / "nope.pcapng"), "--no-gui"], ["--no-gui"]):
        r = run_exe(unzipped, *args)
        assert r.returncode == 2 and "Traceback" not in r.stderr, (args, r.stderr[-300:])
    assert "--pcap" in run_exe(unzipped, "--help").stdout


def test_replay_exe_runs_the_text_replay_too(unzipped):
    shutil.copy(ROOT / "fixtures" / "synthetic_combat.log", unzipped / "synthetic_combat.log")
    r = run_exe(unzipped, "--replay", str(unzipped / "synthetic_combat.log"), "--no-gui")
    assert r.returncode == 0 and "Fights: 5" in r.stdout and "Total damage: 274" in r.stdout
