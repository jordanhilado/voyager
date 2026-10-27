"""Live end-to-end run of the Windows backend against scripts/win_fixture.py, using the Operator directly.

Starts the fixture window, then: types a name, presses Greet, ticks a check box, finds an item far
down a list (scrolling it), and checks that typing into the password box is refused. Every result is
checked in code against the window's status line. Usage (from a terminal in your Windows session):

    python scripts/e2e_windows.py            # Jev eyes (needs TYPESAFE_API_KEY)
    python scripts/e2e_windows.py --local    # offline heuristic eyes, no API key (smoke run only)
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from voyager.config import load_settings
from voyager.operator import Operator

logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(levelname)s %(message)s")
BRIEF = {"goal": "exercise the test form", "allowed_apps": ["Voyager Test Form"], "may_share": ["Ada"], "site_check": "lenient"}


def status(tree) -> str:
    return next((e.name for e in tree.elements if e.name.startswith("Status:")), "")


async def main() -> int:
    fixture = subprocess.Popen([sys.executable, str(Path(__file__).with_name("win_fixture.py"))])
    eyes = None
    if "--local" in sys.argv:
        from voyager.eyes.local import LocalEyes

        eyes = LocalEyes()
    op = Operator(load_settings(), eyes=eyes)
    await op.start()
    checks: list[tuple[str, bool, str]] = []
    try:
        time.sleep(1.0)
        b = await op.bind("the Voyager Test Form window", brief_inline=BRIEF)
        print("bind:", json.dumps({k: b[k] for k in ("bound", "title", "interactive_elements")}))

        r = await op.run_plan({"steps": [
            {"do": "type", "target": "the 'Your name' text field", "text": "Ada"},
            {"do": "click", "target": "the 'Greet' button", "expect": "the status line says 'Status: hello Ada'"},
        ]})
        obs = await op.backend.observe_ax()
        checks.append(("type + click", r["status"] == "done" and status(obs.tree) == "Status: hello Ada", f"{r['status']}: {status(obs.tree)!r} {r.get('reason', '')}"))

        r = await op.run_plan({"steps": [{"do": "click", "target": "the 'Subscribe to newsletter' check box",
                                          "expect": "the status line says 'Status: subscribed'"}]})
        obs = await op.backend.observe_ax()
        box = next((e for e in obs.tree.elements if e.name == "Subscribe to newsletter"), None)
        checks.append(("check box", r["status"] == "done" and box is not None and "checked" in box.flags, f"{r['status']}: {box.flags if box else None}"))

        r = await op.run_plan({"steps": [{"do": "find", "target": "the list item 'Fruit item 35'", "in": "the 'Fruits' list",
                                          "expect": "the status line says 'Status: picked Fruit item 35'"}]})
        obs = await op.backend.observe_ax()
        checks.append(("find in a list", r["status"] == "done" and status(obs.tree) == "Status: picked Fruit item 35", f"{r['status']}: {status(obs.tree)!r} {r.get('reason', '')}"))

        r = await op.run_plan({"steps": [{"do": "type", "target": "the 'Password' text field", "text": "hunter2"}]})
        checks.append(("password refused", r["status"] == "failed" and "password" in r.get("reason", ""), f"{r['status']}: {r.get('reason', '')}"))

        shot = await op.backend.screenshot()
        checks.append(("screenshot", len(shot) > 1000, f"{len(shot)} bytes"))
    finally:
        await op.stop()
        fixture.terminate()
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else '!!'}] {name}: {detail}")
    ok = all(c[1] for c in checks)
    print("E2E", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
