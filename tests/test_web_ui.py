"""Web UI (pywebview) data path: WebSink / WebState JSON, the fixtures' totals, layout file, and the Tk fallback.
No window is opened; the pywebview import itself is only probed or faked."""
from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path

import pytest

from eqoa_parser import app
from eqoa_parser.config import load_config
from eqoa_parser.net.sources import PcapReplaySource
from eqoa_parser.persistence import PastFight, RunStore
from eqoa_parser.testmode import TestCharacter, TestResult
from eqoa_parser.ui import web_app
from eqoa_parser.ui.web_app import (WEB_DIR, WebSink, WebState, WebUnavailable, clamp_to_screen, fight_payload,
                                    load_layout, run_payload, save_layout, result_payload, to_json)
from test_ui_fixtures import make_fight, make_run

ROOT = Path(__file__).resolve().parent.parent
CAP2 = ROOT / "fixtures" / "real" / "cap2_bard_melee_necro_pet.pcapng"
CAP5 = ROOT / "fixtures" / "real" / "cap5_mage_spells_then_pet.pcapng"


def replay(path, tmp_path, port=None):
    """Run a fixture through the real Runtime with a WebSink; returns (sink, runtime)."""
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), runs_dir=str(tmp_path / "runs"))
    cfg.runs_dir = str(tmp_path / "runs")
    if port is not None:
        cfg.net.local_port = port
    sink = WebSink()
    rt = app.Runtime(cfg, PcapReplaySource(path, cfg, speed=0), RunStore(cfg.runs_dir, "t"), sink)
    rt.engine.run()
    return sink, rt


def roundtrip(msg):
    """Everything the page receives must survive JSON."""
    return json.loads(to_json(msg))


# -- serialisation of each payload type ---------------------------------------------------------------------
def test_live_closed_finished_messages_are_json():
    s = make_fight(fid=3, target="a rat", you=50, pet=20, duration=5.0)
    run = make_run([s])
    sink = WebSink()
    sink.on_live(s)
    sink.on_live(s)                      # a burst keeps only the newest live message
    msgs = [roundtrip(m) for m in sink.drain()]
    assert [m["type"] for m in msgs] == ["live"]
    f = msgs[0]["fight"]
    assert (f["id"], f["target"], f["duration"]) == (3, "a rat", 5.0)
    g = f["groups"][0]
    assert g["name"] == "You" and g["player"]["total"] == 50 and g["pet"]["total"] == 20
    assert g["player"]["dps"] == 10.0 and g["combined_total"] == 70 and f["totals"]["damage"] == 70
    assert f["rows"][0]["label"] == "You" and f["rows"][-1]["kind"] == "combined"

    sink.on_live(s)
    sink.on_closed(s, run)
    sink.on_finished(run)
    msgs = [roundtrip(m) for m in sink.drain()]
    assert [m["type"] for m in msgs] == ["closed", "finished"]   # the live update before closing was superseded
    assert msgs[0]["run"]["fight_count"] == 1 and msgs[0]["run"]["total_damage"] == 70
    assert msgs[1]["run"]["best"]["id"] == 3


def test_status_messages_and_test_result():
    sink = WebSink()
    sink.on_connection("following: you (port 1)")
    sink.on_pine("PINE: off")
    sink.on_error("source error: boom")
    sink.on_error(None)
    r = TestResult("dummy", 60.0, 10.0, True, [TestCharacter("Zecron", "k", 600, 3, 300, 1200, 4, 400, "Djinni",
                                                              1800, 30.0)])
    sink.on_test(r)
    msgs = [roundtrip(m) for m in sink.drain()]
    assert [(m["type"], m.get("text")) for m in msgs[:4]] == [
        ("connection", "following: you (port 1)"), ("pine", "PINE: off"), ("error", "source error: boom"),
        ("error", None)]
    t = msgs[4]["result"]
    assert (t["label"], t["total"], t["dps"], t["hits"], t["max_hit"], t["pets"]) == ("dummy", 1800, 30.0, 7, 400,
                                                                                        ["Djinni"])
    assert {row["kind"] for row in t["rows"]} == {"player", "pet", "combined"}
    assert result_payload(r) == t


