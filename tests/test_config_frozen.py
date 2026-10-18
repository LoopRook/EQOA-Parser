"""Frozen (PyInstaller) path logic in config.py. Simulated with monkeypatched sys attributes; no real freeze."""
from __future__ import annotations

import sys
from pathlib import Path

from eqoa_parser import config as cfgmod
from eqoa_parser.config import Config, find_project_root


def test_root_when_not_frozen_is_the_checkout():
    assert find_project_root(False) == Path(cfgmod.__file__).resolve().parent.parent


def test_root_when_frozen_is_the_exe_folder_not_the_temp_bundle(tmp_path):
    exe = tmp_path / "app" / "EQOAParser.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert find_project_root(True, str(exe)) == exe.parent.resolve()


def test_frozen_resolve_prefers_exe_folder_falls_back_to_bundle_only_for_bundled_files(tmp_path, monkeypatch):
    exe_dir, bundle = tmp_path / "exe", tmp_path / "_MEI123"
    exe_dir.mkdir(); bundle.mkdir()
    (bundle / "patterns.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    cfg = Config()
    cfg.base_dir = exe_dir
    assert cfg.resolve("patterns.yaml") == bundle / "patterns.yaml"      # only bundled -> bundle
    (exe_dir / "patterns.yaml").write_text("mine", encoding="utf-8")
    assert cfg.resolve("patterns.yaml") == exe_dir / "patterns.yaml"     # user's copy next to the exe wins
    assert cfg.resolve("runs") == exe_dir / "runs"                       # never into the temp folder
    assert cfg.resolve("unmatched.log") == exe_dir / "unmatched.log"
    assert cfg.resolve(str(tmp_path / "abs.log")) == tmp_path / "abs.log"


def test_not_frozen_resolve_unchanged(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "patterns.yaml").write_text("x", encoding="utf-8")
    cfg = Config()
    cfg.base_dir = tmp_path / "elsewhere"
    assert cfg.resolve("patterns.yaml") == tmp_path / "elsewhere" / "patterns.yaml"
