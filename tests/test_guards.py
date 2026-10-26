"""Batch guards: picks must show what the target names, destructive clicks are double-checked, a failed
check is retried only when the action changed nothing, and every step leaves a receipt."""
from tests.fakes import FakeBackend
from voyager.ax import candidate_criteria, candidates, parse_ax
from voyager.brief import Brief
from voyager.config import Settings
from voyager.eyes.base import FakeEyes
from voyager.pipeline import Pipeline
from voyager.plan import Plan
from voyager.policy import KillSwitch, Policy
from voyager.questions import Thresholds
from voyager.runlog import RunLog

PAGE = """Window: "Supplies", App: Google Chrome.
0 standard window Supplies
\t1 HTML content Supplies, URL: localhost/t/reorder
\t\t2 container Catalog
\t\t\t3 table
\t\t\t\t4 row
\t\t\t\t\t5 cell USB-C cable 1 m
\t\t\t\t\t6 cell CB-101
\t\t\t\t\t7 cell
\t\t\t\t\t\t8 button Add
\t\t\t\t9 row
\t\t\t\t\t10 cell USB-C cable 1 m, braided
\t\t\t\t\t11 cell CB-102
\t\t\t\t\t12 cell
\t\t\t\t\t\t13 button Add
\t\t14 container Draft order
\t\t\t15 table
\t\t\t\t16 row
\t\t\t\t\t17 cell USB-C cable 1 m (CB-101)
\t\t\t\t\t18 cell
\t\t\t\t\t\t19 button Remove
\t\t\t\t20 row
\t\t\t\t\t21 cell DisplayPort 1.4 cable 2 m (DP-142)
\t\t\t\t\t22 cell
\t\t\t\t\t\t23 button Remove
"""
POPUP = PAGE + """\t\t24 dialog Shipping notice
\t\t\t25 text Cables 2 m and longer ship separately.
\t\t\t26 button Got it
"""
ADDED = PAGE.replace("\t\t14 container Draft order\n", "\t\t14 container Draft order\n\t\t\t27 text Added USB-C cable 1 m, braided\n")


