import io
from pathlib import Path

from PIL import Image

from tests.fakes import FakeBackend
from voyager.brief import Brief
from voyager.config import Settings
from voyager.eyes.base import FakeEyes
from voyager.eyes.local import LocalEyes
from voyager.pipeline import Pipeline
from voyager.plan import Plan
from voyager.policy import KillSwitch, Policy
from voyager.questions import Thresholds
from voyager.runlog import RunLog

FIX = Path(__file__).parent / "fixtures"


def _shot():
    b = io.BytesIO(); Image.new("RGB", (800, 600), "white").save(b, "JPEG"); return b.getvalue()


def make_backend():
    pages = {"open": (FIX / "chat_open.txt").read_text(), "panel": (FIX / "chat_panel.txt").read_text()}
    return FakeBackend(pages, "open", {("open", "click:Chat with us"): "panel"}, shot=_shot())


def make_pipeline(tmp_path, eyes, backend=None):
    s = Settings(data_dir=tmp_path, settle_cap_s=0.5)
    backend = backend or make_backend()
    runlog = RunLog(s.runs_dir, "test")
    pipe = Pipeline(backend, eyes, Thresholds(), s, runlog)
    policy = Policy(s, Brief(name="t", allowed_sites=["example-store.com"]), KillSwitch())
    return pipe, policy, backend


def label_eyes():
    """Eyes that pick by quoted label and verify by keyword presence (deterministic)."""
    return LocalEyes()


async def test_plan_click_type_commit(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, label_eyes())
    plan = Plan(steps=[
        {"do": "click", "target": "the 'Chat with us' button", "expect": "a 'Chat with support' panel with a 'Send' button is open"},
        {"do": "type", "target": "the 'Type your message' box in the chat panel", "text": "Hi, how long does shipping take?"},
        {"do": "commit", "target": "the 'Send' button in the chat panel", "summary": "Send the shipping question"},
    ], stop_if=["the page asks the user to 'sign in'"])
    res = await pipe.run_plan(plan, policy, Brief(name="t"))
    assert res.status == "ready_to_commit", res.reason
    assert be.actions[:2] == ["click:Chat with us", "set:Type your message"]
    assert res.pending_commit["typed_text"].startswith("Hi, how long")
    assert pipe.pending is not None and pipe.pending.step_index == 2
    assert res.steps_completed == 2
    assert pipe.step_timings and pipe.step_timings[0]["rung"] == 1
    # commit target double-check agrees for a clear label
    g, info = await pipe.grounder.confirm_commit(plan.steps[2], await be.observe_ax())
    assert g is not None and g.label.startswith('button "Send"')


async def test_stop_condition_halts(tmp_path):
    def responder(state, qs, images):
        out = {}
        for k, q in qs.items():
            if q["type"] == "choice":
                out[k] = {"type": "choice", "choice": "none", "confidence": 0.9, "probabilities": {n: 0 for n in q["criteria"]}}
            else:
                out[k] = {"type": "noul", "noul": 0.95 if k.startswith("stop_") else 0.0}
        return out

    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(responder))
    plan = Plan(steps=[{"do": "click", "target": "x", "expect": "y"}], stop_if=["the chat is closed"])
    res = await pipe.run_plan(plan, policy)
    assert res.status == "stopped" and "chat is closed" in res.reason
    assert be.actions == []


async def test_blocker_needs_help_with_screenshot(tmp_path):
    def responder(state, qs, images):
        out = {}
        for k, q in qs.items():
            if k == "blocker":
                out[k] = {"type": "choice", "choice": "sign_in_wall", "confidence": 0.9, "probabilities": {}}
            elif q["type"] == "choice":
                out[k] = {"type": "choice", "choice": "none", "confidence": 0.9, "probabilities": {}}
            else:
                out[k] = {"type": "noul", "noul": 0.0}
        return out

    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(responder))
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "x", "expect": "y"}]), policy)
    assert res.status == "needs_help" and "sign_in_wall" in res.reason
    assert res.screenshot_path and Path(res.screenshot_path).exists()


async def test_grounding_falls_through_ladder_to_box(tmp_path):
    """Force rung 1 and 2 to answer none; rung 3 Box with presence should yield a point click."""
    calls = []

    def responder(state, qs, images):
        out = {}
        for k, q in qs.items():
            calls.append((k, q["type"], bool(images)))
            if q["type"] == "choice":
                out[k] = {"type": "choice", "choice": "none", "confidence": 0.95, "probabilities": {n: (0.95 if n == "none" else 0.0) for n in q["criteria"]}}
            elif q["type"] == "box":
                out[k] = {"type": "box", "box": {"top": 0.5, "bottom": 0.6, "left": 0.2, "right": 0.3}, "confidence": 0.9}
            else:
                out[k] = {"type": "noul", "noul": 0.9 if k == "present" else 0.0}
        return out

    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(responder))
    from voyager.plan import Step

    obs = await be.observe_full()
    g, info = await pipe.grounder.ground(Step(do="click", target="the tiny icon", expect="x"), obs)
    assert g is not None and g.kind == "point" and g.rung == 3
    assert g.xy == (200, 330)  # center of the box on an 800x600 image
    kinds = [(k, t, img) for k, t, img in calls]
    assert ("pick", "choice", False) in kinds and ("pick", "choice", True) in kinds and ("box", "box", True) in kinds


