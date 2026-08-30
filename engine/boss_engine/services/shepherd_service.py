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
import logging
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import emit_event, make_id
from .jobs import JOB_PRIORITY_BACKGROUND

logger = logging.getLogger(__name__)

#: Most repairs the autopilot will start in one tick. The first live run fired six at once
#: into a worker that drains one job at a time, and the operator's meeting waited behind them.
MAX_ACTIONS_PER_TICK = 2

#: Above this many queued/running jobs the autopilot does nothing at all. Repair work is never
#: more urgent than the work already in flight, and a backlog that grows every 180s is worse
#: than a repair that waits.
QUEUE_DEPTH_PAUSE = 3

#: Who the audit trail credits for an automatic remedy. "shepherd" everywhere else in this
#: module, kept as one constant so a reader grepping the events can find every automatic act.
SHEPHERD_ACTOR = "shepherd"
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
        "remedy_action": verdict.remedy.action if verdict.remedy else None,
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


#: A job queued longer than this, with other work being claimed, is starved.
STARVED_AFTER_MINUTES = 12

#: A job holding a worker slot longer than this is wedged, not busy. Deliberately generous —
#: a real code task legitimately runs for over an hour.
WEDGED_AFTER_MINUTES = 120

#: NO PROGRESS is a better signal than elapsed time, and it is the one that was missing.
#: A run made a single llm_call at 16:56, wrote zero files, and sat in run_tests for 53
#: minutes — against a sandbox command timeout of 120 SECONDS, so nothing was actually
#: executing. Elapsed-time detection would not have touched it for two hours; "nothing has
#: happened since" caught it in twenty minutes. A long run that is still calling models or
#: still writing files is working, however long it takes.
NO_PROGRESS_MINUTES = 25

#: When SILENCE BECOMES VISIBLE — which is much earlier than when it is acted on.
#:
#: A run went quiet for 27m46s: no model call, no turn, no event, nothing. It recovered on its
#: own, so the 25-minute intervention above was arguably right not to have killed it. But for
#: those 27 minutes the console showed a phase counting up and no reason, and the honest reading
#: of a screen that says nothing is that nothing is happening.
#:
#: So quiet time is now REPORTED from five minutes and every five after, while the decision to
#: intervene stays exactly where it was. Saying "nothing has happened for 10 minutes" costs one
#: row and answers the question the blank could not.
QUIET_WARN_MINUTES = 5


#: Runs whose execution is a job. If the job is gone and the run still says it is working, the
#: run is lying — nothing will ever move it. Every one of these produced the same symptom at
#: some point today: a card that sits forever with no terminal state.
#:
#: (table, id column, the statuses that mean "still working")
_RUN_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # 'blocked' is included, and the no-live-job rule is what makes that safe.
    #
    # I excluded it earlier reasoning that blocked means "waiting on a human", which is a
    # legitimate resting state. That is true only WHILE A JOB EXISTS TO RESUME. A run blocked
    # on a decision whose job has gone can never proceed — approving the decision resumes
    # nothing — yet it renders as a live BLOCKED card, indistinguishable from a build that is
    # genuinely waiting for its operator. One such card sat next to a running build of the same
    # app and made the running one look blocked.
    ("code_task_runs", "id", ("running", "repairing", "queued", "blocked")),
    ("meeting_runs", "id", ("running", "queued")),
    ("discovery_runs", "id", ("running",)),
)

#: Grace period before a run with no job is called abandoned. Long enough that a run created
#: milliseconds before its job is never mistaken for an orphan.
ORPHAN_AFTER_MINUTES = 20


#: How many times a preview may fail for one run before the autopilot stops rebuilding it.
#:
#: `restart_preview` is the cheapest remedy, so it is tried FIRST — and the only thing that
#: suppressed it was an open escalation, which a preview failure never created. So it fired on
#: every 180s tick, forever: one run produced 28 consecutive failed previews over four hours,
#: each a full clone + npm install + build, every one the identical rollup parse error, all on
#: the single worker lane while the operator's real work queued behind them.
#:
#: Two is the limit because the SECOND identical failure is the evidence. The first can be a
#: cold cache or a lost port; a third attempt has never told us anything the second did not.
PREVIEW_MAX_ATTEMPTS = 2

#: How many attempts one LINEAGE may make before the shepherd stops retrying it.
#:
#: The per-run ledger cannot bound this. It refuses the same remedy on the same `task_ref`, and
#: `retry_code_task` mints a NEW run id for every attempt — so the child is a fresh task_ref with
#: an empty ledger and the same remedy applies again, and again. Bounded per instance, unbounded
#: per lineage. Unlike the preview loop this one SPENDS: $4-15 a link, unattended.
#:
#: MAX_ACTIONS_PER_TICK bounds the rate, not the total. The only thing preventing this today is
#: that autopilot defaults to off — a feature flag standing in for a limit.
#:
#: Three, because the second attempt is the one that carries the first's findings as feedback and
#: is therefore the one most likely to succeed; a fourth has never told us anything a third did not.
SHEPHERD_MAX_LINEAGE_ATTEMPTS = 3

#: A preview that has been 'starting' longer than this never came up. Previews are NOT
#: job-driven — start_preview runs inline — so the no-live-job rule cannot see them, and a row
#: left at 'starting' is what rendered "Starting preview…" forever. Generous: a cold npm
#: install plus a build legitimately takes minutes.
PREVIEW_STARTING_STALE_MINUTES = 25


def sweep_stale_previews(engine, settings: Settings, project_id: str, *,
                         actor: str = SHEPHERD_ACTOR,
                         dry_run: bool = False) -> dict[str, Any]:
    """Fail a preview stuck at 'starting'.

    The gap in sweep_abandoned_runs, and the same mistake in miniature: that sweeper
    enumerated the tables I had been looking at, and previews are governed by a different rule
    entirely. A preview has no job — start_preview builds and boots inline — so "no live job"
    is trivially true for EVERY preview and the no-job rule would have failed them all. Time is
    the only signal there is.
    """
    from ..db import begin

    with begin(engine) as c:
        rows = c.execute(
            sql("SELECT id FROM app_previews WHERE project_id = :p AND status = 'starting' "
                "  AND created_at < now() - (:m || ' minutes')::interval LIMIT 200"),
            {"p": project_id, "m": PREVIEW_STARTING_STALE_MINUTES}).mappings().all()
        ids = [r["id"] for r in rows]
        if ids and not dry_run:
            c.execute(
                sql("UPDATE app_previews SET status = 'failed', "
                    "error = COALESCE(error, :e), failure_kind = "
                    "COALESCE(failure_kind, 'never_started') WHERE id = ANY(:ids)"),
                {"ids": ids,
                 "e": (f"never left 'starting' after {PREVIEW_STARTING_STALE_MINUTES} minutes; "
                       "the process that was building it is gone")})
    return {"stale_previews": len(ids), "ids": ids[:10], "dry_run": dry_run}



#: Run tables that carry a `decision_id`. `discovery_runs` does NOT — and because the sweep's
#: per-table error handling swallows failures so one missing table cannot stop the rest, a
#: column reference that is wrong for one table would have SILENTLY DISABLED its sweep rather
#: than failing loudly. Kept as an explicit set, cross-checked against db/schema.sql by a test.
_TABLES_WITH_DECISIONS = frozenset({"code_task_runs", "meeting_runs"})


def _answerable_decision_clause(table: str) -> str:
    """SQL excluding runs that are waiting on a decision someone can still act on.

    A RUN WAITING ON AN ANSWERABLE DECISION IS NOT ABANDONED. Including 'blocked' in the sweep
    was justified as "approving a decision on a run whose job is gone resumes nothing". That
    premise was wrong: blocking on a plan gate ENDS the job by design (the handler returns
    done=True) and approving CREATES a new code.task job and flips the run back to running. So
    a run legitimately waiting for a person is indistinguishable from an orphan by the
    no-live-job rule alone, and the sweep would kill the very thing the operator was about to
    approve.

    `expired` is included in the answerable set because an expired decision is REOPENED by
    answering it (see R4) — a deadline nobody met must not also destroy the work.
    """
    if table not in _TABLES_WITH_DECISIONS:
        return ""
    return (" AND NOT EXISTS (SELECT 1 FROM decisions d "
            "                 WHERE d.id = r.decision_id "
            "                   AND d.status IN ('pending','delegated','expired')) ")

