from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from eqoa_parser.persistence import PastFight
from eqoa_parser.ui.tk_app import TkApp
from test_ui_fixtures import make_fight, make_run


@pytest.fixture(scope="module")
def shared_root():
    # Repeatedly creating/destroying tk.Tk() in one process is flaky on Windows; use one hidden root
    # and a Toplevel per test.
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def app_factory(shared_root):
    made = []

    def make(**kw):
        a = TkApp(root=tk.Toplevel(shared_root), **kw)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


def rows(app):
    return [app.fight_tree.item(i, "values") for i in app.fight_tree.get_children()]


def test_smoke_queue_updates_widgets(app_factory):
    prev = [PastFight("r0", make_fight(1, target="old a")), PastFight("r0", make_fight(2, target="old b"))]
    app = app_factory(previous_fights=prev, source_label="replay:x", show_group=False)
    r = rows(app)  # previous fights shown newest first, marked
    assert [x[2] for x in r] == ["old b", "old a"] and all(x[0] == "prev" for x in r)

    f1 = make_fight(1, target="a rat", bob=9, ongoing=True, duration=2.0)
    app.sink.on_live(f1)
    app.sink.on_live(f1)
    assert app.drain() == 2
    assert "a rat" in app.live_head.get()
    kids = app.live_tree.get_children()
    assert [app.live_tree.item(i, "text") for i in kids] == ["You", "    Pet", "You combined"]   # Bob hidden without show_group
    assert str(app.live_tree.item(kids[0], "values")[0]) == "50"

    done = make_fight(1, target="a rat")
    app.sink.on_closed(done, make_run([done]))
    app.sink.on_finished(make_run([done]))
    app.drain()
    r = rows(app)
    assert len(r) == 3 and r[0][0] == "now" and r[0][2] == "a rat" and r[1][0] == "prev"
    assert r[0][4] == "You 70/14.0"
    assert "top abilities: melee (You) 50" in app.detail_var.get()   # newest selected by default
    app.fight_tree.selection_set(app.fight_tree.get_children()[-1])  # user picks the oldest prev row
    app.root.update()
    assert "old a" in app.detail_var.get()
    assert app.root.winfo_reqwidth() <= 560
    assert "Fights: 1" in app.run_var.get() and "Total damage: 70" in app.run_var.get()
    assert "Finished" in app.live_head.get()
    app.root.update()


def test_show_group_adds_rows(app_factory):
    app = app_factory(show_group=True)
    app.sink.on_live(make_fight(bob=9, ongoing=True))
    app.drain()
    assert [app.live_tree.item(i, "text") for i in app.live_tree.get_children()] == ["You", "    Pet", "You combined", "Bob"]


def test_always_on_top_toggle_and_error(app_factory):
    app = app_factory(always_on_top=True)
    assert bool(app.root.attributes("-topmost")) is True
    app.top_var.set(False)
    app._apply_topmost()
    assert bool(app.root.attributes("-topmost")) is False
    app.sink.on_error("boom")
    app.drain()
    assert "ERROR: boom" in app.status_var.get()


def test_close_calls_on_close_once_and_destroys(app_factory):
    calls = []
    app = app_factory(on_close=lambda: calls.append(1))
    app.start_polling()
    root = app.root
    app.close()
    app.close()
    assert calls == [1]
    with pytest.raises(tk.TclError):
        root.title()


def test_many_group_actors_all_listed_and_source_error_cleared(app_factory):
    app = app_factory(show_group=True, source_label="live:pine")
    f = make_fight(ongoing=True)
    for i in range(12):
        f.actors[f"Member{i}"] = f.actors["you"].__class__(f"Member{i}", 10 + i, 1, 10 + i, 1.0 + i)
    app.sink.on_live(f)
    app.drain()
    assert len(app.live_tree.get_children()) == 3 + 12
    app.sink.on_error("source error: PCSX2 went away")
    app.drain()
    assert app.status_var.get() == "ERROR: source error: PCSX2 went away"
    app.sink.on_error(None)
    app.drain()
    assert app.status_var.get() == "live:pine"


def test_connection_text_shown_in_status_line(app_factory):
    app = app_factory(source_label="pcap:x  run 1")
    app.sink.on_connection("connection 21173, player 0x13FF89D8")
    app.drain()
    assert app.status_var.get() == "pcap:x  run 1  |  connection 21173, player 0x13FF89D8"
    app.sink.on_error("capture error: boom")
    app.drain()
    assert app.status_var.get().startswith("ERROR: capture error")
    app.sink.on_error(None)
    app.drain()
    assert "connection 21173" in app.status_var.get()


def test_multibox_rows_and_others_in_detail_line(app_factory):
    from test_ui_fixtures import actor
    app = app_factory()
    app.own_provider = lambda: ["you", "pet", "Zecron", "Zecron's pet", "Third", "Third's pet"]
    f = make_fight(2, you=0, pet=0)
    f.actors.clear()
    for n, t in (("Zecron", 10), ("Zecron's pet", 675), ("Third", 5), ("Third's pet", 7)):
        f.actors[n] = actor(n, t, 1, t, 1.0)
    f.ongoing = True
    f.own_actors = ["you", "pet", "Zecron", "Zecron's pet", "Third", "Third's pet"]
    app.sink.on_live(f)
    app.drain()
    assert [app.live_tree.item(i, "text") for i in app.live_tree.get_children()] == [
        "You", "    Pet", "You combined", "Zecron", "    Zecron's pet", "Zecron combined", "Third",
        "    Third's pet", "Third combined", "All characters"]                    # per character, scrollable
    f.ongoing = False
    app.sink.on_closed(f, make_run([f]))
    app.drain()
    detail = {app.detail_tree.item(i, "text").strip(): app.detail_tree.item(i, "values")[0]
              for i in app.detail_tree.get_children()}
    assert [str(detail[k]) for k in ("Zecron", "Zecron's pet", "Zecron combined", "Third combined")] == ["10", "675", "685", "12"]
    assert str(detail["All characters"]) == "697"
    assert "Zecron 685/" in rows(app)[0][4] and "Third 12/" in rows(app)[0][4]
