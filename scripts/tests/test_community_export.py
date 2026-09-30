from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import subprocess
import tomllib
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


def test_community_license_is_checksum_pinned(tmp_path: Path) -> None:
    source = (export.ROOT / "LICENSE").read_bytes()
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    export.validate_manifest(manifest)
    assert "LICENSE" in manifest["include"]
    assert "LICENSE" in manifest["required_release"]
    assert "LICENSE" not in manifest["requires_split"]
    assert hashlib.sha256(source).hexdigest() == manifest["reviewed_license_sha256"]

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "LICENSE").write_bytes(source)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "LICENSE"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    focused = {
        "schema_version": 1,
        "include": ["LICENSE"],
        "exclude": ["other-private-file"],
        "requires_split": ["unused-private-file"],
        "required_runtime": ["LICENSE"],
        "required_release": ["LICENSE"],
        "reviewed_license_sha256": manifest["reviewed_license_sha256"],
    }
    export.validate_manifest(focused)
    report, candidate = export.analyze(repo, sha, focused)
    assert report["mechanically_complete"]
    assert candidate["LICENSE"] == source

    focused["reviewed_license_sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, focused)
    assert "reviewed-license-changed" in {blocker["kind"] for blocker in report["blockers"]}
    assert "LICENSE" not in candidate

    del focused["reviewed_license_sha256"]
    try:
        export.validate_manifest(focused)
    except ValueError:
        pass
    else:
        raise AssertionError("unreviewed root license was accepted")


def test_reviewed_third_party_notices_are_included_and_checksum_pinned(tmp_path: Path) -> None:
    root = export.ROOT
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    notice_paths = set(manifest["reviewed_notice_sha256"])
    assert notice_paths == {
        "THIRD-PARTY-NOTICES.md",
        "third_party/notices/caniuse-lite-1.0.30001793-LICENSE",
        "third_party/notices/spdx-exceptions-2.5.0-README.md",
        "third_party/notices/sharp-libvips-1.2.4-LICENSE",
        "third_party/notices/sharp-libvips-1.2.4-THIRD-PARTY-NOTICES.md",
    }
    assert notice_paths.issubset(manifest["include"])
    assert notice_paths.issubset(manifest["required_release"])
    assert all(
        hashlib.sha256((root / path).read_bytes()).hexdigest() == digest
        for path, digest in manifest["reviewed_notice_sha256"].items()
    )
    dockerfile = (root / "Dockerfile.community").read_text()
    assert "COPY THIRD-PARTY-NOTICES.md ./THIRD-PARTY-NOTICES.md" in dockerfile
    assert "COPY third_party/notices ./third_party/notices" in dockerfile
    assert "COPY --chown=boss:boss THIRD-PARTY-NOTICES.md ./THIRD-PARTY-NOTICES.md" in dockerfile
    assert "COPY --chown=boss:boss third_party/notices ./third_party/notices" in dockerfile

    repo = tmp_path / "source"
    fixture_paths = notice_paths | {"LICENSE"}
    for path in fixture_paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / path).read_bytes())
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    focused = {
        "schema_version": 1,
        "include": sorted(fixture_paths),
        "exclude": ["other-private-file"],
        "requires_split": ["unused-private-file"],
        "required_runtime": ["THIRD-PARTY-NOTICES.md"],
        "required_release": sorted(fixture_paths),
        "reviewed_license_sha256": manifest["reviewed_license_sha256"],
        "reviewed_notice_sha256": manifest["reviewed_notice_sha256"].copy(),
    }
    export.validate_manifest(focused)
    report, candidate = export.analyze(repo, sha, focused)
    assert report["mechanically_complete"]
    assert notice_paths.issubset(candidate)

    changed_path = "third_party/notices/spdx-exceptions-2.5.0-README.md"
    focused["reviewed_notice_sha256"][changed_path] = "0" * 64
    report, candidate = export.analyze(repo, sha, focused)
    assert {"kind": "reviewed-notice-changed", "path": changed_path} in report["blockers"]
    assert changed_path not in candidate

    del focused["reviewed_notice_sha256"]
    try:
        export.validate_manifest(focused)
    except ValueError:
        pass
    else:
        raise AssertionError("unreviewed third-party notice was accepted")


