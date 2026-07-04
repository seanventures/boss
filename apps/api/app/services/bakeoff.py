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
                   q.status AS gate_status, q.gates_failed
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
        })
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
        wall = c.pop("wall_clock_s")
        c["mean_wall_clock_s"] = round(sum(wall) / len(wall), 1) if wall else None
        c["pass_rate"] = round(c["gate_pass"] / c["runs"], 3) if c["runs"] else None
        c["cost_per_verified_outcome_usd_micros"] = (
            int(c["total_cost_usd_micros"] / c["gate_pass"]) if c["gate_pass"] else None
        )
    return {"batch_id": batch_id, "total_runs": len(runs), "configs": configs}


def render_markdown(report: dict) -> str:
    """Human-readable bake-off report."""
    lines = [
        f"# Bake-off report — batch `{report['batch_id']}`",
        "",
        f"Total runs: {report['total_runs']}. Costs come from `llm_calls` only "
        "($1 = 1,000,000 micros); `est. calls` counts calls priced via the DEFAULT_PRICE "
        "fallback — add BOSS_MODEL_PRICES_JSON entries if it is non-zero.",
        "",
        "| config | runs | pass rate | $/verified outcome | total $ | retries | mean wall clock | false pass | est. calls |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    def _usd(micros):
        return f"${micros / 1_000_000:.2f}" if micros is not None else "—"
    for name, c in sorted(report["configs"].items()):
        lines.append(
            f"| {name} | {c['runs']} | "
            f"{('%.0f%%' % (c['pass_rate'] * 100)) if c['pass_rate'] is not None else '—'} | "
            f"{_usd(c['cost_per_verified_outcome_usd_micros'])} | "
            f"{_usd(c['total_cost_usd_micros'])} | {c['retries']} | "
            f"{str(c['mean_wall_clock_s']) + 's' if c['mean_wall_clock_s'] is not None else '—'} | "
            f"{c['false_pass']}/{c['known_bad_runs']} | {c['price_estimated_calls']} |"
        )
    lines.append("")
    for name, c in sorted(report["configs"].items()):
        if c["failure_breakdown"]:
            lines.append(f"**{name} failures:** " + ", ".join(
                f"{gate}×{n}" for gate, n in sorted(c["failure_breakdown"].items())))
    return "\n".join(lines) + "\n"
