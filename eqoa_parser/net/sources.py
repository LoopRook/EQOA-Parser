"""LogSources that yield Events built from game-server UDP packets. Core: stdlib only, no UI imports.

Items are ``(ts, Event)`` (``Engine.step`` feeds an Event straight to the segmenter) or ``(ts, HEARTBEAT)``.
Protocol and attribution facts are INFERRED from 2 captures; see ``protocol.py`` / ``attribution.py``.
"""
from __future__ import annotations

import dataclasses
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Iterator

from ..config import Config
from ..events import Event
from ..sources import HEARTBEAT, pace
from .attribution import CharacterGroup, ConnectionRouter
from .names import NameTable, OwnNames
from .pcapng import UdpDatagram, check_pcapng_header, read_pcapng
from .protocol import PacketDecoder


class PacketSource:
    """Base: datagrams -> records -> Events. Subclasses provide the datagram iterator."""

    def __init__(self, config: Config, memory=None) -> None:
        self.config = config
        self.memory = memory  # optional MemoryEnricher (PINE); None = off
        self.pine_note = "PINE: off"  # status text while memory is None
        self.decoder = PacketDecoder(config.net.server_ip)
        self.names = NameTable(config.net.server_ip)
        self.own_names = OwnNames(config.net.server_ip)
        self.router = ConnectionRouter(config, names=self.names, own_names=self.own_names, memory=memory)
        self._closed = threading.Event()
        self._recorder = None       # object with write(UdpDatagram) -> bool; see net/pcapng_writer.py
        self._recorder_lock = threading.Lock()
        self.datagrams = 0          # datagrams offered to the decoder
        self.server_datagrams = 0   # ... that came from net.server_ip
        self.records = 0            # protocol records decoded (after dedupe)
        self._first_ts: float | None = None   # server datagram times: the run's elapsed time
        self._last_ts = 0.0

    @property
    def has_game_traffic(self) -> bool:
        """False if no datagram from net.server_ip was seen (so far / after iteration): the app can warn."""
        return self.server_datagrams > 0

    def pine_status(self) -> str:
        """Status line text: ``PINE: off`` / ``PINE: on (slot N), group of K`` / ``PINE: not reachable``."""
        if self.memory is None:
            return self.pine_note
        return self.memory.status_text()

    def own_actors(self) -> list[str]:
        """Actor names emitted for followed connections so far, ``you``/``pet`` first (first-seen order after)."""
        return self.router.own_actors()

    def character_groups(self) -> list[CharacterGroup]:
        """Followed characters, primary first; display names may change as names arrive (keys never do)."""
        return self.router.character_groups()

    def progress(self):
        """(kills / XP / tunar per character, seconds of server traffic seen): the run summary's progress part."""
        el = 0.0 if self._first_ts is None else max(0.0, self._last_ts - self._first_ts)
        return self.router.progress_rows(), el

    def attach_recorder(self, recorder) -> None:
        """Pass every datagram to ``recorder.write`` from now on (any thread, any time). The recorder itself decides
        what it keeps (PcapngWriter keeps only server packets)."""
        with self._recorder_lock:
            self._recorder = recorder

    def detach_recorder(self) -> None:
        with self._recorder_lock:
            self._recorder = None

    def events_from(self, dgram: UdpDatagram) -> list[Event]:
        out: list[Event] = []
        with self._recorder_lock:
            rec = self._recorder
        if rec is not None:
            try:
                rec.write(dgram)
            except Exception:  # recording must never take the meter down; stop it instead
                self.detach_recorder()
        self.own_names.feed(dgram)
        self.names.feed(dgram)  # names first, so a name in this datagram resolves its own records
        self.datagrams += 1
        if dgram.src_ip == self.config.net.server_ip:
            self.server_datagrams += 1
            if self._first_ts is None:
                self._first_ts = dgram.ts
            self._last_ts = max(self._last_ts, dgram.ts)
        for rec in self.decoder.feed(dgram):
            self.records += 1
            out.extend(self.router.feed(rec))
        return out

    def _events(self, datagrams: Iterable[UdpDatagram]) -> Iterator[tuple[float, Event]]:
        for dg in datagrams:
            if self._closed.is_set():
                return
            for ev in self.events_from(dg):
                yield ev.ts, ev

    def close(self) -> None:
        self._closed.set()
        if self.memory is not None:
            self.memory.close()


