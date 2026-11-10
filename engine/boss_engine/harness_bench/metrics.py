"""What one benchmark task run measured, read from BOSS's own records, never the agent's output.

    llm_calls      tokens in/out, cache read/write, USD, model calls, engine-reported turns
    agent_turns    transcript rows and tool calls
    code_task_runs end status, pull request, merge decision, work branch
    quality_gates  BOSS's verify result, gate verdicts, review verdict
    events         repair rounds, checks-failing PRs, parked gates
    decisions      interventions: questions put to a person beyond the planned merge approval

Success is decided here too, from those records plus the benchmark's own acceptance checks
(`success_components`): an agent saying it is done counts for nothing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import text


@dataclass(frozen=True)
class Metric:
    name: str
    better: str          # "higher" or "lower"
    unit: str
    meaning: str
    #: How a suite pass aggregates per-task values: "sum" over tasks, or a ratio computed from
    #: summed parts (success_rate, cache_share).
    aggregate: str = "sum"


METRICS: tuple[Metric, ...] = (
    Metric("success_rate", "higher", "share",
           "runs that met every success component / runs", aggregate="ratio"),
    Metric("wall_s", "lower", "s", "wall time from start_code_task to the run's end"),
    Metric("tokens_in", "lower", "tokens",
           "input tokens: prompt + cache read + cache write (llm_calls)"),
    Metric("tokens_out", "lower", "tokens", "completion tokens (llm_calls)"),
    Metric("cache_share", "higher", "share", "cache_read_tokens / tokens_in", aggregate="ratio"),
    Metric("cost_usd", "lower", "USD", "sum of llm_calls.cost_usd_micros / 1e6 (list price)"),
    Metric("model_calls", "lower", "calls", "llm_calls rows for the run"),
    Metric("turns", "lower", "turns", "sum of llm_calls.turns (the engine's own turn count)"),
    Metric("repair_rounds", "lower", "rounds",
           "fix rounds BOSS ran: repair attempts, warm review rounds, implement continuations"),
    Metric("interventions", "lower", "decisions",
           "decisions asked of a person beyond the planned merge approval, plus parked gates"),
)
METRIC_BY_NAME = {m.name: m for m in METRICS}

#: The events that are a fix round BOSS ran (one event per round): a warm review round in the
#: same session, and a cold review-repair round (a new run in the lineage). The implement loop's
#: own retries are counted from quality_gates.evidence.repair_attempts, which has no event.
REPAIR_EVENTS = ("code_task.inrun_convergence_round", "code_task.review_repair_round")
#: A turn-cap continuation: more turns for the same attempt, reported but not a fix round.
CONTINUATION_EVENTS = ("code_task.implement_continued",)
#: A pull request that opened with its checks failing is not a success, whatever else passed.
CHECKS_FAILING_EVENTS = ("code_task.shipped_with_dissent", "code_task.pr_opened_with_checks_failing")
#: The merge question every completed build leaves: planned, never an intervention.
PLANNED_DECISION_PREFIXES = ("Approve code change:",)
#: Phases that wait for a person: each is an intervention (the benchmark answers nothing).
PARKED_PHASES = ("awaiting_gate_override", "awaiting_plan_approval")


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def summarize_calls(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """llm_calls rows -> token, cost and call totals, with a per-stage breakdown."""
    out = {"model_calls": 0, "turns": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd_micros": 0,
           "failed_calls": 0, "price_estimated_calls": 0, "included_calls": 0,
           "engines": [], "models": [], "routes": [], "by_stage": {}}
    engines: set[str] = set()
    models: set[str] = set()
    routes: set[str] = set()
    for r in rows:
        out["model_calls"] += 1
        out["turns"] += int(r.get("turns") or 0)
        for key in ("prompt_tokens", "completion_tokens", "cache_read_tokens",
                    "cache_write_tokens", "cost_usd_micros"):
            out[key] += int(r.get(key) or 0)
        out["failed_calls"] += 0 if r.get("ok", True) else 1
        out["price_estimated_calls"] += 1 if r.get("price_estimated") else 0
        out["included_calls"] += 1 if r.get("included_in_subscription") else 0
        for s, v in ((engines, r.get("engine")), (models, r.get("model")), (routes, r.get("route_id"))):
            if v:
                s.add(str(v))
        stage = str(r.get("stage") or "-")
        st = out["by_stage"].setdefault(stage, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                                "cost_usd_micros": 0})
        st["calls"] += 1
        st["tokens_in"] += (int(r.get("prompt_tokens") or 0) + int(r.get("cache_read_tokens") or 0)
                            + int(r.get("cache_write_tokens") or 0))
        st["tokens_out"] += int(r.get("completion_tokens") or 0)
        st["cost_usd_micros"] += int(r.get("cost_usd_micros") or 0)
    out["engines"], out["models"], out["routes"] = sorted(engines), sorted(models), sorted(routes)
    out["tokens_in"] = out["prompt_tokens"] + out["cache_read_tokens"] + out["cache_write_tokens"]
    out["tokens_out"] = out["completion_tokens"]
    out["cache_share"] = (round(out["cache_read_tokens"] / out["tokens_in"], 6)
                          if out["tokens_in"] else 0.0)
    out["cost_usd"] = out["cost_usd_micros"] / 1_000_000
    return out


def count_repair_rounds(event_types: Iterable[str], implement_retries: int = 0) -> int:
    """Fix rounds: the implement loop's retries plus one per warm or cold review round."""
    return int(implement_retries or 0) + sum(1 for e in event_types if e in REPAIR_EVENTS)


