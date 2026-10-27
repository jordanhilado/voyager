"""Fewer planner turns: find, conditional and read steps, plans on bind, the page digest, and
grounding that switches observations (partial reads, re-reads)."""
import pytest

from tests.fakes import FakeBackend
from tests.test_pipeline import make_pipeline
from voyager.ax import parse_ax
from voyager.backend.base import Observation
from voyager.brief import Brief
from voyager.config import Settings
from voyager.digest import digest
from voyager.eyes.local import LocalEyes
from voyager.operator import Operator
from voyager.plan import Plan
from voyager.settle import settle

HEAD = 'Window: "Messenger", App: Google Chrome.\n0 standard window Messenger\n\t1 scroll area\n\t\t2 HTML content Messenger, URL: example-store.com/messages\n'


def chats(names: list[str], extra: str = "") -> str:
    lines = [HEAD, "\t\t\t3 list Chats\n"]
    for k, n in enumerate(names):
        lines.append(f"\t\t\t\t{4 + k} link {n}, Value: example-store.com/t/{k}\n")
    return "".join(lines) + extra


CHAT = HEAD + "\t\t\t3 heading Dad\n\t\t\t4 text entry area (settable) Message Dad\n\t\t\t5 button Send\n"


async def test_find_scrolls_until_found_then_clicks(tmp_path):
    pages = {"p1": chats(["Alice", "Bob", "Carol"]), "p2": chats(["Dave", "Dad", "Erin"]), "chat": CHAT}
    be = FakeBackend(pages, "p1", {("p1", "scroll:down"): "p2", ("p2", "click:Dad"): "chat"})
    pipe, policy, _ = make_pipeline(tmp_path, LocalEyes(), be)
    plan = Plan(steps=[
        {"do": "find", "target": "the chat named 'Dad'", "in": "the chats list", "expect": "a 'Message Dad' box is shown"},
        {"do": "type", "target": "the 'Message Dad' box", "text": "Hello!"},
        {"do": "commit", "key": "Return", "summary": "Send 'Hello!' to Dad"},
    ])
    res = await pipe.run_plan(plan, policy, Brief(name="t"))
    assert res.status == "ready_to_commit", res.reason
    assert be.actions[:2] == ["scroll:down", "click:Dad"]
    assert res.pending_commit["typed_text"] == "Hello!"


class MispickEyes(LocalEyes):
    """Picks the first option with high confidence when no option shows the quoted words."""

    async def _ask(self, state, questions, images):
        out = await super()._ask(state, questions, images)
        for qid, q in questions.items():
            if q["type"] == "choice" and qid.startswith("pick") and out[qid]["choice"] == "none":
                first = next(k for k in q["criteria"] if k != "none")
                out[qid] = {"type": "choice", "choice": first, "confidence": 0.95, "probabilities": {first: 0.95, "none": 0.05}}
        return out


async def test_find_keeps_scrolling_past_a_pick_that_does_not_show_the_target(tmp_path):
    pages = {"p1": chats(["Alice", "Bob", "Carol"]), "p2": chats(["Dave", "Dad", "Erin"]), "chat": CHAT}
    be = FakeBackend(pages, "p1", {("p1", "scroll:down"): "p2", ("p2", "click:Dad"): "chat"})
    pipe, policy, _ = make_pipeline(tmp_path, MispickEyes(), be)
    plan = Plan(steps=[{"do": "find", "target": "the chat named 'Dad'", "in": "the chats list", "expect": "a 'Message Dad' box is shown"}])
    res = await pipe.run_plan(plan, policy, Brief(name="t"))
    assert res.status == "done", res.reason
    assert be.actions[:2] == ["scroll:down", "click:Dad"]


async def test_find_reports_end_of_list(tmp_path):
    be = FakeBackend({"p1": chats(["Alice", "Bob"])}, "p1", {})
    pipe, policy, _ = make_pipeline(tmp_path, LocalEyes(), be)
    plan = Plan(steps=[{"do": "find", "target": "the chat named 'Dad'", "expect": "the chat is open"}])
    res = await pipe.run_plan(plan, policy, Brief(name="t"))
    assert res.status == "needs_help" and "showed nothing new" in res.reason
    assert be.actions == ["scroll:down"]


BANNER = HEAD + "\t\t\t3 container Cookie banner\n\t\t\t\t4 button Accept cookies\n\t\t\t5 button Chat with us\n"
NO_BANNER = HEAD + "\t\t\t5 button Chat with us\n"


@pytest.mark.parametrize("page,expected", [("banner", ["click:Accept cookies", "click:Chat with us"]), ("plain", ["click:Chat with us"])])
async def test_conditional_step(tmp_path, page, expected):
    be = FakeBackend({"banner": BANNER, "plain": NO_BANNER}, page, {("banner", "click:Accept cookies"): "plain"})
    pipe, policy, _ = make_pipeline(tmp_path, LocalEyes(), be)
    plan = Plan(steps=[
        {"do": "click", "target": "the 'Accept cookies' button", "expect": "no 'Cookie banner' is shown", "if": "a 'Cookie banner' is shown"},
        {"do": "click", "target": "the 'Chat with us' button", "expect": "'Chat with us' was clicked"},
    ])
    res = await pipe.run_plan(plan, policy, Brief(name="t"))
    assert res.status == "done", res.reason
    assert be.actions == expected
    assert res.skipped == ([] if page == "banner" else [0])


