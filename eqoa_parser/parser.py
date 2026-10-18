"""Line parser. Core module: no UI imports. See docs/CONTRACTS.md section "parser".

Pattern formats are SYNTHETIC/ASSUMED until verified against real captures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .events import (
    ACTOR_PET,
    ACTOR_YOU,
    KIND_DAMAGE,
    KIND_DAMAGE_TAKEN,
    KIND_DEATH,
    KIND_HEAL,
    KIND_MISS,
    KNOWN_KINDS,
    Event,
)
from .sources import HEARTBEAT

KIND_IGNORE = "ignore"
_VALID_KINDS = KNOWN_KINDS | {KIND_IGNORE}
_STATUSES = {"ASSUMED", "VERIFIED"}
UNKNOWN_ACTOR = "unknown"  # actor of a pattern that yields no actor (e.g. a death with no killer)


@dataclass(frozen=True)
class Pattern:
    name: str
    kind: str                 # an events.KIND_* value, or "ignore" (matched noise: no Event, not logged)
    regex: str
    actor: str | None = None  # fixed value used when the regex has no `actor` group
    target: str | None = None
    ability: str | None = None
    status: str = "ASSUMED"   # ASSUMED | VERIFIED


def _validate(p: Pattern) -> re.Pattern[str]:
    """Compile `p.regex` and check required fields; ValueError mentions the pattern name."""
    name = p.name
    if not isinstance(name, str) or not name:
        raise ValueError(f"pattern {name!r}: missing name")
    if p.kind not in _VALID_KINDS:
        raise ValueError(f"pattern {name!r}: invalid kind {p.kind!r} (expected one of {sorted(_VALID_KINDS)})")
    if not isinstance(p.regex, str) or not p.regex:
        raise ValueError(f"pattern {name!r}: missing regex")
    if p.status not in _STATUSES:
        raise ValueError(f"pattern {name!r}: invalid status {p.status!r}")
    try:
        rx = re.compile(p.regex)
    except re.error as exc:
        raise ValueError(f"pattern {name!r}: bad regex: {exc}") from exc
    if p.kind == KIND_IGNORE:
        return rx
    groups = rx.groupindex
    if p.kind in (KIND_DAMAGE, KIND_DAMAGE_TAKEN) and "amount" not in groups:
        raise ValueError(f"pattern {name!r}: kind {p.kind} needs an `amount` group")
    needs = {
        KIND_DAMAGE: ("actor", "target"),
        KIND_DAMAGE_TAKEN: ("actor", "target"),
        KIND_MISS: ("actor", "target"),
        KIND_DEATH: ("target",),
        KIND_HEAL: ("actor", "target"),
    }[p.kind]
    for field in needs:
        if field not in groups and getattr(p, field) is None:
            raise ValueError(f"pattern {name!r}: kind {p.kind} needs a `{field}` group or fixed `{field}`")
    return rx


def load_patterns(path: str | Path) -> list[Pattern]:
    """Load and validate patterns.yaml. Raises ValueError on bad regex/kind/missing fields."""
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("patterns"), list):
        raise ValueError(f"{path}: expected a mapping with a `patterns` list")
    out: list[Pattern] = []
    seen: set[str] = set()
    allowed = {"name", "kind", "regex", "actor", "target", "ability", "status"}
    for i, entry in enumerate(doc["patterns"]):
        if not isinstance(entry, dict):
            raise ValueError(f"pattern #{i}: must be a mapping")
        name = entry.get("name")
        label = name if isinstance(name, str) and name else f"#{i}"
        unknown = set(entry) - allowed
        if unknown:
            raise ValueError(f"pattern {label!r}: unknown keys {sorted(unknown)}")
        for req in ("name", "kind", "regex"):
            if not entry.get(req) or not isinstance(entry[req], str):
                raise ValueError(f"pattern {label!r}: missing required field `{req}`")
        for opt in ("actor", "target", "ability"):
            if entry.get(opt) is not None and not isinstance(entry[opt], str):
                raise ValueError(f"pattern {label!r}: `{opt}` must be a string")
        if name in seen:
            raise ValueError(f"pattern {name!r}: duplicate name")
        seen.add(name)
        pat = Pattern(
            name=name, kind=entry["kind"], regex=entry["regex"], actor=entry.get("actor"),
            target=entry.get("target"), ability=entry.get("ability"), status=entry.get("status", "ASSUMED"),
        )
        _validate(pat)
        out.append(pat)
    return out


_ARTICLE = re.compile(r"(a|an|the) ", re.IGNORECASE)
_YOU = frozenset({"you", "yourself"})
_PET = frozenset({"your pet"})


MAX_LINE_LEN = 512   # longer stripped lines are unmatched without regex matching (bounds regex cost)
MAX_AMOUNT_DIGITS = 9

_ESC = {ord("\\"): "\\\\", ord("\r"): "\\r", ord("\n"): "\\n", ord("\t"): "\\t"}
for _c in [*range(0x20), 0x7F, 0x85, 0x2028, 0x2029]:
    _ESC.setdefault(_c, f"\\x{_c:02x}" if _c < 0x100 else f"\\u{_c:04x}")


def _escape(raw: str) -> str:
    """Make `raw` one physical line for any str.splitlines() reader (backslash, controls, separators)."""
    return raw.translate(_ESC)


_UNMATCHED = object()


class Parser:
    def __init__(
        self,
        patterns: str | Path | list[Pattern],
        *,
        unmatched_path: str | Path | None = None,
        pet_names: Iterable[str] = (),
        player_name: str | None = None,
    ) -> None:
        if isinstance(patterns, (str, Path)):
            patterns = load_patterns(patterns)
        self._compiled = [(p, _validate(p)) for p in patterns]
        self._pet = _PET | {n.strip().lower() for n in pet_names if n and n.strip()}
        self._you = set(_YOU)
        if player_name and player_name.strip():
            self._you.add(player_name.strip().lower())
        self._unmatched_count = 0
        self._fh = None
        if unmatched_path is not None:
            up = Path(unmatched_path)
            up.parent.mkdir(parents=True, exist_ok=True)
            self._fh = up.open("a", encoding="utf-8", errors="backslashreplace", newline="\n")

    def _normalize(self, name: str | None) -> str | None:
        if name is None:
            return None
        name = name.strip()
        low = name.lower()
        if low in self._you:
            return ACTOR_YOU
        if low in self._pet:
            return ACTOR_PET
        m = _ARTICLE.match(name)
        if m:
            name = name[: m.end()].lower() + name[m.end():]
        return name

    def _match(self, ts: float, raw: str, line: str):
        for pat, rx in self._compiled:
            m = rx.fullmatch(line)
            if m is None:
                continue
            if pat.kind == KIND_IGNORE:
                return None
            g = m.groupdict()
            if pat.kind in (KIND_DAMAGE, KIND_DAMAGE_TAKEN):
                digits = g["amount"]
                if len(digits) > MAX_AMOUNT_DIGITS:
                    return _UNMATCHED
                amount = int(digits)
            else:
                amount = 0
            actor = self._normalize(g.get("actor") or pat.actor)
            target = self._normalize(g.get("target") or pat.target)
            ability = g.get("ability") or pat.ability
            return Event(
                ts=ts, actor=actor if actor is not None else UNKNOWN_ACTOR, target=target,
                amount=amount, ability=ability, kind=pat.kind, raw=raw,
            )
        return _UNMATCHED

    def parse(self, ts: float, raw: str) -> Event | None:
        """First matching pattern wins. Returns None for HEARTBEAT, "ignore" matches and unmatched
        lines; unmatched lines are appended to unmatched_path as "<ts>\\t<raw>\\n"."""
        line = raw.strip()
        if raw == HEARTBEAT or not line:
            return None
        if len(line) <= MAX_LINE_LEN:
            try:
                ev = self._match(ts, raw, line)
            except Exception:  # never raise on any input line; treat as unmatched
                ev = _UNMATCHED
            if ev is not _UNMATCHED:
                return ev
        self._unmatched_count += 1
        if self._fh is not None:
            self._fh.write(f"{ts!r}\t{_escape(raw)}\n")
            self._fh.flush()
        return None

    @property
    def unmatched_count(self) -> int:
        return self._unmatched_count

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
