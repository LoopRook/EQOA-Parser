"""Log source contract. Core module: no UI imports.

OWNER: main chat (protocol). Session B implements ReplaySource in this file.
PineBufferSource lives in pine_source.py (session A) and is re-exported lazily here.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

# A raw_line equal to HEARTBEAT carries no text: it only advances the clock to `ts`
# so idle fights can close while no lines arrive. Consumers must never parse it.
HEARTBEAT = ""


@runtime_checkable
class LogSource(Protocol):
    """Yields (ts, raw_line) tuples in non-decreasing ts order, then stops (or runs forever when live).

    - ts: float seconds, same clock as Event.ts.
    - raw_line: one text line without trailing newline, or HEARTBEAT.
    - close(): release resources; safe to call more than once; makes a blocked/forever iterator stop.
    """

    def __iter__(self) -> Iterator[tuple[float, str]]: ...

    def close(self) -> None: ...


def pace(records, speed: float, heartbeat_interval: float, closed: threading.Event):
    """Yield (ts, item) from `records`, sleeping scaled gaps when speed > 0 and yielding
    (replay_clock_ts, HEARTBEAT) about every `heartbeat_interval` real seconds while waiting.
    speed == 0: no sleeping, no heartbeats. Stops promptly once `closed` is set. Shared by ReplaySource
    and net.sources.PcapReplaySource."""
    if speed == 0:
        for rec in records:
            if closed.is_set():
                return
            yield rec
        return
    t0_real: float | None = None
    t0_file = 0.0
    last_ts = 0.0
    for ts, item in records:
        if t0_real is None:
            t0_real, t0_file = time.monotonic(), ts
            last_ts = ts
        due = t0_real + (ts - t0_file) / speed
        while True:
            if closed.is_set():
                return
            remaining = due - time.monotonic()
            if remaining <= 0:
                break
            closed.wait(min(remaining, heartbeat_interval))
            if closed.is_set():
                return
            if due - time.monotonic() > 0:
                clock = t0_file + (time.monotonic() - t0_real) * speed
                clock = max(min(clock, ts), last_ts)
                last_ts = clock
                yield clock, HEARTBEAT
        last_ts = max(last_ts, ts)
        yield ts, item


class ReplaySource:
    """Replays a capture file. IMPLEMENTED BY SESSION B.

    File format (ours, not the game's): UTF-8 text, one line per message:
        <ts><TAB><raw_line>
    ts is a float in seconds. Blank lines and lines starting with '#' are skipped.
    Malformed lines (no tab / bad float) are skipped with a warning to stderr, never crash.

    speed: 0 = as fast as possible (no sleeping, no heartbeats);
           1.0 = real time; 10.0 = 10x faster. While sleeping between lines with speed > 0,
           yields (replay_clock_ts, HEARTBEAT) about every `heartbeat_interval` real seconds.
    Yielded ts are the file's ts unchanged (replays are deterministic), except that a ts lower than the
    previous one is clamped to the previous ts with a stderr warning, so yielded ts are non-decreasing.
    """

    def __init__(self, path: str | Path, speed: float = 0.0, heartbeat_interval: float = 0.25) -> None:
        if speed < 0:
            raise ValueError("speed must be >= 0")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be > 0")
        self.path = Path(path)
        self.speed = float(speed)
        self.heartbeat_interval = float(heartbeat_interval)
        self._closed = threading.Event()

    def _warn(self, lineno: int, why: str, line: str) -> None:
        print(f"ReplaySource: {self.path}:{lineno}: skipped malformed line ({why}): {line!r}", file=sys.stderr)

    def _records(self) -> Iterator[tuple[float, str]]:
        with self.path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            prev: float | None = None
            for lineno, line in enumerate(fh, 1):
                if self._closed.is_set():
                    return
                line = line.rstrip("\r\n")
                if not line.strip() or line.startswith("#"):
                    continue
                ts_text, tab, raw = line.partition("\t")
                if not tab:
                    self._warn(lineno, "no tab separator", line)
                    continue
                try:
                    ts = float(ts_text)
                except ValueError:
                    self._warn(lineno, "bad timestamp", line)
                    continue
                if not math.isfinite(ts):
                    self._warn(lineno, "non-finite timestamp", line)
                    continue
                if prev is not None and ts < prev:
                    print(f"ReplaySource: {self.path}:{lineno}: timestamp {ts} regresses below {prev}; "
                          f"clamped to {prev}: {line!r}", file=sys.stderr)
                    ts = prev
                prev = ts
                yield ts, raw

    def __iter__(self) -> Iterator[tuple[float, str]]:
        return pace(self._records(), self.speed, self.heartbeat_interval, self._closed)

    def close(self) -> None:
        self._closed.set()


def __getattr__(name: str):
    if name == "PineBufferSource":
        from .pine_source import PineBufferSource

        return PineBufferSource
    raise AttributeError(name)
