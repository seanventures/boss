"""ONE BENCHMARK RECORD PER WORKFLOW RUN (BOSS_OSS_v1 stack recommendations §8).

    scenario, stack, source_commit, models, run_id, result, pr_url, tests_passed, tests_total,
    human_approvals, manual_interventions, elapsed_minutes, model_cost_usd, retries,
    deployment_smoke_test

Built only from what BOSS already stored about the run — run_retrospective.assemble_workflow (the
run report's own numbers: elapsed time, cost, models, the build and its PR), the build's quality
gate, the decisions people answered, the events an operator's hand left, the deploy and smoke-test
steps. A value BOSS did not measure is null, never a guess and never a zero standing in for
"unknown": a benchmark that rounds the unknown to zero is a benchmark nobody can trust.

`stack` is the adapter id (services.stacks.ADAPTERS) the build's services matched; it is what the
promotion rule (stacks.PROMOTION_RULE, stacks.certification_evidence) reads.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

FIELDS = ("scenario", "stack", "source_commit", "models", "run_id", "result", "pr_url",
          "tests_passed", "tests_total", "human_approvals", "manual_interventions",
          "elapsed_minutes", "model_cost_usd", "retries", "deployment_smoke_test")

SUCCESS = frozenset({"completed"})
FAILURE = frozenset({"failed", "deadlettered", "cancelled", "compensated"})
#: What an operator's hand looks like in the run's events.
INTERVENTION_EVENTS = ("workflow.step_retried", "workflow.step_skipped", "workflow.run_resumed")
BUILD_INTERVENTION_EVENTS = ("code_task.steered",)
RETRY_EVENTS = ("workflow.step_retried", "workflow.step_retry_scheduled")
_DEPLOY_OK = frozenset({"succeeded", "healthy"})
_DEPLOY_BAD = frozenset({"failed", "blocked", "rolled_back", "unhealthy", "rejected"})


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _builds(steps: list[dict[str, Any]]) -> list[str]:
    return [str(s["work_id"]) for s in steps if s.get("kind") == "code_task" and s.get("work_id")]


def _stack_id(connection: Connection, run_input: dict, build_id: Optional[str]) -> Optional[str]:
    from . import stacks

    if build_id:
        state = connection.execute(text("SELECT state FROM code_task_runs WHERE id = :i"),
                                   {"i": build_id}).scalar()
        services = (_dict(_dict(state).get("stack_verify")).get("services")
                    if isinstance(state, dict) else None)
        if isinstance(services, list) and services:
            found = stacks.adapter_for(services)
            if found:
                return found.id
    tpl = _dict(run_input.get("template"))
    if tpl.get("stack"):
        found = stacks.adapter_for_template(str(tpl["stack"]), tpl.get("framework") or None)
        return found.id if found else None
    return None


def _source_commit(connection: Connection, run_id: str, steps_output: list[dict]) -> Optional[str]:
    sha = connection.execute(text(
        "SELECT head_sha FROM ci_waits WHERE workflow_run_id = :r AND head_sha IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1"), {"r": run_id}).scalar()
    if sha:
        return str(sha)
    for out in steps_output:
        evidence = out.get("evidence") if isinstance(out.get("evidence"), dict) else out
        if isinstance(evidence, dict) and evidence.get("commit"):
            return str(evidence["commit"])
    return None


def _tests(connection: Connection, build_id: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Counted test CASES (a JUnit report BOSS parsed), never commands. Null when none was."""
    if not build_id:
        return None, None
    evidence = connection.execute(text(
        "SELECT evidence FROM quality_gates WHERE code_task_run_id = :i "
        "ORDER BY created_at DESC LIMIT 1"), {"i": build_id}).scalar()
    tt = _dict(_dict(_dict(evidence).get("template")).get("template_tests"))
    if isinstance(tt.get("tests"), int) and isinstance(tt.get("passed"), int):
        return int(tt["passed"]), int(tt["tests"])
    return None, None


def _approvals(connection: Connection, project_id: str, run_id: str) -> int:
    from .agent_identity import is_person

    rows = connection.execute(text("""
        SELECT d.approved_by FROM decisions d
         WHERE d.project_id = :p AND d.status IN ('approved', 'executed', 'completed')
           AND d.approved_by IS NOT NULL
           AND (d.recommendation->>'workflow_run_id' = :r
                OR d.id IN (SELECT decision_id FROM workflow_step_runs
                             WHERE workflow_run_id = :r AND decision_id IS NOT NULL)
                OR d.recommendation->>'architecture_review_id' IN (
                     SELECT id FROM architecture_reviews WHERE workflow_run_id = :r))
    """), {"p": project_id, "r": run_id}).all()
    return sum(1 for (who,) in rows if is_person(who, connection, project_id))


