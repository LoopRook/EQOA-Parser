"""Stdlib pcapng reader yielding UDP datagrams. Core: no UI imports.

Handles SHB / IDB / EPB blocks in either byte order, link types Ethernet (1), raw IP (101) and IPv4 (228);
everything else (other blocks, IPv6, non-UDP, fragments) is skipped. pktmon logs each packet once per network
component, so a datagram equal (src, dst, ports, payload) to one kept less than 50 ms earlier is dropped.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DUP_WINDOW = 0.050  # seconds

_LINK_ETHERNET = 1
_LINK_RAW = (101, 228)
_BLOCK_SHB = 0x0A0D0D0A
_BLOCK_IDB = 0x00000001
_BLOCK_EPB = 0x00000006
_BLOCK_SPB = 0x00000003


@dataclass(frozen=True)
class UdpDatagram:
    ts: float          # seconds (capture clock; epoch for pcapng files)
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    payload: bytes


def parse_ip_udp(data: bytes, ts: float) -> UdpDatagram | None:
    """Parse an IPv4 packet carrying UDP. Returns None for anything else (also fragments)."""
    if len(data) < 20 or data[0] >> 4 != 4:
        return None
    ihl = (data[0] & 0x0F) * 4
    if ihl < 20 or len(data) < ihl + 8 or data[9] != 17:
        return None
    flags_frag = struct.unpack_from(">H", data, 6)[0]
    if flags_frag & 0x3FFF:  # more-fragments flag or non-zero fragment offset
        return None
    total = struct.unpack_from(">H", data, 2)[0]
    if total == 0:  # length not filled in (segmentation offload); same rule as net/capture.py
        total = len(data)
    if total < ihl + 8 or total > len(data):  # header lies, or the frame was cut: drop, like net/capture.py does
        return None
    sport, dport, ulen = struct.unpack_from(">HHH", data, ihl)
    if ulen < 8 or ihl + ulen > total:
        return None
    return UdpDatagram(
        ts, socket.inet_ntoa(data[12:16]), sport, socket.inet_ntoa(data[16:20]), dport, bytes(data[ihl + 8:ihl + ulen])
    )


def _decode_frame(link: int, data: bytes, ts: float) -> UdpDatagram | None:
    if link == _LINK_ETHERNET:
        if len(data) < 14:
            return None
        off = 12
        ethertype = struct.unpack_from(">H", data, off)[0]
        while ethertype in (0x8100, 0x88A8) and len(data) >= off + 6:  # VLAN tags
            off += 4
            ethertype = struct.unpack_from(">H", data, off)[0]
        if ethertype != 0x0800:
            return None
        return parse_ip_udp(data[off + 2:], ts)
    if link in _LINK_RAW:
        return parse_ip_udp(data, ts)
    return None


def _tsresol(options: bytes, endian: str) -> float:
    """Seconds per timestamp tick from IDB options (option 9 if_tsresol); default 1e-6."""
    i = 0
    while i + 4 <= len(options):
        code, length = struct.unpack_from(endian + "HH", options, i)
        if code == 0:
            break
        if code == 9 and length >= 1 and i + 4 < len(options):
            v = options[i + 4]
            return 2.0 ** -(v & 0x7F) if v & 0x80 else 10.0 ** -v
        i += 4 + ((length + 3) & ~3)
    return 1e-6


def check_pcapng_header(path: str | Path) -> None:
    """Raise ValueError if a non-empty file does not start with a pcapng section header block.
    An empty or very short truncated file passes (it simply holds no datagrams)."""
    with Path(path).open("rb") as fh:
        head = fh.read(12)
    sig = bytes([0x0A, 0x0D, 0x0D, 0x0A])
    if head[:4] != sig[:len(head[:4])]:
        raise ValueError(f"{path}: not a pcapng file (no section header block)")
    if len(head) == 12 and head[8:12] not in (bytes([0x4D, 0x3C, 0x2B, 0x1A]), bytes([0x1A, 0x2B, 0x3C, 0x4D])):
        raise ValueError(f"{path}: not a pcapng file (bad byte-order magic)")


def iter_raw_datagrams(path: str | Path) -> Iterator[UdpDatagram]:
    """All UDP datagrams in capture order, including pktmon duplicates."""
    data = Path(path).read_bytes()
    pos, n = 0, len(data)
    endian = "<"
    ifaces: list[tuple[int, float]] = []  # (linktype, seconds per tick)
    while pos + 12 <= n:
        btype_le = struct.unpack_from("<I", data, pos)[0]
        if btype_le == _BLOCK_SHB:
            magic = data[pos + 8:pos + 12]
            if magic == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif magic == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise ValueError("not a pcapng file: bad byte-order magic")
            ifaces = []
        elif not ifaces and pos == 0:
            raise ValueError("not a pcapng file: missing section header block")
        btype, blen = struct.unpack_from(endian + "II", data, pos)
        if blen < 12 or blen % 4 or pos + blen > n:
            break  # truncated or corrupt tail
        body = data[pos + 8:pos + blen - 4]
        if btype == _BLOCK_IDB and len(body) >= 8:
            link = struct.unpack_from(endian + "H", body, 0)[0]
            ifaces.append((link, _tsresol(body[8:], endian)))
        elif btype == _BLOCK_EPB and len(body) >= 20:
            iface, thi, tlo, caplen, _orig = struct.unpack_from(endian + "IIIII", body, 0)
            if caplen < _orig:
                pass  # snaplen-truncated frame: its payload would lose the tail/checksum, so it is not trusted
            elif iface < len(ifaces):
                link, res = ifaces[iface]
                ts = ((thi << 32) | tlo) * res
                dg = _decode_frame(link, body[20:20 + caplen], ts)
                if dg is not None:
                    yield dg
        pos += blen


def read_pcapng(path: str | Path) -> Iterator[UdpDatagram]:
    """UDP datagrams in capture order with pktmon duplicates (same bytes < 50 ms apart) dropped."""
    last: dict[tuple, float] = {}
    count = 0
    for dg in iter_raw_datagrams(path):
        key = (dg.src_ip, dg.src_port, dg.dst_ip, dg.dst_port, dg.payload)
        prev = last.get(key)
        if prev is not None and dg.ts - prev < DUP_WINDOW:
            continue
        last[key] = dg.ts
        count += 1
        if count % 4096 == 0:  # keep the table small on long captures
            cutoff = dg.ts - DUP_WINDOW
            last = {k: t for k, t in last.items() if t >= cutoff}
        yield dg
