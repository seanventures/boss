"""First-class AI SRE capability ledger.

Maps https://github.com/agamm/awesome-ai-sre categories onto BOSS surfaces.
Honest: missing stays missing. This does not call vendors or invent coverage.
"""
from __future__ import annotations

from typing import Any

AWESOME_AI_SRE = "https://github.com/agamm/awesome-ai-sre"
KERNEL_VERSION = "1.0.0"

# status: shipped | partial | missing
CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "id": "sre_agents",
        "awesome": "AI SRE Agents",
        "status": "partial",
        "surfaces": [
            "/v1/metatron/overwatch",
            "/v1/focused-agents",
            "/v1/reliability/incidents/{id}/diagnose",
        ],
        "gap": "Always-on overwatch does not yet tick reliability.scan/diagnose/page.",
    },
    {
        "id": "production_debugging",
        "awesome": "AI Production Debugging",
        "status": "partial",
        "surfaces": [
            "/v1/reliability/incidents/{id}/diagnose",
            "/v1/reliability/incidents/{id}/remediate",
        ],
        "gap": "No production log/snapshot injection without redeploy.",
    },
    {
        "id": "incident_management",
        "awesome": "Incident Management",
        "status": "partial",
        "surfaces": ["/v1/reliability/incidents", "/v1/oncall-sre/pages"],
        "gap": "Ack/resolve/page exist; evidence-only postmortem artifact is missing.",
    },
    {
        "id": "observability",
        "awesome": "Observability Platforms",
        "status": "partial",
        "surfaces": ["/v1/observability-suite", "/v1/telemetry/connectors/catalog"],
        "gap": "NL query is a plan, not a live PromQL executor.",
    },
    {
        "id": "aiops",
        "awesome": "AIOps Platforms",
        "status": "partial",
        "surfaces": ["/v1/reliability/scan", "/v1/anomalies/scan"],
        "gap": "Clustering lives inside scan; no dedicated noise-reduction view.",
    },
    {
        "id": "log_anomaly",
        "awesome": "Log Analysis and Anomaly Detection",
        "status": "partial",
        "surfaces": ["telemetry_mining", "/v1/activity"],
        "gap": "Model-free templates only; no invented ML scores.",
    },
    {
        "id": "chaos",
        "awesome": "Chaos Engineering",
        "status": "partial",
        "surfaces": ["/v1/chaos/run"],
        "gap": "Named scenarios only; no LLM-designed blast radius.",
    },
    {
        "id": "runbooks",
        "awesome": "Runbook Automation",
        "status": "missing",
        "surfaces": ["/v1/ai-sre/runbooks"],
        "gap": "Incident-attached, approval-gated runbooks are not first-class.",
    },
    {
        "id": "cost",
        "awesome": "Cloud Cost Optimization",
        "status": "partial",
        "surfaces": ["/v1/cost-insights", "budget"],
        "gap": "Recommendations only; no autonomous rightsizing.",
    },
    {
        "id": "llm_devops",
        "awesome": "LLM-Powered DevOps Tools",
        "status": "partial",
        "surfaces": ["scripts/reliability_mcp_server.py", "software factory"],
        "gap": "MCP exposes list/diagnose/capabilities/verify/page; no live vendor notify.",
    },
    {
        "id": "sre_bench",
        "awesome": "Agent Benchmarks",
        "status": "partial",
        "surfaces": ["/v1/ai-sre/bench"],
        "gap": "Deterministic InvestigationEngine fixtures only; not a vendor bench.",
    },
)


def capability_ledger() -> dict[str, Any]:
    items = [dict(row) for row in CAPABILITIES]
    counts = {"shipped": 0, "partial": 0, "missing": 0}
    for item in items:
        status = str(item.get("status") or "missing")
        if status not in counts:
            status = "missing"
            item["status"] = status
        counts[status] += 1
    return {
        "product": "boss_ai_sre",
        "version": KERNEL_VERSION,
        "source": AWESOME_AI_SRE,
        "public_ga": False,
        "counts": counts,
        "capabilities": items,
        "loop": [
            "scan",
            "cluster",
            "diagnose",
            "page",
            "remediate",
            "verify",
            "postmortem",
        ],
        "rules": [
            "Never invent RCA confidence or scores.",
            "Remediation is a governed PR, never a silent production write.",
            "External notify without credentials is dry-run / fail-closed.",
            "Decisions and deadletters never auto-apply.",
        ],
    }


def capability_by_id(capability_id: str) -> dict[str, Any] | None:
    wanted = str(capability_id or "").strip()
    for item in CAPABILITIES:
        if item["id"] == wanted:
            return dict(item)
    return None
