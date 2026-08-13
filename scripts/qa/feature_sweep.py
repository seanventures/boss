"""Community operator feature sweep for the unified QA harness.

Phase 1 of ``scripts/qa/harness.py``. Probes a curated settings + features
surface — not every mounted route. Never POSTs spendy or mutating work
(no code-task create, changeset apply, provider easy-connect, budget reserve).

Never prints API keys.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.qa.lib import (
    DEFAULT_PROJECT_ID,
    BossClient,
    CheckResult,
    is_2xx,
    item_count,
)

PHASE = "features"

EXPECT_2XX = "2xx"
EXPECT_2XX_OR_404 = "2xx_or_404"
EXPECT_2XX_OR_503 = "2xx_or_503"
EXPECT_401_UNAUTH = "401_unauth"

# Paid Cloud/Enterprise operational surfaces. Community edition honesty:
# 404 is PASS; 500 is FAIL. Inspected list GETs (prefix-only paths 404 even
# when the paid router is mounted).
PAID_PREFIXES = (
    "/v1/account-connect",
    "/v1/marketplace",
    "/v1/self-improvement",
    "/v1/enterprise/clusters",
)

# Never enqueue these from this sweep.
FORBIDDEN_POST_PREFIXES = (
    "/v1/code-tasks",
    "/v1/changesets",
    "/v1/providers",
    "/v1/budget/reserve",
    "/v1/budget/consume",
    "/v1/budget/quote",
    "/v1/commands",
    "/v1/github/easy-connect",
    "/v1/cloud/easy-connect",
)


@dataclass
class Probe:
    name: str
    path: str
    method: str = "GET"
    query: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    required: bool = True
    expect: str = EXPECT_2XX
    authenticate: bool = True


def _q(project_id: str, **extra: Any) -> dict[str, Any]:
    return {"project_id": project_id, **extra}


def build_probes(
    project_id: str,
    *,
    actor: str = "executive_console",
) -> list[Probe]:
    """Curated Community operator catalog. Paths inspected against apps/api."""
    q = _q(project_id)
    budget_q = {"scope_type": "project", "scope_id": project_id}

    probes: list[Probe] = [
        # Core
        Probe("core.health", "/health", authenticate=False),
        Probe("core.edition", "/v1/edition", query=q),
        Probe("core.auth-session", "/v1/auth/session"),
        Probe("core.projects", "/v1/projects"),
        Probe("core.orgs", "/v1/orgs"),
        Probe("core.members", "/v1/members", query=q),
        Probe("core.tasks", "/v1/tasks", query=q),
        Probe("core.jobs", "/v1/jobs", query=q),
        Probe("core.events", "/v1/events", query=q),
        Probe("core.receipts", "/v1/receipts", query=q),
        Probe("core.decisions-inbox", "/v1/decisions/inbox", query=q),
        Probe("core.deadletters", "/v1/deadletters", query=q),
        Probe("core.artifacts", "/v1/artifacts", query=q),
        Probe("core.plans", "/v1/plans", query=q),
        # Budget / settings
        Probe("settings.budget-status", "/v1/budget/status", query=budget_q),
        Probe("settings.secret-refs", "/v1/secret-refs", query=q),
        Probe("settings.providers", "/v1/providers", query=q),
        Probe("settings.provider-definitions", "/v1/provider-definitions"),
        Probe("settings.changesets", "/v1/changesets", query=q),
        Probe("settings.resources", "/v1/resources", query=q),
        Probe("settings.worker-pools", "/v1/worker-pools", query=q),
        Probe("settings.entitlements-sources", "/v1/entitlements/sources", query=q),
        # Factory / GitHub — bindings live at repository-bindings, not /bindings
        Probe("factory.wip", "/v1/factory/wip", query=q),
        Probe("factory.code-tasks", "/v1/code-tasks", query=q),
        Probe("factory.github-connections", "/v1/github/connections", query=q),
        Probe(
            "factory.github-bindings",
            "/v1/github/repository-bindings",
            query=q,
        ),
        Probe("factory.previews", "/v1/previews", query=q),
        # Workforce
        Probe("workforce.agent-studio-agents", "/v1/agent-studio/agents", query=q),
        Probe("workforce.assurance-overview", "/v1/assurance/overview", query=q),
        Probe("workforce.assurance-metrics", "/v1/assurance/metrics", query=q),
        Probe("workforce.assurance-cases", "/v1/assurance/cases", query=q),
        Probe("workforce.workflows", "/v1/workflows", query=q),
        Probe("workforce.meetings", "/v1/meetings", query=q),
        Probe("workforce.benchmark-apps", "/v1/qa/benchmark-apps", query=q),
        # Identity — Community catalog, must be 2xx not 404
        Probe("identity.identity-providers", "/v1/identity-providers"),
        # Paid expected 404 on Community (edition honesty). 500 fails.
        Probe(
            "paid.account-connect",
            "/v1/account-connect/health-checks",
            query=q,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "paid.marketplace",
            "/v1/marketplace/listings",
            query=q,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "paid.self-improvement",
            "/v1/self-improvement/proposals",
            query=q,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "paid.enterprise-clusters",
            "/v1/enterprise/clusters",
            query=q,
            expect=EXPECT_2XX_OR_404,
        ),
        # Optional operator extras. Missing schema → 503 WARN, not fail.
        Probe(
            "optional.token-efficiency-scorecard",
            "/v1/token-efficiency/scorecard",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_503,
        ),
        Probe(
            "optional.elastic-status",
            "/v1/elastic/status",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_503,
        ),
        Probe(
            "optional.worker-autoscaling-status",
            "/v1/worker-autoscaling/status",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_503,
        ),
        Probe(
            "optional.cloud-status",
            "/v1/cloud/status",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_503,
        ),
        # One safe POST: classify only, never escalate model spend/decisions.
        Probe(
            "safe.deadletters-classify",
            "/v1/deadletters/classify",
            method="POST",
            body={
                "project_id": project_id,
                "escalate_model": False,
                "actor": actor,
                "limit": 50,
            },
        ),
    ]
    return probes


def dry_run_catalog() -> dict[str, Any]:
    probes = build_probes(DEFAULT_PROJECT_ID)
    return {
        "count": len(probes),
        "names": [probe.name for probe in probes],
        "required": sum(1 for probe in probes if probe.required),
    }


def evaluate(
    probe: Probe,
    status: int | None,
    payload: Any = None,
) -> tuple[bool, str]:
    """Pure status evaluator used by the live sweep and unit tests."""
    path = probe.path
    method = probe.method
    expect = probe.expect
    count = item_count(payload)
    count_bit = f" items={count}" if count is not None else ""

    if status is None:
        return False, f"{method} {path} unreachable"

    if is_2xx(status):
        if expect == EXPECT_401_UNAUTH:
            return False, f"{method} {path} → {status} expected 401/403"
        return True, f"{method} {path} → {status}{count_bit}"

    if expect == EXPECT_2XX_OR_404 and status == 404:
        return True, f"{method} {path} → 404 (edition honesty)"

    if expect == EXPECT_2XX_OR_503 and status == 503:
        return True, f"{method} {path} → 503 WARN missing schema"

    if expect == EXPECT_401_UNAUTH and status in (401, 403):
        return True, f"{method} {path} → {status} fail-closed"

    return False, f"{method} {path} → {status} expected {expect}"


def _evidence(probe: Probe, payload: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "method": probe.method,
        "path": probe.path,
        "expect": probe.expect,
    }
    count = item_count(payload)
    if count is not None:
        evidence["item_count"] = count
    if isinstance(payload, dict):
        evidence["keys"] = [str(key) for key in list(payload)[:12]]
    return evidence


def run_feature_sweep(client: BossClient) -> list[CheckResult]:
    """Execute the curated catalog against a live ``BossClient``."""
    actor = getattr(client, "actor", "executive_console") or "executive_console"
    project_id = getattr(client, "project_id", None) or DEFAULT_PROJECT_ID
    probes = build_probes(project_id, actor=actor)
    checks: list[CheckResult] = []

    for probe in probes:
        try:
            status, payload, elapsed = client.request(
                probe.method,
                probe.path,
                query=probe.query,
                body=probe.body,
                authenticate=probe.authenticate,
            )
        except RuntimeError as error:
            checks.append(
                CheckResult(
                    name=probe.name,
                    ok=False,
                    required=probe.required,
                    detail=str(error),
                    status_code=None,
                    duration_ms=0,
                    evidence={"path": probe.path, "method": probe.method},
                    phase=PHASE,
                )
            )
            continue

        ok, detail = evaluate(probe, status, payload)
        checks.append(
            CheckResult(
                name=probe.name,
                ok=ok,
                required=probe.required,
                detail=detail,
                status_code=status,
                duration_ms=elapsed,
                evidence=_evidence(probe, payload),
                phase=PHASE,
            )
        )
    return checks
