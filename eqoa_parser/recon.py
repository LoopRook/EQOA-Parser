"""Recon tools for locating the chat/combat text buffer in EE RAM via PINE (read-only).

    python -m eqoa_parser.recon [--slot N] [--host H] [--socket PATH] <command> ...
      scan     --text STR --encoding ascii|utf16le|shiftjis
      snapshot --out FILE [--start A] [--end B]
      diff     A B   (B may be the word `live`)  [--text-only --encoding E]
      watch    --addr A --len N

Core module: no UI imports. UNVERIFIED against a real PCSX2; tested against eqoa_parser.mock_pine only.
Nothing here writes to the emulator (the PINE client has no write opcodes).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Iterator

from .config import PineConfig
from .pine import DEFAULT_SLOT, TEXT_CODECS, PineClient, PineError

RAM_START = 0x00000000
RAM_END = 0x02000000  # exclusive: EE RAM is 32 MB (0x00000000-0x01FFFFFF)
DEFAULT_CHUNK = 1 << 20

CODECS = TEXT_CODECS
ENCODINGS = list(CODECS)


def encode_text(text: str, encoding: str) -> bytes:
    try:
        return text.encode(CODECS[encoding])
    except UnicodeEncodeError as exc:
        raise ValueError(f"text {text!r} cannot be encoded as {encoding}: {exc}") from exc


def decode_lossy(data: bytes, encoding: str) -> str:
    """Decode for display: undecodable/control bytes become '.'."""
    text = data.decode(CODECS[encoding], errors="replace")
    return "".join(c if c.isprintable() else "." for c in text)


def hexdump(data: bytes, base: int, width: int = 16) -> list[str]:
    lines = []
    for i in range(0, len(data), width):
        row = data[i : i + width]
        hx = " ".join(f"{b:02x}" for b in row).ljust(width * 3 - 1)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"  {base + i:08X}  {hx}  |{asc}|")
    return lines


# -- scanning --------------------------------------------------------------------------------------------------
def iter_chunks(client: PineClient, start: int, end: int, chunk: int = DEFAULT_CHUNK) -> Iterator[tuple[int, bytes]]:
    pos = start
    while pos < end:
        n = min(chunk, end - pos)
        yield pos, client.read_bytes(pos, n)
        pos += n


def find_matches(client: PineClient, needle: bytes, start: int = RAM_START, end: int = RAM_END,
                 chunk: int = DEFAULT_CHUNK) -> Iterator[int]:
    """Yield addresses of every occurrence of needle in [start, end), including ones spanning chunk boundaries.

    Each chunk is searched together with the last len(needle)-1 bytes of the previous one. A carried tail is shorter
    than the needle so it can never hold a whole match by itself: no duplicates, no misses.
    """
    if not needle:
        raise ValueError("empty search string")
    keep = len(needle) - 1
    carry = b""
    for pos, data in iter_chunks(client, start, end, chunk):
        buf = carry + data
        base = pos - len(carry)
        i = buf.find(needle)
        while i != -1:
            yield base + i
            i = buf.find(needle, i + 1)
        carry = buf[-keep:] if keep else b""


def cmd_scan(client: PineClient, args, out=print) -> int:
    needle = encode_text(args.text, args.encoding)
    out(f"scanning 0x{args.start:08X}-0x{args.end - 1:08X} for {args.text!r} as {args.encoding} ({needle.hex(' ')})")
    count = 0
    for addr in find_matches(client, needle, args.start, args.end, args.chunk):
        count += 1
        note = ""
        if args.encoding == "utf16le" and addr % 2:
            note = "  (odd address: unlikely for real UTF-16 text)"
        out(f"match {count}: 0x{addr:08X}{note}")
        c0 = max(RAM_START, addr - args.context)
        ctx = client.read_bytes(c0, min(args.context * 2 + len(needle), RAM_END - c0))
        for line in hexdump(ctx, c0):
            out(line)
        out(f"  text: {decode_lossy(ctx, args.encoding)}")
        if count >= args.max_matches:
            out(f"stopped after {count} matches (--max-matches)")
            break
    out(f"{count} match(es)")
    return 0 if count else 1


# -- snapshot / diff -------------------------------------------------------------------------------------------
def read_range(client: PineClient, start: int, end: int, chunk: int = DEFAULT_CHUNK, progress=None) -> bytes:
    parts = []
    for pos, data in iter_chunks(client, start, end, chunk):
        parts.append(data)
        if progress:
            progress(pos + len(data) - start, end - start)
    return b"".join(parts)


def _meta_path(p: Path) -> Path:
    return p.with_name(p.name + ".meta.json")


def save_snapshot(path: Path, data: bytes, start: int) -> None:
    path.write_bytes(data)
    _meta_path(path).write_text(json.dumps({"start": start, "length": len(data)}), encoding="utf-8")


def load_snapshot(path: Path) -> tuple[int, bytes]:
    """(start_address, bytes). A sidecar FILE.meta.json (written by `snapshot`) gives the start; else 0."""
    data = path.read_bytes()
    meta = _meta_path(path)
    start = json.loads(meta.read_text(encoding="utf-8"))["start"] if meta.exists() else 0
    return start, data


def cmd_snapshot(client: PineClient, args, out=print) -> int:
    def progress(done, total):
        if args.progress:
            print(f"\r  {done * 100 // total:3d}%", end="", file=sys.stderr, flush=True)

    data = read_range(client, args.start, args.end, args.chunk, progress)
    if args.progress:
        print(file=sys.stderr)
    save_snapshot(Path(args.out), data, args.start)
    out(f"saved {len(data)} bytes (0x{args.start:08X}-0x{args.end - 1:08X}) to {args.out}")
    return 0


def changed_ranges(a: bytes, b: bytes, base: int = 0, gap: int = 16, block: int = 256) -> list[tuple[int, int]]:
    """Merged [start, end) address ranges where a and b differ (changes <= `gap` apart are merged)."""
    if len(a) != len(b):
        raise ValueError("inputs must be the same length")
    raw: list[list[int]] = []
    for off in range(0, len(a), block):
        if a[off : off + block] == b[off : off + block]:
            continue
        for i in range(off, min(off + block, len(a))):
            if a[i] != b[i]:
                if raw and i - raw[-1][1] <= gap:
                    raw[-1][1] = i + 1
                else:
                    raw.append([i, i + 1])
    return [(base + s, base + e) for s, e in raw]


_ASCII_RUN = rb"[\x20-\x7e\t\r\n]"
_TEXT_RUN = {
    "ascii": lambda n: re.compile(_ASCII_RUN + b"{%d,}" % n),
    "utf16le": lambda n: re.compile(rb"(?:[\x20-\x7e\t\r\n]\x00){%d,}" % n),
    "shiftjis": lambda n: re.compile(
        rb"(?:[\x20-\x7e]|[\xa1-\xdf]|[\x81-\x9f\xe0-\xfc][\x40-\x7e\x80-\xfc]){%d,}" % n
    ),
}


def looks_like_text(data: bytes, encoding: str, min_len: int = 4) -> bool:
    """True if `data` contains a run of >= min_len characters that decode as printable text in `encoding`."""
    for m in _TEXT_RUN[encoding](min_len).finditer(data):
        try:
            if m.group().decode(CODECS[encoding]).strip():
                return True
        except UnicodeDecodeError:
            continue
    return False


def cmd_diff(client: PineClient | None, args, out=print) -> int:
    start_a, a = load_snapshot(Path(args.a))
    if args.b == "live":
        assert client is not None
        b = read_range(client, start_a, start_a + len(a), args.chunk)
        start_b = start_a
    else:
        start_b, b = load_snapshot(Path(args.b))
    if start_a != start_b or len(a) != len(b):
        out(f"error: snapshots differ in range (A 0x{start_a:08X}+{len(a)}, B 0x{start_b:08X}+{len(b)})")
        return 2
    ranges = changed_ranges(a, b, start_a, args.gap)
    shown = 0
    for lo, hi in ranges:
        s, e = max(start_a, lo - args.context), min(start_a + len(a), hi + args.context)
        new = b[s - start_a : e - start_a]
        if args.text_only and not looks_like_text(new, args.encoding, args.min_len):
            continue
        shown += 1
        out(f"region 0x{lo:08X}-0x{hi - 1:08X} ({hi - lo} bytes changed)")
        out("  old:")
        for line in hexdump(a[s - start_a : e - start_a], s):
            out(line)
        out("  new:")
        for line in hexdump(new, s):
            out(line)
        out(f"  new text ({args.encoding}): {decode_lossy(new, args.encoding)}")
        if shown >= args.max_regions:
            out(f"stopped after {shown} regions (--max-regions)")
            break
    out(f"{len(ranges)} changed region(s) total, {shown} shown" + (" (text filter on)" if args.text_only else ""))
    return 0


# -- watch -----------------------------------------------------------------------------------------------------
def cmd_watch(client: PineClient, args, out=print) -> int:
    prev: bytes | None = None
    shown = 0
    deadline = None if args.duration is None else time.monotonic() + args.duration
    while True:
        cur = client.read_bytes(args.addr, args.len)
        if cur != prev:
            diff = 0 if prev is None else sum(1 for x, y in zip(prev, cur) if x != y)
            stamp = time.strftime("%H:%M:%S")
            out(f"[{stamp}] 0x{args.addr:08X}+{args.len}: " + ("initial" if prev is None else f"{diff} byte(s) changed"))
            for line in hexdump(cur, args.addr):
                out(line)
            out(f"  text ({args.encoding}): {decode_lossy(cur, args.encoding)}")
            prev = cur
            shown += 1
        if args.count is not None and shown >= args.count:
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            return 0
        time.sleep(args.interval)


# -- CLI -------------------------------------------------------------------------------------------------------
def _addr(s: str) -> int:
    return int(s, 0)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m eqoa_parser.recon", description=__doc__.split("\n\n")[0])
    ap.add_argument("--slot", type=int, default=DEFAULT_SLOT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--socket", default=None, help="UNIX socket path override (Linux)")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="bytes per read chunk (default 1 MiB)")
    sub = ap.add_subparsers(dest="command", required=True)

    def rng(p):
        p.add_argument("--start", type=_addr, default=RAM_START)
        p.add_argument("--end", type=_addr, default=RAM_END, help="exclusive; default 0x02000000")

    p = sub.add_parser("scan", help="find a string in EE RAM")
    p.add_argument("--text", required=True)
    p.add_argument("--encoding", choices=ENCODINGS, default="ascii")
    p.add_argument("--context", type=int, default=32, help="bytes of context before/after each match")
    p.add_argument("--max-matches", type=int, default=50)
    rng(p)
    p = sub.add_parser("snapshot", help="save RAM (or a range) to a file")
    p.add_argument("--out", required=True)
    p.add_argument("--progress", action="store_true")
    rng(p)
    p = sub.add_parser("diff", help="changed regions between two snapshots, or a snapshot and `live`")
    p.add_argument("a")
    p.add_argument("b", help="snapshot file, or the word live")
    p.add_argument("--gap", type=int, default=16, help="merge changes this close together")
    p.add_argument("--context", type=int, default=0, help="extra bytes shown around each region")
    p.add_argument("--text-only", action="store_true", help="only regions whose NEW bytes decode as text")
    p.add_argument("--encoding", choices=ENCODINGS, default="ascii")
    p.add_argument("--min-len", type=int, default=4, help="min characters in a text run for --text-only")
    p.add_argument("--max-regions", type=int, default=200)
    p = sub.add_parser("watch", help="dump a region live whenever it changes")
    p.add_argument("--addr", type=_addr, required=True)
    p.add_argument("--len", type=int, required=True)
    p.add_argument("--encoding", choices=ENCODINGS, default="ascii")
    p.add_argument("--interval", type=float, default=0.2)
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds (default: Ctrl+C)")
    p.add_argument("--count", type=int, default=None, help="stop after N dumps")
    return ap


def _tolerate_console_encoding() -> None:
    """Decoded game text (e.g. Japanese) must not crash printing on a cp1252 console: escape instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _tolerate_console_encoding()
    args = build_parser().parse_args(argv)
    if args.command in ("scan", "snapshot"):
        if not RAM_START <= args.start < args.end <= RAM_END:
            print("error: need 0 <= start < end <= 0x02000000", file=sys.stderr)
            return 2
    cfg = PineConfig(slot=args.slot, host=args.host, unix_socket=args.socket, timeout=args.timeout)
    needs_client = not (args.command == "diff" and args.b != "live")
    try:
        if needs_client:
            with PineClient(cfg) as client:
                return _dispatch(client, args)
        return _dispatch(None, args)
    except PineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch(client, args) -> int:
    return {"scan": cmd_scan, "snapshot": cmd_snapshot, "diff": cmd_diff, "watch": cmd_watch}[args.command](client, args)


if __name__ == "__main__":
    sys.exit(main())
