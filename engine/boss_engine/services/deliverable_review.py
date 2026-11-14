"""A person's usefulness review of a recorded delivery, never a merge/deploy approval.

The snapshot is read in one database statement. It describes BOSS's recorded revision
and evidence, not the current state of a remote branch. No adapter, model or job is called.
Reviews are append-only; the run lock serializes duplicate submissions and keeps a stale
page from accepting a replacement delivery. Changes requested are notes for Change it.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import text

from .core import emit_event, make_id


class ReviewConflict(ValueError):
    pass


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def snapshot(connection, project_id: str, run_id: str) -> dict:
    row = connection.execute(text("""
        WITH run AS (
          SELECT * FROM code_task_runs WHERE id=:r AND project_id=:p
        ), reservations AS (
          SELECT b.id, b.status, b.amount_usd_micros, b.expires_at
          FROM budget_reservations b, run r
          WHERE b.project_id=:p AND (b.id=r.reservation_id OR b.job_id IN (
            SELECT id FROM jobs WHERE project_id=:p AND payload->>'run_id'=:r))
        ), costs AS (
          SELECT c.id, c.receipt_id, c.status, c.estimated_usd_micros,
                 c.actual_usd_micros, c.equivalent_usd_micros, c.included_in_subscription
          FROM cost_receipts c WHERE c.project_id=:p
            AND c.reservation_id IN (SELECT id FROM reservations)
        )
        SELECT r.id, r.status, r.pull_request_url, r.decision_id,
          r.spec->'acceptance_criteria' AS criteria,
          d.recommendation AS delivery,
          t.id AS test_report_id, t.content AS test_report,
          q.id AS quality_gate_id, q.status AS quality_status,
          q.gates_passed, q.gates_failed, q.evidence AS quality_evidence,
          pr.id AS receipt_id, pr.status AS receipt_status, pr.effect_kind,
          pr.result->>'decision_id' AS receipt_decision,
          pr.result->>'pull_request_url' AS receipt_url,
          COALESCE((SELECT jsonb_agg(to_jsonb(b) ORDER BY b.id) FROM reservations b), '[]') AS reservations,
          COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM costs c), '[]') AS costs,
          (SELECT count(*) FROM model_gateway_invocations i JOIN jobs j ON j.id=i.job_id
           WHERE i.project_id=:p AND j.project_id=:p AND j.payload->>'run_id'=:r
             AND (i.status='in_progress' OR i.actual_cost_usd_micros IS NULL)) AS unresolved_calls
        FROM run r
        LEFT JOIN decisions d ON d.id=r.decision_id AND d.project_id=r.project_id
        LEFT JOIN artifacts t ON t.id=d.recommendation->>'test_report_artifact_id'
          AND t.project_id=r.project_id AND t.artifact_type='test_report'
        LEFT JOIN quality_gates q ON q.code_task_run_id=r.id AND q.project_id=r.project_id
        LEFT JOIN receipts pr ON pr.id=d.recommendation->>'receipt_id' AND pr.project_id=r.project_id
    """), {"p": project_id, "r": run_id}).mappings().first()
    if row is None:
        raise LookupError("Code task not found.")
    delivery = row["delivery"] if isinstance(row["delivery"], dict) else {}
    report = row["test_report"] if isinstance(row["test_report"], dict) else {}
    head = delivery.get("head_sha")
    checked = report.get("head_sha")
    if not isinstance(checked, str) or not re.fullmatch(r"[0-9a-f]{40}", checked):
        checked = None
    valid_head = isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head)
    linked = (delivery.get("action") == "review_pull_request"
              and delivery.get("run_id") == run_id
              and row["effect_kind"] == "github.pull_request.create"
              and row["receipt_status"] == "succeeded"
              and row["receipt_decision"] == row["decision_id"]
              and row["receipt_url"] == row["pull_request_url"]
              and delivery.get("pull_request_url") == row["pull_request_url"])
    reason = None
    if row["status"] != "completed":
        reason = "The build has not completed. Its current work is still in Outputs."
    elif not valid_head or not linked or not row["pull_request_url"]:
        reason = "This delivery has no linked receipt and recorded revision to review."
    check_state = ("not_recorded" if not row["test_report_id"] else
                   "unbound" if not checked else
                   "invalidated" if checked != head else str(report.get("status") or "unknown"))
    costs = row["costs"]
    reservations = row["reservations"]
    held = sum(b["amount_usd_micros"] for b in reservations if b["status"] == "active")
    # Included legacy rows may retain API-equivalent amounts in their actual column.
    # They represent no per-call cash. Keep missing actual evidence distinct from zero.
    actuals = [0 if c["included_in_subscription"] else c["actual_usd_micros"]
               for c in costs if c["actual_usd_micros"] is not None]
    included_value = sum(next((c[key] for key in ("equivalent_usd_micros", "actual_usd_micros",
                                                "estimated_usd_micros") if c[key] is not None), 0)
                         for c in costs if c["included_in_subscription"])
    uncertain = int(row["unresolved_calls"]) + sum(c["actual_usd_micros"] is None for c in costs)
    result = {
        "project_id": project_id, "run_id": run_id, "run_status": row["status"],
        "delivered_head": head if valid_head else None, "pull_request_url": row["pull_request_url"],
        "decision_id": row["decision_id"], "receipt_id": row["receipt_id"],
        "receipt_status": row["receipt_status"], "effect_kind": row["effect_kind"],
        "criteria": _strings(row["criteria"]),
        "checks": {"state": check_state, "recorded_status": str(report.get("status") or "unknown"),
                   "checked_head": checked, "artifact_id": row["test_report_id"],
                   "evidence_hash": _hash(report), "quality_evidence_hash": _hash(row["quality_evidence"]),
                   "commands": _strings(report.get("tests_added_or_run")),
                   "quality_gate_id": row["quality_gate_id"], "quality_status": row["quality_status"],
                   "gates_passed": _strings(row["gates_passed"]), "gates_failed": _strings(row["gates_failed"])},
        "cost": {"observed_usd_micros": sum(actuals) if actuals else None,
                 "estimated_usd_micros": sum(c["estimated_usd_micros"] for c in costs
                                             if not c["included_in_subscription"]),
                 "included_equivalent_usd_micros": included_value,
                 "held_usd_micros": held, "unresolved_records": uncertain,
                 "confidence": "incomplete" if uncertain or held else "recorded" if actuals else "unknown",
                 "included_calls": sum(bool(c["included_in_subscription"]) for c in costs),
                 "receipts": costs, "reservations": reservations},
    }
    return {"snapshot": result, "snapshot_hash": _hash(result), "reviewable": reason is None,
            "unavailable_reason": reason}


def history(connection, project_id: str, run_id: str) -> list[dict]:
    return [dict(row) for row in connection.execute(text("""
        SELECT id, actor, outcome, note, snapshot_hash, snapshot, created_at
        FROM deliverable_reviews WHERE project_id=:p AND run_id=:r ORDER BY created_at DESC, id DESC
    """), {"p": project_id, "r": run_id}).mappings()]


def record(connection, *, project_id: str, run_id: str, actor: str, snapshot_hash: str,
           outcome: str, note: str, submission_id: str) -> dict:
    """Caller authorizes the human at this project. Only local review/audit rows change."""
    if outcome not in ("accepted", "changes_requested"):
        raise ValueError("Choose accepted or changes_requested.")
    note = note.strip()
    if outcome == "changes_requested" and not note:
        raise ValueError("Describe the bounded change you want.")
    if len(note) > 4000 or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", submission_id):
        raise ValueError("Invalid review note or submission identifier.")
    found = connection.execute(text(
        "SELECT id FROM code_task_runs WHERE id=:r AND project_id=:p FOR UPDATE"
    ), {"p": project_id, "r": run_id}).scalar()
    if found is None:
        raise LookupError("Code task not found.")
    prior = connection.execute(text("""
        SELECT * FROM deliverable_reviews WHERE project_id=:p AND run_id=:r AND submission_id=:s
    """), {"p": project_id, "r": run_id, "s": submission_id}).mappings().first()
    if prior:
        if (prior["actor"], prior["snapshot_hash"], prior["outcome"], prior["note"]) != (
                actor, snapshot_hash, outcome, note):
            raise ReviewConflict("This submission already records different review evidence.")
        return {"id": prior["id"], "outcome": outcome, "reused": True}
    current = snapshot(connection, project_id, run_id)
    if not current["reviewable"]:
        raise ReviewConflict(current["unavailable_reason"])
    if current["snapshot_hash"] != snapshot_hash:
        raise ReviewConflict("The recorded delivery or evidence changed. Reload the result before reviewing it.")
    review_id = make_id("drev")
    connection.execute(text("""
        INSERT INTO deliverable_reviews
          (id, project_id, run_id, actor, outcome, note, snapshot_hash, snapshot, submission_id)
        VALUES (:id, :p, :r, :a, :o, :n, :h, CAST(:snap AS JSONB), :s)
    """), {"id": review_id, "p": project_id, "r": run_id, "a": actor, "o": outcome,
            "n": note, "h": snapshot_hash, "snap": json.dumps(current["snapshot"], default=str),
            "s": submission_id})
    emit_event(connection, project_id, "code_task.result_reviewed", actor, "code_task_run", run_id,
               {"review_id": review_id, "outcome": outcome, "snapshot_hash": snapshot_hash,
                "delivered_head": current["snapshot"]["delivered_head"]})
    if outcome == "changes_requested":
        # AP-P4: a delivery sent back is a failed acceptance — a miss, PROPOSED to the
        # annotation inbox for a person to confirm (services/evals; never raises).
        from .evals import capture_failed_acceptance

        capture_failed_acceptance(connection, project_id, run_id=run_id, review_id=review_id,
                                  actor=actor, note=note, snapshot=current["snapshot"])
    return {"id": review_id, "outcome": outcome, "reused": False}
