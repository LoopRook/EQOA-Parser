"""Mock PINE server serving a fake EE RAM image. Core module: no UI imports.

Implements the SERVER side of the wire format described at the top of pine.py (read from PCSX2's PINE.cpp).
It is a test double: ASSUMED to match real PCSX2 only as far as that source reading goes.
  * Write opcodes (4-7), SaveState (9), LoadState (0xA) and anything unknown are REJECTED with FAIL and counted
    in `rejected_commands`; RAM is never modified by a client.
  * Reads outside the RAM image FAIL (ASSUMED; real PCSX2 behaviour for such addresses is UNVERIFIED).
  * Request/reply size limits mirror PINE.cpp (650000 / 450000) so client batch-splitting is exercised.

Run manually:  python -m eqoa_parser.mock_pine --ram FILE --slot 28011
"""
from __future__ import annotations

import argparse
import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path

from .pine import (
    DEFAULT_SLOT,
    MAX_IPC_RETURN_SIZE,
    MAX_IPC_SIZE,
    TEXT_CODECS,
    default_unix_socket_path,
)

EE_RAM_SIZE = 32 * 1024 * 1024
_READ_WIDTH = {0: 1, 1: 2, 2: 4, 3: 8}
_INFO_STRINGS = {0x8: "version", 0xB: "title", 0xC: "game_id", 0xD: "uuid", 0xE: "game_version"}


class MockPineServer:
    """Threaded PINE server. `ram` is a mutable bytearray; use write_ram() from tests/simulators."""

    def __init__(
        self,
        ram: bytearray | bytes | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        unix_socket: str | None = None,
        has_vm: bool = True,
    ) -> None:
        if ram is None:
            ram = bytearray(EE_RAM_SIZE)
        self.ram = ram if isinstance(ram, bytearray) else bytearray(ram)
        self.lock = threading.Lock()
        self.has_vm = has_vm
        self.status = 0  # 0 running, 1 paused, 2 shutdown
        self.version = "PCSX2 v0.0.0-mock"
        self.title = "Mock Game"
        self.game_id = "SLUS-00000"
        self.uuid = "deadbeef"
        self.game_version = "1.00"
        self.rejected_commands = 0
        self.request_sizes: list[int] = []  # total_length of each request, for batching assertions
        self._host, self._port, self._unix = host, port, unix_socket
        self._servers: list[socketserver.BaseServer] = []
        self._threads: list[threading.Thread] = []
        self._conns: set[socket.socket] = set()
        self.tcp_port: int | None = None
        self.unix_path: str | None = None

    # -- RAM helpers (thread-safe) ----------------------------------------------------------------------------
    def write_ram(self, addr: int, data: bytes) -> None:
        with self.lock:
            self.ram[addr : addr + len(data)] = data

    def read_ram(self, addr: int, n: int) -> bytes:
        with self.lock:
            return bytes(self.ram[addr : addr + n])

    # -- protocol ---------------------------------------------------------------------------------------------
    def handle_request(self, payload: bytes) -> bytes:
        """payload = commands after the 4-byte length. Returns the full reply (with length prefix)."""
        fail = struct.pack("<IB", 5, 0xFF)
        out = bytearray()
        pos = 0
        n = len(payload)
        with self.lock:
            while pos < n:
                op = payload[pos]
                pos += 1
                if op in _READ_WIDTH:
                    width = _READ_WIDTH[op]
                    if not self.has_vm or pos + 4 > n or 5 + len(out) + width >= MAX_IPC_RETURN_SIZE:
                        return fail
                    (addr,) = struct.unpack_from("<I", payload, pos)
                    pos += 4
                    if addr + width > len(self.ram):
                        return fail
                    out += self.ram[addr : addr + width]
                elif op in _INFO_STRINGS:
                    if op != 0x8 and not self.has_vm:
                        return fail
                    data = getattr(self, _INFO_STRINGS[op]).encode("utf-8") + b"\x00"
                    out += struct.pack("<I", len(data)) + data
                elif op == 0xF:
                    out += struct.pack("<I", self.status)
                else:  # writes 4-7, SaveState 9, LoadState 0xA, unknown: rejected, RAM untouched
                    self.rejected_commands += 1
                    return fail
        return struct.pack("<IB", 5 + len(out), 0) + bytes(out)

    # -- sockets ----------------------------------------------------------------------------------------------
    def _make_handler(self):
        server = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                server._conns.add(sock)
                try:
                    self._loop(sock)
                finally:
                    server._conns.discard(sock)

            def _loop(self, sock):
                while True:
                    head = _recv_exact(sock, 4)
                    if head is None:
                        return
                    (total,) = struct.unpack("<I", head)
                    if total < 4 or total > MAX_IPC_SIZE:
                        return  # real server resets and keeps reading; closing is good enough for a mock
                    body = _recv_exact(sock, total - 4)
                    if body is None:
                        return
                    server.request_sizes.append(total)
                    sock.sendall(server.handle_request(body))

        return Handler

    def start(self) -> "MockPineServer":
        class TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        tcp = TcpServer((self._host, self._port), self._make_handler())
        self.tcp_port = tcp.server_address[1]
        self._servers.append(tcp)
        if self._unix and hasattr(socket, "AF_UNIX"):
            import os

            class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                daemon_threads = True

            if os.path.exists(self._unix):
                os.unlink(self._unix)
            unix = UnixServer(self._unix, self._make_handler())
            self.unix_path = self._unix
            self._servers.append(unix)
        for srv in self._servers:
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        for c in list(self._conns):  # drop live clients like a closing emulator would
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=2)
        self._servers, self._threads = [], []
        if self.unix_path:
            import os

            try:
                os.unlink(self.unix_path)
            except OSError:
                pass

    def __enter__(self) -> "MockPineServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def load_ram(path: str | Path | None) -> bytearray:
    ram = bytearray(EE_RAM_SIZE)
    if path is not None:
        data = Path(path).read_bytes()
        if len(data) > EE_RAM_SIZE:
            raise ValueError(f"RAM file is {len(data)} bytes, larger than {EE_RAM_SIZE}")
        ram[: len(data)] = data
    return ram