def sweep_abandoned_runs(engine, settings: Settings, project_id: str, *,
                         actor: str = SHEPHERD_ACTOR) -> dict[str, Any]:
    """Close any run that claims to be working but has no job that could advance it.

    THE PLATFORM-LAYER FIX for a symptom that appeared three separate ways in one afternoon:
    a preview button stuck on "Starting…", a convene button stuck on "Convening…", and a
    meeting card reading "PANEL SITTING" two hours after its job had gone. Each was fixed in
    the UI where it surfaced, which is treating the symptom: the UI was faithfully rendering a
    row that said `running`, and the row was wrong.

    A run's execution IS its job. When the job is cancelled, deadlettered or lost with the
    process, nothing remains that can advance the run — so a run in a working state with no
    live job is not in progress, it is abandoned, and saying so is the whole fix. No UI branch
    is needed for a state the data no longer produces.

    Deliberately generic over the run tables rather than written for code tasks: fixing
    code_task_runs and not meeting_runs is exactly the miss that produced the third instance.
    """
    from ..db import begin

    closed: list[dict[str, Any]] = []
    for table, id_col, working in _RUN_TABLES:
        try:
            with begin(engine) as c:
                rows = c.execute(
                    sql(f"SELECT r.{id_col} AS id, r.phase AS phase FROM {table} r "
                        f"WHERE r.project_id = :p AND r.status = ANY(:working) "
                        f"  AND r.updated_at < now() - (:m || ' minutes')::interval "
                        f"  AND NOT EXISTS (SELECT 1 FROM jobs j "
                        f"                  WHERE j.payload->>'run_id' = r.{id_col} "
                        f"                    AND j.status IN ('queued','running','leased')) "
                        # A RUN WAITING ON AN ANSWERABLE DECISION IS NOT ABANDONED.
                        #
                        # Including 'blocked' in the sweep was justified as "approving a
                        # decision on a run whose job is gone resumes nothing". That premise
                        # was WRONG. Blocking on a plan gate ENDS the job by design (the
                        # handler returns done=True), and approving CREATES A NEW code.task
                        # job and flips the run back to running. So a run legitimately waiting
                        # for a person looks exactly like an orphan, and this swept it away
                        # after ORPHAN_AFTER_MINUTES - killing the very thing the operator was
                        # about to approve.
                        #
                        # The real distinction is not "has a job" but "can still be resumed".
                        # A decision that can still be acted on IS the resume path.
                        f"{_answerable_decision_clause(table)}"
                        f"LIMIT 200"),
                    {"p": project_id, "working": list(working),
                     "m": ORPHAN_AFTER_MINUTES}).mappings().all()
                # THE PHASE SAYS WHICH TERMINAL STATE IS TRUE.
                #
                # Eight runs sat at status='running' with phase='done': they had finished
                # every stage and nothing ever moved the status. Closing those as `cancelled`
                # would replace one wrong answer with another and erase work that succeeded.
                # A run that reached its last phase COMPLETED; one that never left the first
                # was abandoned.
                done = [r["id"] for r in rows if str(r["phase"] or "") == "done"]
                stalled = [r["id"] for r in rows if str(r["phase"] or "") != "done"]
                if done:
                    c.execute(
                        sql(f"UPDATE {table} SET status = 'completed', updated_at = now() "
                            f"WHERE {id_col} = ANY(:ids)"), {"ids": done})
                if stalled:
                    c.execute(
                        sql(f"UPDATE {table} SET status = 'cancelled', "
                            f"error = COALESCE(error, :e), updated_at = now() "
                            f"WHERE {id_col} = ANY(:ids)"),
                        {"ids": stalled,
                         "e": "abandoned: no job remains that could advance this run"})
                if done or stalled:
                    closed.append({"table": table, "completed": len(done),
                                   "cancelled": len(stalled),
                                   "count": len(done) + len(stalled)})
        except Exception as exc:  # noqa: BLE001 — one missing table must not stop the rest
            closed.append({"table": table, "error": f"{type(exc).__name__}"})

    total = sum(int(c.get("count") or 0) for c in closed)
    if total:
        with begin(engine) as c:
            emit_event(c, project_id, "shepherd.abandoned_runs_closed", actor,
                       "project", project_id, {"closed": closed})
    return {"project_id": project_id, "closed_total": total, "detail": closed}


def _quiet_minutes(engine, job_id: str) -> float:
    """How long since this job's run last did anything. 0 when it is unknown."""
    from ..db import begin

    with begin(engine) as c:
        row = c.execute(
            sql("SELECT j.payload->>'run_id' AS run_id, "
                "       EXTRACT(EPOCH FROM (now() - COALESCE(("
                "         SELECT max(created_at) FROM llm_calls l "
                "         WHERE l.code_task_run_id = j.payload->>'run_id'), j.updated_at)))/60 "
                "       AS quiet_min "
                "FROM jobs j WHERE j.id = :i"), {"i": job_id}).mappings().first()
    if not row or not row["run_id"]:
        return 0.0
    return float(row["quiet_min"] or 0)


