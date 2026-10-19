"""Tkinter window. OWNER: session C.

Thin on purpose: all state arrives as messages on a queue.Queue and is applied in `drain()`, so a
different front end (e.g. a click-through overlay) only needs to consume the same messages.

Messages (tuples):
    ("live", FightSummary)
    ("closed", FightSummary, RunSummary)
    ("finished", RunSummary)
    ("error", str)
"""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk
from typing import Callable, Sequence

from ..persistence import PastFight
from ..stats import FightSummary, RunSummary, group_rows
from .console import DisplayRow, display_rows, format_abilities


class QueueSink:
    """Engine-thread side: same interface as ConsoleUI, just enqueues."""

    def __init__(self, q: "queue.Queue[tuple]") -> None:
        self.q = q

    def on_live(self, summary: FightSummary) -> None:
        self.q.put(("live", summary))

    def on_closed(self, summary: FightSummary, run: RunSummary) -> None:
        self.q.put(("closed", summary, run))

    def on_finished(self, run: RunSummary) -> None:
        self.q.put(("finished", run))

    def on_connection(self, text: str) -> None:
        self.q.put(("connection", text))

    def on_error(self, message: str | None) -> None:  # None = recovered
        self.q.put(("error", message))

    def on_pine(self, text: str) -> None:
        self.q.put(("pine", text))


FIGHT_COLUMNS = (
    ("run", "Run", 38, "center"),
    ("id", "#", 24, "e"),
    ("target", "Target", 92, "w"),
    ("dur", "Dur", 46, "e"),
    ("chars", "Characters (combined dmg/dps)", 250, "w"),
)


def characters_cell(s: FightSummary) -> str:
    """Compact per-character combined cell: ``Zecron 675/8.1 · You 20670/251.5`` (characters that dealt damage)."""
    parts = [f"{g.name} {g.combined_total}/{g.combined_dps:.1f}" for g in group_rows(s) if g.combined_total > 0]
    return " · ".join(parts) if parts else "-"


def fill_rows(tree: ttk.Treeview, rows: Sequence[DisplayRow]) -> None:
    """Grouped breakdown: Name, Name's pet (indented), Name combined (bold), then All characters (bold)."""
    tree.delete(*tree.get_children())
    for r in rows:
        text = ("    " + r.label) if r.kind == "pet" else r.label
        tree.insert("", "end", text=text, tags=(r.kind,),
                    values=(r.total, f"{r.dps:.1f}", "" if r.max_hit is None else r.max_hit))


def make_breakdown_tree(parent: tk.Misc, height: int) -> ttk.Treeview:
    tree = ttk.Treeview(parent, columns=("total", "dps", "max"), height=height, selectmode="none")
    tree.heading("#0", text="Character")
    tree.column("#0", width=170, anchor="w")
    for col, text in (("total", "Total"), ("dps", "DPS"), ("max", "Max hit")):
        tree.heading(col, text=text)
        tree.column(col, width=70, anchor="e")
    bold = ("TkDefaultFont", 9, "bold")
    tree.tag_configure("combined", font=bold, background="#eef2f7")
    tree.tag_configure("all", font=bold, background="#dde6f0")
    return tree