def _encode(text: str, encoding: str) -> bytes:
    return text.encode(TEXT_CODECS[encoding])


def _feeder(server: MockPineServer, addr: int, lines: list[str], interval: float, encoding: str, stop: threading.Event):
    """Simulated game: append NUL-terminated lines, one per interval. ASSUMED layout, for manual trials only."""
    cursor = addr
    for line in lines:
        if stop.wait(interval):
            return
        data = _encode(line, encoding) + (b"\x00\x00" if encoding == "utf16le" else b"\x00")
        server.write_ram(cursor, data)
        cursor += len(data)
        print(f"[mock] game wrote {len(data)} bytes at 0x{cursor - len(data):08X}: {line!r}", flush=True)


def main(argv: list[str] | None = None, stop: threading.Event | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eqoa_parser.mock_pine", description=__doc__.split("\n\n")[0])
    ap.add_argument("--ram", help="RAM image file to serve (padded with zeros to 32 MB)")
    ap.add_argument("--slot", type=int, default=DEFAULT_SLOT, help="TCP port (like PCSX2 on Windows); default 28011")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--unix", nargs="?", const="default", metavar="PATH",
        help="also listen on a UNIX socket (default path derived per the PINE spec from --slot)",
    )
    ap.add_argument("--plant", action="append", default=[], metavar="ADDR=TEXT", help="write TEXT into RAM at ADDR")
    ap.add_argument("--encoding", default="ascii", choices=["ascii", "utf16le", "shiftjis"], help="for --plant/--feed")
    ap.add_argument("--feed-addr", type=lambda s: int(s, 0), help="simulate the game appending lines starting here")
    ap.add_argument("--feed-file", help="text file, one line per game message")
    ap.add_argument("--feed-interval", type=float, default=1.0)
    ap.add_argument("--run-seconds", type=float, default=None, help="exit after this long (default: until Ctrl+C)")
    args = ap.parse_args(argv)

    server = MockPineServer(
        load_ram(args.ram), host=args.host, port=args.slot,
        unix_socket=(default_unix_socket_path(args.slot) if args.unix == "default" else args.unix),
    )
    for item in args.plant:
        addr_s, _, text = item.partition("=")
        data = _encode(text, args.encoding) + (b"\x00\x00" if args.encoding == "utf16le" else b"\x00")
        server.write_ram(int(addr_s, 0), data)
    server.start()
    print(f"[mock] PINE mock listening on tcp://{args.host}:{server.tcp_port}"
          + (f" and unix:{server.unix_path}" if server.unix_path else "") + " (read-only; writes rejected)", flush=True)
    stop = stop if stop is not None else threading.Event()
    if args.feed_addr is not None and args.feed_file:
        lines = [ln.rstrip("\r\n") for ln in Path(args.feed_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
        threading.Thread(
            target=_feeder, args=(server, args.feed_addr, lines, args.feed_interval, args.encoding, stop), daemon=True
        ).start()
    try:
        stop.wait(args.run_seconds)
    except KeyboardInterrupt:
        pass
    stop.set()
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
