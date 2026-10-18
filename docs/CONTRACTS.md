# Contracts (authoritative)

Written by the main chat before delegation. Sessions build against this document and the stub
signatures in `eqoa_parser/`. If a contract seems wrong, report it back; do not silently change it.

## Vocabulary
- **run**: one launch of the app. A run contains fights. (The word "session" is reserved for Claude Code session cards.)
- **fight**: a span of combat, from the first damage event until `idle_timeout` seconds pass with no damage dealt or received.
- **SYNTHETIC / ASSUMED**: invented data or formats not checked against the real game. **UNVERIFIED**: code whose
  behaviour against real emulator memory has not been tested.

## Known unknowns (never present as fact)
Real combat message formats, text encoding, the EE RAM address of the chat/combat buffer, and how that buffer
behaves (ring buffer? fixed slots? scrolling?) are all unknown. Everything in `patterns.yaml` and `fixtures/` is
SYNTHETIC/ASSUMED.

## Layout and file ownership
Core modules must have **zero UI imports** (enforced by `tests/test_contracts.py`). Each session edits only the files it owns.

| Path | Owner | Notes |
|---|---|---|
| `eqoa_parser/events.py`, `config.py`, `docs/CONTRACTS.md`, `patterns.yaml`, `fixtures/*`, `tests/test_contracts.py`, `requirements.txt`, `pyproject.toml`, `config.yaml` | main | A may add fields to `PineConfig`/`BufferConfig` only |
| `eqoa_parser/pine.py`, `pine_source.py`, `mock_pine.py`, `recon.py`, `docs/finding-the-buffer.md`, `tests/test_pine*.py`, `tests/test_recon*.py` | A recon | |
| `eqoa_parser/parser.py`, `segmenter.py`, `stats.py`, `pipeline.py`, `ReplaySource` in `sources.py`, `tests/test_parser*.py`, `test_segmenter*.py`, `test_stats*.py`, `test_pipeline*.py`, `test_replay*.py` | B parser | keep the existing dataclasses/signatures |
| `eqoa_parser/persistence.py`, `eqoa_parser/ui/**`, `eqoa_parser/__main__.py`, `eqoa_parser/app.py`, `tests/test_persistence*.py`, `tests/test_ui*.py`, `tests/test_cli*.py` | C ui | |
| `tests/test_verify*.py`, `docs/VERIFICATION.md` | D verifier | |

Core = `events, config, sources, parser, segmenter, stats, pipeline, persistence, pine, pine_source, mock_pine, recon`.

## events (`eqoa_parser/events.py`) — implemented
`Event(ts, actor, target, amount, ability, kind, raw)`, frozen. `actor` is a free string: `"you"`, `"pet"`, or any
name. Kinds: `damage` (friendly actor dealt damage), `damage_taken` (hostile actor hit you/pet/a friend), `miss`,
`death`. `ACTIVITY_KINDS = {damage, damage_taken}`.

## config (`eqoa_parser/config.py`) — implemented
`load_config(path=None, **overrides) -> Config`. YAML; missing keys default; unknown keys raise `ValueError`.
`Config.resolve(p)` resolves relative paths against the config file's dir (or the project root).
Key fields: `idle_timeout=8.0`, `show_group=False`, `pet_names=[]`, `player_name=None`, `patterns_file`,
`unmatched_log`, `runs_dir`, `pine.{slot=28011,host,unix_socket,timeout}`, `buffer.{address=None,size,encoding,poll_interval}`,
`ui.{always_on_top=True,refresh_ms=250}`.

## sources (`eqoa_parser/sources.py`)
`LogSource` protocol: iterable of `(ts: float, raw_line: str)` in non-decreasing ts, plus `close()`.
`HEARTBEAT = ""` as raw_line = no text, just "the clock is now ts" (lets idle fights close in live/real-time replay).

**ReplaySource(path, speed=0.0, heartbeat_interval=0.25)** (B). Replay file format (ours): UTF-8, `<ts>\t<raw>` per
line; `#` comments and blank lines skipped; malformed lines skipped with a stderr warning. Yields file ts unchanged.
`speed=0` → no sleeping; `speed>0` → sleeps scaled gaps and yields heartbeats while waiting. A replayed line that
repeats an earlier one is a distinct message and must be yielded again (no dedupe in replay).

