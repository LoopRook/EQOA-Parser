# Richer metrics: what the packets give us, and what Parsec (SWTOR) suggests

Research note, 2026-10-19. No code changed. Labels: **[OBSERVED]** = counted or read in a capture by a throwaway script;
**[READ]** = taken from a document or web page; **[INFER]** = my interpretation, not yet tested in game.
Captures used: `cap1`/`cap2`/`cap5` (public fixtures), plus the private ~9 minute two-character capture (called **cap-long**
here) and three short pre-release captures. Character names and raw bytes are deliberately left out.

## 1. What we can extract today

Basis: the server sends each client only records that involve *that client's own character or pet* (CONFIRMED earlier),
plus a lot of non-combat traffic. Everything below is per client connection (one emulator = one UDP port).

### 1.1 Opcode tally (cap-long, both connections, de-duplicated by connection/seq/body)
| Opcode | Count | Meaning | Confidence |
|---|---|---|---|
| `0x00B1` | 937 | swing / attack-round start (see 1.4) | medium |
| `0x00DB` | 392 | HP change (damage / heal) | high |
| `0x0A7A` | 226 | **system text** (UTF-16): XP, faction, loot, debt, "you receive N tunar" | high |
| `0x00CD` | 182 | own cast bar / ability use (8-10 bytes) | medium |
| `0x0060` | 78 | slowly stepping value, 2 variants (u32 = 6 or 7, then a 16-bit float high half: 112, 104, 96 ... 4, 1.9). Not mob HP; looks like one of *my own* meters (power/cooldown?). Unexplained | low |
| `0x0A7B` | 56 | **system/chat text** (UTF-16): errors and `X shouts:` | high |
| `0x07E0` | 34 | 2-byte heartbeat, every ~16.4 s | high |
| `0x0020`, `0x00D9`, `0x0052` | 21, 21, 19 | all fire together at each kill; `0xD9` = remaining XP debt as u32 (matches the debt in the `0A7A` text), `0x20` = constant `78 00`, `0x52` = u16 counter | medium |
| `0x069E` | 6 | group chat line (`X tells the group:`) with the speaker's entity id | high |
| `0x0013` | 9 | every ~61.6 s, two 8-byte values: clock/keep-alive | low |
| `0x1003`/`0x1004` | few | length-prefixed ability/effect name (e.g. `Wilting Flesh`, `Frenzied Spirit` in cap2) | medium |

Many "opcodes" with count 1 are not opcodes: they are sequence numbers read from a different message layout (they all start
`xx xx 10 00 00 01 01`). Also, when the character id's low byte pair contains `0xFB`, a naive `0xFB` scan can match inside the
datagram header; the repo's decoder survives because it only accepts known opcodes. **[OBSERVED]** both effects; counting
opcodes properly needs the real outer framing, which is still undecoded.

### 1.2 Damage taken (you and your pet)
- **You: yes, [OBSERVED].** cap2 (bard vs a training dummy): 56 hits of -21 = -1,176 from one source, matched 1:1 by 56 `+300` self heal ticks.
  cap-long, healer-type character: 35 hits from 3 mobs (-83 .. -897, total -8,845; 15/10/10 per mob) in 510 s.
  Per-source tables (who hit me, how hard) are straightforward. Confidence **high**.
- **Pet: no.** [OBSERVED] in cap2/cap5/cap-long the pet attacks a lot (36 extended records in cap5, 186 in cap-long) but no record
  has the pet as *target* of damage. Mobs do `b1`-swing at the pet (cap2: dummy swings at pet every ~6 s) but no `db` record
  follows. [INFER] pet HP loss is simply not sent as `db 00`; so "damage taken by pet" is **not available** from packets.
- Dodges/absorbs: nothing seen (see 1.4).

