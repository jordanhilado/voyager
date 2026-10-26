"""TypeSafe System One client (Jev). Warm HTTP/2 connection, text and multipart image requests,
exponential backoff on 429/529. Contract: https://docs.typesafe.ai/api.md
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from typing import Any

import httpx

from ..config import Settings
from ..images import fit, load, to_jpeg
from .base import Answer, Eyes, Images, Question

log = logging.getLogger("voyager.eyes.jev")
RETRY_STATUSES = {429, 529, 502, 503}


class JevEyes(Eyes):
    name = "jev"

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        super().__init__()
        if not settings.typesafe_api_key and http is None:
            raise RuntimeError("TYPESAFE_API_KEY is not set; Voyager needs it for Jev eyes")
        self.api = settings.typesafe_api
        self.model = settings.jev_model
        self.http = http or httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"Authorization": f"Bearer {settings.typesafe_api_key}", "User-Agent": "voyager-cu/0.1"},
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=300),
        )
        self.supports_box = True

    async def warm(self) -> None:
        try:
            r = await self.http.get(self.api.rsplit("/", 1)[0] + "/models")
            r.raise_for_status()
            names = [m.get("name") for m in r.json().get("models", [])]
            log.info("TypeSafe reachable; models: %s", names)
        except Exception as e:  # pragma: no cover - network
            log.warning("TypeSafe warm-up failed: %s", e)

    async def close(self) -> None:
        await self.http.aclose()

    async def _ask(self, state: Any, questions: dict[str, Question], images: Images | None) -> dict[str, Answer]:
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": questions}
        if not images:
            return await self._post(json_body=body)
        if not isinstance(body["state"], dict):
            body["state"] = {"text": body["state"]}
        assets: dict[str, Any] = {}
        parts: dict[str, tuple[str, bytes, str]] = {}
        for name, data in images.items():
            ref = f"img-{uuid.uuid4()}"
            part = f"part-{name}"
            assets[ref] = {"type": "image", "part": part}
            parts[part] = (f"{name}.jpg", to_jpeg(fit(load(data))), "image/jpeg")
            body["state"][name] = ref
        body["assets"] = assets
        files = {"request": (None, json.dumps(body), "application/json"), **parts}
        return await self._post(files=files, headers={"TypeSafe-Danger-Zone-Images": "true"})

    async def _post(self, json_body=None, files=None, headers=None) -> dict[str, Answer]:
        delay = 0.2
        last: Exception | None = None
        for attempt in range(3):
            try:
                if files is not None:
                    r = await self.http.post(self.api, files=files, headers=headers)
                else:
                    r = await self.http.post(self.api, json=json_body, headers=headers)
                if r.status_code in RETRY_STATUSES:
                    ra = r.headers.get("retry-after")
                    wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                    log.warning("TypeSafe %s, retrying in %.2fs", r.status_code, wait)
                    await asyncio.sleep(wait + random.uniform(0, 0.05))
                    delay *= 2
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"TypeSafe {r.status_code}: {r.text[:500]}")
                return r.json()["answers"]
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError(f"TypeSafe request failed after retries: {last}")
