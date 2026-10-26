"""The grounding ladder (spec section 6). Each rung runs only if the one above is below threshold.

1. Text Choice over candidate elements (no image). A confident lookahead pick counts as rung 1.
2. Image Choice: same question with the window screenshot.
3. Box on the window screenshot, paired with a presence Noul.
4. Box on a padded crop around the rung-3 box.
5. Snap to element (needs bounds; unavailable on macOS AX text) else click the coordinate.
6. Return to the planner with the screenshot and the best candidates.

Commit targets always get a second question with the candidate order shuffled; both answers must
agree and both confidences must clear T_COMMIT.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from . import questions as Q
from .ax import AXTree, Element, candidate_criteria, candidates, describe
from .backend.base import Backend, Observation
from .eyes.base import Eyes, box as q_box, choice as q_choice, noul as q_noul
from .images import Box, downscale, load, padded_crop, to_jpeg
from .plan import Grounded, Step
from .questions import Thresholds

log = logging.getLogger("voyager.grounding")


@dataclass
class GroundInfo:
    rung: int = 0
    answers: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    top: list[str] = field(default_factory=list)
    ms: float = 0.0
    source: str = ""
    # Set when grounding re-observed the page: the returned index refers to this observation,
    # and the caller must use it (indices are renumbered on every read).
    obs: Observation | None = None


def pick_from_answer(a: dict | None, mapping: dict[str, Element], th: float) -> tuple[Element | None, float]:
    if not a or a.get("type") != "choice":
        return None, 0.0
    ch, conf = a.get("choice"), float(a.get("confidence") or 0.0)
    if ch in mapping and ch != "none" and conf >= th:
        return mapping[ch], conf
    return None, conf


def top_candidates(a: dict | None, mapping: dict[str, Element], tree: AXTree, n: int = 5) -> list[str]:
    if not a or "probabilities" not in a:
        return []
    probs = sorted(a["probabilities"].items(), key=lambda kv: -kv[1])
    out = []
    for label, p in probs[:n]:
        if label in mapping:
            out.append(f"{describe(mapping[label], tree)} ({p:.2f})")
        elif label == "none":
            out.append(f"none ({p:.2f})")
    return out


class Grounder:
    def __init__(self, eyes: Eyes, backend: Backend, th: Thresholds, runlog=None, state_fn=None):
        self.eyes = eyes
        self.backend = backend
        self.th = th
        self.runlog = runlog
        self.state_fn = state_fn  # (obs, criteria) -> dict state for Eyes
        self.rung_counts: dict[int, int] = {}

    def _state(self, obs: Observation, crit: dict[str, str] | None = None) -> dict:
        """The state sent with a pick. ``crit`` must be the criteria of that same question: a shuffled
        question relabels the elements, and a state listing them in page order under the same labels
        would contradict it."""
        if self.state_fn:
            return self.state_fn(obs, crit)
        tree = obs.tree
        return {"window": tree.window_title, "app": tree.app, "host": tree.host, "visible_text": tree.visible_text(4000)}

    def _count(self, rung: int) -> None:
        self.rung_counts[rung] = self.rung_counts.get(rung, 0) + 1

    # ---- rung 1 / 2 -------------------------------------------------------------------
    async def ask_pick(
        self, target: str, obs: Observation, image: bytes | None = None, shuffle: bool = False
    ) -> tuple[Element | None, float, dict, dict[str, Element]]:
        cands = candidates(obs.tree)
        if shuffle:
            cands = list(cands)
            random.shuffle(cands)
        crit, mapping = candidate_criteria(obs.tree, cands)
        qs = {"pick": q_choice(Q.PICK.format(target=target), crit)}
        state = self._state(obs, crit)
        images = {"screenshot": image} if image else None
        if image:
            state = {**state, "note": "`screenshot` shows the window; the elements are listed top to bottom"}
        answers = await self.eyes.ask(state, qs, images)
        el, conf = pick_from_answer(answers.get("pick"), mapping, self.th.T_PICK)
        return el, conf, answers, mapping

    # ---- rung 3 / 4 -------------------------------------------------------------------
    async def ask_box(self, target: str, image: bytes, name: str, instr: str) -> tuple[Box | None, float, float, dict]:
        qs = {"box": q_box(instr.format(target=target)), "present": q_noul(Q.PRESENT.format(target=target) + f" (see `{name}`)")}
        answers = await self.eyes.ask({"note": f"`{name}` is an image of the window"}, qs, {name: image})
        present = float(answers["present"].get("noul", 0.0))
        b = answers.get("box") or {}
        conf = b.get("confidence")
        conf = float(conf) if conf is not None else present
        bx = Box.from_answer(b["box"]) if b.get("box") else None
        return bx, present, conf, answers

    # ---- the ladder ---------------------------------------------------------------------
    async def ground(
        self,
        step: Step,
        obs: Observation,
        prior: dict[str, Any] | None = None,
        prior_mapping: dict[str, Element] | None = None,
        slot: int = 1,
        lookahead_key: tuple[str, float] | None = None,
        text_only: bool = False,
        full_fallback: bool = True,
    ) -> tuple[Grounded | None, GroundInfo]:
        """Climb the ladder. ``text_only`` stops after the text rungs (used by find and
        conditional steps, which handle "not found" themselves)."""
        info = GroundInfo()
        target = step.target or ""
        tree = obs.tree

        # Rung 1a: the fused call's pick for this step (same observation).
        if prior and prior_mapping is not None:
            el, conf = pick_from_answer(prior.get(f"pick_{slot}"), prior_mapping, self.th.T_PICK)
            if el is not None:
                fresh = tree.find_key(el.key)
                if fresh is not None:
                    self._count(1)
                    info.rung, info.source, info.answers = 1, "fused", {"pick": prior.get(f"pick_{slot}")}
                    return Grounded("element", fresh.index, fresh.key, describe(fresh, tree), conf, 1), info
            info.top = top_candidates(prior.get(f"pick_{slot}"), prior_mapping, tree)

        # Rung 1b: a confident lookahead pick whose target is still on screen.
        if lookahead_key:
            key, conf = lookahead_key
            fresh = tree.find_key(key)
            if fresh is not None and conf >= self.th.T_PICK:
                self._count(1)
                info.rung, info.source = 1, "lookahead"
                return Grounded("element", fresh.index, fresh.key, describe(fresh, tree), conf, 1), info

        # Rung 1c: fresh text Choice.
        el, conf, ans, mapping = await self.ask_pick(target, obs)
        info.answers["pick_text"] = ans.get("pick")
        if el is not None:
            self._count(1)
            info.rung, info.source = 1, "text"
            return Grounded("element", el.index, el.key, describe(el, tree), conf, 1), info
        info.top = top_candidates(ans.get("pick"), mapping, tree) or info.top

        # Rung 1d: the observation covered only the area around the viewport; ask again over the
        # whole window before paying for images.
        if obs.partial and full_fallback:
            full_obs = await self.backend.observe_ax(scope="full")
            el, conf, ans, mapping = await self.ask_pick(target, full_obs)
            info.answers["pick_text_full"] = ans.get("pick")
            info.obs = full_obs
            obs, tree = full_obs, full_obs.tree
            if el is not None:
                self._count(1)
                info.rung, info.source = 1, "text_full"
                return Grounded("element", el.index, el.key, describe(el, tree), conf, 1), info
            info.top = top_candidates(ans.get("pick"), mapping, tree) or info.top

        if text_only or not self.eyes.supports_images:
            info.rung = 1
            return None, info

        # Rung 2: image Choice.
        full = await self.backend.observe_full()
        obs, tree = full, full.tree  # indices are renumbered by every read: ground on this one
        info.obs = full
        shot = full.screenshot
        if not shot:
            return None, info
        el, conf, ans, mapping = await self.ask_pick(target, full, image=shot)
        info.answers["pick_image"] = ans.get("pick")
        if el is not None:
            self._count(2)
            info.rung, info.source = 2, "image"
            return Grounded("element", el.index, el.key, describe(el, tree), conf, 2), info
        info.top = top_candidates(ans.get("pick"), mapping, tree) or info.top

        if self.runlog:
            info.screenshot_path = self.runlog.save_image("ground", downscale(shot))

        if not self.eyes.supports_box:
            return None, info

        # Rung 3: Box on the window, with presence.
        img = load(shot)
        w, h = img.size
        bx, present, bconf, ans = await self.ask_box(target, shot, "screenshot", Q.BOX_WINDOW)
        info.answers["box_window"] = ans
        if bx is None or present < self.th.T_PRESENT:
            return None, info
        if bconf >= self.th.T_BOX and bx.area() < 0.25:
            self._count(3)
            x, y = bx.center_px(w, h)
            info.rung, info.source = 3, "box"
            return Grounded("point", None, None, f"point ({x},{y}) for {target!r}", bconf, 3, xy=(x, y)), info

        # Rung 4: Box on a padded crop at full resolution.
        crop = padded_crop(img, bx)
        cb, cpresent, cconf, ans2 = await self.ask_box(target, to_jpeg(crop.image, 90), "crop", Q.BOX_CROP)
        info.answers["box_crop"] = ans2
        if cb is not None and cpresent >= self.th.T_PRESENT and cconf >= self.th.T_BOX:
            cx, cy = cb.center_px(crop.image.width, crop.image.height)
            x, y = crop.to_original(cx, cy)
            self._count(4)
            info.rung, info.source = 4, "crop"
            # Rung 5 would snap to an element here if bounds were available.
            return Grounded("point", None, None, f"point ({x},{y}) for {target!r}", cconf, 4, xy=(x, y)), info

        # Rung 6: hand back to the planner.
        self._count(6)
        info.rung = 6
        return None, info

    async def confirm_commit(self, step: Step, obs: Observation) -> tuple[Grounded | None, GroundInfo]:
        """Shuffled double check for commit targets. Both answers must agree and clear T_COMMIT."""
        info = GroundInfo()
        target = step.target or ""
        for rung, image in ((1, None), (2, "shot")):
            shot = None
            if image:
                full = await self.backend.observe_full()
                obs = full
                shot = full.screenshot
            a_el, a_conf, a_ans, _ = await self.ask_pick(target, obs, image=shot, shuffle=False)
            b_el, b_conf, b_ans, _ = await self.ask_pick(target, obs, image=shot, shuffle=True)
            info.answers[f"commit_rung{rung}"] = {"a": a_ans.get("pick"), "b": b_ans.get("pick")}
            if a_el is not None and b_el is not None and a_el.key == b_el.key and min(a_conf, b_conf) >= self.th.T_COMMIT:
                self._count(rung)
                info.rung = rung
                g = Grounded("element", a_el.index, a_el.key, describe(a_el, obs.tree), a_conf, rung)
                g.shuffled_confidence = b_conf
                return g, info
            if shot and self.runlog:
                info.screenshot_path = self.runlog.save_image("commit", downscale(shot))
        info.rung = 6
        return None, info