**PineBufferSource(config)** (A, in `pine_source.py`, re-exported from `sources`). Polls `config.buffer` via the
read-only PINE client, decodes with `config.buffer.encoding`, yields each **new** line once with `ts = time.time()`
at discovery, and yields heartbeats on polls with no new text. Must dedupe re-reads of the same buffer
(repeated scans of unchanged memory; ring-buffer wraparound) **without** dropping genuinely repeated messages where
the buffer layout makes them distinguishable. All dedupe logic is marked **UNVERIFIED** in code and docs.
If `config.buffer.address is None` it raises a clear error pointing at `docs/finding-the-buffer.md`.

## PINE (`eqoa_parser/pine.py`) (A)
Implemented from the PINE spec in the PCSX2 repository (fetched, not from memory). **Read-only**: no write opcodes
are implemented at all. Windows: TCP `127.0.0.1:<slot>`. Linux: UNIX socket path per the spec, overridable with
`pine.unix_socket`. OS detected at startup (`sys.platform`). Default slot 28011.
API: `PineClient(config.pine)`, `read8/16/32/64(addr)`, `read_batch(...)`, `read_bytes(addr, n)` (chunked batch
reads), plus whatever info/status calls the spec offers that are read-only. Context manager.

## parser (`eqoa_parser/parser.py`) (B)
`load_patterns(path)`, `Parser(patterns, *, unmatched_path, pet_names, player_name)`, `parse(ts, raw) -> Event | None`.
- Line is `strip()`ped then `re.fullmatch`ed against each pattern in file order; first match wins.
- Named groups `actor, target, amount, ability` fill the Event; otherwise the pattern's fixed values. Amount → int
  (0 for miss/death). Missing ability → the pattern's fixed ability, else None.
- Normalization of actor and target: `"You"/"you"/"YOU"/"yourself"` → `"you"`; `"your pet"/"Your pet"` → `"pet"`;
  names in `pet_names` → `"pet"`; `player_name` → `"you"` (all case-insensitive). A leading article
  `A /An /The ` is lower-cased (`"A decaying skeleton"` → `"a decaying skeleton"`). Otherwise the name is kept as-is.
- `kind: ignore` → None, not logged. HEARTBEAT → None, not logged.
- Unmatched non-empty lines → None, appended to `unmatched_path` as `<ts>\t<raw>\n` (raw with newlines/tabs escaped
  as `\n`/`\t` so each entry stays on one line) and flushed immediately. `unmatched_count` counts them.

## segmenter (`eqoa_parser/segmenter.py`) (B)
See the docstring on `Segmenter`. Event time only, never wall clock. New fight when an activity event arrives and
`ts - end_ts >= idle_timeout` (exactly 8.0s gap → new fight). Misses and deaths never start or extend a fight.
Overlapping targets inside one timeout window are **one fight** with several `targets`. Fight ids start at 1 per run.

## stats (`eqoa_parser/stats.py`) (B)
Dataclasses are fixed (see file). `summarize_fight(fight, ongoing=False)`, `summarize_run(summaries)`.
- Only `damage` events count toward actor totals; `damage_taken` sums into `damage_taken`.
- `dps = total / effective_duration(duration)` where `effective_duration = max(duration, 1.0)`.
- `combined_total`/`combined_dps` = you + pet only. Group actors are still computed and stored.
- Ability key: `event.ability or "(unknown)"`.
- `top_abilities`: you+pet abilities sorted by total desc, then actor (`you` before `pet`), then ability name; top 3.
- Primary `target`: most `damage` received across all actors; tie → earliest first hit. None if no damage events.
- `RunSummary.average_dps = total_damage / sum(effective_duration)`, 0.0 for no fights; best fight = highest
  `combined_dps`, ties → earliest.

## pipeline (`eqoa_parser/pipeline.py`) (B)
`Engine(config, source, *, parser=None, on_live, on_closed, on_finished)`; `run(stop=None)` blocks, consumes the
source, calls `step()` for each tuple, then `finish()`. Callbacks fire on the run() thread. `closed_fights`,
`current_summary()`, `run_summary()`. The Engine does not persist; the app layer does.

## persistence (`eqoa_parser/persistence.py`) (C) — core, no UI imports
File: `<runs_dir>/<YYYY-MM-DD>.json` (local date at run start), written atomically (temp file + replace) after
every closed fight. Schema:
```json
{"schema_version": 1, "date": "2026-10-17",
 "runs": [{"run_id": "20261017T190000", "started_at": "2026-10-17T19:00:00-07:00",
           "source": "replay:fixtures/synthetic_combat.log", "fights": [<FightSummary.to_dict()>, ...]}]}
```
On start the app loads today's file, keeps earlier runs intact, and appends a new run. Corrupt/unreadable file:
rename to `<name>.corrupt-<timestamp>` and start fresh; never crash and never silently overwrite good data.

