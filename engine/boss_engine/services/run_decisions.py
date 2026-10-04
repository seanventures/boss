"""A run that ends takes its unanswered decisions with it.

SEEN LIVE ON boss-1 (2026-10-04). Runs were stopped (POST /v1/workflow-runs/{id}/cancel) and
then removed (DELETE /v1/delivery-workflows/runs/{id}); both answered 200. Their gates stayed
in Approvals → "Needs action" — "Create the tickets — E2E: Meeting to tickets (2)", "Send it
for review — E2E: Trend to content", "Tell the salesperson — E2E: Sales signal" — each with
Approve and Reject, 21 in all, and in the Command Center's count as "“Approval” is waiting for
you on …". Approving one would have released a step of a run that no longer exists.

Nothing closed them because nothing owned the question "this run is over — what about the
decisions it raised?". Cancel marked steps and the run; delete cascaded the step rows (and with
them the only link from a decision step's card back to its run); neither touched `decisions`.

THE RULE, IN ONE PLACE: `withdraw_with_run`. Every path that ends a run for good — cancelled
(including Stop-and-run-again, which cancels the run it replaces), removed, completed, undone
(compensated) — calls it, and engine/tests/test_a_gone_run_takes_its_approvals.py fails on any
code in engine/ or apps/ that ends a run without it. A decision it closes is `withdrawn`: a
terminal status with no buttons (decision_actions offers nothing on it), never "expired", which
a hold default keeps answerable. `resolution_note` says why, in words.

A run STOPPED AT A FAILED STEP (`failed`, `deadlettered`) is not over: Try again and Skip resume
it, and a gate waiting on another branch is released by the same answer it would have had. Its
gates stay open until the run is stopped or removed, which withdraws them here.

WHAT A RUN OWNS. A decision belongs to a run when the run raised it for itself:
  * a governance gate or a held step (workflow_capability_runner) records `workflow_run_id` in
    its recommendation;
  * a decision step (`action: resume_workflow`) records it too from now on, and before that was
    linked only by its step row — or, once the run was deleted, by its reason sentence
    "Workflow wfr_… is waiting at decision step …".
A decision the run BORROWED — a panel's own PRD approval used as a spec-review gate — is the
panel's, means something without the run, and is left alone. So is a decision another live run
is still waiting on (only a borrowed one can be: see ONE GATE, ONE RUN below).

ONE GATE, ONE RUN. Every decision a run raises to gate one of its steps is opened by
`open_step_decision`, and belongs to exactly that run and step: the run identity is written into
its recommendation, its question is kept distinct from another run's open card in words, and
create_blocking_decision's "one open decision per cause" merge counts the run and step as part of
the cause. Two runs of one workflow at the same Approval gate used to share ONE card, and one
Approve released both. engine/tests/test_one_gate_one_run.py fails on any code that raises a
step's decision any other way.

Answering is guarded as well: `refusal_to_answer` is what DecisionService.transition asks before
acting, so a card withdrawn here, or one whose run went some way this module did not see, is
refused with what happened instead of releasing a step of a run that is gone.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from sqlalchemy import bindparam, text

from . import decision_actions
from .core import create_blocking_decision, emit_event

#: The terminal status of a decision whose run ended before anybody answered it.
WITHDRAWN = "withdrawn"

#: Every status in which a decision can still be answered, or reopened by answering (an
#: expired hold default, a dead-end revision_requested): the inbox's own candidates, so the two
#: cannot drift. All of them are withdrawn.
UNANSWERED = decision_actions.ATTENTION_CANDIDATE_STATUSES

#: The sentence a decision step wrote before it recorded its run (workflows._run_step).
_DECISION_STEP_SENTENCE = re.compile(r"Workflow (wfr_[0-9A-Za-z]+) is waiting at decision step")


def _closed_run_states() -> frozenset[str]:
    from .workflow_run_actions import CLOSED_RUN_STATES

    return CLOSED_RUN_STATES


def _recommendation(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation")
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = None
    return rec if isinstance(rec, dict) else {}


def step_gate_question(connection, project_id: str, task_id: Optional[str], question: str,
                       run_id: str, step_id: str) -> str:
    """The question in words, kept distinct from another run's (or step's) open decision.

    Two runs of one workflow ask the same words at the same step. A second open card with the
    same words gets "(2)" — readable on the Approvals page, where a raw run id was not. The ids
    stay in the recommendation, which is the evidence.
    """
    for n in range(1, 50):
        candidate = question if n == 1 else f"{question} ({n})"
        row = connection.execute(
            text("SELECT recommendation->>'workflow_run_id' AS run_id, "
                 "recommendation->>'step_id' AS step_id FROM decisions "
                 "WHERE project_id=:p AND question=:q AND status='pending' "
                 "AND task_id IS NOT DISTINCT FROM :t LIMIT 1"),
            {"p": project_id, "q": candidate, "t": task_id},
        ).mappings().first()
        if row is None or (row["run_id"] == run_id and row["step_id"] == step_id):
            return candidate
    return f"{question} · {run_id[-8:]}"


def open_step_decision(
    connection, project_id: str, task_id: Optional[str], question: str,
    recommendation: Mapping[str, Any], cost_summary: Mapping[str, Any], reason: str, *,
    run_id: str, step_id: str, risk_tier: int = 2, default_action: str = "hold",
) -> str:
    """THE ONE WAY a decision that gates a run's step is raised: it belongs to that run+step.

    Answering it releases only that run's step. The run identity goes in LAST, so a spec's own
    recommendation cannot replace it, and the merge in create_blocking_decision then only ever
    folds a repeat of this same run's same step into the open card.
    """
    if not run_id or not step_id:
        raise ValueError("a step's decision needs the run and the step it gates")
    rec = {**dict(recommendation), "workflow_run_id": run_id, "step_id": step_id}
    return create_blocking_decision(
        connection, project_id, task_id,
        step_gate_question(connection, project_id, task_id, question, run_id, step_id),
        rec, dict(cost_summary), reason, risk_tier=risk_tier, default_action=default_action,
    )


def owning_run_id(connection, decision: Mapping[str, Any]) -> Optional[str]:
    """The run that raised this decision for itself, or None when no run owns it."""
    rec = _recommendation(decision)
    run_id = str(rec.get("workflow_run_id") or "").strip()
    if run_id:
        return run_id
    if rec.get("action") != "resume_workflow":
        return None
    linked = connection.execute(
        text("SELECT workflow_run_id FROM workflow_step_runs WHERE decision_id = :d "
             "ORDER BY created_at LIMIT 1"),
        {"d": decision["id"]},
    ).scalar()
    if linked:
        return str(linked)
    for value in (rec.get("reason"), rec.get("reason_code")):
        match = _DECISION_STEP_SENTENCE.search(str(value or ""))
        if match:
            return match.group(1)
    return None


def _how_the_run_ended(connection, run_id: str) -> Optional[str]:
    """'removed', or the closed status the run is in; None while it can still move."""
    status = connection.execute(
        text("SELECT status FROM workflow_runs WHERE id = :r"), {"r": run_id}).scalar()
    if status is None:
        return "removed"
    return str(status) if status in _closed_run_states() else None


def _awaited_by_another_live_run(connection, decision_id: str, run_id: str) -> bool:
    closed = sorted(_closed_run_states())
    return connection.execute(
        text(
            """
            SELECT 1 FROM workflow_step_runs s
            JOIN workflow_runs r ON r.id = s.workflow_run_id
            WHERE s.decision_id = :d AND s.workflow_run_id <> :r
              AND s.status = 'waiting' AND r.status NOT IN :closed
            LIMIT 1
            """
        ).bindparams(bindparam("closed", expanding=True)),
        {"d": decision_id, "r": run_id, "closed": closed},
    ).first() is not None


def _withdraw(connection, decision: Mapping[str, Any], run_id: str, *, why: str,
              actor: str) -> bool:
    note = f"Withdrawn: {why}. There is no step left for an answer to release."
    changed = connection.execute(
        text(
            """
            UPDATE decisions
            SET status = :withdrawn, resolution_note = :note, updated_at = now()
            WHERE id = :d AND status IN :open
            """
        ).bindparams(bindparam("open", expanding=True)),
        {"d": decision["id"], "withdrawn": WITHDRAWN, "note": note[:1000],
         "open": list(UNANSWERED)},
    ).rowcount
    if changed != 1:
        return False
    emit_event(
        connection, decision["project_id"], "decision.withdrawn", actor, "decision",
        decision["id"],
        {"workflow_run_id": run_id, "why": why, "was": decision.get("status")},
    )
    return True


def _decisions_of(connection, project_id: str, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT d.id, d.project_id, d.status, d.recommendation
            FROM decisions d
            WHERE d.project_id = :p AND d.status IN :open
              AND (
                d.recommendation->>'workflow_run_id' = :r
                OR (d.recommendation->>'action' = 'resume_workflow'
                    AND (d.id IN (SELECT decision_id FROM workflow_step_runs
                                  WHERE workflow_run_id = :r AND decision_id IS NOT NULL)
                         OR d.recommendation->>'reason' LIKE :sentence))
              )
            """
        ).bindparams(bindparam("open", expanding=True)),
        {"p": project_id, "r": run_id, "open": list(UNANSWERED),
         "sentence": f"%Workflow {run_id} is waiting at decision step%"},
    ).mappings().all()
    return [dict(row) for row in rows]


