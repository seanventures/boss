"""Operation-level MCP accounting against the mounted edition compositions."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

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
        else:
            assert row["typed_input_schema"] == (
                row["input_schema_quality"] != "loose_json_body"
            )
