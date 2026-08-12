"""API capability catalog — introspect exposed HTTP surface for operators/agents.

This is the machine-readable map of "what can BOSS control over the API".
It groups routes by domain and links them to product areas.
"""

from __future__ import annotations

from typing import Any, Optional


# Domain taxonomy used by docs + /v1/meta/api-catalog
API_DOMAINS: dict[str, dict[str, Any]] = {
    "meta": {
        "title": "Meta & discovery",
        "description": "Edition, health, OpenAPI, API catalog, capabilities.",
        "prefixes": ["/health", "/v1/edition", "/v1/meta", "/v1/capabilities", "/openapi"],
    },
    "auth": {
        "title": "Auth & identity",
        "description": "Sessions, OIDC/SAML, SCIM, API keys, members, roles.",
        "prefixes": [
            "/v1/auth",
            "/v1/members",
            "/v1/roles",
            "/v1/org-members",
            "/saml/",
            "/scim/",
        ],
    },
    "core": {
        "title": "Core control plane",
        "description": "Commands, tasks, plans, decisions, jobs, receipts, events, budget.",
        "prefixes": [
            "/v1/commands",
            "/v1/tasks",
            "/v1/plans",
            "/v1/decisions",
            "/v1/jobs",
            "/v1/receipts",
            "/v1/events",
            "/v1/budget",
            "/v1/deadletters",
        ],
    },
    "agents": {
        "title": "Agents & skills",
        "description": "Agent studio, runs, chat, skills, toolchains, A2A, shepherd.",
        "prefixes": [
            "/v1/agent-studio",
            "/v1/agent-runs",
            "/v1/agent/",
            "/v1/agents",
            "/v1/skills",
            "/v1/toolchain",
            "/v1/a2a",
            "/v1/shepherd",
            "/v1/agent-groups",
            "/v1/engine/agent",
        ],
    },
    "delivery": {
        "title": "Software factory & delivery",
        "description": "Code tasks, GitHub, delivery, previews, sandboxes, CI/CD, deployments.",
        "prefixes": [
            "/v1/code-tasks",
            "/v1/github",
            "/v1/delivery",
            "/v1/previews",
            "/v1/sandboxes",
            "/v1/software-factory",
            "/v1/deployment",
            "/v1/workflow-runs",
            "/v1/repo",
            "/v1/ci-config",
            "/v1/pipeline-config",
            "/v1/pipeline-adapters",
            "/v1/verification-target",
            "/v1/design-prompts",
            "/v1/design-routing",
            "/v1/verifier-debate",
        ],
    },
    "providers": {
        "title": "ProviderOps & multi-cloud",
        "description": "Provider accounts, changesets, infra, elastic fabric, multi-cloud catalog.",
        "prefixes": [
            "/v1/providers",
            "/v1/provider",
            "/v1/changesets",
            "/v1/infra",
            "/v1/elastic",
            "/v1/multi-cloud",
            "/v1/provider-usage",
            "/v1/finops",
            "/v1/resource",
            "/v1/worker-pools",
            "/v1/autoscaler",
            "/v1/aws",
        ],
    },
    "goldeneye": {
        "title": "Goldeneye bare-metal PaaS",
        "description": "Nodes, modules/packages, app deploys, fleet status.",
        "prefixes": ["/v1/goldeneye"],
    },
    "work": {
        "title": "Work orchestration & trackers",
        "description": "Work items, trackers (Plane/Jira/Linear/GitHub/ADO), allocations.",
        "prefixes": ["/v1/work-trackers", "/v1/work-orchestration", "/v1/work"],
    },
    "cloud": {
        "title": "Cloud product & editions",
        "description": "Easy Connect, entitlements, account connect, commercial.",
        "prefixes": [
            "/v1/cloud",
            "/v1/account-connect",
            "/v1/entitlements",
            "/v1/commercial",
            "/v1/license",
        ],
    },
    "governance": {
        "title": "Assurance & governance",
        "description": "Assurance metrics/cases, integration registry, agent groups, SOC2.",
        "prefixes": [
            "/v1/assurance",
            "/v1/integration-registry",
            "/v1/enterprise",
            "/v1/break-glass",
            "/v1/policy",
            "/v1/independence",
        ],
    },
    "models": {
        "title": "Model gateway",
        "description": "Model leases, invocations, credentials, usage, llm_calls ledger.",
        "prefixes": [
            "/v1/model-gateway",
            "/v1/models",
            "/v1/llm-calls",
            "/v1/token-efficiency",
        ],
    },
    "ops": {
        "title": "Ops, reliability, observability",
        "description": "Health, reliability, chaos, canaries, dashboard, QA, reports.",
        "prefixes": [
            "/v1/reliability",
            "/v1/chaos",
            "/v1/canaries",
            "/v1/dashboard",
            "/v1/qa",
            "/v1/reports",
            "/v1/anomalies",
            "/v1/diag",
            "/v1/evals",
            "/v1/mcp",
            "/v1/webhook",
            "/v1/comms",
            "/v1/meetings",
            "/v1/self-improvement",
            "/v1/improvement-programs",
            "/v1/telemetry",
            "/v1/change-correlation",
            "/v1/redaction",
            "/v1/platform-standards",
            "/v1/oncall-sre",
            "/v1/oncall",
            "/v1/observability-suite",
            "/v1/observability",
            "/v1/security-scan",
            "/v1/worker-autoscaling",
        ],
    },
    "identity": {
        "title": "Identity providers",
        "description": "IdP catalog, OIDC/SAML/SCIM blueprints for top enterprise providers.",
        "prefixes": [
            "/v1/identity-providers",
            "/v1/enterprise",
            "/v1/auth",
            "/saml/v2",
            "/scim/v2",
        ],
    },
    "delivery_cicd": {
        "title": "CI/CD & release planning",
        "description": "Multi-vendor pipelines, release boards, feature status.",
        "prefixes": [
            "/v1/cicd",
            "/v1/ci-config",
            "/v1/release-planning",
            "/v1/features",
            "/v1/delivery",
            "/v1/pipeline-config",
            "/v1/pipeline-adapters",
        ],
    },
    "knowledge": {
        "title": "Knowledge & learning",
        "description": "Context packing, knowledge graph, learning loop, feature catalog.",
        "prefixes": [
            "/v1/knowledge",
            "/v1/learning",
            "/v1/features",
            "/v1/capability-manifest",
            "/v1/intent",
            "/v1/platform-standards",
        ],
    },
}