async def test_grounding_crop_rung_when_window_box_is_weak(tmp_path):
    def responder(state, qs, images):
        out = {}
        for k, q in qs.items():
            if q["type"] == "choice":
                out[k] = {"type": "choice", "choice": "none", "confidence": 0.95, "probabilities": {}}
            elif q["type"] == "box":
                weak = images and "screenshot" in images
                out[k] = {"type": "box", "box": {"top": 0.4, "bottom": 0.6, "left": 0.4, "right": 0.6}, "confidence": 0.3 if weak else 0.9}
            else:
                out[k] = {"type": "noul", "noul": 0.9}
        return out

    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(responder))
    from voyager.plan import Step

    obs = await be.observe_full()
    g, info = await pipe.grounder.ground(Step(do="click", target="x", expect="y"), obs)
    assert g is not None and g.rung == 4 and g.kind == "point"
    assert "box_crop" in info.answers


async def test_password_field_refused_in_plan(tmp_path):
    pages = {"login": (FIX / "login.txt").read_text()}
    be = FakeBackend(pages, "login", {})
    pipe, policy, _ = make_pipeline(tmp_path, label_eyes(), be)
    policy.brief = Brief(name="t", allowed_sites=["accounts.example.com"])
    res = await pipe.run_plan(Plan(steps=[{"do": "type", "target": "the 'Password' field", "text": "hunter2"}]), policy, policy.brief)
    assert res.status == "failed" and "password" in res.reason
    assert be.actions == []


async def test_luhn_refused_in_plan(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, label_eyes())
    pipe_be = be
    be.page = "panel"
    res = await pipe.run_plan(Plan(steps=[{"do": "type", "target": "the 'Type your message' box", "text": "my card is 4111111111111111"}]), policy)
    assert res.status == "failed" and "Luhn" in res.reason and pipe_be.actions == []


async def test_off_allowlist_host_refused(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, label_eyes())
    policy.brief = Brief(name="t", allowed_sites=["americanexpress.com"])
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "the 'Chat with us' button", "expect": "x"}]), policy)
    assert res.status == "failed" and "allowed_sites" in res.reason and be.actions == []


async def test_kill_switch_aborts_mid_plan(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, label_eyes())
    orig = be.click

    async def click_then_trip(*a, **k):
        policy.kill.trip()
        return await orig(*a, **k)

    be.click = click_then_trip
    plan = Plan(steps=[
        {"do": "click", "target": "the 'Chat with us' button", "expect": "a 'Send' button is visible"},
        {"do": "click", "target": "the 'Send' button", "expect": "sent"},
    ])
    res = await pipe.run_plan(plan, policy)
    assert res.status == "stopped" and "kill switch" in res.reason
    assert be.actions == ["click:Chat with us"]


async def test_verification_failure_retries_then_fails(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, label_eyes())
    plan = Plan(steps=[{"do": "click", "target": "the 'Shipping policy' link", "expect": "a 'Refund calculator' is shown"}])
    res = await pipe.run_plan(plan, policy)
    assert res.status == "failed" and "verification failed" in res.reason
    assert be.actions.count("click:Shipping policy") == 2  # one retry


async def test_wait_until(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, label_eyes())
    ok, obs = await pipe.wait_until("a 'Chat with support' heading is visible", 0.5)
    assert not ok
    be.page = "panel"
    ok, obs = await pipe.wait_until("a 'Chat with support' heading is visible", 2)
    assert ok


async def test_verify_retry_regrounds_the_same_step(tmp_path):
    """Regression: after a failed verification the retry must re-pick step i, not reuse the
    lookahead pick that was asked for step i+1 (which once clicked the next button)."""
    pipe, policy, be = make_pipeline(tmp_path, label_eyes())
    plan = Plan(steps=[
        {"do": "click", "target": "the 'Shipping policy' link", "expect": "a 'Refund calculator' is shown"},
        {"do": "click", "target": "the 'Chat with us' button", "expect": "a 'Send' button is visible"},
    ])
    res = await pipe.run_plan(plan, policy)
    assert res.status == "failed" and res.step_index == 0
    assert be.actions == ["click:Shipping policy", "click:Shipping policy"]


def test_invisible_marks_stripped():
    from voyager.ax import parse_line

    _, _, e = parse_line("\t4 text \u200e2\u200e+\u200e3")
    assert e.name == "2+3"


