"""Per-run folder: ``events.jsonl`` with every observation hash, Eyes request and answer, action,
verification and per-stage timing, plus screenshots only when one was taken. Pruned after N days."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class RunLog:
    def __init__(self, runs_dir: Path, name: str = "run"):
        self.id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.dir = runs_dir / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self._f = self.path.open("a", encoding="utf-8")
        self.started = time.time()
        self.event("run_start", name=name)

    def event(self, kind: str, **data: Any) -> None:
        rec = {"t": round(time.time() - self.started, 3), "kind": kind, **data}
        self._f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def save_image(self, name: str, data: bytes, ext: str = "jpg") -> str:
        p = self.dir / f"{int((time.time() - self.started) * 1000):07d}-{name}.{ext}"
        p.write_bytes(data)
        return str(p)

    def close(self) -> None:
        self.event("run_end")
        self._f.close()


def prune(runs_dir: Path, days: int) -> int:
    if not runs_dir.exists():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for d in runs_dir.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def iter_events(runs_dir: Path, limit_runs: int | None = None):
    dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir()]) if runs_dir.exists() else []
    if limit_runs:
        dirs = dirs[-limit_runs:]
    for d in dirs:
        p = d / "events.jsonl"
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    yield d.name, json.loads(line)
                except json.JSONDecodeError:
                    continue


def latest_run(runs_dir: Path) -> Path | None:
    dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir()]) if runs_dir.exists() else []
    return dirs[-1] if dirs else None
