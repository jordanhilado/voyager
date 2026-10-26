"""``voyager stats``: p50/p95 per stage across recent runs, and ladder rung usage."""
from __future__ import annotations

from pathlib import Path

from .runlog import iter_events


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def compute(runs_dir: Path, limit_runs: int | None = 20) -> dict:
    stages: dict[str, list[float]] = {"ground_ms": [], "act_ms": [], "settle_ms": [], "eyes_ms": [], "total_ms": []}
    eyes: list[float] = []
    rungs: dict[int, int] = {}
    spec_hits = spec_total = 0
    plans: dict[str, int] = {}
    n_runs = set()
    for run, ev in iter_events(runs_dir, limit_runs):
        n_runs.add(run)
        k = ev.get("kind")
        if k == "step_done":
            for s in stages:
                if s in ev:
                    stages[s].append(float(ev[s]))
            if ev.get("speculation_hit") is not None:
                spec_total += 1
                spec_hits += 1 if ev["speculation_hit"] else 0
        elif k == "eyes":
            eyes.append(float(ev.get("ms", 0)))
        elif k == "ground":
            r = int(ev.get("rung", 0))
            rungs[r] = rungs.get(r, 0) + 1
        elif k == "plan_result":
            plans[ev.get("status", "?")] = plans.get(ev.get("status", "?"), 0) + 1
    return {
        "runs": len(n_runs),
        "steps": len(stages["total_ms"]),
        "stages": {s: {"p50": round(pct(v, 50)), "p95": round(pct(v, 95)), "n": len(v)} for s, v in stages.items()},
        "eyes_calls": {"p50": round(pct(eyes, 50)), "p95": round(pct(eyes, 95)), "n": len(eyes)},
        "rungs": dict(sorted(rungs.items())),
        "speculation_hit_rate": round(spec_hits / spec_total, 2) if spec_total else None,
        "plan_results": plans,
    }


def render(stats: dict) -> str:
    lines = [f"runs: {stats['runs']}   steps: {stats['steps']}   plans: {stats['plan_results']}", "", f"{'stage':<12}{'p50 ms':>10}{'p95 ms':>10}{'n':>6}"]
    for s, v in stats["stages"].items():
        lines.append(f"{s:<12}{v['p50']:>10}{v['p95']:>10}{v['n']:>6}")
    e = stats["eyes_calls"]
    lines.append(f"{'eyes call':<12}{e['p50']:>10}{e['p95']:>10}{e['n']:>6}")
    lines.append("")
    lines.append("ladder rungs used: " + ", ".join(f"rung {k}: {v}" for k, v in stats["rungs"].items()) if stats["rungs"] else "ladder rungs used: none yet")
    if stats["speculation_hit_rate"] is not None:
        lines.append(f"speculative eyes-call hit rate: {stats['speculation_hit_rate']:.0%}")
    return "\n".join(lines)