class RowBackend(FakeBackend):
    """Records which element index each click hit (rows share button names)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.clicked: list[int] = []

    async def click(self, index=None, xy=None, *, count=1, button="left", mouse=False):
        el = self._obs().tree.get(index) if index is not None else None
        self.clicked.append(index)
        return self._apply(f"click:{el.name}@{index}" if el else f"click_xy:{xy}")


def responder(picks: dict[str, tuple[str, float]], verify: float = 0.95):
    """picks: question key ('pick_1' = the fused lookahead, 'pick' = a fresh or shuffled pick) ->
    (text the chosen option's description must contain, confidence)."""
    def answer(state, qs, images):
        out = {}
        for k, q in qs.items():
            if q["type"] == "choice":
                choice, conf = "none", 0.95
                rule = picks.get(k) or (picks.get("pick_1") if k.startswith("pick_") else None)
                if rule:
                    needle, conf = rule
                    choice = next((lab for lab, desc in q["criteria"].items() if desc.startswith("button") and needle in desc), "none")
                out[k] = {"type": "choice", "choice": choice, "confidence": conf, "probabilities": {}}
            else:
                out[k] = {"type": "noul", "noul": verify if k == "verify" else (0.9 if k.startswith("present") else 0.0)}
        return out
    return FakeEyes(answer)


def make(tmp_path, eyes, pages=None, start="page", transitions=None):
    s = Settings(data_dir=tmp_path, settle_cap_s=0.3)
    be = RowBackend(pages or {"page": PAGE}, start, transitions or {}, shot=None)
    pipe = Pipeline(be, eyes, Thresholds(), s, RunLog(s.runs_dir, "test"))
    policy = Policy(s, Brief(name="t", allowed_sites=["localhost"]), KillSwitch())
    return pipe, policy, be


BRAIDED_ADD = "the 'Add' button in the 'USB-C cable 1 m, braided' row"


def test_lookalike_candidates_show_their_row():
    tree = parse_ax(PAGE)
    crit, _ = candidate_criteria(tree, candidates(tree))
    adds = [d for d in crit.values() if d.startswith('button "Add"')]
    assert len(adds) == 2
    assert 'row "USB-C cable 1 m | CB-101"' in adds[0] and 'row "USB-C cable 1 m, braided | CB-102"' in adds[1]


async def test_wrong_row_is_refused_before_acting(tmp_path):
    eyes = responder({"pick_1": ("CB-101", 0.95), "pick": ("CB-101", 0.95)})
    pipe, policy, be = make(tmp_path, eyes)
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": BRAIDED_ADD, "expect": "the braided cable is in the order"}]), policy)
    assert res.status == "needs_help" and "braided" in res.reason
    assert be.clicked == []
    assert res.receipts[0]["check"] == "refused"


async def test_stale_lookahead_pick_is_regrounded(tmp_path):
    eyes = responder({"pick_1": ("CB-101", 0.95), "pick": ("CB-102", 0.95)})
    pipe, policy, be = make(tmp_path, eyes, {"page": PAGE, "added": ADDED}, transitions={("page", "click:Add@13"): "added"})
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": BRAIDED_ADD, "expect": "the braided cable is in the order"}]), policy)
    assert res.status == "done", res.reason
    assert be.clicked == [13]
    r = res.to_dict()["receipts"][0]
    assert r["check"] == "passed" and "CB-102" in r["picked"] and "Added USB-C cable 1 m, braided" in r["changed"]


async def test_destructive_click_needs_a_second_agreeing_pick(tmp_path):
    target = "the 'Remove' button in the 'DisplayPort 1.4 cable 2 m (DP-142)' row"
    plan = Plan(steps=[{"do": "click", "target": target, "expect": "DisplayPort is gone from the draft order"}])
    # The shuffled second pick is unsure: refuse.
    pipe, policy, be = make(tmp_path, responder({"pick_1": ("DP-142", 0.95), "pick": ("DP-142", 0.6)}))
    res = await pipe.run_plan(plan, policy)
    assert res.status == "needs_help" and "shuffled pick did not confirm" in res.reason
    assert be.clicked == []
    # Both picks agree: click it.
    pipe, policy, be = make(tmp_path, responder({"pick_1": ("DP-142", 0.95), "pick": ("DP-142", 0.95)}))
    res = await pipe.run_plan(plan, policy)
    assert res.status == "done", res.reason
    assert be.clicked == [23]


async def test_no_retry_when_the_action_changed_the_page(tmp_path):
    """The 'Got it' click worked (the dialog is gone) but the check said no: hand back, never click twice."""
    eyes = responder({"pick_1": ("Got it", 1.0), "pick": ("Got it", 1.0)}, verify=0.0)
    pipe, policy, be = make(tmp_path, eyes, {"popup": POPUP, "page": PAGE}, start="popup",
                            transitions={("popup", "click:Got it@26"): "page"})
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "the 'Got it' button", "expect": "the shipping notice is closed"}]), policy)
    assert res.status == "failed" and "not retried" in res.reason
    assert be.clicked == [26]
    assert res.receipts[-1]["check"] == "failed" and "Shipping notice" in res.receipts[-1]["changed"]


async def test_retry_still_happens_when_nothing_changed(tmp_path):
    eyes = responder({"pick_1": ("CB-102", 0.95), "pick": ("CB-102", 0.95)}, verify=0.0)
    pipe, policy, be = make(tmp_path, eyes)  # no transition: the click does nothing
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": BRAIDED_ADD, "expect": "the braided cable is in the order"}]), policy)
    assert res.status == "failed" and "verification failed" in res.reason
    assert be.clicked == [13, 13]


async def test_shuffled_pick_state_uses_the_questions_labels(tmp_path):
    """Regression: the state listed elements in page order as e0..eN while a shuffled question used the same
    labels for a different order, so the second pick of every double check saw contradictory input."""
    seen = []

    def record(state, qs, images):
        seen.append((state, qs))
        return {k: {"type": "choice", "choice": "none", "confidence": 0.9, "probabilities": {}} for k in qs}

    pipe, policy, be = make(tmp_path, FakeEyes(record))
    obs = await be.observe_ax()
    for shuffle in (False, True, True, True):
        await pipe.grounder.ask_pick("the 'Remove' button in the 'DP-142' row", obs, shuffle=shuffle)
    for state, qs in seen:
        listed = dict(line.split(": ", 1) for line in state["elements"].split("\n"))
        asked = {k: v for k, v in qs["pick"]["criteria"].items() if k != "none"}
        assert listed == asked
