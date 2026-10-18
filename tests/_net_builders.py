"""Hand-built packets for the net tests (session B). Not a test module."""
from __future__ import annotations

import socket
import struct

SERVER = "5.161.217.188"
CLIENT = "192.168.1.156"


def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63) if n >= 0 else ((-n) << 1) - 1


def varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def hp_body(source: int, amount: int, target: int, extra: bytes = b"") -> bytes:
    return b"\xdb\x00" + struct.pack("<I", source) + varint(zigzag(amount)) + struct.pack("<I", target) + extra


def swing_body(attacker: int, target: int, extra: int = 768000, tail: bytes = b"\x00") -> bytes:
    return b"\xb1\x00" + struct.pack("<II", attacker, target) + struct.pack("<i", extra) + tail


def msg(seq: int, body: bytes) -> bytes:
    return bytes([0xFB, len(body)]) + struct.pack("<H", seq) + body


def payload(h16: int, *messages: bytes, prefix: bytes = b"\x01\x02\x03") -> bytes:
    return struct.pack("<H", h16) + prefix + b"".join(messages) + b"\xde\xad\xbe\xef"


def ip_udp(src: str, sport: int, dst: str, dport: int, data: bytes, *, mf: bool = False) -> bytes:
    udp = struct.pack(">HHHH", sport, dport, 8 + len(data), 0) + data
    flags = 0x2000 if mf else 0
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, flags, 64, 17, 0, socket.inet_aton(src),
                      socket.inet_aton(dst))
    return hdr + udp


def eth(ip: bytes, ethertype: int = 0x0800, vlan: bool = False) -> bytes:
    if vlan:
        return b"\x00" * 12 + b"\x81\x00\x00\x05" + struct.pack(">H", ethertype) + ip
    return b"\x00" * 12 + struct.pack(">H", ethertype) + ip


def _block(btype: int, body: bytes, e: str) -> bytes:
    body += b"\x00" * (-len(body) % 4)
    total = 12 + len(body)
    return struct.pack(e + "II", btype, total) + body + struct.pack(e + "I", total)


def pcapng(frames: list[tuple[float, bytes]], *, link: int = 1, endian: str = "<", tsresol: int | None = None) -> bytes:
    """frames: (ts_seconds, frame_bytes) on one interface of the given link type."""
    e = endian
    shb = _block(0x0A0D0D0A, struct.pack(e + "IHHq", 0x1A2B3C4D, 1, 0, -1), e)
    opts = b""
    res = 1e-6
    if tsresol is not None:
        opts = struct.pack(e + "HH", 9, 1) + bytes([tsresol]) + b"\x00\x00\x00" + struct.pack(e + "HH", 0, 0)
        res = 2.0 ** -(tsresol & 0x7F) if tsresol & 0x80 else 10.0 ** -tsresol
    idb = _block(1, struct.pack(e + "HHI", link, 0, 0) + opts, e)
    out = shb + idb
    for ts, frame in frames:
        ticks = round(ts / res)
        body = struct.pack(e + "IIIII", 0, ticks >> 32, ticks & 0xFFFFFFFF, len(frame), len(frame)) + frame
        out += _block(6, body, e)
    return out