def count_interventions(decisions: Iterable[dict[str, Any]], *, merge_decision_id: Optional[str],
                        phase: Optional[str]) -> tuple[int, list[str]]:
    """(count, questions): decisions beyond the planned merge approval, plus a parked phase."""
    asked: list[str] = []
    for d in decisions:
        q = str(d.get("question") or "")
        if merge_decision_id and d.get("id") == merge_decision_id:
            continue
        if q.startswith(PLANNED_DECISION_PREFIXES):
            continue
        asked.append(q[:160])
    if phase in PARKED_PHASES:
        asked.append(f"(the run parked in {phase})")
    return len(asked), asked


def gate_state(gate_row: Optional[dict[str, Any]]) -> dict[str, Any]:
    """quality_gates row -> {status, tests_passed, review, passed: [...], failed: [...]}."""
    if not gate_row:
        return {"status": "missing", "tests_passed": False, "review": "not_run",
                "passed": [], "failed": [], "repair_attempts": 0}
    passed = _json(gate_row.get("gates_passed")) or []
    failed = _json(gate_row.get("gates_failed")) or []
    evidence = _json(gate_row.get("evidence")) or {}
    review = ("failed" if "code_review_passed" in failed
              else "passed" if "code_review_passed" in passed else "not_run")
    try:
        repair_attempts = int((evidence or {}).get("repair_attempts") or 0)
    except (TypeError, ValueError):
        repair_attempts = 0
    return {"status": gate_row.get("status") or "missing",
            "tests_passed": bool(gate_row.get("tests_passed")),
            "adversarial_verdict": gate_row.get("adversarial_verdict"),
            "review": review, "passed": sorted(passed), "failed": sorted(failed),
            "repair_attempts": repair_attempts}


def success_components(*, run: dict[str, Any], gates: dict[str, Any],
                       event_types: list[str], acceptance: dict[str, Any]) -> dict[str, bool]:
    """Every condition a successful task run meets. Success is all of them."""
    return {
        "completed": run.get("status") == "completed",
        "pr_opened": bool(run.get("pull_request_url")),
        "verify_passed": bool(gates.get("tests_passed")) and "written_tests_pass" not in gates.get("failed", []),
        "review_passed": gates.get("review") == "passed",
        "gates_passed": gates.get("status") == "pass",
        "no_checks_failing_pr": not any(e in CHECKS_FAILING_EVENTS for e in event_types),
        "acceptance_passed": bool(acceptance.get("passed")),
    }


