from voyager.calibrate import propose
from voyager.questions import Thresholds
from voyager.runlog import RunLog
from voyager.stats import compute, pct


def test_stats_from_runlog(tmp_path):
    rl = RunLog(tmp_path / "runs", "t")
    for i in range(5):
        rl.event("ground", step=i, rung=1 if i < 4 else 3, confidence=0.9, ms=10)
        rl.event("step_done", step=i, do="click", ground_ms=10, act_ms=90, settle_ms=300, eyes_ms=250, total_ms=650, rung=1, speculation_hit=True, verified=True)
        rl.event("eyes", provider="fake", ms=250)
    rl.event("plan_result", status="done", step=5, reason="", ms=3000)
    rl.close()
    st = compute(tmp_path / "runs")
    assert st["steps"] == 5 and st["stages"]["total_ms"]["p50"] == 650 and st["rungs"] == {1: 4, 3: 1}
    assert st["speculation_hit_rate"] == 1.0 and st["plan_results"] == {"done": 1}
    assert pct([1, 2, 3, 4, 100], 95) == 100


def test_calibrate_proposes_threshold():
    samples = [(0.95, 1, True)] * 30 + [(0.85, 1, True)] * 15 + [(0.85, 1, False)] * 3 + [(0.7, 1, False)] * 5
    t, report = propose(samples, Thresholds())
    assert t.T_PICK == 0.9  # 0.85 bucket has >1% wrong, 0.9 bucket clean
    t2, report2 = propose(samples[:5], Thresholds())
    assert "unchanged" in report2["note"]
