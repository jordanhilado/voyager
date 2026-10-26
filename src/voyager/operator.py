"""The Operator: one object behind both the MCP server tools and the standalone planner.

Owns the backend, the eyes, the policy, the run log and the pending commit.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import questions as Q
from .ax import describe
from .backend import make_backend
from .backend.base import RuntimeError_, TargetInfo
from .brief import Brief, find_brief
from .config import Settings
from .eyes import make_eyes
from .eyes.base import choice as q_choice, noul as q_noul
from .digest import digest
from .images import downscale
from .notify import push
from .pipeline import Pipeline, text_state
from .plan import PendingCommit, Plan, PlanResult
from .policy import Aborted, DriverLock, KillSwitch, Policy, PolicyViolation
from .questions import Thresholds
from .runlog import RunLog, prune
from .settle import settle

log = logging.getLogger("voyager.operator")


class Operator:
    def __init__(self, settings: Settings, backend=None, eyes=None):
        self.settings = settings
        self.backend = backend or make_backend(settings)
        self.eyes = eyes or make_eyes(settings)
        self.th = Thresholds.load(settings.thresholds_path)
        self.kill = KillSwitch()
        self.lock = DriverLock(settings.lock_path)
        self.brief: Brief = Brief.permissive()
        self.runlog: RunLog | None = None
        self.policy: Policy | None = None
        self.pipeline: Pipeline | None = None
        self.bound: TargetInfo | None = None
        self.started = False

    # ---- lifecycle --------------------------------------------------------------------
    async def start(self) -> None:
        import asyncio

        self.kill.bind_loop(asyncio.get_running_loop())
        self.lock.acquire()
        await self.backend.start()
        try:
            await self.backend.list_targets()  # learns the platform and warms the backend
        except Exception as e:  # noqa: BLE001
            log.warning("initial target listing failed: %s", e)
        await self.eyes.warm()
        n = prune(self.settings.runs_dir, self.settings.run_log_days)
        if n:
            log.info("pruned %d old runs", n)
        self.started = True
        log.info("operator ready: eyes=%s backend=%s", self.eyes.name, type(self.backend).__name__)

    async def stop(self) -> None:
        try:
            await self.backend.stop()
        finally:
            await self.eyes.close()
            if self.runlog:
                self.runlog.close()
            self.lock.release()

    def _new_run(self, name: str) -> None:
        if self.runlog:
            self.runlog.close()
        self.runlog = RunLog(self.settings.runs_dir, name)
        self.eyes.on_call = lambda rec: self.runlog and self.runlog.event("eyes", **rec)
        self.kill.reset()
        self.policy = Policy(self.settings, self.brief, self.kill)
        self.pipeline = Pipeline(self.backend, self.eyes, self.th, self.settings, self.runlog)

    def _require(self) -> Pipeline:
        if not self.pipeline or not self.bound:
            raise PolicyViolation("no bound target: call cu_bind first")
        return self.pipeline

    # ---- tools --------------------------------------------------------------------------
    async def bind(self, target: str, brief: str | None = None, brief_inline: dict | None = None, plan: dict | None = None) -> dict:
        out = await self._bind(target, brief, brief_inline)
        return await self._then_plan(out, plan)

    async def _bind(self, target: str, brief: str | None, brief_inline: dict | None) -> dict:
        self.brief = find_brief(brief, self.settings.briefs_dir, brief_inline)
        self._new_run(f"bind:{target}")
        assert self.policy and self.runlog
        targets = await self.backend.list_targets()
        if not targets:
            raise PolicyViolation("the backend reports no apps or windows")
        chosen = _match_target(target, targets)
        if chosen is None and getattr(self.backend, "platform", "") in ("mac", "unknown"):
            # macOS launches/binds apps by display name; try the literal name before asking the eyes.
            name = re.sub(r"^(the\s+)?|(\s+(app|application|window))$", "", target.strip(), flags=re.I).strip()
            if name and len(name) <= 40:
                try:
                    self.policy.check_app(name)
                    obs = await self.backend.bind(TargetInfo("app", name, app=name))
                    chosen = TargetInfo("app", name, app=name, title=obs.tree.window_title)
                    return await self._after_bind(chosen, obs)
                except (RuntimeError_, PolicyViolation) as e:
                    log.info("direct bind of %r failed (%s); asking the eyes", name, e)
        if chosen is None:
            crit = {f"t{i}": t.describe() for i, t in enumerate(targets[:200])}
            crit["none"] = "none of these"
            a = await self.eyes.ask({"note": "pick the target the user described"}, {"pick": q_choice(Q.BIND_PICK.format(target=target), crit)})
            ch = a["pick"].get("choice")
            if ch == "none" or ch not in crit or float(a["pick"].get("confidence", 0)) < self.th.T_PICK:
                raise PolicyViolation(f"could not identify target {target!r}; running apps: {[t.app or t.title for t in targets][:30]}")
            chosen = targets[int(ch[1:])]
        self.policy.check_app(chosen.app or str(chosen.id), chosen.title)
        obs = await self.backend.bind(chosen)
        return await self._after_bind(chosen, obs)

    async def _after_bind(self, chosen: TargetInfo, obs) -> dict:
        assert self.policy and self.runlog
        self.bound = chosen
        self.policy.check_screen(obs.tree)
        self.policy.check_site(obs.tree)
        self.runlog.event("bind", target=chosen.describe(), title=obs.tree.window_title, url=obs.tree.url, brief=self.brief.name)
        a = await self.eyes.ask(text_state(obs, None), {"blocker": q_choice(Q.BLOCKER, Q.BLOCKER_CRITERIA)})
        return {
            "bound": chosen.describe(), "title": obs.tree.window_title, "url": obs.tree.url, "host": obs.tree.host,
            "blocker": a["blocker"].get("choice"), "interactive_elements": len(obs.tree.interactive()),
            "brief": self.brief.name, "autonomy": self.brief.autonomy, "run_id": self.runlog.id, "eyes": self.eyes.name,
            "page": digest(obs.tree),
        }

    async def _then_plan(self, out: dict, plan: dict | None) -> dict:
        """Run the plan that came with cu_open / cu_bind, so a predictable task needs no extra turn.
        The page digest stays in the result only when the planner will need it to re-plan."""
        if plan is None or out.get("status") == "failed":
            return out
        if out.get("blocker") in ("sign_in_wall", "captcha", "error_message"):
            out["plan_result"] = {"status": "not_run", "reason": f"blocker on open: {out['blocker']}"}
            return out
        res = await self.run_plan(plan)
        out["plan_result"] = res
        if res.get("status") in ("done", "ready_to_commit"):
            out.pop("page", None)
        return out

    async def open(self, url: str, brief: str | None = None, brief_inline: dict | None = None, browser: str | None = None, plan: dict | None = None) -> dict:
        """Open a URL in the desktop browser, wait for it to load, bind that window, and run
        ``plan`` if one was given."""
        out = await self._open(url, brief, brief_inline, browser)
        return await self._then_plan(out, plan)

    async def _open(self, url: str, brief: str | None, brief_inline: dict | None, browser: str | None) -> dict:
        import asyncio
        from urllib.parse import urlsplit

        from .ax import host_of
        from .brief import base_domain
        from .launch import open_url, pick_browser

        if "://" not in url:
            url = "https://" + url
        host = host_of(url)
        if not host:
            raise PolicyViolation(f"not a valid URL: {url!r}")
        self.brief = find_brief(brief, self.settings.briefs_dir, brief_inline)
        if not self.brief.allowed_sites:
            self.brief.allowed_sites = [base_domain(host)]
        if self.brief.site_check == "lenient" and brief_inline is None and brief is None:
            self.brief.site_check = "strict"
        self._new_run(f"open:{url}")
        assert self.policy and self.runlog
        self.policy.check_url(url)
        running = [t.app for t in await self.backend.list_targets() if t.running]
        app = pick_browser(browser or self.settings.browser, self.settings.allowed_apps, running)
        if not app:
            raise PolicyViolation("no supported browser found; set VOYAGER_BROWSER")
        self.policy.check_app(app)
        if app not in self.settings.allowed_apps:
            raise PolicyViolation(f"browser {app!r} is not in the daemon's allowed_apps; add it to ~/.voyager/config.yaml")
        self.runlog.event("open", url=url, browser=app)
        await asyncio.to_thread(open_url, url, app)
        target = TargetInfo("app", app, app=app)
        want = urlsplit(url).hostname or host
        deadline = time.time() + 45
        obs = None
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                obs = await self.backend.bind(target)
            except RuntimeError_ as e:  # a just-launched browser can time out at first
                last_err = e
                self.runlog.event("open_retry", error=str(e)[:200])
                await asyncio.sleep(1.0)
                continue
            h = obs.tree.host or ""
            if h and (h == want or base_domain(h) == base_domain(want)):
                break
            await asyncio.sleep(0.5)
        if obs is None:
            raise PolicyViolation(f"could not read the browser window after opening the URL ({last_err})")
        obs = await self._wait_for_content(obs, min(deadline, time.time() + 15))
        target.title = obs.tree.window_title
        out = await self._after_bind(target, obs)
        out["opened"] = url
        if not (obs.tree.host and base_domain(obs.tree.host) == base_domain(want)):
            out["warning"] = f"the browser window shows {obs.tree.url!r}, not {url!r}; it may still be loading"
        return out

    async def _wait_for_content(self, obs, deadline: float):
        """A just-opened page can report its URL before the browser has built its accessibility
        tree. Wait until the web content has elements, then until it stops changing."""
        def has_content(tree) -> bool:
            webs = [e for e in tree.elements if e.role in ("HTML content", "web area")]
            return not webs or any(sum(1 for _ in tree.subtree(w)) > 3 for w in webs)

        while not has_content(obs.tree) and time.time() < deadline:
            await asyncio.sleep(0.25)
            obs = await self.backend.observe_ax()
        s = await settle(self.backend, obs, cap_s=max(0.5, min(3.0, deadline - time.time())))
        return s.obs

    async def run_plan(self, plan_data: dict) -> dict:
        pipe = self._require()
        assert self.policy and self.runlog
        plan = Plan(**plan_data)
        if plan.brief and plan.brief != self.brief.name:
            self.brief = find_brief(plan.brief, self.settings.briefs_dir)
            self.policy.brief = self.brief
        self.runlog.event("plan", steps=[s.model_dump(exclude_none=True) for s in plan.steps], stop_if=plan.stop_if)
        t0 = time.perf_counter()
        try:
            result = await pipe.run_plan(plan, self.policy, self.brief)
        except Aborted as e:
            result = PlanResult("stopped", 0, str(e))
        except PolicyViolation as e:
            result = PlanResult("failed", 0, f"policy: {e}")
        result.timings["plan_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self.runlog.event("plan_result", status=result.status, step=result.step_index, reason=result.reason, ms=result.timings["plan_ms"])
        if result.status == "ready_to_commit":
            await push(self.settings, "Voyager: approval needed", result.reason or "a commit is waiting for approval")
        elif result.status in ("stopped", "needs_help"):
            await push(self.settings, f"Voyager: {result.status}", result.reason, priority="default")
        return result.to_dict()

    async def commit(self, step_ref: int | None, summary: str, approved: bool = True) -> dict:
        pipe = self._require()
        assert self.policy and self.runlog
        pc: PendingCommit | None = pipe.pending
        if pc is None:
            raise PolicyViolation("no pending commit; run a plan whose last step is a commit first")
        if step_ref is not None and step_ref != pc.step_index:
            raise PolicyViolation(f"pending commit is step {pc.step_index}, not {step_ref}")
        if time.time() - pc.created_at > 600:
            pipe.pending = None
            raise PolicyViolation("pending commit expired (10 minutes); re-run the plan")
        self.policy.kill.check()
        if not approved:
            if self.brief.autonomy != "auto_send_within_brief":
                raise PolicyViolation("commit not approved and the brief requires approve_each_send")
            if pc.typed_text:
                a = await self.eyes.ask(
                    {"draft": pc.typed_text, "may_share": self.brief.may_share, "ask": self.brief.ask},
                    {"ok": q_noul(Q.WITHIN_BRIEF)},
                )
                if float(a["ok"].get("noul", 0)) < self.th.T_WITHIN_BRIEF:
                    raise PolicyViolation(f"auto-send refused: draft is not clearly within the brief ({a['ok']['noul']:.2f})")
        obs = await self.backend.observe_ax()
        self.policy.check_site(obs.tree)
        step = pc.step
        target = None
        if step.target:
            target, info = await pipe.grounder.confirm_commit(step, obs)
            if target is None:
                self.runlog.event("commit_refused", reason="shuffled double check disagreed", answers=info.answers)
                return {"status": "needs_help", "reason": "the shuffled double check did not agree on the commit target", "screenshot_path": info.screenshot_path}
        self.runlog.event("commit", step=pc.step_index, summary=summary, target=target.to_dict() if target else {"key": step.key})
        t0 = time.perf_counter()
        if target is not None:
            first = await self.backend.click(index=target.index) if target.kind == "element" else await self.backend.click(xy=target.xy)
        else:
            first = await self.backend.press_key(step.key or "Return")
        s = await settle(self.backend, first, self.settings.settle_cap_s)
        pipe.pending = None
        from .ax import diff as ax_diff

        d = ax_diff(obs.tree, s.obs.tree)
        ms = (time.perf_counter() - t0) * 1000
        self.runlog.event("commit_done", ms=round(ms, 1), changes=d.summary(800))
        return {"status": "done", "summary": summary, "ms": round(ms, 1), "changes": d.summary(1500), "page_text": s.obs.tree.visible_text(2500), "title": s.obs.tree.window_title}

    async def wait_until(self, statement: str, timeout_s: float = 60) -> dict:
        pipe = self._require()
        timeout_s = max(1, min(timeout_s, 300))
        before = await self.backend.observe_ax()
        assert self.policy
        self.policy.check_screen(before.tree)
        ok, obs = await pipe.wait_until(statement, timeout_s, self.policy)
        from .ax import diff as ax_diff

        d = ax_diff(before.tree, obs.tree)
        return {"status": "true" if ok else "timeout", "statement": statement, "changes": d.summary(2000), "page_text": obs.tree.visible_text(4000), "title": obs.tree.window_title, "url": obs.tree.url}

    async def read(self, region: str) -> dict:
        pipe = self._require()
        obs = await self.backend.observe_ax(scope="full")
        assert self.policy
        self.policy.check_screen(obs.tree)
        r = await pipe.read_region(obs, region)
        r.pop("key", None)
        if r.get("region") is None:
            full = await self.backend.observe_full()
            if full.screenshot and self.runlog:
                r["screenshot_path"] = self.runlog.save_image("read", downscale(full.screenshot))
        return r

    async def observe(self) -> tuple[dict, bytes | None]:
        if not self.bound:
            targets = await self.backend.list_targets()
            return {"bound": None, "targets": [t.describe() for t in targets[:60]]}, None
        full = await self.backend.observe_full()
        tree = full.tree
        if self.policy:
            self.policy.check_screen(tree)
        from .ax import candidates

        cands = candidates(tree)[:80]
        img = downscale(full.screenshot) if full.screenshot else None
        path = self.runlog.save_image("observe", img) if (img and self.runlog) else None
        info = {
            "title": tree.window_title, "app": tree.app, "url": tree.url, "host": tree.host,
            "focused": describe(tree.get(tree.focused), tree) if tree.focused is not None and tree.get(tree.focused) else None,
            "elements": [describe(e, tree) for e in cands], "screenshot_path": path,
            "visible_text": tree.visible_text(2000),
        }
        return info, img

    async def stop_run(self) -> dict:
        self.kill.trip()
        if self.pipeline:
            self.pipeline.pending = None
        try:
            await self.backend.reset()
        except Exception as e:  # noqa: BLE001
            log.warning("reset after stop failed: %s", e)
        if self.runlog:
            self.runlog.event("stopped_by_user")
        return {"status": "stopped"}

    def health(self) -> dict:
        return {
            "ok": self.started, "eyes": self.eyes.name, "bound": self.bound.describe() if self.bound else None,
            "brief": self.brief.name, "run": self.runlog.id if self.runlog else None, "platform": getattr(self.backend, "platform", "?"),
        }


def _match_target(text: str, targets: list[TargetInfo]) -> TargetInfo | None:
    t = text.lower()
    scored: list[tuple[int, TargetInfo]] = []
    for ti in targets:
        app = (ti.app or "").lower()
        title = (ti.title or "").lower()
        score = 0
        if app and app in t:
            score += 3
        if title and title in t:
            score += 2
        if ti.url and any(w in t for w in [ti.url.lower()]):
            score += 2
        if ti.running:
            score += 1
        if score >= 3:
            scored.append((score, ti))
    scored.sort(key=lambda s: -s[0])
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None