class PcapReplaySource(PacketSource):
    """Replays a pcapng capture (pktmon duplicates dropped). Yields ``(ts, Event)`` in capture order;
    Event ts = datagram ts. speed 0 = as fast as possible; speed > 0 sleeps scaled gaps and yields heartbeats
    like ReplaySource."""

    def __init__(self, path: str | Path, config: Config, speed: float = 0.0, heartbeat_interval: float = 0.25,
                 memory=None) -> None:
        super().__init__(config, memory)
        if speed < 0:
            raise ValueError("speed must be >= 0")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be > 0")
        self.path = Path(path)
        self.speed = float(speed)
        self.heartbeat_interval = float(heartbeat_interval)
        self.clamped = 0  # datagrams whose regressing ts was clamped to the previous one
        check_pcapng_header(self.path)  # ValueError if not a pcapng (an empty file passes: no datagrams)

    def _ordered(self) -> Iterator[UdpDatagram]:
        prev: float | None = None
        for dg in read_pcapng(self.path):
            if prev is not None and dg.ts < prev:
                self.clamped += 1
                if self.clamped == 1:
                    print(f"PcapReplaySource: {self.path}: timestamp {dg.ts} regresses below {prev}; clamped "
                          "(further clamps are counted, not printed)", file=sys.stderr)
                dg = dataclasses.replace(dg, ts=prev)
            prev = dg.ts
            yield dg

    def __iter__(self) -> Iterator[tuple[float, Event | str]]:
        return pace(self._events(self._ordered()), self.speed, self.heartbeat_interval, self._closed)


class LivePacketSource(PacketSource):
    """Live capture via ``net.capture.RawCapture`` (session A). UNVERIFIED against a real raw socket.

    ``RawCapture.read()`` returns None after ``net.poll_interval`` without a datagram; that becomes a
    ``(time.time(), HEARTBEAT)`` item so idle fights close. A ``CaptureError`` (e.g. not Administrator) is raised on
    the first ``next()``. ``close()`` stops iteration promptly and closes the capture."""

    def __init__(self, config: Config, capture_factory=None, memory=None) -> None:
        super().__init__(config, memory)
        self._factory = capture_factory
        self._capture = None

    def __iter__(self) -> Iterator[tuple[float, Event | str]]:
        factory = self._factory
        if factory is None:
            from .capture import RawCapture as factory  # lazy: only live use needs it
        cap = factory(self.config.net)
        self._capture = cap
        try:
            with cap:
                last = time.time()
                while not self._closed.is_set():
                    dg = cap.read()
                    emitted = False
                    if dg is not None:
                        for ev in self.events_from(dg):
                            emitted = True
                            yield ev.ts, ev
                    # idle clock: a datagram without an Event (or a read timeout), or poll_interval of wall time
                    # without a heartbeat, advances the segmenter so finished fights close while the server talks.
                    now = time.time()
                    if not emitted or now - last >= self.config.net.poll_interval:
                        last = now
                        yield now, HEARTBEAT
        finally:
            self._capture = None

    def close(self) -> None:
        super().close()
        cap = self._capture
        if cap is not None:
            cap.close()


class ClientSocketSource:
    """Yields Events from the EQOA Native Rust client's local UDP telemetry stream (127.0.0.1:28015).

    Requires NO Administrator permissions, NO packet sniffing, and NO PINE.
    """

    def __init__(self, port: int = 28015, poll_interval: float = 0.5) -> None:
        import socket
        self.port = port
        self.poll_interval = poll_interval
        self._closed = threading.Event()
        self.pine_note = "Native Client: active"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(("127.0.0.1", self.port))
        sock.settimeout(self.poll_interval)
        self._sock = sock

    @property
    def has_game_traffic(self) -> bool:
        return True

    def pine_status(self) -> str:
        return self.pine_note

    def own_actors(self) -> list[str]:
        return ["you", "pet"]

    def character_groups(self) -> list:
        return []

    def __iter__(self) -> Iterator[tuple[float, Event | str]]:
        import json
        sock = self._sock
        if sock is None:
            return
        last_heartbeat = time.time()
        try:
            while not self._closed.is_set():
                try:
                    data, _ = sock.recvfrom(4096)
                except TimeoutError:
                    now = time.time()
                    yield now, HEARTBEAT
                    continue
                except OSError:
                    break

                now = time.time()
                try:
                    obj = json.loads(data.decode("utf-8"))
                    ev = Event(
                        ts=float(obj.get("ts", now)),
                        actor=str(obj.get("actor", "you")),
                        target=obj.get("target"),
                        amount=int(obj.get("amount", 0)),
                        ability=obj.get("ability", "melee"),
                        kind=str(obj.get("kind", "damage")),
                        raw=data.decode("utf-8", errors="replace"),
                    )
                    yield ev.ts, ev
                except Exception:
                    pass

                if now - last_heartbeat >= self.poll_interval:
                    last_heartbeat = now
                    yield now, HEARTBEAT
        finally:
            self.close()

    def close(self) -> None:
        self._closed.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
