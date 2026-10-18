"""Application wiring: CLI -> config -> source -> Engine -> persistence -> UI. OWNER: session C."""
from __future__ import annotations

import argparse
import math
import os
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

import yaml

from . import pipeline
from .config import PROJECT_ROOT, Config, load_config
from .parser import load_patterns
from .persistence import RunStore
from .stats import FightSummary, RunSummary, summarize_run

EXIT_USAGE = 2
BUFFER_DOC = "docs/finding-the-buffer.md"
CAPTURE_DOC = "docs/live-capture.md"


def _finite_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be a finite number, got {text!r}")
    return value


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {text!r}") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1-65535, got {value}")
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m eqoa_parser",
        description="EQOA combat parser / DPS meter. Reads damage from the game server's UDP packets "
                    "(live capture or a .pcapng file); EQOA has no combat text log.",
    )
    p.add_argument("--config", metavar="PATH", help="config YAML (default: config.yaml in the project root)")
    src = p.add_argument_group("source (exactly one)")
    src.add_argument("--pcap", metavar="FILE", help="replay a packet capture (.pcapng) of the game traffic")
    src.add_argument("--live", action="store_true",
                     help="capture the game's UDP packets live (needs Administrator; see docs/live-capture.md)")
    src.add_argument("--replay", metavar="FILE", help="replay a synthetic text log (<ts>\\t<line> per line)")
    src.add_argument("--live-pine", action="store_true",
                     help="LEGACY/EXPERIMENTAL: read a text buffer via PINE (needs buffer.address; EQOA has no "
                          "combat text, so this is unlikely to be useful)")
    p.add_argument("--connection", type=_port, metavar="PORT",
                   help="limit the meter to the one emulator whose client UDP port is PORT (config net.local_port); "
                        "default: follow every emulator connected to the game server")
    p.add_argument("--speed", type=_finite_float, metavar="X",
                   help="replay speed multiplier; 0 = as fast as possible (default: 1.0 GUI, 0 with --no-gui)")
    p.add_argument("--no-gui", action="store_true", help="print to stdout instead of opening a window")
    p.add_argument("--show-group", action="store_true", help="also show other actors, not just You and Pet")
    p.add_argument("--idle-timeout", type=_finite_float, metavar="N", help="seconds without damage that end a fight")
    p.add_argument("--runs-dir", metavar="DIR", help="where runs/<date>.json is stored (default: config runs_dir)")
    p.add_argument("--always-on-top", action=argparse.BooleanOptionalAction, default=None,
                   help="keep the window above others (default: config ui.always_on_top)")
    return p


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and validate; invalid combinations exit with status 2 via parser.error()."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    chosen = [bool(args.replay), bool(args.pcap), bool(args.live), bool(args.live_pine)]
    if sum(chosen) != 1:
        parser.error("exactly one of --pcap FILE, --live, --replay FILE or --live-pine is required")
    if args.speed is not None:
        if args.speed < 0:
            parser.error("--speed must be >= 0")
        if args.live or args.live_pine:
            parser.error("--speed only applies to --pcap and --replay")
    if args.idle_timeout is not None and args.idle_timeout <= 0:
        parser.error("--idle-timeout must be > 0")
    return args


def make_config(args: argparse.Namespace) -> Config:
    path = args.config
    if path is None and (PROJECT_ROOT / "config.yaml").exists():
        path = PROJECT_ROOT / "config.yaml"
    cfg = load_config(
        path,
        idle_timeout=args.idle_timeout,
        show_group=True if args.show_group else None,
    )
    if args.runs_dir is not None:
        cfg.runs_dir = str(Path(args.runs_dir).resolve())
    else:
        cfg.runs_dir = str(cfg.resolve(cfg.runs_dir))
    if args.always_on_top is not None:
        cfg.ui.always_on_top = args.always_on_top
    if args.connection is not None:
        cfg.net.local_port = args.connection
    return cfg


class StartupError(Exception):
    """A user-input or startup failure: reported as `error: ...`, exit status 2."""


