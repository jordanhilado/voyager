"""Drive a running jevd (`voyager serve`) through MCP over streamable HTTP, like Claude Code does.

    voyager serve &            # with VOYAGER_ALLOWED_APPS=Calculator for the demo
    python scripts/e2e_mcp_client.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _text(res) -> str:
    return "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")


async def main() -> int:
    base = os.environ.get("JEVD_URL", "http://127.0.0.1:8765")
    print("health:", httpx.get(f"{base}/health", timeout=5).json())
    async with streamable_http_client(f"{base}/mcp") as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            assert {"cu_bind", "cu_run_plan", "cu_commit", "cu_wait_until", "cu_read", "cu_observe", "cu_stop"} <= set(names)
            r = await s.call_tool("cu_bind", {"target": "the Calculator app"})
            print("cu_bind:", _text(r)[:300])
            plan = {"steps": [
                {"do": "click", "target": "the 'All Clear' button", "expect": "the display shows '0'"},
                {"do": "click", "target": "the '7' button", "expect": "the display shows '7'"},
                {"do": "click", "target": "the 'Multiply' button", "expect": "the display shows 7 followed by a multiplication sign"},
                {"do": "click", "target": "the '6' button", "expect": "the display shows the expression 7×6"},
                {"do": "click", "target": "the 'Equals' button", "expect": "the display shows '42'"},
            ]}
            r = await s.call_tool("cu_run_plan", {"plan": plan})
            out = json.loads(_text(r))
            print("cu_run_plan:", json.dumps({k: v for k, v in out.items() if k in ("status", "step_index", "steps_completed", "reason")}))
            r = await s.call_tool("cu_read", {"region": "the calculator display"})
            print("cu_read:", _text(r)[:300])
            r = await s.call_tool("cu_observe", {})
            kinds = [getattr(c, "type", "?") for c in r.content]
            print("cu_observe content types:", kinds)
            r = await s.call_tool("cu_wait_until", {"statement": "the display shows '42'", "timeout_s": 5})
            print("cu_wait_until:", _text(r)[:200])
            r = await s.call_tool("cu_commit", {"summary": "nothing pending"})
            print("cu_commit (expected refusal):", _text(r)[:200])
            r = await s.call_tool("cu_stop", {})
            print("cu_stop:", _text(r)[:100])
            ok = out.get("status") == "done" and "image" in kinds
            print("MCP E2E", "PASS" if ok else "FAIL")
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
