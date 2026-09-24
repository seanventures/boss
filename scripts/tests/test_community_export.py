from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import subprocess
from pathlib import Path

from scripts.generate_feature_docs import parse_traceability

MODULE_PATH = Path(__file__).resolve().parents[1] / "community_export.py"
spec = importlib.util.spec_from_file_location("community_export", MODULE_PATH)
assert spec and spec.loader
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def test_manifest_rejects_unsafe_and_overlapping_rules() -> None:
    manifest = {
        "schema_version": 1,
        "include": ["apps/"],
        "exclude": ["apps/private.py"],
        "requires_split": ["apps/private.py"],
        "required_runtime": ["apps/main.py"],
        "required_release": ["LICENSE"],
    }
    try:
        export.validate_manifest(manifest)
    except ValueError:
        pass
    else:
        raise AssertionError("overlapping export rules were accepted")
    manifest["requires_split"] = ["../escape.py"]
    try:
        export.validate_manifest(manifest)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe export path was accepted")


def test_local_import_graph_finds_withheld_modules() -> None:
    source = {
        "apps/api/app/main.py",
        "apps/api/app/secret.py",
        "apps/web/app/page.tsx",
        "apps/web/app/paid.tsx",
    }
    selected = {"apps/api/app/main.py", "apps/web/app/page.tsx"}
    py = export.python_edges(
        "apps/api/app/main.py", b"from .secret import paid\n", source, selected
    )
    ts = export.typescript_edges(
        "apps/web/app/page.tsx", b"import Paid from './paid';\n", source, selected
    )
    assert [(edge["kind"], edge["to"]) for edge in py + ts] == [
        ("python-import", "apps/api/app/secret.py"),
        ("typescript-import", "apps/web/app/paid.tsx"),
    ]


def test_internal_ui_audit_media_is_excluded() -> None:
    assert (
        export.matching_rule("apps/web/audits/command-center.png", ["apps/web/audits/"])
        == "apps/web/audits/"
    )


def test_public_issue_forms_are_exact_manifest_paths() -> None:
    manifest = json.loads((MODULE_PATH.parent / "community_export_manifest.json").read_text())
    issue_paths = sorted(
        rule for rule in manifest["include"] if rule.startswith(".github/")
    )
    assert issue_paths == sorted([
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/install.yml",
        ".github/ISSUE_TEMPLATE/feature.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
    ])


def test_community_traceability_is_pinned_and_references_exported_files() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    export.validate_manifest(manifest)
    rewrite = manifest["traceability_export_rewrite"]
    source = export.ROOT / rewrite["source"]
    source_bytes = source.read_bytes()
    assert hashlib.sha256(source_bytes).hexdigest() == rewrite["sha256"]
    assert rewrite["destination"] in manifest["required_runtime"]
    assert not any(marker in source_bytes for marker in export.PRIVATE_MARKERS)
    assert not any(marker in source_bytes for marker in export.PAID_SOURCE_MARKERS)
    assert not re.search(r"\b(?:Cloud|Enterprise|customer|commercial|hosted)\b", source.read_text(), re.I)

    features = parse_traceability(source)
    labels = {feature.label for feature in features}
    assert "MET-001" in labels and "DOC-001" in labels
    assert "NFR-002" in labels
    assert not labels.intersection({"COM-PKG-001", "ACC-001", "ENT-CLUST-001", "MEB-V2-001"})

    sha = export.git(export.ROOT, "rev-parse", "HEAD^{commit}").decode().strip()
    report, selected = export.analyze(export.ROOT, sha, manifest)
    assert report["traceability_export_rewrite_applied"] is True
    assert selected[rewrite["destination"]] == source_bytes
    assert rewrite["source"] not in selected
    assert not any(
        blocker.get("path") == rewrite["destination"]
        or blocker.get("to") == rewrite["destination"]
        for blocker in report["blockers"]
    )
    for feature in features:
        for column in (feature.implementation, feature.tests):
            refs = re.findall(r"`([^`]+)`", column)
            assert refs, feature.label
            assert all(ref in selected for ref in refs), feature.label


