"""Live end-to-end smoke run on macOS, using the Operator directly.

Drives macOS Calculator: AC, 2, +, 3, = and checks the display reads 5 (checked in code, not by the
eyes). Harmless and stateless. Usage:
    VOYAGER_ALLOWED_APPS=Calculator python scripts/e2e_calculator.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from voyager.config import load_settings
from voyager.operator import Operator

logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main() -> int:
    op = Operator(load_settings())
    t0 = time.perf_counter()
    await op.start()
    print(f"started in {(time.perf_counter() - t0) * 1000:.0f} ms; eyes={op.eyes.name}")
    try:
        b = await op.bind("the Calculator app")
        print("bind:", json.dumps(b))
        plan = {
            "steps": [
                {"do": "click", "target": "the 'All Clear' button", "expect": "the display shows '0'"},
                {"do": "click", "target": "the '2' button", "expect": "the display shows '2'"},
                {"do": "click", "target": "the 'Add' button", "expect": "the display shows 2 followed by a plus sign"},
                {"do": "click", "target": "the '3' button", "expect": "the display shows the expression 2+3"},
                {"do": "click", "target": "the 'Equals' button", "expect": "the display shows '5'"},
            ],
            "stop_if": ["the app shows an 'error' dialog"],
        }
        t1 = time.perf_counter()
        r = await op.run_plan(plan)
        plan_ms = (time.perf_counter() - t1) * 1000
        print("plan result:", json.dumps({k: v for k, v in r.items() if k not in ("page_text", "eyes")}, indent=1, default=str)[:3000])
        obs = await op.backend.observe_ax()
        display = next((e.name for e in obs.tree.elements if e.role == "text" and e.parent is not None and obs.tree.get(e.parent).id == "StandardInputView"), None)
        display = (display or "").replace("‎", "").strip()
        print(f"display reads: {display!r}   plan took {plan_ms:.0f} ms for {r.get('steps_completed')} steps")
        for st in r["timings"]["steps"]:
            print("  step", st["step"], st["do"], {k: st[k] for k in ("ground_ms", "act_ms", "settle_ms", "eyes_ms", "total_ms", "rung", "speculation_hit")})
        rd = await op.read("the calculator display")
        print("read:", json.dumps(rd)[:300])
        ok = r.get("status") == "done" and display == "5"
        print("E2E", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await op.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
