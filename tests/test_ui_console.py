from __future__ import annotations

import io
import re

from eqoa_parser.ui.console import ConsoleUI, format_fight, format_live, format_run
from test_ui_fixtures import make_fight, make_run


def test_format_fight_you_pet_only_by_default():
    out = format_fight(make_fight(3, target="a bat", you=60, pet=20, bob=9, duration=4.0))
    assert "Fight 3: a bat - 4.0s" in out
    assert re.search(r"You\s+total\s+60\s+dps\s+15\.0\s+max hit\s+30", out)
    assert re.search(r"Pet\s+total\s+20", out)
    assert re.search(r"You combined\s+total\s+80\s+dps\s+20\.0", out) and "(damage taken 3)" in out
    assert "Top abilities: melee (You) 60, bite (Pet) 20" in out
    assert "All characters" not in out  # one group: no All line
    assert "Bob" not in out


def test_format_fight_show_group_lists_others():
    out = format_fight(make_fight(bob=9), show_group=True)
    assert "Bob" in out and "total      9" in out


def test_format_live_and_missing_target():
    f = make_fight(2, target=None, ongoing=True, duration=2.0)
    live = format_live(f)
    assert live.startswith("[live] fight 2 vs (no target) 2.0s")
    assert "You 50 (25.0 dps, max 30)" in live and "Pet 20" in live and "Bob" not in live
    assert "Bob" in format_live(make_fight(bob=4), show_group=True)


def test_format_run():
    fights = [make_fight(1, you=100, pet=0, duration=10.0), make_fight(2, target="a bat", you=90, pet=0, duration=3.0)]
    out = format_run(make_run(fights))
    assert "Fights: 2" in out and "Total damage: 190" in out
    assert "Average DPS: 14.6" in out and "Best fight: fight 2 vs a bat at 30.0 dps" in out
    assert "No fights." in format_run(make_run([]))


def test_empty_pet_shows_zero_row():
    assert re.search(r"Pet\s+total\s+0(?!\d)", format_fight(make_fight(pet=0)))


def test_console_ui_throttles_live_by_fight_time():
    buf = io.StringIO()
    ui = ConsoleUI(out=buf, live_interval=2.0)
    for i in range(6):                       # end_ts 100.5 .. 103.0
        ui.on_live(make_fight(1, duration=0.5 + i * 0.5, ongoing=True))
    ui.on_live(make_fight(2, start=200.0, ongoing=True))   # new fight always prints
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 3 and lines[-1].startswith("[live] fight 2")


def test_console_ui_closed_and_finished():
    buf = io.StringIO()
    ui = ConsoleUI(out=buf)
    f = make_fight(1)
    ui.on_closed(f, make_run([f]))
    ui.on_finished(make_run([f]))
    text = buf.getvalue()
    assert "=== Fight 1" in text and "=== Run summary ===" in text


def test_console_connection_line_on_stdout():
    buf = io.StringIO()
    ConsoleUI(out=buf).on_connection("following: you (0x00000001, port 1)")
    assert buf.getvalue() == "[net] following: you (0x00000001, port 1)\n"


def _multibox_fight():
    from test_ui_fixtures import actor
    f = make_fight(2, you=0, pet=0)
    f.actors.clear()
    f.actors["Zecron's pet"] = actor("Zecron's pet", 675, 36, 25, 8.1)
    f.actors["Bob"] = actor("Bob", 9, 1, 9, 1.0)
    f.combined_total = 675
    f.own_actors = ["you", "pet", "Zecron's pet"]
    return f


def test_rows_show_own_actors_and_group_only_on_request():
    from eqoa_parser.ui.console import format_others, row_actors
    f = _multibox_fight()
    own = ["you", "pet", "Zecron's pet"]
    assert [n for n, _ in row_actors(f, own)] == ["You", "Pet", "Zecron's pet"]
    assert [n for n, _ in row_actors(f, own, show_group=True)] == ["You", "Pet", "Zecron's pet", "Bob"]
    assert [n for n, _ in row_actors(f, None)] == ["You", "Pet"]          # single-emulator / text path unchanged
    # the fight's own list beats the caller's (current run's), e.g. for a reloaded fight
    assert [n for n, _ in row_actors(f, [])] == ["You", "Pet", "Zecron's pet"]
    f.own_actors = None                                                   # old file: you+pet rule
    assert [n for n, _ in row_actors(f, own)] == ["You", "Pet"]
    f.own_actors = own
    assert format_others(f, own) == "Zecron's pet 675 / 8.1" and format_others(f, None) == ""
    # a Phase 3 file (own_actors but no groups) still shows the other character, now as its own group
    assert re.search(r"Zecron's pet\s+total\s+675", format_fight(f, own=own))
    assert "Zecron's pet 675 (8.1 dps, max 25)" in format_live(f, own=own) and "Bob" not in format_live(f, own=own)


def test_console_ui_uses_own_provider():
    buf = io.StringIO()
    ui = ConsoleUI(out=buf)
    ui.own_provider = lambda: ["you", "pet", "Zecron's pet"]
    f = _multibox_fight()
    ui.on_closed(f, make_run([f]))
    assert "Zecron's pet" in buf.getvalue() and "Bob" not in buf.getvalue()
