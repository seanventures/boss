import logging
import re

from sqlalchemy import text

from .config import get_settings
from .db import ensure_schema, get_engine
from .services.core import emit_event, make_id
from .services.agent_studio import AgentStudioService
from .services.a2a import A2AService
from .services.entitlements import EntitlementBrokerService
from .services.event_subscription_service import seed_default_event_subscriptions
from .services.feature_catalog import FeatureCatalog
from .services.mcp_registry import ensure_mcp_server
from .services.members import MemberDirectoryService
from .services.skill_registry import seed_system_skills
from .services.specpack import SpecPackService
from .services.toolchain_registry import ToolchainRegistryService
from .services.workflows import seed_specpack_workflows
from .services.work_orchestration import WorkOrchestrationService

_log = logging.getLogger(__name__)


# Built-in MCP servers BOSS ships with — idempotently registered so they're
# always present in the capability catalog (the processes are launched
# separately, e.g. via tools/mcp_*_server.py).
BUILTIN_MCP_SERVERS = [
    {
        "name": "boss-devops-reference",
        "url": "http://127.0.0.1:8765/mcp",
        "risk_tier": 1,
        "data_boundary": "public",
        "description": "External DevOps reference data: cloud_reference_pricing, engineering_runbook",
    },
    {
        "name": "web-intel",
        "url": "http://127.0.0.1:8770/mcp",
        "risk_tier": 2,
        "data_boundary": "public",
        "description": "Render & study external websites + screenshot HTML mockups with Playwright: fetch_page, study_site, render_mockup",
    },
    {
        "name": "doc-render",
        "url": "http://127.0.0.1:8771/mcp",
        "risk_tier": 1,
        "data_boundary": "internal",
        "description": "Render Markdown to PDF: render_markdown_pdf",
    },
]


