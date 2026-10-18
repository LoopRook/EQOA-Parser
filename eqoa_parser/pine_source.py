"""PineBufferSource: a LogSource that polls a text buffer in EE RAM through the read-only PINE client.

Core module: no UI imports.

EVERYTHING ABOUT THE REAL BUFFER IS UNKNOWN. The layouts below are ASSUMED shapes, and ALL DEDUPE LOGIC IN THIS FILE
IS UNVERIFIED against real PCSX2 / EQOA memory (it is tested only against the mock server and synthetic buffers).

Layout strategies (pluggable: subclass BufferLayout and `register_layout(name, cls)`; select via buffer.layout):

  "scroll"  ASSUMED: a contiguous region of NUL/newline separated lines. New lines are appended after the last one
            and, once the region is full, the oldest lines drop off the front (whole-line shifts).
            Dedupe: compare the complete lines of this poll with the previous poll; the largest k such that the
            last k previous lines equal the first k current lines (the first of them may be a truncated tail, the last
            may have grown since it was half written) is the overlap, everything after it is new. Unchanged memory ->
            k == everything -> nothing new. If the previous poll's newest line was half written and has grown, it does
            not break the overlap (older lines are not re-emitted); the grown line is emitted again. Lines are emitted in
            the poll that first shows them, so a half-written line that is observed is emitted truncated and again
            complete (cannot be retracted; stable_reads is the only guard).
            A genuinely repeated identical message is still new when it extends the sequence
            ([A] -> [A, A] yields one A).
            CANNOT be told apart (documented limits): (a) a full window that scrolls by n lines where the n new
            lines equal the n lines that fell off ([A,B,A,B] -> [A,B,A,B]) looks unchanged -> dropped;
            (b) more new lines between two polls than fit the region -> the overflow is lost and, if nothing
            overlaps, the whole window is re-emitted; (c) a buffer cleared and rewritten with the same lines between
            two polls looks unchanged.
  "ring"    ASSUMED: a fixed-size ring: new text overwrites the oldest text in place, a write cursor wraps.
            Dedupe is positional: a line is new iff some byte in its span differs from the previous poll's bytes at
            the same offsets. Unchanged remnants of older longer lines are therefore ignored, identical messages in
            different slots are distinct, wraparound is natural. Within one poll the new lines are ordered starting at
            the offset where the previous poll's newest line ended (wrapping), which is a guess at the write cursor.
            A message whose bytes wrap past the end of the region (unterminated tail + head at offset 0) is
            reassembled from tail and head. Ring lines are emitted immediately, so a message caught mid-write is
            emitted truncated and again complete (UNVERIFIED; stable_reads is the only guard).
            CANNOT be told apart: a message overwritten by byte-identical text at the same offset; a ring that
            cycled completely between two polls (lost data, no way to know).

Torn reads: with buffer.stable_reads (default) a poll re-reads until two consecutive reads are identical (max
MAX_STABLE_TRIES) so a message being written mid-poll is not emitted half-finished. UNVERIFIED whether this is
needed or sufficient. Text already in the buffer at start is history and is not yielded unless buffer.emit_initial.
An unterminated final segment (no NUL/newline after it inside the region) is never emitted: it may be incomplete.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from .config import Config
from .pine import TEXT_CODECS, PineClient, PineError
from .sources import HEARTBEAT

MAX_STABLE_TRIES = 3
_CODECS = TEXT_CODECS
_TERM_BYTES = re.compile(rb"[\x00\r\n]+")


class BufferNotConfiguredError(RuntimeError):
    """buffer.address is None: the chat/combat buffer has not been located yet."""


@dataclass(frozen=True)
class Segment:
    start: int       # byte offset in the region
    end: int         # byte offset one past the last text byte (terminator excluded)
    text: str
    terminated: bool  # a terminator follows inside the region


def split_segments(raw: bytes, encoding: str) -> list[Segment]:
    """Split a raw region into non-empty text segments separated by NUL / CR / LF (in `encoding`'s code units)."""
    codec = _CODECS[encoding]
    spans: list[tuple[int, int, bool]] = []
    if encoding == "utf16le":
        unit_start = None
        n = len(raw) // 2 * 2
        for i in range(0, n, 2):
            term = raw[i + 1] == 0 and raw[i] in (0, 10, 13)
            if term:
                if unit_start is not None:
                    spans.append((unit_start, i, True))
                    unit_start = None
            elif unit_start is None:
                unit_start = i
        if unit_start is not None:
            spans.append((unit_start, n, False))
    else:
        pos = 0
        for m in _TERM_BYTES.finditer(raw):
            if m.start() > pos:
                spans.append((pos, m.start(), True))
            pos = m.end()
        if pos < len(raw):
            spans.append((pos, len(raw), False))
    return [Segment(s, e, raw[s:e].decode(codec, errors="replace"), t) for s, e, t in spans]


class BufferLayout:
    """Strategy: given the raw region bytes of each poll, return the NEW lines since the previous poll."""

    def __init__(self, encoding: str, emit_initial: bool = False) -> None:
        if encoding not in _CODECS:
            raise ValueError(f"unknown encoding {encoding!r}; use one of {sorted(_CODECS)}")
        self.encoding = encoding
        self.emit_initial = emit_initial
        self._primed = False

    def update(self, raw: bytes) -> list[str]:
        new = self._update(raw)
        if not self._primed:
            self._primed = True
            return new if self.emit_initial else []
        return new

    def _update(self, raw: bytes) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError


class ScrollLayout(BufferLayout):
    """See module docstring, layout "scroll". UNVERIFIED.

    The previous poll's newest line is treated as possibly half written: if it GREW since (the old text is a proper
    prefix of the line now at that position) the overlap with the older lines still holds, nothing older is re-emitted,
    and the grown line is emitted again as a changed line. New lines are emitted in the poll that first shows them (the
    verification tests require this), so a half-written line that stays visible is emitted truncated first and
    complete later (same behaviour as the ring layout); it cannot be retracted.
    """

    def __init__(self, encoding: str, emit_initial: bool = False) -> None:
        super().__init__(encoding, emit_initial)
        self._prev: list[str] = []

    @staticmethod
    def _overlap(prev: list[str], cur: list[str]) -> int:
        for k in range(min(len(prev), len(cur)), 0, -1):
            ok = True
            for j in range(k):
                p, c = prev[len(prev) - k + j], cur[j]
                # first overlapping line may be truncated at the front of the window; the last (previous newest)
                # line may have been half written and grown since
                if not (p == c or (j == 0 and p.endswith(c)) or (j == k - 1 and c.startswith(p))):
                    ok = False
                    break
            if ok:
                return k
        return 0

    def _update(self, raw: bytes) -> list[str]:
        cur = [s.text for s in split_segments(raw, self.encoding) if s.terminated]
        prev, self._prev = self._prev, cur
        k = self._overlap(prev, cur)
        out: list[str] = []
        if k and cur[k - 1] != prev[-1] and cur[k - 1].startswith(prev[-1]):
            out.append(cur[k - 1])  # the previous newest line grew: report its new text, keep older lines quiet
        return out + cur[k:]


class RingLayout(BufferLayout):
    """See module docstring, layout "ring". UNVERIFIED."""

    def __init__(self, encoding: str, emit_initial: bool = False) -> None:
        super().__init__(encoding, emit_initial)
        self._prev: bytes | None = None
        self._cursor = 0

    def _entries(self, raw: bytes) -> list[tuple[int, int, list[tuple[int, int]], str]]:
        """(start, end, byte spans, text) per complete message. A message that wraps past the end of the region
        (unterminated tail + a head starting at offset 0) is reassembled from tail and head. ASSUMED/UNVERIFIED."""
        tw = 2 if self.encoding == "utf16le" else 1  # include the terminator: a shorter overwrite changes it
        segs = split_segments(raw, self.encoding)
        out = []
        if len(segs) >= 2 and not segs[-1].terminated and segs[0].start == 0 and segs[0].terminated:
            tail, head = segs[-1], segs[0]
            text = (raw[tail.start :] + raw[: head.end]).decode(_CODECS[self.encoding], errors="replace")
            out.append((tail.start, head.end, [(tail.start, len(raw)), (0, head.end + tw)], text))
            segs = segs[1:-1]
        out += [(s.start, s.end, [(s.start, s.end + tw)], s.text) for s in segs if s.terminated]
        return out

    def _update(self, raw: bytes) -> list[str]:
        prev, self._prev = self._prev, raw
        entries = self._entries(raw)
        if prev is None:
            if entries:
                self._cursor = max(e[1] for e in entries)
            return [e[3] for e in entries]
        if prev == raw:
            return []
        changed = [e for e in entries if any(raw[a:b] != prev[a:b] for a, b in e[2])]
        if not changed:
            return []
        # order: continue from the old write cursor, wrapping around the end of the region
        changed.sort(key=lambda e: (e[0] < self._cursor, e[0]))
        self._cursor = changed[-1][1]
        return [e[3] for e in changed]


LAYOUTS: dict[str, Callable[..., BufferLayout]] = {"scroll": ScrollLayout, "ring": RingLayout}


def register_layout(name: str, factory: Callable[..., BufferLayout]) -> None:
    LAYOUTS[name] = factory


class PineBufferSource:
    """LogSource over a PINE-readable text buffer. See module docstring: ALL DEDUPE UNVERIFIED.

    config: eqoa_parser.config.Config (uses .buffer and .pine). client/layout/clock are injectable for tests.
    """

    def __init__(
        self,
        config: Config,
        *,
        client: PineClient | None = None,
        layout: BufferLayout | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        buf = config.buffer
        if buf.address is None:
            raise BufferNotConfiguredError(
                "buffer.address is not set: the chat/combat text buffer has not been located yet. "
                "Follow docs/finding-the-buffer.md, then put the address in config.yaml under buffer.address."
            )
        if buf.layout not in LAYOUTS and layout is None:
            raise ValueError(f"unknown buffer.layout {buf.layout!r}; known: {sorted(LAYOUTS)}")
        self.config = config
        self.client = client if client is not None else PineClient(config.pine)
        self.layout = layout if layout is not None else LAYOUTS[buf.layout](buf.encoding, buf.emit_initial)
        self._clock = clock
        self._stop = threading.Event()
        self._last_ts = float("-inf")
        self._ever_read = False
        self.last_error: PineError | None = None

    def _read_region(self) -> bytes:
        buf = self.config.buffer
        raw = self.client.read_bytes(buf.address, buf.size)
        if buf.stable_reads:
            for _ in range(MAX_STABLE_TRIES - 1):
                again = self.client.read_bytes(buf.address, buf.size)
                if again == raw:
                    break
                raw = again
        return raw

    def poll_once(self) -> list[str]:
        """One read + dedupe step; returns the new lines (possibly empty). Raises PineError on read failure."""
        lines = self.layout.update(self._read_region())
        self._ever_read = True
        self.last_error = None
        return lines

    def _now(self) -> float:
        self._last_ts = max(self._last_ts, self._clock())  # keep ts non-decreasing even if the clock steps back
        return self._last_ts

    def __iter__(self) -> Iterator[tuple[float, str]]:
        while not self._stop.is_set():
            try:
                lines = self.poll_once()
            except PineError as exc:
                if not self._ever_read:
                    raise  # fail loudly on the very first read (PCSX2 not running / no game / wrong slot)
                self.last_error = exc  # later drop-outs: keep heartbeating, reconnect on the next poll
                lines = []
            ts = self._now()
            if lines:
                for line in lines:
                    yield ts, line
            else:
                yield ts, HEARTBEAT
            self._stop.wait(self.config.buffer.poll_interval)

    def close(self) -> None:
        self._stop.set()
        self.client.close()