def classify_path(path: str) -> str:
    for domain, meta in API_DOMAINS.items():
        for prefix in meta["prefixes"]:
            if path == prefix or path.startswith(prefix):
                return domain
    if path.startswith("/v1/"):
        return "other"
    return "system"


def build_api_catalog(
    routes: list[dict[str, Any]],
    *,
    openapi_paths: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Build domain-grouped catalog from route descriptors.

    Each route: {method, path, name, tags?, summary?}
    """
    openapi_paths = openapi_paths or set()
    by_domain: dict[str, list[dict[str, Any]]] = {k: [] for k in API_DOMAINS}
    by_domain["other"] = []
    by_domain["system"] = []

    for route in routes:
        path = str(route.get("path") or "")
        method = str(route.get("method") or "GET").upper()
        domain = classify_path(path)
        entry = {
            "method": method,
            "path": path,
            "name": route.get("name"),
            "summary": route.get("summary") or route.get("name"),
            "tags": route.get("tags") or [],
            "in_openapi": path in openapi_paths
            or any(
                _path_template_match(path, op) for op in openapi_paths
            ),
            "domain": domain,
        }
        by_domain.setdefault(domain, []).append(entry)

    domains_out = []
    total = 0
    documented = 0
    for key, meta in list(API_DOMAINS.items()) + [
        ("other", {"title": "Other /v1", "description": "Unclassified v1 routes."}),
        ("system", {"title": "System", "description": "Non-v1 system routes."}),
    ]:
        items = sorted(
            by_domain.get(key) or [],
            key=lambda r: (r["path"], r["method"]),
        )
        if not items and key in ("other", "system"):
            continue
        total += len(items)
        doc = sum(1 for i in items if i.get("in_openapi"))
        documented += doc
        domains_out.append(
            {
                "id": key,
                "title": meta["title"],
                "description": meta["description"],
                "route_count": len(items),
                "documented_count": doc,
                "routes": items,
            }
        )

    # Cheap operator note: which catalog domains currently expose mounted routes.
    services_with_routes = [
        {
            "id": d["id"],
            "title": d["title"],
            "route_count": d["route_count"],
            "documented_count": d["documented_count"],
        }
        for d in domains_out
        if d.get("route_count", 0) > 0
    ]

    return {
        "version": 1,
        "product": "boss",
        "domain_count": len(domains_out),
        "route_count": total,
        "documented_route_count": documented,
        "documentation_coverage": (
            round(documented / total, 4) if total else 0.0
        ),
        "services_with_routes": services_with_routes,
        "domains": domains_out,
        "openapi_url": "/openapi.json",
        "docs": {
            "markdown_index": "docs/api/README.md",
            "openapi_yaml": "api/openapi.yaml",
            "product_guide": "docs/product/API_SURFACE.md",
        },
    }


def _path_template_match(concrete: str, template: str) -> bool:
    """Match /v1/foo/bar vs /v1/foo/{id} loosely."""
    if concrete == template:
        return True
    c_parts = concrete.strip("/").split("/")
    t_parts = template.strip("/").split("/")
    if len(c_parts) != len(t_parts):
        return False
    for c, t in zip(c_parts, t_parts):
        if t.startswith("{") and t.endswith("}"):
            continue
        if c != t:
            return False
    return True