## app / UI (C)
Entry point `python -m eqoa_parser`:
`--config PATH` `--replay FILE` `--speed X` (default 1.0 for GUI, 0 for --no-gui) `--live` `--no-gui`
`--show-group` `--idle-timeout N` `--runs-dir DIR` `--always-on-top/--no-always-on-top`.
Exactly one of `--replay` / `--live`. GUI = Tkinter (stdlib). Engine runs on a worker thread; the Tk main thread drains a
queue via `after(ui.refresh_ms)`. Panels: live fight (You and Pet: total, DPS, max hit; group rows only with show_group);
fight list (target, duration, You total/DPS, Pet total/DPS, combined, top 3 abilities), including fights reloaded from
earlier runs today (visibly marked); run summary (fight count, total damage, average DPS, best fight) for the current run.
`--no-gui` prints the same live and summary data to stdout and exits when a replay ends.
`--live` uses PineBufferSource; with no buffer address configured it exits with a clear message.

## Amendments after verification (session D, docs/VERIFICATION.md)
Decisions by the main chat. Each one fixes a bug D found, and D's tests in `tests/test_verify*.py` are the acceptance
tests. Owners must not edit D's tests; if one seems wrong, report it.

- **config** (main, done): `load_config` validates values: invalid YAML, wrong types (e.g. `idle_timeout: '8'`),
  non-finite numbers, `idle_timeout/timeout/size/poll_interval/refresh_ms <= 0`, a slot outside 1-65535, and an unknown
  `buffer.encoding` all raise `ValueError`. CLI overrides are validated the same way.
- **app/CLI** (C): every user-input or startup failure ends with `error: <message>` on stderr and **exit code 2**, never a
  traceback. That covers bad config, a missing or invalid patterns file, a bad regex, a missing replay file, PINE
  unreachable at `--live` startup, and nan/inf from `--speed`/`--idle-timeout` (`--speed` must be finite and >= 0;
  `--idle-timeout` finite and > 0). The patterns file and replay file are checked before the run banner is printed.
  If the live source fails after startup (PCSX2 closed), the GUI status line and the console show it rather than going quiet;
  a reconnect clears it. Console: a closed stdout (broken pipe) exits quietly. With `show_group`, the live table has
  room for (or scrolls to) any number of actors.
- **persistence** (C): several app launches may run at the same time (e.g. two emulators). Every save takes a lock
  (lock file next to the day file), re-reads the file, replaces only this run's entry by `run_id` and keeps every other
  run, then writes atomically. `run_id` must be unique even for launches in the same second (add a suffix).
- **parser** (B): never raises on any input line. Lines longer than 512 characters after strip are unmatched without
  regex matching (bounds regex cost). An amount group longer than 9 digits makes the line unmatched. Unmatched entries
  escape backslash, `\r \n \t`, every other character `str.splitlines()` splits on (`\x0b \x0c \x1c \x1d \x1e \x85  
   `) and other C0 controls as `\xNN`/`\uNNNN`. The file is written with `errors="backslashreplace"` so lone
  surrogates cannot crash it.
- **stats** (B): no overflow on any int amount the parser can produce. Since the parser caps amounts, this is just a test.
- **sources/ReplaySource** (B): a line whose ts is lower than the previous yielded ts is yielded with ts clamped to the
  previous ts, and a warning goes to stderr. Yielded ts are non-decreasing.
- **pipeline** (B, doc only): unmatched and ignored lines advance the clock like heartbeats (current behaviour, now contract).
- **PineBufferSource** (A, ASSUMED layouts, still UNVERIFIED): scroll layout: a torn or partially written newest line must
  not cause older lines to be re-emitted. Treat the newest line in a window as provisional until it is followed by another
  line or unchanged across polls, and match the overlap so the previous provisional tail can grow. Ring layout: a message
  that wraps past the end of the region is reassembled from the tail and the head. Both stay marked UNVERIFIED.

# Phase 2: packet source (authoritative; written by the main chat 2026-10-17)

