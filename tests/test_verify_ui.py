"""Independent verification (session D): real Engine -> Runtime -> Tk sink wiring (no mocks of Engine), SYNTHETIC data."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from eqoa_parser import app as app_mod
from eqoa_parser.config import load_config
from eqoa_parser.persistence import RunStore
from eqoa_parser.sources import ReplaySource
from eqoa_parser.ui.console import ConsoleUI, format_fight, format_live, format_run
from eqoa_parser.ui.tk_app import TkApp
from test_ui_fixtures import make_fight

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "synthetic_combat.log"


@pytest.fixture(scope="module")
def shared_root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    r.withdraw()
    yield r
    import gc
    gc.collect()      # finalise Tk variables on the main thread, while the interpreter is alive (see test_verify_net_app)
    r.destroy()
    gc.collect()


def run_into_window(tmp_path, root, show_group=False, replay=FIXTURE):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"), show_group=show_group)
    store = RunStore(cfg.runs_dir, "replay:test")
    win = TkApp(root=tk.Toplevel(root), show_group=show_group, previous_fights=store.previous_fights, source_label="t")
    rt = app_mod.Runtime(cfg, ReplaySource(replay, speed=0), store, win.sink)
    rt.engine.run()
    win.drain()
    return win, store


def fight_rows(win):
    return [win.fight_tree.item(i, "values") for i in win.fight_tree.get_children()]


def test_real_engine_drives_window_and_second_launch_marks_previous(tmp_path, shared_root):
    win, _ = run_into_window(tmp_path, shared_root)
    rows = fight_rows(win)
    assert len(rows) == 5 and [r[1] for r in rows] == ["5", "4", "3", "2", "1"] and all(r[0] == "now" for r in rows)
    by_id = {r[1]: r for r in rows}
    assert by_id["1"][2] == "a decaying skeleton" and by_id["1"][4] == "You 110/18.3"
    s1 = next(s for s in win._fights.values() if s.fight_id == 1)
    assert (s1.you.total, round(s1.you.dps, 1), s1.pet.total, round(s1.pet.dps, 1)) == (82, 13.7, 28, 4.7)
    assert by_id["2"][3] == "0.0s" and by_id["3"][3] == "7.5s"
    assert "Fights: 5" in win.run_var.get() and "Total damage: 274" in win.run_var.get() and "Average DPS: 16.6" in win.run_var.get()
    assert "fight 2" in win.run_var.get() or "#2" in win.run_var.get()
    assert "Finished" in win.live_head.get()
    assert win.live_tree.get_children() == ()
    win.close()
    win2, _ = run_into_window(tmp_path, shared_root)
    rows2 = fight_rows(win2)
    assert len(rows2) == 10 and [r[0] for r in rows2] == ["now"] * 5 + ["prev"] * 5
    assert "Fights: 5" in win2.run_var.get()              # run summary covers the current run only
    win2.close()


def test_show_group_controls_live_and_nothing_else_leaks(tmp_path, shared_root):
    for show in (False, True):
        win, _ = run_into_window(tmp_path / str(show), shared_root, show_group=show)
        live_rows = []
        win.show_live(make_fight(3, bob=34, ongoing=True))
        live_rows = [win.live_tree.item(i, "text") for i in win.live_tree.get_children()]
        assert live_rows == (["You", "    Pet", "You combined", "Bob"] if show else ["You", "    Pet", "You combined"])
        win.close()


def test_target_less_and_zero_fights_render(tmp_path, shared_root):
    rp = tmp_path / "taken.log"
    rp.write_text("100.0\ta rat hits you for 5 points of damage.\n", encoding="utf-8")
    win, _ = run_into_window(tmp_path, shared_root, replay=rp)
    (row,) = fight_rows(win)
    assert row[2] == "-" and row[4] == "-"
    win.close()
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    win, _ = run_into_window(tmp_path / "e", shared_root, replay=empty)
    assert fight_rows(win) == [] and "Fights: 0" in win.run_var.get() and "Finished" in win.live_head.get()
    win.close()


def test_close_twice_calls_shutdown_once(shared_root):
    calls = []
    win = TkApp(root=tk.Toplevel(shared_root), on_close=lambda: calls.append(1))
    win.close()
    win.close()
    assert calls == [1]


def test_console_formatting_never_crashes_on_odd_summaries():
    f = make_fight(1, target=None, you=0, pet=0, duration=0.0)
    assert "(no target)" in format_fight(f) and "(no target)" in format_live(f)
    assert "No fights" in format_run(app_mod.summarize_run([]))
    big = make_fight(1, you=10**30, pet=0)
    format_fight(big), format_live(big)
