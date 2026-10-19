"""Timed test runs: fixed-length DPS measurement. Core module: no UI imports.

See docs/CONTRACTS.md section "Phase 7". Time is event/engine time (event ts, heartbeats), never the wall clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .events import KIND_DAMAGE, Event

ARMED, RUNNING, DONE, CANCELLED = "armed", "running", "done", "cancelled"


@dataclass
class TestCharacter:
    __test__ = False  # not a pytest class
    name: str
    key: str
    player_total: int = 0
    player_hits: int = 0
    player_max: int = 0
    pet_total: int = 0
    pet_hits: int = 0
    pet_max: int = 0
    pet_name: str | None = None
    combined: int = 0
    dps: float = 0.0

    @property
    def hits(self) -> int:
        return self.player_hits + self.pet_hits

    @property
    def max_hit(self) -> int:
        return max(self.player_max, self.pet_max)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TestCharacter":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TestResult:
    __test__ = False
    label: str
    length_s: float
    start_ts: float
    complete: bool = True       # False: the source ended before the clock reached start + length_s
    characters: list[TestCharacter] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(c.combined for c in self.characters)

    @property
    def hits(self) -> int:
        return sum(c.hits for c in self.characters)

    @property
    def max_hit(self) -> int:
        return max((c.max_hit for c in self.characters), default=0)

    @property
    def dps(self) -> float:
        return self.total / self.length_s

    @property
    def pet_names(self) -> list[str]:
        return [c.pet_name for c in self.characters if c.pet_name]

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "length_s": self.length_s, "start_ts": self.start_ts,
                "complete": self.complete, "characters": [c.to_dict() for c in self.characters]}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TestResult":
        return cls(label=str(d["label"]), length_s=float(d["length_s"]), start_ts=float(d["start_ts"]),
                   complete=bool(d.get("complete", True)),
                   characters=[TestCharacter.from_dict(c) for c in d.get("characters", [])])


DEFAULT_GROUPS: tuple[dict[str, Any], ...] = ({"name": "You", "key": "you", "player": "you", "pet": "pet"},)


class TestRun:
    """One timed test. Feed it every Event (``feed``) and every clock tick (``tick``) in order.

    armed -> running at the first own damage Event; running -> done when the clock reaches start + length_s.
    """
    __test__ = False

    def __init__(self, label: str, length_s: float) -> None:
        if not length_s > 0:
            raise ValueError("test length must be > 0")
        self.label = label
        self.length_s = float(length_s)
        self.state = ARMED
        self.start_ts: float | None = None
        self.now: float | None = None            # latest engine time seen
        self.complete = True
        self._chars: dict[str, TestCharacter] = {}
        self._names: dict[str, str] = {}         # character key -> latest display name
        self._pet_npc: dict[str, str] = {}       # character key -> latest pet entity name

    # -- state ------------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.state in (ARMED, RUNNING)

    @property
    def finished(self) -> bool:
        return self.state == DONE

    def remaining(self) -> float | None:
        """Seconds left on the engine clock: the full length while armed, None once over."""
        if self.state == ARMED:
            return self.length_s
        if self.state != RUNNING:
            return None
        return max(0.0, self.start_ts + self.length_s - (self.now if self.now is not None else self.start_ts))

    def status_text(self) -> str:
        if self.state == ARMED:
            return f"Test '{self.label}': armed"
        if self.state == RUNNING:
            left = int(self.remaining() + 0.999)
            return f"Test '{self.label}': {left // 60}:{left % 60:02d} left"
        return f"Test '{self.label}': {self.state}"

    def cancel(self) -> None:
        if self.active:
            self.state = CANCELLED

    # -- input ------------------------------------------------------------------------------------------
    def tick(self, ts: float) -> None:
        """Advance the clock (heartbeat, unmatched line, or the time of an Event)."""
        if not self.active:
            return
        if self.now is None or ts > self.now:
            self.now = ts
        if self.state == RUNNING and ts >= self.start_ts + self.length_s:
            self.state = DONE

    def feed(self, event: Event, groups: Sequence[Mapping[str, Any]] | None = None,
             pet_names: Mapping[str, str] | None = None) -> None:
        """`groups`: the followed characters [{"name","key","player","pet"}] (None = one "You");
        `pet_names`: pet actor key -> pet entity name."""
        self.tick(event.ts)
        if not self.active or event.kind != KIND_DAMAGE:
            return
        owner = None
        for g in (groups or DEFAULT_GROUPS):
            if event.actor == g["player"]:
                owner = (g, False)
            elif event.actor == g["pet"]:
                owner = (g, True)
            if owner:
                break
        if owner is None:
            return
        g, is_pet = owner
        if self.state == ARMED:
            self.state, self.start_ts = RUNNING, event.ts
        if not self.start_ts <= event.ts < self.start_ts + self.length_s:
            return
        key = g.get("key") or g["player"]
        c = self._chars.get(key)
        if c is None:
            c = self._chars[key] = TestCharacter(name=g["name"], key=key)
        self._names[key] = g["name"]
        if is_pet:
            c.pet_total += event.amount
            c.pet_hits += 1
            c.pet_max = max(c.pet_max, event.amount)
            npc = (pet_names or {}).get(g["pet"])
            if npc:
                self._pet_npc[key] = npc
        else:
            c.player_total += event.amount
            c.player_hits += 1
            c.player_max = max(c.player_max, event.amount)

    def end_of_stream(self) -> None:
        """The source ended: a running test is finalized as incomplete, an armed one is cancelled."""
        if self.state == RUNNING:
            self.complete = False
            self.state = DONE
        elif self.state == ARMED:
            self.state = CANCELLED

    # -- output -----------------------------------------------------------------------------------------
    def result(self) -> TestResult | None:
        if self.state != DONE:
            return None
        chars = []
        for key, c in self._chars.items():
            out = TestCharacter.from_dict(c.to_dict())
            out.name = self._names.get(key, c.name)
            out.pet_name = self._pet_npc.get(key)
            out.combined = c.player_total + c.pet_total
            out.dps = out.combined / self.length_s
            chars.append(out)
        return TestResult(self.label, self.length_s, self.start_ts, self.complete, chars)
