"""Local benchmark site ("Northwind Ops"): one small app per task, state kept server-side for the checkers.

    python bench/server.py            # serve on http://localhost:8777 to look at the pages by hand
"""
from __future__ import annotations

import copy
import json
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks import BY_SLUG, TASKS, fresh_state  # noqa: E402

WEB = Path(__file__).resolve().parent / "web"


class BenchServer:
    def __init__(self, port: int = 8777):
        self.port = port
        self.lock = threading.Lock()
        self.states = {t.slug: fresh_state(t) for t in TASKS}
        self.loads = {t.slug: 0 for t in TASKS}  # page loads, so the harness knows a page is up
        self.httpd: ThreadingHTTPServer | None = None

    @property
    def base(self) -> str:
        return f"http://localhost:{self.port}"

    def reset(self, slug: str) -> None:
        with self.lock:
            self.states[slug] = fresh_state(BY_SLUG[slug])

    def state(self, slug: str) -> dict:
        with self.lock:
            return copy.deepcopy(self.states[slug])

    def start(self) -> "BenchServer":
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200) -> None:
                self._send(code, json.dumps(obj).encode(), "application/json")

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    links = "".join(f'<li><a href="{t.path}">{t.id}: {t.title}</a></li>' for t in TASKS)
                    return self._send(200, f"<!doctype html><title>Northwind Ops</title><h1>Northwind Ops</h1><ul>{links}</ul>".encode(), "text/html")
                if path.startswith("/t/"):
                    f = WEB / f"{path[3:]}.html"
                    if path[3:] in BY_SLUG and f.exists():
                        return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    f = (WEB / "static" / path[8:]).resolve()
                    if f.is_file() and (WEB / "static") in f.parents:
                        return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "api" and parts[1] in BY_SLUG and parts[2] == "data":
                    task = BY_SLUG[parts[1]]
                    with srv.lock:
                        srv.loads[task.slug] += 1
                        st = {k: v for k, v in srv.states[task.slug].items() if k != "submissions"}
                        return self._json({"seed": task.seed, "state": copy.deepcopy(st)})
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                parts = self.path.split("?")[0].strip("/").split("/")
                if len(parts) != 3 or parts[0] != "api" or parts[1] not in BY_SLUG:
                    return self._send(404, b"not found", "text/plain")
                task = BY_SLUG[parts[1]]
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except json.JSONDecodeError:
                    return self._json({"ok": False, "error": "bad json"}, 400)
                with srv.lock:
                    st = srv.states[task.slug]
                    if parts[2] == "submit":
                        st["submissions"].append({**body, "_at": time.time()})
                        return self._json({"ok": True})
                    if parts[2] == "action" and task.act:
                        return self._json(task.act(st, body))
                self._send(404, b"not found", "text/plain")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    s = BenchServer(port).start()
    print(f"Northwind Ops on {s.base}  (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        s.stop()
