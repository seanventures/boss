from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]

READY = "ready"
BLOCKED = "blocked"
EXTERNAL_VERIFICATION_REQUIRED = "external_verification_required"


@dataclass(frozen=True)
class ReadinessCheck:
    id: str
    requirement: str
    status: str
    summary: str
    evidence: list[str]
    missing: list[str]
    warnings: list[str]
    next_action: str


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _configured(env: Mapping[str, str], key: str) -> bool:
    return bool((env.get(key) or "").strip())


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _file_evidence(root: Path, paths: Sequence[str]) -> tuple[list[str], list[str]]:
    evidence: list[str] = []
    missing: list[str] = []
    for path_text in paths:
        path = root / path_text
        if path.exists():
            evidence.append(f"file present: {_relative(path, root)}")
        else:
            missing.append(path_text)
    return evidence, missing


def _env_evidence(env: Mapping[str, str], keys: Sequence[str]) -> tuple[list[str], list[str]]:
    evidence: list[str] = []
    missing: list[str] = []
    for key in keys:
        if _configured(env, key):
            evidence.append(f"env configured: {key}")
        else:
            missing.append(key)
    return evidence, missing


def evaluate_readiness(
    env: Mapping[str, str] | None = None,
    root: Path = ROOT,
) -> dict[str, object]:
    env = os.environ if env is None else env
    checks = [
        _community_local_mode_check(env, root),
        _hosted_account_connect_check(env),
        _hosted_marketplace_team_billing_check(env),
        _enterprise_scim_runtime_check(env),
        _enterprise_identity_runtime_check(env),
        _external_support_routing_check(env),
        _self_hosted_package_check(root),
        _live_enterprise_cluster_check(root),
        _providerops_write_rollout_check(env),
    ]
    status = _overall_status(checks)
    return {
        "status": status,
        "summary": _summary_for_status(status),
        "checks": [asdict(check) for check in checks],
    }


def _overall_status(checks: Sequence[ReadinessCheck]) -> str:
    if any(check.status == BLOCKED for check in checks):
        return BLOCKED
    if any(check.status == EXTERNAL_VERIFICATION_REQUIRED for check in checks):
        return EXTERNAL_VERIFICATION_REQUIRED
    return READY


def _summary_for_status(status: str) -> str:
    if status == BLOCKED:
        return (
            "One or more external rollout prerequisites are missing. "
            "BOSS should remain fail-closed for those hosted/provider paths."
        )
    if status == EXTERNAL_VERIFICATION_REQUIRED:
        return (
            "Local prerequisites are present, but live hosted/provider rollout "
            "still requires environment-specific verification."
        )
    return "All tracked local and external rollout prerequisites are ready."


def _community_local_mode_check(
    env: Mapping[str, str],
    root: Path,
) -> ReadinessCheck:
    evidence, missing = _file_evidence(
        root,
        [
            ".env.example",
            "packages/specpack/packs/community_edition.yaml",
            "docs/product/PRD-v2.0.md",
        ],
    )
    env_edition = (env.get("BOSS_EDITION") or "").strip().lower()
    if not env_edition:
        evidence.append("env default: BOSS_EDITION unset, runtime defaults to community")
    else:
        evidence.append(f"env configured: BOSS_EDITION={env_edition}")

    env_example = root / ".env.example"
    if env_example.exists() and "BOSS_EDITION=community" in env_example.read_text(
        encoding="utf-8"
    ):
        evidence.append(".env.example defaults Community to local/no hosted auth")
    else:
        missing.append(".env.example BOSS_EDITION=community default")

    # AUTO-004: verify bootstrap gate documented + default false (local only, fail-closed; P0 from SEC/GAP-007)
    if env_example.exists():
        ex_text = env_example.read_text(encoding="utf-8")
        if "BOSS_BOOTSTRAP_SEED_API_ENABLED=false" in ex_text:
            evidence.append(".env.example documents BOSS_BOOTSTRAP_SEED_API_ENABLED=false (fail-closed default per AGENTS/SEC)")
        else:
            missing.append(".env.example BOSS_BOOTSTRAP_SEED_API_ENABLED=false comment/doc")
    bootstrap_env = (env.get("BOSS_BOOTSTRAP_SEED_API_ENABLED") or "").strip().lower()
    if bootstrap_env in {"", "false"}:
        evidence.append("env default or false: BOSS_BOOTSTRAP_SEED_API_ENABLED (seed API gated)")
    elif bootstrap_env == "true":
        evidence.append("env: BOSS_BOOTSTRAP_SEED_API_ENABLED=true (explicit dev only)")

    status = READY if not missing and env_edition in {"", "community"} else BLOCKED
    return ReadinessCheck(
        id="community_local_mode",
        requirement="Community edition remains useful without hosted BOSS auth.",
        status=status,
        summary=(
            "Community local mode can be reviewed without Account Connect."
            if status == READY
            else "Community local mode defaults are not aligned with the PRD."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "No action required for local Community readiness."
            if status == READY
            else "Restore Community as the default local edition and keep hosted auth optional."
        ),
    )


