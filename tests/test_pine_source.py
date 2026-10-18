"""PineBufferSource + layouts. Synthetic buffers only; dedupe is UNVERIFIED against the real game."""
from __future__ import annotations

import pytest

from eqoa_parser.config import Config, load_config
from eqoa_parser.mock_pine import MockPineServer
from eqoa_parser.pine import PineClient, PineConnectionError
from eqoa_parser.pine_source import (
    BufferNotConfiguredError, PineBufferSource, RingLayout, ScrollLayout, split_segments,
)
from eqoa_parser.sources import HEARTBEAT, LogSource

ADDR, SIZE = 0x1000, 256
JP = "ゴブリンに5ダメージ"  # goblin-ni-5-damage, katakana/hiragana (SYNTHETIC)
CODEC = {"ascii": "ascii", "utf16le": "utf-16-le", "shiftjis": "shift_jis"}


def region(*lines, size=SIZE, enc="ascii"):
    term = b"\x00\x00" if enc == "utf16le" else b"\x00"
    data = b"".join(l.encode(CODEC[enc]) + term for l in lines)
    assert len(data) <= size
    return data.ljust(size, b"\x00")


scroll = region


# --- layout units ------------------------------------------------------------------------------------------------
def test_split_segments_all_encodings():
    assert [s.text for s in split_segments(b"one\r\ntwo\x00\x00three", "ascii")] == ["one", "two", "three"]
    for enc, text in (("utf16le", "one"), ("shiftjis", JP)):
        segs = split_segments(region(text, "two", size=64, enc=enc), enc)
        assert [s.text for s in segs] == [text, "two"] and all(s.terminated for s in segs)
    assert [s.terminated for s in split_segments(b"abc\x00def", "ascii")] == [True, False]


def make(layout_cls, enc="ascii", initial=False):
    return layout_cls(enc, initial)


def test_first_poll_is_history_unless_emit_initial():
    assert make(ScrollLayout).update(scroll("old1", "old2")) == []
    assert make(ScrollLayout, initial=True).update(scroll("old1", "old2")) == ["old1", "old2"]
    assert make(RingLayout).update(scroll("old1")) == []


@pytest.mark.parametrize("cls", [ScrollLayout, RingLayout])
def test_unchanged_rereads_yield_nothing(cls):
    lay = make(cls)
    lay.update(scroll("a hit b"))
    for _ in range(5):
        assert lay.update(scroll("a hit b")) == []


@pytest.mark.parametrize("cls", [ScrollLayout, RingLayout])
def test_appended_lines_yield_once(cls):
    lay = make(cls)
    lay.update(scroll("l1"))
    assert lay.update(scroll("l1", "l2")) == ["l2"]
    assert lay.update(scroll("l1", "l2")) == []
    assert lay.update(scroll("l1", "l2", "l3", "l4")) == ["l3", "l4"]


@pytest.mark.parametrize("cls", [ScrollLayout, RingLayout])
def test_repeated_identical_message_is_new(cls):
    lay = make(cls)
    lay.update(scroll("You hit a rat for 5"))
    assert lay.update(scroll("You hit a rat for 5", "You hit a rat for 5")) == ["You hit a rat for 5"]
    assert lay.update(scroll(*["You hit a rat for 5"] * 3)) == ["You hit a rat for 5"]


def test_scroll_window_shift_drops_old_lines_without_reemitting():
    lay = make(ScrollLayout)
    lay.update(scroll("a1", "b2", "c3"))
    assert lay.update(scroll("b2", "c3", "d4")) == ["d4"]
    assert lay.update(scroll("d4", "e5", "f6")) == ["e5", "f6"]


def test_scroll_truncated_front_line_still_overlaps():
    lay = make(ScrollLayout)
    lay.update(scroll("xx first", "second"))
    assert lay.update(b"first\x00second\x00third\x00") == ["third"]


def test_scroll_repeat_after_scroll_in_full_window():
    lay = make(ScrollLayout)
    lay.update(scroll("A", "B", "C"))
    assert lay.update(scroll("B", "C", "B")) == ["B"]  # repeat of an older message is still new


