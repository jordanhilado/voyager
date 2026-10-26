# Voyager vs Cua Driver: 11 computer-use tasks

_Full report from `bench/run.py`, run on 2026-10-25 on an M1 Pro MacBook (macOS 26). Voyager used the native macOS backend with Jev eyes._

Planner model `claude-sonnet-5-5` for both · 1 repeat(s) · started 2026-10-25T22:04:56 · 2.1.310 (Claude Code) · cua-driver 0.28.2

**Accuracy: Cua Driver (10/11 solved vs 8/11)**  
**Speed, on the 8 task runs both solved: Voyager is 1.63× faster (median 58s vs 95s) and faster on 7 of 8**  

## Summary

| | Voyager | Cua Driver |
|---|---|---|
| Tasks solved (all checkpoints) | 8/11 | 10/11 |
| Mean checkpoint score | 83% | 94% |
| Median wall time per task | 47.4 s | 93.9 s |
| Median wall time, tasks both solved | 58.0 s | 94.6 s |
| Total wall time | 571 s | 1346 s |
| Mean time inside tools per task | 29.9 s | 53.8 s |
| Mean model (planner) time per task | 21.4 s | 67.9 s |
| Median latency per tool call | 0.91 s | 1.48 s |
| Mean tool calls per task | 9.8 | 49.2 |
| Mean planner turns per task | 10.8 | 50.2 |
| Planner cost (Claude), total | $1.54 | $11.45 |
| Agent's DONE/FAILED claim matched the checker | 10/11 | 11/11 |
| Timeouts / infra errors | 0 / 0 | 0 / 0 |

## Per task

Cell: result · mean checkpoint score · median wall time · median tool calls

| Task | Voyager | Cua Driver | Faster (both solved) |
|---|---|---|---|
| t01 Expense claim from inbox receipts | ✅ 1/1 · 100% · 48s · 10 calls | ✅ 1/1 · 100% · 115s · 55 calls | Voyager (2.4×) |
| t02 Purchase-order sheet from a chat channel | ✅ 1/1 · 100% · 47s · 8 calls | ✅ 1/1 · 100% · 95s · 39 calls | Voyager (2.0×) |
| t03 Inventory sheet arithmetic | ✅ 1/1 · 100% · 42s · 4 calls | ✅ 1/1 · 100% · 61s · 26 calls | Voyager (1.5×) |
| t04 Schedule around calendars and status notes | ✅ 1/1 · 100% · 26s · 3 calls | ✅ 1/1 · 100% · 62s · 27 calls | Voyager (2.4×) |
| t05 Admin console across four tabs | ✅ 1/1 · 100% · 83s · 13 calls | ✅ 1/1 · 100% · 94s · 53 calls | Voyager (1.1×) |
| t06 Deduplicate a paginated CRM | ❌ 0/1 · 25% · 42s · 14 calls | ✅ 1/1 · 100% · 44s · 26 calls | — |
| t07 Support triage by written policy | ✅ 1/1 · 100% · 81s · 12 calls | ✅ 1/1 · 100% · 143s · 66 calls | Voyager (1.8×) |
| t08 Constrained shopping with coupon terms | ❌ 0/1 · 67% · 15s · 4 calls | ✅ 1/1 · 100% · 65s · 21 calls | — |
| t09 Receipt in the browser, math in Calculator | ✅ 1/1 · 100% · 83s · 11 calls | ✅ 1/1 · 100% · 14s · 5 calls | Cua Driver (5.8×) |
| t10 Organize files in Finder | ❌ 0/1 · 17% · 35s · 17 calls | ❌ 0/1 · 33% · 544s · 167 calls | — |
| t11 Reorder supplies (batching stress test) | ✅ 1/1 · 100% · 68s · 12 calls | ✅ 1/1 · 100% · 109s · 56 calls | Voyager (1.6×) |

## Failed checkpoints

- **voyager-t06-r1** [ok]: deleted older duplicate Kenji Watanabe <KEN@watanabe.jp> 2025-12-01; deleted older duplicate Maria Gonzalez <maria.gonzalez@acme.com> 2026-03-14; deleted older duplicate Samuel O'Neil <Sam.ONeil@globex.io> 2026-01-09
- **voyager-t08-r1** [ok]: cart has PortPro 7-in-1 hub ×1 (cart {'m3': 1, 'h3': 1}); nothing else in the cart (cart {'m3': 1, 'h3': 1})
- **voyager-t10-r1** [ok]: folder inbox/Receipts exists; receipt-uber-oct06.pdf moved into Receipts; receipt-marriott-oct08.pdf moved into Receipts; receipt-saltie-girl.png moved into Receipts; notes-draft.txt renamed to notes-final.txt
- **cua-t10-r1** [ok]: receipt-uber-oct06.pdf moved into Receipts; receipt-marriott-oct08.pdf moved into Receipts; receipt-saltie-girl.png moved into Receipts; notes-draft.txt renamed to notes-final.txt

## Method

- Each run is a fresh `claude -p` with the same model, system prompt and task prompt. Built-in tools are off (no shell, files or web), and exactly one MCP server is loaded: Voyager's daemon or `cua-driver mcp`.
- The harness resets the task, opens the start page in a throwaway Chrome profile (or the Finder folder), then starts the clock. Wall time runs from launching the agent to its final message.
- Checkers read ground truth the agent cannot fake: the benchmark server's recorded submissions, the file system, and Calculator's display through accessibility.
- Tool time is the union of tool-call intervals; model time is wall time minus startup and tool time.
- Planner cost covers Claude only. Voyager's daemon also calls its own eyes model (Jev or Claude) inside tool time; that spend is not in the planner cost.
- Who goes first alternates per task and repeat, so cache warm-up does not favor one side.