def test_audited_shared_markers_are_exact_and_paid_gateway_still_blocks() -> None:
    schemas = b"""from typing import Optional
class BaseModel: pass
class AccountConnectOrgSummary(BaseModel):
    id: str
class EditionStatusResponse(BaseModel):
    account_connect_required: bool
    account_connect_status: str
    account_connect_org: Optional[AccountConnectOrgSummary] = None
class LicenseEntitlementSummary(BaseModel):
    account_connect_org_id: Optional[str] = None
"""
    path = "engine/boss_engine/schemas.py"
    masked, valid = export.reviewed_marker_bytes(path, schemas)
    assert valid
    assert not any(marker in masked for marker in export.PAID_SOURCE_MARKERS)
    changed = schemas.replace(b"    account_connect_status: str", b"    account_connect_status: object")
    assert export.reviewed_marker_bytes(path, changed)[1] is False
    added = schemas + b"class HostedMarketplaceRequest(BaseModel): pass\n"
    masked, valid = export.reviewed_marker_bytes(path, added)
    assert valid
    assert any(marker in masked for marker in export.PAID_SOURCE_MARKERS)

    broker = b"""class Broker:
    def _runtime_flags(self, source_id):
        if source_id == "managed_cloud_model_gateway":
            runtime_supported = bool(
                self.settings.managed_cloud_model_gateway_enabled
                and self.settings.account_connect_url
            )
            return runtime_supported, bool(
                runtime_supported and self.settings.account_connect_token
            )
"""
    broker_path = "engine/boss_engine/services/entitlements.py"
    masked, valid = export.reviewed_marker_bytes(broker_path, broker)
    assert valid
    assert not any(marker in masked for marker in export.PAID_SOURCE_MARKERS)
    assert not export.has_semantic_paid_source(broker_path, broker)
    assert export.has_semantic_paid_source(
        broker_path, broker + b"enterprise_model_gateway\n"
    )

    pack = b"""id: community_edition
license_candidate: Apache-2.0
legal_review_required: true
hosted_account_connect_required: false
"""
    pack_path = "packages/specpack/packs/community_edition.yaml"
    masked, valid = export.reviewed_marker_bytes(pack_path, pack)
    assert valid
    assert not any(marker in masked for marker in export.PAID_SOURCE_MARKERS)
    assert export.reviewed_marker_bytes(
        pack_path, pack.replace(b"Apache-2.0", b"AGPLv3")
    )[1] is False
    assert export.reviewed_marker_bytes(
        pack_path, pack + b"commercial_packaging_policy\n"
    )[1] is False

    providers_path = "packages/specpack/models/providers.yaml"
    providers = b"""source_records:
      - source_id: managed_cloud_model_gateway
        source_kind: managed_cloud_model_gateway
        entitlement_kind: hosted_usage_based_gateway
        auth_method: account_connect
"""
    masked, valid = export.reviewed_marker_bytes(providers_path, providers)
    assert valid
    assert b"account_connect" not in masked
    assert not export.has_semantic_paid_source(providers_path, providers)
    assert export.reviewed_marker_bytes(
        providers_path, providers.replace(b"managed_cloud_model_gateway", b"another_source")
    )[1] is False
    assert export.reviewed_marker_bytes(
        providers_path, providers.replace(b"hosted_usage_based_gateway", b"other_entitlement")
    )[1] is False
    assert export.reviewed_marker_bytes(
        providers_path, providers.replace(b"auth_method: account_connect", b"auth_method: account_connect_v2")
    )[1] is False
    assert export.has_semantic_paid_source(
        providers_path, providers + b"enterprise_model_gateway\n"
    )


def test_community_router_is_selected_after_source_audit() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    assert "apps/api/app/routers/community.py" not in manifest["requires_split"]
    assert export.matching_rule("apps/api/app/routers/community.py", manifest["include"])


def test_exported_markdown_links_must_resolve_inside_candidate() -> None:
    selected = {
        "README.md": b"[Guide](docs/community/guide.md) [Web](https://example.org)\n",
        "docs/community/guide.md": b"[Local](../../README.md) [Missing](../product/spec.md) [Here](#section)\n",
    }
    edges = export.markdown_link_edges(selected)
    assert [(edge["kind"], edge["to"]) for edge in edges] == [
        ("markdown-link-target-missing", "docs/product/spec.md"),
    ]


def test_goldeneye_community_boundary_is_preserved() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    export.validate_manifest(manifest)
    for path in (
        "engine/boss_engine/services/goldeneye_paas.py",
        "packages/goldeneye/install.sh",
        "packages/goldeneye/core/bin/ge",
    ):
        assert export.matching_rule(path, manifest["include"])
        assert not export.matching_rule(path, manifest["exclude"])
        assert path in manifest["required_runtime"]
    assert b"GoldeneyePaas" not in export.PAID_SOURCE_MARKERS
    assert b"goldeneye_paas" not in export.PAID_SOURCE_MARKERS
    assert manifest["safe_symlinks"] == {
        "packages/goldeneye/bin": "core/bin",
        "packages/goldeneye/lib": "core/lib",
        "packages/goldeneye/packages": "modules",
    }
    dockerfile = (export.ROOT / "Dockerfile.community").read_bytes()
    assert b"COPY packages/goldeneye ./packages/goldeneye" in dockerfile