def _count_events(connection: Connection, types: tuple[str, ...], entity_type: str,
                  ids: list[str], *, operator_only: bool = False) -> int:
    if not ids:
        return 0
    from sqlalchemy import bindparam

    rows = connection.execute(text(
        "SELECT event_type, data FROM events WHERE entity_type = :t AND entity_id IN :ids "
        "AND event_type IN :types").bindparams(bindparam("ids", expanding=True),
                                               bindparam("types", expanding=True)),
        {"t": entity_type, "ids": ids, "types": list(types)}).all()
    count = 0
    for event_type, data in rows:
        if (operator_only and event_type == "workflow.step_skipped"
                and not _dict(data).get("by_operator")):
            continue  # the system skipping a step is not a person's hand
        count += 1
    return count


def _repairs(connection: Connection, builds: list[str]) -> Optional[int]:
    total, known = 0, False
    for build in builds:
        state = connection.execute(text("SELECT state FROM code_task_runs WHERE id = :i"),
                                   {"i": build}).scalar()
        value = _dict(state).get("repair_attempts")
        if isinstance(value, int):
            total, known = total + value, True
    return total if known else None


def _smoke(connection: Connection, rows: list[dict]) -> str:
    """pass | fail | not-run: the smoke-test step's probe, else the deployment's health checks."""
    verdict = None
    for row in rows:
        out = _dict(row["output"])
        if "reachable" in out or row["step_id"].startswith("smoke"):
            if row["status"] == "succeeded" and out.get("reachable") is not False:
                verdict = "pass"
            elif row["status"] == "failed":
                verdict = "fail"
    if verdict:
        return verdict
    for row in rows:
        awaited = _dict(_dict(row["output"]).get("await"))
        kind, work = str(awaited.get("kind") or ""), str(awaited.get("id") or "")
        table = {"deployment": "deployment_runs", "stack_deployment": "stack_deployments"}.get(kind)
        if not table or not work:
            continue
        status = connection.execute(text(f"SELECT status FROM {table} WHERE id = :i"),
                                    {"i": work}).scalar()
        if status in _DEPLOY_OK:
            verdict = "pass"
        elif status in _DEPLOY_BAD:
            verdict = "fail"
    return verdict or "not-run"


def record(connection: Connection, project_id: str, workflow_run_id: str) -> dict[str, Any]:
    """The benchmark record of one workflow run. {"ok": False, "error"} for an unknown run."""
    from .action_preview import run_title
    from .run_retrospective import assemble_workflow

    report = assemble_workflow(connection, project_id, workflow_run_id)
    if not report.get("ok"):
        return report
    run = connection.execute(text("SELECT input, status FROM workflow_runs WHERE id = :r"),
                             {"r": workflow_run_id}).mappings().first() or {}
    run_input = _dict(run.get("input"))
    rows = [dict(r) for r in connection.execute(text(
        "SELECT step_id, status, output FROM workflow_step_runs WHERE workflow_run_id = :r "
        "ORDER BY COALESCE(started_at, completed_at, created_at)"),
        {"r": workflow_run_id}).mappings().all()]
    steps = report.get("steps") or []
    builds = _builds(steps)
    latest = builds[-1] if builds else None
    status = str(report.get("status") or "")
    build = _dict(_dict(report.get("product")).get("build"))
    pr_url = build.get("pull_request_url")
    if not pr_url and latest:
        pr_url = connection.execute(text("SELECT pull_request_url FROM code_task_runs WHERE id = :i"),
                                    {"i": latest}).scalar()
    passed, total = _tests(connection, latest)
    models = sorted({m for c in report.get("contributors") or [] for m in c.get("models") or []})
    step_retries = _count_events(connection, RETRY_EVENTS, "workflow_run", [workflow_run_id])
    repairs = _repairs(connection, builds)
    finished = status in SUCCESS | FAILURE
    return {
        "scenario": run_title(run_input, str(report.get("workflow") or "")) or None,
        "workflow": report.get("workflow"),
        "stack": _stack_id(connection, run_input, latest),
        "source_commit": _source_commit(connection, workflow_run_id,
                                        [_dict(r["output"]) for r in rows]),
        "models": models,
        "run_id": workflow_run_id,
        "status": status,
        "result": "success" if status in SUCCESS else "failure" if status in FAILURE else None,
        "pr_url": pr_url or None,
        "tests_passed": passed,
        "tests_total": total,
        "human_approvals": _approvals(connection, project_id, workflow_run_id),
        "manual_interventions": (
            _count_events(connection, INTERVENTION_EVENTS, "workflow_run", [workflow_run_id],
                          operator_only=True)
            + _count_events(connection, BUILD_INTERVENTION_EVENTS, "code_task_run", builds)),
        # Elapsed so far while the run is live; its whole span once it ended.
        "elapsed_minutes": report.get("elapsed_minutes"),
        "elapsed_final": finished,
        "model_cost_usd": report.get("total_usd"),
        "retries": step_retries + (repairs or 0),
        "retries_detail": {"step_retries": step_retries, "build_repairs": repairs},
        "deployment_smoke_test": _smoke(connection, rows),
    }


def as_yaml(rec: dict[str, Any]) -> str:
    """The record in the §8 shape, for the end of the run report."""
    import yaml

    return yaml.safe_dump({k: rec.get(k) for k in FIELDS}, sort_keys=False,
                          default_flow_style=False).strip()