def test_exported_package_metadata_has_a_resolvable_apache_license() -> None:
    root = export.ROOT
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    assert "engine/LICENSE" in manifest["include"]
    engine_metadata = tomllib.loads((root / "engine/pyproject.toml").read_text())
    assert engine_metadata["project"]["license"]["file"] == "LICENSE"
    assert (root / "engine/LICENSE").is_file()

    for path in (
        "package.json",
        "apps/web/package.json",
        "packages/api-client/package.json",
        "packages/create-boss/package.json",
    ):
        package = json.loads((root / path).read_text())
        assert package["license"] == "Apache-2.0"
    create_boss = json.loads((root / "packages/create-boss/package.json").read_text())
    assert "LICENSE" in create_boss["files"]
    assert (root / "packages/create-boss/LICENSE").read_bytes() == (root / "LICENSE").read_bytes()


def test_private_infrastructure_markers_are_withheld(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "public.md").write_text("Community guide\n")
    markers = (
        b"seanheiney/goldeneye",
        b"2.29.2.202",
        b"3.94.66.139",
        b"46.225.93.220",
        b"/Volumes/DataSSD1/test/boss",
    )
    for index, marker in enumerate(markers):
        (docs / f"private-{index}.md").write_bytes(b"Host: " + marker + b"\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["docs/"],
        "exclude": ["docs/unused.md"],
        "requires_split": ["docs/private-source.md"],
        "required_runtime": ["docs/public.md"],
        "required_release": ["docs/public.md"],
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert candidate == {"docs/public.md": b"Community guide\n"}
    assert {
        blocker["path"] for blocker in report["blockers"]
        if blocker["kind"] == "private-source-or-host-reference"
    } == {f"docs/private-{index}.md" for index in range(len(markers))}


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


def test_private_host_settings_fixture_is_not_exported() -> None:
    """The filename still names the operator host, so the fixture does not ship."""
    path = "apps/api/tests/test_goldeneye_config.py"
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    assert path in manifest["exclude"]
    data = (export.ROOT / path).read_bytes()
    assert b"@db.example.test" in data
    assert b"goldeneye" in data.lower()


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
        ".github/pull_request_template.md",
        ".github/workflows/pages.yml",
    ])


def test_public_examples_are_included() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    assert "examples/" in manifest["include"]
    for path in (
        "examples/README.md",
        "examples/_common.py",
        "examples/setup_all.py",
        "examples/weekly-newsletter/setup.py",
    ):
        assert export.matching_rule(path, manifest["include"])
        assert not export.matching_rule(path, manifest["exclude"])


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


def test_community_test_boundary_preserves_shared_fixtures() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    export.validate_manifest(manifest)
    for directory in ("apps/api/tests/", "apps/web/tests/", "engine/tests/"):
        assert directory not in manifest["exclude"]

    shared = (
        "apps/api/tests/test_github_project_connections.py",
        "apps/api/tests/test_skill_marketplace.py",
        "apps/web/tests/github-operations.test.tsx",
        "apps/worker/tests/test_worker.py",
        "engine/tests/test_run_targets_its_own_repo.py",
    )
    for path in shared:
        assert export.matching_rule(path, manifest["include"])
        assert not export.matching_rule(path, manifest["exclude"])
        assert not any(marker in (export.ROOT / path).read_bytes() for marker in export.PRIVATE_MARKERS)

    source_only = (
        "apps/api/tests/test_account_connect_integration.py",
        "apps/api/tests/test_cloud_product.py",
        "apps/web/tests/commercial-enterprise.test.ts",
        "apps/web/tests/edition-visibility.test.ts",
        "apps/worker/tests/test_external_rollout_readiness.py",
        "engine/tests/test_community_specpack_boundary.py",
        # The operator's private fork-promotion helper is not included in Community.
        "engine/tests/test_promoting_a_fork_pull_request.py",
    )
    assert all(path in manifest["exclude"] for path in source_only)

    rewrite = manifest["test_conftest_export_rewrite"]
    source = (export.ROOT / rewrite["source"]).read_bytes()
    assert hashlib.sha256(source).hexdigest() == rewrite["sha256"]
    assert b'os.environ["BOSS_EDITION"] = "community"' in source
    assert not any(marker in source for marker in export.PAID_SOURCE_MARKERS)

    sha = export.git(export.ROOT, "rev-parse", "HEAD^{commit}").decode().strip()
    report, selected = export.analyze(export.ROOT, sha, manifest)
    assert report["test_conftest_export_rewrite_applied"] is True
    assert selected[rewrite["destination"]] == source
    assert rewrite["source"] not in selected
    assert all(path in selected for path in shared)
    assert "docs/product/BOSS_COMMUNITY_EDITION_SPEC.md" not in selected


