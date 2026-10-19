"""A panel's PRD decision when a WORKFLOW builds what it specs.

A meeting convened by a workflow step raises the PRD decision ("publish this PRD", with
`start_code_task`). Approving one used to start a build from the decision itself
(decisions._start_code_task_after_prd) — correct for a panel convened on its own, and a second
build when the workflow that convened it has a Build step of its own. On wfr_723213d8 (Minor
Feature v6, 2026-10-19) the run's build crun_e18a3a67 was already running while dec_eb911c56
waited; approving it would have started another build on the same branch, racing the first.

So, for a decision whose panel a workflow convened:

- `workflow_build_for_prd` says which run governs it, whether that run builds, and the build it
  already started. The approval then never starts one: the run's own Build step builds the spec
  once it is approved (runner._hold_for_spec holds the panel's step until then).
- `mark_gate_skipped` is for a run that went past the spec without it (started before the hold
  existed): the card says the build is already running and offers only Acknowledge or Reject
  (decision_actions.allowed_actions reads the mark).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Where the mark lives on the decision's recommendation.
SKIPPED_KEY = "spec_gate_skipped"

def _builds(connection: Connection, run_id: str) -> bool:
    """Whether the run's workflow has a step that writes code (workflow_asks.BUILD_CAPABILITIES)."""
    from . import workflows
    from .workflow_asks import BUILD_CAPABILITIES

    try:
        steps = workflows._run_spec(connection, run_id).get("steps") or []
    except Exception:  # noqa: BLE001 — an unreadable run is not one known to build
        return False
    return any(str((s.get("binding") or {}).get("capability") or "") in BUILD_CAPABILITIES
               for s in steps if isinstance(s, dict))


def run_build(connection: Connection, run_id: str) -> Optional[dict[str, Any]]:
    """The newest build this workflow run started that is not dead, or None."""
    row = connection.execute(text(
        "SELECT id, status FROM code_task_runs WHERE finalize_ctx->>'workflow_run_id' = :r "
        "AND status NOT IN ('failed', 'cancelled') ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id}).mappings().first()
    return {"id": str(row["id"]), "status": str(row["status"])} if row else None


def workflow_build_for_prd(connection: Connection, decision_id: str,
                           task_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The workflow run that convened this PRD decision's panel AND builds it, or None.

    Read from the panel itself: `meeting_runs.decision_id` names the decision, and its
    `finalize_ctx.origin.step_run_id` the workflow step that convened it. A panel convened from
    the composer (the panel path, where the build is what then starts a workflow) has no step,
    and its approval starts the build exactly as before.

    By the panel's task too: an AUTO-approved PRD is approved inside the panel's finalize,
    before `meeting_runs.decision_id` is written.
    """
    row = connection.execute(text(
        "SELECT s.workflow_run_id FROM meeting_runs m "
        "JOIN workflow_step_runs s ON s.id = m.finalize_ctx->'origin'->>'step_run_id' "
        "WHERE m.decision_id = :d OR (m.decision_id IS NULL AND m.task_id = :t) "
        "ORDER BY (m.decision_id = :d) DESC NULLS LAST, m.created_at DESC LIMIT 1"),
        {"d": decision_id, "t": task_id or ""}).mappings().first()
    if not row or not row["workflow_run_id"]:
        return None
    run_id = str(row["workflow_run_id"])
    if not _builds(connection, run_id):
        return None
    return {"workflow_run_id": run_id, "build": run_build(connection, run_id)}


def skipped_sentence(build_run_id: str) -> str:
    return (f"The workflow did not wait for this spec: its build {build_run_id} is already "
            "running from the panel's PRD. Approving starts nothing — it records that you "
            "accept the spec. Rejecting records that you do not; the build is not stopped by "
            "this card (cancel it in Factory if it should not go on).")


def mark_gate_skipped(connection: Connection, *, run_id: str) -> list[str]:
    """Mark every still-open PRD decision of this run's panels as skipped by its build.

    Called while a build step of the run is in flight. A run that held its spec has no open PRD
    decision by then, so this marks only a run that went past one. Returns the decisions marked.
    """
    build = run_build(connection, run_id)
    if not build:
        return []
    rows = connection.execute(text(
        "SELECT d.id FROM workflow_step_runs s "
        "JOIN meeting_runs m ON m.finalize_ctx->'origin'->>'step_run_id' = s.id "
        "JOIN decisions d ON d.id = m.decision_id "
        "WHERE s.workflow_run_id = :r AND d.status IN ('pending', 'delegated', 'expired') "
        f"AND d.recommendation->'{SKIPPED_KEY}' IS NULL"),
        {"r": run_id}).scalars().all()
    for decision_id in rows:
        mark = {SKIPPED_KEY: {"run_id": run_id, "build_run_id": build["id"],
                              "said": skipped_sentence(build["id"])}}
        connection.execute(text(
            "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) "
            "|| CAST(:m AS JSONB), updated_at = now() WHERE id = :d"),
            {"d": decision_id, "m": json.dumps(mark)})
    return [str(d) for d in rows]