def withdraw_with_run(connection, project_id: str, run_id: str, *, why: str,
                      actor: str) -> list[str]:
    """THE ONE PLACE: close every decision this run raised that nobody answered.

    Called by every path that ends a run for good, BEFORE a removal deletes its step rows (the
    link a decision step's card has back to its run). Idempotent: an answered or already
    withdrawn decision is not touched. Returns the ids it withdrew.
    """
    withdrawn: list[str] = []
    for decision in _decisions_of(connection, project_id, run_id):
        if _awaited_by_another_live_run(connection, decision["id"], run_id):
            continue
        if _withdraw(connection, decision, run_id, why=why, actor=actor):
            withdrawn.append(str(decision["id"]))
    return withdrawn


def refusal_to_answer(connection, decision: Mapping[str, Any]) -> Optional[str]:
    """Why this decision must not be answered because its run is over — or None.

    Read by DecisionService.transition before it acts, for a person and a policy alike.
    """
    if decision.get("status") == WITHDRAWN:
        note = str(decision.get("resolution_note") or "").strip()
        return (f"This decision was withdrawn, so nothing was done. {note}".strip()
                + " Start the workflow again if the work is still wanted.")
    run_id = owning_run_id(connection, decision)
    if not run_id:
        return None
    ended = _how_the_run_ended(connection, run_id)
    if ended is None or _awaited_by_another_live_run(connection, decision["id"], run_id):
        return None
    return (f"The run this decision belongs to ({run_id}) was {ended}, so there is no step "
            "left for an answer to release, and nothing was done. Start the workflow again "
            "if the work is still wanted.")


