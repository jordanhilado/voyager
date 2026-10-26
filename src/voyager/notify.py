"""ntfy push notifications for approvals and stops."""
from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger("voyager.notify")


async def push(settings: Settings, title: str, message: str, priority: str = "high", tags: str = "robot") -> bool:
    if not settings.ntfy_topic:
        return False
    url = f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(url, content=message.encode(), headers={"Title": title, "Priority": priority, "Tags": tags})
            r.raise_for_status()
            return True
    except Exception as e:  # pragma: no cover - network
        log.warning("ntfy push failed: %s", e)
        return False
