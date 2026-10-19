"""Configuration contract. Core module: no UI imports.

OWNER: main chat. Session A may add fields to PineConfig and BufferConfig only.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml



def find_project_root(frozen: bool | None = None, executable: str | None = None) -> Path:
    """Folder that config.yaml / runs / unmatched.log live in. Frozen (PyInstaller): the folder of the .exe, NEVER the
    temporary extraction folder. Otherwise the source checkout's root."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return Path(executable or sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = find_project_root()


@dataclass
class PineConfig:
    enabled: bool = False           # Phase 5: read group members and names from game memory (read-only)
    slot: int = 28011               # PINE slot; TCP port on Windows, socket-name suffix on Linux
    host: str = "127.0.0.1"         # TCP host (Windows transport)
    unix_socket: str | None = None  # explicit UNIX socket path override (Linux); None = derive per PINE spec
    timeout: float = 2.0            # socket timeout, seconds


@dataclass
class BufferConfig:
    """Location/format of the chat/combat text buffer in EE RAM. ALL UNKNOWN until Phase 2."""
    address: int | None = None      # EE RAM address of the text buffer; None = not located yet
    size: int = 4096                # bytes to read per poll
    encoding: str = "ascii"         # UNKNOWN: ascii | utf16le | shiftjis
    poll_interval: float = 0.1      # seconds between reads
    layout: str = "scroll"          # ASSUMED layout strategy in pine_source: "scroll" | "ring" (see pine_source.py)
    emit_initial: bool = False      # False: text already in the buffer at start is history, not yielded
    stable_reads: bool = True       # re-read until two consecutive reads match (guards torn reads); UNVERIFIED


@dataclass
class NetConfig:
    """Packet capture of the game's UDP traffic (Phase 2). See docs/protocol-notes.md."""
    server_ip: str = "5.161.217.188"       # Sandstorm game server
    local_port: int | None = None           # follow this emulator connection; None = auto (first with combat)
    interface_ip: str | None = None         # live capture: local IPv4 to bind; None = auto-detect
    poll_interval: float = 0.25             # live: max seconds between heartbeats when no packets arrive


@dataclass
class UIConfig:
    always_on_top: bool = True
    refresh_ms: int = 250


@dataclass
class Config:
    idle_timeout: float = 8.0               # seconds with no damage dealt/received that ends a fight
    show_group: bool = False                # MVP shows You and Pet only; True reveals other actors
    pet_names: list[str] = field(default_factory=list)  # actor/target names that mean "pet"
    player_name: str | None = None          # actor/target name that means "you"
    patterns_file: str = "patterns.yaml"
    unmatched_log: str = "unmatched.log"
    runs_dir: str = "runs"
    pine: PineConfig = field(default_factory=PineConfig)
    buffer: BufferConfig = field(default_factory=BufferConfig)
    net: NetConfig = field(default_factory=NetConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    # Directory relative paths resolve against: the config file's directory, else the project root.
    base_dir: Path = field(default=PROJECT_ROOT, compare=False, repr=False)

    def resolve(self, value: str | Path) -> Path:
        p = Path(value)
        if p.is_absolute():
            return p
        target = self.base_dir / p
        # Frozen exe: a default file that is only bundled (e.g. patterns.yaml) is read from the bundle. Files that are
        # never bundled (runs/, unmatched.log) always resolve next to the exe, never into the temp folder.
        bundle = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
        if bundle is not None and not target.exists() and (Path(bundle) / p).exists():
            return Path(bundle) / p
        return target


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"config section {where or '<root>'} must be a mapping")
    known = {f.name: f for f in dataclasses.fields(cls) if f.name != "base_dir"}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {where}{key}")
        ftype = known[key].type
        sub = {"PineConfig": PineConfig, "BufferConfig": BufferConfig, "NetConfig": NetConfig, "UIConfig": UIConfig}.get(
            ftype if isinstance(ftype, str) else getattr(ftype, "__name__", "")
        )
        if sub is not None:
            value = _build(sub, value or {}, f"{where}{key}.")
        elif key == "address" and isinstance(value, str):
            try:
                value = int(value, 0)  # accept "0x01234567"
            except ValueError:
                raise ValueError(f"config {where}{key}: not an integer: {value!r}") from None
        kwargs[key] = value
    return cls(**kwargs)


_TYPES = {"int": (int,), "float": (int, float), "bool": (bool,), "str": (str,)}
_CHOICES = {"encoding": ("ascii", "utf16le", "shiftjis")}
_POSITIVE = {"idle_timeout", "timeout", "size", "poll_interval", "refresh_ms"}


def _check_types(obj: Any, where: str) -> None:
    for f in dataclasses.fields(obj):
        if f.name == "base_dir":
            continue
        value, name = getattr(obj, f.name), f"{where}{f.name}"
        if dataclasses.is_dataclass(value):
            _check_types(value, f"{name}.")
            continue
        ftype = str(f.type)
        if value is None:
            if "None" not in ftype:
                raise ValueError(f"config {name}: must not be null")
            continue
        if ftype.startswith("list["):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"config {name}: must be a list of strings, got {value!r}")
            continue
        base = ftype.split("|")[0].strip()
        ok = _TYPES.get(base)
        if ok is None:
            continue
        if isinstance(value, bool) and base != "bool" or not isinstance(value, ok):
            raise ValueError(f"config {name}: must be {base}, got {value!r}")
        if base in ("int", "float"):
            if not math.isfinite(value):
                raise ValueError(f"config {name}: must be finite, got {value!r}")
            if f.name in _POSITIVE and value <= 0:
                raise ValueError(f"config {name}: must be > 0, got {value!r}")
        if f.name in _CHOICES and value not in _CHOICES[f.name]:
            raise ValueError(f"config {name}: must be one of {_CHOICES[f.name]}, got {value!r}")


def validate_config(cfg: Config) -> Config:
    """Raise ValueError on wrong types, non-finite numbers or out-of-range values."""
    _check_types(cfg, "")
    if not 0 < cfg.pine.slot < 65536:
        raise ValueError(f"config pine.slot: must be 1-65535, got {cfg.pine.slot!r}")
    if cfg.buffer.address is not None and cfg.buffer.address < 0:
        raise ValueError(f"config buffer.address: must be >= 0, got {cfg.buffer.address!r}")
    if cfg.net.local_port is not None and not 0 < cfg.net.local_port < 65536:
        raise ValueError(f"config net.local_port: must be 1-65535, got {cfg.net.local_port!r}")
    for key in ("server_ip", "interface_ip"):
        value = getattr(cfg.net, key)
        if value is None and key == "interface_ip":
            continue
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            raise ValueError(f"config net.{key}: must be an IPv4 address, got {value!r}") from None
    return cfg


def load_config(path: str | Path | None = None, **overrides: Any) -> Config:
    """Load config from YAML (missing keys take defaults). Unknown keys, invalid YAML and invalid values
    (wrong type, non-finite, out of range; see validate_config) raise ValueError.

    `overrides` are top-level fields applied after loading (e.g. from CLI flags); None values are ignored.
    """
    data: dict[str, Any] = {}
    base = PROJECT_ROOT
    if path is not None:
        path = Path(path)
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: invalid YAML: {exc}") from exc
        base = path.resolve().parent
    cfg = _build(Config, data, "")
    cfg.base_dir = base
    for key, value in overrides.items():
        if value is None:
            continue
        if not hasattr(cfg, key):
            raise ValueError(f"unknown config override: {key}")
        setattr(cfg, key, value)
    return validate_config(cfg)