def _hosted_account_connect_check(env: Mapping[str, str]) -> ReadinessCheck:
    evidence, missing = _env_evidence(env, ["BOSS_ACCOUNT_CONNECT_URL"])
    warnings = []
    if _configured(env, "BOSS_ACCOUNT_CONNECT_TOKEN"):
        evidence.append("env configured: BOSS_ACCOUNT_CONNECT_TOKEN")
    else:
        warnings.append(
            "BOSS_ACCOUNT_CONNECT_TOKEN is not configured; only unauthenticated "
            "or separately authenticated hosted endpoints could be verified."
        )
    status = EXTERNAL_VERIFICATION_REQUIRED if not missing else BLOCKED
    return ReadinessCheck(
        id="hosted_account_connect_service",
        requirement="Cloud/attached Enterprise Account Connect requires a real hosted service.",
        status=status,
        summary=(
            "Hosted Account Connect endpoint is configured locally, but live service "
            "health/auth/link flows still require verification."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Hosted Account Connect endpoint is not configured."
        ),
        evidence=evidence,
        missing=missing,
        warnings=warnings,
        next_action=(
            "Run Account Connect health/auth/link smoke checks against the hosted service."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Deploy or choose the hosted Account Connect service and set BOSS_ACCOUNT_CONNECT_URL."
        ),
    )


def _hosted_marketplace_team_billing_check(env: Mapping[str, str]) -> ReadinessCheck:
    evidence, missing = _env_evidence(env, ["BOSS_ACCOUNT_CONNECT_URL"])
    status = EXTERNAL_VERIFICATION_REQUIRED if not missing else BLOCKED
    return ReadinessCheck(
        id="hosted_marketplace_team_billing",
        requirement=(
            "Hosted marketplace distribution, team invites, billing portal, and "
            "entitlement sync require the hosted Account Connect service."
        ),
        status=status,
        summary=(
            "Hosted base URL is configured; live catalog/team/billing endpoints still "
            "need smoke verification."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Hosted marketplace/team/billing service base URL is missing."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "Run hosted marketplace sync, team invite, and billing portal smoke checks."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Configure BOSS_ACCOUNT_CONNECT_URL before claiming hosted marketplace/team/billing readiness."
        ),
    )


def _enterprise_scim_runtime_check(env: Mapping[str, str]) -> ReadinessCheck:
    evidence, missing = _env_evidence(env, ["BOSS_ENTERPRISE_SCIM_BEARER_TOKENS"])
    status = EXTERNAL_VERIFICATION_REQUIRED if not missing else BLOCKED
    return ReadinessCheck(
        id="enterprise_scim_runtime",
        requirement="IdP-facing SCIM runtime must fail closed unless bearer tokens and rollout policy are configured.",
        status=status,
        summary=(
            "SCIM bearer token material is configured locally; active provider metadata "
            "and IdP push verification remain environment-specific."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "SCIM bearer token material is not configured."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "Create active SCIM provider metadata, rollout policy, and run IdP provisioning smoke checks."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Set BOSS_ENTERPRISE_SCIM_BEARER_TOKENS before enabling IdP-facing SCIM runtime."
        ),
    )


