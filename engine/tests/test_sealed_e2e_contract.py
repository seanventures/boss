"""Fail-closed sealed E2E contract — no spend, no live PR claim."""

from __future__ import annotations

from boss_engine.services.sealed_e2e_contract import (
    PROCESS_HANDOFF_ENDPOINTS,
    PROCESS_HANDOFF_STEPS,
    SEALED_DECISION_ACTION,
    SEALED_DECISION_REASON,
    SEALED_EFFECT_KIND,
    SEALED_EVIDENCE_KEYS,
    attach_sealed_evidence,
    attach_sealed_receipt_result,
    simulated_is_not_sealed,
    validate_evidence_attach,
    validate_sealed_decision,
    validate_sealed_receipt,
)


def _complete_receipt(**overrides):
    result = attach_sealed_receipt_result(
        {
            "decision_id": "dec_1",
            "pull_request_url": "https://github.com/acme/app/pull/9",
            "receipt_id": "rcpt_1",
            "artifact_id": "art_review",
            "quality_gate_id": "qg_1",
        },
        evidence={
            "ui_evidence": {"states_covered": ["ok"]},
            "ux_evidence": {"has_e2e": False, "rubric": {}},
            "capability_evidence": {"scenario_mapping": []},
        },
    )
    payload = {
        "id": "rcpt_1",
        "dedupe_key": "code:task:crun_1",
        "effect_kind": SEALED_EFFECT_KIND,
        "status": "succeeded",
        "provider_ref": "https://github.com/acme/app/pull/9",
        "result": result,
    }
    payload.update(overrides)
    return payload


def _complete_decision(**overrides):
    rec = {
        "action": SEALED_DECISION_ACTION,
        "receipt_id": "rcpt_1",
        "no_auto_merge": True,
        **attach_sealed_evidence(
            {
                "ui_evidence": {"states_covered": ["ok"]},
                "ux_evidence": {"rubric": {}},
                "capability_evidence": {},
            }
        ),
    }
    payload = {
        "id": "dec_1",
        "status": "pending",
        "default_action": "hold",
        "reason": SEALED_DECISION_REASON,
        "recommendation": rec,
    }
    payload.update(overrides)
    return payload


def test_receipt_without_decision_hold_is_not_sealed():
    receipt = _complete_receipt()
    receipt["result"] = dict(receipt["result"])
    receipt["result"].pop("decision_id")
    verdict = validate_sealed_receipt(receipt)
    assert verdict["ok"] is False
    assert "result.decision_id" in verdict["missing"]
    assert verdict["proven_live"] is False


def test_receipt_without_evidence_keys_is_not_sealed():
    receipt = _complete_receipt()
    receipt["result"] = {
        "decision_id": "dec_1",
        "pull_request_url": "https://github.com/acme/app/pull/9",
        "receipt_id": "rcpt_1",
        "artifact_id": "art_review",
    }
    verdict = validate_sealed_receipt(receipt)
    assert verdict["ok"] is False
    assert "result.ui_evidence" in verdict["missing"]
    assert "result.ux_evidence" in verdict["missing"]
    assert "result.capability_evidence" in verdict["missing"]


def test_receipt_without_code_evidence_is_not_sealed():
    receipt = _complete_receipt()
    receipt["result"] = attach_sealed_receipt_result(
        {
            "decision_id": "dec_1",
            "pull_request_url": "https://github.com/acme/app/pull/9",
            "receipt_id": "rcpt_1",
        }
    )
    verdict = validate_sealed_receipt(receipt)
    assert verdict["ok"] is False
    assert "result.code_evidence" in verdict["missing"]


def test_complete_receipt_is_structurally_sealed_but_not_a_live_proof():
    verdict = validate_sealed_receipt(_complete_receipt())
    assert verdict["ok"] is True
    assert verdict["missing"] == []
    assert verdict["proven_live"] is False


def test_empty_evidence_dicts_are_honest_and_seal():
    attached = attach_sealed_evidence({})
    assert set(attached) == set(SEALED_EVIDENCE_KEYS)
    assert all(attached[key] == {} for key in SEALED_EVIDENCE_KEYS)
    verdict = validate_evidence_attach(attached)
    assert verdict["ok"] is True
    assert verdict["invented"] == []


def test_missing_or_invented_evidence_fails_closed():
    assert validate_evidence_attach({})["ok"] is False
    missing = validate_evidence_attach({"ui_evidence": {}})
    assert missing["ok"] is False
    assert "ux_evidence" in missing["missing"]
    invented = validate_evidence_attach(
        {
            "ui_evidence": {"invented": True},
            "ux_evidence": {},
            "capability_evidence": {},
        }
    )
    assert invented["ok"] is False
    assert "ui_evidence" in invented["invented"]


def test_decision_without_hold_or_receipt_is_not_sealed():
    auto = _complete_decision(default_action="approve")
    hold = validate_sealed_decision(auto)
    assert hold["ok"] is False
    assert "default_action" in hold["missing"]

    rec = dict(_complete_decision()["recommendation"])
    rec.pop("receipt_id")
    no_receipt = _complete_decision(recommendation=rec)
    verdict = validate_sealed_decision(no_receipt)
    assert verdict["ok"] is False
    assert "recommendation.receipt_id" in verdict["missing"]


def test_decision_hold_default_with_evidence_is_structurally_sealed():
    verdict = validate_sealed_decision(_complete_decision())
    assert verdict["ok"] is True
    assert verdict["hold_default"] is True
    assert verdict["proven_live"] is False


def test_simulated_process_is_never_a_sealed_factory_path():
    assert simulated_is_not_sealed(simulated=True, auto_build=False) is False
    assert simulated_is_not_sealed(simulated=False, auto_build=False) is False
    assert simulated_is_not_sealed(simulated=False, auto_build=True) is True


def test_process_handoff_catalog_covers_ceo_to_accept():
    assert "/v1/agent-processes/runs" in PROCESS_HANDOFF_ENDPOINTS
    assert "/v1/agent-processes/tick-all" in PROCESS_HANDOFF_ENDPOINTS
    assert PROCESS_HANDOFF_STEPS[0] == "intake_prd"
    assert PROCESS_HANDOFF_STEPS[-1] == "captain_accept"


def test_factory_propose_pr_wires_receipt_decision_and_evidence():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    factory = (root / "engine/boss_engine/services/software_factory.py").read_text(
        encoding="utf-8"
    )
    governed = (root / "engine/boss_engine/services/governed_code_task_service.py").read_text(
        encoding="utf-8"
    )
    assert 'dedupe_key' in factory
    assert '"github.pull_request.create"' in factory
    assert "create_or_reuse" in factory
    assert "create_blocking" in factory
    assert "merge_decision_required" in factory
    assert "mark_succeeded" in factory
    assert "attach_sealed_evidence" in factory
    assert "attach_sealed_receipt_result" in factory
    assert "no_auto_merge" in factory
    assert "evidence=" in governed
    assert "quality_gate_id=" in governed
