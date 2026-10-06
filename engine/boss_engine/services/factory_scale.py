"""Software-factory scale-up (spec §E): WIP limits + queue-depth gating, an explicit lint stage and
CI-green gate in the fail-closed quality gate, confidence-gated merge with an optional second-model
cross-check and a stored review packet, and a spec→chunk planner that turns one plan into several
small PRs instead of one giant one.

The gate evaluation, confidence scoring, and chunk planning are pure functions (testable without a
live model/GitHub); WIP gating is a DB count; `record_quality_gate` persists the gate + review
packet through the existing quality_gates table so it composes with the governed pipeline.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id

DEFAULT_WIP_LIMIT = 5

# Gates that MUST pass for a merge (fail-closed). Missing/false → the gate fails regardless of score.
REQUIRED_GATES = ("tests_passed", "lint_passed", "ci_green", "reviewer_independent")
# Confidence weights — sum to 1.0 at full confidence.
GATE_WEIGHTS = {
    "tests_passed": 0.30,
    "ci_green": 0.25,
    "lint_passed": 0.15,
    "reviewer_independent": 0.15,
    "test_author_independent": 0.05,
    "adversarial_survived": 0.05,
    "cross_check_agreement": 0.05,
}
DEFAULT_MIN_CONFIDENCE = 0.8


def wip_status(
    connection: Connection, project_id: str, *, wip_limit: int = DEFAULT_WIP_LIMIT
) -> dict[str, Any]:
    """In-flight code tasks vs the per-project WIP limit. Throughput only scales when validation
    keeps up — admit a new task only if a slot is free (spec §E WIP limits + queue-depth gating)."""
    in_flight = int(
        connection.execute(
            text(
                "SELECT count(*) FROM code_task_runs "
                "WHERE project_id = :p AND status = 'running'"
            ),
            {"p": project_id},
        ).scalar()
        or 0
    )
    queued = int(
        connection.execute(
            text(
                "SELECT count(*) FROM code_task_runs "
                "WHERE project_id = :p AND status = 'running' AND phase = 'queued'"
            ),
            {"p": project_id},
        ).scalar()
        or 0
    )
    admit = in_flight < wip_limit
    return {
        "project_id": project_id,
        "in_flight": in_flight,
        "queued": queued,
        "wip_limit": wip_limit,
        "available_slots": max(0, wip_limit - in_flight),
        "admit": admit,
        "reason": "under_wip_limit" if admit else "wip_limit_reached",
    }


def evaluate_gate(
    checks: dict[str, Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required: tuple[str, ...] = REQUIRED_GATES,
) -> dict[str, Any]:
    """Evaluate the fail-closed quality gate and a merge-confidence score. A required gate that is
    false fails the gate outright; merge is allowed only when status is pass AND confidence ≥ floor."""
    cross_check = checks.get("cross_check_agreement")
    gates = {
        "tests_passed": bool(checks.get("tests_passed")),
        "ci_green": bool(checks.get("ci_green")),
        "lint_passed": bool(checks.get("lint_passed")),
        "reviewer_independent": bool(checks.get("reviewer_independent")),
        "test_author_independent": bool(checks.get("test_author_independent")),
        "adversarial_survived": checks.get("adversarial_verdict") == "survived",
        # If no second-model cross-check was run, don't penalize (treat as agreeing).
        "cross_check_agreement": True if cross_check is None else bool(cross_check),
    }
    confidence = round(sum(GATE_WEIGHTS[k] for k, value in gates.items() if value), 4)
    passed = [k for k, value in gates.items() if value]
    failed = [k for k, value in gates.items() if not value]
    required_failed = [k for k in required if not gates.get(k)]
    status = "pass" if not required_failed else "fail"
    allow_merge = status == "pass" and confidence >= min_confidence
    reasons: list[str] = []
    if required_failed:
        reasons.append(f"required gates failed: {required_failed}")
    if confidence < min_confidence:
        reasons.append(f"confidence {confidence:.2f} below floor {min_confidence:.2f}")
    if not reasons:
        reasons.append("all required gates satisfied and confidence above floor")
    return {
        "status": status,
        "confidence": confidence,
        "min_confidence": min_confidence,
        "allow_merge": allow_merge,
        "gates": gates,
        "gates_passed": passed,
        "gates_failed": failed,
        "required_failed": required_failed,
        "fail_closed": True,
        "reasons": reasons,
    }


def chunk_spec(
    spec: dict[str, Any], *, max_files_per_chunk: int = 3, max_chunks: int = 20
) -> dict[str, Any]:
    """Split a plan/spec into small, independently-shippable PR chunks instead of one giant PR
    (spec §E spec→chunk planner). Prefers explicit items; otherwise groups files."""
    title = spec.get("title") or "change"
    items = spec.get("items") or spec.get("tasks") or []
    files = spec.get("files") or []
    base_criteria = spec.get("acceptance_criteria") or []
    chunks: list[dict[str, Any]] = []

    if items:
        strategy = "items"
        for index, item in enumerate(items[:max_chunks], start=1):
            if isinstance(item, dict):
                chunks.append(
                    {
                        "index": index,
                        "title": item.get("title") or f"{title} (part {index})",
                        "scope": item.get("scope"),
                        "files": item.get("files") or [],
                        "acceptance_criteria": item.get("acceptance_criteria") or base_criteria,
                    }
                )
            else:
                chunks.append(
                    {
                        "index": index,
                        "title": str(item),
                        "scope": None,
                        "files": [],
                        "acceptance_criteria": base_criteria,
                    }
                )
    elif files:
        strategy = "files"
        index = 0
        for start in range(0, len(files), max_files_per_chunk):
            index += 1
            if index > max_chunks:
                break
            group = files[start : start + max_files_per_chunk]
            chunks.append(
                {
                    "index": index,
                    "title": f"{title} (part {index})",
                    "scope": None,
                    "files": group,
                    "acceptance_criteria": base_criteria,
                }
            )
    else:
        strategy = "single"
        chunks.append(
            {
                "index": 1,
                "title": title,
                "scope": None,
                "files": [],
                "acceptance_criteria": base_criteria,
            }
        )

    truncated = bool(
        (items and len(items) > max_chunks)
        or (not items and files and (len(files) + max_files_per_chunk - 1) // max_files_per_chunk > max_chunks)
    )
    return {
        "title": title,
        "strategy": strategy,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "truncated": truncated,
    }


class QualityGateOverrideRefused(ValueError):
    """A recorded verdict would be replaced without a stated reason (an API 409)."""


class FactoryScaleService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def record_quality_gate(
        self,
        connection: Connection,
        project_id: str,
        *,
        checks: dict[str, Any],
        actor: str = "release_manager",
        code_task_run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        risk_tier: int = 2,
        override_reason: Optional[str] = None,
        recorded_via: str = "service",
    ) -> dict[str, Any]:
        """Evaluate + persist a quality gate, storing a review-packet artifact and the merge verdict.

        A run that already has a recorded verdict is never overwritten silently: replacing it
        needs `override_reason`, the prior verdict is kept in the evidence, a pass written over a
        verdict that was not a pass is recorded as `waived` (never as a pass the pipeline gave),
        and `quality_gate.waived` / `quality_gate.overridden` is emitted."""
        result = evaluate_gate(checks, min_confidence=min_confidence)
        prior = None
        if code_task_run_id:
            prior = connection.execute(
                text("SELECT id, status, gates_failed, evidence FROM quality_gates "
                     "WHERE project_id = :p AND code_task_run_id = :r FOR UPDATE"),
                {"p": project_id, "r": code_task_run_id}).mappings().first()
        reason = (override_reason or "").strip()
        override: Optional[dict[str, Any]] = None
        if prior is not None:
            if not reason:
                raise QualityGateOverrideRefused(
                    f"code task run {code_task_run_id} already has a recorded quality gate "
                    f"({prior['status']}); replacing it needs override_reason")
            waived = result["status"] == "pass" and str(prior["status"]) != "pass"
            override = {"prior_gate_id": prior["id"], "prior_status": prior["status"],
                        "prior_gates_failed": prior["gates_failed"], "by": actor,
                        "reason": reason[:500], "waived": waived}
            if waived:
                result = {**result, "status": "waived",
                          "reasons": [*result["reasons"], f"waived over a {prior['status']} "
                                      f"verdict by {actor}: {reason[:200]}"]}
        review_packet = {
            "project_id": project_id,
            "code_task_run_id": code_task_run_id,
            "task_id": task_id,
            "status": result["status"],
            "confidence": result["confidence"],
            "allow_merge": result["allow_merge"],
            "gates": result["gates"],
            "gates_passed": result["gates_passed"],
            "gates_failed": result["gates_failed"],
            "reasons": result["reasons"],
            "checks": checks,
            "reviewed_by": actor,
        }
        review_packet_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "review_packet",
            f"Review packet — {result['status']} (confidence {result['confidence']:.2f})",
            review_packet,
        )
        gate_id = make_id("qg")
        gates = result["gates"]
        connection.execute(
            text(
                """
                INSERT INTO quality_gates (
                    id, project_id, task_id, code_task_run_id, risk_tier,
                    test_author_independent, reviewer_independent, tests_passed,
                    adversarial_verdict, status, fail_closed, gates_passed, gates_failed,
                    evidence, lint_passed, ci_green, cross_check_agreement, confidence,
                    review_packet_artifact_id)
                VALUES (
                    :id, :p, :t, :run, :tier, :tai, :ri, :tp, :av, :st, true,
                    CAST(:gp AS JSONB), CAST(:gf AS JSONB), CAST(:ev AS JSONB),
                    :lint, :ci, :xc, :conf, :rp)
                ON CONFLICT (project_id, code_task_run_id) DO UPDATE
                  SET status = EXCLUDED.status, gates_passed = EXCLUDED.gates_passed,
                      gates_failed = EXCLUDED.gates_failed, evidence = EXCLUDED.evidence,
                      lint_passed = EXCLUDED.lint_passed, ci_green = EXCLUDED.ci_green,
                      cross_check_agreement = EXCLUDED.cross_check_agreement,
                      confidence = EXCLUDED.confidence,
                      review_packet_artifact_id = EXCLUDED.review_packet_artifact_id
                """
            ),
            {
                "id": gate_id, "p": project_id, "t": task_id, "run": code_task_run_id,
                "tier": int(risk_tier),
                "tai": gates["test_author_independent"],
                "ri": gates["reviewer_independent"],
                "tp": gates["tests_passed"],
                "av": checks.get("adversarial_verdict", "not_run"),
                "st": result["status"],
                "gp": json.dumps(result["gates_passed"]),
                "gf": json.dumps(result["gates_failed"]),
                "ev": json.dumps({**checks, "confidence": result["confidence"],
                                  "recorded_via": recorded_via,
                                  **({"override": override} if override else {})}),
                "lint": gates["lint_passed"], "ci": gates["ci_green"],
                "xc": checks.get("cross_check_agreement"),
                "conf": result["confidence"], "rp": review_packet_artifact_id,
            },
        )
        if override is not None:
            emit_event(
                connection, project_id,
                "quality_gate.waived" if override["waived"] else "quality_gate.overridden",
                actor, "quality_gate", gate_id,
                {**override, "status": result["status"], "code_task_run_id": code_task_run_id})
        emit_event(
            connection, project_id, "quality_gate.recorded", actor, "quality_gate", gate_id,
            {"status": result["status"], "confidence": result["confidence"],
             "allow_merge": result["allow_merge"], "gates_failed": result["gates_failed"],
             "review_packet_artifact_id": review_packet_artifact_id,
             "code_task_run_id": code_task_run_id})
        return {
            "gate_id": gate_id,
            "review_packet_artifact_id": review_packet_artifact_id,
            **result,
        }
