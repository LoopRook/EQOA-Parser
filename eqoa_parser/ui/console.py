"""--no-gui output. OWNER: session C.

Formatting is split from I/O so it can be tested from hand-built FightSummary objects.
`ConsoleUI` has the same sink interface as the Tk window's queue sink:
on_live(summary), on_closed(summary, run_summary), on_finished(run_summary).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Sequence, TextIO

from ..events import ACTOR_PET, ACTOR_YOU
from ..testmode import TestResult
from ..stats import (ALL_CHARACTERS, TOP_SOURCES, ActorStats, CharacterProgress, FightSummary, RunSummary,
                     HealStats, TakenStats, effective_duration, group_rows, pet_name)


def _label(summary: FightSummary) -> str:
    return summary.target or "(no target)"


def _actor_line(label: str, a: ActorStats) -> str:
    return f"{label:<{max(10, len(label) + 1)}} total {a.total:>6}  dps {a.dps:>7.1f}  max hit {a.max_hit:>5}"


def group_actors(summary: FightSummary) -> list[ActorStats]:
    """Actors other than you/pet, highest damage first."""
    others = [a for n, a in summary.actors.items() if n not in (ACTOR_YOU, ACTOR_PET)]
    return sorted(others, key=lambda a: (-a.total, a.actor))


def actor_names(summary: FightSummary) -> dict[str, str]:
    """Actor key -> display name (`Zecron`, `Zecron's pet`) for the fight's characters."""
    out: dict[str, str] = {}
    for g in group_rows(summary):
        out[g.player.actor] = g.name
        out[g.pet.actor] = g.pet_name
    return out


def format_abilities(summary: FightSummary) -> str:
    if not summary.top_abilities:
        return "-"
    names = actor_names(summary)
    return ", ".join(f"{t['ability']} ({names.get(t['actor'], t['actor'])}) {t['total']}" for t in summary.top_abilities)


def flow_lines(summary: FightSummary) -> list[str]:
    """Per character: damage taken (DTPS, biggest hit, top sources) and healing (done/HPS, received), when nonzero.
    The name prefixes the line only with 2+ characters."""
    rows = group_rows(summary)
    out: list[str] = []
    for g in rows:
        pre = f"{g.name}: " if len(rows) >= 2 else ""
        if g.taken_total:
            top = ", ".join(f"{n} {t}" for n, t, _ in g.taken_sources[:TOP_SOURCES])
            out.append(f"{pre}Taken: {g.taken_total} (DTPS {g.dtps:.1f}), biggest hit {g.taken_max}, top: {top}")
        if g.heal_done or g.heal_recv:
            line = f"{pre}Healing: done {g.heal_done} (HPS {g.hps:.1f}), received {g.heal_recv}"
            if g.recv_sources:
                line += " from " + ", ".join(f"{n} {t}" for n, t in g.recv_sources[:TOP_SOURCES])
            out.append(line)
    return out


def progress_text(p: CharacterProgress, elapsed: float) -> str:
    """``Kills 21 (88/h), mastery 3,491,313 (14,750/h), tunar 247 (1,045/h)``: only what is nonzero; the per-hour
    figures use the run's elapsed time."""
    def part(label: str, v: int) -> str:
        return f"{label} {v:,}" + (f" ({v * 3600 / elapsed:,.0f}/h)" if elapsed > 0 else "")
    parts = [part("Kills", p.kills)]
    for label, v in (("XP", p.xp), ("mastery", p.mastery), ("tunar", p.tunar)):
        if v:
            parts.append(part(label, v))
    if p.debt:
        parts.append(f"debt paid {p.debt:,}")
    return ", ".join(parts)


def progress_lines(run: RunSummary) -> list[str]:
    """One line per character with progress; the name leads only with 2+ characters."""
    shown = [p for p in run.progress if p.any()]
    return [(f"{p.name}: " if len(shown) >= 2 else "") + progress_text(p, run.elapsed) for p in shown]


@dataclass
class DisplayRow:
    label: str
    kind: str                       # "player" | "pet" | "combined" | "all" | "other"
    total: int
    dps: float
    max_hit: int | None = None      # None for combined / all rows
    taken: str = ""                 # damage taken ("total", or "total (DTPS)" on combined / all rows)
    healed: str = ""                # healing done ("total", or "total (HPS)" on combined / all rows)


_NO_TAKEN, _NO_HEAL = TakenStats(), HealStats()


