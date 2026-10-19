"""Run store: runs/<YYYY-MM-DD>.json. Core module: no UI imports.

OWNER: session C. See docs/CONTRACTS.md section "persistence".

One file per local day. Each app launch appends one run; earlier runs are kept byte-for-byte as parsed
JSON (even fights this code cannot interpret). The file is rewritten atomically (temp file + os.replace)
after every closed fight. A corrupt or unrecognised file is renamed aside, never overwritten.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .stats import FightSummary
from .testmode import TestResult

SCHEMA_VERSION = 1


@dataclass
class PastFight:
    """A fight loaded from an earlier run of the same day."""
    run_id: str
    summary: FightSummary


def day_file(runs_dir: str | Path, when: datetime) -> Path:
    return Path(runs_dir) / f"{when.strftime('%Y-%m-%d')}.json"


LOCK_TIMEOUT = 10.0   # seconds to wait for another launch's lock
LOCK_STALE = 30.0     # a lock older than this belongs to a crashed launch


def _replace(src: str, dst: Path) -> None:
    # On Windows os.replace fails with PermissionError while another process has the target open; retry briefly.
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON so a crash leaves either the old file or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _valid_doc(doc: Any) -> bool:
    return (
        isinstance(doc, dict)
        and doc.get("schema_version") == SCHEMA_VERSION
        and isinstance(doc.get("runs"), list)
        and all(isinstance(r, dict) and isinstance(r.get("fights", []), list) for r in doc["runs"])
    )


class RunStore:
    """Today's run file plus the current run.

    Construct once per launch. `previous_fights` are fights from earlier runs of the same day (oldest first).
    `add_fight()` appends to the current run and rewrites the file atomically.
    """

    def __init__(
        self,
        runs_dir: str | Path,
        source_label: str,
        *,
        now: datetime | None = None,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self.now = now or datetime.now().astimezone()
        self.path = day_file(runs_dir, self.now)
        self._warn = warn or (lambda m: print(m, file=sys.stderr))
        self.corrupt_path: Path | None = None
        with self._locked():
            self._doc = self._load()
            self.run: dict[str, Any] = {
                "run_id": self._unique_run_id(self.now.strftime("%Y%m%dT%H%M%S")),
                "started_at": self.now.isoformat(timespec="seconds"),
                "source": source_label,
                "fights": [],
            }
            self.previous_fights: list[PastFight] = self._read_previous()
            self.previous_tests: list[TestResult] = self._read_previous_tests()
            self._doc["runs"].append(self.run)  # in memory only: a launch with no fights leaves no trace
        self._saved = False

    @contextlib.contextmanager
    def _locked(self):
        """Cross-process lock: a lock file next to the day file, created exclusively."""
        lock = self.path.with_name(self.path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE:
                        lock.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise OSError(f"could not lock {lock}: another launch holds it")
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    # -- loading ---------------------------------------------------------------------------------
    def _fresh_doc(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "date": self.now.strftime("%Y-%m-%d"), "runs": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._fresh_doc()
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not _valid_doc(doc):
                raise ValueError("unrecognised structure or schema_version")
            return doc
        except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            return self._quarantine(exc)

    def _quarantine(self, exc: Exception) -> dict[str, Any]:
        stamp = self.now.strftime("%Y%m%dT%H%M%S")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        n = 1
        while target.exists():
            n += 1
            target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{n}")
        try:
            _replace(str(self.path), target)
            self.corrupt_path = target
            self._warn(f"warning: {self.path} unreadable ({exc}); moved to {target}, starting a fresh file")
        except OSError as rename_exc:
            # Cannot move it aside: write to a sibling file instead so the original is never touched.
            alt = self.path.with_name(f"{self.path.stem}.{stamp}.json")
            self.path = alt
            self._warn(
                f"warning: {target.parent / self.path.name} unreadable ({exc}) and could not be renamed "
                f"({rename_exc}); this run will be written to {alt}"
            )
        return self._fresh_doc()

    def _unique_run_id(self, base: str, doc: dict[str, Any] | None = None) -> str:
        taken = {r.get("run_id") for r in (doc or self._doc)["runs"]}
        rid, n = base, 1
        while rid in taken:
            n += 1
            rid = f"{base}-{n}"
        return rid

    def _read_previous(self) -> list[PastFight]:
        out: list[PastFight] = []
        for run in self._doc["runs"]:
            for raw in run.get("fights", []):
                try:
                    out.append(PastFight(str(run.get("run_id", "?")), FightSummary.from_dict(raw)))
                except (TypeError, KeyError, ValueError, AttributeError):
                    self._warn(f"warning: skipped an unreadable fight in run {run.get('run_id')} (kept in file)")
        return out

    def _read_previous_tests(self) -> list[TestResult]:
        out: list[TestResult] = []
        for run in self._doc["runs"]:
            tests = run.get("tests", [])
            for raw in tests if isinstance(tests, list) else []:
                try:
                    out.append(TestResult.from_dict(raw))
                except (TypeError, KeyError, ValueError, AttributeError):
                    self._warn(f"warning: skipped an unreadable test in run {run.get('run_id')} (kept in file)")
        return out

    # -- writing ---------------------------------------------------------------------------------
    @property
    def run_id(self) -> str:
        return self.run["run_id"]

    def add_fight(self, summary: FightSummary) -> None:
        self.run["fights"].append(summary.to_dict())
        self.save()

    def add_test(self, result: TestResult) -> None:
        """Append a finished timed test to the current run's additive "tests" list."""
        self.run.setdefault("tests", []).append(result.to_dict())
        self.save()

    def save(self) -> None:
        """Re-read the file under the lock, replace only this run's entry (by run_id), write atomically."""
        with self._locked():
            doc = self._load() if self.path.exists() else self._fresh_doc()
            runs = doc["runs"]
            if not self._saved:
                # First save: another launch in the same second may have taken our run_id meanwhile.
                self.run["run_id"] = self._unique_run_id(self.run["run_id"], doc)
            for i, r in enumerate(runs):
                if r.get("run_id") == self.run["run_id"]:
                    runs[i] = self.run
                    break
            else:
                runs.append(self.run)
            self._doc = doc
            atomic_write_json(self.path, doc)
            self._saved = True
