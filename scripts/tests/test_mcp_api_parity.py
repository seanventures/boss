"""Operation-level MCP accounting against the mounted edition compositions."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/operations/mcp-api-capability-matrix.private.json"
COMMUNITY_MATRIX = ROOT / "docs/community/mcp-api-capability-matrix.json"


def test_mcp_capability_matrix_matches_both_live_openapi_compositions():
    paid_source_available = (ROOT / "apps/api/app/enterprise_extensions").is_dir()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/audit_mcp_api_parity.py"),
         "--check" if paid_source_available else "--check-community"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    community = json.loads(COMMUNITY_MATRIX.read_text())
    assert community["edition"] == "community"
    assert community["operation_count"] == len(community["operations"])
    assert len({(row["method"], row["path"]) for row in community["operations"]}) == len(
        community["operations"]
    )
    if not paid_source_available:
        operations = community["operations"]
    else:
        matrix = json.loads(MATRIX.read_text())
        operations = matrix["operations"]
        assert matrix["community_operation_count"] == community["operation_count"]
        assert matrix["community_operation_count"] == sum(
            row["edition"] == "community_and_paid" for row in operations
        )
        assert matrix["enterprise_operation_count"] == len(operations)
        assert {(row["method"], row["path"]) for row in community["operations"]} == {
            (row["method"], row["path"]) for row in operations
            if row["edition"] == "community_and_paid"
        }
    assert len({(row["method"], row["path"]) for row in operations}) == len(operations)
    assert len({row["alias"] for row in operations}) == len(operations)
    for row in operations:
        if row["mcp_status"] == "excluded":
            assert row["reason"]
            assert not row["domain_scoped_direct_tool"]
        elif row["mcp_status"] == "operator_only_unavailable":
            assert row["reason"] == "trusted_operator_confirmation_unavailable"
            assert not row["domain_scoped_direct_tool"]
        elif row["mcp_status"] == "human_confirmation_gated":
            assert row["reason"] == "fresh_human_confirmation_required"
            assert row["domain_scoped_direct_tool"]
        else:
            assert row["typed_input_schema"] == (
                row["input_schema_quality"] != "loose_json_body"
            )


def test_community_curated_request_fields_are_audited_and_never_paid_only():
    overlay = json.loads((ROOT / "engine/boss_engine/mcp/curated_request_bodies.json").read_text())
    assert overlay["version"] == 1
    assert len(overlay["operations"]) == 14
    community = json.loads(COMMUNITY_MATRIX.read_text())
    rows = {(row["method"], row["path"]): row for row in community["operations"]}
    assert sum(row["mounted_input_schema_quality"] == "loose_json_body"
               for row in rows.values()) <= 84
    assert sum(row["input_schema_quality"] == "loose_json_body"
               for row in rows.values()) <= 66
    for key, body in overlay["operations"].items():
        method, path = key.split(" ", 1)
        row = rows[(method, path)]
        if row["mounted_input_schema_quality"] == "loose_json_body":
            assert row["input_schema_quality"] == "curated_json_body"
            assert row["curated_input_fields"] is True
            assert row["runtime_validated_input_schema"] is False
        else:
            assert row["input_schema_quality"] == "typed_json_body"
            assert row["curated_input_fields"] is False
            assert row["runtime_validated_input_schema"] is True
        assert body["type"] == "object" and body["additionalProperties"] is True
        assert body["properties"]
    assert not {
        "POST /v1/qa/benchmark-apps/{benchmark_id}/runs",
        "POST /v1/previews/{preview_id}/feedback",
    } & overlay["operations"].keys()
    # The private source contains the original curated field maps. The public
    # export replaces this file with the Community contract; its operation
    # inventory check above still protects the published overlay boundary.
    if (ROOT / "api/openapi.community.yaml").exists():
        private = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
        for key, body in overlay["operations"].items():
            method, path = key.split(" ", 1)
            documented = private["paths"][path][method.lower()]["requestBody"]["content"][
                "application/json"
            ]["schema"]
            if rows[(method, path)]["mounted_input_schema_quality"] == "loose_json_body":
                assert documented == body, key
            else:
                assert "$ref" in documented, key


def test_operator_boundary_preserves_human_and_api_policy_gates():
    community = json.loads(COMMUNITY_MATRIX.read_text())
    rows = {(row["method"], row["path"]): row for row in community["operations"]}
    allowed = {
        ("DELETE", "/v1/tasks/{task_id}"),
        ("POST", "/v1/workflow-runs/{run_id}/migrations/rollback"),
    }
    for key in allowed:
        row = rows[key]
        assert row["mcp_status"] == "api_policy_gated"
        assert row["typed_input_schema"] is True
    for key in {
        ("POST", "/v1/members"),
        ("DELETE", "/v1/meeting-roles/{role_key}"),
        ("DELETE", "/v1/console/v2/org/{agent_id}"),
        ("POST", "/v1/improvement-programs/trials/{trial_id}/promote"),
        ("POST", "/v1/decisions/{decision_id}/reject"),
        ("POST", "/v1/decisions/{decision_id}/revise"),
        ("POST", "/v1/decisions/{decision_id}/delegate"),
        ("POST", "/v1/decisions/{decision_id}/execute"),
        ("POST", "/v1/changesets/{changeset_id}/approve"),
        ("POST", "/v1/github/merge-authorizations/{authorization_id}/authorize"),
        ("POST", "/v1/github/resolution-plans/{plan_id}/approve"),
        ("POST", "/v1/decisions/{decision_id}/approve"),
        ("POST", "/v1/decisions/{decision_id}/settle"),
        ("POST", "/v1/previews/{preview_id}/approve"),
        ("POST", "/v1/console/v2/autonomy"),
        ("POST", "/v1/console/v2/autonomy/apply"),
    }:
        assert rows[key]["mcp_status"] == "human_confirmation_gated"
    assert rows[("POST", "/v1/delivery/deploy")]["mcp_status"] == (
        "human_confirmation_gated"
    )
    assert rows[("POST", "/v1/activity/retention/prune")]["mcp_status"] == "excluded"
    assert rows[("POST", "/v1/platform-standards/apply")]["mcp_status"] == (
        "api_policy_gated"
    )
    # Counts of the matrix regenerated 2026-10-22 (it had been stale since well before rc10.9:
    # 992 of 1,622 operations). +7 confirmation-gated are main's agent and role-skill routes.
    # The 68 operator-only are DELETEs and revoke/rotate/promote/reject tails: 65 from main, and
    # from the knowledge graph POST facts/{id}/promote and /reject (a person's step-up) and
    # DELETE /v1/setup/embedding-model, kept out of MCP on purpose. +1 confirmation-gated is
    # POST /v1/projects: held for a non-human caller like POST /v1/orgs, a person confirms it.
    # +1 is POST /v1/decisions/{id}/settle: the same human step-up as approve.
    assert rows[("POST", "/v1/projects")]["mcp_status"] == "human_confirmation_gated"
    assert sum(row["mcp_status"] == "human_confirmation_gated"
               for row in community["operations"]) == 73
    # +3 operator-only: Rules review's approve / edit / reject (BOSSAPP-768), a person's step-up.
    for action in ("approve", "edit", "reject"):
        assert rows[("POST", f"/v1/knowledge/view/rules/{{rule_id}}/{action}")]["mcp_status"] == (
            "operator_only_unavailable")
    # +1 operator-only: DELETE /v1/setup/sign-in/{sign_in_id} (cancel an in-container
    # subscription sign-in, BOSSAPP-769); starting and polling one stay api_policy_gated.
    assert rows[("DELETE", "/v1/setup/sign-in/{sign_in_id}")]["mcp_status"] == (
        "operator_only_unavailable")
    assert sum(row["mcp_status"] == "operator_only_unavailable"
               for row in community["operations"]) == 72