def report_quiet(engine, job_id: str) -> Optional[float]:
    """Put a run's silence in the Activity Log, long before anything is done about it.

    Observation only — it changes nothing about when the shepherd intervenes. It exists because
    a 27-minute blank in the log is indistinguishable from a dead system, and an operator
    watching one is right to conclude the product is wasting their time.

    Emitted at each five-minute step rather than on every sweep, so a genuinely long stage
    leaves a readable trail instead of a wall of identical rows.
    """
    from ..db import begin

    minutes = _quiet_minutes(engine, job_id)
    if minutes < QUIET_WARN_MINUTES:
        return None
    step = int(minutes // QUIET_WARN_MINUTES) * QUIET_WARN_MINUTES
    try:
        with begin(engine) as c:
            row = c.execute(
                sql("SELECT payload->>'run_id' AS run_id, payload->>'project_id' AS project_id "
                    "FROM jobs WHERE id = :i"), {"i": job_id}).mappings().first()
            if not row or not row.get("run_id"):
                return minutes
            project_id = row.get("project_id")
            if not project_id:
                project_id = c.execute(
                    sql("SELECT project_id FROM code_task_runs WHERE id = :r"),
                    {"r": row["run_id"]}).scalar()
            if not project_id:
                return minutes
            # One row per five-minute step. `emitted_step` makes the sweep idempotent: without
            # it a sweep every 30s would write the same warning sixty times.
            already = c.execute(
                sql("SELECT 1 FROM events WHERE project_id = :p AND entity_id = :r "
                    "AND event_type = 'code_task.quiet' "
                    "AND (data->>'quiet_minutes')::int = :s LIMIT 1"),
                {"p": project_id, "r": row["run_id"], "s": step}).first()
            if already:
                return minutes
            emit_event(
                c, project_id, "code_task.quiet", "shepherd", "code_task_run", row["run_id"],
                {"quiet_minutes": step,
                 "note": "no model call, turn or file since — the run may be installing "
                         "dependencies, running tests, or stuck",
                 "intervenes_at_minutes": NO_PROGRESS_MINUTES},
            )
    except Exception:  # noqa: BLE001 — reporting never breaks the sweep
        pass
    return minutes


def _made_no_progress(engine, job_id: str) -> bool:
    """Has this job's run done ANYTHING recently — a model call, or a file?

    The failure this exists for: one llm_call, zero files, 53 minutes in a stage whose command
    timeout is 120 seconds. By elapsed time it was young; by progress it had been dead for the
    better part of an hour. A long-running task that is still calling models is working.
    """
    from ..db import begin

    with begin(engine) as c:
        row = c.execute(
            sql("SELECT j.payload->>'run_id' AS run_id, "
                "       EXTRACT(EPOCH FROM (now() - COALESCE(("
                "         SELECT max(created_at) FROM llm_calls l "
                "         WHERE l.code_task_run_id = j.payload->>'run_id'), j.updated_at)))/60 "
                "       AS quiet_min "
                "FROM jobs j WHERE j.id = :i"), {"i": job_id}).mappings().first()
    if not row or not row["run_id"]:
        return False
    return float(row["quiet_min"] or 0) > NO_PROGRESS_MINUTES


def sweep_starved_work(engine, settings: Settings, project_id: str, *,
                       actor: str = SHEPHERD_ACTOR) -> dict[str, Any]:
    """Find work that is queued and not progressing, and unblock it.

    This is the sensor the taxonomy was missing. Every other check here is failure-triggered —
    it needs an error, a failed gate, a red health check. A job that is simply never claimed
    produces none of those, so a PRD meeting sat queued for two hours while every check in the
    system reported green.

    Works on the JOBS table, not on code_task_runs, and that is the point: jobs are where every
    kind of work meets — meetings, code tasks, previews, deployments, discovery, agent briefs.
    A sensor built on code_task_runs would have covered exactly the one kind that was not stuck.

    Two remedies, both free and both reversible:
      * the blockers are lower-value      -> demote them, and the queue drains in the order a
                                             person would expect
      * a blocker has held a slot for      -> cancel it; it is wedged, not busy
        hours
    """
    from ..db import begin
    from .jobs import JOB_PRIORITY_BACKGROUND, JobLeaseService

    with begin(engine) as c:
        starved = c.execute(
            sql("SELECT id, kind, priority, priority_score, "
                "       EXTRACT(EPOCH FROM (now() - created_at))/60 AS waited_min "
                "FROM jobs WHERE project_id = :p AND status = 'queued' "
                "  AND run_after <= now() "
                "  AND created_at < now() - (:m || ' minutes')::interval "
                "ORDER BY priority ASC, created_at ASC LIMIT 20"),
            {"p": project_id, "m": STARVED_AFTER_MINUTES}).mappings().all()
        holding = c.execute(
            sql("SELECT id, kind, priority, priority_score, "
                "       EXTRACT(EPOCH FROM (now() - updated_at))/60 AS running_min "
                "FROM jobs WHERE project_id = :p AND status IN ('running','leased') "
                "LIMIT 20"), {"p": project_id}).mappings().all()

    if not starved:
        return {"project_id": project_id, "starved": 0, "acted": [],
                "note": "nothing has been waiting long enough to call starved"}

    best_waiting = min(int(r["priority"] or 999) for r in starved)
    acted: list[dict[str, Any]] = []
    lease = JobLeaseService(settings)

    for blocker in holding:
        # A blocker only counts if it OUTRANKS the waiting work. Something more important
        # running first is a queue working correctly, not a fault.
        outranks = (blocker["priority_score"] is not None
                    or int(blocker["priority"] or 999) <= best_waiting)
        if not outranks:
            continue
        wedged = float(blocker["running_min"] or 0) > WEDGED_AFTER_MINUTES
        if not wedged:
            # Report the silence first, at five-minute steps, whatever is decided below. The
            # 27-minute blank that prompted this recovered on its own — so intervention was
            # right to hold off, and the console was wrong to say nothing for 27 minutes.
            report_quiet(engine, blocker["id"])
            wedged = _made_no_progress(engine, blocker["id"])
        if wedged:
            with begin(engine) as c:
                res = lease.cancel_queued_job(
                    c, blocker["id"], force=True, actor=actor,
                    reason=(f"held a worker slot for {int(blocker['running_min'])} min while "
                            f"{len(starved)} job(s) waited"))
            acted.append({"job_id": blocker["id"], "kind": blocker["kind"],
                          "remedy": "cancel_wedged_blocker", "ok": res.get("ok")})
        elif blocker["priority_score"] is not None:
            # A priority_score overrides every band at once. Clearing it is the smallest
            # change that restores the ordering the bands describe.
            with begin(engine) as c:
                c.execute(sql("UPDATE jobs SET priority_score = NULL, "
                              "priority = GREATEST(priority, :b), updated_at = now() "
                              "WHERE id = :i"),
                          {"i": blocker["id"], "b": JOB_PRIORITY_BACKGROUND})
            acted.append({"job_id": blocker["id"], "kind": blocker["kind"],
                          "remedy": "demote_blockers", "ok": True})

    if acted:
        with begin(engine) as c:
            emit_event(c, project_id, "shepherd.unstarved", actor, "project", project_id,
                       {"starved": len(starved), "acted": acted})
    return {"project_id": project_id, "starved": len(starved),
            "waiting": [dict(r) | {"waited_min": round(float(r["waited_min"]), 1)}
                        for r in starved[:5]],
            "acted": acted}


def autopilot(engine, settings: Settings, jobs, project_id: str, *,
              limit: int = 10, actor: str = "shepherd") -> dict[str, Any]:
    """Propose AND apply, for everything the confirmation gate says needs no human.

    `act()` had no caller anywhere in the codebase. `sweep` and `propose` only WROTE proposals,
    so the shepherd could diagnose every failure in the project and never once fix one — which
    is why nothing here has ever repaired itself. The remedy table, the cost estimates and
    `needs_operator_confirmation` were all already built; the loop that turns a decision into an
    action was the missing piece.

    Safety is delegated, not reinvented: a proposal that `propose()` marked `confirm_needed`
    (spend, or a remedy already spent on this run) is left for the operator exactly as before.
    Only the free, idempotent remedies fire unattended.
    """
    from ..db import begin

    candidates: list[str] = [p["run_id"] for p in sweep(engine, settings, project_id, limit=limit)
                             if p.get("run_id")]

    # Runs whose PREVIEW failed are invisible to `sweep`, which only looks at runs that are
    # themselves failed/blocked with no PR. A preview failure happens on a run that SUCCEEDED —
    # it built, it opened a PR, and the thing it produced will not boot. That is precisely the
    # case the operator was staring at, and the sweep could not see it.
    with begin(engine) as c:
        rows = c.execute(
            sql("SELECT p.run_id, count(*) AS failures, "
                "       count(DISTINCT left(coalesce(p.error, ''), 120)) AS signatures "
                "FROM app_previews p "
                "JOIN code_task_runs r ON r.id = p.run_id "
                "WHERE r.project_id = :p AND p.status = 'failed' "
                "  AND NOT EXISTS (SELECT 1 FROM shepherd_escalations e "
                "                  WHERE e.task_ref = p.run_id AND e.status = 'open') "
                "GROUP BY p.run_id ORDER BY max(p.created_at) DESC LIMIT :l"),
            {"p": project_id, "l": limit}).mappings().all()

        for row in rows:
            run_id = row["run_id"]
            if not run_id:
                continue
            # A REPEATED FAILURE IS EVIDENCE, NOT A REASON TO TRY AGAIN.
            #
            # Rebuilding is cheap per attempt and ruinous in aggregate: unbounded, it spent four
            # hours of the only worker lane re-proving one rollup error 28 times. Past the limit
            # the run stops being a candidate and becomes an escalation, which also makes the
            # NOT EXISTS clause above suppress it from here on — the loop closes itself.
            if row["failures"] >= PREVIEW_MAX_ATTEMPTS:
                c.execute(
                    sql("INSERT INTO shepherd_escalations "
                        "(id, project_id, task_ref, question, options, evidence) "
                        "VALUES (:i, :p, :t, :q, CAST(:o AS JSONB), CAST(:e AS JSONB))"),
                    {"i": make_id("shesc"), "p": project_id, "t": run_id,
                     "q": (f"This app failed to preview {row['failures']} times"
                           + ("" if row["signatures"] > 1 else " with the same error")
                           + ". Rebuilding it again will not change that. Fix the app, or stop "
                             "previewing this run?"),
                     "o": json.dumps(["open a fix task", "stop previewing"]),
                     "e": json.dumps({"failed_previews": row["failures"],
                                      "distinct_errors": row["signatures"],
                                      "limit": PREVIEW_MAX_ATTEMPTS}, default=str)})
                continue
            if run_id not in candidates:
                candidates.append(run_id)

    # AN AUTONOMOUS REPAIRER MUST NOT STARVE THE QUEUE IT SHARES.
    #
    # The first live run fired six remedies at once. The worker drains ONE job at a time, so
    # those six code-task retries went in front of the operator's PRD meeting and pushed it
    # hours out — repair work crowding out the interactive work it exists to protect. Every
    # 180s tick added more, so the backlog grew faster than it drained.
    #
    # Two bounds. Depth: if real work is already waiting, this tick does nothing. Width: at
    # most MAX_ACTIONS_PER_TICK repairs even when the queue is clear, because a remedy that is
    # worth doing is still worth doing three minutes from now.
    # Starvation FIRST, and deliberately before the depth guard below. A deep queue is exactly
    # the condition in which something is starving, so skipping the check when the queue is
    # deep would blind the sensor in the only situation it exists for.
    # Sensors and cleanup are FREE and always run: they close rows that claim to be working,
    # unstick a starved queue, and cost nothing. Only the part that STARTS WORK is gated.
    unstarved = sweep_starved_work(engine, settings, project_id, actor=actor)
    # A run that cannot move is not progress. Closing it is what stops a card sitting forever.
    abandoned = sweep_abandoned_runs(engine, settings, project_id, actor=actor)
    # Previews need their own rule; see sweep_stale_previews.
    stale_previews = sweep_stale_previews(engine, settings, project_id, actor=actor)
    # Reclaiming disk costs nothing and must run whatever the spend gate says. A preview that
    # can never serve again is holding a full npm install; 31 failed previews had accumulated
    # before anything deleted one, on the same disk the code agent works on.
    try:
        from .preview_service import sweep_preview_workdirs
        workdirs = sweep_preview_workdirs(engine)
    except Exception:  # noqa: BLE001
        logger.debug("sweep_preview_workdirs_failed", exc_info=True)
        workdirs = {"reclaimed": 0, "bytes": 0}

    with begin(engine) as c:
        queue_depth = int(c.execute(
            sql("SELECT count(*) FROM jobs WHERE project_id = :p "
                "AND status IN ('queued','running')"), {"p": project_id}).scalar() or 0)
    if queue_depth > QUEUE_DEPTH_PAUSE:
        return {"project_id": project_id, "considered": len(candidates), "applied": [],
                "deferred": [], "escalated": [], "failed": [], "unstarved": unstarved,
                "abandoned": abandoned, "stale_previews": stale_previews,
                "reclaimed_workdirs": workdirs,
                "paused_because": f"{queue_depth} jobs already queued or running; repair work "
                                  "must not push ahead of the operator's work"}

    if not getattr(settings, "shepherd_autopilot_enabled", False):
        return {"project_id": project_id, "considered": len(candidates), "applied": [],
                "deferred": [], "escalated": [], "failed": [], "unstarved": unstarved,
                "abandoned": abandoned, "stale_previews": stale_previews,
                "reclaimed_workdirs": workdirs,
                "paused_because": "BOSS_SHEPHERD_AUTOPILOT_ENABLED is false — sensors and "
                                  "cleanup ran, but no work was started. Remedies that spend "
                                  "money are opt-in."}

    applied, deferred, escalated, failed = [], [], [], []
    for run_id in candidates[:limit]:
        if len(applied) >= MAX_ACTIONS_PER_TICK:
            deferred.append({"run_id": run_id, "remedy": None,
                             "reason": f"tick cap reached ({MAX_ACTIONS_PER_TICK})"})
            continue
        try:
            proposal = propose(engine, settings, run_id)
        except Exception as exc:  # noqa: BLE001 — one bad run must not stop the sweep
            failed.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:160]})
            continue
        if proposal.get("class") == NOT_A_FAILURE or not proposal.get("remedy"):
            continue
        # ESCALATE IS A QUESTION, NOT A REPAIR. It is the remedy whose whole content is "ask a
        # human", so running it through act() reports `applied` with a null outcome — a summary
        # claiming five repairs when two were questions nobody has answered. A shepherd that
        # overstates what it fixed is the same defect as one that fixes nothing quietly.
        if not proposal.get("remedy_action"):
            escalated.append({"run_id": run_id, "class": proposal.get("class_name"),
                              "question": proposal.get("check")})
            continue
        if proposal.get("needs_confirmation"):
            deferred.append({"run_id": run_id, "remedy": proposal.get("remedy"),
                             "reason": "needs operator confirmation"})
            continue
        try:
            result = act(engine, settings, jobs, run_id, actor=actor)
            applied.append({"run_id": run_id, "remedy": proposal.get("remedy"),
                            "outcome": result.get("outcome")})
        except Exception as exc:  # noqa: BLE001
            failed.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:160]})
    return {"project_id": project_id, "considered": len(candidates),
            "applied": applied, "deferred": deferred, "escalated": escalated,
            "failed": failed, "unstarved": unstarved, "abandoned": abandoned,
            "stale_previews": stale_previews}


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
            # BACKGROUND: a repair the system chose to do must never queue ahead of a person.
            # Eleven of these went in front of an operator's PRD meeting and it never started.
            # The ceiling is passed EXPLICITLY rather than left to the callee's default: an
            # automated repair and an operator pressing retry are the same function, and only
            # one of them should be stopped by a limit it cannot argue with.
            try:
                res = retry_code_task(engine, settings, jobs, run_id, fix_note=note,
                                      profile=("lean" if remedy.id == "reissue_lean" else None),
                                      job_priority=JOB_PRIORITY_BACKGROUND,
                                      attempt_ceiling=SHEPHERD_MAX_LINEAGE_ATTEMPTS)
                new_run = res.get("run_id")
                outcome = "started"
            except ValueError as exc:
                # Stopping silently would trade a spend loop for work nobody is told is stuck.
                # The escalation also engages the open-escalation guard in `sweep`, so the run
                # stops being a candidate and the loop closes itself.
                _escalate_exhausted_lineage(engine, p.get("project_id"), run_id, str(exc))
                outcome = f"no_action: {str(exc)[:200]}"
        elif remedy.action == "restart_preview":
            # The cheap remedy, tried first: clone/install/boot are idempotent.
            from .preview_service import start_preview
            res = start_preview(engine, settings, run_id)
            outcome = ("started" if str(res.get("status") or "") not in ("failed", "error")
                       else f"failed: {str(res.get('error') or 'preview failed again')[:160]}")
        elif remedy.action in ("demote_blocking_jobs", "cancel_wedged_jobs"):
            # Starvation is a property of the QUEUE, not of one run, so the remedy runs the
            # sweep rather than acting on this run alone. Wired here too because a remedy that
            # names an action and takes none is not a remedy — the guard that says so is what
            # caught these.
            result = sweep_starved_work(engine, settings,
                                        p.get("project_id") or _project_of(engine, run_id),
                                        actor=actor)
            outcome = "started" if result.get("acted") else "no_action: nothing was starving"
        elif remedy.action in ("redeploy_deployment", "preflight_target"):
            outcome = _act_on_deployment(engine, settings, run_id, remedy.action, actor)
        elif remedy.action == "create_code_task":
            # A REMEDY THAT NAMES AN ACTION AND TAKES NONE IS NOT A REMEDY.
            #
            # Every non-retry action fell through to "no_action": the delivery classes
            # (deploy, health check, canary) and the preview class all recorded a decision and
            # did nothing about it. The shepherd looked like it was working — ledger rows,
            # reasoning rows, predicted costs — while no fix was ever opened. Autonomy has to
            # end in an action or it is only bookkeeping.
            new_run = _open_fix_task(engine, settings, jobs, run_id, p, remedy, note)
            outcome = "started" if new_run else "failed: could not open fix task"
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


