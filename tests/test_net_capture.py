"""net/capture.py with hand-built IPv4/UDP bytes and fake sockets. No real raw socket is ever opened (UNVERIFIED live)."""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from eqoa_parser.config import NetConfig
from eqoa_parser.net import capture
from eqoa_parser.net.capture import CaptureError, IPv4UdpParser, RawCapture

SERVER = "5.161.217.188"
LOCAL = "192.168.1.50"


def ip_bytes(a: str) -> bytes:
    return bytes(int(x) for x in a.split("."))


def ipv4(src, dst, payload, *, proto=17, ident=1, flags=0, frag_off=0, ihl_words=5, total_len=None, options=b""):
    hdr_len = ihl_words * 4
    assert hdr_len == 20 + len(options)
    tl = hdr_len + len(payload) if total_len is None else total_len
    hdr = struct.pack("!BBHHHBBH4s4s", (4 << 4) | ihl_words, 0, tl, ident, (flags << 13) | (frag_off // 8), 64, proto, 0,
                      ip_bytes(src), ip_bytes(dst))
    return hdr + options + payload


def udp(sport, dport, data, length=None):
    return struct.pack("!HHHH", sport, dport, 8 + len(data) if length is None else length, 0) + data


def test_server_to_client_datagram():
    p = IPv4UdpParser(SERVER, lambda: 5.0)
    d = p.feed(ipv4(SERVER, LOCAL, udp(10070, 50000, b"hello")))
    assert (d.ts, d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload) == (5.0, SERVER, 10070, LOCAL, 50000, b"hello")


def test_client_to_server_datagram():
    d = IPv4UdpParser(SERVER).feed(ipv4(LOCAL, SERVER, udp(50000, 10071, b"\x01\x02")), 9.5)
    assert (d.ts, d.src_ip, d.dst_ip, d.dst_port, d.payload) == (9.5, LOCAL, SERVER, 10071, b"\x01\x02")


def test_other_hosts_and_protocols_filtered():
    p = IPv4UdpParser(SERVER)
    assert p.feed(ipv4(LOCAL, "8.8.8.8", udp(1, 53, b"q"))) is None
    assert p.feed(ipv4(SERVER, LOCAL, b"\x00" * 20, proto=6)) is None  # TCP from the server host
    assert p.stats.other_host == 2 and p.stats.matched == 0


def test_ip_options_and_trailing_padding_ignored():
    pkt = ipv4(SERVER, LOCAL, udp(10070, 5, b"abc"), ihl_words=6, options=b"\x01\x01\x01\x00")
    d = IPv4UdpParser(SERVER).feed(pkt + b"\x00\x00\x00\x00\x00\x00")  # e.g. Ethernet padding after total length
    assert d.payload == b"abc"


def test_udp_length_shorter_than_ip_payload_trims_payload():
    d = IPv4UdpParser(SERVER).feed(ipv4(SERVER, LOCAL, udp(10070, 5, b"abcdef", length=8 + 3)))
    assert d.payload == b"abc"


def test_empty_payload_ok():
    assert IPv4UdpParser(SERVER).feed(ipv4(SERVER, LOCAL, udp(1, 2, b""))).payload == b""


@pytest.mark.parametrize("make", [
    lambda: b"",                                                          # nothing
    lambda: b"\x45\x00\x00",                                              # shorter than an IP header
    lambda: ipv4(SERVER, LOCAL, udp(1, 2, b"abcdef"))[:-3],               # cut inside the payload (total_len too big)
    lambda: ipv4(SERVER, LOCAL, b"\x27\x56\x00"),                         # UDP header cut short
    lambda: ipv4(SERVER, LOCAL, udp(1, 2, b"abc", length=200)),           # UDP length larger than the data
    lambda: ipv4(SERVER, LOCAL, udp(1, 2, b"abc", length=4)),             # UDP length below 8
    lambda: b"\x65" + ipv4(SERVER, LOCAL, udp(1, 2, b"x"))[1:],           # IP version 6 nibble
    lambda: ipv4(SERVER, LOCAL, udp(1, 2, b"x"), total_len=10),           # total length below the header length
])
def test_truncated_or_malformed_dropped_without_exception(make):
    p = IPv4UdpParser(SERVER)
    assert p.feed(make()) is None
    assert p.stats.truncated == 1 and p.stats.matched == 0


def test_total_length_zero_uses_buffer_length():
    d = IPv4UdpParser(SERVER).feed(ipv4(LOCAL, SERVER, udp(1, 2, b"xy"), total_len=0))
    assert d is not None and d.payload == b"xy"


def test_fragments_are_reassembled_in_any_order():
    full = udp(10070, 50000, bytes(range(200)))                 # 208 bytes of IP payload
    cuts = [(0, full[:96], 1), (96, full[96:192], 1), (192, full[192:], 0)]
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        p = IPv4UdpParser(SERVER)
        out = [p.feed(ipv4(SERVER, LOCAL, cuts[i][1], ident=7, flags=cuts[i][2] << 0 and 1, frag_off=cuts[i][0])) for i in order]
        assert [o for o in out[:-1]] == [None, None]
        assert out[-1].payload == bytes(range(200)) and out[-1].src_port == 10070
        assert p.stats.reassembled == 1 and p.stats.fragments == 3


def test_fragment_sets_with_different_ids_do_not_mix_and_incomplete_expire():
    now = [0.0]
    p = IPv4UdpParser(SERVER, lambda: now[0])
    full = udp(1, 2, bytes(100))
    first = ipv4(SERVER, LOCAL, full[:64], ident=1, flags=1)
    assert p.feed(first) is None
    assert p.feed(ipv4(SERVER, LOCAL, full[64:], ident=2, flags=0, frag_off=64)) is None  # other id: never completes id 1
    now[0] = 100.0
    assert p.feed(ipv4(SERVER, LOCAL, full[64:], ident=1, flags=0, frag_off=64)) is None  # first part expired
    assert p.stats.fragments_expired >= 1


def test_fragments_of_other_hosts_are_not_stored():
    p = IPv4UdpParser(SERVER)
    assert p.feed(ipv4(LOCAL, "9.9.9.9", bytes(64), flags=1)) is None
    assert p._pending == {}


def test_pending_fragment_sets_are_bounded():
    p = IPv4UdpParser(SERVER, lambda: 0.0)
    for i in range(capture.MAX_PENDING_FRAGMENTED + 50):
        p.feed(ipv4(SERVER, LOCAL, bytes(16), ident=i, flags=1))
    assert len(p._pending) <= capture.MAX_PENDING_FRAGMENTED


# --- RawCapture with fake sockets ---------------------------------------------------------------------------------
class FakeSock:
    def __init__(self, packets=(), block=False):
        self.packets, self.block, self.closed, self.ioctls = list(packets), block, False, []
        self.timeout = 0.05

    def recv(self, n):
        if self.closed:
            raise OSError(9, "bad file descriptor")
        if self.packets:
            return self.packets.pop(0)
        time.sleep(self.timeout)
        if self.closed:
            raise OSError(9, "bad file descriptor")
        raise socket.timeout()

    def ioctl(self, *a):
        self.ioctls.append(a)

    def close(self):
        self.closed = True


def make_cap(sock, platform="win32", **net):
    cfg = NetConfig(server_ip=SERVER, interface_ip=LOCAL, poll_interval=0.05, **net)
    return RawCapture(cfg, platform=platform, socket_factory=lambda plat, iface, to: sock)


def test_capture_yields_only_wanted_datagrams_and_heartbeat_none():
    good = ipv4(SERVER, LOCAL, udp(10070, 50000, b"A"))
    bad = ipv4(LOCAL, "8.8.8.8", udp(1, 53, b"q"))
    sock = FakeSock([bad, good, ipv4(LOCAL, SERVER, udp(50000, 10070, b"B"))])
    with make_cap(sock) as cap:
        assert cap.read() is None                       # packet for another host
        assert cap.read().payload == b"A"
        assert cap.read().payload == b"B"
        assert cap.read() is None                       # timeout -> None so the caller can emit a heartbeat
        assert cap.stats.matched == 2
    assert sock.closed and sock.ioctls[-1][0] == socket.SIO_RCVALL if hasattr(socket, "SIO_RCVALL") else sock.closed


def test_iteration_ends_on_close():
    sock = FakeSock([ipv4(SERVER, LOCAL, udp(1, 2, bytes([i]))) for i in range(3)])
    cap = make_cap(sock).open()
    got = []

    def run():
        for d in cap:
            got.append(d.payload)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)
    cap.close()
    t.join(2)
    assert not t.is_alive() and got == [b"\x00", b"\x01", b"\x02"]


