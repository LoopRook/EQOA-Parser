"""Live, read-only capture of the game server's UDP traffic. Core module: no UI imports, stdlib only.

    with RawCapture(config.net) as cap:
        while True:
            dgram = cap.read()          # UdpDatagram, or None after net.poll_interval without one (heartbeat time)
        # or: for dgram in cap: ...     # yields datagrams only; ends when close() is called

VERIFIED LIVE ONCE on Windows 11 as Administrator (2026-10-18, reported by the user via the main chat): the Windows
raw-socket path (bind + SIO_RCVALL), IP/UDP parsing and filtering worked in a real fight. Everything else is still only
tested with hand-built packets and fake sockets: the Linux AF_PACKET path, real IP fragments, multi-adapter/VPN setups,
and whether client->server packets are delivered (the damage-carrying server->client direction is what was exercised).

What it does
  * Windows: a raw IPv4 socket (SOCK_RAW/IPPROTO_IP) bound to the local interface IP with SIO_RCVALL (needs
    Administrator). ASSUMED/UNVERIFIED: that this also delivers the packets the machine SENDS to the server; some
    Windows versions only deliver received packets through SIO_RCVALL (still UNVERIFIED for outbound). If
    client->server datagrams never show up, use the pktmon fallback in docs/live-capture.md. Server->client datagrams
    (the ones the decoder needs) are inbound and are verified.
  * Linux (best effort, UNVERIFIED): AF_PACKET / SOCK_DGRAM, ETH_P_IP; the kernel strips the link header so packets
    start at the IPv4 header. Needs root or CAP_NET_RAW. Both directions are seen. Other platforms: CaptureError.
  * Parses the IPv4 and UDP headers itself and keeps only UDP datagrams whose source or destination is
    net.server_ip. Checksums are NOT verified (checksum offload makes locally generated packets unreliable).
  * IP fragments are REASSEMBLED (per (src, dst, id, proto); at most MAX_PENDING_FRAGMENTED datagrams pending, each
    dropped after FRAGMENT_TIMEOUT seconds). Fragments of other hosts are ignored before they are stored. Truncated or
    malformed packets are dropped and counted in `stats`.
  * Nothing is ever sent: only recv() is used. Interface auto-detection uses a UDP connect(), which sends no packet.
  * close() sets a stop flag; reads use a socket timeout of net.poll_interval, so a blocked read returns promptly.
"""
from __future__ import annotations

import errno
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from ..config import NetConfig

try:  # defined by net/pcapng.py (session B); the fallback has the contract's exact field list
    from .pcapng import UdpDatagram
except ImportError:  # pragma: no cover - only while pcapng.py does not exist yet

    @dataclass(frozen=True)
    class UdpDatagram:  # type: ignore[no-redef]
        ts: float
        src_ip: str
        src_port: int
        dst_ip: str
        dst_port: int
        payload: bytes


PROTO_UDP = 17
FRAGMENT_TIMEOUT = 5.0
MAX_PENDING_FRAGMENTED = 256
RECV_SIZE = 65535
_WSAEACCES = 10013


class CaptureError(RuntimeError):
    """Live capture cannot start or has failed (not elevated, unsupported platform, no route, socket error)."""


