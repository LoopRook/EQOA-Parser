"""Engine: source -> parser -> segmenter -> stats. Core module: no UI imports.

See docs/CONTRACTS.md section "pipeline".
"""
from __future__ import annotations

import threading
from typing import Callable

from .config import Config
from .events import KIND_DAMAGE, Event
from .parser import Parser
from .segmenter import Fight, Segmenter
from .sources import HEARTBEAT, LogSource
from .testmode import TestResult, TestRun
from .stats import FightSummary, RunSummary, summarize_fight, summarize_run

LiveCallback = Callable[[FightSummary], None]
ClosedCallback = Callable[[FightSummary], None]


class Engine:
    """Consumes a LogSource and maintains fight state for one run.

    Callbacks run on the thread that calls run(); a UI must marshal them to its own thread.
      on_live(summary)    -- after every event attached to the current fight (summary.ongoing=True)
      on_closed(summary)  -- when a fight closes (idle timeout, tick, or end of stream)
      Unmatched and ignored lines advance the clock like heartbeats (contract; they tick the segmenter).
      on_finished()       -- once, after the source is exhausted/closed and the last fight flushed
    """

    def __init__(
        self,
        config: Config,
        source: LogSource,
        *,
        parser: Parser | None = None,  # default: Parser built from config (patterns, unmatched log, names)
        on_live: LiveCallback | None = None,
        on_closed: ClosedCallback | None = None,
        on_finished: Callable[[], None] | None = None,
        on_test: Callable[[TestResult], None] | None = None,  # once, when a timed test finishes
    ) -> None:
        self.config = config
        self.source = source
        self._owns_parser = parser is None
        self.parser = parser if parser is not None else Parser(
            config.resolve(config.patterns_file),
            unmatched_path=config.resolve(config.unmatched_log),
            pet_names=config.pet_names,
            player_name=config.player_name,
        )
        self._segmenter = Segmenter(config.idle_timeout)
        self._on_live = on_live
        self._on_closed = on_closed
        self._on_finished = on_finished
        self._on_test = on_test
        self._test: TestRun | None = None
        self._closed: list[FightSummary] = []
        self._lock = threading.RLock()
        self._finished = False

    def run(self, stop: threading.Event | None = None) -> None:
        """Blocking. Stops when the source ends or `stop` is set (then closes the source)."""
        try:
            for ts, raw in self.source:
                if stop is not None and stop.is_set():
                    break
                self.step(ts, raw)
                if stop is not None and stop.is_set():
                    break
        finally:
            self.source.close()
            self.finish()

    def step(self, ts: float, raw: "str | Event") -> None:
        """Process one (ts, item) tuple; HEARTBEAT only ticks the segmenter. Used by run().

        `item` is a text line (parsed with the Parser) or an already-built Event (packet sources), which goes
        straight to the segmenter.

        Lines that parse to no event (ignored, unmatched) also advance the clock like a heartbeat.
        """
        if isinstance(raw, Event):
            event = raw
        else:
            event = None if raw == HEARTBEAT else self.parser.parse(ts, raw)
        live: FightSummary | None = None
        with self._lock:
            tested = self._step_test(ts, event)
            if event is None:
                closed = self._segmenter.tick(ts)
            else:
                closed = self._segmenter.feed(event)
                cur = self._segmenter.current
                if cur is not None and cur.events and cur.events[-1] is event:
                    live = summarize_fight(cur, ongoing=True, own_actors=self._own_actors(), groups=self._groups(cur),
                                         pet_names=self._pet_names())
            summaries = [self._record(f) for f in closed]
        if tested is not None and self._on_test is not None:
            self._on_test(tested)
        for s in summaries:
            if self._on_closed is not None:
                self._on_closed(s)
        if live is not None and self._on_live is not None:
            self._on_live(live)

    def _own_actors(self) -> list[str] | None:
        """Actor names that count as "ours" when the source follows several characters; None for text sources."""
        fn = getattr(self.source, "own_actors", None)
        return list(fn()) if callable(fn) else None

    # -- timed test (docs/CONTRACTS.md "Phase 7"); the segmenter never sees it ---------------------------
    @property
    def test(self) -> TestRun | None:
        return self._test

    def arm_test(self, test: TestRun) -> None:
        """Make `test` the current test (replacing a finished or cancelled one; an active one is cancelled)."""
        with self._lock:
            if self._test is not None:
                self._test.cancel()
            self._test = test

    def cancel_test(self) -> None:
        with self._lock:
            if self._test is not None:
                self._test.cancel()

    def _step_test(self, ts: float, event: Event | None) -> TestResult | None:
        """Feed the test (lock held); returns its result on the step it finishes."""
        t = self._test
        if t is None or not t.active:
            return None
        if event is None:
            t.tick(ts)
        elif event.kind == KIND_DAMAGE:
            t.feed(event, self._groups(None), self._pet_names())
        else:
            t.tick(event.ts)
        return t.result() if t.finished else None

    def _pet_names(self) -> dict[str, str] | None:
        """Pet actor key -> pet entity name (PINE only); None when none is known."""
        fn = getattr(self.source, "character_groups", None)
        if not callable(fn):
            return None
        out = {g.pet_key: g.pet_npc for g in fn() if getattr(g, "pet_npc", None)}
        return out or None

    def _groups(self, fight: Fight | None) -> list[dict] | None:
        """Characters followed by the source, as stored per fight: [{"name","player","pet"}]; None for text sources
        or while nothing is followed yet (= one "You")."""
        fn = getattr(self.source, "character_groups", None)
        if not callable(fn):
            return None
        # a character first seen after the fight ended is not part of it
        groups = [{"name": g.name, "key": g.player_key, "player": g.player_key, "pet": g.pet_key}
                  for g in fn() if fight is None or getattr(g, "first_ts", 0.0) <= fight.end_ts]
        return groups or None

    def _record(self, fight: Fight) -> FightSummary:
        s = summarize_fight(fight, own_actors=self._own_actors(), groups=self._groups(fight),
                           pet_names=self._pet_names())
        self._closed.append(s)
        return s

    def finish(self) -> None:
        """Flush the current fight (firing on_closed) and fire on_finished. Idempotent."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            tested = None
            if self._test is not None and self._test.active:
                self._test.end_of_stream()
                tested = self._test.result()
            summaries = [self._record(f) for f in self._segmenter.flush()]
        try:
            if tested is not None and self._on_test is not None:
                self._on_test(tested)
            for s in summaries:
                if self._on_closed is not None:
                    self._on_closed(s)
            if self._on_finished is not None:
                self._on_finished()
        finally:
            if self._owns_parser:
                self.parser.close()

    @property
    def closed_fights(self) -> list[FightSummary]:
        with self._lock:
            return list(self._closed)

    def current_summary(self) -> FightSummary | None:
        with self._lock:
            cur = self._segmenter.current
            if cur is None:
                return None
            return summarize_fight(cur, ongoing=True, own_actors=self._own_actors(), groups=self._groups(cur),
                                         pet_names=self._pet_names())

    def run_summary(self) -> RunSummary:
        """Over this run's closed fights only."""
        with self._lock:
            fn = getattr(self.source, "progress", None)
            rows, elapsed = fn() if callable(fn) else ((), 0.0)
            return summarize_run(self._closed, rows, elapsed)