def test_close_unblocks_a_blocked_read_promptly():
    sock = FakeSock()
    sock.timeout = 0.05
    cap = make_cap(sock).open()
    result = []
    t = threading.Thread(target=lambda: result.append(cap.read()))
    t.start()
    time.sleep(0.01)
    t0 = time.monotonic()
    cap.close()
    t.join(2)
    assert not t.is_alive() and time.monotonic() - t0 < 1.0 and result == [None]
    cap.close()                                          # idempotent
    assert cap.read() is None


class RaisingFactory:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, *a):
        raise self.exc


@pytest.mark.parametrize("exc", [PermissionError(13, "denied"), OSError(10013, "An attempt was made to access a socket..."),
                                 OSError(1, "Operation not permitted")])
def test_not_admin_error_windows(exc):
    cap = RawCapture(NetConfig(interface_ip=LOCAL), platform="win32", socket_factory=RaisingFactory(exc))
    with pytest.raises(CaptureError, match="Administrator"):
        cap.open()


def test_not_root_error_linux():
    cap = RawCapture(NetConfig(interface_ip=LOCAL), platform="linux", socket_factory=RaisingFactory(PermissionError(1, "x")))
    with pytest.raises(CaptureError, match="CAP_NET_RAW"):
        cap.open()


def test_other_open_errors_are_captureerrors():
    cap = RawCapture(NetConfig(interface_ip=LOCAL), platform="win32",
                     socket_factory=RaisingFactory(OSError(10049, "address not valid in this context")))
    with pytest.raises(CaptureError, match="cannot open"):
        cap.open()