EQOA has no combat text. Damage comes from the game server's UDP packets; see `docs/protocol-notes.md` for the
evidence. The text parser, `patterns.yaml` and `PineBufferSource` stay as they are (synthetic replay tests use them)
but are not used for live play. Every byte-level fact below is from 2 real captures. Anything marked **INFERRED** must
keep that label in code docstrings until confirmed against more captures.

## Ownership (Phase 2)
| Path | Owner |
|---|---|
| `eqoa_parser/net/__init__.py`, `net/pcapng.py`, `net/protocol.py`, `net/attribution.py`, `net/sources.py` (`PcapReplaySource`, `PacketSource` base), Event support in `pipeline.py`, `tests/test_net_*.py` (except capture tests) | B |
| `eqoa_parser/net/capture.py` (live capture), `docs/live-capture.md`, `tests/test_net_capture*.py` | A |
| `--pcap` / `--live` wiring in `app.py`, UI changes, `tests/test_cli*.py` additions | C (after A and B report) |
| `fixtures/real/*`, `events.py` (`KIND_HEAL` added), `config.py` (`NetConfig` added), this document | main |

## Datagrams (`net/pcapng.py`, B)
`UdpDatagram(ts: float, src_ip: str, src_port: int, dst_ip: str, dst_port: int, payload: bytes)` (frozen dataclass).
`read_pcapng(path) -> Iterator[UdpDatagram]`: stdlib only. Handles SHB/IDB/EPB, both endians, link types Ethernet (1)
and raw IPv4 (101, 228); skips anything else. pktmon logs each packet once per network component, so drop a datagram
whose (src, dst, ports, payload) equals one seen less than 50 ms earlier.
Live capture (A) yields the same `UdpDatagram` type.

## Protocol (`net/protocol.py`, B)
Only **server -> client** datagrams (`src_ip == net.server_ip`) are decoded. Per datagram:
- `header_low16 = u16 LE of payload[0:2]` = low 16 bits of the logged-in character's entity ID (CONFIRMED on 3 logins).
- The last 4 bytes are a checksum (INFERRED) and are never part of a message.
- Messages: `0xFB <len u8> <seq u16 LE> <body: len bytes>`, where body starts with a u16 LE opcode. The outer header
  layout is not decoded; scan for `0xFB` and accept a message only if it fits before the checksum and its body is valid
  for its opcode. Unknown opcodes are skipped.
- `0x00DB HpChange`: `db 00 <source u32 LE> <amount zigzag varint> <target u32 LE> [extra bytes]`. Base length =
  2+4+len(varint)+4. If `len` is greater than the base length, the record is **extended** (INFERRED: pet action
  credited to its owner; all 36 pet records in cap2 were extended, and none of 69 player records were).
  Negative amount = damage, positive = heal (CONFIRMED).