async def test_read_step_returns_region_text(tmp_path):
    page = HEAD + "\t\t\t3 container Transcript\n\t\t\t\t4 text Dad: are you coming Sunday?\n\t\t\t5 button Send\n"
    be = FakeBackend({"p": page}, "p", {})
    pipe, policy, _ = make_pipeline(tmp_path, LocalEyes(), be)
    res = await pipe.run_plan(Plan(steps=[{"do": "read", "region": "the transcript"}]), policy, Brief(name="t"))
    assert res.status == "done"
    assert res.reads and "are you coming Sunday" in res.reads[0]["text"]
    assert "reads" in res.to_dict()


async def test_bind_returns_digest_and_runs_plan(tmp_path):
    s = Settings(data_dir=tmp_path, allowed_apps=["Google Chrome"], settle_cap_s=0.3)
    pages = {"p1": chats(["Alice", "Dad"]), "chat": CHAT}
    be = FakeBackend(pages, "p1", {("p1", "click:Dad"): "chat"})
    op = Operator(s, backend=be, eyes=LocalEyes())
    inline = {"goal": "message Dad", "allowed_sites": ["example-store.com"], "may_share": ["Hello!"]}
    out = await op.bind("the Google Chrome app", brief_inline=inline)
    assert "Dad" in str(out["page"]["elements"]) and out["page"]["note"].startswith("Untrusted")
    plan = {"steps": [
        {"do": "find", "target": "the chat named 'Dad'", "expect": "a 'Message Dad' box is shown"},
        {"do": "type", "target": "the 'Message Dad' box", "text": "Hello!"},
        {"do": "commit", "key": "Return", "summary": "Send 'Hello!' to Dad"},
    ]}
    be.page = "p1"
    out = await op.bind("the Google Chrome app", brief_inline=inline, plan=plan)
    assert out["plan_result"]["status"] == "ready_to_commit", out["plan_result"]
    assert "page" not in out  # the plan succeeded, so the digest is not needed
    done = await op.commit(None, "Send 'Hello!' to Dad")
    assert done["status"] == "done" and be.actions[-1] == "key:Return"


def test_digest_groups_by_container():
    tree = parse_ax(chats(["Alice", "Dad"], "\t\t\t9 text entry area (settable) Search Messenger\n"))
    d = digest(tree)
    assert any("Chats" in k for k in d["elements"])
    assert any("Search Messenger" in e for v in d["elements"].values() for e in v)


class PartialBackend(FakeBackend):
    """Action-scope reads omit the off-screen part of the page; full reads include it."""

    def __init__(self, *a, hidden: str, **k):
        super().__init__(*a, **k)
        self.hidden = hidden

    async def observe_ax(self, scope="action"):
        obs = self._obs()
        if scope == "full":
            return obs
        text = "\n".join(l for l in obs.raw.split("\n") if self.hidden not in l)
        o = Observation.from_text(text)
        o.partial = True
        return o


async def test_partial_read_falls_back_to_full_and_acts_on_that_tree(tmp_path):
    page = HEAD + "\t\t\t3 button Top\n\t\t\t4 button Far below\n"
    be = PartialBackend({"p": page, "q": NO_BANNER}, "p", {("p", "click:Far below"): "q"}, hidden="Far below")
    pipe, policy, _ = make_pipeline(tmp_path, LocalEyes(), be)
    res = await pipe.run_plan(Plan(steps=[{"do": "click", "target": "the 'Far below' button", "expect": "'Chat with us' is shown"}]), policy, Brief(name="t"))
    assert res.status == "done", res.reason
    assert be.actions == ["click:Far below"]


class Ticking(FakeBackend):
    settle_quiet_s = 0.2
    settle_poll_s = 0.01


async def test_settle_waits_for_quiet_window():
    be = Ticking({"p": NO_BANNER}, "p", {})
    first = await be.observe_ax()
    s = await settle(be, first, cap_s=2.0)
    assert s.stable and s.ms >= 200 and s.samples >= 5


def test_find_and_read_validation():
    with pytest.raises(ValueError, match="expect"):
        Plan(steps=[{"do": "find", "target": "x"}])
    Plan(steps=[{"do": "find", "target": "x", "then": "none"}])
    with pytest.raises(ValueError, match="region"):
        Plan(steps=[{"do": "read"}])
    with pytest.raises(ValueError, match="conditional"):
        Plan(steps=[{"do": "commit", "key": "Return", "summary": "s", "if": "x"}])


class SlowPage(FakeBackend):
    """The first reads show the URL with an empty web area, like a tab that is still building."""

    def _obs(self, shot=False):
        self.obs_count += 1
        if self.obs_count <= 3:
            return Observation.from_text(HEAD)
        return Observation.from_text(self._text(), screenshot=self.shot if shot else None)


async def test_open_waits_for_page_content(tmp_path, monkeypatch):
    monkeypatch.setattr("voyager.launch.installed_browsers", lambda: ["Google Chrome"])
    monkeypatch.setattr("voyager.launch.open_url", lambda url, app: None)
    s = Settings(data_dir=tmp_path, allowed_apps=["Google Chrome"], settle_cap_s=0.3)
    be = SlowPage({"p": chats(["Alice", "Dad"])}, "p", {})
    op = Operator(s, backend=be, eyes=LocalEyes())
    out = await op.open("https://example-store.com/messages", brief_inline={"goal": "g"}, browser="Google Chrome")
    assert out["interactive_elements"] >= 2 and "Dad" in str(out["page"])