class TkApp:
    def __init__(
        self,
        *,
        show_group: bool = False,
        always_on_top: bool = True,
        refresh_ms: int = 250,
        previous_fights: Sequence[PastFight] = (),
        source_label: str = "",
        on_close: Callable[[], None] | None = None,
        root: tk.Misc | None = None,  # a Tk or Toplevel; destroyed on close()
    ) -> None:
        self.show_group = show_group
        self.own_provider: Callable[[], Sequence[str]] | None = None  # set by the app for multi-emulator runs
        self.refresh_ms = refresh_ms
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.sink = QueueSink(self.queue)
        self._on_close = on_close
        self._after_id: str | None = None
        self.closed_ui = False
        self._fights: dict[str, FightSummary] = {}
        self.recording = None  # a RecordingController (live sources only); see set_recording()
        self.ask_note: Callable[[], str | None] = self._ask_note

        self.root = root or tk.Tk()
        self.root.title("EQOA Parser")
        self.root.geometry("500x840")
        self.root.minsize(420, 420)
        self.top_var = tk.BooleanVar(master=self.root, value=always_on_top)
        self._apply_topmost()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._build(source_label)
        # previous runs: oldest first in the file; inserting each at the top leaves the newest first.
        for pf in previous_fights:
            self._insert_fight(pf.summary, mark="prev", tag="prev")
        self._select_newest()
        self.show_idle()
        self.set_run(None)

    # -- layout ----------------------------------------------------------------------------------
    def _build(self, source_label: str) -> None:
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        ttk.Checkbutton(bar, text="Always on top", variable=self.top_var, command=self._apply_topmost).pack(side="left")
        self.mark_button = ttk.Button(bar, text="Mark", command=self.mark_clicked, state="disabled")
        self.mark_button.pack(side="right")
        self.record_button = ttk.Button(bar, text="Record", command=self.toggle_record, state="disabled")
        self.record_button.pack(side="right", padx=(0, 4))
        self._label = source_label
        self._base_status = source_label
        self.status_var = tk.StringVar(master=self.root, value=source_label)
        status = ttk.Label(outer, textvariable=self.status_var, foreground="#666", wraplength=470, justify="left")
        status.bind("<Configure>", lambda e: status.configure(wraplength=max(120, e.width - 4)))
        status.pack(fill="x", pady=(2, 0))

        live = ttk.LabelFrame(outer, text="Current fight", padding=4)
        live.pack(fill="x", pady=(6, 0))
        self.live_head = tk.StringVar(master=self.root)
        ttk.Label(live, textvariable=self.live_head).pack(anchor="w")
        self.live_tree = make_breakdown_tree(live, height=5)
        if True:  # many rows (several characters, group actors): scroll instead of growing
            lsb = ttk.Scrollbar(live, orient="vertical", command=self.live_tree.yview)
            self.live_tree.configure(yscrollcommand=lsb.set)
            lsb.pack(side="right", fill="y")
        self.live_tree.pack(fill="x")

        run = ttk.LabelFrame(outer, text="This run", padding=4)
        run.pack(fill="x", pady=(6, 0))
        self.run_var = tk.StringVar(master=self.root)
        ttk.Label(run, textvariable=self.run_var, justify="left").pack(anchor="w")

        lst = ttk.LabelFrame(outer, text="Fights (newest first; grey prev = earlier runs)", padding=4)
        lst.pack(fill="both", expand=True, pady=(6, 0))
        self.fight_tree = ttk.Treeview(lst, columns=[c[0] for c in FIGHT_COLUMNS], show="headings", selectmode="browse")
        for key, text, width, anchor in FIGHT_COLUMNS:
            self.fight_tree.heading(key, text=text)
            self.fight_tree.column(key, width=width, anchor=anchor, stretch=(key == "target"))
        self.fight_tree.tag_configure("prev", foreground="#888888")
        self.detail_var = tk.StringVar(master=self.root)
        detail = ttk.Label(lst, textvariable=self.detail_var, wraplength=440, justify="left")
        detail.bind("<Configure>", lambda e: detail.configure(wraplength=max(120, e.width - 8)))
        detail.pack(side="bottom", fill="x", pady=(4, 0))
        self.detail_tree = make_breakdown_tree(lst, height=10)
        self.detail_tree.pack(side="bottom", fill="x", pady=(4, 0))
        sb = ttk.Scrollbar(lst, orient="vertical", command=self.fight_tree.yview)
        self.fight_tree.configure(yscrollcommand=sb.set)
        self.fight_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.fight_tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())

    # -- state updates ---------------------------------------------------------------------------
    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.top_var.get()))

    def _own(self) -> Sequence[str] | None:
        if self.own_provider is None:
            return []
        try:
            return list(self.own_provider())
        except Exception:
            return []

    def show_idle(self, note: str = "Idle - waiting for combat") -> None:
        self.live_head.set(note)
        self.live_tree.delete(*self.live_tree.get_children())

    def show_live(self, s: FightSummary) -> None:
        self.live_head.set(f"Fight {s.fight_id} vs {s.target or '(no target)'}  -  {s.duration:.1f}s")
        self.live_tree.delete(*self.live_tree.get_children())
        fill_rows(self.live_tree, display_rows(s, self.show_group))

    def _insert_fight(self, s: FightSummary, *, mark: str, tag: str = "") -> None:
        vals = (mark, s.fight_id, s.target or "-", f"{s.duration:.1f}s", characters_cell(s))
        iid = self.fight_tree.insert("", 0, values=vals, tags=(tag,) if tag else ())
        self._fights[iid] = s

    def _select_newest(self) -> None:
        kids = self.fight_tree.get_children()
        if kids:
            self.fight_tree.selection_set(kids[0])
            self.fight_tree.see(kids[0])
        self._show_detail()

    def _show_detail(self) -> None:
        sel = self.fight_tree.selection()
        s = self._fights.get(sel[0]) if sel else None
        if s is None:
            self.detail_var.set("Top abilities: select a fight")
            self.detail_tree.delete(*self.detail_tree.get_children())
            return
        fill_rows(self.detail_tree, display_rows(s, self.show_group))
        self.detail_var.set(f"Fight {s.fight_id} vs {s.target or '-'} - top abilities: {format_abilities(s)}")

    def set_run(self, run: RunSummary | None) -> None:
        if run is None or run.fight_count == 0:
            self.run_var.set("Fights: 0   Total damage: 0   Average DPS: 0.0   Best fight: -")
            return
        best = f"#{run.best_fight_id} {run.best_fight_target or '-'} ({run.best_fight_dps:.1f} dps)"
        lines = [
            f"Fights: {run.fight_count}   Total damage: {run.total_damage}   "
            f"Average DPS: {run.average_dps:.1f}   Best fight: {best}"
        ]
        for c in run.characters:
            fight = f"best #{c.best_fight_id} ({c.best_dps:.1f})" if c.best_fight_id is not None else "-"
            lines.append(f"{c.name}: {c.total} dmg  {c.dps:.1f} dps  {fight}")
        self.run_var.set("\n".join(lines))

    def apply(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "live":
            self.show_live(msg[1])
        elif kind == "closed":
            kids = self.fight_tree.get_children()
            follow = not self.fight_tree.selection() or (kids and self.fight_tree.selection()[0] == kids[0])
            self._insert_fight(msg[1], mark="now")
            if follow:  # keep the newest fight selected unless the user picked another row
                self._select_newest()
            self.set_run(msg[2])
            self.show_idle()
        elif kind == "finished":
            self.set_run(msg[1])
            self.show_idle("Finished - source ended")
        elif kind == "connection":
            self._connection_text = msg[1]
            self._refresh_status()
        elif kind == "pine":
            self._pine_text = msg[1]
            self._refresh_status()
        elif kind == "error":
            self.status_var.set(f"ERROR: {msg[1]}" if msg[1] else self._base_status)

    # -- recording ---------------------------------------------------------------------------------
    def set_recording(self, controller) -> None:
        """Enable the Record/Mark buttons for a live source (None = replay: both stay disabled)."""
        self.recording = controller
        self._sync_record()

    def _sync_record(self) -> None:
        rec = self.recording
        active = rec is not None and rec.active
        self.record_button.configure(text="Stop recording" if active else "Record",
                                     state="normal" if rec is not None else "disabled")
        self.mark_button.configure(state="normal" if active else "disabled")
        text = rec.status_text() if rec is not None else None
        if text != getattr(self, "_record_text", None):
            self._record_text = text
            self._refresh_status()

    def toggle_record(self) -> None:
        rec = self.recording
        if rec is None:
            return
        try:
            if rec.active:
                rec.stop()
            else:
                rec.start()
        except OSError as exc:
            self.apply(("error", f"cannot record: {exc}"))
        self._sync_record()

    def _ask_note(self) -> str | None:
        from tkinter import simpledialog

        return simpledialog.askstring("Mark", "Short note for this moment:", initialvalue="mark", parent=self.root)

    def mark_clicked(self) -> None:
        rec = self.recording
        if rec is None or not rec.active:
            return
        note = self.ask_note()
        if note is None:  # cancelled
            return
        rec.mark(note.strip() or "mark")

    def _refresh_status(self) -> None:
        parts = [self._label, getattr(self, "_connection_text", None), getattr(self, "_pine_text", None),
                 getattr(self, "_record_text", None)]
        self._base_status = "  |  ".join(p for p in parts if p)
        if not self.status_var.get().startswith("ERROR"):
            self.status_var.set(self._base_status)

    def drain(self) -> int:
        """Apply every queued message; of a burst of 'live' messages only the newest is rendered."""
        n = 0
        pending_live: FightSummary | None = None
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            n += 1
            if msg[0] == "live":
                pending_live = msg[1]
                continue
            pending_live = None  # superseded by this closed/finished/error message
            self.apply(msg)
        if pending_live is not None:
            self.show_live(pending_live)
        return n

    # -- main loop -------------------------------------------------------------------------------
    def _tick(self) -> None:
        if self.closed_ui:
            return
        self.drain()
        if self.recording is not None:
            self._sync_record()  # packet count in the status line; also notices a stopped writer
        self._after_id = self.root.after(self.refresh_ms, self._tick)

    def start_polling(self) -> None:
        self._after_id = self.root.after(self.refresh_ms, self._tick)

    def mainloop(self) -> None:
        self.start_polling()
        self.root.mainloop()

    def close(self) -> None:
        if self.closed_ui:
            return
        self.closed_ui = True
        if self._on_close is not None:
            self._on_close()  # stops the engine thread before the window goes away
        try:
            if self._after_id is not None:
                self.root.after_cancel(self._after_id)
            self.root.destroy()
        except tk.TclError:
            pass
