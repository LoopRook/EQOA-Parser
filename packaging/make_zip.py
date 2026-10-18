"""Builds dist/EQOAParser-<version>.zip from dist/*.exe + packaging files. Run by build-exe.bat after PyInstaller."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eqoa_parser import __version__  # noqa: E402


def build() -> Path:
    dist = ROOT / "dist"
    items = [
        (dist / "EQOAParser.exe", "EQOAParser.exe"),
        (dist / "EQOAParser-replay.exe", "EQOAParser-replay.exe"),
        (ROOT / "packaging" / "config.dist.yaml", "config.yaml"),
        (ROOT / "packaging" / "READ-ME-FIRST.txt", "READ-ME-FIRST.txt"),
    ]
    for src, _ in items:
        if not src.is_file():
            raise SystemExit(f"missing {src}")
    out = dist / f"EQOAParser-{__version__}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, name in items:
            z.write(src, name)
    return out


if __name__ == "__main__":
    out = build()
    print(f"{out.name}: {out.stat().st_size / 1e6:.1f} MB")
    with zipfile.ZipFile(out) as z:
        for i in z.infolist():
            print(f"  {i.filename}  {i.file_size} bytes")
