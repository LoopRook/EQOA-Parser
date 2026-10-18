# Phase 1 report: EQOA combat parser MVP

Status: **Phase 1 complete.** The pipeline works end to end in replay mode on SYNTHETIC data, and the recon tools work
against the mock PINE server. The PINE client and the recon scan were also checked against a real PCSX2 (read-only).
Every combat format is still ASSUMED. Phase 2 needs real captures from you.

## Delegation mechanism
Claude Code **session cards** (`start_session`), not the subagent fallback. A, B and C ran in parallel on Sonnet 5.5;
D (the verifier) ran on Sonnet 5.5 at high effort with fresh context. The main chat wrote the contracts first
(`docs/CONTRACTS.md`, `events.py`, `config.py`, the stub signatures, the fixtures, and a hand-computed oracle) and
re-checked every session's evidence itself. That re-checking found two issues the sessions' own reports missed: the
mock server crashed with `--encoding utf16le`, and the window was 800px wide. D found 9 more bugs; all were fixed and
re-verified.

| Session | Built |
|---|---|
| A recon | `pine.py` (read-only PINE client), `mock_pine.py`, `recon.py` (scan/snapshot/diff/watch), `pine_source.py`, `docs/finding-the-buffer.md` |
| B parser | `parser.py`, `segmenter.py`, `stats.py`, `pipeline.py` (Engine), `ReplaySource` |
| C ui | `persistence.py`, `app.py`/`__main__.py` (CLI), `ui/tk_app.py` (Tkinter), `ui/console.py` (`--no-gui`) |
| D verifier | `tests/test_verify_{core,pine,ui}.py`, `docs/VERIFICATION.md` |

## Evidence

### 1. Full pytest output (`python -m pytest -q`; the verbose list is in `docs/evidence/pytest-full.txt`)
```
........................................................................ [ 16%]
............................................................s........... [ 32%]
........................................................................ [ 48%]
........................................................................ [ 65%]
........................................................................ [ 81%]
........................................................................ [ 97%]
...........                                                              [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_pine_client.py:149: needs AF_UNIX (Linux/macOS)
442 passed, 1 skipped in 45.68s
```
`tests/test_contracts.py::test_replay_fixture_matches_hand_computed_oracle` compares the replay against numbers worked
out by hand (`fixtures/synthetic_combat.expected.json`), written before any implementation existed.

### 2. Replay run and resulting run JSON
```
python -m eqoa_parser --replay fixtures/synthetic_combat.log --no-gui --runs-dir docs/evidence/runs
```
Result: `docs/evidence/runs/2026-10-17.json` (`schema_version` 1, one run, 5 fights). Per fight:

| # | target | dur | You | Pet | combined (dps) | top 3 |
|---|---|---|---|---|---|---|
| 1 | a decaying skeleton | 6.0s | 82 | 28 | 110 (18.33) | melee(you) 42, Fire Bolt(you) 40, melee(pet) 28 |
| 2 | a fire beetle | 0.0s | 55 | 0 | 55 (55.0) | Fire Bolt(you) 55 |
| 3 | a gnoll pup (+ a gnoll scout) | 7.5s | 65 | 19 | 84 (11.2) | Fire Bolt(you) 38, melee(you) 27, melee(pet) 19 |
| 4 | a large rat | 0.0s | 7 | 0 | 7 (7.0) | melee(you) 7 |
| 5 | a large rat | 0.0s | 9 | 9 | 18 (18.0) | melee(you) 9, melee(pet) 9 |

Run: 5 fights, 274 total, 16.6 average DPS, best fight #2 (55.0 DPS). A second launch appends a second run to the same
file and reloads the first run's fights. Two launches at once no longer erase each other.

### 3. `--no-gui` output on the replay file
Full text: `docs/evidence/nogui-replay.txt`. Excerpt:
```
=== Fight 1: a decaying skeleton - 6.0s ===
  You        total     82  dps    13.7  max hit    40
  Pet        total     28  dps     4.7  max hit    11
  Combined   total    110  dps    18.3  (damage taken 8)
  Top abilities: melee (you) 42, Fire Bolt (you) 40, melee (pet) 28
...
=== Run summary ===
  Fights: 5
  Total damage: 274
  Average DPS: 16.6
  Best fight: fight 2 vs a fire beetle at 55.0 dps
```
Error handling: a missing replay file, `--speed nan`, `--live` with no buffer address, and `--replay` together with
`--live` each print `error: ...` and exit 2, with no traceback.

### 4. GUI on replay data
`docs/evidence/gui-replay-final.png` (516x639 including the title bar; the minimum size is 420x420). It has an "Always on
top" checkbox, the current-fight panel (You/Pet rows plus group rows with `--show-group`), the run summary, and the fight
list newest first, with fights from earlier runs today shown in grey as `prev`. The top 3 abilities for the selected fight
appear in the line at the bottom.

### 5. Mock PINE result
`tests/test_pine_client.py`, `test_pine_source.py`, `test_recon.py` and `test_verify_pine.py`: **115 passed, 1 skipped**
(the UNIX socket test, which needs Linux). This covers every read width, batch reads, matches across chunk boundaries in
ascii/utf16le/shiftjis, snapshot/diff, watch, PineBufferSource dedupe (unchanged re-reads, appends, scroll with a torn last
line, ring wraparound, repeated identical lines), and proof that no write opcode exists. Manual check: the mock server
CLI plus `recon scan` finds planted text in all three encodings.

### 6. Real PCSX2, read-only (beyond Phase 1)
PCSX2 v2.8.2, *EverQuest Online Adventures - Frontiers*, SLUS-20744 v1.02, PINE on **slot 28012**. Info calls returned
the values above. 8/16/32/64-bit reads agree with byte reads (little-endian). 1 MiB reads in 0.09s, and a full 32 MB
`recon scan --text EverQuest` took 2.6s and found 2 matches. A read at 0x02000000 returns 0 instead of failing. Nothing
was written to the emulator, and no buffer hunting was done.

## Still unverified against the real game
1. **Combat message formats:** all 11 patterns in `patterns.yaml` are ASSUMED (EverQuest-style wording), as is every
   line in `fixtures/`. That includes the pet's name or wording, how other players appear, the miss/death/xp lines, and
   whether ability names appear at all.
2. **Text encoding** of the chat/combat buffer (ASSUMED ascii).
3. **Buffer address** in EE RAM: unknown. `--live` refuses to start without one.
4. **Buffer layout and dedupe:** PineBufferSource's `scroll` and `ring` layouts, overlap matching, torn-line handling,
   `stable_reads` and `emit_initial` are all UNVERIFIED. It is also unverified whether two identical consecutive hits
   can be told apart in the real buffer.
5. **Timestamps:** live events are stamped at discovery time, with poll jitter of about `poll_interval` (0.1s). Real
   fight lengths and DPS may differ slightly from what in-game timing would give.
6. **The Linux UNIX socket transport** (only tested against the mock server path logic on Windows). On Windows, TCP,
   the info calls and the read opcodes are verified against real PCSX2.
7. **Fight segmentation** on real pacing: the 8s idle timeout, and treating overlapping pulls as one fight.

## Phase 2: what I need from you
- Real combat lines: copy them exactly as shown in game (or as screenshots). Include your melee hits, your spells,
  your pet's hits, mob hits on you and your pet, misses, kills, and a few non-combat chat lines.
- Then follow `docs/finding-the-buffer.md` to locate the buffer with `recon scan/snapshot/diff/watch`. Use
  `--slot 28012`, or set it back to 28011 in PCSX2.
- PCSX2 version: already known (2.8.2).
