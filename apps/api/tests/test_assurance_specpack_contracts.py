from __future__ import annotations

from pathlib import Path

import yaml


SPEC_ROOT = Path(__file__).resolve().parents[3] / "packages" / "specpack"
OBJECT_ROOT = SPEC_ROOT / "objects"

CONTRACTS = {
    "ground_truth_metric_definition": "GroundTruthMetricDefinition",
    "ground_truth_observation": "GroundTruthObservation",
    "regression_signal": "RegressionSignal",
    "settled_conclusion": "SettledConclusion",
    "closed_question_match": "ClosedQuestionMatch",
    "claim": "Claim",
    "evidence": "Evidence",
    "claim_verification": "ClaimVerification",
    "surface_ownership": "SurfaceOwnership",
    "ownership_transition": "OwnershipTransition",
    "verification_policy": "VerificationPolicy",
    "verification_run": "VerificationRun",
    "assurance_case": "AssuranceCase",
    "assurance_policy_snapshot": "AssurancePolicySnapshot",
    "agent_group_spec": "AgentGroupSpec",
    "agent_group_run": "AgentGroupRun",
    "integration_definition": "IntegrationDefinition",
    "integration_connection": "IntegrationConnection",
    "executor_binding": "ExecutorBinding",
    "capability_resolution": "CapabilityResolution",
}

MANIFEST_GROUPS = {
    "ground_truth": list(CONTRACTS)[:14],
    "agent_groups": list(CONTRACTS)[14:16],
    "integrations": list(CONTRACTS)[16:],
}

REQUIRED_CONTRACT_FIELDS = {
    "ground_truth_metric_definition": {
        "baseline_value",
        "threshold_value",
        "evaluation_cadence_seconds",
        "owner_ref",
        "evidence_query",
        "version",
    },
    "ground_truth_observation": {
        "metric_definition_version",
        "status",
        "evaluator_job_id",
        "lease_owner",
        "heartbeat_at",
        "dedupe_key",
    },
    "regression_signal": {
        "last_good_observation_id",
        "trend",
        "breach_duration_seconds",
        "incident_ref",
        "decision_id",
    },
    "settled_conclusion": {
        "tell_patterns",
        "cause_menu",
        "evidence_refs",
        "reopen_conditions",
        "review_at",
        "version",
    },
    "closed_question_match": {
        "publication_boundary",
        "context_class",
        "outcome",
        "stop_payload",
        "audit_event_ref",
    },
    "claim": {
        "claim_type",
        "state",
        "evidence_contract_ref",
        "prior_baseline_ref",
        "claimed_variable",
    },
    "evidence": {
        "content_hash",
        "provenance",
        "reproduction_instructions",
        "valid_until",
        "invalidated_at",
    },
    "claim_verification": {
        "verification_kind",
        "precondition_evidence_refs",
        "postcondition_evidence_refs",
        "control_evidence_refs",
        "conflicting_evidence_refs",
        "result",
    },
    "surface_ownership": {
        "surface_key",
        "environment",
        "owner_actor_ref",
        "coordination_route",
        "ownership_version",
        "status",
    },
    "ownership_transition": {
        "action",
        "expected_version",
        "resulting_version",
        "approval_decision_id",
        "status",
    },
    "verification_policy": {
        "subject_selectors",
        "claim_types",
        "risk_tiers",
        "sample_rate",
        "verification_method",
        "independence_policy",
        "budget_policy_ref",
    },
    "verification_run": {
        "claim_version",
        "evidence_refs",
        "verifier_independent",
        "lease_expires_at",
        "heartbeat_at",
        "budget_reservation_id",
        "receipt_refs",
        "outcome",
    },
    "assurance_case": {
        "policy_snapshot_ref",
        "observation_refs",
        "claim_refs",
        "evidence_refs",
        "surface_ownership_refs",
        "verification_run_refs",
        "agent_group_run_refs",
        "workflow_run_refs",
        "skill_invocation_refs",
        "integration_connection_refs",
        "graph_version",
    },
    "assurance_policy_snapshot": {
        "scope_precedence",
        "evidence_contract_ref",
        "surface_ownership_rule",
        "verifier_requirement",
        "conflicts",
        "composition_result",
        "snapshot_hash",
    },
    "agent_group_spec": {
        "member_role_specs",
        "topology",
        "collaboration_protocol",
        "allowed_capabilities",
        "context_sharing_policy",
        "independence_requirements",
        "budget_limit_micros",
        "completion_rule",
        "escalation_target",
    },
    "agent_group_run": {
        "member_assignments",
        "work_product_refs",
        "message_refs",
        "dissent",
        "quorum_result",
        "budget_reservation_refs",
        "receipt_refs",
        "lease_expires_at",
        "heartbeat_at",
        "final_disposition",
    },
    "integration_definition": {
        "supported_skill_specs",
        "executor_types",
        "configuration_schema",
        "health_contract",
        "provenance",
        "trusted",
        "definition_hash",
    },
    "integration_connection": {
        "integration_definition_version",
        "secret_refs",
        "granted_capabilities",
        "health_status",
        "health_fresh_until",
        "rate_limits",
        "enabled",
        "revoked_at",
        "version",
    },
    "executor_binding": {
        "skill_spec_version",
        "executor_type",
        "input_schema_hash",
        "output_schema_hash",
        "required_permissions",
        "side_effect_class",
        "receipt_required",
        "dedupe_key_required",
        "changeset_required",
        "resource_receipt_required",
    },
    "capability_resolution": {
        "role_spec_ref",
        "agent_group_spec_ref",
        "workflow_step_ref",
        "requested_capabilities",
        "effective_capabilities",
        "connection_health_snapshot",
        "policy_snapshot",
        "risk_check",
        "data_boundary_check",
        "denial_reasons",
        "resolution_hash",
    },
}

