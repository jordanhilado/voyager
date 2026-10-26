"""Event-based settling: the page counts as settled when its AX hash stops changing.

A native read returns in tens of milliseconds, so the hash must stay unchanged for
``backend.settle_quiet_s`` (a page that is about to react to a click must not be called settled
just because two fast reads agreed).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .backend.base import Backend, Observation


@dataclass
class Settled:
    obs: Observation
    samples: int
    ms: float
    stable: bool


async def settle(backend: Backend, first: Observation, cap_s: float = 2.5, first_delay_s: float = 0.0) -> Settled:
    t0 = time.perf_counter()
    quiet = getattr(backend, "settle_quiet_s", 0.0)
    poll = getattr(backend, "settle_poll_s", 0.12)
    prev = first
    since = time.perf_counter()  # when prev's hash was first seen
    samples = 1
    if first_delay_s:
        await asyncio.sleep(first_delay_s)
    while True:
        cur = await backend.observe_ax()
        samples += 1
        now = time.perf_counter()
        if cur.tree.hash == prev.tree.hash:
            if now - since >= quiet:
                return Settled(cur, samples, (now - t0) * 1000, True)
        else:
            since = now
        prev = cur
        if now - t0 > cap_s:
            return Settled(cur, samples, (now - t0) * 1000, False)
        await asyncio.sleep(poll)
