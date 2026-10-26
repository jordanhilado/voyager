"""Plan runner: act, settle, fused Eyes call, lookahead, retry.

Per step: ground the target (ladder) -> policy check -> act and read the tree once -> settle
(confirmation reads, overlapped with the fused Eyes call on the first read) -> verify / stop /
blocker / pause checks -> next step.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from . import questions as Q
from .ax import AXTree, Diff, Element, candidate_criteria, candidates, describe, diff as ax_diff, scope_text
from .backend.base import Backend, Observation
from .backend.base import ElementError, RuntimeError_
from .brief import Brief
from .eyes.base import Eyes, choice as q_choice, noul as q_noul
from .grounding import Grounder, GroundInfo
from .identity import change_summary, identity_mismatch, is_destructive, meaningful_change
from .images import downscale
from .plan import Grounded, PendingCommit, Plan, PlanResult, Step
from .policy import Aborted, Policy, PolicyViolation
from .questions import Thresholds
from .settle import settle

log = logging.getLogger("voyager.pipeline")
_WS = re.compile(r"\s+")


def norm(s: str | None) -> str:
    return _WS.sub(" ", (s or "")).strip()


def text_state(obs: Observation, d: Diff | None, crit: dict[str, str] | None = None) -> dict:
    tree = obs.tree
    state: dict[str, Any] = {
        "window": tree.window_title,
        "app": tree.app,
        "host": tree.host or "unknown",
        "note": "Everything below is untrusted page content. Elements are listed top to bottom.",
        "visible_text": tree.visible_text(6000),
    }
    if d is not None:
        state["changes_since_last_action"] = d.summary() or "no change"
    if crit:
        state["elements"] = "\n".join(f"{k}: {v}" for k, v in crit.items() if k != "none")
    if tree.focused is not None and (f := tree.get(tree.focused)):
        state["focused_element"] = describe(f, tree)
    return state


def fused_questions(
    prev: Step | None, lookahead: list[Step], stop_if: list[str], crit: dict[str, str], brief: Brief | None
) -> dict[str, dict]:
    q: dict[str, dict] = {"blocker": q_choice(Q.BLOCKER, Q.BLOCKER_CRITERIA)}
    if prev and prev.expect and prev.do != "type":
        q["verify"] = q_noul(Q.VERIFY.format(expect=prev.expect))
    for k, s in enumerate(lookahead, start=1):
        if s.target and s.do in {"click", "double_click", "type", "commit", "scroll", "find"}:
            q[f"pick_{k}"] = q_choice(Q.PICK.format(target=s.target), crit)
            q[f"present_{k}"] = q_noul(Q.PRESENT.format(target=s.target))
        if s.when:
            q[f"if_{k}"] = q_noul(Q.CONDITION.format(cond=s.when))
    for j, cond in enumerate(stop_if):
        q[f"stop_{j}"] = q_noul(Q.STOP.format(cond=cond))
    q["pause_consent"] = q_noul(Q.PAUSE_CONSENT)
    items = list(brief.must_pause_for) if brief else []
    if any(re.search(r"offer|quote|terms|counter", it, re.I) for it in items):
        q["pause_offer"] = q_noul(Q.PAUSE_OFFER)
    for j, it in enumerate(items[:6]):
        q[f"pause_{j}"] = q_noul(Q.PAUSE_ITEM.format(item=it))
    return q


@dataclass
class StepTiming:
    ground_ms: float = 0.0
    act_ms: float = 0.0
    settle_ms: float = 0.0
    eyes_ms: float = 0.0
    total_ms: float = 0.0
    rung: int = 0
    speculation_hit: bool | None = None

    def to_dict(self) -> dict:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class Fused:
    answers: dict[str, Any]
    mapping: dict[str, Element]
    obs: Observation
    ms: float = 0.0


class _Stop(Exception):
    def __init__(self, reason: str, obs: Observation, answers: dict):
        super().__init__(reason)
        self.reason, self.obs, self.answers = reason, obs, answers


class Pipeline:
    def __init__(self, backend: Backend, eyes: Eyes, th: Thresholds, settings, runlog=None):
        self.backend = backend
        self.eyes = eyes
        self.th = th
        self.settings = settings
        self.runlog = runlog
        self.grounder = Grounder(eyes, backend, th, runlog, state_fn=lambda o, crit=None: text_state(o, None, crit if crit is not None else candidate_criteria(o.tree, candidates(o.tree))[0]))
        self.lookahead: dict[int, tuple[str, float]] = {}  # step index -> (element key, confidence)
        self.step_timings: list[dict] = []
        self.receipts: list[dict] = []
        self.pending: PendingCommit | None = None
        self.policy: Policy | None = None

    def _log(self, kind: str, **data: Any) -> None:
        if self.runlog:
            self.runlog.event(kind, **data)

    # ---- fused call -----------------------------------------------------------------
    async def fused(self, prev: Step | None, obs: Observation, d: Diff | None, plan: Plan, next_index: int, brief: Brief | None) -> Fused:
        if self.policy is not None:
            self.policy.check_screen(obs.tree)
        cands = candidates(obs.tree)
        crit, mapping = candidate_criteria(obs.tree, cands)
        lookahead = plan.steps[next_index : next_index + 3]
        qs = fused_questions(prev, lookahead, plan.stop_if, crit, brief)
        t0 = time.perf_counter()
        answers = await self.eyes.ask(text_state(obs, d, crit), qs)
        ms = (time.perf_counter() - t0) * 1000
        for k, s in enumerate(lookahead, start=1):
            a = answers.get(f"pick_{k}")
            if a and a.get("choice") in mapping and a["choice"] != "none":
                self.lookahead[next_index + k - 1] = (mapping[a["choice"]].key, float(a.get("confidence", 0)))
        self._log("fused", prev=prev.do if prev else None, next_index=next_index, ms=round(ms, 1), hash=obs.tree.hash[:12])
        return Fused(answers, mapping, obs, ms)

    # ---- checks on fused answers -------------------------------------------------------
    def _triggered_stop(self, answers: dict, plan: Plan) -> str | None:
        for j, cond in enumerate(plan.stop_if):
            a = answers.get(f"stop_{j}")
            if a and float(a.get("noul", 0)) >= self.th.T_STOP:
                return cond
        return None

    def _pause(self, answers: dict, brief: Brief | None = None) -> str | None:
        labels = {"pause_consent": "the page asks for consent or confirmation", "pause_offer": "the page asks the user to accept or decline an offer or terms"}
        for j, it in enumerate((brief.must_pause_for if brief else [])[:6]):
            labels[f"pause_{j}"] = f"must_pause_for: {it}"
        for key, label in labels.items():
            a = answers.get(key)
            if a and float(a.get("noul", 0)) >= self.th.T_PAUSE:
                return f"{label} ({float(a['noul']):.2f})"
        return None

    def _blocker(self, answers: dict) -> str:
        a = answers.get("blocker") or {}
        if a.get("choice") and a["choice"] != "none" and float(a.get("confidence", 0)) >= 0.6:
            return a["choice"]
        return "none"

    # ---- helpers --------------------------------------------------------------------------
    async def _snapshot_path(self, name: str = "help") -> str | None:
        try:
            full = await self.backend.observe_full()
            if full.screenshot and self.runlog:
                return self.runlog.save_image(name, downscale(full.screenshot))
        except Exception as e:  # noqa: BLE001
            log.warning("screenshot failed: %s", e)
        return None

    def _result(self, status: str, i: int, reason: str, obs: Observation, answers: dict | None = None, d: Diff | None = None, **extra) -> PlanResult:
        tree = obs.tree
        return PlanResult(
            status=status, step_index=i, reason=reason, answers=_compact(answers or {}),
            diff_text=d.summary(1500) if d else "", page_text=tree.visible_text(2500), title=tree.window_title,
            url=tree.url, timings={"steps": self.step_timings, "rungs": dict(self.grounder.rung_counts)},
            receipts=list(self.receipts), **extra,
        )

    def _receipt(self, i: int, step: Step, g: Grounded | None, check: str, changed: str = "", tree: AXTree | None = None) -> None:
        """One line per step for the planner: what was picked (with its row), whether the step checked out,
        and what changed. ``tree`` is the observation the pick was made on."""
        r = {"step": i, "do": step.do, "check": check}
        if step.target:
            r["target"] = step.target
        if g is not None:
            r["picked"] = g.label
            el = tree.get(g.index) if tree is not None and g.index is not None else None
            if el is not None and (row := scope_text(tree, el)):
                r["picked"] += f' · row "{row}"'
        if changed:
            r["changed"] = changed
        self.receipts.append(r)

    async def _guard(self, step: Step, i: int, g: Grounded, info: GroundInfo, obs: Observation, slot: int) -> tuple[Grounded, Observation, str | None]:
        """Before acting: the pick must show what the target names, and destructive clicks need a second,
        shuffled pick to agree. Returns (grounded, observation, refusal reason or None)."""
        tree = obs.tree
        el = tree.get(g.index) if g.kind == "element" and g.index is not None else None
        missing = identity_mismatch(step.target or "", tree, el)
        if missing and step.do != "find" and info.source in ("fused", "lookahead"):
            # The pick came from a call made while planning ahead; ask once more on this page before refusing.
            self._log("identity_reground", step=i, picked=g.label, missing=missing)
            g2, info2 = await self.grounder.ground(step, obs, slot=slot, text_only=True)
            if info2.obs is not None:
                obs, tree = info2.obs, info2.obs.tree
            el2 = tree.get(g2.index) if g2 is not None and g2.index is not None else None
            if g2 is not None and not identity_mismatch(step.target or "", tree, el2):
                g, el, missing = g2, el2, []
        if missing:
            self._log("identity_mismatch", step=i, picked=g.label, missing=missing)
            names = ", ".join(repr(m) for m in missing)
            return g, obs, (f"picked {g.label}, which does not show {names} from the target {step.target!r}; not acting. "
                            "Name the target by its exact visible label or the row it is in")
        do = step.then if step.do == "find" else step.do
        if do in ("click", "double_click") and is_destructive(step.target, g.label):
            if el is None:
                return g, obs, f"{step.target!r} looks destructive and was found only by screen position; not clicking it unchecked"
            el2, conf2, _, _ = await self.grounder.ask_pick(step.target or "", obs, shuffle=True)
            agreed = el2 is not None and el2.key == el.key and conf2 >= self.th.T_COMMIT
            self._log("destructive_check", step=i, picked=g.label, agreed=agreed, confidence=round(conf2, 3))
            if not agreed:
                return g, obs, f"{step.target!r} is destructive and a second, shuffled pick did not confirm {g.label}; not clicking"
        return g, obs, None

    async def _act(self, step: Step, g: Grounded | None, obs: Observation) -> Observation:
        do = step.then if step.do == "find" else step.do
        if do in ("click", "double_click"):
            count = 2 if do == "double_click" else 1
            mouse = bool(getattr(step, "_retried", False))  # a retry after a failed check uses a real pointer click
            if g and g.kind == "element":
                return await self.backend.click(index=g.index, count=count, mouse=mouse)
            return await self.backend.click(xy=g.xy, count=count, mouse=mouse)
        if step.do == "key":
            return await self.backend.press_key(step.key or "")
        if step.do == "scroll":
            if g and g.kind == "element":
                return await self.backend.scroll(step.direction or "down", step.amount, index=g.index)
            xy = g.xy if g else None
            return await self.backend.scroll(step.direction or "down", step.amount, xy=xy)
        raise ValueError(step.do)

    async def _type(self, step: Step, g: Grounded, obs: Observation) -> tuple[Observation, bool, str]:
        """Enter text with the cheapest method that leaves evidence in the accessibility tree.

        1. setValue when the element is settable (plain inputs, most contenteditables).
        2. click + paste (rich editors such as Messenger's ignore setValue but accept a paste).
        3. typeText keystrokes.
        Each attempt is verified in code against the tree: the element's value, its child text, or
        text that newly appeared on the page.
        """
        text = step.text or ""
        el = obs.tree.get(g.index) if g.index is not None else None
        after = obs
        attempts: list[str] = []
        if el is not None and el.settable:
            try:
                after = await self.backend.set_value(g.index, text)
                ok, how = text_entered(obs.tree, after.tree, g.key, text)
                if ok:
                    return after, True, f"setValue:{how}"
                attempts.append(f"setValue -> {how}")
            except ElementError as e:
                attempts.append(f"setValue error: {str(e)[:80]}")
        # Focus the box (click) then paste; paste goes to the focused element on macOS apps.
        focused = await self.backend.click(index=g.index) if g.kind == "element" else await self.backend.click(xy=g.xy)
        fresh = focused.tree.find_key(g.key) if g.key else None
        after = await self.backend.paste(text, index=fresh.index if fresh is not None else None)
        ok, how = text_entered(obs.tree, after.tree, g.key, text)
        if ok:
            return after, True, f"paste:{how}"
        attempts.append(f"paste -> {how}")
        after = await self.backend.type_text(text, index=fresh.index if fresh is not None else None)
        ok, how = text_entered(obs.tree, after.tree, g.key, text)
        if ok:
            return after, True, f"typeText:{how}"
        attempts.append(f"typeText -> {how}")
        return after, False, "; ".join(attempts)

    async def _password_check(self, el: Element | None, obs: Observation, policy: Policy) -> None:
        if el is None or not policy.password_noul_needed(el):
            return
        a = await self.eyes.ask({"field": describe(el, obs.tree), "page": obs.tree.visible_text(2000)}, {"pw": q_noul(Q.PASSWORD_FIELD)})
        if float(a["pw"].get("noul", 0)) > self.th.T_PASSWORD:
            raise PolicyViolation(f"refusing to type: the field looks like a password/PIN/code field ({a['pw']['noul']:.2f})")

    async def _leak_check(self, step: Step, brief: Brief | None) -> None:
        if not brief or not brief.never_share or not step.text:
            return
        a = await self.eyes.ask({"draft": step.text}, {"leak": q_noul(Q.LEAK.format(never_share=", ".join(brief.never_share)))})
        if float(a["leak"].get("noul", 0)) >= self.th.T_LEAK:
            raise PolicyViolation(f"refusing to type: the draft appears to contain never_share information ({a['leak']['noul']:.2f})")

    # ---- reading ------------------------------------------------------------------------------
    async def read_region(self, obs: Observation, region: str) -> dict:
        """Text of the container that best matches ``region`` (a text Choice over containers)."""
        tree = obs.tree
        conts = [c for c in tree.containers() if c.index != 0]
        if conts:
            crit: dict[str, str] = {}
            mapping: dict[str, Element] = {}
            for i, c in enumerate(conts[:200]):
                snippet = " ".join(e.name for e in list(tree.subtree(c))[1:12] if e.name)[:120]
                crit[f"c{i}"] = f"{describe(c, tree)} containing: {snippet}"
                mapping[f"c{i}"] = c
            crit["none"] = "none of these containers"
            a = await self.eyes.ask(text_state(obs, None), {"pick": q_choice(Q.REGION_PICK.format(region=region), crit)})
            ch = a["pick"].get("choice")
            conf = float(a["pick"].get("confidence", 0))
            if ch in mapping and conf >= 0.5:
                c = mapping[ch]
                lines = [e.name if not e.value else f"{e.name}: {e.value}" for e in tree.subtree(c) if (e.name or e.value)]
                return {"region": describe(c, tree), "text": "\n".join(lines)[:12000], "confidence": round(conf, 3), "key": c.key}
        return {"region": None, "text": tree.visible_text(8000), "note": "no matching container; full visible text returned"}

    async def _find(self, step: Step, i: int, obs: Observation, answers: dict, mapping: dict, slot: int, policy: Policy,
                    plan: Plan, brief: Brief | None) -> tuple[Grounded | None, GroundInfo, Observation, dict, dict, str]:
        """Scroll ``step.within`` (or the page) until ``step.target`` can be grounded from text."""
        box_key: str | None = None
        if step.within:
            full = await self.backend.observe_ax(scope="full")
            r = await self.read_region(full, step.within)
            box_key = r.get("key")
            obs = await self.backend.observe_ax()  # back to the action scope (indices follow the latest read)
            f = await self.fused(None, obs, None, plan, i, brief)
            answers, mapping, slot = f.answers, f.mapping, 1
        scrolls = 0
        info = GroundInfo()
        while True:
            # Only the first attempt may re-read the whole window; after a scroll the newly shown
            # items are inside the action-scope read.
            g, info = await self.grounder.ground(step, obs, answers, mapping, slot=slot, lookahead_key=self.lookahead.get(i),
                                                 text_only=True, full_fallback=scrolls == 0)
            if info.obs is not None:
                obs = info.obs
            if g is not None:
                return g, info, obs, answers, mapping, ""
            if scrolls >= step.max_scrolls:
                return None, info, obs, answers, mapping, f"not found after {scrolls} scrolls"
            policy.kill.check()
            policy.budget.step()
            box = obs.tree.find_key(box_key) if box_key else None
            before = obs.tree.hash
            first = await self.backend.scroll("down", 1, index=box.index if box is not None else None)
            s = await settle(self.backend, first, self.settings.settle_cap_s)
            scrolls += 1
            self._log("find_scroll", step=i, n=scrolls, within=step.within, moved=s.obs.tree.hash != before)
            if s.obs.tree.hash == before:
                # Nothing new is listed (the end of the list, or a list whose items were all
                # listed already): another scroll would show the eyes the same thing.
                return None, info, s.obs, answers, mapping, f"scrolling {step.within or 'the page'} showed nothing new after {scrolls} scroll(s)"
            obs = s.obs
            f = await self.fused(None, obs, None, plan, i, brief)
            answers, mapping, slot = f.answers, f.mapping, 1
            self.lookahead.pop(i, None)
            if (cond := self._triggered_stop(answers, plan)):
                raise _Stop(f"stop condition: {cond}", obs, answers)
            if (why := self._pause(answers, brief)):
                raise _Stop(f"paused: {why}", obs, answers)

    # ---- the loop ---------------------------------------------------------------------------
    async def run_plan(self, plan: Plan, policy: Policy, brief: Brief | None = None) -> PlanResult:
        self.policy = policy
        self.receipts = []
        obs = await self.backend.observe_ax()
        try:
            policy.check_screen(obs.tree)
            policy.check_site(obs.tree)
            f = await self.fused(None, obs, None, plan, 0, brief)
        except PolicyViolation as e:
            return self._result("failed", 0, f"policy: {e}", obs)
        answers, mapping = f.answers, f.mapping
        base = 0  # plan index of lookahead slot 1 in ``answers``
        d: Diff | None = None
        typed_text: str | None = None
        reads: list[dict] = []
        skipped: list[int] = []

        def result(status: str, i: int, reason: str, o: Observation, ans: dict | None = None, dd: Diff | None = None, **extra) -> PlanResult:
            r = self._result(status, i, reason, o, ans, dd, **extra)
            r.reads, r.skipped = reads, skipped
            return r

        i = 0
        while i < len(plan.steps):
            step = plan.steps[i]
            t_step = time.perf_counter()
            tm = StepTiming()
            try:
                policy.kill.check()
                policy.budget.step()
            except (Aborted, PolicyViolation) as e:
                return result("stopped" if isinstance(e, Aborted) else "failed", i, str(e), obs, answers, d)

            # Answers about step i live in slot i - base + 1 of the last fused call (3 slots).
            if not 1 <= i - base + 1 <= 3:
                try:
                    f = await self.fused(None, obs, None, plan, i, brief)
                except PolicyViolation as e:
                    return result("failed", i, f"policy: {e}", obs)
                answers, mapping, base = f.answers, f.mapping, i
            slot = i - base + 1

            if (cond := self._triggered_stop(answers, plan)):
                return result("stopped", i, f"stop condition: {cond}", obs, answers, d)
            if (why := self._pause(answers, brief)):
                return result("stopped", i, f"paused: {why}", obs, answers, d)
            b = self._blocker(answers)
            if b == "loading":
                s = await settle(self.backend, obs, self.settings.settle_cap_s, first_delay_s=0.3)
                obs = s.obs
                f = await self.fused(None, obs, None, plan, i, brief)
                answers, mapping, base, slot = f.answers, f.mapping, i, 1
                b = self._blocker(answers)
            if b in ("sign_in_wall", "captcha", "error_message"):
                path = await self._snapshot_path()
                return result("needs_help", i, f"blocker: {b}", obs, answers, d, screenshot_path=path)
            # A modal/popover is not a blocker by itself: plans open panels and menus on purpose.
            # It only matters if the next target cannot be grounded; then it is reported as context.
            dialog_note = " (a dialog or panel is open; close it or target something inside it)" if b == "modal_dialog" else ""

            # ---- conditional steps ---------------------------------------------------------
            if step.when:
                a = answers.get(f"if_{slot}")
                if a is None:
                    a = (await self.eyes.ask(text_state(obs, None), {"if": q_noul(Q.CONDITION.format(cond=step.when))}))["if"]
                v = float(a.get("noul", 0))
                self._log("condition", step=i, when=step.when, noul=v)
                if v < self.th.T_IF:
                    skipped.append(i)
                    self._receipt(i, step, None, "skipped", "condition was false")
                    i += 1
                    continue

            # ---- wait_for --------------------------------------------------------------
            if step.do == "wait_for":
                ok, obs = await self.wait_until(step.expect or "", step.timeout_s, policy)
                if not ok:
                    self._receipt(i, step, None, "failed", "timed out")
                    return result("failed", i, f"wait_for timed out: {step.expect}", obs, answers)
                self._receipt(i, step, None, "passed")
                f = await self.fused(None, obs, None, plan, i + 1, brief)
                answers, mapping, base, d = f.answers, f.mapping, i + 1, None
                i += 1
                continue

            # ---- read --------------------------------------------------------------------
            if step.do == "read":
                full = await self.backend.observe_ax(scope="full")
                try:
                    policy.check_screen(full.tree)
                except PolicyViolation as e:
                    return result("failed", i, f"policy: {e}", full)
                r = await self.read_region(full, step.region or "")
                r.pop("key", None)
                reads.append({"step": i, "asked": step.region, **r})
                self._receipt(i, step, None, "read", f"{len(r.get('text', ''))} chars from {r.get('region') or step.region!r}")
                self._log("read", step=i, region=step.region, found=r.get("region"), chars=len(r.get("text", "")))
                # The full read renumbered the elements: observe again before the next step acts.
                obs = await self.backend.observe_ax()
                self.lookahead.clear()
                f = await self.fused(None, obs, None, plan, i + 1, brief)
                answers, mapping, base, d = f.answers, f.mapping, i + 1, None
                i += 1
                continue

            # ---- ground ------------------------------------------------------------------
            g: Grounded | None = None
            info = GroundInfo()
            el: Element | None = None
            if step.do == "find":
                t0 = time.perf_counter()
                try:
                    g, info, obs, answers, mapping, why = await self._find(step, i, obs, answers, mapping, slot, policy, plan, brief)
                except _Stop as st:
                    return result("stopped", i, st.reason, st.obs, st.answers)
                except (Aborted, PolicyViolation) as e:
                    return result("stopped" if isinstance(e, Aborted) else "failed", i, str(e), obs, answers)
                base = i
                tm.ground_ms = (time.perf_counter() - t0) * 1000
                if g is None:
                    path = await self._snapshot_path()
                    return result("needs_help", i, f"could not find {step.target!r}: {why}{dialog_note}", obs, {**answers, **info.answers}, d,
                                  screenshot_path=path, candidates=info.top)
                tm.rung = info.rung
                el = obs.tree.get(g.index) if g.index is not None else None
                self._log("ground", step=i, target=step.target, rung=info.rung, source=info.source, picked=g.label, confidence=g.confidence, ms=round(tm.ground_ms, 1))
                if step.then == "none":
                    self._receipt(i, step, g, "found", tree=obs.tree)
                    f = await self.fused(None, obs, None, plan, i + 1, brief)
                    answers, mapping, base, d = f.answers, f.mapping, i + 1, None
                    policy.budget.succeeded()
                    tm.total_ms = (time.perf_counter() - t_step) * 1000
                    self.step_timings.append({"step": i, "do": step.do, **tm.to_dict()})
                    self._log("step_done", step=i, do=step.do, verified=True, **tm.to_dict())
                    i += 1
                    continue
            elif step.target and step.do != "key":
                t0 = time.perf_counter()
                g, info = await self.grounder.ground(step, obs, answers, mapping, slot=slot, lookahead_key=self.lookahead.get(i), text_only=bool(step.when))
                tm.ground_ms = (time.perf_counter() - t0) * 1000
                tm.rung = info.rung
                if info.obs is not None:
                    obs = info.obs  # grounding re-read the page; its indices are the live ones
                    self.lookahead.clear()
                if g is None:
                    if step.when:
                        # The condition held but nothing matching the target is listed: treat the
                        # condition as false rather than climbing to screenshots for an optional step.
                        self._log("condition_target_missing", step=i, target=step.target)
                        skipped.append(i)
                        i += 1
                        continue
                    path = info.screenshot_path or await self._snapshot_path()
                    return result(
                        "needs_help", i, f"could not ground {step.target!r} (ladder exhausted at rung {info.rung}){dialog_note}",
                        obs, {**answers, **info.answers}, d, screenshot_path=path, candidates=info.top,
                    )
                el = obs.tree.get(g.index) if g.index is not None else None
                self._log("ground", step=i, target=step.target, rung=info.rung, source=info.source, picked=g.label, confidence=g.confidence, ms=round(tm.ground_ms, 1))

            # ---- guard: the pick shows what the target names; destructive clicks are double-checked ----
            if g is not None and step.target:
                g, guarded_obs, why = await self._guard(step, i, g, info, obs, slot)
                if guarded_obs is not obs:
                    obs = guarded_obs
                    self.lookahead.clear()
                el = obs.tree.get(g.index) if g.kind == "element" and g.index is not None else None
                if why:
                    self._receipt(i, step, g, "refused", why, obs.tree)
                    path = await self._snapshot_path()
                    return result("needs_help", i, why, obs, answers, d, screenshot_path=path, candidates=info.top)

            # ---- policy ------------------------------------------------------------------
            try:
                policy.check_action(step, obs.tree, el)
                if step.do == "type":
                    await self._password_check(el, obs, policy)
                    await self._leak_check(step, brief)
            except PolicyViolation as e:
                self._log("policy_refused", step=i, reason=str(e))
                return result("failed", i, f"policy: {e}", obs, answers, d)
            except Aborted as e:
                return result("stopped", i, str(e), obs, answers, d)

            # ---- commit: hand back ---------------------------------------------------------
            if step.do == "commit":
                self.pending = PendingCommit(plan, i, step, g, typed_text, brief.name if brief else None, time.time())
                self._receipt(i, step, g, "pending commit", tree=obs.tree)
                self._log("ready_to_commit", step=i, summary=step.summary, target=g.to_dict() if g else None)
                return result(
                    "ready_to_commit", i, step.summary or "", obs, answers, d,
                    pending_commit={"step_index": i, "summary": step.summary, "target": g.to_dict() if g else {"key": step.key}, "typed_text": typed_text},
                    steps_completed=i,
                )

            # ---- act --------------------------------------------------------------------------
            t0 = time.perf_counter()
            try:
                if step.do == "type":
                    first, ok, how = await self._type(step, g, obs)  # type: ignore[arg-type]
                    tm.act_ms = (time.perf_counter() - t0) * 1000
                    if not ok:
                        policy.budget.failed()
                        return result("failed", i, f"typed text did not appear in the box ({how})", first, answers)
                    typed_text = step.text
                    self._log("typed", step=i, how=how)
                else:
                    first = await self._act(step, g, obs)
                    tm.act_ms = (time.perf_counter() - t0) * 1000
            except ElementError as e:
                # Stale index: re-observe and retry the step once from grounding.
                self._log("stale_element", step=i, error=str(e)[:200])
                obs = await self.backend.observe_ax()
                f = await self.fused(None, obs, None, plan, i, brief)
                answers, mapping, base = f.answers, f.mapping, i
                if getattr(step, "_retried", False):
                    return result("failed", i, f"element error: {e}", obs, answers)
                object.__setattr__(step, "_retried", True)
                continue
            except NotImplementedError as e:
                return result("failed", i, str(e), obs, answers)
            except PolicyViolation as e:
                return result("failed", i, f"policy: {e}", obs, answers)
            except RuntimeError_ as e:
                return result("failed", i, f"action failed: {e}", obs, answers)
            self._log("act", step=i, do=step.do, target=g.to_dict() if g else None, act_ms=round(tm.act_ms, 1), backend=getattr(self.backend, "last_timing", {}))

            # ---- settle, with the fused call overlapped on the first read -------------------
            t0 = time.perf_counter()
            d1 = ax_diff(obs.tree, first.tree)
            next_index = i + 1
            spec_task = asyncio.create_task(self.fused(step, first, d1, plan, next_index, brief))
            s = await settle(self.backend, first, self.settings.settle_cap_s)
            tm.settle_ms = (time.perf_counter() - t0) * 1000
            try:
                if s.obs.tree.hash == first.tree.hash:
                    f = await spec_task
                    tm.speculation_hit = True
                else:
                    spec_task.cancel()
                    d1 = ax_diff(obs.tree, s.obs.tree)
                    f = await self.fused(step, s.obs, d1, plan, next_index, brief)
                    tm.speculation_hit = False
            except PolicyViolation as e:
                return result("failed", i, f"policy: {e}", s.obs)
            tm.eyes_ms = f.ms
            # Act on the latest read: its indices are the live ones (same hash as f.obs when the
            # speculation hit).
            new_obs, d = s.obs, d1
            answers, mapping, base = f.answers, f.mapping, next_index

            # ---- verify -----------------------------------------------------------------------
            verified = True
            if step.expect and step.do != "type":
                v = float((answers.get("verify") or {}).get("noul", 1.0))
                if v < self.th.T_VERIFY:
                    verified = await self._verify_with_image(step, new_obs)
                    if not verified and meaningful_change(d1):
                        # The action did something, just not what was expected. Retrying could do it twice
                        # (a second item in the cart, a second delete), so hand back with what changed.
                        policy.budget.failed()
                        self._log("verify_failed_after_change", step=i, noul=v, changed=change_summary(d1))
                        self._receipt(i, step, g, "failed", change_summary(d1), obs.tree)
                        path = await self._snapshot_path("verify")
                        return result("failed", i, f"the step changed the page but did not confirm: {step.expect} (noul {v:.2f}); "
                                      "not retried, so nothing is done twice. See 'changes'", new_obs, answers, d, screenshot_path=path)
                    if not verified and not getattr(step, "_retried", False):
                        # Retry the action once: re-ground on the fresh page and go again. The fused
                        # answers in hand were asked for step i+1, so re-ask with step i in slot 1.
                        object.__setattr__(step, "_retried", True)
                        self._log("verify_retry", step=i, noul=v)
                        policy.budget.failed()
                        self.lookahead.pop(i, None)
                        obs = new_obs
                        try:
                            f = await self.fused(None, obs, None, plan, i, brief)
                        except PolicyViolation as e:
                            return result("failed", i, f"policy: {e}", obs)
                        answers, mapping, base, d = f.answers, f.mapping, i, None
                        continue
                    if not verified:
                        policy.budget.failed()
                        self._receipt(i, step, g, "failed", change_summary(d1), obs.tree)
                        path = await self._snapshot_path("verify")
                        return result("failed", i, f"verification failed: {step.expect} (noul {v:.2f})", new_obs, answers, d, screenshot_path=path)
            policy.budget.succeeded()
            tm.total_ms = (time.perf_counter() - t_step) * 1000
            self.step_timings.append({"step": i, "do": step.do, **tm.to_dict()})
            self._log("step_done", step=i, do=step.do, verified=verified, **tm.to_dict())
            check = "text entered" if step.do == "type" else ("passed" if step.expect else "unchecked")
            self._receipt(i, step, g, check, change_summary(d1), obs.tree)
            obs = new_obs
            i += 1

        return result("done", len(plan.steps), "plan completed", obs, answers, d, steps_completed=len(plan.steps))

    async def _verify_with_image(self, step: Step, obs: Observation) -> bool:
        if not self.eyes.supports_images:
            return False
        full = await self.backend.observe_full()
        if not full.screenshot:
            return False
        a = await self.eyes.ask(
            {**text_state(full, None), "note": "`screenshot` shows the window"},
            {"verify": q_noul(Q.VERIFY.format(expect=step.expect))},
            {"screenshot": full.screenshot},
        )
        v = float(a["verify"].get("noul", 0))
        self._log("verify_image", step=step.do, noul=v)
        return v >= self.th.T_VERIFY

    # ---- wait_until (also used by cu_wait_until) ----------------------------------------
    async def wait_until(self, statement: str, timeout_s: float, policy: Policy | None = None) -> tuple[bool, Observation]:
        deadline = time.time() + timeout_s
        prev = await self.backend.observe_ax()
        last_hash = None
        while True:
            if policy:
                policy.kill.check()
            if prev.tree.hash != last_hash:
                last_hash = prev.tree.hash
                if policy:
                    policy.check_screen(prev.tree)
                a = await self.eyes.ask(text_state(prev, None), {"ok": q_noul(Q.WAIT_UNTIL.format(statement=statement))})
                if float(a["ok"].get("noul", 0)) >= self.th.T_VERIFY:
                    return True, prev
            if time.time() >= deadline:
                return False, prev
            await asyncio.sleep(0.4)
            prev = await self.backend.observe_ax()


def text_entered(before: AXTree, after: AXTree, key: str | None, text: str) -> tuple[bool, str]:
    """Did ``text`` land in the target box? Evidence, strongest first: the element's value, text in
    its subtree, the focused element's value/subtree (rich editors rename themselves on focus), or
    text that is on the page now and was not before."""
    want = norm(text)
    if not want:
        return True, "empty"
    cands: list[Element] = []
    el = after.find_key(key) if key else None
    if el is not None:
        cands.append(el)
    if after.focused is not None and (f := after.get(after.focused)) is not None and f is not el:
        cands.append(f)
    for c in cands:
        got = norm(c.value)
        if got == want:
            return True, "exact"
        if got and want in got:
            return True, "contains"
        sub = norm(" ".join((x.name or "") + " " + (x.value or "") for x in after.subtree(c)))
        if want in sub:
            return True, "subtree"
    if want in norm(after.raw) and want not in norm(before.raw):
        return True, "appeared_on_page"
    if el is None and not cands:
        return False, "target box not found after typing"
    return False, f"box value is {norm(cands[0].value)[:60]!r}"


def _compact(answers: dict) -> dict:
    out = {}
    for k, a in answers.items():
        if not isinstance(a, dict):
            continue
        if a.get("type") == "choice":
            out[k] = {"choice": a.get("choice"), "confidence": round(float(a.get("confidence", 0)), 3)}
        elif a.get("type") == "noul":
            out[k] = round(float(a.get("noul", 0)), 3)
        else:
            out[k] = {kk: vv for kk, vv in a.items() if kk != "probabilities"}
    return out
