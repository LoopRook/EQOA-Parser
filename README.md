# EQOA Parser

A damage meter for EverQuest Online Adventures on PCSX2, for players on the Sandstorm server. It shows how much damage
you and your pet do in each fight, works with two emulators running at once, and has a timed test mode for comparing
pets or gear on a dummy. With PINE turned on it can also show your group's damage.

![EQOA Parser window](docs/images/screenshot.png)

## Download and run

1. Download `EQOAParser-<version>.zip` from the [latest release](https://github.com/LoopRook/EQOA-Parser/releases/latest) and unzip it anywhere.
2. In PCSX2, open Settings → Network & HDD, enable Ethernet, and set the device type to **Sockets**.
3. Double-click `EQOAParser.exe`. Windows will probably say "Windows protected your PC" because the program isn't
   code-signed; click **More info**, then **Run anyway**. Then say yes to the Administrator prompt (the safety section
   below explains why it asks).
4. Play. A fight starts with the first hit and ends after 8 seconds with no damage dealt or taken.

The zip also has a `READ-ME-FIRST.txt` with the same steps, if you're passing it on to someone.

## What you see

The current fight lists each character as three rows: the character, their pet, and the two combined, with total
damage, DPS and biggest hit. With two emulators running there's also an "All characters" line.

Below that is a list of past fights showing the target, how long it lasted, and each character's damage. Click a fight
to see its full breakdown and top hits. The run summary at the top gives each character's total, DPS and best fight.

Fights are saved to `runs/<date>.json` next to the program and load back in if you restart it the same day. The window
stays on top of other windows by default. There's also a console mode (`--no-gui`).

Start the meter before you log in or zone if you can. Names for characters and monsters come from data the server sends
when they first appear, so anything already on screen when the meter starts shows up as an ID like `0x01312F0A` until it
comes back into view.

## Safety and privacy

The meter listens to the traffic between your PC and the Sandstorm server (`5.161.217.188`) and never sends anything.
It never changes the emulator or the game. Without PINE it doesn't touch the emulator at all; with PINE on it reads the
emulator's memory for names and your group, and never writes to it. Everything it saves (fights, test results, and
recordings if you make any) stays on your PC; nothing gets uploaded. It needs Administrator rights because Windows
won't show network packets to a normal program.

It's a passive tool, but it's still worth checking Sandstorm's rules on third-party programs before you use it.

## Recording a fight

If something looks wrong, press **Record**, play until it happens, and press **Mark** to note the moment. The file in
the `captures` folder next to the exe only holds what the server sent you, so it's safe to send to whoever is helping
you look into it.

## Testing on a dummy

Fights end after a few quiet seconds, so their lengths vary and pauses pull the DPS down. For comparing pets, gear or
spells, press **Test**, name it (for example "Djinni Vizier 1"), keep the length at 180 seconds and start hitting the
dummy. The clock starts at your first damage and DPS is total damage divided by the full length, so every run is
measured the same way. Keep the setup identical, do three runs of each thing and compare them. Results appear in the
Tests list with the per-character breakdown, are saved in `runs/<date>.json`, and show the pet's name when PINE is on.
From a terminal, `--no-gui --test 180 --test-label "Djinni Vizier 1"` runs one test.

## Two emulators

The meter follows every copy of PCSX2 talking to the server and gives each character their own rows. If you only want
one, pass its connection port (shown in the status line): `EQOAParser.exe --connection 20605`.

## Optional: group and names (PINE)

By default the meter only sees your own characters. With PINE on it also reads the group list and character names from
the emulator's memory (read-only), so groupmates' damage shows up as extra characters with their own rows, and names
are known straight away.

1. In PCSX2, open Settings → Advanced, turn on PINE, and note the slot number (28011 by default).
2. Run `EQOAParser.exe --pine` (add `--pine-slot 28012` if your slot isn't the default), or put this in `config.yaml`:
   `pine: {enabled: true, slot: 28012}`.

The status line shows `PINE: on (slot 28012), group of 2`, or `PINE: not reachable` if PCSX2 isn't answering; the meter
keeps working either way and retries every few seconds. PCSX2 copies share one settings file, so only one emulator can
have PINE on at a time. Groupmates on other PCs only show up if that emulator is the one with PINE on.

## If it shows nothing

Check that PCSX2's network is set to Sockets and that you said yes to the Administrator prompt. If you're on a VPN or
your PC has more than one network adapter, open `config.yaml` (next to the exe) and set `interface_ip` to your PC's IPv4
address. If Sandstorm ever moves to a new address, change `server_ip` in the same file.
[docs/live-capture.md](docs/live-capture.md) has more troubleshooting.

## Known limitations

This is an early version and it has only been tested on one PC. The packet format was pieced together from real game
captures, so some of it is educated guesswork.

- Without PINE, monsters that were already on screen when the meter started show as IDs instead of names.
- Spell and ability names aren't decoded yet; they show as "(unknown)".
- Group members on other people's PCs only show up with PINE on (see above).

Notes on how the packets were decoded, and which parts are confirmed, are in [docs/protocol-notes.md](docs/protocol-notes.md).

## Roadmap

1. Names for monsters that were already on screen, without needing PINE.
2. Spell and ability names.
3. A transparent overlay you can click through.

## Running from source

You'll need Python 3.11 or newer on Windows.
```
pip install -r requirements.txt
python -m eqoa_parser --live          # needs an Administrator terminal; or double-click run-live.bat
python -m eqoa_parser --pcap file.pcapng --no-gui   # replay a saved capture, no admin needed
python -m eqoa_parser --help
```
To run the tests, install `requirements-dev.txt` and run `python -m pytest -q`. They use three real captures in
`fixtures/real/`, trimmed down to the server's packets only with `tools/sanitize_pcapng.py`. To build the exes and the
zip, run `packaging\build-exe.bat`; everything ends up in `dist\`. `packaging\build-prerelease.bat` builds a test copy
in `prerelease\` with PINE already switched on. The design notes are in [docs/CONTRACTS.md](docs/CONTRACTS.md), and the test and
verification history is in [docs/VERIFICATION.md](docs/VERIFICATION.md).

If you record your own capture for debugging (the `pktmon` steps are in [docs/live-capture.md](docs/live-capture.md)),
don't post it publicly. Captures include your own login traffic.

## License

GPL-3.0; see [LICENSE](LICENSE).

This is an unofficial fan project. It isn't affiliated with or endorsed by Sony, Daybreak Game Company, the PCSX2
project, or the Sandstorm server team. EverQuest is a trademark of its owner.
