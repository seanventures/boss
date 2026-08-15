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
    # Simulated + auto_build is still a process handoff, not a sealed factory PR.
    assert simulated_is_not_sealed(simulated=True, auto_build=True) is False


def test_simulated_receipt_is_not_sealed():
    """A structurally complete PR receipt is still not sealed if it is simulated."""
    top = _complete_receipt(simulated=True)
    top_verdict = validate_sealed_receipt(top)
    assert top_verdict["ok"] is False
    assert "not_simulated" in top_verdict["missing"]
    assert top_verdict["proven_live"] is False
    assert top_verdict["sealed"] is False

    nested = _complete_receipt()
    nested["result"] = dict(nested["result"])
    nested["result"]["simulated"] = True
    nested_verdict = validate_sealed_receipt(nested)
    assert nested_verdict["ok"] is False
    assert "not_simulated" in nested_verdict["missing"]
    assert nested_verdict["proven_live"] is False


def test_receipt_missing_identity_or_pr_url_is_not_sealed():
    no_dedupe = _complete_receipt(dedupe_key="")
    assert "dedupe_key" in validate_sealed_receipt(no_dedupe)["missing"]

    wrong_kind = _complete_receipt(effect_kind="code.task.simulated")
    assert "effect_kind" in validate_sealed_receipt(wrong_kind)["missing"]

    failed = _complete_receipt(status="failed")
    assert "status" in validate_sealed_receipt(failed)["missing"]

    no_pr = _complete_receipt(provider_ref="")
    no_pr["result"] = dict(no_pr["result"])
    no_pr["result"].pop("pull_request_url", None)
    assert "result.pull_request_url" in validate_sealed_receipt(no_pr)["missing"]

    empty = validate_sealed_receipt(None)
    assert empty["ok"] is False
    assert empty["proven_live"] is False
    assert "dedupe_key" in empty["missing"]


def test_decision_missing_evidence_or_auto_merge_is_not_sealed():
    rec = dict(_complete_decision()["recommendation"])
    rec.pop("ui_evidence")
    rec.pop("ux_evidence")
    rec.pop("capability_evidence")
    missing = validate_sealed_decision(_complete_decision(recommendation=rec))
    assert missing["ok"] is False
    assert "recommendation.ui_evidence" in missing["missing"]
    assert "recommendation.ux_evidence" in missing["missing"]
    assert "recommendation.capability_evidence" in missing["missing"]

    mergeable = dict(_complete_decision()["recommendation"])
    mergeable["no_auto_merge"] = False
    auto_merge = validate_sealed_decision(_complete_decision(recommendation=mergeable))
    assert auto_merge["ok"] is False
    assert "no_auto_merge" in auto_merge["missing"]

    approved = validate_sealed_decision(_complete_decision(status="approved"))
    assert approved["ok"] is False
    assert "status" in approved["missing"]
    assert approved["proven_live"] is False

    wrong_action = dict(_complete_decision()["recommendation"])
    wrong_action["action"] = "merge_pull_request"
    action = validate_sealed_decision(_complete_decision(recommendation=wrong_action))
    assert action["ok"] is False
    assert "recommendation.action" in action["missing"]


def test_none_or_non_dict_evidence_fails_closed_and_never_proves_live():
    none_values = validate_evidence_attach(
        {"ui_evidence": None, "ux_evidence": None, "capability_evidence": None}
    )
    assert none_values["ok"] is False
    assert set(none_values["missing"]) == set(SEALED_EVIDENCE_KEYS)
    assert none_values["proven_live"] is False

    not_dicts = validate_evidence_attach(
        {
            "ui_evidence": "ok",
            "ux_evidence": [],
            "capability_evidence": 0,
        }
    )
    assert not_dicts["ok"] is False
    assert set(not_dicts["missing"]) == set(SEALED_EVIDENCE_KEYS)

    receipt = _complete_receipt()
    receipt["result"] = dict(receipt["result"])
    for key in SEALED_EVIDENCE_KEYS:
        receipt["result"][key] = None
    verdict = validate_sealed_receipt(receipt)
    assert verdict["ok"] is False
    assert f"result.{SEALED_EVIDENCE_KEYS[0]}" in verdict["missing"]
    assert verdict["proven_live"] is False


def test_attach_result_holds_merge_and_never_claims_live_proof():
    attached = attach_sealed_receipt_result(
        {"decision_id": "dec_1", "pull_request_url": "https://example.test/pull/1"},
        evidence={},
        receipt_id="rcpt_hold",
        quality_gate_id="qg_1",
    )
    assert attached["no_auto_merge"] is True
    assert attached["receipt_id"] == "rcpt_hold"
    assert attached["quality_gate_id"] == "qg_1"
    assert attached["ui_evidence"] == {}
    assert attached["ux_evidence"] == {}
    assert attached["capability_evidence"] == {}
    receipt = {
        "id": "rcpt_hold",
        "dedupe_key": "code:task:hold",
        "effect_kind": SEALED_EFFECT_KIND,
        "status": "succeeded",
        "provider_ref": "https://example.test/pull/1",
        "result": attached,
    }
    verdict = validate_sealed_receipt(receipt)
    assert verdict["ok"] is True
    assert verdict["proven_live"] is False


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