# Deployment environments a request for "live" could mean.
LIVE_ENVIRONMENTS = ("production", "prod", "live")


def describe_undeployable(*, status: str, repo: str, targets: list) -> str:
    """Explain why an app cannot be deployed, in terms the operator can act on.

    Pure on purpose. This was inline inside a database branch, which meant the wording could
    only be exercised by reaching production -- and it reached production saying "this app has
    never been deployed, so there is no build to re-ship" to an operator who already knew the
    app had a staging target and no production one. Two different problems, one sentence, no
    options.

    Split out so every failure state can be driven from a table in the tests: no targets at all,
    staging but no production, a live target that exists but is not active, a build that never
    completed. The agent's answer is only as good as the facts it is handed.
    """
    envs = [str(t.get("environment") or "?").lower() for t in targets]
    live = [t for t in targets if str(t.get("environment") or "").lower() in LIVE_ENVIRONMENTS]

    if not targets:
        where = "This app has NO deployment targets configured at all."
        options = ("The only way forward is to create a target for the environment they want, "
                   "then deploy to it.")
    else:
        have = ", ".join(f"{t.get('environment')} ({t.get('status')})" for t in targets)
        where = f"This app HAS deployment targets: {have}."
        if not live:
            others = ", ".join(sorted({str(t.get("environment")) for t in targets}))
            where += (" There is no production/live target, so \"deploy to live\" has nowhere "
                      "to go yet.")
            options = (f"Real options: deploy to an environment that DOES have a target "
                       f"({others}), or create the missing production target first.")
        elif any(str(t.get("status") or "").lower() not in ("active", "ready") for t in live):
            bad = ", ".join(f"{t.get('environment')}={t.get('status')}" for t in live)
            where += f" The live target exists but is not usable ({bad})."
            options = ("Real options: fix or re-activate that target, or deploy to another "
                       "environment meanwhile.")
        else:
            options = ("A live target exists and looks usable, so the missing piece is the "
                       "deployment run itself.")

    build = ""
    if status and status.lower() not in ("completed", "succeeded", "passed"):
        build = (f" Note the build run is '{status}' — if no build ever completed, deploying "
                 "cannot work until one does, and that is a REBUILD, which costs money.")

    return (
        "no_action: there is no deployment run for this app yet, so there is nothing to "
        "re-ship" + (f" (its build run is {status})" if status else "")
        + f". Repo: {repo}. {where}{build} {options}"
        + " TELL THE OPERATOR THIS STATE PLAINLY — which environments exist, which do not, and "
          "what that means for what they asked. They can see the console; a bare 'not possible' "
          "reads as BOSS not knowing. Then lay out the real options and let them choose. Put "
          "each option they can actually take behind a propose_action BUTTON — an option "
          "described in prose is not an option they can take. "
          "DO NOT substitute a build for a deploy — they are different actions and one costs "
          "money."
    )