REQUIRED_RULES = {
    "ground_truth_metric_definition": "baseline_change_requires_permission_reason_and_event",
    "ground_truth_observation": "lease_and_heartbeat_required",
    "regression_signal": "breach_creates_actionable_incident_or_decision",
    "settled_conclusion": "reopen_requires_permission_reason_evidence_and_decision",
    "closed_question_match": "direct_recommendation_match_blocks_before_publication",
    "claim": "authoritative_requires_passing_claim_verification",
    "evidence": "expired_or_invalidated_evidence_cannot_satisfy_gate",
    "claim_verification": "isolation_requires_single_changed_variable_and_control",
    "surface_ownership": "exactly_one_active_owner_per_surface_environment",
    "ownership_transition": "transition_is_atomic_compare_and_swap",
    "verification_policy": "critical_policy_cannot_select_producer_as_verifier",
    "verification_run": "model_or_tool_spend_requires_reservation_and_receipts",
    "assurance_case": "conflicting_mandatory_rules_fail_closed_to_decision",
    "assurance_policy_snapshot": "lower_scope_cannot_weaken_evidence_safety_ownership_or_budget",
    "agent_group_spec": "consensus_cannot_bypass_policy_or_evidence_gate",
    "agent_group_run": "quorum_without_proof_cannot_satisfy_assurance",
    "integration_definition": "untrusted_definition_cannot_enable_effect_capabilities",
    "integration_connection": "raw_credentials_never_enter_agent_context",
    "executor_binding": "provider_write_requires_changeset_and_resource_receipt",
    "capability_resolution": "effective_is_intersection_not_union",
}


def _load_contract(name: str) -> dict:
    payload = yaml.safe_load(
        (OBJECT_ROOT / f"{name}.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _field_map(payload: dict) -> dict[str, dict]:
    fields = payload["fields"]
    assert isinstance(fields, list)
    assert all(isinstance(field, dict) for field in fields)
    result = {field["name"]: field for field in fields}
    assert len(result) == len(fields), "contract field names must be unique"
    return result


def test_v2_manifest_catalogs_all_twenty_assurance_contracts():
    manifest = yaml.safe_load(
        (SPEC_ROOT / "manifest.yaml").read_text(encoding="utf-8")
    )

    for group, expected in MANIFEST_GROUPS.items():
        assert manifest["objects"][group] == expected

    cataloged = [
        name
        for group in MANIFEST_GROUPS
        for name in manifest["objects"][group]
    ]
    assert cataloged == list(CONTRACTS)
    assert len(cataloged) == 20


def test_contracts_are_typed_versioned_and_structurally_complete():
    for filename, object_id in CONTRACTS.items():
        payload = _load_contract(filename)
        fields = _field_map(payload)

        assert payload["kind"] == "ObjectSpec"
        assert payload["id"] == object_id
        assert payload["version"] == "0.1.0"
        assert payload["description"].strip()
        assert fields["id"]["required"] is True
        assert all(
            {"name", "type", "required"}.issubset(field)
            for field in fields.values()
        )
        assert REQUIRED_CONTRACT_FIELDS[filename].issubset(fields)
        assert payload["rules"][REQUIRED_RULES[filename]] is True
        assert payload["constraints"]["unique"]


def test_tenant_records_are_scoped_and_deduplicated():
    global_contracts = {
        "integration_definition",
        "executor_binding",
    }
    for filename in set(CONTRACTS) - global_contracts:
        fields = _field_map(_load_contract(filename))
        assert {"org_id", "workspace_id", "project_id"}.issubset(fields)

    for filename in {
        "ground_truth_observation",
        "regression_signal",
        "closed_question_match",
        "claim",
        "claim_verification",
        "ownership_transition",
        "verification_run",
        "assurance_case",
        "agent_group_run",
        "capability_resolution",
    }:
        fields = _field_map(_load_contract(filename))
        assert fields["dedupe_key"]["required"] is True


def test_fail_closed_state_contracts_preserve_inconclusive_and_denied_outcomes():
    expected_enum_values = {
        ("claim", "state"): {"blocked", "inconclusive", "refuted"},
        ("claim_verification", "result"): {"refuted", "inconclusive", "blocked"},
        ("verification_run", "outcome"): {"refuted", "inconclusive"},
        ("assurance_policy_snapshot", "composition_result"): {
            "blocked",
            "decision_required",
        },
        ("capability_resolution", "result"): {"allowed", "denied"},
    }
    for (filename, field_name), expected in expected_enum_values.items():
        field = _field_map(_load_contract(filename))[field_name]
        assert field["type"] == "enum"
        assert expected.issubset(set(field["values"]))


def test_contracts_use_secret_refs_not_raw_credential_fields():
    forbidden = {"secret", "api_key", "password", "token", "credential"}
    for filename in CONTRACTS:
        fields = _field_map(_load_contract(filename))
        assert forbidden.isdisjoint(fields)

    connection_fields = _field_map(_load_contract("integration_connection"))
    assert connection_fields["secret_refs"]["type"] == "array"
    assert (
        _load_contract("integration_connection")["rules"][
            "raw_credentials_never_enter_agent_context"
        ]
        is True
    )
