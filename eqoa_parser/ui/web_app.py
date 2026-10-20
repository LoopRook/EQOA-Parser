"""Web front end: a pywebview window showing eqoa_parser/ui/web/ (HTML/CSS/JS).

Same sink contract as the Tk window (on_live / on_closed / on_finished / on_test / on_connection / on_pine /
on_error), so ``app.run_gui`` drives either. The engine thread only enqueues; a pump thread serialises to JSON
dicts (``WebSink.drain``), folds them into ``WebState`` and pushes them to the page with ``window.evaluate_js``.
Everything up to ``WebApp`` is pure Python (no pywebview import), so it is tested without a window.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from ..persistence import PastFight
from ..stats import (FightSummary, RunSummary, effective_duration, group_rows)
from ..testmode import TestResult
from .console import DisplayRow, display_rows, flow_lines, format_abilities, progress_text, result_rows

WEB_DIR = Path(__file__).resolve().parent / "web"
if getattr(sys, "frozen", False):  # PyInstaller: data files are unpacked under sys._MEIPASS
    WEB_DIR = Path(getattr(sys, "_MEIPASS", WEB_DIR.parent)) / "eqoa_parser" / "ui" / "web"

COMPACT_W = 340
EXPANDED_SIZE = (640, 720)
MODES = ("compact", "expanded")


class WebUnavailable(Exception):
    """pywebview or the WebView2 runtime cannot be used: the caller falls back to the classic Tk window."""


# -- payloads ---------------------------------------------------------------------------------------------
def _r1(x: float) -> float:
    return round(float(x), 1)


def _actor(summary: FightSummary, name: str, key: str, eff: float) -> dict[str, Any]:
    a = summary.actor(key)
    t = (summary.taken or {}).get(key)
    h = (summary.heals or {}).get(key)
    taken, healed = (t.total if t else 0), (h.done if h else 0)
    return {"name": name, "key": key, "total": a.total, "dps": _r1(a.dps), "max_hit": a.max_hit,
            "taken": taken, "dtps": _r1(taken / eff), "healed": healed, "hps": _r1(healed / eff),
            "received": h.received if h else 0,
            "active": bool(a.total or taken or healed or (h.received if h else 0))}


def row_payload(r: DisplayRow) -> dict[str, Any]:
    return {"label": r.label, "kind": r.kind, "total": r.total, "dps": _r1(r.dps), "max_hit": r.max_hit,
            "taken": r.taken, "healed": r.healed}


def fight_payload(s: FightSummary, show_group: bool = False) -> dict[str, Any]:
    """Everything the page draws for one fight (compact bars, FIGHTS row, DETAIL tab)."""
    eff = effective_duration(s.duration)
    groups = []
    for g in group_rows(s):
        groups.append({
            "name": g.name, "key": g.key,
            "player": _actor(s, g.name, g.player.actor, eff),
            "pet": _actor(s, g.pet_name, g.pet.actor, eff),
            "combined_total": g.combined_total, "combined_dps": _r1(g.combined_dps),
            "taken_total": g.taken_total, "taken_max": g.taken_max, "dtps": _r1(g.dtps),
            "heal_done": g.heal_done, "hps": _r1(g.hps), "heal_recv": g.heal_recv,
            "taken_sources": [list(x) for x in g.taken_sources], "recv_sources": [list(x) for x in g.recv_sources],
        })
    max_hit = max([s.actor(k).max_hit for k in
                   {x for gr in group_rows(s) for x in (gr.player.actor, gr.pet.actor)}] + [0])
    heal_done = sum(g["heal_done"] for g in groups)
    taken = sum(g["taken_total"] for g in groups)
    return {
        "id": s.fight_id, "target": s.target, "duration": _r1(s.duration), "ongoing": bool(s.ongoing),
        "start_ts": s.start_ts, "end_ts": s.end_ts,
        "groups": groups,
        "totals": {"damage": sum(g["combined_total"] for g in groups),
                   "dps": _r1(sum(g["combined_dps"] for g in groups)),
                   "taken": taken, "dtps": _r1(taken / eff), "healed": heal_done, "hps": _r1(heal_done / eff),
                   "max_hit": max_hit},
        "rows": [row_payload(r) for r in display_rows(s, show_group)],
        "lines": flow_lines(s),
        "abilities": format_abilities(s),
    }


def progress_payload(run: RunSummary) -> list[dict[str, Any]]:
    out = []
    for p in run.progress:
        if not p.any():
            continue
        per_hour = (lambda v: round(v * 3600 / run.elapsed) if run.elapsed > 0 else None)
        out.append({"name": p.name, "key": p.key, "kills": p.kills, "xp": p.xp, "mastery": p.mastery,
                    "tunar": p.tunar, "debt": p.debt, "loot": p.loot,
                    "per_hour": {"kills": per_hour(p.kills), "xp": per_hour(p.xp), "mastery": per_hour(p.mastery),
                                 "tunar": per_hour(p.tunar)},
                    "text": progress_text(p, run.elapsed)})
    return out


def run_payload(run: RunSummary) -> dict[str, Any]:
    prog = progress_payload(run)
    totals = {k: sum(p[k] for p in prog) for k in ("kills", "xp", "mastery", "tunar")}
    totals["per_hour"] = {k: (round(totals[k] * 3600 / run.elapsed) if run.elapsed > 0 else None)
                          for k in ("kills", "xp", "mastery", "tunar")}
    return {
        "fight_count": run.fight_count, "total_damage": run.total_damage, "average_dps": _r1(run.average_dps),
        "best": None if run.best_fight_id is None else
        {"id": run.best_fight_id, "target": run.best_fight_target, "dps": _r1(run.best_fight_dps)},
        "characters": [{"name": c.name, "total": c.total, "dps": _r1(c.dps), "best_fight_id": c.best_fight_id,
                        "best_dps": _r1(c.best_dps)} for c in run.characters],
        "progress": prog, "progress_totals": totals, "elapsed": _r1(run.elapsed),
    }


def result_payload(r: TestResult) -> dict[str, Any]:
    return {"label": r.label, "length_s": r.length_s, "complete": r.complete, "start_ts": r.start_ts,
            "total": r.total, "dps": _r1(r.dps), "hits": r.hits, "max_hit": r.max_hit, "pets": r.pet_names,
            "rows": [row_payload(x) for x in result_rows(r)]}


# -- sink + state -----------------------------------------------------------------------------------------
class WebSink:
    """Engine-thread side. Callbacks only enqueue; ``drain()`` (pump thread) returns JSON-ready dict messages,
    keeping just the newest of a burst of ``live`` updates."""

    def __init__(self, show_group: bool = False) -> None:
        self.show_group = show_group
        self.q: "queue.Queue[tuple]" = queue.Queue()

    def on_live(self, summary: FightSummary) -> None:
        self.q.put(("live", summary))

    def on_closed(self, summary: FightSummary, run: RunSummary) -> None:
        self.q.put(("closed", summary, run))

    def on_finished(self, run: RunSummary) -> None:
        self.q.put(("finished", run))

    def on_test(self, result: TestResult) -> None:
        self.q.put(("test", result))

    def on_connection(self, text: str) -> None:
        self.q.put(("connection", text))

    def on_pine(self, text: str) -> None:
        self.q.put(("pine", text))

    def on_error(self, message: str | None) -> None:  # None = recovered
        self.q.put(("error", message))

    def drain(self) -> list[dict[str, Any]]:
        raw: list[tuple] = []
        while True:
            try:
                raw.append(self.q.get_nowait())
            except queue.Empty:
                break
        out: list[dict[str, Any]] = []
        pending_live: FightSummary | None = None
        for m in raw:
            kind = m[0]
            if kind == "live":
                pending_live = m[1]
                continue
            if pending_live is not None and kind in ("closed", "finished"):
                pending_live = None  # superseded
            out.append(self._encode(m))
        if pending_live is not None:
            out.append(self._encode(("live", pending_live)))
        return out

    def _encode(self, m: tuple) -> dict[str, Any]:
        kind = m[0]
        if kind == "live":
            return {"type": "live", "fight": fight_payload(m[1], self.show_group)}
        if kind == "closed":
            return {"type": "closed", "fight": fight_payload(m[1], self.show_group), "run": run_payload(m[2])}
        if kind == "finished":
            return {"type": "finished", "run": run_payload(m[1])}
        if kind == "test":
            return {"type": "test", "result": result_payload(m[1])}
        return {"type": kind, "text": m[1]}


class WebState:
    """What the page needs to be rebuilt from scratch (reload, late ``ready``), folded from the sink messages.
    Owned by the pump thread only."""

    def __init__(self, source_label: str = "", previous_fights: Sequence[PastFight] = (),
                 previous_tests: Sequence[TestResult] = (), show_group: bool = False) -> None:
        self.fights: list[dict[str, Any]] = []   # newest first
        for pf in previous_fights:
            p = fight_payload(pf.summary, show_group)
            self.fights.insert(0, dict(p, uid=f"p{pf.run_id}-{p['id']}", prev=True, run_id=pf.run_id))
        self.tests: list[dict[str, Any]] = []
        for r in previous_tests:
            self.tests.insert(0, result_payload(r))
        self.live: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = self.fights[0] if self.fights else None
        self.run: dict[str, Any] | None = None
        self.status: dict[str, Any] = {
            "label": source_label, "connection": None, "pine": None, "record": None, "test": None, "error": None,
            "state": "idle", "can_record": False, "recording": False, "can_test": False, "test_active": False}

    def set_status(self, **kw: Any) -> bool:
        """Update status fields; True when something changed."""
        changed = False
        for k, v in kw.items():
            if self.status.get(k) != v:
                self.status[k] = v
                changed = True
        return changed

    def apply(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        """Fold one sink message; returns the messages to push to the page."""
        t = msg["type"]
        if t == "live":
            self.live = msg["fight"]
            return [msg] + ([self._status_msg()] if self.set_status(state="live") else [])
        if t == "closed":
            f = dict(msg["fight"], uid=f"n{msg['fight']['id']}", prev=False)
            self.fights.insert(0, f)
            self.live, self.last, self.run = None, f, msg["run"]
            self.set_status(state="idle")
            return [{"type": "closed", "fight": f, "run": msg["run"]}, self._status_msg()]
        if t == "finished":
            self.run, self.live = msg["run"], None
            self.set_status(state="finished")
            return [msg, self._status_msg()]
        if t == "test":
            self.tests.insert(0, msg["result"])
            return [msg]
        changed = self.set_status(**{"error" if t == "error" else t: msg["text"]})
        return [self._status_msg()] if changed else []

    def _status_msg(self) -> dict[str, Any]:
        return {"type": "status", "status": dict(self.status)}

    def snapshot(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {"type": "snapshot", "settings": settings, "fights": self.fights, "live": self.live,
                "last": self.last, "tests": self.tests, "run": self.run, "status": dict(self.status)}


def to_json(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=True, separators=(",", ":"))


# -- window layout persistence ----------------------------------------------------------------------------
def load_layout(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_layout(path: Path | None, layout: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        tmp = Path(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(layout, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # remembering the window position is never worth an error


def clamp_to_screen(x: int, y: int, w: int, h: int, screen: tuple[int, int, int, int]) -> tuple[int, int]:
    """Move (x, y) so at least a 60 px strip of the window stays inside `screen` = (left, top, right, bottom)."""
    left, top, right, bottom = screen
    x = min(max(x, left - w + 60), right - 60)
    y = min(max(y, top), bottom - 40)
    return x, y


def _virtual_screen() -> tuple[int, int, int, int]:
    try:
        import ctypes

        u = ctypes.windll.user32
        l, t = u.GetSystemMetrics(76), u.GetSystemMetrics(77)      # SM_XVIRTUALSCREEN / YVIRTUALSCREEN
        return l, t, l + u.GetSystemMetrics(78), t + u.GetSystemMetrics(79)
    except Exception:
        return 0, 0, 1920, 1080


# -- availability -----------------------------------------------------------------------------------------
def webview2_present() -> bool:
    """The Evergreen WebView2 runtime (ships with Windows 11) is registered. Non-Windows: True (other backends)."""
    if sys.platform != "win32":
        return True
    import winreg

    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
                      (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients")):
        try:
            with winreg.OpenKey(hive, sub + "\\" + guid) as k:
                if winreg.QueryValueEx(k, "pv")[0] not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


def check_available() -> str | None:
    """None when the web UI can run, else the reason (one line) for the fallback to the classic window."""
    try:
        import webview  # noqa: F401
    except Exception as exc:
        return f"pywebview is not available ({type(exc).__name__}: {exc})"
    if sys.platform == "win32":
        if not webview2_present():
            return "the Microsoft WebView2 runtime is not installed"
        try:
            from webview.platforms import winforms  # noqa: F401  (loads pythonnet / the CLR)
        except Exception as exc:
            return f"pywebview cannot load its Windows backend ({type(exc).__name__}: {exc})"
    return None


# -- the window -------------------------------------------------------------------------------------------
class WebApi:
    """Methods the page calls as ``pywebview.api.<name>(...)``; each returns plain JSON data."""

    def __init__(self, app: "WebApp") -> None:
        self._app = app

    def ready(self) -> bool:
        self._app._need_snapshot.set()
        return True

    def set_view(self, expanded: bool, height: int = 0) -> bool:
        return self._app.set_view(bool(expanded))

    def fit(self, height: int) -> bool:
        return self._app.fit(int(height))

    def start_test(self, label: str, seconds: float) -> dict[str, Any]:
        return self._app.start_test(str(label), seconds)

    def cancel_test(self) -> bool:
        self._app.cancel_test()
        return True

    def toggle_record(self) -> dict[str, Any]:
        return self._app.toggle_record()

    def mark(self, note: str) -> bool:
        rec = self._app.recording
        return bool(rec is not None and rec.active and rec.mark((note or "").strip() or "mark"))

    def set_topmost(self, flag: bool) -> bool:
        return self._app.set_topmost(bool(flag))

    def set_theme(self, theme: str) -> bool:
        if theme in ("light", "dark"):
            self._app.layout["theme"] = theme
            self._app.persist_layout()
        return True

    def minimize(self) -> bool:
        self._app.window.minimize()
        return True

    def close(self) -> bool:
        self._app.close()
        return True


class WebApp:
    """Drop-in for TkApp in ``app.run_gui``: ``.sink``, ``set_tests``, ``set_recording``, ``own_provider``,
    ``mainloop()``. Raises WebUnavailable (before anything starts) when the web UI cannot run here."""

    def __init__(
        self,
        *,
        show_group: bool = False,
        always_on_top: bool = True,
        refresh_ms: int = 250,
        previous_fights: Sequence[PastFight] = (),
        source_label: str = "",
        on_close: Callable[[], None] | None = None,
        layout_path: Path | None = None,
    ) -> None:
        reason = check_available()
        if reason is not None:
            raise WebUnavailable(reason)
        import webview

        self._webview = webview
        self.show_group = show_group
        self.refresh_s = max(0.05, refresh_ms / 1000)
        self.sink = WebSink(show_group)
        self.state = WebState(source_label, previous_fights, (), show_group)
        self.own_provider: Callable[[], Sequence[str]] | None = None
        self.recording = None
        self.tests_api = None
        self.active_test = None
        self._on_close = on_close
        self._layout_path = Path(layout_path) if layout_path else None
        self.layout: dict[str, Any] = load_layout(self._layout_path)
        self.layout.setdefault("topmost", always_on_top)
        self.expanded = False
        self._need_snapshot = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._fit_h = 360

        pos = (self.layout.get("compact") or {})
        x, y = pos.get("x"), pos.get("y")
        if isinstance(x, int) and isinstance(y, int):
            x, y = clamp_to_screen(x, y, COMPACT_W, self._fit_h, _virtual_screen())
        else:
            x = y = None
        self.window = webview.create_window(
            "EQOA Parser", url=str(WEB_DIR / "index.html"), js_api=WebApi(self),
            width=COMPACT_W, height=self._fit_h, x=x, y=y, frameless=True, easy_drag=False,
            on_top=bool(self.layout["topmost"]), resizable=False, background_color="#FFFFFF",
        )
        self.window.events.closing += self._on_closing

    # -- wiring used by run_gui --------------------------------------------------------------------------
    def set_tests(self, api, previous: Sequence[TestResult] = ()) -> None:
        self.tests_api = api
        for r in previous:
            self.state.tests.insert(0, result_payload(r))
        self.state.set_status(can_test=True)

    def set_recording(self, controller) -> None:
        self.recording = controller
        self.state.set_status(can_record=controller is not None)

    # -- page -> python ----------------------------------------------------------------------------------
    def start_test(self, label: str, seconds: Any) -> dict[str, Any]:
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            secs = 0.0
        if self.tests_api is None:
            return {"ok": False, "error": "tests are not available"}
        if not 0 < secs < 86400:
            return {"ok": False, "error": "Length must be a number of seconds above 0."}
        self.active_test = self.tests_api.arm_test(label.strip() or "test", secs)
        return {"ok": True}

    def cancel_test(self) -> None:
        if self.tests_api is not None:
            self.tests_api.cancel_test()
        self.active_test = None

    def toggle_record(self) -> dict[str, Any]:
        rec = self.recording
        if rec is None:
            return {"ok": False, "error": "recording is only available with --live"}
        try:
            rec.stop() if rec.active else rec.start()
        except OSError as exc:
            return {"ok": False, "error": f"cannot record: {exc}"}
        return {"ok": True}

    def set_topmost(self, flag: bool) -> bool:
        self.layout["topmost"] = flag
        try:
            self.window.on_top = flag
        except Exception:
            pass
        self.persist_layout()
        return flag

    def _size_for(self, expanded: bool) -> tuple[int, int]:
        if expanded:
            s = self.layout.get("expanded") or {}
            return int(s.get("w") or EXPANDED_SIZE[0]), int(s.get("h") or EXPANDED_SIZE[1])
        return COMPACT_W, self._fit_h

    def set_view(self, expanded: bool) -> bool:
        """Switch compact <-> expanded, remembering the position of the mode being left."""
        if expanded == self.expanded:
            return True
        cur = self._remember_position()
        self.expanded = expanded
        new = "expanded" if expanded else "compact"
        w, h = self._size_for(expanded)
        pos = self.layout.get(new) or {}
        self.window.resize(w, h)
        x, y = pos.get("x", cur.get("x")), pos.get("y", cur.get("y"))
        if isinstance(x, int) and isinstance(y, int):
            x, y = clamp_to_screen(x, y, w, h, _virtual_screen())
            self.window.move(x, y)
        self.persist_layout()
        return True

    def fit(self, height: int) -> bool:
        """The compact page reports its content height; the window follows (expanded keeps its own size)."""
        h = max(120, min(900, height + 2))
        self._fit_h = h
        if not self.expanded:
            self.window.resize(COMPACT_W, h)
        return True

    # -- events ------------------------------------------------------------------------------------------
    def _remember_position(self) -> dict[str, int]:
        """Store the window's current position under the current mode (the `moved` event reports odd values
        around close/resize, so the live window.x/y is read instead)."""
        try:
            cur = {"x": int(self.window.x), "y": int(self.window.y)}
        except Exception:
            return {}
        self.layout.setdefault("expanded" if self.expanded else "compact", {}).update(cur)
        return cur

    def persist_layout(self) -> None:
        save_layout(self._layout_path, self.layout)

    def _on_closing(self) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._remember_position()
        self.persist_layout()
        if self._on_close is not None:
            self._on_close()  # stops the engine thread before the window goes away
        try:
            self.window.destroy()
        except Exception:
            pass

    # -- pump --------------------------------------------------------------------------------------------
    def _settings(self) -> dict[str, Any]:
        return {"theme": self.layout.get("theme"), "topmost": bool(self.layout.get("topmost", True)),
                "expanded": self.expanded}

    def _poll_status(self) -> None:
        t = self.active_test
        if t is not None and not t.active:
            self.active_test = t = None
        rec = self.recording
        self.state.set_status(
            test=t.status_text() if t is not None else None, test_active=t is not None,
            record=rec.status_text() if rec is not None else None,
            recording=bool(rec is not None and rec.active))

    def _push(self, msg: dict[str, Any]) -> None:
        self.window.evaluate_js(f"window.eqoa&&window.eqoa.push({to_json(msg)})")

    def pump_once(self) -> list[dict[str, Any]]:
        """One pump cycle: drain the sink, fold into the state, return the messages for the page."""
        out: list[dict[str, Any]] = []
        if self._need_snapshot.is_set():
            self._need_snapshot.clear()
            self._poll_status()
            out.append(self.state.snapshot(self._settings()))
        for m in self.sink.drain():
            out += self.state.apply(m)
        before = dict(self.state.status)
        self._poll_status()
        if self.state.status != before:
            out.append(self.state._status_msg())
        # of repeated status messages only the last matters
        seen_status = False
        pruned: list[dict[str, Any]] = []
        for m in reversed(out):
            if m["type"] == "status":
                if seen_status:
                    continue
                seen_status = True
            pruned.append(m)
        return pruned[::-1]

    def _pump(self) -> None:
        while not self._stop.wait(self.refresh_s):
            try:
                for m in self.pump_once():
                    self._push(m)
            except Exception as exc:  # a closing window can raise here; the meter must not die loudly
                if self._stop.is_set():
                    break
                print(f"warning: web UI update failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def mainloop(self) -> None:
        threading.Thread(target=self._pump, name="eqoa-web-pump", daemon=True).start()
        try:
            self._webview.start(gui="edgechromium" if sys.platform == "win32" else None)
        finally:
            self._stop.set()
            self.persist_layout()
