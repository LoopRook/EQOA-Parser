# Live capture (reading the game's network traffic)

EQOA has no combat text; the damage numbers travel in the game server's UDP packets (see `docs/protocol-notes.md`).
PCSX2 runs in *Sockets* network mode, so that traffic is ordinary UDP between `pcsx2-qt.exe` and the server
(`5.161.217.188`, ports 10070/10071).

> **Status:** verified live once on Windows 11 as Administrator (2026-10-18): the raw-socket capture worked in a real
> fight. Still UNVERIFIED: Linux (`AF_PACKET`), IP-fragmented datagrams, VPN/multi-adapter machines, and whether packets
> your PC *sends* are delivered (the damage numbers come from the inbound direction, which works).

## What it captures (and what it never does)

* Only **UDP datagrams to or from the game server** (`net.server_ip`); packets to/from any other host are discarded
  immediately and never stored or logged.
* **Read-only:** it only *receives*. It never sends a packet, never touches PCSX2's memory, and does not modify traffic.
  Picking the network interface uses a UDP `connect()` that sends nothing.
* IP fragments of those datagrams are reassembled; incomplete ones are dropped after 5 seconds. Checksums are not verified.
* Windows: a raw IPv4 socket with `SIO_RCVALL` on the network interface that routes to the server. Linux (best effort):
  an `AF_PACKET` socket. macOS and others are not supported (use the fallback below).

## Running as Administrator (Windows)

Raw sockets need elevation, otherwise you get `error: live capture needs Administrator rights`.

1. Press Start, type `PowerShell` (or `Windows Terminal`), right-click it, choose **Run as administrator**.
2. `cd` to the project folder, e.g. `cd "C:\Users\<you>\Github Projects\EQOA Parser"`.
3. Start the parser with live capture (the exact flag is wired up by the app; see `python -m eqoa_parser --help`):

   ```bash
   python -m eqoa_parser --live
   ```
4. Start or keep playing in PCSX2 as normal. PCSX2 itself does **not** need to run as Administrator.

On Linux run with `sudo`, or give Python the capability once: `sudo setcap cap_net_raw+ep $(readlink -f $(which python3))`.

Config (`config.yaml`, section `net`): `server_ip` (default `5.161.217.188`), `interface_ip` (leave unset to
auto-detect; set it to this PC's IPv4 address if you have several adapters, a VPN, or auto-detect fails),
`local_port` (follow one emulator when two run), `poll_interval` (how often a quiet capture wakes up, default 0.25 s).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `needs Administrator rights` | The terminal is not elevated. Reopen it with *Run as administrator*. |
| `cannot determine which network interface routes to …` | No route/Internet when starting. Connect first, or set `net.interface_ip`. |
| `cannot open the capture socket on <ip>` | Wrong `interface_ip` (not an address of this PC), or a security product blocks raw sockets. |
| Starts, but no damage / no datagrams | (1) Wrong adapter (VPN/second NIC): set `net.interface_ip` to the adapter the game uses. (2) Is PCSX2 in *Sockets* mode and logged in to Sandstorm? (3) The raw socket may not deliver packets *sent* by this PC on some Windows versions; the server→you direction (the one that carries damage) is inbound, so damage should still appear. If nothing at all arrives, use the pktmon fallback to check that packets exist. |
| Duplicate-looking numbers | The server re-sends messages; the decoder dedupes. If you see doubled totals, send a capture. |
| CPU/disk concerns | The raw socket sees all IP traffic on the adapter but discards non-game packets at once; nothing is written to disk. |

## Fallback: pktmon (no code, works today)

Capture with Windows' built-in `pktmon` from an **elevated** terminal, then replay the file:

```bash
pktmon filter remove
pktmon filter add EQOA -t UDP -i 5.161.217.188
pktmon start --capture --pkt-size 0
# ... play for a while ...
pktmon stop
pktmon etl2pcap PktMon.etl --out eqoa.pcapng
pktmon filter remove
```

Then analyse it offline with `python -m eqoa_parser --pcap eqoa.pcapng` (wiring owned by the app layer). pktmon records each
packet once per network component, so the reader drops near-duplicates; this is expected.
