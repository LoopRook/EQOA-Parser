"""Protocol records -> Events: player/pet identification and connection selection. Core: no UI imports.

Everything here rests on 2 real captures and is INFERRED unless stated (docs/protocol-notes.md):
  * Player id (INFERRED): the first entity id in a connection's records whose low 16 bits equal the datagram
    header's ``header_low16`` (the header word is CONFIRMED to be the low 16 bits of the character id).
  * Pet id (INFERRED): the attacker of a Swing at target T, other than the player, followed within 1.0 s by an
    *extended* HpChange from the player at T. The first such attacker wins. The pet id is needed only for damage
    *to* the pet.
  * Pet damage (INFERRED): the server credits it to the owner; such records are "extended", so extended
    player-sourced damage becomes actor ``pet``.
  * Unrelated combat (neither source nor target is the player or the learned pet) produces no Event; it is
    counted in ``Attributor.dropped_unrelated``. To be revisited once group members can be identified.
  * Names (INFERRED, net/names.py): anything that is not the player or pet is named from the NameTable when the
    id is known at the time the Event is created, else ``"0x%08X"``. A name arriving mid-fight leaves two target
    labels (hex, then name) in that fight: accepted MVP limitation.
  * Group members (docs/CONTRACTS.md "Phase 5", only with PINE on): records whose source or target is a group member
    who is not a followed connection's player are counted under the member's name (``<Name>'s pet`` for extended
    records), taken from one feeder connection only. Without PINE nothing changes.
  * ``ability`` is None everywhere (not decoded yet).
  * Several emulators (docs/CONTRACTS.md "Following several emulators"): the router follows every connection by
    default. The primary connection (first to produce an Event) keeps ``you``/``pet``; another connection's player
    is its label (name if known at its first Event, else ``char:<port>``; fixed for the run) and its pet is
    ``"<label>'s pet"``. A character's damage is counted only from its own connection (records involving neither
    its player nor its pet are dropped), so nothing is double counted.
"""
from __future__ import annotations

import sys
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable, Iterable, TextIO

from ..config import Config
from ..events import (
    ACTOR_PET,
    ACTOR_YOU,
    KIND_DAMAGE,
    KIND_DAMAGE_TAKEN,
    KIND_HEAL,
    Event,
)
from .memory import MemorySnapshot
from .names import NameTable, OwnNames
from .protocol import HpChange, Record, Swing

PET_WINDOW = 1.0  # seconds between a Swing and the player's extended HpChange at the same target
_SWING_MEMORY = 64


def entity_name(entity_id: int) -> str:
    return "0x%08X" % entity_id


@dataclass(frozen=True)
class CharacterGroup:
    """One followed connection as a character. ``player_key``/``pet_key`` are the (fixed) actor keys in Events;
    ``name`` is the display name and may change while the run goes on."""
    name: str
    player_key: str
    pet_key: str
    port: int
    entity_id: int | None
    first_ts: float = 0.0   # time of the connection's first Event: a fight that ended earlier doesn't list the group