def display_rows(summary: FightSummary, show_group: bool = False) -> list[DisplayRow]:
    """The grouped view shared by the console and the Tk panel: per character `Name`, `Name's pet`,
    `Name combined`; with 2+ characters a final `All characters`; with show_group the remaining actors."""
    groups = group_rows(summary)
    rows: list[DisplayRow] = []
    taken = lambda a: str(((summary.taken or {}).get(a.actor) or _NO_TAKEN).total)
    healed = lambda a: str(((summary.heals or {}).get(a.actor) or _NO_HEAL).done)
    for g in groups:
        rows.append(DisplayRow(g.name, "player", g.player.total, g.player.dps, g.player.max_hit,
                               taken(g.player), healed(g.player)))
        rows.append(DisplayRow(g.pet_name, "pet", g.pet.total, g.pet.dps, g.pet.max_hit, taken(g.pet), healed(g.pet)))
        rows.append(DisplayRow(f"{g.name} combined", "combined", g.combined_total, g.combined_dps, None,
                               f"{g.taken_total} ({g.dtps:.1f})", f"{g.heal_done} ({g.hps:.1f})"))
    if len(groups) >= 2:
        total = sum(g.combined_total for g in groups)
        eff = effective_duration(summary.duration)
        tk, hl = sum(g.taken_total for g in groups), sum(g.heal_done for g in groups)
        rows.append(DisplayRow(ALL_CHARACTERS, "all", total, total / eff, None,
                               f"{tk} ({tk / eff:.1f})", f"{hl} ({hl / eff:.1f})"))
    if show_group:
        known = {g.player.actor for g in groups} | {g.pet.actor for g in groups}
        rest = [a for n, a in summary.actors.items() if n not in known]
        rows += [DisplayRow(a.actor, "other", a.total, a.dps, a.max_hit)
                 for a in sorted(rest, key=lambda a: (-a.total, a.actor))]
    return rows


def result_rows(result: TestResult) -> list[DisplayRow]:
    """Per-character breakdown of a timed test (DPS over the fixed test length), like display_rows."""
    L = result.length_s
    rows: list[DisplayRow] = []
    for c in result.characters:
        rows.append(DisplayRow(c.name, "player", c.player_total, c.player_total / L, c.player_max))
        rows.append(DisplayRow(pet_name(c.name, c.pet_name), "pet", c.pet_total, c.pet_total / L, c.pet_max))
        rows.append(DisplayRow(f"{c.name} combined", "combined", c.combined, c.dps))
    if len(result.characters) >= 2:
        rows.append(DisplayRow(ALL_CHARACTERS, "all", result.total, result.dps))
    return rows


def format_test(result: TestResult) -> str:
    rows = result_rows(result)
    width = max([10] + [len(r.label) + 5 for r in rows])
    note = "" if result.complete else " (source ended early)"
    lines = [f"=== Test '{result.label}': {result.length_s:g}s{note} ===",
             f"  Total {result.total}  DPS {result.dps:.1f}  hits {result.hits}  biggest hit {result.max_hit}"]
    lines += [_row_line(r, width) for r in rows]
    return "\n".join(lines)


def own_for(summary: FightSummary, fallback: Sequence[str] | None = None) -> Sequence[str] | None:
    """Own actors to draw `summary` with. `fallback` None means the plain You/Pet view. Otherwise the fight's own
    stored own_actors win over the caller's list (the current run's), so fights reloaded from earlier runs keep
    their breakdown; None stored = the old you+pet rule. A summary without the field uses `fallback`."""
    if fallback is None:
        return None
    if hasattr(summary, "own_actors"):
        return summary.own_actors
    return fallback


def row_actors(summary: FightSummary, own: Sequence[str] | None = None, show_group: bool = False) -> list[tuple[str, ActorStats]]:
    """Rows to show: You, Pet, the other own actors (first-seen order, from `own`), then - only with show_group -
    every remaining actor, highest damage first. `own` None = the single-emulator view (You and Pet only)."""
    own = own_for(summary, own)  # the fight's own list wins over the caller's (current run's) list
    rows = [("You", summary.you), ("Pet", summary.pet)]
    seen = {ACTOR_YOU, ACTOR_PET}
    for name in own or ():
        if name not in seen and name in summary.actors:
            rows.append((name, summary.actors[name]))
        seen.add(name)
    if show_group:
        rest = [a for n, a in summary.actors.items() if n not in seen]
        rows += [(a.actor, a) for a in sorted(rest, key=lambda a: (-a.total, a.actor))]
    return rows


def format_others(summary: FightSummary, own: Sequence[str] | None) -> str:
    """Damage of own actors other than You/Pet (other emulators' characters), or ''."""
    rows = [(n, a) for n, a in row_actors(summary, own)[2:]]
    if not rows:
        return ""
    return ", ".join(f"{n} {a.total} / {a.dps:.1f}" for n, a in rows)


