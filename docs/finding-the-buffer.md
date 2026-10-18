# Finding the chat/combat buffer in PCSX2

Goal: find out **where** in the PS2's 32 MB of RAM EQOA keeps its chat/combat text, **how it is encoded**, and **how the
buffer behaves**. Nothing about this is known yet; every step below is a way to find out. The tools only ever
*read* memory (the PINE client has no write commands), so they cannot change your game.

Run every command from the project folder (`EQOA Parser`). On Windows PowerShell and Linux the commands are the same.

> Status: the PINE client (Windows TCP transport, version/status/title/id info calls, Read8/16/32/64) has been
> **verified on a real PCSX2 v2.8.2** (1 MiB read ~0.09 s, a full 32 MB scan ~2.6 s). Reads at addresses past
> `0x01FFFFFF` do **not** fail on real PCSX2, they return data (zeros were seen), so only trust addresses below `0x02000000`.
> Still UNVERIFIED: the Linux UNIX-socket transport and everything about the game's buffer. If something behaves
> oddly (errors, odd addresses), that is useful information — send it back.

## 1. Enable PINE in PCSX2

In PCSX2 (Qt build): **Settings → Advanced → "PINE Settings"** group: tick **Enable PINE**, keep the slot at
**28011** (the default). (Source: `pcsx2-qt/Settings/AdvancedSettingsWidget.ui` in the PCSX2 repo has these two widgets;
menu wording in your version may differ.) Restart the emulation if the setting does not apply at once.
On Windows PINE listens on `127.0.0.1:<slot>`; on Linux on a UNIX socket (`$XDG_RUNTIME_DIR/pcsx2.sock`, with `.<slot>`
appended for a non-default slot). Use `--slot N` on every command below if you changed the slot; use `--socket PATH` to
override the Linux socket path.

**Gotchas (observed on PCSX2 2.8.2):**

* All PCSX2 instances share **one `PCSX2.ini`**, so the PINE enable flag and slot are the same for every instance you
  start. If you run two emulators at once, give each a separate config (e.g. a portable install with its own
  `portable.txt`/data folder) and a **different slot**, and pass `--slot N` to the tools.
* PINE starts listening **only after PCSX2 has been restarted** with the setting enabled. Ticking the box in a running
  instance is not enough: close and reopen PCSX2, then start the game.

Check the connection with the game running (take note of the PCSX2 version string it prints):

```bash
python -c "from eqoa_parser.pine import PineClient; c=PineClient(); print(c.version(), '|', c.status(), '|', c.title())"
```

"cannot connect to PCSX2 …" means PCSX2 isn't running, PINE is off, or the slot differs. Reads only work while a
game is loaded (otherwise "PCSX2 rejected the PINE command").

## 2. Scan for text you just saw in game

Get an easily searchable line on screen, e.g. send yourself a chat message with a unique word such as
`zebraquartz` (or take a combat line and use a distinctive piece of it). Leave the game running or paused (a paused game
makes the numbers stand still, which is fine). Then:

```bash
python -m eqoa_parser.recon scan --text "zebraquartz" --encoding ascii
python -m eqoa_parser.recon scan --text "zebraquartz" --encoding utf16le
python -m eqoa_parser.recon scan --text "zebraquartz" --encoding shiftjis
```

Each scan reads all 32 MB (a few to several tens of seconds) and prints every match as an address with a hex dump and
decoded text. Try all three encodings; at most one or two should hit. For `shiftjis` a plain-ASCII word will look
identical to ASCII, so also try a string containing a character that only exists in Japanese text if the game uses any.
Notes:

* Several hits are normal (the typing box, the text you typed, the chat window, caches). Note **all** of them.
* The text you *typed* can live in an input buffer rather than the chat history. Better: have a *different* character
  or an NPC say something, or use a combat line the game printed for you.
* If nothing matches in any encoding, the game may store text as indexes/IDs, compressed, or with a
  different charset; try a shorter fragment (`--text "zebra"`), different case, or send the dump to me.
* A hit at an odd address in `utf16le` is likely a false positive.

## 3. Snapshot / diff around a combat message

Snapshots find buffers when text search fails or to see what else changes. Work in a quiet spot with the game
paused or idle:

```bash
python -m eqoa_parser.recon snapshot --out before.bin --progress
# now trigger ONE new combat/chat line in game (e.g. hit a mob once)
python -m eqoa_parser.recon snapshot --out after.bin --progress
python -m eqoa_parser.recon diff before.bin after.bin --text-only --encoding ascii
```

(`--progress` prints to stderr; each snapshot is 32 MB.) The diff lists changed regions, merged into ranges, and
`--text-only` keeps only regions whose *new* bytes decode as text in the chosen encoding. Repeat with
`--encoding utf16le` and `--encoding shiftjis`. A region that contains your new line is a candidate buffer location.
Do it twice with two different messages: the buffer address region should show up both times. Also useful:
`diff before.bin live --text-only` compares a saved snapshot to RAM right now.

Look at: does the new text appear **after** the previous line (appending), or does the **whole block shift** (scrolling),
or does it overwrite an old slot (ring)? The `old:`/`new:` dumps show this.

## 4. Confirm with `watch`

Pick the candidate address (start of the region/line) and watch ~512 bytes around it:

```bash
python -m eqoa_parser.recon watch --addr 0x01234500 --len 512 --encoding ascii --interval 0.2
```

It prints a dump (hex + decoded text) every time the bytes change; Ctrl+C to stop. Generate a few combat lines and
watch what happens: new lines appearing at the end? text moving up? one slot changing at a time? Find the **start of the
buffer** (does the region begin where the first line sits, or does it start earlier?) and how big it is (when does it stop being
text?). Fight with a pet / hit a few times in a row so you also capture two identical lines in a row.

## 5. What to record and send back

1. **PCSX2 version** (the string from step 1) and your **EQOA build/server** (Sandstorm), and whether the game was paused.
2. **Address(es)** of the buffer, and which of the several hits you believe is the combat/chat history.
3. **Encoding** that matched (`ascii` / `utf16le` / `shiftjis`) and the exact hex dump of 2–3 lines around the hit.
4. **Layout observations**: appending vs. scrolling vs. ring/slots; line separator seen (NUL `00`, `0a`, `0d 0a`, length prefix?);
   approximate buffer size; whether there are per-line headers (timestamps, colour codes, channel IDs) in the bytes.
5. **Verbatim examples** of 5–10 real combat lines (damage dealt, damage taken, miss, kill, pet) exactly as they appear.
6. The output of `watch` for one fight (paste it or save it to a file).
7. Anything that went wrong: error text, scans that hit odd addresses, snapshots that took very long.

Once you have an address, a first attempt for `config.yaml` (still ASSUMED until it works):

```yaml
buffer:
  address: 0x01234500   # your address
  size: 4096
  encoding: ascii       # or utf16le / shiftjis
  layout: scroll        # or ring — see eqoa_parser/pine_source.py for what each assumes
```

If the real buffer doesn't fit either layout (e.g. length-prefixed records), the layout strategy in
`eqoa_parser/pine_source.py` is pluggable and can be changed once we know.

## Trying the tools without PCSX2

```bash
python -m eqoa_parser.mock_pine --plant "0x00123456=You hit a rat for 5 points of damage." --slot 28011
```

starts a read-only fake PINE server; in another terminal run any of the commands above (they use the same default slot).
