"""Model bake-off — VAG spec §8 (docs/product/BOSS_VERIFICATION_AMPLIFIED_GENERATION.md).

Runs a fixed task suite through the EXISTING governed pipeline once per model config and
reports the numbers the strategy turns on: gate pass-rate, retries, wall-clock, and
$/verified-outcome per config, plus false-pass counts from known-bad probes.

This module is the importable core (request building + aggregation + rendering) so it is
unit-testable; scripts/run_bakeoff.py is the thin CLI driver around it. Batch identity
rides in code_task_runs.finalize_ctx->'bakeoff' (no extra tables); results are derived
on demand, so a batch can be re-aggregated at any time.

Cost accounting invariant: llm_calls is the SINGLE source of dollars. The same spend also
appears aggregated in code_task_runs.usage and quality_gates.evidence — never add those
to llm_calls sums (they are the same dollars, counted once per level).
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text


def build_run_request(task: dict, config: dict, batch_id: str, iteration: int,
                      defaults: Optional[dict] = None) -> dict:
    """The POST /v1/code-tasks payload for one (task × config × iteration) run."""
    d = defaults or {}
    payload = {
        "project_id": d.get("project_id"),
        "title": f"[bakeoff:{config['name']}#{iteration}] {task['title']}",
        "instructions": task["instructions"],
        "acceptance_criteria": task.get("acceptance_criteria") or [],
        "test_commands": task.get("test_commands") or d.get("test_commands") or [],
        "risk_tier": task.get("risk_tier", d.get("risk_tier")),
        "profile": config.get("profile") or task.get("profile"),
        "model_config": config.get("model_config") or {},
        # VAG Phase 3: per-config repair-loop budget (evidence-fed cheap retries). Falls
        # back to the deployment's BOSS_CODE_REPAIR_MAX_RETRIES when the config omits it.
        "repair_retries": config.get("repair_retries", d.get("repair_retries")),
        # VAG Phase 1 (#9): per-config refute-panel size — lets one batch compare panel=1 vs
        # panel=3 apples-to-apples. Falls back to the deployment's BOSS_CODE_REVIEW_PANEL_SIZE.
        "review_panel_size": config.get("review_panel_size", d.get("review_panel_size")),
        # Bake-offs measure the gate, not delivery — suppress the PR unless asked.
        "open_pr": bool(d.get("open_pr", False)),
        "finalize_ctx": {"bakeoff": {
            "batch_id": batch_id,
            "config": config["name"],
            "task_key": task["key"],
            "iteration": iteration,
            "expect": task.get("expect", "pass"),
        }},
    }
    for key in ("owner", "repo", "base_branch"):
        if d.get(key):
            payload[key] = d[key]
    return {k: v for k, v in payload.items() if v is not None}


def aggregate_batch(connection, batch_id: str) -> dict:
    """Aggregate one batch from code_task_runs ⋈ quality_gates ⋈ llm_calls.

    Known-bad probes (expect=fail) are excluded from pass-rate and $/verified-outcome —
    they measure the VERIFIER; a passing gate on one counts as a false_pass instead.
    """
    runs = connection.execute(
        text("""
            SELECT r.id, r.status, r.error,
                   EXTRACT(EPOCH FROM (r.updated_at - r.created_at)) AS wall_clock_s,
                   r.finalize_ctx->'bakeoff' AS bk,
                   q.status AS gate_status, q.gates_failed,
                   q.evidence->>'repair_attempts' AS repair_attempts
              FROM code_task_runs r
              LEFT JOIN quality_gates q ON q.code_task_run_id = r.id
             WHERE r.finalize_ctx->'bakeoff'->>'batch_id' = :b
        """),
        {"b": batch_id},
    ).mappings().all()
    run_ids = [r["id"] for r in runs]
    costs: dict[str, dict] = {}
    if run_ids:
        for row in connection.execute(
            text("""
                SELECT code_task_run_id AS run_id,
                       COALESCE(SUM(cost_usd_micros), 0) AS cost,
                       COALESCE(SUM(retries), 0) AS retries,
                       COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN price_estimated THEN 1 ELSE 0 END), 0) AS estimated
                  FROM llm_calls
                 WHERE code_task_run_id = ANY(:ids)
                 GROUP BY code_task_run_id
            """),
            {"ids": run_ids},
        ).mappings():
            costs[row["run_id"]] = dict(row)

    configs: dict[str, dict] = {}
    for r in runs:
        bk = r["bk"] if isinstance(r["bk"], dict) else json.loads(r["bk"] or "{}")
        name = bk.get("config", "unknown")
        c = configs.setdefault(name, {
            "runs": 0, "gate_pass": 0, "false_pass": 0, "known_bad_runs": 0,
            "total_cost_usd_micros": 0, "retries": 0, "llm_calls": 0,
            "price_estimated_calls": 0, "wall_clock_s": [], "failure_breakdown": {},
            # VAG Phase 3 repair-loop attempts used per run (a histogram: n_retries -> count).
            "repair_hist": {},
        })
        try:
            ra = int(r["repair_attempts"]) if r["repair_attempts"] is not None else 0
        except (TypeError, ValueError):
            ra = 0
        c["repair_hist"][ra] = c["repair_hist"].get(ra, 0) + 1
        cost = costs.get(r["id"], {})
        c["total_cost_usd_micros"] += int(cost.get("cost") or 0)
        c["retries"] += int(cost.get("retries") or 0)
        c["llm_calls"] += int(cost.get("calls") or 0)
        c["price_estimated_calls"] += int(cost.get("estimated") or 0)
        if r["wall_clock_s"] is not None:
            c["wall_clock_s"].append(float(r["wall_clock_s"]))

        gate_passed = r["gate_status"] == "pass"
        if bk.get("expect") == "fail":
            c["known_bad_runs"] += 1
            if gate_passed:
                c["false_pass"] += 1  # the verifier was fooled — the #1 VAG guardrail
            continue
        c["runs"] += 1
        if gate_passed:
            c["gate_pass"] += 1
        else:
            failed = r["gates_failed"]
            if isinstance(failed, str):
                failed = json.loads(failed or "[]")
            for gate in (failed or ([f"run:{r['status']}"] if r["status"] != "completed" else ["gate:unknown"])):
                c["failure_breakdown"][gate] = c["failure_breakdown"].get(gate, 0) + 1

    for c in configs.values():
        wall = sorted(c.pop("wall_clock_s"))
        c["mean_wall_clock_s"] = round(sum(wall) / len(wall), 1) if wall else None
        # Latency spread matters as much as the mean for an SLA — a p90 tail is what a
        # user actually waits on. total = wall-clock a config's whole task set consumed.
        c["median_wall_clock_s"] = round(_pct(wall, 0.5), 1) if wall else None
        c["p90_wall_clock_s"] = round(_pct(wall, 0.9), 1) if wall else None
        c["total_wall_clock_s"] = round(sum(wall), 1) if wall else None
        c["pass_rate"] = round(c["gate_pass"] / c["runs"], 3) if c["runs"] else None
        c["cost_per_verified_outcome_usd_micros"] = (
            int(c["total_cost_usd_micros"] / c["gate_pass"]) if c["gate_pass"] else None
        )
        # Time-per-verified-outcome: the wall-clock a config spends per PASSING run —
        # the time analogue of $/verified-outcome (the third decision axis).
        c["wall_clock_per_verified_s"] = (
            round(c["total_wall_clock_s"] / c["gate_pass"], 1)
            if c["gate_pass"] and c["total_wall_clock_s"] is not None else None
        )
    models = _aggregate_models(connection, run_ids) if run_ids else {}
    return {"batch_id": batch_id, "total_runs": len(runs), "configs": configs, "models": models}


def _pct(sorted_vals: list, q: float) -> float:
    """Nearest-rank percentile of an already-sorted list (q in [0,1])."""
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def _aggregate_models(connection, run_ids: list) -> dict:
    """Per-MODEL latency + cost across the batch — the raw time-vs-budget signal for
    deciding offerings. latency_ms is per LLM call (a repair loop makes MORE calls, so a
    model's total time = avg_latency × calls scales with iterations). p90 flags tail risk.
    """
    rows = connection.execute(
        text("""
            SELECT COALESCE(model, '(unknown)') AS model,
                   COUNT(*) AS calls,
                   ROUND(AVG(latency_ms)) AS avg_latency_ms,
                   PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90_latency_ms,
                   COALESCE(SUM(latency_ms), 0) AS total_latency_ms,
                   COALESCE(SUM(cost_usd_micros), 0) AS total_cost_usd_micros,
                   COALESCE(SUM(CASE WHEN ok THEN 0 ELSE 1 END), 0) AS errors
              FROM llm_calls
             WHERE code_task_run_id = ANY(:ids)
             GROUP BY COALESCE(model, '(unknown)')
        """),
        {"ids": run_ids},
    ).mappings().all()
    out: dict = {}
    for r in rows:
        calls = int(r["calls"]) or 1
        out[r["model"]] = {
            "calls": int(r["calls"]),
            "avg_latency_ms": int(r["avg_latency_ms"] or 0),
            "p90_latency_ms": int(r["p90_latency_ms"] or 0),
            "total_latency_ms": int(r["total_latency_ms"]),
            "total_cost_usd_micros": int(r["total_cost_usd_micros"]),
            "avg_cost_usd_micros": int(int(r["total_cost_usd_micros"]) / calls),
            "errors": int(r["errors"]),
        }
    return out


def render_markdown(report: dict) -> str:
    """Human-readable bake-off report."""
    lines = [
        f"# Bake-off report — batch `{report['batch_id']}`",
        "",
        f"Total runs: {report['total_runs']}. Costs come from `llm_calls` only "
        "($1 = 1,000,000 micros); `est. calls` counts calls priced via the DEFAULT_PRICE "
        "fallback — add BOSS_MODEL_PRICES_JSON entries if it is non-zero.",
        "",
        "The three decision axes: **quality** (pass rate), **budget** ($/verified), "
        "**time** (s/verified = wall-clock per passing run; p90 = tail latency a user waits on).",
        "",
        "| config | runs | pass rate | $/verified | s/verified | mean/p90 wall | repairs used | total $ | false pass |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    def _usd(micros):
        return f"${micros / 1_000_000:.2f}" if micros is not None else "—"
    def _s(v):
        return f"{v:.0f}s" if v is not None else "—"
    def _hist(h):  # repair-attempt histogram: "n×count", omitting the all-zero common case
        h = {k: v for k, v in (h or {}).items() if k}
        return ", ".join(f"{n}×{cnt}" for n, cnt in sorted(h.items())) if h else "0"
    for name, c in sorted(report["configs"].items()):
        lines.append(
            f"| {name} | {c['runs']} | "
            f"{('%.0f%%' % (c['pass_rate'] * 100)) if c['pass_rate'] is not None else '—'} | "
            f"{_usd(c['cost_per_verified_outcome_usd_micros'])} | "
            f"{_s(c.get('wall_clock_per_verified_s'))} | "
            f"{_s(c['mean_wall_clock_s'])}/{_s(c.get('p90_wall_clock_s'))} | "
            f"{_hist(c.get('repair_hist'))} | {_usd(c['total_cost_usd_micros'])} | "
            f"{c['false_pass']}/{c['known_bad_runs']} |"
        )
    # Per-model latency × budget — the raw offering signal (a repair loop makes more calls,
    # so a slow model's total time compounds with each iteration).
    models = report.get("models") or {}
    if models:
        lines += ["", "### Per-model latency & cost (all calls in the batch)", "",
                  "| model | calls | avg latency | p90 latency | avg $/call | total $ | errors |",
                  "|---|---|---|---|---|---|---|"]
        for m, d in sorted(models.items(), key=lambda kv: -kv[1]["total_latency_ms"]):
            lines.append(
                f"| {m} | {d['calls']} | {d['avg_latency_ms'] / 1000:.1f}s | "
                f"{d['p90_latency_ms'] / 1000:.1f}s | {_usd(d['avg_cost_usd_micros'])} | "
                f"{_usd(d['total_cost_usd_micros'])} | {d['errors']} |")
    lines.append("")
    for name, c in sorted(report["configs"].items()):
        if c["failure_breakdown"]:
            lines.append(f"**{name} failures:** " + ", ".join(
                f"{gate}×{n}" for gate, n in sorted(c["failure_breakdown"].items())))
    return "\n".join(lines) + "\n"
