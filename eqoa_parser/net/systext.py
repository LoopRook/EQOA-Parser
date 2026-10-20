"""System text (message opcode 0x0A7A) -> typed progress records, and per-connection totals. Core: stdlib only.

Layout (CONFIRMED on all 226 messages of a 9-minute capture): ``7a 0a <n u32 LE> <n UTF-16LE code units>`` -- the body
is exactly ``6 + 2n`` bytes. Texts seen (numbers vary, commas tolerated):
  ``You receive 1200 XP.``  /  ``You receive 0 XP. 166253 was paid towards your debt of 31726780.``
  ``You received 166253 class mastery XP.``   ``You receive 13 tunar, your share of the group loot.``
  ``1778 faction with Citizens of Freeport (+1)`` (standing, then the change)   ``<name> loots <item>``
Anything else (chat, errors, ``You receive 50000 tunar in bank.``) parses to None and is ignored; text is never stored
or logged. A kill is counted once per ``You receive N XP`` message: it is the one message that carries meaning in the
kill marker group (0x0020 is a constant, 0x0052 is missing at 2 of 21 kills), and on that capture it gave 20 kills per character, the same as the 0x00D9 markers
once the one re-sent datagram (21 raw) is dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OP_SYSTEXT = 0x0A7A

_N = r"(\d[\d,]*)"
_XP = re.compile(rf"^You receive {_N} XP\.(?: {_N} was paid towards your debt of {_N}\.)?$")
_MASTERY = re.compile(rf"^You received {_N} class mastery XP\.$")
_TUNAR = re.compile(rf"^You receive {_N} tunar(?:, [^.]*)?\.$")
_FACTION = re.compile(r"^([+-]?\d+) faction with (.+) \(([+-]\d+)\)$")
_LOOT = re.compile(r"^(\S+) loots (.+)$")


def _int(text: str) -> int:
    return int(text.replace(",", ""))


@dataclass(frozen=True)
class XpGain:
    xp: int
    debt_paid: int = 0      # XP that went to the debt instead
    debt_left: int = 0


@dataclass(frozen=True)
class MasteryXp:
    amount: int


@dataclass(frozen=True)
class Tunar:
    amount: int


@dataclass(frozen=True)
class Faction:
    name: str
    standing: int
    delta: int


@dataclass(frozen=True)
class Loot:
    looter: str
    item: str


SysRecord = XpGain | MasteryXp | Tunar | Faction | Loot


def decode_text(body: bytes) -> str | None:
    """The text of a 0x0A7A message body (opcode included), or None if the length prefix does not fit."""
    if len(body) < 6:
        return None
    n = int.from_bytes(body[2:6], "little")
    if 6 + 2 * n != len(body):
        return None
    try:
        return body[6:].decode("utf-16le")
    except UnicodeDecodeError:
        return None


def parse_text(text: str) -> SysRecord | None:
    m = _XP.match(text)
    if m:
        return XpGain(_int(m[1]), _int(m[2]) if m[2] else 0, _int(m[3]) if m[3] else 0)
    m = _MASTERY.match(text)
    if m:
        return MasteryXp(_int(m[1]))
    m = _TUNAR.match(text)
    if m:
        return Tunar(_int(m[1]))
    m = _FACTION.match(text)
    if m:
        return Faction(m[2], int(m[1]), int(m[3]))
    m = _LOOT.match(text)
    if m:
        return Loot(m[1], m[2])
    return None


@dataclass
class Totals:
    """What one connection's character earned so far."""
    kills: int = 0
    xp: int = 0
    mastery: int = 0
    debt: int = 0
    tunar: int = 0
    loot: int = 0          # loot lines seen (the whole group's, not only yours)
    factions: dict[str, int] = field(default_factory=dict)   # name -> summed change


class ProgressTracker:
    """Totals per connection (client port)."""

    def __init__(self) -> None:
        self.by_conn: dict[int, Totals] = {}

    def add(self, conn: int, rec: SysRecord) -> None:
        t = self.by_conn.setdefault(conn, Totals())
        if isinstance(rec, XpGain):
            t.kills += 1
            t.xp += rec.xp
            t.debt += rec.debt_paid
        elif isinstance(rec, MasteryXp):
            t.mastery += rec.amount
        elif isinstance(rec, Tunar):
            t.tunar += rec.amount
        elif isinstance(rec, Loot):
            t.loot += 1
        else:
            t.factions[rec.name] = t.factions.get(rec.name, 0) + rec.delta
