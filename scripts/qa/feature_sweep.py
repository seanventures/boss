"""Community operator feature sweep for the unified QA harness.

Phase 1 of ``scripts/qa/harness.py``. Probes a curated settings + features
surface — not every mounted route. Never POSTs spendy or mutating work
(no code-task create, changeset apply, provider/github/cloud easy-connect,
budget reserve, skill/pack import, assurance seed, standup create, preview
fixture create, comms deliver, focused-agent create, agent-process start,
or Metatron session/drive/overwatch).

Community / workforce routes added after the original 55-check catalog are
``required=False``. A 404 on an older live tag is **skip**, not an invented
pass. 500 is fail. 2xx is pass with payload evidence.

``POST /v1/diag/sandbox-echo`` is freeze-honest: 422 frozen or 503 no host
is not a required fail (and does not launch a canary).

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
EXPECT_2XX_OR_405 = "2xx_or_404_or_405"
EXPECT_2XX_OR_503 = "2xx_or_503"
EXPECT_401_UNAUTH = "401_unauth"
# 2xx / 404 old image / 405 GET-on-POST / 422 claims frozen / 503 no host.
EXPECT_FREEZE_HONEST = "freeze_honest"

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
    "/v1/skills/import",
    "/v1/packs/import",
    "/v1/packs/install",
    "/v1/previews/fixture",
    "/v1/assurance/seed-demo",
    "/v1/meetings/standup",
    "/v1/meetings/simulated-run",
    "/v1/comms/outbox",
    "/v1/entitlements/sources",
    "/v1/focused-agents",
    "/v1/agent-processes",
    "/v1/metatron/session",
    "/v1/metatron/goals",
    "/v1/metatron/watch",
    "/v1/metatron/drive",
    "/v1/metatron/overwatch",
    "/v1/process-manager",
)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_SKIP = "skip"

# List keys used by newer workforce payloads (templates/agents/goals).
_WORKFORCE_COUNT_KEYS = (
    "templates",
    "agents",
    "goals",
    "runs",
    "programs",
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
        Probe("settings.resource-receipts", "/v1/resource-receipts", query=q),
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
        # Workforce add-ons after the original 55-check catalog. GET only.
        # required=False: 404 on older live tags is skip, not an invented pass.
        Probe(
            "workforce.focused-agents",
            "/v1/focused-agents",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "workforce.agent-process-templates",
            "/v1/agent-processes/templates",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "workforce.agent-process-runs",
            "/v1/agent-processes/runs",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "workforce.metatron-goals",
            "/v1/metatron/goals",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "workforce.metatron-session",
            "/v1/metatron/session",
            query={**q, "actor": actor},
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        # BOSS-378 PM steward is not mounted yet. Probe presence; 404 = skip.
        Probe(
            "workforce.process-manager",
            "/v1/process-manager",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
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
        # One required safe POST: classify only, never escalate model spend.
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
        # Community routes added after the original 55-check catalog.
        # required=False: 404 on older live tags is not a false red.
        Probe(
            "community.deadletter-remediations",
            "/v1/deadletters/remediations",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "community.sandbox-echo",
            "/v1/diag/sandbox-echo",
            method="POST",
            body={},
            required=False,
            expect=EXPECT_FREEZE_HONEST,
        ),
        Probe(
            "community.skills",
            "/v1/skills",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "community.skills-export",
            "/v1/skills/deployment.health.check/export",
            query={**q, "actor": actor},
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "community.skills-import",
            "/v1/skills/import",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.meetings-standup",
            "/v1/meetings/standup",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.meetings-simulated-run",
            "/v1/meetings/simulated-run",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.assurance-seed-demo",
            "/v1/assurance/seed-demo",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.packs",
            "/v1/packs",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "community.packs-export",
            "/v1/packs/community_edition/export",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "community.packs-import",
            "/v1/packs/import",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.preview-fixture",
            "/v1/previews/fixture",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.comms-outbox",
            "/v1/comms/outbox",
            query=q,
            required=False,
            expect=EXPECT_2XX_OR_404,
        ),
        Probe(
            "community.comms-dry-run",
            "/v1/comms/outbox/_qa_absent/dry-run",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.providers-easy-connect",
            "/v1/providers/easy-connect",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.github-easy-connect",
            "/v1/github/easy-connect",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.cloud-easy-connect",
            "/v1/cloud/easy-connect",
            required=False,
            expect=EXPECT_2XX_OR_405,
        ),
        Probe(
            "community.openai-compatible-source",
            "/v1/entitlements/sources/approved_local_model_host/openai-compatible",
            required=False,
            expect=EXPECT_2XX_OR_405,
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


def _payload_count(payload: Any) -> int | None:
    count = item_count(payload)
    if count is not None:
        return count
    if not isinstance(payload, dict):
        return None
    for key in _WORKFORCE_COUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def classify_verdict(probe: Probe, status: int | None, ok: bool) -> str:
    """Honest pass / fail / skip. 404 on optional probes is skip, never a pass."""
    if not ok or status is None:
        return VERDICT_FAIL
    if probe.required is False and status == 404:
        return VERDICT_SKIP
    if probe.expect == EXPECT_2XX_OR_503 and status == 503:
        return VERDICT_SKIP
    if probe.expect == EXPECT_FREEZE_HONEST and status in (404, 422, 503):
        return VERDICT_SKIP
    return VERDICT_PASS


def evaluate(
    probe: Probe,
    status: int | None,
    payload: Any = None,
) -> tuple[bool, str]:
    """Pure status evaluator used by the live sweep and unit tests."""
    path = probe.path
    method = probe.method
    expect = probe.expect
    count = _payload_count(payload)
    count_bit = f" items={count}" if count is not None else ""

    if status is None:
        return False, f"{method} {path} unreachable"

    if is_2xx(status):
        if expect == EXPECT_401_UNAUTH:
            return False, f"{method} {path} → {status} expected 401/403"
        return True, f"{method} {path} → {status}{count_bit}"

    if expect == EXPECT_2XX_OR_404 and status == 404:
        if probe.required:
            return True, f"{method} {path} → 404 (edition honesty)"
        return True, f"{method} {path} → 404 skip (not mounted / old image)"

    if expect == EXPECT_2XX_OR_405 and status in (404, 405, 422):
        # 404: unmounted / old image, or GET matched a parameterized sibling
        #      (e.g. GET /v1/packs/{id} for path ".../import").
        # 405: POST-only route mounted.
        # 422: GET hit a sibling that requires query/body (647c742 live:
        #      /v1/packs/import and /v1/providers/easy-connect without
        #      project_id). Presence, not a fail. Never POST these.
        reason = {
            404: "old image",
            405: "POST-only mounted",
            422: "GET validation / sibling path",
        }[status]
        return True, f"{method} {path} → {status} ({reason})"

    if expect == EXPECT_2XX_OR_503 and status == 503:
        return True, f"{method} {path} → 503 WARN missing schema"

    if expect == EXPECT_FREEZE_HONEST and status in (404, 405, 422, 503):
        reason = {
            404: "old image",
            405: "POST-only mounted",
            422: "frozen",
            503: "no host",
        }[status]
        return True, f"{method} {path} → {status} freeze-honest {reason}"

    if expect == EXPECT_401_UNAUTH and status in (401, 403):
        return True, f"{method} {path} → {status} fail-closed"

    return False, f"{method} {path} → {status} expected {expect}"


def _evidence(probe: Probe, payload: Any, *, verdict: str, status: int | None) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "method": probe.method,
        "path": probe.path,
        "expect": probe.expect,
        "verdict": verdict,
    }
    if verdict == VERDICT_SKIP:
        evidence["skipped"] = True
    count = _payload_count(payload)
    if count is not None:
        evidence["item_count"] = count
    if isinstance(payload, dict):
        evidence["keys"] = [str(key) for key in list(payload)[:12]]
    if status is not None:
        evidence["status"] = status
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
                    evidence={
                        "path": probe.path,
                        "method": probe.method,
                        "verdict": VERDICT_FAIL,
                    },
                    phase=PHASE,
                )
            )
            continue

        ok, detail = evaluate(probe, status, payload)
        verdict = classify_verdict(probe, status, ok)
        checks.append(
            CheckResult(
                name=probe.name,
                ok=ok,
                required=probe.required,
                detail=detail,
                status_code=status,
                duration_ms=elapsed,
                evidence=_evidence(probe, payload, verdict=verdict, status=status),
                phase=PHASE,
            )
        )
    return checks