- `0x00B1 Swing`: `b1 00 <attacker u32> <target u32> <extra i32> <tail>` (INFERRED: NPC/pet melee swing start; the
  player's own melee has no swing record).
- `0x00CD CastBar`: own character's cast bar (INFERRED; not used for events yet).
- Server re-sends: dedupe per connection (client port) on `(seq, body)`.
API: `decode_datagram(dgram) -> list[Record]`; record dataclasses `HpChange(ts, conn, header_low16, seq, source,
amount, target, extended, raw: bytes)` and `Swing(...)`. Here `conn` = client port, which identifies the emulator.

## Attribution (`net/attribution.py`, B)
`Attributor(config)` with `feed(record) -> list[Event]`, one per connection:
- **Player id** = the first entity id seen in that connection's records whose low 16 bits equal `header_low16`.
- **Pet id** (needed for damage *to* the pet) = the attacker of a `Swing` at target T, other than the player, followed
  within 1.0 s by an extended `HpChange` from the player at T. The first such attacker wins. INFERRED.
- `HpChange` amount <= 0 (damage; 0 counts as a 0-damage landed hit):
  - source == player → `damage`, actor `pet` if extended else `you`, target = name(target)
  - target == player → `damage_taken`, actor = name(source), target `you`; target == pet → target `pet`
  - otherwise → `damage`, actor = name(source) (other players or NPCs; shown only with `show_group`)
- amount > 0 → `heal`, actor = name(source) (`you`/`pet`/hex), target = name(target).
- Names: `you`, `pet`, otherwise `"0x%08X"`. Entity names aren't decoded yet.
- `ability` = None for everything (not decoded yet). `raw` = `record.raw.hex(" ")`.
- Connection selection: `config.net.local_port` if set; otherwise follow the **first connection that produces an
  HpChange** and ignore the others, logging once to stderr which connection and player id was picked and which were ignored.

## Sources and Engine (B)
- `LogSource` items may now also be `(ts, Event)`: `Engine.step` feeds an Event straight to the segmenter (no parser).
  HEARTBEAT still means a tick. Existing text behaviour is unchanged.
- `PcapReplaySource(path, config, speed=0.0)` in `net/sources.py` yields `(ts, Event)` in capture order, with
  heartbeats while sleeping like `ReplaySource`. Event ts = datagram ts.
- The live source wraps A's capture with the same protocol + attribution code: `LivePacketSource(config)` in
  `net/sources.py`, built on `net/capture.py`.

## Live capture (`net/capture.py`, A)
`RawCapture(config.net)`: a context manager yielding `UdpDatagram` from the game server, using stdlib only. Windows: a
raw IPv4 socket bound to the local interface with `SIO_RCVALL` (needs Administrator). Parse the IP and UDP headers
yourself. Not admin → raise `CaptureError` with a clear "run as Administrator" message. Linux: `AF_PACKET` or a raw
socket (needs root/CAP_NET_RAW). It may be written and unit-tested with fake packets, but it is UNVERIFIED until run
on Windows as admin. It is read-only by nature (nothing is ever sent). Interface choice: `net.interface_ip`, or
auto-detect the IP of the interface that routes to `server_ip` (e.g. a UDP `connect()` trick, sending nothing).

## Oracle
`fixtures/real/expected_records.json` lists every unique HpChange in both captures as decoded by the main chat's
own independent scratch decoder. B's decoder must reproduce it exactly: same records, order, amounts and extended
flags. Attribution acceptance on cap2: bard connection → 56 `damage` (you → 0x01312F0A), 56 `damage_taken` of 21,
56 `heal` of 300; necro connection → 27 `damage` pet 25 + 9 `damage` pet 0 (the pet id is 0x01374606). On cap1 →
13 `damage` from you with the user's numbers in order.

## Phase 2 amendments after verification (session D, docs/VERIFICATION.md "Phase 2")
- **config** (main, done): `net.local_port` must be 1-65535 or null; `net.server_ip` and `net.interface_ip` must be IPv4
  (interface_ip may be null).
- **Unrelated combat is dropped (B)** — replaces the `otherwise -> damage` rule. An HpChange where neither source nor
  target is the player or the learned pet produces **no Event** (count it in `Attributor.dropped_unrelated`). Combat
  between other entities nearby must not start or extend your fights. This will be revisited once group members can be
  identified.
- **Live idle clock (B):** `LivePacketSource` yields a HEARTBEAT `(time.time(), HEARTBEAT)` after any datagram that produces no
  Event, and at least every `net.poll_interval` seconds of wall time. A finished fight must close `idle_timeout` after its last
  activity even while the server keeps sending.
- **Truncated frames (B):** `read_pcapng` drops an EPB whose captured length is shorter than its original length (snaplen).
  The pcapng reader and `RawCapture` must agree on what is dropped.
- **Time order (B):** `PcapReplaySource` yields non-decreasing ts; a regressing datagram ts is clamped to the previous one,
  as in `ReplaySource`.
- **Not a pcapng (B + C):** `PcapReplaySource` validates the section header when constructed and raises `ValueError`. The app
  turns that into `error: ...` and exit 2 before the run banner (C). An empty file, a truncated file, or a file with no server
  traffic prints a one-line notice on stderr (e.g. "no game-server traffic found in FILE") instead of a silent "No fights."
- Deferred (documented risks, no change now): player-id low16 collisions, pet relearning after re-summon, group members
  swinging just before your pet's hit, auto-selection after a reconnect from a new client port.
- N2-1b (main, done): `net/pcapng.parse_ip_udp` now applies RawCapture's length rules exactly (IP total 0 = frame length;
  an IP total or UDP length that runs past the data is dropped, not clamped). An older verifier test asserting the clamp was
  updated to match.

