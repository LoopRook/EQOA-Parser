"""PyInstaller entry point for the portable exe (not used by `python -m eqoa_parser`).

Two executables are built from this file (see eqoa.spec):
  EQOAParser.exe         windowed, asks for Administrator at launch, defaults to --live when no source flag is given
  EQOAParser-replay.exe  console, does NOT ask for Administrator, needs an explicit source (--pcap FILE, --replay FILE ...)
Both accept every normal CLI flag (see --help). This file only (1) injects --live for the live exe and (2) makes a
windowed process (no stdout/stderr) show errors in a message box instead of dying silently.
"""
from __future__ import annotations

import io
import os
import sys

SOURCE_FLAGS = ("--pcap", "--live", "--replay", "--live-pine")


def _is_replay_build() -> bool:
    return os.path.splitext(os.path.basename(sys.executable))[0].lower().endswith("-replay")


def _needs_default_live(args: list[str]) -> bool:
    if any(a in ("-h", "--help") for a in args):
        return False
    return not any(a == f or a.startswith(f + "=") for a in args for f in SOURCE_FLAGS)


def _message_box(title: str, text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text[:1800], title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def run(argv: list[str]) -> int:
    from eqoa_parser.app import main

    args = list(argv)
    if not _is_replay_build() and _needs_default_live(args):
        args = ["--live"] + args
    windowed = sys.stdout is None or sys.stderr is None
    if windowed:  # PyInstaller windowed build: no console streams
        sys.stdout = io.StringIO()
        sys.stderr = err = io.StringIO()
    try:
        code = main(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        if windowed and not isinstance(exc.code, int) and exc.code is not None:
            err.write(str(exc.code))
    except Exception as exc:  # last resort: never a silent crash for a double-clicked exe
        code = 1
        if windowed:
            import traceback

            err.write("".join(traceback.format_exception(exc)))
        else:
            raise
    if windowed and code not in (0, None):
        _message_box("EQOA Parser", err.getvalue().strip() or f"Exited with status {code}.")
    return int(code or 0)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