def format_live(summary: FightSummary, show_group: bool = False, own: Sequence[str] | None = None) -> str:
    parts = [f"[live] fight {summary.fight_id} vs {_label(summary)} {summary.duration:.1f}s"]
    for r in display_rows(summary, show_group):
        if r.max_hit is None:
            parts.append(f"{r.label} {r.total} ({r.dps:.1f} dps)")
        else:
            parts.append(f"{r.label} {r.total} ({r.dps:.1f} dps, max {r.max_hit})")
    return " | ".join(parts)


def _row_line(r: DisplayRow, width: int) -> str:
    pad = "    " if r.kind == "combined" else "  "
    label = f"{pad}{r.label}"
    hit = f"  max hit {r.max_hit:>5}" if r.max_hit is not None else ""
    return f"{label:<{width}} total {r.total:>6}  dps {r.dps:>7.1f}{hit}"


def format_fight(summary: FightSummary, show_group: bool = False, own: Sequence[str] | None = None) -> str:
    rows = display_rows(summary, show_group)
    width = max([10] + [len(r.label) + 5 for r in rows])
    lines = [f"=== Fight {summary.fight_id}: {_label(summary)} - {summary.duration:.1f}s ==="]
    lines += [_row_line(r, width) for r in rows]
    lines += [
        f"  (damage taken {summary.damage_taken})",
        *[f"  {t}" for t in flow_lines(summary)],
        f"  Top abilities: {format_abilities(summary)}",
    ]
    return "\n".join(lines)


def format_run(run: RunSummary) -> str:
    if run.fight_count == 0:
        return "\n".join(["=== Run summary ===", "  No fights.", *[f"  {t}" for t in progress_lines(run)]])
    best = (
        f"fight {run.best_fight_id} vs {run.best_fight_target or '(no target)'} at {run.best_fight_dps:.1f} dps"
        if run.best_fight_id is not None
        else "-"
    )
    lines = [
        "=== Run summary ===",
        f"  Fights: {run.fight_count}",
        f"  Total damage: {run.total_damage}",
        f"  Average DPS: {run.average_dps:.1f}",
        f"  Best fight: {best}",
    ]
    chars = run.characters
    width = max([10] + [len(c.name) + 3 for c in chars])
    for c in chars:
        fight = f"fight {c.best_fight_id} ({c.best_dps:.1f} dps)" if c.best_fight_id is not None else "-"
        lines.append(f"  {c.name:<{width}} total {c.total:>6}  dps {c.dps:>7.1f}  best fight {fight}")
    lines += [f"  {t}" for t in progress_lines(run)]
    return "\n".join(lines)


class ConsoleClosed(Exception):
    """stdout is gone (broken pipe); raised once to stop the engine, afterwards output is dropped."""


class ConsoleUI:
    def __init__(
        self,
        show_group: bool = False,
        *,
        out: TextIO | None = None,
        live_interval: float = 2.0,
    ) -> None:
        self.show_group = show_group
        self.out = out or sys.stdout
        self.live_interval = live_interval  # throttle in fight time (not wall time): same output at any replay speed
        self._last_live = 0.0
        self._live_fight: int | None = None
        self._broken = False
        self.own_provider: Callable[[], Sequence[str]] | None = None  # set by the app for multi-emulator runs

    def _print(self, text: str, file: TextIO | None = None) -> None:
        if self._broken:
            return
        try:
            print(text, file=file or self.out, flush=True)
        except (BrokenPipeError, OSError):
            self._broken = True
            raise ConsoleClosed from None

    def _own(self) -> Sequence[str] | None:
        if self.own_provider is None:
            return []
        try:
            return list(self.own_provider())
        except Exception:
            return []

    def banner(self, text: str) -> None:
        self._print(text)

    def on_connection(self, text: str) -> None:
        self._print(f"[net] {text}")

    def on_pine(self, text: str) -> None:
        self._print(f"[net] {text}")

    def on_error(self, message: str | None) -> None:
        """Live-source trouble (message) or recovery (None); goes to stderr so stdout stays clean data."""
        self._print(message if message else "source recovered", file=sys.stderr)

    def on_live(self, summary: FightSummary) -> None:
        first = summary.fight_id != self._live_fight
        if first or summary.end_ts - self._last_live >= self.live_interval:
            self._live_fight = summary.fight_id
            self._last_live = summary.end_ts
            self._print(format_live(summary, self.show_group, own_for(summary, self._own())))

    def on_closed(self, summary: FightSummary, run: RunSummary | None = None) -> None:
        self._print(format_fight(summary, self.show_group, own_for(summary, self._own())))

    def on_finished(self, run: RunSummary) -> None:
        self._print(format_run(run))

    def on_test(self, result: TestResult) -> None:
        self._print(format_test(result))
