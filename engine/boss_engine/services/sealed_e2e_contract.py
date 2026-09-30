"""Fail-closed contract for a sealed CEO → code evidence → PR decision → Receipt path.

A path is sealed only when every spendy effect has a unique Receipt, the PR
is held for a human Decision, and workspace evidence is attached (empty is
honest; invented scores are not). Simulated / fixture work is never sealed.

This module is pure. It does not reserve budget, enqueue jobs, or call
providers. Live PR success remains unproven until a weekly dogfood run.
"""

from __future__ import annotations

from typing import Any

SEALED_EFFECT_KIND = "github.pull_request.create"
SEALED_DECISION_ACTION = "review_pull_request"
SEALED_DECISION_REASON = "merge_decision_required"
SEALED_DEFAULT_ACTION = "hold"
SEALED_EVIDENCE_KEYS = (
    "ui_evidence",
    "ux_evidence",
    "capability_evidence",
)
SEALED_RECEIPT_IDENTITY_FIELDS = (
    "dedupe_key",
    "effect_kind",
    "status",
)
SEALED_RECEIPT_RESULT_FIELDS = (
    "decision_id",
    "pull_request_url",
    "receipt_id",
    *SEALED_EVIDENCE_KEYS,
)
SEALED_CODE_EVIDENCE_FIELDS = (
    "quality_gate_id",
    "artifact_id",
    "test_report_artifact_id",
)
PROCESS_HANDOFF_EXPORTS = (
    "AgentProcessService",
    "PRD_TO_ACCEPTED_SPEC",
)
PROCESS_HANDOFF_ENDPOINTS = (
    "/v1/agent-processes/templates",
    "/v1/agent-processes/runs",
    "/v1/agent-processes/tick-all",
)
PROCESS_HANDOFF_STEPS = (
    "intake_prd",
    "captain_build",
    "qa_check",
    "ux_review",
    "captain_verify",
    "panel_certify",
    "captain_accept",
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def attach_sealed_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Copy evidence keys onto a receipt/decision payload. Empty dicts stay empty.

    Missing evidence is recorded as {}. Callers must not invent scores.
    """
    src = _as_dict(evidence)
    out: dict[str, Any] = {}
    for key in SEALED_EVIDENCE_KEYS:
        blob = src.get(key)
        out[key] = blob if isinstance(blob, dict) else {}
    return out


def attach_sealed_receipt_result(
    result: dict[str, Any] | None,
    *,
    evidence: dict[str, Any] | None = None,
    decision_id: str | None = None,
    receipt_id: str | None = None,
    quality_gate_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Merge sealed identity + evidence onto a PR receipt result."""
    out = dict(_as_dict(result))
    out.update(attach_sealed_evidence(evidence if evidence is not None else out))
    if decision_id:
        out["decision_id"] = decision_id
    if receipt_id:
        out["receipt_id"] = receipt_id
    if quality_gate_id:
        out["quality_gate_id"] = quality_gate_id
    if run_id:
        out["run_id"] = run_id
    out["no_auto_merge"] = True
    return out


def validate_sealed_receipt(receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Fail closed: a PR receipt without decision hold + evidence is not sealed."""
    missing: list[str] = []
    row = _as_dict(receipt)
    if not str(row.get("dedupe_key") or "").strip():
        missing.append("dedupe_key")
    if row.get("effect_kind") != SEALED_EFFECT_KIND:
        missing.append("effect_kind")
    if row.get("status") != "succeeded":
        missing.append("status")
    result = _as_dict(row.get("result"))
    if not str(result.get("decision_id") or "").strip():
        missing.append("result.decision_id")
    pr_url = result.get("pull_request_url") or row.get("provider_ref")
    if not (isinstance(pr_url, str) and pr_url.strip()):
        missing.append("result.pull_request_url")
    if not str(result.get("receipt_id") or row.get("id") or "").strip():
        missing.append("result.receipt_id")
    for key in SEALED_EVIDENCE_KEYS:
        if key not in result:
            missing.append(f"result.{key}")
        elif result.get(key) is None:
            missing.append(f"result.{key}")
    if not any(result.get(field) for field in SEALED_CODE_EVIDENCE_FIELDS):
        missing.append("result.code_evidence")
    if result.get("simulated") is True or row.get("simulated") is True:
        missing.append("not_simulated")
    return {
        "ok": not missing,
        "missing": missing,
        "sealed": not missing,
        "proven_live": False,
    }


def validate_sealed_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    """Fail closed: PR decisions hold by default and carry receipt + evidence."""
    missing: list[str] = []
    row = _as_dict(decision)
    rec = _as_dict(row.get("recommendation"))
    default_action = row.get("default_action") or SEALED_DEFAULT_ACTION
    if default_action != SEALED_DEFAULT_ACTION:
        missing.append("default_action")
    status = row.get("status")
    if status not in {None, "pending", "blocked"}:
        missing.append("status")
    if rec.get("action") != SEALED_DECISION_ACTION:
        missing.append("recommendation.action")
    reason = row.get("reason") or rec.get("reason_code")
    if reason != SEALED_DECISION_REASON:
        missing.append("reason")
    if not str(rec.get("receipt_id") or "").strip():
        missing.append("recommendation.receipt_id")
    if rec.get("no_auto_merge") is False:
        missing.append("no_auto_merge")
    for key in SEALED_EVIDENCE_KEYS:
        if key not in rec:
            missing.append(f"recommendation.{key}")
        elif rec.get(key) is None:
            missing.append(f"recommendation.{key}")
    return {
        "ok": not missing,
        "missing": missing,
        "hold_default": default_action == SEALED_DEFAULT_ACTION,
        "proven_live": False,
    }


def validate_evidence_attach(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Keys must exist. Empty is honest. Invented rubric scores fail."""
    missing: list[str] = []
    invented: list[str] = []
    src = _as_dict(payload)
    for key in SEALED_EVIDENCE_KEYS:
        if key not in src:
            missing.append(key)
            continue
        blob = src.get(key)
        if not isinstance(blob, dict):
            missing.append(key)
            continue
        if blob.get("invented") is True:
            invented.append(key)
    return {
        "ok": not missing and not invented,
        "missing": missing,
        "invented": invented,
        "proven_live": False,
    }


def simulated_is_not_sealed(*, simulated: bool = False, auto_build: bool = False) -> bool:
    """Simulated / planned process handoffs never count as a sealed factory PR."""
    return not simulated and bool(auto_build)


__all__ = [
    "PROCESS_HANDOFF_ENDPOINTS",
    "PROCESS_HANDOFF_EXPORTS",
    "PROCESS_HANDOFF_STEPS",
    "SEALED_CODE_EVIDENCE_FIELDS",
    "SEALED_DECISION_ACTION",
    "SEALED_DECISION_REASON",
    "SEALED_DEFAULT_ACTION",
    "SEALED_EFFECT_KIND",
    "SEALED_EVIDENCE_KEYS",
    "SEALED_RECEIPT_IDENTITY_FIELDS",
    "SEALED_RECEIPT_RESULT_FIELDS",
    "attach_sealed_evidence",
    "attach_sealed_receipt_result",
    "simulated_is_not_sealed",
    "validate_evidence_attach",
    "validate_sealed_decision",
    "validate_sealed_receipt",
]