def test_pause_questions_follow_the_brief():
    from voyager.pipeline import fused_questions

    q = fused_questions(None, [], [], {"none": "n"}, None)
    assert "pause_consent" in q and "pause_offer" not in q
    b = Brief(name="b", must_pause_for=["any offer or counteroffer", "the page asks the user to sign in"])
    q = fused_questions(None, [], [], {"none": "n"}, b)
    assert "pause_offer" in q and "pause_0" in q and "pause_1" in q and "sign in" in q["pause_1"]["instructions"]


async def test_brief_pause_item_stops_plan(tmp_path):
    def responder(state, qs, images):
        out = {}
        for k, q in qs.items():
            if q["type"] == "choice":
                out[k] = {"type": "choice", "choice": "none", "confidence": 0.9, "probabilities": {}}
            else:
                out[k] = {"type": "noul", "noul": 0.95 if k == "pause_0" else 0.0}
        return out

    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(responder))
    brief = Brief(name="b", must_pause_for=["a payment form is shown"])
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "x", "expect": "y"}]), policy, brief)
    assert res.status == "stopped" and "payment form" in res.reason and be.actions == []


def _dialog_responder(pick_label):
    def responder(state, qs, images):
        out = {}
        for k, q in qs.items():
            if k == "blocker":
                out[k] = {"type": "choice", "choice": "modal_dialog", "confidence": 0.95, "probabilities": {}}
            elif q["type"] == "choice":
                names = list(q["criteria"])
                ch = next((n for n in names if pick_label and pick_label in q["criteria"][n]), "none")
                out[k] = {"type": "choice", "choice": ch, "confidence": 0.97, "probabilities": {n: 0 for n in names}}
            else:
                out[k] = {"type": "noul", "noul": 0.9 if k in ("verify", "present_1") else 0.0}
        return out
    return responder


async def test_open_panel_is_not_a_blocker_when_target_is_inside(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(_dialog_responder('button "Send"')))
    be.page = "panel"
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "the Send button in the panel", "expect": "sent"}]), policy)
    assert res.status == "done" and be.actions == ["click:Send"]


async def test_open_panel_reported_when_target_missing(tmp_path):
    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(_dialog_responder(None)))
    be.page = "panel"
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "the Add carry-on bag button", "expect": "x"}]), policy)
    assert res.status == "needs_help" and "panel is open" in res.reason and be.actions == []


async def test_sign_in_wall_still_blocks(tmp_path):
    def responder(state, qs, images):
        return {k: ({"type": "choice", "choice": "sign_in_wall" if k == "blocker" else "none", "confidence": 0.95, "probabilities": {}} if q["type"] == "choice" else {"type": "noul", "noul": 0.0}) for k, q in qs.items()}
    pipe, policy, be = make_pipeline(tmp_path, FakeEyes(responder))
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "x", "expect": "y"}]), policy)
    assert res.status == "needs_help" and "sign_in_wall" in res.reason


async def test_rich_editor_falls_back_to_paste(tmp_path):
    """Messenger-style composer: setValue reports success but leaves the box empty; the pipeline
    must fall through to click + paste and verify by the child text node."""
    pages = {"panel": (FIX / "chat_panel.txt").read_text()}
    be = FakeBackend(pages, "panel", {}, rich={"Type your message"})
    pipe, policy, _ = make_pipeline(tmp_path, label_eyes(), be)
    plan = Plan(steps=[
        {"do": "type", "target": "the 'Type your message' box in the chat panel", "text": "Testing from my AI agent"},
        {"do": "commit", "key": "Return", "summary": "Send the test message"},
    ])
    res = await pipe.run_plan(plan, policy)
    assert res.status == "ready_to_commit", res.reason
    assert be.actions == ["set:Type your message", "click:Type your message", "paste:Testing from my AI agent"]


def test_text_entered_evidence(fixture_text):
    from voyager.ax import parse_ax
    from voyager.pipeline import text_entered

    before = parse_ax(fixture_text("chat_panel.txt"))
    key = before.get(11).key
    after = parse_ax(fixture_text("chat_panel.txt").replace("(settable) Type your message\n", "(settable) Type your message, Value: hello there\n"))
    assert text_entered(before, after, key, "hello there") == (True, "exact")
    # child text node under the box (rich editor)
    after2 = parse_ax(fixture_text("chat_panel.txt").replace("\t\t\t\t\t11 text entry area (settable) Type your message\n", "\t\t\t\t\t11 text entry area (settable) Type your message\n\t\t\t\t\t\t99 text hello there\n"))
    assert text_entered(before, after2, key, "hello there") == (True, "subtree")
    # text that already existed on the page is not evidence
    assert text_entered(before, before, key, "Chat with support")[0] is False