class Attributor:
    """Attribution state for ONE connection (one emulator / character).

    Without a router (``claim_primary`` None) the connection is always the primary one: ``you`` / ``pet``."""

    def __init__(
        self,
        names: NameTable | None = None,
        claim_primary: Callable[["Attributor"], bool] | None = None,
        other_label: Callable[["Attributor", int], str | None] | None = None,
        on_role: Callable[["Attributor", int], None] | None = None,
        claimed_elsewhere: Callable[["Attributor", int], bool] | None = None,
        unique_label: Callable[[str], str] | None = None,
    ) -> None:
        self._claimed_elsewhere = claimed_elsewhere
        self._unique_label = unique_label
        self._recent: deque[set[int]] = deque(maxlen=2)
        self.names = names  # INFERRED entity names (net/names.py); None or unknown id -> hex
        self.player_id: int | None = None
        self.pet_id: int | None = None
        self._h16: int | None = None
        self._swings: deque[Swing] = deque(maxlen=_SWING_MEMORY)
        self.dropped_unrelated = 0  # HpChange records involving neither the player nor the pet (no Event)
        self._claim = claim_primary
        self._other_label = other_label
        self._on_role = on_role
        self.primary = claim_primary is None
        self.label: str | None = None   # non-primary: fixed at the connection's first Event
        self.role_fixed = claim_primary is None
        self.first_ts = 0.0             # ts of the first related record (when the role was fixed)
        self.muted = False              # state-only (explicit --connection followed another port): no roles, no events

    def _ensure_role(self, conn: int, ts: float = 0.0) -> None:
        if self.role_fixed or self.muted:
            return
        self.role_fixed = True
        self.first_ts = ts
        self.primary = bool(self._claim(self)) if self._claim is not None else True
        if not self.primary:
            known = self.names.get(self.player_id) if self.names is not None and self.player_id is not None else None
            self.label = known or f"char:{conn}"
            if self._unique_label is not None:
                self.label = self._unique_label(self.label)
        if self._on_role is not None:
            self._on_role(self, conn)

    def player_label(self) -> str:
        return ACTOR_YOU if self.primary else (self.label or "")

    def pet_label(self) -> str:
        return ACTOR_PET if self.primary else f"{self.label}'s pet"

    def _name(self, entity_id: int) -> str:
        if entity_id == self.player_id:
            return self.player_label() if self.role_fixed else ACTOR_YOU
        if entity_id == self.pet_id:
            return self.pet_label() if self.role_fixed else ACTOR_PET
        if self._other_label is not None:
            other = self._other_label(self, entity_id)
            if other is not None:
                return other
        return (self.names.get(entity_id) if self.names is not None else None) or entity_name(entity_id)

    def _learn_player(self, record: Record, ids: Iterable[int]) -> None:
        if self._h16 != record.header_low16:  # first record, or the character changed on this connection
            self._h16 = record.header_low16
            self.player_id = None
            self.pet_id = None
            self._swings.clear()
            self._recent.clear()
        if self.player_id is None:
            for i in ids:
                if i & 0xFFFF == record.header_low16:
                    self.player_id = i
                    return

    def _eligible_pet(self, entity_id: int) -> bool:
        if entity_id == self.player_id:
            return False
        return self._claimed_elsewhere is None or not self._claimed_elsewhere(self, entity_id)

    def _learn_pet(self, rec: HpChange) -> None:
        """INFERRED. On each extended hit by my player at T, the candidates are the eligible attackers that swung at T
        within PET_WINDOW before it (never another followed connection's player or learned pet). First learning takes
        the earliest such swing. Replacing a learned pet needs a challenger in both of the last 2 extended hits while
        the current pet appears in none of them (e.g. after a re-summon). The pet id only matters for damage *taken*
        by the pet; pet damage comes from extended records credited to the owner."""
        if not rec.extended or rec.source != self.player_id:
            return
        cands: list[int] = []
        for sw in self._swings:
            if 0 <= rec.ts - sw.ts <= PET_WINDOW and sw.target == rec.target and self._eligible_pet(sw.attacker):
                if sw.attacker not in cands:
                    cands.append(sw.attacker)
        self._recent.append(set(cands))
        if not cands:
            return
        if self.pet_id is None:
            self.pet_id = cands[0]
            return
        if len(self._recent) < 2:
            return
        if any(self.pet_id in r for r in self._recent):
            return
        counts = Counter(c for r in self._recent for c in r)
        best, n = counts.most_common(1)[0]
        if n >= 2 and sum(1 for c, k in counts.items() if k == n) == 1:
            self.pet_id = best

    def feed(self, record: Record) -> list[Event]:
        if isinstance(record, Swing):
            self._learn_player(record, (record.attacker, record.target))
            self._swings.append(record)
            return []
        self._learn_player(record, (record.source, record.target))
        self._learn_pet(record)
        related = (self.player_id, self.pet_id)
        if record.source not in related and record.target not in related:
            self.dropped_unrelated += 1  # other entities' combat must not start or extend your fights
            return []
        self._ensure_role(record.conn, record.ts)
        raw = record.raw.hex(" ")
        if record.amount > 0:
            return [Event(record.ts, self._name(record.source), self._name(record.target), record.amount, None,
                          KIND_HEAL, raw)]
        amount = -record.amount
        player = self.player_id
        if record.source == player:
            actor = self.pet_label() if record.extended else self.player_label()
            return [Event(record.ts, actor, self._name(record.target), amount, None, KIND_DAMAGE, raw)]
        if record.target == player or (self.pet_id is not None and record.target == self.pet_id):
            tgt = self.player_label() if record.target == player else self.pet_label()
            return [Event(record.ts, self._name(record.source), tgt, amount, None, KIND_DAMAGE_TAKEN, raw)]
        # source is the learned pet (not expected: pet damage is credited to the owner), or a non-player source
        # hitting the pet's owner's other entity: dropped like any unrelated record.
        self.dropped_unrelated += 1
        return []


