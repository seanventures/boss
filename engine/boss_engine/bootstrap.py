import logging
import os
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


# Built-in MCP servers BOSS ships with — idempotently registered so they're always present in
# the capability catalog. They are SERVED by the `mcp-tools` compose service
# (tools/mcp_tools_mux.py, scripts/install/community-compose.yml), so the seeded address is that
# service's, never 127.0.0.1: inside the API container 127.0.0.1 is the API itself, and every
# install that seeded the loopback address opened on three dead servers (skills-mcp spec RC3).
# `BOSS_MCP_TOOLS_HOST` names the host for a BOSS run outside compose (a developer's machine).
# test_seeded_mcp_servers_are_in_compose keeps every seeded host a compose service.
MCP_TOOLS_HOST = os.environ.get("BOSS_MCP_TOOLS_HOST", "mcp-tools").strip() or "mcp-tools"


def mcp_tools_ports() -> tuple[int, int, int]:
    """(devops-reference, web-intel, doc-render) ports; BOSS_MCP_TOOLS_PORTS overrides them for a
    machine where the defaults are taken. tools/mcp_tools_mux.py reads the same variable."""
    raw = os.environ.get("BOSS_MCP_TOOLS_PORTS", "").strip()
    try:
        ports = tuple(int(p) for p in raw.split(",")) if raw else (8765, 8770, 8771)
    except ValueError:
        ports = (8765, 8770, 8771)
    return ports if len(ports) == 3 else (8765, 8770, 8771)  # type: ignore[return-value]


_DEVOPS_PORT, _WEB_INTEL_PORT, _DOC_RENDER_PORT = mcp_tools_ports()

#: The release's own review of the servers it ships, recorded as such (not as a person's).
FIRST_PARTY_REVIEW = "boss (first-party)"

#: Bumped when the seeded rows change; a row on an older version, untouched by any person, is
#: brought up to date on the next boot (`migrate_builtin_mcp_servers`).
BUILTIN_MCP_VERSION = "2"

BUILTIN_MCP_SERVERS = [
    {
        "name": "boss-devops-reference",
        "url": f"http://{MCP_TOOLS_HOST}:{_DEVOPS_PORT}/mcp",
        "risk_tier": 0,
        "data_boundary": "public",
        "description": "External DevOps reference data: cloud_reference_pricing, engineering_runbook",
    },
    {
        "name": "web-intel",
        "url": f"http://{MCP_TOOLS_HOST}:{_WEB_INTEL_PORT}/mcp",
        # READS ONLY: fetching, studying and searching public pages and rendering a mockup change
        # nothing anywhere. Tier 2 made every call wait for a person.
        "risk_tier": 0,
        "data_boundary": "public",
        "description": "Render, study and search external websites, and screenshot HTML mockups: "
                       "fetch_page, study_site, search_web, render_mockup",
    },
    {
        "name": "doc-render",
        "url": f"http://{MCP_TOOLS_HOST}:{_DOC_RENDER_PORT}/mcp",
        "risk_tier": 0,
        "data_boundary": "internal",
        "description": "Render Markdown to PDF: render_markdown_pdf",
    },
]


def builtin_provenance() -> dict:
    return {"source": "builtin", "version": BUILTIN_MCP_VERSION, "trusted": True,
            "reviewed_by": FIRST_PARTY_REVIEW}


