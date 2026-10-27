"""Live end-to-end smoke run on macOS or Windows, using the Operator directly.

Drives Calculator: clear, 2, +, 3, = and checks the display reads 5 (checked in code, not by the
eyes). Harmless and stateless. Usage:
    VOYAGER_ALLOWED_APPS=Calculator python scripts/e2e_calculator.py
On Windows (PowerShell): $env:VOYAGER_ALLOWED_APPS="Calculator"; python scripts/e2e_calculator.py
Pass --local to use the offline heuristic eyes instead of Jev (no API key; for smoke runs only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from voyager.config import IS_WINDOWS, load_settings
from voyager.operator import Operator

logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(levelname)s %(message)s")

# Button labels differ: macOS Calculator says "All Clear" / "Add", Windows Calculator "Clear" / "Plus" / "Two".
if IS_WINDOWS:
    STEPS = [("the 'Clear' button", "the display shows '0'"), ("the 'Two' button", "the display shows '2'"),
             ("the 'Plus' button", "the expression shows 2 +"), ("the 'Three' button", "the display shows '3'"),
             ("the 'Equals' button", "the display shows '5'")]
else:
    STEPS = [("the 'All Clear' button", "the display shows '0'"), ("the '2' button", "the display shows '2'"),
             ("the 'Add' button", "the display shows 2 followed by a plus sign"), ("the '3' button", "the display shows the expression 2+3"),
             ("the 'Equals' button", "the display shows '5'")]


def display_of(tree) -> str:
    if IS_WINDOWS:  # Text "Display is 5" with AutomationId CalculatorResults
        el = next((e for e in tree.elements if e.id == "CalculatorResults"), None)
        return (el.name.rsplit(" ", 1)[-1] if el and el.name else "").strip()
    el = next((e for e in tree.elements if e.role == "text" and e.parent is not None and tree.get(e.parent).id == "StandardInputView"), None)
    return ((el.name if el else "") or "").replace("‎", "").strip()


async def main() -> int:
    eyes = None
    if "--local" in sys.argv:
        from voyager.eyes.local import LocalEyes

        eyes = LocalEyes()
    op = Operator(load_settings(), eyes=eyes)
    t0 = time.perf_counter()
    await op.start()
    print(f"started in {(time.perf_counter() - t0) * 1000:.0f} ms; eyes={op.eyes.name}")
    try:
        b = await op.bind("the Calculator app")
        print("bind:", json.dumps(b))
        plan = {
            "steps": [{"do": "click", "target": t, "expect": x} for t, x in STEPS],
            "stop_if": ["the app shows an 'error' dialog"],
        }
        t1 = time.perf_counter()
        r = await op.run_plan(plan)
        plan_ms = (time.perf_counter() - t1) * 1000
        print("plan result:", json.dumps({k: v for k, v in r.items() if k not in ("page_text", "eyes")}, indent=1, default=str)[:3000])
        obs = await op.backend.observe_ax()
        display = display_of(obs.tree)
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