def seed_project_defaults(
    connection,
    settings,
    project_id,
    *,
    entitlements,
    members,
    specpack,
    actor="bootstrap",
    budget_policy_id=None,
):
    """Seed the COMMON per-project defaults every org/project gets: a budget
    policy, default members, the core pack + default agent modules, entitlement
    sources, and the built-in MCP servers. Idempotent. Shared by bootstrap and
    by onboarding a new organization so every org starts from the same template."""
    bp_id = budget_policy_id or f"bp_{project_id}"
    connection.execute(
        text(
            """
            INSERT INTO budget_policies (
                id, scope_type, scope_id, daily_cap_usd_micros, monthly_cap_usd_micros,
                burst_cap_usd_micros_per_min, max_concurrent_jobs, max_workers_total,
                allowed_model_tiers, soft_cap_pct, auto_lite_mode_pct,
                freeze_provisioning_pct, freeze_tier2_pct
            )
            VALUES (
                :bp_id, 'project', :scope_id, :daily_cap, :monthly_cap, :burst_cap,
                3, 2, '["gpt-5.2-codex"]'::jsonb, 0.70, 0.85, 0.90, 0.95
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "bp_id": bp_id,
            "scope_id": project_id,
            "daily_cap": settings.daily_cap_usd_micros,
            "monthly_cap": settings.monthly_cap_usd_micros,
            "burst_cap": settings.burst_cap_usd_micros_per_min,
        },
    )
    members.seed_default_members(connection, project_id, actor)
    specpack.install_pack(connection, project_id, settings.core_pack_id, actor)
    specpack.install_default_agent_modules(connection, project_id, actor)
    # THE WORKFLOWS A DIRECTIVE IS ROUTED TO. Without them `Build` answers 409 "No approved
    # workflow mapping matched this class of work" — the core feature failing on a clean
    # install, which is exactly what happened here. The API also seeds at boot for the default
    # project; doing it HERE means any project that is ever seeded gets it, rather than only
    # the one the container was told about.
    try:
        from .services.workflow_catalogue import seed as seed_workflows

        seed_workflows(connection, project_id, actor=actor)
    except Exception as exc:  # noqa: BLE001 — a project must still be created without it
        _log.warning("delivery catalogue not seeded for %s: %s", project_id, exc)
    diagnostics = []
    for module_id in specpack.DEFAULT_AGENT_MODULE_IDS:
        module = specpack.load_agent_module_spec(module_id)
        role_key, module_diagnostics = (
            specpack.skill_reference_diagnostics_for_module(module)
        )
        diagnostics.extend(
            {
                **item,
                "module_id": module_id,
                "role_key": role_key,
            }
            for item in module_diagnostics
        )
    if diagnostics:
        emit_event(
            connection,
            project_id,
            "rolespec.skill_reference_diagnostics",
            actor,
            "project",
            project_id,
            {
                "count": len(diagnostics),
                "unresolved": sum(
                    item["status"] == "unresolved" for item in diagnostics
                ),
                "legacy_aliases": sum(
                    item["status"] == "legacy_alias" for item in diagnostics
                ),
                "items": diagnostics,
            },
        )
    entitlements.seed_default_sources(connection, project_id, actor)
    seed_system_skills(
        connection,
        project_id,
        entitlements.list_skills(),
        actor,
    )
    ToolchainRegistryService(settings).seed_sources(connection, project_id, actor)
    # seed the meeting role-agent template so a new org's panel isn't empty
    from .services.meeting_service import seed_roles
    seed_roles(connection, project_id)
    for srv in BUILTIN_MCP_SERVERS:
        ensure_mcp_server(
            connection,
            project_id,
            srv["name"],
            srv["url"],
            risk_tier=srv["risk_tier"],
            data_boundary=srv["data_boundary"],
            description=srv["description"],
            publisher={"id": "boss", "name": "BOSS", "kind": "system"},
            provenance={
                "source": "builtin",
                "version": "1",
                "trusted": True,
            },
        )
    seed_default_event_subscriptions(connection, project_id)
    FeatureCatalog(settings).seed_from_traceability(connection, project_id)
    seed_specpack_workflows(
        connection,
        project_id=project_id,
        workflow_root=settings.specpack_root / "workflows",
        actor=actor,
    )
    AgentStudioService(settings).seed_catalog(connection, project_id, actor)
    WorkOrchestrationService(settings).seed_defaults(connection, project_id, actor)
    a2a = A2AService(settings)
    for channel in (
        {
            "channel_key": "coordination",
            "name": "Work coordination",
            "description": "Instructions, status, questions, answers, task offers, and resource coordination.",
            "allowed_message_kinds": [
                "instruction", "status", "question", "answer", "task_offer",
                "task_accept", "resource_request", "resource_grant", "cancel", "error",
            ],
        },
        {
            "channel_key": "evidence",
            "name": "Evidence and decisions",
            "description": "Artifacts, evidence, and explicit decision handoffs.",
            "allowed_message_kinds": [
                "artifact", "evidence", "decision_request", "decision_result", "error",
            ],
        },
        {
            "channel_key": "telemetry",
            "name": "Runtime telemetry",
            "description": "Job, agent, panel, worker, and transport status for operations visibility.",
            "allowed_message_kinds": ["status", "metric", "heartbeat", "error"],
        },
    ):
        a2a.create_channel(
            connection,
            project_id=project_id,
            description=channel["description"],
            allowed_message_kinds=channel["allowed_message_kinds"],
            allowed_schema_versions={"boss.a2a": [1]},
            retention_seconds=604800,
            max_payload_bytes=settings.a2a_max_payload_bytes,
            actor=actor,
            channel_key=channel["channel_key"],
            name=channel["name"],
        )


def onboard_org(connection, settings, name, *, entitlements, members, specpack, slug=None):
    """Create a new organization (org → workspace → project) and seed it with the
    common defaults. Returns the new ids. Used by the admin onboarding endpoint."""
    org_id = make_id("org")
    workspace_id = make_id("ws")
    project_id = make_id("proj")
    base_slug = re.sub(r"[^a-z0-9]+", "-", (slug or name or "org").lower()).strip("-") or "org"
    project_slug = f"{base_slug}-main"
    connection.execute(
        text("INSERT INTO orgs (id, name) VALUES (:id, :name)"),
        {"id": org_id, "name": name},
    )
    connection.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, :name)"),
        {"id": workspace_id, "org": org_id, "name": "Default workspace"},
    )
    connection.execute(
        text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :ws, :name, :slug)"),
        {"id": project_id, "ws": workspace_id, "name": f"{name} workspace", "slug": project_slug},
    )
    seed_project_defaults(
        connection, settings, project_id,
        entitlements=entitlements, members=members, specpack=specpack, actor="onboarding",
    )
    return {"org_id": org_id, "workspace_id": workspace_id, "project_id": project_id, "name": name}


def seed() -> None:
    settings = get_settings()
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    engine = get_engine(settings)
    entitlements = EntitlementBrokerService(settings)
    members = MemberDirectoryService(settings)
    specpack = SpecPackService(settings)
    ensure_schema(engine, settings)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO orgs (id, name)
                VALUES (:id, 'BOSS')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": settings.default_org_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, org_id, name)
                VALUES (:id, :org_id, 'Dogfood')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": settings.default_workspace_id, "org_id": settings.default_org_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:id, :workspace_id, 'BOSS dogfood bootstrap', 'boss-dogfood')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": settings.default_project_id,
                "workspace_id": settings.default_workspace_id,
            },
        )
        seed_project_defaults(
            connection, settings, settings.default_project_id,
            entitlements=entitlements, members=members, specpack=specpack,
            budget_policy_id="bp_boss_default",
        )


def main() -> None:
    seed()
    print("BOSS bootstrap seed complete.")


if __name__ == "__main__":
    main()
