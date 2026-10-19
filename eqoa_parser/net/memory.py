"""Optional game-memory enrichment over PINE: own name/ids, entity list, group. Core: no UI imports. READ-ONLY.

Memory addresses and structure offsets come from the MIT-licensed project FrontiersForge by mmvest
(https://github.com/mmvest/FrontiersForge, see docs/research/frontiersforge.md). They were verified by the main chat on
SLUS-20744 v1.02 / PCSX2 2.8.2 (own name, own id, group of 2). Entity classes, pet id and 8-member groups are not
all verified. Every value is sanity-checked and junk is skipped, never guessed.

Nothing here writes to the emulator: PineClient has no write calls and none are added.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Callable

from ..config import PineConfig
from ..pine import PineClient

RAM_LIMIT = 0x2000000           # EE RAM is 32 MB; a pointer at or above this is junk

OWN_NAME_ADDR = 0x1FBBA10       # C string
CELL_ADDR = 0x4E37F0            # u32 pointer-like base ("cell"), used directly as the base
OWN_ID_OFF = 0x71F4             # u32 at cell + off
PET_ID_OFF = 0x2BE4C            # u32
ENTITY_LIST_ADDR = 0x1FB6C30    # 24 u32 pointers
ENTITY_SLOTS = 24
ENT_ID, ENT_NAME, ENT_LEVEL, ENT_CLASS = 0x0C, 0x58, 0x70, 0x256
ENT_NAME_LEN = 32
ENT_READ = 0x260                # bytes read per entity record (covers the class byte)
IN_GROUP_OFF = 0x2BD70          # u32
GROUP_COUNT_OFF = 0x2BE48       # u32, includes self
GROUP_MEMBERS_OFF = 0x2BD78     # 0x34 bytes each
GROUP_MEMBER_SIZE = 0x34
GROUP_NAME_OFF, GROUP_NAME_LEN = 0x1C, 24
MAX_GROUP = 8

_KINDS = {0: "npc", 1: "npc", 2: "player", 3: "pet"}


def _cstring(raw: bytes) -> str | None:
    """NUL-terminated printable-ASCII string, or None when empty or junk."""
    text = raw.split(b"\x00", 1)[0]
    if not text or any(not 0x20 <= b < 0x7F for b in text):
        return None
    return text.decode("ascii").strip() or None


class GameMemory:
    """Typed, read-only views of EQOA game memory. Methods raise PineError when PINE fails; junk values give
    None / empty results."""

    def __init__(self, client: PineClient) -> None:
        self.client = client

    def _cell(self) -> int | None:
        cell = self.client.read32(CELL_ADDR)
        return cell if 0 < cell < RAM_LIMIT - 0x30000 else None

    def own_name(self) -> str | None:
        return _cstring(self.client.read_bytes(OWN_NAME_ADDR, 32))

    def own_id(self) -> int | None:
        cell = self._cell()
        if cell is None:
            return None
        return self.client.read32(cell + OWN_ID_OFF) or None

    def pet_id(self) -> int | None:
        cell = self._cell()
        if cell is None:
            return None
        return self.client.read32(cell + PET_ID_OFF) or None

    def entities(self) -> list[tuple[int, str, int, str]]:
        """[(id, name, level, kind)] for the occupied slots; kind is npc / player / pet / other."""
        raw = self.client.read_bytes(ENTITY_LIST_ADDR, 4 * ENTITY_SLOTS)
        out: list[tuple[int, str, int, str]] = []
        for i in range(ENTITY_SLOTS):
            ptr = int.from_bytes(raw[4 * i:4 * i + 4], "little")
            if not 0 < ptr < RAM_LIMIT - ENT_READ:
                continue
            rec = self.client.read_bytes(ptr, ENT_READ)
            level = rec[ENT_LEVEL]
            eid = int.from_bytes(rec[ENT_ID:ENT_ID + 4], "little")
            name = _cstring(rec[ENT_NAME:ENT_NAME + ENT_NAME_LEN])
            if level == 0 or eid == 0 or name is None:
                continue
            out.append((eid, name, level, _KINDS.get(rec[ENT_CLASS], "other")))
        return out

    def group(self) -> tuple[bool, list[tuple[int, str]]]:
        """(in_group, [(id, name)]); members include yourself. Junk or an implausible count gives (False, [])."""
        cell = self._cell()
        if cell is None:
            return False, []
        in_group = self.client.read32(cell + IN_GROUP_OFF)
        count = self.client.read32(cell + GROUP_COUNT_OFF)
        if in_group != 1 or not 0 < count <= MAX_GROUP:
            return False, []
        raw = self.client.read_bytes(cell + GROUP_MEMBERS_OFF, count * GROUP_MEMBER_SIZE)
        members: list[tuple[int, str]] = []
        for i in range(count):
            rec = raw[i * GROUP_MEMBER_SIZE:(i + 1) * GROUP_MEMBER_SIZE]
            mid = int.from_bytes(rec[:4], "little")
            name = _cstring(rec[GROUP_NAME_OFF:GROUP_NAME_OFF + GROUP_NAME_LEN])
            if mid and name:
                members.append((mid, name))
        return bool(members), members


@dataclass(frozen=True)
class MemorySnapshot:
    version: int = 0                # increments whenever the content changes
    ok: bool = False                # last poll succeeded
    error: str | None = None
    own_name: str | None = None
    own_id: int | None = None
    pet_id: int | None = None
    entities: tuple = ()
    in_group: bool = False
    members: tuple = ()             # ((id, name), ...) including yourself

    def content(self) -> tuple:
        return (self.ok, self.own_name, self.own_id, self.pet_id, self.entities, self.in_group, self.members)

    def names(self) -> dict[int, str]:
        """id -> name from entities, group members and the own character."""
        out = {eid: name for eid, name, _lvl, _kind in self.entities}
        out.update({mid: name for mid, name in self.members})
        if self.own_id is not None and self.own_name:
            out[self.own_id] = self.own_name
        return out


class MemoryEnricher:
    """Background poller (~1 s) holding the latest MemorySnapshot. Never raises: if PINE is unreachable or fails the
    snapshot says ``ok=False`` and it retries every ``retry`` seconds (one warning to ``on_status``-style callers via
    the snapshot, not by printing)."""

    def __init__(self, pine: PineConfig | None = None, *, factory: Callable[[], GameMemory] | None = None,
                 interval: float = 1.0, retry: float = 5.0) -> None:
        self.pine = pine if pine is not None else PineConfig()
        self._factory = factory or (lambda: GameMemory(PineClient(self.pine)))
        self.interval = interval
        self.retry = retry
        self._lock = threading.Lock()
        self._snap = MemorySnapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mem: GameMemory | None = None

    # -- public -----------------------------------------------------------------------------------------------
    def snapshot(self) -> MemorySnapshot:
        with self._lock:
            return self._snap

    def start(self) -> "MemoryEnricher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="eqoa-memory", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._drop()

    def status_text(self) -> str:
        s = self.snapshot()
        if not s.ok:
            return "PINE: not reachable"
        text = f"PINE: on (slot {self.pine.slot})"
        return f"{text}, group of {len(s.members)}" if s.in_group else text

    # -- internals --------------------------------------------------------------------------------------------
    def _drop(self) -> None:
        mem, self._mem = self._mem, None
        client = getattr(mem, "client", None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def poll_once(self) -> bool:
        """One read pass; returns True on success. Used by the thread and by tests."""
        try:
            if self._mem is None:
                self._mem = self._factory()
            mem = self._mem
            own_id = mem.own_id()
            snap = dict(own_name=mem.own_name(), own_id=own_id, pet_id=mem.pet_id(),
                        entities=tuple(mem.entities()), in_group=False, members=())
            in_group, members = mem.group()
            snap.update(in_group=in_group, members=tuple(members))
            self._publish(ok=True, error=None, **snap)
            return True
        except Exception as exc:  # PineError, OSError, anything: the meter must keep running
            self._drop()
            with self._lock:  # keep the last known group and names: they are still the best information
                old = self._snap
            self._publish(**{**old.__dict__, "ok": False, "error": f"{type(exc).__name__}: {exc}"}, _keep=True)
            return False

    def _publish(self, _keep: bool = False, **fields) -> None:
        fields.pop("version", None)
        with self._lock:
            old = self._snap
            new = MemorySnapshot(version=old.version, **fields)
            if new.content() != old.content() or new.error != old.error:
                new = replace(new, version=old.version + 1)
            self._snap = new

    def _run(self) -> None:
        while not self._stop.is_set():
            ok = self.poll_once()
            self._stop.wait(self.interval if ok else self.retry)
