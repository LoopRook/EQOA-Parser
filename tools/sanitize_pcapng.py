"""Rewrite a pcapng keeping only server->client UDP packets, with scrubbed SHB/IDB options (for public fixtures).

usage: python tools/sanitize_pcapng.py IN OUT SERVER_IP   (e.g. 5.161.217.188)
Raw captures contain your own login traffic (account name); never publish them, publish only this output.
- SHB: rewritten with no options (drops OS / hardware / application / comment strings).
- IDB: link type + snaplen kept; options dropped except if_tsresol (code 9), which changes timestamp decoding.
- EPB: kept verbatim only if it is IPv4/UDP from SERVER_IP (Ethernet or raw IPv4); options dropped.
- Every other block type is dropped.
"""
import socket
import struct
import sys

src, dst, server = sys.argv[1], sys.argv[2], sys.argv[3]
data = open(src, "rb").read()
out = bytearray()
pos, endian = 0, "<"
links = []
kept = dropped = 0


def pad4(b):
    return b + b"\x00" * (-len(b) % 4)


def block(btype, body):
    body = pad4(body)
    total = 12 + len(body)
    return struct.pack(endian + "II", btype, total) + body + struct.pack(endian + "I", total)


def from_server(link, frame):
    if link == 1:
        if len(frame) < 14:
            return False
        off, et = 12, struct.unpack_from(">H", frame, 12)[0]
        while et in (0x8100, 0x88A8) and len(frame) >= off + 6:
            off += 4
            et = struct.unpack_from(">H", frame, off)[0]
        if et != 0x0800:
            return False
        ip = frame[off + 2:]
    elif link in (101, 228):
        ip = frame
    else:
        return False
    return len(ip) >= 20 and ip[0] >> 4 == 4 and ip[9] == 17 and socket.inet_ntoa(ip[12:16]) == server


while pos + 12 <= len(data):
    if data[pos:pos + 4] == b"\x0a\x0d\x0d\x0a":
        endian = "<" if data[pos + 8:pos + 12] == b"\x4d\x3c\x2b\x1a" else ">"
        links = []
    btype, blen = struct.unpack_from(endian + "II", data, pos)
    body = data[pos + 8:pos + blen - 4]
    if btype == 0x0A0D0D0A:
        out += block(btype, body[:16])  # byte-order magic, version, section length; no options
    elif btype == 1:
        link, _res, snap = struct.unpack_from(endian + "HHI", body, 0)
        links.append(link)
        opts, i, keep = body[8:], 0, b""
        while i + 4 <= len(opts):
            code, ln = struct.unpack_from(endian + "HH", opts, i)
            if code == 0:
                break
            if code == 9:
                keep += opts[i:i + 4 + ln + (-ln % 4)]
            i += 4 + ln + (-ln % 4)
        if keep:
            keep += struct.pack(endian + "HH", 0, 0)
        out += block(1, body[:8] + keep)
    elif btype == 6:
        iface, th, tl, caplen, origlen = struct.unpack_from(endian + "IIIII", body, 0)
        frame = body[20:20 + caplen]
        if iface < len(links) and from_server(links[iface], frame):
            out += block(6, body[:20] + pad4(frame))
            kept += 1
        else:
            dropped += 1
    pos += blen
open(dst, "wb").write(out)
print(f"{src} -> {dst}: kept {kept} server->client packets, dropped {dropped}; {len(data)} -> {len(out)} bytes")