def _act_on_deployment(engine, settings, run_id: str, action: str, actor: str) -> str:
    """Carry out the two DEPLOY_FAILED remedies against this run's latest deployment.

    Both were declared when the delivery classes were added and neither was ever wired, so a
    failed deployment recorded a remedy and sat there. `redeploy_deployment` is safe to take
    unattended because transfer and docker steps are idempotent; `preflight_target` only reads.
    """
    from ..db import begin

    with begin(engine) as c:
        row = c.execute(
            # `deployment_target_id` is the column; `target_id` does not exist. Every caller of
            # this path — the shepherd's DEPLOY_FAILED remedy since it was written, and now the
            # redeploy tool — failed on it, silently, because the exception was swallowed into
            # "no deployment run for this task". BOSS found this itself when an operator finally
            # asked it to retry a deployment, and correctly reported it as a framework bug
            # rather than an application one.
            sql("SELECT id, deployment_target_id AS target_id FROM deployment_runs "
                "WHERE release_packet->>'code_task_run_id' = :i "
                "ORDER BY created_at DESC LIMIT 1"),
            {"i": run_id}).mappings().first()
    if not row:
        # THE MESSAGE IS THE GUARDRAIL.
        #
        # This said only "no deployment run for this task". Asked to deploy an app whose last
        # build had FAILED, the agent read that, silently called retry_code_task instead, and
        # started a build worth minutes and real money — then described it as "retried the
        # failed task, which will redeploy". The operator could not see that a build had begun.
        #
        # A distant instruction in the system prompt did not prevent it, so the correction goes
        # where the model is actually standing when it decides: in the result it just received.
        # SAY WHAT IS ACTUALLY THERE, NOT JUST WHAT IS MISSING.
        #
        # This returned one sentence -- "never deployed, no build to re-ship" -- and the agent
        # relayed exactly that. The operator already knew more than BOSS was saying: the app has
        # a STAGING target and no production one, which is a different problem with different
        # options. BOSS had even found that a turn earlier and then dropped it.
        #
        # A refusal the operator cannot act on is a dead end. The tool result now carries the
        # concrete state -- which environments exist, which are missing, what the build did --
        # so the agent has something to have a conversation about instead of a wall.
        status, repo, targets = "", "", []
        try:
            with begin(engine) as c:
                r = c.execute(
                    sql("SELECT status, project_id, spec->>'owner' AS owner, "
                        "       spec->>'repo' AS repo "
                        "FROM code_task_runs WHERE id = :i"),
                    {"i": run_id}).mappings().first() or {}
                status = str(r.get("status") or "")
                repo = f"{r.get('owner') or '?'}/{r.get('repo') or '?'}"
                if r.get("repo") and r.get("project_id"):
                    targets = [dict(t) for t in c.execute(
                        sql("SELECT environment, status, app_name FROM deployment_targets "
                            "WHERE project_id = :p AND repo = :r "
                            "ORDER BY environment"),
                        {"p": r["project_id"], "r": r["repo"]}).mappings().all()]
        except Exception:  # noqa: BLE001 — never let the explanation break the refusal
            pass

        return describe_undeployable(status=status, repo=repo, targets=targets)

    if action == "redeploy_deployment":
        from .delivery_pipeline import redeploy
        res = redeploy(engine, settings, row["id"], actor=actor)
        status = str(res.get("status") or "")
        return "started" if status not in ("failed", "error") else f"failed: {status}"

    # preflight_target — read-only: prove the host is reachable before shipping to it again.
    from .deployment_fleet import verify_host
    with begin(engine) as c:
        host_id = c.execute(sql("SELECT host_id FROM deployment_targets WHERE id = :i"),
                            {"i": row["target_id"]}).scalar()
        if not host_id:
            return "no_action: target has no host to preflight"
        report = verify_host(c, settings, host_id=host_id)
    problems = (report.get("last_preflight") or report).get("problems") or []
    return "started" if not problems else f"failed: {'; '.join(str(x) for x in problems)[:160]}"


def _open_fix_task(engine, settings, jobs, run_id: str, p: dict, remedy, note: str):
    """Open a scoped repair task against the run that failed, carrying the EVIDENCE.

    Not a retry of the original brief: that brief already succeeded on its own terms. What
    failed is downstream — the preview would not boot, the health gate went red, the canary
    regressed — so the task has to say what broke and hand over the log. A repair agent told
    only "it does not work" guesses, and guessing is how a Vite preview script ended up in a
    repo that has no Vite.
    """
    from ..db import begin
    from .code_task_service import start_code_task

    with begin(engine) as c:
        run = c.execute(
            sql("SELECT project_id, title, spec FROM code_task_runs WHERE id=:i"),
            {"i": run_id}).mappings().first()
    if not run:
        return None
    spec = run["spec"]
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            spec = {}
    spec = spec or {}

    evidence = str(p.get("evidence") or p.get("check") or "")[:4000]
    instructions = (
        f"{note}\n\n"
        f"The change in run {run_id} merged or completed, but the step AFTER it failed: "
        f"{p.get('class_name')}.\n\n"
        f"EVIDENCE (the actual failure output):\n{evidence}\n\n"
        "Fix the APPLICATION so this step passes. Constraints:\n"
        "- Do NOT introduce a build system, framework or preview harness the repo does not "
        "already use. If a tool is missing, that is a finding to report, not a dependency to "
        "add.\n"
        "- Keep the change as small as the failure requires; do not refactor around it.\n"
        "- If the failure is environmental rather than a code defect, say so and change "
        "nothing."
    )
    try:
        with begin(engine) as c:
            return start_code_task(
                c, project_id=run["project_id"], task_id=None,
                title=f"Auto-repair: {p.get('class_name')} — {str(run['title'])[:90]}",
                instructions=instructions,
                acceptance_criteria=[
                    f"the {p.get('class_name')} that failed for {run_id} now passes",
                    "no new build system or framework was added to the repo",
                ],
                test_commands=list(spec.get("test_commands") or []),
                owner=spec.get("owner") or settings.target_github_owner,
                repo=spec.get("repo") or settings.target_github_repo,
                base_branch=spec.get("base_branch") or "main",
                finalize_ctx=None, jobs=jobs, open_pr=True,
            )
    except Exception:  # noqa: BLE001 — a remedy that cannot start is data, not a crash
        return None


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