class ConnectionRouter:
    """Routes records to one Attributor per connection.

    ``net.local_port`` set: follow only that connection (it is the primary: ``you``/``pet``); other connections
    keep state but emit nothing. Unset: follow every connection. The primary connection is the first one that
    produces an Event; the others get labels (see module docstring). ``selected`` = the primary connection (or
    ``local_port``); ``own_actors()`` = every actor name emitted so far, ``you``/``pet`` first.
    """

    def __init__(self, config: Config, log: TextIO | None = None, names: NameTable | None = None,
                 own_names: OwnNames | None = None, memory=None) -> None:
        if memory is not None and names is None:
            names = NameTable(config.net.server_ip)
        self.memory = memory  # optional MemoryEnricher (snapshot()); None = PINE off
        self._mem_version = 0
        self._snap = MemorySnapshot()
        self._member_keys: dict[int, str] = {}   # member id -> actor key, fixed at its first Event
        self._member_first: dict[int, float] = {}
        self._fallback_feeder: int | None = None
        self.names = names
        self.own_names = own_names
        self._role_order: list[int] = []
        self.local_port = config.net.local_port
        self._log = log if log is not None else sys.stderr
        self._attrs: dict[int, Attributor] = {}
        self._conn_of: dict[int, int] = {}
        self.selected: int | None = self.local_port
        self._own: dict[str, None] = {}
        self._announced = False
        self._labels: set[str] = set()

    def attributor(self, conn: int) -> Attributor | None:
        return self._attrs.get(conn)

    def connections(self) -> list[int]:
        """Connections that have produced at least one Event, primary first."""
        out = [c for c, a in self._attrs.items() if a.role_fixed and not a.muted]
        return sorted(out, key=lambda c: (c != self.selected,))

    def own_actors(self) -> list[str]:
        names = list(self._own)
        head = [n for n in (ACTOR_YOU, ACTOR_PET) if n in self._own]
        return head + [n for n in names if n not in head]

    def _claim(self, attr: Attributor) -> bool:
        if self.selected is None:
            self.selected = next(c for c, a in self._attrs.items() if a is attr)
        return self._attrs.get(self.selected) is attr

    def _claimed_elsewhere(self, requester: Attributor, entity_id: int) -> bool:
        """True if the id is another followed connection's player or learned pet (never my pet)."""
        return any(a is not requester and entity_id in (a.player_id, a.pet_id) and entity_id is not None
                   for a in self._attrs.values())

    def _unique_label(self, label: str) -> str:
        out, n = label, 1
        while out in self._labels:
            n += 1
            out = f"{label} ({n})"
        self._labels.add(out)
        return out

    def _other_label(self, requester: Attributor, entity_id: int) -> str | None:
        for a in self._attrs.values():
            if a is requester or not a.role_fixed or a.muted:
                continue
            if entity_id == a.player_id:
                return a.player_label()
            if entity_id == a.pet_id:
                return a.pet_label()
        return None

    # -- PINE group members ----------------------------------------------------------------------------------
    def _sync_memory(self) -> None:
        if self.memory is None:
            return
        snap = self.memory.snapshot()
        if snap.version != self._mem_version:
            self._mem_version = snap.version
            self._snap = snap
            if self.names is not None:
                self.names.set_memory_names(snap.names())

    def _followed_players(self) -> set[int]:
        return {a.player_id for a in self._attrs.values() if a.player_id is not None and not a.muted}

    def _feeder(self) -> int | None:
        own = self._snap.own_id
        if own is not None:
            for c, a in self._attrs.items():
                if a.player_id == own and not a.muted:
                    return c
        return self._fallback_feeder

    def _members(self) -> dict[int, str]:
        """Group members that are not a followed connection's player: id -> current name."""
        if not self._snap.members:
            return {}
        followed = self._followed_players()
        return {mid: name for mid, name in self._snap.members if mid not in followed}

    def _member_key(self, mid: int, name: str, ts: float) -> str:
        key = self._member_keys.get(mid)
        if key is None:
            taken = set(self._member_keys.values()) | self._labels | {ACTOR_YOU, ACTOR_PET}
            key, n = name, 1
            while key in taken:
                n += 1
                key = f"{name} ({n})"
            self._member_keys[mid] = key
            self._member_first[mid] = ts
        return key

    def _member_events(self, attr: Attributor, conn: int, record: Record) -> list[Event]:
        if not isinstance(record, HpChange) or not self._snap.members or record.amount > 0:
            return []
        members = self._members()
        src, tgt = members.get(record.source), members.get(record.target)
        if src is None and tgt is None:
            return []
        feeder = self._feeder()
        if feeder is None:
            feeder = self._fallback_feeder = conn
        if conn != feeder:
            return []  # the same groupmate fight reaches every emulator in the group: count it once
        raw = record.raw.hex(" ")
        amount = -record.amount
        if src is not None:
            key = self._member_key(record.source, src, record.ts)
            actor = f"{key}'s pet" if record.extended else key
            target = self._member_key(record.target, tgt, record.ts) if tgt is not None else attr._name(record.target)
            return [Event(record.ts, actor, target, amount, None, KIND_DAMAGE, raw)]
        key = self._member_key(record.target, tgt, record.ts)
        return [Event(record.ts, attr._name(record.source), key, amount, None, KIND_DAMAGE_TAKEN, raw)]

    def character_groups(self) -> list[CharacterGroup]:
        """Followed connections (those that produced an Event) in first-seen order, primary first.
        Display name: the connection's own-name rule, else the NameTable name of the player id, else ``You``
        (primary) or the connection's label. Duplicates get `` (2)``, `` (3)``. Keys never change; names may."""
        order = [c for c in self._role_order if (a := self._attrs.get(c)) is not None and a.role_fixed and not a.muted]
        order.sort(key=lambda c: c != self.selected)  # stable: primary first
        out: list[CharacterGroup] = []
        used: set[str] = set()
        for conn in order:
            a = self._attrs[conn]
            name = self.names.memory_name(a.player_id) if self.names is not None and a.player_id is not None else None
            if name is None and self.own_names is not None:
                name = self.own_names.get(conn)
            if name is None and a.player_id is not None and self.names is not None:
                name = self.names.get(a.player_id)
            if name is None:
                name = "You" if a.primary else (a.label or f"char:{conn}")
            base, n = name, 1
            while name in used:
                n += 1
                name = f"{base} ({n})"
            used.add(name)
            out.append(CharacterGroup(name, a.player_label(), a.pet_label(), conn, a.player_id, a.first_ts))
        for mid, key in self._member_keys.items():  # group members, after your own characters
            out.append(CharacterGroup(key, key, f"{key}'s pet", self._feeder() or 0, mid, self._member_first[mid]))
        return out

    def _on_role(self, attr: Attributor, conn: int) -> None:
        if conn not in self._role_order:
            self._role_order.append(conn)
        if self.local_port is not None:
            return  # explicit --connection: nothing to announce
        pid = "unknown" if attr.player_id is None else entity_name(attr.player_id)
        if attr.primary:
            msg = f"following connection (client port) {conn}, player id {pid}, as you"
        else:
            msg = f"also following connection (client port) {conn}, player id {pid}, as {attr.label}"
        print(f"eqoa net: {msg}", file=self._log)

    def feed(self, record: Record) -> list[Event]:
        conn = record.conn
        attr = self._attrs.get(conn)
        if attr is None:
            attr = self._attrs[conn] = Attributor(self.names, self._claim, self._other_label, self._on_role,
                                                   self._claimed_elsewhere, self._unique_label)
            attr.muted = self.local_port is not None and conn != self.local_port
        self._sync_memory()
        before = attr.dropped_unrelated
        events = attr.feed(record)
        if attr.muted:
            return []
        if not events and attr.dropped_unrelated > before and self._snap.members:
            events = self._member_events(attr, conn, record)
            if events:
                attr.dropped_unrelated = before  # counted as a group member's combat, not as unrelated
        for e in events:
            if e.kind == KIND_DAMAGE:
                self._own.setdefault(e.actor, None)
        return events