def _enterprise_identity_runtime_check(env: Mapping[str, str]) -> ReadinessCheck:
    oidc_keys = ["BOSS_OIDC_ISSUER", "BOSS_OIDC_AUDIENCE", "BOSS_OIDC_JWKS_URL"]
    oidc_ready = _truthy(env.get("BOSS_OIDC_ENABLED")) and all(
        _configured(env, key) for key in oidc_keys
    )
    saml_ready = _truthy(env.get("BOSS_ENTERPRISE_SAML_ENABLED"))
    evidence: list[str] = []
    missing: list[str] = []
    if oidc_ready:
        evidence.extend(["env configured: BOSS_OIDC_ENABLED", *[f"env configured: {key}" for key in oidc_keys]])
    if saml_ready:
        evidence.append("env configured: BOSS_ENTERPRISE_SAML_ENABLED")
    if not oidc_ready and not saml_ready:
        missing.append(
            "BOSS_OIDC_ENABLED with issuer/audience/JWKS or BOSS_ENTERPRISE_SAML_ENABLED"
        )
    status = EXTERNAL_VERIFICATION_REQUIRED if not missing else BLOCKED
    return ReadinessCheck(
        id="enterprise_identity_runtime",
        requirement="Live enterprise SSO/SAML/OIDC rollout requires explicit provider configuration and rollout policy.",
        status=status,
        summary=(
            "Enterprise identity runtime has local env configuration; active provider "
            "metadata and login smoke checks remain external verification."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "No enterprise OIDC or SAML runtime is configured."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "Configure active identity-provider records, rollout policy, and run OIDC/SAML login smoke checks."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Configure OIDC or SAML runtime before claiming live enterprise SSO readiness."
        ),
    )


def _external_support_routing_check(env: Mapping[str, str]) -> ReadinessCheck:
    evidence, missing = _env_evidence(env, ["BOSS_ACCOUNT_CONNECT_URL"])
    status = EXTERNAL_VERIFICATION_REQUIRED if not missing else BLOCKED
    return ReadinessCheck(
        id="external_support_provider_routing",
        requirement="Hosted enterprise support escalation requires a real support endpoint through Account Connect.",
        status=status,
        summary=(
            "Account Connect URL is configured; live support-provider escalation "
            "still needs an external smoke check."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Hosted support escalation endpoint is not configured."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "Run a hosted support-escalation smoke check and verify the returned hosted case reference."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Configure BOSS_ACCOUNT_CONNECT_URL and hosted support routing before claiming support escalation readiness."
        ),
    )


def _self_hosted_package_check(root: Path) -> ReadinessCheck:
    required_files = [
        "deploy/kubernetes/base/kustomization.yaml",
        "deploy/kubernetes/base/secret-template.yaml",
        "deploy/kubernetes/base/api.yaml",
        "deploy/kubernetes/base/web.yaml",
        "deploy/kubernetes/base/worker.yaml",
        "deploy/helm/boss/Chart.yaml",
        "deploy/helm/boss/values.yaml",
        "deploy/helm/boss/templates/api.yaml",
        "deploy/helm/boss/templates/web.yaml",
        "deploy/helm/boss/templates/worker.yaml",
    ]
    evidence, missing = _file_evidence(root, required_files)
    status = READY if not missing else BLOCKED
    return ReadinessCheck(
        id="self_hosted_package_artifacts",
        requirement="Enterprise self-hosted packages must include API, web, worker, secrets, and guardrails.",
        status=status,
        summary=(
            "Kustomize and Helm package files are present for local validation."
            if status == READY
            else "Self-hosted package files are incomplete."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "Use make k8s-validate for package guardrail validation."
            if status == READY
            else "Restore missing self-hosted package artifacts before release."
        ),
    )


