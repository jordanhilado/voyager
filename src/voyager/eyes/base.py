"""The Eyes interface and question builders.

Answers are normalized to the TypeSafe wire shape regardless of provider::

    choice -> {"type": "choice", "choice": str, "confidence": float, "probabilities": {name: p}}
    noul   -> {"type": "noul", "noul": float}
    box    -> {"type": "box", "box": {"top","bottom","left","right"}, "confidence": float|None}
    score  -> {"type": "score", "score": float, "confidence": float, "probabilities": {...}}
"""
from __future__ import annotations

import abc
import time
from typing import Any, Callable

Question = dict[str, Any]
Answer = dict[str, Any]
Images = dict[str, bytes]  # name -> JPEG/PNG bytes


def choice(instructions: str, criteria: dict[str, str]) -> Question:
    if not 1 <= len(criteria) <= 255:
        raise ValueError("choice needs 1..255 options")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def noul(instructions: str) -> Question:
    return {"type": "noul", "instructions": instructions}


def box(instructions: str) -> Question:
    return {"type": "box", "instructions": instructions}


def score(instructions: str, levels: list[str]) -> Question:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


class Eyes(abc.ABC):
    name = "eyes"
    supports_images = True
    supports_box = True

    def __init__(self) -> None:
        self.last_latency_ms: float = 0.0
        self.calls: int = 0
        self.provider: str = self.name  # who answered the last call
        self.on_call: Callable[[dict], None] | None = None  # run-log hook

    async def warm(self) -> None:  # pragma: no cover - network
        return None

    async def close(self) -> None:
        return None

    async def ask(self, state: Any, questions: dict[str, Question], images: Images | None = None) -> dict[str, Answer]:
        t0 = time.perf_counter()
        answers = await self._ask(state, questions, images)
        self.last_latency_ms = (time.perf_counter() - t0) * 1000
        self.calls += 1
        if self.on_call:
            self.on_call(
                {
                    "provider": self.provider,
                    "ms": round(self.last_latency_ms, 1),
                    "n_questions": len(questions),
                    "images": sorted(images.keys()) if images else [],
                    "questions": {k: {"type": q["type"], "instructions": q.get("instructions")} for k, q in questions.items()},
                    "answers": answers,
                }
            )
        return answers

    @abc.abstractmethod
    async def _ask(self, state: Any, questions: dict[str, Question], images: Images | None) -> dict[str, Answer]: ...


class FakeEyes(Eyes):
    """Scripted eyes for tests: ``script`` maps question id (or a callable) to an answer."""

    name = "fake"

    def __init__(self, responder: Callable[[Any, dict[str, Question], Images | None], dict[str, Answer]]):
        super().__init__()
        self.responder = responder
        self.log: list[tuple[Any, dict[str, Question], Images | None]] = []

    async def _ask(self, state, questions, images):
        self.log.append((state, questions, images))
        return self.responder(state, questions, images)