### 1.3 Heals
- **Received (self HoT, other healers): yes, [OBSERVED].** cap2: 56 x +300 from a HoT (credited to the *enemy* as source, a quirk).
  cap-long: own self heals 34 (+9,506; mostly 300 and 296 = HoT ticks, a few 781); a groupmate's heals on me: 2 records (+1,562)
  in 510 s, because the server sends only records involving *me*. High confidence for what shows up, **low coverage** of the heals I receive.
- **Given: yes for my own casts, [OBSERVED].** cap-long healer: 55 heals on one groupmate (+16,432), 34 on self, 5 on each of two others
  (+1,492 each): ~170 heals, ~29k total. Heal target identity is in the record, so per-target "healing done" is possible.
- **Heals by groupmates on third parties: impossible** from packets (never sent). PINE group roster could estimate it
  (HP percent over time) but not attribute it.
- **Overhealing: not available.** A heal record is the amount *applied*? or *rolled*? Unknown, and no record carries HP before/after.
  [INFER] it needs target max HP/current HP from PINE (entity list / group roster) and a guess about whether the amount is clipped.
  Test required (see section 3).

### 1.4 Misses, blocks, resists
- **Amount-0 `db` records exist but rarely:** 9 in cap2, all *extended* (pet) records, all same source/target; 0 in cap-long, cap5. [INFER]
  pet attack that did nothing (miss/resist/cast without damage). The meter (and the notes) already treat necro 0-damage casts this way.
- **`b1` swings are not 1:1 with hits, so "swing without hit = miss" is unsafe.** [OBSERVED] cap2: dummy emits 14 swings at the bard but
  56 hits (4 hits per swing). cap-long: mobs' swings at me = 14, hits on me = 35. Every swing that targeted me had a hit within 1.5 s (0 swings without a hit), so no
  miss signal exists in this data. [INFER] a `b1` is an attack *round* (several hits), and misses are probably simply not sent as `db`.
  Self-swings (attacker = target) are cast/animation noise: 178 + 31 + 29 in cap-long.
- **Failure messages are in text** (`0x0A7B`, [OBSERVED] cap-long): `Recovery time for that ability has not been met (6.3 s)` x30, `Out of range.` x10,
  `No line of sight.` x4, `Not enough power.` x3. Useful as "wasted casts / downtime" stats, not as combat misses. They reveal ability cooldowns
  (6.3 s, ~60 s) but not the ability's name.
- No block/dodge/resist/crit flag found in any `db` or `b1` record. Crit might be visible as outliers (-897, -760 vs ~-100 typical), [INFER] not proven.

### 1.5 Kills and deaths
- **Kills: yes, as group events.** [OBSERVED] cap-long: 20 kill markers on my connection (`0x0020` + `0x00D9` + `0x0052` + `0A7A` "You receive N XP / class mastery XP"),
  14 of them also on the other connection (same second). Loot lines (`X loots ...`, `You receive N tunar, your share of the group loot`) arrive 0.5-1 s later.
  Matches 20 kills in 510 s (about one per 25 s). Kills of mobs *I* did not help kill still show for group XP. Kill => encounter end
  is a good signal; **which mob** died is not named, but the last mob I hit before the marker is the usual candidate [INFER] (the same six mob ids respawn and are reused).
- `You can't cast abilities on dead creatures.` x3 at one moment = target already dead (cap-long, ~376 s).
- **Deaths: not found.** I searched all payloads (ASCII and UTF-16) for slain / died / dead / corpse / resurrect / lost / release: only the "dead creatures" message.
  The only damage-taken events on the character connection are normal hits and nothing stands out at a death. So **no death signal is known**, and I could not
  separate deaths from the data without timestamps for them. A controlled capture is needed (section 3).
- **Mob HP over time: not in packets.** `db` records have no HP-after field and no opcode I found steps like a mob HP bar (`0x0060` is
  something of mine). Estimate: cumulative damage on a mob from my records only, which undercounts group damage. Real mob HP percent requires **PINE**
  (entity list HP fraction, per `docs/research/frontiersforge.md`), polled ~2-4 Hz. Group member HP percent too.