def migrate_builtin_mcp_servers(connection, project_id) -> list[str]:
    """Bring the servers BOSS seeded up to the current release: the `mcp-tools` address, the
    tier, and the first-party review. Returns the names moved.

    ONLY ROWS NO PERSON TOUCHED: provenance source "builtin" (an edit makes it
    "builtin_edited") and no `edited_by`. A server an operator re-pointed or removed stays as
    they left it (seed_exclusions is asked by ensure_mcp_server). The recorded reachability is
    cleared, because it described the old address."""
    import json as _json

    moved: list[str] = []
    wanted = {s["name"].lower(): s for s in BUILTIN_MCP_SERVERS}
    rows = connection.execute(
        text("SELECT id, name, url, risk_tier, provenance FROM mcp_servers "
             "WHERE project_id = :p"), {"p": project_id}).mappings().all()
    for row in rows:
        srv = wanted.get(str(row["name"]).lower())
        if srv is None:
            continue
        provenance = row["provenance"] if isinstance(row["provenance"], dict) else \
            _json.loads(row["provenance"] or "{}")
        if provenance.get("source") != "builtin" or provenance.get("edited_by"):
            continue
        if (row["url"] == srv["url"] and int(row["risk_tier"]) == srv["risk_tier"]
                and provenance.get("version") == BUILTIN_MCP_VERSION):
            continue
        from .services.mcp_registry import migrate_seeded_server

        migrate_seeded_server(connection, project_id, str(row["id"]), url=srv["url"],
                              risk_tier=srv["risk_tier"], description=srv["description"],
                              provenance={**provenance, **builtin_provenance(),
                                          "seeded_url_before": row["url"]})
        emit_event(connection, project_id, "mcp.builtin_server_migrated", "bootstrap",
                   "mcp_server", str(row["id"]), {"name": row["name"], "from": row["url"],
                                                   "to": srv["url"]})
        moved.append(str(row["name"]))
    return moved


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
    by onboarding a new organization so every org starts from the same template.

    EVERY AGENT IT TOUCHES IS SEALED ONCE, at the end (agent_identity.sealing_once): a role
    installed and then given eight skills and two MCP servers is v1, not v11.
    """
    from .services.agent_identity import sealing_once

    with sealing_once():
        _seed_project_defaults(
            connection, settings, project_id, entitlements=entitlements, members=members,
            specpack=specpack, actor=actor, budget_policy_id=budget_policy_id)


def _seed_project_defaults(
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
    # AND GIVE THE AGENTS THEIR SKILLS. Roles declare `allowed_skills`; the runtime grants on
    # `role_skills`; nothing joined them, so every install had agents holding nothing. Runs
    # after seed_system_skills because it can only assign skills the project actually has.
    from .services.skill_definitions import sync_role_skills

    try:
        sync_role_skills(connection, project_id=project_id, actor=actor)
    except Exception:  # noqa: BLE001 — a fresh project must still come up
        _log.warning("role_skill_sync_failed", exc_info=True)
    try:
        from .services.knowledge.grants import grant_knowledge_roles

        grant_knowledge_roles(connection, project_id=project_id, actor=actor)
    except Exception:  # noqa: BLE001 — a fresh project must still come up
        _log.warning("knowledge_role_grant_failed", exc_info=True)

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
            provenance=builtin_provenance(),
            # A re-seed must not undo the operator: a server they edited keeps their URL and
            # tier, and one they removed stays removed. See ensure_mcp_server.
            respect_operator=True,
        )
    # AND THE MCP SERVERS THEIR ROLES NAME (permissions.mcp_servers), now that the built-in
    # servers exist. The seed's half of the MCP grant: additive, and a server an operator took
    # off an agent stays off (agents.sync_role_mcp asks seed_exclusions).
    from .services.agents import sync_role_mcp

    try:
        with connection.begin_nested():
            sync_role_mcp(connection, project_id=project_id, actor=actor)
    except Exception:  # noqa: BLE001 — a fresh project must still come up
        _log.warning("role_mcp_sync_failed", exc_info=True)
    # THE WHOLE SHIPPED CAST, AND EVERY AGENT IS SOMEBODY: the archetypes and any role of a
    # baked pack this project has that the core install did not cover, their grants, and an
    # agt_ id, a birth and a version for any role that predates Agent World. The same function
    # the API runs on every boot for every project (shipped_cast.sync_project; idempotent).
    from .services.shipped_cast import sync_project as sync_shipped_cast

    try:
        with connection.begin_nested():
            sync_shipped_cast(connection, settings, project_id, specpack=specpack, actor=actor)
    except Exception:  # noqa: BLE001
        _log.warning("shipped_cast_sync_failed", exc_info=True)
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


def _install_builtin_templates(connection, org_id: str) -> None:
    """An org gets the shipped templates when it is CREATED, so reading its template list later
    writes nothing (a GET stays a GET). Never fails the seed: a template that cannot be read is
    the template validator's report, not a broken install."""
    try:
        from .templates.service import sync_builtins

        with connection.begin_nested():
            sync_builtins(connection, org_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[seed] built-in templates not installed for {org_id}: {type(exc).__name__}")


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
    _install_builtin_templates(connection, org_id)
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
        _install_builtin_templates(connection, settings.default_org_id)
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, org_id, name)
                VALUES (:id, :org_id, 'Default workspace')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": settings.default_workspace_id, "org_id": settings.default_org_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:id, :workspace_id, 'Default project', 'boss-dogfood')
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
