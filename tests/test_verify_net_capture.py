"""Independent verification, Phase 2 (session D): net/capture.py with hand-built IPv4 packets and fake sockets only.

NO real raw socket is ever opened (the real-socket code path, _open_socket for win32/linux, is never called).
Tests starting "BUG-N2-x" assert required behaviour and are expected to fail against the current code.
"""
from __future__ import annotations

import errno
import inspect
import socket
import struct
import threading
import time

import pytest

from eqoa_parser.config import NetConfig
from eqoa_parser.net import capture
from eqoa_parser.net.capture import (
    FRAGMENT_TIMEOUT,
    MAX_PENDING_FRAGMENTED,
    CaptureError,
    IPv4UdpParser,
    RawCapture,
)

SERVER = "5.161.217.188"
CLIENT = "192.168.1.156"
OTHER = "8.8.8.8"


def ipv4(src, dst, l4: bytes, *, proto=17, ident=1, flags_frag=0, options=b"", total=None, ihl_override=None, version=4):
    ihl = 5 + len(options) // 4 if ihl_override is None else ihl_override
    tl = 4 * (5 + len(options) // 4) + len(l4) if total is None else total
    hdr = struct.pack(">BBHHHBBH4s4s", (version << 4) | ihl, 0, tl, ident, flags_frag, 64, proto, 0,
                      socket.inet_aton(src), socket.inet_aton(dst)) + options
    return hdr + l4


def udp(sport, dport, data: bytes, ulen=None):
    return struct.pack(">HHHH", sport, dport, 8 + len(data) if ulen is None else ulen, 0) + data


def fragments(src, dst, l4: bytes, ident: int, size: int = 64):
    """Split an IP payload into fragments of `size` bytes (multiple of 8)."""
    out = []
    for off in range(0, len(l4), size):
        chunk = l4[off:off + size]
        more = off + size < len(l4)
        out.append(ipv4(src, dst, chunk, ident=ident, flags_frag=(0x2000 if more else 0) | (off // 8)))
    return out


def parser(clock=lambda: 100.0):
    return IPv4UdpParser(SERVER, clock)


PAY = bytes(range(200))


# ============================================================================== IPv4UdpParser
def test_server_to_client_and_client_to_server_are_both_kept_others_counted():
    p = parser()
    a = p.feed(ipv4(SERVER, CLIENT, udp(10070, 20000, PAY)))
    b = p.feed(ipv4(CLIENT, SERVER, udp(20000, 10070, b"hello")))
    c = p.feed(ipv4(OTHER, CLIENT, udp(53, 20000, b"x" * 20)))
    d = p.feed(ipv4(SERVER, CLIENT, b"\x00" * 20, proto=6))
    assert (a.src_ip, a.src_port, a.dst_ip, a.dst_port, a.payload, a.ts) == (SERVER, 10070, CLIENT, 20000, PAY, 100.0)
    assert (b.src_ip, b.payload) == (CLIENT, b"hello") and c is None and d is None
    assert (p.stats.matched, p.stats.other_host, p.stats.packets) == (2, 2, 4)


def test_explicit_timestamp_wins_over_clock():
    p = parser(clock=lambda: 1.0)
    assert p.feed(ipv4(SERVER, CLIENT, udp(1, 2, b"abc")), ts=555.5).ts == 555.5
    assert p.feed(ipv4(SERVER, CLIENT, udp(1, 2, b"abc"))).ts == 1.0


def test_ip_options_and_trailing_garbage_and_empty_payload():
    p = parser()
    d = p.feed(ipv4(SERVER, CLIENT, udp(1, 2, PAY), options=b"\x01\x01\x01\x00"))
    assert d.payload == PAY
    pkt = ipv4(SERVER, CLIENT, udp(1, 2, PAY)) + b"\xee" * 30          # bytes beyond total length (link padding)
    assert p.feed(pkt).payload == PAY
    assert p.feed(ipv4(SERVER, CLIENT, udp(1, 2, b""))).payload == b""
    pkt = ipv4(SERVER, CLIENT, udp(1, 2, PAY, ulen=8 + 50))             # UDP length shorter than IP payload
    assert p.feed(pkt).payload == PAY[:50]


def test_total_length_zero_is_treated_as_whole_buffer():
    p = parser()
    assert p.feed(ipv4(SERVER, CLIENT, udp(1, 2, PAY), total=0)).payload == PAY


@pytest.mark.parametrize("case", ["short", "v6", "v5", "ihl4", "total_lt_ihl", "total_gt_len", "udp_short", "udp_len_lt8",
                                  "udp_len_gt_seg", "empty"])
def test_malformed_packets_are_dropped_and_counted(case):
    p = parser()
    good = ipv4(SERVER, CLIENT, udp(1, 2, PAY))
    pkt = {
        "short": good[:19], "v6": ipv4(SERVER, CLIENT, udp(1, 2, PAY), version=6),
        "v5": ipv4(SERVER, CLIENT, udp(1, 2, PAY), version=5),
        "ihl4": ipv4(SERVER, CLIENT, udp(1, 2, PAY), ihl_override=4),
        "total_lt_ihl": ipv4(SERVER, CLIENT, udp(1, 2, PAY), total=10),
        "total_gt_len": ipv4(SERVER, CLIENT, udp(1, 2, PAY), total=5000),
        "udp_short": ipv4(SERVER, CLIENT, b"\x00\x01\x00"), "udp_len_lt8": ipv4(SERVER, CLIENT, udp(1, 2, PAY, ulen=4)),
        "udp_len_gt_seg": ipv4(SERVER, CLIENT, udp(1, 2, PAY, ulen=9000)), "empty": b"",
    }[case]
    assert p.feed(pkt) is None
    assert p.stats.matched == 0 and p.stats.truncated + p.stats.other_host >= 1


def test_df_flag_is_not_a_fragment():
    p = parser()
    assert p.feed(ipv4(SERVER, CLIENT, udp(1, 2, PAY), flags_frag=0x4000)).payload == PAY
    assert p.stats.fragments == 0


@pytest.mark.parametrize("order", [[0, 1, 2, 3], [3, 2, 1, 0], [1, 3, 0, 2], [0, 0, 2, 1, 3, 3]])
def test_fragment_reassembly_any_order_and_duplicates(order):
    p = parser()
    big = bytes(i % 251 for i in range(230))
    frags = fragments(SERVER, CLIENT, udp(10070, 20000, big), ident=7, size=64)      # 4 fragments
    assert len(frags) == 4
    res = [p.feed(frags[i]) for i in order]
    done = [r for r in res if r is not None]
    assert len(done) == 1 and done[0].payload == big and p.stats.reassembled == 1 and len(p._pending) <= 1  # a late duplicate of the last fragment leaves a harmless stale entry that expires


def test_interleaved_fragment_sets_do_not_mix():
    p = parser()
    a = fragments(SERVER, CLIENT, udp(1, 2, b"A" * 150), ident=1)
    b = fragments(SERVER, CLIENT, udp(1, 2, b"B" * 150), ident=2)
    got = []
    for x, y in zip(a, b):
        got += [p.feed(x), p.feed(y)]
    assert sorted(d.payload[:1] for d in got if d) == [b"A", b"B"]


def test_hole_never_completes_and_expires_after_timeout():
    t = [100.0]
    p = IPv4UdpParser(SERVER, lambda: t[0])
    frags = fragments(SERVER, CLIENT, udp(1, 2, b"x" * 200), ident=9)
    assert p.feed(frags[0], t[0]) is None and p.feed(frags[-1], t[0]) is None and len(p._pending) == 1
    t[0] += FRAGMENT_TIMEOUT + 1
    assert p.feed(frags[1], t[0]) is None                       # too late: the old set expired, this starts a new one
    assert p.stats.fragments_expired >= 1


def test_fragments_of_other_hosts_are_not_stored():
    p = parser()
    for f in fragments(OTHER, CLIENT, udp(1, 2, b"x" * 300), ident=3):
        assert p.feed(f) is None
    assert not p._pending and p.stats.fragments == 0


def test_pending_fragment_table_is_bounded():
    p = parser()
    for ident in range(MAX_PENDING_FRAGMENTED + 50):
        p.feed(fragments(SERVER, CLIENT, udp(1, 2, b"x" * 200), ident=ident)[0])
    assert len(p._pending) <= MAX_PENDING_FRAGMENTED and p.stats.fragments_expired >= 50


def test_conflicting_fragment_overlap_does_not_crash():
    p = parser()
    frags = fragments(SERVER, CLIENT, udp(1, 2, b"y" * 200), ident=4)
    evil = ipv4(SERVER, CLIENT, b"Z" * 100, ident=4, flags_frag=(16 // 8))             # overlaps the first fragment
    for f in (frags[0], evil, *frags[1:]):
        p.feed(f)


def test_fragment_with_huge_offset_and_no_total_is_harmless():
    p = parser()
    assert p.feed(ipv4(SERVER, CLIENT, b"x" * 8, ident=5, flags_frag=0x1FFF)) is None
    assert p.feed(ipv4(SERVER, CLIENT, b"x" * 8, ident=5, flags_frag=0x2000 | 0x1FFE)) is None


def test_checksums_are_not_verified_by_design():
    pkt = bytearray(ipv4(SERVER, CLIENT, udp(1, 2, PAY)))
    pkt[10:12] = b"\xde\xad"
    assert parser().feed(bytes(pkt)) is not None


# ============================================================================== RawCapture with a fake socket
class FakeSock:
    def __init__(self, script):
        self.script = list(script)
        self.closed = 0
        self.ioctls = []
        self.timeout = None
        self.gate = threading.Event()

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if self.script:
            it = self.script.pop(0)
            if isinstance(it, BaseException):
                raise it
            return it
        self.gate.wait(self.timeout or 0.05)
        if self.closed:
            raise OSError(errno.EBADF, "closed")
        raise socket.timeout()

    def ioctl(self, *a):
        self.ioctls.append(a)

    def close(self):
        self.closed += 1
        self.gate.set()


def cap_with(script, platform="linux", net=None, factory_calls=None):
    sock = FakeSock(script)

    def factory(plat, iface, timeout):
        if factory_calls is not None:
            factory_calls.append((plat, iface, timeout))
        sock.settimeout(timeout)
        return sock

    net = net or NetConfig(interface_ip="10.0.0.5", poll_interval=0.05)
    return RawCapture(net, platform=platform, socket_factory=factory, clock=lambda: 42.0), sock


GOOD = ipv4(SERVER, CLIENT, udp(10070, 20000, PAY))


def test_open_uses_configured_interface_without_autodetect(monkeypatch):
    monkeypatch.setattr(capture, "detect_interface_ip", lambda *a: pytest.fail("autodetect used"))
    calls = []
    cap, _ = cap_with([], factory_calls=calls)
    with cap:
        assert calls == [("linux", "10.0.0.5", 0.05)] and cap.interface_ip == "10.0.0.5"


def test_open_autodetects_when_interface_not_configured(monkeypatch):
    monkeypatch.setattr(capture, "detect_interface_ip", lambda server_ip, *a: "172.16.1.9")
    calls = []
    cap, _ = cap_with([], net=NetConfig(poll_interval=0.05), factory_calls=calls)
    with cap:
        assert calls[0][1] == "172.16.1.9"


def test_read_returns_datagram_none_on_timeout_none_on_other_traffic():
    cap, sock = cap_with([GOOD, ipv4(OTHER, CLIENT, udp(1, 2, b"x" * 9)), b"\x45"])
    with cap:
        d = cap.read()
        assert d is not None and d.payload == PAY and d.ts == 42.0
        assert cap.read() is None            # unrelated host
        assert cap.read() is None            # malformed
        assert cap.read() is None            # timeout
    assert cap.stats.matched == 1 and cap.stats.other_host == 1 and cap.stats.truncated == 1
    assert sock.closed >= 1


def test_iteration_yields_only_datagrams_and_ends_on_close():
    cap, sock = cap_with([GOOD, b"junk", GOOD])
    got = []
    with cap:
        threading.Timer(0.3, cap.close).start()
        for d in cap:
            got.append(d)
    assert len(got) == 2


def test_close_unblocks_a_blocked_reader_quickly_and_is_idempotent():
    cap, sock = cap_with([], net=NetConfig(interface_ip="10.0.0.5", poll_interval=5.0))
    cap.open()
    out = []
    th = threading.Thread(target=lambda: out.append(cap.read()), daemon=True)
    th.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    cap.close()
    th.join(timeout=2)
    assert not th.is_alive() and time.monotonic() - t0 < 1.0 and out == [None]
    cap.close()
    assert cap.read() is None


def test_error_after_startup_is_a_capture_error_but_not_after_close():
    cap, sock = cap_with([OSError(errno.ENETDOWN, "network down")])
    with cap:
        with pytest.raises(CaptureError, match="capture socket failed"):
            cap.read()
    cap2, sock2 = cap_with([])
    cap2.open()
    cap2.close()
    assert cap2.read() is None


@pytest.mark.parametrize("platform,exc,needle", [
    ("win32", PermissionError(13, "denied"), "Administrator"),
    ("win32", OSError(10013, "forbidden"), "Administrator"),
    ("linux", PermissionError(1, "not permitted"), "root"),
    ("linux", OSError(errno.EPERM, "nope"), "CAP_NET_RAW"),
    ("win32", OSError(10049, "bad address"), "cannot open the capture socket"),
    ("linux", OSError(errno.ENODEV, "no device"), "cannot open the capture socket"),
])
def test_open_failures_map_to_capture_error_with_clear_text(platform, exc, needle):
    def factory(*a):
        raise exc

    cap = RawCapture(NetConfig(interface_ip="10.0.0.5"), platform=platform, socket_factory=factory)
    with pytest.raises(CaptureError) as e:
        cap.open()
    assert needle in str(e.value)
    assert cap._sock is None


def test_capture_error_from_factory_passes_through_unchanged():
    def factory(*a):
        raise CaptureError("custom")

    with pytest.raises(CaptureError, match="custom"):
        RawCapture(NetConfig(interface_ip="1.2.3.4"), socket_factory=factory).open()


@pytest.mark.parametrize("plat", ["darwin", "freebsd13", "cygwin", "aix"])
def test_unsupported_platforms_raise_capture_error_before_touching_any_socket(plat):
    with pytest.raises(CaptureError, match="only implemented for Windows and Linux"):
        capture._open_socket(plat, "10.0.0.5", 0.1)


def test_detect_interface_failure_message(monkeypatch):
    class Boom:
        def __init__(self, *a):
            pass

        def __enter__(self):
            raise OSError("Network is unreachable")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(capture.socket, "socket", Boom)
    with pytest.raises(CaptureError, match="net.interface_ip"):
        capture.detect_interface_ip(SERVER)


def test_windows_close_never_raises_and_closes_socket():
    cap, sock = cap_with([], platform="win32")
    cap.open()
    cap.close()
    assert sock.closed == 1


def test_capture_never_references_a_send_api():
    src = inspect.getsource(capture)
    for needle in (".send(", ".sendto(", ".sendall(", "sendmsg"):
        assert needle not in src, needle


def test_stats_account_for_every_packet():
    cap, _ = cap_with([GOOD, GOOD, b"", b"\x60" + b"\x00" * 40, ipv4(OTHER, CLIENT, udp(1, 2, b"x"))])
    with cap:
        for _ in range(5):
            cap.read()
    s = cap.stats
    assert s.packets == 5 and s.matched == 2 and s.truncated == 2 and s.other_host == 1