# --------------------------------------------------------------------------- #
# Phase 4 — watching work that is still running
# --------------------------------------------------------------------------- #
def _inflight_snapshots(connection, project_id: str, limit: int = 50) -> list:
    """Facts about every in-flight run in this project, in one pass.

    One query on purpose. Asking per-run invites a shepherd that is itself the slow thing on
    the box, and the duplicate check needs the whole set anyway — "is there an older run like
    this one" is not answerable a row at a time.
    """
    from .code_task_service import work_fingerprint
    from .shepherd_watch import ORPHAN_LEASE_MINUTES, RunSnapshot

    rows = connection.execute(
        sql("""
            SELECT r.id, r.project_id, r.status, r.phase, r.title,
                   r.spec->>'work_fingerprint' AS fp, r.spec AS spec,
                   r.error AS run_error,
                   COALESCE(r.finalize_ctx->>'root_run_id', r.id) AS root_run,
                   EXTRACT(EPOCH FROM (now() - r.created_at))/60.0 AS mins_running,
                   EXTRACT(EPOCH FROM (now() - r.updated_at))/60.0 AS mins_since_update,
                   r.created_at,
                   j.lease_owner, j.ever_leased, j.deferred_mins, j.ended_mins, j.last_error_msg,
                   EXTRACT(EPOCH FROM (now() - COALESCE(j.heartbeat_at, j.created_at)))/60.0
                     AS mins_since_heartbeat,
                   j.status AS job_status
            FROM code_task_runs r
            LEFT JOIN LATERAL (
                SELECT lease_owner, heartbeat_at, created_at, status,
                       EXTRACT(EPOCH FROM (run_after - now()))/60.0 AS deferred_mins,
                       EXTRACT(EPOCH FROM (now() - updated_at))/60.0 AS ended_mins,
                       last_error->>'message' AS last_error_msg,
                       (lease_owner IS NOT NULL OR heartbeat_at IS NOT NULL) AS ever_leased
                FROM jobs
                WHERE payload->>'run_id' = r.id ORDER BY created_at DESC LIMIT 1
            ) j ON TRUE
            WHERE r.project_id = :p
              AND (r.status IN ('running', 'queued')
                   -- Failures are in scope now. This clause read `status IN (running, queued)`,
                   -- so a run that DIED was outside the shepherd's field of view entirely: a
                   -- build killed by a SIGTERM at seventeen minutes stayed failed until a
                   -- person happened to look and ask why nothing had noticed. A watchdog that
                   -- cannot see failures is not watching for the thing most worth watching.
                   --
                   -- Bounded by age so a sweep does not re-litigate months of history every
                   -- two minutes; anything older than this is a person's to judge.
                   OR (r.status = 'failed'
                       AND r.updated_at > now() - interval '6 hours'))
            ORDER BY r.created_at ASC LIMIT :l
        """), {"p": project_id, "l": limit}).mappings().all()

    # The earliest in-flight run for each fingerprint is the keeper; every later one with the
    # same fingerprint is redundant. Rows arrive oldest-first, so first-seen wins.
    # Runs created before the fingerprint was introduced have none stored. Computing it here
    # from the same fields, with the same function, means the shepherd can still recognise the
    # backlog it was built for — otherwise the seven duplicates that prompted all of this
    # would be exactly the ones it could not see.
    fps: dict[str, Optional[str]] = {}
    for row in rows:
        fp = row["fp"]
        if not fp:
            spec = row["spec"] or {}
            if spec.get("instructions"):
                fp = work_fingerprint(
                    project_id=row["project_id"], owner=spec.get("owner") or "",
                    repo=spec.get("repo") or "", base_branch=spec.get("base_branch") or "",
                    title=row["title"] or "",
                    # Stored instructions carry the engine's appended deployability note;
                    # the note is identical for every task, so it cannot make two different
                    # specs collide, and stripping it is not worth a second code path.
                    instructions=spec.get("instructions") or "")
        fps[row["id"]] = fp

    first_by_fp: dict[str, str] = {}
    for row in rows:
        fp = fps.get(row["id"])
        if fp and fp not in first_by_fp:
            first_by_fp[fp] = row["id"]

    median, samples = _latency_baseline(connection, project_id)
    since_lease = _minutes_since_any_lease(connection, project_id)
    # How often the platform has already killed each piece of work, counted over the whole
    # lineage. A per-run count is always zero -- every retry is a new row -- so the cap that
    # stops an unbounded retry loop would never bind.
    platform_retries = {
        r[0]: int(r[1]) for r in connection.execute(sql("""
            SELECT COALESCE(finalize_ctx->>'root_run_id', id) AS root, count(*) - 1
            FROM code_task_runs
            WHERE project_id = :p AND status = 'failed'
            GROUP BY 1"""), {"p": project_id}).all()
    }
    snaps = []
    for row in rows:
        fp = fps.get(row["id"])
        keeper = first_by_fp.get(fp) if fp else None
        snaps.append(RunSnapshot(
            run_id=row["id"], project_id=row["project_id"], status=row["status"],
            phase=row["phase"] or "", title=row["title"] or "",
            minutes_running=float(row["mins_running"] or 0.0),
            minutes_since_update=float(row["mins_since_update"] or 0.0),
            work_fingerprint=fp,
            duplicate_of=keeper if (keeper and keeper != row["id"]) else None,
            # "Has a worker" means one that is STILL THERE. The first version asked only
            # whether `lease_owner` was set, so a worker that died 37 days ago holding the
            # lease still counted as live — and ten runs wedged in `coding` since July fell
            # through to "stalled, ask a human" instead of "orphaned, take it back". A lease
            # is a claim with a heartbeat; without the heartbeat it is just a name.
            has_live_worker=(bool(row["lease_owner"]) and row["job_status"] == "running"
                             and row["mins_since_heartbeat"] is not None
                             and float(row["mins_since_heartbeat"]) < ORPHAN_LEASE_MINUTES),
            minutes_since_heartbeat=(float(row["mins_since_heartbeat"])
                                     if row["mins_since_heartbeat"] is not None else None),
            has_job=row["job_status"] is not None,
            job_status=row["job_status"],
            job_deferred_minutes=(float(row["deferred_mins"])
                                  if row["deferred_mins"] is not None else None),
            job_ended_minutes=(float(row["ended_mins"])
                               if row["ended_mins"] is not None else None),
            job_last_error=row["last_error_msg"],
            run_error=row["run_error"],
            # How many times this same work has already died on the platform. Counted across
            # the LINEAGE, not the row: each retry is a new run, so counting per-run would
            # always read zero and the cap would never bind.
            platform_retries=platform_retries.get(row["root_run"], 0),
            was_leased=bool(row["ever_leased"]),
            minutes_since_any_lease=since_lease,
            median_minutes=median, sample_size=samples,
        ))
    return snaps


def _minutes_since_any_lease(connection, project_id: str):
    """How long since ANY job here was claimed — the queue's pulse, not one run's.

    Distinguishes "this run is unlucky" from "nothing is consuming the queue". Without it a
    dead worker looks like eight independently unlucky tasks, and the shepherd proposes eight
    task-level remedies for one platform problem.
    """
    row = connection.execute(
        sql("SELECT EXTRACT(EPOCH FROM (now() - MAX(GREATEST("
            "  COALESCE(heartbeat_at, to_timestamp(0)), "
            "  COALESCE(updated_at, to_timestamp(0))))))/60.0 AS mins "
            "FROM jobs WHERE project_id = :p AND lease_owner IS NOT NULL"),
        {"p": project_id}).mappings().first()
    if row and row["mins"] is not None:
        return float(row["mins"])

    # Nothing here was EVER claimed. That is not "unknown" — it is the most starved a queue
    # can be, and reading the NULL as unknown is why ten runs queued for 37 days came back
    # "stalled, ask a human" instead of "nobody is consuming this queue". Fall back to the age
    # of the oldest waiting job: how long the queue has gone undrained.
    row = connection.execute(
        sql("SELECT EXTRACT(EPOCH FROM (now() - MIN(created_at)))/60.0 AS mins "
            "FROM jobs WHERE project_id = :p "
            "  AND status NOT IN ('completed','failed','cancelled')"),
        {"p": project_id}).mappings().first()
    return float(row["mins"]) if row and row["mins"] is not None else None


