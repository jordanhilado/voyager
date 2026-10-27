"""``voyager calibrate``: treat each step's verification outcome as ground truth for the pick that
preceded it, bucket picks by confidence, and propose thresholds that keep the wrong-element rate
under 1%. Writes ``<data_dir>/thresholds.yaml``, which questions.py loads if present."""
from __future__ import annotations

from pathlib import Path

import yaml

from .questions import Thresholds
from .runlog import iter_events

BUCKETS = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]


def collect(runs_dir: Path, limit_runs: int | None = None) -> list[tuple[float, int, bool]]:
    """(pick confidence, rung, verified) per completed step."""
    samples: list[tuple[float, int, bool]] = []
    pending: dict[tuple[str, int], tuple[float, int]] = {}
    for run, ev in iter_events(runs_dir, limit_runs):
        k = ev.get("kind")
        if k == "ground":
            pending[(run, int(ev["step"]))] = (float(ev.get("confidence", 0)), int(ev.get("rung", 0)))
        elif k == "step_done":
            key = (run, int(ev["step"]))
            if key in pending:
                conf, rung = pending.pop(key)
                samples.append((conf, rung, bool(ev.get("verified", True))))
        elif k == "plan_result" and ev.get("status") == "failed" and "verification failed" in str(ev.get("reason", "")):
            key = (run, int(ev.get("step", -1)))
            if key in pending:
                conf, rung = pending.pop(key)
                samples.append((conf, rung, False))
    return samples


def propose(samples: list[tuple[float, int, bool]], current: Thresholds, max_wrong_rate: float = 0.01) -> tuple[Thresholds, dict]:
    report: dict = {"samples": len(samples), "buckets": {}}
    t = Thresholds(**current.dump())
    if len(samples) < 20:
        report["note"] = "fewer than 20 samples; thresholds unchanged"
        return t, report

    def best_threshold(sub: list[tuple[float, bool]]) -> float | None:
        for b in BUCKETS:
            above = [ok for c, ok in sub if c >= b]
            if len(above) >= 10:
                wrong = 1 - sum(above) / len(above)
                report["buckets"][str(b)] = {"n": len(above), "wrong_rate": round(wrong, 3)}
                if wrong <= max_wrong_rate:
                    return b
        return None

    text_picks = [(c, ok) for c, r, ok in samples if r in (1, 2)]
    box_picks = [(c, ok) for c, r, ok in samples if r in (3, 4)]
    if (b := best_threshold(text_picks)) is not None:
        t.T_PICK = b
    if (b := best_threshold(box_picks)) is not None:
        t.T_BOX = b
    return t, report


def run(runs_dir: Path, out_path: Path, current: Thresholds) -> tuple[Thresholds, dict]:
    samples = collect(runs_dir)
    t, report = propose(samples, current)
    out_path.write_text(yaml.safe_dump(t.dump(), sort_keys=False), encoding="utf-8")
    report["written"] = str(out_path)
    return t, report
