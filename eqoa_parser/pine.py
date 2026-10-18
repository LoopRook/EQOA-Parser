"""Read-only PINE client for PCSX2. Core module: no UI imports.

SPEC SOURCE (fetched and read, not written from memory):
  * https://github.com/PCSX2/pcsx2/blob/master/pcsx2/PINE.cpp  (last commit touching the file when read:
    b67e793283f2ba45b9eabd655afe35994a3e138f, 2026-06-15; read on 2026-10-17)
  * https://github.com/PCSX2/pcsx2/blob/master/pcsx2/PINE.h      (PINE_DEFAULT_SLOT 28011, slot concept)
  * https://github.com/GovanifY/pine (README only; its protocol "standard/" draft was NOT read, the wire format
    below comes from the PCSX2 server source, which is the reference server)

WIRE FORMAT (as implemented by PINE.cpp ClientLoop/ParseCommand):
  * All integers little-endian.
  * Request  = u32 total_length (INCLUDES these 4 bytes) + one or more commands back to back.
    A command is 1 opcode byte, followed (for reads) by a u32 address.
  * Reply    = u32 total_length (INCLUDES these 4 bytes) + 1 status byte (0x00 OK, 0xFF FAIL) + the results of
    every command, concatenated, in order. Read8/16/32/64 results are 1/2/4/8 bytes, little-endian.
  * Batch semantics: several commands per request are executed in order; ANY failing command makes the whole
    reply just `05 00 00 00 FF` (no partial results).
  * Limits (server side): MAX_IPC_SIZE 650000 bytes per request, MAX_IPC_RETURN_SIZE 450000 per reply (the
    server fails the batch if reply bytes would reach 450000). The server's own comments say "50,000 Write64
    / Read64" per message; this client uses MAX_BATCH_OPS = 50000 reads per message (5 + 8*50000 = 400005 reply bytes).
  * Read opcodes: 0 Read8, 1 Read16, 2 Read32, 3 Read64. Info opcodes used here (read-only):
    8 Version (u32 size + "PCSX2 <gitrev>\\0"), 0xB Title, 0xC ID (disc serial), 0xD UUID (disc CRC hex),
    0xE GameVersion (each: u32 size + NUL-terminated string; FAIL when no VM is running),
    0xF Status (u32: 0 running, 1 paused, 2 shutdown; works with no VM).
  * Memory reads fail (0xFF) when no VM is valid. Address semantics are `vtlb_ramRead(addr)`; PINE.cpp does not
    document them and vtlb was NOT read. UNVERIFIED: whether out-of-range / unmapped addresses fail or return
    garbage on real PCSX2. This client never reads outside what the caller asks for.
  * Transport: Windows = TCP 127.0.0.1:<slot> (port is the slot even for the default slot). Everything else =
    AF_UNIX stream socket at "$XDG_RUNTIME_DIR/pcsx2.sock" (macOS: "$TMPDIR/pcsx2.sock"; if the variable is
    unset: "/tmp/pcsx2.sock"), plus the suffix ".<slot>" ONLY when slot != 28011.

READ-ONLY BY DESIGN: the write opcodes (4-7), SaveState (9) and LoadState (0xA) are deliberately NOT implemented;
no code path in this module can emit them (tests/test_pine_client.py checks this).

VERIFIED against a real PCSX2 v2.8.2 (reported by the main chat, EQOA Frontiers SLUS-20744 v1.02, slot 28012): the
Windows TCP transport, the info calls (version/status/title/id/game_version), the Read8/16/32/64 opcodes (widths agree
with read_bytes, little-endian), 1 MiB reads in ~0.09 s and a full 32 MB scan in ~2.6 s. Also observed: a read at
0x02000000 (past the 32 MB of EE RAM) returned 0 with NO error, i.e. PCSX2 does not reject out-of-range reads; callers
must bound addresses themselves (the mock still FAILs them, which differs from real PCSX2).
STILL UNVERIFIED: the Linux/macOS UNIX-socket transport (path rules are tested, a live socket only on the mock), and
PINE behaviour on other PCSX2 versions.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
from typing import Iterable, Sequence

from .config import PineConfig

DEFAULT_SLOT = 28011
MAX_IPC_SIZE = 650000
MAX_IPC_RETURN_SIZE = 450000
MAX_BATCH_OPS = 50000

# Read-only opcodes. Write/SaveState/LoadState opcodes are intentionally absent.
OP_READ8, OP_READ16, OP_READ32, OP_READ64 = 0, 1, 2, 3
OP_VERSION, OP_TITLE, OP_ID, OP_UUID, OP_GAME_VERSION, OP_STATUS = 0x8, 0xB, 0xC, 0xD, 0xE, 0xF
IMPLEMENTED_OPCODES = frozenset(
    {OP_READ8, OP_READ16, OP_READ32, OP_READ64, OP_VERSION, OP_TITLE, OP_ID, OP_UUID, OP_GAME_VERSION, OP_STATUS}
)

STATUS_OK, STATUS_FAIL = 0x00, 0xFF
EMU_STATUS = {0: "running", 1: "paused", 2: "shutdown"}
# The ONE encoding-name -> Python codec mapping used by recon, pine_source and mock_pine.
TEXT_CODECS = {"ascii": "ascii", "utf16le": "utf-16-le", "shiftjis": "shift_jis"}
_WIDTH_OP = {1: OP_READ8, 2: OP_READ16, 4: OP_READ32, 8: OP_READ64}


class PineError(Exception):
    """Base class for PINE problems."""


class PineConnectionError(PineError):
    """PCSX2 unreachable (not running, PINE disabled, wrong slot) or the connection dropped."""


class PineTimeoutError(PineConnectionError):
    """No reply within the configured timeout."""


class PineProtocolError(PineError):
    """The reply did not look like PINE."""


class PineCommandFailed(PineError):
    """The server answered FAIL (0xFF): no game/VM running, bad address, or oversized batch."""


def default_unix_socket_path(slot: int = DEFAULT_SLOT, environ=None, platform: str | None = None) -> str:
    """Socket path per PINE.cpp: <runtime dir>/pcsx2.sock[.<slot> if slot != 28011]."""
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    runtime_dir = env.get("TMPDIR") if platform == "darwin" else env.get("XDG_RUNTIME_DIR")
    path = "/tmp/pcsx2.sock" if runtime_dir is None else runtime_dir + "/pcsx2.sock"
    if slot != DEFAULT_SLOT:
        path += "." + str(slot)
    return path


def _check_addr(addr: int) -> int:
    if not isinstance(addr, int) or not 0 <= addr <= 0xFFFFFFFF:
        raise ValueError(f"address out of u32 range: {addr!r}")
    return addr


class PineClient:
    """Blocking read-only PINE client. Connects lazily on first use; usable as a context manager."""

    def __init__(self, pine_config: PineConfig | None = None, *, platform: str | None = None) -> None:
        self.config = pine_config if pine_config is not None else PineConfig()
        self.platform = sys.platform if platform is None else platform
        # Transport is chosen once, from the platform, as the spec's server does at compile time.
        self.transport = "tcp" if self.platform == "win32" else "unix"
        self._sock: socket.socket | None = None

    # -- connection -------------------------------------------------------------------------------------------
    @property
    def endpoint(self) -> str:
        if self.transport == "tcp":
            return f"tcp://{self.config.host}:{self.config.slot}"
        return "unix:" + (self.config.unix_socket or default_unix_socket_path(self.config.slot, platform=self.platform))

    def connect(self) -> None:
        if self._sock is not None:
            return
        hint = (
            "Is PCSX2 running with PINE enabled (and the same slot)? "
            "See docs/finding-the-buffer.md, step 1."
        )
        try:
            if self.transport == "tcp":
                sock = socket.create_connection((self.config.host, self.config.slot), timeout=self.config.timeout)
            else:
                if not hasattr(socket, "AF_UNIX"):
                    raise PineConnectionError("this Python has no AF_UNIX support; set a TCP-capable platform")
                path = self.config.unix_socket or default_unix_socket_path(self.config.slot, platform=self.platform)
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self.config.timeout)
                try:
                    sock.connect(path)
                except BaseException:
                    sock.close()
                    raise
        except PineError:
            raise
        except socket.timeout as exc:
            raise PineTimeoutError(f"timed out connecting to {self.endpoint}. {hint}") from exc
        except OSError as exc:
            raise PineConnectionError(f"cannot connect to PCSX2 at {self.endpoint}: {exc}. {hint}") from exc
        sock.settimeout(self.config.timeout)
        self._sock = sock

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "PineClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- framing ----------------------------------------------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        chunks: list[bytes] = []
        remaining = n
        while remaining:
            try:
                data = self._sock.recv(min(remaining, 1 << 20))
            except socket.timeout as exc:
                self.close()
                raise PineTimeoutError(f"no reply from PCSX2 within {self.config.timeout}s") from exc
            except OSError as exc:
                self.close()
                raise PineConnectionError(f"connection to PCSX2 lost: {exc}") from exc
            if not data:
                self.close()
                raise PineConnectionError("PCSX2 closed the PINE connection")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def _transact(self, commands: bytes) -> bytes:
        """Send one request, return the reply body (after the status byte). Raises PineCommandFailed on FAIL."""
        total = 4 + len(commands)
        if total > MAX_IPC_SIZE:
            raise ValueError(f"request of {total} bytes exceeds MAX_IPC_SIZE {MAX_IPC_SIZE}")
        self.connect()
        assert self._sock is not None
        try:
            self._sock.sendall(struct.pack("<I", total) + commands)
        except socket.timeout as exc:
            self.close()
            raise PineTimeoutError("timed out sending to PCSX2") from exc
        except OSError as exc:
            self.close()
            raise PineConnectionError(f"connection to PCSX2 lost: {exc}") from exc
        (reply_len,) = struct.unpack("<I", self._recv_exact(4))
        if reply_len < 5 or reply_len > MAX_IPC_RETURN_SIZE:
            self.close()
            raise PineProtocolError(f"implausible reply length {reply_len}; not a PINE server?")
        rest = self._recv_exact(reply_len - 4)
        if rest[0] == STATUS_FAIL:
            raise PineCommandFailed(
                "PCSX2 rejected the PINE command (no game running / invalid address / oversized batch)"
            )
        if rest[0] != STATUS_OK:
            self.close()
            raise PineProtocolError(f"unknown reply status byte 0x{rest[0]:02x}")
        return rest[1:]

    # -- memory reads (opcodes 0-3 only) ----------------------------------------------------------------------
    def read_batch(self, reads: Iterable[tuple[int, int]]) -> list[int]:
        """reads = [(width_bytes, addr), ...] with width in {1,2,4,8}; returns ints, same order.

        More than MAX_BATCH_OPS reads are split over several messages (each message is atomic, the sequence is not).
        """
        ops = list(reads)
        results: list[int] = []
        for i in range(0, len(ops), MAX_BATCH_OPS):
            part = ops[i : i + MAX_BATCH_OPS]
            body = self._batch_raw(part)
            pos = 0
            for width, _ in part:
                results.append(int.from_bytes(body[pos : pos + width], "little"))
                pos += width
        return results

    def _batch_raw(self, ops: Sequence[tuple[int, int]]) -> bytes:
        if not ops:
            return b""
        fmt = "<" + "BI" * len(ops)
        flat: list[int] = []
        for width, addr in ops:
            if width not in _WIDTH_OP:
                raise ValueError(f"unsupported read width {width}")
            flat.append(_WIDTH_OP[width])
            flat.append(_check_addr(addr))
        body = self._transact(struct.pack(fmt, *flat))
        expected = sum(w for w, _ in ops)
        if len(body) != expected:
            raise PineProtocolError(f"reply carried {len(body)} result bytes, expected {expected}")
        return body

    def read8(self, addr: int) -> int:
        return self.read_batch([(1, addr)])[0]

    def read16(self, addr: int) -> int:
        return self.read_batch([(2, addr)])[0]

    def read32(self, addr: int) -> int:
        return self.read_batch([(4, addr)])[0]

    def read64(self, addr: int) -> int:
        return self.read_batch([(8, addr)])[0]

    def read_bytes(self, addr: int, n: int) -> bytes:
        """n bytes starting at addr, via batched Read8 (unaligned head/tail) and Read64 (aligned middle).

        Little-endian replies concatenate to memory order (the EE is little-endian), so the reply body IS the data.
        """
        if n < 0:
            raise ValueError("n must be >= 0")
        _check_addr(addr)
        if n and addr + n - 1 > 0xFFFFFFFF:
            raise ValueError("read runs past the end of the 32-bit address space")
        ops: list[tuple[int, int]] = []
        cur, end = addr, addr + n
        while cur < end and cur % 8:
            ops.append((1, cur))
            cur += 1
        while end - cur >= 8:
            ops.append((8, cur))
            cur += 8
        while cur < end:
            ops.append((1, cur))
            cur += 1
        out = bytearray()
        for i in range(0, len(ops), MAX_BATCH_OPS):
            out += self._batch_raw(ops[i : i + MAX_BATCH_OPS])
        return bytes(out)

    # -- read-only info calls ---------------------------------------------------------------------------------
    def _info_string(self, opcode: int) -> str:
        body = self._transact(bytes([opcode]))
        if len(body) < 4:
            raise PineProtocolError("short info reply")
        (size,) = struct.unpack_from("<I", body)
        data = body[4 : 4 + size]
        if len(data) != size:
            raise PineProtocolError("truncated info string")
        return data.split(b"\x00", 1)[0].decode("utf-8", "replace")

    def version(self) -> str:
        """e.g. "PCSX2 v2.x.y-..." (the server prints "PCSX2 <GitRev>")."""
        return self._info_string(OP_VERSION)

    def title(self) -> str:
        return self._info_string(OP_TITLE)

    def game_id(self) -> str:
        return self._info_string(OP_ID)

    def game_uuid(self) -> str:
        return self._info_string(OP_UUID)

    def game_version(self) -> str:
        return self._info_string(OP_GAME_VERSION)

    def status(self) -> str:
        """"running" | "paused" | "shutdown"."""
        body = self._transact(bytes([OP_STATUS]))
        if len(body) != 4:
            raise PineProtocolError("bad status reply")
        (value,) = struct.unpack("<I", body)
        return EMU_STATUS.get(value, f"unknown({value})")