def test_community_actions_do_not_depend_on_paid_server_actions() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    shared = export.ROOT / "apps/web/app/actions.ts"
    paid = export.ROOT / "apps/web/app/actions-paid.ts"
    assert shared.is_file() and paid.is_file()
    assert not export.matching_rule("apps/web/app/actions.ts", manifest["requires_split"])
    assert export.matching_rule("apps/web/app/actions-paid.ts", manifest["exclude"])
    assert not any(marker in shared.read_bytes() for marker in export.PAID_SOURCE_MARKERS)
    assert b"submitCommandAction" in shared.read_bytes()
    assert b"upsertEnterpriseIdentityProviderAction" in shared.read_bytes()
    assert b"linkAccountConnectAction" in paid.read_bytes()


def test_community_edition_navigation_does_not_import_paid_chrome() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    for path in ("apps/web/lib/edition.ts", "apps/web/app/edition-ui.tsx"):
        assert not any(
            marker in (export.ROOT / path).read_bytes() for marker in export.PAID_SOURCE_MARKERS
        )
    for path in ("apps/web/lib/edition-paid.ts", "apps/web/app/edition-ui-paid.tsx"):
        assert export.matching_rule(path, manifest["exclude"])
    assert b"org_chart" in (export.ROOT / "apps/web/lib/edition.ts").read_bytes()


def test_local_agent_modules_do_not_fetch_hosted_marketplace() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    local = (export.ROOT / "apps/web/lib/agent-modules.ts").read_bytes()
    paid = (export.ROOT / "apps/web/lib/agent-modules-paid.ts").read_bytes()
    assert not any(marker in local for marker in export.PAID_SOURCE_MARKERS)
    assert b"listAgentModules" in local and b"listAgentModuleInstallRecords" in local
    assert b"listHostedMarketplaceListings" not in local
    assert b"listHostedMarketplaceListings" in paid
    assert export.matching_rule("apps/web/lib/agent-modules-paid.ts", manifest["exclude"])


def test_mixed_web_components_keep_community_forms_in_candidate() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    for path in (
        "apps/web/app/client-forms.tsx",
        "apps/web/app/phase2-ui.tsx",
        "apps/web/app/phase4-ui.tsx",
    ):
        source = (export.ROOT / path).read_bytes()
        assert b'"./actions-paid"' not in source
        assert b"'./actions-paid'" not in source
    for path in (
        "apps/web/app/client-forms-paid.tsx",
        "apps/web/app/phase2-paid-ui.tsx",
        "apps/web/app/phase4-paid-ui.tsx",
    ):
        assert export.matching_rule(path, manifest["exclude"])


