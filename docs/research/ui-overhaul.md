# UI overhaul research

Mockup: [ui-mockup.html](ui-mockup.html) (open in a browser; sample data, theme and DPS/HPS/DTPS toggles work).
Data limits are from [metrics-and-parsec.md](metrics-and-parsec.md): only the user's own characters and pets, no groupmate damage, no threat, no misses, no deaths, no damage ability names.

## 1. What is standard in MMO parsers

Depth varies: the ACT/Skada/Details!/GamParse/StarParse rows come from vendor or project pages; EQLogParser, Parsec and ixparse were thin in search (see gaps).

| Tool | Game | Notes (sourced) |
|---|---|---|
| ACT | EQ2, FFXIV, SWTOR, others | Plugin-based parser; drill into individual actions; Encounter VCR and Timeline graphs; OverlayPlugin adds a customisable mini-parse overlay, drag to move, resize at the corner ([ACT](https://advancedcombattracker.com/), [OverlayPlugin](https://github.com/RainbowMage/OverlayPlugin/blob/master/README-en.md)). Ember overlay keeps up to 5 encounters and can blur names ([ember](https://github.com/GoldenChrysus/ffxiv-ember-overlay)). |
| Skada | WoW | Modular bar meter, several modes, segmented fights, multiple window types (bar, inline, LDB text); left-click drills down, right-click goes back; themes and plugin API ([WowAce](https://www.wowace.com/projects/skada)). |
| Details! | WoW | Real-time bars, per-ability breakdown, healing with overheal, multiple windows (e.g. DPS and HPS side by side), built-in plugins ([guide, single secondary source](https://noobtoboss.com/?p=6907)). |
| EQLogParser | EverQuest | Windows .NET 8 app for EQ logs ([RedGuides](https://www.redguides.com/community/resources/eqlogparser.3112)). Feature list not confirmed here. |
| GamParse | EverQuest | Live battle tracking, in-game overlays, DPS/spell/tanking/melee stats, HTML export ([GamboSoft](https://gambosoft.eqresource.com/)). |
| StarParse (ixparse.com) | SWTOR | Reads combat log files, per-ability detail, upload to Parsely, group raiding sync ([guide](https://swtorista.com/articles/how-to-use-starparse/)). |
| Parsec | SWTOR | "Raid-aware real-time log parser"; tanking info, DTPS ([forum](https://forums.swtor.com/topic/597112-introducing-parsec-a-new-raid-aware-real-time-combat-log-parser/page/6/)). Current status unconfirmed. |

Gaps: no authoritative feature page found for EQLogParser, Parsec or ixparse itself. Rows below are marked "common" only where at least two tools show it.

| Feature | How common | EQOA Parser | Why |
|---|---|---|---|
| Meter bars per actor, sorted | Universal | **Yes** | Characters and pets are known per fight. Only own party, never groupmates. |
| DPS / HPS / DTPS mode toggle | Universal (Skada modes, Details! windows) | **Yes** | Damage dealt, taken, healing done and received are all parsed. |
| Per-fight list, encounter detection | Universal | **Yes** | Already segments on 8 s idle. Lacks boss/named detection, only target name. |
| Per-ability breakdown | Very common (Details!, ACT, StarParse) | **Partial** | Healing and damage-taken by source work; damage ability names unproven. Show by source/type only. |
| Timeline graph (DPS over time) | Common (ACT, Details!) | **Partial** | Hit timestamps exist, so a DPS-over-time line is feasible. Needs per-hit history kept in summaries. |
| Compact / overlay mode | Very common (ACT Overlay, GamParse, Skada) | **Yes** | Always-on-top small window. True transparent overlay over PCSX2 is harder (see tech). |
| Click-through overlay | Common (OverlayPlugin) | **Partial** | Needs Win32 `WS_EX_TRANSPARENT`; possible in any toolkit via ctypes. Lock/unlock toggle needed. |
| Export / share | Common (HTML export, Parsely upload) | **Partial** | Local CSV/JSON/PNG easy. Online sharing out of scope. |
| Segment compare | Common (Skada, Details!) | **Yes** | Fights and timed tests are comparable; tests already designed for it. |
| History across sessions | Common | **Yes** | Persistence already exists (runs folder). Needs a browser for old runs. |
| Group-wide meters, threat, deaths, misses | Standard in log parsers | **No** | Server never sends groupmate hits; others not in the data. |
| Multi-window (DPS and HPS at once) | Details!, Skada | **Yes** | Cheap once state is shared. Low priority. |

## 2. Design language from track-level-companion

Read via `gh api` (CLAUDE.md §E, `src/index.css`, `tailwind.config.js`). Nothing was cloned or run. vellum-design-kit is a different taste (Swiss, glass, token-driven with a `tokens.json` source and a validator); only its "single token file generates CSS" workflow is worth borrowing.

**Tokens**
- Dark: background `#000000` (OLED), surface `#0A0A0C`, raised `#121215`, border `#27272A`. Light: background `#F5F5F5`, surface white. **Light is the default** (sunlight use); dark is respected once saved.
- One accent: vermilion `#D71921`, used as a CSS variable, never hard-coded. Status: success `#4A9E5C`, warning `#D4A843`, interactive `#5B9BF6` on dark; darkened `#2F6B3C`, `#8A6A16`, `#1D4ED8` on light to hit AA. Status colour goes on the value, not the label or row.
- Fonts (Google Fonts): Space Grotesk (UI text), Space Mono (data and labels), Doto (dot-matrix hero numbers).
- Radius 4–8px on controls, up to 16px on cards, no pills on text-bearing items. 1px borders, no shadows.
- Spacing: 44px touch targets on the phone app; for a desktop meter use a 4px grid with 24px bar rows.

**Rules**
- Uppercase bracketed identifiers for actions and labels (`[ CLOSE ]`, `[ ON GRADE ]`), which never wrap (`white-space: nowrap`).
- Hierarchy through type (one dot-matrix hero number per screen), not colour or fill.
- Motion is small: 0.22 s `cubic-bezier(0.16,1,0.3,1)` slides, a 2 s subtle pulse, no bounce. Respect `prefers-reduced-motion`.
- Dot-grid `radial-gradient(circle, #a1a1aa 1.1px, transparent 1.1px)` at 16px for empty space.
- Light and dark must stay in sync from the first paint (anti-flash script) and component colours come from variables only.

## 3. Proposal

### Screens (priority order)
1. **Compact meter** (P0), about 300×(auto) px, always on top, remembers position. Header: fight label + timer + live dot. Mode toggle `[ DPS ] [ HPS ] [ DTPS ]`. One Doto hero number (combined total in the mode). Below, a group per emulator with a sub-total, bars for each character and, indented with a dashed edge, its pet. Bars scale to the top actor in the mode; the number sits on the right. Footer: damage, max hit, XP. Button `[ + ]` expands. A lock toggle enables click-through.
2. **Expanded view** (P0), about 600px, tabs `FIGHTS`, `DETAIL`, `TESTS`, `RUN`.
   - FIGHTS: table of fights (run, #, target, duration, per-character dps+pet, status tag). Click for DETAIL.
   - DETAIL: per-actor damage, DPS, max hit, taken, healed; healing received by healer; damage taken by source. Optional DPS-over-time line later.
   - TESTS: start/stop timed test, results table, best highlighted, compare two.
   - RUN: tunar/hr, XP/hr, kills, class mastery, per-character totals.
3. **Settings/connection** (P1): emulator slots, PINE on/off, names, theme, opacity, bar scale. Status strip is already in the header.
4. **History browser** (P2) and **export** (P2): old runs, CSV/JSON/PNG.
5. **Timeline graph and ability breakdown** (P3): once data supports it.

### Two emulators
Never mix them into one list. Compact: one block per emulator with its own header (`EMU 1`, `EMU 2`) and sub-total, one combined hero total on top. Bars share a single scale so characters across emulators compare directly. If only one emulator is live, the second block hides. Expanded: the character column carries a small `E1/E2` tag.

### UI technology

| | Tkinter (now) | pywebview + HTML/CSS | PySide6 / Qt | Dear PyGui |
|---|---|---|---|---|
| Exe size (PyInstaller) | 13.6 MB (measured, `dist/EQOAParser.exe`) | about 15–25 MB (uses system WebView2; estimate) | about 60–120 MB (estimate) | about 30–40 MB (estimate) |
| New dependency | none | `pywebview` (+ pythonnet, WebView2 runtime; present on Win11) | `PySide6` | `dearpygui` |
| Design-kit fit | Poor: no custom fonts without hacks, no rounded corners/antialiasing, bars via Canvas | **Excellent**: the mockup CSS maps directly, fonts and light/dark are free | Good via QSS/QML, but a rewrite of every style | Poor/medium: themeable but not typographically rich |
| Always-on-top, click-through | Yes (`-topmost`, ctypes) | Yes (`on_top`; click-through via ctypes on the window handle; transparent windows are less reliable) | Yes, best-supported | Yes |
| Effort | Lowest (restyle in place) | Medium: JS bridge, one new page; core untouched | High | Medium-high |
| Risk | Looks dated; cannot hit the style | WebView2 absent on old Windows; fonts need bundling for offline | Large exe, licensing LGPL awareness | Immediate-mode, awkward for tables |

**Recommendation: pywebview + HTML/CSS**, built in two steps. The core already pushes `FightSummary`/`RunSummary` through a sink (`on_live`, `on_closed`, `on_finished`, `on_test`, ...; see `QueueSink` in `tk_app.py`). A `WebSink` that serialises summaries to JSON and calls `window.evaluate_js` (or an async API) is a drop-in, and the existing `tk_app.py` stays as a fallback. The mockup's CSS and rendering functions are the starting point.

If a heavy dependency is unwelcome, the **fallback is Tkinter restyled**: flat dark/light palette, Canvas bars, bundled TTF loaded via `ctypes` `AddFontResourceEx` for Space Mono/Doto. That gets about 70% of the look at near-zero risk but not rounded cards or crisp type.

Do not pick Qt (size) or Dear PyGui (poor tables) unless pywebview proves unreliable for the transparent always-on-top case. Before committing, prototype two things: always-on-top + borderless + drag on pywebview, and offline fonts. That is a half-day spike.

### Suggested sequence
1. Spike: pywebview borderless always-on-top window showing static mockup, fed by replay mode (`EQOAParser-replay.exe`).
2. `WebSink` + compact meter live. Keep Tk as `--classic`.
3. Expanded view tabs, tests, run stats.
4. Settings, click-through lock, export.
5. Remove the Tk UI once parity is reached.

## Open questions
- Whether to treat transparent/click-through overlay over PCSX2 fullscreen as required (borderless-window mode of PCSX2 is needed for any toolkit).
- Whether the sandbox/AV flags on a pywebview exe differ from the current exe; test with the release zip.