def test_mixed_test_suites_have_exact_exclusions_and_community_coverage() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    mixed_suites = {
        "apps/api/tests/test_api.py",
        "apps/api/tests/test_cloud_easy_connect_e2e.py",
        "apps/api/tests/test_edition_routers.py",
        "apps/web/tests/api-client.test.ts",
        "apps/web/tests/providerops.test.ts",
    }
    assert mixed_suites.issubset(set(manifest["exclude"]))
    for path in (
        "apps/api/tests/test_community_cloud_routes.py",
        "apps/api/tests/test_edition_routers_community.py",
        "apps/api/tests/test_provider_easy_connect.py",
        "apps/web/tests/api-client-community.test.ts",
        "apps/web/tests/providerops-community.test.ts",
    ):
        assert (export.ROOT / path).is_file()
        assert export.matching_rule(path, manifest["include"])
        assert not export.matching_rule(path, manifest["exclude"])
        assert not any(marker in (export.ROOT / path).read_bytes() for marker in export.PRIVATE_MARKERS)


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
legal_review_required: false
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
        pack_path, pack.replace(b"legal_review_required: false", b"legal_review_required: true")
    )[1] is False
    assert export.reviewed_marker_bytes(
        pack_path, pack + b"commercial_packaging_policy\n"
    )[1] is False
    actual_pack = (export.ROOT / pack_path).read_bytes()
    assert export.reviewed_marker_bytes(pack_path, actual_pack)[1]
    assert b"commercial_packaging_policy" not in actual_pack

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


def test_specpack_local_hosted_entitlement_filter_is_exactly_audited() -> None:
    path = "engine/boss_engine/services/specpack.py"
    source = b'''class SpecPackService:
    def _project_capabilities(self):
        active_account_connect_org_id = (
            edition.get("account_connect_org") or {}
        ).get("hosted_org_id")
        sql = """
                SELECT capability_key, capabilities, source, account_connect_org_id
                FROM license_entitlements
        """
        for row in rows:
            if (
                row["source"] == "account_connect_hosted"
                and row["account_connect_org_id"] != active_account_connect_org_id
            ):
                continue
'''
    masked, valid = export.reviewed_marker_bytes(path, source)
    assert valid
    assert not any(marker in masked for marker in export.PAID_SOURCE_MARKERS)
    changed = source.replace(
        b'                row["source"] == "account_connect_hosted"',
        b'                row["source"] == "account_connect_hosted_or_trial"',
    )
    assert export.reviewed_marker_bytes(path, changed)[1] is False
    added = source + b"# account_connect_billing_portal_session\n"
    masked, valid = export.reviewed_marker_bytes(path, added)
    assert valid
    assert any(marker in masked for marker in export.PAID_SOURCE_MARKERS)
    relocated = source.replace(
        b"def _project_capabilities(self):",
        b"def unrelated_method(self):",
    )
    assert export.reviewed_marker_bytes(path, relocated)[1] is False


def test_paid_audit_extension_and_regression_stay_outside_community_manifest() -> None:
    manifest = json.loads((MODULE_PATH.parent / "community_export_manifest.json").read_text())
    assert "engine/boss_engine/services/enterprise_audit.py" in manifest["exclude"]
    assert "engine/tests/test_enterprise_audit_boundary.py" in manifest["exclude"]


def test_community_router_is_selected_after_source_audit() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    assert "apps/api/app/routers/community.py" not in manifest["requires_split"]
    assert export.matching_rule("apps/api/app/routers/community.py", manifest["include"])