def _latency_baseline(connection, project_id: str) -> tuple:
    """How long this project's finished runs actually take. Measured, never assumed.

    A constant here would flag every task in a slow repo and miss a wedged one in a fast
    project. Only successful runs count: a run that failed after four minutes is not evidence
    of how long the work takes.
    """
    row = connection.execute(
        sql("""
            SELECT count(*) AS n,
                   percentile_cont(0.5) WITHIN GROUP (
                     ORDER BY EXTRACT(EPOCH FROM (updated_at - created_at))/60.0
                   ) AS median_minutes
            FROM code_task_runs
            WHERE project_id = :p AND status = 'completed'
              AND updated_at > created_at
              AND created_at > now() - interval '30 days'
        """), {"p": project_id}).mappings().first()
    if not row or not row["n"]:
        return None, 0
    return (float(row["median_minutes"]) if row["median_minutes"] else None), int(row["n"])


def _cancel_run(connection, run_id: str, reason: str) -> int:
    """Stop a run and the job carrying it.

    Both, or neither matters: leaving the job queued means a worker picks up a cancelled run
    and does the work anyway, which is the whole cost we were trying not to pay.
    """
    changed = connection.execute(
        sql("UPDATE code_task_runs SET status='cancelled', phase='cancelled', "
            "error=:e, updated_at=now() WHERE id=:i AND status IN ('running','queued')"),
        {"i": run_id, "e": reason[:500]}).rowcount
    connection.execute(
        sql("UPDATE jobs SET status='cancelled', updated_at=now() "
            "WHERE payload->>'run_id' = :i AND status IN ('queued','running')"),
        {"i": run_id})
    return changed


def _fail_run(connection, run_id: str, reason: str) -> int:
    """Stop claiming a run is alive when nothing can advance it.

    Not a cancellation — nobody chose to stop this; it was already over and the row had not
    noticed. Saying so is the whole value: a console listing ten runs as "Queued — running…"
    that will never move is worse than one listing ten failures, because the first invites
    waiting.
    """
    return connection.execute(
        sql("UPDATE code_task_runs SET status='failed', phase='abandoned', error=:e, "
            "updated_at=now() WHERE id=:i AND status IN ('running','queued')"),
        {"i": run_id, "e": reason[:500]}).rowcount


