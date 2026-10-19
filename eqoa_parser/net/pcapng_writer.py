"""Minimal pcapng writer for recording the game server's packets. Core: stdlib only, no UI imports.

Only datagrams from ``server_ip`` (server to client) are ever written: raw captures also hold the player's login,
which travels client to server. The file has no OS/host/interface strings (SHB and IDB carry no options).
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

from .pcapng import UdpDatagram

_FLUSH_EVERY = 0.5  # seconds
_LINK_RAW_IPV4 = 101


def _block(btype: int, body: bytes) -> bytes:
    pad = (-len(body)) % 4
    total = 12 + len(body) + pad
    return struct.pack("<II", btype, total) + body + b"\0" * pad + struct.pack("<I", total)


def _checksum(header: bytes) -> int:
    s = sum(struct.unpack(">%dH" % (len(header) // 2), header))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def build_ip_udp(dg: UdpDatagram) -> bytes:
    udp_len = 8 + len(dg.payload)
    total = 20 + udp_len
    head = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, 17, 0,
                       socket.inet_aton(dg.src_ip), socket.inet_aton(dg.dst_ip))
    head = head[:10] + struct.pack(">H", _checksum(head)) + head[12:]
    return head + struct.pack(">HHHH", dg.src_port, dg.dst_port, udp_len, 0) + dg.payload


class PcapngWriter:
    def __init__(self, path: str | Path, server_ip: str) -> None:
        self.path = Path(path)
        self.server_ip = server_ip
        self.packets = 0
        self.error: str | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("wb")
        shb = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
        idb = struct.pack("<HHI", _LINK_RAW_IPV4, 0, 0)
        self._fh.write(_block(0x0A0D0D0A, shb) + _block(0x00000001, idb))
        self._fh.flush()
        self._last_flush = time.monotonic()

    @property
    def marks_path(self) -> Path:
        return self.path.with_name(self.path.name + ".marks.txt")

    @property
    def closed(self) -> bool:
        return self._closed

    def write(self, dg: UdpDatagram) -> bool:
        """Store the datagram if it came from the server; returns whether it was written."""
        if dg.src_ip != self.server_ip:
            return False
        with self._lock:
            if self._closed:
                return False
            try:
                frame = build_ip_udp(dg)
                us = int(round(dg.ts * 1e6))
                body = struct.pack("<IIIII", 0, us >> 32, us & 0xFFFFFFFF, len(frame), len(frame)) + frame
                self._fh.write(_block(0x00000006, body))
                self.packets += 1
                self._maybe_flush()
            except (OSError, struct.error, ValueError) as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self._close_locked()
                return False
        return True

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush >= _FLUSH_EVERY:
            self._fh.flush()
            self._last_flush = now

    def mark(self, text: str = "mark", ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        clean = " ".join(str(text).split()) or "mark"
        line = f"{datetime.fromtimestamp(ts).isoformat(timespec='seconds')}\t{ts:.6f}\t{clean}\n"
        with self._lock:
            with self.marks_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            if not self._closed:
                try:
                    self._fh.flush()
                except OSError:
                    pass

    def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._fh.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._close_locked()
