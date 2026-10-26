"""The MCP server (streamable HTTP on 127.0.0.1:8765/mcp, or stdio) exposing the narrow cu_* tools."""
from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings, load_settings
from .operator import Operator
from .plan import Plan
from .policy import PolicyViolation
from .prompts import PLANNER_RULES

log = logging.getLogger("voyager.server")

_operator: Operator | None = None


def get_operator() -> Operator:
    if _operator is None:
        raise RuntimeError("operator not started")
    return _operator


def build_server(settings: Settings, operator: Operator | None = None) -> MCPServer:
    @contextlib.asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        global _operator
        op = operator or Operator(settings)
        await op.start()
        _operator = op
        hk = None
        if settings.hotkey:
            from .hotkey import start_hotkey

            hk = start_hotkey(op.kill)
        try:
            yield {"operator": op}
        finally:
            if hk:
                hk.stop()
            await op.stop()
            _operator = None

    mcp = MCPServer("voyager", instructions=PLANNER_RULES, lifespan=lifespan)

    def _err(e: Exception) -> dict:
        return {"status": "failed", "reason": str(e)}

    @mcp.tool(name="cu_bind", description="Bind the window or app the run will use, and load a brief: either a named YAML brief or an inline brief dict {goal, allowed_sites, allowed_apps, may_share, never_share, must_pause_for, autonomy}. Checks the allowlists. Returns a page digest (elements grouped by panel, visible text). Optional 'plan' (same schema as cu_run_plan) runs right after binding and comes back as plan_result. For a website, prefer cu_open. Example: cu_bind(target='the Calculator app', brief_inline={'goal': '...', 'allowed_apps': ['Calculator'], 'site_check': 'lenient'}).")
    async def cu_bind(target: str, brief: str | None = None, brief_inline: dict | None = None, plan: dict | None = None) -> dict:
        try:
            return await get_operator().bind(target, brief, brief_inline, plan)
        except PolicyViolation as e:
            return _err(e)

    @mcp.tool(name="cu_open", description="Open a URL in the desktop browser, wait for it to load, and bind that window. Takes an inline brief dict (goal, may_share, never_share, must_pause_for, autonomy); allowed_sites defaults to the URL's domain. Returns a page digest (elements grouped by panel, visible text) to plan from. When the steps are predictable from the request, pass 'plan' (same schema as cu_run_plan): it runs right after the page loads and comes back as plan_result, saving a turn. Use this for any task that starts on a website. Example: cu_open(url='https://www.messenger.com', brief_inline={...}, plan={'steps': [{'do': 'find', 'target': \"the chat named 'Dad'\", 'in': 'the chats list', 'expect': 'the chat with Dad is open'}, ...]}).")
    async def cu_open(url: str, brief: str | None = None, brief_inline: dict | None = None, browser: str | None = None, plan: dict | None = None) -> dict:
        try:
            return await get_operator().open(url, brief, brief_inline, browser, plan)
        except PolicyViolation as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            log.exception("cu_open failed")
            return {"status": "failed", "reason": f"{type(e).__name__}: {e}"}

    @mcp.tool(name="cu_run_plan", description="Run a batched plan through the fast loop. Returns done, ready_to_commit, stopped, failed or needs_help with the step index, eyes answers, timings, the page changes and a screenshot path when relevant. " + "Schema: " + json.dumps(Plan.model_json_schema())[:8000])
    async def cu_run_plan(plan: dict) -> dict:
        try:
            return await get_operator().run_plan(plan)
        except PolicyViolation as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            log.exception("run_plan failed")
            return {"status": "failed", "reason": f"{type(e).__name__}: {e}"}

    @mcp.tool(name="cu_commit", description="Perform the pending commit returned by ready_to_commit (Send, Submit, Confirm, Accept, Enter-to-send), after a shuffled double check of the target. 'summary' must state exactly what is sent or submitted. This is the only tool that can submit anything.")
    async def cu_commit(step_ref: int | None = None, summary: str = "") -> dict:
        try:
            return await get_operator().commit(step_ref, summary, approved=True)
        except PolicyViolation as e:
            return _err(e)

    @mcp.tool(name="cu_wait_until", description="Wait until a statement about the page becomes true (max 300 s per call; call again to keep waiting). Returns the new page text and changes, so no separate read is needed.")
    async def cu_wait_until(statement: str, timeout_s: float = 60) -> dict:
        try:
            return await get_operator().wait_until(statement, timeout_s)
        except PolicyViolation as e:
            return _err(e)

    @mcp.tool(name="cu_read", description="Return the visible text of a region (for example 'the chat transcript'), chosen by a text choice over containers. Falls back to the full visible text plus a screenshot path.")
    async def cu_read(region: str) -> dict:
        try:
            return await get_operator().read(region)
        except PolicyViolation as e:
            return _err(e)

    @mcp.tool(name="cu_observe", description="Title, URL, interactive-element summary and a downscaled screenshot of the bound window. For orientation and recovery only; before a bind it lists available targets.")
    async def cu_observe() -> list:
        try:
            info, img = await get_operator().observe()
        except PolicyViolation as e:
            return [json.dumps(_err(e))]
        out: list = [json.dumps(info, ensure_ascii=False)]
        if img:
            out.append(Image(data=img, format="jpeg"))
        return out

    @mcp.tool(name="cu_stop", description="Abort the current run: trips the kill switch, clears any pending commit and resets the backend.")
    async def cu_stop() -> dict:
        return await get_operator().stop_run()

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        op = _operator
        return JSONResponse(op.health() if op else {"ok": False, "reason": "starting"})

    @mcp.custom_route("/stop", methods=["POST"])
    async def stop_route(_: Request) -> JSONResponse:
        op = _operator
        if not op:
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse(await op.stop_run())

    return mcp


def serve(settings: Settings | None = None, transport: str = "streamable-http") -> None:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp = build_server(settings)
    if transport == "stdio":
        mcp.run("stdio")
    else:
        import uvicorn

        log.info("jevd listening on http://%s:%d/mcp (health: /health)", settings.host, settings.port)
        app = mcp.streamable_http_app(host=settings.host)
        # Short graceful-shutdown window: open MCP streams from a planner must not keep a stopping
        # daemon alive (SIGTERM from `voyager down` would otherwise hang).
        uvicorn.run(app, host=settings.host, port=settings.port, timeout_graceful_shutdown=3, log_level=settings.log_level.lower())