def _retry_run(engine, settings, jobs, run_id: str, reason: str) -> int:
    """Run the work again, as a new attempt in the same lineage.

    A retry is the one remedy the shepherd lacked, and the only correct response to work that
    died without a verdict: the run is terminal, so requeueing its job does nothing, and
    cancelling something already failed is meaningless.

    Reuses retry_code_task rather than reimplementing it, so an automatic attempt is the same
    object as an operator-pressed one -- same lineage, same branch, same resume logic that
    keeps whatever the killed attempt had already written.

    Returns 1 when a new attempt exists, 0 when it does not, so the caller can tell a real
    remedy from an announcement.
    """
    from .code_task_service import retry_code_task

    try:
        # No actor parameter: retry_code_task records lineage, not authorship. The
        # shepherd.retry event below is what attributes this to the shepherd.
        result = retry_code_task(
            engine, settings, jobs, run_id,
            fix_note=f"Automatic retry by the task shepherd: {reason}",
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("shepherd: retry of %s failed: %s", run_id, error)
        return 0
    return 1 if (result or {}).get("run_id") else 0


def _requeue_run(connection, run_id: str) -> int:
    """Hand an orphaned run back to the queue: clear the dead lease, make it claimable.

    Returns the number of jobs actually freed. Zero means the remedy did not apply — a job
    nobody ever leased has nothing to put back — and the caller must NOT report that as a fix.
    """
    changed = connection.execute(
        sql("UPDATE jobs SET status='queued', lease_owner=NULL, lease_token_hash=NULL, "
            "lease_expires_at=NULL, run_after=now(), updated_at=now() "
            "WHERE payload->>'run_id' = :i AND status='running'"),
        {"i": run_id}).rowcount
    if changed:
        connection.execute(
            sql("UPDATE code_task_runs SET phase='queued', updated_at=now() WHERE id=:i"),
            {"i": run_id})
    return changed


def projects_with_running_work(engine) -> list:
    """Every project that currently has something in flight.

    The worker is not project-scoped, and neither is neglect: a shepherd that only watches
    the project someone remembered to name is not watching.
    """
    from ..db import begin
    with begin(engine) as c:
        rows = c.execute(
            sql("SELECT DISTINCT project_id FROM code_task_runs "
                "WHERE status IN ('running','queued')")).mappings().all()
    return [r["project_id"] for r in rows]


def watch_all(engine, settings: Settings, *, dry_run: bool = False,
              actor: str = "shepherd", class_modes: Optional[dict] = None,
              jobs=None) -> dict[str, Any]:
    """Watch in-flight work across every project that has any."""
    results = [watch(engine, settings, p, dry_run=dry_run, actor=actor,
                     class_modes=class_modes, jobs=jobs)
               for p in projects_with_running_work(engine)]
    return {
        "projects": len(results),
        "in_flight": sum(r["in_flight"] for r in results),
        "acted": [a for r in results for a in r["acted"]],
        "escalated": [e for r in results for e in r["escalated"]],
        "ineffective": [i for r in results for i in r.get("ineffective", [])],
        "dry_run": dry_run,
    }


def _record_observation(engine, project_id: str, snap, verdict, mode: str, *,
                        acted: bool, dry_run: bool) -> None:
    """Write down what the shepherd concluded, whether or not it did anything.

    The ledger is what makes readiness measurable instead of remembered. Without it, shadow
    mode is a promise to review something that produces no output — the flag stays off, the
    evidence lives nowhere, and re-enabling months later reintroduces the bugs it was switched
    off for.

    Never fails the sweep: a shepherd that stops watching because it could not write a note
    has its priorities backwards.
    """
    if dry_run:
        return
    from ..db import begin
    try:
        with begin(engine) as c:
            c.execute(
                sql("INSERT INTO shepherd_watch_observations "
                    "(id, project_id, run_id, watch_class, class_name, action, mode, acted, "
                    " reason, evidence) "
                    "VALUES (:i, :p, :r, :k, :n, :a, :m, :ac, :why, CAST(:e AS JSONB))"),
                {"i": make_id("shobs"), "p": project_id, "r": snap.run_id,
                 "k": verdict.klass, "n": verdict.name, "a": verdict.action, "m": mode,
                 "ac": acted, "why": verdict.reason[:1000],
                 "e": json.dumps(verdict.evidence, default=str)})
    except Exception:  # noqa: BLE001
        pass


def readiness(engine, project_id: Optional[str] = None, *, days: int = 30) -> dict[str, Any]:
    """Per class: what it has judged, how often it was overruled, and whether to trust it.

    Written to be read by someone who has forgotten this component exists. If shadow mode is
    going to hold a capability back, something has to say — unprompted — "this class has
    judged forty runs and was never overruled, promote it or say why not". Otherwise the safe
    option quietly becomes the permanent one.
    """
    from ..db import begin
    from .shepherd_watch import WATCH_NAMES, mode_for, promotion_status

    where = "created_at > now() - make_interval(days => :d)"
    params: dict[str, Any] = {"d": days}
    if project_id:
        where += " AND project_id = :p"
        params["p"] = project_id
    with begin(engine) as c:
        rows = c.execute(
            sql(f"SELECT watch_class, class_name, mode, "
                f"       count(*) AS observed, "
                f"       count(*) FILTER (WHERE acted) AS acted, "
                f"       count(*) FILTER (WHERE verdict_correct IS FALSE) AS wrong, "
                f"       count(*) FILTER (WHERE verdict_correct IS TRUE) AS confirmed, "
                f"       max(created_at) AS last_seen "
                f"FROM shepherd_watch_observations WHERE {where} "
                f"GROUP BY 1, 2, 3 ORDER BY observed DESC"), params).mappings().all()

    classes = []
    for row in rows:
        mode = mode_for(row["watch_class"])
        eligible, sentence = promotion_status(
            row["watch_class"], int(row["observed"]), int(row["wrong"]), mode)
        classes.append({
            "class": row["watch_class"],
            "name": row["class_name"] or WATCH_NAMES.get(row["watch_class"], "unknown"),
            "mode": mode, "observed": int(row["observed"]), "acted": int(row["acted"]),
            "confirmed": int(row["confirmed"]), "overruled": int(row["wrong"]),
            "eligible_for_promotion": eligible, "assessment": sentence,
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
        })
    promotable = [c["name"] for c in classes if c["eligible_for_promotion"]]
    return {
        "days": days, "classes": classes,
        "promotable": promotable,
        # One line for someone who has not thought about the shepherd in a fortnight.
        "headline": (f"{len(promotable)} class(es) have earned promotion: "
                     f"{', '.join(promotable)}" if promotable else
                     f"{len(classes)} class(es) observed; none has enough evidence to promote"),
    }


def watch(engine, settings: Settings, project_id: str, *, dry_run: bool = False,
          actor: str = "shepherd",
          class_modes: Optional[dict] = None,
          jobs=None) -> dict[str, Any]:
    """Look at everything currently running, and deal with what is obviously wrong.

    This is the half of the shepherd that was missing. `sweep()` reasons about runs that have
    already stopped; by then the money is spent. Seven identical benchmark runs sat queued and
    healthy on the production box precisely because nothing looks at healthy work.

    Autonomous only where the verdict is a fact rather than a judgement — a redundant
    duplicate, an orphaned lease. A stalled or slow run is escalated, because "wedged" and
    "genuinely hard" are indistinguishable from out here and cancelling the wrong one throws
    away work that would have landed.
    """
    from ..db import begin
    from .shepherd_watch import (
        LIVE, MAX_ACTIONS_PER_SWEEP, HEALTHY, act_now, assess, mode_for)

    acted, escalated, ineffective, seen = [], [], [], 0
    with begin(engine) as c:
        snaps = _inflight_snapshots(c, project_id)
    for snap in snaps:
        seen += 1
        verdict = assess(snap)
        if verdict.klass == HEALTHY:
            continue
        # Every verdict carries the facts it was decided from, not just its conclusion.
        # Diagnosing why ten runs came back "stalled" instead of "orphaned" took three
        # deploys of guesswork because the answer — which of has_job / was_leased /
        # has_live_worker was unexpectedly true — was never in the output. A judgement whose
        # inputs are invisible cannot be argued with, and the design says CHECK carries the
        # same weight as the conclusion.
        observed = {
            "has_job": snap.has_job,
            "was_leased": snap.was_leased,
            "has_live_worker": snap.has_live_worker,
            "minutes_since_heartbeat": (round(snap.minutes_since_heartbeat, 1)
                                        if snap.minutes_since_heartbeat is not None else None),
            "minutes_since_any_lease": (round(snap.minutes_since_any_lease, 1)
                                        if snap.minutes_since_any_lease is not None else None),
            "job_status": snap.job_status,
            "job_deferred_minutes": (round(snap.job_deferred_minutes, 1)
                                     if snap.job_deferred_minutes is not None else None),
            "job_last_error": snap.job_last_error,
            "duplicate_of": snap.duplicate_of,
            "has_fingerprint": bool(snap.work_fingerprint),
        }
        record = {"run_id": snap.run_id, "title": snap.title, "class": verdict.klass,
                  "name": verdict.name, "reason": verdict.reason, "action": verdict.action,
                  "evidence": {**verdict.evidence, "observed": observed}}
        # Shadow classes judge and stand back. Recorded either way: a capability held back
        # for safety must still produce evidence, or "review it later" becomes "never".
        klass_mode = mode_for(verdict.klass, class_modes)
        may_act, why = act_now(verdict, snap)
        if may_act and klass_mode != LIVE:
            may_act, why = False, f"{verdict.name} is in shadow mode — recorded, not acted on"
        _record_observation(engine, project_id, snap, verdict, klass_mode,
                            acted=False, dry_run=dry_run)
        if dry_run:
            (acted if may_act else escalated).append(
                {**record, "dry_run": True, "gate": why})
            continue
        if may_act and len(acted) >= MAX_ACTIONS_PER_SWEEP:
            # Refuse the rest of the sweep rather than clear someone's board on one bad
            # judgement. The remaining runs are still here next time, and a capped mistake is
            # a small incident instead of a systematic one.
            escalated.append({**record, "gate": "per-sweep action cap reached"})
            continue
        if may_act:
            with begin(engine) as c:
                if verdict.action == "cancel":
                    changed = _cancel_run(c, snap.run_id, f"shepherd: {verdict.reason}")
                elif verdict.action == "fail":
                    changed = _fail_run(c, snap.run_id, f"shepherd: {verdict.reason}")
                elif verdict.action == "retry":
                    # Outside this transaction on purpose: retry_code_task opens its own, and
                    # nesting them deadlocks the sweep against itself.
                    changed = None
                else:
                    changed = _requeue_run(c, snap.run_id)
                if changed:
                    emit_event(c, project_id, f"shepherd.{verdict.action}", actor,
                               "code_task_run", snap.run_id,
                               {"class": verdict.name, "reason": verdict.reason,
                                **verdict.evidence})
            if verdict.action == "retry":
                changed = _retry_run(engine, settings, jobs, snap.run_id,
                                     verdict.reason)
                if changed:
                    with begin(engine) as c:
                        emit_event(c, project_id, "shepherd.retry", actor,
                                   "code_task_run", snap.run_id,
                                   {"class": verdict.name, "reason": verdict.reason,
                                    **verdict.evidence})
            # An action that changed nothing is not an action. Reporting it as one is a
            # shepherd announcing a fix every two minutes while the run sits exactly where it
            # was — the precise dishonesty that makes an operator stop believing the console.
            if changed:
                _record_observation(engine, project_id, snap, verdict, klass_mode,
                                    acted=True, dry_run=dry_run)
                acted.append(record)
            else:
                ineffective.append({**record, "note": "remedy did not apply — nothing changed"})
        else:
            record = {**record, "gate": why}
            # One open question per run. A shepherd that re-asks is noise, and an operator who
            # learns to ignore it is worse than no shepherd — the same rule sweep() follows.
            with begin(engine) as c:
                existing = c.execute(
                    sql("SELECT 1 FROM shepherd_escalations "
                        "WHERE task_ref=:t AND status='open'"), {"t": snap.run_id}).first()
                if not existing:
                    c.execute(
                        sql("INSERT INTO shepherd_escalations "
                            "(id, project_id, task_ref, question, options, evidence) "
                            "VALUES (:i, :p, :t, :q, CAST(:o AS JSONB), CAST(:e AS JSONB))"),
                        {"i": make_id("shesc"), "p": project_id, "t": snap.run_id,
                         "q": f"{snap.title or snap.run_id}: {verdict.reason}. Let it continue, "
                              f"or cancel it?",
                         "o": json.dumps(["continue", "cancel"]),
                         "e": json.dumps(verdict.evidence, default=str)})
                    escalated.append(record)
    return {"project_id": project_id, "in_flight": seen, "acted": acted,
            "escalated": escalated, "ineffective": ineffective, "dry_run": dry_run}


def _escalate_exhausted_lineage(engine, project_id: Optional[str], run_id: str,
                                reason: str) -> None:
    """Record that automatic repair has given up, and why.

    A bound that just stops is indistinguishable from a bound that never fired. The escalation
    is also what makes the bound STICK: `sweep` skips runs with an open escalation, so the run
    leaves the candidate set instead of being reconsidered on every 180s tick.
    """
    from ..db import begin

    if not project_id:
        project_id = _project_of(engine, run_id)
    if not project_id:
        return
    try:
        with begin(engine) as c:
            c.execute(
                sql("INSERT INTO shepherd_escalations "
                    "(id, project_id, task_ref, question, options, evidence) "
                    "SELECT :i, :p, :t, :q, CAST(:o AS JSONB), CAST(:e AS JSONB) "
                    "WHERE NOT EXISTS (SELECT 1 FROM shepherd_escalations e "
                    "                  WHERE e.task_ref = :t AND e.status = 'open')"),
                {"i": make_id("shesc"), "p": project_id, "t": run_id,
                 "q": ("Automatic repair has stopped: this work has been retried to its limit "
                       "and keeps failing the same way. Diagnose it, retry it explicitly, or "
                       "abandon it?"),
                 "o": json.dumps(["diagnose", "retry anyway", "abandon"]),
                 "e": json.dumps({"reason": reason,
                                  "limit": SHEPHERD_MAX_LINEAGE_ATTEMPTS}, default=str)})
    except Exception:
        logger.debug("escalate_exhausted_lineage_failed run=%s", run_id, exc_info=True)