def test_state_folds_messages_and_rebuilds_a_snapshot():
    old = make_fight(fid=1, target="old", you=10, pet=0)
    st = WebState("replay:x", [PastFight("r0", old)])
    assert st.fights[0]["prev"] and st.fights[0]["uid"] == "pr0-1"
    sink = WebSink()
    live = make_fight(fid=1, target="new", you=40, pet=5)
    sink.on_live(live)
    sink.on_connection("following: you")
    sink.on_closed(live, make_run([live]))
    sink.on_error("bad")
    out = []
    for m in sink.drain():
        out += st.apply(m)
    kinds = [m["type"] for m in out]
    assert "closed" in kinds and kinds.count("status") >= 1
    snap = roundtrip(st.snapshot({"theme": "dark"}))
    assert [f["uid"] for f in snap["fights"]] == ["n1", "pr0-1"]       # newest first, current vs previous run
    assert snap["status"]["connection"] == "following: you" and snap["status"]["error"] == "bad"
    assert snap["live"] is None and snap["last"]["target"] == "new" and snap["run"]["fight_count"] == 1
    assert snap["settings"] == {"theme": "dark"}


def test_html_escaping_is_left_to_the_page_but_json_is_ascii():
    s = make_fight(target='<b>"x" é</b>')
    text = to_json({"fight": fight_payload(s)})
    assert text.isascii() and "<b>" in text and json.loads(text)["fight"]["target"] == '<b>"x" é</b>'


# -- fixtures: expected totals in the payloads ---------------------------------------------------------------
def test_cap5_payload_totals(tmp_path):
    sink, rt = replay(CAP5, tmp_path, port=9032)
    msgs = [roundtrip(m) for m in sink.drain()]
    fights = [m["fight"] for m in msgs if m["type"] == "closed"]
    own = sum(g["player"]["total"] for f in fights for g in f["groups"])
    pet = sum(g["pet"]["total"] for f in fights for g in f["groups"])
    assert (own, pet) == (7973, 28566)
    assert msgs[-1]["type"] == "finished" and msgs[-1]["run"]["total_damage"] == 7973 + 28566


def test_cap2_bard_payload_totals(tmp_path):
    sink, rt = replay(CAP2, tmp_path, port=21173)
    msgs = [roundtrip(m) for m in sink.drain()]
    fight = next(m["fight"] for m in msgs if m["type"] == "closed")
    g = fight["groups"][0]
    assert g["combined_total"] == 20670 and fight["totals"]["damage"] == 20670
    assert g["taken_total"] == 1176 and fight["totals"]["taken"] == 1176
    assert g["heal_recv"] == 16800
    assert g["recv_sources"] and sum(v for _, v in g["recv_sources"]) == 16800
    assert any("Healing" in line for line in fight["lines"]) and any("Taken" in line for line in fight["lines"])
    assert run_payload(rt.run_summary())["total_damage"] == 20670


# -- layout file ----------------------------------------------------------------------------------------------
def test_layout_roundtrip_and_bad_files(tmp_path):
    path = tmp_path / "ui-state.json"
    assert load_layout(path) == {} and load_layout(None) == {}
    save_layout(path, {"theme": "dark", "compact": {"x": 5, "y": 6}})
    assert load_layout(path) == {"theme": "dark", "compact": {"x": 5, "y": 6}}
    path.write_text("not json", encoding="utf-8")
    assert load_layout(path) == {}
    path.write_text("[1]", encoding="utf-8")
    assert load_layout(path) == {}
    save_layout(tmp_path / "missing" / "x.json", {"a": 1})        # an unwritable location is not an error


def test_clamp_keeps_the_window_reachable():
    screen = (0, 0, 1920, 1080)
    assert clamp_to_screen(100, 100, 340, 300, screen) == (100, 100)
    assert clamp_to_screen(-3000, -50, 340, 300, screen) == (-280, 0)
    assert clamp_to_screen(5000, 5000, 340, 300, screen) == (1860, 1040)