def test_real_factory_maps_permission_error(monkeypatch):
    """Mock the OSError at the socket() call itself, as an unelevated Windows process would see it."""
    def deny(*a, **k):
        raise OSError(10013, "access denied")

    monkeypatch.setattr(capture.socket, "socket", deny)
    with pytest.raises(CaptureError, match="Administrator"):
        RawCapture(NetConfig(interface_ip=LOCAL), platform="win32").open()


def test_unsupported_platform_message():
    with pytest.raises(CaptureError, match="only implemented for Windows and Linux"):
        RawCapture(NetConfig(interface_ip=LOCAL), platform="darwin").open()


def test_socket_error_during_read_becomes_captureerror():
    class Boom(FakeSock):
        def recv(self, n):
            raise OSError(10054, "reset")

    with make_cap(Boom()) as cap:
        with pytest.raises(CaptureError, match="capture socket failed"):
            cap.read()


def test_interface_autodetect_sends_nothing(monkeypatch):
    calls = []

    class S:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def connect(self, addr):
            calls.append(("connect", addr))

        def getsockname(self):
            return ("10.1.2.3", 55555)

        def __getattr__(self, name):  # any send*/write would show up here
            calls.append(name)
            raise AssertionError(name)

    monkeypatch.setattr(capture.socket, "socket", S)
    assert capture.detect_interface_ip(SERVER) == "10.1.2.3"
    assert calls == [("connect", (SERVER, 10070))]


def test_interface_autodetect_failure_message(monkeypatch):
    def fail(*a, **k):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(capture.socket, "socket", fail)
    with pytest.raises(CaptureError, match="net.interface_ip"):
        capture.detect_interface_ip(SERVER)


def test_capture_module_has_no_send_calls():
    import inspect
    src = inspect.getsource(capture)
    for forbidden in (".send(", ".sendto(", ".sendall(", ".write("):
        assert forbidden not in src
