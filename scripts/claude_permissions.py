"""Idempotently merge Voyager's tool permissions into ~/.claude/settings.json (backup kept)."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

ALLOW = ["mcp__voyager__cu_open", "mcp__voyager__cu_bind", "mcp__voyager__cu_run_plan", "mcp__voyager__cu_wait_until",
         "mcp__voyager__cu_read", "mcp__voyager__cu_observe", "mcp__voyager__cu_stop",
         "Bash(voyager up)", "Bash(voyager up:*)", "Bash(voyager call:*)", "Bash(~/.voyager/venv/bin/voyager up:*)"]
ASK = ["mcp__voyager__cu_commit"]


def main() -> None:
    path = Path.home() / ".claude" / "settings.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    perms = data.setdefault("permissions", {})
    changed = False
    for key, items in (("allow", ALLOW), ("ask", ASK)):
        lst = perms.setdefault(key, [])
        for it in items:
            if it not in lst:
                lst.append(it)
                changed = True
    if changed:
        if path.exists():
            shutil.copy(path, path.with_suffix(f".json.bak-{int(time.time())}"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    print("changed" if changed else "already up to date")


if __name__ == "__main__":
    main()
