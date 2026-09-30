from __future__ import annotations

from pathlib import Path

import yaml

from apps.api.app.services.workflows import validate_workflow_spec


SPEC_ROOT = Path(__file__).resolve().parents[3] / "packages" / "specpack"
WORKFLOW_ROOT = SPEC_ROOT / "workflows"

WORKFLOW_GROUPS = {
    "ground_truth": [
        "ground_truth_metric_evaluation",
        "ground_truth_baseline_change",
        "regression_signal_lifecycle",
        "closed_question_publication_guard",
        "settled_conclusion_reopen",
        "claim_authority_gate",
        "red_to_green_verification",
        "causal_isolation_verification",
        "surface_ownership_acquire",
        "surface_ownership_transfer",
        "ownership_effect_preflight",
        "verification_schedule",
        "adversarial_verification_execute",
        "assurance_case_evaluate",
    ],
    "agent_groups": [
        "agent_group_panel",
        "agent_group_producer_reviewer",
        "agent_group_bounded_debate",
        "agent_group_fan_out_join",
    ],
    "integrations": [
        "integration_connection_lifecycle",
        "capability_resolution_gate",
    ],
}

REQUIRED_RULES = {
    "ground_truth_metric_evaluation": "missing_query_or_evidence_is_not_healthy",
    "ground_truth_baseline_change": "degraded_observation_cannot_change_baseline",
    "regression_signal_lifecycle": "no_silent_incident_drop",
    "closed_question_publication_guard": "direct_recommendation_match_blocks",
    "settled_conclusion_reopen": "new_evidence_and_reason_required",
    "claim_authority_gate": "missing_stale_or_conflicting_evidence_fails_closed",
    "red_to_green_verification": "pre_fix_must_fail_on_real_condition",
    "causal_isolation_verification": "exactly_one_claimed_variable_changes",
    "surface_ownership_acquire": "exactly_one_current_owner",
    "surface_ownership_transfer": "no_interval_with_two_current_owners",
    "ownership_effect_preflight": "denied_preflight_never_invokes_external_effect",
    "verification_schedule": "spend_requires_budget_reservation",
    "adversarial_verification_execute": "inconclusive_is_never_coerced_to_passed",
    "assurance_case_evaluate": "policy_conflict_or_missing_evidence_fails_closed",
    "agent_group_panel": "consensus_never_bypasses_evidence_policy_budget_or_approval",
    "agent_group_producer_reviewer": "unresolved_work_cannot_self_approve",
    "agent_group_bounded_debate": "material_dissent_is_never_erased_or_coerced",
    "agent_group_fan_out_join": "partial_failures_are_preserved_and_visible",
    "integration_connection_lifecycle": "agents_never_receive_raw_secrets",
    "capability_resolution_gate": "effective_capability_is_intersection_never_union",
}


def _load_workflow(name: str) -> dict:
    payload = yaml.safe_load(
        (WORKFLOW_ROOT / f"{name}.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _all_names() -> list[str]:
    return [name for group in WORKFLOW_GROUPS.values() for name in group]


def test_manifest_catalogs_all_twenty_assurance_workflows():
    manifest = yaml.safe_load(
        (SPEC_ROOT / "manifest.yaml").read_text(encoding="utf-8")
    )

    assert {
        group: manifest["workflows"][group] for group in WORKFLOW_GROUPS
    } == WORKFLOW_GROUPS
    assert len(_all_names()) == 20
    assert len(set(_all_names())) == 20


def test_assurance_workflows_are_native_runtime_valid_and_structurally_complete():
    for name in _all_names():
        payload = _load_workflow(name)
        normalized = validate_workflow_spec(payload)

        assert payload["kind"] == "WorkflowSpec"
        assert payload["id"] == name
        assert payload["version"] == "0.1.0"
        assert payload["name"].strip()
        assert payload["description"].strip()
        assert payload["input_schema"]["required"]
        assert "project_id" in payload["input_schema"]["required"]
        assert "dedupe_key" in payload["input_schema"]["required"]
        assert payload["output_schema"]["required"]
        assert payload["completion"]["artifacts_required"]
        assert payload["rules"][REQUIRED_RULES[name]] is True
        assert normalized["entry_step"] == payload["entry_step"]

        # Shipped definitions are inert until an administrator creates an
        # explicit work-class mapping; the spec itself cannot self-select.
        assert "mapping" not in payload
        assert "selector" not in payload
        assert "default_mapping" not in payload


def test_approval_and_blocking_paths_default_to_hold():
    decision_workflows = {
        "ground_truth_baseline_change",
        "settled_conclusion_reopen",
        "claim_authority_gate",
        "surface_ownership_transfer",
        "ownership_effect_preflight",
        "assurance_case_evaluate",
        "agent_group_panel",
        "agent_group_producer_reviewer",
        "agent_group_bounded_debate",
        "agent_group_fan_out_join",
        "integration_connection_lifecycle",
    }
    for name in decision_workflows:
        decisions = [
            step
            for step in _load_workflow(name)["steps"]
            if step["kind"] == "decision"
        ]
        assert decisions, f"{name} must expose its blocking path as a Decision"
        assert all(
            step.get("input", {}).get("default_action") == "hold"
            for step in decisions
        )


def test_agent_group_protocols_preserve_governance_and_effect_evidence():
    for name in WORKFLOW_GROUPS["agent_groups"]:
        payload = _load_workflow(name)
        rules = payload["rules"]

        assert rules[
            "consensus_never_bypasses_evidence_policy_budget_or_approval"
        ] is True
        assert rules["every_member_effect_has_receipt_and_dedupe_key"] is True
        assert "Receipt" in payload["completion"]["artifacts_required"]
        assert any(
            step["kind"] in {"parallel", "bounded_loop"}
            for step in payload["steps"]
        )


def test_integration_workflows_fail_closed_without_raw_credentials():
    lifecycle = _load_workflow("integration_connection_lifecycle")
    capability = _load_workflow("capability_resolution_gate")

    input_fields = set(lifecycle["input_schema"]["required"])
    assert "secret_refs" in input_fields
    assert {"secret", "api_key", "password", "token", "credential"}.isdisjoint(
        input_fields
    )
    assert lifecycle["rules"]["agents_never_receive_raw_secrets"] is True
    assert capability["rules"][
        "missing_stale_unhealthy_or_conflicting_authority_fails_closed"
    ] is True
    assert capability["rules"]["denied_resolution_never_invokes_executor"] is True