def read_run(connection, run_id: str, *, project_id: str, since: Any = None,
             until: Any = None) -> dict[str, Any]:
    """Everything the benchmark reads about one code task run, as plain dicts."""
    run = connection.execute(text(
        "SELECT id, status, phase, error, pull_request_url, pull_request_number, decision_id, "
        "finalize_ctx, created_at, updated_at FROM code_task_runs WHERE id = :i"),
        {"i": run_id}).mappings().first()
    run = dict(run) if run else {}
    run["finalize_ctx"] = _json(run.get("finalize_ctx")) or {}
    calls = [dict(r) for r in connection.execute(text(
        "SELECT stage, engine, model, route_id, prompt_tokens, completion_tokens, "
        "cache_read_tokens, cache_write_tokens, cost_usd_micros, turns, ok, price_estimated, "
        "included_in_subscription FROM llm_calls WHERE code_task_run_id = :i"),
        {"i": run_id}).mappings()]
    turn_stats = connection.execute(text(
        "SELECT COUNT(*) AS rows, "
        "COALESCE(SUM(CASE WHEN direction = 'tool_call' THEN 1 ELSE 0 END), 0) AS tool_calls "
        "FROM agent_turns WHERE code_task_run_id = :i"), {"i": run_id}).mappings().first()
    gate_row = connection.execute(text(
        "SELECT status, tests_passed, adversarial_verdict, gates_passed, gates_failed, evidence "
        "FROM quality_gates WHERE code_task_run_id = :i"), {"i": run_id}).mappings().first()
    events = [dict(r) for r in connection.execute(text(
        "SELECT event_type, data, created_at FROM events WHERE entity_id = :i "
        "ORDER BY created_at, id"), {"i": run_id}).mappings()]
    for e in events:
        e["data"] = _json(e.get("data")) or {}
    decisions = []
    if since is not None:
        decisions = [dict(r) for r in connection.execute(text(
            "SELECT id, question, status FROM decisions WHERE project_id = :p "
            "AND created_at >= :since AND (CAST(:until AS TIMESTAMPTZ) IS NULL "
            "OR created_at <= :until) ORDER BY created_at"),
            {"p": project_id, "since": since, "until": until}).mappings()]
    return {"run": run, "calls": calls, "agent_turn_rows": int((turn_stats or {}).get("rows") or 0),
            "tool_calls": int((turn_stats or {}).get("tool_calls") or 0),
            "gate_row": dict(gate_row) if gate_row else None, "events": events,
            "decisions": decisions}


def merge_lineage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """One task's runs (the first build and any repair children it queued), as one record: every
    cost summed, every event kept, and the LAST run's status, pull request and gates — the task
    succeeded only if the lineage ended well."""
    if not records:
        raise ValueError("a lineage has at least one run")
    seen: set[str] = set()
    decisions = []
    for r in records:
        for d in r.get("decisions") or []:
            if d.get("id") not in seen:
                seen.add(d.get("id"))
                decisions.append(d)
    return {
        "run": records[-1]["run"],
        "runs": [r["run"].get("id") for r in records],
        "calls": [c for r in records for c in r["calls"]],
        "agent_turn_rows": sum(int(r.get("agent_turn_rows") or 0) for r in records),
        "tool_calls": sum(int(r.get("tool_calls") or 0) for r in records),
        "gate_row": records[-1]["gate_row"],
        "events": [e for r in records for e in r["events"]],
        "decisions": decisions,
        "implement_retries": sum(gate_state(r["gate_row"])["repair_attempts"] for r in records),
    }


def measure(record: dict[str, Any], *, wall_s: float,
            acceptance: dict[str, Any]) -> dict[str, Any]:
    """One task's metrics and success, from `read_run` (or `merge_lineage`) and the acceptance
    result."""
    run = record["run"]
    calls = summarize_calls(record["calls"])
    gates = gate_state(record["gate_row"])
    kinds = [e["event_type"] for e in record["events"]]
    retries = record.get("implement_retries", gates["repair_attempts"])
    interventions, asked = count_interventions(record["decisions"],
                                               merge_decision_id=run.get("decision_id"),
                                               phase=run.get("phase"))
    components = success_components(run=run, gates=gates, event_types=kinds, acceptance=acceptance)
    return {
        "success": all(components.values()),
        "components": components,
        "status": run.get("status"),
        "phase": run.get("phase"),
        "error": (str(run.get("error") or "")[:500] or None),
        "pull_request_url": run.get("pull_request_url"),
        "metrics": {
            "wall_s": round(float(wall_s), 2),
            "tokens_in": calls["tokens_in"],
            "tokens_out": calls["tokens_out"],
            "cache_read_tokens": calls["cache_read_tokens"],
            "cache_write_tokens": calls["cache_write_tokens"],
            "cache_share": calls["cache_share"],
            "cost_usd": calls["cost_usd"],
            "model_calls": calls["model_calls"],
            "turns": calls["turns"],
            "tool_calls": record["tool_calls"],
            "agent_turn_rows": record["agent_turn_rows"],
            "repair_rounds": count_repair_rounds(kinds, retries),
            "continuations": sum(1 for k in kinds if k in CONTINUATION_EVENTS),
            "lineage_runs": len(record.get("runs") or [run.get("id")]),
            "interventions": interventions,
        },
        "calls": {k: calls[k] for k in ("by_stage", "engines", "models", "routes", "failed_calls",
                                        "price_estimated_calls", "included_calls")},
        "gates": gates,
        "interventions_asked": asked,
        "events": sorted(set(k for k in kinds if k.startswith("code_task."))),
    }
