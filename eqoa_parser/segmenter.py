"""Fight segmentation. Core module: no UI imports. See docs/CONTRACTS.md section "segmenter"."""
from __future__ import annotations

from dataclasses import dataclass, field

from .events import ACTIVITY_KINDS, Event


@dataclass
class Fight:
    fight_id: int          # 1-based, sequential within a run
    start_ts: float        # ts of the first activity (damage/damage_taken) event
    end_ts: float          # ts of the latest activity event (misses/deaths never move it)
    events: list[Event] = field(default_factory=list)  # all events attached to this fight, in feed order

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts


class Segmenter:
    """Rules (time = event ts, never wall clock):
    - Activity event = kind in events.ACTIVITY_KINDS.
    - feed(e) first does tick(e.ts).
    - Activity event, no current fight -> new fight (start_ts = end_ts = e.ts).
    - Activity event, current fight -> appended; end_ts = max(end_ts, e.ts).
    - Non-activity event, current fight -> appended; end_ts unchanged. No current fight -> dropped.
    - tick(now): if a current fight exists and now - end_ts >= idle_timeout, it is closed and returned.
    - flush(): closes and returns the current fight, if any (end of stream).
    """

    def __init__(self, idle_timeout: float = 8.0, first_fight_id: int = 1) -> None:
        self.idle_timeout = idle_timeout
        self._next_id = first_fight_id
        self._current: Fight | None = None

    @property
    def current(self) -> Fight | None:
        return self._current

    def feed(self, event: Event) -> list[Fight]:
        """Returns fights closed as a result of this event (0 or 1)."""
        closed = self.tick(event.ts)
        if event.kind in ACTIVITY_KINDS:
            if self._current is None:
                self._current = Fight(self._next_id, event.ts, event.ts)
                self._next_id += 1
            else:
                self._current.end_ts = max(self._current.end_ts, event.ts)
            self._current.events.append(event)
        elif self._current is not None:
            self._current.events.append(event)
        return closed

    def tick(self, now: float) -> list[Fight]:
        cur = self._current
        if cur is not None and now - cur.end_ts >= self.idle_timeout:
            self._current = None
            return [cur]
        return []

    def flush(self) -> list[Fight]:
        cur, self._current = self._current, None
        return [cur] if cur is not None else []
