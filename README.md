# EQOA Parser

A DPS meter for **EverQuest Online Adventures** on the **PCSX2** emulator, playing on the **Sandstorm** server.

EQOA has no combat log, so EQOA Parser reads the damage numbers straight from the game server's network traffic: the
same numbers that pop up over your target. For every fight it shows each of your characters, their pet, and their
combined damage and DPS. It works with two emulators running at once.

![EQOA Parser window](docs/images/screenshot.png)

## Download and run (no Python needed)
1. Download **`EQOAParser-<version>.zip`** from the [latest release](https://github.com/LoopRook/EQOA-Parser/releases/latest) and unzip it anywhere.
2. In PCSX2: **Settings → Network & HDD**, with Ethernet enabled and the device type set to **Sockets**.
3. Double-click **`EQOAParser.exe`**.
   - Windows may say *"Windows protected your PC"*, because the program isn't code-signed. Click **More info → Run anyway**.
   - Accept the **Administrator** prompt. Windows only lets administrators read network traffic; that's the only reason it asks.
4. Play. A fight starts at the first hit and ends after 8 seconds with no damage dealt or received.

The zip's `READ-ME-FIRST.txt` repeats these steps in plain language for anyone you send it to.

## What you see
- **Current fight**, for each character: `Name`, `Name's pet`, `Name combined` (total, DPS, max hit), plus an
  `All characters` line when you run two emulators.
- **Fight list:** every fight with its target, duration and each character's combined damage. Select a fight to see its
  full breakdown and top hits.
- **This run:** totals, DPS and best fight per character, then all characters together.
- Fights are saved to `runs/<date>.json` next to the program and reloaded when you restart it the same day.
- Optional: an always-on-top window (on by default), `--no-gui` for console output, and `--show-group` for other actors.

**Tip:** start the meter *before* you log in or zone. Character and monster names come from data the server sends when
they appear; anything already on screen when the meter starts shows as an ID such as `0x01312F0A` until it reappears.

## Is it safe?
- It only **listens** to UDP traffic between your PC and the Sandstorm server (`5.161.217.188`). It never sends anything,
  and never reads or changes the emulator or the game.
- It needs Administrator rights only because Windows requires them to see network packets.
- It saves only fight summaries, locally. Nothing is uploaded anywhere.
- Check the server's rules on third-party tools before using it. This is a passive meter, but that's their call.

## Two emulators
Just run both. The meter follows every PCSX2 instance talking to the server, and each character gets its own rows.
To follow only one, pass its connection port (shown in the status line): `EQOAParser.exe --connection 20605`.

## If it shows nothing
- Check PCSX2 network is set to **Sockets**, and that you accepted the Administrator prompt.
- If you use a VPN or have several network adapters, set `interface_ip` in `config.yaml` (next to the exe) to your PC's IPv4 address.
- If Sandstorm's server address ever changes, update `server_ip` in `config.yaml`.
- More troubleshooting: [docs/live-capture.md](docs/live-capture.md).

## Known limitations
This is an early version, tested on one PC. The packet format was worked out from captures, so some parts are inferred.
- **Target names:** monsters that were already on screen when the meter started show as IDs.
- **Abilities** aren't decoded yet and show as "(unknown)".
- **Pet vs owner** is told apart by a marker on the server's damage records. It's verified on a bard with a pet, but not
  yet confirmed for a necromancer casting while its pet fights.
- **Group members** other than your own emulators aren't shown yet.
- How the packets were decoded, and what's confirmed vs inferred: [docs/protocol-notes.md](docs/protocol-notes.md).

## Roadmap
1. Confirm pet attribution on a necromancer (pet alone, then casting while the pet fights).
2. Target names for monsters already on screen.
3. Ability and spell names.
4. Group members' damage.
5. A transparent click-through overlay.

## Run from source
Requires Python 3.11+ on Windows.
```
pip install -r requirements.txt
python -m eqoa_parser --live          # needs an Administrator terminal; or double-click run-live.bat
python -m eqoa_parser --pcap file.pcapng --no-gui   # replay a saved capture, no admin needed
python -m eqoa_parser --help
```
- Tests: `pip install -r requirements-dev.txt`, then `python -m pytest -q`. They include two real captures
  (`fixtures/real/`), reduced to the server's packets only.
- Build the exes and zip: `packaging\build-exe.bat` (outputs to `dist\`).
- Design notes: [docs/CONTRACTS.md](docs/CONTRACTS.md). Verification history: [docs/VERIFICATION.md](docs/VERIFICATION.md).
- Recording a capture for analysis: see the `pktmon` steps in [docs/live-capture.md](docs/live-capture.md). Captures
  include your own login traffic, so **don't share raw captures publicly**.

## License and disclaimer
GPL-3.0; see [LICENSE](LICENSE). EQOA Parser is an unofficial fan project. It isn't affiliated with or endorsed by
Sony, Daybreak Game Company, the PCSX2 project, or the Sandstorm server team. EverQuest is a trademark of its owner.