def test_scroll_documented_limit_full_window_identical_shift_is_missed():
    lay = make(ScrollLayout)
    lay.update(scroll("A", "B", "A", "B"))
    assert lay.update(scroll("A", "B", "A", "B")) == []  # genuinely two new lines, indistinguishable


def test_scroll_ignores_unterminated_tail_until_complete():
    lay = make(ScrollLayout)
    lay.update(b"one\x00")
    assert lay.update(b"one\x00tw") == []
    assert lay.update(b"one\x00two\x00") == ["two"]


def test_scroll_no_overlap_reemits_all_documented():
    lay = make(ScrollLayout)
    lay.update(scroll("a", "b"))
    assert lay.update(scroll("x", "y")) == ["x", "y"]


def test_ring_wraparound_and_cursor_order():
    size = 40
    lay = make(RingLayout)
    lay.update(bytes(size))
    r = bytearray(size)
    r[0:10] = b"line one\x00\x00"
    r[10:20] = b"line two\x00\x00"
    r[20:32] = b"line three\x00\x00"
    assert lay.update(bytes(r)) == ["line one", "line two", "line three"]
    # wrap: the game overwrites offset 0; leftovers of the older longer line stay behind and must be ignored
    r[0:10] = b"line 4\x00\x00\x00\x00"
    assert lay.update(bytes(r)) == ["line 4"]
    # two lines in one poll straddling the wrap: cursor was at offset 6 -> order continues from there
    r[10:20] = b"line 5\x00\x00\x00\x00"
    r[0:10] = b"line 6\x00\x00\x00\x00"
    assert lay.update(bytes(r)) == ["line 5", "line 6"]


def test_ring_repeated_identical_message_in_new_slot():
    lay = make(RingLayout)
    lay.update(bytes(32))
    r = bytearray(32)
    r[0:6] = b"hit 5\x00"
    assert lay.update(bytes(r)) == ["hit 5"]
    r[6:12] = b"hit 5\x00"
    assert lay.update(bytes(r)) == ["hit 5"]


def test_ring_documented_limit_identical_overwrite_missed():
    lay = make(RingLayout)
    lay.update(bytes(16))
    r = bytearray(16)
    r[0:6] = b"hit 5\x00"
    lay.update(bytes(r))
    assert lay.update(bytes(r)) == []  # same bytes rewritten at the same offset: invisible


@pytest.mark.parametrize("cls", [ScrollLayout, RingLayout])
def test_utf16_and_shiftjis_layouts(cls):
    lay = make(cls, "utf16le")
    lay.update(region(size=64, enc="utf16le"))
    assert lay.update(region("You hit a rat", size=64, enc="utf16le")) == ["You hit a rat"]
    lay = make(cls, "shiftjis")
    lay.update(region(size=64, enc="shiftjis"))
    assert lay.update(region(JP, size=64, enc="shiftjis")) == [JP]


# --- source over the real socket ---------------------------------------------------------------------------------
@pytest.fixture
def server():
    with MockPineServer(bytearray(1 << 16)) as s:
        yield s


def make_source(server, layout="scroll", clock=None, **buf):
    cfg = Config()
    cfg.pine.slot = server.tcp_port
    cfg.buffer.address, cfg.buffer.size, cfg.buffer.layout, cfg.buffer.poll_interval = ADDR, SIZE, layout, 0.01
    for k, v in buf.items():
        setattr(cfg.buffer, k, v)
    kw = {"client": PineClient(cfg.pine, platform="win32")}
    if clock:
        kw["clock"] = clock
    return PineBufferSource(cfg, **kw)


@pytest.mark.parametrize("layout", ["scroll", "ring"])
def test_source_end_to_end_polling(server, layout):
    src = make_source(server, layout)
    server.write_ram(ADDR, region("history line"))
    assert src.poll_once() == []  # history
    assert src.poll_once() == []  # unchanged re-read
    server.write_ram(ADDR, region("history line", "You hit a rat for 5"))
    assert src.poll_once() == ["You hit a rat for 5"]
    assert src.poll_once() == []
    server.write_ram(ADDR, region("history line", "You hit a rat for 5", "You hit a rat for 5"))
    assert src.poll_once() == ["You hit a rat for 5"]  # genuine repeat
    src.close()