def _ip_str(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


@dataclass
class CaptureStats:
    packets: int = 0            # raw IP packets received
    matched: int = 0            # datagrams yielded
    other_host: int = 0         # IPv4 packets not to/from server_ip, or not UDP
    truncated: int = 0          # malformed / truncated packets dropped
    fragments: int = 0          # fragments received from the server host
    reassembled: int = 0        # datagrams completed from fragments
    fragments_expired: int = 0  # incomplete fragment sets dropped


@dataclass
class _Pending:
    first_seen: float
    parts: dict = field(default_factory=dict)  # offset -> bytes
    total: int | None = None                   # IP payload length, known once the last fragment arrived


class IPv4UdpParser:
    """Pure packet -> UdpDatagram logic (no sockets), so it can be tested with hand-built bytes."""

    def __init__(self, server_ip: str, clock: Callable[[], float] = time.time) -> None:
        self.server_ip = server_ip
        self.clock = clock
        self.stats = CaptureStats()
        self._pending: dict[tuple, _Pending] = {}

    def feed(self, packet: bytes, ts: float | None = None) -> UdpDatagram | None:
        """One raw IPv4 packet (starting at the IP header). Returns a datagram to/from server_ip, else None."""
        st = self.stats
        st.packets += 1
        if len(packet) < 20 or packet[0] >> 4 != 4:
            st.truncated += 1
            return None
        ihl = (packet[0] & 0x0F) * 4
        total_len = struct.unpack_from("!H", packet, 2)[0]
        if total_len == 0:  # length not filled in (segmentation offload on locally generated packets)
            total_len = len(packet)
        if ihl < 20 or total_len < ihl or total_len > len(packet):
            st.truncated += 1
            return None
        ident, flags_frag = struct.unpack_from("!HH", packet, 4)
        proto = packet[9]
        src, dst = _ip_str(packet[12:16]), _ip_str(packet[16:20])
        if proto != PROTO_UDP or self.server_ip not in (src, dst):
            st.other_host += 1
            return None
        ts = self.clock() if ts is None else ts
        body = packet[ihl:total_len]
        more = bool(flags_frag & 0x2000)
        offset = (flags_frag & 0x1FFF) * 8
        if more or offset:
            st.fragments += 1
            body = self._add_fragment((src, dst, ident, proto), offset, more, body, ts)
            if body is None:
                return None
            st.reassembled += 1
        return self._udp(src, dst, body, ts)

    def _udp(self, src: str, dst: str, seg: bytes, ts: float) -> UdpDatagram | None:
        if len(seg) < 8:
            self.stats.truncated += 1
            return None
        sport, dport, ulen = struct.unpack_from("!HHH", seg, 0)
        if ulen < 8 or ulen > len(seg):
            self.stats.truncated += 1
            return None
        self.stats.matched += 1
        return UdpDatagram(ts, src, sport, dst, dport, bytes(seg[8:ulen]))

    def _add_fragment(self, key: tuple, offset: int, more: bool, data: bytes, ts: float) -> bytes | None:
        self._expire(ts)
        pend = self._pending.get(key)
        if pend is None:
            if len(self._pending) >= MAX_PENDING_FRAGMENTED:
                oldest = min(self._pending, key=lambda k: self._pending[k].first_seen)
                del self._pending[oldest]
                self.stats.fragments_expired += 1
            pend = self._pending[key] = _Pending(first_seen=ts)
        pend.parts[offset] = data
        if not more:
            pend.total = offset + len(data)
        if pend.total is None:
            return None
        out = bytearray()
        pos = 0
        for off in sorted(pend.parts):
            if off > pos:
                return None  # hole: wait for more
            chunk = pend.parts[off]
            out[off:off + len(chunk)] = chunk
            pos = max(pos, off + len(chunk))
        if pos < pend.total:
            return None
        del self._pending[key]
        return bytes(out[: pend.total])

    def _expire(self, now: float) -> None:
        for key in [k for k, p in self._pending.items() if now - p.first_seen > FRAGMENT_TIMEOUT]:
            del self._pending[key]
            self.stats.fragments_expired += 1


def detect_interface_ip(server_ip: str, port: int = 10070) -> str:
    """Local IPv4 of the interface that routes to server_ip. A UDP connect() only sets the default peer and the
    kernel picks the source address; NO packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((server_ip, port))
            return s.getsockname()[0]
    except OSError as exc:
        raise CaptureError(
            f"cannot determine which network interface routes to {server_ip}: {exc}. "
            "Set net.interface_ip in config.yaml to this PC's IPv4 address on the connection PCSX2 uses."
        ) from exc


def _is_permission_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        codes = {exc.errno, getattr(exc, "winerror", None), exc.args[0] if exc.args else None}
        return bool(codes & {errno.EACCES, errno.EPERM, _WSAEACCES})
    return False


def _open_socket(platform: str, interface_ip: str, timeout: float) -> socket.socket:
    """Real raw socket (the only code that touches the OS). UNVERIFIED."""
    if platform == "win32":
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
        try:
            sock.bind((interface_ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
            sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)  # type: ignore[attr-defined]
        except BaseException:
            sock.close()
            raise
    elif platform.startswith("linux"):
        if not hasattr(socket, "AF_PACKET"):
            raise CaptureError("this Python has no AF_PACKET support")
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.ntohs(0x0800))  # ETH_P_IP
    else:
        raise CaptureError(f"live capture is only implemented for Windows and Linux, not {platform!r}; use pktmon/tcpdump")
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    sock.settimeout(timeout)
    return sock


class RawCapture:
    """Context manager / iterator over UDP datagrams to and from net.server_ip. See the module docstring."""

    def __init__(
        self,
        net: NetConfig,
        *,
        platform: str | None = None,
        socket_factory: Callable[[str, str, float], "socket.socket"] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.net = net
        self.platform = sys.platform if platform is None else platform
        self._factory = socket_factory or _open_socket
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self.parser = IPv4UdpParser(net.server_ip, clock)
        self._clock = clock
        self.interface_ip: str | None = None

    @property
    def stats(self) -> CaptureStats:
        return self.parser.stats

    def open(self) -> "RawCapture":
        if self._sock is not None:
            return self
        self._stop.clear()
        iface = self.net.interface_ip or detect_interface_ip(self.net.server_ip)
        self.interface_ip = iface
        try:
            self._sock = self._factory(self.platform, iface, self.net.poll_interval)
        except CaptureError:
            raise
        except OSError as exc:
            if _is_permission_error(exc):
                if self.platform == "win32":
                    raise CaptureError(
                        "live capture needs Administrator rights: close this window and start the parser from a "
                        "terminal opened with 'Run as administrator' (see docs/live-capture.md)."
                    ) from exc
                raise CaptureError(
                    "live capture needs root or the CAP_NET_RAW capability (e.g. run with sudo, or "
                    "'setcap cap_net_raw+ep' on the Python binary); see docs/live-capture.md."
                ) from exc
            raise CaptureError(f"cannot open the capture socket on {iface}: {exc}") from exc
        return self

    def read(self) -> UdpDatagram | None:
        """Block for at most net.poll_interval. Returns the next datagram, or None on timeout, on a packet that is
        not a datagram we want, or once the capture is closed."""
        sock = self._sock
        if sock is None or self._stop.is_set():
            return None
        try:
            packet = sock.recv(RECV_SIZE)
        except socket.timeout:
            return None
        except OSError as exc:
            if self._stop.is_set() or self._sock is None:
                return None  # close() raced with the blocked read
            raise CaptureError(f"capture socket failed: {exc}") from exc
        return self.parser.feed(packet, self._clock())

    def __iter__(self) -> Iterator[UdpDatagram]:
        while not self._stop.is_set() and self._sock is not None:
            dgram = self.read()
            if dgram is not None:
                yield dgram

    def close(self) -> None:
        """Safe to call more than once and from another thread; a blocked read() returns within poll_interval."""
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is None:
            return
        if self.platform == "win32" and hasattr(sock, "ioctl"):
            try:
                sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                pass
        try:
            sock.close()
        except OSError:
            pass

    def __enter__(self) -> "RawCapture":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
