#!/usr/bin/env python3
"""Add missing FastAPI routes into api/openapi.yaml as documented operations.

Does not remove paths. Merges path items for implemented routes missing from
OpenAPI. Default write path is a conservative splice so the existing 40k+ line
document is not reformatted. Use --full-dump to rewrite via PyYAML.

Scans:
  - apps/api/app/main.py
  - apps/api/app/routers/*.py  (honors APIRouter prefix=)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "api" / "openapi.yaml"
MAIN = ROOT / "apps" / "api" / "app" / "main.py"
ROUTERS_DIR = ROOT / "apps" / "api" / "app" / "routers"

DECORATOR_RE = re.compile(
    r"@(?:app|router|community_router|enterprise_router)"
    r"\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']"
)
PREFIX_RE = re.compile(
    r"(?:router|community_router|enterprise_router)\s*=\s*APIRouter\("
    r"(?P<body>.*?)\)",
    re.DOTALL,
)
PREFIX_KWARG_RE = re.compile(r"prefix\s*=\s*[\"']([^\"']+)[\"']")

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


def iter_source_files() -> list[Path]:
    files = [MAIN]
    if ROUTERS_DIR.is_dir():
        files.extend(sorted(p for p in ROUTERS_DIR.glob("*.py") if p.name != "__init__.py"))
    return [path for path in files if path.is_file()]


def _router_prefix(text: str) -> str:
    match = PREFIX_RE.search(text)
    if not match:
        return ""
    kw = PREFIX_KWARG_RE.search(match.group("body") or "")
    return kw.group(1) if kw else ""


def _join_prefix(prefix: str, path: str) -> str:
    if not prefix:
        return path
    if path.startswith("/v1/") or path.startswith("/saml/") or path in {"/health", "/openapi.json"}:
        return path
    return prefix.rstrip("/") + (path if path.startswith("/") else f"/{path}")


def collect_impl() -> dict[str, set[str]]:
    """path -> set of methods"""
    out: dict[str, set[str]] = {}
    for source in iter_source_files():
        text = source.read_text(encoding="utf-8")
        prefix = _router_prefix(text) if source != MAIN else ""
        for method, path in DECORATOR_RE.findall(text):
            full = _join_prefix(prefix, path)
            out.setdefault(full, set()).add(method.lower())
    return out


def normalize_fastapi_path(path: str) -> str:
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)


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


def _attach_request_body(operation: dict, method: str) -> None:
    if method in {"post", "put", "patch"}:
        operation["requestBody"] = {
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


def collect_missing(
    impl: dict[str, set[str]],
    paths: dict,
    prefixes: list[str],
) -> tuple[dict[str, dict], int, int]:
    """Return {oa_path: {method: op}} for missing operations only."""
    added_paths = 0
    methods_added = 0
    missing: dict[str, dict] = {}
    existing = set(paths.keys())
    for path, methods in sorted(impl.items()):
        if not path.startswith("/"):
            continue
        oa_path = normalize_fastapi_path(path)
        if prefixes and not any(oa_path.startswith(prefix) for prefix in prefixes):
            continue
        node = paths.get(oa_path) or {}
        is_new_path = oa_path not in existing
        for method in sorted(methods):
            if method in node:
                continue
            stub = op_stub(method, oa_path)
            _attach_request_body(stub, method)
            missing.setdefault(oa_path, {})[method] = stub
            methods_added += 1
        if is_new_path and oa_path in missing:
            added_paths += 1
    return missing, added_paths, methods_added


def _indent_block(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    lines = []
    for line in text.splitlines():
        lines.append(f"{pad}{line}" if line else "")
    return "\n".join(lines) + ("\n" if text else "")


def splice_new_paths(original: str, new_paths: dict, yaml_mod) -> str:
    """Insert brand-new path items before the top-level components/tags key."""
    if not new_paths:
        return original
    chunk = _indent_block(
        yaml_mod.safe_dump(new_paths, sort_keys=False, allow_unicode=True, width=100)
    )
    for marker in ("\ncomponents:\n", "\ntags:\n"):
        idx = original.find(marker)
        if idx != -1:
            return original[:idx] + "\n" + chunk + original[idx:]
    return original.rstrip() + "\n" + chunk


def splice_new_methods(original: str, existing_path_ops: dict, yaml_mod) -> str:
    """Insert missing methods under an already-documented path."""
    if not existing_path_ops:
        return original
    for oa_path, operations in existing_path_ops.items():
        needle = f"\n  {oa_path}:\n"
        idx = original.find(needle)
        if idx == -1:
            # path key might be quoted
            quoted = f'\n  "{oa_path}":\n'
            idx = original.find(quoted)
            needle_len = len(quoted) if idx != -1 else 0
        else:
            needle_len = len(needle)
        if idx == -1:
            continue
        insert_at = idx + needle_len
        chunk = _indent_block(
            yaml_mod.safe_dump(operations, sort_keys=False, allow_unicode=True, width=100),
            spaces=4,
        )
        original = original[:insert_at] + chunk + original[insert_at:]
    return original


def append_tags(original: str, new_tags: list[str], yaml_mod) -> str:
    if not new_tags:
        return original
    payload = [{"name": tag, "description": f"{tag} API surface"} for tag in new_tags]
    chunk = yaml_mod.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
    if not original.endswith("\n"):
        original += "\n"
    return original + chunk


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge implemented FastAPI routes into api/openapi.yaml stubs."
    )
    parser.add_argument(
        "--prefix",
        action="append",
        default=[],
        help="Only sync routes beginning with this path prefix. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be added without writing api/openapi.yaml.",
    )
    parser.add_argument(
        "--full-dump",
        action="store_true",
        help="Rewrite the entire OpenAPI document with PyYAML (reformats 40k+ lines).",
    )
    return parser.parse_args()


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("PyYAML required", file=sys.stderr)
        return 2

    args = parse_args()
    impl = collect_impl()
    original = OPENAPI.read_text(encoding="utf-8")
    doc = yaml.safe_load(original)
    paths = doc.setdefault("paths", {})
    missing, added, methods_added = collect_missing(impl, paths, args.prefix)

    if args.dry_run:
        for path, operations in missing.items():
            for method in operations:
                print(f"ADD {method.upper()} {path}")
        print(
            f"paths_touched_new={added} methods_added={methods_added} "
            f"total_paths={len(paths) + added}"
        )
        return 0

    if not missing:
        print(f"paths_touched_new=0 methods_added=0 total_paths={len(paths)}")
        return 0

    existing_tags = {
        tag.get("name") for tag in (doc.get("tags") or []) if isinstance(tag, dict)
    }
    new_tags: list[str] = []
    for path, operations in missing.items():
        for operation in operations.values():
            for tag in operation.get("tags") or []:
                if tag not in existing_tags:
                    new_tags.append(tag)
                    existing_tags.add(tag)

    if args.full_dump:
        for path, operations in missing.items():
            node = paths.setdefault(path, {})
            node.update(operations)
        if new_tags:
            tag_list = doc.setdefault("tags", [])
            for tag in new_tags:
                tag_list.append({"name": tag, "description": f"{tag} API surface"})
        header = ""
        if original.startswith("#"):
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
    else:
        new_paths = {path: ops for path, ops in missing.items() if path not in paths}
        existing_path_ops = {path: ops for path, ops in missing.items() if path in paths}
        updated = splice_new_paths(original, new_paths, yaml)
        updated = splice_new_methods(updated, existing_path_ops, yaml)
        updated = append_tags(updated, new_tags, yaml)
        OPENAPI.write_text(updated, encoding="utf-8")

    print(
        f"paths_touched_new={added} methods_added={methods_added} "
        f"total_paths={len(paths) + added}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