def make_source(args: argparse.Namespace, cfg: Config):
    """Returns (source, label). Raises StartupError for unusable setups."""
    speed = args.speed if args.speed is not None else (0.0 if args.no_gui else 1.0)
    if args.pcap:
        from .net.sources import PcapReplaySource

        if not Path(args.pcap).is_file():
            raise StartupError(f"capture file not found: {args.pcap}")
        check_pcapng_magic(args.pcap)
        try:
            source = PcapReplaySource(args.pcap, cfg, speed=speed)
        except ValueError as exc:  # B's constructor validates the section header
            raise StartupError(f"{args.pcap}: {exc}") from exc
        return source, f"pcap:{args.pcap}"
    if args.live:
        from .net.capture import CaptureError, RawCapture
        from .net.sources import LivePacketSource

        try:  # open and close the capture now so "not Administrator" fails before any banner or window
            with RawCapture(cfg.net):
                pass
        except CaptureError as exc:
            raise StartupError(_with_doc(exc)) from exc
        return LivePacketSource(cfg), "live:capture"
    if args.live_pine:
        if cfg.buffer.address is None:
            raise StartupError(
                "--live-pine needs buffer.address in the config, and it is not set. The EE RAM address of the "
                f"chat/combat buffer is unknown until you locate it; see {BUFFER_DOC}."
            )
        from .pine import PineError
        from .sources import PineBufferSource

        source = PineBufferSource(cfg)
        try:  # fail now, before any banner, if PCSX2/PINE is unreachable (read-only probe; layout state untouched)
            source.client.read_bytes(cfg.buffer.address, 4)
        except PineError as exc:
            source.close()
            raise StartupError(f"cannot read the emulator over PINE: {exc}") from exc
        return source, "live:pine"
    from .sources import ReplaySource

    if not Path(args.replay).is_file():
        raise StartupError(f"replay file not found: {args.replay}")
    return ReplaySource(args.replay, speed=speed), f"replay:{args.replay}"


def check_pcapng_magic(path: str | Path) -> None:
    """Reject files that are plainly not pcapng (the section header block type is 0x0A0D0D0A in either byte order).
    An empty file passes: it is reported as 'no game-server traffic' after the run instead."""
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head and head != b"\x0a\x0d\x0d\x0a":
        raise StartupError(f"{path} is not a pcapng file (wrong file? classic .pcap is not supported)")


def _with_doc(exc: Exception) -> str:
    text = str(exc)
    return text if CAPTURE_DOC in text else f"{text} (see {CAPTURE_DOC})"


def own_actors_provider(source: Any):
    """source.own_actors if the packet source has it, else the router's; None for text sources."""
    fn = getattr(source, "own_actors", None) or getattr(getattr(source, "router", None), "own_actors", None)
    return fn if callable(fn) else None


def connection_text(source: Any) -> str | None:
    """Which characters are followed, e.g. 'following: you (0x174A1A40, port 21173), Zecron (port 20605)'.

    Uses router.connections() (ports that produced an Event, primary first) and each Attributor's player_label()
    and player_id; falls back to router.selected for older routers."""
    router = getattr(source, "router", None)
    if router is None:
        return None
    conns = getattr(router, "connections", None)
    ports = list(conns()) if callable(conns) else ([router.selected] if router.selected is not None else [])
    groups = getattr(router, "character_groups", None)
    shown = {g.port: g.name for g in groups()} if callable(groups) else {}  # display names (Phase 4)
    parts = []
    for port in ports:
        attr = router.attributor(port)
        label_fn = getattr(attr, "player_label", None)
        label = shown.get(port) or (label_fn() if callable(label_fn) else "you")
        pid = getattr(attr, "player_id", None)
        parts.append(f"{label} ({'0x%08X, ' % pid if pid is not None else ''}port {port})")
    return "following: " + ", ".join(parts) if parts else None


class ErrorWatch:
    """Wraps a live source and reports changes of its last_error (PineBufferSource) to the sink."""

    def __init__(self, inner: Any, sink: Any) -> None:
        self.inner, self.sink = inner, sink
        self._shown: str | None = None
        self._conn: str | None = None
        self.items = 0  # non-heartbeat items seen (Events)

    def __iter__(self):
        for item in self.inner:
            if item[1] != "":
                self.items += 1
            conn = connection_text(self.inner)
            if conn != self._conn and conn is not None:
                self._conn = conn
                self.sink.on_connection(conn)
            err = getattr(self.inner, "last_error", None)
            text = f"source error: {err}" if err is not None else None
            if text != self._shown:
                self._shown = text
                self.sink.on_error(text)
            yield item

    def close(self) -> None:
        self.inner.close()

    def __getattr__(self, name: str):
        # the Engine looks own_actors up on the source it is given; expose it only when the inner source has it
        if name == "own_actors":
            fn = own_actors_provider(self.inner)
            if fn is not None:
                return fn
        if name == "character_groups":
            fn = getattr(self.inner, "character_groups", None)
            if callable(fn):
                return fn
        raise AttributeError(name)


