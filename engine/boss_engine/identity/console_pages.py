"""What opening each console page needs: the Python copy of the console's own declarations
(apps/web/lib/identity/pages.ts IDENTITY_PAGES and CONSOLE_PAGES, areas.ts AREA_ACCESS), for the
role editor's impact preview ("which pages they gain or lose", IDENTITY.md §6.10). The API image
does not ship the console's sources, so the table lives here and
apps/api/tests/test_identity_role_impact_counts.py fails when it drifts from the console's.

One row per page that needs a permission: (path, or `area:<id>` for an area of the console shell;
label; permission) — `{ permission }` pages and `{ anyScope: permission }` pages (held at the org or
any project) alike. Public and session-only pages are not listed (no role changes them).
"""
from __future__ import annotations

CONSOLE_PAGES: tuple[tuple[str, str, str], ...] = (
    ("/identity", "Overview", "directory:read"),
    ("/identity/people", "People", "user:read"),
    ("/identity/people/[id]", "Person", "user:read"),
    # `{ anyScope: permission }`: opens when the permission is held at the org OR any project
    ("/identity/requests", "Access requests", "role_binding:assign"),
    ("/identity/groups", "Groups", "group:read"),
    ("/identity/groups/[id]", "Group", "group:read"),
    ("/identity/seats", "Approval seats", "role_binding:read"),
    ("/identity/roles", "Access roles", "access_role:read"),
    ("/identity/roles/[id]", "Access role", "access_role:read"),
    ("/identity/service-accounts", "Service accounts", "service_account:read"),
    ("/identity/agents", "Agents", "agent_passport:read"),
    ("/identity/agents/[id]", "Passport", "agent_passport:read"),
    ("/identity/app-sign-in", "App sign-in", "oidc_client:read"),  # anyScope
    ("/identity/sign-in", "Sign-in & SSO", "identity_provider:read"),
    ("/identity/audit", "Audit", "audit:read"),
    ("/account/keys", "API keys", "api_key:manage_own"),
    ("/v2", "Factory", "code_task:read"),
    ("/v2/missions", "Factory", "code_task:read"),
    ("/v2/missions/[id]", "Build", "code_task:read"),
    ("/v2/hive", "Hive", "assistant:chat"),
    ("/v2/factory", "Agent Studio", "agent:read"),
    ("/v2/org", "Team structure", "agent:read"),
    ("/v2/panels", "Approvals", "decision:read"),
    ("/v2/boss", "Workflows", "workflow:read"),
    ("/v2/timeline", "Receipts", "activity:read"),
    ("/v2/infra", "Infra & Models", "infra:read"),
    ("/v2/settings", "Settings", "setup:read"),
    ("/github-app", "GitHub App", "setup:read"),
    ("area:overview", "Overview", "project:read"),
    ("area:code_tasks", "Code tasks", "code_task:read"),
    ("area:delivery_workflows", "Delivery workflows", "workflow:read"),
    ("area:workforce", "Workforce", "agent:read"),
    ("area:decisions", "Decisions", "decision:read"),
    ("area:infra", "Infra", "infra:read"),
    ("area:observability", "Observability", "observability:read"),
    ("area:activity", "Activity", "activity:read"),
    ("area:discovery", "Discovery", "discovery:read"),
    ("area:meetings", "Meetings", "meeting:read"),
    ("area:agent", "Agent", "assistant:chat"),
    ("area:dashboard", "Dashboard", "work_item:read"),
    ("area:work", "Work", "work_item:read"),
    ("area:portfolio", "Portfolio", "work_item:read"),
    ("area:processes", "Processes", "autopilot:read"),
    ("area:shepherd", "Shepherd", "autopilot:read"),
    ("area:reliability", "Reliability", "reliability:read"),
    ("area:ai-sre", "AI SRE", "reliability:read"),
    ("area:reports", "Reports", "activity:read"),
    ("area:setup", "Setup", "setup:read"),
    ("area:skills", "Skills", "skill:read"),
    ("area:budget", "Budget", "budget:read"),
    ("area:comms", "Comms", "comms:read"),
    ("area:ops", "Ops", "system:read"),
    ("area:assurance", "Assurance", "assurance:read"),
    ("area:org", "Org", "pack:read"),
    ("area:github", "Github", "github:read"),
    ("area:capabilities", "Capabilities", "skill:read"),
    ("area:sandboxes", "Sandboxes", "sandbox:read"),
    ("area:foundations", "Foundations", "system:read"),
    ("area:components", "Components", "system:read"),
    ("area:enterprise", "Enterprise", "directory:read"),
)


def pages_for(permissions: frozenset[str] | set[str]) -> list[str]:
    """The labels of the pages `permissions` open (each label once, in table order)."""
    out: list[str] = []
    for _path, label, permission in CONSOLE_PAGES:
        if permission in permissions and label not in out:
            out.append(label)
    return out
