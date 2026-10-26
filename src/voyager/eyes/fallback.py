"""Jev answers every question. Claude answers a call only when Jev fails it (after Jev's own
retries), and the next call goes back to Jev."""
from __future__ import annotations

import logging
from typing import Any

from .base import Answer, Eyes, Images, Question

log = logging.getLogger("voyager.eyes")


class JevWithFallback(Eyes):
    name = "jev"

    def __init__(self, jev: Eyes, fallback: Eyes | None = None):
        super().__init__()
        self.jev = jev
        self.fallback = fallback
        self.fallbacks = 0
        self.supports_images = jev.supports_images
        self.supports_box = jev.supports_box

    async def warm(self) -> None:
        await self.jev.warm()

    async def close(self) -> None:
        await self.jev.close()
        if self.fallback:
            await self.fallback.close()

    async def _ask(self, state: Any, questions: dict[str, Question], images: Images | None) -> dict[str, Answer]:
        try:
            answers = await self.jev._ask(state, questions, images)
            self.provider = self.jev.name
            return answers
        except Exception as e:
            if self.fallback is None:
                raise
            log.warning("Jev failed (%s); answering this call with Claude", e)
            self.fallbacks += 1
            self.provider = self.fallback.name
            return await self.fallback._ask(state, questions, images)
