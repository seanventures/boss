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
is still waiting on (create_blocking_decision can hand two runs one row for the same question).

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
from .core import emit_event

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