def _live_enterprise_cluster_check(root: Path) -> ReadinessCheck:
    evidence, missing_files = _file_evidence(
        root,
        [
            "deploy/kubernetes/base/kustomization.yaml",
            "deploy/helm/boss/Chart.yaml",
        ],
    )
    missing = [
        *missing_files,
        "target Kubernetes cluster/context",
        "runtime Secret values for database/API/worker/auth",
        "persistent storage and database backup plan",
        "explicit rollout approval",
        "recorded live cluster validation run",
    ]
    return ReadinessCheck(
        id="live_enterprise_cluster_rollout",
        requirement="Verified live multi-node enterprise rollout requires real infrastructure and approval.",
        status=EXTERNAL_VERIFICATION_REQUIRED,
        summary=(
            "Local deployment packages can be validated, but this checker does not claim "
            "a live cluster rollout."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "Apply reviewed overlays to an approved cluster, configure secrets/storage, "
            "then record a live cluster validation run in BOSS."
        ),
    )


def _providerops_write_rollout_check(env: Mapping[str, str]) -> ReadinessCheck:
    provider_tokens = [
        "BOSS_DIGITALOCEAN_TOKEN",
        "BOSS_CLOUDFLARE_API_TOKEN",
        "BOSS_SUPABASE_ACCESS_TOKEN",
    ]
    evidence: list[str] = []
    missing: list[str] = []
    if _truthy(env.get("BOSS_INFRA_REAL_PROVISION_ALLOWED")):
        evidence.append("env configured: BOSS_INFRA_REAL_PROVISION_ALLOWED")
    else:
        missing.append("BOSS_INFRA_REAL_PROVISION_ALLOWED=true")
    token_evidence, token_missing = _env_evidence(env, provider_tokens)
    evidence.extend(token_evidence)
    if not token_evidence:
        missing.append("one of " + ", ".join(token_missing))
    status = EXTERNAL_VERIFICATION_REQUIRED if not missing else BLOCKED
    return ReadinessCheck(
        id="providerops_write_rollout",
        requirement="Provider writes require explicit enablement, credentials, ChangeSet, BudgetReservation, approval, and ResourceReceipt.",
        status=status,
        summary=(
            "Provider write env prerequisites are configured; actual provider writes "
            "still require policy-gated ChangeSet smoke tests."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Provider write rollout remains fail-closed by local configuration."
        ),
        evidence=evidence,
        missing=missing,
        warnings=[],
        next_action=(
            "Run provider-specific ChangeSet smoke tests with approvals and verify ResourceReceipts."
            if status == EXTERNAL_VERIFICATION_REQUIRED
            else "Keep provider writes disabled, or configure explicit provider credentials and enablement for an approved rollout."
        ),
    )


def format_text(report: Mapping[str, object]) -> str:
    lines = [
        f"External rollout readiness: {report['status']}",
        str(report["summary"]),
    ]
    checks = report.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            lines.append("")
            lines.append(f"- {check['id']}: {check['status']}")
            lines.append(f"  Requirement: {check['requirement']}")
            lines.append(f"  Summary: {check['summary']}")
            missing = check.get("missing") or []
            warnings = check.get("warnings") or []
            if missing:
                lines.append(f"  Missing: {', '.join(str(item) for item in missing)}")
            if warnings:
                lines.append(f"  Warnings: {'; '.join(str(item) for item in warnings)}")
            lines.append(f"  Next: {check['next_action']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report external rollout blockers without calling hosted or provider services."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any tracked blocker or external verification remains.",
    )
    args = parser.parse_args(argv)
    report = evaluate_readiness()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text(report))
    if report["status"] == BLOCKED:
        return 1
    if args.strict and report["status"] != READY:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