def test_public_client_manifest_rewrites_the_package_root() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    export.validate_manifest(manifest)
    assert manifest["package_export_rewrite"] == {
        "path": "packages/api-client/package.json",
        "community_source": "packages/api-client/src/community.ts",
        "from": "./src/index.ts",
        "to": "./src/community.ts",
    }
    assert "packages/api-client/src/index.ts" in manifest["exclude"]


def test_exported_markdown_links_must_resolve_inside_candidate() -> None:
    selected = {
        "README.md": b"[Guide](docs/community/guide.md) [Web](https://example.org)\n",
        "docs/community/guide.md": b"[Local](../../README.md) [Missing](../product/spec.md) [Here](#section)\n",
    }
    edges = export.markdown_link_edges(selected)
    assert [(edge["kind"], edge["to"]) for edge in edges] == [
        ("markdown-link-target-missing", "docs/product/spec.md"),
    ]


def test_private_host_kit_stays_out_of_the_community_candidate() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    export.validate_manifest(manifest)
    for path in (
        "engine/boss_engine/services/goldeneye_paas.py",
        "packages/goldeneye/install.sh",
        "packages/goldeneye/core/bin/ge",
        "docs/product/GOLDENEYE_PAAS.md",
    ):
        included = export.matching_rule(path, manifest["include"])
        excluded = export.matching_rule(path, manifest["exclude"])
        assert excluded or not included
        assert path not in manifest["required_runtime"]
    assert manifest["safe_symlinks"] == {}
    dockerfile = (export.ROOT / "Dockerfile.community").read_bytes()
    assert b"goldeneye" not in dockerfile.lower()
    assert b"COPY LICENSE ./LICENSE" in dockerfile
    assert b"COPY --chown=boss:boss LICENSE ./LICENSE" in dockerfile


def test_community_candidate_omits_the_private_host_name() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    export.validate_manifest(manifest)
    sha = export.git(export.ROOT, "rev-parse", "HEAD^{commit}").decode().strip()
    report, selected = export.analyze(export.ROOT, sha, manifest)
    assert report["blocker_count"] == 0, report["blockers"][:8]
    leaked = [
        path for path, data in selected.items()
        if "goldeneye" in path.lower() or b"goldeneye" in data.lower()
    ]
    assert leaked == []


def test_private_host_redaction_keeps_shared_infra_status_test_valid() -> None:
    path = "apps/api/tests/test_e2e_infra_ui_v1.py"
    source = (export.ROOT / path).read_bytes()
    redacted = export._private_host.redact_private_host(path, source)
    assert b"goldeneye" not in redacted.lower()
    compile(redacted, path, "exec")
    text = redacted.decode()
    assert "def test_e2e_elastic_status_empty_is_200_not_configured" in text
    assert "def test_e2e_provider_usage_summary_empty_is_200_not_configured" in text
    assert "def test_e2e_status_routes_stay_honest_after_missing_schema" in text
    assert "def test_e2e_create_droplet_http_blocked_without_provision_flag" in text
    assert "def test_e2e_goldeneye_status_empty_is_200_not_configured" not in text
    assert "DROP TABLE IF EXISTS provider_usage_events" in text
    assert "DROP TABLE IF EXISTS worker_instances" in text
    assert "connection.execute(\n        )" not in text