## Phase 2b: entity names (B; INFERRED, best effort)
`net/names.py`: `NameTable` with `feed(dgram)` (server datagrams, all connections) and `get(entity_id) -> str | None`.
Rule (from `fixtures/real/expected_names.json`): in a server payload, find a byte `0x80|len` (len 3..32) followed by `0x04`,
then `len` bytes matching `[A-Za-z][A-Za-z' -]+`; the entity id is the u32 LE immediately before the nearest preceding
`82 31` within 120 bytes. Strip trailing spaces; a later name for the same id replaces the earlier one. The object stream is
partly compressed, so many names are missed (fragments like "ing Bones"). Never guess: unknown ids keep the hex name.
`Attributor` (or `ConnectionRouter`) uses the table for actor/target names other than `you`/`pet`. Names may arrive
*after* an entity was first referenced. The name is resolved when each Event is created, so if a name arrives
mid-fight, the earlier hex name and the later real name count as two targets in that fight (accepted MVP limitation,
documented; Event has no id field and stays unchanged). The parser must stay correct if no names are ever found.

# Phase 3: multi-emulator view and portable exe (main chat, 2026-10-18)

## Following several emulators (B core, C UI)
- **Default changes:** with `net.local_port` null (no `--connection`), the meter follows **every** connection (every client
  port talking to the game server), not just the first. `--connection PORT` still restricts it to that one connection.
- **Primary connection** = the first connection that produces an Event. Its player and pet keep the actor names `you` and
  `pet` (single-emulator behaviour is unchanged).
- **Other connections:** actor `label` for the player and `"<label>'s pet"` for its pet. `label` is the character's name
  from the NameTable if known when that connection's first Event is created, else `"char:<port>"`. **A connection's label is
  fixed for the whole run** (stability beats a late name). The same rule applies to targets: when the target is another
  connection's player, use that connection's label.
- **No double counting:** each connection's Attributor still drops records involving neither its player nor its pet, so a
  character's damage is counted only from its own connection. Heals may appear from both sides; they aren't shown.
- **Own actors:** the packet source exposes `own_actors() -> list[str]`, all actor names it emits for followed connections,
  in first-seen order (`you`, `pet` first when present). `summarize_fight(fight, *, ongoing=False, own_actors=None)` and the
  Engine pass it through: `combined_total`, `combined_dps` and `top_abilities` cover `own_actors` (None = the old you+pet
  rule, so the text path and all old tests are unchanged). `FightSummary.to_dict` schema stays the same, so persistence is
  untouched.
- **Dropping unrelated combat is per connection and unchanged.**
- **Status line / console header (C):** list the followed characters, e.g. `following: you (0x174A1A40, port 21173),
  Zecron (port 20605)`, updated as connections appear.
- **UI rows (C):** live panel and console show one row per own actor (you, pet, then the others in first-seen order), plus
  other actors only with `--show-group`. The fight-list columns You and Pet stay. Add a compact "Others" column (the summed
  damage/DPS of the other own actors) only if it fits the 500px width; otherwise show others in the detail line under the
  selected fight. Combined = all own actors.
- **Acceptance on cap2 with default settings** (both connections, at different times): fight 1 = the bard (primary) as
  `you`, 20,670 over 56 hits; fight 2 = the necro connection as a non-primary: its pet has 675 over 36 hits, with the
  label per the rule above (report which one it got), and `you` 0. Combined for fight 2 = 675. `--connection 20605` keeps the
  old result (necro = you, pet = pet).

