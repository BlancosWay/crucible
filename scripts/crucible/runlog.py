"""Append-only provenance run-log: run directory, events, DAG, full-text artifacts."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crucible.config import Config
from crucible.integrity import RUN_SCHEMA_VERSION


def _fsync_dir(dirpath: Path) -> None:
    """Best-effort durable fsync of a directory entry (e.g. after ``os.replace`` or first
    creating a file), so the rename/creation survives a crash on POSIX. Directory fsync is
    not supported everywhere (e.g. Windows), so any failure is ignored."""
    try:
        fd = os.open(dirpath, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform/filesystem without directory fsync
        pass
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and durably (S1): write a sibling temp file,
    fsync it, ``os.replace`` it into place (an atomic rename on POSIX and Windows), then fsync
    the parent directory so the rename itself is durable. A crash mid-write can never leave a
    half-written ``dag.json``/``config.json`` — readers see either the old or the new file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


class RunLogCorruptError(Exception):
    """Raised when runlog.jsonl contains a corrupt record that is not just a torn final
    write (e.g. an interior malformed line, a complete but invalid record, or a non-object
    line) — real corruption the human must see, never silently dropped."""


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


class RunLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @property
    def _events_file(self) -> Path:
        return self.path / "runlog.jsonl"

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        first_create = not self._events_file.exists()
        with self._events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # S1: durably persist each appended record
        if first_create:  # persist the new file's directory entry too
            _fsync_dir(self.path)
        return record

    def read_events(self) -> list[dict[str, Any]]:
        if not self._events_file.exists():
            return []
        data = self._events_file.read_bytes()
        if not data:
            return []
        ends_clean = data.endswith(b"\n")
        lines = data.splitlines()
        last = len(lines) - 1
        out: list[dict[str, Any]] = []
        for i, raw in enumerate(lines):
            if not raw.strip():
                continue
            torn_eligible = False
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                # Garbled/incomplete bytes plausibly indicate an interrupted (torn) write.
                torn_eligible = True
                problem = f"invalid JSON ({e})"
            else:
                if isinstance(event, dict) and "event" in event:
                    out.append(event)
                    continue
                # Parsed as complete JSON but the wrong shape: the write finished, so this is
                # real corruption, not a torn tail — surface it regardless of newline.
                problem = 'record is not a JSON object with an "event" field'
            # Tolerate ONLY an unparseable final record with no trailing newline (a torn append).
            if torn_eligible and i == last and not ends_clean:
                print(f"crucible: ignoring a partial trailing record in {self._events_file} "
                      f"(interrupted write)", file=sys.stderr)
                break
            raise RunLogCorruptError(f"{self._events_file}: line {i + 1} is corrupt — {problem}")
        return out

    def save_dag(self, dag_data: dict[str, Any]) -> None:
        _atomic_write_text(self.path / "dag.json",
                           json.dumps(dag_data, indent=2, ensure_ascii=False))

    def load_dag(self) -> dict[str, Any]:
        return json.loads((self.path / "dag.json").read_text(encoding="utf-8"))


def init_run(goal: str, cfg: Config, base_dir: str | Path = "runs") -> RunLog:
    base = Path(base_dir)
    slug = slugify(goal)[:40].strip("-") or "run"
    run_dir = None
    for attempt in range(1000):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-%f")
        suffix = "" if attempt == 0 else f"-{attempt}"
        candidate = base / f"{stamp}-{slug}{suffix}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            run_dir = candidate
            break
        except FileExistsError:
            continue
    if run_dir is None:  # pragma: no cover - 1000 same-microsecond collisions is implausible
        raise RuntimeError("could not allocate a unique run directory")
    _atomic_write_text(run_dir / "config.json",
                       json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))
    run = RunLog(run_dir)
    run.append("run_start", schema_version=RUN_SCHEMA_VERSION, goal=goal, config=cfg.to_dict())
    return run