def withdraw_orphaned(connection, project_id: Optional[str] = None, *,
                      actor: str = "boss") -> list[str]:
    """The one-time repair: withdraw decisions left open by runs that ended before this rule.

    Idempotent, so it runs on every boot; after the first it finds nothing. Uses the same
    ownership and the same withdrawal as `withdraw_with_run`.
    """
    where = ["status IN :open",
             "(recommendation->>'workflow_run_id' IS NOT NULL "
             " OR recommendation->>'action' = 'resume_workflow')"]
    params: dict[str, Any] = {"open": list(UNANSWERED)}
    if project_id:
        where.append("project_id = :p")
        params["p"] = project_id
    rows = connection.execute(
        text("SELECT id, project_id, status, recommendation FROM decisions WHERE "
             + " AND ".join(where)).bindparams(bindparam("open", expanding=True)),
        params,
    ).mappings().all()
    withdrawn: list[str] = []
    for row in rows:
        decision = dict(row)
        run_id = owning_run_id(connection, decision)
        if not run_id:
            continue
        ended = _how_the_run_ended(connection, run_id)
        if ended is None or _awaited_by_another_live_run(connection, decision["id"], run_id):
            continue
        if _withdraw(connection, decision, run_id, why=f"its run {run_id} was {ended}",
                     actor=actor):
            withdrawn.append(str(decision["id"]))
    return withdrawn


# ── a build's questions that nothing will answer (factory walk D2) ───────────────────────────


def _withdraw_with_note(connection, decision_id: str, project_id: str, note: str, *,
                        actor: str, why: dict[str, Any]) -> bool:
    """Close one open decision as `withdrawn` with its note: the same status and the same event
    as `_withdraw`, for a question its build answered another way."""
    changed = connection.execute(
        text("UPDATE decisions SET status = :withdrawn, resolution_note = :note, "
             "updated_at = now() WHERE id = :d AND status IN :open"
             ).bindparams(bindparam("open", expanding=True)),
        {"d": decision_id, "withdrawn": WITHDRAWN, "note": note[:1000],
         "open": list(UNANSWERED)},
    ).rowcount
    if changed != 1:
        return False
    emit_event(connection, project_id, "decision.withdrawn", actor, "decision", decision_id, why)
    return True


