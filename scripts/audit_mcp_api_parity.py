#!/usr/bin/env python3
"""Account for every mounted Community and paid API operation in MCP.

The inventory comes from each edition's live FastAPI composition in a separate
process.  Method and path, rather than FastAPI's sometimes duplicated operation
ID, are the canonical keys.  --check fails if the committed matrix is stale.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/community/mcp-api-capability-matrix.json"


def mounted_rows(edition: str) -> list[dict]:
    os.environ["BOSS_EDITION"] = edition
    sys.path[:0] = [str(ROOT / "apps/api"), str(ROOT / "engine")]
    from app.main import app
    from boss_engine.mcp.api_surface import ApiSurface

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        surface = ApiSurface(app.openapi(), edition=edition)
    rows = []
    for op in surface.operations.values():
        schema_quality = surface.input_schema_quality(op)
        if op.excluded_reason:
            status = "excluded"
        elif op.operator_confirmation:
            status = "operator_only_unavailable"
        else:
            status = "api_policy_gated"
        rows.append({
            "method": op.method,
            "path": op.path,
            "alias": op.alias,
            "domain": op.domain,
            "summary": op.summary,
            "mcp_status": status,
            "reason": op.excluded_reason or (
                "trusted_operator_confirmation_unavailable" if op.operator_confirmation else None
            ),
            "input_schema_quality": schema_quality,
            "typed_input_schema": status != "excluded" and schema_quality != "loose_json_body",
            "domain_scoped_direct_tool": status == "api_policy_gated",
            "requires_explicit_bound_api_credential": status != "excluded",
            "read_only_available": status == "api_policy_gated" and not op.write,
        })
    return sorted(rows, key=lambda row: (row["path"], row["method"]))


def _read_edition(edition: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--dump-edition", edition],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def build_matrix() -> dict:
    community = _read_edition("community")
    enterprise = _read_edition("enterprise")
    community_keys = {(row["method"], row["path"]) for row in community}
    enterprise_keys = {(row["method"], row["path"]) for row in enterprise}
    if not community_keys <= enterprise_keys:
        raise ValueError("Community mounted operations missing from Enterprise composition")
    if len(community_keys) != len(community) or len(enterprise_keys) != len(enterprise):
        raise ValueError("Duplicate mounted method/path operation")
    counts: dict[str, int] = {}
    rows = []
    for row in enterprise:
        row = dict(row)
        row["edition"] = "community_and_paid" if (
            row["method"], row["path"]
        ) in community_keys else "paid_only"
        key = f"{row['edition']}:{row['mcp_status']}"
        counts[key] = counts.get(key, 0) + 1
        rows.append(row)
    return {
        "version": 1,
        "source": "mounted FastAPI app.openapi() per edition",
        "canonical_key": "method + path",
        "invocation": "MCP operation alias through explicit principal-bound API key; API enforces permissions, edition, budget, approval, lease, and receipt policy",
        "community_operation_count": len(community),
        "enterprise_operation_count": len(enterprise),
        "counts": dict(sorted(counts.items())),
        "operations": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump-edition", choices=("community", "enterprise"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-community", action="store_true",
                        help="Verify only mounted Community rows against the dual-edition matrix")
    args = parser.parse_args()
    if args.dump_edition:
        print(json.dumps(mounted_rows(args.dump_edition), separators=(",", ":")))
        return 0
    if args.check_community:
        if not args.output.is_file():
            print(f"MCP API capability matrix is missing: {args.output}", file=sys.stderr)
            return 1
        recorded = json.loads(args.output.read_text())
        community_rows = mounted_rows("community")
        recorded_rows = [row for row in recorded.get("operations", [])
                         if row.get("edition") == "community_and_paid"]
        if (len(community_rows) != recorded.get("community_operation_count")
                or len(recorded_rows) != len(community_rows) or any(
            {key: value for key, value in row.items() if key != "edition"} != expected
            for row, expected in zip(recorded_rows, community_rows, strict=True)
        )):
            print("MCP Community API capability matrix is stale", file=sys.stderr)
            return 1
        print(f"MCP API parity: {len(community_rows)} Community operations accounted for")
        return 0
    matrix = build_matrix()
    rendered = json.dumps(matrix, indent=2, sort_keys=False) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text() != rendered:
            print(f"MCP API capability matrix is stale: {args.output}", file=sys.stderr)
            return 1
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(
        f"MCP API parity: {matrix['community_operation_count']} Community, "
        f"{matrix['enterprise_operation_count']} Enterprise operations accounted for"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
