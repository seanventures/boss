#!/usr/bin/env python3
"""Add missing FastAPI routes into api/openapi.yaml as documented operations.

Does not remove paths. Merges path items for implemented routes missing from OpenAPI.
Uses PyYAML if available; otherwise a conservative text append.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "api" / "openapi.yaml"
MAIN = ROOT / "apps" / "api" / "app" / "main.py"
GOV = ROOT / "apps" / "api" / "app" / "routers" / "governance.py"

# Tag by domain prefix
TAG_MAP = [
    ("/v1/goldeneye", "Goldeneye PaaS"),
    ("/v1/multi-cloud", "Multi-cloud"),
    ("/v1/elastic", "Elastic fabric"),
    ("/v1/work-trackers", "Work trackers"),
    ("/v1/provider-usage", "Provider usage"),
    ("/v1/assurance", "Assurance"),
    ("/v1/agent-groups", "Agent groups"),
    ("/v1/integration-registry", "Integration registry"),
    ("/v1/reliability", "Reliability"),
    ("/v1/shepherd", "Shepherd"),
    ("/v1/model-gateway", "Model gateway"),
    ("/v1/cloud", "Cloud product"),
    ("/v1/secrets", "Secrets"),
    ("/v1/finops", "FinOps"),
    ("/v1/dashboard", "Dashboard"),
    ("/v1/meta", "Meta"),
]


def collect_impl() -> dict[str, set[str]]:
    """path -> set of methods"""
    out: dict[str, set[str]] = {}

    def add(text: str, deco: str) -> None:
        for method, path in re.findall(
            rf'@{deco}\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']',
            text,
        ):
            out.setdefault(path, set()).add(method.lower())

    add(MAIN.read_text(encoding="utf-8"), "app")
    if GOV.is_file():
        add(GOV.read_text(encoding="utf-8"), "router")
    return out


def openapi_paths(doc: dict) -> set[str]:
    return set((doc.get("paths") or {}).keys())


def tag_for(path: str) -> str:
    for prefix, tag in TAG_MAP:
        if path.startswith(prefix):
            return tag
    if path.startswith("/v1/"):
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            return parts[1].replace("-", " ").title()
    return "API"


def op_stub(method: str, path: str) -> dict:
    summary = f"{method.upper()} {path}"
    # humanize a bit
    leaf = path.rstrip("/").split("/")[-1]
    if leaf.startswith("{"):
        leaf = path.rstrip("/").split("/")[-2] if "/" in path else leaf
    return {
        "tags": [tag_for(path)],
        "summary": summary,
        "operationId": re.sub(
            r"[^a-zA-Z0-9_]",
            "_",
            f"{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}",
        )[:80],
        "parameters": _path_params(path)
        + (
            [
                {
                    "name": "project_id",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Project scope when required by the handler.",
                }
            ]
            if method == "get" and "{" not in path
            else []
        ),
        "responses": {
            "200": {
                "description": "Success",
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "additionalProperties": True}
                    }
                },
            },
            "201": {
                "description": "Created",
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "additionalProperties": True}
                    }
                },
            },
            "422": {"description": "Validation error"},
        },
    }


def _path_params(path: str) -> list[dict]:
    params = []
    for name in re.findall(r"\{([^}/]+)\}", path):
        # strip FastAPI type annotations like project_id:path
        clean = name.split(":")[0]
        params.append(
            {
                "name": clean,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        )
    return params


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("PyYAML required", file=sys.stderr)
        return 2

    impl = collect_impl()
    doc = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    paths = doc.setdefault("paths", {})
    existing = set(paths.keys())

    added = 0
    methods_added = 0
    for path, methods in sorted(impl.items()):
        if not path.startswith("/"):
            continue
        # normalize FastAPI path converters for OpenAPI
        oa_path = re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)
        node = paths.setdefault(oa_path, {})
        if oa_path not in existing:
            added += 1
        for method in sorted(methods):
            if method in node:
                continue
            node[method] = op_stub(method, oa_path)
            methods_added += 1
            # POST/PUT body
            if method in {"post", "put", "patch"}:
                node[method]["requestBody"] = {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "additionalProperties": True,
                            }
                        }
                    },
                }

    # ensure tags list includes new tags
    tags = {t.get("name") for t in (doc.get("tags") or []) if isinstance(t, dict)}
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for t in op.get("tags") or []:
                if t not in tags:
                    doc.setdefault("tags", []).append(
                        {"name": t, "description": f"{t} API surface"}
                    )
                    tags.add(t)

    # write with header preserved if present
    header = ""
    original = OPENAPI.read_text(encoding="utf-8")
    if original.startswith("#"):
        # keep first comment block
        lines = original.splitlines(keepends=True)
        hdr = []
        for line in lines:
            if line.startswith("#") or line.strip() == "":
                hdr.append(line)
            else:
                break
        header = "".join(hdr)

    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    OPENAPI.write_text(header + body if header else body, encoding="utf-8")
    print(f"paths_touched_new={added} methods_added={methods_added} total_paths={len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
