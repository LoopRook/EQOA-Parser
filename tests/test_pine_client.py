"""PineClient against the mock server over a real socket. Mock and client share one reading of PINE.cpp (UNVERIFIED vs PCSX2)."""
from __future__ import annotations

import inspect
import os
import socket
import struct
import sys

import pytest

from eqoa_parser import pine
from eqoa_parser.config import PineConfig
from eqoa_parser.mock_pine import MockPineServer
from eqoa_parser.pine import (
    PineClient, PineCommandFailed, PineConnectionError, PineTimeoutError, default_unix_socket_path,
)


@pytest.fixture
def server():
    ram = bytearray(1 << 20)
    ram[0x100:0x108] = bytes.fromhex("0102030405060708")
    with MockPineServer(ram) as s:
        yield s


@pytest.fixture
def client(server):
    with PineClient(PineConfig(slot=server.tcp_port, timeout=3.0), platform="win32") as c:
        yield c


def test_each_width_little_endian(client):
    assert client.read8(0x100) == 0x01
    assert client.read16(0x100) == 0x0201
    assert client.read32(0x100) == 0x04030201
    assert client.read64(0x100) == 0x0807060504030201


def test_read_batch_mixed_widths_in_order(client):
    assert client.read_batch([(1, 0x107), (8, 0x100), (2, 0x102), (4, 0x104)]) == [
        0x08, 0x0807060504030201, 0x0403, 0x08070605]


def test_read_batch_splits_over_max_size(server, client):
    n = pine.MAX_BATCH_OPS * 2 + 7
    out = client.read_batch([(1, 0x100)] * n)
    assert len(out) == n and set(out) == {1}
    assert len(server.request_sizes) == 3 and max(server.request_sizes) <= pine.MAX_IPC_SIZE


def test_read_bytes_unaligned_and_exact(server, client):
    data = bytes(range(256)) * 40
    server.write_ram(0x1003, data)
    for addr, n in [(0x1003, 1), (0x1003, 7), (0x1003, 100), (0x1005, 1000), (0x1000, 0)]:
        assert client.read_bytes(addr, n) == (server.read_ram(addr, n))
    assert client.read_bytes(0x1003, 5000) == data[:5000]


def test_read_bytes_large_uses_multiple_messages(server, client):
    big = os.urandom(600_000)
    server.write_ram(0x0, big)
    assert client.read_bytes(0, len(big)) == big
    assert len(server.request_sizes) >= 2


def test_info_calls(client, server):
    assert client.version() == "PCSX2 v0.0.0-mock"
    assert client.title() == "Mock Game"
    assert client.game_id() == "SLUS-00000"
    assert client.game_uuid() == "deadbeef"
    assert client.game_version() == "1.00"
    assert client.status() == "running"
    server.status = 1
    assert client.status() == "paused"


def test_no_vm_fails_reads_but_status_works(server, client):
    server.has_vm = False
    with pytest.raises(PineCommandFailed):
        client.read8(0)
    assert client.status() == "running"


def test_out_of_range_read_fails_whole_batch(client):
    with pytest.raises(PineCommandFailed):
        client.read_batch([(1, 0), (1, 0xFFFFFFF0)])
    assert client.read8(0) == 0  # connection still usable


def test_bad_arguments(client):
    with pytest.raises(ValueError):
        client.read8(-1)
    with pytest.raises(ValueError):
        client.read8(1 << 32)
    with pytest.raises(ValueError):
        client.read_batch([(3, 0)])


def test_not_running_gives_clear_error():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    c = PineClient(PineConfig(slot=port, timeout=1.0), platform="win32")
    with pytest.raises(PineConnectionError, match="PCSX2"):
        c.connect()


def test_timeout_on_silent_server():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    c = PineClient(PineConfig(slot=srv.getsockname()[1], timeout=0.3), platform="win32")
    try:
        with pytest.raises(PineTimeoutError):
            c.read8(0)
    finally:
        c.close(); srv.close()


def test_server_close_midway_raises_connection_error(server):
    c = PineClient(PineConfig(slot=server.tcp_port, timeout=2.0), platform="win32")
    c.connect()
    server.stop()
    with pytest.raises(PineConnectionError):
        c.read8(0)


def test_wire_format_matches_spec_bytes(server):
    """Raw bytes: request = u32 len(incl. itself) + opcode + u32 addr; reply = u32 len + 0x00 + LE value."""
    server.write_ram(0x10, b"\xAA\xBB")
    with socket.create_connection(("127.0.0.1", server.tcp_port), timeout=2) as s:
        s.sendall(struct.pack("<IBI", 9, 1, 0x10))
        assert s.recv(64) == struct.pack("<IB", 7, 0) + b"\xAA\xBB"


def test_transport_by_platform():
    assert PineClient(platform="win32").transport == "tcp"
    assert PineClient(platform="linux").transport == "unix"
    assert PineClient(PineConfig(slot=1234), platform="win32").endpoint == "tcp://127.0.0.1:1234"


def test_unix_socket_path_rules():
    assert default_unix_socket_path(28011, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "linux") == "/run/user/1000/pcsx2.sock"
    assert default_unix_socket_path(28012, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "linux") == "/run/user/1000/pcsx2.sock.28012"
    assert default_unix_socket_path(28011, {}, "linux") == "/tmp/pcsx2.sock"
    assert default_unix_socket_path(5, {}, "linux") == "/tmp/pcsx2.sock.5"
    assert default_unix_socket_path(28011, {"TMPDIR": "/var/T"}, "darwin") == "/var/T/pcsx2.sock"
    assert default_unix_socket_path(28011, {"XDG_RUNTIME_DIR": "/x"}, "darwin") == "/tmp/pcsx2.sock"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="needs AF_UNIX (Linux/macOS)")
def test_unix_socket_transport(tmp_path):
    path = str(tmp_path / "p.sock")
    ram = bytearray(4096); ram[0:4] = b"abcd"
    with MockPineServer(ram, unix_socket=path):
        with PineClient(PineConfig(unix_socket=path), platform="linux") as c:
            assert c.read32(0) == 0x64636261


def test_no_write_opcodes_exist():
    forbidden = {4, 5, 6, 7, 9, 0xA}
    assert not (pine.IMPLEMENTED_OPCODES & forbidden)
    assert pine.IMPLEMENTED_OPCODES == {0, 1, 2, 3, 8, 0xB, 0xC, 0xD, 0xE, 0xF}
    public = [n for n in dir(PineClient) if not n.startswith("_")]
    assert not [n for n in public if "write" in n.lower() or "save" in n.lower() or "load" in n.lower()]
    assert not hasattr(pine, "OP_WRITE8")
    src = inspect.getsource(pine)
    assert "MsgWrite" not in src.replace("Write opcodes", "")  # only mentioned in prose if at all


def test_mock_rejects_write_opcodes_and_ram_untouched(server):
    before = server.read_ram(0x100, 8)
    with socket.create_connection(("127.0.0.1", server.tcp_port), timeout=2) as s:
        for op, payload in [(4, b"\x00\x01\x00\x00\xFF"), (5, b"\x00\x01\x00\x00\xFF\xFF"),
                            (6, b"\x00\x01\x00\x00" + b"\xFF" * 4), (7, b"\x00\x01\x00\x00" + b"\xFF" * 8),
                            (9, b"\x00"), (0xA, b"\x00")]:
            body = bytes([op]) + payload
            s.sendall(struct.pack("<I", 4 + len(body)) + body)
            assert s.recv(16) == struct.pack("<IB", 5, 0xFF)
    assert server.rejected_commands == 6
    assert server.read_ram(0x100, 8) == before
