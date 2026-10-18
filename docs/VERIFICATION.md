# Phase 1 verification (session D, independent)

Verifier: no access to the implementers' reasoning; everything below comes from `docs/CONTRACTS.md` and from running
the code. Date of run: 2026-10-17, Windows 11, Python 3.14.6, Tk 8.6, PyYAML 6.0.3, pytest 9.1.1.
No existing file was modified. New files: `tests/test_verify_core.py`, `tests/test_verify_pine.py`,
`tests/test_verify_ui.py`, this document. All data is SYNTHETIC; no real PCSX2 was involved; nothing was written to
emulator memory (the PINE client has no write opcodes, and a spy on the mock server confirmed every opcode sent).

**Verdict:** Phase 1 does what the contract says on the synthetic fixture and in the happy paths of every component.
The adversarial pass found **9 bugs** (none corrupts the happy path), all reproduced by failing tests that are
left failing on purpose (not xfailed). Most serious: ungraceful failure on bad configuration (BUG-1), the scroll layout
re-emitting old lines after a half-written line (BUG-9), and lost runs when two launches overlap (BUG-2).

---------------------------------------------------------------------------------------------------------------

## 1. Test suite

Baseline before my tests (`python -m pytest -q`, existing suite only):
```
........................................................................ [ 33%]
........................s............................................... [ 67%]
......................................................................   [100%]
SKIPPED [1] tests\test_pine_client.py:149: needs AF_UNIX (Linux/macOS)
213 passed, 1 skipped in 26.17s
```

Full suite including my tests (`python -m pytest -q`):
```
28 failed, 373 passed, 1 skipped in 58.24s
```
188 new tests: 160 pass, 28 fail. **Every failure is one of my new tests exposing a bug below.** The existing 213
tests still pass. Full, unabridged output (including all tracebacks) is saved in the shared notes folder:
`...\side-session-notes\local_0455ce0c-...\pytest_full_output.txt`. Failure list (the "short test summary info"):

```
SKIPPED [1] tests\test_pine_client.py:149: needs AF_UNIX (Linux/macOS)
FAILED test_verify_core.py::test_absurd_amounts_do_not_crash_the_engine[400|4301|5000]            BUG-4
FAILED test_verify_core.py::test_replaysource_out_of_order_line_is_flagged_or_clamped             BUG-6
FAILED test_verify_core.py::test_unmatched_entries_survive_str_splitlines[\x0b \x0c \x1c \x1d \x1e \x85    ]   BUG-5
FAILED test_verify_core.py::test_lone_surrogate_in_unmatched_line_does_not_crash                  BUG-3
FAILED test_verify_core.py::test_concurrent_launches_do_not_lose_each_others_runs                 BUG-2
FAILED test_verify_core.py::test_cli_live_with_unreachable_pine_fails_cleanly                     BUG-1
FAILED test_verify_core.py::test_cli_bad_inputs_exit_cleanly_without_traceback[7 cases]           BUG-1
FAILED test_verify_core.py::test_cli_rejects_non_finite_numbers[--idle-timeout nan|inf, --speed nan]   BUG-7
FAILED test_verify_pine.py::test_ring_straddling_message_is_reassembled                           BUG-8
FAILED test_verify_pine.py::test_scroll_partial_last_line_then_completed_does_not_reemit_older_lines[False|True]   BUG-9
28 failed, 373 passed, 1 skipped in 58.24s
```
Tests named `test_limit_*` pass: they pin down limits the code documents (they characterise, they do not endorse).

---------------------------------------------------------------------------------------------------------------

## 2. Bugs (most severe first)

Severity = effect on a user of the DPS meter, assuming the (unverified) real buffer behaves like the assumed layouts.

### BUG-1 (medium) — bad inputs end in raw tracebacks, or in silent nonsense, instead of `error:` + exit 2
`app.py:173-185` only wraps `make_config`/`make_source` in `except (OSError, ValueError, RuntimeError)`. Everything else
escapes: YAML syntax errors (`yaml.YAMLError` is not a `ValueError`; `config.py:93`), patterns problems (the Parser is
built later, inside `Runtime`/`Engine`, `app.py:120/157`), a missing replay file (opened lazily while iterating,
`sources.py:62`), and `--live` with an address set but PCSX2 unreachable (`PineTimeoutError` from the first poll,
`app.py:124`). `idle_timeout` from the config *file* is never validated (only the CLI flag is, `app.py:50`).
Repros (all with `--replay fixtures/synthetic_combat.log --no-gui`, bad thing in the config):
| input | observed |
|---|---|
| `idle_timeout: [unclosed` | `yaml.parser.ParserError` traceback, exit 1 |
| `patterns_file: nope.yaml` | `FileNotFoundError` traceback, exit 1 |
| a pattern with regex `(unclosed` | `ValueError: pattern 'x': bad regex` traceback, exit 1 |
| `--replay nope.log` | `FileNotFoundError` traceback, exit 1 (after the "run ... saving to" banner) |
| `idle_timeout: '8'` | processes the first fight, then `TypeError: '>=' not supported between 'float' and 'str'`, exit 1 |
| `idle_timeout: 0` or `-3` | exit 0, **22 fights** (every damage line is its own fight), no warning |
| `--live`, `buffer.address: 0x00100000`, nothing listening | `PineTimeoutError` traceback (the message inside is good and names the doc), exit 1 |
Contrast: an unknown config key correctly gives `error: unknown config key` and exit 2, so the intended behaviour exists.
Tests: `test_cli_bad_inputs_exit_cleanly_without_traceback`, `test_cli_live_with_unreachable_pine_fails_cleanly`.

### BUG-9 (medium, ASSUMED "scroll" layout) — one half-written line makes the next poll re-emit every older line
`pine_source.py:120-128` (`ScrollLayout._update`). Overlap detection needs the previous window's tail to equal the
current window's head. If a poll sees the newest line half written (followed by NULs, which is how a zero-filled region
looks), the next poll sees that same line complete, no suffix matches, and `return list(cur)` yields the **whole
window as new**: all earlier damage lines get counted again.
Repro (`test_scroll_partial_last_line_then_completed_does_not_reemit_older_lines`): window holds 4 lines (damage 11..14),
poll once (history); then the game writes line 5; the poll sees `"You hit a rat for 15"` (cut), the next sees the full
line. Emitted: `['You hit a rat for 15', '...11 points...', '...12...', '...13...', '...14...', '...15 points of damage.']`.
Fails for `stable_reads` on and off because `stable_reads` only helps if the torn state is not visible to two consecutive
reads (UNVERIFIED whether that holds on real hardware). The ring layout does not have this problem (it emits the fragment
and then the full line: LIMIT-3).

### BUG-2 (medium) — overlapping launches silently erase each other's runs
`persistence.py:82-89,155`: each `RunStore` loads the day file once and rewrites the whole cached document on every save.
Repro (`test_concurrent_launches_do_not_lose_each_others_runs`): open store A (e.g. a GUI left running), open store B
(a second launch, `--no-gui` replay), B saves a fight, then A saves a fight. The file ends up with only A's run; B's
data is gone, no warning. This contradicts "never silently overwrite good data". Same-second launches also get the same
`run_id` until one saves (ids are made unique only against what is already in the file).

### BUG-8 (medium-low, ASSUMED "ring" layout) — a message that wraps past the end of the region is corrupted
`pine_source.py:139-151` treats the region linearly. A message whose bytes continue at offset 0 leaves an unterminated
head at the end (never emitted) and a bogus tail at the start. Repro (`test_ring_straddling_message_is_reassembled`,
80-byte ring, 12 messages written byte-wise with wraparound): output contains `'number 003 here'` and `'06 here'` in place of
`'message number 003 here'` / `'message number 006 here'`. For a damage line that is a lost hit (the fragment lands in
`unmatched.log`), about once per ring cycle. The module docstring says wraparound is "natural".