def test_private_host_redaction_rewrites_published_operation_counts() -> None:
    features = export._private_host.redact_private_host(
        "docs/community/FEATURES.md",
        (export.ROOT / "docs/community/FEATURES.md").read_bytes(),
    ).decode()
    assert "All 982 mounted" in features
    assert "851 use API policy" in features
    assert "none remain held" in features
    assert "993" not in features
    assert "2 remain held" not in features
    assert "993" in (export.ROOT / "docs/community/FEATURES.md").read_text()

    manual = export._private_host.redact_private_host(
        "docs/community/MCP.md",
        (export.ROOT / "docs/community/MCP.md").read_bytes(),
    ).decode()
    assert "**982 Community**" in manual
    assert "**1,034 Enterprise**" in manual
    assert "| Community | 851 | 64 | 0 | 67 |" in manual
    assert "| Paid-only additions | 39 | 0 | 2 | 0 |" in manual

    surfaces = export._private_host.redact_private_host(
        "docs/community/CONTROL_SURFACES.md",
        (export.ROOT / "docs/community/CONTROL_SURFACES.md").read_bytes(),
    ).decode()
    assert "all 982" in surfaces
    assert "969 `/v1/`" in surfaces
    assert "851 are eligible" in surfaces
    assert "993" not in surfaces

    matrix = export._private_host.redact_private_host(
        "docs/community/TERMINAL_CAPABILITY_MATRIX.md",
        (export.ROOT / "docs/community/TERMINAL_CAPABILITY_MATRIX.md").read_bytes(),
    ).decode()
    assert "| Goldeneye |" not in matrix
    assert "**982**" in matrix
    assert "**969**" in matrix
    assert "all 982" in matrix
    assert "**1,034**" in matrix
    assert "993" not in matrix


def test_private_host_redaction_keeps_community_main_valid() -> None:
    path = "apps/api/app/main.py"
    rewritten = export.rewrite_community_main((export.ROOT / path).read_bytes())
    assert rewritten is not None
    redacted = export._private_host.redact_private_host(path, rewritten)
    assert b"goldeneye" not in redacted.lower()
    compile(redacted, path, "exec")
    text = redacted.decode()
    assert "def run_resource_janitor" in text
    assert "/v1/goldeneye" not in text
    assert "def goldeneye_package_catalog" not in text
    assert "def list_goldeneye_package_installs" not in text
    assert "GoldeneyePaasService" not in text


def test_shared_config_exception_is_exact_and_still_blocks_paid_code() -> None:
    path = "engine/boss_engine/config.py"
    approved = b"class Settings:\n" + export.SHARED_CONFIG_PAID_FIELDS
    assert not export.has_paid_source_markers(path, approved)
    assert export.has_paid_source_markers("engine/boss_engine/other.py", approved)
    assert export.has_paid_source_markers(path, approved + b"account_connect_callback = True\n")
    changed = approved.replace(b"BOSS_ACCOUNT_CONNECT_URL", b"BOSS_ACCOUNT_CONNECT_API_URL")
    assert export.has_paid_source_markers(path, changed)
    assert export.has_paid_source_markers(path, approved + b"CloudProduct = object()\n")


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
    (source.parent / "index.ts").write_text('export * from "./paid";\n')
    (source.parent / "paid.ts").write_text("export const commercial = true;\n")
    tests = repo / "packages/api-client/tests"
    tests.mkdir()
    (tests / "private-compat.test.ts").write_text('import "../src/index";\n')
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
        "exclude": [
            "packages/api-client/src/index.ts",
            "packages/api-client/src/paid.ts",
            "packages/api-client/tests/private-compat.test.ts",
        ],
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
    assert "packages/api-client/src/index.ts" not in candidate
    assert "packages/api-client/src/paid.ts" not in candidate
    assert "packages/api-client/tests/private-compat.test.ts" not in candidate
    assert not any(b["kind"] == "typescript-import" for b in report["blockers"])
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