def test_iteration_yields_lines_and_heartbeats_with_monotonic_ts(server):
    ticks = iter([5.0, 4.0, 6.0, 7.0, 8.0, 9.0])  # a clock stepping back must not reorder ts
    src = make_source(server, clock=lambda: next(ticks))
    server.write_ram(ADDR, region("old"))
    it = iter(src)
    first = next(it)
    assert first == (5.0, HEARTBEAT)  # first poll: history only -> heartbeat
    server.write_ram(ADDR, region("old", "new1", "new2"))
    got = [next(it), next(it)]
    assert [g[1] for g in got] == ["new1", "new2"] and got[0][0] == got[1][0]
    ts = [first[0], got[0][0]]
    assert ts == sorted(ts)
    nxt = next(it)
    assert nxt[1] == HEARTBEAT and nxt[0] >= ts[-1]
    src.close()
    assert list(it) == []  # close() stops the iterator


def test_is_a_logsource(server):
    src = make_source(server)
    assert isinstance(src, LogSource)
    src.close()
    src.close()


def test_address_none_raises_pointing_to_doc():
    with pytest.raises(BufferNotConfiguredError, match=r"docs/finding-the-buffer\.md"):
        PineBufferSource(Config())


def test_unknown_layout_rejected(server):
    with pytest.raises(ValueError, match="layout"):
        make_source(server, layout="nope")


def test_first_read_failure_is_loud():
    cfg = Config()
    cfg.buffer.address, cfg.pine.slot, cfg.pine.timeout = ADDR, 1, 0.2
    src = PineBufferSource(cfg, client=PineClient(cfg.pine, platform="win32"))
    with pytest.raises(PineConnectionError):
        next(iter(src))


def test_later_dropout_heartbeats_then_recovers(server):
    src = make_source(server)
    server.write_ram(ADDR, region("old"))
    it = iter(src)
    assert next(it)[1] == HEARTBEAT
    server.has_vm = False  # reads now FAIL
    assert next(it)[1] == HEARTBEAT and src.last_error is not None
    server.has_vm = True
    server.write_ram(ADDR, region("old", "back"))
    assert next(it)[1] == "back" and src.last_error is None
    src.close()


def test_stable_reads_rereads_torn_region(server):
    src = make_source(server)
    server.write_ram(ADDR, region("old", "comple"))  # a message being written
    reads = []
    real = src.client.read_bytes

    def torn(addr, n):
        reads.append(1)
        data = real(addr, n)
        if len(reads) == 1:  # the write finishes right after the first read
            server.write_ram(ADDR, region("old", "complete line"))
        return data

    src.client.read_bytes = torn
    assert src.poll_once() == []  # history
    assert len(reads) >= 3  # first read differed from the second, so it kept reading until two matched
    assert src.poll_once() == []  # the settled content was consumed as history, no half line leaked


def test_config_new_fields_load(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("buffer:\n  address: 0x1000\n  layout: ring\n  emit_initial: true\n  stable_reads: false\n", encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.buffer.layout, cfg.buffer.emit_initial, cfg.buffer.stable_reads) == ("ring", True, False)


def test_reexported_from_sources():
    from eqoa_parser import sources

    assert sources.PineBufferSource is PineBufferSource


# --- verification fixes (BUG-9 scroll growth, BUG-8 ring wrap) -------------------------------------------------------
def test_scroll_grown_newest_line_keeps_older_lines_quiet():
    lay = make(ScrollLayout)
    lay.update(scroll("a1", "b2"))
    assert lay.update(scroll("a1", "b2", "You hit a rat for 1")) == ["You hit a rat for 1"]  # observed half written
    assert lay.update(scroll("a1", "b2", "You hit a rat for 15 points")) == ["You hit a rat for 15 points"]
    assert lay.update(scroll("a1", "b2", "You hit a rat for 15 points")) == []


def test_ring_message_wrapping_past_region_end_is_reassembled():
    lay = make(RingLayout)
    ram = bytearray(24)
    lay.update(bytes(ram))
    data = b"hello wrapping world\x00"  # 21 bytes
    cursor = 10
    for b in data:
        ram[cursor] = b
        cursor = (cursor + 1) % 24
    assert lay.update(bytes(ram)) == ["hello wrapping world"]
    assert lay.update(bytes(ram)) == []
