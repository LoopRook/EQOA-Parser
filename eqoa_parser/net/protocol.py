"""EQOA server->client datagram framing and combat records. Core: stdlib only, no UI imports.

Byte-level facts are from 2 real captures (docs/protocol-notes.md). Labels:
  CONFIRMED -- checked against what the user saw in game.   INFERRED -- fits the data, not yet tested.

Datagram layout (server -> client):
  - payload[0:2]  = low 16 bits of the logged-in character's entity ID, u16 LE (CONFIRMED on 3 logins).
  - last 4 bytes  = checksum (INFERRED); never part of a message.
  - messages      = ``0xFB <len u8> <seq u16 LE> <body: len bytes>``; body starts with a u16 LE opcode. The outer
    header layout is not decoded, so we scan for 0xFB and accept a message only if it fits before the checksum
    and its body is valid for a known opcode. Unknown opcodes are skipped.
Opcodes:
  0x00DB HpChange  ``db 00 <source u32 LE> <amount zigzag varint> <target u32 LE> [extra]``
      negative = damage, positive = heal (CONFIRMED). "extended" = len > base length (INFERRED: a pet action
      credited to its owner; all 36 pet records in cap2 were extended, none of 69 player records were).
  0x00B1 Swing     ``b1 00 <attacker u32> <target u32> <extra i32> <tail>`` (INFERRED: NPC/pet melee swing
      start; the player's own melee has no swing record).
  0x0A7A SystemText ``7a 0a <n u32> <n UTF-16LE units>`` (CONFIRMED; net/systext.py)
  0x00CD CastBar   own character's cast bar (INFERRED); recognised but not decoded into a record.
Server re-sends are dropped per connection (client port) on (seq, body).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Union

from .pcapng import UdpDatagram
from .systext import OP_SYSTEXT, decode_text

OP_HPCHANGE = 0x00DB
OP_SWING = 0x00B1
OP_CASTBAR = 0x00CD
CHECKSUM_LEN = 4
DEDUPE_WINDOW = 120.0  # seconds of datagram time a (seq, body) is remembered (seq is only 16 bits)


@dataclass(frozen=True)
class HpChange:
    ts: float
    conn: int              # client UDP port: identifies the emulator
    header_low16: int      # low 16 bits of the logged-in character's entity id
    seq: int
    source: int
    amount: int            # signed: negative = damage, positive = heal
    target: int
    extended: bool         # INFERRED: pet action credited to its owner
    raw: bytes             # the message body


@dataclass(frozen=True)
class Swing:
    ts: float
    conn: int
    header_low16: int
    seq: int
    attacker: int
    target: int
    extra: int             # INFERRED meaning (768000 for pet swings, 1536000 for the dummy in cap2)
    raw: bytes


@dataclass(frozen=True)
class SystemText:
    """Opcode 0x0A7A: a system message for the connection's character (net/systext.py)."""
    ts: float
    conn: int
    header_low16: int
    seq: int
    text: str
    raw: bytes


Record = Union[HpChange, Swing, SystemText]


def read_varint(buf: bytes, pos: int, end: int) -> tuple[int, int] | None:
    """Unsigned LEB128 varint (max 5 bytes). Returns (value, next_pos) or None if malformed/out of range."""
    value = shift = 0
    for i in range(5):
        if pos + i >= end:
            return None
        b = buf[pos + i]
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos + i + 1
        shift += 7
    return None


def zigzag_decode(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def _decode_body(body: bytes, ts: float, conn: int, h16: int, seq: int) -> Record | None:
    op = body[0] | body[1] << 8
    if op == OP_HPCHANGE:
        if len(body) < 11:
            return None
        source = struct.unpack_from("<I", body, 2)[0]
        v = read_varint(body, 6, len(body))
        if v is None or v[1] + 4 > len(body):
            return None
        raw_amount, p = v
        target = struct.unpack_from("<I", body, p)[0]
        return HpChange(ts, conn, h16, seq, source, zigzag_decode(raw_amount), target, len(body) > p + 4, body)
    if op == OP_SWING:
        if len(body) < 14:
            return None
        attacker, target, extra = struct.unpack_from("<IIi", body, 2)
        return Swing(ts, conn, h16, seq, attacker, target, extra, body)
    if op == OP_SYSTEXT:
        text = decode_text(body)
        return None if text is None else SystemText(ts, conn, h16, seq, text, body)
    return None


def decode_datagram(dgram: UdpDatagram) -> list[Record]:
    """Records in one server->client datagram, in wire order. Stateless: no dedupe (see PacketDecoder)."""
    p = dgram.payload
    n = len(p)
    if n < 2 + CHECKSUM_LEN:
        return []
    h16 = p[0] | p[1] << 8
    limit = n - CHECKSUM_LEN
    conn = dgram.dst_port
    out: list[Record] = []
    i = 2
    while i + 6 <= limit:
        if p[i] != 0xFB:
            i += 1
            continue
        ln = p[i + 1]
        end = i + 4 + ln
        if ln < 2 or end > limit:
            i += 1
            continue
        seq = p[i + 2] | p[i + 3] << 8
        rec = _decode_body(p[i + 4:end], dgram.ts, conn, h16, seq)
        if rec is None:
            i += 1
            continue
        out.append(rec)
        i = end
    return out


class PacketDecoder:
    """Stateful: filters to server->client datagrams and drops re-sent messages per connection."""

    def __init__(self, server_ip: str) -> None:
        self.server_ip = server_ip
        self._seen: dict[int, dict[tuple[int, bytes], float]] = {}
        self._pruned = 0.0

    def feed(self, dgram: UdpDatagram) -> list[Record]:
        if dgram.src_ip != self.server_ip:
            return []
        seen = self._seen.setdefault(dgram.dst_port, {})
        out: list[Record] = []
        for rec in decode_datagram(dgram):
            key = (rec.seq, rec.raw)
            last = seen.get(key)
            if last is not None and dgram.ts - last < DEDUPE_WINDOW:
                continue
            seen[key] = dgram.ts
            out.append(rec)
        if dgram.ts - self._pruned > DEDUPE_WINDOW:
            self._pruned = dgram.ts
            for conn, table in self._seen.items():
                self._seen[conn] = {k: t for k, t in table.items() if dgram.ts - t < DEDUPE_WINDOW}
        return out