def test_fixed_commit_candidate_withholds_private_and_reports_edges(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    files = {
        "apps/api/app/main.py": b"from boss_engine.services.paid import Paid\n",
        "engine/boss_engine/services/paid.py": b"class Paid: pass\n",
        "install.sh": b"BOSS_REPO_URL=https://github.com/seanheiney/boss.git\n",
        "README.md": b"CloudProduct is available only in the hosted edition.\n",
        "docker-compose.yaml": b"services:\n  account-connect:\n    image: private\n",
        "scripts/install/community-compose.yml": b"services:\n  api:\n    build:\n      dockerfile: Dockerfile\n",
        "scripts/install/launch.sh": b"#!/bin/sh\nexit 0\n",
        "Dockerfile.community": b"FROM scratch\nCOPY EXPORT_CANDIDATE_STATUS.json /status.json\nCOPY apps/api/app/main.py /main.py\n",
        "packages/goldeneye/core/bin/ge": b"#!/bin/sh\nexit 0\n",
    }
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (repo / "packages/goldeneye/bin").symlink_to("core/bin")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--chmod=+x", "scripts/install/launch.sh"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    (repo / "untracked-secret.txt").write_text("never export me")
    manifest = {
        "schema_version": 1,
        "include": [
            "apps/api/",
            "engine/boss_engine/",
            "install.sh",
            "docker-compose.yaml",
            "README.md",
            "scripts/install/",
            "Dockerfile.community",
            "packages/goldeneye/",
        ],
        "exclude": ["engine/boss_engine/services/paid.py"],
        "requires_split": ["docker-compose.yaml"],
        "required_runtime": [
            "apps/api/app/main.py",
            "install.sh",
            "docker-compose.yaml",
            "Dockerfile.community",
        ],
        "required_release": ["README.md"],
        "safe_symlinks": {"packages/goldeneye/bin": "core/bin"},
        "compose_rewrite": {
            "path": "scripts/install/community-compose.yml",
            "from": "dockerfile: Dockerfile",
            "to": "dockerfile: Dockerfile.community",
            "expected_count": 1,
        },
    }
    export.validate_manifest(manifest)
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    report, candidate = export.analyze(repo, sha, manifest)
    kinds = {item["kind"] for item in report["blockers"]}
    assert "python-import" in kinds
    assert "private-source-or-host-reference" in kinds
    assert "mixed-paid-source-needs-split" in kinds
    assert "compose-dockerfile-missing" not in kinds
    assert report["compose_rewrite_applied"]
    assert "missing-runtime-entrypoint" in kinds
    assert set(candidate) == {
        "apps/api/app/main.py",
        "scripts/install/community-compose.yml",
        "scripts/install/launch.sh",
        "Dockerfile.community",
        "packages/goldeneye/core/bin/ge",
        "packages/goldeneye/bin",
    }
    assert b"dockerfile: Dockerfile.community" in candidate["scripts/install/community-compose.yml"]
    manifest["compose_rewrite"]["expected_count"] = 2
    mismatch_report, mismatch_candidate = export.analyze(repo, sha, manifest)
    assert any(b["kind"] == "compose-rewrite-count-mismatch" for b in mismatch_report["blockers"])
    assert (
        b"dockerfile: Dockerfile\n" in mismatch_candidate["scripts/install/community-compose.yml"]
    )
    candidate_dir = tmp_path / "candidate"
    export.write_candidate(
        candidate_dir, candidate, report["executable_paths"], report["symlink_paths"]
    )
    assert (candidate_dir / "packages/goldeneye/bin").is_symlink()
    export.finalize_candidate(candidate_dir, report)
    assert (candidate_dir / "scripts/install/launch.sh").stat().st_mode & 0o111
    assert not json.loads((candidate_dir / "EXPORT_CANDIDATE_STATUS.json").read_text())[
        "mechanically_complete"
    ]
    assert report["ready_for_publication"] is False


def test_mechanically_complete_tree_has_no_blocked_marker(tmp_path: Path) -> None:
    output = tmp_path / "candidate"
    export.write_candidate(output, {}, [])
    export.finalize_candidate(
        output,
        {"source_commit": "a" * 40, "mechanically_complete": True, "blocker_count": 0},
    )
    assert not (output / "EXPORT_CANDIDATE_BLOCKED.txt").exists()
    status = json.loads((output / "EXPORT_CANDIDATE_STATUS.json").read_text())
    assert status["mechanically_complete"] is True
    assert status["ready_for_publication"] is False


def test_public_api_client_export_rewrite_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    source = repo / "packages/api-client/src/community.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const community = true;\n")
    package = repo / "packages/api-client/package.json"
    package.write_text('{"name":"@boss/api-client","exports":{".":"./src/index.ts"}}\n')
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["packages/api-client/"],
        "exclude": ["packages/api-client/src/paid.ts"],
        "requires_split": ["packages/api-client/src/private.ts"],
        "required_runtime": ["packages/api-client/src/community.ts"],
        "required_release": ["packages/api-client/package.json"],
        "package_export_rewrite": {
            "path": "packages/api-client/package.json",
            "community_source": "packages/api-client/src/community.ts",
            "from": "./src/index.ts",
            "to": "./src/community.ts",
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["package_export_rewrite_applied"] is True
    assert json.loads(candidate["packages/api-client/package.json"])["exports"]["."] == (
        "./src/community.ts"
    )
    changed = json.loads(json.dumps(manifest))
    changed["package_export_rewrite"]["from"] = "./src/changed.ts"
    report, _ = export.analyze(repo, sha, changed)
    assert {b["kind"] for b in report["blockers"]} == {"package-export-shape-changed"}
    missing = json.loads(json.dumps(manifest))
    missing["package_export_rewrite"]["community_source"] = "packages/api-client/src/missing.ts"
    missing["package_export_rewrite"]["to"] = "./src/missing.ts"
    report, _ = export.analyze(repo, sha, missing)
    assert "package-export-community-source-missing" in {b["kind"] for b in report["blockers"]}


def test_community_page_rewrite_is_checksum_pinned(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    app = repo / "apps/web/app"
    app.mkdir(parents=True)
    (app / "page.tsx").write_text("export const AccountConnect = true;\n")
    community = b"export default function Page() { return null; }\n"
    (app / "page-community.tsx").write_bytes(community)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["apps/web/app/"],
        "exclude": ["apps/web/app/unused.tsx"],
        "requires_split": ["apps/web/app/page.tsx"],
        "required_runtime": ["apps/web/app/page.tsx"],
        "required_release": ["apps/web/app/page.tsx"],
        "page_export_rewrite": {
            "source": "apps/web/app/page-community.tsx",
            "destination": "apps/web/app/page.tsx",
            "sha256": hashlib.sha256(community).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["page_export_rewrite_applied"] is True
    assert candidate == {"apps/web/app/page.tsx": community}
    assert not any(b["kind"] in {"requires-community-split", "missing-runtime-entrypoint"} for b in report["blockers"])

    changed = json.loads(json.dumps(manifest))
    changed["page_export_rewrite"]["sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert report["page_export_rewrite_applied"] is False
    assert "page-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "apps/web/app/page.tsx" not in candidate

    missing = json.loads(json.dumps(manifest))
    missing["page_export_rewrite"]["source"] = "apps/web/app/missing.tsx"
    report, _ = export.analyze(repo, sha, missing)
    assert "page-export-community-source-withheld" in {b["kind"] for b in report["blockers"]}


def test_community_readme_rewrite_is_checksum_pinned(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("Private source: seanheiney/boss\n")
    public = b"# BOSS Community\n\n[Guide](docs/community/guide.md)\n"
    (repo / "README.community.md").write_bytes(public)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["README.md", "README.community.md"],
        "exclude": ["unused.md"],
        "requires_split": ["unused-split.md"],
        "required_runtime": ["README.md"],
        "required_release": ["README.md"],
        "readme_export_rewrite": {
            "source": "README.community.md",
            "destination": "README.md",
            "sha256": hashlib.sha256(public).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["readme_export_rewrite_applied"] is True
    assert candidate == {"README.md": public}
    assert not any(b.get("path") == "README.md" for b in report["blockers"])

    changed = json.loads(json.dumps(manifest))
    changed["readme_export_rewrite"]["sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert report["readme_export_rewrite_applied"] is False
    assert "readme-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "README.md" not in candidate

    missing = json.loads(json.dumps(manifest))
    missing["readme_export_rewrite"]["source"] = "missing.md"
    missing["include"].append("missing.md")
    report, _ = export.analyze(repo, sha, missing)
    assert "readme-export-community-source-withheld" in {b["kind"] for b in report["blockers"]}


def test_community_contributing_rewrite_is_checksum_pinned(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "CONTRIBUTING.md").write_text("Private source: seanheiney/boss\n")
    public = b"# Contributing to BOSS Community\n"
    (repo / "CONTRIBUTING.community.md").write_bytes(public)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["CONTRIBUTING.md", "CONTRIBUTING.community.md"],
        "exclude": ["unused.md"],
        "requires_split": ["unused-split.md"],
        "required_runtime": ["CONTRIBUTING.md"],
        "required_release": ["CONTRIBUTING.md"],
        "contributing_export_rewrite": {
            "source": "CONTRIBUTING.community.md",
            "destination": "CONTRIBUTING.md",
            "sha256": hashlib.sha256(public).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["contributing_export_rewrite_applied"] is True
    assert candidate == {"CONTRIBUTING.md": public}

    changed = json.loads(json.dumps(manifest))
    changed["contributing_export_rewrite"]["sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert report["contributing_export_rewrite_applied"] is False
    assert "contributing-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "CONTRIBUTING.md" not in candidate


def test_community_makefile_rewrite_is_checksum_pinned(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "Makefile").write_text("private:\n\t@echo private\n")
    public = b"help:\n\t@echo BOSS Community\n"
    (repo / "Makefile.community").write_bytes(public)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["Makefile", "Makefile.community"],
        "exclude": ["unused.md"],
        "requires_split": ["Makefile"],
        "required_runtime": ["Makefile"],
        "required_release": ["Makefile"],
        "makefile_export_rewrite": {
            "source": "Makefile.community",
            "destination": "Makefile",
            "sha256": hashlib.sha256(public).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["makefile_export_rewrite_applied"] is True
    assert candidate == {"Makefile": public}
    assert not any(b.get("path") == "Makefile" for b in report["blockers"])

    changed = json.loads(json.dumps(manifest))
    changed["makefile_export_rewrite"]["sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert report["makefile_export_rewrite_applied"] is False
    assert "makefile-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "Makefile" not in candidate