def test_web_assets_and_licensed_fonts_are_bundled():
    for name in ("index.html", "style.css", "app.js", "fonts/Doto.ttf", "fonts/SpaceMono-Regular.ttf",
                 "fonts/SpaceMono-Bold.ttf", "fonts/OFL-Doto.txt", "fonts/OFL-SpaceMono.txt"):
        assert (WEB_DIR / name).is_file(), name
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8") + (WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html      # no CDN at runtime


# -- fallback -------------------------------------------------------------------------------------------------
def test_check_available_reports_missing_pywebview(monkeypatch):
    monkeypatch.delitem(sys.modules, "webview", raising=False)
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "webview" or name.startswith("webview."):
            raise ImportError("No module named 'webview'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    reason = web_app.check_available()
    assert reason and "pywebview" in reason
    with pytest.raises(WebUnavailable):
        web_app.WebApp()


def test_check_available_reports_missing_webview2(monkeypatch):
    pytest.importorskip("webview")
    monkeypatch.setattr(web_app, "webview2_present", lambda: False)
    if sys.platform == "win32":
        assert "WebView2" in web_app.check_available()


def test_run_gui_falls_back_to_tk_with_one_warning(monkeypatch, capsys, tmp_path):
    """pywebview missing: run_gui warns on stderr and builds the classic TkApp (stubbed here: no display needed)."""
    built = []

    class FakeTk:
        def __init__(self, **kw):
            built.append(kw)
            self.sink = WebSink()

        def mainloop(self):
            pass

    import eqoa_parser.ui.tk_app as tk_app

    monkeypatch.setattr(tk_app, "TkApp", FakeTk)

    def unavailable(cfg):
        raise WebUnavailable("pywebview is not available (ImportError: nope)")

    monkeypatch.setattr(app, "web_factory", lambda cfg: lambda **kw: unavailable(cfg))
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    cfg.runs_dir = str(tmp_path / "runs")
    src = PcapReplaySource(CAP5, cfg, speed=0)
    assert app.run_gui(cfg, src, "t") == 0
    err = capsys.readouterr().err
    assert len(built) == 1 and err.count("warning: web UI unavailable") == 1 and "classic window" in err


def test_classic_flag_skips_the_web_ui(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(app, "web_factory", lambda cfg: called.append(1))

    class FakeTk:
        def __init__(self, **kw):
            self.sink = WebSink()

        def mainloop(self):
            pass

    import eqoa_parser.ui.tk_app as tk_app

    monkeypatch.setattr(tk_app, "TkApp", FakeTk)
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    cfg.runs_dir = str(tmp_path / "runs")
    app.run_gui(cfg, PcapReplaySource(CAP5, cfg, speed=0), "t", classic=True)
    assert called == []
    assert app.parse_args(["--pcap", "x", "--classic"]).classic is True
    with pytest.raises(SystemExit):
        app.parse_args(["--pcap", "x", "--classic", "--no-gui"])


def test_coalesce_keeps_only_the_latest_live_and_status():
    """Freeze fix: a busy fight queues one 'live' message per hit; only the newest is worth drawing."""
    from eqoa_parser.ui.web_app import coalesce
    msgs = [{"type": "live", "fight": {"id": 1, "n": i}} for i in range(50)]
    msgs += [{"type": "status", "status": {"n": 1}}, {"type": "status", "status": {"n": 2}}]
    out = coalesce(msgs)
    assert out == [{"type": "live", "fight": {"id": 1, "n": 49}}, {"type": "status", "status": {"n": 2}}]


def test_coalesce_drops_live_superseded_by_closed_and_keeps_order():
    from eqoa_parser.ui.web_app import coalesce
    msgs = [{"type": "live", "fight": {"id": 1}}, {"type": "closed", "fight": {"id": 1}, "run": {}},
            {"type": "test", "result": {}}, {"type": "live", "fight": {"id": 2}}]
    assert [m["type"] for m in coalesce(msgs)] == ["closed", "test", "live"]
    assert coalesce(msgs)[-1]["fight"]["id"] == 2
    assert coalesce([]) == []
