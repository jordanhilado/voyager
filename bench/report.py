"""Turn results.jsonl into report.md and report.html (accuracy first, then speed)."""
from __future__ import annotations

import html
import json
import statistics as st
from pathlib import Path

LABELS = {"voyager": "Voyager", "cua": "Cua Driver"}
COLORS = {"voyager": "#2457d6", "cua": "#d9822b"}


def _load(out: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    rows = [json.loads(l) for l in (out / "results.jsonl").read_text().splitlines() if l.strip()] if (out / "results.jsonl").exists() else []
    return meta, rows


def _med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def _f(x, unit="", nd=1):
    return "—" if x is None else f"{x:.{nd}f}{unit}"


def summarize(rows: list[dict]) -> dict:
    systems = [s for s in LABELS if any(r["system"] == s for r in rows)]
    tasks = sorted({r["task"] for r in rows})
    titles = {r["task"]: r["title"] for r in rows}
    per = {s: [r for r in rows if r["system"] == s] for s in systems}
    summary = {}
    for s, rs in per.items():
        calls = [c["s"] for r in rs for c in r.get("tool_calls", []) if not c.get("unfinished")]
        summary[s] = {
            "runs": len(rs), "solved": sum(r["success"] for r in rs), "mean_score": _mean([r["score"] for r in rs]),
            "median_wall": _med([r["wall_s"] for r in rs]), "total_wall": sum(r["wall_s"] for r in rs),
            "mean_tool_s": _mean([r["tool_s"] for r in rs]), "mean_model_s": _mean([r["model_s"] for r in rs]),
            "median_call_s": _med(calls), "mean_calls": _mean([r["n_tool_calls"] for r in rs]),
            "mean_turns": _mean([r.get("turns") for r in rs]), "cost": sum(r.get("cost_usd") or 0 for r in rs),
            "timeouts": sum(r["status"] == "timeout" for r in rs), "infra": sum(r["status"] == "infra" for r in rs),
            "claim_match": sum(r.get("claimed_done") == r["success"] for r in rs),
        }
    # Head to head: same task and repeat, both systems succeeded.
    h2h = {s: [] for s in systems}
    wins = {s: 0 for s in systems}
    pairs = 0
    if len(systems) == 2:
        a, b = systems
        for r in per[a]:
            o = next((x for x in per[b] if x["task"] == r["task"] and x["rep"] == r["rep"]), None)
            if o and r["success"] and o["success"]:
                pairs += 1
                h2h[a].append(r["wall_s"])
                h2h[b].append(o["wall_s"])
                wins[a if r["wall_s"] < o["wall_s"] else b] += 1
    by_task = {}
    for t in tasks:
        by_task[t] = {s: [r for r in per[s] if r["task"] == t] for s in systems}
    return {"systems": systems, "tasks": tasks, "titles": titles, "summary": summary, "h2h": h2h, "wins": wins,
            "pairs": pairs, "by_task": by_task}


def verdict(S: dict) -> list[str]:
    systems, sm = S["systems"], S["summary"]
    if len(systems) != 2:
        return []
    a, b = systems
    lines = []
    ka = (sm[a]["solved"], sm[a]["mean_score"] or 0)
    kb = (sm[b]["solved"], sm[b]["mean_score"] or 0)
    acc = a if ka > kb else b if kb > ka else None
    lines.append(f"Accuracy: {LABELS[acc]} ({sm[acc]['solved']}/{sm[acc]['runs']} solved vs "
                 f"{sm[b if acc == a else a]['solved']}/{sm[b if acc == a else a]['runs']})" if acc else
                 f"Accuracy: tie ({sm[a]['solved']}/{sm[a]['runs']} solved each)")
    if S["pairs"]:
        ma, mb = st.median(S["h2h"][a]), st.median(S["h2h"][b])
        fast, slow = (a, b) if ma < mb else (b, a)
        lines.append(f"Speed, on the {S['pairs']} task runs both solved: {LABELS[fast]} is {max(ma, mb) / min(ma, mb):.2f}× faster "
                     f"(median {min(ma, mb):.0f}s vs {max(ma, mb):.0f}s) and faster on {S['wins'][fast]} of {S['pairs']}")
    else:
        lines.append("Speed: no task was solved by both systems, so there is no like-for-like time comparison.")
    return lines


def _cell(rs: list[dict]) -> str:
    if not rs:
        return "—"
    ok = sum(r["success"] for r in rs)
    mark = "✅" if ok == len(rs) else ("❌" if ok == 0 else "◐")
    sc = _mean([r["score"] for r in rs])
    return f"{mark} {ok}/{len(rs)} · {sc * 100:.0f}% · {_med([r['wall_s'] for r in rs]):.0f}s · {_med([r['n_tool_calls'] for r in rs]):.0f} calls"


def to_markdown(meta: dict, S: dict) -> str:
    sy, sm = S["systems"], S["summary"]
    out = [f"# Voyager vs Cua Driver: {len(S['tasks'])} computer-use task{'s' if len(S['tasks']) != 1 else ''}", "",
           f"Planner model `{meta.get('model')}` for both · {meta.get('repeat', 1)} repeat(s) · started {meta.get('started')} · "
           f"{meta.get('claude_version', '')} · {meta.get('cua_driver_version', '')}", ""]
    out += [f"**{l}**  " for l in verdict(S)] + [""]
    out += ["## Summary", "", "| | " + " | ".join(LABELS[s] for s in sy) + " |", "|---|" + "---|" * len(sy)]
    rows = [
        ("Tasks solved (all checkpoints)", lambda s: f"{sm[s]['solved']}/{sm[s]['runs']}"),
        ("Mean checkpoint score", lambda s: _f((sm[s]['mean_score'] or 0) * 100, "%", 0)),
        ("Median wall time per task", lambda s: _f(sm[s]['median_wall'], " s")),
        ("Median wall time, tasks both solved", lambda s: _f(_med(S['h2h'][s]), " s")),
        ("Total wall time", lambda s: _f(sm[s]['total_wall'], " s", 0)),
        ("Mean time inside tools per task", lambda s: _f(sm[s]['mean_tool_s'], " s")),
        ("Mean model (planner) time per task", lambda s: _f(sm[s]['mean_model_s'], " s")),
        ("Median latency per tool call", lambda s: _f(sm[s]['median_call_s'], " s", 2)),
        ("Mean tool calls per task", lambda s: _f(sm[s]['mean_calls'])),
        ("Mean planner turns per task", lambda s: _f(sm[s]['mean_turns'])),
        ("Planner cost (Claude), total", lambda s: f"${sm[s]['cost']:.2f}"),
        ("Agent's DONE/FAILED claim matched the checker", lambda s: f"{sm[s]['claim_match']}/{sm[s]['runs']}"),
        ("Timeouts / infra errors", lambda s: f"{sm[s]['timeouts']} / {sm[s]['infra']}"),
    ]
    out += [f"| {name} | " + " | ".join(fn(s) for s in sy) + " |" for name, fn in rows]
    out += ["", "## Per task", "", "Cell: result · mean checkpoint score · median wall time · median tool calls", "",
            "| Task | " + " | ".join(LABELS[s] for s in sy) + " | Faster (both solved) |", "|---|" + "---|" * (len(sy) + 1)]
    for t in S["tasks"]:
        cells = [_cell(S["by_task"][t][s]) for s in sy]
        faster = "—"
        if len(sy) == 2:
            solved = {s: [r["wall_s"] for r in S["by_task"][t][s] if r["success"]] for s in sy}
            if all(solved.values()):
                m = {s: st.median(v) for s, v in solved.items()}
                f = min(m, key=m.get)
                other = [s for s in sy if s != f][0]
                faster = f"{LABELS[f]} ({m[other] / m[f]:.1f}×)"
        out.append(f"| {t} {S['titles'][t]} | " + " | ".join(cells) + f" | {faster} |")
    out += ["", "## Failed checkpoints", ""]
    any_fail = False
    for t in S["tasks"]:
        for s in sy:
            for r in S["by_task"][t][s]:
                bad = [c for c in r["checkpoints"] if not c["ok"]]
                if bad or r["status"] != "ok":
                    any_fail = True
                    out.append(f"- **{r['run_id']}** [{r['status']}]: " + "; ".join(f"{c['name']} ({c['detail']})" if c["detail"] else c["name"] for c in bad))
    if not any_fail:
        out.append("None.")
    out += ["", "## Method", "",
            "- Each run is a fresh `claude -p` with the same model, system prompt and task prompt. Built-in tools are off "
            "(no shell, files or web), and exactly one MCP server is loaded: Voyager's daemon or `cua-driver mcp`.",
            "- The harness resets the task, opens the start page in a throwaway Chrome profile (or the Finder folder), then "
            "starts the clock. Wall time runs from launching the agent to its final message.",
            "- Checkers read ground truth the agent cannot fake: the benchmark server's recorded submissions, the file "
            "system, and Calculator's display through accessibility.",
            "- Tool time is the union of tool-call intervals; model time is wall time minus startup and tool time.",
            "- Planner cost covers Claude only. Voyager's daemon also calls its own eyes model (Jev, Claude on fallback) inside tool "
            "time; that spend is not in the planner cost.",
            "- Who goes first alternates per task and repeat, so cache warm-up does not favor one side."]
    return "\n".join(out) + "\n"


def to_html(meta: dict, S: dict, md: str) -> str:
    sy, sm = S["systems"], S["summary"]
    e = html.escape
    maxw = max([r["wall_s"] for t in S["tasks"] for s in sy for r in S["by_task"][t][s]] or [1])
    bars = []
    for t in S["tasks"]:
        rows = []
        for s in sy:
            rs = S["by_task"][t][s]
            if not rs:
                continue
            w = _med([r["wall_s"] for r in rs])
            ok = sum(r["success"] for r in rs)
            sc = _mean([r["score"] for r in rs]) * 100
            rows.append(f'<div class="bar-row"><span class="sys">{LABELS[s]}</span><div class="track"><div class="bar" '
                        f'style="width:{w / maxw * 100:.1f}%;background:{COLORS[s]}"></div></div>'
                        f'<span class="val">{w:.0f}s · {"✅" if ok == len(rs) else "❌" if ok == 0 else "◐"} {sc:.0f}%</span></div>')
        bars.append(f'<div class="task"><div class="tname">{e(t)} · {e(S["titles"][t])}</div>{"".join(rows)}</div>')
    cards = "".join(
        f'<div class="card" style="border-top:4px solid {COLORS[s]}"><h3>{LABELS[s]}</h3>'
        f'<div class="big">{sm[s]["solved"]}/{sm[s]["runs"]}</div><div class="muted">tasks solved</div>'
        f'<div class="kv"><span>Mean score</span><b>{(sm[s]["mean_score"] or 0) * 100:.0f}%</b></div>'
        f'<div class="kv"><span>Median wall time</span><b>{_f(sm[s]["median_wall"], " s")}</b></div>'
        f'<div class="kv"><span>Median, both solved</span><b>{_f(_med(S["h2h"][s]), " s")}</b></div>'
        f'<div class="kv"><span>Median per tool call</span><b>{_f(sm[s]["median_call_s"], " s", 2)}</b></div>'
        f'<div class="kv"><span>Tool calls per task</span><b>{_f(sm[s]["mean_calls"])}</b></div>'
        f'<div class="kv"><span>Planner cost</span><b>${sm[s]["cost"]:.2f}</b></div></div>' for s in sy)
    v = "".join(f"<li>{e(l)}</li>" for l in verdict(S))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Voyager vs Cua Driver</title><style>
body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}}
main{{max-width:1000px;margin:0 auto;padding:28px}} h1{{font-size:22px;margin:0 0 4px}} .muted{{color:#5f6b7a}}
.verdict{{background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:12px 18px;margin:16px 0;font-size:15px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}} .card{{background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:14px 18px}}
.card h3{{margin:0}} .big{{font-size:34px;font-weight:700}} .kv{{display:flex;justify-content:space-between;border-top:1px solid #eef0f3;padding:4px 0}}
.task{{background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:10px 14px;margin:10px 0}} .tname{{font-weight:600;margin-bottom:6px}}
.bar-row{{display:grid;grid-template-columns:90px 1fr 120px;gap:10px;align-items:center;margin:3px 0}} .track{{background:#eef0f3;border-radius:4px;height:14px}}
.bar{{height:14px;border-radius:4px}} .val{{font-variant-numeric:tabular-nums}} pre{{white-space:pre-wrap;background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:14px;font-size:12px}}
</style></head><body><main><h1>Voyager vs Cua Driver</h1>
<div class="muted">{len(S['tasks'])} tasks · planner {e(str(meta.get('model')))} for both · {meta.get('repeat', 1)} repeat(s) · {e(str(meta.get('started')))}</div>
<ul class="verdict">{v}</ul><div class="cards">{cards}</div>
<h2>Wall time per task (median), with result and checkpoint score</h2>{''.join(bars)}
<h2>Full report</h2><pre>{e(md)}</pre></main></body></html>"""


def build_report(out: Path) -> tuple[Path, Path]:
    meta, rows = _load(out)
    S = summarize(rows)
    md = to_markdown(meta, S)
    (out / "report.md").write_text(md)
    (out / "report.html").write_text(to_html(meta, S, md))
    return out / "report.md", out / "report.html"
