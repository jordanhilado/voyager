"""Standalone planner: the Anthropic API drives the same cu_* tools, with approvals in the terminal.

    voyager run "Ask the store's support chat about shipping" --brief shop-support

Claude Code remains the recommended front door (``voyager serve`` + claude/.mcp.json); this
planner is for people who want a single command and for CI-style use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Awaitable, Callable

from .config import Settings
from .operator import Operator
from .plan import Plan
from .policy import PolicyViolation
from .prompts import PLANNER_RULES

log = logging.getLogger("voyager.planner")

TOOLS = [
    {"name": "cu_bind", "description": "Bind the window/app to drive and load the brief (named or inline).", "input_schema": {"type": "object", "properties": {"target": {"type": "string"}, "brief": {"type": "string"}, "brief_inline": {"type": "object"}, "plan": {"type": "object", "description": "optional plan to run right after binding"}}, "required": ["target"]}},
    {"name": "cu_open", "description": "Open a URL in the desktop browser and bind that window (inline brief optional).", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "brief": {"type": "string"}, "brief_inline": {"type": "object"}, "plan": {"type": "object", "description": "optional plan to run right after the page loads"}}, "required": ["url"]}},
    {"name": "cu_run_plan", "description": "Run a batched plan. Returns done | ready_to_commit | stopped | failed | needs_help.", "input_schema": {"type": "object", "properties": {"plan": Plan.model_json_schema()}, "required": ["plan"]}},
    {"name": "cu_commit", "description": "Perform the pending commit after the user's approval.", "input_schema": {"type": "object", "properties": {"step_ref": {"type": "integer"}, "summary": {"type": "string"}}, "required": ["summary"]}},
    {"name": "cu_wait_until", "description": "Wait until a statement about the page is true; returns the new text.", "input_schema": {"type": "object", "properties": {"statement": {"type": "string"}, "timeout_s": {"type": "number"}}, "required": ["statement"]}},
    {"name": "cu_read", "description": "Read the visible text of a region.", "input_schema": {"type": "object", "properties": {"region": {"type": "string"}}, "required": ["region"]}},
    {"name": "cu_observe", "description": "Title, URL, element summary and a screenshot of the bound window.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "cu_stop", "description": "Abort the run.", "input_schema": {"type": "object", "properties": {}}},
]

Approver = Callable[[str], Awaitable[bool]]


async def terminal_approver(summary: str) -> bool:
    print(f"\n=== APPROVAL NEEDED ===\n{summary}\nApprove this send/submit? [y/N] ", end="", flush=True)
    ans = await asyncio.to_thread(sys.stdin.readline)
    return ans.strip().lower() in ("y", "yes")


async def run_goal(settings: Settings, goal: str, brief: str | None, approver: Approver = terminal_approver, max_turns: int = 40, operator: Operator | None = None) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)
    op = operator or Operator(settings)
    own = operator is None
    if own:
        await op.start()
    try:
        system = PLANNER_RULES + (f"\nThe brief to use is {brief!r}; pass it to cu_bind." if brief else "")
        messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
        final = ""
        for _ in range(max_turns):
            resp = await client.messages.create(model=settings.planner_model, max_tokens=4096, system=system, tools=TOOLS, messages=messages)
            messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    print(f"\n[planner] {b.text.strip()}")
                    final = b.text
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                print(f"[tool] {tu.name} {json.dumps(tu.input)[:300]}")
                content = await _dispatch(op, tu.name, tu.input, approver)
                print(f"[result] {str(content)[:400]}")
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content if isinstance(content, list) else json.dumps(content, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
        return final
    finally:
        if own:
            await op.stop()


async def _dispatch(op: Operator, name: str, args: dict, approver: Approver):
    try:
        if name == "cu_bind":
            return await op.bind(args["target"], args.get("brief"), args.get("brief_inline"), args.get("plan"))
        if name == "cu_open":
            return await op.open(args["url"], args.get("brief"), args.get("brief_inline"), plan=args.get("plan"))
        if name == "cu_run_plan":
            return await op.run_plan(args["plan"])
        if name == "cu_commit":
            summary = args.get("summary", "")
            approved = True
            if op.brief.autonomy == "approve_each_send":
                approved = await approver(summary)
                if not approved:
                    return {"status": "failed", "reason": "the user declined the commit"}
            return await op.commit(args.get("step_ref"), summary, approved=approved)
        if name == "cu_wait_until":
            return await op.wait_until(args["statement"], float(args.get("timeout_s", 60)))
        if name == "cu_read":
            return await op.read(args["region"])
        if name == "cu_observe":
            info, img = await op.observe()
            content: list[dict] = [{"type": "text", "text": json.dumps(info, ensure_ascii=False)}]
            if img:
                import base64

                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(img).decode()}})
            return content
        if name == "cu_stop":
            return await op.stop_run()
        return {"status": "failed", "reason": f"unknown tool {name}"}
    except PolicyViolation as e:
        return {"status": "failed", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return {"status": "failed", "reason": f"{type(e).__name__}: {e}"}
