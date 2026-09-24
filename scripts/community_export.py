#!/usr/bin/env python3
"""Build a quarantined, fail-closed BOSS Community source candidate.

This never pushes to GitHub, creates a Git history, changes visibility, or
asserts license/security approval. It reads blobs from one fixed Git commit so
untracked files and local credentials cannot slip into the candidate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).with_name("community_export_manifest.json")
LOCAL_MODULE_ROOTS = {"apps", "api", "boss_engine", "db", "packages"}
TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".json", ".css")
TS_IMPORT_RE = re.compile(
    r"(?:\b(?:import|export)\s+(?:[^;]*?\s+from\s+)?|\b(?:import|require)\s*\()\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
MARKDOWN_LINK_RE = re.compile(r"\]\(([^)]+)\)")
PRIVATE_MARKERS = (
    b"seanheiney/boss",
    b".sean.network",
    b"tail95d980.ts.net",
    b"100.66.255.78",
)
PAID_SOURCE_MARKERS = (
    b"AccountConnect",
    b"CloudProduct",
    b"EnterpriseCluster",
    b"HostedMarketplace",
    b"account_connect",
    b"hosted_marketplace",
    b"enterprise_cluster",
    b"commercial_packaging_policy",
    b"enterprise_edition",
    b"cloud_edition",
)
# This exact block is shared configuration consumed by the private paid
# extensions. It contains no adapter, route, or paid behavior. Keep it in the
# Community settings module so the private edition retains its configuration
# after composition, but never exempt any other paid marker in that file.
SHARED_CONFIG_PAID_FIELDS = b'''    account_connect_url: Optional[str] = Field(
        default=None, alias="BOSS_ACCOUNT_CONNECT_URL"
    )
    account_connect_token: Optional[str] = Field(
        default=None, alias="BOSS_ACCOUNT_CONNECT_TOKEN"
    )
    account_connect_timeout_seconds: int = Field(
        default=10, alias="BOSS_ACCOUNT_CONNECT_TIMEOUT_SECONDS"
    )
'''


def has_paid_source_markers(path: str, data: bytes) -> bool:
    scan_data = data
    if path == "engine/boss_engine/config.py":
        # Exact replacement is intentionally brittle: any new paid field or
        # changed implementation blocks the export until it is reviewed.
        scan_data = data.replace(SHARED_CONFIG_PAID_FIELDS, b"", 1)
    return any(marker in scan_data for marker in PAID_SOURCE_MARKERS)

# These are Community status/configuration contracts. Each exception applies to
# complete, reviewed source lines in one file and is checked before marker scan.
SHARED_MARKER_LINES = {
    "engine/boss_engine/schemas.py": {
        "class AccountConnectOrgSummary(BaseModel):": "AccountConnectOrgSummary",
        "    account_connect_required: bool": "EditionStatusResponse",
        "    account_connect_status: str": "EditionStatusResponse",
        "    account_connect_org: Optional[AccountConnectOrgSummary] = None": "EditionStatusResponse",
        "    account_connect_org_id: Optional[str] = None": "LicenseEntitlementSummary",
    },
    "engine/boss_engine/services/entitlements.py": {
        "                and self.settings.account_connect_url": "_runtime_flags",
        "                runtime_supported and self.settings.account_connect_token": "_runtime_flags",
    },
}
SEMANTIC_PAID_MARKERS = {
    "engine/boss_engine/services/entitlements.py": (b"enterprise_model_gateway",),
    "packages/specpack/models/providers.yaml": (
        b"enterprise_model_gateway",
        b"private_enterprise_gateway_adapter",
    ),
}


def has_semantic_paid_source(path: str, data: bytes) -> bool:
    return any(marker in data for marker in SEMANTIC_PAID_MARKERS.get(path, ()))


def reviewed_marker_bytes(path: str, data: bytes) -> tuple[bytes, bool]:
    """Return source for paid-marker scan, with only audited Community lines masked.

    A missing, duplicated, relocated, or changed line fails the contract. The
    original bytes always enter the candidate when the contract passes.
    """
    rules = SHARED_MARKER_LINES.get(path)
    if path == "packages/specpack/models/providers.yaml":
        try:
            lines = data.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return data, False
        allowed = "        auth_method: account_connect"
        positions = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == allowed]
        if len(positions) != 1:
            return data, False
        expected_owner = [
            "      - source_id: managed_cloud_model_gateway",
            "        source_kind: managed_cloud_model_gateway",
            "        entitlement_kind: hosted_usage_based_gateway",
        ]
        if positions[0] < len(expected_owner) or [
            line.rstrip("\r\n")
            for line in lines[positions[0] - len(expected_owner):positions[0]]
        ] != expected_owner:
            return data, False
        return b"".join(
            b"\n" if line.rstrip("\r\n") == allowed else line.encode()
            for line in lines
        ), True
    if path == "packages/specpack/packs/community_edition.yaml":
        try:
            lines = data.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return data, False
        allowed = "hosted_account_connect_required: false"
        valid = (
            sum(line.rstrip("\r\n") == allowed for line in lines) == 1
            and b"id: community_edition\n" in data
            and b"license_candidate: Apache-2.0\n" in data
            and b"legal_review_required: true\n" in data
            and b"commercial_packaging_policy" not in data
        )
        if not valid:
            return data, False
        return b"".join(
            b"\n" if line.rstrip("\r\n") == allowed else line.encode()
            for line in lines
        ), True
    if rules is None:
        return data, True
    try:
        text = data.decode("utf-8")
        tree = ast.parse(text)
    except (UnicodeDecodeError, SyntaxError):
        return data, False
    lines = text.splitlines(keepends=True)
    for allowed, owner in rules.items():
        positions = [i + 1 for i, line in enumerate(lines) if line.rstrip("\r\n") == allowed]
        if len(positions) != 1:
            return data, False
        nodes = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and node.name == owner
            and node.lineno <= positions[0] <= (node.end_lineno or node.lineno)
        ]
        if len(nodes) != 1:
            return data, False
    allowed_lines = set(rules)
    return b"".join(
        b"\n" if line.rstrip("\r\n") in allowed_lines else line.encode()
        for line in lines
    ), True


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # Git errors can contain a remote URL. Do not print stderr in release evidence.
        raise RuntimeError(f"git {args[0]} failed with exit {result.returncode}")
    return result.stdout


def match_rule(path: str, rule: str) -> bool:
    return path.startswith(rule) if rule.endswith("/") else path == rule


def matching_rule(path: str, rules: list[str]) -> str | None:
    return next((rule for rule in rules if match_rule(path, rule)), None)


def safe_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and not path.startswith("/") and ".." not in parts and "\x00" not in path


def validate_manifest(manifest: dict) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported export manifest schema")
    for key in ("include", "exclude", "requires_split", "required_runtime", "required_release"):
        rules = manifest.get(key)
        if not isinstance(rules, list) or not rules or not all(isinstance(v, str) for v in rules):
            raise ValueError(f"manifest {key} must be a nonempty string list")
        if len(set(rules)) != len(rules) or any(not safe_path(v) for v in rules):
            raise ValueError(f"manifest {key} has duplicate or unsafe paths")
    if set(manifest["requires_split"]) & set(manifest["exclude"]):
        raise ValueError("requires_split and exclude must be disjoint")
    symlinks = manifest.get("safe_symlinks", {})
    if not isinstance(symlinks, dict) or any(
        not isinstance(path, str)
        or not isinstance(target, str)
        or not safe_path(path)
        or not safe_path(target)
        or not matching_rule(path, manifest["include"])
        or matching_rule(path, manifest["exclude"])
        for path, target in symlinks.items()
    ):
        raise ValueError("safe_symlinks must be included relative paths and targets")
    rewrite = manifest.get("compose_rewrite")
    if rewrite is not None:
        if not isinstance(rewrite, dict) or not safe_path(rewrite.get("path", "")):
            raise ValueError("compose_rewrite needs a safe path")
        if not all(isinstance(rewrite.get(key), str) and rewrite[key] for key in ("from", "to")):
            raise ValueError("compose_rewrite needs nonempty from/to strings")
        if type(rewrite.get("expected_count")) is not int or rewrite["expected_count"] < 1:
            raise ValueError("compose_rewrite needs a positive expected_count")
        if not matching_rule(rewrite["path"], manifest["include"]):
            raise ValueError("compose_rewrite path must be included")
    package_rewrite = manifest.get("package_export_rewrite")
    if package_rewrite is not None:
        if not isinstance(package_rewrite, dict) or any(
            not isinstance(package_rewrite.get(key), str) or not safe_path(package_rewrite[key])
            for key in ("path", "community_source")
        ):
            raise ValueError("package_export_rewrite needs safe package and source paths")
        if not all(
            isinstance(package_rewrite.get(key), str) and package_rewrite[key].startswith("./src/")
            for key in ("from", "to")
        ):
            raise ValueError("package_export_rewrite needs src entrypoint paths")
        package_dir = str(PurePosixPath(package_rewrite["path"]).parent)
        target = str(PurePosixPath(package_dir) / package_rewrite["to"].removeprefix("./"))
        if target != package_rewrite["community_source"]:
            raise ValueError("package export target must match Community source")
        if not matching_rule(package_rewrite["path"], manifest["include"]):
            raise ValueError("package_export_rewrite path must be included")
    page_rewrite = manifest.get("page_export_rewrite")
    if page_rewrite is not None:
        if not isinstance(page_rewrite, dict) or any(
            not isinstance(page_rewrite.get(key), str) or not safe_path(page_rewrite[key])
            for key in ("source", "destination")
        ):
            raise ValueError("page_export_rewrite needs safe source and destination paths")
        if not re.fullmatch(r"[0-9a-f]{64}", str(page_rewrite.get("sha256", ""))):
            raise ValueError("page_export_rewrite needs reviewed source sha256")
        if page_rewrite["source"] == page_rewrite["destination"]:
            raise ValueError("page export source and destination must differ")
        if not matching_rule(page_rewrite["source"], manifest["include"]):
            raise ValueError("page export source must be included")
        if page_rewrite["destination"] not in manifest["requires_split"]:
            raise ValueError("page export destination must be withheld for split")
    readme_rewrite = manifest.get("readme_export_rewrite")
    if readme_rewrite is not None:
        if not isinstance(readme_rewrite, dict) or any(
            not isinstance(readme_rewrite.get(key), str) or not safe_path(readme_rewrite[key])
            for key in ("source", "destination")
        ):
            raise ValueError("readme_export_rewrite needs safe source and destination paths")
        if readme_rewrite["destination"] != "README.md":
            raise ValueError("readme export destination must be root README.md")
        if not re.fullmatch(r"[0-9a-f]{64}", str(readme_rewrite.get("sha256", ""))):
            raise ValueError("readme_export_rewrite needs reviewed source sha256")
        if not all(matching_rule(readme_rewrite[key], manifest["include"]) for key in ("source", "destination")):
            raise ValueError("readme export source and destination must be included")
    contributing_rewrite = manifest.get("contributing_export_rewrite")
    if contributing_rewrite is not None:
        if not isinstance(contributing_rewrite, dict) or any(
            not isinstance(contributing_rewrite.get(key), str) or not safe_path(contributing_rewrite[key])
            for key in ("source", "destination")
        ):
            raise ValueError("contributing_export_rewrite needs safe source and destination paths")
        if contributing_rewrite["destination"] != "CONTRIBUTING.md":
            raise ValueError("contributing export destination must be root CONTRIBUTING.md")
        if not re.fullmatch(r"[0-9a-f]{64}", str(contributing_rewrite.get("sha256", ""))):
            raise ValueError("contributing_export_rewrite needs reviewed source sha256")
        if not all(matching_rule(contributing_rewrite[key], manifest["include"]) for key in ("source", "destination")):
            raise ValueError("contributing export source and destination must be included")
    makefile_rewrite = manifest.get("makefile_export_rewrite")
    if makefile_rewrite is not None:
        if not isinstance(makefile_rewrite, dict) or any(
            not isinstance(makefile_rewrite.get(key), str) or not safe_path(makefile_rewrite[key])
            for key in ("source", "destination")
        ):
            raise ValueError("makefile_export_rewrite needs safe source and destination paths")
        if makefile_rewrite["destination"] != "Makefile":
            raise ValueError("makefile export destination must be root Makefile")
        if not re.fullmatch(r"[0-9a-f]{64}", str(makefile_rewrite.get("sha256", ""))):
            raise ValueError("makefile_export_rewrite needs reviewed source sha256")
        if not matching_rule(makefile_rewrite["source"], manifest["include"]):
            raise ValueError("makefile export source must be included")
        if makefile_rewrite["destination"] not in manifest["requires_split"]:
            raise ValueError("makefile export destination must be withheld for split")
    schema_rewrite = manifest.get("schema_export_rewrite")
    if schema_rewrite is not None:
        if not isinstance(schema_rewrite, dict) or (
            schema_rewrite.get("source") != "db/schema.community.sql"
            or schema_rewrite.get("destination") != "db/schema.sql"
        ):
            raise ValueError("schema export rewrite requires fixed Community and runtime paths")
        if not re.fullmatch(r"[0-9a-f]{64}", str(schema_rewrite.get("sha256", ""))):
            raise ValueError("schema export rewrite needs reviewed source sha256")
        if not matching_rule(schema_rewrite["source"], manifest["include"]):
            raise ValueError("schema export source must be included")
        if schema_rewrite["destination"] not in manifest["requires_split"]:
            raise ValueError("schema export destination must be withheld for split")
        if schema_rewrite["destination"] not in manifest["required_runtime"]:
            raise ValueError("schema export destination must be a required runtime file")
    openapi_rewrite = manifest.get("openapi_export_rewrite")
    if openapi_rewrite is not None:
        if not isinstance(openapi_rewrite, dict) or (
            openapi_rewrite.get("source") != "api/openapi.community.yaml"
            or openapi_rewrite.get("destination") != "api/openapi.yaml"
        ):
            raise ValueError("OpenAPI export rewrite requires fixed Community and runtime paths")
        if not re.fullmatch(r"[0-9a-f]{64}", str(openapi_rewrite.get("sha256", ""))):
            raise ValueError("OpenAPI export rewrite needs reviewed source sha256")
        if not matching_rule(openapi_rewrite["source"], manifest["include"]):
            raise ValueError("OpenAPI export source must be included")
        if openapi_rewrite["destination"] not in manifest["requires_split"]:
            raise ValueError("OpenAPI export destination must be withheld for split")
        if openapi_rewrite["destination"] not in manifest["required_runtime"]:
            raise ValueError("OpenAPI export destination must be a required runtime file")


def tracked_blobs(repo: Path, sha: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for raw in git(repo, "ls-tree", "-r", "-z", sha).split(b"\x00"):
        if not raw:
            continue
        metadata, filename = raw.split(b"\t", 1)
        mode, _kind, oid = metadata.decode().split(" ")
        path = filename.decode("utf-8")
        if not safe_path(path):
            raise ValueError("source commit contains an unsafe path")
        entries[path] = (mode, oid)
    return entries


def module_candidates(name: str) -> tuple[str, str]:
    stem = name.replace(".", "/")
    if name.startswith("boss_engine.") or name == "boss_engine":
        stem = "engine/" + stem
    return f"{stem}.py", f"{stem}/__init__.py"


def python_edges(path: str, data: bytes, source_paths: set[str], selected: set[str]) -> list[dict]:
    try:
        tree = ast.parse(data, filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return [{"kind": "python-parse", "from": path}]
    stem = path.removesuffix("/__init__.py").removesuffix(".py")
    if stem.startswith("engine/"):
        stem = stem.removeprefix("engine/")
    module = stem.replace("/", ".")
    package = module.split(".") if path.endswith("/__init__.py") else module.split(".")[:-1]
    edges = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                name = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                name = node.module or ""
            names = [name]
            names.extend(f"{name}.{alias.name}" for alias in node.names if alias.name != "*")
        for name in names:
            if not name or name.split(".", 1)[0] not in LOCAL_MODULE_ROOTS:
                continue
            # An import may name a symbol rather than a module. Only local
            # modules present in the source tree are dependency edges.
            candidate = next((p for p in module_candidates(name) if p in source_paths), None)
            if candidate and candidate not in selected:
                edges.append(
                    {"kind": "python-import", "from": path, "to": candidate, "line": node.lineno}
                )
    return edges


def typescript_edges(
    path: str, data: bytes, source_paths: set[str], selected: set[str]
) -> list[dict]:
    content = data.decode("utf-8", "replace")
    edges = []
    for match in TS_IMPORT_RE.finditer(content):
        target = match.group(1)
        if target.startswith("@boss/api-client"):
            needed = "packages/api-client/package.json"
            if needed not in selected:
                edges.append({"kind": "workspace-import", "from": path, "to": needed})
            continue
        if not target.startswith("."):
            continue
        base = PurePosixPath(path).parent.joinpath(target)
        normalized = str(PurePosixPath(*[part for part in base.parts if part != "."]))
        # PurePosixPath retains ..; normalize without touching the host FS.
        stack: list[str] = []
        for part in PurePosixPath(normalized).parts:
            if part == "..":
                if stack:
                    stack.pop()
            else:
                stack.append(part)
        normalized = "/".join(stack)
        options = [normalized, *(normalized + ext for ext in TS_EXTENSIONS)]
        options.extend(normalized + "/index" + ext for ext in TS_EXTENSIONS)
        found = next((candidate for candidate in options if candidate in source_paths), None)
        if found and found not in selected:
            edges.append(
                {
                    "kind": "typescript-import",
                    "from": path,
                    "to": found,
                    "line": content.count("\n", 0, match.start()) + 1,
                }
            )
    return edges


def packaging_edges(selected: dict[str, bytes]) -> list[dict]:
    """Name Docker and Compose references absent from the actual candidate."""
    edges: list[dict] = []
    paths = set(selected)
    for dockerfile in ("Dockerfile.community", "Dockerfile"):
        if dockerfile not in selected:
            continue
        content = selected[dockerfile].decode("utf-8", "replace")
        for line_number, line in enumerate(content.splitlines(), 1):
            if not line.startswith("COPY "):
                continue
            try:
                arguments = [part for part in shlex.split(line[5:]) if not part.startswith("--")]
            except ValueError:
                continue
            if len(arguments) < 2 or "--from=" in line:
                continue
            for source in arguments[:-1]:
                if source == "EXPORT_CANDIDATE_STATUS.json":
                    continue  # generated after the secret scan
                prefix = source.rstrip("/") + "/"
                if source not in paths and not any(path.startswith(prefix) for path in paths):
                    edges.append(
                        {
                            "kind": "docker-copy-source-missing",
                            "from": dockerfile,
                            "to": source,
                            "line": line_number,
                        }
                    )
    for compose in ("scripts/install/community-compose.yml", "docker-compose.yaml"):
        if compose not in selected:
            continue
        content = selected[compose].decode("utf-8", "replace")
        for line_number, line in enumerate(content.splitlines(), 1):
            if compose == "docker-compose.yaml" and re.match(r"^  account-connect:\s*$", line):
                edges.append(
                    {
                        "kind": "compose-private-service",
                        "from": compose,
                        "to": "apps/account_connect/",
                        "line": line_number,
                    }
                )
            match = re.match(r"^\s+dockerfile:\s*(\S+)\s*$", line)
            if match and match.group(1) not in paths:
                edges.append(
                    {
                        "kind": "compose-dockerfile-missing",
                        "from": compose,
                        "to": match.group(1),
                        "line": line_number,
                    }
                )
    return edges


def markdown_link_edges(selected: dict[str, bytes]) -> list[dict]:
    """Find local document links that would break in the exported root tree."""
    paths = set(selected)
    edges: list[dict] = []
    for source, data in selected.items():
        if not source.endswith(".md"):
            continue
        content = data.decode("utf-8", "replace")
        for match in MARKDOWN_LINK_RE.finditer(content):
            raw = match.group(1).strip().strip("<>")
            target_url = urlsplit(raw)
            if target_url.scheme or target_url.netloc or not target_url.path:
                continue
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(source), unquote(target_url.path))
            )
            if target.startswith(("../", "/")) or target == "..":
                edges.append({"kind": "markdown-link-outside-export", "from": source, "to": raw})
            elif target not in paths and not any(path.startswith(target.rstrip("/") + "/") for path in paths):
                line = content.count("\n", 0, match.start()) + 1
                edges.append({"kind": "markdown-link-target-missing", "from": source, "to": target, "line": line})
    return edges


def analyze(repo: Path, sha: str, manifest: dict) -> tuple[dict, dict[str, bytes]]:
    entries = tracked_blobs(repo, sha)
    source_paths = set(entries)
    included = {p for p in source_paths if matching_rule(p, manifest["include"])}
    selected: dict[str, bytes] = {}
    symlink_paths: dict[str, str] = {}
    blockers: list[dict] = []
    for rule in manifest["include"]:
        if not any(match_rule(p, rule) for p in source_paths):
            blockers.append({"kind": "missing-inclusion", "path": rule})
    for path in sorted(included):
        if matching_rule(path, manifest["exclude"]):
            continue
        if matching_rule(path, manifest["requires_split"]):
            blockers.append({"kind": "requires-community-split", "path": path})
            continue
        mode, oid = entries[path]
        if mode not in {"100644", "100755", "120000"}:
            blockers.append({"kind": "non-regular-file", "path": path})
            continue
        if any(
            (part.startswith(".env") and part != ".env.example")
            or part in {".git", "node_modules", ".next"}
            for part in PurePosixPath(path).parts
        ):
            blockers.append({"kind": "sensitive-or-generated-path", "path": path})
            continue
        data = git(repo, "cat-file", "blob", oid)
        if mode == "120000":
            target = manifest.get("safe_symlinks", {}).get(path)
            if target is None or data != target.encode():
                blockers.append({"kind": "non-regular-file", "path": path})
                continue
            symlink_paths[path] = target
        if len(data) > 5_000_000:
            blockers.append({"kind": "large-file-needs-review", "path": path})
            continue
        schema_rewrite = manifest.get("schema_export_rewrite")
        if schema_rewrite and path == schema_rewrite["source"]:
            if hashlib.sha256(data).hexdigest() == schema_rewrite["sha256"]:
                # The fixed-path, exact-byte schema is audited separately and
                # intentionally retains local Easy Connect account records.
                selected[path] = data
                continue
            blockers.append({"kind": "schema-export-source-changed", "path": path})
            continue
        openapi_rewrite = manifest.get("openapi_export_rewrite")
        if openapi_rewrite and path == openapi_rewrite["source"]:
            if hashlib.sha256(data).hexdigest() == openapi_rewrite["sha256"]:
                selected[path] = data
                continue
            blockers.append({"kind": "openapi-export-source-changed", "path": path})
            continue
        if any(marker in data for marker in PRIVATE_MARKERS):
            blockers.append({"kind": "private-source-or-host-reference", "path": path})
            continue
        if has_semantic_paid_source(path, data):
            blockers.append({"kind": "semantic-paid-source-needs-split", "path": path})
            continue
        marker_data, contract_ok = reviewed_marker_bytes(path, data)
        if not contract_ok:
            blockers.append({"kind": "shared-marker-contract-changed", "path": path})
            continue
        if has_paid_source_markers(path, marker_data):
            blockers.append({"kind": "mixed-paid-source-needs-split", "path": path})
            continue
        selected[path] = data
    for path, target in list(symlink_paths.items()):
        resolved = str(PurePosixPath(path).parent / target)
        prefix = resolved.rstrip("/") + "/"
        if (resolved not in selected or resolved in symlink_paths) and not any(
            p.startswith(prefix) and p not in symlink_paths for p in selected
        ):
            blockers.append({"kind": "symlink-target-missing", "path": path, "to": resolved})
            del selected[path]
            del symlink_paths[path]
    rewrite = manifest.get("compose_rewrite")
    rewrite_applied = False
    if rewrite:
        path = rewrite["path"]
        if path not in selected:
            blockers.append({"kind": "compose-rewrite-source-missing", "path": path})
        else:
            old = rewrite["from"].encode()
            count = selected[path].count(old)
            if count != rewrite["expected_count"]:
                blockers.append(
                    {
                        "kind": "compose-rewrite-count-mismatch",
                        "path": path,
                        "expected": rewrite["expected_count"],
                        "actual": count,
                    }
                )
            else:
                selected[path] = selected[path].replace(old, rewrite["to"].encode())
                rewrite_applied = True
    package_rewrite = manifest.get("package_export_rewrite")
    package_rewrite_applied = False
    if package_rewrite:
        package_path = package_rewrite["path"]
        source_path = package_rewrite["community_source"]
        if source_path not in source_paths:
            blockers.append(
                {"kind": "package-export-community-source-missing", "path": source_path}
            )
        elif source_path not in selected:
            blockers.append(
                {"kind": "package-export-community-source-withheld", "path": source_path}
            )
        elif package_path not in selected:
            blockers.append({"kind": "package-export-manifest-withheld", "path": package_path})
        else:
            try:
                package = json.loads(selected[package_path])
            except (ValueError, UnicodeDecodeError):
                package = None
            exports = package.get("exports") if isinstance(package, dict) else None
            if not isinstance(exports, dict) or exports.get(".") != package_rewrite["from"]:
                blockers.append({"kind": "package-export-shape-changed", "path": package_path})
            else:
                exports["."] = package_rewrite["to"]
                selected[package_path] = (json.dumps(package, indent=2) + "\n").encode()
                package_rewrite_applied = True
    page_rewrite = manifest.get("page_export_rewrite")
    page_rewrite_applied = False
    if page_rewrite:
        source = page_rewrite["source"]
        destination = page_rewrite["destination"]
        if destination not in source_paths:
            blockers.append({"kind": "page-export-private-route-missing", "path": destination})
        elif source not in selected:
            blockers.append({"kind": "page-export-community-source-withheld", "path": source})
        elif destination in selected:
            blockers.append({"kind": "page-export-private-route-selected", "path": destination})
        elif hashlib.sha256(selected[source]).hexdigest() != page_rewrite["sha256"]:
            blockers.append({"kind": "page-export-source-changed", "path": source})
        else:
            selected[destination] = selected.pop(source)
            blockers = [
                blocker for blocker in blockers
                if not (
                    blocker["kind"] == "requires-community-split"
                    and blocker["path"] == destination
                )
            ]
            page_rewrite_applied = True
    readme_rewrite = manifest.get("readme_export_rewrite")
    readme_rewrite_applied = False
    if readme_rewrite:
        source = readme_rewrite["source"]
        destination = readme_rewrite["destination"]
        if destination not in source_paths:
            blockers.append({"kind": "readme-export-private-readme-missing", "path": destination})
        elif source not in selected:
            blockers.append({"kind": "readme-export-community-source-withheld", "path": source})
        elif destination in selected:
            blockers.append({"kind": "readme-export-private-readme-selected", "path": destination})
        elif hashlib.sha256(selected[source]).hexdigest() != readme_rewrite["sha256"]:
            blockers.append({"kind": "readme-export-source-changed", "path": source})
        else:
            selected[destination] = selected.pop(source)
            blockers = [
                blocker for blocker in blockers
                if not (
                    blocker.get("path") == destination
                    and blocker["kind"] in {
                        "private-source-or-host-reference",
                        "mixed-paid-source-needs-split",
                        "requires-community-split",
                    }
                )
            ]
            readme_rewrite_applied = True
    contributing_rewrite = manifest.get("contributing_export_rewrite")
    contributing_rewrite_applied = False
    if contributing_rewrite:
        source = contributing_rewrite["source"]
        destination = contributing_rewrite["destination"]
        if destination not in source_paths:
            blockers.append({"kind": "contributing-export-private-source-missing", "path": destination})
        elif source not in selected:
            blockers.append({"kind": "contributing-export-community-source-withheld", "path": source})
        elif destination in selected:
            blockers.append({"kind": "contributing-export-private-source-selected", "path": destination})
        elif hashlib.sha256(selected[source]).hexdigest() != contributing_rewrite["sha256"]:
            blockers.append({"kind": "contributing-export-source-changed", "path": source})
        else:
            selected[destination] = selected.pop(source)
            blockers = [
                blocker for blocker in blockers
                if not (
                    blocker.get("path") == destination
                    and blocker["kind"] in {
                        "private-source-or-host-reference",
                        "mixed-paid-source-needs-split",
                        "requires-community-split",
                    }
                )
            ]
            contributing_rewrite_applied = True
    makefile_rewrite = manifest.get("makefile_export_rewrite")
    makefile_rewrite_applied = False
    if makefile_rewrite:
        source = makefile_rewrite["source"]
        destination = makefile_rewrite["destination"]
        if destination not in source_paths:
            blockers.append({"kind": "makefile-export-private-source-missing", "path": destination})
        elif source not in selected:
            blockers.append({"kind": "makefile-export-community-source-withheld", "path": source})
        elif destination in selected:
            blockers.append({"kind": "makefile-export-private-source-selected", "path": destination})
        elif hashlib.sha256(selected[source]).hexdigest() != makefile_rewrite["sha256"]:
            blockers.append({"kind": "makefile-export-source-changed", "path": source})
        else:
            selected[destination] = selected.pop(source)
            blockers = [
                blocker for blocker in blockers
                if not (blocker["kind"] == "requires-community-split" and blocker.get("path") == destination)
            ]
            makefile_rewrite_applied = True
    schema_rewrite = manifest.get("schema_export_rewrite")
    schema_rewrite_applied = False
    if schema_rewrite:
        source = schema_rewrite["source"]
        destination = schema_rewrite["destination"]
        if destination not in source_paths:
            blockers.append({"kind": "schema-export-private-source-missing", "path": destination})
        elif source not in selected:
            blockers.append({"kind": "schema-export-community-source-withheld", "path": source})
        elif destination in selected:
            blockers.append({"kind": "schema-export-private-source-selected", "path": destination})
        else:
            selected[destination] = selected.pop(source)
            blockers = [
                blocker for blocker in blockers
                if not (
                    blocker["kind"] == "requires-community-split"
                    and blocker.get("path") == destination
                )
            ]
            schema_rewrite_applied = True
    openapi_rewrite = manifest.get("openapi_export_rewrite")
    openapi_rewrite_applied = False
    if openapi_rewrite:
        source = openapi_rewrite["source"]
        destination = openapi_rewrite["destination"]
        if destination not in source_paths:
            blockers.append({"kind": "openapi-export-private-source-missing", "path": destination})
        elif source not in selected:
            blockers.append({"kind": "openapi-export-community-source-withheld", "path": source})
        elif destination in selected:
            blockers.append({"kind": "openapi-export-private-source-selected", "path": destination})
        elif hashlib.sha256(selected[source]).hexdigest() != openapi_rewrite["sha256"]:
            blockers.append({"kind": "openapi-export-source-changed", "path": source})
        else:
            selected[destination] = selected.pop(source)
            blockers = [
                blocker for blocker in blockers
                if not (blocker["kind"] == "requires-community-split" and blocker.get("path") == destination)
            ]
            openapi_rewrite_applied = True
    selected_paths = set(selected)
    for path in manifest["required_runtime"]:
        if path not in selected_paths:
            blockers.append({"kind": "missing-runtime-entrypoint", "path": path})
    for path in manifest["required_release"]:
        if path not in selected_paths:
            blockers.append({"kind": "missing-release-document", "path": path})
    for path, data in selected.items():
        if path.endswith(".py"):
            blockers.extend(python_edges(path, data, source_paths, selected_paths))
        elif path.endswith((".ts", ".tsx", ".js", ".jsx")):
            blockers.extend(typescript_edges(path, data, source_paths, selected_paths))
    blockers.extend(packaging_edges(selected))
    blockers.extend(markdown_link_edges(selected))
    unique: dict[tuple, dict] = {}
    for blocker in blockers:
        identity = (blocker["kind"], blocker.get("path"), blocker.get("from"), blocker.get("to"))
        unique.setdefault(identity, blocker)
    blockers = list(unique.values())
    report = {
        "schema_version": 1,
        "source_commit": sha,
        "source_file_count": len(source_paths),
        "candidate_file_count": len(selected),
        "compose_rewrite_applied": rewrite_applied,
        "package_export_rewrite_applied": package_rewrite_applied,
        "page_export_rewrite_applied": page_rewrite_applied,
        "readme_export_rewrite_applied": readme_rewrite_applied,
        "contributing_export_rewrite_applied": contributing_rewrite_applied,
        "makefile_export_rewrite_applied": makefile_rewrite_applied,
        "schema_export_rewrite_applied": schema_rewrite_applied,
        "openapi_export_rewrite_applied": openapi_rewrite_applied,
        "executable_paths": sorted(p for p in selected if entries[p][0] == "100755"),
        "symlink_paths": symlink_paths,
        "candidate_is_new_root_tree": True,
        "ready_for_publication": False,
        "mechanically_complete": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "excluded_file_count": len(included - selected_paths),
        "review_required": [
            "BOSS-300 counsel reference and Apache LICENSE/NOTICE metadata",
            "independent secret and dependency/license scans",
            "clean Linux VM API, worker, web, TUI, MCP and installer acceptance",
            "BOSS-293 public-flip checklist and human release signoff",
        ],
    }
    return report, selected


def write_candidate(
    output: Path,
    files: dict[str, bytes],
    executable_paths: list[str],
    symlink_paths: dict[str, str] | None = None,
) -> None:
    if output.exists():
        raise ValueError("output already exists; use a fresh quarantine directory")
    output.mkdir(parents=True)
    symlink_paths = symlink_paths or {}
    for name, data in files.items():
        if name in symlink_paths:
            continue
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if name in executable_paths:
            target.chmod(0o755)
    for name, link_target in symlink_paths.items():
        (output / name).symlink_to(link_target)
    (output / "EXPORT_CANDIDATE_BLOCKED.txt").write_text(
        "Quarantined source candidate only. Do not push or publish. Read the JSON report and BOSS-293 gates.\n"
    )


def finalize_candidate(output: Path, report: dict) -> None:
    status = {
        "schema_version": 1,
        "source_commit": report["source_commit"],
        "mechanically_complete": report["mechanically_complete"],
        "blocker_count": report["blocker_count"],
        "ready_for_publication": False,
    }
    (output / "EXPORT_CANDIDATE_STATUS.json").write_text(json.dumps(status, indent=2) + "\n")
    if status["mechanically_complete"]:
        (output / "EXPORT_CANDIDATE_BLOCKED.txt").unlink()


def containing_git_worktree(path: Path) -> Path | None:
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    result = subprocess.run(
        ["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return Path(result.stdout.decode().strip()).resolve() if result.returncode == 0 else None


def scan_candidate(output: Path) -> tuple[dict, list[dict]]:
    if not shutil.which("gitleaks"):
        return {"status": "unavailable", "finding_count": None}, [{"kind": "gitleaks-unavailable"}]
    with tempfile.TemporaryDirectory(prefix="boss-export-scan-") as temporary:
        report_path = Path(temporary) / "gitleaks.json"
        result = subprocess.run(
            [
                "gitleaks",
                "dir",
                str(output),
                "--no-banner",
                "--redact=100",
                "--exit-code",
                "5",
                "--report-format",
                "json",
                "--report-path",
                str(report_path),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode not in {0, 5}:
            return {"status": "error", "finding_count": None}, [{"kind": "gitleaks-error"}]
        try:
            findings = json.loads(report_path.read_text()) if report_path.exists() else []
        except json.JSONDecodeError:
            return {"status": "error", "finding_count": None}, [{"kind": "gitleaks-invalid-report"}]
    summary = []
    for item in findings:
        try:
            path = str(Path(item.get("File", "")).relative_to(output))
        except ValueError:
            path = "<unexpected-scan-path>"
        summary.append(
            {
                "kind": "gitleaks-finding",
                "path": path,
                "rule": item.get("RuleID"),
                "line": item.get("StartLine"),
            }
        )
    return {"status": "findings" if findings else "clean", "finding_count": len(findings)}, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-ref", default="HEAD", help="fixed local Git commit/ref to inspect"
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--output", type=Path, required=True, help="fresh directory outside the source repository"
    )
    parser.add_argument(
        "--report", type=Path, required=True, help="fresh JSON report path outside the candidate"
    )
    parser.add_argument(
        "--preview", action="store_true", help="write a quarantined candidate even when blocked"
    )
    args = parser.parse_args()
    repo = ROOT.resolve()
    output = args.output.resolve()
    report_path = args.report.resolve()
    if output == repo or repo in output.parents:
        parser.error("output must be outside the source repository")
    if containing_git_worktree(output) or containing_git_worktree(report_path):
        parser.error("output and report must be outside every Git worktree")
    if output == report_path or output in report_path.parents:
        parser.error("report must be outside the candidate")
    if output.exists() or report_path.exists():
        parser.error("output and report paths must be fresh")
    try:
        manifest = json.loads(args.manifest.read_text())
        validate_manifest(manifest)
        sha = git(repo, "rev-parse", f"{args.source_ref}^{{commit}}").decode().strip()
        report, files = analyze(repo, sha, manifest)
        if not report["blockers"] or args.preview:
            write_candidate(output, files, report["executable_paths"], report["symlink_paths"])
            scan, findings = scan_candidate(output)
            report["secret_scan"] = scan
            report["blockers"].extend(findings)
            report["blocker_count"] = len(report["blockers"])
            report["mechanically_complete"] = not report["blockers"]
            finalize_candidate(output, report)
        else:
            report["secret_scan"] = {"status": "not-run", "finding_count": None}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(
            f"Community export: {report['candidate_file_count']} candidate files; "
            f"{report['blocker_count']} blockers; report={report_path}; "
            f"candidate={'quarantined' if output.exists() else 'not written'}"
        )
        return 1 if report["blockers"] else 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Community export failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
