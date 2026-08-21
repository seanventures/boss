"""Approving a preview: the single human gate between "it works" and "it is live".

Design: docs/product/DEPLOYMENT_FLEET_AND_APPROVAL_FLOW.md (G3)

A preview is built from the task's BRANCH, before merging — `preview_service` says so in its
first line. So the thing an operator judges when they look at a preview is exactly the thing
the merge decision asks about, and the two were nevertheless unrelated: the preview had no
approval concept at all, and merge approval happened somewhere else entirely.

This module joins them WITHOUT adding a second way to deploy. Approving a preview approves the
run's existing `review_pull_request` decision; that merges; the merge fires the one delivery
trigger that already exists. The rule in CICD_DEPLOYMENT_PLAN §7 — "do not let a deployment be
triggered by anything other than a merge decision" — survives intact, because this is not a
deployment trigger. It is the gate in front of the one that was already there.

What it therefore refuses to do:
  * deploy anything directly;
  * merge by itself (the decision transition does that, with its own role checks and receipt);
  * approve a preview that is not actually running, or whose run has no merge gate open.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..ports import sql
from .core import emit_event

# The branch a code task builds on. The merge decision records `branch_name`, and the preview
# records `run_id`; this is the only thing that links a preview to its gate.
_BRANCH_PREFIX = "boss/code/"


class PreviewGateError(Exception):
    """A preview cannot be approved, with the HTTP status that best explains why."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _preview(connection, preview_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        sql("SELECT * FROM app_previews WHERE id = :i"), {"i": preview_id}
    ).mappings().first()
    return dict(row) if row else None


def _merge_decision_for_run(connection, run_id: str,
                            branch: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The merge gate for this preview, found by branch name.

    Matching on `recommendation->>'branch_name'` rather than task_id: one task can produce
    several runs (a repair opens a new PR on the same task), and approving the preview of run A
    must never approve the gate belonging to run B.

    PREFER the branch the preview actually recorded. Deriving it as `boss/code/<run_id>` is
    wrong for any follow-up run: work started on an existing app's branch keeps THAT branch,
    so run `crun_eedc…` was observed sitting on `boss/code/crun_b913…`. Reconstructing the
    name found no decision and reported "no merge gate" for a task that had one — the same
    class of mistake as deriving a preview's file list instead of reading its lineage.
    """
    candidates = [b for b in (branch, f"{_BRANCH_PREFIX}{run_id}" if run_id else None) if b]
    for candidate in candidates:
        row = connection.execute(
            sql("SELECT id, project_id, task_id, status, recommendation "
                "FROM decisions "
                "WHERE recommendation->>'action' = 'review_pull_request' "
                "  AND recommendation->>'branch_name' = :b "
                "ORDER BY created_at DESC LIMIT 1"),
            {"b": candidate}).mappings().first()
        if row:
            return dict(row)
    return None


def _app_targets(connection, project_id: str, repo: Optional[str]) -> list[dict[str, Any]]:
    """Where an approval will actually land, so the console can say so before the click.

    Matches BOTH spellings of the repo. `app_previews.repo` holds "paramg/boss-sandbox" while
    `deployment_targets.repo` holds whatever `settings.target_github_repo` is — which on this
    deployment is the bare "boss-sandbox". Matching one spelling returned no targets, so the
    gate quietly promised to deploy nothing while targets existed. The merge path itself keys
    on the bare name, so the bare name is the one that must always work.
    """
    if not repo:
        return []
    bare = repo.rsplit("/", 1)[-1]
    rows = connection.execute(
        sql("SELECT t.app_name, t.environment, t.auto_deploy, t.paused, t.target_ref, "
            "       h.name AS host_name, h.arch AS host_arch, h.status AS host_status "
            "FROM deployment_targets t "
            "LEFT JOIN deployment_hosts h ON h.id = t.host_id "
            "WHERE t.project_id = :p AND t.status = 'active' "
            "  AND (t.repo = :full OR t.repo = :bare) "
            "ORDER BY t.app_name"),
        {"p": project_id, "full": repo, "bare": bare}).mappings().all()
    return [dict(r) for r in rows]


def describe_gate(connection, *, preview_id: str) -> Optional[dict[str, Any]]:
    """Everything needed to render "what will approving this do?" — and why it might not.

    Returns None only when the preview itself does not exist; every other refusal is a
    populated `blockers` list, because "you cannot approve" is far more useful with a reason
    attached than as a disabled button.
    """
    preview = _preview(connection, preview_id)
    if not preview:
        return None

    blockers: list[str] = []
    run_id = preview.get("run_id") or ""
    if preview.get("status") != "running":
        # Approving on the strength of a preview that died is approving something nobody saw.
        blockers.append(
            f"preview status is {preview.get('status')!r}, not 'running' — "
            "regenerate it before approving")
    if not run_id:
        blockers.append("preview is not linked to a code task run")

    decision = _merge_decision_for_run(connection, run_id, preview.get("branch")) \
        if (run_id or preview.get("branch")) else None
    if (run_id or preview.get("branch")) and not decision:
        blockers.append("no review_pull_request decision found for this run")
    elif decision and decision.get("status") != "pending":
        blockers.append(f"merge decision is already {decision.get('status')!r}")

    rec = (decision or {}).get("recommendation") or {}
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}

    project_id = (decision or {}).get("project_id") or preview.get("project_id")
    targets = _app_targets(connection, project_id, preview.get("repo"))
    if decision and not targets:
        # Not a blocker: approving still merges, which is a legitimate thing to want. But the
        # console should not imply a deploy that no target exists to receive.
        pass

    return {
        "preview_id": preview_id,
        "preview_status": preview.get("status"),
        "preview_url": preview.get("url"),
        "run_id": run_id,
        "repo": preview.get("repo"),
        "branch": preview.get("branch"),
        "project_id": project_id,
        "decision_id": (decision or {}).get("id"),
        "decision_status": (decision or {}).get("status"),
        "pull_request_url": rec.get("pull_request_url"),
        "title": rec.get("title"),
        "validation_status": rec.get("validation_status"),
        "app_targets": targets,
        "will_deploy": [t["app_name"] for t in targets
                        if t.get("auto_deploy") and not t.get("paused")],
        "can_approve": not blockers,
        "blockers": blockers,
    }


def record_preview_approval(connection, *, preview_id: str, actor: str,
                            note: Optional[str] = None) -> dict[str, Any]:
    """Check the gate and record WHO approved WHAT, then hand back the decision to approve.

    Deliberately does not perform the approval itself: the caller routes it through the same
    decision endpoint a human uses, so role checks, receipts and the merge stay on one path.
    """
    gate = describe_gate(connection, preview_id=preview_id)
    if gate is None:
        raise PreviewGateError(f"no such preview: {preview_id}", status=404)
    if not gate["can_approve"]:
        raise PreviewGateError("; ".join(gate["blockers"]), status=409)

    # The evidence: which preview, which URL, which commit-shaped thing the human actually
    # looked at. "Approved" with no record of what was on screen is not worth much later.
    emit_event(
        connection, gate["project_id"], "preview.approved", actor, "app_preview", preview_id,
        {"run_id": gate["run_id"], "repo": gate["repo"], "branch": gate["branch"],
         "preview_url": gate["preview_url"], "decision_id": gate["decision_id"],
         "pull_request_url": gate["pull_request_url"],
         "will_deploy": gate["will_deploy"], "note": (note or "")[:500]},
    )
    return {**gate, "note": note}
