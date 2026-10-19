"""Start/stop recording of a live packet source to a pcapng file (shared by the GUI buttons and ``--record``)."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .net.pcapng_writer import PcapngWriter


def default_capture_path(captures_dir: str | Path, now: datetime | None = None) -> Path:
    return Path(captures_dir) / f"EQOA-{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}.pcapng"


class RecordingController:
    """``source`` needs attach_recorder / detach_recorder (PacketSource). Only server packets are written."""

    def __init__(self, source: Any, captures_dir: str | Path, server_ip: str) -> None:
        self.source = source
        self.captures_dir = Path(captures_dir)
        self.server_ip = server_ip
        self._writer: PcapngWriter | None = None
        self._lock = threading.Lock()
        self.last_path: Path | None = None

    @property
    def active(self) -> bool:
        w = self._writer
        return w is not None and not w.closed

    @property
    def path(self) -> Path | None:
        return self._writer.path if self._writer is not None else self.last_path

    @property
    def packets(self) -> int:
        return self._writer.packets if self._writer is not None else 0

    @property
    def error(self) -> str | None:
        return self._writer.error if self._writer is not None else None

    def start(self, path: str | Path | None = None) -> Path:
        with self._lock:
            if self.active:
                return self._writer.path
            target = Path(path) if path else default_capture_path(self.captures_dir)
            self._writer = PcapngWriter(target, self.server_ip)
            self.last_path = target
            self.source.attach_recorder(self._writer)
            return target

    def stop(self) -> None:
        with self._lock:
            w = self._writer
            if w is None:
                return
            self.source.detach_recorder()
            w.close()

    def mark(self, text: str = "mark") -> bool:
        w = self._writer
        if w is None or w.closed:
            return False
        w.mark(text)
        return True

    def status_text(self) -> str | None:
        """``Recording: <file> (<n> packets)`` while active, a stop reason if the writer failed, else None."""
        w = self._writer
        if w is None:
            return None
        if w.error:
            return f"Recording stopped: {w.error}"
        if w.closed:
            return None
        return f"Recording: {w.path.name} ({w.packets} packets)"