### BUG-4 (low) — absurd damage amounts crash the engine
`parser.py:175` `int(g["amount"])` raises `ValueError` for more than 4300 digits (Python's int-string limit);
`stats.py:123/135` `total / eff` raises `OverflowError` for amounts of roughly 309+ digits. Repro:
`You hit a rat for 99999...(400 digits) points of damage.` through `Engine.run` raises `OverflowError`; with 5000 digits,
`ValueError`. The exception propagates out of `Engine.run` (console exits with a traceback; the GUI worker shows it in the
status line and stops processing). Test: `test_absurd_amounts_do_not_crash_the_engine`.

### BUG-3 (low) — a lone surrogate in an unmatched line crashes the parser
`parser.py:182` writes with `encoding="utf-8"`: `Parser.parse(1.0, "garbled \ud800 text")` raises `UnicodeEncodeError`.
Not reachable from the current sources (replay uses `errors="replace"`, buffer decode uses `errors="replace"`), but any
new source or user-supplied `Parser` input hits it. Test: `test_lone_surrogate_in_unmatched_line_does_not_crash`.

### BUG-5 (low) — `unmatched.log` entries can still span several lines for `str.splitlines()` readers
`parser.py:118` escapes only `\r \n \t`. Raw text containing `\x0b \x0c \x1c-\x1e \x85    ` stays in the file,
and `read_text().splitlines()` (the method `tests/test_contracts.py` uses to count entries) splits it. Plain
`for line in file` iteration and counting `\n` are fine (that part is verified: one entry per line for 18 odd inputs).
Garbage decoded from utf-16 noise can contain these characters. Test: `test_unmatched_entries_survive_str_splitlines`.

### BUG-6 (low) — `ReplaySource` neither enforces nor flags the non-decreasing-ts rule
Protocol (`sources.py:22`, CONTRACTS "sources") says non-decreasing ts; `sources.py:62-81` passes a regressing ts through
silently. Repro: lines at ts 1000, 1008, 1001 give fights `[1000]`, `[1008, 1001]`: the late line is attached to
the wrong fight (no crash, durations stay >= 0). Test: `test_replaysource_out_of_order_line_is_flagged_or_clamped`.

### BUG-7 (low) — CLI accepts `nan`/`inf`
`app.py:28,32,46,50` use `type=float` with `< 0` / `<= 0` checks, which `nan` passes. Observed: `--speed nan` hangs
forever (killed after 10 s, exit 124); `--idle-timeout nan` and `inf` both yield one fight for the whole file (exit 0).
`--speed inf` works (same as 0). Test: `test_cli_rejects_non_finite_numbers`.

### Observations (not bugs under the contract; no failing test)
* O-1: `PineBufferSource` swallows every PINE error after the first successful read (`pine_source.py:226-229`) and
  keeps heartbeating; `last_error` is never read by `app.py` or the UI (`grep last_error`). If PCSX2 is closed mid-session
  the meter just goes quiet with no indication. Reconnect itself works (tested: server stopped, new server on the same port,
  new lines arrive).
* O-2: the existing `tests/test_cli.py::test_real_replay_end_to_end_no_gui` appends to the repo's real `unmatched.log` on every
  pytest run (4504 -> 5146 bytes during my session). The file is gitignored, but the test pollutes it.
* O-3: a run with zero closed fights is never written (`persistence.py:155` saves only on `add_fight`). Consistent with
  "after every closed fight"; just be aware an empty launch leaves no trace.
* O-4: `Engine.step` (`pipeline.py:69-72`) treats unmatched/ignored lines as clock ticks (like heartbeats). Sensible, but
  it is in the code comment only, not in CONTRACTS.
* O-5: ReDoS-ish cost: a 100 000-character line `"Bob's " + "hits "*20000` takes 13 s in the `other_ability` pattern
  (20 000 chars: 0.5 s; ordinary long lines with no match: milliseconds). Bounded in practice by `buffer.size` (4096 by default).
* O-6: closing stdout early (`... | head`) gives an `OSError: [Errno 22]` traceback on Windows (console UI does not handle a
  broken pipe).
* O-7 (code reading only, not run): the GUI live table height is fixed at `2 + 3` rows with `show_group`
  (`tk_app.py:107`); more than three group actors would need scrolling.
* O-8: CONTRACTS asks for "top 3 abilities" in the fight list; the GUI shows them in a detail line for the selected
  fight (`tk_app.py:171-177`), not as a column. The console shows them per fight. Judged acceptable, noted as a loose reading.

---------------------------------------------------------------------------------------------------------------

## 3. Contract section by section

| Section | Result | Evidence |
|---|---|---|
| events | PASS | fields/order, frozen, slotted (`test_event_is_frozen_and_slotted`, `test_contracts`) |
| config | PASS with gap | defaults, unknown keys, hex address, null sections, `resolve` OK (`test_config_edge_cases`). Gap: no value validation (BUG-1 `idle_timeout`) |
| sources / ReplaySource | PASS with gap | format, comments, BOM, CRLF, bad utf-8, malformed lines (5 warnings), identical lines yielded each time, heartbeats monotonic and bounded, stop/close prompt. Gap: BUG-6, BUG-7 (`--speed nan`) |
| PineBufferSource | PASS happy path; BUG-8, BUG-9 | see section 5 |
| PINE client | PASS (mock) | read-only proven three ways (no write identifiers via AST, opcode set `{0-3, 8, B-F}`, wire spy: `rejected_commands == 0`, RAM hash unchanged). Spec cross-check: a summarising fetch of `PINE.cpp` agreed on opcodes (Read 0-3, Write 4-7, Version 8, SaveState 9, LoadState 0xA, Title..Status 0xB-0xF), MAX_IPC_SIZE 650000, MAX_IPC_RETURN_SIZE 450000, OK=0 / FAIL=0xFF, length prefix including itself, Windows TCP loopback on the slot, Unix path + `.<slot>` suffix only when slot != 28011. Not read line by line by me |
| parser | PASS with gaps | strip + fullmatch, first wins, normalisation (article lower-casing, you/yourself/pet/pet_names/player_name, case-insensitive), ignore, heartbeat, unmatched format and immediate flush; BUG-3/4/5 |
| segmenter | PASS | exactly-8.0 gap -> new fight, 7.99 joins, misses/deaths never extend, overlap = one fight with several targets, ids from 1 |
| stats | PASS | independent hand recomputation of fixture fights 1 and 3 matched (you 82/28 pet, Bob 34, primary target pup 62 vs scout 56); min 1.0 s DPS floor, tie rules, zero-fight run summary |
| pipeline | PASS | callback order (closed before live of the new fight), stop mid-run flushes + finishes once, idempotent finish, pre-set stop, stop from another thread, heartbeat-only stream |
| persistence | PASS with BUG-2 | schema, run_id format, atomic write (failure keeps old file, no temp left), corrupt/empty/truncated/array/future-schema/BOM/non-utf8/directory all quarantined, bad fight entries kept byte-for-byte, sequential launches append and reload |
| app / UI / CLI | PASS with BUG-1/7, O-7, O-8 | all flags, exactly-one-source, `--live` without address (see 4), GUI launch/close, widget contents driven by the real Engine |
| zero UI imports | PASS | existing dynamic test + my static AST scan including function-local imports for all 12 core modules |
| requirements.txt | PASS | contains `PyYAML>=6.0` and `pytest>=8.0`; the AST scan of all of `eqoa_parser/` finds `yaml` as the only third-party import; a fresh venv with only PyYAML runs the replay CLI and imports `mock_pine`, `recon`, `ui.tk_app`. `pyproject.toml` dependencies match |

Deviations from the contract, with locations: BUG-1 (`app.py:173-185`, `config.py:93`, no validation in `config.py:116`),
BUG-2 (`persistence.py:82-89,155`, "never silently overwrite good data"), BUG-6 (`sources.py:62-81`), BUG-8/9
(`pine_source.py:120-151`, "dedupe ... without dropping/duplicating"), O-8 (`tk_app.py:171`). Everything else matched.

---------------------------------------------------------------------------------------------------------------

## 4. End-to-end runs (real commands, real output, abridged)

Setup: a config in my scratch dir pointing `unmatched_log` at scratch (so the repo's `unmatched.log` stays untouched), and
`--runs-dir` in scratch.

**Replay, twice, `--no-gui`** (`python -m eqoa_parser --config cfg.yaml --replay fixtures/synthetic_combat.log --no-gui --runs-dir runs`):
```
[replay:fixtures/synthetic_combat.log] run 20261017T125613; 0 fight(s) from earlier runs today; saving to ...\runs\2026-10-17.json
...
=== Fight 1: a decaying skeleton - 6.0s ===
  You        total     82  dps    13.7  max hit    40
  Pet        total     28  dps     4.7  max hit    11
  Combined   total    110  dps    18.3  (damage taken 8)
  Top abilities: melee (you) 42, Fire Bolt (you) 40, melee (pet) 28
...
=== Run summary ===
  Fights: 5   Total damage: 274   Average DPS: 16.6   Best fight: fight 2 vs a fire beetle at 55.0 dps
```
Both runs exit 0, stderr empty; second banner says `run 20261017T125613-2; 5 fight(s) from earlier runs today`; the outputs
after the banner are identical. JSON: `schema_version 1`, `date 2026-10-17`, two runs with distinct ids, `started_at` ISO with offset,
`source` `replay:fixtures/synthetic_combat.log`, each with 5 fights whose keys are exactly `FightSummary.to_dict()`; sums 274; zero-duration fights
have `duration 0.0`. Unmatched log: 3 entries per launch (6 after two). All numbers match the hand-computed oracle in
`fixtures/synthetic_combat.expected.json` (and my own recomputation of two fights).

**`--show-group`:** live lines gain `| Bob 14 (7.0 dps, max 14)`, fight 3 gains `Bob total 34 dps 4.5 max hit 20`; without the flag `Bob` never appears in
stdout, but the JSON always stores group actors.

**`--idle-timeout`** (reasoning first): gaps between damage events in the fixture are 8.0 (fight 1 to 2), 16.0, 22.5 and 10.0; the largest gap
inside a fight is 2.0. So 3 behaves like 8 (5 fights); 20 merges 1+2+3 (gaps 8 and 16 < 20; 22.5 splits) and 4+5 (gap 10) -> 2 fights, durations 37.5 s and 10.0 s;
8.01 merges only 1+2 (4 fights); 1.9 splits further (> 5).
Observed: timeout 3 -> `Fights: 5`; timeout 20 -> `Fights: 2, Total damage: 274, Average DPS: 5.8, Best fight: fight 1 ... 6.6 dps`
(Fight 1 37.5 s, Fight 2 10.0 s). Total damage is conserved. Encoded as `test_cli_idle_timeout_changes_segmentation_as_reasoned`.

**`--live` with no buffer address:**
```
error: --live needs buffer.address in the config, and it is not set. The EE RAM address of the chat/combat buffer is unknown until you locate it; see docs/finding-the-buffer.md.
exit=1
```
No traceback, nothing on stdout, no runs dir created. (`--live` + `--replay`, or neither: usage error, exit 2.)
With an address set and no emulator: traceback (BUG-1).

**GUI** (`--replay ... --speed 20`, scratch runs dir): window `EQOA Parser` appeared and stayed up; after posting `WM_CLOSE` the process exited with code 0, empty stderr,
and the fights were saved (5 when left to run 6 s). Closing mid-replay at 4 s (speed 20: 3 fights saved) and at speed 1 (1 fight saved)
also exited cleanly, with the in-progress fight flushed to the runs file. Test `test_real_engine_drives_window_and_second_launch_marks_previous`
drives the real Engine into a Tk window: 5 rows newest first, correct per-fight numbers, run label, and on a second launch 10 rows (5 `now` + 5 `prev`).

---------------------------------------------------------------------------------------------------------------

## 5. Edge cases attacked

| Case | Result |
|---|---|
| two mobs interleaved | PASS one fight, targets in first-hit order, primary by total |
| second mob pulled at +7.99 / +7.999999 / +8.0 / +8.01 | PASS 1 / 1 / 2 / 2 fights |
| timeout measured from last damage of *any* mob | PASS |
| miss/death at 7.9 / exactly 8.0 after last damage | PASS neither extends; the 8.0 death closes the fight and is not attached |
| heartbeat closes at exactly 8.0 and not at 7.999 | PASS |
| single hit; many hits same ts; 0.5 s fight | PASS durations 0.0 / 0.0 / 0.5, DPS floor of 1 s applied |
| run summary of only zero-duration fights | PASS average = total / number of fights |
| fight with only damage taken; amount 0; 1 point | PASS target None, dps 0, round-trips through JSON |
| huge amounts | PASS up to 300 digits; BUG-4 beyond |
| unmatched: empty / whitespace / tab / HEARTBEAT | PASS not logged |
| unmatched: 18 odd lines (emoji, kana, NUL, tabs, CR, LF, CRLF, 5000 chars, BOM, near-misses) | PASS one physical line each, flushed immediately, appended not truncated |
| unmatched: other Unicode line separators | FAIL BUG-5; lone surrogate FAIL BUG-3 |
| near-miss lines (no period, trailing junk, missing "for", comma amount "1,234", wrong case) | PASS produce no event |
| replay file: duplicate identical lines, same ts, 4 copies | PASS counted 4 times |
| replay file: BOM, CRLF, bad utf-8, bad/nan/inf ts, empty text field, indented `#` | PASS (5 stderr warnings, no crash) |
| empty / comment-only replay | PASS zero fights, run summary "No fights" |
| pet + player same second, same exact ts, both orders | PASS identical results, tie order `you` before `pet` |
| out-of-order timestamps | no crash, damage conserved, durations >= 0; BUG-6 (mis-merge, no warning) |
| heartbeat-only stream (2000 beats) | PASS no fights, nothing in unmatched.log, `on_finished` once |
| Engine stop mid-replay / pre-set / from another thread | PASS fight flushed, finish once, thread ends within 3 s |
| real-time replay: idle fight closes via heartbeat before the next line | PASS (30 file-seconds at 20x: closed at ~0.4 s real, not 1.5 s) |
| persistence: corrupt, empty, truncated, array, scalar, future schema, non-utf8, directory, BOM | PASS quarantined (original bytes kept), warning, fresh file |
| persistence: two sequential launches / same-second ids / 3 corrupt files same second | PASS |
| persistence: concurrent launches | FAIL BUG-2 |
| **PineBufferSource** vs mock PINE | |
| unchanged memory polled 60x (both layouts, stable on/off) | PASS yields nothing |
| `emit_initial` | PASS history once only |
| appended lines (25 lines x 3 polls) | PASS each exactly once, in order |
| identical combat line appended repeatedly / 3 at once | PASS (scroll and ring) while the region is not full |
| scroll window continuously scrolling, random batches 0-3, 400 polls | PASS exactly once, in order |
| scroll, 4-line alphabet of repeated damage lines, full window | PASS |
| scroll, window of only identical lines | LIMIT-1 (documented): 5 genuine messages, 0 seen |
| ring wraparound without straddle, random batches | PASS exact once, in order |
| ring, byte-identical text rewritten in the same slot | LIMIT-4 (documented): 12 genuine, 4 seen |
| ring, message straddling the wrap point | FAIL BUG-8 |
| unterminated text running to the end of the region | PASS held back until terminated |
| half-written line followed by NULs | LIMIT-3: emitted as a fragment; scroll additionally BUG-9 |
| torn read seen by first read only (stable_reads) | PASS fragment never emitted |
| utf-16le, Shift-JIS, undecodable bytes | PASS |
| iterator: heartbeats, non-decreasing ts (also when the clock steps back), close idempotent | PASS |
| first read fails (nothing listening / no VM) | PASS `PineConnectionError` with hint; mid-run server loss: keeps heartbeating then recovers (O-1: silent) |
| 4096-byte poll latency against the mock | PASS well under 0.5 s |
| full stack over mock PINE (game simulator -> source -> Engine, wall clock, idle_timeout 0.6) | PASS 2 fights, first closed by heartbeat |

---------------------------------------------------------------------------------------------------------------

## 6. Still unverified against the real game (and anything presented as fact?)

I found nothing presented as fact that is not: `patterns.yaml` is headed SYNTHETIC with every pattern `ASSUMED`; module
docstrings in `pine_source.py`/`pine.py`/`mock_pine.py`/`recon.py` mark dedupe, layouts, read semantics UNVERIFIED. The one claim I
could not check at all is the menu path in `docs/finding-the-buffer.md` (PCSX2 Settings -> Advanced -> PINE Settings), which cites a `.ui`
file and says wording may differ.

Unverified (cannot be verified without a real PCSX2 + EQOA + Sandstorm):
1. Every combat message wording, punctuation, number formatting (commas? `1,234` is unmatched today), capitalisation, article use, pet naming,
   how incoming damage names a pet, whether group members appear, whether misses/deaths are worded as assumed.
2. The text encoding of the buffer (ascii / utf-16le / Shift-JIS), terminators (NUL / CR / LF / something else), timestamps in the text.
3. The EE RAM address of the buffer; whether it is a scrolling window, a ring, fixed slots, or something else; whether the write
   is atomic from a poller's point of view (torn reads: BUG-9, LIMIT-2, LIMIT-3 depend on this); whether wrap straddles messages (BUG-8).
4. Real PINE behaviour: reads of unmapped addresses (fail vs garbage), consistency of a 512-op batch while the game runs, whether
   one batch is atomic against emulation, latency at the configured poll interval, reply limits under load, the Linux/macOS UNIX-socket
   path (the AF_UNIX test is skipped on this Windows machine), PINE enable steps in the current PCSX2 build.
5. Whether chat/combat text is in memory at all as plain strings (could be IDs, compressed, or rendered glyphs).
6. Fight semantics against real play: whether 8 s is a sensible idle timeout, DoT/ticks, pet damage attribution, reflect/absorb text, how
   death/kill lines appear (the fixture's `You have slain ...` is invented), multiple characters with the same name.
7. Real-time behaviour of the GUI under a live feed (only replay at 1x-20x was exercised), always-on-top behaviour in game/fullscreen,
   DPI scaling, behaviour on Linux/macOS Tk.
8. Wall-clock `ts` in live mode (ts = time of discovery, so DPS inherits poll-interval jitter and batches of lines sharing one ts).

## 7. How to reproduce

```
python -m pytest -q                                   # 28 failed, 373 passed, 1 skipped
python -m pytest -q tests/test_verify_core.py tests/test_verify_pine.py tests/test_verify_ui.py
python -m pytest -q tests/test_verify_core.py -k "bad_inputs or concurrent or absurd"   # BUG-1/2/4 only
```


---------------------------------------------------------------------------------------------------------------
---------------------------------------------------------------------------------------------------------------

# Phase 2 verification: packet source (session D, independent)

Date of run: 2026-10-17 (evening), Windows 11, Python 3.14.6. Scope: `eqoa_parser/net/{pcapng,protocol,attribution,sources,capture}.py`,
Event support in `pipeline.py`, `--pcap` / `--live` / `--live-pine` / `--connection` in `app.py`, against
"Phase 2: packet source" in `docs/CONTRACTS.md`, with the real captures and oracle in `fixtures/real/`.
No existing file was modified. New files: `tests/test_verify_net.py`, `tests/test_verify_net_capture.py`,
`tests/test_verify_net_app.py` and this section. My test builders are my own (not imported from `tests/_net_builders.py`).
**No raw socket was opened**: `--live` was exercised only with a fake `RawCapture` patched into `eqoa_parser.net.capture`,
and `RawCapture` with fake sockets; the real `_open_socket` was called only for unsupported platforms (it raises before touching a socket).
The one live-adjacent call I made was `detect_interface_ip()` (a UDP `connect()`, sends nothing): it returned `192.168.1.156`.

**Verdict.** The packet path is in good shape: on both real captures the decoder reproduces the oracle exactly with no extra records, attribution
reproduces every acceptance number in the contract, and 63,000+ mutated inputs plus a seeded fuzz test raise nothing. The attack pass found
**5 bugs** (none wrong on the real captures) and 7 inherent risks of the INFERRED design. Most relevant before the live test:
BUG-N2-5 (idle detection in live mode depends on socket read timeouts) and BUG-N2-3 (a non-pcapng file gives a traceback).

## P2.1 Test suite

Before my Phase 2 tests (everything else, including my Phase 1 tests, which now pass after the fixes): `544 passed, 1 skipped`.

After (`python -m pytest -q`, full output saved to the shared notes folder as `pytest_phase2_full_output.txt`):
```
SKIPPED [1] tests\test_pine_client.py:149: needs AF_UNIX (Linux/macOS)
FAILED tests/test_verify_net.py::test_pcapng_snaplen_truncated_frame_is_not_trusted                              BUG-N2-1
FAILED tests/test_verify_net.py::test_pcap_replay_events_are_time_ordered_even_if_capture_timestamps_are_not     BUG-N2-2
FAILED tests/test_verify_net.py::test_live_idle_fight_closes_even_while_the_server_keeps_sending_non_combat_datagrams   BUG-N2-5
FAILED tests/test_verify_net_app.py::test_cli_pcap_not_a_pcapng_is_a_clean_error[garbage]                        BUG-N2-3
FAILED tests/test_verify_net_app.py::test_cli_pcap_not_a_pcapng_is_a_clean_error[text]                           BUG-N2-3
FAILED tests/test_verify_net_app.py::test_cli_net_config_values_are_validated[local_port: 99999]                 BUG-N2-4
FAILED tests/test_verify_net_app.py::test_cli_net_config_values_are_validated[local_port: 0]                     BUG-N2-4
FAILED tests/test_verify_net_app.py::test_cli_net_config_values_are_validated[server_ip: not-an-ip]              BUG-N2-4
FAILED tests/test_verify_net_app.py::test_cli_net_config_values_are_validated[server_ip: '']                     BUG-N2-4
9 failed, 726 passed, 1 skipped in 85.21s (0:01:25)
```
191 new tests (182 pass, 9 fail = 5 distinct bugs). All other tests, old and new, pass. Docstrings starting `BUG-N2-x` are the failing ones (deliberately not xfailed);
`RISK-n` tests pass and pin down inherent properties of the INFERRED design.

## P2.2 Bugs (most severe first)

### BUG-N2-5 (medium-low) — in live mode the idle clock only advances when `capture.read()` times out or sees an unrelated packet
`net/sources.py:78-91` (`LivePacketSource.__iter__`): a heartbeat is yielded only when `cap.read()` returns `None`. A server datagram with no combat record
returns a datagram, `events_from` yields nothing, and **no item is yielded, so the segmenter clock does not advance**. The server sends continuously
(cap2: median gap 0.06 s on the necro connection, 0.11 s on the bard one; 84 of 452 bard-connection gaps exceed the 0.25 s poll interval). While packets keep
arriving, a finished fight is not closed (live panel keeps "Fight N ... ongoing", the fight is not saved or added to the run summary) until a quiet gap happens,
unrelated traffic arrives (also returns `None`), or the next hit. Typical effect: late closing by seconds; worst case: no closing during a busy stretch.
The same happens when the *other* emulator's connection is the busy one.
Repro (`test_live_idle_fight_closes_even_while_the_server_keeps_sending_non_combat_datagrams`): fake capture returns one hit, then a non-combat server datagram every
10 ms; `idle_timeout=0.4`; 1.1 s after the hit the fight is still open (`assert 0 == 1`). Fix idea: tick on every datagram (or from a wall-clock timer), not only on timeouts.

### BUG-N2-3 (medium) — a file that is not pcapng ends in a raw traceback
`net/pcapng.py:103` raises `ValueError("not a pcapng file: ...")` lazily, while the engine iterates; `app.main` catches `OSError`, `PineError`, `CaptureError` around the run
(`app.py:313-318`) but not `ValueError`, so the user sees the traceback and exit 1 after the run banner (the Phase 1 amendment promised `error:` + exit 2).
Repro: `python -m eqoa_parser --pcap notes.txt --no-gui` (any non-pcapng file, e.g. a classic `.pcap` or a log) -> `ValueError: not a pcapng file: missing section header block`, exit 1.
A GUI launch with the same file shows the error in the window (`work()` catches it), so only the console is affected. Related silent cases (not failures): an empty file,
a file truncated mid-block, or a capture with no server traffic all end with "No fights." and exit 0 with nothing on stderr (`test_cli_pcap_with_no_game_traffic_or_truncated_file_says_nothing_but_no_fights`).

### BUG-N2-4 (low) — `net` config values are not validated
`load_config` checks types and `net.poll_interval > 0` but not `net.local_port` range or IPv4 syntax of `server_ip`. `local_port: 99999` or `0`, `server_ip: not-an-ip` or `''`
are accepted; the run then ends with "No fights." and exit 0, because nothing can match. (`--connection` itself is validated 1-65535 by argparse.)
Repro: config with `net:\n  local_port: 99999` and `--pcap fixtures/real/cap1_necro_spells.pcapng --no-gui`.

### BUG-N2-1 (low) — snap-length-truncated frames are decoded as complete datagrams
`net/pcapng.py:46-48,50` clamps to the bytes present (`min(len(data), total)`) and never compares `caplen` with the original length, so a capture taken with a snaplen yields a
shortened payload whose last 4 bytes are then taken as the "checksum". `net/capture.py`'s own parser drops such packets as truncated (`ulen > len(seg)`), so the two readers disagree.
(`pktmon --pkt-size 0`, which the docs use, captures whole packets, so the documented route is unaffected.) Test: `test_pcapng_snaplen_truncated_frame_is_not_trusted`.

### BUG-N2-2 (low) — `PcapReplaySource` can yield decreasing timestamps
The Phase 1 amendment clamps regressing ts in `ReplaySource` and warns; `PcapReplaySource` yields Events in capture order with their own datagram time. Capture files whose records
are slightly out of order (pktmon writes one record per component) give a regressing `(ts, Event)` sequence; the segmenter tolerates it, but the LogSource contract (non-decreasing ts) is not met.
Both real fixtures are monotonic (`test_real_capture_events_are_in_non_decreasing_time_order` passes). Test: `test_pcap_replay_events_are_time_ordered_even_if_capture_timestamps_are_not`.

## P2.3 Inherent risks of the INFERRED design (tests pass; they document behaviour the contract chose)

* RISK-1 scan false positives: after an unrecognised message the scan resumes at the next byte, so a valid-looking `fb ... db 00 ...` record inside an unknown message body or in the
  unparsed header region is decoded as real (`test_risk_scan_decodes_a_phantom_message_inside_an_unknown_message`). Decoded messages are skipped as a unit, so a `0xFB` inside a decoded record
  (source id, amount, extra bytes) is never mis-framed (tested). Measured on the real captures: 465 `0xFB` bytes in server payloads of cap2, 436 pass the length check, and the decoder's output
  equals the oracle with **zero extra records**; their opcodes are mostly the known `db`/`b1`/`cd` plus others (`0xa7b`, `0x60`, `0x7e0`, `0x13`, ...), which suggests the framing is real, not coincidence.
* RISK-2 player id: the first entity with `id & 0xFFFF == header_low16` wins; an NPC with the same low 16 bits acting before the player's first record is taken for "you" (1/65536 per entity).
* RISK-3 pet id: any non-player attacker that swings at T within 1.0 s before the player's extended hit at T becomes "the pet" (a group member's swing qualifies); hostile hits on that member then show as damage taken by "pet".
* RISK-4/RISK-7 `otherwise -> damage`: a mob hitting a group member or an unrecognised pet, and any combat between unrelated entities the server may send, becomes a hex-named `damage` event. It starts/extends **your** fight
  (idle never fires while someone nearby fights) and the primary-target logic can flip the fight's target to the victim.
* RISK-5 connection: auto-selection follows the first connection with an HpChange forever; if the client reconnects from a new source port (or you close one emulator and start another) every later event is dropped and nothing is printed.
  An explicit `--connection` that never appears also yields nothing silently. `conn` is the client port only (two client IPs using the same port would merge).
* RISK-6 an amount-0 record from a mob at the player (a resist/absorb or non-damage action, if the server sends those) starts a fight with 0 damage (contract: 0 counts as a hit).
* The pet is never re-learned after the first one (new pet after death/re-summon: its hostile damage is mis-labelled); entity names and abilities are not decoded (targets show as `0x01312F0A`, abilities as `(unknown)`).

## P2.4 Contract section by section

| Section | Result | Evidence |
|---|---|---|
| Datagrams / `read_pcapng` | PASS with BUG-N2-1/2/3 | LE and BE sections (also mixed in one file), link types 1/101/228, VLAN and QinQ, IP options, link padding, UDP length shorter/longer than IP, tsresol 1e-3/1e-6/1e-9/2^-10, unknown link types (0, 12, 113, 127), IPv6, TCP, ARP, fragments (MF and offset) skipped while DF is not, unknown iface index, caplen 0/oversize, SPB/statistics/unknown blocks, IDB option garbage, truncated tail and bad block lengths (0, 3, 11, 13, 2^32-1) keep earlier datagrams, pktmon duplicate window (<50 ms vs last kept) |
| Protocol / framing | PASS | every truncation of a valid datagram, length byte lies, message must end before the 4 checksum bytes, varints (0, +-1, boundaries, 2^31, overlong, unterminated, non-canonical `80 00`, varint ending at the checksum boundary), unknown/short bodies skipped, 60,000 `0xFB` bytes decode in 0.04 s |
| Protocol / dedupe | PASS | same seq+body resent (same datagram and later) dropped; same seq + different body kept; same body + different seq kept; per connection; only server->client; 120 s memory window; seq wraparound; table stays bounded (< 600 entries over 5000 messages) |
| Oracle | PASS | both captures: every record (conn, header, seq, source, amount, target, extended) equal and in order; event time within 2 ms of the oracle's offsets |
| Attribution | PASS with RISK-2..7 | bard: 56 damage = 20,670 (you), 56 damage_taken of 21, 56 heal of 300; necro connection 27 x 25 + 9 x 0, all `pet`, pet id 0x01374606, no damage_taken; cap1: 598 598 599 599 599 444 599 599 598 460 460 467 598 in order (the user's numbers). Player id learned late / never / header change reset; pet window boundaries 0, 0.999, 1.0 (learned) vs 1.001, negative (not) |
| Heal events | PASS | never start a fight, never extend one (7.9 s inside the window changes nothing), not counted anywhere in totals; a heals-only stream through the Engine creates no fights |
| Connections | PASS with RISK-5 | interleaved connections isolated; auto-select logs once to stderr; `--connection` either way; swing-only connection not selected |
| Sources / `PcapReplaySource` | PASS with BUG-N2-2 | speed 0: no heartbeats, event ts; speed>0: bounded monotonic heartbeats and an idle fight closes at ~0.27 s real (8 file-s at 30x), not at the next event; `close()` stops a 5000 s gap promptly |
| `LivePacketSource` | PASS with BUG-N2-5 | events and heartbeats in order; `close()` from another thread unblocks in < 1 s and closes the capture (idempotent); startup `CaptureError` surfaces on first `next()`; error after startup surfaces *and* the engine still flushes the fight and fires `on_finished`; garbage / non-server / empty datagrams harmless |
| `RawCapture` / IPv4 parsing (fakes only) | PASS | both directions, options (IHL 6), total length 0, trailing bytes, UDP length lies, v5/v6/short/IHL<5, checksums unchecked as documented, fragments in any order / duplicated / interleaved / holes / expiry at 5 s / bounded at 256 pending / foreign hosts never stored, stats add up, permission errors map to Administrator (win32) or root/CAP_NET_RAW (linux) messages, unsupported platforms fail before any socket, no send API anywhere in the module |
| app / CLI | PASS with BUG-N2-3/4, O-N2-1..3 | flag combinations (exactly one of `--pcap/--live/--replay/--live-pine`), `--connection` validated 1-65535, `--live` not admin -> `error:` exit 2 with `docs/live-capture.md`, no banner, no runs dir; mid-run capture error -> exit 2 and the fight is saved and summarised first; Ctrl+C flushes and exits 0 |
| Zero UI imports / requirements | PASS | `net/*` and `pipeline.py` import only stdlib + project modules and no `ui`/tkinter (AST scan + subprocess import check, `test_net_modules_are_stdlib_only_and_ui_free`). Note `tests/test_contracts.py` (main-owned) still lists only the 12 Phase 1 core modules, so it does not enforce the rule for `net/*` |

Deviations from the contract, with locations: `net/sources.py:78-91` (idle clock; BUG-N2-5), `app.py:307-318` and `net/pcapng.py:103` (error path; BUG-N2-3), `config.py` NetConfig (no value validation; BUG-N2-4),
`net/pcapng.py:46-50` (BUG-N2-1), `net/sources.py:62` (BUG-N2-2). Documentation inconsistency: `docs/protocol-notes.md:25-26` still describes the framing as `fb <ch> <seq varint>` with "channels"; the code and contract
use `fb <len> <seq u16>` and the real data agrees with the latter (e.g. seq 683 = `ab 02`, a 2-byte varint would be `ab 05`; the "channel" bytes `0b/0c/0e` are body lengths 11/12/14).
The 50 ms duplicate rule is implemented against the last *kept* datagram (CONTRACTS: "seen less than 50 ms earlier"); harmless because the protocol layer dedupes again.

## P2.5 End-to-end runs (real commands)

Scratch config pointing `unmatched_log` at scratch; `--runs-dir` in scratch.
```
python -m eqoa_parser --config cfg.yaml --pcap fixtures/real/cap2_bard_melee_necro_pet.pcapng --no-gui            (exit 0)
  stderr: eqoa net: following connection (client port) 21173, player id 0x174A1A40; ignoring no other connections yet
  stdout: [net] following connection 21173, player 0x174A1A40
          === Fight 1: 0x01312F0A - 82.2s ===
            You  total  20670  dps  251.5  max hit  477  /  Pet total 0  /  Combined 20670, 251.5 dps (damage taken 1176)
            Top abilities: (unknown) (you) 20670
          Run summary: Fights 1, Total damage 20670, Average DPS 251.5
--connection 21173  -> identical numbers, no auto-pick message on stderr
--connection 20605  -> [net] following connection 20605, player 0x1751CB82
          === Fight 1: 0x01312F0A - 83.3s ===  You total 0  Pet total 675 dps 8.1 max hit 25   Run summary: Total damage 675, Average DPS 8.1
cap1_necro_spells.pcapng -> You total 7218, max hit 599, 13 hits, 74.6 s, 96.7 dps (the user's 13 numbers in order)
```
Checks against the oracle by hand: bard 20,670 = sum of the 56 player-source records; damage taken 56 x 21 = 1,176; necro pet 27 x 25 = 675.
Timing: a synthetic 100,000-datagram / 12 MB capture replays in about 10 s with `tracemalloc` on (peak 15 MB); the whole file is read into memory first.
GUI: `--pcap cap2... --speed 20` (and again with `--connection 20605`) showed one window; `WM_CLOSE` -> exit code 0, stderr only the auto-pick line (none with `--connection`), and the run file had the 1 fight saved both times.
`--live` as Administrator was **not** run (instructed; also I am not elevated). Its not-admin failure path was verified with a fake capture only.

## P2.6 Edge cases (Phase 2)

| Case | Result |
|---|---|
| truncated / garbage / 0xFB-filled datagrams, every truncation point | PASS no exception, nothing decoded past the checksum |
| bad pcapng block lengths, big-endian, mixed endianness, IPv6, unknown link types, fragments | PASS (see P2.4) |
| non-pcapng input to the CLI | FAIL BUG-N2-3; empty/truncated/no-traffic files silently "No fights." |
| 0xFB inside another message's body | decoded message: PASS (skipped as a unit); unknown message: RISK-1 |
| varint overlong / unterminated / at checksum boundary | PASS |
| same seq+body re-sent / same seq different body | PASS dropped / kept |
| two connections interleaved, `--connection` selection, auto-selection | PASS; RISK-5 for reconnects |
| player id late / never / header change | PASS (no crash; "you" never appears if the header never matches); RISK-2 collision |
| pet learning window, first wins, non-extended, other target, player swing | PASS; RISK-3, never re-learned |
| extended record with amount 0 | PASS -> `damage` 0 by `pet`, counted as a hit, DPS 0 |
| heal events | PASS never start/extend |
| `PcapReplaySource` speed>0 heartbeats | PASS |
| `LivePacketSource` fake capture: close(), error after startup | PASS; idle clock BUG-N2-5 |
| `RawCapture` header parsing, fragments, error mapping | PASS (fakes only) |
| fuzz: 30,000 mutated datagrams, 30,000 mutated IP packets, 3,000 mutated pcapng files, 300 random attribution streams (ad hoc), plus a seeded 6,000/800/100 version in the suite | PASS no exception other than the intended `ValueError("not a pcapng")` |

## P2.7 Observations (not failing tests)
* O-N2-1: `--pcap`/`--live` still require a valid `patterns.yaml` and create/append `unmatched.log` through the text `Parser` that the packet path never uses (`test_pcap_mode_needs_a_valid_text_patterns_file...`).
* O-N2-2: `--connection` is silently ignored with `--replay` / `--live-pine`.
* O-N2-3: the auto-pick is reported twice (stderr `eqoa net: ...` and stdout/GUI `[net] ...`); the stderr text says "ignoring no other connections yet" even though a second emulator may appear later and then be ignored.
* O-N2-4: `LivePacketSource` emits a heartbeat for every unrelated IP packet the raw socket sees (`read()` returns `None` for them): on a busy machine that is thousands of no-op ticks per second (cheap, but noisy; also what masks BUG-N2-5 on busy machines).
* O-N2-5: `make_source` opens and closes a real capture once (to fail early), then `LivePacketSource` opens a second one.
* O-N2-6: damage timing uses datagram arrival time; all records of one datagram share a timestamp, so within-datagram ordering/durations are coarse (cap2 server datagrams arrive every 0.06-0.2 s).
* O-N2-7: the repo root has a `runs/2026-10-17.json` containing a `live:capture` run (id 20261017T190132, 4 fights) that is not one of mine (my tests and runs use temp dirs); it appeared while I was working, presumably from your live test.

## P2.8 Still unverified against the real game / real system
1. The raw-socket path on Windows: elevation message, `SIO_RCVALL` delivering the server->client direction, interface auto-detection with VPNs/multiple adapters, IP fragments in the wild, packet loss under load, CPU use. (Only fakes and hand-built bytes.)
2. Everything INFERRED in `protocol.py` / `attribution.py`: framing generality beyond 2 captures and 3 logins (what else uses `0xFB`, whether a header word ever differs from the character id), `extended` = pet, pet-vs-you pairing via Swing, zero-amount semantics, heal source naming, that other players' combat is or is not sent, group members, crits/resists/misses/deaths (no such records are decoded), multi-target and DoT records, damage from other classes' skills (only necro spells, bard melee and a necro pet were captured).
3. Whether the server really re-sends identical `(seq, body)` only (and never legitimately repeats a body with the same seq within 120 s), seq wrap behaviour on long sessions, and whether sequence counters restart on reconnect.
4. Client port stability across zoning/reconnects (RISK-5) and behaviour with two characters on one connection.
5. Whether pktmon's `etl2pcap` output on other Windows builds matches the two fixtures (link type, timestamp resolution, snaplen).
6. Real-time GUI behaviour under live traffic and the 8 s idle timeout against real fight patterns.
7. Entity names and ability names (not decoded), so targets show as hex ids; whether the same entity id can be reused.

## P2.9 Reproduce
```
python -m pytest -q                                                              # 9 failed, 726 passed, 1 skipped
python -m pytest -q tests/test_verify_net.py tests/test_verify_net_capture.py tests/test_verify_net_app.py   # 9 failed, 182 passed
python -m eqoa_parser --pcap fixtures/real/cap2_bard_melee_necro_pet.pcapng --no-gui [--connection 21173|20605]
```


## P2.10 After the Phase 2 amendments (session D re-check)

Re-ran everything against the amended code (`docs/CONTRACTS.md`, "Phase 2 amendments after verification"). Full output saved in the shared notes
folder as `pytest_phase2_after_fixes_output.txt`.

```
SKIPPED [1] tests\test_pine_client.py:149: needs AF_UNIX (Linux/macOS)
FAILED tests/test_verify_net_amend.py::test_pcapng_reader_and_rawcapture_agree_on_inconsistent_length_frames[ip_total_gt_frame]
FAILED tests/test_verify_net_amend.py::test_pcapng_reader_and_rawcapture_agree_on_inconsistent_length_frames[udp_len_gt_segment]
FAILED tests/test_verify_net_amend.py::test_pcapng_reader_and_rawcapture_agree_on_inconsistent_length_frames[frame_cut_but_orig_equals_cap]
FAILED tests/test_verify_net_amend.py::test_pcapng_reader_and_rawcapture_agree_on_inconsistent_length_frames[ip_total_below_header]
4 failed, 799 passed, 1 skipped in 98.63s (0:01:38)
```
(One run had a second skip, `test_gui_path_surfaces_live_capture_error_in_the_window`, because Tk found no display at that moment; it passes when a display is present.)

**Tests updated to the amended contract** (not weakened; each now asserts what the amended code must do): the 7 tests built on the old
"otherwise -> damage" rule (late player id, never-matching header, impostor player id, pet never relearned, hostile damage to third parties, zero-amount
extended records, other people's combat) now assert that unrelated records produce no Event and are counted in `Attributor.dropped_unrelated`
(per connection, including unselected ones), that records involving the player or the learned pet are all still kept (damage, damage_taken on `you`/`pet`,
heals both ways), that a stranger's combat no longer starts or extends a fight (the fight closes `idle_timeout` after your last hit), and the empty/truncated
file test now asserts the one-line stderr notice. New file `tests/test_verify_net_amend.py` covers the other amendments.

**Status of the five bugs**
| Bug | Status | Check |
|---|---|---|
| N2-5 live idle clock | FIXED | heartbeat after every Event-less datagram and at least every `poll_interval`; fight closes while the server keeps sending, also when the *other* connection is the busy one; stream of only unrelated combat produces no fights and keeps ticking; heartbeat/event ts non-decreasing |
| N2-3 not a pcapng | FIXED | garbage, text, one-line text and a classic .pcap give `error: ...` exit 2 before the banner, nothing printed to stdout, no run file; GUI launch fails before any window; `PcapReplaySource` raises `ValueError` on construction; empty and tiny valid-prefix files are accepted |
| notice for empty/truncated/no-traffic | DONE | empty, truncated, no server traffic, wrong `server_ip`, wrong `--connection`: exactly one stderr line "no game-server combat found ...", exit 0; absent for a good capture; reaches the GUI/console sink via `on_error`; never printed in live mode |
| N2-4 net config | FIXED | `local_port` 1-65535/null (0, 65536, -1, `true` rejected), `server_ip`/`interface_ip` IPv4 only (`256.1.1.1`, `1.2.3`, `::1`, hostnames, leading space, `/24` rejected) |
| N2-2 time order | FIXED | regressing ts clamped in both the tuple and `Event.ts`, nothing dropped or reordered, one warning then counted in `clamped`, untouched on ordered input, also at speed > 0, and a late-stamped datagram no longer merges two fights |
| N2-1 truncated frames | FIXED for snaplen, **residual bug N2-1b (low)** | caplen < orig_len (1, 4, 5, 20, 60 bytes cut; headers cut) is dropped, real captures have 0 such frames and still decode to the oracle. But see below |

**Remaining real bug: N2-1b (low).** The amendment says the pcapng reader and `RawCapture` "must agree on what is dropped".
They still disagree on frames whose IP total length or UDP length claims more bytes than the frame holds while the capture header says
`caplen == orig_len` (an unflagged cut): `parse_ip_udp` (`net/pcapng.py:46-50`) clamps with `min()` and keeps a shortened payload,
`IPv4UdpParser.feed` (`net/capture.py`) drops it. Repro: `test_pcapng_reader_and_rawcapture_agree_on_inconsistent_length_frames` (4 cases:
IP total 40 bytes longer than the frame, UDP length 40 longer than the segment, a frame cut by 5 bytes, IP total below the header length). The
retained last four bytes of such a payload then play the checksum. Not seen in the fixtures; kept failing on purpose.

**New observations / risks from the amendment**
* RISK-8 (design interaction, test passes and documents it): auto-selection still follows the first connection that produces an `HpChange`, even if that
  record is dropped as unrelated. A connection that only ever sees strangers fight selects itself, yields no Event, and the connection where you fight is ignored
  for the whole run with no message (`test_risk_autoselect_is_taken_by_a_connection_whose_only_combat_is_other_peoples`). Before the amendment the same rule picked it
  too, but the strangers' events were at least visible. Suggest selecting on the first record that involves the player (the first one that yields an Event).
* The impostor-player-id risk (RISK-2) got worse in effect: the real player's records are now dropped as unrelated (before they showed under a hex name), so the session
  silently attributes the impostor's hits to `you`. Deferred by the contract; test updated to pin it.
* With a header word that never matches an entity, every record is dropped and the user gets the generic "no game-server combat found (empty, truncated, or no traffic ...)"
  notice, which does not describe that case; the dropped count is not shown anywhere (only `Attributor.dropped_unrelated`). Same wording appears for a wrong `--connection`.
* Hostile damage to your pet before the pet is learned (the first extended hit), and to a re-summoned pet, is now silently dropped, so `damage taken` under-counts in those windows (deferred risk, now invisible instead of mislabelled).

**End-to-end after the fixes** (`--pcap fixtures/real/cap2_bard_melee_necro_pet.pcapng --no-gui`, default / `--connection 21173` / `--connection 20605`): identical to the earlier
results: You 20,670 (56 hits, damage taken 1,176), same again with 21173, and Pet 675 with 20605; cap1 7,218 with the user's 13 numbers; GUI launch + close exit 0.
Still unverified: everything in P2.8 (the real raw socket on Windows, INFERRED protocol rules, reconnect behaviour, live GUI behaviour under real traffic).


---------------------------------------------------------------------------------------------------------------
---------------------------------------------------------------------------------------------------------------

# Phase 3 verification: several emulators, own_actors, portable exe (session D, independent)

Date of run: 2026-10-18, Windows 11, Python 3.14.6. Scope: "Phase 3" in `docs/CONTRACTS.md`, `net/attribution.py` (router, labels), `net/names.py`,
`stats.summarize_fight(own_actors=)`, `pipeline.py`, `app.py`/`ui/*` for the new rows and status line, `packaging/` and `dist/`.
New files: `tests/test_verify_phase3.py` (37 tests) and `tests/test_verify_phase3_exe.py` (8 tests). I also updated 11 older tests of mine that asserted the
"follow only the first connection" default (details below). `EQOAParser.exe` (UAC) was never run; `--live` was never run (no raw socket).

## P3.1 Test suite

```
SKIPPED [1] tests\test_pine_client.py:149: needs AF_UNIX (Linux/macOS)
FAILED tests/test_verify_phase3.py::test_other_characters_pet_swinging_at_the_same_mob_does_not_become_my_pet                BUG-P3-1
FAILED tests/test_verify_phase3.py::test_other_players_swing_at_my_mob_does_not_turn_their_damage_into_my_pet_damage        BUG-P3-2
FAILED tests/test_verify_phase3.py::test_reloaded_fights_from_an_earlier_run_still_show_the_other_characters_breakdown      BUG-P3-3
FAILED tests/test_verify_phase3.py::test_tk_reloaded_fight_detail_line_lists_other_characters                                BUG-P3-3
4 failed, 876 passed, 1 skipped in 108.43s (0:01:48)
```
Full output: `pytest_phase3_full_output.txt` in the shared notes folder; the suite was run three times in a row with the same result (no flake).
Before my update the suite was `12 failed, 822 passed, 1 skipped`: the 11 old-default tests plus the pine flake below.

**Old tests updated to the Phase 3 contract (not weakened).** Each now asserts the new behaviour: the cap2 acceptance (`you` 20,670 over 56 hits; the necro connection as a
non-primary with pet label `Zecron's pet`, 27 x 25 + 9 x 0 = 675 over 36 hits, no `damage_taken`, own_actors `["you", "Zecron's pet"]`, run total 21,345, JSON `combined_total`
`[20670, 675]`); two interleaved connections both followed (first Event = `you`, the other `char:<port>` and `char:<port>'s pet` for extended hits, one "following" and one
"also following" line); the primary is the first connection that produces an *Event* (a swing-only or strangers-only connection gets no role); a connection appearing later
from a new port is followed under its own label (was RISK-5); `--connection PORT` still gives the old single result (CLI header `following: you (0x..., port N)`, no auto-pick
message, one fight, no labels); the live path with `--connection`; the connection status string; and the live-idle test now has two variants (a second connection with only
strangers' combat must not keep the fight open; a second followed character who keeps hitting does keep the one shared fight open and is counted).

## P3.2 The `test_unchanged_memory_polled_many_times_yields_nothing[True-ring]` failure: a flake in my test suite, not a race in `pine_source`

Evidence:
* The failure was `TimeoutError` inside `PineClient._recv_exact` (the 2 s socket timeout): one mock-server reply took more than 2 s. In isolation the test takes 0.02 s; 1,800 requests
  idle: median 0.11 ms, max 5 ms; with 16 CPU-burning processes: 3,600 requests, max 181 ms. So no slowness and no deadlock in `pine_source`/`pine`.
* The failing full-suite output also carried `PytestUnraisableExceptionWarning ... tkinter Variable.__del__ ... RuntimeError: main thread is not in main loop`.
* Mechanism, reproduced deterministically in a 20-line script: a Tk variable left as unreachable garbage while its Tk interpreter is still alive is finalised by the garbage collector in
  whichever thread happens to trigger the collection. When that is not the main thread, Tkinter waits about 1 s for a main loop that never runs (then raises the RuntimeError above).
  If that thread is the mock server's request thread, the client's request stalls ~1 s per leaked variable, and two or more make it exceed the 2 s timeout. (Repro: one leaked `StringVar`
  with a cycle, `gc.collect()` inside the server thread -> `request 3: ok 1072 ms`.)
* The leak was **mine**: `test_verify_net_app.py::test_gui_path_surfaces_live_capture_error_in_the_window` left one Tk object and several variables alive (`gc` census at the pine test: 1 Tk, 5 Variables).
  It only runs when a display exists, which is why the failure looked random. Fix (in my tests): collect on the main thread before and after `root.destroy()`, in that test
  and in the module-scoped Tk fixture of `test_verify_ui.py`. After the fix the Tk-object census returns to 0 after each of my GUI tests, and three full runs were clean.
* Side note for the product, not a bug today: the same stall can happen in the real app if Tk variables are finalised on the engine thread while the main thread is not in `mainloop()` (e.g.
  during `shutdown()`, which joins the engine thread for up to 3 s). Worst case is a slow window close; not reproduced in the app.

## P3.3 Bugs found (most severe first)

### BUG-P3-1 (medium) — a pet is learned from ANY swing at the same mob, including another character's pet
`net/attribution.py` `Attributor._learn_pet` (lines 167-175): the most recent swing at T within 1.0 s before the player's extended hit at T, from any attacker other than the player, becomes the pet.
Phase 3 delivers every character's swing records to every connection, so two pet classes on one mob mislearn each other.
Repro (`test_other_characters_pet_swinging_at_the_same_mob_does_not_become_my_pet`): A and B connected; A's pet swings at the mob at t, B's pet at t+0.1, both owners'
extended hits at t+0.5. Result: A's attributor learns **B's pet** (`pet_id == 0x01445566`, A's own pet is lost). Consequences: a hostile hit on B's pet is then emitted as
`damage_taken` on A's pet **and** on B's connection (double counted in `damage_taken`), and a hit on A's real pet is dropped. Two necros on one mob is the natural case.

### BUG-P3-2 (medium) — the mislearned "pet" turns another player's damage into damage counted twice (breaks "no double counting")
Same root cause, worse consequence (`test_other_players_swing_at_my_mob_does_not_turn_their_damage_into_my_pet_damage`). If the server also sends other PLAYERS' swing records (UNVERIFIED; the
protocol notes say the local player's own melee has no swing record, nothing is known about others'), a swing by B at the mob right before A's extended hit makes B "A's pet".
Records with B as source are then no longer dropped on A's connection: `Attributor.feed` falls to the "learned pet itself is the source" branch and emits them as damage by A's pet label.
Repro: B's 500-damage melee hit is counted twice, once as `char:20001` (B's own connection) and once as `pet` (A's connection): true total 527, counted 1027.
Fix direction: only accept a swing whose attacker is not another followed connection's player or pet, or drop the "pet itself is the source" branch (pet damage is credited to the owner anyway).

### BUG-P3-3 (low) — reloaded fights lose the other characters in their breakdown
`ui/console.py` `row_actors`/`format_others` and `ui/tk_app.py` `_show_detail` use the **current run's** own-actor list. A fight from an earlier run (marked `prev`) is drawn with that list, which
is empty until the new run has produced an Event, so e.g. "Zecron 20" or "Zecron's pet 675" vanish from its detail line although they are inside its stored Combined total.
Repro: `test_reloaded_fights_from_an_earlier_run_still_show_the_other_characters_breakdown` (`format_others(persisted, []) == ""`) and the Tk version (detail line has no "Others (dmg / dps)").
Fix direction: for reloaded fights derive the "others" from the fight itself (actors other than you/pet that appear in its combined/top abilities) or store the own-actor list per fight.

## P3.4 Risks and observations (tests pass and document them)

* RISK-9 labels are not made unique: two followed connections that resolve to the same name merge into one actor everywhere (`test_two_connections_with_the_same_name_would_merge_their_stats`).
* RISK-10 label and role are fixed per connection for the run: a different character logging in on the same client port (header word changes) keeps the previous character's label
  (`test_risk_label_is_stale_when_a_different_character_logs_in_on_the_same_connection`). A reconnect from a NEW port is followed correctly (as another label), but the player then shows as a label, not `you`.
* RISK-11 "primary = first connection that produces an Event" includes heals: an idle character whose connection carries a heal or regeneration record first becomes `you`, and the character who actually fights becomes a label.
* Names: the label is the name only if known when the connection's first Event is created; a name arriving later does not change it (tested, as specified). A name in the same datagram as the first Event is used. Mob names
  that arrive mid-fight still give two target labels (documented limitation). `NameTable` agrees with `fixtures/real/expected_names.json` on every pair it returns for cap2 (no wrong pair; bard and dummy stay hex), but
  that oracle is the same heuristic, so it only proves self-consistency. Random noise, non-server traffic, too-short/too-long/non-alphabetic strings produce no names.
* `--show-group` is effectively dead for packet sources: with unrelated combat dropped, no non-own actor can deal damage, so there is nothing for it to show.
* `own_actors` only contains actors that dealt damage; a connection that only heals or takes damage never appears in the damage rows (it does appear in the `following:` line).
* The console shows fixed `You` and `Pet` rows even when the fight has none (fight 2 of cap2: "You 0", "Pet 0", then "Zecron's pet 675"): noise, not wrong.
* The status line example in the contract ("Zecron (port 20605)") differs slightly from the output ("Zecron (0x1751CB82, port 20605)"): fine, the contract said "e.g.".
* `own_actors` is per run and never shrinks: after a character's emulator closes its rows stay (zeros) in later fights.

## P3.5 What was checked and passed

| Area | Result |
|---|---|
| Two characters on the same mob, mirrored records (60 random hits, damage taken on both, A's heal ticks) | PASS each hit counted once; per-actor totals, `damage_taken` once each, `dropped_unrelated` equals the other character's records; combined = A + B; one shared fight |
| Pet of the second character vs a character without a pet | PASS B's pet damage counted once, from B's connection; A learns no pet |
| Each owner's pet on different mobs more than 1 s apart | PASS labels per owner never mixed |
| Heal between the two characters; mob hitting one character | PASS target labelled with the other connection's label; damage taken only on the victim's connection |
| Labels | PASS name known before the first Event -> `Zecron` / `Zecron's pet`; after -> stays `char:<port>`; same-datagram name used; primary stays `you` even if named; mob names resolve |
| Third connection, own_actors order | PASS `you`, `pet` first, then first-seen (C's pet, C, B); copy returned; third label `char:<port3>` |
| Disappearing / returning connection | PASS label kept, later fights unaffected (zero share), same label when it returns |
| Strangers-only connection | PASS never becomes primary, no label, not in `connections()` or own_actors |
| `--connection` with 3 connections | PASS each of the three as `you`/`pet`, others muted (no role, no label, no output), no follow messages, a never-appearing port yields nothing |
| `summarize_fight(own_actors=)` | PASS None = old you+pet rule; list = exactly those actors (others still stored); `[]` = 0; rank tie-break and top 3; run summary uses combined; Engine live and closed summaries; text replay unchanged |
| Persistence | PASS labelled fights (apostrophes, colons) round-trip through the run store; schema unchanged |
| Console rows | PASS one row per own actor in first-seen order; none for non-own actors without `--show-group` |
| Live path, two emulators (fake capture) | PASS mirrored traffic counted once (40 + 40), fight closes by idle heartbeat |
| cap2 default (CLI) | PASS fight 1 you 20,670 / 56 hits / damage taken 1,176; fight 2 necro non-primary, label `Zecron` (name known at its first Event), pet 675 / 36 hits, `you` 0, Combined 675; run 21,345 / 129.0 dps; `--connection 20605` -> necro = you/pet, 675; `--connection 21173` -> 20,670 |
| GUI | `--pcap cap2 --speed 20` launches and closes with exit 0 (checked in Phase 2, unchanged); the reloaded-fight detail issue is BUG-P3-3 |

## P3.6 Portable exe (`dist/`)

Method: extracted `dist/EQOAParser-0.1.0.zip` into a fresh temp directory, copied the capture there, ran `EQOAParser-replay.exe` from a different working directory.
* Zip contents: `EQOAParser.exe` (13,517,595 bytes), `EQOAParser-replay.exe` (13,521,179), `config.yaml` (596), `READ-ME-FIRST.txt` (1,953, identical to `packaging/`). Both exes are byte-identical to `dist/`.
* Manifests (static byte search, nothing executed): `EQOAParser.exe` has `requestedExecutionLevel level="requireAdministrator"`; the replay build has `asInvoker` and no `requireAdministrator`. **`EQOAParser.exe` itself was not run.**
* `EQOAParser-replay.exe --pcap cap2_bard_melee_necro_pet.pcapng --no-gui` (cwd elsewhere): exit 0 in about 2.4 s; stderr `eqoa net: following connection (client port) 21173, ... as you` / `also following ... as Zecron`;
  output identical to the Python CLI: Fight 1 You 20,670, damage taken 1,176; Fight 2 `Zecron's pet 675`, Combined 675; run 21,345, 129.0 dps.
* `runs/2026-10-18.json` and `unmatched.log` were created **next to the exe**; the working directory stayed empty. A second run appends ("2 fight(s) from earlier runs today").
* `--connection 20605` -> necro = you/pet, 675. `config.yaml` next to the exe is honoured (`local_port: 21173` -> 1 fight, 20,670); with the file removed the bundled default applies (2 fights); a bad value
  (`idle_timeout: banana`) -> `error: config idle_timeout: must be float, got 'banana'`, exit 2. A missing capture, a non-pcapng file and no source flag -> `error:` / usage, exit 2, no traceback.
  `--replay synthetic_combat.log --no-gui` -> 5 fights, 274 total (patterns bundled). `--help` works (it says `python -m eqoa_parser`, cosmetic).
* `requirements.txt` is unchanged (PyYAML, pytest); `requirements-dev.txt` adds `-r requirements.txt` and `pyinstaller>=6.0`. Frozen-path logic is confined to `config.py` (`find_project_root`, `sys.frozen`, `_MEIPASS`).
* Not verified: `EQOAParser.exe` startup, the UAC prompt, the windowed build's message-box error path, antivirus or SmartScreen behaviour, Windows versions other than this one, the live raw-socket path inside the exe.
  The checks above are also in `tests/test_verify_phase3_exe.py` (runs only the replay exe, from a temp unzip; skipped if `dist/` is missing).

## P3.7 Still unverified against the real game / real system
1. Whether the server really delivers other characters' records and **swing records** to every client (the Phase 3 premise, and BUG-P3-1/2, depend on it); how other players' pets appear.
2. Pet, name and label rules beyond cap2: NameTable is a heuristic checked on one capture (24 pairs); many names are missed or fragmentary; behaviour with real two-emulator live traffic.
3. Everything from P2.8 (raw socket as Administrator, interface auto-detection, fragments in the wild, client-port stability across zoning or reconnects).
4. DPS on a shared fight: one Segmenter serves all characters, so two characters fighting different mobs within the idle window are merged into one fight (by design); real play patterns untested.
5. The exe on a clean machine, and the admin build end to end.
