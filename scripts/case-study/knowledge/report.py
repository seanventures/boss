#!/usr/bin/env python3
"""Turn case-study results into report.md with tables and SVG charts (case study §4, §5).

    report.py artifacts/case-study/knowledge/<date> [--out DIR]

Rules from the protocol:
- Cost per solved task counts failed attempts' spend: it is total spend / tasks solved. Savings
  are never reported over only the tasks both arms solved.
- Paired comparison over tasks: a two-sided sign test on cost per task (B vs A).
- Primary criteria (§5) are evaluated per engine and printed as PASS/FAIL/INCOMPLETE.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

LEARNING_PAIRS = (("T3", "T7"), ("T4", "T9"))


def load(results_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(results_dir.rglob("T*-r*.json"))]


def solved(r: dict) -> bool:
    return bool((r.get("evaluation") or {}).get("solved"))


def usd(r: dict) -> float:
    return float((r.get("usage") or {}).get("usd") or 0.0)


def sign_test(diffs: Iterable[float]) -> dict:
    """Two-sided exact sign test; ties dropped."""
    diffs = [d for d in diffs if d != 0]
    n, k = len(diffs), sum(1 for d in diffs if d < 0)
    if n == 0:
        return {"n": 0, "b_cheaper": 0, "p": None}
    tail = sum(math.comb(n, i) for i in range(min(k, n - k) + 1)) / 2 ** n
    return {"n": n, "b_cheaper": k, "p": round(min(1.0, 2 * tail), 4)}


def summarize(results: list[dict]) -> dict:
    by = defaultdict(list)
    for r in results:
        by[(r["engine"], r["arm"])].append(r)
    out = {}
    for (engine, arm), rows in sorted(by.items()):
        n_solved = sum(solved(r) for r in rows)
        spend = sum(usd(r) for r in rows)
        out[(engine, arm)] = {
            "builds": len(rows), "solved_tasks": len({r["task"] for r in rows if solved(r)}),
            "tasks": len({r["task"] for r in rows}), "spend_usd": round(spend, 2),
            "usd_per_solved": round(spend / n_solved, 2) if n_solved else None,
            "median_wall_min_solved": statistics.median([r.get("wall_minutes") or 0 for r in rows if solved(r)])
            if n_solved else None,
            "tokens": sum(sum((r.get("usage") or {}).get(k, 0) for k in ("prompt_tokens", "completion_tokens"))
                          for r in rows)}
    return out


def per_task(results: list[dict]) -> dict:
    """(engine, task) -> arm -> mean cost, any solved, mean minutes."""
    grid: dict = defaultdict(lambda: defaultdict(list))
    for r in results:
        grid[(r["engine"], r["task"])][r["arm"]].append(r)
    out = {}
    for key, arms in grid.items():
        out[key] = {arm: {"usd": round(statistics.fmean(usd(r) for r in rows), 2),
                          "solved": f"{sum(solved(r) for r in rows)}/{len(rows)}",
                          "minutes": round(statistics.fmean(r.get("wall_minutes") or 0 for r in rows), 1),
                          "turns": round(statistics.fmean((r.get("usage") or {}).get("turns", 0) for r in rows))}
                    for arm, rows in arms.items()}
    return out


def criteria(summary: dict, tasks: dict) -> dict:
    out = {}
    for engine in sorted({e for e, _ in summary}):
        a, b = summary.get((engine, "A")), summary.get((engine, "B"))
        if not a or not b:
            out[engine] = {"verdict": "INCOMPLETE", "why": "both arms are needed"}
            continue
        need = 2 if engine == "E2" else 0
        outcome = b["solved_tasks"] >= a["solved_tasks"] + need
        efficiency = (a["usd_per_solved"] is not None and b["usd_per_solved"] is not None
                      and b["usd_per_solved"] <= 0.8 * a["usd_per_solved"])
        diffs = [t["B"]["usd"] - t["A"]["usd"] for (e, _), t in tasks.items() if e == engine and "A" in t and "B" in t]
        complete = a["tasks"] >= 12 and b["tasks"] >= 12
        out[engine] = {"outcome": outcome, "efficiency": efficiency, "sign_test": sign_test(diffs),
                       "verdict": ("PASS" if outcome and efficiency else "FAIL") if complete else "INCOMPLETE"}
    return out


def learning(results: list[dict]) -> list[dict]:
    rows = []
    for engine in sorted({r["engine"] for r in results}):
        for first, second in LEARNING_PAIRS:
            def cost(arm, task):
                vals = [usd(r) for r in results if r["engine"] == engine and r["arm"] == arm and r["task"] == task]
                return statistics.fmean(vals) if vals else None
            a1, a2, b1, b2 = cost("A", first), cost("A", second), cost("B", first), cost("B", second)
            if None in (a1, a2, b1, b2) or not (a1 and b1 and a2):
                continue
            ratio_b, ratio_a = b2 / b1, a2 / a1
            rows.append({"engine": engine, "pair": f"{first}→{second}", "B_ratio": round(ratio_b, 2),
                         "A_ratio": round(ratio_a, 2), "normalised": round(ratio_b / ratio_a, 2),
                         "meets_70pct": ratio_b / ratio_a <= 0.7})
    return rows


def bar_svg(title: str, labels: list[str], series: dict[str, list[float]], unit: str) -> str:
    """Grouped bars, one group per label; plain SVG with currentColor text so it reads in both themes."""
    colors = {"A": "#8a94a6", "B": "#2f6fdf"}
    width, height, pad, top = 640, 260, 40, 30
    peak = max([v for vals in series.values() for v in vals] + [1e-9])
    group = (width - 2 * pad) / max(1, len(labels))
    bar = group / (len(series) + 1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{title}" font-family="sans-serif" font-size="11">',
             f'<text x="{pad}" y="18" fill="currentColor" font-size="13">{title}</text>']
    for gi, label in enumerate(labels):
        x0 = pad + gi * group
        for si, (name, vals) in enumerate(series.items()):
            v = vals[gi]
            h = (height - top - pad) * v / peak
            x = x0 + bar * (si + 0.5)
            parts.append(f'<rect x="{x:.1f}" y="{height - pad - h:.1f}" width="{bar * 0.9:.1f}" height="{h:.1f}" '
                         f'fill="{colors.get(name, "#999")}"><title>{name} {label}: {v:g} {unit}</title></rect>')
        parts.append(f'<text x="{x0 + group / 2:.1f}" y="{height - pad + 14}" text-anchor="middle" '
                     f'fill="currentColor">{label}</text>')
    for si, name in enumerate(series):
        parts.append(f'<rect x="{width - 120 + si * 50}" y="8" width="10" height="10" fill="{colors.get(name)}"/>'
                     f'<text x="{width - 106 + si * 50}" y="17" fill="currentColor">arm {name}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render(results: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary, tasks = summarize(results), per_task(results)
    crit, learn = criteria(summary, tasks), learning(results)
    lines = ["# Knowledge Graph case study: NorthPeak Retail", "",
             f"{len(results)} builds · Saleor `{results[0].get('saleor_sha', '?')[:12]}` · stages: "
             f"{', '.join(sorted({r.get('stage', '?') for r in results}))} · approved by "
             f"{', '.join(sorted({r.get('approved_by') or '?' for r in results}))}", "",
             "## Arms", "", "| Engine | Arm | Builds | Tasks solved | Spend | $ per solved task | Median min (solved) |",
             "|---|---|---|---|---|---|---|"]
    for (engine, arm), s in summary.items():
        lines.append(f"| {engine} | {arm} | {s['builds']} | {s['solved_tasks']}/{s['tasks']} | ${s['spend_usd']} | "
                     f"{'$' + str(s['usd_per_solved']) if s['usd_per_solved'] is not None else '—'} | "
                     f"{s['median_wall_min_solved'] if s['median_wall_min_solved'] is not None else '—'} |")
    lines += ["", "Cost per solved task = all spend, failed attempts included, / tasks solved.", "",
              "## Per task", "", "| Engine | Task | A solved | A $ | A min | B solved | B $ | B min |",
              "|---|---|---|---|---|---|---|---|"]
    for (engine, task), arms in sorted(tasks.items(), key=lambda kv: (kv[0][0], int(kv[0][1][1:]))):
        a, b = arms.get("A", {}), arms.get("B", {})
        lines.append(f"| {engine} | {task} | {a.get('solved', '—')} | {a.get('usd', '—')} | {a.get('minutes', '—')} | "
                     f"{b.get('solved', '—')} | {b.get('usd', '—')} | {b.get('minutes', '—')} |")
    lines += ["", "## Primary criteria (§5)", ""]
    for engine, c in crit.items():
        lines.append(f"- **{engine}: {c['verdict']}**" + (f" — outcome {'met' if c.get('outcome') else 'not met'}, "
                     f"efficiency {'met' if c.get('efficiency') else 'not met'}, sign test {c.get('sign_test')}"
                     if "outcome" in c else f" — {c['why']}"))
    if learn:
        lines += ["", "## Learning pairs", "", "| Engine | Pair | B second/first | A second/first | Normalised | ≤ 0.7 |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {r['engine']} | {r['pair']} | {r['B_ratio']} | {r['A_ratio']} | {r['normalised']} | "
                  f"{'yes' if r['meets_70pct'] else 'no'} |" for r in learn]
    for engine in sorted({e for e, _ in tasks}):
        labels = sorted({t for e, t in tasks if e == engine}, key=lambda t: int(t[1:]))
        series = {arm: [tasks[(engine, t)].get(arm, {}).get("usd", 0) for t in labels] for arm in ("A", "B")}
        name = f"cost_{engine}.svg"
        (out_dir / name).write_text(bar_svg(f"Model spend per task, {engine} (USD)", labels, series, "USD"))
        lines += ["", f"![Model spend per task, {engine}]({name})"]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results_dir")
    p.add_argument("--out")
    args = p.parse_args()
    results = load(Path(args.results_dir))
    if not results:
        raise SystemExit("no results found")
    print(render(results, Path(args.out or args.results_dir)))


if __name__ == "__main__":
    main()