### 1.6 Casts and ability use
- `0x00CD` fires 182 times in cap-long (also 8 in cap5 for 8 casts, 4 in cap2); bodies are 8-10 bytes with one or two small ids, a short trailing value. Counts line up with
  cast attempts, not with `db` records. [INFER] u16 after the opcode (0x37, 0x28) is an ability/cast-class id and the rest a recovery/timer; not decoded.
  Ability *names* are not in `db`. Names appear in `0x1003`/`0x1004` effect records and in the PINE spellbook. So per-ability breakdown needs a mapping from 
  `db` to ability, and none was found: **no ability id in `db 00`**. [OBSERVED] for all 556+ `db` records.
- Own melee has no `b1` (CONFIRMED earlier). Pet melee has `b1` 0.5 s before the hit.

### 1.7 Everything else worth knowing
- **Groupmates:** their swings *are* visible (`b1`: one groupmate swung at mobs 446 times in cap-long, the three mobs that also hit me swung at them about 58 times) but their damage and the damage they take are
  not. So *group activity* (swing counts, who the mobs target) is knowable; *group damage* is not. Group chat is also visible (0x069E).
- **Chat/system text** is a goldmine for session stats: XP per kill (class mastery XP 166,253 then 665,012 as level changed), faction changes, loot, tunar, XP-debt.
- Time: datagram timestamps are good to ~10-50 ms; mob swing, then hit, is ~0.1-0.5 s. Enough for DPS windows.

### 1.8 Impossible / needs PINE
| Wanted | Verdict |
|---|---|
| Groupmates' damage done | **Impossible** from packets (server filters). PINE: not found either (see protocol-notes). |
| Damage taken by groupmates or pet | Impossible from packets. PINE group roster HP percent shows *that* HP dropped, not who did it. |
| Heals between others | Impossible from packets; HP-percent deltas only via PINE. |
| Overheal | Needs PINE HP/max HP (own and group). |
| Mob HP percent | Needs PINE entity list. |
| Ability names for damage | Needs a link between cast events and `db` records (time correlation with `0x00CD`/spellbook), unproven. |
| Deaths | Unknown; maybe PINE (HP = 0) or an undiscovered opcode. |

## 2. Parsec (SWTOR) and fit for EQOA Parser

Sources are thin: Parsec is a 2013-era .NET ClickOnce app; its official pages are gone or return 403 for my fetches (forum thread
"Introducing Parsec - a new raid aware real time combat log parser", swtor.com archive mirror). What follows is from search snippets of that thread
and a few forum replies, so **[READ] second-hand and incomplete**; treat feature lists as approximate. Newer SWTOR tools (StarParse, Parsely,
a GitHub "DPS" parser) are mentioned only where they fill gaps, flagged as such.

| Feature | What it does [READ] | EQOA Parser? | Why |
|---|---|---|---|
| Damage Dealt tab | totals and per-second | **Yes (own+pet)** | Already built; own and pet damage only. |
| Damage Taken tab (DTPS) | per-source/ability incoming damage, incl. mitigation stats (shield/defense %) | **Partially** | I see my own incoming hits per source; no mitigation data, no pet. |
| Healing Done / HPS | per-player and per-ability | **Partially** | Only heals by *me* (to anyone) and heals *to* me; not group-wide. |
| Healing Received | what each player receives | **Partially** | Only me; coverage of others' heals on me is incomplete. |
| Threat tab | per-player threat | **No** | No threat data anywhere in packets. |
| Raid tab / scoreboard | everyone's DPS/HPS/DTPS in one table | **No (packets) / partially (shared)** | Server filters other players' records. Possible only if each player runs the tool and shares totals (what Parsec did). |
| Raid channel sharing | players join a shared channel and exchange *totals* (full logs too heavy) | **Partially, later** | Feasible as opt-in aggregation, needs a server and a stable record format; also each player must capture packets (admin). |
| Per-ability breakdown | damage/heal split by ability | **No (today)** | No ability id in `db`; cast events not linked. |
| Combat-end window | waits ~1 s after combat end to catch late events | **Yes** | Already segmenting; the kill marker (1.5) gives a clean end signal. |
| Pop-out stat windows / overlays | small always-on-top DPS/HPS windows, stopwatch for PvP | **Yes** | UI task only. |
| Keyword filter | search the log | **Partially** | Possible on system text and chat, not on a combat log that does not exist. |
| Timeline / graphs, death list, replay (Parsely, StarParse, not Parsec) | DPS over time, group deaths, interactive replay | **Partially** | Own-only DPS/DTPS/HPS timeline: yes. Deaths unknown; replay is own-view only. |
| Encounter detection | combat start/end, per-fight stats | **Yes** | Segmenter exists; kills = clean end markers. |
| Absorbs | not logged for own shield in SWTOR either [READ] | **No** | No absorb concept visible. |