def test_community_schema_rewrite_is_exact_and_preserves_easy_connect(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    db = repo / "db"
    db.mkdir(parents=True)
    (db / "schema.sql").write_text(
        "CREATE TABLE account_connect_orgs ();\nCREATE TABLE enterprise_clusters ();\n"
    )
    community = b"CREATE TABLE account_connect_orgs ();\n"
    (db / "schema.community.sql").write_bytes(community)
    (db / "unreviewed.sql").write_text("CREATE TABLE hosted_marketplace_sync_runs ();\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
        ],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["db/"],
        "exclude": ["db/unused.sql"],
        "requires_split": ["db/schema.sql"],
        "required_runtime": ["db/schema.sql"],
        "required_release": ["db/schema.sql"],
        "schema_export_rewrite": {
            "source": "db/schema.community.sql",
            "destination": "db/schema.sql",
            "sha256": hashlib.sha256(community).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["schema_export_rewrite_applied"] is True
    assert candidate == {"db/schema.sql": community}
    assert any(
        blocker["path"] == "db/unreviewed.sql"
        and blocker["kind"] == "mixed-paid-source-needs-split"
        for blocker in report["blockers"]
    )
    assert not any(blocker.get("path") == "db/schema.sql" for blocker in report["blockers"])

    changed = json.loads(json.dumps(manifest))
    changed["schema_export_rewrite"]["sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert report["schema_export_rewrite_applied"] is False
    assert "schema-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "db/schema.sql" not in candidate

    (db / "schema.community.sql").write_bytes(community + b"-- drift\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "drift",
        ],
        check=True,
    )
    drift_sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    report, candidate = export.analyze(repo, drift_sha, manifest)
    assert "schema-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "db/schema.sql" not in candidate


def test_community_schema_rewrite_scans_pinned_output_for_private_host(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    db = repo / "db"
    db.mkdir(parents=True)
    (db / "schema.sql").write_text("CREATE TABLE enterprise_clusters ();\n")
    community = b"CREATE TABLE shared ();\n-- 2.29.2.202\n"
    (db / "schema.community.sql").write_bytes(community)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["db/"],
        "exclude": ["db/unused.sql"],
        "requires_split": ["db/schema.sql"],
        "required_runtime": ["db/schema.sql"],
        "required_release": ["db/schema.sql"],
        "schema_export_rewrite": {
            "source": "db/schema.community.sql",
            "destination": "db/schema.sql",
            "sha256": hashlib.sha256(community).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert "db/schema.sql" not in candidate
    assert {"kind": "private-source-or-host-reference", "path": "db/schema.sql"} in report["blockers"]
    assert {"kind": "missing-runtime-entrypoint", "path": "db/schema.sql"} in report["blockers"]


def test_community_openapi_rewrite_is_checksum_pinned(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    (repo / "api").mkdir(parents=True)
    (repo / "api/openapi.yaml").write_text("private: true\n")
    public = b"openapi: 3.1.0\ninfo:\n  title: BOSS Community API\n"
    (repo / "api/openapi.community.yaml").write_bytes(public)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["api/openapi.yaml", "api/openapi.community.yaml"],
        "exclude": ["unused.md"],
        "requires_split": ["api/openapi.yaml"],
        "required_runtime": ["api/openapi.yaml"],
        "required_release": ["api/openapi.yaml"],
        "openapi_export_rewrite": {
            "source": "api/openapi.community.yaml",
            "destination": "api/openapi.yaml",
            "sha256": hashlib.sha256(public).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["openapi_export_rewrite_applied"] is True
    assert candidate == {"api/openapi.yaml": public}
    assert not any(b.get("path") == "api/openapi.yaml" for b in report["blockers"])

    changed = json.loads(json.dumps(manifest))
    changed["openapi_export_rewrite"]["sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert report["openapi_export_rewrite_applied"] is False
    assert "openapi-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "api/openapi.yaml" not in candidate

    (repo / "api/openapi.community.yaml").write_bytes(public + b"x-extra: true\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "drift"],
        check=True,
    )
    drift_sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    report, candidate = export.analyze(repo, drift_sha, manifest)
    assert report["openapi_export_rewrite_applied"] is False
    assert "openapi-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "api/openapi.yaml" not in candidate

def test_reviewed_community_services_allow_only_pinned_easy_connect_fields(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    service_dir = repo / "engine/boss_engine/services"
    service_dir.mkdir(parents=True)
    bodies = {
        "engine/boss_engine/services/community_edition.py": b"account_connect_url = None\n",
        "engine/boss_engine/services/community_cloud.py": b"account_connect_status = 'unlinked'\n",
    }
    for path, data in bodies.items():
        (repo / path).write_bytes(data)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["engine/boss_engine/services/"],
        "exclude": ["unused.py"],
        "requires_split": ["withheld.py"],
        "required_runtime": ["engine/boss_engine/services/community_edition.py"],
        "required_release": ["engine/boss_engine/services/community_cloud.py"],
        "reviewed_community_service_sha256": {
            path: hashlib.sha256(data).hexdigest() for path, data in bodies.items()
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert candidate == bodies
    assert report["blocker_count"] == 0

    changed = json.loads(json.dumps(manifest))
    changed["reviewed_community_service_sha256"]["engine/boss_engine/services/community_cloud.py"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert "reviewed-community-service-changed" in {b["kind"] for b in report["blockers"]}
    assert "engine/boss_engine/services/community_cloud.py" not in candidate

    tainted = dict(bodies)
    tainted["engine/boss_engine/services/community_cloud.py"] += b"HostedMarketplace = True\n"
    (repo / "engine/boss_engine/services/community_cloud.py").write_bytes(
        tainted["engine/boss_engine/services/community_cloud.py"]
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "tainted"],
        check=True,
    )
    changed = json.loads(json.dumps(manifest))
    changed["reviewed_community_service_sha256"]["engine/boss_engine/services/community_cloud.py"] = hashlib.sha256(
        tainted["engine/boss_engine/services/community_cloud.py"]
    ).hexdigest()
    report, candidate = export.analyze(repo, export.git(repo, "rev-parse", "HEAD").decode().strip(), changed)
    assert "reviewed-community-service-has-paid-marker" in {b["kind"] for b in report["blockers"]}
    assert "engine/boss_engine/services/community_cloud.py" not in candidate

def test_community_specpack_rewrite_is_checksum_pinned(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    target = repo / "packages/specpack"
    target.mkdir(parents=True)
    (target / "manifest.yaml").write_text("packs: [enterprise_edition]\n")
    public = b"packs: [community_edition]\n"
    (target / "manifest.community.yaml").write_bytes(public)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["packages/specpack/"],
        "exclude": ["unused.yaml"],
        "requires_split": ["packages/specpack/manifest.yaml"],
        "required_runtime": ["packages/specpack/manifest.yaml"],
        "required_release": ["packages/specpack/manifest.yaml"],
        "specpack_export_rewrites": [
            {
                "source": "packages/specpack/manifest.community.yaml",
                "destination": "packages/specpack/manifest.yaml",
                "sha256": hashlib.sha256(public).hexdigest(),
            }
        ],
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["specpack_export_rewrites_applied"] == [
        "packages/specpack/manifest.yaml"
    ]
    assert candidate == {"packages/specpack/manifest.yaml": public}

    changed = json.loads(json.dumps(manifest))
    changed["specpack_export_rewrites"][0]["sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert report["specpack_export_rewrites_applied"] == []
    assert "specpack-export-source-changed" in {b["kind"] for b in report["blockers"]}
    assert "packages/specpack/manifest.yaml" not in candidate


def test_public_specpack_manifest_references_existing_reviewed_specs() -> None:
    import yaml

    repo = MODULE_PATH.parents[1]
    manifest = json.loads((MODULE_PATH.parent / "community_export_manifest.json").read_text())
    export.validate_manifest(manifest)
    catalog = yaml.safe_load(
        (repo / "packages/specpack/manifest.community.yaml").read_text()
    )
    for group in catalog["objects"].values():
        for name in group:
            path = f"packages/specpack/objects/{name}.yaml"
            assert (repo / path).is_file()
            assert export.matching_rule(path, manifest["exclude"]) is None
    for group in catalog["workflows"].values():
        for name in group:
            path = f"packages/specpack/workflows/{name}.yaml"
            assert (repo / path).is_file()
            assert export.matching_rule(path, manifest["exclude"]) is None
    for kind in ("policies", "packs"):
        for name in catalog[kind]:
            path = f"packages/specpack/{kind}/{name}.yaml"
            assert (repo / path).is_file()
            assert export.matching_rule(path, manifest["exclude"]) is None
    for rewrite in manifest["specpack_export_rewrites"]:
        source = repo / rewrite["source"]
        assert source.is_file()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == rewrite["sha256"]
        assert not any(marker in source.read_bytes() for marker in export.PRIVATE_MARKERS)
    safety = yaml.safe_load(
        (repo / "packages/specpack/policies/default_safety.community.yaml").read_text()
    )
    assert safety["rules"]["sandbox_real_mode"]["github_real_pr_allowed"] is False
    assert safety["rules"]["sandbox_real_mode"]["allowed_github_targets"] == []
    for skill_file in ("github", "software_factory"):
        skill_spec = yaml.safe_load(
            (repo / f"packages/specpack/skills/{skill_file}.community.yaml").read_text()
        )
        assert all(
            skill["policy"]["allowed_targets"] == []
            for skill in skill_spec["skills"]
            if "allowed_targets" in skill.get("policy", {})
        )

def test_public_docs_do_not_export_internal_product_brief() -> None:
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    internal = "docs/product/BOSS_COMMUNITY_EDITION_SPEC.md"
    assert not export.matching_rule(internal, manifest["include"])
    assert export.matching_rule("docs/community/FEATURES.md", manifest["include"])
    assert export.matching_rule("docs/product/V1_FEATURE_LOCK.md", manifest["include"])
    public_docs = (export.ROOT / "docs/community/FEATURES.md").read_text()
    assert internal not in public_docs
def test_pinned_public_makefile_keeps_metatron_entrypoint() -> None:
    import hashlib

    public = (export.ROOT / "Makefile.community").read_bytes()
    manifest = json.loads(export.DEFAULT_MANIFEST.read_text())
    assert hashlib.sha256(public).hexdigest() == manifest["makefile_export_rewrite"]["sha256"]
    assert b"\nmetatron:" in public
    assert b"scripts/metatron_tui.py" in public


def test_community_main_rewrite_removes_exact_paid_composition(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "source"
    main = repo / "apps/api/app/main.py"
    main.parent.mkdir(parents=True)
    source = (
        b"".join(old for old, _new in export.MAIN_COMMUNITY_REPLACEMENTS[:3])
        + b"SERVICE_APPROVAL_GATES = [\n"
        + export.MAIN_COMMUNITY_ACCOUNT_GATE
        + b"]\n"
        + export.MAIN_COMMUNITY_REPLACEMENTS[3][0]
    )
    main.write_bytes(source)
    public = export.rewrite_community_main(source)
    assert public is not None
    assert b"CloudProductService" not in public
    assert b"EnterpriseAuditTrailService" not in public
    assert b"register_paid_cloud_routes" not in public
    assert b"CommunityCloudService(settings, commercial)" in public
    assert export.rewrite_community_main(source.replace(b"register_paid_cloud_routes", b"register_new_paid_routes")) is None

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    manifest = {
        "schema_version": 1,
        "include": ["apps/api/app/main.py"],
        "exclude": ["unused.py"],
        "requires_split": ["apps/api/app/main.py"],
        "required_runtime": ["apps/api/app/main.py"],
        "required_release": ["apps/api/app/main.py"],
        "main_export_rewrite": {
            "path": "apps/api/app/main.py",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "output_sha256": hashlib.sha256(public).hexdigest(),
        },
    }
    export.validate_manifest(manifest)
    report, candidate = export.analyze(repo, sha, manifest)
    assert report["main_export_rewrite_applied"] is True
    assert candidate["apps/api/app/main.py"] == public
    assert not any(blocker.get("path") == "apps/api/app/main.py" for blocker in report["blockers"])

    changed = json.loads(json.dumps(manifest))
    changed["main_export_rewrite"]["output_sha256"] = "0" * 64
    report, candidate = export.analyze(repo, sha, changed)
    assert "main-export-output-changed" in {blocker["kind"] for blocker in report["blockers"]}
    assert "apps/api/app/main.py" not in candidate


def test_only_the_reviewed_public_site_url_is_allowed_under_a_private_domain() -> None:
    assert not export.has_private_marker(b'<a href="https://boss.sean.network/">BOSS</a>')
    assert not export.has_private_marker(b"site_url: https://boss.sean.network/docs/\n")
    assert not export.has_private_marker(b"[Website](https://boss.sean.network)")
    assert export.has_private_marker(b"https://plane.local.sean.network/api/v1")
    assert export.has_private_marker(b"https://boss-api.local.sean.network")
    assert export.has_private_marker(b"https://boss.sean.network.example/")
    assert export.has_private_marker(b"https://boss.sean.networks/")
    assert export.has_private_marker(b"https://boss.sean.network/ and https://analytics.sean.network/")
    assert export.has_private_marker(b"http://boss.sean.network/")