class Runtime:
    """Engine + persistence + a sink with on_live(s), on_closed(s, run), on_finished(run)."""

    def __init__(self, cfg: Config, source: Any, store: RunStore, sink: Any) -> None:
        self.cfg, self.source, self.store, self.sink = cfg, source, store, sink
        self.closed: list[FightSummary] = []
        self.watch = ErrorWatch(source, sink) if (hasattr(source, "last_error") or hasattr(source, "router")) else None
        watched = self.watch or source
        provider = own_actors_provider(source)
        if provider is not None and hasattr(sink, "own_provider"):
            sink.own_provider = provider
        self.engine = pipeline.Engine(
            cfg, watched, on_live=sink.on_live, on_closed=self._on_closed, on_finished=self._on_finished
        )

    def run_summary(self) -> RunSummary:
        return summarize_run(self.closed)

    def _on_closed(self, summary: FightSummary) -> None:
        self.closed.append(summary)
        try:
            self.store.add_fight(summary)
        except OSError as exc:  # a full disk must not kill the meter
            print(f"warning: could not save fight {summary.fight_id}: {exc}", file=sys.stderr)
        self.sink.on_closed(summary, self.run_summary())

    def _on_finished(self) -> None:
        path = getattr(self.source, "path", None)
        if self.watch is not None and path is not None and hasattr(self.source, "router") and self.watch.items == 0:
            # empty/truncated file, or no game-server traffic: say so instead of a silent "No fights."
            self.sink.on_error(f"no game-server combat found in {path} (empty, truncated, or no traffic from "
                               f"{self.cfg.net.server_ip})")
        self.sink.on_finished(self.run_summary())


def run_console(cfg: Config, source: Any, label: str) -> int:
    from .ui.console import ConsoleClosed, ConsoleUI

    store = RunStore(cfg.runs_dir, label)
    ui = ConsoleUI(cfg.show_group)
    rt = Runtime(cfg, source, store, ui)
    try:
        ui.banner(f"[{label}] run {store.run_id}; {len(store.previous_fights)} fight(s) from earlier runs today; "
                  f"saving to {store.path}")
        rt.engine.run()
    except KeyboardInterrupt:
        source.close()
        rt.engine.finish()
    except ConsoleClosed:
        # stdout went away (e.g. piped into head): exit quietly and keep Python from complaining at shutdown
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        source.close()
    return 0


def run_gui(cfg: Config, source: Any, label: str, app_factory=None) -> int:
    from .ui.tk_app import TkApp

    store = RunStore(cfg.runs_dir, label)
    stop = threading.Event()
    holder: dict[str, Any] = {}
    thread_ref: list[threading.Thread] = []

    def shutdown() -> None:
        stop.set()
        try:
            source.close()
        except Exception:
            pass
        for t in thread_ref:
            t.join(timeout=3.0)

    factory = app_factory or TkApp
    window = factory(
        show_group=cfg.show_group,
        always_on_top=cfg.ui.always_on_top,
        refresh_ms=cfg.ui.refresh_ms,
        previous_fights=store.previous_fights,
        source_label=f"{label}  run {store.run_id}",
        on_close=shutdown,
    )
    rt = Runtime(cfg, source, store, window.sink)
    provider = own_actors_provider(source)
    if provider is not None:
        window.own_provider = provider
    holder["rt"] = rt

    def work() -> None:
        try:
            rt.engine.run(stop)
        except Exception as exc:  # surfaced in the window, not lost on a dead thread
            window.sink.on_error(f"{type(exc).__name__}: {exc}")

    t = threading.Thread(target=work, name="eqoa-engine", daemon=True)
    thread_ref.append(t)
    t.start()
    window.mainloop()
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from .net.capture import CaptureError
    from .pine import PineError

    try:
        cfg = make_config(args)
        load_patterns(cfg.resolve(cfg.patterns_file))  # validate before any banner or run file
        source, label = make_source(args, cfg)
    except StartupError as exc:
        return _fail(str(exc))
    except yaml.YAMLError as exc:
        return _fail(f"invalid YAML: {exc}")
    except (OSError, ValueError, RuntimeError) as exc:
        return _fail(str(exc))
    try:
        if args.no_gui:
            return run_console(cfg, source, label)
        return run_gui(cfg, source, label)
    except PineError as exc:
        return _fail(f"emulator connection failed: {exc}")
    except CaptureError as exc:
        return _fail(_with_doc(exc))
    except OSError as exc:
        return _fail(str(exc))
