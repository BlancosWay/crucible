"""Append-only provenance run-log: run directory, events, DAG, full-text artifacts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crucible.config import Config


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
        with self._events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def read_events(self) -> list[dict[str, Any]]:
        if not self._events_file.exists():
            return []
        out = []
        for line in self._events_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def save_dag(self, dag_data: dict[str, Any]) -> None:
        (self.path / "dag.json").write_text(json.dumps(dag_data, indent=2, ensure_ascii=False))

    def load_dag(self) -> dict[str, Any]:
        return json.loads((self.path / "dag.json").read_text(encoding="utf-8"))


def init_run(goal: str, cfg: Config, base_dir: str | Path = "runs") -> RunLog:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    run_dir = Path(base_dir) / f"{stamp}-{slugify(goal)[:40] or 'run'}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(json.dumps(cfg.to_dict(), indent=2))
    run = RunLog(run_dir)
    run.append("run_start", goal=goal, config=cfg.to_dict())
    return run
