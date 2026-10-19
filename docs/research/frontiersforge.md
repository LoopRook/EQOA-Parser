# FrontiersForge research (read-only study)

Repo: https://github.com/mmvest/FrontiersForge (shallow clone taken 2026-10-18; MIT).
Paths below are relative to that repo. Tags: **[READ]** = stated in their code (cited),
**[INFER]** = our inference. Nothing was built, run or installed. `UiForge/` is an
empty submodule (separate repo `mmvest/User-Interface-Forge`) and was not read.

## 1. What it is and how it gets data

**[READ]** A Lua addon framework for EQOA Frontiers on Sandstorm + PCSX2 (`README.md:6-12`).
`UiForge.exe` **injects a DLL into the PCSX2 process**, which exposes the emulated EE RAM
as a host pointer (`extern uintptr_t EEmem`, `scripts/modules/frontiers_forge/util.lua:7,76`)
plus ImGui drawing and a LuaJIT host. Lua mods read guest memory directly
(`EEmem + offset`, `util.lua:136-140`) every frame.
- No packet capture and no PINE. wereoxxs_ui's README: "Nothing here touches the network" (`scripts/wereoxxs_ui/README.md:3`).
- Needs: DLL injection into PCSX2, and "You may need to run the script as an administrator" (`README.md:40`); DirectX 11/12 renderer only (`README.md:22`); Windows only. Tested PCSX2 v1.7.3727, v2.0.2, v2.2.0 (`README.md:19`). Injection warning at `README.md:17`.
- **It also writes to guest memory.** `modules/frontiers_forge/cave.lua:1-20` allocates MIPS "code caves" in the EE kernel gap (0xF1000-0xF8000), and `combat.lua`, `ability_list.lua`, `chat.lua`, `input.lua` patch game instructions with `jal` hooks. Our passive sniffing has none of that risk.

## 2. How the damage meter gets combat data

**[READ]** `modules/frontiers_forge/combat.lua`. Header comment `:7-16`: "Server opcode 0xDB is the
only combat 'number' message"; the client handles it only visually, so they hook the client's
server-message dispatcher and copy every event into a ring buffer in a code cave (64 entries x 0x20 bytes, `:54-61`).
- Patch site static address `0x0062FB24` (`:20`), in the relocatable UI overlay; resolved via the overlay "slide" (`window.lua:147-157`), verified against 4 exact instruction words (`:24-29`), with a RAM scan fallback `0x300000-0x1800000` (`:43-44, 205-218`).
- The hook reads the already-parsed fields off the dispatcher's stack frame: attacker `sp+0x8DC`, amount `sp+0x8E0` (signed int32), defender `sp+0x8E4` (`:25-29, 80-83`). Amount < 0 is damage, > 0 is healing (`:450, 512`).
- **Optional 3-byte trailer** after those fields in `0xDB`: an RGB number colour, little-endian, tagged `0x1000000` (`:68-75, 84-99`). "pet swings arrive credited to the owner and colored red, 0x10000FF" (`:72-73`, `:452-454`).
- They detect and chain into the **Sandstorm server patch's own cave** at the same site, because that patch rewrites a pet event's attacker to the owner (`:7-16, 32-40`). Hooking first gives them the raw ids.

Event shape (`:446-461, 502-513`): `attacker_id, defender_id, amount, seq, color, outgoing, incoming, from_pet, to_pet, is_heal`.
Dispatched as `OnDamageDealt / OnDamageReceived / OnHealingDealt / OnHealingReceived` (`:664-669, 736-744`).

**Who counts: only the local player and their pet.**
- `outgoing = attacker == player_id or attacker == pet_id` (`:508`), `incoming = defender == player_id` (`:509`). Events between other parties are captured but never dispatched (`:736-744`).
- Local player id: `[0x4E37F0] + 0x71F4` as u32 (`:360-367`). Pet id: `[0x4E37F0] + 0x2BE4C`, `-1`/0 when none (`:369-380`); `Player.GetPetEntityId` reads `0x1FDB580` (`player.lua:241`).
- They admit pet vs owner can't be separated reliably: "Not reliable enough to split pet output from the player's, so nothing classifies on it" (`:455-459, 496-499`). wereoxxs_ui's README: meter is "fed from your own combat log alone" (`wereoxxs_ui/README.md:48-49`); `meter.lua:13-14`: "there is no server to receive anyone else's numbers". So **no party or enemy damage tables**, one row for you (`wereoxxs_ui.lua:742-760`, `solo_meter.lua`).

