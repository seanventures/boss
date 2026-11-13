"""Eval-harness — regression suites that PROVE the quality of the moats that the self-improvement
runtime gates on: routing (a real fallback chain, never a silent dead-end), policy (the audit trail
round-trips), the software factory (its quality gate fails closed), and the autonomy ladder (its
capability levels stay monotonic). Run on demand or nightly; results persist to eval_runs and gate
self-improvement rollouts (spec §C: eval-harness regression suites + evidence-gated rollouts).

Like the canary suite these run for real against live primitives/config and are deterministic — no
external model/GitHub calls — so they can gate a rollout without burning budget or flaking.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from ..db import begin
from .core import emit_event, make_id, record_policy_eval
from .entitlements import EntitlementBrokerService
from .role_governance import RoleGovernanceService
from .software_factory import SoftwareFactoryService


def _eval_routing_fallback_chain(engine, settings, project_id):
    """Routing regression: every route decision is logged and resolves to a structured outcome
    (a selected source OR an explicit denial) with a ranked fallback chain — never a silent dead-end."""
    broker = EntitlementBrokerService(settings)
    with begin(engine) as c:
        route = broker.preview_route(
            c,
            project_id,
            skill_id="model.generate_text",
            task_kind="code",
            workload_class="owner_sandbox",
            risk_tier=1,
            actor="eval_harness",
        )
    logged = bool(route.get("route_decision_log_id"))
    structured = bool(route.get("source_id")) or bool(route.get("denial_reason"))
    fallback_order = (route.get("decision") or {}).get("fallback_order") or []
    eligible = (route.get("decision") or {}).get("eligible_sources") or []
    ok = logged and structured
    metrics = {
        "logged": logged,
        "resolved": bool(route.get("source_id")),
        "fallback_depth": len(fallback_order),
        "eligible_sources": len(eligible),
        "rejected_sources": len(route.get("rejected_sources") or []),
    }
    detail = (
        f"logged={logged} selected={route.get('source_id') or route.get('denial_reason')} "
        f"fallback_depth={len(fallback_order)} eligible={len(eligible)}"
    )
    return ok, detail, metrics


def _eval_policy_audit_roundtrip(engine, settings, project_id):
    """Policy regression: a policy evaluation is durably recorded to policy_eval_log and reads back
    with the same decision/boundary — the audit trail behind 'why did we do that' never loses a row."""
    boundary = "eval.policy_roundtrip"
    with begin(engine) as c:
        eval_id = record_policy_eval(
            c, project_id, boundary, "deny",
            action="eval_harness_probe", reason="eval-harness roundtrip probe",
            detail={"probe": True})
        row = c.execute(
            text("SELECT boundary, decision FROM policy_eval_log WHERE id = :i"),
            {"i": eval_id},
        ).mappings().first()
    ok = row is not None and row["boundary"] == boundary and row["decision"] == "deny"
    return ok, f"policy_eval {eval_id} → {dict(row) if row else 'MISSING'}", {"recorded": bool(row)}


def _eval_factory_gate_fail_closed(engine, settings, project_id):
    """Factory-quality regression: a failing sandbox validation must yield a do-not-merge
    recommendation and a low-confidence verdict — the quality gate fails closed, never open."""
    factory = SoftwareFactoryService(settings)
    rec = factory._review_recommendation_for_status("failed")
    confidence = factory._review_confidence_for_status("failed")
    passed_rec = factory._review_recommendation_for_status("passed")
    blocks_on_fail = "do not merge" in rec.lower()
    fail_distinct = rec != passed_rec
    low_confidence = "failed" in confidence
    ok = blocks_on_fail and fail_distinct and low_confidence
    return ok, f"failed→'{rec}' ({confidence}); passed→'{passed_rec}'", {
        "blocks_on_fail": blocks_on_fail, "fail_distinct_from_pass": fail_distinct,
        "low_confidence_on_fail": low_confidence}


def _eval_autonomy_ladder_monotonic(engine, settings, project_id):
    """Autonomy regression: each ladder level's capabilities are a superset of the level below
    (no privilege gap), and an upgrade still requires an explicit human decision."""
    gov = RoleGovernanceService(settings)
    policy = gov._load_autonomy_policy()
    levels = sorted(int(k) for k in (policy.get("levels") or {}).keys() if str(k).isdigit())
    monotonic = True
    breaks = []
    for lower, higher in zip(levels, levels[1:]):
        lower_caps = set(gov._expanded_allowed_capabilities(policy, lower))
        higher_caps = set(gov._expanded_allowed_capabilities(policy, higher))
        if not lower_caps.issubset(higher_caps):
            monotonic = False
            breaks.append(f"{higher} lost {sorted(lower_caps - higher_caps)}")
    explicit = bool((policy.get("upgrade_requires") or {}).get("explicit_decision_required"))
    ok = monotonic and explicit and len(levels) >= 2
    detail = (
        f"levels={levels} monotonic={monotonic} explicit_decision={explicit}"
        + (f" breaks={breaks}" if breaks else "")
    )
    return ok, detail, {"levels": len(levels), "monotonic": monotonic, "explicit_decision": explicit}


EVALS = [
    ("routing_fallback_chain", _eval_routing_fallback_chain),
    ("policy_audit_roundtrip", _eval_policy_audit_roundtrip),
    ("factory_gate_fail_closed", _eval_factory_gate_fail_closed),
    ("autonomy_ladder_monotonic", _eval_autonomy_ladder_monotonic),
]


def run_eval_suite(engine, settings, project_id: str, actor: str = "eval_harness",
                   suite: str = "all") -> dict:
    results = []
    for name, fn in EVALS:
        try:
            ok, detail, metrics = fn(engine, settings, project_id)
            results.append({"eval": name, "status": "pass" if ok else "fail",
                            "detail": detail, "metrics": metrics})
        except Exception as exc:  # noqa: BLE001 — an eval error is an eval failure, not a crash
            results.append({"eval": name, "status": "fail",
                            "detail": f"error: {exc}", "metrics": {}})

    with begin(engine) as c:
        return record_eval_run(c, project_id, suite, results, actor)


def record_eval_run(connection, project_id: str, suite: str, results: list[dict],
                    actor: str) -> dict:
    """One eval_runs row + its event, for the suite above and for a skill's saved test cases
    (run_skill_cases) alike — one record of "what was evaluated, and how it went"."""
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    overall = "fail" if failed else "pass"
    run_id = make_id("eval")
    connection.execute(
        text(
            """
            INSERT INTO eval_runs (id, project_id, suite, status, results, passed, failed, skipped)
            VALUES (:id, :p, :su, :st, CAST(:r AS JSONB), :pa, :fa, :sk)
            """
        ),
        {"id": run_id, "p": project_id, "su": suite, "st": overall,
         "r": json.dumps(results, default=str), "pa": passed, "fa": failed, "sk": skipped},
    )
    emit_event(connection, project_id, "eval.suite_run", actor, "eval_run", run_id,
               {"suite": suite, "status": overall, "passed": passed,
                "failed": failed, "skipped": skipped})
    return {"id": run_id, "suite": suite, "status": overall, "passed": passed,
            "failed": failed, "skipped": skipped, "results": results}


# ── A skill's saved test cases (BOSSAPP-844) ──────────────────────────────────────────────────
#
# A case is run against ONE version's content through the executor that version's behaviour runs
# through (the same road a workflow step takes, skill_executors), with the version's content put
# in front of the live row for the duration. Only what is safe to run on a person's "Run tests"
# press is executed: a guide (returns its text), a knowledge-only builtin (its knowledge), an
# agent program and a sandboxed command (their own isolation, which refuses where there is none).
# A skill that reaches outside — another builtin executor, an MCP tool — is `skipped` with the
# reason: a test press must never publish, pay or post.

RUNNABLE_IN_TESTS = ("guide", "agent_program", "sandboxed_command")


def execute_skill_case(connection, settings, project_id: str, skill: dict, content: dict,
                       case_input: dict, actor: str) -> dict:
    """{status: ran|error|skipped, output}. Never raises: a failing case is a result."""
    from .skill_executors import SkillExecutorContext, behaviour_executor
    from .skill_versions import VERSION_OVERRIDE

    behaviour = str(content.get("behaviour_kind") or "")
    config = content.get("config") or {}
    if behaviour == "builtin" and str(config.get("executor_id") or "") == "knowledge.only":
        return {"status": "ran", "output": {"text": str(content.get("knowledge") or "")}}
    if behaviour not in RUNNABLE_IN_TESTS:
        return {"status": "skipped", "output": {
            "reason": f"a {behaviour} skill reaches outside BOSS, so a test press does not run "
                      "it — try it in a workflow run instead."}}
    handler = behaviour_executor(behaviour)
    if handler is None:
        return {"status": "error", "output": {"error": f"no executor for {behaviour}"}}
    info = connection.info
    info[VERSION_OVERRIDE] = {"skill_key": skill["skill_key"], "content": content}
    try:
        with connection.begin_nested():
            ctx = SkillExecutorContext(settings=settings, connection=connection,
                                       project_id=project_id, actor=actor)
            output = handler(ctx, {**(case_input or {}), "_skill_key": skill["skill_key"]})
        return {"status": "ran", "output": output if isinstance(output, dict)
                else {"result": output}}
    except Exception as exc:  # noqa: BLE001 — an error is the case's outcome
        return {"status": "error", "output": {"error": f"{type(exc).__name__}: {exc}"[:500]}}
    finally:
        info.pop(VERSION_OVERRIDE, None)


def run_skill_cases(connection, settings, project_id: str, skill: dict, version: dict,
                    cases: list[dict], actor: str) -> dict:
    """Run every case against `version`; recorded as one eval run (suite skill:<key>@v<n>).
    Returns {eval_run, outcomes: [{case_id, status, output}]}."""
    outcomes = []
    results = []
    for case in cases:
        done = execute_skill_case(connection, settings, project_id, skill, version["content"],
                                  case.get("input") or {}, actor)
        outcomes.append({"case_id": case["id"], **done})
        # Ungraded until a person (or a typed decision) grades it: an eval row says ran/skipped.
        results.append({"eval": case["name"],
                        "status": "skipped" if done["status"] == "skipped" else
                                  ("fail" if done["status"] == "error" else "pass"),
                        "detail": done["status"], "metrics": {}})
    run = record_eval_run(connection, project_id,
                          f"skill:{skill['skill_key']}@v{version['version_no']}", results, actor)
    return {"eval_run": run, "outcomes": outcomes}


def list_eval_runs(engine, project_id: str, limit: int = 20) -> list[dict]:
    with begin(engine) as c:
        rows = c.execute(
            text(
                """
                SELECT id, suite, status, results, passed, failed, skipped, created_at
                FROM eval_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT :l
                """
            ),
            {"p": project_id, "l": limit},
        ).mappings().all()
    return [dict(r) for r in rows]
