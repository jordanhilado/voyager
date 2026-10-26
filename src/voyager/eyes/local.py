"""Local heuristic eyes: no model, no network. For tests, demos and offline smoke runs ONLY.

Choice: token overlap between the question's target words and each option's description.
Noul: keyword overlap between the statement and the state text. Box: unsupported.
Tests inject it directly; the daemon never uses it. It cannot read meaning.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import Answer, Eyes, Images, Question

_WORD = re.compile(r"[a-z0-9']+")
_STOP = {"the", "a", "an", "of", "in", "on", "at", "to", "is", "it", "or", "and", "for", "with", "which", "listed", "element", "options", "describe", "one", "ui", "each", "top", "bottom", "order", "page", "answer", "none", "if", "no", "this", "true", "described", "state", "right", "now", "statement", "about", "present", "are", "that", "by", "its", "see"}


def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in _STOP and len(w) > 1}


def _quoted(s: str) -> list[str]:
    return [q.lower() for q in re.findall(r"[\"'“‘]([^\"'”’]{1,80})[\"'”’]", s)]


class LocalEyes(Eyes):
    name = "local"
    supports_images = False
    supports_box = False

    def __init__(self, settings=None):
        super().__init__()

    async def _ask(self, state: Any, questions: dict[str, Question], images: Images | None) -> dict[str, Answer]:
        text = json.dumps(state, ensure_ascii=False).lower() if not isinstance(state, str) else state.lower()
        out: dict[str, Answer] = {}
        for qid, q in questions.items():
            t = q["type"]
            if t == "choice":
                out[qid] = self._choice(q)
            elif t == "noul":
                out[qid] = self._noul(q, text, qid)
            elif t == "score":
                n = len(q["criteria"])
                out[qid] = {"type": "score", "score": 0.0, "confidence": 0.3, "probabilities": {str(i): 1 / n for i in range(n)}, "legend": {str(i): c for i, c in enumerate(q["criteria"])}}
            else:
                out[qid] = {"type": "box", "box": {"top": 0.4, "bottom": 0.6, "left": 0.4, "right": 0.6}, "confidence": 0.0}
        return out

    def _choice(self, q: Question) -> Answer:
        instr = q["instructions"]
        quoted = _quoted(instr)
        want = _tokens(instr)
        scores: dict[str, float] = {}
        for name, desc in q["criteria"].items():
            if name == "none":
                continue
            d = str(desc).lower()
            s = 0.0
            for qq in quoted:
                if qq in d:
                    s += 3.0
            s += len(want & _tokens(d)) * 0.5
            if "blocker" in instr.lower() or "best describes" in instr.lower():
                s = 0.0
            scores[name] = s
        if "best describes" in instr.lower():
            probs = {n: (0.9 if n == "none" else 0.1 / max(len(q["criteria"]) - 1, 1)) for n in q["criteria"]}
            return {"type": "choice", "choice": "none", "confidence": 0.9, "probabilities": probs}
        if not scores or max(scores.values()) <= 0:
            probs = {n: (0.8 if n == "none" else 0.2 / max(len(scores), 1)) for n in q["criteria"]}
            return {"type": "choice", "choice": "none", "confidence": 0.8, "probabilities": probs}
        total = sum(scores.values()) + 0.01
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        ranked = sorted(scores.values(), reverse=True)
        margin = (ranked[0] - (ranked[1] if len(ranked) > 1 else 0)) / ranked[0]
        conf = min(0.99, 0.5 + 0.5 * margin) if ranked[0] >= 3 else min(0.7, 0.3 + 0.4 * margin)
        if quoted and sum(1 for s in scores.values() if s >= 3) == 1:
            conf = max(conf, 0.97)  # exactly one option carries the quoted label
        probs = {n: (s / total) for n, s in scores.items()}
        probs["none"] = 0.01
        return {"type": "choice", "choice": best, "confidence": conf, "probabilities": probs}

    def _noul(self, q: Question, text: str, qid: str) -> Answer:
        instr = q["instructions"].lower()
        if qid.startswith(("pause_", "stop_")) or "password" in instr or "never_share" in instr or "contain" in instr:
            return {"type": "noul", "noul": 0.05}
        want = _tokens(instr)
        if not want:
            return {"type": "noul", "noul": 0.5}
        have = _tokens(text)
        quoted = _quoted(q["instructions"])
        hit = len(want & have) / len(want)
        if quoted and all(qq in text for qq in quoted):
            hit = max(hit, 0.95)
        return {"type": "noul", "noul": round(0.2 + 0.75 * hit, 3)}