## Portable exe (A)
`packaging/` with a PyInstaller spec (or build script) producing `dist/EQOAParser.exe`: one-file, windowed (GUI) build of
`python -m eqoa_parser --live` with `--uac-admin`, so Windows asks for admin on launch. It must also work with a second build
or flag for `--no-gui` console use, or document that the exe accepts the normal CLI flags. `config.yaml` is looked for next to
the exe first, then the bundled default; `runs/` and `unmatched.log` go next to the exe, never into the temp extraction folder.
PyInstaller is a **build-time** dependency only: add `requirements-dev.txt` and leave `requirements.txt` as is. Building
the exe must not change any runtime module except where needed to locate the config/runs folder when frozen
(`sys.frozen`), and that change must be tiny and coordinated with C (it's in app.py/config path handling).

## Phase 3 amendments after verification (session D, docs/VERIFICATION.md "Phase 3")
- **Pet learning (B):** a swing attacker is never accepted as my pet if it is a followed connection's player, or the
  learned pet of another followed connection (the router shares this). A candidate becomes the pet only after it preceded
  (within 1.0 s, same target) at least **2** of my extended hits with no other candidate doing so more often. A pet may be
  re-learned the same way, e.g. after a re-summon. Pet *damage* never depends on the pet id: it comes from extended records
  credited to the owner. Remove the "learned pet itself is the source → pet damage" branch; such records are dropped like
  any unrelated record. The pet id is used only for damage **taken** by the pet.
- **Unique labels (B):** if a non-primary connection's label is already taken in the run, append ` (2)`, ` (3)`, …
- **Own actors per fight (B + C):** `FightSummary` gains `own_actors: list[str] | None = None`. `summarize_fight` stores the
  list it used; `from_dict` accepts its absence (old files → None → the you+pet rule). The schema stays version 1, since the
  field is additive. The UI and console use the fight's own `own_actors` for its rows and its "Others" line, never the
  current run's list (fixes reloaded fights).

# Phase 4: per-character display (main chat, 2026-10-18)
User request: show character names instead of "you", and break every view down **per character**:
`Zaptir / Zaptir's pet / Zaptir combined`, then `Maige / Maige's pet / Maige combined`, and so on. Nothing should be
added across characters except in one clearly labelled "All characters" line.

## Own character name (net, INFERRED from 1 sample)
At login or zone-in, the server sends on the character's own connection a message with the zone file followed by
the name: `...data\tunaria.esf <varint> <u32 LE n> <n ASCII letters>`. Rule: after `.esf`, skip one varint, read u32 n
(3..20), then n bytes matching `[A-Za-z]+`. That's the connection's own character name (cap2: port 20605 → "Zecron" at
t+114 s, before its combat). Only names from that connection's own server traffic count.

## Display names vs actor keys
Internal actor keys don't change (`you`, `pet`, `<label>`, `<label>'s pet`), so stats, segmentation and stored actor
dicts keep working. On top of them:
- Each followed connection is a **character group** with a display name, chosen in this order: the own-name rule above,
  then the NameTable name of the player id, then `You` (primary) or the existing label. **Display names may update live**
  when a name arrives later (display only; keys stay fixed).
- The packet source exposes `character_groups() -> list[CharacterGroup]` in first-seen order (primary first). Each has
  `name` (display), `player_key`, `pet_key`, `port`, `entity_id`.
- `FightSummary` gains `groups: list[dict] | None = None`: `[{"name", "player", "pet"}]`, with the display names as known
  when the fight closed, stored per fight (additive, schema stays 1, absent = None). Per-group combined total/DPS is computed
  from `actors`. Old fights and the synthetic text path: None, meaning one group named "You" with player `you` and pet `pet`.
- A small pure helper (core, no UI imports), e.g. `stats.group_rows(summary) -> list[GroupRow(name, player: ActorStats,
  pet: ActorStats, combined_total, combined_dps)]`, so the console and Tk use the same numbers. Group combined DPS = group
  total / effective_duration(fight).

## Views
- **Live panel and console:** for each group: `Name`, `Name's pet`, `Name combined` (visually set off, e.g. bold or
  indented). With 2+ groups, a final `All characters` row. Columns stay total / DPS / max hit (combined rows: total / DPS).
  Groups with no damage in this fight still show (0s) if they're followed. With 1 group, there's no All row.
- **Fight list:** one row per fight. Columns: Run, #, Target, Dur, then a compact **per-character combined** cell per group
  ("Zecron 675/8.1") when it fits; otherwise one "Characters" column with "Zecron 675 · Zaptir 20670". The selected fight's
  detail area shows the full grouped breakdown (as in the live panel) and the top abilities.
- **Run summary:** one line per character: name, total damage, DPS (total over the summed effective durations of the
  fights where that group dealt damage), best fight. Then the `All characters` line. Characters are matched across fights
  by display name.
- Pet rows read `Name's pet`. The old You/Pet wording stays only where no name is known.
- Window width stays about 500px; heights may grow and the panels scroll.

## Acceptance
- cap2 default: fight 1 shows a group named `You` (the bard's name isn't in the capture) with 20,670, plus a `Zecron`
  group at 0 if already followed by then (it isn't: Zecron's connection first appears later, so fight 1 has only the bard
  group). Fight 2 shows `Zecron` 0, `Zecron's pet` 675, `Zecron combined` 675. Run summary: `You 20,670` and `Zecron 675`
  on separate lines, then `All characters 21,345`.
- cap2 `--connection 20605`: one group `Zecron` / `Zecron's pet` 675 / `Zecron combined` 675.
- cap1: one group (the necro was already logged in, so `You`) 7,218.
- Reloading saved fights keeps their groups and names.
