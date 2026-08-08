"""Task Shepherd — Phase 1: observe, hypothesise, CHECK, classify, propose. Execute nothing.

Design: docs/product/TASK_SHEPHERD_DESIGN.md

Phase 1 exists so the reasoning can be judged before it is trusted with a budget. Every
conclusion lands in `shepherd_escalations` as "I would do X, because Y, costing ~$Z" for a
human to accept or reject. Nothing here calls a write tool.

The division of labour is deliberate and is the whole design:

    this module + task_shepherd.py   gather evidence, CHECK hypotheses against data,
                                     classify, apply the ledger, compute cost and bounds
    the model                        explain WHY, in language an operator can argue with

The model never decides whether to continue, never picks an action outside the ledger, and
never sets a price. It is asked to reason about evidence that has already been gathered, and
its output is narrative attached to a decision the code already made. That is what keeps an
"intelligent" shepherd from being an unbounded one.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import emit_event, make_id
from .task_shepherd import (
    CLASS_NAMES, NOT_A_FAILURE, ShepherdState, Verdict, classify, escalation,
    estimate_cost, needs_operator_confirmation, next_step, task_budget,
)


# --------------------------------------------------------------------------- #
# Observe — gather the evidence a human would look at
# --------------------------------------------------------------------------- #
def signals(connection, run_id: str) -> dict[str, Any]:
    """Everything an engineer would check before forming an opinion, in one query pass.

    Chosen from what actually diagnosed the session's incidents: stage repetition revealed
    duplicate execution, lease-owner changes confirmed it, and phase-moving-backwards was the
    symptom that started it. A shepherd that cannot see these is guessing.
    """
    run = connection.execute(
        sql("SELECT id, project_id, status, phase, title, error, pull_request_url, "
            "state, finalize_ctx, state_version, owner_token, "
            "EXTRACT(EPOCH FROM (now() - created_at))/60 AS age_min, "
            "EXTRACT(EPOCH FROM (now() - updated_at))/60 AS idle_min "
            "FROM code_task_runs WHERE id = :id"), {"id": run_id}).mappings().first()
    if not run:
        return {}

    stages = connection.execute(
        sql("SELECT stage, count(*) AS calls, sum(cost_usd_micros)/1e6 AS usd, "
            "sum(turns) AS turns FROM llm_calls WHERE code_task_run_id = :id "
            "GROUP BY stage ORDER BY min(created_at)"), {"id": run_id}).mappings().all()

    fctx = run["finalize_ctx"] or {}
    if isinstance(fctx, str):
        try:
            fctx = json.loads(fctx)
        except Exception:  # noqa: BLE001
            fctx = {}

    repeated = {s["stage"]: int(s["calls"]) for s in stages if int(s["calls"]) > 1}
    spent = round(sum(float(s["usd"] or 0) for s in stages), 2)

    return {
        "run_id": run["id"],
        "project_id": run["project_id"],
        "status": run["status"],
        "phase": run["phase"],
        "title": run["title"],
        "error": run["error"] or "",
        "has_pr": bool(run["pull_request_url"]),
        "pull_request_url": run["pull_request_url"],
        "age_min": round(float(run["age_min"] or 0), 1),
        "idle_min": round(float(run["idle_min"] or 0), 1),
        "spent_usd": spent,
        "stages": [{"stage": s["stage"], "calls": int(s["calls"]),
                    "usd": round(float(s["usd"] or 0), 2)} for s in stages],
        # The tell for duplicate execution: the same stage completing more than once. This is
        # what two write_tests calls 29 seconds apart looked like in the data.
        "repeated_stages": repeated,
        # A burst here means two executions were writing the same run.
        "state_version": int(run["state_version"] or 0),
        "checkpointed": bool(fctx.get("work_branch")),
        "completed_stages": list(fctx.get("completed_stages") or []),
        "attempt": int(fctx.get("attempt") or 1),
        "recorded_failure_class": fctx.get("shepherd_class"),
        "failing_gates": list(fctx.get("advisory_failing_gates")
                              or fctx.get("failing_gates") or []),
    }


# --------------------------------------------------------------------------- #
# Check — a hypothesis must be tested, not asserted
# --------------------------------------------------------------------------- #
def check_hypothesis(sig: dict[str, Any], failure_class: int) -> tuple[bool, str]:
    """Confirm or refute the classification against a SECOND, independent observation.

    This step exists because of a specific mistake: during the session a run was declared "a
    zombie with 0 agents" from a single `ps` sample taken between stages. It was running fine.
    One observation is an impression; a hypothesis needs corroboration from a different signal
    than the one that suggested it.

    Returns (confirmed, what_was_checked). An unconfirmed hypothesis is not fatal — it lowers
    confidence and, for anything expensive, routes to a human.
    """
    from .task_shepherd import (
        DUPLICATE_EXECUTION, GATES_FAILED, NO_CODE, PR_STEP_FAILED, SPEND_CAP, STALE_BASE,
    )

    if failure_class == DUPLICATE_EXECUTION:
        rep = sig.get("repeated_stages") or {}
        if rep:
            return True, (f"corroborated: {', '.join(f'{k} ran {v}x' for k, v in rep.items())} "
                          "— a single execution cannot repeat a completed stage")
        return False, ("NOT corroborated: no stage ran more than once, so the error string is "
                       "the only evidence of duplication")

    if failure_class == SPEND_CAP:
        if sig.get("checkpointed"):
            return True, ("corroborated: work is checkpointed on the branch, so the cap "
                          "interrupted a run that was making progress")
        return False, ("NOT corroborated: nothing was checkpointed, so this may have failed "
                       "before doing any work")

    if failure_class == STALE_BASE:
        if sig.get("has_pr"):
            return True, "corroborated: a PR exists, so the work reached the merge step"
        return False, ("NOT corroborated: no PR was ever opened, so a stale BASE cannot be "
                       "the problem")

    # completed_stages only began being recorded on 2026-08-04. For a run that predates it the
    # field is empty because nothing was WRITING it, not because nothing happened — treating
    # that as evidence refutes correct classifications on every historical run. Absence of
    # instrumentation is not absence of the event.
    _instrumented = bool(sig.get("completed_stages")) or bool(sig.get("checkpointed"))

    if failure_class == PR_STEP_FAILED:
        if not _instrumented:
            return True, ("classification stands on the error text; this run predates stage "
                          "instrumentation, so the absence of completed_stages proves nothing")
        # Inverted on the first pass: "PR not created" means the step ran and FAILED, so the
        # absence of a PR is exactly what this class predicts. Asking for a PR as corroboration
        # would refute the hypothesis with its own definition.
        if not sig.get("has_pr") and sig.get("completed_stages"):
            return True, (f"corroborated: {len(sig['completed_stages'])} stage(s) completed but "
                          "no PR exists — the work was done and the PR call is what failed")
        if sig.get("has_pr"):
            return False, ("NOT corroborated: a PR exists, so the PR step did not fail")
        return False, ("NOT corroborated: no stage completed, so the run failed before it "
                       "could have reached the PR step")

    if failure_class == NO_CODE:
        if not _instrumented:
            return False, ("cannot corroborate: this run predates stage instrumentation, so "
                           "'nothing was built' cannot be distinguished from 'not recorded'")
        if not sig.get("completed_stages"):
            return True, "corroborated: no stage completed, consistent with nothing being built"
        return False, (f"NOT corroborated: {len(sig['completed_stages'])} stage(s) completed, "
                       "so something was built")

    if failure_class == GATES_FAILED:
        if sig.get("has_pr") and sig.get("failing_gates"):
            return True, ("corroborated: the PR shipped with findings attached — this is the "
                          "designed outcome, not a fault")
        return False, "NOT corroborated: expected a shipped PR carrying gate findings"

    return True, "no independent check defined for this class; classification stands on the error"


# --------------------------------------------------------------------------- #
# Propose — one step of reasoning, persisted
# --------------------------------------------------------------------------- #
def _shepherd_minutes(connection, run_id: str) -> float:
    """Minutes since this shepherd's FIRST reasoning step on this task. Zero if it has not
    started, however old the underlying failure is."""
    v = connection.execute(
        sql("SELECT EXTRACT(EPOCH FROM (now() - MIN(created_at)))/60 "
            "FROM shepherd_reasoning WHERE task_ref = :t"), {"t": run_id}).scalar()
    return float(v or 0.0)


def propose(engine, settings: Settings, run_id: str, *,
            model_narrator: Optional[Any] = None) -> dict[str, Any]:
    """Run one shepherd step against a run and PERSIST the reasoning. Executes nothing.

    Returns the reasoning record. When the verdict is to stop for a human, an escalation is
    written too — one decision-shaped question with costed options.
    """
    from ..db import begin

    with begin(engine) as c:
        sig = signals(c, run_id)
        if not sig:
            return {"error": "no such run", "run_id": run_id}
        project_id = sig["project_id"]

        cls = classify(sig["error"], gates_failed=sig["failing_gates"],
                       has_pr=sig["has_pr"],
                       recorded_class=sig.get("recorded_failure_class"))
        confirmed, check_note = check_hypothesis(sig, cls)

        budget = task_budget(c, project_id)
        used = {
            (int(r["failure_class"]), r["remedy_id"])
            for r in c.execute(
                sql("SELECT failure_class, remedy_id FROM shepherd_ledger "
                    "WHERE task_ref = :t AND status = 'used'"), {"t": run_id}).mappings()
        }
        history = [
            (int(r["failure_class"]), r["remedy_id"], r["outcome"] or "")
            for r in c.execute(
                sql("SELECT failure_class, remedy_id, outcome FROM shepherd_ledger "
                    "WHERE task_ref = :t ORDER BY created_at"), {"t": run_id}).mappings()
        ]
        state = ShepherdState(
            task_ref=run_id, budget_usd=budget, spent_usd=sig["spent_usd"],
            used_remedies=used, attempt_history=history,
            # NOT run age — the shepherd's own elapsed effort, which for a first
            # proposal is zero however old the failure is.
            shepherd_minutes=_shepherd_minutes(c, run_id),
        )
        verdict: Verdict = next_step(
            state, cls, succeeded=(sig["status"] == "completed" and sig["has_pr"]),
            failure_signature=(sig["error"] or "")[:120])

        # Cost is MEASURED for this project, never the constant.
        est = (estimate_cost(c, project_id, verdict.remedy, cls) if verdict.remedy
               and verdict.remedy.action else 0.0)
        confirm_needed = bool(verdict.remedy and verdict.remedy.action
                              and needs_operator_confirmation(verdict.remedy, state))

        observe = (f"{sig['status']} at {sig['phase']} after {sig['age_min']} min, "
                   f"${sig['spent_usd']} spent, attempt {sig['attempt']}"
                   f"{', PR open' if sig['has_pr'] else ', no PR'}")
        hypothesis = f"{CLASS_NAMES.get(cls, 'unknown')} ({sig['error'][:110]})"
        rationale = verdict.reason
        if model_narrator is not None:
            # The model explains; it does not decide. It is handed the evidence and the
            # decision already made, and asked to argue it in language the operator can push
            # back on. Its output can never change the verdict, the remedy, or the cost.
            try:
                rationale = model_narrator(sig, cls, verdict, check_note) or rationale
            except Exception:  # noqa: BLE001 — narration must never break the step
                pass

        step_no = int(c.execute(
            sql("SELECT COALESCE(MAX(step_no), 0) + 1 FROM shepherd_reasoning "
                "WHERE task_ref = :t"), {"t": run_id}).scalar() or 1)
        rid = make_id("shrz")
        c.execute(
            sql("INSERT INTO shepherd_reasoning (id, project_id, task_ref, step_no, observe, "
                "signals, hypothesis, confidence, check_result, check_confirmed, "
                "failure_class, remedy_chosen, rationale, predicted_cost_usd_micros, "
                "predicted_outcome, stopped_because) VALUES (:id, :p, :t, :n, :o, "
                "CAST(:sg AS JSONB), :h, :cf, :cr, :cc, :fc, :rm, :ra, :pc, :po, :sb)"),
            {"id": rid, "p": project_id, "t": run_id, "n": step_no, "o": observe,
             "sg": json.dumps(sig, default=str), "h": hypothesis,
             "cf": 0.85 if confirmed else 0.45, "cr": check_note, "cc": confirmed,
             "fc": cls, "rm": (verdict.remedy.id if verdict.remedy else None),
             "ra": rationale, "pc": int(est * 1e6),
             "po": (verdict.remedy.describe if verdict.remedy else None),
             "sb": verdict.stopped_because})

        esc_id = None
        # Escalate when a human is required, when the spend is large enough to deserve assent,
        # or when the check did NOT corroborate an expensive action. An unconfirmed hypothesis
        # must not be acted on with money.
        if verdict.stopped_because == "human" or confirm_needed or (
                not confirmed and est > 0):
            payload = escalation(run_id, cls, evidence={
                "observed": observe, "check": check_note, "confirmed": confirmed,
                "signals": {k: sig[k] for k in
                            ("phase", "age_min", "spent_usd", "repeated_stages",
                             "checkpointed", "has_pr") if k in sig},
                "error": sig["error"][:400],
            }, costs=({verdict.remedy.id: est} if verdict.remedy and est else None))
            esc_id = make_id("shesc")
            c.execute(
                sql("INSERT INTO shepherd_escalations (id, project_id, task_ref, question, "
                    "options, evidence) VALUES (:id, :p, :t, :q, CAST(:o AS JSONB), "
                    "CAST(:e AS JSONB))"),
                {"id": esc_id, "p": project_id, "t": run_id, "q": payload["question"],
                 "o": json.dumps(payload["options"]), "e": json.dumps(payload["evidence"],
                                                                     default=str)})

        # Phase 1 records what it WOULD do. The ledger row is marked available, never used —
        # nothing here spends anything.
        if verdict.remedy and verdict.remedy.action:
            c.execute(
                sql("INSERT INTO shepherd_ledger (id, project_id, task_ref, failure_class, "
                    "remedy_id, status) VALUES (:id, :p, :t, :fc, :rm, 'available') "
                    "ON CONFLICT (task_ref, failure_class, remedy_id) DO NOTHING"),
                {"id": make_id("shldg"), "p": project_id, "t": run_id, "fc": cls,
                 "rm": verdict.remedy.id})

        emit_event(c, project_id, "shepherd.proposed", "shepherd", "code_task_run", run_id,
                   {"class": cls, "class_name": CLASS_NAMES.get(cls),
                    "verdict": verdict.action, "remedy": verdict.remedy.id if verdict.remedy
                    else None, "confirmed": confirmed, "est_usd": est,
                    "escalation_id": esc_id, "step": step_no})

    return {
        "run_id": run_id, "step": step_no, "class": cls,
        "class_name": CLASS_NAMES.get(cls), "observe": observe,
        "hypothesis": hypothesis, "check": check_note, "confirmed": confirmed,
        "verdict": verdict.action, "stopped_because": verdict.stopped_because,
        "remedy": verdict.remedy.id if verdict.remedy else None,
        "would_do": verdict.remedy.describe if verdict.remedy else None,
        "estimated_cost_usd": est, "budget_usd": budget,
        "needs_confirmation": confirm_needed, "escalation_id": esc_id,
        "rationale": rationale,
    }


def sweep(engine, settings: Settings, project_id: str, *, limit: int = 20) -> list[dict]:
    """Propose for every run that ended without a PR and has no open escalation.

    Deliberately excludes NOT_A_FAILURE and anything already escalated — a shepherd that
    re-raises the same question is noise, and an operator who learns to ignore it is worse
    than no shepherd at all.
    """
    from ..db import begin
    with begin(engine) as c:
        rows = c.execute(
            sql("SELECT r.id FROM code_task_runs r "
                "WHERE r.project_id = :p AND r.status IN ('failed','blocked') "
                "  AND r.pull_request_url IS NULL "
                "  AND NOT EXISTS (SELECT 1 FROM shepherd_escalations e "
                "                  WHERE e.task_ref = r.id AND e.status = 'open') "
                "ORDER BY r.updated_at DESC LIMIT :l"),
            {"p": project_id, "l": limit}).mappings().all()
    out = []
    for row in rows:
        res = propose(engine, settings, row["id"])
        if res.get("class") != NOT_A_FAILURE:
            out.append(res)
    return out


# --------------------------------------------------------------------------- #
# Phase 2 — bounded autonomy
# --------------------------------------------------------------------------- #
# Only classes whose remedy is unambiguous AND whose failure is unmistakably the platform's
# fault. Everything else still escalates, however confident the shepherd feels. This list is
# the blast radius: it should grow only when calibration data justifies it (Phase 3), never
# because a remedy "usually works".
from .task_shepherd import (  # noqa: E402
    INFRASTRUCTURE as _INFRA, PR_STEP_FAILED as _PRSTEP, SPEND_CAP as _SPEND,
    TURN_CAP as _TURN,
)

AUTONOMOUS_CLASSES = frozenset({_SPEND, _TURN, _INFRA, _PRSTEP})


def act(engine, settings: Settings, jobs, run_id: str, *,
        actor: str = "shepherd", dry_run: bool = False) -> dict[str, Any]:
    """Propose, and then ACT only if every bound permits it.

    The refusals below are ordered cheapest-to-check first, and each one is a real incident:

      not autonomous      the class needs a human (approval, unknown, conflicts)
      not corroborated    the check failed — never spend on an unconfirmed diagnosis
      needs confirmation  the spend is large relative to this project's budget
      stop verdict        loop, budget, ledger or deadline already said no

    An executed remedy writes its ACTUAL cost and outcome back, which is what makes Phase 3
    calibration possible: a shepherd that cannot be measured cannot be trusted with more.
    """
    from ..db import begin

    p = propose(engine, settings, run_id)
    if p.get("error"):
        return p

    refusal = None
    if p["class"] not in AUTONOMOUS_CLASSES:
        refusal = f"class {p['class']} ({p['class_name']}) is not in the autonomous set"
    elif not p["confirmed"]:
        refusal = "the hypothesis was not corroborated; not spending on an unconfirmed diagnosis"
    elif p["needs_confirmation"]:
        refusal = "the estimated spend is large relative to this project's budget"
    elif p["verdict"] != "act":
        refusal = f"bounded: {p['stopped_because']}"
    elif not p["remedy"]:
        refusal = "no actionable remedy"

    if refusal or dry_run:
        p["acted"] = False
        p["refused_because"] = refusal or "dry_run"
        return p

    from .task_shepherd import REMEDIES
    remedy = next((r for r in REMEDIES[p["class"]] if r.id == p["remedy"]), None)
    before = _spent(engine, run_id)
    outcome, new_run = "error", None
    try:
        from .code_task_service import retry_code_task
        note = (f"Task Shepherd: {p['class_name']}. {p['check']} "
                f"Applying remedy {remedy.id!r}: {remedy.describe}.")
        if remedy.action in ("retry_code_task", "extend_code_task"):
            res = retry_code_task(engine, settings, jobs, run_id, fix_note=note,
                                  profile=("lean" if remedy.id == "reissue_lean" else None))
            new_run = res.get("run_id")
            outcome = "started"
        else:
            outcome = "no_action"
    except Exception as exc:  # noqa: BLE001 — a failed remedy is data, not a crash
        outcome = f"failed: {type(exc).__name__}: {exc}"[:200]

    with begin(engine) as c:
        c.execute(
            sql("UPDATE shepherd_ledger SET status='used', used_at=now(), outcome=:o, "
                "cost_usd_micros=:cu WHERE task_ref=:t AND failure_class=:fc AND remedy_id=:rm"),
            {"o": outcome, "cu": int(max(0.0, _spent(engine, run_id) - before) * 1e6),
             "t": run_id, "fc": p["class"], "rm": p["remedy"]})
        c.execute(
            sql("UPDATE shepherd_reasoning SET actual_outcome=:o WHERE task_ref=:t "
                "AND step_no=:n"), {"o": outcome, "t": run_id, "n": p["step"]})
        emit_event(c, p.get("project_id") or _project_of(engine, run_id), "shepherd.acted",
                   actor, "code_task_run", run_id,
                   {"remedy": p["remedy"], "class": p["class"], "outcome": outcome,
                    "successor_run": new_run, "predicted_usd": p["estimated_cost_usd"]})
    p.update({"acted": True, "outcome": outcome, "successor_run": new_run})
    return p


def _spent(engine, run_id: str) -> float:
    from ..db import begin
    with begin(engine) as c:
        v = c.execute(sql("SELECT COALESCE(SUM(cost_usd_micros),0)/1e6 FROM llm_calls "
                          "WHERE code_task_run_id=:i"), {"i": run_id}).scalar()
    return float(v or 0.0)


def _project_of(engine, run_id: str) -> str:
    from ..db import begin
    with begin(engine) as c:
        return c.execute(sql("SELECT project_id FROM code_task_runs WHERE id=:i"),
                         {"i": run_id}).scalar() or ""


# --------------------------------------------------------------------------- #
# Phase 3 — calibration. "Is it actually reasoning?" as a measurable question.
# --------------------------------------------------------------------------- #
def calibration(connection, project_id: str) -> dict[str, Any]:
    """Predicted vs actual, per remedy. The console's honesty panel.

    A shepherd that consistently under-estimates is not reasoning, it is pattern-matching with
    a confident tone — and under-estimating is the direction that starts work it cannot
    finish. Surfacing the ratio makes that impossible to hide behind a total.
    """
    rows = connection.execute(
        sql("""
            SELECT r.remedy_chosen AS remedy, r.failure_class,
                   count(*) AS n,
                   avg(r.predicted_cost_usd_micros)/1e6 AS predicted_usd,
                   avg(l.cost_usd_micros)/1e6 AS actual_usd,
                   count(*) FILTER (WHERE l.outcome = 'started') AS applied,
                   count(*) FILTER (WHERE r.check_confirmed) AS corroborated
            FROM shepherd_reasoning r
            LEFT JOIN shepherd_ledger l
                   ON l.task_ref = r.task_ref AND l.remedy_id = r.remedy_chosen
            WHERE r.project_id = :p AND r.remedy_chosen IS NOT NULL
            GROUP BY 1, 2 ORDER BY 3 DESC
        """), {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        pred = float(r["predicted_usd"] or 0)
        actual = float(r["actual_usd"] or 0)
        out.append({
            "remedy": r["remedy"], "failure_class": int(r["failure_class"] or 0),
            "n": int(r["n"]), "predicted_usd": round(pred, 2),
            "actual_usd": round(actual, 2),
            "ratio": round(actual / pred, 2) if pred else None,
            "corroborated_pct": round(100.0 * int(r["corroborated"]) / int(r["n"]), 0),
        })
    return {"project_id": project_id, "remedies": out}
