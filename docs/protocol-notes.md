# EQOA combat data: findings so far (Phase 2 research)

Status as of 2026-10-17. Each finding is marked **CONFIRMED** (matched against what you saw in game) or **INFERRED**
(fits the data, but not yet tested against something you observed).

## The Phase 1 premise was wrong
EQOA has **no combat text log**; damage only appears as floating pop-up numbers. `patterns.yaml`, the text parser and
`PineBufferSource` were built for a text buffer that does not exist. The Event / segmenter / stats / UI / persistence
layers are unaffected.

## Game memory (EE RAM via PINE): dead end for damage
Read-only experiments with full 32 MB snapshots about every 2.4 s found no persistent copy of the damage numbers. Encodings tried:
u16/u32/f32, negative, big-endian, ASCII and UTF-16 digits. A sequence of 20 known spell hits matched no address, and no
target-HP value dropped by the total damage. PINE itself works against PCSX2 2.8.2 (see `docs/PHASE1-REPORT.md`).

## Network packets: damage found
PCSX2 network mode is DEV9 **Sockets**, so game traffic is ordinary UDP from `pcsx2-qt.exe`. The Sandstorm server is
`5.161.217.188`, mostly port `10070` (`10071` was also seen). Captured with the built-in Windows `pktmon` (admin):
`captures/eqoa.pcapng` (necro spells) and `captures/eqoa2.pcapng` (bard melee + heal; necro pet on a dummy).

### Packet framing
- **CONFIRMED (2 captures, 3 characters):** the first 2 bytes of every packet are the low 16 bits of the logged-in
  character's entity ID (`d8 89` = necro `0x13FF89D8`, `40 1a` = bard `0x174A1A40`, `82 cb` = necro `0x1751CB82`).
  This lets us tell the two emulators and characters apart.
- **INFERRED:** the last 4 bytes are a checksum. Messages travel on numbered channels `fb <ch> <seq varint>` (`fb 0b`,
  `fb 0c`, `fb 0e` seen), each with its own sequence counter. The server re-sends messages, so dedupe on (channel, seq).
- **CONFIRMED:** entity IDs change between logins (the necro was `0x13FF89D8`, later `0x1751CB82`).

### `db 00`: HP change record
`db 00 <source id u32 LE> <amount: zigzag varint> <target id u32 LE>`
- **CONFIRMED:** 13/13 necro spell hits in order (−598, −599, −444, …), and bard melee in the reported 3xx–4xx range.
- **CONFIRMED:** negative = damage, positive = heal (+300 heal ticks on the bard).
- **CONFIRMED:** the dummy hits back (dummy → bard −21, once per swing).
- **INFERRED:** **pet damage is credited to the owner** as source (pet 25s appear as necro `0x1751CB82` → dummy −25).
  The +300 heal records name the dummy as source; that may be how procs are credited.
- **INFERRED:** necro 0-damage casts appear as `db 00` with amount 0 from the necro.

### `b1 00`: swing/attack record
`b1 00 <attacker id u32> <target id u32> <u32 extra>`
- **INFERRED:** the pet (`0x01374606`) swings at the dummy (`extra` 768000) about 0.5 s before each −25 `db 00` record,
  and the dummy swings at the pet every ~6 s (`extra` 1536000). Pairing `db 00` with a preceding `b1 00` from the pet
  is the current plan for telling pet damage from yours.
- Capture 1 had 13 `fb 08` messages, one per spell cast. They may be cast records (unexamined).

## Open questions for the next session
1. Pet vs you attribution: confirm the `b1 00` pairing (e.g. pet attacks alone, then you melee alone, same character).
2. Your own melee: does it also produce a `b1 00` swing with your ID, and spells an `fb 08` cast?
3. Misses, kills, and other players' damage (for the later group view).
4. Live capture method, a design decision: `pktmon` real-time mode or a Python raw socket (both need admin), or Npcap.
   Then a `PacketSource` that yields Events directly, replacing the text parser for live use.

Scratch analysis scripts (pcapng reader, sequence matcher, record decoder) are in the session scratchpad. They'll be
rebuilt as proper modules with tests when this becomes code.

## Update 2026-10-19: pet attribution confirmed
A controlled capture on a magician (`fixtures/real/cap5_mage_spells_then_pet.pcapng`, covered by `tests/test_real_cap5.py`)
settles the you-vs-pet question:
- **CONFIRMED:** the character's own spells arrive as **plain** `db 00` records: all 8 casts the user saw, in order.
- **CONFIRMED:** every pet action arrives as an **extended** `db 00` record credited to the owner. That covers melee (~400-640,
  with a pet `b1 00` swing 0.5 s before), pet abilities (1,144-2,817, no swing) and 1-damage procs.
- **Trailer:** the extra 3 bytes are a colour. FrontiersForge reads them as an RGB number colour; this pet's are always
  `ff 00 00` (red). The necro pet in cap2 used `1a e6 db`, so the colour may vary, but any trailer means "pet". The meter
  keys on the presence of the trailer, not the colour.
- Seen in a 2-emulator capture: a player's own melee *can* have `b1 00` swing records (on the other emulator's connection).
  The meter doesn't rely on swing records for player damage.
- `tools/sanitize_pcapng.py` turns a raw capture into a publishable fixture (server packets only, scrubbed headers).
