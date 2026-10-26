"""voyager / jevd command line."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from . import __version__
from .config import load_settings


def cmd_serve(args) -> None:
    from .server import serve

    s = load_settings()
    if args.port:
        s.port = args.port
    serve(s, transport=args.transport)


def cmd_run(args) -> None:
    from .planner import run_goal

    s = load_settings()
    if not (s.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        sys.exit("ANTHROPIC_API_KEY is required for `voyager run` (or use Claude Code with `voyager serve`)")
    asyncio.run(run_goal(s, args.goal, args.brief))


def cmd_stats(args) -> None:
    from .stats import compute, render

    s = load_settings()
    st = compute(s.runs_dir, args.runs)
    print(json.dumps(st, indent=1) if args.json else render(st))


def cmd_calibrate(args) -> None:
    from .calibrate import run
    from .questions import Thresholds

    s = load_settings()
    t, report = run(s.runs_dir, s.thresholds_path, Thresholds.load(s.thresholds_path))
    print(json.dumps({"thresholds": t.dump(), **report}, indent=1))


def cmd_tail(args) -> None:
    from .runlog import latest_run

    s = load_settings()
    run = latest_run(s.runs_dir)
    if not run:
        sys.exit("no runs yet")
    path = run / "events.jsonl"
    print(f"tailing {path}")
    with path.open(encoding="utf-8") as f:
        while True:
            line = f.readline()
            if not line:
                if not args.follow:
                    break
                time.sleep(0.3)
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = ev.pop("kind", "?")
            t = ev.pop("t", 0)
            short = {kk: (vv if not isinstance(vv, (dict, list)) else json.dumps(vv, ensure_ascii=False)[:160]) for kk, vv in ev.items()}
            print(f"{t:8.2f}s  {k:<16} " + "  ".join(f"{kk}={vv}" for kk, vv in short.items()))


def cmd_doctor(args) -> None:
    s = load_settings()
    ok = True

    def row(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else '!!'}] {label}{(': ' + detail) if detail else ''}")

    row("python", sys.version_info >= (3, 11), sys.version.split()[0])
    row("data dir", s.data_dir.exists(), str(s.data_dir))
    from .config import IS_MAC

    row("TYPESAFE_API_KEY (Jev eyes)", bool(s.typesafe_api_key), "" if s.typesafe_api_key else "required")
    has_anthropic = bool(s.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))
    row("ANTHROPIC_API_KEY", True, "set: Claude backs up Jev if a call fails" if has_anthropic else "not set (optional; no fallback if Jev fails)")
    row("ntfy topic", True, s.ntfy_topic or "(not set; approvals only prompt in the terminal/Claude Code)")
    try:
        import pynput  # noqa: F401

        row("hotkey (pynput)", True)
    except Exception:
        row("hotkey (pynput)", True, "not installed; optional (pip install voyager-cu[hotkey])")
    row("allowed apps", True, ", ".join(s.allowed_apps))
    if IS_MAC:
        try:
            from .backend.macnative import native_available
            from .backend.macwin import screen_recording_granted

            good, why = native_available()
            shots = screen_recording_granted()
        except Exception as e:  # noqa: BLE001
            good, why, shots = False, f"pyobjc unavailable ({e})", False
        row("accessibility", good, "native backend ready" if good else why)
        row("screenshots", shots, "native (ScreenCaptureKit)" if shots else
            "grant Screen Recording to the app that starts the daemon (System Settings > Privacy & Security > "
            "Screen & System Audio Recording); without it Voyager runs text-only")
    else:
        row("platform", False, "Voyager currently runs on macOS only; Windows support is pending")
    print("\nall good" if ok else "\nfix the items marked !! and run again")


def _health(s, timeout=2.0):
    import httpx

    try:
        r = httpx.get(f"http://{s.host}:{s.port}/health", timeout=timeout)
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def cmd_up(args) -> None:
    """Start jevd in the background if it is not already healthy. Idempotent, quiet, fast."""
    import subprocess

    s = load_settings()
    h = _health(s)
    if h and h.get("ok"):
        print(json.dumps({"status": "running", **h}))
        return
    log = s.log_path.open("a")
    kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "cwd": str(s.data_dir)}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "voyager.cli", "serve"], **kwargs)
    deadline = time.time() + float(args.timeout)
    while time.time() < deadline:
        h = _health(s)
        if h and h.get("ok"):
            print(json.dumps({"status": "started", "log": str(s.log_path), **h}))
            return
        time.sleep(0.4)
    sys.exit(f"jevd did not become healthy within {args.timeout}s; see {s.log_path}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cmd_down(args) -> None:
    """Stop the background jevd: SIGTERM, wait up to --timeout seconds, then SIGKILL. Clears the lock."""
    import signal

    s = load_settings()
    if not s.lock_path.exists():
        print("not running"); return
    try:
        pid = int(s.lock_path.read_text().strip())
    except ValueError:
        s.lock_path.unlink(missing_ok=True); print("stale lock removed"); return
    if not _alive(pid):
        s.lock_path.unlink(missing_ok=True); print(f"stale lock (pid {pid} gone) removed"); return
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + float(args.timeout)
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    if _alive(pid):
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        time.sleep(0.5)
        print(f"jevd pid {pid} killed after {args.timeout}s")
    else:
        print(f"jevd pid {pid} stopped")
    if s.lock_path.exists():
        try:
            if int(s.lock_path.read_text().strip() or 0) == pid:
                s.lock_path.unlink(missing_ok=True)
        except ValueError:
            s.lock_path.unlink(missing_ok=True)


def cmd_call(args) -> None:
    """Call one cu_* tool on the running daemon over MCP and print its JSON result.
    Fallback for Claude Code sessions where the voyager MCP server is not loaded."""
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    s = load_settings()
    payload = json.loads(args.json) if args.json else {}

    async def go():
        async with streamable_http_client(f"http://{s.host}:{s.port}/mcp") as (read, write, *_):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                res = await sess.call_tool(args.tool, payload)
                for c in res.content:
                    if getattr(c, "type", "") == "text":
                        print(c.text)
                    elif getattr(c, "type", "") == "image":
                        p = s.data_dir / "last-observe.jpg"
                        import base64

                        p.write_bytes(base64.b64decode(c.data)); print(json.dumps({"image": str(p)}))

    asyncio.run(go())


def cmd_stop(args) -> None:
    import httpx

    s = load_settings()
    try:
        r = httpx.post(f"http://{s.host}:{s.port}/stop", timeout=5)
        print(r.json())
    except Exception as e:  # noqa: BLE001
        sys.exit(f"could not reach jevd at {s.host}:{s.port}: {e}")


def cmd_health(args) -> None:
    import httpx

    s = load_settings()
    try:
        print(httpx.get(f"http://{s.host}:{s.port}/health", timeout=5).json())
    except Exception as e:  # noqa: BLE001
        sys.exit(f"could not reach jevd at {s.host}:{s.port}: {e}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="voyager", description="Fast, safe computer use: Claude plans, Jev verifies, the daemon acts.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the jevd MCP server (default: streamable HTTP on 127.0.0.1:8765)")
    sp.add_argument("--transport", choices=["streamable-http", "stdio"], default="streamable-http")
    sp.add_argument("--port", type=int)
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("run", help="drive a goal with the Anthropic API as planner (approvals in the terminal)")
    sp.add_argument("goal")
    sp.add_argument("--brief")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("stats", help="p50/p95 per stage and ladder rung usage across recent runs")
    sp.add_argument("--runs", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("calibrate", help="propose thresholds from run logs; writes thresholds.yaml")
    sp.set_defaults(fn=cmd_calibrate)

    sp = sub.add_parser("tail", help="stream the latest run log")
    sp.add_argument("-f", "--follow", action="store_true")
    sp.set_defaults(fn=cmd_tail)

    sp = sub.add_parser("doctor", help="check the installation")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("up", help="start jevd in the background if it is not running (idempotent)")
    sp.add_argument("--timeout", type=float, default=25)
    sp.set_defaults(fn=cmd_up)

    sp = sub.add_parser("down", help="terminate the background jevd")
    sp.add_argument("--timeout", type=float, default=10)
    sp.set_defaults(fn=cmd_down)

    sp = sub.add_parser("restart", help="down then up")
    sp.add_argument("--timeout", type=float, default=25)
    sp.set_defaults(fn=lambda a: (cmd_down(argparse.Namespace(timeout=10)), cmd_up(a)))

    sp = sub.add_parser("call", help="call a cu_* tool on the running jevd, e.g. voyager call cu_open '{\"url\": \"https://example.com\"}'")
    sp.add_argument("tool")
    sp.add_argument("json", nargs="?")
    sp.set_defaults(fn=cmd_call)

    sp = sub.add_parser("stop", help="abort the current run on a running jevd")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("health", help="query a running jevd")
    sp.set_defaults(fn=cmd_health)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