**Fight segmentation and DPS** (`wereoxxs_ui/modules/meter.lua`):
- Fed lifetime counters (`damage_done/taken`, `healing_done/taken`) and diffs them (`:110-145`); counters summed in `wereoxxs_ui.lua:208-231`.
- Damage opens a fight; healing never opens or extends one (`:33-37, 159-172`). A fight ends after `idle_timeout_ms = 5000` without damage (`:49, 188-196`).
- Duration = first to last damage event, excluding the idle tail (`:17-22, 210-215`). DPS = total / max(duration, 1 s) (`:40, 226-231`). Manually started windows count wall time (`:205-209`).
- History, session roster, CSV export: `damage_meter.lua:426-500`.

## 3. Names

All **[READ]**, and all from the **client's in-memory structures**, not from packets:
- **Own name:** `Player.GetName()` = C string at `0x1FBBA0C + 0x04` (`player.lua:6, 11-13`). Level `+0x24`, exp `+0x28`. Class/race ids and name tables: `player.lua:254-308`.
- **Entity list:** array of 24 u32 pointers (EE offsets) at `0x1FB6C30`; slot 0 is always the player (`entity_list.lua:7-9`). Record layout (`entity.lua`):

| Offset | Field | Line |
|---|---|---|
| +0x0C | entity id (u32) | 69 |
| +0x19 | HP fraction (u8 / 255) | 76 |
| +0x40 | x,y,z floats | 83 |
| +0x58 | name, NUL-terminated char[] | 91 |
| +0x70 | level (u8); 0 = empty slot | 98, 145-147 |
| +0x256 | class byte: 0/1 NPC, 2 player character, 3 pet | 113-135 |
| +0x262 | status flags (poison/disease/hide plate) | 157-178 |

- **Target:** id at `singleton+0xC140`, disposition 0-6 at `+0x2BB6C`, validity flag `+0x2BB64` (`entity.lua:21-39`). The u32 at `0x4E37F0` points at `singleton+4`, hence the "offset-4" chain steps (`entity.lua:21-23`); resolver `util.lua:210-224`. Current target pointer also at `0x1FBB870` (`player.lua:219`).
- **Entities already on screen at startup:** the entity list is simply the client's current table, so no spawn message is needed. Limit: **24 entities** (`max_entity_list_idx = 23`), i.e. the loaded/nearby set, not the whole zone.
- NPC names offline: `scripts/resources/entity_database.json` (1.8 MB; name -> world, level range, positions). Provenance unknown.

## 4. Group and abilities

**Group [READ]** `group.lua`: `GroupMember` struct, 0x34 bytes (`:8-20`): `+0x00 entity_id`, `+0x08 percent_hp (u8)`, `+0x09 hp_known`, `+0x0A active`, `+0x10 x/y/z` cached position, `+0x1C name[24]` inline. Located at singleton `+0x2BD78` (array), count `+0x2BE48` (includes self), in-group flag `+0x2BD70`, leader flag `+0x2BD74` (`:25-30`). **Names and entity ids of party members are here even when their entity isn't loaded**, which fills our "group membership for other players" gap. Only HP percent is known, no exact numbers (`wereoxxs_ui/README.md:37-39`).

**Abilities [READ]** `ability.lua:5-33`: spellbook records, stride `0x1D8`: `+0x10 id`, `+0x18 display_index`, `+0x24 lockout_ms`, `+0x2C level`, `+0x34 cast_time`, `+0x38 power cost`, `+0x50 name wchar_t[64]`, `+0xD0 description`. List base/count/root via singleton `+0x2BB30/+0x2BB28/+0x2BB34` (`ability_list.lua:13-23, 99`). Hotbar: `ability_bar.lua:120` (`+0x2BB74`). Buff/debuff names: wchar_t[64] array at `0x1FAF730 + 0x2C194`, count `+0x2C170` (`effects.lua:12-15`). These are **your own** spellbook and effects only.
- Cast detection: hook at the call site `0x0062EF2C` of the server **opcode 0xCD** (cooldown/lockout) handler captures `display_index` (`sp+0x855`) and lockout ms (`sp+0x88C`) (`ability_list.lua:165-173`). README: this is "the one signal the client has that an ability was just used" (`wereoxxs_ui/README.md:73-76`).
- Nothing ties an ability name to a 0xDB damage event; the 0xDB fields carry no ability id (`combat.lua:25-29, 446-461`). **No spell names for other players or monsters.**

