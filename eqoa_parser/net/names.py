"""Entity-id -> name table from server spawn/update data. Core: stdlib only, no UI imports.

INFERRED (best effort; one heuristic checked against 24 pairs from a single capture, fixtures/real/expected_names.json):
in a server payload, a byte ``0x80|len`` (len 3..32) followed by ``0x04`` then ``len`` bytes matching
``[A-Za-z][A-Za-z' -]+`` is a name; the entity id is the u32 LE immediately before the nearest preceding ``82 31``
within 120 bytes. Trailing spaces are stripped; a later name for the same id replaces the earlier one.
The object stream is partly compressed, so many names are missed (fragments are possible). Never guess: unknown
ids return None and callers keep the hex name. No attempt is made to decompress the stream.
"""
from __future__ import annotations

import re
import struct

from .pcapng import UdpDatagram
from .protocol import read_varint

_NAME = re.compile(rb"[A-Za-z][A-Za-z' -]+")
_MARKER = b"\x82\x31"
_BACK = 120


class NameTable:
    def __init__(self, server_ip: str | None = None) -> None:
        self.server_ip = server_ip
        self._names: dict[int, str] = {}

    def get(self, entity_id: int) -> str | None:
        return self._names.get(entity_id)

    def as_dict(self) -> dict[int, str]:
        return dict(self._names)

    def feed(self, dgram: UdpDatagram) -> None:
        """Scan one server datagram (other sources are ignored when server_ip is set)."""
        if self.server_ip is not None and dgram.src_ip != self.server_ip:
            return
        p = dgram.payload
        n = len(p)
        for i in range(n - 4):
            b = p[i]
            ln = b & 0x7F
            if not (b & 0x80) or ln < 3 or ln > 32 or p[i + 1] != 0x04 or i + 2 + ln > n:
                continue
            raw = p[i + 2:i + 2 + ln]
            if _NAME.fullmatch(raw) is None:
                continue
            j = p.rfind(_MARKER, max(0, i - _BACK), i)
            if j < 4:
                continue
            name = raw.decode("ascii").rstrip(" ")
            if len(name) >= 3:
                self._names[struct.unpack_from("<I", p, j - 4)[0]] = name


_ESF = b".esf"
_ALPHA = re.compile(rb"[A-Za-z]+")


class OwnNames:
    """A connection's own character name (INFERRED from 1 sample, fixtures/real cap2 port 20605 at t+114 s).

    On login or zone-in the server sends on the character's own connection a message with the zone file followed by
    the name: ``...data\tunaria.esf <varint> <u32 LE n> <n ASCII letters>``. Rule: after ``.esf`` skip one varint,
    read u32 n (3..20), then n bytes matching ``[A-Za-z]+``. Only that connection's own server traffic counts
    (connection = the datagram's destination port). A later name on the same connection replaces the earlier one.
    A character that was already logged in when the capture started never gets a name.
    """

    def __init__(self, server_ip: str | None = None) -> None:
        self.server_ip = server_ip
        self._names: dict[int, str] = {}

    def get(self, conn: int) -> str | None:
        return self._names.get(conn)

    def as_dict(self) -> dict[int, str]:
        return dict(self._names)

    def feed(self, dgram: UdpDatagram) -> None:
        if self.server_ip is not None and dgram.src_ip != self.server_ip:
            return
        p = dgram.payload
        i = p.find(_ESF)
        while i >= 0:
            v = read_varint(p, i + len(_ESF), len(p))
            if v is not None:
                pos = v[1]
                if pos + 4 <= len(p):
                    n = struct.unpack_from("<I", p, pos)[0]
                    raw = p[pos + 4:pos + 4 + n]
                    if 3 <= n <= 20 and len(raw) == n and _ALPHA.fullmatch(raw) is not None:
                        self._names[dgram.dst_port] = raw.decode("ascii")
            i = p.find(_ESF, i + 1)
