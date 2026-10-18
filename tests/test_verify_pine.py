"""Independent verification (session D): PineBufferSource / PINE client against eqoa_parser.mock_pine.

Everything is SYNTHETIC: the real buffer layout is unknown, so the "game" below is a simulator of the ASSUMED
layouts ("scroll", "ring") written from the contract text, not from the implementation.  Tests whose docstring starts
with "BUG-n" assert contract behaviour and are EXPECTED TO FAIL against the current code (real findings, listed in
docs/VERIFICATION.md).  Tests starting "LIMIT" are characterisation tests of limits the module documents.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time

import pytest

from eqoa_parser.config import PineConfig, load_config
from eqoa_parser.mock_pine import MockPineServer
from eqoa_parser.pine import PineClient, PineConnectionError, PineError
from eqoa_parser.pine_source import BufferNotConfiguredError, PineBufferSource
from eqoa_parser.pipeline import Engine
from eqoa_parser.sources import HEARTBEAT

ADDR = 0x0010_0000
LINE = "You hit a rat for 5 points of damage."


# ---------------------------------------------------------------------------------------------------- harness
@pytest.fixture()
def server():
    with MockPineServer() as s:
        yield s


def make_cfg(server, *, layout="scroll", size=256, encoding="ascii", emit_initial=False, stable=True, poll=0.01, tmp=None, **kw):
    cfg = load_config(**kw)
    cfg.pine = PineConfig(slot=server.tcp_port, timeout=2.0)
    cfg.buffer.address = ADDR
    cfg.buffer.size = size
    cfg.buffer.layout = layout
    cfg.buffer.encoding = encoding
    cfg.buffer.emit_initial = emit_initial
    cfg.buffer.stable_reads = stable
    cfg.buffer.poll_interval = poll
    if tmp is not None:
        cfg.unmatched_log = str(tmp / "unmatched.log")
        cfg.runs_dir = str(tmp / "runs")
    return cfg


def make_src(server, **kw):
    cfg = make_cfg(server, **kw)
    return PineBufferSource(cfg, client=PineClient(cfg.pine)), cfg


def enc(text, encoding="ascii"):
    return text.encode({"ascii": "ascii", "utf16le": "utf-16-le", "shiftjis": "shift_jis"}[encoding])


class ScrollGame:
    """ASSUMED scroll layout: region holds NUL-terminated lines from offset 0; oldest whole lines drop off the front."""

    def __init__(self, server, size):
        self.server, self.size, self.lines = server, size, []

    def _render(self):
        lines = list(self.lines)
        while sum(len(x) + 1 for x in lines) > self.size:
            lines.pop(0)
        self.lines = lines
        data = b"".join(x.encode() + b"\x00" for x in lines)
        return data.ljust(self.size, b"\x00")

    def append(self, *new):
        self.lines.extend(new)
        self.server.write_ram(ADDR, self._render())

    def clear(self):
        self.lines = []
        self.server.write_ram(ADDR, bytes(self.size))


class RingGame:
    """ASSUMED ring: text overwrites the oldest text at a write cursor.  straddle=False: a message that does not fit
    before the end of the region restarts at offset 0 (stale tail bytes remain).  straddle=True: bytes wrap mid-message."""

    def __init__(self, server, size, straddle=False):
        self.server, self.size, self.straddle, self.cursor = server, size, straddle, 0
        self.ram = bytearray(size)

    def write(self, *msgs):
        for m in msgs:
            data = m.encode() + b"\x00"
            if self.straddle:
                for b in data:
                    self.ram[self.cursor] = b
                    self.cursor = (self.cursor + 1) % self.size
            else:
                if self.cursor + len(data) > self.size:
                    self.cursor = 0
                self.ram[self.cursor:self.cursor + len(data)] = data
                self.cursor += len(data)
        self.server.write_ram(ADDR, bytes(self.ram))


def drain(src, polls=1):
    out = []
    for _ in range(polls):
        out.extend(src.poll_once())
    return out


class ScriptedClient:
    """Wraps a real PineClient; `script` entries override the next read_bytes() results (None = real read)."""

    def __init__(self, real, script=()):
        self.real, self.script, self.reads = real, list(script), 0

    def read_bytes(self, addr, n):
        self.reads += 1
        if self.script:
            item = self.script.pop(0)
            if item is not None:
                return item.ljust(n, b"\x00")[:n]
        return self.real.read_bytes(addr, n)

    def close(self):
        self.real.close()


# ============================================================================== construction / wiring
def test_no_address_raises_clear_error_pointing_at_doc():
    cfg = load_config()
    with pytest.raises(BufferNotConfiguredError) as e:
        PineBufferSource(cfg)
    assert "finding-the-buffer.md" in str(e.value) and "buffer.address" in str(e.value)


def test_unknown_layout_and_encoding_rejected(server):
    with pytest.raises(ValueError):
        make_src(server, layout="nonsense")
    with pytest.raises(ValueError):
        make_src(server, encoding="latin-1")


def test_sources_module_reexports_the_same_class():
    import eqoa_parser.sources as s

    assert s.PineBufferSource is PineBufferSource


# ============================================================================== duplicates: unchanged memory
@pytest.mark.parametrize("layout", ["scroll", "ring"])
@pytest.mark.parametrize("stable", [True, False])
def test_unchanged_memory_polled_many_times_yields_nothing(server, layout, stable):
    server.write_ram(ADDR, b"old line one\x00old line two\x00" + enc(LINE) + b"\x00")
    src, _ = make_src(server, layout=layout, stable=stable)
    assert drain(src, 60) == []                 # initial content is history (emit_initial False), and stays unseen
    src.close()


@pytest.mark.parametrize("layout", ["scroll", "ring"])
def test_emit_initial_yields_history_exactly_once(server, layout):
    server.write_ram(ADDR, b"first\x00second\x00third\x00")
    src, _ = make_src(server, layout=layout, emit_initial=True)
    assert drain(src) == ["first", "second", "third"]
    assert drain(src, 40) == []
    src.close()


@pytest.mark.parametrize("layout", ["scroll", "ring"])
def test_empty_buffer_never_yields(server, layout):
    src, _ = make_src(server, layout=layout)
    assert drain(src, 20) == []
    src.close()


# ============================================================================== appended text yields once
def test_scroll_appended_lines_yield_once_each_in_order(server):
    g = ScrollGame(server, 256)
    g.append("history-1", "history-2")
    src, _ = make_src(server)
    assert drain(src) == []
    got = []
    for i in range(25):
        g.append(f"new line {i}")
        got += drain(src, 3)                    # 1 poll that sees it + 2 repeat polls of unchanged memory
    assert got == [f"new line {i}" for i in range(25)]
    src.close()


@pytest.mark.parametrize("layout", ["scroll", "ring"])
def test_genuinely_repeated_identical_combat_line_is_new_each_time(server, layout):
    """Same text appended repeatedly (while the region is not yet full) is a distinct message every time."""
    if layout == "scroll":
        g = ScrollGame(server, 512)
        write = g.append
    else:
        r = RingGame(server, 512)
        write = r.write
    write("seed line")
    src, _ = make_src(server, layout=layout, size=512)
    drain(src)
    got = []
    for _ in range(6):
        write(LINE)
        got += drain(src, 2)
    assert got == [LINE] * 6
    write(LINE, LINE, LINE)                      # three identical lines landing between two polls
    assert drain(src, 2) == [LINE] * 3
    src.close()


# ============================================================================== wrap / scroll
def test_scroll_window_scrolling_random_batches_exactly_once(server):
    rnd = random.Random(1234)
    size = 160
    g = ScrollGame(server, size)
    src, _ = make_src(server, size=size)
    drain(src)
    expected, got, n = [], [], 0
    for _ in range(400):
        batch = [f"line {n + i:05d} " + "x" * rnd.randint(0, 10) for i in range(rnd.randint(0, 3))]
        n += len(batch)
        expected += batch
        if batch:
            g.append(*batch)
        got += drain(src, rnd.randint(1, 3))
    assert got == expected and len(expected) > 300
    src.close()


def test_scroll_random_repeating_alphabet_batch_one(server):
    """Few distinct combat lines repeated at random, one new line per poll, full scrolling window."""
    rnd = random.Random(99)
    alphabet = [f"You hit a rat for {d} points of damage." for d in (5, 6, 7, 8)]
    size = 200
    g = ScrollGame(server, size)
    g.append(*[rnd.choice(alphabet) for _ in range(10)])     # full window before we start
    src, _ = make_src(server, size=size)
    drain(src)
    expected, got = [], []
    for _ in range(300):
        line = rnd.choice(alphabet)
        expected.append(line)
        g.append(line)
        got += drain(src)
    assert got == expected


def test_limit_full_window_of_identical_lines_is_undetectable(server):
    """LIMIT-1 (documented in pine_source.py, limit (a)): once a scrolling window is entirely identical lines, a further
    identical line is indistinguishable from 'nothing happened' and is dropped.  This characterises, not endorses."""
    size = 3 * (len(LINE) + 1)
    g = ScrollGame(server, size)
    g.append(LINE, LINE, LINE)
    src, _ = make_src(server, size=size)
    drain(src)
    got = []
    for _ in range(5):
        g.append(LINE)
        got += drain(src)
    assert got == []                                  # 5 genuine messages, 0 seen


def test_scroll_cleared_then_rewritten_with_new_text(server):
    g = ScrollGame(server, 128)
    g.append("a1", "a2")
    src, _ = make_src(server, size=128)
    drain(src)
    g.clear()
    assert drain(src, 2) == []
    g.append("b1")
    assert drain(src, 2) == ["b1"]
    src.close()


def test_scroll_same_length_in_place_change_is_new(server):
    g = ScrollGame(server, 128)
    g.append("You hit a rat for 5 points of damage.")
    src, _ = make_src(server, size=128)
    drain(src)
    server.write_ram(ADDR, b"You hit a rat for 6 points of damage.\x00")
    assert drain(src, 2) == ["You hit a rat for 6 points of damage."]
    src.close()


def test_ring_nonstraddling_wraparound_each_message_once_in_order(server):
    size = 200
    r = RingGame(server, size)
    r.write("seed")
    src, _ = make_src(server, layout="ring", size=size)
    drain(src)
    rnd = random.Random(5)
    expected, got = [], []
    for i in range(300):
        batch = [f"msg {i}.{j} " + "y" * rnd.randint(0, 15) for j in range(rnd.randint(1, 2))]
        expected += batch
        r.write(*batch)
        got += drain(src, rnd.randint(1, 2))
    assert got == expected


def test_ring_identical_messages_in_different_slots_are_distinct_within_one_cycle(server):
    size = 4 * (len(LINE) + 1)
    r = RingGame(server, size)
    src, _ = make_src(server, layout="ring", size=size)
    drain(src)
    got = []
    for _ in range(4):                                    # four slots, four byte-identical messages: all distinct
        r.write(LINE)
        got += drain(src)
    assert got == [LINE] * 4


def test_limit_ring_byte_identical_overwrite_of_the_same_slot_is_invisible(server):
    """LIMIT-4 (documented in pine_source.py): a message overwritten by byte-identical text at the same offset cannot be
    detected.  With fixed-size slots and a repeated line (same mob, same damage) every message after the first cycle is
    lost.  Characterisation only."""
    size = 4 * (len(LINE) + 1)
    r = RingGame(server, size)
    src, _ = make_src(server, layout="ring", size=size)
    drain(src)
    got = []
    for _ in range(12):
        r.write(LINE)
        got += drain(src)
    assert got == [LINE] * 4                              # 12 genuine messages, 4 seen


def test_ring_straddling_message_is_reassembled(server):
    """BUG-8 (medium, ASSUMED-layout gap): the ring layout treats the region linearly.  A message whose bytes wrap
    from the end of the region to its start is cut in two: its head is an unterminated tail segment (never emitted) and
    its tail, at offset 0, is emitted as a bogus fragment line.  The module claims 'wraparound is natural'."""
    size = 80
    r = RingGame(server, size, straddle=True)
    msgs = [f"message number {i:03d} here" for i in range(12)]    # 24+1 bytes each: wraps inside a message
    src, _ = make_src(server, layout="ring", size=size)
    drain(src)
    got = []
    for m in msgs:
        r.write(m)
        got += drain(src)
    assert got == msgs


# ============================================================================== torn / partial writes
@pytest.mark.parametrize("layout", ["scroll", "ring"])
def test_text_running_to_the_end_of_the_region_is_held_back_until_terminated(server, layout):
    size = 32
    src, _ = make_src(server, layout=layout, size=size)
    drain(src)
    server.write_ram(ADDR, b"x" * size)                                  # unterminated inside the region
    assert drain(src, 5) == []
    server.write_ram(ADDR, b"short line\x00" + b"\x00" * (size - 11))
    assert drain(src, 3) == ["short line"]
    src.close()


def test_limit_partial_write_followed_by_zero_fill_looks_like_a_complete_line_ring(server):
    """LIMIT-3: the 'never emit an unterminated segment' guard only works at the end of the region.  In a zero-filled
    region a half-written line is followed by NULs, so it is emitted as a (truncated) line, and again when completed."""
    src, _ = make_src(server, layout="ring", size=128)
    drain(src)
    server.write_ram(ADDR, b"You hit a rat for 5 po")
    first = drain(src, 3)
    server.write_ram(ADDR, b"You hit a rat for 5 points of damage.\x00")
    second = drain(src, 3)
    assert first == ["You hit a rat for 5 po"] and second == ["You hit a rat for 5 points of damage."]
    src.close()


@pytest.mark.parametrize("stable", [False, True])
def test_scroll_partial_last_line_then_completed_does_not_reemit_older_lines(server, stable):
    """BUG-9 (medium, ASSUMED scroll layout): if ANY poll observes the newest line half written (visible to every stable
    read, or stable_reads off), ScrollLayout loses its overlap with the previous window on the next poll -- the previous
    last line changed, so no suffix of old lines matches -- and re-emits EVERY older line in the window as new.
    In the DPS meter that double-counts all earlier damage lines still in the buffer."""
    cfg = make_cfg(server, size=256, stable=stable)
    cl = ScriptedClient(PineClient(cfg.pine))
    old = [f"You hit a rat for {d} points of damage." for d in (11, 12, 13, 14)]
    server.write_ram(ADDR, b"".join(enc(x) + b"\x00" for x in old))
    src = PineBufferSource(cfg, client=cl)
    drain(src)                                                              # prime: all four are history
    full = "You hit a rat for 15 points of damage."
    server.write_ram(ADDR, b"".join(enc(x) + b"\x00" for x in old) + enc(full) + b"\x00")
    torn = b"".join(enc(x) + b"\x00" for x in old) + enc(full[:20]) + b"\x00"
    cl.script = [torn, torn, torn]                                          # visible to all (<=3) reads of one poll
    got = drain(src, 4)
    assert not set(old) & set(got), f"older lines re-emitted as new: {got}"
    assert got.count(full) == 1


# ============================================================================== encodings
def test_utf16le_lines(server):
    data = b"".join(enc(t, "utf16le") + b"\x00\x00" for t in ["history", LINE, "Zoë hits a rat for 5 points of damage."])
    server.write_ram(ADDR, enc("seed", "utf16le") + b"\x00\x00")
    src, _ = make_src(server, encoding="utf16le", size=256)
    drain(src)
    server.write_ram(ADDR, enc("seed", "utf16le") + b"\x00\x00" + data)
    assert drain(src, 3) == ["history", LINE, "Zoë hits a rat for 5 points of damage."]
    src.close()


def test_shiftjis_lines(server):
    jp = "テストのメッセージ"
    src, _ = make_src(server, encoding="shiftjis", size=128)
    drain(src)
    server.write_ram(ADDR, enc(jp, "shiftjis") + b"\x00")
    assert drain(src, 2) == [jp]
    src.close()


def test_undecodable_bytes_do_not_raise(server):
    src, _ = make_src(server, size=64)
    drain(src)
    server.write_ram(ADDR, b"\xff\xfe\x80 garbage \x90\x00ok\x00")
    got = drain(src, 2)
    assert len(got) == 2 and got[1] == "ok"
    src.close()


# ============================================================================== iterator: heartbeats, ts, errors, close
def test_iterator_heartbeats_between_lines_with_monotonic_ts(server):
    src, _ = make_src(server, size=128, poll=0.005)
    out, stop = [], threading.Event()

    def reader():
        for item in src:
            out.append(item)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(0.15)
    server.write_ram(ADDR, b"alpha\x00")
    time.sleep(0.15)
    server.write_ram(ADDR, b"alpha\x00beta\x00")
    time.sleep(0.15)
    src.close()
    th.join(timeout=3)
    assert not th.is_alive()
    texts = [r for _, r in out if r != HEARTBEAT]
    assert texts == ["alpha", "beta"]
    assert sum(1 for _, r in out if r == HEARTBEAT) >= 10
    ts = [t for t, _ in out]
    assert ts == sorted(ts)


def test_iterator_ts_never_decreases_when_clock_steps_back(server):
    cfg = make_cfg(server, size=64, poll=0.0)
    clock_values = iter([100.0, 99.0, 50.0, 101.0, 100.5] + [102.0] * 50)
    src = PineBufferSource(cfg, client=PineClient(cfg.pine), clock=lambda: next(clock_values))
    ts = []
    for t, _ in src:
        ts.append(t)
        if len(ts) == 5:
            src.close()
    assert ts == sorted(ts)


def test_first_read_failure_raises_pine_error_with_hint():
    cfg = load_config()
    cfg.buffer.address = ADDR
    cfg.pine = PineConfig(slot=1, timeout=0.3)               # nothing listens on tcp/1
    src = PineBufferSource(cfg)
    with pytest.raises(PineConnectionError) as e:
        next(iter(src))
    assert "finding-the-buffer.md" in str(e.value) and "127.0.0.1:1" in str(e.value)
    src.close()


def test_no_vm_means_first_read_fails_loudly(server):
    server.has_vm = False
    src, _ = make_src(server)
    with pytest.raises(PineError):
        next(iter(src))
    src.close()


def test_mid_run_server_loss_keeps_heartbeating_and_recovers():
    with MockPineServer() as s1:
        port = s1.tcp_port
        s1.write_ram(ADDR, b"seed\x00")
        cfg = load_config()
        cfg.pine = PineConfig(slot=port, timeout=0.5)
        cfg.buffer.address, cfg.buffer.size, cfg.buffer.poll_interval = ADDR, 128, 0.01
        src = PineBufferSource(cfg)
        out = []
        th = threading.Thread(target=lambda: [out.append(x) for x in src], daemon=True)
        th.start()
        time.sleep(0.2)
    time.sleep(0.4)                                           # server gone
    n_down = len(out)
    assert th.is_alive() and src.last_error is not None
    with MockPineServer(port=port) as s2:
        s2.write_ram(ADDR, b"seed\x00after restart\x00")
        time.sleep(0.6)
        src.close()
        th.join(timeout=3)
    assert not th.is_alive()
    assert [r for _, r in out if r != HEARTBEAT] == ["after restart"]
    assert len(out) > n_down


def test_close_is_idempotent_and_stops_iteration(server):
    src, _ = make_src(server)
    src.close()
    src.close()
    assert list(src) == []


# ============================================================================== read-only guarantee
def test_whole_stack_never_sends_a_write_and_never_changes_ram(server):
    sent = []
    orig = server.handle_request

    def spy(payload):
        pos = 0
        while pos < len(payload):
            op = payload[pos]
            sent.append(op)
            pos += 5 if op in (0, 1, 2, 3, 4, 5, 6, 7) else 1
        return orig(payload)

    server.handle_request = spy
    server.write_ram(ADDR, b"seed\x00")
    before = hashlib.sha256(bytes(server.ram)).hexdigest()
    src, _ = make_src(server, size=512)
    drain(src, 5)
    c = PineClient(PineConfig(slot=server.tcp_port))
    c.read8(0), c.read16(0), c.read32(0), c.read64(0), c.read_bytes(3, 100), c.version(), c.title(), c.game_id()
    c.game_uuid(), c.game_version(), c.status()
    c.close()
    src.close()
    assert set(sent) <= {0, 1, 2, 3, 0x8, 0xB, 0xC, 0xD, 0xE, 0xF}, sorted(set(sent))
    assert server.rejected_commands == 0
    assert hashlib.sha256(bytes(server.ram)).hexdigest() == before


# ============================================================================== performance sanity + end to end
def test_poll_latency_for_default_4096_byte_buffer(server):
    src, _ = make_src(server, size=4096)
    t0 = time.perf_counter()
    for _ in range(20):
        src.poll_once()
    per_poll = (time.perf_counter() - t0) / 20
    src.close()
    assert per_poll < 0.5, f"{per_poll:.3f}s per poll (x2 with stable_reads)"


def test_engine_end_to_end_over_mock_pine(server, tmp_path):
    """Game simulator appends combat lines; PineBufferSource -> Engine -> fights, using wall-clock ts and heartbeats."""
    g = ScrollGame(server, 1024)
    g.append("old chat line")
    cfg = make_cfg(server, size=1024, poll=0.01, tmp=tmp_path, idle_timeout=0.6)
    src = PineBufferSource(cfg, client=PineClient(cfg.pine))
    closed = []
    eng = Engine(cfg, src, on_closed=closed.append)
    stop = threading.Event()
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.2)
    for amt in (10, 10, 10):                                   # three identical hits, polled between each
        g.append(f"You hit a rat for {amt} points of damage.")
        time.sleep(0.12)
    g.append("Your pet bites a rat for 7 points of damage.")
    time.sleep(1.0)                                            # > idle_timeout, with heartbeats only
    assert len(closed) == 1 and closed[0].you.hits == 3 and closed[0].pet.total == 7   # closed by heartbeat, not by end
    g.append("You hit a bat for 4 points of damage.")
    time.sleep(0.2)
    stop.set()
    src.close()
    th.join(timeout=3)
    assert not th.is_alive()
    assert [f.target for f in eng.closed_fights] == ["a rat", "a bat"]
