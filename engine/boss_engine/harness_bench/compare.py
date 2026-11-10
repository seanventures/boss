"""Baseline vs candidate: is the harness better, the same, or worse? (Rules stated in suite.yaml.)

THE NUMBERS. A run is N passes over the suite's tasks (`--repeat N`). Each metric is aggregated
over the tasks both runs measured, once per pass: success_rate = passing runs / runs and
cache_share = cache read / input tokens; every other metric is the SUM over tasks. Each metric
then has N samples per run: mean, sample stdev (0 with one pass), min and max.

NOISE. A difference counts only when it is larger than its band:
    band = max(abs_floor, rel_floor x |baseline mean|, k_stdev x max(stdev baseline, stdev candidate))
so nothing is ever called better or worse on noise. With one pass the stdevs are 0 and only the
floors decide, which is why a real engine should be run with --repeat 3 or more.

GUARDS. A guard fails when the candidate is worse than the baseline by more than its limit AND by
more than the band. `max_drop` / `max_rise` are absolute limits on the worsening, `max_rise_pct` a
percentage of the baseline mean (a baseline of 0 allows no rise beyond the band).

VERDICT. `worse` when any guard fails; `better` when at least one metric is better and none is
worse; otherwise `same` (an unguarded metric that got worse is still reported, and makes the
verdict `same`, never `better`). Exit code: 0 better/same, 1 worse, 2 not comparable.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .metrics import METRIC_BY_NAME, METRICS

EXIT_OK, EXIT_WORSE, EXIT_NOT_COMPARABLE = 0, 1, 2
_EPS = 1e-9


def stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "stdev": None, "min": None, "max": None}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": mean, "stdev": sd, "min": min(values), "max": max(values)}


def _by_pass(results: list[dict[str, Any]], tasks: list[str]) -> dict[int, dict[str, dict]]:
    passes: dict[int, dict[str, dict]] = {}
    for r in results:
        if r.get("task_id") in tasks and not r.get("skipped"):
            passes.setdefault(int(r.get("iteration") or 1), {})[r["task_id"]] = r
    # A pass that lacks a task (stopped by the budget, or an error before it was recorded) is not
    # a sample of the same suite: it is left out rather than summed short.
    return {p: rows for p, rows in passes.items() if set(rows) == set(tasks)}


def pass_values(results: list[dict[str, Any]], tasks: list[str]) -> dict[str, list[float]]:
    """{metric: [one aggregate per complete pass]} over `tasks`."""
    out: dict[str, list[float]] = {m.name: [] for m in METRICS}
    for _, rows in sorted(_by_pass(results, tasks).items()):
        ms = [rows[t].get("metrics") or {} for t in tasks]
        out["success_rate"].append(sum(1 for t in tasks if rows[t].get("success")) / len(tasks))
        tin = sum(float(m.get("tokens_in") or 0) for m in ms)
        cread = sum(float(m.get("cache_read_tokens") or 0) for m in ms)
        out["cache_share"].append(cread / tin if tin else 0.0)
        for metric in METRICS:
            if metric.aggregate == "sum":
                out[metric.name].append(sum(float(m.get(metric.name) or 0) for m in ms))
    return out


def _flat(d: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(d, dict):
        return {prefix: d}
    out: dict[str, Any] = {}
    for k, v in d.items():
        out.update(_flat(v, f"{prefix}.{k}" if prefix else str(k)))
    return out


def config_diff(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, list[Any]]:
    a, b = _flat(base or {}), _flat(cand or {})
    return {k: [a.get(k), b.get(k)] for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def comparable(base: dict[str, Any], cand: dict[str, Any], *, allow_suite_change: bool = False,
               allow_cross_engine: bool = False) -> list[str]:
    """Why these two reports cannot be compared (an empty list: they can)."""
    b, c = base.get("run") or {}, cand.get("run") or {}
    why = []
    if c.get("status") != "completed":
        why.append(f"the candidate run is {c.get('status')!r}, not completed "
                   "(a run stopped by its budget or an error is not a sample of the suite)")
    if b.get("suite_sha256") != c.get("suite_sha256") and not allow_suite_change:
        why.append("the suite changed between the two runs (suite_sha256 differs): re-make the "
                   "baseline on this suite, or pass --allow-suite-change to compare anyway")
    if b.get("engine") != c.get("engine") and not allow_cross_engine:
        why.append(f"different engines ({b.get('engine')} vs {c.get('engine')}): pass "
                   "--cross-engine to compare engines on purpose")
    if b.get("level") != c.get("level"):
        why.append(f"different scaffold levels ({b.get('level')} vs {c.get('level')})")
    return why


def compare(base: dict[str, Any], cand: dict[str, Any], rules: dict[str, Any], *,
            allow_suite_change: bool = False, allow_cross_engine: bool = False) -> dict[str, Any]:
    """The comparison of two reports under `rules` (suite.rules_for). See the module docstring."""
    reasons = comparable(base, cand, allow_suite_change=allow_suite_change,
                         allow_cross_engine=allow_cross_engine)
    base_tasks = {r["task_id"] for r in base.get("results") or [] if not r.get("skipped")}
    cand_tasks = {r["task_id"] for r in cand.get("results") or [] if not r.get("skipped")}
    common = sorted(base_tasks & cand_tasks)
    if not common:
        reasons.append("the two runs share no measured task")
    out: dict[str, Any] = {
        "baseline": {k: (base.get("run") or {}).get(k) for k in
                     ("id", "baseline_name", "engine", "model", "level", "suite_version",
                      "suite_sha256", "boss_sha", "config_fingerprint", "repeat")},
        "candidate": {k: (cand.get("run") or {}).get(k) for k in
                      ("id", "engine", "model", "level", "suite_version", "suite_sha256",
                       "boss_sha", "config_fingerprint", "repeat")},
        "tasks": common,
        "only_in_baseline": sorted(base_tasks - cand_tasks),
        "only_in_candidate": sorted(cand_tasks - base_tasks),
        "config_changed": config_diff((base.get("run") or {}).get("config") or {},
                                      (cand.get("run") or {}).get("config") or {}),
        "rules": rules, "metrics": {}, "guards": [], "per_task": {}, "notes": [],
    }
    if reasons:
        out.update(verdict="not_comparable", exit_code=EXIT_NOT_COMPARABLE, reasons=reasons)
        return out
    bv, cv = pass_values(base["results"], common), pass_values(cand["results"], common)
    if not bv["success_rate"] or not cv["success_rate"]:
        out.update(verdict="not_comparable", exit_code=EXIT_NOT_COMPARABLE,
                   reasons=["a run has no complete pass over the shared tasks"])
        return out
    k = float(rules.get("k_stdev", 2.0))
    better, worse = [], []
    for metric in METRICS:
        b, c = stats(bv[metric.name]), stats(cv[metric.name])
        cfg = (rules.get("metrics") or {}).get(metric.name) or {}
        band = max(float(cfg.get("abs_floor", 0.0)),
                   float(cfg.get("rel_floor", 0.0)) * abs(b["mean"]),
                   k * max(b["stdev"], c["stdev"]))
        delta = c["mean"] - b["mean"]
        worsening = delta if metric.better == "lower" else -delta
        if abs(delta) <= band + _EPS:
            verdict = "same"
        elif worsening > 0:
            verdict = "worse"
            worse.append(metric.name)
        else:
            verdict = "better"
            better.append(metric.name)
        out["metrics"][metric.name] = {
            "better_is": metric.better, "unit": metric.unit, "baseline": b, "candidate": c,
            "delta": delta, "delta_pct": (delta / abs(b["mean"]) * 100) if b["mean"] else None,
            "noise_band": band, "verdict": verdict,
        }
    for name, rule in (rules.get("guards") or {}).items():
        m = out["metrics"].get(name)
        if m is None or not rule:
            continue
        key, limit = next(iter(rule.items()))
        worsening = m["delta"] if METRIC_BY_NAME[name].better == "lower" else -m["delta"]
        allowed = (float(limit) / 100.0 * abs(m["baseline"]["mean"]) if key == "max_rise_pct"
                   else float(limit))
        failed = worsening > allowed + _EPS and worsening > m["noise_band"] + _EPS
        out["guards"].append({
            "metric": name, "rule": f"{key} {limit}", "allowed_worsening": allowed,
            "worsening": worsening, "noise_band": m["noise_band"],
            "status": "fail" if failed else "pass"})
    failed = [g for g in out["guards"] if g["status"] == "fail"]
    if failed:
        out["verdict"] = "worse"
    elif better and not worse:
        out["verdict"] = "better"
    else:
        out["verdict"] = "same"
    out["exit_code"] = EXIT_WORSE if failed else EXIT_OK
    out["moved"] = {"better": better, "worse": worse}
    n = min(len(bv["success_rate"]), len(cv["success_rate"]))
    if n < 2:
        out["notes"].append("one pass per run: no spread is known, so only the floors guard "
                            "against noise (run --repeat 3 for a real engine)")
    if out["only_in_baseline"] or out["only_in_candidate"]:
        out["notes"].append("compared on the tasks both runs measured only")
    if worse and not failed:
        out["notes"].append(f"worse beyond noise but unguarded: {', '.join(worse)}")
    out["per_task"] = _per_task(base["results"], cand["results"], common)
    return out


def _per_task(base: list[dict], cand: list[dict], tasks: list[str]) -> dict[str, Any]:
    def rows(results, task):
        return [r for r in results if r.get("task_id") == task and not r.get("skipped")]

    out = {}
    for task in tasks:
        b, c = rows(base, task), rows(cand, task)
        entry: dict[str, Any] = {
            "success": [sum(1 for r in b if r.get("success")), len(b),
                        sum(1 for r in c if r.get("success")), len(c)],
        }
        for name in ("wall_s", "cost_usd", "model_calls", "repair_rounds", "interventions"):
            bm = stats([float((r.get("metrics") or {}).get(name) or 0) for r in b])["mean"]
            cm = stats([float((r.get("metrics") or {}).get(name) or 0) for r in c])["mean"]
            entry[name] = [bm, cm]
        failing = sorted({k for r in c if not r.get("success")
                          for k, v in ((r.get("components") or {}).items()) if not v})
        if failing:
            entry["failing_components"] = failing
        out[task] = entry
    return out


def _fmt(name: str, v: Optional[float]) -> str:
    if v is None:
        return "-"
    if abs(v) < _EPS:
        v = 0.0     # never "-0.0%"
    if name in ("success_rate", "cache_share"):
        return f"{v * 100:.1f}%"
    if name == "cost_usd":
        return f"${v:.4f}"
    if name == "wall_s":
        return f"{v:.1f}s"
    return f"{v:,.1f}" if abs(v - round(v)) > _EPS else f"{int(round(v)):,}"


def render(cmp: dict[str, Any]) -> str:
    """The comparison as a terminal table."""
    lines = []
    b, c = cmp["baseline"], cmp["candidate"]
    lines.append(f"baseline  {b.get('baseline_name') or b.get('id')}  engine={b.get('engine')} "
                 f"level={b.get('level')} boss={str(b.get('boss_sha') or '')[:10]} "
                 f"config={b.get('config_fingerprint')} passes={b.get('repeat')}")
    lines.append(f"candidate {c.get('id')}  engine={c.get('engine')} level={c.get('level')} "
                 f"boss={str(c.get('boss_sha') or '')[:10]} config={c.get('config_fingerprint')} "
                 f"passes={c.get('repeat')}")
    if cmp["verdict"] == "not_comparable":
        lines.append("NOT COMPARABLE: " + "; ".join(cmp.get("reasons") or []))
        return "\n".join(lines)
    if cmp.get("config_changed"):
        changed = ", ".join(f"{k}: {v[0]!r} -> {v[1]!r}" for k, v in cmp["config_changed"].items())
        lines.append(f"config changed: {changed}")
    lines.append(f"tasks: {len(cmp['tasks'])} ({', '.join(cmp['tasks'])})")
    lines.append("")
    lines.append(f"{'metric':<14} {'baseline':>14} {'candidate':>14} {'delta':>12} "
                 f"{'noise band':>11} {'spread b/c':>21}  verdict")
    for name, m in cmp["metrics"].items():
        spread = (f"{_fmt(name, m['baseline']['stdev'])}/{_fmt(name, m['candidate']['stdev'])}")
        lines.append(f"{name:<14} {_fmt(name, m['baseline']['mean']):>14} "
                     f"{_fmt(name, m['candidate']['mean']):>14} "
                     f"{('+' if m['delta'] > 0 else '') + _fmt(name, m['delta']):>12} "
                     f"{_fmt(name, m['noise_band']):>11} {spread:>21}  {m['verdict']}")
    lines.append("")
    for g in cmp["guards"]:
        lines.append(f"guard {g['metric']:<14} {g['rule']:<18} worsening "
                     f"{_fmt(g['metric'], g['worsening'])} (allowed {_fmt(g['metric'], g['allowed_worsening'])}, "
                     f"noise {_fmt(g['metric'], g['noise_band'])})  {g['status'].upper()}")
    regressed = [t for t, e in cmp.get("per_task", {}).items()
                 if e["success"][2] * max(e["success"][1], 1) < e["success"][0] * max(e["success"][3], 1)]
    if regressed:
        lines.append("tasks that pass less often: " + ", ".join(
            f"{t} ({', '.join(cmp['per_task'][t].get('failing_components') or [])})" for t in regressed))
    for note in cmp.get("notes") or []:
        lines.append(f"note: {note}")
    lines.append(f"VERDICT: {cmp['verdict'].upper()}")
    return "\n".join(lines)