## 3. Recommendation (prioritised)

All items below assume only the connection that is *yours*; every table must say "you and your pet" so it is not misread as group data.

1. **Damage taken (DTPS) + per-source breakdown.** Data already in `HpChange` (target = own id, amount < 0). Effort **S** (stats + one table
   + a timeline row). No new capture needed; cap-long has 35 hits to test on. Also show hits-per-source and biggest hit.
2. **Healing: done (HPS) and received.** Split into: heals I cast, by target; HoT/other-source heals on me. Effort **S-M**. Label overheal as "n/a". No new capture; a group-healer
   capture would be a nice extra test.
3. **Kill/encounter markers from the XP block** (`0x0020`/`0x00D9`/`0A7A`). Effort **M**. Gives fight end, kill count, kills per hour, XP per kill and per hour, tunar and loot. Needs `0A7A` text
   decoding (UTF-16 length-prefixed). Reasonable to ship alongside #1. No new capture, though more kill/XP varieties would help.
4. **Own-only timeline view** (DPS / DTPS / HPS per second, plus kill ticks). Effort **M**, depends on #1-#3. No capture.
5. **Wasted-cast stats** from `0A7B` failures (cooldown, out of range, LoS, no power), count and time lost. Effort **S**. Fun, honest, cheap.
6. **PINE: mob HP percent and group HP percent timeline, overheal estimate.** Effort **L** (polling thread, timing, data model); optional like the existing PINE enrichment.
   Needs captures with PINE running at the same time (below).
7. **Per-ability breakdown.** Effort **L**, uncertain. Needs the cast to damage link first.
8. **Group totals sharing (Parsec raid channel).** Effort **XL**; park until single-player metrics are solid.

### Captures I would ask for (all raw, local; with PINE running where noted)
1. **Death capture.** Play a character at low HP, die to a mob (note game time or say "now" in group chat when you die and again when you
   release/resurrect), then recover. Ideally also let your *pet* die once. Goal: find the death opcode / text and see whether pet damage taken appears.
2. **Overheal test (PINE on).** Cleric/healer or HoT user: heal a groupmate at 100% HP, at 50% and at 5% missing, with known max HP; write down the floating numbers seen. Goal: does the
   record carry the rolled or the applied amount?
3. **Miss/resist test.** Fight a much higher-level or lower-level mob with a melee class for 3-4 minutes and note every "miss/dodge/resist" shown on screen (a screen
   recording works) to see whether any record, or lack of one, corresponds.
4. **Ability attribution.** Cast four *different* spells in a fixed order with a pause between, none overlapping, and note names and numbers; repeat melee special abilities the same way. Goal: link `0x00CD` ids to `db` records.
5. **Mob HP (PINE on).** Solo-kill one mob while a script logs the PINE entity HP fraction at 4 Hz; note that mob's displayed level. Compare with the cumulative damage and the kill marker.

### Open uncertainties (honest list)
- Whether cap-long contains deaths is unknown: no death signal exists in what I decoded, and I cannot tell without the times.
- `0x0060` and `0x00CD` meanings are guesses.
- The Parsec feature table comes from search-result snippets and forum comments, not from its own documentation.
