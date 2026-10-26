# Voyager vs Cua Driver benchmark

The same 10 computer-use tasks, the same Claude planner, the same prompt. The only difference is the computer-use layer: Voyager's daemon (`cu_*` tools) or [Cua Driver](https://cua.ai/cua-driver) (`cua-driver mcp`). Every run is timed and graded against ground truth.

The tasks borrow OSWorld 2.0's challenge types (cross-source reasoning, conflicting sources, implicit state, multi-item tracking, policy following) but each one takes minutes, not hours. All of them run against a local site (`localhost:8777`, "Northwind Ops") plus Calculator and Finder, so nothing touches real accounts.

| ID | Task | What makes it hard |
|---|---|---|
| t01 | File an expense claim from inbox receipts | Finance's email sets the trip name and categories; a personal Uber and a Netflix receipt are decoys |
| t02 | Fill a PO sheet from a #procurement channel | An approval is later revoked, a hallway "approval" doesn't count, and a quantity changes |
| t03 | Inventory sheet arithmetic | 6 products, a reorder rule and a total; numeric precision |
| t04 | Schedule a meeting | The correct slot (15:00) depends on a status note that isn't on the calendar; reading the grid alone gives 11:30 |
| t05 | Admin console across four tabs | One control stays hidden until a checkbox is ticked; notification settings must stay unchanged |
| t06 | Deduplicate a paginated CRM | Emails differ only in case, spread over 3 pages; two contacts share a name but not an email; each delete needs a confirm dialog |
| t07 | Support triage by written policy | 6 tickets, first-match routing rules and compound priority rules |
| t08 | Constrained shopping | Cheapest 27" 4K monitor with rating ≥ 4.5, a hub with 7+ ports, the best eligible coupon (one has expired, another is for new accounts only), and stop at order review |
| t09 | Read a receipt in Chrome, do the math in Calculator | Cross-app; the tip is on the subtotal, not the total |
| t10 | Organize files in Finder | Create a folder, move 3 files, rename 1; a decoy file has "receipts" in its name |

One extra stress task is left out of the default run, so the core comparison stays at the same 10 tasks. Run it with `--tasks t11` (or `--tasks all`):

| ID | Task | What makes it hard |
|---|---|---|
| t11 | Reorder supplies | Built to break batched plans: near-identical items ("USB-C cable 1 m" vs "…, braided"), every row's button is just "Add" or "Remove", the catalog re-sorts after each add, a blocking popup appears after the second add, and quantities must be exact (one Add too many fails) |

Each task is scored on checkpoints (partial credit). It counts as solved only when every checkpoint passes. Checkers read things the agent can't fake: what the site recorded, the file system, and Calculator's display read through accessibility.

## Setup (once)

1. Voyager is installed (`./install.sh`). The harness starts its own daemon on port 8766 with its own data directory, so a `jevd` you already have running is left alone.
2. Cua Driver is installed and has its permissions:
   ```bash
   cua-driver permissions grant      # turn on Accessibility and Screen Recording for CuaDriver, then relaunch it
   ```
3. The app you run the benchmark from (Terminal, iTerm, T3 Code) has **Accessibility** permission. Voyager's daemon and the checker helper both need it.
4. Google Chrome is installed, and `claude` is logged in.

## Run

```bash
python3 bench/run.py selftest                 # checkers vs. known answers; no agents, ~5 s
python3 bench/run.py run                      # 10 tasks × 2 systems, Sonnet 5.5 planner
python3 bench/run.py run --repeat 3           # 3 repeats, for tighter timings
python3 bench/run.py run --systems cua --tasks t04,t08 --model claude-opus-5-5
python3 bench/run.py report bench/results/<run>    # rebuild the report
```

Don't use the Mac while it runs. Both systems drive real windows, and Voyager brings an app to the front for typing and pointer clicks. One run of 20 agent sessions takes about 45–90 minutes. With Sonnet, planner cost is roughly $0.30–1.00 per session.

Results go to `bench/results/<timestamp>/`:

- `report.md` and `report.html`: the verdict, a summary table, per-task results, failed checkpoints and the method.
- `results.jsonl`: one row per run with every checkpoint, timings, each tool call's latency, token counts, cost and the final message.
- `transcripts/*.jsonl`: the full `claude` stream-json output for each run, with arrival times.

## What is measured

- **Accuracy**: tasks solved, mean checkpoint score, and whether the agent's own DONE/FAILED claim matched the checker.
- **Speed**: wall time from launching the agent to its final message, split into time inside tool calls and planner (model) time, plus median latency per tool call and the number of calls and turns. The headline speed number uses only tasks both systems solved, so a fast failure never counts as a win.

## Fairness

- Both systems get identical `claude -p` sessions: the same model, system prompt and task prompt. Built-in tools are off (`--tools ""`), so there's no shell, file or web access and no way to bypass the UI. User settings, plugins and hooks are off (`--setting-sources ""`), and the working directory is neutral, so no CLAUDE.md or project memory loads. Each system's own MCP server instructions are its only guidance.
- Every run starts in a fresh, throwaway Chrome profile (`--force-renderer-accessibility`, the same window size). Task state on the server is reset before each run.
- The order alternates per task and per repeat, so neither system always runs first.
- Planner cost counts Claude only. Voyager also calls its own eyes model (Jev, with Claude only as a fallback when a Jev call fails) inside its tool time; that time is in the wall clock, but its cost isn't in the planner cost.