def withdraw_superseded_pr_questions(connection, run_id: str, *, actor: str = "boss") -> list[str]:
    """ONE MERGE QUESTION PER PULL REQUEST. A retry lands on the same branch, reuses the open PR
    and asks to merge it again: the earlier attempt's question stayed pending beside the new
    one, so `Merge PR #1?` showed twice on the Floor, the pill and in Approvals. The newest
    attempt's question stands; earlier attempts' questions about the same PR are withdrawn."""
    run = connection.execute(
        text("SELECT id, project_id, decision_id, pull_request_number, pull_request_url, "
             "COALESCE(finalize_ctx->>'root_run_id', id) AS root, "
             "COALESCE((finalize_ctx->>'attempt')::int, 1) AS attempt "
             "FROM code_task_runs WHERE id = :id"), {"id": run_id}).mappings().first()
    if not run or not run["decision_id"] or not (run["pull_request_number"]
                                                 or run["pull_request_url"]):
        return []
    earlier = connection.execute(
        text("SELECT decision_id FROM code_task_runs WHERE project_id = :p AND id <> :id "
             "AND decision_id IS NOT NULL AND decision_id <> :d "
             "AND COALESCE(finalize_ctx->>'root_run_id', id) = :root "
             "AND (pull_request_number = :n OR pull_request_url = :u)"),
        {"p": run["project_id"], "id": run_id, "d": run["decision_id"], "root": run["root"],
         "n": run["pull_request_number"], "u": run["pull_request_url"]}).scalars().all()
    out: list[str] = []
    for decision_id in earlier:
        if _withdraw_with_note(connection, str(decision_id), str(run["project_id"]),
                               f"Superseded by attempt {run['attempt']}", actor=actor,
                               why={"code_task_run_id": run_id, "superseded_by": run["decision_id"]}):
            out.append(str(decision_id))
    return out


_DIRECT_PRD = """
    SELECT d.id AS decision_id, d.project_id, s.id AS step_run_id, r.id AS workflow_run_id,
           c.id AS code_task_run_id
    FROM decisions d
    JOIN workflow_step_runs s ON s.decision_id = d.id AND s.step_id = 'spec_review'
                              AND s.status = 'waiting'
    JOIN workflow_runs r ON r.id = s.workflow_run_id
    JOIN code_task_runs c ON c.id = r.input->>'adopt_code_task_run_id'
    WHERE d.status IN :open
      AND COALESCE(r.input->>'adopt_decision_id', '') = ''
      AND COALESCE(r.input->>'adopt_meeting_run_id', '') = ''
      AND (c.pull_request_url IS NOT NULL OR c.pull_request_number IS NOT NULL)
"""


def withdraw_stale_build_questions(connection, project_id: Optional[str] = None, *,
                                   actor: str = "boss") -> list[str]:
    """The one-time repair for builds made before the fixes (factory walk D2). Idempotent, so it
    runs on every boot; after the first it finds nothing.

    1. A direct change's "PRD approval (automatic)" — a gate nobody and no automation answers —
       on a build that already opened its PR: withdrawn, and its step recorded as having no PRD
       to approve (what a new direct change records), so the run above it moves on.
    2. Duplicate merge questions for one PR across a build's attempts: the newest stands."""
    from . import workflows

    out: list[str] = []
    sql = _DIRECT_PRD + (" AND d.project_id = :p" if project_id else "")
    params: dict[str, Any] = {"open": list(UNANSWERED)}
    if project_id:
        params["p"] = project_id
    for row in connection.execute(
            text(sql).bindparams(bindparam("open", expanding=True)), params).mappings().all():
        if not _withdraw_with_note(
                connection, str(row["decision_id"]), str(row["project_id"]),
                "Withdrawn: a direct change has no PRD to approve; its build already opened "
                "its PR", actor=actor,
                why={"workflow_run_id": row["workflow_run_id"],
                     "code_task_run_id": row["code_task_run_id"]}):
            continue
        out.append(str(row["decision_id"]))
        try:
            with connection.begin_nested():
                workflows.force_complete_waiting_step(
                    connection, project_id=str(row["project_id"]),
                    step_run_id=str(row["step_run_id"]),
                    output={"capability": "spec_review", "tool": None,
                            "status": "not_applicable",
                            "note": "No PRD to approve — this build was started as a direct "
                                    "change."})
        except Exception:  # noqa: BLE001 — the question is closed either way
            pass
    newest = connection.execute(
        text("SELECT DISTINCT ON (COALESCE(finalize_ctx->>'root_run_id', id), "
             "COALESCE(pull_request_number::text, pull_request_url)) id "
             "FROM code_task_runs WHERE decision_id IS NOT NULL "
             "AND (pull_request_number IS NOT NULL OR pull_request_url IS NOT NULL)"
             + (" AND project_id = :p" if project_id else "")
             + " ORDER BY COALESCE(finalize_ctx->>'root_run_id', id), "
               "COALESCE(pull_request_number::text, pull_request_url), created_at DESC"),
        {"p": project_id} if project_id else {}).scalars().all()
    for run_id in newest:
        out.extend(withdraw_superseded_pr_questions(connection, str(run_id), actor=actor))
    return out
