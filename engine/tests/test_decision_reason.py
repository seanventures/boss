from boss_engine.services.core import (
    attach_decision_reason,
    decision_reason_from_payload,
)
from boss_engine.services.sealed_e2e_contract import validate_sealed_decision


def test_attach_decision_reason_sets_reason_code_without_clobber():
    rec = attach_decision_reason({"action": "review_pull_request"}, "merge_decision_required")
    assert rec["reason_code"] == "merge_decision_required"
    assert rec["reason"] == "merge_decision_required"
    assert rec["action"] == "review_pull_request"
    kept = attach_decision_reason({"reason_code": "existing"}, "other")
    assert kept["reason_code"] == "existing"


def test_decision_reason_prefers_reason_code():
    assert (
        decision_reason_from_payload({"reason_code": "merge_decision_required", "reason": "x"})
        == "merge_decision_required"
    )
    assert decision_reason_from_payload({}, "hold") == "hold"
    assert decision_reason_from_payload({}) is None


def test_sealed_decision_accepts_reason_on_recommendation():
    decision = {
        "status": "pending",
        "default_action": "hold",
        "reason": "merge_decision_required",
        "recommendation": {
            "action": "review_pull_request",
            "reason_code": "merge_decision_required",
            "receipt_id": "rcpt_1",
            "no_auto_merge": True,
            "ui_evidence": {},
            "ux_evidence": {},
            "capability_evidence": {},
        },
    }
    assert validate_sealed_decision(decision)["ok"] is True