## 5. Addresses, versions, formats

All values are **EE RAM offsets**. **[INFER]** With PINE they should be usable directly as guest addresses (all < 0x2000000), so batched `read32/read8` works. They target the **US build** (`README.md:20`); no serial or CRC is named anywhere (grep for SLUS/CRC found nothing). Static code addresses (0x62xxxx) sit in a relocatable overlay and need the slide. Patched Sandstorm clients exist (world ids 6-7 "on patched clients", `util.lua:396-398`), so offsets might differ per client build.

| What | Address | Source |
|---|---|---|
| In-game flag (u8) | `0x1FDB480` | util.lua:390-392 |
| Singleton ptr cell | `0x4E37F0` (value = singleton+4) | entity.lua:21-25 |
| Own entity id | `[0x4E37F0]+0x71F4` | combat.lua:360-367 |
| Own name | `0x1FBBA10` (`0x1FBBA0C+4`) | player.lua:6-13 |
| Pet id | `[0x4E37F0]+0x2BE4C` or `0x1FDB580` | combat.lua:369; player.lua:241 |
| Entity ptr array (24) | `0x1FB6C30` | entity_list.lua:7 |
| Group | `[0x4E37F0]+0x2BD70..0x2BE48` | group.lua:25-30 |
| World id (u32) | `0x1FB5D60` (0 Tunaria, 1 Rathe, 2 Odus...) | util.lua:396-402 |
| Dispatcher 0xDB case | `0x0062FB24`+slide | combat.lua:20 |
| 0xCD (lockout) call site | `0x0062EF2C`+slide | ability_list.lua:165 |

Packet info is only indirect (post-parse stack fields, not wire bytes): 0xDB = attacker, signed amount, defender, then optional 3-byte RGB trailer. **[INFER]** This matches our `db 00 <src u32> <zigzag amount> <tgt u32>`, and our "extended record" for pets is probably that colour trailer (red = pet swing credited to owner), not a pet id. Wire decoding (zigzag varint, `b1 00` swing records) stays ours; they use neither.

## 6. License

**[READ]** `LICENSE.txt`: MIT, Copyright (c) 2025-2026 mmvest (wereox). **[INFER]** MIT is GPL-3.0-compatible: we may reuse ideas freely and code with attribution, keeping their copyright and MIT notice on copied parts (comment header plus a third-party notes file). Not covered by that: UiForge (separate repo, licence unchecked), bundled fonts (OFL), `entity_database.json` (data of unknown origin; don't copy without asking). Memory offsets are facts, but credit them anyway. We don't contact the owner for now; any integration talk is a deliberate later step.

## 7. Recommendation

**Cheapest fix for both gaps: read client memory over PINE, keep our packet decoder.**
1. **Own name mid-session:** read the C string at `0x1FBBA10`; own id = read u32 at `0x4E37F0`, add `0x71F4`, read u32 there. One-off reads, no hooks, independent of when the meter starts.
2. **Names for on-screen entities:** poll the 24 pointers at `0x1FB6C30` about once a second; per valid pointer read id `+0x0C`, name `+0x58`, level `+0x70`, class byte `+0x256`. Keep an `id -> name/kind` map the packet pipeline looks up lazily instead of waiting for spawn data. Class byte also separates player (2), pet (3), NPC (0/1).
3. **Group members:** read the `GroupMember` array for names and ids (even unloaded members) and flag them as party.
4. **Pet attribution:** read the pet id and class byte 3 as a cross-check. Since the server credits pet hits to the owner, the red colour trailer after `db 00` is the likely discriminator; confirm in our captures.
5. **Ability names:** only feasible for your own casts (correlate a 0xCD lockout with your next `db 00`), which needs a hook in their design. Defer. No source here exposes other players' spell names.

**Combining makes sense, in one direction:** their memory layout for names/group/own id, our packets for events. Their meter sees only you and your pet because it hooks one client; our sniffer already sees everyone's `db 00` records. Do not adopt code-cave patching (writes to game memory, fragile across overlay slides and clients, and entangled with the Sandstorm patch). **Verify before relying on anything:** on SLUS-20744 v1.02 check that `0x1FBBA10` reads the logged-in character name and that `0x1FB6C30` yields plausible pointers; treat all addresses as unconfirmed until then, since they never state their disc version. Borrow their fight rules as a reference for our DPS maths: 5 s idle timeout, healing never opens a fight, duration = first to last damage, 1 s floor.
